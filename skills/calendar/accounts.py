"""Calendar account onboarding and discovery, shared by tools and the widget.

Credentials remain in the existing encrypted skill vault; this module does not
write host Settings, grant permissions, or activate any autonomous work.
"""
from __future__ import annotations
import hashlib
import uuid
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict

import ops
from credentials import _mutate_tokens
from model import PROVIDER_GOOGLE, PROVIDER_YANDEX, PROVIDER_CALDAV, iso_utc, now_utc
from providers import Providers, CalDAVAdapter, YandexAdapter, YANDEX_BASE


def connect_caldav(ctx, *, login, password, server_url='', alias='', preset='caldav'):
    """Save one selected connection, then report actual discovery, including failures."""
    login = str(login or '').strip()
    if not login or not password:
        return {'status': 'error', 'message': 'Нужны логин и пароль приложения'}
    if preset == PROVIDER_YANDEX:
        server_url = YANDEX_BASE
        account_id = PROVIDER_YANDEX + ':' + login
        adapter = YandexAdapter(login, password)
    elif preset == PROVIDER_CALDAV:
        server_url = _public_base(server_url)
        key = hashlib.sha256((server_url + '\0' + login).encode()).hexdigest()[:24]
        account_id = PROVIDER_CALDAV + ':' + key
        adapter = CalDAVAdapter(login, password, server_url, account_id=account_id)
    else:
        return {'status': 'error', 'message': 'preset: caldav | yandex'}
    entry = {'provider': preset, 'login': login, 'password': str(password), 'server_url': server_url,
             'alias': str(alias or login), 'generation': uuid.uuid4().hex}
    def save(vault):
        vault[account_id] = entry
        return True
    _mutate_tokens(ctx.state_dir, str(ctx.secrets.get('CALENDAR_TOKEN_KEY') or ''), save)
    ctx.store.upsert_account({'id': account_id, 'provider': preset, 'login': login, 'alias': entry['alias'], 'status': 'connecting'})
    try:
        calendars = adapter.list_calendars()
        entry['home_url'] = adapter.home
        def discovered(vault):
            if vault.get(account_id, {}).get('generation') != entry['generation']:
                return False
            return save(vault)
        if not _mutate_tokens(ctx.state_dir, str(ctx.secrets.get('CALENDAR_TOKEN_KEY') or ''), discovered):
            return {'status': 'superseded', 'account_id': account_id, 'message': 'Подключение уже изменено или отключено'}
        for calendar in calendars:
            existing = ctx.store.get_calendar(calendar['id'])
            ctx.store.upsert_calendar({**calendar,
                **({key: existing[key] for key in ('role_visible', 'role_busy', 'role_publish', 'publish_mode', 'is_default')} if existing else {})})
        ctx.store.set_account_status(account_id, 'ok')
    except ops.ProviderError as exc:
        ctx.store.set_account_status(account_id, 'auth_failed' if exc.kind == 'auth' else 'error', exc.message)
        return {'status': 'error', 'account_id': account_id, 'saved': True, 'message': exc.message}
    ctx.store.set_setting('sync_requested_at', iso_utc(now_utc()))
    ctx.providers = Providers(ctx.secrets, ctx.state_dir)
    return {'status': 'connected', 'account_id': account_id, 'label': entry['alias'],
            'calendars': [{'id': calendar['id'], 'name': calendar['name']} for calendar in calendars],
            'transport': urllib.parse.urlsplit(server_url).scheme}


def complete_google(ctx, *, code='', state='', error='', error_description=''):
    """Finish or cancel only the exact issued flow; discovery runs independently."""
    import providers_google as gp
    try:
        if error:
            gp.cancel_auth(ctx.store, state)
            return {'ok': False, 'code': 'oauth_denied', 'error': str(error_description or error)}
        if not code or not state:
            return {'ok': False, 'code': 'oauth_incomplete', 'error': 'Google не передал code/state'}
        done = gp.finish_auth(ctx.store, ctx.secrets, ctx.state_dir, str(code), str(state))
        existing = ctx.store.get_account(done['account_id'])
        ctx.store.upsert_account({'id': done['account_id'], 'provider': PROVIDER_GOOGLE, 'login': done['email'],
                                 'alias': existing['alias'] if existing else done['email'], 'status': 'connected'})
        ctx.store.set_setting('sync_requested_at', iso_utc(now_utc()))
        message = ('Google подключён; календари появятся после синхронизации' if done.get('offline_access') else
                   'Google подключён на срок access token; для постоянного доступа нужен повторный вход с offline grant')
        return {'ok': True, **done, 'status': 'connected', 'message': message}
    except ops.ProviderError as exc:
        return {'ok': False, 'code': 'oauth_' + exc.kind, 'error': exc.message}

def reload_yandex(ctx) -> Dict[str, Any]:
    """Read accounts from the secret, discover calendars, keep owner roles of known calendars."""
    st = ctx.store
    if ctx.providers.yandex_error:
        return {"status": "error", "message": ctx.providers.yandex_error}
    if not ctx.providers.yandex_accounts:
        return {"status": "not_connected", "message": "YANDEX_CALDAV_ACCOUNTS пуст или не выдан скиллу (грант выдаётся после появления ключа в Secrets)"}
    report = []
    for acc in ctx.providers.yandex_accounts:
        account_id = f"{PROVIDER_YANDEX}:{acc['login']}"
        st.upsert_account({"id": account_id, "provider": PROVIDER_YANDEX, "alias": acc["alias"], "login": acc["login"], "status": "ok"})
        adapter = ctx.providers.adapter_for(account_id)
        try:
            cals = adapter.list_calendars()
        except ops.ProviderError as exc:
            st.set_account_status(account_id, "auth_failed" if exc.kind == "auth" else "error", exc.message)
            report.append({"account": account_id, "status": exc.kind, "message": exc.message})
            continue
        for cal in cals:
            existing = st.get_calendar(cal["id"])
            st.upsert_calendar({**cal, "role_visible": existing["role_visible"] if existing else True,
                                "role_busy": existing["role_busy"] if existing else True,
                                "role_publish": existing["role_publish"] if existing else False,
                                "publish_mode": existing["publish_mode"] if existing else "busy"})
        st.set_account_status(account_id, "ok", "", synced=False)
        report.append({"account": account_id, "status": "ok", "calendars": [{"id": c["id"], "name": c["name"], "writable": c["writable"]} for c in cals]})
    st.set_setting("sync_requested_at", iso_utc(now_utc()))
    return {"status": "ok", "accounts": report, "next_step": "Роли календарей (показывать / источник занятости / публикация «Занят») — cal_settings(action='set_calendar', …)"}


def _public_base(value):
    parsed = urllib.parse.urlsplit(str(value or '').strip())
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError('Нужен адрес установки http(s) без query/fragment или учётных данных, включая префикс пути при его наличии')
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', ''))


def connect_google(ctx, public_base_url="") -> Dict[str, Any]:
    """Prepare a skill-owned PKCE flow using the host's passive return page."""
    import providers_google as gp
    if not gp.client_credentials(ctx.secrets)[0]:
        return {"status": "not_connected", "message": "Нет GOOGLE_OAUTH_CLIENT_ID или прежнего GOOGLE_CALENDAR_CLIENT_ID в Settings → Secrets"}
    try:
        port = int((ctx.api.get_runtime_info() or {}).get("server_port") or 0)
        if not 0 < port < 65536:
            raise ValueError('Host не сообщил порт сервера')
        local_base = f'http://127.0.0.1:{port}'
        page = '/onboarding/static/provider-return.html'
        request = urllib.request.Request(local_base + page, method='HEAD')
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise ValueError('Host return page is unavailable')
        base = _public_base(public_base_url or ctx.store.get_setting('public_base_url') or local_base)
    except (ValueError, AttributeError, urllib.error.URLError, OSError) as exc:
        return {'status': 'unavailable', 'code': 'oauth_return_unavailable',
                'message': f'Не удалось подготовить возврат из Google: {exc}. Обнови host или укажи адрес установки.'}
    redirect_uri = base + page
    try:
        started = gp.start_auth(ctx.store, ctx.secrets, redirect_uri)
    except ops.ProviderError as exc:
        return {"status": "error", "message": exc.message}
    return {"status": "ok", "auth_url": started["auth_url"], "redirect_uri": redirect_uri,
            'scopes_requested': list(gp.SCOPES),
            "message": "Открой ссылку под нужным Google-аккаунтом. Для loopback возврата браузер должен быть на машине Уробороса; "
                       "для удалённой установки укажи public_base_url и зарегистрируй этот redirect URI у Google Web client.",
            "next_step": "После входа: cal_status; роли календарей — set_calendar. Отключение здесь не отзывает доступ всего Google-проекта."}


def reload_google(ctx) -> Dict[str, Any]:
    """Discover calendars of every Google account whose tokens are stored; keep owner roles."""
    import providers_google as gp
    st = ctx.store
    try:
        tokens = gp.load_tokens(ctx.state_dir, str(ctx.secrets.get("CALENDAR_TOKEN_KEY") or ""))
    except ops.ProviderError as exc:
        return {"status": "error", "message": exc.message}
    if not tokens:
        return {"status": "not_connected", "message": "Ни один аккаунт Google ещё не подключён: cal_settings(action='connect_google')"}
    report = []
    for key, entry in tokens.items():
        if entry.get('provider', PROVIDER_GOOGLE) != PROVIDER_GOOGLE:
            continue
        email = entry.get('email') or key
        account_id = f"{PROVIDER_GOOGLE}:{key}"
        st.upsert_account({"id": account_id, "provider": PROVIDER_GOOGLE, "alias": st.get_account(account_id)["alias"] if st.get_account(account_id) else email.split("@")[0],
                           "login": email, "status": "ok"})
        adapter = ctx.providers.adapter_for(account_id)
        if adapter is None:
            report.append({"account": account_id, "status": "not_connected", "message": ctx.providers.errors.get(account_id, "")})
            continue
        try:
            cals = adapter.list_calendars()
        except ops.ProviderError as exc:
            st.set_account_status(account_id, "auth_failed" if exc.kind == "auth" else "error", exc.message)
            report.append({"account": account_id, "status": exc.kind, "message": exc.message})
            continue
        for cal in cals:
            existing = st.get_calendar(cal["id"])
            st.upsert_calendar({**cal, "role_visible": existing["role_visible"] if existing else True,
                                "role_busy": existing["role_busy"] if existing else True,
                                "role_publish": existing["role_publish"] if existing else False,
                                "publish_mode": existing["publish_mode"] if existing else "busy"})
            st.set_setting(f"google_default_reminders:{cal['id']}", cal.get("default_reminders") or [])
        st.set_account_status(account_id, "ok", "", synced=False)
        report.append({"account": account_id, "status": "ok", "calendars": [{"id": c["id"], "name": c["name"], "writable": c["writable"], "primary": c.get("is_primary", False)} for c in cals]})
    st.set_setting("sync_requested_at", iso_utc(now_utc()))
    return {"status": "ok", "accounts": report}
