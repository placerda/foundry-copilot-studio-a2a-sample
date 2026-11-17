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
param routerClientId string
@minLength(36)
@maxLength(36)
param routerPrincipalId string
@minLength(1)
@maxLength(64)
param foundryAccountName string
@minLength(1)
@maxLength(64)
param foundryProjectName string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource logger 'Microsoft.ApiManagement/service/loggers@2024-05-01' existing = {
  parent: apim
  name: 'applicationinsights'
}

var template = loadTextContent('./apim/logistics-a2a-policy.xml')
var callerPolicy = replace(replace(replace(replace(template,
  '__TENANT__', tenantId),
  '__AUDIENCE__', gatewayApiClientId),
  '__ROUTER_CLIENT__', routerClientId),
  '__ROUTER_PRINCIPAL__', routerPrincipalId)
var policy = replace(replace(callerPolicy,
  '__FOUNDRY_ORIGIN__', 'https://${foundryAccountName}.services.ai.azure.com'),
  '__LOGISTICS_PATH__', '/api/projects/${foundryProjectName}/agents/contoso-motors-logistics/endpoint/protocols/a2a')

resource requestSchema 'Microsoft.ApiManagement/service/schemas@2024-05-01' = {
  parent: apim
  name: 'contoso-logistics-a2a-request'
  properties: {
    schemaType: 'json'
    document: loadJsonContent('./apim/a2a-request.schema.json')
  }
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'contoso-logistics-a2a-v1'
  properties: {
    displayName: 'Contoso Motors Router-only logistics A2A v1'
    path: 'agents/motors-logistics/versions/1/a2a'
    protocols: ['https']
    subscriptionRequired: false
    type: 'http'
  }
}

resource diagnostic 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = {
  parent: api
  name: 'applicationinsights'
  properties: {
    loggerId: logger.id
    alwaysLog: 'allErrors'
    verbosity: 'error'
    sampling: { samplingType: 'fixed', percentage: 0 }
    frontend: {
      request: { headers: ['x-request-id'], body: { bytes: 0 } }
      response: { headers: ['x-request-id'], body: { bytes: 0 } }
    }
    backend: {
      request: { headers: [], body: { bytes: 0 } }
      response: { headers: [], body: { bytes: 0 } }
    }
  }
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

resource sendMessage 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'send-message'
  properties: {
    displayName: 'Send a bounded A2A 1.0 message to Motors logistics v1'
    method: 'POST'
    urlTemplate: '/'
    request: {
      representations: [{ contentType: 'application/json' }]
    }
    responses: [{
      statusCode: 200
      representations: [{ contentType: 'application/json' }]
    }]
  }
  dependsOn: [apiPolicy, diagnostic]
}

output logisticsA2aPath string = '/agents/motors-logistics/versions/1/a2a'
