# Learn the integration: Foundry → APIM → Copilot Studio

[Home](../README.md#start-here) · [Replicate the demo](replicate-demo.md) · [Architecture](connectivity.md) ·
[Operations](operations.md) · [Status](status.md)

This guide shows how the Contoso Motors sample connects a Microsoft Foundry
agent to a Copilot Studio agent through Azure API Management. It explains the
code, authentication flow, and configuration steps.

**Building your own environment? Follow the [replication guide](replicate-demo.md)
in order.** This page explains the important implementation choices and provides
the detailed Studio configuration used by that guide.

## The Foundry-to-Studio connection

The Hosted Router is Python code running in Foundry. It discovers an APIM URL
in API Center, obtains a token for the Gateway API with its managed identity,
and sends an **A2A 1.0 JSON-RPC `SendMessage`** request. APIM forwards that
request to the published Copilot Studio A2A channel with a different credential.

```text
Foundry Hosted Router
  -> APIM /agents/studio-copilot-harness/versions/1/a2a
  -> Copilot Studio A2A client channel
```

The wire contract below is the shape sent by
[`A2ATransport`](../contoso/providers/a2a.py), not a direct HTTP call to a model:

```http
POST /agents/studio-copilot-harness/versions/1/a2a
Authorization: Bearer <Router-token-for-the-Gateway-API>
Content-Type: application/json
A2A-Version: 1.0
```

```json
{
  "jsonrpc": "2.0",
  "id": "11111111-1111-4111-8111-111111111111",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "22222222-2222-4222-8222-222222222222",
      "role": "ROLE_USER",
      "parts": [{
        "text": "What exact penalty will the buyer charge for delivering the remaining 200 units Monday?"
      }]
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"],
      "returnImmediately": false
    }
  }
}
```

Generate fresh UUIDs for `id` and `messageId` on each request. The IDs above
illustrate the format required by the APIM schema.

The adapter accepts a completed A2A task or a message response, extracts text,
and validates the request ID and result. The Router then validates the answer's
source attribution. It does not merely display any HTTP 200 response.

For the **local application's entrance**, the same A2A envelope goes to APIM
`/api/a2a` using the requester's token. That API forwards it to the Router's
Foundry **invocations** endpoint. The adapter in
[`native_a2a.py`](../contoso/router/native_a2a.py) handles the A2A envelope on
that passthrough route. Do not confuse this entrance with the Router's outgoing
call to Studio.

## Configure agent routes in APIM

The agent definitions live in Foundry and Studio. In APIM you configure three
HTTP APIs, their POST operations, backend addresses, credentials and policies:

| API / public path | Allowed caller | Backend / outbound credential | Template and policy |
|---|---|---|---|
| `contoso-a2a` / `/api/a2a` | Local requester application with `CONTOSO.Invoke` | Foundry Router `/endpoint/protocols/invocations?api-version=v1`, using APIM managed identity | [apim-business.bicep](../contoso/infra/apim-business.bicep), [a2a-policy.xml](../contoso/infra/apim/a2a-policy.xml) |
| `contoso-studio-a2a-v1` / `/agents/studio-copilot-harness/versions/1/a2a` | Router identity with `CONTOSO.Connection.Use` | Fixed Studio A2A channel, using the shared delegated connection | [apim-studio-a2a.bicep](../contoso/infra/apim-studio-a2a.bicep), [studio-a2a-policy.xml](../contoso/infra/apim/studio-a2a-policy.xml) |
| `contoso-logistics-a2a-v1` / `/agents/motors-logistics/versions/1/a2a` | Router identity with `CONTOSO.Connection.Use` | Foundry logistics `/endpoint/protocols/a2a`, using APIM managed identity | [apim-logistics-a2a.bicep](../contoso/infra/apim-logistics-a2a.bicep), [logistics-a2a-policy.xml](../contoso/infra/apim/logistics-a2a-policy.xml) |

In the Azure portal, open **API Management → APIs → the API → Design → All
operations** to inspect the installed policy. The templates already create the
operations and substitute the parameters. There is no separate manual
"register the agent prompt in APIM" step.

| Parameters | Where to obtain the values |
|---|---|
| `apimName` | APIM resource name |
| `tenantId`, `gatewayApiClientId` | Gateway API Entra tenant and application/client ID |
| `requesterClientId`, `requesterPrincipalId` | Requester app client ID and its Enterprise Application object ID |
| `routerClientId`, `routerPrincipalId` | Actual Hosted Router identity's client ID and service-principal object ID |
| `foundryAccountName`, `foundryProjectName`, `routerAgentName` | Your Foundry resource/project and `contoso-foundry-agent` |
| `studioEnvironmentId`, `studioAgentSchema` | Published Studio A2A channel URL |
| `studioOauthClientId` | OAuth client registration used by Credential Manager |
| `studioConnectionName` | Credential Manager connection ID, also used as `connectionName` in the connection template |

The specialist modules expect an existing APIM managed identity and logger named
`applicationinsights`. The Studio route also expects an authorized connection.
Follow [stage 7 of the replication guide](replicate-demo.md#7-configure-the-three-apim-routes)
for the deployment commands and role assignments.

## Credential exchange inside APIM

Read [studio-a2a-policy.xml](../contoso/infra/apim/studio-a2a-policy.xml) in this order:

1. **`router-jwt`** validates the tenant, audience, exact Router client/principal,
   app-only token type and `CONTOSO.Connection.Use` role.
2. **`request-schema`** validates the bounded A2A message and protocol version.
3. **`studio-connection`** calls `get-authorization-context` for provider
   `contoso-studio-v2-pkce` and the configured connection ID.
4. **`connection-jwt`** validates the delegated Studio token's audience, tenant,
   OAuth client, expiration and `MCS.InvokeAsA2A` scope.
5. **`set-header` / `rewrite-uri`** replaces `Authorization` with that delegated
   token and fixes the destination to the published agent on
   `https://api.powerplatform.com`.
6. **Outbound policies** bound the response and remove credential/cookie headers.

This is the key bridge: **Router managed identity authenticates to APIM.
The shared user's delegated connection authenticates APIM to Studio.**
It is not end-user on-behalf-of forwarding and not app-only Studio invocation.
The Studio token stays in APIM.

For logistics, the corresponding policy uses
`authentication-managed-identity` with resource `https://ai.azure.com` instead
of a Studio connection. The A2A message shape stays the same.

## Discovery is separate from the gateway

[`apicenter-motors.bicep`](../contoso/infra/apicenter-motors.bicep) publishes each
capability, APIM endpoint, route version and citable source in API Center.
[`APICenterClient`](../contoso/api_center.py) reads this inventory on each request.
[`APIMRoutePolicy` and `DiscoveredRouterRuntime`](../contoso/router/discovery.py)
validate the metadata, select a matching registered capability and call APIM.

Registering an entry in API Center does **not** create an APIM API, publish a
Studio agent or grant an identity permission. Configure those first. Conversely,
an APIM API that is absent from the valid catalog will not be discovered by this
Router.

To adapt the example, change the relevant layer rather than every component:

| Change | Edit/configure |
|---|---|
| Supplier instructions or knowledge | Studio prompt and approved source |
| Studio destination or shared connection | Supplier APIM module parameters and Credential Manager |
| Callable capability / route metadata | API Center record and the Router's supported capability contract |
| Accepted demo questions | `motors-scenarios.json` and the classifier contract |
| Browser entrance | Local SPA/API/requester configuration |

## 1. Follow one supplier question

Start with `contoso-demo/motors-scenarios.json`, then follow these files in order.
The classifier recognizes the registered demo questions; it is not an
unrestricted model-based planner.

| Step | Source | Responsibility |
|---|---|---|
| Browser entrance | `frontend/contoso.js`, `contoso_backend/local_server.py`, `contoso_backend/auth.py` | Draft/send, same-origin API, JWT validation and the approved-user gate |
| Local requester | `contoso_backend/config.py`, `requester_certificate.py`, `a2a_requester.py`, `contoso_requester.py` | Obtain its own application token and send bounded A2A to APIM `/api/a2a`; do not forward the browser token |
| Ingress APIM | `contoso/infra/apim-business-policy.bicep`, `apim/a2a-policy.xml` | Admit the exact requester identity/role and call the Router's native invocations endpoint using APIM managed identity |
| Compose the Router | `contoso/entrypoint.py`, `configuration.py` | Select `router` + `api-center`, fixed shared supplier variant and registered Motors questions |
| Read the catalog | `contoso/api_center.py`, `router/discovery.py` | Authenticated API Center data-plane read on every admitted request; validate schema, lifecycle, source sections and APIM origin |
| Invoke the supplier | `router/discovery.py`, `providers/a2a.py` | Router managed identity obtains the APIM application token; bounded A2A 1.0 request to the discovered version-addressed route |
| Exchange credentials inside APIM | `contoso/infra/apim-studio-a2a.bicep`, `apim/studio-a2a-policy.xml` | Validate Router identity/role, acquire the shared delegated connection token, validate it, and forward to the fixed Studio endpoint |
| Answer from synthetic evidence | `contoso-demo/prompts/motors-supplier-v1.md`, `knowledge/motors-supplier-delivery-terms-v1.md` | Studio supplier instructions and sole source; response carries exact source/version/section citations |
| Validate and present | `contoso/providers/common.py`, `router/runtime.py`, `frontend/contoso.js` | Validate citations and response contracts, then show answer and references |

These are repository-relative paths; short filenames in a row share that row's
directory. Follow `contoso/router/tests/test_discovery.py` and
`contoso/infra/tests/test_apim_studio_a2a.py` alongside the code for negative cases.
Catalog data cannot grant caller permissions, choose arbitrary origins or
change credential scopes. Discovery fails closed; the static catalog is not an
automatic fallback.

**Teams takes a different entrance into the same Router.** Its native Activity
adapter is `contoso/router/native_activity.py`, composed by `router/native_a2a.py`.
It trusts the Foundry SDK's validated caller context, not Activity display names
or caller-supplied headers. ACK and delivered answer are separate outcomes.
There is no intermediate local server or Azure Function in that path.

## 2. Keep the identities separate

| Hop | Credential / authority | Required configuration |
|---|---|---|
| Browser → local Python | User delegated token | SPA registration, exact localhost redirect, API `access_as_user` scope, matching tenant/audience and approved application-tenant user object ID |
| Python → APIM | Requester application's certificate assertion → Entra Bearer token | Separate requester app; public certificate registered in Entra; matching private key in the current Windows user's Credential Manager; gateway API role and exact client/principal admission |
| APIM → Router | APIM managed identity | Permission to invoke the selected private Foundry Router and its expected platform caller |
| Router → API Center | Actual Router managed identity | Data-plane catalog-read permission on the selected API Center; scope `https://azure-apicenter.net/.default` |
| Router → specialist APIM route | Actual Router managed identity | Gateway API application role `CONTOSO.Connection.Use`; exact client ID and principal ID in the policy; scope `api://<gateway-api-client-id>/.default` |
| APIM → Studio | Delegated token for the approved **shared connection owner** | OAuth authorization-code/PKCE provider, owner access to the published agent, consent and APIM identity access to the connection |
| APIM → logistics | APIM managed identity | Foundry invocation permission for the separately configured logistics agent |
| Teams → Router | Foundry-native Activity authorization plus runtime caller admission | Teams/M365 publication and the separate `CONTOSO_TEAMS_PLATFORM_CALLER_IDS` allowlist |

The browser user is **not** the shared Studio connection owner. Router managed
identity is **not** the OAuth delegated identity. API Center is a catalog, not
a token broker. Studio's delegated token never returns to the Router or browser.
The local certificate is used to sign an Entra assertion: APIM receives a
Bearer token, not mTLS.

## 3. Understand the configuration before filling it in

All `<...>` values are placeholders. For your own deployment, collect the IDs
and endpoints as you complete the [replication guide](replicate-demo.md).
For an existing deployment, obtain them from its operator. App/client IDs,
service-principal IDs and app registration object IDs are different values.

### Local application

Follow [local setup](setup.md#local-application-setup). Only
`frontend/config.js` and `local-demo.config.json` contain local client settings,
and both are ignored. The public certificate path is specific to **your**
computer. No public sample private key is supplied.

### Hosted Router

These are settings on the selected Hosted Router version, **not** extra fields
to add to `local-demo.config.json`:

| Setting | Meaning |
|---|---|
| `CONTOSO_RUNTIME_ROLE` | `router` |
| `CONTOSO_ROUTER_SCOPE` | `api-center` |
| `CONTOSO_APICENTER_ENDPOINT` | Exact API Center **data-plane origin**, matching `https://<service>.data.<region>.azure-apicenter.ms`; not an ARM ID |
| `CONTOSO_APIM_A2A_BASE_URL` | `https://<apim-name>.azure-api.net/agents`, without a trailing slash |
| `CONTOSO_ENTRA_TENANT_ID` | Canonical gateway tenant UUID |
| `CONTOSO_ENTRA_API_CLIENT_ID` | Gateway API application/client UUID |
| `CONTOSO_PLATFORM_CALLER_IDS` | JSON array of explicitly approved Foundry platform caller IDs for the Python/APIM route |
| `CONTOSO_TEAMS_PLATFORM_CALLER_IDS` | Separate JSON array of approved Teams platform callers; omitting it leaves the Activity host unconfigured |

The runtime uses managed identity, never an operator credential fallback.
Use [the initial SDK/ACR sequence](replicate-demo.md#6-build-and-bootstrap-the-hosted-router)
for a new environment and [operations](operations.md) for the full reference
deployment's maintenance. The historical `contoso/azure.yaml` does not
reconstruct these settings.

### Private operator bindings

Copy the two `contoso/motors-*.bindings.example.json` files to an approved
location **outside the checkout**. They are intentionally not usable as-is:

* `motors-admin.bindings.example.json` pins the selected tenant/operator,
  matching project ARM ID and endpoint, separate agent principals, numeric
  selected versions, reviewed definitions/source hashes and model deployment.
* `motors-dependencies.bindings.example.json` identifies the same environment's
  Dataverse origin, Studio bot/schema/file component, published instructions hash
  and timestamp, APIM resource/principal and managed provider/connection.
* A definition hash is the helper's canonical definition digest, not a Git
  commit hash. `router_source_sha256` is the deterministic `source_archive`
  digest from `contoso.ops.create_image`, not a directory hash.
* `studio_source_sha256` hashes the exact supplier Markdown bytes. The
  instructions hash covers the composed instruction string described below.
  Never replace a reviewed pin with whatever live value makes preflight pass.

The fixed agent names and `Contoso Motors Supplier` display name are part of
this sample's guarded helper contracts. They are not personal resource IDs.
Using other names requires a separately reviewed adaptation, not relaxing pins.

## 4. Configure the Studio supplier and shared connection

Use an authorized Copilot Studio environment with the A2A client channel
available, a licensed/authorized connection owner and the required Entra
administrative permissions. If the channel is absent, resolve environment
availability first; do not substitute the legacy Standard SDK route.

1. In Copilot Studio, select the intended environment and create or select the
   **Contoso Motors Supplier** agent using the Microsoft Copilot experience
   supported by the [A2A client channel](https://learn.microsoft.com/microsoft-copilot-studio/agents-experience/publication-channels-mcp-agent-to-agent).
   Do not modify another existing supplier agent accidentally.
2. Attach only `contoso-demo/knowledge/motors-supplier-delivery-terms-v1.md`
   as the approved synthetic source. Set instructions to the contents of
   `contoso-demo/prompts/motors-supplier-v1.md` (trim surrounding whitespace),
   then two newlines, then this exact reference block:

   ```text
   <approved-source-reference>
   <complete unmodified contents of motors-supplier-delivery-terms-v1.md>
   </approved-source-reference>
   ```

   The middle line is an instruction to insert the **file's contents**, not
   literal placeholder text. The exact composition checked by the dependency
   helper is `prompt.strip() + "\n\n<approved-source-reference>\n" + source_text
   + "\n</approved-source-reference>"`. Preserve headings, source ID, version
   and citation syntax. The attached file plus inline reference are deliberate:
   this demo answers from the complete inline source without another retrieval.
3. Do not add general knowledge, web search, Work IQ or business-action tools.
   Publish the reviewed instructions and source. Record the bot ID, schema,
   file-component ID, publication timestamp and approved hashes privately.
   The helper checks this sample's attached-file schema suffix
   `.file.motorssupplierdeliverytermsv1`; verify it against the actual component.
4. Open **Channels → Add a channel → A2A client** on the published agent and
   copy its actual endpoint. This policy supports only the production
   `https://api.powerplatform.com/copilotstudio-a2a/tenants/<tenant>/environments/<environment>/agents/<schema>`
   contract. Match its IDs to the policy parameters. Do not accept an arbitrary
   host from user input or silently rewrite an endpoint from another environment.
5. In the agent's Entra tenant, use a single-tenant OAuth client registration
   distinct from the requester/gateway identities. Add delegated
   `MCS.InvokeAsA2A` for API application
   `6f46e6ed-07bf-4616-80f0-47546850f654` and obtain required admin consent.
   Follow the official channel guide if the API is not yet listed.
6. Configure APIM Credential Manager's `contoso-studio-v2-pkce` provider.
   `apim-studio-connection.bicep` describes authorization-code + PKCE, with
   tenant-specific `/oauth2/v2.0/authorize` and `/oauth2/v2.0/token` endpoints
   and scopes
   `https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A offline_access`.
   Register the exact **Web** callback
   `https://authorization-manager.consent.azure-apim.net/redirect/apim/<apim-name>`
   on the OAuth application. Supply its secret through the reviewed Key Vault
   parameter-reference path, never a literal command or checked-in file.
7. For a new environment choose a non-personal connection ID such as
   `shared-owner`. For an existing environment **reuse its existing ID**.
   The connection template's `connectionName`, the supplier route's required
   `studioConnectionName` and private binding `connection_name` must all match.
   This source change neither renames nor recreates a live connection.
8. Grant the actual APIM managed identity access to that connection. Through
   the managed consent flow, the intended shared owner signs in and consents;
   confirm that owner can access the published Studio agent. A template cannot
   grant consent on the owner's behalf. Do not store login links, codes or
   tokens in Git.
9. For initial setup, verify the published source and authorized connection in
   Studio/APIM, then continue with [the logistics agent](replicate-demo.md#5-create-the-logistics-prompt-agent).
   After the full reference configuration and its bindings exist, the read-only
   `motors_dependencies ... studio-source` and `... connection` checks can verify
   this configuration again. They are maintenance checks, not prerequisites for
   creating the agents. Test the business response through the actual Router
   and APIM route in [the first conversation](replicate-demo.md#9-run-the-local-demo).

## 5. Connect APIM, API Center and Foundry

For initial setup, follow [replication stages 2-8](replicate-demo.md#2-create-the-azure-foundation).
The list below summarizes how the components connect. The
[IaC inventory](../contoso/infra/README.md) describes each module's scope.

1. Establish the approved private Foundry project, model, ACR build path,
   identities, private endpoints/DNS and connectivity. The demo uses APIM
   **Developer, External VNet**. Keep the scoped Teams Activity entry separate
   from general project public access.
2. Configure requester ingress separately from Router specialist access.
   `apim-business.bicep` establishes the bounded API; its separate policy module
   points to the Router. The supplier and logistics modules depend on an
   existing APIM service and its `applicationinsights` logger. They do not
   create Entra applications, grant roles or establish networking.
3. Supply the actual Router client/principal IDs to both specialist policies.
   The supplier policy uses the shared connection from section 4. The
   logistics policy uses APIM managed identity to invoke the separately
   selected `contoso-motors-logistics` Prompt Agent.
4. Review the catalog schema in
   `contoso/infra/apicenter/routing.schema.json` and the two records in
   `apicenter-motors.bicep`. Keep unrelated APIs. Grant the Router catalog-read
   access and configure its exact data-plane origin. An ARM inventory read
   alone does not prove that the Hosted identity can discover these records.
5. Build and create the first Router using
   [replication stage 6](replicate-demo.md#6-build-and-bootstrap-the-hosted-router),
   then configure business routing in stage 8. For subsequent changes to the full
   reference deployment, use [operations](operations.md#update-a-router-image).
   `create_prompt` is a logistics **version-1 bootstrap** helper, not a general
   updater for an agent with later versions.
6. Enable optional Teams entry through the native publication and admission
   process in [operations](operations.md#teams-admission-and-delivery), then
   install/use the approved Teams app. No separate Function is required.

APIM route version `1`, source version `1`, Agent Card version and Foundry
agent versions are different namespaces. The logistics APIM backend invokes
the agent endpoint's selected version; a `/versions/1/a2a` APIM path is **not**
permission to roll the Foundry selection back to version 1.

## 6. Verify in layers

1. **Offline source check:** run Python, JavaScript and browser fixture tests
   plus the frontend build from [setup](setup.md#development-checks).
2. **Operator preflight:** with explicit authorization and reviewed bindings,
   verify private DNS, identities, immutable definitions and selectors.
3. **Dependency checks:** validate attached/inline Studio source, published
   metadata, connection configuration and API Center discovery.
4. **Live channel acceptance:** invoke each agreed registered question once
   through the intended UI/Teams channel; confirm the actual new reply,
   source sections, identity boundary and selected version. Do not confuse
   ACK, a populated draft, a probe or `Connected` metadata with a business reply.
5. **Rollback exercise:** preserve old pins and separately authorize/test the
   reverse selection. Keeping a return version is not rollback evidence.

Keep your deployment's results with its private setup record. The original
demo's acceptance history is recorded in [status](status.md).

## Optional and historical material

Logistics and Teams extend the same Router; the supplier path above is the
guided first read, not a second implementation. Container Apps hosting is an
undeployed option. The static catalog, Standard Studio SDK transport, token
broker, Function worker and historical diagrams remain for tests and migration
history, not as instructions to revive them. See the
[infrastructure inventory](../contoso/infra/README.md) and
[legacy worker note](../contoso/publication/README.md).
