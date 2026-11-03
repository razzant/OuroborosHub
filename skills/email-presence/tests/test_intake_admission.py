"""Unadmitted No-Place mail settles honestly; admitted and retryable work keeps custody."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from lib.host_adapter import LoopbackPresenceHostAdapter, _decode, _ref
from lib.runtime import EmailRuntime
from lib.store import EmailStore
from test_binding_core import core_host as _core_host

core_host = _core_host


def receive(store, uid=1):
    store.ingest({'uid': uid, 'message_id': f'<incoming-{uid}>', 'sender': 'reader@example.invalid',
                  'subject': 'Synthetic', 'body': 'Private fixture body'})


def row(store):
    with store._connect() as db:
        return dict(db.execute('SELECT state,attempts,last_error,host_reference FROM inbox ORDER BY id LIMIT 1').fetchone())


def test_supported_no_place_settles_existing_mail_without_hiding_history_or_admitted_work(tmp_path, core_host):
    places, drive, adapter = core_host
    adapter.account_id = 'mailbox'
    sync = adapter._place_http
    def model_must_not_run(*args, **kwargs):
        pytest.fail('No-Place admission reached model work')
    sync.app.state.host_service_context.presence_runner = model_must_not_run
    posts, polls = [], []
    def transport(request):
        response = sync.request(request.method, str(request.url), headers=dict(request.headers), content=request.content)
        if request.url.path == '/presence/turn':
            posts.append((response.status_code, response.json()))
        return httpx.Response(response.status_code, json=response.json(), request=request)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            adapter._http = http
            store = EmailStore(tmp_path / 'old-mail')
            receive(store)
            client = SimpleNamespace(settings={'EMAIL_USER': 'owner@example.invalid'},
                                     poll=lambda *args, **kwargs: polls.append(True) or 0)
            runtime = EmailRuntime(store, client, adapter, account='mailbox')
            assert runtime.poll() == 0 and polls == []
            assert store.get('poll_health')['intake'] == 'no_place'
            assert await runtime.process_inbox('intake')
            first = row(store)
            assert first['state'] == 'failed' and first['attempts'] == 1
            assert 'place_not_connected' in first['last_error'] and 'blocked' in first['last_error']
            assert 'Private fixture body' not in first['last_error']
            assert len(posts) == 1 and posts[0][1]['code'] == 'place_not_connected'
            assert posts[0][1]['disposition'] == 'blocked'
            assert _decode(first['host_reference'], 'rejected')['response'] == posts[0][1]
            assert not await runtime.process_inbox('intake')
            assert store.inbox_event('INBOX', 0, 1).body == 'Private fixture body'
            assert adapter.capture_binding('mailbox', '<ordinary-tool>', require_place=False)['allowed']

            # An old admitted result still queues its selected speech under current
            # delivery policy; absence of an intake Place does not resubmit or erase it.
            admitted = EmailStore(tmp_path / 'admitted-mail')
            receive(admitted)
            item = admitted.claim_inbox()
            admitted.set_host_reference(item.row_id, item.lease_token,
                _ref('completed', {'status': 'completed', 'text': 'Already selected', 'turn_ref': 'old-turn'}))
            admitted.retry_inbox(item.row_id, item.lease_token, '', 0)
            observer = EmailRuntime(admitted, client, adapter, account='mailbox')
            assert await observer.process_inbox('observe')
            assert row(admitted)['state'] == 'completed' and len(posts) == 1
            assert admitted.status()['outbox_held'] == 1

            places.connect_place(drive, connector='email-presence', provider='email', account_id='mailbox', state='active')
            assert not await runtime.process_inbox('intake')  # no automatic historical replay
            assert row(store) == first
    asyncio.run(exercise())


@pytest.mark.parametrize('binding_version,code,disposition,terminal', [
    (1, 'place_not_connected', 'blocked', True),
    (0, 'place_not_connected', 'blocked', False),
    (1, 'presence_auth_blocked', 'blocked', False),
    (1, 'presence_capacity_full', 'retry', False),
    (1, 'presence_start_unwritable', 'retry', False),
    (1, 'unknown_admission_state', 'blocked', False),
])
def test_only_supported_no_place_is_terminal_and_retry_codes_stay_visible(tmp_path, binding_version, code, disposition, terminal):
    store = EmailStore(tmp_path)
    receive(store)
    async def exercise():
        posts = []
        def handle(request):
            if request.url.path == '/identity':
                return httpx.Response(200, json={'places_version': 1, 'presence_delivery_version': 1,
                    **({'presence_place_binding_version': binding_version} if binding_version else {})})
            posts.append(True)
            return httpx.Response(409, json={'ok': False, 'code': code, 'disposition': disposition,
                'field': 'conversation', 'error': 'Arbitrary private error text', 'retry_after': 0})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            host = LoopbackPresenceHostAdapter('', host_service_url='http://127.0.0.1:8767', skill_token='fixture', http_client=http)
            runtime = EmailRuntime(store, SimpleNamespace(), host, account='mailbox')
            assert await runtime.process_inbox('intake')
            saved = row(store)
            assert saved['state'] == ('failed' if terminal else 'retry')
            assert code in saved['last_error']
            if not terminal:
                assert disposition in saved['last_error'] and 'conversation' in saved['last_error']
                assert 'Arbitrary private error text' not in saved['last_error']
            assert bool(saved['host_reference']) == terminal
            assert len(posts) == 1
    asyncio.run(exercise())


def test_network_outage_keeps_unadmitted_mail_and_does_not_persist_exception_body(tmp_path):
    store = EmailStore(tmp_path)
    receive(store)
    async def exercise():
        def handle(request):
            if request.url.path == '/identity':
                return httpx.Response(200, json={'places_version': 1, 'presence_delivery_version': 1, 'presence_place_binding_version': 1})
            raise httpx.ReadTimeout('Private transport detail')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            host = LoopbackPresenceHostAdapter('', host_service_url='http://127.0.0.1:8767', skill_token='fixture', http_client=http)
            await EmailRuntime(store, SimpleNamespace(), host, account='mailbox').process_inbox('intake')
            assert row(store)['state'] == 'retry'
            assert row(store)['last_error'] == 'ReadTimeout'
            assert row(store)['host_reference'] == ''
    asyncio.run(exercise())
