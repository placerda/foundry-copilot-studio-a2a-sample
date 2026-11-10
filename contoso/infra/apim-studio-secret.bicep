targetScope = 'resourceGroup'

@minLength(3)
@maxLength(24)
param vaultName string

@secure()
@minLength(1)
@description('Supply only in memory through the ARM deployment API, never a literal parameter file or CLI argument.')
param oauthClientSecret string

@minValue(1)
@description('Unix expiration timestamp matching the new Entra client credential.')
param credentialExpiresAt int

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: vaultName
}

resource credential 'Microsoft.KeyVault/vaults/secrets@2024-11-01' = {
  parent: vault
  name: 'apim-studio-client-secret'
  properties: {
    value: oauthClientSecret
    attributes: {
      enabled: true
      exp: credentialExpiresAt
    }
  }
}

output secretId string = credential.id
