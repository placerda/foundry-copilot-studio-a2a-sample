import json
from types import SimpleNamespace

import jsonschema
import pytest
import yaml
from pathlib import Path

from config import Settings
from errors import ChatFailure, unauthenticated


def event(**changes):
    return {"requestContext": {"http": {"method": "POST"}, "requestId": "request-1"},
            "rawPath": "/", "headers": {"content-type": "application/json", "authorization": "Bearer synthetic"},
            "body": '{"messages":[{"role":"user","content":"PRIVATE-PROMPT"}]}', **changes}


class FakeAuth:
    def __init__(self, failure=None):
        self.calls = 0
        self.failure = failure
    def verify(self, token):
        self.calls += 1
        if self.failure:
            raise self.failure


class FakeGraph:
    def __init__(self, failure=None, limited=False):
        self.calls = []
        self.failure = failure
        self.limited = limited
    def invoke(self, state):
        self.calls.append(state)
        if self.failure:
            raise self.failure
        return {"answer": "Resposta", "output_limited": self.limited}


def run(event_value, environment, auth=None, graph=None):
    from app import handle
    return handle(event_value, SimpleNamespace(aws_request_id="request-1", get_remaining_time_in_millis=lambda: 25000),
                  Settings.from_env(environment), auth or FakeAuth(), graph or FakeGraph())


@pytest.mark.parametrize("limited", [False, True])
def test_success_contract(limited, environment):
    graph = FakeGraph(limited=limited)
    result = run(event(), environment, graph=graph)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    contract = yaml.safe_load((Path(__file__).parents[2] / "contracts" /
                               "chat-api.openapi.yaml").read_text(encoding="utf-8"))
    jsonschema.validate(body, contract["components"]["schemas"]["ChatResponse"])
    assert body["output_limited"] is limited
    assert len(graph.calls) == 1
    assert result["headers"]["Cache-Control"] == "no-store"
    assert not any("access-control" in key.lower() for key in result["headers"])


@pytest.mark.parametrize("value,status", [
    (event(requestContext={"http": {"method": "GET"}}), 405),
    (event(rawPath="/else"), 400), (event(body="not json"), 400),
    (event(body='{"messages":[]}'), 400),
])
def test_reject_before_graph(value, status, environment):
    graph = FakeGraph()
    result = run(value, environment, graph=graph)
    assert result["statusCode"] == status
    assert not graph.calls
    if status == 405:
        assert result["headers"]["Allow"] == "POST, OPTIONS"


@pytest.mark.parametrize("failure", [unauthenticated(), ChatFailure(403, "forbidden", "Acesso negado.")])
def test_auth_precedence(failure, environment):
    graph = FakeGraph()
    result = run(event(body='{"messages":[]}'), environment, FakeAuth(failure), graph)
    assert result["statusCode"] == failure.status
    assert not graph.calls


@pytest.mark.parametrize("failure,status", [
    (ChatFailure(429, "throttled", "Aguarde."), 429),
    (ChatFailure(502, "dependency_unavailable", "Indisponível."), 502),
    (RuntimeError("SECRET-SENTINEL"), 500),
])
def test_failures_and_sanitized_logs(failure, status, environment, caplog):
    graph = FakeGraph(failure)
    result = run(event(), environment, graph=graph)
    assert result["statusCode"] == status
    assert len(graph.calls) == 1
    assert "SECRET-SENTINEL" not in result["body"] + caplog.text
    assert "PRIVATE-PROMPT" not in caplog.text
    assert "Bearer synthetic" not in caplog.text
    assert "Traceback" not in caplog.text


def test_options(environment):
    graph = FakeGraph()
    result = run(event(requestContext={"http": {"method": "OPTIONS"}}), environment, graph=graph)
    assert result["statusCode"] == 200
    assert not graph.calls


@pytest.mark.parametrize("value,auth,graph,message", [
    (event(requestContext={"http": {"method": "GET"}}), None, None, "Use POST to send messages."),
    (event(rawPath="/else"), None, None, "Unsupported path."),
    (event(body="not json"), None, None, "Invalid message or format."),
    (event(), FakeAuth(unauthenticated()), None, "Invalid or expired session."),
    (event(), None, FakeGraph(RuntimeError("SECRET")), "Internal error. Try again manually."),
])
def test_english_error_messages(value, auth, graph, message, environment):
    result = run(value, environment, auth, graph)
    assert json.loads(result["body"])["error"]["message"] == message


def test_real_graph_insufficient_budget(environment):
    from app import handle
    from bedrock_client import Model
    from graph import build_graph
    from contoso_backend.tests.test_graph import FakeConverse
    settings = Settings.from_env(environment)
    client = FakeConverse()
    result = handle(event(), SimpleNamespace(aws_request_id="budget",
                    get_remaining_time_in_millis=lambda: 21000),
                    settings, FakeAuth(), build_graph(Model(settings, client)))
    assert result["statusCode"] == 502
    assert not client.calls


def test_handler_disables_inherited_tracing(environment, monkeypatch):
    from langsmith import get_tracing_context
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")

    class UntracedGraph(FakeGraph):
        def invoke(self, state):
            assert get_tracing_context()["enabled"] is False
            return super().invoke(state)

    assert run(event(), environment, graph=UntracedGraph())["statusCode"] == 200


def test_lambda_entrypoint_reuses_services_without_retaining_conversation(environment, monkeypatch, context):
    import app
    import bedrock_client
    import graph
    builds = []
    fake = FakeGraph()
    monkeypatch.setattr(app, "_services", None)
    monkeypatch.setattr(bedrock_client, "Model", lambda settings: builds.append(settings))
    monkeypatch.setattr(graph, "build_graph", lambda model: fake)
    monkeypatch.setattr("auth.Authorizer", lambda settings, cache: FakeAuth())
    assert app.lambda_handler(event(), context)["statusCode"] == 200
    assert app.lambda_handler(event(body='{"messages":[{"role":"user","content":"second"}]}'),
                              context)["statusCode"] == 200
    assert len(builds) == 1
    assert [state["messages"] for state in fake.calls] == [
        [{"role": "user", "content": "PRIVATE-PROMPT"}],
        [{"role": "user", "content": "second"}],
    ]


def test_lambda_configuration_failure_is_explicit_and_sanitized(environment, monkeypatch, context, caplog):
    import app
    monkeypatch.setattr(app, "_services", None)
    monkeypatch.delenv("TENANT_ID")
    result = app.lambda_handler(event(), context)
    assert result["statusCode"] == 500
    assert "configuration_error" in caplog.text
    assert "PRIVATE-PROMPT" not in caplog.text + result["body"]
    assert "Traceback" not in caplog.text
