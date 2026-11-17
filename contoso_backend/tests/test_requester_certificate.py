import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sys

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from a2a_requester import A2ARequester, NATIVE_OPTIONS
from config import CONTOSOSettings
from errors import ChatFailure
import requester_certificate as certificates
import windows_credentials as credentials
from contoso_backend.tests.test_a2a_requester import A2ASession
from contoso_backend.tests.test_contoso_requester import (
    API, CLIENT, SECRET, SERVICE_TOKEN, TENANT, FakeResponse, FakeSession, token_result,
)


@pytest.fixture
def certificate_env(tmp_path, monkeypatch):
    protected = {}
    monkeypatch.setattr(credentials, "_write", lambda target, client, blob: protected.update(
        {target: (client, blob)},
    ))

    def read(target):
        if target not in protected:
            raise credentials.CredentialUnavailable("PRIVATE-MISSING-MARKER")
        return protected[target]

    monkeypatch.setattr(credentials, "_read", read)
    path = tmp_path / "requester-public.pem"
    certificates.create_local_certificate(CLIENT, str(path))
    env = {
        "CONTOSO_GATEWAY_URL": "https://sample-apim.azure-api.net/api/a2a",
        "CONTOSO_APIM_HOST": "sample-apim.azure-api.net",
        "CONTOSO_ENTRA_TENANT_ID": TENANT,
        "CONTOSO_GATEWAY_API_CLIENT_ID": API,
        "CONTOSO_REQUESTER_CLIENT_ID": CLIENT,
        "CONTOSO_REQUESTER_CREDENTIAL_SOURCE": "windows-certificate",
        "CONTOSO_REQUESTER_CERTIFICATE_PATH": str(path),
    }
    return env, path, protected


def decode_assertion(value, certificate):
    token_url = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    return jwt.decode(value, certificate.public_key(), algorithms=["PS256"],
                      audience=token_url, issuer=CLIENT,
                      options={"require": ["iss", "sub", "aud", "jti", "iat", "nbf", "exp"]})


def test_certificate_a2a_authentication_cache_refresh_and_no_mtls(certificate_env, caplog):
    env, path, protected = certificate_env
    settings = CONTOSOSettings.from_env(env)
    assert settings.requester_client_secret is None
    assert path.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    certificate = x509.load_pem_x509_certificate(path.read_bytes())
    fingerprint = certificate.fingerprint(hashes.SHA256())
    target = f"ContosoAgentDemo:requester:{CLIENT}:certificate:{fingerprint.hex()}"
    assert set(protected) == {target}
    private = protected[target][1]
    assert b"PRIVATE KEY" not in path.read_bytes()
    session = A2ASession()
    worker = A2ARequester(settings, lambda: session)
    results = [worker.invoke("question", NATIVE_OPTIONS, 90000)]
    results.append(worker.invoke("second question", NATIVE_OPTIONS, 90000))
    assert len(session.calls) == 3
    worker._expires = 0
    session.responses.append(FakeResponse(token_result()))
    results.append(worker.invoke("third question", NATIVE_OPTIONS, 90000))
    assert len(session.calls) == 5
    assertions = []
    for _, args in (session.calls[0], session.calls[3]):
        data = args["data"]
        assert set(data) == {
            "grant_type", "client_id", "scope", "client_assertion_type", "client_assertion",
        }
        assert data["client_id"] == CLIENT
        assert data["grant_type"] == "client_credentials"
        assert data["scope"] == f"api://{API}/.default"
        assert data["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        claims = decode_assertion(data["client_assertion"], certificate)
        assert claims["sub"] == CLIENT
        assert 0 < claims["exp"] - claims["nbf"] <= 300
        assert claims["exp"] <= int(certificate.not_valid_after_utc.timestamp())
        assert jwt.get_unverified_header(data["client_assertion"]) == {
            "alg": "PS256", "typ": "JWT",
            "x5t#S256": base64.urlsafe_b64encode(fingerprint).rstrip(b"=").decode(),
        }
        assert args["timeout"][1] <= 8
        assertions.append(claims)
    assert assertions[0]["jti"] != assertions[1]["jti"]
    for index in (1, 2, 4):
        args = session.calls[index][1]
        assert args["headers"]["Authorization"] == f"Bearer {SERVICE_TOKEN}"
        assert args["headers"]["A2A-Version"] == "1.0"
        assert "client_assertion" not in json.dumps(args)
    for _, args in session.calls:
        assert "cert" not in args and args["allow_redirects"] is False
    assert private.decode() not in repr(settings) + json.dumps(results) + caplog.text
    assert "client_assertion" not in json.dumps(results) + caplog.text


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_certificate_rejection_never_uses_legacy_secret(certificate_env, status, caplog):
    env, _, protected = certificate_env
    protected[f"ContosoAgentDemo:requester:{CLIENT}"] = (CLIENT, SECRET.encode())
    session = FakeSession([FakeResponse(status=status, raw=b"PRIVATE-TOKEN-ERROR")])
    worker = A2ARequester(CONTOSOSettings.from_env(env), lambda: session)
    with pytest.raises(ChatFailure) as failure:
        worker.invoke("question", NATIVE_OPTIONS, 90000)
    assert failure.value.status == 502 and len(session.calls) == 1
    assert "client_secret" not in session.calls[0][1]["data"]
    assert worker._token is None
    assert "PRIVATE-TOKEN-ERROR" not in failure.value.message + caplog.text


def test_requester_does_not_accept_two_credentials(certificate_env):
    env, _, _ = certificate_env
    settings = CONTOSOSettings.from_env(env)
    with pytest.raises(ValueError, match="Exactly one requester credential"):
        A2ARequester(replace(settings, requester_client_secret=SECRET))


def test_expired_certificate_blocks_even_a_cached_token(certificate_env, monkeypatch):
    env, _, _ = certificate_env
    session = A2ASession()
    worker = A2ARequester(CONTOSOSettings.from_env(env), lambda: session)
    worker.invoke("first", NATIVE_OPTIONS, 90000)

    def expired(self):
        raise certificates.CertificateUnavailable("Expired")

    monkeypatch.setattr(certificates.RequesterCertificate, "require_valid", expired)
    with pytest.raises(ChatFailure):
        worker.invoke("second", NATIVE_OPTIONS, 90000)
    assert len(session.calls) == 2 and worker._token is None


@pytest.mark.parametrize("problem", [
    "expired", "not-yet-valid", "wrong-key", "weak-key", "ec-key",
    "missing-key", "missing-file", "malformed", "private-file",
])
def test_bad_certificate_fails_closed_before_network(certificate_env, problem, caplog):
    env, path, protected = certificate_env
    original_key = serialization.load_pem_private_key(next(iter(protected.values()))[1], password=None)
    now = datetime.now(timezone.utc)
    if problem in ("expired", "not-yet-valid", "wrong-key", "weak-key", "ec-key"):
        key = original_key
        if problem == "weak-key":
            key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        elif problem == "ec-key":
            key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic requester")])
        before, after = now - timedelta(minutes=1), now + timedelta(days=1)
        if problem == "expired":
            before, after = now - timedelta(days=2), now - timedelta(days=1)
        elif problem == "not-yet-valid":
            before, after = now + timedelta(days=1), now + timedelta(days=2)
        certificate = (
            x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(before).not_valid_after(after).sign(key, hashes.SHA256())
        )
        path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        if problem == "wrong-key":
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
        protected[f"ContosoAgentDemo:requester:{CLIENT}:certificate:{fingerprint}"] = (
            CLIENT, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()),
        )
    elif problem == "missing-key":
        protected.clear()
    elif problem == "missing-file":
        path.unlink()
    elif problem == "private-file":
        path.write_bytes(next(iter(protected.values()))[1])
    else:
        path.write_bytes(b"PRIVATE-INVALID-MARKER")
    with pytest.raises(ValueError, match="Invalid CONTOSO configuration") as error:
        CONTOSOSettings.from_env(env)
    assert "PRIVATE-" not in str(error.value) + caplog.text


@pytest.mark.parametrize("changes", [
    {"CONTOSO_REQUESTER_CLIENT_SECRET": SECRET},
    {"CONTOSO_REQUESTER_CERTIFICATE_PATH": ""},
    {"CONTOSO_REQUESTER_CERTIFICATE_PATH": "relative-public.pem"},
    {"CONTOSO_REQUESTER_CERTIFICATE_PATH": "\\\\server\\share\\public.pem"},
    {"CONTOSO_REQUESTER_CREDENTIAL_SOURCE": "windows"},
    {"CONTOSO_REQUESTER_CREDENTIAL_SOURCE": "environment"},
])
def test_ambiguous_or_invalid_certificate_settings_rejected(certificate_env, changes):
    env, _, _ = certificate_env
    with pytest.raises(ValueError, match="Invalid CONTOSO configuration"):
        CONTOSOSettings.from_env({**env, **changes})


def test_creation_refuses_overwrite_and_cli_does_not_print_key(certificate_env, monkeypatch, capsys):
    _, path, protected = certificate_env
    original = path.read_bytes()
    with pytest.raises(certificates.CertificateUnavailable):
        certificates.create_local_certificate(CLIENT, str(path))
    assert path.read_bytes() == original and len(protected) == 1
    new_path = path.with_name("rotation-public.pem")
    monkeypatch.setattr(sys, "argv", ["certificate", "--client-id", CLIENT,
                                    "--public-certificate", str(new_path)])
    certificates.main()
    output = capsys.readouterr()
    assert new_path.exists() and len(protected) == 2
    assert "No cloud resources" in output.out
    assert "PRIVATE KEY" not in output.out + output.err
    for _, private in protected.values():
        assert private.decode() not in output.out + output.err
    certificates.RequesterCertificate.from_windows(CLIENT, str(path))
    certificates.RequesterCertificate.from_windows(CLIENT, str(new_path))
