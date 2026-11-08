"""Stateless Hosted Responses SDK contracts. No live or identity proof."""

import asyncio
from dataclasses import replace
import importlib
import json
from types import SimpleNamespace

import pytest

from contoso.router.core import (
    AuthMetadata, AuthScheme, Capability, CatalogEntry, CatalogError, STUDIOVariant,
    Protocol, SourceMetadata,
)
from contoso.providers import (
    ConfigurationError, FoundryResponsesAdapter, FoundryResponsesTransport,
    ProtocolError, ProviderTimeout, ResponseTooLarge, TransportError, extract_citations,
)
from contoso.providers.responses import FOUNDRY_AUDIENCE, FOUNDRY_SCOPE
from contoso.providers.tests.test_transports import block_network


ORIGIN = "https://demo.services.ai.azure.com"
ENDPOINT = ORIGIN + "/api/projects/test-project/agents/test-knowledge/endpoint/protocols/openai/responses?api-version=v1"
TOKEN = "in-memory-test-service-bearer"
ANSWER = 'CONTOSO-DEMO-KNOWLEDGE-001, Version 1; Section: "Approved intervention".'


def payload(text=ANSWER):
    return {
        "id": "resp_test", "object": "response", "status": "completed", "error": None,
        "output": [{"type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text}]}],
    }


class Stream:
    def __init__(self, value=None, *, status=200, raw=None, delay=0, error=None):
        self.data = raw if raw is not None else json.dumps(payload() if value is None else value).encode()
        self.status_code = status
        self.delay = delay
        self.error = error
        self.closed = False
        self.started = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_bytes(self):
        if self.started is not None:
            self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        for offset in range(0, len(self.data), 32):
            yield self.data[offset:offset + 32]


class Client:
    def __init__(self, stream):
        self.stream = stream
        self.responses = SimpleNamespace(with_streaming_response=self)
        self.calls = []
        self.factory_calls = []
        self.closed = False

    def factory(self, base_url, token, headers, connect_timeout, read_timeout):
        self.factory_calls.append((base_url, token, dict(headers), connect_timeout, read_timeout))
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.stream


def transport(client=None, **kwargs):
    client = client or Client(Stream())
    return FoundryResponsesTransport(
        endpoint=ENDPOINT, approved_origins=frozenset({ORIGIN}),
        client_factory=client.factory, **kwargs,
    )


def entry():
    return CatalogEntry(
        "test-knowledge", Capability.KNOWLEDGE_KNOWLEDGE, None, ENDPOINT, Protocol.FOUNDRY_RESPONSES,
        AuthMetadata(AuthScheme.ENTRA_BEARER, "test-tenant", FOUNDRY_AUDIENCE,
                     frozenset({FOUNDRY_SCOPE})),
        (SourceMetadata("CONTOSO-DEMO-KNOWLEDGE-001", "test-knowledge", "1", "Test", "contoso-demo",
                        "demo-source", frozenset({"Approved intervention"})),),
    )


def test_distinct_responses_provider_returns_exact_text_and_attribution():
    client = Client(Stream())
    adapter = FoundryResponsesAdapter(
        endpoint=ENDPOINT, approved_origins=frozenset({ORIGIN}), client_factory=client.factory,
    )
    response = asyncio.run(adapter.answer(entry(), "Registered knowledge question", TOKEN))
    assert response.text == ANSWER
    assert response.protocol == Protocol.FOUNDRY_RESPONSES
    assert response.protocol.value == "foundry-responses"
    assert response.channel == "foundry-responses-sdk"
    assert response.capability == Capability.KNOWLEDGE_KNOWLEDGE
    assert response.variant is None
    assert response.state == "completed"
    assert response.endpoint == ENDPOINT
    assert extract_citations(entry(), response)[0].section == "Approved intervention"
    assert client.calls == [{"input": "Registered knowledge question", "store": False}]
    assert client.factory_calls[0][0] == ENDPOINT.split("responses?")[0]
    assert client.closed and client.stream.closed


@pytest.mark.parametrize("change", ["audience", "scope", "endpoint", "protocol", "consumer"])
def test_adapter_rejects_mismatched_route_before_any_sdk_call(change):
    selected = entry()
    if change == "audience":
        selected = replace(selected, auth=replace(selected.auth, audience="https://wrong.example"))
    elif change == "scope":
        selected = replace(selected, auth=replace(selected.auth, scopes=frozenset({"wrong"})))
    elif change == "endpoint":
        selected = replace(selected, endpoint=ENDPOINT.replace("test-knowledge", "other-agent"))
    elif change == "protocol":
        selected = replace(selected, protocol=Protocol.A2A)
    else:
        selected = replace(selected, capability=Capability.SUPPLIER_DELIVERY_TERMS,
                           variant=STUDIOVariant.STANDARD, protocol=Protocol.SDK_D2E)
    client = Client(Stream())
    adapter = FoundryResponsesAdapter(
        endpoint=ENDPOINT, approved_origins=frozenset({ORIGIN}), client_factory=client.factory,
    )
    with pytest.raises(ConfigurationError):
        asyncio.run(adapter.answer(selected, "question", TOKEN))
    assert client.factory_calls == []


def test_actual_sdk_wire_is_stateless_fixed_native_responses_and_no_retries(monkeypatch, caplog):
    import openai

    real_default_client = openai.DefaultAsyncHttpxClient
    http_root = next(
        base.__module__.split(".")[0] for base in real_default_client.__mro__
        if base.__module__.split(".")[0] in ("httpx", "httpx2")
    )
    http = importlib.import_module(http_root)
    calls = []

    def handle(request):
        calls.append(request)
        return http.Response(200, json=payload(), request=request)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient",
                        lambda **kwargs: real_default_client(transport=http.MockTransport(handle), **kwargs))
    adapter = FoundryResponsesTransport(endpoint=ENDPOINT, approved_origins=frozenset({ORIGIN}))
    assert asyncio.run(adapter.invoke("actual SDK, stubbed HTTP", TOKEN)) == ANSWER
    assert len(calls) == 1
    assert str(calls[0].url) == ENDPOINT
    assert calls[0].method == "POST"
    assert calls[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(calls[0].content) == {"input": "actual SDK, stubbed HTTP", "store": False}
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("status", ["in_progress", "failed", "incomplete", "queued", None])
def test_only_completed_response_can_be_an_answer(status):
    value = payload()
    value["status"] = status
    with pytest.raises(ProtocolError):
        asyncio.run(transport(Client(Stream(value))).invoke("question", TOKEN))


@pytest.mark.parametrize("field,value", [
    ("error", {"message": "secret-upstream-detail"}),
    ("incomplete_details", {"reason": "max_output_tokens"}),
    ("output", []), ("output", None), ("output", ["malformed"]),
    ("object", "not-a-response"), ("id", None),
])
def test_invalid_or_error_response_is_never_an_answer(field, value):
    result = payload()
    result[field] = value
    with pytest.raises(ProtocolError) as caught:
        asyncio.run(transport(Client(Stream(result))).invoke("question", TOKEN))
    assert "secret-upstream-detail" not in str(caught.value)


@pytest.mark.parametrize("part", [
    {"type": "refusal", "refusal": "blocked"},
    {"type": "output_text", "text": 42}, {"type": "output_text", "text": ""},
])
def test_no_refusal_or_nontext_success(part):
    value = payload()
    value["output"][0]["content"] = [part]
    with pytest.raises(ProtocolError):
        asyncio.run(transport(Client(Stream(value))).invoke("question", TOKEN))


@pytest.mark.parametrize("raw", [b"invalid JSON", b"\xff", b'{"id":"a","id":"b"}'])
def test_malformed_and_duplicate_json_fail_closed(raw):
    with pytest.raises(ProtocolError):
        asyncio.run(transport(Client(Stream(raw=raw))).invoke("question", TOKEN))


@pytest.mark.parametrize("status", [302, 307, 401, 403, 500])
def test_http_errors_and_redirects_do_not_fall_back(status):
    client = Client(Stream(status=status))
    with pytest.raises(TransportError):
        asyncio.run(transport(client).invoke("question", TOKEN))
    assert len(client.calls) == 1
    assert client.closed and client.stream.closed


@pytest.mark.parametrize("endpoint", [
    ENDPOINT.replace(ORIGIN, "https://foreign.services.ai.azure.com"),
    ENDPOINT.replace("?api-version=v1", "?api-version=v2"),
    ENDPOINT + "&url=https://foreign.example",
    ENDPOINT.replace("openai/responses", "a2a"),
    ENDPOINT.replace("/test-knowledge/", "/../"),
    "https://demo.services.ai.azure.com/arbitrary-proxy",
])
def test_only_fixed_approved_native_endpoint_is_allowed(endpoint):
    with pytest.raises(ConfigurationError):
        FoundryResponsesTransport(endpoint=endpoint, approved_origins=frozenset({ORIGIN}))


def test_query_exception_remains_narrow_for_catalogs():
    assert entry().endpoint == ENDPOINT
    with pytest.raises(CatalogError):
        replace(entry(), endpoint=ENDPOINT.replace("/openai/responses?", "/openai/arbitrary?"))


@pytest.mark.parametrize("token", ["", " ", None, "unsafe\nheader", "unsafe token", "unsafe\x7f"])
def test_missing_or_unsafe_bearers_cannot_call_sdk(token):
    client = Client(Stream())
    with pytest.raises(ConfigurationError):
        asyncio.run(transport(client).invoke("question", token))
    assert not client.factory_calls


def test_response_and_input_byte_limits():
    with pytest.raises(ResponseTooLarge):
        asyncio.run(transport(Client(Stream(raw=b"x" * 1000)), max_response_bytes=100).invoke(
            "question", TOKEN))
    client = Client(Stream())
    with pytest.raises(ConfigurationError):
        asyncio.run(transport(client, max_input_bytes=10).invoke("é" * 6, TOKEN))
    assert not client.factory_calls


def test_overall_timeout_closes_resources_without_protocol_fallback():
    client = Client(Stream(delay=0.1))
    with pytest.raises(ProviderTimeout):
        asyncio.run(transport(client, overall_timeout=0.001).invoke("question", TOKEN))
    assert client.closed and client.stream.closed
    assert len(client.calls) == 1


def test_cancellation_closes_native_async_client_without_background_worker():
    client = Client(Stream(delay=60))

    async def run():
        client.stream.started = asyncio.Event()
        task = asyncio.create_task(transport(client).invoke("question", TOKEN))
        await client.stream.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert client.closed and client.stream.closed


def test_per_call_platform_context_is_not_an_identity_override(caplog):
    client = Client(Stream())
    values = iter(["verified-one", "verified-two"])
    sdk = transport(client, platform_headers_factory=lambda: {"x-agent-foundry-call-id": next(values)})

    async def run():
        await sdk.invoke("question-one", "fresh-token-one")
        return await sdk.invoke("question-two", "fresh-token-two")

    assert asyncio.run(run()) == ANSWER
    assert [call[1:3] for call in client.factory_calls] == [
        ("fresh-token-one", {"x-agent-foundry-call-id": "verified-one"}),
        ("fresh-token-two", {"x-agent-foundry-call-id": "verified-two"}),
    ]
    assert "verified-" not in caplog.text
    assert "fresh-token" not in caplog.text
    bad = transport(Client(Stream()), platform_headers_factory=lambda: {"user-id": "forbidden"})
    with pytest.raises(ConfigurationError):
        asyncio.run(bad.invoke("question", TOKEN))


def test_sdk_errors_are_safe(caplog):
    from openai import OpenAIError

    client = Client(Stream(error=OpenAIError(TOKEN + " internal-trace-detail")))
    with pytest.raises(TransportError) as caught:
        asyncio.run(transport(client).invoke("question", TOKEN))
    assert TOKEN not in str(caught.value) + caplog.text
    assert "internal-trace-detail" not in str(caught.value) + caplog.text
    assert caught.value.__suppress_context__
