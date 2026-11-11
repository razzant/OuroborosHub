# Changelog

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
