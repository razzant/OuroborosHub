"""Google Calendar adapter: BYO Desktop OAuth client (PKCE), REST v3 on urllib, encrypted tokens.

Owner-provided client parameters come from Settings → Secrets; per-account tokens
live in the shared skill credential vault ``state_dir/google_tokens.enc`` (Fernet;
a generated local key for new installs, or the retained CALENDAR_TOKEN_KEY/PBKDF2 key). Sync: first pass ``timeMin = now − 365 d`` and NO timeMax (the sync
token inherits the filters), then ``syncToken`` only; 410 → the caller wipes this
calendar's external cache and starts over.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import secrets as pysecrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from model import AVAIL_BUSY, PROVIDER_GOOGLE, account_id as make_account_id, calendar_id as make_calendar_id, get_tz, iso_utc, now_utc, own_reminders, parse_stored
from model import tz_name as zone_name
from ops import ProviderError, created_content_matches
from credentials import (
    TOKENS_FILE as TOKENS_FILE, SALT_FILE as SALT_FILE, KEY_FILE as KEY_FILE,
    load_tokens, save_tokens as save_tokens, _mutate_tokens, _write_tokens as _write_tokens, _fernet as _fernet,
)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
SCOPES = ('openid', 'email', "https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.calendarlist",
          'https://www.googleapis.com/auth/calendar.calendars', 'https://www.googleapis.com/auth/calendar.acls')
USERINFO_URL = 'https://openidconnect.googleapis.com/v1/userinfo'
HTTP_TIMEOUT = 25
INITIAL_PAST_DAYS = 365
PENDING_PREFIX = "google_oauth_pending:"


# ── OAuth (PKCE, loopback callback on the host) ─────────────────────

def client_credentials(secrets, entry=None):
    """One installation default, retaining the client that actually issued each grant."""
    pairs = [(str(secrets.get(prefix + '_CLIENT_ID') or '').strip(), str(secrets.get(prefix + '_CLIENT_SECRET') or ''))
             for prefix in ('GOOGLE_OAUTH', 'GOOGLE_CALENDAR')]
    if entry is not None:
        if entry.get('client_id'):
            return next((pair for pair in pairs if pair[0] == entry['client_id']),
                        (entry['client_id'], str(entry.get('client_secret') or '')))
        pairs.reverse()  # legacy calendar tokens were issued to the calendar-specific client
    return next((pair for pair in pairs if pair[0]), ('', ''))


def start_auth(store, secrets: Dict[str, Any], redirect_uri: str) -> Dict[str, Any]:
    client_id, _ = client_credentials(secrets)
    if not client_id:
        raise ProviderError("auth", "нет GOOGLE_OAUTH_CLIENT_ID (или прежнего GOOGLE_CALENDAR_CLIENT_ID) в Settings → Secrets")
    verifier = base64.urlsafe_b64encode(os.urandom(48)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    state = base64.urlsafe_b64encode(json.dumps({'v': 1, 'skill': 'calendar', 'nonce': pysecrets.token_urlsafe(24)},
                                              separators=(',', ':')).encode()).decode().rstrip('=')
    for key, value in list(store.all_settings().items()):   # abandoned logins do not pile up
        if key.startswith(PENDING_PREFIX):
            created = parse_stored((value or {}).get("created_at")) if isinstance(value, dict) else None
            if not isinstance(value, dict) or created is None or now_utc() - created > timedelta(minutes=30):
                store.delete_setting(key)
    store.set_setting(PENDING_PREFIX + state, {"verifier": verifier, "redirect_uri": redirect_uri,
                                             'client_id': client_id, "created_at": iso_utc(now_utc())})
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(SCOPES),
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "access_type": "offline", "prompt": "consent"}
    return {"auth_url": AUTH_URL + "?" + urllib.parse.urlencode(params), "state": state, "redirect_uri": redirect_uri}


def finish_auth(store, secrets: Dict[str, Any], state_dir: str, code: str, state: str) -> Dict[str, Any]:
    pending = store.pop_setting(PENDING_PREFIX + state)
    if not pending:
        raise ProviderError("auth", "неизвестный или устаревший state в ответе Google — начни подключение заново")
    created = parse_stored(pending.get("created_at"))
    if created and now_utc() - created > timedelta(minutes=30):
        raise ProviderError("auth", "ссылка для входа устарела (30 минут) — начни подключение заново")
    client_id, client_secret = client_credentials(secrets, {'client_id': pending.get('client_id')})
    if not client_id:
        raise ProviderError('auth', 'OAuth-клиент больше не настроен; начни вход заново')
    body = {"client_id": client_id, "code": code, "code_verifier": pending["verifier"], "grant_type": "authorization_code",
            "redirect_uri": pending["redirect_uri"]}
    if client_secret:
        body["client_secret"] = client_secret
    data = _token_request(body)
    adapter = GoogleAdapter("pending", data["access_token"], data.get("refresh_token", ""), now_utc() + timedelta(seconds=int(data.get("expires_in") or 3600)),
                            client_id, client_secret, None)
    email = adapter.primary_email()
    if not email:
        raise ProviderError("auth", "Google не подтвердил личность аккаунта")
    subject = (getattr(adapter, '_identity', {}) or {}).get('sub')
    connected = {}
    def connect(tokens):
        key = next((key for key, value in tokens.items() if subject and value.get('subject') == subject), email)
        if subject and tokens.get(key, {}).get('subject') not in (None, subject):
            key = 'oidc:' + subject
        prev = tokens.get(key) or {}
        if prev.get('client_id') not in (None, client_id):
            prev = {}  # a refresh token belongs to its issuing OAuth client
        tokens[key] = {"access_token": data["access_token"], "refresh_token": data.get("refresh_token") or prev.get("refresh_token", ""),
                         "expires_at": iso_utc(now_utc() + timedelta(seconds=int(data.get("expires_in") or 3600))),
                         "scope": data.get("scope", ""), 'email': email, 'subject': subject, 'provider': PROVIDER_GOOGLE,
                         'client_id': client_id, 'client_secret': client_secret, "generation": uuid.uuid4().hex}
        connected['key'] = key
        connected['offline_access'] = bool(tokens[key].get('refresh_token'))
        return True
    _mutate_tokens(state_dir, str(secrets.get("CALENDAR_TOKEN_KEY") or ""), connect)
    return {"email": email, "account_id": make_account_id(PROVIDER_GOOGLE, connected['key']),
            'offline_access': connected['offline_access']}


def cancel_auth(store, state):
    if not store.pop_setting(PENDING_PREFIX + str(state)):
        raise ProviderError('auth', 'Неизвестный или уже завершённый OAuth state')


def forget_account(state_dir: str, passphrase: str, email: str) -> bool:
    """Drop the stored tokens of one account (disconnect): reload_google must not resurrect it."""
    def forget(tokens):
        if email not in tokens:
            return False
        del tokens[email]
        return True
    try:
        return _mutate_tokens(state_dir, passphrase, forget)
    except ProviderError:
        return False


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
    except (http.client.HTTPException, OSError) as exc:
        raise ProviderError('network', f'Соединение с Google OAuth прервано: {type(exc).__name__}') from exc


# ── adapter ─────────────────────────────────────────────────────────

class GoogleAdapter:
    provider = PROVIDER_GOOGLE

    def __init__(self, email: str, access_token: str, refresh_token: str, expires_at: datetime, client_id: str, client_secret: str, persist,
                 *, credential_key=None, scopes=None):
        self.email = email
        self.credential_key = credential_key or email
        self.account_id = make_account_id(PROVIDER_GOOGLE, self.credential_key)
        self.scopes = set(str(scopes).split()) if scopes else None
        self._identity = {}
        self.access_token, self.refresh_token, self.expires_at = access_token, refresh_token, expires_at
        self.client_id, self.client_secret = client_id, client_secret
        self._persist = persist   # callable(tokens dict) or None

    @classmethod
    def for_account(cls, secrets: Dict[str, Any], state_dir: str, email: str, *, tokens=None) -> Optional["GoogleAdapter"]:
        passphrase = str(secrets.get("CALENDAR_TOKEN_KEY") or "")
        tokens = load_tokens(state_dir, passphrase) if tokens is None else tokens
        entry = tokens.get(email)
        if not entry or entry.get('provider', PROVIDER_GOOGLE) != PROVIDER_GOOGLE or not (entry.get('access_token') or entry.get("refresh_token")):
            return None
        client_id, client_secret = client_credentials(secrets, entry)
        if not client_id:
            return None
        expires = parse_stored(entry.get("expires_at")) or now_utc()

        def persist(update: Dict[str, Any]) -> None:
            def refresh(fresh):
                current = fresh.get(email)
                if not current or (current.get('generation'), current.get('refresh_token')) != (entry.get('generation'), entry.get('refresh_token')):
                    return False  # a disconnect or a new OAuth connection wins
                fresh[email] = {**current, **update}
                return True
            _mutate_tokens(state_dir, passphrase, refresh)

        return cls(entry.get('email') or email, entry.get("access_token", ""), entry.get("refresh_token", ''), expires,
                   client_id, client_secret, persist, credential_key=email, scopes=entry.get('scope'))

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
        except (http.client.HTTPException, OSError) as exc:
            raise ProviderError('network', f'Соединение с Google Calendar прервано: {type(exc).__name__}') from exc

    # identity / calendars
    def primary_email(self) -> str:
        """Identity comes from Google userinfo, never an arbitrary calendar resource."""
        request = urllib.request.Request(USERINFO_URL, headers={'Authorization': 'Bearer ' + self.access_token})
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                identity = json.loads(response.read().decode('utf-8'))
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            raise ProviderError('auth', 'Не удалось подтвердить личность Google; требуется вход с openid/email') from exc
        if not identity.get('sub') or not identity.get('email') or identity.get('email_verified') is not True:
            raise ProviderError('auth', 'Google не вернул подтверждённые sub/email')
        self._identity = {'sub': str(identity['sub']), 'email': str(identity['email'])}
        return self._identity['email']

    def list_calendars(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        token = None
        while True:
            _, _, data = self._request("GET", "/users/me/calendarList", {"maxResults": 250, "pageToken": token, "showHidden": "true"})
            for item in data.get("items", []):
                role = str(item.get("accessRole") or "reader")
                out.append({"id": make_calendar_id(PROVIDER_GOOGLE, self.credential_key, str(item["id"])), "account_id": self.account_id, "provider": PROVIDER_GOOGLE,
                            "external_id": str(item["id"]), "href": "", "name": str(item.get("summaryOverride") or item.get("summary") or item["id"]),
                            'description': str(item.get('description') or ''),
                            "tz": str(item.get("timeZone") or ""), "writable": role in ("owner", "writer"), "access_role": role,
                            "is_primary": bool(item.get("primary")), "default_reminders": item.get("defaultReminders") or []})
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def calendar_capabilities(self):
        def granted(*values):
            if self.scopes is None:
                return None
            return bool(self.scopes.intersection({'https://www.googleapis.com/auth/calendar', *values}))
        return {'create': granted('https://www.googleapis.com/auth/calendar.calendars'),
                'update': granted('https://www.googleapis.com/auth/calendar.calendars'),
                'delete': granted('https://www.googleapis.com/auth/calendar.calendars'),
                'sharing': granted('https://www.googleapis.com/auth/calendar.acls'),
                'offline_access': bool(self.refresh_token),
                'granted_scopes': sorted(self.scopes) if self.scopes is not None else None}

    def _calendar_record(self, data):
        if not data.get('id'):
            raise ProviderError('parse', 'Google не вернул идентификатор календаря')
        return {'id': make_calendar_id(PROVIDER_GOOGLE, self.credential_key, str(data['id'])),
                'account_id': self.account_id, 'provider': PROVIDER_GOOGLE, 'external_id': str(data['id']),
                'name': data.get('summary') or data['id'], 'description': data.get('description') or '',
                'tz': data.get('timeZone') or '', 'writable': True, 'access_role': 'owner'}

    def create_calendar(self, changes):
        _, _, data = self._request('POST', '/calendars', body={key: value for key, value in {
            'summary': changes.get('name'), 'description': changes.get('description'), 'timeZone': changes.get('timezone')}.items() if value is not None})
        return self._calendar_record(data)

    def update_calendar(self, calendar, changes):
        fields = {'name': 'summary', 'description': 'description', 'timezone': 'timeZone'}
        body = {fields[key]: value for key, value in changes.items() if key in fields}
        _, _, data = self._request('PATCH', '/calendars/' + urllib.parse.quote(calendar['external_id'], safe=''), body=body)
        return {**calendar, **self._calendar_record(data), 'access_role': calendar.get('access_role', 'owner'),
                'writable': calendar.get('writable', True)}

    def delete_calendar(self, calendar):
        self._request('DELETE', '/calendars/' + urllib.parse.quote(calendar['external_id'], safe=''))
        return {'deleted': True, 'calendar_id': calendar['id']}

    def calendar_acl(self, calendar):
        items, token = [], None
        while True:
            _, _, data = self._request('GET', '/calendars/' + urllib.parse.quote(calendar['external_id'], safe='') + '/acl',
                                       {'pageToken': token, 'maxResults': 250})
            items.extend(data.get('items') or [])
            token = data.get('nextPageToken')
            if not token:
                return items

    def share_calendar(self, calendar, sharing):
        scope = {'type': str(sharing.get('type') or 'user')}
        if sharing.get('value'):
            scope['value'] = str(sharing['value'])
        _, _, data = self._request('POST', '/calendars/' + urllib.parse.quote(calendar['external_id'], safe='') + '/acl',
                                   {'sendNotifications': 'true' if sharing.get('send_notifications') else 'false'},
                                   {'scope': scope, 'role': str(sharing.get('role') or 'reader')})
        return data

    def unshare_calendar(self, calendar, sharing):
        rule = str(sharing.get('rule_id') or '')
        if not rule:
            raise ProviderError('parse', 'Нужен rule_id из cal_calendars(include_acl=true)')
        self._request('DELETE', '/calendars/' + urllib.parse.quote(calendar['external_id'], safe='') + '/acl/' + urllib.parse.quote(rule, safe=''))
        return {'deleted': True, 'rule_id': rule}

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

    def exists_event(self, calendar: Dict[str, Any], external_id: str) -> bool:
        """Confirm deletion separately from absence in a time-bounded initial feed."""
        try:
            _, _, item = self._request("GET", f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/{urllib.parse.quote(external_id, safe='')}")
        except ProviderError as exc:
            if exc.kind in ("not_found", "gone"):
                return False
            raise
        return item.get("status") != "cancelled"

    def create(self, calendar: Dict[str, Any], event: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        body = row_to_gevent(event, mute=bool(payload.get("mute_provider_reminders")))
        body["id"] = _google_id(event)
        attendees = json.loads(event.get("attendees_json") or "[]")
        params = {"sendUpdates": "all" if (payload.get("send_updates") and attendees) else "none"}
        try:
            _, headers, data = self._request("POST", f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events", params, body)
        except ProviderError as exc:
            if exc.kind == "conflict" or (exc.kind == "http" and exc.status == 409):
                # Read first. The caller binds a conditional revival or checks a
                # lost-create receipt; this adapter does not overwrite on 409.
                _, _, data = self._request("GET", f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/{body['id']}")
                zone = get_tz(event.get('tz') or '')
                matched = created_content_matches(gevent_to_row(body, calendar['id'], zone), gevent_to_row(data, calendar['id'], zone))
                return {"external_id": data.get("id", body["id"]), "href": "", "etag": str(data.get("etag") or ""),
                        "existed": True, 'matches': matched, 'cancelled': str(data.get('status') or '') == 'cancelled'}
            raise
        return {"external_id": str(data.get("id") or body["id"]), "href": "", "etag": str(data.get("etag") or headers.get("etag") or "")}

    def created_identity(self, calendar: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, str]:
        """The id a create of this event uses (stable from its UID), so a lost answer stays addressable."""
        if not event.get("uid"):
            raise ProviderError("parse", "у события нет uid")
        return {"external_id": _google_id(event), "href": ""}

    def lookup_created(self, calendar: Dict[str, Any], event: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Identity and etag of what a create of this event wrote; None when Google has none (or only a cancelled tombstone)."""
        identity = self.created_identity(calendar, event)
        try:
            _, _, data = self._request("GET", f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/{identity['external_id']}")
        except ProviderError as exc:
            if exc.kind in ("not_found", "gone"):
                return None
            raise
        if str(data.get("status") or "") == "cancelled":
            return None
        return {**identity, "etag": str(data.get("etag") or "")}

    def update(self, calendar: Dict[str, Any], event: Dict[str, Any], expected_etag: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        external_id = str(event.get("external_id") or "")
        if not external_id and event.get("master_id"):
            external_id = self._instance_id(calendar, event, payload)
            if not external_id:
                raise ProviderError("retry", "экземпляр серии у Google ещё не найден (мастер ещё не записан?) — повторим позже")
        if not external_id:
            return self.create(calendar, event, payload)
        body = row_to_gevent(event, mute=bool(payload.get("mute_provider_reminders")))
        changes = payload.get('changes') or {}
        if (event.get('master_id') and payload.get('scope', 'this') == 'this'
                and not payload.get('previous_recurrence_id') and 'changes' in payload):
            names = {'title':'summary', 'description':'description', 'location':'location',
                     'availability':'transparency', 'attendees':'attendees',
                     'reminders':'reminders', 'status':'status'}
            keep = {names[k] for k in changes if k in names}
            if any(k in changes for k in ('start_utc','end_utc','all_day','tz')):
                keep.update(('start','end'))
            if payload.get('mute_provider_reminders'):
                keep.add('reminders')
            body = {k:v for k,v in body.items() if k in keep}
        if "attendees" not in (payload.get("changes") or {}):
            # PATCH arrays replace the whole provider array. Unrelated edits must
            # not rewrite guests from a potentially older local projection.
            body.pop("attendees", None)
        if event.get("master_id") and "reminders" not in (payload.get("changes") or {}) and not payload.get("mute_provider_reminders"):
            # PATCH of a title-only occurrence must not turn inherited master reminders
            # into an explicit Google instance default (or overwrite an existing override).
            body.pop("reminders", None)
        # Removing the last guest is still an update for which the caller may
        # request notifications. The remaining guest count is not that choice.
        params = {"sendUpdates": "all" if payload.get("send_updates") else "none"}
        if not expected_etag:
            raise ProviderError('conflict', 'нет закреплённого ETag Google; запись не отправлена')
        headers = {"If-Match": expected_etag}
        method = 'PUT' if payload.get('_revive_cancelled') else 'PATCH'
        _, resp_headers, data = self._request(method, f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/{urllib.parse.quote(external_id, safe='')}",
                                              params, body, headers)
        return {"etag": str(data.get("etag") or resp_headers.get("etag") or ""), "external_id": str(data.get("id") or external_id)}

    def delete(self, calendar: Dict[str, Any], event: Dict[str, Any], expected_etag: str, payload: Optional[Dict[str, Any]] = None) -> None:
        external_id = str(event.get("external_id") or "")
        if not external_id:
            return
        attendees = json.loads(event.get("attendees_json") or "[]")
        send = "all" if ((payload or {}).get("send_updates") and attendees) else "none"
        if not expected_etag:
            raise ProviderError('conflict', 'нет закреплённого ETag Google; запись не отправлена')
        headers = {"If-Match": expected_etag}
        self._request("DELETE", f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/{urllib.parse.quote(external_id, safe='')}",
                      {"sendUpdates": send}, None, headers)

    def read_current(self, calendar, event, payload=None):
        external_id = event.get('external_id') or self._instance_id(calendar, event, payload)
        if not external_id:
            raise ProviderError('not_found', 'экземпляр события не найден', 404)
        path = (f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/"
                f"{urllib.parse.quote(external_id, safe='')}")
        _, headers, data = self._request('GET', path)
        row = gevent_to_row(data, calendar['id'], get_tz(event.get('tz') or ''))
        return {'event': row, 'etag': str(data.get('etag') or headers.get('etag') or '')}

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
        current = self.read_current(calendar, {**event, 'external_id': external_id}, payload)
        expected = payload.get('_expected_etag') or ''
        if not current['event'] or not expected or current['etag'] != expected:
            raise ProviderError('conflict', 'приглашение изменилось на сервере', 412)
        attendees = json.loads(current['event'].get('attendees_json') or '[]')
        hit = False
        for att in attendees:
            if att.get("self") or str(att.get("email") or "").lower() == self.email.lower():
                att["responseStatus"] = response
                hit = True
        if not hit:
            raise ProviderError("unsupported", "среди участников события нет этого аккаунта — ответить нечем")
        body = {"attendees": [_attendee_to_google(a) for a in attendees if a.get("email")]}
        params = {"sendUpdates": "all" if payload.get("notify_organizer") else "none"}
        _, headers, data = self._request("PATCH", f"/calendars/{urllib.parse.quote(calendar['external_id'], safe='')}/events/{urllib.parse.quote(external_id, safe='')}", params, body, {'If-Match': expected})
        return {"etag": str(data.get("etag") or headers.get("etag") or ""),
                "external_id": str(data.get('id') or external_id)}

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
    attendees = [{**{k: a[k] for k in ("comment", "additionalGuests", "resource") if k in a},
                  "email": a.get("email", ""), "name": a.get("displayName", ""), "status": a.get("responseStatus", ""), "self": bool(a.get("self")),
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
        "my_response": my_response, "reminders_json": json.dumps("off" if rem.get("useDefault") is False and not reminders else sorted(set(reminders))), "origin": "external",
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


def _attendee_to_google(attendee: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve writable guest metadata when an attendee-array change is intended."""
    result = {k: attendee[k] for k in ("email", "comment", "additionalGuests", "resource") if k in attendee}
    status = attendee.get("responseStatus") or attendee.get("status")
    if status:
        result["responseStatus"] = status
    if attendee.get("name"):
        result["displayName"] = attendee["name"]
    if attendee.get("role") == "optional":
        result["optional"] = True
    return result


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
    offsets = own_reminders(event.get("reminders_json"))   # None = the calendar's defaults, [] = explicitly none
    if mute or offsets == []:
        body["reminders"] = {"useDefault": False, "overrides": []}
    elif offsets:
        body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": int(m)} for m in offsets][:5]}
    else:
        body["reminders"] = {"useDefault": True}
    try:
        attendees = json.loads(event.get("attendees_json") or "[]")
    except ValueError:
        attendees = []
    body["attendees"] = [_attendee_to_google(a) for a in attendees if a.get("email")]
    return body
