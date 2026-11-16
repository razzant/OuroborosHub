"""Owner decision 15 Nov 17:07: a Silent/Paused Place of a bare email address holds ANY delivery to that address,
Reply All included; a paused RFC conversation holds only that conversation.

Producer -> queue -> delivery against the real core Place endpoint (OUROBOROS_SOURCE_DIR, a core with the r6
``match`` wire): the automatic Presence reply through process_inbox, and email_send through MailService. Each
envelope address is bound with ``match=exact_casefold`` and checked with the mode stored beside its binding.
Provider delivery is an offline recorder; only the model turn is a fixture.
"""
import asyncio
import json

import httpx
import pytest

from lib.host_adapter import (LEGACY_REPLY_HELD, PLACE_POLICY_UNREAD, RECIPIENT_PLACES, HostDelivery, HostTurnStatus,
                              LoopbackPresenceHostAdapter, capture_recipients, recipient_key, reply_target_rows)
from lib.mime import parse_message
from test_binding_core import mail_context, release_hold

pytest_plugins = ['test_binding_core']

BOB = 'bob@example.invalid'


class TurnHost(LoopbackPresenceHostAdapter):
    """The real adapter and Place endpoint; only the model turn is a fixture that answers once."""
    def __init__(self, adapter, text='Automatic answer'):
        self.__dict__.update(adapter.__dict__)
        self.available, self.text, self.submitted = True, text, []
    async def discover_delivery_support(self):
        return 0
    async def submit(self, item):
        self.submitted.append(item.message_id)
        return 'fixture-turn'
    async def status(self, reference):
        return HostTurnStatus('ready', turn_ref='fixture-turn')
    async def deliver(self, reference):
        return HostDelivery((self.text,), 0, 'fixture-turn')


def mail(sender=BOB, reply_to='', message_id='<question@example.invalid>', references=''):
    raw = (f'From: {sender}\r\nTo: second@example.invalid\r\n' + (f'Reply-To: {reply_to}\r\n' if reply_to else '')
           + f'Message-ID: {message_id}\r\n' + (f'References: {references}\r\n' if references else '')
           + 'Subject: Question\r\n\r\nBody').encode()
    return {'uidvalidity': 1, **parse_message(raw, folder='INBOX', uid=1)}


def connect(places, drive, account, conversation='', state='active', *, account_scoped=True):
    return places.connect_place(drive, connector='email-presence', provider='email',
                                account_id=account if account_scoped else '', conversation_id=conversation,
                                scope='conversation' if conversation else 'account', state=state)


def setup(tmp_path, core_host, *, account_place='active'):
    places, drive, adapter = core_host
    accounts, first, second, service, store, delivery, runtime = mail_context(tmp_path, adapter)
    # email_send's producer uses the same real adapter as delivery.
    service.capture_recipients = lambda account, envelope: capture_recipients(adapter, account['id'], envelope)
    if account_place:
        connect(places, drive, second['id'], state=account_place)
    return places, drive, adapter, second, service, store, delivery, runtime


def automatic(runtime, adapter, store, message):
    """Intake -> one automatic final through process_inbox (which records the event's reply_targets)."""
    store.ingest(message)
    runtime.host = TurnHost(adapter)
    assert asyncio.run(runtime.process_inbox())
    item_key = f"INBOX:1:{message['uid']}:{message['message_id']}"
    return f'presence:{item_key}:final'


def deliver(runtime, store):
    """Release policy holds once and claim until nothing is due; the number of claims."""
    release_hold(store)
    claims = 0
    while runtime.process_outbox() and claims < 20:
        claims += 1
    return claims


def rows_of(receipt):
    record = receipt['delivery_reporting'][RECIPIENT_PLACES]
    assert record['match'] == 'exact_casefold'
    assert receipt['delivery_reporting']['place_match'][RECIPIENT_PLACES] == 'exact_casefold'
    return [(row['address'], row['key'], row['binding']['place_id']) for row in record['rows']]


def test_policy_key_folds_only_the_key():
    assert recipient_key('Bob@Example.INVALID') == BOB
    assert recipient_key(' "Bob" <Bob@Example.INVALID> ') == BOB
    assert reply_target_rows(['Bob@Example.INVALID', BOB]) == [
        {'conversation': BOB, 'effect': 'write', 'match': 'exact_casefold'}]


# --- the automatic final ------------------------------------------------------------------------------------

def test_automatic_reply_to_paused_address_is_held_then_released_once(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    bob = connect(places, drive, second['id'], BOB, 'paused')
    request = automatic(runtime, adapter, store, mail())
    receipt = store.receipt(request)
    assert receipt['state'] == 'held' and receipt['last_error'].endswith('is paused')
    assert rows_of(receipt) == [(BOB, BOB, bob.place_id)]
    assert receipt['delivery_reporting']['reply_targets'] == reply_target_rows([BOB])
    assert deliver(runtime, store) and not delivery.sent and store.receipt(request)['state'] == 'held'
    places.edit_place(drive, bob.place_id, 'set_state', author='owner', state='active')
    assert deliver(runtime, store) and len(delivery.sent) == 1 and delivery.sent[0][1] == [BOB]
    assert store.receipt(request)['state'] == 'completed'
    assert deliver(runtime, store) == 0 and len(delivery.sent) == 1


def test_automatic_reply_follows_the_actual_envelope_not_from(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    connect(places, drive, second['id'], BOB, 'paused')
    # Reply-To is the paused address: held although From has no Place.
    held = automatic(runtime, adapter, store, mail(sender='alice@example.invalid', reply_to=BOB))
    assert store.receipt(held)['state'] == 'held'
    # From is the paused address but Reply-To receives the reply: Bob gets no delivery, so nothing holds it.
    other = automatic(runtime, adapter, store, {**mail(sender=BOB, reply_to='list@example.invalid',
                                                       message_id='<second@example.invalid>'), 'uid': 2})
    assert store.receipt(other)['state'] == 'pending'
    assert deliver(runtime, store) and delivery.sent[-1][1] == ['list@example.invalid']


def test_unrelated_paused_rfc_conversation_does_not_hold_the_automatic_reply(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    connect(places, drive, second['id'], '<private@example.invalid>', 'paused')
    request = automatic(runtime, adapter, store, mail())
    assert store.receipt(request)['state'] == 'pending'
    assert deliver(runtime, store) and delivery.sent[-1][1] == [BOB]


def test_only_narrow_places_keep_an_unbound_snapshot_that_a_later_place_restricts(tmp_path, core_host):
    """No account Place: the reply's own RFC Place admits it. A recipient no Place names is a confirmed unbound
    snapshot (place_id ''), checked again at delivery: a Paused Place of that address connected later holds it."""
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host, account_place='')
    root = '<question@example.invalid>'
    connect(places, drive, second['id'], root, 'active')
    connect(places, drive, second['id'], '<private@example.invalid>', 'paused')
    request = automatic(runtime, adapter, store, mail())
    assert rows_of(store.receipt(request)) == [(BOB, BOB, '')]
    assert store.receipt(request)['state'] == 'pending'
    assert deliver(runtime, store) and delivery.sent[-1][1] == [BOB]
    again = automatic(runtime, adapter, store, {**mail(message_id='<two@example.invalid>', references=root), 'uid': 2})
    assert store.receipt(again)['state'] == 'pending'
    bob = connect(places, drive, second['id'], BOB, 'paused')
    assert deliver(runtime, store) and len(delivery.sent) == 1 and store.receipt(again)['state'] == 'held'
    places.edit_place(drive, bob.place_id, 'set_state', author='owner', state='active')
    assert deliver(runtime, store) and len(delivery.sent) == 2


def test_automatic_reply_place_paused_after_queueing_is_held_at_delivery(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    request = automatic(runtime, adapter, store, mail())
    assert store.receipt(request)['state'] == 'pending'
    bob = connect(places, drive, second['id'], BOB, 'silent')
    assert deliver(runtime, store) and not delivery.sent
    assert store.receipt(request)['state'] == 'held' and store.receipt(request)['last_error'].endswith('is silent')
    places.edit_place(drive, bob.place_id, 'set_state', author='owner', state='active')
    assert deliver(runtime, store) and len(delivery.sent) == 1


def test_accepted_automatic_reply_without_reply_targets_stays_held_and_unchanged(tmp_path, core_host):
    """A 0.10.10 automatic row was prepared before its recipients reached Safety: kept, never sent or rewritten."""
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    store.enqueue_outbox(request_id='presence:INBOX:1:1:<question@example.invalid>:final', recipients=[BOB],
                         subject='Re: Question', body='Accepted earlier', in_reply_to='<question@example.invalid>',
                         references=['<question@example.invalid>'],
                         reporting={'version': 0, 'account_id': second['id'], 'origin': {'kind': 'automatic'}})
    before = store.queued_message('presence:INBOX:1:1:<question@example.invalid>:final')
    for _ in range(3):
        assert deliver(runtime, store)
    receipt = store.receipt(before.request_id)
    assert not delivery.sent and receipt['last_error'] == LEGACY_REPLY_HELD and receipt['attempts'] == 0
    assert store.queued_message(before.request_id) == before


# --- email_send: To / Cc / Bcc ----------------------------------------------------------------------------

@pytest.mark.parametrize('field', ['to', 'cc', 'bcc'])
def test_send_to_paused_address_in_any_envelope_group_is_held(tmp_path, core_host, field):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    bob = connect(places, drive, second['id'], BOB, 'paused')
    args = {'to': 'carol@example.invalid', field: BOB} if field != 'to' else {'to': BOB}
    sent = service.send(account_id=second['id'], body='Hello', request_id='r', **args)
    assert sent['state'] == 'held'
    assert (BOB, BOB, bob.place_id) in rows_of(sent['receipt'])
    assert deliver(runtime, store) and not delivery.sent
    places.edit_place(drive, bob.place_id, 'set_state', author='owner', state='active')
    assert deliver(runtime, store) and len(delivery.sent) == 1 and BOB in delivery.sent[0][1]


def test_send_with_unrelated_paused_rfc_conversation_is_delivered(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    connect(places, drive, second['id'], '<private@example.invalid>', 'paused')
    sent = service.send(account_id=second['id'], to=BOB, cc='carol@example.invalid', body='Group', request_id='g')
    assert sent['state'] == 'pending'
    assert deliver(runtime, store) and delivery.sent[-1][1] == [BOB, 'carol@example.invalid']


def test_send_address_paused_after_queueing_is_held_at_delivery(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    sent = service.send(account_id=second['id'], to=BOB, body='Hello', request_id='r')
    assert sent['state'] == 'pending' and rows_of(sent['receipt']) == [(BOB, BOB, '')]
    connect(places, drive, second['id'], BOB, 'paused')
    assert deliver(runtime, store) and not delivery.sent and store.receipt('r')['state'] == 'held'


def test_wire_spelling_is_kept_and_a_lower_case_place_holds_it(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    bob = connect(places, drive, second['id'], BOB, 'paused')
    sent = service.send(account_id=second['id'], to='Bob@Example.INVALID', body='Hello', request_id='r')
    assert sent['state'] == 'held' and rows_of(sent['receipt']) == [('Bob@Example.INVALID', BOB, bob.place_id)]
    places.edit_place(drive, bob.place_id, 'set_state', author='owner', state='active')
    assert deliver(runtime, store) and delivery.sent[-1][1] == ['Bob@Example.INVALID']


def test_mixed_case_stored_place_restricts(tmp_path, core_host):
    """Formerly a recorded gap: a Place stored as Bob@Example.invalid now names bob@example.invalid (exact_casefold)."""
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    bob = connect(places, drive, second['id'], 'Bob@Example.invalid', 'paused')
    sent = service.send(account_id=second['id'], to=BOB, body='Hello', request_id='r')
    assert sent['state'] == 'held' and rows_of(sent['receipt']) == [(BOB, BOB, bob.place_id)]
    assert deliver(runtime, store) and not delivery.sent


def test_equal_rank_active_and_paused_spellings_hold(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    connect(places, drive, second['id'], BOB, 'active')
    connect(places, drive, second['id'], 'Bob@Example.INVALID', 'paused')
    sent = service.send(account_id=second['id'], to=BOB, body='Hello', request_id='r')
    assert sent['state'] == 'held'
    assert deliver(runtime, store) and not delivery.sent


def test_accountless_address_place_restricts_every_mailbox_and_an_account_one_only_its_own(tmp_path, core_host):
    """Formerly a recorded gap: an address Place without account_id lost to the Active account Place. Account-only
    Places are never exact candidates, so it now holds. One connected under another account does not."""
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    other = connect(places, drive, 'imap:another-mailbox', 'carol@example.invalid', 'paused')
    sent = service.send(account_id=second['id'], to='carol@example.invalid', body='Hello', request_id='own')
    assert sent['state'] == 'pending'
    connect(places, drive, second['id'], BOB, 'paused', account_scoped=False)
    held = service.send(account_id=second['id'], to=BOB, body='Hello', request_id='everywhere')
    assert held['state'] == 'held'
    places.edit_place(drive, other.place_id, 'disconnect', author='owner')


def test_paused_account_place_still_holds_every_recipient(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host, account_place='paused')
    sent = service.send(account_id=second['id'], to=BOB, body='Hello', request_id='r')
    assert sent['state'] == 'held'
    assert deliver(runtime, store) and not delivery.sent


def test_paused_mailbox_does_not_hold_recipients_of_an_explicitly_active_conversation(tmp_path, core_host):
    """The address dimension applies only Places naming an address: the paused mailbox Place is the email's own
    conversation matter, where an Active RFC Place keeps its exception for the automatic reply."""
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host, account_place='paused')
    connect(places, drive, second['id'], '<question@example.invalid>', 'active')
    request = automatic(runtime, adapter, store, mail())
    assert rows_of(store.receipt(request)) == [(BOB, BOB, '')]
    assert store.receipt(request)['state'] == 'pending'
    assert deliver(runtime, store) and delivery.sent[-1][1] == [BOB]
    bob = connect(places, drive, second['id'], BOB, 'paused')
    again = automatic(runtime, adapter, store, {**mail(message_id='<two@example.invalid>',
                                                       references='<question@example.invalid>'), 'uid': 2})
    assert store.receipt(again)['state'] == 'held'
    places.edit_place(drive, bob.place_id, 'disconnect', author='owner')
    connect(places, drive, second['id'], BOB, 'active')
    assert deliver(runtime, store) and len(delivery.sent) == 1  # a disconnected address binding stays held


# --- accepted rows are immutable ----------------------------------------------------------------------------

def test_accepted_row_without_recipient_bindings_is_checked_never_rewritten(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    store.enqueue_outbox(request_id='old', recipients=[BOB], subject='Re: Question', body='Accepted earlier',
                         in_reply_to='<question@example.invalid>', references=['<question@example.invalid>'],
                         reporting={'version': 0, 'account_id': second['id']})
    before = store.queued_message('old')
    bob = connect(places, drive, second['id'], 'Bob@Example.INVALID', 'paused')
    assert deliver(runtime, store) and not delivery.sent
    after = store.queued_message('old')
    assert store.receipt('old')['state'] == 'held' and after == before
    assert RECIPIENT_PLACES not in after.reporting
    places.edit_place(drive, bob.place_id, 'set_state', author='owner', state='active')
    assert deliver(runtime, store) and len(delivery.sent) == 1
    assert RECIPIENT_PLACES not in store.queued_message('old').reporting


def test_changed_envelope_or_record_is_never_sent(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    service.send(account_id=second['id'], to=BOB, body='Hello', request_id='r')
    with store._connect() as db:
        db.execute("""UPDATE outbox SET reporting_json=json_set(reporting_json,
                      '$.recipient_places.rows[0].address','eve@example.invalid') WHERE request_id='r'""")
    assert deliver(runtime, store) and not delivery.sent
    assert store.receipt('r')['last_error'].startswith('Held: Its recipients differ')


def test_new_format_row_missing_its_mode_is_corrupt_not_legacy(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    service.send(account_id=second['id'], to=BOB, body='Hello', request_id='r')
    with store._connect() as db:
        db.execute("""UPDATE outbox SET reporting_json=json_remove(reporting_json,
                      '$.place_match.recipient_places') WHERE request_id='r'""")
    assert deliver(runtime, store) and not delivery.sent
    assert store.receipt('r')['last_error'] == 'Held: Its retained place bindings are incomplete; it will not go out.'


class _Outage:
    """The real core endpoint, except that recipient (exact_casefold) requests answer 503 places_unavailable."""
    def __init__(self, inner):
        self.inner, self.down = inner, True
    def get(self, url, **kwargs):
        if self.down and (kwargs.get('params') or {}).get('match') == 'exact_casefold':
            return httpx.Response(503, json={'ok': False, 'code': 'places_unavailable', 'disposition': 'retry'},
                                  request=httpx.Request('GET', url))
        return self.inner.get(url, **kwargs)
    def close(self):
        pass


def test_unreadable_recipient_check_holds_without_spending_attempts(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    bob = connect(places, drive, second['id'], BOB, 'active')
    sent = service.send(account_id=second['id'], to=BOB, body='Hello', request_id='r')
    assert rows_of(sent['receipt']) == [(BOB, BOB, bob.place_id)]
    outage = _Outage(adapter._place_http)
    adapter._place_http = outage
    for _ in range(3):
        assert deliver(runtime, store)
    receipt = store.receipt('r')
    assert not delivery.sent and receipt['state'] == 'held' and receipt['last_error'] == PLACE_POLICY_UNREAD
    assert receipt['attempts'] == 0
    outage.down = False
    assert deliver(runtime, store) and len(delivery.sent) == 1


def test_unreadable_recipient_bind_queues_nothing(tmp_path, core_host):
    places, drive, adapter, second, service, store, delivery, runtime = setup(tmp_path, core_host)
    adapter._place_http = _Outage(adapter._place_http)
    with pytest.raises(httpx.HTTPStatusError):
        service.send(account_id=second['id'], to=BOB, body='Hello', request_id='r')
    assert store.queued_message('r') is None


# --- drafts deliver nothing ---------------------------------------------------------------------------------

def test_draft_to_paused_address_is_saved_and_its_folder_still_checked(tmp_path, monkeypatch, core_host):
    """A saved draft delivers nothing, so the owner's delivery rule does not refuse it. The real Host guard of the
    shipped manifest (to/cc/bcc are draft audience, no recipient group) and the draft's own folder check run."""
    from test_reply_identity_compatibility import configure_host, invoke
    from test_reply_source_authority import service_for
    from test_write_targets import host_guard
    account, call, store, client = service_for(tmp_path, 'imap')
    places, drive, adapter = core_host
    configure_host(monkeypatch, core_host, account, [])
    connect(places, drive, account, BOB, 'paused')
    guard = host_guard(tmp_path, monkeypatch, 'email_draft', account, [], drive=drive)
    args, done = invoke(call, 'email_draft', {'account_id': account, 'to': BOB, 'subject': 'Draft', 'body': 'Unsent'}, guard)
    assert done.get('ok') and client.saved and store.status()['outbox'] == {}
    drafts = connect(places, drive, account, 'Drafts', 'paused')
    args, refused = invoke(call, 'email_draft', {**args, 'body': 'Second'}, guard)
    assert 'refusal' in refused or refused.get('ok') is False
    places.edit_place(drive, drafts.place_id, 'set_state', author='owner', state='active')
    assert json.dumps(args)  # the prepared arguments stay plain JSON
