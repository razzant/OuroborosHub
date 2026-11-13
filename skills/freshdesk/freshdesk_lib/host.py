"""The Host Service (loopback) as this connector uses it: identity, place state, Presence turns, receipts, work.

In process the token comes from ``api.get_skill_token()`` and the address from ``OUROBOROS_HOST_SERVICE_PORT``
(the telegram-bot connector does the same); the companion gets ``HOST_SERVICE_URL``/``HOST_SERVICE_TOKEN``.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

PROVIDER = "freshdesk"


class HostUnavailable(Exception):
    def __init__(self, code: str, status: int = 0, data=None):
        super().__init__(code)
        self.code, self.status, self.data = code, status, data


def default_base_url() -> str:
    url = (os.environ.get("HOST_SERVICE_URL") or "").strip()
    if url:
        return url.rstrip("/")
    try:
        port = int(os.environ.get("OUROBOROS_HOST_SERVICE_PORT") or "8767")
    except ValueError:
        port = 8767
    return f"http://127.0.0.1:{port}"


class HostLink:
    def __init__(self, token, base_url: str = "", *, transport=None):
        """``token``: a callable returning the skill token string (refreshed per call; edits invalidate tokens).
        ``transport``: tests only, a callable ``(method, path, headers, body, params) -> (status, payload)``."""
        self._token = token
        self._transport = transport
        self.base_url = (base_url or default_base_url()).rstrip("/")
        host = urllib.parse.urlsplit(self.base_url).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the Host Service is reached on loopback only")

    def request(self, method: str, path: str, *, body=None, params: dict | None = None, timeout: float = 15.0):
        headers = {"x-skill-token": self._token(), "Accept": "application/json"}
        if self._transport is not None:
            return self._transport(method, path, headers, body, params)
        url = self.base_url + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310 - loopback only
                raw, status = answer.read(), answer.status
        except urllib.error.HTTPError as exc:
            raw, status = (exc.read() if exc.fp else b""), exc.code
        except (urllib.error.URLError, OSError) as exc:
            raise HostUnavailable("host_unreachable") from exc
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            payload = {"raw": raw[:500].decode("utf-8", "replace")}
        return status, payload

    def identity(self) -> dict:
        status, data = self.request("GET", "/identity", timeout=10)
        if status != 200 or not isinstance(data, dict):
            raise HostUnavailable("identity_unavailable", status, data)
        return data

    def place(self, account_id: str, conversation_id: str = "", thread_id: str = "") -> dict:
        """``{"state": "active" | "silent" | "paused", "label", "place_id"}``, or ``{"state": "unbound"}`` when no
        place holds this destination (tools then work as ordinary tools)."""
        status, data = self.request("GET", "/presence/place", params={
            "provider": PROVIDER, "account_id": account_id, "conversation_id": conversation_id,
            "thread_id": thread_id}, timeout=10)
        if status == 404 and isinstance(data, dict) and data.get("code") in ("place_not_connected", None):
            return {"state": "unbound"}
        if status != 200 or not isinstance(data, dict):
            raise HostUnavailable("place_unavailable", status, data)
        return {"state": str(data.get("state") or "").lower(), "label": data.get("label") or "",
                "place_id": data.get("place_id") or ""}

    def bind(self, account_id: str, conversation_id: str, thread_id: str, *, require_place: bool = True) -> dict:
        status, data = self.request("GET", "/presence/place", params={
            "provider": PROVIDER, "account_id": account_id, "conversation_id": conversation_id,
            "thread_id": thread_id, "operation": "bind", "require_place": "true" if require_place else "false"})
        if status not in (200, 404, 409) or not isinstance(data, dict):
            raise HostUnavailable("bind_unavailable", status, data)
        return data

    def check(self, binding: dict, account_id: str, conversation_id: str, thread_id: str) -> dict:
        """The retained binding's verdict. A 400 (``place_binding_invalid``: it no longer fits this connector or
        destination) is a verdict too, not an outage: the output stays held instead of retrying forever."""
        status, data = self.request("GET", "/presence/place", params={
            "provider": PROVIDER, "account_id": account_id, "conversation_id": conversation_id,
            "thread_id": thread_id, "operation": "check", "binding": json.dumps(binding, separators=(",", ":"))})
        if status not in (200, 400, 404, 409) or not isinstance(data, dict):
            raise HostUnavailable("check_unavailable", status, data)
        return data

    def turn(self, payload: dict, timeout: float = 1800.0):
        return self.request("POST", "/presence/turn", body=payload, timeout=timeout)

    def delivery(self, report: dict):
        return self.request("POST", "/presence/delivery", body=report, timeout=15)

    def work(self, ref: str):
        return self.request("GET", "/presence/work/" + urllib.parse.quote(ref, safe=""), timeout=40)
