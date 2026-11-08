from dataclasses import replace
import socket

import pytest

from contoso.router.core import (
    AccessDenied, AuthMetadata, AuthScheme, CallerGrant, Capability, Catalog,
    CatalogEntry, CatalogError, Citation, STUDIOVariant, Protocol, ProviderResponse,
    ResponseStatus, RouteStatus, SourceMetadata, VariantUnavailable,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Offline core must not access the network")
    monkeypatch.setattr(socket.socket, "connect", deny)


@pytest.fixture
def consumer():
    return CatalogEntry(
        provider_id="consumer-demo",
        capability=Capability.SUPPLIER_DELIVERY_TERMS,
        variant=STUDIOVariant.STANDARD,
        endpoint="https://consumer.example.com/configured-agent",
        protocol=Protocol.SDK_D2E,
        auth=AuthMetadata(AuthScheme.ENTRA_BEARER, "configured-tenant",
                          "configured-audience", frozenset({"invoke"})),
        sources=(SourceMetadata(
            "CONTOSO-DEMO-SUPPLIER-001", "consumer-demo", "1",
            "Supplier partial-delivery terms", "Synthetic demonstration data",
            "knowledge/supplier-delivery-terms.md",
            frozenset({"Buyer acceptance conditions", "Recommended next step",
                       "Evidence limits"}),
        ),),
    )


@pytest.fixture
def knowledge(consumer):
    return replace(
        consumer, provider_id="knowledge-demo",
        capability=Capability.KNOWLEDGE_KNOWLEDGE, variant=None, protocol=Protocol.A2A,
        endpoint="https://knowledge.example.com/a2a",
        sources=(SourceMetadata(
            "CONTOSO-DEMO-KNOWLEDGE-001", "knowledge-demo", "1", "Returnable packaging",
            "Synthetic demonstration data", "knowledge/knowledge-returnables.md",
            frozenset({"Approved intervention", "Evidence limits"}),
        ),),
    )


def make_catalog(*entries):
    return Catalog(
        entries,
        (CallerGrant("approved", frozenset(Capability)),
         CallerGrant("consumer-only", frozenset({Capability.SUPPLIER_DELIVERY_TERMS}))),
        frozenset({"https://consumer.example.com", "https://knowledge.example.com"}),
    )


@pytest.fixture
def catalog(consumer, knowledge):
    harness = replace(consumer, variant=STUDIOVariant.COPILOT_HARNESS,
                      protocol=Protocol.A2A,
                      endpoint="https://consumer.example.com/harness-a2a")
    return make_catalog(consumer, harness, knowledge)


@pytest.fixture
def response():
    return ProviderResponse(
        "consumer-demo", Capability.SUPPLIER_DELIVERY_TERMS, STUDIOVariant.STANDARD,
        ResponseStatus.ANSWERED, "Synthetic hypothesis; investigate cash outlay and pack size.",
        (Citation("CONTOSO-DEMO-SUPPLIER-001", "consumer-demo", "1",
                  "Buyer acceptance conditions"),),
    )


@pytest.mark.parametrize("variant,protocol,path", [
    ("standard", Protocol.SDK_D2E, "/configured-agent"),
    ("copilot-harness", Protocol.A2A, "/harness-a2a"),
])
def test_authorized_consumer_variant(catalog, variant, protocol, path):
    decision = catalog.lookup("consumer-only", "supplier-delivery-terms", variant=variant)
    assert decision.status == RouteStatus.MATCH
    assert decision.entry.protocol == protocol
    assert decision.entry.endpoint == f"https://consumer.example.com{path}"
    assert decision.entry.auth.audience == "configured-audience"
    assert decision.entry.sources[0].source_id == "CONTOSO-DEMO-SUPPLIER-001"


def test_authorized_knowledge(catalog):
    decision = catalog.lookup("approved", "knowledge-knowledge")
    assert decision.entry.provider_id == "knowledge-demo"
    assert decision.entry.variant is None


@pytest.mark.parametrize("caller,capability,variant", [
    ("unknown", "supplier-delivery-terms", "standard"),
    ("consumer-only", "knowledge-knowledge", None),
    ("unknown", "payroll", None),
])
def test_denies_callers(catalog, caller, capability, variant):
    with pytest.raises(AccessDenied):
        catalog.lookup(caller, capability, variant=variant)


def test_explicit_no_match(catalog):
    decision = catalog.lookup("approved", "payroll-tax-approval")
    assert decision.status == RouteStatus.NO_MATCH
    assert decision.entry is None


def test_missing_knowledge_route_is_no_match(consumer):
    assert make_catalog(consumer).lookup(
        "approved", "knowledge-knowledge").status == RouteStatus.NO_MATCH


@pytest.mark.parametrize("variant", [None, "", "unknown", "STANDARD", "https://evil.example.com"])
def test_requires_known_explicit_variant(catalog, variant):
    with pytest.raises(CatalogError):
        catalog.lookup("approved", "supplier-delivery-terms", variant=variant)


def test_does_not_fallback_to_standard(consumer):
    with pytest.raises(VariantUnavailable):
        make_catalog(consumer).lookup(
            "approved", "supplier-delivery-terms", variant="copilot-harness")


def test_rejects_variant_on_knowledge(catalog):
    with pytest.raises(CatalogError):
        catalog.lookup("approved", "knowledge-knowledge", variant="standard")


@pytest.mark.parametrize("endpoint", [
    "http://consumer.example.com", "https://user:secret@consumer.example.com",
    "https://consumer.example.com?token=secret", "https://consumer.example.com#fragment",
    "https://localhost/a2a", "https://agent.local", "https://agent.internal",
    "https://127.0.0.1/a2a", "https://10.0.0.1", "https://169.254.169.254",
    "https://[::1]", "https://consumer.example.com:99999",
    "https://consumer.example.com:0", "https://consumer.example.com\\@evil.example.com",
    "https://consumer.example.com/\nsecret", "https://%63onsumer.example.com",
    "file:///agent", "//consumer.example.com/a2a",
])
def test_invalid_endpoints(consumer, endpoint):
    with pytest.raises(CatalogError):
        replace(consumer, endpoint=endpoint)


@pytest.mark.parametrize("endpoint", [
    "https://unapproved.example.com/a2a",
    "https://consumer.example.com.evil.example.com/a2a",
    "https://consumer.example.com:8443/a2a",
])
def test_unapproved_origins_rejected(consumer, endpoint):
    with pytest.raises(CatalogError, match="origin is not approved"):
        make_catalog(replace(consumer, endpoint=endpoint))


def test_normalized_origin_preserves_configured_endpoint(consumer):
    entry = replace(consumer, endpoint="https://CONSUMER.example.com:443/a2a")
    assert make_catalog(entry).lookup(
        "approved", "supplier-delivery-terms", variant="standard").entry.endpoint == entry.endpoint


def test_native_foundry_a2a_preserves_required_version_query(knowledge):
    endpoint = (
        "https://contoso.services.ai.azure.com/api/projects/project/agents/"
        "knowledge-demo/endpoint/protocols/a2a?api-version=v1"
    )
    entry = replace(knowledge, endpoint=endpoint)
    catalog = Catalog(
        (entry,), (CallerGrant("approved", frozenset(Capability)),),
        frozenset({"https://contoso.services.ai.azure.com"}),
    )
    assert catalog.lookup("approved", "knowledge-knowledge").entry.endpoint == endpoint


@pytest.mark.parametrize("endpoint", [
    "https://contoso.services.ai.azure.com/api/projects/p/agents/a/endpoint/protocols/a2a?api-version=v2",
    "https://contoso.services.ai.azure.com/api/projects/p/agents/a/endpoint/protocols/a2a?api-version=v1&extra=1",
    "https://contoso.services.ai.azure.com/api/projects/p/agents/a/endpoint/protocols/a2a?api-version=v1&api-version=v1",
    "https://contoso.services.ai.azure.com/api/projects/p/agents/a/endpoint/protocols/a2a?%61pi-version=v1",
    "https://contoso.services.ai.azure.com/other?api-version=v1",
    "https://contoso.services.ai.azure.com/api/projects/../agents/a/endpoint/protocols/a2a?api-version=v1",
    "https://contoso.services.ai.azure.com.evil.example.com/api/projects/p/agents/a/endpoint/protocols/a2a?api-version=v1",
    "https://contoso.services.ai.azure.com:8443/api/projects/p/agents/a/endpoint/protocols/a2a?api-version=v1",
])
def test_native_version_query_does_not_allow_arbitrary_queries_or_origins(knowledge, endpoint):
    with pytest.raises(CatalogError):
        replace(knowledge, endpoint=endpoint)


def test_origin_allowlist_cannot_be_a_path(consumer):
    with pytest.raises(CatalogError):
        Catalog((consumer,), (), frozenset({"https://consumer.example.com/a2a"}))


@pytest.mark.parametrize("changes", [
    {"variant": "unknown"}, {"variant": None}, {"capability": "unknown"},
    {"protocol": Protocol.A2A}, {"auth": None},
    {"sources": ()}, {"provider_id": "wrong-provider"},
])
def test_invalid_entry_metadata(consumer, changes):
    with pytest.raises(CatalogError):
        replace(consumer, **changes)


def test_duplicate_entries_or_grants_rejected(consumer):
    with pytest.raises(CatalogError, match="Ambiguous"):
        make_catalog(consumer, consumer)
    grant = CallerGrant("approved", frozenset(Capability))
    with pytest.raises(CatalogError, match="Duplicate caller"):
        Catalog((), (grant, grant), frozenset())


def test_response_preserves_text_citations_and_source_metadata(catalog, response):
    result = catalog.validate_response(
        "approved", "supplier-delivery-terms", response, variant="standard")
    assert result.response is response
    assert result.response.answer == response.answer
    assert result.response.citations == response.citations
    assert result.sources[0].classification == "Synthetic demonstration data"
    assert result.sources[0].reference == "knowledge/supplier-delivery-terms.md"


@pytest.mark.parametrize("changes", [
    {"provider_id": "knowledge-demo"}, {"variant": STUDIOVariant.COPILOT_HARNESS},
    {"capability": Capability.KNOWLEDGE_KNOWLEDGE},
])
def test_response_route_attribution(catalog, response, changes):
    with pytest.raises(CatalogError, match="attribution"):
        catalog.validate_response("approved", "supplier-delivery-terms",
                                  replace(response, **changes), variant="standard")


@pytest.mark.parametrize("changes", [
    {"source_id": "CONTOSO-DEMO-KNOWLEDGE-001"},
    {"provider_id": "knowledge-demo"}, {"version": "2"}, {"section": "invented section"},
])
def test_rejects_mismatched_citations(catalog, response, changes):
    citation = replace(response.citations[0], **changes)
    with pytest.raises(CatalogError, match="Citation attribution"):
        catalog.validate_response("approved", "supplier-delivery-terms",
                                  replace(response, citations=(citation,)), variant="standard")


def test_response_authorization_rechecked(catalog, response):
    with pytest.raises(AccessDenied):
        catalog.validate_response("unknown", "supplier-delivery-terms", response, variant="standard")


def test_response_requires_matching_route(catalog, response):
    with pytest.raises(CatalogError, match="No matching"):
        catalog.validate_response("approved", "payroll", response)


def test_answer_requires_provenance(response):
    with pytest.raises(CatalogError):
        replace(response, citations=())


def test_unavailable_cannot_be_a_generated_success(catalog, response):
    with pytest.raises(CatalogError):
        replace(response, status=ResponseStatus.UNAVAILABLE)
    unavailable = replace(response, status=ResponseStatus.UNAVAILABLE,
                          answer=None, citations=())
    result = catalog.validate_response(
        "approved", "supplier-delivery-terms", unavailable, variant="standard")
    assert result.response.answer is None
    assert result.sources == ()


def test_insufficient_evidence_is_preserved(catalog, response):
    limited = replace(response, status=ResponseStatus.INSUFFICIENT_EVIDENCE,
                      answer="The synthetic source does not establish that percentage.")
    assert catalog.validate_response(
        "approved", "supplier-delivery-terms", limited, variant="standard").response is limited


def test_inputs_are_immutable_snapshots(consumer):
    scopes = {"invoke"}
    auth = AuthMetadata(AuthScheme.ENTRA_BEARER, "tenant", "audience", scopes)
    scopes.add("admin")
    assert auth.scopes == frozenset({"invoke"})
    assert isinstance(consumer.sources, tuple)


def test_catalog_retains_only_demo_metadata(consumer, knowledge):
    for entry in (consumer, knowledge):
        assert not hasattr(entry.sources[0], "content")
        assert entry.sources[0].classification == "Synthetic demonstration data"
