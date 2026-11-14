# Operator guide

[Home](../README.md) · [Setup](setup.md) · [Demo](demo.md) ·
[Architecture](connectivity.md) · [Status](status.md)

This guide describes the existing **private-project SDK/ACR workflow** used for
the current Motors demo. It does not request a new deployment, grant or login.
It replaces the old Function-based instructions as the operational overview.

> **Availability:** the `motors_*`, `publish_motors_router` and
> `select_motors_router` helpers and their binding examples are still in the
> prepared local working copy, not published on `main`. Commands in this guide
> require that reviewed implementation. They are not executable instructions
> for a fresh clone. See the [availability matrix](status.md#source-availability).

## Prerequisites

Use the existing selected Foundry project and deployment boundaries. Operators
need approved private connectivity and DNS, an authorized Azure CLI identity,
the explicit tenant, and the matching Python dependencies.

Set `AZURE_CONFIG_DIR` only in the command process to the selected existing
Azure CLI configuration directory. Do not switch the user's global context,
create a new login unnecessarily, disable TLS or enable Foundry public access.
Check the selected subscription and tenant before any mutation.

Keep reviewed binding files outside Git. The local
`contoso\motors-admin.bindings.example.json` requires:

| Binding group | Required values |
|---|---|
| Operator/project | Tenant, authorized operator object ID, project ARM ID and matching endpoint |
| Router | Numeric version, immutable definition hash, deterministic source archive hash and actual agent principal |
| Logistics | Numeric version, immutable definition hash, actual agent principal and model deployment |
| Supplier | Reviewed local Studio source hash |

The actual Hosted agent identity is not interchangeable with the project,
APIM or requester identity. Resolve identities independently. Never copy a
freshly observed hash into a binding just to bypass an unexpected mismatch.
Expected pins must come from the reviewed release.

The SDK helpers use explicit `AzureCliCredential`, disable automatic retries
and redirects, and retain TLS checks. Their preflight rejects mismatched
operator, project, private DNS, agent selector, definition or identity.

## Read-only checks

From the prepared working copy, with the selected process-local Azure context:

```powershell
.\.venv\Scripts\python.exe -m contoso.ops.motors_admin `
  --bindings "<absolute-path-to-reviewed-bindings.json>" preflight
```

| Operation | What it verifies | Does not prove |
|---|---|---|
| `preflight` | Reviewed project, identities, definitions and selectors | A successful business request |
| `records`, `metadata`, `diagnostics` | Selected deployment records and diagnostics | Equivalence with every old Function operation |
| `sources` | Local Router source archive, Studio reference and logistics embedded source | Remote Studio retrieval or source publication |
| `a2a-session-snapshot` | One explicitly named session at its expected agent version | Other sessions, a reply or rollback |

For a session snapshot, add `--agent contoso-foundry-agent` or
`--agent contoso-motors-logistics` and the exact `--session-id`.
The helper does not enumerate, stop or delete sessions.

`contoso.ops.motors_dependencies` also takes a reviewed `--dependencies` file.
Its `studio-source` operation compares the selected remote Studio attachment
and instructions with approved source/publication metadata.
Its `connection` operation checks the Credential Manager configuration, PKCE
endpoints, scopes, managed callback, connection status and APIM access policy.
Neither operation requests a Studio token or starts interactive consent.
`Connected` is not proof of refresh or a business invocation.

## Update a Router image

Publication, selection and acceptance are separate steps. Preserve the previous
release's reviewed definition, image and return pins throughout.

1. Make and test the requested code change. Confirm the exact source archive
   and exclude unrelated working-copy changes. The build archive includes runtime
   code, not documentation or tests.
2. Use the reviewed `contoso.ops.create_image` helper with the explicit existing
   registry ARM ID and tenant. This **mutates** the persistent ACR task and
   schedules a build. It is not a read-only check.
3. Capture successful build evidence: `task_id`, `task_principal_id`, `run_id`,
   `status`, `source_sha256` and digest-pinned `image`. Do not reuse evidence from
   an older source archive.
4. Preview publication with the command below. Review its source/image pins.
5. Add `--apply` only for the authorized publication. It creates and reads back
   an immutable **unselected** image-only candidate, preserving the active state.
6. Use the separate selector, then read back the selected state and perform
   the required channel/scenario acceptance.

```powershell
.\.venv\Scripts\python.exe -m contoso.ops.publish_motors_router `
  --bindings "<absolute-path-to-reviewed-bindings.json>" `
  --image-evidence "<absolute-path-to-new-build-evidence.json>" `
  --expected-source-sha256 "<reviewed-source-sha256>" `
  --expected-image "<registry>.azurecr.io/contoso-runtime@sha256:<reviewed-digest>"
```

The preview is offline. Apply repeats live preflight and validates the actual
private registry/build run. An already-current image is a verified no-op, not
evidence of new-version creation. ACR uses its existing managed-identity task
and reviewed private-registry access, not an anonymous public registry.

An ambiguous create or readback error requires independent inspection before
retry. Do not create duplicate candidates blindly. Do not replace this workflow
with an all-service legacy manifest deployment.

## Select a version and preserve rollback

The default selector command is a **live read-only preview**:

```powershell
.\.venv\Scripts\python.exe -m contoso.ops.select_motors_router `
  --bindings "<absolute-path-to-current-bindings.json>" `
  --target-version "<reviewed-numeric-version>" `
  --target-definition-sha256 "<reviewed-target-definition-sha256>"
```

Review the returned parent-state hash, target definition and explicit rollback
pins. For an authorized changed selection, use the same command with:

```text
--apply
--expected-parent-sha256 <parent-hash-from-reviewed-preview>
--exclusive-change-window
```

Exclude concurrent selector/configuration writers during this operation.
The helper compares observed state but does **not** claim an atomic
service-side compare-and-swap. It changes only the selector and independently
reads back the result.

Update the external bindings only after selection is verified. Save the old
bindings and return pins first. A rollback starts from the **currently selected**
version's bindings and uses a reviewed previous version and definition.
If other configuration has changed since the preview, review it again.
Never force a stale pin through or claim that a no-op exercised rollback.

The latest recorded change was Router19 → Router20 with logistics6 preserved.
Router19 remains a return target. An actual reverse selection has not been
accepted as part of that change.

## Teams admission and delivery

Use the Foundry native
[Microsoft 365 publication flow](https://learn.microsoft.com/azure/foundry/agents/how-to/publish-copilot-virtual-network).
For the configured private demo, preserve `Shared` visibility,
`BotServiceRbac`, existing Entra authorization and protocols, and only the
scoped Activity public endpoint. Do not enable general Foundry public access.

Teams callers are configured separately in `CONTOSO_TEAMS_PLATFORM_CALLER_IDS`.
Do not put them into the Python `CONTOSO_PLATFORM_CALLER_IDS` allowlist.
Only the validated Foundry SDK request context is identity evidence.
Activity `from`, display names, conversation IDs and arbitrary HTTP headers
do not authorize a caller.

`publish_motors_router --teams-platform-caller <reviewed-caller>` is the separate
admission-only mode, using the same `--bindings` and preview/apply convention.
Repeat that argument for the complete approved caller list. It changes neither
the image nor Python admission. Image and admission publication modes are
mutually exclusive. Review and select the candidate separately.

For a presentation change, check one agreed real question once, then inspect
the new reply and exact session version. A timed-out browser read does not
justify resending. Check the conversation first. An idle session may pick up the
new selection on the next message, as observed for 19 → 20. Do not stop or delete
sessions preemptively.

The SDK returns an ACK before provider execution and uses its supported
conversation continuation for the reply. ACK means **accepted**, not **answered**.
The current host bounds active turns, preserves the verified caller context and
uses the original 65-second total deadline, including reply delivery.

## Catalog and specialist configuration

The current local implementation uses `CONTOSO_ROUTER_SCOPE=api-center`.
Its settings bind the API Center origin, APIM A2A base URL, Entra tenant/API
application and explicit platform callers. The shared supplier variant is fixed.

API Center's `customProperties["contoso-routing"]` describes each managed entry:

| Fields | Contract |
|---|---|
| `schema_version`, `publication_state` | Integer 1 and `active` / `inactive` |
| `provider_id`, `capability` | Approved provider and supported capability |
| `studio_variant` | `copilot-harness` for supplier, explicit `none` for logistics. Missing is invalid |
| `endpoint`, `agent_version` | Approved APIM base and explicit version-addressed route |
| `protocol`, `protocol_version` | `a2a`, `1.0` |
| `sources` | Versioned source metadata with exact citable sections |

Active entries also need `preview` or `production` lifecycle. Metadata must not
change allowed callers, credential scopes or backend origins. Unmanaged entries
are ignored. Malformed managed entries and ambiguous active capabilities fail
closed. Preserve unrelated APIs when publishing the two Motors records.

The supplier and logistics APIM routes admit the actual Router identity and
required application role, validate bounded A2A input, remove forged headers
and return sanitized failures. Studio uses Credential Manager's delegated token
inside APIM. Logistics uses APIM's managed identity.
Do not substitute an operator probe for positive Router-identity acceptance.

The logistics source is embedded in the Prompt Agent instructions. It has no
tools/RAG. Source version, route version, Agent Card version and selected
Foundry version are distinct. Initial version1 bootstrap instructions do not
authorize changing the currently selected logistics version 6 endpoint back to 1.

## Function retirement and remaining duties

The Function, exclusive App Service plan and six exclusive grants were removed
after explicit owner approval with reduced demo acceptance scope. Do not repeat
their deletion or recreate them from the retained template.

Shared storage, monitoring, network, API Center, APIM and Foundry remain.
An obsolete identity still appearing in an allowlist is not permission to
weaken the guard or remove unrelated identities.

Keep these distinctions when assessing replacement coverage:

| Legacy duty | Current boundary |
|---|---|
| `/ops/sources` wrote Blob documents | `sources` verifies references, it is not a replacement writer |
| Linux/managed-identity worker diagnostics | New diagnostics inspect the selected private deployment |
| Identity bootstrap `probe` / `link-probe` | Not business questions for the Motors runtime |
| Callback correlation record inspection | Managed APIM callback is a different implementation |
| Version activation / return | Explicit guarded SDK selection, rollback still needs its own exercise |

Record any still-needed duty and its consumer before replacing it. Similar
operation names do not establish equivalent behavior. The owner's deletion
approval does not prove every original operational gate passed.

## Troubleshooting

| Symptom | Safe next check |
|---|---|
| Private project timeout or public DNS result | Confirm selected context, approved private DNS/VPN and disabled public access. Do not open the project publicly |
| Preflight pin mismatch | Inspect the changed selector/definition/identity independently. Do not overwrite the expected hash to force success |
| APIM401/403 | Identify which boundary rejected the request, then verify audience, role and exact caller |
| Connection is `Connected`, but Studio fails | Verify actual delegated access and downstream status. Connection metadata alone is insufficient |
| No matching capability | Confirm exact registered question and active compatible catalog entry |
| Citation validation failure | Compare source ID, provider, version and case-sensitive section names |
| Teams ACK without reply | Inspect the actual new message/session and sanitized continuation/provider outcome |
| Publication or selection timed out | Read live state independently before any retry or rollback |
| Latency budget exceeded | Diagnose the specific stage. Do not silently raise limits or truncate evidence |

Logs and reports must not include token values, authorization headers, private
keys or unnecessary prompt bodies. Keep raw caller/session evidence private.
Record sanitized status, exact versions and evidence boundaries in [status](status.md).
