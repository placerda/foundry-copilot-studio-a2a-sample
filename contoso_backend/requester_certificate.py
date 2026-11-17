"""Local certificate assertions with private keys in Windows Credential Manager."""

import argparse
import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import jwt
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

if __package__:
    from .windows_credentials import read_requester_certificate_key, store_requester_certificate_key
else:
    from windows_credentials import read_requester_certificate_key, store_requester_certificate_key


class CertificateUnavailable(ValueError):
    pass


def _public_path(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise CertificateUnavailable("Invalid public certificate path")
    path = Path(value)
    if not path.is_absolute() or path.drive.startswith("\\\\"):
        raise CertificateUnavailable("A local absolute public certificate path is required")
    return path


@dataclass(frozen=True)
class RequesterCertificate:
    _key: rsa.RSAPrivateKey = field(repr=False)
    _certificate: x509.Certificate = field(repr=False)

    @classmethod
    def from_windows(cls, client_id, certificate_path):
        try:
            path = _public_path(certificate_path)
            with path.open("rb") as stream:
                document = stream.read(65537)
            if len(document) > 65536 or b"PRIVATE KEY" in document:
                raise ValueError
            certificate = x509.load_pem_x509_certificate(document)
            fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
            key = serialization.load_pem_private_key(
                read_requester_certificate_key(client_id, fingerprint), password=None,
            )
            public = certificate.public_key()
            if (not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048
                    or not isinstance(public, rsa.RSAPublicKey)
                    or key.public_key().public_numbers() != public.public_numbers()):
                raise ValueError
            credential = cls(key, certificate)
            credential.require_valid()
            return credential
        except (OSError, ValueError, TypeError, UnsupportedAlgorithm):
            raise CertificateUnavailable("Requester certificate is unavailable or invalid") from None

    def require_valid(self):
        now = datetime.now(timezone.utc)
        if not self._certificate.not_valid_before_utc <= now < self._certificate.not_valid_after_utc:
            raise CertificateUnavailable("Requester certificate is not currently valid")

    def assertion(self, client_id, tenant_id):
        self.require_valid()
        now = datetime.now(timezone.utc)
        expires = min(now + timedelta(minutes=5), self._certificate.not_valid_after_utc)
        thumbprint = base64.urlsafe_b64encode(
            self._certificate.fingerprint(hashes.SHA256()),
        ).rstrip(b"=").decode("ascii")
        return jwt.encode(
            {"iss": client_id, "sub": client_id,
             "aud": f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
             "jti": str(uuid4()), "iat": int(now.timestamp()), "nbf": int(now.timestamp()),
             "exp": int(expires.timestamp())},
            self._key, algorithm="PS256", headers={"x5t#S256": thumbprint},
        )


def create_local_certificate(client_id, public_certificate_path):
    """Create only a local key and a public PEM, without changing Entra or Azure."""
    try:
        if str(UUID(client_id)) != client_id:
            raise ValueError
        path = _public_path(public_certificate_path)
        if path.exists() or not path.parent.is_dir():
            raise ValueError
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Contoso local requester")])
        certificate = (
            x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=90)).sign(key, hashes.SHA256())
        )
        private_key = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
        # Only the public certificate is written to the filesystem.
        with path.open("xb") as stream:
            stream.write(certificate.public_bytes(serialization.Encoding.PEM))
        store_requester_certificate_key(client_id, fingerprint, private_key)
        return fingerprint
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm):
        raise CertificateUnavailable(
            "Local certificate setup failed. Use a new public PEM path with an existing parent "
            "directory and an available Windows Credential Manager. No cloud changes were made."
        ) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--public-certificate", required=True)
    args = parser.parse_args()
    try:
        create_local_certificate(args.client_id, args.public_certificate)
    except CertificateUnavailable as error:
        parser.exit(1, str(error) + "\n")
    print("Local certificate created. Upload only the public PEM to the requester app registration.")
    print("No cloud resources or active application settings were changed.")


if __name__ == "__main__":
    main()
