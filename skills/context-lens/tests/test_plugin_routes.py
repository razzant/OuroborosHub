"""Route-level tests against a fake PluginAPI and a fake request object.

The gateway (``ouroboros/gateway/extensions.py``) calls a registered handler as
``handler(request)`` on a worker thread and turns a returned mapping into a
JSON response, so a stand-in with ``query_params`` reproduces the real calling
convention without importing the host. When Starlette happens to be installed,
the same handlers are additionally exercised through a real ``Request``.
"""

from __future__ import annotations

import importlib.util
import datetime as _dt
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load("context_lens_plugin_under_test", "plugin.py")
lens_core = plugin.lens_core


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, **params) -> None:
        self.query_params = dict(params)
        self.method = "GET"


class FakePluginAPI:
    """Records registrations and enforces the permissions this skill declares."""

    DECLARED_PERMISSIONS = {"route", "widget"}

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.routes = {}
        self.tabs = {}
        self.tools = []
        self.ws_handlers = []
        self.unload = []
        self.logs = []

    def log(self, level, message, *args, **kwargs):
        # The host log needs no declared permission, so this records rather than
        # refuses — but it still goes through _require-free code only, which is
        # what makes an undeclared permission the ONLY thing this double fails on.
        self.logs.append((str(level), str(message)))

    def get_runtime_info(self):
        return {"runtime_mode": "advanced", "data_dir": self.data_dir,
                "execution_mode": "in_process", "capabilities": []}

    def _require(self, permission: str) -> None:
        if permission not in self.DECLARED_PERMISSIONS:
            raise AssertionError("skill used undeclared permission %r" % permission)

    def register_route(self, path, handler, *, methods=("GET",)):
        self._require("route")
        assert "/" not in path.strip("/") or path.count("/") == 0, path
        self.routes[path] = {"handler": handler, "methods": tuple(methods)}

    def register_ui_tab(self, tab_id, title, *, icon="extension", render=None):
        self._require("widget")
        self.tabs[tab_id] = {"title": title, "icon": icon, "render": dict(render or {})}

    def register_tool(self, *args, **kwargs):   # pragma: no cover - must not happen
        self._require("tool")

    def register_ws_handler(self, *args, **kwargs):  # pragma: no cover
        self._require("ws_handler")

    def get_settings(self, keys):               # pragma: no cover
        self._require("read_settings")

    def on_unload(self, callback):
        self.unload.append(callback)


def write_ledger(root: str, rows) -> str:
    state_dir = os.path.join(root, "state")
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "usage_attempts.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for index, row in enumerate(rows, start=1):
            handle.write(json.dumps(dict(row, seq=index), sort_keys=True) + "\n")
    return path


def chain(attempt_id, *, prompt_tokens=None, task_id="task-1", model="vendor/model-a",
          category="task", final="settled", physical_context=None):
    common = {"kind": "attempt", "attempt_id": attempt_id, "model": model,
              "provider": "openrouter", "category": category, "source": "llm",
              "task_id": task_id, "root_task_id": task_id, "parent_task_id": ""}
    if physical_context is not None:
        common["physical_context"] = physical_context
    return [
        dict(common, state="reserved", ts="2026-09-07T10:00:00+00:00"),
        dict(common, state="dispatched", ts="2026-09-07T10:00:00+00:00"),
        dict(common, state=final, ts="2026-09-07T10:00:04+00:00",
             **({"prompt_tokens": prompt_tokens} if final == "settled" else {})),
    ]


# ---------------------------------------------------------------------------


class RouteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="context-lens-routes-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.api = FakePluginAPI(self.root)

    def register(self) -> FakePluginAPI:
        plugin.register(self.api)
        return self.api


class TestRegistration(RouteTestCase):
    def test_registers_only_its_own_namespace_surfaces(self) -> None:
        api = self.register()
        self.assertEqual(sorted(api.routes), ["data", "trajectory"])
        for spec in api.routes.values():
            self.assertEqual(spec["methods"], ("GET",))
        self.assertEqual(api.tools, [])
        self.assertEqual(api.ws_handlers, [])

    def test_registers_the_module_widget_with_a_fixed_frame_height(self) -> None:
        """A declared `height` is what keeps the host's auto-height observer off.

        `web/modules/widget_module.js` mounts the resize bridge only when
        `render.height` is undefined or null; that bridge is the ResizeObserver
        whose height round trip WebKit reported as an undelivered-notification
        loop. So the fixed height is load-bearing, not cosmetic, and it must stay
        inside the host's own frame bounds.
        """
        api = self.register()
        render = api.tabs["lens"]["render"]
        self.assertEqual(render["kind"], "module")
        self.assertEqual(render["entry"], "widget.js")
        self.assertEqual(render["start"], "auto")
        self.assertEqual(render["height"], 760)
        self.assertIsNotNone(render["height"])
        self.assertNotIn("max_height", render)      # a fixed box, not a ceiling
        self.assertLessEqual(render["height"], 8192)
        self.assertGreaterEqual(render["height"], 320)
        self.assertTrue((_ROOT / render["entry"]).is_file())

    def test_the_widget_owns_the_scrolling_the_fixed_height_implies(self) -> None:
        # With no auto-height bridge the frame cannot grow, so the document must
        # scroll itself and no inner pane may compete with that scroll.
        widget = (_ROOT / "widget.js").read_text(encoding="utf-8")
        self.assertIn("body{min-height:100%;overflow-y:auto;", widget)
        self.assertNotIn("max-height:268px;overflow-y:auto", widget)
        self.assertEqual(widget.count("overflow-y:auto"), 1)

    def test_asks_the_host_for_a_wide_card_through_the_declared_span(self) -> None:
        # `span` is the host's own width contract for a widget card
        # (extension_surface_names._widget_span_from_render normalizes it to 1
        # or 2), so the chart is asked for two columns rather than being made
        # wide by the widget's own CSS.
        api = self.register()
        render = api.tabs["lens"]["render"]
        self.assertEqual(render["span"], 2)
        self.assertIn(render["span"], (1, 2))

    def test_manifest_and_registration_agree(self) -> None:
        api = self.register()
        render = api.tabs["lens"]["render"]
        text = (_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("tab_id: lens", text)
        self.assertIn("entry: widget.js", text)
        self.assertIn("height: %d" % render["height"], text)
        self.assertIn("span: %d" % render["span"], text)
        self.assertIn("start: auto", text)
        self.assertNotIn("max_height:", text)

    def test_manifest_version_matches_the_documented_release(self) -> None:
        text = (_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("version: 1.1.2", text)

    def test_unload_callback_releases_the_reader(self) -> None:
        api = self.register()
        write_ledger(self.root, chain("a1", prompt_tokens=1000))
        plugin.route_data(FakeRequest())
        self.assertTrue(api.unload)
        for callback in api.unload:
            callback()
        self.assertIsNone(plugin._window)


class TestDataRoute(RouteTestCase):
    def test_happy_path(self) -> None:
        self.register()
        write_ledger(self.root, chain("a1", prompt_tokens=120000)
                     + chain("a2", prompt_tokens=8000, task_id="task-2"))
        payload = plugin.route_data(FakeRequest())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["available"])
        self.assertEqual(len(payload["points"]), 2)
        self.assertEqual(payload["counters"]["measured"], 2)
        self.assertIn("models", payload["facets"])
        self.assertIn("lines_read", payload["window"])

    def test_limit_parameter_is_bounded_and_forgiving(self) -> None:
        self.register()
        rows = []
        for index in range(20):
            rows.extend(chain("a%d" % index, prompt_tokens=1000 + index))
        write_ledger(self.root, rows)
        self.assertEqual(len(plugin.route_data(FakeRequest(limit="5"))["points"]), 5)
        self.assertEqual(len(plugin.route_data(FakeRequest(limit="not a number"))["points"]), 20)
        self.assertEqual(len(plugin.route_data(FakeRequest(limit="-4"))["points"]), 1)
        self.assertLessEqual(
            len(plugin.route_data(FakeRequest(limit="999999"))["points"]),
            lens_core.MAX_RECORDS,
        )

    def test_refresh_parameter_forces_a_cold_read(self) -> None:
        self.register()
        write_ledger(self.root, chain("a1", prompt_tokens=1000))
        plugin.route_data(FakeRequest())
        payload = plugin.route_data(FakeRequest(refresh="1"))
        self.assertEqual(payload["counters"]["physical_attempts"], 1)
        self.assertEqual(payload["window"]["lines_read"], 3)   # re-read, not doubled

    def test_missing_ledger_is_an_explained_unavailability(self) -> None:
        self.register()
        payload = plugin.route_data(FakeRequest())
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "no_ledger")
        self.assertIn("first model request", payload["message"])

    def test_no_data_dir_is_reported_without_a_crash(self) -> None:
        api = FakePluginAPI("")
        plugin.register(api)
        payload = plugin.route_data(FakeRequest())
        self.assertEqual(payload["reason"], "no_data_dir")

    def test_a_failing_runtime_info_is_named_not_guessed_at(self) -> None:
        """A host that cannot answer is NOT the same as a host with no data dir."""
        secret = os.path.join(self.root, "state", "usage_attempts.jsonl")

        class Broken(FakePluginAPI):
            def get_runtime_info(self):
                raise RuntimeError("could not stat " + secret + " for the runtime")

        api = Broken(self.root)
        plugin.register(api)

        payload = plugin.route_data(FakeRequest())
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "runtime_info_unavailable")
        self.assertNotEqual(payload["reason"], "no_data_dir")
        self.assertEqual(plugin.route_trajectory(FakeRequest(task="t-abcdefabcdef"))["reason"],
                         "runtime_info_unavailable")

        # The owner is told, in the host log, and only the exception TYPE is said.
        self.assertEqual([level for level, _ in api.logs], ["warning"])
        line = api.logs[0][1]
        self.assertIn("RuntimeError", line)
        self.assertNotIn(secret, line)
        self.assertNotIn(self.root, line)
        self.assertNotIn("could not stat", line)

        # Nothing about the path or the exception reaches the browser.
        blob = json.dumps(payload)
        self.assertNotIn(self.root, blob)
        self.assertNotIn("usage_attempts", blob)
        self.assertNotIn("RuntimeError", blob)
        self.assertNotIn("Traceback", blob)

        # A later, working registration clears the flag rather than latching it.
        plugin.register(FakePluginAPI(self.root))
        self.assertEqual(plugin.route_data(FakeRequest())["reason"], "no_ledger")

    def test_errors_never_disclose_a_path_or_a_ledger_line(self) -> None:
        self.register()
        write_ledger(self.root, chain("a1", prompt_tokens=1000))
        os.chmod(os.path.join(self.root, "state", "usage_attempts.jsonl"), 0)
        self.addCleanup(os.chmod, os.path.join(self.root, "state", "usage_attempts.jsonl"), 0o600)
        payload = plugin.route_data(FakeRequest())
        blob = json.dumps(payload)
        if payload["ok"]:
            self.skipTest("this filesystem ignores mode 0 for the owner")
        self.assertEqual(payload["reason"], "ledger_unreadable")
        self.assertNotIn(self.root, blob)
        self.assertNotIn("usage_attempts", blob)
        self.assertNotIn("Traceback", blob)

    def test_response_is_json_serialisable(self) -> None:
        self.register()
        write_ledger(self.root, chain("a1", prompt_tokens=1000))
        json.dumps(plugin.route_data(FakeRequest()))

    def test_concurrent_requests_do_not_double_count(self) -> None:
        self.register()
        rows = []
        for index in range(30):
            rows.extend(chain("a%d" % index, prompt_tokens=1000 + index))
        write_ledger(self.root, rows)
        results = []
        errors = []

        def worker() -> None:
            try:
                results.append(plugin.route_data(FakeRequest()))
            except Exception as exc:                     # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        for payload in results:
            self.assertEqual(payload["counters"]["physical_attempts"], 30)
            self.assertEqual(payload["window"]["lines_read"], 90)


class TestHorizonParameter(RouteTestCase):
    """The route must normalize the horizon, apply it, and say what it applied.

    The route takes its own anchor from the clock, so these fixtures are dated
    relative to now rather than to a frozen literal — a fixed date would drift
    out of every span and stop testing the cut at all.
    """

    @staticmethod
    def _ago(minutes: float) -> str:
        moment = lens_core.now_ms() - int(minutes * 60000)
        return _dt.datetime.fromtimestamp(moment / 1000.0, _dt.timezone.utc).isoformat()

    def _stamp(self, rows, minutes: float):
        for row in rows:
            row["ts"] = self._ago(minutes)
        return rows

    def _write_two_ages(self) -> None:
        recent = self._stamp(chain("recent", prompt_tokens=1000), 5)
        old = self._stamp(
            chain("old", prompt_tokens=2000, model="vendor/model-old"), 30 * 24 * 60)
        write_ledger(self.root, recent + old)

    def test_the_default_is_available_and_nothing_is_cut(self) -> None:
        self.register()
        self._write_two_ages()
        payload = plugin.route_data(FakeRequest())
        self.assertEqual(payload["horizon"]["selected"], "available")
        self.assertIsNone(payload["horizon"]["cutoff_ms"])
        self.assertEqual(len(payload["points"]), 2)

    def test_a_bounded_horizon_cuts_the_answer_and_states_its_anchor(self) -> None:
        self.register()
        self._write_two_ages()
        payload = plugin.route_data(FakeRequest(horizon="7d"))
        self.assertEqual(payload["horizon"]["selected"], "7d")
        self.assertEqual(payload["horizon"]["span_ms"], 7 * 24 * 3600 * 1000)
        self.assertEqual(payload["horizon"]["cutoff_ms"],
                         payload["horizon"]["now_ms"] - 7 * 24 * 3600 * 1000)
        self.assertEqual(payload["horizon"]["excluded_older_than_cutoff"], 1)
        self.assertEqual(len(payload["points"]), 1)
        # A record older than the cutoff is still retained, so the seven days
        # asked for really are inside the read window.
        self.assertTrue(payload["horizon"]["covers_selected_span"])
        self.assertNotIn("vendor/model-old", json.dumps(payload))

    def test_an_unknown_horizon_is_neither_trusted_nor_reflected(self) -> None:
        self.register()
        self._write_two_ages()
        for supplied in ("1 week", "<script>alert(1)</script>", "../../etc", "",
                         "9999d", "AVAILABLE"):
            payload = plugin.route_data(FakeRequest(horizon=supplied))
            self.assertIn(payload["horizon"]["selected"], lens_core.HORIZONS)
            if supplied.strip().lower() not in lens_core.HORIZONS:
                self.assertEqual(payload["horizon"]["selected"], "available")
            # An empty request echoes nothing by construction; every other
            # supplied token must be absent from the answer verbatim.
            if supplied.strip():
                self.assertNotIn(supplied.strip()[:12], json.dumps(payload["horizon"]))

    def test_the_horizon_is_applied_before_the_limit_through_the_route(self) -> None:
        self.register()
        rows = []
        for index in range(12):
            rows.extend(chain("a%d" % index, prompt_tokens=1000 + index))
        # The first three chains sit 30 days back — genuinely older than the
        # cutoff, rather than carrying a timestamp the reader cannot use.
        self._stamp(rows[: 3 * 3], 30 * 24 * 60)
        self._stamp(rows[3 * 3:], 5)
        write_ledger(self.root, rows)
        payload = plugin.route_data(FakeRequest(horizon="7d", limit="4"))
        self.assertEqual(len(payload["points"]), 4)
        self.assertEqual(payload["horizon"]["attempts_selected"], 9)
        self.assertEqual(payload["points_omitted"], 5)
        cutoff = payload["horizon"]["cutoff_ms"]
        for point in payload["points"]:
            self.assertGreaterEqual(point["t"], cutoff)

    def test_the_trajectory_route_takes_the_same_horizon(self) -> None:
        self.register()
        # Dated relative to now: the shared fixture's literal timestamp ages
        # out of a 24h span as the calendar moves, which would silently turn
        # this into a test of the empty case.
        rows = self._stamp(chain("a1", prompt_tokens=10000, task_id="root"), 5)
        # 30 days back, not year 2000: a timestamp before MIN_EPOCH_MS is
        # "unusable", which is a different branch from "older than the cutoff".
        old = self._stamp(chain("a2", prompt_tokens=30000, task_id="root"), 30 * 24 * 60)
        for row in rows + old:
            row["root_task_id"] = "root"
        write_ledger(self.root, rows + old)
        task = lens_core._opaque("root", "t-")
        bounded = plugin.route_trajectory(FakeRequest(task=task, horizon="24h"))
        self.assertEqual(bounded["horizon"]["selected"], "24h")
        self.assertEqual(bounded["own_outside_horizon"], 1)
        sizes = [p["prompt_tokens"] for g in bounded["groups"] for p in g["points"]]
        self.assertEqual(sizes, [10000])
        every = plugin.route_trajectory(FakeRequest(task=task))
        self.assertEqual(every["horizon"]["selected"], "available")
        self.assertEqual(every["own_outside_horizon"], 0)
        sizes = sorted(p["prompt_tokens"] for g in every["groups"] for p in g["points"])
        self.assertEqual(sizes, [10000, 30000])

    def test_an_unavailable_ledger_still_answers_without_a_horizon_claim(self) -> None:
        self.register()
        payload = plugin.route_data(FakeRequest(horizon="6h"))
        self.assertFalse(payload["ok"])
        self.assertNotIn("horizon", payload)


class TestTrajectoryRoute(RouteTestCase):
    def _write_tree(self) -> str:
        rows = (
            chain("a1", prompt_tokens=10000, task_id="root")
            + chain("a2", prompt_tokens=30000, task_id="root")
            + chain("a3", prompt_tokens=4000, task_id="root",
                    model="vendor/model-light", category="consolidation")
        )
        for row in rows:
            if row["task_id"] == "root":
                row["root_task_id"] = "root"
        child = chain("a4", prompt_tokens=8000, task_id="child", category="subagent")
        for row in child:
            row["root_task_id"] = "root"
            row["parent_task_id"] = "root"
        write_ledger(self.root, rows + child)
        return lens_core._opaque("root", "t-")

    def test_groups_and_related_series(self) -> None:
        self.register()
        task = self._write_tree()
        payload = plugin.route_trajectory(FakeRequest(task=task))
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["groups"]), 2)
        self.assertEqual(len(payload["related"]), 1)
        self.assertTrue(all(group["joined"] for group in payload["groups"]))
        self.assertFalse(payload["related"][0]["joined"])

    def test_missing_task_parameter(self) -> None:
        self.register()
        self._write_tree()
        payload = plugin.route_trajectory(FakeRequest())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["groups"], [])

    def test_oversized_task_parameter_is_truncated_not_rejected(self) -> None:
        self.register()
        self._write_tree()
        payload = plugin.route_trajectory(FakeRequest(task="t-" + "f" * 5000))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["groups"], [])

    def test_supplied_text_is_never_reflected_back(self) -> None:
        self.register()
        self._write_tree()
        for supplied in ("<img src=x onerror=alert(1)>", "t-" + "f" * 5000,
                         "../../state/usage_attempts.jsonl", "t-ROOTKEY00000"):
            payload = plugin.route_trajectory(FakeRequest(task=supplied))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["task"], "")
            self.assertEqual(payload["groups"], [])
            self.assertNotIn(supplied[:32], json.dumps(payload))

    def test_a_valid_opaque_key_is_echoed_because_this_skill_minted_it(self) -> None:
        self.register()
        task = self._write_tree()
        payload = plugin.route_trajectory(FakeRequest(task=task))
        self.assertEqual(payload["task"], task)
        self.assertRegex(payload["task"], r"\At-[0-9a-f]{12}\Z")

    def test_unavailable_ledger(self) -> None:
        self.register()
        payload = plugin.route_trajectory(FakeRequest(task="t-abcdefabcdef"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "no_ledger")


class TestStarletteIntegration(RouteTestCase):
    def test_handlers_accept_a_real_starlette_request(self) -> None:
        try:
            from starlette.applications import Starlette
            from starlette.responses import JSONResponse
            from starlette.routing import Route
            from starlette.testclient import TestClient
        except Exception:                                # pragma: no cover
            self.skipTest("starlette is not installed in this interpreter")

        api = self.register()
        write_ledger(self.root, chain("a1", prompt_tokens=120000))

        def mount(handler):
            async def endpoint(request):
                return JSONResponse(handler(request))
            return endpoint

        app = Starlette(routes=[
            Route("/api/extensions/context-lens/" + path,
                  mount(spec["handler"]), methods=list(spec["methods"]))
            for path, spec in api.routes.items()
        ])
        with TestClient(app) as client:
            response = client.get("/api/extensions/context-lens/data?limit=10")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["points"][0]["prompt_tokens"], 120000)

            task = payload["points"][0]["task"]
            trajectory = client.get("/api/extensions/context-lens/trajectory?task=" + task)
            self.assertEqual(trajectory.status_code, 200)
            self.assertTrue(trajectory.json()["ok"])

            self.assertEqual(
                client.post("/api/extensions/context-lens/data").status_code, 405
            )


if __name__ == "__main__":
    unittest.main()
