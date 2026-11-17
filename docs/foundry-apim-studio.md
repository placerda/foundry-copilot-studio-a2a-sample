# Guided path: Foundry → APIM → Copilot Studio

[Home](../README.md) · [Local setup](setup.md) · [Architecture](connectivity.md) ·
[Operations](operations.md) · [Status](status.md)

This guide shows how the Contoso Motors sample connects a Microsoft Foundry
agent to a Copilot Studio agent through Azure API Management. It explains the
code, authentication flow, and configuration steps.

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

All `<...>` values in examples are deliberately invalid placeholders, not demo
credentials. Obtain real IDs, endpoints and approved pins from your operator;
do not infer them from display names or copy another user's values.

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
Preserve the deployed private-project SDK/ACR release workflow described in
[operations](operations.md). The historical `contoso/azure.yaml` does not
reconstruct these current settings or the current selector.

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

This section is an operator checklist, not an automated provisioning claim.
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
9. Run authorized read-only `motors_dependencies ... studio-source` and
   `... connection` checks with the reviewed binding files. These verify
   metadata/configuration, not an actual business response. The final gate is
   a bounded supplier question through the real Router identity and APIM route,
   followed by response/citation validation. Keep that live acceptance separate
   from local tests.

## 5. Connect APIM, API Center and Foundry

Review the [IaC inventory](../contoso/infra/README.md) before any deployment.
There is no single complete fresh-environment installer in this revision.

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
5. Publish the Router image and select a version only through the reviewed
   workflow in [operations](operations.md#update-a-router-image). Preserve
   rollback pins. `create_prompt` is a guarded logistics **version-1 bootstrap**
   helper, not a general updater for the existing version 6.
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

The live final local UI and rollback gaps remain in [status](status.md#known-gaps).
No new cloud provisioning, deployment or live acceptance was performed while
consolidating this source.

## Optional and historical material

Logistics and Teams extend the same Router; the supplier path above is the
guided first read, not a second implementation. Container Apps hosting is an
undeployed option. The static catalog, Standard Studio SDK transport, token
broker, Function worker and historical diagrams remain for tests and migration
history, not as instructions to revive them. See the
[infrastructure inventory](../contoso/infra/README.md) and
[legacy worker note](../contoso/publication/README.md).
