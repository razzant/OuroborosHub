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
OPENAI_ROW_KEY = "sk-openai-row-sentinel"
OPENAI_BASE = "https://api.openai.com/v1"
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
    """A fictional host: a value is served only for a requested key, and every
    absent requested key is simply missing, never denied. An inventory asking
    for too little thus loses exactly what the real host would withhold."""

    def __init__(self, values):
        self.values = values

    def get_settings_state(self, keys):
        return {"status": "ok",
                "values": {key: value for key, value in self.values.items() if key in keys},
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
    # Positive evidence that the dedicated row was seen and deliberately not
    # read, rather than dropped: it surfaces in the snapshot as a keyless
    # OpenRouter row attributed to OPENAI_COMPATIBLE_API_KEY alone.
    keyless = [row for row in response["snapshot"]["rows"]
               if row["status"]["code"] == "keyless"]
    assert len(keyless) == 1
    assert keyless[0]["kind"] == "openrouter"
    assert [use["setting"] for use in keyless[0]["uses"]] == ["OPENAI_COMPATIBLE_API_KEY"]


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
    # "no_readable_keys" + sent == [] on their own would also hold for empty settings,
    # so they prove that nothing was fetched, not why. The snapshot pins the cause:
    # the flat key surfaces as exactly one row, an OpenAI-compatible credential whose
    # non-canonical legacy base names no recognized issuer, reported
    # no_metadata_endpoint and attributed to OPENAI_API_KEY. Its key is never bound to
    # a read, so the sentinel goes nowhere and api.openai.com is not contacted either.
    assert sent == []
    rows = response["snapshot"]["rows"]
    assert len(rows) == 1
    assert rows[0]["status"]["code"] == "no_metadata_endpoint"
    assert [use["setting"] for use in rows[0]["uses"]] == ["OPENAI_API_KEY"]


def test_foreign_legacy_base_with_dedicated_router_pair_reads_only_the_router(sent):
    response = refresh({"OPENAI_API_KEY": OPENAI_KEY,
                        "OPENAI_BASE_URL": PROXY_BASE,
                        "OPENAI_COMPATIBLE_BASE_URL": OPENROUTER_BASE,
                        "OPENAI_COMPATIBLE_API_KEY": COMPAT_KEY})
    assert response["outcome"]["reason"] == "finished"
    # The dedicated pair supersedes the legacy compatible use, not its issuer
    # evidence: the only reads are the two OpenRouter endpoints carrying the
    # pair's own sentinel. The foreign legacy base is contacted by nothing and
    # the OpenAI sentinel goes nowhere.
    assert {url for url, _ in sent} == OPENROUTER_URLS
    assert {auth for _, auth in sent} == {"Bearer " + COMPAT_KEY}
    assert len(sent) == 2
    # The flat key behind the foreign legacy base still surfaces, and the
    # foreign base itself spawns no row: exactly one unverified_issuer OpenAI
    # row attributed to OPENAI_API_KEY, whose key is never bound to a read.
    assert len(response["snapshot"]["rows"]) == 2
    legacy = [row for row in response["snapshot"]["rows"]
              if row["status"]["code"] == "unverified_issuer"]
    assert len(legacy) == 1
    assert legacy[0]["kind"] == "openai"
    assert [use["setting"] for use in legacy[0]["uses"]] == ["OPENAI_API_KEY"]


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
    # "no_readable_keys" + sent == [] on their own would also hold for empty settings
    # or for a row dropped by the name filter, so they prove that nothing was fetched,
    # not why. The snapshot pins the cause: this row parsed and surfaced as an
    # OpenAI-compatible credential attributed to OUROBOROS_CONNECTIONS, reported
    # no_metadata_endpoint because its non-canonical base names no recognized issuer,
    # so its key is never bound to a read.
    assert sent == []
    unverified = [row for row in response["snapshot"]["rows"]
                  if row["status"]["code"] == "no_metadata_endpoint"]
    assert len(unverified) == 1
    assert [use["setting"] for use in unverified[0]["uses"]] == [CONNECTIONS]


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


def test_two_named_connections_each_send_only_their_own_key(sent):
    response = refresh({CONNECTIONS: connections(
        {"kind": "openai-compatible", "name": "oai",
         "api_key": OPENAI_ROW_KEY, "base_url": OPENAI_BASE},
        {"kind": "openai-compatible", "name": "router",
         "api_key": ROUTER_ROW_KEY, "base_url": OPENROUTER_BASE})})
    assert response["outcome"]["reason"] == "finished"
    # Both bases are canonical, so each row binds its own key to its own issuer and
    # both are read. The exact (URL, Authorization) pairs are asserted per host: a
    # single named row cannot distinguish this from an inventory that reuses one
    # row's api_key for every named row, which would carry the first row's sentinel
    # to the other host as well.
    openai_pairs = {pair for pair in sent if pair[0].startswith("https://api.openai.com/")}
    router_pairs = {pair for pair in sent if pair[0].startswith("https://openrouter.ai/")}
    assert openai_pairs == {(url, "Bearer " + OPENAI_ROW_KEY) for url in OPENAI_URLS}
    assert router_pairs == {(url, "Bearer " + ROUTER_ROW_KEY) for url in OPENROUTER_URLS}
    assert len(sent) == 4


def test_keyless_named_openrouter_row_never_borrows_the_openai_key(sent):
    response = refresh({"OPENAI_API_KEY": OPENAI_KEY,
                        CONNECTIONS: connections(
                            {"kind": "openai-compatible", "name": "router",
                             "base_url": OPENROUTER_BASE})})
    assert response["outcome"]["reason"] == "finished"
    # The named row carries no api_key, so it surfaces keyless and is never read,
    # while the flat OpenAI key keeps serving only the built-in OpenAI pair. A named
    # row that fell back to the flat OPENAI_API_KEY would additionally read both
    # OpenRouter endpoints with the OpenAI sentinel.
    assert {url for url, _ in sent} == OPENAI_URLS
    assert {auth for _, auth in sent} == {"Bearer " + OPENAI_KEY}
    assert len(sent) == 2
    # Positive evidence that the row was seen and deliberately not read, rather than
    # dropped: it appears in the snapshot as keyless, attributed to the connection.
    keyless = [row for row in response["snapshot"]["rows"]
               if row["status"]["code"] == "keyless"]
    assert len(keyless) == 1
    assert [use["setting"] for use in keyless[0]["uses"]] == [CONNECTIONS]
