# Supplier–Buyer Agent Collaboration

A reference implementation that connects a private **Microsoft Foundry Hosted agent** to a published **Copilot Studio agent** through authenticated Agent-to-Agent (A2A) communication.

The Foundry agent routes registered questions to Copilot Studio, preserves source attribution, and returns an explicit no-match when the requested capability is not available. A fictional B2B supplier proposes 300 units Friday and 200 Monday against a buyer's request for 500 units Friday. Agents explain the buyer's conditions; they cannot approve a revised order or verify inventory.

The four [configured scenarios](contoso-demo/scenarios.json) cover proposal assessment, acceptance conditions and evidence limits, an unsupported penalty amount, and an approval request that must return no-match. The active source is [supplier delivery terms](contoso-demo/knowledge/supplier-delivery-terms.md), `CONTOSO-DEMO-SUPPLIER-001`, version 1. All business data is synthetic; local test responses are not live acceptance evidence.

## Start here

**To open the application, start the Python server with `start-local.ps1`, then open `http://localhost:8000/` in your browser.** `npm run build` only builds the frontend. The test commands do not start the application.

The local application serves both the browser UI and `/api/chat` on port 8000. It connects to an already configured Azure gateway and agents. It does not create cloud resources or run an AI model locally.

**Default: run the application on your own computer.** Your browser and the Python server run on that same machine. **Optional: host this entire chat application, UI and Python backend together, in Azure App Service** so the client also runs in the cloud. App Service is not required for local use and is not an extra backend. Foundry and Copilot Studio remain cloud services in either option.

### Minimal local runtime (Windows)

For a prepared workstation:

1. Open PowerShell in this repository's root folder, where `README.md` and `start-local.ps1` are located.
2. Start the application:

   ```powershell
   .\start-local.ps1
   ```

3. Keep that PowerShell window open. When the launcher prints `Open http://localhost:8000/`, open **[http://localhost:8000/](http://localhost:8000/)** in Edge or Chrome.
4. Select **Sign in with Microsoft** and use the approved guest account. Choose a delivery-proposal question and send it in the chat. Only the configured shared delegated connection is enabled.
5. To stop the application, return to its PowerShell window and press **Ctrl+C**.

If port 8000 is already occupied, the launcher refuses to start another server and preserves the existing process. Open the URL to use the application already running there. Do not stop an unrelated process to free the port.

The launcher prefers `.venv-local`, then `.venv`. It validates the frontend build, public configuration and protected Windows credential before starting `python -m contoso_backend.local_server`. It does not install dependencies, deploy resources or change networking.

### First-time local preparation

You need Windows, PowerShell 5.1 or later, Python 3.12 or later, and Node.js/npm. The Azure gateway, agents, Entra applications, invocation permissions and approved guest access must already be prepared through the [setup guide](docs/setup.md). This sample is **not a one-command cloud deployment**.

From the repository root, create the minimal Python environment and install the frontend dependencies:

```powershell
python -m venv .venv-local
.\.venv-local\Scripts\python.exe -m pip install -r contoso_backend\requirements-local.txt
npm ci
```

Create the ignored configuration files only if they do not already exist:

```powershell
if (-not (Test-Path frontend\config.js)) {
    Copy-Item frontend\config.example.js frontend\config.js
}
if (-not (Test-Path local-demo.config.json)) {
    Copy-Item local-demo.config.example.json local-demo.config.json
}
```

Edit both files with the approved **public** tenant, application, user and gateway bindings. Keep the frontend tenant, SPA client and API scope aligned with the backend configuration. Do not put passwords, client secrets, tokens or credential-source overrides in either file.

The backend's requester credential must already be provisioned in the current Windows user's protected store under `ContosoAgentDemo:requester:<client-id>`, matching the configured requester application. The launcher reads that exact credential without printing or copying it. If it is missing, complete the authorized credential setup before continuing. Do not export a cloud secret into the repository.

Build the frontend and check the local prerequisites:

```powershell
npm run build
.\start-local.ps1 -CheckOnly
```

`-CheckOnly` does not start a server or make cloud calls. After it reports `Local configuration and protected Windows credential are ready.`, run `.\start-local.ps1` and open **http://localhost:8000/** as described above.

### If the application does not open

| Symptom | Action |
|---|---|
| Browser cannot connect to localhost | Start `.\start-local.ps1` and keep its terminal open. Wait for the startup message before opening the URL. |
| Port 8000 is already in use | Open the existing application at `http://localhost:8000/`. The launcher deliberately does not replace its process. |
| Frontend build is missing | Configure `frontend\config.js`, then run `npm run build`. |
| Public configuration is missing or invalid | Complete `local-demo.config.json` using the example's allowed fields. Do not add credentials. |
| Protected requester credential is missing or mismatched | Have the authorized setup provision the correct Windows credential for this requester. There is no fallback identity. |
| Signed in, but chat says the user has no access | Sign-in alone does not grant chat access. The local backend requires the signed-in user's object ID in this tenant to match `ALLOWED_USER_OID`, and the access token must include `access_as_user`. Verify the approved configuration, not just the login status. |
| Other Microsoft sign-in or chat authorization failure | Use the approved account and verify the configured Entra applications, API scope and invocation access. Do not enable another identity mode as a workaround. |

## Architecture

![High-level Foundry and Copilot Studio A2A architecture](docs/architecture-overview.svg)

Read the numbered stages from left to right: **1** the local chat application on your own computer (browser and Python server), **2** authenticated API Management (APIM), **3** private Foundry Hosted agent, **4** published Copilot Studio agent. App Service is only an optional cloud host for the same stage 1 application.

**Local request path:** browser → Python server at `http://localhost:8000/` → configured APIM gateway → private Foundry Hosted agent → Copilot Studio A2A endpoint. Responses return through the same chain. The proposed gateway target is APIM Developer in Internal VNet mode with Credential Manager, not a claim that this cutover is complete.

**APIM, not APIC:** API Management is the traffic gateway and hosts Credential Manager for the shared delegated OAuth/PKCE connection. Azure API Center (APIC) is a separate catalog, not a request proxy or token broker. Catalog discovery is not an implemented hop in this sample.

**Optional all-cloud application hosting:** move the same chat UI and Python backend from your computer to App Service. Users then open its HTTPS URL instead of localhost. This does not add another agent or duplicate the chat backend. Local execution uses [`start-local.ps1`](start-local.ps1) and needs no App Service. Cloud hosting still requires adapting the loopback Host/Origin checks and Windows credential access, configuring HTTPS/Entra redirects and private networking, and completing acceptance. The launcher is not an App Service deployment script.

**No Functions in the target application path:** APIM provides the API gateway and Credential Manager replaces the custom OAuth broker and its dedicated host/token-state storage. Build and publication use a separate authorized private workstation or runner, not APIM. Credential Manager requires controlled HTTPS egress to its managed service (`AzureConnectors`), Entra and the configured OAuth endpoints. The diagrams show the proposed topology, not a completed migration. Existing Functions and storage remain unchanged until replacement acceptance and separate retirement approval.

<details>
<summary>Current implementation and migration evidence (advanced)</summary>

### Current implementation and migration status

The APIM foundation and protected business policy have been deployed in the reference environment, with external ingress rather than the proposed Internal mode. Six live negative authentication/routing checks passed. Complete Credential Manager/Router cutover, Internal-mode networking and App Service hosting remain pending. A local client needs corporate connectivity and private DNS to reach an Internal APIM gateway, even though the retained external-ingress path did not require a client VPN.

The live JSON request representation fix now accepts valid framing while
rejecting invalid envelopes. Authorized business calls initially returned
503; Router 4 publication below resolved the admission blocker and an actual
APIM-to-Studio business call now succeeds. Earlier APIM management connectivity
failures recovered without network changes. The fixed SDK Responses operator
diagnostic, generic product-error cleanup and explicit Hosted `/invocations`
passthrough binding are now deployed. The SDK probe returns 200 with verified
APIM workload identity, not a Studio conversation or S2S proof. A separate
fixed operator-only Router framing probe receives upstream 200 with matching
JSON-RPC ID and error `-32602`, not a result. Anonymous, endpoint-injection and
requester callers are rejected 401/400/403. Its fixed error classifier identifies
the Router handler's admission/configuration branch without returning the
upstream message or data. Protected read-only metadata verifies that published
Router version 4 has two configured callers and admits the APIM workload while
retaining its old caller. Foundry RBAC does not replace this application
admission check.
A bounded allowlisted error-count telemetry query returned 403.
Policy-only update templates preserve existing API defaults;
the structural diagnostic returns only bounded allowlisted metadata, never
upstream content or tokens. No Function retirement has occurred. The actual
loopback backend now serves static frontend/capabilities with 200 and rejects
anonymous chat with 401 and an invalid Origin with 403. Authenticated browser
acceptance still requires the admitted guest's normal Microsoft login.

**Current local client:** browser → loopback Python `/api/chat` → the configured authenticated gateway. The APIM binding forwards the native A2A request to Foundry. This client remains local and does not establish that the App Service alternative or replacement token path has been accepted.

The browser and real Python HTTP backend share `http://localhost:8000/`. The backend is a requester, not a locally hosted reasoning agent. The chat keeps the conversation prominent. Expand **Delivery proposal** for the synthetic context. **How the agents collaborate** distinguishes loopback HTTP/JSON from A2A 1.0/HTTPS cloud hops. The identity selector shows three choices, with shared delegated selected initially. The backend's `/api/capabilities` keeps automatic initiating-user delegation and SDK S2S disabled with explicit reasons. These adapters and their integrity-protected caller contract are not proven/deployed. Unknown/unavailable selections are rejected without fallback; mode changes clear the conversation. Verified application tenant/object identity is displayed separately from the fixed Studio connection owner, whose live identity is not falsely declared verified.

The frontend always uses the native A2A / Copilot Harness route. Configure the connection and set `CONTOSO_ENABLED: true` before using the chat. A disabled connection or invalid response never falls back to a model. Reset and sign-out preserve the fixed route. Existing Function resources remain available for rollback and private publication until replacement acceptance and separate deletion confirmation. Session actions appear after sign-in. Authentication and conversation data remain in page memory.

The existing authorized private publisher supports an absolute owned workspace
(`CONTOSO_PUBLICATION_WORKSPACE_ROOT`, defaulting to `HOME/data/contoso-publication`)
and trusted `CONTOSO_PUBLICATION_APIM_CALLER_ID` configuration. Neither is an
HTTP request field. A project-bound nonsecret package override can supply these
settings; conflicting settings are rejected. Adding APIM preserves every
existing Router caller. The verified original Flex package received this
minimal overlay, and private publication produced Router version 4. A real
authenticated APIM business call returned a strictly validated `answered`
result from `studio-copilot-harness` with one source. Browser/local acceptance,
Credential Manager migration, S2S and initiating-user delegation remain
separate outstanding checks. No VPN or local networking changes were required.

The same pinned configuration and durable Blob job store are shared with
`python -m contoso.publication.worker`, independently of the Functions HTTP host.
Its fixed commands are `preflight`, `sources`, `records`, `metadata`,
`diagnostics --process-user-agent <printable-value>`, and
`publish-service --service <approved-service> --process-user-agent <printable-value>`.
Only the existing three service names are accepted. Invalid arguments are
rejected before constructing an executor, and failed operations exit nonzero.
The worker requires an explicitly authorized private Linux execution host with
the existing system-managed-identity endpoint, pinned publication package,
trusted environment bindings and private DNS connectivity. It does not fall back
to the operator's Azure CLI credentials or accept targets, commands or package
paths from CLI arguments. Blob job metadata uses the executor's same explicit
system identity. Local checks do not establish an available replacement host,
successful private publication or permission to retire the Function.

`contoso/infra/apim-studio-proof.bicep` defines a separate connection
diagnostic. It adds only an HTTPS API, its policy and one fixed
zero-body POST operation at `/api/ops/studio-connection/`. Existing operator
client, principal and `CONTOSO.Deploy` admission must pass before APIM accesses
the fixed PKCE connection through its own managed identity. The policy validates
the connection token's signature, tenant, shared owner, OAuth client, delegated
scope and expiry, allowing only the distinct Studio A2A resource URI or its
registered application ID. It cross-checks v1/v2 token issuers and client claims,
and returns bounded proof metadata, never an access or refresh token.
The generic Power Platform resource is not interchangeable with that A2A
resource. This diagnostic does not call Studio, prove refresh or change the
token facade, Router, connection, permissions or networking. Static boundary
tests and Bicep compilation do not validate APIM expressions at runtime.
Deployment needs explicit approval, provider validation and a preview limited
to the three isolated children. Actual negative admission checks and positive
signature-verified proof remain required before any broker cutover.

`contoso/infra/apim-token.bicep` and `contoso/infra/apim/token-policy.xml` are
the isolated Router-only token facade deployed in the authorized demo.
Deployment and full policy readback passed on 2026-11-10. A policy-only
correction added caller/downstream JWT IDs so the error handler preserves
authentication rejection instead of masking it as a connection failure.
Live negative checks returned five safe, non-cacheable 401 responses and a
404 for unsupported GET, without returning credentials. Downstream 502,
throttling 429 and successful Router token release are not proven by those
negative checks. Existing Router grants and APIM connection access were
verified, but directory configuration is not positive runtime proof.
The authorized
demo diagnostic returned HTTP 200 on 2026-11-10 after signature, tenant, owner,
client, scope and expiry verification. Its actual v2 token audience was
`6f46e6ed-07bf-4616-80f0-47546850f654`, not the generic Power Platform resource.
The facade pins that exact audience and projects the verified JWT audience.
Harness catalog and connection metadata use the same resource ID, while the
separate Standard SDK audience is unchanged. Expiry conversion uses the same
documented JWT property as the executed diagnostic, without `ToUniversalTime`.
The working broker and Router remain unchanged. A business invocation through
the replacement token facade and Router cutover remain unproven, and no
additional identity mode is enabled. Earlier successful business receipts
belong to the retained path, not this replacement. Usable connection-token
continuity after the original token expired was observed without another
owner login, but does not independently identify the OAuth refresh grant.
`contoso/infra/apim-studio-connection.bicep` prepares the documented generic
OAuth2-with-PKCE provider, the fixed pending authorization and connection access
for the existing APIM managed identity only. The Router remains separate.
Supply its required secure client-secret parameter through an
ARM Key Vault reference, not a literal file or command-line argument.
If no approved vault exists, `contoso/infra/apim-studio-vault.bicep` prepares
a dedicated Standard RBAC vault with soft delete, purge protection and ARM
template access. Its Secrets Officer grant is scoped only to that vault and
the explicitly selected operator. Creating the vault and granting access
require approval, validation and what-if before deployment. This foundation
does not create an OAuth credential or authorized connection.
`contoso/infra/apim-studio-secret.bicep` separately prepares storage of an
authorized credential in an existing vault through ARM. Supply the required
secure value only in memory through the ARM deployment API, never a literal
parameter file or CLI argument, and match the required Unix expiration to
the Entra credential. Only the secret resource ID is output. This template
does not create an Entra credential or change vault networking, roles or
connections. Validate and preview it separately before deployment. Source
tests and compilation do not prove a live secret write or successful retrieval
through a connection's Key Vault parameter reference.
The deployment tenant supplies the v2 endpoints and the exact delegated
Studio scope plus `offline_access`. Register the generated APIM web callback
on the confidential client without removing existing redirects. Before
deployment, verify the existing APIM managed identity and confidential-client
registration, run validation/what-if and ensure no existing connection changes.
After deployment, obtain the authorization's actual `getLoginLinks` response
and verify S256 PKCE before the intended owner signs in. Provisioning does not
perform consent, mark the connection authorized or establish token shape.
Owner, tenant, audience, scope and expiry must be verified before exposing a
token facade. Refresh behavior and actual Studio acceptance through that
facade must pass before switching the Router.
Source preparation now allows an APIM HTTPS origin in `CONTOSO_GATEWAY_URL`
only when `CONTOSO_ROUTER_SCOPE=studio-harness`, in both runtime composition
and publication validation. Use a bare HTTPS origin without an explicit
port, credentials, query or fragment. The full catalog still requires the retained
Function broker, because this facade releases only the fixed Harness
connection. Keep the existing API audience and managed identity. This
compatibility does not change any deployed version or establish successful
Router token release. A separately validated candidate and live acceptance
are required before cutover.
Microsoft's [provider reference](https://github.com/Azure/APIManagement-Authorizations/blob/main/docs/identityproviders.md)
identifies Generic OAuth 2 with PKCE as `"identityProvider": "oauth2pkce"`.
The non-PKCE `"oauth2"` provider is not interchangeable: a real server-issued
authorization redirect from that provider omitted both `code_challenge` and
`code_challenge_method`. The template selects `oauth2pkce` with confidential
credentials and explicit authorization, token and refresh URLs. Do not invent
PKCE flags, append a static challenge or supply a verifier outside the provider.
Source checks do not replace observing S256 in the actual generated redirect.
Live Graph confirms the exact
`https://api.powerplatform.com/copilotstudio-a2a` resource exposes enabled
delegated `MCS.InvokeAsA2A` (User consent type). A fresh server-issued connection
login link, not a constructed consent URL, must ultimately be authorized by
the fixed owner. No missing administrator role was demonstrated.
APIM owns the challenge/verifier and login-link/consent handling. Verify the
generated request's client, tenant, callback and scopes as well as S256 before
consent. A deployed connection with `Unauthenticated` is not an authorized
connection. Actual owner authorization, token compatibility and refresh remain
separate runtime acceptance gates.
Select the PKCE provider at creation. A live in-place change from `oauth2` to
`oauth2pkce` returned a successful ARM deployment but retained `oauth2` on readback.
Do not treat deployment success as proof of that change or proceed to consent.
Recreating an existing candidate requires explicit deletion approval, or a new
provider name and matching token-policy binding must be prepared and validated.
The template and isolated token policy use `contoso-studio-v2-pkce` for an
additive PKCE candidate. This avoids deleting or modifying an existing
`contoso-studio-v2` candidate. The original remains unused and unauthenticated,
and any cleanup requires approval. Creating the parallel connection does not
deploy the token API, change the working Router or establish owner consent.

The reusable sample names its fixed authorization `shared-owner` in the
connection template and both APIM policies. This is a new-deployment reference,
not an instruction to rename an existing connection. Keep the template, policies,
approved owner and consent aligned when adapting the sample.

### SDK S2S credential attempt — November 9, 2026

Live metadata confirms the existing SDK workload is an enabled Foundry
`agentIdentity` / `ServiceIdentity` with the actual application permission
`CopilotStudio.Copilots.Invoke` on Power Platform. A real, bounded
`ManagedIdentityCredential` request for `https://api.powerplatform.com/.default`
using that workload ID failed with `CredentialUnavailableError` in 0.39 seconds.
The current Windows execution host has no configured managed-identity endpoint
or selected workload credential; the role assignment is not a usable credential.
No token was acquired and no Studio conversation was attempted. This is not
proof that SDK S2S is unsupported. Initial private-publisher metadata confirmed
SDK version 1 had the intended agent identity, distinct from the
project's managed identity. This is declared binding, not token proof.
The isolated operator worker in `contoso/providers/sdk_proof.py` requests a
resource-correct credential inside the Hosted workload. SDK3 now executes that
worker and acquired a token, but the first attempt stopped at local
`InvalidSignatureError`, before Studio. This is not a Studio rejection.
Following [Microsoft's token ownership guidance](https://learn.microsoft.com/entra/identity-platform/access-tokens),
the corrected client distinguishes non-authoritative diagnostic claims from
actual resource authorization: only Studio validates its access token.
Wrong resource, project-identity mismatch, missing role or delegated scope
still stop the proof; diagnostic claims alone never prove authorization.
Its separate image command leaves the normal runtime and Router 4 unchanged.
The existing protected APIM SDK operation now accepts only its allowlisted
proof metadata as well as the earlier bootstrap reply; no endpoint was added.
SDK5 returned HTTP 404 during the SDK attempt. Its receipt does not distinguish
discovery from conversation startup.
The App channel supplies an `agenticruntime/3p` connection URL rather than
the legacy engine route. The isolated proof now uses that exact channel URL
and rejects mismatched configured targets. SDK6 reached a bare SDK `ClientError`.
SDK7 preserves HTTP errors through
`ConnectionSettings.client_session_settings={"raise_for_status": True}` and
recorded HTTP 405 during start-conversation. SDK8's bounded live diagnostic
confirmed the final method was POST with no redirects, eliminating the
redirect-to-GET hypothesis for this attempt.
A successful conversation remains unproved. Both SDK calls initially use
POST and expect SSE. The official sample's application-permission claim does
not provide a working app-only configuration for this target. No unsupported
method substitution or permission expansion has been applied. Original build
task and publisher image bindings were restored after publication.
No user/CLI fallback, extra grant, token export, mode enablement, shared-mode
change or network change occurred.

The diagnostic source no longer embeds deployment-specific identities or an
App-channel URL. In addition to its existing trusted runtime configuration,
it requires `CONTOSO_SDK_EXPECTED_PRINCIPAL_ID`,
`CONTOSO_SDK_EXPECTED_CLIENT_ID` and `CONTOSO_STUDIO_SDK_CONNECTION_URL`.
Both operator-probe Bicep templates also require `sdkExpectedPrincipalId`,
bound to the same verified SDK principal, rather than a source-code identity.
Both identity bindings and the channel's configured environment/schema must
match, otherwise the proof fails closed. These are operator settings, not
request fields. The currently deployed SDK8 is unchanged. Using this source
requires separately validated runtime configuration and image deployment,
and does not resolve the observed Studio HTTP 405 or enable SDK S2S.

</details>

The diagrams use unmodified official Microsoft Azure and Power Platform product icons stored locally. Browser-to-backend is loopback HTTP/JSON on your computer. Only the optional App Service host changes that hop to HTTPS. Both backend-to-Foundry (via APIM) and Foundry-to-Copilot Studio use A2A 1.0 / JSON-RPC 2.0 over HTTPS, with protocol-preserving Foundry HTTP passthrough.

## Detailed connectivity

![Complete connectivity architecture](docs/architecture.svg)

The detailed view uses the same four stage numbers. The **Your computer** block contains the local browser and Python server. The remaining blocks separate private APIM/Foundry traffic, public SaaS calls, Credential Manager token exchange and private operator connectivity. Foundry, Key Vault, Container Registry and Azure Monitor have private-endpoint attachments in the target. **Only the optional App Service deployment** adds the fifth private endpoint and a dedicated outbound-integration subnet for the same stage 1 application. It is not another instance required by local use. APIM Internal uses VNet injection rather than another private endpoint. The old Function and its broker storage are not target components.

[Connectivity guide](docs/connectivity.md) · [Setup guide](docs/setup.md) · [Overview PNG](docs/architecture-overview.png) · [Detailed PNG](docs/architecture.png)

Editable sources: [overview](docs/architecture-overview.excalidraw) and [detailed architecture](docs/architecture.excalidraw). Product icons follow [Microsoft's permitted architecture-icon usage](docs/icons/README.md).

### Operator access to private monitoring

The existing `python -m contoso.ops.poc_dns prepare` utility accepts
`--include-monitor` to read the deployed Azure Monitor private DNS zones and
include exact query API names using the actual private API record. Without
this option, preparation retains its original five workload zones. Use a new
`--output-dir` to preserve the previous export. This only exports files. It
does not import or connect a VPN, change operating-system DNS, or start a DNS
listener. Do not interrupt another connected VPN without a specific decision.
An exported profile is not proof of private HTTPS or telemetry access.

## What's included

| Component | Location |
|---|---|
| Hosted runtime, explicit routing, A2A transport, and attribution | [`contoso/`](contoso/) |
| Authenticated gateway, delegated-token broker, and private publication worker | [`contoso/gateway/`](contoso/gateway/), [`contoso/connections/`](contoso/connections/), [`contoso/publication/`](contoso/publication/) |
| Private Azure infrastructure reference | [`contoso/infra/`](contoso/infra/) |
| Local Python HTTP requester and authenticated browser client | [`contoso_backend/local_server.py`](contoso_backend/local_server.py), [`frontend/`](frontend/) |
| Synthetic questions and source documents | [`contoso-demo/`](contoso-demo/) |

## Runtime and migration configuration

Required backend process variables: `TENANT_ID`, `API_AUDIENCE`,
`ALLOWED_USER_OID`, `CONTOSO_ENTRA_TENANT_ID`, `CONTOSO_GATEWAY_API_CLIENT_ID`,
`CONTOSO_REQUESTER_CLIENT_ID`, `CONTOSO_REQUESTER_CLIENT_SECRET`,
`CONTOSO_GATEWAY_URL` (exact HTTPS `/api/a2a` URL).
For APIM also bind `CONTOSO_APIM_HOST` to the exact approved
`<name>.azure-api.net`; other APIM hosts are rejected. Retained
`.azurewebsites.net` Gateway URLs are supported during migration.
Use a legitimately provisioned local confidential-client credential with
invocation-only authorization, never Studio token retrieval. Do not export
cloud secrets, put credentials in the bundle or repository, or print tokens.
On Windows, an explicitly provisioned protected credential can instead be
read with `CONTOSO_REQUESTER_CREDENTIAL_SOURCE=windows`; omit
`CONTOSO_REQUESTER_CLIENT_SECRET`. The runtime reads only the current user's
`ContosoAgentDemo:requester:<client-id>` credential, checks its stored requester
identity and fails closed if missing or mismatched. It never searches other
credential targets or falls back to another authentication scenario.

Open `http://localhost:8000/` and sign in normally. Ctrl+C stops only the new
server; an occupied port fails startup and does not terminate an existing
process. This host enforces exact Host/Origin, fixed routes, bounded JSON and
response bodies, ten-second transport admission, verified JWT/guest admission,
eight HTTP workers and a single 75-second bounded A2A worker. Expired JWTs
return 401; service credential/downstream failures return sanitized errors,
never an inference retry. Both response JSON and headers carry request IDs.
Do not bind it to a LAN interface.

APIM's built-in Entra credential provider is v1-only. The v2 Studio scope
requires proof of a generic OAuth 2.0 PKCE provider with tenant-specific
authorize/token/refresh endpoints, fresh legitimate guest consent and automatic
refresh. The callback is
`https://authorization-manager.consent.azure-apim.net/redirect/apim/<name>`.
The fixed connection token API must admit only the exact Router identity/role;
the local requester must be rejected there. These gates are not passed by
unit tests. APIM staging infrastructure does not publish either API. A private,
authorized operator environment must independently prove publication/source
upload/diagnostics before removing the Function; APIM cannot run those jobs.

Gateway deployment bindings use `CONTOSO_REQUESTER_CLIENT_ID` and
`CONTOSO_REQUESTER_OID`. These are explicit Entra application/principal IDs,
not the local browser user or the shared Studio connection owner. The sample
does not rename existing applications or update a running deployment.

The setup and connectivity guides describe retained migration assets, not
proof that every replacement path has passed live acceptance.

### Development checks (do not start the application)

The commands below install development dependencies, run tests and build the frontend. They do **not** start `http://localhost:8000/`. For normal use on a prepared Windows workstation, run `.\start-local.ps1` as described in [Minimal local runtime (Windows)](#minimal-local-runtime-windows).

Use Python 3.12 and Node.js:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest --import-mode=importlib contoso contoso_backend
npm ci
npm test
npm run test:ui
npm run build
```

## Scope

The demonstrated business route is Foundry → Copilot Studio A2A using delegated `MCS.InvokeAsA2A` authorization. Registered questions are matched explicitly, not through unrestricted autonomous planning. Contoso is a fictional scenario, not a platform dependency.

Additional knowledge and SDK adapters are retained as reference code, not claimed as accepted business routes. Teams, Microsoft 365 channels, Power Automate, and bidirectional A2A are not part of this sample. Preview APIs, regional availability, token consent, and upstream response-time limits require deployment-specific review.

To regenerate both editable diagrams and PNGs, run `node tools\render-diagrams.mjs --png` after installing dependencies and Playwright Chromium. The generator preserves numbered stages **1–4**, official icons, and rejects connector crossings. Diagram regeneration does not change VPN, DNS, or cloud resources.
