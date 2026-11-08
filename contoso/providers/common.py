"""Shared transport contracts; raw text does not imply verified provenance."""

from dataclasses import dataclass
from collections.abc import Callable, Mapping
import math

from contoso.router.core import (
    Capability, Catalog, CatalogEntry, CatalogError, STUDIOVariant, Protocol,
    _https_origin,
)


class ProviderError(Exception):
    """Safe error suitable for callers; never contains upstream error details."""


class ConfigurationError(ProviderError):
    pass


class TransportError(ProviderError):
    pass


class ProtocolError(ProviderError):
    pass


class ProviderTimeout(TransportError):
    pass


class ResponseTooLarge(ProtocolError):
    pass


@dataclass(frozen=True)
class RawReply:
    text: str
    provider_id: str
    capability: Capability
    variant: STUDIOVariant | None
    protocol: Protocol
    channel: str
    state: str | None
    endpoint: str
    environment_id: str | None = None
    agent_identifier: str | None = None
    conversation_id: str | None = None


def validate_call(
    entry: CatalogEntry, question: str, token: str,
    approved_origins: frozenset[str], protocol: Protocol, variant: STUDIOVariant | None,
    scope: str | None, *, capability: Capability = Capability.SUPPLIER_DELIVERY_TERMS,
) -> None:
    if not isinstance(entry, CatalogEntry):
        raise ConfigurationError("A configured catalog entry is required")
    try:
        Catalog((entry,), (), approved_origins)
    except CatalogError:
        raise ConfigurationError("Provider catalog configuration is invalid") from None
    if (entry.capability != capability
            or entry.protocol != protocol or entry.variant != variant):
        raise ConfigurationError("Provider protocol or variant is unsupported")
    if scope is not None and scope not in entry.auth.scopes:
        raise ConfigurationError("Provider scope is not configured")
    validate_text_token(question, token)


def validate_text_token(question: str, token: str) -> None:
    if not isinstance(question, str) or not question.strip():
        raise ConfigurationError("Question must be nonempty text")
    if (not isinstance(token, str) or not token.strip()
            or any(c.isspace() or ord(c) < 32 or ord(c) > 126 for c in token)):
        raise ConfigurationError("An in-memory bearer token is required")


def validate_limits(*timeouts: float, max_response_bytes: int) -> None:
    if any(isinstance(t, bool) or not isinstance(t, (int, float))
           or not math.isfinite(t) or t <= 0 for t in timeouts):
        raise ConfigurationError("Timeouts must be finite positive numbers")
    if (isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int) or max_response_bytes <= 0):
        raise ConfigurationError("Response byte limit must be a positive integer")


def validate_origins(origins: frozenset[str]) -> frozenset[str]:
    if not isinstance(origins, frozenset) or not origins:
        raise ConfigurationError("Approved provider origins must be configured")
    try:
        Catalog((), (), origins)
    except CatalogError:
        raise ConfigurationError("Approved provider origins are invalid") from None
    return origins


def validate_endpoint(endpoint: str, approved_origins: frozenset[str]) -> None:
    """Reuse core URL/origin rules without inventing a knowledge-source entry."""
    try:
        origin = _https_origin(endpoint)
        approved = {_https_origin(value) for value in approved_origins}
    except CatalogError:
        raise ConfigurationError("Configured endpoint is invalid") from None
    if origin not in approved:
        raise ConfigurationError("Configured endpoint origin is not approved")


def platform_headers(factory: Callable[[], Mapping[str, str]] | None) -> dict[str, str]:
    if factory is None:
        return {}
    if not callable(factory):
        raise ConfigurationError("Platform headers factory is invalid")
    try:
        headers = factory()
    except (OSError, RuntimeError, ValueError, TypeError, LookupError):
        raise ConfigurationError("Verified platform call context is unavailable") from None
    if (not isinstance(headers, Mapping)
            or set(headers) != {"x-agent-foundry-call-id"}):
        raise ConfigurationError("Platform headers contain missing or unsupported fields")
    value = headers["x-agent-foundry-call-id"]
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or any(ord(char) < 32 or ord(char) > 126 for char in value)):
        raise ConfigurationError("Platform call context header is invalid")
    return {"x-agent-foundry-call-id": value}


def reply(
    entry: CatalogEntry, text: str, *, channel: str, state: str | None,
    environment_id: str | None = None, agent_identifier: str | None = None,
    conversation_id: str | None = None,
) -> RawReply:
    if entry.capability == Capability.SUPPLIER_DELIVERY_TERMS and entry.variant is None:
        raise ConfigurationError("STUDIO variant is required")
    return RawReply(
        text=text, provider_id=entry.provider_id, capability=entry.capability,
        variant=entry.variant, protocol=entry.protocol, channel=channel,
        state=state, endpoint=entry.endpoint, environment_id=environment_id,
        agent_identifier=agent_identifier, conversation_id=conversation_id,
    )
