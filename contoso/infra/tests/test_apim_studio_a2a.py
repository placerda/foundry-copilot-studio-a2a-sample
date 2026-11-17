"""Offline proxy boundaries, not proof of APIM execution or Studio access."""

import re
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
POLICY = ElementTree.parse(ROOT / "apim" / "studio-a2a-policy.xml").getroot()
TEMPLATE = (ROOT / "apim-studio-a2a.bicep").read_text(encoding="utf-8")


def test_isolated_versioned_api_requires_policy_and_zero_body_diagnostics_first():
    assert "name: 'contoso-studio-a2a-v1'" in TEMPLATE
    assert "path: 'agents/studio-copilot-harness/versions/1/a2a'" in TEMPLATE
    assert "dependsOn: [apiPolicy, diagnostic]" in TEMPLATE
    assert "method: 'POST'" in TEMPLATE and "urlTemplate: '/'" in TEMPLATE
    assert TEMPLATE.count("body: { bytes: 0 }") == 4
    assert "percentage: 0" in TEMPLATE and "headers: ['Authorization']" not in TEMPLATE
    for forbidden in ("roleAssignments", "authorizationProviders@", "Microsoft.Network",
                      "Microsoft.Web", "agentCard", "listSecrets", "securestring"):
        assert forbidden not in TEMPLATE


def test_bindings_are_required_and_no_unreplaced_placeholder_is_possible():
    bindings = {
        "tenantId": "__TENANT__", "gatewayApiClientId": "__AUDIENCE__",
        "routerClientId": "__ROUTER_CLIENT__", "routerPrincipalId": "__ROUTER_PRINCIPAL__",
        "studioOauthClientId": "__OAUTH_CLIENT__",
        "studioConnectionName": "__STUDIO_CONNECTION__",
    }
    for parameter, placeholder in bindings.items():
        assert f"param {parameter} string\n" in TEMPLATE
        assert f"'{placeholder}', {parameter})" in TEMPLATE
    placeholders = set(re.findall(r"__[A-Z_]+__", ElementTree.tostring(POLICY).decode()))
    assert placeholders == {*bindings.values(), "__STUDIO_PATH__"}
    assert "'__STUDIO_PATH__', studioPath)" in TEMPLATE
    assert "/tenants/${tenantId}/environments/${studioEnvironmentId}/agents/${studioAgentSchema}" in TEMPLATE


def test_workload_admission_is_strict_and_precedes_body_and_connection_access():
    jwt = POLICY.find("inbound/validate-jwt")
    assert jwt.attrib["id"] == "router-jwt"
    assert jwt.attrib["require-signed-tokens"] == jwt.attrib["require-expiration-time"] == "true"
    claims = {claim.attrib["name"]: [v.text for v in claim.findall("value")]
              for claim in jwt.findall("required-claims/claim")}
    assert claims == {
        "ver": ["2.0"], "tid": ["__TENANT__"], "idtyp": ["app"],
        "oid": ["__ROUTER_PRINCIPAL__"], "azp": ["__ROUTER_CLIENT__"],
        "roles": ["CONTOSO.Connection.Use"],
    }
    guard = POLICY.find("inbound/choose/when").attrib["condition"]
    assert 'ContainsKey("scp")' in guard
    assert 'caller.Audiences.Count() != 1' in guard
    tags = [element.tag for element in POLICY.find("inbound")]
    assert tags.index("validate-jwt") < tags.index("validate-content") < tags.index("get-authorization-context")


def test_existing_a2a_schema_and_duplicate_byte_guard_are_reused_without_weakening():
    assert "loadJsonContent('./apim/a2a-request.schema.json')" in TEMPLATE
    validation = POLICY.find("inbound/validate-content")
    assert validation.attrib["max-size"] == "32768"
    assert validation.find("content").attrib["schema-id"] == "contoso-studio-a2a-request"
    existing = ElementTree.parse(ROOT / "apim" / "a2a-policy.xml").getroot()
    assert POLICY.find("inbound/set-variable[@name='request-valid']").attrib["value"] == (
        existing.find("inbound/set-variable[@name='request-valid']").attrib["value"]
    )
    guard = POLICY.findall("inbound/choose/when")[1].attrib["condition"]
    for check in ("Query.Count != 0", '"A2A-Version", "") != "1.0"', '"Content-Encoding"'):
        assert check in guard


def test_only_fixed_shared_connection_and_fixed_studio_origin_are_used():
    connection = POLICY.find("inbound/get-authorization-context")
    assert connection.attrib == {
        "id": "studio-connection", "provider-id": "contoso-studio-v2-pkce",
        "authorization-id": "__STUDIO_CONNECTION__", "context-variable-name": "studioConnection",
        "identity-type": "managed", "ignore-error": "false",
    }
    assert POLICY.find("inbound/set-backend-service").attrib == {"base-url": "https://api.powerplatform.com"}
    assert POLICY.find("inbound/rewrite-uri").attrib == {
        "template": "__STUDIO_PATH__", "copy-unmatched-params": "false",
    }
    assert POLICY.find(".//authentication-managed-identity") is None
    assert POLICY.find(".//send-request") is None
    assert POLICY.find(".//retry") is None
    header = POLICY.find("inbound/set-header[@name='Authorization']")
    assert header.attrib["exists-action"] == "override" and "studioConnection" in header.findtext("value")


def test_connection_binding_preserves_an_existing_id_without_changing_the_policy_boundary():
    template = (ROOT / "apim" / "studio-a2a-policy.xml").read_text(encoding="utf-8")
    for existing_name in ("shared-owner", "existing-demo-connection"):
        rendered = ElementTree.fromstring(template.replace("__STUDIO_CONNECTION__", existing_name))
        connection = rendered.find("inbound/get-authorization-context")
        assert connection.attrib["authorization-id"] == existing_name
        assert connection.attrib["provider-id"] == "contoso-studio-v2-pkce"
        assert connection.attrib["identity-type"] == "managed"
        assert connection.attrib["ignore-error"] == "false"
    assert not re.search(r"param studioConnectionName string\s*=", TEMPLATE)


def test_studio_token_is_validated_but_never_returned_or_logged():
    jwt = POLICY.find("inbound/validate-jwt[@id='connection-jwt']")
    assert jwt.attrib["require-signed-tokens"] == jwt.attrib["require-expiration-time"] == "true"
    claims = {claim.attrib["name"]: [v.text for v in claim.findall("value")]
              for claim in jwt.findall("required-claims/claim")}
    assert claims == {"ver": ["1.0", "2.0"], "tid": ["__TENANT__"], "scp": ["MCS.InvokeAsA2A"]}
    assert jwt.find("required-claims/claim[@name='scp']").attrib["separator"] == " "
    guard = POLICY.findall("inbound/choose")[-1].find("when").attrib["condition"]
    for check in ('version == "2.0" ? "azp" : "appid"', "jwt.Issuer != issuer",
                  'claims[clientClaim][0] != "__OAUTH_CLIENT__"', "jwt.Audiences.Count() != 1",
                  "DateTime.UtcNow.AddSeconds(30)", "DateTime.UtcNow.AddHours(48)"):
        assert check in guard
    for body in POLICY.findall(".//set-body"):
        assert "AccessToken" not in body.text and "context.LastError.Message" not in body.text
    for tag in ("trace", "log-to-eventhub", "cache-store", "cache-store-value"):
        assert POLICY.find(".//" + tag) is None


def test_backend_is_bounded_nonstreaming_no_redirect_and_does_not_parse_a2a_result():
    forward = POLICY.find("backend/forward-request")
    assert forward.attrib == {
        "timeout": "30", "follow-redirects": "false", "buffer-response": "true",
        "fail-on-error-status-code": "false",
    }
    outbound = ElementTree.tostring(POLICY.find("outbound"), encoding="unicode")
    assert "JObject" not in outbound and "As&lt;" not in outbound
    assert POLICY.find("outbound/validate-content").attrib["max-size"] == "131072"
    removed = {element.attrib["name"] for element in POLICY.findall("outbound/set-header")
               if element.attrib["exists-action"] == "delete"}
    assert {"Authorization", "Set-Cookie", "Location", "WWW-Authenticate"} <= removed
    for response in POLICY.findall(".//return-response"):
        assert response.findtext("set-header[@name='Cache-Control']/value") == "no-store"
    error_status = POLICY.find("on-error/return-response/set-status").attrib["code"]
    for code in ("404", "401", "400", "429", "409", "502"):
        assert code in error_status
