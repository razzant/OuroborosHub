---
name: telegram-presence-bot
description: Optional second Telegram Bot API transport for exact forum-topic Presence, separate from the owner command bot.
version: 0.1.0
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
permissions: [net, route, widget, read_settings, presence, companion_process, transcribe_audio]
env_from_settings: [TELEGRAM_PRESENCE_BOT_TOKEN]
companion_processes:
  - name: presence_bot
    command: [python3, scripts/companion.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 5
when_to_use: The owner explicitly configures a second, distinct Telegram bot and exact Presence bindings for selected chats/topics.
model_experience:
  what_model_sees: Bound peer/topic messages arrive as third-party Presence turns, not owner commands. Unbound updates remain private; no participant tool is granted automatically.
  token_effect: One Presence turn per bound incoming update, subject to the owner's behavior profile and budget.
---

# Second Telegram Presence bot

Source-only optional candidate; install, review, grant and enable are separate owner actions. This is NOT the bundled owner-only Telegram bridge and does not accept its token or route messages to `/chat/inject`. The host stores both tokens separately and rejects equal values under the Settings write lock; the companion checks live host separation before each Telegram API call. Only this bot receives `TELEGRAM_PRESENCE_BOT_TOKEN`; it never receives the owner bot token. A missing/unreadable equality authority is a refusal, not a warning. Nothing in this source package is enabled or logged in by its presence on disk.

The companion polls Bot API updates and atomically stores each selected update and its current exact Presence binding (or `unbound`) before advancing its durable offset; unrelated text advances the offset without occupying the media inbox. Later changes to that mapping cannot retroactively authorize an already captured unbound update or rebind one captured under another topic. A remove-and-restore of the exact same binding ID before the companion observes the removal is treated as reauthorizing its still-pending captured events; rotate the binding ID for a hard epoch break. Already held events stay held. The companion rechecks configuration after long polling and before each Host admission and outbound send; a mapping that changed during a long poll causes that batch's affected media to remain private rather than guessing its previous authority. Media bytes from every chat the Bot API actually delivers enter bounded private skill state even without a Presence binding. Presence and public replies require an exact owner-created `(chat_id, message_thread_id)` binding and the reviewed behavior profile. General is a separate empty thread, never a fallback for an unknown topic. The account id is read from `getMe`, not a setup form. Telegram privacy settings and bot membership determine which updates Telegram actually sends; no offline test proves capture of unseen messages.

Photos and voice/audio are downloaded locally with per-file and aggregate limits. Video and ordinary document downloads default OFF and have separate Settings switches; a refusal retains metadata and its code, never a false file. An audio file gets a fixed suffix matching its reported MIME: MP3 uses `.mp3`, Ogg `.ogg`, WebM `.webm`, and unknown audio `.bin`; an older ambiguous receipt keeps its bytes but is held for explicit repair; suffix-less Ogg records must also pass an Ogg container signature check after digest verification. If a pre-fix Ogg/WebM receipt already froze `audio_unsupported` before an uncertain Host turn, its source is held with the prior result and a `legacy_parameterized_audio_unresolved` reason; the owner reconciles that source before any deliberate replay, never an automatic second Presence turn. Supported Ogg/WebM/MP3 asks the reviewed `whisper-voice` target through the Host Service `/media/transcribe` boundary; unsupported formats remain accurately typed attachments with an explicit no-transcript status. Telegram traffic and transcripts are third-party speech, not owner instructions. The owner must consent to a separate live pilot in a test forum before any claim of real forum delivery.

Bot API has no MTProto `random_id`: a send is durably marked attempted before the network call; lost/ambiguous replies remain UNKNOWN and are not auto-resubmitted. Replies above Telegram's 4096 UTF-8 byte limit stay in the private held inbox with a typed `reply_too_long` reason and are not partially sent or retried automatically. A successful `sendMessage` receipt proves Telegram acceptance, not that a person read it. Private inbox payloads and bytes are retained until deliberate owner cleanup (not secure deletion). The 1,000-event and 128 MiB limits fail visibly rather than silently dropping unseen history. Setting a public network bind without `OUROBOROS_NETWORK_PASSWORD` exposes the owner-facing setup route; configure the password or keep the gateway private.
