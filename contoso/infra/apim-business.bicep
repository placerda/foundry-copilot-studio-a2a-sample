targetScope = 'resourceGroup'

@description('Deploy only after foundation completion and hosted caller admission validation.')
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

var policyTemplate = loadTextContent('./apim/a2a-policy.xml')
var callerPolicy = replace(replace(replace(replace(replace(policyTemplate,
  '__TENANT__', tenantId),
  '__AUDIENCE__', gatewayApiClientId),
  '__REQUESTER_CLIENT__', requesterClientId),
  '__REQUESTER_PRINCIPAL__', requesterPrincipalId),
  '__FOUNDRY_ORIGIN__', 'https://${foundryAccountName}.services.ai.azure.com')
var policy = replace(callerPolicy, '__ROUTER_PATH__',
  '/api/projects/${foundryProjectName}/agents/${routerAgentName}/endpoint/protocols/invocations?api-version=v1')

resource requestSchema 'Microsoft.ApiManagement/service/schemas@2024-05-01' = {
  parent: apim
  name: 'contoso-a2a-request'
  properties: {
    schemaType: 'json'
    document: loadJsonContent('./apim/a2a-request.schema.json')
  }
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'contoso-a2a'
  properties: {
    displayName: 'Contoso agent collaboration'
    path: 'api/a2a'
    protocols: ['https']
    subscriptionRequired: false
    type: 'http'
  }
}

resource sendMessage 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'send-message'
  properties: {
    displayName: 'SendMessage'
    method: 'POST'
    urlTemplate: '/'
    request: {
      representations: [
        {
          contentType: 'application/json'
        }
      ]
    }
  }
  dependsOn: [apiPolicy]
}

resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: policy
  }
  dependsOn: [requestSchema]
}

output businessPath string = '/api/a2a'
