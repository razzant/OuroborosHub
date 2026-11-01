"""Behavior tests for the a2a agent-card builder (_agent_card).

Mocks the Host Service (/identity and /tools/schemas) so we exercise the real
card-building logic without a live host. v1.2.0 contract: the card name is the
stable "Ouroboros" (operator override wins; the /identity heading is a section
title, never a name); the description leads with the curated capability summary
and appends the /identity first line as flavor; the skill list ALWAYS starts
with the five curated capability categories, followed by live per-tool entries;
the request path stays fast (short fetch + last-good cache) while the full
retry ladder belongs to the background refresher. Also asserts the A2A v0.3
dict-card transport fields survive.

Run with an interpreter that has httpx + starlette (the skill's runtime deps):
    python3 -m pytest tests/test_a2a_agent_card.py
or standalone:
    python3 tests/test_a2a_agent_card.py
"""
from __future__ import annotations

import importlib.util
import logging
import os
import pathlib

_DAEMON = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "a2a_daemon.py"


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _load_daemon(env: dict | None = None):
    """(Re)load the daemon module with a controlled environment."""
    for key in ("A2A_AGENT_NAME", "A2A_AGENT_DESCRIPTION", "HOST_SERVICE_TOKEN"):
        os.environ.pop(key, None)
    os.environ["OUROBOROS_SKILL_STATE_DIR"] = "/tmp/a2a_test_state_dir_nonexistent"
    # The module-level app build starts the background tools refresher; with the
    # sleep below neutralized those threads would spin against the shared httpx
    # mock and corrupt later tests' call counters. Disable it for the harness.
    os.environ["A2A_TOOLS_REFRESHER"] = "0"
    for k, v in (env or {}).items():
        os.environ[k] = v
    spec = importlib.util.spec_from_file_location("a2ad_under_test", _DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.time.sleep = lambda *_a, **_k: None  # neutralize retry backoff
    return mod


def _install_get(mod, *, identity, tools):
    """identity: (status, payload) or Exception. tools: (status, payload) or Exception.
    Returns a counter dict tracking how many times /tools/schemas was hit."""
    calls = {"identity": 0, "tools": 0}

    def fake_get(url, **_kw):
        if url.endswith("/identity"):
            calls["identity"] += 1
            if isinstance(identity, Exception):
                raise identity
            return _FakeResponse(*identity)
        if url.endswith("/tools/schemas"):
            calls["tools"] += 1
            if isinstance(tools, Exception):
                raise tools
            return _FakeResponse(*tools)
        raise AssertionError(f"unexpected url {url}")

    mod.httpx.get = fake_get
    return calls


def _capture_warnings(mod):
    records: list[str] = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = _H(level=logging.WARNING)
    mod.logger.addHandler(h)
    mod.logger.setLevel(logging.WARNING)
    return records


_CATEGORY_IDS = [
    "code-and-files",
    "web-and-media",
    "long-running-tasks",
    "self-modification",
    "skills-extensions",
]


def _install_get_tool_sequence(mod, *, identity, tool_payloads):
    """/tools/schemas returns a SEQUENCE of payloads across successive calls (the
    last payload repeats once exhausted) — models a host that is empty for the
    first polls and then becomes ready."""
    calls = {"identity": 0, "tools": 0}
    seq = list(tool_payloads)

    def fake_get(url, **_kw):
        if url.endswith("/identity"):
            calls["identity"] += 1
            return _FakeResponse(*identity)
        if url.endswith("/tools/schemas"):
            idx = min(calls["tools"], len(seq) - 1)
            calls["tools"] += 1
            return _FakeResponse(*seq[idx])
        raise AssertionError(f"unexpected url {url}")

    mod.httpx.get = fake_get
    return calls


def test_capability_first_card_with_live_tools():
    mod = _load_daemon()
    _install_get(
        mod,
        identity=(200, {"ok": True, "name": "Who I Am", "description": "a self-authoring agent"}),
        tools=(200, {"tools": [
            {"function": {"name": "read_file", "description": "Read a file."}},
            {"function": {"name": "web_search", "description": "Search the web."}},
        ]}),
    )
    card = mod._agent_card()
    # The identity heading is a section title, never the card name.
    assert card["name"] == "Ouroboros"
    # Capability summary leads; identity first line survives as flavor.
    assert card["description"].startswith(mod._CAPABILITY_LEAD)
    assert card["description"].endswith("a self-authoring agent")
    ids = [s["id"] for s in card["skills"]]
    assert ids[:5] == _CATEGORY_IDS
    assert ids[5:] == ["read_file", "web_search"]
    # v0.3 transport fields preserved
    assert card["protocolVersion"] == "0.3.0"
    assert card["preferredTransport"] == "JSONRPC"
    assert card["additionalInterfaces"] and card["additionalInterfaces"][0]["transport"] == "JSONRPC"
    assert card["version"] == "1.5.0"


def test_env_override_beats_everything():
    mod = _load_daemon(env={"A2A_AGENT_NAME": "OpName", "A2A_AGENT_DESCRIPTION": "OpDesc"})
    _install_get(
        mod,
        identity=(200, {"ok": True, "name": "IdentityName", "description": "IdentityDesc"}),
        tools=(200, {"tools": []}),
    )
    card = mod._agent_card()
    assert card["name"] == "OpName"
    assert card["description"] == "OpDesc"
    # Categories are still served under an operator identity.
    assert [s["id"] for s in card["skills"]][:5] == _CATEGORY_IDS


def test_identity_failure_still_serves_capabilities():
    mod = _load_daemon()
    recs = _capture_warnings(mod)
    _install_get(
        mod,
        identity=RuntimeError("connection refused"),
        tools=(200, {"tools": [{"function": {"name": "read_file", "description": "Read."}}]}),
    )
    card = mod._agent_card()
    assert card["name"] == "Ouroboros"
    assert card["description"] == mod._CAPABILITY_LEAD  # no flavor available
    assert [s["id"] for s in card["skills"]] == _CATEGORY_IDS + ["read_file"]
    assert any("/identity" in r for r in recs), recs


def test_request_path_fetch_is_short_and_card_stays_useful():
    """A discovery GET must not ride the full warmup ladder (a cold host once held
    the card endpoint for ~57s). With no last-good list, the request path does the
    SHORT fetch only — and the card still carries the curated categories."""
    mod = _load_daemon()
    recs = _capture_warnings(mod)
    calls = _install_get(
        mod,
        identity=(200, {"ok": True, "name": "Who I Am", "description": "real desc"}),
        tools=RuntimeError("host chat-agent not ready"),
    )
    card = mod._agent_card()
    assert calls["tools"] == mod._TOOLS_FETCH_ATTEMPTS_REQUEST
    ids = [s["id"] for s in card["skills"]]
    assert ids == _CATEGORY_IDS
    # Never the two historical collapses.
    assert all(s["name"] != "General" for s in card["skills"])
    assert all(s["id"] != "ouroboros" for s in card["skills"])
    assert any("/tools/schemas" in r for r in recs), recs


def test_background_ladder_uses_full_attempts():
    mod = _load_daemon()
    calls = _install_get(
        mod,
        identity=(200, {"ok": True, "name": "n", "description": "d"}),
        tools=RuntimeError("still booting"),
    )
    assert mod._fetch_tool_schemas() == []
    assert calls["tools"] == mod._TOOLS_FETCH_ATTEMPTS


def test_empty_200_is_retried_within_request_budget():
    """An empty 200 (host chat-agent not ready) is retried, not accepted as final —
    within the SHORT request budget."""
    mod = _load_daemon()
    empty = (200, {"tools": []})
    ready = (200, {"tools": [
        {"function": {"name": "read_file", "description": "Read a file."}},
        {"function": {"name": "web_search", "description": "Search the web."}},
    ]})
    calls = _install_get_tool_sequence(
        mod,
        identity=(200, {"ok": True, "name": "n", "description": "real desc"}),
        tool_payloads=[empty, ready],
    )
    card = mod._agent_card()
    assert calls["tools"] == 2
    assert [s["id"] for s in card["skills"]] == _CATEGORY_IDS + ["read_file", "web_search"]


def test_last_good_cache_serves_without_refetch_and_never_regresses():
    """Once populated, the card serves the cached tool list without a refetch on
    the request path, and a later empty background fetch does not regress it."""
    mod = _load_daemon()
    ready = (200, {"tools": [{"function": {"name": "read_file", "description": "Read."}}]})
    empty = (200, {"tools": []})
    _install_get_tool_sequence(
        mod,
        identity=(200, {"ok": True, "name": "n", "description": "real desc"}),
        tool_payloads=[ready],
    )
    first = mod._agent_card()
    assert [s["id"] for s in first["skills"]] == _CATEGORY_IDS + ["read_file"]
    # Warm cache: the next card build performs ZERO tool fetches.
    calls = _install_get_tool_sequence(
        mod,
        identity=(200, {"ok": True, "name": "n", "description": "real desc"}),
        tool_payloads=[empty],
    )
    second = mod._agent_card()
    assert calls["tools"] == 0
    assert [s["id"] for s in second["skills"]] == _CATEGORY_IDS + ["read_file"]
    # A background ladder that now sees only empties keeps the last-good list.
    recs = _capture_warnings(mod)
    assert mod._fetch_tool_schemas() == [{"function": {"name": "read_file", "description": "Read."}}]
    assert any("last known-good" in r for r in recs), recs


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL_OK ({len(fns)} tests)")


if __name__ == "__main__":
    _run_all()
