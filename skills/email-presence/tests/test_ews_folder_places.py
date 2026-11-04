"""Run the registered Exchange tools through the Host before every attempt."""
import pytest

from lib.ews import EwsClient
from lib.ews_wire import NS
from test_declared_target_preparation import published_targets
from test_ews_mailbox import ACCOUNT, names
from test_ews_ownership import SharedStores, legacy_message
from test_write_targets import host_guard, tools_for


def handle(native):
    return f'ews-folder:{ACCOUNT["id"]}:{native}'


SOURCE, DEST, BIN, ROOT = (handle('B-' + value) for value in ('inbox', 'projects', 'deleteditems', 'msgfolderroot'))
MUTATIONS = {'MoveItem', 'CopyItem', 'UpdateItem', 'DeleteItem', 'CreateFolder'}


def scene(tmp_path):
    server = SharedStores()
    server.item_folder = 'inbox'
    exchange = EwsClient({**ACCOUNT, 'address': 'b@example.invalid'}, session=server)
    account, call = tools_for(tmp_path, 'ews', exchange, account_id=exchange.account['id'])
    return server, account, call


@pytest.mark.parametrize('action,selection,held,changed', [
    ('move', {'destination': 'Projects'}, SOURCE, True),
    ('move', {'destination': 'Projects'}, DEST, True),
    ('move', {'destination': DEST}, 'Projects', True),
    ('copy', {'destination': 'Projects'}, DEST, True),
    ('copy', {'destination': DEST}, 'Projects', True),
    ('copy', {'destination': DEST, 'folder': 'Posteingang'}, 'Posteingang', False),
    ('trash', {'destination': 'Decoy'}, BIN, True),
    ('trash', {'destination': 'Decoy'}, 'trash', True),
    ('trash', {}, 'deleteditems', True),
    ('trash', {}, 'Gelöscht', True),
    ('trash', {}, 'Posteingang', True),
    ('flags', {'flags': [r'\Flagged'], 'remove_flags': True}, SOURCE, True),
    ('mark_read', {}, 'inbox', True),
    ('create', {'folder': 'Projects', 'destination': 'Q4'}, DEST, True),
    ('create', {'folder': DEST, 'destination': 'Q4'}, 'Projects', True),
    ('create', {'destination': 'Q4'}, ROOT, True),
    ('create', {'destination': 'Q4'}, 'msgfolderroot', True),
    ('create', {'destination': 'Q4'}, 'Q4', True),
])
@pytest.mark.parametrize('restrict', [True, False])
def test_real_folder_guard_and_exact_soap_effect(tmp_path, monkeypatch, action, selection, held, changed, restrict):
    server, account, call = scene(tmp_path)
    tool = 'email_mark_read' if action == 'mark_read' else 'email_mailbox'
    mode = 'silent' if action in ('create', 'flags') else 'paused'
    guard = host_guard(tmp_path, monkeypatch, tool, account, [('', 'active'), (held, mode if restrict else 'active')])
    args = {'account_id': account, **({'action': action} if action != 'mark_read' else {}),
            **({'message_ref': legacy_message(ACCOUNT, 'B-msg'), 'folder': 'Decoy'} if action != 'create' else {}),
            'folder_names': ['Decoy'], **selection}
    for attempt in range(2):
        refusal = guard(args)
        if refusal:
            assert restrict and changed and mode.title() in refusal
            assert not MUTATIONS.intersection(names(server))
            return
        result = call(tool, args)
        if result.get('status') != 'needs_arguments':
            break
        assert attempt == 0 and not MUTATIONS.intersection(names(server))
        args = result['retry_arguments']
        parent = (ROOT if 'folder' not in selection else DEST) if action == 'create' else SOURCE
        parent_names = ({ROOT, 'Postfach', 'msgfolderroot'} if parent == ROOT else
                        {DEST, 'Projects'} if parent == DEST else {SOURCE, 'Posteingang', 'inbox'})
        destination_names = ({DEST, 'Projects'} if action in ('move', 'copy') else
                             {BIN, 'Gelöscht', 'trash', 'deleteditems'} if action == 'trash' else
                             {'Q4'} if action == 'create' else set())
        expected = destination_names | (parent_names if action != 'copy' else set())
        assert set(args['folder_names']) == expected
        assert set(published_targets(tool, args)['folder_names']) == expected
        if action not in ('copy', 'create'):
            assert args['folder'] == SOURCE and args['message_ref'] == legacy_message(ACCOUNT, 'B-msg')
        if action in ('copy', 'move', 'trash'):
            assert args['destination'] == (BIN if action == 'trash' else DEST)
    assert result['ok'] and not (restrict and changed)
    effects = [op for op in server.operations if op.tag.split('}')[-1] in MUTATIONS]
    assert len(effects) == 1
    op, = effects
    if action == 'create':
        assert op.tag.endswith('}CreateFolder') and op.findtext('.//t:DisplayName', namespaces=NS) == 'Q4'
        assert op.find('m:ParentFolderId/t:FolderId', NS).get('Id') == ('B-projects' if 'folder' in selection else 'B-msgfolderroot')
    elif action in ('flags', 'mark_read'):
        assert op.tag.endswith('}UpdateItem') and op.get('ConflictResolution') == 'NeverOverwrite'
        assert op.find('.//t:ItemId', NS).attrib == {'Id': 'B-msg', 'ChangeKey': 'key'}
        field, value = ('IsRead', 'true') if action == 'mark_read' else ('FlagStatus', 'NotFlagged')
        assert op.findtext('.//t:' + field, namespaces=NS) == value
    else:
        assert op.tag.endswith('}CopyItem' if action == 'copy' else '}MoveItem')
        assert op.find('m:ItemIds/t:ItemId', NS).get('Id') == 'B-msg'
        assert op.find('m:ToFolderId/t:FolderId', NS).get('Id') == ('B-deleteditems' if action == 'trash' else 'B-projects')


@pytest.mark.parametrize('action', ['copy', 'move', 'create'])
def test_name_reassignment_between_guarded_attempts_keeps_original_native_id(tmp_path, monkeypatch, action):
    server, account, call = scene(tmp_path)
    tool = 'email_mailbox'
    guard = host_guard(tmp_path, monkeypatch, tool, account, [('', 'active'), (handle('B-new-owner'), 'paused')])
    args = {'account_id': account, 'action': action, **({'folder': 'Projects', 'destination': 'New'} if action == 'create' else
            {'destination': 'Projects', 'message_ref': legacy_message(ACCOUNT, 'B-msg')})}
    assert not guard(args)
    first = call(tool, args)['retry_arguments']
    assert (first['folder'] if action == 'create' else first['destination']) == DEST
    initial_lookups = names(server).count('FindFolder')
    server.labels.update({'B-projects': ('Renamed', ''), 'B-new-owner': ('Projects', '')})
    server.folders.append('B-new-owner')
    assert not guard(first)
    second = call(tool, first)['retry_arguments']
    assert 'Renamed' in second['folder_names'] and 'Projects' not in second['folder_names']
    assert not MUTATIONS.intersection(names(server)) and not guard(second)
    assert call(tool, second)['ok'] and names(server).count('FindFolder') == initial_lookups
    final = server.operations[-1]
    location = 'ParentFolderId' if action == 'create' else 'ToFolderId'
    assert final.find('m:' + location + '/t:FolderId', NS).get('Id') == 'B-projects'


def test_restricted_folder_search_and_read_remain_available(tmp_path, monkeypatch):
    server, account, call = scene(tmp_path)
    for tool, extra in [('email_folders', {}), ('email_mailbox', {'action': 'list'}),
                        ('email_search', {'folder': SOURCE}), ('email_read', {'message_ref': legacy_message(ACCOUNT, 'B-msg')})]:
        guard = host_guard(tmp_path, monkeypatch, tool, account, [('', 'active'), (SOURCE, 'silent')])
        args = {'account_id': account, **extra}
        assert not guard(args) and call(tool, args)
    assert not MUTATIONS.intersection(names(server))
