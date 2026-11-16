"""The companion's daemon: reloads that take turns and wait for a dial, and a stop on SIGTERM that keeps
one deadline."""
import asyncio
import logging
import time

import pytest

from conftest import run, until
from hubfake import LoopbackHubServer, ScriptedProvider, answered_place_call, history
from phone import config as cfg
from phone import control
from phone import daemon as daemon_mod
from phone import manager as mgr
from phone.daemon import Daemon
from phone.peer import HostClient
from sipfake import FakeNode, SDP, contact


class SlowByeProvider(ScriptedProvider):
    """Legs whose hangup is never confirmed: a BYE nobody answers."""

    async def dial(self, number, on_event):
        leg = await super().dial(number, on_event)
        leg.hangup_delay = 60
        return leg


def line_config(node: FakeNode, *, register: bool, **sip):
    return cfg._merge(cfg.DEFAULTS, {
        "sip": {"registrar": "127.0.0.1", "port": node.port, "username": "line", "register": register,
                "local_host": "127.0.0.1", **sip},
        "outbound": {"allowed_prefixes": ["+1555"]}})


def unregisters(node: FakeNode) -> int:
    return len({m.top_via.branch for m, _ in node.received
                if m.is_request and m.method == "REGISTER" and m.get("Expires") == "0"})


async def registrar(node: FakeNode, *, alive=lambda: True, unregister_delay: float = 0.0):
    """Answers each REGISTER while ``alive()``; an unregistration (Expires: 0) after ``unregister_delay``."""
    seen = 0
    loop = asyncio.get_running_loop()
    while True:
        requests = [(m, a) for m, a in node.received if m.is_request and m.method == "REGISTER"]
        for message, addr in requests[seen:]:
            if not alive():
                continue
            if message.get("Expires") == "0":
                loop.call_later(unregister_delay, node.reply, message, addr, 200)
            else:
                node.reply(message, addr, 200, extra=[("Contact", message.get("Contact") + ";expires=300")])
        seen = len(requests)
        await asyncio.sleep(0.01)


async def answered_call(m, node: FakeNode, nth: int):
    dial = asyncio.ensure_future(m.dial(number="+15550001001", purpose="p", owner_approved=True))
    invite, addr = await node.expect("INVITE", 5, nth=nth)
    node.reply(invite, addr, 200, to_tag=f"answer{nth}", extra=contact(node), body=SDP)
    await node.expect("ACK", nth=nth)
    result = await dial
    assert result["ok"] is True, result
    assert await until(lambda: m.active is not None and m.active.state == "answered", 3)
    return m.active


def test_reload_waits_for_a_dial_and_never_restarts_a_line_with_a_call(tmp_path):
    async def scenario():
        hub = await LoopbackHubServer().start()
        try:
            gate = (asyncio.Event(), asyncio.Event())
            provider = ScriptedProvider(dial_gate=gate)
            config = cfg._merge(cfg.DEFAULTS, {"outbound": {"allowed_prefixes": ["+1555"]}})
            cfg.save(tmp_path, config)
            daemon = Daemon(tmp_path, HostClient(hub.url, "tok"), "")
            m = mgr.CallManager(tmp_path, config, "tok", daemon.host, provider=provider)
            m.policy.limits.dial_cooldown_seconds = 0
            daemon.manager, daemon._fingerprint = m, cfg.fingerprint(config)
            dial = asyncio.ensure_future(m.dial(number="+15550001001", purpose="p", owner_approved=True,
                                                live_call_id="live-1"))
            await asyncio.wait_for(gate[0].wait(), 2)
            cfg.save(tmp_path, cfg._merge(config, {"sip": {"display_name": "Reloaded"}}))
            reload = asyncio.ensure_future(daemon._maybe_reload())
            await asyncio.sleep(0.1)
            assert not reload.done() and not m._stopping  # it waits for the dial's lock
            gate[1].set()
            assert (await asyncio.wait_for(dial, 2))["ok"] is True
            await asyncio.wait_for(reload, 2)
            # the reload saw the call under the lock and left the line alone
            assert daemon.manager is m and not m._stopping and m.active is not None
            assert provider.legs[0].hangups == [] and provider.legs[0].state != "ended"
            await m.hangup("agent_hangup")
            await daemon._maybe_reload()  # between calls the new settings apply
            assert daemon.manager is not m and m._stopping
            await daemon.manager.stop()
        finally:
            await hub.stop()

    run(scenario())


# -- Reloads take turns ------------------------------------------------------------------------------------

@pytest.mark.parametrize("second", ["forced", "periodic"])
def test_two_reloads_at_once_leave_exactly_one_line_running(tmp_path, monkeypatch, second):
    started = []
    real_start = Daemon._start_manager

    async def tracking_start(self, config):
        await real_start(self, config)
        started.append(self.manager)

    monkeypatch.setattr(Daemon, "_start_manager", tracking_start)

    def socket_open(manager):
        ua = getattr(manager.provider, "ua", None)
        return ua is not None and ua._udp is not None and not ua._udp.transport.is_closing()

    async def scenario():
        node = await FakeNode.start()
        config = line_config(node, register=False)
        cfg.save(tmp_path, config)
        daemon = Daemon(tmp_path, None, "")
        try:
            await daemon._start_manager(config)
            cfg.save(tmp_path, cfg._merge(config, {"sip": {"display_name": "Reloaded"}}))
            # a control request's reload and, at the same moment, another one (a second request, or the periodic check)
            await asyncio.wait_for(asyncio.gather(daemon._maybe_reload(force=True),
                                                  daemon._maybe_reload(force=second == "forced")), 10)
            live = [manager for manager in started if socket_open(manager)]
            return live == [daemon.manager], len(started), len(live)
        finally:
            for manager in started:
                await asyncio.wait_for(manager.stop(), 10)
            node.close()

    only_current_live, made, live = run(scenario())
    assert only_current_live, (made, live)
    assert made == (3 if second == "forced" else 2), made


# -- SIGTERM stops the line first --------------------------------------------------------------------------

@pytest.mark.parametrize("during", ["dial", "hangup"])
def test_sigterm_during_a_dial_or_a_hangup_stops_the_line_at_once(tmp_path, monkeypatch, during):
    providers = []

    def scripted_provider(config, secret, on_incoming, on_status):
        providers.append(SlowByeProvider())
        return providers[-1]

    monkeypatch.setattr(mgr, "create_provider", scripted_provider)

    async def scenario():
        hub = await LoopbackHubServer().start()
        cfg.save(tmp_path, cfg._merge(cfg.DEFAULTS, {"outbound": {"allowed_prefixes": ["+1555"]}}))
        daemon = Daemon(tmp_path, HostClient(hub.url, "tok"), "")
        runner = asyncio.ensure_future(daemon.run())
        request = None
        facts = {}
        try:
            assert await until(lambda: (tmp_path / control.CONTROL_FILE).exists(), 3)
            m = daemon.manager
            m.policy.limits.dial_cooldown_seconds = 0
            if during == "hangup":
                _, leg = await answered_place_call(m, providers[0], hub)
                request = asyncio.ensure_future(asyncio.to_thread(
                    control.request, tmp_path, "hangup", {"reason": "agent_hangup"}, timeout_sec=8))
                assert await until(lambda: leg.hangups, 3)  # the BYE went out and waits for an answer
            else:
                entered, _ = hub.add_gate("open_call")  # admission hangs: the dial waits on the Host Service
                request = asyncio.ensure_future(asyncio.to_thread(
                    control.request, tmp_path, "dial", {"number": "+15550001001", "purpose": "p",
                                                        "owner_approved": True}, timeout_sec=8))
                await asyncio.wait_for(entered.wait(), 3)
            started = time.monotonic()
            daemon._stop.set()  # what the SIGTERM handler does
            facts["line_stopped"] = await until(lambda: m._stopping and history(tmp_path) and not hub.active_peers, 4)
            facts["line_stop_s"] = time.monotonic() - started
            try:
                await asyncio.wait_for(asyncio.shield(runner), 4)
                facts["run_s"] = time.monotonic() - started
            except asyncio.TimeoutError:
                facts["run_s"] = None
            facts["rows"] = [row["end_reason"] for row in history(tmp_path)]
        finally:
            await hub.stop()  # releases the admission gate
            if not runner.done():
                runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
            if request is not None:
                facts["reply"] = (await asyncio.gather(request, return_exceptions=True))[0]
        return facts

    facts = run(scenario())
    assert facts["line_stopped"] and facts["line_stop_s"] < 2.5, facts
    assert facts["run_s"] is not None and facts["run_s"] < 3.5, facts
    assert facts["rows"] == (["line_stopped"] if during == "dial" else ["agent_hangup"]), facts
    reply = facts["reply"]
    # A dial cut short may also answer for itself, in time: the stop's timing above is what this test is about.
    answers = ("PHONE_LINE_STOPPING", "PHONE_CALL_CANCELLED") if during == "dial" else ("PHONE_LINE_STOPPING",)
    assert isinstance(reply, dict) and (reply.get("code") in answers or reply.get("ok") is True), reply


# -- One deadline for the whole stop -----------------------------------------------------------------------

def test_sigterm_with_two_unanswered_byes_and_a_silent_registrar_stops_within_the_deadline(tmp_path, monkeypatch):
    # The first call's BYE runs on unanswered after phone_hangup gave up on it (sooner here than its 5 s), the second
    # call is up when SIGTERM comes, and from then on neither the far end nor the registrar answers anything.
    monkeypatch.setattr(mgr, "HANGUP_WAIT_SECONDS", 0.3, raising=False)

    async def scenario():
        node = await FakeNode.start()
        hub = await LoopbackHubServer().start()
        alive = {"on": True}
        answering = asyncio.ensure_future(registrar(node, alive=lambda: alive["on"]))
        cfg.save(tmp_path, line_config(node, register=True))
        daemon = Daemon(tmp_path, HostClient(hub.url, "tok"), "pw")
        runner = asyncio.ensure_future(daemon.run())
        facts = {}
        try:
            assert await until(lambda: (tmp_path / control.CONTROL_FILE).exists(), 5)
            m = daemon.manager
            assert await until(lambda: m.provider is not None
                               and m.provider.status().get("registration") == "registered", 5)
            m.policy.limits.dial_cooldown_seconds = 0
            alive["on"] = False
            await answered_call(m, node, 1)
            assert (await m.hangup("agent_hangup"))["ok"] is True  # its BYE goes on, unanswered, inside the line
            assert await until(lambda: not m.busy, 3)
            await answered_call(m, node, 2)
            assert await until(lambda: hub.active_peers, 3)
            started = time.monotonic()
            daemon._stop.set()  # what the SIGTERM handler does
            marks = {}
            while not runner.done() and time.monotonic() - started < 12:
                now = time.monotonic() - started
                if "history_s" not in marks and len(history(tmp_path)) == 2:
                    marks["history_s"] = now
                if "peer_left_s" not in marks and not hub.active_peers:
                    marks["peer_left_s"] = now
                await asyncio.sleep(0.01)
            facts.update(marks)
            facts["run_s"] = time.monotonic() - started if runner.done() else None
            facts["rows"] = [row["end_reason"] for row in history(tmp_path)]
            facts["byes"] = node.count("BYE")
            facts["unregisters"] = unregisters(node)
            facts["registration"] = m.provider.ua.registration["state"]
            facts["socket"] = m.provider.ua._udp
            facts["control_file"] = (tmp_path / control.CONTROL_FILE).exists()
        finally:
            answering.cancel()
            if not runner.done():
                runner.cancel()
            await asyncio.gather(runner, answering, return_exceptions=True)
            await hub.stop()
            node.close()
        return facts

    facts = run(scenario())
    # STOP_SECONDS (3.5 s) plus the control channel's close and some scheduling slack: under the 5 s after which
    # the supervisor kills the companion
    assert facts["run_s"] is not None and facts["run_s"] < 4.5, facts
    assert facts["rows"] == ["agent_hangup", "line_stopped"], facts
    # the record and the call peer never wait for the BYE nobody answers
    assert facts["history_s"] < 1.0 and facts["peer_left_s"] < 1.0, facts
    assert facts["byes"] == 2 and facts["unregisters"] == 1, facts
    assert facts["registration"] == "stopped" and facts["socket"] is None and not facts["control_file"], facts


@pytest.mark.parametrize("reload_by", ["periodic_check", "control_request"])
def test_a_stop_after_a_reload_cut_short_by_sigterm_is_that_same_stop(tmp_path, monkeypatch, caplog, reload_by):
    # SIGTERM while a settings reload stops the old line (its unregistration is answered after 0.5 s): the stop at
    # the end of Daemon.run is that same stop, and once a line is stopped another stop returns at once without
    # touching its socket again (no second unregistration lost on a socket that closes, none sent on a closed one).
    monkeypatch.setattr(daemon_mod, "RELOAD_CHECK_SECONDS", 0.1 if reload_by == "periodic_check" else 60.0)

    async def scenario():
        node = await FakeNode.start()
        answering = asyncio.ensure_future(registrar(node, unregister_delay=0.5))
        config = line_config(node, register=True)
        cfg.save(tmp_path, config)
        daemon = Daemon(tmp_path, None, "pw")
        runner = asyncio.ensure_future(daemon.run())
        facts = {}
        request = None
        try:
            assert await until(lambda: (tmp_path / control.CONTROL_FILE).exists(), 5)
            m = daemon.manager
            assert await until(lambda: m.provider is not None
                               and m.provider.status().get("registration") == "registered", 5)
            if reload_by == "periodic_check":
                cfg.save(tmp_path, cfg._merge(config, {"sip": {"display_name": "Reloaded"}}))
            else:
                request = asyncio.ensure_future(asyncio.to_thread(control.request, tmp_path, "reload", {},
                                                                  timeout_sec=8))
            assert await until(lambda: unregisters(node) == 1, 3)  # the reload is stopping the old line
            started = time.monotonic()
            daemon._stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(runner), 6)
                facts["run_s"] = time.monotonic() - started
            except asyncio.TimeoutError:
                facts["run_s"] = None
            facts["same_line"] = daemon.manager is m
            again = time.monotonic()
            try:
                await asyncio.wait_for(m.stop(), 6)
                await asyncio.wait_for(m.provider.ua.stop(), 6)
                facts["again"] = "returned"
            except Exception as exc:  # the line's own failure, kept as a fact
                facts["again"] = f"{type(exc).__name__}: {exc}"
            facts["again_s"] = time.monotonic() - again
            facts["unregisters"] = unregisters(node)
            facts["registration"] = m.provider.ua.registration["state"]
            facts["socket"] = m.provider.ua._udp
        finally:
            answering.cancel()
            if not runner.done():
                runner.cancel()
            await asyncio.gather(runner, answering, return_exceptions=True)
            if request is not None:
                await asyncio.gather(request, return_exceptions=True)
            node.close()
        facts["stop_failures"] = [record.getMessage() for record in caplog.records
                                  if record.getMessage().startswith("phone line stop")]
        return facts

    with caplog.at_level(logging.WARNING, logger="telephony"):
        facts = run(scenario())
    assert facts["run_s"] is not None and facts["run_s"] < 1.5, facts
    assert facts["same_line"] and facts["again"] == "returned" and facts["again_s"] < 0.1, facts
    assert facts["unregisters"] == 1 and facts["registration"] == "stopped" and facts["socket"] is None, facts
    assert facts["stop_failures"] == [], facts


def test_sigterm_while_a_settings_check_waits_for_a_dial_stops_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "RELOAD_CHECK_SECONDS", 0.1)
    gate = {}
    providers = []

    def gated_provider(config, secret, on_incoming, on_status):  # its dial waits on the gate, under the line's lock
        providers.append(ScriptedProvider(dial_gate=(gate["entered"], gate["release"])))
        return providers[-1]

    monkeypatch.setattr(mgr, "create_provider", gated_provider)

    async def scenario():
        gate["entered"], gate["release"] = asyncio.Event(), asyncio.Event()
        hub = await LoopbackHubServer().start()
        config = cfg._merge(cfg.DEFAULTS, {"outbound": {"allowed_prefixes": ["+1555"]}})
        cfg.save(tmp_path, config)
        daemon = Daemon(tmp_path, HostClient(hub.url, "tok"), "")
        runner = asyncio.ensure_future(daemon.run())
        facts = {}
        dial = None
        try:
            assert await until(lambda: (tmp_path / control.CONTROL_FILE).exists(), 3)
            m = daemon.manager
            m.policy.limits.dial_cooldown_seconds = 0
            dial = asyncio.ensure_future(m.dial(number="+15550001001", purpose="p", owner_approved=True))
            await asyncio.wait_for(gate["entered"].wait(), 3)  # the dial holds the line's lock
            cfg.save(tmp_path, cfg._merge(config, {"sip": {"display_name": "Reloaded"}}))
            await asyncio.sleep(0.5)  # several settings checks: one of them now waits for the dial to finish
            assert m._lock.locked() and not m._stopping
            started = time.monotonic()
            daemon._stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(runner), 4.5)
                facts["run_s"] = time.monotonic() - started
            except asyncio.TimeoutError:
                facts["run_s"] = None
            facts["rows"] = [row["end_reason"] for row in history(tmp_path)]
            facts["peers"] = set(hub.active_peers)
            facts["same_line"] = daemon.manager is m
        finally:
            gate["release"].set()
            await hub.stop()
            if dial is not None:
                facts["dial"] = (await asyncio.gather(dial, return_exceptions=True))[0]
            if not runner.done():
                runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        return facts

    facts = run(scenario())
    assert facts["run_s"] is not None and facts["run_s"] < 4.0, facts
    assert facts["rows"] == ["line_stopped"] and facts["peers"] == set() and facts["same_line"], facts
    assert isinstance(facts["dial"], dict) and facts["dial"].get("code") == "PHONE_CALL_CANCELLED", facts
