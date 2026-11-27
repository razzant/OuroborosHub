"""Host-supervised transport workers; all decisions remain with Presence."""
from __future__ import annotations

import asyncio
import logging
import smtplib
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone

import httpx

from .delivery import DeliveryDeferred, DeliveryRejected, email_report
from .accounts import AccountError
from .gmail import GmailError
from .gmail_host import HostNotReached
from .host_adapter import (_BINDINGS_CORRUPT, _DRAFT_POLICY_UNREAD, _TARGETS_DIFFER, LEGACY_REPLY_HELD, MATCH_SCOPE,
                           PLACE_MATCH, RECIPIENT_PLACES, HostBindingTerminalError, HostRetryError, HostTurnStatus,
                           automatic_reply_targets, binding_modes, capture_hold, capture_recipients, match_arguments,
                           place_refusal_hold, recipient_delivery_hold, reply_target_rows, valid_reply_targets)
from .mime import (MailAddressError, conversation_root, emitted_conversation, envelope_groups, extract_body,
                   reply_chain)
from .retained import RetainedSourceError, automatic_reply_envelope
from .settings import SENDING_OFF_HOLD, load_settings
from .store import RequestIdConflict, draft_held_notice, drafted_notice, unresolved_notice

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
    note = type(exc).__name__
    if isinstance(exc, HostRetryError):
        # Protocol diagnostics, never the Host's arbitrary error text or mail body.
        fields = [value for value in (exc.code, exc.disposition, exc.field) if value]
        if fields:
            note += ': ' + ' / '.join(fields)
    if isinstance(exc, HostRetryError) and exc.code == "presence_capacity_full":
        backoff = min(_CAPACITY_BACKOFF_MAX_SECONDS, 2 ** min(max(attempts - 1, 0), 5))
        return note, backoff if exc.retry_after is None else max(exc.retry_after, backoff)
    return note, min(60, 2 ** min(attempts, 5))


class ReplyAnchorUnusable(MailAddressError):
    """A version 2 output's reply_anchor names no captured mail of its thread: nothing is queued for it."""


def _submitted_targets_key(item):
    return f"reply_targets:{item.provider_event_key}"


def _target_keys(rows):
    return [(row["conversation"], row["effect"], row["match"], row.get("thread", "")) for row in rows]


def _automatic(item):
    """An automatic Presence reply: its recorded origin, or (rows older than origins) its presence: request id."""
    origin = item.reporting.get("origin")
    if isinstance(origin, Mapping):
        return origin.get("kind") == "automatic"
    return item.request_id.startswith("presence:")


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
            conversation = conversation_root(references=message.get("references"), in_reply_to=message.get("in_reply_to"), message_id=message["message_id"])
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
                rescan = cursor.get("rescan")
                if ((not rescan or message["internal_date"] >= cursor["activated_at"]) and accepts(message)
                        and not (rescan and self.store.captured_before_id_repair(message))):
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

        Its message's folder/UIDVALIDITY/UID select one stored row, which must be that exact source event and
        Message-ID and in the polled mail's thread (this store is one mailbox). Children and older Hosts carry no
        anchor. An anchor that names no such mail is unusable: the reply is not redirected to another message."""
        if anchor is None or anchor == {}:
            return item
        if (not isinstance(anchor, Mapping) or not isinstance(anchor.get("source_event_id"), str)
                or not anchor["source_event_id"].strip() or not isinstance(anchor.get("message"), Mapping)):
            raise ReplyAnchorUnusable("reply_anchor has no usable source_event_id and message; nothing was queued")
        source, message = anchor["source_event_id"], anchor["message"]
        if source == item.provider_event_key:
            # Even the waking event must not carry contradictory identity facts. Legacy anchors can omit fields;
            # their absent reply_targets are held separately, with the original words preserved.
            expected = {"folder": item.folder, "uidvalidity": item.uidvalidity, "imap_uid": item.uid,
                        "message_id": item.message_id}
            if any(key in message and (type(message[key]) is not type(value) or message[key] != value)
                   for key, value in expected.items()):
                raise ReplyAnchorUnusable("reply_anchor contradicts the captured wake event; nothing was queued")
            return item
        key = (message.get("folder"), message.get("uidvalidity"), message.get("imap_uid"))
        target = (self.store.inbox_event(*key)
                  if isinstance(key[0], str) and type(key[1]) is int and type(key[2]) is int else None)
        if (target is None or target.provider_event_key != source or target.message_id != message.get("message_id")
                or target.thread_key != item.thread_key):
            raise ReplyAnchorUnusable(f"reply_anchor {source} is not a captured mail of this thread; nothing was queued")
        return target

    def remember_reply_targets(self, item):
        """Record, before the event is submitted, the ``reply_targets`` this transport's event for ``item`` carries and, for
        Gmail, in the same write, the access the mailbox has as the event begins: an automatic reply to it records that
        access (_reply), whenever its words are ready, and goes out under the mailbox's current sign-in (rule A).

        A record already kept for the event stays as it is and is never added to: a turn may have begun under it, and an
        event of an earlier version has no access recorded, so its reply is queued without one.
        What is not confirmed is not guessed (GmailClient.intake_link): a host that cannot be read for now raises here, the
        event has not begun, and its claim is retried before anything is submitted. A mailbox that can be read but has no
        confirmed outgoing access is recorded without one: its mail goes on, and only the sending of its reply lacks the
        basis."""
        key = _submitted_targets_key(item)
        record = {"reply_targets": automatic_reply_targets(item)}
        if getattr(self.client, "transport", None) != "gmail_api":
            self.store.put(key, record)
            return
        if self.store.get(key) is not None:
            return  # no need to ask the host again
        link = self.client.intake_link()
        # The first record stays, whichever process writes next. The host is not asked inside this write.
        self.store.put_first(key, {**record, **({"gmail_connection": link} if link else {})})

    def _seen_targets(self, item, anchor):
        """The ``reply_targets`` the Host read before Safety for this output: from the selected anchor's message,
        or (no anchor: the wake event) from the event this transport submitted. None when they were absent."""
        anchor = anchor if isinstance(anchor, Mapping) else {}
        if str(anchor.get("source_event_id") or ""):
            message = anchor.get("message") if isinstance(anchor.get("message"), Mapping) else {}
            return message["reply_targets"] if "reply_targets" in message else None
        recorded = self.store.get(_submitted_targets_key(item))
        return recorded.get("reply_targets") if isinstance(recorded, Mapping) else None

    def _reply(self, item, texts, suffix, mode=0, turn_ref="", anchor=None):
        text = "\n".join(texts).strip()
        if text:
            request_id = f"presence:{item.provider_event_key}:{suffix}"
            if self.store.receipt(request_id) is not None:
                return  # The first durable output identity wins, even after the anchor changes.
            # A version 2 author output answers its last covered input: reply to that
            # captured message in the same thread. Without an anchor (children, older
            # Hosts), reply to the polled event as before. The request id stays the
            # polled event's, so each output is queued once.
            target = self._anchored(item, anchor)
            source, recipients = automatic_reply_envelope(target)
            chain = reply_chain(source)
            subject = target.subject if target.subject.lower().startswith("re:") else f"Re: {target.subject}"
            client_settings = getattr(self.client, "settings", {})
            reporting = {"version": mode, "account_id": self.account,
                         "reply_source_syntax": 1,
                         "sender": client_settings.get("EMAIL_DEFAULT_SENDER") or client_settings.get("EMAIL_USER", ""),
                         "origin": {"kind": "automatic", "task_id": turn_ref, "source_event_id": item.provider_event_key}}
            if getattr(self.client, "transport", None) == "gmail_api" and target.context.get("provider_thread_id"):
                reporting["gmail_thread_id"] = target.context["provider_thread_id"]
            if getattr(self.client, "transport", None) == "gmail_api":
                kept = self.store.get(_submitted_targets_key(target))
                if isinstance(kept, Mapping) and isinstance(kept.get("gmail_connection"), Mapping):
                    # The access its mail began under, recorded with the reply; it goes out under the current one (rule A).
                    reporting["gmail_connection"] = dict(kept["gmail_connection"])
            # The recipients whose Places the Host read before Safety must be exactly this reply's envelope. Absent
            # (prepared before they were checked), malformed or different targets keep the words held, never sent.
            seen = self._seen_targets(item, anchor)
            to = envelope_groups(recipients)[0]
            hold = ""
            if seen is None:
                hold = LEGACY_REPLY_HELD
            elif not valid_reply_targets(seen):
                hold = _TARGETS_DIFFER
            else:
                reporting["reply_targets"] = [dict(row) for row in seen]
                reporting["reply_anchor_event"] = target.provider_event_key
                if _target_keys(seen) != _target_keys(reply_target_rows(to)):
                    hold = _TARGETS_DIFFER
            capture = getattr(self.host, "capture_binding", None)
            if capture:
                # Transport failure precedes any outbox record. Let the inbox's
                # existing retry custody replay the cached paid output, not mint
                # an immutable unknown binding from a temporary outage.
                modes = {}
                facts = capture(self.account, target.thread_key, thread_id=target.thread_key, require_place=True)
                if facts.get("supported"):
                    reporting["place_binding"], modes["place_binding"] = facts.get("binding"), MATCH_SCOPE
                    if not facts.get("allowed"):
                        hold = hold or place_refusal_hold(facts)
                emitted = emitted_conversation(target.message_id, chain)
                if emitted != target.thread_key:
                    facts = capture(self.account, emitted, thread_id=emitted)
                    if facts.get('supported'):
                        reporting['emitted_place_binding'], modes['emitted_place_binding'] = facts.get('binding'), MATCH_SCOPE
                        if not facts.get('allowed'):
                            hold = hold or place_refusal_hold(facts)
                # Each actual envelope address (Reply-To, else From) keeps its own exact binding: a Silent/Paused
                # Place of that address holds this reply as it holds email_send.
                reason, record = capture_recipients(self.host, self.account, to)
                if record is not None:
                    reporting[RECIPIENT_PLACES], modes[RECIPIENT_PLACES] = record, record["match"]
                hold = hold or reason
                if modes:
                    reporting[PLACE_MATCH] = modes
            try:
                self.store.enqueue_outbox(request_id=request_id,
                                          recipients=recipients, subject=subject,
                                          body=text, in_reply_to=target.message_id,
                                          references=chain,
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
            # A retained event keeps its historical key. If old CFWS parsing selected another scope, check the
            # actual ancestry as well before submitting it; a pause there must not create a new author turn.
            if not item.host_reference:
                chain = reply_chain({**item.context, 'references': item.references, 'in_reply_to': item.in_reply_to})
                actual = emitted_conversation(item.message_id, chain)
                if actual != item.thread_key:
                    capture = getattr(self.host, 'capture_binding', None)
                    reason = capture_hold(capture, self.account, actual) if capture else None
                    if reason is None:
                        check = getattr(self.host, 'place_hold', None)
                        reason = check(self.account, actual, thread_id=actual) if check else None
                    if reason:
                        self.store.retry_inbox(item.row_id, item.lease_token, reason, 30)
                        return True
            if not item.host_reference:
                # The event carries these reply_targets (email_presence_event); remembered so an output without an
                # anchor (the wake event) is compared with what the Host actually read.
                await asyncio.to_thread(self.remember_reply_targets, item)  # for Gmail it also names the access the event begins under
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
            address_errors = []
            async def queue_reply(texts, identity, mode=0, turn_ref="", anchor=None):
                try:
                    await asyncio.to_thread(self._reply, item, texts, identity, mode, turn_ref, anchor)
                except MailAddressError as exc:
                    # Preserve the original Host reference/selected source. One bad
                    # recipient must not stop observation or another anchored output.
                    address_errors.append(f"invalid_recipient ({identity}): {exc}")
            def failure_note():
                return "; ".join(filter(None, [status.error, *address_errors]))
            await queue_reply(status.texts, "immediate", status.delivery_reporting_version, status.turn_ref)
            for output in status.outputs:
                identity = output.identity
                if ref.startswith("deferred:") and self.store.receipt(f"presence:{item.provider_event_key}:final"):
                    # An older worker may have queued this immutable terminal selection
                    # before a crash. Its existing final receipt wins over a new output id.
                    identity = "final"
                await queue_reply((output.text,), identity, status.delivery_reporting_version, output.turn_ref,
                                  getattr(output, "anchor", None))
            if status.state == "pending" and not observe:
                # Released at once: the thread's next mail may follow, and the work
                # named by the reference is observed on a later claim.
                self.store.retry_inbox(item.row_id, item.lease_token, failure_note(), 0)
            elif status.state == "pending":
                # Plain pending keeps its pace; an unresolved or unreadable observation backs off.
                self.store.retry_inbox(item.row_id, item.lease_token, failure_note() or "Host turn pending",
                                       min(60, 2 ** min(item.attempts, 5)) if status.error else 5)
            elif status.state == "failed":
                if self.store.fail_inbox(item.row_id, item.lease_token, failure_note()):
                    self._release_host_reference(status.host_reference or ref)
            else:
                delivery = await self.host.deliver(ref)
                await queue_reply(delivery.texts, "final", delivery.delivery_reporting_version, delivery.turn_ref)
                settled = (self.store.fail_inbox(item.row_id, item.lease_token, failure_note()) if address_errors else
                           self.store.complete_inbox(item.row_id, item.lease_token))
                if settled:
                    self._release_host_reference(status.host_reference or ref)
        except HostBindingTerminalError as exc:
            self.store.fail_inbox(item.row_id, item.lease_token, str(exc))
        except Exception as exc:
            # A capacity refusal keeps the same event first in its thread; other
            # threads' mail is claimed meanwhile.
            note, delay = _retry(exc, item.attempts)
            self.store.retry_inbox(item.row_id, item.lease_token, note, delay)
        return True

    def _release_host_reference(self, reference):
        release = getattr(self.host, "release", None)
        if release:
            release(reference, self.store.active_host_references())

    def _delivery_hold(self, item):
        try:
            modes = binding_modes(item.reporting)
        except ValueError:
            return _BINDINGS_CORRUPT  # A recognised new-format row is never read with legacy defaults.
        try:
            envelope = item.envelope_recipients
        except ValueError:
            envelope = None  # The send itself fails on its empty envelope, as before.
        if envelope is not None and _automatic(item):
            # An accepted automatic reply goes out only with the recipients the Host read before Safety.
            targets = item.reporting.get("reply_targets")
            if not valid_reply_targets(targets):
                return LEGACY_REPLY_HELD
            if _target_keys(targets) != _target_keys(reply_target_rows(envelope)):
                return _TARGETS_DIFFER
        hold = self._conversation_hold(item, modes)
        if hold or envelope is None:
            return hold
        # Every actual envelope address, including rows accepted before recipient bindings existed. An old row
        # stays as accepted: it is checked now and held if an address Place refuses, never rebound or resent.
        return recipient_delivery_hold(self.host, self.account, envelope, item.reporting, modes)

    def _conversation_hold(self, item, modes=None):
        try:
            if reason := self.store.retained_outbox_hold(item):
                return reason
        except RetainedSourceError as exc:
            return 'Held: ' + str(exc)
        conversation = conversation_root(references=item.references, in_reply_to=item.in_reply_to, message_id=item.message_id)
        if "place_binding" in item.reporting:
            check = getattr(self.host, "binding_hold", None)
            binding = item.reporting["place_binding"]
            conversation = binding.get("conversation_id", "") if isinstance(binding, dict) else conversation
            thread = binding.get("thread_id", "") if isinstance(binding, dict) else ""
            hold = (check(binding, self.account, conversation, thread_id=thread,
                          **match_arguments(modes, "place_binding"))
                    if check else "Held: Host cannot check the original Place binding")
        else:
            check = getattr(self.host, "place_hold", None)
            hold = check(self.account, conversation, thread_id=conversation) if check else ""
        if hold or not (item.in_reply_to or item.references):
            return hold
        emitted = emitted_conversation(item.in_reply_to, item.references, item.message_id)
        if emitted == conversation:
            return ""
        # Old queue rows are not rewritten or rebound. Their original authority remains above; additionally check
        # the actual emitted scope now. New rows retain both bindings, so neither disconnect is forgotten.
        check = getattr(self.host, 'binding_hold', None)
        if 'emitted_place_binding' in item.reporting:
            return (check(item.reporting['emitted_place_binding'], self.account, emitted, thread_id=emitted,
                          **match_arguments(modes, 'emitted_place_binding'))
                    if check else 'Held: Host cannot check the emitted Place binding')
        capture = getattr(self.host, 'capture_binding', None)
        if capture:
            # A transient Host failure here is an unread policy, held like binding_hold's: never a spent attempt.
            hold = capture_hold(capture, self.account, emitted)
            if hold is not None:
                return hold
        check = getattr(self.host, 'place_hold', None)
        return check(self.account, emitted, thread_id=emitted) if check else 'Held: Host cannot check the emitted conversation'

    def _sending_off(self):
        current = self.account_loader() if self.account_loader else load_settings(self.store.state_dir)
        return current.get("sending", "on") == "off"

    def process_outbox(self):
        item = self.store.claim_outbox()
        if not item:
            return False
        sending = False
        try:
            # A disconnected account raises AccountError, held below; Sending off holds the same item.
            if self._sending_off():
                self.store.hold_outbox(item, SENDING_OFF_HOLD, kind="sending_off")
                return True
            if getattr(self.client, "transport", None) == "gmail_api":
                # Rule A: a mail that has not gone out goes, as before 0.11, under whichever confirmed sign-in of this
                # mailbox's own Google account serves it now (its verified subject, never an address): the access it
                # was queued with, or none recorded by an earlier version, is replaced by the one named here, and this
                # pass presents exactly that one. With none confirmed now it waits, unsent (AccountError below).
                link = self.client.sending_link()
                if item.reporting.get("gmail_connection") != link:
                    reporting = {**item.reporting, "gmail_connection": link}
                    self.store.set_reporting(item, reporting)
                    item = replace(item, reporting=reporting)
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
            gmail = getattr(self.client, "transport", None) == "gmail_api"
            delivery = ((lambda: self.client.delivery(item)) if gmail
                        else getattr(self.client, "delivery", None) or self.client.smtp)
            with delivery() as smtp:
                # The account and Place policy may have changed while the provider connection opened. Nothing
                # was submitted yet, so a hold here is certain and the same item stays queued for release.
                if self._sending_off():
                    self.store.hold_outbox(item, SENDING_OFF_HOLD, kind="sending_off")
                    return True
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
        except DeliveryDeferred as exc:
            # A read the send needs first was not answered, or Gmail asked to wait: nothing was sent, and it waits,
            # spending no attempt, for the provider's Retry-After (at least the usual 5 s) or a transport pause.
            self.store.wait_outbox(item, str(exc), delay=30 if exc.retry_after is None else max(5, exc.retry_after))
        except DeliveryRejected as exc:
            if exc.retryable and item.attempts < 5:
                self.store.retry_outbox(item.row_id, item.lease_token, str(exc), max(5, exc.retry_after or 0))
            else:
                self.store.fail_outbox(item.row_id, item.lease_token, str(exc), reports=[email_report(item, "failed", error=str(exc))])
        except AccountError as exc:
            # Its access cannot be named or issued now: nothing was sent, and it waits, spending no attempt, for as long
            # as that lasts. A sign-in host that cannot be reached at all is asked again after a longer pause.
            self.store.hold_outbox(item, "Held: " + str(exc), kind="access",
                                   delay=30 if isinstance(exc, HostNotReached) else 5)
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
                                           f"{type(exc).__name__} during mail submission; inspect Sent before retry",
                                           reports=[email_report(item, "uncertain", error=type(exc).__name__)])
            elif item.attempts >= 5:
                self.store.fail_outbox(item.row_id, item.lease_token, type(exc).__name__, reports=[email_report(item, "failed", error=type(exc).__name__)])
            else:
                self.store.retry_outbox(item.row_id, item.lease_token, type(exc).__name__, min(60, 2 ** item.attempts))
        return True

    def process_uncertain(self):
        """Rule A for a Gmail mail whose submission began and whose outcome is unknown: it is never sent a second time.

        Its own mailbox, while a confirmed sign-in of the same Google account serves it, is searched for it by the
        Message-ID it went out with. Found sent, it is recorded sent. Found as a draft (the one it was sent from, or one
        saved for it before), that draft is kept; otherwise one draft of it is saved there (a mail sent from a draft is
        put back as that draft was queued), once the Places of that mailbox's Drafts allow writing there, as for
        email_draft. Either way the owner is told once where it is, or that it waits for those Places. Nothing is drafted
        in another mailbox: with no confirmed sign-in, a policy that cannot be read, or a search or write that fails, it
        is asked about again later. One mail per call; False when none is due."""
        if getattr(self.client, "transport", None) != "gmail_api":
            return False
        item = self.store.claim_uncertain()
        if item is None:
            return False
        address = getattr(self.client, "account", {}).get("address") or self.account
        try:
            link = self.client.sending_link()
            found, details = self.client.find_submitted(item)
            if found == "sent":
                self.store.settle_uncertain(item, "sent", receipt=details)
                return True
            if found is None:
                hold = self._drafts_hold()
                if hold:
                    # A Place (or this skill's access) keeps Mail from writing these Drafts: it waits, and the owner, told
                    # once, decides (allow writing there, or delete it).
                    self.store.defer_uncertain(item, hold, notice=draft_held_notice(item.subject, address,
                                                                                    item.request_id))
                    return True
                if hold is None:
                    self.store.defer_uncertain(item, "The policy of Drafts could not be read now")
                    return True
                try:
                    details = self.client.save_unsent(item, link)
                except (GmailError, ValueError, OSError) as exc:
                    # The draft itself cannot be made (Gmail refused it, or its stored content no longer builds): asking
                    # again changes nothing. An access or transport failure is asked about again later.
                    if isinstance(exc, AccountError) or (isinstance(exc, GmailError) and exc.status not in (400, 404)):
                        raise
                    self.store.settle_uncertain(item, "unresolved", error=type(exc).__name__,
                                                notice=unresolved_notice(item.subject, address, item.request_id))
                    return True
            self.store.settle_uncertain(item, "drafted", draft=details,
                                        notice=drafted_notice(item.subject, address, item.request_id))
        except (AccountError, GmailError, httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
            self.store.defer_uncertain(item, getattr(exc, "mail_diagnostic", None) or type(exc).__name__)
        return True

    def _drafts_hold(self):
        """What the Places of this mailbox's Drafts say about saving a draft there now, asked as email_draft asks for both
        names of Gmail's Drafts: "" when they allow it, the hold when a Place or this skill's access refuses it, None while
        the policy cannot be read."""
        check = getattr(self.host, "draft_folder_hold", None)
        if check is None:
            return ""
        for folder in ("DRAFT", "Drafts"):
            hold = check(self.account, folder, thread_id="")
            if hold == _DRAFT_POLICY_UNREAD:
                return None
            if hold:
                return hold
        return ""

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

        def poll_page():
            count = self.poll()
            # Provider pages already checkpointed; the normal idle delay is for an empty tail.
            return count, (self.store.get("poll_health") or {}).get("more_changes", False)

        async def poller():
            interval = self.poll_interval
            while not stop.is_set():
                try:
                    count, more = await asyncio.to_thread(poll_page)
                    if more:
                        continue  # Re-check stop and Place authority before draining another page.
                    interval = 5 if count else min(self.poll_interval, interval * 2)
                except Exception as exc:
                    reason = getattr(exc, "mail_diagnostic", None) or type(exc).__name__
                    self.store.put("poll_health", {"ok": False, "checked_at": time.time(), "error": reason})
                    log.warning("Mail poll failed: %s", reason)
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
                    if not await asyncio.to_thread(self.process_outbox) and not await asyncio.to_thread(
                            self.process_uncertain):
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
