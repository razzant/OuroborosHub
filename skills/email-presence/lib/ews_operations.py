"""Exchange-specific mutations and sync pages; the Mail runtime owns custody."""

from __future__ import annotations

import base64
import time
from contextlib import contextmanager
from email.policy import SMTP
from email.utils import getaddresses

from .accounts import AccountError
from .delivery import DeliveryRejected
from .ews_wire import NS, EwsError, ews_timestamp, m, t
from .mime import build_message, mailbox_addresses


_MAIL_TYPES = {f"{{{NS['t']}}}{kind}" for kind in
               ("Message", "MeetingMessage", "MeetingRequest", "MeetingResponse", "MeetingCancellation", "PostItem", "Item")}


def _mailboxes(tag, addresses):
    group = t(tag)
    for name, address in addresses:
        mailbox = t("Mailbox")
        if name:
            mailbox.append(t("Name", text=name))
        mailbox.append(t("EmailAddress", text=address))
        group.append(mailbox)
    return group


def _message_item(message, blind):
    # The visible wire message has no Bcc header. Blind recipients are a
    # native envelope property, including in a saved draft.
    item = t("Message", t("MimeContent", text=base64.b64encode(message.as_bytes(policy=SMTP)).decode("ascii")))
    for tag, values in (("ToRecipients", message.get_all("To", [])),
                        ("CcRecipients", message.get_all("Cc", [])), ("BccRecipients", blind),
                        ("From", message.get_all("From", []))):
        pairs = getaddresses([str(x) for x in values])
        if pairs:
            item.append(_mailboxes(tag, pairs))
    return item


def _create(folder, item, disposition):
    return m("CreateItem", m("SavedItemFolderId", folder), m("Items", item), MessageDisposition=disposition)


class EwsOperations:
    @contextmanager
    def delivery(self):
        # Open/authenticate before the outbox crosses its uncertain-send boundary.
        try:
            self.test_connection()
        except EwsError as exc:
            if exc.http_status in {401, 403}:
                raise AccountError(str(exc)) from exc
            raise
        yield self

    def transmit(self, _session, message, recipients, *, has_bcc=False, reporting=None):
        visible = {addr.casefold() for _, addr in getaddresses([str(message.get("To", "")), str(message.get("Cc", ""))])}
        blind = [addr for addr in recipients if addr.casefold() not in visible] if has_bcc else []
        op = _create(self._folder("sentitems"), _message_item(message, blind), "SendAndSaveCopy")
        try:
            self._call(op)
        except EwsError as exc:
            if exc.http_status in {401, 403}:
                raise AccountError(str(exc)) from exc
            if exc.proven and exc.code == "ErrorServerBusy":
                raise DeliveryRejected(exc, retryable=True, retry_after=exc.retry_after) from exc
            if exc.proven and exc.code in {"ErrorAccessDenied", "ErrorSendAsDenied", "ErrorInvalidRecipients",
                                          "ErrorMissingRecipients", "ErrorNameResolutionNoResults",
                                          "ErrorNonExistentMailbox", "ErrorMessageSizeExceeded"}:
                raise DeliveryRejected(exc) from exc
            raise  # No proven refusal: EmailRuntime records uncertain, never auto-resends.
        # Success/NoError with empty Items is the documented SendAndSaveCopy
        # acknowledgment. There is no remote item id to invent.
        return {}, {"transport": "ews", "internet_message_id": str(message["Message-ID"])}

    def draft(self, *, to, subject, body="", folder="drafts", reply_to_message_id="", references=None,
              cc=(), bcc=(), html_body="", body_type="plain", attachments=(), sender="", draft_id=""):
        if draft_id:
            raise ValueError("Exchange email_draft saves a new draft; updating an existing draft is not supported")
        message = build_message(sender=sender or self.account["address"], recipients=mailbox_addresses(to),
                                subject=subject, body=body, in_reply_to=reply_to_message_id,
                                references=references or [], cc=mailbox_addresses(cc),
                                html_body=html_body, body_type=body_type, attachments=attachments)
        reply = self._call(_create(self._folder(folder), _message_item(message, mailbox_addresses(bcc)), "SaveOnly"))
        identifier = reply.find("m:Items/t:Message/t:ItemId", NS)
        if identifier is None or not identifier.get("Id"):
            raise RuntimeError("Exchange acknowledged saving a draft but returned no item id; inspect Drafts before retrying")
        return {"ok": True, "message_id": str(message["Message-ID"]), "folder": folder,
                "message_ref": "ews:" + identifier.get("Id")}

    def _sync_page(self, folder_id, cursor):
        shape = m("ItemShape", t("BaseShape", text="IdOnly"))
        if cursor["initializing"]:
            shape.append(t("AdditionalProperties", t("FieldURI", FieldURI="item:DateTimeReceived"),
                           t("FieldURI", FieldURI="item:DateTimeCreated")))
        op = m("SyncFolderItems", shape, m("SyncFolderId", folder_id))
        if cursor["state"]:
            op.append(m("SyncState", text=cursor["state"]))
        op.append(m("MaxChangesReturned", text="512"))  # Exchange's page ceiling.
        return self._call(op)

    def poll(self, store, *, folder="INBOX", accepts=None):
        # Resolve the stable native folder once for this configured worker. A
        # distinguished name, its localized title and its explicit handle can
        # all name the same folder, including across an account restart.
        cached = getattr(self, "_sync_target", None)
        if cached is None or cached[0] != folder:
            self._sync_target = cached = (folder, self._folder_info(folder))
        target = cached[1]
        folder_id = t("FolderId", Id=target["id"])
        key = "ews_sync:" + target["id"]
        cursor = store.get(key)
        if cursor is None:
            server_time = target.get("server_time")
            cursor = {"state": "", "initializing": True,
                      "activated_at": time.time() if server_time is None else server_time}
            store.put(key, cursor)  # Keep the original boundary across startup failures.
        try:
            page = self._sync_page(folder_id, cursor)
        except EwsError as exc:
            if exc.proven and exc.code == "ErrorFolderNotFound":
                self._sync_target = None  # A replacement folder has a new native id.
            if not (exc.proven and exc.code == "ErrorInvalidSyncStateData" and cursor["state"]):
                raise
            # Re-enumerate from the original activation, never silently set a
            # new baseline after outage. Captured native events are replay-safe.
            cursor = {**cursor, "initializing": True, "state": "", "last_reset_at": time.time()}
            store.put(key, cursor)
            page = self._sync_page(folder_id, cursor)
        state = page.findtext("m:SyncState", namespaces=NS)
        last = page.findtext("m:IncludesLastItemInRange", namespaces=NS)
        changes = page.find("m:Changes", NS)
        if not state or last not in {"true", "false", "1", "0"} or changes is None:
            raise RuntimeError("Incomplete Exchange sync page; cursor retained")
        complete = last in {"true", "1"}
        if not complete and state == cursor["state"]:
            raise RuntimeError("Exchange sync page did not advance; cursor retained")
        count = 0
        for created in changes.findall("t:Create", NS):
            if len(created) != 1:
                raise RuntimeError("Invalid Exchange create notification; cursor retained")
            item = created[0]
            if item.tag not in _MAIL_TYPES:
                continue  # Other folder item types (contacts, tasks, calendar) are not mail.
            identifier = item.find("t:ItemId", NS)
            if identifier is None or not identifier.get("Id"):
                raise RuntimeError("Exchange mail notification omitted item id; cursor retained")
            if cursor["initializing"]:
                value = item.findtext("t:DateTimeReceived", namespaces=NS) or item.findtext("t:DateTimeCreated", namespaces=NS)
                if not value:
                    raise RuntimeError("Exchange baseline mail omitted its delivery date; cursor retained")
                if ews_timestamp(value) < cursor["activated_at"]:
                    continue
            reference = "ews:" + identifier.get("Id")
            try:
                message = self.read(message_ref=reference, include_attachment_data=True)
            except EwsError as exc:
                if exc.proven and exc.code == "ErrorItemNotFound":
                    continue  # Removed before this notification could be captured.
                raise
            if accepts is None or accepts(message):
                message.update(folder=self._folder_ref(target["id"]), uid=None)
                _, inserted = store.ingest(message)
                count += int(inserted)
        # One page per poll: the companion can service stop/config changes
        # during a large initial scan. An interrupted page is replayed.
        store.put(key, {**cursor, "state": state, "initializing": cursor["initializing"] and not complete})
        store.put("poll_health", {"ok": True, "checked_at": time.time(), "new_messages": count,
                                  "more_changes": not complete,
                                  **({"last_sync_reset_at": cursor["last_reset_at"]} if "last_reset_at" in cursor else {})})
        return count
