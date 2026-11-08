targetScope = 'resourceGroup'

@description('Existing new CONTOSO Foundry account name in the deployment resource group; not an ARM ID.')
@minLength(2)
param foundryAccountName string

@description('Existing new CONTOSO project child name; not the account/project path or an ARM ID.')
@minLength(2)
param foundryProjectName string

@description('Existing new CONTOSO ACR name in the same deployment resource group; not its hostname or ARM ID.')
@minLength(5)
@maxLength(50)
param acrName string

@description('Explicit project connection name, 3-33 alphanumeric/underscore/hyphen characters, starting alphanumeric. The official azd convention is <acrName>-conn when it fits.')
@minLength(3)
@maxLength(33)
param connectionName string

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: foundryAccountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' existing = {
  parent: account
  name: foundryProjectName
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-06-01-preview' existing = {
  name: acrName
}

// Exact azd Foundry connection mapping; the field named clientId deliberately uses
// project.identity.principalId, not an inferred OAuth client ID or a future agent identity.
// https://github.com/Azure/azure-dev/blob/main/cli/azd/extensions/azure.ai.agents/internal/synthesis/templates/modules/foundry-project.bicep
// https://learn.microsoft.com/azure/templates/microsoft.cognitiveservices/2025-04-01-preview/accounts/projects/connections
resource connection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: project
  name: connectionName
  properties: {
    category: 'ContainerRegistry'
    target: registry.properties.loginServer
    authType: 'ManagedIdentity'
    credentials: {
      clientId: project.identity.principalId
      resourceId: registry.id
    }
    isSharedToAll: true
    metadata: {
      ResourceId: registry.id
    }
  }
}

output connectionId string = connection.id
output registryConnectionName string = connection.name
output connectionTarget string = registry.properties.loginServer
output acrId string = registry.id
output projectId string = project.id
output projectPrincipalId string = project.identity.principalId
