from uuid import uuid4

import pytest

from contoso_backend import windows_credentials as credentials
from contoso_backend.config import CONTOSOSettings


CLIENT = str(uuid4())


def environment():
    return {
        "CONTOSO_GATEWAY_URL": "https://sample-apim.azure-api.net/api/a2a",
        "CONTOSO_APIM_HOST": "sample-apim.azure-api.net",
        "CONTOSO_ENTRA_TENANT_ID": str(uuid4()),
        "CONTOSO_GATEWAY_API_CLIENT_ID": str(uuid4()),
        "CONTOSO_REQUESTER_CLIENT_ID": CLIENT,
        "CONTOSO_REQUESTER_CREDENTIAL_SOURCE": "windows",
    }


def test_reads_only_exact_requester_identity(monkeypatch):
    calls = []
    monkeypatch.setattr(credentials, "_read", lambda target: (
        calls.append(target) or CLIENT, b"fake-local-secret",
    ))
    settings = CONTOSOSettings.from_env(environment())
    assert settings.requester_client_secret == "fake-local-secret"
    assert calls == ["ContosoAgentDemo:requester:" + CLIENT]
    assert "fake-local-secret" not in repr(settings)


@pytest.mark.parametrize(("username", "blob"), [
    (str(uuid4()), b"not-the-requester"), (CLIENT, b""),
    (CLIENT, b"\xff"), (CLIENT, b"secret\n"), (CLIENT, b"   "),
])
def test_wrong_identity_or_invalid_blob_fails_closed(monkeypatch, username, blob):
    monkeypatch.setattr(credentials, "_read", lambda target: (username, blob))
    with pytest.raises(ValueError, match="Invalid CONTOSO configuration"):
        CONTOSOSettings.from_env(environment())


def test_missing_windows_credential_never_falls_back(monkeypatch):
    def missing(target):
        raise credentials.CredentialUnavailable("Missing")
    monkeypatch.setattr(credentials, "_read", missing)
    with pytest.raises(ValueError, match="Invalid CONTOSO configuration"):
        CONTOSOSettings.from_env(environment())


def test_ambiguous_or_unknown_source_rejected():
    env = environment()
    env["CONTOSO_REQUESTER_CLIENT_SECRET"] = "other-secret"
    with pytest.raises(ValueError):
        CONTOSOSettings.from_env(env)
    del env["CONTOSO_REQUESTER_CLIENT_SECRET"]
    env["CONTOSO_REQUESTER_CREDENTIAL_SOURCE"] = "automatic"
    with pytest.raises(ValueError):
        CONTOSOSettings.from_env(env)


@pytest.mark.parametrize("client_id", [
    "other-credential", "", None, "ABCDEFAB-ABCD-4ABC-8ABC-ABCDEFABCDEF",
])
def test_arbitrary_credential_names_rejected_before_os_access(monkeypatch, client_id):
    monkeypatch.setattr(credentials, "_read", lambda target: pytest.fail("OS access forbidden"))
    with pytest.raises(credentials.CredentialUnavailable):
        credentials.read_requester_secret(client_id)


def test_writer_uses_requester_namespace_and_utf8(monkeypatch):
    calls = []
    monkeypatch.setattr(credentials, "_write", lambda *args: calls.append(args))
    target = credentials.store_requester_secret(CLIENT, "fake-sécret")
    assert calls == [(target, CLIENT, "fake-sécret".encode("utf-8"))]
    assert target == "ContosoAgentDemo:requester:" + CLIENT


@pytest.mark.parametrize("secret", ["", " ", "secret\n", None, "s" * 2561, "\ud800"])
def test_invalid_secret_never_written(monkeypatch, secret):
    monkeypatch.setattr(credentials, "_write", lambda *args: pytest.fail("OS write forbidden"))
    with pytest.raises(credentials.CredentialUnavailable):
        credentials.store_requester_secret(CLIENT, secret)
