targetScope = 'resourceGroup'

param apimName string
param foundryProjectName string
param routerAgentName string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' existing = {
  parent: apim
  name: 'contoso-operator-probe'
}

resource operation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'probe-router-configuration'
  properties: {
    displayName: 'Read fixed Router admission metadata'
    method: 'POST'
    urlTemplate: '/router-configuration'
  }
}

var template = loadTextContent('./apim/operator-records-policy.xml')
var policy = replace(replace(template, '__ROUTER_PATH__',
  '/api/projects/${foundryProjectName}/agents/${routerAgentName}?api-version=v1'),
  '__APIM_PRINCIPAL__', apim.identity.principalId)

resource operationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = {
  parent: operation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: policy
  }
}
