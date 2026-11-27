"""Gmail's transition repairs, on the ordinary queue and read paths.

Fictional mailboxes, an inert scripted host (test_gmail_transition.Host) and recording fake providers: nothing here
reaches Google, a real host or a real mailbox, and a "send" is only recorded.
"""
import asyncio
import base64
import json
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
import yaml

from lib.accounts import AccountError, AccountStore
from lib.gmail import GmailClient, GmailError
from lib.gmail_host import CARD, GmailSignIn, read_view, serves_locally, sync_gmail
from lib.runtime import EmailRuntime, _submitted_targets_key
from lib.store import EmailStore
from test_gmail_transition import HOST, IMPORTED, KEY, MOVED, Host, listed, mailbox, reader

SKILL = Path(__file__).resolve().parents[1]


def lost(_body):  # a handover answer that never arrives
    return httpx.Response(502, json={'ok': False})


def raw(store, request_id):  # the row's own state: `held` is the one no pass claims
    with store._connect() as db:
        return db.execute('SELECT state FROM outbox WHERE request_id=?', (request_id,)).fetchone()[0]


def drain(runtime):
    while runtime.process_outbox():
        pass


def test_mail_queued_where_the_host_named_no_consent_goes_under_the_one_it_names_now(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    sent = []

    def provider(request):  # an inert recorder in place of Gmail: nothing is delivered anywhere
        assert request.method == 'POST' and request.url.path == '/mail/messages/send'
        sent.append(request.headers['Authorization'])
        return httpx.Response(200, json={'id': f'{len(sent):x}', 'threadId': 'fixture-thread'})

    host = Host(handover=2)  # updated since these mails were queued: its list names each sign-in's consent
    host.held[KEY] = {**listed('cn-1', via='card'), 'consent': IMPORTED}
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        account = mailbox(accounts, gmail_host=dict(MOVED))
        sync_gmail(accounts)
        assert accounts.get('google:one')['gmail_host']['consent'] == IMPORTED
        store = EmailStore(accounts.mail_dir(account))
        for request_id, reporting in (('old', {}),  # no record of its access: queued by an earlier version
                                      ('v1', {'gmail_connection': {'host': 'cn-1'}}),  # the connection, no consent
                                      ('v2', {'gmail_connection': {'host': 'cn-1', 'consent': IMPORTED}})):
            store.enqueue_outbox(request_id=request_id, to=['reader@example.test'], body=request_id, reporting=reporting)
        accounts.save({**accounts.get('google:one'), 'gmail_host': dict(MOVED)})  # the local copy now lags: it names no consent
        client = GmailClient(accounts, 'google:one', http=mail)
        drain(EmailRuntime(store, client, SimpleNamespace(), account='google:one'))
        # A change begun under a connection with no consent is still refused where the host names one: it was named
        # before this host named consents, and a change is issued only under the access it began with.
        with pytest.raises(AccountError, match='changed while this operation was being prepared'):
            client._access_token(change=True, link={'host': 'cn-1'})
    # Rule A: the same mailbox, so each goes once under the host's current connection and consent.
    assert [store.receipt(rid)['state'] for rid in ('old', 'v1', 'v2')] == ['completed'] * 3
    assert sent == ['Bearer fixture-host-access'] * 3 and [body.get('consent') for body in host.tokens] == [IMPORTED] * 3
    assert all(store.receipt(rid)['delivery_reporting']['gmail_connection'] == {'host': 'cn-1', 'consent': IMPORTED}
               for rid in ('old', 'v1', 'v2'))


def test_mail_queued_by_an_earlier_version_goes_under_the_saved_sign_in_and_waits_for_sending_and_places(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    sent, paused = [], [True]

    def provider(request):  # an inert recorder in place of Gmail: nothing is delivered anywhere
        assert request.method == 'POST' and request.url.path == '/mail/messages/send'
        sent.append(request.headers['Authorization'])
        return httpx.Response(200, json={'id': f'{len(sent):x}', 'threadId': 'fixture-thread'})

    accounts = AccountStore(tmp_path)  # no sign-in service: Mail's own sign-in serves this mailbox
    account = mailbox(accounts)
    store = EmailStore(accounts.mail_dir(account))
    place = SimpleNamespace(place_hold=lambda account, conversation, thread_id='':  # a Place pauses one conversation
                            'Held: Work mail is paused' if paused[0] and conversation == store.message_id_for('paused') else '')
    current = {'id': accounts.gmail_connection('google:one')}
    store.enqueue_outbox(request_id='unrecorded', to=['reader@example.test'], body='queued by an earlier version')
    store.enqueue_outbox(request_id='replaced', to=['reader@example.test'], body='queued under an earlier sign-in',
                         reporting={'gmail_connection': {'id': 'fixture-earlier'}})
    store.enqueue_outbox(request_id='paused', to=['reader@example.test'], body='a place pauses it',
                         reporting={'gmail_connection': current})
    with httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        runtime = EmailRuntime(store, GmailClient(accounts, 'google:one', http=mail), place, account='google:one')
        accounts.update_preferences('google:one', {'sending': 'off'})  # Sending off holds every one of them
        runtime.account_loader = lambda: accounts.resolve('google:one')
        drain(runtime)
        assert [raw(store, rid) for rid in ('unrecorded', 'replaced', 'paused')] == ['retry'] * 3 and sent == []
        accounts.update_preferences('google:one', {'sending': 'on'})
        with store._connect() as db:
            db.execute("UPDATE outbox SET available_at=0 WHERE state='retry'")
        drain(runtime)
        assert [store.receipt(rid)['state'] for rid in ('unrecorded', 'replaced', 'paused')] == ['completed'] * 2 + ['held']
        status = store.status()
        assert status['outbox_held'] == 1 and status['outbox'] == {'completed': 2, 'retry': 1}
        paused[0] = False  # the place is active again
        with store._connect() as db:
            db.execute("UPDATE outbox SET available_at=0 WHERE request_id='paused'")
        drain(runtime)
    assert [store.receipt(rid)['state'] for rid in ('unrecorded', 'replaced', 'paused')] == ['completed'] * 3
    assert sent == ['Bearer fixture-old-access'] * 3
    assert {store.receipt(rid)['delivery_reporting']['gmail_connection']['id'] for rid in ('unrecorded', 'replaced')} == {
        current['id']}  # each went under the mailbox's own current sign-in


def test_a_mailbox_not_yet_reserved_keeps_the_reads_it_had_and_claims_no_permission(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    reads = []
    host = Host(handover=2)
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(reader(reads))) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        before = mailbox(store)  # saved by an earlier version: a recorded return, and a bearer that names no permissions
        _, (row,) = read_view(store)
        assert row['connected'] and row['auth_owner'] == 'legacy' and row['gmail_auth_state'] == 'signed_in'
        assert row['scopes'] is None and AccountStore.public(row)['scope_status'] == 'unknown'
        result = GmailClient(store, 'google:one', http=mail).test_connection()
        assert result['ok'] and result['scope_status'] == 'unknown' and reads == ['GET']
    saved = store.get('google:one')
    assert saved['generation'] == before['generation'] and 'gmail_transfer' not in saved and 'gmail_host' not in saved
    assert host.handovers == [] and host.tokens == []  # nothing was offered, and no host bearer was used for it


@pytest.mark.parametrize('fact', ['a sign-in of its subject on the card', 'a reserved transfer', 'a break the host recorded'])
def test_the_reads_it_had_end_only_with_a_fact_that_ends_them(tmp_path, monkeypatch, fact):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    reads = []
    host = Host(handover=2)
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(reader(reads))) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(store)
        if fact.startswith('a sign-in'):
            host.held[KEY] = listed('cn-1', via='card')
        elif fact == 'a reserved transfer':
            host.answers = [lost]  # its answer never arrives: the reservation stays unanswered
            sync_gmail(store)
            assert store.get('google:one')['gmail_transfer']
        else:
            (tmp_path / 'sign_in_withdrawn.json').write_text(
                json.dumps({'v': 1, 'accounts': [{'service': 'google', 'subject': 'one'}]}), encoding='utf-8')
        with pytest.raises(AccountError):
            GmailClient(store, 'google:one', http=mail).test_connection()
    assert reads == []


def test_the_hosts_own_receipt_of_a_closed_attempt_gives_the_saved_sign_in_back_and_confirms_nothing_else(
        tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    reads = []
    host = Host(handover=2)
    host.answers = [lost, host.imported]  # the first answer never arrives
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(reader(reads))) as mail:
        sign_in = GmailSignIn(*HOST, http=wire)
        store = AccountStore(tmp_path, sign_in=sign_in)
        original = mailbox(store, gmail_connection={'id': 'fixture-local'})
        sync_gmail(store)
        reserved = store.get('google:one')
        generation = reserved['gmail_transfer']['generation']
        assert not reserved['connected'] and 'refusal' not in reserved['gmail_transfer']  # dormant: the outcome is unknown
        host.filed[('google:one', generation)] = {'result': 'refused', 'reason': 'provider_unreachable', 'exchange': 'none'}  # the host's own receipt
        sync_gmail(store)
        closed = store.get('google:one')
        assert closed['connected'] and closed['gmail_transfer']['refusal'] == 'not_started'
        assert len(host.handovers) == 1  # nothing was submitted again
        assert host.reads == [{'service': 'google', 'receipt': {'legacy_key': 'google:one', 'legacy_generation': generation}}]
        # Only the saved sign-in's use came back: no owner, no continuation of earlier mail, no permission, no new connection.
        assert 'gmail_host' not in closed and closed['gmail_connection'] == {'id': 'fixture-local'}
        assert [closed[key] for key in ('oauth', 'tokens', 'scopes')] == [original[key] for key in ('oauth', 'tokens', 'scopes')]
        _, (row,) = read_view(store)
        assert row['auth_owner'] == 'legacy' and row['scopes'] is None
        assert GmailClient(store, 'google:one', http=mail).test_connection()['ok'] and reads == ['GET']
        sync_gmail(store)
        assert len(host.handovers) == 1  # offered again only after its pause
        sign_in.offers['google:one'] = (0, 30)  # that pause has passed
        sync_gmail(store)
    assert len(host.handovers) == 2 and host.handovers[1]['grants'][0]['legacy_generation'] != generation
    assert store.get('google:one')['gmail_host']['key'] == KEY


def test_an_ordinary_recover_answer_never_closes_an_attempt(tmp_path):
    host = Host(handover=2)

    def recover_none(body):  # the host says nothing was exchanged, to a recover request
        item, = body['grants']
        assert body['recover'] is True
        return httpx.Response(200, json={'ok': True, 'grants': [
            {'legacy_key': item['legacy_key'], 'result': 'refused', 'exchange': 'none'}]})

    host.answers = [lost, recover_none]
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(store)
        sync_gmail(store)
        sync_gmail(store)  # its own receipt says nothing proven: recover is asked, and its word closes nothing
    saved = store.get('google:one')
    assert [bool(body.get('recover')) for body in host.handovers] == [False, True] and len(host.reads) == 1
    assert 'refusal' not in saved['gmail_transfer'] and not saved['connected']


def test_a_refused_bearer_of_a_change_is_reported_to_the_host_at_once_and_the_change_is_not_repeated(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    posts = []

    def provider(request):  # an inert Gmail that refuses the bearer
        assert request.method == 'POST' and request.url.path == '/mail/drafts'
        posts.append(request.headers['Authorization'])
        return httpx.Response(401, json={'error': {'errors': [{'reason': 'authError'}]}})

    host = Host()
    host.access = 'fixture-stale'
    host.held[KEY] = listed('cn-1', via='card')
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(store, gmail_host=dict(MOVED))
        client = GmailClient(store, 'google:one', http=mail)
        with pytest.raises(GmailError) as refused:
            client.draft(to='reader@example.test', subject='Fixture', body='A draft')
    assert refused.value.status == 401 and posts == ['Bearer fixture-stale']  # sent once, never repeated
    assert [body.get('rejected') for body in host.tokens] == [None, 'fixture-stale']  # the host heard of that very bearer
    assert 'connection_id' not in host.tokens[1] and client._rejected_bearer is None  # and nothing waits to tell it


@pytest.mark.parametrize(('failure', 'seen', 'attempts'), [
    (httpx.ConnectError, ['retry'] * 4 + ['failed'], 5), (httpx.ConnectTimeout, ['retry'] * 4 + ['failed'], 5),
    (httpx.ReadTimeout, ['uncertain'], 1)])
def test_a_send_that_never_connected_is_no_uncertain_send_but_a_lost_answer_is(tmp_path, monkeypatch, failure, seen, attempts):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    calls, states = [], []

    def provider(request):  # an inert provider: the request never connects, or its answer is lost
        calls.append(request.method)
        raise failure('fixture', request=request)

    accounts = AccountStore(tmp_path)  # no sign-in service: Mail's own sign-in serves this mailbox
    account = mailbox(accounts)
    store = EmailStore(accounts.mail_dir(account))
    store.enqueue_outbox(request_id='send', to=['reader@example.test'], body='hello',
                         reporting={'gmail_connection': {'id': accounts.gmail_connection('google:one')}})
    with httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        runtime = EmailRuntime(store, GmailClient(accounts, 'google:one', http=mail), SimpleNamespace(), account='google:one')
        for _ in range(6):
            with store._connect() as db:
                db.execute('UPDATE outbox SET available_at=0')
            if not runtime.process_outbox():
                break
            states.append(store.receipt('send')['state'])
    assert states == seen and len(calls) == attempts  # a lost answer is never sent again
    words = store.receipt('send')['last_error']
    assert ('nothing was sent' if failure is not httpx.ReadTimeout else 'inspect Sent') in words


def test_the_sign_in_card_says_what_full_gmail_access_gives_before_sign_in_and_points_to_the_card_by_its_name():
    manifest = yaml.safe_load(SKILL.joinpath('SKILL.md').read_text(encoding='utf-8').split('---', 2)[1])
    service, = manifest['sign_in']
    optional = {row['scope']: row for row in service['optional_scopes']}
    settings, full = optional['https://www.googleapis.com/auth/gmail.settings.basic'], optional['https://mail.google.com/']
    assert (settings['default'], full['default']) == (True, False) and len(optional) == 2  # no choice was added or dropped
    assert service['scopes'] == ['openid', 'email', 'https://www.googleapis.com/auth/gmail.modify']
    assert full['label'].startswith('Full Gmail access') and 'permanent deletion that skips Trash' in full['label']
    assert 'Gmail settings' in full['label'] and 'Full Gmail access' in service['notes']['refused']
    # The limits the host's declaration reader enforces (service_sign_in_declaration): labels 120, notes 600 characters.
    assert all(0 < len(row['label']) <= 120 for row in service['optional_scopes'])
    assert all(0 < len(text) <= 120 for text in service['scope_labels'].values())
    assert all(0 < len(text) <= 600 for text in service['notes'].values())
    assert set(service['notes']) <= {'client', 'refused', 'refresh_rejected'}
    name = manifest['name']  # the installed card's exact technical name: what is drawn there may differ from the catalog's title
    assert name == 'email-presence' and name in CARD and 'Extensions › Skills' in CARD and 'Email Presence' not in CARD
    assert f'Extensions › Skills › {name}' in SKILL.joinpath('ui', 'accounts.js').read_text(encoding='utf-8')
    from lib.gmail import ACCESS_CHANGED  # a change stopped by a sign-in that changed meanwhile says nothing happened
    assert 'nothing was sent or changed' in ACCESS_CHANGED and 'request_id' not in ACCESS_CHANGED
    entry = SKILL.parents[1] / 'entries' / 'email-presence.json'
    if entry.is_file():  # tested inside its catalog: it lists this very skill, under a title that is true of the catalog only
        assert json.loads(entry.read_text(encoding='utf-8'))['name'] == name


RECORDED = {'without Gmail access': ['openid', 'email'], 'empty': [], 'garbage': 'x'}  # recorded, so not "never recorded"


@pytest.mark.parametrize('recorded', ['absent', 'unknown', *RECORDED])
def test_a_grant_that_never_recorded_its_permissions_keeps_ordinary_reading_and_nothing_else(tmp_path, monkeypatch, recorded):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    seen, posts = [], []

    def provider(request):  # an inert Gmail: a read is answered, a change would be recorded
        (seen if request.method == 'GET' else posts).append(request.headers['Authorization'])
        return httpx.Response(200, json={'emailAddress': 'one@example.test'})

    host = Host(handover=2)
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        saved = mailbox(store, scopes=RECORDED.get(recorded))
        if recorded == 'absent':  # a record that has no such field at all
            saved.pop('scopes')
            store.save(saved)
        client = GmailClient(store, 'google:one', http=mail)
        if recorded in RECORDED:  # permissions without Gmail, an empty answer or garbage were recorded: a refusal, not unknown
            with pytest.raises(AccountError, match='has not been confirmed'):
                client.test_connection()
            assert seen == [] and not serves_locally(store, store.get('google:one'))
            return
        _, (row,) = read_view(store)
        assert row['connected'] and row['auth_owner'] == 'legacy' and row['scopes'] is None  # no permission is claimed
        assert serves_locally(store, store.get('google:one'))  # so Mail, not the card, can disconnect it
        assert client.test_connection()['scope_status'] == 'unknown' and seen == ['Bearer fixture-old-access']
        # Reading only: nothing is changed, queued or offered under this, and an offer still needs recorded permissions.
        with pytest.raises(AccountError, match='has not been confirmed'):
            client.draft(to='reader@example.test', subject='Fixture', body='A draft')
        with pytest.raises(AccountError, match='Nothing was queued'):
            client.connection_link()
        sync_gmail(store)
    assert posts == [] and host.handovers == [] and 'gmail_transfer' not in store.get('google:one')


def test_the_old_grant_renews_its_bearer_without_naming_permissions_it_never_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    asked = []

    def provider(request):  # an inert Google and Gmail
        if request.url.path == '/token':
            asked.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={'access_token': 'fixture-renewed', 'expires_in': 3600})
        assert request.url.path == '/mail/profile' and request.headers['Authorization'] == 'Bearer fixture-renewed'
        return httpx.Response(200, json={'emailAddress': 'one@example.test'})

    host = Host(handover=2)
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(store, scopes=None, tokens={'refresh_token': 'fixture-old-refresh', 'expires_at': 0})
        result = GmailClient(store, 'google:one', http=mail).test_connection()
    saved = store.get('google:one')
    assert result['ok'] and result['scope_status'] == 'unknown' and len(asked) == 1
    assert 'scope' not in asked[0] and asked[0]['refresh_token'] == ['fixture-old-refresh']  # no limit to ask for
    assert saved['tokens']['access_token'] == 'fixture-renewed' and 'scope' not in saved['tokens'] and saved['scopes'] is None


def test_mail_queued_before_a_new_sign_in_the_local_copy_has_not_seen_goes_under_that_sign_in(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    sent = []

    def provider(request):  # an inert recorder in place of Gmail: nothing is delivered anywhere
        assert request.method == 'POST' and request.url.path == '/mail/messages/send'
        sent.append(request.headers['Authorization'])
        return httpx.Response(200, json={'id': f'{len(sent):x}', 'threadId': 'fixture-thread'})

    host = Host(handover=2)
    host.held[KEY] = {**listed('cn-1', via='card'), 'consent': IMPORTED}
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        account = mailbox(accounts, gmail_host=dict(MOVED))
        sync_gmail(accounts)  # the local copy names the first sign-in's consent
        store = EmailStore(accounts.mail_dir(account))
        client = GmailClient(accounts, 'google:one', http=mail)
        store.enqueue_outbox(request_id='old', to=['reader@example.test'], body='queued under the first sign-in',
                             reporting={'gmail_connection': client.connection_link()})
        host.held[KEY] = {**host.held[KEY], 'consent': 'd' * 32}  # a new sign-in on the card: the local copy is behind
        store.enqueue_outbox(request_id='new', to=['reader@example.test'], body='queued under the new sign-in',
                             reporting={'gmail_connection': client.connection_link()})
        drain(EmailRuntime(store, client, SimpleNamespace(), account='google:one'))
    # Each is named the host's current row as it goes out, never the stale local copy: both go once, under it.
    assert [store.receipt(rid)['state'] for rid in ('old', 'new')] == ['completed'] * 2
    assert sent == ['Bearer fixture-host-access'] * 2 and [body.get('consent') for body in host.tokens] == ['d' * 32] * 2


def test_a_sign_in_host_that_cannot_be_reached_for_the_bearer_keeps_the_mail_waiting_until_it_can(tmp_path, monkeypatch):
    """The host lists the mailbox's sign-in, but its token request never connects. Nothing was sent: the mail waits, held
    for access and spending no attempt, however long that lasts, and goes once the host answers again (rule A)."""
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    asked, sent, reachable = [], [], {'now': False}
    host = Host()
    host.held[KEY] = listed('cn-1', via='card')

    def wire(request):  # the sign-in host's token request never connects; its other answers are the fixture's
        if request.url.path == '/sign-in/token' and not reachable['now']:
            asked.append(request.url.path)
            raise httpx.ConnectError('refused', request=request)
        return host(request)

    def gmail(request):  # an inert recorder in place of Gmail: nothing is delivered anywhere
        assert request.method == 'POST' and request.url.path == '/mail/messages/send'
        sent.append(request.headers['Authorization'])
        return httpx.Response(200, json={'id': 'a1', 'threadId': 'fixture-thread'})

    with httpx.Client(transport=httpx.MockTransport(wire)) as http, \
            httpx.Client(transport=httpx.MockTransport(gmail)) as mail:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=http))
        account = mailbox(accounts, gmail_host=dict(MOVED))
        store = EmailStore(accounts.mail_dir(account))
        store.enqueue_outbox(request_id='send', to=['reader@example.test'], body='hello',
                             reporting={'gmail_connection': {'host': 'cn-1'}})
        runtime = EmailRuntime(store, GmailClient(accounts, 'google:one', http=mail), SimpleNamespace(), account='google:one')
        states = []
        for _ in range(8):
            with store._connect() as db:
                db.execute('UPDATE outbox SET available_at=0')
            assert runtime.process_outbox()
            states.append(store.receipt('send')['state'])
            with store._connect() as db:
                pause, attempts = db.execute("SELECT available_at - updated_at, attempts FROM outbox").fetchone()
            assert pause >= 25 and attempts == 0  # asked again after a pause, no attempt spent
        assert states == ['held'] * 8 and len(asked) == 8 and sent == []  # certain: nothing was sent
        assert store.receipt('send')['last_error'] == 'Held: The Google sign-in host did not return a readable answer.'
        reachable['now'] = True
        with store._connect() as db:
            db.execute('UPDATE outbox SET available_at=0')
        assert runtime.process_outbox()
    assert store.receipt('send')['state'] == 'completed' and sent == ['Bearer fixture-host-access']


@pytest.mark.parametrize(('result', 'continues'), [('imported', True), ('already_present', False)])
def test_the_consent_mail_continues_under_comes_only_from_an_imported_receipt(tmp_path, result, continues):
    host = Host(handover=2)

    def filed(body):  # the host files the grant and lists it; its receipt names the consent either way
        item, = body['grants']
        host.held[KEY] = {**listed('cn-1', generation=item['legacy_generation']), 'consent': IMPORTED}
        return httpx.Response(200, json={'ok': True, 'grants': [{
            'legacy_key': item['legacy_key'], 'result': result, 'account': {'key': KEY}, 'consent': IMPORTED,
            'legacy_generation': item['legacy_generation'], 'connection_id': 'cn-1'}]})

    host.answers = [filed]
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(accounts, gmail_connection={'id': 'fixture-local'})
        sync_gmail(accounts)
    binding = accounts.get('google:one')['gmail_host']
    assert binding['key'] == KEY and (binding.get('continues') == 'fixture-local') is continues


def test_a_client_without_a_registry_still_gets_gmail_error_for_a_refused_change():
    client = object.__new__(GmailClient)  # a stand-in with no account registry, as some fixtures build
    client.account = {}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401, json={}))) as http:
        client.http = http
        with pytest.raises(GmailError) as refused:
            client._request('POST', '/drafts', body={}, access='fixture-stale')
    assert refused.value.status == 401


@pytest.mark.parametrize(('receipt', 'closes'), [
    ({'result': 'refused', 'reason': 'provider_unreachable', 'exchange': 'none'}, True),  # the form the host confirmed
    ({'result': 'refused', 'reason': 'any other words', 'exchange': 'none'}, True),  # the fact is read, not the words
    ({'result': 'refused', 'exchange': 'none'}, True),
    ({'result': 'refused', 'reason': 'provider_unreachable', 'exchange': 'unknown'}, False),
    ({'result': 'refused', 'reason': 'provider_unreachable', 'exchange': 'saved'}, False)])
def test_a_closed_attempt_is_read_as_the_fact_and_nothing_else_closes_one(tmp_path, receipt, closes):
    host = Host(handover=2)
    host.answers = [lost, lost]  # the first answer never arrives; a recover request finds nothing either
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(store)
        sync_gmail(store)
        generation = store.get('google:one')['gmail_transfer']['generation']
        host.filed[('google:one', generation)] = receipt
        sync_gmail(store)
    saved = store.get('google:one')
    assert saved['connected'] is closes and (saved['gmail_transfer'].get('refusal') == 'not_started') is closes


def test_a_transfer_the_hosts_own_record_offers_again_goes_once_more_as_reserved_with_its_saved_sign_in_parked(tmp_path):
    host = Host(handover=2)

    def resubmit(body):  # only an outage stopped it before any exchange, while a Disconnect of another mailbox is known
        item, = body['grants']
        refusal = {'result': 'refused', 'reason': 'provider_unreachable', 'exchange': 'resubmit'}
        host.filed[(item['legacy_key'], item['legacy_generation'])] = refusal  # the host's own record of it
        return httpx.Response(200, json={'ok': True, 'grants': [{'legacy_key': item['legacy_key'], **refusal}]})

    host.answers = [resubmit, host.imported]
    (tmp_path / 'sign_in_withdrawn.json').write_text(json.dumps({'v': 1, 'accounts': [  # that other mailbox's break
        {'service': 'google', 'account': 'a-' + '2' * 20, 'subject': 'two'}]}), encoding='utf-8')
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        mailbox(store)  # its saved bearer has no record of its permissions: the offer hands only the refresh token
        sync_gmail(store)
        parked = store.get('google:one')
        assert not parked['connected'] and 'refusal' not in parked['gmail_transfer']  # the old path is not given back
        sync_gmail(store)
    first, again = host.handovers
    assert again == first and 'access_token' not in again['grants'][0]['tokens']  # its own offer and registration, no recover
    assert len(host.reads) == 1
    moved = store.get('google:one')
    assert moved['gmail_host']['key'] == KEY and moved['connected'] and 'gmail_transfer' not in moved


def letter(message_id):  # the format=raw source Gmail returns for a fictional mail
    message = EmailMessage()
    message['Message-ID'], message['From'], message['Subject'] = message_id, 'reader@example.test', 'Fixture'
    message.set_content('Hello.')
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip('=')


@contextmanager
def hosted(tmp_path, monkeypatch, provider, events):
    # A host-owned fictional mailbox on an inert scripted host, beside a recording provider. The host's list and bearer
    # requests are noted in `events`, cleared once the mailbox is set up. Yields (host, accounts, service, provider client).
    from lib import service as mail_service

    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    host = Host(handover=2)
    host.held[KEY] = {**listed('cn-1', via='card'), 'consent': IMPORTED}

    def wire(request):
        if request.url.path == '/sign-in/accounts':
            events.append('list')
        elif request.url.path == '/sign-in/token':
            events.append(('token', json.loads(request.content).get('connection_id')))
        return host(request)

    with httpx.Client(transport=httpx.MockTransport(wire)) as hosted_wire, \
            httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        sign_in = GmailSignIn(*HOST, http=hosted_wire)
        monkeypatch.setattr(mail_service.GmailSignIn, 'environment', classmethod(lambda cls: sign_in))
        monkeypatch.setattr(mail_service, 'account_client',
                            lambda accounts, row, **_: GmailClient(accounts, row['id'], http=mail))
        accounts = AccountStore(tmp_path, sign_in=sign_in)
        mailbox(accounts, gmail_host=dict(MOVED))
        sync_gmail(accounts)  # the local copy names the connection and the consent the host lists
        events.clear()
        service = mail_service.MailService(SimpleNamespace(get_state_dir=lambda: str(tmp_path), get_settings=lambda keys: {}))
        yield host, accounts, service, mail


def test_a_change_is_issued_under_the_access_named_when_it_began(tmp_path, monkeypatch):
    events = []

    def provider(request):  # an inert Gmail: two reads prepare the change, then the change itself
        path = request.url.path.removeprefix('/mail')
        events.append((request.method, path))
        if request.method == 'POST':
            assert path == '/messages/7b/trash' and request.headers['Authorization'] == 'Bearer fixture-host-access'
            return httpx.Response(200, json={})
        if request.url.params.get('format') == 'raw':
            return httpx.Response(200, json={'id': '7b', 'labelIds': ['INBOX'], 'raw': letter('<fixture-7b@example.test>')})
        return httpx.Response(200, json={'id': '7b', 'labelIds': ['INBOX'], 'payload': {'headers': [
            {'name': 'Message-ID', 'value': '<fixture-7b@example.test>'}]}})

    with hosted(tmp_path, monkeypatch, provider, events) as (host, accounts, _service, mail):
        client = GmailClient(accounts, 'google:one', http=mail)
        assert client.mailbox(action='trash', message_ref='gmail:7b', prepare_targets=lambda targets: None) == {}
    assert events[0] == 'list' and events.count('list') == 1  # named once, before anything was read to prepare the change
    assert ('GET', '/messages/7b') in events[1:-2]
    assert events[-2:] == [('token', 'cn-1'), ('POST', '/messages/7b/trash')]
    # The reads name no connection. The change names the one it began under with its consent, and is sent once.
    assert [body.get('connection_id') for body in host.tokens] == [None, 'cn-1'] and host.tokens[1]['consent'] == IMPORTED


def test_a_new_mail_and_a_draft_begin_under_the_access_named_at_the_start_of_the_call(tmp_path, monkeypatch):
    events = []

    def provider(request):  # an inert Gmail: the draft being replaced is read, then saved over; no mail is sent here
        path = request.url.path.removeprefix('/mail')
        events.append((request.method, path))
        if request.method == 'PUT':
            assert path == '/drafts/d1' and request.headers['Authorization'] == 'Bearer fixture-host-access'
            return httpx.Response(200, json={'id': 'd1', 'message': {'id': 'a2'}})
        if path == '/drafts/d1':
            return httpx.Response(200, json={'id': 'd1', 'message': {'id': 'a1', 'labelIds': ['DRAFT'], 'payload': {
                'headers': [{'name': 'Message-ID', 'value': '<fixture-draft@example.test>'}]}}})
        assert path == '/messages/a1' and request.url.params.get('format') == 'raw'
        return httpx.Response(200, json={'id': 'a1', 'labelIds': ['DRAFT'], 'raw': letter('<fixture-draft@example.test>')})

    with hosted(tmp_path, monkeypatch, provider, events) as (host, _accounts, service, _mail):
        mail_arguments = dict(account_id='google:one', to='reader@example.test', subject='Fixture', body='Hello',
                              request_id='one')
        queued = service.send(**mail_arguments)
        assert queued['ok'] and queued['receipt']['delivery_reporting']['gmail_connection'] == {
            'host': 'cn-1', 'consent': IMPORTED}
        lists = events.count('list')
        assert service.send(**mail_arguments)['deduplicated'] and events.count('list') == lists  # a queued request names none
        prepared = service.draft(account_id='google:one', to='reader@example.test', subject='Fixture', body='A draft',
                                 draft_id='d1')
        assert prepared['status'] == 'needs_arguments'  # the folder and the old draft's labels are named first
        events.clear()
        saved = service.draft(**prepared['retry_arguments'])
    assert saved['ok'] and saved['draft_id'] == 'd1'
    assert events[0] == 'list' and events.count('list') == 1  # named before the old draft was read
    assert events[-2:] == [('token', 'cn-1'), ('PUT', '/drafts/d1')]
    assert host.tokens[-1]['connection_id'] == 'cn-1' and host.tokens[-1]['consent'] == IMPORTED


def test_an_automatic_reply_records_its_events_access_and_goes_out_once(tmp_path, monkeypatch):
    from lib.host_adapter import automatic_reply_targets

    sent = []

    def provider(request):  # an inert recorder in place of Gmail: nothing is delivered anywhere
        assert request.method == 'POST' and request.url.path == '/mail/messages/send'
        sent.append(request.headers['Authorization'])
        return httpx.Response(200, json={'id': f'{len(sent):x}', 'threadId': 'fixture-thread'})

    class Presence:  # the Host's author takes each event and keeps its turn
        available = True

        async def submit(self, item):
            return 'continuing:fixture'

    with hosted(tmp_path, monkeypatch, provider, []) as (_host, accounts, _service, mail):
        store = EmailStore(accounts.mail_dir(accounts.get('google:one')))
        runtime = EmailRuntime(store, GmailClient(accounts, 'google:one', http=mail), Presence(), account='google:one')

        def arrive(uid):
            store.ingest({'folder': 'INBOX', 'uidvalidity': 1, 'uid': uid, 'message_id': f'<question-{uid}@example.test>',
                          'sender': 'reader@example.test', 'subject': 'Question', 'body': 'Hello.'})
            return store.inbox_event('INBOX', 1, uid)

        first = arrive(1)
        asyncio.run(runtime.process_inbox('intake'))  # the event begins: its access is recorded with its addressees
        recorded = store.get(_submitted_targets_key(first))
        assert recorded == {'reply_targets': automatic_reply_targets(first),
                            'gmail_connection': {'host': 'cn-1', 'consent': IMPORTED}}
        runtime.remember_reply_targets(first)  # processed again at the same sign-in: the saved record is left as it is
        assert store.get(_submitted_targets_key(first)) == recorded
        runtime._reply(first, ['Answer'], 'one')
        queued = store.queued_message(f'presence:{first.provider_event_key}:one')
        assert queued.reporting['gmail_connection'] == recorded['gmail_connection']
        assert (queued.body, queued.recipients) == ('Answer', ('reader@example.test',))
        runtime._reply(first, ['Answer again'], 'one')  # the first queued copy stays
        assert store.queued_message(queued.request_id) == queued
        drain(runtime)
        assert store.receipt(queued.request_id)['state'] == 'completed' and sent == ['Bearer fixture-host-access']
        # A record an earlier version wrote holds no access: it is left as it is, and its reply is queued without one.
        old = arrive(2)
        store.put(_submitted_targets_key(old), {'reply_targets': automatic_reply_targets(old)})
        runtime.remember_reply_targets(old)
        assert store.get(_submitted_targets_key(old)) == {'reply_targets': automatic_reply_targets(old)}
        runtime._reply(old, ['Answer'], 'one')
        assert 'gmail_connection' not in store.queued_message(f'presence:{old.provider_event_key}:one').reporting
        drain(runtime)  # rule A: the same mailbox sends it under its current sign-in, as before 0.11
        assert store.receipt(f'presence:{old.provider_event_key}:one')['state'] == 'completed'
        assert sent == ['Bearer fixture-host-access'] * 2


def test_one_search_asks_the_host_for_its_list_once_and_the_next_search_asks_again(tmp_path, monkeypatch):
    from test_gmail import payload

    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    lists = []

    def provider(request):  # an inert Gmail: a page of two messages, read under the saved local bearer
        assert request.method == 'GET' and request.headers['Authorization'] == 'Bearer fixture-old-access'
        path = request.url.path.removeprefix('/mail')
        if path == '/messages':
            return httpx.Response(200, json={'messages': [{'id': 'a1'}, {'id': 'a2'}]})
        return httpx.Response(200, json=payload(path.rsplit('/', 1)[-1]))

    host = Host(handover=2)

    def wire(request):
        if request.url.path == '/sign-in/accounts':
            lists.append(1)
        return host(request)

    with httpx.Client(transport=httpx.MockTransport(wire)) as wired, \
            httpx.Client(transport=httpx.MockTransport(provider)) as mail:
        store = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wired))
        mailbox(store)  # its own saved sign-in serves its reads while this host lists no sign-in of it
        client = GmailClient(store, 'google:one', http=mail)
        assert len(client.search(query='from:reader')['messages']) == 2
        assert len(lists) == 1  # the page and both of its messages: one snapshot of the host's list
        assert len(client.search(query='from:reader')['messages']) == 2
        assert len(lists) == 2  # the next search asks again
        client.read(message_ref='gmail:a1')
        assert len(lists) == 3  # and so does a lone read


def test_a_mailbox_that_may_only_be_read_still_takes_its_mail_in_with_no_access_to_send_under(tmp_path):
    from lib.host_adapter import automatic_reply_targets

    host = Host(handover=2)
    with httpx.Client(transport=httpx.MockTransport(host)) as wired, \
            httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail('nothing is read or sent here'))) as mail:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wired))
        account = mailbox(accounts, scopes=None)  # its permissions were never recorded: its saved sign-in may read, and nothing confirms a right to send
        store = EmailStore(accounts.mail_dir(account))
        store.ingest({'folder': 'INBOX', 'uidvalidity': 1, 'uid': 1, 'message_id': '<question@example.test>',
                      'sender': 'reader@example.test', 'subject': 'Question', 'body': 'Hello.'})
        event = store.inbox_event('INBOX', 1, 1)
        runtime = EmailRuntime(store, GmailClient(accounts, 'google:one', http=mail), SimpleNamespace(), account='google:one')
        assert runtime.client.intake_link() is None  # reading is confirmed; nothing confirms a right to send
        runtime.remember_reply_targets(event)
        assert store.get(_submitted_targets_key(event)) == {'reply_targets': automatic_reply_targets(event)}
