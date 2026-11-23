"""The context snapshot: compact published tail text plus optional full detail.

The collector (the skill's one supervised task; no new timer, no model call)
writes ``context-snapshot.json`` into its own state directory after a
successful sweep. The host shows the compact ``text`` (``brief_text``) at
eligible context tails, while the optional top-level ``details`` JSON field
keeps the fuller ``snapshot_text`` layout when it fits inside the host's
32,768-byte UTF-8 envelope. Hosts that expose its ``query_data`` reader
return the whole dated envelope; an expired file is not a current reading.
Both views use the same measured facts as ``quota_summary``; this module
formats and dates them, without measuring more quota.

Wire (one atomic, complete JSON object): ``schema_version`` 1, ``observed_at``
(UTC ISO; not refreshed for an unchanged snapshot unless validity nears its
end), ``valid_until`` (UTC ISO), ``text`` (compact, <= BRIEF_MAX_CHARS, O(1)
in account count) and optional ``details`` (full detail <= MAX_CHARS, omitted
only if the remaining byte budget cannot hold even an omission notice).

Rules this module keeps:

* both views are pure functions of the tool answer: no per-account observation
  time of a fresh reading (the envelope dates the file), no relative age
  ("5 min ago"), no clock; identical current resource facts fold into one
  counted cohort in ``details``;
* ``brief_text`` stays short and O(1) in account count: per window, specific
  family/window, explicit scope (never presenting a model/prefix/kind/unreadable
  limit as universal), current measured reserve with denominator coverage (never
  mixing measured 0 and missing data, noting unknown applicability), a marker
  for restriction flags and the measured subtotal without flags, trailing-hour
  watched pace and its coverage, a pointer to resource details without inferring
  availability, the origin of the account list once, and a short pointer to
  ``details`` and ``quota_summary``;
  joint metrics, per-flag counts, resets and resources live in ``details`` and
  ``quota_summary``;
* a reported instant prints absolute UTC to the nearest second (sub-second
  jitter is not a change; validity keeps the exact instant); a last-known
  resource keeps its own dated observation in ``details``, never "now";
* derived figures (pace, capacity, conditional hours) print at one decimal
  (positive values below 0.05 print ``<0.1``); measured reserve keeps two;
* when ``text`` or ``details`` would exceed its character bound or the host's
  complete-file bound (MAX_FILE_BYTES, UTF-8 with JSON escaping included),
  whole lower-priority lines drop with an explicit omission note; no line is
  cut mid-fact;
* ``publish()`` rewrites atomically whenever ``text`` OR ``details`` changes
  (or validity nears expiry / tightens), so updated detail reaches
  ``query_data`` even when the compact ``text`` is unchanged;
* ``valid_until`` is at most VALID_FOR_SEC after the read, bounded by resets
  of measured windows, the carry bounds of counted last-known windows, known
  ends of the holds on measured windows, and current resource/grant boundaries.
  It describes freshness, never authority.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from . import quota_resources as qr
    from . import quota_summary as qs
except ImportError:
    import quota_resources as qr
    import quota_summary as qs

SCHEMA_VERSION = 1
SNAPSHOT_FILE = "context-snapshot.json"
MAX_CHARS = 12000  # detailed snapshot layout (`snapshot_text` / optional JSON `details` field)
BRIEF_MAX_CHARS = 900  # compact published tail text (`brief_text` / JSON `text` field)
VALID_FOR_SEC = 420.0  # SIGHTING_GAP_SEC: three missed sweeps plus one status timeout
RENEW_MARGIN_SEC = 180.0  # an unchanged snapshot is renewed once its validity is this close to ending
# The host reads at most this many bytes of the whole file and reports a larger one as oversized
# (ouroboros/skill_context_snapshot.py MAX_SNAPSHOT_BYTES): the bound of the complete UTF-8 envelope.
MAX_FILE_BYTES = 32768
# The widest date the envelope can carry (iso_exact: microseconds and a Z). Both texts are measured
# with dates this wide, so the file published later, with real dates, is never larger than measured.
WIDEST_DATE = "0000-00-00T00:00:00.000000Z"

HEADER = (
    "Claudexor quota snapshot — measured reserve of the authorized accounts; advisory, not a dispatch "
    "guarantee, no pins changed. Unit: account-windows (one account's share left of one limit = 1; "
    "limits of different length or scope are never added)."
)
BRIEF_HEADER = (
    "Claudexor quotas (advisory; 1 aw = one account's full quota window; "
    "1h watched pace; never sum windows/scopes):"
)
DETAIL_READER = (
    'query_data(op="json",kind="skill_state",ids=["claudexor_quotas"],'
    'state_file="context_snapshot")'
)
BRIEF_FOOTER = f"Details: {DETAIL_READER} or quota_summary."
EMPTY_NOTE = "No quota windows or account resources represented in this summary."

# The display bounds of provider text in one fact. A text field is shown whole up to MAX_FIELD characters, then
# bounded and said so. An amount is never printed in part (a cut number reads as another number): up to MAX_AMOUNT
# characters it is exact, else said not shown. The omission note names up to MAX_NAMED families and counts the rest.
# quota_summary retains every value whole.
MAX_FIELD = 160
MAX_AMOUNT = 40
MAX_NAMED = 8
BRIEF_MAX_NAMED = 4


def _breaks(text: str) -> bool:
    """Whether a text holds a character that ends a line or controls a terminal (C0, DEL, C1, the Unicode line and
    paragraph separators): such a text would turn one fact into several lines."""
    return any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f or c in "  " for c in text)


def _escaped(text: str) -> str:
    """A text quoted, its backslashes, quotes and control characters escaped as JSON writes them (the separators
    \\u2028/\\u2029 and DEL/C1 too): one line, nothing of it lost."""
    out = []
    for c in text:
        if c in '\\"':
            out.append("\\" + c)
        elif c in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[c])
        elif ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f or c in "  ":
            out.append(f"\\u{ord(c):04x}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def _field(value: Any, limit: int = MAX_FIELD) -> str:
    """A provider's text as one snapshot fact holds it: as sent when it is one line within ``limit`` characters; a
    line break or another control character in it is written escaped (the value quoted), so it never starts a line of
    its own; a longer text is shown in part and says how many characters it leaves out."""
    text = str(value)
    cut = max(0, len(text) - limit)
    shown = text[:limit] if cut else text
    if _breaks(shown) or any(c in shown for c in '\\"'):
        shown = _escaped(shown)
    return shown + (f"… (+{cut} characters; quota_summary has it whole)" if cut else "")


def _brief_field(value: Any, limit: int = 32) -> str:
    """Short single-line provider text for ``brief_text``: quotes controls and discloses any cut."""
    text = str(value)
    cut = max(0, len(text) - limit)
    shown = text[:limit] if cut else text
    if _breaks(shown) or any(c in shown for c in '\\"'):
        shown = _escaped(shown)
    return shown + (f"…(+{cut})" if cut else "")


def _amount(raw: Any) -> str:
    """A reported amount (a string, as the engine sends it): exact up to MAX_AMOUNT characters (escaped if it holds a
    control character), else said not shown with its length — never cut into another number."""
    if isinstance(raw, str) and len(raw) > MAX_AMOUNT:
        return f"amount not shown ({len(raw)} characters; quota_summary has it)"
    return _field(raw, MAX_AMOUNT)


def _rate(value: float) -> str:
    """A derived figure at one decimal; a positive value below that resolution is not 0."""
    return qs.amount_words(value, 1)


def _pace_rate(pace: Dict[str, Any]) -> str:
    """The measured pace as it is printed.

    The tool sums the per-account rates and rounds that sum before it reaches
    this module, so a sum that is positive but below the sum's own resolution
    arrives as ``0.0``. It is not a confirmed zero: ``zero_growth`` counts the
    measured accounts whose own counter did not move, and fewer of them than
    ``accounts_known`` means at least one account did grow. Such a sum prints
    ``<0.1``; a confirmed zero (every measured account without growth) and an
    unknown pace keep their own words.
    """
    value = float(pace.get("windows_per_hour"))
    if value == 0.0:
        known = pace.get("accounts_known")
        zero = pace.get("zero_growth") or 0
        if isinstance(known, int) and not isinstance(known, bool) and zero < known:
            return "<0.1"
    return _rate(value)


def rounded_hours(value: Any) -> str:
    """Conditional hours at the derived resolution: positive sub-hour work is never 0."""
    return "—" if value is None else _rate(float(value))


def _second(value: Any) -> str:
    """A reported instant in the text, to the nearest second; unreadable text stays as reported (in one line)."""
    stamp = qs.parse_instant(value)
    return qs.iso(round(stamp)) if stamp is not None else _field(value)


def _listed(names: List[Any], omitted: int, suffix: str = "", *, brief: bool = False) -> str:
    """Names left out of this line and names already missing from the tool are different counts."""
    cap = 1 if brief else 3
    shown = ", ".join((_brief_field(n, 24) if brief else _field(n)) + suffix for n in names[:cap])
    notes = []
    if len(names) > cap:
        notes.append(f"+{len(names) - cap} retained in row")
    if omitted:
        notes.append(f"{omitted} outside summary")
    if shown and notes:
        return f"{shown} ({'; '.join(notes)})"
    return shown or "; ".join(notes)


def _scope_words(row: Dict[str, Any]) -> str:
    """The limit's whole scope in words, each field independent of the others (names, family prefixes, the
    unspecified-model flag, request kinds, what cannot be read), so two limits of one name and length that govern
    different requests never read as one."""
    words = ""
    if row.get("models") or row.get("models_omitted"):
        words += " models " + _listed(row.get("models") or [], row.get("models_omitted") or 0)
    if row.get("model_prefixes") or row.get("model_prefixes_omitted"):
        words += (" and model families " if words else " model families ") + (
            _listed(row.get("model_prefixes") or [], row.get("model_prefixes_omitted") or 0, "*")
        )
    if row.get("model_scope") == "model-scoped; model names unavailable from history":
        words += " model-scoped, names unavailable from history"
    if row.get("governs_unspecified_model"):
        words += ", also governs requests naming no model"
    unreadable = [str(f) for f in (row.get("scope_unreadable") or [])]
    kinds = row.get("request_kinds")
    if "request_kinds" in unreadable:
        # The kinds the window names could not be read in full: the readable
        # part is not the window's scope, and an empty list is not "none".
        words += " request kinds unreadable (applicability unknown)"
        unreadable = [f for f in unreadable if f != "request_kinds"]
    elif kinds is not None:
        words += (" " + " and ".join(_field(k) for k in kinds) + " requests only") if kinds else " no request kinds"
    if unreadable:
        words += (
            " scope unreadable: "
            + ", ".join(f.replace("_", " ") for f in unreadable)
            + " (applicability unknown)"
        )
    if row.get("request_scope"):
        words += " request scope " + str(row["request_scope"])
    return words


def _limit_name(row: Dict[str, Any]) -> str:
    return f"{_field(row.get('limit'))} ({row.get('duration')})" + _scope_words(row)


def limit_title(group: Dict[str, Any]) -> str:
    """Use the detailed text's window vocabulary to distinguish identical chip labels."""
    return _limit_name({"limit": group["label"], "duration": group["duration"], **qs.compact_scope(group)})


def _limit_line(row: Dict[str, Any]) -> str:
    accounts = row.get("of_accounts") or 0
    known = row.get("accounts_known_to_apply")
    name = _limit_name(row)
    if accounts:
        at_limit = row.get("accounts_at_limit")
        reserve = qs.measured_words(row.get("remaining_windows"), accounts, at_limit)
        average = qs.measured_words(row.get("average_remaining_pct"), accounts, at_limit, places=1)
        avg = average + "%" if average != "unknown" else average
        parts = [
            f"{name}: {reserve} left across {accounts} measured of "
            f"{known if known is not None else accounts} (avg {avg})"
        ]
    else:
        parts = [f"{name}: no current reading of any of {known if known is not None else '?'}"]
    if row.get("accounts_at_limit"):
        parts.append(f"{row['accounts_at_limit']} at the limit")
    last_known = row.get("last_known") or {}
    if last_known.get("accounts"):
        reserve = qs.measured_words(last_known.get("windows"), last_known["accounts"], row.get("_last_known_at_limit"))
        parts.append(
            f"last known {reserve} from {last_known['accounts']} not current "
            f"(oldest observed {last_known.get('oldest_observed_at')})"
        )
    if row.get("unknown_accounts"):
        parts.append(f"{row['unknown_accounts']} unknown")
    if row.get("applicability_unknown_accounts"):
        parts.append(f"{row['applicability_unknown_accounts']} other accounts: applicability unknown")
    reset = (row.get("next_reset") or {}).get("at")
    if reset:
        parts.append(f"next reported reset {_second(reset)} ({(row['next_reset'] or {}).get('accounts')} accounts)")
    restrictions = row.get("restrictions") or {}
    holds = [f"{k} {(v or {}).get('accounts')}" for k, v in sorted(restrictions.items())
             if k != "account_state_unknown"]
    if holds:
        parts.append("held: " + ", ".join(holds))
    if note := qs.unflagged_note(row, unit="account-windows"):
        parts.append(note)
    pace = row.get("recent_pace") or {}
    if pace.get("windows_per_hour") is not None:
        parts.append(
            f"measured pace {_pace_rate(pace)} account-windows/h "
            f"({pace.get('accounts_known')} of {pace.get('of')}; {pace.get('state')}; "
            "trailing-hour watched observations; exact spans in quota_summary)"
        )
    else:
        parts.append(f"measured pace unknown ({pace.get('accounts_known', 0)} of {pace.get('of')})")
    return "; ".join(parts)


def _joint_line(joint: Dict[str, Any]) -> str:
    if not joint.get("available"):
        return ""
    scope = joint.get("scope") or {}
    capacity = joint.get("same_account_capacity")
    amount = _approx_words(capacity)
    if capacity == 0:
        amount += " at tool precision"
    words = (f"same-account capacity ({', '.join(_field(w) for w in joint.get('windows') or [])}; "
             f"{'unspecified-model request' if not scope.get('model') else 'model ' + _field(scope['model'])}, "
             f"{_field(scope.get('request_kind') or 'text')}; unit: shares of the {_field(joint.get('unit'))}): "
             f"{amount} from {joint.get('accounts_counted')} of "
             f"{joint.get('accounts')} accounts")
    left = joint.get("left_out") or {}
    if left:
        words += "; left out " + ", ".join(f"{k} {v}" for k, v in sorted(left.items()))
    if joint.get("conditional_account_hours") is not None:
        words += (f"; conditional {_approx_words(joint['conditional_account_hours'])} account-hours if each "
                  f"account's common-watched-period pace holds (up to 7 days; "
                  f"{joint.get('hours_known_accounts')} known; "
                  f"{joint.get('hours_unknown_accounts', 0)} unknown; not a task duration)")
    return words


def _approx_words(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    shown = rounded_hours(value)
    return shown if shown.startswith("<") else f"≈{shown}"


def _resource_lines(row: Dict[str, Any]) -> List[str]:
    """Every anonymous facet separately; amounts of unlike accounts/scopes are
    never added. Last-known rows keep their observation date, including in a
    mixed facet. Current rows use the envelope date and may fold into cohorts."""
    def quantity(item: Dict[str, Any], field: str) -> str:
        # The established monetary contract is integer minor units + scale.
        # Scale by string placement, never float arithmetic. Other units stay
        # raw (cents are not silently relabelled as whole USD). Every text is
        # one line (_field); an amount is exact or said not shown (_amount).
        unit, currency = item.get("unit") or "", item.get("currency") or ""
        raw, places = item.get(field), item.get("decimal_places")
        digits = raw.lstrip("+-") if isinstance(raw, str) else ""
        if (unit == "minor" and isinstance(places, int) and not isinstance(places, bool)
                and 0 <= places <= 12 and digits and raw in (digits, '+' + digits, '-' + digits)
                and all('0' <= c <= '9' for c in digits) and len(raw) <= MAX_AMOUNT):
            padded = digits.zfill(places + 1)
            number = (padded[:-places] + "." + padded[-places:]) if places else padded
            return ("-" if raw.startswith("-") else "") + number + " " + _field(currency or unit)
        value = _amount(raw) if raw is not None else "unknown"
        if unit.casefold() == currency.casefold():
            return f"{value} {_field(currency or unit)}"
        return f"{value} {_field(unit)}" + (f" [{_field(currency)}]" if currency else "") + (
            f" (decimal_places {places})" if unit == "minor" else "")

    lines = []
    for facet, observations in (row.get("facets") or {}).items():
        for observation in observations:
            state = observation.get("state")
            if state == "not_reported":
                lines.append(f"{facet} not reported")
                continue
            sections = {}
            for item in observation.get("rows") or []:
                old = state != "current" or item.get("state") == "last_known"
                values = sections.setdefault(old, [])
                label = _field(item.get("label") or item.get("id") or facet)
                note = _field(item.get("note") or "no availability inferred")
                if facet == "balances":
                    values.append(f"{label} {quantity(item, 'amount')} "
                                  f"({item.get('scope') or 'scope unknown'}, {item.get('usage_kind') or 'kind unknown'}; "
                                  f"{note})")
                elif facet == "spending":
                    values.append(f"{label} {quantity(item, 'used')}/{quantity(item, 'limit')} "
                                  f"({item.get('scope') or 'scope unknown'}; {note})")
                elif facet == "resets":
                    usable = {True: "yes", False: "no", None: "unknown"}.get(item.get("usable_now"), "unknown")
                    usable += " when read" if old else ""
                    count = item.get('available_count')
                    values.append(f"{label} count {count if count is not None else 'unknown'} usable {usable} "
                                  f"reset {_second(item['resets_at']) if item.get('resets_at') else 'not reported'} "
                                  "(information only)")
                else:
                    values.append(_field(item.get("code") or "diagnostic"))
                if facet in ("balances", "spending"):
                    until = (item.get("period") or {}).get("ends_at") or item.get("resets_at")
                    if until:
                        values[-1] += f"; reported period/reset ends {_second(until)}"
            omitted = observation.get("omitted") or 0
            for old, values in (sections or {state != "current": []}).items():
                dated = (f"last known observed {_field(observation.get('observed_at') or 'time not reported')}"
                         if old else "current")
                lines.append(f"{facet}: {', '.join(values) if values else 'none reported'}; {dated}")
            if omitted:
                lines[-1] += f"; {omitted} resource rows omitted"
    # Repeated identical resource facts are a counted cohort, not dozens of
    # repeated paragraphs. This never adds amounts across scopes/accounts.
    return [line + (f" ({count} accounts)" if count > 1 else "")
            for line, count in Counter(lines).items()]


def _families(answer: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, str], Dict[str, Any]]:
    rows = list(answer.get("groups") or [])
    families: List[str] = []
    harness_of: Dict[str, str] = {}
    for row in rows:
        hid = str(row.get("key", "")).split("|")[0]
        family = str(row.get("family") or hid)
        if family not in families:
            families.append(family)
        harness_of.setdefault(family, hid)
    resources = answer.get("resources") or {}
    for hid in resources:
        if hid not in harness_of.values() and hid not in families:
            families.append(hid)
            harness_of[hid] = hid
    return rows, families, harness_of, resources


def serialize(body: Dict[str, Any]) -> bytes:
    """The file's bytes exactly as publish() writes them: compact JSON, UTF-8, no ASCII escaping."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def envelope_bytes(text: str, details: Optional[str] = None) -> int:
    """The size of the complete file publish() would write for ``text`` (and optional ``details``),
    its two dates at their widest (WIDEST_DATE): what the host's byte bound (MAX_FILE_BYTES) is
    checked against. A real publication, with its real dates, is never larger."""
    body: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": WIDEST_DATE,
        "valid_until": WIDEST_DATE,
        "text": text,
    }
    if details:
        body["details"] = details
    return len(serialize(body))


def _brief_scope(row: Dict[str, Any]) -> str:
    """Concise scope marker so a partial/model/prefix/kind/unreadable limit never reads as universal."""
    parts: List[str] = []
    models = row.get("models") or []
    models_omitted = row.get("models_omitted") or 0
    if models or models_omitted:
        parts.append(("model " if models else "models ") + _listed(models, models_omitted, brief=True))
    prefixes = row.get("model_prefixes") or []
    prefixes_omitted = row.get("model_prefixes_omitted") or 0
    if prefixes or prefixes_omitted:
        parts.append(("prefix " if prefixes else "prefixes ") + _listed(prefixes, prefixes_omitted, "*", brief=True))
    if row.get("model_scope") == "model-scoped; model names unavailable from history":
        parts.append("model-scoped, names unavailable")
    if row.get("governs_unspecified_model"):
        parts.append("+unspecified")
    unreadable = [str(f) for f in (row.get("scope_unreadable") or [])]
    kinds = row.get("request_kinds")
    if "request_kinds" in unreadable:
        # The kinds could not be read in full: the readable part is not the
        # window's scope, and an empty list is not "no request kinds".
        parts.append("kinds applicability unknown")
        unreadable = [f for f in unreadable if f != "request_kinds"]
    elif kinds is not None:
        parts.append(("kinds " + "/".join(_brief_field(k, 16) for k in kinds)) if kinds else "no request kinds")
    if unreadable:
        parts.append("scope unreadable, see details")
    if row.get("request_scope"):
        parts.append("request scope unknown, see details")
    return (" [" + "; ".join(parts) + "]") if parts else ""


def _brief_limit_line(family: str, row: Dict[str, Any]) -> str:
    """One compact factual line: family/window, scope, measured reserve + denominator coverage, 1h pace + coverage."""
    accounts = row.get("of_accounts") or 0
    known = row.get("accounts_known_to_apply")
    denom = known if known is not None else (accounts if accounts else "?")
    head = (
        f"{_brief_field(family, 28)} {_brief_field(row.get('limit'), 32)} "
        f"({_brief_field(row.get('duration'), 20)}){_brief_scope(row)}"
    )
    cov: List[str] = [f"{accounts}/{denom} measured"]
    if (row.get("restrictions") or {}).keys() - {"account_state_unknown"}:
        # Per-flag counts overlap; they are not a count of distinct accounts.
        cov.append("restriction flags")
    if note := qs.unflagged_note(row):
        cov.append(note)
    if row.get("accounts_at_limit"):
        cov.append(f"{row['accounts_at_limit']} at limit")
    last_known = row.get("last_known") or {}
    if last_known.get("accounts"):
        cov.append(f"{last_known['accounts']} last-known")
    if row.get("unknown_accounts"):
        cov.append(f"{row['unknown_accounts']} unknown")
    if row.get("applicability_unknown_accounts"):
        cov.append(f"{row['applicability_unknown_accounts']} applicability unknown")
    if not accounts:
        return f"{head}: no current reading ({', '.join(cov)})"
    pace = row.get("recent_pace") or {}
    pace_of = pace.get("of") if pace.get("of") is not None else accounts
    if pace.get("windows_per_hour") is not None:
        pace_str = f"pace {_pace_rate(pace)} aw/h ({pace.get('accounts_known', 0)}/{pace_of})"
    else:
        pace_str = f"pace unknown ({pace.get('accounts_known', 0)}/{pace_of})"
    reserve = qs.measured_words(row.get("remaining_windows"), accounts, row.get("accounts_at_limit"))
    return f"{head}: {reserve} aw ({', '.join(cov)}); {pace_str}"


def _brief_footer(answer: Dict[str, Any]) -> str:
    omitted_rows = answer.get("resource_rows_omitted")
    if omitted_rows:
        return f"Details ({omitted_rows} resource rows omitted at input bound): {DETAIL_READER} or quota_summary."
    return BRIEF_FOOTER


def _roster_note(answer: Dict[str, Any]) -> str:
    """Where the list of accounts came from, said once per summary.

    A list that is not this answer's own (kept from an earlier read, or not
    read at all) is named here, so a measured fraction is never read as the
    whole list of accounts. The tool's own answer carries the same state.
    """
    roster = answer.get("roster")
    if roster == "cached":
        return "Account list: kept from an earlier read (cached)."
    if roster == "unknown":
        return "Account list: unknown; counts cover this summary only."
    return ""


def brief_text(answer: Dict[str, Any], bound: int = BRIEF_MAX_CHARS, byte_bound: int = MAX_FILE_BYTES) -> str:
    """Compact factual tail note for automatic publication in ``context-snapshot.json["text"]``.

    Stays within ``bound`` characters and ``byte_bound`` UTF-8 envelope bytes,
    independent of account count. Prioritizes fresh measured family
    representation ahead of secondary, scoped, and unmeasured windows, using
    single-pass greedy packing (no quadratic drop loop).
    """
    rows, families, _harness_of, _resources = _families(answer)
    # (tier, family_idx, line_idx, family, line)
    items: List[Tuple[int, int, int, str, str]] = []
    for bi, family in enumerate(families):
        mine = [r for r in rows if str(r.get("family") or str(r.get("key", "")).split("|")[0]) == family]
        if not mine:
            # The family may hold a window this answer does not carry (the
            # tool's own group bound can cut one): say what is shown, not
            # that no window exists.
            items.append((1, bi, 0, family,
                          f"{_brief_field(family, 28)}: no window in this summary; "
                          "resources: see quota_summary (availability not inferred)"))
            continue
        rep = next((i for i, r in enumerate(mine) if r.get("tightest_in_family") and (r.get("of_accounts") or 0) > 0),
                   next((i for i, r in enumerate(mine) if (r.get("of_accounts") or 0) > 0), None))
        for li, row in enumerate(mine):
            measured = (row.get("of_accounts") or 0) > 0
            scoped = bool(row.get("models") or row.get("model_prefixes") or row.get("request_kinds") is not None
                          or row.get("scope_unreadable") or row.get("request_scope")
                          or row.get("model_scope") == "model-scoped; model names unavailable from history")
            if li == rep:
                tier = 0
            elif measured and not scoped:
                tier = 1
            elif measured:
                tier = 2
            else:
                tier = 2 if (rep is None and li == 0) else 3
            items.append((tier, bi, li, family, _brief_limit_line(family, row)))

    footer = _brief_footer(answer)

    def render(kept: List[Tuple[int, int, int, str, str]], omitted: List[Tuple[int, int, int, str, str]]) -> str:
        parts = [BRIEF_HEADER]
        roster_note = _roster_note(answer)
        if roster_note:
            parts.append(roster_note)
        if not items:
            parts.append(EMPTY_NOTE)
        else:
            for _tier, _bi, _li, _fam, line in sorted(kept, key=lambda x: (x[1], x[2])):
                parts.append(line)
        if omitted:
            omitted_fams = sorted({x[3] for x in omitted})
            named = ", ".join(_brief_field(name, 24) for name in omitted_fams[:BRIEF_MAX_NAMED])
            more = len(omitted_fams) - BRIEF_MAX_NAMED
            parts.append(
                f"({len(omitted)} line(s) left out for {named}"
                + (f" and {more} more families" if more > 0 else "")
                + "; see details and quota_summary)"
            )
        parts.append(footer)
        return "\n".join(parts)

    def fits(candidate: str) -> bool:
        return len(candidate) <= bound and envelope_bytes(candidate) <= byte_bound

    if len(items) <= 12:
        whole = render(items, [])
        if fits(whole):
            return whole

    ordered = sorted(items, key=lambda x: (x[0], x[1], x[2]))
    kept: List[Tuple[int, int, int, str, str]] = []
    best: Optional[str] = None
    empty_cand = render([], ordered)
    if fits(empty_cand):
        best = empty_cand
    for idx, item in enumerate(ordered):
        trial = kept + [item]
        cand = render(trial, ordered[idx + 1:])
        if fits(cand):
            kept = trial
            best = cand
        else:
            break
    if best is not None:
        return best
    for fallback in (BRIEF_HEADER + "\n(all lines left out; see details/quota_summary)",
                     "Snapshot omitted; see quota_summary."):
        if fits(fallback):
            return fallback
    raise ValueError("snapshot bounds cannot hold an omission notice")


def snapshot_text(answer: Dict[str, Any], bound: int = MAX_CHARS, byte_bound: int = MAX_FILE_BYTES,
                  *, reserved_text: str = "") -> Optional[str]:
    """Detailed deterministic snapshot text from the model tool's answer dictionary.

    Used directly by existing detailed-snapshot consumers and stored in the
    optional ``details`` JSON field alongside ``brief_text``. When
    ``reserved_text`` is provided, checks the complete envelope carrying both
    ``reserved_text`` and the returned detail against ``byte_bound``, returning
    ``None`` only when ``reserved_text`` leaves no room for an omission notice.
    """
    rows, families, harness_of, resources = _families(answer)
    blocks: List[Tuple[str, List[Tuple[int, str]]]] = []
    for family in families:
        lines: List[Tuple[int, str]] = []
        mine = [r for r in rows if str(r.get("family") or str(r.get("key", "")).split("|")[0]) == family]
        harness = harness_of[family]
        if not mine:
            lines.append((0, "  no quota window in this summary; resource data does not imply availability"))
        for row in mine:
            lines.append((
                0 if row.get("tightest_in_family") else 2 if row.get("models") or row.get("model_prefixes") else 1,
                "  " + _limit_line(row) + (" [lowest left]" if row.get("tightest_in_family") else ""),
            ))
        joint = _joint_line((answer.get("joint") or {}).get(harness) or {})
        if joint:
            lines.append((0, "  " + joint))
        offers = (answer.get("manual_reset_offers") or {}).get(harness)
        if offers:
            lines.append((
                1,
                f"  manual reset offers reported usable: {offers.get('available')} "
                + ("(partial subtotal) " if offers.get("offers_uncounted") else "")
                + f"(accounts: {offers.get('accounts_current')} current, "
                f"{offers.get('accounts_last_known')} last known, "
                f"{offers.get('accounts_not_reported')} not reported; "
                f"{offers.get('offers_uncounted', 0)} offers uncounted); information only, never spent here",
            ))
        lines.extend((1, "  " + line) for line in _resource_lines(resources.get(harness) or {}))
        blocks.append((family, lines))
    flat = [(bi, li, pri, line) for bi, (_f, lines) in enumerate(blocks) for li, (pri, line) in enumerate(lines)]
    echo = answer.get("availability_scope") or {}
    scope_line = (
        f"Engine availability basis {_field(echo.get('basis'))}; "
        f"{_field(echo.get('model')) if echo.get('model') else 'unspecified-model request'}, "
        f"{_field(echo.get('request_kind'))}"
        if echo.get("stated") else "Engine request scope not stated"
    )

    def render(drop: set) -> str:
        parts = [HEADER, scope_line]
        roster_note = _roster_note(answer)
        if roster_note:
            parts.append(roster_note)
        if not blocks:
            parts.append(EMPTY_NOTE)
        if answer.get("resource_rows_omitted"):
            parts.append(f"Resource input: {answer['resource_rows_omitted']} rows omitted at the input bound")
        for bi, (family, lines) in enumerate(blocks):
            body = [line for li, (_p, line) in enumerate(lines) if (bi, li) not in drop]
            if body:
                parts.append(_field(family))
                parts.extend(body)
        if drop:
            omitted = sorted({blocks[bi][0] for bi, _li in drop})
            named = ", ".join(_field(name) for name in omitted[:MAX_NAMED])
            more = len(omitted) - MAX_NAMED
            parts.append(
                f"({len(drop)} line(s) left out for {named}"
                + (f" and {more} more families" if more > 0 else "")
                + "; the quota_summary answer lists the windows it carries)"
            )
        return "\n".join(parts)

    def fits(candidate: str) -> bool:
        # The character bound is checked first: a candidate that is already too
        # long is never serialized (the envelope bytes cost more than the
        # length test, and the drop loop tries many candidates).
        if len(candidate) > bound:
            return False
        size = envelope_bytes(reserved_text, candidate) if reserved_text else envelope_bytes(candidate)
        return size <= byte_bound

    drop: set = set()
    text = render(drop)
    for bi, li, _pri, _t in sorted(flat, key=lambda x: (-x[2], -x[0], -x[1])):
        if fits(text):
            break
        drop.add((bi, li))
        text = render(drop)
    if not fits(text):
        text = HEADER + "\n(every line left out; the quota_summary answer lists the windows it carries)"
        if not fits(text):
            text = "Snapshot details omitted; the quota_summary answer lists the windows it carries."
        if not fits(text):
            if reserved_text:
                return None
            raise ValueError("snapshot bounds cannot hold an omission notice")
    return text


def earliest_reset(answer: Dict[str, Any]) -> Optional[str]:
    resets = [
        str((r.get("next_reset") or {}).get("at")) for r in answer.get("groups") or []
        if (r.get("next_reset") or {}).get("at")
    ]
    # Current monetary/resource facts have cycle boundaries too, and a reset
    # offer's usable grants their expiry (quota_resources.boundaries).
    # Resource-only families and `details` must not remain fresh past them merely
    # because no quota-window group exists. Last-known facets stay dated.
    for family in (answer.get("resources") or {}).values():
        for observations in (family.get("facets") or {}).values():
            for observation in observations:
                if observation.get("state") != "current":
                    continue
                for row in observation.get("rows") or []:
                    if row.get("state") == "last_known":
                        continue
                    resets.extend(qr.boundaries(row))
    # ISO spellings with different offsets are not lexically time-ordered.
    valid = [(qs.parse_instant(value), value) for value in resets]
    valid = [(stamp, value) for stamp, value in valid if stamp is not None]
    return min(valid, key=lambda pair: pair[0])[1] if valid else None


def envelope(text: str, read_at: float, reset_at: Optional[float], iso,
             details: Optional[str] = None) -> Dict[str, Any]:
    """The file's content: observation and validity from the collector's own
    semantics (see the module note)."""
    if details is not None and not isinstance(details, str):
        raise TypeError("details must be a string or None")
    valid = read_at + VALID_FOR_SEC
    if reset_at is not None:
        if reset_at <= read_at:
            raise ValueError("a passed reset cannot renew a fresh snapshot")
        valid = min(valid, reset_at)
    body: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": iso(read_at),
        "valid_until": iso(valid),
        "text": text,
    }
    if details:
        body["details"] = details
    return body


def read_snapshot(path: Path) -> Optional[Dict[str, Any]]:
    """The published file, or None: absent, unreadable, or larger than the host
    reads (MAX_FILE_BYTES, read one byte past it as the host does). An oversized
    file is no previous snapshot: the next sweep writes one the host can read."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def publish(state_dir: Path, text: str, read_at: float, reset_at: Optional[float], iso, parse,
            control=None, *, details: Optional[str] = None) -> str:
    """Write the snapshot atomically, or leave it. Rewrite if ``text`` or
    ``details`` changes, validity nears expiry, or a boundary tightens it. Otherwise
    a rewrite would only churn ``observed_at``. Return ``written`` | ``unchanged`` |
    ``skipped``. A failed or oversized write leaves the last file to expire."""
    path = state_dir / SNAPSHOT_FILE
    if reset_at is not None and reset_at <= read_at:
        return "skipped"
    body = envelope(text, read_at, reset_at, iso, details=details)
    data = serialize(body)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"context snapshot is {len(data)} bytes, over the host's {MAX_FILE_BYTES}-byte bound")
    previous = read_snapshot(path)
    same_details = (
        ("details" in previous) == ("details" in body) and previous.get("details") == body.get("details")
        if previous else False
    )
    if previous and previous.get("text") == text and same_details and previous.get("schema_version") == SCHEMA_VERSION:
        until = parse(previous.get("valid_until"))
        if (until is not None and until - read_at > RENEW_MARGIN_SEC
                and (reset_at is None or until <= reset_at)):
            return "unchanged"
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=SNAPSHOT_FILE + ".", suffix=".tmp", dir=str(state_dir))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if control is not None:
            control.admit()
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise
    return "written"
