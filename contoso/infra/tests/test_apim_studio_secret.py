"""ARM-only credential-storage source boundaries, not live write acceptance."""

from pathlib import Path
import re

TEMPLATE = Path(__file__).resolve().parents[1] / "apim-studio-secret.bicep"


def test_only_a_secret_in_an_existing_vault_is_written():
    text = TEMPLATE.read_text()
    assert re.findall(r"resource \w+ '([^']+)'", text) == [
        "Microsoft.KeyVault/vaults@2024-11-01",
        "Microsoft.KeyVault/vaults/secrets@2024-11-01",
    ]
    assert "resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing =" in text
    assert "parent: vault" in text
    assert "name: 'apim-studio-client-secret'" in text


def test_credential_is_required_secure_and_has_no_default():
    text = TEMPLATE.read_text()
    assert re.search(
        r"@secure\(\)\s+@minLength\(1\)\s+@description\('[^']+'\)\s+"
        r"param oauthClientSecret string\s*\n",
        text,
    )
    assert "param oauthClientSecret string =" not in text
    assert "value: oauthClientSecret" in text
    assert "never a literal parameter file or CLI argument" in text


def test_expiration_is_required_and_bound_to_the_secret():
    text = TEMPLATE.read_text()
    assert re.search(
        r"@minValue\(1\)\s+@description\('[^']+'\)\s+"
        r"param credentialExpiresAt int\s*\n",
        text,
    )
    assert "param credentialExpiresAt int =" not in text
    assert "enabled: true" in text
    assert "exp: credentialExpiresAt" in text


def test_output_contains_only_the_resource_id():
    text = TEMPLATE.read_text()
    assert re.findall(r"output (\w+) \w+ = ([^\n]+)", text) == [
        ("secretId", "credential.id")
    ]


def test_no_network_role_application_connection_or_runtime_changes():
    text = TEMPLATE.read_text()
    assert re.findall(r"param (\w+)", text) == [
        "vaultName", "oauthClientSecret", "credentialExpiresAt"
    ]
    for unsupported in (
        "publicNetworkAccess", "networkAcls", "privateEndpoint",
        "Microsoft.Network", "Microsoft.Authorization", "Microsoft.ApiManagement",
        "Microsoft.Graph", "Microsoft.App", "Microsoft.Web", "listSecrets",
        "deploymentScripts",
    ):
        assert unsupported not in text
