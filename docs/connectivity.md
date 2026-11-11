# Supplier–Buyer Agent Collaboration: connectivity

The [README](../README.md) starts the chat application on your own computer:
the browser and Python server share `http://localhost:8000/`. App Service is
an optional way to host that entire UI/backend in the cloud, not a prerequisite
or another chat backend. This guide separates the APIM cloud target from
the retained Function implementation, which remains until replacement acceptance.
The identity selector defaults to the fixed shared delegated connection;
automatic user delegation and SDK S2S remain disabled, not working paths.

The main integration direction is **Microsoft Foundry → Copilot Studio through A2A**. There is no reverse Studio-to-Foundry trigger, Power Automate flow, or Microsoft 365 channel in this implementation.

The [four synthetic B2B scenarios](../contoso-demo/scenarios.json) use the `supplier-delivery-terms` capability and [versioned supplier source](../contoso-demo/knowledge/supplier-delivery-terms.md). The approval request has no matching capability: it must not invoke a provider or claim a buyer approval. Source attribution and authorization are preserved across the same numbered stages **1–4** shown in the [overview](architecture-overview.svg) and [detailed diagram](architecture.svg). The agents explain conditions; they do not create orders or dispatch goods.

## Target topology (cutover pending)

| Connection | Network / protocol | Authorization |
|---|---|---|
| Browser → local Python UI/backend on your computer | Loopback HTTP/JSON at `http://localhost:8000/` | Microsoft Entra sign-in and verified guest admission |
| Optional: browser → same UI/backend hosted in App Service | Private HTTPS instead of the local loopback hop | Microsoft Entra sign-in and verified guest admission |
| Client backend → APIM | Private HTTPS, A2A 1.0 / JSON-RPC 2.0 | Fixed requester application identity and invocation-only access |
| APIM → Foundry project / Hosted endpoint | VNet injection → Foundry private endpoint, HTTPS | APIM managed identity, scoped Foundry role and explicit Router caller admission |
| Hosted runtime → APIM token API | Private HTTPS, separate from the business route | Exact Hosted identity and connection-use role, never the client requester |
| APIM Credential Manager → OAuth provider | Controlled public HTTPS, including the `AzureConnectors` service tag | Shared delegated OAuth/PKCE connection, approved owner consent and managed refresh |
| Hosted runtime → Copilot Studio | Public HTTPS, native A2A | Approved delegated `MCS.InvokeAsA2A` token |
| Private workstation/runner → Azure services | Existing corporate connectivity, private DNS and HTTPS | Explicit operator and workload authorization |

APIM is **API Management**, the gateway and managed OAuth broker. APIC is
**API Center**, a separate catalog, not another gateway or a token service.
APIC discovery is not implemented in the current request path.

The default client runs the chat UI and Python requester on your own computer,
not an AI model. Use `start-local.ps1` and open `http://localhost:8000/`.
App Service is an optional Azure host for that same complete application.
It replaces the local client host, not the agents or gateway. Cloud hosting
requires adapting loopback/Host/Origin checks, credentials, Entra redirects
and private access. The Windows launcher is not an Azure deployment script.

The APIM target with a local client uses three cloud subnets: Foundry Hosted,
APIM Internal without delegation, and shared private endpoints. Foundry,
Key Vault, ACR and AMPLS have four private-endpoint attachments.
Only the optional App Service host adds a fourth subnet for outbound integration
and a fifth private endpoint for its ingress. These are separate network
features. Reuse corporate VPN/ExpressRoute and private DNS for private clients
and operators. Internal APIM is not reachable through the old public-ingress
assumption, whether the client is local or cloud-hosted.

Credential Manager replaces the custom Function broker and its dedicated
host/token-state storage in this target, not in the live deployment. It needs
outbound HTTPS to its managed service and OAuth provider. Studio and Entra
remain public SaaS services. APIM cannot execute image builds or publication
jobs, which need a separately authorized private execution environment.
No existing Function or storage retirement is authorized by this diagram.

See Microsoft's [Credential Manager overview](https://learn.microsoft.com/azure/api-management/credentials-overview)
and [APIM Internal networking](https://learn.microsoft.com/azure/api-management/api-management-using-with-internal-vnet).
Documented platform capabilities do not replace acceptance of this Studio
connection, token facade, Router cutover or proposed client host.

## Retained Function runtime and identity

The following reference describes the retained Function path, not the target
diagram. Existing resources and their publication dependencies remain unchanged.

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

Copilot Studio and Entra remain public SaaS destinations reached over TLS. The sample does not claim that the complete end-to-end route is private. The external requester calls the authenticated HTTPS gateway without a site-to-site tunnel.

## Retained Function network topology

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

## Retained deployment and operator paths

An operator uses Entra-authenticated public HTTPS to the gateway's restricted publication routes. The gateway runs the publication worker inside its integrated VNet and uses private Foundry, registry, and Blob endpoints. Metadata, manifests, extensions, and images are pinned and bound to operator configuration.

Azure Resource Manager is a separate public HTTPS control plane. The reference image builder can schedule a persistent ACR task through ARM. The task's managed identity and explicit trusted-service bypass permit a private registry build without opening anonymous registry access. That bypass is not general public network access and is disabled in the sample foundation by default.

Direct operator access to private endpoints can optionally use an Entra-authenticated point-to-site Azure VPN plus correctly scoped private DNS. This is distinct from the cloud runtime route. The original cloud route was accepted without the client VPN, while direct VPN/DNS acceptance was deferred.

The published source includes only synthetic scenarios and reusable code. Resource names in the drawing are logical labels, not existing deployment identifiers.
