import json
import inspect
from types import SimpleNamespace

import azure.functions as func
import pytest

from contoso_backend.errors import unauthenticated
from contoso.gateway_bootstrap import create_app, create_publication_app


ENV = {
    "CONTOSO_ENTRA_TENANT_ID": "11111111-1111-4111-8111-111111111111",
    "CONTOSO_ENTRA_API_CLIENT_ID": "22222222-2222-4222-8222-222222222222",
    "CONTOSO_AUTHORING_CLIENT_ID": "04b07795-8ddb-461a-bbee-02f9e1bf7b46",
    "CONTOSO_AUTHORING_ADMIN_OID": "44444444-4444-4444-8444-444444444444",
}


@pytest.fixture
def anyio_contoso_backend():
    return "asyncio"


class Auth:
    def __init__(self, kind="user", rejected=False):
        self.kind = kind
        self.rejected = rejected
        self.policy = None

    def verify(self, header, **policy):
        self.policy = policy
        if self.rejected:
            raise unauthenticated()
        return SimpleNamespace(kind=self.kind)


@pytest.mark.anyio
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("transport", ["native", "http-passthrough"])
async def test_fixed_native_a2a_probe_preserves_operator_guard_and_rejects_all_inputs(monkeypatch, failed, transport):
    from contoso import gateway_a2a_probe, gateway_bootstrap
    calls = []
    endpoint = "https://contoso.services.ai.azure.com/api/projects/p/agents/contoso-foundry-agent/endpoint/protocols/a2a?api-version=v1"
    monkeypatch.setitem(ENV, "CONTOSO_ROUTER_A2A_ENDPOINT", endpoint)
    monkeypatch.setitem(ENV, "CONTOSO_ROUTER_A2A_TRANSPORT", transport)
    monkeypatch.setattr(gateway_bootstrap, "operator_config", lambda env: "trusted-project-config")
    receipt = {"status": "failed" if failed else "identity-bootstrap", "integration": "a2a"}

    async def probe(config, *, endpoint, hosting_transport="native"):
        calls.append((config, endpoint, hosting_transport))
        return receipt

    monkeypatch.setattr(gateway_a2a_probe, "probe_native_a2a", probe)
    for auth in (Auth(rejected=True), Auth(kind="workload")):
        response, _, _, _ = await request("a2a-probe", auth=auth)
        assert response.status_code in (401, 403) and not calls
    for payload in [
        {"service": "contoso-foundry-sdk-agent"}, {"url": "https://attacker.example"},
        {"prompt": "not-allowed"}, {"caller_id": "forged"}, {"padding": "x" * 4097},
    ]:
        response, _, _, _ = await request("a2a-probe", payload)
        assert response.status_code == 400 and not calls
    response, auth, executor, factories = await request("a2a-probe", {})
    assert response.status_code == (503 if failed else 200)
    assert json.loads(response.get_body()) == receipt
    assert calls == [("trusted-project-config", endpoint, transport)]
    assert not factories and not executor.calls
    assert auth.policy["delegated_scope"] == "CONTOSO.Deploy"
    assert auth.policy["application_role"] == "CONTOSO.DeploymentAdmin"


@pytest.mark.anyio
async def test_session_snapshot_is_operator_only_fixed_input_and_does_not_invoke_native_probe(monkeypatch):
    from contoso import gateway_a2a_probe, gateway_session_probe, gateway_bootstrap
    calls = []
    endpoint = "https://contoso.services.ai.azure.com/api/projects/p/agents/contoso-foundry-agent/endpoint/protocols/a2a?api-version=v1"
    monkeypatch.setitem(ENV, "CONTOSO_ROUTER_A2A_ENDPOINT", endpoint)
    monkeypatch.setattr(gateway_bootstrap, "operator_config", lambda env: "trusted-project")
    monkeypatch.setattr(gateway_a2a_probe, "probe_native_a2a", lambda *a, **k: pytest.fail("No native invocation"))
    async def snapshot(config, *, endpoint):
        calls.append((config, endpoint))
        return {"status": "session-snapshot", "native_a2a_invoked": False}
    monkeypatch.setattr(gateway_session_probe, "snapshot_native_sessions", snapshot)
    for auth in (Auth(rejected=True), Auth(kind="workload")):
        response, _, _, _ = await request("a2a-session-snapshot", auth=auth)
        assert response.status_code in (401, 403) and not calls
    response, _, _, _ = await request("a2a-session-snapshot", {"session_id": "untrusted"})
    assert response.status_code == 400 and not calls
    response, _, _, _ = await request("a2a-session-snapshot", {})
    assert response.status_code == 200 and calls == [("trusted-project", endpoint)]


@pytest.mark.anyio
async def test_callback_metadata_requires_selected_user_and_only_returns_safe_record():
    from contoso.gateway.callback_diagnostics import record_callback
    correlation = "e" * 32
    metadata = record_callback(
        correlation, "POST", {"Content-Type": "application/x-www-form-urlencoded"},
        b"state=private-state&code=private-code&client_info=private-info", "form_fields", False,
    )
    response, _, _, factories = await request(
        "oauth-callback-diagnostics", {"correlation_id": correlation},
    )
    assert response.status_code == 200 and json.loads(response.get_body()) == metadata
    assert not factories and b"private" not in response.get_body()
    for auth in (Auth(rejected=True), Auth(kind="workload")):
        response, _, _, _ = await request(
            "oauth-callback-diagnostics", {"correlation_id": correlation}, auth=auth,
        )
        assert response.status_code in (401, 403)
    response, _, _, _ = await request(
        "oauth-callback-diagnostics", {"correlation_id": "not-a-correlation"},
    )
    assert response.status_code == 400
    response, _, _, _ = await request(
        "oauth-callback-diagnostics", {"correlation_id": "0" * 32},
    )
    assert response.status_code == 404


class Executor:
    def __init__(self):
        self.calls = []

    def preflight(self):
        self.calls.append("preflight")

    def publish_service(self, service, *, process_user_agent):
        self.calls.append((service, process_user_agent))
        return SimpleNamespace(as_dict=lambda: {"status": "succeeded"})

    def publish_sources(self):
        self.calls.append("sources")
        return {"document": {"etag": "actual"}}

    def get_agent_records(self, *, include_endpoint=True):
        self.calls.append("records" if include_endpoint else "metadata")
        return {"agents": []}

    def diagnostic_self_test(self, *, process_user_agent):
        self.calls.append(("diagnostics", process_user_agent))
        return {"status": "succeeded", "checks": {"executable": True}}


async def request(operation, payload=None, *, auth=None, failed=False):
    executor = Executor()
    if failed:
        def publish(service, *, process_user_agent):
            return SimpleNamespace(as_dict=lambda: {
                "status": "failed", "error_code": "deployment_nonzero_exit",
                "job_id": "a" * 32,
            })
        executor.publish_service = publish
    factories = []

    def factory():
        factories.append(True)
        return executor

    auth = auth or Auth()
    app = create_publication_app(ENV, authenticator=auth, executor_factory=factory)
    handler = app.get_functions()[0].get_user_function()
    response = await handler(func.HttpRequest(
        method="POST", url="https://example.test/api/ops/" + operation,
        headers={"Authorization": "Bearer test"}, params={},
        route_params={"operation": operation},
        body=json.dumps({} if payload is None else payload).encode(),
    ))
    assert response.headers["Cache-Control"] == "no-store"
    return response, auth, executor, factories


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["preflight", "sources", "records", "metadata"])
async def test_fixed_read_and_source_operations(operation):
    response, auth, executor, _ = await request(operation)
    assert response.status_code == 200
    assert executor.calls == [operation]
    assert auth.policy == {
        "allowed_clients": frozenset({ENV["CONTOSO_AUTHORING_CLIENT_ID"]}),
        "allowed_callers": frozenset({ENV["CONTOSO_AUTHORING_ADMIN_OID"]}),
        "delegated_scope": "CONTOSO.Deploy",
        "application_role": "CONTOSO.DeploymentAdmin",
    }


@pytest.mark.anyio
async def test_metadata_read_explicitly_does_not_accept_endpoints():
    response, _, _, _ = await request("metadata")
    result = json.loads(response.get_body())
    assert result == {
        "record_contract": 1, "endpoint_validation": "not_requested", "records": {"agents": []},
    }
    response, _, executor, factories = await request("metadata", {"include_endpoint": True})
    assert response.status_code == 400
    assert executor.calls == [] and factories == []


@pytest.mark.anyio
async def test_publish_one_fixed_service_with_one_shot_user_agent():
    payload = {"service": "contoso-foundry-sdk-agent", "process_user_agent": "skill-test"}
    response, _, executor, _ = await request("publish-service", payload)
    assert response.status_code == 200
    assert executor.calls == [("contoso-foundry-sdk-agent", "skill-test")]


@pytest.mark.anyio
async def test_fixed_safe_diagnostics_has_no_arbitrary_command_input():
    response, _, executor, _ = await request(
        "diagnostics", {"process_user_agent": "skill-test"},
    )
    assert response.status_code == 200
    assert executor.calls == [("diagnostics", "skill-test")]


@pytest.mark.anyio
async def test_terminal_deployment_failure_is_not_http_success():
    response, _, _, _ = await request(
        "publish-service",
        {"service": "contoso-foundry-sdk-agent", "process_user_agent": "skill-test"},
        failed=True,
    )
    assert response.status_code == 503
    assert json.loads(response.get_body())["status"] == "failed"


@pytest.mark.anyio
@pytest.mark.parametrize("auth,status", [
    (Auth(rejected=True), 401), (Auth(kind="workload"), 403),
])
async def test_unauthorized_cannot_create_executor_or_touch_sources(auth, status):
    response, _, executor, factories = await request("sources", auth=auth)
    assert response.status_code == status
    assert not factories
    assert not executor.calls


@pytest.mark.anyio
@pytest.mark.parametrize("operation,payload", [
    ("preflight", {"command": "anything"}),
    ("publish-service", {"service": "other", "process_user_agent": "test"}),
    ("publish-service", {"service": "contoso-knowledge-agent", "process_user_agent": "\n"}),
    ("publish-service", {"service": "contoso-knowledge-agent", "process_user_agent": 2}),
    ("publish-service", {"service": "contoso-knowledge-agent", "process_user_agent": "x", "url": "https://bad.test"}),
])
async def test_no_arbitrary_commands_endpoints_or_services(operation, payload):
    response, _, executor, factories = await request(operation, payload)
    assert response.status_code == 400
    assert not factories
    assert not executor.calls


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["invoke", "connections/token", "oauth/callback"])
async def test_no_business_token_or_oauth_routes_in_publication_phase(operation):
    response, _, executor, factories = await request(operation)
    assert response.status_code == 404
    assert not factories
    assert not executor.calls


def test_no_implicit_phase_or_runtime_success_fallback():
    with pytest.raises(ValueError):
        create_app(ENV)
    with pytest.raises(ValueError):
        create_app({**ENV, "CONTOSO_GATEWAY_PHASE": "runtime"})


def test_http_binding_name_matches_actual_python_parameter_for_worker_indexing():
    app = create_publication_app(ENV, authenticator=Auth(), executor_factory=Executor)
    function = app.get_functions()[0]
    bindings = json.loads(function.get_function_json())["bindings"]
    trigger = next(binding for binding in bindings if binding["type"] == "httpTrigger")
    assert trigger["name"] in inspect.signature(function.get_user_function()).parameters


@pytest.mark.anyio
async def test_fixed_oauth_readiness_fails_safely_without_complete_runtime_config():
    response, _, executor, factories = await request("oauth-readiness")
    assert response.status_code == 503
    result = json.loads(response.get_body())
    assert set(result) == {"error", "exception_type", "missing_module"}
    assert result["error"] == "oauth_runtime_unavailable"
    assert not factories and not executor.calls
