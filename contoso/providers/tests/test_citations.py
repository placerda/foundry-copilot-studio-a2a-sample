"""Attribution syntax tests only; no live transport or source retrieval."""

from dataclasses import replace
import logging
import traceback

import pytest

from contoso.router.core import (
    AuthMetadata, AuthScheme, Capability, CatalogEntry, Citation, STUDIOVariant,
    Protocol, SourceMetadata,
)
from contoso.providers import AttributionError, RawReply, extract_citations
from contoso.providers.a2a import A2A_SCOPE
from contoso.providers.citations import AttributionReason, log_attribution_rejection
from contoso.providers.standard import STANDARD_SCOPE
from contoso.providers.tests.test_diagnostics import sdk_console
from contoso.providers.tests.test_transports import block_network


SECTIONS = (
    "Fictional proposal", "Buyer acceptance conditions",
    "Recommended next step", "Evidence limits",
)


def configured_entry():
    return CatalogEntry(
        provider_id="demo-studio", capability=Capability.SUPPLIER_DELIVERY_TERMS,
        variant=STUDIOVariant.COPILOT_HARNESS, protocol=Protocol.A2A,
        endpoint="https://api.powerplatform.com/copilotstudio-a2a/test/agents/demo",
        auth=AuthMetadata(AuthScheme.ENTRA_BEARER, "demo-tenant",
                          "https://api.powerplatform.com", frozenset({A2A_SCOPE})),
        sources=(SourceMetadata(
            "CONTOSO-DEMO-SUPPLIER-001", "demo-studio", "1",
            "Supplier partial-delivery terms ...", "contoso-demo", "demo-reference", frozenset(SECTIONS),
        ),),
    )


def raw(text, entry=None):
    selected = entry or configured_entry()
    return RawReply(
        text, selected.provider_id, selected.capability, selected.variant,
        selected.protocol, "native-a2a", "TASK_STATE_COMPLETED", selected.endpoint,
    )


@pytest.mark.parametrize("id_text", ["CONTOSO-DEMO-SUPPLIER-001", "**CONTOSO-DEMO-SUPPLIER-001**"])
@pytest.mark.parametrize("section_text", [
    '"Fictional proposal"', "“Fictional proposal”", "‘Fictional proposal’",
    "**Fictional proposal**", "Fictional proposal",
])
def test_formats_extract_exact_source_version_section_without_changing_text(id_text, section_text):
    text = f'{id_text}, "Supplier partial-delivery terms ...," Version 1; Sections: {section_text}.'
    response = raw(text)
    assert extract_citations(configured_entry(), response) == (
        Citation("CONTOSO-DEMO-SUPPLIER-001", "demo-studio", "1", "Fictional proposal"),
    )
    assert response.text == text


def test_all_supported_sections_multiline_and_bold_version():
    text = (
        '**CONTOSO-DEMO-SUPPLIER-001**, “Supplier partial-delivery terms ...,” **Version 1**\n'
        'Sections: **Fictional proposal**, “Buyer acceptance conditions”;\n'
        '‘Recommended next step’ and **Evidence limits**.'
    )
    citations = extract_citations(configured_entry(), raw(text))
    assert tuple(c.section for c in citations) == SECTIONS


def test_optional_title_and_implicit_section_list():
    text = 'CONTOSO-DEMO-SUPPLIER-001, Version 1: “Evidence limits”.'
    assert extract_citations(configured_entry(), raw(text))[0].section == "Evidence limits"


def test_version_sentence_punctuation_before_section_field():
    text = 'CONTOSO-DEMO-SUPPLIER-001, Version 1.\nSections: “Recommended next step”.'
    assert extract_citations(configured_entry(), raw(text))[0].section == "Recommended next step"


def test_explicit_version_prefix_in_config_preserves_configured_version():
    selected = configured_entry()
    selected = replace(selected, sources=(replace(selected.sources[0], version="Version 1"),))
    text = 'CONTOSO-DEMO-SUPPLIER-001, Version 1; Section: "Recommended next step".'
    assert extract_citations(selected, raw(text, selected))[0].version == "Version 1"


def test_duplicate_citations_are_deduplicated():
    citation = 'CONTOSO-DEMO-SUPPLIER-001, Version 1; Section: "Evidence limits".'
    assert len(extract_citations(configured_entry(), raw(citation + "\n\n" + citation))) == 1


@pytest.mark.parametrize("text", [
    "No attribution.",
    "CONTOSO-DEMO-SUPPLIER-001",
    'CONTOSO-DEMO-SUPPLIER-001, Version 1',
    'CONTOSO-DEMO-SUPPLIER-001, Version 1; Section: "Unsupported".',
    'CONTOSO-DEMO-SUPPLIER-001, Version 1; Sections: "Fictional proposal", "Unsupported".',
    'CONTOSO-DEMO-SUPPLIER-001, Version 1; Section: "Fictional proposal extended".',
    'CONTOSO-DEMO-SUPPLIER-001, Version 1; Section: Fictional proposal extended.',
    'CONTOSO-DEMO-SUPPLIER-001, Version 2; Section: "Fictional proposal".',
    'CONTOSO-DEMO-SUPPLIER-001, Version 10; Section: "Fictional proposal".',
    'CONTOSO-DEMO-SUPPLIER-001, Version v1; Section: "Fictional proposal".',
    'CONTOSO-DEMO-CONSUMER-999, Version 1; Section: "Fictional proposal".',
    'CONTOSO-DEMO-SUPPLIER-001-FAKE, Version 1; Section: "Fictional proposal".',
    'CONTOSO-DEMO-SUPPLIER-001, Version 1.\n\nUnrelated “Fictional proposal” heading.',
    'CONTOSO-DEMO-SUPPLIER-001, Version 1; Sections: "fictional insight".',
])
def test_missing_mismatched_and_unsupported_attribution_fails_closed(text):
    with pytest.raises(AttributionError):
        extract_citations(configured_entry(), raw(text))


def test_multiple_sources_cannot_borrow_version_or_sections():
    selected = configured_entry()
    second = replace(selected.sources[0], source_id="CONTOSO-DEMO-CONSUMER-002",
                     version="2", sections=frozenset({"Second section"}))
    selected = replace(selected, sources=selected.sources + (second,))
    text = (
        'CONTOSO-DEMO-SUPPLIER-001, Version 1; Section: "Fictional proposal"; '
        'CONTOSO-DEMO-CONSUMER-002, Version 2; Section: "Second section".'
    )
    assert tuple(c.source_id for c in extract_citations(selected, raw(text, selected))) == (
        "CONTOSO-DEMO-SUPPLIER-001", "CONTOSO-DEMO-CONSUMER-002",
    )
    with pytest.raises(AttributionError):
        extract_citations(selected, raw(text.replace('"Second section"', '"Fictional proposal"'), selected))


@pytest.mark.parametrize("field,value", [
    ("provider_id", "wrong-provider"), ("capability", Capability.KNOWLEDGE_KNOWLEDGE),
    ("variant", STUDIOVariant.STANDARD), ("protocol", Protocol.SDK_D2E),
    ("endpoint", "https://unapproved.example"),
])
def test_reply_route_identity_must_match(field, value):
    response = raw('CONTOSO-DEMO-SUPPLIER-001, Version 1; Section: "Fictional proposal".')
    with pytest.raises(AttributionError):
        extract_citations(configured_entry(), replace(response, **{field: value}))


def test_errors_never_echo_raw_text(caplog):
    text = 'CONTOSO-DEMO-SUPPLIER-001, Version 1; Section: "private-trace-or-token".'
    with pytest.raises(AttributionError) as caught:
        extract_citations(configured_entry(), raw(text))
    displayed = "".join(traceback.format_exception_only(caught.type, caught.value)) + caplog.text
    assert "private-trace-or-token" not in displayed
    assert "CONTOSO-DEMO-SUPPLIER-001" not in displayed


def test_knowledge_source_version_and_approved_intervention_are_generic():
    selected = configured_entry()
    selected = replace(
        selected, provider_id="demo-knowledge", capability=Capability.KNOWLEDGE_KNOWLEDGE,
        variant=None,
        sources=(replace(selected.sources[0], source_id="CONTOSO-DEMO-KNOWLEDGE-001",
                         provider_id="demo-knowledge",
                         sections=frozenset({"Approved intervention"})),),
    )
    text = '**CONTOSO-DEMO-KNOWLEDGE-001**, "Knowledge playbook," Version 1; Section: “Approved intervention”.'
    response = raw(text, selected)
    assert extract_citations(selected, response) == (
        Citation("CONTOSO-DEMO-KNOWLEDGE-001", "demo-knowledge", "1", "Approved intervention"),
    )
    assert response.variant is None
    assert response.text == text
    with pytest.raises(AttributionError):
        extract_citations(selected, raw(text.replace("Version 1", "Version 2"), selected))
    with pytest.raises(AttributionError):
        extract_citations(selected, raw(text.replace("Approved intervention", "Invented intervention"), selected))


@pytest.mark.parametrize("suffix", [
    ', section "Buyer acceptance conditions". Investigate the objection.',
    '; Section: “Buyer acceptance conditions.” Investigate the objection.',
    '; Section: "Buyer acceptance conditions" [1]. Investigate the objection.',
])
def test_inline_section_clause_ends_before_descriptive_prose_or_footnote(suffix):
    text = 'CONTOSO-DEMO-SUPPLIER-001, Version 1' + suffix
    response = raw(text)
    assert extract_citations(configured_entry(), response) == (
        Citation("CONTOSO-DEMO-SUPPLIER-001", "demo-studio", "1", "Buyer acceptance conditions"),
    )
    assert response.text == text


A2A_REAL_FORMAT = (
    '**This is synthetic demonstration data.** It comes from '
    '**CONTOSO-DEMO-SUPPLIER-001, "Supplier partial-delivery terms," Version 1**, '
    'which is fictional and is not Coca-Cola consumer research[1].\n\n'
    '**Consumer insight (from "Fictional proposal")**\n'
    'Example insight.\n\n'
    '**Questions to ask the retailer (from "Buyer acceptance conditions")**\n'
    'Example questions.\n\n'
    '**Recommendation (from "Recommended next step")**\n'
    'Example recommendation.\n\n'
    '**What the source does not establish (from "Evidence limits")**\n'
    'Example limits.'
)


def test_real_a2a_introduction_and_later_source_attributed_headings():
    response = raw(A2A_REAL_FORMAT)
    citations = extract_citations(configured_entry(), response)
    assert tuple(c.section for c in citations) == SECTIONS
    assert all(c.source_id == "CONTOSO-DEMO-SUPPLIER-001" and c.version == "1" for c in citations)
    assert response.text == A2A_REAL_FORMAT


def test_real_standard_footer_with_punctuation_inside_curly_quotes():
    selected = configured_entry()
    selected = replace(selected, variant=STUDIOVariant.STANDARD, protocol=Protocol.SDK_D2E,
                       auth=replace(selected.auth, scopes=frozenset({STANDARD_SCOPE})))
    text = (
        'Example answer.\n\n'
        'Source: **CONTOSO-DEMO-SUPPLIER-001, "Supplier partial-delivery terms," '
        'version 1**, sections “Fictional proposal,” “Buyer acceptance conditions,” '
        '“Recommended next step,” and “Evidence limits.” [1]'
    )
    response = raw(text, selected)
    citations = extract_citations(selected, response)
    assert tuple(c.section for c in citations) == SECTIONS
    assert response.text == text


@pytest.mark.parametrize("replacement", [
    ("Version 1", "Version 2"),
    ("CONTOSO-DEMO-SUPPLIER-001", "CONTOSO-DEMO-CONSUMER-999"),
    ('Version 1', 'no version supplied'),
    ('(from "Fictional proposal")', '(from "Unsupported insight")'),
])
def test_real_a2a_headings_do_not_rescue_invalid_source_version_or_section(replacement):
    with pytest.raises(AttributionError):
        extract_citations(configured_entry(), raw(A2A_REAL_FORMAT.replace(*replacement)))


@pytest.mark.parametrize("text", [
    '**Consumer insight (from "Fictional proposal")**\nExample answer.',
    'CONTOSO-DEMO-SUPPLIER-001, Version 1. No section supplied.',
    'CONTOSO-DEMO-SUPPLIER-001, Version 1.\n\nI casually mention "Fictional proposal" in body text.',
    'CONTOSO-DEMO-SUPPLIER-001, Version 1.\n\n**Unattributed heading "Fictional proposal"**',
])
def test_heading_support_never_infers_missing_attribution(text):
    with pytest.raises(AttributionError):
        extract_citations(configured_entry(), raw(text))


def test_global_heading_attribution_rejects_multiple_identified_sources():
    selected = configured_entry()
    second = replace(selected.sources[0], source_id="CONTOSO-DEMO-CONSUMER-002")
    selected = replace(selected, sources=selected.sources + (second,))
    text = (
        'CONTOSO-DEMO-SUPPLIER-001, Version 1.\n\n'
        'CONTOSO-DEMO-CONSUMER-002, Version 1.\n\n'
        '**Recommendation (from "Recommended next step")**\nExample recommendation.'
    )
    with pytest.raises(AttributionError, match="ambiguous"):
        extract_citations(selected, raw(text, selected))


@pytest.mark.parametrize("list_text", [
    'sections “Fictional proposal,” “Unknown section.” [1]',
    'sections "Fictional proposal", "Unknown section". Ordinary prose.',
    'sections “Fictional proposal extended.” Ordinary prose.',
    'sections "Fictional proposal.", "Unknown section".',
    'sections Fictional proposal extended. Ordinary prose.',
    'section "Evidence limits", private-unsupported-item.',
])
def test_unknown_explicit_section_lists_still_fail_before_prose_boundaries(list_text):
    text = 'CONTOSO-DEMO-SUPPLIER-001, Version 1, ' + list_text
    with pytest.raises(AttributionError):
        extract_citations(configured_entry(), raw(text))


def test_explicit_unknown_list_cannot_be_hidden_by_supported_global_heading():
    text = (
        'CONTOSO-DEMO-SUPPLIER-001, Version 1, sections "Unsupported".\n\n'
        '**Recommendation (from "Recommended next step")**\nExample recommendation.'
    )
    with pytest.raises(AttributionError):
        extract_citations(configured_entry(), raw(text))


@pytest.mark.parametrize("text,reason", [
    ("", AttributionReason.EMPTY_TEXT),
    ("Uncited answer.", AttributionReason.ATTRIBUTION_MISSING),
    ("CONTOSO-DEMO-CONSUMER-999, Version 1.", AttributionReason.SOURCE_UNKNOWN),
    ("CONTOSO-DEMO-SUPPLIER-001, Version 2.", AttributionReason.VERSION_MISMATCH),
    ("CONTOSO-DEMO-SUPPLIER-001, Version 1.", AttributionReason.SECTION_MISSING),
    ('**Insight (from "Fictional proposal")**', AttributionReason.HEADING_SOURCE_AMBIGUOUS),
    ('CONTOSO-DEMO-SUPPLIER-001, Version 1; Section: "Private section".',
     AttributionReason.SECTION_UNSUPPORTED),
    ('CONTOSO-DEMO-SUPPLIER-001, Version 1; Section: Private section.',
     AttributionReason.SECTION_MISSING_OR_UNSUPPORTED),
    ('CONTOSO-DEMO-SUPPLIER-001, Version 1; Section: "Evidence limits" private prose.',
     AttributionReason.SECTION_LIST_MALFORMED),
])
def test_rejection_branches_expose_fixed_reasons_without_changing_acceptance(text, reason):
    with pytest.raises(AttributionError) as caught:
        extract_citations(configured_entry(), raw(text))
    assert caught.value.reason is reason
    assert "Private section" not in str(caught.value)
    assert "private prose" not in str(caught.value)


def test_invalid_input_and_identity_rejections_have_separate_reasons():
    with pytest.raises(AttributionError) as caught:
        extract_citations(configured_entry(), None)
    assert caught.value.reason is AttributionReason.INVALID_INPUT
    with pytest.raises(AttributionError) as caught:
        extract_citations(configured_entry(), replace(raw(A2A_REAL_FORMAT), provider_id="other"))
    assert caught.value.reason is AttributionReason.IDENTITY_MISMATCH


def test_arbitrary_rejection_reason_cannot_enter_diagnostics():
    with pytest.raises(ValueError, match="Unsupported attribution rejection reason"):
        AttributionError("Safe message", reason="private-reason")


def rejection_line(response, sdk_console):
    with pytest.raises(AttributionError) as caught:
        extract_citations(configured_entry(), response)
    log_attribution_rejection(
        logging.getLogger("contoso.router.runtime"), configured_entry(), response, caught.value,
    )
    rendered = sdk_console.getvalue()
    assert len(rendered.splitlines()) == 1
    assert " WARNING contoso.router.runtime: contoso_attribution " in rendered
    assert len(rendered.split(": contoso_attribution ", 1)[1]) < 512
    for private in ("CONTOSO-DEMO-SUPPLIER-001", "CONTOSO-DEMO-CONSUMER-999", "Version 1",
                    "Version 2", "Fictional proposal", "private-bearer", "private-reason"):
        assert private not in rendered
    return rendered


def test_historical_format_version_mismatch_has_exact_safe_rendered_metadata(sdk_console):
    response = raw(A2A_REAL_FORMAT.replace("Version 1", "Version 2") + "\nprivate-bearer")
    rendered = rejection_line(response, sdk_console)
    assert rendered.endswith(
        "contoso_attribution provider=studio-copilot-harness status=rejected reason=version_mismatch "
        "headers=1 known_headers=1 version_matches=0 headings=4 known_headings=4 "
        "id_seen=yes version_seen=no section_seen=yes truncated=no\n"
    )


def test_lexical_presence_cannot_be_used_to_invent_a_citation(sdk_console):
    response = raw("CONTOSO-DEMO-SUPPLIER-001\nVersion 1\nEvidence limits\nprivate-bearer")
    rendered = rejection_line(response, sdk_console)
    assert rendered.endswith(
        "contoso_attribution provider=studio-copilot-harness status=rejected reason=attribution_missing "
        "headers=0 known_headers=0 version_matches=0 headings=0 known_headings=0 "
        "id_seen=yes version_seen=yes section_seen=yes truncated=no\n"
    )
    with pytest.raises(AttributionError):
        extract_citations(configured_entry(), response)


def test_diagnostic_counts_saturate_without_disclosing_values(sdk_console):
    text = 'CONTOSO-DEMO-SUPPLIER-001, Version 2; Section: "Evidence limits".\n\n' * 40
    rendered = rejection_line(raw(text), sdk_console)
    assert "headers=many known_headers=many version_matches=0" in rendered
    assert "truncated=no" in rendered


def test_diagnostic_analysis_window_is_bounded_and_declared(sdk_console):
    text = "x" * 16384 + '\nCONTOSO-DEMO-SUPPLIER-001, Version 2; Section: "Evidence limits".'
    rendered = rejection_line(raw(text), sdk_console)
    assert rendered.endswith(
        "headers=0 known_headers=0 version_matches=0 headings=0 known_headings=0 "
        "id_seen=no version_seen=no section_seen=no truncated=yes\n"
    )
