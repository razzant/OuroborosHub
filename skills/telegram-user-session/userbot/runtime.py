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
from userbot.presence import PresenceClient, build_event
from userbot.store import DurableStore
from userbot.telegram import AmbiguousSend, FloodWait, LoginRequired, SendRejected, TelegramAdapter
from userbot.topic import classify_message, reply_target


class OversizedReply(ValueError):
    """A Host answer cannot be sent as one Telegram text message."""


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
                 delivery_reporting_version: int = 1, authorization=None) -> None:
        if delivery_reporting_version not in (0, 1):
            raise ValueError("invalid Host delivery version")
        self.state_dir = Path(state_dir)
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
        if peer not in self.config.allowed_peer_ids:
            return None
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
                                    catalog: set[int], *, first: Any = None) -> int | None:
        """Bounded, cycle-safe ancestry proof for a forum reply lacking top_msg_id.

        Return General only after reaching a fetched headerless ordinary root.
        Unknown, deleted or cyclic chains remain pending; never borrow another
        topic's binding. The same proof is used before outgoing sends.
        """
        seen: set[int] = set()
        current = target_id
        for _ in range(16):
            if type(current) is not int or not 0 < current < (1 << 31) or current in seen:
                raise ValueError("forum reply ancestry invalid or cyclic")
            seen.add(current)
            target = first if current == target_id and first is not None else (
                await self.adapter.client.get_messages(peer, ids=current))
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

    def _queue_reply(self, raw: dict[str, Any], topic: int | None, binding_id: str,
                     text: str, version: int, source_event_id: str,
                     turn_ref: str, *, phase: str = "final") -> None:
        if phase not in {"interim", "final"}:
            raise ValueError("invalid reply phase")
        if not text.strip() or len(text.encode("utf-8")) > 4096:
            raise OversizedReply("automatic reply exceeds Telegram text boundary")
        prefix = "auto_" + hashlib.sha256(source_event_id.encode("utf-8")).hexdigest()
        intent = prefix + ("_interim" if phase == "interim" else "")
        ref = hashlib.sha256(f"{self.config.account_id}:{intent}".encode()).hexdigest()
        part = "0" if phase == "interim" or self.store.get(str(self.config.account_id),
                                                             prefix + "_interim") is None else "1"
        payload = {
            "text": text, "peer_id": raw["peer_id"], "topic_id": topic,
            "reply_id": raw["message_id"],
            "binding_id": binding_id,
            "source_event_id": source_event_id, "delivery_reporting_version": version,
            "turn_ref": turn_ref, "part_id": part,
        }
        path = self.state_dir / "outgoing" / (ref + ".json")
        if path.exists():
            if _read_json(path) != payload:
                raise ValueError("automatic reply identity changed")
        else:
            _atomic_json(path, payload)
        self.store.enqueue(str(self.config.account_id), intent, peer_id=raw["peer_id"],
                           payload_ref=ref, topic_id=topic, reply_id=raw["message_id"])

    async def process_incoming(self, *, limit: int = 100) -> int:
        """Replay raw, pending-Host and deferred-work events without minting new ids."""
        count = 0
        attempted = 0
        async with self._lock:
            # Capture order is the SQLite ordinal assigned synchronously by
            # spool_update. A rotating offset into a shrinking hash-sorted file
            # list can both reorder and skip messages (A,B,C -> A,C,B).
            now = time.monotonic()
            blocked_peers: set[int] = {peer for peer, until in self._blocked_until.items()
                                       if until > now}
            while attempted < limit:
                next_event = self.store.next_raw(str(self.config.account_id), blocked_peers,
                                                 self._inbox_cursor)
                if next_event is None:
                    break
                seq, filename, original = next_event
                self._inbox_cursor = seq
                attempted += 1
                path = self.state_dir / "incoming" / filename
                if not path.exists():
                    _atomic_json(path, original)  # crash between index and file
                raw = _read_json(path)
                if raw.get("account_id") != self.config.account_id:
                    self.store.settle_raw(str(self.config.account_id), filename, held=True)
                    continue
                peer = raw["peer_id"]
                if peer in blocked_peers:
                    continue
                if raw.get("binding_revision") != _binding_revision(self.config):
                    # A later owner binding cannot inherit a prior binding's
                    # unread Telegram event, even in the same peer.
                    self.store.settle_raw(str(self.config.account_id), filename, held=True)
                    os.replace(path, path.with_suffix(".held"))
                    continue
                if raw["peer_id"] not in self.config.allowed_peer_ids:
                    # A revocation stops admission, including old raw spools.
                    self.store.settle_raw(str(self.config.account_id), filename, held=True)
                    os.replace(path, path.with_suffix(".held"))
                    continue
                try:
                    work_ref = raw.get("work_ref") or ""
                    binding_id = raw.get("selected_binding") or ""
                    if work_ref and binding_id:
                        # Admission already happened. Do not re-fetch a replied
                        # message that may have since been deleted: its absence
                        # cannot erase an admitted work handle.
                        topic = raw.get("selected_topic")
                        eligible = True
                    else:
                        eligible, topic = await self._eligible(raw)
                        if not eligible:
                            path.unlink()
                            self.store.settle_raw(str(self.config.account_id), filename)
                            count += 1
                            continue
                        binding_id = self._binding_for(raw["peer_id"], topic)
                    if binding_id is None:
                        # No owner-created exact binding: not an owner message,
                        # and never deliver this event to another room later.
                        os.replace(path, path.with_suffix(".held"))
                        self.store.settle_raw(str(self.config.account_id), filename, held=True)
                        continue
                    if binding_id != self._binding_for(raw["peer_id"], topic):
                        self.store.settle_raw(str(self.config.account_id), filename, held=True)
                        continue
                    if raw.get("selected_binding") not in ("", binding_id):
                        raise ValueError("event selected another binding")
                    if raw.get("selected_binding") != binding_id:
                        raw["selected_binding"] = binding_id
                        raw["selected_topic"] = topic
                        _atomic_json(path, raw)
                    event = build_event(self.config.account_id, raw["peer_id"],
                                        raw["message_id"], raw["actor_id"], raw["text"],
                                        topic_id=topic, event_kind=raw["kind"],
                                        revision=raw["revision"], message={
                                            "mentioned": raw["mentioned"],
                                            "reply_to_message_id": raw["reply_to_msg_id"],
                                        })
                    event_id = event["source_event_id"]
                    self.store.reserve_inbox(str(self.config.account_id), event_id, event)
                    pending_interim = raw.get("pending_interim")
                    if pending_interim:
                        # Crash after the work_ref write but before enqueue must
                        # replay the interim, not skip straight to the child.
                        self._queue_reply(raw, topic, binding_id,
                                          pending_interim["text"], pending_interim["version"],
                                          event_id, pending_interim["turn_ref"], phase="interim")
                        raw.pop("pending_interim")
                        _atomic_json(path, raw)
                    presence = self._presence_for(binding_id)
                    response = await asyncio.to_thread(presence.work, work_ref) if work_ref else (
                        await asyncio.to_thread(presence.turn, event,
                                                delivery_reporting_version=self.delivery_reporting_version))
                    if response.kind == "completed" and response.outcome == "deferred" and response.text:
                        if not response.text.strip() or len(response.text.encode("utf-8")) > 4096:
                            raw["failure_code"] = "REPLY_TOO_LONG"
                            _atomic_json(path, raw)
                            os.replace(path, path.with_suffix(".held"))
                            self.store.settle_raw(str(self.config.account_id), filename, held=True)
                            continue
                        raw["pending_interim"] = {
                            "text": response.text,
                            "version": response.delivery_reporting_version,
                            "turn_ref": response.turn_ref or work_ref,
                        }
                    if (response.work_ref and response.work_ref != work_ref) or raw.get("pending_interim"):
                        raw["work_ref"] = response.work_ref
                        _atomic_json(path, raw)
                    if response.kind == "pending":
                        blocked_peers.add(peer)
                        continue
                    if response.kind != "completed":
                        self._ingress_health(peer, "HOST_UNAVAILABLE" if response.kind == "retry"
                                             else "EVENT_UNAVAILABLE")
                        if response.code in {"presence_work_failed", "presence_work_cancelled"}:
                            self.store.settle_raw(str(self.config.account_id), filename, held=True)
                            os.replace(path, path.with_suffix(".held"))
                        elif response.work_ref or work_ref:
                            blocked_peers.add(peer)
                        elif response.kind in {"blocked", "rejected"}:
                            # Do not pay for the same permanent refusal every
                            # 3 seconds. Retain exact private bytes as a held
                            # event until a deliberate owner repair/replay.
                            held = path.with_suffix(".held")
                            if not held.exists():
                                os.replace(path, held)
                            self.store.settle_raw(str(self.config.account_id), filename, held=True)
                        else:
                            blocked_peers.add(peer)
                        continue
                    if response.outcome == "deferred":
                        if raw.get("pending_interim"):
                            interim = raw["pending_interim"]
                            self._queue_reply(raw, topic, binding_id, interim["text"],
                                              interim["version"], event_id, interim["turn_ref"],
                                              phase="interim")
                            raw.pop("pending_interim")
                            _atomic_json(path, raw)
                        if not (raw.get("work_ref") or work_ref):
                            raw["failure_code"] = "DEFERRED_WITHOUT_WORK_REF"
                            _atomic_json(path, raw)
                            os.replace(path, path.with_suffix(".held"))
                            self.store.settle_raw(str(self.config.account_id), filename, held=True)
                            continue
                        blocked_peers.add(peer)
                        continue
                    if response.outcome == "message" and response.text:
                        try:
                            self._queue_reply(raw, topic, binding_id, response.text,
                                              response.delivery_reporting_version, event_id,
                                              response.turn_ref or response.work_ref or work_ref)
                        except OversizedReply:
                            # Do not ACK an answer we cannot deliver. Hold the
                            # exact source for owner correction instead of
                            # repeatedly posting an unsendable job.
                            raw["failure_code"] = "REPLY_TOO_LONG"
                            _atomic_json(path, raw)
                            os.replace(path, path.with_suffix(".held"))
                            self.store.settle_raw(str(self.config.account_id), filename, held=True)
                            continue
                    self.store.ack_inbox(str(self.config.account_id), event_id)
                    path.unlink()
                    self.store.settle_raw(str(self.config.account_id), filename)
                    self._ingress_health(peer, "none")
                    count += 1
                except (ValueError, OSError, RuntimeError, ConnectionError,
                        TimeoutError, telegram_errors.RPCError) as exc:
                    # Failed catalog, provider fetch or disk write stays raw/pending;
                    # retry after bounded pacing in the companion loop. Do not
                    # catch cancellation, and never include private text in logs.
                    code = ("TELEGRAM_READ" if isinstance(exc, telegram_errors.RPCError)
                            else "PRIVATE_STATE_UNAVAILABLE" if isinstance(exc, OSError)
                            else "EVENT_UNAVAILABLE")
                    self._ingress_health(peer, code)
                    blocked_peers.add(peer)
                    continue
        return count

    async def _authorize_write(self, peer, topic, binding):
        if self.authorization is None:
            raise ValueError('current dispatch authorization is required')
        self.authorization.current(peer, True, topic=topic, expected_binding=binding)
        if await self.adapter.get_me() != int(self.authorization.account):
            raise ValueError('authenticated account changed')
        await self.authorization.authorize(peer, topic or '', True, expected_binding=binding)

    async def send_one(self) -> bool:
        """At most ONE unattempted outbox job; no retry of unknown/sent effects."""
        account = str(self.config.account_id)
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
            # Only an immutable automatic intent uses this naming contract.
            prefix = "auto_" + hashlib.sha256(source.encode("utf-8")).hexdigest()
            if job.intent_id not in {prefix, prefix + "_interim"} or job.remote_message_id is None:
                self.store.mark_report_invalid(account, job.intent_id)
                continue
            result = await asyncio.to_thread(
                self._presence_for(payload["binding_id"]).report_delivered,
                delivery_id=prefix, part_id=payload.get("part_id", "0"),
                provider_message_id=job.remote_message_id,
                account_id=self.config.account_id, peer_id=job.peer_id,
                topic_id=job.topic_id, text=payload["text"],
                source_event_id=source, task_id=payload.get("turn_ref") or "",
                delivery_reporting_version=1)
            if result.kind == "completed":
                _atomic_json(receipt, {"delivery_id": prefix, "message_id": job.remote_message_id})
                self.store.mark_reported(account, job.intent_id)
                count += 1
            else:
                self.store.defer_report(account, job.intent_id)
        return count
