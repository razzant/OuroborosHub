"""Local Host-mediated Whisper request for an exact Presence-bound audio event.

Only the caller's Host token is sent to the Host Service. Its /media/transcribe
endpoint checks the caller's binding, current grants and reviewed target; it
constructs a fresh synthetic request without forwarding this token to Whisper.
"""
from __future__ import annotations

import json
from urllib import error, parse, request

MAX_AUDIO = 393216


def transcribe(host: str, token, *, binding_id: str, account_id: int,
               peer_id: int, topic_id: int | None, audio: bytes, mime: str) -> tuple[str, str]:
    url = parse.urlsplit(host)
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.port is None or url.username or url.password or url.path or url.query or url.fragment):
        return "", "host_unavailable"
    # Old immutable receipts can carry codec parameters. Validate and send
    # their MIME essence, without rewriting the captured source identity.
    essence = (mime.split(";", 1)[0].strip().lower()
               if isinstance(mime, str) and len(mime) <= 80 and mime.isascii() else "")
    if not audio or len(audio) > MAX_AUDIO or essence not in {"audio/ogg", "audio/webm", "audio/mpeg"}:
        return "", "audio_unsupported"
    if not binding_id or type(account_id) is not int or type(peer_id) is not int:
        return "", "origin_unavailable"

    class NoRedirect(request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = request.build_opener(request.ProxyHandler({}), NoRedirect)
    headers = {"x-skill-token": token.use_in_request(), "x-presence-binding": binding_id,
               "x-media-provider": "telegram", "x-media-account": str(account_id),
               "x-media-conversation": str(peer_id),
               "x-media-thread": str(topic_id) if topic_id is not None else "",
               "Content-Type": essence}
    outgoing = request.Request(host + "/media/transcribe/whisper-voice", data=audio,
                               headers=headers, method="POST")
    try:
        with opener.open(outgoing, timeout=120) as response:
            data = response.read(16385)
            if response.status != 200 or len(data) > 16384:
                return "", "transcription_unavailable"
        result = json.loads(data)
        text = result.get("text") if isinstance(result, dict) else None
        if text == "" and result.get("status") == "no_speech":
            return "", "no_speech"
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 8000:
            return "", "transcription_unavailable"
        return text.strip(), "ready"
    except (error.URLError, OSError, ValueError, TypeError, TimeoutError):
        return "", "transcription_unavailable"
