"""The payload as a package: manifest, declared files, what it registers, and that it ships no fixtures."""
from __future__ import annotations

import asyncio
import json
import re

import pytest
import yaml

from conftest import PAYLOAD, payload_module


def manifest():
    text = (PAYLOAD / "SKILL.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---", 2)[1])


def test_manifest_is_a_pluginapi_2_1_extension_with_one_module_tab():
    m = manifest()
    assert m["name"] == "memory-atlas" and m["type"] == "extension" and m["entry"] == "plugin.py"
    assert m["plugin_api"] == "2.1" and m["permissions"] == ["route", "widget"]   # read_memory is 2.1
    render = m["ui_tab"]["render"]
    assert render["kind"] == "module" and render["entry_type"] == "module" and render["appearance"] == "host"
    assert render["start"] == "manual" and render["height"] == 600 and render["span"] == 2
    assert len(m["ui_tab"]["icon"]) == 1   # one glyph


def test_plugin_registers_the_same_render_as_the_manifest():
    plugin = payload_module("plugin")
    assert plugin.RENDER == manifest()["ui_tab"]["render"]


def test_declared_files_are_exactly_the_browser_files():
    render = manifest()["ui_tab"]["render"]
    declared = {render["entry"], *render["assets"]}
    on_disk = {p.relative_to(PAYLOAD).as_posix() for p in (PAYLOAD / "app").rglob("*") if p.is_file()}
    assert declared == on_disk
    for rel in declared:   # the lease contract: ASCII segments, accepted suffixes, UTF-8 text
        assert re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*", rel) and len(rel) <= 240
        assert rel.rsplit(".", 1)[1] in ("js", "css")
        (PAYLOAD / rel).read_text(encoding="utf-8")


def test_payload_ships_no_fixture_or_development_code():
    files = [p for p in PAYLOAD.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    names = {p.relative_to(PAYLOAD).as_posix() for p in files}
    assert names == {"SKILL.md", "plugin.py", "atlas_reader.py", "atlas_routes.py", "atlas_search.py",
                     "app/main.js", "app/atlas.js", "app/client.js", "app/words.js", "app/markdown.js", "app/parts.js",
                     "app/atlas.css"}
    for p in files:
        text = p.read_text(encoding="utf-8")
        for banned in ("fixture_reader", "corpus.json", "FixtureReader", "seed_memory", "realhost"):
            assert banned not in text, (p, banned)
        assert "Mira" not in text and "greenhouse" not in text, p   # no sample memory in the payload


def test_python_reads_memory_only_through_the_host():
    """The payload opens no file, database or index of memory: every word comes from ``api.read_memory``."""
    for name in ("plugin.py", "atlas_reader.py", "atlas_routes.py", "atlas_search.py"):
        text = (PAYLOAD / name).read_text(encoding="utf-8")
        for banned in ("open(", "sqlite3", "read_text(", "read_bytes(", "os.walk", "glob(", "chronicle_store",
                       "memory_reader", "knowledge.py", "jsonl"):
            assert banned not in text, (name, banned)


def test_browser_code_uses_no_network_or_storage_of_its_own():
    for p in (PAYLOAD / "app").glob("*.js"):
        text = p.read_text(encoding="utf-8")
        for banned in ("XMLHttpRequest", "WebSocket", "EventSource", "localStorage", "sessionStorage", "innerHTML",
                       "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert banned not in text, (p.name, banned)


def test_css_adds_no_border_or_outline_to_controls():
    css = (PAYLOAD / "app" / "atlas.css").read_text(encoding="utf-8")
    for rule in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        selector, body = rule
        if re.search(r"\.btn|button|\.row|\.link|\.seg", selector):
            assert not re.search(r"\bborder\s*:(?!\s*0\b)", body), selector      # only `border: 0`
            assert "border-color" not in body and "border-width" not in body, selector
            if "focus-visible" not in selector:
                assert "outline" not in body, selector
    assert ":focus-visible" in css   # the one ring stays


class FakeApi:
    def __init__(self, read_memory=None):
        self.routes, self.tabs, self.unload = {}, [], []
        if read_memory is not None:
            self.read_memory = read_memory

    def register_route(self, path, handler, methods=("GET",)):
        self.routes[path] = (handler, tuple(methods))

    def register_ui_tab(self, tab_id, title, icon=None, render=None):
        self.tabs.append((tab_id, title, icon, render))

    def on_unload(self, fn):
        self.unload.append(fn)


V2 = {"atlas/v2/" + op for op in ("overview", "list", "text", "entries", "changes", "search", "inbound")}


def test_register_adds_read_only_routes_and_the_reader_the_host_allows():
    plugin = payload_module("plugin")
    reader_mod = payload_module("atlas_reader")
    p = plugin.MemoryAtlasPlugin()
    api = FakeApi()
    p.register(api)
    assert set(api.routes) == V2 | {"author-kit"}
    assert all(methods == ("GET",) for _, methods in api.routes.values())
    assert isinstance(p.reader, reader_mod.UnconnectedReader)   # a host without read_memory
    assert api.tabs == [("atlas", "Memory Atlas", "◈", plugin.RENDER)]
    assert api.unload == [p.dispose]
    p.dispose()
    assert p.reader is None

    connected = plugin.MemoryAtlasPlugin()
    connected.register(FakeApi(read_memory=lambda operation, parameters=None: {}))
    assert isinstance(connected.reader, reader_mod.CoreReader)


def _request(path, query=b""):
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/api/extensions/memory-atlas/" + path,
                    "query_string": query, "headers": []})


def test_route_handler_answers_the_refusal_as_json():
    pytest.importorskip("starlette")
    plugin = payload_module("plugin")
    p = plugin.MemoryAtlasPlugin()
    api = FakeApi()
    p.register(api)
    handler = api.routes["atlas/v2/text"][0]
    response = asyncio.run(handler(_request("atlas/v2/text", b'address={"kind":"record","id":"abc"}')))
    assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
    assert json.loads(response.body)["error"]["code"] == "reader_not_connected"
    response = asyncio.run(handler(_request("atlas/v2/text", b'address={"kind":"record","id":"abc"}&path=/etc')))
    assert response.status_code == 400 and json.loads(response.body)["error"]["code"] == "unknown_parameter"
    p.dispose()
    response = asyncio.run(handler(_request("atlas/v2/text", b'address={"kind":"record","id":"abc"}')))
    assert response.status_code == 503   # a disposed plugin answers, never crashes
