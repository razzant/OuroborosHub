"""The control channel between the tools and the companion: its token, requests in flight when it closes,
and a hang-up that answers within its bound."""
import asyncio
import json
import time

import pytest

from conftest import SKILL, import_plugin, run, until
from hubfake import LoopbackHubServer, history, sip_line_config
from phone import config as cfg
from phone import control
from phone import manager as mgr
from phone.control import ControlServer, ControlUnavailable, request
from phone.daemon import Daemon
from phone.peer import HostClient
from sipfake import FakeNode, SDP, contact


def test_control_channel_requires_its_token(tmp_path):
    async def scenario():
        async def handler(op, args):
            return {"ok": True, "op": op}
        server = ControlServer(tmp_path, handler)
        port = await server.start()
        good = await asyncio.to_thread(request, tmp_path, "status", {})
        endpoint = json.loads((tmp_path / "control.json").read_text())
        endpoint["token"] = "wrong"
        forged = tmp_path / "forged"
        forged.mkdir()
        (forged / "control.json").write_text(json.dumps(endpoint))
        bad = await asyncio.to_thread(request, forged, "status", {})
        mode = (tmp_path / "control.json").stat().st_mode & 0o777
        await server.stop()
        return good, bad, mode, port
    good, bad, mode, port = asyncio.run(scenario())
    assert good == {"ok": True, "op": "status"} and bad["code"] == "PHONE_CONTROL_REFUSED" and mode == 0o600
    with pytest.raises(ControlUnavailable):
        request(tmp_path, "status", {})  # the server removed its control file


# -- The control channel waits for no request --------------------------------------------------------------

def test_closing_the_control_channel_answers_requests_in_flight_and_waits_for_none(tmp_path):
    async def scenario():
        release = asyncio.Event()

        async def handler(op, args):
            await release.wait()  # a dial in admission, a hangup waiting for its BYE
            return {"ok": True}

        server = ControlServer(tmp_path, handler)
        await server.start()
        pending = asyncio.ensure_future(asyncio.to_thread(control.request, tmp_path, "dial", {}, timeout_sec=6))
        await asyncio.sleep(0.3)
        started = time.monotonic()
        try:
            try:
                await asyncio.wait_for(server.stop(), 3)
                stopped = time.monotonic() - started
            except asyncio.TimeoutError:
                stopped = None
            try:
                reply = await asyncio.wait_for(asyncio.shield(pending), 3)
            except (asyncio.TimeoutError, control.ControlUnavailable) as exc:
                reply = repr(exc)
        finally:
            release.set()
            try:
                await asyncio.wait_for(pending, 7)
            except Exception:
                pass
        return stopped, reply, (tmp_path / control.CONTROL_FILE).exists()

    stopped, reply, file_left = run(scenario())
    assert stopped is not None and stopped < 1.5, stopped
    assert isinstance(reply, dict) and reply.get("code") == "PHONE_LINE_STOPPING", reply
    assert not file_left


# -- Bounded hang-ups --------------------------------------------------------------------------------------

def test_phone_hangup_answers_ok_when_the_bye_is_never_answered(tmp_path, monkeypatch):
    plugin = import_plugin()
    monkeypatch.setattr(plugin, "_STATE_DIR", tmp_path)

    async def scenario():
        node = await FakeNode.start()
        hub = await LoopbackHubServer().start()
        cfg.save(tmp_path, sip_line_config(node))
        daemon = Daemon(tmp_path, HostClient(hub.url, "tok"), "")
        runner = asyncio.ensure_future(daemon.run())
        facts = {}
        try:
            assert await until(lambda: (tmp_path / control.CONTROL_FILE).exists(), 3)
            m = daemon.manager
            m.policy.limits.dial_cooldown_seconds = 0
            dial = asyncio.ensure_future(m.dial(number="+15550001001", purpose="p", owner_approved=True,
                                                live_call_id="live-1"))
            invite, addr = await node.expect("INVITE")
            node.reply(invite, addr, 200, to_tag="r1", extra=contact(node), body=SDP)
            await node.expect("ACK")
            assert (await dial)["ok"] is True
            assert await until(lambda: hub.active_peers, 3)
            started = time.monotonic()
            text = await asyncio.to_thread(plugin.phone_hangup)  # the tool, with its own control timeout
            facts["seconds"] = time.monotonic() - started
            facts["text"] = text
            facts["active"] = m.active
            facts["bye_in_flight"] = any(tx.method == "BYE" for tx in m.provider.ua._client_tx.values())
            facts["rows"] = [row["end_reason"] for row in history(tmp_path)]
            facts["left"] = not hub.active_peers
            bye, baddr = await node.expect("BYE")
            node.reply(bye, baddr, 200)  # answered at last: the line stops at once
        finally:
            daemon._stop.set()
            await asyncio.wait_for(runner, 10)
            await hub.stop()
            node.close()
        return facts

    facts = run(scenario())
    assert facts["text"].startswith("{") and json.loads(facts["text"])["ok"] is True, facts["text"][:200]
    assert facts["seconds"] < plugin.CONTROL_TIMEOUT_SEC - 5, facts["seconds"]
    assert facts["active"] is None and facts["rows"] == ["agent_hangup"] and facts["left"]
    assert facts["bye_in_flight"]  # the BYE goes on in the background, to its own timer
    readme = " ".join((SKILL / "README.md").read_text(encoding="utf-8").split())
    assert f"`phone_hangup` waits up to {mgr.HANGUP_WAIT_SECONDS:g} s" in readme
