"""Per-request API Center routing with an administrator-owned APIM boundary."""

import asyncio
from dataclasses import dataclass, field, replace
import logging
import re
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

import requests
from azure.core.exceptions import AzureError

from contoso.bounded import run_bounded_sync
from contoso.providers import A2ATransport, ProviderTimeout, TransportError
from contoso.providers.common import reply
from .core import (
    AuthMetadata, AuthScheme, CallerGrant, Capability, Catalog, CatalogEntry,
    CatalogError, Protocol, SourceMetadata, STUDIOVariant, _https_origin,
)
from .runtime import RouterRuntime, ScopedToken, validate_runtime_request


_logger = logging.getLogger(__name__)
REQUEST_BUDGET = 65
_ROUTE_FIELDS = {
    "schema_version", "publication_state", "provider_id", "capability",
    "studio_variant", "endpoint", "protocol", "protocol_version", "agent_version",
    "sources",
}
_SOURCE_FIELDS = {
    "source_id", "provider_id", "version", "title", "classification", "reference",
    "sections",
}
_IDENTIFIER = r"[a-z0-9][a-z0-9-]{0,62}"
_VERSION = r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*"


def _bounded_text(value, limit):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > limit or any(ord(char) < 32 for char in value)):
        raise CatalogError("API Center routing metadata contains invalid text")
    return value


@dataclass(frozen=True)
class APIMRoutePolicy:
    base_url: str
    tenant_id: str
    api_client_id: str
    origin: str = field(init=False)
    auth: AuthMetadata = field(init=False)

    def __post_init__(self):
        origin = _https_origin(self.base_url)
        parsed = urlsplit(self.base_url)
        if (not parsed.hostname.endswith(".azure-api.net") or parsed.port is not None
                or not re.fullmatch(r"/[a-z0-9][a-z0-9/-]{0,127}", parsed.path)
                or "//" in parsed.path or parsed.path.endswith("/")
                or origin + parsed.path != self.base_url):
            raise CatalogError("An explicit canonical APIM origin and base path are required")
        try:
            tenant = str(UUID(self.tenant_id))
            client = str(UUID(self.api_client_id))
        except (ValueError, TypeError, AttributeError):
            raise CatalogError("APIM tenant and API application IDs must be UUIDs") from None
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "auth", AuthMetadata(
            AuthScheme.ENTRA_BEARER, tenant, client, frozenset({f"api://{client}/.default"}),
        ))

    def endpoint(self, provider_id, version):
        if (not re.fullmatch(_IDENTIFIER, _bounded_text(provider_id, 63))
                or not re.fullmatch(_VERSION, _bounded_text(version, 64))):
            raise CatalogError("The published APIM provider or version is invalid")
        return f"{self.base_url}/{provider_id}/versions/{version}/a2a"

    def require_entry(self, entry):
        if (not isinstance(entry, CatalogEntry) or entry.protocol != Protocol.A2A
                or entry.auth != self.auth):
            raise CatalogError("The route does not use the authorized APIM service identity")
        expected = (
            re.escape(self.base_url) + "/" + re.escape(entry.provider_id)
            + r"/versions/(" + _VERSION + r")/a2a"
        )
        if (not re.fullmatch(_IDENTIFIER, entry.provider_id)
                or len(entry.endpoint) > 2048
                or not re.fullmatch(expected, entry.endpoint)
                or entry.variant not in (None, STUDIOVariant.COPILOT_HARNESS)):
            raise CatalogError("The route is outside the authorized versioned APIM path")


def _source(value):
    if not isinstance(value, dict) or set(value) != _SOURCE_FIELDS:
        raise CatalogError("The published source metadata is invalid")
    for key in _SOURCE_FIELDS - {"sections"}:
        _bounded_text(value[key], 1024 if key == "reference" else 256)
    sections = value["sections"]
    if (not isinstance(sections, list) or not 1 <= len(sections) <= 16
            or any(not isinstance(section, str) for section in sections)
            or len(set(sections)) != len(sections)):
        raise CatalogError("The published source sections are invalid")
    for section in sections:
        _bounded_text(section, 128)
    if (not re.fullmatch(_VERSION, value["version"])
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", value["source_id"])):
        raise CatalogError("The published source identity or version is invalid")
    return SourceMetadata(**{**value, "sections": frozenset(sections)})


def discovered_entries(records, policy: APIMRoutePolicy):
    entries, providers = [], set()
    try:
        if not isinstance(records, list) or len(records) > 128:
            raise CatalogError("The API Center inventory is invalid")
        for record in records:
            if not isinstance(record, dict):
                raise CatalogError("The API Center record is invalid")
            if "properties" in record:
                raise CatalogError("An ARM envelope is not an API Center data-plane record")
            custom = record.get("customProperties", {})
            if not isinstance(custom, dict):
                raise CatalogError("The API Center custom properties are invalid")
            if "contoso-routing" not in custom:
                continue
            metadata = custom["contoso-routing"]
            if (not isinstance(metadata, dict) or set(metadata) != _ROUTE_FIELDS
                    or type(metadata["schema_version"]) is not int
                    or metadata["schema_version"] != 1
                    or metadata["publication_state"] not in ("active", "inactive")
                    or metadata["protocol"] != "a2a" or metadata["protocol_version"] != "1.0"
                    or not isinstance(metadata["sources"], list)
                    or not 1 <= len(metadata["sources"]) <= 8):
                raise CatalogError("The published routing schema is invalid")
            _bounded_text(record.get("name"), 128)
            if metadata["endpoint"] != policy.endpoint(
                metadata["provider_id"], metadata["agent_version"],
            ):
                raise CatalogError("The published endpoint does not match its provider and version")
            entry = CatalogEntry(
                metadata["provider_id"], Capability(metadata["capability"]),
                STUDIOVariant(metadata["studio_variant"])
                if metadata["studio_variant"] not in (None, "none") else None,
                metadata["endpoint"], Protocol.A2A, policy.auth,
                tuple(_source(source) for source in metadata["sources"]),
            )
            policy.require_entry(entry)
            if metadata["publication_state"] == "inactive":
                continue
            if record.get("lifecycleStage") not in ("preview", "production"):
                raise CatalogError("An active route requires a published API lifecycle stage")
            if entry.provider_id in providers:
                raise CatalogError("The active provider identity is ambiguous")
            providers.add(entry.provider_id)
            entries.append(entry)
    except (ValueError, TypeError):
        _logger.warning("service=api_center outcome=invalid_routing_metadata")
        raise CatalogError("API Center routing metadata is invalid") from None
    return tuple(entries)


class APIMServiceTokens:
    def __init__(self, credential, policy):
        self.credential = credential
        self.policy = policy
        self._slot = threading.BoundedSemaphore(1)

    async def __call__(self, entry):
        self.policy.require_entry(entry)

        def acquire():
            token = self.credential.get_token(next(iter(self.policy.auth.scopes)))
            scoped = ScopedToken(
                token.token, self.policy.auth.tenant_id, self.policy.auth.audience,
                self.policy.auth.scopes, token.expires_on,
            )
            scoped.require(entry, time.time() + 60)
            return scoped

        try:
            async with asyncio.timeout(8):
                return await run_bounded_sync(self._slot, acquire)
        except TimeoutError:
            _logger.warning("service=apim outcome=credential_timeout")
            raise ProviderTimeout("The APIM service credential exceeded its time budget") from None
        except AzureError:
            _logger.warning("service=apim outcome=credential_unavailable")
            raise TransportError("The APIM service credential is unavailable") from None


class APIMA2AProvider:
    def __init__(self, policy, *, session_factory=requests.Session):
        self.policy = policy
        self.session_factory = session_factory
        self._slot = threading.BoundedSemaphore(1)

    async def answer(self, entry, question, token):
        self.policy.require_entry(entry)
        transport = A2ATransport(
            endpoint=entry.endpoint, approved_origins=frozenset({self.policy.origin}),
            connect_timeout=3, read_timeout=30, overall_timeout=40,
            max_response_bytes=131072, session_factory=self.session_factory,
            worker_slot=self._slot,
        )
        text = await transport.invoke(question, token)
        return reply(entry, text, channel="a2a", state=None)


class DiscoveredRouterRuntime:
    def __init__(self, client, policy, callers, classifier, credential, *,
                 session_factory=requests.Session):
        self.client = client
        self.policy = policy
        self.classifier = classifier
        self.grants = tuple(
            CallerGrant(caller, frozenset(Capability)) for caller in sorted(callers)
        )
        self.catalog = Catalog((), self.grants, frozenset({policy.origin}))
        self.tokens = APIMServiceTokens(credential, policy)
        self.provider = APIMA2AProvider(policy, session_factory=session_factory)

    async def run(self, question, *, caller_id, variant=None):
        self.catalog.require_caller(caller_id)
        validate_runtime_request(question, variant)
        if variant not in (None, STUDIOVariant.COPILOT_HARNESS):
            raise CatalogError("Only the shared Studio variant is available")
        try:
            async with asyncio.timeout(REQUEST_BUDGET):
                entries = discovered_entries(await self.client.list_apis(), self.policy)
                try:
                    catalog = Catalog(entries, self.grants, frozenset({self.policy.origin}))
                except CatalogError:
                    _logger.warning("service=api_center outcome=ambiguous_routing_metadata")
                    raise
                published = {entry.capability for entry in entries}

                async def classify(text):
                    capability = await self.classifier(text)
                    if capability is None:
                        return None
                    try:
                        selected = Capability(capability)
                    except (ValueError, TypeError):
                        raise CatalogError("Classifier returned an unsupported capability") from None
                    return selected if selected in published else None

                runtime = RouterRuntime(
                    catalog, classify,
                    {entry.provider_id: self.provider for entry in entries}, self.tokens,
                )
                result = await runtime.run(
                    question, caller_id=caller_id, variant=STUDIOVariant.COPILOT_HARNESS,
                )
                return replace(result, path=tuple(
                    "Authenticated API Center catalog" if step == "Demo capability catalog" else step
                    for step in result.path
                ))
        except TimeoutError:
            _logger.warning("service=router outcome=total_budget_exceeded")
            raise ProviderTimeout("The discovered route exceeded its total time budget") from None
