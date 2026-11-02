"""Retained and continuing receipt envelopes preserve the same selected reply target."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from lib.host_adapter import LoopbackPresenceHostAdapter, _ref
from lib.runtime import EmailRuntime
from lib.store import EmailStore


@pytest.mark.parametrize('kind', ['retained', 'continuing'])
@pytest.mark.parametrize('selection', ['released', 'terminal'])
@pytest.mark.parametrize('anchor_case', ['same_thread', 'absent', 'other_thread'])
def test_reply_anchor_matches_selected_mail_after_restart_and_deduplicates(tmp_path, kind, selection, anchor_case):
    store = EmailStore(tmp_path)
    for uid, sender in [(1, 'alice@example.invalid'), (2, 'bob@example.invalid')]:
        store.ingest({'uid': uid, 'uidvalidity': 1, 'message_id': f'<m{uid}>', 'sender': sender,
            'references': ['<other>' if uid == 2 and anchor_case == 'other_thread' else '<root>'],
            'subject': 'Thread', 'body': f'Question {uid}'})
    first = store.claim_inbox()
    reference = _ref(kind, {'response': {'turn_ref': 'author', 'disposition': 'retained'}} if kind == 'retained'
        else {'continuation_ref': 'author', 'turn_ref': 'author', 'outcome': 'deferred', 'text': ''})
    store.set_host_reference(first.row_id, first.lease_token, reference)
    store.retry_inbox(first.row_id, first.lease_token, '', 0)
    target = store.inbox_event('INBOX', 1, 2)
    anchor = {'source_event_id': target.provider_event_key,
              'message': {'folder': 'INBOX', 'uidvalidity': 1, 'imap_uid': 2, 'message_id': '<m2>'}}
    selected = {'output_ref': 'selection', 'outcome': 'message', 'text': 'Full selected answer',
                **({'reply_anchor': anchor} if anchor_case != 'absent' else {})}
    body = {'status': 'failed', 'outcome': 'silent', 'text': '', 'work_ref': 'author',
            'continuation_ref': 'author', 'continuation_version': 2, 'outputs': []}
    if selection == 'released':
        body['outputs'] = [selected]
    else:
        body.update(selected)
    async def exercise():
        polls, captures = [], []
        def handle(request):
            if request.url.path == '/identity':
                return httpx.Response(200, json={'presence_delivery_version': 1, 'presence_continuation_versions': [2]})
            assert request.url.path == '/presence/work/author'
            polls.append(True)
            return httpx.Response(200, json=body)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            host = LoopbackPresenceHostAdapter('a'*32, host_service_url='http://127.0.0.1:8767', skill_token='fixture', http_client=http)
            def capture(*args, **kwargs):
                captures.append(True)
                if len(captures) == 1:
                    raise httpx.ReadTimeout('Synthetic first binding failure')
                return {'supported': False}
            host.capture_binding = capture
            reopened = EmailStore(tmp_path)
            runtime = EmailRuntime(reopened, SimpleNamespace(settings={'EMAIL_USER': 'owner@example.invalid'}), host, account='mailbox')
            assert await runtime.process_inbox('observe')
            assert reopened.status()['inbox'] == {'pending': 1, 'retry': 1}
            assert reopened.status()['outbox'] == {}
            with reopened._connect() as db:
                db.execute('UPDATE inbox SET available_at=0 WHERE uid=1')
            assert await runtime.process_inbox('observe')
            assert len(polls) == 1  # exact paid result stayed cached during capture failure
            assert not await runtime.process_inbox('observe')
            with reopened._connect() as db:
                outgoing = dict(db.execute('SELECT request_id,recipients_json,in_reply_to,body FROM outbox').fetchone())
                saved = dict(db.execute('SELECT state,host_reference FROM inbox WHERE uid=1').fetchone())
            assert saved == {'state': 'failed', 'host_reference': reference}
            assert outgoing['body'] == 'Full selected answer'
            expected = ('bob@example.invalid', '<m2>') if anchor_case == 'same_thread' else ('alice@example.invalid', '<m1>')
            assert (json.loads(outgoing['recipients_json']), outgoing['in_reply_to']) == ([expected[0]], expected[1])
            assert outgoing['request_id'].endswith(':output:selection')
            status = await host.status(reference)
            for output in status.outputs:
                assert dict(output.anchor) == (anchor if anchor_case != 'absent' else {})
                runtime._reply(first, (output.text,), output.identity, status.delivery_reporting_version, output.turn_ref, output.anchor)
            assert reopened.status()['outbox'] == {'pending': 1}
    asyncio.run(exercise())


def test_previously_queued_output_keeps_its_original_recipient_after_anchor_recovery(tmp_path):
    store = EmailStore(tmp_path)
    for uid, sender in [(1, 'alice@example.invalid'), (2, 'bob@example.invalid')]:
        store.ingest({'uid': uid, 'uidvalidity': 1, 'message_id': f'<m{uid}>', 'sender': sender,
                      'references': ['<root>'], 'subject': 'Thread', 'body': 'Question'})
    first = store.claim_inbox()
    runtime = EmailRuntime(store, SimpleNamespace(settings={'EMAIL_USER': 'owner@example.invalid'}), SimpleNamespace())
    runtime._reply(first, ('Original selected answer',), 'output:selection')
    second = store.inbox_event('INBOX', 1, 2)
    anchor = {'source_event_id': second.provider_event_key,
              'message': {'folder': 'INBOX', 'uidvalidity': 1, 'imap_uid': 2, 'message_id': '<m2>'}}
    runtime._reply(first, ('Original selected answer',), 'output:selection', anchor=anchor)
    with store._connect() as db:
        outgoing = [dict(row) for row in db.execute('SELECT recipients_json,in_reply_to FROM outbox')]
    assert outgoing == [{'recipients_json': json.dumps(['alice@example.invalid']), 'in_reply_to': '<m1>'}]
