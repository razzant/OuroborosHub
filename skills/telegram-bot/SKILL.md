---
name: telegram-bot
description: Durable Telegram transport for generic Ouroboros presences, with exact actor and conversation
  provenance, media staging, and provider receipts.
version: 0.10.0
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
when_to_use: The owner wants Ouroboros in Telegram as a labelled bot (private chats people open with it,
  groups and channels it is added to), separate from the native owner-control Telegram bridge. For a
  dedicated ordinary account use the telegram-user-session connector instead.
ui_tab:
  tab_id: transport
  title: Telegram Transport
  icon: message
  render:
    kind: declarative
    schema_version: 1
    components:
    - type: markdown
      text: "**Telegram bot.** Telegram labels this account as a bot. People reach it in a private
        chat they opened themselves and in groups or channels it was added to; group coverage depends
        on privacy mode and admin rights. A bot cannot start a private chat with someone who never
        opened one. The Bot API cannot read earlier chat history: only messages that arrive while it
        runs are received. A dedicated ordinary account is the separate Telegram participant connector."
    - type: form
      route: settings/save
      method: POST
      submit_label: Save transport settings
      fields:
      - name: management_group_id
        label: Management group ID
        type: text
        placeholder: Optional numeric Telegram group ID
        help: Marks this room in event facts. Work instructions stay in the place notes;
          no owner permissions are granted.
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
        label: Outbox held
        path: outbox_held
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
      - label: Reactions in Host context
        path: reaction_context
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
  description: Queue an edit of this bot's own message, or set or clear its reaction on a reachable message, with durable receipts.
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
    settings: transport
    facts:
      - any_message
      - direct_message
      - mention
      - reply_to_me
      - from_bot
      - {id: signal.words, label: "Words you chose appear (connector setting)"}
    wakes: [direct_message, mention, reply_to_me]
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
bot account ID, or one group at a time (see
[Connecting groups](#connecting-groups-090)). On hosts advertising `places_version >= 1`, private chats,
groups and channels are captured without a binding ID. A private chat carries
the `direct_message` fact; the person opened that chat with the bot, and the
bot cannot open one first (Telegram refuses it). Suggested wakes are direct
messages, explicit mentions and replies to the bot. Provider facts come from
Telegram entities, chat type and reply identities; the optional `signal.words`
comes from the connector's word setting. Other bots carry `from_bot`; own bot
echoes are ignored. Owner identity is the numeric Telegram ID the owner confirms
in the Places sheet; a private chat with this bot grants no owner authority by
itself. The owner-control chat remains the built-in Telegram bridge with its own
bot token.

The Bot API has no method for reading earlier chat history. This connector
receives only updates delivered while it runs (Telegram keeps undelivered
updates for about a day) and offers no history import. A dedicated ordinary
account (the `telegram-user-session` connector) is the option that can read
history the account can see.

Older hosts retain the previous event filtering and receive the already-saved
`binding_id` from local settings. Deferred references, offsets, staged files and
delivery identities survive the upgrade. Before each outgoing message part or
mutation the worker re-reads the place, including a private chat's place.
Silent/Paused or an unavailable lookup holds the queued bytes and receipts
without consuming retries; Active resumes.

Supported v1 Telegram content is text/caption, photos, documents, edited
messages, and message-reaction provider facts. Voice/audio bytes are still
outside the interpreted content contract: this transport makes no
speech-to-text claim. Reaction updates require the bot to be an administrator
in the chat and an explicit Bot API `allowed_updates` subscription; Telegram
does not deliver bot-authored reactions as user reactions. See
[Reactions](#reactions-0100).

## Connecting groups (0.9.0)

Each group or supergroup the bot has been seen in is offered by name when you
connect a Telegram place, so groups can be connected one at a time:

1. Add the bot to the group. Telegram reports the addition to the bot
   (`my_chat_member`, which needs no admin right); any message the bot receives
   there also adds the group.
2. In Ouroboros open Consciousness, choose **Connect a place**, then **Telegram**.
   Beside **Whole account**, each group is listed as its title and numeric ID,
   for example `Book club (-1001234567890)`.
3. Choose **Connect** on that group. The new place starts Silent: it hears and
   thinks there and sends nothing.
4. Make the place Active on its sheet when the bot may speak in that group.

The status route returns the list as `connect: [{account_id, conversation_id,
label}]`: one row per group whatever its topics, ordered by title, without a
limit. Private chats and channels are not listed. Only the bot that `getMe`
confirmed for the token configured now has a list. When the token is missing,
when `getMe` has not confirmed the current token yet or refused it, or when the
transport is not running, `connect` is `null`. The status names a stopped bot,
missing token or unconfirmed identity; `runtime_state` and `last_error` retain
transport errors instead of promising that waiting will fix them.
Connect then reports the list as unavailable. A confirmed bot seen in no group
yet has `connect: []`. Reading the list makes no Telegram call.

The list records what the bot has observed and does not check current membership.

- Removing the bot (its status becomes `left` or `kicked`, or a service message
  names it as the member who left) takes the group off the list, and adding it
  again brings it back. A rename updates the title. Telegram's update order
  decides, so an older message never undoes a later removal, rename or migration.
- A group upgraded to a supergroup gets a new ID. The old ID leaves the list,
  and the supergroup appears once one of its own updates, which carry its type
  and title, arrives. A place connected to the old ID is not moved: connect the
  supergroup as a new place.
- Additions, removals, renames and migrations only update this list and the
  offset; they never become events, turns or places. Nothing is connected
  automatically, and the list never asks Telegram about members.
- Messages from a group that has no place yet stay queued in custody while the
  Host refuses them, and reach Ouroboros after you connect the group. The retry
  back-off is unchanged, so the first of them can wait up to about four minutes.
- A group from which the bot captured no message before 0.9.0 appears after the
  bot's next update there, for example a message that mentions the bot.

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

## Timely conversations (0.8.0)

When `/identity` lists `2` in `presence_continuation_versions`, each turn request
carries `continuation_version: 2`. The scalar `presence_continuation_version`
never negotiates it. Other Hosts keep the earlier behaviour: no continuation
version is sent, and each request waits up to 30 minutes for its terminal answer
while the conversation's later events wait behind it.

With version 2:

- An event that starts an author is answered once that author takes
  conversation mail: `continuing`, with `continuation_ref` equal to `turn_ref`.
  This is custody, not speech. The reference is stored in the same commit that
  marks the event submitted, the conversation's input queue moves on, and the
  existing work worker polls `/presence/work/{continuation_ref}`. A `continuing`
  answer without that real reference is not custody: the event stays queued and
  is retried, and no reference is invented.
- The conversation's next events reach the Host only after that answer. A burst
  received at startup and corrections sent while the author reasons therefore
  join that author as context. They answer `completed`/`silent`: acknowledged,
  with nothing to send. Any reply comes through the author's poll.
- A busy admission, `presence_capacity_full` with `field: conversation` or
  `capacity`, admitted nothing. The same event stays first in its conversation,
  keeps its staged files and retries after the Host's `retry_after`, honoured
  as a minimum delay (at least one second). The delay does not grow and the attempt
  is not counted. The worker serves other conversations meanwhile. A
  `presence_capacity_full` refusal without that field keeps the ordinary
  retry backoff.
- Outputs the author releases are sent as its polls report them, and its
  terminal output after them. Each `output_ref` is sent at most once for its
  event, whichever reference names it: the author's continuation, a promoted
  child, the original turn or a retained reference.
- `reply_anchor: {source_event_id, message}` on an output or a terminal names
  the last input the author's model call covered. The reply's first part
  answers that `message.message_id` (Telegram `reply_parameters`) in the turn's
  chat and topic, when the anchor names an event this connector captured in that
  conversation with that message id. Otherwise, and for promoted children,
  retained references and older Hosts, which carry no anchor, speech answers the
  work's own event as before.
- A stopped turn polls as `cancelled` with `outcome: "silent"`: nothing more is
  sent, an output released before the stop stays delivered once, and the source
  event is never submitted again. A later event is new work.
- After a restart or a lost turn or poll answer, the same request is repeated.
  The Host replays its stored answer, so no second author starts and no output
  is queued twice. Telegram acceptance without a stored receipt can still repeat
  a send, and delivery reports keep their own backoff, as described below.

The transport status shows the negotiated `presence_continuation_version`
(`2`, or `0` for completed-only requests). The transport adds no scheduler,
debounce, digest, drop or forced reply: which events wake, which author hears
them and whether to speak remain the Host's decisions.

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
kept as `message.reply_chain_id`. Reaction updates carry no topic; a reaction uses
the topic of its exact retained target and otherwise the chat's conversation (see
[Reactions](#reactions-0100)). Album members keep Telegram's `media_group_id`.
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

## Reactions (0.10.0)

A reaction can be the whole social response: it shows you understood, saw or
laughed without another message. It is optional, it is not speech, a read
receipt or agreement, and an early gesture does not replace a promised result.
This connector has no emoji dictionary and no required sequence of gestures.

**What this bot can read.** Telegram sends a bot reaction updates only when it is
an administrator of the chat, and only as they happen. `message_reaction` names
one person's or chat's change. `message_reaction_count` reports anonymous totals,
grouped and delayed by up to a few minutes. Neither names the target's topic, text
or author. The Bot API has no method to read a message's current reactions or
earlier history, so what this bot knows is what it observed. Being an administrator
is the owner's choice; this connector never asks for or changes it. Delivery in
private chats is not established by Telegram's documentation.

**Incoming.** With a Host whose `/identity` says `presence_reaction_context: 1`
(strictly that integer, read when the transport meets the Host), each update
becomes an event with empty `text`, no attachments, Telegram's raw
`old_reaction`/`new_reaction` or `reaction_counts`, `message.sent_at` from its
`date`, and `event.message.reaction` (contract version 1):

- `change` is `add`, `remove` or `replace` for a person or chat, with `old` and
  `new` as that actor's values in this update, or `count` with `counts` for an
  aggregate. Values are `{"type": "emoji", "value": "👍"}`, `{"type":
  "custom_emoji", "value": "<id>"}` and `{"type": "paid", "value": ""}`. They say
  nothing about other people's reactions. An update that changes nothing is
  dropped.
- `actor.kind` is `user` for one person or bot, `chat` with
  `authenticated: false` for a chat acting as itself, and `anonymous` with
  `authenticated: false` and no id for an aggregate. Neither of the last two is a
  person or an owner candidate. `user` is not necessarily the owner.
- `target.resolution` says whether `event.thread_id` is proven. A retained exact
  account+chat+message record proves its topic. Without one, a private chat, group,
  channel or non-forum supergroup is its chat-level conversation and is `resolved`;
  a forum, or a chat whose retained messages have shown topics, is `unresolved`
  with an empty `thread_id`, so a reaction is never presumed to be in General. A
  private chat can have topics too, so its later messages outside a topic do not
  undo the topics it has shown.
- A retained record also adds `target.author` (with `is_self`, set only from the
  exact receipt of this bot's own send) and `target.excerpt`: up to 500 characters
  with `truncated: true` whenever words were cut; the full words stay in
  `message.target_lookup`. A record keeps the newest words this bot received, so its
  own later edits are not reflected.

The record is an exact-key table of messages this bot received or sent from this
version on, in the custody database. Earlier messages stay unknown.
The Host, not this connector, decides that a reaction starts no focus.

**On an older or unknown Host.** A reaction is not delivered, because that Host
cannot promise not to wake on it. It stays in custody as a terminal row counted by
`reactions_withheld` (not `inbox_failed`), and `status` shows
`reaction_context: update_required`. It is never replayed after an update, as a
live arrival or as history. A reaction row queued before this version is treated
the same on any Host. Ordinary messages are unaffected.

**Outgoing.** `telegram_operation` with `setMessageReaction` takes at most one
reaction: `{"type": "emoji", "emoji": ...}` or `{"type": "custom_emoji",
"custom_emoji_id": ...}`, and `[]` clears it. Telegram's rule is that this replaces
the bot's earlier reaction. Paid reactions are refused because bots cannot use
them. Which emoji or custom emoji a chat allows is left to Telegram's answer.

In a forum the guard needs the real topic. A retained target in a topic requires
that `topic_id`, and a wrong one is refused. A target this bot has no record of, in
a forum or in a group it has not yet observed, is refused with `forum_topic_unknown`
unless you pass a `topic_id` you know; nothing is sent, and the whole chat's permission is
not borrowed. Such an explicit topic is recorded as `resolution: unresolved`. Known
private and non-forum targets need no topic. A private chat's records cannot show
that it has no topics, so a `topic_id` for a private target with no record is kept
the same way. Once a private chat has shown a topic, such a target needs its
`topic_id` as in a forum, whatever messages outside topics came later; a private
chat that has shown none stays one conversation. Until a group's next update after this
version, its forum status is unknown, so such a target needs a `topic_id` even if
the group has no topics.

These rules read the records of the bot `getMe` last confirmed for the token
configured now. The tool often runs in a pooled worker whose own runtime never
polls, so that answer is kept in custody beside a digest of the token, never the
token itself. Until `getMe` confirms the current token (a new token, one Telegram
refused, or none), no record is read as this bot's. A group or forum
target without a `topic_id` is then refused with `forum_topic_unknown`, which names
the unconfirmed identity; nothing is sent. An explicit `topic_id` and a private
target are kept as `resolution: unresolved`. A failed `getMe` that does not refuse
the token leaves the last answer standing. This is separate from the status route,
which reports only its own running process's bot.

`telegram_receipt(operation="operation", ...)` then shows each outcome:

| Provider answer | Receipt | Meaning |
| --- | --- | --- |
| True | `delivered`, `effect: applied` | Telegram accepted this set for the requested message. It does not say what it replaced. |
| 400 or 403 | `failed`, once, with Telegram's description | The next queued item proceeds at once. |
| Lost or unreadable response, 5xx, any code not listed here such as 408 | `uncertain`, once | Never sent again, including after a restart mid-call. |
| Earlier attempt ended with no answer: expired or stopped lease, or a cancel; or 0.9.0 made it and kept no proof it did nothing ([Upgrading to 0.10.0](#upgrading-to-0100)) | `uncertain`, with no call | The next queued item proceeds at once. A row without a recorded result may have reached Telegram. |
| 401, 404, 429 | retried, bounded | Telegram answered that it did not do this now. No effect is claimed meanwhile. |

A cancel or crash just before the call also ends `uncertain`, since the row cannot
show that no call was made. The reaction is then simply not retried. The marker is
written before the call, so a failure before it (token, Host, place read, or that
write itself) proves no call: the row retries, and after five attempts ends `failed`.
A retry after a hold or a 401, 404 or 429 also sends.

A confirmed result is never asked about again. If recording it fails, the row keeps
the result and finishes later with no token, Host or call. If the store refuses even
that, this process keeps the result until the lease expires; a restart before then
loses it, and the row, whose marker is stored, ends `uncertain` rather than repeat.
The same double failure after a 429 also ends `uncertain`.
Telegram documents no error for a reaction
that is already set, so no already-present result is claimed.

`target.message_id` and `effect` describe the requested message. In an album Telegram
[sets the reaction on the first non-deleted member instead](https://core.telegram.org/bots/api#setmessagereaction)
and answers only True, so which member took it is unknown here. When the retained
record names an album, the receipt and report add
`album: {media_group_id, effective_message_id: "unknown"}`. Without `album`, nothing is
proved about albums: a target with no record may still be a member.

Silent and Paused hold the write. Just before the call the worker re-reads the place
of the queued `topic_id`, the topic the guard checked. So a reaction queued while
active is held once that topic's own place is Paused, even while the chat stays
Active. A reaction queued without a topic is checked as the chat's, as before. Later
rows for that topic wait behind the held one; General and other topics do not. A Host
advertising `presence_explicit_completion: 1` also receives `/presence/delivery`
with empty text, `message.kind: "operation"` and a typed `message.reaction`
(`operation: set`, `new`, `target`, and `effect` when it took effect, with no `old`
because the earlier value is unknown). Other Hosts receive no reaction report, since
they could count it as speech, and the receipt's `history_limitation` says so. Edits
and sends report as before.

That rule also holds at the moment a report is sent, including one frozen before this
version or for a Host that has since changed. On a Host without the capability, or
one that cannot be asked, the report stays stored unchanged and unacknowledged and is
checked again later. Its `error` starts with `Held:`, `status` counts it in
`delivery_reports_held`, and Telegram is not called again. This connector keeps a
Host's supported or unsupported `/identity` answer until it restarts, and asks again
only while the Host cannot answer. So a Host's capability change reaches a running
connector at its next restart, and until then it keeps acting on the earlier answer,
for held reports and new reports alike. On a capable Host, an
earlier version's list-shaped report is sent in the typed shape built from its own
message ID and values, with `resolution: unresolved` because it recorded no topic. One
that does not state them stays held and says so.

## Connector settings and facts (0.6.3)

The place's connector settings open the Telegram section. `words` is empty by
default; its form takes one entry per line and stores a list of plain strings.
Surrounding whitespace and empty lines are removed. Settings apply to newly
captured updates without a restart; queued events keep their captured facts.

How I decide each event fact:

- `any_message`: every supported message or edit, including media captions.
- `direct_message`: the message or edit is in a private chat, which exists only
  after that person opened it with the bot.
- `mention`: a Telegram mention entity names this bot's username or user id;
  plain text alone never manufactures a provider mention.
- `reply_to_me`: the replied-to message's sender id is this bot's id.
- `from_bot`: Telegram identifies the sender as a bot. Own bot echoes are ignored.
- `signal.words`: at least one configured plain string in `words` matches a whole
  word or phrase in the message text or caption, case-insensitively, with non-word
  boundaries on each side. Punctuation is literal, never a pattern. The settings
  form takes one entry per line and stores a list; an empty list reports no word
  signal. Sender names, chat titles and replied-to text are not searched.
  Reactions have no message text and report no word signal.

The signal's manifest label is "Words you chose appear (connector setting)";
the status payload adds nothing for it. It is sent only when the host advertises
`presence_wake_signals: 1`. It grants no owner identity, addressed status or
permission to speak. To notice a name in a group, add it to `words` and choose
this signal under "What calls it here" on that place's sheet. Suggested wakes
remain direct messages, mentions and replies. A matching event can wake a
Silent place through ordinary intake, while Silent still holds replies and
writes. Bot events still require the owner's `from_bot` wake choice; that
choice itself can wake on bots.
Every supported event is reported whether or not words match.

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

`telegram_operation` extends the same outbox for two provider operations:
`editMessageText` updates this bot's own message, and `setMessageReaction` sets or
clears this bot's reaction on any message it can reach (see
[Reactions](#reactions-0100)). It
accepts Bot API-shaped parameters and retains a new operation identity plus an
optional `original_delivery_id` source reference, so an edit/reaction never
rewrites the original outbound delivery history. For edits, omit `parse_mode`
for plain text or choose `HTML` / `MarkdownV2` for provider formatting. Inspect its result with
`telegram_receipt(operation="operation", request_id=...)`. The original
delivery reference stays in provider message facts; Host origin retains its
standard `kind`/task/source-event provenance. A known `topic_id` is a local fact
for the guard, ordering and history, and is never sent to Telegram. Provider
rights and errors remain authoritative; a queued row is not a delivery claim.
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

## Identity (0.6.2)

When `/identity` advertises `presence_identity: 1`, the status route and every
turn event declare `identity.sends_as` with the bot id and `@username` returned
by Telegram `getMe`, and `kind: bot`. This is the sending credential, separate
from the incoming message's actor. Older hosts receive no identity key.

## Upgrading to 0.7.0

- Private chats people open with the bot are now captured on Places hosts with
  the `direct_message` fact, and replies to them pass the same place check as
  groups instead of being held as unsupported. Add `direct_message` to the
  place's wakes if private chats should wake Ouroboros; existing wake lists are
  not changed by the upgrade.
- Updates already acknowledged before the upgrade are not fetched again, so
  earlier private messages are not replayed.

## Upgrading to 0.8.0

- Early answers need a Host that lists `2` in `presence_continuation_versions`;
  with any other Host the requests are unchanged.
- The custody database keeps its layout. Queued events keep their order, events
  submitted before the upgrade are never submitted again, and outputs already
  queued keep their delivery identities, so a later poll naming one of them
  through another reference sends nothing new.

## Upgrading to 0.9.0

- The custody file gains a `telegram_groups` table on first start. The groups of
  events already in the inbox, in every state, are added once, in short batches;
  inbox, work and outbox rows, leases, receipts and the offset stay unchanged.
  An event whose bot or chat cannot be read is not assigned to any bot; the
  status counts such events as `group_backfill_unreadable`.
- This one-time pass finishes before the skill starts. Large retained inboxes
  can delay that first start; short batches bound lock time, not total work.
  Later status reads use the group projection and never repeat the history scan.
- `getUpdates` also requests `my_chat_member`. Telegram applies the new
  subscription to updates created after it, so an addition made before the
  upgrade is not reported.

## Upgrading to 0.10.0

- `getUpdates` also requests `message_reaction_count`. Telegram applies it to
  updates created after it.
- A reaction row that 0.9.0 left leased, or released after a restart or cancel,
  has no record of whether Telegram took it. It ends `uncertain` without another
  call. Edit, send and moderation rows keep their existing handling.
- 0.9.0 marked nothing before asking Telegram and kept only the newest attempt's
  error. A reaction row it released for a retry is sent again only when that error
  proves its one counted attempt did nothing: Telegram answered 400, 401, 403, 404
  or 429, or the call was never made (no token, a failed `getMe` or
  `deleteWebhook`). Any other answer, such as 408, 5xx or an unreadable
  `error_code`, a hold that replaced the error, or more than one counted attempt
  ends `uncertain` without another call, whatever happens to this version's first
  attempt. A row 0.9.0 never attempted, or only held, is sent as usual.
- Attempts 0.9.0 makes during a rollback are judged the same way. Each reaction row
  keeps the attempt count this version last left it with, so on return any attempt
  counted since then is 0.9.0's; a hold gives its attempt back in either version.
  0.9.0 reads neither that count nor the send marker, so while it runs it may repeat
  a reaction this version was sending when it stopped.
- The custody file gains a `telegram_messages` table on first start. It starts
  empty: messages received or sent earlier stay unknown as reaction targets, and
  nothing is backfilled.
- Without `presence_reaction_context: 1` on the Host, reactions stop reaching it and
  stay in custody. Without `presence_explicit_completion: 1`, own reactions stop being
  reported as deliveries, and reports frozen before the upgrade wait unsent (see
  [Reactions](#reactions-0100)). The promise that a reaction alone never wakes anyone
  needs an updated Host and this version together.
