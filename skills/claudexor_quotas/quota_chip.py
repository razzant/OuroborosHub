"""Compact display of the widget's tightest measured limit per family.

Thresholds affect color only: <=10% bad, <=25% warn, otherwise ok.
No quota policy, refresh, routing or freshness judgment lives here.
"""


def chip(view):
    reserve = view.get("reserve") or {}
    if not view.get("ok") or view.get("transport_error") or reserve.get("error"):
        reason = view.get("transport_error") or reserve.get("error") or "Quota readings unavailable"
        return {"text": "—", "tone": "muted", "title": reason}
    groups = (reserve.get("summary") or {}).get("groups", [])
    families = {}
    for group in groups:
        key = group["harness"]
        if key not in families or group.get("tightest"):
            families[key] = group
    readings, titles = [], []
    for key, group in families.items():
        label = group.get("family") or key
        # Known ids lack display labels on the dedicated passive endpoint.
        label = {"codex": "Codex", "claude": "Claude"}.get(label, label)
        share = (group.get("measured") or {}).get("average_remaining_pct") if group.get("tightest") else None
        reset = (group.get("next_reset") or {}).get("at") or "unknown"
        if share is None:
            titles.append(f"{label}: remaining unknown; next reset {reset}")
        else:
            readings.append((share, label))
            titles.append(f"{label}: {share:g}% average remaining ({group['label']}); next reset {reset}")
    if not readings:
        return {"text": "—", "tone": "muted", "title": "\n".join(titles) or "No current quota readings"}
    readings.sort(key=lambda row: row[0])
    # Prioritize the tightest families; the title always carries every family.
    parts = []
    for share, label in readings:
        part = f"{label} {share:.0f}%"
        if len(" · ".join(parts + [part])) > 24:
            break
        parts.append(part)
    if not parts:
        share, label = readings[0]
        suffix = f" {share:.0f}%"
        parts = [label[:23 - len(suffix)] + "…" + suffix]
    minimum = readings[0][0]
    return {"text": " · ".join(parts), "tone": "bad" if minimum <= 10 else "warn" if minimum <= 25 else "ok",
            "title": "\n".join(titles)}
