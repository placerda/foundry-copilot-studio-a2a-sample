from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import os
from uuid import UUID

from contoso.identity import IdentitySettings


def _unique_identity_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate managed-agent identity field")
        result[key] = value
    return result


@dataclass(frozen=True)
class GatewaySettings:
    tenant_id: str
    audience: str
    aws_requester_client_id: str
    aws_requester_oid: str
    authoring_client_id: str
    authoring_admin_oid: str
    managed_agent_client_id: str
    managed_agent_oid: str
    authentication_timeout: float = 15
    connection_timeout: float = 60
    invocation_timeout: float = 90
    managed_agent_identities: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        for name in (
            "tenant_id", "audience", "aws_requester_client_id", "aws_requester_oid",
            "authoring_client_id", "authoring_admin_oid", "managed_agent_client_id",
            "managed_agent_oid",
        ):
            value = getattr(self, name)
            try:
                if not isinstance(value, str):
                    raise ValueError
                object.__setattr__(self, name, str(UUID(value)))
            except ValueError:
                raise ValueError("Gateway configuration requires explicit Entra UUIDs") from None
        try:
            if (not isinstance(self.managed_agent_identities, (tuple, list))
                    or len(self.managed_agent_identities) > 15):
                raise ValueError
            additional = []
            seen = {(self.managed_agent_client_id, self.managed_agent_oid)}
            for pair in self.managed_agent_identities:
                if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                        or any(not isinstance(value, str) for value in pair)):
                    raise ValueError
                normalized = tuple(str(UUID(value)) for value in pair)
                if normalized in seen:
                    raise ValueError
                seen.add(normalized)
                additional.append(normalized)
            object.__setattr__(self, "managed_agent_identities", tuple(additional))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Managed-agent identities require unique explicit client/OID pairs") from None
        for name in ("authentication_timeout", "connection_timeout", "invocation_timeout"):
            value = getattr(self, name)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not 0 < value <= 120):
                raise ValueError("Gateway timeouts must be bounded to 120 seconds")

    @property
    def identity_settings(self) -> IdentitySettings:
        return IdentitySettings(self.tenant_id, self.audience)

    @property
    def token_identities(self) -> tuple[tuple[str, str], ...]:
        return ((self.managed_agent_client_id, self.managed_agent_oid), *self.managed_agent_identities)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "GatewaySettings":
        env = os.environ if environ is None else environ
        fields = {
            "tenant_id": "CONTOSO_TENANT_ID",
            "audience": "CONTOSO_GATEWAY_AUDIENCE",
            "aws_requester_client_id": "CONTOSO_AWS_REQUESTER_CLIENT_ID",
            "aws_requester_oid": "CONTOSO_AWS_REQUESTER_OID",
            "authoring_client_id": "CONTOSO_AUTHORING_CLIENT_ID",
            "authoring_admin_oid": "CONTOSO_AUTHORING_ADMIN_OID",
            "managed_agent_client_id": "CONTOSO_MANAGED_AGENT_CLIENT_ID",
            "managed_agent_oid": "CONTOSO_MANAGED_AGENT_OID",
        }
        try:
            options = {field: env[key] for field, key in fields.items()}
        except KeyError:
            raise ValueError("Required Gateway environment configuration is missing") from None
        if "CONTOSO_MANAGED_AGENT_IDENTITIES" in env:
            try:
                raw = env["CONTOSO_MANAGED_AGENT_IDENTITIES"]
                if not isinstance(raw, str) or not 0 < len(raw) <= 8192:
                    raise ValueError
                entries = json.loads(raw, object_pairs_hook=_unique_identity_fields)
                if (not isinstance(entries, list) or len(entries) > 15
                        or any(not isinstance(entry, dict) or set(entry) != {"client_id", "oid"}
                               for entry in entries)):
                    raise ValueError
                options["managed_agent_identities"] = tuple(
                    (entry["client_id"], entry["oid"]) for entry in entries
                )
            except (ValueError, TypeError, RecursionError):
                raise ValueError("Invalid additional managed-agent identity configuration") from None
        return cls(**options)
