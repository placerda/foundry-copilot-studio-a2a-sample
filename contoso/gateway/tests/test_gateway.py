import asyncio
import base64
from dataclasses import replace
import json
import logging
import socket
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import azure.functions as func
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest
import requests

from contoso_backend.errors import ChatFailure
from contoso.connections import (
    AuthorizationStart, ConnectionDenied, ConnectionRequired, ConnectionUnavailable,
    DelegatedConnectionBroker, ProviderConnection,
)
from contoso.connections.oauth import CONNECTION_USE, DEPLOYMENT_ADMIN, STANDARD_SCOPE, HARNESS_SCOPE
from contoso.connections.tests.test_oauth import FakeFactory, MemoryBlobs
from contoso.gateway import (
    Gateway, GatewayResponse, GatewaySettings, create_function_app, create_managed_identity_store,
    validate_runtime_result,
)
from contoso.identity import EntraAuthenticator, IdentitySettings
from contoso.router.core import STUDIOVariant
from contoso.router.runtime import RuntimeResult, ScopedToken
from contoso.state import EncryptedStateStore


TENANT = "11111111-1111-4111-8111-111111111111"
AUDIENCE = "99999999-9999-4999-8999-999999999999"
AWS_CLIENT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
AWS_OID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
AUTHORING_CLIENT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
ADMIN_OID = "55555555-5555-4555-8555-555555555555"
AGENT_CLIENT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
AGENT_OID = "44444444-4444-4444-8444-444444444444"
GUEST = "33333333-3333-4333-8333-333333333333"
NATIVE_CLIENT = "22222222-2222-4222-8222-222222222222"
OTHER = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
SECOND_AGENT_CLIENT = "77777777-7777-4777-8777-777777777777"
SECOND_AGENT_OID = "88888888-8888-4888-8888-888888888888"
SECRET = "private-opaque-bearer-token"
STATE = "s" * 43
QUESTION = "What are the approved consumer insights?"
INPUT = {"question": QUESTION, "studio_variant": "standard", "integration": "sdk"}


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    original_pair = socket.socketpair
    original_connect = socket.socket.connect
    def local_pair(*args, **kwargs):
        # Windows asyncio uses socketpair's internal loopback connection for its
        # wakeup pipe. Permit only that synchronous standard-library operation.
        with monkeypatch.context() as internal:
            internal.setattr(socket.socket, "connect", original_connect)
            return original_pair(*args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("Gateway tests must not contact the network")
    monkeypatch.setattr(socket, "socketpair", local_pair)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def runtime_result(variant="standard"):
    return {
        "status": "answered", "answer": "Approved answer.", "provider_id": "studio-standard",
        "studio_variant": variant,
        "sources": [{
            "source_id": "source", "provider_id": "studio-standard", "version": "1",
            "title": "Approved source", "classification": "public",
        }],
        "citations": [{"source_id": "source", "version": "1", "section": "Findings"}],
        "execution_path": ["Authenticated caller", "Approved provider"],
    }


class FakeBroker:
    def __init__(self):
        self.calls = []
        self.exception = None
        self.start = AuthorizationStart("https://login.microsoftonline.com/authorize", STATE)
        self.token = ScopedToken(
            SECRET, TENANT, "https://api.powerplatform.com", {STANDARD_SCOPE}, time.time() + 600,
        )
        self.callback_result = None

    def prepare_flow(self, provider_id, **kwargs):
        self.calls.append(("prepare", provider_id, kwargs))
        if self.exception:
            raise self.exception
        return self.start

    def get_token(self, provider_id, **kwargs):
        self.calls.append(("token", provider_id, kwargs))
        if self.exception:
            raise self.exception
        return self.token

    def connection_status(self, provider_id, **kwargs):
        self.calls.append(("status", provider_id, kwargs))
        if self.exception:
            raise self.exception
        return "stored"

    def complete_callback(self, **kwargs):
        self.calls.append(("callback", kwargs))
        if self.exception:
            raise self.exception
        return self.callback_result


class FakeNetwork:
    def __init__(self):
        self.calls = []
        self.result = runtime_result()
        self.exception = None
        self.delay = 0
        self.cancelled = False

    async def invoke(self, **kwargs):
        self.calls.append(kwargs)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.exception:
                raise self.exception
            return self.result
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.fixture
def setup(signing_key):
    settings = GatewaySettings(
        TENANT, AUDIENCE, AWS_CLIENT, AWS_OID, AUTHORING_CLIENT, ADMIN_OID, AGENT_CLIENT, AGENT_OID,
    )
    authenticator = EntraAuthenticator(
        settings.identity_settings, cache=SimpleNamespace(get=lambda kid: signing_key.public_key()),
    )
    broker, network = FakeBroker(), FakeNetwork()
    gateway = Gateway(settings, broker=broker, network_client=network, authenticator=authenticator)
    return gateway, settings, authenticator, broker, network, signing_key


def signed(setup, route="invoke", **changes):
    now = int(time.time())
    client, caller, permission = {
        "invoke": (AWS_CLIENT, AWS_OID, "CONTOSO.Invoke"),
        "prepare": (AUTHORING_CLIENT, ADMIN_OID, "CONTOSO.Deploy"),
        "status": (AUTHORING_CLIENT, ADMIN_OID, "CONTOSO.Deploy"),
        "token": (AGENT_CLIENT, AGENT_OID, CONNECTION_USE),
    }[route]
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": AUDIENCE,
        "tid": TENANT, "ver": "2.0", "exp": now + 600, "nbf": now - 10, "iat": now - 10,
        "oid": caller, "azp": client,
    }
    if route in ("prepare", "status"):
        claims["scp"] = permission
    else:
        claims.update(idtyp="app", roles=[permission])
    claims.update(changes)
    return "Bearer " + jwt.encode(claims, setup[-1], algorithm="RS256", headers={"kid": "offline-key"})


def request(setup, route="invoke", *, raw=None, payload=None, headers=None, method="POST", path=None):
    paths = {
        "invoke": "/api/invoke", "prepare": "/api/connections/prepare",
        "status": "/api/connections/status",
        "token": "/api/connections/token", "callback": "/api/oauth/callback",
    }
    default = INPUT if route == "invoke" else {"provider_id": "studio-standard"}
    header = {"Content-Type": "application/json"}
    if route != "callback":
        header["Authorization"] = signed(setup, route)
    else:
        header["Content-Type"] = "application/x-www-form-urlencoded"
    if headers is not None:
        header.update(headers)
    if raw is None:
        if route == "callback":
            raw = urlencode({"state": STATE, "code": "private-code"} if payload is None else payload).encode()
        else:
            raw = json.dumps(default if payload is None else payload).encode()
    return asyncio.run(setup[0].handle(
        method, paths[route] if path is None else path, headers=header, body=raw,
    ))


def decoded(response):
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert len(response.headers["X-Correlation-ID"]) == 32
    return json.loads(response.body)


def test_operator_status_is_metadata_only_and_uses_preparer_grant(setup):
    response = request(setup, "status")
    assert response.status_code == 200
    assert decoded(response) == {
        "provider_id": "studio-standard", "status": "stored", "live_token_validated": False,
    }
    assert setup[3].calls == [("status", "studio-standard", {
        "preparer_id": ADMIN_OID, "preparer_roles": frozenset({DEPLOYMENT_ADMIN}),
    })]
    assert SECRET not in response.body.decode()


@pytest.mark.parametrize("identity", ["invoke", "token"])
def test_operator_status_rejects_requester_and_token_consumers(setup, identity):
    response = request(setup, "status", headers={"Authorization": signed(setup, identity)})
    assert response.status_code == 403
    assert not setup[3].calls


def test_operator_status_missing_connection_and_untrusted_fields(setup):
    setup[3].exception = ConnectionRequired("private-cache-details")
    response = request(setup, "status")
    assert response.status_code == 409
    assert decoded(response)["error"]["code"] == "connection_required"
    assert b"private" not in response.body
    setup[3].calls.clear()
    response = request(setup, "status", payload={"provider_id": "studio-standard", "scopes": ["arbitrary"]})
    assert response.status_code == 400
    assert not setup[3].calls


@pytest.mark.parametrize("extra", [{"client_info": "private-client-info"}, {"iss": "private-issuer"}])
def test_callback_diagnostics_record_exact_guard_without_oauth_values(setup, caplog, extra):
    from contoso.gateway.callback_diagnostics import read_callback
    with caplog.at_level(logging.INFO, logger="contoso.gateway"):
        response = request(setup, "callback", payload={
            "state": STATE, "code": "private-real-code", "session_state": "private-session",
            "provider_id": "private-untrusted-routing", **extra,
        })
    assert response.status_code == 400
    diagnostic = read_callback(decoded(response)["correlation_id"])
    assert diagnostic["validation_stage"] == "form_fields"
    assert diagnostic["method"] == "POST"
    assert diagnostic["content_type"] == "application/x-www-form-urlencoded"
    assert diagnostic["field_names"] == sorted(["state", "code", "session_state", *extra])
    assert diagnostic["unknown_field_count"] == 1
    assert diagnostic["has_code"] and diagnostic["has_state"] and not diagnostic["has_error"]
    assert diagnostic["broker_called"] is False
    assert not setup[3].calls
    rendered = json.dumps(diagnostic) + caplog.text
    for value in [STATE, "private-real-code", "private-session", "private-untrusted-routing", *extra.values()]:
        assert value not in rendered


@pytest.mark.parametrize("metadata", [
    {"client_info": "opaque-untrusted-provider-metadata"},
    {"client_info": ""},
    {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0"},
    {"client_info": "opaque-untrusted-provider-metadata",
     "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "session_state": "private-session"},
])
def test_callback_standard_metadata_is_bounded_and_never_forwarded_as_identity(setup, metadata):
    response = request(setup, "callback", payload={"state": STATE, "code": "private-code", **metadata})
    assert response.status_code == 200
    assert setup[3].calls == [("callback", {"auth_response": {"state": STATE, "code": "private-code"}})]
    assert b"private-code" not in response.body and b"opaque-untrusted" not in response.body


@pytest.mark.parametrize("metadata", [
    {"client_info": "x" * 2049},
    {"client_info": "private\x00metadata"},
    {"iss": f"https://login.microsoftonline.com/{OTHER}/v2.0"},
    {"iss": f"http://login.microsoftonline.com/{TENANT}/v2.0"},
    {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0?attacker=1"},
    {"iss": f"https://attacker.example/{TENANT}/v2.0"},
    {"iss": ""},
    {"guest_oid": GUEST},
    {"scope": HARNESS_SCOPE},
    {"client_info": "ignored", "error_description": "not-an-error"},
], ids=["oversized-client-info", "control-character", "other-tenant", "non-https-issuer",
        "issuer-query", "other-host", "empty-issuer", "guest-spoof", "scope-injection", "mixed-error"])
def test_callback_metadata_security_negatives_do_not_reach_broker(setup, metadata):
    response = request(setup, "callback", payload={"state": STATE, "code": "private-code", **metadata})
    assert response.status_code == 400
    assert not setup[3].calls


@pytest.mark.parametrize("field", ["client_info", "iss", "state", "code", "session_state"])
def test_callback_duplicate_metadata_or_security_fields_are_rejected(setup, field):
    body = urlencode({
        "state": STATE, "code": "private-code", "client_info": "opaque",
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "session_state": "opaque-session",
    }) + "&" + urlencode({field: "duplicate"})
    assert request(setup, "callback", raw=body.encode()).status_code == 400
    assert not setup[3].calls


@pytest.mark.parametrize("tail", ["&client_info=%ZZ", "&client_info=%FF", "&client_info", "&iss=%C3"])
def test_callback_malformed_metadata_form_encoding_is_rejected(setup, tail):
    body = urlencode({"state": STATE, "code": "private-code"}) + tail
    assert request(setup, "callback", raw=body.encode()).status_code == 400
    assert not setup[3].calls


@pytest.mark.parametrize("variant,integration", [
    ("standard", "sdk"), ("standard", "a2a"),
    ("copilot-harness", "sdk"), ("copilot-harness", "a2a"),
])
def test_invoke_signed_exact_workload_only_and_no_token_forwarding(setup, variant, integration):
    setup[4].result = runtime_result(variant)
    payload = {**INPUT, "studio_variant": variant, "integration": integration}
    response = request(setup, payload=payload)
    assert response.status_code == 200
    assert decoded(response) == setup[4].result
    assert setup[4].calls == [payload]
    assert not setup[3].calls
    assert "Bearer" not in repr(setup[4].calls)


def test_prepare_delegated_exact_admin_maps_proved_scope_to_operator_role(setup):
    response = request(setup, "prepare")
    assert response.status_code == 200
    assert decoded(response) == {"auth_uri": setup[3].start.auth_uri, "state": STATE}
    assert setup[3].calls == [(
        "prepare", "studio-standard",
        {"preparer_id": ADMIN_OID, "preparer_roles": frozenset({DEPLOYMENT_ADMIN})},
    )]
    assert not setup[4].calls


@pytest.mark.parametrize("scope", [STANDARD_SCOPE, HARNESS_SCOPE])
def test_token_route_returns_scoped_token_only_to_exact_agent_workload(setup, scope, caplog):
    setup[3].token = replace(setup[3].token, scopes={scope})
    with caplog.at_level(logging.INFO, logger="contoso.gateway"):
        response = request(setup, "token")
    assert response.status_code == 200
    assert decoded(response) == {
        "token": SECRET, "tenant_id": TENANT, "audience": "https://api.powerplatform.com",
        "scopes": [scope], "expires_at": setup[3].token.expires_at,
    }
    assert setup[3].calls == [(
        "token", "studio-standard", {"caller_id": AGENT_OID, "caller_roles": frozenset({CONNECTION_USE})},
    )]
    assert SECRET not in caplog.text and AGENT_OID not in caplog.text
    assert SECRET not in repr(response) and isinstance(response, GatewayResponse)


@pytest.mark.parametrize("route", ["invoke", "prepare", "token"])
@pytest.mark.parametrize("changes", [
    {"azp": OTHER}, {"oid": OTHER},
    {"aud": OTHER}, {"tid": OTHER}, {"exp": 1},
    {"roles": ["Other.Role"], "scp": "Other.Scope"},
])
def test_wrong_signed_client_oid_permission_or_claims_never_reaches_dependencies(setup, route, changes):
    response = request(setup, route, headers={"Authorization": signed(setup, route, **changes)})
    assert response.status_code in (401, 403)
    decoded(response)
    assert not setup[3].calls and not setup[4].calls


@pytest.mark.parametrize("route", ["invoke", "prepare", "token"])
def test_identity_kind_separation_even_when_matching_permission(setup, route):
    if route == "prepare":
        token = signed(setup, route, scp=None, idtyp="app", roles=["CONTOSO.Deploy"])
    else:
        permission = "CONTOSO.Invoke" if route == "invoke" else CONNECTION_USE
        token = signed(setup, route, idtyp=None, roles=None, scp=permission)
    response = request(setup, route, headers={"Authorization": token})
    assert response.status_code == 403
    assert not setup[3].calls and not setup[4].calls


@pytest.mark.parametrize("route", ["invoke", "prepare", "token"])
@pytest.mark.parametrize("header", ["", "Basic private", "Bearer", "Bearer forged"])
def test_missing_or_forged_bearer_cannot_use_spoofed_easy_auth_identity(setup, route, header):
    response = request(setup, route, headers={
        "Authorization": header,
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(b'{"roles":["CONTOSO.Connection.Use"]}').decode(),
        "X-MS-TOKEN-AAD-ACCESS-TOKEN": signed(setup, route),
        "X-MS-CLIENT-PRINCIPAL-ID": AGENT_OID,
    })
    assert response.status_code == 401
    assert not setup[3].calls and not setup[4].calls


def test_anonymous_callback_has_no_bearer_or_operator_roles_and_no_reflection(setup, caplog):
    with caplog.at_level(logging.INFO, logger="contoso.gateway"):
        response = request(setup, "callback", payload={
            "state": STATE, "code": "private-code", "session_state": "private-session-state",
        }, headers={"X-Correlation-ID": "private-attacker-id"})
    assert response.status_code == 200
    assert decoded(response) == {
        "status": "connected", "correlation_id": response.headers["X-Correlation-ID"],
    }
    assert setup[3].calls == [("callback", {"auth_response": {"state": STATE, "code": "private-code"}})]
    for secret in (STATE, "private-code", "private-session-state", "private-attacker-id"):
        assert secret not in response.body.decode() and secret not in caplog.text
    assert not setup[4].calls


@pytest.mark.parametrize("field", ["provider_id", "caller_id", "tenant_id", "roles", "audience", "scope"])
def test_callback_cannot_authorize_identity_or_provider_from_body(setup, field):
    response = request(setup, "callback", payload={
        "state": STATE, "code": "private-code", field: "private-substitution",
    })
    assert response.status_code == 400
    assert not setup[3].calls
    assert "private-substitution" not in response.body.decode()


def test_callback_known_oauth_error_is_redacted_and_not_success(setup):
    setup[3].exception = ConnectionRequired("private-error-description")
    response = request(setup, "callback", payload={
        "state": STATE, "error": "access_denied", "error_description": "private-description with PII",
        "error_uri": "https://private.example/error",
    })
    assert response.status_code == 409
    assert setup[3].calls == [("callback", {"auth_response": {"state": STATE, "error": "access_denied"}})]
    assert "private" not in response.body.decode()


@pytest.mark.parametrize("raw", [
    b'{"question":"first","question":"second","studio_variant":"standard","integration":"sdk"}',
    b'{"question":"q","studio_variant":"standard","integration":"sdk","roles":["CONTOSO.Invoke"]}',
    b'{"question":"q","studio_variant":null,"integration":"sdk"}',
    b'{"question":"q","studio_variant":"unknown","integration":"sdk"}',
    b'{"question":"q","studio_variant":"standard","integration":"http"}',
    b'{"question":2,"studio_variant":"standard","integration":"sdk"}',
    b'{"question":"\\ud800","studio_variant":"standard","integration":"sdk"}',
    b'{"question":"\\u0000","studio_variant":"standard","integration":"sdk"}',
    b'{"question":NaN,"studio_variant":"standard","integration":"sdk"}',
    b"{}", b"[]", b"null", b"\xff", b"", b"{" * 3000,
])
def test_strict_json_utf8_exact_keys_and_enums(setup, raw):
    response = request(setup, raw=raw)
    assert response.status_code == 400
    decoded(response)
    assert not setup[3].calls and not setup[4].calls


@pytest.mark.parametrize("route", ["prepare", "token"])
@pytest.mark.parametrize("raw", [
    b'{"provider_id":"first","provider_id":"second"}',
    b'{"provider_id":"p","caller_roles":["CONTOSO.Connection.Use"]}',
    b'{"provider_id":null}', b'{"provider_id":" "}', b'{"provider_id":"p q"}',
    b'{"provider_id":"\\ud800"}', b"[]",
])
def test_connection_json_is_exact_and_bounded(setup, route, raw):
    response = request(setup, route, raw=raw)
    assert response.status_code == 400 and not setup[3].calls


@pytest.mark.parametrize("raw", [
    f"state={STATE}&state={STATE}&code=private".encode(),
    f"state={STATE}&code=private&code=private2".encode(),
    f"state={STATE}&code=%GG".encode(),
    f"state={STATE}&code=%FF".encode(),
    f"state={STATE}&code=%".encode(),
    f"state={STATE}&code=private&error=denied".encode(),
    f"state={STATE}&error_description=private".encode(),
    f"state={STATE}&code=private&error_description=private".encode(),
    f"state={STATE}&code=".encode(), b"state=short&code=private",
    f"state={STATE}&code=private&missing-value".encode(), b"\xff",
])
def test_strict_form_encoding_duplicates_and_oauth_fields(setup, raw):
    response = request(setup, "callback", raw=raw)
    assert response.status_code == 400 and not setup[3].calls


@pytest.mark.parametrize("question", ["", " ", "x" * 2001, "\u0800" * 2001],
                         ids=["empty", "whitespace", "too-long", "unicode-too-long"])
def test_question_limits(setup, question):
    response = request(setup, payload={**INPUT, "question": question})
    assert response.status_code == 400 and not setup[4].calls


@pytest.mark.parametrize("route,raw", [
    ("invoke", b"x" * 16385), ("prepare", b"x" * 1025), ("token", b"x" * 1025),
    ("callback", b"x" * 32769),
], ids=["invoke-limit", "prepare-limit", "token-limit", "callback-limit"])
def test_body_caps(setup, route, raw):
    response = request(setup, route, raw=raw)
    assert response.status_code == 400
    assert not setup[3].calls and not setup[4].calls


@pytest.mark.parametrize("headers", [
    {"Content-Type": "text/plain"}, {"Content-Type": "application/json; charset=latin-1"},
    {"Content-Type": "application/json; charset=utf-8; charset=utf-8"},
    {"Content-Encoding": "gzip"}, {"Content-Length": "1"},
    {"authorization": "Bearer private"}, {"X-Correlation-ID": "private\r\nInjected: yes"},
])
def test_headers_content_types_and_lengths_are_strict(setup, headers):
    response = request(setup, headers=headers)
    assert response.status_code == 400
    assert not setup[3].calls and not setup[4].calls


@pytest.mark.parametrize("route", ["prepare", "token", "callback"])
@pytest.mark.parametrize("exception,status", [
    (ConnectionDenied("private-provider-denial"), 403),
    (ConnectionRequired("private-token-invalid"), 409),
    (ConnectionUnavailable("private-cache-unavailable"), 503),
    (ValueError("private-token-dump"), 503),
])
def test_broker_errors_are_no_store_redacted_without_success(setup, route, exception, status, caplog):
    setup[3].exception = exception
    with caplog.at_level(logging.INFO, logger="contoso.gateway"):
        response = request(setup, route)
    assert response.status_code == status
    decoded(response)
    assert "private" not in response.body.decode() and "private" not in caplog.text


@pytest.mark.parametrize("changed", [
    {"status": "unavailable"}, {"status": "unknown"}, {"answer": "x" * 12001},
    {"provider_id": None}, {"studio_variant": "copilot-harness"},
    {"sources": [{"source_id": "not-complete"}]}, {"citations": [{"source_id": "unbound"}]},
    {"execution_path": []}, {"execution_path": ["step"] * 13},
    {"unexpected": SECRET}, {"answer": "\ud800"},
])
def test_provider_runtime_response_is_validated_and_unavailable_is_not_success(setup, changed):
    setup[4].result.update(changed)
    response = request(setup)
    assert response.status_code == 503
    decoded(response)
    assert SECRET not in response.body.decode()
    assert len(setup[4].calls) == 1


def test_unbound_citation_and_wrong_source_provider_are_rejected(setup):
    setup[4].result["citations"][0]["version"] = "unbound"
    assert request(setup).status_code == 503
    setup[4].result = runtime_result()
    setup[4].result["sources"][0]["provider_id"] = "other-provider"
    assert request(setup).status_code == 503


def test_existing_runtime_result_no_match_shape_is_supported_without_fallback(setup):
    setup[4].result = RuntimeResult(
        "no-matching-capability", "No approved agent matches this request.", None,
        ("Authenticated caller", "No matching capability"),
    )
    response = request(setup)
    assert response.status_code == 200
    result = decoded(response)
    assert result["status"] == "no-matching-capability" and result["provider_id"] is None
    assert len(setup[4].calls) == 1 and not setup[3].calls


def test_non_studio_runtime_result_preserves_optional_variant_contract(setup):
    setup[4].result = runtime_result()
    setup[4].result["provider_id"] = "knowledge-knowledge"
    setup[4].result["studio_variant"] = None
    setup[4].result["sources"][0]["provider_id"] = "knowledge-knowledge"
    response = request(setup)
    assert response.status_code == 200
    assert decoded(response)["studio_variant"] is None
    assert len(setup[4].calls) == 1


def test_exported_validator_is_strict_bounded_detached_and_safe():
    payload = runtime_result()
    validated = validate_runtime_result(payload)
    assert validated == payload and validated is not payload
    payload["sources"][0]["title"] = "changed"
    assert validated["sources"][0]["title"] == "Approved source"
    for malformed in (
        {"token": SECRET}, {**runtime_result(), "unexpected": SECRET},
        {**runtime_result(), "studio_variant": "invalid"},
        {**runtime_result(), "answer": "\ud800"},
    ):
        with pytest.raises(ValueError) as caught:
            validate_runtime_result(malformed)
        assert str(caught.value) == "Invalid CONTOSO runtime result."
        assert SECRET not in repr(caught.value)
    unavailable = {**runtime_result(), "status": "unavailable"}
    assert validate_runtime_result(unavailable)["status"] == "unavailable"
    with pytest.raises(ValueError):
        validate_runtime_result(RuntimeResult("no-matching-capability", "No match", None, ("No match",)))


def test_async_callable_network_dependency_invoke_and_functions_factory(setup):
    calls = []
    async def invoke(question: str, studio_variant: str, integration: str):
        calls.append((question, studio_variant, integration))
        return validate_runtime_result(runtime_result(studio_variant))
    gateway = Gateway(
        setup[1], broker=setup[3], network_client=invoke, authenticator=setup[2],
    )
    response = asyncio.run(gateway.handle(
        "POST", "/api/invoke", headers={
            "Content-Type": "application/json", "Authorization": signed(setup),
        }, body=json.dumps(INPUT).encode(),
    ))
    assert response.status_code == 200
    assert calls == [(QUESTION, "standard", "sdk")]
    app = create_function_app(
        setup[1], broker=setup[3], network_client=invoke, authenticator=setup[2],
    )
    assert len(app.get_functions()) == 6
    with pytest.raises(ValueError):
        Gateway(setup[1], broker=setup[3], network_client=object(), authenticator=setup[2])


@pytest.mark.parametrize("token", [
    None, {"token": SECRET},
    ScopedToken(SECRET, OTHER, "https://api.powerplatform.com", {STANDARD_SCOPE}, time.time() + 600),
    ScopedToken(SECRET, TENANT, "https://graph.microsoft.com", {"User.Read"}, time.time() + 600),
    ScopedToken(SECRET, TENANT, "https://api.powerplatform.com", {STANDARD_SCOPE}, 1),
])
def test_invalid_broker_token_return_is_never_exposed(setup, token):
    setup[3].token = token
    response = request(setup, "token")
    assert response.status_code == 503 and SECRET not in response.body.decode()


@pytest.mark.parametrize("result", [False, {"token": SECRET}, SECRET])
def test_callback_must_not_return_tokens_or_arbitrary_results(setup, result):
    setup[3].callback_result = result
    response = request(setup, "callback")
    assert response.status_code == 503 and SECRET not in response.body.decode()


def test_async_network_timeout_cancels_with_no_retry_or_fallback(setup):
    setup[0].settings = replace(setup[1], invocation_timeout=0.01)
    setup[4].delay = 1
    response = request(setup)
    assert response.status_code == 504
    assert decoded(response)["error"]["code"] == "timeout"
    assert setup[4].cancelled and len(setup[4].calls) == 1 and not setup[3].calls


def test_authentication_timeout_fails_before_network(setup, monkeypatch):
    setup[0].settings = replace(setup[1], authentication_timeout=0.01)
    def delayed(*args, **kwargs):
        time.sleep(0.02)
        raise RuntimeError("private")
    monkeypatch.setattr(setup[2], "verify", delayed)
    response = request(setup)
    assert response.status_code == 504
    assert not setup[3].calls and not setup[4].calls


def test_broker_timeout_fails_without_success(setup, monkeypatch):
    setup[0].settings = replace(setup[1], connection_timeout=0.01)
    def delayed(*args, **kwargs):
        time.sleep(0.02)
    monkeypatch.setattr(setup[3], "complete_callback", delayed)
    response = request(setup, "callback")
    assert response.status_code == 504 and decoded(response)["error"]["code"] == "timeout"


def test_unexpected_auth_or_network_exception_has_safe_correlated_log(setup, caplog):
    setup[4].exception = RuntimeError("private-bearer-and-cache-dump")
    with caplog.at_level(logging.INFO, logger="contoso.gateway"):
        response = request(setup)
    assert response.status_code == 503
    assert "private" not in response.body.decode() + caplog.text
    assert response.headers["X-Correlation-ID"] in caplog.text
    assert len(setup[4].calls) == 1


@pytest.mark.parametrize("path", ["/api/unknown", "/api/invoke/", "/invoke", "/api/oauth/callback/"])
def test_unknown_routes_are_safe(setup, path):
    response = request(setup, path=path)
    assert response.status_code == 404
    decoded(response)
    assert not setup[3].calls and not setup[4].calls


@pytest.mark.parametrize("route", ["invoke", "prepare", "token", "callback"])
def test_known_routes_are_post_only(setup, route):
    response = request(setup, route, method="GET")
    assert response.status_code == 405 and response.headers["Allow"] == "POST"
    decoded(response)
    assert not setup[3].calls and not setup[4].calls


def test_cloud_settings_env_has_no_defaults_for_identities(setup):
    env = {
        "CONTOSO_TENANT_ID": TENANT, "CONTOSO_GATEWAY_AUDIENCE": AUDIENCE,
        "CONTOSO_AWS_REQUESTER_CLIENT_ID": AWS_CLIENT, "CONTOSO_AWS_REQUESTER_OID": AWS_OID,
        "CONTOSO_AUTHORING_CLIENT_ID": AUTHORING_CLIENT, "CONTOSO_AUTHORING_ADMIN_OID": ADMIN_OID,
        "CONTOSO_MANAGED_AGENT_CLIENT_ID": AGENT_CLIENT, "CONTOSO_MANAGED_AGENT_OID": AGENT_OID,
    }
    assert GatewaySettings.from_env(env) == setup[1]
    with pytest.raises(ValueError):
        GatewaySettings.from_env({})
    with pytest.raises(ValueError):
        GatewaySettings.from_env({**env, "CONTOSO_AUTHORING_ADMIN_OID": "admin@example.com"})
    with pytest.raises(ValueError):
        replace(setup[1], invocation_timeout=float("nan"))
    with pytest.raises(ValueError):
        replace(setup[1], connection_timeout=121)
    with pytest.raises(ValueError):
        Gateway(
            setup[1], broker=setup[3], network_client=setup[4],
            authenticator=EntraAuthenticator(IdentitySettings(TENANT, OTHER)),
        )


def test_real_functions_entrypoint_factory_registers_and_dispatches_all_routes(setup):
    app = create_function_app(
        setup[1], broker=setup[3], network_client=setup[4], authenticator=setup[2],
    )
    assert isinstance(app, func.FunctionApp)
    functions = {f.get_function_name(): f for f in app.get_functions()}
    assert set(functions) == {"contoso_invoke", "contoso_prepare", "contoso_status", "contoso_token", "contoso_callback", "contoso_unknown"}
    for name in ("contoso_invoke", "contoso_prepare", "contoso_status", "contoso_token", "contoso_callback"):
        function = functions[name]
        binding = next(b for b in function.get_dict_repr()["bindings"] if b["type"] == "httpTrigger")
        assert [method.value for method in binding["methods"]] == ["POST"]
        assert binding["authLevel"].value == "anonymous"
    request_object = func.HttpRequest(
        method="POST", url="https://gateway.example.com/api/invoke",
        headers={"Content-Type": "application/json", "Authorization": signed(setup)},
        body=json.dumps(INPUT).encode(),
    )
    response = asyncio.run(functions["contoso_invoke"].get_user_function()(request_object))
    assert isinstance(response, func.HttpResponse) and response.status_code == 200
    assert json.loads(response.get_body()) == runtime_result()
    callback_object = func.HttpRequest(
        method="POST", url="https://gateway.example.com/api/oauth/callback",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urlencode({"state": STATE, "code": "private"}).encode(),
    )
    response = asyncio.run(functions["contoso_callback"].get_user_function()(callback_object))
    assert response.status_code == 200 and b"private" not in response.get_body()
    unauthorized = func.HttpRequest(
        method="POST", url="https://gateway.example.com/api/invoke",
        headers={"Content-Type": "application/json"}, body=json.dumps(INPUT).encode(),
    )
    response = asyncio.run(functions["contoso_invoke"].get_user_function()(unauthorized))
    assert response.status_code == 401 and response.headers["Cache-Control"] == "no-store"
    query_code = func.HttpRequest(
        method="POST", url="https://gateway.example.com/api/oauth/callback?code=private",
        headers={"Content-Type": "application/x-www-form-urlencoded"}, body=b"state=none",
    )
    response = asyncio.run(functions["contoso_callback"].get_user_function()(query_code))
    assert response.status_code == 404 and b"private" not in response.get_body()


def test_actual_managed_identity_state_constructor_no_network(monkeypatch):
    import contoso.gateway.state as state_module
    calls = []
    credential = object()
    container = object()
    def identity(**kwargs):
        calls.append(("identity", kwargs))
        return credential
    def container_factory(url, **kwargs):
        calls.append(("container", url, kwargs))
        return container
    monkeypatch.setattr(state_module, "ManagedIdentityCredential", identity)
    monkeypatch.setattr(state_module.ContainerClient, "from_container_url", container_factory)
    store = create_managed_identity_store(
        container_url="https://test.blob.core.windows.net/oauth-tokens",
        encoded_key=base64.urlsafe_b64encode(b"x" * 32).decode(),
        namespace="protected-gateway", managed_identity_client_id=AGENT_CLIENT,
    )
    assert isinstance(store, EncryptedStateStore)
    assert store.contoso_backend.container is container
    assert calls == [
        ("identity", {"client_id": AGENT_CLIENT}),
        ("container", "https://test.blob.core.windows.net/oauth-tokens", {
            "credential": credential, "connection_timeout": 3.05, "read_timeout": 10, "retry_total": 1,
        }),
    ]


@pytest.mark.parametrize("url", [
    "http://test.blob.core.windows.net/oauth-tokens",
    "https://private.example.com/oauth-tokens",
    "https://test.blob.core.windows.net/oauth-tokens?sig=private",
    "https://user:private@test.blob.core.windows.net/oauth-tokens",
    "https://test.blob.core.windows.net/oauth-tokens/another",
])
def test_state_factory_disallows_keys_sas_and_non_blob_endpoints(url):
    with pytest.raises(ValueError):
        create_managed_identity_store(
            container_url=url, encoded_key=base64.urlsafe_b64encode(b"x" * 32).decode(), namespace="test",
        )


@pytest.mark.parametrize("provider,variant,audience,scope", [
    ("studio-standard", STUDIOVariant.STANDARD, "https://api.powerplatform.com", STANDARD_SCOPE),
    ("studio-copilot-harness", STUDIOVariant.COPILOT_HARNESS,
     "https://api.powerplatform.com/copilotstudio-a2a", HARNESS_SCOPE),
])
@pytest.mark.parametrize("metadata", [
    {}, {"client_info": "untrusted-profile-metadata"},
    {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0"},
    {"client_info": "untrusted-profile-metadata", "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0"},
], ids=["core", "client-info", "issuer", "both"])
def test_gateway_real_broker_operator_flow_anonymous_guest_callback_and_replay(
    setup, provider, variant, audience, scope, metadata,
):
    contoso_backend = MemoryBlobs()
    store = EncryptedStateStore(
        contoso_backend, base64.urlsafe_b64encode(b"x" * 32).decode(), namespace="gateway-offline",
    )
    spec = ProviderConnection(
        provider, TENANT, NATIVE_CLIENT, GUEST,
        "https://gateway.example.com/api/oauth/callback", variant, audience, {scope},
    )
    factory = FakeFactory()
    factory.exchange_result["scope"] = factory.silent_result["scope"] = scope
    broker = DelegatedConnectionBroker(
        store, {spec.provider_id: spec}, {AGENT_OID: {spec.provider_id}},
        preparer_providers={ADMIN_OID: {spec.provider_id}}, msal_factory=factory,
    )
    setup[0].broker = broker
    prepared = request(setup, "prepare", payload={"provider_id": provider})
    assert prepared.status_code == 200
    flow = decoded(prepared)
    calls = len(factory.calls)
    wrong_issuer = request(setup, "callback", payload={
        "state": flow["state"], "code": "private-code",
        "client_info": "untrusted-profile-metadata", "iss": f"https://login.microsoftonline.com/{OTHER}/v2.0",
    })
    assert wrong_issuer.status_code == 400 and len(factory.calls) == calls
    assert store.get("oauth-flow", "callback:" + flow["state"])
    response = request(setup, "callback", payload={
        "state": flow["state"], "code": "private-code", "session_state": "private-session", **metadata,
    })
    assert response.status_code == 200
    status = request(setup, "status", payload={"provider_id": provider})
    assert status.status_code == 200 and decoded(status)["status"] == "stored"
    assert request(setup, "token", payload={"provider_id": provider}).status_code == 200
    exchange = next(call for call in factory.calls if call[0] == "exchange")
    assert exchange[2] == {"state": flow["state"], "code": "private-code"}
    count = len(factory.calls)
    replay = request(setup, "callback", payload={
        "state": flow["state"], "code": "private-code", **metadata,
    })
    assert replay.status_code == 409 and len(factory.calls) == count
    prepared = decoded(request(setup, "prepare", payload={"provider_id": provider}))
    factory.exchange_result["id_token_claims"] = {"tid": TENANT, "oid": ADMIN_OID}
    failed = request(setup, "callback", payload={
        "state": prepared["state"], "code": "private-code", **metadata,
    })
    assert failed.status_code == 409
    forbidden = request(setup, "token", headers={"Authorization": signed(setup, "prepare")})
    assert forbidden.status_code == 403
    unauthorized = request(setup, "token", payload={"provider_id": "other-provider"})
    assert unauthorized.status_code == 403
    error_flow = decoded(request(setup, "prepare", payload={"provider_id": provider}))
    calls = len(factory.calls)
    failed = request(setup, "callback", payload={
        "state": error_flow["state"], "error": "access_denied",
        "error_description": "private-provider-error", "error_uri": "https://example.test/private-error",
        "session_state": "private-session", "client_info": "untrusted-profile-metadata",
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
    })
    assert failed.status_code == 409 and len(factory.calls) == calls
    assert b"private-provider-error" not in failed.body
    nonce_flow = decoded(request(setup, "prepare", payload={"provider_id": provider}))
    factory.exchange_exception = RuntimeError("private-nonce-mismatch")
    failed = request(setup, "callback", payload={
        "state": nonce_flow["state"], "code": "private-code", "client_info": "untrusted-profile-metadata",
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
    })
    assert failed.status_code == 409 and b"private-nonce-mismatch" not in failed.body


def multi_identity_settings(setup):
    settings = replace(
        setup[1], managed_agent_identities=((SECOND_AGENT_CLIENT, SECOND_AGENT_OID),),
    )
    setup[0].settings = settings
    return settings


@pytest.mark.parametrize("client,oid", [
    (AGENT_CLIENT, AGENT_OID), (SECOND_AGENT_CLIENT, SECOND_AGENT_OID),
])
def test_each_explicit_agent_pair_can_token_vend(setup, client, oid):
    multi_identity_settings(setup)
    response = request(setup, "token", headers={
        "Authorization": signed(setup, "token", azp=client, oid=oid),
    })
    assert response.status_code == 200
    assert decoded(response)["token"] == SECRET
    assert setup[3].calls == [(
        "token", "studio-standard", {"caller_id": oid, "caller_roles": frozenset({CONNECTION_USE})},
    )]


@pytest.mark.parametrize("client,oid", [
    (AGENT_CLIENT, SECOND_AGENT_OID), (SECOND_AGENT_CLIENT, AGENT_OID),
    (OTHER, AGENT_OID), (AGENT_CLIENT, OTHER),
    (AWS_CLIENT, SECOND_AGENT_OID), (AUTHORING_CLIENT, SECOND_AGENT_OID),
])
def test_crossed_unknown_or_foreign_agent_pair_is_denied_before_broker(setup, client, oid):
    multi_identity_settings(setup)
    response = request(setup, "token", headers={
        "Authorization": signed(setup, "token", azp=client, oid=oid),
    })
    assert response.status_code == 403
    decoded(response)
    assert not setup[3].calls and not setup[4].calls


@pytest.mark.parametrize("route", ["invoke", "prepare"])
def test_additional_agent_pair_cannot_broaden_requester_or_authoring_access(setup, route):
    multi_identity_settings(setup)
    response = request(setup, route, headers={
        "Authorization": signed(
            setup, route, azp=SECOND_AGENT_CLIENT, oid=SECOND_AGENT_OID,
        ),
    })
    assert response.status_code == 403
    assert not setup[3].calls and not setup[4].calls


def test_additional_agent_pair_still_requires_workload_and_exact_application_role(setup):
    multi_identity_settings(setup)
    for changes in (
        {"idtyp": None, "roles": None, "scp": CONNECTION_USE},
        {"roles": ["Connection.Use"]}, {"roles": [DEPLOYMENT_ADMIN]},
    ):
        response = request(setup, "token", headers={
            "Authorization": signed(
                setup, "token", azp=SECOND_AGENT_CLIENT, oid=SECOND_AGENT_OID, **changes,
            ),
        })
        assert response.status_code == 403
    assert not setup[3].calls and not setup[4].calls


def test_additional_identity_collection_is_immutable_and_canonical(setup):
    additional = [[SECOND_AGENT_CLIENT.upper(), SECOND_AGENT_OID.upper()]]
    settings = replace(setup[1], managed_agent_identities=additional)
    additional[0][0] = OTHER
    assert settings.managed_agent_identities == ((SECOND_AGENT_CLIENT, SECOND_AGENT_OID),)
    assert settings.token_identities == (
        (AGENT_CLIENT, AGENT_OID), (SECOND_AGENT_CLIENT, SECOND_AGENT_OID),
    )
    with pytest.raises(TypeError):
        settings.managed_agent_identities[0][0] = OTHER


@pytest.mark.parametrize("additional", [
    ((AGENT_CLIENT, AGENT_OID),),
    ((SECOND_AGENT_CLIENT, SECOND_AGENT_OID), (SECOND_AGENT_CLIENT.upper(), SECOND_AGENT_OID.upper())),
    (("not-a-client", SECOND_AGENT_OID),), ((SECOND_AGENT_CLIENT, "not-an-oid"),),
    ((SECOND_AGENT_CLIENT,),), ((SECOND_AGENT_CLIENT, SECOND_AGENT_OID, OTHER),),
    ((SECOND_AGENT_CLIENT, None),), SECOND_AGENT_CLIENT, None,
    {"client_id": SECOND_AGENT_CLIENT, "oid": SECOND_AGENT_OID},
])
def test_invalid_or_duplicate_additional_identity_config_is_rejected(setup, additional):
    with pytest.raises(ValueError):
        replace(setup[1], managed_agent_identities=additional)


def settings_env(setup):
    settings = setup[1]
    return {
        "CONTOSO_TENANT_ID": settings.tenant_id, "CONTOSO_GATEWAY_AUDIENCE": settings.audience,
        "CONTOSO_AWS_REQUESTER_CLIENT_ID": settings.aws_requester_client_id,
        "CONTOSO_AWS_REQUESTER_OID": settings.aws_requester_oid,
        "CONTOSO_AUTHORING_CLIENT_ID": settings.authoring_client_id,
        "CONTOSO_AUTHORING_ADMIN_OID": settings.authoring_admin_oid,
        "CONTOSO_MANAGED_AGENT_CLIENT_ID": settings.managed_agent_client_id,
        "CONTOSO_MANAGED_AGENT_OID": settings.managed_agent_oid,
    }


def test_explicit_additional_identity_env_schema_preserves_singleton_support(setup):
    env = settings_env(setup)
    assert GatewaySettings.from_env(env).token_identities == ((AGENT_CLIENT, AGENT_OID),)
    env["CONTOSO_MANAGED_AGENT_IDENTITIES"] = json.dumps([
        {"client_id": SECOND_AGENT_CLIENT, "oid": SECOND_AGENT_OID},
    ])
    settings = GatewaySettings.from_env(env)
    assert settings.token_identities == (
        (AGENT_CLIENT, AGENT_OID), (SECOND_AGENT_CLIENT, SECOND_AGENT_OID),
    )
    env["CONTOSO_MANAGED_AGENT_IDENTITIES"] = "[]"
    assert GatewaySettings.from_env(env) == setup[1]


@pytest.mark.parametrize("raw", [
    "", "null", "{}", "invalid", '[{"client_id":"invalid","oid":"invalid"}]',
    f'[{{"client_id":"{SECOND_AGENT_CLIENT}","oid":"{SECOND_AGENT_OID}","roles":["CONTOSO.Connection.Use"]}}]',
    f'[{{"client_id":"{SECOND_AGENT_CLIENT}","client_id":"{OTHER}","oid":"{SECOND_AGENT_OID}"}}]',
    f'[{{"client_id":"{AGENT_CLIENT}","oid":"{AGENT_OID}"}}]',
    json.dumps([{"client_id": SECOND_AGENT_CLIENT, "oid": SECOND_AGENT_OID}] * 2),
])
def test_invalid_identity_env_json_is_fail_closed(setup, raw):
    env = {**settings_env(setup), "CONTOSO_MANAGED_AGENT_IDENTITIES": raw}
    with pytest.raises(ValueError) as caught:
        GatewaySettings.from_env(env)
    assert raw not in str(caught.value) if raw else True


def test_multi_identity_gateway_real_broker_keeps_provider_oid_allowlists(setup):
    multi_identity_settings(setup)
    contoso_backend = MemoryBlobs()
    store = EncryptedStateStore(
        contoso_backend, base64.urlsafe_b64encode(b"x" * 32).decode(), namespace="multi-agent-tests",
    )
    spec = ProviderConnection(
        "studio-standard", TENANT, NATIVE_CLIENT, GUEST,
        "https://gateway.example.com/api/oauth/callback", STUDIOVariant.STANDARD,
        "https://api.powerplatform.com", {STANDARD_SCOPE},
    )
    broker = DelegatedConnectionBroker(
        store, {spec.provider_id: spec}, {
            AGENT_OID: {spec.provider_id}, SECOND_AGENT_OID: {spec.provider_id},
        }, preparer_providers={ADMIN_OID: {spec.provider_id}}, msal_factory=FakeFactory(),
    )
    setup[0].broker = broker
    start = decoded(request(setup, "prepare"))
    assert request(setup, "callback", payload={"state": start["state"], "code": "private"}).status_code == 200
    for client, oid in setup[0].settings.token_identities:
        response = request(setup, "token", headers={
            "Authorization": signed(setup, "token", azp=client, oid=oid),
        })
        assert response.status_code == 200
    response = request(setup, "token", headers={
        "Authorization": signed(setup, "token", azp=SECOND_AGENT_CLIENT, oid=AGENT_OID),
    })
    assert response.status_code == 403
