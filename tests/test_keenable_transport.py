"""Exercise the optional MCP session contract through the shipped client."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
import urllib.error

from skills.keenable import keenable_client as client
from skills.keenable import plugin


ROOT = Path(__file__).resolve().parents[1]
# Actual 2026-10-04 keyless InitializeResult, without request/network identifiers.
INITIALIZE = json.loads((ROOT / "tests/fixtures/keenable/initialize-sessionless.json")
                        .read_text(encoding="utf-8"))


class MCPTransport:
    """In-process MCP peer; tool text is synthetic, never copied from a website."""

    def __init__(self, session_id=None):
        self.session_id = session_id
        self.initialize_body = copy.deepcopy(INITIALIZE)
        self.initialize_status = 200
        self.notify_response = (202, {}, "")
        self.tool_responses = []
        self.calls = []

    @property
    def methods(self):
        return [call["payload"]["method"] for call in self.calls]

    def __call__(self, url, body, headers, timeout):
        payload = json.loads(body.decode("utf-8"))
        self.calls.append({"payload": payload, "headers": dict(headers), "timeout": timeout})
        method = payload["method"]
        if method == "initialize":
            # Mixed case covers the same normalization as the urllib transport.
            reply_headers = {"Mcp-Session-Id": self.session_id} if self.session_id else {}
            text = self.initialize_body
            if not isinstance(text, str):
                text = json.dumps(text)
            return self.initialize_status, reply_headers, text
        if method == "notifications/initialized":
            return self.notify_response
        if self.tool_responses:
            response = self.tool_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        text = "Title: Test source\nURL: https://example.org/\n"
        if payload["params"]["name"] == "search_web_pages":
            text += "Published: 2026-10-01\nAcquired: 2026-10-04\nSnippets: Synthetic search result."
        else:
            text += "\nSynthetic page content."
        return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 2,
                                   "result": {"content": [{"type": "text", "text": text}]}})


class KeenableTransportTests(unittest.TestCase):
    def setUp(self):
        client.reset_session_cache()
        self.addCleanup(client.reset_session_cache)

    def call(self, transport, key=""):
        return client.search({"query": "Synthetic test"}, key=key, transport=transport)

    def test_stateful_and_sessionless_search_fetch_share_initialization(self):
        for session_id in (None, "issued-session"):
            with self.subTest(session_id=session_id):
                client.reset_session_cache()
                peer = MCPTransport(session_id)
                search = self.call(peer)
                fetch = client.fetch({"url": "https://example.org/"}, transport=peer)
                self.assertTrue(search["ok"], search)
                self.assertEqual(search["results"][0]["url"], "https://example.org/")
                self.assertTrue(fetch["ok"], fetch)
                self.assertIn("Synthetic page content.", fetch["content"])
                self.assertEqual(peer.methods, ["initialize", "notifications/initialized",
                                                "tools/call", "tools/call"])
                self.assertNotIn("MCP-Protocol-Version", peer.calls[0]["headers"])
                self.assertNotIn("Mcp-Session-Id", peer.calls[0]["headers"])
                for call in peer.calls[1:]:
                    self.assertEqual(call["headers"]["MCP-Protocol-Version"], client.PROTOCOL_VERSION)
                    self.assertEqual(call["headers"].get("Mcp-Session-Id"), session_id)

    def test_key_change_reinitializes_both_modes_without_auth_fallback(self):
        for session_id in (None, "issued-session"):
            with self.subTest(session_id=session_id):
                client.reset_session_cache()
                peer = MCPTransport(session_id)
                for key in ("", "synthetic-key-a", "synthetic-key-a", "synthetic-key-b", ""):
                    before = len(peer.calls)
                    self.assertTrue(self.call(peer, key)["ok"])
                    for call in peer.calls[before:]:
                        self.assertEqual(call["headers"].get("X-API-Key", ""), key)
                self.assertEqual(peer.methods.count("initialize"), 4)
                self.assertEqual(peer.methods.count("tools/call"), 5)

    def test_reset_and_plugin_unload_forget_id_version_and_key(self):
        for session_id in (None, "issued-session"):
            for reset in (client.reset_session_cache, plugin._on_unload):
                with self.subTest(session_id=session_id, reset=reset.__name__):
                    client.reset_session_cache()
                    peer = MCPTransport(session_id)
                    self.assertTrue(self.call(peer, "synthetic-key")["ok"])
                    reset()
                    self.assertTrue(all(value is None for value in client._SESSION.values()))
                    self.assertTrue(self.call(peer, "synthetic-key")["ok"])
                    self.assertEqual(peer.methods.count("initialize"), 2)

    def test_call_uses_its_session_snapshot_after_shared_cache_changes(self):
        peer = MCPTransport("issued-session")
        ensure = client._ensure_session

        def replace_cache(*args, **kwargs):
            snapshot = ensure(*args, **kwargs)
            client.reset_session_cache()
            return snapshot

        with patch.object(client, "_ensure_session", side_effect=replace_cache):
            self.assertTrue(self.call(peer)["ok"])
        self.assertEqual(peer.calls[-1]["headers"]["Mcp-Session-Id"], "issued-session")
        self.assertEqual(peer.calls[-1]["headers"]["MCP-Protocol-Version"], client.PROTOCOL_VERSION)

    def test_expired_stateful_session_recovers_once(self):
        for next_session in (None, "new-session"):
            with self.subTest(next_session=next_session):
                client.reset_session_cache()
                peer = MCPTransport("issued-session")
                peer.tool_responses = [(404, {}, "Session expired")]

                def expire(*args):
                    response = peer(*args)
                    if response[0] == 404:
                        peer.session_id = next_session
                    return response

                self.assertTrue(self.call(expire)["ok"])
                self.assertEqual(peer.methods, ["initialize", "notifications/initialized", "tools/call"] * 2)
                self.assertEqual(peer.calls[2]["headers"]["Mcp-Session-Id"], "issued-session")
                self.assertNotIn("Mcp-Session-Id", peer.calls[3]["headers"])
                self.assertEqual(peer.calls[-1]["headers"].get("Mcp-Session-Id"), next_session)

    def test_repeated_session_loss_stops_after_one_retry(self):
        peer = MCPTransport("issued-session")
        peer.tool_responses = [(404, {}, "Session expired")] * 2
        response = self.call(peer)
        self.assertEqual(response["error"], "keenable_protocol_error")
        self.assertEqual(peer.methods.count("initialize"), 2)
        self.assertEqual(peer.methods.count("tools/call"), 2)

    def test_stateless_404_and_jsonrpc_errors_do_not_invent_session_recovery(self):
        error = json.dumps({"jsonrpc": "2.0", "id": 2,
                            "error": {"code": -32001, "message": "Not found"}})
        for status, body in ((404, "Endpoint not found"), (200, error)):
            with self.subTest(status=status):
                client.reset_session_cache()
                peer = MCPTransport()
                peer.tool_responses = [(status, {}, body)]
                response = self.call(peer)
                self.assertEqual(response["error"], "keenable_protocol_error")
                self.assertEqual(response["error_class"], "not_read")
                self.assertEqual(peer.methods.count("initialize"), 1)
                self.assertEqual(peer.methods.count("tools/call"), 1)

    def test_stateful_jsonrpc_session_loss_recovers_once(self):
        peer = MCPTransport("issued-session")
        peer.tool_responses = [(200, {}, json.dumps({"jsonrpc": "2.0", "id": 2,
                               "error": {"code": -32001, "message": "Session expired"}}))]
        self.assertTrue(self.call(peer)["ok"])
        self.assertEqual(peer.methods.count("initialize"), 2)

    def test_invalid_initialize_is_not_cached_or_used_for_a_tool(self):
        invalid = ["", "{", "not JSON", "[]", {}, {**INITIALIZE, "result": {}},
                   {**INITIALIZE, "result": None}, {**INITIALIZE, "id": 2},
                   {**INITIALIZE, "id": True}, {**INITIALIZE, "jsonrpc": "1.0"},
                   {"jsonrpc": "2.0", "id": 1,
                    "error": {"code": -32001, "message": "Initialization failed"}}]
        for field, value in (("protocolVersion", None), ("protocolVersion", "1900-01-01"),
                             ("capabilities", None), ("serverInfo", {})):
            invalid.append({**INITIALIZE, "result": {**INITIALIZE["result"], field: value}})
        for body in invalid:
            with self.subTest(body=body):
                client.reset_session_cache()
                peer = MCPTransport()
                peer.initialize_body = body
                response = self.call(peer)
                self.assertEqual(response["error"], "keenable_protocol_error")
                self.assertEqual(peer.methods, ["initialize"])
                peer.initialize_body = copy.deepcopy(INITIALIZE)
                self.assertTrue(self.call(peer)["ok"])
                self.assertEqual(peer.methods.count("initialize"), 2)

    def test_http_failures_remain_typed_without_retry_or_keyless_fallback(self):
        cases = [(400, "keenable_protocol_error"), (401, "keenable_auth_invalid_key"),
                 (429, "keenable_rate_limited"), (503, "keenable_server_error")]
        for stage in ("initialize", "tools/call"):
            for status, expected in cases:
                with self.subTest(stage=stage, status=status):
                    client.reset_session_cache()
                    peer = MCPTransport("issued-session")
                    if stage == "initialize":
                        peer.initialize_status = status
                    else:
                        peer.tool_responses = [(status, {}, "Synthetic vendor failure")]
                    response = self.call(peer, "synthetic-key")
                    self.assertEqual(response["error"], expected)
                    self.assertEqual(peer.methods.count("initialize"), 1)
                    self.assertEqual(peer.methods.count("tools/call"), int(stage == "tools/call"))
                    self.assertTrue(all(call["headers"].get("X-API-Key") == "synthetic-key"
                                        for call in peer.calls))

    def test_initialize_404_is_not_a_lost_session(self):
        peer = MCPTransport()
        peer.initialize_status = 404
        self.assertEqual(self.call(peer)["error"], "keenable_protocol_error")
        self.assertEqual(peer.methods, ["initialize"])

    def test_keyless_auth_failure_is_distinct(self):
        peer = MCPTransport()
        peer.initialize_status = 401
        self.assertEqual(self.call(peer)["error"], "keenable_auth_required")
        self.assertEqual(peer.methods, ["initialize"])

    def test_transport_and_tool_failures_keep_existing_taxonomy(self):
        cases = [(TimeoutError(), "keenable_timeout"),
                 (urllib.error.URLError("Synthetic offline"), "keenable_transport_error"),
                 ((200, {}, json.dumps({"result": {"isError": True, "content": [
                     {"type": "text", "text": "Synthetic extraction failure"}]}})), "keenable_tool_error"),
                 ((200, {}, "{}"), "keenable_protocol_error")]
        for failure, expected in cases:
            with self.subTest(expected=expected):
                client.reset_session_cache()
                peer = MCPTransport()
                peer.tool_responses = [failure]
                self.assertEqual(self.call(peer)["error"], expected)
                self.assertEqual(peer.methods.count("tools/call"), 1)

    def test_notification_failure_still_leaves_tools_usable(self):
        peer = MCPTransport()
        peer.notify_response = (503, {}, "Synthetic notification failure")
        self.assertTrue(self.call(peer)["ok"])
        self.assertTrue(self.call(peer)["ok"])
        self.assertEqual(peer.methods.count("initialize"), 1)

    def test_recovery_spends_the_original_operation_budget(self):
        peer = MCPTransport("issued-session")
        peer.tool_responses = [(404, {}, "Session expired")]
        clock = Mock(return_value=100.0)
        durations = iter((20, 5, 15, 15, 5, 1))

        def timed_transport(*args):
            response = peer(*args)
            clock.return_value += next(durations)
            return response

        with patch.object(client.time, "monotonic", clock):
            self.assertTrue(self.call(timed_transport)["ok"])
        self.assertEqual([call["timeout"] for call in peer.calls], [45, 10, 45, 40, 10, 20])

    def test_exhausted_budget_does_not_start_another_request(self):
        peer = MCPTransport("issued-session")
        peer.tool_responses = [(404, {}, "Session expired")]
        clock = Mock(return_value=100.0)

        def slow_transport(*args):
            response = peer(*args)
            if peer.methods[-1] == "tools/call":
                clock.return_value = 179.0
            return response

        with patch.object(client.time, "monotonic", clock):
            self.assertEqual(self.call(slow_transport)["error"], "keenable_timeout")
        self.assertEqual(peer.methods, ["initialize", "notifications/initialized", "tools/call"])

    def test_shipped_envelope_verifier(self):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "skills/keenable/verify_envelope_bounds.py")],
                                capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ALL ENVELOPE BOUNDS HOLD", result.stdout)


if __name__ == "__main__":
    unittest.main()
