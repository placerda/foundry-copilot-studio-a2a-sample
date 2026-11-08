import time
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest

from contoso_backend.errors import ChatFailure
from contoso.identity import EntraAuthenticator, IdentitySettings


TENANT = "00000000-0000-4000-8000-000000000001"
AUDIENCE = "00000000-0000-4000-8000-000000000002"
CLIENT = "00000000-0000-4000-8000-000000000003"
CALLER = "00000000-0000-4000-8000-000000000004"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(signing_key):
    settings = IdentitySettings(TENANT, AUDIENCE)
    cache = SimpleNamespace(get=lambda kid: signing_key.public_key())
    return EntraAuthenticator(settings, cache=cache)


def token(signing_key, **changes):
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": AUDIENCE, "tid": TENANT, "ver": "2.0",
        "exp": now + 300, "nbf": now - 10, "iat": now - 10,
        "oid": CALLER, "azp": CLIENT, "scp": "CONTOSO.Invoke",
    }
    claims.update(changes)
    return "Bearer " + jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test-key"})


def verify(verifier, header, **changes):
    policy = {
        "allowed_clients": frozenset({CLIENT}), "allowed_callers": frozenset({CALLER}),
        "delegated_scope": "CONTOSO.Invoke", "application_role": "CONTOSO.Invoke",
    }
    policy.update(changes)
    return verifier.verify(header, **policy)


def test_verified_user_identity_not_prompt_metadata(verifier, signing_key):
    identity = verify(verifier, token(signing_key))
    assert identity.caller_id == CALLER
    assert identity.client_id == CLIENT
    assert identity.kind == "user"


def test_workload_requires_application_identity_and_exact_role(verifier, signing_key):
    identity = verify(verifier, token(signing_key, scp=None, idtyp="app", roles=["CONTOSO.Invoke"]))
    assert identity.kind == "workload"


@pytest.mark.parametrize("changes", [
    {"aud": "00000000-0000-4000-8000-000000000099"}, {"tid": "other-tenant"},
    {"ver": "1.0"}, {"exp": 1}, {"oid": "not-an-oid"},
    {"azp": "not-an-app"}, {"nbf": "0"}, {"iat": True},
])
def test_invalid_signed_claims_fail(verifier, signing_key, changes):
    with pytest.raises(ChatFailure) as error:
        verify(verifier, token(signing_key, **changes))
    assert error.value.status == 401
    assert "Bearer" not in str(error.value)


@pytest.mark.parametrize("changes", [
    {"scp": "Wrong.Scope"}, {"scp": None, "roles": ["CONTOSO.Invoke"]},
    {"idtyp": "app", "roles": ["CONTOSO.Invoke"]},
    {"idtyp": "app", "scp": None, "roles": ["Other.Role"]},
    {"idtyp": "app", "scp": None, "roles": "CONTOSO.Invoke"},
    {"idtyp": "app", "scp": None, "roles": ["CONTOSO.Invoke", 1]},
])
def test_missing_or_wrong_permission_is_forbidden(verifier, signing_key, changes):
    with pytest.raises(ChatFailure) as error:
        verify(verifier, token(signing_key, **changes))
    assert error.value.status == 403


def test_valid_token_from_unapproved_application_is_forbidden(verifier, signing_key):
    with pytest.raises(ChatFailure) as error:
        verify(verifier, token(signing_key), allowed_clients=frozenset({AUDIENCE}))
    assert error.value.status == 403


def test_unknown_principal_is_forbidden(verifier, signing_key):
    with pytest.raises(ChatFailure) as error:
        verify(verifier, token(signing_key), allowed_callers=frozenset({AUDIENCE}))
    assert error.value.status == 403


@pytest.mark.parametrize("header", [None, "", "Bearer", "Basic test", "Bearer not.a.jwt", "x" * 16385])
def test_malformed_header_is_unauthenticated(verifier, header):
    with pytest.raises(ChatFailure) as error:
        verify(verifier, header)
    assert error.value.status == 401


def test_signature_is_required(verifier):
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(ChatFailure) as error:
        verify(verifier, token(wrong_key))
    assert error.value.status == 401


def test_no_empty_authorization_policy(verifier, signing_key):
    with pytest.raises(ValueError):
        verify(verifier, token(signing_key), allowed_callers=frozenset())
