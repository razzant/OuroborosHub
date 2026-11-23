# Changelog

## 1.13.0

- The Widgets card is redrawn as one surface (a module `widget.js`). Its first line says whether
  the link works and whether anything needs attention ("Connected · nothing needs attention",
  "Connected · 3 messages not delivered"); the Places sheet shows the same words and tone. What
  needs attention is listed below it, worst tone first (error, then warn, then muted) and within
  one tone by consequence: the link, outcomes nobody knows, what was not delivered, what was not
  handled, what is held, notes, what is only partly known. The first line names the first item,
  so its dot and its words agree, and adds "· N more need attention". Each item says what it means
  and what to do, with its time when the store recorded one; the raw error sits in a fold beside it. Quiet totals since install and a Details fold
  keep every fact the old card showed. Credentials and message contents are still never shown.
- The card shows when the status was read and keeps refreshing every 10 seconds while it is
  visible. A failed read keeps the last data and says so with "Try again"; the old poll stopped
  silently after about five minutes.
- "Outcome unknown" (the bridge has no saved final result for a send, edit or deletion, whatever
  the reason) is its own item and is never called a failure: nothing repeats it, and the owner
  checks in Slack whether it took effect before asking for it again. Problems are
  attention for 24 hours; older ones stay in the totals with their date. A connected bridge whose
  state is only partly known (Ouroboros has not answered its capability check) never reads as
  all clear.
- `GET status` gains additive fields: `observed_at`, `link` (`state`, `detail`, `companion_alive`,
  `since`, `error`; never `connected` while the companion process is dead), `recent_problems`,
  a structured `last_error` built only from failed or uncertain rows, `outbound_text_chunks`,
  `outbound_messages` (logical messages, not 3,900-character parts) with `outbound_messages_last_at`
  (when the newest message of the not-delivered and the unknown class got that outcome, so the date
  beside a count belongs to the messages counted; an older payload's date is labelled as the last
  part outcome), `units` and `attention`;
  `held_reasons` (every reason holding outbound rows, with its count), `inbox_retrying` (events
  Ouroboros has not confirmed receiving, which the bridge retries, from the queue rows), and per-message
  `failed_messages_partly_delivered` / `uncertain_messages_partly_delivered`, so the card never
  advises sending a message again whole when some of its parts already arrived. A message with a
  failed or unknown part counts as a problem even while another of its parts still waits or is
  held; `failed_messages_still_queued` / `uncertain_messages_still_queued` and
  `outbound_messages_with_problem_parts` count those messages, so the card advises waiting for the
  queued parts and sending only the refused ones instead of sending the message again.
  `failed_messages_others_delivered_or_unknown` and `failed_messages_every_part_refused` class the
  other refused messages (check `slack_receipt` and send only the refused parts, or send again),
  `uncertain_messages_partly_delivered_none_queued` and `outbound_messages_with_recent_problem_parts`
  keep the other advice to the messages it fits. `failed_messages_in_parts` and
  `uncertain_messages_in_parts` count the messages sent in several parts, and
  `uncertain_messages_with_refused_part` those unknown-outcome messages that also had a part refused;
  `failed_messages_still_queued_partly_delivered` and `uncertain_messages_still_queued_partly_delivered`
  those still-queued ones that already have a part in the conversation, so the card says both.
  A message in several parts with a refused or unknown part gets one advice, part by part (wait for
  its queued or held parts, check each part with `slack_receipt`, check unknown parts in Slack itself,
  send again only the parts confirmed missing), whatever the age of that part and the state of the
  others; only a single-part message or an action is advised to be sent or repeated whole. The
  24-hour window applies to each outcome's own
  time, and an item's error and time come from the messages it counts. Each queue in `recent_problems` also gains `last_failed_error` and `last_uncertain_error`
  (the newest error of that state among the messages its item counts inside the 24-hour window; a
  message with both a recent refused and a recent unknown part counts as unknown, so its refused
  part's error is not `last_failed_error`), so each item carries its own error;
  `last_error` stays. `mutations_leased` counts actions in progress and `host_adapter_checked`
  says whether a companion ever recorded its view of the Host binding. All of it is read in one
  transaction, with one grouped pass over the outgoing text rows. `text` and `tone` keep their keys; their words and tone now follow the rule
  above. Every other existing key keeps its name and meaning.
- Token advice appears only when Slack refused the tokens (the bridge's own refusal codes); any
  other connection error is shown as itself. A refusal of the app-level token after sign-in now
  reaches that advice: the daemon records its final error after cleanup instead of leaving
  "disconnected". When several reasons hold messages, one the owner must act on (the skill's access
  refused, a place not connected) comes first and sets the amber tone; a paused or silenced place
  and a Host that did not answer stay neutral.
- Details no longer says Ouroboros needs an update for reactions when it simply did not answer the
  capability check. Whichever request finds the Host binding again after a failed check (an event's
  hand-off, a place check before a send, a report) records it once, so "events passed on" and the
  Places line follow it; overlapping checks that end in either order leave what is recorded equal
  to what the bridge knows. A check Ouroboros did not answer is not read as a missing place: the
  card says the next event or send checks again. A bridge without an Ouroboros service key
  (`HOST_SERVICE_TOKEN`) asks nothing, so it is not said to be asked again: `host_check_cause`
  (`credential_missing` or `request_failed`) carries the reason, and the card advises turning the
  skill off and on, which starts the bridge with its key. Only an answer from Ouroboros changes what the
  bridge knows about it: a failed request after a confirmed answer keeps that answer, and a
  recording that failed (a locked database) is written again at the next check without a new request.
  While the bridge is not confirmed running, the Host facts read as its last
  check, and a store that can't be read leaves every store-based row "unknown".
- A token could reach the card through error text (an HTTP library prints the `Authorization`
  value of a token with an illegal character); stored error text and every free-text error field
  of `GET status`, records of older versions included, now leave without Slack token shapes, values
  after `Bearer`/`Authorization` and the configured tokens (also without their outer spaces). A token
  shape is found anywhere, also right after an underscore or a channel ID. A value
  after `Bearer`/`Authorization` or a token shape is removed up to its closing quote, escaped and
  control characters included, so no tail of a split token stays; a `ticket` or other credential
  query parameter in a URL is masked and the rest of the URL kept. Every field the card shows passes
  through the same cleaning, the workspace name and the sends-as label included. Log lines of the
  bridge and of the libraries it uses (websockets, httpx, asyncio), and Python's own printouts of an
  uncaught exception (the main process, threads, asyncio tasks, finalizers, warnings), go through the
  same cleaning; an error that ends the bridge is logged that way and the process exits with status 1.
  Every report to the Host leaves without credentials in its free text, built now or queued by an
  older version. Its identity and routing (delivery, part, account, conversation and thread IDs,
  state) are never changed, so two reports never merge into one receipt. Its `text`, the copy of the
  message, stays exact for a delivered or accepted part, which Ouroboros matches against the turn's
  reply, and is cleaned for a failed or uncertain one. A 409 `presence_receipt_conflict` for a
  cleaned copy means Ouroboros already holds a receipt for that same part and state (its key is
  skill, account, delivery, part and state), so the report is done; any other 409 stays pending.
  The settings form never echoes a rejected value: it names the field and the entry's position.
  The message posted to Slack is never changed. The earlier 1.12.x bridge-hardening findings stay as they were.
- `GET status` answers with string `text` and `tone` on every path, including a queue store that
  can't be read ("Status unknown · the bridge's queue store can't be read").
- 1.13.0 introduces the companion liveness probe (POSIX signal 0; on Windows `OpenProcess` and
  `GetExitCodeProcess` with declared signatures, never signal 0; a failed exit-code query reads as
  not known and the card says the state is partly known) and the compare-and-clear of
  `companion_pid`: an exiting companion writes its final state and clears its PID in one transaction,
  only while the PID is still its own, so a newer companion's PID and state stay. The newest problem
  is chosen by comparing instants, not ISO strings of varying precision.
- Residuals: a reused PID can still read as alive (there is no companion heartbeat), and on POSIX
  an exited child not yet reaped reads as alive until it is; "waiting unusually long" is not shown
  because status reports no age of the oldest waiting row; each status read still groups the
  whole outgoing history by message, so its cost grows with history (on a synthetic snapshot of
  40,000 parts the median read took 46.3 ms against 11.0 ms in 1.12.2, at 1,000 parts 1.15 against
  0.22 ms; a later measurement gave 41.31 against 9.33 ms and 1.25 against 0.28 ms); earlier review findings on
  bridge hardening are not addressed here.

## 1.12.2

- File uploads now recover by themselves before anything is shared. Each physical upload takes
  a fresh slot from `files.getUploadURLExternal`, streams the staged copy in 256 KiB writes with
  its length declared, and ends with `files.completeUploadExternal`, the one call that shares the
  file. A lost or malformed answer, a transport error, HTTP 408, 429 or 5xx, or a length mismatch
  before completion is retried with a new slot, at most three uploads in all; an explicit refusal
  or an unreadable staged copy is final. Once completion has started, an unknown outcome stays
  `uncertain` and is never repeated, and only 429 or a request that never left retries. In 1.12.1
  a failed byte transfer was recorded `uncertain` without its file ID and the file was never
  sent: a 42 MiB upload ended that way ([Hub #301](https://github.com/razzant/OuroborosHub/issues/301));
  its network cause is not established.
- The byte transfer used a single write and could exceed the client's 30-second write timeout
  on a healthy slow link once socket buffers filled. Each 256 KiB write now has its own
  timeout. Only HTTP 200 counts as accepted, an `OK - N` answer naming another length is
  refused before completion, and a redirect from the upload host is not followed.
- A running upload keeps its claim: the 120-second lease is extended every 30 seconds through the
  body, a slow answer and completion. Before, a second worker could settle the row `uncertain`
  while the first went on to share the file, and the first worker's next write raised and ended
  its loop ([Hub #123](https://github.com/razzant/OuroborosHub/issues/123) describes the class).
  A worker whose claim lapsed or was replaced before completion now stops there and ends without
  writing; every outbox write checks its claim and raises a typed `OutboxLeaseLost`.
- `slack_receipt` lists each physical upload in `upload_attempts`, a new outbox column: the
  provider `file_id`, phase, declared and sent lengths, byte and completion outcomes and a failure
  class. An identity from an incomplete slot response is kept too; `completed` does not claim a
  conversation share when no channel was requested. Upload URLs are not stored, and the request log now masks every URL that is not a Slack
  Web API method, so an upload URL no longer reaches the companion log.
- `slack_api` refuses `files.completeUploadExternal` for any effect, verb or spelling, including a
  row an earlier version queued; files are shared with `slack_file_upload`. Completing an upload
  started outside the bridge, or sharing several files in one call, is no longer possible. Slot
  allocation and every other method are unchanged. The dedicated tool explicitly identifies
  `initial_comment` as its message text for the Host.
- Rows from earlier versions keep their outcomes: an `uncertain` upload stays so, an upload that
  1.12.1 marked before its slot is recovered as `uncertain`, and a pending one continues under the
  new rules.

## 1.12.1

- `slack_api` declares which of its calls deliver words: the Slack Web API methods that post
  or change a message (`chat.postMessage`, `chat.update`, `chat.postEphemeral`,
  `chat.scheduleMessage`, `chat.meMessage`), with the destination channel, thread and text
  at `body.channel`, `body.thread_ts` and `body.text`. A host that reads the `sends`
  declaration treats those calls as messages to people: the Safety Supervisor judges them as
  speech, the place's notes and Silent/Paused state apply, and a message the Supervisor could
  not judge waits instead of leaving. On 4 November eight messages reached colleagues through
  this generic tool after the dedicated send was held, because the host saw only a write.
  Reactions, pins, bookmarks and reads are unchanged. A host that does not know the
  declaration ignores it.

## 1.12.0

- `slack_reaction_add` and `slack_reaction_remove` declare `effect: gesture` and
  `message: ts`, so a Host that knows gestures can offer a reaction on the waking message
  beside `slack_send` as a third form of response, next to words and silence. Their
  registered descriptions now say what a reaction is for and give `name` an example. On a
  Host that predates gestures the unknown effect is an undeclared write, refused by Silent,
  Paused and Advises as before; arguments, queue and receipts are unchanged.

## 1.11.2

- A thread reply is `my_thread` when this bot wrote the thread's root or a message of the
  captured thread, and never `reply_to_me`. A Slack thread is flat: every reply names the
  root's author as its `parent_user_id`, and no message names one it answers. Every reply
  under a root the bot wrote was still reported as a reply to the bot, so the Host told
  the model the event addressed it and kept the words as addressed to it: on 6 November a
  message the owner wrote to a colleague in a thread Ouroboros had opened was answered and
  turned into work. The place no longer declares or suggests `reply_to_me`; a place that
  kept it without `my_thread` wakes on those replies again once My threads is checked.
- Mentions and thread authors carry names. `message.mentions` lists each explicit mention
  as `{id, name, is_self}`, and `conversation.thread_history.authors` the captured thread's
  authors the same way, so a reader sees who is named and who said what without a lookup.
  A name comes from one this credential observed within a day (an earlier `users.info`
  read or the `slack_resolve` directory scan), else from one `users.info` read: at most
  four at once, within one ten-second budget and none after Slack limits the rate, kept
  as one small row per person. A person left unread is `null`. `provider_facts` are
  unchanged.
- A revision that changes only Slack's bookkeeping is not an edit. A root's thread state
  (`reply_count`, `reply_users`, `reply_users_count`, `latest_reply`, `is_locked`, the
  bot's `subscribed` and `last_read`, the root's own `thread_ts`), the editor's
  `source_team` and `user_team`, and a regenerated `block_id` join `language` and
  `edited`. Deleting a reply re-sends its root with only the read mark gone, and on
  6 November that woke a turn as a new message from the root's author. Of the 1,009
  revisions one install's bridge had accepted, 496 changed no words, files or
  attachments; they are now ignored as `message_content_unchanged`. New words, files,
  link unfurls and a deleted root's tombstone still reach the model.

## 1.11.1

- `from_bot` follows the author's account, not the client that posted. A person writing
  through an app (an AI assistant posting as them, for example) carried `bot_id`,
  `app_id` or `bot_profile` and was reported as a bot, so a direct message or a reply in
  Ouroboros's own thread never woke it: in one colleague's DM, 8 of 42 messages, all of
  them his substantive updates, waited for the next own wake. The account's `users.info`
  `is_bot` now decides; the markers decide only when Slack names no person or the lookup
  is unavailable. They stay visible as `provider_facts.posted_via_app` (stored rows keep
  their earlier `actor_is_bot` name and are read the same way).

## 1.11.0

- A direct or group chat is one conversation: its top-level messages carry an empty
  `thread_id` and the Host key `slack:<team>:<channel>:0`, which Ouroboros's own
  top-level sends there already use. A top-level answer to a proactive DM therefore
  sees it, and on a Host with continuation version 2 a second message reaches the
  answer being prepared. Edits, deletions and reactions of
  such a message, Ouroboros's own reactions included, land there too. A reply in a
  thread keeps its thread, in a chat too; a top-level channel message still opens its
  own thread, and a message or mention without a channel type keeps its per-message
  conversation. The bridge decides this once, when an event is received, and inbound
  readers take the conversation stored with it: the event, its queue lane, the reply
  and its receipt, the thread read and the output identity. A reaction takes its
  target's conversation from that message's retained record, and from one
  `conversations.replies` read only when none is stored; the receipt of Ouroboros's
  own top-level channel post uses the channel type stored from a received event.
- The automatic answer to a top-level message of a direct or group chat is a plain
  message in the chat, no longer a thread under it. `slack_send` still places a
  message anywhere, with or without `thread_ts`.
- The receipt of Ouroboros's own top-level channel post names the thread the post
  opens once a received event has stated the channel's type, so the post is recorded
  with the replies under it. A post sent in several parts, or into a channel no event
  has described, stays at the channel's top level. Nothing a person sees changes.
- A top-level message of a direct or group chat no longer reads
  `conversations.replies`, which returned only that message.
- Upgrade: events received before the update keep the conversation they were stored
  with, and with it their retries, reply thread and output identity. Earlier
  per-message conversations of a direct chat stay as the conversations of the threads
  under those messages; the chat's own conversation starts with what Ouroboros sent
  there at top level. Nothing is imported, renamed or resent. Rolling back to 1.10.1
  or earlier while a top-level message that 1.11.0 stored for a direct or group chat
  still waits makes a Host with continuation refuse that event, and the bridge
  records it as failed, so roll back with no such message waiting.
- Restores what 1.10.0 dropped. It was published from a local copy of 1.9.0, so it
  reverted the changes of 1.9.1 to 1.9.3: hold lines that say what a hold means and
  only the next step, a skill the Host refused waiting for its owner, and claims
  that stay idle without write transactions (see 1.9.2 and 1.9.3).

## 1.10.1

- Every event carries `message.sent_at`, Slack's send time of its message (for a
  reaction, when it was made), and an edited message `message.edited_at`, when its
  words were last edited.

## 1.10.0

- New top-level posts in channels listed by exact ID (`SLACK_NEW_ROOT_CHANNELS`) carry
  `signal.new_root`, an opt-in wake, sent only to Hosts that advertise
  `presence_wake_signals: 1`. Published from a local copy of 1.9.0, this release
  lacked the changes of 1.9.1 to 1.9.3, and so did 1.10.1.

## 1.9.3

- A hold line says the state and the next step, not the Host's answer behind it. An
  unread place policy reads `Held: It retries by itself.`, a refused skill
  `Held: Check this skill's access in Skills.`, and a held reaction report that
  Ouroboros records it after an update; SKILL.md says which Host answer each means.
  A place's state and the Host's "not connected" read as before.

## 1.9.2

- Keep one store connection open instead of opening one for every call. A companion
  worker with nothing to claim now only reads: it takes no write lock and writes nothing.
  An outbox claim still first settles a send that may have reached Slack as `uncertain`,
  never to be sent again, and still moves reaction rows an older version left in their
  channel's lane; which rows are leased, in what order, is unchanged. Every commit stays
  `synchronous=FULL`. The companion closes its connection when it stops, including
  startup or cleanup failures. Discarding a temporary tool store closes its connection too.
- The status route and settings saves read and write their files beside the Host's HTTP
  loop, not on it. Two settings saves at once still keep each other's fields.
