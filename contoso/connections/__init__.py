"""Protected Gateway delegated connections; no generic resource token vending."""

from .oauth import (
    AuthorizationStart,
    ConnectionDenied,
    ConnectionError,
    ConnectionRequired,
    ConnectionUnavailable,
    DelegatedConnectionBroker,
    ProviderConnection,
)

__all__ = [
    "AuthorizationStart", "ConnectionDenied", "ConnectionError",
    "ConnectionRequired", "ConnectionUnavailable", "DelegatedConnectionBroker",
    "ProviderConnection",
]
