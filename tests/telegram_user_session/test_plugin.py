"""Offline extension-entrypoint contracts; no Telegram login, network or host runtime."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[2] / "skills" / "telegram-user-session"
MODULE_KEY = "_test_user_session_extension"
SPEC = importlib.util.spec_from_file_location(
    MODULE_KEY, SKILL_ROOT / "plugin.py", submodule_search_locations=[str(SKILL_ROOT)])
assert SPEC and SPEC.loader
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_KEY] = plugin
SPEC.loader.exec_module(plugin)
Config = plugin.Config
Binding = plugin.Binding
load_config = plugin.load_config
save_config = plugin.save_config
DurableStore = plugin.DurableStore
DossierStore = plugin.DossierStore
ActionStore = plugin.ActionStore


API_HASH = "a" * 32
BINDING = "b" * 32
CODE = "765432"
PASSWORD = "very-sensitive-2fa-password"
PHONE = "+15551234567"


class FakeAPI:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.tools = {}
        self.routes = {}
        self.sections = {}
        self.companions = []

    def get_state_dir(self):
        return str(self.state_dir)

    def register_tool(self, name, handler, *, description, schema, timeout_sec=60):
        self.tools[name] = (handler, description, schema)

    def register_route(self, path, handler, *, methods=("GET",)):
        self.routes[path] = (handler, tuple(methods))

    def register_settings_section(self, section_id, title, *, schema):
        self.sections[section_id] = (title, schema)

    def register_companion_process(self, name):
        self.companions.append(name)


class FakeRequest:
    def __init__(self, method="GET", body=None, host="198.51.100.8"):
        self.method = method
        self._body = body
        self.client = SimpleNamespace(host=host)

    async def json(self):
        return self._body


def route(api, name, method="GET", body=None, *, host="198.51.100.8"):
    handler, methods = api.routes[name]
    assert method in methods
    response = asyncio.run(handler(FakeRequest(method, body, host)))
    return response.status_code, json.loads(response.body.decode("utf-8"))


def setup(tmp_path):
    api = FakeAPI(tmp_path / "private-state")
    plugin.register(api)
    return api


def verified_account(api):
    save_config(api.state_dir, Config(api_id=12345, api_hash=API_HASH,
                                     phone=PHONE, binding_id=BINDING,
                                     allowed_peer_ids=(-1001234567890, 200),
                                     account_id=654321,
                                     bindings=(Binding(-1001234567890, 42, BINDING),
                                               Binding(200, None, BINDING))))


def test_registration_matches_manifest_and_does_not_start_telethon(tmp_path):
    api = setup(tmp_path)
    assert api.companions == ["mtproto"]
    assert set(api.tools) == {"send_text", "send_status", "dossier_add", "dossier_list", "dossier_delete_peer",
                              "action_edit", "action_delete", "action_react", "action_status"}
    assert api.routes.keys() == {"setup/config", "setup/request_code", "setup/login", "setup/status"}
    assert api.routes["setup/config"][1] == ("GET", "POST")
    assert api.routes["setup/status"][1] == ("GET", "POST")
    assert "NOT sent/delivered" in api.tools["send_text"][1]
    assert "UNKNOWN" in api.tools["send_text"][1]
    assert "UNKNOWN" in api.tools["send_status"][1]
    assert "source" in api.tools["dossier_add"][1]
    for name in ("action_edit", "action_delete", "action_react"):
        description, schema = api.tools[name][1:]
        assert "UNKNOWN" in description and "not mutation/delivery" in description
        assert "topic_id" in schema["required"]
        assert schema["additionalProperties"] is False
    assert "UNKNOWN" in api.tools["action_status"][1]
    assert "dossier_delete_user" not in api.tools
    assert not any(component.get("route") == "setup/login_reset"
                   for component in api.sections["user_session"][1]["components"])
    assert api.sections["user_session"][1]["components"]
    assert not api.state_dir.exists()  # registration has no login/state side effects


def test_owner_setup_reports_safe_specific_correction_without_echoing_secrets(tmp_path, monkeypatch):
    api = setup(tmp_path)
    code, body = route(api, "setup/config", "POST", {"api_id": "bad", "api_hash": "secret"})
    assert code == 400 and body["code"] == "configuration_invalid"
    assert "api_id" in body["message"] and "secret" not in json.dumps(body)
    verified_account(api)
    from userbot.config import save_login_challenge
    save_login_challenge(api.state_dir, "test-challenge")
    code, body = route(api, "setup/login", "POST", {"code": "not-a-code", "password": PASSWORD})
    assert code == 400 and body["code"] == "login_input_invalid"
    assert "code" in body["message"] and PASSWORD not in json.dumps(body)
    def busy(*_args, **_kwargs):
        raise plugin.ConfigError("config lock unavailable")
    monkeypatch.setattr(plugin, "update_config", busy)
    code, body = route(api, "setup/config", "POST", {"api_id": "12345"})
    assert code == 503 and body["code"] == "state_lock_unavailable"


def test_actual_config_lock_contention_returns_retryable_503(tmp_path, monkeypatch):
    from userbot import config as config_module
    api = setup(tmp_path)
    verified_account(api)
    real_update = config_module.update_config
    monkeypatch.setattr(plugin, "update_config", lambda root, transform:
                        real_update(root, transform, timeout_sec=0.01))
    with config_module._config_lock(api.state_dir, 1):
        code, body = route(api, "setup/config", "POST", {"api_id": "12345"})
    assert code == 503 and body["code"] == "state_lock_unavailable"
    assert "api_hash" not in json.dumps(body)


def test_unconfigured_and_unallowlisted_sends_do_not_persist(tmp_path):
    api = setup(tmp_path)
    send = api.tools["send_text"][0]
    with pytest.raises(plugin.PluginInputError, match="account"):
        send("one", 200, "hello")
    verified_account(api)
    with pytest.raises(plugin.PluginInputError, match="allowlist"):
        send("one", 201, "hello")
    assert not (api.state_dir / "outgoing").exists()


def test_send_requires_owner_created_presence_binding_before_enqueue(tmp_path):
    api = setup(tmp_path)
    save_config(api.state_dir, Config(api_id=12345, api_hash=API_HASH, phone=PHONE,
                                     allowed_peer_ids=(200,), account_id=654321))
    with pytest.raises(plugin.PluginInputError, match="Presence binding"):
        api.tools["send_text"][0]("unbound", 200, "hello")
    assert not (api.state_dir / "outgoing").exists()
    assert not (api.state_dir / "transport.sqlite3").exists()


def test_durable_payload_before_queue_and_exact_intent_replay(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    send = api.tools["send_text"][0]
    first = json.loads(send("intent-1", -1001234567890, "Hello 🌿", 42, 45))
    assert first["status"] == "queued"
    assert "delivery" in first["note"]
    db = api.state_dir / "transport.sqlite3"
    with DurableStore(db) as store:
        job = store.get("654321", "intent-1")
        assert job and job.status == "queued" and job.random_id != 0
        assert job.topic_id == 42 and job.reply_id == 45
        ref, random_id = job.payload_ref, job.random_id
    target = api.state_dir / "outgoing" / (ref + ".json")
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "text": "Hello 🌿", "peer_id": -1001234567890, "topic_id": 42,
        "reply_id": 45, "binding_id": BINDING,
    }
    original = target.read_bytes()
    assert json.loads(send("intent-1", -1001234567890, "Hello 🌿", 42, 45))["status"] == "queued"
    assert target.read_bytes() == original
    with DurableStore(db) as store:
        assert store.get("654321", "intent-1").random_id == random_id
    with pytest.raises(plugin.PluginInputError, match="different payload"):
        send("intent-1", -1001234567890, "Changed", 42, 45)
    assert target.read_bytes() == original
    assert not any((api.state_dir / "outgoing").glob("*.tmp"))


def test_send_status_never_promotes_attempt_to_delivery(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    send, status = api.tools["send_text"][0], api.tools["send_status"][0]
    assert json.loads(status("unseen"))["status"] == "not_found"
    assert json.loads(send("ambig", 200, "Hello"))["status"] == "queued"
    with DurableStore(api.state_dir / "transport.sqlite3") as store:
        claimed = store.claim("654321")
        assert claimed and claimed.lease_token
        store.mark_attempted("654321", "ambig", claimed.lease_token)
    assert json.loads(status("ambig"))["status"] == "attempted"
    with DurableStore(api.state_dir / "transport.sqlite3", recover_attempted=True):
        pass
    assert json.loads(status("ambig"))["status"] == "unknown"
    assert json.loads(send("ambig", 200, "Hello"))["status"] == "unknown"
    with DurableStore(api.state_dir / "transport.sqlite3") as store:
        store.settle("654321", "ambig", claimed.lease_token,
                     success=True, remote_message_id=125)
    result = json.loads(status("ambig"))
    assert result == {"intent_id": "ambig", "status": "sent", "remote_message_id": 125}


def test_oversized_reply_id_is_rejected_before_intent_published(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    with pytest.raises(plugin.PluginInputError, match="int32"):
        api.tools["send_text"][0]("bad-reply", 200, "Hello", None, 2**31)
    assert not (api.state_dir / "outgoing").exists()


def test_send_intent_is_frozen_to_presence_binding(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    send = api.tools["send_text"][0]
    assert json.loads(send("bound", 200, "One"))["status"] == "queued"
    save_config(api.state_dir, Config(api_id=12345, api_hash=API_HASH,
                                     phone=PHONE, binding_id="c" * 32,
                                     allowed_peer_ids=(200,), account_id=654321,
                                     bindings=(Binding(200, None, "c" * 32),)))
    with pytest.raises(plugin.PluginInputError, match="different payload"):
        send("bound", 200, "One")
    with DurableStore(api.state_dir / "transport.sqlite3") as store:
        ref = store.get("654321", "bound").payload_ref
    assert json.loads((api.state_dir / "outgoing" / (ref + ".json")).read_text())["binding_id"] == BINDING


def test_payload_escaping_can_grow_beyond_text_byte_limit_without_false_mismatch(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    result = json.loads(api.tools["send_text"][0]("controls", 200, "x" + "\n" * 4095))
    assert result["status"] == "queued"
    assert json.loads(api.tools["send_text"][0]("controls", 200, "x" + "\n" * 4095))["status"] == "queued"


@pytest.mark.parametrize("intent,peer,text,topic,reply", [
    ("../bad", 200, "hello", None, None),
    ("good", True, "hello", None, None),
    ("good", 200, "", None, None),
    ("good", 200, "x" * 4097, None, None),
    ("good", 200, "\ud800", None, None),
    ("good", 200, "hello", 0, None),
    ("good", 200, "hello", None, True),
])
def test_invalid_send_rejected_before_payload(tmp_path, intent, peer, text, topic, reply):
    api = setup(tmp_path)
    verified_account(api)
    with pytest.raises(plugin.PluginInputError):
        api.tools["send_text"][0](intent, peer, text, topic, reply)
    assert not (api.state_dir / "outgoing").exists()


def test_setup_and_status_never_echo_credentials_login_or_session(tmp_path):
    api = setup(tmp_path)
    status_code, result = route(api, "setup/config", "POST", {
        "api_id": "12345", "api_hash": API_HASH, "phone": PHONE,
        "binding_id": BINDING, "allowed_peer_ids": "-1001234567890,200",
    })
    assert status_code == 200 and result["ok"] is True
    config = load_config(api.state_dir)
    assert config and config.api_hash == API_HASH and config.phone == PHONE
    assert config.allowed_peer_ids == (-1001234567890, 200)
    config_get = route(api, "setup/config")[1]
    assert config_get == {"api_id": "12345", "binding_id": BINDING,
                          "bindings": "[]",
                          "allowed_peer_ids": "-1001234567890,200"}
    assert route(api, "setup/status")[1]["account_id_recorded"] is False
    assert "not recorded" in route(api, "setup/status", "POST", {})[1]["message"]
    from userbot.config import save_login_challenge
    save_login_challenge(api.state_dir, "test-challenge")
    login_code, login_result = route(api, "setup/login", "POST", {"code": CODE, "password": PASSWORD})
    assert login_code == 200 and login_result["ok"] is True
    (api.state_dir / "mtproto.session").write_bytes(b"secret-session-bytes")
    status_result = route(api, "setup/status")[1]
    assert status_result["session_present"] is True
    for projection in (config_get, status_result, login_result, result):
        encoded = json.dumps(projection)
        assert all(secret not in encoded for secret in (API_HASH, PHONE, CODE, PASSWORD, "secret-session-bytes"))
    assert (api.state_dir / "auth-handoff.json").exists()
    assert status_code == 200  # A request through the normal gateway, not Telethon.


def test_stale_running_heartbeat_is_not_live_companion_health(tmp_path):
    # Review defect 2: companion-health.json "running" written once at startup
    # stays forever after an abrupt kill; the status route must treat a stale
    # heartbeat as unknown instead of live health.
    api = setup(tmp_path)
    verified_account(api)
    state = api.state_dir
    now = datetime.now(timezone.utc)
    (state / "companion-health.json").write_text(json.dumps(
        {"category": "running", "at": now.isoformat()}), encoding="utf-8")
    fresh = route(api, "setup/status")[1]
    assert fresh["companion_category"] == "running"
    (state / "companion-health.json").write_text(json.dumps(
        {"category": "running", "at": (now - timedelta(seconds=120)).isoformat()}),
        encoding="utf-8")
    stale = route(api, "setup/status")[1]
    assert stale["companion_category"] == "unknown"
    (state / "companion-health.json").write_text(json.dumps(
        {"category": "running", "at": now.replace(tzinfo=None).isoformat()}),
        encoding="utf-8")
    naive = route(api, "setup/status")[1]
    assert naive["companion_category"] == "unknown"


def test_status_ingress_projection_keeps_blocked_peer_visible(tmp_path):
    # Multi-peer ingress projection: a successful peer never erases a blocked
    # peer's repair warning, and peers outside the allowlist stay invisible.
    api = setup(tmp_path)
    verified_account(api)
    state = api.state_dir
    blocked = {"code": "HOST_UNAVAILABLE", "attempts": 5, "repair_needed": True}
    healthy = {"code": "TELEGRAM_READ", "attempts": 1, "repair_needed": False}
    (state / "inbound-health.json").write_text(json.dumps(
        {"peers": {"-1001234567890": blocked, "200": healthy,
                   "999": {"code": "HOST_UNAVAILABLE", "attempts": 9,
                           "repair_needed": True}},
         "at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    result = route(api, "setup/status")[1]
    assert result["inbound_last_error"] == "HOST_UNAVAILABLE"
    assert result["inbound_repair_needed"] is True
    assert result["inbound_affected_peers"] == 2  # 999 is not allowlisted


def test_session_freezes_credentials_and_rejects_secret_echo(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    (api.state_dir / "mtproto.session").write_bytes(b"session-secret")
    before = (api.state_dir / "config.json").read_bytes()
    code, result = route(api, "setup/config", "POST", {"api_id": "12345", "api_hash": "c" * 32})
    assert code == 400 and result["ok"] is False
    assert "c" * 32 not in json.dumps(result)
    assert (api.state_dir / "config.json").read_bytes() == before
    code, result = route(api, "setup/config", "POST", {"api_id": "12345",
                                                        "allowed_peer_ids": "200"})
    assert code == 400 and result["ok"] is False  # cannot leave stale binding to removed peer
    code, result = route(api, "setup/config", "POST", {"api_id": "12345",
                                                        "allowed_peer_ids": "200", "bindings": "[]"})
    assert code == 200 and result["ok"] is True
    assert load_config(api.state_dir).account_id == 654321
    assert load_config(api.state_dir).allowed_peer_ids == (200,)


def test_partial_setup_merges_latest_companion_change_not_old_snapshot(tmp_path, monkeypatch):
    api = setup(tmp_path)
    verified_account(api)
    original = plugin.update_config

    def companion_updates_before_setup_lock(state_dir, transform):
        # A companion update becomes durable just before the route acquires
        # its lock. The form omits both account and bindings/allowlist.
        original(state_dir, lambda latest: replace(latest, account_id=67890,
                                                   allowed_peer_ids=(200,),
                                                   bindings=(Binding(200, None, "c" * 32),)))
        return original(state_dir, transform)

    monkeypatch.setattr(plugin, "update_config", companion_updates_before_setup_lock)
    code, result = route(api, "setup/config", "POST", {"binding_id": "d" * 32})
    assert code == 200 and result["ok"]
    current = load_config(api.state_dir)
    assert current.account_id == 67890
    assert current.allowed_peer_ids == (200,)
    assert current.bindings == (Binding(200, None, "c" * 32),)
    assert current.binding_id == "d" * 32


def test_setup_lock_timeout_does_not_persist_or_expose_values(tmp_path, monkeypatch):
    api = setup(tmp_path)
    verified_account(api)
    before = (api.state_dir / "config.json").read_bytes()
    original = plugin.update_config
    monkeypatch.setattr(plugin, "update_config", lambda state, transform:
                        original(state, transform, timeout_sec=0))
    import threading
    entered, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=lambda: original(
        api.state_dir, lambda current: (entered.set(), release.wait(5), current)[2]))
    holder.start()
    try:
        assert entered.wait(5)
        code, result = route(api, "setup/config", "POST", {"binding_id": "d" * 32})
        assert code == 503 and result["ok"] is False
        assert result["code"] == "state_lock_unavailable"
        assert "d" * 32 not in json.dumps(result)
        assert (api.state_dir / "config.json").read_bytes() == before
    finally:
        release.set()
        holder.join(5)
    assert not holder.is_alive()


def test_settings_hydration_gets_are_2xx_and_side_effect_free(tmp_path):
    # Review defect: the host hydrates each form/action route with a bodyless
    # GET; a non-2xx answer other than 404/405 degrades the whole section.
    api = setup(tmp_path)
    verified_account(api)
    for name in ("setup/request_code", "setup/login"):
        status, body = route(api, name)
        assert status == 200 and body == {}
    # A GET probe must never stage a login request or consume a handoff.
    assert not (api.state_dir / "login-request.json").exists()
    assert not (api.state_dir / "auth-handoff.json").exists()


def test_login_code_requires_explicit_request_and_duplicate_reconciliation(tmp_path):
    api = setup(tmp_path)
    missing_code, missing = route(api, "setup/request_code", "POST", {})
    assert missing_code == 409 and missing["code"] == "configuration_missing"
    malformed_code, malformed = route(api, "setup/request_code", "POST", {"password": PASSWORD})
    assert malformed_code == 400 and malformed["code"] == "request_invalid"
    assert PASSWORD not in json.dumps(malformed)
    verified_account(api)
    assert not (api.state_dir / "login-request.json").exists()
    code, body = route(api, "setup/request_code", "POST", {})
    assert code == 200 and body["ok"] is True
    marker = api.state_dir / "login-request.json"
    assert json.loads(marker.read_text()) == {"request_code": True}
    assert route(api, "setup/status")[1]["code_request_status"] == "queued"
    pending_code, pending = route(api, "setup/request_code", "POST", {})
    assert pending_code == 409 and pending["code"] == "login_code_pending"
    assert "setup/login_reset" not in api.routes
    inflight = api.state_dir / "login-request.inflight"
    marker.rename(inflight)  # companion claim; physical delivery still unknown
    assert route(api, "setup/status")[1]["code_request_status"] == "unknown"
    assert route(api, "setup/request_code", "POST", {})[0] == 409
    (api.state_dir / "login-challenge.json").write_text('{"secret":"never-echo"}')
    assert route(api, "setup/request_code", "POST", {})[0] == 409
    assert inflight.exists()  # no online reset can erase a physically in-flight request
    assert (api.state_dir / "login-challenge.json").exists()
    assert "never-echo" not in json.dumps(route(api, "setup/status")[1])


def test_malformed_setup_body_refused_without_value_echo(tmp_path):
    api = setup(tmp_path)
    for path, bad_body in [("setup/config", {"api_hash": API_HASH, "unexpected": PASSWORD}),
                           ("setup/login", {"code": "not-a-code-" + CODE})]:
        status, body = route(api, path, "POST", bad_body)
        assert status == 400
        assert body["ok"] is False
        assert API_HASH not in json.dumps(body) and PASSWORD not in json.dumps(body)


def test_gateway_proxy_client_address_is_not_treated_as_owner_identity(tmp_path):
    # The OOP proxy synthesizes loopback; handlers do not use request.client as
    # an identity proof. Access control belongs to the normal gateway network
    # authentication; deployment without that password must not expose the port.
    api = setup(tmp_path)
    code, body = route(api, "setup/config", "POST", {"api_id": "12345", "api_hash": API_HASH,
                                                         "phone": PHONE}, host="127.0.0.1")
    assert code == 200 and body["ok"]
    assert "client.host" not in plugin._make_config_route.__code__.co_names


def test_dossier_tools_explicit_source_and_peer_isolation(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    add = api.tools["dossier_add"][0]
    listed = api.tools["dossier_list"][0]
    delete = api.tools["dossier_delete_peer"][0]
    first_peer, second_peer = 200, -1001234567890
    when = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    assert not (api.state_dir / "dossier.sqlite3").exists()  # registration is not ingestion
    first = json.loads(add(first_peer, 77, 101, when, "Likes roses", 2))
    second = json.loads(add(second_peer, 77, 202, when, "Shares work schedule", 7))
    assert first["source_message_id"] == 101 and first["source_peer_id"] == first_peer
    assert first["source_at"] == when and first["fact"] == "Likes roses"
    assert second["source_peer_id"] == second_peer
    assert json.loads(add(first_peer, 77, 101, when, "Likes roses", 2))["id"] == first["id"]
    assert [fact["fact"] for fact in json.loads(listed(first_peer, 77))["facts"]] == ["Likes roses"]
    assert [fact["fact"] for fact in json.loads(listed(second_peer, 77))["facts"]] == ["Shares work schedule"]
    assert json.loads(listed(first_peer, 78))["facts"] == []
    with pytest.raises(plugin.PluginInputError, match="confirm"):
        delete(first_peer, 77, False)
    assert json.loads(delete(first_peer, 77, True))["deleted"] == 1
    assert json.loads(listed(first_peer, 77))["facts"] == []
    assert len(json.loads(listed(second_peer, 77))["facts"]) == 1


def test_dossier_rejects_unknown_peer_and_different_account(tmp_path):
    api = setup(tmp_path)
    add, listed, delete = (api.tools[name][0] for name in
                           ("dossier_add", "dossier_list", "dossier_delete_peer"))
    when = datetime.now(timezone.utc).isoformat()
    with pytest.raises(plugin.PluginInputError, match="account"):
        add(200, 77, 101, when, "unauthorized")
    assert not (api.state_dir / "dossier.sqlite3").exists()
    verified_account(api)
    json.loads(add(200, 77, 101, when, "Only account A"))
    for call in (lambda: add(201, 77, 103, when, "wrong peer"),
                 lambda: listed(201, 77), lambda: delete(201, 77, True)):
        with pytest.raises(plugin.PluginInputError, match="allowlist"):
            call()
    save_config(api.state_dir, Config(api_id=12345, api_hash=API_HASH, phone=PHONE,
                                     binding_id=BINDING, allowed_peer_ids=(200,), account_id=654322))
    assert json.loads(listed(200, 77))["facts"] == []
    assert json.loads(delete(200, 77, True))["deleted"] == 0
    with DossierStore(api.state_dir / "dossier.sqlite3") as store:
        assert store.list_facts(654321, 77, peer_id=200)[0].fact == "Only account A"
    # Even a previously allowlisted peer is denied when removed from config.
    save_config(api.state_dir, Config(api_id=12345, api_hash=API_HASH, phone=PHONE,
                                     binding_id=BINDING, allowed_peer_ids=(), account_id=654321))
    with pytest.raises(plugin.PluginInputError, match="allowlist"):
        listed(200, 77)


@pytest.mark.parametrize("peer,user,message,stamp,fact,ttl", [
    (200, True, 101, "now", "fact", 90),
    (200, -77, 101, "now", "fact", 90),
    (200, 77, 0, "now", "fact", 90),
    (200, 77, True, "now", "fact", 90),
    (True, 77, 101, "now", "fact", 90),
    (200, 77, 101, "2026-10-12T12:00:00", "fact", 90),
    (200, 77, 101, "bad date", "fact", 90),
    (200, 77, 101, "now", "", 90),
    (200, 77, 101, "now", "too long" * 75, 90),
    (200, 77, 101, "now", "fact", 0),
    (200, 77, 101, "now", "fact", True),
    (200, 77, 101, "now", "fact", 366),
])
def test_dossier_invalid_add_has_no_written_fact(tmp_path, peer, user, message, stamp, fact, ttl):
    api = setup(tmp_path)
    verified_account(api)
    when = datetime.now(timezone.utc).isoformat() if stamp == "now" else stamp
    with pytest.raises(plugin.PluginInputError):
        api.tools["dossier_add"][0](peer, user, message, when, fact, ttl)
    assert not (api.state_dir / "dossier.sqlite3").exists() or json.loads(
        api.tools["dossier_list"][0](200, 77))["facts"] == []


def test_dossier_expiry_is_hidden_without_periodic_ingestion(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    add = api.tools["dossier_add"][0]
    listed = api.tools["dossier_list"][0]
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    json.loads(add(200, 77, 105, yesterday, "temporary", 2))
    assert len(json.loads(listed(200, 77))["facts"]) == 1
    # The actual SQLite store owns expiry; no Telegram listener is invoked.
    with DossierStore(api.state_dir / "dossier.sqlite3",
                      clock=lambda: datetime.now(timezone.utc).timestamp() + 3 * 86400) as store:
        assert store.list_facts(654321, 77, peer_id=200) == []
        assert store.purge_expired() == 1
    assert json.loads(listed(200, 77))["facts"] == []


def test_exact_binding_map_settings_and_send_reject_unmapped_topic(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    mappings = [{"peer_id": -1001234567890, "topic_id": 42, "binding_id": BINDING},
                {"peer_id": 200, "topic_id": None, "binding_id": "c" * 32},
                {"peer_id": 200, "topic_id": 59, "binding_id": "d" * 32}]
    code, result = route(api, "setup/config", "POST", {"bindings": json.dumps(mappings)})
    assert code == 200 and result["ok"]
    assert json.loads(route(api, "setup/config")[1]["bindings"]) == mappings
    assert load_config(api.state_dir).binding_for(200, 59) == "d" * 32
    send = api.tools["send_text"][0]
    with pytest.raises(plugin.PluginInputError, match="exact Presence binding"):
        send("general-unmapped", -1001234567890, "not sent")
    with pytest.raises(plugin.PluginInputError, match="exact Presence binding"):
        send("unknown-topic", 200, "not sent", 60)
    assert not (api.state_dir / "outgoing").exists()
    assert json.loads(send("peer-topic", 200, "mapped", 59))["status"] == "queued"
    with DurableStore(api.state_dir / "transport.sqlite3") as store:
        ref = store.get("654321", "peer-topic").payload_ref
    assert json.loads((api.state_dir / "outgoing" / (ref + ".json")).read_text())["binding_id"] == "d" * 32
    # Omitting mapping from an unrelated form edit preserves the exact table.
    assert route(api, "setup/config", "POST", {"allowed_peer_ids": "-1001234567890,200"})[0] == 200
    assert load_config(api.state_dir).bindings == tuple(Binding(**entry) for entry in mappings)


@pytest.mark.parametrize("bindings", [
    "[] garbage",
    "{}",
    '[{"peer_id":200,"topic_id":null,"binding_id":"' + BINDING + '","peer_id":201}]',
    json.dumps([{"peer_id": 200, "topic_id": True, "binding_id": BINDING}]),
    json.dumps([{"peer_id": 200, "topic_id": 1, "binding_id": BINDING}]),
    json.dumps([{"peer_id": 201, "topic_id": None, "binding_id": BINDING}]),
    json.dumps([{"peer_id": 200, "topic_id": None, "binding_id": BINDING},
                {"peer_id": 200, "topic_id": None, "binding_id": BINDING}]),
    json.dumps([{"peer_id": 200, "topic_id": 42, "binding_id": BINDING},
                {"peer_id": 200, "topic_id": None, "binding_id": BINDING}]),
])
def test_settings_reject_ambiguous_binding_maps_without_mutation(tmp_path, bindings):
    api = setup(tmp_path)
    verified_account(api)
    before = (api.state_dir / "config.json").read_bytes()
    code, result = route(api, "setup/config", "POST", {"bindings": bindings})
    assert code == 400 and result["ok"] is False
    assert (api.state_dir / "config.json").read_bytes() == before
    assert BINDING not in json.dumps(result)


@pytest.mark.parametrize("name,payload_key,value", [
    ("action_edit", "text", "Replace 🌿"),
    ("action_react", "emoji", "👍"),
])
def test_action_payload_is_private_immutable_and_digest_bound(tmp_path, name, payload_key, value):
    api = setup(tmp_path)
    verified_account(api)
    action = api.tools[name][0]
    first = json.loads(action("change-1", -1001234567890, 101, 42, value))
    assert first["status"] == "queued" and "not Telegram" in first["note"]
    with ActionStore(api.state_dir / "actions.sqlite3") as store:
        job = store.get("654321", "change-1")
        assert job and job.status == "queued" and job.binding_id == BINDING
        assert job.peer_id == -1001234567890 and job.topic_id == 42 and job.message_id == 101
    path = api.state_dir / "actions" / (job.payload_ref + ".json")
    original = path.read_bytes()
    assert hashlib.sha256(original).hexdigest() == job.payload_sha256
    assert json.loads(original) == {"operation": name.removeprefix("action_"),
                                    "peer_id": -1001234567890, "topic_id": 42,
                                    "message_id": 101, "binding_id": BINDING,
                                    payload_key: value}
    assert json.loads(action("change-1", -1001234567890, 101, 42, value))["status"] == "queued"
    with pytest.raises(plugin.PluginInputError, match="different payload"):
        action("change-1", -1001234567890, 101, 42, value + "!")
    with pytest.raises(plugin.PluginInputError, match="different payload"):
        action("change-1", -1001234567890, 102, 42, value)
    assert path.read_bytes() == original
    assert not list((api.state_dir / "actions").glob("*.tmp"))


def test_delete_has_no_payload_and_frozen_binding_and_scope(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    delete, react = (api.tools[name][0] for name in ("action_delete", "action_react"))
    assert json.loads(delete("del-1", 200, 55, None))["status"] == "queued"
    assert not (api.state_dir / "actions").exists()
    with ActionStore(api.state_dir / "actions.sqlite3") as store:
        job = store.get("654321", "del-1")
        assert job and job.binding_id == BINDING
        assert job.payload_ref is None and job.payload_sha256 is None
    assert json.loads(delete("del-1", 200, 55, None))["status"] == "queued"
    with pytest.raises(plugin.PluginInputError, match="different action"):
        delete("del-1", 200, 56, None)
    save_config(api.state_dir, Config(api_id=12345, api_hash=API_HASH, phone=PHONE,
                                     allowed_peer_ids=(200,), account_id=654321,
                                     bindings=(Binding(200, None, "c" * 32),)))
    with pytest.raises(plugin.PluginInputError, match="different action"):
        delete("del-1", 200, 55, None)
    assert json.loads(api.tools["action_status"][0]("del-1"))["status"] == "not_found"
    with pytest.raises(plugin.PluginInputError, match="different action"):
        react("del-1", 200, 55, None, "👍")


def test_action_status_attempted_unknown_and_revocation(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    action, status = api.tools["action_delete"][0], api.tools["action_status"][0]
    assert json.loads(status("unseen"))["status"] == "not_found"
    action("ambiguous", 200, 56, None)
    with ActionStore(api.state_dir / "actions.sqlite3") as store:
        claimed = store.claim("654321")
        assert claimed and claimed.lease_token
        store.mark_attempted("654321", "ambiguous", claimed.lease_token)
    assert json.loads(status("ambiguous"))["status"] == "attempted"
    with ActionStore(api.state_dir / "actions.sqlite3", recover_attempted=True):
        pass
    report = json.loads(status("ambiguous"))
    assert report["status"] == "unknown" and "Never auto-resend" in report["note"]
    assert json.loads(action("ambiguous", 200, 56, None))["status"] == "unknown"
    # A revoked peer cannot query a prior action or queue a new one.
    save_config(api.state_dir, Config(api_id=12345, api_hash=API_HASH, phone=PHONE,
                                     allowed_peer_ids=(), account_id=654321))
    assert json.loads(status("ambiguous"))["status"] == "not_found"
    with pytest.raises(plugin.PluginInputError, match="allowlist"):
        action("fresh", 200, 56, None)


@pytest.mark.parametrize("operation,message,topic,value", [
    ("action_edit", 0, None, "ok"),
    ("action_edit", True, None, "ok"),
    ("action_edit", 2**31, None, "ok"),
    ("action_edit", 1, 1, "ok"),
    ("action_edit", 1, 42, ""),
    ("action_edit", 1, 42, "\ud800"),
    ("action_edit", 1, 42, "x" * 4097),
    ("action_react", 1, 42, "👍 "),
    ("action_react", 1, 42, "👍" * 33),
])
def test_invalid_actions_have_no_custody(tmp_path, operation, message, topic, value):
    api = setup(tmp_path)
    verified_account(api)
    with pytest.raises(plugin.PluginInputError):
        api.tools[operation][0]("invalid", -1001234567890, message, topic, value)
    assert not (api.state_dir / "actions.sqlite3").exists()


def test_action_needs_configured_account_allowlist_and_exact_topic_binding(tmp_path):
    api = setup(tmp_path)
    edit = api.tools["action_edit"][0]
    with pytest.raises(plugin.PluginInputError, match="account"):
        edit("one", 200, 1, None, "hello")
    verified_account(api)
    with pytest.raises(plugin.PluginInputError, match="allowlist"):
        edit("one", 201, 1, None, "hello")
    with pytest.raises(plugin.PluginInputError, match="exact Presence binding"):
        edit("one", -1001234567890, 1, None, "hello")
    with pytest.raises(plugin.PluginInputError, match="exact Presence binding"):
        edit("one", 200, 1, 42, "hello")
    assert not (api.state_dir / "actions").exists()
    assert not (api.state_dir / "actions.sqlite3").exists()


def test_action_tool_results_never_echo_private_values(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    private_text = "please do not echo this private text"
    private_reaction = "🌿"
    edit = api.tools["action_edit"][0]
    outputs = [edit("sensitive", 200, 55, None, private_text),
               api.tools["action_react"][0]("sensitive-reaction", 200, 55, None, private_reaction),
               api.tools["action_status"][0]("sensitive")]
    for output in outputs:
        for secret in (private_text, private_reaction, API_HASH, PHONE, CODE, PASSWORD, BINDING):
            assert secret not in output


def test_setup_status_names_corrupt_private_queue_without_raw_database_text(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    secret = "secret-queue-content"
    (api.state_dir / "transport.sqlite3").write_bytes(secret.encode("ascii"))
    code, body = route(api, "setup/status", "POST", {})
    assert code == 200 and body["ok"] is False
    assert body["queue_status"] == "storage_unavailable"
    assert body["inbound_pending"] is None and body["reports"]["pending"] is None
    assert "back up private skill state" in body["message"]
    assert secret not in json.dumps(body)


def test_password_required_and_already_authorized_code_status_are_actionable(tmp_path):
    from scripts.companion import _login_if_requested, _retire_unused_code_request
    from userbot.config import save_login_challenge
    api = setup(tmp_path)
    verified_account(api)
    save_login_challenge(api.state_dir, "safe-challenge-token")
    from userbot.config import stage_login_secret
    stage_login_secret(api.state_dir, code="123456")
    class SessionPasswordNeededError(Exception):
        pass
    class Login:
        async def authorization_status(self):
            return False
        async def complete_login(self, *_args, **_kwargs):
            raise SessionPasswordNeededError()
    cfg = load_config(api.state_dir)
    assert asyncio.run(_login_if_requested(api.state_dir, cfg, Login())) is False
    code, body = route(api, "setup/status", "POST", {})
    assert code == 200 and body["code_request_status"] == "password_required"
    assert "2FA password" in body["message"]
    assert route(api, "setup/request_code", "POST", {})[0] == 409
    assert "safe-challenge-token" not in json.dumps(body)

    (api.state_dir / "login-2fa-required.json").unlink()
    (api.state_dir / "login-challenge.json").unlink()
    code, _ = route(api, "setup/request_code", "POST", {})
    assert code == 200
    _retire_unused_code_request(api.state_dir)
    assert not (api.state_dir / "login-request.json").exists()
    assert route(api, "setup/status", "POST", {})[1]["code_request_status"] == "already_authorized"


def test_definite_login_code_refusal_retires_inflight_marker(tmp_path):
    from scripts.companion import _login_if_requested
    from telethon.errors import PhoneNumberInvalidError
    api = setup(tmp_path)
    verified_account(api)
    assert route(api, "setup/request_code", "POST", {})[0] == 200
    class Login:
        async def authorization_status(self):
            return False
        async def request_login_code(self, *_args):
            raise PhoneNumberInvalidError(None)
    with pytest.raises(PhoneNumberInvalidError):
        asyncio.run(_login_if_requested(api.state_dir, load_config(api.state_dir), Login()))
    assert not (api.state_dir / "login-request.inflight").exists()
    body = route(api, "setup/status", "POST", {})[1]
    assert body["code_request_status"] == "definitely_refused"
    assert "phone" not in json.dumps(body).lower()


def test_code_input_before_challenge_is_not_consumed(tmp_path):
    api = setup(tmp_path)
    verified_account(api)
    code, body = route(api, "setup/login", "POST", {"code": CODE})
    assert code == 409 and body["code"] == "login_not_ready"
    assert not (api.state_dir / "auth-handoff.json").exists()
