param location string
param suffix string
param vnetName string
param aadAudience string
param aadTenant string
param aadIssuer string
param tags object
param gatewaySubnetPrefix string = '10.77.254.0/27'
param clientAddressPrefix string = '172.30.77.0/24'

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: vnetName
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: 'GatewaySubnet'
  properties: {
    addressPrefix: gatewaySubnetPrefix
  }
}

resource publicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'contoso-vpn-ip-${suffix}'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  zones: ['1', '2', '3']
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource gateway 'Microsoft.Network/virtualNetworkGateways@2024-05-01' = {
  name: 'contoso-vpn-${suffix}'
  location: location
  tags: tags
  properties: {
    gatewayType: 'Vpn'
    vpnType: 'RouteBased'
    vpnGatewayGeneration: 'Generation1'
    activeActive: false
    enableBgp: false
    sku: {
      name: 'VpnGw1AZ'
      tier: 'VpnGw1AZ'
    }
    ipConfigurations: [{
      name: 'gateway'
      properties: {
        privateIPAllocationMethod: 'Dynamic'
        publicIPAddress: { id: publicIp.id }
        subnet: { id: subnet.id }
      }
    }]
    vpnClientConfiguration: {
      vpnClientAddressPool: {
        addressPrefixes: [clientAddressPrefix]
      }
      vpnClientProtocols: ['OpenVPN']
      vpnAuthenticationTypes: ['AAD']
      aadAudience: aadAudience
      aadTenant: aadTenant
      aadIssuer: aadIssuer
    }
  }
}

output resources object = {
  gatewayId: gateway.id
  gatewayName: gateway.name
  publicIpId: publicIp.id
  clientAddressPrefix: clientAddressPrefix
}
