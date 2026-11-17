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
from contoso.router.core import AccessDenied, Capability, CatalogError, Protocol, STUDIOVariant, VariantUnavailable
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
    scenarios = json.loads(
        (root / "contoso-demo" / "motors-scenarios.json").read_text(encoding="utf-8"),
    )["scenarios"]
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
    assert len(suggestions.prompts) == 5
    assert len(set(suggestions.prompts)) == 5
    router = demo_router(motors=True)
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


def test_motors_questions_route_both_specialists_without_changing_legacy_questions():
    root = Path(__file__).resolve().parents[2]
    scenarios = json.loads(
        (root / "contoso-demo" / "motors-scenarios.json").read_text(encoding="utf-8"),
    )["scenarios"]
    router = demo_router(motors=True)
    legacy = demo_router()
    for scenario in scenarios:
        question = scenario["question"]
        assert asyncio.run(router(question)) == scenario["expected_capability"]
        if scenario["expected_capability"] is None:
            assert scenario["expected_source_id"] is None
            continue
        document = root / "contoso-demo" / scenario["expected_source_file"]
        assert f"Source ID: {scenario['expected_source_id']}" in document.read_text(encoding="utf-8")
        if scenario["expected_capability"] == "knowledge-knowledge":
            assert asyncio.run(legacy(question)) is None
    assert asyncio.run(router("Schedule transport and approve the purchase")) is None


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


@pytest.mark.parametrize("gateway_url", [
    "https://approved.azurewebsites.net", "https://approved.azure-api.net",
])
def test_harness_only_host_needs_no_deferred_configuration_or_operator_credentials(
    monkeypatch, router_env, gateway_url,
):
    from contoso import configuration, entrypoint

    class Credential:
        def get_token(self, *args):
            pytest.fail("Hosted construction cannot acquire credentials")

    def forbidden(*args, **kwargs):
        pytest.fail("Deferred providers must not be constructed in Harness-only scope")

    captured = {}
    token_client = entrypoint.ProviderTokens

    def tokens(credential, **kwargs):
        captured["tokens"] = token_client(credential, **kwargs)
        return captured["tokens"]

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
    monkeypatch.setattr(entrypoint, "ProviderTokens", tokens)
    env = {**harness_only_env(router_env), "CONTOSO_GATEWAY_URL": gateway_url}
    assert build_host(env, Credential()) is not None
    assert set(captured["runtime"].providers) == {"studio-copilot-harness"}
    assert captured["fixed_variant"].value == "copilot-harness"
    assert captured["tokens"].gateway_url == gateway_url
    assert captured["tokens"].gateway_api_id == env["CONTOSO_ENTRA_API_CLIENT_ID"]


@pytest.mark.parametrize("gateway_url", [
    "http://approved.azure-api.net",
    "https://approved.azure-api.net.evil.example",
    "https://evil.example",
    "https://approved.azure-api.net/api/connections/token",
    "https://user:invalid@approved.azure-api.net",
    "https://approved.azure-api.net?unexpected=value",
    "https://approved.azure-api.net#fragment",
    "https://approved.azure-api.net:443",
    "https://approved.azure-api.net:8443",
])
def test_harness_only_token_origin_rejects_unsafe_apim_configuration(router_env, gateway_url):
    class Credential:
        def get_token(self, *args):
            pytest.fail("Invalid configuration cannot acquire credentials")

    env = {**harness_only_env(router_env), "CONTOSO_GATEWAY_URL": gateway_url}
    with pytest.raises(CatalogError):
        build_host(env, Credential())


def test_catalog_does_not_use_the_harness_only_apim_token_facade(router_env):
    router_env["CONTOSO_GATEWAY_URL"] = "https://approved.azure-api.net"
    with pytest.raises(CatalogError, match="unapproved host"):
        RouterSettings.from_environment(router_env)


@pytest.mark.parametrize("name", [
    "CONTOSO_ENTRA_TENANT_ID", "CONTOSO_ENTRA_API_CLIENT_ID", "CONTOSO_GATEWAY_URL",
    "CONTOSO_STUDIO_HARNESS_ENDPOINT", "CONTOSO_PLATFORM_CALLER_IDS",
])
def test_harness_only_required_settings_never_default(router_env, name):
    env = harness_only_env(router_env)
    del env[name]
    with pytest.raises(CatalogError):
        HarnessRouterSettings.from_environment(env)


def test_discovery_composition_has_fixed_shared_variant_and_no_legacy_broker(monkeypatch, router_env):
    from contoso import entrypoint

    class Credential:
        def get_token(self, *args):
            pytest.fail("Construction cannot acquire credentials")

    def forbidden(*args, **kwargs):
        pytest.fail("Discovery scope cannot construct a legacy catalog or token broker")

    captured = {}

    class Host:
        def __init__(self, runtime, **kwargs):
            captured["runtime"] = runtime
            captured.update(kwargs)

        def build_host(self, *, teams_router=None):
            captured["teams_router"] = teams_router
            return self

    monkeypatch.setattr(entrypoint, "ProviderTokens", forbidden)
    monkeypatch.setattr(entrypoint.RouterSettings, "from_environment", forbidden)
    monkeypatch.setattr(entrypoint.HarnessRouterSettings, "from_environment", forbidden)
    monkeypatch.setattr(entrypoint, "HostedRouter", Host)
    env = {
        key: value for key, value in harness_only_env(router_env).items()
        if key not in {"CONTOSO_GATEWAY_URL", "CONTOSO_STUDIO_HARNESS_ENDPOINT"}
    } | {
        "CONTOSO_ROUTER_SCOPE": "api-center",
        "CONTOSO_APICENTER_ENDPOINT": "https://approved.data.eastus2.azure-apicenter.ms",
        "CONTOSO_APIM_A2A_BASE_URL": "https://approved.azure-api.net/agents",
    }
    assert build_host(env, Credential()) is not None
    assert captured["fixed_variant"].value == "copilot-harness"
    assert captured["overall_timeout"] < 70
    assert captured["teams_router"] is None
    assert captured["runtime"].policy.base_url == env["CONTOSO_APIM_A2A_BASE_URL"]
    root = Path(__file__).resolve().parents[2]
    scenarios = json.loads(
        (root / "contoso-demo" / "motors-scenarios.json").read_text(encoding="utf-8"),
    )["scenarios"]
    for scenario in scenarios:
        assert asyncio.run(captured["runtime"].classifier(scenario["question"])) == scenario["expected_capability"]
    assert captured["runtime"].catalog.lookup(
        "approved-service", "knowledge-knowledge",
    ).entry is None
    del env["CONTOSO_APIM_A2A_BASE_URL"]
    with pytest.raises(CatalogError):
        build_host(env, Credential())


@pytest.mark.parametrize("teams_callers", ['["teams-user"]', "[]", "", '["teams-user","teams-user"]'])
def test_teams_discovery_grants_are_explicit_and_never_expand_other_protocols(
    monkeypatch, router_env, teams_callers,
):
    from contoso import entrypoint

    class Host:
        def __init__(self, runtime, **kwargs):
            self.runtime = runtime
            self.fixed_variant = kwargs["fixed_variant"]

        def build_host(self, *, teams_router=None):
            self.teams_router = teams_router
            return self

    monkeypatch.setattr(entrypoint, "HostedRouter", Host)
    env = {
        **harness_only_env(router_env),
        "CONTOSO_ROUTER_SCOPE": "api-center",
        "CONTOSO_APICENTER_ENDPOINT": "https://approved.data.eastus2.azure-apicenter.ms",
        "CONTOSO_APIM_A2A_BASE_URL": "https://approved.azure-api.net/agents",
        "CONTOSO_TEAMS_PLATFORM_CALLER_IDS": teams_callers,
    }
    if teams_callers != '["teams-user"]':
        with pytest.raises(CatalogError):
            build_host(env, object())
        return
    host = build_host(env, object())
    host.runtime.catalog.require_caller("approved-service")
    host.teams_router.runtime.catalog.require_caller("teams-user")
    assert host.teams_router.fixed_variant == STUDIOVariant.COPILOT_HARNESS
    with pytest.raises(AccessDenied):
        host.runtime.catalog.require_caller("teams-user")
    with pytest.raises(AccessDenied):
        host.teams_router.runtime.catalog.require_caller("approved-service")
