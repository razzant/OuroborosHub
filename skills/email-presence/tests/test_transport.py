import asyncio
import imaplib
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from lib.client import MailClient
from lib.host_adapter import HostDelivery, HostTurnStatus
from lib.runtime import EmailRuntime
from lib.store import EmailStore


class FakeIMAP:
    def __init__(self, now):
        self.messages = {}
        self.validity = 7
        self.next_uid = 1
        self.now = now
        self.capabilities = (b"MOVE",)
        self.commands = []

    def add(self, uid, message_id, text="body", references="", arrived=None):
        message = EmailMessage()
        message["From"] = "sender@example.org"
        message["To"] = "bot@example.org"
        message["Subject"] = "Subject"
        message["Message-ID"] = message_id
        if references:
            message["References"] = references
        message.set_content(text)
        self.messages[uid] = (message.as_bytes(), self.now if arrived is None else arrived)
        self.next_uid = max(self.next_uid, uid + 1)

    def response(self, name):
        value = self.validity if name == "UIDVALIDITY" else self.next_uid
        return name, [str(value).encode()]

    def uid(self, command, *args):
        self.commands.append((command, args))
        if command == "SEARCH":
            # Deliberately mimic n:* returning the last UID even past the end.
            return "OK", [" ".join(str(i) for i in sorted(self.messages)).encode()]
        if command == "FETCH":
            uid = int(args[0])
            raw, arrived = self.messages[uid]
            date = datetime.fromtimestamp(arrived, timezone.utc).strftime('%d-%b-%Y %H:%M:%S +0000')
            return "OK", [(f'{uid} (UID {uid} FLAGS () INTERNALDATE "{date}" BODY[] {{{len(raw)}}}'.encode(), raw), b")"]
        return "OK", [b"done"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

    def create(self, folder):
        self.commands.append(("CREATE", folder))
        return "OK", [b"created"]


class FakeClient(MailClient):
    def __init__(self, box):
        super().__init__({"EMAIL_USER": "bot@example.org"})
        self.box = box
        self.sent = []
        self.fail_connect = False
        self.fail_data = False

    @contextmanager
    def imap(self, folder=None, readonly=True):
        yield self.box

    @contextmanager
    def smtp(self):
        if self.fail_connect:
            raise ConnectionError("not connected")
        yield self

    def send_message(self, message):
        self.sent.append(message)
        if self.fail_data:
            raise ConnectionError("SMTP acceptance is unknown")
        return {}


class FakeHost:
    available = True

    def __init__(self):
        self.submissions = []
        self.pending = False
        self.silent = False

    async def submit(self, item):
        self.submissions.append(item)
        return "receipt"

    async def status(self, reference):
        return HostTurnStatus("pending" if self.pending else "ready", texts=("Reading now",) if self.pending else ())

    async def deliver(self, reference):
        return HostDelivery(()) if self.silent else HostDelivery(("Answer",))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = [1800000000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    box = FakeIMAP(clock[0])
    client = FakeClient(box)
    store = EmailStore(tmp_path)
    host = FakeHost()
    runtime = EmailRuntime(store, client, host, account="bot@example.org")
    return clock, box, client, store, host, runtime


def test_new_only_intake_does_not_disable_historical_mail_tools(rig):
    clock, box, client, store, host, runtime = rig
    box.add(1, "<old>", arrived=clock[0] - 100)
    assert runtime.poll() == 0
    old = client.search()["messages"][0]
    assert client.read(uid=old["uid"], uidvalidity=old["uidvalidity"])["message_id"] == "<old>"
    assert not host.submissions
    clock[0] += 10
    box.add(2, "<new>", arrived=clock[0])
    assert runtime.poll() == 1
    assert runtime.poll() == 0
    assert all("BODY.PEEK[" in args[1] for op, args in box.commands if op == "FETCH")
    assert any("BODY.PEEK[HEADER]" in args[1] for op, args in box.commands if op == "FETCH")
    asyncio.run(runtime.process_inbox())
    assert [x.message_id for x in host.submissions] == ["<new>"]
    assert runtime.process_outbox()
    assert len(client.sent) == 1
    receipt = store.status()["receipts"][0]
    assert receipt["state"] == "completed"
    assert receipt["provider_message_id"] == client.sent[0]["Message-ID"]
    assert client.sent[0]["In-Reply-To"] == "<new>"
    assert not runtime.process_outbox()


def test_read_only_peeks_and_mark_read_is_the_separate_flag_write(rig):
    clock, box, client, store, host, runtime = rig
    box.add(1, "<peek>")
    sessions = []
    opened = client.imap

    @contextmanager
    def recording(folder=None, readonly=True):
        sessions.append(readonly)
        with opened(folder, readonly) as handle:
            yield handle

    client.imap = recording
    assert client.read(uid=1, uidvalidity=box.validity)["message_id"] == "<peek>"
    assert sessions == [True]
    assert [op for op, _ in box.commands] == ["FETCH"] and "BODY.PEEK[]" in box.commands[0][1][1]
    with pytest.raises(TypeError):
        client.read(uid=1, uidvalidity=box.validity, mark_as_read=True)
    assert client.mark_read(uid=1, uidvalidity=box.validity) == {"ok": True, "uid": 1, "uidvalidity": box.validity}
    assert sessions[-1] is False
    assert box.commands[-1] == ("STORE", ("1", "+FLAGS.SILENT", "(\\Seen)"))
    with pytest.raises(ValueError, match="UIDVALIDITY"):
        client.mark_read(uid=1, uidvalidity=box.validity - 1)
    assert [op for op, _ in box.commands].count("STORE") == 1


def test_uidvalidity_reset_deduplicates_and_keeps_new_arrivals(rig):
    clock, box, client, store, host, runtime = rig
    box.add(1, "<old>", arrived=clock[0] - 100)
    runtime.poll()
    clock[0] += 10
    box.add(2, "<new>", arrived=clock[0])
    runtime.poll()
    box.validity += 1
    box.messages.clear()
    box.add(1, "<new>", arrived=clock[0])
    box.add(2, "<old>", arrived=clock[0] - 100)
    box.add(3, "<later>", arrived=clock[0] + 5)
    assert runtime.poll() == 1
    assert store.status()["inbox"] == {"pending": 2}
    with pytest.raises(ValueError, match="UIDVALIDITY changed"):
        client.read(uid=1, uidvalidity=box.validity - 1)


def test_expired_inbox_lease_resumes_persisted_host_receipt(rig):
    clock, box, client, store, host, runtime = rig
    store.ingest({"uid": 2, "message_id": "<new>", "sender": "sender@example.org"})
    item = store.claim_inbox(lease_seconds=5)
    store.set_host_reference(item.row_id, item.lease_token, "saved")
    clock[0] += 6
    asyncio.run(runtime.process_inbox())
    assert host.submissions == []
    assert store.status()["inbox"] == {"completed": 1}


def test_pre_send_retry_reuses_message_id_but_ambiguous_send_is_not_retried(rig):
    clock, box, client, store, host, runtime = rig
    store.enqueue_outbox(request_id="logical", recipients=["a@example.org"], subject="x", body="y")
    original = store.receipt("logical")["provider_message_id"]
    client.fail_connect = True
    runtime.process_outbox()
    assert store.receipt("logical")["state"] == "retry"
    assert store.receipt("logical")["provider_message_id"] == original
    client.fail_connect = False
    clock[0] += 10
    client.fail_data = True
    runtime.process_outbox()
    assert store.receipt("logical")["state"] == "uncertain"
    assert client.sent[0]["Message-ID"] == original
    clock[0] += 1000
    assert not runtime.process_outbox()
    assert len(client.sent) == 1


def test_crash_while_sending_is_uncertain_but_before_send_is_recoverable(rig):
    clock, box, client, store, host, runtime = rig
    store.enqueue_outbox(request_id="before", recipients=["a@b.c"], subject="x", body="y")
    abandoned = store.claim_outbox(lease_seconds=2)
    clock[0] += 3
    resumed = store.claim_outbox(lease_seconds=2)
    assert resumed.message_id == abandoned.message_id
    assert store.mark_sending(resumed)
    clock[0] += 3
    assert store.claim_outbox() is None
    assert store.receipt("before")["state"] == "uncertain"


def test_deferred_ack_and_final_each_delivered_once_and_silence_respected(rig):
    clock, box, client, store, host, runtime = rig
    store.ingest({"uid": 2, "message_id": "<child>", "sender": "a@b.c", "references": ["<root>", "<parent>"]})
    host.pending = True
    asyncio.run(runtime.process_inbox())
    runtime.process_outbox()
    clock[0] += 6
    asyncio.run(runtime.process_inbox())
    assert not runtime.process_outbox()
    host.pending = False
    clock[0] += 6
    asyncio.run(runtime.process_inbox())
    runtime.process_outbox()
    assert len(client.sent) == 2
    assert all(m["References"] == "<root> <parent> <child>" for m in client.sent)
    assert host.submissions[0].thread_key == "<root>"
    store.ingest({"uid": 3, "message_id": "<silent>"})
    host.silent = True
    asyncio.run(runtime.process_inbox())
    assert not runtime.process_outbox()


def test_mailbox_operations_use_uid_and_never_global_expunge(rig):
    clock, box, client, store, host, runtime = rig
    box.add(1, "<old>")
    assert client.mailbox()["folders"]
    client.mailbox(action="create", destination="Archive")
    client.mailbox(action="move", uid=1, uidvalidity=7, destination="Archive")
    client.mailbox(action="flags", uid=1, uidvalidity=7, flags=["\\Seen"])
    assert any(op == "MOVE" for op, args in box.commands)
    assert not any(op == "EXPUNGE" for op, args in box.commands)


def test_move_accepts_stdlib_imaplib_string_capabilities(rig):
    _clock, box, client, _store, _host, _runtime = rig
    imap = object.__new__(imaplib.IMAP4)
    imap._encoding = "ascii"
    imap.capability = lambda: ("OK", [b"IMAP4rev1 MOVE"])
    imap._get_capabilities()
    assert imap.capabilities == ("IMAP4REV1", "MOVE")
    box.capabilities = imap.capabilities
    box.add(1, "<own-test>")
    assert client.mailbox(action="move", uid=1, uidvalidity=7, destination="Archive") == {
        "ok": True, "uid": 1, "uidvalidity": 7,
    }
    assert any(op == "MOVE" for op, _args in box.commands)


def test_move_refuses_when_server_has_no_move_capability(rig):
    _clock, box, client, _store, _host, _runtime = rig
    box.capabilities = ("IMAP4REV1", "UIDPLUS")
    box.add(1, "<own-test>")
    with pytest.raises(RuntimeError, match="does not support atomic UID MOVE"):
        client.mailbox(action="move", uid=1, uidvalidity=7, destination="Archive")
    assert not any(op == "MOVE" for op, _args in box.commands)


def test_pending_turn_blocks_only_later_messages_in_same_thread(rig):
    clock, box, client, store, host, runtime = rig
    for uid, refs in [(1, ["<root>"]), (2, ["<root>"]), (3, ["<other>"])]:
        store.ingest({"uid": uid, "message_id": f"<{uid}>", "references": refs})
    first = store.claim_inbox()
    store.retry_inbox(first.row_id, first.lease_token, "pending", delay=30)
    assert store.claim_inbox().uid == 3
    clock[0] += 31
    assert store.claim_inbox().uid == 1


def test_draft_is_saved_without_smtp(rig):
    clock, box, client, store, host, runtime = rig
    appended = []
    box.append = lambda *args: (appended.append(args) or ("OK", [b"APPENDUID 7 20"]))
    receipt = client.draft(to="a@example.org", subject="Draft", body="Not sent", folder="Drafts")
    assert receipt["ok"]
    assert appended[0][1] == "(\\Draft)"
    assert b"Not sent" in appended[0][3]
    assert not client.sent


def test_draft_without_optional_body_is_saved_without_smtp(rig):
    _clock, box, client, _store, _host, _runtime = rig
    appended = []
    box.append = lambda *args: (appended.append(args) or ("OK", [b"APPENDUID 7 20"]))
    receipt = client.draft(to="a@example.org", subject="Draft")
    assert receipt["ok"]
    assert appended[0][1] == "(\\Draft)"
    assert b"Subject: Draft" in appended[0][3]
    assert not client.sent


def test_message_arriving_in_activation_second_is_not_dropped(rig):
    clock, box, client, store, host, runtime = rig
    clock[0] += 0.75
    runtime.poll()
    box.add(1, "<same-second>", arrived=int(clock[0]))
    assert runtime.poll() == 1
    assert store.claim_inbox().message_id == "<same-second>"


def test_deferred_receipt_releases_same_thread_for_next_message(rig):
    clock, box, client, store, host, runtime = rig
    for uid in (1, 2):
        store.ingest({"uid": uid, "message_id": f"<{uid}>", "references": ["<root>"]})
    first = store.claim_inbox()
    store.set_host_reference(first.row_id, first.lease_token, "deferred-receipt")
    store.retry_inbox(first.row_id, first.lease_token, "pending", delay=30)
    assert store.claim_inbox().uid == 2


def test_temporary_smtp_recipient_refusal_retries_without_duplicate(rig):
    import smtplib
    clock, box, client, store, host, runtime = rig
    store.enqueue_outbox(request_id="temporary", recipients=["a@b.c"], subject="x", body="y")
    original = client.send_message
    def refused(message):
        raise smtplib.SMTPRecipientsRefused({"a@b.c": (450, b"Try later")})
    client.send_message = refused
    runtime.process_outbox()
    assert store.receipt("temporary")["state"] == "retry"
    assert client.sent == []
    clock[0] += 6
    client.send_message = original
    runtime.process_outbox()
    assert store.receipt("temporary")["state"] == "completed"
    assert len(client.sent) == 1
