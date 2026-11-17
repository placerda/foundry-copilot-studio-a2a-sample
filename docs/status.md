# Current status and source availability

[Home](../README.md) · [Setup](setup.md) · [Demo](demo.md) ·
[Architecture](connectivity.md) · [Operations](operations.md)

Source and documentation consolidated on **2026-11-17**. The cloud observations
below remain the recorded demo acceptance from **2026-11-13**, reviewed on
**2026-11-14**. Packaging and local tests are not new live acceptance, a
continuously refreshed health report or a new Azure audit.

## Configured demo

| Area | Recorded state | Evidence boundary |
|---|---|---|
| Router | `contoso-foundry-agent`, version 20, selected at 100% | Image candidate, selection and independent readback verified |
| Logistics | `contoso-motors-logistics`, version 6, selected at 100% | Preserved during the Router20 presentation change |
| Supplier | Separate Contoso Motors Supplier in Copilot Studio | Shared delegated connection and Motors source |
| API Center | Per-request authenticated discovery for both specialists | Real Router-identity path was exercised, not just ARM inventory |
| APIM | Developer, External VNet, direct Router ingress and two specialist A2A routes | No Function in the current request path |
| Local application | Windows Python backend and browser UI | Earlier integrated acceptance exists. Final post-cutover UI revalidation is still pending |
| Teams | Native Router Activity entry | Both destinations accepted on Router18/logistics6 for one approved administrator |
| Latest Teams presentation | Compact references on Router20 | One new supplier response, 140 words, four inline citations and four cited sections. Full DOM and footer screenshot checked |
| Teams session transition | Same conversation/session moved from idle version 19 to version 20 | No session stop, deletion or duplicate submission |
| Legacy Function | Removed with its exclusive plan and six exclusive grants | Absence was verified. Shared services were retained |
| Shared connection renewal | Later signed token accepted after the prior token expired, without a new login | Does not expose or prove the internal OAuth refresh request or cold-start latency |

Router19 was retained as a return target for Router20. **Keeping a version is
not the same as exercising rollback.** Local tests and an SDK preflight are not
proof that all business routes or administrative duties are accepted.

## Source availability

This revision includes the previously local Motors implementation. The
[guided code and setup path](foundry-apim-studio.md) follows that same
implementation; it does not introduce a second simplified runtime.

| Material | In this revision? | Consequence |
|---|---|---|
| Frontend, Python server, launcher and config examples | Yes | Local client implementation; real sign-in needs your tenant and credentials |
| Router, provider clients and focused tests | Yes | Current source is available; publishing source does not rebuild Router20 |
| API Center runtime and Motors catalog/route templates | Yes | Per-request discovery and bounded APIM routes; resources/access still need setup |
| Native Teams Activity implementation and admission tests | Yes | No separate Function or bot runtime; Teams publication and caller admission are separate |
| Motors scenarios, prompts and source files | Yes | Versioned synthetic material, including exact source/citation contracts |
| Certificate requester and updated launcher | Yes | Public PEM plus protected Windows private key; legacy secret mode retained explicitly |
| Administration, dependency, publication and selection helpers | Yes | Reviewed private binding pins and authorized private connectivity required |
| Function-disabled-by-default IaC | Yes | Partial reference, not a full fresh-environment installer |
| Current documentation | Yes | Guided setup, code map, credential boundaries and historical tooling inventory |
| Private bindings, credentials, raw logs and session evidence | Intentionally no | Keep outside Git. Only sanitized outcome summaries belong here |

A fresh source copy supports offline development. A complete new cloud
environment has **not** been provisioned and accepted from this revision.
Studio environment/channel availability, publishing, delegated consent, Entra
grants, private DNS/networking and immutable release bindings remain external
setup steps. Do not use the existing live deployment as proof of clean-room
cloud reproducibility.

## Local validation

Validation uses an isolated Git-tree export without local configuration,
credentials or ignored operator files. Existing Python and Node dependencies
are reused; this is a source-isolation check, not a fresh dependency download
or a cloud deployment. Exact commands are in [development checks](setup.md#development-checks).
The release preparation records the candidate tree and test outcomes separately
from the dated live evidence above.

Recorded on **2026-11-17**:

| Check | Result |
|---|---|
| Current Python suite | 2,706 passed, 3 skipped, 11 subtests passed |
| JavaScript contracts | 16 passed |
| Fixture-based browser suite | 55 passed; isolated loopback test server |
| Frontend build | Passed with `config.example.js`, not private client settings |
| Local documentation paths | 82 links resolved |
| Bicep compilation | Five templates compiled: foundation, catalog, Studio connection and both specialist routes |

The Python run reported 36 SDK adapter deprecations and one background Azure
Monitor configuration-worker warning when a test's network guard blocked its
request. Bicep reported two fixed-public-cloud URL warnings and nine existing
foundation resource-type warnings. These checks do not replace service-side
template validation, a fresh dependency installation or live channel acceptance.

## Known gaps

| Item | Status |
|---|---|
| Current implementation and clean-source validation | Source included; local checks are separate from cloud provisioning |
| Provision and accept a complete fresh Azure/Studio environment | Not exercised; requires the external setup and authorizations above |
| Revalidate both destinations through the final local UI after cutover | Pending. The prior unsubmitted draft is not an accepted invocation |
| Check every still-needed legacy administrative duty against its replacement | Incomplete |
| Exercise explicit version rollback with endpoint/identity/authorization preserved | Pending |
| Multiple-user acceptance | Waived by the owner for this demo, not proven |
| Production identity and isolation acceptance | Not established by the administrator-only demo |
| Paid evaluation and stage 2 | Deferred |
| Optional Container Apps hosting | Not deployed |
| General cold-start and latency guarantees | Not established |

The Function was removed under explicit reduced-scope demo approval.
That decision did not turn the remaining checks into successful evidence.

## How to maintain this record

After an authorized deployment, record the immutable versions, image/build
identity, selection readback, exact channel/scenario tested and remaining gaps.
Keep publication, activation, live acceptance and rollback outcomes separate.
Update the architecture if network mode, identity boundaries or routing changes.

Do not commit operator IDs, tenant-specific binding files, token responses,
browser sessions, private keys or raw conversation dumps. Retain operational
evidence in the approved private location and publish only sanitized summaries.
