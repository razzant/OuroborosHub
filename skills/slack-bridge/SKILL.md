---
name: slack-bridge
description: Slack presence transport with durable delivery, directory discovery, provider updates, file transfer, message actions, and provider context.
version: 1.9.0
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
permissions: [net, read_settings, widget, route, tool, companion_process, presence]
env_from_settings: [SLACK_BOT_TOKEN, SLACK_APP_TOKEN]
dependencies: [httpx, websockets]
when_to_use: User wants Ouroboros to receive Slack messages or files, reply in Slack threads, or send proactive Slack text messages.
timeout_sec: 30
companion_processes:
  - name: slack_socket_mode
    command: [python3, scripts/slack_daemon.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 10
tools:
  - name: slack_send
    effect: message
    target: channel_or_user
    conversation: channel_or_user
    thread: thread_ts
    description: Queue a proactive Slack message or threaded reply with explicit Markdown, Slack mrkdwn, or plain text format.
  - name: slack_user_info
    effect: read
    target: user_id
    description: Read one user's available Slack profile facts by exact user ID.
  - name: slack_conversation_info
    effect: read
    target: channel_id
    conversation: channel_id
    description: Read one conversation's provider metadata by exact channel ID.
  - name: slack_history
    effect: read
    target: channel_id
    conversation: channel_id
    description: Read one explicit page of conversation history without trimming message text.
  - name: slack_thread
    effect: read
    target: channel_id
    conversation: channel_id
    thread: thread_ts
    description: Read one explicit page of a thread using its root timestamp or a reply's.
  - name: slack_reactions
    effect: read
    target: channel_id
    conversation: channel_id
    description: Read one message's current reactions by exact channel and timestamp, with counts and possibly partial users.
  - name: slack_list_conversations
    effect: read
    target: []
    description: List one paginated conversation directory page with exact IDs and URLs.
  - name: slack_list_users
    effect: read
    target: []
    description: List one paginated user directory page with exact IDs and profile facts.
  - name: slack_lookup_user_email
    effect: read
    target: email
    description: Resolve one email to an exact Slack user ID when the provider permits it.
  - name: slack_members
    effect: read
    target: channel_id
    conversation: channel_id
    description: List one paginated member-ID page for an exact conversation.
  - name: slack_join
    effect: write
    target: channel_id
    conversation: channel_id
    description: Queue an explicit join of one public conversation by exact ID and keep its provider receipt.
  - name: slack_resolve
    effect: read
    target: query
    description: Return name/email/URL/ID directory candidates without guessing ambiguous matches.
  - name: slack_file_upload
    effect: message
    target: channel_id
    conversation: channel_id
    thread: thread_ts
    description: Stage immutable bytes and queue Slack External Upload API delivery.
  - name: slack_file_download
    effect: read
    target: file_id
    description: Download one provider file into the skill state artifact directory.
  - name: slack_message_edit
    effect: message
    target: channel
    conversation: channel
    description: Queue an update of an own Slack message by exact channel and timestamp.
  - name: slack_message_delete
    effect: write
    target: channel
    conversation: channel
    description: Queue deletion of an own Slack message by exact channel and timestamp.
  - name: slack_reaction_add
    effect: write
    target: channel
    conversation: channel
    description: Queue adding this bot's reaction to an exact Slack message; a reaction is not speech.
  - name: slack_reaction_remove
    effect: write
    target: channel
    conversation: channel
    description: Queue removing this bot's reaction from an exact Slack message.
  - name: slack_pin_add
    effect: write
    target: channel
    conversation: channel
    description: Queue pinning an exact Slack message.
  - name: slack_pin_remove
    effect: write
    target: channel
    conversation: channel
    description: Queue unpinning an exact Slack message.
  - name: slack_bookmark_add
    effect: write
    target: channel
    conversation: channel
    description: Queue adding a bookmark to an exact Slack conversation.
  - name: slack_bookmark_remove
    effect: write
    target: channel
    conversation: channel
    description: Queue removing a bookmark by exact provider bookmark ID.
  - name: slack_api
    effect: write
    target: []
    description: Call an actual Slack Web API method with a model-selected read or durable write effect.
  - name: slack_receipt
    effect: read
    target: []
    description: Inspect provider outcome, uncertainty and Host history-report status by request ID.
icon: ui/icon.svg
contributes:
  - id: place
    provides: place
    label: Slack
    destination: vendor:Slack
    scope: account
    source: {route: status, refresh_s: 15}
    icon: ui/icon.svg
    wakes: [direct_message, group_dm, mention, my_thread, reply_to_me]
---

# Slack Bridge

Slack Bridge is a provider-neutral Slack transport. It receives Slack events
through Socket Mode, commits every acknowledged envelope to a local SQLite
queue, preserves exact Slack provenance, stages inbound private files with the
bot credential, and durably delivers text replies.

The skill does not decide who is an administrator, reinterpret slash commands,
or turn Slack messages into owner commands. It transports conversation events
and leaves identity, authority, memory, and turn policy to the host presence
runtime.

## Slack place

Connect Slack in Places using provider `slack` and the workspace Team ID shown
in the widget. The host selects the place by account and conversation. Suggested
wakes are direct messages, group DMs, mentions, my threads, and replies to me.
Every supported event reaches the host for capture; its typed facts determine
whether consciousness wakes. Bot messages carry `from_bot` and need an explicit
wake choice. Place notes supply behavior instructions.

The adapter negotiates `places_version >= 1` through `/identity`, omits
`binding_id`, and uses the dedicated `presence` permission. Older hosts still
receive an already-saved local binding ID and the legacy event shape. Existing
receipts and deferred work references remain valid. The injected Host Service
token is opaque and revealed only at authenticated loopback requests.

Queued sends re-read the place immediately before delivery. Silent or Paused
keeps the item held with its original identity and bytes; Active allows it to
resume. An unavailable policy lookup also holds with a visible reason. Holds
do not consume the transport retry budget.

## Slack app setup

1. Create a Slack app from `manifest.json` and enable Socket Mode.
2. Create an app-level token with `connections:write` and save it as
   `SLACK_APP_TOKEN`.
3. Install the app and save its bot token as `SLACK_BOT_TOKEN`.
4. Grant both settings to this reviewed skill, then enable it.
5. Connect the Slack place and choose its wakes and notes.
6. Invite the bot to channels where it should participate.

Every DM, MPDM, public channel, and private channel event that the installed app
can receive is transported; the host place decides admission. Invite the bot where Slack requires explicit
membership; there is no second bridge-local channel allowlist.

Every file a Slack message declares becomes one entry of the event's
`message.attachments`: its Slack ID, name, type and size, the curated file facts
Slack supplied (such as `title`, `mode`, `external_type`, `external_url`,
`permalink`, `file_access`) and what staging observed. Bytes from Slack's
authenticated `url_private` locations are downloaded into the skill state
directory and submitted to the host (`content_available: true`; `staged_as` is
the staged file's basename, up to 180 characters after its ordinal; the host
derives its attachment label from it but may shorten or normalize that label, so
the two need not match exactly). A file this attempt could not stage is
described with `content_available: false`, `stage_error` and
`stage_error_details` while the message and its other files are still
delivered: no private URL, a URL outside the private-file host (for example a
Google Drive document), beyond ten files or 50 MiB of accepted bytes, a
redirect, or a completed HTTP answer other than 408, 429 or 5xx. That is a
fact about this attempt, not a promise the file stays unavailable. A 408, 429
or 5xx answer, a network failure or a local write failure keeps the message
queued and retried. `content_available` describes the bridge's staging; the
host attachment manifest decides what the model can open. Credentialed URLs
(`url_private*`, `permalink_public`) and previews are never placed in the
event, including the file lists of edited and deleted messages.

Rows queued by an earlier bridge version keep the file declarations they were
stored with: those versions dropped files without a private URL (such as
external or ID-only files) and their curated facts, and that is not recovered
from raw storage on upgrade. Such a row drains with what it kept, and a message
an earlier version ignored stays ignored.

`slack_file_download` exposes the same provider-authenticated path for a file
ID. Because those reads carry the bot credential, they are confined to Slack's
documented private-file host `files.slack.com`: another scheme, an embedded
userinfo component, a non-443 port, an IP literal or any other hostname is
refused before the request is built, and a redirect response is refused rather
than followed, so the credential is never replayed to another origin and a
login-page redirect is never staged as a file's bytes.

Outbound `slack_file_upload` copies immutable bytes into the skill state
directory at enqueue, then the companion runs Slack's current three-phase
External Upload API (`files.getUploadURLExternal`, raw bytes POST,
`files.completeUploadExternal`). A lost response after bytes or completion is
recorded as `uncertain` and the upload is not started again; a completion
request that never reached Slack (connection failure) retries the whole upload.
The bridge never claims a provider-side exactly-once mutation. Each staged copy
gets its own random file name: `request_id` stays an opaque dedupe key, and a retry
with the same ID keeps the first copy's bytes.

`slack_list_conversations` and `slack_list_users` remain one-page low-level tools:
follow `next_cursor` yourself and treat `complete=false` as incomplete.
`slack_resolve` handles directory pagination internally for names and returns
all matching candidates without choosing a person. Exact IDs, Slack mentions,
conversation/profile permalinks and user emails use direct provider lookups.
It never joins a channel or links identities as part of resolution.

Name scans share a credential-scoped directory cache in the existing bridge
database, reused across queries for up to five minutes. Results disclose
`observed_at`, `cache_hit`, `cache_age_sec`, `coverage`, `entries_scanned`,
`pages_fetched` and `complete`. `refresh=true` starts a new scan; omit `cursor`
then. Exact lookups always use the provider. A directory snapshot is not an
atomic Slack export and may change while being scanned. Private rooms that the
credential cannot see remain invisible; profile observations do not grant authority.

A scan fetches at most 50 pages and spends at most 45 seconds inside the
60-second tool call. `limit` is page size (default 200), not a result count.
Timeouts, missing/repeated cursors and rate limits return partial candidates,
`complete=false`, an error and the available `next_cursor`. Honor
`error.retry_after` before continuing with the same query/kind and cursor.
The cache retains traversed pages across continuation calls; an incomplete
empty result is never a cached proof that someone is absent. If no cursor is
available, retry with `refresh=true`. A caller-supplied cursor without a saved
prefix reports `coverage=from_supplied_cursor`, even at the provider's last page.
Very broad substring queries may still exceed the host's result-size limit;
refine the name or use an exact ID/email. The resolver does not silently prune
candidate matches to fit that limit.

`slack_lookup_user_email` uses Slack's exact `users.lookupByEmail` method.
`slack_members` lists member IDs,
while `slack_join` is an explicit provider mutation: it is queued in the same
durable mutation outbox as the other writes, returns a `request_id`, and its
provider result, terminal refusal (`already_in_channel`, `is_archived`, a
missing scope) or uncertainty is read with `slack_receipt`. It is never an
automatic fallback for a failed history read.

Message edits, deletes, reactions, pins, bookmarks and channel joins use the
durable mutation queue and retain the provider response or an explicit
failed/uncertain result. Every tool returns one JSON object encoded as text, so
a result is machine-readable exactly as documented here.
The app manifest must be reinstalled in a workspace after scope changes; an
edited public manifest does not grant scopes to an already-installed app.

## Provider context and on-demand reads

After the Socket envelope is durably accepted, the inbound worker looks up its
exact author with `users.info` and room with `conversations.info`. Messages and
mentions also read `conversations.replies` using their thread root, or their own
timestamp when they started the thread. A root with no replies returns its one
message; a queued root can already have later replies at processing time. Other
event kinds read thread context when they carry a thread timestamp, except
reactions, which read at most [their target](#reactions). Cursor pages
share the same ten-second provider-I/O budget (page size 200). These
observations run concurrently outside the Socket acknowledgement handler. The
worker saves their results, sources and observation times in the existing inbox
before submitting to Host. A retry or restart reuses that event's snapshot;
a subsequent event gets a fresh snapshot. Existing submitted rows retain their
original Host reference.

`conversation.thread_history` carries the currently accessible thread, including
messages newer than the triggering event. It preserves full provider message text,
author IDs, creation/edit timestamps and blocks; files use the same curated facts
as live messages, without credentialed URLs or previews. `observed_at` and
`completed_at` date the scan, not the original speech. This is a current provider
view, not a reconstruction of past revisions or an atomic export. `complete=true`
means the provider returned no more pages, not that the credential could see every
message ever posted. A later-page failure preserves the received prefix as
`partial`, with the error and available cursor. A first-page refusal is
`unavailable`; neither is an empty complete history. `continuation` names an
explicit `slack_thread` read, or a timestamp range when Slack supplies no cursor.

History is context for this one event. It creates no extra inbox events, replies,
new work or obligation to answer; the model judges meaning and participation.
A Host that retains Presence context sources exposes this dated snapshot through
the incoming row's `memory_read(source_ref=...)` address for later turns. Older
Hosts still receive the full current-event context but do not supply that later
reader. Reading a retained source does not refresh Slack; `slack_thread` does.

The model sees available display/real names, Slack username, profile fields
(including email and title when returned), timezone, channel name/type/topic/
purpose, and the workspace name already supplied by `auth.test`. Exact actor,
workspace, channel and thread IDs remain unchanged. Missing scopes, rate limits
or failed lookups appear as explicit `unavailable` observations; the original
message still reaches Presence. Empty or absent fields are not invented. A
stored observation describes the recorded moment, not a claim that the profile
is still current. `slack_user_info` and `slack_conversation_info` can obtain fresh
provider data on demand. Provider profile facts do not link people, infer roles
or grant system ownership; those judgments stay with the model.

`slack_history` and `slack_thread` fetch one page per call and preserve full
provider message text, timestamps, author IDs and thread fields. These explicit
tools do not retrieve a directory, feed historical messages into Presence as new
events, or change the transport's intake cursor. The automatic thread observation
above uses the same provider reader across pages. Use `slack_user_info` to resolve
an author ID. `slack_thread` takes the root message's timestamp or a reply's,
and also returns the root when Slack includes it. Follow `next_cursor` with the same
filters until `complete=true`; `has_more=true` without a cursor remains explicitly
incomplete and requires an explicit timestamp-range continuation. `oldest`,
`latest`, and `inclusive` expose Slack's normal time filters. Requested page size
is not a completeness guarantee; Slack's app classification may reduce it.

The app manifest declares `users:read` for profiles, `users:read.email` for email,
and `channels:read`, `groups:read`, `im:read`, `mpim:read` for room metadata.
History uses the corresponding existing `*:history` scopes. Existing apps need
the corresponding granted scopes; merely editing this file does not grant them.
All reads use the existing bot token. Method-specific bot-token restrictions,
conversation membership, scopes and rate limits remain provider facts: a refusal
returns its error code, HTTP status, required scopes when supplied, and retry
delay instead of pretending the result was empty. No automatic account login or
HTTP retry is introduced.

## Inbound provider updates

The manifest subscribes to `app_mention` with the `app_mentions:read` scope as
well as the existing message subscriptions. An invitation via a mention may
arrive without a corresponding general message event; it is admitted with its
exact provider identity. If `message` and `app_mention` describe the same workspace, channel,
message timestamp, text and provider revision, both envelopes are durable but
only the first starts a turn (`duplicate_message_surface` on the second).
Original revisions use the message timestamp; edits use `edited.ts` or their
change-event timestamp. A later metadata update retaining an old edit stamp is
still a separate change. Ordinary messages without mentions, real edits,
deletes and reactions retain their existing admission. This adds no rule about
who should answer or who is an owner.

Accepted events expose the authenticated bot's `self_user_id`, when known, in
`message.provider_facts`. Current messages also retain `mentioned_user_ids`
from explicit Slack `<@ID>` tokens and rich-text user elements, plus the
provider's `parent_user_id` when supplied. For edits these facts describe the
current nested message, not its previous revision; reactions and deletes do
not acquire current-message mention or parent facts. Missing fields remain
unknown, including in older persisted events. Mentions are occurrences, which
may be quoted, and the parent is the thread-root author; neither establishes
the intended addressee or an obligation to reply. The model decides whether
to participate. All otherwise admissible messages still reach Presence.

Message edits (`message_changed`) are normalized from Slack's nested
`message`/`previous_message` objects while both objects remain in provider
facts. When both snapshots identify the same message and contain matching
content, changes only to `language` or the `edited` marker do not start another
Presence turn. The complete envelope is still committed as `ignored` with
reason `message_content_unchanged` before acknowledgement. Every other field,
including unknown fields, remains in the comparison; missing comparison facts
do not suppress an update. Real edits retain their original message timestamp
and still reach the model. Slack documents automatic language detection as one
source of [`message_changed`](https://docs.slack.dev/reference/events/message/message_changed/).
This snapshot comparison does not recover an original message missed while
disconnected: an unchanged revision remains ignored even if it arrives first.
The automatic thread observation can recover accessible earlier speech as context;
it does not replay that speech as new inbox work.
A delete's `message_id` is the deleted message (`deleted_ts`, else
`previous_message.ts`) and its `thread_id` is that message's original thread;
the deletion's own timestamp stays its `event_ts`, and `deleted_ts` and the
previous message remain provider facts. Deletes already queued by an earlier
version keep the identity they were stored with.
`reaction_added` and `reaction_removed` preserve the reacted message ID,
reaction name, actor and `item_user`; [Reactions](#reactions) describes what they
become. Blocks-only messages are accepted when `blocks` carry
content even if Slack's `text` field is empty. Other bot/app events remain
provider facts and can reach Presence; the bridge drops only its own bot/app
events using the authenticated identity, preserving self-deduplication and
avoiding reply loops. These updates use the existing ordered inbox, host
adapter, and LLM-selected silent/message outcomes; no keyword or semantic gate
is added by the transport.

## Reactions

A reaction can be the whole social response: it shows you understood, saw or
laughed without another message. It is optional, it is not speech, a read
receipt or agreement, and an early gesture does not replace a promised result.
This bridge has no emoji dictionary and no required sequence of gestures.

**Incoming.** With a Host whose `/identity` says `presence_reaction_context: 1`
(strictly that integer, read when the bridge meets the Host; a supported or
unsupported answer is kept until the bridge restarts, and only an unreachable Host is
asked again), each
`reaction_added`/`reaction_removed` on a message becomes an event with empty
`text`, no attachments and `event.message.reaction` (contract version 1):

- `change` is `add` or `remove`, with the one value this event names in `new` or
  `old` as `{"type": "name", "value": "thumbsup::skin-tone-2"}`. That is a delta:
  it says nothing about other reactions on the message.
- `target.message_id` is the exact reacted message. `target.resolution` says
  whether `event.thread_id` is proven. `resolved` comes from an exact
  team+channel+message record (this bridge's inbox, or the creation receipt of its
  own posted text) or from one narrow `conversations.replies` read of that
  timestamp when no record exists. A top-level message keeps its own timestamp as
  its conversation and a reply names its proved root. No retry and no history scan
  follow. Refused, rate-limited or empty reads leave `unresolved` with an empty
  `thread_id`, never a thread named after the reacted message, and the read's
  error stays in `provider_facts.reaction.target_lookup`.
- `target.author` holds what is known (`item_user`, or the retained or read
  message's user, and `is_self`). `target.excerpt` holds up to 500 characters with
  `truncated: true` whenever words were cut; the full retained or read message
  stays in `target_lookup`. An own message's excerpt is the text this bridge sent,
  so a later edit of it is not reflected.
- `actor.kind` is `user`: one person or bot, not necessarily the owner.
  `message.sent_at` is the provider's `event_ts`. Other bots' reactions keep their
  `from_bot` fact and the bridge's own stay dropped.

The Host, not this bridge, decides that a reaction starts no focus. Reactions on
files and other non-message items have no message target and stay ignored.

**On an older or unknown Host.** A reaction is not delivered, not read from
Slack and not listed in transport queues, because that Host cannot promise not to
wake on it. It stays in this bridge's inbox as `ignored` with reason
`reaction_host_context_unavailable`, counted by `reactions_withheld`.
`status` shows `reaction_context_state: update_required`. It is never replayed
after an update, as a live arrival or as history. Ordinary messages are
unaffected. Reactions already submitted before this version keep their references.
One exception: a transport queue snapshot saved before this version may already list
a reaction. A retry repeats its saved observation unchanged, so that snapshot still
goes to the Host as it is. No snapshot taken from this version on lists one.

**Reading on demand.** `slack_reactions` reads one message's current reactions with
Slack's `reactions.get` (`full=true`), by exact channel and timestamp, a reply's
included. It is a declared read, so it stays available in Advises, Silent and Paused
places, with the bot credential and the existing `reactions:read` scope. Each row
keeps Slack's `name`, `count` and `users`. `count` covers everyone who reacted, and
`users` may list only some of them, as
[Slack documents](https://docs.slack.dev/reference/methods/reactions.get/). A message
without reactions gives `reactions: []`. A deleted or unknown message returns
`ok: false` with its provider code, such as `message_not_found`, not an empty list.
The read keeps no state and creates no event.

`slack_thread` with a reply's timestamp reads the message and its conversation.
Slack's `conversations.replies` page does not document a `reactions` array. When one
is present it is passed through whole, and its absence is not proof of no reactions.

**Outgoing.** `slack_reaction_add` and `slack_reaction_remove` use the durable
mutation queue with the bot's identity and return `queued`. Surrounding whitespace is
removed from `channel`, `ts` and `name` once, so the place check, the write, the
target lookup and the receipt all name the same message; a row queued earlier is read
the same way. `slack_receipt` then shows each outcome:

| Provider answer | Receipt | Meaning |
| --- | --- | --- |
| ok | `delivered`, `effect: applied` | This write took effect. |
| `already_reacted` | `delivered`, `effect: already_present` | This bot's reaction was already there. No new effect. |
| `no_reaction` | `delivered`, `effect: already_absent` | This bot had no such reaction. Others' reactions are not implied. No new effect. |
| Documented permanent refusal | `failed`, once | The next queued item proceeds at once. |
| Lost response, 408/5xx, `internal_error` | `uncertain`, once | Never sent again. |
| Rate limit | retried, bounded | No effect is claimed meanwhile. |

Silent and Paused hold the write as for any other mutation. Just before the write
the bridge re-reads the place in the thread that the target's retained record names,
the same thread its receipt names. So a reaction queued while active is held once
that thread's own place is Paused, even under an Active channel. A target with no
record is checked as the channel's, as before. The tool declares only the channel,
so the call-time guard sees the channel's place, and such a thread's hold comes at
this re-read. Reaction writes queue per reacted message, the same `channel` and `ts`.
A held reaction keeps its place only ahead of later reaction writes to that message,
which keep their queue order whatever name they carry. That holds even when the
message's record arrived after the first write was queued, and for rows an earlier
version queued. Top-level sends, edits, pins, uploads and reactions to other messages
in that Active channel go on meanwhile. In exchange, a reaction is no longer ordered
against those other writes in its channel. `slack_api` calls of `reactions.add` and
`reactions.remove` that name a `channel` and `timestamp` share that message's queue.
Their bytes and handling are unchanged. A Host advertising
`presence_explicit_completion: 1` also receives `/presence/delivery` with empty
text, `message.kind: "operation"` and a typed `message.reaction`
(`operation`, `new` or `old`, `target`, and `effect` when it took effect); other
Hosts keep only this local receipt, and `reaction_history_limitation` says so. A
report frozen for a Host that no longer advertises it stays pending and is not sent;
`history_report_error` says why. Because the bridge keeps its last `/identity` answer
until it restarts, a Host's capability change reaches a running bridge at its next
restart, and the report is sent then if the Host advertises it.
`slack_api` writes to `reactions.add` remain generic operations.

Queued rows from an earlier version become this event shape when they are claimed.
Their target is looked up the same way unless a saved provider snapshot predates
it, in which case they are `unresolved`.

## Generic Slack Web API access

`slack_api` is the narrow provider escape hatch for methods that do not yet
have a dedicated convenience tool. The `path` is one actual Slack Web API
method name (for example, `conversations.list` or `chat.postMessage`), and
the existing bot credential is supplied by the client; callers never provide
or persist a token. The model selects the provider effect separately from the
HTTP transport method: `effect="read"` executes a read immediately, while
`effect="write"` (the default) queues either GET or POST in the durable
mutation outbox. Slack documents some writes over GET, so the HTTP verb cannot
establish read-only behavior. Writes use a stable `request_id` when the
caller supplies one, provider receipts on completion, and an explicit
`uncertain` result when the response may have been lost after acceptance.
Use `slack_receipt` with that request ID to inspect each durable part's provider
result, failure or uncertainty and the separate Host history-report state.
For `chat.postMessage`, confirmed provider message/channel/timestamp facts
create a speech delivery report through the existing Host history path. For
other provider methods, select `result_kind="message"` only when the write
creates speech; the report still requires those actual provider facts.
`result_kind="operation"` retains the operation receipt without creating
new speech. The method/path validator
rejects full URLs, traversal and malformed method names while leaving the
provider's own scopes, method validation and errors authoritative.

Profile, conversation, history and thread reads use GET query parameters because
Slack's read methods do not reliably consume JSON POST arguments; message sends
continue to use POST JSON.

References: [conversations.list](https://docs.slack.dev/reference/methods/conversations.list/),
[users.list](https://docs.slack.dev/reference/methods/users.list/),
[users.lookupByEmail](https://docs.slack.dev/reference/methods/users.lookupByEmail/),
[conversations.members](https://docs.slack.dev/reference/methods/conversations.members/),
[conversations.join](https://docs.slack.dev/reference/methods/conversations.join/),
[files External Upload](https://docs.slack.dev/messaging/working-with-files/),
[users.info](https://docs.slack.dev/reference/methods/users.info/),
[conversations.info](https://docs.slack.dev/reference/methods/conversations.info/),
[conversations.history](https://docs.slack.dev/reference/methods/conversations.history/),
[conversations.replies](https://docs.slack.dev/reference/methods/conversations.replies/).

## Delivery behavior

- Socket envelopes are acknowledged only after their durable SQLite transaction
  commits.
- Slack retry envelopes and duplicate event IDs are deduplicated.
- Expired inbox leases and outbound leases that never began a provider write are
  reclaimed after a crash. An outbound write whose receipt was not durably
  recorded becomes `uncertain`, with no automatic resend.
- Admission is ordered per Slack thread while independent threads may run
  concurrently. Deferred and continuing work retain their durable reference and
  polling, but allow later messages in the same thread after the initial
  acknowledgement.
- The companion runs two inbound pools, each of the configured inbound worker
  count. Input workers only submit events that have no Host reference yet. Once
  a reference is durable, the input worker queues any speech its envelope
  already carries and releases the row. Observation workers poll stored
  references. Output polling therefore never occupies an input worker, with
  any Host version.
- Retryable refusals and connection failures before sending have at most five
  attempts. An ambiguous write result ends `uncertain` on its first occurrence;
  failed and uncertain items no longer block later messages in the same thread.
- Outgoing writes keep queue order within one lane. For sends and most mutations,
  a lane is one thread, or a conversation's top level. For reaction writes, it is
  the reacted message. A held or in-flight item holds back only its own lane.
  Opening the store moves live reaction rows, including such `slack_api` calls,
  that an earlier version queued per channel into their message's lane. Each claim
  does the same, so a row an older process queues later is ordered too. Their
  lease, send marker and bytes stay the same.
- Long outbound text is split into Slack-safe chunks before it enters the
  durable outbox.
- The Widgets tab reports connection state, queue depth, failures, and recent
  activity without exposing tokens or message contents.

Save settings before enabling the skill, or toggle it after a settings change.
An absent settings file simply means "not configured yet". A settings file that
exists but cannot be read, is not JSON, or is not a JSON object is reported as
`binding_state: "unreadable"` with `local_settings_error` in the status route,
the companion refuses to start on it, and saving settings returns HTTP 409
without overwriting the bytes nobody could parse. Opening the settings form
reads the saved Binding ID and worker counts back without writing anything, so
changing one field keeps the others; an unreadable file answers that read with
409 as well, which disables Save.

Delivery is durable and retries are bounded. Immediately before a provider
write, the outbox persists an attempt marker under its current lease. A lost
response, HTTP 408/5xx, malformed response or ambiguous Slack internal error
ends that item `uncertain` immediately. Cancellation after the marker does the
same; an expired marked lease is recovered as uncertain, never as permission
to resend. A 429 rate-limit refusal or a connection/pool failure before the
request can be sent clears the marker for a bounded retry. The marker precedes
the network call, so a crash in that small gap can leave an unsent item
uncertain. This is a deliberate unresolved result, not a delivery claim.
If the client rejects mutation arguments locally, or cannot obtain nonempty
bytes from a staged upload before any provider request, the receipt ends `failed`
with `uncertain: false`. No provider request was sent, and the same request ID
is not automatically retried. This applies only to those local refusals;
an unclassified error after dispatch remains `uncertain`.

On upgrade, pre-marker leased rows are treated conservatively as started.
Historical pending retries retain their queued identity; the new bridge cannot
recover whether an older version already repeated a send. Neither the marker
nor a stable outbox ID promises Slack-side exactly-once delivery.

Slack documents possible partial success for
[`internal_error` and `fatal_error`](https://docs.slack.dev/reference/methods/chat.postMessage/#errors)
and permits retry after the stated delay for
[HTTP 429](https://docs.slack.dev/apis/web-api/rate-limits/#responding-to-rate-limiting-conditions).

## Presence continuation

The same `/identity` answer may advertise `presence_continuation_version: 1`.
Only then does each new submission add `continuation_version: 1`, with either
delivery-reporting mode, unless version 2 is listed (see
[Timely intake](#timely-intake-version-2) below). Without either, no new field
is sent and the request waits for the turn's end exactly as before. Rows queued
by an earlier bridge version keep their original references.

A Host may then answer while the author's result still waits for review:
`status: "continuing"` with the author's `continuation_ref`, the initial
`outcome`/`text`/`output_ref`, and a promoted child's own `work_ref`. The bridge
stores that write-once envelope as the event's Host reference; a retried event
receives the identical stored envelope, never a rerun. Like deferred work, the
event then only polls, so later messages in its thread are submitted meanwhile.
If promotion happens on a later reentry, a pending or terminal author poll (or
an older Host's `interrupted` one) can supply `child_work_ref`. The bridge
checkpoints each discovered reference beside the initial envelope before polling
it. After a restart, the child remains independently pollable even if the author
is unavailable.
The author and the child are polled concurrently through `/presence/work`.
The input worker that stored the envelope commits its initial selection and
releases the row. An observation worker commits that selection again (once,
by identity) before starting either poll, then commits each response's outputs
as it arrives. A child result can be sent while an author poll is still
awaiting HTTP, and vice versa. A later message can be submitted while these
polls are in flight. The observation worker owns and reaps both requests on
cancellation; no detached poll queue is added.
The event is finished when every polled reference is terminal.

Each released or terminal output is queued once under its Host `output_ref` and
the event's exact Slack destination. This includes a v1 turn completed within
the initial request, with no `continuation_ref`; its selection does not fall
back to a legacy reply index. The outbox key comes from those
identities, never from the text or a reply index: a repeated poll, a replayed
envelope or a companion restart cannot queue it again, while an identical
correction under a new `output_ref` is new speech. `silent` and
`tool_delivered` send nothing; without a send tool, late text arrives through
the poll. Released text is queued as soon as Host returns it; an unreadable poll
backs off without holding it back. An output whose Slack outcome was failed or
uncertain is never queued again by a later poll. With reporting enabled, each
physical part keeps the automatic origin (source event plus the author's or
child's task) and names the carried `output_ref` in the report's `message`.
Release to the bridge is not proof of Slack delivery; provider receipts keep
that role.

An author whose attempt ended without a canonical answer polls `failed`
(`cancelled` when recorded so) with `outcome: "silent"` and its reason facts on
the current Host. Its input is not resubmitted; real children remain
independently observed after the author's reference ends. Released outputs keep
their identities. Older Hosts may return `unresolved`, which stays observed, or
`interrupted`, which ends the row `failed` after its children finish, preserving
sent output.

Older persisted `refused:` references keep their original refusal and any
admitted child. New `rejected` responses retain their full receipt and end the
event; `blocked` refusals before admission keep retry custody for configuration
repair. An older Host's blocked/retry refusal with an admitted work reference
keeps the legacy `refused:` observation path instead of resubmitting that event.

With a continuation-capable Host, each submission also carries
`event.conversation.transport_queue`: a snapshot of this thread's later events
that the bridge has durably received without a stored Host reference. It is read from the
existing inbox immediately before the event's first submission attempt and kept
with the event, so a retry resubmits the same observation, like the
provider-context snapshot. Its fields are
`schema_version: 1`, `source: "slack-bridge inbox"`, `observed_at`,
`conversation_key`, `after_source_event_id`, `pending_count`, `omitted_count`,
`complete`, `text_limit_chars`, a `note`, and `events`, each with
`source_event_id`, `event_type`, `subtype`, `actor`, `message_id`,
`thread_id`, `event_ts`, `received_at`, `inbox_state`, `text`, `text_chars`,
`text_truncated`, declared `files` (ID and name only, never private URLs),
`provider_facts` (the same blocks/edit/reaction facts and curated file projection
as a normal submission), and, when present, `change` or `reaction`. On a Host
without `presence_reaction_context: 1` a reaction is not listed.

Default snapshots carry every observed queued event and its full text:
`omitted_count: 0`, `text_limit_chars: null`, `text_truncated: false` and
`complete: true`. The Host can retain this exact event-bound observation for
its scoped source reader rather than forcing an author to rely on a clipped
preview or an unresolvable bridge URI. Explicit diagnostic limits report every
omitted event and mark `complete: false` if either rows or text were cut.
Older persisted snapshots are not silently replaced on retry: their cut
counters remain, and their older `complete` field covered rows only. Consumers
must also inspect `text_truncated` before claiming full coverage. This does increase request size with the observed
queue; no hidden event-count cap is introduced.

Each continuing-author poll also takes a fresh snapshot from the same inbox
and posts `{transport_queue}` to `/presence/work/{continuation_ref}` before its
GET (including the saved `binding_id` on legacy hosts). This uses the original event's `after_source_event_id` and exact
conversation. The pending report is saved separately from the immutable initial
event and retried byte-for-byte after a lost ACK or restart. A `recorded`,
`duplicate` or `stale` ACK permits a fresh observation on the next poll. The
child's poll runs independently; released output is queued before either poll
or observation report starts.

A leased row may already have a submission in flight: absence of a stored Host
reference is not proof of non-admission. These are observations of
correspondents' words, not owner directives, a second queue or a submission: every listed event still reaches Host later as its own
event. Host retains full refresh observations in the continuing task's source
store for its existing scoped reader. The observation is dated: arrivals after
that snapshot, including those racing reentry and delivery, remain unknown
until a later report or admission. The bridge makes no continuous-freshness
promise. A failed report is visible in the inbox error and retried; it cannot
turn unavailable facts into an empty queue.

### Timely intake (version 2)

When `/identity` lists 2 in `presence_continuation_versions`, new submissions
carry `continuation_version: 2`. Otherwise the scalar
`presence_continuation_version: 1` still selects version 1, and a Host
advertising neither gets no continuation field. The request is otherwise
unchanged, including the transport queue snapshot and its refresh before each
author poll.

- `status: "continuing"` with `outcome: "deferred"`, empty text and a
  `continuation_ref` is custody, not a reply. It arrives once the author's intake
  is open. The bridge stores it, which releases that thread's queue, and an
  observation worker polls the reference. A replayed envelope keeps the version
  it was stored under: a version 1 envelope returned for a version 2 request is
  observed as version 1. Any other echoed version is refused and the event stays
  queued.
- `status: "completed"` with `outcome: "silent"` means the event was heard or
  delivered as context to the conversation's live author. The event is finished
  and nothing is sent; any reply arrives through that author's poll.
- HTTP 429 `presence_capacity_full` with `disposition: "retry"` (field
  `conversation` or `capacity`) means nothing ran. The same event stays first
  in its thread and is resubmitted unchanged after max(hint, backoff). The hint
  is the Host's numeric `retry_after` and is never shortened. The backoff is the
  bridge's own: 1 second, doubling per refusal, capped at 30 seconds. It is used
  alone when the hint is missing or malformed. There is no attempt cap. Other
  threads continue meanwhile. Nothing is dropped, merged or debounced.
- Author outputs and terminals may carry `reply_anchor: {source_event_id,
  message}`, which names the last input the author covered. A Slack reply still
  goes to the event's own channel and thread; the anchor does not change the
  destination. Promoted children and retained views carry no anchor and keep
  that same destination. Each `output_ref` is queued once across the initial
  envelope, polls, the terminal, children and restarts.
- A stopped author's terminal is `cancelled` with `outcome: "silent"`. Nothing
  new is sent, the event ends `failed`, and it is never resubmitted.

Each arrival is still its own submission in arrival order. While an author is
live, arrivals in its thread reach it as context without waiting for its answer.
Older Hosts keep version 1 or plain turns: their thread queue is still held until
the author parks or ends, but polling stays off the input workers.
References stored as version 2 cannot be observed by an older bridge that is
rolled back. They keep their custody until this version runs again.

## Message formatting

New automatic replies and `slack_send` calls default to `text_format="markdown"`.
Standard Markdown such as `**bold**`, `*italic*`, fenced code, lists and
`[label](https://example.org)` is sent through Slack's native `markdown_text`
field. The skill does not rewrite markup or maintain a Markdown parser.

Choose `text_format="mrkdwn"` for Slack-native `*bold*` and `<url|label>` syntax,
or `text_format="plain"` to show punctuation/markup literally. These modes use
the ordinary `text` field with `mrkdwn=true` or `false`; Markdown sends never
combine `markdown_text` with `text` or `blocks`. The outbox stores the selected
format with each chunk. Existing rows keep their original Slack-native mrkdwn
interpretation; retries retain the same text, chunks, target, thread and format.
An ambiguous provider error never triggers a second send in another format;
it settles the item as uncertain. Known no-effect retries preserve the format.

The existing lossless 3,900-character chunker stays in use. Very long code fences
or other markup spanning a chunk boundary may render separately; keep formatted
sections within a chunk or use plain text when exact literal presentation matters.
No message characters are silently dropped.

Reference: [chat.postMessage formatting fields](https://docs.slack.dev/reference/methods/chat.postMessage/).

## Receipt-backed conversation history

The companion discovers `presence_delivery_version` from the authenticated
loopback `/identity` endpoint. On a supporting Host, new Presence submissions
opt into delivery reporting. The actual mode echoed by the original turn is
kept with both its immediate and deferred results; an old cached turn or old
automatic outbox row stays in legacy mode even after an upgrade.

Each new explicit send or mutation captures only compact origin references from its tool
context, never the complete task or credentials. New sends opt into reporting
when Host capability discovery has succeeded. Older Hosts continue sending;
`status` exposes `history_reporting_state` and `history_reporting_limitation`
when receipt-backed history is unavailable. No unknown turn fields are sent to
an older Host, and no old terminal outbox rows are retroactively imported.

After Slack confirms a physical message chunk, the outbox commits its actual
resolved channel, provider timestamp and immutable report payload before any
history callback. The existing outbound workers submit that report through
`/presence/delivery`. Report ACK, lease and bounded-backoff retries live beside
the existing provider receipt in the same outbox. Each existing outbound worker
keeps at most one tracked report task in flight while continuing provider sends;
stopping the worker cancels and awaits that task. A slow or failed report never
requeues the provider send or holds later sends waiting for a report ACK. A restart
or lost ACK retries exactly the same report; the Host owns idempotent history
acceptance. The status route exposes pending/acknowledged report counts and
the last report error separately from provider delivery state.

Reports identify tool versus automatic origin explicitly. Successful chunks
are `delivered`; definitive terminal Slack errors are `failed`, and
ambiguous write failures are `uncertain`, never a delivered full logical message. An
unresolved user target is retained as requested with `target_resolved=false`;
it does not claim a resolved DM channel. Queued messages are not spoken history.
The first unknown provider effect stops automatic retries of that item.
Host report deduplication is separate from the provider effect and does not
establish provider-side exactly-once delivery.

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
is observed on its own. Older Hosts may still answer `unresolved`, which
stays observable. Canonical selected outputs keep their source and delivery
identity across repeated polls and restarts; a retained receipt itself is never
sent as speech. This does not claim exactly-once provider delivery after
ambiguous network acceptance.
`rejected` ends an event-specific refusal; before-admission `retry` and `blocked`
retain retry custody, including after account or binding repair. Missing or unknown typed
dispositions stay retryable, even on HTTP 403/404. Full queue recovery requires
both an updated Host and this transport; older Hosts may still return a
retry-only refusal. Existing completed delivery receipts remain unchanged.

The existing Host-reference envelope retains the receipt and each observed
reference. Upgrade through the normal payload review/enable lifecycle; old
references are not reinterpreted as new submissions.

## Identity (1.7.2)

When `/identity` advertises `presence_identity: 1`, the status route and every
turn event declare `identity.sends_as` with the bot's `user_id` and `user` name
from Slack `auth.test`, and `kind: bot`. This is the credential that speaks,
separate from the incoming message's actor. Older hosts receive no identity key.
