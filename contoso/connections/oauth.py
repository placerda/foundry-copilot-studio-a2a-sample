"""MSAL public-client OAuth/cache operations for the protected Gateway.

Caller IDs, roles and authenticated guest IDs are trusted transport inputs, never
prompt/body fields. Only administrators construct this broker's configuration.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import secrets
import time
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import UUID

import msal

from contoso.router.core import Capability, CatalogEntry, STUDIOVariant, _https_origin
from contoso.router.runtime import ScopedToken
from contoso.state import (
    EncryptedStateStore, StateConflict, StateError, StateMissing,
)


STANDARD_SCOPE = "https://api.powerplatform.com/CopilotStudio.Copilots.Invoke"
HARNESS_SCOPE = "https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A"
CONNECTION_USE = "CONTOSO.Connection.Use"
DEPLOYMENT_ADMIN = "CONTOSO.DeploymentAdmin"
_SCOPES = {
    STUDIOVariant.STANDARD: STANDARD_SCOPE,
    STUDIOVariant.COPILOT_HARNESS: HARNESS_SCOPE,
}


class ConnectionError(RuntimeError):
    """Safe connection failure, with no SDK result or credentials attached."""


class ConnectionDenied(ConnectionError):
    pass


class ConnectionRequired(ConnectionError):
    pass


class ConnectionUnavailable(ConnectionError):
    pass


def _uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value.lower():
            raise ValueError
        return value.lower()
    except (ValueError, AttributeError):
        raise ConnectionError("Connection configuration requires canonical object IDs") from None


def _bounded_text(value, limit=512) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(c.isspace() or ord(c) < 32 for c in value))


def _number(value, *, maximum: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ConnectionError("Invalid connection lifetime or timeout")
    return float(value)


@dataclass(frozen=True)
class ProviderConnection:
    provider_id: str
    tenant_id: str
    client_id: str
    guest_oid: str = field(repr=False)
    redirect_uri: str
    variant: STUDIOVariant
    audience: str
    scopes: frozenset[str]

    def __post_init__(self):
        if not _bounded_text(self.provider_id, 128) or not _bounded_text(self.audience):
            raise ConnectionError("Invalid configured provider")
        for name in ("tenant_id", "client_id", "guest_oid"):
            object.__setattr__(self, name, _uuid(getattr(self, name)))
        if not isinstance(self.variant, STUDIOVariant) or self.variant not in _SCOPES:
            raise ConnectionError("Only supplier-delivery STUDIO variants can use this connection")
        if not isinstance(self.scopes, (set, frozenset, tuple, list)):
            raise ConnectionError("Invalid configured STUDIO scopes")
        try:
            scopes = frozenset(self.scopes)
        except TypeError:
            raise ConnectionError("Invalid configured STUDIO scopes") from None
        if scopes != frozenset({_SCOPES[self.variant]}):
            raise ConnectionError("Only the exact approved STUDIO invocation scope is permitted")
        object.__setattr__(self, "scopes", scopes)
        if not _bounded_text(self.redirect_uri, 2048):
            raise ConnectionError("An exact public HTTPS callback URI is required")
        try:
            _https_origin(self.redirect_uri)
        except ValueError:
            raise ConnectionError("An exact public HTTPS callback URI is required") from None

    @classmethod
    def from_catalog(
        cls, entry: CatalogEntry, *, client_id: str, guest_oid: str, redirect_uri: str,
    ) -> "ProviderConnection":
        if not isinstance(entry, CatalogEntry) or entry.capability != Capability.SUPPLIER_DELIVERY_TERMS:
            raise ConnectionError("Only supplier-delivery STUDIO catalog entries can use this connection")
        return cls(
            entry.provider_id, entry.auth.tenant_id, client_id, guest_oid,
            redirect_uri, entry.variant, entry.auth.audience, entry.auth.scopes,
        )

    @property
    def binding(self) -> str:
        # Configuration changes cannot reuse an old flow or another resource's cache.
        encoded = json.dumps(
            [self.provider_id, self.tenant_id, self.client_id, self.guest_oid,
             self.redirect_uri, self.variant.value, self.audience, sorted(self.scopes)],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class AuthorizationStart:
    auth_uri: str = field(repr=False)
    state: str = field(repr=False)


class DelegatedConnectionBroker:
    def __init__(
        self,
        store: EncryptedStateStore,
        connections: Mapping[str, ProviderConnection],
        caller_providers: Mapping[str, frozenset[str]],
        *,
        preparer_providers: Mapping[str, frozenset[str]] | None = None,
        msal_factory: Callable = msal.PublicClientApplication,
        clock: Callable[[], float] = time.time,
        flow_ttl: float = 600,
        cache_ttl: float = 86400,
        minimum_token_ttl: float = 60,
        network_timeout: tuple[float, float] = (3.05, 10),
    ):
        if not isinstance(connections, Mapping) or not connections:
            raise ConnectionError("Approved provider connections are required")
        configured = dict(connections)
        if any(not isinstance(s, ProviderConnection) or key != s.provider_id
               for key, s in configured.items()):
            raise ConnectionError("Invalid provider connection mapping")
        grants = self._copy_grants(caller_providers, configured)
        preparers = self._copy_grants(
            {} if preparer_providers is None else preparer_providers, configured,
        )
        if not isinstance(network_timeout, tuple) or len(network_timeout) != 2:
            raise ConnectionError("A bounded connect/read timeout is required")
        self._timeout = tuple(_number(t, maximum=30) for t in network_timeout)
        self._flow_ttl = _number(flow_ttl, maximum=600)
        self._cache_ttl = _number(cache_ttl, maximum=2592000)
        self._minimum_ttl = _number(minimum_token_ttl, maximum=300)
        self._store = store
        self._connections = MappingProxyType(configured)
        self._grants = MappingProxyType(grants)
        self._preparers = MappingProxyType(preparers)
        self._factory = msal_factory
        self._clock = clock

    @staticmethod
    def _copy_grants(mapping, configured):
        if not isinstance(mapping, Mapping):
            raise ConnectionError("An explicit identity/provider allowlist is required")
        grants = {}
        for identity, providers in mapping.items():
            identity = _uuid(identity)
            if not isinstance(providers, (set, frozenset, tuple, list)):
                raise ConnectionError("Invalid identity/provider allowlist")
            try:
                providers = frozenset(providers)
            except TypeError:
                raise ConnectionError("Invalid identity/provider allowlist") from None
            if not providers or not providers.issubset(configured):
                raise ConnectionError("Invalid identity/provider allowlist")
            grants[identity] = providers
        return grants

    def _authorize(self, provider_id, caller_id, caller_roles) -> ProviderConnection:
        if (not isinstance(provider_id, str) or not isinstance(caller_id, str)
                or not isinstance(caller_roles, (set, frozenset, tuple, list))
                or CONNECTION_USE not in caller_roles
                or provider_id not in self._grants.get(caller_id.lower(), ())):
            raise ConnectionDenied("Caller is not approved to use this connection")
        return self._connections[provider_id]

    def _app(self, spec, cache):
        try:
            return self._factory(
                spec.client_id,
                authority=f"https://login.microsoftonline.com/{spec.tenant_id}",
                token_cache=cache,
                timeout=self._timeout,
                instance_discovery=False,
                enable_pii_log=False,
                enable_broker_on_windows=False,
                enable_broker_on_mac=False,
                enable_broker_on_linux=False,
                enable_broker_on_wsl=False,
            )
        except Exception:
            raise ConnectionUnavailable("Delegated authentication service is unavailable") from None

    @staticmethod
    def _call(operation):
        try:
            return operation()
        except ConnectionError:
            raise
        except (ValueError, RuntimeError):
            # Includes MSAL state/nonce errors, which can contain sensitive claims.
            raise ConnectionRequired("Delegated authentication could not be validated") from None
        except Exception:
            raise ConnectionUnavailable("Delegated authentication service is unavailable") from None

    def _read(self, spec):
        try:
            return self._store.get("oauth-cache", spec.binding)
        except StateMissing:
            raise ConnectionRequired("The approved guest connection must be established") from None
        except StateError:
            raise ConnectionUnavailable("Protected connection storage is unavailable") from None

    def _write(self, spec, payload, etag):
        try:
            return self._store.put(
                "oauth-cache", spec.binding, payload, ttl=self._cache_ttl, etag=etag,
            )
        except StateConflict:
            raise
        except StateMissing:
            raise ConnectionRequired("The approved guest connection must be established") from None
        except StateError:
            raise ConnectionUnavailable("Protected connection storage is unavailable") from None

    @staticmethod
    def _flow_identity(spec, caller_id, state):
        return json.dumps([spec.binding, caller_id.lower(), state], separators=(",", ":"))

    def begin(
        self, provider_id: str, *, caller_id: str, caller_roles: frozenset[str],
        authenticated_guest_oid: str,
    ) -> AuthorizationStart:
        spec = self._authorize(provider_id, caller_id, caller_roles)
        if not isinstance(authenticated_guest_oid, str) or authenticated_guest_oid.lower() != spec.guest_oid:
            raise ConnectionDenied("Only the administrator-selected guest can connect")
        return self._initiate(spec, caller_id, "service")

    def prepare_flow(
        self, provider_id: str, *, preparer_id: str, preparer_roles: frozenset[str],
    ) -> AuthorizationStart:
        """Verified operators may initiate guest login, not impersonate the guest.

        The transport additionally enforces CONTOSO.Deploy and its exact admin/client
        allowlist. No guest authentication is asserted until the MSAL exchange.
        """
        spec = self._authorize_preparer(provider_id, preparer_id, preparer_roles)
        return self._initiate(spec, preparer_id, "operator")

    def _authorize_preparer(self, provider_id, preparer_id, preparer_roles):
        if (not isinstance(provider_id, str) or not isinstance(preparer_id, str)
                or not isinstance(preparer_roles, (set, frozenset, tuple, list))
                or DEPLOYMENT_ADMIN not in preparer_roles
                or provider_id not in self._preparers.get(preparer_id.lower(), ())):
            raise ConnectionDenied("Operator is not approved to prepare this connection")
        return self._connections[provider_id]

    def connection_status(
        self, provider_id: str, *, preparer_id: str, preparer_roles: frozenset[str],
    ) -> str:
        """Read stored guest proof only; never refresh or assert live token validity."""
        spec = self._authorize_preparer(provider_id, preparer_id, preparer_roles)
        record = self._read(spec)
        cache = self._load(spec, record)
        key = record.payload["account_key"]
        if (not _bounded_text(key.get("home_account_id"))
                or key.get("environment") != "login.microsoftonline.com"):
            raise ConnectionRequired("The stored connection identity is invalid")
        try:
            profiles = list(cache.search(
                msal.TokenCache.CredentialType.ACCOUNT, query={**key, "realm": spec.tenant_id},
            ))
            valid = any(p.get("local_account_id") == spec.guest_oid
                        and p.get("authority_type") == "MSSTS" for p in profiles)
        except (ValueError, TypeError, AttributeError, KeyError):
            raise ConnectionRequired("The stored connection cache is invalid") from None
        if not valid:
            raise ConnectionRequired("The stored connection guest proof is invalid")
        return "stored"

    def _initiate(self, spec, initiator_id, initiation_kind):
        try:
            previous_etag = self._store.get("oauth-cache", spec.binding).etag
        except StateMissing:
            previous_etag = None
        except StateError:
            raise ConnectionUnavailable("Protected connection storage is unavailable") from None
        cache = msal.SerializableTokenCache()
        app = self._app(spec, cache)
        state = secrets.token_urlsafe(32)
        flow = self._call(lambda: app.initiate_auth_code_flow(
            scopes=sorted(spec.scopes), redirect_uri=spec.redirect_uri, state=state,
            response_mode="form_post",
        ))
        if (not isinstance(flow, dict) or flow.get("state") != state
                or not _bounded_text(flow.get("nonce"))
                or not _bounded_text(flow.get("code_verifier"))
                or flow.get("redirect_uri") != spec.redirect_uri
                or not _bounded_text(flow.get("auth_uri"), 16384)):
            raise ConnectionRequired("Delegated authorization flow is invalid")
        try:
            uri = urlsplit(flow["auth_uri"])
            if uri.scheme != "https" or uri.hostname != "login.microsoftonline.com" or uri.username:
                raise ValueError
        except ValueError:
            raise ConnectionRequired("Delegated authorization flow is invalid") from None
        try:
            self._store.put(
                "oauth-flow", self._flow_identity(spec, initiator_id, state),
                {"binding": spec.binding, "flow": flow, "previous_etag": previous_etag,
                 "initiator_id": initiator_id.lower(), "initiation_kind": initiation_kind,
                 "state": state},
                ttl=self._flow_ttl,
            )
            if initiation_kind == "operator":
                self._store.put(
                    "oauth-flow", "callback:" + state,
                    {"binding": spec.binding, "provider_id": spec.provider_id,
                     "initiator_id": initiator_id.lower(),
                     "initiation_kind": initiation_kind, "state": state},
                    ttl=self._flow_ttl,
                )
        except StateError:
            raise ConnectionUnavailable("Protected connection storage is unavailable") from None
        return AuthorizationStart(flow["auth_uri"], state)

    @staticmethod
    def _claims(spec, claims):
        if (not isinstance(claims, dict)
                or not isinstance(claims.get("tid"), str)
                or not isinstance(claims.get("oid"), str)
                or claims["tid"].lower() != spec.tenant_id
                or claims["oid"].lower() != spec.guest_oid):
            raise ConnectionRequired("Delegated token does not identify the approved guest")

    @staticmethod
    def _account(spec, app, cache, expected_key=None):
        accounts = DelegatedConnectionBroker._call(app.get_accounts)
        if not isinstance(accounts, list):
            raise ConnectionRequired("The approved guest account is missing from the cache")
        matches = {}
        for account in accounts:
            if not isinstance(account, dict):
                continue
            home, environment = account.get("home_account_id"), account.get("environment")
            if not _bounded_text(home) or environment != "login.microsoftonline.com":
                continue
            key = {"home_account_id": home, "environment": environment}
            if expected_key is not None and key != expected_key:
                continue
            # get_accounts() in MSAL 1.35 groups B2B entries by home_account_id.
            # Its top-level local_account_id/realm may describe a different tenant.
            # Verify the selected tenant's raw cache profile, not the home OID.
            profiles = list(cache.search(
                msal.TokenCache.CredentialType.ACCOUNT,
                query={**key, "realm": spec.tenant_id},
            ))
            if any(p.get("local_account_id") == spec.guest_oid
                   and p.get("authority_type") == "MSSTS" for p in profiles):
                matches[home] = account
        if len(matches) != 1:
            raise ConnectionRequired("The cache does not contain the approved guest account")
        account = next(iter(matches.values()))
        return account, {
            "home_account_id": account["home_account_id"], "environment": account["environment"],
        }

    def _token(self, spec, result, started):
        if not isinstance(result, dict) or "error" in result:
            raise ConnectionRequired("The approved guest connection requires authentication")
        if result.get("token_type") != "Bearer" or not _bounded_text(result.get("access_token"), 16384):
            raise ConnectionRequired("Delegated authentication returned no usable bearer token")
        if "id_token_claims" in result:
            self._claims(spec, result["id_token_claims"])
        if "scope" in result:
            granted = result["scope"]
            if not isinstance(granted, str) or len(granted) > 8192:
                raise ConnectionRequired("Delegated token scope metadata is invalid")
            required = next(iter(spec.scopes))
            # Entra can return either a fully-qualified scope or a resource-local
            # scope. This exchange requested exactly one configured resource.
            if not {required, required.rsplit("/", 1)[1]}.intersection(granted.split()):
                raise ConnectionRequired("Delegated token lacks the approved invocation scope")
        try:
            duration = result.get("expires_in")
            if type(duration) not in (str, int, float) or isinstance(duration, bool):
                raise ValueError
            duration = float(duration)
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError
            expires = started + duration
            if "expires_on" in result:
                absolute = result["expires_on"]
                if type(absolute) not in (str, int, float) or isinstance(absolute, bool):
                    raise ValueError
                absolute = float(absolute)
                if not math.isfinite(absolute):
                    raise ValueError
                expires = min(expires, absolute)
            now = self._clock()
            if not math.isfinite(now) or not math.isfinite(expires) or expires <= now + self._minimum_ttl:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise ConnectionRequired("Delegated token has insufficient usable lifetime") from None
        # Target metadata is the trusted configured MSAL exchange, not a decoded
        # access-token JWT. Power Platform access tokens may be opaque.
        return ScopedToken(
            result["access_token"], spec.tenant_id, spec.audience, spec.scopes, expires,
        )

    @staticmethod
    def _payload(spec, cache, key):
        return {
            "binding": spec.binding, "tenant_id": spec.tenant_id, "guest_oid": spec.guest_oid,
            "account_key": key, "cache": cache.serialize(),
        }

    def complete(
        self, provider_id: str, *, caller_id: str, caller_roles: frozenset[str],
        auth_response: Mapping[str, str],
    ) -> None:
        spec = self._authorize(provider_id, caller_id, caller_roles)
        state = self._response_state(auth_response)
        saved = self._consume_flow(spec, caller_id, state, "service")
        self._exchange(spec, saved, auth_response)

    @staticmethod
    def _response_state(auth_response):
        state = auth_response.get("state") if isinstance(auth_response, Mapping) else None
        if not isinstance(state, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", state):
            raise ConnectionRequired("Delegated authorization state is invalid")
        return state

    def _consume(self, identity):
        try:
            return self._store.consume("oauth-flow", identity)
        except (StateMissing, StateConflict):
            raise ConnectionRequired("Delegated authorization flow has expired or was already used") from None
        except StateError:
            raise ConnectionUnavailable("Protected connection storage is unavailable") from None

    def complete_callback(self, *, auth_response: Mapping[str, str]) -> None:
        """Anonymous form-post continuation authorized only by protected state."""
        state = self._response_state(auth_response)
        index = self._consume("callback:" + state)
        if (set(index) != {"binding", "provider_id", "initiator_id", "initiation_kind", "state"}
                or index.get("state") != state or index.get("initiation_kind") != "operator"
                or not isinstance(index.get("provider_id"), str)
                or not isinstance(index.get("initiator_id"), str)):
            raise ConnectionRequired("Saved delegated callback authorization is invalid")
        provider_id, initiator_id = index["provider_id"], index["initiator_id"]
        if provider_id not in self._preparers.get(initiator_id, ()):
            raise ConnectionDenied("Operator is no longer approved to prepare this connection")
        spec = self._connections[provider_id]
        if index["binding"] != spec.binding:
            raise ConnectionRequired("Saved delegated callback configuration has changed")
        saved = self._consume_flow(spec, initiator_id, state, "operator")
        self._exchange(spec, saved, auth_response)

    def _consume_flow(self, spec, initiator_id, state, initiation_kind):
        saved = self._consume(self._flow_identity(spec, initiator_id, state))
        flow = saved.get("flow")
        if (set(saved) != {"binding", "flow", "previous_etag", "initiator_id",
                          "initiation_kind", "state"}
                or saved.get("binding") != spec.binding or not isinstance(flow, dict)
                or saved.get("initiator_id") != initiator_id.lower()
                or saved.get("initiation_kind") != initiation_kind or saved.get("state") != state
                or flow.get("state") != state or flow.get("redirect_uri") != spec.redirect_uri
                or not _bounded_text(flow.get("nonce")) or not _bounded_text(flow.get("code_verifier"))):
            raise ConnectionRequired("Saved delegated authorization flow is invalid")
        return saved

    def _exchange(self, spec, saved, auth_response):
        flow = saved["flow"]
        # Only OAuth response fields reach MSAL. No tenant/audience/scope from the
        # form is interpreted, including provider IDs masquerading as OAuth data.
        response = {}
        for key in ("state", "code", "error"):
            if key in auth_response:
                value = auth_response[key]
                if not _bounded_text(value, 16384):
                    raise ConnectionRequired("Delegated authorization response is invalid")
                response[key] = value
        if "code" not in response or "error" in response:
            raise ConnectionRequired("Delegated authorization requires a new sign-in")
        cache = msal.SerializableTokenCache()
        app = self._app(spec, cache)
        started = self._clock()
        result = self._call(lambda: app.acquire_token_by_auth_code_flow(
            flow, response, scopes=sorted(spec.scopes),
        ))
        self._token(spec, result, started)
        self._claims(spec, result.get("id_token_claims"))
        _, key = self._call(lambda: self._account(spec, app, cache))
        try:
            self._write(spec, self._payload(spec, cache, key), saved.get("previous_etag"))
        except StateConflict:
            raise ConnectionRequired("Connection changed during authorization; begin a new flow") from None

    def _load(self, spec, record):
        payload = record.payload
        if (payload.get("binding") != spec.binding or payload.get("tenant_id") != spec.tenant_id
                or payload.get("guest_oid") != spec.guest_oid
                or not isinstance(payload.get("cache"), str)
                or not isinstance(payload.get("account_key"), dict)
                or set(payload["account_key"]) != {"home_account_id", "environment"}):
            raise ConnectionRequired("The stored connection identity is invalid")
        cache = msal.SerializableTokenCache()
        try:
            cache.deserialize(payload["cache"])
            # deserialize() accepts JSON arrays; reject that invalid cache shape.
            if not isinstance(json.loads(payload["cache"]), dict):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise ConnectionRequired("The stored connection cache is invalid") from None
        return cache

    def get_token(
        self, provider_id: str, *, caller_id: str, caller_roles: frozenset[str],
    ) -> ScopedToken:
        spec = self._authorize(provider_id, caller_id, caller_roles)
        expected_key = None
        for attempt in range(2):
            record = self._read(spec)
            cache = self._load(spec, record)
            key = record.payload["account_key"]
            if expected_key is not None and key != expected_key:
                raise ConnectionRequired("The approved connection account changed concurrently")
            expected_key = key
            app = self._app(spec, cache)
            account, _ = self._call(lambda: self._account(spec, app, cache, key))
            started = self._clock()
            result = self._call(lambda: app.acquire_token_silent(
                scopes=sorted(spec.scopes), account=account,
            ))
            token = self._token(spec, result, started)
            self._call(lambda: self._account(spec, app, cache, key))
            try:
                # Always pin the version before returning, even on an AT cache hit.
                # A conflict reloads/reacquires once, never overwrites a stale cache.
                self._write(spec, self._payload(spec, cache, key), record.etag)
                return token
            except StateConflict:
                if attempt:
                    raise ConnectionUnavailable("Connection changed concurrently; retry later") from None
        raise ConnectionUnavailable("Connection changed concurrently; retry later")
