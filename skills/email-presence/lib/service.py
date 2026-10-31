"""Account-aware mail operations used by the extension's tools and account UI."""
from __future__ import annotations

import base64
import inspect
import json
import os
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .accounts import AccountError, AccountStore, SETTING_KEYS
from .client import MailClient
from .delivery import tool_origin
from .gmail import GmailClient
from .host_adapter import LoopbackPresenceHostAdapter
from .mime import reply_all_recipients
from .settings import SENDING_OFF
from .store import EmailStore, RequestIdConflict


def json_tool(handler):
    if inspect.iscoroutinefunction(handler):
        @wraps(handler)
        async def encoded_async(*args, **kwargs):
            return json.dumps(await handler(*args, **kwargs), ensure_ascii=False)
        return encoded_async
    @wraps(handler)
    def encoded(*args, **kwargs):
        return json.dumps(handler(*args, **kwargs), ensure_ascii=False)
    return encoded


def addresses(value):
    return [str(x).strip() for x in (value if isinstance(value, (list, tuple)) else str(value or "").split(",")) if str(x).strip()]


class MailService:
    def __init__(self, api, *, mail_client=MailClient):
        self.api = api
        self.state = Path(api.get_state_dir())
        self.mail_client = mail_client

    @property
    def accounts(self):
        return AccountStore(self.state, self.api.get_settings(SETTING_KEYS))

    @contextmanager
    def target(self, account_id=""):
        accounts = self.accounts
        account = accounts.resolve(account_id)
        store = EmailStore(accounts.mail_dir(account))
        if account["provider"] == "gmail":
            client = GmailClient(accounts, account["id"])
        else:
            settings = {**account["config"], "EMAIL_DEFAULT_SENDER": account.get("sender") or account["address"],
                        "EMAIL_DEFAULT_FOLDER": account.get("folder") or "INBOX"}
            client = self.mail_client(settings)
        try:
            yield account, store, client
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    def capture_binding(self, account, conversation):
        getter = getattr(self.api, "get_skill_token", None)
        if getter is None:
            return {"supported": False}
        port = os.environ.get("OUROBOROS_HOST_SERVICE_PORT", "8767")
        url = os.environ.get("HOST_SERVICE_URL") or f"http://127.0.0.1:{port}"
        host = LoopbackPresenceHostAdapter("", host_service_url=url, skill_token=getter(), sync_only=True)
        try:
            return host.capture_binding(account["id"], conversation)
        finally:
            host.close_sync()

    @staticmethod
    def _sender(account, sender):
        selected = str(sender or account.get("sender") or account["address"])
        if selected not in account["senders"]:
            raise AccountError("Choose a sender listed for this account in email_accounts; configure additional addresses in Email accounts")
        return selected

    @staticmethod
    def wrap_ref(account, message):
        native = message.get("message_ref") or {"uid": message["uid"], "uidvalidity": message["uidvalidity"], "folder": message["folder"]}
        payload = {"account_id": account["id"], "ref": native}
        return "mail:" + base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")

    @staticmethod
    def unwrap_ref(account, reference):
        try:
            if not str(reference).startswith("mail:"):
                raise ValueError
            encoded = reference[5:]
            value = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if value["account_id"] != account["id"]:
                raise AccountError("This message_ref belongs to another account")
            result = value["ref"]
            if account["provider"] == "gmail" and isinstance(result, str):
                return {"message_ref": result}
            if account["provider"] == "imap" and isinstance(result, dict) and set(result) == {"uid", "uidvalidity", "folder"}:
                return result
        except AccountError:
            raise
        except (ValueError, TypeError, KeyError):
            pass
        raise ValueError("Use the complete message_ref returned by email_search or email_read")

    def list_accounts(self):
        return {"accounts": [AccountStore.public(row) for row in self.accounts.list()],
                "setup": "Open the Email accounts widget to connect Google, IMAP or Proton Bridge. A Place is optional for ordinary tools."}

    def search(self, *, account_id="", folder=None, criteria=None, query="", limit=50, page_token=""):
        with self.target(account_id) as (account, _store, client):
            kwargs = {"folder": folder or account.get("folder") or "INBOX", "criteria": criteria, "limit": limit}
            if account["provider"] == "gmail":
                kwargs.update(query=query, page_token=page_token)
            elif query or page_token:
                raise ValueError("Use IMAP criteria for this account; query/page_token belong to Gmail search")
            result = client.search(**kwargs)
            for message in result.get("messages", []):
                message["message_ref"] = self.wrap_ref(account, message)
                message["account_id"] = account["id"]
            return {**result, "account_id": account["id"]}

    def read(self, *, account_id="", message_ref="", uid=None, uidvalidity=None, folder=None):
        with self.target(account_id) as (account, store, client):
            kwargs = self.unwrap_ref(account, message_ref) if message_ref else {
                "uid": uid, "uidvalidity": uidvalidity, "folder": folder or account.get("folder") or "INBOX"}
            if not message_ref and (uid is None or uidvalidity is None):
                raise ValueError("Supply message_ref or both uid and uidvalidity")
            message = client.read(**kwargs, include_attachment_data=True)
            if message.get("_raw_source") is not None or message.get("attachments"):
                store.stage_inbound_attachments(message)
            if message.get("message_id"):
                store.remember_read(message)
            message.update(account_id=account["id"], message_ref=self.wrap_ref(account, {**kwargs, **message}))
            return message

    def mark_read(self, *, account_id="", message_ref="", uid=None, uidvalidity=None, folder=None):
        with self.target(account_id) as (account, _store, client):
            kwargs = self.unwrap_ref(account, message_ref) if message_ref else {
                "uid": uid, "uidvalidity": uidvalidity, "folder": folder or account.get("folder") or "INBOX"}
            return {**client.mark_read(**kwargs), "account_id": account["id"]}

    def mailbox(self, *, account_id="", message_ref="", **kwargs):
        with self.target(account_id) as (account, _store, client):
            if message_ref:
                kwargs.update(self.unwrap_ref(account, message_ref))
            return {**client.mailbox(**kwargs), "account_id": account["id"]}

    def folders(self, *, account_id=""):
        return self.mailbox(account_id=account_id, action="list")

    @staticmethod
    def _reply_source(store, client, message_id, folder):
        if not message_id:
            return None
        original = store.message_by_id(message_id)
        if original is None:
            original = client.find_message(message_id, folder=folder)
            if original:
                store.remember_read(original)
        return original

    def send(self, ctx=None, *, account_id="", sender="", to="", body="", subject="", reply_to_message_id="", references=None,
             cc=None, bcc=None, reply_all=False, html_body="", body_type="plain", attachments=None, request_id="", reply_folder=""):
        with self.target(account_id) as (account, store, client):
            if account.get("sending", "on") == "off":
                return {"ok": False, "account_id": account["id"], "code": "sending_off", "error": SENDING_OFF}
            chosen_sender = self._sender(account, sender)
            recipients, visible_cc, visible_bcc = addresses(to), addresses(cc), addresses(bcc)
            original = None
            if reply_all or (reply_to_message_id and account["provider"] == "gmail"):
                if not reply_to_message_id:
                    raise ValueError("reply_to_message_id is required for reply_all")
                original = self._reply_source(store, client, reply_to_message_id,
                                              reply_folder or ("ALL" if account["provider"] == "gmail" else account.get("folder", "INBOX")))
                if not original:
                    raise ValueError("Reply source was not found; read it first or select its folder with reply_folder")
                if references is None:
                    references = original.get("references") or ([original["in_reply_to"]] if original.get("in_reply_to") else [])
            if reply_all:
                derived = reply_all_recipients(original, chosen_sender)
                own = {value.casefold() for value in account["senders"]}
                recipients = [value for value in derived["to"] if value.casefold() not in own] + recipients
                visible_cc = [value for value in derived["cc"] if value.casefold() not in own] + visible_cc
            if not recipients or (not str(body).strip() and not str(html_body).strip()):
                raise ValueError("to (or reply_all source) and body are required")
            rid = str(request_id or uuid.uuid4().hex)
            thread = (references or [reply_to_message_id or store.message_id_for(rid)])[0]
            binding = self.capture_binding(account, thread)
            reporting = {"version": -1, "account_id": account["id"], "sender": chosen_sender, "origin": tool_origin(ctx)}
            if original and original.get("provider_thread_id"):
                reporting["gmail_thread_id"] = original["provider_thread_id"]
            hold = ""
            if binding.get("supported"):
                reporting["place_binding"] = binding.get("binding")
                if not binding.get("allowed"):
                    hold = str(binding.get("code") or binding.get("state") or "Place unavailable")
            else:
                reporting["binding_support"] = "unsupported"
            staged = store.stage_outbound_attachments(rid, attachments or [])
            try:
                inserted = store.enqueue_outbox(request_id=rid, to=recipients, cc=visible_cc, bcc=visible_bcc, subject=subject,
                                                body=body, in_reply_to=reply_to_message_id, references=references or [],
                                                attachments=staged, html_body=html_body, body_type=body_type,
                                                reporting=reporting, hold_reason=hold)
            except RequestIdConflict as exc:
                return {"ok": False, "account_id": account["id"], "code": "request_id_conflict", "error": str(exc), "request_id": rid}
            receipt = store.receipt(rid)
            return {"ok": True, "account_id": account["id"], "sender": chosen_sender, "request_id": rid,
                    "state": receipt["state"], "accepted_by_provider": receipt["state"] == "completed",
                    "deduplicated": not inserted, "receipt": receipt,
                    "message": "Queued is not sent. email_receipt reports the provider outcome; held/uncertain items are not blindly resent."}

    def draft(self, *, account_id="", sender="", reply_all=False, reply_folder="", **kwargs):
        with self.target(account_id) as (account, store, client):
            chosen_sender = self._sender(account, sender)
            original = None
            if reply_all or (kwargs.get("reply_to_message_id") and account["provider"] == "gmail"):
                source_id = kwargs.get("reply_to_message_id")
                if not source_id:
                    raise ValueError("reply_to_message_id is required for reply_all")
                original = self._reply_source(store, client, source_id,
                                              reply_folder or ("ALL" if account["provider"] == "gmail" else account.get("folder", "INBOX")))
                if not original:
                    raise ValueError("Read the reply source first or choose reply_folder")
                if kwargs.get("references") is None:
                    kwargs["references"] = original.get("references") or ([original["in_reply_to"]] if original.get("in_reply_to") else [])
            if reply_all:
                derived = reply_all_recipients(original, chosen_sender)
                own = {value.casefold() for value in account["senders"]}
                kwargs["to"] = [value for value in derived["to"] if value.casefold() not in own] + addresses(kwargs.get("to"))
                kwargs["cc"] = [value for value in derived["cc"] if value.casefold() not in own] + addresses(kwargs.get("cc"))
            kwargs["sender"] = chosen_sender
            kwargs["attachments"] = store.stage_outbound_attachments("draft:" + uuid.uuid4().hex, kwargs.pop("attachments", None) or [])
            for key in ("cc", "bcc"):
                kwargs[key] = addresses(kwargs.get(key))
            if original and original.get("provider_thread_id"):
                kwargs["thread_id"] = original["provider_thread_id"]
            result = client.draft(**kwargs)
            if result.get("message_ref"):
                result["message_ref"] = self.wrap_ref(account, result)
            return {**result, "account_id": account["id"], "sender": chosen_sender}

    def receipt(self, request_id, *, account_id=""):
        account = self.accounts.get(account_id) if account_id else self.accounts.resolve()
        directory = self.accounts.mail_dir(account)
        if not (directory / "email_presence.sqlite3").exists():
            return None
        result = EmailStore(directory).receipt(request_id)
        if result:
            result["account_id"] = account["id"]
        return result

    def test_connection(self, *, account_id=""):
        with self.target(account_id) as (account, _store, client):
            return {**client.test_connection(), "account_id": account["id"]}

    def settings_read(self, *, account_id="", **kwargs):
        with self.target(account_id) as (account, _store, client):
            if account["provider"] != "gmail":
                return {"ok": False, "code": "website_required", "message": "This provider exposes account settings on its website; IMAP/SMTP manage messages and folders."}
            return client.settings_read(**kwargs)

    def settings_write(self, *, account_id="", **kwargs):
        with self.target(account_id) as (account, _store, client):
            if account["provider"] != "gmail":
                return {"ok": False, "code": "website_required", "message": "Configure filters, forwarding and account settings on this provider's website."}
            return client.settings_write(**kwargs)


def register_tools(api, service):
    string, integer = {"type": "string"}, {"type": "integer"}
    array = {"type": "array", "items": string}
    account = {"account_id": {"type": "string", "description": "Exact canonical id from email_accounts; required when more than one account is connected."}}
    message = {"message_ref": string, "uid": integer, "uidvalidity": integer, "folder": string}
    compose = {"to": string, "body": string, "subject": string, "sender": string, "reply_to_message_id": string,
               "reply_folder": string, "references": array, "cc": array, "bcc": array, "reply_all": {"type": "boolean"},
               "html_body": string, "body_type": {"type": "string", "enum": ["plain", "html"]},
               "attachments": {"type": "array", "items": {"type": "object"}}}
    def tool(name, handler, description, properties, required=(), *, select=True):
        api.register_tool(name, json_tool(handler), description=description,
                          schema={"type": "object", "properties": {**(account if select else {}), **properties}, "required": list(required)}, timeout_sec=120)
    tool("email_accounts", service.list_accounts, "List configured mailbox identities, sender addresses and granted scopes; no passwords or tokens are returned.", {}, select=False)
    # email_accounts has no selection argument: it is the discovery surface.
    tool("email_send", service.send, "Queue a mail or RFC-threaded reply from an explicit account/sender. request_id is account-scoped. Queued is not sent; inspect email_receipt for held, accepted or uncertain outcomes.",
         {**compose, "request_id": string}, ("body",))
    tool("email_search", service.search, "Search old/new mail without marking it read. Gmail uses query/page_token; IMAP uses criteria tokens. Returned message_ref is account-bound; a page reports its continuation.",
         {"folder": string, "criteria": array, "query": string, "limit": integer, "page_token": string})
    tool("email_read", service.read, "Read exact message_ref, or legacy uid+uidvalidity+folder. Peeks only; use email_mark_read for Seen. Historical reads do not enter Presence.", message)
    tool("email_mark_read", service.mark_read, "Mark this account's selected message read; accepts message_ref or legacy uid+uidvalidity+folder.", message)
    tool("email_folders", service.folders, "Read folders/labels without changing anything, including while a Place is Silent or consciousness Advises.", {})
    tool("email_mailbox", service.mailbox, "Manage this account's folders/messages. IMAP: list/create/copy/move/flags, no global expunge. Gmail also labels/archive/trash/untrash/delete/update_label/delete_label/delete_draft; permanent delete requires its grant.",
         {**message, "action": string, "destination": string, "flags": array, "remove_flags": {"type": "boolean"},
          "add_labels": array, "remove_labels": array, "label_id": string, "draft_id": string, "properties": {"type": "object"}})
    tool("email_draft", service.draft, "Save a draft without sending. Choose a sender from email_accounts; Gmail draft_id updates an existing draft, while IMAP appends to its selected folder.",
         {**compose, "folder": string, "draft_id": string}, ("to", "subject"))
    tool("email_receipt", service.receipt, "Read the exact account-scoped delivery receipt. Uncertain acceptance must be inspected, never blindly sent again.", {"request_id": string}, ("request_id",))
    tool("email_test_connection", service.test_connection, "Check the selected account's authentication/TLS without sending mail or admitting historical messages.", {})
    settings = {"resource": string, "identifier": string}
    tool("email_settings_read", service.settings_read, "Read Gmail filters, send-as, vacation, language, IMAP/POP or forwarding settings. Other providers return website_required.", settings)
    tool("email_settings", service.settings_write, "Change provider-supported Gmail settings: filters create/delete, vacation, language, IMAP/POP and existing send-as properties. Unsupported administrative forwarding actions name the website path.",
         {**settings, "action": string, "values": {"type": "object"}}, ("resource",))
