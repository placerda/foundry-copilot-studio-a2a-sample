"""Verify rendered console text, without telemetry exporters or network access."""

from contextlib import redirect_stderr
from dataclasses import replace
import io
import logging

import pytest
from azure.core.exceptions import AzureError

from contoso.router.core import STUDIOVariant
from contoso.providers.diagnostics import ERROR_CLASSES, STAGES, STATUSES, log_stage
from contoso.providers.tests.test_transports import block_network, entry


def expected_line(stage, status, *, provider="studio-copilot-harness", error="none", http="none"):
    return (
        f"contoso_stage stage={stage} provider={provider} status={status} "
        f"error_class={error} http_status={http}"
    )


@pytest.fixture
def sdk_console(monkeypatch):
    from azure.ai.agentserver.core import _tracing

    root = logging.getLogger()
    original_level = root.level
    stream = io.StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(root, "handlers", [])
        patch.setattr(_tracing, "_SUPPRESSED_LOGGERS", ())
        patch.setattr(_tracing, "_configure_tracing", lambda **kwargs: None)
        try:
            with redirect_stderr(stream):
                _tracing.configure_observability()
            assert root.level == logging.INFO
            yield stream
        finally:
            for handler in root.handlers:
                handler.close()
            root.setLevel(original_level)


@pytest.mark.parametrize("stage", sorted(STAGES))
@pytest.mark.parametrize("status", sorted(STATUSES))
def test_actual_sdk_default_formatter_renders_all_fixed_fields(sdk_console, stage, status):
    logger = logging.getLogger("contoso.rendered_stage_test")
    log_stage(logger, entry(), stage, status)
    level = "WARNING" if status in {"failed", "cancelled", "response"} else "INFO"
    assert sdk_console.getvalue().endswith(
        f" {level} contoso.rendered_stage_test: {expected_line(stage, status)}\n"
    )


@pytest.mark.parametrize("variant,provider", [
    (STUDIOVariant.COPILOT_HARNESS, "studio-copilot-harness"),
    (STUDIOVariant.STANDARD, "studio-standard"),
])
def test_provider_is_fixed_label_not_catalog_identifier(sdk_console, variant, provider):
    configured = entry(variant)
    configured = replace(
        configured, provider_id="private-catalog-value",
        sources=tuple(replace(source, provider_id="private-catalog-value")
                      for source in configured.sources),
    )
    log_stage(logging.getLogger("contoso.rendered_stage_test"), configured, "provider_token", "start")
    rendered = sdk_console.getvalue()
    assert rendered.endswith(
        f": {expected_line('provider_token', 'start', provider=provider)}\n"
    )
    assert "private" not in rendered
    assert len(rendered.splitlines()) == 1


@pytest.mark.parametrize("http,expected", [(200, "200"), (403, "403"), (999, "other"),
                                         ("private\nhttp-value", "other")])
def test_http_field_is_bounded_even_for_invalid_values(sdk_console, http, expected):
    log_stage(logging.getLogger("contoso.rendered_stage_test"), entry(), "broker_http", "response",
              http_status=http)
    rendered = sdk_console.getvalue()
    assert rendered.endswith(f": {expected_line('broker_http', 'response', http=expected)}\n")
    assert "private" not in rendered
    assert len(rendered.splitlines()) == 1


def test_error_class_is_allowlisted_without_exception_text_or_traceback(sdk_console):
    class PrivateSubclass(AzureError):
        pass

    log_stage(logging.getLogger("contoso.rendered_stage_test"), entry(), "service_credential", "failed",
              error=PrivateSubclass("private-bearer-do-not-log"))
    rendered = sdk_console.getvalue()
    assert rendered.endswith(
        f": {expected_line('service_credential', 'failed', error='AzureError')}\n"
    )
    assert "private" not in rendered.lower()
    assert "PrivateSubclass" not in rendered
    assert len(rendered.splitlines()) == 1
    assert "AzureError" in ERROR_CLASSES


def test_unknown_error_class_uses_fixed_fallback(sdk_console):
    log_stage(logging.getLogger("contoso.rendered_stage_test"), entry(), "provider_transport", "failed",
              error=ValueError("private-error"))
    assert sdk_console.getvalue().endswith(
        f": {expected_line('provider_transport', 'failed', error='Other')}\n"
    )


@pytest.mark.parametrize("stage,status", [("private-stage", "start"), ("broker_http", "private-status")])
def test_unsupported_enums_are_not_logged(sdk_console, stage, status):
    with pytest.raises(ValueError, match="Unsupported diagnostic stage or status"):
        log_stage(logging.getLogger("contoso.rendered_stage_test"), entry(), stage, status)
    assert sdk_console.getvalue() == ""
