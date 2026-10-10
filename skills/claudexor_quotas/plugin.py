"""Claudexor Quotas — quota/limit projection for authorized accounts.

Cached reads request the host's passive quota view. Older hosts may answer
with their full status projection; the response marker distinguishes them
without another GET. The owner's explicit
Refresh action uses the dedicated host quota-refresh endpoint. No daemon token
is touched and quota policy remains in Claudexor.

The skill keeps its data in its own state directory: the reader's
display choices (the widget cannot keep them itself: it runs in an
opaque-origin sandbox where every browser store throws, so a preference kept
there is silently forgotten), and a bounded history of passive quota readings
kept by one server-owned supervised collector (see quota_history.py). The
collector also keeps an empty OS lease file to serialize reloads. It only ever issues the same passive status GET the widget polls with;
it never asks a provider for a fresh reading.

Every projection below preserves provenance: a facet that was not read or that
failed is reported as such, never as an empty or zero value. The reserve
overview, the chart and the model tool are one calculation (quota_summary.py)
over the same status projection and the same history view.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import errno
import itertools
import json
import math
import os
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:  # the host loads plugin.py as a package: siblings are relative imports
    from . import quota_history
    from . import quota_summary as qs
except ImportError:  # imported directly from the skill directory (tests)
    import quota_history  # type: ignore[no-redef]
    import quota_summary as qs  # type: ignore[no-redef]

STATUS_TIMEOUT_SEC = 25.0
REFRESH_TIMEOUT_SEC = 180.0
STATUS_PATH = "/api/claudexor/status?view=quota"
# The status of a request that may have reached the host but whose answer was
# not read: a timeout, a connection closed after the request went out, an
# answer that broke off. For a POST its outcome is unknown. 0 is a request
# that never reached a host (the connection was refused, the name did not
# resolve).
NO_ANSWER_STATUS = -1
REFRESH_PATH = "/api/claudexor/quota/refresh"

# The collector requests the passive quota view, without the host's outer
# subsystem diagnostics. The engine's account read may still run cold CLI
# discovery. An older host may ignore that query and answer its
# full status; no second request is sent to guess which. A source superseded within
# two minutes may go unseen; that costs the pace a point, never adds one, since
# only the readings actually seen are kept. A read the widget route made
# moments ago is recorded instead of issuing another.
COLLECT_INTERVAL_SEC = qs.COLLECT_INTERVAL_SEC
COLLECT_FIRST_DELAY_SEC = 20.0
# The model tool has no host-side timeout for a synchronous handler, so its
# one status read is bounded here, shorter than the widget's.
TOOL_STATUS_TIMEOUT_SEC = 20.0
TOOL_MAX_GROUPS = 24
# A chart or group switch in the widget may reuse a status read this recent
# rather than asking the daemon again; the widget's timed poll never does.
REUSE_MAX_AGE_SEC = 45.0
HORIZON_CHOICES = tuple(qs.HORIZONS)
# One glyph: the host renders no named icons ("gauge" was shown as its own
# generic widget mark).
UI_ICON = "\u25d4"

# Display choices, and only those. Anything the reader picks that is not in
# these tables is not stored: the file is written by a route, and a route takes
# whatever it is given. Legacy since 0.8.0: the widget no longer reads or saves
# them (its timeline's span and scenario are choices for one visit); the file,
# the route and its save ordering stay for an older widget.
PREFS_FILE = "prefs.json"
DENSITIES = ("compact", "normal", "detailed")
MODEL_VIEWS = ("all", "models", "shared")
# Why an account can be folded out of the list, in the order its sections are
# shown. The reader answers each of them separately. An account can match more
# than one (a failed check is often reported with no login); the widget then
# files it by its own order of checks, no login first, not by this one.
FOLD_REASONS = ("failed", "disabled", "signed_out")
DEFAULT_PREFS: Dict[str, Any] = {
    "density": "normal",
    "models": {},
    "fold": {reason: True for reason in FOLD_REASONS},
}
# A harness id is a short slug from the host's own catalog. The cap is there so
# a malformed call cannot grow the file without bound.
MAX_MODEL_ENTRIES = 32
MAX_HARNESS_ID = 64
# A save's order: the widget load that made it, and its number there. Neither
# is stored; the skill remembers the newest number of this many loads.
MAX_PREFS_FRAME = 64
MAX_PREFS_FRAMES = 16

FACETS = ("catalog", "accounts", "quota")
READ_OK = "ok"
READ_STATES = (READ_OK, "not_read", "failed")
# Which top-level keys of the host's status payload carry each facet: what
# is kept from the last read that answered it.
FACET_KEYS: Dict[str, Tuple[str, ...]] = {
    "catalog": ("harnesses",),
    "accounts": ("profiles", "unified_accounts"),
    "quota": ("quota", "quota_absences"),
}


def _server_port(api: Any) -> int:
    """Resolve the live gateway port from the runtime's own port file."""
    for candidate in _port_candidates(api):
        try:
            value = int(str(candidate).strip())
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 65535:
            return value
    return 8765


def _port_candidates(api: Any) -> List[Any]:
    out: List[Any] = []
    try:
        info = api.get_runtime_info() or {}
        for key in ("server_port", "port"):
            if info.get(key):
                out.append(info.get(key))
    except Exception:
        pass
    try:
        port_file = Path(api.get_state_dir()).resolve().parents[1] / "server_port"
        if port_file.is_file():
            out.append(port_file.read_text(encoding="utf-8"))
    except Exception:
        pass
    if os.environ.get("OUROBOROS_SERVER_PORT"):
        out.append(os.environ["OUROBOROS_SERVER_PORT"])
    return out


def _request_json(
    port: int,
    path: str,
    method: str = "GET",
    timeout_sec: float = STATUS_TIMEOUT_SEC,
) -> Tuple[Optional[Dict[str, Any]], str, int]:
    """Return one loopback JSON response without exposing host credentials,
    the error, and the HTTP status — 0 when the request never reached a host,
    NO_ANSWER_STATUS when it may have and no answer was read."""
    url = f"http://127.0.0.1:{port}{path}"
    body = b"{}" if method == "POST" else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout_sec) as response:
            response_body = response.read().decode("utf-8", "replace")
            code = int(getattr(response, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} from {path}", int(exc.code)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}", 0 if _never_sent(exc) else NO_ANSWER_STATUS
    if code != 200:
        return None, f"HTTP {code} from {path}", code
    try:
        payload = json.loads(response_body)
    except Exception:
        return None, "response was not JSON", code
    if not isinstance(payload, dict):
        return None, "response was not a JSON object", code
    return payload, "", code


def _never_sent(exc: BaseException) -> bool:
    """Whether a request certainly never reached a host: the connection was
    refused, or the address did not resolve. Anything else — a timeout, a
    reset, an answer cut short — may have reached it."""
    reason = getattr(exc, "reason", None) if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror))


def _fetch_status(
    port: int,
    timeout_sec: float = STATUS_TIMEOUT_SEC,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """One request for the quota view; never a provider-refresh action.

    A supporting core marks its envelope ``view: quota``. An older core may
    ignore the query and run its legacy full-status diagnostics; passive_read_info
    reports that compatibility path explicitly. Never probe a second URL.
    """
    payload, error, _status = _request_json(port, STATUS_PATH, timeout_sec=timeout_sec)
    return payload, error


def _refresh_quota(port: int) -> Tuple[Optional[Dict[str, Any]], str, int]:
    """Request exactly one foreground quota refresh through the host."""
    return _request_json(
        port,
        REFRESH_PATH,
        method="POST",
        timeout_sec=REFRESH_TIMEOUT_SEC,
    )


def facet_states(payload: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Per-facet provenance. An unusable reads block is indeterminate, not ok."""
    if not isinstance(payload, dict):
        return {facet: "indeterminate" for facet in FACETS}
    reads = payload.get("reads")
    if not isinstance(reads, dict):
        return {facet: "indeterminate" for facet in FACETS}
    out: Dict[str, str] = {}
    for facet in FACETS:
        raw = str(reads.get(facet) or "").strip()
        out[facet] = raw if raw in READ_STATES else "indeterminate"
    return out


def facet_note(states: Dict[str, str]) -> str:
    """Name exactly which facets did not answer; empty when all are ok."""
    bad = [f"{facet}: {state}" for facet, state in states.items() if state != READ_OK]
    return "; ".join(bad)


def passive_read_info(payload: Optional[Dict[str, Any]], error: str = "") -> Dict[str, Any]:
    """Small response provenance, separate from quota source freshness.

    The marker is required to claim the dedicated view. Safe phase codes
    and numeric durations are useful diagnostics; arbitrary error bodies,
    paths and additional response metadata are not forwarded.
    """
    mode = ("unavailable" if error or not isinstance(payload, dict) else
            "quota" if payload.get("view") == "quota" else "legacy_status")
    out: Dict[str, Any] = {"mode": mode, "timings_ms": {}, "read_errors": {}}
    if mode != "quota":
        return out
    raw_timings = payload.get("timings_ms")
    timings = raw_timings if isinstance(raw_timings, dict) else {}
    for phase in ("discovery", "accounts", "quota", "total"):
        value = timings.get(phase)
        if not isinstance(value, bool) and isinstance(value, (int, float)) \
                and 0 <= value <= 86400000 and math.isfinite(float(value)):
            out["timings_ms"][phase] = value
    raw_errors = payload.get("read_errors")
    errors = raw_errors if isinstance(raw_errors, dict) else {}
    for phase in ("discovery", "accounts", "quota"):
        entry = errors.get(phase)
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        if not isinstance(code, str) or not 0 < len(code) <= 80 \
                or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for c in code):
            continue
        safe: Dict[str, Any] = {"code": code}
        status = entry.get("status_code")
        if not isinstance(status, bool) and isinstance(status, int) and 100 <= status <= 599:
            safe["status_code"] = status
        out["read_errors"][phase] = safe
    return out


def _subject_key(value: Any) -> str:
    """Native login is subject_id null/''; a profile is its exact profile id."""
    if value is None:
        return ""
    return str(value).strip()


def _latest_observed_at(rows: List[Dict[str, Any]]) -> str:
    values = [str(row.get("observed_at") or "") for row in rows]
    return max((value for value in values if value), default="")


def _retry_deadline(absence: Dict[str, Any]) -> str:
    """Translate a typed vendor Retry-After into one absolute deadline."""
    try:
        retry_ms = int(absence.get("retry_after_ms"))
    except (TypeError, ValueError):
        return ""
    if retry_ms < 0:
        return ""
    raw = str(absence.get("observed_at") or "")
    try:
        observed = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return ""
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=_dt.timezone.utc)
    return (observed + _dt.timedelta(milliseconds=retry_ms)).isoformat()


def _later_deadline(first: str, second: str) -> str:
    """Return the later parseable ISO instant, preferring current evidence."""
    parsed: List[Tuple[_dt.datetime, str]] = []
    for raw in (first, second):
        try:
            instant = _dt.datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=_dt.timezone.utc)
        parsed.append((instant, str(raw)))
    fallback = (_dt.datetime.min.replace(tzinfo=_dt.timezone.utc), second)
    return max(parsed, default=fallback)[1]


def _absence_view(
    absences: Any,
    harness_id: str,
    subject_id: str,
    refresh_skipped: Any = None,
) -> Optional[Dict[str, str]]:
    """Map typed absence facts to the approved generic action vocabulary."""
    matching = [
        row for row in (absences if isinstance(absences, list) else [])
        if isinstance(row, dict)
        and str((row.get("subject") or {}).get("harness") or "") == harness_id
        and _subject_key((row.get("subject") or {}).get("subject_id")) == subject_id
    ]
    matching.sort(key=lambda row: str(row.get("observed_at") or ""), reverse=True)
    row = matching[0] if matching else None
    action_kind = ""
    retry_at = ""
    observed_at = str((row or {}).get("observed_at") or "")
    reason = str((row or {}).get("reason") or "")
    if reason in {"not_logged_in", "auth_revoked"}:
        action_kind = "sign_in_if_unverified"
    elif reason == "no_source":
        action_kind = "source_missing"
    elif reason == "rate_limited":
        retry_at = _retry_deadline(row or {})
        action_kind = "retry" if retry_at else ""

    skipped = next((
        item for item in (refresh_skipped if isinstance(refresh_skipped, list) else [])
        if isinstance(item, dict)
        and str(item.get("vendor") or "") == harness_id
        and str(item.get("not_before") or "")
    ), None)
    if skipped is not None:
        skipped_at = str(skipped.get("not_before") or "")
        if action_kind == "retry":
            retry_at = _later_deadline(retry_at, skipped_at)
        elif not action_kind:
            retry_at = skipped_at
            action_kind = "retry"
    if row is None and skipped is None:
        return None
    return {
        "message": "Quota temporarily unavailable",
        "action_kind": action_kind,
        "retry_at": retry_at,
        "observed_at": observed_at,
    }


def _is_future(iso_text: Any) -> Optional[bool]:
    """True/False for a parseable instant, None when it cannot be parsed."""
    raw = str(iso_text or "").strip()
    if not raw:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed > _dt.datetime.now(_dt.timezone.utc)


def _used_text(ratio: Optional[float]) -> Optional[str]:
    """The share used, as the widget prints it before its "%": a whole
    percent as the provider reported it, a finer reading to one decimal —
    and never "100" for a share that is not full, nor "0" for one that has
    been used at all ("<100", ">0"). Rounding is for the eye only; full is
    decided on the ratio itself, by the reserve's own tolerance."""
    if ratio is None:
        return None
    full = 1.0 - ratio <= qs.RATIO_EPS
    pct = ratio * 100.0
    whole = round(pct)
    if abs(pct - whole) < 1e-6 and (whole < 100 or full) and (whole > 0 or ratio <= 0.0):
        return str(int(whole))
    text = f"{pct:.1f}".rstrip("0").rstrip(".")
    if not full and float(text) >= 100.0:
        return "<100"
    if ratio > 0.0 and float(text) <= 0.0:
        return ">0"
    return text


def _constraint_view(constraint: Dict[str, Any], at_limit: bool = False,
                     scope: Optional[str] = None) -> Dict[str, Any]:
    """One window as the account view draws it. The ratio is read the way
    the reserve reads it (quota_summary.ratio_of): a value outside [0, 1], a
    string or NaN is refused and named in ``ratio_problem``, never clamped
    into a share nobody reported. ``used_pct`` is the unrounded percent (for
    bars and tones), ``used_text`` the words for it. ``at_limit`` is the
    caller's verdict on a current reading; a last-known or not-current
    window never carries one. ``scope_key`` is the reserve's identity of the
    window's own model list — taken from the constraint itself when the
    caller has no reading of it (a bare cooldown, a credit counter)."""
    ratio, problem = qs.ratio_of(constraint.get("used_ratio"))
    models = constraint.get("applies_to_models")
    scoped = [str(m) for m in models if m] if isinstance(models, list) else []
    if scope is None:
        scope = qs._scope_hash(qs._models_of(models))
    return {
        "id": str(constraint.get("id") or ""),
        "label": str(constraint.get("label") or constraint.get("id") or "constraint"),
        "used_pct": None if ratio is None else round(ratio * 100.0, 6),
        "used_text": _used_text(ratio),
        "at_limit": bool(at_limit and ratio is not None),
        "ratio_problem": "" if ratio is not None or problem == "missing" else problem,
        "resets_at": str(constraint.get("resets_at") or ""),
        "cooldown_until": str(constraint.get("cooldown_until") or ""),
        "scoped_models": scoped,
        # Display names can be shortened; the reserve's identity cannot.
        "scope_key": scope,
        "window_seconds": constraint.get("window_seconds"),
    }


def _spent(view: Dict[str, Any]) -> bool:
    """A current window at its limit is spent — the unrounded verdict the
    view carries: a window at 99.6% is not spent, however its percent is
    printed. A cooldown is not a spent share: it is carried as a cooldown
    of its own (``_cooldown_view``), never as "Limit reached"."""
    return view.get("at_limit") is True


def _cooldown_view(cooldown: qs.Cooldown, now: float) -> Dict[str, Any]:
    """One cooldown as the account view carries it, apart from every
    window's share and reset: what it holds (the whole account, or some
    models), until when — or why that is not known — and which reading
    reported it."""
    if cooldown.until is not None:
        until, note = qs.iso(cooldown.until) or "", "" if cooldown.until > now else "passed"
    else:
        until, note = "", "unreadable" if cooldown.until_text else "not_reported"
    return {
        "scope": "models" if cooldown.models else "account",
        "scope_key": cooldown.scope,
        "models": list(cooldown.models),
        "models_omitted": cooldown.models_omitted,
        "label": cooldown.label,
        "until": "" if note else until,
        "until_note": note,
        "kind": cooldown.kind,
        "source": cooldown.source,
        "freshness": "fresh" if cooldown.fresh else "stale",
        "observed_at": qs.iso(cooldown.observed_at) or "",
    }


def _cooldowns(rows: List[Dict[str, Any]], harness_id: str, subject_id: str,
               now: float, projection: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every cooldown the account's readings report that holds now, by the
    reserve's own rule (quota_summary.cooldowns_of): from whichever source,
    fresh or stale, whatever number is drawn. One cooldown reported twice
    (by the row's state and its constraint, or by two sources) is one fact,
    kept from its freshest, newest report."""
    found = [cooldown for row in rows
             for cooldown in qs.cooldowns_of(row, harness_id, subject_id, now, projection)]
    found.sort(key=lambda c: (not c.fresh, -(c.observed_at or -math.inf), c.kind != "constraint"))
    out: List[Dict[str, Any]] = []
    seen = set()
    for cooldown in found:
        key = (cooldown.scope, cooldown.until if cooldown.until is not None else cooldown.until_text)
        if key not in seen:
            seen.add(key)
            out.append(_cooldown_view(cooldown, now))
    return out


def _exhaustion_view(exhaustion: qs.Exhaustion) -> Dict[str, Any]:
    """One model-scoped exhaustion as the account view carries it: which
    limit of which models the engine reports out, until when — or that its
    reset was not reported, cannot be read, or has passed — and which reading
    reported it. ``live`` says whether it holds those models now; nothing
    else about it is a share, a forecast or a cooldown."""
    return {
        "constraint_id": exhaustion.constraint_id,
        "scope_key": exhaustion.scope,
        "models": list(exhaustion.models),
        "models_omitted": exhaustion.models_omitted,
        "live": exhaustion.live,
        # The reported reset, read; "" when none was reported or it cannot
        # be read (``reset_note`` says which). A passed one stays as the fact.
        "resets_at": qs.iso(exhaustion.resets_at) or "",
        "reset_note": exhaustion.note,
        "source": exhaustion.source,
        "freshness": "fresh" if exhaustion.fresh else "stale",
        "observed_at": qs.iso(exhaustion.observed_at) or "",
    }


def _exhaustions(rows: List[Dict[str, Any]], harness_id: str, subject_id: str,
                 now: float) -> List[Dict[str, Any]]:
    """Every model-scoped exhaustion the account's readings report, by the
    reserve's own rule (quota_summary.exhaustions_of), fresh or stale. One
    reported twice (by two sources) is one fact, kept from its freshest,
    newest report; live ones come first."""
    found = [e for row in rows for e in qs.exhaustions_of(row, harness_id, subject_id, now)]
    found.sort(key=lambda e: (not e.live, not e.fresh, -(e.observed_at or -math.inf)))
    out: List[Dict[str, Any]] = []
    seen = set()
    for exhaustion in found:
        key = (exhaustion.scope, exhaustion.constraint_id,
               exhaustion.resets_at if exhaustion.resets_at is not None else exhaustion.resets_text)
        if key not in seen:
            seen.add(key)
            out.append(_exhaustion_view(exhaustion))
    return out


# Why a fresh reading of a limit is not drawn as current — the reserve's own
# reasons (quota_summary.resolve_member), in the account view's words.
_RATIO_WORDS = {"out_of_range": "ratio outside 0–100%", "not_a_number": "ratio not a number",
                "not_finite": "ratio not a number", "missing": "no ratio"}


def _not_current_why(reading: qs.Reading, now: float) -> str:
    if reading.observed_at is None:
        return "no observation time"
    if reading.observed_at > now + qs.FUTURE_SKEW_SEC:
        return "observed in the future"
    if reading.ratio is None:
        return _RATIO_WORDS.get(reading.ratio_problem, "ratio unreadable")
    if reading.resets_at is not None and reading.resets_at <= now:
        return "its reported reset has passed"
    return "its sources disagree"


def quota_for(
    snapshots: Any,
    harness_id: str,
    subject_id: str,
    quota_read: str,
    absences: Any = None,
    refresh_skipped: Any = None,
    now: Optional[float] = None,
    *,
    attributed: Optional[qs.Attributed] = None,
) -> Dict[str, Any]:
    """Project quota for ONE account. Absence is stated, never invented.

    The account's readings are the ones the reserve gives it
    (``attributed``, from quota_summary.attribute — the unified engine's
    default alias included). Without an account list (a Refresh envelope
    carries none) they are matched by exact subject.

    A limit's current window is the reading the reserve overview counts:
    every fresh source of it is read and resolved by quota_summary's own
    rules (reading_of, resolve_member), so a reading the overview refuses —
    unreadable, observed at no time or in the future, of a cycle whose reset
    has passed, or from sources that disagree — is never a current bar or a
    spent verdict here. It stays in view as a reading "not current", with
    the reason, beside the stale ones.

    Cooldowns are read apart from the numbers, by the reserve's own rule
    (quota_summary.cooldowns_of), from every reading of the account — so a
    cooldown reported by a source whose number is not the one drawn, or by
    a stale reading, is not lost. A cooldown is "Cooling down", never
    "Limit reached", and its end is not a reset.

    Model-scoped exhaustions the availability reports are read the same way
    (quota_summary.exhaustions_of) and carried as facts of their own
    (``model_exhaustions``): a model's limit, never the account's state.

    Each window is current or not by its own freshness
    (quota_summary.constraint_freshness, over the whole answer): one snapshot
    can give a current weekly window and a stale 5-hour one. A snapshot with
    no window speaks by its own word."""
    moment = time.time() if now is None else float(now)
    if quota_read != READ_OK:
        return {
            "state": "not_checked",
            "label": f"Limits not checked — quota facet {quota_read}",
            "resets_at": "",
            "note": "",
            "constraints": [],
            "reset_credits": qs.reset_credits_of([], harness_id, moment, quota_read),
            "stale": [],
            "cooldowns": [],
            "model_exhaustions": [],
            "cooling_until": "",
            "availability": "",
            "observed_at": "",
            "absence": None,
        }
    if attributed is not None:
        rows = attributed.rows_of(harness_id, subject_id)
    else:
        rows = [
            row for row in (snapshots if isinstance(snapshots, list) else [])
            if isinstance(row, dict)
            and str((row.get("subject") or {}).get("harness") or "") == harness_id
            and _subject_key((row.get("subject") or {}).get("subject_id")) == subject_id
        ]
    projection = attributed.freshness if attributed is not None else qs.freshness_projection(snapshots)
    reset_credits = qs.reset_credits_of(rows, harness_id, moment, projection=projection)
    cooldowns = _cooldowns(rows, harness_id, subject_id, moment, projection)
    exhaustions = _exhaustions(rows, harness_id, subject_id, moment)
    # Held as a whole: "Cooling down" (unless a window is at its limit),
    # whatever the numbers say and however fresh they are — the reserve
    # counts the same cooldowns as its "cooling" restriction.
    cooling = [c for c in cooldowns if c["scope"] == "account"]
    cooling_until = ""
    if cooling and all(c["until"] for c in cooling):
        # Unavailable until the last of them ends, if every end is known.
        cooling_until = max(cooling, key=lambda c: qs.parse_instant(c["until"]) or 0.0)["until"]
    # Each snapshot's windows by their own freshness: the current ones, and
    # the rest by their word, in the order they were reported.
    current: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    held: List[Tuple[Dict[str, Any], str, List[Dict[str, Any]]]] = []
    for row in rows:
        listed = row.get("constraints")
        windows = [c for c in (listed if isinstance(listed, list) else []) if isinstance(c, dict)]
        if not windows:
            word = str(row.get("freshness") or "")
            if word == "fresh":
                current.append((row, []))
            else:
                held.append((row, word or "unknown", []))
            continue
        words: Dict[str, List[Dict[str, Any]]] = {}
        for constraint in windows:
            words.setdefault(qs.constraint_freshness(row, constraint, projection), []).append(constraint)
        if "fresh" in words:
            current.append((row, words.pop("fresh")))
        held.extend((row, word, items) for word, items in words.items())
    # Fresh credits/cooldowns are facts of their own, not evidence of a
    # current quota window. Keep their projections without hiding stale usage.
    fresh = [row for row, windows in current if not held or not windows or any(
        qs.reading_of(c, harness_id, subject_id, source=str(row.get("source") or "unnamed"),
                      fresh=True, observed=qs.parse_instant(row.get("observed_at"))) is not None
        for c in windows)]
    other = [row for row, _word, _windows in held]
    absence = _absence_view(
        absences,
        harness_id,
        subject_id,
        refresh_skipped,
    )
    stale_views = [
        {
            "observed_at": str(row.get("observed_at") or ""),
            "freshness": word,
            "source": str(row.get("source") or ""),
            "constraints": [_constraint_view(c) for c in windows],
        }
        for row, word, windows in held
    ]

    if not fresh:
        if stale_views:
            return {
                "state": "cooling" if cooling else "no_fresh_window",
                "label": "Cooling down" if cooling else "No fresh reading — last reading is stale",
                "resets_at": "",
                "note": (
                    "Stale percentages do not grant routing; "
                    "live cooldown evidence may still deny or rank."
                ),
                "constraints": [],
                "reset_credits": reset_credits,
                "stale": stale_views,
                "cooldowns": cooldowns,
                "model_exhaustions": exhaustions,
                "cooling_until": cooling_until,
                "availability": "",
                "observed_at": _latest_observed_at(other),
                "absence": absence,
            }
        return {
            "state": "no_data",
            "label": "No quota window reported for this account",
            "resets_at": "",
            "note": "",
            "constraints": [],
            "reset_credits": reset_credits,
            "stale": [],
            "cooldowns": cooldowns,
            "model_exhaustions": exhaustions,
            "cooling_until": "",
            "availability": "",
            "observed_at": "",
            "absence": absence,
        }

    views: List[Dict[str, Any]] = []
    availability = ""
    # In the order the engine first reported each: a quota window (by its
    # limit, every source of it together), or a constraint that is not one
    # (a credit counter, a bare cooldown), shown as it is.
    order: List[Any] = []
    by_key: Dict[str, List[qs.Reading]] = {}
    origin: Dict[int, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
    for row, windows in current:
        # The availability word is the snapshot's own: only a fresh one speaks for now.
        if str(row.get("freshness") or "") == "fresh":
            availability = availability or str((row.get("availability") or {}).get("state") or "")
        observed = qs.parse_instant(row.get("observed_at"))
        source = str(row.get("source") or "").strip()[:64] or "unnamed"
        for constraint in windows:
            reading = qs.reading_of(constraint, harness_id, subject_id, source=source,
                                    fresh=True, observed=observed)
            if reading is None:
                # A manual credit counter is never a percentage or an
                # automatic refill, even if malformed window fields appear.
                shown = (dict(constraint, used_ratio=None, window_seconds=None, resets_at="")
                         if qs.is_reset_credit(constraint, harness_id) else constraint)
                order.append(_constraint_view(shown))
                continue
            if reading.key not in by_key:
                order.append(reading.key)
            by_key.setdefault(reading.key, []).append(reading)
            origin[id(reading)] = (row, constraint)

    set_aside: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for item in order:
        if isinstance(item, dict):
            views.append(item)
            continue
        readings = by_key[item]
        member = qs.resolve_member(readings, moment)
        if member.status == "measured":
            views.append(_constraint_view(origin[id(member.reading)][1],
                                          member.remaining <= qs.RATIO_EPS,
                                          member.reading.scope))
            continue
        if all(reading.ratio_problem == "missing" for reading in readings):
            # Reported with no share at all: it claims nothing, and stays in
            # view as the window it is, with no bar.
            newest = max(readings, key=lambda r: r.observed_at if r.observed_at is not None else -math.inf)
            views.append(_constraint_view(origin[id(newest)][1], scope=newest.scope))
            continue
        for reading in readings:
            row, constraint = origin[id(reading)]
            why = _not_current_why(reading, moment)
            entry = set_aside.setdefault((id(row), why), {
                "observed_at": str(row.get("observed_at") or ""),
                "freshness": "fresh",
                "source": str(row.get("source") or ""),
                "why": why,
                "constraints": [],
            })
            entry["constraints"].append(_constraint_view(constraint, scope=reading.scope))
    not_current = list(set_aside.values())

    spent_windows: List[Dict[str, Any]] = []
    worst: Optional[Dict[str, Any]] = None
    scoped_spent: List[str] = []
    # A share counts only from a current window (at_limit). A cooldown is not
    # a share: it is read apart (``cooldowns``), whichever window is drawn.
    for view in views:
        if view["scoped_models"]:
            if _spent(view):
                scoped_spent.append(view["label"])
            continue
        if _spent(view):
            spent_windows.append(view)
        if view["used_pct"] is None:
            continue
        if worst is None or view["used_pct"] > worst["used_pct"]:
            worst = view

    note = ", ".join(sorted(set(scoped_spent)))
    note = f"per-model caps spent: {note}" if note else ""
    if spent_windows:
        state, label = "exhausted", "Limit reached"
        # The spent window's own reported reset; never a cooldown's end.
        resets_at = next((v["resets_at"] for v in spent_windows if v["resets_at"]), "")
    elif cooling:
        state, label = "cooling", "Cooling down"
        resets_at = ""
    elif worst is not None:
        state, label = "ok", f"{worst['used_text']}% used"
        resets_at = worst["resets_at"]
    elif not_current:
        whys = sorted({entry["why"] for entry in not_current})
        state = "not_current"
        label = "No current reading — " + (whys[0] if len(whys) == 1 else "see the details")
        resets_at = ""
    else:
        state = "no_data"
        label = "Read, but no usage numbers reported"
        resets_at = ""
    return {
        "state": state,
        "label": label,
        "resets_at": str(resets_at or ""),
        "note": note,
        "constraints": views,
        "reset_credits": reset_credits,
        # Readings that are not current — fresh ones the reserve does not
        # count (each says why), then stale ones — as last-known facts.
        "stale": not_current + stale_views,
        # Every cooldown that holds now, each with its own facts; ``state``
        # is "cooling" when one holds the whole account and no window is at
        # its limit, and ``cooling_until`` is when the last whole-account one
        # ends (empty when there is none or any end is unknown).
        "cooldowns": cooldowns,
        # Every model-scoped exhaustion the availability reports, live or
        # only disclosed (``live``, ``reset_note``); never the account's state.
        "model_exhaustions": exhaustions,
        "cooling_until": cooling_until,
        # The engine's own availability word for the first fresh reading, as
        # reported; not re-derived from shares or cooldowns.
        "availability": availability,
        "observed_at": _latest_observed_at(fresh),
        "absence": absence,
    }


def verification_view(
    verification: str,
    source: str,
    accounts_read: str,
    signed_in: bool,
) -> Dict[str, str]:
    """Honest verification wording; degraded reads can never look green."""
    verification = str(verification or "").strip()
    source = str(source or "").strip()
    if verification == "passed" and source == "vendor":
        view = {"tone": "ok", "label": "Verified live"}
    elif verification == "passed":
        detail = source or "local session"
        view = {"tone": "muted", "label": f"Signed in — not verified live ({detail})"}
    elif verification == "failed":
        view = {"tone": "warn", "label": "Verification failed"}
    elif signed_in:
        view = {"tone": "muted", "label": "Signed in — not verified live (local session)"}
    else:
        view = {"tone": "muted", "label": "Not verified"}
    if accounts_read != READ_OK:
        view = {"tone": "muted", "label": f"{view['label']} — last known"}
    return view


def pool_routing(profiles_block: Dict[str, Any]) -> Dict[str, Any]:
    """Routing verdicts by harness. A unified engine empties `harnessAccounts`
    and carries them in the additive `accountPools` key instead; on a legacy
    engine this is simply empty and the old per-harness row keeps answering."""
    rows = profiles_block.get("accountPools")
    out: Dict[str, Any] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        hid = str(row.get("harness_id") or "")
        if hid:
            out[hid] = row.get("next_up") if isinstance(row.get("next_up"), dict) else {}
    return out


def is_next_up(verdict: Any, kind: str, subject_id: str) -> bool:
    """Whether this account is the one the harness would use next. Routing is
    never re-derived from the profile list: with no verdict on the wire the
    honest answer is "not stated", which is False here and disclosed as
    `routing_read` on the group."""
    if not isinstance(verdict, dict):
        return False
    if str(verdict.get("kind") or "") != kind:
        return False
    if kind == "native":
        return True
    # The pool spells it camelCase, the legacy row snake_case. Same fact.
    named = str(verdict.get("profileId") or verdict.get("profile_id") or "")
    return bool(named) and named == subject_id


def build_groups(payload: Dict[str, Any], states: Dict[str, str],
                 now: Optional[float] = None) -> List[Dict[str, Any]]:
    """One card per agent family; account rows inside it."""
    harnesses = payload.get("harnesses")
    harnesses = [h for h in harnesses if isinstance(h, dict)] if isinstance(harnesses, list) else []
    profiles_block = payload.get("profiles")
    profiles_block = profiles_block if isinstance(profiles_block, dict) else {}
    native_rows = [r for r in (profiles_block.get("harnessAccounts") or []) if isinstance(r, dict)]
    profile_rows = [r for r in (profiles_block.get("profiles") or []) if isinstance(r, dict)]
    snapshots = payload.get("quota")
    # Which account each reading belongs to, by the reserve's own rule.
    attributed = qs.attribute(payload)
    absences = payload.get("quota_absences")
    pools = pool_routing(profiles_block)
    accounts_read = states.get("accounts", "indeterminate")
    quota_read = states.get("quota", "indeterminate")

    order: List[str] = [str(h.get("id") or "") for h in harnesses if h.get("id")]
    for row in native_rows:
        hid = str(row.get("harness_id") or "")
        if hid and hid not in order:
            order.append(hid)
    for row in profile_rows:
        hid = str((row.get("profile") or {}).get("harness_id") or "")
        if hid and hid not in order:
            order.append(hid)

    groups: List[Dict[str, Any]] = []
    for hid in order:
        meta = next((h for h in harnesses if str(h.get("id") or "") == hid), {})
        accounts: List[Dict[str, Any]] = []
        native_row = next(
            (r for r in native_rows if str(r.get("harness_id") or "") == hid), None
        )
        # The pool owns routing where it speaks; the legacy row answers where it
        # does not. Neither present means routing was not stated at all — nor
        # does a list not read now state it: its verdict is last known, and
        # "next up" is a claim about now (as verification_view words a check).
        verdict = pools.get(hid) if hid in pools else (native_row or {}).get("next_up")
        routing_read = (hid in pools or native_row is not None) and accounts_read == READ_OK
        if not routing_read:
            verdict = None
        for row in native_rows:
            if str(row.get("harness_id") or "") != hid:
                continue
            accounts.append(_native_account(
                row, snapshots, absences, hid, quota_read, accounts_read, verdict, now,
                attributed=attributed,
            ))
        for row in profile_rows:
            profile = row.get("profile") or {}
            if str(profile.get("harness_id") or "") != hid:
                continue
            accounts.append(_profile_account(
                row, snapshots, absences, hid, quota_read, accounts_read, verdict, now,
                attributed=attributed,
            ))
        groups.append({
            "harness_id": hid,
            "family_label": str(meta.get("display_name") or meta.get("displayName") or hid),
            # A cached catalog can still name a family, but cannot say
            # whether its harness is healthy or enabled now.
            "harness_status": str(meta.get("status") or "") if states.get("catalog") == READ_OK else "",
            "harness_enabled": bool(meta.get("enabled")) if meta and states.get("catalog") == READ_OK else None,
            "provider_family": str(meta.get("provider_family") or meta.get("providerFamily") or ""),
            "catalog_known": states.get("catalog") == READ_OK and bool(meta),
            "routing_read": routing_read,
            "accounts": accounts,
            "reset_credits": qs.family_reset_credits([a["quota"]["reset_credits"] for a in accounts]),
            "accounts_signed_in": sum(1 for a in accounts if a["signed_in"]),
            "accounts_unavailable": accounts_read != READ_OK,
        })
    return groups


def _native_account(
    row: Dict[str, Any],
    snapshots: Any,
    absences: Any,
    hid: str,
    quota_read: str,
    accounts_read: str,
    verdict: Any = None,
    now: Optional[float] = None,
    *,
    attributed: Optional[qs.Attributed] = None,
) -> Dict[str, Any]:
    identity = row.get("identity") or {}
    signed_in = bool(row.get("native_login_detected"))
    return {
        "key": f"{hid}:native",
        "kind": "native",
        "subject_id": None,
        "label": "Default CLI login",
        "caption": "managed by the vendor CLI",
        "email": str(identity.get("email") or ""),
        "plan": str(identity.get("plan") or ""),
        "enabled": bool(row.get("native_credentials_enabled")),
        "signed_in": signed_in,
        "next_up": is_next_up(verdict, "native", ""),
        "last_verified_at": "",
        "verification_state": "",
        "verification_source": "",
        "verified_live": False,
        "verification": verification_view("", "", accounts_read, signed_in),
        "quota": quota_for(snapshots, hid, "", quota_read, absences, now=now, attributed=attributed),
    }


def _profile_account(
    row: Dict[str, Any],
    snapshots: Any,
    absences: Any,
    hid: str,
    quota_read: str,
    accounts_read: str,
    verdict: Any = None,
    now: Optional[float] = None,
    *,
    attributed: Optional[qs.Attributed] = None,
) -> Dict[str, Any]:
    profile = row.get("profile") or {}
    status = row.get("status") or {}
    identity = row.get("identity") or {}
    profile_id = str(profile.get("profile_id") or "")
    verification = str(status.get("verification") or "")
    verification_source = str(status.get("verification_source") or "")
    availability = str(status.get("availability") or "")
    signed_in = verification == "passed" or availability == "available"
    return {
        "key": f"{hid}:{profile_id}",
        "kind": "profile",
        "subject_id": profile_id,
        "label": str(profile.get("display_name") or profile_id or "account"),
        "caption": str(profile.get("credential_kind") or ""),
        "email": str(identity.get("email") or ""),
        "plan": str(identity.get("plan") or status.get("plan_label") or ""),
        "enabled": bool(profile.get("enabled")),
        "signed_in": bool(signed_in),
        "next_up": is_next_up(verdict, "profile", profile_id),
        "last_verified_at": str(status.get("last_verified_at") or ""),
        "verification_state": verification,
        "verification_source": verification_source,
        "verified_live": (
            accounts_read == READ_OK
            and verification == "passed"
            and verification_source == "vendor"
        ),
        "detail": str(status.get("detail") or ""),
        "availability": availability,
        "verification": verification_view(
            verification, verification_source,
            accounts_read, bool(signed_in),
        ),
        "quota": quota_for(snapshots, hid, profile_id, quota_read, absences, now=now,
                           attributed=attributed),
    }


def build_quota_updates(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one exact foreground envelope without rebuilding status."""
    if (
        not isinstance(payload.get("snapshots"), list)
        or not isinstance(payload.get("absences"), list)
        or "refreshed_at" not in payload
    ):
        return {"ok": False, "message": "Live quota refresh returned an invalid response"}
    snapshots = [
        row for row in (payload.get("snapshots") or []) if isinstance(row, dict)
    ]
    absences = [
        row for row in (payload.get("absences") or []) if isinstance(row, dict)
    ]
    refresh_skipped = [
        row for row in (payload.get("refresh_skipped") or []) if isinstance(row, dict)
    ]
    identities: List[Tuple[str, str]] = []
    for row in snapshots + absences:
        subject = row.get("subject") if isinstance(row.get("subject"), dict) else {}
        harness_id = str(subject.get("harness") or "")
        identity = (harness_id, _subject_key(subject.get("subject_id")))
        if harness_id and identity not in identities:
            identities.append(identity)
    return {
        "ok": True,
        "quota_updates": [
            {
                "harness": harness_id,
                "subject_id": None if subject_id == "" else subject_id,
                "quota": quota_for(
                    snapshots,
                    harness_id,
                    subject_id,
                    READ_OK,
                    absences,
                    refresh_skipped,
                ),
            }
            for harness_id, subject_id in identities
        ],
        "refreshed_at": str(payload.get("refreshed_at") or ""),
    }


def build_view(payload: Optional[Dict[str, Any]], transport_error: str,
               now: Optional[float] = None, *,
               reads: Optional[Dict[str, str]] = None,
               cached: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """The whole widget payload, provenance first. ``now`` is the moment the
    account view judges readings at — the route passes the reserve's own.

    ``payload`` may be an effective one (LatestRead.effective) whose
    ``cached`` facets come from an earlier read; ``reads`` are then the
    facet states the host actually gave, and stay what ``facets`` says."""
    effective = facet_states(payload)
    states = dict(reads) if reads is not None else effective
    # Accounts are listed from the effective payload (a kept roster stays on
    # screen) while their verification says "last known" whenever the
    # account list was not read now.
    group_states = dict(effective)
    group_states["accounts"] = states.get("accounts", "indeterminate")
    group_states["catalog"] = states.get("catalog", "indeterminate")
    daemon = (payload or {}).get("daemon")
    daemon = daemon if isinstance(daemon, dict) else {}
    runtime = daemon.get("runtime") if isinstance(daemon.get("runtime"), dict) else {}
    passive = passive_read_info(payload, transport_error)
    # The dedicated envelope deliberately omits catalog diagnostics. Keep
    # its truthful not_read state, without treating that omission as a failed
    # quota read or preventing a healthy screen from becoming the fallback.
    required = tuple(f for f in FACETS if not (
        f == "catalog" and passive["mode"] == "quota" and states.get(f) == "not_read"))
    view: Dict[str, Any] = {
        "ok": bool(payload) and not transport_error,
        "transport_error": transport_error,
        "facets": states,
        "facet_note": facet_note({f: states.get(f, "indeterminate") for f in required}),
        "passive_read": passive,
        "daemon": {
            "state": str(daemon.get("state") or (
                "unknown" if payload and not transport_error and passive["mode"] != "quota" else "")),
            "engine_version": str(daemon.get("engine_version") or ""),
            "self_started": bool(daemon.get("self_started")),
            "last_error": str(runtime.get("last_error") or daemon.get("last_error") or ""),
        },
        "groups": build_groups(payload, group_states, now) if isinstance(payload, dict) else [],
        # Facets shown from the last read that answered them: facet -> when.
        "cached": {facet: qs.iso(at) for facet, at in sorted((cached or {}).items())},
        # Every requested facet read now and nothing failed on the way: the
        # only kind of answer the widget keeps as its own fallback.
        "complete": bool(payload) and not transport_error
        and all(states.get(facet) == READ_OK for facet in required),
    }
    return view


def clean_prefs(raw: Any) -> Dict[str, Any]:
    """Whatever comes back from disk or from the widget, reduced to what this
    skill is willing to remember. An unknown value is not corrected into a
    guess — it is dropped, and the default stands in its place."""
    out: Dict[str, Any] = {
        "density": DEFAULT_PREFS["density"],
        "models": {},
        # A copy, not the map itself: one shared map here and the first
        # write would edit the defaults themselves.
        "fold": dict(DEFAULT_PREFS["fold"]),
    }
    if not isinstance(raw, dict):
        return out
    density = raw.get("density")
    if density in DENSITIES:
        out["density"] = density
    models = raw.get("models")
    if isinstance(models, dict):
        for harness_id, choice in list(models.items())[:MAX_MODEL_ENTRIES]:
            key = str(harness_id)[:MAX_HARNESS_ID].strip()
            if key and choice in MODEL_VIEWS:
                out["models"][key] = choice
    fold = raw.get("fold")
    if isinstance(fold, dict):
        for reason in FOLD_REASONS:
            choice = fold.get(reason)
            # Only a real boolean answers this. "no", 0 and null are somebody
            # else's idea of false, and guessing which way they meant would
            # fold accounts away, or stop folding them, behind the reader.
            if isinstance(choice, bool):
                out["fold"][reason] = choice
    return out


def prefs_order(raw: Any) -> Optional[Tuple[str, int]]:
    """The reader's order of one save — ``(frame, seq)``: the widget load that
    made it and its number there — or None when the save carries none (an
    older widget) or one that is not a short name and a positive whole
    number. A save without one is kept by the order it arrived in."""
    if not isinstance(raw, dict):
        return None
    frame, seq = raw.get("frame"), raw.get("seq")
    if not isinstance(frame, str) or not 0 < len(frame) <= MAX_PREFS_FRAME:
        return None
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        return None
    return frame, seq


def _prefs_path(api: Any) -> Optional[Path]:
    try:
        return Path(api.get_state_dir()) / PREFS_FILE
    except Exception:
        return None


def read_prefs(api: Any) -> Dict[str, Any]:
    """Never raises: a widget that cannot learn the saved choice still has to
    draw one, and the default is a fine answer."""
    path = _prefs_path(api)
    if path is None or not path.is_file():
        return clean_prefs(None)
    try:
        return clean_prefs(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return clean_prefs(None)


def write_prefs(api: Any, raw: Any) -> Tuple[Dict[str, Any], str]:
    """Returns (what is now stored, error). The stored value is returned rather
    than echoed back from the request: the widget then draws what the skill
    actually kept, not what it hoped to send."""
    prefs = clean_prefs(raw)
    path = _prefs_path(api)
    if path is None:
        return prefs, "no state directory"
    # Written beside the file and moved over it in one step. A write cut in half
    # leaves prefs.json unreadable, and the next read answers with the defaults
    # — the reader's choices gone without anything saying so. The temporary
    # file has a name of its own, so two saves at once (from two loads of this
    # skill, or two processes) never write into one.
    tmp: Optional[Path] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        tmp = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(prefs))
        os.replace(tmp, path)
    except Exception as exc:
        try:
            if tmp is not None:
                tmp.unlink()
        except Exception:
            # Nothing to clean up, or nothing that can be: the error that
            # brought us here is the one worth reporting.
            pass
        return prefs, f"{type(exc).__name__}: {exc}"
    return prefs, ""


# ---------------------------------------------------------------------------
# Reserve overview, history collector and the model tool


class LatestRead:
    """The last successful passive status read in this process, so a route
    call and the collector do not ask the daemon twice within moments.

    A live Refresh makes every earlier read out of date: :meth:`invalidate`
    forgets it, and a read that was already in the air (begun under an older
    :meth:`epoch`) is not remembered when it returns.

    Apart from that, it keeps each facet of the last read that answered it
    (memory only, this process only): a later read that fails a facet — or
    fails altogether — is answered for that facet from it, marked as such
    (:meth:`effective`). Invalidation does not forget these: they are dated
    last-known facts, not a read to reuse."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: Optional[Dict[str, Any]] = None
        self._at = 0.0
        self._clock = 0.0
        self._epoch = 0
        # facet -> (the parts of the payload that carry it, when it was read)
        self._facets: Dict[str, Tuple[Dict[str, Any], float]] = {}

    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def invalidate(self) -> None:
        with self._lock:
            self._epoch += 1
            self._payload = None

    def put(self, payload: Optional[Dict[str, Any]], error: str, read_at: float,
            epoch: Optional[int] = None) -> None:
        if not isinstance(payload, dict) or error:
            # A read that failed is newer than the last one that did not: a
            # chart or family switch must not answer from that older read as
            # if nothing had failed since. The dated facets below are kept.
            with self._lock:
                if (epoch is None or epoch == self._epoch) and read_at >= self._at:
                    self._payload = None
            return
        states = facet_states(payload)
        with self._lock:
            if epoch is not None and epoch != self._epoch:
                return  # begun before a Refresh returned
            self._payload, self._at, self._clock = payload, read_at, time.monotonic()
            for facet, keys in FACET_KEYS.items():
                if states.get(facet) != READ_OK:
                    continue
                kept = self._facets.get(facet)
                if kept is not None and kept[1] > read_at:
                    continue  # a newer read already answered this facet
                self._facets[facet] = ({key: payload.get(key) for key in keys}, read_at)

    def effective(self, payload: Optional[Dict[str, Any]]
                  ) -> Tuple[Optional[Dict[str, Any]], Dict[str, float]]:
        """``payload`` with every facet it did not read answered from the last
        read that did, and which facets those are (facet -> read time).

        A kept quota facet is last known, never fresh: each of its readings is
        marked stale. A kept account list keeps the roster only (the summary
        reports every account's state unknown). A facet this read answered —
        an empty roster included — is never replaced: a current answer wins,
        so an account deleted since is not brought back."""
        states = facet_states(payload)
        with self._lock:
            kept = {facet: self._facets[facet] for facet in FACETS
                    if states.get(facet) != READ_OK and facet in self._facets}
        if not kept:
            return payload, {}
        out: Dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
        reads = dict(states)
        cached: Dict[str, float] = {}
        for facet, (parts, read_at) in kept.items():
            for key, value in parts.items():
                if facet == "quota" and key == "quota" and isinstance(value, list):
                    value = [qs.stale_snapshot(row) if isinstance(row, dict) else row
                             for row in value]
                out[key] = value
            reads[facet] = READ_OK
            cached[facet] = read_at
        out["reads"] = reads
        return out, cached

    def get(self, max_age_sec: float) -> Optional[Tuple[Dict[str, Any], float]]:
        with self._lock:
            if self._payload is None or time.monotonic() - self._clock > max_age_sec:
                return None
            return self._payload, self._at


def history_store(api: Any) -> Optional[quota_history.HistoryStore]:
    try:
        return quota_history.HistoryStore(Path(api.get_state_dir()))
    except Exception:
        return None


def reserve_view(
    store: Optional[quota_history.HistoryStore],
    payload: Optional[Dict[str, Any]],
    status_read_at: Optional[float],
    now: float,
    *,
    harness: str = "",
    group: str = "",
    horizon: str = "24h",
    chart: bool = True,
    cached: Optional[Dict[str, float]] = None,
    reads: Optional[Dict[str, str]] = None,
    name_accounts: bool = False,
) -> Dict[str, Any]:
    """The one reserve calculation the widget and the tool share. History is
    read, never written, here. ``cached``/``reads``: see build_view.
    ``name_accounts`` puts the widget's account key on each bar — the widget
    route only, beside the account list it already sends; the tool and every
    other answer carry no account identity."""
    def read(norm: qs.Normalized, groups: List[qs.GroupCalc],
             chart_for: Optional[qs.GroupCalc]) -> qs.HistoryView:
        if store is None:
            return qs.HistoryView(state="unavailable", error="no state directory")
        return store.read(
            lambda salt: qs.history_requests(groups, salt, now, chart_for, horizon, norm=norm), now,
            latest=lambda salt: qs.latest_requests(norm, groups, salt),
            roster=lambda salt: qs.roster_requests(norm, salt),
            chart_series=(chart_for.key, now - qs.HORIZONS.get(horizon, qs.HORIZONS["24h"]))
            if chart_for is not None else None,
        )

    norm, groups = qs.prepare(payload, now, cached)
    chart_calc = qs.pick_chart_group(groups, group, harness) if chart else None
    view = read(norm, groups, chart_calc)
    state = qs.finish(norm, groups, view, now)
    if chart and store is not None:
        # A limit known only from the history exists after finish, not
        # before: the read above could not ask for its range. When the chart
        # opens on one (the one asked for, or the family's default), read
        # once more, bounded the same way, with it as the charted limit.
        found = qs.pick_chart_group(state.groups, group, harness)
        if found is not None and (chart_calc is None or found.key != chart_calc.key):
            chart_calc = found
            norm, groups = qs.prepare(payload, now, cached)
            view = read(norm, groups, chart_calc)
            state = qs.finish(norm, groups, view, now)
    out: Dict[str, Any] = {
        "summary": qs.build_summary(payload, view, now, status_read_at=status_read_at, state=state,
                                    reads=reads if reads is not None else qs.facet_reads(payload),
                                    name_accounts=name_accounts),
    }
    if chart:
        out["chart"] = qs.build_chart(state, chart_calc.key if chart_calc else "", harness, horizon)
    return out


def sweep_state(payload: Optional[Dict[str, Any]], error: str) -> Tuple[bool, str]:
    """Whether a sweep read the quota facet at all, and if not, why not."""
    if error or not isinstance(payload, dict):
        return False, "status_unreadable"
    state = qs.facet_reads(payload)["quota"]
    if state != "ok":
        return False, f"quota_{state}"
    return True, ""


def persist_sweep(
    store: quota_history.HistoryStore,
    payload: Optional[Dict[str, Any]],
    error: str,
    read_at: float,
    *, control: Optional[quota_history.StopControl] = None,
) -> Dict[str, Any]:
    """Record one sweep: the fresh numeric readings it saw, or only the fact
    that it saw nothing (a gap in the record, never a zero)."""
    ok, reason = sweep_state(payload, error)
    readings: List[qs.Reading] = []
    if ok:
        readings = qs.recordable(qs.normalize(payload, read_at), read_at)
    return store.record_sweep(readings, read_at, ok, reason, control=control)


# The collector's cycle lease: an empty file whose OS lock, taken without
# waiting, lets one worker at a time read and write, across reloads in one
# process as well as across processes sharing the state directory. Both locks
# below belong to one open file (not to the process), and the OS drops them if
# the holder dies, so a crash leaves no stale lease behind.
LEASE_FILE = "quota_collector.lock"
Lease = Tuple[Callable[[int], bool], Callable[[int], None]]
# What a nonblocking Windows lock reports when another handle holds the byte.
_WINDOWS_BUSY = frozenset(
    code for code in (errno.EACCES, getattr(errno, "EDEADLOCK", None), getattr(errno, "EDEADLK", None))
    if code is not None
)


def _posix_lease(fcntl: Any) -> Lease:
    """flock on the whole file (macOS, Linux)."""

    def acquire(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def release(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

    return acquire, release


def _windows_lease(msvcrt: Any) -> Lease:
    """One byte at offset 0 through msvcrt.locking, which locks from the
    current position; locking past the end of the empty file is allowed."""

    def acquire(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in _WINDOWS_BUSY:
                return False
            raise
        return True

    def release(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    return acquire, release


def os_lease() -> Lease:
    """(acquire, release) for this platform's nonblocking exclusive file
    lock. A platform with neither fails closed: no cycle runs without it."""
    try:
        import fcntl
    except ImportError:
        pass
    else:
        return _posix_lease(fcntl)
    try:
        import msvcrt
    except ImportError:
        raise RuntimeError("no OS file lock on this platform; sweep not kept") from None
    return _windows_lease(msvcrt)


def make_collector(
    api: Any,
    latest: LatestRead,
    stop: threading.Event | quota_history.StopControl,
    *,
    read: Optional[Callable[[], Tuple[Optional[Dict[str, Any]], str]]] = None,
    interval_sec: float = COLLECT_INTERVAL_SEC,
    first_delay_sec: float = COLLECT_FIRST_DELAY_SEC,
    clock: Callable[[], float] = time.time,
) -> Callable[[], Any]:
    """One supervised coroutine, with one blocking cycle in the host executor.

    A nonblocking OS file lease (os_lease) serializes cycles across module
    reloads. The stop callback only requests cancellation; control.settled
    acknowledges that the worker has finished rollback/commit, close and lease
    release. Public host unload does not await that acknowledgement.

    control is the registration's stop; each run of the returned factory
    stops under a control of its own. A run that ends on an error stops only
    itself, so the host's on_failure restart runs again. Unload and host
    cancellation stop the registration, and a run started after either ends
    before any I/O.
    """
    control = stop if isinstance(stop, quota_history.StopControl) else quota_history.StopControl(stop)
    store: Optional[quota_history.HistoryStore] = None
    unreadable = ""

    def cycle(run: quota_history.StopControl) -> None:
        nonlocal store, unreadable
        run.check()
        # Resolved on the worker: a platform without an OS file lock fails
        # closed, rather than silently running without cross-generation ownership.
        acquire, release = os_lease()
        store = store or history_store(api)
        if store is None:
            raise RuntimeError("no state directory; sweep not kept")
        run.check()
        store.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(store.path.parent / LEASE_FILE), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if not acquire(fd):
                return  # previous registration is still settling
            try:
                run.check()
                cached = latest.get(interval_sec / 2)
                if cached is not None:
                    payload, error, read_at = cached[0], "", cached[1]
                else:
                    epoch = latest.epoch()
                    # Port lookup (runtime info, a file read) comes first; the
                    # request is admitted right before it starts, so a stop
                    # requested before admission prevents it. One admitted just
                    # before a stop may start after the stop callback returns;
                    # its answer is discarded below.
                    port = _server_port(api) if read is None else 0
                    run.admit()
                    payload, error = read() if read is not None else _fetch_status(port)
                    read_at = clock()
                    # A read returning after stop must never reach storage.
                    run.check()
                    latest.put(payload, error, read_at, epoch)
                run.check()
                # Returns only after its connection is closed; the lease is
                # released after that, and settlement is signalled after this.
                persist_sweep(store, payload, error, read_at, control=run)
                unreadable = ""
            finally:
                release(fd)
        finally:
            os.close(fd)

    async def collector() -> None:
        nonlocal unreadable
        control.settled.clear()
        run = quota_history.StopControl(parent=control)
        pending = None
        work_done = threading.Event()
        work_done.set()
        phase_lock = threading.Lock()
        phase = "done"

        def work() -> None:
            nonlocal phase
            with phase_lock:
                if phase != "pending":
                    return
                phase = "active"
            try:
                cycle(run)
            finally:
                with phase_lock:
                    phase = "done"
                    work_done.set()

        try:
            await asyncio.sleep(first_delay_sec)
            while not run.is_set():
                with phase_lock:
                    phase = "pending"
                    work_done.clear()
                pending = asyncio.get_running_loop().run_in_executor(None, work)
                # Completion can be posted to the loop just after work_done
                # is set. Consume errors even if cancellation wins that race.
                pending.add_done_callback(lambda f: None if f.cancelled() else f.exception())
                try:
                    await asyncio.shield(pending)
                except quota_history.HistoryStopped:
                    return
                except quota_history.HistoryCorrupt as exc:
                    if not run.is_set() and str(exc) != unreadable:
                        unreadable = str(exc)
                        api.log("warning", "claudexor quota history unreadable; left as is and "
                                f"not written ({exc}). Remove {quota_history.HISTORY_FILE} "
                                "from the skill's state directory to start a new history.")
                except Exception as exc:
                    if not run.is_set():
                        api.log("warning", f"claudexor quota history sweep not kept: {type(exc).__name__}")
                if not run.is_set():
                    await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            # Disable, unload and shutdown cancel the task: nothing after it
            # may start again, even if this factory is called once more.
            control.set()
            raise
        finally:
            # Only this run: an error that ends it must not stop the next one.
            run.set()
            with phase_lock:
                if phase == "pending":
                    phase = "done"  # a queued job can no longer start I/O
                    work_done.set()
                    if pending is not None:
                        pending.cancel()
            # Future cancellation is not worker completion. Drain without
            # blocking the loop or consuming another executor thread. Repeated
            # host cancellation still cannot detach a running writer.
            while not work_done.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    pass
            control.settled.set()

    collector.control = control
    return collector


TOOL_DESCRIPTION = (
    "Read-only summary of measured subscription quota reserve per agent family "
    "and limit: remaining account-windows from current readings (each account's "
    "remaining share of one limit counts as one; not tokens or hours) and, apart, "
    "dated last-known windows, coverage, restrictions (disabled, signed out, "
    "cooling down, another limit spent), next reported reset, even pace to that "
    "reset, recent pace from local history and a one-line headline per limit. "
    "Reads the host's "
    "cached status and this skill's own history; never refreshes a provider and "
    "never changes model or account pins. Optional: consult before choosing "
    "executors when quota matters."
)

TOOL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "harness": {
            "type": "string",
            "description": "Optional agent family id (for example claude or codex) to limit the answer.",
        },
        "detail": {
            "type": "boolean",
            "description": "Also return plan breakdowns and why recent pace is unknown.",
        },
    },
    "additionalProperties": False,
}


def tool_answer(
    api: Any,
    latest: LatestRead,
    harness: Any = "",
    detail: Any = False,
    *,
    fetch: Optional[Callable[[], Tuple[Optional[Dict[str, Any]], str]]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    wanted = str(harness or "").strip()[:64] if isinstance(harness, str) else ""
    cached = latest.get(REUSE_MAX_AGE_SEC)
    if cached is not None:
        payload, error, read_at = cached[0], "", cached[1]
    else:
        epoch = latest.epoch()
        reader = fetch or (lambda: _fetch_status(_server_port(api), TOOL_STATUS_TIMEOUT_SEC))
        payload, error = reader()
        read_at = time.time()
        # Kept as the widget route keeps its reads: the facets it answered
        # are what a later failed read is answered from, and a failed one
        # stops an older read being reused (LatestRead.put, epoch-aware).
        latest.put(payload, error, read_at, epoch)
    moment = time.time() if now is None else now
    effective, cached = latest.effective(payload)
    result = reserve_view(history_store(api), effective, None if error else read_at, moment,
                          chart=False, cached=cached, reads=qs.facet_reads(payload))
    answer = qs.compact(result["summary"], wanted, detail is True)
    answer["passive_read"] = passive_read_info(payload, error)
    if len(answer["groups"]) > TOOL_MAX_GROUPS:
        answer["groups_omitted"] = len(answer["groups"]) - TOOL_MAX_GROUPS
        answer["groups"] = answer["groups"][:TOOL_MAX_GROUPS]
    if error:
        answer["status_error"] = ("The host status endpoint did not answer; nothing is claimed "
                                  "as current." + (" Last-known readings come from the facets "
                                                   "in cached_facets." if cached else ""))
    return answer


def _query(request: Any, name: str) -> str:
    params = getattr(request, "query_params", None)
    if params is None and isinstance(request, dict):
        params = request.get("query_params")
    try:
        value = params.get(name) if params is not None else None
    except Exception:
        value = None
    return str(value or "")[:200]


def register(api: Any) -> None:
    latest = LatestRead()
    stop = quota_history.StopControl()

    def quotas_route(request: Any) -> Dict[str, Any]:
        cached = latest.get(REUSE_MAX_AGE_SEC) if _query(request, "reuse") == "1" else None
        if cached is not None:
            payload, transport_error, read_at = cached[0], "", cached[1]
        else:
            epoch = latest.epoch()
            payload, transport_error = _fetch_status(_server_port(api))
            read_at = time.time()
            if transport_error:
                api.log("error", f"claudexor status read failed: {transport_error}")
            latest.put(payload, transport_error, read_at, epoch)
        # One moment for the account view and the reserve: a reading is
        # current, or not, for both at once.
        moment = time.time()
        # A facet this read did not answer is shown from the last read that
        # did, dated and never fresh; the facets keep saying what was read.
        effective, cached = latest.effective(payload)
        reads = facet_states(payload)
        view = build_view(effective, transport_error, moment, reads=reads, cached=cached)
        # Legacy since 0.8.0: the display choices (row detail, model filter,
        # folding) belonged to the account list the 0.7 widget drew. This
        # widget draws nothing from them and saves none; they are still sent,
        # and the prefs route still keeps them, for an older widget.
        view["prefs"] = read_prefs(api)
        horizon = _query(request, "horizon")
        try:
            view["reserve"] = reserve_view(
                history_store(api), effective, None if transport_error else read_at, moment,
                harness=_query(request, "harness"), group=_query(request, "group"),
                horizon=horizon if horizon in HORIZON_CHOICES else "24h",
                # The widget keeps its chart folded until asked; while it is,
                # the chart and its longer history read are not computed.
                chart=_query(request, "chart") != "0",
                cached=cached, reads=reads, name_accounts=True,
            )
        except Exception as exc:
            api.log("error", f"claudexor reserve summary failed: {type(exc).__name__}: {exc}")
            view["reserve"] = {"summary": None, "chart": None,
                               "error": f"reserve summary failed ({type(exc).__name__})"}
        return view

    # The prefs file is read and written off the event loop. One save at a
    # time, as when they ran on the loop; and when two cross on their way to
    # the file, the one that arrived later stands. The later arrival is not
    # always the reader's later choice, though: a widget numbers its saves,
    # and one that arrives after a newer save of the same widget load is not
    # written at all. That check runs here on the loop, at arrival, so among
    # the saves that are written arrival order is the reader's order.
    prefs_lock = threading.Lock()
    prefs_arrivals = itertools.count(1)
    prefs_stored = 0
    prefs_newest: Dict[str, int] = {}  # per widget load, the newest save arrived

    def save_prefs(arrival: int, payload: Any) -> Tuple[Dict[str, Any], str]:
        nonlocal prefs_stored
        with prefs_lock:
            if arrival < prefs_stored:
                return read_prefs(api), ""  # a later choice is already kept
            prefs_stored = arrival
            return write_prefs(api, payload)

    async def prefs_route(request: Any) -> Dict[str, Any]:
        unread = object()
        try:
            payload = await request.json()
        except Exception:
            payload = unread
        loop = asyncio.get_running_loop()
        # A body that did not parse is not a request to forget everything.
        # Writing the cleaned defaults here would wipe the reader's choices and
        # answer as if it had saved them.
        if payload is unread:
            api.log("error", "claudexor prefs write refused: request body was not JSON")
            return {"prefs": await loop.run_in_executor(None, read_prefs, api),
                    "error": "request body was not JSON"}
        order = prefs_order(payload)
        if order is not None:
            frame, seq = order
            if seq <= prefs_newest.get(frame, 0):
                # Overtaken on its way here by a newer choice of the same
                # widget: not written, and not an error — it answers with
                # what is kept, which that widget no longer draws from.
                return {"prefs": await loop.run_in_executor(None, read_prefs, api), "error": ""}
            prefs_newest.pop(frame, None)
            prefs_newest[frame] = seq
            while len(prefs_newest) > MAX_PREFS_FRAMES:
                prefs_newest.pop(next(iter(prefs_newest)))
        prefs, error = await loop.run_in_executor(None, save_prefs, next(prefs_arrivals), payload)
        if error:
            api.log("error", f"claudexor prefs write failed: {error}")
        return {"prefs": prefs, "error": error}

    def refresh_route(_request: Any) -> Dict[str, Any]:
        payload, transport_error, status = _refresh_quota(_server_port(api))
        # Whatever the outcome (the host may still finish a request that
        # failed on this side), a status read from before it is not reused:
        # the next chart or family switch, tool call or sweep reads anew.
        latest.invalidate()
        updates = None if transport_error else build_quota_updates(payload if isinstance(payload, dict) else {})
        if updates is not None and not updates.get("ok"):
            # A success status whose body is not a refresh envelope says
            # nothing about whether the refresh ran: no answer read either.
            transport_error, status = "response was not a refresh envelope", 200
        if transport_error:
            # No answer read — a timeout, a connection closed after the
            # request went out, an answer that broke off or could not be
            # read: the host may have refreshed. Its outcome is unknown, said
            # so, and the request is never sent again on its own; the next
            # reading shows whatever it changed. Only an answer that says it
            # failed (or a request that never reached the host) is a failure.
            if status in (NO_ANSWER_STATUS, 200):
                api.log("error", f"claudexor live quota refresh outcome unknown: {transport_error}")
                return {
                    "ok": False,
                    "compatibility_error": False,
                    "outcome_unknown": True,
                    "message": ("Live refresh got no readable answer, so whether it ran is unknown. "
                                "It is not sent again; the next reading shows any change."),
                }
            api.log("error", f"claudexor live quota refresh failed: {transport_error}")
            compatibility_error = status in {404, 405}
            return {
                "ok": False,
                "compatibility_error": compatibility_error,
                "message": (
                    "Live refresh requires a newer Ouroboros host"
                    if compatibility_error
                    else "Live quota refresh failed"
                ),
            }
        return updates

    def quota_summary_tool(ctx: Any = None, harness: str = "", detail: bool = False) -> str:
        try:
            answer = tool_answer(api, latest, harness, detail)
        except Exception as exc:
            api.log("error", f"claudexor quota_summary failed: {type(exc).__name__}: {exc}")
            answer = {"error": f"quota summary failed ({type(exc).__name__}); nothing is claimed"}
        return json.dumps(answer, separators=(",", ":"))

    api.register_route("quotas", quotas_route, methods=("GET",))
    api.register_route("refresh", refresh_route, methods=("POST",))
    api.register_route("prefs", prefs_route, methods=("POST",))
    api.register_tool(
        "quota_summary",
        quota_summary_tool,
        description=TOOL_DESCRIPTION,
        schema=TOOL_SCHEMA,
        timeout_sec=45,
    )
    # One collector per loaded skill instance, owned by the host: it starts
    # only when the server publishes this registration (worker processes only
    # record it), and disable, unload or shutdown cancels it. Widget frames
    # never start one, however many are open.
    api.register_supervised_task(
        "quota_collector",
        make_collector(api, latest, stop),
        restart_policy="on_failure",
        max_restarts=3,
        backoff_seconds=30.0,
    )
    api.on_unload(stop.set)
    api.register_ui_tab(
        "quotas",
        "Claudexor Quotas",
        icon=UI_ICON,
        render={
            "kind": "module", "entry": "widget.js", "appearance": "host",
            "span": 2, "start": "auto",
        },
    )
