"""Outgoing STUDIO adapters. Authentication and attribution remain caller concerns."""

from .a2a import A2AAdapter, A2ATransport
from .citations import AttributionError, extract_citations
from .common import (
    ConfigurationError, ProtocolError, ProviderError, ProviderTimeout, RawReply,
    ResponseTooLarge, TransportError,
)
from .standard import StandardSDKAdapter, standard_catalog_endpoint
from .responses import FoundryResponsesAdapter, FoundryResponsesTransport

__all__ = [
    "A2AAdapter", "StandardSDKAdapter", "RawReply", "ProviderError",
    "ConfigurationError", "ProtocolError", "ProviderTimeout",
    "ResponseTooLarge", "TransportError",
    "AttributionError", "extract_citations",
    "A2ATransport",
    "standard_catalog_endpoint",
    "FoundryResponsesAdapter", "FoundryResponsesTransport",
]
