targetScope = 'resourceGroup'

param apimName string
param tenantId string
param gatewayApiClientId string
param requesterClientId string
param requesterPrincipalId string
param foundryAccountName string
param foundryProjectName string
param routerAgentName string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' existing = {
  parent: apim
  name: 'contoso-a2a'
}

var policyTemplate = loadTextContent('./apim/a2a-policy.xml')
var callerPolicy = replace(replace(replace(replace(replace(policyTemplate,
  '__TENANT__', tenantId),
  '__AUDIENCE__', gatewayApiClientId),
  '__REQUESTER_CLIENT__', requesterClientId),
  '__REQUESTER_PRINCIPAL__', requesterPrincipalId),
  '__FOUNDRY_ORIGIN__', 'https://${foundryAccountName}.services.ai.azure.com')
var policy = replace(callerPolicy, '__ROUTER_PATH__',
  '/api/projects/${foundryProjectName}/agents/${routerAgentName}/endpoint/protocols/invocations?api-version=v1')

resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: policy
  }
}
