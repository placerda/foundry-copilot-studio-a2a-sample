"""Offline route boundaries. Real managed-identity invocation is checked separately."""

import re
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
POLICY = ElementTree.parse(ROOT / "apim" / "logistics-a2a-policy.xml").getroot()
STUDIO = ElementTree.parse(ROOT / "apim" / "studio-a2a-policy.xml").getroot()
TEMPLATE = (ROOT / "apim-logistics-a2a.bicep").read_text(encoding="utf-8")


def test_route_is_isolated_versioned_and_depends_on_policy_and_private_diagnostics():
    assert "name: 'contoso-logistics-a2a-v1'" in TEMPLATE
    assert "path: 'agents/motors-logistics/versions/1/a2a'" in TEMPLATE
    assert "dependsOn: [apiPolicy, diagnostic]" in TEMPLATE
    assert TEMPLATE.count("body: { bytes: 0 }") == 4
    assert "percentage: 0" in TEMPLATE
    assert "loadJsonContent('./apim/a2a-request.schema.json')" in TEMPLATE
    for forbidden in ("roleAssignments", "authorizationProviders@", "Microsoft.Network",
                      "Microsoft.Web", "agentCard", "listSecrets"):
        assert forbidden not in TEMPLATE


def test_router_admission_and_duplicate_detection_are_as_strict_as_the_studio_route():
    for xpath in ("inbound/validate-jwt", "inbound/choose",
                  "inbound/set-variable[@name='request-valid']"):
        assert ElementTree.tostring(POLICY.find(xpath)) == ElementTree.tostring(STUDIO.find(xpath))
    tags = [element.tag for element in POLICY.find("inbound")]
    assert tags.index("validate-jwt") < tags.index("validate-content")
    assert tags.index("validate-content") < tags.index("authentication-managed-identity")
    validation = POLICY.find("inbound/validate-content")
    assert validation.attrib["max-size"] == "32768"
    assert validation.find("content").attrib["schema-id"] == "contoso-logistics-a2a-request"


def test_only_the_fixed_logistics_target_uses_managed_identity_without_studio_credentials():
    assert POLICY.find("inbound/set-backend-service").attrib == {"base-url": "__FOUNDRY_ORIGIN__"}
    assert POLICY.find("inbound/rewrite-uri").attrib == {
        "template": "__LOGISTICS_PATH__", "copy-unmatched-params": "false",
    }
    assert POLICY.find("inbound/authentication-managed-identity").attrib == {
        "id": "foundry-credential", "resource": "https://ai.azure.com",
        "output-token-variable-name": "foundry-token", "ignore-error": "false",
    }
    assert POLICY.find("inbound/set-header[@name='Authorization']/value").text == (
        '@("Bearer " + (string)context.Variables["foundry-token"])'
    )
    assert POLICY.find(".//get-authorization-context") is None
    assert POLICY.find(".//send-request") is None
    assert POLICY.find(".//retry") is None
    assert "/agents/contoso-motors-logistics/endpoint/protocols/a2a" in TEMPLATE
    assert set(re.findall(r"__[A-Z_]+__", ElementTree.tostring(POLICY).decode())) == {
        "__TENANT__", "__AUDIENCE__", "__ROUTER_CLIENT__", "__ROUTER_PRINCIPAL__",
        "__FOUNDRY_ORIGIN__", "__LOGISTICS_PATH__",
    }


def test_response_is_bounded_nonstreaming_and_errors_cannot_disclose_backend_tokens():
    assert POLICY.find("backend/forward-request").attrib == STUDIO.find("backend/forward-request").attrib
    assert POLICY.find("outbound/validate-content").attrib["max-size"] == "131072"
    outbound = ElementTree.tostring(POLICY.find("outbound"), encoding="unicode")
    assert "JObject" not in outbound
    for response in POLICY.findall(".//return-response"):
        assert response.findtext("set-header[@name='Cache-Control']/value") == "no-store"
        assert "foundry-token" not in response.findtext("set-body")
    for tag in ("trace", "log-to-eventhub", "cache-store", "cache-store-value"):
        assert POLICY.find(".//" + tag) is None
