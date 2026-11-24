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
from typing import Any, Dict, Optional, Tuple

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
    transport = urllib.parse.urlsplit(server_url).scheme
    transport_info = {'transport': transport, 'transport_warning':
                      'HTTP sends your login and password without encryption.' if transport == 'http' else ''}
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
            return {'status': 'superseded', 'account_id': account_id, **transport_info, 'message': 'Подключение уже изменено или отключено'}
        for calendar in calendars:
            existing = ctx.store.get_calendar(calendar['id'])
            ctx.store.upsert_calendar({**calendar,
                **({key: existing[key] for key in ('role_visible', 'role_busy', 'role_publish', 'publish_mode', 'is_default')} if existing else {})},
                restore=True)  # observed by this connection, so an earlier disconnect no longer hides it
        ctx.store.set_account_status(account_id, 'ok')
    except ops.ProviderError as exc:
        ctx.store.set_account_status(account_id, 'auth_failed' if exc.kind == 'auth' else 'error', exc.message)
        return {'status': 'error', 'account_id': account_id, 'saved': True, 'message': exc.message, **transport_info}
    ctx.store.set_setting('sync_requested_at', iso_utc(now_utc()))
    ctx.providers = Providers(ctx.secrets, ctx.state_dir, host=(getattr(ctx.providers, 'google_host', None) or {}).get('host'))
    return {'status': 'connected', 'account_id': account_id, 'label': entry['alias'],
            'calendars': [{'id': calendar['id'], 'name': calendar['name']} for calendar in calendars],
            **transport_info}


def complete_google(ctx, *, code='', state='', error='', error_description=''):
    """Finish or cancel only the exact issued flow; discovery runs independently."""
    import providers_google as gp
    try:
        if error:
            gp.cancel_auth(ctx.store, state)
            return {'ok': False, 'code': 'oauth_denied', 'error': str(error_description or error)}
        if not code or not state:
            return {'ok': False, 'code': 'oauth_incomplete', 'error': 'Google не передал code/state'}
        import google_host
        from host_sign_in import ABSENT, READY
        authority = (google_host.host_of(ctx.providers).state() if getattr(ctx.providers, 'state_dir', None) else ABSENT)
        migrating = authority == READY or (authority != ABSENT and ctx.store.get_setting(google_host.MOVED) is not None)
        before_withdrawn = ctx.store.get_setting(google_host.WITHDRAWN) or {}
        before_moved = ctx.store.get_setting(google_host.MOVED) or {}
        before_refused = ctx.store.get_setting(google_host.REFUSED) or {}
        chosen: Dict[str, Any] = {}
        def choose_key(store, vault, subject, email):
            chosen['subject'] = subject
            local_key = google_host.legacy_return_key(store, vault, subject, email, on_card=migrating)
            prior = vault.get(local_key) or {}
            chosen['prior_generation'] = prior.get('generation')
            marks = store.get_setting(google_host.MOVED) or {}
            withdrawn = store.get_setting(google_host.WITHDRAWN) or {}
            chosen['host_withdrawn'] = google_host.host_withdrawn_state(
                ctx.state_dir, local_key, prior,
                mark=marks.get(local_key) or before_moved.get(local_key),
                withdrawn=withdrawn.get(local_key) or before_withdrawn.get(local_key),
            )
            return local_key
        def before_vault(store, local_key, _prior, *, commit: bool) -> bool:
            hw = chosen.get('host_withdrawn')
            if authority != ABSENT or hw not in ('withdrawn', 'unknown'):
                return True
            account_id = PROVIDER_GOOGLE + ':' + local_key
            if commit:
                if hw == 'withdrawn':
                    store.cancel_intents_for_account(account_id, reason='account disconnected before downgrade')
                else:
                    store.hold_intents_for_account(account_id, reason=google_host.UNCONFIRMED_DISCONNECT)
                return True
            return not any(row['state'] == 'pending' for row in store.unsettled_account_intents(account_id))
        def keep_refresh(store, local_key, prev, subject):
            withdrawn = store.get_setting(google_host.WITHDRAWN) or {}
            marks = store.get_setting(google_host.MOVED) or {}
            refused = store.get_setting(google_host.REFUSED) or {}
            prior_refusal = (refused.get(local_key) or before_refused.get(local_key) or {}).get('reason')
            return (bool(subject) and prev.get('subject') == subject
                    and store.get_account(PROVIDER_GOOGLE + ':' + local_key) is not None
                    and local_key not in marks and local_key not in before_moved
                    and prior_refusal not in (google_host.UNCONFIRMED, 'account_disconnected')
                    and not google_host.withdrawn_generation(withdrawn.get(local_key), prev)
                    and not google_host.withdrawn_generation(before_withdrawn.get(local_key), prev)
                    and google_host.host_withdrawn_state(ctx.state_dir, local_key, prev,
                                                         mark=marks.get(local_key) or before_moved.get(local_key),
                                                         withdrawn=withdrawn.get(local_key) or before_withdrawn.get(local_key)) == 'clear')
        done = gp.finish_auth(ctx.store, ctx.secrets, ctx.state_dir, str(code), str(state),
                              choose_key=choose_key, can_keep_refresh=keep_refresh, before_vault=before_vault)
        key, saved_generation = done['account_id'].split(':', 1)[1], done.get('generation')
        with ctx.store.transaction() as view:
            existing = view.get_account(done['account_id'])
            withdrawn = view.get_setting(google_host.WITHDRAWN) or {}
            if authority == ABSENT and chosen.get('host_withdrawn') == 'withdrawn' and saved_generation:
                # Prior pending intents were already withdrawn before fresh credentials were saved.
                marks = view.get_setting(google_host.MOVED) or {}
                if key in marks:
                    marks.pop(key, None)
                    view.set_setting(google_host.MOVED, marks)
                prior_gen = chosen.get('prior_generation')
                if prior_gen:
                    withdrawn[key] = {'at': iso_utc(now_utc()), 'generation': prior_gen}
                else:
                    withdrawn[key] = {**(withdrawn.get(key) or {}), 'at': iso_utc(now_utc()),
                                      'generation_unknown': True, 'allowed_generation': saved_generation}
                view.set_setting(google_host.WITHDRAWN, withdrawn)
            elif authority == ABSENT and saved_generation and (
                    chosen.get('host_withdrawn') == 'unknown' or google_host._read_host_withdrawn(ctx.state_dir) is None):
                marks = view.get_setting(google_host.MOVED) or {}
                if key in marks:
                    marks.pop(key, None)
                    view.set_setting(google_host.MOVED, marks)
                entry_w = {**(withdrawn.get(key) or {}), 'at': iso_utc(now_utc()), 'fresh_local_generation': saved_generation}
                if entry_w.get('generation_unknown') is True and withdrawn.get(key) == before_withdrawn.get(key):
                    entry_w['allowed_generation'] = saved_generation
                withdrawn[key] = entry_w
                view.set_setting(google_host.WITHDRAWN, withdrawn)
            elif (key in withdrawn and withdrawn.get(key) == before_withdrawn.get(key)
                  and withdrawn[key].get('generation_unknown') is True and saved_generation):
                withdrawn[key] = {**withdrawn[key], 'allowed_generation': saved_generation}
                view.set_setting(google_host.WITHDRAWN, withdrawn)
            refused = view.get_setting(google_host.REFUSED) or {}
            if key in refused and (authority == ABSENT
                                   or (refused[key] or {}).get('reason') not in (google_host.UNCONFIRMED, 'account_disconnected')):
                refused.pop(key, None)
                view.set_setting(google_host.REFUSED, refused)
            if chosen.get('subject'):
                identities = view.get_setting(google_host.IDENTITIES) or {}
                if google_host.remember_subject(identities, str(chosen['subject']), key):
                    view.set_setting(google_host.IDENTITIES, identities)
            if not (migrating and existing and key in (view.get_setting(google_host.MOVED) or {})):
                view.upsert_account({'id': done['account_id'], 'provider': PROVIDER_GOOGLE, 'login': done['email'],
                                     'alias': existing['alias'] if existing else done['email'], 'status': 'connected'})
            view.set_setting('sync_requested_at', iso_utc(now_utc()))
        if getattr(ctx, 'providers', None) is not None:
            ctx.providers = Providers(ctx.secrets, ctx.state_dir, host=(getattr(ctx.providers, 'google_host', None) or {}).get('host'))
        message = ('Google подключён; календари появятся после синхронизации' if done.get('offline_access') else
                   'Google подключён на срок access token; для постоянного доступа нужен повторный вход с offline grant')
        return {'ok': True, **{k: v for k, v in done.items() if k != 'generation'}, 'status': 'connected', 'message': message}
    except ops.ProviderError as exc:
        return {'ok': False, 'code': 'oauth_' + exc.kind, 'error': exc.message}


def withdraw_account_in_tx(view, state_dir, passphrase, account, *, expect_host_key=None, expect_host_mark=None) -> Tuple[Dict[str, Any], Optional[Tuple[str, Any]]]:
    """Commit account withdrawal inside an existing SQLite transaction; return ``(result, google_vault_cleanup)``."""
    google = account['provider'] == PROVIDER_GOOGLE
    key = account['id'].split(':', 1)[-1] if google else ''
    mark: Dict[str, Any] = {}
    cleanup: Optional[Tuple[str, Any]] = None
    if google:
        import google_host
        import providers_google as gp
        marks = view.get_setting(google_host.MOVED) or {}
        mark = marks.get(key) or {}
        if ((expect_host_key is not None and str(mark.get('account')) != str(expect_host_key))
                or (expect_host_mark is not None and mark != expect_host_mark)):
            return {'status': 'rebound', 'cancelled_intents': 0, 'uncertain_intents': 0, 'tokens_dropped': False}, None
        generation = subject = None
        known, has_entry = True, False
        try:
            tokens = gp.load_tokens(state_dir, passphrase)
            has_entry = key in tokens
            entry = tokens.get(key) or {}
            generation, subject = entry.get('generation'), entry.get('subject')
        except (ops.ProviderError, OSError, ValueError, AttributeError):
            known = False
    cancelled = view.cancel_intents_for_account(account['id'])
    view.delete_account(account['id'])
    uncertain = sum(row['state'] == 'uncertain' for row in view.unsettled_account_intents(account['id']))
    if google:
        record: Dict[str, Any] = {'at': iso_utc(now_utc())}
        if not known:
            record['generation_unknown'] = True
        elif has_entry:
            record['generation'] = generation
            cleanup = (key, generation)
        if key in marks:
            marks.pop(key)
            view.set_setting(google_host.MOVED, marks)
        if expect_host_key is None and mark.get('account'):
            record['host_account'] = mark.get('account')   # a local disconnect of a moved account: keep the host grant out of Calendar
            if mark.get('connection_id'):
                record['host_connection_id'] = mark['connection_id']
        withdrawn = view.get_setting(google_host.WITHDRAWN) or {}
        if 'generation_unknown' in record or 'generation' in record or record.get('host_account'):
            withdrawn[key] = record
        else:
            withdrawn.pop(key, None)
        view.set_setting(google_host.WITHDRAWN, withdrawn)
        subject = subject or mark.get('subject')
        if subject:
            identities = view.get_setting(google_host.IDENTITIES) or {}
            if google_host.remember_subject(identities, str(subject), key):
                view.set_setting(google_host.IDENTITIES, identities)
    return {'cancelled_intents': cancelled, 'uncertain_intents': uncertain}, cleanup


def withdraw_account(store, state_dir, passphrase, account, *, expect_host_key=None, expect_host_mark=None) -> Dict[str, Any]:
    """Stop using one external account here: its unsubmitted writes are withdrawn, its calendars retired and
    its saved credentials dropped (for Google also the record that Ouroboros holds its sign-in); history stays.
    The withdrawal is recorded atomically with the account's removal, so an interrupted credential drop can
    never be offered to the host again. ``expect_host_key`` (the host-driven path) additionally requires the
    mark to still map to that exact host key: a concurrently rebound account is never withdrawn."""
    with store.transaction() as view:
        done, cleanup = withdraw_account_in_tx(view, state_dir, passphrase, account,
                                               expect_host_key=expect_host_key, expect_host_mark=expect_host_mark)
    if done.get('status') == 'rebound':
        return done
    if account['provider'] == PROVIDER_GOOGLE:
        import providers_google as gp
        forgot = bool(cleanup) and gp.forget_account(state_dir, passphrase, cleanup[0], expected_generation=cleanup[1])
    else:
        def forget(vault):
            return vault.pop(account['id'], None) is not None
        forgot = _mutate_tokens(state_dir, passphrase, forget)
    return {**done, 'tokens_dropped': forgot}


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
                                "publish_mode": existing["publish_mode"] if existing else "busy"}, restore=True)
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
    """Prepare a skill-owned PKCE flow using the host's passive return page (an Ouroboros that holds sign-ins
    signs in on Calendar's card instead)."""
    import providers_google as gp
    import google_host
    kind = google_host.sign_in_kind(ctx.providers)
    if kind == 'ouroboros':
        return google_host.on_card()
    if kind == 'unreachable':
        return {'status': 'unreachable', 'message': 'Служба входа Ouroboros не ответила: непонятно, где добавляются '
                                                    'Google-аккаунты. Повтори позже; существующие подключения не изменились.'}
    if not gp.client_credentials(ctx.secrets)[0]:
        return {"status": "not_connected", "message": "Нет GOOGLE_OAUTH_CLIENT_ID или прежнего GOOGLE_CALENDAR_CLIENT_ID в Settings → Secrets"}
    try:
        port = int((ctx.api.get_runtime_info() or {}).get("server_port") or 0)
        if not 0 < port < 65536:
            raise ValueError('Host не сообщил порт сервера')
        local_base = f'http://127.0.0.1:{port}'
        page = '/onboarding/static/provider-return.html'
        request = urllib.request.Request(local_base + page, method='HEAD')
        # This probe always targets the local host, independently of the public OAuth redirect base.
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
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
    """Discover calendars of every Google account whose tokens are stored or whose sign-in Ouroboros holds; keep owner roles."""
    import providers_google as gp
    import google_host
    st = ctx.store
    held = {}
    if google_host.sign_in_kind(ctx.providers) != 'skill':
        google_host.follow(st, ctx.secrets, ctx.state_dir, google_host.host_of(ctx.providers))
        held = st.get_setting(google_host.MOVED) or {}
        ctx.providers = Providers(ctx.secrets, ctx.state_dir, host=google_host.host_of(ctx.providers))   # with the accounts follow just recorded
    try:
        tokens = gp.load_tokens(ctx.state_dir, str(ctx.secrets.get("CALENDAR_TOKEN_KEY") or ""))
    except ops.ProviderError as exc:
        if not held:
            return {"status": "error", "message": exc.message}
        tokens = {}
    tokens = {**{key: {'email': mark.get('label')} for key, mark in held.items()}, **tokens}
    withdrawn = st.get_setting(google_host.WITHDRAWN) or {}
    moved_marks = st.get_setting(google_host.MOVED) or {}
    hw_states = {key: google_host.host_withdrawn_state(ctx.state_dir, key, entry,
                                                       mark=moved_marks.get(key), withdrawn=withdrawn.get(key))
                 for key, entry in tokens.items()
                 if key not in held and entry.get('provider', PROVIDER_GOOGLE) == PROVIDER_GOOGLE}
    tokens = {key: entry for key, entry in tokens.items()
              if key in held or not (entry.get('provider', PROVIDER_GOOGLE) == PROVIDER_GOOGLE
                                     and (google_host.withdrawn_generation(withdrawn.get(key), entry)
                                          or hw_states.get(key) != 'clear'))}
    if not tokens:
        if any(state == 'unknown' for state in hw_states.values()):
            return {"status": "error", "message": "Не удалось подтвердить статус прежнего входа Google по sign_in_withdrawn.json; старые локальные входы временно не используются."}
        kind = google_host.sign_in_kind(ctx.providers)
        if kind == 'unreachable':
            return {"status": "unreachable", "message": "Служба входа Ouroboros не ответила: проверить, какие "
                                                        "Google-аккаунты подключены, сейчас нельзя."}
        return {"status": "not_connected", "message": google_host.on_card()['message'] if kind == 'ouroboros'
                else "Ни один аккаунт Google ещё не подключён: cal_settings(action='connect_google')"}
    report = []
    for key, entry in tokens.items():
        if entry.get('provider', PROVIDER_GOOGLE) != PROVIDER_GOOGLE:
            continue
        email = (held.get(key) or {}).get('label') or entry.get('email') or key
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
                                "publish_mode": existing["publish_mode"] if existing else "busy"}, restore=True)
            st.set_setting(f"google_default_reminders:{cal['id']}", cal.get("default_reminders") or [])
        st.set_account_status(account_id, "ok", "", synced=False)
        report.append({"account": account_id, "status": "ok", "calendars": [{"id": c["id"], "name": c["name"], "writable": c["writable"], "primary": c.get("is_primary", False)} for c in cals]})
    st.set_setting("sync_requested_at", iso_utc(now_utc()))
    return {"status": "ok", "accounts": report}
