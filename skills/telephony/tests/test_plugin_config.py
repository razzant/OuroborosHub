import importlib
import json
import sys
import types

import pytest

from conftest import import_plugin
from phone import config as cfg
from phone.provider import ProviderError
from phone.sip_provider import sip_config

plugin = import_plugin()


def test_live_call_id_from_execution_transport_and_legacy_key():
    ctx = types.SimpleNamespace(task_metadata={"execution_transport": {"kind": "live_conversation", "call_id": "c1"}})
    assert plugin.live_call_id(ctx) == "c1"
    assert plugin.live_call_id(types.SimpleNamespace(task_metadata={"live_call": {"call_id": "c2"}})) == "c2"
    other = types.SimpleNamespace(task_metadata={"execution_transport": {"kind": "worker", "call_id": "x"}})
    assert plugin.live_call_id(other) == "" and plugin.live_call_id(None) == ""


def test_tools_without_companion_say_so(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_STATE_DIR", tmp_path)
    reply = plugin.phone_call(None, number="+74951234567", purpose="x", owner_approved=True)
    assert reply.startswith("⚠️ PHONE_COMPANION_UNAVAILABLE")
    status = json.loads(plugin.phone_status(None).split("\n", 1)[-1])
    assert status["ok"] is False and status["in_live_call"] is False


def test_register_declares_everything():
    calls = {}

    class Api:
        def get_state_dir(self): return "/tmp/x"
        def register_tool(self, name, handler, *, description, schema, timeout_sec=60):
            assert len(name) <= 24 and schema["type"] == "object"
            calls.setdefault("tools", []).append(name)
        def register_route(self, path, handler, *, methods=("GET",)): calls.setdefault("routes", []).append(path)
        def register_settings_section(self, sid, title, *, schema): calls["section"] = sid
        def register_companion_process(self, name): calls["companion"] = name

    plugin.register(Api())
    assert calls == {"tools": ["phone_call", "phone_hangup", "phone_send_dtmf", "phone_status"],
                     "routes": ["status", "settings/save"], "section": "connection", "companion": "phone_line"}


def test_form_roundtrip_and_validation(tmp_path):
    base = cfg.load(tmp_path)
    assert base["inbound"]["policy"] == "off" and base["outbound"]["allowed_prefixes"] == []
    updated = cfg.from_form(base, {"profile": "twilio", "registrar": "acme.sip.twilio.com", "username": "agent",
                                   "expires": "300", "allowed_prefixes": "+1\n+44", "inbound_policy": "allowlist",
                                   "inbound_allowlist": "+15550001001, +15550001002"})
    assert updated["sip"]["expires"] == 600  # Twilio's minimum
    assert updated["outbound"]["allowed_prefixes"] == ["+1", "+44"]
    assert updated["inbound"]["allowlist"] == ["+15550001001", "+15550001002"]
    cfg.save(tmp_path, updated)
    assert (tmp_path / "config.json").stat().st_mode & 0o777 == 0o600
    form = cfg.to_form(cfg.load(tmp_path))
    assert form["allowed_prefixes"] == "+1\n+44" and form["profile"] == "twilio"
    assert "password" not in json.dumps(form).lower()
    for bad in ({"profile": "other"}, {"inbound_policy": "maybe"}, {"port": "x"}, {"unknown": "1"},
                {"profile": "twilio", "registrar": "sip.example.com"}):
        with pytest.raises(ValueError):
            cfg.from_form(base, bad)


def test_form_refuses_values_that_would_break_headers_or_sockets(tmp_path):
    base = cfg.load(tmp_path)
    for bad in ({"outbound_proxy": "proxy.example:abc"}, {"outbound_proxy": "proxy.example:70000"},
                {"username": "agent@evil"}, {"caller_id": "+1<script>"}, {"registrar": "pbx example"},
                {"domain": "a\x00b"}, {"display_name": 'x"y'}, {"trusted_sources": "10.0.0.0/33"},
                {"port": "²"}):
        with pytest.raises(ValueError):
            cfg.from_form(base, bad)
    ok = cfg.from_form(base, {"outbound_proxy": "sip.example.com:5070", "registrar": "acme.sip.twilio.com",
                              "trusted_sources": "54.172.60.0/30\nsip.example.com", "username": "agent"})
    assert ok["sip"]["trusted_sources"] == ["54.172.60.0/30", "sip.example.com"]


# -- Form rejects IPv6 and 0.0.0.0/0 in Extra hosts (trusted_sources) --------------------------------------

def test_form_rejects_ipv6_and_wildcard_cidr_in_trusted_sources(tmp_path):
    base = cfg.load(tmp_path)
    for bad in ("2001:db8::1", "[2001:db8::1]", "2001:db8::/32", "0.0.0.0/0", "999.999.999.999"):
        with pytest.raises(ValueError):
            cfg.from_form(base, {"trusted_sources": bad})


# -- Trusted CIDR ranges wider than /8 ---------------------------------------------------------------------

def test_trusted_ranges_wider_than_a_slash_8_are_refused(tmp_path):
    base = cfg.load(tmp_path)
    for prefix in range(0, 8):
        for network in (f"0.0.0.0/{prefix}", f"128.0.0.0/{prefix}"):
            with pytest.raises(ValueError):
                cfg.from_form(base, {"trusted_sources": network})
    accepted = cfg.from_form(base, {"trusted_sources": "10.0.0.0/8\n203.0.113.0/28\nsip.example.com"})
    assert accepted["sip"]["trusted_sources"] == ["10.0.0.0/8", "203.0.113.0/28", "sip.example.com"]
    hand_edited = cfg._merge(cfg.DEFAULTS, {"sip": {"registrar": "127.0.0.1", "username": "line",
                                                    "trusted_sources": ["128.0.0.0/1"]}})
    with pytest.raises(ProviderError) as refused:
        sip_config(hand_edited, "")  # a config.json edited by hand never starts a line trusting half the internet
    assert refused.value.code == "not_configured"
