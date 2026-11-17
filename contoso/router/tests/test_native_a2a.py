"""Network-blocked native ASGI contracts, not live Foundry/A2A evidence."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import logging
import socket
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
import requests
from a2a.types import Message, Part, Role, SendMessageConfiguration, SendMessageRequest
from azure.ai.agentserver.core import get_request_context
from google.protobuf.json_format import MessageToDict

from contoso.identity_probe import build_probe_host
from contoso.router.core import (
    AccessDenied, AuthMetadata, AuthScheme, CallerGrant, Capability, Catalog, CatalogEntry,
    Citation, ProviderResponse, ResponseStatus, RouterResponse, STUDIOVariant, Protocol, SourceMetadata,
)
from contoso.router.hosted import HostedRouter
from contoso.router.native_a2a import build_native_host
from contoso.router.runtime import RouterRuntime, RuntimeResult, ScopedToken
from contoso.providers import ProviderError, ProviderTimeout, RawReply

HEADERS = {
    "A2A-Version": "1.0", "x-agent-user-id": "allowed-partition",
    "x-agent-foundry-call-id": "real-test-call",
    "Authorization": "Bearer test-secret-never-log",
}
RPC_PATHS = ("/a2a", "/invocations/a2a", "/invocations")


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    original_connect, original_socketpair = socket.socket.connect, socket.socketpair
    internal = threading.local()

    def blocked(*args, **kwargs):
        pytest.fail("Native ASGI tests must not use a network")

    def connect(sock, address):
        if getattr(internal, "socketpair", False):
            return original_connect(sock, address)
        blocked()

    def socketpair(*args, **kwargs):
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
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED", "true")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")


def envelope(question="A registered demo question"):
    return {
        "jsonrpc": "2.0", "id": "request-123", "method": "SendMessage",
        "params": {
            "message": {"messageId": "message-123", "role": "ROLE_USER", "parts": [{"text": question}]},
            "configuration": {"acceptedOutputModes": ["text/plain"], "returnImmediately": False},
        },
    }


@pytest.fixture
def runtime():
    def require_caller(caller):
        if caller not in {"allowed-partition", "second-partition"}:
            raise AccessDenied("Secret unsafe exception text must not escape")

    return SimpleNamespace(
        catalog=SimpleNamespace(require_caller=Mock(side_effect=require_caller)),
        run=AsyncMock(return_value=RuntimeResult(
            "no-matching-capability", "A test-only response without invented citations", None, (),
        )),
    )


@pytest.fixture(params=RPC_PATHS)
def rpc_path(request):
    return request.param


def host(runtime, **kwargs):
    return HostedRouter(runtime, fixed_variant=STUDIOVariant.COPILOT_HARNESS, **kwargs).build_host()


async def post(app, payload=None, *, path="/a2a", headers=None, raw=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://container.invalid",
    ) as client:
        return await client.post(
            path, headers=HEADERS if headers is None else headers,
            **({"content": raw} if raw is not None else {"json": payload if payload is not None else envelope()}),
        )


@pytest.mark.parametrize("path", RPC_PATHS)
def test_native_sendmessage_returns_actual_runtime_json_and_real_context(runtime, path):
    async def run():
        response = await post(host(runtime), path=path)
        assert response.status_code == 200
        value = response.json()
        assert value["jsonrpc"] == "2.0" and value["id"] == "request-123"
        assert "error" not in value
        text = value["result"]["message"]["parts"][0]["text"]
        assert json.loads(text) == runtime.run.return_value.to_dict()
        runtime.run.assert_awaited_once_with(
            "A registered demo question", caller_id="allowed-partition", variant="copilot-harness",
        )
        assert get_request_context().user_id is None
        assert get_request_context().platform_headers() == {}
    asyncio.run(run())


@pytest.mark.parametrize("path", [
    "/a2a/.well-known/agent-card.json", "/invocations/a2a/.well-known/agent-card.json", "/readiness",
])
def test_native_card_aliases_and_core_readiness(runtime, path):
    async def run():
        app = host(runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://container.invalid",
        ) as client:
            response = await client.get(path)
        assert response.status_code == 200
        if "agent-card" in path:
            interface = response.json()["supportedInterfaces"][0]
            assert interface["protocolVersion"] == "1.0"
            assert interface["protocolBinding"] == "JSONRPC"
        assert "/responses" in {route.path for route in app.routes}
        runtime.run.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("headers", [
    {"A2A-Version": "1.0"},
    {key: value for key, value in HEADERS.items() if key != "x-agent-user-id"},
    {key: value for key, value in HEADERS.items() if key != "x-agent-foundry-call-id"},
    {**HEADERS, "x-agent-user-id": "unapproved-partition"},
    {**HEADERS, "x-agent-user-id": ""},
    {**HEADERS, "x-agent-foundry-call-id": "bad call"},
])
def test_missing_or_unapproved_platform_context_rejects_without_provider_work(
    runtime, headers, caplog, rpc_path,
):
    response = asyncio.run(post(host(runtime), headers=headers, path=rpc_path))
    assert "error" in response.json()
    runtime.run.assert_not_awaited()
    assert "test-secret-never-log" not in response.text + caplog.text
    assert "Secret unsafe exception text" not in response.text + caplog.text


def test_native_context_isolated_and_call_id_is_not_manufactured(runtime, rpc_path):
    async def run():
        seen = []

        async def answer(question, *, caller_id, variant):
            before = get_request_context()
            await asyncio.sleep(0)
            after = get_request_context()
            seen.append((caller_id, before.user_id, after.user_id, after.platform_headers()))
            return RuntimeResult("no-matching-capability", question, None, ())

        runtime.run.side_effect = answer
        app = host(runtime)
        responses = await asyncio.gather(
            post(app, path=rpc_path),
            post(app, headers={**HEADERS, "x-agent-user-id": "second-partition",
                               "x-agent-foundry-call-id": "second-real-call"}, path=rpc_path),
        )
        assert all(response.status_code == 200 for response in responses)
        assert sorted(seen) == [
            ("allowed-partition", "allowed-partition", "allowed-partition",
             {"x-agent-foundry-call-id": "real-test-call"}),
            ("second-partition", "second-partition", "second-partition",
             {"x-agent-foundry-call-id": "second-real-call"}),
        ]
        assert get_request_context().user_id is None
    asyncio.run(run())


@pytest.mark.parametrize("method", ["message/send", "GetTask", "CancelTask", "SendStreamingMessage"])
def test_obsolete_streaming_or_stateful_methods_are_not_supported(runtime, method, rpc_path):
    payload = envelope()
    payload["method"] = method
    response = asyncio.run(post(host(runtime), payload, path=rpc_path))
    assert response.json()["error"]["code"] == -32004
    runtime.run.assert_not_awaited()


@pytest.mark.parametrize("change", [
    lambda p: p.update(metadata={"caller_id": "allowed-partition"}),
    lambda p: p["params"].update(metadata={"caller_id": "allowed-partition"}),
    lambda p: p["params"]["message"].update(contextId="untrusted-context"),
    lambda p: p["params"]["message"].update(taskId="another-task"),
    lambda p: p["params"]["message"].update(role="ROLE_AGENT"),
    lambda p: p["params"]["message"].update(parts=[{"text": "one"}, {"text": "two"}]),
    lambda p: p["params"]["message"].update(parts=[{"data": {"caller": "allowed-partition"}}]),
    lambda p: p["params"]["configuration"].update(returnImmediately=True),
    lambda p: p["params"]["configuration"].update(acceptedOutputModes=["application/json"]),
    lambda p: p["params"]["configuration"].update(historyLength=10),
    lambda p: p.update(id=True),
    lambda p: p.update(id="x" * 129),
])
def test_untrusted_routing_identity_and_invalid_params_never_reach_runtime(runtime, change, rpc_path):
    payload = deepcopy(envelope())
    change(payload)
    response = asyncio.run(post(host(runtime), payload, path=rpc_path))
    assert response.json()["error"]["code"] == -32602
    runtime.run.assert_not_awaited()


@pytest.mark.parametrize("question", [
    "", " ", "x" * 2001, "\U0001f600" * 2001,
    '{"question":"A question","studio_variant":"standard"}',
])
def test_question_bounds_and_model_selected_routing_rejected(runtime, question, rpc_path):
    response = asyncio.run(post(host(runtime), envelope(question), path=rpc_path))
    assert "error" in response.json()
    runtime.run.assert_not_awaited()


@pytest.mark.parametrize("raw", [
    b"{", b"\xff", b"[]" , b"x" * 16385,
    b'{"id":"one","id":"two"}',
    json.dumps(envelope("\ud800")).encode(),
    b'{"id":' + b"1" * 5000 + b"}",
])
def test_malformed_duplicate_and_oversize_wire_payloads_fail_safely(runtime, raw, rpc_path):
    response = asyncio.run(post(host(runtime), raw=raw, path=rpc_path))
    assert "error" in response.json()
    runtime.run.assert_not_awaited()


@pytest.mark.parametrize("headers,path,code", [
    ({}, "/a2a", -32009),
    ({**HEADERS, "A2A-Version": "0.3"}, "/a2a", -32009),
    ({**HEADERS, "A2A-Version": "test-secret-never-log"}, "/a2a", -32009),
    (HEADERS, "/a2a?a2a-version=0.3", -32602),
])
def test_version_failures_do_not_echo_untrusted_selectors(runtime, headers, path, code, caplog):
    response = asyncio.run(post(host(runtime), headers=headers, path=path))
    assert response.json()["error"]["code"] == code
    assert "test-secret-never-log" not in response.text + caplog.text
    runtime.run.assert_not_awaited()


def test_case_insensitive_query_version_selects_native_10(runtime, rpc_path):
    headers = {key: value for key, value in HEADERS.items() if key != "A2A-Version"}
    response = asyncio.run(post(host(runtime), headers=headers, path=rpc_path + "?A2A-VERSION=1.0"))
    assert "result" in response.json()


def test_invocations_contract_selects_10_only_when_selector_is_absent(runtime):
    headers = {key: value for key, value in HEADERS.items() if key != "A2A-Version"}
    response = asyncio.run(post(host(runtime), headers=headers, path="/invocations"))
    assert response.status_code == 200
    assert "result" in response.json()
    runtime.run.assert_awaited_once()


@pytest.mark.parametrize("configuration", [
    None, SendMessageConfiguration(), SendMessageConfiguration(return_immediately=False),
    SendMessageConfiguration(accepted_output_modes=["text/plain"], return_immediately=False),
])
@pytest.mark.parametrize("selector", ["header", "query", "route-contract"])
def test_official_protobuf_omitted_defaults_reach_native_identity_probe(configuration, selector):
    request = SendMessageRequest(message=Message(
        message_id=str(uuid4()), role=Role.ROLE_USER, parts=[Part(text="CONTOSO identity probe")],
    ))
    if configuration is not None:
        request.configuration.CopyFrom(configuration)
    payload = envelope()
    payload["params"] = MessageToDict(request)
    headers = {
        key: value for key, value in HEADERS.items()
        if selector == "header" or key != "A2A-Version"
    }
    path = "/invocations?api-version=v1"
    if selector == "query":
        path += "&A2A-Version=1.0"
    response = asyncio.run(post(build_probe_host(), payload, headers=headers, path=path))
    assert response.status_code == 200
    result = response.json()
    assert result["id"] == payload["id"] and "error" not in result
    probe = json.loads(result["result"]["message"]["parts"][0]["text"])
    assert probe == {
        "status": "identity-bootstrap", "platform_caller_id": "allowed-partition",
        "platform_call_id": "real-test-call", "knowledge_access": False,
    }


@pytest.mark.parametrize("missing", [
    ("x-agent-user-id",), ("x-agent-foundry-call-id",),
    ("x-agent-user-id", "x-agent-foundry-call-id"),
])
def test_official_minimal_request_does_not_bypass_required_platform_context(runtime, missing):
    payload = envelope()
    payload["params"] = MessageToDict(SendMessageRequest(message=Message(
        message_id=str(uuid4()), role=Role.ROLE_USER, parts=[Part(text="CONTOSO identity probe")],
    )))
    headers = {key: value for key, value in HEADERS.items() if key not in missing}
    response = asyncio.run(post(host(runtime), payload, headers=headers, path="/invocations?api-version=v1"))
    assert response.json()["error"]["message"] == "The request or platform caller is not authorized"
    runtime.run.assert_not_awaited()


@pytest.mark.parametrize("configuration", [
    None, [], {"returnImmediately": True}, {"returnImmediately": 0},
    {"acceptedOutputModes": ["application/json"]}, {"historyLength": 0},
    {"taskPushNotificationConfig": {}},
])
def test_invalid_or_stateful_configuration_still_rejects_with_safe_reason(runtime, configuration, caplog):
    payload = envelope()
    payload["params"]["configuration"] = configuration
    response = asyncio.run(post(host(runtime), payload, path="/invocations"))
    assert response.json()["error"] == {
        "code": -32602, "message": "Invalid A2A request", "data": {"reason": "configuration"},
    }
    assert "test-secret-never-log" not in response.text + caplog.text
    runtime.run.assert_not_awaited()


@pytest.mark.parametrize("change,reason", [
    (lambda p: p.update(id=True), "envelope"),
    (lambda p: p.update(params={"metadata": {"caller": "do-not-echo"}}), "params_fields"),
    (lambda p: p["params"]["message"].update(role="do-not-echo"), "message"),
    (lambda p: p["params"]["message"].update(parts=[{"text": ""}]), "text"),
])
def test_structural_rejection_diagnostics_are_only_fixed_enums(runtime, change, reason, caplog):
    payload = envelope()
    change(payload)
    response = asyncio.run(post(host(runtime), payload, path="/invocations"))
    assert response.json()["error"]["data"] == {"reason": reason}
    assert "do-not-echo" not in response.text + caplog.text
    runtime.run.assert_not_awaited()


def test_selector_conflict_diagnostic_does_not_echo_header_values(runtime, caplog):
    response = asyncio.run(post(
        host(runtime), headers={**HEADERS, "A2A-Version": "do-not-echo"},
        path="/invocations?a2a-version=1.0",
    ))
    assert response.json()["error"]["data"] == {"reason": "protocol_conflict"}
    assert "do-not-echo" not in response.text + caplog.text
    runtime.run.assert_not_awaited()


@pytest.mark.parametrize("path", ["/a2a", "/invocations/a2a"])
def test_existing_native_routes_still_require_explicit_version(runtime, path):
    headers = {key: value for key, value in HEADERS.items() if key != "A2A-Version"}
    response = asyncio.run(post(host(runtime), headers=headers, path=path))
    assert response.json()["error"]["code"] == -32009
    runtime.run.assert_not_awaited()


@pytest.mark.parametrize("headers,path,code", [
    ({**HEADERS, "A2A-Version": "0.3"}, "/invocations", -32009),
    ({**HEADERS, "A2A-Version": "test-secret-never-log"}, "/invocations", -32009),
    (HEADERS, "/invocations?a2a-version=0.3", -32602),
    ({key: value for key, value in HEADERS.items() if key != "A2A-Version"},
     "/invocations?a2a-version=0.3", -32009),
])
def test_invocations_does_not_override_invalid_or_conflicting_selectors(
    runtime, headers, path, code, caplog,
):
    response = asyncio.run(post(host(runtime), headers=headers, path=path))
    assert response.json()["error"]["code"] == code
    runtime.run.assert_not_awaited()
    assert "test-secret-never-log" not in response.text + caplog.text


def test_invocations_without_selector_does_not_manufacture_platform_context(runtime):
    response = asyncio.run(post(host(runtime), headers={}, path="/invocations"))
    assert response.json()["error"]["code"] == -32602
    runtime.run.assert_not_awaited()
    assert get_request_context().user_id is None


@pytest.mark.parametrize("with_selector", [True, False])
def test_invocations_delivers_identical_bytes_to_actual_sdk_route(runtime, with_selector):
    async def run():
        app = host(runtime)
        route = next(route for route in app.routes if route.path == "/invocations/a2a")
        sdk_app = route.app
        received = bytearray()
        observed = {}
        payload = json.dumps(envelope("Pergunta com açúcar"), ensure_ascii=False, indent=3).encode()

        async def observe(scope, receive, send):
            observed["path"] = scope["path"]
            observed["query_string"] = scope["query_string"]
            observed["user_id"] = get_request_context().user_id
            observed["platform_headers"] = get_request_context().platform_headers()

            async def record():
                message = await receive()
                received.extend(message.get("body", b""))
                return message

            await sdk_app(scope, record, send)

        route.app = observe
        headers = {
            key: value for key, value in HEADERS.items()
            if with_selector or key != "A2A-Version"
        }
        response = await post(
            app, headers={**headers, "Content-Type": "application/json"},
            raw=payload, path="/invocations",
        )
        assert received == payload
        assert observed == {
            "path": "/invocations/a2a", "query_string": b"", "user_id": "allowed-partition",
            "platform_headers": {"x-agent-foundry-call-id": "real-test-call"},
        }
        value = response.json()
        assert response.status_code == 200 and value["id"] == "request-123"
        assert json.loads(value["result"]["message"]["parts"][0]["text"]) == runtime.run.return_value.to_dict()
        runtime.run.assert_awaited_once_with(
            "Pergunta com açúcar", caller_id="allowed-partition", variant="copilot-harness",
        )
        assert get_request_context().user_id is None
    asyncio.run(run())


def test_invocations_probe_uses_actual_sdk_startup_and_shutdown():
    async def run():
        app = build_probe_host()
        async with app.router.lifespan_context(app):
            assert app._task_manager is None
            assert app._owned_task_store is None
            response = await post(app, envelope("CONTOSO identity probe"), path="/invocations")
            value = json.loads(response.json()["result"]["message"]["parts"][0]["text"])
            assert value["status"] == "identity-bootstrap"
            assert value["knowledge_access"] is False
            assert value["platform_caller_id"] == "allowed-partition"
            assert value["platform_call_id"] == "real-test-call"
        assert get_request_context().user_id is None
    asyncio.run(run())


def test_provider_failure_has_only_safe_error_metadata(runtime, caplog, rpc_path):
    runtime.run.side_effect = ProviderTimeout("Bearer test-secret-never-log A registered demo question")
    caplog.set_level(logging.INFO, logger="contoso.router.native_a2a")
    response = asyncio.run(post(host(runtime), path=rpc_path))
    assert "error" in response.json()
    own_logs = "\n".join(record.getMessage() for record in caplog.records
                         if record.name == "contoso.router.native_a2a")
    assert f"protocol=a2a/1.0 path={rpc_path}" in own_logs
    assert "error_class=ProviderTimeout" in own_logs
    assert "test-secret-never-log" not in response.text + caplog.text
    assert "A registered demo question" not in own_logs


def test_native_timeout_cancels_runtime_work(runtime, rpc_path):
    async def run():
        ended = asyncio.Event()

        async def pending(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

        runtime.run.side_effect = pending
        response = await post(host(runtime, overall_timeout=0.01), path=rpc_path)
        assert "error" in response.json()
        assert ended.is_set()
    asyncio.run(run())


def test_slow_request_body_has_a_bounded_read_deadline(monkeypatch, rpc_path):
    import contoso.router.native_a2a as native

    monkeypatch.setattr(native, "_READ_TIMEOUT", 0.01)

    async def run():
        answer = AsyncMock()
        app = build_native_host(answer, name="Test", description="Network-free test")
        send = AsyncMock()

        async def receive():
            await asyncio.Event().wait()

        await app({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "https", "path": rpc_path, "raw_path": rpc_path.encode(),
            "query_string": b"", "root_path": "", "headers": [],
            "client": ("127.0.0.1", 1), "server": ("container.invalid", 443),
        }, receive, send)
        messages = [call.args[0] for call in send.await_args_list]
        body = b"".join(message.get("body", b"") for message in messages)
        assert json.loads(body)["error"]["message"] == "Request timed out"
        answer.assert_not_awaited()
    asyncio.run(run())


def test_oversize_output_is_not_returned_as_completed(runtime, rpc_path):
    runtime.run.return_value = replace(runtime.run.return_value, answer="x" * 1048577)
    response = asyncio.run(post(host(runtime), path=rpc_path))
    assert "error" in response.json()
    assert "result" not in response.json()


def test_native_identity_bootstrap_preserves_knowledge_false_and_verified_context(rpc_path):
    response = asyncio.run(post(build_probe_host(), envelope("CONTOSO identity probe"), path=rpc_path))
    value = json.loads(response.json()["result"]["message"]["parts"][0]["text"])
    assert value == {
        "status": "identity-bootstrap", "platform_caller_id": "allowed-partition",
        "platform_call_id": "real-test-call", "knowledge_access": False,
    }
    rejected = asyncio.run(post(build_probe_host(), envelope("A business question"), path=rpc_path))
    assert "error" in rejected.json()


@pytest.mark.parametrize("attributed", [True, False])
def test_native_harness_runtime_preserves_only_actually_cited_source(attributed, caplog, rpc_path):
    source = SourceMetadata(
        "CONTOSO-DEMO-SUPPLIER-001", "studio-copilot-harness", "1", "Configured demo source",
        "Synthetic demonstration data", "configured-reference", frozenset({"Evidence limits"}),
    )
    entry = CatalogEntry(
        "studio-copilot-harness", Capability.SUPPLIER_DELIVERY_TERMS, STUDIOVariant.COPILOT_HARNESS,
        "https://api.powerplatform.com/copilotstudio-a2a/configured-agent", Protocol.A2A,
        AuthMetadata(AuthScheme.ENTRA_BEARER, "configured-tenant", "configured-audience",
                     frozenset({"configured-scope"})), (source,),
    )
    catalog = Catalog(
        (entry,), (CallerGrant("allowed-partition", frozenset({Capability.SUPPLIER_DELIVERY_TERMS})),),
        frozenset({"https://api.powerplatform.com"}),
    )
    reply = RawReply(
        'Source CONTOSO-DEMO-SUPPLIER-001, Version 1, section "Evidence limits". Test-only evidence.'
        if attributed else "Unattributed test-only answer",
        entry.provider_id, entry.capability, entry.variant, entry.protocol,
        "a2a", "TASK_STATE_COMPLETED", entry.endpoint,
    )
    provider = SimpleNamespace(answer=AsyncMock(return_value=reply))
    broker = AsyncMock(return_value=ScopedToken(
        "test-secret-never-log", "configured-tenant", "configured-audience",
        frozenset({"configured-scope"}), 1000,
    ))
    runtime = RouterRuntime(
        catalog, AsyncMock(return_value="supplier-delivery-terms"),
        {entry.provider_id: provider}, broker, clock=lambda: 100,
    )
    response = asyncio.run(post(host(runtime), path=rpc_path))
    if attributed:
        value = json.loads(response.json()["result"]["message"]["parts"][0]["text"])
        assert value["provider_id"] == "studio-copilot-harness"
        assert value["studio_variant"] == "copilot-harness"
        assert value["answer"] == reply.text
        assert value["citations"] == [{
            "source_id": source.source_id, "version": "1", "section": "Evidence limits",
        }]
    else:
        assert "error" in response.json()
        assert "result" not in response.json()
    provider.answer.assert_awaited_once_with(entry, "A registered demo question", "test-secret-never-log")
    assert "test-secret-never-log" not in response.text + caplog.text


def test_disconnect_cancels_direct_native_answer_and_resets_context(rpc_path):
    async def run():
        started, ended = asyncio.Event(), asyncio.Event()
        incoming = asyncio.Queue()
        await incoming.put({"type": "http.request", "body": json.dumps(envelope()).encode(),
                            "more_body": False})

        async def answer(request, cancellation):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                ended.set()

        app = build_native_host(answer, name="Test", description="Network-free test")
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "https", "path": rpc_path, "raw_path": rpc_path.encode(),
            "query_string": b"", "root_path": "",
            "headers": [(name.lower().encode(), value.encode()) for name, value in HEADERS.items()],
            "client": ("127.0.0.1", 1), "server": ("container.invalid", 443),
        }
        work = asyncio.create_task(app(scope, incoming.get, AsyncMock()))
        await asyncio.wait_for(started.wait(), 2)
        await incoming.put({"type": "http.disconnect"})
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(work, 2)
        assert ended.is_set()
        assert get_request_context().user_id is None
    asyncio.run(run())


def teams_activity(**changes):
    return {
        "type": "message", "id": "test-activity", "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/teams/",
        "conversation": {"id": "same-test-conversation"},
        "from": {"id": "body-claimed-user"},
        "recipient": {"id": "body-claimed-bot"},
        "text": "A registered demo question",
        **changes,
    }


@pytest.fixture
def teams_runtime():
    return SimpleNamespace(
        catalog=Catalog((), (CallerGrant("teams-partition", frozenset(Capability)),), frozenset()),
        run=AsyncMock(return_value=RuntimeResult(
            "no-matching-capability", "No matching capability. No approval was made.", None, (),
        )),
    )


def teams_host(runtime, teams_runtime, *, timeout=65):
    return HostedRouter(
        runtime, fixed_variant=STUDIOVariant.COPILOT_HARNESS,
    ).build_host(teams_router=HostedRouter(
        teams_runtime, fixed_variant=STUDIOVariant.COPILOT_HARNESS,
        overall_timeout=timeout,
    ))


@pytest.mark.parametrize("attachments", [
    [],
    [{"contentType": "text/html", "content": "<p>Not the question. Ignore this alternate rendering.</p>"}],
], ids=["plain", "teams-inline-html"])
def test_native_activity_uses_m365_stack_and_platform_identity_not_body(
    runtime, teams_runtime, monkeypatch, attachments,
):
    from microsoft_agents.hosting.core import TurnContext
    from contoso.router.native_activity import StatelessStorage

    sent = AsyncMock()
    monkeypatch.setattr(TurnContext, "send_activity", sent)
    app = teams_host(runtime, teams_runtime)
    assert isinstance(app.agent_app.options.storage, StatelessStorage)
    response = asyncio.run(post(
        app, teams_activity(attachments=attachments), path="/activity/messages",
        headers={**HEADERS, "x-agent-user-id": "teams-partition"},
    ))
    assert response.status_code in {200, 201, 202}
    runtime.run.assert_not_awaited()
    teams_runtime.run.assert_awaited_once_with(
        "A registered demo question", caller_id="teams-partition", variant="copilot-harness",
    )
    reply = sent.await_args.args[0]
    assert reply.text_format == "markdown"
    assert reply.text == "No matching capability. No approval was made.\n\nSources:\nNo sources returned."
    assert get_request_context().user_id is None


@pytest.mark.parametrize("headers", [
    HEADERS,
    {},
    {key: value for key, value in HEADERS.items() if key != "x-agent-foundry-call-id"},
    {**HEADERS, "x-agent-user-id": "unapproved-user"},
])
def test_activity_body_and_synthesized_outbound_claims_do_not_grant_admission(
    runtime, teams_runtime, monkeypatch, headers,
):
    from microsoft_agents.hosting.core import TurnContext

    sent = AsyncMock()
    monkeypatch.setattr(TurnContext, "send_activity", sent)
    response = asyncio.run(post(
        teams_host(runtime, teams_runtime), teams_activity(**{"from": {"id": "teams-partition"}}),
        path="/activity/messages", headers=headers,
    ))
    assert response.status_code >= 400
    runtime.run.assert_not_awaited()
    teams_runtime.run.assert_not_awaited()
    sent.assert_not_awaited()


@pytest.mark.parametrize("headers,observed", [
    ({**HEADERS, "x-agent-user-id": 'unapproved-"platform"-caller'}, True),
    ({}, False),
    ({key: value for key, value in HEADERS.items() if key != "x-agent-foundry-call-id"}, False),
])
def test_activity_rejected_caller_diagnostic_uses_only_verified_sdk_context(
    runtime, teams_runtime, headers, observed, caplog,
):
    response = asyncio.run(post(
        teams_host(runtime, teams_runtime),
        teams_activity(text="private-question-never-log", **{"from": {"id": "private-body-caller"}}),
        path="/activity/messages", headers=headers,
    ))
    assert response.status_code >= 400
    own_logs = [record.getMessage() for record in caplog.records
                if record.name == "contoso.router.native_activity"]
    diagnostics = [line for line in own_logs if "reason=caller_not_approved" in line]
    assert len(diagnostics) == int(observed)
    if observed:
        assert diagnostics[0].endswith(
            "platform_caller=" + json.dumps(headers["x-agent-user-id"], ensure_ascii=True),
        )
        assert "platform_context_verified=true" in diagnostics[0]
    assert not any(value in "\n".join(own_logs) for value in (
        "private-question-never-log", "private-body-caller", HEADERS["x-agent-foundry-call-id"],
    ))
    runtime.run.assert_not_awaited()
    teams_runtime.run.assert_not_awaited()


@pytest.mark.parametrize("path", (*RPC_PATHS, "/responses"))
def test_teams_allowlist_does_not_authorize_python_protocols(runtime, teams_runtime, path):
    response = asyncio.run(post(
        teams_host(runtime, teams_runtime), {"input": "Question", "store": False}
        if path == "/responses" else envelope(), path=path,
        headers={**HEADERS, "x-agent-user-id": "teams-partition"},
    ))
    assert "error" in response.json()
    runtime.run.assert_not_awaited()
    teams_runtime.run.assert_not_awaited()


@pytest.mark.parametrize("changes", [
    {"channelId": "emulator"},
    {"serviceUrl": "http://smba.trafficmanager.net/teams/"},
    {"serviceUrl": "https://smba.trafficmanager.net.evil.example/teams/"},
    {"serviceUrl": "https://smba.trafficmanager.net:invalid/teams/"},
    {"serviceUrl": "https://user:private@smba.trafficmanager.net/teams/"},
    {"text": '{"question":"Question","studio_variant":"standard"}'},
    {"text": "x" * 2001},
    {"attachments": [{"contentType": "image/png", "contentUrl": "https://untrusted.invalid/image"}]},
    {"attachments": [{"contentType": "text/html", "contentUrl": "https://untrusted.invalid/html"}]},
    {"attachments": [
        {"contentType": "text/html", "content": "<p>A registered demo question</p>"},
        {"contentType": "application/vnd.ms-teams.file.download.info", "content": {}},
    ]},
])
def test_activity_rejects_unsupported_channel_reply_target_or_input_before_provider_work(
    runtime, teams_runtime, monkeypatch, changes,
):
    from microsoft_agents.hosting.core import TurnContext

    sent = AsyncMock()
    monkeypatch.setattr(TurnContext, "send_activity", sent)
    response = asyncio.run(post(
        teams_host(runtime, teams_runtime), teams_activity(**changes), path="/activity/messages",
        headers={**HEADERS, "x-agent-user-id": "teams-partition"},
    ))
    assert response.status_code >= 400
    runtime.run.assert_not_awaited()
    teams_runtime.run.assert_not_awaited()
    sent.assert_not_awaited()


@pytest.mark.parametrize(
    "raw", [b'{"text":"one","text":"two"}', b"[" + b" " * 65536 + b"]"],
    ids=["duplicate-field", "oversized"],
)
def test_activity_bounds_and_duplicate_field_rejection_precede_sdk(
    runtime, teams_runtime, raw,
):
    response = asyncio.run(post(
        teams_host(runtime, teams_runtime), path="/activity/messages", raw=raw,
        headers={**HEADERS, "x-agent-user-id": "teams-partition"},
    ))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_activity"
    teams_runtime.run.assert_not_awaited()


def test_activity_discards_state_and_does_not_replay_conversation_history(
    runtime, teams_runtime, monkeypatch,
):
    from microsoft_agents.hosting.core import TurnContext

    monkeypatch.setattr(TurnContext, "send_activity", AsyncMock())

    async def run():
        app = teams_host(runtime, teams_runtime)
        for question in ("First independent question", "Second independent question"):
            response = await post(
                app, teams_activity(text=question), path="/activity/messages",
                headers={**HEADERS, "x-agent-user-id": "teams-partition"},
            )
            assert response.status_code in {200, 201, 202}
        assert [call.args[0] for call in teams_runtime.run.await_args_list] == [
            "First independent question", "Second independent question",
        ]
    asyncio.run(run())


@pytest.mark.parametrize("question", ["x" * 24001, "\U0001f600" * 6001], ids=["ascii", "utf8"])
def test_activity_reply_limit_counts_bytes_and_never_truncates_attribution(question):
    from contoso.router.native_activity import render_result

    with pytest.raises(ProviderError, match="byte limit"):
        render_result(RuntimeResult("no-matching-capability", question, None, ()))


def test_activity_rejects_query_or_content_encoding_before_sdk(runtime, teams_runtime):
    async def run():
        app = teams_host(runtime, teams_runtime)
        headers = {**HEADERS, "x-agent-user-id": "teams-partition"}
        query = await post(app, teams_activity(), path="/activity/messages?unexpected=value", headers=headers)
        encoded = await post(
            app, teams_activity(), path="/activity/messages",
            headers={**headers, "Content-Encoding": "gzip"},
        )
        assert query.status_code == encoded.status_code == 400
        teams_runtime.run.assert_not_awaited()
    asyncio.run(run())


def test_activity_budget_also_cancels_a_stalled_connector_reply(runtime, teams_runtime, monkeypatch, caplog):
    from microsoft_agents.hosting.core import TurnContext

    async def run():
        cancelled = asyncio.Event()

        async def send(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(TurnContext, "send_activity", send)
        response = await post(
            teams_host(runtime, teams_runtime, timeout=0.1), teams_activity(),
            path="/activity/messages", headers={**HEADERS, "x-agent-user-id": "teams-partition"},
        )
        assert response.status_code == 202
        assert cancelled.is_set()
        assert get_request_context().user_id is None
    asyncio.run(run())
    assert "outcome=failed error_class=TimeoutError" in caplog.text
    assert "outcome=completed" not in caplog.text


def test_activity_concurrent_turns_keep_platform_identity_and_call_ids_separate(
    runtime, teams_runtime, monkeypatch,
):
    from microsoft_agents.hosting.core import TurnContext

    monkeypatch.setattr(TurnContext, "send_activity", AsyncMock())

    async def run():
        teams_runtime.catalog = Catalog((), tuple(
            CallerGrant(caller, frozenset(Capability))
            for caller in ("teams-partition", "second-teams-partition")
        ), frozenset())
        both_started = asyncio.Event()
        entered = []
        result = teams_runtime.run.return_value

        async def answer(question, *, caller_id, variant):
            entered.append((question, caller_id))
            if len(entered) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 2)
            context = get_request_context()
            assert context.user_id == caller_id
            assert context.platform_headers()["x-agent-foundry-call-id"] == question
            return result

        teams_runtime.run.side_effect = answer
        app = teams_host(runtime, teams_runtime)
        replies = await asyncio.gather(*(
            post(
                app, teams_activity(text=call_id), path="/activity/messages",
                headers={**HEADERS, "x-agent-user-id": caller, "x-agent-foundry-call-id": call_id},
            )
            for caller, call_id in (("teams-partition", "first-call"), ("second-teams-partition", "second-call"))
        ))
        assert all(response.status_code in {200, 201, 202} for response in replies)
        assert set(entered) == {("first-call", "teams-partition"), ("second-call", "second-teams-partition")}
        assert get_request_context().user_id is None
    asyncio.run(run())


@pytest.mark.parametrize("word_count", [150, 151])
def test_activity_answer_word_limit_rejects_without_truncating(word_count):
    from contoso.router.native_activity import render_result

    answer = " ".join(["word"] * word_count)
    result = RuntimeResult("no-matching-capability", answer, None, ())
    if word_count > 150:
        with pytest.raises(ProviderError, match="word limit"):
            render_result(result)
    else:
        assert render_result(result) == answer + "\n\nSources:\nNo sources returned."


def test_activity_reply_preserves_actual_attribution_without_runtime_envelope():
    from contoso.router.native_activity import render_result

    source = SourceMetadata(
        "CONTOSO-MOTORS-LOGISTICS-001", "motors-logistics", "1", "Logistics source",
        "Synthetic demonstration data", "approved-reference", frozenset({"Evidence limits"}),
    )
    response = ProviderResponse(
        source.provider_id, Capability.KNOWLEDGE_KNOWLEDGE, None, ResponseStatus.ANSWERED,
        "This is a fictional proposal, not a confirmed pickup.",
        (Citation(source.source_id, source.provider_id, source.version, "Evidence limits"),),
    )
    text = render_result(RuntimeResult(
        "answered", response.answer, RouterResponse(response, (source,)), ("test-only-path",),
    ))
    assert text.startswith(response.answer + "\n")
    assert "**References**\n\nLogistics source (version 1)" in text
    assert "Source ID: `CONTOSO-MOTORS-LOGISTICS-001`" in text
    assert text.count(source.source_id) == 1
    assert "Sections cited: Evidence limits" in text
    assert "test-only-path" not in text
    assert '"execution_path"' not in text


def test_activity_markdown_separates_source_headings_without_shortening_citations():
    from contoso.router.native_activity import render_result

    source = SourceMetadata(
        "CONTOSO-MOTORS-SUPPLIER-001", "supplier", "1", "Supplier proposal",
        "Synthetic demonstration data", "approved-reference",
        frozenset({"Fictional proposal", "Evidence limits"}),
    )
    citation = 'Source: CONTOSO-MOTORS-SUPPLIER-001, Version 1, Section: "Evidence limits".'
    answer = (
        "**Fictional proposal** 300 Friday, 200 Monday. It is **not approved**. "
        '**Evidence limits** No inventory was verified. ' + citation
    )
    response = ProviderResponse(
        source.provider_id, Capability.KNOWLEDGE_KNOWLEDGE, None, ResponseStatus.ANSWERED,
        answer, (Citation(source.source_id, source.provider_id, source.version, "Evidence limits"),),
    )
    text = render_result(RuntimeResult(
        "answered", answer, RouterResponse(response, (source,)), (),
    ))
    expected = (
        "**Fictional proposal**\n\n300 Friday, 200 Monday. It is **not approved**."
        "\n\n**Evidence limits**\n\nNo inventory was verified. " + citation
    )
    assert text.startswith(expected + "\n\n**References**\n")
    assert text.split("\n\n**References**", 1)[0].split() == answer.split()
    assert text.endswith("Sections cited: Evidence limits")


def test_activity_groups_cited_sections_under_the_exact_source_version():
    from contoso.router.native_activity import render_result

    sources = (
        SourceMetadata(
            "SUPPLIER-001", "supplier", "1", "Earlier proposal",
            "Synthetic demonstration data", "approved-reference",
            frozenset({"Fictional proposal", "Evidence limits"}),
        ),
        SourceMetadata(
            "SUPPLIER-001", "supplier", "2", "Revised proposal",
            "Synthetic demonstration data", "approved-reference",
            frozenset({"Buyer acceptance conditions", "Recommended next step"}),
        ),
    )
    citations = (
        Citation("SUPPLIER-001", "supplier", "2", "Buyer acceptance conditions"),
        Citation("SUPPLIER-001", "supplier", "1", "Fictional proposal"),
        Citation("SUPPLIER-001", "supplier", "2", "Recommended next step"),
        Citation("SUPPLIER-001", "supplier", "1", "Evidence limits"),
    )
    response = ProviderResponse(
        "supplier", Capability.SUPPLIER_DELIVERY_TERMS, STUDIOVariant.COPILOT_HARNESS,
        ResponseStatus.ANSWERED, "No purchase was approved.", citations,
    )
    text = render_result(RuntimeResult(
        "answered", response.answer, RouterResponse(response, sources), (),
    ))
    assert text == (
        response.answer + "\n\n**References**\n\nEarlier proposal (version 1)"
        "\nSource ID: `SUPPLIER-001`\n\nSections cited: Fictional proposal, Evidence limits"
        "\n\nRevised proposal (version 2)\nSource ID: `SUPPLIER-001`"
        "\n\nSections cited: Buyer acceptance conditions, Recommended next step"
    )


@pytest.mark.parametrize("source_id,provider_id,version", [
    ("unknown", "supplier", "1"),
    ("SUPPLIER-001", "unknown", "1"),
    ("SUPPLIER-001", "supplier", "2"),
])
def test_activity_never_omits_a_citation_without_a_matching_source(source_id, provider_id, version):
    from contoso.router.native_activity import render_result

    source = SourceMetadata(
        "SUPPLIER-001", "supplier", "1", "Supplier proposal",
        "Synthetic demonstration data", "approved-reference", frozenset({"Evidence limits"}),
    )
    response = ProviderResponse(
        "supplier", Capability.SUPPLIER_DELIVERY_TERMS, STUDIOVariant.COPILOT_HARNESS,
        ResponseStatus.ANSWERED, "No inventory was verified.",
        (Citation(source_id, provider_id, version, "Evidence limits"),),
    )
    with pytest.raises(ProviderError, match="no matching source"):
        render_result(RuntimeResult(
            "answered", response.answer, RouterResponse(response, (source,)), (),
        ))


async def start_teams_exchange(app):
    incoming = asyncio.Queue()
    await incoming.put({
        "type": "http.request", "body": json.dumps(teams_activity()).encode(), "more_body": False,
    })
    path = "/activity/messages"
    headers = {**HEADERS, "Content-Type": "application/json", "x-agent-user-id": "teams-partition"}
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "https", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        "client": ("127.0.0.1", 1), "server": ("container.invalid", 443),
    }
    events = []
    acknowledged = asyncio.Event()

    async def send(event):
        events.append(event)
        if event["type"] == "http.response.body" and not event.get("more_body", False):
            acknowledged.set()

    return SimpleNamespace(
        task=asyncio.create_task(app(scope, incoming.get, send)),
        incoming=incoming, events=events, acknowledged=acknowledged,
    )


def test_activity_ack_precedes_slow_provider_and_post_ack_disconnect_keeps_reply(
    runtime, teams_runtime, monkeypatch,
):
    from microsoft_agents.hosting.core import TurnContext

    sent = AsyncMock()
    monkeypatch.setattr(TurnContext, "send_activity", sent)

    async def run():
        release, started = asyncio.Event(), asyncio.Event()
        result = teams_runtime.run.return_value

        async def pending(*args, **kwargs):
            started.set()
            await release.wait()
            assert get_request_context().user_id == "teams-partition"
            return result

        teams_runtime.run.side_effect = pending
        exchange = await start_teams_exchange(teams_host(runtime, teams_runtime))
        try:
            await asyncio.wait_for(exchange.acknowledged.wait(), 2)
            await asyncio.wait_for(started.wait(), 2)
            assert not exchange.task.done()
            assert exchange.events[0]["status"] == 202
            assert b"x-platform-server" in dict(exchange.events[0]["headers"])
            await exchange.incoming.put({"type": "http.disconnect"})
            await asyncio.sleep(0)
            assert not exchange.task.done()
            sent.assert_not_awaited()
            release.set()
            await asyncio.wait_for(exchange.task, 2)
            assert sent.await_args.args[0].text.startswith(result.answer)
            assert len([event for event in exchange.events if event["type"] == "http.response.start"]) == 1
            assert get_request_context().user_id is None
        finally:
            exchange.task.cancel()
            await asyncio.gather(exchange.task, return_exceptions=True)
    asyncio.run(run())


def test_activity_disconnect_before_ack_does_not_execute_deferred_provider(runtime, teams_runtime):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        app = teams_host(runtime, teams_runtime)

        @app.agent_app.before_turn
        async def hold_admission(context, state):
            entered.set()
            await release.wait()
            return True

        exchange = await start_teams_exchange(app)
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await exchange.incoming.put({"type": "http.disconnect"})
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(exchange.task, 2)
            teams_runtime.run.assert_not_awaited()
            assert get_request_context().user_id is None
        finally:
            exchange.task.cancel()
            await asyncio.gather(exchange.task, return_exceptions=True)
    asyncio.run(run())


def test_activity_host_cancellation_cancels_accepted_work_and_releases_capacity(
    runtime, teams_runtime, monkeypatch,
):
    from microsoft_agents.hosting.core import TurnContext

    monkeypatch.setattr(TurnContext, "send_activity", AsyncMock())

    async def run():
        started, ended = asyncio.Event(), asyncio.Event()
        result = teams_runtime.run.return_value

        async def pending(*args, **kwargs):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                ended.set()

        teams_runtime.run.side_effect = pending
        app = teams_host(runtime, teams_runtime)
        exchange = await start_teams_exchange(app)
        try:
            await asyncio.wait_for(started.wait(), 2)
            exchange.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await exchange.task
            assert ended.is_set()
            teams_runtime.run.side_effect = None
            teams_runtime.run.return_value = result
            response = await post(
                app, teams_activity(), path="/activity/messages",
                headers={**HEADERS, "x-agent-user-id": "teams-partition"},
            )
            assert response.status_code == 202
            assert get_request_context().user_id is None
        finally:
            exchange.task.cancel()
            await asyncio.gather(exchange.task, return_exceptions=True)
    asyncio.run(run())


def test_activity_rejects_excess_turn_before_ack_and_releases_all_capacity(runtime, teams_runtime, monkeypatch):
    from microsoft_agents.hosting.core import TurnContext

    monkeypatch.setattr(TurnContext, "send_activity", AsyncMock())

    async def run():
        entered, release, all_started = [], asyncio.Event(), asyncio.Event()
        result = teams_runtime.run.return_value

        async def pending(*args, **kwargs):
            entered.append(args)
            if len(entered) == 4:
                all_started.set()
            await release.wait()
            return result

        teams_runtime.run.side_effect = pending
        app = teams_host(runtime, teams_runtime)
        exchanges = [await start_teams_exchange(app) for _ in range(4)]
        try:
            await asyncio.wait_for(asyncio.gather(*(item.acknowledged.wait() for item in exchanges)), 2)
            await asyncio.wait_for(all_started.wait(), 2)
            rejected = await post(
                app, teams_activity(), path="/activity/messages",
                headers={**HEADERS, "x-agent-user-id": "teams-partition"},
            )
            assert rejected.status_code >= 400
            assert len(entered) == 4
            release.set()
            await asyncio.wait_for(asyncio.gather(*(item.task for item in exchanges)), 2)
            next_turn = await post(
                app, teams_activity(), path="/activity/messages",
                headers={**HEADERS, "x-agent-user-id": "teams-partition"},
            )
            assert next_turn.status_code == 202
            assert len(entered) == 5
        finally:
            for item in exchanges:
                item.task.cancel()
            await asyncio.gather(*(item.task for item in exchanges), return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["provider-error", "provider-timeout", "answer-word-limit"])
def test_activity_failure_after_ack_is_explicit_and_never_reported_as_completed(
    runtime, teams_runtime, monkeypatch, caplog, failure,
):
    from microsoft_agents.hosting.core import TurnContext

    sent = AsyncMock()
    monkeypatch.setattr(TurnContext, "send_activity", sent)

    async def run():
        ended = asyncio.Event()

        async def fail(*args, **kwargs):
            if failure == "provider-error":
                raise ProviderError("private-provider-error-never-show")
            if failure == "answer-word-limit":
                return RuntimeResult("no-matching-capability", "word " * 151, None, ())
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

        teams_runtime.run.side_effect = fail
        response = await post(
            teams_host(runtime, teams_runtime, timeout=1), teams_activity(),
            path="/activity/messages", headers={**HEADERS, "x-agent-user-id": "teams-partition"},
        )
        assert response.status_code == 202
        assert sent.await_args.args[0].text == (
            "Unable to answer this request. No business approval or booking was made."
        )
        assert "private-provider-error" not in sent.await_args.args[0].text
        assert get_request_context().user_id is None
        if failure == "provider-timeout":
            assert ended.is_set()
    asyncio.run(run())
    assert "outcome=failed" in caplog.text
    assert "outcome=completed" not in caplog.text
