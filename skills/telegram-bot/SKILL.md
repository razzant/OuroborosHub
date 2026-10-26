---
name: telegram-bot
description: Durable Telegram transport for generic Ouroboros presences, with exact actor and conversation
  provenance, media staging, and provider receipts.
version: 0.6.0
type: extension
plugin_api: '2.0'
runtime: python3
entry: plugin.py
permissions:
- net
- fs
- read_settings
- supervised_task
- route
- widget
- tool
- presence
env_from_settings:
- TELEGRAM_PUBLIC_BOT_TOKEN
timeout_sec: 60
when_to_use: The owner wants a reviewed Telegram bot transport for one or more external presence
  places, separate from the native owner-control Telegram bridge.
ui_tab:
  tab_id: transport
  title: Telegram Transport
  icon: message
  render:
    kind: declarative
    schema_version: 1
    components:
    - type: form
      route: settings/save
      method: POST
      submit_label: Save transport settings
      fields:
      - name: management_group_id
        label: Management group ID
        type: text
        placeholder: Optional numeric Telegram group ID
        help: Marks this room in event facts. Work instructions stay in place notes; no
          owner permissions are granted.
    - type: poll
      route: status
      method: GET
      target: status
      interval_ms: 5000
      auto_start: true
      max_ticks: 100
      label: Refresh transport status
    - type: callout
      path: runtime_state
      tone: info
      target: status
    - type: group
      title: Provider custody
      layout: grid
      columns: 4
      components:
      - type: metric
        label: Inbox waiting
        path: inbox_waiting
        target: status
      - type: metric
        label: Inbox leased
        path: inbox_leased
        target: status
      - type: metric
        label: Submitted
        path: inbox_submitted
        target: status
      - type: metric
        label: Inbox failed
        path: inbox_failed
        target: status
      - type: metric
        label: Outbox waiting
        path: outbox_waiting
        target: status
      - type: metric
        label: Delivered
        path: outbox_delivered
        target: status
      - type: metric
        label: Outbox failed
        path: outbox_failed
        target: status
      - type: metric
        label: Outbox uncertain
        path: outbox_uncertain
        target: status
      - type: metric
        label: Telegram offset
        path: telegram_offset
        target: status
      target: status
    - type: kv
      fields:
      - label: Bot
        path: bot_label
      - label: Last provider event
        path: last_event_at
      - label: Last delivery
        path: last_delivery_at
      - label: Last error
        path: last_error
      target: status
tools:
- name: telegram_send
  effect: message
  target: chat_id
  conversation: chat_id
  thread: topic_id
  description: Queue a proactive Telegram text, photo, or document for durable delivery.
- name: telegram_moderate
  effect: write
  target: chat_id
  conversation: chat_id
  description: Queue exact-message deletion or member restriction, ban, or unban with durable receipts.
- name: telegram_operation
  effect: write
  target: chat_id
  conversation: chat_id
  thread: topic_id
  description: Queue an own-message edit or reaction through the Telegram Bot API with durable receipts.
- name: telegram_receipt
  effect: read
  target: []
  description: Inspect delivery state and provider receipts for a queued Telegram operation.
icon: ui/icon.svg
contributes:
  - id: place
    provides: place
    label: Telegram
    destination: vendor:Telegram
    scope: account
    source: {route: status, refresh_s: 15}
    icon: ui/icon.svg
    wakes: [mention, reply_to_me]
---

# Telegram Bot Presence Transport

This extension owns Telegram provider custody. It is deliberately separate from
the bundled owner-control Telegram bridge.

It provides:

- exact Telegram actor, chat, topic, message, reply, and attachment provenance;
- a SQLite inbox/outbox with stable event ids, leases, deduplication, bounded
  delivery retries, terminal failure state, and an offset committed in the same
  transaction as each accepted update; leases left by a stopped runtime return
  to their queues when the runtime starts again, with attempts unchanged (this
  assumes one runtime per state dir: a reload that overlaps a still-running
  runtime can resend a row, as a lease expiry would, only sooner);
- inbound photo/document staging through Telegram `getFile`;
- outbound text, photo, and document provider helpers with durable receipts;
- a namespaced `telegram_send` tool for proactive text/photo/document delivery
  to exact numeric chat, topic, and message ids;
- a bounded poller and fixed worker set; and
- an operational Widget showing provider and custody state.

The payload does not call the owner `/chat/inject` path, allocate synthetic
internal chats, interpret owner commands, or create prompt envelopes. It uses
only the reviewed `presence` Host permission. Connect a Telegram place with the
bot account ID. On hosts advertising `places_version >= 1`, groups and channels
are captured without a binding ID; private bot chats are excluded. Suggested
wakes are explicit mentions and replies to the bot. Typed facts come from
Telegram entities and reply identities, not text interpretation. Other bots
carry `from_bot`; own bot echoes are ignored.

Older hosts retain the previous event filtering and receive the already-saved
`binding_id` from local settings. Deferred references, offsets, staged files and
delivery identities survive the upgrade. Before each outgoing message part or
mutation the worker re-reads the place. Silent/Paused or an unavailable lookup
holds the queued bytes and receipts without consuming retries; Active resumes.

Supported v1 Telegram content is text/caption, photos, documents, edited
messages, and message-reaction provider facts. Voice/audio bytes are still
outside the interpreted content contract: this transport makes no
speech-to-text claim. Reaction updates require the bot to be an administrator
in the chat and an explicit Bot API `allowed_updates` subscription; Telegram
does not deliver bot-authored reactions as user reactions.

## Retained input and late results

A typed Host `disposition: retained` means the original event is saved but has
no canonical final result to deliver. The transport stores the complete refusal receipt
with its source event, releases that conversation's input queue, and observes
the original `turn_ref` and any real child `work_ref` independently. It never
resubmits that retained event. A child finishing does not erase the original
uncertainty or stop observation of its late selected output.

A work view answers `pending` while the turn's executor can still change it and
is terminal once nothing can. A failed task may still carry a selected answer;
lifecycle status alone does not suppress speech. A turn that ended without a
canonical answer reads `failed` (`cancelled` when recorded so) with
`outcome: "silent"`: nothing further to deliver from that reference. Its reason
facts (`code`, `recorded_status`, `uncertainty`) prove neither delivery nor the
absence of external effects, and observation starts no new generation. A real
child keeps its own reference in `child_work_ref`, which any view may name, and
is observed on its own. The transport stores it in the same commit that records
the poll that named it, releasing a pending reference or settling a terminal
one. Older Hosts may still answer `unresolved`, which stays observable.
Canonical selected outputs keep their source and delivery identity across
repeated polls and restarts; a retained receipt itself is never sent as speech.
This does not claim exactly-once provider delivery after ambiguous network
acceptance.
`rejected` ends an event-specific refusal; before-admission `retry` and `blocked`
retain retry custody, including after account or binding repair. Missing or unknown typed
dispositions stay retryable, even on HTTP 403/404. Full queue recovery requires
both an updated Host and this transport; older Hosts may still return a
retry-only refusal. Existing completed delivery receipts remain unchanged.

The work table migrates transactionally from one row per event to one row per
work reference, preserving existing work and its outbox identities. The original
refusal remains in the inbox receipt after any child finishes.

## Delivery history

Hosts advertising `presence_delivery_version=1` receive delivery observations
through `/presence/delivery`. Each confirmed text chunk or media send freezes its
actual transmitted text/format, exact destination, provider receipt and producer
origin in the existing outbox before reporting. Captionless files retain their
filename/provider media descriptors. A partial failure preserves confirmed parts
and reports the remainder as failed or uncertain rather than delivered speech.

History acknowledgements have their own backoff beside the provider receipt.
The existing outbound worker owns a concurrent report attempt, so a slow or
failed history callback does not hold later sends or resend successful parts.
Restart reuses the same immutable report. Queueing alone creates no spoken row.
Automatic turns keep the Host's actual echoed reporting mode through deferred
work; legacy rows use mode 0 and are not retroactively imported. Old Hosts keep
normal sending with an explicit unsupported/unavailable reporting status in
transport status and delivery receipts. Origin metadata never grants authority.

Telegram acceptance without a stored receipt can still lead to a duplicate on
the existing provider retry path; reporting does not claim provider exactly-once.

## Incoming context

Each event preserves the current sender's provider identity, the chat title and
topic ID, and the message's text or caption. A conversation is one chat, or one
topic when Telegram marks the message with `is_topic_message`; an edit of a topic
message stays in that topic. Telegram also sets `message_thread_id` on replies
outside topics (ordinary groups, a forum's General topic, private chats); that
value names a reply chain, not a topic, so the reply stays in the chat's
conversation (`topic_id` null, replies sent without a topic) and the raw value is
kept as `message.reply_chain_id`. Reaction updates carry no topic, so reactions
always use the chat's conversation, also in forums. Album members keep
Telegram's `media_group_id`.
Text and caption entities retain their original UTF-16 offsets, hidden link
URLs, and text-mention user facts.
When Telegram supplies a replied-to message, the event includes its text or
caption, original author/chat/message IDs, date, entities, and photo/document
descriptors. Selected quotes and forwarding origins remain separate source
facts; a forwarded author does not replace the current sender.

Edits retain their original message id with `message.event_kind` set to
`edited_message`. Reaction updates retain the target message id and the old/new
reaction arrays, with `event_kind` set to `message_reaction` and no synthetic
message text. These are observations for model judgment, not automatic replies.

Reply context comes only from that incoming update. It does not fetch history,
follow nested reply chains, or download the replied-to message's media. Direct
photo/document attachments continue through the existing staging path.

## Configured room and selected actions

The optional `management_group_id` in the transport settings is a numeric group
ID, not a secret. Messages in that group carry
`conversation.configured_room="management_group"`; the place notes
interprets this room fact. It grants no owner commands or system-settings access,
and the adapter does not copy a member roster or classify message intent. For
all human messages to reach the bot, disable bot privacy mode in BotFather or
provide the bot with the appropriate group visibility.

The selected `telegram_moderate` tool queues deletion of an exact message,
restriction/ban of an exact member, or unban. The model supplies the decision and
provider IDs; Telegram enforces the bot's actual rights. Restriction permissions
are ordinary Telegram `ChatPermissions`, so restrictions can also be lifted.
Reuse `request_id` when retrying the same action, and inspect `telegram_receipt`
with `operation="moderate"` (or `"send"`) for the provider outcome. A queue receipt
is not proof of provider delivery.

`telegram_operation` extends the same outbox for the two provider operations
that update an own message: `editMessageText` and `setMessageReaction`. It
accepts Bot API-shaped parameters and retains a new operation identity plus an
optional `original_delivery_id` source reference, so an edit/reaction never
rewrites the original outbound delivery history. For edits, omit `parse_mode`
for plain text or choose `HTML` / `MarkdownV2` for provider formatting. Inspect its result with
`telegram_receipt(operation="operation", request_id=...)`. The original
delivery reference stays in provider message facts; Host origin retains its
standard `kind`/task/source-event provenance. A reaction update contains
only Telegram's reaction objects; this skill does not invent a forum topic ID
when the provider's reaction update omits one. Supply a known local topic ID
only for `editMessageText` when the caller has it. Provider rights, reaction
administration requirements, and errors remain authoritative; a queued row is
not a delivery claim.
An ambiguous transport result for an edit/reaction is terminally `uncertain` in
its own operation receipt, without an automatic second provider call. A
confirmed operation is checkpointed before its row completes, so restart
finishes from that receipt rather than repeating the provider mutation.

The outbox runs independently while a Presence turn is reasoning. The model may
call `telegram_send` for an intermediate acknowledgement, continue working, and
later return an answer or silence. There is no automatic acknowledgement policy.

Transport retries deduplicate known events and persisted receipts. A network
interruption after Telegram accepts a request but before the receipt is stored
can still cause a repeated send; no provider-side exactly-once guarantee is
claimed. Telegram's deletion window, bot rights, file limits and member-operation
rules remain provider constraints: https://core.telegram.org/bots/api.

## Message formatting

Text and media captions use the native Telegram skill's reviewed Markdown-to-HTML
presentation helpers included in this payload. Bold, links, lists, code and
bounded monospace tables render without exposing Markdown markers. Set
`markdown: false` on `telegram_send` when punctuation or XML must stay literal.
Automatic Presence replies use Markdown; for an explicitly literal reply, use
`telegram_send` with that flag and finish with `tool_delivered`.

Use `text` and `caption` only for the content people should see. Other arguments
are separate JSON fields, for example:

```json
{"chat_id":"-10042","kind":"document","file_path":"/selected/workspace/report.pdf","caption":"**Report** with [sources](https://example.org)","request_id":"report-v1","markdown":true}
```

The transport does not remove XML-looking text or infer tool arguments from
captions. A caption above 1024 visible UTF-16 units returns an explicit error:
shorten it and send the remainder separately. It is never silently truncated.

New text deliveries freeze their rendered, block-aware chunks in the durable
outbox. Retries preserve exact chunk boundaries, thread/reply IDs and confirmed
receipts. Existing queued rows keep literal presentation; an already partially
delivered legacy row also keeps its original chunk boundaries. Only an explicit
Telegram 400 rejection of an HTML send permits an immediate plain-text fallback;
a timeout or unknown network response never triggers that fallback. The ordinary
bounded outbox retry policy and its documented unknown-acceptance duplicate risk
remain unchanged. See `FORMATTER_PROVENANCE.md` for the included helper source.

## Upgrading to 0.5.0

- The payload content hash changes, so each install needs a fresh owner review
  and grant before the new version runs.
- Updates already queued before the upgrade keep their old conversation keys; a
  reply chain that was keyed by its `message_thread_id` is a separate history
  once, and new updates use the chat's key.
- A binding pinned to an exact thread that was created for a reply chain no
  longer matches: those events now carry an empty thread and the Host refuses
  them (403). Re-check such bindings and use the chat-level conversation.
- Reactions in forums stay on the chat's conversation (`:0`), not the topic.
