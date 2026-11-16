"""A later email_read never erases an already known conflict between two messages with one Message-ID.

Before this repair, remember_read kept one record under read_message:<Message-ID>. Reading two different physical
messages that share an exact Message-ID but carry different reply authority (e.g. References) replaced the first
with the second, so message_by_id saw only the last read and a reply by that ID silently took its conversation
(from a Paused root to an Active one through the real Host guard). The legacy key still holds the latest read; the
replaced read of another physical message with different authority is kept under read_source:<digest>, so the ID
stays ambiguous and is refused. Repeat reads of one source, identical copies and older records are unchanged.
"""
import json

import pytest

from lib.accounts import public_message_ref
from lib.store import EmailStore
from test_reply_identity_compatibility import configure_host, invoke
from test_reply_source_authority import service_for, source
from test_write_targets import host_guard

pytest_plugins = ['test_binding_core']

OLD, NEW, MID = '<historical@example.invalid>', '<different@example.invalid>', '<duplicate@example.invalid>'


def _sources(provider, second_references=(NEW,)):
    native = {'gmail': ('gmail:a1', 'gmail:a2'), 'ews': ('ews:A1', 'ews:A2')}.get(provider)
    values = {}
    for index, references in ((1, [OLD]), (2, list(second_references))):
        value = source(MID, references=references, uid=index, uidvalidity=1, folder='INBOX')
        if native:
            value['message_ref'] = native[index - 1]
        values[index] = value
    return values


def _reader(provider, account, call, client, values):
    client.read = lambda **kw: dict(values[kw.get('uid') or (1 if kw['message_ref'].endswith('1') else 2)])
    def read(index):
        args = (dict(account_id=account, uid=index, uidvalidity=1, folder='INBOX') if provider == 'imap' else
                {'account_id': account, 'message_ref': public_message_ref(account, values[index]['message_ref'])})
        return call('email_read', args)
    return read


def _send(account, **changes):
    return {'account_id': account, 'to': 'peer@example.invalid', 'body': 'Private answer', 'subject': 'Re: source',
            'reply_to_message_id': MID, 'request_id': 'probe', **changes}


def _cache(store):
    with store._connect() as db:
        return {r[0]: json.loads(r[1]) for r in db.execute(
            "SELECT key,value FROM runtime_state WHERE key LIKE 'read_message:%' OR key LIKE 'read_source:%'")}


@pytest.mark.parametrize('provider', ['imap', 'gmail', 'ews'])
@pytest.mark.parametrize('order', [(1, 2), (2, 1), (1, 2, 1)])
@pytest.mark.parametrize('tool', ['email_send', 'email_draft'])
def test_conflicting_reads_keep_the_id_ambiguous(tmp_path, provider, order, tool):
    account, call, store, client = service_for(tmp_path, provider)
    read = _reader(provider, account, call, client, _sources(provider))
    for index in order:
        assert read(index)['message_id'] == MID
    with pytest.raises(ValueError, match='Ambiguous reply source'):
        call(tool, _send(account))
    assert client.lookups == [] and not client.saved and store.status()['outbox'] == {}
    cache = _cache(store)
    # The legacy key is unchanged in name and holds the latest read; one kept entry per other physical source.
    assert cache['read_message:' + MID]['references'] == [OLD if order[-1] == 1 else NEW]
    kept = [value for key, value in cache.items() if key.startswith('read_source:')]
    assert {tuple(value['references']) for value in kept} | {tuple(cache['read_message:' + MID]['references'])} == {(OLD,), (NEW,)}
    # Each read replaced by a conflicting one is kept once, under its own physical source.
    assert len(kept) == len(order) - 1 and len({value['uid'] for value in kept}) == len(kept)


@pytest.mark.parametrize('provider', ['imap', 'gmail', 'ews'])
def test_paused_root_stays_held_through_the_host_after_a_conflicting_read(tmp_path, monkeypatch, core_host, provider):
    account, call, store, client = service_for(tmp_path, provider)
    drive, _adapter = configure_host(monkeypatch, core_host, account, [(OLD, 'paused'), (NEW, 'active')])
    read = _reader(provider, account, call, client, _sources(provider))
    read(1)
    guard = host_guard(tmp_path, monkeypatch, 'email_send', account, [], drive=drive)
    _args, blocked = invoke(call, 'email_send', _send(account), guard)
    assert 'Paused' in blocked['refusal']
    read(2)
    with pytest.raises(ValueError, match='Ambiguous reply source'):
        invoke(call, 'email_send', _send(account), guard)
    assert store.status()['outbox'] == {} and client.lookups == []


@pytest.mark.parametrize('provider', ['imap', 'gmail', 'ews'])
def test_repeat_reads_and_identical_copies_still_reply_in_one_call(tmp_path, provider):
    account, call, store, client = service_for(tmp_path, provider)
    values = _sources(provider, second_references=(OLD,))  # Another physical copy with the same reply authority.
    read = _reader(provider, account, call, client, values)
    for index in (1, 1, 2, 1):
        read(index)
    assert not [key for key in _cache(store) if key.startswith('read_source:')]
    done = call('email_send', _send(account, conversation_id=OLD, thread_id=OLD))
    assert done['ok'] and client.lookups == []
    assert list(store.queued_message('probe').references) == [OLD]


def test_rereading_one_source_whose_parse_changed_is_not_a_conflict(tmp_path):
    account, call, store, client = service_for(tmp_path, 'gmail')
    values = _sources('gmail')
    read = _reader('gmail', account, call, client, values)
    read(1)
    values[1] = {**values[1], 'sender': 'renamed@example.invalid'}  # e.g. a corrected parser, same provider message
    read(1)
    assert not [key for key in _cache(store) if key.startswith('read_source:')]
    assert call('email_send', _send(account, to='renamed@example.invalid', conversation_id=OLD, thread_id=OLD))['ok']


def test_a_kept_source_is_refreshed_by_its_own_later_read(tmp_path):
    account, call, store, client = service_for(tmp_path, 'gmail')
    values = _sources('gmail')
    read = _reader('gmail', account, call, client, values)
    read(1), read(2)
    values[1] = {**values[1], 'sender': 'renamed@example.invalid'}
    read(2), read(1), read(2)
    kept = {value['message_ref']: value for key, value in _cache(store).items() if key.startswith('read_source:')}
    assert sorted(kept) == ['gmail:a1', 'gmail:a2']  # one entry per physical source, never a stale duplicate
    assert kept['gmail:a1']['sender'] == 'renamed@example.invalid'


def test_record_without_a_physical_source_keeps_the_earlier_replacement(tmp_path):
    """A read kept by an earlier version, or by a caller, without provider ref or IMAP UID names no physical
    message, so the newest read replaces it as before; no conflict is invented from it."""
    store = EmailStore(tmp_path)
    store.put('read_message:' + MID, {'message_id': MID, 'references': [OLD], 'sender': 'peer@example.invalid'})
    store.remember_read({**source(MID, references=[NEW]), 'uid': 2, 'uidvalidity': 1})
    assert store.message_by_id(MID)['references'] == [NEW]
    assert set(_cache(store)) == {'read_message:' + MID}


def test_conflict_with_a_captured_message_is_still_refused(tmp_path):
    account, call, store, client = service_for(tmp_path, 'imap', captured=[source(MID, references=[OLD])])
    read = _reader('imap', account, call, client, _sources('imap'))
    read(2)
    with pytest.raises(ValueError, match='Ambiguous reply source'):
        call('email_send', _send(account))
    assert store.status()['outbox'] == {}


def test_ambiguity_names_the_reply_folder_remedy_and_rereading_does_not_clear_it(tmp_path):
    account, call, store, client = service_for(tmp_path, 'imap')
    read = _reader('imap', account, call, client, _sources('imap'))
    read(1), read(2), read(2), read(1)
    with pytest.raises(ValueError, match='Ambiguous reply source.*reply_folder'):
        call('email_send', _send(account))
    assert client.lookups == [] and store.status()['outbox'] == {}


@pytest.mark.parametrize('provider', ['imap', 'gmail', 'ews'])
@pytest.mark.parametrize(('folder', 'root', 'allowed'), [('Archive', OLD, False), ('Lists', NEW, True)])
def test_explicit_reply_folder_selects_one_verified_source_still_guarded(tmp_path, monkeypatch, core_host, provider,
                                                                        folder, root, allowed):
    account, call, store, client = service_for(tmp_path, provider)
    drive, _adapter = configure_host(monkeypatch, core_host, account, [(OLD, 'paused'), (NEW, 'active')])
    values = _sources(provider)
    read = _reader(provider, account, call, client, values)
    read(1), read(2)
    selected = {'Archive': values[1], 'Lists': values[2]}
    def find_message(message_id, *, folder):
        client.lookups.append((message_id, folder))
        return dict(selected[folder])
    client.find_message = find_message
    guard = host_guard(tmp_path, monkeypatch, 'email_send', account, [], drive=drive)
    args, result = invoke(call, 'email_send', _send(account, reply_folder=folder), guard)
    # Every prepare step verifies the selected folder's source again; the store's conflict is never resolved by it.
    assert client.lookups and set(client.lookups) == {(MID, folder)}
    assert args['conversation_id'] == root
    if allowed:
        assert result['ok'] and list(store.queued_message('probe').references) == [NEW]
    else:
        assert 'Paused' in result['refusal'] and store.status()['outbox'] == {}


def test_explicit_reply_folder_holding_both_messages_is_still_refused(tmp_path):
    account, call, store, client = service_for(tmp_path, 'imap')
    values = _sources('imap')
    read = _reader('imap', account, call, client, values)
    read(1), read(2)
    from lib.mime import unique_reply_source
    client.find_message = lambda message_id, *, folder: unique_reply_source([values[1], values[2]], message_id)
    with pytest.raises(ValueError, match='Ambiguous reply source'):
        call('email_send', _send(account, reply_folder='INBOX'))
    assert store.status()['outbox'] == {}


def test_list_copy_read_beside_a_captured_original_is_selected_by_folder(tmp_path):
    """A mailing-list copy (different Reply-To) read from another folder beside the captured original."""
    original = {**source(MID, references=[]), 'reply_to': []}
    account, call, store, client = service_for(tmp_path, 'imap', captured=[original])
    copy = {**source(MID, references=[]), 'reply_to': ['list@example.invalid'], 'folder': 'Lists', 'uid': 7, 'uidvalidity': 3}
    store.remember_read(copy)
    with pytest.raises(ValueError, match='reply_folder'):
        call('email_send', _send(account))
    client.find_message = lambda message_id, *, folder: dict(original if folder == 'INBOX' else copy)
    done = call('email_send', _send(account, reply_folder='INBOX', conversation_id=MID, thread_id=MID))
    assert done['ok'] and store.queued_message('probe').in_reply_to == MID
