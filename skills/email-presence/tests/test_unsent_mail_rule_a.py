"""Rule A for queued Gmail mail (the owner's decision for Mail 0.11, 27.11.2026).

A mail that has not gone out goes, as before the update, under whichever confirmed sign-in of its mailbox's own Google
account serves it. One whose sending began and whose outcome is unknown is never sent a second time: found sent in its
mailbox it is recorded sent, otherwise it is kept as one draft in that mailbox's Drafts and the owner is told once.
While the mailbox is signed out it waits, and the owner is told once why. "Send" and "delete" are the mind's tools.

Fictional mailboxes, an inert scripted host and an inert recording Gmail: nothing here reaches Google, a real host or
a real mailbox, and a "send" is only recorded.
"""
import asyncio
import base64
import importlib.util
import json
import time
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from lib.accounts import AccountStore
from lib.gmail import GmailClient
from lib.gmail_host import GmailSignIn
from lib.mime import canonical_message_id
from lib.owner_notice import OwnerNotifier, tell_owner
from lib.runtime import EmailRuntime
from lib.service import MailService
from lib.store import RECONCILE_AFTER, EmailStore
from test_gmail_transition import HOST, Host, mailbox

ADDRESS = 'one@example.test'


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def unb64(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


class Gmail:
    """An inert Gmail of one mailbox. Messages and drafts live here; every request is recorded."""

    def __init__(self):
        self.requests, self.messages, self.drafts, self.refuse_drafts = [], {}, {}, None

    def keep(self, raw, labels):
        native = format(len(self.messages) + 0xa0, 'x')
        token = canonical_message_id(message_from_bytes(raw, policy=default).get('Message-ID'))
        self.messages[native] = {'raw': raw, 'labels': list(labels), 'token': token}
        return native

    def draft(self, raw, draft_id=None):
        draft_id = draft_id or f'draft-{len(self.drafts) + 1}'
        self.drafts[draft_id] = self.keep(raw, ['DRAFT'])
        return draft_id

    def posts(self, path):
        return [json.loads(body) for method, seen, body in self.requests if method == 'POST' and seen == path]

    def __call__(self, request):
        path, query = request.url.path.removeprefix('/mail'), dict(request.url.params)
        self.requests.append((request.method, path, request.content.decode() if request.content else ''))
        wanted = query.get('q', '').removeprefix('rfc822msgid:')
        if request.method == 'GET' and path == '/messages':
            return httpx.Response(200, json={'messages': [{'id': native, 'threadId': 'fixture-thread'}
                                                          for native, row in self.messages.items() if row['token'] == wanted]})
        if request.method == 'GET' and path.startswith('/messages/'):
            native = path.rsplit('/', 1)[-1]
            return httpx.Response(200, json={'id': native, 'labelIds': self.messages[native]['labels']})
        if request.method == 'GET' and path == '/drafts':
            return httpx.Response(200, json={'drafts': [{'id': draft_id, 'message': {'id': native}}
                                                        for draft_id, native in self.drafts.items()
                                                        if self.messages[native]['token'] == wanted]})
        if request.method == 'GET' and path.startswith('/drafts/'):
            draft_id = path.rsplit('/', 1)[-1]
            if draft_id not in self.drafts:
                return httpx.Response(404, json={'error': {'errors': [{'reason': 'notFound'}]}})
            native = self.drafts[draft_id]
            return httpx.Response(200, json={'id': draft_id, 'message': {
                'id': native, 'threadId': 'fixture-thread', 'labelIds': ['DRAFT'],
                **({'raw': b64(self.messages[native]['raw'])} if query.get('format') == 'raw' else {})}})
        body = json.loads(request.content)
        if path == '/drafts':
            if self.refuse_drafts:
                return httpx.Response(self.refuse_drafts, json={'error': {'errors': [{'reason': 'invalidArgument'}]}})
            draft_id = self.draft(unb64(body['message']['raw']))
            return httpx.Response(200, json={'id': draft_id, 'message': {'id': self.drafts[draft_id]}})
        if path == '/drafts/send':
            native = self.drafts.pop(body['id'])
            self.messages[native]['labels'] = ['SENT']
            return httpx.Response(200, json={'id': native, 'threadId': 'fixture-thread', 'labelIds': ['SENT']})
        assert path == '/messages/send'
        native = self.keep(unb64(body['raw']), ['SENT'])
        return httpx.Response(200, json={'id': native, 'threadId': 'fixture-thread'})


class Notify:
    """An inert host: it answers /identity and records each /notify, answering from `answers` (200 when empty)."""

    def __init__(self, *, version=1, answers=()):
        self.identity = {'ok': True, **({'notify_version': version} if version else {})}
        self.answers, self.posted, self.asked = list(answers), [], 0

    def __call__(self, request):
        if request.url.path == '/identity':
            self.asked += 1
            return httpx.Response(200, json=self.identity)
        assert request.url.path == '/notify' and request.headers['x-skill-token'] == HOST[1]
        self.posted.append(json.loads(request.content)['text'])
        status = self.answers.pop(0) if self.answers else 200
        return httpx.Response(status, json={'ok': status == 200})


@pytest.fixture
def mail(tmp_path, monkeypatch):
    """A mailbox its own saved sign-in serves (no sign-in service), its store, a recording Gmail and a runtime."""
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    for name in ('HOST_SERVICE_URL', 'HOST_SERVICE_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    gmail = Gmail()
    with httpx.Client(transport=httpx.MockTransport(gmail)) as http:
        accounts = AccountStore(tmp_path)
        account = mailbox(accounts)
        store = EmailStore(accounts.mail_dir(account))
        monkeypatch.setattr('lib.service.account_client',
                            lambda registry, row, **_: GmailClient(registry, row['id'], http=http))
        service = MailService(SimpleNamespace(get_state_dir=lambda: str(tmp_path), get_settings=lambda keys: {}))
        runtime = EmailRuntime(store, GmailClient(accounts, 'google:one', http=http), SimpleNamespace(),
                               account='google:one')
        yield SimpleNamespace(accounts=accounts, store=store, gmail=gmail, http=http, service=service, runtime=runtime,
                              tmp_path=tmp_path)


def queue(store, request_id, *, unknown=False, **extra):
    fields = {'to': ['reader@example.test'], 'subject': 'Quarterly numbers', 'body': 'The private body ' + request_id,
              'reporting': {'account_id': 'google:one', 'sender': ADDRESS}, **extra}
    store.enqueue_outbox(request_id=request_id, **fields)
    if unknown:  # its sending began and its answer was lost long enough ago for Gmail search to show it
        with store._connect() as db:
            db.execute("UPDATE outbox SET state='uncertain',available_at=0,"
                       "last_error='Process lost during mail delivery; inspect Message-ID before retry' WHERE request_id=?",
                       (request_id,))
    return store.queued_message(request_id)


def notices(store):
    return store.status()['owner_notices']


def test_a_mail_gmail_already_holds_as_sent_is_recorded_sent_and_never_drafted(mail):
    item = queue(mail.store, 'lost', unknown=True)
    letter = EmailMessage()
    letter['Message-ID'], letter['Subject'] = item.message_id, 'Quarterly numbers'
    letter.set_content('as Gmail kept it')
    mail.gmail.keep(letter.as_bytes(), ['SENT'])
    assert mail.runtime.process_uncertain()
    receipt = mail.store.receipt('lost')
    assert receipt['state'] == 'completed' and receipt['unknown_outcome'] == 'sent'
    assert receipt['provider_receipt']['found'] == 'rfc822msgid' and 'draft_id' not in receipt
    assert mail.gmail.posts('/drafts') == [] and mail.gmail.posts('/messages/send') == []
    assert notices(mail.store) == [] and not mail.runtime.process_uncertain()


def test_a_mail_of_unknown_outcome_goes_to_drafts_once_and_the_owner_is_told_once(mail):
    attachment = {'filename': 'numbers.csv', 'content_type': 'text/csv', 'data': 'q,1\n'}
    item = queue(mail.store, 'lost', unknown=True, cc=['colleague@example.test'], bcc=['quiet@example.test'],
                 attachments=mail.store.stage_outbound_attachments('lost', [attachment]),
                 reporting={'account_id': 'google:one', 'sender': ADDRESS, 'gmail_thread_id': 'fixture-thread'})
    assert mail.runtime.process_uncertain()
    created, = mail.gmail.posts('/drafts')
    assert created['message']['threadId'] == 'fixture-thread'
    saved = message_from_bytes(unb64(created['message']['raw']), policy=default)
    assert (saved['To'], saved['Cc'], saved['Bcc']) == ('reader@example.test', 'colleague@example.test', 'quiet@example.test')
    assert saved['Message-ID'] == item.message_id and saved['From'] == ADDRESS and saved['Subject'] == 'Quarterly numbers'
    assert [part.get_filename() for part in saved.iter_attachments()] == ['numbers.csv']
    receipt = mail.store.receipt('lost')
    assert receipt['state'] == 'uncertain' and receipt['unknown_outcome'] == 'drafted' and receipt['draft_id'] == 'draft-1'
    told, = notices(mail.store)
    assert told['text'] == (f'The email "Quarterly numbers" (request_id lost) is in Drafts of {ADDRESS}: '
                            'check it and send it.')
    assert receipt['owner_notices'] == [{'kind': 'drafted', 'text': told['text'], 'state': 'pending'}]
    assert mail.gmail.posts('/messages/send') == []  # never sent a second time
    assert not mail.runtime.process_uncertain()
    restarted = EmailStore(mail.store.state_dir)  # a new companion over the same mailbox
    again = EmailRuntime(restarted, GmailClient(mail.accounts, 'google:one', http=mail.http), SimpleNamespace(),
                         account='google:one')
    assert not again.process_uncertain() and not again.process_outbox()
    assert len(mail.gmail.posts('/drafts')) == 1 and len(notices(restarted)) == 1


def test_a_draft_saved_before_its_answer_was_lost_is_found_and_kept(mail):
    item = queue(mail.store, 'lost', unknown=True)
    letter = EmailMessage()
    letter['Message-ID'], letter['Subject'] = item.message_id, 'Quarterly numbers'
    letter.set_content('saved before the companion stopped')
    kept = mail.gmail.draft(letter.as_bytes())
    assert mail.runtime.process_uncertain()
    receipt = mail.store.receipt('lost')
    assert receipt['unknown_outcome'] == 'drafted' and receipt['draft_id'] == kept
    assert receipt['delivery_reporting']['draft']['how'] == 'found' and mail.gmail.posts('/drafts') == []
    assert len(notices(mail.store)) == 1


def test_a_lost_answer_is_searched_for_only_once_gmail_search_can_show_it(mail, monkeypatch):
    queue(mail.store, 'lost')

    def lost_answer(request):  # the request left this computer and its answer never came
        raise httpx.ReadTimeout('lost', request=request)

    with httpx.Client(transport=httpx.MockTransport(lost_answer)) as broken:
        mail.runtime.client.http = broken
        assert mail.runtime.process_outbox()
    mail.runtime.client.http = mail.http
    assert mail.store.receipt('lost')['state'] == 'uncertain'
    assert not mail.runtime.process_uncertain()  # Gmail search can lag a moment behind a send
    with mail.store._connect() as db:
        due = db.execute("SELECT available_at FROM outbox WHERE request_id='lost'").fetchone()[0]
        assert due >= time.time() + RECONCILE_AFTER - 60
        db.execute("UPDATE outbox SET available_at=0 WHERE request_id='lost'")
    assert mail.runtime.process_uncertain()
    assert mail.store.receipt('lost')['unknown_outcome'] == 'drafted' and len(mail.gmail.posts('/drafts')) == 1


def test_a_draft_gmail_refuses_is_reported_once_as_unresolved(mail):
    queue(mail.store, 'lost', unknown=True)
    mail.gmail.refuse_drafts = 400
    assert mail.runtime.process_uncertain()
    receipt = mail.store.receipt('lost')
    assert receipt['unknown_outcome'] == 'unresolved' and 'draft_id' not in receipt
    told, = notices(mail.store)
    assert 'may already have gone out' in told['text'] and 'check Sent' in told['text']
    assert not mail.runtime.process_uncertain() and len(mail.gmail.posts('/drafts')) == 1


def test_a_signed_out_mailbox_keeps_its_mail_waiting_and_the_owner_is_told_once(mail):
    queue(mail.store, 'queued')
    queue(mail.store, 'lost', unknown=True)
    saved = mail.accounts.get('google:one')
    mail.accounts.disconnect('google:one')
    host = Notify()
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        notifier = OwnerNotifier(*HOST, http=wire)
        assert tell_owner(mail.accounts, notifier) == 2
        assert tell_owner(mail.accounts, notifier) == 0 and len(host.posted) == 2  # told once each
    waiting, unknown = sorted(host.posted, key=lambda text: 'may already' in text)
    assert waiting == (f'The email "Quarterly numbers" (request_id queued) waits for {ADDRESS} to be signed in to Mail '
                       'again and then goes by itself; you can also ask to send it from another mailbox or to delete it.')
    assert 'may already have gone out' in unknown and 'never sent twice' in unknown
    assert all('private body' not in text and 'reader@' not in text for text in host.posted)
    assert mail.runtime.process_uncertain()  # nothing is searched or drafted while it is signed out: asked again later
    assert mail.store.receipt('lost')['delivery_reporting']['reconcile_tries'] == 1
    with mail.store._connect() as db:
        db.execute('UPDATE outbox SET available_at=0')
    assert mail.runtime.process_outbox() and mail.store.receipt('queued')['state'] == 'held'
    assert mail.gmail.requests == []
    mail.accounts.save({**saved, 'gmail_connection': {'id': 'fixture-again'}})  # the same Google account signs in again
    with mail.store._connect() as db:
        db.execute('UPDATE outbox SET available_at=0')
    while mail.runtime.process_outbox() or mail.runtime.process_uncertain():
        pass
    assert mail.store.receipt('queued')['state'] == 'completed' and len(mail.gmail.posts('/messages/send')) == 1
    assert mail.store.receipt('lost')['unknown_outcome'] == 'drafted'


def test_a_notice_the_host_refuses_stays_in_the_status_and_goes_out_once_it_is_granted(mail):
    queue(mail.store, 'lost', unknown=True)
    assert mail.runtime.process_uncertain()
    host = Notify(answers=[403, 503])
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        notifier = OwnerNotifier(*HOST, http=wire)
        assert tell_owner(mail.accounts, notifier) == 0  # no notify_owner grant for these files yet
        told, = notices(mail.store)
        assert told['attempts'] == 1 and tell_owner(mail.accounts, notifier) == 0  # it waits before asking again
        for expected in (0, 1):  # the host answers 503 (no chat writer yet), then writes it
            with mail.store._connect() as db:
                db.execute("UPDATE runtime_state SET value=json_set(value,'$.available_at',0) WHERE key LIKE 'owner_notice:%'")
            assert tell_owner(mail.accounts, notifier) == expected
        assert notices(mail.store) == [] and tell_owner(mail.accounts, notifier) == 0
    assert len(host.posted) == 3 and len(set(host.posted)) == 1


def test_an_older_host_without_notices_keeps_them_in_the_status(mail):
    queue(mail.store, 'lost', unknown=True)
    assert mail.runtime.process_uncertain()
    host = Notify(version=None)
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        assert tell_owner(mail.accounts, OwnerNotifier(*HOST, http=wire)) == 0
    assert host.posted == [] and host.asked == 1
    told, = notices(mail.store)
    assert told['kind'] == 'drafted' and 'is in Drafts' in told['text']


def draft_raw(**headers):
    letter = EmailMessage()
    for name, value in {'From': ADDRESS, 'To': 'Reader <reader@example.test>', 'Cc': 'colleague@example.test',
                        'Bcc': 'quiet@example.test', 'Subject': 'Draft to send', 'Message-ID': '<draft-1@example.test>',
                        **headers}.items():
        letter[name] = value
    letter.set_content('Written in Gmail.')
    return letter.as_bytes()


def test_email_send_with_a_draft_id_sends_that_draft_as_it_is_after_the_host_checks_its_recipients(mail):
    mail.gmail.draft(draft_raw(), 'dr1')
    with pytest.raises(ValueError, match='omit sender'):  # the draft's own From sends it
        mail.service.send(account_id='google:one', draft_id='dr1', sender=ADDRESS)
    first = mail.service.send(account_id='google:one', draft_id='dr1', request_id='from-draft')
    assert first['status'] == 'needs_arguments' and mail.store.queued_message('from-draft') is None
    assert first['retry_arguments'] == {'account_id': 'google:one', 'draft_id': 'dr1', 'request_id': 'from-draft',
                                        'to': 'reader@example.test', 'cc': ['colleague@example.test'],
                                        'bcc': ['quiet@example.test'], 'subject': 'Draft to send',
                                        'body': 'Written in Gmail.'}  # what Safety reads, as for any mail
    other = mail.service.send(**{**first['retry_arguments'], 'body': 'Other words.'})
    assert other['code'] == 'canonical_content_required' and other['retry_arguments'] == first['retry_arguments']
    queued = mail.service.send(**first['retry_arguments'])
    assert queued['ok'] and queued['state'] == 'pending'
    item = mail.store.queued_message('from-draft')
    assert item.message_id == '<draft-1@example.test>' and item.bcc == ('quiet@example.test',)
    assert mail.runtime.process_outbox()
    receipt = mail.store.receipt('from-draft')
    assert receipt['state'] == 'completed' and mail.gmail.posts('/drafts/send') == [{'id': 'dr1'}]
    assert mail.gmail.posts('/messages/send') == [] and 'dr1' not in mail.gmail.drafts
    again = mail.service.send(**first['retry_arguments'])  # a replay names its receipt and queues nothing
    assert again['deduplicated'] and again['state'] == 'completed'


@pytest.mark.parametrize('change', ['edited', 'deleted'])
def test_a_draft_changed_or_deleted_after_its_check_is_never_sent(mail, change):
    mail.gmail.draft(draft_raw(), 'dr1')
    retry = mail.service.send(account_id='google:one', draft_id='dr1', request_id='from-draft')['retry_arguments']
    assert mail.service.send(**retry)['ok']
    if change == 'edited':  # someone edits the draft in Gmail after the Host checked its recipients
        mail.gmail.messages[mail.gmail.drafts['dr1']]['raw'] = draft_raw(To='someone-else@example.test')
    else:
        del mail.gmail.drafts['dr1']
    assert mail.runtime.process_outbox()
    receipt = mail.store.receipt('from-draft')
    assert receipt['state'] == 'failed' and mail.gmail.posts('/drafts/send') == []
    assert ('changed after it was checked' if change == 'edited' else 'no longer exists') in receipt['last_error']


def test_the_owner_can_drop_or_send_now_a_mail_that_has_not_gone_out(mail):
    queue(mail.store, 'drop')
    queue(mail.store, 'lost', unknown=True)
    queue(mail.store, 'held')
    mailbox_tool = mail.service.mailbox
    dropped = mailbox_tool(account_id='google:one', action='discard_queued', request_id='drop')
    assert dropped['ok'] and dropped['receipt']['state'] == 'cancelled'
    assert mailbox_tool(account_id='google:one', action='discard_queued', request_id='lost')['ok']
    assert not mail.runtime.process_uncertain() and mail.store.receipt('lost')['unknown_outcome'] == 'discarded'
    with mail.store._connect() as db:  # held by a Place an hour ago: asked about again only later
        db.execute("UPDATE outbox SET state='retry',last_error='Held: paused',available_at=? WHERE request_id='held'",
                   (time.time() + 3600,))
    sent_now = mailbox_tool(account_id='google:one', action='send_queued', request_id='held')
    assert sent_now['ok'] and mail.store.receipt('held')['delivery_reporting']['gmail_connection']['id']
    while mail.runtime.process_outbox():
        pass
    assert [mail.store.receipt(rid)['state'] for rid in ('drop', 'held')] == ['cancelled', 'completed']
    assert len(mail.gmail.posts('/messages/send')) == 1 and mail.gmail.posts('/drafts') == []
    assert mailbox_tool(account_id='google:one', action='discard_queued', request_id='held')['code'] == 'not_discardable'
    queue(mail.store, 'later')
    mail.accounts.disconnect('google:one')  # both still answer while the mailbox is signed out
    refused = mailbox_tool(account_id='google:one', action='send_queued', request_id='later')
    assert refused['code'] == 'mailbox_not_signed_in' and 'email_receipt' in refused['error']
    assert mailbox_tool(account_id='google:one', action='discard_queued', request_id='later')['ok']
    with pytest.raises(ValueError, match='request_id'):
        mailbox_tool(account_id='google:one', action='trash', request_id='later', message_ref='')


def test_a_replayed_send_never_requeues_a_mail_the_owner_dropped(mail):
    first = mail.service.send(account_id='google:one', to='reader@example.test', subject='Once', body='Words',
                              request_id='replayed')
    assert first['ok'] and first['state'] == 'pending'
    assert mail.service.mailbox(account_id='google:one', action='discard_queued', request_id='replayed')['ok']
    again = mail.service.send(account_id='google:one', to='reader@example.test', subject='Once', body='Words',
                              request_id='replayed')
    assert again['deduplicated'] and again['state'] == 'cancelled'
    assert not mail.runtime.process_outbox() and mail.gmail.posts('/messages/send') == []


def test_an_older_host_keeps_the_local_sign_in_sending_queued_mail_and_its_notices_in_the_status(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    gmail, host = Gmail(), Host(older=True)  # an Ouroboros without service sign-in: no sign_in_version, no notify_version
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(gmail)) as http:
        sign_in = GmailSignIn(*HOST, http=wire)
        accounts = AccountStore(tmp_path, sign_in=sign_in)
        account = mailbox(accounts)
        assert sign_in.mode() == 'legacy'
        store = EmailStore(accounts.mail_dir(account))
        queue(store, 'earlier')  # queued by 0.10.11: no record of its access
        queue(store, 'lost', unknown=True)
        runtime = EmailRuntime(store, GmailClient(accounts, 'google:one', http=http), SimpleNamespace(), account='google:one')
        while runtime.process_outbox() or runtime.process_uncertain():
            pass
        assert tell_owner(accounts, OwnerNotifier(*HOST, http=wire)) == 0
    assert store.receipt('earlier')['state'] == 'completed' and len(gmail.posts('/messages/send')) == 1
    assert store.receipt('earlier')['delivery_reporting']['gmail_connection']['id']  # its own saved sign-in
    assert store.receipt('lost')['unknown_outcome'] == 'drafted' and len(gmail.posts('/drafts')) == 1
    assert [notice['kind'] for notice in store.status()['owner_notices']] == ['drafted'] and host.tokens == []


def test_the_companion_tells_the_owner_once_about_mail_waiting_in_a_signed_out_mailbox(mail, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'scripts/email_daemon.py'
    spec = importlib.util.spec_from_file_location('mail_manager_owner_notices', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    queue(mail.store, 'queued')
    mail.accounts.disconnect('google:one')  # no worker serves it now; the companion still tells the owner
    host = Notify()
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        monkeypatch.setattr(module.OwnerNotifier, 'environment', classmethod(lambda cls: OwnerNotifier(*HOST, http=wire)))
        monkeypatch.setattr(module, '_NOTICE_SECONDS', 0.01)

        async def exercise():
            stop = asyncio.Event()
            task = asyncio.create_task(module.manage_accounts(mail.tmp_path, stop, refresh_seconds=.005))
            while not host.posted:
                await asyncio.sleep(.01)
            await asyncio.sleep(.1)  # several more passes: nothing is posted twice
            stop.set()
            await task

        asyncio.run(asyncio.wait_for(exercise(), 20))
    assert host.posted == [f'The email "Quarterly numbers" (request_id queued) waits for {ADDRESS} to be signed in to '
                           'Mail again and then goes by itself; you can also ask to send it from another mailbox or to '
                           'delete it.']
    assert mail.gmail.requests == [] and mail.store.receipt('queued')['state'] == 'pending'


def test_a_notice_fits_the_hosts_limit_and_one_the_host_refuses_as_text_is_not_posted_again(mail):
    from lib.store import drafted_notice, waiting_notice

    long_name = 'x' * 240 + '@example.test'
    assert len(drafted_notice('Q' * 900, long_name)) <= 400 and '…' in drafted_notice('Q' * 900, long_name)
    assert len(waiting_notice('Q\n' * 300, long_name, unknown=True)) <= 400
    assert drafted_notice('Two\r\nlines', ADDRESS).startswith('The email "Two lines" is in Drafts')
    queue(mail.store, 'lost', unknown=True)
    assert mail.runtime.process_uncertain()
    host = Notify(answers=[400])
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        notifier = OwnerNotifier(*HOST, http=wire)
        assert tell_owner(mail.accounts, notifier) == 0
        with mail.store._connect() as db:
            db.execute("UPDATE runtime_state SET value=json_set(value,'$.available_at',0) WHERE key LIKE 'owner_notice:%'")
        assert tell_owner(mail.accounts, notifier) == 0
    assert len(host.posted) == 1  # refused as text: posting it again changes nothing
    told, = notices(mail.store)  # and it stays in the status
    assert told['kind'] == 'drafted'
