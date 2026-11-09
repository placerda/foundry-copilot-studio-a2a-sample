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

resource probe 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'probe-router-framing'
  properties: {
    displayName: 'Probe fixed Router response structure'
    method: 'POST'
    urlTemplate: '/router-framing'
  }
}

var template = loadTextContent('./apim/operator-router-probe-policy.xml')
var policy = replace(template, '__ROUTER_PATH__',
  '/api/projects/${foundryProjectName}/agents/${routerAgentName}/endpoint/protocols/invocations?api-version=v1')

resource operationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = {
  parent: probe
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: policy
  }
}
