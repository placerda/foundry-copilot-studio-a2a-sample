targetScope = 'resourceGroup'

param apiCenterName string
param apimName string

resource center 'Microsoft.ApiCenter/services@2024-03-01' existing = {
  name: apiCenterName
}

resource workspace 'Microsoft.ApiCenter/services/workspaces@2024-03-01' existing = {
  parent: center
  name: 'default'
}

resource routingSchema 'Microsoft.ApiCenter/services/metadataSchemas@2024-03-01' = {
  parent: center
  name: 'contoso-routing'
  properties: {
    schema: loadTextContent('./apicenter/routing.schema.json')
    assignedTo: [
      {
        entity: 'api'
        required: false
        deprecated: false
      }
    ]
  }
}

var providers = [
  {
    id: 'studio-copilot-harness'
    title: 'Contoso Motors Supplier A2A'
    capability: 'supplier-delivery-terms'
    variant: 'copilot-harness'
    sourceId: 'CONTOSO-MOTORS-SUPPLIER-001'
    sourceTitle: 'Contoso Motors supplier delivery proposal'
    reference: 'contoso-demo/knowledge/motors-supplier-delivery-terms-v1.md'
    sections: [
      'Fictional proposal'
      'Buyer acceptance conditions'
      'Recommended next step'
      'Evidence limits'
    ]
  }
  {
    id: 'motors-logistics'
    title: 'Contoso Motors Logistics A2A'
    capability: 'knowledge-knowledge'
    variant: 'none'
    sourceId: 'CONTOSO-MOTORS-LOGISTICS-001'
    sourceTitle: 'Contoso Motors returnable packaging coordination'
    reference: 'contoso-demo/knowledge/motors-logistics-returnables-v1.md'
    sections: [
      'Receiving coordination'
      'Returnable packaging process'
      'Recommended next step'
      'Evidence limits'
    ]
  }
]

resource routes 'Microsoft.ApiCenter/services/workspaces/apis@2024-03-01' = [for provider in providers: {
  parent: workspace
  name: provider.id
  properties: {
    title: provider.title
    kind: 'rest'
    description: 'Synthetic Contoso Motors A2A destination. No business actions are authorized.'
    customProperties: {
      'contoso-routing': {
        schema_version: 1
        publication_state: 'active'
        provider_id: provider.id
        capability: provider.capability
        studio_variant: provider.variant
        endpoint: 'https://${apimName}.azure-api.net/agents/${provider.id}/versions/1/a2a'
        protocol: 'a2a'
        protocol_version: '1.0'
        agent_version: '1'
        sources: [
          {
            source_id: provider.sourceId
            provider_id: provider.id
            version: '1'
            title: provider.sourceTitle
            classification: 'Synthetic demonstration data'
            reference: provider.reference
            sections: provider.sections
          }
        ]
      }
    }
  }
  dependsOn: [
    routingSchema
  ]
}]

resource routeVersions 'Microsoft.ApiCenter/services/workspaces/apis/versions@2024-03-01' = [for (provider, index) in providers: {
  parent: routes[index]
  name: 'version-1'
  properties: {
    title: '1'
    lifecycleStage: 'preview'
  }
}]
