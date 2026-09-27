"""Opt-in headless Chromium check of observer.js and the Playwright driver.

The driver is built with synthetic fixtures: every request is answered from
PAGES (keyed by scheme://host/path) or aborted, service workers are blocked
and host resolution is disabled, so nothing reaches the network. A temporary
marked profile is used, never the owner's. Run explicitly:

    YANDEX_EATS_BROWSER_FIXTURE=1 python -m unittest discover -s tests -p test_yandex_eats_browser.py

(set PLAYWRIGHT_BROWSERS_PATH if Playwright's Chromium lives outside $HOME).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SKILL = Path(__file__).resolve().parents[1] / "skills" / "yandex-travel"
_PACKAGE = "yandex_travel_browser_fixture"
_spec = importlib.util.spec_from_file_location(_PACKAGE, SKILL / "plugin.py",
                                               submodule_search_locations=[str(SKILL)])
_plugin = importlib.util.module_from_spec(_spec)
sys.modules[_PACKAGE] = _plugin
_spec.loader.exec_module(_plugin)
core = sys.modules[f"{_PACKAGE}.eats_session"]
driver_module = importlib.import_module(f"{_PACKAGE}.eats_driver")

ENABLED = os.environ.get("YANDEX_EATS_BROWSER_FIXTURE") == "1"
try:
    import playwright.sync_api  # noqa: F401
except ImportError:
    ENABLED = False

EDA = "https://eda.yandex.kz"
SECRETS = ("hunter2-secret", "424242", "4111111111111111", "secret-address", "secret-note", "secret-editable",
           "secret-option", "secret-custom-field", "cookie-secret", "storage-secret", "track-secret")
_HEAD = ("<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>{title}</title><style>"
         "body{{font:16px sans-serif;margin:0}} header{{position:sticky;top:0;background:#fff;padding:8px}}"
         ".card{{border:1px solid #ccc;margin:8px;padding:8px;width:320px}} .tile{{cursor:pointer;padding:8px}}"
         "</style></head><body>")
_INPUT_SPY = """<script>window.__inputs = [];
for (const type of ['pointerdown', 'mousedown', 'click', 'keydown', 'focusin', 'input', 'submit'])
  document.addEventListener(type, () => window.__inputs.push(type), true);</script>"""
PAGES = {
    EDA + "/": _HEAD.format(title="Главная") + """
<header><form role="search" action="/search"><input name="q" placeholder="Найти ресторан или блюдо"></form>
<button aria-haspopup="dialog">Адрес доставки</button></header>
<main><h1>Рестораны</h1>
<div class="tile" onclick="location.href='/r/medovic'"><b>Medovic</b> <small>Кондитерская · 30 мин</small></div>
<a href="/r/burger?utm=track-secret">Burger Place</a> <a href="https://partner.example/promo">Партнёр</a></main>
</body></html>""",
    EDA + "/search": _HEAD.format(title="Поиск") + """
<main><h1>Результаты поиска</h1><a href="/r/medovic">Medovic</a> <a href="/r/burger">Burger Place</a></main>
</body></html>""",
    EDA + "/r/medovic": _HEAD.format(title="Medovic") + """
<main><h1>Medovic</h1>
<div class="card" id="c1"><h3>Медовик</h3><p>250 г · 2 500 ₸</p><button class="add">Добавить</button></div>
<div class="card" id="c2"><h3>Наполеон</h3><p>200 г · 2 200 ₸</p><button class="add">Добавить</button></div>
<div class="card" id="c3"><h3>Эклер</h3><p>Нет в наличии</p><button class="add" disabled>Добавить</button></div>
</main>
<aside aria-label="Корзина"><div id="cart">Корзина пуста</div></aside>
<form id="login"><input type="password" autocomplete="current-password" aria-label="Пароль">
<button type="submit">Войти</button></form>
<script>
for (const button of document.querySelectorAll('.add')) {
  button.addEventListener('click', () => {
    const dish = button.closest('.card').querySelector('h3').textContent;
    document.getElementById('cart').textContent = 'Корзина: ' + dish + ' × 1';
  });
}
</script></body></html>""",
    EDA + "/r/layout": _HEAD.format(title="Меню") + """
<main><h1>Меню</h1><div class="card"><h3>Котлеты с гречкой</h3>
<div class="footer"><span>5 500 ₸</span><button>Добавить</button><button>Подробнее</button></div></div>
<div id="ordinary-cart"><div><h3>Корзина</h3><span>16 позиций</span></div><p>Котлеты с гречкой × 1</p>""" +
    "".join(f"<h4>Позиция {n}</h4><p>Длинная запись заказа для снимка контейнера</p>" for n in range(15)) +
    """<p aria-hidden="true" id="painted-row">Сумма: 500 ₸</p>""" +
    """</div><section><h3>Информация</h3><p>Открытый раздел</p></section>
<div id="pointer-card" style="cursor:pointer" onclick="window.__selected = true"><h1>Другое блюдо</h1>
<button>Добавить</button><button>Подробнее</button></div>
<article id="restaurant-tile" style="cursor:pointer" onclick="window.__restaurant = true">Ресторан без кнопки</article>
</main></body></html>""",
    EDA + "/overlay": _HEAD.format(title="Оверлей") + """
<main><button id="under">Под слоем</button></main>
<div style="position:fixed;inset:0;background:rgba(0,0,0,.3)"></div></body></html>""",
    EDA + "/modal": _HEAD.format(title="Модальное окно") + """
<main><button>Фоновая кнопка</button></main>
<div role="dialog" aria-modal="true" aria-label="Выбор адреса" style="position:fixed;top:40px;left:40px;background:#fff">
<button>Дом</button><button>Работа</button><button aria-label="Закрыть">×</button></div></body></html>""",
    EDA + "/private": _HEAD.format(title="Оформление") + """
<main><h1>Оформление</h1>
<label>Адрес <input id="address" value="Абая 1 secret-address"></label>
<label>Комментарий <textarea>leave at door secret-note</textarea></label>
<div contenteditable="true" aria-label="Заметка">typed secret-editable</div>
<label>Карта <select><option>Наличные</option><option selected>Visa secret-option</option></select></label>
<div role="textbox ">secret-custom-field</div>
<form><label>Пароль <input type="password" value="hunter2-secret"></label>
<label>Код <input autocomplete="one-time-code" inputmode="numeric" value="424242"></label>
<label>Номер карты <input inputmode="numeric" value="4111111111111111"></label>
<button type="button">Продолжить</button></form></main>
<script>document.cookie = 'session=cookie-secret; path=/';
localStorage.setItem('token', 'storage-secret'); sessionStorage.setItem('t', 'storage-secret');</script>
</body></html>""",
    EDA + "/long": _HEAD.format(title="Меню") + _INPUT_SPY + """
<main><h1>Меню</h1>""" + "".join(f"<div class='card'><h3>Блюдо {n}</h3><button>+</button></div>"
                                  for n in range(60)) + "</main></body></html>",
    "https://passport.yandex.kz/auth": _HEAD.format(title="Яндекс ID — вход") + """
<main><h1>Войдите с Яндекс ID</h1><form><input name="login" value="owner-login">
<input type="password" value="hunter2-secret"><button>Войти</button></form></main></body></html>""",
}


def _session(state: Path) -> core.Session:
    profile = core.prepare_profile(state)
    return core.Session(lambda: driver_module.PlaywrightDriver(profile, fixtures=PAGES, headless=True, channel="chrome"),
                        state_dir=state)


@unittest.skipUnless(ENABLED, "opt-in: set YANDEX_EATS_BROWSER_FIXTURE=1 with Playwright + Chromium installed")
class HeadlessFixtureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = _session(Path(self._tmp.name))
        opened = self.session.handle("open", {})
        assert opened["status"] == "ok", opened

    def tearDown(self):
        self.session.close()
        self._tmp.cleanup()

    def go(self, url, **observe):
        self.session._driver.goto(url if url.startswith("https://") else EDA + url)
        result = self.session.handle("observe", observe)
        return result["observation"] if result["status"] == "ok" else result

    def act(self, observation, element_id, action="click", **extra):
        args = {"action": action, "observation_id": observation["observation_id"], "element_id": element_id}
        if action == "click":
            args["intent"] = extra.pop("intent", "choose")
        return self.session.handle("act", {**args, **extra})

    @staticmethod
    def find(observation, predicate):
        matches = [item for item in observation["elements"] if predicate(item)]
        assert len(matches) == 1, (matches, observation["elements"])
        return matches[0]

    def test_home_search_and_choose_result(self):
        home = self.go("/")
        self.assertEqual(home["mode"], "agent_can_act")
        tile = self.find(home, lambda item: item["role"] == "clickable" and "Medovic" in item["name"])
        self.assertTrue(tile["id"].startswith("e"))
        burger = self.find(home, lambda item: item["name"] == "Burger Place")
        self.assertEqual(burger["href"], EDA + "/r/burger")
        external = self.find(home, lambda item: item["name"] == "Партнёр")
        self.assertEqual(external["href"], "external:partner.example")
        refused = self.act(home, external["id"], intent="open")
        self.assertEqual((refused["code"], refused["effect"]), ("link_leaves_service", "none"))

        searched = self.session.handle("search", {"query": "Medovic"})
        self.assertEqual(searched["status"], "done", searched)
        self.assertTrue(searched["navigated"])
        self.assertEqual(searched["observation"]["url"], EDA + "/search")  # the ?q= query is not returned
        results = searched["observation"]
        link = self.find(results, lambda item: item["role"] == "link" and item["name"] == "Medovic")
        opened = self.act(results, link["id"], intent="open")
        self.assertEqual(opened["status"], "done", opened)
        self.assertEqual(opened["observation"]["url"], EDA + "/r/medovic")

    def test_restaurant_add_ambiguity_disabled_and_sensitive(self):
        menu = self.go("/r/medovic")
        ambiguous = self.session.handle("act", {"action": "click", "observation_id": menu["observation_id"],
                                                "target_name": "Добавить", "intent": "add_item"})
        self.assertEqual((ambiguous["code"], len(ambiguous["candidates"])), ("ambiguous", 3))
        eclair = self.find(menu, lambda item: "Эклер" in item.get("in", ""))
        self.assertIn("disabled", eclair["state"])
        self.assertEqual(self.act(menu, eclair["id"], intent="add_item")["code"], "element_disabled")
        password = self.find(menu, lambda item: item["name"] == "Пароль")
        self.assertIn("owner_only", password["state"])
        self.assertEqual(self.act(menu, password["id"], action="fill", text="x")["code"], "sensitive_field")
        login = self.find(menu, lambda item: item["name"] == "Войти")
        self.assertEqual(self.act(menu, login["id"], intent="open")["code"], "sensitive_form")

        honey = self.find(menu, lambda item: "Медовик" in item.get("in", ""))
        added = self.act(menu, honey["id"], intent="add_item")
        self.assertEqual((added["status"], added["effect"]), ("done", "performed"), added)
        self.assertIn("Корзина: Медовик × 1", added["observation"]["text"])
        self.assertEqual(added["action"]["element"]["in"], honey["in"])

    def test_price_footer_keeps_dish_identity_and_plain_div_cart_is_captureable(self):
        menu = self.go("/r/layout", scope="page")
        add = self.find(menu, lambda item: item["name"] == "Добавить" and
                        "Котлеты с гречкой" in item.get("in", ""))
        self.assertIn("Котлеты с гречкой", add["in"])
        cart = self.find(menu, lambda item: item["role"] == "region" and
                         "Котлеты с гречкой × 1" in item["name"])
        self.assertEqual(self.act(menu, cart["id"], intent="open")["code"], "capture_only_region")
        captured = self.session.handle("capture", {"observation_id": menu["observation_id"],
                                                    "element_id": cart["id"]})
        self.assertEqual(captured["status"], "captured_private", captured)
        self.assertEqual(Path(captured["path"]).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        from struct import unpack
        page = self.session._driver._page
        box = page.locator("#ordinary-cart").bounding_box()
        row = page.locator("#ordinary-cart p").last.bounding_box()
        png = Path(captured["path"]).read_bytes()
        _width, height = unpack(">II", png[16:24])  # PNG IHDR dimensions
        self.assertGreaterEqual(height, row["y"] + row["height"] - box["y"] - 1)
        section = self.find(menu, lambda item: item["role"] == "region" and item["name"].startswith("Информация"))
        self.assertEqual(self.act(menu, section["id"], intent="open")["code"], "capture_only_region")
        buttons = [item for item in menu["elements"] if item["name"] == "Добавить"]
        self.assertTrue(any("Другое блюдо" in item.get("in", "") for item in buttons))
        card = self.find(menu, lambda item: item["name"].startswith("Другое блюдо") and
                         item["role"] == "clickable")
        self.assertEqual(self.act(menu, card["id"], intent="choose")["status"], "done")
        self.assertTrue(page.evaluate("() => window.__selected"))
        latest = self.session.handle("observe", {"scope": "page"})["observation"]
        restaurant = self.find(latest, lambda item: item["name"] == "Ресторан без кнопки")
        self.assertEqual(restaurant["role"], "clickable")
        self.assertEqual(self.act(latest, restaurant["id"], intent="open")["status"], "done")
        self.assertTrue(page.evaluate("() => window.__restaurant"))

    def test_capture_div_can_become_clickable_on_a_fresh_observation(self):
        first = self.go("/r/layout", scope="page")
        cart = self.find(first, lambda item: item["role"] == "region" and
                         "Котлеты с гречкой × 1" in item["name"])
        page = self.session._driver._page
        page.evaluate("""() => { const cart = document.getElementById('ordinary-cart');
          cart.style.cursor = 'pointer'; cart.onclick = () => window.__cartClicked = true; }""")
        second = self.session.handle("observe", {"scope": "page"})["observation"]
        current = self.find(second, lambda item: item["id"] == cart["id"])
        self.assertEqual(current["role"], "clickable")
        capture = self.session.handle("capture", {"observation_id": second["observation_id"],
                                                  "element_id": current["id"]})
        self.assertEqual((capture["status"], capture["code"]), ("refused", "capture_only_region"))
        self.assertEqual(self.act(second, current["id"], intent="choose")["status"], "done")
        self.assertTrue(page.evaluate("() => window.__cartClicked"))

    def test_lower_cart_row_change_invalidates_region_capture(self):
        menu = self.go("/r/layout", scope="page")
        cart = self.find(menu, lambda item: item["role"] == "region" and
                         "Котлеты с гречкой × 1" in item["name"])
        page = self.session._driver._page
        page.evaluate("""() => { document.querySelector('#ordinary-cart p:last-child').textContent =
          'Позиция 14: количество стало 2'; }""")
        result = self.session.handle("capture", {"observation_id": menu["observation_id"],
                                                 "element_id": cart["id"]})
        self.assertEqual((result["status"], result["code"]), ("refused", "stale_element"))
        self.assertEqual(list((self.session._state_dir / "captures").glob("*.png")), [])

    def test_painted_aria_hidden_cart_row_still_invalidates_capture(self):
        menu = self.go("/r/layout", scope="page")
        cart = self.find(menu, lambda item: item["role"] == "region" and
                         "Котлеты с гречкой × 1" in item["name"])
        self.session._driver._page.evaluate("""() => {
          document.getElementById('painted-row').textContent = 'Сумма: 950 ₸'; }""")
        result = self.session.handle("capture", {"observation_id": menu["observation_id"],
                                                 "element_id": cart["id"]})
        self.assertEqual((result["status"], result["code"]), ("refused", "stale_element"))

    def test_capture_selected_cart_region_is_private_and_session_bound(self):
        menu = self.go("/r/medovic")
        viewport = self.session.handle("capture", {"observation_id": menu["observation_id"]})
        self.assertEqual(viewport["status"], "captured_private", viewport)
        self.assertEqual(Path(viewport["path"]).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        cart = self.find(menu, lambda item: item["name"] == "Корзина")
        result = self.session.handle("capture", {"observation_id": menu["observation_id"],
                                                 "element_id": cart["id"]})
        self.assertEqual(result["status"], "captured_private", result)
        self.assertEqual(Path(result["path"]).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertNotIn("image_base64", result)
        self.assertEqual(self.session.handle("capture", {"observation_id": "old", "element_id": cart["id"]})["code"],
                         "stale_observation")
        self.go("https://passport.yandex.kz/auth")
        self.assertEqual(self.session.handle("capture", {"observation_id": menu["observation_id"],
                                                          "element_id": cart["id"]})["code"], "outside_service")

    def test_new_tab_becomes_current_without_relaunching_chrome(self):
        home = self.go("/")
        old_id = home["observation_id"]
        page = self.session._driver._page
        with page.expect_popup() as opened:
            page.evaluate("url => window.open(url, '_blank')", EDA + "/r/medovic")
        opened.value.wait_for_load_state("domcontentloaded")
        self.assertTrue(self.session._driver.alive())
        current = self.session.handle("observe", {})
        self.assertEqual(current["status"], "ok", current)
        self.assertEqual(current["observation"]["url"], EDA + "/r/medovic")
        self.assertNotEqual(current["observation"]["observation_id"], old_id)
        self.assertEqual(self.session.launches, 1)
        self.session._driver._page.close()

    def test_action_opening_new_tab_returns_that_tab_without_extra_observe(self):
        home = self.go("/")
        page = self.session._driver._page
        page.evaluate("""() => { const button = document.createElement('button');
          button.textContent = 'Открыть меню';
          button.onclick = () => window.open('/r/medovic', '_blank');
          document.body.appendChild(button); }""")
        fresh = self.session.handle("observe", {})["observation"]
        button = self.find(fresh, lambda item: item["name"] == "Открыть меню")
        result = self.act(fresh, button["id"], intent="open")
        self.assertEqual(result["status"], "done", result)
        self.assertTrue(result["navigated"])
        self.assertEqual(result["observation"]["url"], EDA + "/r/medovic")
        self.assertNotEqual(result["observation"]["observation_id"], home["observation_id"])

    def test_changed_button_wording_uses_fresh_card_evidence(self):
        for label in ("+", "В корзину", "Add"):
            with self.subTest(label=label):
                self.session._driver.goto(EDA + "/r/medovic")
                self.session._driver._page.evaluate(
                    "label => { document.querySelector('#c1 button').textContent = label; }", label)
                menu = self.session.handle("observe", {})["observation"]
                honey = self.find(menu, lambda item: "Медовик" in item.get("in", ""))
                result = self.act(menu, honey["id"], intent="add_item")
                self.assertEqual(result["status"], "done", result)
                self.assertIn("Корзина: Медовик × 1", result["observation"]["text"])

    def test_rerendered_renamed_or_navigated_nodes_are_stale(self):
        menu = self.go("/r/medovic")
        page = self.session._driver._page
        napoleon = self.find(menu, lambda item: "Наполеон" in item.get("in", ""))
        page.evaluate("() => { const card = document.getElementById('c2');"
                      " card.replaceWith(card.cloneNode(true)); }")
        replaced = self.act(menu, napoleon["id"], intent="add_item")
        self.assertEqual((replaced["code"], replaced["effect"]), ("stale_element", "none"))
        fresh = replaced["observation"]
        honey = self.find(fresh, lambda item: "Медовик" in item.get("in", ""))
        page.evaluate("() => { document.querySelector('#c1 button').textContent = 'В корзину'; }")
        renamed = self.act(fresh, honey["id"], intent="add_item")
        self.assertEqual((renamed["code"], renamed["effect"]), ("stale_element", "none"))
        self.assertIn("В корзину", renamed["reason"])
        self.assertNotIn("Медовик × 1", page.inner_text("body"))

        latest = renamed["observation"]
        self.session._driver.goto(EDA + "/search")
        moved = self.act(latest, honey["id"], intent="add_item")
        self.assertEqual((moved["code"], moved["effect"]), ("stale_observation", "none"))
        self.assertEqual(moved["observation"]["url"], EDA + "/search")

    def test_covered_and_modal(self):
        overlay = self.go("/overlay")
        under = self.find(overlay, lambda item: item["name"] == "Под слоем")
        self.assertIn("covered", under["state"])
        self.assertEqual(self.act(overlay, under["id"])["code"], "element_covered")

        modal = self.go("/modal")
        self.assertEqual(modal["modal"], "Выбор адреса")
        self.assertEqual([item["name"] for item in modal["elements"]], ["Дом", "Работа", "Закрыть"])

    def test_field_values_editable_text_cookies_and_storage_never_leave_the_page(self):
        private = self.go("/private", scope="page")
        dump = json.dumps(private, ensure_ascii=False)
        for secret in SECRETS:
            self.assertNotIn(secret, dump)
        page = self.session._driver._page
        self.assertEqual(page.evaluate("() => localStorage.getItem('token')"), "storage-secret")  # it was there
        names = {item["name"]: item for item in private["elements"]}
        self.assertIn("owner_only", names["Пароль"]["state"])
        self.assertIn("owner_only", names["Код"]["state"])
        self.assertIn("owner_only", names["Номер карты"]["state"])
        refused = self.act(private, names["Номер карты"]["id"], action="fill", text="4111")
        self.assertEqual((refused["code"], refused["effect"]), ("sensitive_field", "none"))
        self.assertIn("Заметка", names)

    def test_yandex_id_page_is_login_required_and_not_read(self):
        result = self.go("https://passport.yandex.kz/auth?track_id=track-secret", scope="page")
        self.assertEqual(result["status"], "login_required", result)
        self.assertEqual(result["page"], {"kind": "yandex_id", "host": "passport.yandex.kz"})
        dump = json.dumps(result, ensure_ascii=False)
        for secret in (*SECRETS, "owner-login", "Яндекс ID — вход", "Войдите", "/auth"):
            self.assertNotIn(secret, dump)
        driver = self.session._driver
        self.assertEqual(driver.observe("__probe", "x.1.1", {"scope": "page"}), {"blocked": True})
        self.assertEqual(driver.inspect("__probe", "x.1.1", 0, {}), {"blocked": True})
        refused = self.session.handle("search", {"query": "Medovic"})
        self.assertEqual((refused["code"], refused["effect"]), ("login_required", "none"))

    def test_scroll_moves_the_view_without_input_events(self):
        before = self.go("/long")
        self.assertEqual(before["scroll"]["y"], 0)
        after = self.session.handle("observe", {"scroll": "down"})
        self.assertEqual((after["status"], after["effect"]), ("ok", "scrolled"))
        self.assertGreater(after["observation"]["scroll"]["y"], 0)
        self.assertEqual(self.session._driver._page.evaluate("() => window.__inputs"), [])


@unittest.skipUnless(ENABLED, "opt-in: set YANDEX_EATS_BROWSER_FIXTURE=1 with Playwright + Chromium installed")
class HeadlessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_live_session_configuration_acts_in_synthetic_chromium(self):
        session = _session(Path(self._tmp.name))
        self.addCleanup(session.close)
        session.handle("open", {})
        session._driver.goto(EDA + "/r/medovic")
        observation = session.handle("observe", {})["observation"]
        self.assertEqual(observation["mode"], "agent_can_act")
        honey = [item for item in observation["elements"] if "Медовик" in item.get("in", "")][0]
        result = session.handle("act", {"action": "click", "observation_id": observation["observation_id"],
                                        "element_id": honey["id"], "intent": "add_item"})
        self.assertEqual((result["status"], result["effect"]), ("done", "performed"))
        self.assertIn("Корзина: Медовик × 1", result["observation"]["text"])

    def test_closed_window_is_session_loss_and_reopening_invalidates_ids(self):
        session = _session(Path(self._tmp.name))
        self.addCleanup(session.close)
        first = session.handle("open", {})
        self.assertTrue(first["launched"])
        session._driver._page.close()
        self.assertEqual(session.handle("observe", {})["status"], "session_lost")
        reopened = session.handle("open", {})
        self.assertTrue(reopened["launched"])
        stale = session.handle("act", {"action": "click", "observation_id": first["observation"]["observation_id"],
                                       "element_id": "e1", "intent": "open"})
        self.assertEqual((stale["code"], stale["effect"]), ("stale_observation", "none"))


if __name__ == "__main__":
    unittest.main()
