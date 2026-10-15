---
name: whisper-voice
version: 0.1.0
description: Optional local Whisper voice input for an editable Ouroboros composer draft.
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [route, widget, companion_process, net]
voice_input:
  version: 1
  route: transcribe
  mime_types: [audio/webm, audio/ogg, audio/mpeg]
  max_bytes: 393216
media_transcription:
  version: 1
  route: transcribe
  mime_types: [audio/webm, audio/ogg, audio/mpeg]
  max_bytes: 393216
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
when_to_use: The owner explicitly enables optional local voice input in the composer.
model_experience:
  what_model_sees: No model tool or prompt injection. Transcription only populates the owner's unsent composer draft.
  token_effect: Zero while idle; the model is held after first use until 60 minutes idle, or continuously if selected.
---

# Local voice for Ouroboros

This directory is a standalone optional skill **candidate**, not installed or enabled. Install it under the external skills bucket and pass the normal review → isolated dependencies → grants → enable lifecycle before use. The core capability declaration is only a hint: the composer appears when the live extension has a matching registered POST route and executable current review; route dispatch separately rechecks readiness. If absent, the composer has no microphone or hotkey. The owner must still press Send after reviewing the transcript.

`POST /api/extensions/whisper-voice/transcribe` accepts a WebM, Ogg or MP3 audio container (at most 393216 bytes); it decodes through `av`, rejects decoded audio over 60 seconds, and returns `{ "text": "..." }`. Invalid, missing, overlong, unreadable and unavailable models fail with an HTTP error and a specific `{error:{code,message}}` object; there is no cloud inference. Language is detected locally rather than forced to Russian, which remains supported. `GET status` gives `ready`, `resident`, model path, idle policy and any reason or download error. All plugin routes call a private token-bound loopback companion over 127.0.0.1; no externally accessible listener. Host supervision kills the companion on skill disable/Panic; the extension child itself never loads weights.

When present, the default selection is Hélène's existing **read-only** receipt at `C:\Program Files\Helene\data\models\whisper\installed.json`, which points to its turbo CTranslate2 snapshot. Otherwise the Settings form starts with this skill's own `models/` directory, even before any download, and reports unavailable until a model is selected. Nothing writes to Hélène or imports her Python libraries. A valid snapshot contains nonempty `model.bin`, `config.json`, and `tokenizer.json`. Settings also select 60-minute idle eviction (default) versus keeping an already loaded model resident. The companion process stays supervised after eviction so a later recording can load it again; no heavyweight model is loaded at mere skill startup.

The explicit "Download selected model" Settings action supports `small` (~480 MB) and `large-v3-turbo` (~1.6 GB). It starts a background Hugging Face snapshot download only after the owner's click and stores new weights under `state/skills/whisper-voice/models`. On success it atomically selects that exact snapshot unless the owner changed the selection while the download was running; a later selection wins and the status explains that the newly downloaded snapshot was left unselected. On failure the previous selection survives and the status reports the error. This action needs network access; the microphone/transcription path does not. The skill's isolated wheel-only dependencies install **only** in its own environment, never into host Python. PyAV is bounded rather than pinned to one wheel: 18.1.0 has no Python 3.10 wheel (the current packaged host uses 3.10), while 17.1.0 does; a Python 3.14 install may resolve 18.1.0. No general global model path or automatic download is assumed. Audio stays local, and neither transcript nor audio bytes enter plugin logs. Cancelling the browser request discards its transcript; already-running native inference may continue until it completes, under the companion's ordinary process custody. This is not a claim of immediate CPU cancellation.

This is a local candidate pending independent review, tests, and skill installation. Its `voice_input` field requires the matching core candidate in this checkout; older Ouroboros releases treat it as an inert unknown manifest extra, so installing the skill alone does not create composer controls. A source PR does not install this skill or authorize modifying either running installation.
