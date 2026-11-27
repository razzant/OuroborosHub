"""Account-aware mail operations used by the extension's tools and account UI."""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import re
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .accounts import AccountError, AccountStore, SETTING_KEYS, public_message_ref
from .client import MailClient
from .delivery import tool_origin
from .gmail import GmailClient
from .gmail_host import GmailSignIn, read_view
from .host_adapter import (MATCH_SCOPE, PLACE_MATCH, RECIPIENT_PLACES, LoopbackPresenceHostAdapter, capture_recipients,
                           place_refusal_hold)
from .mime import (AmbiguousReplySource, _message_ids, canonical_message_id, checked_display_names, conversation_root, display_names_of, emitted_conversation,
                   envelope_groups, mailbox_addresses, named_recipient_groups, recipient_groups, reply_all_recipients,
                   reply_chain, usable_display_names, wire_thread_problem)
from .settings import SENDING_OFF
from .retained import reply_source
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


def declared_values(value):
    """One target argument as the Host reads it (tool_effects.target_values): a string, or a list of them."""
    items = value if isinstance(value, (list, tuple)) else [value]
    return [str(item).strip() for item in items if type(item) in (str, int) and str(item).strip()]


def recipient_arguments(to, cc, bcc, groups):
    """Canonical To/Cc/Bcc arguments for ``groups`` (envelope_groups), and whether the supplied ones already are.

    The Host checks each declared recipient value as one destination, so every value must be one actual envelope
    address, listed once. A single To stays a string, as an ordinary call writes it."""
    to_list, cc_list, bcc_list = (list(group) for group in groups)
    canonical = declared_values(to) == to_list and declared_values(cc) == cc_list and declared_values(bcc) == bcc_list
    single = not isinstance(to, (list, tuple)) and len(to_list) <= 1
    return {"to": (to_list[0] if to_list else "") if single else to_list, "cc": cc_list, "bcc": bcc_list}, canonical


def recipient_display_names(groups, explicit, supplied, original=None):
    """Header display names for the bare recipients in ``groups``: explicit display_names, then names spelled
    in the supplied mailboxes, then the Reply All source's headers. Only listed recipients can be named."""
    listed = {address.casefold(): address for group in groups for address in group}
    names = {}
    if original:
        structured = original.get("address_names")
        if isinstance(structured, dict):
            # Names parsed from the source's raw header syntax, so an encoded "Doe, John" stays one name.
            names.update({str(key).casefold(): value for key, value in structured.items()
                          if isinstance(key, str) and isinstance(value, str) and value.strip()})
        else:
            # A read or capture stored by an earlier version has only the decoded header text.
            headers = original.get("headers") or {}
            names.update(display_names_of(*[value for key in ("cc", "to", "from", "reply-to") for value in headers.get(key, [])]))
        sender = str(original.get("sender") or "").casefold()
        if sender and str(original.get("sender_name") or "").strip():
            names[sender] = str(original["sender_name"]).strip()
    names.update(display_names_of(*supplied))
    # A name read from mail or named input is only carried when a header writes it back unchanged.
    names = usable_display_names(names)
    names.update(checked_display_names(explicit, groups))
    # Keyed by the canonical spelling the Host sees in to/cc/bcc.
    return {listed[key]: name for key, name in names.items() if key in listed}


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
        return AccountStore(self.state, self.api.get_settings(SETTING_KEYS), sign_in=GmailSignIn.environment())

    @contextmanager
    def target(self, account_id=""):
        accounts = self.accounts
        account = accounts.resolve(account_id)
        store = EmailStore(accounts.mail_dir(account))
        client = account_client(accounts, account, imap_client=self.mail_client)
        if account['provider'] == 'gmail':
            # drafts.get reveals the underlying physical message only inside the provider. Check retained
            # authority there too, before a replacement/deletion can erase an old comment-derived Place.
            def retained_source_check(message):
                if held := self._historical_mutation_hold(account, store, message):
                    raise ValueError(held['error'])
            client.retained_source_check = retained_source_check
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

    def capture_recipients(self, account, envelope):
        """Each actual envelope address's own Place scope, retained with the queued send (see host_adapter)."""
        with self._presence_host() as host:
            return capture_recipients(host, account["id"], envelope) if host else ("", None)

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
            return {"inbox": {}, "outbox": {}, "outbox_held": 0, "receipts": [], "waiting": [], "delivery_reports_pending": 0}
        store = EmailStore(directory)
        return {**store.status(), "poll_health": store.get("poll_health")}

    def list_accounts(self):
        mode, rows = read_view(self.accounts)
        setup = "Use Email accounts for Exchange (EWS), IMAP, or Proton Bridge. A Place is optional for ordinary tools."
        gmail = {"legacy": "Google mail is also added in Email accounts.",
                 "host": "For Google mail, use the email-presence card in Extensions › Skills.",
                 "unavailable": "Google sign-in could not be checked; retry when the host is available."}
        return {"accounts": [{**AccountStore.public(row), "queue": self.queue_status(row)} for row in rows],
                "setup": setup + " " + gmail[mode], "google_auth_mode": mode}

    def search(self, *, account_id="", folder=None, criteria=None, query="", limit=50, page_token=""):
        with self.target(account_id) as (account, _store, client):
            kwargs = {"folder": folder or account.get("folder") or "INBOX", "criteria": criteria, "limit": limit,
                      "page_token": page_token}
            if account["provider"] in {"gmail", "ews"}:
                kwargs["query"] = query
            elif query:
                raise ValueError("Use IMAP criteria for this account; query belongs to Gmail or Exchange search")
            else:
                kwargs["scope"] = account["id"]  # An IMAP continuation stays with the account that issued it.
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

    # A supplied target the operation does not affect is cleared (with this empty value) rather than trusted.
    _UNUSED = {'folder_names': [], 'message_conversation_id': '', 'message_thread_id': ''}

    @staticmethod
    def _write_target_preparer(public, account_id, *, clear_unused=()):
        # Compare what Host actually saw with the selected mailbox and the
        # provider's final targets. One retry exposes all missing facts.
        def prepare(targets):
            actual = {**{k: MailService._UNUSED[k] for k in clear_unused if public.get(k)}, **targets,
                      'account_id': account_id}
            missing = {key: value for key, value in actual.items() if public.get(key) != value}
            if missing:
                return {'ok': False, 'status': 'needs_arguments',
                        'code': 'canonical_account_required' if 'account_id' in missing else 'canonical_target_required',
                        'message': 'Retry with the actual mail targets below; no provider change was made.',
                        'retry_arguments': {**public, **missing}}
        return prepare

    def mark_read(self, *, account_id="", message_ref="", uid=None, uidvalidity=None, folder=None, remove_labels=None,
                  folder_names=None, message_conversation_id="", message_thread_id="", label_names=None):
        with self.target(account_id) as (account, _store, client):
            kwargs = self.unwrap_ref(account, message_ref) if message_ref else {
                "uid": uid, "uidvalidity": uidvalidity, "folder": folder or account.get("folder") or "INBOX"}
            if held := self._historical_mutation_hold(account, _store, kwargs):
                return held
            public = {k: v for k, v in dict(account_id=account_id, message_ref=message_ref, uid=uid,
                      uidvalidity=uidvalidity, folder=folder, remove_labels=remove_labels, folder_names=folder_names,
                      message_conversation_id=message_conversation_id, message_thread_id=message_thread_id,
                      label_names=label_names).items()
                      if v is not None}
            # Every provider prepares the message's current conversation (message_conversation_id/thread) itself;
            # the supplied values are only compared with it.
            if account['provider'] == 'gmail':
                kwargs.update(message_conversation_id=message_conversation_id, message_thread_id=message_thread_id,
                              label_names=label_names)
            elif label_names:
                raise ValueError("label_names on email_mark_read applies only to Gmail")
            if account['provider'] == 'gmail' and folder_names:
                raise ValueError("folder_names on email_mark_read applies only to IMAP and Exchange")
            if account['provider'] != 'gmail' and remove_labels:
                raise ValueError("remove_labels on email_mark_read applies only to Gmail")
            return self._account_result(account, client.mark_read(**kwargs, prepare_targets=self._write_target_preparer(
                public, account['id'], clear_unused=tuple(self._UNUSED))))

    def mailbox(self, *, account_id="", message_ref="", folder_names=None, request_id="", **kwargs):
        if kwargs.get("action") in ("discard_queued", "send_queued"):
            return self.queued(account_id=account_id, request_id=request_id, message_ref=message_ref,
                               folder_names=folder_names, **kwargs)
        if request_id:
            raise ValueError("request_id on email_mailbox applies only to discard_queued and send_queued")
        with self.target(account_id) as (account, _store, client):
            public = {k: v for k, v in kwargs.items() if v is not None}
            public.update(account_id=account_id, message_ref=message_ref)
            if folder_names is not None:
                public['folder_names'] = folder_names
            prepare = self._write_target_preparer(public, account['id'], clear_unused=tuple(self._UNUSED))
            if message_ref:
                kwargs.update(self.unwrap_ref(account, message_ref))
            if kwargs.get('action', 'list') not in ('list', 'create'):
                if held := self._historical_mutation_hold(account, _store, kwargs):
                    return held
            if kwargs.get('draft_conversation_id') and kwargs.get('action') != 'delete_draft':
                raise ValueError("draft_conversation_id on email_mailbox applies only to Gmail delete_draft")
            if account['provider'] == 'gmail':
                if folder_names:
                    raise ValueError("folder_names on email_mailbox applies only to IMAP and Exchange")
                return self._account_result(account, client.mailbox(**kwargs, prepare_targets=prepare))
            for key in ('label_id', 'label_names', 'add_labels', 'remove_labels', 'draft_conversation_id'):
                if kwargs.pop(key, None) not in (None, '', []):
                    raise ValueError(f"{key} on email_mailbox applies only to Gmail")
                public.pop(key, None)
            # Compared with the conversation the provider reads now; never passed on as an input.
            kwargs.pop('message_conversation_id', None)
            kwargs.pop('message_thread_id', None)
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

    def _historical_mutation_hold(self, account, store, selection):
        # The provider's fresh parser checks actual ancestry. A retained old projection may also have a Place;
        # neither a new read nor a parser upgrade releases restrictions attached to that historical scope.
        if account.get('provider') == 'imap':
            folder = selection.get('folder', 'INBOX')  # same default and INBOX case semantics as MailClient.mailbox
            if isinstance(folder, str):
                selection = {**selection, 'folder': MailClient._canonical_folder(folder)}
        scopes = store.historical_message_roots(selection)
        if scopes:
            with self._presence_host() as host:
                if host:
                    for scope in scopes:
                        if reason := host.draft_folder_hold(account['id'], scope, thread_id=scope):
                            return {'ok': False, 'code': 'historical_source_held', 'error': reason,
                                    'account_id': account['id']}

    def folders(self, *, account_id=""):
        return self.mailbox(account_id=account_id, action="list")

    def _reply_context(self, account, store, client, message_id, references, reply_all, folder, replaying=False):
        """``(original, references, root, repair)`` for a new reply, from its source verified in this mailbox.

        A queued request replays its accepted row unchanged. Otherwise the caller's reply_to_message_id must name
        one Message-ID; its source is looked up in this mailbox (captured or read mail, else the selected provider
        folder, whose parsed token must match that ID) for every provider, whether or not references were
        supplied. The References chain and conversation come from that source, never from the caller: supplied
        references are compared with it, and references without a reply source are refused. ``repair`` names the
        canonical spellings to retry with and whether supplied references named another conversation."""
        if replaying:
            return None, references, conversation_root(references=references, in_reply_to=message_id), {}
        if references is not None and (not isinstance(references, (list, tuple))
                                       or any(not isinstance(item, str) for item in references)):
            raise ValueError('references must be a list of Message-ID strings from email_read')
        if reply_all and not message_id:
            raise ValueError('reply_to_message_id is required for reply_all')
        if not message_id:
            if any(item.strip() for item in references or ()):
                raise ValueError('references continue a conversation only together with reply_to_message_id of a '
                                 'message in this mailbox; omit references for a new email')
            return None, references, '', {}
        canonical = canonical_message_id(message_id)
        if not canonical:
            raise ValueError('reply_to_message_id must be one Message-ID such as <id@example.com>, '
                             'as email_read or email_search returns it')
        original = self._reply_source(store, client, canonical,
                                      folder or ('ALL' if account['provider'] == 'gmail' else account.get('folder', 'INBOX')),
                                      selected=bool(folder))
        if original is None:
            raise ValueError('Reply source was not found; read it first or select its folder with reply_folder')
        original = reply_source(original)
        chain = reply_chain(original)
        root = conversation_fields(original).get('conversation_id', '')
        historical = store.historical_message_roots(original)
        if len(historical) > 1:
            raise ValueError('Retained source has conflicting historical conversation scopes; nothing was queued or saved')
        if historical:
            # A folder-selected fresh read resolves the physical source, not permission to forget its old Place.
            # Existing source/emitted bindings can represent this pair without migrating either identity.
            root = historical[0]
        repair = {}
        if original['message_id'] != message_id:
            repair['respelled'] = True
        if references is not None and _message_ids(' '.join(references)) != _message_ids(' '.join(chain)):
            repair['mismatch'] = True
        return original, chain, root, repair

    @staticmethod
    def _reply_source(store, client, message_id, folder, *, selected=False):
        """The captured or read source with this canonical token, else the provider’s verified source.

        When captured or read mail names different messages with this ID, only the caller's explicit reply_folder
        selects one: the provider's verified source there, itself refused when that folder is ambiguous too."""
        try:
            original = store.message_by_id(message_id)
        except AmbiguousReplySource:
            if not selected:
                raise AmbiguousReplySource('Ambiguous reply source: this Message-ID names different messages in the '
                                           'mailbox. Nothing was queued or saved; to answer one of them, name the folder '
                                           'that holds it with reply_folder') from None
            original = None
        if original is None or canonical_message_id(original.get('message_id')) != canonical_message_id(message_id):
            original = client.find_message(message_id, folder=folder)
            if original is not None and canonical_message_id(original.get('message_id')) != canonical_message_id(message_id):
                original = None
            if original:
                store.remember_read(original)
        return original

    @staticmethod
    def _reply_groups(account, original, to, cc, bcc):
        derived = reply_all_recipients(original, [account["address"], *account["senders"]])
        return recipient_groups([*derived["to"], *to], [*derived["cc"], *cc], bcc)

    @staticmethod
    def _reply_fingerprint(account_id, request_id, message_id, supplied, envelope):
        # The explicit inputs and the disclosed envelope must BOTH match. The queue
        # alone cannot distinguish automatically included copies from manual ones.
        data = ["reply-all:1", account_id, request_id, message_id, supplied, envelope]
        return hashlib.sha256(json.dumps(data, separators=(",", ":")).encode("ascii")).hexdigest()

    @staticmethod
    def _prepare_conversation(root, conversation_id, thread_id, retry_arguments, *, required,
                              missing_account=False, expanded=False, repair=None, emitted="", supplied_emitted=""):
        fields = {'conversation_id': root, 'thread_id': root}
        actual_emitted = emitted if emitted and emitted != root else ''
        if actual_emitted or supplied_emitted:
            fields['emitted_conversation_id'] = actual_emitted
        retry_arguments = {key: value for key, value in retry_arguments.items() if value is not None}
        repair = repair or {}
        if repair.get('mismatch') or any(value and value != root for value in (conversation_id, thread_id)):
            return {'ok': False, 'status': 'error', 'code': 'conversation_identity_mismatch',
                    'error': ('Conversation fields and references must match the verified RFC reply source; '
                              'retry_arguments carry its chain. Nothing was queued or saved.'),
                    'retry_arguments': {**retry_arguments, **fields}}
        missing_thread = required and (not conversation_id or not thread_id)
        respelled = bool(repair.get('respelled')) or supplied_emitted != actual_emitted
        if missing_account or respelled or missing_thread or expanded:
            return {'ok': False, 'status': 'needs_arguments',
                    'code': ('canonical_account_required' if missing_account else
                             'canonical_reply_source_required' if respelled else
                             'canonical_conversation_required' if missing_thread else 'canonical_recipients_required'),
                    'message': 'Retry with the resolved mail arguments below; no message or draft was queued or saved.'
                               + (' Each actual recipient appears once in to, cc or bcc as a bare address.' if expanded else ''),
                    'retry_arguments': {**retry_arguments, **fields}}
        return None

    def send(self, ctx=None, *, account_id="", sender="", to="", body="", subject="", reply_to_message_id="", references=None,
             cc=None, bcc=None, reply_all=False, html_body="", body_type="plain", attachments=None, request_id="", reply_folder="",
             conversation_id="", thread_id="", reply_all_fingerprint="", display_names=None, emitted_conversation_id="",
             draft_id=""):
        if draft_id:
            given = {"sender": sender, "reply_to_message_id": reply_to_message_id, "references": references,
                     "reply_all": reply_all, "body_type": body_type if body_type != "plain" else "",
                     "attachments": attachments, "reply_folder": reply_folder,
                     "reply_all_fingerprint": reply_all_fingerprint, "display_names": display_names,
                     "emitted_conversation_id": emitted_conversation_id}
            return self._send_draft(ctx, account_id=account_id, draft_id=draft_id, to=to, cc=cc, bcc=bcc,
                                    request_id=request_id, conversation_id=conversation_id, thread_id=thread_id,
                                    content={"subject": subject, "body": body, "html_body": html_body},
                                    given=[name for name, value in given.items() if value])
        with self.target(account_id) as (account, store, client):
            if account.get("sending", "on") == "off":
                return {"ok": False, "account_id": account["id"], "code": "sending_off", "error": SENDING_OFF}
            chosen_sender = self._sender(account, sender)
            recipients, visible_cc, visible_bcc = addresses(to), addresses(cc), addresses(bcc)
            rid = str(request_id or uuid.uuid4().hex)
            queued = store.queued_message(rid)
            # A new mail records the access it began with: named here, before its source is read (it goes out under the
            # mailbox's current sign-in, rule A).
            # A request_id already queued replays its accepted row and names none.
            begin = getattr(client, "change_link", None) if queued is None and account["provider"] == "gmail" else None
            link = begin() if begin else None
            if queued is not None and references is None:
                references = list(queued.references)
            original, references, thread, repair = self._reply_context(
                account, store, client, reply_to_message_id, references, reply_all, reply_folder,
                replaying=queued is not None)
            if queued is None and reply_to_message_id:
                reply_to_message_id = original['message_id']
            supplied = (recipients, visible_cc, visible_bcc)
            if reply_all:
                groups = (envelope_groups(queued.to, queued.cc, queued.bcc) if queued is not None
                          else envelope_groups(*self._reply_groups(account, original, *supplied)))
                reply_all_fingerprint = self._reply_fingerprint(account['id'], rid, reply_to_message_id, supplied, groups)
                if queued is not None and queued.reporting.get('reply_all_fingerprint') not in {
                        reply_all_fingerprint,
                        # A row queued before 0.10.11 may hold named spellings in its fingerprinted groups.
                        self._reply_fingerprint(account['id'], rid, reply_to_message_id, supplied,
                                                (queued.to, queued.cc, queued.bcc))}:
                    known = bool(queued.reporting.get('reply_all_fingerprint'))
                    return {'ok': False, 'account_id': account['id'], 'request_id': rid,
                            'code': 'request_id_conflict' if known else 'reply_all_replay_unverifiable',
                            'error': ('The Reply All arguments differ from the accepted send.' if known else
                                      'The queued email has no record of the original Reply All expansion.')
                                     + ' Use its accepted explicit arguments to replay, or check email_receipt.'}
                # A replay enqueues the accepted rows as stored, header names included.
                recipients, visible_cc, visible_bcc = (queued.to, queued.cc, queued.bcc) if queued is not None else groups
            elif queued is None:
                # A new send queues exactly the envelope the Host checks, one bare address per recipient.
                recipients, visible_cc, visible_bcc = envelope_groups(*supplied)
            elif envelope_groups(*supplied) == envelope_groups(queued.to, queued.cc, queued.bcc):
                # A replay names the queued mailboxes, perhaps spelled as in its first, prepared call.
                recipients, visible_cc, visible_bcc = queued.to, queued.cc, queued.bcc
            if not (recipients or visible_cc or visible_bcc) or (not str(body).strip() and not str(html_body).strip()):
                # A mail may have only Cc or Bcc recipients (one moved from another mailbox keeps them apart).
                raise ValueError("a recipient (to, cc or bcc, or a reply_all source) and body are required")
            bare = envelope_groups(recipients, visible_cc, visible_bcc)
            prepared, canonical = recipient_arguments(to, cc, bcc, bare)
            if queued is not None:
                # A replay keeps the accepted header names; a different explicit naming is another email.
                names = recipient_display_names(bare, None, (queued.to, queued.cc, queued.bcc))
                if display_names and recipient_display_names(bare, display_names, ()) != names:
                    return {"ok": False, "account_id": account["id"], "code": "request_id_conflict", "request_id": rid,
                            "error": "display_names differ from the accepted send; use a new request_id for a new email"}
            else:
                names = recipient_display_names(bare, display_names, supplied, original if reply_all else None)
                canonical = canonical and recipient_display_names(bare, display_names, ()) == names
                # Checked before any preparation or queue: the named headers deliver to exactly these addresses.
                named = named_recipient_groups(bare, names)
            thread = thread or store.message_id_for(rid)
            # Identical retries keep the first row's binding, including an unknown
            # or disconnected one. The store still compares payload and sender.
            preparation = self._prepare_conversation(thread, conversation_id, thread_id, {
                'account_id': account['id'], 'sender': chosen_sender, 'to': prepared['to'], 'body': body, 'subject': subject,
                'reply_to_message_id': reply_to_message_id, 'references': references,
                'cc': prepared['cc'] if prepared['cc'] or cc is not None or reply_all else None,
                'bcc': prepared['bcc'] if prepared['bcc'] or bcc is not None or reply_all else None,
                'reply_all': False, 'html_body': html_body, 'body_type': body_type, 'attachments': attachments,
                'request_id': rid, 'reply_folder': reply_folder, 'reply_all_fingerprint': reply_all_fingerprint or None,
                'display_names': names or None},
                required=queued is None and bool(reply_to_message_id or references),
                missing_account=queued is None and not account_id,
                expanded=queued is None and (reply_all or not canonical), repair=repair,
                emitted=emitted_conversation(reply_to_message_id, references or []) if queued is None and reply_to_message_id else '',
                supplied_emitted=emitted_conversation_id if queued is None else '')
            if preparation:
                return preparation
            if queued is None and (problem := wire_thread_problem(reply_to_message_id, references or [])):
                raise ValueError(problem + '; nothing was queued')
            binding = self.capture_binding(account, thread) if queued is None else {}
            reporting = {"version": -1, "account_id": account["id"], "sender": chosen_sender, "origin": tool_origin(ctx)}
            if queued is None and original is not None:
                reporting['reply_source_syntax'] = 1
            # This optional round-trip value only recognizes a later implicit replay.
            # A missing or modified value never selects recipients or authorizes a send.
            if reply_to_message_id and isinstance(reply_all_fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", reply_all_fingerprint):
                reporting["reply_all_fingerprint"] = reply_all_fingerprint
            if account["provider"] == "gmail" and original and original.get("provider_thread_id"):
                reporting["gmail_thread_id"] = original["provider_thread_id"]
            if link:
                reporting["gmail_connection"] = link  # the access this mail began under, named again as it goes out
            hold, modes = "", {}
            if binding.get("supported"):
                # Each new binding keeps the mode it was bound with; delivery checks it with the same mode.
                reporting["place_binding"], modes["place_binding"] = binding.get("binding"), MATCH_SCOPE
                if not binding.get("allowed"):
                    hold = place_refusal_hold(binding)
            else:
                reporting["binding_support"] = "unsupported"
            if queued is None:
                # The Host checked these bare addresses; the queued headers carry their display names.
                recipients, visible_cc, visible_bcc = named
            if queued is None and emitted_conversation_id:
                emitted_binding = self.capture_binding(account, emitted_conversation_id)
                if emitted_binding.get('supported'):
                    reporting['emitted_place_binding'] = emitted_binding.get('binding')
                    modes['emitted_place_binding'] = MATCH_SCOPE
                    if not emitted_binding.get('allowed'):
                        hold = hold or place_refusal_hold(emitted_binding)
            if queued is None:
                # To, Cc and Bcc as delivered: a Silent/Paused Place naming any of these addresses (exact_casefold)
                # holds the send; the recipients are audience, separate from the call's content binding.
                reason, record = self.capture_recipients(account, [address for group in bare for address in group])
                if record is not None:
                    reporting[RECIPIENT_PLACES], modes[RECIPIENT_PLACES] = record, record['match']
                hold = hold or reason
                if modes:
                    reporting[PLACE_MATCH] = modes
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

    def _send_draft(self, ctx, *, account_id, draft_id, to, cc, bcc, request_id, conversation_id, thread_id, content,
                    given):
        """email_send with draft_id: send one existing Gmail draft as it is now (users.drafts.send) through the queue, so
        Sending off, Place holds, its receipt and the never-twice rule apply to it as to any mail. The Host checks the
        draft's actual To, Cc and Bcc, and its conversation when it replies, and Safety reads its subject and text, from
        this very call: needs_arguments names them first. Delivery sends it only while it is still the draft that was
        checked. Its attachments are not named in the call."""
        with self.target(account_id) as (account, store, client):
            if account["provider"] != "gmail":
                raise ValueError("draft_id applies only to Gmail; send this account's drafts from its mail client")
            if given:
                raise ValueError("draft_id sends that draft as it is; omit " + ", ".join(given))
            content = {name: str(value or "") for name, value in content.items()}
            if account.get("sending", "on") == "off":
                return {"ok": False, "account_id": account["id"], "code": "sending_off", "error": SENDING_OFF}
            rid = str(request_id or uuid.uuid4().hex)
            queued = store.queued_message(rid)
            if queued is not None:
                if (queued.reporting.get("gmail_draft") or {}).get("id") != draft_id:
                    return {"ok": False, "account_id": account["id"], "code": "request_id_conflict", "request_id": rid,
                            "error": "This request_id already names a different email; use a new request_id"}
                receipt = store.receipt(rid)
                return {"ok": True, "account_id": account["id"], "request_id": rid, "state": receipt["state"],
                        "accepted_by_provider": receipt["state"] == "completed", "deduplicated": True, "receipt": receipt}
            link = client.change_link()  # named before the draft is read, presented again where it is sent
            letter = client.draft_letter(draft_id)
            if letter["sender"].casefold() not in {sender.casefold() for sender in account["senders"]}:
                raise AccountError("The draft's From is not a sender listed for this account in email_accounts; "
                                   "nothing was queued")
            groups = envelope_groups(letter["recipients"], letter["cc"], letter["bcc"])
            if not any(groups):
                raise ValueError("The draft names no recipient; nothing was queued")
            prepared, canonical = recipient_arguments(to, cc, bcc, groups)
            replying = bool(letter["in_reply_to"] or letter["references"])
            root = (conversation_root(references=letter["references"], in_reply_to=letter["in_reply_to"],
                                      message_id=letter["message_id"]) or store.message_id_for(rid))
            fields = {"conversation_id": root, "thread_id": root} if replying else {}
            # What it says, as Safety reads a mail: its subject and text (and its HTML when it has one).
            words = {"subject": letter["subject"], "body": letter["body"],
                     **({"html_body": letter["body_html"]} if letter.get("body_html") else {})}
            retry = {"account_id": account["id"], "draft_id": draft_id, "request_id": rid, "to": prepared["to"],
                     "cc": prepared["cc"], "bcc": prepared["bcc"], **fields, **words}
            stray = any(value and value != root for value in (conversation_id, thread_id))
            missing = replying and (conversation_id != root or thread_id != root)
            unread = any(content[name] != words.get(name, "") for name in content)
            if not account_id or not canonical or stray or missing or unread:
                return {"ok": False, "status": "needs_arguments", "account_id": account["id"],
                        "code": ("canonical_account_required" if not account_id else
                                 "canonical_recipients_required" if not canonical else
                                 "canonical_conversation_required" if stray or missing else "canonical_content_required"),
                        "message": "Retry with the draft's actual recipients, conversation and words below; nothing "
                                   "was queued.",
                        "retry_arguments": retry}
            if replying and (problem := wire_thread_problem(letter["in_reply_to"], letter["references"])):
                raise ValueError(problem + '; nothing was queued')
            # The draft as it is read now, kept whole: rule A puts it back from this source if its send's outcome is
            # unknown and the draft is gone, and its receipt gives its HTML and files to send from another mailbox.
            source, files = store.stage_draft(rid, letter)
            reporting = {"version": -1, "account_id": account["id"], "sender": letter["sender"], "origin": tool_origin(ctx),
                         "gmail_draft": {"id": draft_id, "digest": letter["digest"], "source": source},
                         "reply_source_syntax": 1}
            if letter["provider_thread_id"]:
                reporting["gmail_thread_id"] = letter["provider_thread_id"]
            if link:
                reporting["gmail_connection"] = link
            hold, modes = "", {}
            binding = self.capture_binding(account, root)
            if binding.get("supported"):
                reporting["place_binding"], modes["place_binding"] = binding.get("binding"), MATCH_SCOPE
                if not binding.get("allowed"):
                    hold = place_refusal_hold(binding)
            else:
                reporting["binding_support"] = "unsupported"
            reason, record = self.capture_recipients(account, [address for group in groups for address in group])
            if record is not None:
                reporting[RECIPIENT_PLACES], modes[RECIPIENT_PLACES] = record, record["match"]
            hold = hold or reason
            if modes:
                reporting[PLACE_MATCH] = modes
            try:
                store.enqueue_outbox(request_id=rid, to=groups[0], cc=groups[1], bcc=groups[2], subject=letter["subject"],
                                     body=letter["body"], html_body=letter.get("body_html") or "",
                                     in_reply_to=letter["in_reply_to"], references=letter["references"],
                                     attachments=files, reporting=reporting, hold_reason=hold,
                                     message_id=letter["message_id"])
            except RequestIdConflict as exc:
                return {"ok": False, "account_id": account["id"], "code": "request_id_conflict", "error": str(exc), "request_id": rid}
            receipt = store.receipt(rid)
            return {"ok": True, "account_id": account["id"], "sender": letter["sender"], "request_id": rid,
                    "state": receipt["state"], "accepted_by_provider": False, "deduplicated": False, "receipt": receipt,
                    "message": "Queued is not sent: the draft goes out as it is now, and only while it is unchanged since "
                               "this check. email_receipt reports the outcome."}

    def queued(self, *, account_id="", action="", request_id="", **unused):
        """email_mailbox discard_queued / send_queued: the owner's "delete" or "send" for one mail of this mailbox that has
        not gone out, chosen by its request_id or by the ref Mail gave it (the owner's line and email_accounts' `waiting`
        name it by that). Both work while the mailbox is signed out of Mail: "delete" takes it off this mailbox's queue,
        in one write and only while it has not begun to go out, and returns the mail, which can then be sent from another
        mailbox; "send" needs this mailbox signed in again. A mail whose sending began and whose outcome is unknown is
        never offered for sending anywhere: it may already have gone out."""
        if not account_id or not request_id:
            raise ValueError(f"{action} takes the mail's account_id and request_id")
        if any(value not in (None, "", [], {}, False) for value in unused.values()):
            raise ValueError(f"{action} takes only account_id and request_id")
        accounts = self.accounts
        account = accounts.get(account_id)
        directory = accounts.mail_dir(account)
        if not (directory / "email_presence.sqlite3").exists():
            return {"ok": False, "code": "not_found", "account_id": account["id"], "request_id": request_id,
                    "error": "No mail with this request_id in this account"}
        store = EmailStore(directory)
        try:
            # Its request_id, or the ref a line to the owner (and `waiting`) names it by: the mail's own request_id below.
            request_id = store.request_id_of(request_id) or request_id
            receipt = store.receipt(request_id)
            if receipt is None:
                return {"ok": False, "code": "not_found", "account_id": account["id"], "request_id": request_id,
                        "error": "No mail with this request_id in this account"}
            began = receipt["state"] in ("uncertain", "partial")
            unknown = (f"Its sending began and its outcome is unknown: it may already have gone out from "
                       f"{account['address']}, so Mail never sends it again")
            if action == "discard_queued":
                # Only what the deleting write itself did speaks: the mail may have begun to go out since it was read above.
                receipt, done, found = store.discard_queued(request_id)
                if done == "cancelled":
                    message = ("It will not go out from this mailbox: it had not begun to. Its receipt stays, with the "
                               "mail in `letter`, so it can be sent from another mailbox with email_send.")
                elif done == "discarded":
                    message = (unknown + ". It is no longer looked for in Sent or put in Drafts; its receipt stays. "
                               "It is not to be sent from another mailbox.")
                elif found in ("processing", "sending", "uncertain", "partial"):
                    error = (f"It was not deleted: its sending began or is under way, so it may already have gone out "
                             f"from {account['address']}. Mail never sends it again, and it is not to be sent from "
                             "another mailbox; check its receipt.")
                else:
                    error = "Only a mail that has not gone out can be deleted from the queue; check its receipt."
                return {"ok": bool(done), "account_id": account["id"], "request_id": request_id, "receipt": receipt,
                        **({"message": message} if done else {"code": "not_discardable", "error": error})}
            if began:
                return {"ok": False, "code": "not_queued", "account_id": account["id"], "request_id": request_id,
                        "receipt": receipt, "error": unknown + (
                            ": once its mailbox is signed in, it is looked for in Sent and put in Drafts if it is not "
                            "there. Check its receipt." if not receipt.get("unknown_outcome") else
                            ". Check its receipt.")}
            if receipt["state"] not in ("pending", "retry", "held", "processing"):
                return {"ok": False, "code": "not_queued", "account_id": account["id"], "request_id": request_id,
                        "receipt": receipt, "error": "Only a mail still queued or held can be sent this way; check its receipt."}
            link = None
            try:
                if account["provider"] == "gmail":
                    client = GmailClient(accounts, account["id"])
                    try:
                        link = client.sending_link()  # this mailbox's current confirmed sign-in, positively its own account
                    finally:
                        client.close()
                elif not account["connected"]:
                    raise AccountError("This account is disconnected; reconnect it in Email accounts")
            except AccountError as exc:
                return {"ok": False, "code": "mailbox_not_signed_in", "account_id": account["id"], "request_id": request_id,
                        "error": f"{account['address']} is not signed in to Mail now ({exc}). The mail goes by itself once "
                                 "it is. To send it from another mailbox instead, first discard_queued it here: that "
                                 "takes it off this mailbox's queue, only while it has not begun to go out, and returns "
                                 "the mail (also in email_receipt). Only when it answers that the mail will not go out "
                                 "from this mailbox, send that mail from the other mailbox with email_send; when it "
                                 "answers that the mail may already have gone out, it is never sent again."}
            if not store.release_queued(request_id, link):
                return {"ok": False, "code": "not_queued", "account_id": account["id"], "request_id": request_id,
                        "receipt": store.receipt(request_id),
                        "error": "Only a mail still queued or held can be sent this way; check its receipt."}
            return {"ok": True, "account_id": account["id"], "request_id": request_id, "receipt": store.receipt(request_id),
                    "message": "It goes out now under this mailbox's current sign-in; Sending off and Place holds still "
                               "apply. email_receipt reports the outcome."}
        finally:
            store.close()

    def draft(self, *, account_id="", sender="", reply_all=False, reply_folder="", conversation_id="", thread_id="", folder_names=None,
              display_names=None, draft_conversation_id="", emitted_conversation_id="", **kwargs):
        with self.target(account_id) as (account, store, client):
            kwargs.pop("link", None)  # the access a change began under comes from the mailbox's client, never from the caller
            # A change is made under the access it began with: named here, before the reply source, the draft or its labels
            # are read to prepare it, and presented again when the draft is saved (GmailClient.change_link).
            begin = getattr(client, "change_link", None) if account["provider"] == "gmail" else None
            link = begin() if begin else None
            chosen_sender = self._sender(account, sender)
            original, references, thread, repair = self._reply_context(
                account, store, client, kwargs.get('reply_to_message_id'), kwargs.get('references'), reply_all, reply_folder)
            kwargs['references'] = references
            if kwargs.get('reply_to_message_id'):
                kwargs['reply_to_message_id'] = original['message_id']
            supplied_folder = kwargs.get('folder')
            folder = supplied_folder or {'gmail': 'DRAFT', 'ews': 'drafts', 'imap': 'Drafts'}[account['provider']]
            if not isinstance(folder, str):
                raise ValueError("Choose a folder string returned by email_folders")
            if account['provider'] == 'gmail' and folder == 'Drafts':
                folder = 'DRAFT'
            if account['provider'] == 'imap' and folder.isascii() and folder.upper() == 'INBOX':
                folder = 'INBOX'  # Only INBOX is case-insensitive in IMAP.
            save = client.draft
            names, replaced = [], ''
            if account['provider'] == 'ews':
                folder, names, save = client.draft_destination(folder)
            elif account['provider'] == 'gmail' and folder == 'DRAFT':
                names = ['Drafts']
                if kwargs.get('draft_id'):
                    # Replacing a draft rewrites its message wherever it is now: every label it bears and its RFC
                    # conversation, read before anything is saved. The saved content stays what the caller asked for.
                    names, replaced = client.draft_targets(kwargs['draft_id'])
            elif folder_names:
                raise ValueError("Omit folder_names unless returned for this account in retry_arguments")
            if draft_conversation_id and not (account['provider'] == 'gmail' and kwargs.get('draft_id')):
                raise ValueError("Omit draft_conversation_id unless returned for a Gmail draft_id in retry_arguments")
            supplied = (addresses(kwargs.get("to")), addresses(kwargs.get("cc")), addresses(kwargs.get("bcc")))
            groups = envelope_groups(*(self._reply_groups(account, original, *supplied) if reply_all else supplied))
            # The draft saves exactly the recipients the Host checks, one bare address each, with their display names.
            prepared, canonical = recipient_arguments(kwargs.get('to', ''), kwargs.get('cc'), kwargs.get('bcc'), groups)
            display = recipient_display_names(groups, display_names, supplied, original if reply_all else None)
            canonical = canonical and recipient_display_names(groups, display_names, ()) == display
            named = named_recipient_groups(groups, display)  # Bcc names stay in the saved draft's header.
            retry = {**kwargs, 'to': prepared['to'], 'account_id': account['id'], 'sender': chosen_sender,
                     'reply_all': False, 'reply_folder': reply_folder, 'folder': folder,
                     **({'folder_names': names} if names else {}), **({'display_names': display} if display else {}),
                     **({'draft_conversation_id': replaced} if replaced else {}),
                     **({'emitted_conversation_id': emitted_conversation_id} if emitted_conversation_id else {})}
            for key in ('cc', 'bcc'):
                if prepared[key] or kwargs.get(key) is not None or reply_all:
                    retry[key] = prepared[key]
            preparation = self._prepare_conversation(thread, conversation_id, thread_id, retry,
                required=bool(kwargs.get('reply_to_message_id') or references), missing_account=not account_id,
                expanded=reply_all or not canonical, repair=repair,
                emitted=emitted_conversation(kwargs.get('reply_to_message_id'), references or []) if original else '',
                supplied_emitted=emitted_conversation_id)
            if not preparation and (folder != supplied_folder or names != (folder_names or [])
                                    or draft_conversation_id != replaced):
                retry.update(conversation_id=thread, thread_id=thread)
                preparation = self._prepare_target('folder', folder, {k: v for k, v in retry.items() if v is not None})
            # Host dispatch checked the new RFC conversation. Check the physical folder, a replaced draft's
            # labels and its old conversation independently, so neither conversation hides the other;
            # before a retry, retain a caller alias that would disappear.
            targets = (folder, *names, *([replaced] if replaced else []),
                       *([emitted_conversation_id] if emitted_conversation_id else []))
            if preparation:
                targets = (supplied_folder,) if supplied_folder and supplied_folder not in targets else ()
            if targets:
                with self._presence_host() as host:
                    if host:
                        for target in dict.fromkeys(targets):
                            # The replaced draft's old conversation is checked with its thread (a Place stored with
                            # both); folders and labels are checked without one.
                            hold = host.draft_folder_hold(account['id'], target,
                                                          thread_id=target if target in (replaced, emitted_conversation_id) else '')
                            if hold:
                                return {"ok": False, "code": "draft_folder_held", "error": hold,
                                        "folder": target, "account_id": account['id']}
            if preparation:
                return preparation
            if problem := wire_thread_problem(kwargs.get('reply_to_message_id') or '', references or []):
                raise ValueError(problem + '; nothing was saved')
            kwargs["folder"] = folder
            kwargs["sender"] = chosen_sender
            kwargs["attachments"] = store.stage_outbound_attachments("draft:" + uuid.uuid4().hex, kwargs.pop("attachments", None) or [])
            to, cc, bcc = named
            # A single To keeps its string form, as the prepared arguments write it.
            kwargs.update(to=(to[0] if to else "") if isinstance(prepared["to"], str) else to, cc=cc, bcc=bcc)
            if account["provider"] == "gmail" and original and original.get("provider_thread_id"):
                kwargs["thread_id"] = original["provider_thread_id"]
            result = save(**kwargs, **({"link": link} if link else {}))
            return self._account_result(account, {**result, "sender": chosen_sender})

    def receipt(self, request_id, *, account_id=""):
        account = self.accounts.get(account_id) if account_id else self.accounts.resolve()
        directory = self.accounts.mail_dir(account)
        if not (directory / "email_presence.sqlite3").exists():
            return None
        store = EmailStore(directory)
        try:  # its request_id, or the ref a line to the owner (and `waiting`) names it by
            result = store.receipt(store.request_id_of(request_id) or request_id, with_letter=True)
        finally:
            store.close()
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
    # The Host checks every To/Cc/Bcc value as one destination, so each value is one bare address.
    recipients = ("Bare email addresses such as name@example.com, each actual recipient once across to, cc and bcc. "
                  "Display names, comma-separated text or repeats return needs_arguments with the actual list; "
                  "names move to display_names and stay in the sent headers.")
    compose = {"to": {"anyOf": [string, array], "description": "One recipient as a string, or a list of them. " + recipients},
               "body": string, "subject": string, "sender": string, "reply_to_message_id": string,
               'conversation_id': {'type': 'string', 'description': 'Canonical RFC conversation from email_read or retry_arguments; required for a threaded reply.'},
               'thread_id': {'type': 'string', 'description': 'The same canonical RFC root, so the existing host guard checks the actual conversation.'},
               "emitted_conversation_id": {"type": "string", "description": "From retry_arguments only: an emitted RFC root that differs from the historical source. Checked independently with its thread; never overrides source authority."},
               "reply_folder": string, "references": array, "cc": {**array, "description": recipients},
               "bcc": {**array, "description": recipients}, "reply_all": {"type": "boolean"},
               "html_body": string, "body_type": {"type": "string", "enum": ["plain", "html"]},
               "attachments": {"type": "array", "items": {"type": "object"}},
               "display_names": {"type": "object", "additionalProperties": string, "description":
                   "Optional header names for listed recipients, keyed by their bare address in to/cc/bcc, for example "
                   '{"sue@example.com": "Lee, Sue"}. Each name is one line of text and must read back unchanged from '
                   "the written header. Never adds a recipient; retry_arguments fill it from named input "
                   "or the Reply All source."}}
    draft_conversation = {"type": "string", "description": "Gmail draft_id only: the RFC conversation the existing draft "
                          "replies in, from retry_arguments. Checked separately from conversation_id; it never threads "
                          "the saved content."}
    message_conversation = {"type": "string", "description": "The selected message's current RFC conversation (Gmail, IMAP "
                            "and Exchange), from retry_arguments. An authority target only; never changes the provider request."}
    message_thread = {"type": "string", "description": "The same RFC root as message_conversation_id (for delete_draft, "
                      "draft_conversation_id), from retry_arguments, so the Host checks a Place stored with a thread."}
    def tool(name, handler, description, properties, required=(), *, select=True):
        api.register_tool(name, json_tool(handler), description=description,
                          schema={"type": "object", "properties": {**(account if select else {}), **properties}, "required": list(required)}, timeout_sec=120)
    tool("email_accounts", service.list_accounts, "List configured mailbox identities, sender addresses and granted scopes; no passwords or tokens are returned.", {}, select=False)
    # email_accounts has no selection argument: it is the discovery surface.
    tool("email_send", service.send, "Queue a mail or RFC-threaded reply from an explicit account_id/sender. For replies use conversation_id/thread_id from email_read. Reply All first returns the actual recipients; submit the returned arguments before sending. request_id is account-scoped. Queued is not sent; inspect email_receipt for held, accepted or uncertain outcomes. A mail whose sending began and whose outcome is unknown is never sent twice: Mail finds it sent in its own mailbox or puts it in that mailbox's Gmail Drafts and tells the owner once. Gmail draft_id instead sends that existing draft as it is now (its receipt or email_search folder DRAFT names it): submit the draft's actual recipients, subject and body from retry_arguments.",
         {**compose, "request_id": string, "reply_all_fingerprint": {"type": "string", "description":
              "Opaque replay value from Reply All retry_arguments. Pass it back unchanged when submitting the explicit recipients."},
          "draft_id": {"type": "string", "description": "Gmail only: send this existing draft as it is now; its actual "
                       "to, cc, bcc, conversation, subject and body come back in retry_arguments to submit unchanged."}})
    tool("email_search", service.search, "Search old/new mail without marking it read. Gmail and Exchange accept a provider-native query; IMAP uses raw criteria tokens. Pass each page's next_page_token back as page_token with the same account, folder and query or criteria until it comes back empty. Returned message_ref is account-bound. An empty or failed search is not proof that a message never arrived.",
         {"folder": string,
          "criteria": {"type": "array", "items": string, "description": 'IMAP only: raw SEARCH tokens; several keys must all match, for example ["SINCE", "1-Jun-2026", "FROM", "billing@example.com"]. Quote an ASCII operand that contains spaces: ["SUBJECT", "\\"New sign-in\\""]. A non-ASCII operand is sent as UTF-8 with or without quotes.'},
          "query": string,
          "limit": {"type": "integer", "description": "Messages per page, 1-200; default 50."},
          "page_token": {"type": "string", "description": "next_page_token of the previous page of this same search. IMAP pages run newest UID first (the folder's storage order, not message date) and report total, returned and has_more; mail that arrives after the first page needs a new search."}})
    tool("email_read", service.read, "Read exact message_ref, or legacy uid+uidvalidity+folder, including canonical conversation_id/thread_id for replies. Peeks only; use email_mark_read for Seen. Historical reads do not enter Presence.", message)
    tool("email_mark_read", service.mark_read, "Mark this account's selected message read; accepts message_ref or legacy uid+uidvalidity+folder. Follow retry_arguments for the actual source folder or Gmail UNREAD label.",
         {**message, "remove_labels": array, "folder_names": array, "message_conversation_id": message_conversation,
          "message_thread_id": message_thread, "label_names": array})
    tool("email_folders", service.folders, "Read folders/labels without changing anything, including while a Place is Silent or consciousness Advises.", {})
    tool("email_mailbox", service.mailbox, "Manage this account's folders/messages. IMAP and Exchange: list/create/copy/move/flags; Exchange also supports trash. After an Exchange move/copy, use the returned account-bound message_ref, or search the destination if no reference was supplied. Gmail also labels/archive/trash/untrash/delete/update_label/delete_label/delete_draft; permanent delete requires its grant. Follow retry_arguments to expose all affected folders, folder names or labels before a write. For one queued mail of this account that has not gone out, chosen by its request_id or by the ref the owner's line and email_accounts' waiting name it by: discard_queued drops it (also while the mailbox is signed out) and returns the mail, send_queued sends it now under the mailbox's current sign-in. To send it from another mailbox, discard_queued it here first and, only when it answers that the mail will not go out from here, email_send the returned mail there; one that may already have gone out is never sent again.",
         {**message, "action": string, "destination": string, "folder_names": array, "flags": array, "remove_flags": {"type": "boolean"},
          "request_id": {"type": "string", "description": "discard_queued and send_queued only: the queued mail's request_id, or the ref (m-...) a line to the owner names it by."},
          "add_labels": array, "remove_labels": array, "label_names": array, "label_id": string, "draft_id": string, "properties": {"type": "object"},
          "draft_conversation_id": draft_conversation, "message_conversation_id": message_conversation,
          "message_thread_id": message_thread})
    tool("email_draft", service.draft, "Save a draft without sending. Supply account_id and, for replies, canonical conversation_id/thread_id; needs_arguments returns exact retry_arguments before saving when identity, Reply All recipients or the effective folder need preparation. Choose a sender from email_accounts; Gmail draft_id updates an existing draft, while IMAP and Exchange save new drafts in the selected folder.",
         {**compose, "folder": string, "folder_names": {"type": "array", "items": string, "description": "Names of the actual draft destination from retry_arguments, and for a Gmail draft_id the label IDs and names the existing draft bears. Do not invent folder aliases."},
          "draft_id": string, "draft_conversation_id": draft_conversation}, ("to", "subject"))
    tool("email_receipt", service.receipt, "Read the exact account-scoped delivery receipt, by its request_id or by the ref (m-...) a line to the owner names the mail by. Uncertain acceptance must be inspected, never blindly sent again.", {"request_id": string}, ("request_id",))
    tool("email_test_connection", service.test_connection, "Check the selected account's authentication/TLS without sending mail or admitting historical messages.", {})
    settings = {"resource": string, "identifier": string}
    tool("email_settings_read", service.settings_read, "Read Gmail filters, send-as, vacation, language, IMAP/POP or forwarding settings. Other providers return website_required.", settings)
    tool("email_settings", service.settings_write, "Change supported Gmail settings. A filter creation exposes the actual account, forwarding address, label IDs and their current names before saving. Follow returned retry_arguments when any target is missing or stale. Deleting a filter does not execute its actions.",
         {**settings, "action": string, "values": {"type": "object", "description":
              "The Gmail resource fields. A send-as replyToAddress is one bare address the Host checks as this "
              "setting's destination; an empty string removes Reply-To."},
          "forward_to": {"type": "string", "description": "Actual forwarding address on filters/create. Given alone it fills values.action.forward."},
          "add_labels": array, "remove_labels": array,
          "label_names": {"type": "array", "items": string, "description": "Current names of filter label IDs from retry_arguments. These target fields do not change values.action."}}, ("resource",))
