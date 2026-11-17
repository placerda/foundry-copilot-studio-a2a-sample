# Contoso Motors: Supplier–Buyer Agent Collaboration

A demonstration of a **Microsoft Foundry Hosted Router** coordinating a
**Copilot Studio supplier agent** and a **Foundry logistics Prompt Agent**.
Users ask questions through a local web application or Microsoft Teams.
Answers explain fictional delivery and returnable-packaging conditions, with
versioned source references. The agents do not approve purchases, verify stock
or book transport.

## Start here

**Want to build this demo in your own environment? Start with
[Replicate the Contoso Motors demo](docs/replicate-demo.md).**
It walks through prerequisites, Azure resources, identities, Studio publication,
Foundry agents, APIM routes, API Center, local startup and a first demo question.
Each stage explains what to configure, which values to save and how to check it.
Teams is an optional final step.

### Learn the integration

| Key question | Start reading here |
|---|---|
| How does a Foundry agent call a Copilot Studio agent over A2A? | [One request, from Router to Studio](docs/foundry-apim-studio.md#the-foundry-to-studio-connection) |
| Where do I configure the agents' routes, backends and policies in APIM? | [The three APIM APIs](docs/foundry-apim-studio.md#configure-agent-routes-in-apim), then [deploy the routes](docs/replicate-demo.md#7-configure-the-three-apim-routes) |
| How does APIM authenticate the Router and call Studio as the shared connection owner? | [Credential exchange inside APIM](docs/foundry-apim-studio.md#credential-exchange-inside-apim) |
| How does the Router discover the right agent? | [API Center discovery](docs/foundry-apim-studio.md#discovery-is-separate-from-the-gateway) |
| Where are the agent instructions, reference documents and runnable code? | [Follow a supplier question through the source](docs/foundry-apim-studio.md#1-follow-one-supplier-question) |

APIM exposes and protects the agents' APIs. **It does not host their prompts or
models.** The supplier is configured in Copilot Studio, the Router and logistics
agent in Foundry, and their callable routes and credentials in APIM.

### Run or maintain the demo

| I want to… | Read |
|---|---|
| Build my own demo environment | [Replication guide](docs/replicate-demo.md) |
| Run the already configured local application | [Local setup and startup](docs/setup.md#local-application-setup) |
| Use the existing Teams app or try a demo question | [Demo walkthrough](docs/demo.md) |
| Understand the agents, network and identities | [Architecture and connectivity](docs/connectivity.md) |
| Operate the private deployment or update an agent | [Operator guide](docs/operations.md) |
| Run local development checks | [Development checks](docs/setup.md#development-checks) |
| Consult the reference deployment's acceptance record | [Current status](docs/status.md) |

### Already configured on your Windows computer?

Open PowerShell in the repository root:

```powershell
.\start-local.ps1 -CheckOnly
.\start-local.ps1
```

Keep that window open, visit **http://localhost:8000/**, and sign in with the
approved Microsoft account. Select a suggested question, review the draft and
send it. A suggestion does not send automatically in the current Motors UI.
Stop your server with **Ctrl+C**.

This starts the local application, not Azure resources. `npm run build` builds
the frontend but does not start it. Do not stop another process if port 8000 is
occupied. For a new computer connected to an existing deployment, complete
[local setup](docs/setup.md). For new Azure and Studio resources, follow the
[replication guide](docs/replicate-demo.md).

## Overview

[![Contoso Motors architecture: Teams and local chat, Foundry Router, API Center discovery, APIM specialist routes, Copilot Studio supplier, logistics agent and private connectivity](docs/contoso-motors-architecture.png)](docs/contoso-motors-architecture.svg)

The demo at a glance, with separate message, discovery and network paths.
[Open the vector diagram](docs/contoso-motors-architecture.svg) or
[download the editable source](docs/contoso-motors-architecture.excalidraw).

## How the current demo works

```mermaid
flowchart LR
    Web["Browser + local Python"] -->|Bearer / A2A| APIM["API Management"]
    APIM -->|Managed identity| Router["Foundry Hosted Router"]
    Teams["Microsoft Teams"] -->|Native Activity| Router
    Router -->|Read catalog per request| Catalog["API Center"]
    Router -->|A2A / managed identity| Routes["APIM specialist routes"]
    Routes -->|Shared delegated connection| Supplier["Copilot Studio supplier"]
    Routes -->|APIM managed identity| Logistics["Foundry logistics Prompt Agent"]
```

API Center is the **catalog**, not a gateway. APIM is the **gateway and credential
boundary**. The Router classifies registered demo questions and discovers the
matching specialist through API Center. Teams enters the Router directly through
its native Activity endpoint, not through the local Python server.

The old Azure Function and its exclusive App Service plan have been removed
from the configured demo. APIM, Foundry, API Center, shared storage, monitoring
and networking remain. [Network and identity details](docs/connectivity.md)
explain which paths are public, private or delegated.

## What this example demonstrates

The fictional order is **500 interior trim sets due Friday**, with a supplier
proposal to deliver **300 Friday and 200 Monday**. The supplier explains written
acceptance conditions and evidence limits. Logistics explains receiving and
returning racks and boxes without inventing packaging counts or bookings.

The configured demo uses **scenario 3: a shared delegated Studio connection**.
Browser sign-in identifies the application user, not the identity of that shared
connection. Per-user Studio delegation and Studio service-to-service execution
are not enabled. This is a bounded demo, not an unrestricted assistant.

## Repository map

| Path | Purpose |
|---|---|
| `frontend/` | Browser UI, Microsoft sign-in and UI tests |
| `contoso_backend/` | Loopback Python server, user admission and requester |
| `contoso/router/` | Hosted routing, protocol adapters and tests |
| `contoso/providers/` | Downstream provider clients and response validation |
| `contoso/infra/` | Infrastructure references and APIM policies |
| `contoso/ops/` | Build and operational helpers |
| `contoso/publication/` | Retained **legacy Function publication worker** |
| `contoso-demo/` | Synthetic scenarios and versioned reference material |
| `docs/` | Usage, setup, architecture, operations and status |

The [replication guide](docs/replicate-demo.md) explains how to supply your own
resource IDs, permissions and consent. The retained multi-service
`contoso/azure.yaml` is historical. Use the guide's initial setup sequence, then
the [operator guide](docs/operations.md) for subsequent updates.

## Safety and scope

Use only authorized identities and explicitly selected Azure contexts. Keep
credentials, private keys, tokens, local configuration and raw operational
evidence out of Git. Never broaden access to make a demo pass.

Optional Azure Container Apps hosting is **not deployed**. The current entry
points are the local application and Teams. Production readiness, final local
UI revalidation and the remaining operational checks are tracked in
[known gaps](docs/status.md#known-gaps).

## License

The sample's original code and documentation are available under the
[MIT license](LICENSE). Microsoft product icons, trademarks and third-party
dependencies retain their own terms. See [third-party notices](THIRD_PARTY_NOTICES.md)
and [icon attribution](docs/icons/README.md).
