import asyncio
import json
from types import SimpleNamespace

import pytest

from contoso.router.core import CatalogError
from contoso.network import FoundryNetworkClient
from contoso.providers import ProtocolError


NO_MATCH = {
    "status": "no-matching-capability",
    "answer": "No approved agent matches this request. No knowledge provider was called.",
    "provider_id": None, "studio_variant": None, "sources": [], "citations": [],
    "execution_path": ["Authenticated caller", "Demo capability catalog", "No matching capability"],
}


class Credential:
    def __init__(self):
        self.scopes = []

    def get_token(self, *scopes):
        self.scopes.append(scopes)
        return SimpleNamespace(token="owned-service-token", expires_on=9999999999)


class SDKProject:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.responses = self
        self.calls = []
        self.reply = NO_MATCH

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get_openai_client(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps(self.reply))


class A2A:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []

    async def invoke(self, text, token):
        self.calls.append((json.loads(text), token))
        return json.dumps(NO_MATCH)


def make(settings):
    project = SDKProject()
    rpc = A2A()
    credential = Credential()
    network = FoundryNetworkClient(
        credential,
        project_endpoint="https://selected-foundry.services.ai.azure.com/api/projects/demo",
        sdk_agent_name="contoso-foundry-sdk-agent",
        a2a_endpoint=settings.knowledge_endpoint,
        catalog=settings.catalog(), catalog_caller_id="approved-service",
        project_factory=lambda **kwargs: project,
        a2a_factory=lambda **kwargs: rpc,
    )
    return network, project, rpc, credential


def test_sdk_invokes_named_real_agent_with_stateless_envelope(router_settings):
    network, project, rpc, credential = make(router_settings)
    result = asyncio.run(network.invoke("Payroll?", "standard", "sdk"))
    assert result == NO_MATCH
    assert project.calls[0]["agent_name"] == "contoso-foundry-sdk-agent"
    assert project.calls[1]["store"] is False
    assert json.loads(project.calls[1]["input"]) == {"question": "Payroll?", "studio_variant": "standard"}
    assert rpc.calls == []
    assert credential.scopes == []


def test_a2a_uses_only_separate_gateway_service_token(router_settings):
    network, project, rpc, credential = make(router_settings)
    assert asyncio.run(network.invoke("Payroll?", "copilot-harness", "a2a")) == NO_MATCH
    assert rpc.calls == [
        ({"question": "Payroll?", "studio_variant": "copilot-harness"}, "owned-service-token"),
    ]
    assert credential.scopes == [("https://ai.azure.com/.default",)]
    assert project.calls == []


@pytest.mark.parametrize("integration,variant", [("unknown", "standard"), ("sdk", "both")])
def test_explicit_invalid_selection_makes_no_network_calls(router_settings, integration, variant):
    network, project, rpc, credential = make(router_settings)
    with pytest.raises(CatalogError):
        asyncio.run(network.invoke("Question", variant, integration))
    assert project.calls == rpc.calls == credential.scopes == []


def test_duplicate_result_fields_are_not_accepted(router_settings):
    network, *_ = make(router_settings)
    with pytest.raises(ProtocolError, match="duplicate"):
        network._validate('{"status":"answered","status":"no-matching-capability"}')


def test_answer_provenance_must_match_actual_catalog(router_settings):
    network, *_ = make(router_settings)
    payload = {
        "status": "answered", "answer": "A synthetic insight.",
        "provider_id": "studio-standard", "studio_variant": "standard",
        "sources": [{
            "source_id": "CONTOSO-DEMO-SUPPLIER-001", "provider_id": "studio-standard",
            "version": "1", "title": "Supplier partial-delivery terms",
            "classification": "Synthetic demonstration data",
        }],
        "citations": [{
            "source_id": "CONTOSO-DEMO-SUPPLIER-001", "version": "1",
            "section": "Buyer acceptance conditions",
        }],
        "execution_path": ["Authenticated caller", "studio-standard via sdk-d2e"],
    }
    assert network._validate(json.dumps(payload)) == payload
    payload["sources"][0]["title"] = "A fabricated source title"
    with pytest.raises(ProtocolError, match="differs"):
        network._validate(json.dumps(payload))
