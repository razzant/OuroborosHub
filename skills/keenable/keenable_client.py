"""Keenable streamable-HTTP MCP client: transport, error taxonomy, result parsing.

This module deliberately imports NO PluginAPI surface, so it stays directly
importable for verification and unit checks. ``plugin.py`` is the only place
that talks to the host.

Protocol, probed live against ``POST https://api.keenable.ai/mcp``:
  1. ``initialize``                -> 200, optional ``mcp-session-id`` header
  2. ``notifications/initialized`` -> 202, empty body
  3. ``tools/call``                -> 200, ``result.content[0].text``

The observed vendor responses carry text blocks, so search results are parsed
here; a structured-only response is not claimed to be supported. A tool-level failure arrives as HTTP 200 with
``result.isError = true``. An expired session arrives as HTTP 404 with JSON-RPC
``-32001``; when a session ID was sent, we re-initialize and retry once.
Successful initialization without a session ID is cached too. Subsequent requests
carry the negotiated protocol version in both modes.

THE ONE ENVELOPE RULE (v0.2.0)
------------------------------
This skill never asserts anything about the SOURCE it did not observe. Concretely:

* Fields naming what the SKILL did carry that ownership in the name
  (``content_truncated_by_skill``, ``skill_content_char_limit``,
  ``filters_requested``). v0.1.0 shipped ``content_truncated`` /
  ``content_char_limit`` / ``filters``, which only ever attested OUR clipping and
  OUR outgoing arguments, and were read as vendor completeness and vendor
  filter-enforcement guarantees. That misreading is the whole reason this
  revision exists.
* ``vendor_content_complete`` is ALWAYS ``None``. We cannot know it, so we never
  imply it.
* ``measured`` carries raw counts, not conclusions. A measurement cannot be a
  false negative; a verdict can. There is deliberately NO boolean on the
  completeness axis -- a ``False`` there would be a machine-emitted completeness
  attestation, which is the exact defect being removed.
* ``content_incompleteness_indicators`` is a LIST. Empty means "no indicator
  found", NOT "content is complete".
* Explanatory prose lives in the tool descriptions and SKILL.md, which are paid
  for once, not in every envelope on every call.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

MCP_URL = "https://api.keenable.ai/mcp"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "ouroboros-keenable-skill"
CLIENT_VERSION = "0.4.0"

#: Per-request vendor timeout. Named on purpose: the 600s outer tool timeout is far
#: too coarse, and a route handler must not hold the event loop for minutes.
TIMEOUT_SEC = 45.0
#: Budget for ONE logical operation -- every HTTP leg it needs, including a session
#: re-initialize and the single retry, together.
#:
#: Why this exists: ``TIMEOUT_SEC`` bounds each leg, and a cold call makes three
#: (initialize, notifications/initialized, tools/call). Three 45-second legs is 135
#: seconds, while ``SKILL.md`` registers the tools with ``timeout_sec: 90`` -- so the
#: per-leg bound could be honoured exactly while the operation as a whole blew
#: through the host's deadline, and the host would then kill the call with the
#: request thread still inside vendor I/O. A per-leg cap is not a bound on the
#: operation; only a shared deadline is.
#:
#: 80s leaves the host's 90s a real margin for its own dispatch and for JSON work
#: on either side of the network legs. Raising the manifest timeout instead was the
#: alternative and is worse: it would make the tool slower to fail without making
#: the total bounded, which is the actual defect.
OPERATION_BUDGET_SEC = 80.0
#: Below this much remaining budget a further HTTP leg cannot plausibly succeed, so
#: it is refused as a timeout instead of being started and abandoned mid-flight.
MIN_USEFUL_TIMEOUT_SEC = 2.0
#: Hard read bound so a hostile or broken response cannot exhaust memory.
MAX_RESPONSE_BYTES = 2_000_000
#: Skill-side result bounds. Extension tool results are NOT untruncated by the
#: host, so we stay well under its per-tool window instead of being clipped.
RAW_TEXT_LIMIT = 9000
#: Default page-content bound. Sized from the SERIALIZED envelope, not chosen: at
#: 12000 a full page measured 15099 serialized chars against the host's 15000 cap,
#: so the host would have cut it while ``content_truncated_by_skill`` still said
#: false -- a false completeness signal reaching the agent, which is the one outcome
#: this envelope exists to prevent. The fetch envelope's overhead is ~3100 chars
#: because it echoes the extraction prompt (up to 2000) alongside the url, served
#: metadata and disclosure fields. 9500 measures at 12600 with ~2400 to spare.
#:
#: This is the number sent to the vendor as ``max_chars``, so the reduction also
#: means we do not pay for text we would discard.
CONTENT_CHAR_LIMIT = 9500
DEFAULT_SNIPPET_MAX_LENGTH = 400
DEFAULT_FETCH_MAX_CHARS = 8000
MAX_RESULTS = 12
#: Per-record and aggregate result bounds. MAX_RESULTS alone is not enough: the
#: vendor accepts snippet_max_length up to 10000, so 12 records could otherwise
#: reach ~120KB and be clipped by the HOST instead of disclosed by us.
SNIPPET_CHAR_LIMIT = 1200
FIELD_CHAR_LIMIT = 500
#: Bounds accumulated RETAINED FIELD TEXT across all kept records -- not
#: serialized JSON bytes, which additionally carry keys, syntax, and the
#: disclosure metadata below. Enforced per FIELD write, so it is a real ceiling.
RESULTS_TEXT_BUDGET = 10000
#: Ceiling for the SERIALIZED search envelope, which is the number that actually
#: has to fit somewhere.
#:
#: The host caps a generic tool result at ``DEFAULT_TOOL_RESULT_LIMIT`` = 15000 chars
#: (``ouroboros/tool_capabilities.py``) and extension tools are not exempt. A budget
#: over RETAINED FIELD TEXT is therefore not a bound on what the host receives:
#: measured, 20000 retained chars serialized to 20066 — the host would have
#: generically truncated an envelope whose whole purpose is that ITS bounds are the
#: disclosed ones. Worse, on a non-clean parse the raw vendor text (up to
#: ``RAW_TEXT_LIMIT``) rides on top of the records, so the two disclosed bounds could
#: not both be honoured at once.
#:
#: 13500 keeps ~1500 chars of margin under the host cap. Measured serialized sizes at
#: RESULTS_TEXT_BUDGET=10000: 12237 with records only, and `raw` then takes whatever
#: is left rather than a fixed slice.
ENVELOPE_CHAR_BUDGET = 13500
#: Serialized cost of the four ``raw*`` keys and their JSON syntax, reserved before
#: computing how much raw text still fits.
RAW_KEYS_OVERHEAD = 140
#: A record is refused outright once the remaining budget drops below this,
#: rather than being admitted and then clipped into uselessness. Rationale: an
#: omitted record is honest and counted in ``results_omitted``, while a record
#: whose ``url`` was clipped to a prefix is a BROKEN LINK the operator cannot
#: tell apart from a working one. Sized as a minimally useful url + title.
MIN_VIABLE_RECORD_CHARS = 160
MAX_EXTRA_FIELDS = 8
#: Bound for a failure envelope's ``message``. Vendor-controlled text reaches it --
#: ``_call_once`` builds ``keenable_tool_error`` from the vendor's own tool output and
#: ``_request`` builds protocol errors from JSON-RPC messages -- so without this an
#: error envelope could approach ``MAX_RESPONSE_BYTES`` and be truncated by the HOST,
#: taking the typed ``error``/``error_class`` fields with it. Losing the disclosure on
#: a FAILURE is the worst case of all: ``error_class: not_read`` is precisely the
#: field that stops an unread page being read as an absent fact.
ERROR_MESSAGE_LIMIT = 1200
#: Passes ``fit_envelope`` may take over the designated body. Bounded rather than
#: ``while True`` on purpose: each pass strictly shrinks the body, so convergence is
#: monotone and two passes suffice in practice (one for the overflow, one to absorb
#: the disclosure keys that pass added) -- but an unbounded loop inside a request
#: path is a hazard even when the maths says it terminates.
_FIT_MAX_PASSES = 4

#: Vendor-documented ceiling for the fetch extraction prompt. Enforced locally so
#: the schema's stated bound is real rather than decorative.
PROMPT_MAX_LENGTH = 2000

#: Bound for the ``body_under_<N>_chars`` MEASUREMENT below. It is deliberately a
#: measurement and not a verdict: an intentionally short page (a status page, a
#: one-line answer) trips it legitimately, which is why the indicator is named for
#: the number it compared against rather than for a conclusion about the page.
#:
#: Why 500 at all, when this module otherwise refuses magic thresholds: the three
#: interception pages measured live on 2026-08-03 came back at 146, 225 and 267
#: characters (t.me, OpenReview's "Verifying your browser", Google Scholar's
#: "We're sorry..."), while every genuinely extracted page in the same probe run
#: was >= 2454. The gap is an order of magnitude, so the exact cut-off inside it
#: does not matter -- which is precisely why a threshold is defensible here.
SHORT_BODY_CHARS = 500
#: Exported so ``plugin.py`` keys its human-readable text off the same name; a
#: hand-copied literal in two files would drift the moment the bound changes.
SHORT_BODY_INDICATOR = f"body_under_{SHORT_BODY_CHARS}_chars"
REDIRECT_INDICATOR = "served_url_differs_from_request"

VENDOR_LIMITS = (
    "Keenable: 1000 requests/hour per IP without a key, 10 requests/second; "
    "the vendor sends no Retry-After or X-RateLimit headers."
)

#: Error classes. ``not_read`` is the load-bearing one: the request produced NO
#: page content, so it is not evidence that a source lacks anything. The
#: distinction matters because the vendor's own message for a failed extraction
#: says the page WAS reached -- "we never read the page" would be false, while
#: "this is not evidence of absence" is provable.
ERROR_CLASSES: Dict[str, str] = {
    "keenable_tool_error": "not_read",
    "keenable_timeout": "not_read",
    "keenable_server_error": "not_read",
    "keenable_rate_limited": "not_read",
    "keenable_transport_error": "not_read",
    "keenable_session_lost": "not_read",
    "keenable_protocol_error": "not_read",
    "keenable_bad_request": "local_rejection",
    "keenable_auth_invalid_key": "auth",
    "keenable_auth_required": "auth",
}

#: (url, body, headers, timeout) -> (status, lowercased_headers, text)
Transport = Callable[[str, bytes, Dict[str, str], float], Tuple[int, Dict[str, str], str]]

_SEARCH_ARG_KEYS = (
    "query", "site", "acquired_after", "acquired_before",
    "published_after", "published_before", "snippet_max_length", "mode",
)
_FETCH_ARG_KEYS = ("url", "max_chars", "live", "prompt")
#: Caller options that are validated like any other argument but are OURS -- they
#: change what this skill returns and are never forwarded to the vendor.
_LOCAL_ARG_KEYS = ("include_raw",)
#: Date filters whose compliance is OBSERVABLE in the returned records, mapped to
#: the record field that carries the comparable value. Both pairs are observable:
#: a record carries ``acquired`` as well as ``published``, and v0.2.0 checked only
#: the ``published`` pair for no reason other than that it was the pair the
#: original incident happened to involve. ``acquired`` is in fact the MORE reliable
#: of the two on this vendor -- live 2026-08-03, every record carried an
#: ``acquired`` date while ``published`` was frequently empty.
_OBSERVABLE_FILTERS = {
    "published_after": "published",
    "published_before": "published",
    "acquired_after": "acquired",
    "acquired_before": "acquired",
}
#: The ``_after`` half, whose bound can be compared against the newest date this
#: response actually shows -- see ``observe_index_freshness``.
_AFTER_FILTERS = ("published_after", "acquired_after")

#: Counts markup-looking tokens in returned text. A COUNT, never a verdict:
#: a page of code samples legitimately contains tags, so the caller decides what
#: the number means. High counts in what should be markdown indicate the vendor's
#: extractor fell through on a structured document (reproduced on dblp .xml).
_TAG_LIKE = re.compile(r"<[A-Za-z][A-Za-z0-9:_.-]*(?:\s[^<>]{0,400})?/?>")
_ABSOLUTE_LINK = re.compile(r"https?://")
#: A vendor record block always carries ``Label: value`` lines. Used ONLY to tell
#: "record-shaped but unparseable" from "not record-shaped at all"; it matches no
#: specific words, so there is no keyword table to go stale.
_LABELLED_LINE = re.compile(r"^\s*[A-Za-z][A-Za-z ._-]{0,40}:", re.MULTILINE)

_LOCK = threading.Lock()
_SESSION: Dict[str, Optional[str]] = {"id": None, "key_fp": None, "protocol_version": None}


class KeenableError(Exception):
    """Typed vendor/transport failure carrying the code the agent will see."""

    def __init__(self, code: str, message: str, http_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status

    def to_dict(self, tool: str, auth: str) -> Dict[str, Any]:
        """Failure envelope.

        ``error_class`` is the SINGLE authority on what the failure implies; a
        second boolean saying the same thing would be dual authority for one
        question. ``not_read`` means no page content was produced, so the result
        is not evidence that a source lacks the requested information. The
        sentence explaining that lives in the tool description, not here.
        """
        message, truncated, total = clip(self.message, ERROR_MESSAGE_LIMIT)
        out: Dict[str, Any] = {
            "ok": False,
            "tool": tool,
            "auth": auth,
            "error": self.code,
            "error_class": ERROR_CLASSES.get(self.code, "not_read"),
            "message": message,
            "http_status": self.http_status,
            "vendor_limits": VENDOR_LIMITS,
        }
        if truncated:
            out["message_truncated"] = True
            out["message_chars_total"] = total
        return out


def _key_fingerprint(key: str) -> str:
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def reset_session_cache() -> None:
    """Forget the cached MCP session (used by on_unload and on session loss)."""
    with _LOCK:
        _SESSION["id"] = None
        _SESSION["key_fp"] = None
        _SESSION["protocol_version"] = None


def _urllib_transport(
    url: str, body: bytes, headers: Dict[str, str], timeout: float
) -> Tuple[int, Dict[str, str], str]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = _read_bounded(response)
            return int(response.status), _lower_headers(response.headers), raw
    except urllib.error.HTTPError as exc:  # a real HTTP status, not a transport failure
        raw = _read_bounded(exc) if hasattr(exc, "read") else ""
        return int(exc.code), _lower_headers(getattr(exc, "headers", {}) or {}), raw


def _read_bounded(stream: Any) -> str:
    """Read at most ``MAX_RESPONSE_BYTES`` and FAIL if the body exceeded it.

    The extra byte is read on purpose: it is how we detect the overflow. Returning
    the first ``MAX_RESPONSE_BYTES`` as though they were the whole body is the
    defect this whole revision exists to remove -- a payload cut at a transport
    boundary would parse into apparently valid results with no truncation
    disclosure anywhere, which is silent data loss dressed as a complete answer.
    A hard typed failure is the honest outcome: this bound exists to stop a broken
    or hostile response exhausting memory, and a body that hits it is not a
    response we can represent truthfully.
    """
    raw = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise KeenableError(
            "keenable_protocol_error",
            f"Keenable response exceeded the {MAX_RESPONSE_BYTES}-byte read bound; "
            "refusing to parse a partial body as if it were complete",
        )
    return raw.decode("utf-8", "replace")


def _lower_headers(headers: Any) -> Dict[str, str]:
    try:
        items = headers.items()
    except AttributeError:
        return {}
    return {str(key).lower(): str(value) for key, value in items}


def _decode_body(text: str, status: int) -> Dict[str, Any]:
    """Accept a JSON body or an SSE ``data:`` frame; empty body means no payload."""
    stripped = (text or "").strip()
    if not stripped:
        return {}
    if stripped[0] in "{[":
        try:
            decoded = json.loads(stripped)
        except ValueError as exc:
            raise KeenableError("keenable_protocol_error", f"invalid JSON body: {exc}", status) from exc
        return decoded if isinstance(decoded, dict) else {"result": decoded}
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            decoded = json.loads(payload)
        except ValueError as exc:
            raise KeenableError("keenable_protocol_error", f"invalid SSE data frame: {exc}", status) from exc
        return decoded if isinstance(decoded, dict) else {"result": decoded}
    raise KeenableError("keenable_protocol_error", "response body was neither JSON nor an SSE data frame", status)


def _raise_for_status(status: int, text: str, has_key: bool, session_id: Optional[str] = None) -> None:
    if 200 <= status < 300:
        return
    detail = (text or "").strip()[:400]
    if status == 401:
        if has_key:
            raise KeenableError(
                "keenable_auth_invalid_key",
                "Keenable rejected KEENABLE_API_KEY (401). Remove or correct the key in "
                f"Settings -> Secrets to return to the keyless path. Vendor said: {detail}",
                status,
            )
        raise KeenableError(
            "keenable_auth_required",
            "Keenable now requires authentication on the keyless MCP endpoint. "
            f"Vendor said: {detail}",
            status,
        )
    if status == 404 and session_id:
        raise KeenableError("keenable_session_lost", f"MCP session not found (404): {detail}", status)
    if status == 429:
        raise KeenableError(
            "keenable_rate_limited",
            "Keenable rate limit hit (429). Not retried automatically. " + VENDOR_LIMITS,
            status,
        )
    if status >= 500:
        raise KeenableError("keenable_server_error", f"Keenable server error {status}: {detail}", status)
    raise KeenableError("keenable_protocol_error", f"unexpected HTTP {status}: {detail}", status)


def _leg_timeout(deadline: Optional[float]) -> float:
    """Timeout for the next HTTP leg: the per-leg cap, or whatever budget is left.

    Raises rather than returning a uselessly small number, so an operation that has
    already spent its budget fails as a typed timeout instead of starting a leg it
    cannot finish. ``deadline is None`` keeps the historical per-leg-only behaviour
    for direct callers that pass no budget.
    """
    if deadline is None:
        return TIMEOUT_SEC
    remaining = deadline - time.monotonic()
    if remaining <= MIN_USEFUL_TIMEOUT_SEC:
        raise KeenableError(
            "keenable_timeout",
            f"Keenable operation exceeded its {OPERATION_BUDGET_SEC:g}s total budget "
            "before this request could be completed",
        )
    return min(TIMEOUT_SEC, remaining)


def _request(
    payload: Dict[str, Any],
    session_id: Optional[str],
    key: str,
    transport: Transport,
    deadline: Optional[float] = None,
    protocol_version: Optional[str] = None,
) -> Dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": f"{CLIENT_NAME}/{CLIENT_VERSION}",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if protocol_version:
        headers["MCP-Protocol-Version"] = protocol_version
    if key:
        headers["X-API-Key"] = key
    body = json.dumps(payload).encode("utf-8")
    # Computed BEFORE the call and reported in the timeout message, so the message
    # names the bound that actually applied rather than the per-leg constant.
    leg_timeout = _leg_timeout(deadline)
    try:
        status, response_headers, text = transport(MCP_URL, body, headers, leg_timeout)
    except (TimeoutError, socket.timeout) as exc:
        raise KeenableError("keenable_timeout", f"Keenable did not respond within {leg_timeout:g}s") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise KeenableError("keenable_timeout", f"Keenable did not respond within {leg_timeout:g}s") from exc
        raise KeenableError("keenable_transport_error", f"could not reach Keenable: {reason}") from exc
    except OSError as exc:
        raise KeenableError("keenable_transport_error", f"could not reach Keenable: {exc}") from exc

    _raise_for_status(status, text, bool(key), session_id)
    decoded = _decode_body(text, status)
    error = decoded.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = str(error.get("message") or "unknown JSON-RPC error")
        if code == -32001 and session_id:
            raise KeenableError("keenable_session_lost", f"MCP session not found: {message}", status)
        raise KeenableError("keenable_protocol_error", f"JSON-RPC error {code}: {message}", status)
    decoded["_headers"] = _lower_headers(response_headers)
    return decoded


#: Slice for the ADVISORY ``notifications/initialized`` leg. It is fire-and-forget
#: -- its failure is already swallowed below because the session is usable without
#: it -- so it must not be allowed to spend the budget that the actual tools/call
#: needs. Capped separately rather than sharing the operation deadline.
NOTIFY_BUDGET_SEC = 10.0


def _initialize(
    key: str, transport: Transport, deadline: Optional[float] = None,
) -> Tuple[Optional[str], str]:
    decoded = _request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        },
        None,
        key,
        transport,
        deadline,
    )
    result = decoded.get("result")
    if (decoded.get("jsonrpc") != "2.0" or type(decoded.get("id")) is not int
            or decoded["id"] != 1 or not isinstance(result, dict)):
        raise KeenableError("keenable_protocol_error", "initialize returned an invalid JSON-RPC result")
    protocol_version = result.get("protocolVersion")
    if protocol_version != PROTOCOL_VERSION:
        raise KeenableError(
            "keenable_protocol_error", f"initialize returned unsupported protocol version: {protocol_version!r}",
        )
    server_info = result.get("serverInfo")
    if (not isinstance(result.get("capabilities"), dict) or not isinstance(server_info, dict)
            or not all(isinstance(server_info.get(field), str) and server_info[field]
                       for field in ("name", "version"))):
        raise KeenableError("keenable_protocol_error", "initialize returned an invalid InitializeResult")
    session_id = str(decoded.get("_headers", {}).get("mcp-session-id") or "").strip() or None
    notify_deadline = time.monotonic() + NOTIFY_BUDGET_SEC
    if deadline is not None:
        notify_deadline = min(notify_deadline, deadline)
    try:
        _request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id,
            key,
            transport,
            notify_deadline,
            protocol_version,
        )
    except KeenableError:
        pass  # advisory notification; the session is already usable
    return session_id, protocol_version


def _ensure_session(
    key: str, transport: Transport, deadline: Optional[float] = None,
) -> Tuple[Optional[str], str]:
    fingerprint = _key_fingerprint(key)
    with _LOCK:
        version = _SESSION["protocol_version"]
        if version is not None and _SESSION["key_fp"] == fingerprint:
            return _SESSION["id"], version
    # network I/O stays outside the lock
    session_id, version = _initialize(key, transport, deadline)
    with _LOCK:
        _SESSION["id"] = session_id
        _SESSION["key_fp"] = fingerprint
        _SESSION["protocol_version"] = version
    return session_id, version


def _call_once(
    name: str,
    arguments: Dict[str, Any],
    key: str,
    transport: Transport,
    deadline: Optional[float] = None,
) -> str:
    session_id, version = _ensure_session(key, transport, deadline)
    decoded = _request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}},
        session_id,
        key,
        transport,
        deadline,
        version,
    )
    result = decoded.get("result")
    if not isinstance(result, dict):
        raise KeenableError("keenable_protocol_error", "tools/call returned no result object")
    text = _first_text(result.get("content"))
    if result.get("isError"):
        raise KeenableError("keenable_tool_error", text or "Keenable reported a tool error")
    if not text:
        raise KeenableError("keenable_protocol_error", "tools/call returned no text content")
    return text


def _first_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part).strip()


def call_tool(name: str, arguments: Dict[str, Any], key: str = "", transport: Optional[Transport] = None) -> str:
    """Call one vendor tool, re-initializing the session at most once.

    ONE deadline covers the whole operation -- the first attempt, the session
    re-initialize, and the retry. The retry deliberately does not get a fresh
    budget: the point is to bound what the host is waiting for, and a second full
    budget would double exactly the number that must stay under the registered tool
    timeout.
    """
    active = transport or _urllib_transport
    deadline = time.monotonic() + OPERATION_BUDGET_SEC
    try:
        return _call_once(name, arguments, key, active, deadline)
    except KeenableError as first:
        if first.code != "keenable_session_lost":
            raise
    reset_session_cache()
    try:
        return _call_once(name, arguments, key, active, deadline)
    except KeenableError as second:
        if second.code == "keenable_session_lost":
            raise KeenableError(
                "keenable_protocol_error",
                "could not establish a Keenable MCP session after one retry",
                second.http_status,
            ) from second
        raise


def clip(text: str, limit: int) -> Tuple[str, bool, int]:
    """Bound a string and DISCLOSE the bound (never a silent slice)."""
    total = len(text)
    if total <= limit:
        return text, False, total
    return text[:limit], True, total


def _bound_echo(payload: Dict[str, Any], field: str, limit: int = FIELD_CHAR_LIMIT) -> None:
    """Bound an ECHOED value in place, disclosing the cut.

    Echoed fields are caller- or vendor-controlled and were previously unbounded:
    ``url`` and ``query`` come from the caller, ``served_url``/``served_title`` from
    the vendor's own response headers. Any of them can be arbitrarily long, so the
    envelope's size bound was only a bound on the parts we happened to think of.
    """
    value, truncated, total = clip(str(payload.get(field) or ""), max(0, limit))
    payload[field] = value
    if truncated:
        payload[f"{field}_truncated"] = True
        payload[f"{field}_chars_total"] = total


def fit_envelope(payload: Dict[str, Any], body_key: str) -> Dict[str, Any]:
    """Last resort: make the SERIALIZED envelope fit, and say what it cost.

    Every individual bound can be honoured while the total still overflows, because
    the total is a function of fields added at different times by different code
    paths. So the size that has to fit is measured once, at the end, on the real
    serialized payload -- and if it does not fit, the designated body text gives up
    the difference. Disclosed via ``envelope_overflow_trimmed``, never silent.

    ``results`` are dropped from the tail only after the body is exhausted, since a
    dropped record is already an understood disclosure (``results_omitted``) while a
    half-length body is not.
    """
    if len(json.dumps(payload, ensure_ascii=False)) <= ENVELOPE_CHAR_BUDGET:
        return payload
    payload["envelope_char_budget"] = ENVELOPE_CHAR_BUDGET

    # The trim is REMEASURED after every mutation, including the disclosure keys the
    # trim itself adds. A single measure-then-trim pass computed the overflow, cut
    # exactly that much, then appended ~75 characters of disclosure and returned
    # without looking again -- so the budget could be exceeded by the very act of
    # reporting that it had been enforced. On the fetch path there are no `results`
    # to fall back on, so nothing else would have caught it. A stated bound that the
    # reporting breaks is the same class of not-quite-true claim this whole envelope
    # exists to remove, so the loop converges instead of approximating.
    body = str(payload.get(body_key) or "")
    original_body_len = len(body)
    for _ in range(_FIT_MAX_PASSES):
        serialized = len(json.dumps(payload, ensure_ascii=False))
        if serialized <= ENVELOPE_CHAR_BUDGET:
            break
        current = str(payload.get(body_key) or "")
        if not current:
            break
        kept, _, _ = clip(current, max(0, len(current) - (serialized - ENVELOPE_CHAR_BUDGET)))
        if len(kept) == len(current):
            break  # nothing left to give on this axis
        payload[body_key] = kept
        payload["envelope_overflow_trimmed"] = original_body_len - len(kept)
        if body_key == "content":
            payload["content_truncated_by_skill"] = True
        elif body_key == "raw":
            payload["raw_truncated"] = True
            payload["raw_char_limit"] = len(kept)

    # Records go only after the body is exhausted: a dropped record is an already
    # understood disclosure (`results_omitted`), a half-length body is not. This loop
    # remeasures per iteration by construction and is bounded by the record count.
    results = payload.get("results")
    while isinstance(results, list) and results and \
            len(json.dumps(payload, ensure_ascii=False)) > ENVELOPE_CHAR_BUDGET:
        results.pop()
        payload["count"] = len(results)
        payload["results_omitted"] = int(payload.get("results_omitted") or 0) + 1

    # If both axes are spent and the envelope is STILL over, say so rather than
    # returning a number the payload contradicts. Reaching here needs the fixed
    # metadata alone to exceed the budget, which the bounded echo fields make
    # unreachable in practice -- but "unreachable in practice" is not a bound, and an
    # honest terminal disclosure costs one integer.
    final = len(json.dumps(payload, ensure_ascii=False))
    if final > ENVELOPE_CHAR_BUDGET:
        payload["envelope_over_budget_chars"] = final
    return payload


def resolve_max_chars(raw: Any, ceiling: int) -> Dict[str, Any]:
    """Normalize ``max_chars`` at the CLIENT boundary and disclose the outcome.

    The JSON schema guards only the tool path; the widget route and any direct
    import reach ``fetch()`` unvalidated, so normalization cannot live in the
    schema. Handles omitted, over-ceiling, zero, negative, bool, float, numeric
    string, and malformed input.

    ``bool`` is rejected explicitly BEFORE the int check, because ``bool`` is a
    subclass of ``int`` in Python: ``max_chars=True`` would otherwise resolve to
    1 and silently return a single character of the page.
    """
    ceiling = max(1, int(ceiling))
    requested: Optional[int] = None
    invalid = False
    if raw is None:
        pass
    elif isinstance(raw, bool):
        invalid = True
    elif isinstance(raw, int):
        requested = raw
    elif isinstance(raw, float):
        requested = int(raw) if raw == raw and raw not in (float("inf"), float("-inf")) else None
        invalid = requested is None
    elif isinstance(raw, str):
        try:
            requested = int(float(raw.strip()))
        except (TypeError, ValueError):
            invalid = True
    else:
        invalid = True

    if requested is not None and requested < 1:
        invalid = True
        requested = None

    effective = min(requested, ceiling) if requested is not None else min(DEFAULT_FETCH_MAX_CHARS, ceiling)
    out: Dict[str, Any] = {
        "max_chars_requested": requested,
        "max_chars_effective": effective,
        "max_chars_clamped": requested is not None and requested > ceiling,
    }
    if invalid:
        out["max_chars_invalid"] = True
    return out


def measure_content(text: str, effective_cap: int) -> Dict[str, Any]:
    """Raw COUNTS over the returned bytes. No thresholds, no verdicts.

    A measurement cannot be a false negative. Interpretation belongs to the LLM
    caller and to route-side presentation, which is why there is no URL-shape
    table and no magic length here -- both would be hand-maintained
    classification tables going stale against a changing web.
    """
    return {
        "content_chars": len(text),
        "equals_effective_cap": len(text) >= effective_cap,
        "absolute_http_link_count": len(_ABSOLUTE_LINK.findall(text)),
        "tag_like_token_count": len(_TAG_LIKE.findall(text)),
    }


def observed_service(text: str) -> Dict[str, Any]:
    """The page the vendor says it ACTUALLY served, read from its own header lines.

    Every fetch response opens with ``Title: ...`` and ``URL: ...`` written by the
    vendor. That echoed URL is the single most valuable fact in the whole envelope
    and v0.2.0 threw it away: when a site answers a bot challenge, the vendor
    happily extracts the CHALLENGE page and reports success. Live 2026-08-03,
    ``openreview.net/forum?id=Q8AtlPAbFn`` came back as
    ``openreview.net/challenge?redirect=...`` with ``ok: true`` and an empty
    indicator list -- a researcher reading that concludes the paper does not exist.

    Parsing is deliberately forgiving and fails SAFE: if the vendor ever stops
    emitting the header, both values are empty, the comparison below returns
    ``None`` (unknown), and no indicator fires. A format change can therefore cost
    us a signal, never invent one.
    """
    title = url = ""
    for line in (text or "").splitlines()[:4]:
        head, separator, value = line.partition(":")
        if not separator:
            continue
        label = head.strip().lower()
        if label == "title" and not title:
            title = value.strip()
        elif label == "url" and not url:
            url = value.strip()
    return {"served_title": title, "served_url": url}


def _url_identity(url: Any) -> str:
    """Host+path+query, normalized only where the difference cannot change the page.

    Scheme is dropped (an http->https upgrade is not a redirect worth reporting),
    ``www.`` and a trailing slash are folded. Everything else is compared verbatim:
    a differing path or query IS a different page, which is the whole point.
    """
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    path = (parts.path or "").rstrip("/")
    return host + path + (("?" + parts.query) if parts.query else "")


def served_url_matches_request(served_url: Any, requested_url: Any) -> Optional[bool]:
    """True / False / None, where None means "the vendor did not tell us".

    Tri-state on purpose. A boolean would have to guess on an unparseable or
    absent value, and guessing ``True`` there is exactly how a silent interception
    becomes a confident negative result.
    """
    served = _url_identity(served_url)
    requested = _url_identity(requested_url)
    if not served or not requested:
        return None
    return served == requested


def incompleteness_indicators(measured: Dict[str, Any], extraction_mode: str) -> List[str]:
    """Indicators derived ONLY from facts about our own bytes.

    An EMPTY list means "no indicator found" -- never "the content is complete".
    There is deliberately no boolean counterpart: a ``False`` on the completeness
    axis would be a machine-emitted completeness attestation, which is precisely
    the defect this revision removes.

    The redirect indicator is checked BEFORE the mode guard, because a vendor that
    served a different page than the one requested is a fact about WHICH PAGE was
    read, not about the shape of the body. v0.2.0 returned ``[]`` unconditionally
    for ``prompt_extraction``, so a prompt answer extracted from a bot-challenge
    page carried no signal at all -- the worst case, since a prompt answer is the
    one shape whose brevity looks normal.

    Body-shape indicators stay behind that guard: an extraction answer is DESIGNED
    to be short and prose-shaped, so measuring it against full-page expectations
    produces pure false positives.
    """
    found: List[str] = []
    if measured.get("served_url_matches_request") is False:
        found.append(REDIRECT_INDICATOR)
    if extraction_mode != "full_page":
        return found
    if measured.get("equals_effective_cap"):
        found.append("at_effective_cap")
    if measured.get("tag_like_token_count"):
        found.append("markup_like_tokens_present")
    if int(measured.get("content_chars") or 0) < SHORT_BODY_CHARS:
        found.append(SHORT_BODY_INDICATOR)
    return found


def parse_search_text(text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Vendor text blob -> records plus parse ACCOUNTING.

    Returns (records, accounting). Accounting exists because a single good record
    used to be enough for ``parsed: true`` while every other block was silently
    discarded -- one usable result hiding arbitrary loss, the same false-negative
    shape as a truncated page.

    ``parse_status``:
      ``records``    every block became a usable record
      ``partial``    at least one usable record AND at least one discarded block
      ``no_records`` text carries no ``Label: value`` lines at all, so it was
                     never record-shaped (the vendor's "No results found" reply
                     lands here -- a legitimate empty answer, NOT a parse failure)
      ``unparsed``   record-shaped text that yielded no usable record
    """
    blocks = [block.strip() for block in (text or "").split("\n\n---\n\n")]
    blocks = [block for block in blocks if block]
    records = [_parse_record(block) for block in blocks]
    usable = [record for record in records if record["url"] or record["title"]]

    if usable and len(usable) == len(blocks):
        status = "records"
    elif usable:
        status = "partial"
    elif _LABELLED_LINE.search(text or ""):
        status = "unparsed"
    else:
        status = "no_records"
    return usable, {
        "parse_status": status,
        "blocks_total": len(blocks),
        "records_recognized": len(usable),
        "blocks_unrecognized": max(0, len(blocks) - len(usable)),
    }


def _parse_record(block: str) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "title": "", "url": "", "published": "", "acquired": "", "snippet": "", "extra": {},
    }
    snippet_lines: List[str] = []
    in_snippet = False
    known = {"title", "url", "published", "acquired"}
    for line in block.splitlines():
        if in_snippet:
            snippet_lines.append(line)
            continue
        stripped = line.strip()
        if not stripped:
            continue
        head, separator, value = stripped.partition(":")
        label = head.strip().lower()
        value = value.strip()
        if separator and label == "snippets":
            in_snippet = True
            if value:
                snippet_lines.append(value)
        elif separator and label in known:
            record[label] = value
        elif separator and label and " " not in label:
            record["extra"][label] = value
        else:
            snippet_lines.append(line)
    record["snippet"] = "\n".join(snippet_lines).strip()
    return record


def _iso_date(value: Any) -> Optional[datetime.date]:
    """Parse a leading ``YYYY-MM-DD``, or None when the value is not a date."""
    text = str(value or "").strip()[:10]
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def observe_date_filters(
    filters_requested: Dict[str, Any], records: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Report what the returned records SHOW about each requested date filter.

    Three buckets, because missing metadata is NO evidence rather than
    counter-evidence: an empty ``published`` cannot prove the vendor ignored a
    filter. A live probe on 2026-08-03 returned 10/10 records with empty
    ``published`` for ``published_after=2026-07-30``, so a binary
    "filter not honored" flag would have fired on pure absence of data.

    No positive verdict is ever emitted: the best case is
    ``conclusion="no_observed_conflict"``, never "verified".
    """
    observations: Dict[str, Any] = {}
    for name, field in sorted(_OBSERVABLE_FILTERS.items()):
        if name not in filters_requested:
            continue
        bound = _iso_date(filters_requested.get(name))
        wants_after = name.endswith("_after")
        conforming = violating = unobservable = 0
        for record in records:
            seen = _iso_date(record.get(field))
            if seen is None or bound is None:
                unobservable += 1
            elif (seen >= bound) if wants_after else (seen <= bound):
                conforming += 1
            else:
                violating += 1
        observations[name] = {
            "requested": filters_requested.get(name),
            "compared_field": field,
            "observable_conforming": conforming,
            "observable_violating": violating,
            "unobservable": unobservable,
            "conclusion": (
                "observed_conflict" if violating
                else "not_verifiable" if not conforming
                else "no_observed_conflict"
            ),
        }
    return observations


def observe_index_freshness(
    records: List[Dict[str, Any]], filters_requested: Dict[str, Any]
) -> Dict[str, Any]:
    """The newest dates this response actually SHOWS, plus any filter beyond them.

    This is the honest answer to "how fresh is the vendor's index?", a question the
    envelope could not answer at all before. It is a pure observation over returned
    records -- a LOWER BOUND on index freshness and never an upper one: the newest
    date among ten results for one query says what the index definitely reaches, and
    says nothing about what it does not.

    Why it earns its place: a researcher who gets no hits for recent work cannot
    otherwise tell "the vendor has not indexed this yet" from "it does not exist",
    and that is the false-negative class this skill exists to prevent. Measured live
    2026-08-03, the newest ``acquired`` across a fresh query was 2026-07-30 -- four
    days behind, not the eight months a field report had inferred from ONE stale
    cached page. Which is the second reason to measure it: the number replaces
    guesswork in both directions.

    ``requested_after_beyond_observed`` names each ``*_after`` bound that is LATER
    than the newest comparable date seen here. That is a real warning -- such a
    filter can only match records this response gives no evidence exist -- but it is
    still framed as an observation, because ten records are not the index.
    """
    newest: Dict[str, Optional[str]] = {}
    counts: Dict[str, int] = {}
    for field in ("acquired", "published"):
        seen = [date for date in (_iso_date(record.get(field)) for record in records) if date]
        newest[field] = max(seen).isoformat() if seen else None
        counts[field] = len(seen)
    beyond: List[str] = []
    for name in _AFTER_FILTERS:
        if name not in filters_requested:
            continue
        bound = _iso_date(filters_requested.get(name))
        reference = _iso_date(newest.get(_OBSERVABLE_FILTERS[name]))
        if bound is not None and reference is not None and bound > reference:
            beyond.append(name)
    return {
        "newest_acquired_observed": newest["acquired"],
        "newest_published_observed": newest["published"],
        "records_with_acquired": counts["acquired"],
        "records_with_published": counts["published"],
        "requested_after_beyond_observed": beyond,
    }


#: The scalar CONTRACT for every vendor argument: what type it must be, and for a
#: number, its inclusive range. One row per argument in ``_SEARCH_ARG_KEYS`` and
#: ``_FETCH_ARG_KEYS``; an argument absent from this table is not forwarded at all.
#:
#: Why a table and not ad-hoc checks: the JSON schema guards ONLY the agent-tool
#: path. The widget route (``_route_search``/``_route_fetch``) hands this client
#: whatever ``request.json()`` decoded, and a direct import reaches it unvalidated
#: too, so type enforcement cannot live in the schema -- exactly the reasoning that
#: already put ``max_chars`` normalization in ``resolve_max_chars``. This is that
#: same seam widened to the arguments it did not cover.
#:
#: The defect being closed: a non-string value used to pass through verbatim and
#: then be copied verbatim into ``filters_requested``. Strings were never the hole
#: -- every echoed string is bounded by ``clip``/``_bound_echo`` -- but a dict or
#: list has no length bound and no clip path, and ``fit_envelope`` can only give
#: back ``raw`` and ``results``. So a route posting a large object as
#: ``snippet_max_length`` could push the response past both
#: ``ENVELOPE_CHAR_BUDGET`` and the host's tool-result cap, and it would also be
#: sent to the vendor. Rejecting a wrong TYPE outright removes the class; bounding
#: it would instead invent a meaning for an argument the caller never validly sent.
#:
#: "text" carries no length limit here on purpose: ``query``/``prompt``/``url`` have
#: their own dedicated, better-worded checks below, and every text echo is already
#: bounded with disclosure. Adding a second ceiling would mean two rejection
#: stories for one field.
_ARG_TEXT = "text"
_ARG_COUNT = "count"
_ARG_FLAG = "flag"
_ARG_NUMERIC = "numeric"
#: Range for ``snippet_max_length``. The floor matches what ``_SEARCH_SCHEMA`` in
#: ``plugin.py`` advertises to the agent: two different floors would mean the tool
#: path and the widget route disagreed about the same argument, which is one
#: contract described two ways -- the defect class this table exists to remove.
VENDOR_SNIPPET_MIN_LENGTH = 180
VENDOR_SNIPPET_MAX_LENGTH = 10000
_ARG_SPECS: Dict[str, Tuple[str, Optional[int], Optional[int]]] = {
    "query": (_ARG_TEXT, None, None),
    "site": (_ARG_TEXT, None, None),
    "acquired_after": (_ARG_TEXT, None, None),
    "acquired_before": (_ARG_TEXT, None, None),
    "published_after": (_ARG_TEXT, None, None),
    "published_before": (_ARG_TEXT, None, None),
    "mode": (_ARG_TEXT, None, None),
    "url": (_ARG_TEXT, None, None),
    "prompt": (_ARG_TEXT, None, None),
    "snippet_max_length": (_ARG_COUNT, VENDOR_SNIPPET_MIN_LENGTH, VENDOR_SNIPPET_MAX_LENGTH),
    "live": (_ARG_FLAG, None, None),
    # LOCAL option, never forwarded to the vendor -- see `_LOCAL_ARG_KEYS`. It is in
    # this table anyway because the contract is about what the CALLER may send, not
    # about which arguments happen to travel onward: read as `bool(value)` it made
    # the string "false" enable raw output, since every non-empty string is truthy.
    "include_raw": (_ARG_FLAG, None, None),
    # Deliberately only type-gated here: ``resolve_max_chars`` owns its semantics
    # (omitted / over-ceiling / zero / negative / bool / float / numeric string)
    # and discloses the outcome. This row exists so a dict or list cannot reach it.
    "max_chars": (_ARG_NUMERIC, None, None),
}


def _vendor_arguments(
    source: Dict[str, Any], allowed: Tuple[str, ...]
) -> Tuple[Dict[str, Any], Optional["KeenableError"]]:
    """Forward only allowed arguments, each proven to match its scalar contract.

    Returns ``(arguments, error)``. A wrong type is a typed
    ``keenable_bad_request`` with ``error_class: local_rejection`` -- refused before
    any network leg and before any echo, so an invalid value is never sent to the
    vendor and never lands in the response either.

    The rejection message names the argument and the type that arrived, and
    NEVER the value: the value is the unbounded thing being rejected, so quoting
    it back would reintroduce the overflow inside the error that reports it.
    """
    out: Dict[str, Any] = {}
    for key in allowed:
        if key not in source:
            continue
        value = source[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        kind, low, high = _ARG_SPECS.get(key, (_ARG_TEXT, None, None))
        got = type(value).__name__
        if kind == _ARG_TEXT:
            if not isinstance(value, str):
                return out, KeenableError(
                    "keenable_bad_request", f"{key} must be a string, got {got}"
                )
            out[key] = value.strip()
        elif kind == _ARG_FLAG:
            if not isinstance(value, bool):
                return out, KeenableError(
                    "keenable_bad_request", f"{key} must be true or false, got {got}"
                )
            out[key] = value
        elif kind == _ARG_COUNT:
            # ``bool`` before ``int``: bool subclasses int in Python, so
            # ``snippet_max_length=True`` would otherwise become a 1-character
            # snippet -- the same trap ``resolve_max_chars`` documents.
            if isinstance(value, bool) or not isinstance(value, int):
                return out, KeenableError(
                    "keenable_bad_request", f"{key} must be a whole number, got {got}"
                )
            if (low is not None and value < low) or (high is not None and value > high):
                return out, KeenableError(
                    "keenable_bad_request",
                    f"{key} must be between {low} and {high}, got {value}",
                )
            out[key] = value
        else:  # _ARG_NUMERIC -- scalar gate only; semantics live downstream.
            if isinstance(value, (dict, list, tuple, set)):
                return out, KeenableError(
                    "keenable_bad_request", f"{key} must be a number, got {got}"
                )
            out[key] = value
    return out, None


def _bound_field(record: Dict[str, Any], field: str, limit: int) -> int:
    """Clip one field to ``limit`` and return the characters actually retained."""
    value, truncated, total = clip(str(record.get(field) or ""), max(0, limit))
    record[field] = value
    if truncated:
        record[f"{field}_truncated"] = True
        record[f"{field}_chars_total"] = total
    return len(value)


def _bound_records(records: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Bound record COUNT and record SIZE, disclosing every cut.

    Returns (kept, omitted). Each clipped field carries ``<field>_truncated`` and
    ``<field>_chars_total`` so no reduction is silent.

    ``RESULTS_TEXT_BUDGET`` is enforced before EVERY field write, not once per
    record: checking it only at record start let a record that began just under
    the ceiling add four more fields plus eight extra pairs on top of it, so the
    aggregate was an approximation rather than a contract.

    Extra keys are never truncated. A key longer than ``FIELD_CHAR_LIMIT`` is
    DROPPED and counted, because clipping keys made two long keys sharing a
    prefix collide and silently overwrite each other in this dict -- data loss
    beyond truncation. Dropping removes that class instead of reporting it.
    """
    kept: List[Dict[str, Any]] = []
    used = 0
    for record in records:
        if len(kept) >= MAX_RESULTS or RESULTS_TEXT_BUDGET - used < MIN_VIABLE_RECORD_CHARS:
            break
        bounded = dict(record)
        # `url` is bounded FIRST, before the title can eat the remaining budget.
        # A clipped url is the one field whose truncation is invisible downstream:
        # the presentation layer would linkify a `https://...` prefix that looks
        # like a working link and is not, which is the broken-link class
        # MIN_VIABLE_RECORD_CHARS exists to prevent. Reserving the url ahead of the
        # decorative title makes that outcome rare; the presenter additionally
        # refuses to linkify a url flagged `url_truncated`, so the two guards are
        # independent.
        for field, field_limit in (
            ("url", FIELD_CHAR_LIMIT),
            ("title", FIELD_CHAR_LIMIT),
            ("published", FIELD_CHAR_LIMIT),
            ("acquired", FIELD_CHAR_LIMIT),
            ("snippet", SNIPPET_CHAR_LIMIT),
        ):
            used += _bound_field(bounded, field, min(field_limit, RESULTS_TEXT_BUDGET - used))
        extra = bounded.get("extra")
        if isinstance(extra, dict) and extra:
            bounded_extra: Dict[str, str] = {}
            clipped_values: List[str] = []
            keys_dropped = 0
            budget_dropped = 0
            for raw_key, raw_value in list(extra.items())[:MAX_EXTRA_FIELDS]:
                key_text = str(raw_key)
                budget_left = RESULTS_TEXT_BUDGET - used
                if len(key_text) > FIELD_CHAR_LIMIT:
                    keys_dropped += 1
                    continue
                if len(key_text) >= budget_left:
                    budget_dropped += 1
                    continue
                value_text, value_clipped, value_total = clip(
                    str(raw_value), min(FIELD_CHAR_LIMIT, budget_left - len(key_text))
                )
                bounded_extra[key_text] = value_text
                used += len(key_text) + len(value_text)
                if value_clipped:
                    clipped_values.append(f"{key_text}:{value_total}")
            bounded["extra"] = bounded_extra
            if clipped_values:
                bounded["extra_truncated"] = clipped_values
            if keys_dropped:
                bounded["extra_keys_dropped"] = keys_dropped
            if budget_dropped:
                bounded["extra_budget_dropped"] = budget_dropped
            if len(extra) > MAX_EXTRA_FIELDS:
                bounded["extra_omitted"] = len(extra) - MAX_EXTRA_FIELDS
        kept.append(bounded)
    return kept, max(0, len(records) - len(kept))


def search(arguments: Dict[str, Any], key: str = "", transport: Optional[Transport] = None) -> Dict[str, Any]:
    """Normalized search envelope. Never raises for a vendor/transport failure."""
    auth = "api_key" if key else "keyless"
    # Normalized ONCE, here, rather than with `arguments or {}` at each use: the
    # later `arguments.get("include_raw")` read the caller's object directly, so
    # `search(None)` raised AttributeError from a line that looked incidental.
    arguments = arguments if isinstance(arguments, dict) else {}
    vendor_args, arg_error = _vendor_arguments(arguments, _SEARCH_ARG_KEYS)
    if arg_error is not None:
        return arg_error.to_dict("search_web_pages", auth)
    # Local options go through the SAME contract. Validated here rather than read
    # ad-hoc at the point of use, because `bool(arguments.get("include_raw"))` made
    # every non-empty string truthy -- so `include_raw: "false"` from the widget
    # route, which the JSON tool schema never sees, switched raw output ON.
    local_args, local_error = _vendor_arguments(arguments, _LOCAL_ARG_KEYS)
    if local_error is not None:
        return local_error.to_dict("search_web_pages", auth)
    include_raw = bool(local_args.get("include_raw", False))
    if not str(vendor_args.get("query") or "").strip():
        return KeenableError("keenable_bad_request", "query is required").to_dict("search_web_pages", auth)
    vendor_args.setdefault("snippet_max_length", DEFAULT_SNIPPET_MAX_LENGTH)
    try:
        text = call_tool("search_web_pages", vendor_args, key, transport)
    except KeenableError as exc:
        return exc.to_dict("search_web_pages", auth)

    records, accounting = parse_search_text(text)
    kept, omitted = _bound_records(records)
    # `filters_requested`, not `filters`: these are the arguments WE sent. The
    # vendor's enforcement of them is a separate, observed question below.
    # Bounded because these are ECHOES of caller input, which has no length limit of
    # its own; an unbounded echo makes every other size bound decorative. The
    # ORIGINAL length of anything shortened is reported in `filters_truncated`: a
    # first attempt here used `clip(v, ...)[0]` and threw the disclosure away, which
    # presented a shortened filter as the complete requested one -- the same silent
    # reduction this envelope exists to prevent, inside the code that prevents it.
    # Kept in a sibling map rather than as `<key>_truncated` inside
    # `filters_requested`, so a vendor filter can never collide with our metadata.
    filters_requested: Dict[str, Any] = {}
    filters_truncated: Dict[str, int] = {}
    for name, value in vendor_args.items():
        if name == "query":
            continue
        if isinstance(value, str):
            bounded, was_clipped, total = clip(value, FIELD_CHAR_LIMIT)
            filters_requested[name] = bounded
            if was_clipped:
                filters_truncated[name] = total
        else:
            # Bounded BY CONSTRUCTION, not by luck: `_ARG_SPECS` admits only a
            # range-checked int or a bool on this branch, so the echo is at most a
            # few characters. Before that contract existed this line copied an
            # arbitrary caller object -- a dict or list with no length bound and no
            # clip path -- into the response, and `fit_envelope` can only give back
            # `raw` and `results`, so the envelope could exceed both its own budget
            # and the host's cap through the one field nothing bounded.
            filters_requested[name] = value
    payload: Dict[str, Any] = {
        "ok": True,
        "tool": "search_web_pages",
        "auth": auth,
        "query": vendor_args.get("query", ""),
        "filters_requested": filters_requested,
        "filter_observations": observe_date_filters(filters_requested, kept),
        "filters_truncated": filters_truncated,
        "index_freshness": observe_index_freshness(kept, filters_requested),
        "results": kept,
        "count": len(kept),
        "results_omitted": omitted,
        "results_char_budget": RESULTS_TEXT_BUDGET,
        "snippet_char_limit": SNIPPET_CHAR_LIMIT,
        "untrusted_external_data": True,
        "vendor_limits": VENDOR_LIMITS,
    }
    payload.update(accounting)
    _bound_echo(payload, "query")
    # Raw vendor text accompanies anything short of a clean full parse, so
    # discarded blocks stay inspectable instead of vanishing behind one good
    # record.
    if include_raw or accounting["parse_status"] != "records":
        # Raw text takes whatever the SERIALIZED envelope still has room for, rather
        # than a fixed slice on top of everything else. Measured against the actual
        # payload, so it accounts for keys, syntax and every disclosure field.
        used = len(json.dumps(payload, ensure_ascii=False))
        allowance = max(0, ENVELOPE_CHAR_BUDGET - used - RAW_KEYS_OVERHEAD)
        effective = min(RAW_TEXT_LIMIT, allowance)
        raw, truncated, total = clip(text, effective)
        payload.update({
            "raw": raw,
            "raw_truncated": truncated,
            "raw_chars_total": total,
            # The limit that ACTUALLY applied, not the constant. Reporting the
            # constant while a smaller bound did the cutting is the same
            # misdescription this envelope exists to avoid.
            "raw_char_limit": effective,
            "envelope_char_budget": ENVELOPE_CHAR_BUDGET,
        })
    return fit_envelope(payload, "raw")


def fetch(
    arguments: Dict[str, Any],
    key: str = "",
    transport: Optional[Transport] = None,
    content_char_limit: int = CONTENT_CHAR_LIMIT,
) -> Dict[str, Any]:
    """Normalized page-fetch envelope. Never raises for a vendor/transport failure.

    ``content_char_limit`` is supplied by the CALLER rather than read from the
    module constant, so no transport assumption is hidden inside the client: the
    tool path and the widget route each pass the cap they actually live under.

    The resolved cap is sent to the vendor, which stops us paying for text we
    would discard: a live probe requested 30000 chars, the vendor delivered
    30159, and 18159 of them were thrown away by our own clip.
    """
    auth = "api_key" if key else "keyless"
    arguments = arguments if isinstance(arguments, dict) else {}
    vendor_args, arg_error = _vendor_arguments(arguments, _FETCH_ARG_KEYS)
    if arg_error is not None:
        return arg_error.to_dict("fetch_page_content", auth)
    if not str(vendor_args.get("url") or "").strip():
        return KeenableError("keenable_bad_request", "url is required").to_dict("fetch_page_content", auth)
    prompt = str(vendor_args.get("prompt") or "")
    if len(prompt) > PROMPT_MAX_LENGTH:
        return KeenableError(
            "keenable_bad_request",
            f"prompt is {len(prompt)} characters; the vendor limit is {PROMPT_MAX_LENGTH}",
        ).to_dict("fetch_page_content", auth)

    cap = resolve_max_chars(vendor_args.get("max_chars"), content_char_limit)
    vendor_args["max_chars"] = cap["max_chars_effective"]
    try:
        text = call_tool("fetch_page_content", vendor_args, key, transport)
    except KeenableError as exc:
        return exc.to_dict("fetch_page_content", auth)

    content, truncated_by_skill, total = clip(text, content_char_limit)
    extraction_mode = "prompt_extraction" if prompt else "full_page"
    # Read from the UNCLIPPED vendor text: the header lines sit at the very start,
    # so clipping cannot remove them, but reading the original keeps that
    # independent of our own bound.
    served = observed_service(text)
    measured = measure_content(content, cap["max_chars_effective"])
    measured["served_url_matches_request"] = served_url_matches_request(
        served.get("served_url"), vendor_args.get("url")
    )
    payload: Dict[str, Any] = {
        "ok": True,
        "tool": "fetch_page_content",
        "auth": auth,
        "url": vendor_args.get("url", ""),
        "extraction_mode": extraction_mode,
        "extraction_prompt": vendor_args.get("prompt", ""),
        # What the VENDOR says it served, verbatim, beside what we asked for. The
        # comparison lives in measured.served_url_matches_request; both are kept so
        # the caller can see the two urls rather than trust our verdict about them.
        "served": served,
        # The vendor supplies no snapshot date, so a cached answer's age is
        # genuinely unknown and is reported as such rather than omitted.
        "cache": {"live": bool(vendor_args.get("live", False)), "snapshot_date": None},
        "content": content,
        "content_truncated_by_skill": truncated_by_skill,
        "content_chars_total": total,
        "skill_content_char_limit": content_char_limit,
        "measured": measured,
        "content_incompleteness_indicators": incompleteness_indicators(measured, extraction_mode),
        "vendor_content_complete": None,
        "untrusted_external_data": True,
        "vendor_limits": VENDOR_LIMITS,
    }
    payload.update(cap)
    # Bounded AFTER the redirect comparison, which ran on the full values above, so a
    # size bound can never change the verdict it reports.
    _bound_echo(payload, "url")
    served_block = payload.get("served")
    if isinstance(served_block, dict):
        _bound_echo(served_block, "served_url")
        _bound_echo(served_block, "served_title")
    return fit_envelope(payload, "content")
