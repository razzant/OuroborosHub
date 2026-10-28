---
name: telegram
description: Owner Telegram bridge, Mini App gateway and Telegram private-chat place for the existing Ouroboros interface.
version: 1.4.0
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
os: any
permissions: [net, read_settings, widget, route, supervised_task, subscribe_event, inject_chat, subprocess, companion_process, presence, tool]
env_from_settings: [TELEGRAM_BOT_TOKEN]
subscribe_events: [chat.outbound, chat.typing, chat.photo, chat.video, chat.document, chat.links, chat.quiz, chat.quiz_state]
conflicts: [telegram-bridge, telegram-miniapp-poc]
when_to_use: The owner wants to communicate with and control Ouroboros through Telegram.
icon: message-circle
tools:
  - name: hand_to_main
    effect: write
  - name: telegram_message
    effect: message
  - name: telegram_news_read
    effect: read
  - name: telegram_news_publish
    effect: message
contributes:
  - id: place
    provides: place
    label: Telegram
    destination: vendor:Telegram
    scope: account
    wakes: [direct_message]
model_experience:
  what_model_sees: Without a connected Telegram place, Telegram owner text, photos and files enter normal chat. In a connected private chat they arrive as place events; there a focus may pass the owner's own message verbatim to Main owner routing (hand_to_main) and act in that chat (telegram_message). Roots may read waiting Project news and tell the owner what matters (telegram_news_read, telegram_news_publish). Replies to mirrored messages carry host reply references for the mind to interpret, never routing instructions. Quiz answers use the web card's decision ingress. Answers to earlier owner messages use native Telegram replies.
  token_effect: Near-zero while idle; four small tool schemas. Incoming Telegram media arrive as ordinary attachments and cost what any chat attachment costs.
timeout_sec: 60
companion_processes:
  - name: miniapp_gateway
    command: [python3, scripts/companion.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 5
---

# Telegram

One owner-only Telegram integration provides both the established bot bridge
and the optional Mini App. The first positive private Telegram chat binds as
the sole owner. Text, photos and files (documents, video, audio and voice notes within
this integration's 10 MiB download cap) can be sent to Ouroboros, with or without a
caption; replies, photos, videos, documents, typing state, subagent cards, quiz
cards, and opt-in notifications are mirrored back to that owner. Telegram's typing
indicator stays on while a reply you asked for, mirrored to this chat, is actively
being prepared by Ouroboros or a working helper; it stops for Pause, Stop now and
after the answer, and a question waiting for you, a model-access wait or background work
never keep it on (DESIGN §9). A quiz card is
answered by tapping an option or by replying to the card with a free-form
answer; both reach the same host decision ingress as the web UI. The bridge speaks
the install's interface language (Settings → Appearance → Language, or `/language`
here): its own lines are English source translated by the install's translation
memory, so the bot ships no dictionary and follows any language the owner chooses. Owner `/panic`
is available in every command mode, including strict: it sends once through the emergency door,
never queues or replays. Telegram dates before the host's whole-second import time are refused;
their previous outcome is unknown. This assumes aligned clocks: a host ahead rejects fresh Panic
until Telegram reaches that second; a host behind can accept retained commands. Dates in the
boundary second are accepted. Other slash commands keep their command-mode rules even in replies; quote a literal command
as code or include it inside an explanation to send it as a quiz answer.

Version 1.3 remembers every mirrored part, including media and completion
notices, in a bounded durable message map. Replying passes its host row or work
reference through normal chat; Ouroboros decides what it means. Unknown or
evicted ids add nothing. Answers to an earlier owner message from this Telegram
chat use a native reply; answers to its latest owner message stay unquoted.
Silent mode edits only within one task episode and reply origin; Continue
episodes remain separate messages. Relay success stays silent; a host-reported
failure arrives as one line. A relayed row quotes the owner's words verbatim as
plain text, under where he wrote them and with the file names, above the note; a
note between works that quotes none of his words stays in its room.
Quiz buttons and replies keep their decision path; earlier parts of a long
question carry ordinary references.

Version 1.3.2 warns when the derived reference map cannot be saved, without
blocking an owner message or later outbound parts. Completion notifications
reference their work, not an authored answer shown only in Main.
Version 1.3.3 gives a finished root task's notice the task card's cost words for
its whole tree: `$3.42` when final, `up to $3.42` while costs may still arrive,
`Tracked:` when some step has no price; an unknown cost adds nothing.
Version 1.3.6 sends Wrap up's saved-work receipt, which the host types as a terminal
receipt, even with progress mirroring off; other card rows still follow that toggle.
Version 1.3.7 sends no finish notification for a run that finished quietly.

Version 1.2.1 marks questions that wait for an owner answer and clears the
waiting line after an answer submitted through Telegram.
Version 1.2.3 serves the stored settings on GET settings/save so the Settings
form shows saved values before Save, and shortens the four long option labels.
Version 1.2.4 introduced a non-clean completion notice even with the task toggle off;
version 1.2.14 replaces that policy with a true opt-out (below).
Version 1.2.5 shows the whole quiz card: its project, the host's facts about the
asking task, and every option's detail, with localized field names; a card too
long for one Telegram message arrives as ordered parts followed by the keyboard
message, and nothing authored is cut.
Version 1.2.6 edits a sent question card on its lifecycle (`chat.quiz_state`): a
web answer settles it, a closed wait drops the waiting line yet keeps buttons,
a finished task says a late answer still counts, and (1.2.13) a question closed
unanswered states that and its reason without buttons. Answers and closures are final.
An open question (no options) is the same whole card without buttons; it asks for
a reply in your own words.

Version 1.2.9 routes every Telegram API call through the optional skill-local
`TELEGRAM_PROXY` setting, including polling, sends, downloads, notifications,
and the companion's menu button lifecycle. The Settings form masks the proxy
and never reads its stored credentials back into the browser.

Version 1.2.10 also forwards the owner's notes and skill notices (`reminder` and
`skill_notice` System rows, written for the owner) to the pinned chat when the
mirror mode is `telegram_only`; mode `all` already mirrored them.

Version 1.2.11 speaks the install's interface language: `/language` lists English
and the languages the install already has, `/language <name or tag>` chooses one
for the whole installation through the host's one writer, and the bridge's own
menu, buttons, card hints and notifications are read through the install's
translation memory (a line it lacks is generated like a browser miss). The
retired bridge-only `TELEGRAM_LANGUAGE` setting is migrated once.

Version 1.2.12 starts the Mini App when the Ouroboros server is a container's
PID 1 without an init. PID 1 counts as the server only when the host named
itself so; a companion adopted by init or a subreaper still stops.

Version 1.2.14 makes `TELEGRAM_NOTIFY_TASKS=off` suppress every additional task
completion notice, failures and cancellations included. On sends completion
notices with the host's material causes; pure review provenance and missing
host-verification receipts stay in task details, without declaring success.
Required, unresolved or unknown review obligations remain disclosed. Previously
stored Off values now mean no extra notices; no setting is migrated or added.
The status-only Main mirror follows this toggle too; preserved intermediate
content still arrives. Ordinary replies, files and questions are independent, as is
`TELEGRAM_SILENT_MODE` (editing consecutive replies). IDs observed while Off
remain seen, so enabling does not replay that backlog. Older summary rows without
the host's compact cause projection retain their original reason text when On.

## Telegram private chats as places (1.4)

Version 1.4 can make a private chat with this same bot a place (Consciousness > Places), with its own room
history, while the bridge stays the one receiver. Two settings choose it, and both default to the classic
behavior, so an update changes nothing until the owner opts in. **Ordinary messages**
(`TELEGRAM_ORDINARY_CHAT`): `main` (default) relays every ordinary owner message to Main exactly as before,
with no host lookup; `place` sends them to a connected Telegram place. **Mirror mode** (`TELEGRAM_MIRROR_MODE`)
gains the connected mode `main`, described under Mirror.

**Which road a message takes** (with `place`). Slash commands (`/menu`, `/panic`, …), every button and every
quiz answer (a tap or a reply to a question card) keep their immediate paths and never wait for a turn. Any
other private message asks the host whether a place covers that chat (`GET /presence/place`). A connected place
(any state) makes it a place event. Only the host's exact "not connected" answer keeps the classic Main relay
for the owner. Any other answer, an authentication refusal or a not-yet-known bot account included, keeps the
message in this bridge's custody, logs why and asks again: it is never sent to Main on an uncertain answer, and
later messages of that chat wait behind it. A message that entered the place road keeps it: if the place is
disconnected afterwards, the message is held for a reconnect, never rerouted. Replies to mirrored Main or
Project messages are place events too, carrying the host reference of the replied-to message
(`message.reply_to.reference`; `unknown` when this bridge no longer maps that bot message, never a guess). A
connector-wide place also admits other people's private chats with the bot, each its own conversation without
owner powers; connect with the owner's chat id (`configure_presence connect`, `account_id` = the bot id,
`conversation_id` = the owner chat id, both shown in status) to admit only the owner's chat.

**Custody.** The poller commits each place message to `places.sqlite` in this skill's state before the Telegram
offset moves. What is kept then is the provider message with Telegram's durable file ids, not file bytes. A
separate supervised runner downloads the bytes once, keeps them with their digest and reuses them for every
later request until the host has answered (and so holds its own copy), then releases them; a download that
fails in transport keeps the message and is tried again, also after a restart. A file Telegram refuses or that
is over the 10 MiB cap is admitted as an explicit `staged: false, unavailable` fact, never as a delivered file.
The runner posts to `/presence/turn` (facts `direct_message`, plus `reply_to_me` for a reply to the bot;
`continuation_version` and `delivery_reporting_version` only when `/identity` advertises them) and never waits
for a turn before reading on: the next message of a chat is sent as soon as the host has observably captured
the earlier one (its inbound room row with that exact event identity) or answered it, so the host can hand it
to the current author as context. No timer stands in for that fact; a slow download simply keeps the chat in
order. A request that ends without an answer is posted again with the identical frozen event and joins the same
turn; `retained`, `continuing` and `deferred` answers are observed through `/presence/work`. Nothing is
resubmitted as new work.

**Replies.** Each selected output is queued once under its canonical `output_ref`; an output the host gives
without one is recorded as a gap (status `output_gaps`) and never sent. Its Telegram chunks are frozen when
queued and the first one replies natively to the source message. The place is read again before every part:
Silent, Paused, disconnected or unreadable holds the queued words without spending retries. A part is marked in
flight before it is sent; its progress, receipt and report then commit in one transaction. A part found in
flight after a stop or a failed commit, one whose send may have reached Telegram, or one Telegram confirmed
without a message id is `uncertain`: reported, never sent again. With delivery reporting, every part's outcome
is posted to `/presence/delivery` from its own queue.

**Owner work from a place.** A focus can call `hand_to_main` to pass the owner's own message, verbatim, to
ordinary Main owner routing (the classic `/chat/inject` payload, same client message id), which steers existing
Projects and tasks with the owner's authority. The model supplies no words. The calling task id must name the
host's own record of a focus whose retained source is a message this bridge committed; the handed-off message's
own sender must be the bridge's pinned owner and an account the place's records confirm at the moment of the
call (a revocation applies at once). A later message counts only when the host delivered it to that focus as
context. Delegated work, promoted roots, forwarded messages, replies to unmapped old bot messages and other
people are refused with a typed failure. Repeating the call rejoins the same Main message. The host's tool rules
apply: Observe refuses it and a Silent or Paused Telegram place holds it.

**Acting in the place chat.** `telegram_message` lets a root started from a Telegram place send (queued in the
same outbox and rechecked per part), edit a message this bot sent in that chat, react to a message retained
there, or read an earlier action's receipt. A request id is one action's identity across every kind: the same
full request rejoins; another kind, other words or another target under it are refused. An edit counts only
when Telegram returns that exact message; an edit or reaction whose call never returned (the bridge stopped) is
`uncertain` after the restart, reported when reporting was negotiated, and never repeated.

**Project news.** The bridge keeps, by host row address, each message, photo, video or document a Project's
root task writes in its Project chat (never a helper's row, card, media, progress line or routing note). Media
are kept as references (row address, mime, file name, artifact reference), never re-sent. Nothing is sent from
that: a root mind reads the waiting items (`telegram_news_read`) and either tells the owner what matters in its
own words, naming the items covered (`telegram_news_publish` `publish`), or sets items aside with a reason
(`skip`). A publish request and its items commit together; the same request id with the same words rejoins,
other words refuse. Only the newest 500 items wait: a read reports how many older ones `expired` unreviewed, so
an empty list does not mean everything was reviewed, and each item names its Project room, whose own history keeps
every row. A root bound to an external place may read or publish only while its own source sender is
an owner account that place currently confirms; ordinary owner turns and the mind's own wakes are owner roots.
The news goes to the owner's chat through the outbox; a connected place there governs it.

**Mirror.** Rows of this bridge's own place rooms are never mirrored again (the outbox already sent them). A
delegated child's photos, videos and documents stay with its root, as its rows already did; the host now names
each media and links event's producing task. The classic modes are unchanged: `all` still mirrors raw Project rows
and `telegram_only` still sends no web-originated Main reply. The connected mode `main` sends Ouroboros's Main
replies, files, links and questions (web- or Telegram-originated, including Main's answer to a handed-off message
in the owner's chat), a root's deliberate Main notice and Projects' questions, and nothing else. The destination
decides before the origin: a Project's own room never mirrors, even when its work began from Telegram and its room
kept that transport; the Project start, handoff and completion rows the host pins into Main stay out; helper rows,
cards, media and links stay out by their lineage or their producer's task record; relay notes quoting the owner's
words and the owner's own rows never go. An unreadable Project registry sends nothing it cannot place. In `main`,
finish notices (when enabled) name Main roots only.

**Activation** (owner): update the skill and grant its new `presence` and `tool` permissions; turn Consciousness
on (a place focus runs only while it is on: with it off, place messages are heard but not answered); connect the
Telegram place; confirm the owner's Telegram account in its Places sheet; review its wakes and notes; then set
Ordinary messages to `place` and Mirror mode to `main` (keep `Notify on task completion` off for chosen news
only). **Rollback**: set Ordinary messages back to `main`. New ordinary owner messages then take the classic Main
relay. That setting alone neither disconnects the place nor holds its outbox: a message already in this bridge's
custody keeps the place road (also when Telegram redelivers it after a lost offset, so it never reaches Main a
second time), and its owed replies still go while the place is Active. To hold those too, set the place Paused or
Silent, or disconnect it (they then wait until it is Active and connected again), or disable the skill. Room
history, custody, the offset and the reference map are kept. An unreadable or unwritable custody store stops
polling before acknowledging the affected update; repair storage and restart the skill. Telegram controls are
unavailable during that outage; use the app's controls. If even the safe-prefix offset cannot be saved, earlier
batch effects can replay after restart: this is not an exactly-once guarantee.

Not in this version: typing indicators for place turns, groups as places, and a transport inbox snapshot for a
continuing author (a later arrival still reaches a live author as host mail). Photos or videos from hosts older
than this release name no producing task and keep the previous behavior.

The Mini App exposes the unchanged Ouroboros SPA through the established
owner-authenticated sidecar and a pinned Cloudflare Quick Tunnel. It is enabled
by default after owner binding and can be turned off independently without
stopping the text bridge. Disabling the skill destroys process-memory Mini App
sessions, stops public exposure, and best-effort restores the prior Telegram
menu button. Rotate the bot token only while the skill is disabled, then
re-enable it.

Delegated task cards lead with the executor's latest words (marked earlier
when retained), followed by problems and compact activity counts. These are
attributed observations, not the supervising task's narration or a completion
receipt; journal gaps and preview omissions remain visible.

Set `TELEGRAM_BOT_TOKEN` in Settings, grant it to this skill, enable the skill,
and send the bot a private message to bind the owner. No legacy Telegram skill
state is copied or changed. Installations that use `telegram-bridge` or
`telegram-miniapp-poc` must disable or remove those skills before enabling this
one.

If Telegram requires a proxy, open this skill's Telegram settings and enter
`TELEGRAM_PROXY` as `scheme://[user:password@]host[:port]` (`socks5`, `socks5h`,
`http` or `https`; SOCKS needs an explicit port). Leave the masked field empty
to keep a saved value, or use **Clear saved Telegram proxy** to remove it.
Disable and re-enable the skill after changing the proxy so its poller and
companion also pick up the change. No additional Secrets grant is needed.

The Bot API stays `https://api.telegram.org` with TLS through the proxy and no
redirects. Proxy credentials follow the selected protocol: HTTP and SOCKS do
not encrypt the connection to the proxy; HTTPS does. Invalid proxy settings
produce an error naming the key without its value. With no skill proxy, the
bridge retains its existing direct/ambient-proxy behavior and the companion's
Telegram calls stay direct. This setting does not change the application proxy,
Cloudflare tunnel, Mini App web traffic, or local host requests.

The Mini App supports macOS arm64/x86_64, Linux arm64/x86_64, and Windows
x86_64. Only the explicit unsupported OS/architecture case degrades
independently: the text bridge remains available while Mini App status reports
that no pinned cloudflared asset exists. Invalid host runtime, unsafe state, or
companion registration errors fail the skill load instead of claiming a partial
healthy installation.

Registration, pinned cloudflared selection, and runtime status share one
architecture helper. Only when `platform.machine()` is empty on Windows does
it fall back to `sysconfig.get_platform()`: `win-amd64` selects the pinned
Windows asset; ARM64, 32-bit, and unknown builds remain unsupported in this
fallback. A nonempty machine value takes precedence over the interpreter build;
macOS and Linux use only the machine value.

The Mini App is Beta. Its best-effort Cloudflare Quick Tunnel has no SLA and
does not support Server-Sent Events (SSE). It targets native Telegram clients;
Telegram WebA/WebK are not supported.
