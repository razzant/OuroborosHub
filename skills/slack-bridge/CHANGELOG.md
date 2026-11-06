# Changelog

## 1.10.0

- Add the opt-in `signal.new_root` wake fact for top-level, subtype-free Slack
  messages. Replies, edits, deletes and other subtypes do not receive it; the
  existing message, mention, direct-message, thread and bot facts remain intact.

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
