# Setup and troubleshooting

[Home](../README.md) · [Demo](demo.md) · [Architecture](connectivity.md) ·
[Operations](operations.md) · [Status](status.md)

## Choose the correct starting point

This guide prepares a Windows client for an **existing, authorized deployment**.
It does not create Azure resources, grant access or publish agents.

| Your starting point | What to do |
|---|---|
| Existing configured Motors workstation | Run the launcher checks below, then use the demo |
| Fresh clone of this revision | Run offline [development checks](#development-checks); current Motors source and certificate support are included |
| New Azure environment | Follow the [guided Foundry → APIM → Studio path](foundry-apim-studio.md) and [operator prerequisites](operations.md#prerequisites), not a one-command deployment |

**Do not treat `contoso/infra/main.bicep` as a complete Motors deployment.**
It now disables the legacy Function by default, but remains a partial
infrastructure reference. It does not configure Studio, consent, API Center
discovery or the complete APIM topology. Read the
[infrastructure inventory](../contoso/infra/README.md) before selecting modules.
No source update migrates an existing environment.

## Local application setup

You need Windows, PowerShell 5.1 or later, Python 3.12 or later, Node.js/npm,
an approved Microsoft identity, and operator-provided non-secret bindings.
Network and DNS access must already be configured for the selected deployment.
Sign-in alone does not grant application or agent access.

On a new development computer, clone the private repository using your approved
GitHub access:

```powershell
git clone https://github.com/placerda/foundry-copilot-studio-a2a-sample.git
Set-Location foundry-copilot-studio-a2a-sample
python -m venv .venv-local
.\.venv-local\Scripts\python.exe -m pip install -r contoso_backend\requirements-local.txt
npm ci
```

These commands install local client dependencies, not Azure resources.
The local requirements pin the cryptography version used by certificate mode.
Do not recreate an existing virtual environment.

Create the ignored configuration files only when missing:

```powershell
if (-not (Test-Path frontend\config.js)) {
    Copy-Item frontend\config.example.js frontend\config.js
}
if (-not (Test-Path local-demo.config.json)) {
    Copy-Item local-demo.config.example.json local-demo.config.json
}
```

Use the examples from **your checked-out revision**. Do not copy a newer config
into the older launcher, which rejects unsupported fields.

| File / setting | Meaning |
|---|---|
| `frontend\config.js` | Public tenant, SPA application ID and delegated API scope |
| Frontend `API_URL` | Keep `/api/chat` for same-origin requests |
| Frontend `REDIRECT_URI` | Register and use exactly `http://localhost:8000/` |
| Frontend `CONTOSO_ENABLED` | Set `true` for the collaboration route |
| Backend `TENANT_ID`, `API_AUDIENCE` | Tenant and audience of the browser-facing API |
| Backend `ALLOWED_USER_OID` | Exact approved user's object ID in the application tenant |
| `CONTOSO_ENTRA_TENANT_ID` | Tenant issuing the requester's APIM token |
| `CONTOSO_GATEWAY_API_CLIENT_ID` | APIM API application's ID, not the Foundry project identity |
| `CONTOSO_REQUESTER_CLIENT_ID` | Separate confidential requester application's ID |
| `CONTOSO_GATEWAY_URL` | Approved APIM URL ending in `/api/a2a` |
| `CONTOSO_APIM_HOST` | Exact approved `<name>.azure-api.net` host |

The frontend tenant and scope must match the backend tenant and API audience.
Do not put passwords, private keys, secrets or tokens in these files.
An approved administrator used for this isolated demo is an exception, not a
production access model.

### Requester credential

The example selects the current **certificate requester**. Replace the example
path with an absolute path on your own Windows computer:

| Local setting | Value |
|---|---|
| `CONTOSO_REQUESTER_CREDENTIAL_SOURCE` | `windows-certificate` |
| `CONTOSO_REQUESTER_CERTIFICATE_PATH` | Absolute path to the public PEM |

The included `contoso_backend.requester_certificate` module and launcher support
this mode. For an authorized first-time local credential setup, the following
creates a new key in the current Windows user's Credential Manager and writes
only its public certificate. Use a new filename and an existing parent directory;
it does not register anything in Entra or change active settings:

```powershell
.\.venv-local\Scripts\python.exe -m contoso_backend.requester_certificate `
  --client-id "<requester-application-id>" `
  --public-certificate "<absolute-path-to-new-public.pem>"
```

An authorized operator must register that public PEM on the matching requester
application and approve its APIM access before the launcher can work.
The retained secret mode uses `CONTOSO_REQUESTER_CREDENTIAL_SOURCE=windows`
(also the launcher's default when omitted), with no certificate-path field, and reads
`ContosoAgentDemo:requester:<client-id>` from Credential Manager. It is not a
fallback for a failed certificate. Never put either credential in config or Git.

The private key is protected in the current Windows user's Credential Manager,
not stored in Git or the public PEM. Only the public certificate is registered
on the requester application in Entra. The requester signs a short-lived client
assertion to obtain an access token. **APIM receives a Bearer token, not a client
certificate. This is not mTLS.**

The certificate mode checks validity and key matching and does not silently
fall back to the old secret. Rotate by registering the new public certificate
alongside the old one, accepting a real token request and APIM invocation, then
retiring the old credential explicitly. Preserve a reviewed return path.
See [Microsoft's certificate credential contract](https://learn.microsoft.com/entra/identity-platform/certificate-credentials).

### Build and start

Once the configuration and credential match your revision:

```powershell
npm run build
.\start-local.ps1 -CheckOnly
.\start-local.ps1
```

Open **http://localhost:8000/** and keep the terminal running. The launcher
prefers `.venv-local`, then `.venv`. It does not install dependencies, deploy
services or replace an occupied port. Use Ctrl+C to stop only your server.

Use `localhost` consistently. Substituting `127.0.0.1` can fail Host, Origin
or redirect validation even though both addresses are loopback.

The server verifies signature, issuer, tenant, audience, expiry, delegated
scope and the approved user before calling APIM. It acquires its own requester
token rather than forwarding the browser token downstream. Do not bind this
Windows launcher to a LAN interface or treat it as a cloud hosting solution.

## Local troubleshooting

| Symptom | Check / action |
|---|---|
| Browser cannot connect | Start the launcher and check its startup output |
| Port 8000 is occupied | Use the known existing app or identify its owner. Do not kill an unrelated process |
| `invalid_host` or redirect mismatch | Use `http://localhost:8000/` and matching configured redirect |
| Missing frontend build | Complete `frontend\config.js`, then run `npm run build` |
| Unsupported configuration field | Check revision compatibility, especially certificate settings |
| Missing or mismatched credential | Ask the authorized operator to provision the matching protected credential. Do not add fallback identities |
| Signed in but access denied | Verify the application-tenant object ID, API audience and `access_as_user` scope |
| Sign-in popup blocked | Allow the expected Microsoft sign-in interaction in that browser. A different browser's login does not establish this browser's session |
| Question remains in the draft | It has not been sent. A selected suggestion or enabled Send button is not delivery evidence |
| No matching capability | Use an exact registered question from the [demo guide](demo.md#registered-questions) |
| Downstream failure | Record sanitized status and request ID. Check [operations troubleshooting](operations.md#troubleshooting) without logging credentials or prompts |

After an authorized config change, restart only the server you own.
Do not reset the shared connection or broaden allowlists as a troubleshooting
shortcut.

## Teams setup boundary

Using the published Contoso Motors app does not require the local Python
server. An operator must already have published the native Foundry Activity
endpoint and Teams application and granted the intended access.
Follow [Teams usage](demo.md#use-microsoft-teams) and
[Teams admission](operations.md#teams-admission-and-delivery).

Do not deploy another bot, Function or App Service just to use this entry point.
Teams installation, platform authorization and runtime caller admission are
separate checks.

## Development checks

Run these commands from the repository root with this revision's dependencies.
Install only into a new or incomplete development environment. The development
lock includes the Activity adapter; do not replace the pinned agentserver
Git dependencies with unrelated latest releases:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest --import-mode=importlib contoso contoso_backend
npm ci
npm test
npm run test:ui
npm run build
```

UI tests need the Playwright browser required by the installed version. If it
is missing, install it with `npx playwright install chromium`.
If the test port is occupied, set an unused port without stopping another app:

```powershell
$env:CONTOSO_TEST_PORT = "8019"
npm run test:ui
```

The included suite covers catalog discovery, Teams, certificate authentication
and guarded SDK operations. Browser tests use synthetic sign-in and service
fixtures. They do not authenticate to Azure or Studio. Synthetic tests prove
contracts, not live service acceptance. See [validation evidence](status.md#local-validation)
for the clean-tree checks of this revision.

## Diagrams and historical tooling

The README opens with the [icon-based overview](contoso-motors-architecture.svg),
with a [PNG](contoso-motors-architecture.png) and
[editable Excalidraw source](contoso-motors-architecture.excalidraw).
Its generator embeds unmodified official product icons and checks text widths
and connector/text intersections before rendering the PNG:

```powershell
node tools\render-motors-architecture.mjs --png
```

Use the existing Node.js and Playwright environment. Omit `--png` to generate
only SVG and Excalidraw. The Mermaid flows in
[README](../README.md#how-the-current-demo-works) and [connectivity](connectivity.md)
remain complementary technical views. Keep all current views and prose consistent.

`tools\render-diagrams.mjs` and the older `docs/architecture*.svg`, `.png`
and `.excalidraw` assets describe an earlier Internal-APIM target. They are
retained historical material, not the current deployed External-APIM topology.
Running the old renderer does not regenerate the current diagrams.
See [product icon attribution](icons/README.md) when reusing those assets.
