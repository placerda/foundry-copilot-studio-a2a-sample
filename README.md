# Contoso Motors: Supplier–Buyer Agent Collaboration

A demonstration of a **Microsoft Foundry Hosted Router** coordinating a
**Copilot Studio supplier agent** and a **Foundry logistics Prompt Agent**.
Users ask questions through a local web application or Microsoft Teams.
Answers explain fictional delivery and returnable-packaging conditions, with
versioned source references. The agents do not approve purchases, verify stock
or book transport.

> **Read this first:** the configured demo is ahead of the source published on
> `main`. This documentation describes both and labels the difference.
> A fresh clone does **not** yet reproduce the complete current Motors demo.
> See [current status and source availability](docs/status.md).

## Start here

| I want to… | Read |
|---|---|
| Run the already configured local application | [Local setup and startup](docs/setup.md#local-application-setup) |
| Use the existing Teams app or try a demo question | [Demo walkthrough](docs/demo.md) |
| Understand the agents, network and identities | [Architecture and connectivity](docs/connectivity.md) |
| Operate the private deployment or update an agent | [Operator guide](docs/operations.md) |
| Know what is deployed, tested, missing or legacy | [Current status](docs/status.md) |
| Run local development checks | [Development checks](docs/setup.md#development-checks) |

### Already configured on this Windows computer?

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
occupied. For a new computer, read the [source availability warning](docs/status.md#source-availability)
and complete [setup](docs/setup.md) before running these commands.

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

Some current Motors additions to these directories remain unpublished.
Documentation is not a substitute for those files, deployment bindings,
permissions, consent or cloud resources. The [availability matrix](docs/status.md#source-availability)
lists the important gaps.

## Safety and scope

Use only authorized identities and explicitly selected Azure contexts. Keep
credentials, private keys, tokens, local configuration and raw operational
evidence out of Git. Never broaden access to make a demo pass.

Optional Azure Container Apps hosting is **not deployed**. The current entry
points are the local application and Teams. Production readiness, final local
UI revalidation and the remaining operational checks are tracked in
[known gaps](docs/status.md#known-gaps).
