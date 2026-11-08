import base64

import pytest

from contoso.state import (
    EncryptedStateStore, StateConflict, StateError, StateMissing,
)


class MemoryBlobs:
    def __init__(self):
        self.values = {}
        self.sequence = 0

    def read(self, name, max_bytes):
        if name not in self.values:
            raise StateMissing("No state")
        return self.values[name]

    def write(self, name, data, etag):
        if (etag is None and name in self.values
                or etag is not None and (name not in self.values or self.values[name][1] != etag)):
            raise StateConflict("Concurrent update")
        self.sequence += 1
        version = str(self.sequence)
        self.values[name] = data, version
        return version

    def delete(self, name, etag):
        if name not in self.values:
            raise StateMissing("No state")
        if self.values[name][1] != etag:
            raise StateConflict("Concurrent update")
        del self.values[name]


@pytest.fixture
def setup():
    contoso_backend = MemoryBlobs()
    now = [100]
    key = base64.urlsafe_b64encode(b"x" * 32).decode("ascii")
    store = EncryptedStateStore(contoso_backend, key, namespace="test-connection", clock=lambda: now[0])
    return contoso_backend, now, store, key


def test_ciphertext_and_record_repr_never_reveal_token_or_identity(setup):
    contoso_backend, _, store, _ = setup
    record = store.put("oauth-cache", "selected-guest", {"cache": "sensitive-test-token"}, ttl=600)
    assert store.get("oauth-cache", "selected-guest").payload == {"cache": "sensitive-test-token"}
    name, (encrypted, _) = next(iter(contoso_backend.values.items()))
    assert "selected-guest" not in name
    assert b"sensitive-test-token" not in encrypted
    assert "sensitive-test-token" not in repr(record)


@pytest.mark.parametrize("binding", ["identity", "purpose", "namespace", "key"])
def test_ciphertext_cannot_be_rebound(setup, binding):
    contoso_backend, _, store, key = setup
    store.put("oauth-flow", "first", {"flow": "secret-verifier"}, ttl=600)
    original = next(iter(contoso_backend.values.values()))
    purpose, identity = "oauth-flow", "first"
    if binding == "identity":
        identity = "second"
    if binding == "purpose":
        purpose = "oauth-cache"
    if binding == "namespace":
        store = EncryptedStateStore(contoso_backend, key, namespace="different", clock=lambda: 100)
    if binding == "key":
        changed = base64.urlsafe_b64encode(b"y" * 32).decode("ascii")
        store = EncryptedStateStore(contoso_backend, changed, namespace="test-connection", clock=lambda: 100)
    target, _ = store._binding(purpose, identity)
    contoso_backend.values[target] = original
    with pytest.raises(StateError, match="integrity"):
        store.get(purpose, identity)


def test_tampered_ciphertext_is_rejected(setup):
    contoso_backend, _, store, _ = setup
    store.put("oauth-flow", "first", {"flow": "secret"}, ttl=600)
    name, (data, etag) = next(iter(contoso_backend.values.items()))
    contoso_backend.values[name] = data[:-1] + bytes([data[-1] ^ 1]), etag
    with pytest.raises(StateError, match="integrity"):
        store.get("oauth-flow", "first")


def test_expired_state_never_returns_payload(setup):
    _, now, store, _ = setup
    store.put("oauth-flow", "first", {"flow": "secret"}, ttl=60)
    now[0] = 160
    with pytest.raises(StateMissing, match="expired"):
        store.consume("oauth-flow", "first")


def test_flow_is_consumed_exactly_once(setup):
    _, _, store, _ = setup
    store.put("oauth-flow", "first", {"flow": "secret"}, ttl=600)
    assert store.consume("oauth-flow", "first") == {"flow": "secret"}
    with pytest.raises(StateMissing):
        store.consume("oauth-flow", "first")


def test_conditional_update_prevents_lost_refresh_state(setup):
    _, _, store, _ = setup
    first = store.put("oauth-cache", "first", {"cache": "old"}, ttl=600)
    second = store.put("oauth-cache", "first", {"cache": "new"}, ttl=600, etag=first.etag)
    with pytest.raises(StateConflict):
        store.put("oauth-cache", "first", {"cache": "stale"}, ttl=600, etag=first.etag)
    assert store.get("oauth-cache", "first") == second


def test_create_does_not_overwrite_existing_connection(setup):
    _, _, store, _ = setup
    store.put("oauth-cache", "first", {"cache": "existing"}, ttl=600)
    with pytest.raises(StateConflict):
        store.put("oauth-cache", "first", {"cache": "replacement"}, ttl=600)


@pytest.mark.parametrize("ttl", [0, -1, float("nan"), float("inf"), True, 2592001])
def test_invalid_lifetime_never_writes(setup, ttl):
    contoso_backend, _, store, _ = setup
    with pytest.raises(StateError):
        store.put("oauth-flow", "first", {}, ttl=ttl)
    assert not contoso_backend.values


@pytest.mark.parametrize("payload", [[], {"bad": float("nan")}, {"bad": object()}])
def test_invalid_payload_never_writes(setup, payload):
    contoso_backend, _, store, _ = setup
    with pytest.raises(StateError):
        store.put("oauth-flow", "first", payload, ttl=600)
    assert not contoso_backend.values


def test_payload_byte_limit_is_exact(setup):
    contoso_backend, _, _, key = setup
    store = EncryptedStateStore(contoso_backend, key, namespace="test", max_bytes=1024, clock=lambda: 100)
    with pytest.raises(StateError, match="limit"):
        store.put("oauth-cache", "first", {"cache": "x" * 1024}, ttl=600)
    assert not contoso_backend.values


@pytest.mark.parametrize("key", ["", "invalid!", base64.b64encode(b"short").decode("ascii")])
def test_invalid_encryption_configuration_is_explicit(key):
    with pytest.raises(StateError, match="configuration"):
        EncryptedStateStore(MemoryBlobs(), key, namespace="test")
