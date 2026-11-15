"""The widget speaks the language chosen in Ouroboros: its English table, the route that serves it and the
translation memory's rendering of it, and the real widget.js drawing Russian labels and plural forms.

What is held here:
- strings_en.json is the one source of the English: valid keys and placeholders, every key the widget asks for is
  in it, and the few phrases the widget keeps for its first screen (MIN_EN) are the table's own;
- the plugin imports no host module and registers nothing with the host outside the PluginAPI: the i18n route reads
  the language and its memory from the gateway's own GET /api/ui/i18n and reports what the memory lacks through the
  gateway's POST /api/ui/i18n/missing (the host writes its queue); a host-global catalog is never touched;
- the i18n route says nothing but English for English, and for a chosen language gives our keys out of the
  translation memory (a text, or the forms of a phrase that counts; an owner's or an import's pin as it is), shows
  English for a generated entry of another English and reports it reworded, reports a missing phrase once, offers a
  batch the host did not take whole once more after MISS_RETRY_SEC, reports a phrase again only after MISS_AGAIN_SEC or
  when the memory shows a loss (Regenerate empties its refusals), and never writes on every reading or on every batch
  the host's translator writes;
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
    assert plugin._english_text(STRINGS["n.account"]) == "{n} accounts" and STRINGS["btn.refresh"] == "Refresh"
    # a broken file is no phrases, never an exception
    assert plugin.english_strings(HERE / "missing.json") == {}
    assert plugin.clean_strings({"a.b": "x", "c.d": {"one": "1", "other": "n", "few": "?"}, "e.f": 3, "": "x"}) == \
        {"a.b": "x", "c.d": {"one": "1", "other": "n"}}


# ---------------------------------------------------------------------------
# the plugin and the gateway's interface-language routes (the gateway replaced by a faithful small fake)

PREFIX = "code:quotas.ui."


def _is_english(tag):
    return not tag or str(tag).lower().split("-")[0] == "en"


class Gateway:
    """The two routes of ouroboros/gateway/ui_i18n.py the plugin calls over loopback (``_request_json``), answered
    as the host answers them: ``GET /api/ui/i18n`` is ``memory_payload`` (the language, the memory's revision, its
    entries and plural map, ``english`` for English, ``memory_error`` for a memory it cannot read); ``POST
    /api/ui/i18n/missing`` is ``record_missing`` behind the gateway's checks: a language that is not the current one
    is a 409, English accepts nothing, at most 200 items are read, a key the memory refused or one the full queue
    cannot take is dropped, a key already queued counts up (accepted) and every accepted call rewrites the queue.
    ``fail`` answers the POSTs with one transport outcome instead."""

    MAX_PENDING = 2000  # i18n_memory.MAX_PENDING

    def __init__(self, language, doc=None, *, refused=(), memory_error=""):
        self.language, self.doc, self.refused, self.memory_error = language, doc, set(refused), memory_error
        self.pending, self.calls, self.writes, self.fail = {}, [], 0, None

    def posts(self):
        return [payload for method, _path, payload in self.calls if method == "POST"]

    def __call__(self, port, path, method="GET", timeout_sec=0, payload=None):
        self.calls.append((method, path, payload))
        english = _is_english(self.language)
        if (method, path) == ("GET", "/api/ui/i18n"):
            doc = None if english or self.memory_error else self.doc
            return {"language": self.language, "chosen": bool(self.language), "english": english,
                    "revision": int(doc["revision"]) if doc else 0, "plural_select": doc.get("plural_select") if doc else None,
                    "entries": doc["entries"] if doc else {}, "memory_error": self.memory_error,
                    "stats": {"pending": len(self.pending), "refused": 0 if english else len(self.refused)}}, "", 200
        if (method, path) == ("POST", "/api/ui/i18n/missing"):
            if self.fail is not None:
                return self.fail
            if payload.get("language") != self.language:
                return None, "HTTP 409 from /api/ui/i18n/missing", 409
            items = payload["items"]
            if english:
                return {"accepted": 0, "dropped": len(items), "pending": 0}, "", 200
            cleaned = [item for item in items[:200] if isinstance(item, dict)]
            accepted = dropped = 0
            for item in cleaned:
                key = str(item.get("key") or "")
                if not key or len(key) > 2000 or key in self.refused:
                    dropped += 1
                elif key in self.pending:
                    self.pending[key]["count"] += 1
                    accepted += 1
                elif len(self.pending) >= self.MAX_PENDING:
                    dropped += 1
                else:
                    self.pending[key] = {"context": item.get("context"), "count": 1}
                    accepted += 1
            self.writes += 1
            return {"accepted": accepted, "dropped": dropped + len(items) - len(cleaned),
                    "pending": len(self.pending)}, "", 200
        raise AssertionError(f"the plugin asked the host for {method} {path}")


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
    return {"revision": 12, "plural_select": ru_select(), "entries": {
        PREFIX + "btn.about": {"text": "О виджете", "provenance": "generated", "source": STRINGS["btn.about"]},
        PREFIX + "btn.refresh": {"text": "Обновить", "provenance": "generated", "source": "Refresh"},
        PREFIX + "n.account": {"forms": {"one": "{n} аккаунт", "few": "{n} аккаунта", "many": "{n} аккаунтов",
                                         "other": "{n} аккаунта"}, "provenance": "generated", "source": "{n} accounts"},
        "code:task.headline.done": {"text": "Готово", "provenance": "generated"},  # the core's, not ours
    }}


@pytest.fixture
def gateway(monkeypatch):
    """Installs a fresh Gateway as the plugin's one way to the host, with this load's miss record empty."""
    def install(language, doc=None, **kwargs):
        fake = Gateway(language, doc, **kwargs)
        monkeypatch.setattr(plugin, "_request_json", fake)
        monkeypatch.setattr(plugin, "_MISSES", {}, raising=False)  # raising=False: the same tests can be run against an older plugin
        return fake
    return install


def test_the_plugin_imports_no_host_module_and_registers_no_host_catalog(tmp_path, gateway, monkeypatch):
    # The plugin's source names no host module, and a load and a reading import none either (a host process has
    # them importable: a fake host translation module here records any registration).
    import builtins
    import sys
    import types
    source = (HERE / "plugin.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+ouroboros\b|import_module\(\s*['\"]ouroboros", source, re.M)
    registered = []
    fake = types.ModuleType("ouroboros.ui_translation")
    fake.register_catalog = lambda *args, **kwargs: registered.append(args)
    fake._EXTRA_CATALOGS = {}
    monkeypatch.setitem(sys.modules, "ouroboros", types.ModuleType("ouroboros"))
    monkeypatch.setitem(sys.modules, "ouroboros.ui_translation", fake)
    imported = []
    real_import = builtins.__import__

    def watched(name, *args, **kwargs):
        if name == "ouroboros" or name.startswith("ouroboros."):
            imported.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", watched)
    host = Host(tmp_path)
    fake_gateway = gateway("ru", ru_doc())
    plugin.register(host)
    host.routes["i18n"]({})
    for callback in host.unload:
        callback()
    assert imported == [] and registered == [] and fake._EXTRA_CATALOGS == {}
    # everything the skill registered went through the API it was given, which unload tears down
    assert set(host.routes) >= {"i18n", "quotas", "chip", "refresh", "prefs", "display"}
    assert [path for _method, path, _payload in fake_gateway.calls] == \
        ["/api/ui/i18n"] + ["/api/ui/i18n/missing"] * len(fake_gateway.posts())


@pytest.mark.parametrize("tag", ["", "en", "en-GB"])
def test_the_route_says_only_english_for_english(tmp_path, gateway, tag):
    fake = gateway(tag, ru_doc())
    host = Host(tmp_path)
    plugin.register(host)
    answer = host.routes["i18n"]({})
    assert answer["language"] == "" and answer["strings"] == {} and answer["revision"] == 0
    assert answer["en"] == STRINGS
    assert fake.posts() == [] and fake.writes == 0


def test_the_route_is_english_outside_the_hosts_own_runtime(tmp_path, gateway):
    fake = gateway("ru", ru_doc())
    host = _Host(tmp_path)  # its runtime info has no data_dir: nothing to read a language from
    plugin.register(host)
    answer = host.routes["i18n"]({})
    assert answer["language"] == "" and answer["strings"] == {} and answer["en"] == STRINGS
    assert fake.calls == []


def test_the_route_gives_the_translation_of_our_keys_only_and_keeps_pins(tmp_path, gateway):
    doc = ru_doc()
    doc["entries"][PREFIX + "btn.retry"] = {"text": "Ещё раз", "provenance": "owner", "source": "Try again"}
    doc["entries"][PREFIX + "min.title"] = {"text": "Квоты", "provenance": "imported"}
    gateway("ru", doc)
    host = Host(tmp_path)
    plugin.register(host)
    answer = host.routes["i18n"]({})
    assert answer["language"] == "ru" and answer["revision"] == 12
    assert answer["plural_select"]["map"]["3"] == "few"
    assert answer["strings"]["btn.about"] == "О виджете"
    assert answer["strings"]["n.account"] == {"forms": {"one": "{n} аккаунт", "few": "{n} аккаунта",
                                                         "many": "{n} аккаунтов", "other": "{n} аккаунта"}}
    # an owner's pin is shown whatever English it was made for, an import's as it is: never replaced by English
    assert answer["strings"]["btn.retry"] == "Ещё раз" and answer["strings"]["min.title"] == "Квоты"
    assert set(answer["strings"]) == {"btn.about", "btn.refresh", "n.account", "btn.retry", "min.title"}
    assert answer["en"] == STRINGS


def test_a_generated_translation_of_another_english_shows_english_and_is_reported_reworded(tmp_path, gateway):
    doc = ru_doc()
    doc["entries"][PREFIX + "btn.refresh"]["source"] = "Reload"  # the English it was made for
    doc["entries"][PREFIX + "btn.about"]["source"] = "  " + STRINGS["btn.about"] + "\n"  # the same, spaced otherwise
    fake = gateway("ru", doc)
    host = Host(tmp_path)
    plugin.register(host)
    answer = host.routes["i18n"]({})
    assert "btn.refresh" not in answer["strings"] and answer["strings"]["btn.about"] == "О виджете"
    sent = {item["key"]: item["context"] for batch in fake.posts() for item in batch["items"]}
    assert sent[PREFIX + "btn.refresh"] == {"source": "Refresh", "table": "quotas.ui", "note": plugin.I18N_NOTE,
                                            "role": "quotas", "stale": True}
    assert "stale" not in sent[PREFIX + "plate.of"] and PREFIX + "btn.about" not in sent


def test_the_reading_carries_the_text_table_only_when_asked_for_it(tmp_path, gateway, monkeypatch):
    # a widget whose text table did not come by the i18n route asks for it with the reading (text=1): the numbers
    # are never held back for the words; a plain reading does not carry it
    from test_passive_quota_read import envelope
    gateway("ru", ru_doc())
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


def test_what_is_missing_is_reported_once_and_never_on_every_reading_or_translated_batch(tmp_path, gateway):
    fake = gateway("ru", ru_doc())
    host = Host(tmp_path)
    plugin.register(host)
    host.routes["i18n"]({})
    batches = fake.posts()
    items = [item for batch in batches for item in batch["items"]]
    assert len(batches) >= 3 and all(len(batch["items"]) <= 200 and batch["language"] == "ru" for batch in batches)
    assert {item["key"] for item in items} == {PREFIX + key for key in STRINGS} - {
        PREFIX + "btn.about", PREFIX + "btn.refresh", PREFIX + "n.account"}
    first = next(item for item in items if item["key"] == PREFIX + "plate.of")
    assert first["context"] == {"source": "of {n} account-windows left", "table": "quotas.ui",
                                "note": plugin.I18N_NOTE, "role": "quotas"}
    plural = next(item for item in items if item["key"] == PREFIX + "win.avg_read")
    assert plural["context"]["source"] == plugin._english_text(STRINGS["win.avg_read"])
    assert fake.writes == len(batches) and len(fake.pending) == len(items)
    for _ in range(5):  # the widget reads every 30 s: nothing more is written
        host.routes["i18n"]({})
    # the translator wrote a batch (the revision moved): the phrases still queued are not sent again
    fake.doc["revision"] = 13
    fake.doc["entries"][PREFIX + "plate.of"] = {"text": "из {n} аккаунт-окон", "provenance": "generated",
                                                "source": "of {n} account-windows left"}
    for _ in range(3):
        host.routes["i18n"]({})
    assert len(fake.posts()) == len(batches) and fake.writes == len(batches)
    assert host.routes["i18n"]({})["strings"]["plate.of"] == "из {n} аккаунт-окон"


def test_a_phrase_still_missing_is_reported_once_more_after_the_long_rest(gateway):
    fake = gateway("ru", ru_doc())
    english = plugin.english_strings()
    plugin._view_from_gateway({}, english, 8765, now=0.0)
    first = len(fake.posts())
    plugin._view_from_gateway({}, english, 8765, now=plugin.MISS_AGAIN_SEC - 1)
    assert len(fake.posts()) == first
    plugin._view_from_gateway({}, english, 8765, now=plugin.MISS_AGAIN_SEC)
    again = [item["key"] for batch in fake.posts()[first:] for item in batch["items"]]
    assert len(again) == len(STRINGS) - 3 and len(fake.pending) == len(STRINGS) - 3  # queued ones counted up
    plugin._view_from_gateway({}, english, 8765, now=plugin.MISS_AGAIN_SEC + 60)
    assert len(fake.posts()) == 2 * first


@pytest.mark.parametrize("loss", ["regenerate", "older_revision"])
def test_a_memory_that_lost_phrases_has_them_reported_again(gateway, loss):
    refused = PREFIX + "plate.of"
    fake = gateway("ru", ru_doc(), refused=[refused])
    english = plugin.english_strings()
    plugin._view_from_gateway({}, english, 8765, now=0.0)
    plugin._view_from_gateway({}, english, 8765, now=plugin.MISS_RETRY_SEC)  # the one retry of the batch taken in part
    before = len(fake.posts())
    assert refused not in fake.pending
    plugin._view_from_gateway({}, english, 8765, now=plugin.MISS_RETRY_SEC + 30)
    assert len(fake.posts()) == before, "a phrase the host refused rests"
    if loss == "regenerate":
        # Regenerate drops the generated entries and empties the refusals (the revision moves on): the three
        # phrases shown before are missing, and the refused one may be taken now
        fake.doc = {"revision": 14, "plural_select": ru_select(), "entries": {}}
        fake.refused.clear()
    else:
        fake.doc = {"revision": 0, "plural_select": ru_select(), "entries": {}}  # a memory replaced
    plugin._view_from_gateway({}, english, 8765, now=plugin.MISS_RETRY_SEC + 60)
    sent = {item["key"] for batch in fake.posts()[before:] for item in batch["items"]}
    lost = {PREFIX + "btn.about", PREFIX + "btn.refresh", PREFIX + "n.account"}
    # every phrase still missing is reported once more, the one the host refused before too
    assert lost <= sent and refused in sent and len(sent) == len(STRINGS)
    assert (refused in fake.pending) == (loss == "regenerate"), "taken once its refusal is forgotten, not before"
    plugin._view_from_gateway({}, english, 8765, now=plugin.MISS_RETRY_SEC + 90)
    assert len({item["key"] for batch in fake.posts()[before:] for item in batch["items"]}) == len(STRINGS)


def test_a_batch_the_host_took_in_part_is_offered_once_more_later_then_rests(gateway):
    # The host answers counts, not which keys it dropped: a batch with one refused key is not taken whole. It is
    # not sent again at the next reading, once more after MISS_RETRY_SEC, then it rests.
    refused = PREFIX + "plate.of"
    fake = gateway("ru", ru_doc(), refused=[refused])
    english = plugin.english_strings()
    plugin._view_from_gateway({}, english, 8765, now=1000.0)
    first = fake.posts()
    partial = [batch for batch in first if any(item["key"] == refused for item in batch["items"])]
    assert len(partial) == 1 and len(first) > 1, "only the batch with the refused key was taken in part"
    plugin._view_from_gateway({}, english, 8765, now=1030.0)
    plugin._view_from_gateway({}, english, 8765, now=1000.0 + plugin.MISS_RETRY_SEC - 1)
    assert fake.posts() == first, "not on every reading"
    plugin._view_from_gateway({}, english, 8765, now=1000.0 + plugin.MISS_RETRY_SEC)
    retry = fake.posts()[len(first):]
    assert [item["key"] for batch in retry for item in batch["items"]] == [item["key"] for item in partial[0]["items"]]
    for later in (2, 3, 10, 60):
        plugin._view_from_gateway({}, english, 8765, now=1000.0 + later * plugin.MISS_RETRY_SEC)
    assert len(fake.posts()) == len(first) + 1 and fake.writes == len(first) + 1


@pytest.mark.parametrize("outcome", [
    (None, "TimeoutError: timed out", plugin.NO_ANSWER_STATUS),   # may have reached the host: no answer read
    (None, "ConnectionRefusedError: refused", 0),                # never reached it
    (None, "HTTP 500 from /api/ui/i18n/missing", 500),
    ({"accepted": "all"}, "", 200),                              # an answer that is not the host's counts
])
def test_a_batch_the_host_did_not_take_is_offered_once_more_after_the_delay(tmp_path, gateway, outcome):
    fake = gateway("ru", ru_doc())
    fake.fail = outcome
    english = plugin.english_strings()
    plugin._view_from_gateway({}, english, 8765, now=50.0)
    first = len(fake.posts())
    assert first >= 3
    plugin._view_from_gateway({}, english, 8765, now=80.0)
    assert len(fake.posts()) == first
    fake.fail = None
    plugin._view_from_gateway({}, english, 8765, now=50.0 + plugin.MISS_RETRY_SEC)
    assert len(fake.posts()) == 2 * first and len(fake.pending) == len(STRINGS) - 3
    plugin._view_from_gateway({}, english, 8765, now=50.0 + 5 * plugin.MISS_RETRY_SEC)
    assert len(fake.posts()) == 2 * first


def test_a_language_changed_between_the_read_and_the_report_is_not_resent_at_once(gateway, monkeypatch):
    fake = gateway("ru", ru_doc())

    def switched(port, path, method="GET", timeout_sec=0, payload=None):
        answer = fake(port, path, method, timeout_sec, payload)
        if method == "GET":
            fake.language = "de"  # the owner chose another language just after the read
        return answer

    monkeypatch.setattr(plugin, "_request_json", switched)
    english = plugin.english_strings()
    plugin._view_from_gateway({}, english, 8765, now=0.0)
    assert fake.posts() and fake.writes == 0  # every report answered 409: nothing queued under another language
    monkeypatch.setattr(plugin, "_request_json", fake)
    fake.language = "ru"
    plugin._view_from_gateway({}, english, 8765, now=1.0)
    posts = len(fake.posts())
    plugin._view_from_gateway({}, english, 8765, now=plugin.MISS_RETRY_SEC + 1)
    assert len(fake.posts()) > posts and fake.writes == len(fake.posts()) - posts


def test_two_readings_at_once_never_claim_one_key_twice(gateway):
    # The i18n route and a reading with text=1 run on route threads at once: each key is claimed by one of them.
    import threading
    gateway("ru", ru_doc())
    english = plugin.english_strings()
    wanted = {key: False for key in english}
    start, claimed = threading.Barrier(4), []

    def read():
        start.wait()
        claimed.append(plugin._claim_misses("ru", 12, 0, english, wanted, 10.0))

    threads = [threading.Thread(target=read) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    keys = [item["key"] for batch in claimed for item in batch]
    assert len(claimed) == 4 and len(keys) == len(set(keys)) == len(english)


def test_a_language_with_no_memory_yet_is_english_and_queues_everything(tmp_path, gateway):
    fake = gateway("de", None)
    host = Host(tmp_path)
    plugin.register(host)
    answer = host.routes["i18n"]({})
    assert answer["language"] == "de" and answer["strings"] == {} and answer["en"] == STRINGS
    assert sum(len(batch["items"]) for batch in fake.posts()) == len(STRINGS) == len(fake.pending)


def test_a_memory_the_host_cannot_read_reports_nothing(tmp_path, gateway):
    fake = gateway("ru", ru_doc(), memory_error="ru.json: unreadable JSON")
    host = Host(tmp_path)
    plugin.register(host)
    answer = host.routes["i18n"]({})
    assert answer["language"] == "ru" and answer["strings"] == {} and answer["en"] == STRINGS
    assert fake.posts() == []


def test_a_broken_answer_never_breaks_the_route(tmp_path, gateway, monkeypatch):
    gateway("ru", ru_doc())
    monkeypatch.setattr(plugin, "_request_json", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("malformed")))
    host = Host(tmp_path)
    plugin.register(host)
    answer = host.routes["i18n"]({})
    assert answer["language"] == "" and answer["strings"] == {} and answer["en"] == STRINGS
    assert any("i18n read failed" in message for _level, message in host.logs)


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
      title: classes(env.root, 'q-title').map((n) => n.textContent),
      rings: classes(env.root, 'q-ring-name').map((n) => n.textContent),
      ringSub: classes(env.root, 'q-ring-sub').map((n) => n.textContent),
      say: walk(env.root).filter((n) => n.getAttribute('id') === 'quotas-win-say').map((n) => n.textContent),
    };
  }
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exitCode = 1; });
"""

RU = {
    "btn.about": "О виджете", "btn.refresh": "Обновить",
    "plate.of": {"forms": {"one": "из {n} аккаунт-окна осталось", "few": "из {n} аккаунт-окон осталось", "many": "из {n} аккаунт-окон осталось",
                           "other": "из {n} аккаунт-окна осталось"}},
    "n.account": {"forms": {"one": "{n} аккаунт", "few": "{n} аккаунта", "many": "{n} аккаунтов", "other": "{n} аккаунта"}},
    "acc.title": "Аккаунты · {summary}", "acc.word": "Аккаунты · {n}",
    "plate.window": "Окно: {len}", "plate.len_d": "{n}-дневное окно", "ring.avg_left": "в среднем осталось", "win.short.d": "{n}д",
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
        "из 1 аккаунт-окна осталось", "из 3 аккаунт-окон осталось", "из 5 аккаунт-окон осталось"]
    # the word over the bars carries the bare count now ("Accounts · N", key acc.word): the plural forms stand in the line above
    assert [x["accounts"][0] for x in (one, three, five)] == [
        "Аккаунты · 1", "Аккаунты · 3", "Аккаунты · 5"]
    # the plate's head, the ring's short name and its caption are in the install's language
    assert one["title"] == ["7-дневное окно"] and one["rings"] == ["7д"] and one["ringSub"] == ["в среднем осталось"]
    assert one["lowest"] == [] and three["lowest"] == [] and five["lowest"] == [], "the badge 'lowest left' is gone from the plate"
    # the date is the browser's own, in Russian: a short weekday, the day, a short month, the hour (the plate's spoken summary says the next reset)
    assert re.search(r"следующий сброс [а-я]{2} \d{1,2} [а-я]+ \d\d:\d\d", three["say"][0]), three["say"]


def test_a_phrase_the_translation_lacks_stays_english(tmp_path):
    partial = {key: value for key, value in RU.items() if key != "btn.refresh"}
    seen = _run(tmp_path, {"language": "ru", "revision": 1, "plural_select": None, "strings": partial})
    assert seen["n3"]["about"] == ["О виджете"] and seen["n3"]["refresh"] == ["Refresh"]
    # no plural map recorded: the browser's own rules choose the form
    assert seen["n3"]["accounts"] == ["Аккаунты · 3"]


def test_english_and_a_garbled_translation_leave_the_screen_in_english(tmp_path):
    english = _run(tmp_path, {"language": "", "revision": 0, "plural_select": None, "strings": {}})
    assert english["n3"]["about"] == ["About"] and english["n3"]["of"][0].strip() == "of 3 account-windows left"
    assert english["n1"]["of"][0].strip() == "of 1 account-window left" and english["n3"]["accounts"] == ["Accounts · 3"]
    garbled = _run(tmp_path, {"language": "ru", "revision": 2, "plural_select": {"map": "x"},
                              "strings": {"btn.about": 5, "plate.of": {"forms": 3}, "n.account": {"forms": {}}}})
    assert garbled["n3"]["about"] == ["About"] and garbled["n3"]["of"][0].strip() == "of 3 account-windows left"


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
    "cycle.note", "account.some", "status.unread", "status.quota_unread",
    "stamp.read_word", "stamp.read_of", "stamp.title", "stamp.title_failed", "stamp.title_quota", "stamp.title_none",
    "foot.not_read", "foot.read_ago", "foot.read_just_now", "foot.read_up_to", "foot.more", "foot.why_not_read", "foot.why_unreadable", "foot.why_sources_disagree",
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
    assert STRINGS["win.short.h"] == "{n}h" and STRINGS["win.short.d"] == "{n}d" and STRINGS["win.short.m"] == "{n}m"
    # (the note's head says it in the plate's own words: "No fresh data · 3", and a group of readings that are old says "read 4 days ago")
    assert STRINGS["foot.not_read"] == "No fresh data · {n}" and STRINGS["foot.more"] == "{n} more"
    assert STRINGS["foot.read_ago"] == "read {age} ago"
    # ...a reading under two minutes old is "read just now" (never "just now ago"), and accounts read at different ages are "read up to <the oldest> ago"
    assert STRINGS["foot.read_just_now"] == "read just now" and STRINGS["foot.read_up_to"] == "read up to {age} ago"
    # the number over the plate says what it is: what is left, of how many accounts
    assert STRINGS["plate.of"] == {"one": "of {n} account-window left", "other": "of {n} account-windows left"}
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
                        ("foot.more", {"n"}), ("win.none", {"name"}), ("win.avg", {"name", "avg"}),
                        ("cycle.note", {"family"}), ("foot.not_read", {"n"}), ("foot.read_ago", {"age"}), ("foot.read_up_to", {"age"}), ("foot.read_just_now", set()),
                        # the words of the top of the capsule: the bubble at the limit, the credits (the fact's caption is the window's name alone)
                        ("plate.at_limit", {"n"}), ("plate.back", {"in"}),
                        ("plate.over_plan", {"amount"}), ("credits.family", set()), ("credits.family_n", {"count"}),
                        ("credits.on_accounts", {"n"}), ("ring.scoped", {"scope", "len"}), ("plate.window", {"len"})):
        assert set(PLACEHOLDER.findall(" ".join(_forms_of(STRINGS[key])))) == params, key
    # English left in the widget beside the row would be the old defect: the labels of the row and of the scope go through t().
    for gone in ("'Limit window'", "'Limit scope'", "'avg left'", "'no current reading'", "'hidden in Rows'"):
        assert gone not in WIDGET.split("function limitRail(")[1].split("function litOf(")[0], gone


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
    assert all(re.search(r"[А-Яа-яЁё]", text) for text in said.values()), [k for k, v in said.items() if not re.search(r"[А-Яа-яЁё]", v)]


# The words that came to the plate are in the English table and in the Russian memory; the words that left it are in neither.
PLATE_WORDS_GONE = ["plate.lowest", "plate.show_chart", "plate.hide_chart", "plate.last_known", "plate.age_unknown", "plate.avg_left",
                    "legend.unknown", "cycle.no_share",
                    # the rail has no caption ("average across accounts") and no grey place (a window the family does not report is not drawn)
                    "rail.cap", "win.not_reported", "win.cursor_none",
                    # the fact's caption is the name of the window alone: the word "Remaining" is gone from the plate (the title of the figure keeps its sense)
                    "plate.remaining",
                    # the history has no fold of its own: the plate's word Chart is gone, and so is the scope row (each scope has its own ring)
                    "plate.chart", "scope.row", "chart.strip", "chart.strip_label", "acc.strip_of", "cycle.period"]


def test_the_word_chart_and_the_dated_values_line_are_in_both_files_and_the_words_that_left_the_plate_are_in_neither():
    assert STRINGS["legend.last"] == "last known" and "t('legend.last')" in WIDGET
    for key in PLATE_WORDS_GONE:
        assert key not in STRINGS, key
        assert ("'%s'" % key) not in WIDGET, "the widget still asks for %s" % key
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    memory = json.loads(RU_JSON.read_text(encoding="utf-8"))["entries"]
    ru = {key[len(MEMORY_PREFIX):]: value for key, value in memory.items()}
    assert ru["legend.last"]["text"].strip() and ru["legend.last"]["source"] == STRINGS["legend.last"]
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
    assert "t('foot.more', { n: steps.length - max })" in WIDGET
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    memory = json.loads(RU_JSON.read_text(encoding="utf-8"))["entries"]
    ru = {key[len(MEMORY_PREFIX):]: value for key, value in memory.items()}
    for key in LADDER_WORDS:
        assert key in ru, key
    for key in LADDER_WORDS:
        translated = ru[key].get("text") or " ".join(ru[key].get("forms", {}).values())
        assert translated.strip(), key
        assert set(PLACEHOLDER.findall(translated)) == set(PLACEHOLDER.findall(_text(STRINGS[key]))), key
    assert "ladder.date" not in ru, "the memory holds no date of the step"
    assert all("forms" not in ru[k] for k in ("ladder.in_min", "ladder.in_hour", "ladder.in_day", "ladder.in_day_hour"))
    assert set(ru["plate.back"]["forms"]) == {"one", "few", "many", "other"} and all(
        "{in}" in form for form in ru["plate.back"]["forms"].values()), "each form retains the reset countdown"
    assert set(ru["ladder.more_label"]["forms"]) == {"one", "few", "many", "other"}
    assert all(re.search(r"[А-Яа-яЁё]", form) for form in ru["ladder.more_label"]["forms"].values())


# ---------------------------------------------------------------------------
# The words a bar opened to are gone with the opening: no figure and no words in a bar (the card beside the bars says them), so
# neither file holds them and the widget asks for none.
BAR_WORDS_GONE = ["bar.reset_in", "bar.at_limit", "bar.was_at_limit", "bar.restricted_now", "bar.read", "bar.no_data"]


def test_the_words_a_bar_opened_to_are_gone_with_the_opening():
    for key in BAR_WORDS_GONE:
        assert key not in STRINGS, key
        assert ("'%s'" % key) not in WIDGET, "the widget still asks for %s" % key
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    memory = json.loads(RU_JSON.read_text(encoding="utf-8"))["entries"]
    ru = {key[len(MEMORY_PREFIX):]: value for key, value in memory.items()}
    assert not [key for key in BAR_WORDS_GONE if key in ru], "the memory still holds words the bars no longer ask for"


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
    assert _placeholders(ru["fact.tile_when"]["text"]) == {"when", "total"}
    assert not [key for key in TILE_WORDS_GONE if key in ru], "the memory still holds words the tiles no longer ask for"


@pytest.mark.parametrize('path', ['route', 'fallback'])
def test_old_language_response_cannot_cross_page_lifecycle(tmp_path, path):
    harness = test_quotas.NODE_WIDGET_MATRIX.split('(async () => {')[0]
    harness = harness.replace("const wrapper = (url, options) => (isI18n(url) ? Promise.resolve(response(I18N_ANSWER)) : current.call(window, url, options));",
                              "const wrapper = (url, options) => current.call(window, url, options);")
    harness = harness.replace(": response(typeof view === 'function' ? view(url, calls) : view));",
                              ": Promise.resolve(typeof view === 'function' ? view(url, calls) : view).then(response));")
    script = r'''
(async () => {
  const view = JSON.parse(process.env.TEST_VIEW);
  const fallback = process.env.TEST_PATH === 'fallback';
  let phase = 0, resolveOld;
  const en = I18N_ANSWER;
  const ru = {...en, language: 'ru', strings: {'btn.about': 'О виджете', 'btn.refresh': 'Обновить'}};
  const env = await boot(url => {
    if (isI18n(url)) {
      if (phase === 0 && fallback) return {};
      if (phase === 1 && !fallback) return new Promise(resolve => { resolveOld = resolve; });
      return phase === 2 ? ru : en;
    }
    if (fallback && String(url).includes('text=1') && phase === 0) return new Promise(resolve => { resolveOld = resolve; });
    return view;
  });
  if (!fallback) { phase = 1; env.interval()(); }
  await settle();
  assert.equal(typeof resolveOld, 'function', 'old transport remains in flight');
  env.windowListeners.pagehide[0]();
  phase = 2;
  env.windowListeners.pageshow[0]();
  await settle();
  assert.match(byFocus(env.root, 'about').textContent, /О виджете/);
  resolveOld(fallback ? {...view, text: en} : en);
  await settle();
  byFocus(env.root, 'accounts').listeners.click[0]({stopPropagation(){}});
  await settle();
  assert.match(byFocus(env.root, 'about').textContent, /О виджете/, 'old English must not overwrite new Russian');
  env.disposeHooks[0]();
})().catch(e => { console.error(e.stack || e); process.exit(1); });
'''
    widget = Path(os.environ.get('QUOTAS_REVIEW_WIDGET_PATH', HERE / 'widget.js'))
    result = subprocess.run([str(_node()), '-e', harness + script], cwd=HERE,
                            env={**os.environ, 'WIDGET_PATH': str(widget), 'TEST_PATH': path,
                                 'TEST_VIEW': json.dumps(_views(tmp_path)['n1'])},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_selection_and_billing_words_use_current_translation(tmp_path):
    translated = {key: 'ЯЗЫК ' + key + ' ' + ' '.join('{' + name + '}' for name in PLACEHOLDER.findall(_text(value)))
                  for key, value in STRINGS.items()}
    harness = test_quotas.NODE_WIDGET_MATRIX.split('(async () => {')[0]
    harness = harness.replace('const isI18n =', 'Object.assign(I18N_ANSWER, JSON.parse(process.env.TEST_I18N));\nconst isI18n =', 1)
    script = r'''
(async () => {
  const views = JSON.parse(process.env.TEST_VIEWS);
  const env = await boot(views.cursor_mixed);
  const first = allSpoken(env.root);
  for (const key of ['budget.share', 'budget.ends', 'budget.last_read', 'budget.unlimited_note', 'pick.selected', 'pick.select']) {
    assert.ok(first.includes('ЯЗЫК ' + key), 'translated billing/selection ' + key);
  }
  byFocus(env.root, 'accounts').listeners.click[0]({stopPropagation(){}});
  byFocus(env.root, 'acct:cursor:a2').listeners.click[0]({stopPropagation(){}});
  const text = allSpoken(env.root);
  assert.ok(text.includes('ЯЗЫК accounts.unlimited'));
  assert.ok(text.includes('ЯЗЫК pick.selected'));
  assert.doesNotMatch(text, /select this account|unlimited included usage|cycle ends/);
  assert.match(text, /a2/, 'provider account label is preserved');
  env.disposeHooks[0]();
})().catch(e => { console.error(e.stack || e); process.exit(1); });
'''
    result = subprocess.run([str(_node()), '-e', harness + script], cwd=HERE,
                            env={**os.environ, 'WIDGET_PATH': str(HERE / 'widget.js'),
                                 'TEST_I18N': json.dumps({'language': 'ru', 'strings': translated}),
                                 'TEST_VIEWS': json.dumps(W._cursor_views(tmp_path))},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_resize_handlers_and_timer_are_released_and_restored(tmp_path):
    harness = test_quotas.NODE_WIDGET_MATRIX.split('(async () => {')[0]
    harness = harness.replace('    root: made.root,', '    window,\n    root: made.root,')
    script = r'''
(async () => {
 const env = await boot(JSON.parse(process.env.TEST_VIEW));
 assert.equal(env.windowListeners.resize.length, 2);
 let timer, cancelled = false;
 env.window.setTimeout = (fn, ms) => { assert.equal(ms, 120); timer = fn; return 456; };
 const clear = env.window.clearTimeout;
 env.window.clearTimeout = id => { if (id === 456) cancelled = true; else clear(id); };
 env.windowListeners.resize[1]();
 assert.equal(typeof timer, 'function');
 env.windowListeners.pagehide[0]();
 assert.equal(env.windowListeners.resize.length, 0);
 assert.ok(cancelled, 'pending resize timer was cleared');
 env.window.setTimeout = (fn, ms) => setTimeout(fn, ms);
 env.windowListeners.pageshow[0]();
 await settle();
 assert.equal(env.windowListeners.resize.length, 2, 'one handler pair after restoration');
 env.disposeHooks[0]();
 assert.equal(env.windowListeners.resize.length, 0);
})().catch(e => { console.error(e.stack || e); process.exit(1); });
'''
    result = subprocess.run([str(_node()), '-e', harness + script], cwd=HERE,
                            env={**os.environ, 'WIDGET_PATH': str(HERE / 'widget.js'),
                                 'TEST_VIEW': json.dumps(_views(tmp_path)['n1'])}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
