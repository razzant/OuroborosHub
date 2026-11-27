# Changelog

## 0.14.0

- A group can reach Ouroboros through the owner's own Telegram bot. On an Ouroboros advertising
  `telegram_group_feed: 1`, that bot's group updates come from the built-in bridge over the Host relay
  (`/telegram/feed/{identity,updates,call,upload,file}`) into this skill's parser, attachments, inbox and outbox, kept
  in `owner_bot/custody.sqlite3`; no second token is needed. Its private chat, commands, callbacks and Panic stay
  with the bridge. An older Ouroboros can still use its separate bot (`TELEGRAM_PUBLIC_BOT_TOKEN`) without the relay.
  The separate bot waits for the Host's identity response and keeps the original bot of a retained reply when known.
- After getFile succeeds, a wait for file bytes that times out through the owner's bot uses the existing
  bounded attachment retry. This includes a read timeout reported by the native bridge, not an unclassified
  gateway timeout. If retries run out, the original file ID and a failure note remain; the topic moves on.
  The timeout does not establish why the file was not received.
- Every event, reply (immediate or deferred), download, typing request and send of the owner's bot keeps the bot
  and the exact place connection it belongs to (`place_id`, `connected_revision`, topic). Telegram tools take an
  optional `account_id` (manifest targets `[account_id, chat_id]`): a call through the owner's bot names it, and a
  turn that came through that bot is never answered through another. A tool reply in the turn's own chat and topic,
  or an action on its own message, keeps the event's connection instead of binding a newer one. Naming that message
  in a reply keeps its bot; under the owner bot, a different topic is refused. Typing renewals use the bot recorded
  for the originating event in either mode.
- Holds (Silent or Paused, groups Off, a restarting bridge, a busy relay) use no attempt; a refusal before any
  provider call fails; `503 not_sent` and a Host connection that was never made retry; a lost or cut-short answer
  ends `uncertain` and is not sent again, and Host requests never follow a redirect. A row whose connection was
  disconnected or connected again, or whose bot is no longer this profile's (automatic replies of either bot name
  their event's bot), ends `failed` ("Not sent") instead of waiting for ever; an update of an owner's bot that is no
  longer the confirmed one ends before any download and is counted apart from refusals. Retained arrivals wait for
  confirmed A even when they carry only text. B's earlier automatic replies keep their recorded bot; rows that never
  named a bot still do not. The owner bot's sends, edits and moderation record each call before making it.
- The separate bot keeps one order per chat and topic whether or not a caller named it; the owner's bot orders its
  rows per bot and connection and preserves a reaction's topic when the bridge named it. A full feed page that exceeds
  the read limit is retried in smaller pages at the same offset; a single oversized item is reported, not skipped.
  The separate bot contacts Telegram only after Ouroboros has answered, and claims its
  bot before polling where Ouroboros asks for that (`telegram_group_poll_claim: 1`), so one bot is read by one
  reader.
- `GET status` keeps its keys and adds `owner_bot`, `card`, and `ready_wait`. The last field explains why the
  separate bot is not ready, for example when waiting for Ouroboros or poll ownership, distinct from a missing token.
  The Widgets card does not ask for a second token when the owner's bot is enough. If the separate bot's
  token is removed, undelivered and pending records stay visible under the separate bot.
  `GET owner_bot_status` serves the owner bot's place.

## 0.13.1

- The Telegram Transport card on the Widgets page is rebuilt. Its first line answers whether the link works and
  whether anything needs attention: "Connected · nothing needs attention", "Starting…", "Connecting…", "Not set up ·
  the bot token is missing", "Connection error · new messages are not being received" with the time of Telegram's last
  successful answer and the side the last attempt failed on (token, network or Telegram; a token with a space or
  another character a token cannot have is named as such, with advice to correct it, since no request leaves this
  computer), "Error on this computer · new messages are not being processed" when this computer failed (recording
  Telegram's answer, or the bot getMe confirmed), or "Stopped · the bot is not running". The link is judged for the
  token set now: it is green only after a completed successful request for that token, a refusal counts for the token
  that request actually used, and "Connecting…" means no request has completed for it yet (a new token reads so even
  while the runtime still waits out an earlier token's refusal). When several things need attention the first line
  names the worst and counts the rest, for example "Connection error · new messages are not being received · 2 more
  need attention". The Places line for this connector uses the same words and colour.
- Below it, only what needs the owner now, each item saying what it means and what to do: sends that failed or
  whose outcome is unknown, Telegram updates not passed to Ouroboros or still being retried (saying whether passing
  them to Ouroboros or downloading an attachment from Telegram failed, or, when neither was recorded, that it is not
  confirmed here whether they were handled), messages waiting for a place binding, held
  messages with their hold reason, chats Places cannot offer, and reactions that need an Ouroboros update. A hold only
  the owner can end (the skill's access was refused, or the place is not connected) leads the reasons and turns the
  card amber. Failed sends, unknown outcomes and updates Ouroboros refused are listed while they ended within the
  last day; older ones stay in the totals with the date of the last one. Holds, retried updates and the link are
  listed while they last. A held send says what the worker does with it in the link's state, never that it will
  go: it is retried while it waits; without a token, or while Telegram cannot be reached, each retry uses one of its
  5 attempts and after the last it is not sent; nothing is retried while the bot is stopped; its receipt
  (`telegram_receipt`) shows what became of it. The latest error opens inside its item (for waiting updates, the
  latest failed attempt's, or an example when, after a rollback, which failure is the latest was not recorded; a
  note 0.13.0 left on a row that recorded no stage is shown as that note, never as the last error).
  When Telegram confirmed part of a send the card says so; when it confirmed nothing, that delivery could not be
  established. Either way it advises checking the chat and the receipt's `confirmed_messages` before asking
  Ouroboros to send only what is missing; this is advice, nothing is sent again by itself.
- Quiet totals follow: the bot, the last update and delivery, incoming and outgoing counts since install, and the
  current queue (waiting, sending, held). Counts not listed are zero.
- The management group ID and the details are folded. The field and its hint are kept; the button now reads "Save"
  (was "Save transport settings") and the placeholder "e.g. -1001234567890" (was "Optional numeric Telegram group
  ID"). The card checks the value by the server's rule and says "Saved." or "Not saved:" with the reason, naming the
  value sent when the field was changed while it was saving; a saved value stays shown until a newer status read
  confirms it. Details keep every earlier fact: the bot-account explanation word for word, the exact queue counts, the
  Telegram offset, the last error with when and in which loop, and what Ouroboros reported, now worded as the check it
  was, with its time (an unanswered check says so on every row that depends on it; a check that made no request,
  because this skill's Ouroboros access token is unavailable, says that instead). Details › State words the same link
  judgement as the first line, and a bot named while the current token's bot is not confirmed (or the bot is stopped)
  is marked as the last confirmed one. While the custody counts are unknown no row reads as zero or as a total.
- The card says when the status was read and refreshes every 10 seconds while the page is visible, with no limit on
  refreshes; Refresh works at any moment and keeps keyboard focus. The previous card stopped silently after 100
  refreshes. A failed read keeps the last data, says so and offers "Try again". The status and attention repaint
  while the group ID field is being edited, keeping its text, selection and open folds. When the status cannot be
  read at all, the group ID field (with its saved value, read once from `settings/save`) and the bot explanation stay
  available.
- `GET status` gains fields and changes none: `observed_at`, `last_error_at`, `last_error_source`, `provider_ok_at`,
  `poll_result`, `poll_at`, `poll_error`, `poll_failure`, `poll_stage`, `host_checked`, `host_checked_at`,
  `host_unasked`, `places_version`, `bot_unconfirmed`, `link`, `recent_problems` (null when the custody store was not
  read), `handoff_retrying`, `held_reasons` (with `kind` for a hold only the owner can end) and `attention`. Its
  `text` and `tone` now follow the card's first line. The bot, its poll result and the chats offered come from one
  read of the runtime, the last error's text, time and loop from one record, and Ouroboros's capabilities from one
  completed check: a caller that arrives while a check runs waits for it and gets its whole answer (no second
  request), a supported or unsupported answer is kept until restart as before, and a check without an answer, or one
  that could not be asked (`host_unasked`), publishes a fresh record that carries nothing from an earlier one.
  Telegram's refusal of a token stays the link's outcome even when forgetting that token's bot fails on this computer.
- Error texts never carry the bot token. A description Telegram's side returns that repeats the request path has
  the token, as written or URL-quoted, replaced by `<token>`. A token with a space, a tab or another character a
  request line cannot hold is refused before any connection: its error is the type name only (`InvalidURL`, or
  `UnicodeEncodeError` for a non-ASCII character), it chains no exception quoting the URL (which every printed
  traceback would show), and `poll_failure` is `token_format`.
- The custody store gains read-only `card_snapshot()` and `read_status()` (the totals and the card's facts in one
  SQLite read transaction, with one shared pass over failed sends; a stored answer of any shape reads as confirming
  or not, never as an error), and additive changes, made when it opens: `inbox.last_stage` (where a waiting update's
  last attempt stopped) with `last_stage_at` and `last_stage_due`, `outbox.hold_kind` (the Host answer a hold came
  from) with `hold_kind_due`, and the index `outbox_failed_idx`. A mark counts only while its row keeps the
  `available_at` written with it, so rows written before, or by 0.13.0 after a rollback, read as "not recorded"
  (plain waiting, an ordinary hold), never by a stale mark.
- File uploads and sending are unchanged from 0.13.0, apart from keeping the token out of error text. Rolling back to
  0.13.0 restores the previous card; it reads none of the new fields and ignores the new columns.

## 0.13.0

- A file or album send streams its multipart body from disk instead of reading it whole into memory. Framing and
  each file's open size fix the request's Content-Length before any connection, and the body is written in
  fixed-size pieces. The socket timeout applies to each body write and to inactivity while reading the reply, not
  to the whole upload or reply. The bytes on the wire are the same 0.12.1 joined in
  one write; a file that ends before its size stops the request mid-body, which says nothing about what Telegram
  did.
- While a file request runs, its outbox row keeps this process's newest observation, shown as `transfer` in
  `telegram_receipt` (and `queued_at` says when the row was queued): `attempt`, `request` (1, or 2 for the
  plain-caption retry), `phase` — `connecting`, `uploading` or `awaiting_response` — `file_bytes_written` of
  `file_bytes_total` (the last saved lower bound of completed local file writes, framing not counted),
  `started_at` and `updated_at`. The worker tries to save phase changes and the request's end at once, and progress
  between them at most about once a second; each save keeps the row's lease. Only a saved end observation has
  `ended_at` and `answer` (`ok`, `none`, or `refused` with Telegram's `error_code`). A row that ends `uncertain`
  after cancellation can keep its last phase and byte count without either field. Rows with no saved observation
  have no `transfer` field; no progress is inferred for old attempts. Re-reading `telegram_receipt` for the same `request_id`
  during the upload and after it shows the phase, the saved bytes and the answer move. The saved bytes are local,
  never proof that Telegram has them: an interrupted socket write may have emitted part of an uncounted piece,
  reaching the total is not delivery, and the row's state and Telegram's answer decide as before.
- A file send accepts only a whole Bot API answer: `ok` true to a success status, or Telegram's own error — `ok`
  false, its `description` and an integer `error_code` equal to the status — to an error status. A gateway's HTML
  error page, a body cut below its declared length, or a success status without an `ok` answer is no answer from
  Telegram: it ends the row `uncertain`, never a refusal that would permit another call, where 0.12.1 could read
  an error page as "Telegram answered no" and retry it. Multipart POSTs do not follow redirects; an HTTP 3xx
  response leaves delivery uncertain and does not trigger another upload. Text sends, edits, reactions and
  moderation keep their earlier handling.
- On upgrade, a file or album attempt from an older sender without a stored result or proof that nothing was sent
  ends `uncertain` on its next claim, without uploading again. Old refusal text cannot establish the answer to the
  original POST; never-attempted and provably pre-call rows still send, and new verified refusals still retry.
- Rolling back to 0.12.1 reads a file whole again into one write and leaves the transfer observations unused; rows,
  receipts and their states are kept on rollback, but pending uploads are handled under the earlier retry rules.

## 0.12.1

- A send whose outcome is unknown is never made again. Before each text chunk, file or album call the outbox stores
  in the row that the call has started, and removes that record only with Telegram's accepted message, which must
  name its `message_id` (for an album, one per item), or its answer that it did nothing. A lost, cut or unreadable
  answer, an `ok` that names no message, a timeout or reset while the request was written or awaited, a 5xx, a crash,
  restart, cancel or expired lease during the call, or Telegram's yes that could not be stored now ends the row
  `uncertain` with its confirmed parts, where 0.12.0 could send it again or record an empty answer as delivered. Its
  later chunks are not sent and the conversation's next row proceeds. Telegram's 400, 401, 403, 404 and 429, and a
  request that provably never left (unresolved name, refused connection, an upload that could not be built), keep the
  bounded retry. A row its record already decides ends on its claim with no token, `getMe`, Host or place read: one
  whose every part is confirmed finishes `delivered` from the stored receipts, as reported then, and is never ended
  `failed` when marking it delivered fails. Each write a send attempt makes to its row names that attempt, so once
  another claim holds the row (an overlapping reload, an expired lease) the earlier attempt changes nothing there,
  whether it is cancelled, held, failing or late with Telegram's answer; message ids it learns late are only logged.
  Each entry of an album answer keeps its place, so a message is reported for its own item and an entry that is not
  one counts against completeness. Edits, reactions and moderation are unchanged.
- `telegram_send` copies a file when it queues it, unchanged and under its own name, readable by this account only,
  and the row sends that copy after checking its SHA-256 and size. Changing or removing the source afterwards changes
  nothing, a repeated `request_id` keeps the first copy, and the copy stays with the row's history whatever its end.
  `telegram_receipt` names it in `files`. A file that cannot be copied is refused as `file_copy_failed`.
- Rows queued by 0.12.0 or earlier keep their shape. One attempted without proof that the attempt did nothing ends
  `uncertain` on its first claim, without a call, token or Host read; one whose every part Telegram confirmed
  finishes `delivered` from its record, where a stored answer that names no message confirms nothing: it stays in the
  row, is shown in the receipt as `answers_without_message_id` and counts in neither `confirmed_messages` nor
  `confirmed_parts`. A file such a row names is still read from its path at send time and claims no digest. Rolling
  back to 0.12.0 ignores the new records: it may repeat a send this version had open and sends a queued copy by its
  path.

## 0.12.0

- A download failure no longer holds a chat indefinitely. Telegram's own words decide: "file is too big" ends a file
  `too_large` and an unreadable file_id ends it `failed` at once; the generic getFile 400 "wrong file_id or the
  file is temporarily unavailable", a 5xx, a lost answer, a body cut short or of another size than getFile
  declared (never staged) or an expired link retries with the event first in its chat and ends `failed` after 6
  failures that say something about the file and at least 30 minutes since the first. A flood wait counts
  nothing and stops the event's downloads for all of Telegram's delay; a refused token or a network that is down
  counts nothing, so a sleeping Mac fails no file. 0.10.6 already stopped an oversized file from
  holding its chat; other persistent, file-specific failures now have a bound.
- Each `message.attachments` record says `staged`, `too_large`, `failed` or `unsupported` and keeps its
  `file_id`; `note` is one factual sentence on why a file did not arrive, such as "600 MB; the cloud Bot API
  gives bots files only up to 20 MB". Only staged files reach the Host. The 20 MiB limit is one constant, read
  through the client of the configured Bot API server.
- Video, video notes and GIF animations are staged up to that limit, captioned or not; a GIF is no longer also
  staged as a document. Stickers, polls, locations, venues, contacts, dice, stories, paid media, games, invoices,
  giveaways, checklists, and as `other` live photos, rich messages, shared users or chats and web app data,
  arrive as `message.non_text` descriptions instead of being dropped; `event.text` stays the person's words.
  Album members also carry `media_group_position`, counted in a new partial index of album messages that the
  first start after updating builds once (0.5 s per million retained messages measured), and
  `is_automatic_forward` is kept beside `forward_origin`, `quote` and `reply_to_message`.
- `telegram_send` sends video, audio, voice, animation and albums of 2 to 10 files through the same outbox, with
  a receipt part and delivery report per album message and typed refusals above Telegram's upload limits (50 MB,
  10 MB per photo), for captions over 1024 characters and for album mixes Telegram does not allow. An album
  Telegram accepted is never sent again; fewer or more messages than items, or no list, ends `uncertain`, and
  the receipt's `confirmed_messages` lists those returned, as for any send not delivered.
- New `telegram_typing` records a request (it answers `requested`) that the runtime shows as "typing…" in the
  calling turn's own chat while its place may act, read before every showing, until the bot's next message there
  is delivered, even one queued earlier, the turn's end or 2 minutes, all checked again just before each call.
  It is not speech: no outbox row, receipt or report, and a restart never replays it.
- A queued send, edit or automatic reply in a forum topic is held by that topic's own Silent or Paused place before
  each part, as reactions already were; earlier versions read only the chat's place.
- Top-level event keys are unchanged: a Host that does not know `non_text` ignores it, and events 0.10.6 stored
  stay valid. Rolling back to 0.11.x reads every stored row; it ignores the two new custody tables and the album
  index, fetches a `failed` record again, and ends a queued video, audio, voice, animation or album send failed
  after the outbox's five attempts.

## 0.11.2

- `telegram_operation` declares which of its calls deliver words: an
  `editMessageText` call (`method`) changes what people read, so a Host that knows
  the `sends` field treats it as a message to `chat_id`, with its words at `text`,
  and judges, guards and holds it like `telegram_send`. A reaction
  (`setMessageReaction`) stays a write here and keeps `telegram_react` as its
  gesture door. A Host that does not know the field ignores it.

## 0.11.1

- Connect now offers private chats the bot has already seen, labelled with the
  person's observed name, username when available, and numeric chat ID. The owner
  still chooses each chat explicitly; new places start Silent. No place, wake
  choice or reply is created by discovery.
- Private chats reuse the existing custody projection. New updates record ordered
  block/unblock evidence; one resumable pass adds retained private messages.
  Earlier blocks were never recorded, so an old blocked chat may remain offered
  until newer evidence arrives. Rolling back to 0.11.0 keeps private rows visible
  but stops updating their evidence. Group membership, migration and delivery
  records stay unchanged.

## 0.11.0

- New `telegram_react` (`action: set` with one `emoji` or `custom_emoji_id`, or
  `action: clear`) declares `effect: gesture` with `message: message_id`, so a Host that
  knows gestures can offer a reaction on the waking message beside `telegram_send`. It calls
  `telegram_operation` with `setMessageReaction` unchanged: same topic checks, outbox row,
  `request_id` deduplication, receipt and no-resend of an unknown outcome. Nothing new is
  stored. `telegram_operation` stays `write` and keeps `setMessageReaction`. On a Host that
  predates gestures `telegram_react` is an undeclared write and no reaction door is shown.

## 0.10.6

- A photo or document above the Bot API's 20 MiB download limit no longer holds its whole
  chat. Like voice and audio since 0.10.1, it carries `status: too_large` and a note and the
  event goes on with the message reference. A row stored earlier without the mark, and a
  file Telegram refuses with "file is too big" when the update did not size it, take the
  same path, as does a body longer than the limit after `getFile` accepted it. Before this
  a 487 MB archive kept a work group's 16 later messages, two of
  them @mentions of the bot, waiting behind 203 download attempts for a day. Any other
  download failure still retries and keeps the event first.

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
