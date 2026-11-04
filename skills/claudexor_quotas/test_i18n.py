"""The widget speaks the language chosen in Ouroboros: its English table, the route that serves it and the
translation memory's rendering of it, and the real widget.js drawing Russian labels and plural forms.

What is held here:
- strings_en.json is the one source of the English: valid keys and placeholders, every key the widget asks for is
  in it, and the few phrases the widget keeps for its first screen (MIN_EN) are the table's own;
- the plugin registers the table with the host's translation generator (register_catalog, prefix quotas.ui);
- the i18n route says nothing but English for English, and for a chosen language gives our keys out of the
  translation memory (a text, or the forms of a phrase that counts), reports what is missing once, and falls back
  on the gateway's own routes when the host's modules cannot be imported;
- the real widget, given a Russian answer, draws Russian labels, the plural form of 1 / 3 / 5 accounts and the
  dates in Russian; a phrase the translation lacks stays English; with no text table at all it draws its one
  minimal card and a Retry.
"""
import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

import plugin
import test_quotas
import test_window_tabs as W
from conftest import RU_JSON, RU_JSON_SKIP
from test_reserve import _Host, _node

HERE = Path(__file__).parent
STRINGS = json.loads((HERE / "strings_en.json").read_text(encoding="utf-8"))
WIDGET = (HERE / "widget.js").read_text(encoding="utf-8")
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
KEY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")


def _text(value):
    return value if isinstance(value, str) else value["other"]


def _placeholders(value):
    return set(PLACEHOLDER.findall(_text(value)))


# ---------------------------------------------------------------------------
# the one table


def test_the_english_table_is_valid():
    assert len(STRINGS) > 200
    for key, value in STRINGS.items():
        assert KEY.match(key), key
        if isinstance(value, str):
            assert value.strip(), key
            forms = [value]
        else:
            assert set(value) <= {"one", "other"} and "other" in value, key
            forms = list(value.values())
            # a phrase that counts is counted by {n} in every form, and says the same otherwise
            assert all("{n}" in form for form in forms), key
            assert len({frozenset(PLACEHOLDER.findall(form)) for form in forms}) == 1, key
        for form in forms:
            assert form.strip() == form or form.endswith("…") or form.startswith(" ") is False, key
            assert "<" not in form and ">" not in form, f"{key}: the translation memory refuses markup"
            # only the placeholders the writer of the memory accepts: {name}
            assert len(re.findall(r"[{}]", form)) == 2 * len(PLACEHOLDER.findall(form)), f"{key}: stray brace"


def test_the_table_and_the_widget_agree():
    asked = set(re.findall(r"\bt\('([a-z0-9_.]+)'", WIDGET))
    dynamic = set(re.findall(r"\bt\('([a-z0-9_.]+\.|[a-z0-9_.]+_)' \+", WIDGET))
    literal = {key for key in asked if key not in dynamic}
    assert literal <= set(STRINGS), sorted(literal - set(STRINGS))
    # a key the widget never asks for is dead text, unless it is built from a prefix or chosen in a ternary
    dead = [key for key in STRINGS if key not in asked and not any(key.startswith(prefix) for prefix in dynamic)]
    ternary = set(re.findall(r"\? '([a-z0-9_.]+)' :\s*'([a-z0-9_.]+)'", WIDGET))
    ternary = {key for pair in ternary for key in pair}
    ternary |= set(re.findall(r"t\([^()]*\? t\('([a-z0-9_.]+)'", WIDGET))
    assert not [key for key in dead if key not in ternary and not any(
        re.search(r"'%s'" % re.escape(key), WIDGET) for _ in (0,))], dead


def test_the_phrases_the_widget_keeps_for_its_first_screen_are_the_tables():
    block = re.search(r"var MIN_EN = \{(.*?)\n    \};", WIDGET, re.S).group(1)
    kept = dict(re.findall(r"'([a-z0-9_.]+)': '((?:[^'\\]|\\.)*)'", block))
    assert kept, "MIN_EN not found"
    for key, text in kept.items():
        assert STRINGS[key] == text.replace("\\'", "'"), key


def test_the_plugin_reads_the_one_file():
    assert plugin.english_strings() == plugin.clean_strings(STRINGS) == STRINGS
    rows = plugin.catalog_rows()
    assert set(rows) == set(STRINGS)
    assert rows["n.account"] == "{n} accounts" and rows["btn.refresh"] == "Refresh"
    # a broken file is no phrases, never an exception
    assert plugin.english_strings(HERE / "missing.json") == {}
    assert plugin.clean_strings({"a.b": "x", "c.d": {"one": "1", "other": "n", "few": "?"}, "e.f": 3, "": "x"}) == \
        {"a.b": "x", "c.d": {"one": "1", "other": "n"}}


# ---------------------------------------------------------------------------
# the plugin and the host's translation memory (both replaced by small fakes)


class FakeMemory:
    def __init__(self, tag, doc=None):
        self.tag, self.doc, self.recorded = tag, doc, []

    def current_language(self):
        return self.tag

    def cached_memory(self, _root, _tag):
        return self.doc

    def record_missing(self, _root, tag, items):
        self.recorded.append((tag, items))
        return {"accepted": len(items), "dropped": 0, "pending": len(items)}


class FakeTranslation:
    def __init__(self):
        self.catalogs, self.events = [], []

    def register_catalog(self, prefix, table, note):
        self.catalogs.append((prefix, table, note))

    def on_language_event(self, event, _root, tag):
        self.events.append((event, tag))


def _english(tag):
    return not tag or str(tag).lower().split("-")[0] == "en"


class Host(_Host):
    def get_runtime_info(self):
        return {"server_port": 8765, "data_dir": str(self.state_dir)}


def ru_select():
    """The plural map of Russian as the install records it: select(n) for n in 0..100."""
    def category(n):
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    return {"map": {str(n): category(n) for n in range(101)}, "period": 100}


def ru_doc():
    entry = "code:quotas.ui."
    return {"revision": 12, "plural_select": ru_select(), "entries": {
        entry + "btn.about": {"text": "О виджете", "provenance": "generated"},
        entry + "btn.refresh": {"text": "Обновить", "provenance": "generated"},
        entry + "n.account": {"forms": {"one": "{n} аккаунт", "few": "{n} аккаунта", "many": "{n} аккаунтов",
                                        "other": "{n} аккаунта"}, "provenance": "generated"},
        "code:task.headline.done": {"text": "Готово", "provenance": "generated"},  # the core's, not ours
    }}


@pytest.fixture
def fakes(monkeypatch):
    def install(tag, doc=None):
        memory, translation = FakeMemory(tag, doc), FakeTranslation()
        monkeypatch.setattr(plugin, "_host_i18n", lambda: (memory, translation, _english))
        plugin._REPORTED_MISSES.clear()
        return memory, translation
    return install


def test_the_plugin_registers_its_table_with_the_generator(tmp_path, fakes):
    _memory, translation = fakes("")
    host = Host(tmp_path)
    plugin.register(host)
    assert len(translation.catalogs) == 1
    prefix, table, note = translation.catalogs[0]
    assert prefix == "quotas.ui" and "quota" in note.lower() and len(note) < 400
    assert table() == plugin.catalog_rows() and table()["plate.of"] == "left of {n} accounts"
    assert "i18n" in host.routes


def test_registration_survives_a_host_without_the_generator(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_host_i18n", lambda: (None, None, None))
    host = Host(tmp_path)
    plugin.register(host)  # no exception, and the route is there
    assert "i18n" in host.routes


@pytest.mark.parametrize("tag", ["", "en", "en-GB"])
def test_the_route_says_only_english_for_english(tmp_path, fakes, tag):
    memory, translation = fakes(tag, ru_doc())
    host = Host(tmp_path)
    plugin.register(host)
    answer = host.routes["i18n"]({})
    assert answer["language"] == "" and answer["strings"] == {} and answer["revision"] == 0
    assert answer["en"] == STRINGS
    assert memory.recorded == [] and translation.events == []


def test_the_route_is_english_outside_the_hosts_own_runtime(tmp_path, fakes):
    fakes("ru", ru_doc())
    host = _Host(tmp_path)  # its runtime info has no data_dir: nothing to read a language from
    plugin.register(host)
    answer = host.routes["i18n"]({})
    assert answer["language"] == "" and answer["strings"] == {} and answer["en"] == STRINGS


def test_the_route_gives_the_translation_of_our_keys_only(tmp_path, fakes):
    memory, translation = fakes("ru", ru_doc())
    host = Host(tmp_path)
    plugin.register(host)
    answer = host.routes["i18n"]({})
    assert answer["language"] == "ru" and answer["revision"] == 12
    assert answer["plural_select"]["map"]["3"] == "few"
    assert answer["strings"]["btn.about"] == "О виджете"
    assert answer["strings"]["n.account"] == {"forms": {"one": "{n} аккаунт", "few": "{n} аккаунта",
                                                         "many": "{n} аккаунтов", "other": "{n} аккаунта"}}
    assert set(answer["strings"]) == {"btn.about", "btn.refresh", "n.account"}  # nothing of the core's
    assert answer["en"] == STRINGS


def test_the_reading_carries_the_text_table_only_when_asked_for_it(tmp_path, fakes, monkeypatch):
    # a widget whose text table did not come by the i18n route asks for it with the reading (text=1): the numbers
    # are never held back for the words; a plain reading does not carry it
    from test_passive_quota_read import envelope
    fakes("ru", ru_doc())
    monkeypatch.setattr(plugin, "_fetch_status", lambda _port: (envelope(), ""))
    host = Host(tmp_path)
    plugin.register(host)
    plain = host.routes["quotas"]({"query_params": {"chart": "0"}})
    assert "text" not in plain
    asked = host.routes["quotas"]({"query_params": {"chart": "0", "text": "1"}})
    table = asked["text"]
    assert isinstance(table, dict) and table["language"] == "ru" and table["en"] == STRINGS
    assert table["strings"]["btn.refresh"] == "Обновить"
    assert all(isinstance(value, (str, dict)) for value in table["strings"].values())
    assert "text" not in host.routes["quotas"]({"query_params": {"chart": "0", "text": "0"}})
    assert asked["groups"] == plain["groups"], "the text does not change the reading"


def test_what_is_missing_is_reported_once_and_wakes_the_generator(tmp_path, fakes):
    memory, translation = fakes("ru", ru_doc())
    host = Host(tmp_path)
    plugin.register(host)
    host.routes["i18n"]({})
    assert len(memory.recorded) == 1 and translation.events == [("missing", "ru")]
    tag, items = memory.recorded[0]
    assert tag == "ru" and len(items) == len(STRINGS) - 3
    first = next(item for item in items if item["key"] == "code:quotas.ui.plate.of")
    assert first["context"]["source"] == "left of {n} accounts" and first["context"]["table"] == "quotas.ui"
    host.routes["i18n"]({})  # the same misses are not reported again
    assert len(memory.recorded) == 1 and len(translation.events) == 1


def test_a_language_with_no_memory_yet_is_english_and_queues_everything(tmp_path, fakes):
    memory, translation = fakes("de", None)
    host = Host(tmp_path)
    plugin.register(host)
    answer = host.routes["i18n"]({})
    assert answer["language"] == "de" and answer["strings"] == {} and answer["en"] == STRINGS
    assert len(memory.recorded[0][1]) == len(STRINGS)


def test_a_broken_memory_never_breaks_the_route(tmp_path, fakes):
    memory, _translation = fakes("ru", ru_doc())
    memory.cached_memory = lambda *_: (_ for _ in ()).throw(ValueError("malformed"))
    host = Host(tmp_path)
    plugin.register(host)
    answer = host.routes["i18n"]({})
    assert answer["language"] == "" and answer["strings"] == {} and answer["en"] == STRINGS
    assert any("i18n read failed" in message for _level, message in host.logs) or host.logs == []


def test_without_the_hosts_modules_the_gateways_routes_stand_in(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_host_i18n", lambda: (None, None, None))
    plugin._REPORTED_MISSES.clear()
    calls = []

    def fake_request(port, path, method="GET", timeout_sec=0, payload=None):
        calls.append((method, path, payload))
        if path == "/api/ui/i18n":
            return {"language": "ru", "english": False, "revision": 5, "plural_select": ru_select(),
                    "entries": ru_doc()["entries"]}, "", 200
        return {"accepted": 1}, "", 200

    monkeypatch.setattr(plugin, "_request_json", fake_request)
    host = Host(tmp_path)
    plugin.register(host)
    answer = host.routes["i18n"]({})
    assert answer["language"] == "ru" and answer["revision"] == 5 and answer["strings"]["btn.refresh"] == "Обновить"
    posted = [c for c in calls if c[0] == "POST"]
    assert len(posted) == 1 and posted[0][1] == "/api/ui/i18n/missing"
    assert posted[0][2]["language"] == "ru" and posted[0][2]["items"][0]["key"].startswith("code:quotas.ui.")


# ---------------------------------------------------------------------------
# the real widget.js, in the fake DOM of the other widget tests

NODE_I18N = r"""
(async () => {
  const views = JSON.parse(process.env.I18N_VIEWS);
  const out = {};
  for (const [name, view] of Object.entries(views)) {
    const env = await boot(view);
    const texts = walk(env.root).filter((n) => n._text).map((n) => n._text);
    const pick = (re) => texts.filter((t) => re.test(t));
    // About and Refresh are text actions (the kit's .btn-word): the word that is seen is the button's name, the one word of each,
    // busy or idle; what a reader hears leaves out a node that is aria-hidden.
    const heard = (node) => (node.getAttribute('aria-hidden') === 'true' ? '' : (node._text || '') + node.childNodes.map(heard).join(''));
    const named = (key) => { const b = byFocus(env.root, key); return b ? [heard(b)] : []; };
    out[name] = {
      about: named('about'), refresh: named('refresh'),
      of: classes(env.root, 'of').map((n) => n.textContent),
      foot: classes(env.root, 'wt-foot').filter((n) => !/wt-foot-hold/.test(n.className)).map((n) => n.textContent),
      // The word over the bars is a .btn-word now (its first letter in a cap span), so its text is read as a name, like About and Refresh, not as one text node.
      accounts: named('accounts'), reset: classes(env.root, 'nx').map((n) => n.textContent),
      lowest: classes(env.root, 'rs-tight').map((n) => n.textContent), legend: classes(env.root, 'lg').map((n) => n.textContent),
      status: classes(env.root, 'reserve-age').map((n) => n.textContent),
      cards: classes(env.root, 'empty-card').map((n) => n.textContent),
      retry: !!byFocus(env.root, 'retry'),
      chart: classes(env.root, 'l-chart').map((n) => heard(n)),
    };
  }
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exitCode = 1; });
"""

RU = {
    "btn.about": "О виджете", "btn.refresh": "Обновить",
    "plate.of": {"forms": {"one": "осталось из {n} аккаунта", "few": "осталось из {n} аккаунтов", "many": "осталось из {n} аккаунтов",
                           "other": "осталось из {n} аккаунта"}},
    "n.account": {"forms": {"one": "{n} аккаунт", "few": "{n} аккаунта", "many": "{n} аккаунтов", "other": "{n} аккаунта"}},
    "acc.title": "Аккаунты · {summary}", "acc.word": "Аккаунты · {n}", "plate.chart": "График",
    "reset.next": "следующий сброс {when}",
    "len.week": "Недельный",
}


def _views(tmp_path):
    out = {}
    for count in (1, 3, 5):
        ids = tuple(f"s{i}" for i in range(count))
        out[f"n{count}"] = W._family(tmp_path / f"f{count}", "codex", lambda now, i: [
            W._row(now, "codex:primary", "codex primary", .3 + .05 * i, W.WEEK)], accounts=ids)
    return out


def _run(tmp_path, answer, views=None):
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    harness = harness.replace("const isI18n =", "Object.assign(I18N_ANSWER, JSON.parse(process.env.I18N_OVERRIDE));\nconst isI18n =", 1)
    widget = HERE / "widget.js"
    result = subprocess.run(
        [str(node), "-e", textwrap.dedent(harness + NODE_I18N)], cwd=HERE,
        env={**os.environ, "WIDGET_PATH": str(widget), "WIDGET_ROUTE_PREFIX": test_quotas._widget_route_prefix(widget),
             "I18N_OVERRIDE": json.dumps(answer), "I18N_VIEWS": json.dumps(views or _views(tmp_path))},
        text=True, capture_output=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_real_widget_draws_russian_labels_and_the_plural_forms(tmp_path):
    seen = _run(tmp_path, {"language": "ru", "revision": 4, "plural_select": ru_select(), "strings": RU})
    one, three, five = seen["n1"], seen["n3"], seen["n5"]
    assert one["about"] == ["О виджете"] and one["refresh"] == ["Обновить"]
    # the plural form is the language's: 1 / 3 / 5
    assert [x["of"][0].strip() for x in (one, three, five)] == [
        "осталось из 1 аккаунта", "осталось из 3 аккаунтов", "осталось из 5 аккаунтов"]
    # the word over the bars carries the bare count now ("Accounts · N", key acc.word): the plural forms stand in the line above
    assert [x["accounts"][0] for x in (one, three, five)] == [
        "Аккаунты · 1", "Аккаунты · 3", "Аккаунты · 5"]
    # the plate's one control is the word, in the install's language, and nothing else of the old pill is drawn
    assert one["chart"] == ["График"] and three["chart"] == ["График"] and five["chart"] == ["График"]
    assert one["lowest"] == [] and three["lowest"] == [] and five["lowest"] == [], "the badge 'lowest left' is gone from the plate"
    # the date is the browser's own, in Russian: a short weekday, the day, a short month, the hour
    assert re.match(r"следующий сброс [а-я]{2} \d{1,2} [а-я]+ \d\d:\d\d", three["reset"][0]), three["reset"]


def test_a_phrase_the_translation_lacks_stays_english(tmp_path):
    partial = {key: value for key, value in RU.items() if key != "btn.refresh"}
    seen = _run(tmp_path, {"language": "ru", "revision": 1, "plural_select": None, "strings": partial})
    assert seen["n3"]["about"] == ["О виджете"] and seen["n3"]["refresh"] == ["Refresh"]
    # no plural map recorded: the browser's own rules choose the form
    assert seen["n3"]["accounts"] == ["Аккаунты · 3"]


def test_english_and_a_garbled_translation_leave_the_screen_in_english(tmp_path):
    english = _run(tmp_path, {"language": "", "revision": 0, "plural_select": None, "strings": {}})
    assert english["n3"]["about"] == ["About"] and english["n3"]["of"][0].strip() == "left of 3 accounts"
    assert english["n1"]["of"][0].strip() == "left of 1 account" and english["n3"]["accounts"] == ["Accounts · 3"]
    garbled = _run(tmp_path, {"language": "ru", "revision": 2, "plural_select": {"map": "x"},
                              "strings": {"btn.about": 5, "plate.of": {"forms": 3}, "n.account": {"forms": {}}}})
    assert garbled["n3"]["about"] == ["About"] and garbled["n3"]["of"][0].strip() == "left of 3 accounts"


def _foot_views(tmp_path):
    """Codex Weekly, three accounts, the first two not read now (last known): both read half a minute ago, both 3 h ago, and one 3 h and one 5 h ago."""
    def family(name, *steps):
        view = W._family(tmp_path / name, "codex", lambda now, i: [W._row(now, "codex:primary", "codex primary", .3 + .05 * i, W.WEEK)],
                         accounts=("s0", "s1", "s2"), name=name)
        for last, ago in steps:
            view = W._partial(view, str(W.WEEK), last=last, ago=ago)
        return view
    return {"now": family("now", (("s0", "s1"), 30)), "same": family("same", (("s0", "s1"), 3 * 3600)),
            "mixed": family("mixed", (("s0",), 3 * 3600), (("s1",), 5 * 3600))}


def test_the_note_says_read_just_now_under_two_minutes_and_read_up_to_when_the_ages_differ_in_english(tmp_path):
    seen = _run(tmp_path, {"language": "", "revision": 0, "plural_select": None, "strings": {}}, views=_foot_views(tmp_path))
    # a reading under two minutes old: "read just now", never "just now ago"
    assert re.search(r"No fresh data · 2 read just now · s0, s1", seen["now"]["foot"][0]), seen["now"]["foot"]
    assert "ago" not in seen["now"]["foot"][0]
    # the same age said the same: "read 3 h ago"; ages that are said differently: "read up to 5 h ago" (the oldest)
    assert re.search(r"No fresh data · 2 read 3 h ago · s0, s1", seen["same"]["foot"][0]), seen["same"]["foot"]
    assert re.search(r"No fresh data · 2 read up to 5 h ago · s0, s1", seen["mixed"]["foot"][0]), seen["mixed"]["foot"]


def test_the_note_says_it_in_russian_too(tmp_path):
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    entries = json.loads(RU_JSON.read_text(encoding="utf-8"))["entries"]
    strings = {key: entries[MEMORY_PREFIX + key]["text"]
               for key in ("foot.not_read", "foot.read_ago", "foot.read_just_now", "foot.read_up_to", "age.h")}
    seen = _run(tmp_path, {"language": "ru", "revision": 4, "plural_select": ru_select(), "strings": strings}, views=_foot_views(tmp_path))
    assert re.search(r"Нет свежих данных · 2 читали только что · s0, s1", seen["now"]["foot"][0]), seen["now"]["foot"]
    assert "назад" not in seen["now"]["foot"][0]
    assert re.search(r"Нет свежих данных · 2 читали 3 ч назад · s0, s1", seen["same"]["foot"][0]), seen["same"]["foot"]
    assert re.search(r"Нет свежих данных · 2 читали до 5 ч назад · s0, s1", seen["mixed"]["foot"][0]), seen["mixed"]["foot"]


def test_without_a_text_table_the_widget_draws_its_one_card_and_a_retry(tmp_path):
    seen = _run(tmp_path, {"language": "", "revision": 0, "plural_select": None, "strings": {}, "en": {}},
                views={"n1": _views(tmp_path)["n1"]})
    card = seen["n1"]
    assert card["about"] == [] and card["retry"] is True
    assert "Claudexor Quotas" in card["cards"][0] and "could not be loaded" in card["cards"][0]


# ---------------------------------------------------------------------------
# The words of the window row, its stamp and the footnote under it: in the English table and in the Russian memory.

# The Russian memory is made by the install's translation generator and is not part of the skill: conftest.RU_JSON points at it
# (QUOTAS_RU_JSON) and the Russian checks are skipped, with the reason given, when it is not set.
MEMORY_PREFIX = "code:quotas.ui."
WINDOW_ROW_WORDS = [
    "win.row", "win.hidden_in_rows", "win.none", "win.avg", "win.avg_read", "win.applies",
    "win.short.h", "win.short.d", "win.short.m", "win.short.none",
    "cycle.note", "scope.row", "account.some", "status.unread", "status.quota_unread",
    "stamp.read_word", "stamp.read_of", "stamp.title", "stamp.title_failed", "stamp.title_quota", "stamp.title_none",
    "foot.not_read", "foot.read_ago", "foot.read_just_now", "foot.read_up_to", "foot.more", "foot.more_label", "foot.why_not_read", "foot.why_unreadable", "foot.why_sources_disagree",
    "foot.why_reset_passed", "foot.why_too_old", "foot.why_no_reading_in_answer"]


def _forms_of(value):
    return list(value.values()) if isinstance(value, dict) else [value]


def test_the_words_of_the_window_row_the_stamp_and_the_footnote_are_in_the_english_table_and_the_widget_asks_for_them():
    for key in WINDOW_ROW_WORDS:
        assert key in STRINGS, key
        assert ("t('%s'" % key) in WIDGET or ("'%s'" % key) in WIDGET, "the widget never asks for %s" % key
    # The stamp and the footnote say what they say: the count, the star's words, the reasons (each reason is a key of FOOT_WHY).
    # The stamp is three short lines in the narrow rail: the word, the count, the age. The rail says a window in one short word.
    assert STRINGS["stamp.read_word"] == "Read" and STRINGS["stamp.read_of"] == "{n} of {m}" and "stamp.read" not in STRINGS
    assert STRINGS["win.short.h"] == "{n}H" and STRINGS["win.short.d"] == "{n}D" and STRINGS["win.short.m"] == "{n}M"
    # (the note's head says it in the plate's own words: "No fresh data · 3", and a group of readings that are old says "read 4 days ago")
    assert STRINGS["foot.not_read"] == "No fresh data · {n}" and STRINGS["foot.more"] == "{n} more"
    assert STRINGS["foot.read_ago"] == "read {age} ago"
    # ...a reading under two minutes old is "read just now" (never "just now ago"), and accounts read at different ages are "read up to <the oldest> ago"
    assert STRINGS["foot.read_just_now"] == "read just now" and STRINGS["foot.read_up_to"] == "read up to {age} ago"
    # the number over the plate says what it is: what is left, of how many accounts
    assert STRINGS["plate.of"] == {"one": "left of {n} account", "other": "left of {n} accounts"}
    assert STRINGS["foot.more_label"] == "{n} more accounts — open the account list"
    assert STRINGS["stamp.title"] == "Accounts of this window read: {n} of {m} · {observed}"
    assert {STRINGS[key] for key in WINDOW_ROW_WORDS if key.startswith("foot.why_")} == {
        "not read", "unreadable", "sources disagree", "window reset after the reading", "reading older than the window", "not in the answer"}
    for reason in re.findall(r"(?:not_read|unreadable|sources_disagree|reset_passed|too_old|no_reading_in_answer): 'foot\.(why_[a-z_]+)'", WIDGET):
        assert "foot." + reason in STRINGS, reason
    # What the widget passes is what the phrase has: {n} {m} for the count, {observed} for the title, {when} for the failed read.
    for key, params in (("stamp.read_of", {"n", "m"}), ("stamp.read_word", set()), ("win.short.h", {"n"}), ("win.short.d", {"n"}),
                        ("win.short.m", {"n"}), ("win.short.none", set()),
                        ("stamp.title", {"n", "m", "observed"}), ("status.unread", {"when"}),
                        ("status.quota_unread", {"when"}), ("win.avg_read", {"name", "avg", "read", "n"}), ("win.applies", {"m", "n"}),
                        ("foot.more", {"n"}), ("foot.more_label", {"n"}), ("win.none", {"name"}), ("win.avg", {"name", "avg"}),
                        ("cycle.note", {"family"}), ("foot.not_read", {"n"}), ("foot.read_ago", {"age"}), ("foot.read_up_to", {"age"}), ("foot.read_just_now", set()),
                        # the words of the top of the capsule: the bubble at the limit, the credits (the fact's caption is the window's name alone)
                        ("plate.at_limit", {"n"}), ("plate.back", {"in"}),
                        ("plate.over_plan", {"amount"}), ("credits.family", set()), ("credits.family_n", {"count"}),
                        ("credits.on_accounts", {"n"}), ("cycle.period", set())):
        assert set(PLACEHOLDER.findall(" ".join(_forms_of(STRINGS[key])))) == params, key
    # English left in the widget beside the row would be the old defect: the labels of the row and of the scope go through t().
    for gone in ("'Limit window'", "'Limit scope'", "'avg left'", "'no current reading'", "'hidden in Rows'"):
        assert gone not in WIDGET.split("function windowRow(")[1].split("function scopeRow(")[0], gone


def test_the_russian_memory_has_the_same_phrases_as_the_english_table_with_the_same_placeholders_and_the_plural_forms():
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    memory = json.loads(RU_JSON.read_text(encoding="utf-8"))
    assert memory["language"] == "ru" and isinstance(memory["entries"], dict)
    entries = {key[len(MEMORY_PREFIX):]: value for key, value in memory["entries"].items()}
    assert len(entries) == len(memory["entries"]) and set(entries) == set(STRINGS), (
        sorted(set(STRINGS) - set(entries)), sorted(set(entries) - set(STRINGS)))
    for key, english in STRINGS.items():
        entry = entries[key]
        russian = list(entry["forms"].values()) if "forms" in entry else [entry["text"]]
        assert all(form.strip() for form in russian), key
        assert set(PLACEHOLDER.findall(" ".join(russian))) == set(PLACEHOLDER.findall(" ".join(_forms_of(english)))), key
        # the memory remembers the English it translated: a changed English is a stale translation, told by this test
        if "text" in entry and isinstance(english, str):
            assert entry.get("source") == english, "%s: the memory translated another English: %r" % (key, entry.get("source"))
    # The phrases that count have the forms of Russian (one / few / many / other); the words of the stamp and the footnote are Russian.
    for key in ("win.avg_read", "win.applies"):
        assert set(entries[key]["forms"]) == {"one", "few", "many", "other"}, key
    said = {key: entries[key].get("text") for key in WINDOW_ROW_WORDS if "text" in entries[key]}
    assert said["stamp.read_word"] == "Прочитано" and said["stamp.read_of"] == "{n} из {m}"
    assert said["foot.not_read"] == "Нет свежих данных · {n}" and said["foot.more"] == "ещё {n}"
    assert said["foot.read_ago"] == "читали {age} назад"
    assert said["foot.read_just_now"] == "читали только что" and said["foot.read_up_to"] == "читали до {age} назад"
    assert {form for form in entries["plate.of"]["forms"].values()} == {"осталось из {n} аккаунта", "осталось из {n} аккаунтов"}
    # The short names of the windows: "5ч", "7д", "30д" in Russian (the English "5H", "7D", "30D"), the full name stays in the label and the title.
    assert said["win.short.h"] == "{n}ч" and said["win.short.d"] == "{n}д" and said["win.short.m"] == "{n}мин" and said["win.short.none"] == "н/д"
    assert said["win.row"] == "Окно лимита" and said["scope.row"] == "Область лимита" and said["account.some"] == "аккаунт"
    assert all(re.search(r"[А-Яа-яЁё]", text) for text in said.values()), [k for k, v in said.items() if not re.search(r"[А-Яа-яЁё]", v)]


# The words that came to the plate are in the English table and in the Russian memory; the words that left it are in neither.
PLATE_WORDS_GONE = ["plate.lowest", "plate.show_chart", "plate.hide_chart", "plate.last_known", "plate.age_unknown", "plate.avg_left",
                    "legend.unknown", "cycle.no_share",
                    # the rail has no caption ("average across accounts") and no grey place (a window the family does not report is not drawn)
                    "rail.cap", "win.not_reported", "win.cursor_none",
                    # the fact's caption is the name of the window alone: the word "Remaining" is gone from the plate (the title of the figure keeps its sense)
                    "plate.remaining"]


def test_the_word_chart_and_the_dated_values_line_are_in_both_files_and_the_words_that_left_the_plate_are_in_neither():
    assert STRINGS["plate.chart"] == "Chart" and STRINGS["legend.last"] == "last known"
    assert "wordButton('plate.chart')" in WIDGET and "t('legend.last')" in WIDGET
    for key in PLATE_WORDS_GONE:
        assert key not in STRINGS, key
        assert ("'%s'" % key) not in WIDGET, "the widget still asks for %s" % key
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    memory = json.loads(RU_JSON.read_text(encoding="utf-8"))["entries"]
    ru = {key[len(MEMORY_PREFIX):]: value for key, value in memory.items()}
    assert ru["plate.chart"]["text"] == "График" and ru["plate.chart"]["source"] == "Chart"
    assert ru["legend.last"]["text"] == "по прошлому чтению" and ru["legend.last"]["source"] == "last known"
    assert not [key for key in PLATE_WORDS_GONE if key in ru], "the memory still holds words the plate no longer asks for"


# ---------------------------------------------------------------------------
# The words of the ladder of resets under the rings (kit: rail): in the English table, in the Russian memory, and asked for by the widget.
LADDER_WORDS = ["ladder.title", "ladder.none", "ladder.in_min", "ladder.in_hour", "ladder.in_day", "ladder.in_day_hour",
                "ladder.step", "ladder.more_label", "ladder.less", "ladder.less_label"]


def test_the_words_of_the_ladder_of_resets_are_in_both_files_with_the_same_placeholders_and_the_widget_asks_for_them():
    for key in LADDER_WORDS:
        assert key in STRINGS, key
        assert ("t('%s'" % key) in WIDGET, "the widget never asks for %s" % key
    assert STRINGS["ladder.title"] == "Resets" and STRINGS["ladder.none"] == "not reported"
    # The step says in how long its reset comes, as the names of the windows are written (no space inside the number and its unit): "<1m", "40m", "5h", "2d", "2d 5h";
    # the date that was on the step is gone, with its word; the step is told whole for the ear (the moment, in the reader's local time), with the names of its accounts.
    assert "ladder.in_soon" not in STRINGS and STRINGS["ladder.in_min"] == "{v}m" and STRINGS["ladder.in_hour"] == "{v}h"
    assert STRINGS["ladder.in_day"] == "{v}d" and STRINGS["ladder.in_day_hour"] == "{d}d {h}h"
    assert "ladder.date" not in STRINGS and "'ladder.date'" not in WIDGET, "the date of the step is gone"
    assert STRINGS["plate.back"] == "back in {in}", "the bubble at the limit says in how long, not when"
    assert STRINGS["ladder.step"] == "{when} — {accounts}: {names} · returns {amount} if unused"
    assert STRINGS["ladder.more_label"] == {"one": "{n} more reset — show it below", "other": "{n} more resets — show them below"}
    # What the widget passes is what the phrase has.
    for key, params in (("ladder.title", set()), ("ladder.none", set()), ("ladder.in_min", {"v"}),
                        ("ladder.in_hour", {"v"}), ("ladder.in_day", {"v"}), ("ladder.in_day_hour", {"d", "h"}),
                        ("ladder.step", {"when", "accounts", "names", "amount"}), ("ladder.more_label", {"n"})):
        assert set(PLACEHOLDER.findall(" ".join(_forms_of(STRINGS[key])))) == params, key
    # "N more" of the ladder is the footnote's own phrase (the same two words, the same count).
    assert "t('foot.more', { n: extra || 1 })" in WIDGET and "t('foot.more', { n: left })" in WIDGET
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    memory = json.loads(RU_JSON.read_text(encoding="utf-8"))["entries"]
    ru = {key[len(MEMORY_PREFIX):]: value for key, value in memory.items()}
    for key in LADDER_WORDS:
        assert key in ru, key
    assert ru["ladder.title"]["text"] == "Сбросы" and ru["ladder.none"]["text"] == "не сообщены"
    # In Russian as the windows are named (5ч, 7д): "40м", "5ч", "2д", "2д 5ч" ("<1м" is "<" and "1м": the memory refuses the sign); no forms (the unit is a letter, it does not decline).
    assert [ru[k]["text"] for k in ("ladder.in_min", "ladder.in_hour", "ladder.in_day", "ladder.in_day_hour")] == [
        "{v}м", "{v}ч", "{v}д", "{d}д {h}ч"]
    assert "ladder.date" not in ru, "the memory holds no date of the step"
    assert all("forms" not in ru[k] for k in ("ladder.in_min", "ladder.in_hour", "ladder.in_day", "ladder.in_day_hour"))
    assert set(ru["plate.back"]["forms"]) == {"one", "few", "many", "other"} and all(
        "через {in}" in form for form in ru["plate.back"]["forms"].values()), "the bubble: back in {in}; the verb still follows the count"
    assert ru["ladder.step"]["text"] == "{when} — {accounts}: {names} · вернётся {amount}, если не тратить"
    assert set(ru["ladder.more_label"]["forms"]) == {"one", "few", "many", "other"}
    assert all(re.search(r"[А-Яа-яЁё]", form) for form in ru["ladder.more_label"]["forms"].values())


# ---------------------------------------------------------------------------
# The words a bar opens to (kit: bars): in the English table, in the Russian memory, and asked for by the widget. "back in {in}" and the age phrases are the
# plate's and the note's own (plate.back, foot.read_ago, foot.read_just_now), taken as they are.
BAR_WORDS = ["bar.reset_in", "bar.at_limit", "bar.was_at_limit", "bar.restricted_now", "bar.read", "bar.no_data"]


def test_the_words_a_bar_opens_to_are_in_both_files_with_the_same_placeholders_and_the_widget_asks_for_them():
    for key in BAR_WORDS:
        assert key in STRINGS, key
        assert ("t('%s'" % key) in WIDGET, "the widget never asks for %s" % key
    assert [STRINGS[key] for key in BAR_WORDS] == ["reset in {in}", "at the limit", "was at the limit", "restricted now", "read {age}", "no data"]
    # the bar's lines that say "back in" and "read ... ago" are the plate's and the note's phrases, not new ones
    assert "t('plate.back', { n: 1, 'in': until })" in WIDGET and "t('foot.read_ago', { age: agePhrase(part) })" in WIDGET and "t('foot.read_just_now')" in WIDGET
    assert "bar.back_in" not in STRINGS and "bar.read_ago" not in STRINGS, "the plate's and the note's phrases are taken, no twin of them"
    # what the widget passes is what the phrase has
    for key, params in (("bar.reset_in", {"in"}), ("bar.read", {"age"}), ("bar.at_limit", set()), ("bar.was_at_limit", set()),
                        ("bar.restricted_now", set()), ("bar.no_data", set())):
        assert set(PLACEHOLDER.findall(" ".join(_forms_of(STRINGS[key])))) == params, key
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    memory = json.loads(RU_JSON.read_text(encoding="utf-8"))["entries"]
    ru = {key[len(MEMORY_PREFIX):]: value for key, value in memory.items()}
    for key in BAR_WORDS:
        assert key in ru and ru[key]["source"] == STRINGS[key], key
    assert [ru[key]["text"] for key in BAR_WORDS] == [
        "сброс через {in}", "на лимите", "был на лимите", "ограничен сейчас", "прочитано {age}", "нет данных"]


# ---------------------------------------------------------------------------
# The tiles of "what is left at a few moments ahead": a caption and one tile for each moment. The caption lost its colon (it is
# a caption now, not the start of a line), the moment and the total are one phrase under the number ("in 24 h · of 20"), and the old
# closing "(of {total})" is gone from both files.
TILE_WORDS = ["fact.left_nothing", "fact.left_pace", "fact.tile_when"]
TILE_WORDS_GONE = ["fact.of_total"]


def test_the_words_of_the_checkpoint_tiles_are_in_both_files_with_the_same_placeholders_and_the_widget_asks_for_them():
    for key in TILE_WORDS:
        assert key in STRINGS, key
        assert ("t('%s'" % key) in WIDGET, "the widget never asks for %s" % key
    assert [STRINGS[key] for key in TILE_WORDS] == ["Left if nothing more is used", "Left at recent pace", "{when} · of {total}"]
    assert not STRINGS["fact.left_nothing"].endswith(":") and not STRINGS["fact.left_pace"].endswith(":"), "a caption, not the start of a line"
    # what the widget passes is what the phrase has
    assert _placeholders(STRINGS["fact.tile_when"]) == {"when", "total"}
    assert "t('fact.tile_when', { when: offsetWords(cp.after_seconds), total: chart.y_max })" in WIDGET
    for key in TILE_WORDS_GONE:
        assert key not in STRINGS, key
        assert ("'%s'" % key) not in WIDGET, "the widget still asks for %s" % key
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    memory = json.loads(RU_JSON.read_text(encoding="utf-8"))["entries"]
    ru = {key[len(MEMORY_PREFIX):]: value for key, value in memory.items()}
    for key in TILE_WORDS:
        # the memory remembers the English it translated: a changed English is a stale translation
        assert key in ru and ru[key]["source"] == STRINGS[key], key
    assert [ru[key]["text"] for key in TILE_WORDS] == ["Останется, если больше ничего не тратить", "Останется при текущем темпе", "{when} · из {total}"]
    assert _placeholders(ru["fact.tile_when"]["text"]) == {"when", "total"}
    assert not [key for key in TILE_WORDS_GONE if key in ru], "the memory still holds words the tiles no longer ask for"
