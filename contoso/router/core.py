"""Own-code routing using administrator configuration, never model-provided URLs.

The caller identifier must come from an authenticated transport, not a prompt.
This module enforces catalog authorization but does not authenticate identities
or invoke providers. Catalogs contain source metadata, not source documents.
"""

from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address
import re
from urllib.parse import urlsplit


class CatalogError(ValueError):
    """Invalid or inconsistent administrator configuration or response."""


class AccessDenied(PermissionError):
    """The authenticated caller is not approved for this capability."""


class VariantUnavailable(LookupError):
    """The explicitly requested STUDIO variant is not configured."""


class Capability(StrEnum):
    SUPPLIER_DELIVERY_TERMS = "supplier-delivery-terms"
    KNOWLEDGE_KNOWLEDGE = "knowledge-knowledge"


class STUDIOVariant(StrEnum):
    STANDARD = "standard"
    COPILOT_HARNESS = "copilot-harness"


class Protocol(StrEnum):
    SDK_D2E = "sdk-d2e"
    A2A = "a2a"
    FOUNDRY_RESPONSES = "foundry-responses"


class AuthScheme(StrEnum):
    ENTRA_BEARER = "entra-bearer"


class RouteStatus(StrEnum):
    MATCH = "match"
    NO_MATCH = "no-matching-capability"


class ResponseStatus(StrEnum):
    ANSWERED = "answered"
    INSUFFICIENT_EVIDENCE = "insufficient-evidence"
    UNAVAILABLE = "unavailable"


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{label} must be nonempty text")


def _enum(value, expected, label: str) -> None:
    if not isinstance(value, expected):
        raise CatalogError(f"Unknown {label}")


def _https_origin(url: str) -> str:
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        raise CatalogError("Endpoint must be an HTTPS URL")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise CatalogError("Invalid endpoint") from None
    native_hosted_version = (
        hostname is not None
        and hostname.lower().endswith(".services.ai.azure.com")
        and port in (None, 443)
        and parsed.query == "api-version=v1"
        and re.fullmatch(
            r"/api/projects/[A-Za-z0-9][A-Za-z0-9._-]*/agents/"
            r"[A-Za-z0-9][A-Za-z0-9._-]*/endpoint/protocols/(?:a2a|openai/responses)",
            parsed.path,
        ) is not None
    )
    if (parsed.scheme != "https" or not hostname or parsed.username is not None
            or parsed.password is not None
            or (parsed.query and not native_hosted_version) or parsed.fragment
            or "\\" in url or "%" in parsed.netloc):
        raise CatalogError("Endpoint requires HTTPS without credentials, query, or fragment")
    hostname = hostname.lower()
    try:
        address = ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if (len(labels) < 2 or hostname.endswith((".localhost", ".local", ".internal"))
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                       for label in labels)):
            raise CatalogError("Endpoint must have a public DNS name")
    else:
        if not address.is_global:
            raise CatalogError("Endpoint cannot target a private or reserved address")
        hostname = f"[{hostname}]" if address.version == 6 else hostname
    if port == 0:
        raise CatalogError("Invalid endpoint port")
    suffix = f":{port}" if port not in (None, 443) else ""
    return f"https://{hostname}{suffix}"


@dataclass(frozen=True)
class AuthMetadata:
    scheme: AuthScheme
    tenant_id: str
    audience: str
    scopes: frozenset[str]

    def __post_init__(self):
        _enum(self.scheme, AuthScheme, "auth scheme")
        _text(self.tenant_id, "Tenant ID")
        _text(self.audience, "Audience")
        object.__setattr__(self, "scopes", frozenset(self.scopes))
        if not self.scopes:
            raise CatalogError("At least one required scope must be configured")
        for scope in self.scopes:
            _text(scope, "Scope")


@dataclass(frozen=True)
class SourceMetadata:
    source_id: str
    provider_id: str
    version: str
    title: str
    classification: str
    reference: str
    sections: frozenset[str]

    def __post_init__(self):
        for label in ("source_id", "provider_id", "version", "title", "classification", "reference"):
            _text(getattr(self, label), label)
        object.__setattr__(self, "sections", frozenset(self.sections))
        if not self.sections:
            raise CatalogError("Source must declare citable sections")
        for section in self.sections:
            _text(section, "Section")


@dataclass(frozen=True)
class CatalogEntry:
    provider_id: str
    capability: Capability
    variant: STUDIOVariant | None
    endpoint: str
    protocol: Protocol
    auth: AuthMetadata
    sources: tuple[SourceMetadata, ...]

    def __post_init__(self):
        _text(self.provider_id, "Provider ID")
        _enum(self.capability, Capability, "capability")
        _enum(self.protocol, Protocol, "protocol")
        if not isinstance(self.auth, AuthMetadata):
            raise CatalogError("Auth metadata is required")
        _https_origin(self.endpoint)
        if self.capability == Capability.SUPPLIER_DELIVERY_TERMS:
            _enum(self.variant, STUDIOVariant, "STUDIO variant")
            expected = (Protocol.SDK_D2E if self.variant == STUDIOVariant.STANDARD
                        else Protocol.A2A)
            if self.protocol != expected:
                raise CatalogError("STUDIO variant/protocol mismatch")
        elif self.variant is not None:
            raise CatalogError("STUDIO variant applies only to supplier-delivery-terms")
        object.__setattr__(self, "sources", tuple(self.sources))
        if not self.sources or any(not isinstance(s, SourceMetadata) for s in self.sources):
            raise CatalogError("Source metadata is required")
        if any(s.provider_id != self.provider_id for s in self.sources):
            raise CatalogError("Source/provider mismatch")
        if len({s.source_id for s in self.sources}) != len(self.sources):
            raise CatalogError("Duplicate source ID")


@dataclass(frozen=True)
class CallerGrant:
    caller_id: str
    capabilities: frozenset[Capability]

    def __post_init__(self):
        _text(self.caller_id, "Caller ID")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        for capability in self.capabilities:
            _enum(capability, Capability, "granted capability")


@dataclass(frozen=True)
class RouteDecision:
    status: RouteStatus
    entry: CatalogEntry | None = None


@dataclass(frozen=True)
class Citation:
    source_id: str
    provider_id: str
    version: str
    section: str


@dataclass(frozen=True)
class ProviderResponse:
    provider_id: str
    capability: Capability
    variant: STUDIOVariant | None
    status: ResponseStatus
    answer: str | None
    citations: tuple[Citation, ...]

    def __post_init__(self):
        _text(self.provider_id, "Provider ID")
        _enum(self.capability, Capability, "response capability")
        _enum(self.status, ResponseStatus, "response status")
        if self.variant is not None:
            _enum(self.variant, STUDIOVariant, "STUDIO variant")
        object.__setattr__(self, "citations", tuple(self.citations))
        if any(not isinstance(c, Citation) for c in self.citations):
            raise CatalogError("Invalid citation")
        if self.status == ResponseStatus.UNAVAILABLE:
            if self.answer is not None or self.citations:
                raise CatalogError("Unavailable providers cannot supply a success answer")
        else:
            _text(self.answer, "Answer")
        if self.status == ResponseStatus.ANSWERED and not self.citations:
            raise CatalogError("Answers require provenance")


@dataclass(frozen=True)
class RouterResponse:
    """Verified attribution; provider text is preserved, not certified as factual."""

    response: ProviderResponse
    sources: tuple[SourceMetadata, ...]


class Catalog:
    def __init__(
        self,
        entries: tuple[CatalogEntry, ...],
        grants: tuple[CallerGrant, ...],
        approved_origins: frozenset[str],
    ):
        self._entries = {}
        self._grants = {}
        origins = set()
        for origin in approved_origins:
            normalized = _https_origin(origin)
            if urlsplit(origin).path not in ("", "/"):
                raise CatalogError("Approved origins cannot contain paths")
            origins.add(normalized)
        for entry in entries:
            if not isinstance(entry, CatalogEntry):
                raise CatalogError("Invalid catalog entry")
            if _https_origin(entry.endpoint) not in origins:
                raise CatalogError("Endpoint origin is not approved")
            key = (entry.capability, entry.variant)
            if key in self._entries:
                raise CatalogError("Ambiguous capability/variant")
            self._entries[key] = entry
        for grant in grants:
            if not isinstance(grant, CallerGrant):
                raise CatalogError("Invalid caller grant")
            if grant.caller_id in self._grants:
                raise CatalogError("Duplicate caller grant")
            self._grants[grant.caller_id] = grant.capabilities

    def require_caller(self, caller_id: str) -> None:
        if caller_id not in self._grants:
            raise AccessDenied("Caller is not approved")

    def lookup(
        self, caller_id: str, capability: str, *, variant: str | None = None
    ) -> RouteDecision:
        """Resolve an explicit capability, not a free-text intent or URL.

        A trusted upstream classifier can propose a capability. Unknown labels
        return no-match; absent or invalid STUDIO selections fail without fallback.
        """
        self.require_caller(caller_id)
        try:
            selected = STUDIOVariant(variant) if variant is not None else None
        except (ValueError, TypeError):
            raise CatalogError("Unknown STUDIO variant") from None
        try:
            requested = Capability(capability)
        except (ValueError, TypeError):
            return RouteDecision(RouteStatus.NO_MATCH)
        if requested not in self._grants[caller_id]:
            raise AccessDenied("Caller is not approved for this capability")
        if requested == Capability.SUPPLIER_DELIVERY_TERMS and selected is None:
            raise CatalogError("An explicit STUDIO variant is required")
        if requested == Capability.KNOWLEDGE_KNOWLEDGE and selected is not None:
            raise CatalogError("STUDIO variant applies only to supplier-delivery-terms")
        entry = self._entries.get((requested, selected))
        if entry is None:
            if requested == Capability.SUPPLIER_DELIVERY_TERMS:
                raise VariantUnavailable("Requested STUDIO variant is not configured")
            return RouteDecision(RouteStatus.NO_MATCH)
        return RouteDecision(RouteStatus.MATCH, entry)

    def validate_response(
        self, caller_id: str, capability: str, response: ProviderResponse,
        *, variant: str | None = None,
    ) -> RouterResponse:
        """Bind provider output to a freshly authorized configured route."""
        decision = self.lookup(caller_id, capability, variant=variant)
        if decision.status != RouteStatus.MATCH:
            raise CatalogError("No matching provider for response")
        entry = decision.entry
        if (not isinstance(response, ProviderResponse)
                or response.provider_id != entry.provider_id
                or response.capability != entry.capability
                or response.variant != entry.variant):
            raise CatalogError("Response attribution does not match the selected route")
        sources = {source.source_id: source for source in entry.sources}
        cited_sources = {}
        for citation in response.citations:
            source = sources.get(citation.source_id)
            if (source is None or citation.provider_id != source.provider_id
                    or citation.version != source.version
                    or citation.section not in source.sections):
                raise CatalogError("Citation attribution does not match configured source")
            cited_sources[source.source_id] = source
        return RouterResponse(response, tuple(cited_sources.values()))
