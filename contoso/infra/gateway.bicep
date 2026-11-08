param location string
param suffix string
param entraApiClientId string
param entraTenantId string
param entraAdditionalAllowedAudiences array
param flexSubnetId string
param hostStorageName string
param sourceStorageName string
param deploymentContainerName string
param sourceContainerName string
param tokenContainerName string
param applicationInsightsName string
param foundryAccountName string
param foundryProjectName string
param projectPrincipalId string
param acrName string
param includeHostQueueTableEndpoints bool
param pythonVersion string
param instanceMemoryMB int
param maximumInstanceCount int
param tags object

resource host 'Microsoft.Storage/storageAccounts@2025-01-01' existing = {
  name: hostStorageName
}
resource source 'Microsoft.Storage/storageAccounts@2025-01-01' existing = {
  name: sourceStorageName
}
resource sourceBlobs 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' existing = {
  parent: source
  name: 'default'
}
resource sources 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' existing = {
  parent: sourceBlobs
  name: sourceContainerName
}
resource tokens 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' existing = {
  parent: sourceBlobs
  name: tokenContainerName
}
resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}
resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: foundryAccountName
}
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' existing = {
  parent: account
  name: foundryProjectName
}
resource acr 'Microsoft.ContainerRegistry/registries@2025-06-01-preview' existing = {
  name: acrName
}

resource plan 'Microsoft.Web/serverfarms@2025-03-01' = {
  name: 'contoso-flex-${suffix}'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}

resource app 'Microsoft.Web/sites@2025-03-01' = {
  name: 'contoso-gateway-${suffix}'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    // Auth is a separate ARM child PUT, not atomic with site creation. Leave the app
    // stopped until the parent verifies auth + RBAC + OneDeploy and explicitly enables it.
    enabled: false
    scmSiteAlsoStopped: false
    serverFarmId: plan.id
    publicNetworkAccess: 'Enabled'
    httpsOnly: true
    virtualNetworkSubnetId: flexSubnetId
    // Flex routes all traffic via its VNet by design, including private deployment storage.
    siteConfig: {
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      ftpsState: 'Disabled'
      remoteDebuggingEnabled: false
    }
    functionAppConfig: {
      runtime: { name: 'python', version: pythonVersion }
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${host.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: {
        instanceMemoryMB: instanceMemoryMB
        maximumInstanceCount: maximumInstanceCount
        alwaysReady: []
      }
    }
  }
}

resource authentication 'Microsoft.Web/sites/config@2025-03-01' = {
  parent: app
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'Return401'
    }
    httpSettings: { requireHttps: true }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraApiClientId
          openIdIssuer: uri(environment().authentication.loginEndpoint, '${entraTenantId}/v2.0')
        }
        validation: {
          allowedAudiences: union([entraApiClientId, 'api://${entraApiClientId}'], entraAdditionalAllowedAudiences)
        }
      }
    }
    login: { tokenStore: { enabled: false } }
  }
}

resource scmBasicAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2025-03-01' = {
  parent: app
  name: 'scm'
  properties: { allow: false }
}
resource ftpBasicAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2025-03-01' = {
  parent: app
  name: 'ftp'
  properties: { allow: false }
}

resource settings 'Microsoft.Web/sites/config@2025-03-01' = {
  parent: app
  name: 'appsettings'
  properties: union({
    AzureWebJobsStorage__accountName: host.name
    AzureWebJobsStorage__blobServiceUri: host.properties.primaryEndpoints.blob
    AzureWebJobsStorage__credential: 'managedidentity'
    APPLICATIONINSIGHTS_CONNECTION_STRING: insights.properties.ConnectionString
    APPLICATIONINSIGHTS_AUTHENTICATION_STRING: 'Authorization=AAD'
    CONTOSO_SOURCE_BLOB_ENDPOINT: source.properties.primaryEndpoints.blob
    CONTOSO_SOURCE_CONTAINER: sources.name
    CONTOSO_TOKEN_CONTAINER: tokens.name
    CONTOSO_FOUNDRY_PROJECT_ENDPOINT: project.properties.endpoints['AI Foundry API']
    CONTOSO_ACR_LOGIN_SERVER: acr.properties.loginServer
    CONTOSO_ENTRA_TENANT_ID: entraTenantId
    CONTOSO_ENTRA_API_CLIENT_ID: entraApiClientId
  }, includeHostQueueTableEndpoints ? {
    AzureWebJobsStorage__queueServiceUri: host.properties.primaryEndpoints.queue
    AzureWebJobsStorage__tableServiceUri: host.properties.primaryEndpoints.table
  } : {})
  dependsOn: [authentication]
}

var blobOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var blobReader = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
var blobContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource hostAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(host.id, app.id, blobOwner)
  scope: host
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobOwner)
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}
resource sourceAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sources.id, app.id, blobContributor)
  scope: sources
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributor)
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}
resource projectSourceAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sources.id, projectPrincipalId, blobReader)
  scope: sources
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobReader)
    principalId: projectPrincipalId
    principalType: 'ServicePrincipal'
  }
}
resource tokenAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(tokens.id, app.id, blobContributor)
  scope: tokens
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributor)
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}
resource metricsAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, app.id, 'Monitoring Metrics Publisher')
  scope: insights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}
resource foundryAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, app.id, 'Azure AI User')
  scope: project
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// A custom task-only control-plane role avoids the built-in Tasks Contributor's
// agent-pool management and deployments/* permissions. It cannot assign task SAMI RBAC.
resource taskRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'contoso-persistent-task-executor', suffix)
  properties: {
    roleName: 'CONTOSO persistent task executor ${suffix}'
    description: 'Manage and run persistent ACR tasks in this CONTOSO registry; no agent pools or role assignments.'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [{
      actions: [
        'Microsoft.ContainerRegistry/registries/read'
        'Microsoft.ContainerRegistry/registries/tasks/read'
        'Microsoft.ContainerRegistry/registries/tasks/write'
        'Microsoft.ContainerRegistry/registries/tasks/delete'
        'Microsoft.ContainerRegistry/registries/tasks/listDetails/action'
        'Microsoft.ContainerRegistry/registries/scheduleRun/action'
        'Microsoft.ContainerRegistry/registries/runs/read'
        'Microsoft.ContainerRegistry/registries/runs/write'
        'Microsoft.ContainerRegistry/registries/runs/cancel/action'
        'Microsoft.ContainerRegistry/registries/runs/listLogSasUrl/action'
        'Microsoft.ContainerRegistry/registries/taskruns/read'
        'Microsoft.ContainerRegistry/registries/taskruns/write'
        'Microsoft.ContainerRegistry/registries/taskruns/delete'
        'Microsoft.ContainerRegistry/registries/taskruns/listDetails/action'
        'Microsoft.ContainerRegistry/registries/taskruns/operationStatuses/read'
      ]
      notActions: []
      dataActions: []
      notDataActions: []
    }]
  }
}
resource taskAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, app.id, taskRole.id)
  scope: acr
  properties: {
    roleDefinitionId: taskRole.id
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource queueAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (includeHostQueueTableEndpoints) {
  name: guid(host.id, app.id, 'Storage Queue Data Contributor')
  scope: host
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '974c5e8b-45b9-4653-ba55-5f855dd0fb88')
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}
resource tableAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (includeHostQueueTableEndpoints) {
  name: guid(host.id, app.id, 'Storage Table Data Contributor')
  scope: host
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

var scmHostNames = filter(app.properties.enabledHostNames, hostname => contains(hostname, '.scm.'))
output resources object = {
  gatewayId: app.id
  gatewayName: app.name
  gatewayEndpoint: 'https://${app.properties.defaultHostName}'
  gatewayHostNames: app.properties.enabledHostNames
  gatewayScmEndpoint: empty(scmHostNames) ? '' : 'https://${first(scmHostNames)}'
  gatewayPrincipalId: app.identity.principalId
  gatewayAuthSettingsId: authentication.id
  gatewayPlanId: plan.id
  gatewayPlanName: plan.name
  taskExecutorRoleId: taskRole.id
  taskExecutorRoleAssignmentId: taskAccess.id
}
