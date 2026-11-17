# Host integration contract

Exact agreed seam retained, 2026-10-23. Independent host consumer qualification
is required. No new connector endpoint, scheduler or Main transport.

1. `GET /identity` must advertise `places_version >= 1`,
   `presence_continuation_version: 1`, `presence_delivery_version: 1`, which an old
   host also has, plus these exact general identity fields:
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
   negotiated versions set to 1. `review_requested` and `assigned` must be accepted
   as facts/wake choices. Manifest suggestions name `new_issue`, `new_pull_request`,
   `mention`, `my_thread`, `review_requested` and `assigned`; the declared
   `signal.new_discussion` (sent only under `presence_wake_signals: 1`) and
   `any_message` stay owner choices. Discussion is never new_issue. Relation facts
   (`new_issue`, `new_pull_request`, `my_thread`, `reply_to_me`) belong to new words
   only. Every content event names its object in `message.message_id` (GitHub's node
   id), the watched accounts its words mention in `message.mentions` (`{id, name,
   is_self}`; parser spans stay in `fact_evidence.mention`) and its thread in
   `conversation.thread_history` (the opening, then the newest messages within 30,000
   characters, in provider-time order; every raw row stays in connector custody,
   counted on the wire).
   A pull request is one object, observed through the Issues feed (its merge from
   that row's `pull_request.merged_at`).
   With `/identity.presence_change_context: 1` (0.2.0) a revision (edit, merge,
   close, reopen, draft flip, assignment, review request), a review-request list and
   a first sight of an older updated object are changes: only newly added addressed
   facts (no `any_message`), a `state_change` item in `message.non_text`, empty `text`
   unless the words changed, and as `actor` the one account GitHub's own records
   (`userContentEdits.editor`, timeline `actor`) name for every observed change in
   that interval, each rename of a title among them. Revised words keep `edited_at`
   from that record and `forward_origin` naming their first author (an explicitly
   unknown one when GitHub names none) unless its records prove them that account's
   alone: it made every change of the interval, its complete edit record names no
   other editor, and no earlier observed revision put another or an unestablished
   hand on the text or is still being read. Words their first author edits after
   someone else did carry an explicitly unknown origin and the reason in
   `fact_evidence.word_origin`.
   Several accounts, a missing record or an unknown number leave the actor unknown
   with an `actor` coverage gap; a known Bot keeps `is_bot`. Without the capability
   every observation keeps the 0.1.6 form: `any_message`, repeated words and an
   unknown actor for each revision. In the selected repositories, on every host,
   words first observed after they were edited stay new words under their author
   (never a change), but carry `forward_origin` with an explicitly unknown origin
   unless GitHub's edit record names that author alone: `fact_evidence.word_origin`
   gives the recorded other editors, or why the record could not say (then also a
   `revision` coverage gap). Outside them, whether such words address a watched
   account depends on who GitHub's records prove added the mention (below).
   A Discussion's title has no record: first observed after anything happened to it
   (its update time is not its creation time), it carries that unknown origin as
   `title_record_unavailable`, whatever GitHub says of its text.
   A Bot's words and those of an author GitHub gives no number are not judged.
   Events by the account this connector sends as (other than its receipted sends)
   are reported; the host knows them from `identity.sends_as` and does not wake on
   them unless it is the owner's own account.
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

A new event whose attention facts or word origin depend on a transiently failed
read (new words' author, the edit record of words first observed edited, a reply's
thread history when its retained opening does not prove the thread, a change's
actor) stays `enriching`: out of dispatch, with later changes of
its conversation behind it (new words are never held), while the existing job queue
retries that read up to six attempts or 30 minutes; a record GitHub keeps but has
not yet returned gets three. What an attempt did read stays with the waiting event,
under the time it was really read: only reads still open are tried again, and a
later failure takes nothing back. It is then offered once, complete or with the
failure as an explicit gap; an offered event is never rewritten. An event whose form needs
a capability the host no longer advertises (a change without `any_message`, a
declared signal) waits `blocked` with gap `host_capability_missing`, unchanged.

Public outside-selected Notifications use existing place admission, with a real
notification source, verified public subject and unknown service attribution.
Under the change capability a reader's notification update is that reader's
evidence: no words or facts, a background `state_change` by the notification
service, its previous update named in `fact_evidence` (never as
`previous_source_event_id`; its action stays `observed`) with the events it proves.
Each addressed GitHub message or action inside the window that reader observed goes
out first as its own event, keyed by GitHub's node id (comment or timeline event) in
its thread, so another reader's notification of it adds evidence, never a second
event: new words naming a watched account (their author, unidentified or not, as
new words, unless another or an unidentified account also edited them inside that
window: then who added the mention is not recorded, and the item is an unattributed
change); words an edit added (the words before it as last observed of that message,
also after an observed edit removed a mention, else from the reader's previous
observation; the edit's own record: the editor, else unknown); an
assignment or review request (its timeline event and actor). A failed or
incomplete read delays only what depends on it: bounded rereads, then explicit
gaps. What is retained of a message only moves forward in observation time. A
read of a thread marks, in the same transaction, every notification still waiting
on an older copy of it; such a notification is then proven from that newest read
as the notification that made it keeps it (no read of its own, unless a read of
the thread is itself still open), never from its superseded copy, on any attempt
or at release. Its own observation keeps the copy it read. Without a kept newer
read it reads the thread again, and proves nothing until that succeeds: its open
reads end as gaps. Whenever it is proven from a later read than its copy, what
only that copy showed (and was still being read for) is a
`mention_withdrawn_unreported` gap, kept until it goes out, never an event; what
it had read of a message the later read shows unchanged stays read.
Without that capability they carry `any_message` (never a fabricated current
mention). An owner-created Silent
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
