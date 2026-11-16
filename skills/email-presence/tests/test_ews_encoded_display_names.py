"""An encoded display name is never another address in the Exchange structured-read fallback.

Before this repair, when Exchange could not generate MIME, _structured_read decoded RFC 2047 in each whole address
header and only then split the decoded text into addresses. An encoded name such as "display@example.invalid, Actual"
became a second recipient, and an automatic reply (and Reply All) went to it. The fallback now parses the raw header
syntax with the MIME reader's header model, decoding names only inside their own mailbox, exactly as parse_message
does; Exchange's native SMTP mailboxes remain the fallback when no usable RFC header was supplied.
"""
import base64
import json
from email import message_from_bytes, policy
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from lib.accounts import AccountStore
from lib.ews import EwsClient
from lib.ews_wire import NS
from lib.mime import parse_message, reply_all_recipients
from lib.runtime import EmailRuntime
from lib.service import MailService, register_tools
from lib.store import EmailStore
import test_ews_structured_read as fixture
from test_ews_wire import CONFIG, Session, response

pytest_plugins = ['test_binding_core']

ENCODED = '=?utf-8?q?display=40example.invalid=2C_Actual?= <actual@example.invalid>'
NAME = 'display@example.invalid, Actual'
COMMA = '=?utf-8?b?0JTQvtC1LCDQmNCy0LDQvQ==?= <doe@example.invalid>'  # "Дое, Иван"
LOOKALIKE = '=?utf-8?q?Boss_=3Cboss=40example.invalid=3E?= <colleague@example.invalid>'
FROM = 'Автор &lt;sender@example.invalid&gt;'


def _xml(value):
    return value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _with_headers(monkeypatch, native_reply_to=True, **headers):
    """The shared fixture item with these raw address headers (None removes one, a value replaces or adds it).
    ``native_reply_to=False`` also removes Exchange's own ReplyTo mailbox, as for mail sent without Reply-To."""
    original = fixture.properties
    def properties(native):
        text = original(native)
        if not native_reply_to:
            start = text.index('<t:ReplyTo>')
            text = text[:start] + text[text.index('</t:ReplyTo>') + len('</t:ReplyTo>'):]
        for field, value in headers.items():
            field = field.replace('_', '-')
            row = f'<t:InternetMessageHeader HeaderName="{field}">'
            start = text.find(row)
            if start >= 0:
                end = text.index('</t:InternetMessageHeader>', start) + len('</t:InternetMessageHeader>')
                text = text[:start] + text[end:]
            if value is not None:
                text = text.replace('<t:InternetMessageHeaders>', '<t:InternetMessageHeaders>' + row + _xml(value)
                                    + '</t:InternetMessageHeader>')
        return text
    monkeypatch.setattr(fixture, 'properties', properties)


def _structured(monkeypatch, native_reply_to=True, **headers):
    _with_headers(monkeypatch, native_reply_to, **headers)
    read = EwsClient(fixture.ACCOUNT, session=fixture.Mailbox()).read(message_ref='ews:' + fixture.NATIVE)
    assert read['source_format'] == 'ews_properties'
    return read


def _mime(**headers):
    lines = ''.join(f'{field.replace("_", "-")}: {value}\r\n' for field, value in headers.items() if value is not None)
    return parse_message((lines + 'Message-ID: <original@example.invalid>\r\n\r\nBody').encode('utf-8'), folder='INBOX', uid=1)


@pytest.mark.parametrize('case', ['from_only', 'reply_to'])
def test_encoded_name_never_adds_an_address(monkeypatch, case):
    headers = {'From': ENCODED} if case == 'from_only' else {'Reply_To': ENCODED}
    read = _structured(monkeypatch, native_reply_to=False, **headers)
    mime = _mime(From=headers.get('From', 'Автор <sender@example.invalid>'), Reply_To=headers.get('Reply_To'),
                 To='owner@example.invalid, team@example.invalid', Cc='Colleague <other@example.invalid>')
    for key in ('sender', 'sender_name', 'recipients', 'cc', 'reply_to', 'address_names'):
        assert read[key] == mime[key], key
    if case == 'from_only':
        assert (read['sender'], read['sender_name'], read['reply_to']) == ('actual@example.invalid', NAME, [])
        assert reply_all_recipients(read, ['owner@example.invalid'])['to'] == ['actual@example.invalid']
    else:
        assert read['sender'] == 'sender@example.invalid' and read['sender_name'] == 'Автор'
        assert read['reply_to'] == ['actual@example.invalid']
    assert read['address_names'] == {'other@example.invalid': 'Colleague', 'actual@example.invalid': NAME,
                                     **({'sender@example.invalid': 'Автор'} if case == 'reply_to' else {})}
    # The decoded text stays available for display, as the MIME reader keeps it.
    assert NAME in read['headers']['from' if case == 'from_only' else 'reply-to'][0]


def test_visible_lists_match_the_mime_reader_and_reply_all(monkeypatch):
    headers = dict(From=ENCODED, To=f'owner@example.invalid, {COMMA}', Cc=f'{LOOKALIKE}, "Quoted, Name" <q@example.invalid>')
    read = _structured(monkeypatch, native_reply_to=False, **headers)
    mime = _mime(**headers)
    for key in ('sender', 'sender_name', 'recipients', 'cc', 'reply_to', 'address_names'):
        assert read[key] == mime[key], key
    assert read['recipients'] == ['owner@example.invalid', 'doe@example.invalid']
    assert read['cc'] == ['colleague@example.invalid', 'q@example.invalid']
    assert read['address_names'] == {'colleague@example.invalid': 'Boss <boss@example.invalid>', 'q@example.invalid': 'Quoted, Name',
                                     'doe@example.invalid': 'Дое, Иван', 'actual@example.invalid': NAME}
    everyone = reply_all_recipients(read, ['owner@example.invalid'])
    assert everyone == {'to': ['actual@example.invalid'],
                        'cc': ['doe@example.invalid', 'colleague@example.invalid', 'q@example.invalid']}


@pytest.mark.parametrize('damaged', ['garbage <<<', 'broken@', 'undisclosed-recipients:;'])
def test_unusable_rfc_header_falls_back_to_native_smtp_mailboxes(monkeypatch, damaged):
    read = _structured(monkeypatch, To=damaged, Reply_To=damaged)
    # The fixture's native ToRecipients: one SMTP mailbox and one Exchange legacy DN, which is not an address.
    assert read['recipients'] == ['owner@example.invalid'] and read['reply_to'] == ['replies@example.invalid']
    assert read['sender'] == 'sender@example.invalid'


def test_absent_rfc_from_uses_native_smtp_sender_only(monkeypatch):
    read = _structured(monkeypatch, From=None)
    # The fixture's native From is an Exchange legacy DN only: no address is invented from it.
    assert read['sender'] == '' and read['recipients'] == ['owner@example.invalid', 'team@example.invalid']


@pytest.mark.parametrize('case', ['from_only', 'reply_to'])
def test_automatic_reply_through_real_host_binding_reaches_only_the_actual_address(tmp_path, monkeypatch, core_host, case):
    _with_headers(monkeypatch, False, **({'From': ENCODED} if case == 'from_only' else {'Reply_To': ENCODED}))

    class Mailbox(fixture.Mailbox):
        emitted = []

        def post(self, *args, **kwargs):
            op = ET.fromstring(kwargs['data']).find('s:Body', NS)[0]
            if op.tag.split('}')[-1] == 'CreateItem':
                self.emitted.append(op)
                self.responses.append(response('CreateItem'))
                return Session.post(self, *args, **kwargs)
            return super().post(*args, **kwargs)

    session = Mailbox()
    account = fixture.ACCOUNT
    client = EwsClient(account, session=session)
    read = client.read(message_ref='ews:' + fixture.NATIVE)
    store = EmailStore(tmp_path / 'mail')
    store.ingest({**read, 'uid': None})
    captured = store.claim_inbox()
    places, drive, adapter = core_host
    places.connect_place(drive, connector='email-presence', provider='email', account_id=account['id'], state='active')
    runtime = EmailRuntime(store, client, adapter, account=account['id'], account_loader=lambda: account)
    runtime.remember_reply_targets(captured)  # the event submitted to the Host carries these targets
    runtime._reply(captured, ['Private answer'], 'final')
    request_id = 'presence:' + captured.provider_event_key + ':final'
    assert store.receipt(request_id)['delivery_reporting']['place_binding']['place_id']
    assert list(store.queued_message(request_id).envelope_recipients) == ['actual@example.invalid']
    assert runtime.process_outbox() and store.receipt(request_id)['state'] == 'completed'
    assert len(session.emitted) == 1
    created = session.emitted[0]
    native = [node.text for node in created.findall('.//t:ToRecipients/t:Mailbox/t:EmailAddress', NS)]
    assert native == ['actual@example.invalid'] and created.find('.//t:CcRecipients', NS) is None
    wire = message_from_bytes(base64.b64decode(created.findtext('.//t:MimeContent', '', NS)), policy=policy.default)
    assert [a.addr_spec for a in wire['To'].addresses] == ['actual@example.invalid'] and wire['Cc'] is None
    assert runtime.process_outbox() is False


def test_reply_all_tool_keeps_encoded_names_as_names(tmp_path, monkeypatch):
    _with_headers(monkeypatch, False, From=ENCODED, To=f'{fixture.ACCOUNT["address"]}, {COMMA}', Cc=LOOKALIKE)
    accounts = AccountStore(tmp_path)
    account = accounts.save_ews({'address': fixture.ACCOUNT['address'], 'password': 'fixture', 'endpoint': CONFIG['EWS_ENDPOINT']})
    provider = fixture.Mailbox()
    monkeypatch.setattr('lib.service.account_client', lambda registry, account, **kw: EwsClient(account, session=provider))
    service = MailService(SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda _: {}))
    handlers = {}
    register_tools(SimpleNamespace(register_tool=lambda name, handler, **_: handlers.update({name: handler})), service)
    call = lambda tool, args: json.loads(handlers[tool](**args))
    read = call('email_read', dict(account_id=account['id'], message_ref=service.wrap_ref(account, {'message_ref': 'ews:' + fixture.NATIVE})))
    assert read['sender'] == 'actual@example.invalid' and read['cc'] == ['colleague@example.invalid']
    args = dict(account_id=account['id'], to='', subject='Re: Проверка', body='Answer', reply_all=True,
                reply_to_message_id=read['message_id'], request_id='reply-all')
    offer = call('email_send', args)
    assert offer['status'] == 'needs_arguments'
    retry = offer['retry_arguments']
    assert (retry['to'], retry['cc']) == ('actual@example.invalid', ['doe@example.invalid', 'colleague@example.invalid'])
    assert retry['display_names'] == {'actual@example.invalid': NAME, 'doe@example.invalid': 'Дое, Иван',
                                      'colleague@example.invalid': 'Boss <boss@example.invalid>'}
    done = call('email_send', retry)
    assert done['ok']
    queued = store_for(tmp_path, accounts, account).queued_message('reply-all')
    assert list(queued.envelope_recipients) == ['actual@example.invalid', 'doe@example.invalid', 'colleague@example.invalid']
    assert queued.to == (f'"{NAME}" <actual@example.invalid>',)


def store_for(tmp_path, accounts, account):
    return EmailStore(accounts.mail_dir(account))
