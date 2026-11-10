targetScope = 'resourceGroup'

param apimName string

@minLength(36)
@maxLength(36)
param tenantId string

@minLength(36)
@maxLength(36)
param gatewayApiClientId string

@minLength(36)
@maxLength(36)
param operatorClientId string

@minLength(36)
@maxLength(36)
param operatorPrincipalId string

@minLength(36)
@maxLength(36)
param sharedOwnerPrincipalId string

@minLength(36)
@maxLength(36)
param oauthClientId string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

var template = loadTextContent('./apim/studio-proof-policy.xml')
var policy = replace(replace(replace(replace(replace(replace(template,
  '__TENANT__', tenantId),
  '__AUDIENCE__', gatewayApiClientId),
  '__OPERATOR_CLIENT__', operatorClientId),
  '__OPERATOR_PRINCIPAL__', operatorPrincipalId),
  '__SHARED_OWNER__', sharedOwnerPrincipalId),
  '__OAUTH_CLIENT__', oauthClientId)

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'contoso-studio-connection-proof'
  properties: {
    displayName: 'Contoso operator-only Studio connection proof'
    path: 'api/ops/studio-connection'
    protocols: ['https']
    subscriptionRequired: false
    type: 'http'
  }
}

resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: policy
  }
}

resource proof 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'prove-studio-connection'
  properties: {
    displayName: 'Verify the fixed shared Studio connection without returning its token'
    method: 'POST'
    urlTemplate: '/'
  }
  dependsOn: [apiPolicy]
}

output connectionProofPath string = '/api/ops/studio-connection'
