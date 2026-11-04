"""A damaged external Message-ID must not block the message after it."""
import base64
import hashlib
from contextlib import contextmanager
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pytest

from lib.accounts import AccountStore
from lib.gmail import GmailClient
from lib.mime import parse_message
from lib.runtime import EmailRuntime
from lib.service import MailService
from lib.store import EmailStore
from test_accounts import gmail_account
from test_ews_delivery_sync import _FOLDER_RESPONSE, FRESH, change, soap_client
from test_transport import FakeClient, FakeHost, FakeIMAP


GOOD = "<good@example.invalid>"


def source(mid, text):
    header = b"Message-ID: " + mid.encode() + b"\r\n" if mid is not None else b""
    return header + b"From: sender@example.invalid\r\nSubject: sample\r\n\r\n" + text.encode()


def wire_id(raw):
    return f"<sha256-{hashlib.sha256(raw).hexdigest()}@ouroboros.local>"


def drain(store):
    rows = []
    while (item := store.claim_inbox()):
        rows.append((item.message_id, item.body, Path(item.context["source_artifact"]["path"]).read_bytes()))
        store.complete_inbox(item.row_id, item.lease_token)
    return rows


@pytest.mark.parametrize("mid", ["<", "<>", "<<a@b>", "<a@[1.2.3.4", "<a@[", "<a@broken", "not an id", None])
def test_bad_identity_uses_original_bytes_without_losing_the_message(mid):
    raw = source(mid, "retained")
    message = parse_message(raw, folder="INBOX", uid=1, include_attachment_data=True)
    assert message["message_id"] == wire_id(raw)
    assert message["body"] == "retained" and message["_raw_source"] == raw


@pytest.mark.parametrize("mid", [GOOD, "<local-token>", '<"quoted user"@example.invalid>',
                                 "(source) <mail@example.invalid>", "<a@[127.0.0.1]>"])
def test_usable_identity_stays_byte_for_byte(mid):
    assert parse_message(source(mid, "body"), folder="INBOX", uid=1)["message_id"] == mid


def test_imap_bad_mail_does_not_starve_the_next_uid_or_duplicate_after_reset(tmp_path):
    # Unlike FakeIMAP's default shortcut, header search returns only the requested bytes.
    class Box(FakeIMAP):
        def uid(self, op, *args):
            result = super().uid(op, *args)
            if op == "FETCH" and "BODY.PEEK[HEADER]" in args[-1]:
                meta, raw = result[1][0]
                return "OK", [(meta, raw.partition(b"\r\n\r\n")[0] + b"\r\n\r\n"), b")"]
            return result
    import time
    box = Box(time.time() + 10)
    client, store = FakeClient(box), EmailStore(tmp_path)
    worker = EmailRuntime(store, client, FakeHost(), account="imap")
    assert worker.poll() == 0
    raws = [source("<", "one"), source("<", "two"), source(GOOD, "good")]
    box.messages = {uid: (raw,box.now) for uid,raw in enumerate(raws,1)}
    box.next_uid = 4
    assert worker.poll() == 3
    assert drain(store) == list(zip([wire_id(raws[0]), wire_id(raws[1]), GOOD], ["one","two","good"], raws))
    assert store.get(store.cursor_key("imap","INBOX"))["uid"] == 3
    box.commands.clear()
    service = MailService.__new__(MailService)
    @contextmanager
    def target(account_id):
        yield {"id":account_id,"provider":"imap","folder":"INBOX"}, store, client
    service.target=target
    found = service.search(account_id="imap:fixture")["messages"]
    assert [(m["uid"], m["message_id"]) for m in found] == [(3,GOOD),(2,""),(1,"")]
    assert all("message_ref" in m["message_id_note"] for m in found[1:]) and "message_id_note" not in found[0]
    assert all("BODY.PEEK[HEADER]" in args[-1] for op,args in box.commands if op == "FETCH")
    assert "conversation_id" not in found[1] and store.get("read_message:") is None
    read = service.read(account_id="imap:fixture", message_ref=found[1]["message_ref"])
    assert read["message_id"] == read["conversation_id"] == wire_id(raws[1])
    box.validity += 1
    assert worker.poll() == 0 and store.status()["inbox"] == {"completed":3}
    assert not any(cmd == "STORE" for cmd,_ in box.commands)


def test_gmail_damaged_metadata_raw_and_history_agree_and_advance(tmp_path):
    target = AccountStore(tmp_path)
    account = gmail_account(target)
    store = EmailStore(tmp_path/"queue")
    store.put("gmail_cursor", {"history_id":"41","activated_at":1})
    sources = {"a1":("<a@[", "bad1"), "a2":("<a@[", "bad1"), "a3":(GOOD,"good"), "a4":(None,"missing")}
    reqs = []
    def handler(request):
        assert request.method == "GET"
        path = request.url.path
        reqs.append(path)
        if path.endswith("/history"):
            return httpx.Response(200,json={"historyId":"42","history":[
                {"messagesAdded":[{"message":{"id":n}} for n in sources]}]})
        if path.endswith("/messages"):
            return httpx.Response(200,json={"messages":[{"id":n} for n in sources]})
        n=path.rsplit("/",1)[1];mid,body=sources[n]
        fields = {"id":n,"threadId":"b1","internalDate":"2000","labelIds":["INBOX","UNREAD"]}
        if request.url.params["format"]=="metadata":
            fields["payload"]={"headers":[{"name":"Message-ID","value":mid}] if mid else []}
        else:
            fields["raw"]=base64.urlsafe_b64encode(source(mid,body)).decode()
        return httpx.Response(200,json=fields)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client=GmailClient(target, account["id"], http=http)
        found=client.search()["messages"]
        assert [m["message_id"] for m in found] == ["<gmail-a1@ouroboros.local>","<gmail-a2@ouroboros.local>",GOOD,""]
        assert all("body" not in m for m in found)
        assert client.poll(store)==4 and store.get("gmail_cursor")["history_id"]=="42"
        rows=drain(store)
        for j,(n,(mid,text)) in enumerate(sources.items()):
            raw=source(mid,text);expected=found[j]["message_id"] or wire_id(raw)
            assert rows[j] == (expected,text,raw)
            assert client.read(message_ref=found[j]["message_ref"])["message_id"]==expected
        assert client.poll(store)==0 and store.status()["inbox"]=={"completed":4}


def test_exchange_sync_rejects_broken_internet_id_without_losing_items(tmp_path):
    # Same malformed provider property: first has no usable wire ID, second does,
    # third has a usable provider identity despite a broken MIME header.
    data = [("X1", "<", "<a@[", "bad"), ("X2", GOOD, "<a@[", "wire"),
            ("X3", "<>", "<native@example.invalid>", "property")]
    def item(n,mid,prop,body):
        encoded=base64.b64encode(source(mid,body)).decode()
        return (f'<m:Items><t:Message><t:MimeContent>{encoded}</t:MimeContent><t:ItemId Id="{n}"/>'
                f'<t:ParentFolderId Id="folder-id"/><t:InternetMessageId>{escape(prop)}</t:InternetMessageId>'
                f'<t:DateTimeReceived>{FRESH}</t:DateTimeReceived></t:Message></m:Items>')
    # Exchange sends newest-first. Distinct timestamps define the FIFO cut.
    changes="".join(change(row[0],f"2024-01-01T00:00:0{i+1}Z") for i,row in reversed(list(enumerate(data))))
    page=('<m:SyncState>final</m:SyncState><m:IncludesLastItemInRange>true</m:IncludesLastItemInRange>'
          f'<m:Changes>{changes}</m:Changes>')
    client,ops=soap_client([("GetFolder",_FOLDER_RESPONSE),("SyncFolderItems",page)]+[("GetItem",item(*row)) for row in data])
    store=EmailStore(tmp_path)
    assert client.poll(store)==3 and store.get("ews_sync:folder-id")["state"]=="final"
    rows=drain(store);ids=[f'<ews-{hashlib.sha256(b"X1").hexdigest()}@ouroboros.local>',GOOD,"<native@example.invalid>"]
    assert rows==[(expected,body,source(mid,body)) for expected,(_,mid,_,body) in zip(ids,data)]
    assert [op.tag.split("}")[-1] for op in ops]==["GetFolder","SyncFolderItems","GetItem","GetItem","GetItem"]


def test_binary_corruption_is_not_a_shared_lossy_identity_but_utf8_id_is_retained():
    raw = source("<x@example.invalid>", "body")
    for value in (b"<x\xff@example.invalid>", b"<x\xfe@example.invalid>", b"<x\0@example.invalid>"):
        damaged = raw.replace(b"<x@example.invalid>", value)
        assert parse_message(damaged, folder="INBOX", uid=1)["message_id"] == wire_id(damaged)
    # UTF-8 bytes in a valid RFC 6532 Message-ID are not damage.
    mid = "<тест@почта.рф>"
    assert parse_message(source(mid,"body"), folder="INBOX", uid=1)["message_id"] == mid


def test_damaged_date_keeps_source_and_rfc_parent():
    raw = (b"Date: 1 Jan 1000000000000000000 00:00:00 +0000\r\nIn-Reply-To: <p@example.invalid>\r\n"
           b"References: <r@example.invalid> <p@example.invalid>\r\n" + source("<", "still stored"))
    message=parse_message(raw,folder="INBOX",uid=1,include_attachment_data=True)
    assert message["date"].startswith("1 Jan 1000000000000000000")
    assert message["references"] == ["<r@example.invalid>", "<p@example.invalid>"]
    assert message["in_reply_to"] == "<p@example.invalid>" and message["_raw_source"] == raw


@pytest.mark.parametrize(("wire", "expected"), [("&lt;rescued@example.invalid&gt;", "<rescued@example.invalid>"),
                                                ("&lt;&gt;", "")])
def test_ews_conversion_fallback_does_not_trust_a_broken_provider_id(monkeypatch, wire, expected):
    import test_ews_structured_read as fixture
    from lib.ews import EwsClient
    original = fixture.properties
    def shape(n):
        return original(n).replace('<t:InternetMessageHeaders>', '<t:InternetMessageId>&lt;a@[</t:InternetMessageId>'
                                   '<t:InternetMessageHeaders>').replace('&lt;original@example.invalid&gt;', wire)
    monkeypatch.setattr(fixture,"properties",shape)
    result=EwsClient(fixture.ACCOUNT,session=fixture.Mailbox()).read(message_ref="ews:"+fixture.NATIVE)
    fallback = f"<ews-{hashlib.sha256(fixture.NATIVE.encode()).hexdigest()}@ouroboros.local>"
    assert result["source_format"]=="ews_properties" and result["body"]=="Содержимое Exchange."
    assert result["message_id"]==(expected or fallback)


@pytest.mark.parametrize(("previous", "old_id", "expect_new"), [
    ("same", "invalid", False), ("same", "<a@broken>", False),
    ("different", "invalid", True), ("another_folder", "invalid", True),
    ("missing_source", "invalid", True), ("tool_read_only", "invalid", True),
])
def test_upgrade_and_uidvalidity_reset_only_coalesce_a_proven_old_capture(tmp_path, previous, old_id, expect_new):
    # These are values the 0.10.2 header registry accepted. For the second,
    # it inserted a closing bracket which is NOT in the original bytes.
    raw = source("<a@broken" if old_id == "<a@broken>" else "invalid", "bad ID, normal body")
    old_wire = raw + b" (another message)" if previous == "different" else raw
    store = EmailStore(tmp_path)
    former = {"folder":"Archive" if previous == "another_folder" else "INBOX",
              "uid":1, "uidvalidity":7, "message_id":old_id, "body":"old"}
    if previous != "missing_source":
        former["_raw_source"] = old_wire
    if previous == "tool_read_only":
        store.stage_inbound_attachments(former)
        store.remember_read(former)
    else:
        assert store.ingest(former)[1]
        event = store.claim_inbox()
        store.complete_inbox(event.row_id, event.lease_token)
    key = store.cursor_key("imap", "INBOX")
    store.put(key, {"uid":1, "uidvalidity":7, "activated_at":1})
    box = FakeIMAP(1800000000.0)
    box.validity, box.next_uid = 8, 13
    box.messages = {11:(raw,box.now), 12:(source(GOOD, "later"),box.now)}
    worker = EmailRuntime(store, FakeClient(box), FakeHost(), account="imap")
    assert worker.poll() == 1 + expect_new
    assert [row[0] for row in drain(store)] == ([wire_id(raw)] if expect_new else []) + [GOOD]
    assert store.get(key)["uid"] == 12 and store.get(key)["uidvalidity"] == 8
    assert worker.poll() == 0
    # A second reset from a freshly constructed worker also remains idempotent.
    box.validity += 1
    restarted = EmailRuntime(EmailStore(tmp_path), FakeClient(box), FakeHost(), account="imap")
    assert restarted.poll() == 0 and store.get(key)["uidvalidity"] == 9
