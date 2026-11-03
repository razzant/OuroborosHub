# Host integration contract

Exact agreed seam retained, 2026-10-23. Independent host consumer qualification
is required. No new connector endpoint, scheduler or Main transport.

1. `GET /identity` must advertise `places_version >= 1`,
   `presence_continuation_version: 1`, `presence_delivery_version: 1`.
   Those versions already exist on the old host and are **insufficient** for
   this full-watch scope. Require these exact additional general identity fields:
   - `presence_main_contact_version: 1`: substantive shared Main contact and
     owner-answer return through the host-origin/canonical-quiz path.
   - `presence_work_output_version: 1`: real output identities and retained/late
     child projections, including terminal failure repair.
   - `presence_wake_facts`: the current complete supported fact-string list,
     including `any_message`, `from_bot`, `mention`, `my_thread`, `reply_to_me`,
     `new_issue`, `new_pull_request`, `review_requested` and `assigned`.
   Absence/malformed advertisement means unknown, never compatibility inferred
   from a host version. The connector records `host_capability_gap` and holds
   startup; no transport intake or send starts against the narrower contract.
   Provider-only setup probes do not claim host readiness. No place is made
   Active by this payload, before or after negotiation.
2. `POST /presence/turn` carries the closed event schema plus explicit `facts`,
   `staged_files` (empty unless a held-context source is attached), and both
   negotiated versions set to 1. `review_requested`
   and `assigned` must be accepted as facts/wake choices by the host upgrade.
   Manifest suggestions name `new_issue`, `new_pull_request`, `mention`,
   `my_thread`, `review_requested` and `assigned`; `any_message` stays an owner
   choice. Discussion is never new_issue.
3. Reply projections must retain actual `output_ref`, including retained original
   and late child outputs. `work_ref` identifies the polled work;
   `child_work_ref` exposes a late child even on terminal failure. Released
   `outputs` remain independently observable. No local work-ref/text fallback.
4. `/presence/work/{ref}` is polled independently for original, continuation and
   child refs. Nonempty output lacking identity stays a visible protocol gap.
   New arrivals may POST while an earlier HTTP request awaits a model; use the
   existing current-author mail/final-drain lock, not a second mailbox.
5. `retry` retains the exact event (including allowance/capacity); `retained`
   releases input custody and keeps every ref; `blocked` retains configuration
   debt; `rejected` terminates visibly. Heard silent is terminal. No TTL.
6. `GET /presence/place` with exact nonempty account/repo/thread precedes every
   physical mutation. Its authenticated lookup checks current skill authority
   and place state; it cannot make a later GitHub request atomic with a state
   change. Actual late effects remain receipts.
7. Immutable physical receipts go to `POST /presence/delivery` after local
   persistence. Provider effect and host report ACK have separate custody.
8. `send_user_message(destination='main', request_id=...)` and
   `escalate(..., request_id=...)` carry substantive reports/questions through
   host origin and the canonical quiz. A retry with the same request ID must
   preserve all fields; conflicting reuse is refused. The host takes the source
   link from `event.message.url`/`permalink`, then `conversation.url`; this
   connector supplies observed provider message/thread URLs. `/notify` stays
   unchanged. Authenticated owner answers are authority within scope; external
   colleagues' requests are not.
9. Existing `start_task`/`schedule_followup(notify=false)` preserve the carrier.
   Scheduled future roots explicitly call this connector's tool.

The message tool resolves `source_event_id` from the local immutable inbox and
checks the supplied account/repo/thread against it. Automatic output uses its
original event. Both enter one outbox. Close declares `effect: write` and uses
the same path, with a confirmed explanatory comment as a dependency. No built-in
write bypass. Neither a task reference nor a source ID grants external authority.

Integration and live Main/followup qualification remain the integrating host's
responsibility; schema validation alone does not qualify those flows.

## Repair-stage source-bound observations

No identity version or endpoint was invented in this repair. Existing
`POST /presence/work/{ref}` carries a complete `transport_queue` snapshot;
`events` contains only actual pending provider observations. Additional
`held_intents` contains dated connector custody facts with original delivery ID,
source event ID, immutable intent, current history and fingerprint. The checked
real `validate_transport_queue` retains this open-schema field, and
`record_transport_queue` / `transport_queue_observation` preserve/read it on the
same task without changing terminal status or output identity. A wrong source
ID is rejected. Observations include all held intents in that exact conversation,
including those from older source events.

Before first dispatch of a genuine event, the connector freezes its wire payload
separately from the immutable provider inbox. When that conversation has held
intents, the event carries `conversation.transport_queue` and a
`github-held-intents-untrusted-context.json` attachment under the connector's
state directory. The focus renderer does not inline conversation JSON; its
existing attachment renderer supplies the source's `read_file` address in the
assembled input. The file contains the complete snapshot with unknown attribution
and context-only provenance. Reading it supplies the original source/delivery
IDs, intent, changed history and fingerprint without rewriting provider text.
The initial input exposes the source; it does not prove the model read it or made
a decision. Retries retain the same payload and attachment bytes, including an
initial decision to attach nothing. A missing local file can be reconstructed
from the frozen snapshot; a conflicting or unwritable file holds intake.

Queue recording does not wake a terminal author, and no such wake is required.
The next genuine event provides discovery; deliberately scheduled future work
uses the existing followup and explicit tool path. A queue ACK resolves the
work-observation gap but leaves the intent held until an authored decision.
No fake provider event, owner-chat injection or second model scheduler is used.

Public outside-selected Notifications use existing place admission, with a real
notification source, verified public subject, unknown service attribution and
`any_message` (never a fabricated current mention). An owner-created Silent
account-wide place provides the existing admission path for Main contact; the
connector does not auto-enroll a place or grant writes. Use a blank stored
conversation ID for that scope, not a literal `*`. Actual admission under a
Silent fixture was checked; Main delivery and question/answer consumers still
need joint qualification. Discovery events cannot enter the outbox.

The real delivery schema's immutable key includes **state**. Reconciliation
therefore retains an earlier `uncertain` report and later `delivered` report
under the same physical part index. No nonnumeric part IDs or revised reports
are sent. The local report queue uses distinct keys for those immutable states.

Optional `/identity.presence_identity: 1` enables the same credential `identity`
on the connector's status route and each `/presence/turn` event. The sending
entry uses the verified numeric PAT user id (`user`) or App bot user id (`bot`),
as a string matching incoming `platform_actor_id`; its login is the label. An
unresolved App bot user has no sending declaration. A distinct verified Notifications
reader uses its numeric user id under `reads_as`. Neither identity nor `actor.authenticated`
is required from older hosts. GitHub's existing platform actor attestation stays
unchanged; this connector does not add an `authenticated` actor field.
