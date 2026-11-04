"""The permanent-delete tool guards the selected message's current labels in the Host."""
import json
from types import SimpleNamespace

import httpx
import pytest

from lib.accounts import AccountError
from lib.gmail import GmailClient, GmailError
from lib.service import MailService, register_tools
from test_accounts import gmail_account
from test_gmail import payload
from test_write_targets import host_guard, published_targets


@pytest.fixture
def mailbox(tmp_path, monkeypatch):
    svc = MailService(SimpleNamespace(get_state_dir=lambda: tmp_path / "skill", get_settings=lambda _: {}))
    accounts = svc.accounts
    account = gmail_account(accounts)
    account["scopes"].append("https://mail.google.com/")
    accounts.save(account)
    data = SimpleNamespace(message={"id": "7b", "labelIds": ["DRAFT"]}, label_name="Receipts",
                           status=200, requests=[], writes=[])
    def exchange(request):
        assert request.url.host == "gmail.googleapis.com"
        path = request.url.path.removeprefix("/gmail/v1/users/me")
        data.requests.append((request.method, path))
        if request.method == "DELETE":
            assert path == "/messages/7b" and not request.content
            data.writes.append(path)
            return httpx.Response(204)
        assert request.method == "GET"
        if path == "/drafts":
            return httpx.Response(200, json={"drafts": [{"id": "stable-draft", "message": {"id": "7b"}}]})
        if path == "/labels":
            return httpx.Response(200, json={"labels": [{"id": "Label_1", "name": data.label_name}]})
        assert path == "/messages/7b"
        if request.url.params.get("format") == "metadata":  # registered draft search
            return httpx.Response(200, json=payload("7b", labels=["DRAFT"]))
        assert dict(request.url.params) == {"format": "minimal", "fields": "id,labelIds"}
        return httpx.Response(data.status, json=data.message)
    with httpx.Client(transport=httpx.MockTransport(exchange)) as http:
        monkeypatch.setattr("lib.service.account_client", lambda store, row, **_: GmailClient(store, row["id"], http=http))
        handlers = {}
        register_tools(SimpleNamespace(register_tool=lambda name, handler, **_: handlers.update({name: handler})), svc)
        call = lambda name, args: json.loads(handlers[name](**args))
        data.delete = lambda args: call("email_mailbox", args)
        data.search = lambda: call("email_search", {"account_id": account["id"], "folder": "DRAFT"})["messages"][0]
        data.accounts, data.account = accounts, account
        data.args = {"account_id": account["id"], "action": "delete",
                     "message_ref": svc.wrap_ref(account, {"message_ref": "gmail:7b"})}
        data.guard = lambda scopes: host_guard(tmp_path, monkeypatch, "email_mailbox", account["id"], scopes)
        yield data


@pytest.mark.parametrize("label,state", [("DRAFT", "paused"), ("Drafts", "silent"), ("Drafts", "active")])
def test_generic_delete_through_search_ref_respects_draft_place(mailbox, label, state):
    d = mailbox
    draft = d.search()
    assert draft["draft_id"] == "stable-draft" and draft["message_ref"] == d.args["message_ref"]
    args = {**d.args, "message_ref": draft["message_ref"]}
    guard = d.guard([("", "active"), (label, state)])
    assert not guard(args)
    retry = d.delete(args)["retry_arguments"]
    assert not d.writes and retry["message_ref"] == args["message_ref"]
    assert retry["remove_labels"] == ["DRAFT"] and retry["label_names"] == ["Drafts"]
    if state == "active":
        assert not guard(retry)
        d.delete(retry)
        assert d.writes == ["/messages/7b"]
    else:
        assert state.title() in guard(retry) and not d.writes


@pytest.mark.parametrize("held", ["Label_1", "Receipts", "INBOX", None])
def test_actual_message_labels_replace_decoys_before_permanent_delete(mailbox, held):
    d = mailbox
    d.message["labelIds"] = ["Label_1", "INBOX"]
    guard = d.guard([("", "active"), (held, "paused")] if held else [])
    args = {**d.args, "label_names": ["Decoy"], "add_labels": ["Decoy"], "remove_labels": ["Decoy"],
            "destination": "Decoy", "label_id": "Decoy"}
    assert not guard(args)
    retry = d.delete(args)["retry_arguments"]
    assert not d.writes and retry["message_ref"] == d.args["message_ref"]
    assert published_targets("email_mailbox", retry) == {"account_id": [d.account["id"]],
           "remove_labels": ["INBOX", "Label_1"], "label_names": ["Receipts"]}
    if held:
        assert "Paused" in guard(retry) and not d.writes
    else:
        assert not guard(retry)
        d.delete(retry)
        assert d.writes == ["/messages/7b"]


@pytest.mark.parametrize("change", ["applied_label", "renamed_label"])
def test_retry_checks_message_membership_and_current_names_again(mailbox, change):
    d = mailbox
    d.message["labelIds"] = ["Label_1"]
    guard = d.guard([("", "active"), ("INBOX" if change == "applied_label" else "New name", "paused")])
    assert not guard(d.args)
    first = d.delete(d.args)["retry_arguments"]
    assert not guard(first)
    if change == "applied_label":
        d.message["labelIds"].append("INBOX")
    else:
        d.label_name = "New name"
    current = d.delete(first)["retry_arguments"]
    assert "Paused" in guard(current) and current["message_ref"] == d.args["message_ref"] and not d.writes


@pytest.mark.parametrize("metadata", [{"id": "7b"}, {"id": "7b", "labelIds": []}])
def test_unlabelled_mail_can_still_be_deleted_with_unknown_scope_metadata(mailbox, metadata):
    d = mailbox
    d.accounts.save({**d.account, "scopes": None})
    d.message = metadata
    guard = d.guard([("", "active"), ("DRAFT", "paused")])
    assert not guard(d.args)
    d.delete(d.args)
    assert d.requests == [("GET", "/messages/7b"), ("DELETE", "/messages/7b")]


@pytest.mark.parametrize("metadata", [{}, {"id": "7c", "labelIds": []}, {"id": "7b", "labelIds": "DRAFT"},
                                     {"id": "7b", "labelIds": [None]}])
def test_unconfirmed_or_malformed_message_never_deleted(mailbox, metadata):
    d = mailbox
    d.message = metadata
    with pytest.raises(ValueError, match="nothing was deleted"):
        d.delete(d.args)
    assert not d.writes


def test_missing_permission_or_failed_metadata_read_never_deletes(mailbox):
    d = mailbox
    d.accounts.save({**d.account, "scopes": ["https://www.googleapis.com/auth/gmail.modify"]})
    with pytest.raises(AccountError, match="full mail scope"):
        d.delete(d.args)
    assert not d.requests
    d.accounts.save(d.account)
    d.status = 404
    with pytest.raises(GmailError, match="404"):
        d.delete(d.args)
    assert d.requests == [("GET", "/messages/7b")]


@pytest.mark.parametrize("label_name", [None, ""])
def test_unconfirmed_name_of_current_user_label_never_deletes(mailbox, label_name):
    d = mailbox
    d.message["labelIds"] = ["Label_1"]
    d.label_name = label_name
    with pytest.raises(ValueError, match="current Gmail label IDs"):
        d.delete(d.args)
    assert not d.writes
