targetScope = 'resourceGroup'

param apimName string
param tenantId string
param gatewayApiClientId string
param operatorClientId string
param operatorPrincipalId string
param foundryAccountName string
param foundryProjectName string

@minLength(36)
@maxLength(36)
param sdkExpectedPrincipalId string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

var template = loadTextContent('./apim/operator-probe-policy.xml')
var bound = replace(replace(replace(replace(replace(replace(template,
  '__TENANT__', tenantId),
  '__AUDIENCE__', gatewayApiClientId),
  '__OPERATOR_CLIENT__', operatorClientId),
  '__OPERATOR_PRINCIPAL__', operatorPrincipalId),
  '__FOUNDRY_ORIGIN__', 'https://${foundryAccountName}.services.ai.azure.com'),
  '__SDK_EXPECTED_PRINCIPAL__', sdkExpectedPrincipalId)
var policy = replace(bound, '__PROBE_PATH__',
  '/api/projects/${foundryProjectName}/agents/contoso-foundry-sdk-agent/endpoint/protocols/openai/responses?api-version=v1')

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'contoso-operator-probe'
  properties: {
    displayName: 'Contoso private operator identity diagnostic'
    path: 'api/ops/sdk-identity'
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

resource probe 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'probe-sdk-identity'
  properties: {
    displayName: 'Probe SDK hosted identity context'
    method: 'POST'
    urlTemplate: '/'
  }
  dependsOn: [apiPolicy]
}

output operatorProbePath string = '/api/ops/sdk-identity'
