"""Compact display of the widget's tightest measured limit per family.

Thresholds affect color only: <=10% bad, <=25% warn, otherwise ok.
No quota policy, refresh, routing or freshness judgment lives here.
"""

from collections import Counter
from datetime import datetime

if __package__:  # the host loads the skill as a package; tests import it directly
    from .quota_context import limit_title
    from .quota_summary import measured_words
else:
    from quota_context import limit_title
    from quota_summary import measured_words

TEXT_LIMIT = 24  # host composer chip contract


def local_reset(at):
    """The moment of a reset in the time zone of this machine: the chip's title is read by the person at it. A value without a zone stays as it came."""
    if not at:
        return "unknown"
    try:
        moment = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return at
    if moment.tzinfo is None:
        return at
    try:
        return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, OverflowError, OSError):  # a moment the machine's zone cannot hold (a "forever" marker in the year 9999): as it came
        return at


def _coverage(group, roster):
    count = group["measured"]["accounts"]
    if roster == "unknown":
        return f"fresh readings for this limit: {count} account{'s' if count != 1 else ''}"
    slots = group["slots"]  # This limit is known for these accounts, not necessarily for the whole family.
    words = f"fresh readings: {count} of {slots} account{'s' if slots != 1 else ''} known for this limit"
    if other := group["applicability_unknown"]:
        words += f"; no reading for {other} other account{'s' if other != 1 else ''} (limit may not apply)"
    return words


def chip(view):
    reserve = view.get("reserve") or {}
    if not view.get("ok") or view.get("transport_error") or reserve.get("error"):
        reason = view.get("transport_error") or reserve.get("error") or "Quota readings unavailable"
        return {"text": "—", "tone": "muted", "title": reason}
    summary = reserve.get("summary") or {}
    roster = summary.get("roster", "unknown")
    families = {}
    names = Counter()
    for group in summary.get("groups", []):
        key = group["harness"]
        names[key, group["label"]] += 1
        if key not in families or group.get("tightest"):
            families[key] = group
    readings, titles = [], []
    for key, group in families.items():
        label = group.get("family") or key
        # Known ids lack display labels on the dedicated passive endpoint.
        label = {"codex": "Codex", "claude": "Claude"}.get(label, label)
        measured = group["measured"]
        share = measured["average_remaining_pct"] if group.get("tightest") else None
        reset = local_reset((group.get("next_reset") or {}).get("at"))
        if share is None:
            value = "remaining unknown"
        else:
            whole = measured_words(share, measured["accounts"], measured["at_limit"], places=0)
            precise = measured_words(share, measured["accounts"], measured["at_limit"], places=1).removesuffix(".0")
            readings.append((share, label, f" avg {whole}%"))
            value = f"{precise}% average remaining"
        limit = group["label"]
        if names[key, limit] > 1:
            limit = limit_title(group)
        titles.append(f"{label}: {value} ({limit}); {_coverage(group, roster)}; next reset {reset}")
    if not titles:
        titles.append("No current quota readings")
    if roster == "cached":
        titles.append("Account list last known")
    elif roster == "unknown":
        titles.append("Account list unavailable")
    title = "\n".join(titles)
    if not readings:
        return {"text": "—", "tone": "muted", "title": title}
    readings.sort(key=lambda row: row[0])
    # Prioritize the tightest families; the title always carries every family.
    parts = []
    for _, label, suffix in readings:
        part = label + suffix
        if len(" · ".join(parts + [part])) > TEXT_LIMIT:
            if not parts:  # shorten the family, not the meaning or the number
                parts.append(label[:TEXT_LIMIT - len(suffix) - 1] + "…" + suffix)
            break
        parts.append(part)
    minimum = readings[0][0]
    return {"text": " · ".join(parts), "tone": "bad" if minimum <= 10 else "warn" if minimum <= 25 else "ok",
            "title": title}
