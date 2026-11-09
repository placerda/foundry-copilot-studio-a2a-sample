import http.client
import json
import socket
import threading
from uuid import UUID

import pytest

from contoso_backend.config import CONTOSOSettings, gateway_endpoint
from contoso_backend.errors import ChatFailure
from contoso_backend.local_server import ChatService, LocalServer, LocalSettings

IDS = [str(UUID(int=i)) for i in range(1, 5)]
SETTINGS = LocalSettings(*IDS[:3], CONTOSOSettings(
    "https://synthetic.azurewebsites.net/api/a2a", IDS[0], IDS[1], IDS[3], "synthetic",
    a2a_gateway_url="https://synthetic.azurewebsites.net/api/a2a"))
PAYLOAD = {"messages": [{"role": "user", "content": "Supplier terms?"}],
           "contoso_options": {"integration": "a2a", "studio_variant": "copilot-harness"}}
CONNECT = socket.socket.connect


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def loopback_only(sock, address):
        if address[0] != "127.0.0.1":
            raise AssertionError("External networking is forbidden in HTTP tests")
        return CONNECT(sock, address)
    monkeypatch.setattr(socket.socket, "connect", loopback_only)


class Authorizer:
    def verify(self, token):
        if token != "Bearer synthetic":
            raise ChatFailure(401, "unauthenticated", "Sign in again.")
        return {"tenant_id": IDS[0], "object_id": IDS[2]}


class Requester:
    def invoke(self, question, options, remaining_ms):
        assert question == "Supplier terms?"
        assert remaining_ms == 90000
        return {"answer": "No matching capability.", "output_limited": False,
                "contoso": {"status": "no-matching-capability", "provider_id": None,
                            "studio_variant": None, "sources": [], "citations": [],
                            "execution_path": ["router"]}}


@pytest.fixture
def server(tmp_path):
    # pytest's base directory is set to a repository-local directory by the runner.
    (tmp_path / "index.html").write_text("synthetic frontend")
    instance = LocalServer(0, tmp_path, ChatService(SETTINGS, Authorizer(), Requester()))
    worker = threading.Thread(target=instance.serve_forever, daemon=True)
    worker.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    worker.join(timeout=2)


def call(server, method="POST", path="/api/chat", body=None, changes=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    raw = json.dumps(PAYLOAD).encode() if body is None else body
    headers = {"Host": f"localhost:{server.server_port}", "Origin": server.origin,
               "Authorization": "Bearer synthetic", "Content-Type": "application/json",
               "Content-Length": str(len(raw))}
    headers.update(changes or {})
    connection.request(method, path, body=raw if method == "POST" else None, headers=headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_real_http_contract_and_static(server):
    status, headers, raw = call(server)
    value = json.loads(raw)
    assert status == 200 and value["answer"] == "No matching capability."
    assert value["request_id"] == headers["X-Request-ID"]
    assert value["contoso"]["sources"] == []
    assert headers["Cache-Control"] == "no-store"
    assert call(server, "GET", "/")[2] == b"synthetic frontend"


@pytest.mark.parametrize("changes,status", [
    ({"Host": "attacker.example"}, 403), ({"Origin": "https://attacker.example"}, 403),
    ({"Authorization": ""}, 401), ({"Content-Length": "32769"}, 400),
    ({"Transfer-Encoding": "chunked"}, 400), ({"Content-Encoding": "gzip"}, 400),
    ({"Content-Type": "text/plain"}, 400),
])
def test_negative_transport(server, changes, status):
    assert call(server, changes=changes)[0] == status


@pytest.mark.parametrize("body", [
    b'{"messages":[],"messages":[]}', b'{"messages":NaN}', b"not json",
    json.dumps({"messages": PAYLOAD["messages"]}).encode(),
    json.dumps({**PAYLOAD, "contoso_options": {"integration": "sdk",
                                             "studio_variant": "standard"}}).encode(),
])
def test_invalid_request_never_calls_downstream(server, body):
    assert call(server, body=body)[0] == 400


@pytest.mark.parametrize("method,path,status", [
    ("GET", "/../.env", 404), ("GET", "/config.js?token=secret", 404),
    ("POST", "/api/token", 404), ("OPTIONS", "/api/chat", 405),
    ("GET", "/api/chat", 404),
])
def test_routes(server, method, path, status):
    assert call(server, method, path)[0] == status


def test_apim_exact_allowlist():
    assert gateway_endpoint("https://approved.azure-api.net/api/a2a", "/api/a2a",
                            "approved.azure-api.net") == "https://approved.azure-api.net/api/a2a"
    for url in ["http://approved.azure-api.net/api/a2a",
                "https://other.azure-api.net/api/a2a",
                "https://approved.azure-api.net.evil.example/api/a2a",
                "https://approved.azure-api.net:443/api/a2a",
                "https://approved.azure-api.net/api/a2a?x=1",
                "https://approved.azure-api.net/api/token"]:
        with pytest.raises(ValueError):
            gateway_endpoint(url, "/api/a2a", "approved.azure-api.net")
    with pytest.raises(ValueError):
        gateway_endpoint("https://approved.azure-api.net/api/a2a", "/api/a2a")


def test_local_settings_ignore_aws_and_redact_secret():
    env = dict(zip(("TENANT_ID", "API_AUDIENCE", "ALLOWED_USER_OID"), IDS[:3]))
    env.update(CONTOSO_GATEWAY_URL="https://synthetic.azurewebsites.net/api/a2a",
               CONTOSO_ENTRA_TENANT_ID=IDS[0], CONTOSO_GATEWAY_API_CLIENT_ID=IDS[1],
               CONTOSO_REQUESTER_CLIENT_ID=IDS[3], CONTOSO_REQUESTER_CLIENT_SECRET="synthetic-secret",
               AWS_REGION="invalid", BEDROCK_MODEL_ID="invalid")
    settings = LocalSettings.from_env(env)
    assert "synthetic-secret" not in repr(settings)
    assert not hasattr(settings, "region") and not hasattr(settings, "model_id")


@pytest.mark.parametrize("mode,status", [
    ("shared-delegated", 200), ("user-delegation", 409), ("service-to-service", 409),
    ("unknown", 400), (None, 400), ({"connection_id": "attacker"}, 400),
])
def test_mode_dispatch_never_falls_back(server, mode, status):
    body = json.dumps({**PAYLOAD, "identity_mode": mode}).encode()
    response = call(server, body=body)
    assert response[0] == status
    if status == 200:
        identity = json.loads(response[2])["identity"]
        assert identity["mode"] == mode
        assert identity["application_caller"] == {"tenant_id": IDS[0], "object_id": IDS[2]}
        assert identity["studio_caller"] == {"kind": "fixed-shared-user", "verified": False}


def test_backend_readiness(server):
    status, _, raw = call(server, "GET", "/api/capabilities")
    value = json.loads(raw)
    assert status == 200 and value["default"] == "shared-delegated"
    assert value["modes"]["shared-delegated"]["enabled"]
    assert not value["modes"]["user-delegation"]["enabled"]
    assert not value["modes"]["service-to-service"]["enabled"]
