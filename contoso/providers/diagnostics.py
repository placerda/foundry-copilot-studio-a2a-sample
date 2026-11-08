"""Fixed, value-free stage messages for the protected console-log reader."""

import logging

from contoso.router.core import Capability, CatalogEntry, STUDIOVariant


STAGES = frozenset({
    "service_credential", "broker_http", "broker_metadata",
    "provider_token", "provider_transport", "source_attribution",
})
STATUSES = frozenset({"start", "complete", "response", "failed", "cancelled"})
ERROR_CLASSES = frozenset({
    "ClientAuthenticationError", "CredentialUnavailableError", "HttpResponseError",
    "ServiceRequestError", "ServiceResponseError", "AzureError",
    "ConnectTimeout", "ReadTimeout", "Timeout", "ConnectionError", "RequestException",
    "CatalogError", "AttributionError", "ProviderTimeout", "ResponseTooLarge",
    "ConfigurationError", "ProtocolError", "TransportError", "ProviderError",
    "CancelledError",
})


def _provider_label(entry: CatalogEntry) -> str:
    if entry.capability == Capability.KNOWLEDGE_KNOWLEDGE:
        return "knowledge"
    return {
        STUDIOVariant.COPILOT_HARNESS: "studio-copilot-harness",
        STUDIOVariant.STANDARD: "studio-standard",
    }.get(entry.variant, "unknown")


def log_stage(
    logger: logging.Logger, entry: CatalogEntry, stage: str, status: str, *,
    error: BaseException | None = None, http_status: int | None = None,
) -> None:
    if stage not in STAGES or status not in STATUSES:
        raise ValueError("Unsupported diagnostic stage or status")
    error_class = "none" if error is None else next(
        (cls.__name__ for cls in type(error).__mro__ if cls.__name__ in ERROR_CLASSES),
        "Other",
    )
    http = (
        "none" if http_status is None else
        str(http_status) if type(http_status) is int and 100 <= http_status <= 599 else "other"
    )
    level = (
        logging.WARNING if status in {"failed", "cancelled"}
        or (status == "response" and http_status != 200) else logging.INFO
    )
    logger.log(
        level, "contoso_stage stage=%s provider=%s status=%s error_class=%s http_status=%s",
        stage, _provider_label(entry), status, error_class, http,
    )
