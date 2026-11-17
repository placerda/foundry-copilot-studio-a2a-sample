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
@minLength(36)
@maxLength(36)
param studioEnvironmentId string
@minLength(1)
@maxLength(128)
param studioAgentSchema string
@minLength(36)
@maxLength(36)
param studioOauthClientId string
@description('Exact existing Credential Manager connection ID. Reuse its current name; this does not create or rename a connection.')
@minLength(1)
@maxLength(80)
param studioConnectionName string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource logger 'Microsoft.ApiManagement/service/loggers@2024-05-01' existing = {
  parent: apim
  name: 'applicationinsights'
}

var studioPath = '/copilotstudio-a2a/tenants/${tenantId}/environments/${studioEnvironmentId}/agents/${studioAgentSchema}'
var template = loadTextContent('./apim/studio-a2a-policy.xml')
var callerPolicy = replace(replace(replace(replace(template,
  '__TENANT__', tenantId),
  '__AUDIENCE__', gatewayApiClientId),
  '__ROUTER_CLIENT__', routerClientId),
  '__ROUTER_PRINCIPAL__', routerPrincipalId)
var policy = replace(replace(replace(callerPolicy,
  '__STUDIO_PATH__', studioPath),
  '__OAUTH_CLIENT__', studioOauthClientId),
  '__STUDIO_CONNECTION__', studioConnectionName)

resource requestSchema 'Microsoft.ApiManagement/service/schemas@2024-05-01' = {
  parent: apim
  name: 'contoso-studio-a2a-request'
  properties: {
    schemaType: 'json'
    document: loadJsonContent('./apim/a2a-request.schema.json')
  }
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'contoso-studio-a2a-v1'
  properties: {
    displayName: 'Contoso Motors Router-only Studio A2A v1'
    path: 'agents/studio-copilot-harness/versions/1/a2a'
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
    displayName: 'Send a bounded A2A 1.0 message to the fixed Studio agent'
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

output studioA2aPath string = '/agents/studio-copilot-harness/versions/1/a2a'
