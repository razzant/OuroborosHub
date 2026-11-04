"""A rejected login holds background mail without repeatedly presenting the password."""
import time
from types import SimpleNamespace

import pytest

from lib.accounts import AccountStore
from lib.ews import EwsClient
from lib.ews_wire import EwsError, EwsWire, m
from lib.runtime import EmailRuntime
from lib.store import EmailStore
from test_ews_wire import CONFIG, FakeResponse, Session, response


INBOX = '<m:Folders><t:Folder><t:FolderId Id="fixture-inbox"/></t:Folder></m:Folders>'


def test_held_outbox_retries_do_not_reauthenticate_until_connection_is_saved(tmp_path, monkeypatch):
    registry = AccountStore(tmp_path)
    fields = {'address': CONFIG['EMAIL_USER'], 'endpoint': CONFIG['EWS_ENDPOINT']}
    account = registry.save_ews({**fields, 'password': CONFIG['EMAIL_PASSWORD']})
    store = EmailStore(registry.mail_dir(account))
    now = [1_800_000_000.]
    monkeypatch.setattr(time, 'time', lambda: now[0])
    store.enqueue_outbox(request_id='one', to=['peer@example.invalid'], body='Keep this mail')
    denied = Session(*[FakeResponse('', 401) for _ in range(3)])
    runtime = EmailRuntime(store, EwsClient(account, session=denied), SimpleNamespace(), account=account['id'],
                           account_loader=lambda: registry.resolve(account['id']))
    for _ in range(3):
        assert runtime.process_outbox()
        now[0] += 300
    held = store.receipt('one')
    assert held['state'] == 'held' and 'save the connection' in held['last_error']
    assert len(denied.operations) == 1

    changed = registry.save_ews({**fields, 'account_id': account['id'], 'password': 'corrected-fixture'})
    assert changed['id'] == account['id'] and changed['generation'] != account['generation']
    accepted = Session(response('GetFolder', INBOX), response('CreateItem', '<m:Items/>'))
    runtime = EmailRuntime(store, EwsClient(changed, session=accepted), SimpleNamespace(), account=changed['id'],
                           account_loader=lambda: registry.resolve(changed['id']))
    assert runtime.process_outbox() and not runtime.process_outbox()
    assert store.receipt('one')['state'] == 'completed' and len(accepted.operations) == 2


def test_item_permission_refusals_do_not_latch_the_connection():
    session = Session(FakeResponse('', 403), response('CreateItem', klass='Error', code='ErrorSendAsDenied'),
                      response('GetFolder', INBOX))
    wire = EwsWire(CONFIG, session)
    with pytest.raises(EwsError) as acl:
        wire.call(m('GetItem'))
    assert acl.value.http_status == 403
    with pytest.raises(EwsError) as alias:
        wire.call(m('CreateItem'))
    assert alias.value.code == 'ErrorSendAsDenied'
    assert wire.call(m('GetFolder')).get('ResponseClass') == 'Success' and len(session.operations) == 3
