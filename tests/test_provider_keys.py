"""Provider keys: refresh must send each granted key only to its own issuer's endpoints.

The settings are purely fictional and the adapter's HTTP boundary is intercepted, so the
regression runs offline; what it verifies is where each synthetic sentinel would be sent.
"""
import json
from pathlib import Path
import socket
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.provider_keys import keys_adapters, plugin  # noqa: E402

OPENAI_KEY = "sk-openai-sentinel"
COMPAT_KEY = "sk-compat-sentinel"
CUSTOM_KEY = "sk-custom-sentinel"
ROUTER_ROW_KEY = "sk-router-sentinel"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
PROXY_BASE = "https://proxy.example.invalid/v1"
CUSTOM_BASE = "https://custom.example.invalid/v1"
CONNECTIONS = "OUROBOROS_CONNECTIONS"
# Expected canonical metadata destinations, held independently of the adapter's URL table.
OPENAI_URLS = {"https://api.openai.com/v1/models",
              "https://api.openai.com/v1/organization/costs"}
OPENROUTER_URLS = {"https://openrouter.ai/api/v1/key", "https://openrouter.ai/api/v1/credits"}

# Minimal bodies the adapters' decoders accept, keyed by query-scrubbed destination.
BODIES = {
    "https://api.openai.com/v1/models": {"data": [{"id": "synthetic-model"}]},
    "https://api.openai.com/v1/organization/costs": {"data": [{"start_time": 1.0, "end_time": 2.0, "results": []}]},
    "https://openrouter.ai/api/v1/key": {"data": {"usage_daily": 1.0}},
    "https://openrouter.ai/api/v1/credits": {"data": {"total_credits": 2.0, "total_usage": 0.5}},
}


class Settings:
    """A fictional host: every absent requested key is simply missing, never denied."""

    def __init__(self, values):
        self.values = values

    def get_settings_state(self, keys):
        return {"status": "ok", "values": self.values,
                "missing": [key for key in keys if key not in self.values]}


def connections(*rows):
    """Named-connection rows as one settings value; keys_settings parses a JSON string."""
    return json.dumps(list(rows))


@pytest.fixture
def sent(monkeypatch):
    """Intercept the adapter's HTTP boundary; record each destination and its bearer."""
    sent = []

    def refuse(*_args, **_kwargs):
        raise RuntimeError("real sockets are disabled in the provider keys tests")

    def fake_http(url, headers, timeout):
        # The OpenAI costs read appends a varying time-window query, so compare scrubbed URLs.
        destination = url.split("?", 1)[0]
        sent.append((destination, headers["Authorization"]))
        return 200, BODIES[destination], {}

    # Even if future code bypassed keys_adapters.http with a direct urllib or socket
    # call, no sentinel could reach a real provider from this suite.
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(keys_adapters, "http", fake_http)
    return sent


def refresh(values):
    board = plugin.Board(Settings(values))
    try:
        return board.refresh()
    finally:
        board.close()


def test_direct_openai_key_is_sent_only_to_openai(sent):
    response = refresh({"OPENAI_API_KEY": OPENAI_KEY})
    assert response["outcome"]["reason"] == "finished"
    assert {url for url, _ in sent} == OPENAI_URLS
    assert {auth for _, auth in sent} == {"Bearer " + OPENAI_KEY}
    assert len(sent) == 2


def test_dedicated_openrouter_base_without_its_own_key_stays_keyless(sent):
    response = refresh({"OPENAI_API_KEY": OPENAI_KEY,
                        "OPENAI_COMPATIBLE_BASE_URL": OPENROUTER_BASE})
    assert response["outcome"]["reason"] == "finished"
    # The dedicated pair never borrows the OpenAI sentinel: OpenRouter is not read at all.
    assert {url for url, _ in sent} == OPENAI_URLS
    assert {auth for _, auth in sent} == {"Bearer " + OPENAI_KEY}
    assert len(sent) == 2


def test_dedicated_openrouter_base_with_its_own_key_pairs_each_sentinel(sent):
    response = refresh({"OPENAI_API_KEY": OPENAI_KEY,
                        "OPENAI_COMPATIBLE_BASE_URL": OPENROUTER_BASE,
                        "OPENAI_COMPATIBLE_API_KEY": COMPAT_KEY})
    assert response["outcome"]["reason"] == "finished"
    router = [pair for pair in sent if pair[0].startswith("https://openrouter.ai/")]
    openai = [pair for pair in sent if pair[0].startswith("https://api.openai.com/")]
    assert {url for url, _ in router} == OPENROUTER_URLS
    assert {auth for _, auth in router} == {"Bearer " + COMPAT_KEY}
    assert {url for url, _ in openai} == OPENAI_URLS
    assert {auth for _, auth in openai} == {"Bearer " + OPENAI_KEY}
    assert len(sent) == 4


def test_foreign_legacy_base_never_sends_the_openai_key(sent):
    response = refresh({"OPENAI_API_KEY": OPENAI_KEY, "OPENAI_BASE_URL": PROXY_BASE})
    assert response["outcome"]["reason"] == "no_readable_keys"
    # A non-canonical legacy base binds the flat key to a foreign issuer that is
    # never fetched: the sentinel goes nowhere, and api.openai.com is not read either.
    assert sent == []


def test_legacy_openrouter_base_routes_the_key_to_openrouter_only(sent):
    response = refresh({"OPENAI_API_KEY": OPENAI_KEY, "OPENAI_BASE_URL": OPENROUTER_BASE})
    assert response["outcome"]["reason"] == "finished"
    # Positive twin proving the legacy branch is reached: a canonical OpenRouter base
    # re-issues the flat key as an OpenRouter credential, read only at OpenRouter.
    assert {url for url, _ in sent} == OPENROUTER_URLS
    assert {auth for _, auth in sent} == {"Bearer " + OPENAI_KEY}
    assert len(sent) == 2


def test_named_custom_connection_key_reaches_no_destination(sent):
    response = refresh({CONNECTIONS: connections(
        {"kind": "openai-compatible", "name": "custom",
         "api_key": CUSTOM_KEY, "base_url": CUSTOM_BASE})})
    assert response["outcome"]["reason"] == "no_readable_keys"
    # The row parsed (a malformed one would answer settings_unreadable), but its
    # non-canonical base names no recognized issuer, so its key is never bound.
    assert sent == []


def test_named_openrouter_connection_key_reaches_only_openrouter(sent):
    response = refresh({CONNECTIONS: connections(
        {"kind": "openai-compatible", "name": "router",
         "api_key": ROUTER_ROW_KEY, "base_url": OPENROUTER_BASE})})
    assert response["outcome"]["reason"] == "finished"
    # Positive twin: the named-row path really runs, and a canonical OpenRouter base
    # yields exactly two OpenRouter reads carrying only this row's own sentinel.
    assert {url for url, _ in sent} == OPENROUTER_URLS
    assert {auth for _, auth in sent} == {"Bearer " + ROUTER_ROW_KEY}
    assert len(sent) == 2
