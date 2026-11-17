"""Catalog-configured A2A 1.0 transport (no authentication acquisition)."""

import asyncio
from collections.abc import Callable, Mapping
import json
import re
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

import requests

from contoso.router.core import Capability, CatalogEntry, STUDIOVariant, Protocol
from .common import (
    ConfigurationError, ProtocolError, ProviderTimeout, RawReply,
    ResponseTooLarge, TransportError, reply, validate_call, validate_limits,
    validate_origins,
    validate_endpoint, validate_text_token,
    platform_headers,
)

A2A_SCOPE = "https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A"


def _parts(value: object) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get("parts"), list):
        raise ProtocolError("A2A response parts are malformed")
    texts = []
    for part in value["parts"]:
        if not isinstance(part, dict):
            raise ProtocolError("A2A response part is malformed")
        if "text" in part:
            if not isinstance(part["text"], str):
                raise ProtocolError("A2A response text is malformed")
            if part["text"].strip():
                texts.append(part["text"])
    return texts


def _parse(payload: object, request_id: str) -> tuple[str, str | None]:
    if (not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0"
            or payload.get("id") != request_id):
        raise ProtocolError("A2A response envelope or request ID is invalid")
    if "error" in payload:
        raise ProtocolError("A2A returned a JSON-RPC error")
    result = payload.get("result")
    if not isinstance(result, dict) or ("task" in result) == ("message" in result):
        raise ProtocolError("A2A response result is malformed")
    if "task" in result:
        task = result["task"]
        if not isinstance(task, dict) or not isinstance(task.get("status"), dict):
            raise ProtocolError("A2A task status is malformed")
        state = task["status"].get("state")
        if state != "TASK_STATE_COMPLETED":
            raise ProtocolError("A2A task did not complete successfully")
        artifacts = task.get("artifacts")
        if not isinstance(artifacts, list):
            raise ProtocolError("A2A task artifacts are malformed")
        texts = [text for artifact in artifacts for text in _parts(artifact)]
    else:
        state = None
        texts = _parts(result["message"])
    if not texts:
        raise ProtocolError("A2A response contains no text")
    return "\n".join(texts), state


class _A2AHTTP:
    """Shared bounded HTTP exchange, parsing, and per-call platform context.

    The async deadline bounds caller latency. The blocking worker additionally
    has bounded socket waits and checks its deadline between streamed chunks.
    """

    def __init__(
        self, *, approved_origins: frozenset[str], connect_timeout: float = 5,
        read_timeout: float = 30, overall_timeout: float = 60,
        max_response_bytes: int = 1_048_576,
        session_factory: Callable[[], requests.Session] = requests.Session,
        platform_headers_factory: Callable[[], Mapping[str, str]] | None = None,
        worker_slot: threading.Semaphore | None = None,
    ):
        validate_limits(connect_timeout, read_timeout, overall_timeout,
                        max_response_bytes=max_response_bytes)
        if not callable(session_factory):
            raise ConfigurationError("A2A session factory is invalid")
        if platform_headers_factory is not None and not callable(platform_headers_factory):
            raise ConfigurationError("Platform headers factory is invalid")
        if worker_slot is not None and not isinstance(worker_slot, threading.Semaphore):
            raise ConfigurationError("A2A worker admission slot is invalid")
        self.approved_origins = validate_origins(approved_origins)
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.overall_timeout = overall_timeout
        self.max_response_bytes = max_response_bytes
        self.session_factory = session_factory
        self.platform_headers_factory = platform_headers_factory
        self.worker_slot = worker_slot

    async def _request(self, endpoint: str, text: str, token: str) -> tuple[str, str | None]:
        validate_endpoint(endpoint, self.approved_origins)
        validate_text_token(text, token)
        try:
            async with asyncio.timeout(self.overall_timeout):
                platform_headers = self._platform_headers()
                if self.worker_slot is not None:
                    from contoso.bounded import run_bounded_sync

                    return await run_bounded_sync(
                        self.worker_slot,
                        lambda: self._send(
                            endpoint, text, token, platform_headers=platform_headers,
                        ),
                    )
                return await asyncio.to_thread(
                    self._send, endpoint, text, token,
                    platform_headers=platform_headers,
                )
        except TimeoutError:
            raise ProviderTimeout("A2A request timed out") from None

    def _platform_headers(self) -> dict[str, str]:
        return platform_headers(self.platform_headers_factory)

    def _send(
        self, endpoint: str, question: str, token: str, *,
        platform_headers: Mapping[str, str] | None = None,
    ) -> tuple[str, str | None]:
        request_id = str(uuid4())
        payload = {
            "jsonrpc": "2.0", "id": request_id, "method": "SendMessage",
            "params": {
                "message": {"messageId": str(uuid4()), "role": "ROLE_USER",
                            "parts": [{"text": question}]},
                "configuration": {"acceptedOutputModes": ["text/plain"],
                                  "returnImmediately": False},
            },
        }
        deadline = time.monotonic() + self.overall_timeout
        try:
            with self.session_factory() as session:
                session.trust_env = False
                with session.post(
                    endpoint, json=payload,
                    headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0",
                             **(platform_headers or {})},
                    timeout=(min(self.connect_timeout, self.overall_timeout),
                             min(self.read_timeout, self.overall_timeout)),
                    allow_redirects=False, stream=True,
                ) as response:
                    if response.status_code != 200 or response.history:
                        raise TransportError("A2A requires HTTP 200 without redirects")
                    data = bytearray()
                    for chunk in response.iter_content(chunk_size=8192):
                        if time.monotonic() >= deadline:
                            raise ProviderTimeout("A2A request timed out")
                        if len(data) + len(chunk) > self.max_response_bytes:
                            raise ResponseTooLarge("A2A response exceeds the byte limit")
                        data.extend(chunk)
            try:
                decoded = json.loads(data)
            except (ValueError, UnicodeError, RecursionError):
                raise ProtocolError("A2A response JSON is malformed") from None
            return _parse(decoded, request_id)
        except requests.Timeout:
            raise ProviderTimeout("A2A request timed out") from None
        except requests.RequestException:
            raise TransportError("A2A request failed") from None


class A2ATransport(_A2AHTTP):
    """Invoke an administrator-configured endpoint without knowledge metadata.

    The caller supplies a fresh in-memory bearer for the configured endpoint.
    An optional factory reads current verified platform context, not prompt data.
    Returned text is opaque: no RuntimeResult parsing or citation extraction.
    """

    def __init__(
        self, *, endpoint: str, approved_origins: frozenset[str],
        connect_timeout: float = 5, read_timeout: float = 30,
        overall_timeout: float = 60, max_response_bytes: int = 1_048_576,
        session_factory: Callable[[], requests.Session] = requests.Session,
        platform_headers_factory: Callable[[], Mapping[str, str]] | None = None,
        worker_slot: threading.Semaphore | None = None,
    ):
        super().__init__(
            approved_origins=approved_origins, connect_timeout=connect_timeout,
            read_timeout=read_timeout, overall_timeout=overall_timeout,
            max_response_bytes=max_response_bytes, session_factory=session_factory,
            platform_headers_factory=platform_headers_factory,
            worker_slot=worker_slot,
        )
        validate_endpoint(endpoint, self.approved_origins)
        self.endpoint = endpoint

    async def invoke(self, text: str, token: str) -> str:
        response_text, _state = await self._request(self.endpoint, text, token)
        return response_text


class A2AAdapter(_A2AHTTP):
    """Catalog-bound knowledge-provider API over the shared A2A exchange.

    Platform headers are allowed only on a configured Foundry knowledge instance.
    Supplier-delivery providers retain native Copilot Harness endpoint and scope checks.
    """

    async def answer(self, entry: CatalogEntry, question: str, token: str) -> RawReply:
        knowledge = isinstance(entry, CatalogEntry) and entry.capability == Capability.KNOWLEDGE_KNOWLEDGE
        validate_call(
            entry, question, token, self.approved_origins, Protocol.A2A,
            None if knowledge else STUDIOVariant.COPILOT_HARNESS,
            None if knowledge else A2A_SCOPE,
            capability=Capability.KNOWLEDGE_KNOWLEDGE if knowledge else Capability.SUPPLIER_DELIVERY_TERMS,
        )
        if not knowledge:
            if self.platform_headers_factory is not None:
                raise ConfigurationError("Platform headers are not supported for supplier-delivery providers")
            endpoint = urlsplit(entry.endpoint)
            if (endpoint.hostname != "api.powerplatform.com"
                    or endpoint.port not in (None, 443)
                    or not re.fullmatch(
                        r"/copilotstudio-a2a/(?:[A-Za-z0-9_.-]+/)+agents/[A-Za-z0-9_.-]+",
                        endpoint.path)):
                raise ConfigurationError("Native A2A endpoint configuration is invalid")
        text, state = await self._request(entry.endpoint, question, token)
        channel = "foundry-a2a" if knowledge and self.platform_headers_factory is not None else (
            "a2a" if knowledge else "native-a2a"
        )
        return reply(entry, text, channel=channel, state=state)
