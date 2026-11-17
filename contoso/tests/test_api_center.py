import asyncio
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from azure.core.exceptions import AzureError

from contoso.api_center import APICenterClient
from contoso.identity_probe import build_catalog_probe_host
from contoso.providers import ProtocolError, TransportError
from contoso.router.core import AccessDenied


ORIGIN = "https://contoso.data.eastus2.azure-apicenter.ms"
NEXT = ORIGIN + "/workspaces/default/apis?api-version=2024-02-01-preview&skip=32"


class Credential:
    def __init__(self):
        self.scopes = []

    def get_token(self, scope):
        self.scopes.append(scope)
        return SimpleNamespace(token="private-service-bearer", expires_on=time.time() + 300)


class Response:
    history = ()

    def __init__(self, value=None, *, status=200, raw=None):
        self.status_code = status
        self.body = json.dumps(value).encode() if raw is None else raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size):
        yield self.body


class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def client_for(*responses):
    session, credential = Session(*responses), Credential()
    return APICenterClient(ORIGIN, credential, session_factory=lambda: session), session, credential


def test_authenticated_inventory_follows_same_origin_pagination_without_caching():
    first_page = [{"name": f"api-{index}"} for index in range(32)]
    second_page = [{"name": f"api-{index}"} for index in range(32, 64)]
    client, session, credential = client_for(
        Response({"value": first_page, "nextLink": NEXT}),
        Response({"value": second_page}),
        Response({"value": []}),
    )
    assert asyncio.run(client.list_apis()) == first_page + second_page
    assert asyncio.run(client.list_apis()) == []
    assert credential.scopes == ["https://azure-apicenter.net/.default"] * 2
    query = parse_qs(urlsplit(session.calls[0][0]).query)
    assert query["maxpagesize"] == ["32"]
    assert "top" not in query
    assert session.calls[1][0] == NEXT
    assert session.trust_env is False
    for _, request in session.calls:
        assert request["headers"]["Authorization"] == "Bearer private-service-bearer"
        assert request["allow_redirects"] is False
        assert request["stream"] is True


@pytest.mark.parametrize("next_link", [
    "https://attacker.example/apis?api-version=2024-02-01-preview",
    ORIGIN + "/other?api-version=2024-02-01-preview",
    NEXT + "&api-version=other",
    NEXT + "#fragment",
    "",
])
def test_untrusted_pagination_never_receives_the_service_token(next_link):
    client, session, _ = client_for(Response({"value": [], "nextLink": next_link}))
    with pytest.raises(ProtocolError):
        asyncio.run(client.list_apis())
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [301, 401, 403, 429, 503])
def test_empty_http_errors_are_not_parsed_as_json_or_treated_as_an_empty_catalog(status):
    client, session, _ = client_for(Response(status=status, raw=b""))
    with pytest.raises(TransportError, match="inventory is unavailable"):
        asyncio.run(client.list_apis())
    assert len(session.calls) == 1


@pytest.mark.parametrize("raw", [
    b'{"value":[],"value":[]}',
    b'{"value":{}}',
    b'{"value":[null]}',
    b'{"value":[],"nextLink":false}',
    b"x" * 131073,
], ids=["duplicate-fields", "wrong-value", "null-record", "invalid-link", "oversized"])
def test_malformed_or_oversized_inventory_fails_closed(raw):
    client, _, _ = client_for(Response(raw=raw))
    with pytest.raises(ProtocolError):
        asyncio.run(client.list_apis())


def test_cycles_and_page_limits_do_not_produce_a_partial_catalog():
    client, session, _ = client_for(
        Response({"value": [], "nextLink": NEXT}),
        Response({"value": [], "nextLink": NEXT}),
    )
    with pytest.raises(ProtocolError, match="cycle"):
        asyncio.run(client.list_apis())
    assert len(session.calls) == 2
    client, session, _ = client_for(*[
        Response({"value": [], "nextLink": NEXT + f"&page={index}"}) for index in range(4)
    ])
    with pytest.raises(ProtocolError, match="page limit"):
        asyncio.run(client.list_apis())
    assert len(session.calls) == 4


def test_credential_failures_never_reach_http_or_expose_token_service_details(caplog):
    client, session, credential = client_for()

    def fail(scope):
        raise AzureError("private-token-service-detail")

    credential.get_token = fail
    with pytest.raises(TransportError) as error:
        asyncio.run(client.list_apis())
    assert not session.calls
    assert "private-token-service-detail" not in str(error.value) + caplog.text


@pytest.mark.parametrize("caller,question", [
    ("unapproved-caller", "CONTOSO catalog access probe"),
    ("approved-caller", "Show catalog contents"),
])
def test_probe_rejects_unapproved_callers_and_business_inputs_before_acquiring_tokens(
    monkeypatch, caller, question,
):
    client, session, credential = client_for()
    monkeypatch.setattr("contoso.identity_probe.build_json_probe_host", lambda resolve: resolve)
    context = SimpleNamespace(
        user_id=caller, platform_headers=lambda: {"x-agent-foundry-call-id": "verified-call"},
    )
    resolve = build_catalog_probe_host(
        client, allowed_callers={"approved-caller"}, context_factory=lambda: context,
    )
    with pytest.raises(AccessDenied):
        asyncio.run(resolve({"input": question}, asyncio.Event()))
    assert not credential.scopes and not session.calls


def test_probe_returns_only_access_evidence_and_respects_cancellation(monkeypatch):
    client, session, credential = client_for(Response({"value": [{"name": "private-record"}]}))
    monkeypatch.setattr("contoso.identity_probe.build_json_probe_host", lambda resolve: resolve)
    context = SimpleNamespace(
        user_id="approved-caller", platform_headers=lambda: {"x-agent-foundry-call-id": "verified-call"},
    )
    resolve = build_catalog_probe_host(
        client, allowed_callers={"approved-caller"}, context_factory=lambda: context,
    )
    cancelled = asyncio.Event()
    cancelled.set()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(resolve({"input": "CONTOSO catalog access probe"}, cancelled))
    assert not credential.scopes
    result = asyncio.run(resolve({"input": "CONTOSO catalog access probe"}, asyncio.Event()))
    assert result == {
        "status": "catalog-access-proof", "inventory_count": 1, "knowledge_access": False,
    }
    assert "private-record" not in json.dumps(result)
    assert "private-service-bearer" not in json.dumps(result)
