"""Rule A after its review (Hub #382, 27.11.2026): a line to the owner is posted only while it is true and names the mail
it is about; "send" and "delete" reach that mail and never send it twice; a draft sent by its id is kept whole; a wait
for access or for a policy never fails a mail or sends it a second time.

Fictional mailboxes, an inert scripted host and an inert recording Gmail, as in test_unsent_mail_rule_a: nothing here
reaches Google, a real host or a real mailbox, and a "send" is only recorded.
"""
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
from lib.gmail_host import GmailSignIn, sync_gmail
from lib.host_adapter import _DRAFT_POLICY_UNREAD
from lib.owner_notice import OwnerNotifier, tell_owner
from lib.runtime import EmailRuntime
from lib.store import EmailStore, drafted_notice, waiting_notice
from test_gmail_transition import HOST, Host, mailbox
from test_unsent_mail_rule_a import ADDRESS, Gmail, Notify, draft_raw, mail, notices, queue, unb64  # noqa: F401


def due_now(store):
    """The pauses are over: every notice and every queued mail is due now."""
    with store._connect() as db:
        db.execute("UPDATE runtime_state SET value=json_set(value,'$.available_at',0) WHERE key LIKE 'owner_notice:%'")
        db.execute('UPDATE outbox SET available_at=0')


def told(accounts, host):
    """One pass of the companion's notices against `host`; how many lines the host confirmed."""
    with httpx.Client(transport=httpx.MockTransport(host)) as wire:
        return tell_owner(accounts, OwnerNotifier(*HOST, http=wire))


def sign_in_again(mail, saved):
    mail.accounts.save({**saved, 'gmail_connection': {'id': 'fixture-again'}})  # the same Google account


def second_mailbox(mail, monkeypatch, http):
    """Another Gmail mailbox of the owner, `google:two`, served by its own recording Gmail over `http`."""
    account = mailbox(mail.accounts, id='google:two', address='two@example.test', sender='two@example.test',
                      senders=['two@example.test'])
    monkeypatch.setattr('lib.service.account_client', lambda accounts, row, **_: GmailClient(
        accounts, row['id'], http=http if row['id'] == 'google:two' else mail.http))
    return account


def drain(mail, account, http):
    store = EmailStore(mail.accounts.mail_dir(account))
    try:
        runtime = EmailRuntime(store, GmailClient(mail.accounts, account['id'], http=http), SimpleNamespace(),
                               account=account['id'])
        while runtime.process_outbox():
            pass
    finally:
        store.close()


# A line says only what is still true (M2, M3).

@pytest.mark.parametrize('outcome', ['completed', 'cancelled'])
def test_a_waiting_line_ends_with_its_mail_and_is_never_posted_after_it(mail, outcome):
    """The host refuses the line at first (no notify_owner grant yet). Meanwhile the same account signs in again and the
    mail goes by itself, or the owner deletes it: the line ends in that same write and is never posted later."""
    queue(mail.store, 'queued')
    saved = mail.accounts.get('google:one')
    mail.accounts.disconnect('google:one')
    host = Notify(answers=[403])
    assert told(mail.accounts, host) == 0 and len(notices(mail.store)) == 1
    if outcome == 'completed':
        sign_in_again(mail, saved)
        assert mail.runtime.process_outbox()
    else:
        assert mail.service.mailbox(account_id='google:one', action='discard_queued', request_id='queued')['ok']
    receipt = mail.store.receipt('queued')
    assert receipt['state'] == outcome and 'owner_notices' not in receipt and notices(mail.store) == []
    due_now(mail.store)
    assert told(mail.accounts, host) == 0 and len(host.posted) == 1  # only the attempt the host refused


def test_a_waiting_line_not_yet_posted_is_dropped_once_its_mailbox_is_signed_in_again(mail):
    """An older host kept the line in the status. The same account signs in again before the mail is taken: it goes by
    itself now, so a line saying that it waits for that sign-in is never posted, even by a host that takes lines."""
    queue(mail.store, 'queued')
    saved = mail.accounts.get('google:one')
    mail.accounts.disconnect('google:one')
    assert told(mail.accounts, Notify(version=None)) == 0 and len(notices(mail.store)) == 1
    sign_in_again(mail, saved)
    host = Notify()
    assert told(mail.accounts, host) == 0 and host.posted == [] and notices(mail.store) == []
    assert mail.store.receipt('queued')['state'] == 'pending'  # it waits for its worker, not for a sign-in


def test_a_sign_in_moving_to_the_host_is_not_a_sign_out(tmp_path, monkeypatch):
    """The first pass on a host with service sign-in reserves the mailbox's saved sign-in while it moves, and the host's
    answer is lost (503). Nobody signed out: no line asks to sign in again, also while the move's request is still out.
    The owner's own Disconnect is a sign-out: then the line comes."""
    monkeypatch.setattr('lib.gmail.BASE', 'https://gmail.example.test/mail')
    host, notify = Host(), Notify()
    with httpx.Client(transport=httpx.MockTransport(host)) as wire, \
            httpx.Client(transport=httpx.MockTransport(notify)) as chat:
        accounts = AccountStore(tmp_path, sign_in=GmailSignIn(*HOST, http=wire))
        store = EmailStore(accounts.mail_dir(mailbox(accounts)))
        try:
            queue(store, 'queued')
            notifier = OwnerNotifier(*HOST, http=chat)

            def answer_lost(_body):
                assert tell_owner(accounts, notifier) == 0  # the companion's notice pass runs while the move is out
                return httpx.Response(503, json={'ok': False})

            host.answers = [answer_lost]
            sync_gmail(accounts)
            reserved = accounts.get('google:one')
            assert not reserved['connected'] and reserved['gmail_transfer']['generation'] and len(host.handovers) == 1
            assert tell_owner(accounts, notifier) == 0 and notify.posted == [] and notices(store) == []
            accounts.disconnect('google:one')
            assert tell_owner(accounts, notifier) == 1 and 'to be signed in to Mail again' in notify.posted[0]
        finally:
            store.close()


@pytest.mark.parametrize('hold', ['sending_off', 'place', 'sign_in'])
def test_a_waiting_line_says_what_else_holds_its_mail(mail, hold):
    """Only a mail that nothing but the sign-in holds goes by itself once the mailbox is signed in again: Sending off and
    a Place hold are named, the Place's own words staying in the receipt."""
    queue(mail.store, 'queued', **({'hold_reason': 'Held: Work is paused'} if hold == 'place' else {}))
    if hold == 'sending_off':
        mail.accounts.update_preferences('google:one', {'sending': 'off'})
    mail.accounts.disconnect('google:one')
    if hold == 'sign_in':  # the worker met the sign-out itself: held for that alone
        due_now(mail.store)
        assert mail.runtime.process_outbox() and mail.store.receipt('queued')['state'] == 'held'
    host = Notify()
    assert told(mail.accounts, host) == 1
    line, = host.posted
    assert ('goes by itself' in line) == (hold == 'sign_in') and ('Sending is off' in line) == (hold == 'sending_off')
    assert 'Work' not in line and 'reader@' not in line


# A mail sent is one its own mailbox shows sent (M4).

@pytest.mark.parametrize('labels', [['INBOX'], ['SPAM'], ['TRASH'], ['INBOX', 'IMPORTANT']])
def test_a_copy_that_is_not_in_sent_never_counts_as_the_mail_sent(mail, labels):
    item = queue(mail.store, 'lost', unknown=True)
    copy = EmailMessage()
    copy['Message-ID'], copy['Subject'] = item.message_id, 'Quarterly numbers'
    copy.set_content('a copy of it that came in')
    mail.gmail.keep(copy.as_bytes(), labels)
    assert mail.runtime.process_uncertain()
    receipt = mail.store.receipt('lost')
    assert receipt['state'] == 'uncertain' and receipt['unknown_outcome'] == 'drafted'
    assert len(mail.gmail.posts('/drafts')) == 1 and mail.gmail.posts('/messages/send') == []


def test_a_sent_copy_moved_to_trash_still_counts_as_sent(mail):
    item = queue(mail.store, 'lost', unknown=True)
    copy = EmailMessage()
    copy['Message-ID'], copy['Subject'] = item.message_id, 'Quarterly numbers'
    copy.set_content('as it went out')
    mail.gmail.keep(copy.as_bytes(), ['SENT', 'TRASH'])
    assert mail.runtime.process_uncertain()
    assert mail.store.receipt('lost')['unknown_outcome'] == 'sent' and mail.gmail.posts('/drafts') == []


# A draft sent by its id: checked before its send begins, under one sign-in, and kept whole (M5, M7, M9).

def queue_draft(mail, raw, draft_id='dr1', request_id='from-draft'):
    mail.gmail.draft(raw, draft_id)
    retry = mail.service.send(account_id='google:one', draft_id=draft_id, request_id=request_id)['retry_arguments']
    queued = mail.service.send(**retry)
    assert queued['ok'] and queued['state'] == 'pending'
    return retry


def test_a_draft_check_that_fails_before_the_send_leaves_the_mail_queued(mail):
    queue_draft(mail, draft_raw())

    def check_unanswered(request):
        if request.method == 'GET' and request.url.path == '/mail/drafts/dr1':
            raise httpx.ReadTimeout('the read was not answered', request=request)
        return mail.gmail(request)

    with httpx.Client(transport=httpx.MockTransport(check_unanswered)) as broken:
        mail.runtime.client.http = broken
        assert mail.runtime.process_outbox()
    mail.runtime.client.http = mail.http
    assert mail.store.receipt('from-draft')['state'] == 'retry' and mail.gmail.posts('/drafts/send') == []
    due_now(mail.store)
    assert mail.runtime.process_outbox() and mail.store.receipt('from-draft')['state'] == 'completed'
    assert len(mail.gmail.posts('/drafts/send')) == 1


def test_a_bearer_refused_while_the_draft_is_checked_is_renewed_for_its_send(mail):
    queue_draft(mail, draft_raw())
    seen = []

    def provider(request):
        if request.url.path == '/token':
            seen.append('renewed')
            return httpx.Response(200, json={'access_token': 'fixture-renewed', 'expires_in': 3600,
                                             'scope': 'https://www.googleapis.com/auth/gmail.modify'})
        if request.headers.get('authorization') != 'Bearer fixture-renewed':
            seen.append(request.method + ' refused')
            return httpx.Response(401, json={})
        seen.append(request.method)
        return mail.gmail(request)

    with httpx.Client(transport=httpx.MockTransport(provider)) as wire:
        mail.runtime.client.http = wire
        assert mail.runtime.process_outbox()
    assert seen == ['GET refused', 'renewed', 'GET', 'POST']
    assert mail.store.receipt('from-draft')['state'] == 'completed' and len(mail.gmail.posts('/drafts/send')) == 1


def minutes(*, message_id=True):
    letter = EmailMessage()
    letter['From'], letter['To'], letter['Subject'] = ADDRESS, 'review@example.test', 'Meeting notes'
    if message_id:
        letter['Message-ID'] = '<minutes@example.test>'
    letter.set_content('Here are the notes.')
    letter.add_alternative('<p>Here are the <em>notes</em>.</p>', subtype='html')
    letter.add_attachment(b'Synthetic meeting notes.', maintype='text', subtype='plain', filename='minutes.txt')
    return letter.as_bytes()


def test_a_draft_sent_by_its_id_keeps_its_html_and_files_for_another_mailbox(mail):
    queue_draft(mail, minutes(), 'minutes', 'minutes')
    mail.accounts.disconnect('google:one')  # its Gmail draft cannot be read from here now
    letter = mail.service.receipt('minutes', account_id='google:one')['letter']
    assert '<em>notes</em>' in letter['html_body'] and letter['body'].strip() == 'Here are the notes.'
    kept, = letter['attachments']
    assert kept['filename'] == 'minutes.txt' and Path(kept['path']).read_bytes() == b'Synthetic meeting notes.'


@pytest.mark.parametrize('message_id', [True, False])
def test_a_lost_draft_send_is_put_back_in_drafts_as_the_draft_was_queued(mail, message_id):
    """The answer to drafts.send is lost and Gmail named the sent copy itself, so nothing carries the Message-ID the mail
    went out with: the draft is put back exactly as it was queued (HTML, file), with that Message-ID when it had none."""
    raw = minutes(message_id=message_id)
    queue_draft(mail, raw, 'minutes', 'minutes')

    def answer_lost(request):
        reply = mail.gmail(request)
        if request.method == 'POST' and request.url.path == '/mail/drafts/send':
            mail.gmail.messages[reply.json()['id']]['token'] = 'named-by-gmail@example.test'
            raise httpx.ReadTimeout('the answer was lost', request=request)
        return reply

    with httpx.Client(transport=httpx.MockTransport(answer_lost)) as broken:
        mail.runtime.client.http = broken
        assert mail.runtime.process_outbox()
    mail.runtime.client.http = mail.http
    assert mail.store.receipt('minutes')['state'] == 'uncertain'
    due_now(mail.store)
    assert mail.runtime.process_uncertain()
    receipt = mail.store.receipt('minutes')
    assert receipt['unknown_outcome'] == 'drafted' and len(mail.gmail.drafts) == 1
    put_back, = mail.gmail.posts('/drafts')
    restored = unb64(put_back['message']['raw'])
    item = mail.store.queued_message('minutes')
    if message_id:
        assert restored == raw
    else:  # only its Message-ID line is added, so a later search finds it
        assert restored.endswith(raw) and restored[:-len(raw)].strip() == b'Message-ID: ' + item.message_id.encode()
    parsed = message_from_bytes(restored, policy=default)
    assert parsed['Message-ID'] == item.message_id
    assert '<em>notes</em>' in parsed.get_body(('html',)).get_content()
    assert [part.get_filename() for part in parsed.iter_attachments()] == ['minutes.txt']
    assert mail.gmail.posts('/messages/send') == []


# "Send" and "delete" reach the mail and never send it twice (M8, M10, M11).

def test_a_mail_moved_to_another_mailbox_leaves_the_first_queue_before_it_is_sent_there(mail, monkeypatch):
    """The mind follows Mail's advice: it takes the mail off the signed-out mailbox's queue first, then sends that same
    mail from another mailbox. The first mailbox signs in again in between: nothing of it goes from there."""
    second_gmail = Gmail()
    with httpx.Client(transport=httpx.MockTransport(second_gmail)) as second_http:
        second = second_mailbox(mail, monkeypatch, second_http)
        assert mail.service.send(account_id='google:one', request_id='report', to='reader@example.test',
                                 subject='Report', body='Fictional report')['ok']
        saved = mail.accounts.get('google:one')
        mail.accounts.disconnect('google:one')
        advice = mail.service.mailbox(account_id='google:one', action='send_queued', request_id='report')
        assert advice['code'] == 'mailbox_not_signed_in'
        assert advice['error'].index('discard_queued') < advice['error'].index('email_send')  # taken off here first
        taken = mail.service.mailbox(account_id='google:one', action='discard_queued', request_id='report')
        assert taken['ok'] and taken['receipt']['state'] == 'cancelled'
        letter = taken['receipt']['letter']
        sign_in_again(mail, saved)
        assert not mail.runtime.process_outbox()  # nothing of it is left to go from the first mailbox
        moved = mail.service.send(account_id='google:two', request_id='report',
                                  **{key: letter[key] for key in ('to', 'cc', 'bcc', 'subject', 'body')})
        assert moved['ok']
        assert mail.service.receipt('report', account_id='google:one')['letter'] == letter  # kept for another try
        drain(mail, second, second_http)
    assert (len(mail.gmail.posts('/messages/send')), len(second_gmail.posts('/messages/send'))) == (0, 1)


def test_a_mail_whose_outcome_is_unknown_is_never_offered_for_another_mailbox(mail):
    queue(mail.store, 'lost', unknown=True)
    mail.accounts.disconnect('google:one')
    send = mail.service.mailbox(account_id='google:one', action='send_queued', request_id='lost')
    assert not send['ok'] and 'may already have gone out' in send['error'] and 'email_send' not in send['error']
    drop = mail.service.mailbox(account_id='google:one', action='discard_queued', request_id='lost')
    assert drop['ok'] and drop['receipt']['unknown_outcome'] == 'discarded'
    assert 'may already have gone out' in drop['message'] and 'will not go out' not in drop['message']


def test_a_letter_with_only_blind_recipients_moves_with_them_kept_blind(mail, monkeypatch):
    invitation = EmailMessage()
    invitation['From'], invitation['Bcc'], invitation['Subject'] = ADDRESS, 'invited@example.test', 'Invitations'
    invitation.set_content('You are invited.')
    queue_draft(mail, invitation.as_bytes(), 'invitation', 'invite')
    mail.accounts.disconnect('google:one')
    letter = mail.service.mailbox(account_id='google:one', action='discard_queued', request_id='invite')['receipt']['letter']
    assert (letter['to'], letter['cc'], letter['bcc']) == ([], [], ['invited@example.test'])
    second_gmail = Gmail()
    with httpx.Client(transport=httpx.MockTransport(second_gmail)) as second_http:
        second = second_mailbox(mail, monkeypatch, second_http)
        moved = mail.service.send(account_id='google:two', request_id='invite',
                                  **{key: letter[key] for key in ('to', 'cc', 'bcc', 'subject', 'body')})
        assert moved['ok'] and moved['state'] == 'pending'
        item = EmailStore(mail.accounts.mail_dir(second)).queued_message('invite')
        assert (item.to, item.cc, item.bcc) == ((), (), ('invited@example.test',))
        drain(mail, second, second_http)
    sent, = second_gmail.posts('/messages/send')
    wire = message_from_bytes(unb64(sent['raw']), policy=default)
    assert 'invited@' not in str(wire['To'] or '') and str(wire['Bcc']) == 'invited@example.test'


def test_each_line_names_its_mail_and_email_accounts_lists_the_mail_that_waits(mail):
    """A line names its mail by request_id, which "send" and "delete" take; email_accounts lists every mail that waits,
    however many receipts came since. Recipients and body stay out of both."""
    queue(mail.store, 'older-invoice', subject='My older invoice')
    for index in range(20):
        queue(mail.store, f'other-{index}', subject=f'Item {index}')
    mail.accounts.disconnect('google:one')
    host = Notify()
    assert told(mail.accounts, host) == 21
    line, = [text for text in host.posted if 'My older invoice' in text]
    assert '(request_id older-invoice)' in line and 'reader@' not in line and 'private body' not in line
    queued = mail.service.list_accounts()['accounts'][0]['queue']
    assert 'older-invoice' not in [row['request_id'] for row in queued['receipts']]  # beyond the latest twenty
    assert {'request_id': 'older-invoice', 'subject': 'My older invoice', 'state': 'pending'} in queued['waiting']
    assert len(queued['waiting']) == 21 and 'reader@' not in str(queued['waiting'])
    dropped = mail.service.mailbox(account_id='google:one', action='discard_queued', request_id='older-invoice')
    assert dropped['ok'] and 'older-invoice' not in [row['request_id'] for row in
                                                     mail.service.list_accounts()['accounts'][0]['queue']['waiting']]


def test_a_request_id_that_cannot_be_shown_whole_stays_out_of_the_line():
    assert '(request_id ' not in drafted_notice('Notes', ADDRESS, request_id='two words')
    assert '(request_id ' not in drafted_notice('Notes', ADDRESS, request_id='r' * 390)
    shortened = waiting_notice('Q' * 900, ADDRESS, request_id='r' * 100)
    assert len(shortened) <= 400 and '(request_id ' + 'r' * 100 + ')' in shortened and '…' in shortened


def test_a_send_a_stopped_worker_left_is_of_unknown_outcome_while_its_mailbox_is_signed_out(mail):
    queue(mail.store, 'lost-while-sending')
    assert mail.store.mark_sending(mail.store.claim_outbox(lease_seconds=0))  # its worker stopped during the send
    mail.accounts.disconnect('google:one')  # no worker serves it now
    host = Notify()
    assert told(mail.accounts, host) == 1 and 'may already have gone out' in host.posted[0]
    assert mail.store.receipt('lost-while-sending')['state'] == 'uncertain'


def test_a_mail_a_stopped_worker_took_before_its_send_can_be_deleted_while_signed_out(mail):
    queue(mail.store, 'live')
    assert mail.store.claim_outbox(lease_seconds=300).request_id == 'live'  # a worker is on it right now
    queue(mail.store, 'taken')
    taken = mail.store.claim_outbox(lease_seconds=0)  # a worker that stopped before the send began
    assert taken.request_id == 'taken'
    mail.accounts.disconnect('google:one')
    dropped = mail.service.mailbox(account_id='google:one', action='discard_queued', request_id='taken')
    assert dropped['ok'] and dropped['receipt']['state'] == 'cancelled'
    assert not mail.store.mark_sending(taken)  # that worker, were it still running, can no longer begin it
    busy = mail.service.mailbox(account_id='google:one', action='discard_queued', request_id='live')
    assert busy['code'] == 'not_discardable' and mail.store.receipt('live')['state'] == 'processing'


# Rule A asks the policy of Drafts before it writes there (M12).

def drafts_policy(mail, answer):
    asked = []

    def draft_folder_hold(account_id, folder, *, thread_id=''):
        asked.append((account_id, folder, thread_id))
        return answer()

    mail.runtime.host = SimpleNamespace(draft_folder_hold=draft_folder_hold)
    return asked


def test_rule_a_asks_the_drafts_policy_before_it_saves_a_draft(mail):
    asked = drafts_policy(mail, lambda: '')
    queue(mail.store, 'lost', unknown=True)
    assert mail.runtime.process_uncertain()
    assert ('google:one', 'DRAFT', '') in asked and ('google:one', 'Drafts', '') in asked
    assert mail.store.receipt('lost')['unknown_outcome'] == 'drafted' and len(mail.gmail.posts('/drafts')) == 1


def test_a_drafts_place_that_holds_writes_keeps_the_mail_waiting_and_tells_the_owner_once(mail):
    policy = {'hold': 'Draft not saved: Email is paused'}
    drafts_policy(mail, lambda: policy['hold'])
    queue(mail.store, 'lost', unknown=True)
    for _ in range(2):  # asked again later: still held, still told once
        assert mail.runtime.process_uncertain()
        assert 'unknown_outcome' not in mail.store.receipt('lost') and mail.gmail.posts('/drafts') == []
        line, = notices(mail.store)
        assert line['kind'] == 'held' and 'may already have gone out' in line['text'] and 'paused' not in line['text']
        assert '(request_id lost)' in line['text']
        due_now(mail.store)
    policy['hold'] = ''  # the owner lets Mail write Drafts again
    assert mail.runtime.process_uncertain()
    assert mail.store.receipt('lost')['unknown_outcome'] == 'drafted' and len(mail.gmail.posts('/drafts')) == 1
    assert [line['kind'] for line in notices(mail.store)] == ['drafted']  # the held line, never posted, ended with it


def test_a_drafts_policy_that_cannot_be_read_now_is_asked_again_quietly(mail):
    drafts_policy(mail, lambda: _DRAFT_POLICY_UNREAD)
    queue(mail.store, 'lost', unknown=True)
    assert mail.runtime.process_uncertain()
    receipt = mail.store.receipt('lost')
    assert 'unknown_outcome' not in receipt and receipt['delivery_reporting']['reconcile_tries'] == 1
    assert mail.gmail.posts('/drafts') == [] and notices(mail.store) == []
    with mail.store._connect() as db:
        due = db.execute("SELECT available_at FROM outbox WHERE request_id='lost'").fetchone()[0]
    assert due > time.time()
