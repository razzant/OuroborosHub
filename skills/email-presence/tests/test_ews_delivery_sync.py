"""Real Mail queues around synthetic EWS responses. No provider connections."""
import base64
from email import message_from_bytes
from email.policy import default
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from lib.client import MailClient
from lib.ews_operations import EwsOperations
from lib.ews_wire import EwsError
from lib.host_adapter import email_presence_event
from lib.runtime import EmailRuntime
from lib.store import EmailStore

M = "http://schemas.microsoft.com/exchange/services/2006/messages"
T = "http://schemas.microsoft.com/exchange/services/2006/types"
N = {"m": M, "t": T}
FRESH = "2024-01-01T00:00:03Z"
OLD = "2020-01-01T00:00:00Z"


def xml(content):
    return ET.fromstring(f'<m:Response xmlns:m="{M}" xmlns:t="{T}">{content}</m:Response>')


def change(native_id, stamp, *, kind="Create", item="Message"):
    return (f'<t:{kind}><t:{item}><t:ItemId Id="{native_id}"/>'
            + (f'<t:DateTimeReceived>{stamp}</t:DateTimeReceived>' if stamp else '')
            + f'</t:{item}></t:{kind}>')


def page(changes="", state="continued", last="true"):
    return xml(f'<m:SyncState>{state}</m:SyncState><m:IncludesLastItemInRange>{last}</m:IncludesLastItemInRange>'
               f'<m:Changes>{changes}</m:Changes>')


class Exchange(EwsOperations, MailClient):
    transport = "ews"

    def __init__(self, *responses):
        super().__init__({"EMAIL_USER": "login", "EMAIL_DEFAULT_SENDER": "owner@example.invalid"})
        self.account = {"address": "owner@example.invalid"}
        self.responses = list(responses)
        self.ops, self.reads, self.connection_checks = [], [], 0
        self.read_errors = {}

    def _folder(self, name):
        return ET.Element(f'{{{T}}}DistinguishedFolderId', {"Id": name.lower()})

    def test_connection(self):
        self.connection_checks += 1
        return {"ok": True, "server_time": 1704067200}

    def _folder_info(self, folder):
        value = folder.removeprefix("ews-folder:").lower()
        return {**self.test_connection(), "id": value}

    @staticmethod
    def _folder_ref(identifier):
        return "ews-folder:" + identifier

    def _call(self, op):
        self.ops.append(op)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def read(self, *, message_ref, include_attachment_data):
        assert include_attachment_data
        self.reads.append(message_ref)
        if message_ref in self.read_errors:
            raise self.read_errors[message_ref]
        return {"provider_event_key": message_ref, "message_ref": message_ref,
                "message_id": "<" + message_ref[4:] + "@example.invalid>", "sender": "peer@example.invalid",
                "body": "fixture mail", "subject": "fixture subject"}


def queue(tmp_path, response):
    store = EmailStore(tmp_path)
    attachments = store.stage_outbound_attachments("one", [{"filename": "demo.txt", "content": "attachment"}])
    store.enqueue_outbox(request_id="one", to=['"Doe, John" <to@example.invalid>'],
                         cc=["visible@example.invalid"], bcc=['"Blind, Guest" <blind@example.invalid>'],
                         subject="Re: test", body="plain fixture", html_body="<p>HTML</p>",
                         in_reply_to="<parent@example.invalid>", references=["<root@example.invalid>"],
                         attachments=attachments, reporting={"version": 1, "account_id": "ews:fixture"})
    client = Exchange(response)
    runtime = EmailRuntime(store, client, SimpleNamespace(), account="ews:fixture")
    return runtime, client, store


def test_idless_send_acceptance_preserves_envelope_thread_mime_and_receipt(tmp_path):
    runtime, client, store = queue(tmp_path, xml("<m:Items/>"))
    assert runtime.process_outbox() and not runtime.process_outbox()
    op, = client.ops
    assert client.connection_checks == 1
    assert op.tag == f'{{{M}}}CreateItem' and op.get("MessageDisposition") == "SendAndSaveCopy"
    assert op.find("m:SavedItemFolderId/t:DistinguishedFolderId", N).get("Id") == "sentitems"
    item = op.find("m:Items/t:Message", N)
    assert item.findtext("t:ToRecipients/t:Mailbox/t:Name", namespaces=N) == "Doe, John"
    assert item.findtext("t:CcRecipients/t:Mailbox/t:EmailAddress", namespaces=N) == "visible@example.invalid"
    assert item.findtext("t:BccRecipients/t:Mailbox/t:EmailAddress", namespaces=N) == "blind@example.invalid"
    msg = message_from_bytes(base64.b64decode(item.findtext("t:MimeContent", namespaces=N)), policy=default)
    assert msg["Bcc"] is None and msg["In-Reply-To"] == "<parent@example.invalid>"
    assert msg["References"] == "<root@example.invalid> <parent@example.invalid>"
    assert msg.get_body(("html",)).get_content().strip() == "<p>HTML</p>"
    assert next(msg.iter_attachments()).get_payload(decode=True) == b"attachment"
    receipt = store.receipt("one")
    assert receipt["state"] == "completed"
    assert receipt["provider_receipt"] == {"transport": "ews", "internet_message_id": str(msg["Message-ID"])}
    assert receipt["history_reports"][0]["payload"]["message"]["smtp_accepted"] is False


@pytest.mark.parametrize(("error", "state"), [
    (ConnectionResetError(), "uncertain"),
    (EwsError("ErrorTimeoutExpired", http_status=200, proven=True), "uncertain"),
    (EwsError("ErrorInternalServerError", http_status=500, proven=True), "uncertain"),
    (EwsError("ErrorSendAsDenied", http_status=200, proven=False), "uncertain"),
    (EwsError("ErrorSendAsDenied", http_status=200, proven=True), "failed"),
    (EwsError("ErrorServerBusy", http_status=500, retry_after=45, proven=True), "retry"),
    (EwsError("HTTP401", http_status=401), "held"),
])
def test_proven_rejection_and_unknown_acceptance_have_different_custody(tmp_path, error, state):
    runtime, client, store = queue(tmp_path, error)
    assert runtime.process_outbox() and store.receipt("one")["state"] == state
    assert not runtime.process_outbox() and len(client.ops) == 1


def test_sending_off_never_authenticates_or_submits(tmp_path):
    runtime, client, store = queue(tmp_path, xml("<m:Items/>"))
    runtime.account_loader = lambda: {"sending": "off"}
    assert runtime.process_outbox()
    assert store.receipt("one")["state"] == "held" and not client.ops and not client.connection_checks


def test_draft_is_save_only_and_returns_server_ref_without_sending():
    client = Exchange(xml('<m:Items><t:Message><t:ItemId Id="Case/+="/></t:Message></m:Items>'))
    result = client.draft(to="reader@example.invalid", bcc=['"Hidden, Reader" <private@example.invalid>'],
                          subject="draft", body="text")
    assert result["message_ref"] == "ews:Case/+="
    op, = client.ops
    assert op.get("MessageDisposition") == "SaveOnly"
    assert op.find("m:SavedItemFolderId/t:DistinguishedFolderId", N).get("Id") == "drafts"
    assert op.findtext(".//t:BccRecipients/t:Mailbox/t:EmailAddress", namespaces=N) == "private@example.invalid"
    with pytest.raises(ValueError, match="updating"):
        client.draft(to="reader@example.invalid", subject="edit", draft_id="existing")
    assert len(client.ops) == 1


def test_staged_bootstrap_keeps_new_arrivals_and_folder_specific_cursor(tmp_path):
    store = EmailStore(tmp_path)
    client = Exchange(page(change("historical", OLD), state="s1", last="false"))
    assert client.poll(store) == 0 and not client.reads
    # A process restart between baseline pages cannot move the activation boundary.
    client = Exchange(page(change("historical-two", OLD) + change("Aa/+=", FRESH) +
                           change("ignored", FRESH, kind="Update"), state="s2"))
    assert client.poll(EmailStore(tmp_path), folder="ews-folder:inbox") == 1 and client.connection_checks == 1
    assert client.reads == ["ews:Aa/+="]
    assert client.ops[0].findtext("m:SyncState", namespaces=N) == "s1"
    first = store.claim_inbox()
    assert first and first.provider_event_key == "ews:Aa/+=" and first.uidvalidity == -1
    event = email_presence_event(first, account_id="ews:fixture")
    assert event["source_event_id"] == "ews:Aa/+=" and event["message"]["imap_uid"] == first.uid
    client.responses = [page(change("aa/+=", None), state="s3"), page(state="another-folder")]
    assert client.poll(store) == 1
    assert client.poll(store, folder="Archive") == 0
    assert client.connection_checks == 3
    assert client.ops[-1].find("m:SyncState", N) is None
    assert client.ops[-2].findtext("m:SyncState", namespaces=N) == "s2"
    second = store.claim_inbox()
    assert second and second.uid != first.uid and second.provider_event_key == "ews:aa/+="


def test_failed_second_capture_replays_prior_page_and_deduplicates_first_item(tmp_path):
    store = EmailStore(tmp_path)
    both = page(change("first", None) + change("second", None), state="next")
    client = Exchange(page(state="baseline"), both, both)
    assert client.poll(store) == 0
    client.read_errors["ews:second"] = ConnectionError("temporary")
    with pytest.raises(ConnectionError):
        client.poll(store)
    assert store.status()["inbox"] == {"pending": 1}
    client.read_errors.clear()
    assert client.poll(EmailStore(tmp_path)) == 1
    assert [op.findtext("m:SyncState", namespaces=N) for op in client.ops] == [None, "baseline", "baseline"]
    assert store.status()["inbox"] == {"pending": 2}


def test_invalid_state_rescans_since_original_activation_and_records_reset(tmp_path):
    store = EmailStore(tmp_path)
    client = Exchange(page(change("one", FRESH), state="stale"),
                      EwsError("ErrorInvalidSyncStateData", http_status=200, proven=True),
                      page(change("old", OLD) + change("one", FRESH) + change("new", FRESH), state="recovered"))
    assert client.poll(store) == 1
    assert client.poll(store) == 1
    assert [op.findtext("m:SyncState", namespaces=N) for op in client.ops] == [None, "stale", None]
    assert store.get("poll_health")["last_sync_reset_at"] > 0
    assert store.status()["inbox"] == {"pending": 2} and "ews:old" not in client.reads


def test_malformed_page_never_advances_or_ingests(tmp_path):
    store = EmailStore(tmp_path)
    client = Exchange(page(state="baseline"), xml("<m:SyncState>bad</m:SyncState>"),
                      page(change("good", FRESH), state="good"))
    assert client.poll(store) == 0
    with pytest.raises(RuntimeError, match="cursor retained"):
        client.poll(store)
    assert client.poll(store) == 1
    assert client.ops[-1].findtext("m:SyncState", namespaces=N) == "baseline"


def test_native_identity_dedup_and_legacy_rfc_policy(tmp_path):
    store = EmailStore(tmp_path)
    def native(key, rfc):
        return {"provider_event_key": key, "message_ref": key, "message_id": rfc, "folder": "INBOX"}
    assert store.ingest(native("ews:ItemId-A", "<rfc-a>"))[1]
    assert not store.ingest(native("ews:ItemId-A", "<changed-rfc>"))[1]
    assert not store.ingest(native("ews:ItemId-B", "<rfc-a>"))[1]
    assert store.ingest(native("ews:ItemId-b", "<rfc-b>"))[1]
    assert store.ingest({"uid": 3, "uidvalidity": 20, "message_id": "<legacy>"})[1]
    assert not store.ingest({"uid": 4, "uidvalidity": 20, "message_id": "<legacy>"})[1]
    assert store.inbox_event("INBOX", -1, 1).provider_event_key == "ews:ItemId-A"
    assert store.inbox_event("INBOX", -1, 2).provider_event_key == "ews:ItemId-b"


def soap_client(responses):
    from requests import Response
    from lib.ews import EwsClient
    calls = []
    class Session:
        def post(self, url, *, data, **kwargs):
            assert url == "https://exchange.example.invalid/EWS/Exchange.asmx"
            ns = {"s": "http://schemas.xmlsoap.org/soap/envelope/", **N}
            op = ET.fromstring(data).find("s:Body", ns)[0]
            action, content = responses.pop(0)
            assert action == op.tag.partition("}")[2]
            calls.append(op)
            reply = Response()
            reply.status_code = 200
            reply.headers["Date"] = "Mon, 01 Jan 2024 00:00:00 GMT"
            reply._content_consumed = True
            reply._content = (f'<s:Envelope xmlns:s="{ns["s"]}" xmlns:m="{M}" xmlns:t="{T}"><s:Body>'
                              f'<m:{action}Response><m:ResponseMessages><m:{action}ResponseMessage ResponseClass="Success">'
                              f'<m:ResponseCode>NoError</m:ResponseCode>{content}</m:{action}ResponseMessage>'
                              f'</m:ResponseMessages></m:{action}Response></s:Body></s:Envelope>').encode()
            return reply

    record = {"address": "office@example.invalid", "id": "ews:fixture", "provider": "ews",
              "config": {"EWS_ENDPOINT": "https://exchange.example.invalid/EWS/Exchange.asmx",
                         "EMAIL_USER": "other-login", "EMAIL_PASSWORD": "fixture-only"}}
    return EwsClient(record, session=Session()), calls


_FOLDER_RESPONSE = '<m:Folders><t:Folder><t:FolderId Id="folder-id"/></t:Folder></m:Folders>'


def test_actual_ews_client_accepts_empty_create_result_and_preserves_delegated_mailbox(tmp_path):
    runtime, _, store = queue(tmp_path, None)
    runtime.client, calls = soap_client([("GetFolder", _FOLDER_RESPONSE), ("CreateItem", "<m:Items/>")])
    assert runtime.process_outbox() and not runtime.process_outbox()
    assert [op.tag for op in calls] == [f"{{{M}}}GetFolder", f"{{{M}}}CreateItem"]
    for op in calls:
        assert op.findtext(".//t:DistinguishedFolderId/t:Mailbox/t:EmailAddress", namespaces=N) == "office@example.invalid"
    assert calls[1].get("MessageDisposition") == "SendAndSaveCopy"
    assert store.receipt("one")["state"] == "completed"


def test_actual_sync_aliases_share_folder_identity_and_keep_mail_from_bootstrap(tmp_path):
    sync = ('<m:SyncState>first</m:SyncState><m:IncludesLastItemInRange>false</m:IncludesLastItemInRange>'
            f'<m:Changes>{change("old", OLD)}{change("Ab/+", "2024-01-01T00:00:03.0000001Z")}</m:Changes>')
    mime = base64.b64encode(b"From: a@example.invalid\r\nMessage-ID: <fresh@example.invalid>\r\n\r\nfresh").decode()
    get = (f'<m:Items><t:Message><t:MimeContent>{mime}</t:MimeContent><t:ItemId Id="Ab/+"/><t:ParentFolderId Id="folder-id"/>'
           '<t:DateTimeReceived>2024-01-01T00:00:03.0000001Z</t:DateTimeReceived></t:Message></m:Items>')
    client, initial = soap_client([("GetFolder", _FOLDER_RESPONSE), ("SyncFolderItems", sync), ("GetItem", get)])
    store = EmailStore(tmp_path)
    assert client.poll(store, folder="INBOX") == 1
    assert initial[1].find("m:SyncFolderId/t:FolderId", N).get("Id") == "folder-id"
    event = store.claim_inbox()
    assert event.message_id == "<fresh@example.invalid>" and event.provider_event_key == "ews:Ab/+"
    end = '<m:SyncState>last</m:SyncState><m:IncludesLastItemInRange>true</m:IncludesLastItemInRange><m:Changes/>'
    restarted, final = soap_client([("GetFolder", _FOLDER_RESPONSE), ("SyncFolderItems", end)])
    assert restarted.poll(EmailStore(tmp_path), folder=event.folder) == 0
    assert final[-1].findtext("m:SyncState", namespaces=N) == "first"
    assert not store.get("poll_health")["more_changes"]
