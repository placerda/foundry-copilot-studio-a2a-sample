# Supplier–Buyer Agent Collaboration

A chat application that connects a **Microsoft Foundry Hosted agent** to a **Copilot Studio agent**. Try a fictional delivery negotiation and see answers with source references.

## Start here

The application runs on **your own computer** and connects to the configured Azure services. You need Windows and an approved Microsoft account.

**First time on this computer?** Complete the [local setup](docs/setup.md#local-application-setup) first. It covers prerequisites, configuration and the frontend build.

Open PowerShell in this repository's folder and run:

```powershell
.\start-local.ps1
```

Then:

1. Keep the PowerShell window open.
2. Open **[http://localhost:8000/](http://localhost:8000/)** in Edge or Chrome.
3. Select **Sign in with Microsoft** and use the approved account.
4. Choose one of the suggested delivery questions and send it.

To stop the application, press **Ctrl+C** in its PowerShell window.

`npm run build` only builds the frontend. It does not start the application. If port 8000 is already occupied, use the existing application instead of starting another server.

## Try the chat

The example order is for **500 units Friday**. The supplier proposes **300 Friday and 200 Monday**.

| Choose a suggested question about | What to expect |
|---|---|
| The proposed delivery split | An explanation of whether it meets the buyer's terms |
| Acceptance conditions | Required agreement and source references |
| An exact penalty | A clear statement that the source does not specify an amount |
| Approving the proposal | An explicit refusal to approve an order |

All business data is synthetic. The agents explain the terms, not approve purchases or verify inventory. Only the shared delegated connection is currently enabled.

## How it works

![Local application, API Management, Foundry and Copilot Studio](docs/architecture-overview.svg)

Your local Python server hosts the chat UI and calls APIM. Foundry routes supported questions to Copilot Studio and returns the answer with its sources. No AI model runs on your computer.

**Optional cloud hosting:** Azure Container Apps can host the same UI and Python backend together, instead of running the server locally. This option still requires container packaging, cloud configuration and acceptance. It is not deployed by `start-local.ps1`.

The diagrams show the target APIM architecture. The Credential Manager migration and optional cloud hosting are not completed deployments.

## Need help?

| Problem | What to do |
|---|---|
| Cannot open localhost | Run `.\start-local.ps1` and keep its terminal open |
| Missing configuration, build or credential | Follow the [local setup](docs/setup.md#local-application-setup) |
| Signed in but access is denied | Use the approved account. Sign-in alone does not grant chat access. See [troubleshooting](docs/setup.md#local-troubleshooting) |
| An unsupported question returns no match | Try one of the suggested questions |

To check local prerequisites without starting the server:

```powershell
.\start-local.ps1 -CheckOnly
```

## Technical guides

[Setup and troubleshooting](docs/setup.md) · [Connectivity and detailed diagram](docs/connectivity.md) · [Development checks](docs/setup.md#development-checks) · [Example questions and sources](contoso-demo/)
