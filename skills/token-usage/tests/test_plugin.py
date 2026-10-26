"""Strict PluginAPI 2 contract and real Starlette route tests; not live host QA."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import plugin
except ModuleNotFoundError as exc:
    if exc.name != "starlette":
        raise
    plugin = None
from accounting import export_snapshot
from oracle import assert_subset_equal, physical_summary, verify_export

QueryError = plugin.QueryError if plugin else ValueError


class DependencyContractTests(unittest.TestCase):
    def test_missing_starlette_is_an_explicit_import_failure(self):
        script = """
import importlib.abc
import importlib.util
import pathlib
import sys
class BlockStarlette(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'starlette' or fullname.startswith('starlette.'):
            raise ModuleNotFoundError("No module named 'starlette'", name='starlette')
sys.meta_path.insert(0, BlockStarlette())
location = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('requires_starlette', location)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(ROOT / "plugin.py")],
                                cwd=ROOT, capture_output=True, text=True, timeout=5, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No module named 'starlette'", result.stderr)


class OwnedTemporaryCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-plugin-")
        self.addCleanup(self.temporary.cleanup)
        # macOS temp folders sit behind /var -> /private/var; preferences refuse symlinked parents.
        self.root = Path(self.temporary.name).resolve()


@unittest.skipIf(plugin is None, "Starlette is unavailable; strict plugin import requires host dependency")
class PreferencesTests(OwnedTemporaryCase):
    def test_missing_preferences_are_defaults_without_writes(self):
        prefs = plugin.Preferences(self.root / "state")
        self.assertEqual(prefs.read(), plugin.PREF_DEFAULTS)
        self.assertFalse((self.root / "state").exists())

    def test_valid_preferences_round_trip_and_survive_a_new_instance(self):
        prefs = plugin.Preferences(self.root / "state")
        expected = prefs.write({"period": "custom", "custom_start": "2026-09-09", "custom_end": "2026-09-10", "model": "native-test", "scope": "descendants", "task": "root-a"})
        self.assertEqual(plugin.Preferences(self.root / "state").read(), expected)
        self.assertEqual(prefs.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(prefs.directory.glob(".preferences-*.tmp")), [])

    def test_validation_rejects_unsupported_values_and_never_writes_them(self):
        prefs = plugin.Preferences(self.root / "state")
        invalid = [None, [], {"unknown": "x"}, {"period": "forever"}, {"scope": "full-tree"}, {"model": True}, {"model": "x" * 257}, {"custom_start": "2026-02-30"}, {"custom_start": "2026-9-1"}, {"custom_start": "2026-09-11", "custom_end": "2026-09-10"}]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(QueryError):
                    prefs.write(value)
        self.assertFalse(prefs.directory.exists())

    def test_corrupt_and_oversize_preferences_are_visible_errors(self):
        prefs = plugin.Preferences(self.root / "state")
        prefs.directory.mkdir(parents=True)
        prefs.path.write_bytes(b"{")
        with self.assertRaises(json.JSONDecodeError):
            prefs.read()
        prefs.path.write_bytes(b" " * (plugin.MAX_PREFERENCES_BYTES + 1))
        with self.assertRaises(ValueError):
            prefs.read()

    def test_file_symlink_is_rejected_and_target_untouched(self):
        prefs = plugin.Preferences(self.root / "state")
        prefs.directory.mkdir(parents=True)
        target = self.root / "numeric-marker.json"
        target.write_text('{"marker":7}', encoding="utf-8")
        prefs.path.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            prefs.read()
        with self.assertRaises(ValueError):
            prefs.write({"period": "7d"})
        self.assertEqual(target.read_text(encoding="utf-8"), '{"marker":7}')

    def test_directory_symlink_is_rejected(self):
        target = self.root / "actual-state"
        target.mkdir()
        link = self.root / "linked-state"
        link.symlink_to(target, target_is_directory=True)
        prefs = plugin.Preferences(link)
        with self.assertRaises(ValueError):
            prefs.read()
        with self.assertRaises(ValueError):
            prefs.write({"period": "7d"})
        self.assertEqual(list(target.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO metadata test requires POSIX")
    def test_fifo_is_rejected_before_open_so_worker_cannot_block(self):
        prefs = plugin.Preferences(self.root / "state")
        prefs.directory.mkdir(parents=True)
        os.mkfifo(prefs.path, 0o600)
        # A future regression fails immediately instead of hanging this test.
        with patch.object(plugin.os, "open", side_effect=AssertionError("Must reject FIFO before opening")):
            with self.assertRaises(ValueError):
                prefs.read()

    def test_regular_preference_read_uses_nonblocking_no_follow_flags(self):
        prefs = plugin.Preferences(self.root / "state")
        prefs.write({"period": "7d"})
        original_open = os.open
        recorded = []
        def tracked_open(path, flags, *args, **kwargs):
            recorded.append(flags)
            return original_open(path, flags, *args, **kwargs)
        with patch.object(plugin.os, "open", tracked_open):
            self.assertEqual(prefs.read()["period"], "7d")
        self.assertEqual(len(recorded), 1)
        for flag_name in ("O_NOFOLLOW", "O_NONBLOCK"):
            if hasattr(os, flag_name):
                self.assertTrue(recorded[0] & getattr(os, flag_name))

    def test_parallel_reads_and_writes_always_return_one_valid_document(self):
        prefs = plugin.Preferences(self.root / "state")
        def operation(index):
            prefs.write({"model": f"model-{index}", "project": f"project-{index}"})
            value = prefs.read()
            self.assertEqual(value["model"].split("-")[-1], value["project"].split("-")[-1])
            self.assertEqual(plugin.validate_preferences(value), value)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(operation, range(32)))
        self.assertEqual(list(prefs.directory.glob(".preferences-*.tmp")), [])


class FakeAPI:
    def __init__(self, root):
        self.root = root
        self.routes = {}
        self.cleanup = []
        self.tabs = {}
    def get_runtime_info(self):
        return {"data_dir": str(self.root / "data"), "state_dir": str(self.root / "state")}
    def register_route(self, path, handler, *, methods=("GET",)):
        if path in self.routes:
            raise AssertionError("Host disallows duplicate route paths")
        if path not in ("data", "export", "preferences", "chip"):
            raise AssertionError("Only own relative routes are allowed")
        expected = ["GET", "PUT"] if path == "preferences" else ["GET"]
        if methods != expected:
            raise AssertionError("Unexpected route methods")
        self.routes[path] = (handler, methods)
    def register_ui_tab(self, tab_id, title, *, icon="extension", render=None):
        if tab_id in self.tabs:
            raise AssertionError("Duplicate UI tab")
        self.tabs[tab_id] = {"title": title, "icon": icon, "render": render}
    def on_unload(self, callback):
        self.cleanup.append(callback)


class FakeIndex:
    def __init__(self, root, stop_event=None):
        self.stop_event = stop_event
        self.calls = 0
        self.refresh_thread_ids = []
        self.snapshot_thread_ids = []
    def refresh(self):
        self.calls += 1
        self.refresh_thread_ids.append(threading.get_ident())
    def snapshot(self):
        self.snapshot_thread_ids.append(threading.get_ident())
        return {"rows": [], "snapshot_id": "fake-revision", "coverage": {"status": "complete", "issues": []}}


def request(method="GET", query="", body=b"", headers=()):
    from starlette.requests import Request
    chunks = [body[:100], body[100:]]
    async def receive():
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}
    return Request({"type": "http", "method": method, "path": "/", "query_string": query.encode(),
                    "headers": list(headers)}, receive)


def decoded(response):
    return json.loads(response.body)


def status_of(response):
    return response.status_code


@unittest.skipIf(plugin is None, "Starlette is unavailable; real host route integration cannot run")
class PluginContractTests(OwnedTemporaryCase):
    def register_fake(self):
        self.fake_api = FakeAPI(self.root)
        with patch.object(plugin, "UsageIndex", FakeIndex):
            self.service = plugin.register(self.fake_api)
        self.addCleanup(self.service.close)
        return self.fake_api, self.service

    def test_exact_host_registration_ui_cleanup_and_lazy_start(self):
        api, service = self.register_fake()
        self.assertEqual(set(api.routes), {"data", "export", "preferences", "chip"})
        self.assertEqual(api.tabs, {"observatory": {"title": "Token Observatory", "icon": "◉", "render": {
            "kind": "module", "entry": "widget.js", "appearance": "host", "height": 560, "span": 2, "start": "manual"}}})
        self.assertIs(service.index.stop_event, service._stop)
        self.assertEqual(service.index.calls, 0)
        self.assertFalse((self.root / "state").exists())
        self.assertEqual(len(api.cleanup), 1)
        self.assertIsNone(service._thread)
        api.cleanup[0]()
        self.assertTrue(service._closed)
        self.assertTrue(service._stop.is_set())

    def test_unload_cancels_busy_reader_without_blocking(self):
        entered = threading.Event()
        finished = threading.Event()
        class CancellableIndex(FakeIndex):
            def refresh(self):
                entered.set()
                self.stop_event.wait(2)
                finished.set()
        api = FakeAPI(self.root)
        with patch.object(plugin, "UsageIndex", CancellableIndex):
            service = plugin.register(api)
        self.addCleanup(service.close)
        asyncio.run(service.query({"period": "all"}))
        self.assertTrue(entered.wait(1))
        api.cleanup[0]()
        self.assertTrue(finished.wait(1))
        self.assertTrue(service._pool._shutdown)
        with self.assertRaises(RuntimeError):
            asyncio.run(service.query({"period": "all"}))

    def test_registration_failure_closes_service_and_propagates(self):
        api = FakeAPI(self.root)
        api.register_ui_tab = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("registration failed"))
        with patch.object(plugin, "UsageIndex", FakeIndex), patch.object(plugin.Observatory, "close", autospec=True) as close:
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                plugin.register(api)
            close.assert_called_once()

    def test_detached_loader_imports_own_siblings_without_payload_on_sys_path(self):
        script = """
import importlib.util
import pathlib
import sys
path = pathlib.Path(sys.argv[1]).resolve()
assert all(not item or pathlib.Path(item).resolve() != path.parent for item in sys.path)
spec = importlib.util.spec_from_file_location('detached_observatory', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.validate_preferences({'period': '7d'})['period'] == '7d'
assert module.UsageIndex.__name__ == 'UsageIndex'
assert callable(module.query_snapshot)
print('detached-ok')
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(ROOT / "plugin.py")], cwd=ROOT, capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "detached-ok")

    def test_no_adapter_or_setup_alias(self):
        self.assertFalse(hasattr(plugin, "setup"))
        self.assertFalse(hasattr(plugin, "_bind_route"))

    def test_runtime_metadata_must_provide_nonempty_owned_paths(self):
        for runtime in ({}, {"data_dir": "/x", "state_dir": ""}, {"data_dir": None, "state_dir": "/y"}):
            api = FakeAPI(self.root)
            api.get_runtime_info = lambda: runtime
            with self.assertRaisesRegex(RuntimeError, "get_runtime_info"):
                plugin.register(api)

    def test_query_validation_returns_400_and_does_not_disclose_paths(self):
        api, service = self.register_fake()
        result = asyncio.run(api.routes["data"][0](request(query="period=invalid")))
        self.assertEqual(status_of(result), 400)
        self.assertIn("error", decoded(result))
        self.assertNotIn(str(self.root), json.dumps(decoded(result)))

    def test_duplicate_parameters_are_rejected(self):
        with self.assertRaises(QueryError):
            plugin._request_params(request(query="period=all&period=7d"))

    def test_preferences_route_validates_stream_size_and_json(self):
        api, service = self.register_fake()
        async def scenario():
            handler = api.routes["preferences"][0]
            good = await handler(request("PUT", body=b'{"period":"7d","task":"root-a","view":"sessions"}'))
            self.assertEqual(status_of(good), 200)
            self.assertEqual(decoded(await handler(request()))["view"], "sessions")
            for body in (b"{", b" " * (plugin.MAX_PREFERENCES_BYTES + 1), b'{"root_dir":"/client/path"}', b'{"view":"bad"}', b"\xff"):
                self.assertEqual(status_of(await handler(request("PUT", body=body))), 400)
            for length in (b"9000", b"-1", b"invalid"):
                self.assertEqual(status_of(await handler(request("PUT", body=b"{}", headers=[(b"content-length", length)]))), 400)
            self.assertEqual(decoded(await handler(request()))["period"], "7d")
            self.assertEqual(status_of(await handler(request("POST"))), 405)
            self.assertIsNone(service._thread)
        asyncio.run(scenario())

    def test_real_starlette_asgi_routes_and_method_dispatch(self):
        from starlette.applications import Starlette
        from starlette.routing import Route
        api, service = self.register_fake()
        prefix = "/api/extensions/token-usage"
        app = Starlette(routes=[Route(prefix + "/" + path, handler, methods=methods)
                                for path, (handler, methods) in api.routes.items()])
        async def call(path, method="GET", body=b"", query=b""):
            sent = []
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}
            async def send(message):
                sent.append(message)
            await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                       "scheme": "http", "method": method, "path": prefix + "/" + path,
                       "raw_path": (prefix + "/" + path).encode(), "query_string": query, "root_path": "",
                       "headers": [], "server": ("localhost", 80), "client": ("localhost", 1234)}, receive, send)
            return sent[0], b"".join(item.get("body", b"") for item in sent[1:])
        async def scenario():
            start, body = await call("preferences", "PUT", b'{"period":"24h"}')
            self.assertEqual(start["status"], 200)
            self.assertEqual(json.loads(body)["period"], "24h")
            start, body = await call("preferences")
            self.assertEqual(json.loads(body)["period"], "24h")
            for path, method in (("data", "PUT"), ("export", "POST"), ("preferences", "POST")):
                self.assertEqual((await call(path, method))[0]["status"], 405)
            self.assertEqual((await call("data", query=b"period=invalid"))[0]["status"], 400)
            start, body = await call("export", query=b"period=all")
            self.assertEqual(start["status"], 200)
            self.assertIn((b"cache-control", b"no-store"), start["headers"])
            self.assertTrue(any(name == b"content-disposition" for name, _ in start["headers"]))
            self.assertIn("content_sha256", json.loads(body))
        asyncio.run(scenario())

    def test_background_refresh_failure_is_explicit_and_sanitized(self):
        class FailureIndex(FakeIndex):
            def refresh(self):
                raise OSError("SECRET-LIKE MARKER /not/a/source/path")
        with patch.object(plugin, "UsageIndex", FailureIndex):
            service = plugin.Observatory(self.root / "data", self.root / "state", refresh_seconds=60)
        self.addCleanup(service.close)
        service.start()
        self.assertTrue(service._ready.wait(1))
        response = asyncio.run(service.query({"period": "all"}))
        self.assertEqual(response["coverage"]["status"], "error")
        self.assertNotIn("SECRET-LIKE", json.dumps(response))
        self.assertIn("OSError", json.dumps(response["coverage"]))

    def test_initial_catchup_is_visible_while_background_worker_is_busy(self):
        release = threading.Event()
        entered = threading.Event()
        class BusyIndex(FakeIndex):
            def refresh(self):
                entered.set()
                release.wait(2)
        with patch.object(plugin, "UsageIndex", BusyIndex):
            service = plugin.Observatory(self.root / "data", self.root / "state", refresh_seconds=60)
        self.addCleanup(service.close)
        try:
            service.start()
            self.assertTrue(entered.wait(1))
            response = asyncio.run(asyncio.wait_for(service.query({"period": "all"}), timeout=0.5))
            self.assertEqual(response["coverage"]["status"], "loading")
            self.assertFalse(response["coverage"]["history_complete"])
        finally:
            release.set()

    def test_query_and_refresh_run_outside_main_event_loop_thread(self):
        api, service = self.register_fake()
        service.refresh_seconds = 60
        service.start()
        self.assertTrue(service._ready.wait(1))
        main_thread = threading.get_ident()
        called_threads = []
        original = plugin.query_snapshot
        def measured(snapshot, params):
            called_threads.append(threading.get_ident())
            time.sleep(0.05)
            return original(snapshot, params)
        async def scenario():
            ticks = []
            async def heartbeat():
                for _ in range(3):
                    await asyncio.sleep(0.01)
                    ticks.append(time.monotonic())
            response, _ = await asyncio.gather(service.query({"period": "all"}), heartbeat())
            self.assertEqual(response["coverage"]["status"], "complete")
            self.assertEqual(len(ticks), 3)
        with patch.object(plugin, "query_snapshot", measured):
            asyncio.run(scenario())
        self.assertTrue(called_threads)
        self.assertNotIn(main_thread, called_threads)
        self.assertNotIn(main_thread, service.index.refresh_thread_ids)
        self.assertNotIn(main_thread, service.index.snapshot_thread_ids)

    def test_data_and_export_json_response_encoding_runs_off_asgi_thread(self):
        from starlette.responses import JSONResponse
        api, service = self.register_fake()
        main_thread = threading.get_ident()
        encoded_threads = []
        class MeasuredResponse(JSONResponse):
            def render(self, content):
                encoded_threads.append(threading.get_ident())
                return super().render(content)
        async def scenario():
            for path in ("data", "export"):
                response = await api.routes[path][0](request(query="period=all"))
                self.assertEqual(response.status_code, 200)
                self.assertIn("summary", decoded(response))
        with patch.object(plugin, "JSONResponse", MeasuredResponse):
            asyncio.run(scenario())
        self.assertEqual(len(encoded_threads), 2)
        self.assertNotIn(main_thread, encoded_threads)

    def test_concurrent_queries_use_bounded_worker_pool_and_stop_is_idempotent(self):
        api, service = self.register_fake()
        service.refresh_seconds = 60
        active = 0
        maximum = 0
        guard = threading.Lock()
        original = plugin.query_snapshot
        def measured(snapshot, params):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.01)
                return original(snapshot, params)
            finally:
                with guard:
                    active -= 1
        async def scenario():
            responses = await asyncio.gather(*(service.query({"period": "all"}) for _ in range(12)))
            self.assertEqual(len(responses), 12)
            self.assertTrue(all(response["summary"]["physical_calls"] == 0 for response in responses))
        with patch.object(plugin, "query_snapshot", measured):
            asyncio.run(scenario())
        self.assertLessEqual(maximum, 2)
        self.assertGreater(maximum, 0)
        service.close()
        service.close()
        with self.assertRaises(RuntimeError):
            asyncio.run(service.query({"period": "all"}))

    def test_frozen_export_checksum_and_independent_numeric_oracle(self):
        fixture = json.loads((ROOT / "fixtures" / "numeric_snapshot.json").read_text(encoding="utf-8"))
        exported = export_snapshot(fixture, {"period": "all", "page_size": 1})
        verify_export(exported)
        # Export is the complete filtered selection, not the current detail page.
        self.assertEqual(len(exported["rows"]), len(fixture["rows"]))
        self.assertTrue(exported["snapshot_id"])
        self.assertEqual(exported["query"]["page_size"], 1)
        checksum = exported.pop("content_sha256")
        computed = hashlib.sha256(json.dumps(exported, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        self.assertEqual(checksum, computed)
        assert_subset_equal(physical_summary(exported["rows"]), exported["summary"])
        expected_unknown = physical_summary([row for row in exported["rows"] if row["attempt_id"] == "undated"])
        self.assertIsNone(expected_unknown["metrics"]["prompt_tokens"]["value"])
        self.assertEqual(expected_unknown["metrics"]["prompt_tokens"]["missing"], 1)

    def test_export_oracle_rejects_tampering_wrong_selection_and_null_coercion(self):
        fixture = json.loads((ROOT / "fixtures" / "numeric_snapshot.json").read_text(encoding="utf-8"))
        exported = export_snapshot(fixture, {"period": "all", "project": "__unassigned__"})
        verify_export(exported)
        def rehash(document):
            payload = {key: value for key, value in document.items() if key != "content_sha256"}
            document["content_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        tampered = copy.deepcopy(exported)
        tampered["summary"]["physical_calls"] += 1
        with self.assertRaisesRegex(AssertionError, "checksum"):
            verify_export(tampered)
        wrong_selection = copy.deepcopy(exported)
        wrong_selection["query"]["model"] = "nonexistent-model"
        rehash(wrong_selection)
        with self.assertRaisesRegex(AssertionError, "selected model"):
            verify_export(wrong_selection)
        zero_instead_of_null = copy.deepcopy(exported)
        zero_instead_of_null["summary"]["metrics"]["prompt_tokens"]["value"] = 0
        rehash(zero_instead_of_null)
        with self.assertRaisesRegex(AssertionError, "unknown/null"):
            verify_export(zero_instead_of_null)
        wrong_missing = copy.deepcopy(exported)
        wrong_missing["summary"]["metrics"]["prompt_tokens"]["missing"] = 0
        rehash(wrong_missing)
        with self.assertRaisesRegex(AssertionError, "missing"):
            verify_export(wrong_missing)

    def test_independent_money_comparison_tolerance_is_one_microdollar(self):
        assert_subset_equal({"value": 1.46}, {"value": 1.4600005})
        with self.assertRaises(AssertionError):
            assert_subset_equal({"value": 1.46}, {"value": 1.460002})
        with self.assertRaises(AssertionError):
            assert_subset_equal({"value": None}, {"value": 0})
        with self.assertRaises(AssertionError):
            assert_subset_equal({"value": 2**53 + 1}, {"value": 2**53})


if __name__ == "__main__":
    unittest.main()
