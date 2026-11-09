"""Static admission/credential boundaries; not proof of OAuth consent or refresh."""

from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "apim" / "token-policy.xml"


def test_token_api_is_fixed_https_and_has_no_passthrough_or_credentials():
    template = (ROOT / "apim-token.bicep").read_text()
    assert "path: 'api/connections'" in template
    assert "urlTemplate: '/token'" in template
    assert "protocols: ['https']" in template
    assert "serviceUrl" not in template
    assert "clientSecret" not in template
    assert template.count("body: { bytes: 0 }") == 4
    assert "percentage: 0" in template
    assert "Authorization" not in template


def test_router_admission_requires_signed_app_token_and_exact_identity_pair():
    root = ET.parse(POLICY).getroot()
    jwt = root.find("./inbound/validate-jwt")
    assert jwt.attrib["require-signed-tokens"] == "true"
    assert jwt.attrib["require-expiration-time"] == "true"
    claims = {node.attrib["name"]: [v.text for v in node.findall("value")]
              for node in jwt.findall("./required-claims/claim")}
    assert claims["oid"] == ["__ROUTER_OID__"]
    assert claims["azp"] == ["__ROUTER_CLIENT__"]
    assert claims["idtyp"] == ["app"]
    assert claims["roles"] == ["CONTOSO.Connection.Use"]
    assert claims["tid"] == ["__TENANT__"]
    assert jwt.find("./audiences/audience").text == "__API_AUDIENCE__"


def test_credential_access_uses_apim_identity_and_fixed_connection_not_browser_jwt():
    root = ET.parse(POLICY).getroot()
    context = root.find("./inbound/get-authorization-context")
    assert context.attrib == {
        "provider-id": "contoso-studio-v2", "authorization-id": "shared-paulo",
        "context-variable-name": "studioConnection", "identity-type": "managed",
        "ignore-error": "true",
    }
    assert not root.findall(".//forward-request")
    assert not root.findall(".//send-request")
    assert not root.findall(".//trace")
    assert not root.findall(".//log-to-eventhub")


def test_only_canonical_provider_request_is_accepted():
    root = ET.parse(POLICY).getroot()
    condition = next(
        node.attrib["condition"] for node in root.findall(".//when")
        if 'Regex.IsMatch((string)context.Variables["body"]' in node.attrib["condition"]
    )
    literal = re.search(r'@"(.*)"', condition).group(1).replace('""', '"')
    pattern = literal.replace(r"\z", r"\Z")
    assert re.fullmatch(pattern, '{"provider_id": "studio-copilot-harness"}')
    for body in [
        '{"provider_id":"other"}',
        '{"provider_id":"studio-copilot-harness","provider_id":"studio-copilot-harness"}',
        '{"provider_id":"studio-copilot-harness","endpoint":"https://evil.invalid"}',
        '{"authorization_id":"shared-paulo"}',
    ]:
        assert not re.fullmatch(pattern, body)
    text = POLICY.read_text()
    assert "provider_id" in text and "Regex.IsMatch" in text and r"\A\s*\{" in text
    assert "256" in text


def test_body_is_bounded_before_read_and_media_type_is_exact():
    root = ET.parse(POLICY).getroot()
    inbound = ET.tostring(root.find("inbound"), encoding="unicode")
    assert "Content-Length" in inbound
    assert "Transfer-Encoding" in inbound
    assert "Split(';')[0].Trim().Equals" in inbound
    assert ".StartsWith" not in inbound
    assert inbound.index("Int32.Parse") < inbound.index("Body.As")


def test_expiry_uses_documented_jwt_property_not_claim_dictionary():
    text = POLICY.read_text()
    assert "jwt.ExpirationTime.Value.ToUniversalTime()" in text
    assert 'GetValueOrDefault("exp"' not in text


def test_shared_token_is_verified_for_tenant_owner_scope_audience_before_release():
    root = ET.parse(POLICY).getroot()
    jwt = root.find("./inbound/validate-jwt[@output-token-variable-name='studioJwt']")
    claims = {node.attrib["name"]: [v.text for v in node.findall("value")]
              for node in jwt.findall("./required-claims/claim")}
    assert jwt.attrib["require-signed-tokens"] == "true"
    assert claims["oid"] == ["__SHARED_OWNER__"]
    assert claims["scp"] == ["MCS.InvokeAsA2A"]
    assert claims["tid"] == ["__TENANT__"]
    assert jwt.find("./audiences/audience").text == "https://api.powerplatform.com"
    text = POLICY.read_text()
    for field in ("token =", "tenant_id =", "audience =", "scopes =", "expires_at ="):
        assert field in text
    assert "refresh_token" not in text


def test_missing_connection_is_explicit_and_never_uses_retained_broker_fallback():
    root = ET.parse(POLICY).getroot()
    assert root.find(".//set-status[@code='409']") is not None
    assert root.find("./on-error/return-response/set-status").attrib["code"] == "503"
    assert root.find("./backend").text is None
    assert "azurewebsites.net" not in POLICY.read_text()
    assert root.findall(".//set-header[@name='Cache-Control']/value")
