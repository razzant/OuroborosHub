"""Calendar collections and sharing, with receipts in the existing intent store.

Collection mutations are attempted once. A lost response stays uncertain and is
never handed to the event retry loop; the caller can inspect the receipt and the
provider before choosing another operation. Read tools never change provider data.
"""
from __future__ import annotations
import json

from model import PROVIDER_LOCAL, bounded_result, iso_utc, new_id, now_utc
from ops import ProviderError


def _receipt(row):
    return {'request_id': row['id'], 'status': row['state'],
            'result': json.loads(row.get('result_json') or '{}')}


def cal_calendars(ctx, account_id='', calendar_id='', include_acl=False, refresh=False, request_id='', **kwargs):
    """Read account capabilities, collections, sharing, or an earlier mutation receipt."""
    if request_id:
        row = ctx.store.get_intent(str(request_id))
        return bounded_result(_receipt(row) if row and row['kind'] == 'calendar' else {'status': 'not_found'})
    accounts = ctx.store.list_accounts()
    if account_id:
        accounts = [account for account in accounts if account['id'] == account_id]
        if not accounts:
            return bounded_result({'status': 'not_found', 'message': 'Unknown canonical account_id'})
    reports = []
    for account in accounts:
        adapter = ctx.providers.adapter_for(account['id']) if account['provider'] != PROVIDER_LOCAL else None
        capabilities = {'create': True, 'update': True, 'delete': True, 'sharing': False} if account['provider'] == PROVIDER_LOCAL else (
            adapter.calendar_capabilities() if adapter is not None and hasattr(adapter, 'calendar_capabilities') else None)
        report = {'account_id': account['id'], 'label': account['alias'] or account['login'], 'provider': account['provider'], 'status': account['status'], 'capabilities': capabilities}
        if account['provider'] != PROVIDER_LOCAL and adapter is None:
            report['error'] = {'kind': 'not_connected', 'message': ctx.providers.errors.get(account['id'], 'Account credentials unavailable')}
        if refresh and adapter is not None:
            try:
                for calendar in adapter.list_calendars():
                    ctx.store.upsert_calendar(calendar)
            except ProviderError as exc:
                report['error'] = {'kind': exc.kind, 'message': exc.message}
        report['calendars'] = ctx.store.list_calendars(account_id=account['id'])
        reports.append(report)
    result = {'status': 'ok', 'accounts': reports}
    if include_acl:
        calendar = ctx.store.get_calendar(str(calendar_id))
        if calendar is None or (account_id and calendar['account_id'] != account_id):
            return bounded_result({'status': 'not_found', 'message': 'Choose a calendar_id in this account'})
        adapter = ctx.providers.adapter_for(calendar['account_id'])
        if adapter is None and calendar['provider'] != PROVIDER_LOCAL:
            return bounded_result({**result, 'status': 'not_connected', 'message': 'Account credentials unavailable; sharing was not read'})
        try:
            result['sharing'] = adapter.calendar_acl(calendar) if adapter else []
        except ProviderError as exc:
            result.update(status=exc.kind, message=exc.message)
    return bounded_result(result)


def _local(store, action, calendar, account_id, changes):
    if action == 'create':
        cid = 'local:' + new_id('calendar')
        saved = {'id': cid, 'provider': PROVIDER_LOCAL, 'account_id': account_id, 'name': changes['name'],
                 'description': changes.get('description') or '', 'tz': changes.get('timezone') or ''}
        store.upsert_calendar(saved)
        return saved
    if action == 'update':
        store.update_calendar(calendar['id'], {key: value for key, value in
            {'name': changes.get('name'), 'description': changes.get('description'), 'tz': changes.get('timezone')}.items() if value is not None})
        return store.get_calendar(calendar['id'])
    if action == 'delete':
        return {'deleted': True, 'calendar_id': calendar['id']}
    raise ProviderError('unsupported', 'Local calendars have no remote sharing service')


def _retire_calendar(store, calendar_id):
    store.update_calendar(calendar_id, {'deleted_at': iso_utc(now_utc())})
    with store._conn() as conn:
        conn.execute("UPDATE intents SET state='failed', result_json=?, updated_at=? WHERE calendar_id=?"
                     " AND kind!='calendar' AND state IN ('pending','conflict')",
                     (json.dumps({'error': 'calendar removed; queued writes withdrawn'}), iso_utc(now_utc()), calendar_id))
        conn.execute("UPDATE reminders SET state='cancelled', detail='calendar removed' WHERE state IN ('scheduled','no_channel')"
                     " AND event_id IN (SELECT id FROM events WHERE calendar_id=?)", (calendar_id,))


def cal_manage(ctx, action='', account_id='', calendar_id='', changes=None, sharing=None, confirm=False, request_id='', **kwargs):
    """Apply one named collection operation, without automatic retry after uncertainty."""
    changes, sharing = dict(changes or {}), dict(sharing or {})
    previous = ctx.store.get_intent(str(request_id)) if request_id else None
    if previous:
        prior_request = json.loads(previous['payload_json']).get('request') or {}
        replay_request = {'action': action, 'account_id': account_id or prior_request.get('account_id', ''),
                          'calendar_id': calendar_id, 'changes': changes, 'sharing': sharing}
        if previous['kind'] != 'calendar' or prior_request != replay_request:
            return bounded_result({'status': 'request_id_conflict', 'request_id': str(request_id)})
        return bounded_result(_receipt(previous))  # still readable after deletion or disconnect
    if set(changes) - {'name', 'description', 'timezone'}:
        return bounded_result({'status': 'error', 'message': 'changes supports name, description and timezone; unsupported fields were not applied'})
    if action not in ('create', 'update', 'delete', 'share', 'unshare'):
        return bounded_result({'status': 'error', 'message': 'action: create | update | delete | share | unshare'})
    calendar = ctx.store.get_calendar(str(calendar_id)) if calendar_id else None
    if action != 'create' and (calendar is None or calendar.get('deleted_at')):
        return bounded_result({'status': 'not_found', 'message': 'Choose an existing calendar_id'})
    if calendar:
        if account_id and account_id != calendar['account_id']:
            return bounded_result({'status': 'error', 'message': 'calendar_id belongs to another account'})
        account_id = calendar['account_id']
    account = ctx.store.get_account(str(account_id))
    if account is None:
        return bounded_result({'status': 'not_found', 'message': 'Choose a canonical account_id from cal_calendars'})
    if action == 'create' and not str(changes.get('name') or '').strip():
        return bounded_result({'status': 'error', 'message': 'changes.name is required'})
    if changes.get('timezone'):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(changes['timezone'])
        except (ValueError, ZoneInfoNotFoundError):
            return bounded_result({'status': 'error', 'message': 'Unknown IANA timezone'})
    request = {'action': action, 'account_id': account_id, 'calendar_id': calendar_id, 'changes': changes, 'sharing': sharing}
    if confirm is not True:
        return bounded_result({'status': 'needs_confirm', 'request': request, 'message': 'Preview. Apply with confirm=true when covered by the current or standing instruction.'})
    adapter = ctx.providers.adapter_for(account_id) if account['provider'] != PROVIDER_LOCAL else None
    if account['provider'] != PROVIDER_LOCAL and adapter is None:
        return bounded_result({'status': 'not_connected', 'message': ctx.providers.errors.get(account_id, 'Account credentials unavailable')})
    rid = str(request_id or new_id('calendar_op'))
    with ctx.store.transaction() as view:
        previous = view.get_intent(rid)
        if previous:
            if previous['kind'] != 'calendar' or json.loads(previous['payload_json']).get('request') != request:
                return bounded_result({'status': 'request_id_conflict', 'request_id': rid})
            return bounded_result(_receipt(previous))
        view.add_intent('calendar', account_id, str(calendar_id), '', {'request': request}, intent_id=rid)
        view.settle_intent(rid, 'uncertain', {'message': 'Operation admitted; provider outcome not yet recorded. Do not blindly repeat.'})
    try:
        if adapter is None:
            result = _local(ctx.store, action, calendar, account_id, changes)
        elif action == 'create':
            result = adapter.create_calendar(changes)
        elif action == 'update':
            result = adapter.update_calendar(calendar, changes)
        elif action == 'delete':
            result = adapter.delete_calendar(calendar)
        elif action == 'share':
            result = adapter.share_calendar(calendar, sharing)
        else:
            result = adapter.unshare_calendar(calendar, sharing)
        with ctx.store.transaction() as view:
            if action in ('create', 'update'):
                view.upsert_calendar(result)
            elif action == 'delete':
                _retire_calendar(view, calendar['id'])
            view.settle_intent(rid, 'done', result)
        return bounded_result({'status': 'done', 'request_id': rid, 'result': result})
    except ProviderError as exc:
        state = 'uncertain' if exc.kind in ('network', 'server', 'retry', 'parse', 'partial') else 'failed'
        result = {'kind': exc.kind, 'message': exc.message, 'automatic_retry': False}
    except Exception as exc:
        state, result = 'uncertain', {'message': f'Outcome not recorded: {type(exc).__name__}', 'automatic_retry': False}
    ctx.store.settle_intent(rid, state, result)
    return bounded_result({'status': state, 'request_id': rid, 'result': result})
