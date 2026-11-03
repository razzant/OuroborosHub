"""PluginAPI 2.0 entrypoint for the separately configured Telegram user session.

No Telethon client runs in an extension tool or route. The companion is the only
network actor. Tool sends create durable intents; queued is NEVER proof of delivery.
Setup routes use the ordinary host extension gateway's network authentication.
That gate is a deployment boundary, not proof of a personal owner identity:
exposing an unauthenticated non-loopback Ouroboros port also exposes owner APIs.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from datetime import datetime, timezone
from typing import Any

from .userbot.config import (Binding, Config, ConfigError, TWO_FA_NAME, CODE_STATUS_NAME,
                             load_config, load_login_challenge, save_config,
                             stage_login_secret, update_config)
from .userbot.dossier import DossierStore
from .userbot.action_store import ActionStore
from .userbot.store import DurableStore


_INTENT = re.compile(r"[A-Za-z0-9._:-]{1,256}\Z")
_MAX_ID = (1 << 63) - 1
_MAX_TEXT_BYTES = 4096


class PluginInputError(ValueError):
    """Safe, non-secret validation message for an agent-callable send tool."""


def _account(config: Config | None) -> str:
    if config is None or config.account_id is None:
        raise PluginInputError("Telegram account is not configured and verified by the companion")
    return str(config.account_id)


def _intent_id(value: Any) -> str:
    if not isinstance(value, str) or not _INTENT.fullmatch(value):
        raise PluginInputError("intent_id must be a stable 1-256 character opaque identifier")
    return value


def _peer_id(value: Any, name: str = "peer_id") -> int:
    if type(value) is not int or value == 0 or not -_MAX_ID <= value <= _MAX_ID:
        raise PluginInputError(f"{name} must be a nonzero signed integer ID")
    return value


def _optional_id(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 < value <= _MAX_ID:
        raise PluginInputError(f"{name} must be a positive integer ID")
    return value


def _positive_id(value: Any, name: str) -> int:
    result = _optional_id(value, name)
    if result is None:
        raise PluginInputError(f"{name} must be a positive integer ID")
    return result


def _dossier_scope(api: Any, peer_id: int) -> tuple[Path, int, int]:
    peer = _peer_id(peer_id)
    state = _state_dir(api)
    config = load_config(state)
    account = int(_account(config))
    assert config is not None
    if peer not in config.allowed_peer_ids:
        raise PluginInputError("peer_id is not in the owner's explicit allowlist")
    path = state / "dossier.sqlite3"
    if path.is_symlink():
        raise PluginInputError("private dossier path is unsafe")
    return path, account, peer


def _source_time(value: Any) -> datetime:
    if type(value) is not str or len(value) > 40:
        raise PluginInputError("source_at must be an ISO 8601 timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PluginInputError("source_at must be an ISO 8601 timezone-aware timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PluginInputError("source_at must be an ISO 8601 timezone-aware timestamp")
    return parsed.astimezone(timezone.utc)


def _fact_result(item: Any) -> dict[str, Any]:
    return {"id": item.id, "user_id": item.user_id,
            "source_peer_id": item.source_peer_id,
            "source_message_id": item.source_message_id,
            "source_at": item.source_at.isoformat(), "fact": item.fact,
            "expires_at": item.expires_at.isoformat()}


def _dossier_add(api: Any, peer_id: int, user_id: int, source_message_id: int,
                 source_at: str, fact: str, ttl_days: int = 90) -> str:
    path, account, peer = _dossier_scope(api, peer_id)
    user = _positive_id(user_id, "user_id")
    source_message = _positive_id(source_message_id, "source_message_id")
    timestamp = _source_time(source_at)
    if type(ttl_days) is not int or not 1 <= ttl_days <= 365:
        raise PluginInputError("ttl_days must be an integer from 1 to 365")
    if type(fact) is not str or not 1 <= len(fact.strip()) <= 500:
        raise PluginInputError("fact must contain 1..500 characters")
    with DossierStore(path) as store:
        try:
            item = store.add_fact(account, user, source_peer_id=peer,
                                  source_message_id=source_message,
                                  source_at=timestamp, fact=fact, ttl_days=ttl_days)
        except ValueError as error:
            raise PluginInputError(str(error)) from None
    return json.dumps(_fact_result(item), ensure_ascii=False)


def _dossier_list(api: Any, peer_id: int, user_id: int) -> str:
    path, account, peer = _dossier_scope(api, peer_id)
    user = _positive_id(user_id, "user_id")
    with DossierStore(path) as store:
        items = store.list_facts(account, user, peer_id=peer)
    return json.dumps({"peer_id": peer, "user_id": user,
                       "facts": [_fact_result(item) for item in items]}, ensure_ascii=False)


def _dossier_delete_peer(api: Any, peer_id: int, user_id: int, confirm: bool) -> str:
    path, account, peer = _dossier_scope(api, peer_id)
    user = _positive_id(user_id, "user_id")
    if confirm is not True:
        raise PluginInputError("explicit confirm=true required for dossier deletion")
    with DossierStore(path) as store:
        count = store.delete_peer(account, user, peer)
    return json.dumps({"deleted": count, "peer_id": peer, "user_id": user})


def _state_dir(api: Any) -> Path:
    """PluginAPI owns this private path, never the reviewed source tree."""
    return Path(api.get_state_dir())


def _payload_file(root: Path, ref: str, data: bytes, *, subdir: str = "outgoing") -> None:
    """Publish immutable bytes without exposing a partially written final file.

    Same account + intent deterministically yields the same ref. On a replay,
    comparison happens before enqueue, so changed text cannot silently inherit
    an existing Telegram random_id. The temporary and final file share a volume;
    hard-link publication is atomic and does not overwrite an existing intent.
    """
    directory = root / subdir
    if directory.is_symlink():
        raise PluginInputError("private outgoing directory is unsafe")
    directory.mkdir(mode=0o700, exist_ok=True)
    if not directory.is_dir() or directory.is_symlink():
        raise PluginInputError("private outgoing directory is unavailable")
    if os.name == "posix":
        os.chmod(directory, 0o700)
    target = directory / (ref + ".json")
    if target.is_symlink():
        raise PluginInputError("private outgoing payload is unsafe")
    temporary = directory / ("." + ref + "." + secrets.token_hex(16) + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
        if target.is_symlink():
            raise PluginInputError("private outgoing payload is unsafe")
        opened = os.open(target, os.O_RDONLY | getattr(os, "O_BINARY", 0) |
                         getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(opened, "rb") as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or (os.name == "posix" and meta.st_mode & 0o077):
                raise PluginInputError("private outgoing payload is unsafe")
            if meta.st_size != len(data) or stream.read(len(data) + 1) != data:
                raise PluginInputError("intent_id already used for a different payload")
        if os.name == "posix":
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _send_text(api: Any, intent_id: str, peer_id: int, text: str,
               topic_id: int | None = None, reply_id: int | None = None) -> str:
    intent = _intent_id(intent_id)
    peer = _peer_id(peer_id)
    topic = _optional_id(topic_id, "topic_id")
    if topic is not None and not 1 < topic <= (1 << 31) - 1:
        raise PluginInputError("topic_id must be null for General or a known non-General topic ID")
    reply = _optional_id(reply_id, "reply_id")
    if reply is not None and reply >= 1 << 31:
        raise PluginInputError("reply_id must be a positive Telegram int32 ID")
    if not isinstance(text, str) or not text.strip():
        raise PluginInputError("text must be nonempty and at most 4096 UTF-8 bytes")
    try:
        text_bytes = text.encode("utf-8")
    except UnicodeError:
        raise PluginInputError("text must be valid UTF-8") from None
    if len(text_bytes) > _MAX_TEXT_BYTES:
        raise PluginInputError("text must be nonempty and at most 4096 UTF-8 bytes")
    config = load_config(_state_dir(api))
    account = _account(config)
    assert config is not None
    if peer not in config.allowed_peer_ids:
        raise PluginInputError("peer_id is not in the owner's explicit allowlist")
    binding_id = config.binding_for(peer, topic)
    if not binding_id:
        raise PluginInputError("owner must configure an exact Presence binding for this peer and topic")
    # The companion must independently verify topic catalog membership and
    # account/peer authorization at dispatch time, after configuration changes.
    ref = hashlib.sha256((account + "\0" + intent).encode("utf-8")).hexdigest()
    payload = {"text": text, "peer_id": peer, "topic_id": topic, "reply_id": reply,
               "binding_id": binding_id}
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    _payload_file(_state_dir(api), ref, encoded)
    with DurableStore(_state_dir(api) / "transport.sqlite3") as store:
        job = store.enqueue(account, intent, peer_id=peer, payload_ref=ref,
                            topic_id=topic, reply_id=reply)
    return json.dumps({"intent_id": intent, "status": job.status,
                       "note": "A stored send intent, not Telegram delivery. UNKNOWN requires manual reconciliation."})


def _send_status(api: Any, intent_id: str) -> str:
    intent = _intent_id(intent_id)
    account = _account(load_config(_state_dir(api)))
    with DurableStore(_state_dir(api) / "transport.sqlite3") as store:
        job = store.get(account, intent)
    if job is None:
        return json.dumps({"intent_id": intent, "status": "not_found"})
    result: dict[str, Any] = {"intent_id": intent, "status": job.status}
    if job.status == "sent":
        result["remote_message_id"] = job.remote_message_id
    if job.status == "failed":
        result["error_code"] = job.error_code
    if job.status in ("attempted", "unknown"):
        result["note"] = "Telegram acceptance uncertain; inspect and reconcile manually before any new send."
    return json.dumps(result)


def _action_scope(api: Any, peer_id: int, topic_id: int | None) -> tuple[Path, str, int, int | None, str]:
    state = _state_dir(api)
    config = load_config(state)
    account = _account(config)
    assert config is not None
    peer = _peer_id(peer_id)
    topic = _optional_id(topic_id, "topic_id")
    if topic is not None and not 1 < topic < (1 << 31):
        raise PluginInputError("topic_id must be null for General or a non-General int32 topic ID")
    if peer not in config.allowed_peer_ids:
        raise PluginInputError("peer_id is not in the owner's explicit allowlist")
    binding_id = config.binding_for(peer, topic)
    if not binding_id:
        raise PluginInputError("owner must configure an exact Presence binding for this peer and topic")
    # The companion repeats authorization using current config, authenticated
    # account, refreshed topic catalog and the fetched Telegram target.
    return state, account, peer, topic, binding_id


def _action_mutate(api: Any, operation: str, intent_id: str, peer_id: int,
                   message_id: int, topic_id: int | None, value: str | None = None) -> str:
    intent = _intent_id(intent_id)
    state, account, peer, topic, binding = _action_scope(api, peer_id, topic_id)
    message = _positive_id(message_id, "message_id")
    if message >= 1 << 31:
        raise PluginInputError("message_id must be a positive Telegram int32 ID")
    payload_ref = payload_sha256 = None
    if operation in ("edit", "react"):
        name = "text" if operation == "edit" else "emoji"
        if type(value) is not str or not value.strip():
            raise PluginInputError(f"{name} must be nonempty UTF-8")
        try:
            encoded_value = value.encode("utf-8")
        except UnicodeError:
            raise PluginInputError(f"{name} must be valid UTF-8") from None
        if len(encoded_value) > (4096 if operation == "edit" else 64):
            raise PluginInputError(f"{name} exceeds its UTF-8 byte limit")
        if operation == "react" and (value != value.strip() or any(ch.isspace() for ch in value)):
            raise PluginInputError("emoji must not contain whitespace")
        payload_ref = hashlib.sha256((account + "\0" + intent).encode("utf-8")).hexdigest()
        payload = {"operation": operation, "peer_id": peer, "topic_id": topic,
                   "message_id": message, "binding_id": binding, name: value}
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")) + "\n").encode("utf-8")
        payload_sha256 = hashlib.sha256(encoded).hexdigest()
        _payload_file(state, payload_ref, encoded, subdir="actions")
    with ActionStore(state / "actions.sqlite3") as store:
        try:
            job = store.enqueue(account, intent, operation=operation, peer_id=peer,
                                topic_id=topic, message_id=message, binding_id=binding,
                                payload_ref=payload_ref, payload_sha256=payload_sha256)
        except ValueError:
            raise PluginInputError("intent_id already bound to different action bytes or scope") from None
    return json.dumps({"intent_id": intent, "status": job.status,
                       "note": "Stored action intent, not Telegram execution or delivery. "
                               "UNKNOWN requires manual reconciliation; never auto-resend."})


def _action_status(api: Any, intent_id: str) -> str:
    intent = _intent_id(intent_id)
    state = _state_dir(api)
    config = load_config(state)
    account = _account(config)
    assert config is not None
    with ActionStore(state / "actions.sqlite3") as store:
        job = store.get(account, intent)
    # Revoking a peer or its exact binding also revokes model visibility of
    # prior actions. No peer, message text or binding is exposed by this tool.
    if job is None or job.peer_id not in config.allowed_peer_ids or \
            config.binding_for(job.peer_id, job.topic_id) != job.binding_id:
        return json.dumps({"intent_id": intent, "status": "not_found"})
    result: dict[str, Any] = {"intent_id": intent, "status": job.status}
    if job.status == "failed":
        result["error_code"] = job.error_code
    if job.status in ("attempted", "unknown"):
        result["note"] = "Telegram mutation may have happened; inspect and reconcile manually. Never auto-resend."
    return json.dumps(result)


def _response(payload: dict[str, Any], status: int = 200):
    # Starlette is host-provided; returns are serialized in and out of the
    # isolated extension child using the same plugin route response contract.
    from starlette.responses import JSONResponse
    return JSONResponse(payload, status_code=status)


async def _request_body(request: Any, allowed: set[str]) -> dict[str, Any]:
    try:
        body = await request.json()
    except (TypeError, ValueError):
        raise PluginInputError("invalid JSON object") from None
    if type(body) is not dict or not set(body).issubset(allowed):
        raise PluginInputError("invalid setup fields")
    return body


def _config_update(state_dir: Path, data: dict[str, Any]) -> None:
    def merge(old: Config | None) -> Config:
        api_id_raw = data.get("api_id", str(old.api_id) if old else None)
        if type(api_id_raw) is not str or not api_id_raw.isascii() or not api_id_raw.isdecimal():
            raise ConfigError("api_id must be a decimal integer")
        api_id = int(api_id_raw)
        api_hash = data.get("api_hash") or (old.api_hash if old else None)
        phone = data.get("phone") or (old.phone if old else None)
        raw_peers = data.get("allowed_peer_ids", ",".join(map(str, old.allowed_peer_ids)) if old else "")
        if type(raw_peers) is not str or len(raw_peers) > 20_000:
            raise ConfigError("allowed_peer_ids must be comma-separated numeric IDs")
        try:
            peers = tuple(int(piece.strip()) for piece in raw_peers.split(",") if piece.strip())
        except ValueError:
            raise ConfigError("allowed_peer_ids must be comma-separated numeric IDs") from None
        binding = data.get("binding_id", old.binding_id if old else None) or None
        if "bindings" in data:
            raw_bindings = data["bindings"]
            if type(raw_bindings) is not str or not 1 <= len(raw_bindings) <= 100_000:
                raise ConfigError("bindings must be a JSON list of exact peer/topic mappings")
            def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ConfigError("duplicate binding field")
                    result[key] = value
                return result
            try:
                parsed_bindings = json.loads(raw_bindings, object_pairs_hook=unique_pairs)
            except (ValueError, TypeError):
                raise ConfigError("bindings must be valid JSON") from None
            if type(parsed_bindings) is not list or len(parsed_bindings) > 1_000:
                raise ConfigError("bindings must be a bounded JSON list")
            if any(type(entry) is not dict or set(entry) != {"peer_id", "topic_id", "binding_id"}
                   for entry in parsed_bindings):
                raise ConfigError("each binding must contain peer_id, topic_id and binding_id")
            bindings = tuple(Binding(**entry) for entry in parsed_bindings)
        else:
            bindings = old.bindings if old else ()
        return Config(api_id=api_id, api_hash=api_hash, phone=phone,
                      binding_id=binding, allowed_peer_ids=peers, bindings=bindings,
                      account_id=(old.account_id if old and
                                  (old.api_id, old.api_hash, old.phone) == (api_id, api_hash, phone)
                                  else None))
    update_config(state_dir, merge)


def _request_login_code(state_dir: Path, *, resend: bool = False) -> None:
    """Stage one explicit owner request, never initiate a Telegram call here.

    The companion claims the marker into .inflight and retains it across the
    physical request until the challenge is saved or a definite failure. A
    disappearing marker is not proof that a code was delivered.
    """
    if load_config(state_dir) is None:
        raise PluginInputError("configuration missing")
    marker = state_dir / "login-request.json"
    inflight = state_dir / "login-request.inflight"
    challenge = state_dir / "login-challenge.json"
    password_required = state_dir / TWO_FA_NAME
    if any(path.exists() or path.is_symlink() for path in (marker, inflight, password_required)) or (not resend and challenge.exists()) or (resend and not load_login_challenge(state_dir)):
        raise PluginInputError("login code request or challenge already pending")
    temporary = state_dir / (".login-request." + secrets.token_hex(16) + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps({'request_code':True, **({'resend':True} if resend else {})}).encode())
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, marker)
        except FileExistsError:
            raise PluginInputError("login code request already pending") from None
        if inflight.exists() or (not resend and challenge.exists()) or password_required.exists():
            # The companion may have claimed a prior request between checks;
            # never leave a second request for its next polling cycle.
            marker.unlink(missing_ok=True)
            raise PluginInputError("login code request or challenge already pending")
        (state_dir / CODE_STATUS_NAME).unlink(missing_ok=True)
        if os.name == "posix":
            directory_fd = os.open(state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _make_config_route(api: Any):
    async def route(request: Any):
        state_dir = _state_dir(api)
        if request.method == "GET":
            try:
                config = load_config(state_dir)
            except ConfigError:
                return _response({"ok": False, "message": "Private configuration unavailable."}, 409)
            if config is None:
                return _response({})
            # Form hydration never includes api_hash, phone, auth code or session.
            return _response({"api_id": str(config.api_id),
                              "binding_id": config.binding_id or "",
                              "bindings": json.dumps([{"peer_id": item.peer_id,
                                                       "topic_id": item.topic_id,
                                                       "binding_id": item.binding_id}
                                                      for item in config.bindings], separators=(",", ":")),
                              "allowed_peer_ids": ",".join(map(str, config.allowed_peer_ids))})
        try:
            body = await _request_body(request, {"api_id", "api_hash", "phone", "binding_id", "bindings", "allowed_peer_ids"})
            _config_update(state_dir, body)
        except TimeoutError:
            return _response({"ok": False, "code": "state_lock_unavailable",
                              "message": "Configuration lock busy; retry without changing input."}, 503)
        except (PluginInputError, ConfigError) as exc:
            code = "state_lock_unavailable" if str(exc) == "config lock unavailable" else "configuration_invalid"
            return _response({"ok": False, "code": code, "message": str(exc)},
                             503 if code == "state_lock_unavailable" else 400)
        except (OSError, ValueError):
            return _response({"ok": False, "message": "Configuration rejected; verify fields and offline session rotation."}, 400)
        return _response({"ok": True, "message": "Configuration saved. No Telegram login was performed."})
    return route


def _make_login_route(api: Any):
    async def route(request: Any):
        if request.method == "GET":
            # Hydration probe: no saved values to prefill (one-shot secrets);
            # an empty object keeps the section enabled instead of degraded.
            return _response({})
        try:
            body = await _request_body(request, {"code", "password"})
            if load_config(_state_dir(api)) is None:
                raise PluginInputError("configuration missing")
            if body.get("code") and not load_login_challenge(_state_dir(api)):
                return _response({"ok": False, "code": "login_not_ready",
                                  "message": "Request a login code and wait for its challenge first."}, 409)
            if body.get("password") and not body.get("code") and not (
                    _state_dir(api) / TWO_FA_NAME).exists():
                return _response({"ok": False, "code": "login_not_ready",
                                  "message": "A Telegram 2FA challenge is not pending."}, 409)
            stage_login_secret(_state_dir(api), code=body.get("code") or None,
                               password=body.get("password") or None)
        except TimeoutError:
            return _response({"ok": False, "code": "state_lock_unavailable",
                              "message": "Login handoff lock busy; retry the same input."}, 503)
        except (PluginInputError, ConfigError) as exc:
            code = "state_lock_unavailable" if str(exc) == "config lock unavailable" else "login_input_invalid"
            return _response({"ok": False, "code": code, "message": str(exc)},
                             503 if code == "state_lock_unavailable" else 400)
        except (OSError, ValueError):
            return _response({"ok": False, "message": "Login handoff rejected."}, 400)
        return _response({"ok": True, "message": "Login input staged privately for the companion."})
    return route


def _make_request_code_route(api: Any, *, resend: bool = False):
    async def route(request: Any):
        if request.method == "GET":
            # The host's settings hydration probe reads current values with a
            # bodyless GET; an empty object means "defaults", keeping Save on.
            return _response({})
        try:
            await _request_body(request, set())
        except PluginInputError:
            return _response({"ok": False, "code": "request_invalid",
                              "message": "Expected an empty JSON object for a login-code request."}, 400)
        try:
            _request_login_code(_state_dir(api),resend=resend)
        except PluginInputError as exc:
            missing = str(exc) == "configuration missing"
            return _response({"ok": False,
                              "code": "configuration_missing" if missing else "login_code_pending",
                              "message": ("Configure the account before requesting a code." if missing
                                          else "A login code request or challenge is already pending.")}, 409)
        except (ConfigError, OSError, ValueError):
            return _response({"ok": False, "message": "Could not stage a login code request."}, 400)
        return _response({"ok": True, "message": "A login code request was staged; delivery is not confirmed."})
    return route


def _make_status_route(api: Any):
    async def route(_request: Any):
        state = _state_dir(api)
        try:
            config = load_config(state)
        except ConfigError:
            return _response({"ok": False, "status": "configuration_unavailable"}, 409)
        # Never inspect or render session contents, auth handoff, phone/hash.
        if (state / "login-request.inflight").exists():
            code_request_status = "unknown"
        elif (state / TWO_FA_NAME).exists():
            code_request_status = "password_required"
        elif (state / "login-challenge.json").exists():
            code_request_status = "challenge_pending"
        elif (state / "login-request.json").exists():
            code_request_status = "queued"
        else:
            code_request_status = "none"
        capability = state / "presence-capability.json"
        def status_record(path: Path) -> dict[str, Any]:
            if path.is_symlink() or not path.is_file():
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if type(value) is dict else {}
        if code_request_status == "none":
            try:
                prior_code = status_record(state / CODE_STATUS_NAME).get("status")
                if prior_code in {"already_authorized", "definitely_refused"}:
                    code_request_status = prior_code
            except (OSError, ValueError, TypeError):
                pass
        try:
            delivery = status_record(capability)
            delivery_status = delivery.get("status", "not_checked")
            if delivery_status not in {"available", "identity_unavailable",
                                       "host_does_not_advertise_delivery", "not_checked"}:
                delivery_status = "not_checked"
        except (OSError, ValueError, TypeError):
            delivery_status = "not_checked"
        try:
            health = status_record(state / "companion-health.json")
            category = health.get("category", "unknown")
            if category not in {"running", "dependency_missing", "config_invalid",
                                "login_required", "provider_retry", "ingress_failed",
                                "login_failed", "transport_error", "companion_error", "session_mismatch"}:
                category = "unknown"
            if category in {"running", "login_required", "provider_retry"}:
                # A historical running row is not proof that its writer is
                # alive. The companion refreshes it once per loop; the status
                # route never upgrades a stale heartbeat into live health.
                stamp = health.get("at")
                try:
                    measured = datetime.fromisoformat(stamp)
                    age = (datetime.now(timezone.utc) - measured).total_seconds()
                    if measured.utcoffset() is None or not -5 <= age <= 30:
                        category = "unknown"
                except (TypeError, ValueError, OverflowError):
                    category = "unknown"
        except (OSError, ValueError, TypeError):
            category = "unknown"
        inbound = {"pending": 0}
        reports = {"pending": 0, "retrying": 0, "invalid": 0, "unsupported": 0}
        queue_status = "not_initialized"
        if config is not None and config.account_id is not None and (state / "transport.sqlite3").exists():
            try:
                with DurableStore(state / "transport.sqlite3") as store:
                    inbound["pending"] = store.pending_raw_count(str(config.account_id))
                    reports = store.report_counts(str(config.account_id))
                queue_status = "available"
            except (OSError, ValueError, sqlite3.Error):
                inbound = {"pending": None}
                reports = {"pending": None, "retrying": None, "invalid": None,
                           "unsupported": None}
                queue_status = "storage_unavailable"
        ingress_code = "none"
        repair_needed: bool | None = False
        ingress_peer_count = 0
        health_path = state / "inbound-health.json"
        if health_path.exists() or health_path.is_symlink():
            try:
                ingress = status_record(health_path)
                peers = ingress.get("peers")
                if type(peers) is not dict or len(peers) > 1000:
                    raise ValueError("inbound health unavailable")
                visible = []
                for key, row in peers.items():
                    if (type(key) is not str or not key.lstrip("-").isdigit()
                            or type(row) is not dict or type(row.get("code")) is not str
                            or type(row.get("attempts")) is not int
                            or not 1 <= row["attempts"] <= 100):
                        raise ValueError("inbound health unavailable")
                    if config and int(key) in config.allowed_peer_ids:
                        visible.append(row)
                ingress_peer_count = len(visible)
                if visible:
                    # Repair-needed failures outrank later transient failures;
                    # success in another peer cannot erase a blocked peer.
                    chosen = next((row for row in visible if row["attempts"] >= 5), visible[0])
                    ingress_code = chosen["code"]
                    repair_needed = any(row["attempts"] >= 5 for row in visible)
            except (OSError, ValueError, TypeError):
                ingress_code = "unavailable"
                repair_needed = None
                ingress_peer_count = None
        return _response({"ok": queue_status != "storage_unavailable", "message": (
                              "User session status: configuration " + ("present" if config else "missing") +
                              "; account ID " + ("recorded (live authorization unverified)" if config and config.account_id else "not recorded") +
                              "; code request " + code_request_status + "." +
                              (" Enter the Telegram 2FA password in Stage login input."
                               if code_request_status == "password_required" else "") +
                              (" Queue unavailable: inspect and back up private skill state before repair."
                               if queue_status == "storage_unavailable" else "") +
                              (" Authorization required: use Login actions to request a code or submit the pending login input."
                               if category == "login_required" else "") +
                              (" Session/config mismatch: stop the companion, inspect the private session, and reconcile credentials offline."
                               if category == "session_mismatch" else "")),
                          "configured": config is not None,
                          "account_id_recorded": bool(config and config.account_id),
                          "peer_count": len(config.allowed_peer_ids) if config else 0,
                          "session_present": (state / "mtproto.session").exists(),
                          "delivery_reporting": delivery_status,
                          "companion_category": category,
                          "inbound_pending": inbound["pending"],
                          "inbound_last_error": ingress_code,
                          "inbound_repair_needed": repair_needed,
                          "inbound_affected_peers": ingress_peer_count,
                          "reports": reports,
                          "queue_status": queue_status,
                          "code_request_status": code_request_status})
    return route


_SETTINGS_SCHEMA = {"components": [
    {"type": "markdown", "text": "Telegram user-account transport is separate from the owner-only Bot API bridge. "
     "Setup uses the host gateway's network authentication, not a Telegram correspondent's identity. "
     "Never expose Ouroboros on a non-loopback interface without a network password. "
     "Queueing text is not delivery; UNKNOWN requires inspecting Telegram before any new send."},
    {"type": "form", "route": "setup/config", "method": "POST", "fields": [
        {"name": "api_id", "label": "Telegram API ID", "type": "text"},
        {"name": "api_hash", "label": "Telegram API hash", "type": "password",
         "help": "Leave blank to keep existing; never displayed by GET."},
        {"name": "phone", "label": "Account phone (E.164)", "type": "password",
         "help": "Leave blank to keep existing; never displayed by GET."},
        {"name": "binding_id", "label": "Presence binding ID (created by owner)", "type": "text"},
        {"name": "bindings", "label": "Exact peer/topic Presence bindings (JSON list)", "type": "text",
         "help": "Each entry: {\"peer_id\":200,\"topic_id\":null,\"binding_id\":\"32 hex characters\"}. "
                 "Use topic_id null for General. This mapping is authoritative when nonempty."},
        {"name": "allowed_peer_ids", "label": "Permitted numeric peer IDs, comma-separated", "type": "text"},
    ], "submit_label": "Save user-session setup"},
    {"type": "action", "id": "request_code", "route": "setup/request_code", "method": "POST",
     "submit_label": "Request Telegram login code", "fields": []},
    {"type": "action", "id": "login_stage", "route": "setup/login", "method": "POST",
     "submit_label": "Stage login input", "fields": [
         {"name": "code", "label": "Telegram login code", "type": "password"},
         {"name": "password", "label": "Telegram 2FA password (if prompted)", "type": "password"},
     ]},
    {"type": "action", "id": "session_status", "route": "setup/status", "method": "POST",
     "submit_label": "Check user-session status", "fields": []},
]}


def register_legacy(api: Any) -> None:
    """Register narrow capabilities; the manifest must declare `mtproto`."""
    api.register_companion_process("mtproto")
    api.register_tool(
        "send_text", handler=lambda intent_id, peer_id, text, topic_id=None, reply_id=None:
            _send_text(api, intent_id, peer_id, text, topic_id, reply_id),
        description=("Queue one idempotent text send to a specifically allowlisted Telegram peer. "
                     "Requires stable intent_id; QUEUED means durable intent, NOT sent/delivered. "
                     "Before finishing, check send_status until sent/failed/unknown. "
                     "UNKNOWN means Telegram may have accepted it: inspect and manually reconcile, never auto-resend."),
        schema={"type": "object", "properties": {
            "intent_id": {"type": "string", "description": "Stable caller-minted ID; reuse for retries of exactly the same send."},
            "peer_id": {"type": "integer", "description": "Numeric owner-allowlisted Telegram peer."},
            "text": {"type": "string", "description": "Nonempty UTF-8 text (max 4096 bytes)."},
            "topic_id": {"type": ["integer", "null"], "description": "Known forum topic ID; companion validates catalog."},
            "reply_id": {"type": ["integer", "null"], "description": "Optional target message ID."},
        }, "required": ["intent_id", "peer_id", "text"], "additionalProperties": False},
    )
    api.register_tool(
        "send_status", handler=lambda intent_id: _send_status(api, intent_id),
        description=("Read durable state of one idempotent send intent. QUEUED/LEASED/ATTEMPTED are not delivery. "
                     "Poll after enqueue; no automatic completion notification is sent. "
                     "UNKNOWN is ambiguous and needs manual reconciliation before a new send; "
                     "SENT requires the companion's stored Telegram receipt."),
        schema={"type": "object", "properties": {"intent_id": {"type": "string"}},
                "required": ["intent_id"], "additionalProperties": False},
    )
    # Owner-selected tools only. The participant Presence profile grants none
    # of these capabilities; its automatic answer goes through the companion.
    action_properties = {
        "intent_id": {"type": "string", "description": "Stable opaque ID; reuse only for the identical action."},
        "peer_id": {"type": "integer", "description": "Owner-allowlisted numeric Telegram peer."},
        "topic_id": {"type": ["integer", "null"], "description": "Exact bound forum topic; null is General."},
        "message_id": {"type": "integer", "description": "One positive Telegram int32 target message ID."},
    }
    def edit(intent_id, peer_id, message_id, topic_id, text):
        return _action_mutate(api, "edit", intent_id, peer_id, message_id, topic_id, text)

    def delete(intent_id, peer_id, message_id, topic_id):
        return _action_mutate(api, "delete", intent_id, peer_id, message_id, topic_id)

    def react(intent_id, peer_id, message_id, topic_id, emoji):
        return _action_mutate(api, "react", intent_id, peer_id, message_id, topic_id, emoji)

    for operation, value_name, handler in (("edit", "text", edit),
                                           ("delete", None, delete),
                                           ("react", "emoji", react)):
        props = dict(action_properties)
        if value_name:
            props[value_name] = {"type": "string", "description": (
                "Replacement text (max 4096 UTF-8 bytes)." if value_name == "text"
                else "Single Telegram reaction emoji (max 64 UTF-8 bytes).")}
        required = ["intent_id", "peer_id", "message_id", "topic_id"]
        if value_name:
            required.append(value_name)
        api.register_tool(
            "action_" + operation, handler=handler,
            description=("Queue one owner-authorized Telegram message " + operation +
                         ". Exact Presence binding, allowlist, authenticated account and one message required. "
                         "QUEUED means durable intent, not mutation/delivery. UNKNOWN must be reconciled "
                         "manually; never auto-resend. No participant Presence grant."),
            schema={"type": "object", "properties": props, "required": required,
                    "additionalProperties": False},
        )
    api.register_tool(
        "action_status", handler=lambda intent_id: _action_status(api, intent_id),
        description=("Read one owner-authorized action's local custody state, not Telegram delivery. "
                     "UNKNOWN means mutation may have happened; manually reconcile before any new action. "
                     "Revoked peer/binding actions are not visible."),
        schema={"type": "object", "properties": {"intent_id": {"type": "string"}},
                "required": ["intent_id"], "additionalProperties": False},
    )
    # These are explicitly invoked, peer-scoped tools. Inbound messages do not
    # create facts, and the participant Presence profile grants none of them.
    api.register_tool(
        "dossier_add", handler=lambda peer_id, user_id, source_message_id, source_at, fact, ttl_days=90:
            _dossier_add(api, peer_id, user_id, source_message_id, source_at, fact, ttl_days),
        description=("Record one explicitly chosen fact about a numeric Telegram user in one owner-allowlisted peer. "
                     "Caller must supply the real source message ID and timestamp; this tool cannot independently "
                     "verify the content or its source. No ambient message ingestion. TTL defaults to 90 days."),
        schema={"type": "object", "properties": {
            "peer_id": {"type": "integer", "description": "Source peer; must be in the owner's current allowlist."},
            "user_id": {"type": "integer", "description": "Positive numeric Telegram user ID; never a username."},
            "source_message_id": {"type": "integer", "description": "Positive message ID of the stated source in this peer."},
            "source_at": {"type": "string", "description": "Source message's timezone-aware ISO 8601 timestamp."},
            "fact": {"type": "string", "description": "Explicitly approved fact; 1–500 characters."},
            "ttl_days": {"type": "integer", "description": "Retention from source time, 1–365 days; default 90."},
        }, "required": ["peer_id", "user_id", "source_message_id", "source_at", "fact"],
            "additionalProperties": False},
    )
    api.register_tool(
        "dossier_list", handler=lambda peer_id, user_id: _dossier_list(api, peer_id, user_id),
        description="List only unexpired facts for a numeric user sourced within this owner-allowlisted peer; never other peers.",
        schema={"type": "object", "properties": {
            "peer_id": {"type": "integer"}, "user_id": {"type": "integer"},
        }, "required": ["peer_id", "user_id"], "additionalProperties": False},
    )
    api.register_tool(
        "dossier_delete_peer", handler=lambda peer_id, user_id, confirm:
            _dossier_delete_peer(api, peer_id, user_id, confirm),
        description=("Explicitly erase the numeric user's dossier facts sourced in one owner-allowlisted peer only. "
                     "This is local logical deletion, not forensic secure erase; confirm=true required."),
        schema={"type": "object", "properties": {
            "peer_id": {"type": "integer"}, "user_id": {"type": "integer"},
            "confirm": {"type": "boolean"},
        }, "required": ["peer_id", "user_id", "confirm"], "additionalProperties": False},
    )
    api.register_route("setup/config", handler=_make_config_route(api), methods=("GET", "POST"))
    api.register_route("setup/request_code", handler=_make_request_code_route(api), methods=("GET", "POST"))
    api.register_route("setup/login", handler=_make_login_route(api), methods=("GET", "POST"))
    # No online reset: a companion may be physically requesting a login code
    # while the inflight marker exists. Manual recovery requires stopping the
    # companion, reconciling that request, THEN clearing markers offline.
    api.register_route("setup/status", handler=_make_status_route(api), methods=("GET", "POST"))
    api.register_settings_section("user_session", "Telegram user session", schema=_SETTINGS_SCHEMA)


def register(api: Any) -> None:
    """Current participant tools; legacy source/state kept for reversible migration."""
    import sys
    from .userbot.participant_plugin import register as register_participant
    register_participant(api, sys.modules[__name__])
