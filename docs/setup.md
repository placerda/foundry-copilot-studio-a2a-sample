# Setup guide

For normal use, follow [Start here](../README.md#start-here).
Prepare a new workstation using the local setup below.
This guide's cloud deployment steps describe
the retained Function reference, not a completed APIM cutover. APIM networking,
protected OAuth broker/consent, and OBO/SDK feasibility still require validation.
Do not delete the Function or its private operator duties on the strength of
the local client or disabled identity choices.

This sample preserves a working reference implementation. You must supply your own resource names, identities, permissions, consent, and publication bindings. It does not include existing cloud resources or the private deployment's operating history.

## Local application setup

These steps prepare the Windows chat client. They do not deploy Azure resources.
The gateway, agents, Entra applications and approved guest access must already
be configured by an authorized operator.

You need PowerShell 5.1 or later, Python 3.12 or later, and Node.js/npm.
Open PowerShell in the repository root and install the local dependencies:

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

Fill in the approved **public** settings in both files:

| File | What to configure |
|---|---|
| `frontend\config.js` | Tenant, SPA client and API scope. Keep `API_URL: "/api/chat"`, `REDIRECT_URI: "http://localhost:8000/"` and `CONTOSO_ENABLED: true` |
| `local-demo.config.json` | Backend tenant, API audience, approved user's object ID and gateway/requester bindings from the example |

The frontend tenant and API scope must match the backend tenant and audience.
`ALLOWED_USER_OID` is the approved user's object ID **in the application tenant**,
not their home-directory ID. Do not put passwords, client secrets or tokens in
either file, or add credential-source overrides.

The requester credential must already be provisioned in the current Windows
user's protected store under `ContosoAgentDemo:requester:<client-id>`, matching
the requester application. The launcher reads that exact credential without
printing or copying it. If missing, an authorized operator must complete the
credential setup. Do not export cloud secrets into this repository.

Build the frontend and check the prerequisites:

```powershell
npm run build
.\start-local.ps1 -CheckOnly
```

After the check succeeds, run `.\start-local.ps1` and open
**http://localhost:8000/**. Keep the terminal open while using the chat.
The launcher prefers `.venv-local`, then `.venv`. It does not install
dependencies, make cloud changes or replace an existing port 8000 listener.

## Local troubleshooting

| Symptom | Action |
|---|---|
| Browser cannot connect to localhost | Start `.\start-local.ps1` and wait for its startup message |
| Port 8000 is already in use | Use the existing application. Do not stop an unrelated process |
| Frontend build is missing | Complete `frontend\config.js`, then run `npm run build` |
| Public configuration is invalid | Check the example's allowed fields and tenant/audience bindings |
| Protected credential is missing or mismatched | Have the authorized operator provision the correct Windows requester credential. There is no fallback identity |
| Signed in, but chat says the user has no access | The signed-in user's tenant object ID must match `ALLOWED_USER_OID`, and their access token must include `access_as_user`. Verify approved bindings without broadening access |
| Microsoft sign-in fails | Use the approved account and check the exact localhost redirect and delegated API scope |
| A downstream request fails | Use its request ID and sanitized status for diagnosis. Do not log tokens or prompts |

The local server validates the token's signature, issuer, tenant, audience,
expiry, delegated scope and approved user before calling the gateway.
It listens only on loopback. Do not bind this Windows launcher to a LAN
interface or treat it as a Container Apps deployment.

## Cloud prerequisites (operators)

Use Python 3.12, Node.js, Azure CLI, Azure Developer CLI, Docker-compatible image tooling, and access to an Azure subscription and Entra tenant. The Hosted manifest records the required `azd` and agent-extension versions in [`contoso/azure.yaml`](../contoso/azure.yaml).

Confirm that Microsoft Foundry Hosted agents and Azure Functions Flex Consumption are available in your selected Region. The checked infrastructure reference is restricted to `eastus2`. Check preview API support, quotas, and service cost before deployment.

You also need a published Copilot Studio agent with its native A2A endpoint enabled, a Power Platform environment, permission to configure Entra applications and consent, and permission to provision resources and assign service roles.

## 1. Establish identity boundaries

Configure an Entra API application for the gateway. Configure exact tenant, audience, client, principal, and application-role allowlists. The gateway combines App Service authentication with handler-level authorization.

Use a separate confidential requester identity for an external backend. Grant only the gateway invocation role it needs. The gateway's managed identity calls private Foundry. The Hosted agent's actual managed identity is separately authorized to use the connection-token broker. Discover the real agent identity after publication rather than guessing it from the project identity.

Configure the broker's OAuth application and exact gateway callback URL. Complete delegated consent for the approved guest using `MCS.InvokeAsA2A`. The broker stores protected token state in private Blob Storage. Keep its encryption key and confidential-client credential in protected service settings. The callback has its own state and identity checks.

Do not place passwords, private keys, access tokens, refresh tokens, or client secrets in source files, CLI arguments, diagrams, or deployment outputs.

## 2. Provision the private Azure foundation

Review [`main.bicep`](../contoso/infra/main.bicep) and its network, storage, monitoring, Foundry, and gateway modules. Copy [`main.bicepparam.example`](../contoso/infra/main.bicepparam.example) to a local `main.bicepparam`, replace the synthetic IDs and suffix, and target a dedicated resource group.

Compile and review before applying:

```powershell
az bicep build --file contoso\infra\main.bicep
az deployment group what-if --resource-group <resource-group> --parameters contoso\infra\main.bicepparam
```

Resource creation is a separate intentional step:

```powershell
az deployment group create --resource-group <resource-group> --parameters contoso\infra\main.bicepparam
```

The foundation intentionally creates the gateway stopped. Authentication, authorization, package deployment, and explicit runtime bindings must be verified before enabling it.

The template does not publish an agent, upload sources, configure complete OAuth/runtime bindings, or provision a model by default. Private Link is used for Foundry, two Blob Storage accounts, ACR, and Azure Monitor. Queue and Table endpoints are opt-in and not needed for the demonstrated HTTP-only route.

The optional [`vpn.bicep`](../contoso/infra/vpn.bicep) is for direct operator access only. It is not included in the resource-group foundation, and no VPN is required for external-client runtime requests. Do not install DNS helpers or change workstation routes to run the cloud path.

## 3. Build and publish the Hosted runtime

Build the reviewed [`contoso/Dockerfile`](../contoso/Dockerfile) and pin the resulting image digest. The reference provides [`create_image.py`](../contoso/ops/create_image.py) for a persistent managed-identity ACR task. A private registry task needs its own narrowly scoped push authorization and an explicitly reviewed trusted-service network bypass. The foundation leaves that bypass disabled by default.

Create the managed-identity registry connection using [`registry-connection.bicep`](../contoso/infra/registry-connection.bicep). Registry connection discovery and permission to pull an image are separate concerns. Assign `AcrPull` to the actual Hosted agent identity.

The retained publication worker is deliberately restricted to the reviewed multi-service manifest. Do not remove services or change its hash without updating and reviewing the publication contract. Other retained agents can stay in probe-only mode. The accepted business target is `contoso-foundry-agent` with:

```text
CONTOSO_RUNTIME_ROLE=router
CONTOSO_ROUTER_SCOPE=studio-harness
```

Supply all validated tenant, gateway, Studio endpoint, source, identity, and image bindings. [`contoso/publication/README.md`](../contoso/publication/README.md) describes the worker's API, approved authorization gate, pinned CLI/extension requirements, and mandatory `OperatorConfig`.

Use [`stage_gateway.py`](../contoso/ops/stage_gateway.py) and the private publication worker inside the VNet to stage the reviewed runtime and synthetic sources. The operator can reach the authenticated public gateway, while its managed identity performs the private data-plane operations. Build, publication, consent, RBAC, and activation are separate operations, not implicit effects of deploying Bicep.

[`configure_gateway.py`](../contoso/ops/configure_gateway.py) is an advanced post-publication reference that expects records and publication bindings to already exist. It is not a bootstrap script. Review its exact grants, settings, and default dry run before any apply.

## 4. Add an external client, if needed

A client with approved gateway credentials can call the authenticated bridge directly. The reference [`contoso_backend/a2a_requester.py`](../contoso_backend/a2a_requester.py) contains the A2A requester and response parsing.

Use the real loopback Python HTTP backend in
[`contoso_backend/local_server.py`](../contoso_backend/local_server.py).
It verifies an Entra RS256 bearer token, exact tenant, API audience,
delegated scope, and allowed guest OID before calling the configured gateway.
It is not an anonymous chat API or a locally hosted reasoning agent.
The cloud gateway uses `CONTOSO_REQUESTER_CLIENT_ID` and
`CONTOSO_REQUESTER_OID` for the confidential requester's application and
principal IDs. Supply explicit bindings for a fresh deployment. This sample
does not update existing identities or deployed configuration.

For the browser, copy [`frontend/config.example.js`](../frontend/config.example.js) to the ignored `frontend/config.js` and fill in your own non-secret SPA settings. Set `CONTOSO_ENABLED` to `true` to show the A2A integration route. Register the exact redirect URI and delegated chat API scope. MSAL tokens stay in memory. The backend exchanges its own requester credential for a gateway token rather than forwarding the browser token across trust boundaries.

Follow [Local application setup](#local-application-setup) to prepare and start
the Windows client.

## 5. Validate the intended behavior

Run the [development checks](#development-checks) first. Unit tests use synthetic identities and do not replace real service acceptance.

For **Supplier–Buyer Agent Collaboration**, use all four registered scenarios in [`contoso-demo/scenarios.json`](../contoso-demo/scenarios.json). Evaluate the proposed 300-unit Friday / 200-unit Monday split against the buyer's 500-unit Friday request; ask for the acceptance conditions and evidence limits; ask for the unsupported exact penalty; and request approval, which must return no-match with no provider citation.

The active source is [`supplier-delivery-terms.md`](../contoso-demo/knowledge/supplier-delivery-terms.md), `CONTOSO-DEMO-SUPPLIER-001`, version 1. Configure the same source and prompts for both Studio variants. Verify source/version attribution, written-acceptance conditions, and refusal to invent penalties or claim approval. Browser suggestions must match configured questions exactly. The retained reusable-container knowledge document is a deferred B2B adapter reference, not an active scenario.

Only a genuinely signed-in approved guest calling actual deployed services can establish live acceptance. Local tests use synthetic responses and prove contracts, not service outcomes. Nothing in this guide records a purchase-order approval or authorizes VPN, DNS, or cloud changes.

If port 8000 is occupied during local browser tests, select an unused port without stopping another service: `$env:CONTOSO_TEST_PORT = "8019"; npm run test:ui`. The synthetic test server and browser runner use the same port.

Upstream Studio calls have bounded timeouts. A historical read-timeout was observed in the original deployment, and this sample does not claim to eliminate it. Inspect safe request IDs and status codes rather than logging tokens or prompts.

## Development checks

These commands run tests and build the frontend. They do **not** start the
application. For normal use, run `.\start-local.ps1`.

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

To regenerate SVG, PNG and editable Excalidraw diagrams:

```powershell
node tools\render-diagrams.mjs --png
```

PNG rendering requires an installed Playwright Chromium browser.
See [product icon usage](icons/README.md).
