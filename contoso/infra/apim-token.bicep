targetScope = 'resourceGroup'

param apimName string
@minLength(36)
@maxLength(36)
param tenantId string
@minLength(36)
@maxLength(36)
param apiAudience string
@minLength(36)
@maxLength(36)
param routerClientId string
@minLength(36)
@maxLength(36)
param routerObjectId string
@minLength(36)
@maxLength(36)
param sharedOwnerObjectId string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'contoso-studio-token'
  properties: {
    displayName: 'Router-only shared Studio connection'
    path: 'api/connections'
    protocols: ['https']
    subscriptionRequired: false
    type: 'http'
  }
}

resource logger 'Microsoft.ApiManagement/service/loggers@2024-05-01' existing = {
  parent: apim
  name: 'applicationinsights'
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

resource operation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'shared-studio-token'
  properties: {
    displayName: 'Get fixed shared Studio token'
    method: 'POST'
    urlTemplate: '/token'
  }
}

var template = loadTextContent('./apim/token-policy.xml')
var policy = replace(replace(replace(replace(replace(template,
  '__TENANT__', tenantId),
  '__API_AUDIENCE__', apiAudience),
  '__ROUTER_CLIENT__', routerClientId),
  '__ROUTER_OID__', routerObjectId),
  '__SHARED_OWNER__', sharedOwnerObjectId)

resource operationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = {
  parent: operation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: policy
  }
}
