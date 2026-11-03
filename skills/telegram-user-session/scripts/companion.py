"""Host-supervised single-owner Telethon process for telegram-user-session.

The Host service token is inherited only in this process's environment. The reviewed
payload contains neither the token nor account credentials. A stopped/disabled skill
ends this process; a previous attempted send becomes UNKNOWN on its next exclusive
start and is not replayed blindly.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from urllib import parse

# The host launches this reviewed script by path; Python's sys.path[0] is scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from userbot.config import (  # noqa: E402
    clear_login_challenge, consume_login_secret, load_config,
    load_login_challenge, save_login_challenge, update_config,
    ConfigError, HANDOFF_NAME, TWO_FA_NAME, CODE_STATUS_NAME, _read_json, _write_json,
)
from userbot.presence import PresenceClient, PresenceToken  # noqa: E402
from userbot.store import DurableStore, IngressBackpressure  # noqa: E402
from userbot.action_store import ActionStore  # noqa: E402


class SessionMismatch(ValueError):
    """Recorded account or credentials diverge from the live session."""


def _failure_category(exc: Exception) -> str:
    """Small owner-readable taxonomy; never persist provider exception text."""
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return "dependency_missing"
    if isinstance(exc, SessionMismatch):
        return "session_mismatch"
    if isinstance(exc, ConfigError):
        return "config_invalid"
    if type(exc).__name__ in {
        "LoginRequired",
        "PhoneCodeInvalidError", "PhoneNumberInvalidError", "PasswordHashInvalidError",
    }:
        return "login_failed"
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return "transport_error"
    return "companion_error"


def _health(state_dir: Path, category: str) -> None:
    _write_json(state_dir, "companion-health.json", {
        "category": category, "at": datetime.now(timezone.utc).isoformat(),
    })


def TelegramAdapter(*args, **kwargs):
    """Import optional Telethon only after Host identity passed its gate."""
    from userbot.telegram import TelegramAdapter as Adapter
    return Adapter(*args, **kwargs)


def _host_identity() -> tuple[str, PresenceToken]:
    """Reject absent/non-loopback Host identity before any Telegram operation."""
    host = os.environ.get("HOST_SERVICE_URL", "")
    try:
        parsed = parse.urlsplit(host)
        valid_url = (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                     and parsed.port is not None and not parsed.username and not parsed.password
                     and not parsed.path and not parsed.query and not parsed.fragment)
    except ValueError:  # malformed port or IPv6 literal
        valid_url = False
    if not valid_url:
        raise RuntimeError("host service identity unavailable")
    try:
        return host, PresenceToken.from_env()
    except (ValueError, KeyError):
        raise RuntimeError("host service identity unavailable") from None


def _verified_config_update(state_dir: Path, original: object, account_id: int):
    """Write only the authenticated account onto *current* owner scope."""
    def merge(latest):
        if latest is None or (
            latest.api_id, latest.api_hash, latest.phone
        ) != (original.api_id, original.api_hash, original.phone):
            raise SessionMismatch("session credentials changed during login; refusing")
        if latest.account_id is not None and latest.account_id != account_id:
            raise SessionMismatch("session account differs from configured account; refusing")
        return replace(latest, account_id=account_id)

    return update_config(state_dir, merge)


def _retire_unused_code_request(state_dir: Path) -> None:
    """An already-authorized session has no reason to request another code."""
    queued = state_dir / "login-request.json"
    if queued.exists():
        _write_json(state_dir, CODE_STATUS_NAME, {"status": "already_authorized"})
        queued.unlink()


async def _register_and_catch_up(adapter: TelegramAdapter, bridge: UserSessionBridge,
                                 wake: asyncio.Event) -> list[bool]:
    """Do not catch up until BOTH handlers can durably spool missed updates."""
    from telethon import events
    from userbot.runtime import IngressTooLarge
    faults: list[bool] = []

    def capture(event, kind: str) -> None:
        try:
            recorded = bridge.spool_update(event, kind=kind)
        except Exception as exc:
            # Telethon otherwise swallows a callback exception into its own
            # logs while this companion continues, falsely implying intake.
            # No exception text/message body enters the diagnostic.
            try:
                code = ("INGRESS_BACKPRESSURE" if isinstance(exc, IngressBackpressure)
                        else "INGRESS_TOO_LARGE" if isinstance(exc, IngressTooLarge)
                        else "PRIVATE_STATE_UNAVAILABLE")
                bridge._ingress_health(getattr(event, "chat_id", 0), code)
            except Exception:
                pass  # The failure to persist diagnostics cannot resume intake.
            faults.append(True)
            wake.set()
            return
        if recorded is not None:
            wake.set()

    async def on_new(event):
        capture(event, "new")

    async def on_edit(event):
        capture(event, "edit")

    adapter.client.add_event_handler(on_new, events.NewMessage(incoming=True))
    adapter.client.add_event_handler(on_edit, events.MessageEdited(incoming=True))
    # Telethon defaults catch_up=False; explicit call also covers a restart.
    await adapter.client.catch_up()
    return faults


async def _login_if_requested(state_dir: Path, cfg: object, adapter: TelegramAdapter) -> bool:
    """Only explicit owner setup requests can start a code/2FA exchange."""
    if await adapter.authorization_status():
        # A code/password staged concurrently with successful login is no
        # longer useful. Retire it rather than keeping reusable secret bytes.
        consume_login_secret(state_dir)
        (state_dir / TWO_FA_NAME).unlink(missing_ok=True)
        _retire_unused_code_request(state_dir)
        return True
    request = state_dir / "login-request.json"
    if request.exists():
        # A request that reached Telegram before crash is ambiguous. Retire it
        # BEFORE the call into an explicit in-flight marker; never auto-request
        # another code on a restart or on a racing second owner POST.
        request_data = _read_json(request)
        if request_data not in ({'request_code':True},{'request_code':True,'resend':True}):
            raise ValueError('invalid login request')
        resend_hash = load_login_challenge(state_dir) if request_data.get('resend') else None
        if request_data.get('resend') and not resend_hash:
            raise ValueError('no challenge for explicit resend')
        inflight = state_dir / "login-request.inflight"
        if inflight.exists():
            raise ValueError("login code request is already in flight or unknown")
        os.replace(request, inflight)
        try:
            phone_code_hash = await adapter.request_login_code(cfg.phone,resend_hash=resend_hash) if resend_hash else await adapter.request_login_code(cfg.phone)
        except Exception as exc:
            from telethon import errors
            if isinstance(exc, (errors.PhoneNumberInvalidError, errors.PhoneNumberBannedError,
                                errors.ApiIdInvalidError)):
                _write_json(state_dir, CODE_STATUS_NAME,
                            {"status": "definitely_refused", "reason": type(exc).__name__})
                inflight.unlink(missing_ok=True)
            # Transport death/unknown or FloodWait remains in-flight for owner
            # reconciliation; no automatic second code request.
            raise
        save_login_challenge(state_dir, phone_code_hash)
        inflight.unlink()
        _write_json(state_dir,CODE_STATUS_NAME,{'status':'challenge_pending','delivery':getattr(adapter,'code_delivery',{})})
    # A one-shot code must not be consumed before its durable challenge exists.
    # The owner setup route also checks readiness, but the challenge may be
    # interrupted or removed before this companion observes the handoff.
    if (state_dir / HANDOFF_NAME).exists() and not (
            load_login_challenge(state_dir) or (state_dir / TWO_FA_NAME).exists()):
        return False
    secret = consume_login_secret(state_dir)
    if secret is None:
        return False
    if secret.code is not None:
        challenge = secret.phone_code_hash or load_login_challenge(state_dir)
        if not challenge:
            raise ValueError("no outstanding Telegram login challenge")
        try:
            logged = await adapter.complete_login(cfg.phone, secret.code, challenge,
                                                   password=secret.password)
        except Exception as exc:
            # Telethon SessionPasswordNeededError prompts a *new* explicit
            # password handoff; never replay the consumed one-time code.
            if type(exc).__name__ != "SessionPasswordNeededError":
                _write_json(state_dir, CODE_STATUS_NAME, {'status':'login_refused','reason':type(exc).__name__})
                raise
            _write_json(state_dir, TWO_FA_NAME, {"password_required": True})
            return False
    elif secret.password is not None:
        logged = await adapter.complete_password(secret.password)
    else:
        return False
    if logged:
        clear_login_challenge(state_dir)
        (state_dir / TWO_FA_NAME).unlink(missing_ok=True)
    return logged


async def _serve_legacy(state_dir: Path) -> None:
    host, token = _host_identity()
    # Keep optional Telethon import inside main's diagnostic boundary. A missing
    # dependency must still produce a readable, secret-free status category.
    from userbot.runtime import UserSessionBridge, IngressTooLarge
    cfg = load_config(state_dir)
    # Enabling before setup is common. Under on_failure, a clean exit would
    # strand the skill until another lifecycle action, so wait for the owner's
    # explicit config write without making any network request meanwhile.
    while cfg is None:
        await asyncio.sleep(2)
        cfg = load_config(state_dir)
    # An authenticated Host Service read must succeed before *any* Telegram
    # connection or login. Merely having a loopback URL/token-shaped string
    # is not evidence that the running Host accepts this skill revision.
    preflight = PresenceClient(host, token, cfg.binding_id or
                               (cfg.bindings[0].binding_id if cfg.bindings else "identity-probe"))
    _version, host_status = await asyncio.to_thread(preflight.delivery_capability)
    _write_json(state_dir, "presence-capability.json", {
        "delivery_reporting_version": _version, "status": host_status,
    })
    if host_status == "identity_unavailable":
        raise RuntimeError("authenticated Host Service unavailable")
    adapter = TelegramAdapter(state_dir / "mtproto.session", cfg.api_id, cfg.api_hash)
    connected = await adapter.connect()
    try:
        if not connected and not await _login_if_requested(state_dir, cfg, adapter):
            # Stay present for an owner action entered AFTER enablement. Do not
            # auto-request a login code or hallucinate a user id from config.
            while not await adapter.authorization_status():
                await asyncio.sleep(2)
                if await _login_if_requested(state_dir, cfg, adapter):
                    break
        account_id = await adapter.get_me()
        consume_login_secret(state_dir)  # Already-authorized startup branch.
        (state_dir / TWO_FA_NAME).unlink(missing_ok=True)
        _retire_unused_code_request(state_dir)
        cfg = _verified_config_update(state_dir, cfg, account_id)
        if not (cfg.binding_id or cfg.bindings) or not cfg.allowed_peer_ids:
            # Authentication is distinct from Presence enrollment/peer selection.
            while True:
                await asyncio.sleep(2)
                updated = load_config(state_dir)
                if updated is not None and (updated.binding_id or updated.bindings) and updated.allowed_peer_ids:
                    cfg = updated
                    break
        def for_binding(binding_id: str) -> PresenceClient:
            return PresenceClient(host, token, binding_id)

        presence = for_binding(cfg.binding_id or cfg.bindings[0].binding_id)
        delivery_version, delivery_status = await asyncio.to_thread(presence.delivery_capability)
        # Version 2 only when this Host lists it; otherwise each turn still waits for its terminal.
        continuation_version = await asyncio.to_thread(presence.continuation_capability)
        _write_json(state_dir, "presence-capability.json", {
            "delivery_reporting_version": delivery_version, "status": delivery_status,
            "continuation_version": continuation_version,
        })
        if delivery_status == "identity_unavailable":
            raise RuntimeError("authenticated Host Service unavailable")
        with (DurableStore(state_dir / "transport.sqlite3", recover_attempted=True) as store,
              ActionStore(state_dir / "actions.sqlite3", recover_attempted=True) as actions_store):
            from userbot.participant import CurrentAuthorization, Host
            authorization = CurrentAuthorization(state_dir, Host(host, token), account_id, 'legacy')
            bridge = UserSessionBridge(state_dir, cfg, adapter, store, presence,
                                       presence_factory=for_binding,
                                       action_store=actions_store,
                                       delivery_reporting_version=delivery_version,
                                       authorization=authorization,
                                       continuation_version=continuation_version)
            wake = asyncio.Event()
            ingress_faults = await _register_and_catch_up(adapter, bridge, wake)
            # Host intake beside outbound dispatch: each idle chat's head starts at once in its own
            # task (bridge.intake_pump), and events in Host custody are read in a pass of their own
            # at the three-second cadence, so a slow work read or one chat's held start never holds
            # another event, and replies, actions and receipts never wait for a Host answer. A
            # capture, a chat that moved on or an ended custody pass wakes the loop; a busy head
            # shortens the wait to the Host's hint. Unexpected failures surface here; a stop cancels
            # and joins every worker.
            observing: asyncio.Task | None = None
            observed_at = float("-inf")
            ended = asyncio.Event()
            clock = asyncio.get_running_loop().time
            try:
                while True:
                    # Freshness is read-side evidence, never a permanent startup
                    # claim. A killed process stops renewing the row.
                    _health(state_dir, "running")
                    if ingress_faults:
                        raise RuntimeError("durable ingress capture unavailable")
                    if (state_dir / HANDOFF_NAME).exists() and await adapter.authorization_status():
                        # Owner staged while this authorized companion was already
                        # running. Consume only with positive authorization proof.
                        consume_login_secret(state_dir)
                    queued_code = state_dir / "login-request.json"
                    if queued_code.exists() and await adapter.authorization_status():
                        # Do not silently leave a successful-session code request
                        # queued forever, and never contact Telegram for it.
                        _retire_unused_code_request(state_dir)
                    updated = load_config(state_dir)
                    if updated is None or updated.account_id != account_id or (
                        updated.api_id, updated.api_hash, updated.phone
                    ) != (cfg.api_id, cfg.api_hash, cfg.phone):
                        raise SessionMismatch("session configuration changed; refusing")
                    if (updated.binding_id, updated.bindings, updated.allowed_peer_ids) != (
                        cfg.binding_id, cfg.bindings, cfg.allowed_peer_ids
                    ):
                        cfg = updated
                        bridge.config = cfg
                        if cfg.binding_id or cfg.bindings:
                            bridge.presence = for_binding(cfg.binding_id or cfg.bindings[0].binding_id)
                        bridge._catalog.clear()
                    if observing is not None and observing.done():
                        finished, observing = observing, None
                        finished.result()  # An unexpected observation failure stops the companion visibly.
                    if (cfg.binding_id or cfg.bindings) and cfg.allowed_peer_ids:
                        bridge.intake_pump(on_progress=wake.set)  # also surfaces a finished chat's failure
                        if observing is None and clock() - observed_at >= 3:
                            observed_at = clock()
                            observing = asyncio.create_task(bridge.observe_custody(limit=100),
                                                            name="telegram-legacy-host-observe")
                            observing.add_done_callback(lambda _task: ended.set())
                        await bridge.send_one()
                        await bridge.process_action_once()
                        # Host Service uses a shared 60/min lane; one receipt per
                        # tick keeps this companion well below that capacity.
                        await bridge.report_sent(limit=1)
                    busy = bridge.busy_due_in()
                    waiters = [asyncio.create_task(wake.wait()), asyncio.create_task(ended.wait())]
                    try:
                        await asyncio.wait(waiters, timeout=3 if busy is None else min(3, max(0.05, busy)),
                                           return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for waiter in waiters:
                            waiter.cancel()
                    wake.clear()
                    ended.clear()
            finally:
                if observing is not None:
                    observing.cancel()
                    await asyncio.gather(observing, return_exceptions=True)
                await bridge.stop_intake()
    finally:
        await adapter.disconnect()


async def _serve(state_dir: Path) -> None:
    from userbot.participant_state import policy
    from userbot.participant import serve, session_lock
    if policy(state_dir)['mode'] == 'participant':
        host, token = _host_identity()
        await serve(state_dir, host, token, TelegramAdapter, _login_if_requested,
                    _verified_config_update, _health)
    else:
        with session_lock(state_dir):
            await _serve_legacy(state_dir)


def main() -> None:
    root = os.environ.get("OUROBOROS_SKILL_STATE_DIR")
    if not root or not Path(root).is_absolute():
        raise SystemExit("private skill state unavailable")
    # No untrusted exception value in stdout/stderr: provider exceptions may
    # contain account identifiers or message bytes. The host records exit facts.
    try:
        asyncio.run(_serve(Path(root)))
    except Exception as exc:
        try:
            _health(Path(root), _failure_category(exc))
            import traceback
            frames = [{'file':Path(frame.filename).name, 'function':frame.name, 'line':frame.lineno}
                      for frame in traceback.extract_tb(exc.__traceback__)]
            _write_json(Path(root), 'companion-last-error.json', {'category':_failure_category(exc), 'error_type':type(exc).__name__, 'at':datetime.now(timezone.utc).isoformat(), 'frames':frames})
        except (OSError, ConfigError):
            pass  # Keep the actual failure; the absence of a health row is visible.
        raise SystemExit("user-session companion paused; inspect private state and restart explicitly") from None


if __name__ == "__main__":
    main()
