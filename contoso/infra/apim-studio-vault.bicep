targetScope = 'resourceGroup'

@minLength(36)
@maxLength(36)
param operatorObjectId string

var vaultName = 'cs-broker-${uniqueString(resourceGroup().id)}'
var secretsOfficerRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: vaultName
  location: resourceGroup().location
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 7
    enabledForTemplateDeployment: true
    enabledForDeployment: false
    enabledForDiskEncryption: false
    accessPolicies: []
  }
}

resource writer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, operatorObjectId, secretsOfficerRoleId)
  scope: vault
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: secretsOfficerRoleId
  }
}

output vaultId string = vault.id
output vaultUri string = vault.properties.vaultUri
