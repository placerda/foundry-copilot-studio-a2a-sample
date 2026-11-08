param location string
param suffix string
param vnetAddressPrefix string
param hostedAgentSubnetPrefix string
param flexSubnetPrefix string
param privateEndpointSubnetPrefix string
param includeHostQueueTableEndpoints bool
param tags object

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'contoso-vnet-${suffix}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [vnetAddressPrefix]
    }
  }
}

resource hostedSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: 'hosted-agents'
  properties: {
    addressPrefix: hostedAgentSubnetPrefix
    delegations: [{
      name: 'hosted-agent-environments'
      properties: {
        serviceName: 'Microsoft.App/environments'
      }
    }]
  }
}

resource flexSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: 'flex-gateway'
  properties: {
    addressPrefix: flexSubnetPrefix
    delegations: [{
      name: 'flex-environments'
      properties: {
        serviceName: 'Microsoft.App/environments'
      }
    }]
  }
  // Serialize subnet PUTs to avoid concurrent updates of the same new VNet.
  dependsOn: [hostedSubnet]
}

resource peSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: 'private-endpoints'
  properties: {
    addressPrefix: privateEndpointSubnetPrefix
    privateEndpointNetworkPolicies: 'Disabled'
  }
  dependsOn: [flexSubnet]
}

var dnsNames = concat([
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
  'privatelink.azurecr.io'
  'privatelink.blob.${environment().suffixes.storage}'
  'privatelink.monitor.azure.com'
  'privatelink.oms.opinsights.azure.com'
  'privatelink.ods.opinsights.azure.com'
  'privatelink.agentsvc.azure-automation.net'
], includeHostQueueTableEndpoints ? [
  'privatelink.queue.${environment().suffixes.storage}'
  'privatelink.table.${environment().suffixes.storage}'
] : [])

resource dnsZones 'Microsoft.Network/privateDnsZones@2020-06-01' = [for zone in dnsNames: {
  name: zone
  location: 'global'
  tags: tags
}]

resource dnsLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (zone, i) in dnsNames: {
  parent: dnsZones[i]
  name: 'contoso-${suffix}'
  location: 'global'
  properties: {
    virtualNetwork: { id: vnet.id }
    registrationEnabled: false
  }
}]

output hostedAgentSubnetId string = hostedSubnet.id
output flexSubnetId string = flexSubnet.id
output privateEndpointSubnetId string = peSubnet.id
output foundryDnsZoneIds array = [for i in range(0, 3): dnsZones[i].id]
output acrDnsZoneId string = dnsZones[3].id
output blobDnsZoneId string = dnsZones[4].id
output monitorDnsZoneIds array = [for i in [5, 6, 7, 8, 4]: dnsZones[i].id]
output queueDnsZoneId string = includeHostQueueTableEndpoints ? dnsZones[9].id : ''
output tableDnsZoneId string = includeHostQueueTableEndpoints ? dnsZones[10].id : ''
var privateDnsZoneIds = [for i in range(0, length(dnsNames)): dnsZones[i].id]
output resources object = {
  vnetId: vnet.id
  vnetName: vnet.name
  hostedAgentSubnetId: hostedSubnet.id
  flexSubnetId: flexSubnet.id
  privateEndpointSubnetId: peSubnet.id
  privateDnsZoneIds: privateDnsZoneIds
}
