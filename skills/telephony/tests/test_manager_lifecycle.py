"""The call manager over the life of a call, with a real HTTP/WebSocket stand-in for the Host Service:
hang-ups at every await point, late events of an earlier call, peers that must not leak, limits and
the history rows they write, and what the agent is told."""
import asyncio
import os
import time

import pytest

from conftest import run, until
from hubfake import (ANSWERED, LoopbackHubServer, ScriptedLeg, ScriptedProvider, contexts, history, scripted_manager,
                     sip_line_config)
from phone import config as cfg
from phone import manager as mgr
from phone.daemon import Daemon
from phone.peer import CallPeer, HostClient
from sipfake import FakeNode
from test_manager import FakeHost


ON_HOLD = "put the call on hold"


class HeldLeg(ScriptedLeg):
    held = False

    @property
    def on_hold(self):
        return self.held


class HeldProvider(ScriptedProvider):
    async def dial(self, number, on_event):
        leg = HeldLeg("out", number)
        leg.on_event = on_event
        self.legs.append(leg)
        return leg


# -- General call lifecycle across every await boundary ----------------------------------------------------

@pytest.mark.parametrize("stage", [
    "dial_place_open_call",
    "dial_place_ws_connect",
    "dial_place_ws_hello",
    "dial_place_provider_dial",
    "dial_owner_open_call",
    "dial_owner_ws_connect",
    "dial_owner_ws_hello",
    "dial_owner_provider_dial",
    "dial_owner_answered",
    "incoming_ws_connect",
    "incoming_leg_accept",
])
def test_hangup_at_every_await_point_leaves_nothing_open(tmp_path, monkeypatch, stage):
    created_peers = []
    real_init = CallPeer.__init__

    def track_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        created_peers.append(self)

    monkeypatch.setattr(CallPeer, "__init__", track_init)

    async def scenario():
        hub = await LoopbackHubServer().start()
        dial_gate = hub.add_gate("provider_dial") if stage in ("dial_place_provider_dial", "dial_owner_provider_dial") else None
        accept_gate = hub.add_gate("leg_accept") if stage == "incoming_leg_accept" else None
        if stage in ("dial_place_open_call", "dial_owner_open_call"):
            entered, release = hub.add_gate("open_call")
        elif stage in ("dial_place_ws_connect", "dial_owner_ws_connect", "incoming_ws_connect"):
            entered, release = hub.add_gate("ws_connect")
        elif stage in ("dial_place_ws_hello", "dial_owner_ws_hello"):
            entered, release = hub.add_gate("ws_hello")
        elif dial_gate is not None:
            entered, release = dial_gate
        elif accept_gate is not None:
            entered, release = accept_gate
        else:
            entered, release = None, None

        provider = ScriptedProvider(dial_gate=dial_gate)
        config = cfg._merge(cfg.DEFAULTS, {"outbound": {"allowed_prefixes": ["+1555"]}, "inbound": {"policy": "all"}})
        host = HostClient(hub.url, "tok")
        m = mgr.CallManager(tmp_path, config, "tok", host, provider=provider)
        m.policy.limits.dial_cooldown_seconds = 0

        try:
            if stage.startswith("dial_place_"):
                task = asyncio.ensure_future(m.dial(number="+15550001001", purpose="p", owner_approved=True))
                await asyncio.wait_for(entered.wait(), 2.0)
                hang = asyncio.ensure_future(m.hangup("agent_hangup"))
                await asyncio.sleep(0.03)
                release.set()
                res = await asyncio.wait_for(task, 2.0)
                await asyncio.wait_for(hang, 2.0)
                assert res["ok"] is False and res["code"] == "PHONE_CALL_CANCELLED"
                if stage == "dial_place_ws_connect":
                    assert hub.hellos == []

            elif stage in ("dial_owner_open_call", "dial_owner_ws_connect", "dial_owner_ws_hello",
                           "dial_owner_provider_dial"):  # the line joins the owner's call before it dials
                task = asyncio.ensure_future(
                    m.dial(number="+15550001001", purpose="p", owner_approved=True, live_call_id="live-1")
                )
                await asyncio.wait_for(entered.wait(), 2.0)
                hang = asyncio.ensure_future(m.hangup("agent_hangup"))
                await asyncio.sleep(0.03)
                release.set()
                res = await asyncio.wait_for(task, 2.0)
                await asyncio.wait_for(hang, 2.0)
                assert res["ok"] is False and res["code"] == "PHONE_CALL_CANCELLED"
                if stage == "dial_owner_ws_connect":
                    assert hub.hellos == []

            elif stage == "dial_owner_answered":
                res = await m.dial(number="+15550001001", purpose="p", owner_approved=True, live_call_id="live-1")
                assert res["ok"] is True
                leg = provider.legs[0]
                leg.state = "answered"
                leg.on_event(leg, "answered", {})
                await m.hangup("agent_hangup")

            elif stage.startswith("incoming_"):
                leg = ScriptedLeg("in", "+15550001002", accept_gate=accept_gate)
                provider.legs.append(leg)
                inc = asyncio.ensure_future(m._incoming(leg))
                await asyncio.wait_for(entered.wait(), 2.0)
                if stage == "incoming_ws_connect":
                    leg.end("remote_cancelled", 487)
                else:
                    await m.hangup("agent_hangup")
                await asyncio.sleep(0.03)
                release.set()
                await asyncio.wait_for(inc, 2.0)
                if stage == "incoming_ws_connect":
                    assert hub.hellos == []

            for _ in range(40):
                mgr_tasks = list(getattr(m, "_tasks", ()))
                if (m.active is None and not hub.active_peers and not hub.open_ws_writers
                        and all(t.done() for t in mgr_tasks)):
                    break
                await asyncio.sleep(0.02)

            assert m.active is None
            assert hub.active_peers == set()
            assert hub.open_ws_writers == set()
            assert [t for t in getattr(m, "_tasks", ()) if not t.done()] == []
            assert all(p.closed and p._ws is None and all(t.done() for t in p._tasks) for p in created_peers)
            assert all(l.state == "ended" for l in provider.legs)
        finally:
            await m.stop()
            await hub.stop()

    run(scenario())


# -- Late ended event from previous outgoing call ----------------------------------------------------------

def test_late_ended_from_previous_call_does_not_kill_next_call(tmp_path, monkeypatch):
    async def attach_noop(self):
        return None

    async def close_noop(self, reason="hangup"):
        self._closed = True

    monkeypatch.setattr(CallPeer, "attach", attach_noop)
    monkeypatch.setattr(CallPeer, "close", close_noop)

    class SlowCancelLeg(ScriptedLeg):
        async def hangup(self, reason="local_hangup"):
            self.hangups.append(reason)
            # SIP side has not received 487 yet: do not call self.end() immediately.

    class TwoCallProvider(ScriptedProvider):
        async def dial(self, number, on_event):
            leg = SlowCancelLeg("out", number) if not self.legs else ScriptedLeg("out", number)
            leg.on_event = on_event
            self.legs.append(leg)
            return leg

    monkeypatch.setattr(mgr, "HANGUP_WAIT_SECONDS", 0.05, raising=False)  # the unanswered CANCEL: given up at once

    async def scenario():
        hub = await LoopbackHubServer().start()
        try:
            provider = TwoCallProvider()
            config = cfg._merge(cfg.DEFAULTS, {"outbound": {"allowed_prefixes": ["+1555"]}})
            m = mgr.CallManager(tmp_path, config, "tok", HostClient(hub.url, "tok"), provider=provider)
            m.policy.limits.dial_cooldown_seconds = 0

            first = await m.dial(number="+15550001001", purpose="first", owner_approved=True)
            assert first["ok"] is True
            leg_a = provider.legs[0]
            await m.hangup("agent_hangup")
            assert m.active is None

            entered, release = hub.add_gate("open_call")
            second_task = asyncio.ensure_future(m.dial(number="+15550001002", purpose="second", owner_approved=True))
            await asyncio.wait_for(entered.wait(), 2.0)
            # Call A's SIP transaction finally times out while Call B is in admission
            leg_a.end("cancelled", 487)
            await asyncio.sleep(0.03)
            release.set()
            second = await asyncio.wait_for(second_task, 2.0)
            assert second["ok"] is True
            assert m.active is not None and m.active.leg is provider.legs[1] and m.active.leg is not leg_a
            await m.stop()
        finally:
            await hub.stop()

    run(scenario())


# -- Hangup during provider.dial, _stopping guard, Daemon._maybe_reload lock -------------------------------

def test_hangup_during_provider_dial_and_stopping_guard_and_reload_lock(tmp_path):
    async def scenario():
        hub = await LoopbackHubServer().start()
        try:
            entered, release = hub.add_gate("provider_dial")
            provider = ScriptedProvider(dial_gate=(entered, release))
            config = cfg._merge(cfg.DEFAULTS, {"outbound": {"allowed_prefixes": ["+1555"]}})
            m = mgr.CallManager(tmp_path, config, "tok", HostClient(hub.url, "tok"), provider=provider)
            m.policy.limits.dial_cooldown_seconds = 0

            # 1. hangup while provider.dial is awaiting
            dial_task = asyncio.ensure_future(
                m.dial(number="+15550001001", purpose="p", owner_approved=True, live_call_id="live-1")
            )
            await asyncio.wait_for(entered.wait(), 2.0)
            await m.hangup("agent_hangup")
            release.set()
            res = await asyncio.wait_for(dial_task, 2.0)
            assert res["ok"] is False and res["code"] == "PHONE_CALL_CANCELLED"
            assert provider.legs[0].hangups == ["agent_hangup"] and m.active is None

            # 2. dial refuses while _stopping is True
            m._stopping = True
            stopped_res = await m.dial(number="+15550001002", purpose="p", owner_approved=True, live_call_id="live-1")
            assert stopped_res["ok"] is False and stopped_res["code"] == "PHONE_LINE_NOT_READY"
            await m.stop()

            # 3. Daemon._maybe_reload waits on manager._lock before stopping old manager
            daemon = Daemon(tmp_path, None, "")
            cfg.save(tmp_path, cfg.DEFAULTS)
            await daemon._start_manager(cfg.DEFAULTS)
            old_mgr = daemon.manager
            cfg.save(tmp_path, cfg._merge(cfg.DEFAULTS, {"sip": {"display_name": "Reloaded"}}))

            order = []
            async with old_mgr._lock:
                reload_task = asyncio.ensure_future(daemon._maybe_reload())
                await asyncio.sleep(0.05)
                assert not old_mgr._stopping  # blocked on _lock while an operation holds it
                order.append("released_lock")
            await asyncio.wait_for(reload_task, 2.0)
            assert old_mgr._stopping and daemon.manager is not old_mgr and order == ["released_lock"]
            await daemon.manager.stop()
        finally:
            await hub.stop()

    run(scenario())


# -- No leaked CallPeer on place admission hangup or owner_call _answered races ----------------------------

def test_no_peer_leak_on_place_admission_hangup_or_owner_call_answered_race(tmp_path, monkeypatch):
    # Part 1: place admission hangup does not attach or leak a peer
    attached = []

    async def spy_attach(self):
        attached.append(self)

    async def spy_close(self, reason="hangup"):
        self._closed = True

    with monkeypatch.context() as mp:
        mp.setattr(CallPeer, "attach", spy_attach)
        mp.setattr(CallPeer, "close", spy_close)

        async def part1():
            hub = await LoopbackHubServer().start()
            entered, release = hub.add_gate("open_call")
            try:
                config = cfg._merge(cfg.DEFAULTS, {"outbound": {"allowed_prefixes": ["+1555"]}})
                m = mgr.CallManager(tmp_path / "p1", config, "tok", HostClient(hub.url, "tok"),
                                    provider=ScriptedProvider())
                t = asyncio.ensure_future(m.dial(number="+15550001001", purpose="p", owner_approved=True))
                await asyncio.wait_for(entered.wait(), 2.0)
                await m.hangup("agent_hangup")
                release.set()
                res = await asyncio.wait_for(t, 2.0)
                assert res["code"] == "PHONE_CALL_CANCELLED"
                assert attached == [] and m.active is None
                await m.stop()
            finally:
                await hub.stop()

        run(part1())

    # Part 2: owner_call: an unexpected ValueError while the line joins the owner's call (before it dials) ends the
    # call with nothing dialled and frees the line
    with monkeypatch.context() as mp:
        async def boom_attach(self):
            raise ValueError("unexpected codec state")

        mp.setattr(CallPeer, "attach", boom_attach)

        async def part2():
            hub = await LoopbackHubServer().start()
            try:
                provider = ScriptedProvider()
                config = cfg._merge(cfg.DEFAULTS, {"outbound": {"allowed_prefixes": ["+1555"]}})
                m = mgr.CallManager(tmp_path / "p2", config, "tok", HostClient(hub.url, "tok"), provider=provider)
                res = await m.dial(number="+15550001001", purpose="p", owner_approved=True, live_call_id="live-1")
                assert res["ok"] is False and res["code"] == "PHONE_DIAL_FAILED"
                assert provider.legs == [] and m.active is None and m.last.end_reason == "dial_failed"
                await m.stop()
            finally:
                await hub.stop()

        run(part2())


# -- A limit ends a call that has a peer: exactly one history row ------------------------------------------

@pytest.mark.parametrize("limit", ["time_limit", "media_timeout", "no_answer_timeout:out", "no_answer_timeout:in"])
def test_a_limit_on_a_call_with_a_peer_writes_exactly_one_history_row(tmp_path, monkeypatch, limit):
    reason, _, direction = limit.partition(":")
    monkeypatch.setattr(mgr, "MEDIA_TIMEOUT_SECONDS", 0.4 if reason == "media_timeout" else 60)
    notices = {"time_limit": "[Phone line notice] The line hung up at its time limit.",
               "media_timeout": "[Phone line notice] No sound from the line for a minute: it hung up."}

    async def scenario():
        hub = await LoopbackHubServer().start()
        provider = ScriptedProvider()
        config = cfg._merge(cfg.DEFAULTS, {"outbound": {"allowed_prefixes": ["+1555"]}, "inbound": {"policy": "all"}})
        m = mgr.CallManager(tmp_path, config, "tok", HostClient(hub.url, "tok"), provider=provider)
        limits = m.policy.limits
        limits.dial_cooldown_seconds = 0
        limits.ring_timeout_seconds = 0.4 if reason == "no_answer_timeout" else 30
        limits.max_call_seconds = 0.6 if reason == "time_limit" else 600
        gate = (asyncio.Event(), asyncio.Event())
        incoming = None
        try:
            if direction == "in":
                leg = ScriptedLeg("in", "+15550001002", accept_gate=gate)
                incoming = asyncio.ensure_future(m._incoming(leg))
                await asyncio.wait_for(gate[0].wait(), 3)  # the peer has joined the call; the line still rings
            else:
                result = await m.dial(number="+15550001001", purpose="p", owner_approved=True)
                assert result["ok"] is True
                leg = provider.legs[0]
                if reason != "no_answer_timeout":
                    leg.state = "answered"
                    leg.on_event(leg, "answered", {})
            call = m.active
            assert call is not None and call.peer is not None
            for _ in range(200):
                if m.active is None and history(tmp_path) and not hub.active_peers:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.5)  # time for a second finish to write a second row, if one ever did
            rows = history(tmp_path)
            assert [row["end_reason"] for row in rows] == [reason], rows
            assert m.active is None and call.peer.closed and hub.active_peers == set()
            assert leg.hangups == [reason]
            if reason in notices:
                assert any(c.get("text") == notices[reason] for c in hub.contexts), hub.contexts
        finally:
            gate[1].set()
            if incoming is not None:
                await asyncio.wait_for(incoming, 3)
            await m.stop()
            await hub.stop()
        assert len(history(tmp_path)) == 1

    run(scenario())


# -- No wait for Timer B -----------------------------------------------------------------------------------

def test_the_ring_limit_ends_a_call_nobody_answers_long_before_timer_b(tmp_path, monkeypatch):
    async def attach(self):  # FakeHost has no call socket: the line joins the owner's call before it dials
        return None

    async def close(self, reason="hangup"):
        self._closed = True

    monkeypatch.setattr(CallPeer, "attach", attach)
    monkeypatch.setattr(CallPeer, "close", close)

    async def scenario():
        node = await FakeNode.start()
        m = mgr.CallManager(tmp_path, sip_line_config(node), "", FakeHost())
        await m.start()
        facts = {}
        try:
            m.policy.limits.dial_cooldown_seconds = 0
            m.policy.limits.ring_timeout_seconds = 0.5
            result = await m.dial(number="+15550001001", purpose="p", owner_approved=True, live_call_id="live-1")
            assert result["ok"] is True, result
            invite, addr = await node.expect("INVITE")  # and nothing ever answers it
            started = time.monotonic()
            facts["ended"] = await until(lambda: m.active is None, 4)
            facts["seconds"] = time.monotonic() - started
            await asyncio.sleep(0.1)
            facts["rows"] = [(row["end_reason"], row["outcome"]) for row in history(tmp_path)]
            node.reply(invite, addr, 180, to_tag="late")  # a late provisional still gets its CANCEL
            cancel, caddr = await node.expect("CANCEL")
            node.reply(cancel, caddr, 200)
            node.reply(invite, addr, 487, to_tag="late")
            await node.expect("ACK")
        finally:
            await m.stop()
            node.close()
        return facts

    facts = run(scenario())
    assert facts["ended"] and facts["seconds"] < 2.5, facts
    assert facts["rows"] == [("no_answer_timeout", "no_answer")]


# -- A hold that comes with the answer is news -------------------------------------------------------------

@pytest.mark.parametrize("mode", ["owner_call", "place"])
def test_the_agent_hears_of_a_hold_that_came_with_the_answer_after_the_answer(tmp_path, mode):
    async def scenario():
        hub = await LoopbackHubServer().start()
        provider = HeldProvider()
        m, _ = scripted_manager(tmp_path, hub, provider)
        try:
            result = await m.dial(number="+15550001001", purpose="p", owner_approved=True,
                                  live_call_id="live-1" if mode == "owner_call" else "")
            assert result["ok"] is True, result
            leg = provider.legs[0]
            leg.state, leg.held = "answered", True
            leg.on_event(leg, "answered", {})
            leg.on_event(leg, "hold", {"on_hold": True})  # the line reports the answer's own hold right after it
            heard = await until(lambda: contexts(hub, ON_HOLD), 3)
            await asyncio.sleep(0.1)
            return heard, [c["text"] for c in hub.contexts]
        finally:
            await m.stop()
            await hub.stop()

    heard, texts = run(scenario())
    assert heard, texts
    answered_at = next(i for i, text in enumerate(texts) if ANSWERED in text)
    held_at = next(i for i, text in enumerate(texts) if ON_HOLD in text)
    assert answered_at < held_at and sum(ON_HOLD in text for text in texts) == 1, texts


def test_call_history_is_owner_only_from_its_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    old = os.umask(0o022)
    try:
        m = mgr.CallManager(tmp_path, cfg.DEFAULTS, "", None)
        m._append_history({"id": "x"})
    finally:
        os.umask(old)
    assert (tmp_path / "calls.jsonl").stat().st_mode & 0o777 == 0o600
