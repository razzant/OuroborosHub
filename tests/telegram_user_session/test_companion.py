"""No Telegram login: prove missed-update delivery starts AFTER async handlers exist."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from scripts.companion import _host_identity, _login_if_requested, _register_and_catch_up, _serve, _verified_config_update
from userbot.config import Binding, Config, load_config, save_config, stage_login_secret
from userbot.presence import PresenceToken


API_HASH = "a" * 32
OLD_BINDING = "b" * 32
NEW_BINDING = "c" * 32


def _config(*, binding=OLD_BINDING, peers=(200, 300), account=None, api_id=12345):
    return Config(api_id=api_id, api_hash=API_HASH, phone="+15551234567",
                  binding_id=None, allowed_peer_ids=peers, account_id=account,
                  bindings=(() if not peers else (Binding(peers[0], None, binding),)))


def test_login_account_merge_preserves_latest_owner_revocation(tmp_path):
    state = tmp_path / "private"
    original = _config()
    save_config(state, original)
    # The owner removes peer 300 and rotates the only Presence binding while
    # Telegram's async get_me is pending. A stale dataclass replace would undo it.
    revoked = _config(binding=NEW_BINDING, peers=(200,))
    save_config(state, revoked)
    merged = _verified_config_update(state, original, 9001)
    assert merged == load_config(state)
    assert merged == _config(binding=NEW_BINDING, peers=(200,), account=9001)
    assert merged.binding_for(300, None) is None
    assert merged.binding_for(200, None) == NEW_BINDING


def test_login_account_merge_refuses_changed_credentials_or_account(tmp_path):
    state = tmp_path / "private"
    original = _config()
    save_config(state, _config(api_id=43210))
    with pytest.raises(ValueError, match="credentials changed"):
        _verified_config_update(state, original, 9001)
    assert load_config(state).account_id is None
    save_config(state, _config(account=1234))
    with pytest.raises(ValueError, match="session account differs"):
        _verified_config_update(state, original, 9001)
    assert load_config(state).account_id == 1234


def test_authorized_session_discards_staged_login_password_without_new_request(tmp_path):
    state = tmp_path / "private"
    cfg = _config(account=9001)
    save_config(state, cfg)
    stage_login_secret(state, password="sensitive 2fa handoff")
    class Authorized:
        async def authorization_status(self):
            return True
        async def request_login_code(self, *_args):
            pytest.fail("an authorized session must not request another code")
    assert asyncio.run(_login_if_requested(state, cfg, Authorized())) is True
    assert not (state / "auth-handoff.json").exists()


@pytest.mark.parametrize("host, token", [
    ("", "secret"), ("http://127.0.0.1:8765", ""),
    ("https://127.0.0.1:8765", "secret"),
    ("http://remote.example:8765", "secret"),
    ("http://[::1", "secret"),
])
def test_host_identity_rejected_before_telegram_connect(tmp_path, monkeypatch, host, token):
    from scripts import companion
    state = tmp_path / "private"
    save_config(state, _config())
    monkeypatch.setenv("HOST_SERVICE_URL", host)
    monkeypatch.setenv("HOST_SERVICE_TOKEN", token)
    monkeypatch.setattr(companion, "TelegramAdapter", lambda *args, **kwargs:
                        pytest.fail("Telegram constructor ran before Host identity validation"))
    with pytest.raises(RuntimeError, match="host service identity unavailable"):
        asyncio.run(_serve(state))


def test_valid_host_identity_hands_off_opaque_token(monkeypatch):
    monkeypatch.setenv("HOST_SERVICE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("HOST_SERVICE_TOKEN", "not-for-a-log")
    host, token = _host_identity()
    assert host == "http://127.0.0.1:8765"
    assert isinstance(token, PresenceToken)
    assert "not-for-a-log" not in repr(token)


def test_authenticated_host_read_precedes_telegram_connection(tmp_path, monkeypatch):
    from scripts import companion
    state = tmp_path / "private"
    save_config(state, _config())
    monkeypatch.setenv("HOST_SERVICE_URL", "http://127.0.0.1:8767")
    monkeypatch.setenv("HOST_SERVICE_TOKEN", "test-only-token")
    class HostUnavailable:
        def __init__(self, *_args):
            pass
        def delivery_capability(self):
            return 0, "identity_unavailable"
    monkeypatch.setattr(companion, "PresenceClient", HostUnavailable)
    monkeypatch.setattr(companion, "TelegramAdapter", lambda *_args, **_kwargs:
                        pytest.fail("Telegram connection attempted before authenticated Host read"))
    with pytest.raises(RuntimeError, match="authenticated Host Service unavailable"):
        asyncio.run(_serve(state))
    assert (state / "presence-capability.json").exists()


def test_callback_registration_precedes_catch_up(monkeypatch):
    calls = []
    event = SimpleNamespace(source="missed")

    class Client:
        def __init__(self):
            self.callbacks = []

        def add_event_handler(self, callback, event_filter):
            calls.append(("register", event_filter[0]))
            self.callbacks.append(callback)

        async def catch_up(self):
            calls.append(("catch_up", len(self.callbacks)))
            assert len(self.callbacks) == 2
            await self.callbacks[0](event)

    class Bridge:
        def spool_update(self, update, *, kind):
            assert update is event
            calls.append(("spool", kind))
            return "durable-event-ref"

    monkeypatch.setitem(sys.modules, "telethon", SimpleNamespace(events=SimpleNamespace(
        NewMessage=lambda **kwargs: ("new", kwargs),
        MessageEdited=lambda **kwargs: ("edit", kwargs),
    )))
    wake = asyncio.Event()
    assert asyncio.run(_register_and_catch_up(SimpleNamespace(client=Client()), Bridge(), wake)) == []
    assert calls == [
        ("register", "new"), ("register", "edit"),
        ("catch_up", 2), ("spool", "new"),
    ]
    assert wake.is_set()


def test_failed_capture_is_diagnosed_and_stops_the_serving_loop(monkeypatch):
    class Client:
        def add_event_handler(self, callback, _filter):
            self.callbacks = getattr(self, "callbacks", []) + [callback]
        async def catch_up(self):
            await self.callbacks[0](SimpleNamespace(chat_id=71))

    observed = []
    class Bridge:
        def spool_update(self, *_args, **_kwargs):
            raise OSError("private message must never appear in diagnostics")
        def _ingress_health(self, peer, code):
            observed.append((peer, code))

    monkeypatch.setitem(sys.modules, "telethon", SimpleNamespace(events=SimpleNamespace(
        NewMessage=lambda **kwargs: ("new", kwargs),
        MessageEdited=lambda **kwargs: ("edit", kwargs),
    )))
    wake = asyncio.Event()
    faults = asyncio.run(_register_and_catch_up(SimpleNamespace(client=Client()), Bridge(), wake))
    assert faults == [True] and wake.is_set()
    assert observed == [(71, "PRIVATE_STATE_UNAVAILABLE")]
