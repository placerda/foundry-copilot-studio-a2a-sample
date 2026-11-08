"""Network-blocked broker diagnostics; no live credential or connection evidence."""

import asyncio
import json
import logging
import traceback
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from azure.core.exceptions import AzureError

from contoso.router.core import CatalogError
from contoso.providers import ProtocolError, TransportError
from contoso.providers.tests.test_transports import block_network, entry, StubResponse
from contoso.providers.tests.test_diagnostics import expected_line, sdk_console
from contoso.token_client import ProviderTokens


PRIVATE = "private-upstream-error-do-not-log"
SERVICE_TOKEN = "fake-service-bearer-do-not-log"
GUEST_TOKEN = "fake-guest-bearer-do-not-log"
LOGGER = "contoso.token_client"


def payload():
    configured = entry()
    return {
        "token": GUEST_TOKEN, "tenant_id": configured.auth.tenant_id,
        "audience": configured.auth.audience, "scopes": list(configured.auth.scopes),
        "expires_at": 9999999999,
    }


class Session:
    def __init__(self, response):
        self.trust_env = True
        self.post = Mock(return_value=response)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def broker(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    credential = SimpleNamespace(get_token=Mock(return_value=SimpleNamespace(token=SERVICE_TOKEN)))
    session = Session(StubResponse(json.dumps(payload()).encode()))
    factory = Mock(return_value=session)
    tokens = ProviderTokens(
        credential, tenant_id="test-tenant", gateway_url="https://broker.example.com",
        gateway_api_id="configured-api", session_factory=factory,
    )
    return SimpleNamespace(
        credential=credential, session=session, factory=factory, tokens=tokens,
    )


def messages(caplog):
    return [record.getMessage() for record in caplog.records if record.name == LOGGER]


def assert_safe(caplog):
    for private in (PRIVATE, SERVICE_TOKEN, GUEST_TOKEN, "broker.example.com", "configured-api"):
        assert private not in caplog.text
    assert all(record.exc_info is None for record in caplog.records if record.name == LOGGER)


def test_success_preserves_broker_contract_and_emits_only_stage_metadata(broker, caplog):
    result = asyncio.run(broker.tokens(entry()))
    assert result.token == GUEST_TOKEN
    broker.credential.get_token.assert_called_once_with("api://configured-api/.default")
    broker.factory.assert_called_once_with()
    call = broker.session.post.call_args
    assert call.args == ("https://broker.example.com/api/connections/token",)
    assert call.kwargs["json"] == {"provider_id": entry().provider_id}
    assert call.kwargs["headers"]["Authorization"] == f"Bearer {SERVICE_TOKEN}"
    assert call.kwargs["timeout"] == (5, 15)
    assert call.kwargs["allow_redirects"] is False
    assert call.kwargs["stream"] is True
    assert broker.session.trust_env is False
    assert messages(caplog) == [
        expected_line("service_credential", "start"),
        expected_line("service_credential", "complete"),
        expected_line("broker_http", "start"),
        expected_line("broker_http", "response", http="200"),
        expected_line("broker_http", "complete"),
        expected_line("broker_metadata", "start"),
        expected_line("broker_metadata", "complete"),
    ]
    assert_safe(caplog)


@pytest.mark.parametrize("stage,error", [
    ("service_credential", AzureError(PRIVATE)),
    ("broker_http", requests.ConnectTimeout(PRIVATE)),
])
def test_credential_and_broker_transport_failures_are_distinguishable(broker, caplog, stage, error):
    if stage == "service_credential":
        broker.credential.get_token.side_effect = error
    else:
        broker.session.post.side_effect = error
    with pytest.raises(TransportError) as caught:
        asyncio.run(broker.tokens(entry()))
    assert expected_line(stage, "failed", error=type(error).__name__) in messages(caplog)
    assert PRIVATE not in "".join(traceback.format_exception(caught.value))
    if stage == "service_credential":
        broker.factory.assert_not_called()
    assert_safe(caplog)


@pytest.mark.parametrize("status", [302, 401, 403, 409, 503])
def test_broker_rejection_logs_status_without_reading_private_body(broker, caplog, status):
    response = StubResponse(PRIVATE.encode(), status=status)
    response.iter_content = Mock(side_effect=AssertionError("Rejected body must not be read"))
    broker.session.post.return_value = response
    with pytest.raises(TransportError):
        asyncio.run(broker.tokens(entry()))
    assert expected_line("broker_http", "response", http=str(status)) in messages(caplog)
    assert expected_line("broker_http", "failed", error="TransportError") in messages(caplog)
    response.iter_content.assert_not_called()
    assert response.closed
    assert_safe(caplog)


@pytest.mark.parametrize("raw,error", [
    (b"not-json", ProtocolError),
    (b'{"token":"a","token":"b"}', ProtocolError),
    (b"[]", ProtocolError),
    (json.dumps({**payload(), "unexpected": PRIVATE}).encode(), ProtocolError),
    (json.dumps({**payload(), "tenant_id": PRIVATE}).encode(), CatalogError),
    (json.dumps({**payload(), "scopes": ["wrong-scope"]}).encode(), CatalogError),
    (json.dumps({**payload(), "expires_at": 0}).encode(), CatalogError),
])
def test_metadata_failures_retain_typed_error_without_logging_values(broker, caplog, raw, error):
    broker.session.post.return_value = StubResponse(raw)
    with pytest.raises(error):
        asyncio.run(broker.tokens(entry()))
    assert expected_line("broker_metadata", "failed", error=error.__name__) in messages(caplog)
    assert expected_line("broker_metadata", "complete") not in messages(caplog)
    assert_safe(caplog)


def test_oversize_body_is_an_http_read_failure_not_successful_metadata(broker, caplog):
    broker.session.post.return_value = StubResponse(b"x" * 32769)
    with pytest.raises(ProtocolError):
        asyncio.run(broker.tokens(entry()))
    assert expected_line("broker_http", "failed", error="ProtocolError") in messages(caplog)
    assert expected_line("broker_metadata", "start") not in messages(caplog)
    assert_safe(caplog)


@pytest.mark.parametrize("status", [200, 403])
def test_broker_messages_reach_actual_sdk_console_formatter(broker, sdk_console, status):
    broker.session.post.return_value.status_code = status
    if status == 200:
        asyncio.run(broker.tokens(entry()))
    else:
        with pytest.raises(TransportError):
            asyncio.run(broker.tokens(entry()))
    rendered = sdk_console.getvalue()
    lines = rendered.splitlines()
    assert len(lines) == (7 if status == 200 else 5)
    assert all(" contoso.token_client: contoso_stage " in line for line in lines)
    assert f": {expected_line('broker_http', 'response', http=str(status))}\n" in rendered
    for private in (PRIVATE, SERVICE_TOKEN, GUEST_TOKEN, "broker.example.com", "configured-api"):
        assert private not in rendered
