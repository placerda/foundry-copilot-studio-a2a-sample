targetScope = 'resourceGroup'

param apimName string

@minLength(36)
@maxLength(36)
param oauthClientId string

@secure()
@minLength(1)
@description('Supply through an ARM Key Vault parameter reference, never a literal parameter file.')
param oauthClientSecret string

var tenantId = tenant().tenantId
var tokenUrl = 'https://login.microsoftonline.com/${tenantId}/oauth2/v2.0/token'

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource provider 'Microsoft.ApiManagement/service/authorizationProviders@2024-05-01' = {
  parent: apim
  name: 'contoso-studio-v2-pkce'
  properties: {
    displayName: 'Shared Studio delegated connection'
    identityProvider: 'oauth2pkce'
    oauth2: {
      redirectUrl: 'https://authorization-manager.consent.azure-apim.net/redirect/apim/${apim.name}'
      grantTypes: {
        authorizationCode: {
          clientId: oauthClientId
          clientSecret: oauthClientSecret
          scopes: 'https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A offline_access'
          authorizationUrl: 'https://login.microsoftonline.com/${tenantId}/oauth2/v2.0/authorize'
          refreshUrl: tokenUrl
          tokenUrl: tokenUrl
        }
      }
    }
  }
}

resource connection 'Microsoft.ApiManagement/service/authorizationProviders/authorizations@2024-05-01' = {
  parent: provider
  name: 'shared-owner'
  properties: {
    authorizationType: 'OAuth2'
    oauth2grantType: 'AuthorizationCode'
  }
}

resource access 'Microsoft.ApiManagement/service/authorizationProviders/authorizations/accessPolicies@2024-05-01' = {
  parent: connection
  name: 'apim-managed-identity'
  properties: {
    objectId: apim.identity!.principalId!
    tenantId: tenantId
  }
}
