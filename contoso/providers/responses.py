"""Optional stateless Hosted Responses SDK diagnostic, explicitly not native A2A.

The diagnostic/comparison SDK variant invokes actual Hosted code using Responses.
Incoming native A2A service failures remain a separate unresolved integration;
business composition still requires native A2A. This module never silently falls
back, converts agents, or serves canned text.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import json
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from contoso.router.core import Capability, CatalogEntry, Protocol
from .common import (
    ConfigurationError, ProtocolError, ProviderTimeout, RawReply, ResponseTooLarge,
    TransportError, reply, validate_call, validate_endpoint, validate_limits,
    validate_origins, validate_text_token, platform_headers,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI


FOUNDRY_AUDIENCE = "https://ai.azure.com"
FOUNDRY_SCOPE = FOUNDRY_AUDIENCE + "/.default"
ClientFactory = Callable[[str, str, Mapping[str, str], float, float], "AsyncOpenAI"]


def _client(
    base_url: str, token: str, headers: Mapping[str, str],
    connect_timeout: float, read_timeout: float,
) -> AsyncOpenAI:
    try:
        from openai import AsyncOpenAI, DefaultAsyncHttpxClient, Timeout
    except ImportError:
        raise ConfigurationError("The Foundry Responses SDK dependency is unavailable") from None
    timeout = Timeout(read_timeout, connect=connect_timeout)
    return AsyncOpenAI(
        base_url=base_url, api_key=token, default_query={"api-version": "v1"},
        default_headers=headers, timeout=timeout, max_retries=0,
        http_client=DefaultAsyncHttpxClient(timeout=timeout, follow_redirects=False),
    )


def _parse(payload: object) -> str:
    if (not isinstance(payload, dict) or payload.get("object") != "response"
            or not isinstance(payload.get("id"), str) or not payload["id"]):
        raise ProtocolError("Hosted Responses returned an invalid envelope")
    if (payload.get("status") != "completed" or payload.get("error") is not None
            or payload.get("incomplete_details") is not None):
        raise ProtocolError("Hosted Responses did not complete successfully")
    output = payload.get("output")
    if not isinstance(output, list):
        raise ProtocolError("Hosted Responses output is malformed")
    texts = []
    for item in output:
        if not isinstance(item, dict):
            raise ProtocolError("Hosted Responses output item is malformed")
        if item.get("type") != "message":
            continue
        if (item.get("role") != "assistant"
                or item.get("status", "completed") != "completed"
                or not isinstance(item.get("content"), list)):
            raise ProtocolError("Hosted Responses message is malformed")
        for part in item["content"]:
            if (not isinstance(part, dict) or part.get("type") != "output_text"
                    or not isinstance(part.get("text"), str)):
                raise ProtocolError("Hosted Responses must return plain text")
            texts.append(part["text"])
    text = "".join(texts)
    if not text.strip():
        raise ProtocolError("Hosted Responses contains no answer text")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise ProtocolError("Hosted Responses text encoding is invalid") from None
    return text


class FoundryResponsesTransport:
    """Fixed approved native endpoint; the caller supplies its fresh service token.

    Every call uses input=text, store=False, with no model, conversation,
    previous_response_id, background execution, persistence, or protocol fallback.
    A factory may supply only current SDK-verified x-agent-foundry-call-id.
    Input and streaming HTTP response bytes are bounded before SDK JSON parsing.
    """

    def __init__(
        self, *, endpoint: str, approved_origins: frozenset[str],
        connect_timeout: float = 5, read_timeout: float = 30,
        overall_timeout: float = 60, max_response_bytes: int = 1_048_576,
        max_input_bytes: int = 65536,
        platform_headers_factory: Callable[[], Mapping[str, str]] | None = None,
        client_factory: ClientFactory | None = None,
    ):
        validate_limits(connect_timeout, read_timeout, overall_timeout,
                        max_response_bytes=max_response_bytes)
        validate_limits(max_response_bytes=max_input_bytes)
        self.approved_origins = validate_origins(approved_origins)
        validate_endpoint(endpoint, self.approved_origins)
        parsed = urlsplit(endpoint)
        if (not parsed.hostname or not parsed.hostname.endswith(".services.ai.azure.com")
                or parsed.port not in (None, 443) or parsed.query != "api-version=v1"
                or not re.fullmatch(
                    r"/api/projects/[A-Za-z0-9][A-Za-z0-9._-]*/agents/"
                    r"[A-Za-z0-9][A-Za-z0-9._-]*/endpoint/protocols/openai/responses",
                    parsed.path,
                )):
            raise ConfigurationError("A fixed native Hosted Responses endpoint is required")
        if client_factory is not None and not callable(client_factory):
            raise ConfigurationError("The Foundry Responses client factory is invalid")
        if platform_headers_factory is not None and not callable(platform_headers_factory):
            raise ConfigurationError("Platform headers factory is invalid")
        self.platform_headers_factory = platform_headers_factory
        self.endpoint = endpoint
        self.base_url = urlunsplit((
            parsed.scheme, parsed.netloc, parsed.path.removesuffix("responses"), "", "",
        ))
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.overall_timeout = overall_timeout
        self.max_response_bytes = max_response_bytes
        self.max_input_bytes = max_input_bytes
        self.client_factory = client_factory or _client

    async def invoke(self, text: str, token: str) -> str:
        validate_endpoint(self.endpoint, self.approved_origins)
        validate_text_token(text, token)
        try:
            size = len(text.encode("utf-8"))
        except UnicodeError:
            raise ConfigurationError("Hosted Responses input must be valid Unicode") from None
        if size > self.max_input_bytes:
            raise ConfigurationError("Hosted Responses input exceeds the byte limit")
        try:
            from openai import APITimeoutError, OpenAIError
        except ImportError:
            raise ConfigurationError("The Foundry Responses SDK dependency is unavailable") from None
        try:
            async with asyncio.timeout(self.overall_timeout):
                headers = platform_headers(self.platform_headers_factory)
                async with self.client_factory(
                    self.base_url, token, headers,
                    min(self.connect_timeout, self.overall_timeout),
                    min(self.read_timeout, self.overall_timeout),
                ) as client:
                    async with client.responses.with_streaming_response.create(
                        input=text, store=False,
                    ) as response:
                        if response.status_code != 200:
                            raise TransportError("Hosted Responses requires HTTP 200 without redirects")
                        data = bytearray()
                        async for chunk in response.iter_bytes():
                            if len(data) + len(chunk) > self.max_response_bytes:
                                raise ResponseTooLarge("Hosted Responses exceeds the byte limit")
                            data.extend(chunk)
                try:
                    payload = json.loads(data, object_pairs_hook=_unique_fields)
                except (ValueError, UnicodeError, RecursionError):
                    raise ProtocolError("Hosted Responses JSON is malformed") from None
                return _parse(payload)
        except (TimeoutError, APITimeoutError):
            raise ProviderTimeout("Hosted Responses request timed out") from None
        except OpenAIError:
            raise TransportError("Hosted Responses request failed") from None


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Hosted Responses contains duplicate JSON fields")
        result[key] = value
    return result


class FoundryResponsesAdapter(FoundryResponsesTransport):
    """Explicit optional comparison only, not selected by business composition."""

    async def answer(self, entry: CatalogEntry, question: str, token: str) -> RawReply:
        validate_call(
            entry, question, token, self.approved_origins,
            Protocol.FOUNDRY_RESPONSES, None, FOUNDRY_SCOPE,
            capability=Capability.KNOWLEDGE_KNOWLEDGE,
        )
        if entry.endpoint != self.endpoint or entry.auth.audience != FOUNDRY_AUDIENCE:
            raise ConfigurationError("Hosted Responses configuration does not match the authorized route")
        text = await self.invoke(question, token)
        return reply(entry, text, channel="foundry-responses-sdk", state="completed")
