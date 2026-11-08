# Supplier–Buyer Agent Collaboration: connectivity

The main integration direction is **Microsoft Foundry → Copilot Studio through A2A**. There is no reverse Studio-to-Foundry trigger, Power Automate flow, or Microsoft 365 channel in this implementation.

The [four synthetic B2B scenarios](../contoso-demo/scenarios.json) use the `supplier-delivery-terms` capability and [versioned supplier source](../contoso-demo/knowledge/supplier-delivery-terms.md). The approval request has no matching capability: it must not invoke a provider or claim a buyer approval. Source attribution and authorization are preserved across the same numbered stages **1–4** shown in the [overview](architecture-overview.svg) and [detailed diagram](architecture.svg). The agents explain conditions; they do not create orders or dispatch goods.

## Runtime and identity

| Connection | Network / protocol | Authorization |
|---|---|---|
| Browser → optional external requester | Public HTTPS | Entra delegated bearer, exact guest, scope, issuer, and audience |
| External requester → Azure Functions gateway | Public HTTPS, native A2A bridge | Requester application credential exchanged for a gateway access token |
| Gateway → Foundry project / Hosted agent endpoint | VNet integration → Foundry private endpoint, HTTPS | Gateway managed identity, scoped Foundry role |
| Foundry platform → Hosted runtime | Platform dispatch into the injected Hosted subnet | Explicit platform caller allowlist |
| Hosted runtime → gateway connection broker | Gateway's authenticated **public** HTTPS listener | Actual Hosted managed identity and `CONTOSO.Connection.Use` |
| Broker → Entra OAuth endpoint | Public HTTPS | Approved guest consent, bound callback, protected refresh state |
| Hosted runtime → Copilot Studio A2A endpoint | Public outbound HTTPS to `api.powerplatform.com` | Delegated `MCS.InvokeAsA2A` token |
| Broker / gateway → source and token storage | Blob private endpoint, HTTPS | Managed identity and container-scoped roles, protected token state |
| Gateway → host package storage | Separate host Blob private endpoint, HTTPS | Managed identity |
| Hosted platform → ACR | Registry and data private endpoints, HTTPS | Registry connection plus actual agent `AcrPull` |
| Gateway / Foundry telemetry → Azure Monitor | AMPLS private endpoint, HTTPS | Entra-authenticated ingestion, private workspace |

The gateway's public listener is intentional. VNet integration supplies **outbound** private connectivity, not a private inbound listener. Managed identity provides authorization but does not replace network reachability.

Copilot Studio and Entra remain public SaaS destinations reached over TLS. The sample does not claim that the complete end-to-end route is private. No AWS-to-Azure site-to-site tunnel is required.

## Network topology

The checked reference uses three separate subnets inside one Azure VNet. Hosted agents and the Flex gateway each have a dedicated `Microsoft.App/environments` delegation. The private-endpoint subnet has no service delegation. An optional point-to-site VPN uses a separate `GatewaySubnet`.

Foundry, ACR, both Storage accounts, and Azure Monitor are managed services attached through Private Link. They are not VMs inside the subnet. The Hosted runtime is the VNet-injected component.

Nine private DNS zones are linked to the VNet:

```text
privatelink.cognitiveservices.azure.com
privatelink.openai.azure.com
privatelink.services.ai.azure.com
privatelink.azurecr.io
privatelink.blob.core.windows.net
privatelink.monitor.azure.com
privatelink.oms.opinsights.azure.com
privatelink.ods.opinsights.azure.com
privatelink.agentsvc.azure-automation.net
```

The Blob zone is shared by both storage private endpoints and the monitoring connection. Queue and Table zones are additional opt-in resources, not part of the baseline.

Subnet NSGs and outbound rules must preserve platform-required traffic, Azure DNS, and public HTTPS to the gateway, Entra, and Copilot Studio. The reference Bicep does not manage deployment-specific NSGs. Apply and review those separately rather than claiming an unimplemented firewall or fixed IP allowlist.

## Deployment and operator paths

An operator uses Entra-authenticated public HTTPS to the gateway's restricted publication routes. The gateway runs the publication worker inside its integrated VNet and uses private Foundry, registry, and Blob endpoints. Metadata, manifests, extensions, and images are pinned and bound to operator configuration.

Azure Resource Manager is a separate public HTTPS control plane. The reference image builder can schedule a persistent ACR task through ARM. The task's managed identity and explicit trusted-service bypass permit a private registry build without opening anonymous registry access. That bypass is not general public network access and is disabled in the sample foundation by default.

Direct operator access to private endpoints can optionally use an Entra-authenticated point-to-site Azure VPN plus correctly scoped private DNS. This is distinct from the cloud runtime route. The original cloud route was accepted without the client VPN, while direct VPN/DNS acceptance was deferred.

The published source includes only synthetic scenarios and reusable code. Resource names in the drawing are logical labels, not existing deployment identifiers.
