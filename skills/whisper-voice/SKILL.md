---
name: whisper-voice
version: 0.3.0
description: Optional local Whisper voice input for an editable Ouroboros composer draft.
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [route, widget, companion_process, net, tool]
contributes:
  - id: whisper
    provides: stt
    label: Whisper
    runtime: server
    route: transcribe
    destination: host
    languages: [auto]
    formats: [audio/webm, audio/ogg, audio/mp4, audio/mpeg]
    max_bytes: 524288
    max_seconds: 600
    install_size: "~500 MB"
install:
  - kind: pip
    package: faster-whisper==1.2.1
  - kind: pip
    package: av>=17.1.0,<19
companion_processes:
  - name: whisper_worker
    command: [python3, scripts/companion.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 5
when_to_use: The owner enables local voice input or supplies an audio attachment for local transcription.
model_experience:
  what_model_sees: A transcribe_attachment tool accepts the host manifest ordinal, reuses the same local worker and returns text plus stage timings. Composer dictation still only fills the unsent draft.
  token_effect: One small tool schema while enabled; weights are retained after first use until 60 minutes idle, or continuously if selected.
---

# Local voice for Ouroboros

This directory is a standalone optional skill. Install it under the external skills bucket and pass the normal review → isolated dependencies → grants → enable lifecycle before use. The core capability declaration is only a hint: the composer appears when the live extension has a matching registered POST route and executable current review; route dispatch separately rechecks readiness. If absent, the composer has no microphone or hotkey. The owner must still press Send after reviewing the transcript.

`POST /api/extensions/whisper-voice/transcribe` accepts a WebM, Ogg, MP4 (Safari) or MP3 audio container (at most 524288 bytes, `DICTATION_MAX_BYTES`); it decodes through `av`, rejects decoded audio over 600 seconds (`DICTATION_MAX_SECONDS`), and returns `{ "text": "...", "timings": {...} }`. Timings separate imports, decode, lock wait, selection/load, lazy inference consumption and worker total in milliseconds; `model_loaded` reports this request's construction, not historical residency. They contain no audio or transcript and are returned, not logged. Invalid, missing, overlong, unreadable and unavailable models fail with an HTTP error and a specific `{error:{code,message}}` object; there is no cloud inference. Language is detected locally rather than forced to Russian, which remains supported. `GET status` gives `ready`, `resident`, model path, idle policy and any reason or download error. All plugin routes call a private token-bound loopback companion over 127.0.0.1; no externally accessible listener. Host supervision kills the companion on skill disable/Panic; the extension child itself never loads weights.

When present, the default selection is Hélène's existing **read-only** receipt at `C:\Program Files\Helene\data\models\whisper\installed.json`, which points to its turbo CTranslate2 snapshot. Otherwise the Settings form starts with this skill's own `models/` directory, even before any download, and reports unavailable until a model is selected. Nothing writes to Hélène or imports her Python libraries. A valid snapshot contains nonempty `model.bin`, `config.json`, and `tokenizer.json`. Settings also select 60-minute idle eviction (default) versus keeping an already loaded model resident. The companion process stays supervised after eviction so a later recording can load it again; no heavyweight model is loaded at mere skill startup. Saving the same resolved snapshot or changing only residency preserves loaded weights; changing snapshots invalidates them. A payload upgrade/worker restart still requires a first load.

The explicit "Download selected model" Settings action supports `small` (~480 MB) and `large-v3-turbo` (~1.6 GB). It starts a background Hugging Face snapshot download only after the owner's click and stores new weights under `state/skills/whisper-voice/models`. On success it atomically selects that exact snapshot unless the owner changed the selection while the download was running; a later selection wins and the status explains that the newly downloaded snapshot was left unselected. On failure the previous selection survives and the status reports the error. This action needs network access; the microphone/transcription path does not. The skill's isolated wheel-only dependencies install **only** in its own environment, never into host Python. PyAV is bounded rather than pinned to one wheel: 18.1.0 has no Python 3.10 wheel (the current packaged host uses 3.10), while 17.1.0 does; a Python 3.14 install may resolve 18.1.0. No general global model path or automatic download is assumed. Audio is transcribed on the computer running Ouroboros; from a phone the audio travels to that computer, and neither transcript nor audio bytes enter plugin logs. Cancelling the browser request discards its transcript; already-running native inference may continue until it completes, under the companion's ordinary process custody. This is not a claim of immediate CPU cancellation. Only one transcription is admitted at a time; excess requests return HTTP 503 `voice_busy` before decoding instead of queuing stale inference. A timeout is not proof that native work stopped.

The `contributes` provider requires a core release with plugin contributions. Older releases do not expose the dictation choice. Install and enable the skill, then choose Whisper under “+ → Add dictation”. For an attached recording, call the registered `transcribe_attachment` tool with its host manifest `ordinal`; do not discover ports or author HTTP scripts. It accepts only staged, hash-matching audio attachments in the task's artifact store and reuses the composer worker. The narrow reader uses Ouroboros resource-binding/media admission helpers; missing helpers or refused reads fail rather than falling back to arbitrary path access. This is not automatic Telegram ingress transcription: one model tool call is still needed. Existing host extension-tool network policy applies even though recognition is local.

Limits, one constant each in `whisper_runtime.py` (the manifest row above repeats the dictation pair, because the host reads it from there). Composer dictation: 524288 bytes (512 KiB) and 600 seconds, the ceilings Ouroboros itself allows a `provides: stt` row (its manifest validation clamps `max_bytes` to 524288 and `max_seconds` to 600, the gateway's route body cap is 512 KiB, and the composer recorder clamps to both). `transcribe_attachment`: 20 MiB (`ATTACHMENT_MAX_BYTES`, the Bot API download limit of the Hub's telegram-bot, the largest file a Telegram connector here stages; the built-in Telegram skill of the core stops at 10 MiB; core's staging adds no cap of its own, and this path posts the staged file to the loopback companion, so it never crosses the 512 KiB route ingress) and 1800 seconds (`ATTACHMENT_MAX_SECONDS`, this skill's working bound for decoded memory and the tool deadline). A typical Telegram voice note is Ogg/Opus at about 16 to 32 kbit/s, so 512 KiB holds roughly 2 to 4.5 minutes: for dictation-sized uploads the byte cap, not the 600 seconds, is what binds; on the attachment path 20 MiB is far above 30 minutes of voice, so the length cap binds. A recording over its path's limit is refused with the same specific error (`audio must contain 1..N bytes` or `audio longer than N seconds`), never cut. Recognition runs on CPU: the worker wait is 1500 seconds and the tool deadline 1560, and a transcript is accepted up to 60000 characters.
