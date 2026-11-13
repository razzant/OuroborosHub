"""The plugin against the Hub's connector rules and the core's manifest and plugin API contracts:
permissions and secrets, the API minimum, the declared place and tools."""
import asyncio
import importlib.util
import json
import pathlib
import sys
import types
import xml.etree.ElementTree as ET

from conftest import CORE_REPO, SKILL, core_available, import_plugin
from phone.control import ControlServer


def _scalar(raw: str):
    val = raw.strip()
    if not val:
        return ""
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        return [_scalar(part) for part in inner.split(",")] if inner else []
    if val.startswith("{") and val.endswith("}"):
        out = {}
        for part in val[1:-1].split(","):
            if ":" in part:
                k, v = part.split(":", 1)
                out[k.strip()] = _scalar(v)
        return out
    if val.isdigit():
        return int(val)
    return val


def manifest_data() -> dict:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    try:
        import yaml  # type: ignore
        loaded = yaml.safe_load(front)
        if isinstance(loaded, dict):
            return loaded
    except ImportError:
        pass
    data = {}
    lines = front.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.startswith(" "):
            i += 1
            continue
        key, _, rest = line.partition(":")
        key, rest = key.strip(), rest.strip()
        if rest and rest not in (">-", "|"):
            data[key] = _scalar(rest)
            i += 1
            continue
        i += 1
        block = []
        while i < len(lines) and (lines[i].startswith(" ") or not lines[i].strip()):
            if lines[i].strip():
                block.append(lines[i])
            i += 1
        if rest in (">-", "|"):
            data[key] = " ".join(b.strip() for b in block)
        elif block and block[0].lstrip().startswith("- "):
            items = []
            cur = None
            for b in block:
                s = b.strip()
                if s.startswith("- "):
                    cur = {}
                    items.append(cur)
                    s = s[2:].strip()
                if cur is not None and ":" in s:
                    k, v = s.split(":", 1)
                    cur[k.strip()] = _scalar(v)
            data[key] = items
        else:
            mapping = {}
            sub_i = 0
            while sub_i < len(block):
                b = block[sub_i]
                k, _, v = b.strip().partition(":")
                v = v.strip()
                if v in (">-", "|"):
                    sub_i += 1
                    folded = []
                    while sub_i < len(block) and block[sub_i].startswith("    "):
                        folded.append(block[sub_i].strip())
                        sub_i += 1
                    mapping[k.strip()] = " ".join(folded)
                else:
                    mapping[k.strip()] = _scalar(v)
                    sub_i += 1
            data[key] = mapping
    return data


class _CapturingApi:
    def __init__(self, state_dir: pathlib.Path, *, with_refresh: bool = True, forbid_settings: bool = False):
        self._state_dir = str(state_dir)
        self._forbid_settings = forbid_settings
        self.tools = {}
        self.routes = {}
        self.settings_sections = {}
        self.companions = []
        self.tasks = {}
        self.refreshes = []
        if with_refresh:
            self.refresh_companion_process = self._refresh

    def _refresh(self, *args, **kwargs):
        self.refreshes.append((args, kwargs))
        return "unchanged"

    def get_state_dir(self):
        return self._state_dir

    def get_settings(self, keys):
        if self._forbid_settings:
            raise AssertionError(f"plugin must never call get_settings({keys!r})")
        return {}

    def get_settings_state(self, keys):
        if self._forbid_settings:
            raise AssertionError(f"plugin must never call get_settings_state({keys!r})")
        return {"status": "access_closed", "values": {}, "missing": []}

    def register_tool(self, name, handler, *, description="", schema=None, timeout_sec=None):
        self.tools[name] = {"handler": handler, "description": description, "schema": schema,
                            "timeout_sec": timeout_sec}

    def register_route(self, path, handler, *, methods=("GET",)):
        self.routes[path] = {"handler": handler, "methods": tuple(methods)}

    def register_settings_section(self, section_id, title, *, schema):
        self.settings_sections[section_id] = {"title": title, "schema": schema}

    def register_companion_process(self, name):
        self.companions.append(name)

    def register_supervised_task(self, name, factory, **kwargs):
        self.tasks[name] = {"factory": factory, **kwargs}


def _ensure_json_response(monkeypatch):
    if importlib.util.find_spec("starlette") is not None:
        return
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


def test_the_plugin_never_reads_the_sip_password(tmp_path, monkeypatch):
    _ensure_json_response(monkeypatch)
    data = manifest_data()
    assert "read_settings" not in data["permissions"] and "supervised_task" in data["permissions"]
    assert data["env_from_settings"] == ["TELEPHONY_SIP_PASSWORD"]
    assert data["secret_fields"] == {"sipPassword": "TELEPHONY_SIP_PASSWORD"}
    if core_available():
        if str(CORE_REPO) not in sys.path:
            sys.path.append(str(CORE_REPO))
        from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
        parsed = parse_skill_manifest_text(json.dumps(data))
        assert "read_settings" not in parsed.permissions and parsed.secret_fields == {"sipPassword": "TELEPHONY_SIP_PASSWORD"}

    plugin = import_plugin()
    api = _CapturingApi(tmp_path, with_refresh=True, forbid_settings=True)
    plugin.register(api)

    async def exercise_runtime():
        async def handler(op, args):
            if op == "status":
                return {"ok": True, "line": {"registration": "registered"}, "active_call": None}
            return {"ok": True, "op": op, "args": args}

        server = ControlServer(tmp_path, handler)
        await server.start()
        try:
            for name, kwargs in (("phone_status", {}),
                                 ("phone_call", {"number": "+15550001001", "purpose": "p", "owner_approved": True}),
                                 ("phone_send_dtmf", {"digits": "1"}),
                                 ("phone_hangup", {})):
                reply = await asyncio.to_thread(api.tools[name]["handler"], None, **kwargs)
                assert json.loads(reply)["ok"] is True

            status_resp = await api.routes["status"]["handler"](types.SimpleNamespace(method="GET"))
            assert status_resp.status_code == 200
            get_resp = await api.routes["settings/save"]["handler"](types.SimpleNamespace(method="GET"))
            assert get_resp.status_code == 200

            async def post_json():
                return {"registrar": "sip.example.com", "username": "agent"}

            post_resp = await api.routes["settings/save"]["handler"](
                types.SimpleNamespace(method="POST", json=post_json)
            )
            assert post_resp.status_code == 200
            assert await asyncio.to_thread(plugin.apply_saved_password, api) == "unchanged"
        finally:
            await server.stop()

    asyncio.run(exercise_runtime())
    assert len(api.refreshes) >= 3


def test_plugin_api_minimum_is_the_one_with_the_call_doors(tmp_path, monkeypatch):
    data = manifest_data()
    assert data["plugin_api"] == "2.3"
    if core_available():
        if str(CORE_REPO) not in sys.path:
            sys.path.append(str(CORE_REPO))
        import ouroboros.contracts.plugin_api as papi
        from ouroboros.contracts.skill_manifest import parse_skill_manifest_text

        parsed = parse_skill_manifest_text(json.dumps(data))
        assert papi.negotiate_plugin_api(parsed).ok is True
        assert papi.extension_new_pass_admission_error(parsed) == ""
        for host_ver, expected_ok in (("2.2", False), ("2.3", True), ("2.4", True)):
            monkeypatch.setattr(papi, "PLUGIN_API_VERSION", host_ver)
            monkeypatch.setattr(papi, "plugin_api_surface_fingerprint",
                                lambda v=host_ver: papi.PLUGIN_API_SURFACE_FINGERPRINTS[v])
            res = papi.negotiate_plugin_api(parsed)
            assert res.ok is expected_ok, (host_ver, res)

    plugin = import_plugin()
    api_23 = _CapturingApi(tmp_path / "v23", with_refresh=False)
    plugin.register(api_23)
    assert set(api_23.tools) == {"phone_call", "phone_hangup", "phone_send_dtmf", "phone_status"}
    assert api_23.companions == ["phone_line"] and "password_watch" not in api_23.tasks
    assert plugin.apply_saved_password(api_23) == "unsupported"

    api_24 = _CapturingApi(tmp_path / "v24", with_refresh=True)
    plugin.register(api_24)
    assert "password_watch" in api_24.tasks


def test_hub_place_checks_hold_for_the_manifest(tmp_path):
    """The Hub's connector checks: one place, account scope, vendor destination equal to the label, every
    declared tool registered with an effect and a target, a small currentColor icon."""
    data = manifest_data()
    places = [c for c in data["contributes"] if c.get("provides") == "place"]
    assert len(places) == 1
    place = places[0]
    assert place["label"] == "Phone" and place["destination"] == "vendor:Phone" and place["scope"] == "account"
    assert place["wakes"] == ["direct_message"]

    plugin = import_plugin()
    api = _CapturingApi(tmp_path)
    plugin.register(api)

    declared_tools = data["tools"]
    assert set(api.tools) == {t["name"] for t in declared_tools}
    for tool in declared_tools:
        assert tool["effect"] in ("read", "write") and "target" in tool
        schema = api.tools[tool["name"]]["schema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        if isinstance(tool["target"], str) and tool["target"]:
            assert tool["target"] in schema["properties"] and tool["target"] in schema["required"]
    assert place["source"]["route"] in api.routes
    assert place["settings"] in api.settings_sections
    assert api.companions == [c["name"] for c in data["companion_processes"]]

    raw = (SKILL / "ui" / "icon.svg").read_bytes()
    root = ET.fromstring(raw)
    assert len(raw) <= 8192 and b"<!" not in raw
    assert root.attrib["viewBox"] == "0 0 24 24" and "currentColor" in root.attrib.values()  # the Hub's rule
