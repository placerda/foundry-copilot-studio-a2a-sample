"""Network-free contract tests, not evidence of live provider connectivity."""

import asyncio
import builtins
from contextvars import ContextVar
from dataclasses import dataclass, replace
import json
import socket
import threading
import traceback
from uuid import UUID

import pytest
import requests

from contoso.router.core import (
    AuthMetadata, AuthScheme, Capability, CatalogEntry, STUDIOVariant, Protocol,
    SourceMetadata,
)
from contoso.providers import (
    A2AAdapter, A2ATransport, ConfigurationError, ProtocolError, ProviderTimeout,
    ResponseTooLarge, StandardSDKAdapter, TransportError,
    standard_catalog_endpoint,
)
from contoso.providers.a2a import A2A_SCOPE
from contoso.providers.standard import STANDARD_SCOPE

ORIGINS = frozenset({"https://api.powerplatform.com"})
TOKEN = "test-in-memory-token"
QUESTION = "test-question-do-not-log"


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair
    internal = threading.local()

    def blocked(*args, **kwargs):
        pytest.fail("Transport unit tests must not use the network")

    def connect(sock, address):
        if getattr(internal, "socketpair", False):
            return original_connect(sock, address)
        blocked()

    def socketpair(*args, **kwargs):
        # Windows asyncio uses a stdlib loopback socket pair for its wakeup pipe.
        internal.socketpair = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            internal.socketpair = False

    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(requests.Session, "request", blocked)


def entry(variant=STUDIOVariant.COPILOT_HARNESS):
    scope = A2A_SCOPE if variant == STUDIOVariant.COPILOT_HARNESS else STANDARD_SCOPE
    return CatalogEntry(
        provider_id="test-studio", capability=Capability.SUPPLIER_DELIVERY_TERMS,
        variant=variant,
        endpoint=("https://api.powerplatform.com/copilotstudio-a2a/environments/"
                  "test-environment/agents/test_schema"),
        protocol=Protocol.A2A if variant == STUDIOVariant.COPILOT_HARNESS else Protocol.SDK_D2E,
        auth=AuthMetadata(AuthScheme.ENTRA_BEARER, "test-tenant",
                          "https://api.powerplatform.com", frozenset({scope})),
        sources=(SourceMetadata("test-source", "test-studio", "v1", "Test", "internal",
                                "test-reference", frozenset({"Supported section"})),),
    )


def message_result(request_id):
    return {"jsonrpc": "2.0", "id": request_id,
            "result": {"message": {"parts": [{"text": "First"}, {"text": "Second"}]}}}


def task_result(request_id):
    return {"jsonrpc": "2.0", "id": request_id, "result": {"task": {
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"parts": [{"text": "First"}]}, {"parts": [{"text": "Second"}]}],
    }}}


class StubResponse(requests.Response):
    def __init__(self, content, status=200, history=(), chunk_error=None):
        super().__init__()
        self.status_code = status
        self.history = list(history)
        self.content_bytes = content
        self.chunk_error = chunk_error
        self.closed = False

    def iter_content(self, chunk_size=1, decode_unicode=False):
        if self.chunk_error is not None:
            raise self.chunk_error
        for start in range(0, len(self.content_bytes), chunk_size):
            yield self.content_bytes[start:start + chunk_size]

    def close(self):
        self.closed = True


class StubSession(requests.Session):
    def __init__(self, build=message_result, *, status=200, history=(),
                 raw=None, error=None, chunk_error=None):
        super().__init__()
        self.build = build
        self.status = status
        self.history_items = history
        self.raw = raw
        self.error = error
        self.chunk_error = chunk_error
        self.calls = []
        self.response = None
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        content = (self.raw if self.raw is not None
                   else json.dumps(self.build(kwargs["json"]["id"])).encode())
        self.response = StubResponse(content, self.status, self.history_items, self.chunk_error)
        return self.response

    def close(self):
        self.closed = True


def a2a(session=None, **kwargs):
    return A2AAdapter(approved_origins=ORIGINS,
                      session_factory=lambda: session or StubSession(), **kwargs)


def test_native_foundry_a2a_transport_preserves_version_query():
    endpoint = (
        "https://contoso.services.ai.azure.com/api/projects/project/agents/"
        "knowledge-demo/endpoint/protocols/a2a?api-version=v1"
    )
    session = StubSession()
    transport = A2ATransport(
        endpoint=endpoint, approved_origins=frozenset({"https://contoso.services.ai.azure.com"}),
        session_factory=lambda: session,
    )
    assert asyncio.run(transport.invoke(QUESTION, TOKEN)) == "First\nSecond"
    assert session.calls[0][0] == endpoint


@pytest.mark.parametrize("build,state", [
    (message_result, None), (task_result, "TASK_STATE_COMPLETED"),
])
def test_a2a_verified_shapes_and_request(build, state):
    session = StubSession(build)
    selected = entry()
    result = asyncio.run(a2a(session).answer(selected, QUESTION, TOKEN))
    assert result.text == "First\nSecond"
    assert result.protocol == Protocol.A2A
    assert result.variant == STUDIOVariant.COPILOT_HARNESS
    assert result.state == state
    assert result.channel == "native-a2a"
    assert result.endpoint == selected.endpoint
    assert result.provider_id == selected.provider_id
    assert result.capability == selected.capability
    assert result.environment_id is None
    assert not hasattr(result, "citations")
    url, kwargs = session.calls[0]
    assert url == selected.endpoint
    assert kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}", "A2A-Version": "1.0"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert kwargs["timeout"] == (5, 30)
    payload = kwargs["json"]
    assert payload["jsonrpc"] == "2.0"
    assert payload["method"] == "SendMessage"
    assert str(UUID(payload["id"])) == payload["id"]
    message = payload["params"]["message"]
    assert str(UUID(message["messageId"])) == message["messageId"]
    assert message["messageId"] != payload["id"]
    assert message["role"] == "ROLE_USER"
    assert message["parts"] == [{"text": QUESTION}]
    assert payload["params"]["configuration"] == {
        "acceptedOutputModes": ["text/plain"], "returnImmediately": False,
    }
    assert session.closed and session.response.closed
    assert session.trust_env is False


def with_error(request_id):
    payload = message_result(request_id)
    payload["error"] = {"message": f"{TOKEN} {QUESTION}", "code": -1}
    return payload


@pytest.mark.parametrize("build", [
    with_error,
    lambda rid: message_result("mismatched-id"),
    lambda rid: {"jsonrpc": "1.0", "id": rid, "result": {}},
    lambda rid: [],
    lambda rid: {"jsonrpc": "2.0", "id": rid, "result": None},
    lambda rid: {"jsonrpc": "2.0", "id": rid, "result": []},
    lambda rid: {"jsonrpc": "2.0", "id": rid, "result": {}},
    lambda rid: {"jsonrpc": "2.0", "id": rid,
                 "result": {"message": {}, "task": {}}},
    lambda rid: {"jsonrpc": "2.0", "id": rid, "result": {"task": {}}},
    lambda rid: {"jsonrpc": "2.0", "id": rid, "result": {"message": {"parts": None}}},
    lambda rid: {"jsonrpc": "2.0", "id": rid, "result": {"message": {"parts": [None]}}},
    lambda rid: {"jsonrpc": "2.0", "id": rid,
                 "result": {"message": {"parts": [{"text": 3}]}}},
    lambda rid: {"jsonrpc": "2.0", "id": rid,
                 "result": {"message": {"parts": [{"text": " "}]}}},
])
def test_a2a_rejects_invalid_envelopes(build):
    with pytest.raises(ProtocolError) as caught:
        asyncio.run(a2a(StubSession(build)).answer(entry(), QUESTION, TOKEN))
    assert TOKEN not in str(caught.value)
    assert QUESTION not in str(caught.value)


@pytest.mark.parametrize("state", [
    "TASK_STATE_PENDING", "TASK_STATE_WORKING", "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED", "TASK_STATE_INPUT_REQUIRED", None,
])
def test_a2a_rejects_noncompleted_tasks(state):
    def build(rid):
        payload = task_result(rid)
        payload["result"]["task"]["status"]["state"] = state
        return payload
    with pytest.raises(ProtocolError, match="did not complete"):
        asyncio.run(a2a(StubSession(build)).answer(entry(), QUESTION, TOKEN))


@pytest.mark.parametrize("artifacts", [None, {}, [], [{"parts": []}], [{"parts": "bad"}]])
def test_a2a_rejects_missing_artifact_text(artifacts):
    def build(rid):
        payload = task_result(rid)
        payload["result"]["task"]["artifacts"] = artifacts
        return payload
    with pytest.raises(ProtocolError):
        asyncio.run(a2a(StubSession(build)).answer(entry(), QUESTION, TOKEN))


@pytest.mark.parametrize("status,history", [(302, ()), (307, ()), (401, ()), (500, ()),
                                          (200, (requests.Response(),))])
def test_a2a_rejects_http_errors_and_redirects(status, history):
    session = StubSession(status=status, history=history)
    with pytest.raises(TransportError, match="without redirects"):
        asyncio.run(a2a(session).answer(entry(), QUESTION, TOKEN))
    assert len(session.calls) == 1


@pytest.mark.parametrize("raw", [b"not JSON", b"\xff", b'{"result":', b"null"])
def test_a2a_rejects_malformed_json(raw):
    with pytest.raises(ProtocolError):
        asyncio.run(a2a(StubSession(raw=raw)).answer(entry(), QUESTION, TOKEN))


def test_a2a_bounds_bytes_and_closes():
    session = StubSession(raw=b"x" * 20_000)
    with pytest.raises(ResponseTooLarge):
        asyncio.run(a2a(session, max_response_bytes=100).answer(entry(), QUESTION, TOKEN))
    assert session.response.closed and session.closed


@pytest.mark.parametrize("error,expected", [
    (requests.Timeout(f"Authorization: Bearer {TOKEN} {QUESTION}"), ProviderTimeout),
    (requests.ConnectionError(f"Authorization: Bearer {TOKEN} {QUESTION}"), TransportError),
])
@pytest.mark.parametrize("stream_error", [False, True])
def test_a2a_errors_are_safe(error, expected, stream_error, caplog):
    session = StubSession(**({"chunk_error": error} if stream_error else {"error": error}))
    with pytest.raises(expected) as caught:
        asyncio.run(a2a(session).answer(entry(), QUESTION, TOKEN))
    formatted = "".join(traceback.format_exception_only(caught.type, caught.value))
    assert TOKEN not in formatted + caplog.text
    assert QUESTION not in formatted + caplog.text
    assert "Authorization" not in formatted + caplog.text
    assert caught.value.__suppress_context__


def test_a2a_overall_timeout():
    class SlowAdapter(A2AAdapter):
        def _send(self, endpoint, question, token, *, platform_headers=None):
            import time
            time.sleep(0.02)
            return "late", None
    adapter = SlowAdapter(approved_origins=ORIGINS, overall_timeout=0.001)
    with pytest.raises(ProviderTimeout):
        asyncio.run(adapter.answer(entry(), QUESTION, TOKEN))


@dataclass
class StubConversation:
    id: str | None


@dataclass
class StubActivity:
    type: str = "message"
    text: str | None = None
    conversation: StubConversation | None = None


class StubClient:
    def __init__(self, *, start=None, messages=None, error=None, delay=0):
        self.start = ([StubActivity(type="event", conversation=StubConversation("conversation-1"))]
                      if start is None else start)
        self.messages = [StubActivity(text="Answer")] if messages is None else messages
        self.error = error
        self.delay = delay
        self.calls = []

    async def start_conversation(self, emit_start_conversation_event):
        self.calls.append(("start", emit_start_conversation_event))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        for activity in self.start:
            yield activity

    async def ask_question(self, question, conversation_id):
        self.calls.append(("ask", question, conversation_id))
        for activity in self.messages:
            yield activity


def standard(client=None, **kwargs):
    return StandardSDKAdapter(
        approved_origins=ORIGINS, environment_id="test-environment",
        agent_identifier="test_schema",
        client_factory=lambda env, agent, token: client or StubClient(), **kwargs,
    )


def test_standard_filters_activity_types_and_preserves_identity():
    client = StubClient(messages=[
        StubActivity(type="typing", text="Not an answer"),
        StubActivity(type="event", text="Not an answer"),
        StubActivity(text="First"), StubActivity(text=None), StubActivity(text="Second"),
    ])
    selected = entry(STUDIOVariant.STANDARD)
    reply = asyncio.run(standard(client).answer(selected, QUESTION, TOKEN))
    assert reply.text == "First\nSecond"
    assert reply.protocol == Protocol.SDK_D2E
    assert reply.variant == STUDIOVariant.STANDARD
    assert reply.state is None
    assert reply.channel == "copilotstudio-sdk"
    assert reply.environment_id == "test-environment"
    assert reply.agent_identifier == "test_schema"
    assert reply.conversation_id == "conversation-1"
    assert reply.endpoint == selected.endpoint
    assert not hasattr(reply, "citations")
    assert client.calls == [("start", True), ("ask", QUESTION, "conversation-1")]


@pytest.mark.parametrize("start", [
    [], [StubActivity()], [StubActivity(conversation=StubConversation(""))],
    [StubActivity(conversation=StubConversation("first")),
     StubActivity(conversation=StubConversation("second"))],
])
def test_standard_requires_valid_unique_conversation(start):
    client = StubClient(start=start)
    with pytest.raises(ProtocolError):
        asyncio.run(standard(client).answer(entry(STUDIOVariant.STANDARD), QUESTION, TOKEN))
    assert len(client.calls) == 1


@pytest.mark.parametrize("messages", [
    [], [StubActivity(type="typing", text="Ignored")],
    [StubActivity(text=" ")], [StubActivity(text=42)],
])
def test_standard_requires_message_text(messages):
    with pytest.raises(ProtocolError):
        asyncio.run(standard(StubClient(messages=messages)).answer(
            entry(STUDIOVariant.STANDARD), QUESTION, TOKEN))


def test_standard_timeout():
    with pytest.raises(ProviderTimeout):
        asyncio.run(standard(StubClient(delay=0.02), overall_timeout=0.001).answer(
            entry(STUDIOVariant.STANDARD), QUESTION, TOKEN))


@pytest.mark.parametrize("activity_type", ["message", "event", "typing"])
def test_standard_bounds_utf8_response_bytes(activity_type):
    client = StubClient(messages=[StubActivity(type=activity_type, text="é" * 100)])
    with pytest.raises(ResponseTooLarge):
        asyncio.run(standard(client, max_response_bytes=100).answer(
            entry(STUDIOVariant.STANDARD), QUESTION, TOKEN))


def test_standard_bounds_start_stream():
    client = StubClient(start=[StubActivity(text="x" * 1000)])
    with pytest.raises(ResponseTooLarge):
        asyncio.run(standard(client, max_response_bytes=100).answer(
            entry(STUDIOVariant.STANDARD), QUESTION, TOKEN))


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, ValueError, TypeError])
def test_standard_safe_errors(error_type, caplog):
    client = StubClient(error=error_type(f"Bearer {TOKEN} {QUESTION}"))
    with pytest.raises(TransportError) as caught:
        asyncio.run(standard(client).answer(entry(STUDIOVariant.STANDARD), QUESTION, TOKEN))
    assert TOKEN not in str(caught.value) + caplog.text
    assert QUESTION not in str(caught.value) + caplog.text
    assert caught.value.__suppress_context__


def test_standard_lazy_default_sdk_and_diagnostics(monkeypatch, caplog):
    captured = {}
    original = builtins.__import__

    class Settings:
        def __init__(self, **kwargs):
            captured["settings"] = kwargs

    class Module:
        ConnectionSettings = Settings

        @staticmethod
        def CopilotClient(settings, token):
            captured["token"] = token
            return StubClient()

    class HTTPModule:
        class ClientError(Exception):
            pass

    class DiscoveryModule:
        @staticmethod
        def get_discovery_url(settings):
            return entry(STUDIOVariant.STANDARD).endpoint

    def fake_import(name, *args, **kwargs):
        if name == "microsoft_agents.copilotstudio.client":
            return Module
        if name == "aiohttp":
            return HTTPModule
        if name == "microsoft_agents.copilotstudio.client._conversation_endpoint_discovery":
            return DiscoveryModule
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    adapter = StandardSDKAdapter(
        approved_origins=ORIGINS, environment_id="test-environment",
        agent_identifier="test_schema",
    )
    result = asyncio.run(adapter.answer(entry(STUDIOVariant.STANDARD), QUESTION, TOKEN))
    assert result.text == "Answer"
    assert captured["settings"] == {
        "environment_id": "test-environment", "agent_identifier": "test_schema",
        "enable_diagnostics": False,
    }
    assert captured["token"] == TOKEN
    assert TOKEN not in repr(result) + caplog.text


def test_standard_missing_sdk_is_safe_and_lazy(monkeypatch):
    original = builtins.__import__

    class HTTPModule:
        class ClientError(Exception):
            pass

    def missing(name, *args, **kwargs):
        if name == "microsoft_agents.copilotstudio.client":
            raise ImportError(f"secret {TOKEN}")
        if name == "aiohttp":
            return HTTPModule
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    adapter = StandardSDKAdapter(
        approved_origins=ORIGINS, environment_id="test-environment",
        agent_identifier="test_schema",
    )
    with pytest.raises(ConfigurationError) as caught:
        asyncio.run(adapter.answer(entry(STUDIOVariant.STANDARD), QUESTION, TOKEN))
    assert TOKEN not in str(caught.value)
    assert asyncio.run(standard().answer(entry(STUDIOVariant.STANDARD), QUESTION, TOKEN)).text == "Answer"


@pytest.mark.parametrize("adapter,variant", [
    (a2a, STUDIOVariant.COPILOT_HARNESS), (standard, STUDIOVariant.STANDARD),
])
@pytest.mark.parametrize("token", ["", " ", "bad\nheader", "bad token", None, 42])
def test_rejects_empty_or_invalid_tokens(adapter, variant, token):
    with pytest.raises(ConfigurationError):
        asyncio.run(adapter().answer(entry(variant), QUESTION, token))


@pytest.mark.parametrize("adapter,variant", [
    (a2a, STUDIOVariant.COPILOT_HARNESS), (standard, STUDIOVariant.STANDARD),
])
@pytest.mark.parametrize("question", ["", " ", None, 42])
def test_rejects_empty_questions(adapter, variant, question):
    with pytest.raises(ConfigurationError):
        asyncio.run(adapter().answer(entry(variant), question, TOKEN))


@pytest.mark.parametrize("adapter,variant", [
    (a2a, STUDIOVariant.STANDARD), (standard, STUDIOVariant.COPILOT_HARNESS),
])
def test_rejects_other_variant_without_fallback(adapter, variant):
    with pytest.raises(ConfigurationError, match="unsupported"):
        asyncio.run(adapter().answer(entry(variant), QUESTION, TOKEN))


@pytest.mark.parametrize("adapter,variant", [
    (a2a, STUDIOVariant.COPILOT_HARNESS), (standard, STUDIOVariant.STANDARD),
])
def test_rejects_unapproved_origin_and_missing_scope(adapter, variant):
    selected = entry(variant)
    with pytest.raises(ConfigurationError):
        asyncio.run(adapter().answer(
            replace(selected, endpoint="https://unapproved.example/agents/test"), QUESTION, TOKEN))
    with pytest.raises(ConfigurationError, match="scope"):
        asyncio.run(adapter().answer(
            replace(selected, auth=replace(selected.auth, scopes=frozenset({"wrong-scope"}))),
            QUESTION, TOKEN))
    with pytest.raises(ConfigurationError):
        asyncio.run(adapter().answer(None, QUESTION, TOKEN))


@pytest.mark.parametrize("endpoint", [
    "https://api.powerplatform.com/obsolete/message/send",
    "https://api.powerplatform.com/copilotstudio-a2a/test/agents/",
    "https://api.powerplatform.com:8443/copilotstudio-a2a/test/agents/schema",
])
def test_rejects_invalid_native_endpoint(endpoint):
    with pytest.raises(ConfigurationError):
        asyncio.run(a2a().answer(replace(entry(), endpoint=endpoint), QUESTION, TOKEN))


@pytest.mark.parametrize("field,value", [
    ("environment_id", ""), ("agent_identifier", None),
    ("agent_identifier", "https://untrusted.example"), ("environment_id", " bad "),
])
def test_standard_invalid_configuration(field, value):
    kwargs = {"approved_origins": ORIGINS, "environment_id": "test-env",
              "agent_identifier": "test_schema"}
    kwargs[field] = value
    with pytest.raises(ConfigurationError):
        StandardSDKAdapter(**kwargs)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "10"])
def test_invalid_timeout_configuration(timeout):
    with pytest.raises(ConfigurationError):
        a2a(overall_timeout=timeout)
    with pytest.raises(ConfigurationError):
        standard(overall_timeout=timeout)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_byte_limit_configuration(limit):
    with pytest.raises(ConfigurationError):
        a2a(max_response_bytes=limit)
    with pytest.raises(ConfigurationError):
        standard(max_response_bytes=limit)


@pytest.mark.parametrize("origins", [
    None, frozenset(), frozenset({"http://api.powerplatform.com"}),
    frozenset({"https://api.powerplatform.com/path"}),
    frozenset({"https://api.powerplatform.com?secret=value"}),
])
def test_invalid_approved_origin_configuration(origins):
    with pytest.raises(ConfigurationError):
        A2AAdapter(approved_origins=origins)
    with pytest.raises(ConfigurationError):
        StandardSDKAdapter(approved_origins=origins, environment_id="test-env",
                           agent_identifier="test_schema")


def test_standard_non_studio_capability_is_rejected():
    selected = replace(entry(), capability=Capability.KNOWLEDGE_KNOWLEDGE, variant=None)
    with pytest.raises(ConfigurationError, match="unsupported"):
        asyncio.run(standard().answer(selected, QUESTION, TOKEN))


def test_standard_answer_cannot_switch_conversation():
    client = StubClient(messages=[
        StubActivity(text="Unrelated", conversation=StubConversation("other-conversation")),
    ])
    with pytest.raises(ProtocolError, match="conversation ID changed"):
        asyncio.run(standard(client).answer(entry(STUDIOVariant.STANDARD), QUESTION, TOKEN))


def test_standard_rejects_malformed_text_encoding():
    client = StubClient(messages=[StubActivity(text="\ud800")])
    with pytest.raises(ProtocolError, match="encoding"):
        asyncio.run(standard(client).answer(entry(STUDIOVariant.STANDARD), QUESTION, TOKEN))


def test_invalid_factories():
    with pytest.raises(ConfigurationError):
        A2AAdapter(approved_origins=ORIGINS, session_factory=42)
    with pytest.raises(ConfigurationError):
        StandardSDKAdapter(approved_origins=ORIGINS, environment_id="test-env",
                           agent_identifier="test_schema", client_factory=42)


def test_known_source_id_in_text_does_not_create_attribution():
    client = StubClient(messages=[StubActivity(text="I mention test-source but cite nothing.")])
    result = asyncio.run(standard(client).answer(entry(STUDIOVariant.STANDARD), QUESTION, TOKEN))
    assert result.text == "I mention test-source but cite nothing."
    assert not hasattr(result, "citations")
    assert not hasattr(result, "sources")


def knowledge_entry():
    selected = entry()
    return replace(
        selected, provider_id="test-knowledge", capability=Capability.KNOWLEDGE_KNOWLEDGE,
        variant=None, endpoint="https://foundry.example/projects/test/agents/knowledge/a2a",
        auth=replace(selected.auth, audience="https://ai.azure.com",
                     scopes=frozenset({"https://ai.azure.com/.default"})),
        sources=(replace(selected.sources[0], provider_id="test-knowledge",
                         source_id="CONTOSO-DEMO-KNOWLEDGE-001", version="1",
                         sections=frozenset({"Approved intervention"})),),
    )


def knowledge(session, **kwargs):
    return A2AAdapter(
        approved_origins=frozenset({"https://foundry.example"}),
        session_factory=lambda: session, **kwargs,
    )


@pytest.mark.parametrize("build,state", [
    (message_result, None), (task_result, "TASK_STATE_COMPLETED"),
])
def test_knowledge_a2a_uses_same_protocol_without_consumer_variant(build, state):
    session = StubSession(build)
    selected = knowledge_entry()
    result = asyncio.run(knowledge(session).answer(selected, QUESTION, TOKEN))
    assert result.text == "First\nSecond"
    assert result.provider_id == "test-knowledge"
    assert result.capability == Capability.KNOWLEDGE_KNOWLEDGE
    assert result.variant is None
    assert result.protocol == Protocol.A2A
    assert result.endpoint == selected.endpoint
    assert result.state == state
    assert result.channel == "a2a"
    assert session.calls[0][1]["json"]["method"] == "SendMessage"
    assert session.calls[0][1]["headers"] == {
        "Authorization": f"Bearer {TOKEN}", "A2A-Version": "1.0",
    }


def test_knowledge_rejects_sdk_and_unapproved_origin_before_transport():
    session = StubSession()
    selected = knowledge_entry()
    with pytest.raises(ConfigurationError, match="unsupported"):
        asyncio.run(knowledge(session).answer(
            replace(selected, protocol=Protocol.SDK_D2E), QUESTION, TOKEN))
    with pytest.raises(ConfigurationError):
        asyncio.run(knowledge(session).answer(
            replace(selected, endpoint="https://unapproved.example/a2a"), QUESTION, TOKEN))
    assert session.calls == []


@pytest.mark.parametrize("build", [with_error, lambda rid: message_result("wrong-id")])
def test_knowledge_retains_strict_rpc_validation(build):
    with pytest.raises(ProtocolError):
        asyncio.run(knowledge(StubSession(build)).answer(knowledge_entry(), QUESTION, TOKEN))


def test_knowledge_retains_completed_task_requirement():
    def build(rid):
        payload = task_result(rid)
        payload["result"]["task"]["status"]["state"] = "TASK_STATE_FAILED"
        return payload
    with pytest.raises(ProtocolError):
        asyncio.run(knowledge(StubSession(build)).answer(knowledge_entry(), QUESTION, TOKEN))


def test_platform_factory_uses_each_calling_tasks_verified_context(caplog):
    context = ContextVar("verified-foundry-call-context")
    sessions = []
    factory_calls = []

    def platform_factory():
        value = context.get()
        factory_calls.append(value)
        return {"x-agent-foundry-call-id": value}

    def session_factory():
        session = StubSession()
        sessions.append(session)
        return session

    adapter = A2AAdapter(
        approved_origins=frozenset({"https://foundry.example"}),
        session_factory=session_factory, platform_headers_factory=platform_factory,
    )

    async def ask(call_id):
        context.set(call_id)
        return await adapter.answer(knowledge_entry(), QUESTION, TOKEN)

    async def run():
        return await asyncio.gather(ask("verified-call-one"), ask("verified-call-two"))

    results = asyncio.run(run())
    assert factory_calls == ["verified-call-one", "verified-call-two"]
    assert {session.calls[0][1]["headers"]["x-agent-foundry-call-id"]
            for session in sessions} == {"verified-call-one", "verified-call-two"}
    for session in sessions:
        headers = session.calls[0][1]["headers"]
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers["A2A-Version"] == "1.0"
        assert "verified-call" not in json.dumps(session.calls[0][1]["json"])
    for result in results:
        assert result.channel == "foundry-a2a"
        assert "verified-call" not in repr(result) + caplog.text


@pytest.mark.parametrize("headers", [
    {}, None, [], {"Authorization": "Bearer forbidden"},
    {"x-agent-foundry-call-id": "good", "user-id": "forbidden"},
    {"x-agent-foundry-call-id": "good", "x-agent-foundry-user-id": "forbidden"},
    {"x-agent-foundry-call-id": "good", "A2A-Version": "obsolete"},
    {"X-Agent-Foundry-Call-Id": "wrong-case"},
    {"x-agent-foundry-call-id": ""}, {"x-agent-foundry-call-id": " "},
    {"x-agent-foundry-call-id": None}, {"x-agent-foundry-call-id": 42},
    {"x-agent-foundry-call-id": "bad\r\nheader"},
    {"x-agent-foundry-call-id": "bad\x00context"},
    {"x-agent-foundry-call-id": "bad\x7fcontext"},
    {"x-agent-foundry-call-id": "\u200bcontext"},
    {"x-agent-foundry-call-id": " leading-space"},
])
def test_invalid_platform_headers_fail_closed_before_post(headers, caplog):
    session = StubSession()
    with pytest.raises(ConfigurationError) as caught:
        asyncio.run(knowledge(session, platform_headers_factory=lambda: headers).answer(
            knowledge_entry(), QUESTION, TOKEN))
    assert session.calls == []
    assert TOKEN not in str(caught.value) + caplog.text
    assert "forbidden" not in str(caught.value) + caplog.text
    assert "bad" not in str(caught.value) + caplog.text


def test_invalid_platform_factory_and_safe_factory_failure(caplog):
    with pytest.raises(ConfigurationError):
        knowledge(StubSession(), platform_headers_factory=42)

    def unavailable():
        raise LookupError("internal-verified-context-do-not-expose")

    session = StubSession()
    with pytest.raises(ConfigurationError) as caught:
        asyncio.run(knowledge(session, platform_headers_factory=unavailable).answer(
            knowledge_entry(), QUESTION, TOKEN))
    assert session.calls == []
    assert "internal-verified-context" not in str(caught.value) + caplog.text
    assert caught.value.__suppress_context__


def test_platform_context_cannot_be_applied_to_studio():
    session = StubSession()
    calls = []

    def platform_factory():
        calls.append(True)
        return {"x-agent-foundry-call-id": "verified"}

    adapter = A2AAdapter(approved_origins=ORIGINS, session_factory=lambda: session,
                         platform_headers_factory=platform_factory)
    with pytest.raises(ConfigurationError, match="supplier-delivery"):
        asyncio.run(adapter.answer(entry(), QUESTION, TOKEN))
    assert calls == []
    assert session.calls == []


HOSTED_ENDPOINT = "https://foundry.example/projects/test/agents/hosted-router/a2a"
HOSTED_ENVELOPE = '{"question":"Test retailer question","studio_variant":"standard"}'
HOSTED_OUTPUT = '{"status":"answered","answer":"Test runtime result"}'


def hosted(session, **kwargs):
    return A2ATransport(
        endpoint=HOSTED_ENDPOINT, approved_origins=frozenset({"https://foundry.example"}),
        session_factory=lambda: session, **kwargs,
    )


def hosted_completed(request_id):
    payload = task_result(request_id)
    payload["result"]["task"]["artifacts"] = [{"parts": [{"text": HOSTED_OUTPUT}]}]
    return payload


def test_hosted_orchestrator_transport_has_no_provider_or_source_metadata(monkeypatch, caplog):
    from contoso.providers import a2a as module
    from contoso.providers import citations

    def forbidden(*args, **kwargs):
        pytest.fail("Orchestrator transport must not construct provider/source metadata or citations")

    monkeypatch.setattr(module, "CatalogEntry", forbidden)
    monkeypatch.setattr(citations, "extract_citations", forbidden)
    session = StubSession(hosted_completed)
    transport = hosted(session)
    result = asyncio.run(transport.invoke(HOSTED_ENVELOPE, TOKEN))
    assert isinstance(result, str)
    assert result == HOSTED_OUTPUT
    assert TOKEN not in result + caplog.text
    url, kwargs = session.calls[0]
    assert url == HOSTED_ENDPOINT
    assert kwargs["json"]["method"] == "SendMessage"
    assert kwargs["json"]["params"]["message"]["parts"] == [{"text": HOSTED_ENVELOPE}]
    assert set(kwargs["json"]["params"]) == {"message", "configuration"}
    assert not hasattr(transport, "provider_id")
    assert not hasattr(transport, "sources")
    assert not hasattr(transport, "token")
    assert kwargs["allow_redirects"] is False
    assert session.closed and session.response.closed


def test_hosted_transport_leaves_application_json_validation_to_caller():
    result = asyncio.run(hosted(StubSession()).invoke(HOSTED_ENVELOPE, TOKEN))
    assert result == "First\nSecond"


@pytest.mark.parametrize("state", [
    "TASK_STATE_PENDING", "TASK_STATE_WORKING", "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED",
])
def test_hosted_transport_rejects_noncompleted_tasks(state):
    def build(rid):
        payload = hosted_completed(rid)
        payload["result"]["task"]["status"]["state"] = state
        return payload
    with pytest.raises(ProtocolError):
        asyncio.run(hosted(StubSession(build)).invoke(HOSTED_ENVELOPE, TOKEN))


@pytest.mark.parametrize("build", [
    with_error, lambda rid: hosted_completed("foreign-id"),
    lambda rid: {"jsonrpc": "2.0", "id": rid, "result": {}},
])
def test_hosted_transport_reuses_strict_rpc_envelope_validation(build):
    with pytest.raises(ProtocolError) as caught:
        asyncio.run(hosted(StubSession(build)).invoke(HOSTED_ENVELOPE, TOKEN))
    assert TOKEN not in str(caught.value)
    assert HOSTED_ENVELOPE not in str(caught.value)


@pytest.mark.parametrize("status,history", [
    (302, ()), (307, ()), (401, ()), (200, (requests.Response(),)),
])
def test_hosted_transport_rejects_redirects_and_http_errors(status, history):
    session = StubSession(status=status, history=history)
    with pytest.raises(TransportError):
        asyncio.run(hosted(session).invoke(HOSTED_ENVELOPE, TOKEN))
    assert len(session.calls) == 1


@pytest.mark.parametrize("endpoint", [
    "https://unapproved.example/router/a2a", "http://foundry.example/a2a",
    "https://127.0.0.1/a2a", "https://foundry.example/a2a?private-config=value",
    "https://user:private-config@foundry.example/a2a", None,
])
def test_hosted_endpoint_is_approved_administrator_config(endpoint):
    with pytest.raises(ConfigurationError) as caught:
        A2ATransport(endpoint=endpoint, approved_origins=frozenset({"https://foundry.example"}))
    assert "private-config" not in str(caught.value)


@pytest.mark.parametrize("token", ["", " ", "bad\nheader", "bad\x00token", "bad token", "badé", None])
def test_hosted_transport_rejects_missing_or_unsafe_bearers_before_post(token):
    session = StubSession()
    with pytest.raises(ConfigurationError):
        asyncio.run(hosted(session).invoke(HOSTED_ENVELOPE, token))
    assert session.calls == []


@pytest.mark.parametrize("text", ["", " ", None, {}])
def test_hosted_transport_requires_plain_nonempty_text(text):
    session = StubSession()
    with pytest.raises(ConfigurationError):
        asyncio.run(hosted(session).invoke(text, TOKEN))
    assert session.calls == []


def test_hosted_transport_uses_fresh_bearer_and_current_call_context_per_invocation(caplog):
    session = StubSession(hosted_completed)
    call_ids = iter(["verified-hosted-call-one", "verified-hosted-call-two"])
    transport = hosted(
        session, platform_headers_factory=lambda: {"x-agent-foundry-call-id": next(call_ids)},
    )

    async def run():
        first = await transport.invoke(HOSTED_ENVELOPE, "fresh-bearer-one")
        second = await transport.invoke(HOSTED_ENVELOPE, "fresh-bearer-two")
        return first, second

    assert asyncio.run(run()) == (HOSTED_OUTPUT, HOSTED_OUTPUT)
    assert [kwargs["headers"] for url, kwargs in session.calls] == [
        {"Authorization": "Bearer fresh-bearer-one", "A2A-Version": "1.0",
         "x-agent-foundry-call-id": "verified-hosted-call-one"},
        {"Authorization": "Bearer fresh-bearer-two", "A2A-Version": "1.0",
         "x-agent-foundry-call-id": "verified-hosted-call-two"},
    ]
    assert session.calls[0][1]["json"]["id"] != session.calls[1][1]["json"]["id"]
    assert "fresh-bearer" not in caplog.text
    assert "verified-hosted-call" not in caplog.text
    for url, kwargs in session.calls:
        assert "verified-hosted-call" not in json.dumps(kwargs["json"])


def test_hosted_transport_cannot_override_auth_with_platform_headers():
    session = StubSession()
    transport = hosted(session, platform_headers_factory=lambda: {"Authorization": TOKEN})
    with pytest.raises(ConfigurationError):
        asyncio.run(transport.invoke(HOSTED_ENVELOPE, TOKEN))
    assert session.calls == []


def test_hosted_transport_byte_bound():
    with pytest.raises(ResponseTooLarge):
        asyncio.run(hosted(StubSession(hosted_completed), max_response_bytes=20).invoke(
            HOSTED_ENVELOPE, TOKEN))


def test_hosted_transport_timeout_and_safe_socket_errors(caplog):
    session = StubSession(error=requests.Timeout(f"{TOKEN} {HOSTED_ENVELOPE} private-call-context"))
    with pytest.raises(ProviderTimeout) as caught:
        asyncio.run(hosted(session).invoke(HOSTED_ENVELOPE, TOKEN))
    assert caught.value.__suppress_context__
    assert TOKEN not in str(caught.value) + caplog.text
    assert HOSTED_ENVELOPE not in str(caught.value) + caplog.text
    assert "private-call-context" not in str(caught.value) + caplog.text


def test_hosted_transport_overall_deadline():
    class SlowTransport(A2ATransport):
        def _send(self, endpoint, question, token, *, platform_headers=None):
            import time
            time.sleep(0.02)
            return HOSTED_OUTPUT, "TASK_STATE_COMPLETED"

    transport = SlowTransport(endpoint=HOSTED_ENDPOINT,
                              approved_origins=frozenset({"https://foundry.example"}),
                              overall_timeout=0.001)
    with pytest.raises(ProviderTimeout):
        asyncio.run(transport.invoke(HOSTED_ENVELOPE, TOKEN))


def test_standard_catalog_endpoint_delegates_to_sdk_settings_without_token_or_network(monkeypatch):
    original_import = builtins.__import__
    captured = {}
    expected = (
        "https://000000000000000000000000000000.01.environment.api.powerplatform.com/"
        "copilotstudio/agents/demo_schema/conversation-endpoints"
    )

    class Settings:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class SDKModule:
        ConnectionSettings = Settings

    class DiscoveryModule:
        @staticmethod
        def get_discovery_url(settings):
            return expected

    def fake_import(name, *args, **kwargs):
        if name == "microsoft_agents.copilotstudio.client":
            return SDKModule
        if name == "microsoft_agents.copilotstudio.client._conversation_endpoint_discovery":
            return DiscoveryModule
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = standard_catalog_endpoint(
        environment_id="00000000-0000-0000-0000-000000000001", agent_identifier="demo_schema",
    )
    assert result == expected
    assert captured == {
        "environment_id": "00000000-0000-0000-0000-000000000001",
        "agent_identifier": "demo_schema", "enable_diagnostics": False,
    }
    assert "token" not in captured


def test_standard_default_requires_exact_configured_sdk_discovery_endpoint(monkeypatch):
    from contoso.providers import standard as module

    expected = (
        "https://test.environment.api.powerplatform.com/copilotstudio/agents/test_schema/"
        "conversation-endpoints"
    )
    monkeypatch.setattr(module, "standard_catalog_endpoint", lambda **kwargs: expected)
    adapter = StandardSDKAdapter(approved_origins=ORIGINS, environment_id="test-environment",
                                 agent_identifier="test_schema")
    with pytest.raises(ConfigurationError, match="does not match SDK"):
        asyncio.run(adapter.answer(entry(STUDIOVariant.STANDARD), QUESTION, TOKEN))


def test_standard_injected_client_accepts_approved_sdk_environment_endpoint():
    endpoint = (
        "https://test.environment.api.powerplatform.com/copilotstudio/agents/test_schema/"
        "conversation-endpoints"
    )
    selected = replace(entry(STUDIOVariant.STANDARD), endpoint=endpoint)
    adapter = StandardSDKAdapter(
        approved_origins=frozenset({"https://test.environment.api.powerplatform.com"}),
        environment_id="test-environment", agent_identifier="test_schema",
        client_factory=lambda env, agent, token: StubClient(),
    )
    assert asyncio.run(adapter.answer(selected, QUESTION, TOKEN)).endpoint == endpoint
