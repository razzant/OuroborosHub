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

from .accounts import AccountError, AccountStore, SETTING_KEYS, public_message_ref
from .client import MailClient
from .delivery import tool_origin
from .gmail import GmailClient
from .host_adapter import LoopbackPresenceHostAdapter, place_refusal_hold
from .mime import conversation_root, mailbox_addresses, recipient_groups, reply_all_recipients
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
    return mailbox_addresses(value)


def conversation_fields(message):
    root = conversation_root(references=message.get('references'), in_reply_to=message.get('in_reply_to'),
                             message_id=message.get('message_id'))
    return {'conversation_id': root, 'thread_id': root} if root else {}


def account_client(accounts, account, *, imap_client=MailClient):
    """Open the same account transport for direct tools and the companion worker."""
    if account["provider"] == "gmail":
        return GmailClient(accounts, account["id"])
    if account["provider"] == "ews":
        from .ews import EwsClient  # Exchange dependencies are not needed to open other providers.
        return EwsClient(account)
    if account["provider"] == "imap":
        return imap_client({**account["config"], "EMAIL_DEFAULT_SENDER": account.get("sender") or account["address"],
                            "EMAIL_DEFAULT_FOLDER": account.get("folder") or "INBOX"})
    raise AccountError("Unknown mail provider for this account")


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
        client = account_client(accounts, account, imap_client=self.mail_client)
        try:
            yield account, store, client
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    @contextmanager
    def _presence_host(self):
        getter = getattr(getattr(self, "api", None), "get_skill_token", None)
        if getter is None:
            yield None
            return
        port = os.environ.get("OUROBOROS_HOST_SERVICE_PORT", "8767")
        url = os.environ.get("HOST_SERVICE_URL") or f"http://127.0.0.1:{port}"
        host = LoopbackPresenceHostAdapter("", host_service_url=url, skill_token=getter(), sync_only=True)
        try:
            yield host
        finally:
            host.close_sync()

    def capture_binding(self, account, conversation):
        with self._presence_host() as host:
            return host.capture_binding(account["id"], conversation, thread_id=conversation) if host else {"supported": False}

    @staticmethod
    def _sender(account, sender):
        selected = str(sender or account.get("sender") or account["address"])
        if selected not in account["senders"]:
            raise AccountError("Choose a sender listed for this account in email_accounts; configure additional addresses in Email accounts")
        return selected

    @staticmethod
    def wrap_ref(account, message):
        native = message.get("message_ref") or {"uid": message["uid"], "uidvalidity": message["uidvalidity"], "folder": message["folder"]}
        return public_message_ref(account["id"], native)

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
            if account["provider"] in {"gmail", "ews"} and isinstance(result, str):
                return {"message_ref": result}
            if account["provider"] == "imap" and isinstance(result, dict) and set(result) == {"uid", "uidvalidity", "folder"}:
                return result
        except AccountError:
            raise
        except (ValueError, TypeError, KeyError):
            pass
        raise ValueError("Use the complete message_ref returned by email_search or email_read")

    def queue_status(self, account):
        directory = self.accounts.mail_dir(account)
        if not (directory / "email_presence.sqlite3").exists():
            return {"inbox": {}, "outbox": {}, "outbox_held": 0, "receipts": [], "delivery_reports_pending": 0}
        store = EmailStore(directory)
        return {**store.status(), "poll_health": store.get("poll_health")}

    def list_accounts(self):
        return {"accounts": [{**AccountStore.public(row), "queue": self.queue_status(row)} for row in self.accounts.list()],
                "setup": "Open the Email accounts widget to connect Google, Exchange (EWS), IMAP or Proton Bridge. A Place is optional for ordinary tools."}

    def search(self, *, account_id="", folder=None, criteria=None, query="", limit=50, page_token=""):
        with self.target(account_id) as (account, _store, client):
            kwargs = {"folder": folder or account.get("folder") or "INBOX", "criteria": criteria, "limit": limit}
            if account["provider"] in {"gmail", "ews"}:
                kwargs.update(query=query, page_token=page_token)
            elif query or page_token:
                raise ValueError("Use IMAP criteria for this account; query/page_token belong to Gmail or Exchange search")
            result = client.search(**kwargs)
            for message in result.get("messages", []):
                message["message_ref"] = self.wrap_ref(account, message)
                message["account_id"] = account["id"]
                message.update(conversation_fields(message))
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
            message.update(conversation_fields(message))
            return message

    def _account_result(self, account, result):
        if result.get("message_ref"):
            result = {**result, "message_ref": self.wrap_ref(account, result)}
        return {**result, "account_id": account["id"]}

    @staticmethod
    def _write_target_preparer(public, account_id, *, clear_unused=()):
        # Compare what Host actually saw with the selected mailbox and the
        # provider's final targets. One retry exposes all missing facts.
        def prepare(targets):
            actual = {**{k: [] for k in clear_unused if public.get(k)}, **targets, 'account_id': account_id}
            missing = {key: value for key, value in actual.items() if public.get(key) != value}
            if missing:
                return {'ok': False, 'status': 'needs_arguments',
                        'code': 'canonical_account_required' if 'account_id' in missing else 'canonical_target_required',
                        'message': 'Retry with the actual mail targets below; no provider change was made.',
                        'retry_arguments': {**public, **missing}}
        return prepare

    def mark_read(self, *, account_id="", message_ref="", uid=None, uidvalidity=None, folder=None, remove_labels=None, folder_names=None):
        with self.target(account_id) as (account, _store, client):
            kwargs = self.unwrap_ref(account, message_ref) if message_ref else {
                "uid": uid, "uidvalidity": uidvalidity, "folder": folder or account.get("folder") or "INBOX"}
            public = {k: v for k, v in dict(account_id=account_id, message_ref=message_ref, uid=uid,
                      uidvalidity=uidvalidity, folder=folder, remove_labels=remove_labels, folder_names=folder_names).items()
                      if v is not None}
            if account['provider'] == 'gmail' and folder_names:
                raise ValueError("folder_names on email_mark_read applies only to IMAP and Exchange")
            if account['provider'] != 'gmail' and remove_labels:
                raise ValueError("remove_labels on email_mark_read applies only to Gmail")
            return self._account_result(account, client.mark_read(**kwargs, prepare_targets=self._write_target_preparer(
                public, account['id'], clear_unused=('folder_names',))))

    def mailbox(self, *, account_id="", message_ref="", folder_names=None, **kwargs):
        with self.target(account_id) as (account, _store, client):
            public = {k: v for k, v in kwargs.items() if v is not None}
            public.update(account_id=account_id, message_ref=message_ref)
            if folder_names is not None:
                public['folder_names'] = folder_names
            prepare = self._write_target_preparer(public, account['id'], clear_unused=('folder_names',))
            if message_ref:
                kwargs.update(self.unwrap_ref(account, message_ref))
            if account['provider'] == 'gmail':
                if folder_names:
                    raise ValueError("folder_names on email_mailbox applies only to IMAP and Exchange")
                return self._account_result(account, client.mailbox(**kwargs, prepare_targets=prepare))
            for key in ('label_id', 'label_names', 'add_labels', 'remove_labels'):
                if kwargs.pop(key, None) not in (None, '', []):
                    raise ValueError(f"{key} on email_mailbox applies only to Gmail")
                public.pop(key, None)
            if kwargs.get('action', 'list') != 'list':
                kwargs['prepare_targets'] = prepare
            return self._account_result(account, client.mailbox(**kwargs))

    @staticmethod
    def _prepare_target(argument, value, retry_arguments):
        if not isinstance(value, str):
            raise ValueError(f"The canonical {argument} must be a string")
        return {'ok': False, 'status': 'needs_arguments', 'code': 'canonical_target_required',
                'message': f'Retry with the actual {argument} below; no provider change was made.',
                'retry_arguments': {**retry_arguments, argument: value}}

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

    def _reply_context(self, account, store, client, message_id, references, reply_all, folder, replaying=False):
        original = store.message_by_id(message_id) if message_id and not replaying else None
        if message_id and not replaying and original is None and (reply_all or references is None or account['provider'] == 'gmail'):
            original = self._reply_source(store, client, message_id,
                    folder or ('ALL' if account['provider'] == 'gmail' else account.get('folder', 'INBOX')))
            if original is None:
                raise ValueError('Reply source was not found; read it first or select its folder with reply_folder')
        if reply_all and not message_id:
            raise ValueError('reply_to_message_id is required for reply_all')
        if references is None and original:
            references = original.get('references') or ([original['in_reply_to']] if original.get('in_reply_to') else [])
        root = conversation_root(references=references, in_reply_to=message_id)
        if original and root != conversation_fields(original).get('conversation_id'):
            raise ValueError('references names another RFC conversation; use the reply source references from email_read')
        return original, references, root

    @staticmethod
    def _reply_groups(account, original, to, cc, bcc, *, queued=None):
        if queued is None:
            derived = reply_all_recipients(original, [account["address"], *account["senders"]])
            derived_to, derived_cc = derived["to"], derived["cc"]
        else:
            # A repeated high-level request compares against the first send. Only the
            # queue can supply its automatic members, never the current source headers.
            derived_to, derived_cc = queued.to, queued.cc
        return recipient_groups([*derived_to, *to], [*derived_cc, *cc], bcc)

    @staticmethod
    def _prepare_conversation(root, conversation_id, thread_id, retry_arguments, *, required,
                              missing_account=False, expanded=False):
        fields = {'conversation_id': root, 'thread_id': root}
        retry_arguments = {key: value for key, value in retry_arguments.items() if value is not None}
        if any(value and value != root for value in (conversation_id, thread_id)):
            return {'ok': False, 'status': 'error', 'code': 'conversation_identity_mismatch',
                    'error': 'Conversation fields must match the resolved RFC reply source.',
                    'retry_arguments': {**retry_arguments, **fields}}
        missing_thread = required and (not conversation_id or not thread_id)
        if missing_account or missing_thread or expanded:
            return {'ok': False, 'status': 'needs_arguments',
                    'code': ('canonical_account_required' if missing_account else
                             'canonical_conversation_required' if missing_thread else 'canonical_recipients_required'),
                    'message': 'Retry with the resolved mail arguments below; no message or draft was queued or saved.',
                    'retry_arguments': {**retry_arguments, **fields}}
        return None

    def send(self, ctx=None, *, account_id="", sender="", to="", body="", subject="", reply_to_message_id="", references=None,
             cc=None, bcc=None, reply_all=False, html_body="", body_type="plain", attachments=None, request_id="", reply_folder="",
             conversation_id="", thread_id=""):
        with self.target(account_id) as (account, store, client):
            if account.get("sending", "on") == "off":
                return {"ok": False, "account_id": account["id"], "code": "sending_off", "error": SENDING_OFF}
            chosen_sender = self._sender(account, sender)
            recipients, visible_cc, visible_bcc = addresses(to), addresses(cc), addresses(bcc)
            rid = str(request_id or uuid.uuid4().hex)
            queued = store.queued_message(rid)
            if queued is not None and references is None:
                references = list(queued.references)
            original, references, thread = self._reply_context(account, store, client, reply_to_message_id,
                                                               references, reply_all, reply_folder, replaying=queued is not None)
            if reply_all:
                recipients, visible_cc, visible_bcc = self._reply_groups(account, original, recipients, visible_cc, visible_bcc, queued=queued)
                to, cc, bcc = ", ".join(recipients), visible_cc, visible_bcc
            if not recipients or (not str(body).strip() and not str(html_body).strip()):
                raise ValueError("to (or reply_all source) and body are required")
            thread = thread or store.message_id_for(rid)
            # Identical retries keep the first row's binding, including an unknown
            # or disconnected one. The store still compares payload and sender.
            preparation = self._prepare_conversation(thread, conversation_id, thread_id, {
                'account_id': account['id'], 'sender': chosen_sender, 'to': to, 'body': body, 'subject': subject,
                'reply_to_message_id': reply_to_message_id, 'references': references, 'cc': cc, 'bcc': bcc,
                'reply_all': False, 'html_body': html_body, 'body_type': body_type, 'attachments': attachments,
                'request_id': rid, 'reply_folder': reply_folder}, required=queued is None and bool(reply_to_message_id or references),
                missing_account=queued is None and not account_id, expanded=queued is None and reply_all)
            if preparation:
                return preparation
            binding = self.capture_binding(account, thread) if queued is None else {}
            reporting = {"version": -1, "account_id": account["id"], "sender": chosen_sender, "origin": tool_origin(ctx)}
            if account["provider"] == "gmail" and original and original.get("provider_thread_id"):
                reporting["gmail_thread_id"] = original["provider_thread_id"]
            hold = ""
            if binding.get("supported"):
                reporting["place_binding"] = binding.get("binding")
                if not binding.get("allowed"):
                    hold = place_refusal_hold(binding)
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

    def draft(self, *, account_id="", sender="", reply_all=False, reply_folder="", conversation_id="", thread_id="", folder_names=None, **kwargs):
        with self.target(account_id) as (account, store, client):
            chosen_sender = self._sender(account, sender)
            original, references, thread = self._reply_context(account, store, client, kwargs.get('reply_to_message_id'),
                                                               kwargs.get('references'), reply_all, reply_folder)
            kwargs['references'] = references
            supplied_folder = kwargs.get('folder')
            folder = supplied_folder or {'gmail': 'DRAFT', 'ews': 'drafts', 'imap': 'Drafts'}[account['provider']]
            if not isinstance(folder, str):
                raise ValueError("Choose a folder string returned by email_folders")
            if account['provider'] == 'gmail' and folder == 'Drafts':
                folder = 'DRAFT'
            if account['provider'] == 'imap' and folder.isascii() and folder.upper() == 'INBOX':
                folder = 'INBOX'  # Only INBOX is case-insensitive in IMAP.
            save = client.draft
            names = []
            if account['provider'] == 'ews':
                folder, names, save = client.draft_destination(folder)
            elif account['provider'] == 'gmail' and folder == 'DRAFT':
                names = ['Drafts']
            elif folder_names:
                raise ValueError("Omit folder_names unless returned for this account in retry_arguments")
            retry = {**kwargs, 'to': kwargs.get('to', ''), 'account_id': account['id'], 'sender': chosen_sender,
                     'reply_all': reply_all, 'reply_folder': reply_folder, 'folder': folder,
                     **({'folder_names': names} if names else {})}
            if reply_all:
                to, cc, bcc = self._reply_groups(account, original, addresses(kwargs.get("to")),
                                                 addresses(kwargs.get("cc")), addresses(kwargs.get("bcc")))
                retry.update(to=", ".join(to), cc=cc, bcc=bcc, reply_all=False)
            preparation = self._prepare_conversation(thread, conversation_id, thread_id, retry,
                required=bool(kwargs.get('reply_to_message_id') or references), missing_account=not account_id, expanded=reply_all)
            if not preparation and (folder != supplied_folder or names != (folder_names or [])):
                retry.update(conversation_id=thread, thread_id=thread)
                preparation = self._prepare_target('folder', folder, {k: v for k, v in retry.items() if v is not None})
            # Host dispatch checked the RFC conversation. Check the physical folder
            # independently; before a retry, retain a caller alias that would disappear.
            targets = (folder, *names)
            if preparation:
                targets = (supplied_folder,) if supplied_folder and supplied_folder not in targets else ()
            if targets:
                with self._presence_host() as host:
                    if host:
                        for target in dict.fromkeys(targets):
                            hold = host.draft_folder_hold(account['id'], target)
                            if hold:
                                return {"ok": False, "code": "draft_folder_held", "error": hold,
                                        "folder": target, "account_id": account['id']}
            if preparation:
                return preparation
            kwargs["folder"] = folder
            kwargs["sender"] = chosen_sender
            kwargs["attachments"] = store.stage_outbound_attachments("draft:" + uuid.uuid4().hex, kwargs.pop("attachments", None) or [])
            for key in ("cc", "bcc"):
                kwargs[key] = addresses(kwargs.get(key))
            if account["provider"] == "gmail" and original and original.get("provider_thread_id"):
                kwargs["thread_id"] = original["provider_thread_id"]
            result = save(**kwargs)
            return self._account_result(account, {**result, "sender": chosen_sender})

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
                return {"ok": False, "code": "website_required", "message": "This mail adapter manages messages and folders. Open the provider website for account settings."}
            return client.settings_read(**kwargs)

    def settings_write(self, *, account_id="", forward_to="", add_labels=None, remove_labels=None,
                       label_names=None, **kwargs):
        with self.target(account_id) as (account, _store, client):
            if account["provider"] != "gmail":
                return {"ok": False, "code": "website_required", "message": "Configure filters, forwarding and account settings on this provider's website."}
            public = {**kwargs, "account_id": account_id, "forward_to": forward_to, "add_labels": add_labels or [],
                      "remove_labels": remove_labels or [], "label_names": label_names or []}
            if not (kwargs.get("resource") == "filters" and kwargs.get("action") == "create"):
                if forward_to or add_labels or remove_labels or label_names:
                    raise ValueError("Filter targets apply only to creating a Gmail filter")
                # Only the account is affected by these settings writes, including deleting a rule.
                public = {**kwargs, "account_id": account_id}
            return client.settings_write(**kwargs, forward_to=forward_to,
                                         prepare_targets=self._write_target_preparer(public, account["id"]))


def register_tools(api, service):
    string, integer = {"type": "string"}, {"type": "integer"}
    array = {"type": "array", "items": string}
    account = {"account_id": {"type": "string", "description": "Exact canonical id from email_accounts; required for writes with email_send, email_draft, email_mailbox, email_mark_read or email_settings and whenever more than one account is connected. An omitted sole account can be prepared before the effect."}}
    message = {"message_ref": string, "uid": integer, "uidvalidity": integer, "folder": string}
    compose = {"to": string, "body": string, "subject": string, "sender": string, "reply_to_message_id": string,
               'conversation_id': {'type': 'string', 'description': 'Canonical RFC conversation from email_read or retry_arguments; required for a threaded reply.'},
               'thread_id': {'type': 'string', 'description': 'The same canonical RFC root, so the existing host guard checks the actual conversation.'},
               "reply_folder": string, "references": array, "cc": array, "bcc": array, "reply_all": {"type": "boolean"},
               "html_body": string, "body_type": {"type": "string", "enum": ["plain", "html"]},
               "attachments": {"type": "array", "items": {"type": "object"}}}
    def tool(name, handler, description, properties, required=(), *, select=True):
        api.register_tool(name, json_tool(handler), description=description,
                          schema={"type": "object", "properties": {**(account if select else {}), **properties}, "required": list(required)}, timeout_sec=120)
    tool("email_accounts", service.list_accounts, "List configured mailbox identities, sender addresses and granted scopes; no passwords or tokens are returned.", {}, select=False)
    # email_accounts has no selection argument: it is the discovery surface.
    tool("email_send", service.send, "Queue a mail or RFC-threaded reply from an explicit account_id/sender. For replies use conversation_id/thread_id from email_read. Reply All first returns the actual recipients; submit the returned arguments before sending. request_id is account-scoped. Queued is not sent; inspect email_receipt for held, accepted or uncertain outcomes.",
         {**compose, "request_id": string}, ("body",))
    tool("email_search", service.search, "Search old/new mail without marking it read. Gmail and Exchange accept provider-native query/page_token; IMAP uses criteria tokens. Returned message_ref is account-bound; a page reports its continuation.",
         {"folder": string, "criteria": array, "query": string, "limit": integer, "page_token": string})
    tool("email_read", service.read, "Read exact message_ref, or legacy uid+uidvalidity+folder, including canonical conversation_id/thread_id for replies. Peeks only; use email_mark_read for Seen. Historical reads do not enter Presence.", message)
    tool("email_mark_read", service.mark_read, "Mark this account's selected message read; accepts message_ref or legacy uid+uidvalidity+folder. Follow retry_arguments for the actual source folder or Gmail UNREAD label.",
         {**message, "remove_labels": array, "folder_names": array})
    tool("email_folders", service.folders, "Read folders/labels without changing anything, including while a Place is Silent or consciousness Advises.", {})
    tool("email_mailbox", service.mailbox, "Manage this account's folders/messages. IMAP and Exchange: list/create/copy/move/flags; Exchange also supports trash. After an Exchange move/copy, use the returned account-bound message_ref, or search the destination if no reference was supplied. Gmail also labels/archive/trash/untrash/delete/update_label/delete_label/delete_draft; permanent delete requires its grant. Follow retry_arguments to expose all affected folders, folder names or labels before a write.",
         {**message, "action": string, "destination": string, "folder_names": array, "flags": array, "remove_flags": {"type": "boolean"},
          "add_labels": array, "remove_labels": array, "label_names": array, "label_id": string, "draft_id": string, "properties": {"type": "object"}})
    tool("email_draft", service.draft, "Save a draft without sending. Supply account_id and, for replies, canonical conversation_id/thread_id; needs_arguments returns exact retry_arguments before saving when identity, Reply All recipients or the effective folder need preparation. Choose a sender from email_accounts; Gmail draft_id updates an existing draft, while IMAP and Exchange save new drafts in the selected folder.",
         {**compose, "folder": string, "folder_names": {"type": "array", "items": string, "description": "Names of the actual draft destination from retry_arguments. Do not invent folder aliases."}, "draft_id": string}, ("to", "subject"))
    tool("email_receipt", service.receipt, "Read the exact account-scoped delivery receipt. Uncertain acceptance must be inspected, never blindly sent again.", {"request_id": string}, ("request_id",))
    tool("email_test_connection", service.test_connection, "Check the selected account's authentication/TLS without sending mail or admitting historical messages.", {})
    settings = {"resource": string, "identifier": string}
    tool("email_settings_read", service.settings_read, "Read Gmail filters, send-as, vacation, language, IMAP/POP or forwarding settings. Other providers return website_required.", settings)
    tool("email_settings", service.settings_write, "Change supported Gmail settings. A filter creation exposes the actual account, forwarding address, label IDs and their current names before saving. Follow returned retry_arguments when any target is missing or stale. Deleting a filter does not execute its actions.",
         {**settings, "action": string, "values": {"type": "object"},
          "forward_to": {"type": "string", "description": "Actual forwarding address on filters/create. Given alone it fills values.action.forward."},
          "add_labels": array, "remove_labels": array,
          "label_names": {"type": "array", "items": string, "description": "Current names of filter label IDs from retry_arguments. These target fields do not change values.action."}}, ("resource",))
