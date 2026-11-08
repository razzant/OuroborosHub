---
name: telegram-bot
description: Durable Telegram transport for generic Ouroboros presences, with exact actor and conversation
  provenance, media staging, and provider receipts.
version: 0.12.0
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
  description: Queue a proactive Telegram text, photo, document, video, audio, voice, animation or album for durable delivery.
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
  sends:
    when:
      method: [editMessageText]
    target: chat_id
    conversation: chat_id
    thread: topic_id
    text: text
  description: Queue an edit of this bot's own message, or set or clear its reaction on a reachable message, with durable receipts.
- name: telegram_react
  effect: gesture
  target: chat_id
  conversation: chat_id
  thread: topic_id
  message: message_id
  description: Set or clear this bot's reaction on an exact Telegram message; a gesture, not speech.
- name: telegram_typing
  effect: write
  target: chat_id
  conversation: chat_id
  thread: topic_id
  description: Ask for "typing…" in this turn's own chat while actually preparing a reply; a request, not speech, no receipt.
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
- inbound file staging through Telegram `getFile` and plain descriptions of other content;
- outbound text, photo, document, video, audio, voice, animation and album sends with durable receipts;
- a namespaced `telegram_send` tool for proactive delivery to exact numeric chat,
  topic, and message ids, and `telegram_typing` for a turn's "typing…";
- a bounded poller and fixed worker set; and
- an operational Widget showing provider and custody state.

The payload does not call the owner `/chat/inject` path, allocate synthetic
internal chats, interpret owner commands, or create prompt envelopes. It uses
only the reviewed `presence` Host permission. Connect a Telegram place with the
bot account ID, or one chat at a time (see
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
mutation the worker re-reads the place of its chat or forum topic, including a private chat's place.
Silent/Paused or an unavailable lookup holds the queued bytes and receipts
without consuming retries; Active resumes, and an unavailable lookup clears once
Ouroboros answers. When Ouroboros refuses the skill itself (its grant,
enablement, review or token), the hold lasts until its access is fixed in Skills.

Messages, edits and reactions: [Incoming content (0.12.0)](#incoming-content-0120). Reaction updates require the bot to be an administrator
in the chat and an explicit Bot API `allowed_updates` subscription; Telegram
does not deliver bot-authored reactions as user reactions. See
[Reactions](#reactions-0100).

## Incoming content (0.12.0)

Every message a person sends is an event; `event.text` is only their words or caption, never made up.
Files are `message.attachments` records (`kind`, `file_id`, `file_name`, `mime_type`, `file_size`, 0 if
unknown, `duration_sec` for sound and video): photo, document, voice, audio, video, video_note and animation
(a GIF, not also as a document), captioned or not, downloaded unchanged and staged; nothing is cut,
converted or transcribed. A fetched record says `status: staged`. One that did not arrive keeps its `file_id`,
and `note` says why in one factual sentence:

- `too_large`: declared above the 20 MiB the Bot API serves a bot (the configured server's limit), or
  `getFile` answered "file is too big". Never fetched.
- `failed`: `getFile` answered that the file_id cannot be read, or a failure that may pass kept recurring.
- `unsupported`: reserved for a declared file this connector does not fetch.

Only staged files reach the Host's attachment manifest. Telegram's description decides, not its status code:
the generic 400 "wrong file_id or the file is temporarily unavailable" usually passes and is retried, like a
5xx, a lost or cut-off answer or an expired link, while the event stays first in its chat. A failure counts only
when Telegram answered without serving the file, or no whole answer came while getUpdates answered; a flood wait
(429: the event asks nothing more until it passes), a refused token or a network that is down counts nothing, so
a sleeping Mac fails no file. After 6 counted failures and 30 minutes since the first, the record ends `failed`
and the chat moves on. Settled records and fetched files are kept, so a retry, a Host's too, submits the same
event and asks Telegram only about the rest.

Anything else is `message.non_text`: `[{"kind", "description"}]`, plain words as Telegram reports them, up to
4000 characters: sticker (emoji and set; not fetched), poll, location, venue, contact, dice, story, paid_media
(paid files Telegram gives are attachments), game, invoice, giveaway,
checklist, and `other` for a live photo (its photo is an attachment), rich message, shared users or chats and
web app data. A live location's later positions are edits, not events. `forward_origin`,
`is_automatic_forward`, `quote` and `reply_to_message` stay as Telegram sent them. An album member keeps
`media_group_id` and a 1-based `media_group_position` counted from the members this bot retained (one
update each, in message order, even across getUpdates batches).

Voice and audio keep a suffix the Host recognizes (`.ogg` for voice) and are not transcribed here. Nothing
but text can be a mention or a word signal. Storage: every file is fetched before the Host decides attention,
up to 20 MiB each in this skill's staging folder until the Host takes the event, and the Host keeps its own
copy with the event whether or not it wakes a turn.

## Outgoing media and typing (0.12.0)

`telegram_send` takes `kind` `message`, `photo`, `document`, `video`, `audio`, `voice` (OGG/Opus, MP3 or
M4A), `animation` or `album`: `items` of 2 to 10 files, photos with videos, audio or documents only with their
own kind; a top-level `caption` goes on the first item. Files and captions are checked before queueing: 10 MB
per photo, 50 MB per other file, 1024 visible characters per caption. A refusal is typed (`code`
`file_too_large`, `caption_too_long`, `voice_format`, `album_size`, `album_item`, `album_kind`, `album_mixed`,
`album_caption`, `file_missing`) and queues nothing. An album is one outbox row and one `sendMediaGroup` call;
each message returned gets its own receipt entry, `/presence/delivery` report (`part_id` 0, 1, …) and
own-message record. Retries and `request_id` deduplication are the outbox's. Once Telegram answers ok, the album
is never sent again: fewer or more messages than items, or no list, ends the row `uncertain` with those returned
in its receipt's `confirmed_messages`, as for any send not delivered.

`telegram_typing` records a request for "typing…" in the calling turn's own chat or forum topic, never a
reply chain, when the model is actually preparing a reply there, and answers `typing: requested`. It is not
speech: no outbox row, receipt or delivery report. The runtime renews `sendChatAction` about every 4.5 seconds,
once per conversation however many turns asked, while the turn is live by this transport's records (its
`/presence/turn` request open or its work reference observed), reading the place before every showing. It ends
at this bot's first message delivered there, even one queued earlier, at turn end, after 2 minutes, when the
place may not act (Silent, Paused or unread), or at any Telegram refusal, including 429, never retried; the
first three are checked again just before each call. The model may ask again after an intermediate message. A
restart discards requests. Turns that do not ask show nothing; no Host capability is needed.

Large files: `telegram_send`'s registered description, the only connector text every Telegram focus receives
whatever its notes say, lets the model ask a `too_large` or `failed` file's sender to share a download link
there, open to no more people than had the file, never to publish private or work files or pass them beyond
that audience, with fetch recipes for Yandex.Disk, Google Drive (confirm token over 100 MB) and Dropbox
(`dl=1`). No own Bot API server runs.

## Connecting groups (0.9.0)

Each private chat, group or supergroup the bot has seen is offered by name when
you connect a Telegram place. Since 0.11.1 this includes private chats people
have already opened with the bot, using their numeric chat IDs:

1. Have the person open a private chat with the bot, or add the bot to a group.
   Telegram reports a group addition to the bot
   (`my_chat_member`, which needs no admin right); any message the bot receives
   there also adds the group.
2. In Ouroboros open Consciousness, choose **Connect a place**, then **Telegram**.
   Beside **Whole account**, each chat is listed with its numeric ID:
   `Book club (-1001234567890)` or `Mira Lee @mira (7001)`. A private chat uses
   the person's observed first/last name and username when available.
3. Choose **Connect** on that chat. The new place starts Silent: it hears and
   thinks there and sends nothing.
4. Make the place Active on its sheet when the bot may speak there.

The status route returns the list as `connect: [{account_id, conversation_id,
label}]`: one row per chat whatever its topics, ordered by name, without a
limit. Channels are not listed. Only the bot that `getMe`
confirmed for the token configured now has a list. When the token is missing,
when `getMe` has not confirmed the current token yet or refused it, or when the
transport is not running, `connect` is `null`. The status names a stopped bot,
missing token or unconfirmed identity; `runtime_state` and `last_error` retain
transport errors instead of promising that waiting will fix them.
Connect then reports the list as unavailable. A confirmed bot seen in no chat
yet has `connect: []`. Reading the list makes no Telegram call.

The list records what the bot has observed and does not check current membership.

- Removing the bot (its status becomes `left` or `kicked`, or a service message
  names it as the member who left) takes the group off the list, and adding it
  again brings it back. Blocking/unblocking the bot does the same for a private
  chat. A rename updates the label. Telegram's update order
  decides, so an older message never undoes a later removal, rename or migration.
- A group upgraded to a supergroup gets a new ID. The old ID leaves the list,
  and the supergroup appears once one of its own updates, which carry its type
  and title, arrives. A place connected to the old ID is not moved: connect the
  supergroup as a new place.
- Additions, removals, renames and migrations only update this list and the
  offset; they never become events, turns or places. Nothing is connected
  automatically, and the list never asks Telegram about members.
- Messages from a chat that has no place yet stay queued in custody while the
  Host refuses them, and reach Ouroboros after you connect the chat. The retry
  back-off is unchanged, so the first of them can wait up to about four minutes.
- A group from which the bot captured no message before 0.9.0 appears after the
  bot's next update there, for example a message that mentions the bot.
- Upgrading to 0.11.1 makes one resumable pass over the retained inbox to add
  previously seen private chats to the same `telegram_groups` projection. It
  keeps one row per bot/chat with its latest name and membership evidence;
  no second message history or contact registry is created. The existing rows,
  receipts, offsets and connected places stay unchanged. The projection keeps
  the existing custody lifetime, including removed chats' last evidence.
  Status reads use this projection, never repeat the inbox pass. A large retained
  inbox can delay the first startup; each transaction reads at most 200 rows.
  Earlier versions did not record private-chat blocks: a chat reconstructed from
  retained messages may still be offered after an old block, until a newer block,
  unblock or message updates its evidence.
- Rolling back to 0.11.0 keeps these private rows visible in the same chooser,
  but that version no longer updates their names or block/unblock evidence.

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
[Reactions](#reactions-0100)).
Text and caption entities retain their original UTF-16 offsets, hidden link
URLs, and text-mention user facts.
When Telegram supplies a replied-to message, the event includes its text or
caption, original author/chat/message IDs, date, entities, file descriptors and
`non_text`. Selected quotes and forwarding origins remain separate source
facts; a forwarded author does not replace the current sender.

Each message keeps Telegram's send time as `message.sent_at` (ISO, from its `date`;
`sent_at_epoch` stays). Edits retain their original message id and send time with
`message.event_kind` set to `edited_message` and `message.edited_at` from `edit_date`. Reaction updates retain the target message id and the old/new
reaction arrays, with `event_kind` set to `message_reaction` and no synthetic
message text. These are observations for model judgment, not automatic replies.

Reply context comes only from that incoming update. It does not fetch history,
follow nested reply chains, or download the replied-to message's media.

## Reactions (0.10.0)

A reaction can be the whole social response: it shows you understood, saw or
laughed without another message. It is optional, it is not speech, a read
receipt or agreement, and an early gesture does not replace a promised result.
This connector has no emoji dictionary and no required sequence of gestures.

**As a response (0.11.0).** `telegram_react` declares `effect: gesture` with
`message: message_id`. A Host that knows gestures can offer it beside
`telegram_send` for the message that woke a turn, with `chat_id`, `topic_id` and
`message_id` filled in. `action: set` takes one `emoji` or `custom_emoji_id`;
`action: clear` takes neither. It calls `telegram_operation` with
`method: setMessageReaction` and nothing else changed: the same checks and topic
rules below, the same outbox row and receipt `telegram-operation:<request_id>`
(one `request_id` names one operation whichever tool queued it), the same provider
outcomes, and an unknown outcome is never sent again. `telegram_operation` stays a
mixed `write` tool and keeps `setMessageReaction`. A Host that predates gestures
reads `telegram_react` as an undeclared write: Silent, Paused and Advises refuse it
as before, and it offers no reaction door. The reaction stays an act on that
message, never the turn's words or evidence of finished work.

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
| Earlier attempt ended with no answer: expired or stopped lease, or a cancel; or 0.9.0 made it and kept no proof it did nothing ([Earlier upgrades](#earlier-upgrades)) | `uncertain`, with no call | The next queued item proceeds at once. A row without a recorded result may have reached Telegram. |
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
that does not state them stays held and is never sent.

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

## Identity (0.6.2)

When `/identity` advertises `presence_identity: 1`, the status route and every
turn event declare `identity.sends_as` with the bot id and `@username` returned
by Telegram `getMe`, and `kind: bot`. This is the sending credential, separate
from the incoming message's actor. Older hosts receive no identity key.

## Earlier upgrades

- 0.5.0: a reply chain once keyed by its `message_thread_id` became the chat's conversation; a binding pinned
  to such a thread no longer matches.
- 0.7.0: private chats are captured with `direct_message` (add it to a place's wakes to be woken); earlier
  private messages are not replayed.
- 0.8.0: early answers need a Host listing `2` in `presence_continuation_versions`; queued events, submitted
  events and delivery identities are unchanged.
- 0.9.0: the custody file gains `telegram_groups`, filled once before the first start (unreadable events
  count as `group_backfill_unreadable`); `getUpdates` also requests `my_chat_member` for later updates.
- 0.10.0: `getUpdates` also requests `message_reaction_count`, and the custody file gains `telegram_messages`,
  empty at first. A reaction row 0.9.0 left leased or released has no proof whether Telegram took it: it is
  sent again only when its one counted attempt provably did nothing (Telegram answered 400, 401, 403, 404 or
  429, or no call was made) and otherwise ends `uncertain` without a call; a row 0.9.0 never attempted is sent
  as usual. Each row keeps the attempt count this version left, so attempts 0.9.0 makes after a rollback are
  judged the same way; 0.9.0 may repeat a reaction this version was sending. Host capabilities:
  [Reactions](#reactions-0100).
- 0.12.0: the custody file gains `download_failures` and `typing_requests`, which earlier versions ignore.
  Queued rows keep their shape (a GIF queued as a document stays one; a 0.10.6 note stays as written). After a
  rollback 0.11 fetches a `failed` record again.
