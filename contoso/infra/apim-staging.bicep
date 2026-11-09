targetScope = 'resourceGroup'

@description('Staging only. Verify subscription, cost, network and OAuth gates before deployment.')
param apimName string
param publisherName string
param publisherEmail string
@allowed(['eastus2'])
param location string = 'eastus2'
@description('Existing verified VNet in this resource group. Do not use stale sample bindings.')
param vnetName string
@description('New dedicated subnet name; must not collide with any existing subnet.')
param subnetName string
@description('Explicit, preflight-verified unused CIDR inside the existing VNet; no default is inferred.')
param subnetPrefix string
param foundryAccountName string
param foundryProjectName string
param appInsightsName string

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: vnetName
}
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundry
  name: foundryProjectName
}
resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${apimName}-nsg'
  location: location
  properties: {
    securityRules: [
      {
        name: 'PublicAuthenticatedHttps'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
      {
        name: 'ApimManagement'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'ApiManagement'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '3443'
        }
      }
      {
        name: 'LoadBalancer'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'AzureLoadBalancer'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '6390'
        }
      }
    ]
  }
}

// Preserve existing VNet/DNS configuration; add only a separately verified subnet.
resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: subnetName
  properties: {
    addressPrefix: subnetPrefix
    networkSecurityGroup: {
      id: nsg.id
    }
    delegations: []
  }
}

module apimFoundation 'br/public:avm/res/api-management/service:0.15.0' = {
  name: '${apimName}-foundation-${uniqueString(deployment().name)}'
  params: {
    name: apimName
    location: location
    enableTelemetry: false
    sku: 'Developer'
    skuCapacity: 1
    managedIdentities: {
      systemAssigned: true
    }
    publisherName: publisherName
    publisherEmail: publisherEmail
    virtualNetworkType: 'External'
    subnetResourceId: subnet.id
    publicNetworkAccess: 'Enabled'
  }
}
var apimResourceId = resourceId('Microsoft.ApiManagement/service', apimName)

resource foundryAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, apimResourceId, 'Foundry User')
  scope: project
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
    principalId: apimFoundation.outputs.systemAssignedMIPrincipalId!
    principalType: 'ServicePrincipal'
  }
}
resource metricsAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, apimResourceId, 'Monitoring Metrics Publisher')
  scope: insights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
    principalId: apimFoundation.outputs.systemAssignedMIPrincipalId!
    principalType: 'ServicePrincipal'
  }
}
resource logger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  name: '${apimName}/applicationinsights'
  properties: {
    loggerType: 'applicationInsights'
    resourceId: insights.id
    credentials: {
      connectionString: insights.properties.ConnectionString
      identityClientId: 'SystemAssigned'
    }
    isBuffered: true
  }
  dependsOn: [metricsAccess]
}
resource diagnostic 'Microsoft.ApiManagement/service/diagnostics@2024-05-01' = {
  name: '${apimName}/applicationinsights'
  properties: {
    loggerId: logger.id
    alwaysLog: 'allErrors'
    sampling: {
      samplingType: 'fixed'
      percentage: 10
    }
    logClientIp: false
    httpCorrelationProtocol: 'W3C'
    frontend: {
      request: { headers: ['x-request-id'], body: { bytes: 0 } }
      response: { headers: ['x-request-id'], body: { bytes: 0 } }
    }
    backend: {
      request: { headers: ['x-request-id'], body: { bytes: 0 } }
      response: { headers: ['x-request-id'], body: { bytes: 0 } }
    }
  }
}

// No business/token API is published before exact caller and OAuth acceptance.
output apimId string = apimFoundation.outputs.resourceId
output gatewayUrl string = 'https://${apimFoundation.outputs.name}.azure-api.net'
output principalId string = apimFoundation.outputs.systemAssignedMIPrincipalId!
output subnetId string = subnet.id
