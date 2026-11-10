import asyncio
import json
from pathlib import Path
from html.parser import HTMLParser

import pytest

from contoso.configuration import (
    HarnessRouterSettings, RouterSettings, demo_router, identity_link_transport, platform_callers,
)
from contoso.knowledge import ATTRIBUTION
from contoso.entrypoint import build_host
from contoso.router.core import AccessDenied, Capability, CatalogError, Protocol, VariantUnavailable
from contoso.providers import (
    FoundryResponsesTransport, RawReply, extract_citations, standard_catalog_endpoint,
)


def test_both_studio_routes_use_actual_sdk_discovery_and_exact_protocol(router_settings):
    settings = router_settings
    catalog = settings.catalog()
    standard = catalog.lookup("approved-service", "supplier-delivery-terms", variant="standard").entry
    harness = catalog.lookup(
        "approved-service", "supplier-delivery-terms", variant="copilot-harness",
    ).entry
    assert standard.endpoint == standard_catalog_endpoint(
        environment_id=settings.environment_id, agent_identifier=settings.standard_schema,
    )
    assert standard.protocol == Protocol.SDK_D2E
    assert harness.endpoint == settings.harness_endpoint
    assert harness.protocol == Protocol.A2A
    assert standard.auth.audience == "https://api.powerplatform.com"
    assert harness.auth.audience == "6f46e6ed-07bf-4616-80f0-47546850f654"
    with pytest.raises(AccessDenied):
        catalog.lookup("foreign-service", "supplier-delivery-terms", variant="standard")


def test_real_extracted_markdown_has_valid_attribution(router_settings):
    entry = router_settings.catalog().lookup("approved-service", "knowledge-knowledge").entry
    source = (Path(__file__).resolve().parents[2] / "contoso-demo" / "knowledge"
              / "knowledge-returnables.md").read_text(encoding="utf-8")
    reply = RawReply(
        text=source + ATTRIBUTION, provider_id=entry.provider_id, capability=entry.capability,
        variant=None, protocol=Protocol.A2A, channel="a2a", state="completed",
        endpoint=entry.endpoint,
    )
    citations = extract_citations(entry, reply)
    assert "Approved intervention" in {c.section for c in citations}
    assert all(c.source_id == "CONTOSO-DEMO-KNOWLEDGE-001" and c.version == "1" for c in citations)


def test_native_knowledge_endpoint_survives_deployment_configuration(router_env):
    endpoint = (
        "https://contoso.services.ai.azure.com/api/projects/project/agents/"
        "knowledge-demo/endpoint/protocols/a2a?api-version=v1"
    )
    router_env["CONTOSO_KNOWLEDGE_A2A_ENDPOINT"] = endpoint
    settings = RouterSettings.from_environment(router_env)
    assert settings.knowledge_endpoint == endpoint
    assert settings.catalog().lookup("approved-service", "knowledge-knowledge").entry.endpoint == endpoint


@pytest.mark.parametrize("name", [
    "CONTOSO_ENTRA_TENANT_ID", "CONTOSO_STUDIO_ENVIRONMENT_ID", "CONTOSO_GATEWAY_URL",
    "CONTOSO_STUDIO_HARNESS_ENDPOINT", "CONTOSO_KNOWLEDGE_A2A_ENDPOINT", "CONTOSO_PLATFORM_CALLER_IDS",
])
def test_missing_cloud_configuration_never_defaults(router_env, name):
    del router_env[name]
    with pytest.raises(CatalogError):
        RouterSettings.from_environment(router_env)


@pytest.mark.parametrize("value", ['[]', '["duplicate","duplicate"]', '"caller"', '[""]'])
def test_caller_configuration_is_explicit(value):
    with pytest.raises(CatalogError):
        platform_callers({"CONTOSO_PLATFORM_CALLER_IDS": value})


def test_registered_router_preserves_no_match_and_does_not_guess_intents():
    router = demo_router()
    assert asyncio.run(router("Approve the supplier's proposal to deliver 300 of 500 units Friday and the remaining 200 Monday.")) is None
    assert asyncio.run(router("A completely new supplier question")) is None
    assert asyncio.run(router(
        "What exact penalty will the buyer charge for delivering the remaining 200 units Monday?"
    )) == Capability.SUPPLIER_DELIVERY_TERMS


def test_browser_suggestions_match_registered_business_scenarios():
    root = Path(__file__).resolve().parents[2]
    scenarios = json.loads((root / "contoso-demo" / "scenarios.json").read_text())["scenarios"]
    configured = {scenario["question"]: scenario for scenario in scenarios}

    class Suggestions(HTMLParser):
        def __init__(self):
            super().__init__()
            self.prompts = []

        def handle_starttag(self, tag, attrs):
            prompt = dict(attrs).get("data-prompt")
            if tag == "button" and prompt is not None:
                self.prompts.append(prompt)

    suggestions = Suggestions()
    suggestions.feed((root / "frontend" / "index.html").read_text(encoding="utf-8"))
    assert len(suggestions.prompts) == 3
    assert len(set(suggestions.prompts)) == 3
    router = demo_router()
    for prompt in suggestions.prompts:
        assert prompt in configured
        assert asyncio.run(router(prompt)) == configured[prompt]["expected_capability"]
    no_match = configured[
        "Approve the supplier's proposal to deliver 300 of 500 units Friday and the remaining 200 Monday."
    ]
    assert no_match["expected_capability"] is None
    assert no_match["expected_source_id"] is None
    assert no_match["expected_source_file"] is None
    assert no_match["question"] in suggestions.prompts


def test_active_scenario_sources_and_catalog_metadata_are_consistent(router_settings):
    root = Path(__file__).resolve().parents[2]
    scenarios = json.loads((root / "contoso-demo" / "scenarios.json").read_text())["scenarios"]
    assert len(scenarios) == 4
    for scenario in scenarios:
        assert asyncio.run(demo_router()(scenario["question"])) == scenario["expected_capability"]
        if scenario["expected_capability"] is None:
            continue
        document = root / "contoso-demo" / scenario["expected_source_file"]
        text = document.read_text(encoding="utf-8")
        assert f"Source ID: {scenario['expected_source_id']}" in text
        assert "Version: 1" in text
        for variant in ("standard", "copilot-harness"):
            entry = router_settings.catalog().lookup(
                "approved-service", scenario["expected_capability"], variant=variant,
            ).entry
            metadata = entry.sources[0]
            assert metadata.source_id == scenario["expected_source_id"]
            assert metadata.version == "1"
            assert metadata.title in text


def test_actual_sdk_router_composition_registers_without_authentication_fallback(router_env):
    class UnusedCredential:
        def get_token(self, *scopes):
            raise AssertionError("Construction must not acquire tokens")

    assert build_host(router_env, UnusedCredential()) is not None


RESPONSES_ENDPOINT = (
    "https://contoso.services.ai.azure.com/api/projects/project/agents/"
    "knowledge-demo/endpoint/protocols/openai/responses?api-version=v1"
)


def test_business_catalog_stays_native_a2a_even_with_diagnostic_sdk_settings(router_env):
    router_env["CONTOSO_IDENTITY_LINK_TRANSPORT"] = "sdk"
    router_env["CONTOSO_KNOWLEDGE_RESPONSES_ENDPOINT"] = RESPONSES_ENDPOINT
    settings = RouterSettings.from_environment(router_env)
    selected = settings.catalog().lookup("approved-service", "knowledge-knowledge").entry
    assert selected.protocol == Protocol.A2A
    assert selected.endpoint == router_env["CONTOSO_KNOWLEDGE_A2A_ENDPOINT"]


@pytest.mark.parametrize("selection", ["sdk", "responses", "", "unsupported"])
def test_business_config_cannot_enable_responses_as_a2a_replacement(router_env, selection):
    router_env["CONTOSO_KNOWLEDGE_TRANSPORT"] = selection
    router_env["CONTOSO_KNOWLEDGE_RESPONSES_ENDPOINT"] = RESPONSES_ENDPOINT
    with pytest.raises(CatalogError, match="requires native A2A"):
        RouterSettings.from_environment(router_env)


def test_identity_link_sdk_is_explicit_and_never_implicit_fallback(router_env):
    assert identity_link_transport(router_env) == (
        Protocol.A2A, router_env["CONTOSO_KNOWLEDGE_A2A_ENDPOINT"],
    )
    router_env["CONTOSO_IDENTITY_LINK_TRANSPORT"] = "sdk"
    with pytest.raises(CatalogError):
        identity_link_transport(router_env)
    router_env["CONTOSO_KNOWLEDGE_RESPONSES_ENDPOINT"] = RESPONSES_ENDPOINT
    assert identity_link_transport(router_env) == (Protocol.FOUNDRY_RESPONSES, RESPONSES_ENDPOINT)
    router_env["CONTOSO_IDENTITY_LINK_TRANSPORT"] = "unsupported"
    with pytest.raises(CatalogError):
        identity_link_transport(router_env)


def test_diagnostic_sdk_composition_retains_only_explicit_gateway_allowlist(monkeypatch, router_env):
    from contoso import identity_link

    captured = {}

    class Probe:
        def __init__(self, credential, **kwargs):
            captured.update(kwargs)

        def build_host(self):
            return self

    class Credential:
        def get_token(self, *args):
            pytest.fail("Diagnostic host construction must not acquire tokens")

    monkeypatch.setattr(identity_link, "IdentityLinkProbe", Probe)
    router_env.update({
        "CONTOSO_RUNTIME_ROLE": "identity-link-probe",
        "CONTOSO_IDENTITY_LINK_TRANSPORT": "sdk",
        "CONTOSO_KNOWLEDGE_RESPONSES_ENDPOINT": RESPONSES_ENDPOINT,
        "CONTOSO_PLATFORM_CALLER_IDS": '["verified-gateway-only"]',
    })
    assert build_host(router_env, Credential()) is not None
    assert isinstance(captured["transport"], FoundryResponsesTransport)
    assert captured["allowed_callers"] == frozenset({"verified-gateway-only"})
    assert captured["endpoint"] == RESPONSES_ENDPOINT


def harness_only_env(router_env):
    return {
        key: value for key, value in router_env.items()
        if key in {
            "CONTOSO_RUNTIME_ROLE", "CONTOSO_ENTRA_TENANT_ID", "CONTOSO_ENTRA_API_CLIENT_ID",
            "CONTOSO_GATEWAY_URL", "CONTOSO_STUDIO_HARNESS_ENDPOINT", "CONTOSO_PLATFORM_CALLER_IDS",
        }
    } | {"CONTOSO_ROUTER_SCOPE": "studio-harness"}


def test_harness_only_catalog_has_no_standard_or_knowledge_route(router_env):
    settings = HarnessRouterSettings.from_environment(harness_only_env(router_env))
    catalog = settings.catalog()
    selected = catalog.lookup(
        "approved-service", "supplier-delivery-terms", variant="copilot-harness",
    ).entry
    assert selected.protocol == Protocol.A2A
    assert selected.provider_id == "studio-copilot-harness"
    assert selected.endpoint == settings.harness_endpoint
    assert selected.auth.audience == "6f46e6ed-07bf-4616-80f0-47546850f654"
    with pytest.raises(VariantUnavailable):
        catalog.lookup("approved-service", "supplier-delivery-terms", variant="standard")
    with pytest.raises(AccessDenied):
        catalog.lookup("approved-service", "knowledge-knowledge")
    with pytest.raises(AccessDenied):
        catalog.lookup("unapproved-service", "supplier-delivery-terms", variant="copilot-harness")


def test_harness_only_host_needs_no_deferred_configuration_or_operator_credentials(monkeypatch, router_env):
    from contoso import configuration, entrypoint

    class Credential:
        def get_token(self, *args):
            pytest.fail("Hosted construction cannot acquire credentials")

    def forbidden(*args, **kwargs):
        pytest.fail("Deferred providers must not be constructed in Harness-only scope")

    captured = {}

    class Host:
        def __init__(self, runtime, **kwargs):
            captured["runtime"] = runtime
            captured.update(kwargs)

        def build_host(self):
            return self

    monkeypatch.setattr(configuration, "standard_catalog_endpoint", forbidden)
    monkeypatch.setattr(entrypoint, "StandardSDKAdapter", forbidden)
    monkeypatch.setattr(entrypoint, "FoundryResponsesTransport", forbidden)
    monkeypatch.setattr(entrypoint, "HostedRouter", Host)
    assert build_host(harness_only_env(router_env), Credential()) is not None
    assert set(captured["runtime"].providers) == {"studio-copilot-harness"}
    assert captured["fixed_variant"].value == "copilot-harness"


@pytest.mark.parametrize("name", [
    "CONTOSO_ENTRA_TENANT_ID", "CONTOSO_ENTRA_API_CLIENT_ID", "CONTOSO_GATEWAY_URL",
    "CONTOSO_STUDIO_HARNESS_ENDPOINT", "CONTOSO_PLATFORM_CALLER_IDS",
])
def test_harness_only_required_settings_never_default(router_env, name):
    env = harness_only_env(router_env)
    del env[name]
    with pytest.raises(CatalogError):
        HarnessRouterSettings.from_environment(env)
