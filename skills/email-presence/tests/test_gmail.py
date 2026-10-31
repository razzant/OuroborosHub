import base64
import json
import time
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from lib.accounts import AccountError, AccountStore
from lib.gmail import GmailClient
from lib.google_auth import begin, finish
from lib.runtime import EmailRuntime
from lib.store import EmailStore
from test_accounts import gmail_account


def payload(native="a1", *, arrived=None, labels=None):
    mail = EmailMessage()
    mail["From"] = "reader@example.invalid"
    mail["To"] = "one@example.invalid"
    mail["Subject"] = "Проверка 📬"
    mail["Message-ID"] = f"<{native}@example.invalid>"
    mail["References"] = "<root@example.invalid>"
    mail.set_content("Retained body")
    return {"id": native, "threadId": "b1", "labelIds": labels if labels is not None else ["INBOX", "UNREAD"],
            "internalDate": str(int((arrived or time.time()) * 1000)),
            "raw": base64.urlsafe_b64encode(mail.as_bytes()).decode().rstrip("="),
            "payload": {"headers": [{"name": k, "value": str(v)} for k, v in mail.items()]}}


def test_google_pkce_connect_identity_aliases_error_and_single_use_state(tmp_path):
    accounts = AccountStore(tmp_path)
    seen = []
    def handler(request):
        seen.append(request)
        if request.method == "HEAD":
            return httpx.Response(200)
        if request.url.path == "/token":
            body = parse_qs(request.content.decode())
            assert body["grant_type"] == ["authorization_code"]
            assert len(body["code_verifier"][0]) >= 43
            assert body["redirect_uri"] == ["http://127.0.0.1:8765/onboarding/static/provider-return.html"]
            return httpx.Response(200, json={"access_token": "private-access-material", "refresh_token": "private-refresh-material",
                                             "expires_in": 3600, "scope": "openid email https://www.googleapis.com/auth/gmail.modify"})
        if request.url.path.endswith("/userinfo"):
            return httpx.Response(200, json={"sub": "stable-subject", "email": "one@example.invalid", "email_verified": True})
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "one@example.invalid", "historyId": "42"})
        assert request.url.path.endswith("/settings/sendAs")
        return httpx.Response(200, json={"sendAs": [{"sendAsEmail": "alias@example.invalid", "verificationStatus": "accepted"},
                                                    {"sendAsEmail": "pending@example.invalid", "verificationStatus": "pending"}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        opened = begin(accounts, {"GOOGLE_OAUTH_CLIENT_ID": "client"}, origin="http://127.0.0.1:8765", http=http)
        query = parse_qs(urlsplit(opened["authorization_url"]).query)
        assert query["code_challenge_method"] == ["S256"]
        assert "https://mail.google.com/" not in query["scope"][0]
        state = query["state"][0]
        envelope = json.loads(base64.urlsafe_b64decode(state + "=" * (-len(state) % 4)))
        assert envelope["skill"] == "email-presence" and envelope["v"] == 1
        result = finish(accounts, state=state, code="one-time-code", http=http)
        assert result["account_id"] == "google:stable-subject"
        assert result["account"]["senders"] == ["one@example.invalid", "alias@example.invalid"]
        assert "private-access-material" not in json.dumps(result)
        with pytest.raises(AccountError, match="Unknown or expired"):
            finish(accounts, state=state, code="replayed", http=http)
        opened = begin(accounts, {"GOOGLE_OAUTH_CLIENT_ID": "client"}, origin="http://127.0.0.1:8765", http=http)
        denied_state = parse_qs(urlsplit(opened["authorization_url"]).query)["state"][0]
        calls = len(seen)
        assert finish(accounts, state=denied_state, error="access_denied", http=http)["code"] == "authorization_declined"
        assert len(seen) == calls
        assert accounts.resolve()["address"] == "one@example.invalid"


def test_refresh_cannot_restore_disconnected_account(tmp_path):
    accounts = AccountStore(tmp_path)
    account = gmail_account(accounts)
    account["tokens"]["expires_at"] = 0
    account = accounts.save(account)
    def handler(request):
        assert request.url.path == "/token"
        accounts.disconnect(account["id"])
        return httpx.Response(200, json={"access_token": "late-token", "expires_in": 3600})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = GmailClient(accounts, account["id"], http=http)
        with pytest.raises(AccountError, match="changed while"):
            client.test_connection()
    assert accounts.get(account["id"])["connected"] is False
    assert "tokens" not in accounts.get(account["id"])


def test_search_uses_metadata_pagination_and_read_peeks_native_identity(tmp_path):
    accounts = AccountStore(tmp_path)
    account = gmail_account(accounts)
    seen = []
    def handler(request):
        seen.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/messages"):
            if request.url.params.get("pageToken"):
                return httpx.Response(200, json={"messages": [{"id": "a2"}]})
            assert request.url.params["q"] == "subject:Проверка"
            return httpx.Response(200, json={"messages": [{"id": "a1"}], "nextPageToken": "next", "resultSizeEstimate": 2})
        return httpx.Response(200, json=payload(request.url.path.rsplit("/", 1)[-1]))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = GmailClient(accounts, account["id"], http=http)
        first = client.search(query="subject:Проверка")
        assert first["next_page_token"] == "next"
        assert "body" not in first["messages"][0]
        assert seen[-1].url.params["format"] == "metadata"
        second = client.search(query="subject:Проверка", page_token=first["next_page_token"])
        assert second["messages"][0]["message_ref"] == "gmail:a2"
        message = client.read(message_ref="gmail:a1", include_attachment_data=True)
        assert "Retained body" in message["body"] and message["flags"] == []
        assert message["provider_event_key"] == "gmail:a1" and message["provider_thread_id"] == "b1"
        assert seen[-1].url.params["format"] == "raw"


class BindingHost:
    def __init__(self):
        self.disconnected = False
        self.calls = []
    def binding_hold(self, binding, account, conversation, *, thread_id=""):
        self.calls.append((binding, account, conversation, thread_id))
        return "Held: place_disconnected" if self.disconnected else ""


@pytest.mark.parametrize("outcome,state", [("accepted", "completed"), ("timeout", "uncertain"), ("missing_id", "uncertain"), ("denied", "failed"), ("quota", "retry")])
def test_gmail_delivery_receipt_and_uncertain_send_are_not_replayed(tmp_path, outcome, state):
    accounts = AccountStore(tmp_path)
    account = gmail_account(accounts)
    store = EmailStore(accounts.mail_dir(account))
    rid = "send"
    binding = {"version": 1, "connector": "email-presence", "provider": "email", "account_id": account["id"],
               "conversation_id": "", "thread_id": "", "place_id": "place-one"}
    store.enqueue_outbox(request_id=rid, to=["reader@example.invalid"], bcc=["blind@example.invalid"], body="hello", subject="test",
                         reporting={"version": 1, "account_id": account["id"], "sender": account["senders"][1],
                                    "gmail_thread_id": "b1", "place_binding": binding})
    sent = []
    def handler(request):
        assert request.method == "POST" and request.url.path.endswith("/messages/send")
        body = json.loads(request.content)
        mail = BytesParser(policy=default).parsebytes(base64.urlsafe_b64decode(body["raw"] + "=" * (-len(body["raw"]) % 4)))
        sent.append(mail)
        assert mail["From"] == account["senders"][1] and mail["Bcc"] == "blind@example.invalid"
        assert body["threadId"] == "b1"
        if outcome == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
        if outcome == "denied":
            return httpx.Response(403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}})
        if outcome == "quota":
            return httpx.Response(429, headers={"Retry-After": "3600"}, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
        return httpx.Response(200, json={} if outcome == "missing_id" else {"id": "aa", "threadId": "b1"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        host = BindingHost()
        runtime = EmailRuntime(store, GmailClient(accounts, account["id"], http=http), host, account=account["id"])
        assert runtime.process_outbox()
        receipt = store.receipt(rid)
        assert receipt["state"] == state
        assert receipt["account_id"] == account["id"] and receipt["sender"] == account["senders"][1]
        assert host.calls[0][2] == ""  # Preserve the actual opaque enqueue target, do not invent Message-ID routing.
        if outcome == "accepted":
            assert receipt["provider_receipt"]["message_id"] == "aa"
            report = receipt["history_reports"][0]["payload"]
            assert report["message"]["smtp_accepted"] is False
            assert report["message"]["provider_receipt"]["message_id"] == "aa"
            assert report["account_id"] == account["id"]
        assert runtime.process_outbox() is False
        assert len(sent) == 1


def test_bound_send_remains_held_after_disconnect(tmp_path):
    accounts = AccountStore(tmp_path)
    account = gmail_account(accounts)
    store = EmailStore(accounts.mail_dir(account))
    binding = {"version": 1, "connector": "email-presence", "provider": "email", "account_id": account["id"],
               "conversation_id": "old-thread", "thread_id": "", "place_id": "removed-place"}
    store.enqueue_outbox(request_id="held", to=["reader@example.invalid"], body="held", reporting={"place_binding": binding})
    host = BindingHost()
    host.disconnected = True
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("Held mail reached Gmail send"))) as http:
        runtime = EmailRuntime(store, GmailClient(accounts, account["id"], http=http), host, account=account["id"])
        runtime.process_outbox()
    assert store.receipt("held")["state"] == "held"
    assert host.calls[0][0]["place_id"] == "removed-place"


def test_history_reset_rescans_and_native_ids_deduplicate_label_views(tmp_path):
    accounts = AccountStore(tmp_path)
    account = gmail_account(accounts)
    store = EmailStore(accounts.mail_dir(account))
    now = time.time()
    store.put("gmail_cursor", {"history_id": "old", "activated_at": now - 60})
    rounds = [0]
    def handler(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "current"})
        if request.url.path.endswith("/history"):
            rounds[0] += 1
            if rounds[0] == 1:
                return httpx.Response(404, json={"error": {}})
            return httpx.Response(200, json={"historyId": "next", "history": [{"messagesAdded": [{"message": {"id": "a1"}}]}]})
        if request.url.path.endswith("/messages"):
            assert request.url.params["q"].startswith("after:")
            return httpx.Response(200, json={"messages": [{"id": "a1"}, {"id": "a1"}, {"id": "a2"}]})
        native = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=payload(native, arrived=now if native == "a1" else now - 600))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = GmailClient(accounts, account["id"], http=http)
        assert client.poll(store) == 1
        assert store.get("poll_health")["history_reset"] is True
        assert client.poll(store) == 0
    assert store.status()["inbox"] == {"pending": 1}
    item = store.claim_inbox()
    assert item.provider_event_key == "gmail:a1" and item.thread_key == "<root@example.invalid>"
    assert item.context["provider_thread_id"] == "b1"


def test_draft_resource_can_be_discovered_updated_and_deleted(tmp_path):
    accounts = AccountStore(tmp_path)
    account = gmail_account(accounts)
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/drafts") and request.method == "GET":
            return httpx.Response(200, json={"drafts": [{"id": "draft-one", "message": {"id": "a1"}}]})
        if "/messages/" in request.url.path:
            return httpx.Response(200, json=payload())
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"id": "draft-one", "message": {"id": "a2"}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = GmailClient(accounts, account["id"], http=http)
        draft = client.search(folder="DRAFT")["messages"][0]
        assert draft["draft_id"] == "draft-one"
        changed = client.draft(to="reader@example.invalid", subject="Draft", body="changed", draft_id=draft["draft_id"])
        assert changed["draft_id"] == "draft-one" and changed["message_ref"] == "gmail:a2"
        client.mailbox(action="delete_draft", draft_id="draft-one")
    assert calls[-2:] == [("PUT", "/gmail/v1/users/me/drafts/draft-one"), ("DELETE", "/gmail/v1/users/me/drafts/draft-one")]
