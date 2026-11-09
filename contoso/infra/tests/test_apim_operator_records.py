"""Fixed read-only private metadata without exporting agent configuration."""

from pathlib import Path
import re
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
POLICY = ElementTree.parse(ROOT / "apim" / "operator-records-policy.xml").getroot()
TEMPLATE = (ROOT / "apim-operator-records.bicep").read_text(encoding="utf-8")


def test_fixed_read_only_operation_inherits_exact_operator_admission():
    assert POLICY.find("inbound")[0].tag == "base"
    assert POLICY.findtext("inbound/set-method") == "GET"
    assert POLICY.findtext("inbound/set-body") == "@(String.Empty)"
    assert POLICY.find("inbound/rewrite-uri").attrib["copy-unmatched-params"] == "false"
    assert POLICY.find("backend/base") is not None
    assert POLICY.find("on-error/base") is not None
    assert POLICY.find("outbound/base") is None
    assert POLICY.find("inbound/set-backend-service") is None
    assert POLICY.find("inbound/authentication-managed-identity") is None


def test_only_verified_bounded_nonsecret_metadata_is_projected():
    projection = POLICY.findtext("outbound/set-body")
    for guard in ("status != 200", "65536", "4096", "callers.Count > 16", "Length > 256"):
        assert guard in projection
    assert 'definition["environment_variables"]' in projection
    assert 'environment["CONTOSO_PLATFORM_CALLER_IDS"]' in projection
    assert 'c.Type != JTokenType.String' in projection
    assert 'version.Type != JTokenType.String' in projection
    assert 'Regex.IsMatch((string)version, "^[0-9]{1,20}$")' in projection
    returned = projection.split('shape = "verified-hosted-definition",', 1)[1].split("});", 1)[0]
    for field in ("agent_version", "configured_caller_count", "apim_caller_configured"):
        assert field in returned
    for forbidden in ("body", "definition", "environment", "callers", "configured", "raw", "token"):
        assert re.search(r"\b" + forbidden + r"\s*=", returned) is None


def test_projection_is_captured_before_fresh_response_discards_upstream_headers():
    outbound = list(POLICY.find("outbound"))
    assert [element.tag for element in outbound] == ["set-body", "set-variable", "return-response"]
    assert outbound[1].attrib["name"] == "routerAdmissionMetadata"
    response = outbound[2]
    assert response.attrib == {}
    assert response.findtext("set-body") == '@((string)context.Variables["routerAdmissionMetadata"])'
    assert response.findtext("set-header[@name='Cache-Control']/value") == "no-store"
    assert POLICY.find("inbound/trace") is None
    assert POLICY.find("inbound/cache-lookup") is None


def test_only_existing_api_and_two_additive_child_resources():
    assert "resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' existing" in TEMPLATE
    assert "urlTemplate: '/router-configuration'" in TEMPLATE
    assert "agents/${routerAgentName}?api-version=v1" in TEMPLATE
    assert "'__APIM_PRINCIPAL__', apim.identity.principalId" in TEMPLATE
    assert "versions/" not in TEMPLATE
    for forbidden in ("roleAssignments@", "sku:", "serviceUrl:", "listSecrets", "/endpoint/"):
        assert forbidden not in TEMPLATE
