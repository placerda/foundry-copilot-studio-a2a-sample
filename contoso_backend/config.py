import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID


MAX_BODY_BYTES = 32768
MAX_CONTENT_LENGTH = 4000
CONTOSO_DEADLINE_SECONDS = 75
CONTOSO_MIN_REMAINING_MS = 80000


def gateway_endpoint(value, endpoint_path="/api/invoke", approved_apim_host=None):
    if endpoint_path not in ("/api/invoke", "/api/a2a"):
        raise ValueError
    if (not isinstance(value, str) or len(value) > 256
            or any(c.isspace() or ord(c) < 32 for c in value)
            or any(c in value for c in ("\\", "?", "#", "%"))):
        raise ValueError
    parsed = urlsplit(value)
    apim = (isinstance(approved_apim_host, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,61}[a-z0-9]\.azure-api\.net", approved_apim_host)
            and parsed.netloc == approved_apim_host)
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.port is not None or parsed.path not in ("", "/", endpoint_path)
            or not (apim or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                                        r"(?:\.[a-z0-9-]+)*\.azurewebsites\.net", parsed.netloc))):
        raise ValueError
    return f"https://{parsed.netloc}{endpoint_path}"


@dataclass(frozen=True)
class CONTOSOSettings:
    gateway_url: str
    tenant_id: str
    gateway_api_client_id: str
    requester_client_id: str
    requester_client_secret: str = field(repr=False)
    a2a_gateway_url: str | None = None
    approved_apim_host: str | None = None

    @classmethod
    def from_env(cls, env):
        try:
            raw_gateway = env["CONTOSO_GATEWAY_URL"]
            approved_host = env.get("CONTOSO_APIM_HOST")
            if approved_host is not None and not re.fullmatch(
                    r"[a-z0-9][a-z0-9-]{0,61}[a-z0-9]\.azure-api\.net", approved_host):
                raise ValueError
            path = urlsplit(raw_gateway).path
            gateway = gateway_endpoint(raw_gateway, "/api/a2a" if path == "/api/a2a" else "/api/invoke", approved_host)
            a2a_gateway = (gateway_endpoint(raw_gateway, "/api/a2a", approved_host)
                           if path != "/api/invoke" else None)
            ids = [str(UUID(env[name])) for name in (
                "CONTOSO_ENTRA_TENANT_ID", "CONTOSO_GATEWAY_API_CLIENT_ID", "CONTOSO_REQUESTER_CLIENT_ID")]
            credential_source = env.get("CONTOSO_REQUESTER_CREDENTIAL_SOURCE", "environment")
            if credential_source == "windows":
                if "CONTOSO_REQUESTER_CLIENT_SECRET" in env:
                    raise ValueError
                if __package__:
                    from .windows_credentials import read_requester_secret
                else:
                    from windows_credentials import read_requester_secret
                secret = read_requester_secret(ids[2])
            elif credential_source == "environment":
                secret = env["CONTOSO_REQUESTER_CLIENT_SECRET"]
            else:
                raise ValueError
            if (not isinstance(secret, str) or not secret.strip() or len(secret) > 4096
                    or any(ord(c) < 32 for c in secret)):
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ValueError("Invalid CONTOSO configuration") from None
        return cls(gateway, *ids, secret, a2a_gateway_url=a2a_gateway, approved_apim_host=approved_host)


@dataclass(frozen=True)
class Settings:
    tenant_id: str
    audience: str
    owner_oid: str
    scope: str = "access_as_user"
    max_messages: int = 10
    contoso: CONTOSOSettings | None = None

    @property
    def issuer(self):
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    @classmethod
    def from_env(cls, environment=None):
        env = os.environ if environment is None else environment
        try:
            ids = [str(UUID(env[name])) for name in
                   ("TENANT_ID", "API_AUDIENCE", "ALLOWED_USER_OID")]
            scope = env.get("REQUIRED_SCOPE", "access_as_user")
            count = int(env.get("MAX_MESSAGES", "10"))
            contoso_enabled = env.get("CONTOSO_ENABLED", "false")
            if contoso_enabled not in ("true", "false"):
                raise ValueError
            contoso = CONTOSOSettings.from_env(env) if contoso_enabled == "true" else None
            if (count < 1
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+", scope)):
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ValueError("Invalid service configuration") from None
        return cls(*ids, scope=scope, max_messages=count, contoso=contoso)
