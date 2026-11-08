import copy
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from a2a_requester import A2ARequester, NATIVE_OPTIONS, parse_a2a_result
from app import handle
from config import Settings
from errors import ChatFailure
from contoso_backend.tests.test_api import FakeAuth, FakeGraph, event
from contoso_backend.tests.test_contoso_requester import (FakeResponse, FakeSession, runtime_result, token_result,
                                TENANT, API, CLIENT, SECRET, SERVICE_TOKEN)


@pytest.fixture
def native_env(environment):
    return {**environment, "CONTOSO_ENABLED": "true",
            "CONTOSO_GATEWAY_URL": "https://gateway.synthetic.azurewebsites.net/api/a2a",
            "CONTOSO_ENTRA_TENANT_ID": TENANT, "CONTOSO_GATEWAY_API_CLIENT_ID": API,
            "CONTOSO_REQUESTER_CLIENT_ID": CLIENT, "CONTOSO_REQUESTER_CLIENT_SECRET": SECRET}


def envelope(request_id="request-id", *, task=False, runtime=None):
    body = json.dumps(runtime if runtime is not None else runtime_result("copilot-harness"))
    result = {"task": {"status": {"state": "TASK_STATE_COMPLETED"},
                       "artifacts": [{"parts": [{"text": body}]}]}} if task else {
                           "message": {"role": "ROLE_AGENT", "parts": [{"text": body}]}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class A2ASession(FakeSession):
    def __init__(self, *, task=False, failure=None):
        super().__init__([FakeResponse(token_result())])
        self.task = task
        self.failure = failure

    def post(self, url, **kwargs):
        if self.responses:
            return super().post(url, **kwargs)
        assert self.trust_env is False
        self.calls.append((url, kwargs))
        if self.failure is not None:
            return FakeResponse(status=self.failure, raw=b"PRIVATE-UPSTREAM-ERROR")
        return FakeResponse(envelope(kwargs["json"]["id"], task=self.task))


@pytest.mark.parametrize("task", [False, True])
def test_native_a2a_wire_contract_has_no_command_routing_or_identity(native_env, task, caplog):
    settings = Settings.from_env(native_env)
    session = A2ASession(task=task)
    worker = A2ARequester(settings.contoso, lambda: session)
    graph = FakeGraph()
    auth = FakeAuth()
    question = "What consumer insight can help?"
    payload = {"messages": [{"role": "user", "content": "previous"},
                            {"role": "assistant", "content": "previous answer"},
                            {"role": "user", "content": question}], "contoso_options": NATIVE_OPTIONS}
    response = handle(event(body=json.dumps(payload)),
                      SimpleNamespace(get_remaining_time_in_millis=lambda: 90000),
                      settings, auth, graph, worker)
    assert response["statusCode"] == 200 and auth.calls == 1 and not graph.calls
    result = json.loads(response["body"])
    assert result["answer"] == runtime_result()["answer"]
    assert result["contoso"]["studio_variant"] == "copilot-harness"
    assert result["output_limited"] is False
    assert len(session.calls) == 2
    token_url, token_args = session.calls[0]
    assert token_url == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    assert token_args["data"]["scope"] == f"api://{API}/.default"
    url, args = session.calls[1]
    assert url == "https://gateway.synthetic.azurewebsites.net/api/a2a"
    assert args["headers"]["A2A-Version"] == "1.0"
    assert args["headers"]["Authorization"] == f"Bearer {SERVICE_TOKEN}"
    assert args["allow_redirects"] is False
    assert args["stream"] is True and args["timeout"][1] <= 75
    wire = args["json"]
    assert set(wire) == {"jsonrpc", "id", "method", "params"}
    assert wire["jsonrpc"] == "2.0" and wire["method"] == "SendMessage"
    UUID(wire["id"])
    message = wire["params"]["message"]
    UUID(message["messageId"])
    assert message == {"messageId": message["messageId"], "role": "ROLE_USER",
                       "parts": [{"text": question}]}
    assert wire["params"]["configuration"] == {
        "acceptedOutputModes": ["text/plain"], "returnImmediately": False}
    assert set(wire["params"]) == {"message", "configuration"}
    for private in (SECRET, SERVICE_TOKEN, TENANT, API, CLIENT):
        assert private not in response["body"] + caplog.text
    assert "previous" not in json.dumps(wire)
    assert "integration" not in json.dumps(wire) and "studio_variant" not in json.dumps(wire)


@pytest.mark.parametrize("suffix", ["", "/", "/api/a2a"])
def test_native_endpoint_is_exact_configured_host_and_a2a_path(native_env, suffix):
    env = {**native_env, "CONTOSO_GATEWAY_URL": "https://gateway.synthetic.azurewebsites.net" + suffix}
    worker = A2ARequester(Settings.from_env(env).contoso, lambda: A2ASession())
    assert worker.settings.gateway_url == "https://gateway.synthetic.azurewebsites.net/api/a2a"


def test_json_command_endpoint_is_not_a_native_fallback(native_env):
    settings = Settings.from_env({**native_env,
        "CONTOSO_GATEWAY_URL": "https://gateway.synthetic.azurewebsites.net/api/invoke"})
    assert settings.contoso.a2a_gateway_url is None
    with pytest.raises(ValueError, match="native A2A"):
        A2ARequester(settings.contoso)


@pytest.mark.parametrize("options", [
    {"integration": "sdk", "studio_variant": "copilot-harness"},
    {"integration": "a2a", "studio_variant": "standard"},
    {"integration": "sdk", "studio_variant": "standard"},
    {**NATIVE_OPTIONS, "caller_id": "untrusted"},
])
def test_deferred_variants_cannot_be_invoked(native_env, options):
    session = A2ASession()
    worker = A2ARequester(Settings.from_env(native_env).contoso, lambda: session)
    with pytest.raises(ChatFailure) as error:
        worker.invoke("question", options, 90000)
    assert error.value.status == 400 and not session.calls


@pytest.mark.parametrize("changes", [
    {"id": "foreign"}, {"jsonrpc": "1.0"}, {"result": {}},
    {"error": {"code": -32000, "message": "PRIVATE-ERROR"}},
    {"answer": "success-shaped plaintext"},
    {"result": {"message": {"role": "ROLE_USER", "parts": [{"text": "{}"}]}}},
    {"result": {"message": {"parts": [{"text": "ordinary ungrounded text"}]}}},
    {"result": {"message": {"parts": [{"data": {"answer": "fabricated"}}]}}},
    {"result": {"message": {"parts": []}}},
])
def test_native_envelope_errors_never_become_success(changes):
    value = {**envelope(), **changes}
    with pytest.raises(ChatFailure) as error:
        parse_a2a_result(value, "request-id")
    assert error.value.status == 502 and "PRIVATE-ERROR" not in error.value.message


@pytest.mark.parametrize("state", [
    "TASK_STATE_WORKING", "TASK_STATE_SUBMITTED", "TASK_STATE_FAILED",
    "TASK_STATE_INPUT_REQUIRED", "completed", None,
])
def test_incomplete_or_wrong_dialect_tasks_are_not_polled_or_retried(state):
    value = envelope(task=True)
    value["result"]["task"]["status"]["state"] = state
    with pytest.raises(ChatFailure):
        parse_a2a_result(value, "request-id")


@pytest.mark.parametrize("status", [301, 302, 401, 403, 409, 429, 500, 502, 503])
def test_native_http_failures_never_call_json_command_or_responses(native_env, status):
    session = A2ASession(failure=status)
    worker = A2ARequester(Settings.from_env(native_env).contoso, lambda: session)
    with pytest.raises(ChatFailure) as error:
        worker.invoke("q", NATIVE_OPTIONS, 90000)
    assert error.value.status == (429 if status == 429 else 502)
    assert len(session.calls) == 2
    assert all("/api/invoke" not in url and "/responses" not in url for url, _ in session.calls)
    assert worker._token is None


@pytest.mark.parametrize("changes", [
    {"studio_variant": "standard"}, {"studio_variant": None}, {"provider_id": None},
    {"sources": []}, {"citations": []}, {"execution_path": []},
])
def test_native_response_requires_actual_harness_attribution(changes):
    value = {**runtime_result("copilot-harness"), **changes}
    with pytest.raises(ChatFailure):
        parse_a2a_result(envelope(runtime=value), "request-id")


def test_native_runtime_duplicate_json_fields_and_oversized_parts_rejected():
    value = envelope()
    value["result"]["message"]["parts"] = [{"text": '{"status":"answered","status":"answered"}'}]
    with pytest.raises(ChatFailure):
        parse_a2a_result(value, "request-id")
    value = envelope()
    value["result"]["message"]["parts"] = [{"text": "x" * 262145}]
    with pytest.raises(ChatFailure):
        parse_a2a_result(value, "request-id")


def test_lambda_entrypoint_uses_native_adapter_only(native_env, monkeypatch):
    import app
    import a2a_requester
    for name, value in native_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(app, "_services", None)
    monkeypatch.setattr("auth.Authorizer", lambda *_: FakeAuth())
    monkeypatch.setattr("bedrock_client.Model", lambda *_: None)
    monkeypatch.setattr("graph.build_graph", lambda *_: FakeGraph())
    calls = []

    class Native:
        def __init__(self, settings):
            assert settings.a2a_gateway_url.endswith("/api/a2a")
        def invoke(self, question, options, remaining_ms):
            calls.append((question, options, remaining_ms))
            return parse_a2a_result(envelope(), "request-id")

    monkeypatch.setattr(a2a_requester, "A2ARequester", Native)
    payload = {"messages": [{"role": "user", "content": "question"}], "contoso_options": NATIVE_OPTIONS}
    result = app.lambda_handler(event(body=json.dumps(payload)),
        SimpleNamespace(get_remaining_time_in_millis=lambda: 90000))
    assert result["statusCode"] == 200
    assert calls == [("question", NATIVE_OPTIONS, 90000)]
