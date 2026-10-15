"""Host-supervised transport workers; all decisions remain with Presence."""
from __future__ import annotations

import asyncio
import logging
import smtplib
import time
from dataclasses import replace
from datetime import datetime, timezone

from .delivery import email_report
from .host_adapter import HostBindingTerminalError
from .mime import extract_body

log = logging.getLogger("email_presence")


def _wire_text(message):
    """Extract the visible body without calling get_content on multipart MIME."""
    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None and body.get_content_type() == "text/plain":
        return body.get_content()
    return extract_body(message)


class EmailRuntime:
    def __init__(self, store, client, host, *, folder="INBOX", account="", poll_interval=30):
        self.store, self.client, self.host = store, client, host
        self.folder, self.account = folder, account
        self.poll_interval = max(5, min(3600, int(poll_interval)))

    def poll(self):
        with self.client.imap(self.folder) as box:
            validity, next_uid = self.client.metadata(box)
            cursor = self.store.prepare_cursor(self.account, self.folder, validity, next_uid)
            criteria = ["UID", f"{cursor['uid'] + 1}:*"]
            if cursor["uid"] == 0:
                # UIDVALIDITY reset: rescan since activation, dedupe Message-ID.
                criteria += ["SINCE", datetime.fromtimestamp(cursor["activated_at"], timezone.utc).strftime("%d-%b-%Y")]
            count = 0
            for uid in self.client.uids(box, criteria):
                # IMAP n:* includes the last UID even when n is beyond it.
                if uid <= cursor["uid"]:
                    continue
                message = self.client.fetch(box, self.folder, uid, validity, include_attachment_data=True)
                cursor["uid"] = uid
                key = self.store.cursor_key(self.account, self.folder)
                if not cursor.get("rescan") or message["internal_date"] >= cursor["activated_at"]:
                    if message.get("attachments") or message.get("_raw_source") is not None:
                        self.store.stage_inbound_attachments(message)
                    _, inserted = self.store.ingest(message, cursor_key=key, cursor=cursor)
                    count += int(inserted)
                else:
                    self.store.put(key, cursor)
            if cursor.pop("rescan", False):
                self.store.put(self.store.cursor_key(self.account, self.folder), cursor)
            self.store.put("poll_health", {"ok": True, "checked_at": time.time(), "new_messages": count})
            return count

    def _stage_inbound_best_effort(self, message):
        """Compatibility seam; staging policy lives in EmailStore."""
        if hasattr(self.store, "stage_inbound_attachments"):
            return self.store.stage_inbound_attachments(message)
        # Tiny adapter for older test doubles; production policy remains in
        # EmailStore and cannot be bypassed by the runtime.
        staged, omitted = [], []
        for index, item in enumerate(message.get("attachments") or ()):
            try:
                staged.extend(self.store._stage_files([item], bucket="inbound", identity=f"{message.get('message_id') or message.get('uid')}:{index}"))
            except Exception as exc:
                descriptor = {key: value for key, value in dict(item).items() if key not in {"data", "path"}}
                descriptor.update(content_available=False, stage_error=type(exc).__name__, error=type(exc).__name__)
                omitted.append(descriptor)
        message["staged_files"] = staged
        message["attachments"] = [{key: value for key, value in item.items() if key != "path"} for item in staged] + omitted
        if omitted:
            message["attachment_stage_note"] = "Some attachment bytes were not staged; the full RFC822 source artifact is available."
        return staged

    def _reply(self, item, texts, suffix, mode=0, turn_ref=""):
        text = "\n".join(texts).strip()
        if text:
            subject = item.subject if item.subject.lower().startswith("re:") else f"Re: {item.subject}"
            self.store.enqueue_outbox(request_id=f"presence:{item.provider_event_key}:{suffix}",
                                      recipients=item.context.get("reply_to") or [item.sender], subject=subject, body=text,
                                      in_reply_to=item.message_id, references=item.references,
                                      reporting={"version": mode, "account_id": self.account,
                                                 "origin": {"kind": "automatic", "task_id": turn_ref,
                                                            "source_event_id": item.provider_event_key}})

    async def process_inbox(self):
        item = self.store.claim_inbox()
        if not item:
            return False
        try:
            if not self.host.available:
                self.store.retry_inbox(item.row_id, item.lease_token, "Presence binding is not configured", 30)
                return True
            ref = item.host_reference or await self.host.submit(item)
            if not item.host_reference:
                self.store.set_host_reference(item.row_id, item.lease_token, ref)
            status = await self.host.status(ref)
            if status.host_reference and status.host_reference != ref:
                # A retained event's newly named child is kept before it is polled.
                self.store.set_host_reference(item.row_id, item.lease_token, status.host_reference)
            self._reply(item, status.texts, "immediate", status.delivery_reporting_version, status.turn_ref)
            for output in status.outputs:
                self._reply(item, (output.text,), output.identity, status.delivery_reporting_version, output.turn_ref)
            if status.state == "pending":
                # Plain pending keeps its pace; an unresolved or unreadable observation backs off.
                self.store.retry_inbox(item.row_id, item.lease_token, status.error or "Host turn pending",
                                       min(60, 2 ** min(item.attempts, 5)) if status.error else 5)
            elif status.state == "failed":
                self.store.fail_inbox(item.row_id, item.lease_token, status.error)
            else:
                delivery = await self.host.deliver(ref)
                self._reply(item, delivery.texts, "final", delivery.delivery_reporting_version, delivery.turn_ref)
                self.store.complete_inbox(item.row_id, item.lease_token)
        except HostBindingTerminalError as exc:
            self.store.fail_inbox(item.row_id, item.lease_token, str(exc))
        except Exception as exc:
            self.store.retry_inbox(item.row_id, item.lease_token, type(exc).__name__, min(60, 2 ** min(item.attempts, 5)))
        return True

    def process_outbox(self):
        item = self.store.claim_outbox()
        if not item:
            return False
        sending = False
        try:
            message = self.client.message(item)
            reporting = dict(item.reporting)
            if reporting.get("version") == -1:
                support = self.store.get("delivery_support") or {"version": 0, "status": "unavailable"}
                reporting.update(version=support["version"], status=support["status"])
            reporting["account_id"] = str(message["From"])
            # MailClient constructs ordinary messages; match send_message's
            # envelope parsing while retaining the original named To header.
            envelope_recipients = list(item.envelope_recipients)
            reporting["wire"] = {"text": _wire_text(message), "subject": str(message["Subject"]),
                                 "recipients": envelope_recipients, "to": str(message["To"] or ""),
                                 "cc": str(message["Cc"] or "")}
            self.store.set_reporting(item, reporting)
            item = replace(item, reporting=reporting)
            with self.client.smtp() as smtp:
                if not self.store.mark_sending(item):
                    return True
                sending = True
                # Bcc is deliberately absent from MIME headers. Pass the
                # durable envelope explicitly when present; legacy test/fake
                # SMTP clients still receive the old one-argument call.
                if item.bcc:
                    refused = smtp.send_message(message, to_addrs=envelope_recipients)
                else:
                    refused = smtp.send_message(message)
                if refused:
                    accepted = [address for address in envelope_recipients if address not in refused]
                    reports = [email_report(item, "accepted", recipients=accepted)] if accepted else []
                    reports.append(email_report(item, "failed", recipients=list(refused), refused=refused))
                    self.store.record_recipient_outcome(item.row_id, item.lease_token, accepted=accepted, refused=refused)
                    self.store.uncertain_outbox(item.row_id, item.lease_token,
                                               "Some recipients were refused; accepted recipients must not be resent automatically", reports=reports)
                else:
                    self.store.complete_outbox(item.row_id, item.lease_token, reports=[email_report(item, "accepted")])
        except (smtplib.SMTPDataError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            # Typed negative SMTP replies prove this transaction was not accepted.
            code = getattr(exc, "smtp_code", 550)
            if isinstance(exc, smtplib.SMTPRecipientsRefused):
                codes = [value[0] for value in exc.recipients.values()]
                code = 450 if codes and all(400 <= c < 500 for c in codes) else 550
                refused = exc.recipients
                envelope = list(item.envelope_recipients)
                accepted = [address for address in envelope if address not in refused]
                if accepted:
                    self.store.record_recipient_outcome(item.row_id, item.lease_token, accepted=accepted, refused=refused)
                    reports = [email_report(item, "accepted", recipients=accepted),
                               email_report(item, "failed", recipients=list(refused), refused=refused)]
                    self.store.uncertain_outbox(item.row_id, item.lease_token,
                                               "Some recipients were refused; accepted recipients must not be resent automatically",
                                               reports=reports)
                    return True
            if code < 500 and item.attempts < 5:
                self.store.retry_outbox(item.row_id, item.lease_token, type(exc).__name__, 5)
            else:
                self.store.fail_outbox(item.row_id, item.lease_token, type(exc).__name__, reports=[email_report(item, "failed", error=type(exc).__name__)])
        except Exception as exc:
            if sending:
                self.store.uncertain_outbox(item.row_id, item.lease_token,
                                           f"{type(exc).__name__} during SMTP delivery; inspect Message-ID before retry",
                                           reports=[email_report(item, "uncertain", error=type(exc).__name__)])
            elif item.attempts >= 5:
                self.store.fail_outbox(item.row_id, item.lease_token, type(exc).__name__, reports=[email_report(item, "failed", error=type(exc).__name__)])
            else:
                self.store.retry_outbox(item.row_id, item.lease_token, type(exc).__name__, min(60, 2 ** item.attempts))
        return True

    async def process_delivery_report(self):
        pending = self.store.next_delivery_report()
        if not pending:
            return False
        row_id, index, payload = pending
        try:
            await self.host.report_delivery(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.finish_delivery_report(row_id, index, type(exc).__name__)
        else:
            self.store.finish_delivery_report(row_id, index)
        return True

    async def run(self, stop):
        discover = getattr(self.host, "discover_delivery_support", None)
        mode = await discover() if discover else 0
        self.store.put("delivery_support", {"version": mode, "status": getattr(self.host, "delivery_reporting_status", "unsupported")})
        async def wait(seconds):
            try:
                await asyncio.wait_for(stop.wait(), seconds)
            except asyncio.TimeoutError:
                pass

        async def poller():
            interval = self.poll_interval
            while not stop.is_set():
                try:
                    count = await asyncio.to_thread(self.poll)
                    interval = 5 if count else min(self.poll_interval, interval * 2)
                except Exception as exc:
                    self.store.put("poll_health", {"ok": False, "checked_at": time.time(), "error": type(exc).__name__})
                    log.warning("IMAP poll failed: %s", type(exc).__name__)
                    interval = self.poll_interval
                await wait(interval)

        async def inbox():
            while not stop.is_set():
                if not await self.process_inbox():
                    await wait(1)

        async def outbox():
            # Independent from a long Presence call: early tool sends can leave now.
            reporting = None
            try:
                while not stop.is_set():
                    if reporting is None or reporting.done():
                        if reporting is not None:
                            await reporting
                        reporting = asyncio.create_task(self.process_delivery_report())
                    if not await asyncio.to_thread(self.process_outbox):
                        await wait(1)
            finally:
                if reporting is not None:
                    reporting.cancel()
                    await asyncio.gather(reporting, return_exceptions=True)

        await asyncio.gather(poller(), inbox(), outbox())
