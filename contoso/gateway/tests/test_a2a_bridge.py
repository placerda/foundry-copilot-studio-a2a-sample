import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from contoso.connections import ConnectionUnavailable
from contoso.gateway import Gateway, create_function_app
from contoso.gateway.a2a import HostedA2ABridge, validate_a2a_request
from contoso.gateway.tests.test_gateway import (
    setup, signing_key, block_network, request, signed, decoded, runtime_result,
    OTHER,
)


ENDPOINT = "https://contoso.services.ai.azure.com/api/projects/p/agents/contoso-foundry-agent/endpoint/protocols/a2a?api-version=v1"


def frame():
    return {
        "jsonrpc": "2.0", "id": str(uuid4()), "method": "SendMessage",
        "params": {
            "message": {"messageId": str(uuid4()), "role": "ROLE_USER", "parts": [{"text": "Approved question"}]},
            "configuration": {"acceptedOutputModes": ["text/plain"], "returnImmediately": False},
        },
    }


def reply(payload):
    result = runtime_result("copilot-harness")
    result["provider_id"] = "studio-copilot-harness"
    result["sources"][0]["provider_id"] = "studio-copilot-harness"
    return {"jsonrpc": "2.0", "id": payload["id"], "result": {
        "message": {"messageId": str(uuid4()), "role": "ROLE_AGENT", "parts": [{"text": json.dumps(result)}]},
    }}


def bridge_fixture(handler, hosting_transport="native"):
    calls = []
    class Credential:
        def get_token(self, *scopes):
            calls.append(scopes)
            return SimpleNamespace(token="private-gateway-mi-token")
    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    return HostedA2ABridge(
        Credential(), endpoint=ENDPOINT, client_factory=factory, hosting_transport=hosting_transport,
    ), calls


def test_native_default_and_explicit_root_passthrough_carry_identical_a2a_bytes():
    payload = frame()
    returned = reply(payload)
    outgoing = []
    for mode, suffix in [("native", "/a2a?api-version=v1"), ("http-passthrough", "/invocations?api-version=v1")]:
        def server(req):
            assert str(req.url).endswith("/endpoint/protocols" + suffix)
            assert req.headers["a2a-version"] == "1.0"
            outgoing.append(req.content)
            return httpx.Response(200, json=returned)
        bridge, calls = bridge_fixture(server, mode)
        assert bridge.hosting_transport == mode
        assert asyncio.run(bridge.forward(payload)) == returned
        assert calls == [("https://ai.azure.com/.default",)]
    assert outgoing[0] == outgoing[1]
    assert json.loads(outgoing[1]) == payload


@pytest.mark.parametrize("endpoint", [
    "https://attacker.example/a2a", ENDPOINT + "/a2a",
    ENDPOINT.replace("/contoso-foundry-agent/", "/other-agent/"),
    ENDPOINT.replace("/a2a?", "/invocations/a2a?"),
    ENDPOINT.replace("/a2a?", "/invocations?"),
    ENDPOINT + "&url=https://attacker.example", ENDPOINT + "#fragment",
])
def test_passthrough_cannot_replace_fixed_trusted_native_configuration(endpoint):
    with pytest.raises(ValueError):
        HostedA2ABridge(object(), endpoint=endpoint, hosting_transport="http-passthrough")


@pytest.mark.parametrize("mode", ["responses", "auto", "invocations/a2a", "", None])
def test_hosting_transport_has_no_automatic_fallback_or_arbitrary_subpath(mode):
    with pytest.raises(ValueError):
        HostedA2ABridge(object(), endpoint=ENDPOINT, hosting_transport=mode)


def test_passthrough_failure_never_tries_native_or_responses_and_validates_provenance():
    payload = frame()
    paths = []
    returned = reply(payload)
    returned["result"]["message"]["parts"] = [{"text": json.dumps(runtime_result("standard"))}]
    def server(req):
        paths.append(str(req.url))
        return httpx.Response(200, json=returned)
    bridge, _ = bridge_fixture(server, "http-passthrough")
    with pytest.raises(ConnectionUnavailable):
        asyncio.run(bridge.forward(payload))
    assert paths == [ENDPOINT.replace("/a2a?", "/invocations?")]


def test_real_a2a_framing_is_relayed_without_responses_or_requester_token():
    payload = frame()
    returned = reply(payload)
    def server(req):
        assert str(req.url) == ENDPOINT
        assert req.headers["authorization"] == "Bearer private-gateway-mi-token"
        assert req.headers["a2a-version"] == "1.0"
        assert json.loads(req.content) == payload
        return httpx.Response(200, json=returned)
    bridge, calls = bridge_fixture(server)
    assert asyncio.run(bridge.forward(payload)) == returned
    assert calls == [("https://ai.azure.com/.default",)]


@pytest.mark.parametrize("failure", ["native-error", "http500", "wrong-id", "invalid-result", "wrong-provider"])
def test_native_a2a_failure_is_explicit_and_never_sdk_fallback(failure):
    payload = frame()
    returned = reply(payload)
    status = 200
    if failure == "native-error":
        returned = {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32603, "message": "private error"}}
    elif failure == "http500":
        status = 500
    elif failure == "wrong-id":
        returned["id"] = str(uuid4())
    elif failure == "invalid-result":
        returned["result"] = {"message": {"parts": [{"text": "not a RuntimeResult"}]}}
    else:
        returned["result"] = {"message": {"parts": [{"text": json.dumps(runtime_result("standard"))}]}}
    bridge, calls = bridge_fixture(lambda req: httpx.Response(status, json=returned))
    with pytest.raises(ConnectionUnavailable) as caught:
        asyncio.run(bridge.forward(payload))
    assert "private" not in repr(caught.value) and len(calls) == 1


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(method="message/send"), lambda p: p.update(provider_id="other"),
    lambda p: p["params"].update(caller_id=OTHER),
    lambda p: p["params"]["message"].update(role="ROLE_AGENT"),
    lambda p: p["params"]["message"].update(parts=[{"text": "q"}, {"text": "q2"}]),
    lambda p: p["params"]["message"].update(parts=[{"text": "\ud800"}]),
    lambda p: p["params"]["configuration"].update(returnImmediately=True),
])
def test_only_exact_stateless_a2a_envelope_is_accepted(mutate):
    payload = frame()
    mutate(payload)
    with pytest.raises(ValueError, match="Invalid A2A request"):
        validate_a2a_request(payload)


def test_gateway_a2a_binding_uses_exact_requester_role_and_bridge_off_by_default(setup):
    payload = frame()
    assert request(
        setup, path="/api/a2a", payload=payload, headers={"A2A-Version": "1.0"},
    ).status_code == 404
    bridge, _ = bridge_fixture(lambda req: httpx.Response(200, json=reply(payload)))
    gateway = Gateway(
        setup[1], broker=setup[3], network_client=setup[4], authenticator=setup[2],
        enable_invoke=False, a2a_bridge=bridge,
    )
    active = (gateway, *setup[1:])
    response = request(active, path="/api/a2a", payload=payload, headers={"A2A-Version": "1.0"})
    assert response.status_code == 200
    assert decoded(response)["id"] == payload["id"]
    assert request(active, path="/api/invoke").status_code == 404
    assert request(active, path="/api/a2a", payload=payload, headers={
        "A2A-Version": "1.0", "Authorization": "",
    }).status_code == 401
    assert request(active, path="/api/a2a", payload=payload, headers={
        "A2A-Version": "1.0", "Authorization": signed(active, oid=OTHER),
    }).status_code == 403
    assert request(active, path="/api/a2a", payload=payload).status_code == 400
    assert not setup[4].calls and not setup[3].calls
    app = create_function_app(
        setup[1], broker=setup[3], network_client=setup[4], authenticator=setup[2],
        enable_invoke=False, a2a_bridge=bridge,
    )
    names = {f.get_function_name() for f in app.get_functions()}
    assert "contoso_a2a" in names and "contoso_invoke" not in names
