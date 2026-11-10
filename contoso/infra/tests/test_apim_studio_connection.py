"""Source boundaries only, not proof of consent, PKCE or token refresh."""

from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "apim-studio-connection.bicep"


def test_secret_is_required_secure_and_has_no_output_or_literal_default():
    text = TEMPLATE.read_text()
    assert re.search(r"@secure\(\)[\s\S]*?param oauthClientSecret string\s*\n", text)
    assert not re.search(r"param oauthClientSecret string\s*=", text)
    assert not re.search(r"^\s*output\s", text, re.MULTILINE)
    assert "clientSecret: oauthClientSecret" in text
    assert "ARM Key Vault parameter reference" in text


def test_only_connection_children_are_created_under_the_existing_apim():
    resources = re.findall(r"resource \w+ '([^']+)'( existing)? =", TEMPLATE.read_text())
    assert resources == [
        ("Microsoft.ApiManagement/service@2024-05-01", " existing"),
        ("Microsoft.ApiManagement/service/authorizationProviders@2024-05-01", ""),
        ("Microsoft.ApiManagement/service/authorizationProviders/authorizations@2024-05-01", ""),
        ("Microsoft.ApiManagement/service/authorizationProviders/authorizations/accessPolicies@2024-05-01", ""),
    ]
    assert not re.search(r"^\s*module\s", TEMPLATE.read_text(), re.MULTILINE)


def test_generic_authorization_code_uses_the_documented_pkce_provider():
    text = TEMPLATE.read_text()
    assert "identityProvider: 'oauth2pkce'" in text
    assert "identityProvider: 'oauth2'" not in text
    assert "authorizationType: 'OAuth2'" in text
    assert "oauth2grantType: 'AuthorizationCode'" in text
    assert "authorizationCode: {" in text
    for unsupported in ("oauth2-pkce", "codeVerifier", "codeChallenge", "clientCredentials:"):
        assert unsupported not in text
    assert not re.search(r"\bstatus\s*:", text)


def test_oauth_endpoints_are_tenant_specific_v2_with_exact_delegated_scope():
    text = TEMPLATE.read_text()
    assert "var tenantId = tenant().tenantId" in text
    assert "https://login.microsoftonline.com/${tenantId}/oauth2/v2.0/authorize" in text
    assert "https://login.microsoftonline.com/${tenantId}/oauth2/v2.0/token" in text
    assert "refreshUrl: tokenUrl" in text
    assert "tokenUrl: tokenUrl" in text
    assert "scopes: 'https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A offline_access'" in text
    assert "/common/" not in text and ".default" not in text


def test_callback_is_bound_to_the_existing_apim_and_is_not_a_login_link():
    text = TEMPLATE.read_text()
    assert "https://authorization-manager.consent.azure-apim.net/redirect/apim/${apim.name}" in text
    assert "loginLink" not in text
    assert "confirmConsentCode" not in text


def test_only_the_actual_apim_managed_identity_can_use_the_connection():
    text = TEMPLATE.read_text()
    assert "objectId: apim.identity!.principalId!" in text
    assert "tenantId: tenantId" in text
    assert not re.search(r"param \w*(?:Object|Principal|Owner|Router)\w*", text, re.IGNORECASE)
    assert "appIds:" not in text


def test_fixed_connection_ids_match_the_candidate_token_policy():
    text = TEMPLATE.read_text()
    context = ET.parse(ROOT / "apim" / "token-policy.xml").find("./inbound/get-authorization-context")
    assert f"name: '{context.attrib['provider-id']}'" in text
    assert f"name: '{context.attrib['authorization-id']}'" in text
    assert context.attrib["identity-type"] == "managed"
