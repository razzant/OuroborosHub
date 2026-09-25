"""Google Calendar adapter: BYO Desktop OAuth client (PKCE), REST v3 on urllib, encrypted tokens.

Owner-provided client parameters come from Settings → Secrets; per-account tokens
live in ``state_dir/google_tokens.enc`` (Fernet, key derived from CALENDAR_TOKEN_KEY
with PBKDF2). Sync: first pass ``timeMin = now − 365 d`` and NO timeMax (the sync
token inherits the filters), then ``syncToken`` only; 410 → the caller wipes this
calendar's external cache and starts over.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as pysecrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from model import AVAIL_BUSY, PROVIDER_GOOGLE, account_id as make_account_id, calendar_id as make_calendar_id, get_tz, iso_utc, now_utc, parse_stored
from model import tz_name as zone_name
from ops import ProviderError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
SCOPES = ("https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.calendarlist")
HTTP_TIMEOUT = 25
INITIAL_PAST_DAYS = 365
TOKENS_FILE = "google_tokens.enc"
SALT_FILE = "google_tokens.salt"
PENDING_PREFIX = "google_oauth_pending:"


# ── token vault ─────────────────────────────────────────────────────

def _fernet(state_dir: str, passphrase: str):
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except Exception as exc:
        raise ProviderError("unsupported", f"библиотека cryptography недоступна: {exc}")
    if not passphrase:
        raise ProviderError("auth", "нет CALENDAR_TOKEN_KEY в Settings → Secrets — им шифруются токены Google")
    salt_path = os.path.join(state_dir, SALT_FILE)
    try:
        with open(salt_path, "rb") as fh:
            salt = fh.read()
    except OSError:
        salt = os.urandom(16)
        fd = os.open(salt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(salt)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
    return Fernet(key)


def load_tokens(state_dir: str, passphrase: str) -> Dict[str, Dict[str, Any]]:
    path = os.path.join(state_dir, TOKENS_FILE)
    if not os.path.exists(path):
        return {}
    f = _fernet(state_dir, passphrase)
    with open(path, "rb") as fh:
        blob = fh.read()
    try:
        return json.loads(f.decrypt(blob).decode("utf-8"))
    except Exception:
        raise ProviderError("auth", "не удалось расшифровать токены Google: CALENDAR_TOKEN_KEY изменился? Переподключи аккаунты")


def save_tokens(state_dir: str, passphrase: str, tokens: Dict[str, Dict[str, Any]]) -> None:
    f = _fernet(state_dir, passphrase)
    path = os.path.join(state_dir, TOKENS_FILE)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(f.encrypt(json.dumps(tokens).encode("utf-8")))
    os.replace(tmp, path)


# ── OAuth (PKCE, loopback callback on the host) ─────────────────────

def start_auth(store, secrets: Dict[str, Any], redirect_uri: str) -> Dict[str, Any]:
    client_id = str(secrets.get("GOOGLE_CALENDAR_CLIENT_ID") or "").strip()
    if not client_id:
        raise ProviderError("auth", "нет GOOGLE_CALENDAR_CLIENT_ID в Settings → Secrets")
    verifier = base64.urlsafe_b64encode(os.urandom(48)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    state = pysecrets.token_urlsafe(24)
    for key, value in list(store.all_settings().items()):   # abandoned logins do not pile up
        if key.startswith(PENDING_PREFIX):
            created = parse_stored((value or {}).get("created_at")) if isinstance(value, dict) else None
            if not isinstance(value, dict) or created is None or now_utc() - created > timedelta(minutes=30):
                store.delete_setting(key)
    store.set_setting(PENDING_PREFIX + state, {"verifier": verifier, "redirect_uri": redirect_uri, "created_at": iso_utc(now_utc())})
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(SCOPES),
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "access_type": "offline", "prompt": "consent"}
    return {"auth_url": AUTH_URL + "?" + urllib.parse.urlencode(params), "state": state, "redirect_uri": redirect_uri}


def finish_auth(store, secrets: Dict[str, Any], state_dir: str, code: str, state: str) -> Dict[str, Any]:
    pending = store.get_setting(PENDING_PREFIX + state)
    if not pending:
        raise ProviderError("auth", "неизвестный или устаревший state в ответе Google — начни подключение заново")
    created = parse_stored(pending.get("created_at"))
    if created and now_utc() - created > timedelta(minutes=30):
        raise ProviderError("auth", "ссылка для входа устарела (30 минут) — начни подключение заново")
    client_id = str(secrets.get("GOOGLE_CALENDAR_CLIENT_ID") or "").strip()
    client_secret = str(secrets.get("GOOGLE_CALENDAR_CLIENT_SECRET") or "").strip()
    body = {"client_id": client_id, "code": code, "code_verifier": pending["verifier"], "grant_type": "authorization_code",
            "redirect_uri": pending["redirect_uri"]}
    if client_secret:
        body["client_secret"] = client_secret
    data = _token_request(body)
    tokens = load_tokens(state_dir, str(secrets.get("CALENDAR_TOKEN_KEY") or ""))
    adapter = GoogleAdapter("pending", data["access_token"], data.get("refresh_token", ""), now_utc() + timedelta(seconds=int(data.get("expires_in") or 3600)),
                            client_id, client_secret, None)
    email = adapter.primary_email()
    if not email:
        raise ProviderError("auth", "Google не вернул основной календарь — не удалось определить аккаунт")
    prev = tokens.get(email) or {}
    tokens[email] = {"access_token": data["access_token"], "refresh_token": data.get("refresh_token") or prev.get("refresh_token", ""),
                     "expires_at": iso_utc(now_utc() + timedelta(seconds=int(data.get("expires_in") or 3600))), "scope": data.get("scope", "")}
    save_tokens(state_dir, str(secrets.get("CALENDAR_TOKEN_KEY") or ""), tokens)
    store.delete_setting(PENDING_PREFIX + state)
    return {"email": email, "account_id": make_account_id(PROVIDER_GOOGLE, email)}


def forget_account(state_dir: str, passphrase: str, email: str) -> bool:
    """Drop the stored tokens of one account (disconnect): reload_google must not resurrect it."""
    try:
        tokens = load_tokens(state_dir, passphrase)
    except ProviderError:
        return False
    if email not in tokens:
        return False
    del tokens[email]
    save_tokens(state_dir, passphrase, tokens)
    return True


def _token_request(body: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(body).encode("ascii"), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        code = ""
        try:
            code = str(json.loads(text).get("error") or "")
        except ValueError:
            pass
        if code in ("invalid_grant", "invalid_client", "unauthorized_client", "access_denied"):
            raise ProviderError("auth", f"Google отклонил вход ({code}); при invalid_grant нужно переподключить аккаунт", exc.code)
        if exc.code >= 500:
            raise ProviderError("server", f"сервер Google OAuth недоступен ({exc.code})", exc.code)
        raise ProviderError("http", f"Google OAuth: HTTP {exc.code} {code}", exc.code)
    except urllib.error.URLError as exc:
        raise ProviderError("network", f"нет связи с Google OAuth: {exc.reason}")
    except TimeoutError:
        raise ProviderError("network", "таймаут Google OAuth")


# ── adapter ─────────────────────────────────────────────────────────

class GoogleAdapter:
    provider = PROVIDER_GOOGLE

    def __init__(self, email: str, access_token: str, refresh_token: str, expires_at: datetime, client_id: str, client_secret: str, persist):
        self.email = email
        self.account_id = make_account_id(PROVIDER_GOOGLE, email)
        self.access_token, self.refresh_token, self.expires_at = access_token, refresh_token, expires_at
        self.client_id, self.client_secret = client_id, client_secret
        self._persist = persist   # callable(tokens dict) or None

    @classmethod
    def for_account(cls, secrets: Dict[str, Any], state_dir: str, email: str) -> Optional["GoogleAdapter"]:
        passphrase = str(secrets.get("CALENDAR_TOKEN_KEY") or "")
        client_id = str(secrets.get("GOOGLE_CALENDAR_CLIENT_ID") or "").strip()
        if not passphrase or not client_id:
            return None
        tokens = load_tokens(state_dir, passphrase)
        entry = tokens.get(email)
        if not entry or not entry.get("refresh_token"):
            return None
        expires = parse_stored(entry.get("expires_at")) or now_utc()

        def persist(update: Dict[str, Any]) -> None:
            fresh = load_tokens(state_dir, passphrase)
            fresh[email] = {**fresh.get(email, {}), **update}
            save_tokens(state_dir, passphrase, fresh)

        return cls(email, entry.get("access_token", ""), entry["refresh_token"], expires, client_id, str(secrets.get("GOOGLE_CALENDAR_CLIENT_SECRET") or ""), persist)

    # transport
    def _refresh(self) -> None:
        if not self.refresh_token:
            raise ProviderError("auth", "нет refresh token — переподключи аккаунт Google")
        body = {"client_id": self.client_id, "grant_type": "refresh_token", "refresh_token": self.refresh_token}
        if self.client_secret:
            body["client_secret"] = self.client_secret
        data = _token_request(body)
        self.access_token = data["access_token"]
        self.expires_at = now_utc() + timedelta(seconds=int(data.get("expires_in") or 3600))
        if self._persist:
            self._persist({"access_token": self.access_token, "expires_at": iso_utc(self.expires_at)})

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None, _retry: bool = True) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        if not self.access_token or now_utc() >= self.expires_at - timedelta(seconds=60):
            self._refresh()
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")}, doseq=True)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.access_token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            text = ""
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            reason = ""
            try:
                errs = (json.loads(text).get("error") or {}).get("errors") or []
                reason = str(errs[0].get("reason") or "") if errs else ""
            except (ValueError, AttributeError):
                pass
            if exc.code == 401 and _retry:
                self._refresh()
                return self._request(method, path, params, body, headers, _retry=False)
            if exc.code == 401:
                raise ProviderError("auth", "Google не принял токен даже после обновления — переподключи аккаунт", 401)
            if exc.code == 403 and reason in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"):
                raise ProviderError("server", f"лимит Google Calendar API ({reason}); повтор позже", 403)
            if exc.code == 403:
                raise ProviderError("forbidden", f"Google отказал в доступе ({reason or 403})", 403)
            if exc.code == 404:
                raise ProviderError("not_found", "событие или календарь не найдены в Google (404)", 404)
            if exc.code == 410:
                raise ProviderError("gone", "syncToken устарел (410): нужна полная пересинхронизация календаря", 410)
            if exc.code == 412:
                raise ProviderError("conflict", "событие изменилось в Google (412): перечитай и повтори", 412)
            if exc.code == 429 or exc.code >= 500:
                raise ProviderError("server", f"Google временно не отвечает ({exc.code})", exc.code)
            raise ProviderError("http", f"Google Calendar API: HTTP {exc.code} {reason} {text[:160]}", exc.code)
        except urllib.error.URLError as exc:
            raise ProviderError("network", f"нет связи с Google: {exc.reason}")
        except TimeoutError:
            raise ProviderError("network", "таймаут Google Calendar API")

    # identity / calendars
    def primary_email(self) -> str:
        _, _, data = self._request("GET", "/users/me/calendarList", {"minAccessRole": "owner", "maxResults": 50})
        for item in data.get("items", []):
            if item.get("primary"):
                return str(item.get("id") or "")
        items = data.get("items", [])
        return str(items[0].get("id") or "") if items else ""

    def list_calendars(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        token = None
        while True:
            _, _, data = self._request("GET", "/users/me/calendarList", {"maxResults": 250, "pageToken": token, "showHidden": "true"})
            for item in data.get("items", []):
                role = str(item.get("accessRole") or "reader")
                out.append({"id": make_calendar_id(PROVIDER_GOOGLE, self.email, str(item["id"])), "account_id": self.account_id, "provider": PROVIDER_GOOGLE,
                            "external_id": str(item["id"]), "href": "", "name": str(item.get("summaryOverride") or item.get("summary") or item["id"]),
                            "tz": str(item.get("timeZone") or ""), "writable": role in ("owner", "writer"), "access_role": role,
                            "is_primary": bool(item.get("primary")), "default_reminders": item.get("defaultReminders") or []})
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    # events
    def fetch(self, calendar: Dict[str, Any], cursor: str = "", tz=None) -> Tuple[List[Dict[str, Any]], str, str, List[str]]:
        """Incremental when a sync token exists; otherwise initial pass with timeMin only (no timeMax)."""
        tz = tz or timezone.utc
        cal_path = f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events"
        rows: List[Dict[str, Any]] = []
        cancelled: List[str] = []
        page = None
        next_sync = ""
        while True:
            params: Dict[str, Any] = {"maxResults": 2500, "singleEvents": "false", "pageToken": page}
            if cursor:
                params["syncToken"] = cursor
                params["showDeleted"] = "true"
            else:
                params["timeMin"] = (now_utc() - timedelta(days=INITIAL_PAST_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
                params["showDeleted"] = "false"
            _, _, data = self._request("GET", cal_path, params)
            for item in data.get("items", []):
                if str(item.get("status") or "") == "cancelled":
                    cancelled.append(str(item.get("id") or ""))
                    if not item.get("recurringEventId"):
                        continue
                row = gevent_to_row(item, calendar["id"], tz)
                if row is not None:
                    rows.append(row)
            page = data.get("nextPageToken")
            if not page:
                next_sync = str(data.get("nextSyncToken") or "")
                break
        return rows, next_sync, "google_sync_token", cancelled

    def create(self, calendar: Dict[str, Any], event: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        body = row_to_gevent(event, mute=bool(payload.get("mute_provider_reminders")))
        body["id"] = _google_id(event)
        attendees = json.loads(event.get("attendees_json") or "[]")
        params = {"sendUpdates": "all" if (payload.get("send_updates") and attendees) else "none"}
        try:
            _, headers, data = self._request("POST", f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events", params, body)
        except ProviderError as exc:
            if exc.kind == "conflict" or (exc.kind == "http" and exc.status == 409):
                # id already used: a retry after a lost response — read it back instead of duplicating; a cancelled
                # leftover (a copy removed earlier, same stable id) is revived with the new content
                _, _, data = self._request("GET", f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/{body['id']}")
                if str(data.get("status") or "") == "cancelled":
                    revive = {k: v for k, v in body.items() if k != "id"}
                    _, _, data = self._request("PUT", f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/{body['id']}", params, revive)
                return {"external_id": data.get("id", body["id"]), "href": "", "etag": str(data.get("etag") or "")}
            raise
        return {"external_id": str(data.get("id") or body["id"]), "href": "", "etag": str(data.get("etag") or headers.get("etag") or "")}

    def update(self, calendar: Dict[str, Any], event: Dict[str, Any], expected_etag: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        external_id = str(event.get("external_id") or "")
        if not external_id and event.get("master_id"):
            external_id = self._instance_id(calendar, event, payload)
            if not external_id:
                raise ProviderError("retry", "экземпляр серии у Google ещё не найден (мастер ещё не записан?) — повторим позже")
        if not external_id:
            return self.create(calendar, event, payload)
        body = row_to_gevent(event, mute=bool(payload.get("mute_provider_reminders")))
        attendees = json.loads(event.get("attendees_json") or "[]")
        params = {"sendUpdates": "all" if (payload.get("send_updates") and attendees) else "none"}
        headers = {"If-Match": expected_etag} if expected_etag else {}
        _, resp_headers, data = self._request("PATCH", f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/{urllib.parse.quote(external_id, safe='')}",
                                              params, body, headers)
        return {"etag": str(data.get("etag") or resp_headers.get("etag") or ""), "external_id": str(data.get("id") or external_id)}

    def delete(self, calendar: Dict[str, Any], event: Dict[str, Any], expected_etag: str, payload: Optional[Dict[str, Any]] = None) -> None:
        external_id = str(event.get("external_id") or "")
        if not external_id:
            return
        attendees = json.loads(event.get("attendees_json") or "[]")
        send = "all" if ((payload or {}).get("send_updates") and attendees) else "none"
        headers = {"If-Match": expected_etag} if expected_etag else {}
        self._request("DELETE", f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/{urllib.parse.quote(external_id, safe='')}",
                      {"sendUpdates": send}, None, headers)

    def respond(self, calendar: Dict[str, Any], event: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        external_id = str(event.get("external_id") or "")
        response = str(payload.get("response") or event.get("my_response") or "")
        if response not in ("accepted", "declined", "tentative"):
            raise ProviderError("unsupported", "response: accepted | declined | tentative")
        if not external_id and event.get("master_id"):
            external_id = self._instance_id(calendar, event)
            if not external_id:
                raise ProviderError("retry", "экземпляр серии у Google ещё не найден — ответ подождёт")
        if not external_id:
            raise ProviderError("unsupported", "ответить можно только на событие, уже записанное в Google")
        attendees = json.loads(event.get("attendees_json") or "[]")
        hit = False
        for att in attendees:
            if att.get("self") or str(att.get("email") or "").lower() == self.email.lower():
                att["responseStatus"] = response
                hit = True
        if not hit:
            raise ProviderError("unsupported", "среди участников события нет этого аккаунта — ответить нечем")
        body = {"attendees": [{"email": a.get("email"), "responseStatus": a.get("responseStatus") or a.get("status") or "needsAction"} for a in attendees if a.get("email")]}
        params = {"sendUpdates": "all" if payload.get("notify_organizer") else "none"}
        _, headers, data = self._request("PATCH", f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/{urllib.parse.quote(external_id, safe='')}", params, body)
        return {"etag": str(data.get("etag") or headers.get("etag") or "")}

    def set_default_reminders(self, calendar: Dict[str, Any], overrides: List[Dict[str, Any]]) -> None:
        """calendarList.patch — the user's own default reminders for this calendar (20 A: silence provider duplicates)."""
        self._request("PATCH", f"/users/me/calendarList/{urllib.parse.quote(calendar['external_id'], safe='')}", None, {"defaultReminders": overrides})

    def _instance_id(self, calendar: Dict[str, Any], event: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> str:
        """Google instance id for a stored exception: ``<masterId>_<originalStart>``; verified via events.instances on miss.
        After a series shift the instance is still filed under its previous originalStart — that slot is tried first."""
        prev = (payload or {}).get("previous_recurrence_id")
        if prev:
            found = self._instance_id(calendar, {**event, "recurrence_id": prev})
            if found:
                return found
        master_ext = str(event.get("master_external_id") or "")
        occ = parse_stored(event.get("recurrence_id"))
        if not master_ext or occ is None:
            return ""
        all_day = bool(event.get("all_day"))
        local_date = occ.astimezone(get_tz(event.get("tz") or "")).date()
        stamp = local_date.strftime("%Y%m%d") if all_day else occ.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = f"{master_ext}_{stamp}"
        try:
            self._request("GET", f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/{urllib.parse.quote(candidate, safe='')}")
            return candidate
        except ProviderError as exc:
            if exc.kind != "not_found":
                raise
        base = f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/{urllib.parse.quote(master_ext, safe='')}/instances"
        if all_day:
            _, _, data = self._request("GET", base, {"timeMin": f"{local_date - timedelta(days=1)}T00:00:00Z", "timeMax": f"{local_date + timedelta(days=2)}T00:00:00Z",
                                                     "maxResults": 10})
            for item in data.get("items", []):
                if str((item.get("originalStartTime") or {}).get("date") or "") == local_date.isoformat():
                    return str(item.get("id") or "")
            return ""
        _, _, data = self._request("GET", base, {"originalStart": occ.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "maxResults": 1})
        items = data.get("items", [])
        return str(items[0].get("id") or "") if items else ""


# ── conversions ─────────────────────────────────────────────────────

def _google_id(event: Dict[str, Any]) -> str:
    """Stable per local event: uuid hex is inside Google's [a-v0-9]{5,1024} alphabet."""
    seed = str(event.get("uid") or event.get("id") or uuid.uuid4())
    return "o" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:31]


def gevent_to_row(item: Dict[str, Any], calendar_id: str, default_tz) -> Optional[Dict[str, Any]]:
    start = item.get("start") or {}
    end = item.get("end") or {}
    if not start and str(item.get("status") or "") == "cancelled" and item.get("originalStartTime"):
        # Google returns cancelled instances with only id/status/recurringEventId/originalStartTime
        start = dict(item.get("originalStartTime") or {})
        end = dict(start)
    all_day = "date" in start
    tz_name = str(start.get("timeZone") or "")
    tz = get_tz(tz_name) if tz_name else default_tz
    if not tz_name:
        tz_name = zone_name(tz)   # all-day rows remember the zone their dates were read in, so EXDATE/UNTIL round-trip
    try:
        if all_day:
            s = datetime.fromisoformat(start["date"]).replace(tzinfo=tz)
            e = datetime.fromisoformat(end.get("date") or start["date"]).replace(tzinfo=tz)
        else:
            s = datetime.fromisoformat(str(start.get("dateTime")).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(end.get("dateTime") or start.get("dateTime")).replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError):
        return None
    rrule = exdates = rdates = ""
    for line in item.get("recurrence") or []:
        text = str(line)
        if text.startswith("RRULE:"):
            rrule = text[6:]
        elif text.startswith("EXDATE"):
            exdates = ",".join(filter(None, [exdates, _ical_dates(text, tz)]))
        elif text.startswith("RDATE"):
            rdates = ",".join(filter(None, [rdates, _ical_dates(text, tz)]))
    attendees = [{"email": a.get("email", ""), "name": a.get("displayName", ""), "status": a.get("responseStatus", ""), "self": bool(a.get("self")),
                  "role": "optional" if a.get("optional") else ""} for a in item.get("attendees") or []]
    my_response = next((a["status"] for a in attendees if a.get("self")), "")
    rem = item.get("reminders") or {}
    reminders = [int(o.get("minutes") or 0) for o in rem.get("overrides") or [] if o.get("method") in ("popup", "email", None)]
    rec_key = ""
    orig = item.get("originalStartTime") or {}
    if item.get("recurringEventId"):
        try:
            if "date" in orig:
                rec_key = iso_utc(datetime.fromisoformat(orig["date"]).replace(tzinfo=tz))
            else:
                rec_key = iso_utc(datetime.fromisoformat(str(orig.get("dateTime")).replace("Z", "+00:00")))
        except (ValueError, TypeError):
            rec_key = ""
    transparency = str(item.get("transparency") or "opaque")
    return {
        "calendar_id": calendar_id, "uid": str(item.get("iCalUID") or ""), "external_id": str(item.get("id") or ""), "href": "",
        "etag": str(item.get("etag") or ""), "title": str(item.get("summary") or ""), "description": str(item.get("description") or ""),
        "location": str(item.get("location") or ""), "start_utc": iso_utc(s), "end_utc": iso_utc(e), "tz": tz_name, "all_day": all_day,
        "rrule": rrule, "exdates": exdates, "rdates": rdates, "recurrence_id": rec_key, "master_external_id": str(item.get("recurringEventId") or ""),
        "status": "cancelled" if str(item.get("status") or "") == "cancelled" else "confirmed",
        "organizer": str((item.get("organizer") or {}).get("email") or ""), "attendees_json": json.dumps(attendees, ensure_ascii=False),
        "my_response": my_response, "reminders_json": json.dumps(sorted(set(reminders))), "origin": "external",
        "availability": "free" if transparency == "transparent" else AVAIL_BUSY, "sync_state": "synced",
        "raw_payload": json.dumps(item, ensure_ascii=False) if not rec_key else "",
    }


def _ical_dates(line: str, tz) -> str:
    """``EXDATE;TZID=Europe/Moscow:20260928T190000,20261005T190000`` → ISO-UTC csv."""
    head, _, values = line.partition(":")
    tzid = ""
    for part in head.split(";")[1:]:
        if part.upper().startswith("TZID="):
            tzid = part[5:]
    zone = get_tz(tzid) if tzid else tz
    out = []
    for v in values.split(","):
        v = v.strip()
        try:
            if v.endswith("Z"):
                dt = datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            elif "T" in v:
                dt = datetime.strptime(v, "%Y%m%dT%H%M%S").replace(tzinfo=zone)
            else:
                dt = datetime.strptime(v, "%Y%m%d").replace(tzinfo=zone)
        except ValueError:
            continue
        out.append(iso_utc(dt))
    return ",".join(out)


def row_to_gevent(event: Dict[str, Any], mute: bool = False) -> Dict[str, Any]:
    """``mute`` = «напоминает Уроборос» is on for this calendar: the provider keeps no alerts of its own."""
    start = parse_stored(event.get("start_utc"))
    end = parse_stored(event.get("end_utc"))
    if start is None or end is None:
        raise ProviderError("parse", "у события нет времени")
    tz_name = str(event.get("tz") or "") or "UTC"
    tz = get_tz(tz_name)
    body: Dict[str, Any] = {"summary": str(event.get("title") or ""), "description": str(event.get("description") or ""),
                            "location": str(event.get("location") or "")}
    if event.get("all_day"):
        body["start"] = {"date": start.astimezone(tz).date().isoformat()}
        body["end"] = {"date": end.astimezone(tz).date().isoformat()}
    else:
        body["start"] = {"dateTime": start.astimezone(tz).isoformat(), "timeZone": tz_name}
        body["end"] = {"dateTime": end.astimezone(tz).isoformat(), "timeZone": tz_name}
    recurrence = []
    if event.get("rrule"):
        recurrence.append("RRULE:" + str(event["rrule"]))
    if event.get("exdates"):
        stamps = [parse_stored(x) for x in str(event["exdates"]).split(",") if x]
        if event.get("all_day"):
            stamps = [x.astimezone(tz).strftime("%Y%m%d") for x in stamps if x]
            if stamps:
                recurrence.append("EXDATE;VALUE=DATE:" + ",".join(stamps))
        else:
            stamps = [x.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") for x in stamps if x]
            if stamps:
                recurrence.append("EXDATE:" + ",".join(stamps))
    if event.get("rdates"):
        extra = [parse_stored(x) for x in str(event["rdates"]).split(",") if x]
        if event.get("all_day"):
            vals = [x.astimezone(tz).strftime("%Y%m%d") for x in extra if x]
            if vals:
                recurrence.append("RDATE;VALUE=DATE:" + ",".join(vals))
        else:
            vals = [x.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") for x in extra if x]
            if vals:
                recurrence.append("RDATE:" + ",".join(vals))
    if not event.get("master_id") and (recurrence or event.get("rrule") == ""):
        body["recurrence"] = recurrence   # instances (exceptions) never carry recurrence: Google rejects it
    body["status"] = "cancelled" if str(event.get("status") or "") == "cancelled" else "confirmed"
    body["transparency"] = "transparent" if event.get("availability") == "free" else "opaque"
    try:
        offsets = json.loads(event.get("reminders_json") or "[]")
    except ValueError:
        offsets = []
    if mute:
        body["reminders"] = {"useDefault": False, "overrides": []}
    elif offsets:
        body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": int(m)} for m in offsets][:5]}
    else:
        body["reminders"] = {"useDefault": True}
    try:
        attendees = json.loads(event.get("attendees_json") or "[]")
    except ValueError:
        attendees = []
    if attendees:
        body["attendees"] = [{"email": a.get("email"), **({"responseStatus": a.get("status")} if a.get("status") else {})} for a in attendees if a.get("email")]
    return body
