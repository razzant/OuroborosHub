"""Freshdesk REST v2 transport for one helpdesk and one agent's API key.

The key travels only to the helpdesk's own HTTPS origin: redirects are refused, pagination links
must stay on that origin, and attachment downloads (pre-signed storage links) never carry it.
Nothing is retried here: every answer carries the rate-limit facts Freshdesk sent, and a failure
says whether a write may have taken effect, so the caller decides what happens next.
"""
from __future__ import annotations

import base64
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException, RemoteDisconnected

USER_AGENT = "ouroboros-freshdesk-skill"
API_PREFIX = "/api/v2/"
HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.freshdesk\.com$")
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?', re.IGNORECASE)
_PATH_RE = re.compile(r"^[A-Za-z0-9_\-/.]+$")


def canonical_path(path: str) -> str:
    """One spelling per endpoint (``tickets/1/reply``), so a refusal by path cannot be stepped around with ``//``,
    ``./`` or ``.json``; anything else that is not a plain API path is refused."""
    path = str(path or "").strip()
    if path.startswith(API_PREFIX):
        path = path[len(API_PREFIX):]
    path = path.strip("/")
    if path.endswith(".json"):
        path = path[:-len(".json")]
    if not path or not _PATH_RE.match(path) or any(part in ("", ".", "..") for part in path.split("/")):
        raise FreshdeskError("path_invalid", f"Not a Freshdesk API path: {path!r}.")
    return path

# Whether Freshdesk may have applied a write when the call failed.
NOT_SENT = "not_sent"          # refused before any effect (validation, permission, not found)
RATE_LIMITED = "rate_limited"  # refused by the rate limit; nothing happened, retry after ``retry_after``
UNKNOWN = "unknown"            # timeout, disconnect or server error after the request may have arrived


class FreshdeskError(Exception):
    """A typed failure: a stable ``code``, Freshdesk's own words, its field errors and the effect fact."""

    def __init__(self, code: str, message: str, *, status: int = 0, effect: str = NOT_SENT,
                 retry_after: float | None = None, errors: list | None = None, limits: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.effect = code, message, status, effect
        self.retry_after, self.errors, self.limits = retry_after, list(errors or []), dict(limits or {})

    def as_dict(self) -> dict:
        value = {"ok": False, "code": self.code, "message": self.message, "effect": self.effect}
        if self.status:
            value["status"] = self.status
        if self.errors:
            value["errors"] = self.errors
        if self.retry_after is not None:
            value["retry_after"] = self.retry_after
        if self.limits:
            value["limits"] = self.limits
        return value


@dataclass
class Response:
    status: int
    data: object
    headers: dict
    limits: dict = field(default_factory=dict)
    next_url: str = ""


def canonical_host(domain: str) -> str:
    """``example``, ``example.freshdesk.com`` or any URL on it, as the lowercased host."""
    value = (domain or "").strip().lower()
    if "://" in value:
        parts = urllib.parse.urlsplit(value)
        if parts.scheme not in ("https", "http"):
            value = ""
        else:
            value = parts.hostname or ""
    else:
        value = value.split("/", 1)[0].split(":", 1)[0]
    value = value.rstrip(".")
    if value and "." not in value:
        value += ".freshdesk.com"
    if not HOST_RE.match(value):
        raise FreshdeskError(
            "domain_invalid", "Use the helpdesk's Freshdesk address, such as example.freshdesk.com. "
            "Freshdesk's API does not answer on a custom domain (CNAME).")
    return value


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def rate_limits(headers: dict) -> dict:
    """Every ``x-ratelimit-*`` header Freshdesk sent (numbers as numbers) and ``retry_after`` in seconds."""
    limits = {}
    for key, raw in headers.items():
        if not key.startswith("x-ratelimit-"):
            continue
        name = key[len("x-ratelimit-"):].replace("-", "_")
        try:
            number = float(raw)
            limits[name] = int(number) if number.is_integer() else number
        except (TypeError, ValueError):
            limits[name] = raw
    retry = _retry_after(headers.get("retry-after"))
    if retry is not None:
        limits["retry_after"] = retry
    return limits


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None


def _decode(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"raw": raw[:2000].decode("utf-8", "replace")}


def _error_code(status: int, data) -> str:
    provider = data.get("code") if isinstance(data, dict) else None
    if status == 429:
        return "rate_limited"
    if status == 401:
        return "auth_failed"
    if status == 403:
        return str(provider or "access_denied")
    if status == 404:
        return "not_found"
    if status == 405:
        return "method_not_allowed"
    if status == 409:
        return "conflict"
    if status == 400:
        return "invalid_request"
    if status >= 500:
        return "server_error"
    return str(provider or f"http_{status}")


def _message(status: int, data) -> str:
    if isinstance(data, dict):
        for key in ("message", "description"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key].strip()
    return f"Freshdesk answered HTTP {status}."


class Client:
    """One helpdesk. ``base_url`` exists only so tests can serve a fake Freshdesk on loopback."""

    def __init__(self, domain: str, api_key: str, *, timeout: float = 30.0, base_url: str = ""):
        self.host = canonical_host(domain)
        if not isinstance(api_key, str) or not api_key.strip():
            raise FreshdeskError("api_key_missing", "No Freshdesk API key is saved for this skill.")
        token = base64.b64encode(f"{api_key.strip()}:X".encode("utf-8")).decode("ascii")
        self._auth = "Basic " + token
        self.timeout = float(timeout)
        self.origin = base_url.rstrip("/") if base_url else "https://" + self.host
        context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))

    # -- URLs ---------------------------------------------------------------------------------------
    def url(self, path: str, params: dict | None = None) -> str:
        path = canonical_path(path)
        query = urllib.parse.urlencode(
            [(key, value) for key, value in (params or {}).items() if value is not None], doseq=True)
        return f"{self.origin}{API_PREFIX}{path}" + (f"?{query}" if query else "")

    def same_origin(self, url: str) -> bool:
        parts, mine = urllib.parse.urlsplit(url), urllib.parse.urlsplit(self.origin)
        return (parts.scheme, parts.netloc) == (mine.scheme, mine.netloc) and parts.path.startswith(API_PREFIX)

    # -- requests -----------------------------------------------------------------------------------
    def request(self, method: str, path: str = "", *, params: dict | None = None, body=None,
                multipart: tuple | None = None, url: str = "", timeout: float | None = None) -> Response:
        method = method.upper()
        target = url or self.url(path, params)
        if url and not self.same_origin(url):
            raise FreshdeskError("foreign_link", "A pagination link left this helpdesk's API; it was not followed.")
        headers = {"Authorization": self._auth, "Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if multipart is not None:
            data, content_type = _multipart(*multipart)
            headers["Content-Type"] = content_type
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(target, data=data, method=method, headers=headers)
        writes = method != "GET"
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as answer:
                raw, status = answer.read(), answer.status
                answer_headers = {key.lower(): value for key, value in answer.headers.items()}
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp else b""
            answer_headers = {key.lower(): value for key, value in (exc.headers or {}).items()}
            status, payload = exc.code, _decode(raw)
            limits = rate_limits(answer_headers)
            if 300 <= status < 400:
                raise FreshdeskError("redirect_refused", "Freshdesk redirected the request; the key is never sent "
                                     "to another address.", status=status, limits=limits) from None
            effect = RATE_LIMITED if status == 429 else (UNKNOWN if status >= 500 and writes else NOT_SENT)
            errors = payload.get("errors") if isinstance(payload, dict) else None
            raise FreshdeskError(_error_code(status, payload), _message(status, payload), status=status,
                                 effect=effect, retry_after=limits.get("retry_after"), errors=errors,
                                 limits=limits) from None
        except (TimeoutError, socket.timeout) as exc:
            raise FreshdeskError("timeout", f"Freshdesk did not answer in {timeout or self.timeout:g} s.",
                                 effect=UNKNOWN if writes else NOT_SENT) from exc
        except (RemoteDisconnected, ConnectionResetError, HTTPException) as exc:
            raise FreshdeskError("disconnected", "The connection to Freshdesk broke before an answer.",
                                 effect=UNKNOWN if writes else NOT_SENT) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise FreshdeskError("timeout", "Freshdesk did not answer in time.",
                                     effect=UNKNOWN if writes else NOT_SENT) from exc
            sent = isinstance(reason, (ConnectionResetError, RemoteDisconnected))
            raise FreshdeskError("unreachable", f"Freshdesk could not be reached: {reason}.",
                                 effect=UNKNOWN if writes and sent else NOT_SENT) from exc
        limits = rate_limits(answer_headers)
        found = _NEXT_LINK.search(answer_headers.get("link", ""))
        return Response(status, _decode(raw), answer_headers, limits, found.group(1) if found else "")

    def get(self, path: str, params: dict | None = None) -> Response:
        return self.request("GET", path, params=params)

    def post(self, path: str, body=None, *, multipart: tuple | None = None) -> Response:
        return self.request("POST", path, body=body, multipart=multipart)

    def put(self, path: str, body=None, *, multipart: tuple | None = None) -> Response:
        return self.request("PUT", path, body=body, multipart=multipart)

    def delete(self, path: str) -> Response:
        return self.request("DELETE", path)

    def next_page(self, response: Response) -> Response | None:
        return self.request("GET", url=response.next_url) if response.next_url else None


def _multipart(fields: list, files: list) -> tuple[bytes, str]:
    """``fields``: (name, value) pairs; ``files``: (name, filename, bytes, content type) tuples."""
    boundary = "ouroboros-" + uuid.uuid4().hex
    chunks = []
    for name, value in fields:
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(name)}"\r\n\r\n'.encode())
        chunks.append(str(value).encode("utf-8") + b"\r\n")
    for name, filename, content, content_type in files:
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(name)}"; '
                       f'filename="{_quote(filename)}"\r\nContent-Type: {content_type or "application/octet-stream"}'
                       "\r\n\r\n").encode("utf-8"))
        chunks.append(bytes(content) + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _quote(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', "%22").replace("\r", " ").replace("\n", " ")


def download(url: str, max_bytes: int, *, timeout: float = 60.0) -> tuple[bytes, str]:
    """Fetch an attachment's pre-signed link without credentials; refuse more than ``max_bytes``."""
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")):
        raise FreshdeskError("attachment_url_invalid", "The attachment link is not an HTTPS address.")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            content_type = answer.headers.get("Content-Type", "application/octet-stream")
            data = answer.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise FreshdeskError("attachment_unavailable", f"The attachment link answered HTTP {exc.code}; links "
                             "expire, so read the ticket again for a fresh one.", status=exc.code) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, HTTPException, ConnectionResetError) as exc:
        raise FreshdeskError("attachment_unavailable", f"The attachment could not be downloaded: {exc}.") from exc
    if len(data) > max_bytes:
        raise FreshdeskError("attachment_too_large", f"The attachment is larger than {max_bytes} bytes.")
    return data, content_type
