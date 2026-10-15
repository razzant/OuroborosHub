"""Exact-origin Host Presence client for the second Telegram bot."""
from __future__ import annotations

import json
from urllib import error, parse, request

from .transport import TransportUnavailable, _NoRedirect


class HostClient:
    def __init__(self, origin: str, token: str, *, opener=None):
        parsed = parse.urlsplit(origin)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.port is None or parsed.username or parsed.password or parsed.path
                or parsed.query or parsed.fragment or not token):
            raise TransportUnavailable("Host identity unavailable")
        self._origin = origin
        self._token = token
        self._opener = opener or request.build_opener(request.ProxyHandler({}), _NoRedirect)

    def _json(self, method: str, path: str, data=None):
        req = request.Request(self._origin + path,
                              data=(json.dumps(data, ensure_ascii=False).encode() if data is not None else None),
                              method=method, headers={"x-skill-token": self._token,
                                                      "Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=50) as response:
                payload = response.read(65537)
                if len(payload) > 65536:
                    raise TransportUnavailable("Host reply too large")
                return response.status, json.loads(payload)
        except error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read(65537))
            except (ValueError, TypeError, OSError):
                raise TransportUnavailable("Host refusal unavailable") from None
        except (OSError, ValueError, TypeError):
            raise TransportUnavailable("Host response unavailable") from None

    def identity(self) -> None:
        status, payload = self._json("GET", "/identity")
        if status != 200 or payload.get("ok") is not True:
            raise TransportUnavailable("Host identity unavailable")

    def turn(self, binding_id: str, event: dict, files: list[str]) -> dict:
        status, payload = self._json("POST", "/presence/turn", {
            "binding_id": binding_id, "event": event,
            "staged_files": files, "delivery_reporting_version": 1,
        })
        if status not in {200, 202} or not isinstance(payload, dict):
            raise TransportUnavailable("Presence turn unavailable")
        return payload

    def work(self, binding_id: str, work_ref: str) -> dict:
        if not work_ref or "/" in work_ref or len(work_ref) > 200:
            raise ValueError("invalid work reference")
        path = "/presence/work/" + parse.quote(work_ref, safe="") + "?binding_id=" + parse.quote(binding_id, safe="")
        status, payload = self._json("GET", path)
        if status not in {200, 202} or not isinstance(payload, dict):
            raise TransportUnavailable("Presence work unavailable")
        return payload

    def transcribe(self, binding_id: str, account: int, chat: int, topic: int | None,
                   audio: bytes, mime: str) -> tuple[str, str]:
        # A frozen pre-normalization receipt may retain codec parameters. Only
        # the validated essence crosses the Host boundary; never rewrite the
        # immutable media receipt or pretend an MP3 is Ogg.
        essence = (mime.split(";", 1)[0].strip().lower()
                   if isinstance(mime, str) and len(mime) <= 80 and mime.isascii() else "")
        if not audio or len(audio) > 393216 or essence not in {"audio/ogg", "audio/webm", "audio/mpeg"}:
            return "", "audio_unsupported"
        req = request.Request(self._origin + "/media/transcribe/whisper-voice", data=audio,
                              method="POST", headers={"x-skill-token": self._token,
                                  "x-presence-binding": binding_id,
                                  "x-media-provider": "telegram", "x-media-account": str(account),
                                  "x-media-conversation": str(chat),
                                  "x-media-thread": str(topic) if topic is not None else "",
                                  "Content-Type": essence})
        try:
            with self._opener.open(req, timeout=120) as response:
                raw = response.read(16385)
                if response.status != 200 or len(raw) > 16384:
                    return "", "transcription_unavailable"
            payload = json.loads(raw)
            text = payload.get("text") if isinstance(payload, dict) else None
            if text == "" and payload.get("status") == "no_speech":
                return "", "no_speech"
            if not isinstance(text, str) or not 0 < len(text.strip()) <= 8000:
                return "", "transcription_unavailable"
            return text.strip(), "ready"
        except (OSError, error.HTTPError, ValueError, TypeError):
            return "", "transcription_unavailable"

    def delivered(self, *, binding_id: str, source_id: str, account: int, chat: int,
                  topic: int | None, text: str, message_id: int) -> bool:
        # Only after Telegram returned a concrete message_id. A failed Host
        # report is NOT permission to resend that already-accepted message.
        receipt = {"schema_version": 1, "delivery_id": "auto_" + source_id,
                   "part_id": "0", "state": "delivered", "provider": "telegram",
                   "account_id": str(account), "conversation_id": str(chat),
                   "thread_id": str(topic) if topic is not None else "",
                   "text": text, "format": "text", "message": {"message_id": str(message_id)},
                   "origin": {"kind": "automatic", "task_id": "", "source_event_id": source_id}}
        try:
            status, body = self._json("POST", "/presence/delivery", receipt)
            return status == 200 and body.get("ok") is True
        except TransportUnavailable:
            return False
