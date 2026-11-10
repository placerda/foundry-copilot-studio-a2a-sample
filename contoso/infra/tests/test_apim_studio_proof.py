"""Static boundaries only, not APIM expression execution or live token proof."""

import re
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
POLICY = ElementTree.parse(ROOT / "apim" / "studio-proof-policy.xml").getroot()
TEMPLATE = (ROOT / "apim-studio-proof.bicep").read_text(encoding="utf-8")


def test_isolated_template_does_not_change_existing_api_or_connection():
    resources = re.findall(r"resource \w+ '([^']+)'( existing)? =", TEMPLATE)
    assert resources == [
        ("Microsoft.ApiManagement/service@2024-05-01", " existing"),
        ("Microsoft.ApiManagement/service/apis@2024-05-01", ""),
        ("Microsoft.ApiManagement/service/apis/policies@2024-05-01", ""),
        ("Microsoft.ApiManagement/service/apis/operations@2024-05-01", ""),
    ]
    assert "name: 'contoso-studio-connection-proof'" in TEMPLATE
    assert "path: 'api/ops/studio-connection'" in TEMPLATE
    assert "method: 'POST'" in TEMPLATE and "urlTemplate: '/'" in TEMPLATE
    assert "protocols: ['https']" in TEMPLATE
    assert "dependsOn: [apiPolicy]" in TEMPLATE
    for forbidden in ("roleAssignments", "authorizationProviders@", "serviceUrl",
                      "listSecrets", "securestring", "Microsoft.Network", "Microsoft.Web"):
        assert forbidden not in TEMPLATE


def test_all_trusted_identity_bindings_are_required_and_replaced():
    bindings = {
        "tenantId": "__TENANT__", "gatewayApiClientId": "__AUDIENCE__",
        "operatorClientId": "__OPERATOR_CLIENT__", "operatorPrincipalId": "__OPERATOR_PRINCIPAL__",
        "sharedOwnerPrincipalId": "__SHARED_OWNER__", "oauthClientId": "__OAUTH_CLIENT__",
    }
    for parameter, placeholder in bindings.items():
        assert f"param {parameter} string\n" in TEMPLATE
        assert f"param {parameter} string =" not in TEMPLATE
        assert f"'{placeholder}', {parameter})" in TEMPLATE
    assert set(re.findall(r"__[A-Z_]+__", ElementTree.tostring(POLICY).decode())) == set(bindings.values())


def test_operator_admission_matches_existing_deployed_operator_contract():
    existing = ElementTree.parse(ROOT / "apim" / "operator-probe-policy.xml").getroot()
    current_jwt = POLICY.find("inbound/validate-jwt")
    old_jwt = existing.find("inbound/validate-jwt")
    assert ElementTree.tostring(current_jwt).strip() == ElementTree.tostring(old_jwt).strip()
    condition = POLICY.find("inbound/choose/when").attrib["condition"]
    assert condition == existing.find("inbound/choose/when").attrib["condition"]
    assert "CONTOSO.Deploy" in condition and "CONTOSO.Invoke" not in condition
    assert POLICY.find("inbound")[0].tag == "base"


def test_zero_body_no_query_or_encoded_payload_before_connection_access():
    inbound = list(POLICY.find("inbound"))
    gate = POLICY.findall("inbound/choose/when")[1].attrib["condition"]
    for guard in ('Query.Count != 0', '"Content-Length", "") != "0"',
                  'ContainsKey("Transfer-Encoding")', 'ContainsKey("Content-Encoding")'):
        assert guard in gate
    assert next(i for i, element in enumerate(inbound) if element.tag == "choose") < next(
        i for i, element in enumerate(inbound) if element.tag == "get-authorization-context"
    )
    assert POLICY.find(".//set-backend-service") is None
    assert POLICY.find(".//rewrite-uri") is None
    assert POLICY.find(".//forward-request") is None
    assert POLICY.find(".//send-request") is None
    assert POLICY.find(".//authentication-managed-identity") is None
    assert POLICY.find("backend") is not None and len(POLICY.find("backend")) == 0


def test_only_fixed_pkce_connection_with_apim_managed_identity_is_used():
    connection = POLICY.find("inbound/get-authorization-context")
    assert connection.attrib == {
        "provider-id": "contoso-studio-v2-pkce", "authorization-id": "shared-owner",
        "context-variable-name": "studioConnection", "identity-type": "managed", "ignore-error": "true",
    }
    empty = POLICY.findall("inbound/choose")[1].find("when")
    assert "String.IsNullOrWhiteSpace" in empty.attrib["condition"]
    assert empty.find("return-response/set-status").attrib["code"] == "409"
    limit = POLICY.find("inbound/rate-limit-by-key")
    assert limit.attrib == {
        "calls": "6", "renewal-period": "60", "counter-key": "contoso-studio-connection-proof",
    }


def test_connection_signature_owner_tenant_scope_and_distinct_resource_are_verified():
    validation = POLICY.find("inbound/validate-jwt[@id='connection-jwt']")
    assert validation.attrib["require-signed-tokens"] == "true"
    assert validation.attrib["require-expiration-time"] == "true"
    assert validation.attrib["clock-skew"] == "0"
    assert validation.attrib["output-token-variable-name"] == "studioJwt"
    assert "studioConnection" in validation.attrib["token-value"]
    assert [element.text for element in validation.findall("audiences/audience")] == [
        "6f46e6ed-07bf-4616-80f0-47546850f654",
        "https://api.powerplatform.com/copilotstudio-a2a",
    ]
    claims = {element.attrib["name"]: [value.text for value in element.findall("value")]
              for element in validation.findall("required-claims/claim")}
    assert claims == {
        "ver": ["1.0", "2.0"], "tid": ["__TENANT__"],
        "oid": ["__SHARED_OWNER__"], "scp": ["MCS.InvokeAsA2A"],
    }
    assert validation.find("required-claims/claim[@name='scp']").attrib["separator"] == " "
    assert {issuer.text for issuer in validation.findall("issuers/issuer")} == {
        "https://login.microsoftonline.com/__TENANT__/v2.0", "https://sts.windows.net/__TENANT__/",
    }


def test_claim_shape_version_client_issuer_and_expiry_are_cross_checked():
    condition = POLICY.findall("inbound/choose")[2].find("when").attrib["condition"]
    for check in (
        "jwt.Audiences.Count() != 1", 'claims["ver"].Length != 1', 'claims["tid"].Length != 1',
        'claims["oid"].Length != 1', 'claims["scp"].Length != 1',
        'version == "2.0" ? "azp" : "appid"', "jwt.Issuer != issuer",
        "claims[clientClaim].Length != 1", 'claims[clientClaim][0] != "__OAUTH_CLIENT__"',
        "https://login.microsoftonline.com/__TENANT__/v2.0", "https://sts.windows.net/__TENANT__/",
        "!jwt.ExpirationTime.HasValue", "<= DateTime.UtcNow.AddSeconds(30)",
        "> DateTime.UtcNow.AddHours(48)",
    ):
        assert check in condition


def test_proof_response_is_allowlisted_and_cannot_expose_credentials():
    response = POLICY.find("inbound/return-response")
    assert response.find("set-status").attrib["code"] == "200"
    body = response.findtext("set-body")
    projected = body.split("return JsonConvert.SerializeObject(new {", 1)[1].split("});", 1)[0]
    fields = re.findall(r"(\w+)\s*=(?!=)", projected)
    assert fields == [
        "status", "signature_verified", "tenant_verified", "owner_verified", "client_verified",
        "audience_verified", "scope_verified", "audience", "token_version", "expires_at",
        "refresh_verified", "studio_business_verified",
    ]
    assert 'status = "connection-token-proof"' in projected
    assert "refresh_verified = false" in projected
    assert "studio_business_verified = false" in projected
    assert "jwt.Audiences.Single()" in projected
    assert 'jwt.Claims["ver"][0]' in projected
    assert re.findall(r'jwt.Claims\["([^"]+)"\]', projected) == ["ver"]
    for forbidden in ("AccessToken", "RefreshToken", "Authorization", "context.Request", "context.LastError"):
        assert forbidden not in projected
    for forbidden in ("trace", "log-to-eventhub", "cache-store", "send-one-way-request"):
        assert POLICY.find(".//" + forbidden) is None


def test_every_response_is_noncacheable_and_error_details_are_fixed():
    for response in POLICY.findall(".//return-response"):
        assert response.findtext("set-header[@name='Cache-Control']/value") == "no-store"
        assert response.findtext("set-header[@name='Content-Type']/value") == "application/json"
    error = POLICY.find("on-error/return-response")
    codes = error.find("set-status").attrib["code"]
    for status in ("404", "401", "502", "429", "503"):
        assert status in codes
    text = error.findtext("set-body")
    assert "context.LastError.Message" not in text
    assert "context.LastError.Reason" in text
    assert "context.LastError.PolicyId" in text
    assert not list(POLICY.find("outbound"))
