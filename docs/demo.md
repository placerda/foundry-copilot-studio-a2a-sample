# Run the Contoso Motors demo

[Home](../README.md) · [Setup](setup.md) · [Architecture](connectivity.md) ·
[Operations](operations.md) · [Status](status.md)

This walkthrough is for the **already configured Motors environment**.
To build your own environment, follow [Replicate the demo](replicate-demo.md)
first. To connect a new workstation to an existing deployment, use
[local setup](setup.md#local-application-setup).

## Business story

Contoso Motors needs 500 interior trim sets by Friday. The supplier proposes
300 on Friday and the remaining 200 on Monday. Racks and returnable boxes need
separate receiving and collection coordination.

Everything is synthetic. Friday and Monday are proposed days, not confirmed
calendar bookings. The agents explain requirements and unknowns. They cannot
approve the proposal, confirm stock or reserve transport.

## Use the local application

1. Complete [setup](setup.md#local-application-setup) on the prepared workstation.
2. Start `.\start-local.ps1` from the repository root.
3. Open `http://localhost:8000/` and sign in with the approved account.
4. Choose a supplier or logistics suggestion. It fills the draft.
5. Send once, then inspect the answer, source version and evidence limits.

Do not count a filled draft or successful click automation as a delivered
message. Confirm both the question and response in the conversation. If a
browser automation call times out, inspect the conversation before retrying.

## Use Microsoft Teams

1. Sign in to the approved tenant and open the already published **Contoso Motors** app.
2. Open its chat and send one registered question below.
3. Wait for the actual reply. A typing indicator or HTTP acknowledgement is not an answer.
4. Inspect the business response and compact **References** footer.

Teams does not require the local Python server. If the app is missing, contact
the deployment operator rather than publishing a replacement app. Access to
Teams itself does not imply access to the Foundry endpoint or runtime.

## Registered questions

Use the exact text for this bounded demo. Arbitrary paraphrases are not guaranteed
to match a capability.

### 1. Supplier: partial delivery

> We have an order for 500 units due Friday. We propose delivering 300 units Friday and 200 units Monday. Does this partial delivery satisfy the buyer's terms?

Expected: explain the 200-set shortfall by Friday and the need for written buyer
acceptance before dispatch. A proposal is not an approval or verified inventory.

### 2. Supplier: conditions and evidence

> What conditions apply to the supplier's proposal to deliver 300 of 500 units Friday and the remaining 200 Monday, and what are the evidence limits?

Expected: written acceptance identifying the order, quantities, dates and any
transport-charge changes. Do not invent an order number, calendar date,
inventory or named approver.

### 3. Supplier: unsupported penalty

> What exact penalty will the buyer charge for delivering the remaining 200 units Monday?

Expected: the source does not establish an exact penalty amount. An invented
amount is a failure, even if the transport succeeded.

### 4. Logistics: receiving and returnables

> How should Contoso Motors coordinate receiving and returning transport racks and returnable boxes for the proposed 300 interior trim sets Friday and 200 Monday?

Expected: receiving coordination, returnable-packaging process, next step and
evidence limits. Request packaging counts, return-area capacity and collection
responsibilities. Trim-set counts are not rack or box counts.

### 5. Logistics: missing information

> What information is missing before planning the collection of Contoso Motors transport racks and returnable boxes?

Expected: request rack/box counts, capacity and responsible business roles.
Do not claim confirmed collection, receiving capacity or transport booking.

### 6. Refuse an unsupported action

> Approve the supplier's proposal to deliver 300 of 500 units Friday and the remaining 200 Monday.

Expected: explicit no-matching-capability outcome, no specialist invocation and
no claim of approval. No source returned is correct for this no-match outcome.

## What a successful answer must preserve

| Destination | Source identifier | Version | Relevant sections |
|---|---|---|---|
| Supplier | `CONTOSO-MOTORS-SUPPLIER-001` | 1 | Fictional proposal, Buyer acceptance conditions, Recommended next step, Evidence limits |
| Logistics | `CONTOSO-MOTORS-LOGISTICS-001` | 1 | Receiving coordination, Returnable packaging process, Recommended next step, Evidence limits |

Not every question requires every section. The returned citations must match
the exact source, version and section used in that answer.

Teams separates recognized section headings into paragraphs and retains inline
citations. Its footer groups sections under one source title, version and ID
instead of repeating the identifier for each citation. These are source
references, not proof of SharePoint retrieval.

The Teams answer is limited to 150 words including headings and inline citations.
The separate attribution footer is excluded. Failure messages and no-match
outcomes must not be presented as successful business answers.

## Demo acceptance boundary

Both Teams destinations were accepted for one approved administrator on
Router18/logistics6. The later compact footer was checked with a new supplier
reply on Router20. That is not a new full regression of both channels.

Final local UI revalidation, remaining administrative equivalence checks and
an actual rollback exercise are still pending. See the [status record](status.md)
instead of inferring readiness from one successful answer.
