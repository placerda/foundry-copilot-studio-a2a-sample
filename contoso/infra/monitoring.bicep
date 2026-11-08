param location string
param suffix string
param privateEndpointSubnetId string
param monitorDnsZoneIds array
param tags object

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'contoso-law-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    publicNetworkAccessForIngestion: 'Disabled'
    publicNetworkAccessForQuery: 'Disabled'
    features: { disableLocalAuth: true }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'contoso-appi-${suffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: true
    publicNetworkAccessForIngestion: 'Disabled'
    publicNetworkAccessForQuery: 'Disabled'
  }
}

resource ampls 'Microsoft.Insights/privateLinkScopes@2021-07-01-preview' = {
  name: 'contoso-ampls-${suffix}'
  location: 'global'
  tags: tags
  properties: {
    accessModeSettings: {
      ingestionAccessMode: 'PrivateOnly'
      queryAccessMode: 'PrivateOnly'
    }
  }
}

resource scopedInsights 'Microsoft.Insights/privateLinkScopes/scopedResources@2021-07-01-preview' = {
  parent: ampls
  name: 'contoso-appinsights'
  properties: { linkedResourceId: insights.id }
}

resource scopedWorkspace 'Microsoft.Insights/privateLinkScopes/scopedResources@2021-07-01-preview' = {
  parent: ampls
  name: 'contoso-workspace'
  properties: { linkedResourceId: workspace.id }
}

resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'contoso-monitor-${suffix}-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [{
      name: 'azuremonitor'
      properties: {
        privateLinkServiceId: ampls.id
        groupIds: ['azuremonitor']
      }
    }]
  }
  dependsOn: [scopedInsights, scopedWorkspace]
}

resource dnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    // AMPLS also needs the same Blob zone used by both private storage accounts.
    privateDnsZoneConfigs: [for (zoneId, i) in monitorDnsZoneIds: {
      name: 'monitor-${i}'
      properties: { privateDnsZoneId: zoneId }
    }]
  }
}

output applicationInsightsId string = insights.id
output applicationInsightsName string = insights.name
output resources object = {
  applicationInsightsId: insights.id
  applicationInsightsName: insights.name
  applicationInsightsAppId: insights.properties.AppId
  logAnalyticsId: workspace.id
  logAnalyticsName: workspace.name
  amplsId: ampls.id
  amplsName: ampls.name
  privateEndpointId: endpoint.id
}
