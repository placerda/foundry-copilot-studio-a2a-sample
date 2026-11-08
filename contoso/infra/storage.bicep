param location string
param suffix string
param privateEndpointSubnetId string
param blobDnsZoneId string
param queueDnsZoneId string
param tableDnsZoneId string
param includeHostQueueTableEndpoints bool
param sourceContainerName string
param tokenContainerName string
param deploymentContainerName string
param tags object

resource host 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: 'contosohost${suffix}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    dnsEndpointType: 'Standard'
    publicNetworkAccess: 'Disabled'
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

resource source 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: 'contososrc${suffix}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    dnsEndpointType: 'Standard'
    publicNetworkAccess: 'Disabled'
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

resource hostBlobs 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: host
  name: 'default'
}

resource deployment 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = {
  parent: hostBlobs
  name: deploymentContainerName
  properties: { publicAccess: 'None' }
}

resource sourceBlobs 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: source
  name: 'default'
}

resource sources 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = {
  parent: sourceBlobs
  name: sourceContainerName
  properties: { publicAccess: 'None' }
}

resource tokens 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = {
  parent: sourceBlobs
  name: tokenContainerName
  properties: { publicAccess: 'None' }
}

var endpoints = concat([
  { name: 'host-blob', targetId: host.id, group: 'blob', zoneId: blobDnsZoneId }
  { name: 'source-blob', targetId: source.id, group: 'blob', zoneId: blobDnsZoneId }
], includeHostQueueTableEndpoints ? [
  { name: 'host-queue', targetId: host.id, group: 'queue', zoneId: queueDnsZoneId }
  { name: 'host-table', targetId: host.id, group: 'table', zoneId: tableDnsZoneId }
] : [])

resource privateEndpoints 'Microsoft.Network/privateEndpoints@2024-05-01' = [for endpoint in endpoints: {
  name: 'contoso-${endpoint.name}-${suffix}-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [{
      name: endpoint.name
      properties: {
        privateLinkServiceId: endpoint.targetId
        groupIds: [endpoint.group]
      }
    }]
  }
}]

resource dnsGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (endpoint, i) in endpoints: {
  parent: privateEndpoints[i]
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{
      name: endpoint.group
      properties: { privateDnsZoneId: endpoint.zoneId }
    }]
  }
}]

output hostStorageName string = host.name
output sourceStorageName string = source.name
var privateEndpointIds = [for i in range(0, length(endpoints)): privateEndpoints[i].id]
output resources object = {
  hostStorageId: host.id
  hostStorageName: host.name
  hostBlobEndpoint: host.properties.primaryEndpoints.blob
  deploymentContainerId: deployment.id
  deploymentContainerName: deployment.name
  deploymentContainerEndpoint: '${host.properties.primaryEndpoints.blob}${deployment.name}'
  sourceStorageId: source.id
  sourceStorageName: source.name
  sourceBlobEndpoint: source.properties.primaryEndpoints.blob
  sourceContainerId: sources.id
  sourceContainerName: sources.name
  tokenContainerId: tokens.id
  tokenContainerName: tokens.name
  privateEndpointIds: privateEndpointIds
}
