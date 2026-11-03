"""The widget as a consumer of the real host: Ouroboros's own Widgets page, card, opaque module frame and
bridge around arrangement B, reading synthetic memory through PluginAPI 2.1 ``read_memory`` (``real_host``).

Engines: WebKit (the macOS shell's engine, headless Playwright build) and Chromium. Viewports: an 1100×750
window and a 390×844 phone; dark and light through the host's own theme. Every card reports page errors,
console errors and any failed answer of this skill's routes; the isolated host's own SPA requests that fail
(its Claudexor maintenance probe answers 503 without an operator) are not the widget's and are not counted.

Tests that write memory (``producer``) come last in this file and touch only what they create, except the
note edit, which changes a note this file creates.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from conftest import (LATE_SCOPE, LINKS_MANY, LINKS_SCOPE, LINKS_TARGET, PREFIX, PROJECT_AND_NOTE, correct_page, edit_many_links,
                      ensure_late_overview, ensure_many_links, ensure_many_shelves, make_nested_changes)

WIDE = {"width": 1100, "height": 750}
PHONE = {"width": 390, "height": 844}
KEY = "memory-atlas:atlas"
ROUTES = "/api/extensions/memory-atlas/"
SHOTS = os.environ.get("ATLAS_SHOTS")


class Card:
    """The Widgets page of the real host with the Memory Atlas card started, and everything it reported."""

    def __init__(self, pw, host, engine="webkit", viewport=WIDE, theme="dark"):
        self.host, self.engine, self.theme = host, engine, theme
        self.browser = getattr(pw, engine).launch()
        self.context = self.browser.new_context(viewport=viewport, device_scale_factor=1, color_scheme=theme)
        self.page = self.context.new_page()
        self.errors, self.answers, self.expected = [], [], set()
        self.page.on("pageerror", lambda e: self.errors.append("pageerror: " + str(e)))
        self.page.on("console", self._console)
        self.page.on("response", self._response)
        self.page.goto(host.url + "/#widgets")
        self.card = self.page.locator(f'[data-widget-key="{KEY}"]')
        self.card.wait_for(timeout=60000)
        self.card.scroll_into_view_if_needed()
        self.power = self.card.locator("[data-widget-power]")
        self.start()

    def _console(self, message):
        if message.type == "error" and not message.text.startswith("Failed to load resource"):
            self.errors.append("console: " + message.text)   # a failed load is judged by its response below

    def _response(self, response):
        if ROUTES in response.url:
            self.answers.append((response.status, response.url))
            if response.status >= 400 and response.status not in self.expected:
                self.errors.append(f"route answered {response.status}: {response.url}")

    def start(self):
        """Start, as the owner presses it. The Widgets page may redraw its cards once after they first appear
        (its live list catching up); a press that landed on the card it replaced is pressed again."""
        for attempt in range(3):
            assert self.power.inner_text() == "Start"
            self.power.click()
            try:
                self.card.locator("iframe").wait_for(timeout=20000)
                break
            except Exception:
                if attempt == 2 or self.power.inner_text() != "Start":
                    if SHOTS:
                        self.page.screenshot(path=str(Path(SHOTS) / "start-failed.png"))
                    raise
        self.frame = self.card.locator("iframe").element_handle().content_frame()
        self.frame.wait_for_selector(".atlas .nav, .atlas .state h1, .atlas .room-head h1", timeout=60000)
        self.page.wait_for_timeout(300)

    def stop(self):
        self.power.click()
        self.card.locator("iframe").wait_for(state="detached", timeout=30000)

    def ev(self, script, arg=None):
        return self.frame.evaluate(script, arg)

    def open(self, routes):
        self.ev("r => window.__memoryAtlas.open(r)", routes)
        return self.settle()

    def settle(self, timeout=60000):
        """The open place drawn: its title, or the widget's own words when the read failed (an AssertionError)."""
        self.frame.wait_for_function("() => !document.querySelector('.loading') &&"
                                     " (document.querySelector('[data-view-title]') || document.querySelector('.notice, .state'))",
                                     timeout=timeout)
        self.page.wait_for_timeout(150)
        if not self.ev("() => !!document.querySelector('[data-view-title]')"):
            raise AssertionError("the place did not open: " + self.text())
        return self.title()

    def click(self, name, exact=False, within=None):
        scope = self.frame.locator(within) if within else self.frame
        scope.get_by_role("button", name=name, exact=exact).first.click()
        self.page.wait_for_timeout(120)

    def title(self):
        self.frame.wait_for_selector("[data-view-title]", timeout=60000)
        return self.frame.locator("[data-view-title]").first.inner_text()

    def text(self):
        return self.ev("() => (document.querySelector('.pane') || document.querySelector('.atlas')).innerText")

    def wait_text(self, words, timeout=60000):
        self.frame.wait_for_function("w => (document.querySelector('.pane') || document.querySelector('.atlas')).innerText.includes(w)",
                                     arg=words, timeout=timeout)

    def shot(self, name):
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            self.card.screenshot(path=str(Path(SHOTS) / f"{name}-{self.engine}-{self.theme}.png"))

    def assert_clean(self):
        assert self.errors == [], self.errors
        assert self.answers and all(status in (200, *self.expected) for status, _ in self.answers)

    def close(self):
        self.browser.close()


@pytest.fixture
def card(playwright_api, real_host):
    made = []

    def make(engine="webkit", viewport=WIDE, theme="dark"):
        c = Card(playwright_api, real_host, engine, viewport, theme)
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


def rec(ids, key, label="Record", room=None):
    return {"v": "record", "id": ids[key], "label": label, "hint": {"kind": "page", "room": room or "1"}}


# ---- first screen and the design system ------------------------------------------------------------------------

@pytest.mark.parametrize("engine", ["webkit", "chromium"])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_first_screen_is_rooms_beside_main(card, real_host, engine, theme):
    c = card(engine, theme=theme)
    nav = c.ev("() => Array.from(document.querySelectorAll('.nav .row .title')).map(e => e.textContent)")
    assert nav == ["My story", "Main", "Project Garden sensors", "Project Release flow", "Project Атлас памяти",
                   "Unknown room", "Host records without a room", "Knowledge", "Marks", "Identity", "Other sources"]
    assert c.ev("() => document.querySelector('.nav .row[aria-current=true] .title').textContent") == "Main"
    assert c.title() == "Main"
    assert c.ev("() => !document.querySelector('.nav .source-note')")   # this host's own memory: no "not read" note
    assert c.ev("() => document.documentElement.classList.contains('ouro-ui') && document.documentElement.dataset.theme") == theme
    assert c.ev("() => document.getElementById('ouro-kit').textContent.length > 1000")   # the installed web/ui.css
    width, nav_width = c.ev("() => [document.querySelector('.panes').getBoundingClientRect().width, document.querySelector('.nav').getBoundingClientRect().width]")
    assert nav_width == 220 and width > 700
    assert c.ev("() => document.scrollingElement.scrollWidth <= innerWidth")
    c.shot("first-wide")
    c.assert_clean()


BORDERS = """() => Array.from(document.querySelectorAll('button')).map(b => {
  const s = getComputedStyle(b);
  const sides = ['Top', 'Right', 'Bottom', 'Left'].map(k => ({w: parseFloat(s['border' + k + 'Width']), st: s['border' + k + 'Style'], c: s['border' + k + 'Color']}));
  const drawn = sides.filter(x => x.w > 0 && x.st !== 'none' && !/rgba\\(0, 0, 0, 0\\)|transparent/.test(x.c));
  return {cls: b.className, text: b.textContent.trim().slice(0, 30), drawn: drawn.length, outline: s.outlineStyle, shadow: s.boxShadow};
})"""


@pytest.mark.parametrize("engine", ["webkit", "chromium"])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_no_control_draws_a_border_or_outline(card, real_host, engine, theme):
    c = card(engine, theme=theme)
    c.open([{"v": "room", "room": real_host.ids["room:garden-sensors"]}, rec(real_host.ids, "cadence")])
    buttons = c.ev(BORDERS)
    assert len(buttons) >= 12
    assert [b for b in buttons if b["drawn"]] == []
    assert [b for b in buttons if b["outline"] != "none"] == []
    capsules = [b for b in buttons if "btn-default" in b["cls"]]
    quiet = [b for b in buttons if "btn-ghost" in b["cls"] or b["cls"] in ("row", "link", "link small", "navlink")]
    assert capsules and all(b["shadow"] != "none" for b in capsules)       # standing on the rest shadow
    assert quiet and all(b["shadow"] == "none" for b in quiet)             # quiet controls have no body
    c.assert_clean()


@pytest.mark.parametrize("engine", ["webkit", "chromium"])
def test_keyboard_focus_wears_the_one_ring(card, engine):
    c = card(engine)
    c.click("My story")
    c.settle()
    c.frame.locator("[data-view-title]").focus()
    # WebKit, like Safari by default, moves Tab between fields only; Option+Tab reaches every control.
    c.page.keyboard.press("Alt+Tab" if engine == "webkit" else "Tab")
    ring = c.ev("""() => { const a = document.activeElement, s = getComputedStyle(a);
      return {tag: a.tagName, style: s.outlineStyle, width: s.outlineWidth, color: s.outlineColor,
              token: getComputedStyle(document.documentElement).getPropertyValue('--focus-accent-border').trim()}; }""")
    assert ring["tag"] == "BUTTON" and ring["style"] == "solid" and ring["width"] == "2px", ring
    token = ring["token"].lstrip("#")
    rgb = tuple(int(token[i:i + 2], 16) for i in (0, 2, 4))
    assert ring["color"].replace(" ", "") in ("rgb(%d,%d,%d)" % rgb, "#" + token), ring
    c.assert_clean()


# ---- reading flows ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("engine", ["webkit", "chromium"])
def test_room_puts_candidates_first_and_back_returns_to_the_row(card, real_host, engine):
    ids = real_host.ids
    c = card(engine)
    c.click("Project Garden sensors")
    assert c.settle() == "Project Garden sensors"
    heads = c.ev("() => Array.from(document.querySelectorAll('.pane .h-group')).map(e => e.textContent)")
    assert heads[:2] == ["Candidate pages and parts awaiting a decision", "Story in this room"]
    assert "Decisions on drafts" in heads and "Marks written here" in heads
    c.frame.locator(f'[data-key="record:{ids["candidate"]}"]').click()
    c.settle()
    assert c.ev("() => document.querySelector('.kicker .word').textContent") == "Candidate · awaiting a decision"
    assert "A candidate does not act yet" in c.text()
    key = c.ev("() => document.querySelector('.addr code').textContent")
    assert key == f"memory_read(node_id='{ids['candidate']}')"
    c.page.keyboard.press("Escape")
    assert c.settle() == "Project Garden sensors"
    assert c.ev("() => document.activeElement.dataset.key") == "record:" + ids["candidate"]
    c.assert_clean()


def test_drafts_decisions_and_marks_say_their_recorded_state(card, real_host):
    ids = real_host.ids
    c = card()
    c.open([{"v": "room", "room": "5203"}, rec(ids, "draft", room="5203")])
    assert c.ev("() => document.querySelector('.kicker .word').textContent") == "Draft · acting, no decision yet"
    assert "Until Ouroboros decides, this draft acts like its own page" in c.text()
    assert "Draft by Light" in c.text()
    c.open([{"v": "room", "room": ids["room:release-flow"]}])
    c.frame.get_by_role("button", name=re.compile("^Rejected ")).first.click()
    c.settle()
    assert c.ev("() => document.querySelector('.kicker .word').textContent") == "Rejected draft"
    assert c.ev("() => document.querySelector('[data-focus]').innerText").startswith("The decision you opened")
    assert "Rejected by Ouroboros on" in c.text() and "no longer stands in the story" in c.text()
    c.shot("rejected-draft")
    c.open([{"v": "marks"}])
    text = c.text()
    assert text.index("Active") < text.index("Released") and "Global · tied to schedule_followup" in text
    c.frame.locator(f'[data-key="record:{ids["mark_view"]}"]').click()
    c.settle()
    assert "its meaning only" in c.text() and "View chosen" in c.text()
    c.open([{"v": "marks"}, {"v": "record", "id": ids["mark_released"], "label": "Mark"}])
    assert "Released" in c.ev("() => document.querySelector('.kicker').innerText")
    assert "The new key arrived on 20 October" in c.text()
    c.assert_clean()


def test_a_long_unicode_text_reads_on_by_ranges_to_its_correction(card, real_host):
    """1,088,200 code points: the first range, Read on (one revision) to the end, then the signed correction."""
    ids = real_host.ids
    c = card()
    c.open([{"v": "room", "room": ids["room:atlas-ru"]}, rec(ids, "big", room=ids["room:atlas-ru"])])
    total = c.ev("() => document.querySelector('.reader-foot .meta').textContent")
    assert total == "131,072 of 1,088,200 characters"
    assert not c.ev("() => !!document.querySelector('.correction')")
    for _ in range(12):
        if not c.ev("() => !!document.querySelector('[data-key=\"read-on\"]')"):
            break
        c.frame.locator('[data-key="read-on"]').click()
        c.frame.wait_for_function("() => !document.querySelector('[data-key=\"read-on\"][disabled]')", timeout=60000)
    assert not c.ev("() => !!document.querySelector('[data-key=\"read-on\"]')")
    shown = c.ev("() => [document.querySelector('.prose').textContent.length, (document.querySelector('.prose').textContent.match(/🌱/gu) || []).length]")
    assert shown[1] == 8000                                   # every astral emoji arrived whole
    assert shown[0] == ids["big_chars"] + 8000                # UTF-16 length: one extra unit per emoji
    sig = c.ev("() => document.querySelector('.correction .sig').textContent")
    assert sig.startswith("Correction by Ouroboros (main chat, task t-main)")
    assert "вторая половина повторяет первую" in c.ev("() => document.querySelector('.correction').textContent")
    c.assert_clean()


def test_account_reads_sources_as_cited_and_as_now(card, real_host):
    ids = real_host.ids
    c = card()
    c.open([{"v": "story"}, {"v": "record", "id": ids["account"], "label": "Account", "hint": {"kind": "account", "room": "1"}}])
    assert c.title() == "Across Project Garden sensors, Project Release flow, Unknown room and Main"
    text = c.text()
    assert "Selected into My story on" in text and "Sources, as it cited them (5)" in text
    assert "Ouroboros chose to tell these 3 records through this account" in text
    told = c.ev("() => Array.from(document.querySelectorAll('.block .rows .row .text')).map(e => e.textContent)")
    assert len(told) == 3 and all(t and t != "…" for t in told)   # each named record with its own words
    assert "The part did not record member versions" in text
    c.shot("account-top")
    row = c.frame.locator(".srcrow", has_text="after this account; not in its text").first
    row.get_by_role("button", name="Read as cited").click()
    c.settle()
    doc = c.text()
    assert "An earlier version. 1 later correction is not part of it." in doc
    assert c.ev("() => document.querySelector('.addr code').textContent").count("revision='") == 1
    assert not c.ev("() => !!document.querySelector('.correction')")
    c.click("Read it as it stands now")
    c.settle()
    assert c.ev("() => document.querySelectorAll('.correction').length") == 1
    assert "revision=" not in c.ev("() => document.querySelector('.addr code').textContent")
    c.page.keyboard.press("Escape")
    c.settle()
    part = c.frame.locator(".srcrow", has_text="inside it since").first
    part.get_by_role("button", name="Show them").click()
    c.frame.wait_for_selector(".nested", timeout=60000)
    c.frame.locator(".nested").first.get_by_role("button", name="Open it").click()
    c.settle()
    assert c.frame.locator("[data-focus]").count() == 1
    c.assert_clean()


QUOTES = """() => Array.from(document.querySelectorAll('button.quote')).map(b => {
  const s = getComputedStyle(b), probe = document.createElement('div');
  probe.style.background = b.classList.contains('person') ? 'var(--voice-owner)' : 'var(--voice-ouroboros)';
  b.parentNode.appendChild(probe);
  const token = getComputedStyle(probe).backgroundColor;
  probe.remove();
  return {voice: b.className, who: b.querySelector('.who').textContent, text: b.querySelector('p').textContent,
          src: b.querySelector('.src').textContent, border: s.borderTopStyle + ' ' + s.borderTopWidth, outline: s.outlineStyle,
          fill: s.backgroundColor === token};
})"""


@pytest.mark.parametrize("engine", ["webkit", "chromium"])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_quotes_say_who_said_them_and_open_their_chat_row(card, real_host, engine, theme):
    """The approved block under the signed correction: each quote by its checked speaker class, in that voice,
    with the room and time of its row; it opens the row's exact read, the quote as stored and the room."""
    ids = real_host.ids
    c = card(engine, theme=theme)
    c.open([{"v": "room", "room": "1"}, rec(ids, "weekly")])
    heads = c.ev("() => Array.from(document.querySelectorAll('.pane .doc .h-group')).map(e => e.textContent)")
    assert heads.index("Words it quotes, checked against the chat") < heads.index("Facts and links")
    assert c.ev("() => !!(document.querySelector('.correction').compareDocumentPosition(document.querySelector('button.quote'))"
                " & Node.DOCUMENT_POSITION_FOLLOWING)")
    first, second = ids["quotes"]
    assert c.ev(QUOTES) == [
        {"voice": "quote person", "who": "A person", "text": "“" + first["text"] + "”", "src": "Main · 24 Oct 2026, 07:55",
         "border": "none 0px", "outline": "none", "fill": True},
        {"voice": "quote ouroboros", "who": "Ouroboros", "text": "“" + second["text"] + "”", "src": "Main · 24 Oct 2026, 08:05",
         "border": "none 0px", "outline": "none", "fill": True}]
    c.frame.locator("button.quote").first.scroll_into_view_if_needed()
    c.shot("quotes")
    c.frame.locator(f'[data-key="quote:{ids["weekly"]}:0"]').click()
    assert c.settle() == "A person, Main"
    text = c.text()
    assert "The chat row of 24 Oct 2026, 07:55" in text and "the exact words of that row" in text and "spoken by a person" in text
    row = ids["quote_rows"][0]
    assert c.ev("() => document.querySelector('.addr code').textContent") == f"memory_read(rows=true, room_id='1', from='{row}', to='{row}')"
    c.shot("quote-source")
    c.click("The quote as stored")
    c.settle()
    assert json.loads(c.ev("() => document.querySelector('pre.source').textContent")) == first
    c.page.keyboard.press("Escape")
    assert c.settle() == "A person, Main"
    c.page.keyboard.press("Escape")
    c.settle()
    assert c.ev("() => document.activeElement.dataset.key") == f"quote:{ids['weekly']}:0"   # Back lands on the quote
    c.frame.locator(f'[data-key="quote:{ids["weekly"]}:1"]').click()
    assert c.settle() == "Ouroboros, Main" and "spoken by Ouroboros" in c.text()
    c.click("Main", exact=True, within=".facts-grid")
    assert c.settle() == "Main"   # Said in: the room of that row
    c.assert_clean()


def test_quotes_on_a_phone_fit_and_copy_the_row(card, real_host):
    ids = real_host.ids
    c = card(viewport=PHONE, theme="light")
    c.open([{"v": "home"}, {"v": "room", "room": "1"}, rec(ids, "weekly")])
    c.frame.locator("button.quote").first.scroll_into_view_if_needed()
    assert c.ev("() => document.scrollingElement.scrollWidth <= innerWidth")
    assert c.ev("() => Array.from(document.querySelectorAll('button.quote')).every(b => b.scrollWidth <= b.clientWidth)")
    c.shot("quotes-phone")
    c.frame.locator(f'[data-key="quote:{ids["weekly"]}:1"]').click()
    assert c.settle() == "Ouroboros, Main"
    c.frame.locator(".addr").get_by_role("button", name="Copy").click()
    c.frame.wait_for_function("() => /Copied|Select and copy/.test(document.querySelector('.addr button').textContent)", timeout=10000)
    c.shot("quote-source-phone")
    c.frame.get_by_role("button", name=re.compile("^Back to ")).first.click()
    c.settle()
    assert c.ev("() => document.activeElement.dataset.key") == f"quote:{ids['weekly']}:1"
    c.assert_clean()


def test_linked_from_says_it_is_still_reading_a_long_note_and_looks_further(card, real_host):
    """The target's one link in sits past what a step reads: the list says which note it is still reading and
    continues it on Look further, never saying that no note links here."""
    ensure_many_links(real_host)
    c = card()
    c.open([{"v": "knowledge", "scope": LINKS_SCOPE}, {"v": "note", "scope": LINKS_SCOPE, "topic": LINKS_TARGET, "label": "Links target"}])
    linked = ".block:has(> .h-group:text-is('Linked from'))"
    c.frame.wait_for_selector(linked + " .pager-foot button", timeout=60000)
    foot = c.frame.locator(linked + " .pager-foot").inner_text()
    assert "Still checking the links of “links-check/many”: 2,000 read so far" in foot
    assert "No note on this shelf links here" not in c.text()
    c.shot("linked-from-continuing")
    c.frame.locator(linked + " .pager-foot button").click()
    c.frame.wait_for_selector(f'{linked} [data-key="note:{LINKS_SCOPE}|{LINKS_MANY}"]', timeout=60000)
    assert c.frame.locator(linked + " .pager-foot button").count() == 0
    assert "No note on this shelf links here" not in c.text()
    c.assert_clean()


def _history_page(host, record_id, entries, nxt, complete):
    return json.dumps({"contract": "memory-atlas.read/2", "source": {"mode": "core", "label": "constructed at the route boundary"},
                       "memory_contract": "ouroboros.memory/1", "area": "history", "entries": entries, "next": nxt,
                       "complete": complete})


def test_the_decision_block_reads_the_history_to_its_current_decision(card, real_host):
    """The store keeps one decision per draft, so a longer history is constructed here at the route boundary:
    an older decision on a first page that is not complete. The block reads on to the real last decision and
    shows it, not the first page's last; past its bound it says how far it read and reads further on request."""
    import urllib.parse
    import urllib.request

    ids = real_host.ids
    query = urllib.parse.urlencode({"area": "history", "address": json.dumps({"kind": "record", "id": ids["wrong"]}),
                                    "kinds": "decision", "limit": "500"})
    with urllib.request.urlopen(real_host.url + PREFIX + "list?" + query, timeout=60) as response:
        real = json.loads(response.read())["entries"][0]
    older = {**real, "id": "constructed-older-decision", "accepted": True, "preview": "An older reason, constructed.",
             "chars": 29, "ts": "2026-10-01T09:00:00+00:00"}

    def wrong_history(route):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(route.request.url).query)
        if q.get("area") == ["history"] and q.get("kinds") == ["decision"] and ids["wrong"] in q.get("address", [""])[0]:
            if "cursor" not in q or always_more[0]:
                route.fulfill(status=200, content_type="application/json",
                              body=_history_page(real_host, ids["wrong"], [older], "s0", False))
                return
        route.continue_()

    always_more = [False]
    c = card()
    c.page.route("**/atlas/v2/list?*", wrong_history)
    c.open([{"v": "room", "room": ids["room:release-flow"]}, rec(ids, "wrong", room=ids["room:release-flow"])])
    tray = c.ev("() => document.querySelector('.block .tray').innerText")
    assert tray.startswith("Rejected by Ouroboros on") and "The host stamp says the upload failed" in tray
    assert "An older reason" not in tray
    always_more[0] = True   # now every page says more follows: the block stops at its bound and says so
    c.stop()
    c.start()   # a fresh mount reads the draft again
    c.open([{"v": "room", "room": ids["room:release-flow"]}, rec(ids, "wrong", room=ids["room:release-flow"])])
    tray = c.ev("() => document.querySelector('.block .tray').innerText")
    assert "Read 4 decisions of its history so far; the current one comes later." in tray
    assert "An older reason" not in tray and "Rejected by Ouroboros on" not in tray
    c.shot("decision-bounded")
    always_more[0] = False
    c.frame.locator('[data-key="decisions-more"]').click()
    c.frame.wait_for_function("() => document.querySelector('.block .tray').innerText.includes('Rejected by Ouroboros on')", timeout=60000)
    assert "The host stamp says the upload failed" in c.ev("() => document.querySelector('.block .tray').innerText")
    c.assert_clean()


FACT = "k => { const d = Array.from(document.querySelectorAll('.facts-grid dt')).find((x) => x.textContent === k); return d ? d.nextElementSibling.textContent : null; }"


def test_periods_come_from_the_core_and_never_from_a_write_time(card, real_host):
    """Rows and titles show a dated record's period as the core gives it: its span in words, a block's period as
    the core words it, nothing when unknown. The Period fact is the core's text exactly. No write time stands in."""
    import urllib.parse
    import urllib.request

    ids = real_host.ids
    with urllib.request.urlopen(real_host.url + PREFIX + "list?" + urllib.parse.urlencode({"area": "story", "limit": "5"}), timeout=60) as r:
        legacy = next(e for e in json.loads(r.read())["entries"] if e["kind"] == "legacy")
    c = card()
    c.open([{"v": "room", "room": "1"}])
    meta = "k => document.querySelector('[data-key=\"' + k + '\"] .top .meta').textContent"
    assert c.ev(meta, "record:" + ids["weekly"]) == "Page · 24 Oct"
    assert c.ev(meta, "record:" + ids["part"]) == "Part"   # period unknown: nothing, not the day it was written
    assert c.ev(meta, "record:" + legacy["id"]) == "Earlier dialogue · " + legacy["period"]
    c.open([{"v": "room", "room": "1"}, rec(ids, "weekly")])
    assert c.title() == "Main, 24 October"
    assert c.ev(FACT, "Period") == "2026-10-24 07:55 → 2026-10-24 08:05"
    assert c.ev("() => document.querySelector('.cite').textContent").startswith("Account, Main, 24 Oct (incomplete)")
    c.shot("period-page")
    c.open([{"v": "room", "room": "1"}, rec(ids, "part")])
    assert c.title() == "Main" and c.ev(FACT, "Period") == "period unknown"
    c.open([{"v": "story"}, {"v": "record", "id": ids["account"], "label": "Account", "hint": {"kind": "account", "room": "1"}}])
    assert c.title().startswith("Across ") and c.ev(FACT, "Period") == "2026-10-24 07:55 → 2026-10-24 08:05 (incomplete)"
    c.open([{"v": "room", "room": "1"}, {"v": "record", "id": legacy["id"], "label": "Earlier dialogue", "hint": {"kind": "legacy", "room": "1"}}])
    assert c.title() == "Main" and c.ev(FACT, "Period") == legacy["period"]   # a text-only period stays text
    c.open([{"v": "room", "room": "1"}, rec(ids, "fix_weekly")])
    assert c.ev(FACT, "Period") is None   # a correction is not dated by the core
    c.assert_clean()


def test_knowledge_archived_nested_and_the_whole_summary(card, real_host):
    c = card()
    c.click("Knowledge")
    assert c.settle() == "Knowledge"
    c.click("Archived", exact=True)
    c.settle()
    rows = c.ev("() => Array.from(document.querySelectorAll('.shelf .row')).map(r => r.innerText)")
    assert len(rows) == 1 and "Archived" in rows[0]
    c.frame.locator(".shelf .row").first.click()
    c.settle()
    assert "Archived notes stay readable and keep their address." in c.text()
    c.open([{"v": "knowledge", "scope": "project:atlas-ru", "view": "all"}])
    groups = c.ev("() => Array.from(document.querySelectorAll('.shelf .h-group')).map(e => e.textContent)")
    assert groups == ["архив", "большая"]
    assert c.frame.locator(".shelf .row").count() == 65
    assert c.frame.locator(".shelf .row", has_text="No summary").count() == 13
    c.shot("shelf-ru")
    c.open([{"v": "knowledge"}, {"v": "note", "scope": "global", "topic": "атлас/длинное-резюме", "label": "Длинное резюме"}])
    assert c.ev("() => document.querySelector('.kept').textContent").endswith("…")   # the summary's first words
    c.click("Read the whole summary")
    c.settle()
    whole = c.ev("() => document.querySelector('pre.source').textContent")
    assert len(whole) > 2000 and whole.startswith("Длинное авторское резюме") and not whole.endswith("…")
    assert c.ev("() => !document.querySelector('[data-key=\"read-on\"]')")
    c.assert_clean()


def test_a_long_room_shows_more_until_complete(card, real_host):
    c = card()
    c.open([{"v": "room", "room": real_host.ids["room:atlas-ru"]}])
    story = ".section:has(> .h-group:text-is('Story in this room'))"
    assert c.frame.locator(story + " .rows > .row").count() == 50
    for expected in (100, 121):   # 120 notes and the long page; its correction is not a story record
        c.frame.locator(story + " .pager-foot button").click()
        c.frame.wait_for_function("([s, n]) => document.querySelectorAll(s).length >= n",
                                  arg=[".section .rows > .row", expected], timeout=60000)
    assert c.frame.locator(story + " .rows > .row").count() == 121
    assert c.frame.locator(story + " .pager-foot button").count() == 0
    c.assert_clean()


def test_search_states_what_it_read_and_searches_further(card, real_host):
    c = card()
    field = c.frame.get_by_role("searchbox", name="Search memory text")
    field.fill("заметка")
    field.press("Enter")
    c.settle()
    head = c.ev("() => document.querySelector('.byline').textContent")
    assert re.match(r"^\d+ match(es)? for “заметка” · literal, any letter case · read so far: ", head), head
    c.frame.get_by_role("button", name="Search further").click()
    c.frame.wait_for_function("h => document.querySelector('.byline').textContent !== h", arg=head, timeout=120000)
    assert c.frame.locator("mark").count() > 13
    c.assert_clean()


def test_phone_starts_from_places_and_back_returns_to_the_row(card, real_host):
    ids = real_host.ids
    c = card(viewport=PHONE, theme="light")
    assert c.ev("() => [innerWidth < 560, document.querySelector('.atlas').classList.contains('narrow')]") == [True, True]
    assert c.ev("() => !!document.querySelector('.nav') && !document.querySelector('.pane')")
    c.shot("first-phone")
    c.click("Project Garden sensors")
    assert c.settle() == "Project Garden sensors"
    c.frame.locator(f'[data-key="record:{ids["cadence"]}"]').click()
    c.settle()
    assert c.ev("() => document.scrollingElement.scrollWidth <= innerWidth")
    c.shot("record-phone")
    c.click("Back to Project Garden sensors")
    assert c.settle() == "Project Garden sensors"
    assert c.ev("() => document.activeElement.dataset.key") == "record:" + ids["cadence"]
    c.click("Back to Places")
    c.frame.wait_for_selector(".nav", timeout=30000)
    c.assert_clean()


def test_theme_follows_the_host_and_stop_then_start_remounts(card):
    c = card(theme="light")
    c.click("My story")
    c.settle()
    c.ev("() => { window.__memoryAtlas.marker = 'first mount'; }")
    c.page.emulate_media(color_scheme="dark")
    c.frame.wait_for_function("() => document.documentElement.dataset.theme === 'dark'", timeout=30000)
    assert c.ev("() => [window.__memoryAtlas.marker, window.__memoryAtlas.route().v]") == ["first mount", "story"]
    c.stop()
    c.start()
    assert c.ev("() => window.__memoryAtlas.marker") is None                        # a fresh mount
    assert c.title() == "Main" and c.ev("() => document.documentElement.dataset.theme") == "dark"
    c.stop()
    c.start()
    assert c.title() == "Main"
    c.assert_clean()


def test_a_failed_read_says_so_and_try_again_reads_again(card, real_host):
    c = card()
    c.page.route("**/atlas/v2/text?*", lambda route: route.fulfill(
        status=503, content_type="application/json",
        body='{"contract":"memory-atlas.read/2","error":{"code":"unavailable","message":"The chronicle is busy."}}'))
    c.expected.add(503)
    c.ev("r => window.__memoryAtlas.open(r)", [{"v": "room", "room": "1"}, rec(real_host.ids, "weekly")])
    c.wait_text("Couldn’t read this.")
    assert "The chronicle is busy." in c.text() and "unavailable" in c.text()
    c.page.unroute("**/atlas/v2/text?*")
    c.click("Try again")
    c.settle()
    assert c.frame.locator(".correction").count() == 1
    c.assert_clean()


def test_copy_address_copies_or_selects(card, real_host):
    c = card()
    c.open([{"v": "room", "room": "1"}, rec(real_host.ids, "weekly")])
    c.frame.locator(".addr").get_by_role("button", name="Copy").click()
    c.frame.wait_for_function("() => /Copied|Select and copy/.test(document.querySelector('.addr button').textContent)", timeout=10000)
    c.assert_clean()


HOST_ROOM = "Host records without a room"


def _host_room_requests(host):
    """The host's records as the core lists them unfiltered (room id exactly ''); `seen`, which keeps the room_id of each
    records request for the host's room ('') or for every room (left out: None); and a route handler that answers the
    widget's request for room '' with `answer`, constructed at the route boundary."""
    import urllib.parse
    import urllib.request

    with urllib.request.urlopen(host.url + PREFIX + "list?" + urllib.parse.urlencode({"area": "records", "limit": "500"}), timeout=60) as r:
        every = json.loads(r.read())
    entries = [e for e in every["entries"] if e["room_id"] == ""]
    assert every["complete"] and entries and entries[0]["id"] == host.ids["activation"]
    asked, answer = [], {}

    def seen(request):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(request.url).query, keep_blank_values=True)
        if q.get("area") == ["records"] and q.get("room_id", [None]) in ([""], [None]):
            asked.append(q.get("room_id"))   # None is every room: the host's room must never be asked this way
            return q["room_id"] == [""] if "room_id" in q else False
        return False

    def handle(route):
        if seen(route.request):
            route.fulfill(status=answer["status"], content_type="application/json", body=json.dumps(answer["body"]))
            return
        route.continue_()
    return entries, asked, answer, handle, seen


def test_the_hosts_room_is_asked_by_its_empty_id_and_lists_its_records(card, real_host):
    """The host's room is the room whose id is exactly ''. The widget asks for it by that id (left out, it would be
    every room) and lists its records as a room's, as the core answers them: nothing is constructed. Each opens with
    that room in its Facts."""
    entries, asked, _answer, _handle, seen = _host_room_requests(real_host)
    c = card()
    c.page.on("request", seen)
    c.click(HOST_ROOM)
    assert c.settle() == HOST_ROOM and asked == [[""]]
    rows = ".section .rows > .row"
    assert c.frame.locator(rows).count() == len(entries)
    receipt = c.ev("k => document.querySelector('[data-key=\"' + k + '\"]').innerText", "record:" + real_host.ids["activation"])
    assert receipt.startswith("Activation · ") and receipt.endswith("The chronicle’s activation receipt."), receipt
    c.shot("host-room")
    c.frame.locator(f'[data-key="record:{real_host.ids["activation"]}"]').click()
    c.settle()
    assert c.ev(FACT, "Room") == HOST_ROOM   # the room '' is a room
    c.click(HOST_ROOM, within=".facts-grid")
    assert c.settle() == HOST_ROOM and None not in asked
    c.assert_clean()


def test_a_core_that_refuses_the_empty_room_id_keeps_the_receipt(card, real_host):
    """A core without the empty room id refuses it as this core does (invalid_parameter): the host's room shows the
    activation receipt the overview names, and says why the rest is not listed, never an error."""
    entries, asked, answer, handle, _seen = _host_room_requests(real_host)
    answer.update(status=400, body={"contract": "memory-atlas.read/2", "error": {"code": "invalid_parameter", "message": "room_id is a non-empty string"}})
    c = card()
    c.expected.add(400)
    c.page.route("**/atlas/v2/list?*", handle)
    c.click(HOST_ROOM)
    assert c.settle() == HOST_ROOM and asked == [[""]]
    assert c.frame.locator(f'[data-key="record:{real_host.ids["activation"]}"]').count() == 1
    assert c.frame.locator(".notice").count() == 0 and "Couldn’t read" not in c.text()
    c.assert_clean()


# ---- memory that changes while the widget is open (writes into the host; last in this file) ---------------------

NOTE = "atlas-check/drift"


def test_a_note_changed_while_read_stops_the_reading_and_reads_the_new_one(card, real_host):
    write = (
        "from ouroboros.knowledge import read_knowledge_note, resolve_knowledge_address, write_knowledge_note\n"
        f"address = resolve_knowledge_address(root, {NOTE!r}, 'global')\n"
        "try:\n    revision = read_knowledge_note(address).revision\nexcept FileNotFoundError:\n    revision = None\n"
        "text = '---\\ntitle: Drift check\\n---\\n# Drift check\\n\\n' + BODY\n"
        "result = write_knowledge_note(address, text, writer='turn', **({'expected_revision': revision} if revision else {}))\n"
        "assert result.ok, result\n")
    real_host.producer("BODY = 'Первая версия, длинная. ' * 8000\n" + write)
    c = card()
    c.open([{"v": "knowledge"}, {"v": "note", "scope": "global", "topic": NOTE, "label": "Drift check"}])
    assert c.ev("() => document.querySelector('.reader-foot .meta').textContent").startswith("131,072 of ")
    real_host.producer("BODY = 'Вторая версия.\\n'\n" + write)
    c.expected.add(409)   # the refused range is the widget's evidence that the note changed
    c.frame.locator('[data-key="read-on"]').click()
    c.wait_text("This changed while you were reading.")
    assert "it is not joined to the new one" in c.text()
    c.shot("note-changed")
    c.click("Read the current version")
    c.wait_text("Вторая версия.")
    assert "Первая версия" not in c.text() and c.frame.locator('[data-key="read-on"]').count() == 0
    c.assert_clean()


def test_refresh_reads_only_what_changed_and_says_when_it_cannot(card, real_host):
    c = card()
    assert c.title() == "Main"
    before = c.frame.locator(".section .rows > .row").count()
    real_host.producer(
        "from ouroboros.chronicle_store import ChronicleStore\n"
        "store = ChronicleStore(root)\n"
        "author = {'kind': 'mind', 'task_id': 't-main', 'focus': {'role': 'main chat', 'task_id': 't-main'}}\n"
        "cover = {'mode': 'range', 'rows': ['row-900', 'row-901'], 'stream_span': [900, 901]}\n"
        "result = store.publish_page(room_id='1', text='A page written while the Atlas was open. Synthetic test text.',"
        " covers=cover, author=author)\n"
        "assert result.ok, result\n")
    c.click("Refresh: read what changed")
    c.frame.wait_for_function("() => !document.querySelector('.refresh-note').textContent.startsWith('Checking')", timeout=60000)
    c.wait_text("A page written while the Atlas was open.")
    assert c.ev("() => document.querySelector('.refresh-note').textContent").startswith("1 new record since the last check")
    assert c.frame.locator(".section .rows > .row").count() == before + 1
    c.click("Refresh: read what changed")
    c.frame.wait_for_function("() => document.querySelector('.refresh-note').textContent.startsWith('Nothing new')", timeout=60000)
    c.page.route("**/atlas/v2/changes?*", lambda route: route.fulfill(
        status=503, content_type="application/json",
        body='{"contract":"memory-atlas.read/2","error":{"code":"unavailable","message":"The chronicle is busy."}}'))
    c.expected.add(503)
    c.click("Refresh: read what changed")
    c.frame.wait_for_function("() => document.querySelector('.refresh-note').textContent.startsWith('Couldn’t check for changes')", timeout=60000)
    assert "The chronicle is busy." in c.ev("() => document.querySelector('.refresh-note').textContent")
    c.page.unroute("**/atlas/v2/changes?*")
    c.click("Refresh: read what changed")
    c.frame.wait_for_function("() => document.querySelector('.refresh-note').textContent.startsWith('Nothing new')", timeout=60000)
    c.assert_clean()


def test_refresh_keeps_saying_it_could_not_check_the_open_note_and_recovers(card):
    """The open note's revision check fails while the chronicle answers: the line says both, the failure is not
    replaced by "Nothing new", and the next Refresh that can check says Nothing new alone."""
    import urllib.parse

    c = card()
    c.open([{"v": "knowledge"}, {"v": "note", "scope": "global", "topic": "people/mira", "label": "Mira"}])

    def busy_revision_check(route):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(route.request.url).query)
        if q.get("length") == ["1"]:   # the widget's revision check of the open note, nothing else
            route.fulfill(status=503, content_type="application/json",
                          body='{"contract":"memory-atlas.read/2","error":{"code":"unavailable","message":"The note store is busy."}}')
            return
        route.continue_()

    c.expected.add(503)
    c.page.route("**/atlas/v2/text?*", busy_revision_check)
    c.click("Refresh: read what changed")
    c.frame.wait_for_function("() => !document.querySelector('.refresh-note').textContent.startsWith('Checking')", timeout=60000)
    c.page.wait_for_timeout(300)
    note = c.ev("() => document.querySelector('.refresh-note').textContent")
    assert note.startswith("Nothing new · checked ") and note.endswith("Couldn’t check the open note or file: The note store is busy."), note
    assert c.title() == "Mira"   # what was open stays open
    c.shot("refresh-open-check-failed")
    c.page.unroute("**/atlas/v2/text?*")
    c.click("Refresh: read what changed")
    c.frame.wait_for_function("() => !document.querySelector('.refresh-note').textContent.startsWith('Checking')", timeout=60000)
    note = c.ev("() => document.querySelector('.refresh-note').textContent")
    assert note.startswith("Nothing new · checked ") and "Couldn’t" not in note, note
    c.assert_clean()


def test_an_overview_past_the_first_page_shows_in_its_row_then_above(card, real_host):
    """105 notes come before the shelf's overview in the core's order, past the view's first page of 100. Show more
    brings the overview in its row; drawn again, the view shows it above the list, and only there."""
    ensure_late_overview(real_host)
    c = card()
    c.open([{"v": "knowledge", "scope": LATE_SCOPE}])
    overview = f'[data-key="note:{LATE_SCOPE}|overview"]'
    assert c.frame.locator(".shelf .row").count() == 100 and c.frame.locator(overview).count() == 0
    c.frame.locator(".shelf .pager-foot button").click()
    c.frame.wait_for_function("() => !document.querySelector('.shelf .pager-foot button')", timeout=60000)
    shown = {"rows": c.frame.locator(".shelf .row").count(), "overview": c.frame.locator(overview).count(),
             "above": c.frame.locator(".row.overview").count()}
    assert shown == {"rows": 106, "overview": 1, "above": 0}, shown
    c.frame.locator(overview).scroll_into_view_if_needed()
    c.shot("overview-in-its-row")
    c.frame.locator(overview).click()
    assert c.settle() == "Overview"
    c.page.keyboard.press("Escape")
    assert c.settle() == "Knowledge"
    assert c.frame.locator(".row.overview").count() == 1 and c.frame.locator(overview).count() == 1
    assert c.frame.locator(".shelf .row").count() == 105
    assert c.ev("() => document.activeElement.dataset.key") == f"note:{LATE_SCOPE}|overview"   # Back lands on it
    c.assert_clean()


def test_refresh_says_how_much_of_a_long_shelf_it_compared(card, real_host):
    """Refresh compares an open shelf by its first page. On a longer shelf it says so, so a note changed after that page
    is never reported as nothing new; a shelf of one page says Nothing new alone."""
    ensure_late_overview(real_host)
    c = card()
    c.open([{"v": "knowledge", "scope": LATE_SCOPE}])
    c.frame.locator(".shelf .pager-foot button").click()   # read past the first page, to the overview
    c.frame.wait_for_function("() => !document.querySelector('.shelf .pager-foot button')", timeout=60000)
    real_host.producer(PROJECT_AND_NOTE + ("put(%r, 'earlier/note-104', 'Earlier note 104, renamed')\n" % LATE_SCOPE))
    note = "() => document.querySelector('.refresh-note').textContent"
    c.click("Refresh: read what changed")
    c.frame.wait_for_function("() => !document.querySelector('.refresh-note').textContent.startsWith('Checking')", timeout=60000)
    assert re.fullmatch(r"Nothing new · checked \d\d:\d\d\. Only this shelf’s first 100 notes were compared\.", c.ev(note)), c.ev(note)
    c.shot("refresh-long-shelf")
    real_host.producer(PROJECT_AND_NOTE + ("put(%r, 'earlier/note-000', 'Earlier note 000, changed')\n" % LATE_SCOPE))
    c.click("Refresh: read what changed")
    c.frame.get_by_text("Earlier note 000, changed", exact=True).wait_for(timeout=60000)
    assert re.fullmatch(r"This place changed and was read again · checked \d\d:\d\d\. Only this shelf’s first 100 notes were compared\.", c.ev(note)), c.ev(note)
    c.open([{"v": "knowledge"}])   # the global shelf: one page, compared whole
    c.click("Refresh: read what changed")
    c.frame.wait_for_function("() => !document.querySelector('.refresh-note').textContent.startsWith('Checking')", timeout=60000)
    assert re.fullmatch(r"Nothing new · checked \d\d:\d\d\.", c.ev(note)), c.ev(note)
    c.assert_clean()


def test_every_shelf_can_be_chosen_past_the_overviews_first_page(card, real_host):
    """104 more Projects with notes: the shelves run past the overview's first page of 100. The list of places says
    its count is partial, the Shelf choice stays a few choices beside More shelves, and the last shelf is reached a
    page at a time and opens."""
    import urllib.parse
    import urllib.request

    ensure_many_shelves(real_host)
    entries, page = [], {"next": None}
    while True:   # every shelf, as the route pages them
        query = {"area": "shelves", "limit": "100", **({"cursor": page["next"]} if page["next"] else {})}
        with urllib.request.urlopen(real_host.url + PREFIX + "list?" + urllib.parse.urlencode(query), timeout=60) as r:
            page = json.loads(r.read())
        entries += page["entries"]
        if page["complete"]:
            break
    present, last = [s for s in entries if s["present"]], entries[-1]
    assert len(entries) > 100 and last["scope"] == "project:shelf-check-103" and last["present"]
    c = card()
    count = "() => document.querySelector('.nav [data-key=\"nav:knowledge\"] .facts').textContent"
    c.click("Knowledge")
    assert c.settle() == "Knowledge"
    seen = {"count": c.ev(count), "choices": c.ev("() => document.querySelectorAll('.seg[aria-label=\"Shelf\"] button').length"),
            "more": c.frame.get_by_role("button", name="More shelves").count()}
    assert seen == {"count": "%d+ shelves" % sum(1 for s in entries[:100] if s["present"]), "choices": 4, "more": 1}, seen
    c.shot("shelf-choice-bounded")
    c.click("More shelves")
    assert c.settle() == "Shelves"
    rows = ".pane .rows > .row"
    assert c.frame.locator(rows).count() == 100
    c.shot("shelves-list")
    c.frame.get_by_role("button", name="Show more").click()
    c.frame.wait_for_function("([s, n]) => document.querySelectorAll(s).length >= n", arg=[rows, len(entries)], timeout=60000)
    assert c.frame.locator(rows).count() == len(entries) and c.frame.get_by_role("button", name="Show more").count() == 0
    assert c.ev(count) == "%d shelves" % len(present)   # read to the end: the count is whole
    c.frame.locator(f'[data-key="shelf:{last["scope"]}"]').click()
    assert c.settle() == "Knowledge"
    assert c.ev("() => document.querySelector('.kicker').textContent") == "Shelf: Shelf check 103"
    assert c.ev("() => document.querySelector('.seg[aria-label=\"Shelf\"] [aria-pressed=\"true\"]').textContent") == "Shelf check 103"
    assert c.frame.locator(".shelf .row").count() == 1 and c.ev("() => document.querySelectorAll('.pane button').length") < 30
    c.shot("shelf-past-the-first-page")
    c.page.keyboard.press("Escape")
    assert c.settle() == "Shelves" and c.frame.locator(rows).count() == len(entries)   # Back keeps what was read
    c.assert_clean()


LINKS_TO, LINKED_FROM = ".block:has(> .h-group:text-is('Links to'))", ".block:has(> .h-group:text-is('Linked from'))"
BUSY = '{"contract":"memory-atlas.read/2","error":{"code":"unavailable","message":"The note store is busy."}}'


def _ask(host, op, **query):
    """This skill's route asked directly: (status, answer)."""
    import urllib.error
    import urllib.parse
    import urllib.request

    encoded = {k: json.dumps(v) if isinstance(v, dict) else str(v) for k, v in query.items()}
    try:
        with urllib.request.urlopen(host.url + PREFIX + op + "?" + urllib.parse.urlencode(encoded), timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_links_to_a_changed_note_stay_until_the_list_is_read_again_from_the_start(card, real_host):
    """"Links to" continues by the core's cursor, bound to the note's revision. A failure that is not a change keeps
    the cursor: Try again asks the same one. The note edited after two pages: Show more is refused (revision_mismatch).
    The list says it changed and keeps the 400 rows it read until the list read again from the start has its first page
    (a failure of that read keeps them too); that page replaces them, and Show more goes on in the new version by its
    own cursor. The refused cursor is asked once."""
    import urllib.parse

    from playwright.sync_api import expect

    ensure_many_links(real_host)
    note = {"kind": "note", "scope": LINKS_SCOPE, "topic": LINKS_MANY}
    before = _ask(real_host, "text", address=note, length=1)[1]["revision"]
    asked, fail = [], {"cursor": 0, "first": 0}

    def links(route):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(route.request.url).query)
        if q.get("relation") == ["links"] and LINKS_MANY in q.get("address", [""])[0]:
            cursor = q.get("cursor", [None])[0]
            asked.append(cursor)
            if fail["cursor" if cursor else "first"]:
                fail["cursor" if cursor else "first"] -= 1
                route.fulfill(status=503, content_type="application/json", body=BUSY)
                return
        route.continue_()

    c = card()
    rows, foot = c.frame.locator(LINKS_TO + " .links > .linkrow"), c.frame.locator(LINKS_TO + " .pager-foot")
    notice = foot.locator(".notice")
    labels = lambda: rows.evaluate_all("rows => rows.map((r) => r.firstChild.textContent)")   # noqa: E731
    c.expected.update({409, 503})
    c.page.route("**/atlas/v2/list?*", links)
    try:
        c.ev("r => window.__memoryAtlas.open(r)", [{"v": "knowledge", "scope": LINKS_SCOPE},
                                                   {"v": "note", "scope": LINKS_SCOPE, "topic": LINKS_MANY, "label": "Many links"}])
        foot.get_by_role("button", name="Show more").wait_for(timeout=60000)
        expect(rows).to_have_count(200)
        assert asked == [None]
        fail["cursor"] = 1   # a failure that is not a change
        foot.get_by_role("button", name="Show more").click()
        expect(foot).to_contain_text("Couldn’t read more: The note store is busy.", timeout=60000)
        assert rows.count() == 200
        foot.get_by_role("button", name="Try again").click()
        expect(rows).to_have_count(400, timeout=60000)
        assert asked[1:] == ["i200:" + before, "i200:" + before]   # the same cursor, asked again
        now = edit_many_links(real_host)
        foot.get_by_role("button", name="Show more").click()
        notice.wait_for(timeout=60000)
        said = notice.inner_text()
        assert said.startswith("This list changed while you were reading it.") and "it is not joined to the new one" in said
        assert rows.count() == 400 and labels()[:2] == ["n0000", "n0001"] and asked[-1] == "i400:" + before
        assert foot.get_by_role("button", name="Show more").count() == 0
        c.shot("links-to-changed")
        fail["first"] = 1   # the list read again fails once: what was read stays
        foot.get_by_role("button", name="Read again from the start").click()
        expect(notice).to_contain_text("Couldn’t read it again: The note store is busy.", timeout=60000)
        assert rows.count() == 400 and labels()[0] == "n0000" and asked[-1] is None
        foot.get_by_role("button", name="Try again").click()
        expect(notice).to_have_count(0, timeout=60000)
        expect(rows).to_have_count(200)
        shown = labels()
        assert shown[:2] == ["the target", "w0000"] and not [x for x in shown if x.startswith("n")] and asked[-1] is None
        foot.get_by_role("button", name="Show more").click()
        expect(rows).to_have_count(400, timeout=60000)
        assert labels()[200] == "w0199" and asked[-1] == "i200:" + now
        assert asked.count("i400:" + before) == 1   # the refused cursor was never asked again
        c.assert_clean()
    finally:
        ensure_many_links(real_host)


def test_linked_from_reads_again_from_the_start_when_the_note_it_continues_changed(card, real_host):
    """Links in stop inside the many-links note, 2,000 of its links read; the note is edited before Look further. The
    step is refused as the core refused it (revision_mismatch): the list says it changed, not "Couldn't read more",
    and read again from the start it reads the new version, whose link into the target comes first."""
    from playwright.sync_api import expect

    ensure_many_links(real_host)
    c = card()
    foot = c.frame.locator(LINKED_FROM + " .pager-foot")
    c.expected.add(409)
    try:
        c.ev("r => window.__memoryAtlas.open(r)", [{"v": "knowledge", "scope": LINKS_SCOPE},
                                                   {"v": "note", "scope": LINKS_SCOPE, "topic": LINKS_TARGET, "label": "Links target"}])
        foot.get_by_role("button", name="Look further").wait_for(timeout=60000)
        assert "Still checking the links of “links-check/many”: 2,000 read so far" in foot.inner_text()
        edit_many_links(real_host)
        foot.get_by_role("button", name="Look further").click()
        foot.locator(".notice").wait_for(timeout=60000)
        said = foot.locator(".notice").inner_text()
        assert said.startswith("This list changed while you were reading it.") and "earlier version" not in said   # none was shown
        assert "Couldn’t read more" not in c.text() and "No note on this shelf links here" not in c.text()
        c.shot("linked-from-changed")
        foot.get_by_role("button", name="Read again from the start").click()
        c.frame.locator(f'{LINKED_FROM} [data-key="note:{LINKS_SCOPE}|{LINKS_MANY}"]').wait_for(timeout=60000)
        expect(foot.locator(".notice")).to_have_count(0)
        expect(foot.locator("button")).to_have_count(0)
        c.assert_clean()
    finally:
        ensure_many_links(real_host)


def test_a_sources_nested_changes_that_changed_are_read_again_from_the_start(card, real_host):
    """An account cites a part whose two pages were corrected 26 times: the part's source row shows 25 of those nested
    changes and Show more. One more correction changes that list, so the core refuses the next page (position_mismatch:
    the cursor names the list's fingerprint), at the route and in the widget. The widget keeps its 25 rows and says the
    list changed; read again from the start, the new first page replaces them (25 rows, not 50) and Show more reads the
    new list to its end, 27."""
    from playwright.sync_api import expect

    fixture = make_nested_changes(real_host, 26)
    query = {"area": "relations", "address": {"kind": "record", "id": fixture["account"]}, "relation": "source_changes",
             "source": 0, "limit": 25}
    status, first = _ask(real_host, "list", **query)
    assert status == 200 and first["count"] == 26 and len(first["entries"]) == 25 and first["next"] == "i25:" + first["fingerprint"]
    c = card()
    rows, foot = c.frame.locator(".nested-list > .nested"), c.frame.locator(".nested-list + .pager-foot")
    c.expected.add(409)
    c.ev("r => window.__memoryAtlas.open(r)", [{"v": "room", "room": fixture["room"]},
                                               {"v": "record", "id": fixture["account"], "label": "Account", "hint": {"kind": "account", "room": fixture["room"]}}])
    source = c.frame.locator(".srcrow", has_text="26 changes inside it since")
    source.wait_for(timeout=60000)
    source.get_by_role("button", name="Show them").click()
    foot.get_by_role("button", name="Show more").wait_for(timeout=60000)
    expect(rows).to_have_count(25)
    correct_page(real_host, fixture, fixture["pages"][1])
    status, body = _ask(real_host, "list", **query, cursor=first["next"])
    assert status == 409 and body["error"]["code"] == "position_mismatch", body   # the route, as the core refused it
    foot.get_by_role("button", name="Show more").click()
    foot.locator(".notice").wait_for(timeout=60000)
    assert rows.count() == 25 and "it is not joined to the new one" in foot.locator(".notice").inner_text()
    c.shot("nested-changes-changed")
    foot.get_by_role("button", name="Read again from the start").click()
    expect(foot.locator(".notice")).to_have_count(0, timeout=60000)
    expect(rows).to_have_count(25)
    foot.get_by_role("button", name="Show more").click()
    expect(rows).to_have_count(27, timeout=60000)
    expect(foot.locator("button")).to_have_count(0)
    status, again = _ask(real_host, "list", **query)
    assert status == 200 and again["count"] == 27 and again["fingerprint"] != first["fingerprint"]
    c.assert_clean()


# ---- a large note (kept last: on a core without the markdown range fix its abandoned reads busy the host) -------

def test_a_note_over_one_range_opens_promptly_and_reads_on(card):
    """1,158,920 code points. The core's `markdown_source` finds each node's range by a bisected line index: about
    0.6 s a read, where recounting the source from byte 0 for every node took about 36 s."""
    c = card()
    c.ev("r => window.__memoryAtlas.open(r)", [{"v": "knowledge", "scope": "project:atlas-ru"},
                                               {"v": "note", "scope": "project:atlas-ru", "topic": "большая/заметка"}])
    assert c.settle(timeout=20000) == "Большая заметка"
    assert c.ev("() => document.querySelector('.reader-foot .meta').textContent").startswith("131,072 of ")
    c.frame.locator('[data-key="read-on"]').click()
    c.frame.wait_for_function("() => document.querySelector('.reader-foot .meta').textContent.startsWith('262,144 of ')",
                              timeout=20000)
    c.assert_clean()
