"""One user session's durable Telegram ↔ Presence bridge.

The companion owns this class and the Telethon client. Each received update first lands
as a private raw event *before* any network/catalog/Host operation. A pending Host event
and an outbound intent each have independent durable identities. A missing receipt is
UNKNOWN, not permission to repeat a Telegram mutation. No Telegram credentials or
Host token enter the project tree or this bridge's event files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from telethon import errors as telegram_errors
from userbot.action_store import ActionStore
from userbot.actions import Actions, ActionAmbiguous, ActionFloodWait, ActionRejected
from userbot.participant_state import policy, receiving_refusal
from userbot.presence import EARLY_CONTINUATION, PresenceClient, anchored_message_id, build_event, busy_delay
from userbot.store import DurableStore
from userbot.telegram import AmbiguousSend, FloodWait, LoginRequired, SendRejected, TelegramAdapter
from userbot.topic import classify_message, reply_target


class OversizedReply(ValueError):
    """A Host answer cannot be sent as one Telegram text message."""


class ReplyConflict(ValueError):
    """An automatic reply identity already names different reply bytes."""


class IngressTooLarge(RuntimeError):
    """An allowlisted update was not captured; stop and reconcile history."""


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Replace only a private-state JSON file, keeping the prior bytes on failure."""
    path = Path(path)
    # Every caller uses a fixed state-root child. Refuse redirected ancestors
    # BEFORE mkdir/write, including a symlinked state root or incoming/outgoing
    # directory; a lexical child of a private root is not proof of confinement.
    for ancestor in (path.parent, *path.parent.parents):
        if ancestor.is_symlink():
            raise ValueError("private state parent is redirected")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("private state destination is redirected")
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    temp = path.with_name(path.name + ".new-" + os.urandom(8).hex())
    try:
        with temp.open("x", encoding="utf-8") as stream:
            if os.name != "nt":
                os.chmod(temp, 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("private event file is missing or redirected")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("private event is not a JSON object")
    return value


def _source_filename(account_id: int, peer_id: int, message_id: int, kind: str, revision: str) -> str:
    source = f"{account_id}:{peer_id}:{message_id}:{kind}:{revision}".encode("utf-8")
    return hashlib.sha256(source).hexdigest() + ".json"


def _binding_revision(config: Any) -> str:
    """Capture enrollment at ingress; later config edits never rebind old speech."""
    bindings = [(item.peer_id, item.topic_id, item.binding_id)
                for item in getattr(config, "bindings", ())]
    source = json.dumps([config.binding_id, config.allowed_peer_ids, bindings],
                        ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class UserSessionBridge:
    """Drive one configured account. No second TelegramClient may own its session."""

    def __init__(self, state_dir: str | Path, config: Any, adapter: TelegramAdapter,
                 store: DurableStore, presence: PresenceClient,
                 presence_factory: Callable[[str], PresenceClient] | None = None,
                 action_store: ActionStore | None = None,
                 actions: Actions | None = None,
                 delivery_reporting_version: int = 1, authorization=None,
                 continuation_version: int = 0) -> None:
        if delivery_reporting_version not in (0, 1):
            raise ValueError("invalid Host delivery version")
        if continuation_version not in (0, EARLY_CONTINUATION):
            raise ValueError("invalid Host continuation version")
        self.state_dir = Path(state_dir)
        # 2 only when the Host listed it at startup; 0 keeps the request waiting for a terminal.
        self.continuation_version = continuation_version
        # A peer whose head the Host answered busy: the monotonic time that head goes again.
        self._busy: dict[int, float] = {}
        # Chats are submitted side by side, each by one task in its own order (intake_pump);
        # events in Host custody are read in a pass of their own (observe_custody).
        self.intake_limit = 3
        self._submitting: dict[int, asyncio.Task] = {}
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._observe_lock = asyncio.Lock()
        self.config = config
        self.adapter = adapter
        self.store = store
        self.presence = presence
        self._presence_factory = presence_factory
        self.action_store = action_store
        self._actions = actions
        self.authorization = authorization
        self.delivery_reporting_version = delivery_reporting_version
        self._presence_clients: dict[str, PresenceClient] = {}
        self._lock = asyncio.Lock()
        self._catalog: dict[int, set[int] | None] = {}
        self._inbox_cursor = 0
        # observe_custody's cycle: the last event in custody it read, and the newest one
        # in custody when the cycle began (0: begin one at the next pass).
        self._custody_cursor = self._custody_through = 0
        self._ingress_failures: dict[int, tuple[str, int]] = {}
        self._blocked_until: dict[int, float] = {}
        # The health file is a projection of unresolved peers, not the last
        # event to finish. Rebuild retry counts on restart; never erase another
        # peer's repair warning when one peer succeeds.
        health_path = self.state_dir / "inbound-health.json"
        if health_path.exists():
            health = _read_json(health_path)
            peers = health.get("peers")
            if type(peers) is not dict or len(peers) > 1000:
                raise ValueError("inbound health is unavailable")
            for key, row in peers.items():
                if (type(key) is not str or not key.lstrip("-").isdigit() or
                        type(row) is not dict or type(row.get("code")) is not str or
                        type(row.get("attempts")) is not int or
                        not 1 <= row["attempts"] <= 100):
                    raise ValueError("inbound health is unavailable")
                self._ingress_failures[int(key)] = (row["code"], row["attempts"])

    def _ingress_health(self, peer: int, code: str) -> None:
        """Project all unresolved peer failures; a successful peer clears only itself."""
        if type(peer) is not int or peer == 0:
            return  # An unidentifiable callback cannot overwrite known diagnostics.
        if code == "none":
            self._ingress_failures.pop(peer, None)
            self._blocked_until.pop(peer, None)
        else:
            prior_code, prior_count = self._ingress_failures.get(peer, ("", 0))
            attempts = min(prior_count + 1 if prior_code == code else 1, 100)
            self._ingress_failures[peer] = (code, attempts)
            if attempts >= 5:
                self._blocked_until[peer] = time.monotonic() + min(600, 30 * 2 ** min(4, attempts - 5))
        # Config permits at most 1000 peers; fail visibly instead of evicting a
        # blocked peer just to fit a diagnostic cap.
        if len(self._ingress_failures) > 1000:
            raise ValueError("inbound health peer limit exceeded")
        rows = {str(pid): {"code": reason, "attempts": attempts,
                           "repair_needed": attempts >= 5}
                for pid, (reason, attempts) in sorted(self._ingress_failures.items())}
        try:
            _atomic_json(self.state_dir / "inbound-health.json", {
                "peers": rows, "at": datetime.now(timezone.utc).isoformat(),
            })
        except (OSError, ValueError):
            pass  # Source event remains pending; status marks malformed diagnostics unknown.

    def _binding_for(self, peer_id: int, topic_id: int | None) -> str | None:
        reader = getattr(self.config, "binding_for", None)
        if callable(reader):
            return reader(peer_id, topic_id)
        # Compatibility only with the test's simple one-peer configuration.
        return self.config.binding_id if peer_id in self.config.allowed_peer_ids else None

    def _excluded(self, peer: int) -> bool:
        """The owner's current exclusions; in legacy mode the enrolled chats are the scope."""
        return receiving_refusal({**policy(self.state_dir), "scope": "all"}, peer,
                                 self.config.account_id) is not None

    def _presence_for(self, binding_id: str) -> PresenceClient:
        if getattr(self.presence, "binding_id", binding_id) == binding_id:
            return self.presence
        if binding_id not in self._presence_clients:
            if self._presence_factory is None:
                raise ValueError("Presence client for exact binding unavailable")
            self._presence_clients[binding_id] = self._presence_factory(binding_id)
        return self._presence_clients[binding_id]

    def spool_update(self, event: Any, *, kind: str = "new") -> Path | None:
        """Capture an inbound Telethon event synchronously before a later await.

        Unsupported media-only events stay out of the model turn; the future media
        capability needs a separately bounded/staged file contract. We do not claim
        to have ingested them as text or delete a legitimate recorded text event.
        """
        message = event.message
        if getattr(message, "out", False):
            return None  # Never echo our own sent messages back into Presence.
        peer, actor, mid = event.chat_id, event.sender_id, message.id
        if any(type(x) is not int or x == 0 for x in (peer, actor, mid)):
            return None  # Telegram service senders need a separate identity policy.
        if peer not in self.config.allowed_peer_ids or self._excluded(peer):
            return None  # Outside the current receiving scope: never captured.
        text = event.raw_text or ""
        if not text.strip():
            return None
        if len(text.encode("utf-8")) > 60000:
            raise IngressTooLarge("Telegram text exceeds the private capture limit")
        revision = ""
        if kind == "edit":
            date = getattr(message, "edit_date", None)
            if date is None:
                raise ValueError("edit event lacks a stable revision")
            # Telegram edit_date has seconds precision. Distinct edits in the
            # same second must not share a Host/source identity; the body hash
            # also keeps an exact redelivery idempotent after a crash.
            revision = (date.isoformat(timespec="microseconds") + ":" +
                        hashlib.sha256(text.encode("utf-8")).hexdigest())
        elif kind != "new":
            raise ValueError("unsupported event kind")
        header = getattr(message, "reply_to", None)
        raw = {
            "account_id": self.config.account_id, "peer_id": peer,
            "binding_id": self.config.binding_id,
            "binding_revision": _binding_revision(self.config),
            "selected_binding": "",
            "actor_id": actor, "message_id": mid, "text": text,
            "kind": kind, "revision": revision,
            "mentioned": bool(getattr(message, "mentioned", False)),
            "forum_topic": bool(getattr(header, "forum_topic", False)),
            "reply_to_top_id": getattr(header, "reply_to_top_id", None),
            "reply_to_msg_id": getattr(header, "reply_to_msg_id", None),
            "action_constructor_id": getattr(getattr(message, "action", None), "CONSTRUCTOR_ID", None),
            "work_ref": "", "selected_topic": None,
        }
        path = self.state_dir / "incoming" / _source_filename(
            self.config.account_id, peer, mid, kind, revision)
        status = self.store.stage_raw(str(self.config.account_id), path.name, raw)
        if status != "pending":
            # A callback replay cannot re-admit a locally settled event.
            if status == "unverifiable":
                self._ingress_health(peer, "EVENT_UNAVAILABLE")
            return None
        if path.exists():
            old = _read_json(path)
            # The old enrollment snapshot and later Host work_ref belong to
            # the original capture. A new owner binding is not a changed
            # Telegram event and must not crash catch-up or rebind it.
            if DurableStore._provider_raw_digest(old) != DurableStore._provider_raw_digest(raw):
                raise ValueError("inbound event identity changed")
            return path
        _atomic_json(path, raw)
        return path

    async def _catalog_for(self, peer: int) -> set[int] | None:
        if peer in self._catalog:
            return self._catalog[peer]
        try:
            catalog = await self.adapter.topic_ids(peer)
        except ValueError as exc:
            # A plain private/group chat has no topic catalog. A monoforum is
            # a different protocol and must not be treated as an ordinary peer.
            entity = await self.adapter.client.get_entity(peer)
            if getattr(entity, "monoforum", False) or getattr(entity, "forum", False):
                raise ValueError("unsupported or unavailable forum catalog") from exc
            catalog = None
        self._catalog[peer] = catalog
        return catalog

    async def _resolve_forum_target(self, peer: int, target_id: int,
                                    catalog: set[int], *, first: Any = None, guard: Any = None) -> int | None:
        """Bounded, cycle-safe ancestry proof for a forum reply lacking top_msg_id.

        Return General only after reaching a fetched headerless ordinary root.
        Unknown, deleted or cyclic chains remain pending; never borrow another
        topic's binding. The same proof is used before outgoing sends. ``guard``,
        when given, runs before each message read and may raise to stop it.
        """
        seen: set[int] = set()
        current = target_id
        for _ in range(16):
            if type(current) is not int or not 0 < current < (1 << 31) or current in seen:
                raise ValueError("forum reply ancestry invalid or cyclic")
            seen.add(current)
            if current == target_id and first is not None:
                target = first
            else:
                if guard is not None:
                    guard()
                target = await self.adapter.client.get_messages(peer, ids=current)
            if target is None or getattr(target, "id", None) != current:
                raise ValueError("forum reply ancestry unavailable")
            try:
                return classify_message(target, catalog)
            except ValueError:
                header = getattr(target, "reply_to", None)
                if (getattr(target, "action", None) is not None or header is None or
                        getattr(header, "reply_to_top_id", None) is not None):
                    raise
                current = getattr(header, "reply_to_msg_id", None)
        raise ValueError("forum reply ancestry exceeds bounded depth")

    async def _eligible(self, raw: dict[str, Any]) -> tuple[bool, int | None]:
        peer = raw["peer_id"]
        catalog = await self._catalog_for(peer)
        topic = None
        if catalog is not None:
            # Duck-type a read-only message with only the Telegram TL fields the
            # canonical topic classifier consumes. Never infer a topic from a
            # non-forum reply chain.
            from types import SimpleNamespace
            header = SimpleNamespace(reply_to_top_id=raw["reply_to_top_id"],
                                     reply_to_msg_id=raw["reply_to_msg_id"],
                                     forum_topic=raw["forum_topic"])
            message = SimpleNamespace(id=raw["message_id"], forum_topic=raw["forum_topic"],
                                      reply_to=header, action=SimpleNamespace(
                                          CONSTRUCTOR_ID=raw["action_constructor_id"]))
            try:
                topic = classify_message(message, catalog)
            except ValueError:
                # An unknown topic may have just been created; re-read exactly
                # once. A still-unknown root remains pending, never General.
                self._catalog.pop(peer, None)
                refreshed = await self._catalog_for(peer)
                try:
                    topic = classify_message(message, refreshed or set())
                except ValueError:
                    if raw["reply_to_top_id"] is not None:
                        # An explicit unknown top is higher-authority routing
                        # evidence than any ancestry of the replied-to ID.
                        # Never replace it with another topic's binding.
                        raise
                    reply_id = raw["reply_to_msg_id"]
                    if type(reply_id) is not int or reply_id <= 0:
                        raise
                    topic = await self._resolve_forum_target(peer, reply_id, refreshed or set())
        # The exact owner-created peer/topic binding is the admission boundary.
        # Do not hardcode a mention/reply wake policy in the transport: a later
        # participant message may matter to the Presence mind even without an
        # @mention, and a deleted reply target cannot retroactively revoke an
        # admitted turn. The behavior profile chooses speech or silence.
        return True, topic

    def _reply_path(self, intent: str) -> Path:
        ref = hashlib.sha256(f"{self.config.account_id}:{intent}".encode()).hexdigest()
        return self.state_dir / "outgoing" / (ref + ".json")

    def _conversation_key(self, raw: dict[str, Any], topic: int | None) -> str:
        """The Host conversation key of this event's chat and topic (General is 0)."""
        return f"telegram:{self.config.account_id}:{raw['peer_id']}:{topic if topic is not None else 0}"

    def _queue_reply(self, raw: dict[str, Any], topic: int | None, binding_id: str,
                     text: str, version: int, source_event_id: str,
                     turn_ref: str, *, phase: str = "final", output_ref: str = "",
                     source: Path | None = None, anchor: Any = None) -> None:
        if phase not in {"interim", "final"}:
            raise ValueError("invalid reply phase")
        if not text.strip() or len(text.encode("utf-8")) > 4096:
            raise OversizedReply("automatic reply exceeds Telegram text boundary")
        prefix = "auto_" + hashlib.sha256(source_event_id.encode("utf-8")).hexdigest()
        # Without a Host output identity an event has one interim and one final reply.
        intent = prefix + ("_interim" if phase == "interim" else "")
        part = "0" if phase == "interim" or self.store.get(str(self.config.account_id),
                                                             prefix + "_interim") is None else "1"
        payload = {
            "text": text, "peer_id": raw["peer_id"], "topic_id": topic,
            # A version-2 output answers the last input it covers when that message is in this
            # chat and topic; otherwise, and without an anchor, the event's own message.
            "reply_id": anchored_message_id(anchor, self._conversation_key(raw, topic)) or raw["message_id"],
            "binding_id": binding_id,
            "source_event_id": source_event_id, "delivery_reporting_version": version,
            "turn_ref": turn_ref, "part_id": part,
        }
        slot = self._reply_path(intent)
        if output_ref:
            # A Host-selected output is queued once by its identity: a new selection is
            # new speech even with the same text, and a parent and its child may both
            # speak. A slot already holding exactly this reply, queued before replies
            # carried that identity, stays the reply of the first selection matching it
            # and of no other; the source event records which one before it is relied on.
            owner = (raw.get("legacy_reply_outputs") or {}).get(intent)
            if owner is None and slot.exists() and _read_json(slot) == payload:
                raw.setdefault("legacy_reply_outputs", {})[intent] = output_ref
                if source is not None:
                    _atomic_json(source, raw)
            elif owner != output_ref:
                intent = prefix + "_" + hashlib.sha256(output_ref.encode("utf-8")).hexdigest()[:32]
                payload.update(part_id="0", output_ref=output_ref)
        path = self._reply_path(intent)
        if path.exists():
            stored = _read_json(path)
            if stored != payload:
                if not (output_ref and stored == {**payload, "reply_id": stored.get("reply_id")}):
                    raise ReplyConflict("automatic reply identity changed")
                # One output identity, read again with another anchor: the target fixed when it
                # was first queued stands, and it is queued once.
                payload = stored
        else:
            _atomic_json(path, payload)
        self.store.enqueue(str(self.config.account_id), intent, peer_id=raw["peer_id"],
                           payload_ref=path.stem, topic_id=topic, reply_id=payload["reply_id"])

    def _queue_pending(self, raw: dict[str, Any], path: Path, topic: int | None,
                       binding_id: str, event_id: str) -> None:
        """Queue replies a response selected before the next reference was stored."""
        interim = raw.get("pending_interim")  # recorded before replies had output identities
        pending = raw.get("pending_replies") or []
        if not interim and not pending:
            return
        if interim:
            try:
                self._queue_reply(raw, topic, binding_id, interim["text"], interim["version"],
                                  event_id, interim["turn_ref"], phase="interim")
            except ReplyConflict:
                # A second interim from the reference still being polled: that reference
                # repeats it with its own output identity on the next read.
                if interim["turn_ref"] != raw.get("work_ref"):
                    raise
        for reply in pending:
            self._queue_reply(raw, topic, binding_id, reply["text"], reply["version"], event_id,
                              reply["turn_ref"], phase=reply["phase"], output_ref=reply["output_ref"],
                              source=path, anchor=reply.get("anchor"))
        raw.pop("pending_interim", None)
        raw.pop("pending_replies", None)
        _atomic_json(path, raw)

    @staticmethod
    def _selected(response: Any, polled: str) -> list[dict[str, Any]]:
        """Speech the Host selected in ended work, in order; released outputs come first.

        Text of failed or cancelled work counts only with the Host's output identity:
        that is an explicit selection, never a recorded failure notice. So does text of a
        ``continuing`` answer or a pending view: only released outputs are speech there.
        A version-2 output keeps the ``reply_anchor`` it carries; an output without one
        (a promoted child's, a retained turn's) answers the event itself.
        """
        producer = response.task_id or response.turn_ref or response.work_ref or polled
        anchors = dict(response.anchors)
        rows = [(ref, outcome, text, anchors.get(ref)) for ref, outcome, text in response.outputs]
        if response.outcome in {"message", "deferred"} and response.text.strip() and (
                response.output_ref or response.status == "completed"):
            rows.append((response.output_ref, response.outcome, response.text, response.reply_anchor))
        return [{"output_ref": ref, "phase": "interim" if outcome == "deferred" else "final",
                 "text": text, "version": response.delivery_reporting_version, "turn_ref": producer,
                 **({"anchor": anchor} if anchor else {})}
                for ref, outcome, text, anchor in rows]

    def _event_for(self, raw: dict[str, Any], topic: int | None) -> dict[str, Any]:
        """The exact Host event of a retained capture; the same bytes on every pass."""
        return build_event(self.config.account_id, raw["peer_id"], raw["message_id"], raw["actor_id"],
                           raw["text"], topic_id=topic, event_kind=raw["kind"], revision=raw["revision"],
                           message={"mentioned": raw["mentioned"], "reply_to_message_id": raw["reply_to_msg_id"]})

    def _custody_refs(self, response: Any) -> list[str]:
        """The references a version-2 answer hands this event's observation to; [] when none.

        ``continuing`` (its author takes the conversation's later events), a ``retained``
        turn and a ``pending`` work view all keep the event in Host custody: the input FIFO
        moves on, and every named reference is read apart until its own end.
        """
        if self.continuation_version != EARLY_CONTINUATION:
            return []
        if response.kind == "continuing":
            refs = [response.continuation_ref or response.turn_ref, response.work_ref, response.child_work_ref]
        elif response.kind == "retained" and response.turn_ref:
            refs = [response.turn_ref, response.work_ref]
        elif response.kind == "pending" and response.work_ref:
            refs = [response.work_ref, response.child_work_ref]
        else:
            return []
        return list(dict.fromkeys(ref for ref in refs if ref))

    def _take_custody(self, raw: dict[str, Any], path: Path, filename: str, refs: list[str],
                      replies: list[dict[str, Any]], topic: int | None, binding_id: str,
                      event_id: str) -> None:
        """Store the references and what was selected, then release the peer's later events.

        The private file names the references before the index marks custody, so a stop in
        between is completed by the next pass; selected speech is queued from the file.
        """
        raw["work_ref"] = refs[0]
        raw["refs"] = refs
        if replies:
            raw["pending_replies"] = replies
        _atomic_json(path, raw)
        self.store.mark_custody(str(self.config.account_id), filename)
        self._queue_pending(raw, path, topic, binding_id, event_id)

    def _busy_wait(self, peer: int, response: Any) -> None:
        """A busy head keeps its peer's later events behind it and goes again, unchanged, after the Host's wait."""
        self._busy[peer] = time.monotonic() + busy_delay(response.field, response.retry_after)

    def _hold(self, raw: dict[str, Any], path: Path, filename: str, code: str | None = None) -> None:
        """Keep the exact source visible for the owner; nothing submits or observes it again."""
        if code:
            raw["failure_code"] = code
            _atomic_json(path, raw)
        held = path.with_suffix(".held")
        if not held.exists():
            os.replace(path, held)
        self.store.settle_raw(str(self.config.account_id), filename, held=True)

    async def _observe_custody(self, filename: str, original: dict[str, Any]) -> int:
        """Read each reference of an event in Host custody once; settle it when none is left.

        A released or terminal output is queued once under its output identity before the
        references that follow it are relied on; a child is read beside its parent until its
        own end. Failed or cancelled work with nothing further to observe keeps its exact
        source held, as before; a deferral naming no work is held too.
        """
        account = str(self.config.account_id)
        peer = original.get("peer_id")
        if self._blocked_until.get(peer, 0) > time.monotonic():
            return 0
        path = self.state_dir / "incoming" / filename
        try:
            raw = _read_json(path)
            refs, binding_id = raw.get("refs"), raw.get("selected_binding")
            ended_refs = raw.get("ended_refs") or []
            if (raw.get("account_id") != self.config.account_id or type(refs) is not list
                    or not all(isinstance(ref, str) and ref for ref in refs) or type(ended_refs) is not list
                    or not isinstance(binding_id, str) or not binding_id):
                raise ValueError("custody event lacks its references")
            topic = raw.get("selected_topic")
            event_id = self._event_for(raw, topic)["source_event_id"]
            presence = self._presence_for(binding_id)
            self._queue_pending(raw, path, topic, binding_id, event_id)
            # An empty list: every reference ended and was stored so; only the settling is left.
            remaining, live = list(refs), []
            while remaining:
                ref = remaining.pop(0)
                response = await asyncio.to_thread(presence.work, ref)
                if response.kind not in {"pending", "completed"}:
                    # A reference read that did not answer changes nothing; read it again later.
                    self._ingress_health(peer, "HOST_UNAVAILABLE" if response.kind in {"retry", "retained"}
                                         else "EVENT_UNAVAILABLE")
                    live.append(ref)
                    continue
                ended = response.kind == "completed"
                replies = self._selected(response, ref)
                if not ended:
                    # A live author's released outputs; each output identity is queued once.
                    replies = [reply for reply in replies if not self._queued(event_id, reply["output_ref"], raw)]
                if any(len(reply["text"].encode("utf-8")) > 4096 for reply in replies):
                    self._hold(raw, path, filename, "REPLY_TOO_LONG")
                    return 0
                child = response.child_work_ref or (response.work_ref if ended and response.work_ref != ref else "")
                if not ended:
                    live.append(ref)
                # A live parent keeps naming its child; one that already ended is not read again.
                fresh = bool(child) and child not in refs + live + remaining + ended_refs
                if fresh:
                    live.append(child)
                if ended:
                    ended_refs = raw["ended_refs"] = list(dict.fromkeys(ended_refs + [ref]))
                    if not child:
                        if response.status != "completed":
                            raw["unfinished"] = "EVENT_UNAVAILABLE"
                        elif response.outcome == "deferred":
                            raw["unfinished"] = "DEFERRED_WITHOUT_WORK_REF"
                if replies or ended or fresh:
                    # Stored with the replies selected so far, which are queued even if this
                    # process stops before the next reference is read.
                    raw["refs"] = list(dict.fromkeys(live + remaining))
                    if replies:
                        raw["pending_replies"] = replies
                    _atomic_json(path, raw)
                    self._queue_pending(raw, path, topic, binding_id, event_id)
            if live:
                if raw["refs"] != live:
                    raw["refs"] = list(dict.fromkeys(live))
                    _atomic_json(path, raw)
                return 0
            if raw.get("unfinished") == "DEFERRED_WITHOUT_WORK_REF":
                self._hold(raw, path, filename, "DEFERRED_WITHOUT_WORK_REF")
                return 0
            if raw.get("unfinished"):
                # Failed or cancelled work with nothing left to follow keeps its exact source
                # visible, after any speech it selected.
                self._ingress_health(peer, "EVENT_UNAVAILABLE")
                self._hold(raw, path, filename)
                return 0
            self.store.ack_inbox(account, event_id)
            path.unlink()
            self.store.settle_raw(account, filename)
            self._ingress_health(peer, "none")
            return 1
        except (ValueError, OSError, RuntimeError, ConnectionError, TimeoutError,
                telegram_errors.RPCError) as exc:
            self._ingress_health(peer if type(peer) is int else 0,
                                 "PRIVATE_STATE_UNAVAILABLE" if isinstance(exc, OSError) else "EVENT_UNAVAILABLE")
            return 0

    def _queued(self, event_id: str, output_ref: str, raw: dict[str, Any]) -> bool:
        """Whether this output identity already has its automatic reply for this event."""
        if output_ref in (raw.get("legacy_reply_outputs") or {}).values():
            return True
        prefix = "auto_" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        return self.store.get(str(self.config.account_id),
                              prefix + "_" + hashlib.sha256(output_ref.encode("utf-8")).hexdigest()[:32]) is not None

    async def process_incoming(self, *, limit: int = 100) -> int:
        """Replay raw, pending-Host and deferred-work events without minting new ids.

        One sequential pass over the chats' heads, then one pass over events in Host custody;
        each holds its own lock, so neither waits for the other. The companion runs them apart
        instead: ``intake_pump`` beside ``observe_custody``.
        """
        count = await self.process_heads(limit=limit)
        return count + await self.observe_custody(limit=limit)

    def _waiting_peers(self) -> set[int]:
        """Chats whose head waits now: repeated failures back off, a busy head waits for the Host's hint."""
        now = time.monotonic()
        return ({peer for peer, until in self._blocked_until.items() if until > now}
                | {peer for peer, until in self._busy.items() if until > now})

    def busy_due_in(self) -> float | None:
        """Seconds until the soonest busy head may be offered again, or None."""
        now = time.monotonic()
        waits = [until - now for until in self._busy.values() if until > now]
        return min(waits) if waits else None

    def _chat_lock(self, peer: int) -> asyncio.Lock:
        return self._chat_locks.setdefault(peer, asyncio.Lock())

    async def process_heads(self, *, limit: int = 100) -> int:
        """One pass over the chats' heads in capture order; returns the events settled.

        With continuation version 2 an event the Host took custody of (``continuing``, a
        retained turn, a pending view, or an end naming a child) no longer holds its peer:
        the peer's next event is offered as its head, while the held event's references are
        read by ``observe_custody``. A busy answer keeps its event the head and offers it
        again after the Host's hint, unchanged; other peers go on meanwhile.
        """
        count = 0
        attempted = 0
        account = str(self.config.account_id)
        async with self._lock:
            # Capture order is the SQLite ordinal assigned synchronously by
            # spool_update. A rotating offset into a shrinking hash-sorted file
            # list can both reorder and skip messages (A,B,C -> A,C,B).
            blocked_peers = self._waiting_peers() | set(self._submitting)
            while attempted < limit:
                next_event = self.store.next_raw(account, blocked_peers, self._inbox_cursor)
                if next_event is None:
                    break
                seq, filename, original = next_event
                self._inbox_cursor = seq
                attempted += 1
                peer = original["peer_id"]
                async with self._chat_lock(peer):
                    head = self.store.next_raw(account, set(), 0, peer=peer)
                    if head is None or head[0] != seq:
                        continue  # another worker moved this chat on meanwhile
                    moved, settled = await self._process_head(filename, original)
                count += settled
                if not moved:
                    blocked_peers.add(peer)
        return count

    def intake_pump(self, on_progress: Callable[[], Any] | None = None) -> None:
        """Start each idle chat's head now, without waiting for another chat's answer.

        One submission task per chat keeps that chat's events in capture order; up to
        ``intake_limit`` chats go side by side, so a Host still opening one chat's author
        never holds another chat whose author already hears it. A finished task's
        unexpected failure surfaces here. ``on_progress`` runs when a task moved its chat
        past at least one event, so the caller looks again at once; a head that waits is
        offered again on the caller's next capture wake or cadence tick, as before.
        """
        for peer, task in list(self._submitting.items()):
            if task.done():
                del self._submitting[peer]
                task.result()
        excluded = self._waiting_peers() | set(self._submitting)
        while len(self._submitting) < self.intake_limit:
            head = self.store.next_raw(str(self.config.account_id), excluded, 0)
            if head is None:
                break
            peer = head[2]["peer_id"]
            excluded.add(peer)
            task = asyncio.create_task(self._submit_chat(peer), name="telegram-legacy-chat-intake")
            if on_progress is not None:
                task.add_done_callback(lambda done: on_progress() if not done.cancelled() and (
                    done.exception() is not None or done.result()) else None)
            self._submitting[peer] = task

    async def _submit_chat(self, peer: int, *, limit: int = 100) -> int:
        """This chat's heads in capture order, one at a time, until one waits; heads moved past."""
        moved = 0
        async with self._chat_lock(peer):
            while moved < limit and peer not in self._waiting_peers():
                head = self.store.next_raw(str(self.config.account_id), set(), 0, peer=peer)
                if head is None:
                    break
                went, _settled = await self._process_head(head[1], head[2])
                if not went:
                    break
                moved += 1
        return moved

    async def stop_intake(self) -> None:
        """Cancel and join every chat's submission; its event stays pending for the next start."""
        tasks = list(self._submitting.values())
        self._submitting.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def observe_custody(self, *, limit: int = 100) -> int:
        """Read each event in Host custody once, apart from submission; returns the events settled.

        Its own lock: a slow work read or many held references never hold a chat's next event.
        """
        count = 0
        account = str(self.config.account_id)
        async with self._observe_lock:
            # Bounded passes cycle through custody: a cycle reads, in capture order, the events
            # up to the newest in custody when it began, ``limit`` per pass from the last one read.
            # Then the next cycle begins, oldest first, in the same pass. Pending older events
            # never hide a later one, and events arriving meanwhile never hide the older ones.
            if not self._custody_through:
                self._custody_cursor, self._custody_through = 0, self.store.custody_high(account)
            batch = self.store.custody_raw(account, limit=limit, after_seq=self._custody_cursor,
                                           through_seq=self._custody_through)
            if len(batch) < limit:
                # Only events before the cursor: none of them is in this pass yet.
                batch += self.store.custody_raw(account, limit=limit - len(batch),
                                                through_seq=self._custody_cursor)
                self._custody_through = self.store.custody_high(account)
            for seq, filename, original in batch:
                self._custody_cursor = seq
                count += await self._observe_custody(filename, original)
        return count

    async def _process_head(self, filename: str, original: dict[str, Any]) -> tuple[bool, int]:
        """Submit, observe or settle one chat's head: (whether the chat moves on now, events settled).

        False means the head waits where it is (a retry, a busy answer, a version-0
        observation or a failure); the chat's later events stay behind it.
        """
        account = str(self.config.account_id)
        path = self.state_dir / "incoming" / filename
        if not path.exists():
            _atomic_json(path, original)  # crash between index and file
        raw = _read_json(path)
        if raw.get("account_id") != self.config.account_id:
            self.store.settle_raw(account, filename, held=True)
            return True, 0
        peer = raw["peer_id"]
        if raw.get("refs") is not None:
            # Its references were stored before a stop could mark custody in the index.
            self.store.mark_custody(account, filename)
            return True, 0
        if raw.get("binding_revision") != _binding_revision(self.config):
            # A later owner binding cannot inherit a prior binding's
            # unread Telegram event, even in the same peer.
            self.store.settle_raw(account, filename, held=True)
            os.replace(path, path.with_suffix(".held"))
            return True, 0
        if raw["peer_id"] not in self.config.allowed_peer_ids:
            # A revocation stops admission, including old raw spools.
            self.store.settle_raw(account, filename, held=True)
            os.replace(path, path.with_suffix(".held"))
            return True, 0
        try:
            work_ref = raw.get("work_ref") or ""
            binding_id = raw.get("selected_binding") or ""
            if not work_ref and self._excluded(peer):
                # Excluded after capture: its content is never submitted. Work the
                # Host already admitted keeps its custody; replies to an excluded
                # chat are refused at dispatch.
                self.store.settle_raw(account, filename, held=True)
                os.replace(path, path.with_suffix(".held"))
                return True, 0
            if work_ref and binding_id:
                # Admission already happened. Do not re-fetch a replied
                # message that may have since been deleted: its absence
                # cannot erase an admitted work handle.
                topic = raw.get("selected_topic")
            else:
                eligible, topic = await self._eligible(raw)
                if not eligible:
                    path.unlink()
                    self.store.settle_raw(account, filename)
                    return True, 1
                binding_id = self._binding_for(raw["peer_id"], topic)
            if binding_id is None:
                # No owner-created exact binding: not an owner message,
                # and never deliver this event to another room later.
                os.replace(path, path.with_suffix(".held"))
                self.store.settle_raw(account, filename, held=True)
                return True, 0
            if binding_id != self._binding_for(raw["peer_id"], topic):
                self.store.settle_raw(account, filename, held=True)
                return True, 0
            if raw.get("selected_binding") not in ("", binding_id):
                raise ValueError("event selected another binding")
            if raw.get("selected_binding") != binding_id:
                raw["selected_binding"] = binding_id
                raw["selected_topic"] = topic
                _atomic_json(path, raw)
            event = self._event_for(raw, topic)
            event_id = event["source_event_id"]
            self.store.reserve_inbox(account, event_id, event)
            # A crash after storing the next reference must still queue what the
            # previous response selected, not skip straight to the child.
            self._queue_pending(raw, path, topic, binding_id, event_id)
            presence = self._presence_for(binding_id)
            if not work_ref and self._excluded(peer):
                # Excluded while the provider reads above were awaited: the
                # first submission is checked again with nothing awaited after.
                self.store.settle_raw(account, filename, held=True)
                os.replace(path, path.with_suffix(".held"))
                return True, 0
            if work_ref and self.continuation_version == EARLY_CONTINUATION:
                # Admitted work that names no references (stored by version 0, before this
                # Host listed version 2): the Host already holds it. Under its current
                # binding it moves to custody once, read apart from here on and never
                # submitted again; the replies it stored were queued just above.
                self._take_custody(raw, path, filename, [work_ref], [], topic, binding_id, event_id)
                return True, 0
            response = await asyncio.to_thread(presence.work, work_ref) if work_ref else (
                await asyncio.to_thread(presence.turn, event,
                                        delivery_reporting_version=self.delivery_reporting_version,
                                        continuation_version=self.continuation_version))
            if not work_ref and self.continuation_version == EARLY_CONTINUATION:
                if response.kind == "retry" and response.code == "presence_capacity_full":
                    # Its conversation or every active slot is held: nothing ran. The
                    # same event stays the peer's head and goes again after the Host's hint.
                    self._busy_wait(peer, response)
                    return False, 0
                self._busy.pop(peer, None)
                custody = self._custody_refs(response)
                if custody:
                    replies = self._selected(response, "") if response.kind == "continuing" else []
                    if any(len(reply["text"].encode("utf-8")) > 4096 for reply in replies):
                        self._hold(raw, path, filename, "REPLY_TOO_LONG")
                        return True, 0
                    self._take_custody(raw, path, filename, custody, replies, topic, binding_id, event_id)
                    return True, 0
            if response.kind == "retained" and response.turn_ref:
                # The Host keeps the admitted turn: observe its original
                # reference instead of submitting the same event again.
                raw["work_ref"] = response.turn_ref
                _atomic_json(path, raw)
                return False, 0
            if response.kind != "completed":
                if response.work_ref and response.work_ref != work_ref:
                    raw["work_ref"] = response.work_ref
                    _atomic_json(path, raw)
                if response.kind == "pending":
                    return False, 0
                self._ingress_health(peer, "HOST_UNAVAILABLE" if response.kind in {"retry", "retained"}
                                     else "EVENT_UNAVAILABLE")
                if response.work_ref or work_ref:
                    return False, 0
                if response.kind in {"blocked", "rejected"}:
                    # Do not pay for the same permanent refusal every
                    # 3 seconds. Retain exact private bytes as a held
                    # event until a deliberate owner repair/replay.
                    held = path.with_suffix(".held")
                    if not held.exists():
                        os.replace(path, held)
                    self.store.settle_raw(account, filename, held=True)
                    return True, 0
                return False, 0
            # Ended work: queue what its producer selected, whatever the task's
            # status, then follow a separately admitted child. A turn names its
            # child in work_ref; a work read names itself there.
            replies = self._selected(response, work_ref)
            if any(len(reply["text"].encode("utf-8")) > 4096 for reply in replies):
                # Do not ACK an answer we cannot deliver. Hold the
                # exact source for owner correction instead of
                # repeatedly posting an unsendable job.
                raw["failure_code"] = "REPLY_TOO_LONG"
                _atomic_json(path, raw)
                os.replace(path, path.with_suffix(".held"))
                self.store.settle_raw(account, filename, held=True)
                return True, 0
            child = response.child_work_ref or (
                response.work_ref if response.work_ref != work_ref else "")
            if child and child != work_ref:
                if not work_ref and self.continuation_version == EARLY_CONTINUATION:
                    # The author ended within the request and promoted a child: the
                    # child is read apart and this peer's next event is offered.
                    self._take_custody(raw, path, filename, [child], replies, topic, binding_id, event_id)
                    return True, 0
                # Stored with the replies selected so far, which are queued even
                # if this process stops before the child is read.
                raw["work_ref"], raw["pending_replies"] = child, replies
                _atomic_json(path, raw)
                self._queue_pending(raw, path, topic, binding_id, event_id)
                return False, 0
            for reply in replies:
                self._queue_reply(raw, topic, binding_id, reply["text"], reply["version"],
                                  event_id, reply["turn_ref"], phase=reply["phase"],
                                  output_ref=reply["output_ref"], source=path,
                                  anchor=reply.get("anchor"))
            if response.outcome == "deferred" and response.status == "completed":
                raw["failure_code"] = "DEFERRED_WITHOUT_WORK_REF"
                _atomic_json(path, raw)
                os.replace(path, path.with_suffix(".held"))
                self.store.settle_raw(account, filename, held=True)
                return True, 0
            if response.status != "completed":
                # Failed or cancelled work with nothing left to follow keeps its
                # exact source visible, after any speech it selected.
                self._ingress_health(peer, "EVENT_UNAVAILABLE")
                self.store.settle_raw(account, filename, held=True)
                os.replace(path, path.with_suffix(".held"))
                return True, 0
            self.store.ack_inbox(account, event_id)
            path.unlink()
            self.store.settle_raw(account, filename)
            self._ingress_health(peer, "none")
            return True, 1
        except (ValueError, OSError, RuntimeError, ConnectionError,
                TimeoutError, telegram_errors.RPCError) as exc:
            # Failed catalog, provider fetch or disk write stays raw/pending;
            # retry after bounded pacing in the companion loop. Do not
            # catch cancellation, and never include private text in logs.
            code = ("TELEGRAM_READ" if isinstance(exc, telegram_errors.RPCError)
                    else "PRIVATE_STATE_UNAVAILABLE" if isinstance(exc, OSError)
                    else "EVENT_UNAVAILABLE")
            self._ingress_health(peer, code)
            return False, 0

    async def _authorize_write(self, peer, topic, binding):
        if self.authorization is None:
            raise ValueError('current dispatch authorization is required')
        self.authorization.current(peer, True, topic=topic, expected_binding=binding)
        if await self.adapter.get_me() != int(self.authorization.account):
            raise ValueError('authenticated account changed')
        await self.authorization.authorize(peer, topic or '', True, expected_binding=binding)

    def _flood_remaining(self):
        from .participant_state import Journal
        with Journal(self.state_dir) as journal:
            return journal.flood_remaining(str(self.config.account_id))

    def _record_flood(self, seconds):
        from .participant_state import Journal
        with Journal(self.state_dir) as journal:
            journal.flood(str(self.config.account_id), seconds)

    async def send_one(self) -> bool:
        """At most ONE unattempted outbox job; no retry of unknown/sent effects."""
        account = str(self.config.account_id)
        if self._flood_remaining(): return False
        job = self.store.claim(account)
        if job is None:
            return False
        ref = job.payload_ref
        try:
            payload = _read_json(self.state_dir / "outgoing" / (ref + ".json"))
        except (ValueError, OSError):
            self.store.reject_before_send(account, job.intent_id, job.lease_token,
                                          "PAYLOAD_UNAVAILABLE")
            return True
        if (payload.get("peer_id") != job.peer_id or payload.get("topic_id") != job.topic_id
                or payload.get("reply_id") != job.reply_id or
                payload.get("binding_id") != self._binding_for(job.peer_id, job.topic_id) or
                not payload.get("binding_id") or
                not isinstance(payload.get("text"), str)
                or not payload["text"].strip() or
                len(payload["text"].encode("utf-8")) > 4096 or
                (job.reply_id is not None and not 0 < job.reply_id < (1 << 31)) or
                job.peer_id not in self.config.allowed_peer_ids):
            self.store.reject_before_send(account, job.intent_id, job.lease_token,
                                          "PAYLOAD_OR_PERMISSION_CHANGED")
            return True
        # Topics can be renamed/deleted while this process lives. An earlier
        # inbound catalog is never authority for a NEW outgoing mutation.
        self._catalog.pop(job.peer_id, None)
        try:
            catalog = await self._catalog_for(job.peer_id)
        except ValueError:
            self.store.reject_before_send(account, job.intent_id, job.lease_token,
                                          "TOPIC_UNAVAILABLE")
            return True
        except (OSError, TimeoutError, RuntimeError, ConnectionError,
                telegram_errors.RPCError):
            # No mutation started; let the pre-attempt lease expire and retry
            # only after the catalog becomes readable.
            return False
        try:
            if catalog is not None:
                reply_target(job.topic_id, job.reply_id, catalog)
            elif job.topic_id not in (None, 1):
                raise ValueError("topic specified for non-forum peer")
        except ValueError:
            # Refresh once before concluding a topic is deleted/invalid. No
            # network send has begun; a still invalid intent retires so it
            # cannot starve later valid jobs behind the FIFO claim.
            self._catalog.pop(job.peer_id, None)
            try:
                catalog = await self._catalog_for(job.peer_id)
                if catalog is not None:
                    reply_target(job.topic_id, job.reply_id, catalog)
                elif job.topic_id not in (None, 1):
                    raise ValueError("topic specified for non-forum peer")
            except ValueError:
                self.store.reject_before_send(account, job.intent_id,
                                              job.lease_token, "TOPIC_UNAVAILABLE")
                return True
            except (OSError, TimeoutError, RuntimeError, ConnectionError,
                    telegram_errors.RPCError):
                return False  # The refreshed catalog still is not readable.
        if job.reply_id is not None:
            # Prevent cross-topic replies even if an agent supplies an arbitrary ID.
            try:
                target = await self.adapter.client.get_messages(job.peer_id, ids=job.reply_id)
            except (OSError, TimeoutError, ConnectionError, RuntimeError,
                    telegram_errors.RPCError):
                # The preflight has not crossed mark_attempted. Preserve the
                # leased intent and let its exact lease expire; never restart
                # the companion on an ordinary transient provider read.
                return False
            try:
                if target is None or getattr(target, "id", None) != job.reply_id:
                    same_topic = False
                elif catalog is None:
                    same_topic = job.topic_id is None
                else:
                    try:
                        found_topic = classify_message(target, catalog)
                    except ValueError:
                        found_topic = await self._resolve_forum_target(
                            job.peer_id, job.reply_id, catalog, first=target)
                    same_topic = found_topic == job.topic_id
            except ValueError:
                same_topic = False
            except (OSError, TimeoutError, ConnectionError, RuntimeError,
                    telegram_errors.RPCError):
                return False  # Still before the durable send-attempt boundary.
            if not same_topic:
                self.store.reject_before_send(account, job.intent_id,
                                              job.lease_token, "REPLY_UNAVAILABLE")
                return True
        async def before_mutation():
            await self._authorize_write(job.peer_id, job.topic_id, payload['binding_id'])
        try:
            await before_mutation()
        except ValueError:
            self.store.reject_before_send(account, job.intent_id, job.lease_token, 'CURRENT_POLICY_CHANGED')
            return True
        except (OSError, RuntimeError, TimeoutError, LoginRequired, telegram_errors.RPCError):
            return False
        try:
            marked = self.store.mark_attempted(account, job.intent_id, job.lease_token)
        except ValueError:
            # The preflight (catalog refreshes plus reply-target fetch) can
            # outlast the 60s claim lease. Losing the lease is not a send and
            # not a crash: the leased-but-unattempted intent expires and is
            # re-claimed later with the SAME intent_id and frozen random_id.
            return False
        try:
            result = await self.adapter.send_text(job.peer_id, payload["text"],
                                                  random_id=marked.random_id,
                                                  topic_id=job.topic_id, reply_id=job.reply_id,
                                                  before_mutation=before_mutation)
        except (AmbiguousSend, LoginRequired):
            # The sent boundary was crossed; even a login error here does not
            # authorize a fresh random_id. Lease expiry/startup makes UNKNOWN.
            return True
        except (ValueError, OSError, RuntimeError):
            # Even a failure during the adapter's second entity resolution is
            # not proof that the provider call never began. Preserve UNKNOWN
            # without crashing/restarting the entire companion.
            return True
        except (FloodWait, SendRejected) as exc:
            if isinstance(exc, FloodWait): self._record_flood(exc.retry_after)
            code = "FLOOD_WAIT" if isinstance(exc, FloodWait) else "RPC_REJECTED"
            self.store.settle(account, job.intent_id, job.lease_token,
                              success=False, error_code=code)
            return True
        if result.message_id is None:
            # A successful transport response without a matching UpdateMessageID
            # does not supply a delivery receipt. Keep the attempted intent;
            # expiry or exclusive recovery moves it to unknown without resend.
            return True
        self.store.settle(account, job.intent_id, job.lease_token,
                          success=True, remote_message_id=result.message_id)
        return True

    async def process_action_once(self) -> bool:
        """Dispatch one owner-enqueued mutation; an attempted effect is never replayed.

        The exclusive companion owns the ActionStore and the Telegram session.
        The owner-created exact binding is checked *again* at dispatch; neither a
        Presence participant nor an old enrollment can authorize this method.
        """
        if self.action_store is None:
            return False
        from telethon import errors as telegram_errors

        account = str(self.config.account_id)
        if self._flood_remaining(): return False
        job = self.action_store.claim(account)
        if job is None:
            return False
        token = job.lease_token

        def lease_lost() -> bool:
            # The preflight (catalog refresh, target fetch, get_me) can outlast
            # the 60s claim lease. Losing the lease is not a mutation and not a
            # crash: mirror send_one and let the leased-but-unattempted intent
            # expire and be re-claimed with the same intent_id.
            return False

        def reject(code: str) -> bool:
            try:
                self.action_store.reject_before_attempt(account, job.intent_id, token, code)
            except ValueError:
                return lease_lost()
            return True

        def postpone_flood(seconds: Any) -> bool:
            self._record_flood(seconds)
            if type(seconds) is not int or seconds > 30 * 86400:
                return reject("FLOOD_WAIT_TOO_LONG")
            try:
                self.action_store.release_before_attempt(
                    account, job.intent_id, token, delay_seconds=max(30, seconds))
            except ValueError:
                return lease_lost()
            return False

        # Unlike legacy text-send compatibility, owner action authority must
        # come from Config.binding_for and an immutable ActionStore identity.
        binding_for = getattr(self.config, "binding_for", None)
        try:
            bound = binding_for(job.peer_id, job.topic_id) if callable(binding_for) else None
        except ValueError:
            bound = None
        if (self.config.account_id is None or job.peer_id not in self.config.allowed_peer_ids
                or bound is None or bound != job.binding_id):
            return reject("BINDING_OR_PERMISSION_CHANGED")
        try:
            if await self.adapter.get_me() != self.config.account_id:
                return reject("ACCOUNT_CHANGED")
        except ValueError:
            return reject("ACCOUNT_UNAVAILABLE")
        except asyncio.CancelledError:
            try:
                self.action_store.release_before_attempt(account, job.intent_id, token)
            except ValueError:
                pass
            raise
        except telegram_errors.FloodWaitError as exc:
            return postpone_flood(exc.seconds)
        except (LoginRequired, OSError, TimeoutError, telegram_errors.RPCError):
            try:
                self.action_store.release_before_attempt(account, job.intent_id, token,
                                                         delay_seconds=30)
            except ValueError:
                pass
            return False

        payload: dict[str, Any] | None = None
        if job.operation in {"edit", "react"}:
            try:
                ref = job.payload_ref
                path = self.state_dir / "actions" / (ref + ".json")
                if path.is_symlink() or not path.is_file():
                    raise ValueError("action payload unavailable")
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != job.payload_sha256:
                    raise ValueError("action payload digest changed")
                payload = json.loads(data)
                if not isinstance(payload, dict) or any(payload.get(key) != expected for key, expected in (
                        ("binding_id", job.binding_id), ("operation", job.operation),
                        ("peer_id", job.peer_id), ("topic_id", job.topic_id),
                        ("message_id", job.message_id))):
                    raise ValueError("action payload scope changed")
                field = "text" if job.operation == "edit" else "emoji"
                if not isinstance(payload.get(field), str) or not payload[field].strip():
                    raise ValueError("action payload empty")
            except (ValueError, TypeError, OSError, UnicodeError, json.JSONDecodeError):
                return reject("PAYLOAD_UNAVAILABLE")

        actions = self._actions
        if actions is None:
            actions = Actions(self.adapter, set(self.config.allowed_peer_ids))
        try:
            # An independent preflight before the attempted boundary catches
            # stale topic, absent target and foreign authorship as invalid jobs.
            # Actions repeats its own preflight at execution (TOCTOU guard).
            target = actions._own_target if job.operation in {"edit", "delete"} else actions._target
            _, message = await target(job.peer_id, job.message_id, job.topic_id)
            if job.operation == "edit" and (
                    getattr(message, "media", None) is not None or getattr(message, "entities", None)):
                return reject("EDIT_TARGET_UNSUPPORTED")
        except ValueError:
            return reject("TARGET_OR_TOPIC_UNAVAILABLE")
        except asyncio.CancelledError:
            try:
                self.action_store.release_before_attempt(account, job.intent_id, token)
            except ValueError:
                pass
            raise
        except ActionFloodWait as exc:
            return postpone_flood(exc.retry_after)
        except telegram_errors.FloodWaitError as exc:
            return postpone_flood(exc.seconds)
        except (LoginRequired, ActionAmbiguous, OSError, TimeoutError, RuntimeError,
                telegram_errors.RPCError):
            # No physical mutation has started. Release for a later preflight;
            # a lease lost during the slow preflight is not a crash.
            try:
                self.action_store.release_before_attempt(account, job.intent_id, token,
                                                         delay_seconds=30)
            except ValueError:
                pass
            return False

        async def before_mutation():
            await self._authorize_write(job.peer_id, job.topic_id, job.binding_id)
        try:
            await before_mutation()
        except ValueError:
            return reject('CURRENT_POLICY_CHANGED')
        except (OSError, RuntimeError, TimeoutError, LoginRequired, telegram_errors.RPCError):
            return False
        try:
            self.action_store.mark_attempted(account, job.intent_id, token)
        except ValueError:
            # Same class as send_one: a preflight that outlives the claim lease
            # degrades to a deferred re-claim, never a companion exit.
            return False
        try:
            if job.operation == "edit":
                await actions.edit(job.peer_id, job.message_id, payload["text"], topic_id=job.topic_id, before_mutation=before_mutation)
            elif job.operation == "delete":
                await actions.delete(job.peer_id, job.message_id, topic_id=job.topic_id, before_mutation=before_mutation)
            else:
                await actions.react(job.peer_id, job.message_id, payload["emoji"], topic_id=job.topic_id, before_mutation=before_mutation)
        except (ActionRejected, ActionFloodWait) as exc:
            if isinstance(exc, ActionFloodWait): self._record_flood(exc.retry_after)
            code = "FLOOD_WAIT" if isinstance(exc, ActionFloodWait) else "RPC_REJECTED"
            self.action_store.settle(account, job.intent_id, token, success=False, error_code=code)
        except asyncio.CancelledError:
            self.action_store.mark_unknown(account, job.intent_id, token)
            raise
        except Exception:
            # A repeated internal preflight or transport error after the durable
            # mark is not proof that no RPC reached Telegram. No auto retry.
            self.action_store.mark_unknown(account, job.intent_id, token)
        else:
            self.action_store.settle(account, job.intent_id, token, success=True)
        return True

    async def report_sent(self, *, limit: int = 100) -> int:
        """Replay Host reporting only; a failed report NEVER re-sends Telegram."""
        count = 0
        account = str(self.config.account_id)
        for job in self.store.unreported_auto_sends(account, limit=limit):
            path = self.state_dir / "outgoing" / (job.payload_ref + ".json")
            if not path.exists():
                self.store.mark_report_invalid(account, job.intent_id)
                continue
            try:
                payload = _read_json(path)
                # A malformed auto-reply cannot authorize a Host receipt. Keep
                # the original bytes for inspection, but move them out of the
                # bounded .json scan so healthy later receipts can progress.
                source = payload.get("source_event_id")
                if source is not None and (not isinstance(source, str) or not source):
                    raise ValueError("invalid source event id")
                if source and payload.get("delivery_reporting_version") == 1:
                    if ("topic_id" not in payload or
                            type(payload.get("peer_id")) is not int or
                            type(payload.get("topic_id")) not in (int, type(None)) or
                            not isinstance(payload.get("binding_id"), str) or
                            not isinstance(payload.get("text"), str) or
                            not isinstance(payload.get("output_ref", ""), str) or
                            payload.get("part_id", "0") not in {"0", "1"}):
                        raise ValueError("invalid delivery payload fields")
            except (ValueError, TypeError, UnicodeError, OSError):
                quarantine = path.with_name(path.name + ".invalid-" + os.urandom(8).hex())
                try:
                    os.replace(path, quarantine)
                    _atomic_json(quarantine.with_name(quarantine.name + ".diagnostic"),
                                 {"error_code": "MALFORMED_DELIVERY_PAYLOAD",
                                  "original_name": path.name})
                    self.store.mark_report_invalid(account, job.intent_id)
                except OSError:
                    # No receipt is sent. If quarantine failed, leave the
                    # original for another pass; do not crash the companion.
                    pass
                continue
            source = payload.get("source_event_id")
            if source and payload.get("delivery_reporting_version") == 0:
                self.store.mark_report_unsupported(account, job.intent_id)
                continue
            if not source or payload.get("delivery_reporting_version") != 1:
                self.store.mark_report_invalid(account, job.intent_id)
                continue
            if payload.get("binding_id") != self._binding_for(payload["peer_id"], payload["topic_id"]):
                # Provider send may already have happened; reporting through a
                # new owner binding would misattribute this historical effect.
                self.store.mark_report_invalid(account, job.intent_id)
                continue
            receipt = path.with_suffix(".reported")
            if receipt.exists():
                self.store.mark_reported(account, job.intent_id)
                continue
            # Only an immutable automatic intent uses this naming contract. A reply
            # with a Host output identity is its own delivery.
            prefix = "auto_" + hashlib.sha256(source.encode("utf-8")).hexdigest()
            output_ref = payload.get("output_ref", "")
            delivery_id = (prefix + "_" + hashlib.sha256(output_ref.encode("utf-8")).hexdigest()[:32]
                           if output_ref else prefix)
            allowed = {delivery_id} if output_ref else {prefix, prefix + "_interim"}
            if job.intent_id not in allowed or job.remote_message_id is None:
                self.store.mark_report_invalid(account, job.intent_id)
                continue
            result = await asyncio.to_thread(
                self._presence_for(payload["binding_id"]).report_delivered,
                delivery_id=delivery_id, part_id=payload.get("part_id", "0"),
                provider_message_id=job.remote_message_id,
                account_id=self.config.account_id, peer_id=job.peer_id,
                topic_id=job.topic_id, text=payload["text"],
                source_event_id=source, task_id=payload.get("turn_ref") or "",
                delivery_reporting_version=1)
            if result.kind == "completed":
                _atomic_json(receipt, {"delivery_id": delivery_id, "message_id": job.remote_message_id})
                self.store.mark_reported(account, job.intent_id)
                count += 1
            else:
                self.store.defer_report(account, job.intent_id)
        return count
