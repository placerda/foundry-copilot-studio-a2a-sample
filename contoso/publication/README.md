# Restricted private publication

This package is a publication worker, **not** a general deployment API. It does
not provision resources, run ACR tasks, change RBAC, open public access, perform
interactive login, or invoke business agents. The approved Functions Gateway
must run it inside its existing VNet. No additional cloud footprint is needed.

## Public Python API and registration

`OperatorConfig` is immutable, operator-created configuration, never request
JSON. It binds a package root, writable job-workspace root, SHA256-pinned
`azure.yaml`, CLI executable and extension files, project/registry/storage ARM
IDs and HTTPS endpoints, tenant/subscription/group, environment, digest-pinned
image, and two `Document` instances. No resource identifiers are hardcoded.

Only these services are accepted:

* `contoso-knowledge-agent`
* `contoso-foundry-sdk-agent`
* `contoso-foundry-agent`

The service-level image can be the literal approved digest or
`${CONTOSO_RUNTIME_IMAGE}`. The worker controls that variable from operator config.
All three must use `host: azure.ai.agent`, `kind: hosted`, `language: docker`,
`docker: {imagePassthrough: true, remoteBuild: false}`, and project `"."` (or omit
project). The entire reviewed manifest is hash-pinned; hooks, arbitrary commands,
builds, additional services and code-deployment configuration are rejected.
CPU/memory/protocol/card/endpoint settings stay in the parent's reviewed manifest.

`production_executor(config, job_store=durable_store)` constructs production
adapters. `durable_store(metadata)` must persist the safe JSON-shaped job
metadata in the parent's existing private state storage; it receives running
and terminal states. Terminal persistence failure propagates, never becomes
fake success. The parent owns storage lifecycle/retention and route registration.

Register `executor.publish(process_user_agent=one_shot_operator_value)` **only**
behind the existing exact Azure CLI client ID + admin OID + `CONTOSO.Deploy`
delegated **user** authorization. Azure Easy Auth is an additional gate, not a
replacement for this verification. The publication-only phase must not register
invoke, connection-token or callback routes. Prefer one `publish_service()` per
admin request. Use the parent's existing bounded worker mechanism only when
choosing the retained all-three `publish()` operation; this package creates no
daemon or generic job proxy.

Also available: `executor.preflight()`, `executor.publish_sources()`,
`executor.get_agent_records()`, `executor.publish_service(service,
process_user_agent=...)`, `executor.prepare_extensions(process_user_agent=...)`,
`executor.diagnostic_self_test(process_user_agent=...)`,
`PublicationJob.as_dict()`, `PublicationFailure.code`, and `private_dns()`.
Keep diagnostics, preflight and source publication behind the same admin gate.
`PublicationExecutor` accepts injected SDK/command/auth/DNS adapters for tests;
production must use the production factory, not request-selected adapters.
The default command adapter now returns `CommandResult(returncode, error_code,
version_valid, environment_bound, auth_authenticated, token_credential_valid)`: only fixed diagnostic
codes and boolean facts are retained. Existing integer-returning test adapters
remain supported for publication, but integer zero alone cannot pass diagnostics.

### Explicit runtime bindings and identity bootstrap

`environment_values` must explicitly contain **all** these nonsecret bindings;
its historical empty default is now rejected:

* `CONTOSO_ENTRA_TENANT_ID`, `CONTOSO_ENTRA_API_CLIENT_ID`: UUIDs; the runtime tenant
  must match the operator-selected tenant.
* `CONTOSO_STUDIO_ENVIRONMENT_ID`: UUID.
* `CONTOSO_STUDIO_STANDARD_SCHEMA`: a 1–128-character schema identifier using only
  ASCII letters, digits, `_`, `.`, and `-`, consistent with the Standard
  provider's identifier grammar. `new_CONTOSOSTUDIOStandard` and
  `cr953_CONTOSOSTUDIOStandard` are valid. It is **not** an HTTPS endpoint; URLs,
  slash/backslash paths, whitespace and control characters are rejected.
* `CONTOSO_STUDIO_HARNESS_ENDPOINT`: native Copilot Harness A2A HTTPS on exactly
  `api.powerplatform.com`, with optional port `443`. The path must match the
  existing A2A provider contract:
  `/copilotstudio-a2a/<identifier-segments>/agents/<agent-identifier>`.
  This includes `/copilotstudio-a2a/tenants/<tenant>/environments/<environment>/agents/demoConsumerAgent`.
  Identifiers use ASCII letters, digits, `_`, `.`, and `-`; dot-only traversal
  segments and encoded paths are rejected. Standard SDK environment-specific
  discovery hosts are not Harness hosts. Queries, fragments and userinfo are
  rejected, including `api-version` and tokens.
* `CONTOSO_KNOWLEDGE_A2A_ENDPOINT`: HTTPS on the selected Foundry project host and
  under that project's `/agents/contoso-knowledge-agent/` path.
* `CONTOSO_GATEWAY_URL`: HTTPS `*.azurewebsites.net` origin, without an API path.
* `CONTOSO_SOURCE_BLOB_ENDPOINT`: must match the approved source Storage endpoint.
* `CONTOSO_KNOWLEDGE_SOURCE_SHA256`: must match the approved knowledge document hash.
* `CONTOSO_KNOWLEDGE_PLATFORM_CALLER_IDS`, `CONTOSO_ROUTER_PLATFORM_CALLER_IDS`:
  JSON arrays of unique bounded printable opaque strings. They are **not**
  coerced to Entra OIDs.
* `CONTOSO_KNOWLEDGE_RUNTIME_ROLE`: explicitly `identity-probe` or `knowledge`.
* `CONTOSO_ROUTER_RUNTIME_ROLE`: explicitly `identity-probe`,
  `identity-link-probe`, or `router`.

Caller emptiness is **per service role**: the knowledge caller list may be empty
only for knowledge `identity-probe`; the router caller list may be empty only
for router `identity-probe`. `identity-link-probe` requires a nonempty
explicit router caller allowlist and a nonempty approved knowledge A2A endpoint,
even while the knowledge remains `identity-probe` with an empty caller list.

When the knowledge is `identity-probe` and the routers are either
`identity-probe` or `identity-link-probe`, STUDIO environment/schema/Harness and
Gateway values may be explicitly empty; the link role's knowledge endpoint cannot.
Any business role requires all existing provider/Gateway fields to be nonempty
and validated. Tenant/API client, source endpoint and source hash remain
explicit and validated in every mode. All keys remain mandatory: no value is
implicitly supplied. Unknown roles, missing fields and arbitrary endpoints are
rejected. `OperatorConfig.identity_probe` is true only for the original
both-`identity-probe` mode, not the outbound diagnostic link transition.
`AZURE_LOCATION` and `AZURE_AI_MODEL_DEPLOYMENT_NAME` remain optional.
JSON caller-list quotes/newlines are escaped correctly when writing the
nonsecret deployment `.env`.

The parent's Hosted identity-probe runtime owns its exact probe-only behavior;
publication does not invoke it. Publish the three real probe agents individually,
discover actual SDK identities and platform caller keys through the approved
probe, let the parent assign narrowly scoped roles, then redeploy explicit
business bindings. This package never makes those role assignments.

### Fixed agent records after partial publication

`get_agent_records()` performs private DNS enforcement and
`AIProjectClient.agents.get(name)` for **only the three fixed names**, using the
same Gateway MI. It does not require an earlier successful command or `.env`.
Each selected record contains only:

```
name, version, status, instance_identity, agent_endpoint
```

Version and status come from `agent.versions.latest`; a failed/creating version
remains failed/creating. Instance identity projects only SDK
`principal_id`, `client_id`, and identity `status`; UUIDs are validated.
`agent_endpoint` retains selected version/protocol configuration and an actual
URL only when supplied by the response, never authorization schemes or headers.
The pinned SDK's typed endpoint configuration usually contains protocol
configuration, not a native URL: this API does **not** manufacture endpoints.
Actual azd endpoint outputs remain the authoritative deployment result.

A genuine SDK 404 is represented as `status: not_found` with null version,
identity and endpoint. Other failures raise safe `agent_record_lookup_failed`,
not an empty successful result. No definition, runtime environment, raw SDK
serialization, log, token, agent card or opaque user-key-to-OID inference is
returned. This is appropriate for inspecting a real agent created before azd
failed on automatic role assignment; the parent can use its genuine identity
for per-agent AcrPull and retry only that service.

## Authentication evidence and feasibility

Two approaches investigated; no further authentication fallback is attempted:

1. **Native azd managed-identity mode:** pinned azd 1.37.2
   `pkg/auth/manager.go` implements `userProperties.ManagedIdentity` and
   `saveLoginForManagedIdentity`, persisting selection in `auth.json` under
   `auth.account.currentUser`. The exposed configuration options have no
   direct native-MI binding. `auth.useAzCliAuth` delegates to Azure CLI, not MI
   directly. Native login and hand-authoring/migrating `auth.json` are not used.
2. **Supported external credential integration:** azd's official
   `docs/external-authentication.md` documents `AZD_AUTH_ENDPOINT`,
   `AZD_AUTH_KEY`, and Linux `unix:` transport (supported since 1.33.0).
   `CredentialForCurrentUser` selects this credential before CLI/native caches.
   `cmd/middleware/login_guard.go` obtains that credential and checks an ARM
   token; it does **not** require recorded native login details.

The production implementation uses approach 2: an ephemeral Unix HTTP token
broker with a random in-memory key, socket mode 0600 and parent mode 0700.
It requests tokens only from explicit
`ManagedIdentityCredential(client_id=None, identity_config={})` on a Functions
host with `IDENTITY_ENDPOINT` and `IDENTITY_HEADER`. No user-assigned selector,
admin token cache, CLI credential, or default credential chain is used.
Tenant and token audiences are allowlisted. The key and tokens are never written
to disk or returned in job metadata. No TLS certificate is needed for the
documented Unix transport.

**Caveat:** pinned `Manager.LogInDetails` assumes that external credentials
represent a user and require a display username; `azd auth status` can therefore
misreport a genuine MI credential. Its external branch calls
`claims.DisplayUsername()` and returns `ErrNoCurrentUser` when empty, before
`auth status` verifies the credential. Even when a username exists, this branch
reports `EmailLoginType`; `CurrentPrincipalType` consequently maps it to User.
It does **not** implement correct non-user external principal metadata.
The former self-test incorrectly treated that user-login status as an MI
credential test; the diagnostic now checks the token credential directly.

The success broker envelope was already correct:
`{"status":"success","token":"<opaque>","expiresOn":"<RFC3339>"}`.
For an approved request whose SDK token acquisition fails, the broker now sends
HTTP **200** with the documented safe `status:error` / `code:GetTokenError`
envelope; `RemoteCredential` only decodes envelopes after HTTP 200.
Unauthorized, malformed, wrong-tenant or wrong-scope requests remain HTTP 403.
No token or SDK error message is reflected in the failure envelope.

Do not add a username claim, substitute an admin token, use auth status as proof,
or fall back to native config mutation. The login guard itself supports the
external credential, but any extension path requiring `LogInDetails` or
`CurrentPrincipalType` retains the upstream limitation. A passing credential
diagnostic does **not** establish Hosted deployment support. An actual approved
`azd deploy` remains the end-to-end feasibility test; if it requires that
unsupported principal metadata, stop with the fixed failure instead of another
authentication workaround.
No cloud deployment was performed by this package's implementation task.

Functions is the already approved VNet-connected execution footprint for this
private publication operation, **not an azd authentication requirement**.
The restricted command worker runs there to reach private Foundry/ACR/Blob
endpoints; moving azd alone does not fix its external-principal metadata model.

Sources, pinned to the inspected CLI version:

* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/docs/external-authentication.md
* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/pkg/auth/manager.go
* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/pkg/auth/remote_credential.go
* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/cmd/middleware/login_guard.go
* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/pkg/auth/principal.go

## Private preflight and sources

Every Foundry/ACR/Blob FQDN must resolve exclusively to RFC1918 addresses; empty,
mixed public/private, loopback, link-local and public DNS results fail closed.
Production then reads the configured project ARM resource using the MI and
forces an HTTPS `AIProjectClient.agents.list()` page request using that same MI.
`azure-ai-projects==2.10.0` has **no `get_project` method**: the injected
`get_project` adapter is an explicit fixed-URL ARM GET, not a fabricated SDK API.
This requires existing project-read permission. Parent-resource PNA and the five
PE approvals are the parent's already-verified foundation/deployment gate;
the worker does not demand new parent/ACR/storage Reader roles to recheck them.

`publish_sources()` uses `BlobClient` with the same MI, fixed `sources` container,
and only `knowledge-returnables.md` / `supplier-delivery-terms.md`. Both package
hashes are checked before the first write. First uploads are create-only;
updates require an operator-supplied expected ETag and `IfNotModified`.
The response records the real service ETag and hash. It never accepts request
bytes, paths, container names or arbitrary URLs. Source-write conflicts remain
failures. Existing Blob data-write permission at the sources container is
required; this package creates no role assignments.

## Command and result contract

Each service runs exactly the fixed executable:

```
azd deploy <approved-service> --no-prompt --environment <approved-env> --timeout 210
```

The process has a fresh writable job workspace and isolated `HOME`,
`AZD_CONFIG_DIR` and unused Azure CLI config directory. It receives only a small
OS environment allowlist, operator-approved non-secret deployment values and the
ephemeral external-credential variables. `CI=true` prevents interactive login
branches. The caller's one-shot user-agent is passed only in this process
environment, never copied into `.env`, manifest, code or configuration.

The read-only application package is never executed or chmodded directly.
Both static-package and prepared-extension publication copy only pinned files
into `workspace_root/<job_id>/`. The actual azd process path is
`workspace_root/<job_id>/bin/azd`; extension executables are under
`workspace_root/<job_id>/azd-config/extensions/<fixed-id>/<fixed-id-with-dashes>-linux-amd64`.
Copies are SHA256-checked against their approved source pins before permissions
are applied. Only azd and the seven fixed Linux extension executable paths get
explicit `0755`; configuration JSON and other pinned artifacts get `0644`.
Workspace/config parent directories are `0700`. Genuine `extension.installed`
records cannot select unknown executable paths or unpinned files. Every copied
executable is rehashed immediately before each command. Original package bytes
and modes remain unchanged even when all packaged files arrive as `0644`.
Public APIs and operator fields are unchanged.

Command streams are drained concurrently into **bounded in-memory tails**,
at most 128 KiB each for stdout and stderr. No stream bytes, messages, error
objects, usernames, tokens or environment values escape the command adapter,
reach job metadata, or become log files. Only allowlisted structured error codes
and static failure patterns are projected into fixed diagnostic codes. Unknown
nonzero failures retain `deployment_nonzero_exit`; an unknown structured error
event cannot become success even if the process exits zero
(`azd_unclassified_error`). Commands have a 210-second outer process bound,
including reserved cleanup time, as well as the azd
timeout. An Azure-side deployment can outlive timeout; failure means failed
publication observation, not proof that Azure rolled back. No retry is automatic.
`publish()` retains the three-service operation and can take up to 630 seconds.
For Azure's HTTP response limit use `publish_service(<fixed-name>, ...)`:
exactly **one** bounded deployment command and one shared durable job per
request. Unknown services fail before credentials, metadata, sources or commands.
No detached daemon or generic job proxy is created.

### Safe bootstrap diagnostics

Parent may register `executor.diagnostic_self_test(process_user_agent=one_shot_value)`
behind its exact existing admin/delegated-user authorization. No new wrapper,
route or cloud resource is created here. It uses the **same** pinned executable
copy, extension files, manifest, environment creation and fixed-command `_run`
as publication. A fresh owner-only workspace is removed on completion.

The operation validates all eight fixed installed extension records, their
pinned binary paths, absence of supplied auth settings, and manifest binding
references locally. It then runs exactly these built-in commands, each with
at most 30 seconds and an overall 210-second ceiling:

```
azd version --output json --no-prompt
azd env get-values --environment <approved-env> --output json --no-prompt
azd auth token --scope https://management.azure.com/.default --tenant-id <approved-tenant> --output json --no-prompt
```

The version JSON must report at least 1.37.0. Environment JSON must exactly match
the approved seeded values; it is never returned or persisted. Authentication
uses the same Unix external broker and Gateway system-assigned credential.
The final command is the pinned **hidden built-in** implemented by
`cmd/auth_token.go`, not a newly invented auth setting or a stable public CLI
promise. It directly calls `CredentialForCurrentUser` with `NoPrompt:true`,
then `GetToken`; the explicit tenant skips subscription-based tenant discovery.
It does not call `LogInDetails` or require a username. Its JSON contract is
`{"token":"<opaque>","expiresOn":"<RFC3339>"}`.

The adapter consumes that sensitive stdout only in bounded process memory.
It verifies unexpired RFC3339 output against the exact token digest, integer
expiry and management scope observed from this same Gateway-MI broker during
that command. Correlation is cleared before the command and on broker closure;
only boolean facts escape. Missing, mismatched, truncated or expired output
cannot pass. Token stdout is never classified as prose, returned, persisted or
logged; stream buffers are cleared. No claims are added or inferred.
There is no login, auth.json mutation, extension install, agent creation/listing,
source upload, business invocation or project ARM resource request.
Extension registration is a **local metadata/hash check**, not proof of provider
execution. Deliberately do not use `extension list` (it queries registered
sources even with `--installed`) or `ai agent --help` (it may auto-install).

Result contains only:

```
{
  "status": "passed" | "failed",
  "checks": {
    "executable": boolean,
    "version": boolean,
    "extension_registration": boolean,
    "environment_binding": boolean,
    "external_credential_requested": boolean,
    "managed_identity_credential": boolean,
    "azd_credential": boolean
  },
  "error_code": null | "<fixed-code>",
  "limitation_code": "azd_external_principal_metadata_unsupported"
}
```

`azd_credential=true` means a genuine broker-to-CLI token roundtrip, **not a
logged-in user**. `auth status` is no longer run or allowed by `_run`; its
zero-exit `unauthenticated` response alone is not evidence that MI token
acquisition failed. `auth_authenticated` remains an informational parser fact
for compatibility and never satisfies the credential check.

`status:passed` means these seven bootstrap checks passed; it is deliberately
accompanied by the fixed `limitation_code` even on success. That limitation
describes the pinned non-user principal-metadata contract, not failed token
acquisition and not a publish result. No new identity, auth mode or fallback
has been introduced. Native/non-user MI Hosted publication remains unproven.

Parent next step: re-stage this publication module with the **unchanged pinned
Linux toolchain**, then run the existing admin-only `diagnostic_self_test`
with its one-shot process user-agent argument. Inspect only this safe result.
Do not run `auth token` directly to a terminal/log or return its stdout.
No parent Gateway route or binding changes are required.

Fixed failure projection (no raw text is returned):

| Code | Meaning |
| --- | --- |
| `azd_authentication_failed` | CLI reports authentication failure |
| `azd_not_logged_in` | Explicit CLI login failure/code, not zero-exit user-status JSON alone |
| `azd_credential_unavailable` | Missing credential / broker token error |
| `azd_auth_principal_unavailable` | CLI cannot derive username or user principal |
| `azd_executable_permission_denied` | Executable permission failure |
| `azd_executable_format_invalid` | Executable format mismatch |
| `azd_executable_unavailable` | Executable missing |
| `azd_extension_missing` | Fixed installed extension unavailable |
| `azd_invalid_flags` | Unknown flag or command |
| `azd_schema_invalid` | Manifest/YAML/schema failure |
| `azd_environment_binding_invalid` | Missing/incorrect environment or variable |
| `azd_role_assignment_write_forbidden` | Denied `Microsoft.Authorization/roleAssignments/write` |
| `azd_private_network_failure` | Explicit private-network/public-access failure |

Additional fail-closed self-test codes include `azd_version_unconfirmed`,
`azd_external_credential_roundtrip_unconfirmed`, `azd_external_credential_not_observed`,
`azd_auth_configuration_invalid`, `azd_diagnostic_command_failed`, and
`azd_diagnostic_dependency_failed`. Successful earlier checks stay true when a
later check fails; unattempted checks remain false. Ordinary safe package/hash,
timeout, command-result and workspace-cleanup failures also remain explicit.
No diagnostic result is stored as a successful publication job.

The fixed JSON contracts and read-only commands were verified against official
1.37.2 CLI help/source, including its zero-exit unauthenticated behavior:

* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/cmd/auth_status.go
* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/cmd/auth_token.go
* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/pkg/contracts/auth_token.go
* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/pkg/contracts/auth.go
* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/pkg/contracts/version.go
* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/cmd/env.go
* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/cmd/extension.go

Source upload is now a **separate explicit** `publish_sources()` operation.
Neither all-service nor single-service publication automatically uploads or
repeats create-only documents; their job `sources` field therefore remains empty.

Only after exit code zero does the worker read the actual selected
`.azure/<environment>/.env`, verify the unchanged deployment binding, and retain
`AGENT_<SERVICE>_NAME`, `VERSION` and protocol `*_ENDPOINT` fields. Other values
never enter job metadata. Partial service successes remain recorded on a failed
job; missing or mismatched outputs are failures. All ephemeral workspaces are
removed after the attempt.

## Production packaging handoff (parent-owned)

Flex Consumption is a code-package deployment, not a custom-container host.
Include a **Linux amd64** CLI and extension executables in the existing Gateway
code artifact. Local Windows executable/extension files are not usable there.
Extract them during parent-controlled build/packaging, preserve executable bits,
and bind extracted files to operator SHA256 pins. Never download executables
or install extensions from the publication API.

Verified official azd archive:

* URL: https://github.com/Azure/azure-dev/releases/download/azure-dev-cli_1.37.2/azd-linux-amd64.tar.gz
* Archive SHA256: `4ad77050ea5f9e465309891b0aa7946cd87f0f590982ff54531c5ecdc1c2efd1`

Verify that archive **before** extraction, then independently hash the extracted
`azd` executable for `OperatorConfig.azd_sha256`. Those are different hashes.
Use only official `Azure/azure-dev` release assets and reject archive traversal
or symlinks during packaging.

### Current official extension versions, not stale local installation

The **current official registry**, inspected on 2026-11-04 UTC, provides the
versions below. All declare `requiredAzdVersion: >=1.37.0`, compatible with
azd 1.37.2. Agents beta.23 and Projects beta.16 satisfy the parent's documented
beta.20/beta.14 minimums. Previously installed beta.13/beta.8 are **not** the
selected production target.

Registry: https://aka.ms/azd/extensions/registry

Resolved official source:
https://raw.githubusercontent.com/Azure/azure-dev/refs/heads/main/cli/azd/extensions/registry.json

Observed raw-registry SHA256:
`2a8b26f959e42bc0c57b1303d051d9685cf6af01e41efa54a3d038a727583585`.
The registry changes over time; parent must snapshot and hash its actual
selected manifest and archives. These are evidence, not runtime "latest" pins.

| Extension | Selected available version | Linux amd64 archive SHA256 |
|---|---|---|
| `microsoft.foundry` | `1.0.0-beta.4` | Meta-extension; no binary |
| `azure.ai.agents` | `1.0.0-beta.23` | `e9e01c2026a5065e74849d0696a1d79ccce7888d49388b4f0e6c2520b0f4edb0` |
| `azure.ai.projects` | `1.0.0-beta.16` | `01c8704bea7c23f30471335800d184ba5f42a42851b750ff5f538e7e2afeee34` |
| `azure.ai.inspector` | `1.0.0-beta.9` | `aefd6c28e36d99aa1cc6763315358fa388068b177c99c189841542643a9bd1ff` |
| `azure.ai.connections` | `1.0.0-beta.11` | `467bd12fff8dbda4333cfab54a7eb9811779f6b3cdd0b290a33c5045fc938d14` |
| `azure.ai.routines` | `1.0.0-beta.11` | `cdc56195b48430005dd6e3fa342ab92fe6d03866cd004de17f62ff0e91608135` |
| `azure.ai.skills` | `1.0.0-beta.10` | `dfb38ba0a94f92f3b0c992eca2e6dfbaf9fdb8ed74c5ebae205bdbc25bc3d5f8` |
| `azure.ai.toolboxes` | `1.0.0-beta.13` | `e06c0a5c9e234a2c2c7a23afeb4f63d0da5bc0d8e35c6dfbc519d370f85b1d21` |

Each concrete official URL was inspected in that registry. They follow:

```
https://github.com/Azure/azure-dev/releases/download/azd-ext-azure-ai-<component>_<exact-version>/azure-ai-<component>-linux-amd64.tar.gz
```

For example, the selected agent provider:
https://github.com/Azure/azure-dev/releases/download/azd-ext-azure-ai-agents_1.0.0-beta.23/azure-ai-agents-linux-amd64.tar.gz

### Supported offline Linux installation and exact schema

azd 1.37.2's **actual local CLI help** supports:

```
azd extension install microsoft.foundry --source <local-registry-path> --version <exact-meta-version> --no-prompt
```

Official `pkg/extensions/manager.go` verifies archive checksums, accepts local
artifact file paths, installs binaries beneath
`<AZD_CONFIG_DIR>/extensions/<extension-id>/<Linux-entryPoint>`, chmods them,
and saves **relative** executable paths in:

```
config.json -> extension -> installed -> <extension-id>
```

Each installed record includes `id`, `version`, `path`, `namespace`,
`capabilities`, `providers`, `source` and declared `dependencies`.
An example executable path is
`extensions/azure.ai.agents/azure-ai-agents-linux-amd64`.
The relative paths make an actual Linux installation portable to per-job
configuration directories. No Windows installation is copied.

Concrete implementation:

1. Parent stages the seven checksum-verified **official Linux archives** and a
   derived local registry beneath its approved package. Registry contains only
   the fixed eight IDs, exactly one selected version per ID, preserved dependency
   constraints, and only `linux/amd64` artifacts. Artifact `url` values become
   package-relative local paths such as `artifacts/azure-ai-agents-linux-amd64.tar.gz`.
   Do not put HTTP/file-scheme URLs in that staged registry.
2. Construct `ExtensionSetup(registry_relative_path, registry_sha256, artifacts,
   versions)`: `artifacts` is seven `(package_relative_path, sha256)` pairs;
   `versions` is eight `(fixed_extension_id, exact_version)` pairs. Pass it as
   `OperatorConfig.extension_setup`, with `extension_files=()`.
3. Call explicit admin-only
   `executor.prepare_extensions(process_user_agent=one_shot_value)`.
   It rechecks all hashes, validates the complete fixed graph, rewrites only
   staged artifact references to absolute **local** paths, and runs the one
   official install command above. No download, login or auth.json mutation.
   The user-agent exists only in that process environment.
4. Installation lives in a fingerprinted directory under the operator's writable
   `workspace_root/prepared-extensions/`. It is retained, with a safe file-pin
   inventory, so later requests do not reinstall. `TMPDIR`, `TMP`, `TEMP` are
   explicitly bound to that owned workspace's `scratch` directory; azd does not
   write to system temporary directories. The official installer executes a
   hash-rechecked, explicitly `0755` azd copy at
   `workspace_root/prepared-extensions/<fingerprint>/bin/azd`, not the packaged
   executable. Local archive copies are also rehashed. Fixed installed Linux
   binaries receive explicit `0755`; generated config JSON stays non-executable.
5. `prepare_extensions()` verifies **real** `extension.installed` versions and
   executable paths before returning `{"status": "prepared", "versions": {...},
   "files": {relative_config_path: sha256, ...}}`. A nonzero command or missing
   registry/binary is failure, not a preparation success. It does not expose
   auth directories or arbitrary config.
6. `publish_service()` requires the completed preparation, verifies its file pins,
   and copies its clean Linux configuration into the fresh job. It **never**
   runs an extension install itself: one request remains one deploy command.
   Actual local source registrations point at the retained preparation directory,
   so their staged registry/artifacts remain available without public lookup.

Alternatively, parent can provide `extension_files` from a genuine clean Linux
installation. The two modes are mutually exclusive. Missing/unprepared
extensions block publication; nothing is downloaded as a fallback.

Supported schema/install evidence:

* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/pkg/extensions/manager.go
* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/pkg/extensions/extension.go
* https://github.com/Azure/azure-dev/blob/azure-dev-cli_1.37.2/cli/azd/docs/extensions/extension-resolution-and-versioning.md

The official Linux installer has not been executed in Azure by this task.
Its exact command/schema are verified from the pinned CLI help and official
implementation; injected tests verify worker integration without claiming
a real cloud installation or deployment.

**Remaining production gates:** parent-packaged executable/extension permissions
and paths; operator-reviewed final manifest/hash and ACR image digest;
pre-existing project/source/deployment permissions; durable job store wiring;
and an actual VNet-hosted MI deployment proving extension compatibility within
210 seconds. No broadened RBAC, login/config hacks or public networking fallback
is authorized or implemented.

## Tests

From repository root:

```
.\.venv\Scripts\python.exe -m pytest contoso\publication\tests -q
```

Tests use only owned project-relative workspaces and injected calls. They are
not evidence of a completed cloud deployment.

For read-only validation of the complete real operator binding fixture, supply
process-only `CONTOSO_PUBLICATION_TEST_BINDINGS` (the explicitly approved nonsecret
JSON file) and `CONTOSO_PUBLICATION_TEST_PACKAGE` (the staged package directory)
when running the same suite. The integration test calls the parent's exact
`contoso.gateway_bootstrap.operator_config()` with compact JSON, checks the complete
configuration, all staged executable/extension/document hashes and the approved
manifest, and verifies the binding/manifest files are unchanged. It redirects
the constructor's temporary-directory lookup to an owned test workspace without
changing parent code. No preflight, credential acquisition, command, upload or
cloud operation runs. With neither fixture supplied this integration test skips;
synthetic full-shape and wrong-host regressions remain self-contained.
