"""Host-supervised transport workers; all decisions remain with Presence."""
from __future__ import annotations

import asyncio
import logging
import smtplib
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone

from .delivery import email_report
from .accounts import AccountError
from .gmail import DeliveryRejected
from .host_adapter import HostBindingTerminalError, HostRetryError, HostTurnStatus
from .mime import extract_body
from .settings import SENDING_OFF, load_settings
from .store import RequestIdConflict

log = logging.getLogger("email_presence")
# A busy conversation or full capacity refusal resubmits the same event after the
# larger of the Host's retry_after and this transport's own backoff, which starts at
# one second, doubles per refusal and is capped here. The Host's hint is never cut.
_CAPACITY_BACKOFF_MAX_SECONDS = 30
# Which mail each inbox loop claims: new mail (no Host reference yet), admitted work
# to observe (a stored reference), or either in one claim.
_ROLE_CLAIMS = {None: {}, "intake": {"submitted": False}, "observe": {"submitted": True}}


def _retry(exc, attempts):
    """The inbox note and delay for a retryable failure of one claim."""
    if isinstance(exc, HostRetryError) and exc.code == "presence_capacity_full":
        backoff = min(_CAPACITY_BACKOFF_MAX_SECONDS, 2 ** min(max(attempts - 1, 0), 5))
        return str(exc), backoff if exc.retry_after is None else max(exc.retry_after, backoff)
    return type(exc).__name__, min(60, 2 ** min(attempts, 5))


def _wire_text(message):
    """Extract the visible body without calling get_content on multipart MIME."""
    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None and body.get_content_type() == "text/plain":
        return body.get_content()
    return extract_body(message)


class EmailRuntime:
    def __init__(self, store, client, host, *, folder="INBOX", account="", poll_interval=30, account_loader=None):
        self.store, self.client, self.host = store, client, host
        self.folder, self.account = folder, account
        self.poll_interval = max(5, min(3600, int(poll_interval)))
        self.account_loader = account_loader

    def poll(self):
        capture = getattr(self.host, "capture_binding", None)
        target_check = False
        if capture:
            try:
                place = capture(self.account, "", require_place=True)
            except Exception:
                place = {}  # A host outage must not erase mail already owed by an established intake.
            if place.get("supported") and place.get("code") == "place_not_connected":
                self.store.put("poll_health", {"ok": True, "checked_at": time.time(), "new_messages": 0,
                                                "intake": "no_place", "message": "Tools are available; connect a Place for automatic intake."})
                return 0
            target_check = bool(place.get("supported") and not place.get("binding"))
        def accepts(message):
            if not target_check:
                return True
            conversation = (message.get("references") or [message.get("in_reply_to") or message["message_id"]])[0]
            facts = capture(self.account, conversation, thread_id=conversation, require_place=True)
            return not facts.get("supported") or bool((facts.get("binding") or {}).get("place_id"))
        provider_poll = getattr(self.client, "poll", None)
        if provider_poll:
            return provider_poll(self.store, folder=self.folder, accepts=accepts)
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
                if (not cursor.get("rescan") or message["internal_date"] >= cursor["activated_at"]) and accepts(message):
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

    def _anchored(self, item, anchor):
        """The captured mail a version 2 ``reply_anchor`` names, else the polled mail.

        Its message's folder/UIDVALIDITY/UID select one stored row, which must be that
        exact source event and Message-ID and in the polled mail's thread (this store is
        one mailbox). Children and older Hosts carry no anchor.
        """
        anchor = anchor if isinstance(anchor, Mapping) else {}
        source = str(anchor.get("source_event_id") or "")
        if not source or source == item.provider_event_key:
            return item
        message = anchor.get("message") if isinstance(anchor.get("message"), Mapping) else {}
        key = (message.get("folder"), message.get("uidvalidity"), message.get("imap_uid"))
        target = (self.store.inbox_event(*key)
                  if isinstance(key[0], str) and type(key[1]) is int and type(key[2]) is int else None)
        if (target is None or target.provider_event_key != source or target.message_id != message.get("message_id")
                or target.thread_key != item.thread_key):
            log.warning("Presence reply anchor %s is not a captured mail of this thread; replying to %s",
                        source, item.provider_event_key)
            return item
        return target

    def _reply(self, item, texts, suffix, mode=0, turn_ref="", anchor=None):
        text = "\n".join(texts).strip()
        if text:
            # A version 2 author output answers its last covered input: reply to that
            # captured message in the same thread. Without an anchor (children, older
            # Hosts) or an unbound one, reply to the polled event as before. The
            # request id stays the polled event's, so each output is queued once.
            target = self._anchored(item, anchor)
            subject = target.subject if target.subject.lower().startswith("re:") else f"Re: {target.subject}"
            client_settings = getattr(self.client, "settings", {})
            reporting = {"version": mode, "account_id": self.account,
                         "sender": client_settings.get("EMAIL_DEFAULT_SENDER") or client_settings.get("EMAIL_USER", ""),
                         "origin": {"kind": "automatic", "task_id": turn_ref, "source_event_id": item.provider_event_key}}
            if target.context.get("provider_thread_id"):
                reporting["gmail_thread_id"] = target.context["provider_thread_id"]
            request_id = f"presence:{item.provider_event_key}:{suffix}"
            existing = self.store.receipt(request_id)
            hold = ""
            capture = getattr(self.host, "capture_binding", None)
            if existing is not None:
                reporting = existing["delivery_reporting"]
            elif capture:
                # Transport failure precedes any outbox record. Let the inbox's
                # existing retry custody replay the cached paid output, not mint
                # an immutable unknown binding from a temporary outage.
                facts = capture(self.account, target.thread_key, thread_id=target.thread_key, require_place=True)
                if facts.get("supported"):
                    reporting["place_binding"] = facts.get("binding")
                    if not facts.get("allowed"):
                        hold = str(facts.get("code") or facts.get("state") or "place policy unavailable")
            try:
                self.store.enqueue_outbox(request_id=request_id,
                                          recipients=target.context.get("reply_to") or [target.sender], subject=subject,
                                          body=text, in_reply_to=target.message_id,
                                          references=target.references or ([target.in_reply_to] if target.in_reply_to else []),
                                          reporting=reporting, hold_reason=hold)
            except RequestIdConflict:
                # The first queued copy of this output identity stays authoritative.
                log.warning("Presence output %s was already queued with other headers; keeping the first", suffix)

    async def discover_support(self):
        discover = getattr(self.host, "discover_delivery_support", None)
        mode = await discover() if discover else 0
        self.store.put("delivery_support", {"version": mode,
                       "status": getattr(self.host, "delivery_reporting_status", "unsupported"),
                       "presence_identity": getattr(self.host, "presence_identity", 0),
                       "presence_continuation_version": getattr(self.host, "continuation_version", 0)})

    async def process_inbox(self, role=None):
        """One inbox claim: submit new mail, observe admitted work, or (no role) either.

        The companion runs an ``intake`` loop, which only submits mail without a Host
        reference and releases it as soon as that reference is durable, and an
        ``observe`` loop, which only polls stored references. An output poll therefore
        never holds new mail.
        """
        item = self.store.claim_inbox(**_ROLE_CLAIMS[role])
        if not item:
            return False
        try:
            await self.discover_support()
            if not self.host.available:
                self.store.retry_inbox(item.row_id, item.lease_token, "Presence binding is not configured", 30)
                return True
            ref = item.host_reference or await self.host.submit(item)
            observe = True
            if not item.host_reference:
                self.store.set_host_reference(item.row_id, item.lease_token, ref)
                # An input claim never polls, and continuing custody is not a reply:
                # either way only what the reference already says is committed now.
                observe = role != "intake" and not ref.startswith("continuing:")
            if observe:
                status = await self.host.status(ref)
            else:
                initial = getattr(self.host, "initial_status", None)
                status = initial(ref) if initial is not None else HostTurnStatus("pending")
            if status.host_reference and status.host_reference != ref:
                # A retained event's newly named child is kept before it is polled.
                self.store.set_host_reference(item.row_id, item.lease_token, status.host_reference)
            await asyncio.to_thread(self._reply, item, status.texts, "immediate", status.delivery_reporting_version, status.turn_ref)
            for output in status.outputs:
                await asyncio.to_thread(self._reply, item, (output.text,), output.identity, status.delivery_reporting_version, output.turn_ref,
                                        getattr(output, "anchor", None))
            if status.state == "pending" and not observe:
                # Released at once: the thread's next mail may follow, and the work
                # named by the reference is observed on a later claim.
                self.store.retry_inbox(item.row_id, item.lease_token, status.error, 0)
            elif status.state == "pending":
                # Plain pending keeps its pace; an unresolved or unreadable observation backs off.
                self.store.retry_inbox(item.row_id, item.lease_token, status.error or "Host turn pending",
                                       min(60, 2 ** min(item.attempts, 5)) if status.error else 5)
            elif status.state == "failed":
                self.store.fail_inbox(item.row_id, item.lease_token, status.error)
            else:
                delivery = await self.host.deliver(ref)
                await asyncio.to_thread(self._reply, item, delivery.texts, "final", delivery.delivery_reporting_version, delivery.turn_ref)
                self.store.complete_inbox(item.row_id, item.lease_token)
        except HostBindingTerminalError as exc:
            self.store.fail_inbox(item.row_id, item.lease_token, str(exc))
        except Exception as exc:
            # A capacity refusal keeps the same event first in its thread; other
            # threads' mail is claimed meanwhile.
            note, delay = _retry(exc, item.attempts)
            self.store.retry_inbox(item.row_id, item.lease_token, note, delay)
        return True

    def _delivery_hold(self, item):
        conversation = item.references[0] if item.references else item.in_reply_to or item.message_id
        if "place_binding" in item.reporting:
            check = getattr(self.host, "binding_hold", None)
            binding = item.reporting["place_binding"]
            conversation = binding.get("conversation_id", "") if isinstance(binding, dict) else conversation
            thread = binding.get("thread_id", "") if isinstance(binding, dict) else ""
            return check(binding, self.account, conversation, thread_id=thread) if check else "Held: Host cannot check the original Place binding"
        check = getattr(self.host, "place_hold", None)
        return check(self.account, conversation) if check else ""

    def process_outbox(self):
        item = self.store.claim_outbox()
        if not item:
            return False
        sending = False
        try:
            current = self.account_loader() if self.account_loader else load_settings(self.store.state_dir)
            if current.get("sending", "on") == "off":
                self.store.hold_outbox(item, "Held: " + SENDING_OFF)
                return True
            reason = self._delivery_hold(item)
            if reason:
                self.store.hold_outbox(item, reason)
                return True
            message = self.client.message(item)
            reporting = dict(item.reporting)
            if reporting.get("version") == -1:
                support = self.store.get("delivery_support") or {"version": 0, "status": "unavailable"}
                reporting.update(version=support["version"], status=support["status"])
            reporting["account_id"] = self.account or reporting.get("account_id") or str(message["From"])
            reporting.setdefault("sender", str(message["From"]))
            # MailClient constructs ordinary messages; match send_message's
            # envelope parsing while retaining the original named To header.
            envelope_recipients = list(item.envelope_recipients)
            reporting["wire"] = {"text": _wire_text(message), "subject": str(message["Subject"]),
                                 "recipients": envelope_recipients, "to": str(message["To"] or ""),
                                 "cc": str(message["Cc"] or "")}
            self.store.set_reporting(item, reporting)
            item = replace(item, reporting=reporting)
            delivery = getattr(self.client, "delivery", None) or self.client.smtp
            with delivery() as smtp:
                # Policy may have changed while the provider connection opened.
                reason = self._delivery_hold(item)
                if reason:
                    self.store.hold_outbox(item, reason)
                    return True
                if not self.store.mark_sending(item):
                    return True
                sending = True
                # Bcc is deliberately absent from MIME headers. Pass the
                # durable envelope explicitly when present; legacy test/fake
                # SMTP clients still receive the old one-argument call.
                transmit = getattr(self.client, "transmit", None)
                if transmit:
                    refused, provider_receipt = transmit(smtp, message, envelope_recipients, has_bcc=bool(item.bcc), reporting=reporting)
                else:
                    refused = smtp.send_message(message, to_addrs=envelope_recipients) if item.bcc else smtp.send_message(message)
                    provider_receipt = {}
                if refused:
                    accepted = [address for address in envelope_recipients if address not in refused]
                    reports = [email_report(item, "accepted", recipients=accepted)] if accepted else []
                    reports.append(email_report(item, "failed", recipients=list(refused), refused=refused))
                    self.store.record_recipient_outcome(item.row_id, item.lease_token, accepted=accepted, refused=refused)
                    self.store.uncertain_outbox(item.row_id, item.lease_token,
                                               "Some recipients were refused; accepted recipients must not be resent automatically", reports=reports)
                else:
                    if provider_receipt:
                        reporting["provider_receipt"] = provider_receipt
                        item = replace(item, reporting=reporting)
                        self.store.complete_outbox(item.row_id, item.lease_token, reports=[email_report(item, "accepted")], reporting=reporting)
                    else:
                        self.store.complete_outbox(item.row_id, item.lease_token, reports=[email_report(item, "accepted")])
        except DeliveryRejected as exc:
            if exc.retryable and item.attempts < 5:
                self.store.retry_outbox(item.row_id, item.lease_token, str(exc), max(5, exc.retry_after or 0))
            else:
                self.store.fail_outbox(item.row_id, item.lease_token, str(exc), reports=[email_report(item, "failed", error=str(exc))])
        except AccountError as exc:
            self.store.hold_outbox(item, "Held: " + str(exc))
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
        await self.discover_support()
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

        async def inbox(role):
            # Two loops under this same lifecycle: new mail is submitted while an
            # admitted turn's poll may still be waiting on HTTP, and vice versa.
            while not stop.is_set():
                if not await self.process_inbox(role):
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

        workers = [asyncio.create_task(coro) for coro in (poller(), inbox("intake"), inbox("observe"), outbox())]
        try:
            await asyncio.gather(*workers)
        finally:
            stop.set()
            await asyncio.gather(*workers, return_exceptions=True)
