import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from config import Settings
from errors import ChatFailure


@pytest.fixture
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def claims(settings, **changes):
    return {"iss": settings.issuer, "aud": settings.audience, "tid": settings.tenant_id,
            "ver": "2.0", "oid": settings.owner_oid, "scp": settings.scope,
            "exp": int(time.time()) + 3600, **changes}


def sign(key, payload, **headers):
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "key1", **headers})


class Clock:
    def __init__(self):
        self.now = 100
    def __call__(self):
        return self.now


class FakeHTTP:
    def __init__(self, settings, key, clock):
        self.calls = []
        self.clock = clock
        self.failure = False
        self.delay = 0
        self.jwks_url = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        self.keys = [{**jwk, "kid": "key1", "use": "sig", "alg": "RS256"}]
        self.issuer = settings.issuer

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        self.clock.now += self.delay
        if self.failure:
            raise OSError("SECRET")
        if "openid-configuration" in url:
            return {"issuer": self.issuer, "jwks_uri": self.jwks_url}
        return {"keys": self.keys}


@pytest.fixture
def auth_setup(environment, key):
    from auth import Authorizer, KeyCache
    settings = Settings.from_env(environment)
    clock = Clock()
    http = FakeHTTP(settings, key, clock)
    cache = KeyCache(settings, http=http, clock=clock)
    return settings, clock, http, cache, Authorizer(settings, cache)


def test_valid_without_nbf(auth_setup, key):
    settings, _, http, _, authorizer = auth_setup
    authorizer.verify("Bearer " + sign(key, claims(settings)))
    authorizer.verify("Bearer " + sign(key, claims(settings)))
    assert len(http.calls) == 2


@pytest.mark.parametrize("changes", [
    {"exp": 1}, {"exp": None}, {"nbf": int(time.time()) + 3600},
    {"iss": "https://example.com"}, {"aud": "graph"}, {"aud": ["bad"]},
    {"aud": None}, {"ver": "1.0"}, {"tid": "wrong"},
])
def test_invalid_claims(changes, auth_setup, key):
    settings, _, _, _, authorizer = auth_setup
    with pytest.raises(ChatFailure) as error:
        authorizer.verify("Bearer " + sign(key, claims(settings, **changes)))
    assert error.value.status == 401


def test_exp_required_and_strict_scalar_audience(auth_setup, key):
    settings, _, _, _, authorizer = auth_setup
    for payload in ({"aud": [settings.audience]},):
        with pytest.raises(ChatFailure) as error:
            authorizer.verify("Bearer " + sign(key, claims(settings, **payload)))
        assert error.value.status == 401
    payload = claims(settings)
    del payload["exp"]
    with pytest.raises(ChatFailure):
        authorizer.verify("Bearer " + sign(key, payload))


@pytest.mark.parametrize("changes", [{"scp": ""}, {"scp": None}, {"oid": "other"}, {"oid": None}])
def test_forbidden(changes, auth_setup, key):
    settings, _, _, _, authorizer = auth_setup
    with pytest.raises(ChatFailure) as error:
        authorizer.verify("Bearer " + sign(key, claims(settings, **changes)))
    assert error.value.status == 403


def test_guest_uses_resource_tenant_identity_not_home_directory(auth_setup, key):
    settings, _, _, _, authorizer = auth_setup
    home_tenant = "44444444-4444-4444-4444-444444444444"
    home_oid = "55555555-5555-5555-5555-555555555555"
    # B2B guest tokens are issued by the resource tenant, while idp can identify
    # the guest's original identity provider.
    guest = claims(settings, idp=f"https://sts.windows.net/{home_tenant}/")
    authorizer.verify("Bearer " + sign(key, guest))
    with pytest.raises(ChatFailure) as error:
        authorizer.verify("Bearer " + sign(key, {**guest, "oid": home_oid}))
    assert error.value.status == 403
    with pytest.raises(ChatFailure) as error:
        authorizer.verify("Bearer " + sign(key, {
            **guest, "tid": home_tenant,
            "iss": f"https://login.microsoftonline.com/{home_tenant}/v2.0",
        }))
    assert error.value.status == 401


def test_signature_algorithm_and_token_urls(auth_setup, key):
    settings, _, http, _, authorizer = auth_setup
    token = sign(key, claims(settings), jku="https://evil.invalid", x5u="https://evil.invalid")
    authorizer.verify("Bearer " + token)
    assert all(url.startswith("https://login.microsoftonline.com/") for url, _ in http.calls)
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for token in (sign(wrong_key, claims(settings)),
                  jwt.encode(claims(settings), "synthetic" * 5, algorithm="HS256", headers={"kid": "key1"})):
        with pytest.raises(ChatFailure) as error:
            authorizer.verify("Bearer " + token)
        assert error.value.status == 401


def test_cache_rotation_ttl_cooldown(auth_setup):
    _, clock, http, cache, _ = auth_setup
    cache.get("key1")
    with pytest.raises(ChatFailure) as error:
        cache.get("unknown")
    assert error.value.status == 401
    assert len(http.calls) == 2
    clock.now += 31
    with pytest.raises(ChatFailure):
        cache.get("unknown")
    assert len(http.calls) == 4
    http.keys[0]["kid"] = "rotated"
    clock.now += 31
    assert cache.get("rotated")
    assert len(http.calls) == 6
    clock.now += 301
    http.failure = True
    with pytest.raises(ChatFailure) as error:
        cache.get("rotated")
    assert error.value.status == 502
    calls = len(http.calls)
    with pytest.raises(ChatFailure):
        cache.get("rotated")
    assert len(http.calls) == calls


def test_failure_cooldown_keeps_known_valid_key(auth_setup):
    _, clock, http, cache, _ = auth_setup
    cache.get("key1")
    clock.now += 31
    http.failure = True
    with pytest.raises(ChatFailure) as error:
        cache.get("unknown")
    assert error.value.status == 502
    calls = len(http.calls)
    assert cache.get("key1")
    with pytest.raises(ChatFailure) as error:
        cache.get("unknown")
    assert error.value.status == 502
    assert len(http.calls) == calls


@pytest.mark.parametrize("change", ["duplicate", "host", "issuer", "deadline", "private", "encryption"])
def test_invalid_discovery_or_keys(change, auth_setup):
    _, _, http, cache, _ = auth_setup
    if change == "duplicate":
        http.keys *= 2
    elif change == "host":
        http.jwks_url = "https://evil.invalid/keys"
    elif change == "issuer":
        http.issuer = "https://evil.invalid"
    elif change == "deadline":
        http.delay = 2.1
    elif change == "private":
        http.keys[0]["d"] = "private"
    else:
        http.keys[0]["use"] = "enc"
    with pytest.raises(ChatFailure) as error:
        cache.get("key1")
    assert error.value.status == 502
    assert all(timeout <= 4 for _, timeout in http.calls)
