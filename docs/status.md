# Current status and source availability

[Home](../README.md) · [Setup](setup.md) · [Demo](demo.md) ·
[Architecture](connectivity.md) · [Operations](operations.md)

Documentation reviewed on **2026-11-14** against recorded demo acceptance from
**2026-11-13** and the published Git tree. This is a dated status snapshot, not
a continuously refreshed health report or a new live Azure audit.

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

The configured demo was advanced in a local working copy. At this documentation
publication, `main` still has the earlier reference code. **This change publishes
documentation only.** It does not silently commit pending code or redeploy Azure.

| Material | On `main`? | Consequence |
|---|---|---|
| Reference frontend, Python server, launcher and config examples | Yes, older revisions | Useful for development, not a complete reproduction of current Motors |
| Reference Router, provider clients, tests and base infrastructure | Yes, older revisions | Do not assume deployed version 20 matches this source |
| API Center runtime and Motors catalog/route templates | No, pending code publication | Current discovery cannot be reproduced from a fresh clone |
| Native Teams Activity implementation and new admission tests | No, pending code publication | Current Teams behavior is documented, not installable from this baseline |
| Motors scenarios, prompts and source files | No, pending code publication | Use the [documented questions](demo.md#registered-questions) with the existing configured demo |
| Certificate requester and updated launcher | No, pending code publication | The published launcher still uses the protected Windows secret mode |
| `motors_admin`, dependency, publication and selection helpers | No, pending code publication | Operator commands apply only to the prepared working copy |
| Function-disabled-by-default IaC update | No, pending code publication | Published `main.bicep` still contains the legacy Function module |
| Current documentation | Yes | Explains current behavior, limitations and older code explicitly |
| Private bindings, credentials, raw logs and session evidence | Intentionally no | Keep outside Git. Only sanitized outcome summaries belong here |

The local working branch was `local-apim-identity-runtime`, but its name is not
proof of a published remote branch. Do not tell a new user to check it out as a
way to obtain the missing code.

Before declaring the demo reproducible, publish and review the implementation
separately, update this matrix, then run the documented steps from a clean clone.
Do not use the live deployment as proof that the repository contains its source.

## Known gaps

| Item | Status |
|---|---|
| Publish the current implementation and verify a clean clone | Pending, outside this documentation-only update |
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
