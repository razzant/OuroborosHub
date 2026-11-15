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
* when the text would exceed either bound — its own length (MAX_CHARS, what
  it costs a context) or the host's limit on the whole file (MAX_FILE_BYTES:
  the complete JSON envelope in UTF-8, escapes included, read as oversized and
  dropped above it) — whole lower-priority lines are left out and the
  omission is said; a line is never cut in the middle;
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
    from . import quota_resources as qr
    from . import quota_summary as qs
except ImportError:
    import quota_resources as qr
    import quota_summary as qs

SCHEMA_VERSION = 1
SNAPSHOT_FILE = "context-snapshot.json"
MAX_CHARS = 12000  # compact full-family default; never silently erase a resource-only family
VALID_FOR_SEC = 420.0  # SIGHTING_GAP_SEC: three missed sweeps plus one status timeout
RENEW_MARGIN_SEC = 180.0  # an unchanged text is renewed once its validity is this close to ending
# The host reads at most this many bytes of the whole file and reports a larger one as oversized
# (ouroboros/skill_context_snapshot.py MAX_SNAPSHOT_BYTES): the bound of the complete UTF-8 envelope.
MAX_FILE_BYTES = 32768
# The widest date the envelope can carry (iso_exact: microseconds and a Z). A text is measured with both dates
# this wide, so the file published later, with its real dates, is never larger than measured.
WIDEST_DATE = "0000-00-00T00:00:00.000000Z"

HEADER = ("Claudexor quota snapshot — measured reserve of the authorized accounts; advisory, not a dispatch "
          "guarantee, no pins changed. Unit: account-windows (one account's share left of one limit = 1; "
          "limits of different length or scope are never added).")
# The display bounds of the provider's text in one fact. A text field is shown whole up to MAX_FIELD characters (the
# widest bound the tool's fields already have, a resource note's), so every value the tool carries bounded is printed
# exactly; only an unbounded one — the engine's availability_scope echo — can be longer, and is then shown in part with
# what it leaves out. An amount is never printed in part (a cut number reads as another number): up to MAX_AMOUNT
# characters it is exact, a longer one is said not shown. The omission note names up to MAX_NAMED families and counts
# the rest. The tool keeps every value as it is (quota_summary has them whole).
MAX_FIELD = 160
MAX_AMOUNT = 40
MAX_NAMED = 8


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


def _amount(raw: Any) -> str:
    """A reported amount (a string, as the engine sends it): exact up to MAX_AMOUNT characters (escaped if it holds a
    control character), else said not shown with its length — never cut into another number."""
    if isinstance(raw, str) and len(raw) > MAX_AMOUNT:
        return f"amount not shown ({len(raw)} characters; quota_summary has it)"
    return _field(raw, MAX_AMOUNT)


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
    """A reported instant in the text, to the nearest second; unreadable text stays as reported (in one line)."""
    stamp = qs.parse_instant(value)
    return qs.iso(round(stamp)) if stamp is not None else _field(value)


def _listed(names: List[Any], omitted: Any, suffix: str = "") -> str:
    """Up to three names, and how many more the tool lists or leaves out."""
    more = max(0, len(names) - 3) + (omitted if isinstance(omitted, int) and omitted > 0 else 0)
    return ", ".join(_field(n) + suffix for n in names[:3]) + (f" (+{more} more; quota_summary lists them)"
                                                               if more else "")


def _scope_words(row: Dict[str, Any]) -> str:
    """The limit's whole scope in words, each field independent of the others (names, family prefixes, the
    unspecified-model flag, request kinds, what cannot be read), so two limits of one name and length that govern
    different requests never read as one."""
    words = ""
    if row.get("models"):
        words += " models " + _listed(row["models"], row.get("models_omitted"))
    elif row.get("models_omitted"):
        words += f" models: {row['models_omitted']} not shown"
    if row.get("model_prefixes") or row.get("model_prefixes_omitted"):
        words += (" model families " if not row.get("models") else " and model families ") + (
            _listed(row.get("model_prefixes") or [], row.get("model_prefixes_omitted"), "*"))
    if row.get("model_scope") == "model-scoped; model names unavailable from history":
        words += " model-scoped, names unavailable from history"
    if row.get("governs_unspecified_model"):
        words += ", also governs requests naming no model"
    kinds = row.get("request_kinds")
    if kinds is not None:
        words += (" " + " and ".join(_field(k) for k in kinds) + " requests only") if kinds else " no request kinds"
    if row.get("scope_unreadable"):
        words += " scope unreadable: " + ", ".join(str(f).replace("_", " ") for f in row["scope_unreadable"]) + \
                 " (applicability unknown)"
    if row.get("request_scope"):
        words += " request scope " + str(row["request_scope"])
    return words


def _limit_line(row: Dict[str, Any]) -> str:
    accounts = row.get("of_accounts") or 0
    known = row.get("accounts_known_to_apply")
    name = f"{_field(row.get('limit'))} ({row.get('duration')})" + _scope_words(row)
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
    words = (f"same-account capacity ({', '.join(_field(w) for w in joint.get('windows') or [])}; "
             f"{'unspecified-model request' if not scope.get('model') else 'model ' + _field(scope['model'])}, "
             f"{_field(scope.get('request_kind') or 'text')}; unit: shares of the {_field(joint.get('unit'))}): "
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


def serialize(body: Dict[str, Any]) -> bytes:
    """The file's bytes exactly as publish() writes them: compact JSON, UTF-8, no ASCII escaping."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def envelope_bytes(text: str) -> int:
    """The size of the complete file publish() would write for ``text``, its two dates at their widest
    (WIDEST_DATE): what the host's byte bound (MAX_FILE_BYTES) is checked against. A real publication,
    with its real dates, is never larger."""
    return len(serialize({"schema_version": SCHEMA_VERSION, "observed_at": WIDEST_DATE,
                          "valid_until": WIDEST_DATE, "text": text}))


def snapshot_text(answer: Dict[str, Any], bound: int = MAX_CHARS, byte_bound: int = MAX_FILE_BYTES) -> str:
    """The text, from the model tool's own answer dictionary. Families in the
    tool's order; per family the tightest limit first, then the others, the
    same-account line, the manual reset offers. Whole lines that do not fit
    are dropped, lowest priority first, and the drop is said. Two bounds,
    both kept: the text's own length (``bound`` characters) and the complete
    file in UTF-8 (``byte_bound`` bytes, envelope_bytes): multi-byte or
    escaped provider text can pass the first and not the second."""
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
                             + ("(partial subtotal) " if offers.get("offers_uncounted") else "")
                             + f"(accounts: {offers.get('accounts_current')} current, "
                             f"{offers.get('accounts_last_known')} last known, "
                             f"{offers.get('accounts_not_reported')} not reported; "
                             f"{offers.get('offers_uncounted', 0)} offers uncounted); information only, never spent here"))
        lines.extend((1, "  " + line) for line in _resource_lines(resources.get(harness) or {}))
        blocks.append((family, lines))
    flat = [(bi, li, pri, text) for bi, (_f, lines) in enumerate(blocks) for li, (pri, text) in enumerate(lines)]
    # What stands whatever is left out — the header, the engine's scope echo
    # and the omission note — is bounded itself (_field, MAX_NAMED), so that
    # with every droppable line left out the text always fits.
    echo = answer.get("availability_scope") or {}
    scope_line = (f"Engine availability basis {_field(echo.get('basis'))}; "
                  f"{_field(echo.get('model')) if echo.get('model') else 'unspecified-model request'}, "
                  f"{_field(echo.get('request_kind'))}"
                  if echo.get("stated") else "Engine request scope not stated")

    def render(drop: set) -> str:
        parts = [HEADER, scope_line]
        if answer.get("resource_rows_omitted"):
            parts.append(f"Resource input: {answer['resource_rows_omitted']} rows omitted at the input bound")
        for bi, (family, lines) in enumerate(blocks):
            body = [text for li, (_p, text) in enumerate(lines) if (bi, li) not in drop]
            if body:
                parts.append(_field(family))
                parts.extend(body)
        if drop:
            omitted = sorted({blocks[bi][0] for bi, _li in drop})
            named = ", ".join(_field(name) for name in omitted[:MAX_NAMED])
            more = len(omitted) - MAX_NAMED
            parts.append(f"({len(drop)} line(s) left out for {named}"
                         + (f" and {more} more families" if more > 0 else "") + "; quota_summary has them)")
        return "\n".join(parts)

    def fits(candidate: str) -> bool:
        return len(candidate) <= bound and envelope_bytes(candidate) <= byte_bound

    drop: set = set()
    text = render(drop)
    for bi, li, _pri, _t in sorted(flat, key=lambda x: (-x[2], -x[0], -x[1])):
        if fits(text):
            break
        drop.add((bi, li))
        text = render(drop)
    if not fits(text):
        # Only a caller's bound below the fixed parts gets here: the text says
        # it carries nothing, never a cut fact.
        text = HEADER + "\n(every line left out; quota_summary has them)"
        if not fits(text):
            text = "Snapshot details omitted; quota_summary has them."
        if not fits(text):
            raise ValueError("snapshot bounds cannot hold an omission notice")
    return text


def earliest_reset(answer: Dict[str, Any]) -> Optional[str]:
    resets = [str((r.get("next_reset") or {}).get("at")) for r in answer.get("groups") or []
              if (r.get("next_reset") or {}).get("at")]
    # Current monetary/resource facts have cycle boundaries too, and a reset
    # offer's usable grants their expiry (quota_resources.boundaries).
    # Resource-only families must not remain fresh past them merely because
    # no quota-window group exists. Last-known facets stay dated.
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
            control=None) -> str:
    """Write the snapshot atomically, or leave it. An unchanged text whose
    validity is not about to end is not rewritten (the host dedupes it, and a
    rewrite would only churn ``observed_at``). A changed text, or an
    unchanged one near the end of its validity, is written with a fresh
    observation. Returns ``written`` | ``unchanged``; raises on a failed write,
    and before writing anything on a file larger than the host reads
    (MAX_FILE_BYTES): the last file then simply expires."""
    path = state_dir / SNAPSHOT_FILE
    if reset_at is not None and reset_at <= read_at:
        return "skipped"
    body = envelope(text, read_at, reset_at, iso)
    data = serialize(body)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"context snapshot is {len(data)} bytes, over the host's {MAX_FILE_BYTES}-byte bound")
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
