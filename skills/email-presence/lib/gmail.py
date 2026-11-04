"""Gmail resources behind the mail skill's existing MIME and delivery lifecycle."""
from __future__ import annotations

import base64
import copy
import re
import time
from contextlib import contextmanager
from functools import cache
from email.policy import SMTP
from email.utils import getaddresses

import httpx

from .accounts import AccountError
from .client import MailClient
from .delivery import DeliveryRejected
from .mime import build_message, mailbox_addresses, parse_message

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_SYSTEM_LABEL_IDS = frozenset({"INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "STARRED", "UNREAD", "IMPORTANT"})


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

    def close(self):
        if self._owns_http:
            self.http.close()

    def _access_token(self):
        self.account = self.accounts.resolve(self.account_id)
        tokens = self.account.get("tokens") or {}
        if tokens.get("access_token") and float(tokens.get("expires_at") or 0) > time.time() + 60:
            return str(tokens["access_token"])
        oauth = self.account.get("oauth") or {}
        body = {"grant_type": "refresh_token", "refresh_token": tokens.get("refresh_token", ""),
                "client_id": oauth.get("client_id", "")}
        if oauth.get("client_secret"):
            body["client_secret"] = oauth["client_secret"]
        response = self.http.request("POST", "https://oauth2.googleapis.com/token", data=body, timeout=30)
        if response.status_code != 200:
            raise AccountError(f"Google token refresh failed (HTTP {response.status_code}); reconnect this account")
        result = response.json()
        if not result.get("access_token"):
            raise AccountError("Google returned no refreshed access token")
        updated = {"access_token": result["access_token"], "expires_at": time.time() + int(result.get("expires_in") or 3600)}
        if result.get("refresh_token"):
            updated["refresh_token"] = result["refresh_token"]
        if not self.accounts.refresh_tokens(self.account_id, self.account["generation"], updated):
            raise AccountError("Account changed while its token was refreshing; use the current connection")
        return str(result["access_token"])

    def _request(self, method, path, *, params=None, body=None, access=None):
        token = access or self._access_token()
        response = self.http.request(method, BASE + path, params=params, json=body,
                                     headers={"Authorization": "Bearer " + token}, timeout=30)
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
            raw = ("\r\n".join(f"{item['name']}: {item['value']}" for item in fields) + "\r\n\r\n").encode("utf-8")
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

    def find_message(self, message_id, *, folder="ALL"):
        found = self.search(folder=folder, query="rfc822msgid:" + str(message_id), limit=1)
        return self.read(message_ref=found["messages"][0]["message_ref"]) if found["messages"] else None

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
        by_id = {row["id"]: row["name"] for row in labels}
        if not custom <= by_id.keys():
            raise ValueError("Choose current Gmail label IDs from email_folders")
        return {by_id[key] for key in custom if by_id[key] != key}

    def mark_read(self, *, message_ref="", uid=None, uidvalidity=None, folder=None, prepare_targets=None):
        return self.mailbox(action="flags", message_ref=message_ref, uid=uid, uidvalidity=uidvalidity,
                            flags=["\\Seen"], prepare_targets=prepare_targets)

    def mailbox(self, *, action="list", folder="INBOX", uid=None, uidvalidity=None, message_ref="",
                destination="", flags=None, remove_flags=False, add_labels=None, remove_labels=None,
                label_id="", properties=None, draft_id="", label_names=None, prepare_targets=None):
        # One fresh snapshot per operation. Never cache a mutable display name
        # across calls or translate the provider's add/remove ID arrays as names.
        @cache
        def snapshot():
            return self._request("GET", "/labels").get("labels", [])

        def write(method, path, body, targets, affected=(), fixed_names=()):
            if prepare_targets:
                names = set(fixed_names)
                names.update(self._current_label_names(affected, snapshot))
                if names or label_names:
                    targets["label_names"] = sorted(names)
            prepared = prepare_targets(targets) if prepare_targets else None
            return prepared if prepared is not None else self._request(method, path, body=body)

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
            if not isinstance(draft_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", draft_id):
                raise ValueError("Choose draft_id from email_search(folder='DRAFT')")
            return write("DELETE", "/drafts/" + draft_id, None, {"remove_labels": ["DRAFT"]}, fixed_names=["Drafts"])
        native = self.native_id(message_ref or (format(int(uid) % (2 ** 64), "x") if uid is not None and uidvalidity == 1 else ""))
        if action in {"trash", "untrash"}:
            return write("POST", f"/messages/{native}/{action}", {}, {}, fixed_names=["TRASH"])
        if action == "delete":
            scopes = self.account.get("scopes")
            if scopes is not None and "https://mail.google.com/" not in scopes:
                raise AccountError("Permanent deletion requires a new Google consent with the full mail scope; trash is available with gmail.modify")
            return write("DELETE", "/messages/" + native, None, {})
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
        return write("POST", "/messages/" + native + "/modify", body, targets, [*add, *remove])

    def draft(self, *, to, subject, body="", folder="DRAFT", reply_to_message_id="", references=None,
              cc=(), bcc=(), html_body="", body_type="plain", attachments=(), sender="", draft_id="", thread_id=""):
        if folder not in {"DRAFT", "Drafts", ""}:
            raise ValueError("Gmail drafts live in DRAFT; apply additional labels separately with email_mailbox")
        message = build_message(sender=sender or self.settings["EMAIL_USER"], recipients=mailbox_addresses(to), subject=subject, body=body,
                                in_reply_to=reply_to_message_id, references=references or [], cc=mailbox_addresses(cc), bcc=mailbox_addresses(bcc),
                                html_body=html_body, body_type=body_type, attachments=attachments, include_bcc_header=True)
        payload = {"message": {"raw": base64.urlsafe_b64encode(message.as_bytes(policy=SMTP)).decode().rstrip("=")}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        if draft_id:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", str(draft_id)):
                raise ValueError("Invalid draft id")
            result = self._request("PUT", "/drafts/" + str(draft_id), body=payload)
        else:
            result = self._request("POST", "/drafts", body=payload)
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
        body = None if action == "delete" else dict(values or {})
        targets = {}
        if resource == "filters" and action == "create":
            if identifier:
                raise ValueError("Creating a Gmail filter targets the filter collection; omit identifier")
            if any(key.lower() == "action" and key != "action" for key in body):
                raise ValueError("Use the Gmail filter's documented action field")
            rule = body.get("action", {})
            if not isinstance(rule, dict) or rule.keys() - {"forward", "addLabelIds", "removeLabelIds"}:
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
        return self._request({"create": "POST", "update": "PUT", "delete": "DELETE"}[action], path, body=body)

    @contextmanager
    def delivery(self):
        # Authentication refresh happens before the outbox enters uncertain-send custody.
        self._delivery_access = self._access_token()
        try:
            yield self
        finally:
            self._delivery_access = ""

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
        try:
            result = self._request("POST", "/messages/send", body=payload, access=self._delivery_access)
        except GmailError as exc:
            if 400 <= exc.status < 500 and exc.status != 408:
                raise DeliveryRejected(exc, retryable=exc.status == 429, retry_after=exc.retry_after) from exc
            raise
        if not result.get("id"):
            raise RuntimeError("Gmail send response omitted its message id; inspect Sent before sending again")
        return {}, {"transport": "gmail_api", "message_id": str(result.get("id") or ""),
                    "thread_id": str(result.get("threadId") or "")}

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
            ids = [item["id"] for item in self._pages("/messages", "messages", params={
                "q": "after:" + str(int(cursor["activated_at"])), "maxResults": 100})]
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
        profile = self._request("GET", "/profile")
        return {"ok": True, "provider": "gmail", "address": profile.get("emailAddress"),
                "scopes": self.account.get("scopes"), "scope_status": "known" if self.account.get("scopes") is not None else "unknown"}
