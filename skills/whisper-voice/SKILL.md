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
    max_seconds: 120
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

`POST /api/extensions/whisper-voice/transcribe` accepts a WebM, Ogg, MP4 (Safari) or MP3 audio container (at most 524288 bytes, `DICTATION_MAX_BYTES`); it decodes through `av`, rejects decoded audio over 125 seconds (120 plus a 5 s tolerance, `DICTATION_MAX_SECONDS` and `DURATION_GRACE_SECONDS`), and returns `{ "text": "...", "timings": {...} }`. Timings separate imports, decode, lock wait, selection/load, lazy inference consumption and worker total in milliseconds; `model_loaded` reports this request's construction, not historical residency. They contain no audio or transcript and are returned, not logged. Invalid, missing, overlong, unreadable and unavailable models fail with an HTTP error and a specific `{error:{code,message}}` object; there is no cloud inference. Language is detected locally rather than forced to Russian, which remains supported. `GET status` gives `ready`, `resident`, model path, idle policy and any reason or download error. All plugin routes call a private token-bound loopback companion over 127.0.0.1; no externally accessible listener. Host supervision kills the companion on skill disable/Panic; the extension child itself never loads weights.

When present, the default selection is Hélène's existing **read-only** receipt at `C:\Program Files\Helene\data\models\whisper\installed.json`, which points to its turbo CTranslate2 snapshot. Otherwise the Settings form starts with this skill's own `models/` directory, even before any download, and reports unavailable until a model is selected. Nothing writes to Hélène or imports her Python libraries. A valid snapshot contains nonempty `model.bin`, `config.json`, and `tokenizer.json`. Settings also select 60-minute idle eviction (default) versus keeping an already loaded model resident. The companion process stays supervised after eviction so a later recording can load it again; no heavyweight model is loaded at mere skill startup. Saving the same resolved snapshot or changing only residency preserves loaded weights; changing snapshots invalidates them. A payload upgrade/worker restart still requires a first load.

The explicit "Download selected model" Settings action supports `small` (~480 MB) and `large-v3-turbo` (~1.6 GB). It starts a background Hugging Face snapshot download only after the owner's click and stores new weights under `state/skills/whisper-voice/models`. On success it atomically selects that exact snapshot unless the owner changed the selection while the download was running; a later selection wins and the status explains that the newly downloaded snapshot was left unselected. On failure the previous selection survives and the status reports the error. This action needs network access; the microphone/transcription path does not. The skill's isolated wheel-only dependencies install **only** in its own environment, never into host Python. PyAV is bounded rather than pinned to one wheel: 18.1.0 has no Python 3.10 wheel (the current packaged host uses 3.10), while 17.1.0 does; a Python 3.14 install may resolve 18.1.0. No general global model path or automatic download is assumed. Audio is transcribed on the computer running Ouroboros; from a phone the audio travels to that computer, and neither transcript nor audio bytes enter plugin logs. Cancelling the browser request discards its transcript; already-running native inference may continue until it completes, under the companion's ordinary process custody. This is not a claim of immediate CPU cancellation. Only one transcription is admitted at a time; excess requests return HTTP 503 `voice_busy` before decoding instead of queuing stale inference. A timeout is not proof that native work stopped.

The `contributes` provider requires a core release with plugin contributions. Older releases do not expose the dictation choice. Install and enable the skill, then choose Whisper under “+ → Add dictation”. For an attached recording, call the registered `transcribe_attachment` tool with its host manifest `ordinal`; do not discover ports or author HTTP scripts. It accepts only staged, hash-matching audio attachments in the task's artifact store and reuses the composer worker. The narrow reader uses Ouroboros resource-binding/media admission helpers; missing helpers or refused reads fail rather than falling back to arbitrary path access. This is not automatic Telegram ingress transcription: one model tool call is still needed. Existing host extension-tool network policy applies even though recognition is local.

Limits, one constant each in `whisper_runtime.py` (the manifest row above repeats the dictation pair, because the host reads it from there).

- **Composer dictation: 524288 bytes (512 KiB) and 120 seconds.** Core rejects (it does not clamp) a `provides: stt` row above 524288 bytes or 600 seconds: the row is dropped (`ouroboros/extension_ui_validation.py:820-824`), and its route body cap is 512 KiB (`gateway/extensions.py:48`). The row says 120, not 600, on purpose. The composer records at 32 kbit/s in 250 ms chunks and rejects a finished recording that is over the byte cap (`web/modules/voice_recorder.js:63,78,95`), so a time cap that the byte cap reaches first throws a long dictation away, while a time cap that comes first keeps it. 120 s is about 480000 bytes nominal; headless Chromium running core's own recorder produced 498583 bytes in 120 s (4155 B/s), 5 % under the cap, and the 512 KiB cap would trip at about 126 s. A longer dictation stops itself at 120 s and its first two minutes are transcribed. The worker accepts up to 5 s more than the nominal length (`DURATION_GRACE_SECONDS`), because the recorder's stop lands a little after its timer (300 ms measured) and codecs pad their output (12 and 58 samples measured for 120.000 and 600.000 s at 44.1 kHz AAC); without that tolerance a stop 300 ms late would discard the whole dictation. The 512 KiB byte cap still binds first for a recorder at 32 kbit/s, at about 126 s. The composer also gives up on the request after 120 s (`web/modules/dictation.js:113`), so a dictation should be short; use an attached recording for long speech.
- **`transcribe_attachment`: 20 MiB (`ATTACHMENT_MAX_BYTES`) and 600 seconds (`ATTACHMENT_MAX_SECONDS`).** This path posts the staged file to the loopback companion and never crosses the 512 KiB route ingress; core's staging adds no size cap. The 20 MiB is Whisper's own ceiling and equals the Bot API download limit of the Hub's telegram-bot. The Hub's telegram-user-session can stage up to 32 MiB and core's built-in Telegram skill 10 MiB; a staged file over 20 MiB is refused by the host's file reader (`FILE_TOO_LARGE ... max 20971520`, as `attachment_blocked`) before Whisper sees it and stays a file. The 600 s is the skill's own bound for one CPU recognition: its transcript is accepted up to 20000 characters. Measured at about 600 s (no weights): decoding adds 83 MB of RSS with the frame copies released before recognition, faster-whisper's VAD 179 MB more and its mel features 698 MB, weights on top; at 1800 s the same steps measured 237, 451 and 2145 MB, hence 600. A recording over the limit is refused with a specific error (`audio must contain 1..N bytes` or `audio longer than N seconds`, N including the 5 s tolerance: 125 for dictation, 605 for attachments), never cut. At Telegram's typical Ogg/Opus 16 to 32 kbit/s a note of 600 s is 1.2 to 2.4 MB, so the length cap, not the 20 MiB, binds for voice notes; for dictation the 120 s cap is what binds.
- **Waits.** The tool deadline is 1260 seconds and the wait on the companion 1200 (twice a 600 s note, cold model load included, on CPU). The host's deadline for an extension tool is the registered value plus 3 s (`ouroboros/tools/registry_core.py:1054`, `loop_tool_execution.py:292-293`) and the child is killed at the registered value (`extension_process_runner.py:933`), so this skill's own deadline is the one that governs. A dictation waits 125 s. Status, settings and download-start calls wait at most 30 s, and they never queue behind a recognition: while one runs, status answers at once with `busy: true` (the Settings line says so), and saving settings or starting a download answers HTTP 503 `voice_busy` with nothing changed: those calls never wait for the model lock, so a refused save can never apply later. Real CPU recognition time with weights is not measured here, so the 1200 s wait is a budget, not a promise; a note that outlasts it gets 503 `worker_unavailable` and its transcript is not retrievable.
- **One recognition at a time.** While a long attached note is transcribed, a composer dictation answers HTTP 503 `voice_busy` (the single slot is reserved before the body is read, so a second note, or an over-limit one, is answered at once without its body being read; a sender that stalls for 30 s loses the slot). That is deliberate: a stale recording is never queued.
