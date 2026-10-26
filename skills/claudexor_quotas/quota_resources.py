"""Account resources from the passive quota view — standard library only.

The engine's ``resources_freshness`` read carries, beside the quota windows,
one ``resources[]`` row per credential profile with four independently dated
facets: ``balances`` (credits, included/bonus/on-demand amounts), ``spending``
(spend against a limit), ``resets`` (manual reset offers the vendor grants)
and ``diagnostics``. This module reads them exactly, and says only what they
say:

* a facet is *current* only while the engine marks it ``fresh``; ``stale``
  keeps its value as a dated last-known fact, ``unknown`` has none;
* an amount that is not reported is ``None`` — never ``0``; a reported ``"0"``
  of one pool (``included``) is a fact about that pool and is not read as the
  account being blocked, and a team figure is a team figure, never the
  personal quota of the profile it was read through;
* a reset offer is information: nothing here, in the widget or in the tool,
  ever offers to spend one, and a count of manual credits is shown, never
  added to a window;
* the ``availability_scope`` echo says which model and request kind the
  engine judged availability against; an answer without it is shown as
  "scope not stated", never as "any model".

No credential, request or provider call lives here; the module only
reshapes what the host already read.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

try:
    from . import quota_summary as qs
except ImportError:
    import quota_summary as qs

MAX_ROWS = 12
MAX_TEXT = 80
MAX_NOTE = 160
FRESHNESS = ("fresh", "stale", "unknown")
FACETS = ("balances", "spending", "resets", "diagnostics")
SCOPES = ("personal", "team")
USAGE_KINDS = ("included", "bonus", "on_demand", "total")
REQUEST_KINDS = ("text", "image")


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _instant(value: Any) -> Optional[str]:
    return _text(value, 40) or None if isinstance(value, str) else None


def _count(value: Any) -> Optional[int]:
    """A whole non-negative count as reported; anything else is not a count."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _flag(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _enum(value: Any, allowed: Tuple[str, ...]) -> Optional[str]:
    return value if isinstance(value, str) and value in allowed else None


def _period(raw: Any) -> Optional[Dict[str, Optional[str]]]:
    if not isinstance(raw, dict):
        return None
    return {"starts_at": _instant(raw.get("starts_at")), "ends_at": _instant(raw.get("ends_at"))}


def _scope_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "scope": _enum(raw.get("scope"), SCOPES),
        "usage_kind": _enum(raw.get("usage_kind"), USAGE_KINDS),
        "period": _period(raw.get("period")),
        "fee_included": _flag(raw.get("fee_included")),
    }


def _note(row: Dict[str, Any]) -> str:
    """One short disclosure per row, so a figure is never read for more than
    it says: whose it is and what a zero or a missing amount means."""
    notes: List[str] = []
    if row.get("scope") == "team":
        notes.append("team figure, not this profile's personal quota")
    if row.get("usage_kind") == "included" and row.get("amount") == "0":
        notes.append("zero included here is not a block on the account")
    if row.get("fee_included") is True:
        notes.append("includes fees")
    elif row.get("fee_included") is None and "fee_included" in row:
        notes.append("fee treatment not stated")
    return "; ".join(notes)[:MAX_NOTE]


def _balance(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    amount = raw.get("amount")
    row = {
        "id": _text(raw.get("id"), 120), "label": _text(raw.get("label")),
        # As a string, exactly: "0" is a reported zero, None is not reported.
        "amount": amount if isinstance(amount, str) else None,
        "unit": _text(raw.get("unit"), 24), "currency": _text(raw.get("currency"), 8) or None,
        "decimal_places": _count(raw.get("decimal_places")),
        "has_balance": _flag(raw.get("has_balance")), "unlimited": _flag(raw.get("unlimited")),
    }
    row.update(_scope_fields(raw))
    row["note"] = _note(row)
    return row


def _spending(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    used, limit = raw.get("used"), raw.get("limit")
    row = {
        "id": _text(raw.get("id"), 120), "label": _text(raw.get("label")),
        "enabled": _flag(raw.get("enabled")),
        "used": used if isinstance(used, str) else None,
        "limit": limit if isinstance(limit, str) else None,
        "unit": _text(raw.get("unit"), 24), "currency": _text(raw.get("currency"), 8) or None,
        "decimal_places": _count(raw.get("decimal_places")),
        "resets_at": _instant(raw.get("resets_at")), "reason": _text(raw.get("reason"), MAX_NOTE) or None,
    }
    row.update(_scope_fields(raw))
    row["note"] = _note(row)
    return row


def _offer(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    grants = raw.get("grants")
    return {
        "id": _text(raw.get("id"), 120), "kind": _enum(raw.get("kind"), ("granted_reset", "session_refill")),
        "label": _text(raw.get("label")), "available_count": _count(raw.get("available_count")),
        "eligible": _flag(raw.get("eligible")), "usable_now": _flag(raw.get("usable_now")),
        "reason": _text(raw.get("reason"), MAX_NOTE) or None, "resets_at": _instant(raw.get("resets_at")),
        "weekly_limit_applies": _flag(raw.get("weekly_limit_applies")),
        "grants": None if not isinstance(grants, list) else [
            {"id": _text(g.get("id"), 120), "label": _text(g.get("label")),
             "available_count": _count(g.get("available_count")),
             "total_count": _count(g.get("total_count")),
             "usable_now": _flag(g.get("usable_now")),
             "starts_at": _instant(g.get("starts_at")), "expires_at": _instant(g.get("expires_at"))}
            for g in grants[:MAX_ROWS] if isinstance(g, dict)],
    }


def _diagnostic(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    return {"code": _text(raw.get("code"), 64), "detail": _text(raw.get("detail"), MAX_NOTE) or None}


_READERS = {"balances": _balance, "spending": _spending, "resets": _offer, "diagnostics": _diagnostic}


def facet_view(raw: Any, facet: str, kept: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
    """One facet as the widget and the tool carry it.

    ``state``: ``current`` (fresh, with a value), ``last_known`` (stale, with
    a value: dated, never current), ``not_reported`` (no value) — judged only
    on the engine's own freshness word; a facet kept from an earlier read
    (``kept``) is never fresh. A missing or malformed facet is
    ``not_reported``, never an empty list read as "none"."""
    if not isinstance(raw, dict):
        return {"state": "not_reported", "freshness": "unknown", "observed_at": None, "source": None,
                "rows": None, "omitted": 0, "last_error": None}
    freshness = raw.get("freshness") if raw.get("freshness") in FRESHNESS else "unknown"
    if kept and freshness == "fresh":
        freshness = "stale"
    value = raw.get("value")
    rows: Optional[List[Dict[str, Any]]] = None
    omitted = 0
    if isinstance(value, list):
        read = _READERS[facet]
        parsed = [row for row in (read(item) for item in value) if row is not None]
        rows, omitted = parsed[:MAX_ROWS], max(0, len(parsed) - MAX_ROWS)
    if rows is None or freshness == "unknown":
        state = "not_reported"
    else:
        state = "current" if freshness == "fresh" else "last_known"
    if state == "current" and now is not None and facet != "diagnostics":
        for row in rows or []:
            bounds = (row.get("resets_at"), (row.get("period") or {}).get("ends_at"))
            if any(t is not None and t <= now for t in (qs.parse_instant(v) for v in bounds)):
                row["state"] = "last_known"
                row["note"] = ((row.get("note") or "") + "; last known: reported boundary passed").lstrip("; ")
        if rows and all(r.get("state") == "last_known" for r in rows):
            state, freshness = "last_known", "stale"
    return {
        "state": state, "freshness": freshness, "observed_at": _instant(raw.get("observed_at")),
        "source": _text(raw.get("source"), 48) or None, "rows": rows if state != "not_reported" else None,
        "omitted": omitted, "last_error": _text(raw.get("last_error"), MAX_NOTE) or None,
    }


def scope_echo(payload: Any) -> Dict[str, Any]:
    """The engine's ``availability_scope`` echo. ``stated`` is False for an
    answer without a well-formed one (an older engine, or a view that does
    not carry it): availability is then not scoped authority."""
    raw = payload.get("availability_scope") if isinstance(payload, dict) else None
    if (isinstance(raw, dict) and raw.get("requestKind") in REQUEST_KINDS
            and (raw.get("model") is None or (isinstance(raw.get("model"), str) and raw.get("model")))):
        # ``basis`` is the engine's own word for how it judged availability
        # (the quota-block projection); it is carried, never interpreted: the
        # skill has no matching algorithm of its own to compare it with.
        basis = raw.get("basis") if isinstance(raw.get("basis"), str) else None
        return {"stated": True, "model": raw.get("model"), "request_kind": raw["requestKind"], "basis": basis}
    return {"stated": False, "model": None, "request_kind": None, "basis": None}


def _targets(payload: Any) -> Dict[Tuple[str, str], Dict[str, Any]]:
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    rows = payload.get("resources") if isinstance(payload, dict) else None
    for row in (rows if isinstance(rows, list) else []):
        target = row.get("target") if isinstance(row, dict) else None
        if isinstance(target, dict) and _text(target.get("harness"), 64) and _text(target.get("profile_id"), 200):
            out[(_text(target["harness"], 64), _text(target["profile_id"], 200))] = row
    return out


def account_resources(payload: Any, harness: str, subject_id: str, kept: bool = False,
                      now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The four facets of one account, or None when the answer carries no
    resource row for it (an older engine or view: nothing is invented)."""
    row = _targets(payload).get((harness, subject_id))
    if row is None:
        return None
    return {facet: facet_view(row.get(facet), facet, kept, now) for facet in FACETS}


def family_resets(payload: Any, harness: str, kept: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
    """Manual reset offers across one family's accounts, current only: how
    many are reported usable, from how many accounts, and how many accounts
    did not report. Last-known and unreported counts stay out of the sum; an
    offer whose count is not reported adds nothing and is counted apart."""
    available = 0
    known = unknown = last_known = 0
    uncounted = 0
    for (hid, sid), row in sorted(_targets(payload).items()):
        if hid != harness:
            continue
        facet = facet_view(row.get("resets"), "resets", kept, now)
        if facet["state"] == "not_reported":
            unknown += 1
            continue
        if facet["state"] == "last_known":
            last_known += 1
            continue
        known += 1
        for offer in facet["rows"] or []:
            if offer.get("state") == "last_known":
                uncounted += 1
                continue
            if offer["available_count"] is None or offer["usable_now"] is None:
                uncounted += 1
            elif offer["usable_now"] is True:
                available += offer["available_count"]
    return {"available": available, "accounts_current": known, "accounts_last_known": last_known,
            "accounts_not_reported": unknown, "offers_uncounted": uncounted}


def family_resources(payload: Any, kept: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
    """Anonymous resource rows, grouped but NEVER summed across accounts,
    currencies or scopes. No quota window is needed (Cursor is resource-only).
    Keep each facet's observation/source/freshness, including absent facets."""
    out: Dict[str, Any] = {}
    for (hid, _sid), raw in sorted(_targets(payload).items()):
        family = out.setdefault(hid, {"accounts": 0, "facets": {f: [] for f in FACETS}})
        family["accounts"] += 1
        for facet in FACETS:
            family["facets"][facet].append(facet_view(raw.get(facet), facet, kept, now))
    return out
