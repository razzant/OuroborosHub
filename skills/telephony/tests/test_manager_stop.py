"""Stopping and reloading the line while a call is being closed: the end notice, a stuck cleanup, a busy
line, a dial cut short by the stop, and the deadline the manager hands its provider."""
import asyncio
import logging
import time
import types

import pytest

from conftest import import_plugin, run, until
from hubfake import (LoopbackHubServer, ScriptedLeg, ScriptedProvider, answered_place_call, contexts, history,
                     scripted_manager)
from phone import config as cfg
from phone import manager as mgr
from phone import sip_ua
from phone.daemon import Daemon
from sipfake import FakeNode
from test_daemon import SlowByeProvider
from test_manager import FakeHost, FakeProvider
from test_manager_lifecycle import HeldProvider


HUNG_UP = "The other party hung up"


# -- A call keeps the line until its cleanup is over -------------------------------------------------------

@pytest.mark.parametrize("by", ["stop", "reload"])
def test_stop_or_reload_while_the_end_notice_goes_out_leaves_no_peer_joined(tmp_path, monkeypatch, by):
    monkeypatch.setattr(mgr, "NOTICE_SECONDS", 1.0, raising=False)  # a wide window (0.2 s in use)

    async def scenario():
        hub = await LoopbackHubServer().start()
        provider = ScriptedProvider()
        m, config = scripted_manager(tmp_path, hub, provider)
        cfg.save(tmp_path, config)
        daemon = Daemon(tmp_path, m.host, "")
        daemon.manager, daemon._fingerprint = m, cfg.fingerprint(config)
        facts = {}
        try:
            _, leg = await answered_place_call(m, provider, hub)
            leg.end("remote_hangup")
            assert await until(lambda: contexts(hub, HUNG_UP))
            facts["joined_after_notice"] = bool(hub.active_peers)  # the notice is out, the bye not yet
            if by == "stop":
                await m.stop()
            else:
                cfg.save(tmp_path, cfg._merge(config, {"sip": {"display_name": "Reloaded"}}))
                await daemon._maybe_reload()
                facts["deferred"] = daemon.manager is m and not m._stopping
            facts["left"] = await until(lambda: not hub.active_peers and not hub.open_ws_writers, 3)
            # A closed peer precedes history persistence and the end of the manager's cleanup.
            assert await until(lambda: not m.busy, 3)
            if by == "reload":
                await daemon._maybe_reload()  # the call is closed now: the new settings apply
                facts["applied"] = daemon.manager is not m
            facts["rows"] = [row["end_reason"] for row in history(tmp_path)]
        finally:
            for manager in {m, daemon.manager}:
                await manager.stop()
            await hub.stop()
        return facts

    facts = run(scenario())
    assert facts["joined_after_notice"] and facts["left"], facts
    assert facts["rows"] == ["remote_hangup"]
    if by == "reload":
        assert facts["deferred"] and facts["applied"], facts


def test_while_the_last_call_is_being_closed_the_line_is_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr, "NOTICE_SECONDS", 1.0, raising=False)
    plugin = import_plugin()

    async def scenario():
        hub = await LoopbackHubServer().start()
        provider = ScriptedProvider()
        m, _ = scripted_manager(tmp_path, hub, provider)
        facts = {}
        try:
            _, leg = await answered_place_call(m, provider, hub)
            leg.end("remote_hangup")
            assert await until(lambda: contexts(hub, HUNG_UP))
            facts["active"] = m.active
            facts["status"] = status = m.status()
            refreshes = []
            api = types.SimpleNamespace(refresh_companion_process=lambda name: refreshes.append(name) or "restarted")
            facts["password"] = plugin.apply_saved_password(api, ask=lambda op, args, timeout: status)
            facts["refreshes"] = refreshes
            facts["dial"] = await m.dial(number="+15550001002", purpose="p2", owner_approved=True)
            incoming = ScriptedLeg("in", "+15550001003")
            await m._incoming(incoming)
            facts["incoming_rejected"] = list(incoming.rejected)
            facts["closed"] = await until(lambda: not hub.active_peers, 3)
            facts["free"] = await until(lambda: not m.status().get("line_busy"), 3)
            facts["after"] = await m.dial(number="+15550001004", purpose="p3", owner_approved=True)
            facts["rows"] = [(row.get("outcome"), row.get("reason")) for row in history(tmp_path)]
        finally:
            await m.stop()
            await hub.stop()
        return facts

    facts = run(scenario())
    assert facts["active"] is None
    assert facts["dial"]["ok"] is False and facts["dial"]["code"] == "PHONE_LINE_BUSY", facts["dial"]
    assert facts["incoming_rejected"] == [486]
    assert facts["status"]["active_call"] is None and facts["status"].get("line_busy") is True
    assert facts["password"] == "deferred_call_active" and facts["refreshes"] == []
    assert facts["closed"] and facts["free"], facts  # once the call is closed the line takes calls again
    assert facts["after"]["ok"] is True, facts["after"]
    assert ("refused", "line_busy") in facts["rows"]


def test_stop_cuts_a_stuck_cleanup_short_and_still_closes_its_peer(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr, "STOP_WAIT_SECONDS", 0.3, raising=False)

    async def scenario():
        hub = await LoopbackHubServer().start()
        provider = ScriptedProvider()
        m, _ = scripted_manager(tmp_path, hub, provider)
        facts = {}
        try:
            call, leg = await answered_place_call(m, provider, hub)
            leg.hangup_delay = 30  # its BYE is never answered
            m._spawn(m._finish(call, "internal_error"), call=call, label="failure")  # a task failure ends the call
            await asyncio.sleep(0.2)
            facts["joined_while_cleaning"] = bool(hub.active_peers)  # the cleanup still waits for the leg
            started = time.monotonic()
            await asyncio.wait_for(m.stop(), 6)
            facts["stop_s"] = time.monotonic() - started
            facts["left"] = await until(lambda: not hub.active_peers and not hub.open_ws_writers, 2)
            facts["rows"] = [row["end_reason"] for row in history(tmp_path)]
        finally:
            await hub.stop()
        return facts

    facts = run(scenario())
    assert facts["joined_while_cleaning"] and facts["left"], facts
    assert facts["stop_s"] < 2.0, facts
    assert facts["rows"] == ["internal_error"]


# -- The line stopping during a dial is a cancelled call ---------------------------------------------------

def test_the_line_stopping_mid_dial_is_a_clean_cancel(tmp_path, monkeypatch, caplog):
    async def attach(self):  # FakeHost has no call socket: the line joins the owner's call before it dials
        return None

    async def close(self, reason="hangup"):
        self._closed = True

    monkeypatch.setattr(mgr.CallPeer, "attach", attach)
    monkeypatch.setattr(mgr.CallPeer, "close", close)
    gate = {}
    real_open = sip_ua.SipCall._open_rtp

    async def gated_open(self):
        gate["entered"].set()
        await gate["release"].wait()
        await real_open(self)

    monkeypatch.setattr(sip_ua.SipCall, "_open_rtp", gated_open)

    async def scenario():
        gate["entered"], gate["release"] = asyncio.Event(), asyncio.Event()
        node = await FakeNode.start()
        config = cfg._merge(cfg.DEFAULTS, {
            "sip": {"registrar": "127.0.0.1", "port": node.port, "username": "line", "register": False,
                    "local_host": "127.0.0.1"},
            "outbound": {"allowed_prefixes": ["+1555"]}})
        m = mgr.CallManager(tmp_path, config, "", FakeHost())
        await m.start()
        try:
            dial = asyncio.ensure_future(m.dial(number="+15550001001", purpose="p", owner_approved=True,
                                                live_call_id="live-1"))
            await asyncio.wait_for(gate["entered"].wait(), 3)
            stop = asyncio.ensure_future(m.stop())
            for _ in range(200):
                ua = m.provider.ua  # a stopped line has closed its socket and let it go
                if ua._stopping and (ua._udp is None or ua._udp.transport.is_closing()):
                    break
                await asyncio.sleep(0.01)
            gate["release"].set()
            result = await asyncio.wait_for(dial, 3)
            await asyncio.wait_for(stop, 5)
            return result, m
        finally:
            gate["release"].set()
            node.close()

    with caplog.at_level(logging.INFO, logger="telephony"):
        result, m = run(scenario())
    assert result["ok"] is False and result["code"] == "PHONE_CALL_CANCELLED", result
    assert m.active is None and m.last.end_reason == "line_stopped"
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


# -- Provider stubs take the stop deadline (the manager only logs a failing stop, so a TypeError would go unseen) ----

@pytest.mark.parametrize("stub", [FakeProvider, ScriptedProvider, HeldProvider, SlowByeProvider],
                         ids=["FakeProvider", "ScriptedProvider", "HeldProvider", "SlowByeProvider"])
def test_the_manager_stops_a_provider_stub_with_the_deadline_it_was_given(tmp_path, monkeypatch, caplog, stub):
    entered = []
    real_stop = stub.stop

    async def watching_stop(self, deadline=None):
        await real_stop(self, deadline)  # a stub that cannot take the deadline raises TypeError right here
        entered.append(deadline)

    monkeypatch.setattr(stub, "stop", watching_stop)

    async def scenario():
        config = cfg._merge(cfg.DEFAULTS, {"outbound": {"allowed_prefixes": ["+1555"]}})
        m = mgr.CallManager(tmp_path, config, "tok", FakeHost(), provider=stub())
        deadline = time.monotonic() + 2.0
        await m.stop(deadline)
        return deadline

    with caplog.at_level(logging.WARNING, logger="telephony"):
        deadline = run(scenario())
    assert entered == [deadline]  # the stub's stop() ran to its end, with the deadline the manager was given
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("phone line stop")] == []
