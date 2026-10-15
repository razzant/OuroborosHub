"""WHOOP API v2 client — OAuth2 Authorization Code flow with persistent tokens."""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

BASE = "https://api.prod.whoop.com/developer/v2"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"

DEFAULT_SCOPES = (
    "offline read:recovery read:sleep read:cycles read:workout "
    "read:profile read:body_measurement"
)

# In-process access token cache (refreshed when expired / 401 / 403).
_cached_access_token: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── Token persistence ─────────────────────────────────────────────


def _token_path(state_dir: str) -> str:
    return os.path.join(state_dir, "whoop_tokens.json")


def save_tokens(state_dir: str, access_token: str,
                refresh_token: str) -> None:
    """Persist tokens to the skill state directory."""
    os.makedirs(state_dir, exist_ok=True)
    data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "saved_at": _utc_now().isoformat(),
    }
    path = _token_path(state_dir)
    tmp = path + ".tmp"
    # Owner-only file: the refresh token grants access to the owner's health data.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def load_tokens(state_dir: str) -> dict[str, str]:
    """Load persisted tokens. Returns {} if none saved."""
    path = _token_path(state_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# ── OAuth2 Authorization Code flow ────────────────────────────────


def build_auth_url(client_id: str, redirect_uri: str,
                   scope: str = DEFAULT_SCOPES,
                   state: str = "ouroboros") -> str:
    """Build the OAuth2 authorization URL the user opens in a browser."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _token_headers() -> dict[str, str]:
    """Common headers for token endpoint requests (Cloudflare bypass)."""
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    }


def exchange_code(code: str, client_id: str, client_secret: str,
                  redirect_uri: str) -> dict[str, str]:
    """Exchange an authorization code for access + refresh tokens."""
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers=_token_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(
            f"Code exchange failed: HTTP {exc.code}. body={err_body}"
        ) from exc
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    if not access:
        raise RuntimeError(f"Code exchange returned no access_token: {data}")
    return {"access_token": access, "refresh_token": refresh or ""}


def refresh_access_token(refresh_token: str, client_id: str,
                         client_secret: str) -> dict[str, str]:
    """Exchange a refresh token for a fresh access + refresh token pair."""
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers=_token_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(
            f"Token refresh failed: HTTP {exc.code}. body={err_body}"
        ) from exc
    access = data.get("access_token")
    if not access:
        raise RuntimeError(f"Token refresh returned no access_token: {data}")
    return {
        "access_token": access,
        "refresh_token": data.get("refresh_token") or refresh_token,
    }


def get_access_token(state_dir: str, client_id: str,
                     client_secret: str) -> str:
    """Resolve a usable access token from cache or via refresh.

    1. Check in-process cache
    2. Load persisted tokens from state_dir
    3. If refresh_token exists — refresh and persist new pair
    4. Otherwise raise (user needs to run OAuth flow first)
    """
    global _cached_access_token
    if _cached_access_token:
        return _cached_access_token

    stored = load_tokens(state_dir)
    refresh = stored.get("refresh_token")

    if refresh:
        pair = refresh_access_token(refresh, client_id, client_secret)
        save_tokens(state_dir, pair["access_token"], pair["refresh_token"])
        _cached_access_token = pair["access_token"]
        return _cached_access_token

    # No refresh token — maybe a stale access token in storage
    access = stored.get("access_token")
    if access:
        _cached_access_token = access
        return _cached_access_token

    raise RuntimeError(
        "No tokens found. Run whoop_auth_url → whoop_exchange_code first."
    )


def invalidate_and_refresh(state_dir: str, client_id: str,
                           client_secret: str) -> str:
    """Force-refresh the access token (called on 401/403)."""
    global _cached_access_token
    _cached_access_token = None

    stored = load_tokens(state_dir)
    refresh = stored.get("refresh_token")
    if not refresh:
        raise RuntimeError(
            "Cannot refresh — no refresh_token saved. "
            "Run whoop_auth_url → whoop_exchange_code."
        )
    pair = refresh_access_token(refresh, client_id, client_secret)
    save_tokens(state_dir, pair["access_token"], pair["refresh_token"])
    _cached_access_token = pair["access_token"]
    return _cached_access_token


# ── HTTP helpers ──────────────────────────────────────────────────


def _get(path: str, token: str,
         params: dict[str, str] | None = None) -> Any:
    """HTTP GET with Bearer auth.  Returns parsed JSON."""
    url = f"{BASE}{path}"
    if params:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v})
        if qs:
            url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise urllib.error.HTTPError(
            exc.url, exc.code, f"API {path}: {err_body}", exc.headers, None
        ) from exc


def _get_with_retry(path: str, state_dir: str, client_id: str,
                    client_secret: str,
                    params: dict[str, str] | None = None) -> Any:
    """GET with automatic token refresh on 401/403."""
    access = get_access_token(state_dir, client_id, client_secret)
    try:
        return _get(path, access, params)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            access = invalidate_and_refresh(
                state_dir, client_id, client_secret)
            return _get(path, access, params)
        raise


# ── public data helpers ───────────────────────────────────────────


def check(state_dir: str, client_id: str,
          client_secret: str) -> dict:
    """Health-check: fetch user profile."""
    return _get_with_retry(
        "/user/profile/basic", state_dir, client_id, client_secret)


def _collect_pages(path: str, state_dir: str, client_id: str,
                   client_secret: str,
                   start: str, end: str, limit: int = 25) -> list[dict]:
    """Paginate through a WHOOP collection endpoint."""
    items: list[dict] = []
    next_token: str | None = None
    for _ in range(20):  # safety cap
        params: dict[str, str] = {
            "start": start,
            "end": end,
            "limit": str(limit),
        }
        if next_token:
            params["nextToken"] = next_token
        data = _get_with_retry(
            path, state_dir, client_id, client_secret, params)
        records = data.get("records", [])
        items.extend(records)
        next_token = data.get("next_token") or data.get("nextToken")
        if not next_token or not records:
            break
    return items


def recovery_zone(score) -> str | None:
    """Map a recovery score to its color zone.

    For numeric percentages in [0, 100]: red <=33, yellow <=66, else green.
    Missing, non-numeric, non-finite and out-of-range values have no zone.
    """
    if (isinstance(score, (int, float)) and not isinstance(score, bool)
            and 0 <= score <= 100):
        if score <= 33:
            return "red"
        if score <= 66:
            return "yellow"
        return "green"
    return None


def fetch_recovery(state_dir: str, client_id: str, client_secret: str,
                   days: int = 1) -> list[dict]:
    end = _iso(_utc_now())
    start = _iso(_utc_now() - timedelta(days=days))
    records = _collect_pages(
        "/recovery", state_dir, client_id, client_secret, start, end)
    for r in records:
        score = r.get("score")
        if score is not None:
            score["recovery_zone"] = recovery_zone(
                score.get("recovery_score"))
    return records


def fetch_sleep(state_dir: str, client_id: str, client_secret: str,
                days: int = 1) -> list[dict]:
    end = _iso(_utc_now())
    start = _iso(_utc_now() - timedelta(days=days))
    return _collect_pages(
        "/activity/sleep", state_dir, client_id, client_secret, start, end)


def fetch_workout(state_dir: str, client_id: str, client_secret: str,
                  days: int = 1) -> list[dict]:
    end = _iso(_utc_now())
    start = _iso(_utc_now() - timedelta(days=days))
    return _collect_pages(
        "/activity/workout", state_dir, client_id, client_secret, start, end)


def fetch_cycle(state_dir: str, client_id: str, client_secret: str,
                days: int = 1) -> list[dict]:
    end = _iso(_utc_now())
    start = _iso(_utc_now() - timedelta(days=days))
    return _collect_pages(
        "/cycle", state_dir, client_id, client_secret, start, end)


def summary(state_dir: str, client_id: str, client_secret: str,
            days: int = 1) -> dict:
    """One-shot summary: recovery + sleep + cycle for the period."""
    recovery = fetch_recovery(state_dir, client_id, client_secret, days)
    sleep = fetch_sleep(state_dir, client_id, client_secret, days)
    cycles = fetch_cycle(state_dir, client_id, client_secret, days)

    result: dict[str, Any] = {"source": "whoop", "days": days}

    if recovery:
        r = recovery[0]
        score = r.get("score") or {}
        recovery_score = score.get("recovery_score")
        result["recovery"] = {
            "recovery_score": recovery_score,
            "recovery_zone": recovery_zone(recovery_score),
            "user_calibrating": score.get("user_calibrating"),
            "hrv_rmssd_ms": score.get("hrv_rmssd_milli"),
            "resting_hr": score.get("resting_heart_rate"),
            "spo2_pct": score.get("spo2_percentage"),
            "skin_temp_celsius": score.get("skin_temp_celsius"),
        }

    if sleep:
        s = sleep[0]
        score = s.get("score") or {}
        stage = score.get("stage_summary", {})
        result["sleep"] = {
            "sleep_performance_pct": score.get(
                "sleep_performance_percentage"),
            "sleep_needed_ms": score.get("sleep_needed", {}).get(
                "baseline_milli"),
            "respiratory_rate": score.get("respiratory_rate"),
            "total_in_bed_ms": stage.get("total_in_bed_time_milli"),
            "total_light_ms": stage.get("total_light_sleep_time_milli"),
            "total_deep_ms": stage.get("total_slow_wave_sleep_time_milli"),
            "total_rem_ms": stage.get("total_rem_sleep_time_milli"),
            "total_awake_ms": stage.get("total_awake_time_milli"),
            "sleep_cycle_count": stage.get("sleep_cycle_count"),
            "disturbance_count": stage.get("disturbance_count"),
        }

    if cycles:
        c = cycles[0]
        sc = c.get("score") or {}
        result["strain"] = {
            "strain": sc.get("strain"),
            "kilojoule": sc.get("kilojoule"),
            "average_hr": sc.get("average_heart_rate"),
            "max_hr": sc.get("max_heart_rate"),
        }

    return result
