---
name: slack-bridge
description: Slack presence transport with durable delivery, directory discovery, provider updates, file transfer, message actions, and provider context.
version: 1.5.0
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
    description: Queue a proactive Slack message or threaded reply with explicit Markdown, Slack mrkdwn, or plain text format.
  - name: slack_user_info
    description: Read one user's available Slack profile facts by exact user ID.
  - name: slack_conversation_info
    description: Read one conversation's provider metadata by exact channel ID.
  - name: slack_history
    description: Read one explicit page of conversation history without trimming message text.
  - name: slack_thread
    description: Read one explicit page of a thread using its root timestamp.
  - name: slack_list_conversations
    description: List one paginated conversation directory page with exact IDs and URLs.
  - name: slack_list_users
    description: List one paginated user directory page with exact IDs and profile facts.
  - name: slack_lookup_user_email
    description: Resolve one email to an exact Slack user ID when the provider permits it.
  - name: slack_members
    description: List one paginated member-ID page for an exact conversation.
  - name: slack_join
    description: Queue an explicit join of one public conversation by exact ID and keep its provider receipt.
  - name: slack_resolve
    description: Return name/email/URL/ID directory candidates without guessing ambiguous matches.
  - name: slack_file_upload
    description: Stage immutable bytes and queue Slack External Upload API delivery.
  - name: slack_file_download
    description: Download one provider file into the skill state artifact directory.
  - name: slack_message_edit
    description: Queue an update of an own Slack message by exact channel and timestamp.
  - name: slack_message_delete
    description: Queue deletion of an own Slack message by exact channel and timestamp.
  - name: slack_reaction_add
    description: Queue adding a reaction to an exact Slack message.
  - name: slack_reaction_remove
    description: Queue removing a reaction from an exact Slack message.
  - name: slack_pin_add
    description: Queue pinning an exact Slack message.
  - name: slack_pin_remove
    description: Queue unpinning an exact Slack message.
  - name: slack_bookmark_add
    description: Queue adding a bookmark to an exact Slack conversation.
  - name: slack_bookmark_remove
    description: Queue removing a bookmark by exact provider bookmark ID.
  - name: slack_api
    description: Call an actual Slack Web API method with a model-selected read or durable write effect.
  - name: slack_receipt
    description: Inspect provider outcome, uncertainty and Host history-report status by request ID.
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

## Presence binding

Choose the owner-created exact or account-wide presence binding in this skill's settings.
Create it for provider `slack`, the workspace Team ID shown in the widget, and
an exact channel conversation ID or `*`. The bridge keeps that one 32-character lowercase
hexadecimal Binding ID and submits neutral provider events to the reviewed
loopback presence endpoint using the dedicated `presence` permission. The
host-injected Host Service token is held only as an opaque `SkillToken` and
revealed at each loopback request; it is never logged, persisted, or exposed by
the status route. Immediate text is queued once for Slack; deferred work keeps
its durable work reference and is polled until terminal. On a Host that
supports it, a turn whose result waits for review answers early and its author
is polled in the same way (see Presence continuation).

## Slack app setup

1. Create a Slack app from `manifest.json` and enable Socket Mode.
2. Create an app-level token with `connections:write` and save it as
   `SLACK_APP_TOKEN`.
3. Install the app and save its bot token as `SLACK_BOT_TOKEN`.
4. Grant both settings to this reviewed skill, then enable it.
5. Save the owner-created Presence Binding ID in the skill settings.
6. Invite the bot to channels where it should participate.

Every DM, MPDM, public channel, and private channel event that the installed app
can receive is transported; the selected host binding decides admission. Invite the bot where Slack requires explicit
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
exact author with `users.info` and room with `conversations.info`. These two
bounded calls run concurrently outside the Socket acknowledgement handler. The
worker saves their results, sources and observation times in the existing inbox
before submitting to Host. A retry or restart reuses that event's snapshot rather
than silently substituting a later profile. A subsequent event gets a fresh
snapshot. Existing submitted rows retain their original Host reference.

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
provider message text, timestamps, author IDs and thread fields. They never
automatically retrieve a directory or bulk history, feed historical messages
into Presence, or change the transport's intake cursor. Use `slack_user_info` to
resolve an author ID. `slack_thread` needs the root message's timestamp and also
returns the root when Slack includes it. Follow `next_cursor` with the same
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
History reads remain explicit; the bridge does not backfill old messages.
A delete's `message_id` is the deleted message (`deleted_ts`, else
`previous_message.ts`) and its `thread_id` is that message's original thread;
the deletion's own timestamp stays its `event_ts`, and `deleted_ts` and the
previous message remain provider facts. Deletes already queued by an earlier
version keep the identity they were stored with.
`reaction_added` and `reaction_removed` preserve the reacted message ID,
reaction name and actor. Blocks-only messages are accepted when `blocks` carry
content even if Slack's `text` field is empty. Other bot/app events remain
provider facts and can reach Presence; the bridge drops only its own bot/app
events using the authenticated identity, preserving self-deduplication and
avoiding reply loops. These updates use the existing ordered inbox, host
adapter, and LLM-selected silent/message outcomes; no keyword or semantic gate
is added by the transport.

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
- Retryable refusals and connection failures before sending have at most five
  attempts. An ambiguous write result ends `uncertain` on its first occurrence;
  failed and uncertain items no longer block later messages in the same thread.
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
delivery-reporting mode. Without it no new field is sent and the request waits
for the turn's end exactly as before; rows queued by an earlier bridge version
keep their original references.

A Host may then answer while the author's result still waits for review:
`status: "continuing"` with the author's `continuation_ref`, the initial
`outcome`/`text`/`output_ref`, and a promoted child's own `work_ref`. The bridge
stores that write-once envelope as the event's Host reference; a retried event
receives the identical stored envelope, never a rerun. Like deferred work, the
event then only polls, so later messages in its thread are submitted meanwhile.
If promotion happens on a later reentry, a pending, interrupted or terminal
author poll can supply `child_work_ref`. The bridge checkpoints each discovered
reference beside the initial envelope before polling it. After a restart, the
child remains independently pollable even if the author is unavailable.
The author and the child are polled concurrently through `/presence/work`.
The existing inbound worker commits the initial selection before starting
either poll, then commits each response's outputs as it arrives. A child
result can be sent while an author poll is still awaiting HTTP, and vice
versa. A later message can be submitted while these polls are in flight.
The worker owns and reaps both requests on cancellation; no detached poll
queue is added.
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

A lost author (`status: "interrupted"`) is not restarted and its event is not
resubmitted: once any child has finished, the inbox row ends `failed` with a
visible error, keeping everything already sent.

A Host turn response with `disposition: rejected` or `blocked` is retained
durably, including its HTTP status, code and complete response body. If it carries an admitted `work_ref`
(including HTTP 409), the bridge polls that work and queues its selected result
without resubmitting the original event. Child success does not resolve the
original refusal: once the child settles, the inbox row ends `failed` and retains
the refusal. Refusal text itself is never sent as speech. Without admitted work,
the refusal ends the row immediately. `disposition: retry` without a work reference
continues normal backoff with no attempt cap; an admitted reference always moves
the row to polling. The typed disposition controls recovery independently of
HTTP status class. This handling is confined to turn submissions, not arbitrary
responses from other Host endpoints.

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
as a normal submission), and, when present, `change` or `reaction`.

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
and posts `{binding_id, transport_queue}` to `/presence/work/{continuation_ref}`
before its GET. This uses the original event's `after_source_event_id` and exact
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
