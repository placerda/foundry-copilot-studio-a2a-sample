"""Secret-storage foundation boundaries, not live deployment acceptance."""

from pathlib import Path
import re

TEMPLATE = Path(__file__).resolve().parents[1] / "apim-studio-vault.bicep"


def test_only_a_vault_and_vault_scoped_writer_are_created():
    text = TEMPLATE.read_text()
    assert re.findall(r"resource \w+ '([^']+)'", text) == [
        "Microsoft.KeyVault/vaults@2024-11-01",
        "Microsoft.Authorization/roleAssignments@2022-04-01",
    ]
    assert "scope: vault" in text
    assert "principalId: operatorObjectId" in text
    assert "principalType: 'User'" in text
    assert "b86a8fe4-44ce-4948-aee5-eccb2c155cd7" in text


def test_vault_uses_deployment_tenant_region_and_rbac():
    text = TEMPLATE.read_text()
    assert "location: resourceGroup().location" in text
    assert "tenantId: tenant().tenantId" in text
    assert "enableRbacAuthorization: true" in text
    assert "accessPolicies: []" in text
    assert "name: 'standard'" in text


def test_template_secret_access_is_enabled_without_other_host_access():
    text = TEMPLATE.read_text()
    assert "enabledForTemplateDeployment: true" in text
    assert "enabledForDeployment: false" in text
    assert "enabledForDiskEncryption: false" in text


def test_soft_delete_and_purge_protection_cannot_be_disabled_by_parameters():
    text = TEMPLATE.read_text()
    assert "enableSoftDelete: true" in text
    assert "enablePurgeProtection: true" in text
    assert "softDeleteRetentionInDays: 7" in text
    assert re.findall(r"param (\w+)", text) == ["operatorObjectId"]


def test_no_secret_value_network_configuration_or_runtime_cutover():
    text = TEMPLATE.read_text()
    assert re.findall(r"output (\w+) string", text) == ["vaultId", "vaultUri"]
    for unsupported in ("clientSecret", "/secrets@", "Microsoft.Network",
                        "Microsoft.ApiManagement", "networkAcls", "privateEndpoint"):
        assert unsupported not in text
