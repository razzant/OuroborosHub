"""Expose only selected, independently dated provider metadata."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from .keys_adapters import URLS, seconds, timestamp
from .keys_settings import LABELS, UNPROBED

STALE_AFTER_S = 1800
MESSAGES = {
    "key_rejected": "Key rejected (401)", "forbidden": "Access forbidden (403)",
    "api_key_invalid": "Provider reports an invalid API key (400)",
    "payment_required": "Payment refused (402)", "rate_limited": "Rate limited (429)",
    "provider_error": "Provider could not return this reading", "unreachable": "Provider unreachable",
    "invalid_response": "Provider returned an unreadable response", "request_timeout": "Reading timed out",
    "not_granted": "This setting was not granted to the widget", "settings_unreadable": "Settings could not be read",
    "access_closed": "Settings access is closed", "no_metadata_endpoint": "Custom endpoint: no metadata reader",
    "unverified_issuer": "Direct OpenAI key not read: its legacy base names another or unknown issuer",
    "keyless": "Keyless endpoint", "unsupported": "No metadata reader for this provider",
}
ACCESS_SOURCES = {"key": "key metadata", "credits": "account credits", "models": "model list",
                  "costs": "organization costs", "usage": "organization usage"}
ORG_READS = ("costs", "usage")
PARTIAL_PHASES = {"deferred", "unfinished", "not_attempted", "retry_wait"}
CALENDAR_PERIOD = {"daily": "day", "weekly": "week", "monthly": "month"}
EXPECTED_SCOPE_DENIALS = {("costs", "api.usage.read"), ("usage", "admin_or_org_key")}
ENDPOINT_NOTES = {
    "openai": (("rate_limits", "No rate-limit figures from the free metadata read"),),
    "anthropic": (("rate_limits", "No rate-limit figures from the free metadata read"),),
    "xai": (("balance", "This widget has no balance reader for the xAI voice key"),),
    "gemini": (("balance", "No key-accessible balance or usage reader"),),
}


def status(level, code, text, **source):
    return {"level": level, "code": code, "text": text, **source}


def row_status(r, reason):
    if reason:
        return status("unknown", reason, MESSAGES[reason])
    primary = next((m for ep, m in r["endpoints"].items() if ep != "credits" and ep not in ORG_READS), {})
    if r.get("blocked") is True and not primary.get("stale"):
        return status("bad", "key_disabled", "Provider reports key blocked or disabled")
    if r.get("is_available") is False and not primary.get("stale"):
        return status("warn", "unavailable_balance", "Provider reports balance unavailable")
    credit = (r["credits"] or {}).get("remaining_usd") if not r["endpoints"].get("credits", {}).get("stale") else None
    budgets = [(name, label, b) for name, label in (("key_limit", "Key limit"), ("effective_budget", "Effective budget"))
               if (b := r.get(name)) and not (b.get("stale") or b.get("window_state"))]
    for name, label, b in budgets:
        if b.get("remaining_usd") is not None and b["remaining_usd"] <= 0:
            return status("bad", "limit_reached", f"{label} reached", budget=name)
    if credit is not None and credit < 1:
        amount = "less than $0.01" if 0 < credit < .01 else f"${credit:,.2f}"
        return status("bad" if credit <= .1 else "warn", "low_credit", f"Account credit {amount} left")
    for name, label, b in budgets:
        left, cap = b.get("remaining_usd"), b.get("limit_usd")
        if left is not None and cap is not None and cap > 0 and left / cap <= .2:
            return status("warn", "low_limit", f"{label} nearly used", budget=name)
    denied = [e for e in r["unavailable"] if e.get("status") in (401, 403) and e["endpoint"] in ACCESS_SOURCES]
    readable = {ep for ep, m in r["endpoints"].items() if m.get("ok_at") and not (m.get("reason") or m.get("stale"))}
    if denied and readable:
        expected_scope = "models" in readable and all(
            (e["endpoint"], e.get("scope_hint")) in EXPECTED_SCOPE_DENIALS for e in denied)
        sources = ", ".join(ACCESS_SOURCES[e["endpoint"]] for e in denied)
        return status("unknown" if expected_scope else "warn", "partial_access", f"Partial access · {sources} not readable")
    if primary.get("reason"):
        code = primary["reason"]
        level = "bad" if code in ("key_rejected", "api_key_invalid", "payment_required") else (
            "warn" if primary.get("ok_at") or code == "rate_limited" else "unknown")
        return status(level, code, MESSAGES[code])
    if not primary.get("ok_at") or primary.get("stale"):
        return status("unknown", "stale" if primary.get("ok_at") else "not_read",
                      "Out of date" if primary.get("ok_at") else "Not read yet")
    if any(e["reason"] in MESSAGES for e in r["unavailable"] if e["endpoint"] == "credits"):
        return status("warn", "credit_unavailable", "Account credit could not be read")
    if primary.get("phase") in ("unfinished", "deferred", "retry_wait"):
        return status("unknown", primary["phase"], "Showing last reading")
    if "models" in readable and (org := next((e for e in r["unavailable"] if e["endpoint"] in ORG_READS), None)):
        return status("warn", "partial_reading", f"Partial reading · {ACCESS_SOURCES[org['endpoint']]} not updated")
    if any(b.get("window_state") for b in (r.get("key_limit"), r.get("effective_budget")) if b):
        return status("warn", "budget_window", "Current budget window not read")
    if r.get("blocked") is False:
        return status("ok", "ok", "Provider reports key not blocked")
    if (r["key_limit"] or {}).get("no_limit") and not r.get("effective_budget"):
        return status("ok", "ok", "No key limit")
    if r["endpoints"].get("models", {}).get("ok_at"):
        return status("ok", "ok", "Model list readable")
    return status("ok", "ok", "Metadata read")


def calendar_dates(at):
    d = datetime.fromtimestamp(at, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return {"day": d, "week": d - timedelta(days=d.weekday()), "month": d.replace(day=1)}


def dated_budget(b, now, stale, periods):
    b = dict(b)
    reset = seconds(b.get("resets_at"))
    if reset is None and (period := CALENDAR_PERIOD.get(b.get("interval"))):
        start = (periods or {}).get(period)
        if start is None:
            b["window_state"] = "unconfirmed" if periods is None else "crossed"
        else:
            end = ((start + timedelta(days=32)).replace(day=1) if period == "month" else
                   start + timedelta(days=7 if period == "week" else 1))
            reset = end.timestamp()
            b["calendar_ends_at"] = timestamp(reset)
    if reset is not None and reset <= now:
        b["window_state"] = "expired"
    if b.get("window_state"):
        # A stale read is still evidence. An old/ambiguous budget cannot be used as current headroom.
        b.pop("remaining_usd", None)
        b.pop("spent_usd", None)
    if stale:
        b["stale"] = True
    return b


def reading(record, endpoint, now, skipped):
    meta, fields, account = record
    meta = dict(meta)
    ok = seconds(meta.get("ok_at"))
    stale = bool(ok is not None and (meta.get("reason") or now-ok >= STALE_AFTER_S))
    if endpoint == "key" and ok is not None:
        fields = dict(fields)
        start = seconds(meta.get("read_started_at"))
        first = calendar_dates(start) if start is not None else None
        # Release a period identity only if both ends of the read agree.
        periods = None if first is None else {k: d for k, d in calendar_dates(ok).items() if first[k] == d}
        for name in ("key_limit", "effective_budget"):
            if fields.get(name) is not None:
                fields[name] = dated_budget(fields[name], now, stale, periods)
        if fields.get("budgets_extra"):
            fields["budgets_extra"] = [dated_budget(b, now, stale, periods) for b in fields["budgets_extra"]]
        dates = {k: d.date().isoformat() for k, d in (periods or {}).items()}
        if fields.get("provider_spend"):
            fields["provider_spend"] = {**fields["provider_spend"], "as_of": meta["ok_at"], "period_start_utc": dates}
        if fields.get("free_requests"):
            fields["free_requests"] = {**fields["free_requests"], "period_start_utc": dates.get("day")}
    retry = seconds(meta.get("retry_at"))
    phase = skipped or meta.get("phase")
    if retry is not None and retry > now:
        phase = "retry_wait"
    elif phase == "retry_wait":
        phase = None
    meta.update(phase=phase or ("failed" if meta.get("reason") else "read" if ok is not None else "not_attempted"),
                stale=stale)
    return meta, fields, account


def unavailable(endpoint, meta):
    code = meta.get("reason")
    if not code:
        if endpoint in ORG_READS and meta["phase"] in PARTIAL_PHASES:
            return {**meta, "endpoint": endpoint, "reason": meta["phase"]}
        return None
    label = MESSAGES[code]
    if endpoint == "credits" and code in ("forbidden", "key_rejected"):
        label = f"Account credit not available to this key (HTTP {meta['status']})"
    if meta.get("scope_hint") == "api.usage.read":
        label += " · OpenAI reports missing scope api.usage.read"
    elif meta.get("scope_hint") == "admin_or_org_key":
        label += " · Anthropic reports that an Admin or organization-scoped key is required"
    return {**meta, "endpoint": endpoint, "text": label}


def assemble(creds, absent, settings, ids, records, now, refresh, skipped):
    rows, shared_accounts = [], {}
    for cred in creds:
        rid = ids[cred.identity]
        row = dict(id=rid, kind=cred.kind, provider_label=LABELS[cred.kind],
                   group="other" if cred.kind in ("xai", "gemini") else "model",
                   uses=sorted(deepcopy(cred.uses), key=lambda u: u["source"] != "connection"),
                   key_limit=None, credits=None, budgets_extra=[], provider_spend=None, free_requests=None,
                   facts=[], unavailable=[])
        endpoints = {}
        for endpoint in (() if cred.reason else URLS.get(cred.kind, {})):
            ident = rid, endpoint
            meta, fields, account = reading(records.get(ident, ({}, {}, None)), endpoint, now, skipped.get(ident))
            endpoints[endpoint] = meta
            for k, v in fields.items():
                if k == "facts":
                    row["facts"].extend(v)
                else:
                    row[k] = deepcopy(v)
            if endpoint == "credits" and row["credits"]:
                row["credits"]["state"] = "stale" if meta["stale"] else "ok"
            if account:
                shared_accounts.setdefault(account, []).append(row)
            if problem := unavailable(endpoint, meta):
                row["unavailable"].append(problem)
        if not cred.reason:
            for endpoint, label in ENDPOINT_NOTES.get(cred.kind, ()):
                if endpoint != "rate_limits" or not row.get("reported_model_list_rates"):
                    row["unavailable"].append(dict(endpoint=endpoint, reason="no_metadata_endpoint", text=label))
        row["endpoints"] = endpoints
        row["read"] = {k: max((m[k] for m in endpoints.values() if m.get(k)), default=None) for k in ("ok_at", "attempt_at")}
        row["read"].update(refreshing=refresh["state"] == "refreshing" and bool(endpoints),
                           stale=any(m["stale"] for m in endpoints.values()))
        row["status"] = row_status(row, cred.reason)
        rows.append(row)
    for group in shared_accounts.values():
        for row in group:
            if row["credits"]:
                row["credits"]["shared_with"] = [r["id"] for r in group if r is not row]
    rank = {"bad": 3, "warn": 2, "unknown": 1, "ok": 0}
    worst = max(rows, key=lambda r: rank[r["status"]["level"]], default=None)
    if worst:
        name = next((u["label"] for u in worst["uses"] if u["source"] == "connection"), worst["provider_label"])
        summary = dict(level=worst["status"]["level"], text=f"{name}: {worst['status']['text']}", row_id=worst["id"])
    else:
        summary = dict(level="unknown", text=MESSAGES.get(settings["state"], "No readable provider keys"), row_id=None)
    refresh["last_read_at"] = max((r["read"]["ok_at"] for r in rows if r["read"]["ok_at"]), default=None)
    refresh["stale_after_s"] = STALE_AFTER_S
    return dict(schema="provider_keys.snapshot/1", generated_at=timestamp(now), refresh=refresh,
                settings=settings, summary=summary, rows=rows, not_configured=absent,
                unsupported_builtin=[dict(kind=k, label=LABELS[k]) for k in UNPROBED])
