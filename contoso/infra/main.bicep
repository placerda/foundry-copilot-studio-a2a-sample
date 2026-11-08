targetScope = 'resourceGroup'

@description('Deploy only into a dedicated, new CONTOSO resource group. No existing resource references are used.')
@allowed(['eastus2'])
param location string = 'eastus2'

@description('Explicit globally unique lowercase alphanumeric suffix, 3-12 characters. Never reuse another workload name.')
@minLength(3)
@maxLength(12)
param projectSuffix string

@description('Existing Entra registration application/client ID for the gateway API, not its object ID.')
@minLength(36)
@maxLength(36)
param entraApiClientId string

@description('Contoso tenant GUID that issues gateway API access tokens.')
@minLength(36)
@maxLength(36)
param entraTenantId string

@description('Optional exact API audiences in addition to the application ID. No wildcard audiences.')
param entraAdditionalAllowedAudiences array = []

param vnetAddressPrefix string = '10.77.0.0/16'
@description('Dedicated Hosted Agent subnet; use /24 or larger, and do not share it with Flex.')
param hostedAgentSubnetPrefix string = '10.77.0.0/24'
@description('Dedicated Flex subnet delegated to Microsoft.App/environments; /27 minimum, /24 default.')
param flexSubnetPrefix string = '10.77.1.0/24'
param privateEndpointSubnetPrefix string = '10.77.2.0/24'

@description('HTTP-only default: five PEs and nine DNS zones. Queue/Durable/Table bindings require a new review; enabling this adds two PEs and two zones, outside the quoted budget.')
param includeHostQueueTableEndpoints bool = false

@description('Explicitly allow persistent ACR Tasks with system-assigned identity to bypass registry network rules. Does not authorize quick az acr build tasks, create tasks, or grant their future identities AcrPush.')
param networkRuleBypassAllowedForTasks bool = false

@description('Only set true when the parent explicitly requests model provisioning. Quota/advertised availability does not guarantee regional capacity.')
param deployModel bool = false
param modelDeploymentName string = 'gpt-4o-mini'
param modelName string = 'gpt-4o-mini'
param modelVersion string = '2024-07-18'
@allowed(['Standard'])
param modelSkuName string = 'Standard'
@description('Standard deployment capacity units, not PTUs. Default one unit; model-specific TPM mapping applies.')
@minValue(1)
param modelCapacity int = 1

@allowed(['3.11', '3.12', '3.13'])
param pythonVersion string = '3.12'
@allowed([512, 2048, 4096])
param instanceMemoryMB int = 512
@description('Flex service minimum scale ceiling is 40; this is not provisioned/always-ready capacity or a spending cap.')
@minValue(40)
@maxValue(1000)
param maximumInstanceCount int = 40

param sourceContainerName string = 'sources'
param tokenContainerName string = 'encrypted-tokens'
param deploymentContainerName string = 'gateway-package'
param tags object = {
  workload: 'contoso'
  isolation: 'private-backends'
}

var suffix = toLower(projectSuffix)

module network 'network.bicep' = {
  name: 'contoso-network-${suffix}'
  params: {
    location: location
    suffix: suffix
    vnetAddressPrefix: vnetAddressPrefix
    hostedAgentSubnetPrefix: hostedAgentSubnetPrefix
    flexSubnetPrefix: flexSubnetPrefix
    privateEndpointSubnetPrefix: privateEndpointSubnetPrefix
    includeHostQueueTableEndpoints: includeHostQueueTableEndpoints
    tags: tags
  }
}

module storage 'storage.bicep' = {
  name: 'contoso-storage-${suffix}'
  params: {
    location: location
    suffix: suffix
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    blobDnsZoneId: network.outputs.blobDnsZoneId
    queueDnsZoneId: network.outputs.queueDnsZoneId
    tableDnsZoneId: network.outputs.tableDnsZoneId
    includeHostQueueTableEndpoints: includeHostQueueTableEndpoints
    sourceContainerName: sourceContainerName
    tokenContainerName: tokenContainerName
    deploymentContainerName: deploymentContainerName
    tags: tags
  }
}

module monitoring 'monitoring.bicep' = {
  name: 'contoso-monitoring-${suffix}'
  params: {
    location: location
    suffix: suffix
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    monitorDnsZoneIds: network.outputs.monitorDnsZoneIds
    tags: tags
  }
}

module foundry 'foundry.bicep' = {
  name: 'contoso-foundry-${suffix}'
  params: {
    location: location
    suffix: suffix
    hostedAgentSubnetId: network.outputs.hostedAgentSubnetId
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    foundryDnsZoneIds: network.outputs.foundryDnsZoneIds
    acrDnsZoneId: network.outputs.acrDnsZoneId
    applicationInsightsId: monitoring.outputs.applicationInsightsId
    networkRuleBypassAllowedForTasks: networkRuleBypassAllowedForTasks
    deployModel: deployModel
    modelDeploymentName: modelDeploymentName
    modelName: modelName
    modelVersion: modelVersion
    modelSkuName: modelSkuName
    modelCapacity: modelCapacity
    tags: tags
  }
}

module gateway 'gateway.bicep' = {
  name: 'contoso-gateway-${suffix}'
  params: {
    location: location
    suffix: suffix
    entraApiClientId: entraApiClientId
    entraTenantId: entraTenantId
    entraAdditionalAllowedAudiences: entraAdditionalAllowedAudiences
    flexSubnetId: network.outputs.flexSubnetId
    hostStorageName: storage.outputs.hostStorageName
    sourceStorageName: storage.outputs.sourceStorageName
    deploymentContainerName: deploymentContainerName
    sourceContainerName: sourceContainerName
    tokenContainerName: tokenContainerName
    applicationInsightsName: monitoring.outputs.applicationInsightsName
    foundryAccountName: foundry.outputs.accountName
    foundryProjectName: foundry.outputs.projectName
    projectPrincipalId: foundry.outputs.projectPrincipalId
    acrName: foundry.outputs.acrName
    includeHostQueueTableEndpoints: includeHostQueueTableEndpoints
    pythonVersion: pythonVersion
    instanceMemoryMB: instanceMemoryMB
    maximumInstanceCount: maximumInstanceCount
    tags: tags
  }
}

output networkResources object = network.outputs.resources
output storageResources object = storage.outputs.resources
output monitoringResources object = monitoring.outputs.resources
output foundryResources object = foundry.outputs.resources
output gatewayResources object = gateway.outputs.resources
output privateEndpointCount int = includeHostQueueTableEndpoints ? 7 : 5
output privateDnsZoneCount int = includeHostQueueTableEndpoints ? 11 : 9
output gatewayInitiallyEnabled bool = false
