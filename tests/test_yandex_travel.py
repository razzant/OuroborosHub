"""Offline checks for the Yandex Eats session: no Yandex account, browser, network or order.

A fake driver stands in for Chrome. The session core, page policy, loopback
protocol and plugin handlers are the shipped code; the persistence test runs
each tool call in a separate short-lived Python process, as the host does for
this isolated-dependency extension. The fake driver models the live action path without opening a browser.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import yaml

sys.dont_write_bytecode = True
SKILL = Path(__file__).resolve().parents[1] / "skills" / "yandex-travel"
_PACKAGE = "yandex_travel_under_test"
_spec = importlib.util.spec_from_file_location(_PACKAGE, SKILL / "plugin.py",
                                               submodule_search_locations=[str(SKILL)])
plugin = importlib.util.module_from_spec(_spec)
sys.modules[_PACKAGE] = plugin
_spec.loader.exec_module(plugin)
core = sys.modules[f"{_PACKAGE}.eats_session"]
driver_module = importlib.import_module(f"{_PACKAGE}.eats_driver")

TRANSACTIONAL = ("checkout", "order", "pay", "purchase", "buy", "submit", "confirm", "place", "login", "sign")

SECRET = "SECRET-7f3a"


def node(key, role, name, context="", **facts):
    record = {"key": key, "role": role, "name": name, "in": context, "area": "", "tag": "button",
              "entry": False, "search": False, "sensitive": False, "form_sensitive": False,
              "disabled": False, "covered": False, "href": "", "input_type": ""}
    record.update(facts)
    return record


class FakeSite:
    """Mutable page state shared by every driver instance (the 'real' browser)."""

    def __init__(self, url, nodes, text="", title="fixture"):
        self.url, self.nodes, self.text, self.title = url, list(nodes), text, title
        self.epoch = 1
        self.closed = False
        self.launches = 0
        self.effects = []  # every input event the page received
        self.orders = []  # what a disguised order button would have placed
        self.gotos = []
        self.observed = 0  # how often the page was read
        self.tabs = []
        self.reactions = {}
        self.redirect = ""  # e.g. an expired sign-in sends every visit to the passport host
        self.observe_error = None

    def node(self, key):
        return next((item for item in self.nodes if item["key"] == key), None)

    def navigate(self, url, nodes=None, text=None):
        self.url = url
        self.epoch += 1
        if nodes is not None:
            self.nodes = list(nodes)
        if text is not None:
            self.text = text


class FakeDriver:
    def __init__(self, site, *, synthetic=False):
        self.site = site
        self.synthetic = synthetic
        self.registry = (None, [])

    def launch(self):
        self.site.launches += 1
        self.site.closed = False

    def alive(self):
        return not self.site.closed

    def pump(self, ms):
        return False

    def close(self):
        self.site.closed = True

    def url(self):
        return self.site.url

    def epoch(self):
        return self.site.epoch

    def other_tabs(self):
        return list(self.site.tabs)

    def goto(self, url):
        self.site.gotos.append(url)
        self.site.navigate(self.site.redirect or url)

    def settle(self, max_ms):
        pass

    @staticmethod
    def _record(item, index):
        record = {key: value for key, value in item.items() if key not in {"key", "hidden"}}
        record["i"] = index
        return record

    def observe(self, key, obs, options):
        self.site.observed += 1
        if self.site.observe_error is not None:
            raise self.site.observe_error
        shown = [item for item in self.site.nodes if not item.get("hidden")]
        query = options.get("query", "").casefold()
        if query:
            shown = [item for item in shown if query in f"{item['name']} {item['in']}".casefold()]
        self.registry = (obs, [item["key"] for item in shown])
        return {"url": self.site.url, "title": self.site.title, "total": len(shown), "text": self.site.text,
                "elements": [self._record(item, index) for index, item in enumerate(shown)]}

    def _registered(self, obs, index):
        registered, keys = self.registry
        if registered != obs or index >= len(keys):
            return None
        return self.site.node(keys[index])

    def inspect(self, key, obs, index, expect):
        if self.registry[0] != obs:
            return {"missing": "observation_replaced"}
        item = self._registered(obs, index)
        if item is None:
            return {"missing": "detached"}
        record = self._record(item, index)
        now = {field: record.get(field, "") for field in expect}
        return record if now == expect else {"missing": "changed", "now": now}

    def perform(self, key, obs, index, action, value):
        item = self._registered(obs, index)
        if item is None:
            raise LookupError("not registered")
        self.site.effects.append((action, item["key"], value))
        reaction = self.site.reactions.get((item["key"], action))
        if reaction is not None:
            reaction(self.site, value)

    def scroll(self, direction):
        self.site.effects.append(("scroll", direction))

    def capture_element(self, key, obs, index, path):
        item = self._registered(obs, index)
        if item is None:
            raise LookupError("not registered")
        path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")

    def capture_viewport(self, path):
        path.write_bytes(b"\x89PNG\r\n\x1a\nviewport")


def restaurant(add_label="Добавить"):
    site = FakeSite("https://eda.yandex.kz/almaty/r/medovic", [
        node("search", "searchbox", "Найти ресторан или блюдо", tag="input", entry=True, search=True,
             input_type="search"),
        node("add-honey", "button", add_label, "Медовик 250 г 2 500 ₸"),
        node("add-napoleon", "button", add_label, "Наполеон 200 г 2 200 ₸"),
        node("cart", "region", "Корзина", area="aside", tag="aside"),
    ], text="Medovic\nМедовик 250 г 2 500 ₸\nНаполеон 200 г 2 200 ₸\nКорзина пуста", title="Medovic")

    def add_honey(page, _value):
        page.text = page.text.replace("Корзина пуста", "Корзина: Медовик × 1 — 2 500 ₸")

    site.reactions[("add-honey", "click")] = add_honey
    return site


def element(observation, predicate):
    """What the agent does: choose from the observation, not from code."""
    matches = [item for item in observation["elements"] if predicate(item)]
    assert len(matches) == 1, matches
    return matches[0]["id"]


class SessionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)

    def session(self, site):
        return core.Session(lambda: FakeDriver(site), state_dir=self.state)

    def opened(self, site):
        session = self.session(site)
        result = session.handle("open", {})
        self.assertEqual(result["status"], "ok", result)
        return session, result["observation"]

    def assertRefusedUntouched(self, result, code, site):
        self.assertEqual(result["status"], "refused", result)
        self.assertEqual(result["code"], code, result)
        self.assertEqual(result["effect"], "none", result)
        self.assertEqual([effect for effect in site.effects if effect[0] != "scroll"], [])
        self.assertEqual(site.orders, [])

    def assertNothingLeaks(self, value, *secrets):
        dump = json.dumps(value, ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, dump)

    def audit(self):
        path = self.state / "session" / "actions.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""


class API:
    def __init__(self, state):
        self.state, self.tools, self.companions = state, {}, []

    def get_state_dir(self):
        return str(self.state)

    def register_companion_process(self, name):
        self.companions.append(name)

    def register_tool(self, name, handler, **kwargs):
        self.tools[name] = (handler, kwargs)


class ToolSurfaceTests(SessionCase):
    def registered(self):
        api = API(self.state)
        plugin.register(api)
        return api

    def test_live_surface_exposes_actions_with_companion_and_no_taxi(self):
        api = self.registered()
        self.assertEqual(api.companions, ["eats_browser"])
        self.assertEqual(list(api.tools), ["yandex_eats_open", "yandex_eats_observe",
                                           "yandex_eats_act", "yandex_eats_search",
                                           "yandex_eats_capture", "yandex_eats_close"])
        self.assertEqual(tuple(op for _name, _handler, op, _description, _schema in plugin.TOOLS), core.LIVE_OPS)
        for name, (_handler, kwargs) in api.tools.items():
            self.assertLessEqual(len(name), 24)
            self.assertTrue(name.replace("_", "").isalnum())
            self.assertLessEqual(kwargs["timeout_sec"], 300)
            self.assertNotIn("taxi", (name + kwargs["description"]).casefold())

    def test_live_schemas_expose_scoped_actions_without_checkout(self):
        api = self.registered()
        properties = {name: kwargs["schema"]["properties"] for name, (_h, kwargs) in api.tools.items()}
        self.assertEqual(properties["yandex_eats_open"].keys(), {"home"})
        self.assertEqual(properties["yandex_eats_observe"].keys(), {"query", "scope", "scroll", "wait_ms"})
        self.assertEqual(properties["yandex_eats_act"].keys(),
                         {"action", "observation_id", "element_id", "target_name", "target_role",
                          "intent", "text", "key"})
        self.assertEqual(properties["yandex_eats_search"].keys(),
                         {"query", "observation_id", "element_id", "press_enter", "trigger_name"})
        self.assertEqual(properties["yandex_eats_capture"].keys(), {"observation_id", "element_id"})
        self.assertEqual(properties["yandex_eats_close"], {})
        for name, (_handler, kwargs) in api.tools.items():
            self.assertFalse(kwargs["schema"].get("additionalProperties", True), name)
            self.assertNotIn("url", properties[name])
            self.assertNotIn("screenshot", properties[name])
            words = json.dumps(kwargs["schema"]).casefold()
            for word in TRANSACTIONAL:
                self.assertNotIn(word, words, name)

    def test_invalid_arguments_are_refused_before_the_companion_is_contacted(self):
        api = self.registered()
        contacted = []
        original = plugin.call
        plugin.call = lambda *args, **kwargs: contacted.append(args) or {"status": "ok"}
        self.addCleanup(setattr, plugin, "call", original)
        attempts = {
            "yandex_eats_open": [{"url": "https://eda.yandex.kz/checkout"}, {"url": "https://pay.example/"},
                                 {"home": "yes"}, {"action": "click"}],
            "yandex_eats_observe": [{"screenshot": True}, {"action": "click", "element_id": "e1"},
                                    {"scroll": "left"}, {"intent": "checkout"}, {"text": "4111"},
                                    {"key": "Enter"}, {"wait_ms": 99999}],
            "yandex_eats_act": [{"action": "submit", "observation_id": "o", "element_id": "e1"},
                                {"action": "click", "observation_id": "o", "element_id": "e1",
                                 "intent": "checkout"},
                                {"action": "fill", "observation_id": "o", "element_id": "e1",
                                 "text": "x", "url": "https://pay.example/"}],
            "yandex_eats_search": [{"query": ""}, {"query": "x", "url": "https://pay.example/"}],
            "yandex_eats_capture": [{"element_id": "e1"},
                                     {"observation_id": "o", "element_id": "e1", "url": "https://pay.example/"}],
            "yandex_eats_close": [{"confirm": True}],
        }
        for tool, cases in attempts.items():
            for attempt in cases:
                with self.subTest(tool=tool, attempt=attempt):
                    result = json.loads(api.tools[tool][0](None, **attempt))
                    self.assertEqual((result["status"], result["effect"]), ("invalid", "none"))
        self.assertEqual(contacted, [])

    def test_action_vocabulary_has_no_transactional_words(self):
        for label, values in {"action": core.ACTIONS, "intent": core.INTENTS, "key": core.KEYS,
                              "ops": core.LIVE_OPS}.items():
            for value in values:
                for word in TRANSACTIONAL:
                    self.assertNotIn(word, value.casefold(), f"{label}: {value}")
        self.assertEqual(set(core.TIMEOUTS), set(core.LIVE_OPS))

    def test_companion_rejects_unknown_operations(self):
        site = restaurant()
        session, _ = self.opened(site)
        for op in ("checkout", "submit", "pay", "order", "confirm", "login", "screenshot", "back", "goto", ""):
            self.assertRefusedUntouched(session.handle(op, {}), "unknown_operation", site)
        self.assertEqual(site.gotos, [core.ROOT_URL])

    def test_skill_code_carries_no_site_wording_regex_screenshot_or_storage_access(self):
        for name in ("plugin.py", "eats_session.py", "eats_driver.py", "observer.js", "scripts/eats_browser.py"):
            source = (SKILL / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertFalse([ch for ch in source if "Ѐ" <= ch <= "ӿ"])
                for marker in ("import re\n", "re.compile", "RegExp", ".match(", ".test(", "replace(/", "split(/",
                               "document.cookie", "localStorage", "sessionStorage", "indexedDB",
                               "storage_state", "cookies("):
                    self.assertNotIn(marker, source)
        observer = (SKILL / "observer.js").read_text(encoding="utf-8")
        for marker in (".value", "selectedOptions", ".options"):  # field values are never read
            self.assertNotIn(marker, observer.replace(".nodeValue", ""))

    def test_manifest_declares_agent_cart_handoff_and_deferred_taxi(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        front = yaml.safe_load(text.split("---", 2)[1])
        self.assertIn("companion_process", front["permissions"])
        self.assertEqual(front["companion_processes"], [{
            "name": "eats_browser", "command": ["python3", "scripts/eats_browser.py"], "runtime": "python3",
            "restart_policy": "on_failure", "max_restarts": 3}])
        self.assertEqual(front["plugin_api"], "2.0")
        self.assertIn("Yandex Taxi is deferred", text)
        self.assertIn("not installed-host verified", text)
        self.assertIn("There is no Taxi tool", text)
        self.assertIn("заказывай", text)
        self.assertIn("generic click", text)
        self.assertIn("screenshot", text)
        for tool in ("yandex_eats_act", "yandex_eats_search", "yandex_eats_capture"):
            self.assertIn(tool, text)


class LiveBoundaryTests(SessionCase):
    """Page and secret boundaries on the live action path."""

    def test_selected_region_capture_is_private_fresh_and_not_a_send(self):
        site = restaurant()
        session, observed = self.opened(site)
        cart = element(observed, lambda item: item["name"] == "Корзина")
        captured = session.handle("capture", {"observation_id": observed["observation_id"],
                                               "element_id": cart})
        self.assertEqual(captured["status"], "captured_private", captured)
        self.assertEqual(Path(captured["path"]).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertNotIn("image_base64", captured)
        self.assertEqual(site.effects, [])
        session.handle("observe", {})
        stale = session.handle("capture", {"observation_id": observed["observation_id"],
                                            "element_id": cart})
        self.assertEqual((stale["code"], stale["effect"]), ("stale_observation", "none"))
        site.navigate("https://passport.yandex.kz/auth")
        refused = session.handle("capture", {"observation_id": observed["observation_id"],
                                              "element_id": cart})
        self.assertEqual((refused["code"], refused["effect"]), ("outside_service", "none"))

    def test_navigation_during_capture_discards_png_without_returning_a_path(self):
        site = restaurant()

        class MovingDriver(FakeDriver):
            def capture_viewport(self, path):
                super().capture_viewport(path)
                self.site.navigate("https://passport.yandex.kz/auth")

        session = core.Session(lambda: MovingDriver(site), state_dir=self.state)
        opened = session.handle("open", {})
        result = session.handle("capture", {"observation_id": opened["observation"]["observation_id"]})
        self.assertEqual((result["status"], result["code"]), ("refused", "outside_service"))
        self.assertNotIn("path", result)
        self.assertEqual(list((self.state / "captures").iterdir()), [])

    def disguised_order_site(self):
        site = restaurant()
        # Same site, harmless-looking label, but pressing it would place the order.
        site.reactions[("add-honey", "click")] = lambda page, _value: page.orders.append("placed")
        site.reactions[("search", "press")] = lambda page, _value: page.orders.append("placed")
        return site

    def test_generic_click_has_residual_semantic_risk(self):
        site = self.disguised_order_site()
        session, observation = self.opened(site)
        self.assertEqual(observation["mode"], "agent_can_act")
        oid = observation["observation_id"]
        target = element(observation, lambda item: "Медовик" in item.get("in", ""))
        result = session.handle("act", {"action": "click", "observation_id": oid,
                                         "element_id": target, "intent": "add_item"})
        self.assertEqual(result["status"], "done")
        self.assertEqual(site.orders, ["placed"])
        self.assertEqual(site.gotos, [core.ROOT_URL])

    def test_the_real_driver_is_live_unless_given_fixtures(self):
        live = driver_module.PlaywrightDriver(self.state / "profile")
        self.assertIs(live.synthetic, False)
        synthetic = driver_module.PlaywrightDriver(self.state / "profile", fixtures={core.ROOT_URL: "<p>x</p>"})
        self.assertIs(synthetic.synthetic, True)
        self.assertEqual(driver_module.SERVICE_HOST, core.SERVICE_HOST)
        companion = (SKILL / "scripts" / "eats_browser.py").read_text(encoding="utf-8")
        self.assertIn("PlaywrightDriver(prepare_profile(state_dir))", companion)
        for marker in ("fixtures", "fixture_effects", "synthetic", "headless"):
            self.assertNotIn(marker, companion.split('"""', 2)[2])

    def test_open_only_ever_loads_the_service_root(self):
        site = restaurant()
        session, _ = self.opened(site)
        self.assertEqual(session.handle("open", {})["effect"], "none")
        self.assertEqual(session.handle("open", {"home": True})["effect"], "navigated")
        self.assertEqual(session.handle("open", {"url": "https://eda.yandex.kz/checkout"})["status"], "invalid")
        self.assertEqual(site.gotos, [core.ROOT_URL, core.ROOT_URL])

    def test_scroll_moves_the_view_without_input(self):
        site = restaurant()
        session, _ = self.opened(site)
        result = session.handle("observe", {"scroll": "down"})
        self.assertEqual((result["status"], result["effect"]), ("ok", "scrolled"))
        self.assertEqual(site.effects, [("scroll", "down")])
        self.assertEqual(site.orders, [])

    def test_yandex_id_pages_are_login_required_and_never_read(self):
        urls = [f"https://passport.yandex.kz/auth/welcome?retpath=https%3A%2F%2Feda.yandex.kz&track_id={SECRET}",
                f"https://id.yandex.ru/security?uid={SECRET}", f"http://sso.passport.yandex.ru/push?t={SECRET}"]
        for url in urls:
            with self.subTest(url=url):
                site = FakeSite(url, [node("pw", "textbox", "Пароль", tag="input", entry=True, sensitive=True),
                                      node("go", "button", "Войти")], text="Вход в Яндекс ID", title="Яндекс ID")
                site.redirect = url
                session = self.session(site)
                opened = session.handle("open", {})
                observed = session.handle("observe", {"scope": "page", "scroll": "down", "wait_ms": 10})
                for result in (opened, observed):
                    self.assertEqual(result["status"], "login_required", result)
                    self.assertEqual(result["page"]["kind"], "yandex_id")
                    self.assertEqual(set(result) - {"launched"}, {"status", "page", "reason", "effect"})
                    self.assertNothingLeaks(result, SECRET, "retpath", "welcome", "security", "Пароль",
                                            "Войти", "Вход", "Яндекс ID", "?")
                self.assertTrue(opened["launched"])
                self.assertEqual(site.observed, 0)  # the page was never read
                self.assertEqual(site.effects, [])
                self.assertNothingLeaks(self.audit(), SECRET, "retpath", "?")

    def test_other_pages_are_reported_by_host_only(self):
        site = FakeSite(f"https://pay.example/checkout?card=4111111111111111&cvc={SECRET}",
                        [node("pay", "button", "Оплатить")], text="Карта 4111 1111 1111 1111")
        site.redirect = site.url
        session = self.session(site)
        result = session.handle("open", {})
        self.assertEqual((result["status"], result["page"]), ("outside_service",
                                                               {"kind": "other", "host": "pay.example"}))
        self.assertNothingLeaks(result, SECRET, "4111", "checkout", "Оплатить")
        self.assertEqual(site.observed, 0)

    def test_service_observation_strips_queries_and_never_returns_field_values(self):
        site = FakeSite(f"https://eda.yandex.kz/almaty/r/medovic?token={SECRET}#pay", [
            node("addr", "textbox", "Адрес", tag="input", entry=True, value=f"Абая 1 {SECRET}",
                 options=[f"card {SECRET}"]),
            node("otp", "textbox", "Код", tag="input", entry=True, sensitive=True, value="424242"),
            node("link", "link", "Medovic", tag="a", href=f"https://eda.yandex.kz/r/medovic?utm={SECRET}"),
            node("away", "link", "Партнёр", tag="a", href=f"https://partner.example/x?ref={SECRET}"),
        ])
        site.redirect = site.url  # the root redirects to the restaurant page
        site.tabs = [f"https://passport.yandex.kz/auth?track_id={SECRET}", f"https://eda.yandex.kz/r?x={SECRET}"]
        session, observation = self.opened(site)
        self.assertEqual(observation["url"], "https://eda.yandex.kz/almaty/r/medovic")
        self.assertEqual([item.get("href") for item in observation["elements"]],
                         [None, None, "https://eda.yandex.kz/r/medovic", "external:partner.example"])
        self.assertIn("owner_only", observation["elements"][1]["state"])
        self.assertEqual(observation["other_tabs"], [{"kind": "yandex_id", "host": "passport.yandex.kz"},
                                                     {"kind": "service", "host": "eda.yandex.kz"}])
        self.assertNothingLeaks(observation, SECRET, "424242", "Абая 1", "#pay")
        self.assertNothingLeaks(self.audit(), SECRET)

    def test_login_frame_on_a_service_page_is_flagged(self):
        site = restaurant()
        session, _ = self.opened(site)
        original = FakeDriver.observe

        def with_frame(driver, key, obs, options):
            raw = original(driver, key, obs, options)
            raw["frames"] = ["https://passport.yandex.kz", "https://yastatic.net"]
            return raw

        FakeDriver.observe = with_frame
        self.addCleanup(setattr, FakeDriver, "observe", original)
        observation = session.handle("observe", {})["observation"]
        self.assertTrue(observation["login_frame"])
        self.assertEqual(observation["foreign_frames"], ["yandex_id:passport.yandex.kz", "other:yastatic.net"])

    def test_page_leaving_the_service_mid_read_is_discarded(self):
        site = restaurant()
        session, _ = self.opened(site)
        original = FakeDriver.observe

        def moved(driver, key, obs, options):
            raw = original(driver, key, obs, options)
            site.url = f"https://passport.yandex.kz/auth?track_id={SECRET}"
            return raw

        FakeDriver.observe = moved
        self.addCleanup(setattr, FakeDriver, "observe", original)
        result = session.handle("observe", {})
        self.assertEqual(result["status"], "login_required")
        self.assertNothingLeaks(result, SECRET, "Медовик", "Medovic")

    def test_driver_errors_are_reported_without_raw_messages(self):
        site = restaurant()
        session, _ = self.opened(site)
        site.observe_error = RuntimeError(f"Target crashed at https://eda.yandex.kz/?token={SECRET}")
        result = session.handle("observe", {})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"].split(" ")[0], "RuntimeError")
        self.assertNothingLeaks(result, SECRET)


class ProfileTests(SessionCase):
    def test_profile_is_created_marked_private_and_reused_in_the_state_dir(self):
        path = core.prepare_profile(self.state)
        self.assertEqual(path, self.state.resolve() / "chrome-profile")
        self.assertTrue((path / core.PROFILE_MARKER).is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        (path / "Default").mkdir()
        self.assertEqual(core.prepare_profile(self.state), path)

    def test_foreign_or_linked_profiles_are_refused(self):
        copied = self.state / "chrome-profile"
        (copied / "Default").mkdir(parents=True)
        (copied / "Default" / "Cookies").write_bytes(b"owner cookies")
        with self.assertRaises(core.ProfileRefused):
            core.prepare_profile(self.state)
        self.assertFalse((copied / core.PROFILE_MARKER).exists())

        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        linked_state = Path(other.name) / "state"
        linked_state.mkdir()
        (linked_state / "chrome-profile").symlink_to(copied, target_is_directory=True)
        with self.assertRaises(core.ProfileRefused):
            core.prepare_profile(linked_state)

    def test_session_reports_a_refused_profile_without_launching(self):
        (self.state / "chrome-profile").mkdir()
        (self.state / "chrome-profile" / "Local State").write_text("{}")
        launched = []

        def factory():
            core.prepare_profile(self.state)
            launched.append(True)
            return FakeDriver(restaurant())

        session = core.Session(factory, state_dir=self.state)
        result = session.handle("open", {})
        self.assertEqual((result["status"], result["effect"]), ("profile_refused", "none"))
        self.assertEqual(launched, [])

    def test_launch_failure_is_typed_and_closes_the_attempt(self):
        class Broken(FakeDriver):
            def launch(self):
                raise RuntimeError(f"Chromium not found at /Users/owner?{SECRET}")

        site = restaurant()
        session = core.Session(lambda: Broken(site), state_dir=self.state)
        result = session.handle("open", {})
        self.assertEqual(result["status"], "launch_failed")
        self.assertNothingLeaks(result, SECRET, "/Users/owner")
        self.assertTrue(site.closed)
        self.assertEqual(session.handle("observe", {})["status"], "not_open")


class FixtureLaneTests(SessionCase):
    """Live action path: element identity, staleness and refusals before input."""

    def act_on(self, session, oid, element_id, intent="add_item"):
        return session.handle("act", {"action": "click", "observation_id": oid, "element_id": element_id,
                                      "intent": intent})

    def test_successful_action_returns_a_fresh_observation(self):
        site = restaurant()
        session, observation = self.opened(site)
        self.assertEqual(observation["mode"], "agent_can_act")
        target = element(observation, lambda item: "Медовик" in item.get("in", ""))
        result = self.act_on(session, observation["observation_id"], target)
        self.assertEqual((result["status"], result["effect"]), ("done", "performed"), result)
        self.assertEqual(site.effects, [("click", "add-honey", "")])
        self.assertNotEqual(result["observation"]["observation_id"], observation["observation_id"])
        self.assertIn("Медовик × 1", result["observation"]["text"])
        self.assertEqual(result["action"]["element"]["in"], "Медовик 250 г 2 500 ₸")
        self.assertEqual(json.loads(self.audit().splitlines()[-1])["intent"], "add_item")

    def test_renamed_ui_is_driven_by_observation_not_labels(self):
        for label in ("Добавить", "В корзину", "+", "Add"):
            with self.subTest(label=label):
                site = restaurant(add_label=label)
                session, observation = self.opened(site)
                target = element(observation, lambda item: item["role"] == "button"
                                 and "Медовик" in item.get("in", ""))
                self.assertEqual(self.act_on(session, observation["observation_id"], target)["status"], "done")
                self.assertEqual(site.effects, [("click", "add-honey", "")])

    def test_stale_observation_after_navigation_refuses_and_attaches_fresh_view(self):
        site = restaurant()
        session, observation = self.opened(site)
        site.navigate(site.url + "?category=cakes")
        result = self.act_on(session, observation["observation_id"], "e2")
        self.assertRefusedUntouched(result, "stale_observation", site)
        self.assertNotEqual(result["observation"]["observation_id"], observation["observation_id"])

    def test_navigation_to_sign_in_invalidates_ids_and_is_not_read(self):
        site = restaurant()
        session, observation = self.opened(site)
        site.navigate(f"https://passport.yandex.kz/auth?track_id={SECRET}")
        result = self.act_on(session, observation["observation_id"], "e2")
        self.assertRefusedUntouched(result, "login_required", site)
        site.navigate("https://eda.yandex.kz/almaty/r/medovic")
        stale = self.act_on(session, observation["observation_id"], "e2")
        self.assertRefusedUntouched(stale, "stale_observation", site)
        self.assertNothingLeaks([result, stale], SECRET)

    def test_only_the_latest_observation_is_actionable(self):
        site = restaurant()
        session, first = self.opened(site)
        session.handle("observe", {})
        self.assertRefusedUntouched(self.act_on(session, first["observation_id"], "e2"), "stale_observation", site)
        latest = session.handle("observe", {})["observation"]["observation_id"]
        self.assertRefusedUntouched(self.act_on(session, latest, "e99"), "unknown_element", site)

    def test_changed_recycled_or_detached_elements_are_stale(self):
        mutations = {
            "renamed in place": lambda site: site.node("add-honey").update(name="Нет в наличии"),
            "recycled for another dish": lambda site: site.node("add-honey").update({"in": "Эклер 90 г 900 ₸"}),
            "detached": lambda site: site.nodes.remove(site.node("add-honey")),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                site = restaurant()
                session, observation = self.opened(site)
                target = element(observation, lambda item: "Медовик" in item.get("in", ""))
                mutate(site)
                result = self.act_on(session, observation["observation_id"], target)
                self.assertRefusedUntouched(result, "stale_element", site)
                self.assertIn("observation", result)

    def test_ambiguous_name_refuses_with_candidates_then_agent_chooses(self):
        site = restaurant()
        session, observation = self.opened(site)
        oid = observation["observation_id"]
        result = session.handle("act", {"action": "click", "observation_id": oid, "target_name": "добавить",
                                        "intent": "add_item"})
        self.assertRefusedUntouched(result, "ambiguous", site)
        self.assertEqual(result["observation_id"], oid)  # the refusal did not invalidate the view
        chosen = [item["id"] for item in result["candidates"] if "Медовик" in item["in"]]
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(self.act_on(session, oid, chosen[0])["status"], "done")
        latest = session.handle("observe", {})["observation"]["observation_id"]
        missing = session.handle("act", {"action": "click", "target_name": "Оплатить", "intent": "open",
                                         "observation_id": latest})
        self.assertEqual(missing["code"], "not_found")

    def test_structural_boundaries_refuse_without_any_input(self):
        site = FakeSite("https://eda.yandex.kz/almaty/r/demo", [
            node("password", "textbox", "Пароль", tag="input", entry=True, sensitive=True, input_type="password"),
            node("card", "textbox", "Номер", tag="input", entry=True, sensitive=True),
            node("send", "button", "Продолжить", form_sensitive=True),
            node("away", "link", "Партнёр", tag="a", href="https://pay.example/checkout"),
            node("comment", "textbox", "Комментарий", tag="textarea", entry=True),
            node("covered", "button", "Под слоем", covered=True),
        ])
        session, observation = self.opened(site)
        oid = observation["observation_id"]
        cases = [
            ({"action": "fill", "element_id": "e1", "text": "secret"}, "sensitive_field"),
            ({"action": "fill", "element_id": "e2", "text": "4111"}, "sensitive_field"),
            ({"action": "click", "element_id": "e3", "intent": "choose"}, "sensitive_form"),
            ({"action": "click", "element_id": "e4", "intent": "open"}, "link_leaves_service"),
            ({"action": "press", "element_id": "e5", "key": "Enter"}, "enter_outside_search"),
            ({"action": "fill", "element_id": "e3", "text": "x"}, "not_text_entry"),
            ({"action": "click", "element_id": "e6", "intent": "choose"}, "element_covered"),
        ]
        for args, code in cases:
            with self.subTest(code=code):
                self.assertRefusedUntouched(session.handle("act", {**args, "observation_id": oid}), code, site)
                oid = session.handle("observe", {})["observation"]["observation_id"]

    def test_transactional_arguments_are_invalid_in_the_fixture_lane_too(self):
        site = restaurant()
        session, observation = self.opened(site)
        oid = observation["observation_id"]
        for args in ({"action": "click", "observation_id": oid, "element_id": "e2", "intent": "checkout"},
                     {"action": "click", "observation_id": oid, "element_id": "e2"},
                     {"action": "submit", "observation_id": oid, "element_id": "e2"},
                     {"action": "select", "observation_id": oid, "element_id": "e2", "text": "Картой"},
                     {"action": "press", "observation_id": oid, "element_id": "e2", "key": "Space"},
                     {"action": "click", "observation_id": oid, "element_id": "e2", "intent": "open",
                      "confirm": True}):
            with self.subTest(args=args):
                result = session.handle("act", args)
                self.assertEqual((result["status"], result["effect"]), ("invalid", "none"))
        self.assertEqual(site.effects, [])

    def test_search_fills_presses_enter_waits_and_returns_observation(self):
        site = restaurant()
        site.reactions[("search", "press")] = lambda page, _key: page.navigate(
            "https://eda.yandex.kz/almaty/search?query=Medovic", text="Medovic — кондитерская")
        session, _ = self.opened(site)
        result = session.handle("search", {"query": "  Medovic "})
        self.assertEqual((result["status"], result["effect"]), ("done", "performed"), result)
        self.assertEqual(site.effects, [("fill", "search", "Medovic"), ("press", "search", "Enter")])
        self.assertTrue(result["navigated"])
        self.assertIn("кондитерская", result["observation"]["text"])
        self.assertEqual(result["observation"]["url"], "https://eda.yandex.kz/almaty/search")

    def test_agent_chosen_trigger_name_combines_fill_and_results_without_hardcoded_button(self):
        site = FakeSite("https://eda.yandex.kz/", [node("field", "combobox", "Искать", tag="input", entry=True)])

        def filled(page, _value):
            page.nodes.append(node("submit", "button", "Искать сейчас"))

        site.reactions[("field", "fill")] = filled
        site.reactions[("submit", "click")] = lambda page, _value: page.navigate(
            "https://eda.yandex.kz/results", text="Выдача товара 850 ₸")
        session, observation = self.opened(site)
        result = session.handle("search", {"query": "товар", "observation_id": observation["observation_id"],
                                           "element_id": "e1", "press_enter": False,
                                           "trigger_name": "Искать сейчас"})
        self.assertEqual(result["status"], "done", result)
        self.assertEqual(site.effects, [("fill", "field", "товар"), ("click", "submit", "")])
        self.assertIn("Выдача товара", result["observation"]["text"])

        site = FakeSite("https://eda.yandex.kz/", [node("field", "combobox", "Поиск", tag="input", entry=True)])
        site.reactions[("field", "fill")] = lambda page, _value: page.nodes.extend(
            [node("a", "button", "Пуск"), node("b", "button", "Пуск")])
        session, observation = self.opened(site)
        refused = session.handle("search", {"query": "товар", "observation_id": observation["observation_id"],
                                             "element_id": "e1", "press_enter": False, "trigger_name": "Пуск"})
        self.assertEqual(refused["code"], "ambiguous")
        self.assertEqual(site.effects, [("fill", "field", "товар")])

    def test_search_field_must_be_unique_or_designated(self):
        site = restaurant()
        site.nodes.append(node("search2", "searchbox", "Адрес", tag="input", entry=True, search=True))
        session, _ = self.opened(site)
        result = session.handle("search", {"query": "Medovic"})
        self.assertRefusedUntouched(result, "ambiguous", site)
        self.assertEqual(len(result["candidates"]), 2)
        oid = result["observation"]["observation_id"]
        chosen = session.handle("search", {"query": "Medovic", "observation_id": oid, "element_id": "e1"})
        self.assertEqual(chosen["status"], "done")

        plain = FakeSite("https://eda.yandex.kz/", [node("field", "textbox", "Что ищем", tag="input", entry=True)])
        session, _ = self.opened(plain)
        self.assertRefusedUntouched(session.handle("search", {"query": "Medovic"}), "not_found", plain)
        oid = session.handle("observe", {})["observation"]["observation_id"]
        designated = session.handle("search", {"query": "Medovic", "observation_id": oid, "element_id": "e1"})
        self.assertEqual(plain.effects, [("fill", "field", "Medovic")])
        self.assertIn("Enter skipped", designated["note"])

    def test_audit_log_holds_no_typed_text_queries_or_url_queries(self):
        site = restaurant()
        site.url += f"?token={SECRET}"
        site.nodes.append(node("note", "textbox", "Комментарий", tag="textarea", entry=True))
        session, observation = self.opened(site)
        note = element(observation, lambda item: item["name"] == "Комментарий")
        session.handle("act", {"action": "fill", "observation_id": observation["observation_id"],
                               "element_id": note, "text": f"домофон {SECRET}"})
        session.handle("search", {"query": f"Medovic {SECRET}"})
        log = self.audit()
        self.assertEqual(len(log.splitlines()), 3)
        self.assertNothingLeaks(log, SECRET, "домофон", "Medovic", "?")
        self.assertIn('"page": "service:eda.yandex.kz"', log)

    def test_failure_after_input_reports_unknown_effect(self):
        site = restaurant()

        def timeout(_page, _value):
            raise RuntimeError(f"Timeout 8000ms exceeded at https://eda.yandex.kz/?t={SECRET}")

        site.reactions[("add-honey", "click")] = timeout
        session, observation = self.opened(site)
        result = self.act_on(session, observation["observation_id"], "e2")
        self.assertEqual((result["status"], result["effect"]), ("error", "unknown"))
        self.assertNothingLeaks(result, SECRET)


class DecisionTests(SessionCase):
    """Cart and availability questions are reported for the agent and owner, never resolved by code."""

    def test_occupied_cart_is_reported_not_resolved(self):
        site = restaurant()
        site.text = site.text.replace("Корзина пуста", "Корзина: Бургер × 2 — Burger Place")
        site.nodes.append(node("clear", "button", "Очистить корзину", area="aside"))
        session, observation = self.opened(site)
        self.assertIn("Burger Place", observation["text"])
        self.assertIn("Очистить корзину", [item["name"] for item in observation["elements"]])
        clear = element(observation, lambda item: item["name"] == "Очистить корзину")
        # An occupied cart is visible; the agent must ask before removing it.
        self.assertIn("Burger Place", session.handle("observe", {})["observation"]["text"])

    def test_unavailable_dish_is_visible_state_and_left_to_the_agent(self):
        site = restaurant()
        site.node("add-honey")["disabled"] = True
        _session, observation = self.opened(site)
        honey = [item for item in observation["elements"] if "Медовик" in item.get("in", "")][0]
        self.assertIn("disabled", honey["state"])
        self.assertEqual(site.effects, [])

        fixture_site = restaurant()
        fixture_site.node("add-honey")["disabled"] = True
        session, observation = self.opened(fixture_site)
        honey = element(observation, lambda item: "Медовик" in item.get("in", ""))
        result = session.handle("act", {"action": "click", "observation_id": observation["observation_id"],
                                        "element_id": honey, "intent": "add_item"})
        self.assertRefusedUntouched(result, "element_disabled", fixture_site)
        self.assertIn("choose something else", result["reason"])
        other = element(observation, lambda item: "Наполеон" in item.get("in", ""))
        done = session.handle("act", {"action": "click", "observation_id": observation["observation_id"],
                                      "element_id": other, "intent": "add_item"})
        self.assertEqual(done["status"], "done")
        self.assertEqual(fixture_site.effects, [("click", "add-napoleon", "")])


class SessionLifetimeTests(SessionCase):
    def test_chrome_starts_only_on_open(self):
        site = restaurant()
        session = self.session(site)
        self.assertEqual(session.handle("observe", {})["status"], "not_open")
        self.assertEqual(session.handle("close", {})["was_open"], False)
        self.assertEqual(site.launches, 0)
        session.handle("open", {})
        self.assertEqual(site.launches, 1)

    def test_owner_closing_the_window_is_session_loss_then_open_relaunches(self):
        site = restaurant()
        session, observation = self.opened(site)
        site.closed = True
        self.assertEqual(session.handle("observe", {})["status"], "session_lost")
        self.assertEqual(self.act_on_e2(session, observation)["status"], "not_open")
        reopened = session.handle("open", {})
        self.assertTrue(reopened["launched"])
        self.assertEqual(site.launches, 2)
        self.assertRefusedUntouched(self.act_on_e2(session, observation), "stale_observation", site)

    def test_companion_restart_invalidates_earlier_ids(self):
        site = restaurant()
        _first, observation = self.opened(site)
        second, _ = self.opened(site)
        self.assertRefusedUntouched(self.act_on_e2(second, observation), "stale_observation", site)

    @staticmethod
    def act_on_e2(session, observation):
        return session.handle("act", {"action": "click", "observation_id": observation["observation_id"],
                                      "element_id": "e2", "intent": "add_item"})

    def test_one_companion_per_profile(self):
        lock = self.state / "companion.lock"
        with core.hold_lock(lock):
            with self.assertRaises(RuntimeError):
                with core.hold_lock(lock, wait_sec=0.3):
                    pass

    def test_companion_entry_needs_the_host_state_dir(self):
        completed = subprocess.run([sys.executable, str(SKILL / "scripts" / "eats_browser.py")],
                                   env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("OUROBOROS_SKILL_STATE_DIR", completed.stderr)


_CHILD = r"""
import importlib.util, json, sys
sys.dont_write_bytecode = True
skill, state, tool, args = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
spec = importlib.util.spec_from_file_location("per_call_child", skill + "/plugin.py",
                                              submodule_search_locations=[skill])
plugin = importlib.util.module_from_spec(spec)
sys.modules["per_call_child"] = plugin
spec.loader.exec_module(plugin)
class API:
    tools = {}
    def get_state_dir(self): return state
    def register_companion_process(self, name): pass
    def register_tool(self, name, handler, **kwargs): self.tools[name] = handler
api = API()
plugin.register(api)
print(api.tools[tool](None, **args))
"""


class CompanionProtocolTests(SessionCase):
    def serve(self, site):
        session = self.session(site)
        stop = threading.Event()
        thread = threading.Thread(target=core.serve, args=(session, self.state),
                                  kwargs={"should_stop": stop.is_set}, daemon=True)
        thread.start()
        endpoint = self.state / "session" / "endpoint.json"
        deadline = time.monotonic() + 10
        while not endpoint.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(endpoint.exists())

        def shutdown():
            stop.set()
            thread.join(10)

        self.addCleanup(shutdown)
        return session, endpoint, shutdown

    def child(self, tool, **args):
        completed = subprocess.run([sys.executable, "-c", _CHILD, str(SKILL), str(self.state), tool,
                                    json.dumps(args)], capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_session_persists_across_short_lived_tool_processes(self):
        site = restaurant()
        _session, endpoint, shutdown = self.serve(site)
        self.assertEqual(endpoint.stat().st_mode & 0o777, 0o600)
        opened = self.child("yandex_eats_open")
        self.assertTrue(opened["launched"])
        observed = self.child("yandex_eats_observe", query="Медовик")
        self.assertEqual([item["in"] for item in observed["observation"]["elements"]], ["Медовик 250 г 2 500 ₸"])
        scrolled = self.child("yandex_eats_observe", scroll="down")
        self.assertEqual(scrolled["effect"], "scrolled")
        again = self.child("yandex_eats_open")
        self.assertFalse(again["launched"])
        self.assertEqual(site.launches, 1)
        self.assertFalse(site.closed)
        self.assertEqual(site.effects, [("scroll", "down")])

        shutdown()
        self.assertTrue(site.closed)  # stopping the companion closes Chrome
        self.assertFalse(endpoint.exists())
        self.assertEqual(self.child("yandex_eats_observe")["status"], "companion_unavailable")

    def test_short_lived_clients_act_on_one_companion(self):
        site = restaurant()
        self.serve(site)
        opened = self.child("yandex_eats_open")
        oid = opened["observation"]["observation_id"]
        result = self.child("yandex_eats_act", action="click", observation_id=oid,
                            element_id="e2", intent="add_item")
        self.assertEqual(result["status"], "done")
        self.assertIn("Медовик × 1", result["observation"]["text"])
        self.assertEqual(site.launches, 1)
        self.assertFalse(site.closed)
        searched = self.child("yandex_eats_search", query="Medovic")
        self.assertEqual(searched["status"], "done")
        self.assertEqual(site.launches, 1)

    @unittest.skipIf(sys.platform == "win32", "POSIX signal semantics")
    def test_real_companion_process_is_lazy_and_serves_children_until_sigterm(self):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
               "OUROBOROS_SKILL_STATE_DIR": str(self.state)}
        proc = subprocess.Popen([sys.executable, str(SKILL / "scripts" / "eats_browser.py")], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        endpoint = self.state / "session" / "endpoint.json"
        deadline = time.monotonic() + 15
        while not endpoint.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(json.loads(endpoint.read_text(encoding="utf-8"))["pid"], proc.pid)
        # No open is sent: that would start real Chrome on the live site.
        self.assertEqual(self.child("yandex_eats_observe")["status"], "not_open")
        self.assertEqual(core.call(self.state, "act", {"action": "click", "observation_id": "x.1.1",
                                                       "element_id": "e1", "intent": "open"})["status"], "not_open")
        self.assertEqual(self.child("yandex_eats_close")["status"], "closed")
        self.assertFalse((self.state / "chrome-profile").exists())  # nothing prepared before an open
        self.assertIsNone(proc.poll())
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(15), 0)
        self.assertFalse(endpoint.exists())
        self.assertEqual(self.child("yandex_eats_observe")["status"], "companion_unavailable")

    def test_wrong_token_is_refused_without_touching_the_session(self):
        site = restaurant()
        _session, endpoint, _shutdown = self.serve(site)
        data = json.loads(endpoint.read_text(encoding="utf-8"))
        endpoint.write_text(json.dumps({**data, "token": "0" * 48}), encoding="utf-8")
        result = core.call(self.state, "open", {})
        self.assertEqual((result["status"], result["code"]), ("refused", "unauthorized"))
        self.assertEqual(site.launches, 0)

    def test_missing_companion_is_reported(self):
        self.assertEqual(core.call(self.state, "observe", {})["status"], "companion_unavailable")


if __name__ == "__main__":
    unittest.main()
