"""Gmail resources behind the mail skill's existing MIME and delivery lifecycle."""
from __future__ import annotations

import base64
import contextvars
import copy
import email
import email.policy
import hashlib
import re
import time
from contextlib import contextmanager
from collections.abc import Mapping
from functools import cache, wraps
from email.policy import SMTP
from email.utils import getaddresses, parseaddr
from pathlib import Path

import httpx

from .accounts import AccountError, bearer_mark
from .gmail_host import (CARD, SignInError, bearer_scopes, credential_owner, host_link, local_grant,
                         mail_scope, queued_link, sending_link)
from .google_auth import FULL_MAIL_SCOPE, SETTINGS_SCOPE
from .client import MailClient
from .delivery import DeliveryDeferred, DeliveryRejected
from .mime import (_addresses, build_message, canonical_message_id, conversation_root, mailbox_addresses,
                   parse_message)

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_SYSTEM_LABEL_IDS = frozenset({"INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "STARRED", "UNREAD", "IMPORTANT"})
# messages.list leaves Spam and Trash out unless asked (includeSpamTrash defaults to false).
_SPAM_TRASH = frozenset({"SPAM", "TRASH"})
# A change is issued only under the access named when it began (change_link): a sign-in of the mailbox that changed in
# between stops it before anything is sent or changed. A queued mail is named its access anew each time it goes out.
ACCESS_CHANGED = ("This mailbox's Google sign-in changed while this operation was being prepared; nothing was sent or "
                  "changed. Try it again.")


_RENEWAL_WAIT = 45  # seconds a local renewal waits for another process renewing the same sign-in
# Mailbox actions that name the access they began with before anything is read to prepare them (GmailClient.change_link).
# Permanent deletion and settings take their one bearer first (_write_permission): it is not checked again after the
# preparation.
_CHANGES = frozenset({"create", "update_label", "delete_label", "delete_draft", "trash", "untrash", "copy", "move",
                      "archive", "flags", "labels"})
# One search, reply-source lookup or poll reads many messages and shares one snapshot of the host's list of sign-ins
# (gmail_host.credential_owner `read_accounts`): asked for once, and only if a read needs it. Only ordinary reads use it,
# never a change. It belongs to the call that opened it, in the context running it: another operation, or the next
# search, asks the host again.
_READING = contextvars.ContextVar("email_presence_gmail_reading", default=None)


class _ReadSeries:
    def __init__(self, client):
        self.client, self.held = client, None

    def __call__(self):
        if self.held is None:  # a list the host could not give is never kept: the error belongs to the read that asked
            self.held = self.client.accounts.sign_in.accounts()
        return self.held


def _in_series(method):
    """Run a method that reads a series of messages inside one snapshot of the host's list of sign-ins."""
    @wraps(method)
    def in_series(self, *args, **kwargs):
        active = _READING.get()
        if active is not None and active[0] is self:
            return method(self, *args, **kwargs)  # inside its caller's series (find_message's searches)
        token = _READING.set((self, _ReadSeries(self)))
        try:
            return method(self, *args, **kwargs)
        finally:
            _READING.reset(token)
    return in_series


def _fresh(tokens):
    return bool(tokens.get("access_token")) and float(tokens.get("expires_at") or 0) > time.time() + 60


def _draft_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Choose draft_id from email_search(folder='DRAFT')")
    return value


class GmailError(RuntimeError):
    def __init__(self, status, reason="", retry_after=None):
        self.status = int(status)
        self.reason = str(reason)
        self.retry_after = retry_after
        super().__init__(f"Gmail HTTP {status}" + (f" ({reason})" if reason else ""))


class GmailClient(MailClient):
    transport = "gmail_api"

    def __init__(self, accounts, account_id, *, http=None):
        self.accounts, self.account_id = accounts, account_id
        self.account = accounts.resolve(account_id)
        super().__init__({"EMAIL_USER": self.account["address"], "EMAIL_DEFAULT_FOLDER": self.account.get("folder", "INBOX"),
                          "EMAIL_DEFAULT_SENDER": self.account.get("sender") or self.account["address"]})
        self.http = http or httpx.Client(timeout=30)
        self._owns_http = http is None
        self._read_bearer = None
        self._rejected_bearer = None
        self._local_bearer = None  # (access token, late consent generation or '') a local path presented last

    def close(self):
        if self._owns_http:
            self.http.close()

    def _host_credential(self, account, *, reusable=False, change=False, link=None):
        binding = account["gmail_host"]
        host = self.accounts.sign_in
        if not host or not binding.get("verified"):
            raise SignInError(f"This Gmail sign-in could not be confirmed. Check {CARD}.")
        key = binding["key"]
        stamp = (key, account["generation"])
        cached = self._read_bearer
        if reusable and cached and cached[0] == stamp and time.monotonic() < cached[1]:
            access, scopes = cached[2:]
        else:
            denied = self._rejected_bearer
            # A change names the access it may use, and the host refuses any other (connection_changed, consent_changed):
            # a queued mail the one named as it goes out (sending_link); a change begun on the saved sign-in the
            # connection and consent its continuation was last confirmed with; a new change one row of the host's
            # current list. Reading names none.
            if not change:
                frozen = {}
            elif link and link.get("host"):
                if not link.get("consent") and host.handover_version >= 2:
                    # Begun where the host named no consent: its connection alone never proves the consent it began under.
                    raise SignInError(ACCESS_CHANGED)
                frozen = dict(link)
            elif link:
                # Mail queued on the saved sign-in: only while this very binding continues that mail's own grant, under
                # the consent its transfer receipt named (gmail_host `continues`), never through a fresher binding.
                if not link.get("id") or binding.get("continues") != link.get("id"):
                    raise SignInError(ACCESS_CHANGED)
                if bool(binding.get("continues_consent")) != (host.handover_version >= 2):
                    raise SignInError(ACCESS_CHANGED)
                frozen = {"host": binding.get("connection_id"),
                          **({"consent": binding["continues_consent"]} if binding.get("continues_consent") else {})}
            else:
                frozen = host_link(self.accounts, account)
            if change and not frozen.get("host"):
                raise SignInError(f"This Gmail sign-in names no confirmed connection for a change. Check {CARD}.")
            access, scopes, expiry = host.token(key, rejected=denied[1] if denied and denied[0] == key else "",
                                                connection=frozen.get("host") or "", consent=frozen.get("consent") or "")
            if self._rejected_bearer is denied:
                self._rejected_bearer = None
            if not mail_scope(scopes):
                raise SignInError(f"This Google grant does not include Gmail access. Check {CARD}.")
            current = self.accounts.resolve(self.account_id)
            if current["generation"] != account["generation"]:
                raise SignInError("The mailbox changed while its sign-in was being read; retry the operation.")
            if reusable and expiry is not None:
                # Bounded reuse for batched reads only; writes and explicit tests ask the host again.
                self._read_bearer = (stamp, time.monotonic() + min(5, expiry - time.time() - 1), access, scopes)
        self.account = {**account, "scopes": scopes}
        return access, scopes

    def _series(self):
        active = _READING.get()
        return active[1] if active is not None and active[0] is self else None

    def _access_token(self, *, reusable=False, change=False, link=None):
        # link: the access a queued mail or a change keeps (gmail_host.queued_link, change_link); None for a read.
        account = self.accounts.resolve(self.account_id)
        series = None if change else self._series()  # the reads of one search or poll share one snapshot of the host's list
        owner = credential_owner(self.accounts, account, reading=not change,
                                 **({"read_accounts": series} if series is not None else {}))
        if owner == "host":
            return self._host_credential(account, reusable=reusable, change=change, link=link)[0]
        if link and link.get("host"):
            # Queued under the host's connection: only that connection sends it, so it waits while no host serves it.
            raise AccountError("Waiting for the Google connection this operation began under")
        return self._legacy_token(account, link=str((link or {}).get("id") or ""))

    def _legacy_token(self, account, *, proven=False, link=""):
        # Only called after selection of the local owner. Do not report a 401
        # from this old bearer as a rejection of a host-issued bearer.
        self.account = dict(account)
        transitioned = bool(self.account.pop("gmail_host", None) or account.get("gmail_transfer"))
        self._read_bearer = self._rejected_bearer = None
        # The owner's later local sign-in when it serves (its generation), else the original fields.
        grant, late = local_grant(self.accounts, account)
        if link and link != (account.get("gmail_connection") or {}).get("id"):
            # Begun under another local sign-in of this mailbox (a new one replaced it): never issued through this one.
            raise AccountError(ACCESS_CHANGED)
        tokens, oauth, scopes = grant.get("tokens") or {}, grant.get("oauth") or {}, grant.get("scopes")
        self.account["scopes"] = bearer_scopes(tokens, scopes)
        if _fresh(tokens) and not (proven and self.account["scopes"] is None):
            self._local_bearer = (str(tokens["access_token"]), late)
            return str(tokens["access_token"])
        # One renewal of this sign-in at a time and never beside a transfer reservation of it: the account's own lock,
        # which the OS releases when its holder ends; a stopped holder keeps it, so nothing here replaces its result.
        with self.accounts.account_lock(self.account_id, wait=_RENEWAL_WAIT) as held:
            if not held:
                raise AccountError("Another process is renewing this Google sign-in; retry shortly")
            current = self.accounts.get(self.account_id)
            if current["generation"] != account["generation"] or not current["connected"]:
                raise AccountError("Account changed while its token was refreshing; use the current connection")
            tokens = local_grant(self.accounts, current)[0].get("tokens") or {}  # another renewal may have stored one
            self.account["scopes"] = bearer_scopes(tokens, scopes)
            if _fresh(tokens) and not (proven and self.account["scopes"] is None):
                self._local_bearer = (str(tokens["access_token"]), late)
                return str(tokens["access_token"])
            source = str(tokens.get("refresh_token") or "")
            ceiling = scopes if isinstance(scopes, list) and scopes else None  # the one saved limit, asked for again
            # The connection this renewal started under, named before anything is asked of Google: an unrelated edit
            # of the mailbox while the request is out keeps it, and a consent saved meanwhile starts another one, into
            # which this bearer is never written (AccountStore.refresh_tokens).
            origin = self.accounts.gmail_connection(self.account_id, expected_generation=current["generation"])
            body = {"grant_type": "refresh_token", "refresh_token": source, "client_id": oauth.get("client_id", "")}
            if oauth.get("client_secret"):
                body["client_secret"] = oauth["client_secret"]
            if ceiling:
                body["scope"] = " ".join(ceiling)
            response = self.http.request("POST", "https://oauth2.googleapis.com/token", data=body, timeout=30)
            if response.status_code != 200:
                raise AccountError(f"Google token refresh failed (HTTP {response.status_code}); reconnect this account")
            result = response.json()
            # The permissions Google named for this bearer; an answer naming none grants exactly what was asked
            # (RFC 6749, 5.1), and with nothing asked nothing is known of it. An answer that names no permissions
            # is an empty grant, not an unnamed one: it is refused, never served as unknown.
            asked = "scope" in result
            granted = result["scope"] if asked else (" ".join(ceiling) if ceiling else "")
            if not isinstance(granted, str):
                granted = None  # an unreadable grant names no permissions either
            words = set(granted.split()) if asked or granted else None
            # Within the saved limit and with Gmail access a bearer serves, a narrower one too: only an optional
            # operation it lacks stops. Beyond the limit, without Gmail access or unreadable, it is refused.
            confirmed = granted is not None and (words is None or (
                (ceiling is None or words <= set(ceiling)) and mail_scope(words)))
            updated = {}
            if confirmed and result.get("access_token"):
                updated = {"access_token": result["access_token"],
                           "expires_at": time.time() + int(result.get("expires_in") or 3600),
                           **({"scope": granted, "scope_of": bearer_mark(result["access_token"])} if words else {})}
            if result.get("refresh_token"):
                updated["refresh_token"] = result["refresh_token"]  # Google replaced it: kept even if this bearer is refused
            stored = bool(updated) and self.accounts.refresh_tokens(
                self.account_id, account["generation"], updated, legacy_owner=transitioned, source=source, late=late,
                connection=origin)
        if not confirmed:
            raise SignInError("Google did not confirm the saved permissions; reconnect this account.")
        if not result.get("access_token"):
            raise AccountError("Google returned no refreshed access token")
        if not stored:
            raise AccountError("Account changed while its token was refreshing; use the current connection")
        self.account["scopes"] = bearer_scopes(updated, scopes)
        self._local_bearer = (str(result["access_token"]), late)
        return str(result["access_token"])

    def _request(self, method, path, *, params=None, body=None, access=None, again=True, link=None):
        # link: the access a change began with (change_link), presented when it is issued. A read names none, and a change
        # is never repeated.
        token = access or self._access_token(reusable=method == "GET", change=method != "GET",
                                             link=None if method == "GET" else link)
        managed = self.account.get("gmail_host") or {}
        response = self.http.request(method, BASE + path, params=params, json=body,
                                     headers={"Authorization": "Bearer " + token}, timeout=30)
        if response.status_code == 401:
            self._refused(managed, token)
            if method == "GET" and again:
                # Even a one-off read gets the replacement at once; repeating a read changes nothing in the mailbox.
                return self._request(method, path, params=params, body=body, again=False)
            if method != "GET":
                self._tell_host(managed, token)  # a change is never repeated; the host still hears of its refused bearer
        if response.status_code >= 400:
            reason = ""
            try:
                errors = response.json().get("error", {}).get("errors") or []
                if errors:
                    reason = str(errors[0].get("reason") or "")
            except (ValueError, AttributeError, TypeError):
                pass
            retry_after = response.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdecimal() else None
            raise GmailError(response.status_code, reason, delay)
        return {} if response.status_code == 204 else response.json()

    def _refused(self, managed, token):
        """Gmail refused this bearer: it is never presented again. This client's next request names a host bearer to
        the host (`rejected`), which replaces it; a local one is marked expired. Only `_request` repeats, and only a read."""
        self._read_bearer = None
        local = getattr(self, "_local_bearer", None)
        if managed.get("verified"):
            self._rejected_bearer = (managed["key"], token)
        elif local is not None and local[0] == token:
            self.accounts.expire_access(self.account_id, token, late=local[1])
            self._local_bearer = None

    def _tell_host(self, managed, token):
        """After Gmail refused a change, which is never repeated, tell the host (it keeps the bearers it issued) at once
        that this very bearer was refused, so its next answer is a new one. Unreachable now: the next request names it."""
        if not managed.get("verified"):
            return  # a local bearer is expired by _refused; a client without a registry has nothing to tell
        host = self.accounts.sign_in
        if host is None:
            return
        try:
            host.token(managed["key"], rejected=token)  # no connection or consent: nothing is changed with the answer
        except SignInError:
            return
        if self._rejected_bearer == (managed["key"], token):
            self._rejected_bearer = None

    def _write_permission(self, accepted):
        # Gate optional operations on the actual bearer that will carry the write, never a saved or requested scope.
        if not hasattr(self, "accounts"):  # a stand-in without a registry states its bearer's own permissions
            return bool(set(accepted) & set(self.account.get("scopes") or [])), {}
        account = self.accounts.resolve(self.account_id)
        if credential_owner(self.accounts, account) == "host":
            access, scopes = self._host_credential(account, change=True)
            return bool(set(accepted) & set(scopes)), {"access": access}
        # Every local sign-in, moved or not, has one limit; a bearer with no record of its permissions is renewed for
        # them (within that limit) before it may carry an optional operation, and is never presumed to have them.
        access = self._legacy_token(account, proven=True)
        return bool(set(accepted) & set(self.account.get("scopes") or [])), {"access": access}

    def _pages(self, path, key, *, params=None):
        query = dict(params or {})
        while True:
            page = self._request("GET", path, params=query)
            yield from page.get(key, [])
            token = page.get("nextPageToken")
            if not token:
                return
            query["pageToken"] = token

    @staticmethod
    def native_id(message_ref):
        value = str(message_ref or "")
        if value.startswith("gmail:"):
            value = value[6:]
        if not re.fullmatch(r"[a-fA-F0-9]{1,16}", value):
            raise ValueError("Use the Gmail message_ref returned by email_search or email_read")
        return value.lower()

    @staticmethod
    def _uid(value):
        # X-GM-MSGID is the same unsigned64 identity as Gmail's hex id. SQLite
        # uses signed64; this bijection keeps the existing inbox schema intact.
        number = int(value, 16)
        return number if number < 2 ** 63 else number - 2 ** 64

    def _message(self, payload, *, headers_only=False, include_attachment_data=False):
        native = self.native_id(payload["id"])
        if headers_only:
            fields = (payload.get("payload") or {}).get("headers") or []
            # Metadata is only a search projection. A provider value cannot inject a header boundary.
            fields = [(str(item['name']), re.sub(r'[\r\n]+', ' ', str(item['value']))) for item in fields]
            raw = ("\r\n".join(f"{name}: {value}" for name, value in fields
                                  if re.fullmatch(r"[!-9;-~]+", name)) + "\r\n\r\n").encode("utf-8")
        else:
            encoded = payload.get("raw") or ""
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        message = parse_message(raw, folder="Gmail", uid=self._uid(native), include_attachment_data=include_attachment_data,
                                missing_id="" if headers_only else None, invalid_id=f"<gmail-{native}@ouroboros.local>")
        labels = list(payload.get("labelIds") or [])
        message.update(uidvalidity=1, internal_date=int(payload.get("internalDate") or 0) / 1000,
                       provider_id=native, provider_thread_id=str(payload.get("threadId") or ""),
                       provider_event_key="gmail:" + native, message_ref="gmail:" + native,
                       labels=labels, flags=[] if "UNREAD" in labels else ["\\Seen"])
        if headers_only:
            message.pop("body", None)
            message.pop("_raw_source", None)
        return message

    @_in_series
    def search(self, *, folder="INBOX", criteria=None, query="", limit=50, page_token=""):
        if criteria:
            # Existing callers may still use the common unqualified search.
            if criteria != ["ALL"]:
                raise ValueError("For Gmail use query with Gmail search syntax; IMAP criteria remain available on IMAP accounts")
        count = max(1, min(200, int(limit)))
        params = {"maxResults": count}
        label = self._label_id(folder) if folder and folder != "ALL" else ""
        if label and label != "DRAFT":
            params["labelIds"] = label
        if label in _SPAM_TRASH:
            params["includeSpamTrash"] = "true"  # On every page; otherwise this explicit folder lists nothing.
        if query:
            params["q"] = str(query)
        if page_token:
            params["pageToken"] = str(page_token)
        listed = self._request("GET", "/drafts" if label == "DRAFT" else "/messages", params=params)
        messages = []
        for row in listed.get("drafts" if label == "DRAFT" else "messages", []):
            native = row["message"]["id"] if label == "DRAFT" else row["id"]
            message = self._message(self._request("GET", "/messages/" + self.native_id(native),
                                                  params={"format": "metadata"}), headers_only=True)
            if label == "DRAFT":
                message["draft_id"] = row["id"]
            messages.append(message)
        return {"folder": folder, "messages": messages, "result_size_estimate": listed.get("resultSizeEstimate"),
                "next_page_token": listed.get("nextPageToken", ""),
                "search_note": "Gmail API search does not expand aliases or search entire threads like the Gmail website."}

    def read(self, *, message_ref="", uid=None, uidvalidity=None, folder=None, include_attachment_data=False):
        if not message_ref:
            if uid is None or uidvalidity != 1:
                raise ValueError("Use message_ref from email_search")
            message_ref = format(int(uid) % (2 ** 64), "x")
        native = self.native_id(message_ref)
        return self._message(self._request("GET", "/messages/" + native, params={"format": "raw"}),
                             include_attachment_data=include_attachment_data)

    @_in_series
    def find_message(self, message_id, *, folder="ALL"):
        """Read all search candidates; decoded metadata cannot establish a reply's identity."""
        from .mime import canonical_message_id, unique_reply_source
        token = canonical_message_id(message_id)
        matches, page = [], ''
        while True:
            found = self.search(folder=folder, query="rfc822msgid:" + token, limit=50,
                                **({'page_token': page} if page else {}))
            matches.extend(self.read(message_ref=row['message_ref']) for row in found['messages'])
            page = found.get('next_page_token')
            if not page:
                return unique_reply_source(matches, token)

    def _label_id(self, value, *, snapshot=None):
        if value in _SYSTEM_LABEL_IDS:
            return value
        labels = snapshot() if snapshot else self._request("GET", "/labels").get("labels", [])
        by_id = [label["id"] for label in labels if label["id"] == value]
        if by_id:
            return by_id[0]
        matches = [label["id"] for label in labels if label.get("name") == value]
        if len(matches) != 1:
            raise ValueError("Choose an exact label id or unique name from email_folders")
        return matches[0]

    def _current_label_names(self, affected, snapshot=None):
        """Names are alternate Host targets; the Gmail request still uses the caller's exact IDs."""
        custom = set(affected) - _SYSTEM_LABEL_IDS
        if not custom:
            return set()
        labels = snapshot() if snapshot else self._request("GET", "/labels").get("labels", [])
        by_id = {row["id"]: row.get("name") for row in labels}
        if any(not isinstance(by_id.get(key), str) or not by_id[key].strip() for key in custom):
            raise ValueError("Cannot verify the current Gmail label IDs and their names; refresh the message or label selection and retry")
        return {by_id[key] for key in custom if by_id[key] != key}

    def message_state(self, native, outcome):
        """Read the selected message's current labels and RFC conversation before preparing a write."""
        message = self._request("GET", "/messages/" + native,
                                params={"format": "metadata", "fields": "id,labelIds,payload/headers"})
        if not isinstance(message, dict) or message.get("id") != native:
            raise ValueError(f"Gmail did not confirm the selected message; nothing was {outcome}")
        return self._state(message, outcome)

    def _state(self, message, outcome):
        """The same authority facts for a messages.get or drafts.get message resource."""
        labels = message.get("labelIds", [])
        if not isinstance(labels, list) or any(not isinstance(value, str) or not value.strip() for value in labels):
            raise ValueError(f"Gmail did not return the message's label IDs; nothing was {outcome}")
        return sorted(set(labels)), self._identity(message, outcome)

    def _identity(self, metadata, outcome):
        """Use the raw source and intake parser; decoded metadata cannot preserve wire identity."""
        native = self.native_id(metadata["id"])
        payload = self._request("GET", "/messages/" + native, params={"format": "raw"})
        if not isinstance(payload, dict) or payload.get("id") != metadata["id"] or not payload.get("raw"):
            raise ValueError(f"Gmail did not return the selected message's source; nothing was {outcome}")
        full = self._message(payload)
        if check := getattr(self, 'retained_source_check', None):
            check(full)
        return conversation_root(references=full["references"], in_reply_to=full["in_reply_to"],
                                 message_id=full["message_id"])

    def draft_state(self, draft_id, outcome):
        """An existing draft's current label IDs and RFC conversation, read now from its underlying message.

        Replacing or deleting the draft removes that message from every label it bears and from its conversation.
        The conversation is the RFC root intake uses: References, else In-Reply-To, else the draft's own
        Message-ID. ``outcome`` names what did not happen on failure."""
        draft = self._request("GET", "/drafts/" + _draft_id(draft_id), params={"format": "metadata"})
        message = draft.get("message") if isinstance(draft, dict) else None
        if (not isinstance(message, dict) or draft.get("id") != draft_id or not isinstance(message.get("id"), str)
                or not re.fullmatch(r"[a-fA-F0-9]{1,16}", message["id"])):
            raise ValueError(f"Gmail did not confirm the selected draft; nothing was {outcome}")
        return self._state(message, outcome)

    def draft_targets(self, draft_id):
        """Label IDs and their current names (Drafts for DRAFT), and the RFC conversation, that replacing the draft
        changes. They are Host targets only and never alter the saved draft."""
        labels, root = self.draft_state(draft_id, "saved")
        names = {*labels, "Drafts", *self._current_label_names(labels)} - {"DRAFT"}
        return ["Drafts", *sorted(names - {"Drafts"})], root

    def mark_read(self, *, message_ref="", uid=None, uidvalidity=None, folder=None, message_conversation_id="",
                  message_thread_id="", label_names=None, prepare_targets=None):
        return self.mailbox(action="flags", message_ref=message_ref, uid=uid, uidvalidity=uidvalidity,
                            flags=["\\Seen"], label_names=label_names, message_conversation_id=message_conversation_id,
                            message_thread_id=message_thread_id, prepare_targets=prepare_targets)

    def mailbox(self, *, action="list", folder="INBOX", uid=None, uidvalidity=None, message_ref="",
                destination="", flags=None, remove_flags=False, add_labels=None, remove_labels=None,
                label_id="", properties=None, draft_id="", label_names=None, draft_conversation_id="",
                message_conversation_id="", message_thread_id="", prepare_targets=None):
        # One fresh snapshot per operation. Never cache a mutable display name
        # across calls or translate the provider's add/remove ID arrays as names.
        @cache
        def snapshot():
            return self._request("GET", "/labels").get("labels", [])

        @cache
        def state():
            return self.message_state(native, "deleted" if action == "delete" else "changed")

        # A change is made under the access it began with: named here, before the labels or the message are read to
        # prepare it, and presented again when it is issued. Permanent deletion and settings take their one bearer first
        # (_write_permission) and are not checked again after the preparation.
        link = self.change_link() if action in _CHANGES else None

        def write(method, path, body, targets, affected=(), fixed_names=(), *, auth=None):
            if prepare_targets:
                names = set(fixed_names)
                if path.startswith("/messages/"):
                    labels, root = state()
                    # Every message mutation changes this conversation and the message visible in these folders.
                    # These are authority facts only; never add them to the provider's mutation body.
                    names.update(set(labels) - set(targets.get("add_labels", add_labels or ()))
                                 - set(targets.get("remove_labels", remove_labels or ())))
                    if "DRAFT" in labels:
                        names.add("Drafts")
                    affected = {*affected, *labels}
                    # The conversation is a target; the same root as a separate thread argument lets the Host
                    # find a Place stored with both (the manifest's thread selector), without hiding label targets.
                    targets["message_conversation_id"] = targets["message_thread_id"] = root
                elif "message_thread_id" not in targets:
                    if message_conversation_id:
                        targets["message_conversation_id"] = ""
                    if message_thread_id:
                        targets["message_thread_id"] = ""
                names.update(self._current_label_names(affected, snapshot))
                if names or label_names:
                    targets["label_names"] = sorted(names)
            prepared = prepare_targets(targets) if prepare_targets else None
            issue = auth or ({"link": link} if link else {})
            return prepared if prepared is not None else self._request(method, path, body=body, **issue)

        if action == "list":
            return {"folders": self._request("GET", "/labels").get("labels", [])}
        if action == "create":
            body = {"name": str(destination), **(properties or {})}
            if not isinstance(body["name"], str):
                raise ValueError("The label name must be a string")
            return write("POST", "/labels", body, {"destination": body["name"]})
        if action in {"update_label", "delete_label"}:
            selected = self._label_id(label_id or destination, snapshot=snapshot)
            targets = {"label_id" if label_id else "destination": selected}
            if action == "update_label" and (properties or {}).get("name"):
                if not isinstance(properties["name"], str):
                    raise ValueError("The label name must be a string")
                targets = {"label_id": selected, "destination": properties["name"]}
            return write("PATCH" if action == "update_label" else "DELETE", "/labels/" + selected,
                         properties if action == "update_label" else None, targets, [selected])
        if action == "delete_draft":
            _draft_id(draft_id)
            targets, labels = {"remove_labels": ["DRAFT"]}, ["DRAFT"]
            if prepare_targets:
                # Deleting a draft permanently deletes its message, from every label it bears and from its RFC
                # conversation. Both are read now and exposed; any other caller target is cleared, not trusted.
                labels, root = self.draft_state(draft_id, "deleted")
                targets = {"remove_labels": labels or ["DRAFT"]}
                for key, value, empty in (("add_labels", add_labels, []), ("label_id", label_id, ""),
                                          ("destination", destination, "")):
                    if value:
                        targets[key] = empty
                targets["draft_conversation_id"] = targets["message_thread_id"] = root
                if message_conversation_id:
                    targets["message_conversation_id"] = ""
            return write("DELETE", "/drafts/" + draft_id, None, targets, labels, fixed_names=["Drafts"])
        native = self.native_id(message_ref or (format(int(uid) % (2 ** 64), "x") if uid is not None and uidvalidity == 1 else ""))
        if action in {"trash", "untrash"}:
            # Moving into or out of Trash changes where the message appears under every label it bears, so the
            # Host sees its current labels as well as TRASH. Any other caller target is cleared, not trusted.
            labels, _root = state()
            kept = sorted(set(labels) - {"TRASH"})
            if action == "trash":
                targets = {"remove_labels": kept, "add_labels": ["TRASH"]}
            else:
                targets = {"remove_labels": ["TRASH"], "add_labels": kept}
            targets = {key: value for key, value in targets.items()
                       if value or (add_labels if key == "add_labels" else remove_labels)}
            if label_id:
                targets["label_id"] = ""
            if destination:
                targets["destination"] = ""
            return write("POST", f"/messages/{native}/{action}", {}, targets, [*kept, "TRASH"],
                         ["TRASH", *(["Drafts"] if "DRAFT" in labels else [])])
        if action == "delete":
            allowed, auth = self._write_permission((FULL_MAIL_SCOPE,))
            if not allowed:
                raise AccountError("Permanent deletion requires a new Google consent with the full mail scope; trash is available with gmail.modify")
            path = "/messages/" + native
            labels, _root = state()
            targets = {"remove_labels": labels} if labels or remove_labels else {}
            if add_labels:
                targets["add_labels"] = []
            if label_id:
                targets["label_id"] = ""
            if destination:
                targets["destination"] = ""
            return write("DELETE", path, None, targets, labels, ["Drafts"] if "DRAFT" in labels else [], auth=auth)
        for labels in (add_labels, remove_labels):
            if labels is not None and (not isinstance(labels, (list, tuple))
                                      or any(not isinstance(label, str) or not label.strip() for label in labels)):
                raise ValueError("add_labels and remove_labels must be lists of Gmail label IDs")
        add, remove = list(add_labels or []), list(remove_labels or [])
        targets = {}
        if action in {"copy", "move"}:
            targets["destination"] = self._label_id(destination, snapshot=snapshot)
            add.append(targets["destination"])
            if action == "move" and folder and folder != "ALL":
                targets["folder"] = self._label_id(folder, snapshot=snapshot)
                remove.append(targets["folder"])
        elif action == "archive":
            remove.append("INBOX")
        elif action == "flags":
            mapping = {"\\Seen": ("UNREAD", True), "\\Flagged": ("STARRED", False), "\\Deleted": ("TRASH", False)}
            for flag in flags or []:
                if flag not in mapping:
                    raise ValueError("Gmail flags support Seen, Flagged and Deleted; use labels for other tags")
                label, inverse = mapping[flag]
                (remove if bool(remove_flags) != inverse else add).append(label)
        elif action != "labels":
            raise ValueError("Unsupported Gmail mailbox action")
        body = {"addLabelIds": list(dict.fromkeys(add)), "removeLabelIds": list(dict.fromkeys(remove))}
        if action == "flags" and set(add) - set(add_labels or []):
            targets["add_labels"] = body["addLabelIds"]
        if set(remove) - set(remove_labels or []):
            targets["remove_labels"] = body["removeLabelIds"]
        affected, fixed = [*add, *remove], []
        return write("POST", "/messages/" + native + "/modify", body, targets, affected, fixed)

    def draft(self, *, to, subject, body="", folder="DRAFT", reply_to_message_id="", references=None,
              cc=(), bcc=(), html_body="", body_type="plain", attachments=(), sender="", draft_id="", thread_id="",
              link=None):
        if folder not in {"DRAFT", "Drafts", ""}:
            raise ValueError("Gmail drafts live in DRAFT; apply additional labels separately with email_mailbox")
        message = build_message(sender=sender or self.settings["EMAIL_USER"], recipients=mailbox_addresses(to), subject=subject, body=body,
                                in_reply_to=reply_to_message_id, references=references or [], cc=mailbox_addresses(cc), bcc=mailbox_addresses(bcc),
                                html_body=html_body, body_type=body_type, attachments=attachments, include_bcc_header=True)
        payload = {"message": {"raw": base64.urlsafe_b64encode(message.as_bytes(policy=SMTP)).decode().rstrip("=")}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        issue = {"link": link} if link else {}  # the access the change began under (change_link), if the caller named it
        if draft_id:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", str(draft_id)):
                raise ValueError("Invalid draft id")
            result = self._request("PUT", "/drafts/" + str(draft_id), body=payload, **issue)
        else:
            result = self._request("POST", "/drafts", body=payload, **issue)
        return {"ok": True, "draft_id": result["id"], "message_ref": "gmail:" + result["message"]["id"],
                "message_id": str(message["Message-ID"])}

    def settings_read(self, *, resource="sendAs", identifier=""):
        paths = {"sendAs": "/settings/sendAs", "filters": "/settings/filters", "vacation": "/settings/vacation",
                 "forwardingAddresses": "/settings/forwardingAddresses", "autoForwarding": "/settings/autoForwarding",
                 "imap": "/settings/imap", "pop": "/settings/pop", "language": "/settings/language"}
        if resource not in paths:
            raise ValueError("Unsupported Gmail settings resource")
        path = paths[resource]
        if identifier:
            from urllib.parse import quote
            path += "/" + quote(str(identifier), safe="")
        return self._request("GET", path)

    def settings_write(self, *, resource, action="update", identifier="", values=None, forward_to="", prepare_targets=None):
        from urllib.parse import quote
        if values is not None and not isinstance(values, Mapping):
            raise ValueError("values must be an object")
        if resource in {"autoForwarding", "forwardingAddresses"}:
            return {"ok": False, "code": "website_required", "message": "Changing forwarding requires Workspace administrative delegation; use Gmail settings for a personal account."}
        paths = {"sendAs": "/settings/sendAs", "filters": "/settings/filters", "vacation": "/settings/vacation",
                 "imap": "/settings/imap", "pop": "/settings/pop", "language": "/settings/language"}
        if resource not in paths or action not in {"create", "update", "delete"}:
            raise ValueError("Unsupported Gmail settings operation")
        path = paths[resource] + (("/" + quote(str(identifier), safe="")) if identifier else "")
        if resource == "sendAs" and action != "update":
            return {"ok": False, "code": "website_required", "message": "Additional send-as administration may require Workspace delegation; manage it on Gmail's website."}
        if resource == "filters" and action == "update":
            raise ValueError("Gmail filters are created or deleted, not updated")
        if action == "delete" and not identifier:
            raise ValueError("identifier is required for delete")
        allowed, auth = self._write_permission((SETTINGS_SCOPE, FULL_MAIL_SCOPE))
        if not allowed:
            return {"ok": False, "code": "settings_permission_missing",
                    "message": "This mailbox was connected without permission to manage Gmail settings; change them on "
                               "Gmail's website, or reconnect Google with that choice ticked if the administrator allows it."}
        body = None if action == "delete" else dict(values or {})
        targets = {}
        if body:
            # The manifest declares values.replyToAddress, so the Host reads the exact field Gmail receives.
            # Gmail's JSON also accepts other spellings of that field; refuse them rather than hide the address.
            if any(not isinstance(key, str) or (key != "replyToAddress"
                                                and key.replace("_", "").casefold() == "replytoaddress") for key in body):
                raise ValueError("Use values.replyToAddress for the Reply-To address; the Host checks that exact field")
            reply_to = body.get("replyToAddress", "")
            if not isinstance(reply_to, str):
                raise ValueError("values.replyToAddress must be a string: one bare email address, or empty to remove Reply-To")
            if reply_to.strip():
                try:
                    parsed = mailbox_addresses(reply_to)
                except ValueError:
                    parsed = []
                if parsed != [reply_to.strip()] or parseaddr(parsed[0])[1] != parsed[0]:
                    raise ValueError("values.replyToAddress must be one bare email address such as name@example.com, "
                                     "or empty to remove Reply-To")
        if resource == "filters" and action == "create":
            if identifier:
                raise ValueError("Creating a Gmail filter targets the filter collection; omit identifier")
            if any(key.lower() == "action" and key != "action" for key in body):
                raise ValueError("Use the Gmail filter's documented action field")
            rule = body.get("action", {})
            if not isinstance(rule, Mapping) or rule.keys() - {"forward", "addLabelIds", "removeLabelIds"}:
                raise ValueError("Gmail filter action must be an object with forward, addLabelIds or removeLabelIds")
            if not isinstance(rule.get("forward", ""), str) or not isinstance(forward_to, str):
                raise ValueError("Gmail filter forwarding destination must be a string")
            forward = rule.get("forward") or forward_to
            if forward and not rule.get("forward"):
                body["action"] = {**rule, "forward": forward}
            targets["forward_to"] = forward
            affected = []
            for key, wire in (("add_labels", "addLabelIds"), ("remove_labels", "removeLabelIds")):
                ids = rule.get(wire, [])
                if not isinstance(ids, list) or any(not isinstance(i, str) or not i.strip() for i in ids):
                    raise ValueError(f"{wire} must be a list of Gmail label IDs")
                targets[key] = ids
                affected.extend(ids)
            if prepare_targets:
                targets["label_names"] = sorted(self._current_label_names(affected))
        if prepare_targets and (needed := prepare_targets(targets)):
            return needed
        return self._request({"create": "POST", "update": "PUT", "delete": "DELETE"}[action], path, body=body, **auth)

    @contextmanager
    def delivery(self, item=None):
        # Authentication refresh happens before the outbox enters uncertain-send custody. A queued mail goes out under the
        # access named for this pass (EmailRuntime.process_outbox: whichever confirmed sign-in of this mailbox's own Google
        # account serves it now), and the host refuses any other connection or consent when its bearer is issued. A host
        # that cannot give it now (HostNotReached and any other SignInError) leaves the mail waiting, unsent: the outbox
        # holds it as it holds a signed-out mailbox's mail, and it goes once the host answers again.
        link = item.reporting.get("gmail_connection") if item is not None else None
        if item is not None and not (isinstance(link, Mapping) and (link.get("host") or link.get("id"))):
            link = self.sending_link()
        link = link if isinstance(link, Mapping) else None
        self._delivery_access = self._access_token(change=True, link=link)
        try:
            draft = item.reporting.get("gmail_draft") if item is not None else None
            if isinstance(draft, Mapping):
                # Before its sending begins (EmailRuntime marks it sending only once this is done): a read that fails
                # leaves it queued, never of unknown outcome; a draft gone or changed since its check is refused.
                self._check_draft(draft, link)
            yield self
        finally:
            self._delivery_access = ""
            self._checked_draft = None

    def connection_link(self):
        """The access a mail queued now is recorded with (gmail_host.queued_link)."""
        return queued_link(self.accounts, self.accounts.resolve(self.account_id))

    def sending_link(self):
        """The access a queued mail of this mailbox goes out under now: a confirmed sign-in of this mailbox's own Google
        account (its verified subject, never an address), whichever connection and consent serve it now
        (gmail_host.sending_link). SignInError or AccountError while none does: the mail waits, unsent."""
        return sending_link(self.accounts, self.accounts.resolve(self.account_id))

    def change_link(self):
        """The access an operation that is beginning now is made under: the one a mail queued now keeps. It is named before
        anything is read to prepare the operation and presented again where the operation is issued, and the host refuses
        any other connection or consent there. None for a stand-in with no account registry."""
        return self.connection_link() if hasattr(self, "accounts") else None

    def intake_link(self):
        """The access an event beginning now is recorded under, or None when this mailbox is confirmed readable but nothing
        confirms a right to send (gmail_host.queued_link, for_intake). Anything unread, incomplete or unconfirmed raises:
        the event has not begun, and its claim is retried before anything is submitted."""
        return queued_link(self.accounts, self.accounts.resolve(self.account_id), for_intake=True)

    def find_submitted(self, item):
        """What this mailbox holds of a queued mail whose submission began and whose outcome is unknown, searched by the
        Message-ID it went out with: ("sent", receipt) when a message of this mailbox's Sent carries it, ("draft", draft)
        when a draft does (the draft it was sent from, or one saved for it before), else (None, None).

        Gmail search can lag a moment behind a send and Gmail may give a sent message a Message-ID of its own, so an
        empty answer never proves that nothing went out: the store waits before this is asked (EmailStore.RECONCILE_AFTER),
        and the draft saved then asks the owner to check before sending it."""
        token = canonical_message_id(item.message_id)
        if token:
            # Only this mailbox's own sent message shows it went out (SENT, also once moved to Trash); a copy that came in
            # with the same ID (Inbox, Spam) proves nothing. The search asks for sent copies and reads every page of its
            # answer, so copies that came in never hide the sent one; each found is still checked for SENT.
            found = self._pages("/messages", "messages", params={"q": "rfc822msgid:" + token, "labelIds": "SENT",
                                                                 "includeSpamTrash": "true", "maxResults": 10})
            for row in found:
                native = self.native_id(row["id"])
                labels = self._request("GET", "/messages/" + native, params={"format": "minimal"}).get("labelIds") or []
                if "SENT" in labels:
                    return "sent", {"transport": "gmail_api", "message_id": native,
                                    "thread_id": str(row.get("threadId") or ""), "found": "rfc822msgid"}
        sent_from = item.reporting.get("gmail_draft")
        if isinstance(sent_from, Mapping) and sent_from.get("id"):
            try:  # the draft it was to be sent from, while it still exists
                draft = self._request("GET", "/drafts/" + _draft_id(sent_from["id"]), params={"format": "minimal"})
            except GmailError as exc:
                if exc.status != 404:
                    raise
            else:
                return "draft", {"id": draft["id"], "message_ref": "gmail:" + self.native_id(draft["message"]["id"]),
                                 "how": "found"}
        if token:
            drafts = self._request("GET", "/drafts", params={"q": "rfc822msgid:" + token, "maxResults": 10})
            for draft in drafts.get("drafts", []):
                return "draft", {"id": draft["id"], "message_ref": "gmail:" + self.native_id(draft["message"]["id"]),
                                 "how": "found"}
        return None, None

    def save_unsent(self, item, link):
        """One Gmail draft, in this same mailbox, of a queued mail whose outcome is unknown and that it does not hold
        (find_submitted): its exact envelope (Bcc kept in the draft's header, as a draft keeps it), body, attachments,
        thread and Message-ID, so the owner can check and send it from any device and a later search finds it. A mail sent
        from an existing draft is put back as that draft was queued (_queued_draft)."""
        if isinstance(item.reporting.get("gmail_draft"), Mapping):
            raw = self._queued_draft(item)
        else:
            message = copy.deepcopy(self.message(item))
            if item.bcc:
                message["Bcc"] = ", ".join(item.bcc)
            raw = message.as_bytes(policy=SMTP)
        payload = {"message": {"raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")}}
        if item.reporting.get("gmail_thread_id"):
            payload["message"]["threadId"] = item.reporting["gmail_thread_id"]
        result = self._request("POST", "/drafts", body=payload, **({"link": link} if link else {}))
        return {"id": result["id"], "message_ref": "gmail:" + self.native_id(result["message"]["id"]), "how": "created"}

    @staticmethod
    def _queued_draft(item):
        """The source of the draft a mail was sent from, exactly as it was read when it was queued (EmailStore.stage_draft):
        its HTML, files and every header as they were. One without a Message-ID gets the one the mail was queued with, on
        a line of its own, so a later search finds what is put back. ValueError when that source was not kept whole."""
        source = item.reporting["gmail_draft"].get("source")
        if not isinstance(source, Mapping) or not isinstance(source.get("path"), str) or not source["path"]:
            raise ValueError("The draft this mail was sent from was not kept as it was queued")
        raw = Path(source["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source.get("sha256"):
            raise ValueError("The kept source of the draft this mail was sent from is not the one queued")
        if email.message_from_bytes(raw, policy=email.policy.compat32).get("Message-ID") is None:
            newline = b"\r\n" if raw.split(b"\n", 1)[0].endswith(b"\r") else b"\n"
            raw = b"Message-ID: " + item.message_id.encode("ascii") + newline + raw
        return raw

    def draft_letter(self, draft_id):
        """An existing draft as it is now, for email_send(draft_id): its raw source read with the intake parser (its files'
        bytes included, and the source itself as `_raw_source`, for EmailStore.stage_draft), its Bcc, Gmail thread and a
        digest of the bytes, so the send that follows names exactly the draft that was checked."""
        draft = self._request("GET", "/drafts/" + _draft_id(draft_id), params={"format": "raw"})
        message = draft.get("message") if isinstance(draft, dict) else None
        if (not isinstance(message, dict) or draft.get("id") != draft_id or not isinstance(message.get("raw"), str)
                or not message["raw"]):
            raise ValueError("Gmail did not return the selected draft; nothing was queued")
        raw = base64.urlsafe_b64decode(message["raw"] + "=" * (-len(message["raw"]) % 4))
        letter = parse_message(raw, folder="Gmail", uid=0, max_body_chars=None, missing_id="",
                               include_attachment_data=True)
        source = email.message_from_bytes(raw, policy=email.policy.default)
        letter.update(bcc=_addresses(source.get("Bcc")), digest=hashlib.sha256(message["raw"].encode()).hexdigest(),
                      provider_thread_id=str(message.get("threadId") or ""))
        return letter

    def _check_draft(self, draft, link=None):
        """Read the draft a mail sent by draft_id names, with the access it is sent under: refused (DeliveryRejected,
        nothing was sent) when it is gone or changed since it was checked. A bearer Gmail refuses for this read is replaced
        once by the next bearer of the same sign-in (the same `link`), and the send presents that one. A read Gmail did
        not answer, or answered busy or failing (408, 429, 5xx), is DeliveryDeferred: nothing was sent and nothing is
        known about the draft, so the mail waits, with Gmail's Retry-After when it named one."""
        path = "/drafts/" + _draft_id(draft["id"])
        try:
            try:
                current = self._request("GET", path, params={"format": "raw"}, access=self._delivery_access, again=False)
            except GmailError as exc:
                if exc.status != 401:
                    raise
                self._delivery_access = self._access_token(change=True, link=link)  # the refused one is never presented
                current = self._request("GET", path, params={"format": "raw"}, access=self._delivery_access, again=False)
        except GmailError as exc:
            if exc.status == 404:
                raise DeliveryRejected("The draft no longer exists; nothing was sent") from exc
            if exc.status in (408, 429) or exc.status >= 500:
                raise DeliveryDeferred(f"Gmail could not show the draft now ({exc}); nothing was sent",
                                       retry_after=exc.retry_after) from exc
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise DeliveryDeferred(f"Gmail did not answer when the draft was read ({type(exc).__name__}); "
                                   "nothing was sent") from exc
        raw = ((current.get("message") or {}) if isinstance(current, dict) else {}).get("raw")
        if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != draft.get("digest"):
            raise DeliveryRejected("The draft changed after it was checked; nothing was sent. Check it and send it again")
        self._checked_draft = draft["id"]

    def _send_draft(self, draft):
        """Send one existing draft (users.drafts.send) only while it is still the very draft that was checked: delivery()
        read it before the send began; a caller outside it has it read here."""
        if getattr(self, "_checked_draft", None) != draft.get("id"):
            self._check_draft(draft)
        return self._request("POST", "/drafts/send", body={"id": draft["id"]}, access=self._delivery_access)

    def transmit(self, _session, message, recipients, *, has_bcc=False, reporting=None):
        wire = copy.deepcopy(message)
        if has_bcc:
            visible = {address.casefold() for _, address in getaddresses([str(wire.get("To") or ""), str(wire.get("Cc") or "")])}
            bcc = [address for address in recipients if address.casefold() not in visible]
            if bcc:
                wire["Bcc"] = ", ".join(bcc)
        payload = {"raw": base64.urlsafe_b64encode(wire.as_bytes(policy=SMTP)).decode().rstrip("=")}
        if (reporting or {}).get("gmail_thread_id"):
            payload["threadId"] = reporting["gmail_thread_id"]
        draft = (reporting or {}).get("gmail_draft")
        try:
            result = (self._send_draft(draft) if isinstance(draft, Mapping) else
                      self._request("POST", "/messages/send", body=payload, access=self._delivery_access))
        except GmailError as exc:
            if 400 <= exc.status < 500 and exc.status != 408:
                raise DeliveryRejected(exc, retryable=exc.status == 429, retry_after=exc.retry_after) from exc
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # The request never left this computer, so no send is uncertain. A lost answer after it left stays uncertain.
            raise DeliveryRejected(f"Gmail could not be reached ({type(exc).__name__}); nothing was sent",
                                   retryable=True, retry_after=30) from exc
        if not result.get("id"):
            raise RuntimeError("Gmail send response omitted its message id; inspect Sent before sending again")
        return {}, {"transport": "gmail_api", "message_id": str(result.get("id") or ""),
                    "thread_id": str(result.get("threadId") or "")}

    @_in_series
    def poll(self, store, *, folder="INBOX", accepts=None):
        key = "gmail_cursor"
        cursor = store.get(key)
        if cursor is None:
            activated_at = time.time()
            profile = self._request("GET", "/profile")
            store.put(key, {"history_id": str(profile["historyId"]), "activated_at": activated_at})
            store.put("poll_health", {"ok": True, "checked_at": time.time(), "new_messages": 0})
            return 0
        params = {"startHistoryId": cursor["history_id"], "historyTypes": "messageAdded", "maxResults": 100}
        ids, newest, reset = [], cursor["history_id"], False
        try:
            while True:
                page = self._request("GET", "/history", params=params)
                for event in page.get("history", []):
                    ids.extend(row["message"]["id"] for row in event.get("messagesAdded", []) if row.get("message", {}).get("id"))
                newest = str(page.get("historyId") or newest)
                if not page.get("nextPageToken"):
                    break
                params["pageToken"] = page["nextPageToken"]
        except GmailError as exc:
            if exc.status != 404:
                raise
            reset = True
            # Pin the new history boundary before enumeration; later changes are
            # replayed from it, and store identity deduplicates any overlap.
            newest = str(self._request("GET", "/profile")["historyId"])
            rescan = {"q": "after:" + str(int(cursor["activated_at"])), "maxResults": 100}
            if folder in _SPAM_TRASH:  # An intake folder of Spam or Trash is rescanned there too.
                rescan["includeSpamTrash"] = "true"
            ids = [item["id"] for item in self._pages("/messages", "messages", params=rescan)]
        count = 0
        label = self._label_id(folder) if folder and folder != "ALL" else ""
        for native in dict.fromkeys(ids):
            try:
                message = self.read(message_ref="gmail:" + native, include_attachment_data=True)
            except GmailError as exc:
                if exc.status == 404:
                    continue  # It was deleted before it could be captured.
                raise
            if label and label not in message["labels"]:
                continue
            if message["internal_date"] < cursor["activated_at"]:
                continue
            if accepts is not None and not accepts(message):
                continue
            _, inserted = store.ingest(message)
            count += int(inserted)
        store.put(key, {**cursor, "history_id": newest})
        store.put("poll_health", {"ok": True, "checked_at": time.time(), "new_messages": count, "history_reset": reset})
        return count

    def test_connection(self):
        profile = self._request("GET", "/profile", access=self._access_token())
        return {"ok": True, "provider": "gmail", "address": profile.get("emailAddress"),
                "scopes": self.account.get("scopes"), "scope_status": "known" if self.account.get("scopes") is not None else "unknown"}
