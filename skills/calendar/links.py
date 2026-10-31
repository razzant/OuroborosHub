"""Calendar link observations and source selection, independent of delivery state.

The event-level primary flag chooses one source for a relationship. A projection's
observed divergence survives provider ACKs; only an explicit link operation resumes
following. This module writes no provider data and creates no delivery queue.
"""
from model import parse_stored, BUSY_COPY_TITLE

FOLLOWING = 'following'
DIVERGED = 'diverged'
MISSING = 'missing'
COPY_FIELDS = ('title', 'description', 'location', 'start_utc', 'end_utc', 'tz', 'all_day',
               'rrule', 'rdates', 'exdates', 'status', 'availability')


def source(store, row):
    group = row.get('link_group_id') or ''
    if not group:
        return row
    primary = [member for member in store.group_members(group, include_deleted=True)
               if not member.get('master_id') and member.get('is_primary')]
    return primary[0] if len(primary) == 1 else None


def source_is(store, row):
    chosen = source(store, row)
    return chosen is not None and chosen['id'] == row['id'] and not chosen.get('deleted_at')


def followers(store, row):
    if not row.get('link_group_id') or not source_is(store, row):
        return []
    return [member for member in store.group_masters(row['link_group_id'])
            if member['id'] != row['id'] and member.get('link_state', FOLLOWING) == FOLLOWING]


def changed(previous, incoming):
    """A new ETag alone is not an edit of the projection's content."""
    for field in COPY_FIELDS:
        if field not in incoming:
            continue
        left, right = previous.get(field), incoming.get(field)
        if field in ('start_utc', 'end_utc'):
            left, right = parse_stored(left), parse_stored(right)
        elif field == 'all_day':
            left, right = bool(left), bool(right)
        elif field == 'tz':
            continue  # equivalent provider zone spellings do not change the instants
        else:
            left, right = left or '', right or ''
        if left != right:
            return True
    return False


def observe_projection(store, previous, incoming):
    """Keep the observed relation separate from its next delivery acknowledgement."""
    if not previous.get('link_group_id') or source_is(store, previous):
        return
    if changed(previous, incoming):
        store.update_event(previous['id'], {'link_state': DIVERGED})


def inspect(store, row):
    group = row.get('link_group_id') or ''
    if not group:
        return None
    chosen = source(store, row)
    members = [member for member in store.group_members(group, include_deleted=True) if not member.get('master_id')]
    return {'source_id': chosen['id'] if chosen else None,
            'source': {key: chosen.get(key) for key in ('id', 'title', 'calendar_id', 'calendar_name')} if chosen else None,
            'source_missing': bool(chosen and chosen.get('deleted_at')),
            'source_ambiguous': chosen is None,
            'state': row.get('link_state', FOLLOWING),
            'members': [{'id': member['id'], 'calendar_id': member['calendar_id'],
                         'calendar_name': member.get('calendar_name'),
                         'source': bool(chosen and member['id'] == chosen['id']),
                         'state': member.get('link_state', FOLLOWING), 'missing': bool(member.get('deleted_at'))}
                        for member in members]}


def projection_changes(store, source_row, projection):
    fields = {key: source_row.get(key) for key in COPY_FIELDS if source_row.get(key) is not None}
    calendar = store.get_calendar(projection['calendar_id']) or {}
    if calendar.get('publish_mode') == 'busy':
        fields.update(title=BUSY_COPY_TITLE, description='', location='')
    return fields


def select_source(store, row):
    """An explicit choice, never migration by row order. Pending intents are retained."""
    if not row.get('link_group_id') or row.get('deleted_at') or row.get('master_id'):
        raise ValueError('Choose an existing master event in a linked group.')
    members = store.group_members(row['link_group_id'], include_deleted=True)
    for member in members:
        primary = member['id'] == row['id'] or member.get('master_id') == row['id']
        fields = {'is_primary': int(primary)}
        if primary:
            fields['link_state'] = FOLLOWING
        store.update_event(member['id'], fields)
    return {'status': 'ok', 'source_id': row['id'], 'link': inspect(store, store.get_event(row['id']))}


def detach(store, row):
    """Detach only the selected master and its occurrences on an explicit command."""
    if row.get('master_id'):
        raise ValueError('Detach the series master, not one occurrence.')
    for event in [row] + store.exceptions_for(row['id']):
        store.update_event(event['id'], {'link_group_id': '', 'is_primary': 1, 'link_state': FOLLOWING})
    return {'status': 'ok', 'event_id': row['id'], 'link': None}
