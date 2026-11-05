# Changelog

## 0.10.5

- Every message carries `message.sent_at`, Telegram's send time as an ISO time, and an edit
  `message.edited_at` from `edit_date`, so the Host can say when the person wrote instead of
  "at an unrecorded time". Reactions keep theirs; `sent_at_epoch` stays.

## 0.10.4 — unreleased

- A hold line says the state and the next step, not the Host's answer behind it. An
  unread place policy reads `Held: It retries by itself.`, a refused skill
  `Held: Check this skill's access in Skills.`; SKILL.md says which Host answer each
  means. A place's state and the Host's "not connected" read as before.
- A held reaction report says whether Ouroboros records it after an update or never.

## 0.10.3 — unreleased

- Keep one custody connection open instead of opening one for every call. A poll with
  nothing to do is now a plain read: it opens no file, takes no write lock and writes
  nothing, so the waiting workers no longer recreate the custody WAL file several times a
  second on the Host's event loop. A claim with work still decides under `BEGIN IMMEDIATE`
  with the same compare-and-set, so which rows are leased, in what order and with what
  attempts is unchanged. Every commit stays `synchronous=FULL`, now set on that connection
  itself: the `NORMAL` the schema setup asked for never applied to working writes and does
  not now either.
- The runtime, the status route and the tools of one process share that store, and its
  file is set up once per process. The runtime closes it when it stops or is unloaded,
  after its workers return their leases, including failed startup. Discarding a temporary
  store closes its connection too; the next use of a retained store opens it again.
- The status and settings routes read and write their files beside the Host's HTTP loop,
  not on it. Two settings saves at once still keep each other's fields.

## 0.10.2 — unreleased

- Say what a hold means when the Host did not say whether a place allows sending:
  `Held: Ouroboros did not answer whether this place allows sending. Nothing was sent
  yet; it retries by itself and no approval is needed.` `Held: place policy unavailable`
  read like a pending approval. A Host's answer that the place is not connected keeps
  its own words, which say what to do. A place's state reads as before.
- A Host that refuses the skill itself (no grant, turned off, unreviewed or a stale
  token) waits for its owner, not for a retry: `Held: Ouroboros refused this skill's
  request, so nothing was sent; check the skill's access in Skills.`

## 0.10.1 — unreleased

- Deliver voice messages and audio files as ordinary attachments (`kind` `voice` or
  `audio`, with `duration_sec` when Telegram gives it). A voice-only message without
  a caption is no longer dropped. The original bytes are staged unchanged under a
  name whose suffix matches their type, so the Host's byte classification can tell
  the audio apart. No transcription, no new wake fact, no text is made up.
- Mark a voice or audio file Telegram declares above the Bot API's 20 MiB download
  limit `too_large` with a note and do not try to download it, so it cannot hold its
  conversation.

## 0.10.0 — unreleased

- Send incoming reactions as the typed `message.reaction` fact (schema version 1)
  when the Host advertises `presence_reaction_context: 1`, including anonymous
  `message_reaction_count` totals and chats acting as themselves. Keep Telegram's
  raw arrays and `date`. Elsewhere keep them in custody without delivering them
  or replaying them later.
- Prove a reaction's topic, author and excerpt only from an exact
  account+chat+message record of what this bot received or sent. An unproven forum
  target stays `unresolved`.
- Describe `setMessageReaction` correctly: it is not limited to own messages. Refuse
  more than one reaction and paid reactions, refuse a forum target without a known
  topic, and keep `topic_id` local.
- Resolve a reaction's target the same way in a pooled worker as in the polling
  process, from the bot `getMe` last confirmed for the current token, kept in custody
  with a digest of that token. A changed, refused or never-confirmed token reads no
  record: the refusal names the unconfirmed identity, and an explicit topic or a
  private target stays `unresolved`.
- Report own reactions as typed non-speech receipts only when the Host advertises
  `presence_explicit_completion: 1`, including reports frozen earlier, which wait
  unsent until then and go in the typed shape. Settle 400/403 at once, retry only
  401/404/429, and treat a lost or unreadable response, 5xx, any other code or a restart
  mid-call as `uncertain`, never sent twice. That includes a reaction row 0.9.0 left
  leased, released or cancelled with no recorded result, or released for a retry
  without proof that Telegram did nothing, whether before the upgrade or during a
  rollback to 0.9.0.
- Keep a reaction Telegram confirmed when recording it fails: the row finishes from
  that result with no token, Host or second call. A failure before the call stays an
  ordinary retry, and an unreadable `error_code` counts as no code.
- Name a retained album in a reaction's receipt and report, with the member that took
  it left unknown. Keep an explicit `topic_id` for a private target with no record,
  and keep the topics a private chat has shown when a later message is outside one.
- Recheck a queued reaction in its `topic_id`, so a topic whose own place is Paused
  after queueing holds it. A reaction without a topic, and every other send and edit,
  is checked as before.

## 0.9.0 — 2026-10-29

- Offer each group and supergroup the bot has been seen in by name when a
  Telegram place is connected: the status route returns
  `connect: [{account_id, conversation_id, label}]`, labelled with title and
  numeric ID, one row per group across its topics, ordered by title, without a
  limit. Private chats and channels are not offered.
- Keep each group's ID, type and title and the bot's newest membership evidence
  in custody, committed with the update's event and offset. Request
  `my_chat_member`. The bot's own addition, removal and re-addition, renames
  and supergroup migrations update the list only; they never become events,
  turns or places. Telegram's update order decides, so an older message never
  undoes a later removal, rename or migration.
- On upgrade, add the groups of events already in the inbox once, in batches,
  without changing custody rows; count unreadable events as
  `group_backfill_unreadable`.
- Offer only the bot `getMe` confirmed for the token configured now; otherwise
  `connect` is `null` with an unavailable-identity explanation, while transport
  status retains provider errors. Updates fetched for the
  earlier bot keep its account when another worker confirms a new token
  meanwhile.

## 0.8.0 — 2026-10-29

- Request Presence continuation version 2 when `/identity` lists it in
  `presence_continuation_versions`. Store the early `continuing` answer's real
  reference with the submitted event, move the conversation's input queue on and
  poll the author with the existing work worker. Other Hosts keep completed-only
  requests.
- Startup bursts and corrections sent while the author reasons reach it as
  context and are acknowledged without speech.
- Retry a busy `presence_capacity_full` admission (`conversation` or `capacity`)
  as the same first event after the Host's `retry_after`, never shortened (at least one second),
  without a growing delay or a counted attempt, while other
  chats proceed.
- Reply to the covered message a version-2 `reply_anchor` names when this chat
  captured it; promoted children, retained references and older Hosts keep
  answering the original event.
- Send each `output_ref` once per event across the author, its children, the
  original turn and retained references.
- Show the negotiated `presence_continuation_version` in transport status.

## 0.7.0 — 2026-10-26

- Capture private chats that people open with the bot on Places hosts and report
  their messages with the declared `direct_message` fact. It joins the suggested
  wakes; existing place wake lists are not changed by the upgrade.
- Check replies to a private chat against its place like any other place instead
  of holding them as unsupported.
- Explain in the transport tab what the bot can reach and that the Bot API cannot
  read earlier history; a dedicated account is the separate
  `telegram-user-session` connector.

## 0.6.3 — 2026-10-26

- Add the connector's `words` setting, empty by default, with one literal word
  or phrase per line and saved-value loading in the settings form.
- Report `signal.words` on matching message text and captions, including edits,
  using case-insensitive whole-word matching. Retain the fact with the incoming
  event; negotiate signal support before sending it to the host.
- Declare the signal's label in the manifest. The owner selects it per place;
  wake suggestions, bot gating and Silent reply/write holds stay as before.
- Share settings reads between the form and poller; preserve binding and group
  settings when only words change, and preserve words when those settings change.

## 0.6.2

- Declare the bot's sending identity from Telegram `getMe` on status and events
  when the host advertises identity support.
