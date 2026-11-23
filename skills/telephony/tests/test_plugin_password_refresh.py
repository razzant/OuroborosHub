"""A saved SIP password reaches the companion: applied only between calls, deferred while the companion's
status is unknown, and picked up by the periodic watch."""
import asyncio
import importlib.util
import json
import sys
import types

import pytest

from conftest import SKILL, import_plugin, run
from phone.control import ControlUnavailable, request


def test_a_saved_password_is_applied_between_calls_only():
    plugin = import_plugin()

    class Api:
        def __init__(self):
            self.calls = []

        def refresh_companion_process(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return "unchanged"

    api = Api()
    busy = plugin.apply_saved_password(api, ask=lambda *a: {"ok": True, "active_call": {"state": "answered"}})
    idle = plugin.apply_saved_password(api, ask=lambda *a: {"ok": True, "active_call": None})
    assert busy == "deferred_call_active" and idle == "unchanged"
    assert api.calls == [(("phone_line",), {})]  # no expected values: the host compares by itself
    assert plugin.apply_saved_password(object()) == "unsupported"


# -- apply_saved_password defers when status times out or is unknown ---------------------------------------

def test_apply_saved_password_defers_when_status_times_out(tmp_path, monkeypatch):
    plugin = import_plugin()
    monkeypatch.setattr(plugin, "_STATE_DIR", tmp_path)

    class Api:
        def __init__(self):
            self.calls = []

        def refresh_companion_process(self, name):
            self.calls.append(name)
            return "restarted"

    api = Api()
    # Missing control.json => companion genuinely not running => safe to refresh
    assert plugin.apply_saved_password(api) == "restarted"
    assert api.calls == ["phone_line"]
    api.calls.clear()

    # Stalled companion control socket => timeout => status unknown => MUST NOT restart
    async def stalled_scenario():
        accepted = []

        async def hold(reader, writer):
            accepted.append(writer)
            await asyncio.sleep(5)

        srv = await asyncio.start_server(hold, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        (tmp_path / "control.json").write_text(json.dumps({"host": "127.0.0.1", "port": port, "token": "t"}))
        try:
            outcome = await asyncio.to_thread(
                plugin.apply_saved_password, api, lambda op, args, _t: plugin._ask(op, args, 0.1)
            )
            assert api.calls == []
            assert outcome == "deferred_status_unknown"
            with pytest.raises(ControlUnavailable) as exc_info:
                await asyncio.to_thread(request, tmp_path, "status", {}, timeout_sec=0.1)
            assert getattr(exc_info.value, "reason", None) == "timeout"
        finally:
            for w in accepted:
                w.close()
            srv.close()
            await srv.wait_closed()

    run(stalled_scenario())


# -- _password_watch interval, terminal exit, and route triggers -------------------------------------------

def test_password_watch_interval_terminal_exit_and_route_triggers(tmp_path, monkeypatch):
    plugin = import_plugin()
    assert 30 <= plugin.REFRESH_SECONDS <= 60

    if importlib.util.find_spec("starlette") is None:
        starlette_mod = types.ModuleType("starlette")
        responses_mod = types.ModuleType("starlette.responses")

        class _StubJSONResponse:
            def __init__(self, content, status_code=200):
                self.body = json.dumps(content).encode("utf-8")
                self.status_code = status_code

        responses_mod.JSONResponse = _StubJSONResponse
        starlette_mod.responses = responses_mod
        monkeypatch.setitem(sys.modules, "starlette", starlette_mod)
        monkeypatch.setitem(sys.modules, "starlette.responses", responses_mod)

    class TerminalApi:
        def __init__(self, result):
            self.result = result
            self.calls = 0

        def get_state_dir(self):
            return str(tmp_path)

        def refresh_companion_process(self, name):
            self.calls += 1
            return self.result

        def register_tool(self, *a, **k):
            pass

        def register_route(self, *a, **k):
            pass

        def register_settings_section(self, *a, **k):
            pass

        def register_companion_process(self, *a, **k):
            pass

        def register_supervised_task(self, *a, **k):
            pass

    async def scenario():
        for terminal in ("access_closed", "not_server"):
            api = TerminalApi(terminal)
            plugin.register(api)
            await asyncio.wait_for(plugin._password_watch(api)(), timeout=0.5)
            assert api.calls == 1

        api_routes = TerminalApi("unchanged")
        plugin.register(api_routes)
        await plugin._status_route(types.SimpleNamespace(method="GET"))
        assert api_routes.calls == 1

        async def post_json():
            return {"registrar": "sip.example.com", "username": "agent"}

        resp = await plugin._settings_route(types.SimpleNamespace(method="POST", json=post_json))
        assert resp.status_code == 200 and api_routes.calls == 2

    run(scenario())


def test_password_watch_sleeps_its_interval_the_docs_say_it_and_unsupported_hosts_skip_it(tmp_path,
                                                                                             monkeypatch):
    plugin = import_plugin()
    monkeypatch.setattr(plugin, "_STATE_DIR", tmp_path)  # no control.json: the companion is not running
    monkeypatch.setattr(plugin, "_API", plugin._API)     # register() below replaces it; restored afterwards
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(plugin, "asyncio", types.SimpleNamespace(sleep=fake_sleep, to_thread=asyncio.to_thread))

    class Api:
        def __init__(self, refresh):
            self.refreshes, self.supervised = 0, []
            if refresh:
                self.refresh_companion_process = self._refresh

        def _refresh(self, name):
            self.refreshes += 1
            return "unchanged"

        def get_state_dir(self):
            return str(tmp_path)

        def register_supervised_task(self, name, *a, **k):
            self.supervised.append(name)

        def __getattr__(self, name):
            if name.startswith("register_"):
                return lambda *a, **k: None
            raise AttributeError(name)

    async def scenario():
        api = Api(refresh=True)
        with pytest.raises(asyncio.CancelledError):
            await plugin._password_watch(api)()
        assert sleeps == [plugin.REFRESH_SECONDS] * 2 and api.refreshes == 2
        old = Api(refresh=False)
        assert plugin.apply_saved_password(old) == "unsupported"
        await asyncio.wait_for(plugin._password_watch(old)(), 1)  # returns at once, no sleeping
        assert sleeps == [plugin.REFRESH_SECONDS] * 2

    run(scenario())
    registered = Api(refresh=True)
    plugin.register(registered)
    unsupported = Api(refresh=False)
    plugin.register(unsupported)
    assert registered.supervised == ["password_watch"] and unsupported.supervised == []
    assert plugin.REFRESH_SECONDS == 45
    assert f"about {plugin.REFRESH_SECONDS} s" in (SKILL / "README.md").read_text(encoding="utf-8")
    assert f"about {plugin.REFRESH_SECONDS} s" in (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert f"примерно за {plugin.REFRESH_SECONDS} с" in (SKILL / "QUICKSTART.ru.md").read_text(encoding="utf-8")
