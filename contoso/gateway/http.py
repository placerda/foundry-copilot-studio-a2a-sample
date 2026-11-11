"""Bounded Gateway routes; signed JWT claims are the sole caller/role source."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import json
import logging
import math
import re
import time
from typing import Protocol
from urllib.parse import parse_qsl
from urllib.parse import urlsplit
from uuid import uuid4

from contoso_backend.errors import ChatFailure
from contoso.connections import (
    AuthorizationStart, ConnectionDenied, ConnectionError, ConnectionRequired, ConnectionUnavailable,
    DelegatedConnectionBroker,
)
from contoso.connections.oauth import CONNECTION_USE, DEPLOYMENT_ADMIN, HARNESS_SCOPE, STANDARD_SCOPE
from contoso.identity import CallerIdentity, EntraAuthenticator
from contoso.router.runtime import RuntimeResult, ScopedToken

from .settings import GatewaySettings


_CALLBACK_FIELDS = frozenset({
    "state", "code", "error", "session_state", "error_description", "error_uri", "client_info", "iss",
})


class NetworkClient(Protocol):
    async def invoke(
        self, *, question: str, studio_variant: str, integration: str,
    ) -> RuntimeResult | Mapping: ...


NetworkDependency = NetworkClient | Callable[..., Awaitable[RuntimeResult | Mapping]]


@dataclass(frozen=True)
class GatewayResponse:
    status_code: int
    body: bytes = field(repr=False)
    headers: Mapping[str, str]


class _Failure(Exception):
    def __init__(self, status: int, code: str, message: str, validation_stage=None):
        self.status, self.code, self.message = status, code, message
        self.validation_stage = validation_stage


def _invalid(stage="request_format"):
    return _Failure(400, "invalid_request", "Invalid request format.", stage)


def _unavailable():
    return _Failure(503, "dependency_unavailable", "The service is temporarily unavailable.")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid("duplicate_field")
        result[key] = value
    return result


def _text(value, *, chars=256, size=1024, empty=False):
    if not isinstance(value, str) or len(value) > chars or (not empty and not value.strip()):
        raise _invalid()
    try:
        if len(value.encode("utf-8", errors="strict")) > size:
            raise _invalid()
    except UnicodeEncodeError:
        raise _invalid() from None
    if any(ord(c) < 32 and c not in "\t\n\r" for c in value):
        raise _invalid()
    return value


def validate_runtime_result(payload) -> dict:
    """Validate the bounded RuntimeResult JSON contract without reflecting input.

    A structurally valid unavailable result remains unavailable: the Gateway maps
    it to HTTP 503. No claims about source content or authentication are inferred.
    """
    try:
        keys = {"status", "answer", "provider_id", "studio_variant", "sources",
                "citations", "execution_path"}
        if not isinstance(payload, Mapping) or set(payload) != keys:
            raise _invalid()
        status = payload["status"]
        if not isinstance(status, str) or status not in (
            "answered", "insufficient-evidence", "unavailable", "no-matching-capability",
        ):
            raise _invalid()
        _text(payload["answer"], chars=12000, size=48000)
        provider = payload["provider_id"]
        if provider is not None:
            _text(provider, chars=128, size=128)
        variant = payload["studio_variant"]
        if variant is not None and (not isinstance(variant, str)
                                    or variant not in ("standard", "copilot-harness")):
            raise _invalid()
        if status in ("answered", "insufficient-evidence") and provider is None:
            raise _invalid()
        if provider is None and variant is not None:
            raise _invalid()
        if status == "no-matching-capability" and (provider is not None or variant is not None):
            raise _invalid()
        if (not isinstance(payload["sources"], list) or len(payload["sources"]) > 16
                or not isinstance(payload["citations"], list) or len(payload["citations"]) > 32
                or not isinstance(payload["execution_path"], list)
                or not 1 <= len(payload["execution_path"]) <= 12):
            raise _invalid()
        sources = set()
        normalized_sources = []
        for source in payload["sources"]:
            if not isinstance(source, dict) or set(source) != {
                "source_id", "provider_id", "version", "title", "classification",
            }:
                raise _invalid()
            for value in source.values():
                _text(value)
            if source["provider_id"] != provider:
                raise _invalid()
            key = (source["source_id"], source["version"])
            if key in sources:
                raise _invalid()
            sources.add(key)
            normalized_sources.append(dict(source))
        normalized_citations = []
        for citation in payload["citations"]:
            if not isinstance(citation, dict) or set(citation) != {"source_id", "version", "section"}:
                raise _invalid()
            for value in citation.values():
                _text(value)
            if (citation["source_id"], citation["version"]) not in sources:
                raise _invalid()
            normalized_citations.append(dict(citation))
        for step in payload["execution_path"]:
            _text(step)
        if status == "no-matching-capability" and (sources or payload["citations"]):
            raise _invalid()
        normalized = {
            **payload, "sources": normalized_sources, "citations": normalized_citations,
            "execution_path": list(payload["execution_path"]),
        }
        if len(json.dumps(normalized, ensure_ascii=True, allow_nan=False).encode("utf-8")) > 65536:
            raise _invalid()
        return normalized
    except (_Failure, TypeError, ValueError, KeyError, UnicodeError, AttributeError, RecursionError):
        raise ValueError("Invalid CONTOSO runtime result.") from None


class Gateway:
    _ROUTES = {
        "/api/invoke": "invoke",
        "/api/connections/prepare": "prepare",
        "/api/connections/status": "status",
        "/api/connections/token": "token",
        "/api/oauth/callback": "callback",
    }

    def __init__(
        self, settings: GatewaySettings, *, broker: DelegatedConnectionBroker,
        network_client: NetworkDependency, authenticator: EntraAuthenticator | None = None,
        logger: logging.Logger | None = None, clock: Callable[[], float] = time.time,
        enable_invoke: bool = True,
        a2a_bridge=None,
    ):
        self.settings = settings
        self.broker = broker
        self.network_client = network_client
        self._invoke = getattr(network_client, "invoke", network_client)
        if not callable(self._invoke):
            raise ValueError("Gateway requires an async network invoke dependency")
        self.authenticator = (authenticator if authenticator is not None
                              else EntraAuthenticator(settings.identity_settings))
        if self.authenticator.settings != settings.identity_settings:
            raise ValueError("Gateway authenticator must use the configured tenant and API audience")
        self.logger = logger if logger is not None else logging.getLogger("contoso.gateway")
        self.clock = clock
        if type(enable_invoke) is not bool:
            raise ValueError("Gateway invocation enablement must be explicit")
        self._routes = dict(self._ROUTES)
        if not enable_invoke:
            self._routes.pop("/api/invoke")
        self._a2a_bridge = a2a_bridge
        if a2a_bridge is not None:
            self._routes["/api/a2a"] = "a2a"

    async def _authenticate(self, operation, headers):
        header = headers.get("authorization")
        if not isinstance(header, str) or not header.strip():
            raise _Failure(401, "unauthenticated", "A valid bearer token is required.")
        settings = self.settings
        if operation in ("invoke", "a2a"):
            pairs = ((settings.requester_client_id, settings.requester_oid),)
            permission, kind = "CONTOSO.Invoke", "workload"
        elif operation in ("prepare", "status"):
            pairs = ((settings.authoring_client_id, settings.authoring_admin_oid),)
            permission, kind = "CONTOSO.Deploy", "user"
        else:
            pairs = settings.token_identities
            permission, kind = CONNECTION_USE, "workload"
        identity = await asyncio.wait_for(asyncio.to_thread(
            self.authenticator.verify, header,
            allowed_clients=frozenset(client for client, _ in pairs),
            allowed_callers=frozenset(caller for _, caller in pairs),
            delegated_scope=permission, application_role=permission,
        ), timeout=settings.authentication_timeout)
        if (not isinstance(identity, CallerIdentity) or identity.kind != kind
                or (identity.client_id, identity.caller_id) not in pairs):
            raise _Failure(403, "forbidden", "This caller is not authorized.")
        return identity

    @staticmethod
    def _headers(headers):
        if not isinstance(headers, Mapping) or len(headers) > 64:
            raise _invalid("headers_structure")
        normalized = {}
        for key, value in headers.items():
            if (not isinstance(key, str) or not isinstance(value, str)
                    or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", key)
                    or len(value) > 16384 or "\r" in value or "\n" in value):
                raise _invalid("header_value")
            key = key.lower()
            if key in normalized:
                raise _invalid("duplicate_header")
            normalized[key] = value
        return normalized

    def _body(self, operation, headers, body):
        maximum = 32768 if operation == "callback" else 16384 if operation in ("invoke", "a2a") else 1024
        if not isinstance(body, bytes) or not 0 < len(body) <= maximum:
            raise _invalid("body_size")
        if headers.get("content-encoding", "identity").lower() != "identity":
            raise _invalid("content_encoding")
        if "content-length" in headers:
            length = headers["content-length"]
            if not re.fullmatch(r"[0-9]{1,6}", length) or int(length) != len(body):
                raise _invalid("content_length")
        expected = ("application/x-www-form-urlencoded" if operation == "callback"
                    else "application/json")
        parts = [part.strip().lower() for part in headers.get("content-type", "").split(";")]
        if parts[0] != expected or (len(parts) > 1 and parts[1:] != ["charset=utf-8"]):
            raise _invalid("content_type")
        try:
            if operation == "callback":
                text = body.decode("ascii", errors="strict")
                if re.search(r"%(?![0-9a-fA-F]{2})", text):
                    raise _invalid("form_percent_encoding")
                payload = _unique(parse_qsl(
                    text, keep_blank_values=True, strict_parsing=True,
                    encoding="utf-8", errors="strict", max_num_fields=len(_CALLBACK_FIELDS),
                ))
                if (not set(payload).issubset(_CALLBACK_FIELDS) or "state" not in payload
                        or ("code" in payload) == ("error" in payload)
                        or (("error_description" in payload or "error_uri" in payload)
                            and "error" not in payload)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", payload["state"])):
                    raise _invalid("form_fields")
                for key, value in payload.items():
                    try:
                        limit = 2048 if key == "client_info" else 16384
                        _text(value, chars=limit, size=limit, empty=key in ("session_state", "client_info"))
                    except _Failure:
                        raise _invalid("form_value") from None
                if ("iss" in payload and payload["iss"] !=
                        f"https://login.microsoftonline.com/{self.settings.tenant_id}/v2.0"):
                    raise _invalid("form_issuer")
                # Optional provider metadata never becomes guest proof or MSAL routing input.
                return {key: payload[key] for key in ("state", "code", "error") if key in payload}
            payload = json.loads(
                body.decode("utf-8", errors="strict"), object_pairs_hook=_unique,
                parse_constant=lambda _: (_ for _ in ()).throw(_invalid()),
            )
        except (ValueError, UnicodeError, RecursionError):
            raise _invalid("body_decode") from None
        if operation == "a2a":
            from .a2a import validate_a2a_request
            if headers.get("a2a-version") != "1.0":
                raise _invalid()
            try:
                return validate_a2a_request(payload)
            except ValueError:
                raise _invalid() from None
        keys = {"question", "studio_variant", "integration"} if operation == "invoke" else {"provider_id"}
        if not isinstance(payload, dict) or set(payload) != keys:
            raise _invalid()
        if operation == "invoke":
            _text(payload["question"], chars=2000, size=8000)
            if (not isinstance(payload["studio_variant"], str)
                    or payload["studio_variant"] not in ("standard", "copilot-harness")
                    or not isinstance(payload["integration"], str)
                    or payload["integration"] not in ("sdk", "a2a")):
                raise _invalid()
        else:
            provider = _text(payload["provider_id"], chars=128, size=128)
            if any(c.isspace() for c in provider):
                raise _invalid()
        return payload

    @staticmethod
    def _runtime_result(result, variant):
        try:
            result = result.to_dict() if isinstance(result, RuntimeResult) else result
            normalized = validate_runtime_result(result)
            if normalized["status"] == "unavailable":
                raise _unavailable()
            returned_variant = normalized["studio_variant"]
            if returned_variant is not None and returned_variant != variant:
                raise _unavailable()
            return normalized
        except (TypeError, ValueError, KeyError, UnicodeError, AttributeError):
            raise _unavailable() from None

    def _scoped_token(self, token):
        if (not isinstance(token, ScopedToken) or token.tenant_id != self.settings.tenant_id
                or token.scopes not in (frozenset({STANDARD_SCOPE}), frozenset({HARNESS_SCOPE}))
                or not math.isfinite(self.clock()) or token.expires_at <= self.clock()):
            raise _unavailable()
        try:
            _text(token.audience, chars=512, size=2048)
        except _Failure:
            raise _unavailable() from None
        return {
            "token": token.token, "tenant_id": token.tenant_id, "audience": token.audience,
            "scopes": sorted(token.scopes), "expires_at": token.expires_at,
        }

    async def handle(
        self, method: str, path: str, *, headers: Mapping[str, str], body: bytes,
    ) -> GatewayResponse:
        correlation = uuid4().hex
        operation = self._routes.get(path, "unknown") if isinstance(path, str) else "unknown"
        extra_headers = {}
        validation_stage = "method"
        broker_called = False
        try:
            if operation == "unknown":
                raise _Failure(404, "not_found", "Route not found.")
            if method != "POST":
                extra_headers["Allow"] = "POST"
                raise _Failure(405, "method_not_allowed", "POST is required.")
            validation_stage = "headers"
            normalized_headers = self._headers(headers)
            identity = (None if operation == "callback"
                        else await self._authenticate(operation, normalized_headers))
            validation_stage = "body"
            payload = self._body(operation, normalized_headers, body)
            if operation == "a2a":
                output = await asyncio.wait_for(
                    self._a2a_bridge.forward(payload), timeout=self.settings.invocation_timeout,
                )
            elif operation == "invoke":
                result = await asyncio.wait_for(
                    self._invoke(**payload), timeout=self.settings.invocation_timeout,
                )
                output = self._runtime_result(result, payload["studio_variant"])
            elif operation == "prepare":
                start = await asyncio.wait_for(asyncio.to_thread(
                    self.broker.prepare_flow, payload["provider_id"],
                    preparer_id=identity.caller_id, preparer_roles=frozenset({DEPLOYMENT_ADMIN}),
                ), timeout=self.settings.connection_timeout)
                if (not isinstance(start, AuthorizationStart)
                        or not isinstance(start.state, str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", start.state)
                        or not isinstance(start.auth_uri, str) or len(start.auth_uri) > 16384):
                    raise _unavailable()
                parsed = urlsplit(start.auth_uri)
                if (parsed.scheme != "https" or parsed.hostname != "login.microsoftonline.com"
                        or parsed.username is not None or parsed.fragment
                        or any(c.isspace() or ord(c) < 32 for c in start.auth_uri)):
                    raise _unavailable()
                output = {"auth_uri": start.auth_uri, "state": start.state}
            elif operation == "status":
                stored = await asyncio.wait_for(asyncio.to_thread(
                    self.broker.connection_status, payload["provider_id"],
                    preparer_id=identity.caller_id, preparer_roles=frozenset({DEPLOYMENT_ADMIN}),
                ), timeout=self.settings.connection_timeout)
                if stored != "stored":
                    raise _unavailable()
                output = {"provider_id": payload["provider_id"], "status": "stored",
                          "live_token_validated": False}
            elif operation == "token":
                token = await asyncio.wait_for(asyncio.to_thread(
                    self.broker.get_token, payload["provider_id"],
                    caller_id=identity.caller_id, caller_roles=frozenset({CONNECTION_USE}),
                ), timeout=self.settings.connection_timeout)
                output = self._scoped_token(token)
            else:
                validation_stage = "broker"
                broker_called = True
                result = await asyncio.wait_for(asyncio.to_thread(
                    self.broker.complete_callback, auth_response=payload,
                ), timeout=self.settings.connection_timeout)
                if result is not None:
                    raise _unavailable()
                output = {"status": "connected", "correlation_id": correlation}
                validation_stage = "complete"
            status = 200
        except asyncio.TimeoutError:
            status, output = 504, self._error(
                "timeout", "The request exceeded its time budget.", correlation,
            )
        except ChatFailure as failure:
            status = failure.status if failure.status in (401, 403) else 503
            code = "unauthenticated" if status == 401 else "forbidden" if status == 403 else "dependency_unavailable"
            output = self._error(code, "Authentication failed." if status != 503
                                 else "The service is temporarily unavailable.", correlation)
        except ConnectionDenied:
            status, output = 403, self._error("forbidden", "This caller is not authorized.", correlation)
        except ConnectionRequired:
            status, output = 409, self._error(
                "connection_required", "The approved guest connection requires authentication.", correlation,
            )
        except (ConnectionUnavailable, ConnectionError):
            status, output = 503, self._error(
                "dependency_unavailable", "The service is temporarily unavailable.", correlation,
            )
        except _Failure as failure:
            validation_stage = failure.validation_stage or validation_stage
            status, output = failure.status, self._error(failure.code, failure.message, correlation)
        except Exception:
            status, output = 503, self._error(
                "dependency_unavailable", "The service is temporarily unavailable.", correlation,
            )
        if operation == "callback":
            from .callback_diagnostics import record_callback
            try:
                metadata = record_callback(correlation, method, headers, body, validation_stage, broker_called)
                self.logger.info("CONTOSO callback metadata=%s", json.dumps(metadata, separators=(",", ":")))
            except Exception:
                self.logger.warning("CONTOSO callback metadata unavailable")
        self.logger.info("CONTOSO gateway operation=%s status=%d correlation=%s", operation, status, correlation)
        return GatewayResponse(
            status, json.dumps(output, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8"),
            {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
             "Pragma": "no-cache", "X-Content-Type-Options": "nosniff",
             "X-Correlation-ID": correlation, **extra_headers},
        )

    @staticmethod
    def _error(code, message, correlation):
        return {"error": {"code": code, "message": message}, "correlation_id": correlation}
