# Infrastructure inventory and deployment boundaries

Read [the guided setup](../../docs/foundry-apim-studio.md) and
[operations](../../docs/operations.md) first. These are composable references,
**not a one-command installer** for the current Motors environment.
No template invocation is part of cloning, building or offline testing.

## Current Motors building blocks

| Files | Scope and prerequisites |
|---|---|
| `apim-business.bicep`, `apim-business-policy.bicep`, `apim/a2a-policy.xml` | Local requester → existing APIM → Router ingress; exact requester and Foundry bindings |
| `apim-studio-a2a.bicep`, `apim/studio-a2a-policy.xml` | Router-only supplier route; existing APIM/logger, Router role and exact IDs, published Studio endpoint and existing managed connection |
| `apim-logistics-a2a.bicep`, `apim/logistics-a2a-policy.xml` | Router-only logistics route; existing APIM/logger and selected Foundry logistics endpoint/permissions |
| `apicenter-motors.bicep`, `apicenter/routing.schema.json` | Routing metadata and two versioned synthetic source records in an existing API Center/default workspace |
| `apim-studio-connection.bicep` | APIM OAuth PKCE provider/connection and managed-identity access policy; Entra registration, secret, callback and human consent are external prerequisites |
| `apim-studio-vault.bicep`, `apim-studio-secret.bicep` | Separately authorized credential storage/setup references; inspect effects and use protected parameter inputs |
| `registry-connection.bicep` | Private registry connection reference; not the ACR image build/publication/selection workflow |

The specialist policies use the fixed provider `contoso-studio-v2-pkce`.
For Studio, set `studioConnectionName` to the actual existing connection ID.
When creating a connection, `connectionName` defaults to `shared-owner`;
maintenance must pass the existing ID explicitly. Keep the dependency bindings
consistent. Do not deploy the historical token policy to change that ID.

## Partial foundation, not a complete fresh environment

`main.bicep` composes `network.bicep`, `storage.bicep`, `monitoring.bicep` and
`foundry.bicep` for a dedicated new resource group. Its Function module is
conditional: **`deployLegacyFunction=false` by default**.
It still contains historical subnet/storage parameters and shared foundation
resources. Disabling the Function does not remove all historical resource shapes
or provide APIM, API Center, Entra registration/consent, the current Router image,
Studio publication, grants and Teams setup.

`main.bicepparam.example` is a foundation example, not a current environment's
deployment file. `apim-staging.bicep` is an earlier isolated APIM staging
reference; do not assume it recreates the current Developer External-VNet
configuration. `vpn.bicep` requires a separately reviewed network decision.
Templates here were not freshly provisioned together during source consolidation.
Use the recorded [architecture](../../docs/connectivity.md) as the current
topology, and review each module's scope before any authorized deployment.

## Historical tooling — do not deploy as the current demo

* `gateway.bicep`, the optional `main.bicep` Function module,
  `contoso/gateway*`, and `contoso/publication/` preserve the retired Function
  and its worker/contracts. The configured demo's Function and exclusive plan
  were removed; shared services were retained.
* `apim-token.bicep`, `apim/token-policy.xml` and `apim-operator-*` /
  `apim-studio-proof*` files are earlier broker, diagnostic and transition
  tooling. They are not the current Router → APIM → Studio business route.
* `contoso/azure.yaml` is the historical multi-service worker manifest, including
  obsolete services and a version-4 selector. **Do not run an all-service
  `azd up` or `azd deploy` with it to reproduce Motors.** Current private-project
  image build, guarded publication and explicit selection are separate SDK
  operations documented in the operator guide.

These files remain in place because imports, packaging and regression tests
still depend on them. Their presence does not authorize recreating retired
resources. No optional Container Apps hosting or CI is installed by this sample.
