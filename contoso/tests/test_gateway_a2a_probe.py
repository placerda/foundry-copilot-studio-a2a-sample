import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from contoso.gateway_a2a_probe import probe_native_a2a
from contoso.gateway.tests.test_gateway import block_network


PROJECT = "https://contoso.services.ai.azure.com/api/projects/p"
ENDPOINT = PROJECT + "/agents/contoso-foundry-agent/endpoint/protocols/a2a?api-version=v1"
SECRET = "private-gateway-mi-token"
BOOTSTRAP = {
    "status": "identity-bootstrap", "platform_caller_id": "actual-platform-caller",
    "platform_call_id": "actual-platform-call", "knowledge_access": False,
}


def fixture(handler, *, credential_error=False):
    calls = []

    class Credential:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("closed")

        def get_token(self, *scopes):
            calls.append(scopes)
            if credential_error:
                raise RuntimeError(SECRET)
            return SimpleNamespace(token=SECRET, expires_on=time.time() + 3600)

    def credential_factory(**kwargs):
        assert kwargs == {"connection_timeout": 5, "read_timeout": 10, "retry_total": 0}
        calls.append("credential")
        return Credential()

    def client_factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        assert kwargs["timeout"].read == 35
        calls.append("http")
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    def invoke(endpoint=ENDPOINT, project=PROJECT, hosting_transport="native"):
        return asyncio.run(probe_native_a2a(
            SimpleNamespace(project_endpoint=project), endpoint=endpoint,
            credential_factory=credential_factory, client_factory=client_factory,
            hosting_transport=hosting_transport,
        ))

    return invoke, calls


@pytest.mark.parametrize("transport,suffix", [
    ("native", "/a2a?api-version=v1"), ("http-passthrough", "/invocations?api-version=v1"),
])
def test_probe_selects_trusted_transport_without_changing_protocol_or_identity(transport, suffix):
    def server(request):
        assert str(request.url).endswith("/endpoint/protocols" + suffix)
        assert request.headers["authorization"] == "Bearer " + SECRET
        assert request.headers["a2a-version"] == "1.0"
        payload = json.loads(request.content)
        assert payload["method"] == "SendMessage"
        assert payload["params"]["message"]["role"] == "ROLE_USER"
        assert payload["params"]["message"]["parts"] == [{"text": "CONTOSO identity probe"}]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {
            "message": {"parts": [{"text": json.dumps(BOOTSTRAP)}]},
        }})
    invoke, calls = fixture(server)
    result = invoke(hosting_transport=transport)
    assert result["status"] == "identity-bootstrap" and result["hosting_transport"] == transport
    assert result["integration"] == "a2a" and result["protocol_version"] == "1.0"
    assert calls.count("http") == 1


def test_passthrough_probe_failure_is_labeled_and_never_uses_another_transport():
    paths = []
    def server(request):
        paths.append(str(request.url))
        return httpx.Response(405, content=SECRET)
    invoke, _ = fixture(server)
    result = invoke(hosting_transport="http-passthrough")
    assert result["status"] == "failed" and result["hosting_transport"] == "http-passthrough"
    assert result["http_status"] == 405 and SECRET not in json.dumps(result)
    assert paths == [ENDPOINT.replace("/a2a?", "/invocations?")]


def test_probe_does_not_accept_automatic_or_subpath_transports():
    invoke, calls = fixture(lambda request: pytest.fail("No network permitted"))
    for mode in ("auto", "responses", "invocations/a2a"):
        assert invoke(hosting_transport=mode)["error"]["code"] == "invalid_configuration"
    assert not calls


@pytest.mark.parametrize("task", [False, True])
def test_actual_native_frame_own_managed_identity_and_success_only_from_bootstrap(task):
    def server(request):
        assert str(request.url) == ENDPOINT
        assert request.headers["authorization"] == "Bearer " + SECRET
        assert request.headers["a2a-version"] == "1.0"
        payload = json.loads(request.content)
        assert payload["method"] == "SendMessage" and payload["jsonrpc"] == "2.0"
        assert payload["params"]["message"]["role"] == "ROLE_USER"
        assert payload["params"]["message"]["parts"] == [{"text": "CONTOSO identity probe"}]
        assert payload["params"]["configuration"] == {
            "acceptedOutputModes": ["text/plain"], "returnImmediately": False,
        }
        parts = [{"text": json.dumps(BOOTSTRAP)}]
        result = ({"task": {"status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{"parts": parts}]}}
                  if task else {"message": {"parts": parts}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})
    invoke, calls = fixture(server)
    result = invoke()
    assert result["status"] == "identity-bootstrap"
    assert all(result[key] == value for key, value in BOOTSTRAP.items())
    assert result["protocol_version"] == "1.0" and result["method"] == "SendMessage"
    assert result["http_status"] == 200 and result["integration"] == "a2a"
    assert calls == ["credential", ("https://ai.azure.com/.default",), "http", "closed"]
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("endpoint", [
    "http://169.254.169.254/", "https://attacker.example/",
    PROJECT + "/agents/other/endpoint/protocols/a2a?api-version=v1",
    ENDPOINT + "&url=https://attacker.example", ENDPOINT + "#fragment",
    ENDPOINT.replace("/a2a?", "/responses?"), ENDPOINT.replace("contoso.services", "other.services"),
    "https://user:password@contoso.services.ai.azure.com/api/projects/p",
])
def test_configured_project_fixed_agent_and_protocol_pin_prevent_ssrf(endpoint):
    invoke, calls = fixture(lambda request: pytest.fail("No network permitted"))
    result = invoke(endpoint=endpoint)
    assert result["error"]["code"] == "invalid_configuration" and not calls
    assert "knowledge_access" not in result and "platform_caller_id" not in result


@pytest.mark.parametrize("failure", [
    "rpc500", "http403", "http500-json", "redirect", "wrong-id", "business",
    "large", "bad-json", "duplicate-json", "timeout",
])
def test_failure_retains_real_status_or_rpc_without_secrets_or_success_fallback(failure):
    correlation = "22b02e1d-1b8b-4bcc-845f-bff311f0c337"
    def server(request):
        payload = json.loads(request.content)
        headers = {"apim-request-id": correlation, "x-ms-request-id": SECRET}
        result = {"jsonrpc": "2.0", "id": payload["id"], "result": {
            "message": {"parts": [{"text": json.dumps(BOOTSTRAP)}]},
        }}
        if failure == "rpc500":
            result = {"jsonrpc": "2.0", "id": payload["id"], "error": {
                "code": -32603, "message": "Received 500 " + SECRET, "data": {"private": SECRET},
            }}
        elif failure in ("http403", "redirect"):
            return httpx.Response(403 if failure == "http403" else 302, content=SECRET, headers=headers)
        elif failure == "http500-json":
            return httpx.Response(500, json={"error": SECRET}, headers=headers)
        elif failure == "wrong-id":
            result["id"] = "different"
        elif failure == "business":
            result["result"]["message"]["parts"] = [{"text": json.dumps({"status": "answered"})}]
        elif failure == "large":
            return httpx.Response(200, content=b"x" * 65537, headers=headers)
        elif failure == "bad-json":
            return httpx.Response(200, content=SECRET, headers=headers)
        elif failure == "duplicate-json":
            return httpx.Response(200, content=b'{"jsonrpc":"2.0","id":"a","id":"b"}', headers=headers)
        elif failure == "timeout":
            raise httpx.ReadTimeout(SECRET)
        return httpx.Response(200, json=result, headers=headers)
    invoke, calls = fixture(server)
    result = invoke()
    assert result["status"] == "failed"
    assert "knowledge_access" not in result and "platform_caller_id" not in result
    assert SECRET not in json.dumps(result)
    assert calls.count("http") == 1
    if failure == "rpc500":
        assert result["http_status"] == 200 and result["error"]["jsonrpc_code"] == -32603
        assert result["error"]["detail"] == "Received HTTP 500 from upstream."
        assert result["correlations"] == {"apim-request-id": correlation}
    elif failure == "http403":
        assert result["http_status"] == 403 and result["error"]["code"] == "upstream_http_error"


def test_credential_failure_never_invokes_http_or_exposes_error():
    invoke, calls = fixture(lambda request: pytest.fail("No HTTP permitted"), credential_error=True)
    result = invoke()
    assert result["error"]["code"] == "credential_unavailable"
    assert result["http_status"] is None and "http" not in calls
    assert SECRET not in json.dumps(result)
