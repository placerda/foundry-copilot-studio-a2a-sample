import base64
from dataclasses import replace
import inspect
import json
import socket
from urllib.parse import parse_qs, urlsplit

import msal
import pytest
import requests

from contoso.connections import (
    ConnectionDenied, ConnectionError, ConnectionRequired, ConnectionUnavailable,
    DelegatedConnectionBroker, ProviderConnection,
)
from contoso.connections.oauth import CONNECTION_USE, DEPLOYMENT_ADMIN, HARNESS_SCOPE, STANDARD_SCOPE
from contoso.router.core import (
    AuthMetadata, AuthScheme, Capability, CatalogEntry, STUDIOVariant, Protocol, SourceMetadata,
)
from contoso.router.runtime import ScopedToken
from contoso.state import EncryptedStateStore, StateConflict, StateMissing, StateUnavailable


TENANT = "11111111-1111-4111-8111-111111111111"
CLIENT = "22222222-2222-4222-8222-222222222222"
GUEST = "33333333-3333-4333-8333-333333333333"
CALLER = "44444444-4444-4444-8444-444444444444"
OTHER = "55555555-5555-4555-8555-555555555555"
HOME = f"{OTHER}.66666666-6666-4666-8666-666666666666"
REDIRECT = "https://gateway.example.com/connections/callback"
ROLES = frozenset({CONNECTION_USE})
ADMIN_ROLES = frozenset({DEPLOYMENT_ADMIN})
SECRET = "opaque-sensitive-test-token"


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not use the network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.Session, "request", forbidden)


class MemoryBlobs:
    def __init__(self):
        self.values = {}
        self.sequence = 0
        self.writes = []
        self.before_write = None

    def read(self, name, max_bytes):
        if name not in self.values:
            raise StateMissing("Missing")
        return self.values[name]

    def write(self, name, data, etag):
        self.writes.append((name, etag))
        if self.before_write is not None:
            self.before_write(name, data, etag)
        if ((etag is None and name in self.values)
                or (etag is not None and (name not in self.values or self.values[name][1] != etag))):
            raise StateConflict("Conflict")
        self.sequence += 1
        version = str(self.sequence)
        self.values[name] = (data, version)
        return version

    def delete(self, name, etag):
        if name not in self.values:
            raise StateMissing("Missing")
        if self.values[name][1] != etag:
            raise StateConflict("Conflict")
        del self.values[name]


def cache_document(*, local=GUEST, realm=TENANT, home=HOME, extra=None):
    result = {
        "Account": {
            "guest": {
                "home_account_id": home, "environment": "login.microsoftonline.com",
                "local_account_id": local, "realm": realm, "authority_type": "MSSTS",
                "username": "private-guest@example.com",
            },
        },
        "RefreshToken": {"test": {"secret": "private-refresh-token"}},
    }
    if extra:
        result["Account"]["other-profile"] = extra
    return result


def good_result(scope=STANDARD_SCOPE):
    return {
        "access_token": SECRET, "token_type": "Bearer", "expires_in": 3600,
        "scope": f"{scope} openid profile offline_access",
        "id_token_claims": {"tid": TENANT, "oid": GUEST},
    }


class FakeFactory:
    def __init__(self):
        self.calls = []
        self.apps = []
        self.exchange_result = good_result()
        self.silent_result = good_result()
        self.exchange_exception = None
        self.silent_exception = None
        self.cache_document = cache_document()
        self.account_override = None
        self.on_silent = None
        self.flow_changes = {}
        self.clock = None

    def __call__(self, client_id, **kwargs):
        self.calls.append(("constructor", client_id, kwargs))
        app = FakeApp(self, kwargs["token_cache"])
        self.apps.append(app)
        return app


class FakeApp:
    def __init__(self, factory, cache):
        self.factory = factory
        self.cache = cache

    def initiate_auth_code_flow(self, **kwargs):
        self.factory.calls.append(("begin", kwargs))
        self.flow = {
            "state": kwargs["state"], "auth_uri": "https://login.microsoftonline.com/authorize",
            "redirect_uri": kwargs["redirect_uri"], "scope": kwargs["scopes"],
            "code_verifier": "private-pkce-verifier", "nonce": "private-flow-nonce",
            **self.factory.flow_changes,
        }
        return self.flow

    def acquire_token_by_auth_code_flow(self, flow, response, **kwargs):
        self.factory.calls.append(("exchange", flow, response, kwargs))
        if self.factory.exchange_exception:
            raise self.factory.exchange_exception
        if flow["state"] != response["state"]:
            raise ValueError("State mismatch with private-code")
        self.cache.deserialize(json.dumps(self.factory.cache_document))
        return self.factory.exchange_result

    def get_accounts(self):
        self.factory.calls.append(("accounts",))
        if self.factory.account_override is not None:
            return self.factory.account_override
        grouped = {}
        for a in self.cache.search(msal.TokenCache.CredentialType.ACCOUNT):
            grouped[a["home_account_id"]] = dict(a)
        return list(grouped.values())

    def acquire_token_silent(self, **kwargs):
        self.factory.calls.append(("silent", kwargs))
        if self.factory.silent_exception:
            raise self.factory.silent_exception
        if self.factory.on_silent:
            self.factory.on_silent(self)
        document = json.loads(self.cache.serialize())
        document["RefreshToken"]["test"]["secret"] = f"private-rotated-{len(self.factory.apps)}"
        self.cache.deserialize(json.dumps(document))
        self.cache.has_state_changed = True
        return self.factory.silent_result


@pytest.fixture
def setup():
    specs = {
        "standard": ProviderConnection(
            "standard", TENANT, CLIENT, GUEST, REDIRECT, STUDIOVariant.STANDARD,
            "https://api.powerplatform.com", frozenset({STANDARD_SCOPE}),
        ),
        "harness": ProviderConnection(
            "harness", TENANT, CLIENT, GUEST, REDIRECT, STUDIOVariant.COPILOT_HARNESS,
            "https://api.powerplatform.com/copilotstudio-a2a", frozenset({HARNESS_SCOPE}),
        ),
    }
    now = [1000.0]
    contoso_backend = MemoryBlobs()
    store = EncryptedStateStore(
        contoso_backend, base64.urlsafe_b64encode(b"x" * 32).decode(),
        namespace="broker-tests", clock=lambda: now[0],
    )
    factory = FakeFactory()
    broker = DelegatedConnectionBroker(
        store, specs, {CALLER: frozenset(specs)}, msal_factory=factory, clock=lambda: now[0],
    )
    return broker, factory, store, contoso_backend, now, specs


def begin(setup, provider="standard"):
    broker = setup[0]
    return broker.begin(
        provider, caller_id=CALLER, caller_roles=ROLES, authenticated_guest_oid=GUEST,
    )


def complete(setup, start, provider="standard", **fields):
    return setup[0].complete(
        provider, caller_id=CALLER, caller_roles=ROLES,
        auth_response={"state": start.state, "code": "private-auth-code", **fields},
    )


def connect(setup, provider="standard"):
    return complete(setup, begin(setup, provider), provider)


def token(setup, provider="standard"):
    return setup[0].get_token(provider, caller_id=CALLER, caller_roles=ROLES)


@pytest.mark.parametrize("provider,scope", [("standard", STANDARD_SCOPE), ("harness", HARNESS_SCOPE)])
def test_real_api_calls_and_both_fixed_resources(setup, provider, scope):
    broker, factory, store, _, now, specs = setup
    factory.exchange_result = good_result(scope)
    factory.silent_result = good_result(scope)
    start = begin(setup, provider)
    assert "private" not in repr(start) and start.state not in repr(start)
    assert set(start.__dataclass_fields__) == {"auth_uri", "state"}
    constructor = factory.calls[0]
    assert constructor[1] == CLIENT
    assert constructor[2] == {
        "authority": f"https://login.microsoftonline.com/{TENANT}",
        "token_cache": factory.apps[0].cache, "timeout": (3.05, 10.0),
        "instance_discovery": False, "enable_pii_log": False,
        "enable_broker_on_windows": False, "enable_broker_on_mac": False,
        "enable_broker_on_linux": False, "enable_broker_on_wsl": False,
    }
    assert factory.calls[1] == ("begin", {
        "scopes": [scope], "redirect_uri": REDIRECT, "state": start.state,
        "response_mode": "form_post",
    })
    saved = store.get("oauth-flow", broker._flow_identity(specs[provider], CALLER, start.state))
    assert saved.payload["flow"]["code_verifier"] == "private-pkce-verifier"
    assert complete(setup, start, provider) is None
    exchange = next(c for c in factory.calls if c[0] == "exchange")
    assert exchange[1] == saved.payload["flow"]
    assert exchange[2] == {"state": start.state, "code": "private-auth-code"}
    assert exchange[3] == {"scopes": [scope]}
    result = token(setup, provider)
    assert isinstance(result, ScopedToken)
    assert result.token == SECRET
    assert result.tenant_id == TENANT and result.audience == specs[provider].audience
    assert result.scopes == frozenset({scope}) and result.expires_at == now[0] + 3600
    silent = next(c for c in factory.calls if c[0] == "silent")
    assert silent[1] == {"scopes": [scope], "account": {
        **cache_document()["Account"]["guest"],
    }}
    assert "force_refresh" not in silent[1]


@pytest.mark.parametrize("action", ["begin", "complete", "get_token"])
@pytest.mark.parametrize("caller,roles,provider", [
    (OTHER, ROLES, "standard"), (CALLER, frozenset(), "standard"),
    (CALLER, "CONTOSO.Connection.Use", "standard"), (CALLER, ROLES, "arbitrary-resource"),
    (CALLER, frozenset({"Connection.Use"}), "standard"),
    ("", ROLES, "standard"), (None, ROLES, "standard"),
])
def test_allowlist_and_role_rejection_never_calls_msal(setup, action, caller, roles, provider):
    broker, factory, _, contoso_backend, _, _ = setup
    kwargs = {"caller_id": caller, "caller_roles": roles}
    if action == "begin":
        kwargs["authenticated_guest_oid"] = GUEST
    if action == "complete":
        kwargs["auth_response"] = {"state": "s" * 43, "code": "private"}
    with pytest.raises(ConnectionDenied):
        getattr(broker, action)(provider, **kwargs)
    assert factory.calls == [] and contoso_backend.values == {}


def test_empty_allowlist_and_per_provider_grant_are_closed(setup):
    _, factory, store, _, now, specs = setup
    empty = DelegatedConnectionBroker(
        store, specs, {}, msal_factory=factory, clock=lambda: now[0],
    )
    with pytest.raises(ConnectionDenied):
        empty.get_token("standard", caller_id=CALLER, caller_roles=ROLES)
    limited = DelegatedConnectionBroker(
        store, specs, {CALLER: {"standard"}}, msal_factory=factory,
    )
    with pytest.raises(ConnectionDenied):
        limited.get_token("harness", caller_id=CALLER, caller_roles=ROLES)
    assert not factory.calls


@pytest.mark.parametrize("spoof", [OTHER, "private-guest@example.com", None])
def test_begin_requires_selected_guest_without_admin_or_email_substitution(setup, spoof):
    broker, factory, _, contoso_backend, _, _ = setup
    with pytest.raises(ConnectionDenied):
        broker.begin("standard", caller_id=CALLER, caller_roles=ROLES, authenticated_guest_oid=spoof)
    assert not factory.calls and not contoso_backend.values


@pytest.mark.parametrize("changed", [
    {"tenant_id": "common"}, {"client_id": ""}, {"guest_oid": "admin"},
    {"scopes": {STANDARD_SCOPE, HARNESS_SCOPE}}, {"scopes": {"User.Read"}},
    {"scopes": set()}, {"scopes": STANDARD_SCOPE}, {"variant": "standard"},
    {"redirect_uri": "http://gateway.example.com/callback"},
    {"redirect_uri": "https://localhost/callback"}, {"redirect_uri": "https://127.0.0.1/callback"},
    {"redirect_uri": "https://gateway.example.com/callback?code=secret"},
    {"redirect_uri": "https://gateway.example.com/callback#secret"},
    {"redirect_uri": "https://user:secret@gateway.example.com/callback"},
])
def test_configuration_is_parameterized_and_bounded(setup, changed):
    with pytest.raises(ConnectionError):
        replace(setup[-1]["standard"], **changed)


def test_catalog_configuration_and_immutable_copies(setup):
    broker, factory, store, _, _, specs = setup
    source = SourceMetadata("s", "standard", "1", "Demo", "public", "r", {"section"})
    entry = CatalogEntry(
        "standard", Capability.SUPPLIER_DELIVERY_TERMS, STUDIOVariant.STANDARD, REDIRECT,
        Protocol.SDK_D2E, AuthMetadata(AuthScheme.ENTRA_BEARER, TENANT,
                                    "https://api.powerplatform.com", {STANDARD_SCOPE}), (source,),
    )
    assert ProviderConnection.from_catalog(
        entry, client_id=CLIENT, guest_oid=GUEST, redirect_uri=REDIRECT,
    ) == specs["standard"]
    with pytest.raises(ConnectionError):
        ProviderConnection.from_catalog(
            replace(entry, capability=Capability.KNOWLEDGE_KNOWLEDGE, variant=None),
            client_id=CLIENT, guest_oid=GUEST, redirect_uri=REDIRECT,
        )
    specs.clear()
    assert begin(setup).auth_uri.startswith("https://")
    with pytest.raises(TypeError):
        broker._connections["new"] = object()
    for action in (broker.begin, broker.complete, broker.get_token):
        assert "scopes" not in inspect.signature(action).parameters
        assert "tenant_id" not in inspect.signature(action).parameters
        assert "audience" not in inspect.signature(action).parameters
    with pytest.raises(TypeError):
        broker.get_token("standard", caller_id=CALLER, caller_roles=ROLES, scopes=["User.Read"])


@pytest.mark.parametrize("claims", [
    {"tid": OTHER, "oid": GUEST}, {"tid": TENANT, "oid": OTHER},
    {"tid": TENANT, "email": "private-guest@example.com"}, {},
    {"tid": None, "oid": GUEST}, {"tid": TENANT, "oid": None}, None,
])
def test_validated_claims_must_prove_tenant_and_guest(setup, claims):
    setup[1].exchange_result["id_token_claims"] = claims
    start = begin(setup)
    with pytest.raises(ConnectionRequired):
        complete(setup, start)
    assert not any("oauth-cache" in name for name in setup[3].values)
    with pytest.raises(ConnectionRequired):
        complete(setup, start)


def test_nonce_sdk_rejection_is_safe_and_flow_is_consumed_before_exchange(setup):
    _, factory, store, _, _, specs = setup
    start = begin(setup)
    factory.exchange_exception = RuntimeError("nonce invalid: private-flow-nonce private-id-token")
    with pytest.raises(ConnectionRequired) as caught:
        complete(setup, start)
    assert "private" not in str(caught.value) + repr(caught.value)
    assert caught.value.__suppress_context__
    with pytest.raises(StateMissing):
        store.get("oauth-flow", setup[0]._flow_identity(specs["standard"], CALLER, start.state))
    count = len(factory.calls)
    with pytest.raises(ConnectionRequired):
        complete(setup, start)
    assert len(factory.calls) == count


def test_replay_expiry_and_cross_provider_state_never_exchange(setup):
    _, factory, _, _, now, _ = setup
    start = begin(setup)
    with pytest.raises(ConnectionRequired):
        complete(setup, start, "harness")
    assert not any(c[0] == "exchange" for c in factory.calls)
    complete(setup, start)
    count = len(factory.calls)
    with pytest.raises(ConnectionRequired):
        complete(setup, start)
    assert len(factory.calls) == count
    stale = begin(setup)
    now[0] += 600
    count = len(factory.calls)
    with pytest.raises(ConnectionRequired):
        complete(setup, stale)
    assert len(factory.calls) == count


def test_callback_does_not_take_resource_identity_or_raw_errors_from_form(setup):
    start = begin(setup)
    complete(
        setup, start, scope="User.Read", tenant=OTHER, audience="https://graph.microsoft.com",
        oid=OTHER, error_description="private-description",
    )
    exchange = next(c for c in setup[1].calls if c[0] == "exchange")
    assert exchange[2] == {"state": start.state, "code": "private-auth-code"}
    rejected = begin(setup)
    count = len(setup[1].calls)
    with pytest.raises(ConnectionRequired) as caught:
        complete(setup, rejected, error="access_denied", error_description="private-description")
    assert "private" not in repr(caught.value) and len(setup[1].calls) == count


@pytest.mark.parametrize("value", [None, "query-state", "", "s" * 44])
def test_invalid_response_state_is_safe_without_msal(setup, value):
    with pytest.raises(ConnectionRequired):
        setup[0].complete("standard", caller_id=CALLER, caller_roles=ROLES,
                          auth_response={"state": value, "code": "private"})
    assert not setup[1].calls


@pytest.mark.parametrize("provider,metadata", [
    ("standard", "CopilotStudio.Copilots.Invoke openid"),
    ("harness", "MCS.InvokeAsA2A profile"),
    ("standard", None), ("harness", None),
])
def test_full_or_resource_local_grants_and_absent_cache_hit_metadata(setup, provider, metadata):
    scope = next(iter(setup[-1][provider].scopes))
    setup[1].exchange_result = good_result(scope)
    setup[1].silent_result = good_result(scope)
    if metadata is None:
        setup[1].exchange_result.pop("scope")
        setup[1].silent_result.pop("scope")
        setup[1].silent_result.pop("id_token_claims")
    else:
        setup[1].exchange_result["scope"] = metadata
        setup[1].silent_result["scope"] = metadata
    connect(setup, provider)
    assert token(setup, provider).scopes == frozenset({scope})


@pytest.mark.parametrize("scope", [
    HARNESS_SCOPE, "https://other.example/CopilotStudio.Copilots.Invoke",
    "User.Read", "", ["CopilotStudio.Copilots.Invoke"], None,
])
def test_wrong_or_invalid_granted_scope_metadata_is_rejected(setup, scope):
    setup[1].exchange_result["scope"] = scope
    with pytest.raises(ConnectionRequired):
        connect(setup)


@pytest.mark.parametrize("changed", [
    {"error": "invalid_grant", "error_description": "private-description"},
    {"token_type": "PoP"}, {"token_type": None}, {"access_token": None},
    {"access_token": "private token"}, {"access_token": "x" * 16385},
    {"expires_in": None}, {"expires_in": False}, {"expires_in": "nan"},
    {"expires_in": float("inf")}, {"expires_in": 60}, {"expires_in": -1},
    {"expires_on": 999}, {"expires_on": "NaN"},
])
def test_safe_token_errors_never_persist_or_dump_results(setup, changed):
    setup[1].exchange_result.update(changed)
    with pytest.raises(ConnectionRequired) as caught:
        connect(setup)
    assert "private" not in str(caught.value) + repr(caught.value)
    assert not any("oauth-cache" in name for name in setup[3].values)


def test_missing_and_revoked_connection_has_no_fallback(setup):
    with pytest.raises(ConnectionRequired):
        token(setup)
    assert not setup[1].calls
    connect(setup)
    for result in (None, {}, {"error": "invalid_grant", "error_description": "private-details"}):
        setup[1].silent_result = result
        with pytest.raises(ConnectionRequired) as caught:
            token(setup)
        assert "private" not in repr(caught.value)
    assert all(c[1]["scopes"] == [STANDARD_SCOPE] for c in setup[1].calls if c[0] == "silent")


def test_b2b_home_oid_is_not_guest_oid_and_grouped_top_level_is_not_trusted(setup):
    home_profile = {
        **cache_document()["Account"]["guest"],
        "realm": OTHER, "local_account_id": OTHER,
    }
    setup[1].cache_document = cache_document(extra=home_profile)
    connect(setup)
    assert token(setup).token == SECRET
    account = next(c[1]["account"] for c in setup[1].calls if c[0] == "silent")
    assert account["realm"] == OTHER and account["local_account_id"] == OTHER
    assert account["home_account_id"] == HOME


@pytest.mark.parametrize("document", [
    cache_document(local=OTHER), cache_document(realm=OTHER),
    {"Account": {}, "RefreshToken": {}},
])
def test_cached_local_guest_or_tenant_mismatch_is_rejected(setup, document):
    setup[1].cache_document = document
    with pytest.raises(ConnectionRequired):
        connect(setup)


def overwrite_payload(setup, mutate):
    _, _, store, _, _, specs = setup
    record = store.get("oauth-cache", specs["standard"].binding)
    mutate(record.payload)
    store.put("oauth-cache", specs["standard"].binding, record.payload, ttl=600, etag=record.etag)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(tenant_id=OTHER), lambda p: p.update(guest_oid=OTHER),
    lambda p: p.update(binding="other"), lambda p: p.update(cache="private-invalid-json"),
    lambda p: p.update(cache="[]"), lambda p: p.update(account_key={}),
    lambda p: p["account_key"].update(home_account_id=OTHER),
    lambda p: p["account_key"].update(environment="other.example.com"),
])
def test_stale_cache_and_saved_account_mismatch_reject_before_silent(setup, mutate):
    connect(setup)
    overwrite_payload(setup, mutate)
    with pytest.raises(ConnectionRequired):
        token(setup)
    assert not any(c[0] == "silent" for c in setup[1].calls)


def test_guest_profile_removed_after_refresh_is_not_saved(setup):
    connect(setup)
    def remove_guest(app):
        app.cache.deserialize(json.dumps(cache_document(local=OTHER)))
    setup[1].on_silent = remove_guest
    with pytest.raises(ConnectionRequired):
        token(setup)


def test_cache_and_token_reprs_and_encrypted_storage_never_reveal_secrets(setup, caplog):
    start = begin(setup)
    complete(setup, start)
    result = token(setup)
    record = setup[2].get("oauth-cache", setup[-1]["standard"].binding)
    assert "private" not in repr(record) + repr(result) + repr(start)
    assert SECRET not in repr(result)
    for name, (data, _) in setup[3].values.items():
        for secret in (GUEST, HOME, SECRET, "private-refresh-token", "private-pkce-verifier"):
            assert secret not in name and secret.encode() not in data
    assert caplog.text == ""
    assert record.payload["tenant_id"] == TENANT and record.payload["guest_oid"] == GUEST
    assert record.payload["account_key"]["home_account_id"] == HOME
    assert "private-rotated" in record.payload["cache"]


def test_refresh_updates_with_previous_etag_and_private_cache_instances(setup):
    connect(setup)
    store, spec = setup[2], setup[-1]["standard"]
    before = store.get("oauth-cache", spec.binding)
    result = token(setup)
    after = store.get("oauth-cache", spec.binding)
    assert after.etag != before.etag
    assert "private-rotated" in after.payload["cache"]
    assert setup[3].writes[-1][1] == before.etag
    assert result.token == SECRET
    assert setup[1].apps[-1].cache is not setup[1].apps[-2].cache


def test_refresh_conflict_reloads_and_reacquires_once_without_lost_updates(setup):
    connect(setup)
    _, factory, store, contoso_backend, _, specs = setup
    spec = specs["standard"]
    original = store.get("oauth-cache", spec.binding)
    name = store._binding("oauth-cache", spec.binding)[0]
    concurrent_versions = []
    def conflict_once(target, data, etag):
        if target != name:
            return
        contoso_backend.before_write = None
        current = store.get("oauth-cache", spec.binding)
        document = json.loads(current.payload["cache"])
        document["concurrent-data"] = "preserve-me"
        current.payload["cache"] = json.dumps(document)
        saved = store.put("oauth-cache", spec.binding, current.payload, ttl=600, etag=current.etag)
        concurrent_versions.append(saved.etag)
    contoso_backend.before_write = conflict_once
    assert token(setup).token == SECRET
    assert len([c for c in factory.calls if c[0] == "silent"]) == 2
    assert "preserve-me" in store.get("oauth-cache", spec.binding).payload["cache"]
    etags = [etag for target, etag in contoso_backend.writes if target == name]
    assert etags[-3:] == [original.etag, original.etag, concurrent_versions[0]]
    assert factory.apps[-1].cache is not factory.apps[-2].cache


def test_second_refresh_conflict_is_safe_and_bounded(setup):
    connect(setup)
    def conflict(*args):
        raise StateConflict("private-conflict")
    setup[3].before_write = conflict
    with pytest.raises(ConnectionUnavailable) as caught:
        token(setup)
    assert len([c for c in setup[1].calls if c[0] == "silent"]) == 2
    assert "private" not in repr(caught.value)


def test_connection_deleted_during_refresh_cannot_return_token(setup):
    connect(setup)
    def delete(target, data, etag):
        setup[3].before_write = None
        setup[3].delete(target, etag)
    setup[3].before_write = delete
    with pytest.raises(ConnectionRequired):
        token(setup)
    assert len([c for c in setup[1].calls if c[0] == "silent"]) == 1


def test_expired_connection_does_not_create_msal(setup):
    connect(setup)
    count = len(setup[1].calls)
    setup[4][0] += 86400
    with pytest.raises(ConnectionRequired):
        token(setup)
    assert len(setup[1].calls) == count


def test_authorization_cache_update_is_etag_pinned_to_begin(setup):
    connect(setup)
    start = begin(setup)
    token(setup)
    record = setup[2].get("oauth-cache", setup[-1]["standard"].binding)
    with pytest.raises(ConnectionRequired, match="changed during authorization"):
        complete(setup, start)
    assert setup[2].get("oauth-cache", setup[-1]["standard"].binding) == record


def test_expiry_is_conservative_and_budget_includes_exchange_time(setup):
    connect(setup)
    setup[1].silent_result["expires_in"] = 65
    setup[1].on_silent = lambda app: setup[4].__setitem__(0, setup[4][0] + 10)
    with pytest.raises(ConnectionRequired, match="lifetime"):
        token(setup)
    setup[1].on_silent = None
    setup[1].silent_result["expires_in"] = "3600"
    setup[1].silent_result["expires_on"] = setup[4][0] + 1200
    assert token(setup).expires_at == setup[4][0] + 1200


@pytest.mark.parametrize("stage", ["constructor", "exchange", "silent", "storage"])
def test_network_and_storage_failures_are_redacted(setup, stage):
    if stage == "silent":
        connect(setup)
        setup[1].silent_exception = requests.Timeout("private-token timeout")
        action = lambda: token(setup)
    elif stage == "exchange":
        start = begin(setup)
        setup[1].exchange_exception = requests.Timeout("private-auth-code timeout")
        action = lambda: complete(setup, start)
    elif stage == "storage":
        def unavailable(*args):
            raise StateUnavailable("private-storage error")
        setup[3].read = unavailable
        action = lambda: begin(setup)
    else:
        def unavailable(*args, **kwargs):
            raise requests.Timeout("private-discovery error")
        setup[0]._factory = unavailable
        action = lambda: begin(setup)
    with pytest.raises(ConnectionUnavailable) as caught:
        action()
    assert "private" not in str(caught.value) + repr(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("kwargs", [
    {"flow_ttl": 601}, {"cache_ttl": 2592001}, {"minimum_token_ttl": 0},
    {"network_timeout": (1, 31)}, {"network_timeout": (float("nan"), 5)},
    {"network_timeout": None},
])
def test_timeout_and_lifetime_configuration_is_bounded(setup, kwargs):
    with pytest.raises(ConnectionError):
        DelegatedConnectionBroker(setup[2], setup[-1], {CALLER: {"standard"}}, **kwargs)


class OfflineDiscovery:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        assert url == f"https://login.microsoftonline.com/{TENANT}/v2.0/.well-known/openid-configuration"
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({
            "authorization_endpoint": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize",
            "token_endpoint": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
            "issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        }).encode()
        return response

    def post(self, *args, **kwargs):
        raise AssertionError("Smoke check must not exchange a real code")


def test_installed_msal_constructor_signatures_pkce_and_state_without_network(setup):
    assert msal.__version__ == "1.35.1"
    assert "client_id" in inspect.signature(msal.PublicClientApplication).parameters
    assert "response_mode" in inspect.signature(msal.PublicClientApplication.initiate_auth_code_flow).parameters
    assert list(inspect.signature(msal.PublicClientApplication.acquire_token_by_auth_code_flow).parameters) == [
        "self", "auth_code_flow", "auth_response", "scopes", "kwargs",
    ]
    assert "account" in inspect.signature(msal.PublicClientApplication.acquire_token_silent).parameters
    offline = OfflineDiscovery()
    def actual_factory(client_id, **kwargs):
        return msal.PublicClientApplication(client_id, http_client=offline, **kwargs)
    setup[0]._factory = actual_factory
    start = begin(setup)
    query = parse_qs(urlsplit(start.auth_uri).query)
    assert query["client_id"] == [CLIENT]
    assert query["redirect_uri"] == [REDIRECT]
    assert query["response_mode"] == ["form_post"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [start.state]
    assert {STANDARD_SCOPE, "openid", "profile", "offline_access"}.issubset(query["scope"][0].split())
    assert len(offline.calls) == 1
    record = setup[2].get(
        "oauth-flow", setup[0]._flow_identity(setup[-1]["standard"], CALLER, start.state),
    )
    app = actual_factory(CLIENT, authority=f"https://login.microsoftonline.com/{TENANT}",
                         token_cache=msal.SerializableTokenCache(), instance_discovery=False)
    with pytest.raises(ValueError, match="state"):
        app.acquire_token_by_auth_code_flow(
            record.payload["flow"], {"state": "wrong", "code": "fake-code"}, scopes=[STANDARD_SCOPE],
        )


def test_real_sdk_nonce_enforcement_is_not_bypassed(setup, monkeypatch):
    from msal.oauth2cli import oauth2
    offline = OfflineDiscovery()
    setup[0]._factory = lambda client_id, **kwargs: msal.PublicClientApplication(
        client_id, http_client=offline, **kwargs,
    )
    start = begin(setup)
    # Replace only the lower OAuth2 exchange, retaining MSAL's actual OIDC nonce
    # check and public acquire_token_by_auth_code_flow implementation.
    monkeypatch.setattr(
        oauth2.Client, "obtain_token_by_auth_code_flow",
        lambda *args, **kwargs: {**good_result(), "id_token_claims": {
            "tid": TENANT, "oid": GUEST, "nonce": "private-mismatched-nonce",
        }},
    )
    with pytest.raises(ConnectionRequired) as caught:
        complete(setup, start)
    assert "private" not in repr(caught.value)
    with pytest.raises(ConnectionRequired):
        complete(setup, start)


@pytest.fixture
def operator_setup(setup):
    _, factory, store, contoso_backend, now, specs = setup
    broker = DelegatedConnectionBroker(
        store, specs, {CALLER: frozenset(specs)},
        preparer_providers={OTHER: frozenset(specs)},
        msal_factory=factory, clock=lambda: now[0],
    )
    return broker, factory, store, contoso_backend, now, specs


def prepare(setup, provider="standard"):
    return setup[0].prepare_flow(provider, preparer_id=OTHER, preparer_roles=ADMIN_ROLES)


def callback(setup, start, **fields):
    return setup[0].complete_callback(
        auth_response={"state": start.state, "code": "private-auth-code", **fields},
    )


@pytest.mark.parametrize("identity,roles,provider", [
    (CALLER, ADMIN_ROLES, "standard"), (OTHER, ROLES, "standard"),
    (OTHER, {"DeploymentAdmin"}, "standard"), (OTHER, set(), "standard"),
    (OTHER, "CONTOSO.DeploymentAdmin", "standard"), (OTHER, ADMIN_ROLES, "unknown"),
    (None, ADMIN_ROLES, "standard"),
])
def test_operator_initiation_requires_exact_role_and_explicit_mapping(
    operator_setup, identity, roles, provider,
):
    with pytest.raises(ConnectionDenied):
        operator_setup[0].prepare_flow(provider, preparer_id=identity, preparer_roles=roles)
    assert not operator_setup[1].calls and not operator_setup[3].values


def test_operator_preparation_defaults_disabled_and_copies_per_provider_grants(setup):
    with pytest.raises(ConnectionDenied):
        prepare(setup)
    assert not setup[1].calls
    preparers = {OTHER: {"standard"}}
    broker = DelegatedConnectionBroker(
        setup[2], setup[-1], {CALLER: {"standard"}},
        preparer_providers=preparers, msal_factory=setup[1],
    )
    preparers[OTHER].clear()
    assert broker.prepare_flow("standard", preparer_id=OTHER, preparer_roles=ADMIN_ROLES)
    with pytest.raises(ConnectionDenied):
        broker.prepare_flow("harness", preparer_id=OTHER, preparer_roles=ADMIN_ROLES)
    with pytest.raises(TypeError):
        broker._preparers[OTHER] = {"harness"}


@pytest.mark.parametrize("mapping", [
    {OTHER: {"unknown"}}, {OTHER: STANDARD_SCOPE}, {"admin@example.com": {"standard"}}, [],
])
def test_invalid_preparer_configuration_is_rejected(setup, mapping):
    with pytest.raises(ConnectionError):
        DelegatedConnectionBroker(
            setup[2], setup[-1], {CALLER: {"standard"}}, preparer_providers=mapping,
        )


@pytest.mark.parametrize("provider,scope", [("standard", STANDARD_SCOPE), ("harness", HARNESS_SCOPE)])
def test_operator_flow_anonymous_callback_and_service_token_vending(operator_setup, provider, scope):
    broker, factory, store, contoso_backend, _, specs = operator_setup
    factory.exchange_result = good_result(scope)
    factory.silent_result = good_result(scope)
    start = prepare(operator_setup, provider)
    index = store.get("oauth-flow", "callback:" + start.state).payload
    assert index == {
        "provider_id": provider, "initiator_id": OTHER, "initiation_kind": "operator",
        "binding": specs[provider].binding, "state": start.state,
    }
    flow_identity = broker._flow_identity(specs[provider], OTHER, start.state)
    flow = store.get("oauth-flow", flow_identity).payload
    assert flow["initiator_id"] == OTHER and flow["initiation_kind"] == "operator"
    assert "authenticated_guest_oid" not in flow and "guest_oid" not in flow
    call = next(c for c in factory.calls if c[0] == "begin")
    assert call[1] == {
        "scopes": [scope], "redirect_uri": REDIRECT, "state": start.state,
        "response_mode": "form_post",
    }
    assert callback(operator_setup, start) is None
    for identity in ("callback:" + start.state, flow_identity):
        with pytest.raises(StateMissing):
            store.get("oauth-flow", identity)
    assert token(operator_setup, provider).token == SECRET
    for name, (encrypted, _) in contoso_backend.values.items():
        assert OTHER not in name and OTHER.encode() not in encrypted


def test_callback_body_cannot_supply_provider_operator_roles_or_guest(operator_setup):
    start = prepare(operator_setup)
    callback(
        operator_setup, start, provider_id="harness", preparer_id=CALLER, caller_id=CALLER,
        preparer_roles="CONTOSO.DeploymentAdmin", caller_roles="CONTOSO.Connection.Use",
        tenant_id="other", guest_oid=OTHER, audience="https://graph.microsoft.com", scope="User.Read",
    )
    exchange = next(c for c in operator_setup[1].calls if c[0] == "exchange")
    assert exchange[2] == {"state": start.state, "code": "private-auth-code"}
    assert exchange[3] == {"scopes": [STANDARD_SCOPE]}
    assert operator_setup[2].get("oauth-cache", operator_setup[-1]["standard"].binding)
    with pytest.raises(StateMissing):
        operator_setup[2].get("oauth-cache", operator_setup[-1]["harness"].binding)


@pytest.mark.parametrize("claims", [
    {"tid": TENANT, "oid": OTHER}, {"tid": OTHER, "oid": GUEST},
    {"tid": TENANT, "email": "private-guest@example.com"}, None,
])
def test_operator_is_not_the_guest_and_cannot_token_vend(operator_setup, claims):
    operator_setup[1].exchange_result["id_token_claims"] = claims
    start = prepare(operator_setup)
    with pytest.raises(ConnectionRequired):
        callback(operator_setup, start)
    assert not any("oauth-cache" in name for name in operator_setup[3].values)
    count = len(operator_setup[1].calls)
    with pytest.raises(ConnectionDenied):
        operator_setup[0].get_token("standard", caller_id=OTHER, caller_roles=ADMIN_ROLES)
    with pytest.raises(ConnectionDenied):
        operator_setup[0].get_token("standard", caller_id=OTHER, caller_roles=ROLES | ADMIN_ROLES)
    assert len(operator_setup[1].calls) == count
    with pytest.raises(ConnectionRequired):
        callback(operator_setup, start)


def test_service_only_methods_accept_new_role_not_operator_role(operator_setup):
    for method in ("begin", "complete", "get_token"):
        kwargs = {"caller_id": CALLER, "caller_roles": ADMIN_ROLES}
        if method == "begin":
            kwargs["authenticated_guest_oid"] = GUEST
        if method == "complete":
            kwargs["auth_response"] = {"state": "s" * 43, "code": "private"}
        with pytest.raises(ConnectionDenied):
            getattr(operator_setup[0], method)("standard", **kwargs)
    assert not operator_setup[1].calls
    connect(operator_setup)
    assert token(operator_setup).token == SECRET


def test_anonymous_callback_support_is_scoped_to_operator_flows(operator_setup):
    start = begin(operator_setup)
    count = len(operator_setup[1].calls)
    with pytest.raises(ConnectionRequired):
        callback(operator_setup, start)
    assert len(operator_setup[1].calls) == count
    complete(operator_setup, start)


@pytest.mark.parametrize("target", ["index", "flow"])
@pytest.mark.parametrize("failure", ["missing", "tamper", "expired", "etag-race"])
def test_callback_index_or_flow_storage_failure_never_calls_msal(
    operator_setup, target, failure,
):
    broker, factory, store, contoso_backend, now, specs = operator_setup
    start = prepare(operator_setup)
    identity = ("callback:" + start.state if target == "index"
                else broker._flow_identity(specs["standard"], OTHER, start.state))
    name = store._binding("oauth-flow", identity)[0]
    data, etag = contoso_backend.values[name]
    if failure == "missing":
        contoso_backend.delete(name, etag)
    elif failure == "tamper":
        contoso_backend.values[name] = (data[:-1] + bytes([data[-1] ^ 1]), etag)
    elif failure == "expired":
        payload = store.get("oauth-flow", identity).payload
        store.put("oauth-flow", identity, payload, ttl=1, etag=etag)
        now[0] += 2
    else:
        original_delete = contoso_backend.delete
        def race_delete(deleted_name, deleted_etag):
            if deleted_name == name:
                current = contoso_backend.values[name]
                contoso_backend.values[name] = (current[0], "concurrent")
            original_delete(deleted_name, deleted_etag)
        contoso_backend.delete = race_delete
    count = len(factory.calls)
    with pytest.raises(ConnectionError) as caught:
        callback(operator_setup, start)
    assert "private" not in repr(caught.value)
    assert len(factory.calls) == count


def test_callback_race_only_one_exchange_can_occur(operator_setup):
    _, factory, _, contoso_backend, _, _ = operator_setup
    start = prepare(operator_setup)
    original_delete = contoso_backend.delete
    nested_errors = []
    def competing_delete(name, etag):
        original_delete(name, etag)
        if "/oauth-flow/" in name:
            with pytest.raises(ConnectionRequired) as caught:
                callback(operator_setup, start)
            nested_errors.append(caught.value)
    contoso_backend.delete = competing_delete
    callback(operator_setup, start)
    assert len(nested_errors) == 2
    assert len([c for c in factory.calls if c[0] == "exchange"]) == 1
    count = len(factory.calls)
    with pytest.raises(ConnectionRequired):
        callback(operator_setup, start)
    assert len(factory.calls) == count


def test_callback_rechecks_preparer_permissions_and_changed_configuration(operator_setup):
    _, factory, store, _, now, specs = operator_setup
    start = prepare(operator_setup)
    revoked = DelegatedConnectionBroker(
        store, specs, {CALLER: {"standard"}}, preparer_providers={},
        msal_factory=factory, clock=lambda: now[0],
    )
    count = len(factory.calls)
    with pytest.raises(ConnectionDenied):
        revoked.complete_callback(auth_response={"state": start.state, "code": "private"})
    assert len(factory.calls) == count
    start = prepare(operator_setup)
    changed_specs = {**specs, "standard": replace(specs["standard"], client_id=OTHER)}
    changed = DelegatedConnectionBroker(
        store, changed_specs, {CALLER: {"standard"}}, preparer_providers={OTHER: {"standard"}},
        msal_factory=factory, clock=lambda: now[0],
    )
    count = len(factory.calls)
    with pytest.raises(ConnectionRequired, match="configuration"):
        changed.complete_callback(auth_response={"state": start.state, "code": "private"})
    assert len(factory.calls) == count


@pytest.mark.parametrize("field,value", [
    ("state", "other-state"), ("provider_id", "harness"), ("initiator_id", CALLER),
    ("initiation_kind", "service"), ("binding", "other-binding"),
])
def test_index_payload_substitution_cannot_authorize_another_flow(operator_setup, field, value):
    _, factory, store, _, _, _ = operator_setup
    start = prepare(operator_setup)
    identity = "callback:" + start.state
    record = store.get("oauth-flow", identity)
    record.payload[field] = value
    store.put("oauth-flow", identity, record.payload, ttl=600, etag=record.etag)
    count = len(factory.calls)
    with pytest.raises(ConnectionError):
        callback(operator_setup, start)
    assert len(factory.calls) == count


def test_ciphertext_index_rebinding_does_not_authorize_new_state(operator_setup):
    _, factory, store, contoso_backend, _, _ = operator_setup
    start = prepare(operator_setup)
    alternate = "s" * 43
    source = store._binding("oauth-flow", "callback:" + start.state)[0]
    destination = store._binding("oauth-flow", "callback:" + alternate)[0]
    contoso_backend.values[destination] = contoso_backend.values[source]
    count = len(factory.calls)
    with pytest.raises(ConnectionUnavailable):
        operator_setup[0].complete_callback(auth_response={"state": alternate, "code": "private"})
    assert len(factory.calls) == count
    callback(operator_setup, start)


def test_operator_callback_nonce_failure_burns_both_records(operator_setup):
    _, factory, store, _, _, specs = operator_setup
    start = prepare(operator_setup)
    factory.exchange_exception = RuntimeError("private-nonce and private-token mismatch")
    with pytest.raises(ConnectionRequired) as caught:
        callback(operator_setup, start)
    assert "private" not in repr(caught.value)
    for identity in (
        "callback:" + start.state,
        operator_setup[0]._flow_identity(specs["standard"], OTHER, start.state),
    ):
        with pytest.raises(StateMissing):
            store.get("oauth-flow", identity)
    count = len(factory.calls)
    with pytest.raises(ConnectionRequired):
        callback(operator_setup, start)
    assert len(factory.calls) == count


def test_operator_public_signatures_have_no_caller_or_guest_claim_on_callback(operator_setup):
    broker = operator_setup[0]
    assert list(inspect.signature(broker.prepare_flow).parameters) == [
        "provider_id", "preparer_id", "preparer_roles",
    ]
    assert list(inspect.signature(broker.complete_callback).parameters) == ["auth_response"]
    assert inspect.signature(DelegatedConnectionBroker).parameters["preparer_providers"].default is None
    assert CONNECTION_USE == "CONTOSO.Connection.Use"
    assert DEPLOYMENT_ADMIN == "CONTOSO.DeploymentAdmin"


@pytest.mark.parametrize("provider,scope", [("standard", STANDARD_SCOPE), ("harness", HARNESS_SCOPE)])
def test_operator_status_reads_stored_b2b_proof_without_msal_or_writes(operator_setup, provider, scope):
    broker, factory, _, contoso_backend, _, _ = operator_setup
    with pytest.raises(ConnectionRequired):
        broker.connection_status(provider, preparer_id=OTHER, preparer_roles=ADMIN_ROLES)
    assert not factory.calls
    factory.exchange_result = good_result(scope)
    callback(operator_setup, prepare(operator_setup, provider))
    calls, writes = len(factory.calls), len(contoso_backend.writes)
    assert broker.connection_status(
        provider, preparer_id=OTHER, preparer_roles=ADMIN_ROLES,
    ) == "stored"
    assert len(factory.calls) == calls and len(contoso_backend.writes) == writes


@pytest.mark.parametrize("identity,roles,provider", [
    (CALLER, ADMIN_ROLES, "standard"), (OTHER, ROLES, "standard"),
    (OTHER, set(), "standard"), (OTHER, ADMIN_ROLES, "unknown"),
    (None, ADMIN_ROLES, "standard"),
])
def test_operator_status_denies_unapproved_callers_without_access(operator_setup, identity, roles, provider):
    with pytest.raises(ConnectionDenied):
        operator_setup[0].connection_status(provider, preparer_id=identity, preparer_roles=roles)
    assert not operator_setup[1].calls and not operator_setup[3].writes


@pytest.mark.parametrize("change", ["tenant", "guest", "account", "profile", "cache", "expired"])
def test_operator_status_rejects_stale_or_mismatched_stored_proof(operator_setup, change):
    broker, factory, store, contoso_backend, now, specs = operator_setup
    callback(operator_setup, prepare(operator_setup))
    record = store.get("oauth-cache", specs["standard"].binding)
    payload = record.payload
    if change == "tenant":
        payload["tenant_id"] = OTHER
    elif change == "guest":
        payload["guest_oid"] = OTHER
    elif change == "account":
        payload["account_key"]["home_account_id"] = "different-home"
    elif change == "profile":
        payload["cache"] = json.dumps(cache_document(local=OTHER))
    elif change == "cache":
        payload["cache"] = json.dumps({"Account": []})
    else:
        now[0] += 86401
    if change != "expired":
        store.put("oauth-cache", specs["standard"].binding, payload, ttl=86400, etag=record.etag)
    calls, writes = len(factory.calls), len(contoso_backend.writes)
    with pytest.raises(ConnectionRequired):
        broker.connection_status("standard", preparer_id=OTHER, preparer_roles=ADMIN_ROLES)
    assert len(factory.calls) == calls and len(contoso_backend.writes) == writes


@pytest.mark.parametrize("existing_guest", [False, True])
def test_admin_callback_cannot_create_or_replace_selected_guest_cache(operator_setup, existing_guest):
    broker, factory, store, _, _, specs = operator_setup
    binding = specs["standard"].binding
    if existing_guest:
        callback(operator_setup, prepare(operator_setup))
        previous = store.get("oauth-cache", binding)
    start = prepare(operator_setup)
    factory.exchange_result["id_token_claims"] = {"tid": TENANT, "oid": OTHER}
    with pytest.raises(ConnectionRequired):
        callback(operator_setup, start)
    if existing_guest:
        current = store.get("oauth-cache", binding)
        assert current.etag == previous.etag and current.payload == previous.payload
    else:
        with pytest.raises(StateMissing):
            store.get("oauth-cache", binding)
    with pytest.raises(StateMissing):
        store.get("oauth-flow", "callback:" + start.state)
    with pytest.raises(StateMissing):
        store.get("oauth-flow", broker._flow_identity(specs["standard"], OTHER, start.state))
