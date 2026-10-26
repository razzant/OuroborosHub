"""Reviewed optional voice route; no inference, model weights or audio persisted here."""
from __future__ import annotations

import http.client
import json
from pathlib import Path

from starlette.responses import JSONResponse

from .whisper_runtime import MAX_AUDIO_BYTES, audio_format, error, read_settings


def _worker(state_dir: Path, method: str, path: str, body: bytes = b"", mime: str = "") -> JSONResponse:
    try:
        endpoint = json.loads((state_dir / "voice_endpoint.json").read_text(encoding="utf-8"))
        port = endpoint.get("port")
        token = endpoint.get("token")
        if type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str) or len(token) < 30:
            raise ValueError("worker endpoint malformed")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        try:
            conn.request(method, path, body=body, headers={
                "X-Voice-Token": token,
                "Content-Type": mime if method == "POST" else "application/json",
                "Content-Length": str(len(body)),
            })
            response = conn.getresponse()
            result = response.read(65536)
            if response.read(1):
                raise ValueError("oversized worker reply")
            value = json.loads(result)
            if not isinstance(value, dict):
                raise ValueError("worker reply is not an object")
            return JSONResponse(value, status_code=response.status)
        finally:
            conn.close()
    except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
        return JSONResponse(error("worker_unavailable", f"Voice worker unavailable: {type(exc).__name__}"), status_code=503)


def _settings_response(response: JSONResponse) -> JSONResponse:
    """Keep a typed API, but give the existing Settings action its state line."""
    data = json.loads(response.body)
    if response.status_code >= 400:
        issue = data.get("error", {})
        if isinstance(issue, dict):
            data["error_code"] = issue.get("code", "voice_unavailable")
            data["error"] = issue.get("message", "Voice unavailable")
    else:
        state = "downloading" if data.get("installing") else "ready" if data.get("ready") else "unavailable"
        data["voice"] = {
            "state": state,
            "message": data.get("install_error") or data.get("reason") or data.get("install_notice") or
                       ("Model is ready for local speech" if state == "ready" else "Voice unavailable"),
        }
    return JSONResponse(data, status_code=response.status_code)


def _routes(state_dir: Path):
    async def status(_request):
        # Status is read-only even when Settings' action renderer submits POST.
        return _settings_response(_worker(state_dir, "GET", "/status"))

    async def transcribe(request):
        length = request.headers.get("content-length", "")
        if length and (not length.isdecimal() or int(length) > MAX_AUDIO_BYTES):
            return JSONResponse(error("too_large", "Voice recording exceeds 393216 bytes"), status_code=413)
        # Host ingress has its own 512 KiB cap; a skill-specific cap ensures
        # in-process and isolated extension paths share the same bound.
        data = await request.body()
        mime = request.headers.get("content-type", "")
        try:
            audio_format(mime, data)
        except ValueError as exc:
            return JSONResponse(error("invalid_audio", str(exc)), status_code=400)
        return _worker(state_dir, "POST", "/transcribe", data, mime)

    async def settings(request):
        if request.method == "GET":
            try:
                stored = read_settings(state_dir)
                return JSONResponse({**stored, "always_resident_choice": "yes" if stored["always_resident"] else "no"})
            except Exception as exc:
                return JSONResponse(error("settings_unreadable", str(exc)), status_code=503)
        value = await request.json()
        if not isinstance(value, dict):
            return JSONResponse(error("invalid_settings", "Settings must be an object"), status_code=400)
        # The host form uses strings; the companion receives a typed boolean.
        choice = value.get("always_resident_choice")
        if choice not in ("yes", "no"):
            return JSONResponse(error("invalid_settings", "Choose a residency setting"), status_code=400)
        body = json.dumps({"model_path": value.get("model_path"), "always_resident": choice == "yes"}).encode("utf-8")
        return _settings_response(_worker(state_dir, "POST", "/settings", body, "application/json"))

    async def install(request):
        value = await request.json()
        if not isinstance(value, dict) or value.get("model") not in ("small", "large-v3-turbo"):
            return JSONResponse(error("invalid_model", "Choose small or large-v3-turbo"), status_code=400)
        return _settings_response(_worker(state_dir, "POST", "/models/install", json.dumps(value).encode("utf-8"), "application/json"))

    return status, transcribe, settings, install


def register(api):
    state_dir = Path(api.get_state_dir())
    api.register_companion_process("whisper_worker")
    from .attachment_tool import transcribe_attachment

    def attachment(ctx, ordinal: int):
        return transcribe_attachment(ctx, ordinal, state_dir=state_dir, worker=_worker)

    api.register_tool(
        "transcribe_attachment", handler=attachment,
        description="Transcribe a host-staged audio attachment locally by its manifest ordinal (not a file path). Reuses Whisper's resident companion; no cloud audio transfer. Returns text and non-content worker timings. Does not Send a message or change settings. Max 384 KiB / 60 seconds. Use directly for attached voice recordings, without HTTP scripts or port discovery.",
        schema={"type": "object", "properties": {"ordinal": {"type": "integer", "minimum": 0}},
                "required": ["ordinal"], "additionalProperties": False},
        timeout_sec=150,
    )
    status, transcribe, settings, install = _routes(state_dir)
    # Settings actions submit POST even when their descriptor says GET;
    # ordinary passive status readers can still use GET.
    api.register_route("status", status, methods=("GET", "POST"))
    api.register_route("transcribe", transcribe, methods=("POST",))
    api.register_route("settings/save", settings, methods=("GET", "POST"))
    api.register_route("models/install", install, methods=("POST",))
    api.register_settings_section("voice", title="Local voice input", schema={"components": [
        {"type": "markdown", "text": "Optional local speech to editable draft. No audio is sent to an external speech service. On this host the installed Hélène model may be used read-only; new models are downloaded only after an explicit click into this skill's state directory. Recording is limited to 60 seconds and 384 KiB."},
        {"type": "form", "route": "settings/save", "method": "POST", "fields": [
            {"name": "model_path", "label": "Local model snapshot or installed.json receipt", "type": "text", "help": "Absolute read-only path; the selected snapshot must contain model.bin, config.json and tokenizer.json."},
            {"name": "always_resident_choice", "label": "After recording", "type": "select", "options": [
                {"value": "no", "label": "Unload model after 60 minutes idle (default)"},
                {"value": "yes", "label": "Keep model resident until skill is disabled"},
            ]},
        ], "submit_label": "Save voice settings"},
        {"type": "action", "id": "download_small", "route": "models/install", "method": "POST", "fields": [
            {"name": "model", "label": "Download model into skill state", "type": "select", "options": [
                {"value": "small", "label": "Small (~480 MB)"},
                {"value": "large-v3-turbo", "label": "Turbo (~1.6 GB)"},
            ]},
        ], "submit_label": "Download selected model", "busy_label": "Starting download…"},
        {"type": "action", "id": "voice_status", "route": "status", "method": "POST", "fields": [], "submit_label": "Check voice readiness"},
    ]})
