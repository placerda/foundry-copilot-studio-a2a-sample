import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import threading
import time
from types import SimpleNamespace

import pytest
from azure.core.exceptions import AzureError

from contoso.api_center import APICenterClient
from contoso.providers import AttributionError, ProviderTimeout, TransportError
from contoso.router.core import AccessDenied, Capability, CatalogError
from contoso.router.discovery import (
    APIMRoutePolicy, APIMServiceTokens, DiscoveredRouterRuntime, discovered_entries,
)
from contoso.router.runtime import DemoQuestionRouter


TENANT = "11111111-1111-4111-8111-111111111111"
API = "22222222-2222-4222-8222-222222222222"
BASE = "https://approved.azure-api.net/agents"
CATALOG = "https://approved.data.eastus2.azure-apicenter.ms"
POLICY = APIMRoutePolicy(BASE, TENANT, API)


def published(provider="logistics", version="1", *, studio=False, active=True):
    source = {
        "source_id": "MOTORS-LOGISTICS-001" if not studio else "MOTORS-SUPPLIER-001",
        "provider_id": provider, "version": version,
        "title": "Synthetic Motors source", "classification": "Synthetic demonstration data",
        "reference": "contoso-demo/knowledge/motors-source.md", "sections": ["Evidence limits"],
    }
    return {
        "name": provider, "title": "Contoso Motors demonstration", "kind": "rest",
        "lifecycleStage": "production",
        "customProperties": {"contoso-routing": {
            "schema_version": 1, "publication_state": "active" if active else "inactive",
            "provider_id": provider,
            "capability": "supplier-delivery-terms" if studio else "knowledge-knowledge",
            "studio_variant": "copilot-harness" if studio else None,
            "endpoint": POLICY.endpoint(provider, version),
            "protocol": "a2a", "protocol_version": "1.0", "agent_version": version,
            "sources": [source],
        }},
    }


class Credential:
    def __init__(self):
        self.scopes = []
        self.fail_for = None

    def get_token(self, scope):
        self.scopes.append(scope)
        if scope == self.fail_for:
            raise AzureError("private-credential-detail")
        return SimpleNamespace(token="private-managed-bearer", expires_on=time.time() + 3600)


class Response:
    history = ()

    def __init__(self, value, status=200):
        self.value = json.dumps(value).encode()
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size):
        yield self.value


class Backend:
    def __init__(self, inventories):
        self.inventories = iter(inventories)
        self.gets = []
        self.posts = []
        self.trust_env = True
        self.inventory_status = 200
        self.reply_override = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        self.current = next(self.inventories)
        return Response({"value": self.current}, self.inventory_status)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        metadata = next(
            record["customProperties"]["contoso-routing"] for record in self.current
            if record.get("customProperties", {}).get("contoso-routing", {}).get("endpoint") == url
        )
        source = metadata["sources"][0]
        text = self.reply_override or (
            f'Synthetic reference. {source["source_id"]}, Version {source["version"]}, '
            'section "Evidence limits". No real commitment is authorized.'
        )
        return Response({
            "jsonrpc": "2.0", "id": kwargs["json"]["id"],
            "result": {"message": {
                "messageId": "reply", "role": "ROLE_AGENT", "parts": [{"text": text}],
            }},
        })


def configured(*inventories):
    backend, credential = Backend(inventories), Credential()
    client = APICenterClient(CATALOG, credential, session_factory=lambda: backend)
    runtime = DiscoveredRouterRuntime(
        client, POLICY, {"verified-caller"},
        DemoQuestionRouter({
            "Return the racks": "knowledge-knowledge",
            "Explain the delivery": "supplier-delivery-terms",
            "Approve a purchase": None,
        }), credential, session_factory=lambda: backend,
    )
    return runtime, backend, credential


def run(runtime, question="Return the racks", **kwargs):
    return asyncio.run(runtime.run(
        question, caller_id=kwargs.pop("caller_id", "verified-caller"), **kwargs,
    ))


def test_catalog_update_changes_the_actual_http_destination_without_static_routes():
    first, second = published(), published("new-logistics", "2")
    runtime, backend, credential = configured([first], [second])
    initial = run(runtime)
    updated = run(runtime)
    assert initial.result.response.provider_id == "logistics"
    assert updated.result.response.provider_id == "new-logistics"
    assert updated.result.sources[0].version == "2"
    assert updated.result.response.citations[0].version == "2"
    assert len(backend.gets) == 2
    assert [url for url, _ in backend.posts] == [
        first["customProperties"]["contoso-routing"]["endpoint"],
        second["customProperties"]["contoso-routing"]["endpoint"],
    ]
    assert credential.scopes == [
        "https://azure-apicenter.net/.default", f"api://{API}/.default",
    ] * 2
    assert backend.trust_env is False
    for _, request in backend.posts:
        assert request["headers"] == {
            "Authorization": "Bearer private-managed-bearer", "A2A-Version": "1.0",
        }
        assert request["allow_redirects"] is False
        assert set(request["json"]["params"]) == {"message", "configuration"}
        assert request["json"]["params"]["configuration"]["returnImmediately"] is False
    assert backend.posts[0][1]["json"]["id"] != backend.posts[1][1]["json"]["id"]
    assert "Authenticated API Center catalog" in updated.path
    assert "private-managed-bearer" not in json.dumps(updated.to_dict())


def test_both_capabilities_use_only_the_apim_service_token_without_a_studio_broker():
    runtime, backend, credential = configured(
        [published(), published("supplier", studio=True)],
        [published(), published("supplier", studio=True)],
    )
    knowledge = run(runtime)
    supplier = run(runtime, "Explain the delivery")
    assert knowledge.result.response.variant is None
    assert supplier.result.response.variant.value == "copilot-harness"
    assert [url for url, _ in backend.posts] == [
        POLICY.endpoint("logistics", "1"), POLICY.endpoint("supplier", "1"),
    ]
    assert set(credential.scopes) == {
        "https://azure-apicenter.net/.default", f"api://{API}/.default",
    }
    assert all("/connections/" not in url for url, _ in backend.posts)


def test_explicit_no_studio_variant_survives_catalog_null_omission():
    record = published()
    metadata = record["customProperties"]["contoso-routing"]
    metadata["studio_variant"] = "none"
    runtime, backend, _ = configured([record])
    result = run(runtime)
    assert result.result.response.variant is None
    assert backend.posts[0][0] == POLICY.endpoint("logistics", "1")
    del metadata["studio_variant"]
    runtime, backend, credential = configured([record])
    with pytest.raises(CatalogError, match="routing metadata"):
        run(runtime)
    assert not backend.posts
    assert credential.scopes == ["https://azure-apicenter.net/.default"]


@pytest.mark.parametrize("variant", [None, "none"])
def test_supplier_cannot_use_an_explicit_no_studio_variant(variant):
    record = published("supplier", studio=True)
    record["customProperties"]["contoso-routing"]["studio_variant"] = variant
    runtime, backend, credential = configured([record])
    with pytest.raises(CatalogError, match="routing metadata"):
        run(runtime, "Explain the delivery")
    assert not backend.posts
    assert credential.scopes == ["https://azure-apicenter.net/.default"]


@pytest.mark.parametrize("records", [
    [], [published(active=False)], [{"name": "unrelated-petstore", "customProperties": {}}],
])
def test_empty_inactive_and_unmanaged_catalogs_are_no_match_without_backend_calls(records):
    runtime, backend, credential = configured(records)
    result = run(runtime)
    assert result.status == "no-matching-capability"
    assert result.to_dict()["sources"] == []
    assert len(backend.gets) == 1
    assert not backend.posts
    assert credential.scopes == ["https://azure-apicenter.net/.default"]


def test_missing_published_studio_is_no_match_not_a_fallback_to_logistics():
    runtime, backend, _ = configured([published()])
    assert run(runtime, "Explain the delivery").status == "no-matching-capability"
    assert not backend.posts


def test_inventory_failure_never_falls_back_to_a_previous_successful_route(caplog):
    runtime, backend, credential = configured([published()], [published()])
    run(runtime)
    backend.inventory_status = 503
    with pytest.raises(TransportError):
        run(runtime)
    assert len(backend.posts) == 1
    assert credential.scopes.count(f"api://{API}/.default") == 1
    assert "private-managed-bearer" not in caplog.text


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"publication_state": "unknown"},
    {"studio_variant": "standard"}, {"studio_variant": "copilot-harness"},
    {"studio_variant": ""}, {"studio_variant": "None"}, {"studio_variant": False},
    {"protocol": "foundry-responses"}, {"protocol_version": "0.3"},
    {"agent_version": "../1"}, {"agent_version": "2"},
    {"capability": "https://attacker.example"}, {"provider_id": "other-provider"},
    {"endpoint": "https://unapproved.azure-api.net/agents/logistics/versions/1/a2a"},
    {"endpoint": BASE + "/../admin"}, {"endpoint": POLICY.endpoint("logistics", "1") + "?bypass=1"},
    {"scopes": ["https://api.powerplatform.com/.default"]}, {"sources": []},
    {"sources": [{"sections": [["unhashable"]]}]},
])
def test_invalid_routing_metadata_fails_before_backend_authentication(changes, caplog):
    record = published()
    record["customProperties"]["contoso-routing"].update(deepcopy(changes))
    runtime, backend, credential = configured([record])
    with pytest.raises(CatalogError, match="routing metadata"):
        run(runtime)
    assert not backend.posts
    assert credential.scopes == ["https://azure-apicenter.net/.default"]
    assert "invalid_routing_metadata" in caplog.text


def test_ambiguous_routes_and_unpublished_lifecycle_are_not_selected():
    runtime, backend, _ = configured([published(), published("other-logistics")])
    with pytest.raises(CatalogError, match="Ambiguous"):
        run(runtime)
    assert not backend.posts
    record = published()
    record["lifecycleStage"] = "retired"
    with pytest.raises(CatalogError):
        discovered_entries([record], POLICY)


def test_arm_envelopes_cannot_be_mistaken_for_data_plane_inventory():
    record = published()
    with pytest.raises(CatalogError):
        discovered_entries([{"name": "arm-record", "properties": record}], POLICY)


@pytest.mark.parametrize("question,kwargs,error", [
    ("Return the racks", {"caller_id": "untrusted-caller"}, AccessDenied),
    ("", {}, CatalogError),
    ("x" * 2001, {}, CatalogError),
    ("Return the racks", {"variant": "standard"}, CatalogError),
])
def test_untrusted_or_invalid_input_never_queries_the_catalog(question, kwargs, error):
    runtime, backend, credential = configured([published()])
    with pytest.raises(error):
        run(runtime, question, **kwargs)
    assert not backend.gets and not backend.posts and not credential.scopes


def test_explicit_citations_are_required_even_after_authenticated_discovery():
    runtime, backend, _ = configured([published()])
    backend.reply_override = "A plausible but uncited answer."
    with pytest.raises(AttributionError):
        run(runtime)


def test_credential_failure_is_explicit_and_never_uses_an_operator_or_backend_credential(caplog):
    runtime, backend, credential = configured([published()])
    credential.fail_for = f"api://{API}/.default"
    with pytest.raises(TransportError) as error:
        run(runtime)
    assert not backend.posts
    assert "private-credential-detail" not in str(error.value) + caplog.text
    assert "credential_unavailable" in caplog.text


def test_token_client_rejects_a_bypassing_entry_before_acquisition():
    entry, = discovered_entries([published()], POLICY)
    credential = Credential()
    tokens = APIMServiceTokens(credential, POLICY)
    entry = replace(entry, endpoint="https://api.powerplatform.com/bypass")
    with pytest.raises(CatalogError):
        asyncio.run(tokens(entry))
    assert not credential.scopes


def test_total_deadline_also_bounds_catalog_work(monkeypatch):
    from contoso.router import discovery

    runtime, backend, _ = configured([published()])

    async def stalled():
        await asyncio.sleep(10)

    monkeypatch.setattr(runtime.client, "list_apis", stalled)
    monkeypatch.setattr(discovery, "REQUEST_BUDGET", 0.01)
    with pytest.raises(ProviderTimeout, match="total time budget"):
        run(runtime)
    assert not backend.posts


def test_cancelled_transport_retains_its_admission_slot_until_the_worker_finishes(monkeypatch):
    runtime, backend, _ = configured([published()], [published()])
    started, release, finished = (threading.Event() for _ in range(3))
    post = backend.post
    calls = []

    def blocked_post(url, **kwargs):
        calls.append(url)
        started.set()
        try:
            assert release.wait(5)
            return post(url, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(backend, "post", blocked_post)

    async def exercise():
        work = asyncio.create_task(runtime.run("Return the racks", caller_id="verified-caller"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            with pytest.raises(ProviderTimeout, match="busy"):
                await runtime.run("Return the racks", caller_id="verified-caller")
            assert len(calls) == 1
        finally:
            work.cancel()
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)

    asyncio.run(exercise())


@pytest.mark.parametrize("base", [
    "https://approved.azure-api.net", "https://approved.azure-api.net/agents/",
    "https://approved.azure-api.net/agents//nested", "https://approved.azure-api.net:443/agents",
    "https://approved.azure-api.net/agents?next=1", "https://approved.azure-api.net/agents/../ops",
    "https://approved.azure-api.net.evil.example/agents", "http://approved.azure-api.net/agents",
])
def test_administrative_apim_boundary_requires_a_canonical_origin_and_path(base):
    with pytest.raises(CatalogError):
        APIMRoutePolicy(base, TENANT, API)
