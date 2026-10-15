"""Second bot's narrow Bot API and Host Service transports.

The token is passed only to Telegram's fixed HTTPS origin in the request URL;
never print an HTTPError, URL or exception repr (it includes the token). Every
Telegram request is preceded by the Host's live, authenticated equality check.
A successful Bot API result proves Telegram acceptance, never human delivery.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable
from urllib import error, parse, request


class TransportUnavailable(RuntimeError):
    """No proven result; a mutating request must remain UNKNOWN."""


class TokenUnavailable(TransportUnavailable):
    """Host could not establish distinct current bot credentials."""


class MediaTooLarge(TransportUnavailable):
    """A measured provider file exceeds the selected download policy."""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BotTransport:
    def __init__(self, token: str, host_url: str, host_token: str, *, opener=None):
        if (not isinstance(token, str) or not token or any(c in token for c in "/?&\r\n")
                or not isinstance(host_token, str) or not host_token):
            raise TokenUnavailable("bot or Host credential unavailable")
        parsed = parse.urlsplit(host_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.port is None or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise TokenUnavailable("Host Service origin unavailable")
        self._token = token
        self._host = host_url
        self._host_token = host_token
        self._opener = opener or request.build_opener(request.ProxyHandler({}), _NoRedirect)

    def check_separation(self) -> None:
        req = request.Request(self._host + "/telegram/presence-bot-token", method="GET",
                              headers={"x-skill-token": self._host_token,
                                       "x-bot-token": self._token})
        try:
            with self._opener.open(req, timeout=15) as response:
                body = json.loads(response.read(4097))
                if response.status != 200 or body != {"ok": True, "status": "distinct"}:
                    raise TokenUnavailable("bot token separation unavailable")
        except (OSError, ValueError, TypeError, error.HTTPError):
            raise TokenUnavailable("bot token separation unavailable") from None

    def api(self, method: str, payload: dict, *, timeout: int = 35,
            pre_send: Callable[[], bool] | None = None):
        if method not in {"getMe", "getUpdates", "getFile", "sendMessage"}:
            raise ValueError("unsupported Bot API operation")
        if pre_send is not None and method != "sendMessage":
            raise ValueError("pre-send owner check applies only to messages")
        self.check_separation()
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        # Host token separation may block on the network. Re-read owner mapping
        # AFTER it and immediately before the mutating Telegram request.
        if pre_send is not None:
            try:
                permitted = pre_send()
            except (OSError, ValueError, TypeError):
                raise TransportUnavailable("owner binding unreadable before send") from None
            if permitted is not True:
                raise TransportUnavailable("owner binding revoked before send")
        req = request.Request("https://api.telegram.org/bot" + self._token + "/" + method,
                              data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout) as response:
                raw = response.read(2_000_001)
                if response.status != 200 or len(raw) > 2_000_000:
                    raise TransportUnavailable("Bot API result unavailable")
            answer = json.loads(raw)
            if not isinstance(answer, dict) or answer.get("ok") is not True:
                raise TransportUnavailable("Bot API refused request")
            return answer.get("result")
        except (OSError, ValueError, TypeError, error.HTTPError):
            raise TransportUnavailable("Bot API result unavailable") from None

    def download(self, file_id: str, target: Path, *, byte_limit: int) -> tuple[int, str]:
        """One bounded streamed file; caller owns its private temporary path."""
        import hashlib
        if not isinstance(file_id, str) or not 1 <= len(file_id) <= 512:
            raise TransportUnavailable("file identity unavailable")
        info = self.api("getFile", {"file_id": file_id})
        if not isinstance(info, dict) or type(info.get("file_size")) not in (int, type(None)):
            raise TransportUnavailable("file metadata unavailable")
        if type(info.get("file_size")) is int and info["file_size"] > byte_limit:
            raise MediaTooLarge("file exceeds policy limit")
        path = info.get("file_path")
        if (not isinstance(path, str) or not path or len(path) > 512
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or any(c in path for c in "\\?:#\r\n")):
            raise TransportUnavailable("file path unavailable")
        self.check_separation()
        req = request.Request("https://api.telegram.org/file/bot" + self._token + "/" + path,
                              method="GET")
        count = 0
        digest = hashlib.sha256()
        try:
            with self._opener.open(req, timeout=45) as response, target.open("xb") as stream:
                if response.status != 200:
                    raise TransportUnavailable("file download unavailable")
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > byte_limit:
                        raise MediaTooLarge("file exceeds policy limit")
                    stream.write(chunk)
                    digest.update(chunk)
                stream.flush()
                import os
                os.fsync(stream.fileno())
            if not count or (type(info.get("file_size")) is int and count != info["file_size"]):
                raise TransportUnavailable("file size mismatch")
            return count, digest.hexdigest()
        except (OSError, error.HTTPError):
            raise TransportUnavailable("file download unavailable") from None
