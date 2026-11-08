"""Fixed A2A identity diagnostic using explicit Hosted transport and Gateway MI."""

import asyncio
import json
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from azure.identity import ManagedIdentityCredential
import httpx

from contoso.gateway.a2a import _unique, validate_a2a_request
from contoso.gateway.hosted_transport import select_hosted_a2a_endpoint
from contoso.identity_probe import parse_probe_reply
from contoso.providers.a2a import _parse


_SERVICE = "contoso-foundry-agent"
_CORRELATIONS = (
    "apim-request-id", "x-ms-request-id", "request-id",
    "x-ms-correlation-request-id", "traceparent",
)


def _validate_endpoint(project_endpoint, endpoint):
    if not isinstance(project_endpoint, str) or not isinstance(endpoint, str):
        raise ValueError("Invalid fixed A2A diagnostic configuration")
    project = urlsplit(project_endpoint)
    if (project.scheme != "https" or not project.hostname
            or not project.hostname.endswith(".services.ai.azure.com")
            or project.username is not None or project.password is not None
            or project.port is not None or project.query or project.fragment
            or not re.fullmatch(r"/api/projects/[A-Za-z0-9_-]+", project.path)):
        raise ValueError("Invalid fixed A2A diagnostic configuration")
    if endpoint != project_endpoint + f"/agents/{_SERVICE}/endpoint/protocols/a2a?api-version=v1":
        raise ValueError("Invalid fixed A2A diagnostic configuration")


def _safe_correlations(headers):
    return {
        name: headers[name] for name in _CORRELATIONS
        if name in headers and re.fullmatch(r"[A-Fa-f0-9-]{16,128}", headers[name])
    }


def _safe_rpc_detail(error):
    message = error.get("message")
    if isinstance(message, str):
        received = re.search(r"\bReceived ([1-5][0-9]{2})\b", message[:2048])
        if received:
            return f"Received HTTP {received[1]} from upstream."
        if message in {
            "Parse error", "Invalid Request", "Method not found", "Invalid params", "Internal error",
        }:
            return message
    return "Upstream JSON-RPC error; remaining detail redacted."


async def probe_native_a2a(
    config, *, endpoint, credential_factory=ManagedIdentityCredential,
    client_factory=httpx.AsyncClient, hosting_transport="native",
):
    """No configurable input, caller identity, destination service or fallback."""
    request_id = str(uuid4())
    receipt = {
        "status": "failed", "service": _SERVICE, "integration": "a2a",
        "protocol": "a2a", "protocol_version": "1.0", "api_version": "v1",
        "method": "SendMessage", "request_id": request_id,
        "http_status": None, "correlations": {},
        "hosting_transport": hosting_transport if hosting_transport in ("native", "http-passthrough") else "invalid",
        "hosting_route": "a2a" if hosting_transport == "native" else "invocations",
    }

    def failed(code, detail, **extra):
        return {**receipt, "error": {"code": code, "detail": detail, **extra}}

    try:
        _validate_endpoint(config.project_endpoint, endpoint)
        endpoint = select_hosted_a2a_endpoint(
            endpoint, hosting_transport=hosting_transport,
        )
    except (ValueError, AttributeError):
        return failed("invalid_configuration", "Only the configured native Router A2A endpoint is allowed.")
    payload = {
        "jsonrpc": "2.0", "id": request_id, "method": "SendMessage",
        "params": {
            "message": {"messageId": str(uuid4()), "role": "ROLE_USER",
                        "parts": [{"text": "CONTOSO identity probe"}]},
            "configuration": {"acceptedOutputModes": ["text/plain"], "returnImmediately": False},
        },
    }
    validate_a2a_request(payload)
    stage = "credential"
    try:
        async with asyncio.timeout(60):
            with credential_factory(connection_timeout=5, read_timeout=10, retry_total=0) as credential:
                token = await asyncio.to_thread(credential.get_token, "https://ai.azure.com/.default")
                if (not isinstance(token.token, str) or not 0 < len(token.token) <= 16384
                        or any(c.isspace() or ord(c) < 32 for c in token.token)
                        or token.expires_on <= time.time() + 10):
                    return failed("credential_unavailable", "Gateway managed identity returned no usable token.")
                stage = "transport"
                async with client_factory(
                    timeout=httpx.Timeout(connect=5, read=35, write=5, pool=5),
                    follow_redirects=False, trust_env=False,
                ) as client:
                    async with client.stream(
                        "POST", endpoint, json=payload,
                        headers={"Authorization": "Bearer " + token.token, "A2A-Version": "1.0"},
                    ) as response:
                        receipt["http_status"] = response.status_code
                        receipt["correlations"] = _safe_correlations(response.headers)
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > 65536:
                                return failed("response_too_large", "Native A2A response exceeded the diagnostic limit.")
                            body.extend(chunk)
            stage = "response"
            try:
                result = json.loads(body.decode("utf-8", errors="strict"), object_pairs_hook=_unique)
            except (ValueError, UnicodeError, RecursionError):
                if receipt["http_status"] != 200:
                    return failed("upstream_http_error", f"Native A2A endpoint returned HTTP {receipt['http_status']}.")
                return failed("invalid_response", "Native A2A response was not bounded valid JSON.")
            if (isinstance(result, dict) and result.get("jsonrpc") == "2.0"
                    and isinstance(result.get("error"), dict)
                    and type(result["error"].get("code")) is int):
                return failed(
                    "upstream_jsonrpc_error", _safe_rpc_detail(result["error"]),
                    jsonrpc_code=result["error"]["code"],
                    response_id_matches=result.get("id") == request_id,
                )
            if receipt["http_status"] != 200:
                return failed("upstream_http_error", f"Native A2A endpoint returned HTTP {receipt['http_status']}.")
            if (not isinstance(result, dict) or set(result) != {"jsonrpc", "id", "result"}
                    or result.get("jsonrpc") != "2.0" or result.get("id") != request_id):
                return failed("invalid_response", "Native A2A response envelope did not match the request.")
            text, _ = _parse(result, request_id)
            if len(text) > 4096:
                raise ValueError("Invalid identity-bootstrap size")
            json.loads(text, object_pairs_hook=_unique)
            bootstrap = parse_probe_reply(text)
            return {**receipt, **bootstrap}
    except (TimeoutError, httpx.TimeoutException):
        return failed("timeout", f"Native A2A diagnostic exceeded its {stage} time budget.")
    except Exception:
        return failed(
            "credential_unavailable" if stage == "credential" else
            "transport_unavailable" if stage == "transport" else "invalid_response",
            "Gateway managed identity could not acquire the Foundry token." if stage == "credential" else
            "Native A2A transport failed." if stage == "transport" else
            "Native A2A response did not contain a valid identity-bootstrap result.",
        )
