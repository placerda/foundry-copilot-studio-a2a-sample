"""Gateway JWT verification reusing the chat's bounded Entra key transport."""

from dataclasses import dataclass
import math
from uuid import UUID

import jwt

from contoso_backend.auth import KeyCache
from contoso_backend.errors import ChatFailure, unauthenticated


def _identifier(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("An Entra identifier must be a UUID")
    return str(UUID(value))


@dataclass(frozen=True)
class IdentitySettings:
    tenant_id: str
    audience: str

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", _identifier(self.tenant_id))
        object.__setattr__(self, "audience", _identifier(self.audience))

    @property
    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"


@dataclass(frozen=True)
class CallerIdentity:
    caller_id: str
    client_id: str
    kind: str


class EntraAuthenticator:
    def __init__(self, settings: IdentitySettings, *, cache: KeyCache | None = None):
        self.settings = settings
        self.cache = cache if cache is not None else KeyCache(settings)

    def verify(
        self,
        header: str,
        *,
        allowed_clients: frozenset[str],
        allowed_callers: frozenset[str],
        delegated_scope: str,
        application_role: str,
    ) -> CallerIdentity:
        if not allowed_clients or not allowed_callers or not delegated_scope or not application_role:
            raise ValueError("Explicit client, caller, scope and role policy is required")
        clients = frozenset(_identifier(value) for value in allowed_clients)
        callers = frozenset(_identifier(value) for value in allowed_callers)
        if not isinstance(header, str) or len(header) > 16384:
            raise unauthenticated()
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise unauthenticated()
        try:
            unverified = jwt.get_unverified_header(parts[1])
            if unverified.get("alg") != "RS256" or "crit" in unverified:
                raise unauthenticated()
            key = self.cache.get(unverified.get("kid"))
            claims = jwt.decode(
                parts[1],
                key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                leeway=0,
                options={
                    "require": ["exp", "nbf", "iat", "iss", "aud", "tid", "ver", "oid", "azp"],
                    "strict_aud": True,
                },
            )
            if claims["tid"] != self.settings.tenant_id or claims["ver"] != "2.0":
                raise unauthenticated()
            if any(
                type(claims[name]) not in (int, float) or not math.isfinite(claims[name])
                for name in ("exp", "nbf", "iat")
            ):
                raise unauthenticated()
            caller = _identifier(claims["oid"])
            client = _identifier(claims["azp"])
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
            raise unauthenticated() from None
        if caller not in callers or client not in clients:
            raise ChatFailure(403, "forbidden", "This caller is not authorized.")
        scopes = claims.get("scp")
        if claims.get("idtyp") == "app":
            roles = claims.get("roles")
            if (scopes is not None or not isinstance(roles, list)
                    or any(not isinstance(role, str) for role in roles)
                    or application_role not in roles):
                raise ChatFailure(403, "forbidden", "The required application role is missing.")
            return CallerIdentity(caller, client, "workload")
        if not isinstance(scopes, str) or delegated_scope not in scopes.split():
            raise ChatFailure(403, "forbidden", "The required delegated scope is missing.")
        return CallerIdentity(caller, client, "user")
