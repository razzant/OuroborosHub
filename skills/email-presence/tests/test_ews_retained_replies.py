"""Old EWS structured envelopes are evidence, never silently repaired and transmitted."""
from types import SimpleNamespace

import pytest

from lib.mime import parse_message
from lib.runtime import EmailRuntime
from lib.service import MailService
from lib.store import EmailStore
from test_binding_core import DeliveryClient, release_hold
from test_reply_source_authority import service_for

ENCODED = '=?utf-8?q?display=40example.invalid=2C_Actual?= <actual@example.invalid>'
MID = '<ews-old@example.invalid>'


def legacy_source():
    # Exact shape of the pre-fix EWS _structured_read projection; only display text survived.
    return {'folder': 'ews:inbox', 'uid': 1, 'uidvalidity': -1, 'message_id': MID,
            'source_format': 'ews_properties', 'message_ref': 'ews:old', 'provider_event_key': 'ews:old',
            'sender': 'display@example.invalid', 'reply_to': ['display@example.invalid', 'actual@example.invalid'],
            'recipients': ['owner@example.invalid'], 'cc': [], 'references': [], 'in_reply_to': '',
            'headers': {'from': ['display@example.invalid, Actual <actual@example.invalid>'],
                        'reply-to': ['display@example.invalid, Actual <actual@example.invalid>'],
                        'to': ['owner@example.invalid']}, 'subject': 's', 'body': 'Original'}


def test_old_capture_cannot_choose_automatic_or_reply_all_recipients(tmp_path):
    account, call, store, _ = service_for(tmp_path, 'ews', captured=[legacy_source()])
    runtime = EmailRuntime(store, object(), object(), account=account)
    with pytest.raises(ValueError, match='lost raw address syntax'):
        runtime._reply(store.inbox_event('ews:inbox', -1, 1), ['Answer'], 'new')
    with pytest.raises(ValueError, match='lost raw address syntax'):
        call('email_send', dict(account_id=account, reply_to_message_id=MID, reply_all=True, body='Answer'))
    assert not store.status()['outbox']


def test_old_cache_without_format_still_detected(tmp_path):
    account, call, store, _ = service_for(tmp_path, 'ews')
    old = legacy_source(); old.pop('source_format')  # old remember_read omitted source_format
    store.put('read_message:' + MID, old)
    with pytest.raises(ValueError, match='lost raw address syntax'):
        call('email_send', dict(account_id=account, reply_to_message_id=MID, reply_all=True, body='Answer'))


def test_retained_raw_syntax_can_reconstruct_new_reply_without_changing_capture(tmp_path):
    from lib.retained import reply_source
    old = legacy_source()
    old['raw_address_headers'] = {'from': [ENCODED], 'reply-to': [ENCODED], 'to': ['owner@example.invalid']}
    corrected = reply_source(old)
    assert corrected['sender'] == 'actual@example.invalid' and corrected['reply_to'] == ['actual@example.invalid']
    assert old['sender'] == 'display@example.invalid'  # immutable evidence remains


def test_accepted_bad_ews_row_survives_repeated_holds_and_source_refresh(tmp_path):
    store = EmailStore(tmp_path)
    old = legacy_source()
    store.remember_read(old)
    store.enqueue_outbox(request_id='accepted', recipients=old['reply_to'], subject='s', body='Private answer',
                         in_reply_to=MID, references=[])
    before = store.queued_message('accepted')
    # Fresh read must not erase the old accepted envelope's provenance.
    refreshed = {**old, 'sender': 'actual@example.invalid', 'reply_to': ['actual@example.invalid'],
                 'address_names': {'actual@example.invalid': 'display@example.invalid, Actual'}}
    store.remember_read(refreshed)
    delivery = DeliveryClient({'EMAIL_USER': 'owner@example.invalid'})
    runtime = EmailRuntime(store, delivery, object(), account='a', account_loader=lambda: {'sending': 'on'})
    for _ in range(6):
        release_hold(store)
        assert runtime.process_outbox() and not delivery.sent
        receipt = store.receipt('accepted')
        assert receipt['state'] == 'held' and receipt['attempts'] == 0
    after = store.queued_message('accepted')
    assert (after.request_id, after.message_id, after.recipients, after.body, after.references) == (
        before.request_id, before.message_id, before.recipients, before.body, before.references)


def test_uncertain_bad_row_is_never_reclaimed(tmp_path):
    store = EmailStore(tmp_path)
    store.ingest(legacy_source())
    store.enqueue_outbox(request_id='uncertain', recipients=['display@example.invalid'], subject='s', body='b',
                         in_reply_to=MID, references=[])
    claimed = store.claim_outbox()
    store.uncertain_outbox(claimed.row_id, claimed.lease_token, 'outcome unknown')
    delivery = DeliveryClient({'EMAIL_USER': 'owner@example.invalid'})
    runtime = EmailRuntime(store, delivery, object(), account='a', account_loader=lambda: {'sending': 'on'})
    assert not runtime.process_outbox() and not delivery.sent
    assert store.receipt('uncertain')['state'] == 'uncertain'


def test_fresh_selected_source_allows_new_request_but_never_rewrites_old(tmp_path, monkeypatch, core_host):
    from test_reply_identity_compatibility import configure_host, invoke
    from test_write_targets import host_guard
    old = legacy_source()
    fresh = {**old, 'sender': 'actual@example.invalid', 'reply_to': ['actual@example.invalid'],
             'address_names': {'actual@example.invalid': 'display@example.invalid, Actual'}}
    account, call, store, _ = service_for(tmp_path, 'ews', captured=[old], known=[fresh])
    store.enqueue_outbox(request_id='old', recipients=old['reply_to'], subject='s', body='Original accepted answer',
                         in_reply_to=MID, references=[])
    store.remember_read(fresh)
    drive, adapter = configure_host(monkeypatch, core_host, account, [(MID, 'active')])
    guard = host_guard(tmp_path, monkeypatch, 'email_send', account, [], drive=drive)
    _, result = invoke(call, 'email_send', dict(account_id=account, reply_to_message_id=MID,
                         reply_folder='ews:inbox', reply_all=True, body='New answer', request_id='new'), guard)
    assert result['ok']
    delivery = DeliveryClient({'EMAIL_USER': 'owner@example.invalid'})
    runtime = EmailRuntime(store, delivery, adapter, account=account, account_loader=lambda: {'sending': 'on'})
    assert runtime.process_outbox() and store.receipt('old')['state'] == 'held'
    assert runtime.process_outbox() and store.receipt('new')['state'] == 'completed'
    assert delivery.sent[0][1] == ['actual@example.invalid'] and len(delivery.sent) == 1
    assert store.queued_message('old').recipients == tuple(old['reply_to'])
