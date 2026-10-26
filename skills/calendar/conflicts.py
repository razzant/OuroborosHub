"""Explicit resolution of one physical calendar's pending command.

Inspection names all dependent changes. Network reads precede the local CAS;
resolved intents are history, never successful writes or successful prerequisites.
"""
import hashlib
import json

import ops
from model import event_public, iso_utc, new_id, now_utc, parse_stored
from store import EVENT_COLUMNS

_UNFINISHED = ('pending', 'conflict', 'failed')
_PROVIDER_FIELDS = ('title', 'description', 'location', 'start_utc', 'end_utc', 'tz',
                    'all_day', 'rrule', 'rdates', 'exdates', 'recurrence_id', 'status',
                    'organizer', 'attendees_json', 'my_response', 'availability',
                    'external_id', 'href', 'etag', 'raw_payload')


def _payload(intent):
    return json.loads(intent.get('payload_json') or '{}')


def _snapshot(c, event_id):
    found = c.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
    if found is None:
        return None
    row = dict(found)
    selected = [dict(i) for i in c.execute('SELECT * FROM intents WHERE event_id=? ORDER BY rowid', (event_id,))]
    anchor = next((i for i in selected if i['state'] == 'conflict'), None)
    if anchor is None:
        anchor = next((i for i in reversed(selected) if i['state'] in _UNFINISHED), None)
    op_id = _payload(anchor).get('op_id') if anchor else None
    all_intents = [dict(i) for i in c.execute('SELECT * FROM intents WHERE calendar_id=? ORDER BY rowid', (row['calendar_id'],))]
    command = [i for i in all_intents if op_id and _payload(i).get('op_id') == op_id]
    included = {i['id'] for i in command}
    while True:
        dependent_ops = {_payload(i).get('op_id') for i in all_intents if i['state'] in _UNFINISHED
                         and (_payload(i).get('after_intent_id') in included
                              or included.intersection(_payload(i).get('prior_intent_ids', [])))}
        extra = [i for i in all_intents if i['id'] not in included and
                 _payload(i).get('op_id') in dependent_ops and _payload(i).get('op_id')]
        if not extra:
            break
        command.extend(extra)
        included.update(i['id'] for i in extra)
    command = [i for i in all_intents if i['id'] in included]
    roots = {row.get('master_id') or row['id']}
    ids = {row['id']}
    for intent in command:
        p = _payload(intent)
        ids.add(intent['event_id'])
        ids.update(p.get('created_ids', []))
        roots.add(p.get('family_root_id') or row.get('master_id') or row['id'])
    # Separate split segments are related by this command, not link_group_id.
    marks = ','.join('?' for _ in ids)
    candidate_rows = [dict(r) for r in c.execute(f'SELECT * FROM events WHERE calendar_id=? AND id IN ({marks})',
                                               (row['calendar_id'], *sorted(ids)))]
    roots.update(r['id'] for r in candidate_rows if not r.get('master_id'))
    root_marks = ','.join('?' for _ in roots)
    rows = [dict(r) for r in c.execute(f'SELECT * FROM events WHERE calendar_id=? AND '
            f'(id IN ({marks}) OR master_id IN ({root_marks}) OR id IN ({root_marks})) ORDER BY id',
            (row['calendar_id'], *sorted(ids), *sorted(roots), *sorted(roots)))]
    ids.update(r['id'] for r in rows)
    marks = ','.join('?' for _ in ids)
    intents = [dict(i) for i in c.execute(f'SELECT * FROM intents WHERE calendar_id=? AND event_id IN ({marks}) ORDER BY rowid',
                                        (row['calendar_id'], *sorted(ids)))]
    cal = c.execute('SELECT * FROM calendars WHERE id=?', (row['calendar_id'],)).fetchone()
    return {'event': row, 'calendar': dict(cal) if cal else None, 'rows': rows,
            'intents': intents, 'op_id': op_id, 'command_ids': [i['id'] for i in command]}


def _templates(state):
    return [i for i in state['intents'] if i['state'] in _UNFINISHED and
            (i['id'] in state['command_ids'] or i['event_id'] == state['event']['id'])]


def _meaning(state):
    """Retry clocks do not change a comparison; live leases are checked at commit."""
    if state is None:
        return None
    return {**state,
            'event': {k: v for k, v in state['event'].items() if k != 'updated_at'},
            'rows': [{k: v for k, v in r.items() if k != 'updated_at'} for r in state['rows']],
            'intents': [{k: i[k] for k in ('id', 'kind', 'event_id', 'scope', 'payload_json', 'expected_etag', 'state')}
                        for i in state['intents']]}


def _load(ctx, event_id):
    base, occurrence = ops._series_id(str(event_id))
    row = ctx.store.get_event(base)
    if row is None:
        return None, None, {'status': 'not_found'}
    # An unpublished segment has no independent provider comparison: follow
    # its durable prerequisite chain to the actionable conflict.
    queue = [i['id'] for i in ctx.store.intents_for_event(row['id']) if i['state'] in _UNFINISHED]
    visited = set()
    while queue:
        iid = queue.pop()
        if iid in visited:
            continue
        visited.add(iid)
        intent = ctx.store.get_intent(iid)
        if not intent:
            continue
        if intent['state'] == 'conflict' and intent['event_id'] != row['id']:
            return _load(ctx, intent['event_id'])
        p = _payload(intent)
        queue.extend(p.get('prior_intent_ids', []))
        if p.get('after_intent_id'):
            queue.append(p['after_intent_id'])
    if occurrence:
        master, key = ops.resolve_occurrence(ctx.store, row, occurrence)
        row = next((e for e in ctx.store.exceptions_for(master['id']) if e.get('recurrence_id') == key), row)
    with ctx.store._conn() as c:
        c.execute('BEGIN')
        state = _snapshot(c, row['id'])
    if state is None:
        return None, None, {'status': 'not_found'}
    templates = _templates(state)
    if row.get('sync_state') != 'conflict' and not any(i['state'] == 'conflict' for i in templates):
        return state, None, {'status': 'not_conflicted'}
    # A failed dependent points to the actionable head, not a nonexistent remote segment.
    head = next((i for i in templates if i['state'] == 'conflict'), None)
    if head and head['event_id'] != row['id']:
        return _load(ctx, head['event_id'])
    row = state['event']
    affected = {i['event_id'] for i in templates}
    independent = [i for i in state['intents'] if i['state'] in _UNFINISHED and i not in templates]
    body = {'status': 'conflict', 'event_id': row['id'], 'local': event_public(row, ctx.tz, compact=False),
            'operations': [{'id': i['id'], 'event_id': i['event_id'], 'kind': i['kind'], 'state': i['state']}
                           for i in templates],
            'affected_events': [event_public(r, ctx.tz, compact=False) for r in state['rows'] if r['id'] in affected and r['id'] != row['id']],
            'preserved_operations': [i['id'] for i in independent],
            'choice_scope': 'Only this calendar copy: local retries the listed dependent command; provider discards its unconfirmed changes, preserving independent earlier edits.'}
    cal = state['calendar']
    adapter = ctx.providers.adapter_for(cal['account_id']) if cal else None
    if adapter is None:
        return state, None, {**body, 'status': 'unavailable', 'message': 'аккаунт не подключён; выбор не применён'}
    master = next((r for r in state['rows'] if r['id'] == row.get('master_id')), {})
    address = {**row, 'master_external_id': master.get('external_id') or '',
               'master_href': master.get('href') or '', 'master_etag': master.get('etag') or ''}
    chosen = next((i for i in templates if i['event_id'] == row['id'] and i['state'] == 'conflict'), None)
    try:
        current = adapter.read_current(cal, address, _payload(chosen) if chosen else {})
    except ops.ProviderError as exc:
        if exc.kind not in ('not_found', 'gone'):
            return state, None, {**body, 'status': 'unavailable', 'message': exc.message}
        current = {'event': None, 'etag': ''}
    remote = current.get('event')
    body['provider'] = event_public(remote, ctx.tz, compact=False) if remote else None
    if remote is None or not current.get('etag'):
        return state, current, {**body, 'message': 'у провайдера нет выбранного события или ETag; '
                               'локальное намерение сохранено, пересоздание требует отдельного выбора'}
    try:
        current['targets'] = _read_targets(state, adapter, current, templates)
    except ops.ProviderError as exc:
        return state, current, {**body, 'status': 'unavailable', 'message': exc.message}
    body['provider_events'] = {eid: event_public(value['event'], ctx.tz, compact=False) if value['event'] else None
                               for eid, value in current['targets'].items() if eid != row['id']}
    encoded = json.dumps([_meaning(state), current], sort_keys=True, ensure_ascii=False, default=str).encode()
    body['resolution_token'] = hashlib.sha256(encoded).hexdigest()
    return state, current, body


def inspect(ctx, event_id):
    return _load(ctx, event_id)[2]


def _rollback_rows(state):
    """Earliest preimages of the unconfirmed command suffix, not just its head.

    A completed prefix has already changed the provider and is not rolled back.
    Later dependent commands may introduce topology their predecessor never saw.
    """
    before, created = {}, set()
    for intent in state['intents']:
        if intent['id'] not in state['command_ids'] or intent['state'] not in _UNFINISHED:
            continue
        payload = _payload(intent)
        for row in payload.get('restore_rows', []):
            before.setdefault(row['id'], row)
        created.update(payload.get('created_ids', []))
    return before, created


def _read_targets(state, adapter, current, templates):
    rows = {r['id']: r for r in state['rows']}
    before, created = _rollback_rows(state)
    targets = {state['event']['id']: {**{k: v for k, v in current.items() if k != 'targets'},
                                     'source_master_id': state['event'].get('master_id') or ''}}
    for intent in templates:
        eid = intent['event_id']
        if eid in targets:
            continue
        row = before.get(eid) or rows[eid]
        creates = [i for i in state['intents'] if i['event_id'] == eid and i['kind'] == 'create']
        if eid in created and creates and all(i['attempts'] == 0 for i in creates):
            targets[eid] = {'event': None, 'etag': '', 'never_sent': True}
            continue
        master = before.get(row.get('master_id')) or rows.get(row.get('master_id'), {})
        address = {**row, 'master_external_id': master.get('external_id') or '',
                   'master_href': master.get('href') or '', 'master_etag': master.get('etag') or ''}
        try:
            targets[eid] = adapter.read_current(state['calendar'], address, _payload(intent))
        except ops.ProviderError as exc:
            if exc.kind not in ('not_found', 'gone'):
                raise
            targets[eid] = {'event': None, 'etag': ''}
        targets[eid]['source_master_id'] = row.get('master_id') or ''
    return targets


def _clones(state, current, templates, new_op):
    ids = {i['id']: new_id('int') for i in templates}
    row = state['event']
    clones = []
    for original in templates:
        payload = {**_payload(original), 'op_id': new_op}
        dependency = payload.get('after_intent_id')
        if dependency in ids:
            payload['after_intent_id'] = ids[dependency]
        payload['prior_intent_ids'] = [ids.get(i, i) for i in payload.get('prior_intent_ids', [])]
        desired = payload.get('desired') or next(r for r in state['rows'] if r['id'] == original['event_id'])
        payload['desired'] = {k: v for k, v in desired.items() if k in EVENT_COLUMNS and k != 'raw_payload'}
        expected = original['expected_etag']
        observed = current['targets'].get(original['event_id'], {})
        # Inspection may read this occurrence at its old series address. A
        # retry targets the staged new resource: its create ACK binds the
        # CalDAV version, and Google resolves the new instance after creation.
        moving = payload.get('relocated_from_master') and (
            observed.get('source_master_id') != desired.get('master_id'))
        if moving:
            expected = ''
            payload.pop('_provider_external_id', None)
        elif observed.get('etag'):
            expected = observed['etag']
            if state['calendar']['provider'] == 'google':
                payload['_provider_external_id'] = (observed.get('event') or {}).get('external_id') or ''
        ts = now_utc().isoformat(timespec='microseconds')
        clones.append({**original, 'id': ids[original['id']], 'payload_json': json.dumps(payload),
                       'expected_etag': expected, 'state': 'pending', 'attempts': 0,
                       'lease_until': None, 'lease_owner': '', 'result_json': '{}',
                       'created_at': ts, 'updated_at': ts, 'next_attempt_at': ts})
    return clones


def _accept_provider(local, state, current, templates):
    row = state['event']
    affected = {i['event_id'] for i in templates}
    retired = {i['id'] for i in templates}
    independent = {i['event_id'] for i in state['intents'] if i['state'] in _UNFINISHED and i['id'] not in retired}
    before_rows, created = _rollback_rows(state)
    restore = list(before_rows.values())
    for before in restore:
        if before['id'] in affected:
            local.update_event(before['id'], {k: v for k, v in before.items() if k != 'id'})
    # Retain history but withdraw a segment whose prerequisite never succeeded.
    # A possibly sent create is never inferred absent from an empty identity.
    for eid in created:
        if eid not in affected or eid == row['id']:
            continue
        creates = [i for i in state['intents'] if i['event_id'] == eid and i['kind'] == 'create']
        existing = local.get_event(eid)
        never_sent = creates and all(i['attempts'] == 0 for i in creates)
        if existing and never_sent and eid not in independent:
            local.delete_event(eid)
            local.drop_reminders_for(eid)
    for eid in affected:
        if eid in independent:
            # The before-image contains independently accepted unsent content.
            before = before_rows.get(eid)
            if before:
                local.update_event(eid, {k: v for k, v in before.items() if k not in ('id','external_id','href','etag')})
            local.update_event(eid, {'sync_state': 'pending'})
        else:
            observed = current['targets'][eid]['event']
            if observed is None:
                local.delete_event(eid)
            else:
                fields = {k: observed[k] for k in _PROVIDER_FIELDS if k in observed}
                fields.update(sync_state='synced', deleted_at=None)
                local.update_event(eid, fields)
        local.drop_reminders_for(eid)
    local.set_sync_state(row['calendar_id'], cursor='')


def resolve(ctx, event_id, choice, token):
    if choice not in ('local', 'provider'):
        return {'status': 'error', 'message': 'resolution: local | provider'}
    state, current, body = _load(ctx, event_id)
    if not token or body.get('resolution_token') != token:
        return {**body, 'snapshot_changed': True, 'message': 'снимок изменился или недоступен; покажи обе версии заново'}
    row = state['event']
    templates = _templates(state)
    if choice == 'local' and (not templates or current['event'].get('status') == 'cancelled'
                              or any(i['kind'] == 'create' and i['event_id'] == row['id'] for i in templates)):
        return {**body, 'message': 'нет однозначной прежней операции для повтора; можно выбрать provider и задать новое изменение'}
    if choice == 'local' and not state['calendar'].get('writable', True):
        return {**body, 'status': 'error', 'message': 'календарь только для чтения'}
    new_op = new_id('op')
    clones = _clones(state, current, templates, new_op) if choice == 'local' else []
    changed = False
    with ctx.store.transaction() as local:
        with local._conn() as c:
            fresh = _snapshot(c, row['id'])
            changed = _meaning(fresh) != _meaning(state)
            if not changed:
                if any(parse_stored(i.get('lease_until')) and parse_stored(i['lease_until']) > now_utc()
                       for i in fresh['intents']):
                    return {**body, 'status': 'busy', 'message': 'операция этой серии ещё выполняется; дождись её исхода'}
                for i in templates:
                    local.settle_intent(i['id'], 'resolved', {'resolution': choice, 'superseded_by': new_op,
                                         'previous': json.loads(i.get('result_json') or '{}')})
                if choice == 'provider':
                    _accept_provider(local, state, current, templates)
                if clones:
                    for clone in clones:
                        columns = list(clone)
                        c.execute('INSERT INTO intents (' + ','.join(columns) + ') VALUES (' +
                                  ','.join('?' for _ in columns) + ')', tuple(clone[k] for k in columns))
                        local.update_event(clone['event_id'], {'sync_state': 'pending_delete' if clone['kind'] == 'delete' else 'pending'})
                    local.update_event(row['id'], {'raw_payload': current['event'].get('raw_payload') or ''})
    if changed:
        return {**inspect(ctx, event_id), 'snapshot_changed': True, 'message': 'локальный снимок изменился; выбор не применён'}
    return {'status': 'pending' if clones else 'resolved', 'event_id': row['id'], 'resolution': choice,
            'queued': [i['id'] for i in clones], 'affected_events': sorted({i['event_id'] for i in templates}),
            'message': 'перечисленные изменения поставлены заново; запись ещё не подтверждена' if clones else
                       'принята версия провайдера; зависимые неподтверждённые изменения этой копии отменены'}
