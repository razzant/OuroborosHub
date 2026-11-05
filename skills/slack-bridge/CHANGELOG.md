# Changelog

## 1.10.0

- A direct or group chat is one conversation: its top-level messages carry an empty
  `thread_id` and the Host key `slack:<team>:<channel>:0`, which Ouroboros's own
  top-level sends there already use. A reply to a proactive DM therefore sees it, and a
  second message reaches the answer being prepared. Edits, deletions and reactions of
  such a message, Ouroboros's own reactions included, land there too. A reply in a
  thread keeps its thread, in a chat too; a top-level channel message still opens its
  own thread, and an event without a channel type keeps its per-message conversation.
  The bridge decides this once, at receipt, and the event, queue lane, reply, thread
  read and receipt all read the stored inbox key.
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
  there at top level. Nothing is imported, renamed or resent.

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
