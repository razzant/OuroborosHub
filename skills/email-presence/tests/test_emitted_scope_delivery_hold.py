"""A transient Host failure while checking an old queued reply's emitted scope holds it; it never fails it.

An accepted queue row from before emitted bindings carries only its original place_binding. When its written
headers name another conversation, delivery checks that emitted scope with a fresh Host bind. Before this repair a
raised Host error there (restart, 503 places_unavailable, malformed answer) became an ordinary exception:
process_outbox spent an attempt per claim and marked the row failed at the fifth, before anything was sent. It is
now an unread policy, held like binding_hold's: the same row waits without spending attempts and goes out once
the Host answers. An explicit refusal stays held, as before.
"""
from contextlib import contextmanager

import httpx
import pytest

from lib.client import MailClient
from lib.host_adapter import (PLACE_POLICY_UNREAD, SKILL_ACCESS_REFUSED, HostContractError,
                              LoopbackPresenceHostAdapter)
from lib.mime import emitted_conversation
from lib.runtime import EmailRuntime
from lib.store import EmailStore

LEGACY = '<a@example.invalid> <b@example.invalid>'  # a source keyed by a raw multi-ID In-Reply-To
TOKEN = '<src@example.invalid>'
EMITTED = '<a@example.invalid>'


class Delivery(MailClient):
    def __init__(self):
        super().__init__({'EMAIL_USER': 'owner@example.invalid'})
        self.sent = []

    @contextmanager
    def delivery(self):
        yield self

    def transmit(self, _session, message, _recipients, **_):
        self.sent.append(str(message['Message-ID']))
        return {}, {'transport': 'offline'}


class Host:
    """The original binding is Active; the emitted scope's bind fails ``failures`` times, then answers ``answer``."""

    def __init__(self, failures, error, answer):
        self.failures, self.error, self.answer, self.binds = failures, error, answer, []

    def binding_hold(self, binding, account, conversation, thread_id=''):
        assert conversation == LEGACY
        return ''

    def place_hold(self, *_args, **_kwargs):
        pytest.fail('a Host with bindings is asked to bind the emitted scope, not for the old unbound check')

    def capture_binding(self, account, conversation, thread_id='', require_place=False, match=None):
        if match is not None:
            # The old row's envelope address, looked up exactly at delivery: no Place names it in this fixture.
            assert (conversation, thread_id, match) == ('peer@example.invalid', '', 'exact_casefold')
            return {'supported': True, 'allowed': True, 'state': 'unbound', 'place_id': '', 'binding': {'place_id': ''}}
        assert (conversation, thread_id) == (EMITTED, EMITTED)
        self.binds.append(conversation)
        if len(self.binds) <= self.failures:
            raise self.error
        return self.answer


def _old_row(tmp_path):
    assert emitted_conversation(TOKEN, [LEGACY]) == EMITTED != LEGACY
    store = EmailStore(tmp_path)
    store.enqueue_outbox(request_id='old-row', recipients=['peer@example.invalid'], subject='Re: x', body='Answer',
                         in_reply_to=TOKEN, references=[LEGACY],
                         reporting={'version': -1, 'account_id': 'acct', 'sender': 'owner@example.invalid',
                                    'place_binding': {'conversation_id': LEGACY, 'thread_id': LEGACY}})
    return store


def _claim(store, runtime):
    with store._connect() as db:
        db.execute("UPDATE outbox SET available_at=0 WHERE state='retry'")
        rows = [tuple(r) for r in db.execute('SELECT id,request_id FROM outbox')]
    assert rows == [(1, 'old-row')]  # the same row and request throughout: never deleted or re-queued
    return runtime.process_outbox()


def _status_error(status, code):
    request = httpx.Request('GET', 'http://127.0.0.1:8767/presence/place')
    return httpx.HTTPStatusError('Host', request=request, response=httpx.Response(status, json={'code': code}, request=request))


@pytest.mark.parametrize('error', [httpx.ConnectError('Host restarting'), _status_error(503, 'places_unavailable'),
                                   HostContractError('Host omitted the binding facts'), ValueError('not JSON')],
                         ids=['connect', 'http503', 'contract', 'json'])
def test_transient_emitted_check_failure_holds_without_spending_attempts(tmp_path, error):
    store = _old_row(tmp_path)
    host = Host(8, error, {'supported': True, 'allowed': True, 'binding': {'conversation_id': EMITTED}})
    delivery = Delivery()
    runtime = EmailRuntime(store, delivery, host, account='acct', account_loader=lambda: {'sending': 'on'})
    for _ in range(8):  # more failures than the five attempts that used to end the row
        assert _claim(store, runtime)
        receipt = store.receipt('old-row')
        assert (receipt['state'], receipt['last_error'], receipt['attempts']) == ('held', PLACE_POLICY_UNREAD, 0)
        assert delivery.sent == [] and receipt['history_reports'] == []
    assert _claim(store, runtime)  # the Host answers again: the same row goes out once
    receipt = store.receipt('old-row')
    assert receipt['state'] == 'completed' and len(delivery.sent) == 1
    assert runtime.process_outbox() is False and len(delivery.sent) == 1


@pytest.mark.parametrize(('answer', 'reason'), [
    ({'supported': True, 'allowed': False, 'state': 'paused', 'label': 'Email', 'binding': None}, 'Held: Email is paused'),
    (_status_error(403, 'presence_auth_blocked'), SKILL_ACCESS_REFUSED)])
def test_explicit_refusal_of_the_emitted_scope_stays_held(tmp_path, answer, reason):
    store = _old_row(tmp_path)
    failures = 99 if isinstance(answer, Exception) else 0
    host = Host(failures, answer, answer)
    delivery = Delivery()
    runtime = EmailRuntime(store, delivery, host, account='acct', account_loader=lambda: {'sending': 'on'})
    for _ in range(7):
        assert _claim(store, runtime)
        receipt = store.receipt('old-row')
        assert (receipt['state'], receipt['last_error'], receipt['attempts']) == ('held', reason, 0)
    assert delivery.sent == []


def test_real_adapter_outage_on_emitted_bind_holds_then_sends(tmp_path):
    store = _old_row(tmp_path)
    state = {'outage': True, 'binds': 0}

    def handler(request):
        if request.url.path == '/identity':
            return httpx.Response(200, json={'places_version': 1, 'presence_delivery_version': 1,
                                             'presence_place_binding_version': 1})
        operation = request.url.params.get('operation')
        if operation == 'check':
            assert request.url.params['conversation_id'] == LEGACY
            return httpx.Response(200, json={'allowed': True})
        assert operation == 'bind'
        if request.url.params['conversation_id'] != EMITTED:
            # The envelope address's own scope: no Place covers it in this fixture.
            return httpx.Response(200, json={'allowed': True, 'place_id': '', 'binding': {'place_id': ''}})
        state['binds'] += 1
        if state['outage']:
            return httpx.Response(503, json={'code': 'places_unavailable', 'disposition': 'retry'})
        return httpx.Response(200, json={'allowed': True, 'binding': {'conversation_id': EMITTED}})

    delivery = Delivery()
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        host = LoopbackPresenceHostAdapter('', host_service_url='http://127.0.0.1:8767', skill_token='fixture',
                                           place_http_client=http, sync_only=True)
        runtime = EmailRuntime(store, delivery, host, account='acct', account_loader=lambda: {'sending': 'on'})
        for _ in range(6):
            assert _claim(store, runtime)
            receipt = store.receipt('old-row')
            assert (receipt['state'], receipt['last_error'], receipt['attempts']) == ('held', PLACE_POLICY_UNREAD, 0)
        assert delivery.sent == [] and state['binds'] == 6
        state['outage'] = False
        assert _claim(store, runtime)
    assert store.receipt('old-row')['state'] == 'completed' and len(delivery.sent) == 1
