"""The context snapshot: one deterministic, complete text of the same numbers.

The collector (the skill's one supervised task; no new timer, no model call)
writes ``context-snapshot.json`` into its own state directory after a
successful sweep, and the host offers its ``text`` to the acting mind's
future requests. The text is built from exactly the dictionary the model
tool returns — the same functions, the same figures — so the widget, the
tool and the snapshot cannot disagree; nothing is computed here.

Wire (one atomic, complete JSON object): ``schema_version`` 1, ``observed_at``
(when the status was read: UTC ISO, never refreshed for an unchanged text
unless the previous validity is about to end), ``valid_until`` (UTC ISO) and
``text``.

Rules this module keeps:

* the text is a pure function of the figures: no per-account observation
  time of a fresh reading (the envelope dates the text), no relative age
  ("5 min ago"), no clock — a refresh that changed nothing writes nothing new
  and the host dedupes it; identical current facts fold into one counted line;
* a reported instant is printed absolute, UTC, to the nearest second (the
  engine's sub-second jitter is not a change; validity keeps the exact one);
  a last-known value carries its own fixed dated observation, never "now";
* derived figures (pace, capacity, conditional hours) are printed at one
  decimal, the resolution of their calibration; measured reserve keeps two;
* unknown stays unknown: a window nobody reads is "no current reading",
  never 0; an account left out of a joint figure is counted, with why;
* when the text would exceed the bound, whole lower-priority lines are
  left out and the omission is said — a line is never cut in the middle;
* ``valid_until`` follows the collector's own semantics: a status read is
  vouched for for as long as the sweep gap tolerates (three missed sweeps
  plus one status timeout), and never past the earliest reported reset the
  text depends on. It describes the snapshot's freshness, never authority.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from . import quota_summary as qs
except ImportError:
    import quota_summary as qs

SCHEMA_VERSION = 1
SNAPSHOT_FILE = "context-snapshot.json"
MAX_CHARS = 12000  # compact full-family default; never silently erase a resource-only family
VALID_FOR_SEC = 420.0  # SIGHTING_GAP_SEC: three missed sweeps plus one status timeout
RENEW_MARGIN_SEC = 180.0  # an unchanged text is renewed once its validity is this close to ending
MAX_FILE_BYTES = 64 * 1024

HEADER = ("Claudexor quota snapshot — measured reserve of the authorized accounts; advisory, not a dispatch "
          "guarantee, no pins changed. Unit: account-windows (one account's share left of one limit = 1; "
          "limits of different length or scope are never added).")


def _num(value: Any) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def _derived(value: Any, places: int = 1) -> str:
    """A figure derived from a calibration (the same-account capacity, the
    conditional hours): printed at the resolution of the calibration, so a
    sweep that learns a little more about the ratio does not rewrite a text
    whose measured numbers did not change. The tool and the widget carry
    the unrounded value; this is the same value, rounded, and says so."""
    return "—" if value is None else f"{round(float(value), places):.{places}f}"


def _rate(value: float) -> str:
    """A derived figure at one decimal; a positive value below that resolution is not 0."""
    return "<0.1" if 0 < value < .05 else _derived(value)


def rounded_hours(value: Any) -> str:
    """Conditional hours at the derived resolution: positive sub-hour work is never 0."""
    return "—" if value is None else _rate(float(value))


def _second(value: Any) -> str:
    """A reported instant in the text, to the nearest second; unreadable text stays as reported."""
    stamp = qs.parse_instant(value)
    return qs.iso(round(stamp)) if stamp is not None else str(value)


def _limit_line(row: Dict[str, Any]) -> str:
    accounts = row.get("of_accounts") or 0
    known = row.get("accounts_known_to_apply")
    name = f"{row.get('limit')} ({row.get('duration')})"
    if row.get("models"):
        name += " models " + ", ".join(row["models"][:3]) + ("…" if len(row["models"]) > 3 else "")
    elif row.get("model_prefixes"):
        name += " model families " + ", ".join(p + "*" for p in row["model_prefixes"][:3])
    if accounts:
        parts = [f"{name}: {_num(row.get('remaining_windows'))} left across {accounts} measured of "
                 f"{known if known is not None else accounts} (avg {row.get('average_remaining_pct')}%)"]
    else:
        parts = [f"{name}: no current reading of any of {known if known is not None else '?'}"]
    if row.get("accounts_at_limit"):
        parts.append(f"{row['accounts_at_limit']} at the limit")
    last_known = row.get("last_known") or {}
    if last_known.get("accounts"):
        parts.append(f"last known {_num(last_known.get('windows'))} from {last_known['accounts']} not current "
                     f"(oldest observed {last_known.get('oldest_observed_at')})")
    if row.get("unknown_accounts"):
        parts.append(f"{row['unknown_accounts']} unknown")
    if row.get("applicability_unknown_accounts"):
        parts.append(f"{row['applicability_unknown_accounts']} other accounts: applicability unknown")
    reset = (row.get("next_reset") or {}).get("at")
    if reset:
        parts.append(f"next reported reset {_second(reset)} ({(row['next_reset'] or {}).get('accounts')} accounts)")
    restrictions = row.get("restrictions") or {}
    if restrictions:
        parts.append("held: " + ", ".join(f"{k} {(v or {}).get('accounts')}" for k, v in sorted(restrictions.items())))
    pace = row.get("recent_pace") or {}
    if pace.get("windows_per_hour") is not None:
        parts.append(f"measured pace {_rate(float(pace['windows_per_hour']))} account-windows/h "
                     f"({pace.get('accounts_known')} of {pace.get('of')}; {pace.get('state')}; "
                     "trailing-hour watched observations; exact spans in quota_summary)")
    else:
        parts.append(f"measured pace unknown ({pace.get('accounts_known', 0)} of {pace.get('of')})")
    return "; ".join(parts)


def _joint_line(joint: Dict[str, Any]) -> str:
    if not joint.get("available"):
        return ""
    scope = joint.get("scope") or {}
    words = (f"same-account capacity ({', '.join(joint.get('windows') or [])}; "
             f"{'unspecified-model request' if not scope.get('model') else 'model ' + str(scope['model'])}, "
             f"{scope.get('request_kind') or 'text'}; unit: shares of the {joint.get('unit')}): "
             f"≈{_derived(joint.get('same_account_capacity'))} from {joint.get('accounts_counted')} of "
             f"{joint.get('accounts')} accounts")
    left = joint.get("left_out") or {}
    if left:
        words += "; left out " + ", ".join(f"{k} {v}" for k, v in sorted(left.items()))
    if joint.get("conditional_account_hours") is not None:
        words += (f"; conditional ≈{rounded_hours(joint['conditional_account_hours'])} account-hours if each "
                  f"account's common-watched-period pace holds (up to 7 days; "
                  f"{joint.get('hours_known_accounts')} known; "
                  f"{joint.get('hours_unknown_accounts', 0)} unknown; not a task duration)")
    return words


def _resource_lines(row: Dict[str, Any]) -> List[str]:
    """Every anonymous facet separately; amounts of unlike accounts/scopes are
    never added. A last-known facet keeps its observation date; a current one is
    dated by the snapshot envelope, so equal current facts fold into a cohort."""
    def quantity(item: Dict[str, Any], field: str) -> str:
        # The established monetary contract is integer minor units + scale.
        # Scale by string placement, never float arithmetic. Other units stay
        # raw (cents are not silently relabelled as whole USD).
        unit, currency = item.get("unit") or "", item.get("currency") or ""
        raw, places = item.get(field), item.get("decimal_places")
        digits = raw.lstrip("+-") if isinstance(raw, str) else ""
        if (unit == "minor" and isinstance(places, int) and not isinstance(places, bool)
                and 0 <= places <= 12 and digits and raw in (digits, '+' + digits, '-' + digits)
                and all('0' <= c <= '9' for c in digits)):
            padded = digits.zfill(places + 1)
            number = (padded[:-places] + "." + padded[-places:]) if places else padded
            return ("-" if raw.startswith("-") else "") + number + " " + (currency or unit)
        value = raw if raw is not None else "unknown"
        if unit.casefold() == currency.casefold():
            return f"{value} {currency or unit}"
        return f"{value} {unit}" + (f" [{currency}]" if currency else "") + (
            f" (decimal_places {places})" if unit == "minor" else "")

    lines = []
    for facet, observations in (row.get("facets") or {}).items():
        for observation in observations:
            state = observation.get("state")
            if state == "not_reported":
                lines.append(f"{facet} not reported")
                continue
            values = []
            for item in observation.get("rows") or []:
                label = item.get("label") or item.get("id") or facet
                if facet == "balances":
                    values.append(f"{label} {quantity(item, 'amount')} "
                                  f"({item.get('scope') or 'scope unknown'}, {item.get('usage_kind') or 'kind unknown'}; "
                                  f"{item.get('note') or 'no availability inferred'})")
                elif facet == "spending":
                    values.append(f"{label} {quantity(item, 'used')}/{quantity(item, 'limit')} "
                                  f"({item.get('scope') or 'scope unknown'}; {item.get('note') or 'no availability inferred'})")
                elif facet == "resets":
                    usable = {True: "yes", False: "no", None: "unknown"}.get(item.get("usable_now"), "unknown")
                    count = item.get('available_count')
                    values.append(f"{label} count {count if count is not None else 'unknown'} usable {usable} "
                                  f"reset {_second(item['resets_at']) if item.get('resets_at') else 'not reported'} "
                                  "(information only)")
                else:
                    values.append(str(item.get("code") or "diagnostic"))
                if facet in ("balances", "spending"):
                    until = (item.get("period") or {}).get("ends_at") or item.get("resets_at")
                    if until:
                        values[-1] += f"; reported period/reset ends {_second(until)}"
                if item.get("state") == "last_known":
                    values[-1] += "; last known: reported boundary passed"
            omitted = observation.get("omitted") or 0
            dated = "current" if state == "current" else (
                f"last known observed {observation.get('observed_at') or 'time not reported'}")
            lines.append(f"{facet}: {', '.join(values) if values else 'none reported'}; {dated}"
                         + (f"; {omitted} more rows in quota_summary" if omitted else ""))
    # Repeated identical resource facts are a counted cohort, not dozens of
    # repeated paragraphs. This never adds amounts across scopes/accounts.
    return [line + (f" ({count} accounts)" if count > 1 else "")
            for line, count in Counter(lines).items()]


def snapshot_text(answer: Dict[str, Any], bound: int = MAX_CHARS) -> str:
    """The text, from the model tool's own answer dictionary. Families in the
    tool's order; per family the tightest limit first, then the others, the
    same-account line, the manual reset offers. Whole lines that do not fit
    are dropped, lowest priority first, and the drop is said."""
    rows = list(answer.get("groups") or [])
    families: List[str] = []
    for row in rows:
        family = str(row.get("family") or str(row.get("key", "")).split("|")[0])
        if family not in families:
            families.append(family)
    resources = answer.get("resources") or {}
    harness_of = {str(r.get('family') or str(r.get('key', '')).split('|')[0]):
                  str(r.get('key', '')).split('|')[0] for r in rows}
    for hid in resources:
        if hid not in harness_of.values():
            families.append(hid)
            harness_of[hid] = hid
    blocks: List[Tuple[str, List[Tuple[int, str]]]] = []
    for family in families:
        lines: List[Tuple[int, str]] = []
        mine = [r for r in rows if str(r.get("family") or str(r.get("key", "")).split("|")[0]) == family]
        harness = harness_of[family]
        if not mine:
            lines.append((0, "  no quota-window reading; resources only, availability not inferred"))
        for row in mine:
            lines.append((0 if row.get("tightest_in_family") else 2 if row.get("models") or row.get("model_prefixes") else 1,
                          "  " + _limit_line(row) + (" [lowest left]" if row.get("tightest_in_family") else "")))
        joint = _joint_line((answer.get("joint") or {}).get(harness) or {})
        if joint:
            lines.append((0, "  " + joint))
        offers = (answer.get("manual_reset_offers") or {}).get(harness)
        if offers:
            lines.append((1, f"  manual reset offers reported usable: {offers.get('available')} "
                             f"(accounts: {offers.get('accounts_current')} current, "
                             f"{offers.get('accounts_last_known')} last known, "
                             f"{offers.get('accounts_not_reported')} not reported; "
                             f"{offers.get('offers_uncounted', 0)} offers uncounted); information only, never spent here"))
        lines.extend((1, "  " + line) for line in _resource_lines(resources.get(harness) or {}))
        blocks.append((family, lines))
    flat = [(bi, li, pri, text) for bi, (_f, lines) in enumerate(blocks) for li, (pri, text) in enumerate(lines)]

    def render(drop: set) -> str:
        parts = [HEADER]
        echo = answer.get("availability_scope") or {}
        parts.append((f"Engine availability basis {echo.get('basis')}; "
                      f"{echo.get('model') or 'unspecified-model request'}, {echo.get('request_kind')}"
                      if echo.get("stated") else "Engine request scope not stated"))
        for bi, (family, lines) in enumerate(blocks):
            body = [text for li, (_p, text) in enumerate(lines) if (bi, li) not in drop]
            if body:
                parts.append(family)
                parts.extend(body)
        if drop:
            omitted = sorted({blocks[bi][0] for bi, _li in drop})
            parts.append(f"({len(drop)} line(s) left out for {', '.join(omitted)}; quota_summary has them)")
        return "\n".join(parts)

    drop: set = set()
    text = render(drop)
    for bi, li, _pri, _t in sorted(flat, key=lambda x: (-x[2], -x[0], -x[1])):
        if len(text) <= bound:
            break
        drop.add((bi, li))
        text = render(drop)
    return text


def earliest_reset(answer: Dict[str, Any]) -> Optional[str]:
    resets = [str((r.get("next_reset") or {}).get("at")) for r in answer.get("groups") or []
              if (r.get("next_reset") or {}).get("at")]
    # Current monetary/resource facts have cycle boundaries too. Resource-only
    # families must not remain fresh past their reported period/reset merely
    # because no quota-window group exists. Last-known facets stay dated.
    for family in (answer.get("resources") or {}).values():
        for observations in (family.get("facets") or {}).values():
            for observation in observations:
                if observation.get("state") != "current":
                    continue
                for row in observation.get("rows") or []:
                    if row.get("state") == "last_known":
                        continue
                    resets.extend(str(value) for value in
                                  (row.get("resets_at"), (row.get("period") or {}).get("ends_at")) if value)
    # ISO spellings with different offsets are not lexically time-ordered.
    valid = [(qs.parse_instant(value), value) for value in resets]
    valid = [(stamp, value) for stamp, value in valid if stamp is not None]
    return min(valid, key=lambda pair: pair[0])[1] if valid else None


def envelope(text: str, read_at: float, reset_at: Optional[float], iso) -> Dict[str, Any]:
    """The file's content: observation and validity from the collector's own
    semantics (see the module note)."""
    valid = read_at + VALID_FOR_SEC
    if reset_at is not None:
        if reset_at <= read_at:
            raise ValueError("a passed reset cannot renew a fresh snapshot")
        valid = min(valid, reset_at)
    return {"schema_version": SCHEMA_VERSION, "observed_at": iso(read_at), "valid_until": iso(valid), "text": text}


def read_snapshot(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def publish(state_dir: Path, text: str, read_at: float, reset_at: Optional[float], iso, parse,
            control=None) -> str:
    """Write the snapshot atomically, or leave it. An unchanged text whose
    validity is not about to end is not rewritten (the host dedupes it, and a
    rewrite would only churn ``observed_at``). A changed text, or an
    unchanged one near the end of its validity, is written with a fresh
    observation. Returns ``written`` | ``unchanged``; raises on a failed write."""
    path = state_dir / SNAPSHOT_FILE
    if reset_at is not None and reset_at <= read_at:
        return "skipped"
    body = envelope(text, read_at, reset_at, iso)
    previous = read_snapshot(path)
    if previous and previous.get("text") == text and previous.get("schema_version") == SCHEMA_VERSION:
        until = parse(previous.get("valid_until"))
        if (until is not None and until - read_at > RENEW_MARGIN_SEC
                and (reset_at is None or until <= reset_at)):
            return "unchanged"
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=SNAPSHOT_FILE + ".", suffix=".tmp", dir=str(state_dir))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
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
