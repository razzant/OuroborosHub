"""Free provider reads. Only typed, explicitly selected fields leave this module."""
import json
import math
import re
import time
from http.client import HTTPException
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request

MAX_RETRY_S = 86400
MONEY_FIELDS = ("limit_usd", "remaining_usd", "spend_usd")
WINDOWS = (("day", "_daily"), ("week", "_weekly"), ("month", "_monthly"), ("lifetime", ""))
BUDGET_SCOPES = ("key_limit", "workspace", "guardrail", "workspace_limit", "guardrail_limit",
                 "guardrail_budget", "workspace_budget", "organization", "budget", "member", "api_key",
                 "workspace_default_member", "workspace_default_key")
ACL_SCAN_LIMIT = 512

URLS = {
    "openrouter": {"key": "https://openrouter.ai/api/v1/key", "credits": "https://openrouter.ai/api/v1/credits"},
    "openai": {"models": "https://api.openai.com/v1/models", "costs": "https://api.openai.com/v1/organization/costs"},
    "anthropic": {"models": "https://api.anthropic.com/v1/models?limit=1",
                  "usage": "https://api.anthropic.com/v1/organizations/usage_report/messages"},
    "xai": {"api_key": "https://api.x.ai/v1/api-key"},
    "gemini": {"models": "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"},
    "deepseek": {"balance": "https://api.deepseek.com/user/balance"},
}
INVALID_KEY_READS = {URLS["gemini"]["models"]: "gemini", URLS["xai"]["api_key"]: "xai"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def http(url, headers, timeout):
    end = time.monotonic() + timeout
    opener = build_opener(NoRedirect(), ProxyHandler({}))
    try:
        response = opener.open(Request(url, headers=headers), timeout=timeout)
    except HTTPError as error:
        with error:
            # Preserve the HTTP result even if its optional error body cannot be read.
            hints = {}
            kind = INVALID_KEY_READS.get(url)
            if error.code in (401, 403) or (error.code == 400 and kind):
                try:
                    body = bytearray()
                    while len(body) < 4096 and time.monotonic() < end:
                        chunk = error.read1(4096-len(body))
                        if not chunk:
                            break
                        body.extend(chunk)
                    if error.code == 400:
                        hints = {"api_key_invalid": invalid_key(kind, json.loads(body))}
                    else:
                        hints = scope_hints(body.decode("utf-8", errors="replace"))
                except (OSError, ValueError, HTTPException, RecursionError):
                    pass
            return error.code, hints, {"retry-after": error.headers.get("Retry-After")}
    with response:
        status = response.status
        chunks = bytearray()
        while len(chunks) <= 1024 * 1024:
            chunk = response.read1(65536)
            if time.monotonic() >= end:
                raise TimeoutError
            if not chunk:
                break
            chunks.extend(chunk)
        data = bytes(chunks)
        if len(data) > 1024 * 1024:
            raise ValueError("oversized response")
        # Select metadata on the response side. Never propagate arbitrary headers.
        rates = {k.lower(): v for k, v in response.headers.items()
                 if k.lower().startswith(("x-ratelimit-", "anthropic-ratelimit-"))}
        try:
            return status, json.loads(data), rates
        except (ValueError, RecursionError):
            return status, None, {}


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def seconds(value):
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        if value.endswith(("Z", "z")):
            value = value[:-1] + "+00:00"
        # Python 3.10's fromisoformat accepts only 3 or 6 fractional digits.
        value = re.sub(r"\.([0-9]+)(?=[+-][0-9]{2}:[0-9]{2}$)",
                       lambda m: "." + m[1][:6].ljust(6, "0"), value)
        dt = datetime.fromisoformat(value)
        return dt.timestamp() if dt.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def number(v):
    if isinstance(v, (int, float, str)) and not isinstance(v, bool):
        try:
            n = float(v)
            if math.isfinite(n):
                return n
        except (ValueError, OverflowError):
            pass
    return None


def budget(d):
    money = optional_numbers(d, MONEY_FIELDS)
    if "spend_usd" in money:
        money["spent_usd"] = money.pop("spend_usd")
    resets = seconds(d.get("resets_at"))
    interval = d.get("reset_interval")
    if interval is None and "reset_interval" in d:
        interval = "lifetime"
    elif interval not in ("daily", "weekly", "monthly"):
        interval = None
    return {**money,
            "includes_byok": d.get("include_byok") if isinstance(d.get("include_byok"), bool) else None,
            "scope": d.get("scope") if d.get("scope") in BUDGET_SCOPES else None,
            "interval": interval,
            "resets_at": timestamp(resets) if resets is not None else None}


def spend(data, prefix):
    values = optional_numbers(data, (prefix + suffix for _, suffix in WINDOWS))
    return {field + "_usd": values[prefix + suffix] for field, suffix in WINDOWS if prefix + suffix in values}


def optional_numbers(data, keys):
    """Missing, null, and zero differ. A present malformed number is not a reading."""
    if not isinstance(data, dict):
        raise ValueError("invalid numeric object")
    result = {}
    for key in keys:
        if key in data:
            result[key] = number(data[key])
            if data[key] is not None and result[key] is None:
                raise ValueError("invalid numeric value")
    return result


def object_list(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("invalid list of records")
    return value


def scope_hints(value):
    words = value.lower()
    return {"missing_usage_scope": "missing scopes: api.usage.read" in words,
            "admin_key_required": "admin api requires an admin api key" in words}


def invalid_key(kind, body):
    """Only classify advertised invalid-key forms; never forward the error text."""
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    error = error if isinstance(error, dict) else {}
    if kind == "xai":
        return "api_key_invalid" in (body.get("code"), error.get("code"))
    if kind == "gemini":
        details = error.get("details")
        return ((isinstance(details, list) and any(isinstance(d, dict) and d.get("reason") == "API_KEY_INVALID"
                                                    for d in details[:32]))
                or (error.get("status") == "INVALID_ARGUMENT"
                    and error.get("message") == "API key not valid. Please pass a valid API key."))
    return False


def retry_at(value, now):
    try:
        if not isinstance(value, str) or len(value) > 100:
            raise ValueError
        if value.isascii() and value.isdecimal():
            delay = int(value)
        else:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                raise ValueError
            delay = math.ceil(dt.timestamp() - now)
    except (ValueError, TypeError, OverflowError, OSError):
        delay = 60
    return timestamp(now + max(1, min(MAX_RETRY_S, delay)))


def model_list_rates(kind, headers):
    facts = []
    if kind not in ("openai", "anthropic"):
        return facts
    for unit in ("requests", "tokens", "input-tokens", "output-tokens"):
        for field in ("limit", "remaining", "reset"):
            name = (f"x-ratelimit-{field}-{unit}" if kind == "openai"
                    else f"anthropic-ratelimit-{unit}-{field}")
            raw = headers.get(name)
            if not isinstance(raw, str) or len(raw) > 48:
                continue
            if field == "reset":
                instant = seconds(raw)
                if instant is not None:
                    value = timestamp(instant)
                elif re.fullmatch(r"(?:[0-9]{1,8}(?:\.[0-9]{1,3})?(?:ms|d|h|m|s)){1,5}", raw):
                    value = raw
                else:
                    continue
            elif re.fullmatch(r"[0-9]{1,12}", raw):
                value = str(int(raw))
            else:
                continue
            facts.append({"label": f"Model list · {unit.replace('-', ' ')} {field}", "value": value})
    return facts


def own_budget(layers, legacy):
    own = None
    for raw, parsed in layers:
        # An assigned guardrail can be scoped to an API key without being its own cap.
        if parsed["scope"] != "key_limit" or any(raw.get(k) is not None for k in ("guardrail_id", "budget_id")):
            continue
        if "limit_usd" not in parsed and parsed.get("remaining_usd") is None:
            continue
        if legacy is not None and (("limit_usd" in legacy and parsed.get("limit_usd") != legacy["limit_usd"]) or any(
                legacy.get(k) is not None and parsed.get(k) != legacy[k]
                for k in ("remaining_usd", "interval", "includes_byok"))):
            continue
        if own is not None and raw != own[0]:
            return None  # The evidence does not pick between multiple candidates.
        own = raw, parsed
    return own


def key_data(d):
    usage, byok = spend(d, "usage"), spend(d, "byok_usage")
    legacy = {target: d[source] for source, target in (("limit", "limit_usd"), ("limit_remaining", "remaining_usd"),
              ("limit_reset", "reset_interval"), ("include_byok_in_limit", "include_byok")) if source in d}
    limit = budget(dict(scope="key_limit", **legacy)) if "limit_usd" in legacy or legacy.get("remaining_usd") is not None else None
    effective = d.get("effective_budget")
    parsed_effective = budget(effective) if effective is not None else None
    layers = [(raw, budget(raw)) for raw in object_list(d["budgets"])] if d.get("budgets") is not None else []
    own = own_budget(layers + ([(effective, parsed_effective)] if parsed_effective is not None else []), limit)
    own_raw = None
    if own is not None:
        own_raw, limit = own
    if limit is not None:
        if "spent_usd" not in limit and limit.get("limit_usd") is not None and limit.get("remaining_usd") is not None:
            limit["spent_usd"] = number(limit["limit_usd"] - limit["remaining_usd"])
        if limit.get("limit_usd") is None and "limit" in d and d["limit"] is None:
            limit["no_limit"] = True
    extra = [parsed for raw, parsed in layers if raw != effective and raw != own_raw]
    f = d.get("free_model_daily_requests")
    free = optional_numbers(f, ("used", "limit", "remaining")) if f is not None else {}
    if not (limit is not None or any(row.get("limit_usd") is not None or row.get("remaining_usd") is not None
            for row in extra + [parsed_effective or {}])
            or any(n is not None for n in (*usage.values(), *byok.values(), *free.values()))):
        raise ValueError("invalid key response")
    facts = []
    for key, label in (("is_free_tier", "Free tier"), ("is_management_key", "Management key"),
                       ("is_provisioning_key", "Provisioning key")):
        if isinstance(d.get(key), bool):
            facts.append({"label": label, "value": "Yes" if d[key] else "No"})
    if "expires_at" in d and (d["expires_at"] is None or seconds(d["expires_at"]) is not None):
        facts.append({"label": "Expires", "value": "Never" if d["expires_at"] is None else timestamp(seconds(d["expires_at"]))})
    regions = d.get("allowed_data_regions")
    regions = [v for v in regions if v in ("global", "us", "eu", "us-east", "us-west")] if isinstance(regions, list) else []
    if regions:
        facts.append({"label": "Data regions", "value": ", ".join(regions)})
    return {"key_limit": limit, "budgets_extra": extra,
            **({"effective_budget": parsed_effective} if parsed_effective is not None and effective != own_raw else {}),
            "facts": facts, "provider_spend": dict(calendar="utc", week_start="monday", **usage, byok=byok) if usage or byok else None,
            "free_requests": dict(interval="daily", **free) if f is not None else None}


def decode(kind, endpoint, body):
    if not isinstance(body, dict) or "error" in body or body.get("type") == "error":
        raise ValueError("invalid response")
    if endpoint == "key":
        d = body["data"]
        return key_data(d), d.get("organization_id") if isinstance(d.get("organization_id"), str) else None
    if endpoint == "credits":
        d = body["data"]
        values = optional_numbers(d, ("total_credits", "total_usage"))
        if not any(v is not None for v in values.values()):
            raise ValueError("invalid credits response")
        credits = {"state": "ok", "scope": "account", "shared_with": []}
        for src, dst in (("total_credits", "total_usd"), ("total_usage", "used_usd")):
            if src in values:
                credits[dst] = values[src]
        if all(v is not None for v in (credits.get("total_usd"), credits.get("used_usd"))):
            credits["remaining_usd"] = number(credits["total_usd"] - credits["used_usd"])
        return {"credits": credits}, None
    facts = []
    def fact(label, value):
        if value is not None:
            facts.append({"label": label, "value": str(value)})
    if endpoint == "models":
        models = object_list(body.get("models" if kind == "gemini" else "data"))
        field = "name" if kind == "gemini" else "id"
        if any(not isinstance(m.get(field), str) or not m[field].strip() for m in models):
            raise ValueError("invalid model list")
        if kind == "openai" or (kind == "anthropic" and body.get("has_more") is False) or (kind == "gemini" and not body.get("nextPageToken")):
            fact("Models listed", len(models))
        return {"facts": facts}, None
    if endpoint == "api_key":
        scopes = body.get("acls")
        if isinstance(scopes, list):
            seen = scopes[:ACL_SCAN_LIMIT]
            allowed = [s for s in ("api-key:model:*", "api-key:endpoint:*") if s in seen]
            if allowed:
                fact("Scopes", ", ".join(allowed))
            for unit in ("model", "endpoint"):
                prefix = f"api-key:{unit}:"
                count = len({s for s in seen if isinstance(s, str) and s.startswith(prefix)
                             and len(s) > len(prefix) and s != prefix + "*"})
                if count:
                    fact(f"Specific {unit} scopes", f"At least {count}" if len(scopes) > len(seen) else count)
        flags = [body.get(k) for k in ("team_blocked", "api_key_blocked", "api_key_disabled")]
        if not any(isinstance(f, bool) for f in flags):
            raise ValueError("invalid key flags")
        for k, flag in zip(("Team blocked", "Key blocked", "Key disabled"), flags):
            if isinstance(flag, bool):
                fact(k, "Yes" if flag else "No")
        blocked = True if any(f is True for f in flags) else (False if all(f is False for f in flags) else None)
        return {"facts": facts, "blocked": blocked}, None
    if endpoint == "balance":
        available = body.get("is_available")
        balances = []
        if not isinstance(available, bool):
            raise ValueError("missing balance availability")
        for b in object_list(body["balance_infos"]):
            currency = b.get("currency")
            amounts = optional_numbers(b, ("total_balance", "granted_balance", "topped_up_balance"))
            if currency not in ("USD", "CNY") or amounts.get("total_balance") is None:
                raise ValueError("invalid native balance")
            balances.append({"currency": currency, **amounts})
        return {"balances": balances, "is_available": available}, None
    if endpoint in ("costs", "usage"):
        for bucket in object_list(body["data"])[:1]:
            if endpoint == "costs":
                since = number(bucket.get("start_time"))
                until = number(bucket.get("end_time"))
            else:
                since, until = seconds(bucket.get("starting_at")), seconds(bucket.get("ending_at"))
            if since is None or until is None or until <= since:
                raise ValueError("missing usage bucket bounds")
            period = timestamp(since) + " to " + timestamp(until)
            fact("First returned bucket", period)
            for r in object_list(bucket["results"]):
                if endpoint == "costs":
                    amount = r.get("amount")
                    money = optional_numbers(amount, ("value",))
                    currency = amount.get("currency")
                    if not isinstance(currency, str) or not re.fullmatch(r"[a-zA-Z]{3}", currency) or money.get("value") is None:
                        raise ValueError("invalid organization cost")
                    unit = currency.upper()
                    fact("Bucket organization cost (" + unit + ")", money["value"])
                else:
                    counts = optional_numbers(r, ("uncached_input_tokens", "cache_read_input_tokens", "output_tokens"))
                    if not any(v is not None for v in counts.values()):
                        raise ValueError("invalid usage counts")
                    for k, count in counts.items():
                        fact("Bucket organization " + k.replace("_", " "), count)
        return {"facts": facts}, None
    raise ValueError("unsupported metadata response")


def read(kind, key, endpoint, now, timeout):
    url = URLS[kind][endpoint]
    if endpoint == "costs":
        url += "?" + urlencode({"start_time": int(now-86400), "limit": 1})
    if endpoint == "usage":
        url += "?" + urlencode({"starting_at": timestamp(now-86400), "limit": 1})
    headers = {"Authorization": "Bearer " + key}
    if kind == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif kind == "gemini":
        headers = {"x-goog-api-key": key}
    started = time.monotonic()
    attempt = {"attempt_at": timestamp(now), "status": None, "reason": None, "retry_at": None}
    fields, account = {}, None
    try:
        status, body, response_headers = http(url, headers, timeout)
        response_headers = response_headers or {}
        retry = response_headers.get("retry-after")
        attempt["status"] = status
        if status == 200:
            try:
                fields, account = decode(kind, endpoint, body)
            except (ValueError, KeyError, TypeError, OSError, OverflowError):
                attempt["reason"] = "invalid_response"
            if endpoint == "models":
                rates = model_list_rates(kind, response_headers)
                if not attempt["reason"]:
                    fields["facts"].extend(rates)
                    fields["reported_model_list_rates"] = bool(rates)
        else:
            reason = {401: "key_rejected", 402: "payment_required", 403: "forbidden", 429: "rate_limited"}.get(status, "provider_error")
            if status == 400 and url in INVALID_KEY_READS and isinstance(body, dict) and (
                    body.get("api_key_invalid") is True or invalid_key(kind, body)):
                reason = "api_key_invalid"
            if status in (401, 403) and endpoint in ("usage", "costs") and isinstance(body, dict):
                # Provider scope evidence is separate from the actual HTTP refusal.
                hints = scope_hints(str(body.get("error", ""))[:4096])
                if endpoint == "costs" and (body.get("missing_usage_scope") is True or hints["missing_usage_scope"]):
                    attempt["scope_hint"] = "api.usage.read"
                if endpoint == "usage" and (body.get("admin_key_required") is True or hints["admin_key_required"]):
                    attempt["scope_hint"] = "admin_or_org_key"
            attempt["reason"] = reason
            if status == 429:
                attempt["retry_at"] = retry_at(retry, now + (time.monotonic() - started))
    except TimeoutError:
        attempt["reason"] = "request_timeout"
    except URLError as error:
        attempt["reason"] = "request_timeout" if isinstance(error.reason, TimeoutError) else "unreachable"
    except OSError:
        attempt["reason"] = "unreachable"
    except HTTPException:
        attempt["reason"] = "provider_error"
    except (ValueError, RecursionError):
        attempt["reason"] = "invalid_response"
    elapsed = time.monotonic() - started
    attempt["latency_ms"] = round(elapsed * 1000)
    if elapsed > timeout and not attempt["reason"]:
        attempt["reason"] = "request_timeout"
        return attempt, {}, None
    if not attempt["reason"]:
        attempt["ok_at"] = timestamp(now + elapsed)
        attempt["read_started_at"] = attempt["attempt_at"]
    return attempt, fields, account
