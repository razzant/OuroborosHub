# Host contract and disclosed choices

## Required host facts

Intake starts only when `GET /identity` advertises `places_version >= 1`, `presence_explicit_completion: 1`,
`presence_identity: 1`, `presence_delivery_version: 1`, `presence_work_output_version: 1` and `2` in
`presence_continuation_versions`. Otherwise status shows `host_capability_gap` and the tools keep working.
The watcher runs only while the helpdesk-wide place exists (`GET /presence/place` with an empty conversation).

## Identity

- `provider: freshdesk`; `account_id`: the canonical host (`example.freshdesk.com`).
- `conversation_id = thread_id = <ticket number>`; key `freshdesk:<host>:<id>:<id>`. A ticket is one exchange, and
  core lets an untagged follow-up wake the author working on it only inside a thread
  (`place_intake._followup_in_scope`). The author guide says a flow without topics leaves `thread_id` empty; the
  ticket number is a constant container, never a thread per message (the email connector does the same with its
  conversation root). Every message tool declares `conversation` and `thread` as `ticket_id`, and receipts carry
  the same thread, so sends match the event's room.
- `identity.sends_as = {id: <agent id>, label: "<name> <email>", kind: "user"}`. A key for another agent of the
  same helpdesk is accepted; journal rows keep the agent they were sent as. A different helpdesk is refused while
  this skill holds state for the first.

## Events

| Observation | text | facts |
| --- | --- | --- |
| ticket created after the connection | subject and description | `any_message`, `new_issue` (+ `assigned`) |
| new reply or note (not sent by this connector) | its text | `any_message` (+ `my_thread` if this agent wrote earlier in the ticket, `mention` if a note's notify list holds its email, `assigned`) |
| note edited | the new text, `message.edited_at` | `any_message` (+ `mention` only if newly notified) |
| property change | empty; `message.non_text` names the change | `[]`, or `assigned` when the responder becomes this agent |
| older ticket first seen assigned to this agent | empty; "first seen now, assignment time unknown" | `assigned` |

Contact authors are `authenticated: false` (an email sender is not attested); an unknown editor is
`{platform: freshdesk, attribution: unknown}`. `reply_to_me`, `direct_message`, `group_dm` and `from_bot` are never
sent: Freshdesk has no reply relation, no direct messages and no author-kind marker. The connection time is
Freshdesk's `Date` header at connect; a message written in that very second counts as new rather than lost.

## Sending

- Doors: `freshdesk_reply` and `freshdesk_note` target only `[account_id, ticket_id]`. A note that notifies agents
  declares them through `sends: {when: {audience: [notify]}}`; an outbound email declares its recipients and no
  ticket through `sends: {when: {op: [outbound]}}`.
- Each outward call checks `GET /presence/place` immediately before its HTTP call (token from
  `api.get_skill_token()`); Silent or Paused holds it, no place lets it run as an ordinary tool, an unreachable host
  refuses it. Sends made with no place are not reported (there is no room for them).
- Readback after a call Freshdesk did not answer proves delivery only when exactly one entry not claimed by another
  journal row matches author, kind, visibility, attachment count and words, made after the attempt (five minutes of
  clock skew allowed). Otherwise the call stays `uncertain` and lists the candidates.
- Receipts go to `/presence/delivery` once per state (`part_id` `0`, the ticket as conversation and thread).
  Released outputs (`output_ref`) are posted once as private notes, never as emails; one without an `output_ref`
  is a visible gap, not a send.

## Not in this version

Webhooks (they need an administrator's automation rule and a public address), several helpdesks per
installation, history import, ticket summaries, wake choices for property changes other than assignment, and the
editor of a property change (Freshdesk does not name it).
