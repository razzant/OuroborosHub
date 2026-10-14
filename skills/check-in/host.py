"""The one Host Service call this skill makes: ``POST /notify`` on loopback.

Only HTTP 200 with ``ok: true`` confirms that the owner's chat row was written.
A 503 means "not confirmed" (the row may or may not exist; the host asks not to
post blindly again); 400/403/429 and an unreachable host mean nothing was
written; a timeout or any other answer is unknown. The token is revealed only
where the request header is built and is never logged or returned.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict

NOTICE_MAX_CHARS = 400


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):  # never follow a redirect off loopback
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _port() -> int:
    try:
        port = int(os.environ.get("OUROBOROS_HOST_SERVICE_PORT") or "8767")
    except ValueError:
        return 0
    return port if 1 <= port <= 65535 else 0


def post_notice(token: Any, text: str, *, timeout: float = 15.0) -> Dict[str, Any]:
    """Return ``{"outcome": confirmed|not_confirmed|refused|unknown, "http_status": int|None}``."""
    text = " ".join(str(text or "").split())[:NOTICE_MAX_CHARS]
    port = _port()
    if not text or not port:
        return {"outcome": "refused", "http_status": None, "code": "invalid_request"}
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/notify",
        data=json.dumps({"text": text}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-Skill-Token": token.use_in_request()},
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:  # noqa: S310 - loopback Host Service
            status = int(response.status)
            try:
                body = json.loads(response.read(4096).decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                body = {}
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        exc.close()
        if status == 503:
            return {"outcome": "not_confirmed", "http_status": status}
        if status in (400, 403, 429):
            return {"outcome": "refused", "http_status": status}
        return {"outcome": "unknown", "http_status": status}
    except ConnectionRefusedError:
        return {"outcome": "refused", "http_status": None, "code": "host_unreachable"}
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), ConnectionRefusedError):
            return {"outcome": "refused", "http_status": None, "code": "host_unreachable"}
        return {"outcome": "unknown", "http_status": None, "code": "transport_error"}
    except OSError:
        return {"outcome": "unknown", "http_status": None, "code": "transport_error"}
    if status == 200 and isinstance(body, dict) and body.get("ok") is True:
        return {"outcome": "confirmed", "http_status": status}
    return {"outcome": "unknown", "http_status": status}
