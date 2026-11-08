// Adapted from the official 11-private-network-basic-vnet sample. No BYO Cosmos/Search.
// https://github.com/microsoft-foundry/foundry-samples/tree/main/infrastructure/infrastructure-setup-bicep/11-private-network-basic-vnet
param location string
param suffix string
param hostedAgentSubnetId string
param privateEndpointSubnetId string
param foundryDnsZoneIds array
param acrDnsZoneId string
param applicationInsightsId string
param networkRuleBypassAllowedForTasks bool
param deployModel bool
param modelDeploymentName string
param modelName string
param modelVersion string
param modelSkuName string
param modelCapacity int
param tags object

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: 'contoso-foundry-${suffix}'
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    allowProjectManagement: true
    customSubDomainName: 'contoso-foundry-${suffix}'
    publicNetworkAccess: 'Disabled'
    disableLocalAuth: true
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      ipRules: []
      virtualNetworkRules: []
    }
    networkInjections: [{
      scenario: 'agent'
      subnetArmId: hostedAgentSubnetId
      useMicrosoftManagedNetwork: false
    }]
  }
}

resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'contoso-foundry-${suffix}-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [{
      name: 'foundry'
      properties: {
        privateLinkServiceId: account.id
        groupIds: ['account']
      }
    }]
  }
}

resource dnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [for (zoneId, i) in foundryDnsZoneIds: {
      name: 'foundry-${i}'
      properties: { privateDnsZoneId: zoneId }
    }]
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: account
  name: 'contoso-project-${suffix}'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'CONTOSO isolated hosted agents'
    description: 'Separate CONTOSO basic agent project with private VNet-injected backends.'
  }
  dependsOn: [dnsGroup]
}

resource capabilityHost 'Microsoft.CognitiveServices/accounts/projects/capabilityHosts@2025-04-01-preview' = {
  parent: project
  name: 'contoso-agents'
  // The official sample uses this property but the published 2025-04-01-preview
  // ARM/Bicep type omits it (BCP037). Preserve the sample shape without hiding the warning.
  properties: { capabilityHostKind: 'Agents' }
}

resource model 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = if (deployModel) {
  parent: account
  name: modelDeploymentName
  sku: {
    name: modelSkuName
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2025-06-01-preview' = {
  name: 'contosoacr${suffix}'
  location: location
  tags: tags
  sku: { name: 'Premium' }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    dataEndpointEnabled: true
    publicNetworkAccess: 'Disabled'
    networkRuleSet: {
      defaultAction: 'Deny'
      ipRules: []
    }
    networkRuleBypassOptions: 'AzureServices'
    networkRuleBypassAllowedForTasks: networkRuleBypassAllowedForTasks
  }
}

resource acrEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'contoso-acr-${suffix}-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [{
      name: 'registry'
      properties: {
        privateLinkServiceId: acr.id
        groupIds: ['registry']
      }
    }]
  }
}

resource acrDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: acrEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{
      name: 'registry'
      properties: { privateDnsZoneId: acrDnsZoneId }
    }]
  }
}

resource projectPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, project.id, 'AcrPull')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: last(split(applicationInsightsId, '/'))
}

// ConnectionString is resolved by ARM, never accepted as input or emitted as an output.
resource tracingConnection 'Microsoft.CognitiveServices/accounts/connections@2025-04-01-preview' = {
  parent: account
  name: 'contoso-appinsights'
  properties: {
    category: 'AppInsights'
    target: insights.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: { key: insights.properties.ConnectionString }
    metadata: {
      ApiType: 'Azure'
      ResourceId: insights.id
    }
  }
}

resource projectMetrics 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, project.id, 'Monitoring Metrics Publisher')
  scope: insights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output accountName string = account.name
output projectName string = project.name
output projectPrincipalId string = project.identity.principalId
output acrName string = acr.name
output resources object = {
  accountId: account.id
  accountName: account.name
  accountEndpoint: account.properties.endpoint
  accountPrincipalId: account.identity.principalId
  projectId: project.id
  projectName: project.name
  projectEndpoints: project.properties.endpoints
  projectPrincipalId: project.identity.principalId
  capabilityHostId: capabilityHost.id
  acrId: acr.id
  acrName: acr.name
  acrLoginServer: acr.properties.loginServer
  foundryPrivateEndpointId: endpoint.id
  acrPrivateEndpointId: acrEndpoint.id
  modelDeploymentId: deployModel ? model!.id : ''
  modelDeploymentName: deployModel ? model!.name : ''
}
