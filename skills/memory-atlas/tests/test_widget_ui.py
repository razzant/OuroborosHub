"""UI tests for the Memory Atlas module widget.

The widget is loaded in a real browser against tests/ui_harness.html, whose
OuroborosWidget.fetch stub answers only exact (path, query) fixtures from
tests/ui_fixtures.py. A parameter rename or a request off the skill prefix shows
up as an unmatched request, which every test asserts against.

Run with:  python3.11 -m pytest tests/test_widget_ui.py
Chromium comes from the system Google Chrome channel; no download is required.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from ui_fixtures import (  # noqa: E402  (path is set below)
    CATALOG_ITEMS,
    DIALOGUE_SIGNATURE_OTHER,
    DIALOGUE_SIGNATURE_PLAIN,
    DIALOGUE_SIGNATURE_TOP_LEVEL,
    GRAPH_EDGES,
    fixtures,
    fixtures_dialogue_long,
    fixtures_dialogue_malformed,
    fixtures_dialogue_signature,
    fixtures_dialogue_unavailable,
    fixtures_empty_graph_incomplete,
    fixtures_search_incomplete,
    fixtures_without_legacy_dialogue,
    fixtures_without_project,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
HARNESS = (ROOT / "tests" / "ui_harness.html").as_uri()
WIDGET = ROOT / "widget.js"


@pytest.fixture(scope="session")
def browser():
    with playwright_api.sync_playwright() as api:
        instance = api.chromium.launch(channel="chrome")
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1100, "height": 722})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.add_init_script("window.__FIXTURES = " + json.dumps(fixtures()) + ";")
    page.goto(HARNESS)
    page.wait_for_selector("#memory-atlas-root .ma-row")
    page.__dict__["console_errors"] = errors
    yield page
    assert errors == [], f"uncaught page errors: {errors}"
    context.close()


def unmatched(page):
    return page.evaluate("window.__unmatched")


def assert_all_requests_matched(page):
    assert unmatched(page) == [], f"requests did not match the frozen contract: {unmatched(page)}"


def open_source(page, title: str):
    page.click(f'.ma-row:has(.ma-row-title:text-is("{title}"))')
    page.wait_for_selector(".ma-strip .ma-title")


def swap_fixtures(page, new_fixtures):
    """Replace the stub's fixture table, then make the widget read again."""
    page.evaluate("table => { window.__FIXTURES = table; }", new_fixtures)
    page.click('.ma-top button:text-is("Refresh")')


def row_titles(page):
    return page.locator(".ma-row .ma-row-title").all_text_contents()


# --------------------------------------------------------------------------- #
# payload policy
# --------------------------------------------------------------------------- #

def test_widget_payload_has_no_unsafe_sinks_or_remote_assets():
    text = WIDGET.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith(("*", "/*", "//"))
    )
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                      "eval(", "new Function", "localStorage", "sessionStorage",
                      "window.parent", "window.top", "createContextualFragment"):
        assert forbidden not in code, f"{forbidden} must not appear in the widget payload"
    remote = [url for url in re.findall(r"https?://[^\s'\")]+", code)
              if not url.startswith("http://www.w3.org/")]
    assert remote == [], f"no remote assets are allowed: {remote}"
    assert "import " not in code.replace("important", "")
    assert "export " not in code


def test_the_payload_has_no_inference_layer_and_no_model_call():
    text = WIDGET.read_text(encoding="utf-8")
    assert "inferred" not in text, "the inference layer must be removed, not renamed"
    # No model, no foreign transport: the only network surface is the bridge.
    for forbidden in ("XMLHttpRequest", "WebSocket", "EventSource", "navigator.sendBeacon",
                      "anthropic", "openai", "completions", "/v1/messages"):
        assert forbidden not in text, f"{forbidden} must not appear in the widget payload"
    code = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith(("*", "/*", "//"))
    )
    assert "window.OuroborosWidget.fetch" in code
    # the widget never calls anything but the host bridge
    assert re.findall(r"([A-Za-z_.]*)\bfetch\s*\(", code) == []


def test_every_request_stays_on_the_skill_prefix(page):
    urls = page.evaluate("window.__requests")
    assert urls, "the widget made no requests"
    assert all(url.startswith("/api/extensions/memory-atlas/") for url in urls), urls
    assert "inferred" not in " ".join(urls), "the removed parameter must never be sent"
    assert_all_requests_matched(page)


# --------------------------------------------------------------------------- #
# fix 1 — the default view is global memory
# --------------------------------------------------------------------------- #

def test_cold_start_opens_global_memory_and_not_the_first_project(page):
    # Projects exist and "atlas" sorts first, which is exactly the case the old
    # selection logic silently opened on.
    assert page.evaluate("window.__memoryAtlasTestHooks.state.projects") == ["atlas", "beacon"]
    assert page.evaluate("window.__memoryAtlasTestHooks.state.scope") is None
    assert page.locator(".ma-h1").inner_text() == "Global memory"
    assert page.input_value("#ma-scope") == ""
    titles = row_titles(page)
    assert "patterns" in titles
    for project_only in ("design", "workpad", "journal", "notes", "task reflections"):
        assert project_only not in titles, titles
    # the rail says the same thing
    assert "Global memory" in page.locator(".ma-rail").inner_text()
    assert_all_requests_matched(page)


def test_the_scope_switcher_names_the_active_scope_on_screen(page):
    options = page.locator("#ma-scope option").all_text_contents()
    assert options[0] == "Global memory"
    assert "atlas" in options and "beacon" in options
    page.select_option("#ma-scope", "atlas")
    page.wait_for_selector('.ma-h1:text-is("atlas")')
    assert "atlas" in page.locator(".ma-sub").inner_text()
    assert "atlas" in page.locator(".ma-rail h2").first.inner_text()
    page.select_option("#ma-scope", "")
    page.wait_for_selector('.ma-h1:text-is("Global memory")')
    assert_all_requests_matched(page)


def test_scope_options_and_focus_survive_an_ordinary_render(page):
    scope = page.locator("#ma-scope")
    # Global is the cold-start value, and an explicit project choice renders the
    # rest of the UI synchronously. The shell's native option nodes must remain
    # intact so choosing a scope does not throw away focus or selection state.
    assert scope.input_value() == ""
    scope.evaluate("""node => {
        window.__scopeOptionsBeforeRender = Array.from(node.options);
        node.focus();
    }""")
    page.select_option("#ma-scope", "atlas")
    page.wait_for_selector('.ma-h1:text-is("atlas")')
    assert page.evaluate("""() => {
        const scope = document.getElementById('ma-scope');
        return document.activeElement === scope
          && Array.from(scope.options).every((option, index) =>
            option === window.__scopeOptionsBeforeRender[index]);
    }""")
    assert scope.input_value() == "atlas"
    assert_all_requests_matched(page)


def test_scope_switcher_has_room_for_a_readable_project_name(page):
    scope = page.locator("#ma-scope")
    style = scope.evaluate("node => getComputedStyle(node)")
    assert style["maxWidth"] != "120px"
    assert scope.bounding_box()["width"] >= 200


def test_a_project_that_vanishes_falls_back_to_global_not_another_project(page):
    page.select_option("#ma-scope", "atlas")
    page.wait_for_selector('.ma-row-title:text-is("design")')
    swap_fixtures(page, fixtures_without_project("atlas"))
    page.wait_for_selector('.ma-h1:text-is("Global memory")')
    assert page.evaluate("window.__memoryAtlasTestHooks.state.scope") is None
    assert page.input_value("#ma-scope") == ""
    # beacon still exists but must not be silently substituted
    assert "notes" not in row_titles(page)
    assert_all_requests_matched(page)


def test_choosing_a_project_then_returning_is_explicit_both_ways(page):
    page.select_option("#ma-scope", "beacon")
    page.wait_for_selector('.ma-row-title:text-is("notes")')
    titles = row_titles(page)
    assert "notes" in titles and "workpad" not in titles
    page.select_option("#ma-scope", "")
    page.wait_for_selector('.ma-h1:text-is("Global memory")')
    assert "notes" not in row_titles(page)
    assert_all_requests_matched(page)


# --------------------------------------------------------------------------- #
# fix 5 — the first screen
# --------------------------------------------------------------------------- #

def test_the_first_screen_states_where_you_are_and_what_is_here(page):
    assert page.locator(".ma-h1").inner_text() == "Global memory"
    subtitle = page.locator(".ma-sub").inner_text()
    assert "sources" in subtitle
    groups = page.locator(".ma-group-title").all_text_contents()
    assert "Identity" in groups and "Knowledge" in groups
    for group in page.locator(".ma-group").all():
        assert group.locator(".ma-group-desc").inner_text().strip(), "every group explains itself"
    # rows carry title, last-modified and a history chip
    row = page.locator('.ma-row:has(.ma-row-title:text-is("patterns"))')
    assert "Last changed" in row.inner_text()
    assert "Snapshots and activity events" in row.inner_text()
    assert_all_requests_matched(page)


def test_the_removed_lane_apparatus_is_gone(page):
    for selector in (".ma-lane", ".ma-capsule", ".ma-spark", ".ma-axis", ".ma-track"):
        assert page.locator(selector).count() == 0, f"{selector} must be gone"
    assert page.locator('button:text-is("List view")').count() == 0
    assert page.locator('button:text-is("Lane view")').count() == 0
    assert "Strata" not in page.locator("#memory-atlas-root").inner_text()


def test_rows_are_keyboard_reachable_and_open_the_source(page):
    row = page.locator('.ma-row:has(.ma-row-title:text-is("patterns"))')
    row.focus()
    assert page.evaluate(
        "document.activeElement.getAttribute('data-source-id')") == "knowledge:patterns"
    page.keyboard.press("Enter")
    page.wait_for_selector('.ma-strip .ma-title:text-is("patterns")')
    assert_all_requests_matched(page)


def test_an_undated_source_says_so_without_inventing_a_position(page):
    row = page.locator('.ma-row:has(.ma-row-title:text-is("undated"))')
    assert "No modification time" in row.inner_text()
    dated = page.locator('.ma-row:has(.ma-row-title:text-is("patterns")) .ma-row-when')
    assert "Last changed" in dated.inner_text()
    assert "not a record of how far" in dated.get_attribute("title")
    assert_all_requests_matched(page)


def test_the_excluded_set_is_stated_on_the_first_screen(page):
    box = page.locator("details.ma-disclose")
    assert box.count() == 1
    assert "What is not shown here" in box.inner_text()
    box.locator("summary").click()
    body = box.inner_text()
    for phrase in ("Raw chat log", "Owner mailbox", "Control and queue state",
                   "Settings", "Secrets", "Tool and execution logs"):
        assert phrase in body, body
    assert "not a memory source" in body
    assert "left out on purpose" in body
    assert box.locator("li").count() == 6
    assert_all_requests_matched(page)


def test_the_history_status_legend_is_present_in_plain_words(page):
    legend = page.locator('.ma-legend:has(h3:text-is("What the history labels mean"))')
    assert legend.count() >= 1
    body = legend.first.inner_text()
    assert "Full snapshot" in body and "the earlier text itself is stored" in body
    assert "Digest only" in body and "just a fingerprint survives, the old text is gone" in body
    assert "Activity event" in body and "we know it changed, not what it said" in body
    assert "History unavailable" in body and "nothing was recorded" in body
    assert_all_requests_matched(page)


def test_a_missing_history_store_is_named_and_keeps_the_backend_gap(page):
    # `unavailable` is not `none` and must not render as "History unknown".
    row = page.locator('.ma-row:has(.ma-row-title:text-is("vanished"))')
    assert "History could not be read" in row.inner_text()
    open_source(page, "vanished")
    strip = page.locator(".ma-strip").inner_text()
    assert "History could not be read" in strip
    assert "History unknown" not in strip
    page.click('.ma-tab[data-tab="history"]')
    page.wait_for_selector('.ma-gaps:has-text("history unavailable")')
    page.wait_for_selector('.ma-empty:has-text("History could not be read")')
    panel = page.locator(".ma-scroll").inner_text()
    assert "could not be opened" in panel
    assert "No history entries were retained" not in panel
    assert "knowledge:vanished · history unavailable" in page.locator(".ma-gaps").inner_text()
    assert_all_requests_matched(page)


def test_sources_without_history_say_so(page):
    # `history: none` — nothing was recorded. Distinct from `unavailable`
    # (the store exists but could not be read); tooling covers that sibling.
    open_source(page, "World profile")
    assert "History unavailable" in page.locator(".ma-strip").inner_text()
    assert "History could not be read" not in page.locator(".ma-strip").inner_text()
    page.click('.ma-tab[data-tab="history"]')
    page.wait_for_selector(".ma-empty")
    panel = page.locator(".ma-scroll").inner_text()
    assert "History unavailable" in panel
    assert "nothing was recorded" in panel
    assert_all_requests_matched(page)


def test_sources_whose_history_store_could_not_be_read_say_so(page):
    # tooling's catalogue field is `unavailable`, not `none`.
    open_source(page, "tooling")
    assert "History could not be read" in page.locator(".ma-strip").inner_text()
    assert "History unavailable" not in page.locator(".ma-strip").inner_text()
    page.click('.ma-tab[data-tab="history"]')
    page.wait_for_selector('.ma-empty:has-text("History could not be read")')
    empty = page.locator(".ma-empty").inner_text()
    assert "could not be opened" in empty
    assert "nothing was recorded" not in empty
    assert_all_requests_matched(page)


# --------------------------------------------------------------------------- #
# fix 2 — the new source families
# --------------------------------------------------------------------------- #

NEW_SOURCES = [
    ("Dialogue chronicle", "Dialogue chronicle",
     "Consolidated summary blocks of past conversation."),
    ("Dialogue summary (legacy)", "Dialogue summary (legacy)",
     "Nothing writes to it any more, so it is not current."),
    ("World profile", "World profile", "Generated environment profile."),
    ("Memory source registry", "Memory source registry",
     "The map of memory sources with trust and gap annotations."),
    ("Latest self-review", "Latest self-review",
     "there is no evolution timeline for it"),
    ("Task reflections", "Task reflections",
     "Recorded execution history of finished tasks"),
]


@pytest.mark.parametrize("title,group,phrase", NEW_SOURCES)
def test_each_new_source_is_named_described_and_opens(page, title, group, phrase):
    section = page.locator(f'.ma-group:has(.ma-group-title:text-is("{group}"))')
    assert section.count() == 1, f"{group} must have its own labelled section"
    assert phrase in section.locator(".ma-group-desc").inner_text()
    open_source(page, title)
    # The compact header writes the document title once, inside the breadcrumb.
    assert page.locator(".ma-strip .ma-title").inner_text() == title
    assert page.locator(".ma-crumb .ma-title").count() == 1
    # The category description is not repeated as a second paragraph: it stays
    # reachable on the category itself, which is where it belongs.
    assert phrase in page.locator(".ma-strip .ma-crumb-family").get_attribute("title")
    assert phrase not in page.locator(".ma-strip").inner_text()
    assert_all_requests_matched(page)


def test_task_reflections_are_described_as_execution_not_reasoning(page):
    section = page.locator('.ma-group:has(.ma-group-title:text-is("Task reflections"))')
    body = section.locator(".ma-group-desc").inner_text()
    assert "record of execution, not hidden reasoning" in body
    assert "what was done, what it cost" in body.lower()
    assert_all_requests_matched(page)


def test_a_project_has_its_own_task_reflections(page):
    # NDJSON execution history opens as source text, not Markdown. The
    # project's full row lives only in the project log; the global log
    # carries a pointer to it and must stay distinguishable.
    open_source(page, "Task reflections")
    page.wait_for_selector(".ma-srclines")
    global_text = page.locator(".ma-srclines").inner_text()
    assert "project_reflection_pointer" in global_text
    assert "project reflection body" not in global_text
    page.click('button:text-is("← Back")')
    page.wait_for_selector(".ma-group")

    page.select_option("#ma-scope", "atlas")
    page.wait_for_selector('.ma-row-title:text-is("task reflections")')
    open_source(page, "task reflections")
    page.wait_for_selector('.ma-srclines:has-text("project reflection body")')
    assert page.locator(".md").count() == 0
    project_text = page.locator(".ma-srclines").inner_text()
    assert "project_reflection_pointer" not in project_text
    assert "record of execution, not hidden reasoning" in page.locator(
        ".ma-strip .ma-crumb-family").get_attribute("title")
    assert_all_requests_matched(page)


def test_the_legacy_dialogue_summary_disappears_when_it_is_not_catalogued(page):
    assert page.locator(
        '.ma-group:has(.ma-group-title:text-is("Dialogue summary (legacy)"))').count() == 1
    swap_fixtures(page, fixtures_without_legacy_dialogue())
    page.wait_for_function(
        "() => !Array.from(document.querySelectorAll('.ma-group-title'))"
        ".some(n => n.textContent === 'Dialogue summary (legacy)')")
    assert page.locator(
        '.ma-group:has(.ma-group-title:text-is("Dialogue summary (legacy)"))').count() == 0
    assert "Dialogue summary (legacy)" not in row_titles(page)
    assert_all_requests_matched(page)


# --------------------------------------------------------------------------- #
# fix 2 — the dialogue chronicle view
# --------------------------------------------------------------------------- #

def open_dialogue(page):
    open_source(page, "Dialogue chronicle")
    page.wait_for_selector(".ma-block")


def test_dialogue_blocks_render_with_visibly_different_kind_labels(page):
    open_dialogue(page)
    kinds = page.locator(".ma-block .ma-kind").all_text_contents()
    assert kinds == ["Summary block", "Era block (compressed)", "Gap",
                     "Unrecognised block kind"], kinds
    summary = page.locator('.ma-block[data-block-type="summary"]')
    assert "a consolidated summary of a stretch of conversation" in summary.inner_text()
    era = page.locator('.ma-block[data-block-type="era"]')
    assert "Detail was lost when they were compressed" in era.inner_text()
    assert "This block is longer than shown" in era.inner_text()
    gap = page.locator('.ma-block[data-block-type="gap"]')
    assert "never consolidated" in gap.inner_text()
    assert "never covered over by compression" in gap.inner_text()
    assert "Gap id gap-7" in gap.inner_text()
    unknown = page.locator('.ma-block[data-block-type="unknown"]')
    assert "shown as stored" in unknown.inner_text()
    # the four kinds are visually distinct, not just differently worded
    borders = [block.evaluate("n => getComputedStyle(n).borderLeftColor + ' '"
                              "+ getComputedStyle(n).borderLeftStyle")
               for block in page.locator(".ma-block").all()]
    assert len(set(borders)) == 4, borders
    # content goes through the Markdown renderer
    assert summary.locator("h2").inner_text() == "Retention talk"
    assert summary.locator("strong").inner_text() == "summaries"
    assert "messages 100-140" in summary.inner_text()
    assert "41 messages" in summary.inner_text()
    assert_all_requests_matched(page)


def test_dialogue_says_it_is_not_raw_chat(page):
    open_dialogue(page)
    body = page.locator(".ma-scroll").inner_text()
    assert "not the conversation itself" in body
    assert "raw chat log is not shown here" in body
    assert_all_requests_matched(page)


def test_dialogue_meta_explains_where_the_chronicle_stops(page):
    open_dialogue(page)
    meta = page.locator('.ma-legend:has(h3:text-is("Why the chronicle stops where it stops"))')
    assert meta.count() == 1
    body = meta.inner_text()
    assert "Consolidated up to entry 160 of the chat log" in body
    assert "last run 2026-02-04" in body
    assert "has not been summarised yet" in body
    assert "provenance only" in body
    assert "ab" * 8 in body
    assert "ab" * 16 not in body
    assert "[object Object]" not in body
    assert_all_requests_matched(page)


@pytest.mark.parametrize(("signature", "prefix", "full_value"), [
    (DIALOGUE_SIGNATURE_PLAIN, "a1" * 8, DIALOGUE_SIGNATURE_PLAIN),
    (DIALOGUE_SIGNATURE_TOP_LEVEL, "b2" * 8, "b2" * 32),
    (None, "ab" * 8, "ab" * 16),  # The default fixture is the writer's nested form.
    (DIALOGUE_SIGNATURE_OTHER, "c3" * 8, "c3" * 32),
])
def test_dialogue_meta_renders_every_host_signature_shape_as_a_readable_fingerprint(
        page, signature, prefix, full_value):
    if signature is None:
        open_dialogue(page)
    else:
        swap_fixtures(page, fixtures_dialogue_signature(signature))
        open_dialogue(page)
    meta = page.locator('.ma-legend:has(h3:text-is("Why the chronicle stops where it stops"))')
    body = meta.inner_text()
    assert prefix in body
    assert "[object Object]" not in body
    assert "{" not in body
    assert meta.locator('p.ma-meta[title]').last.get_attribute("title") == full_value
    assert_all_requests_matched(page)


def test_dialogue_meta_unavailable_is_stated_not_invented(page):
    open_dialogue(page)
    swap_fixtures(page, fixtures_dialogue_unavailable())
    page.wait_for_selector('.ma-legend:has-text("Consolidation record unavailable")')
    body = page.locator(".ma-scroll").inner_text()
    assert "state file missing" in body
    assert "No consolidation position is claimed" in body
    assert "Consolidated up to entry" not in body
    assert "No consolidated blocks are stored yet" in body
    assert_all_requests_matched(page)


def test_dialogue_pages_further_blocks_on_request(page):
    open_dialogue(page)
    assert page.locator(".ma-block").count() == 4
    assert "A further summary block" not in page.locator(".ma-scroll").inner_text()
    page.click('button:text-is("Load more blocks…")')
    page.wait_for_selector('.ma-block:has-text("A further summary block")')
    assert page.locator(".ma-block").count() == 5
    assert page.locator('button:text-is("Load more blocks…")').count() == 0
    assert "Every stored block is listed." in page.locator(".ma-scroll").inner_text()
    assert_all_requests_matched(page)


def test_dialogue_keeps_a_source_text_view_of_the_raw_json(page):
    open_dialogue(page)
    page.click('button:text-is("Source text")')
    page.wait_for_selector(".ma-srclines")
    raw = page.locator(".ma-srclines").inner_text()
    assert '"block_id": "blk-summary"' in raw
    assert page.locator(".ma-block").count() == 0
    page.click('button:text-is("Chronicle")')
    page.wait_for_selector(".ma-block")
    assert_all_requests_matched(page)


def test_malformed_dialogue_blocks_are_shown_as_stored(page):
    open_dialogue(page)
    swap_fixtures(page, fixtures_dialogue_malformed())
    page.wait_for_function(
        "() => document.querySelectorAll('.ma-block').length === 2")
    blocks = page.locator(".ma-block")
    assert blocks.count() == 2
    assert page.locator('.ma-block .ma-kind:text-is("Unrecognised block kind")').count() == 2
    assert "This block carries no text." in page.locator(".ma-scroll").inner_text()
    assert "dialogue_blocks.json · malformed record ×2" in page.locator(".ma-gaps").inner_text()
    assert page.evaluate("window.__pwned === undefined")
    assert_all_requests_matched(page)


def test_a_very_long_dialogue_block_scrolls_inside_a_bounded_reader(page):
    open_dialogue(page)
    swap_fixtures(page, fixtures_dialogue_long())
    page.wait_for_selector('.ma-block:has-text("Consolidated line about retention.")')
    scroll = page.locator(".ma-scroll")
    client = scroll.evaluate("n => n.clientHeight")
    content = scroll.evaluate("n => n.scrollHeight")
    assert 0 < client <= 722, client
    assert content > client, "a long block must scroll inside the reader, not stretch it"
    root = page.evaluate(
        "document.getElementById('memory-atlas-root').getBoundingClientRect().height")
    assert root <= 722, root
    assert_all_requests_matched(page)


# --------------------------------------------------------------------------- #
# reader and Markdown
# --------------------------------------------------------------------------- #

def test_reader_renders_real_markdown_constructs(page):
    open_source(page, "patterns")
    page.wait_for_selector(".md h1")
    md = page.locator(".md")
    assert md.locator("h1").first.inner_text() == "Retention patterns"
    assert md.locator("strong").first.inner_text() == "retention"
    assert md.locator("em").first.inner_text() == "decay"
    assert md.locator("del").first.inner_text() == "eviction"
    assert md.locator("code").first.inner_text() == "inline code"
    assert md.locator("table thead th").all_text_contents() == [
        "Stratum", "Retention", "Reconstructible"]
    assert md.locator("table tbody tr").count() == 3
    assert md.locator('table tbody tr:nth-child(1) td:nth-child(3)').inner_text() == "digest only"
    assert md.locator("table thead th").nth(1).evaluate(
        "node => node.style.textAlign") == "center"
    assert md.locator("table thead th").nth(2).evaluate(
        "node => node.style.textAlign") == "right"
    boxes = md.locator("li.md-task input[type=checkbox]")
    assert boxes.count() == 2
    assert boxes.nth(0).is_checked() and not boxes.nth(1).is_checked()
    assert boxes.nth(0).is_disabled()
    assert md.locator("ul ul li").count() >= 1
    assert md.locator("ol li").count() == 2
    code_block = md.locator("pre code[data-language=python]")
    assert "def retain(block, ttl):" in code_block.inner_text()
    assert md.locator("blockquote").count() == 1
    assert md.locator("hr").count() == 0  # the rule lives on document page two
    assert_all_requests_matched(page)


def test_markdown_never_becomes_html_or_an_active_link(page):
    open_source(page, "patterns")
    page.wait_for_selector(".md h1")
    assert page.evaluate("window.__pwned === undefined")
    assert page.locator(".md img").count() == 0
    assert page.locator(".md script").count() == 0
    body = page.locator(".md").inner_text()
    assert "<img src=x onerror=" in body
    assert "<script>window.__pwned = true;</script>" in body
    safe = page.locator('.md a[href="https://example.invalid/spec"]')
    assert safe.count() == 1
    assert safe.get_attribute("rel") == "noopener noreferrer nofollow"
    assert page.locator('.md a[href^="javascript:"]').count() == 0
    dangling = page.locator('.md .md-unresolved:text-is("dangling pointer")')
    assert dangling.count() == 1
    assert "Unresolved link" in dangling.get_attribute("title")
    assert_all_requests_matched(page)


def test_the_reader_and_the_links_tab_agree_about_the_same_document(page):
    """One link vocabulary across both tabs.

    Every authored link the backend turns into an edge must also open in the
    reader, and every cross-reference the reader offers must be an edge the
    backend reported. When the two sides resolved links with different
    vocabularies, `[see](patterns.md)` produced an edge but rendered as an
    unresolved link, while `[notes](knowledge:tooling)` navigated in the reader
    and produced no edge at all — which is what made the graph look wrong.
    """
    open_source(page, "patterns")
    page.wait_for_selector(".md h1")
    rendered = sorted(set(page.eval_on_selector_all(
        ".md .md-xref", "nodes => nodes.map(n => n.dataset.sourceId)")))
    unresolved = page.eval_on_selector_all(
        ".md .md-unresolved", "nodes => nodes.map(n => n.textContent)")
    link_kinds = ("markdown_link", "wiki_link")
    expected = sorted({
        edge["target"] for edge in GRAPH_EDGES
        if edge["source"] == "knowledge:patterns" and edge["kind"] in link_kinds})
    # Four written link forms resolve. Image destinations, inline/fenced code,
    # and escaped brackets in the same document are neither reader xrefs nor
    # backend edges.
    assert expected == ["dialogue", "knowledge:tooling", "knowledge:undated",
                        "registry"], expected
    assert rendered == expected, rendered
    page.click('.ma-tab[data-tab="relations"]')
    page.wait_for_selector(".ma-graph")
    reported = sorted(set(page.eval_on_selector_all(
        "li[data-edge-kind]",
        "nodes => nodes.filter(n => n.dataset.edgeKind === 'markdown_link'"
        " || n.dataset.edgeKind === 'wiki_link')"
        ".map(n => n.querySelector('button[data-source-id]').dataset.sourceId)")))
    assert reported == expected, reported
    # Stated separately because it is the whole point of the two inherited
    # names in the fixture: neither tab offers them as a source.
    assert not [t for t in ("constructor", "toString") if t in reported], reported
    assert not [t for t in ("constructor", "toString") if t in rendered], rendered
    # The targets that resolve to nothing are unresolved on both sides: the
    # backend reports no edge for them and the reader offers no button. Two of
    # them, `constructor` and `toString`, are names inherited by every plain
    # JavaScript object; when the catalogue maps were object literals the
    # reader answered those lookups from Object.prototype and drew an active
    # cross-reference to a source the backend had never reported.
    assert sorted(unresolved) == ["dangling pointer", "missing sibling", "note",
                                  "toString"], unresolved
    assert_all_requests_matched(page)


def test_internal_reference_navigates_to_the_catalogued_source(page):
    open_source(page, "patterns")
    page.wait_for_selector(".md h1")
    page.click('.md .md-xref:text-is("tooling notes")')
    page.wait_for_selector('.ma-strip .ma-title:text-is("tooling")')
    page.wait_for_selector('.md h1:text-is("Tooling")')
    assert "Short and complete." in page.locator(".md").inner_text()
    assert_all_requests_matched(page)


def test_load_more_completes_the_document_and_states_completeness(page):
    open_source(page, "patterns")
    page.wait_for_selector(".md h1")
    # How much has been read is stated once, in the header row, not as a banner
    # sitting on top of the text.
    assert page.locator(".ma-scroll .ma-loadstate").count() == 0
    assert "Partial" in page.locator(".ma-strip .ma-loadstate").inner_text()
    assert "Continued after paging" not in page.locator(".md").inner_text()
    page.click('button:text-is("Load more of this document")')
    page.wait_for_selector('.md h2:text-is("Continued after paging")')
    assert "Complete document" in page.locator(".ma-strip .ma-loadstate").inner_text()
    assert page.locator('button:text-is("Load more of this document")').count() == 0
    assert page.locator(".md hr").count() == 1
    assert_all_requests_matched(page)


def test_source_text_view_is_available_and_bounded(page):
    open_source(page, "patterns")
    page.wait_for_selector(".md h1")
    page.click('button:text-is("Source text")')
    page.wait_for_selector(".ma-srclines")
    assert page.locator(".ma-srclines .ma-ln").count() > 10
    overflow = page.locator(".ma-scroll").evaluate(
        "node => getComputedStyle(node).overflowY")
    assert overflow == "auto"
    height = page.locator(".ma-scroll").evaluate("node => node.clientHeight")
    assert 0 < height <= 722
    assert_all_requests_matched(page)


def test_non_markdown_source_opens_as_source_text(page):
    page.select_option("#ma-scope", "atlas")
    page.wait_for_selector('.ma-row-title:text-is("journal")')
    open_source(page, "journal")
    page.wait_for_selector('.ma-srclines:has-text("journal entry one")')
    assert "journal entry two" in page.locator(".ma-srclines").inner_text()
    assert_all_requests_matched(page)


def test_revision_drift_is_reported_honestly(page):
    open_source(page, "drifting")
    page.wait_for_selector(".ma-notice")
    notice = page.locator(".ma-notice").inner_text()
    assert "revision drift" in notice
    assert "changed while it was being read" in notice
    assert page.locator('.ma-notice button:text-is("Reload")').count() == 1
    assert_all_requests_matched(page)


def test_the_reader_shows_a_breadcrumb_and_an_obvious_way_back(page):
    open_source(page, "patterns")
    crumb = page.locator(".ma-crumb")
    assert crumb.count() == 1
    text = crumb.inner_text()
    assert "Global memory" in text and "Knowledge" in text and "patterns" in text
    back = page.locator('.ma-strip button:text-is("← Back")')
    assert back.count() == 1
    assert "Global memory" in back.get_attribute("title")
    back.click()
    page.wait_for_selector('.ma-h1:text-is("Global memory")')
    open_source(page, "patterns")
    page.click('.ma-crumb .ma-linkbtn')
    page.wait_for_selector('.ma-h1:text-is("Global memory")')
    assert_all_requests_matched(page)


def test_the_rail_marks_the_open_document(page):
    open_source(page, "patterns")
    current = page.locator('.ma-rail .ma-navitem[aria-current="true"]')
    assert current.count() == 1
    assert "patterns" in current.inner_text()
    assert "Currently open" in current.get_attribute("title")
    assert_all_requests_matched(page)


# --------------------------------------------------------------------------- #
# history and comparison
# --------------------------------------------------------------------------- #

def test_history_distinguishes_snapshots_digests_and_activity(page):
    open_source(page, "patterns")
    page.click('.ma-tab[data-tab="history"]')
    page.wait_for_selector(".ma-ev")
    assert page.locator(".ma-ev").count() == 3
    chips = page.locator(".ma-ev .ma-chip").all_text_contents()
    assert "Full snapshot" in chips and "Digest only" in chips
    preview_row = page.locator('.ma-ev:has-text("identity write")')
    assert "explicitly truncated" in preview_row.inner_text()
    assert preview_row.locator("input[type=checkbox]").count() == 0
    page.click('button:text-is("Load more history")')
    page.wait_for_selector('.ma-ev:has-text("reindexed")')
    assert page.locator(".ma-ev").count() == 5
    activity_row = page.locator('.ma-ev:has-text("reindexed")')
    assert "Activity event" in activity_row.inner_text()
    assert "No earlier text kept" in activity_row.inner_text()
    assert_all_requests_matched(page)


def test_only_two_full_snapshots_can_be_compared(page):
    open_source(page, "patterns")
    page.click('.ma-tab[data-tab="history"]')
    page.wait_for_selector(".ma-ev")
    compare = page.locator('button:text-is("Compare selected")')
    assert compare.is_disabled()
    assert "Tick two" in compare.get_attribute("title")
    page.check('input[data-event-id="ev-old"]')
    assert compare.is_disabled()
    page.check('input[data-event-id="ev-new"]')
    assert compare.is_enabled()
    compare.click()
    page.wait_for_selector(".ma-diff")
    diff = page.locator(".ma-diff").inner_text()
    assert "- Retention window: 7 days." in diff
    assert "+ Retention window: 30 days." in diff
    assert "+ Added guarantee line." in diff
    assert "  Unchanged tail line." in diff
    assert "Eviction: oldest first." in diff
    assert re.search(r"\d+ lines added, \d+ removed", page.locator(".ma-scroll").inner_text())
    assert_all_requests_matched(page)


def test_compare_is_not_offered_when_no_earlier_text_is_kept(page):
    # `compare_supported: false`, even though two full snapshots are listed.
    open_source(page, "improvement-backlog")
    page.click('.ma-tab[data-tab="history"]')
    page.wait_for_selector(".ma-ev")
    assert page.locator(".ma-ev").count() == 2
    assert page.locator('button:text-is("Compare selected")').count() == 0
    assert page.locator(".ma-ev input[type=checkbox]").count() == 0
    panel = page.locator(".ma-scroll").inner_text()
    assert "Version comparison is not offered for this document" in panel
    assert "written by merging" in panel
    assert "no earlier text to compare against" in panel
    assert_all_requests_matched(page)


def test_catalog_fixtures_match_backend_source_contract(tmp_path):
    """Keep UI source metadata aligned with the backend discovery contract."""
    from memory_reader import MemoryReader

    files = {
        "memory/identity.md": "identity",
        "memory/identity_journal.jsonl": "{}\n",
        "memory/scratchpad.md": "scratchpad",
        "memory/scratchpad_journal.jsonl": "{}\n",
        "memory/dialogue_blocks.json": "[]",
        "memory/dialogue_summary.md": "legacy",
        "memory/WORLD.md": "world",
        "memory/registry.md": "registry",
        "memory/deep_review.md": "review",
        "logs/task_reflections.jsonl": "{}\n",
        "memory/knowledge/patterns.md": "patterns",
        "memory/knowledge/patterns_history.jsonl": "{}\n",
        "memory/knowledge/tooling.md": "tooling",
        "memory/knowledge/undated.md": "undated",
        "memory/knowledge/improvement-backlog.md": "backlog",
        "projects/atlas/knowledge/design.md": "design",
        "projects/atlas/knowledge_history.jsonl": "{}\n",
        "projects/atlas/workpad.md": "workpad",
        "projects/atlas/journal.jsonl": "{}\n",
        "projects/atlas/logs/task_reflections.jsonl": "{}\n",
        "projects/beacon/knowledge/notes.md": "notes",
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    reader = MemoryReader(tmp_path)
    try:
        actual = {item["id"]: item for item in reader.catalog(limit=200)["data"]["items"]}
    finally:
        reader.close()
    expected = {item["id"]: item for item in CATALOG_ITEMS}
    checked = set(actual) & set(expected)
    assert checked
    for source_id in checked:
        for field in ("family", "title", "media_type", "history", "compare_supported"):
            assert expected[source_id][field] == actual[source_id][field], (source_id, field)


def test_snapshot_reads_are_bound_to_the_listed_history_revision(page):
    open_source(page, "patterns")
    page.click('.ma-tab[data-tab="history"]')
    page.wait_for_selector(".ma-ev")
    page.click('button[data-open-event="ev-old"]')
    page.wait_for_selector(".ma-scroll .md h1")
    events = [url for url in page.evaluate("window.__requests")
              if "/history/event" in url]
    assert events and all("revision=" in url for url in events), events
    assert_all_requests_matched(page)


def test_a_snapshot_served_at_another_revision_is_reported_as_drift(page):
    open_source(page, "patterns")
    page.click('.ma-tab[data-tab="history"]')
    page.wait_for_selector(".ma-ev")
    page.click('button:text-is("Load more history")')
    page.wait_for_selector('.ma-ev:has-text("knowledge updated (stale)")')
    page.click('button[data-open-event="ev-stale"]')
    page.wait_for_selector(".ma-notice")
    notice = page.locator(".ma-notice").inner_text()
    assert "revision drift" in notice
    assert "changed while" in notice
    assert "Retention window: 7 days." not in page.locator(".ma-scroll").inner_text()
    assert_all_requests_matched(page)


def test_a_snapshot_opens_as_a_full_reconstructed_version(page):
    open_source(page, "patterns")
    page.click('.ma-tab[data-tab="history"]')
    page.wait_for_selector(".ma-ev")
    page.click('button[data-open-event="ev-old"]')
    page.wait_for_selector(".ma-scroll .md h1")
    panel = page.locator('.ma-scroll section:has(.ma-title)').first
    assert "Complete snapshot" in panel.inner_text()
    assert "Retention window: 7 days." in panel.inner_text()
    assert_all_requests_matched(page)


# --------------------------------------------------------------------------- #
# fix 3 — links, deterministic and without an inference layer
# --------------------------------------------------------------------------- #

def open_links(page, title="patterns"):
    open_source(page, title)
    page.click('.ma-tab[data-tab="relations"]')


def test_the_links_tab_shows_only_contract_kinds_with_their_literal_basis(page):
    open_links(page)
    page.wait_for_selector(".ma-graph")
    rows = page.locator("li[data-edge-kind]")
    assert rows.count() == 6, rows.all_text_contents()
    kinds = [row.get_attribute("data-edge-kind") for row in rows.all()]
    assert kinds == ["markdown_link", "markdown_link", "markdown_link",
                     "wiki_link", "journal_source_ref",
                     "shared_task_id"], kinds
    body = page.locator(".ma-scroll").inner_text()
    for basis in ("Markdown link in this document", "Wiki link in this document",
                  "Journal read reference", "shared task_id abc123"):
        assert basis in body, basis
    # the edge under an unrecognised kind is counted honestly, never drawn
    assert "1 further link(s) were reported under a kind this reader does not recognise" in body
    assert "speculative" not in body.lower()
    assert page.locator(".ma-graph line").count() == 6
    assert_all_requests_matched(page)


def test_shared_task_id_links_are_dotted_and_always_show_their_basis(page):
    open_links(page)
    page.wait_for_selector(".ma-graph")
    dotted = page.locator(".ma-graph .ma-edge-dotted")
    assert dotted.count() == 1
    assert dotted.evaluate("n => getComputedStyle(n).strokeDasharray") not in ("", "none")
    solid = page.locator(".ma-graph line.ma-edge:not(.ma-edge-dotted)")
    assert solid.count() == 5
    assert solid.first.evaluate("n => getComputedStyle(n).strokeDasharray") in ("", "none")
    row = page.locator('li[data-edge-kind="shared_task_id"]')
    assert "shared task_id abc123" in row.inner_text()
    assert "Shared task id" in row.inner_text()
    assert_all_requests_matched(page)


def test_the_links_tab_has_a_legend_in_ordinary_words(page):
    open_links(page)
    page.wait_for_selector(".ma-legend")
    legend = page.locator('.ma-legend:has(h3:text-is("How to read this"))')
    assert legend.count() == 1
    body = legend.inner_text()
    for phrase in ("Markdown link", "Wiki link", "Journal read reference",
                   "Shared task id", "Solid line", "Dotted line"):
        assert phrase in body, phrase
    assert "not a reference" in body
    assert "never opened" in body
    assert_all_requests_matched(page)


def test_no_inference_affordance_exists_anywhere_in_the_ui(page):
    shell = page.locator("#memory-atlas-root").inner_text().lower()
    assert "inferred" not in shell
    assert page.locator('[aria-pressed]:has-text("Inferred")').count() == 0
    open_links(page)
    page.wait_for_selector(".ma-graph")
    reader = page.locator("#memory-atlas-root").inner_text().lower()
    assert "inferred" not in reader
    titles = page.eval_on_selector_all(
        "#memory-atlas-root [title]", "nodes => nodes.map(n => n.title).join(' ')")
    assert "inferred" not in titles.lower()
    graph_requests = [url for url in page.evaluate("window.__requests") if "/graph" in url]
    assert graph_requests and all("inferred" not in url for url in graph_requests)
    assert_all_requests_matched(page)


def test_structural_neighbours_are_shown_separately_from_links(page):
    page.select_option("#ma-scope", "atlas")
    page.wait_for_selector('.ma-row-title:text-is("design")')
    open_links(page, "design")
    page.wait_for_selector(".ma-structure")
    structure = page.locator(".ma-structure")
    assert "Other sources in the same project" in structure.inner_text()
    assert "structure, not links" in structure.inner_text()
    siblings = structure.locator("li")
    assert siblings.count() == 3
    assert "atlas / workpad" in structure.inner_text()
    # they are not mixed into the diagram
    assert page.locator(".ma-graph").count() == 0
    assert page.locator("li[data-edge-kind]").count() == 0
    structure.locator(".ma-linkbtn").first.click()
    page.wait_for_selector(".ma-strip .ma-title")
    assert_all_requests_matched(page)


def test_a_global_source_says_it_belongs_to_no_project(page):
    open_links(page)
    page.wait_for_selector(".ma-structure")
    assert "does not belong to a project" in page.locator(".ma-structure").inner_text()
    assert_all_requests_matched(page)


def test_empty_links_are_stated_not_invented(page):
    open_links(page, "tooling")
    page.wait_for_selector('.ma-empty:has-text("No outgoing links")')
    text = page.locator(".ma-scroll").inner_text()
    assert "no other source links" not in text
    assert page.locator(".ma-graph").count() == 0
    assert_all_requests_matched(page)


def test_empty_links_disclose_an_incomplete_scan(page):
    swap_fixtures(page, fixtures_empty_graph_incomplete())
    open_links(page, "tooling")
    page.wait_for_selector('.ma-empty:has-text("scan was incomplete")')
    text = page.locator(".ma-scroll").inner_text()
    assert "No outgoing links or recorded provenance links" in text
    assert "graph · scan limit" in page.locator(".ma-gaps").inner_text()
    assert_all_requests_matched(page)


def test_the_link_diagram_has_an_equivalent_list(page):
    open_links(page)
    page.wait_for_selector(".ma-graph")
    label = page.locator(".ma-graph").get_attribute("aria-label")
    assert "list below carries the same information" in label
    page.click('li[data-edge-kind="markdown_link"] .ma-linkbtn')
    page.wait_for_selector('.ma-strip .ma-title:text-is("tooling")')
    assert_all_requests_matched(page)


# --------------------------------------------------------------------------- #
# fix 4 — every control explains itself
# --------------------------------------------------------------------------- #

def titles_of(page, selector):
    return page.eval_on_selector_all(
        selector,
        "nodes => nodes.map(n => [n.textContent.trim() || n.id || n.type,"
        " (n.getAttribute('title') || '').trim()])")


def test_every_top_bar_control_carries_a_tooltip(page):
    pairs = titles_of(page, ".ma-top button, .ma-top select, .ma-top input")
    assert pairs, "the top bar has no controls"
    for label, title in pairs:
        assert len(title) > 12, f"top-bar control {label!r} has no usable tooltip"


def test_every_reader_control_carries_a_tooltip(page):
    # The header now holds both segmented controls, so the tooltip contract has
    # to cover them there rather than inside the scrolling panel.
    open_source(page, "patterns")
    page.wait_for_selector(".md h1")
    for selector in (".ma-strip button",
                     ".ma-strip .ma-tabs .ma-seg-btn",
                     ".ma-strip .ma-modebar .ma-seg-btn",
                     # prose cross-references are links inside the text, not controls
                     ".ma-scroll .ma-strip-row button"):
        pairs = titles_of(page, selector)
        assert pairs, f"no controls matched {selector}"
        for label, title in pairs:
            assert len(title) > 12, f"reader control {label!r} has no usable tooltip"
    # Nothing unexplained is left in the header: the category and the
    # completeness statement carry their own explanation too.
    for selector in (".ma-strip .ma-crumb-family", ".ma-strip .ma-loadstate",
                     ".ma-strip .ma-chip"):
        pairs = titles_of(page, selector)
        assert pairs, f"no elements matched {selector}"
        for label, title in pairs:
            assert len(title) > 12, f"header element {label!r} has no usable tooltip"
    page.click('.ma-tab[data-tab="history"]')
    page.wait_for_selector(".ma-ev")
    for label, title in titles_of(page, ".ma-scroll button, .ma-scroll input[type=checkbox]"):
        assert len(title) > 12, f"history control {label!r} has no usable tooltip"


def test_the_reader_header_is_exactly_two_ultra_compact_rows(page):
    open_source(page, "patterns")
    page.wait_for_selector(".md h1")
    rows = page.locator(".ma-strip > .ma-strip-row")
    assert rows.count() == 2, rows.all_text_contents()

    # Row 1: back out, breadcrumb carrying the heading, then the file facts.
    lead = page.locator(".ma-strip .ma-strip-lead")
    assert lead.locator('button:text-is("← Back")').count() == 1
    assert lead.locator("nav.ma-crumb h1.ma-title").count() == 1
    assert "Last modified" in lead.locator(".ma-strip-meta").inner_text()

    # Row 2: the two segmented controls and the completeness statement.
    controls = page.locator(".ma-strip .ma-strip-controls")
    assert controls.locator(".ma-tabs.ma-seg .ma-tab").count() == 3
    assert controls.locator(".ma-modebar.ma-seg .ma-seg-btn").count() == 2
    assert controls.locator(".ma-loadstate").count() == 1

    # Nothing is written twice: one heading, and no repeated category blurb.
    assert page.locator(".ma-strip .ma-title").count() == 1
    assert page.locator(".ma-strip p").count() == 0

    # "Ultra-compact" is a measurement, not an adjective.
    strip = page.locator(".ma-strip").bounding_box()["height"]
    root = page.evaluate(
        "document.getElementById('memory-atlas-root').clientHeight")
    assert strip <= 76, strip
    assert strip / root <= 0.12, (strip, root)
    for row in range(2):
        height = rows.nth(row).bounding_box()["height"]
        assert height <= 30, (row, height)
    assert_all_requests_matched(page)


def reader_height_share(page):
    """clientHeight of the reading surface over the widget, read in one pass.

    Both nodes are looked up inside the same evaluate: a locator resolved
    before a re-render can be measured after it, and a detached node reports
    zero rather than failing loudly.
    """
    return page.evaluate(
        "() => { const s = document.querySelector('.ma-scroll');"
        " const r = document.getElementById('memory-atlas-root');"
        " return [s ? s.clientHeight : 0, r.clientHeight]; }")


@pytest.mark.parametrize("title", ["patterns", "Dialogue chronicle", "unreadable"])
def test_the_markdown_reader_owns_at_least_three_quarters_of_the_height(page, title):
    # The owner's complaint was a header eating 60-65% of the widget. The
    # reading surface has to be the dominant thing on screen instead.
    open_source(page, title)
    # settle: the panel re-renders once the source itself has been read
    page.wait_for_selector(".ma-strip .ma-loadstate, .ma-scroll .ma-empty, .ma-notice")
    scroll, root = reader_height_share(page)
    assert root == 722, root
    assert scroll / root >= 0.75, (title, scroll, root)


def test_the_gaps_footer_stays_a_single_compact_line(page):
    # Gaps are a disclosure strip, not a second panel crowding the document.
    open_source(page, "unreadable")
    page.wait_for_selector('.ma-gaps:has-text("file too large")')
    gaps = page.locator(".ma-gaps")
    height = gaps.bounding_box()["height"]
    assert height <= 30, height
    assert gaps.evaluate("n => n.scrollWidth <= n.clientWidth + 1"), "gaps clip sideways"
    page.wait_for_selector(".ma-notice")
    scroll, root = reader_height_share(page)
    assert scroll / root >= 0.75, (scroll, root)
    assert_all_requests_matched(page)


def test_the_reader_controls_use_the_shared_pill_shape(page):
    # Design code: hairline 1px borders on the near-black surface, fully
    # rounded segmented controls — the same shapes as claudexor_quotas.
    assert page.evaluate(
        "getComputedStyle(document.getElementById('memory-atlas-root'))"
        ".backgroundColor") == "rgb(13, 11, 15)"
    open_source(page, "patterns")
    page.wait_for_selector(".md h1")
    for selector in (".ma-strip .ma-tabs.ma-seg", ".ma-strip .ma-modebar.ma-seg",
                     '.ma-strip button:text-is("← Back")'):
        style = page.locator(selector).evaluate(
            "n => { const s = getComputedStyle(n);"
            " return [s.borderTopLeftRadius, s.borderTopWidth, s.borderTopStyle]; }")
        assert style[0] == "9999px", (selector, style)
        assert style[1] == "1px", (selector, style)
        assert style[2] == "solid", (selector, style)
    selected = page.locator('.ma-tab[aria-selected="true"]')
    assert selected.count() == 1
    assert selected.evaluate(
        "n => getComputedStyle(n).borderTopLeftRadius") == "9999px"
    assert_all_requests_matched(page)


def test_labels_are_plain_english_not_internal_jargon(page):
    shell = page.locator("#memory-atlas-root").inner_text()
    for jargon in ("strata", "lane", "capsule", "sparkline", "digest_preview",
                   "representation", "media_type", "modified_ns", "family"):
        assert jargon not in shell.lower(), jargon
    open_source(page, "patterns")
    tabs = page.locator(".ma-tab").all_text_contents()
    assert tabs == ["Read", "Version history", "Links"], tabs


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #

def test_search_lists_hits_and_opens_the_matching_line(page):
    page.fill("#ma-search", "retention")
    page.press("#ma-search", "Enter")
    page.wait_for_selector(".ma-scroll ul li mark")
    assert "Literal, case-insensitive substring search" in page.locator(".ma-scroll").inner_text()
    assert page.locator(".ma-scroll ul li").count() == 2
    assert page.locator(".ma-scroll ul li mark").first.inner_text() == "retention"
    assert "From another project" in page.locator(".ma-scroll").inner_text()
    page.click('.ma-linkbtn[data-source-id="knowledge:patterns"]')
    page.wait_for_selector(".ma-srclines .ma-hit")
    assert page.locator(".ma-srclines .ma-hit").count() == 1
    assert "retention" in page.locator(".ma-srclines .ma-hit").inner_text()
    assert page.locator(".ma-srclines mark").count() >= 1
    assert_all_requests_matched(page)


def test_empty_search_result_is_explicit(page):
    page.fill("#ma-search", "nothingmatches")
    page.press("#ma-search", "Enter")
    page.wait_for_selector('.ma-empty:has-text("No matches")')
    assert "fully scanned sources" in page.locator(".ma-empty").inner_text()
    assert_all_requests_matched(page)


def test_empty_search_discloses_an_incomplete_scan(page):
    swap_fixtures(page, fixtures_search_incomplete())
    page.fill("#ma-search", "nothingmatches")
    page.press("#ma-search", "Enter")
    page.wait_for_selector('.ma-empty:has-text("scan was incomplete")')
    assert "Known reading limits" in page.locator(".ma-empty").inner_text()
    assert "search · byte scan limit" in page.locator(".ma-gaps").inner_text()
    assert_all_requests_matched(page)


def test_slash_focuses_search_but_not_inside_a_field(page):
    page.click("body")
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "ma-search"
    page.keyboard.type("a/b")
    assert page.input_value("#ma-search") == "a/b"
    assert page.evaluate("document.activeElement.id") == "ma-search"


def test_escape_returns_to_the_first_screen(page):
    open_source(page, "patterns")
    page.wait_for_selector(".md h1")
    page.click(".ma-scroll")
    page.keyboard.press("Escape")
    page.wait_for_selector(".ma-group")
    assert page.locator(".ma-h1").inner_text() == "Global memory"


# --------------------------------------------------------------------------- #
# gaps and lifecycle
# --------------------------------------------------------------------------- #

def test_gaps_are_surfaced_for_the_active_view(page):
    footer = page.locator(".ma-gaps")
    assert footer.is_visible()
    assert "knowledge · unreadable" in footer.inner_text()
    page.fill("#ma-search", "retention")
    page.press("#ma-search", "Enter")
    page.wait_for_selector(".ma-scroll ul li")
    assert "search · byte scan limit" in page.locator(".ma-gaps").inner_text()
    assert_all_requests_matched(page)


def test_dispose_aborts_requests_and_removes_the_widget(page):
    page.evaluate("window.__delayMs = 4000")
    page.click('.ma-row:has(.ma-row-title:text-is("patterns"))')
    page.wait_for_selector('.ma-empty:has-text("Loading document")')
    page.evaluate("window.__disposeAll()")
    assert page.evaluate("window.__abortCount") >= 1
    assert page.evaluate("document.getElementById('memory-atlas-root') === null")
    assert page.evaluate("document.querySelectorAll('style').length") == 0
    page.keyboard.press("/")
    assert page.evaluate("document.body.children.length") == 1


def test_dispose_is_registered_and_idempotent(page):
    assert page.evaluate("window.__disposeHooks.length") == 1
    page.evaluate("window.__memoryAtlasTestHooks.dispose(); "
                  "window.__memoryAtlasTestHooks.dispose();")
    assert page.evaluate("document.getElementById('memory-atlas-root') === null")


# --------------------------------------------------------------------------- #
# stale-response races
# --------------------------------------------------------------------------- #

def slow_first(page, needle: str, ms: int = 500):
    """Delay only the first request whose URL contains `needle`."""
    page.evaluate(
        """([needle, ms]) => {
             let seen = 0;
             window.__delayFor = (url) =>
               url.includes(needle) ? (seen++ === 0 ? ms : 0) : 0;
           }""",
        [needle, ms],
    )


def test_a_stale_document_response_cannot_reopen_or_duplicate_the_reader(page):
    slow_first(page, "/document?id=knowledge%3Apatterns")
    page.click('.ma-row:has(.ma-row-title:text-is("patterns"))')
    page.click('.ma-rail .ma-navitem:has(.ma-navlabel:text-is("tooling"))')
    page.click('.ma-rail .ma-navitem:has(.ma-navlabel:text-is("patterns"))')
    page.wait_for_selector('.md h1:text-is("Retention patterns")')
    page.wait_for_timeout(800)
    assert page.locator('.md h1:text-is("Retention patterns")').count() == 1
    assert page.locator(".ma-strip .ma-title").inner_text() == "patterns"
    assert page.locator(".md").inner_text().count("Retention patterns") == 1
    assert_all_requests_matched(page)


def test_a_stale_search_response_cannot_overwrite_the_current_query(page):
    slow_first(page, "q=retention")
    page.fill("#ma-search", "retention")
    page.press("#ma-search", "Enter")
    page.fill("#ma-search", "nothingmatches")
    page.press("#ma-search", "Enter")
    page.wait_for_selector('.ma-empty:has-text("No matches")')
    page.wait_for_timeout(800)
    assert page.locator('.ma-empty:has-text("No matches")').count() == 1
    assert page.locator(".ma-scroll ul li").count() == 0
    assert "nothingmatches" in page.locator(".ma-scroll .ma-meta").first.inner_text()
    assert_all_requests_matched(page)


def test_a_stale_graph_response_cannot_overwrite_the_current_one(page):
    slow_first(page, "focus=knowledge%3Apatterns")
    open_links(page)
    page.click('.ma-rail .ma-navitem:has(.ma-navlabel:text-is("tooling"))')
    page.click('.ma-tab[data-tab="relations"]')
    page.wait_for_selector(
        '.ma-empty:has-text("No outgoing links or recorded provenance links")')
    page.wait_for_timeout(800)
    assert page.locator("li[data-edge-kind]").count() == 0, \
        "the superseded link set overwrote the current one"
    assert page.locator(".ma-strip .ma-title").inner_text() == "tooling"
    assert_all_requests_matched(page)


def test_a_stale_dialogue_response_cannot_duplicate_blocks(page):
    slow_first(page, "/dialogue")
    open_source(page, "Dialogue chronicle")
    page.click('.ma-rail .ma-navitem:has(.ma-navlabel:text-is("tooling"))')
    page.click('.ma-rail .ma-navitem:has(.ma-navlabel:text-is("Dialogue chronicle"))')
    page.wait_for_selector(".ma-block")
    page.wait_for_timeout(800)
    assert page.locator(".ma-block").count() == 4
    assert_all_requests_matched(page)


def test_a_stale_history_response_cannot_duplicate_entries(page):
    slow_first(page, "/history?id=knowledge%3Apatterns")
    open_source(page, "patterns")
    page.click('.ma-tab[data-tab="history"]')
    page.click('.ma-rail .ma-navitem:has(.ma-navlabel:text-is("tooling"))')
    page.click('.ma-rail .ma-navitem:has(.ma-navlabel:text-is("patterns"))')
    page.click('.ma-tab[data-tab="history"]')
    page.wait_for_selector(".ma-ev")
    page.wait_for_timeout(800)
    assert page.locator(".ma-ev").count() == 3
    assert_all_requests_matched(page)


def test_renders_do_not_retain_listeners_for_detached_nodes(page):
    before = page.evaluate("window.__memoryAtlasTestHooks.retained()")
    for _ in range(4):
        page.click('.ma-row:has(.ma-row-title:text-is("patterns"))')
        page.wait_for_selector(".md h1")
        page.click('.ma-tab[data-tab="history"]')
        page.wait_for_selector(".ma-ev")
        page.click('button:text-is("← Back")')
        page.wait_for_selector(".ma-group")
    after = page.evaluate("window.__memoryAtlasTestHooks.retained()")
    assert after["shell"] == before["shell"], "shell listeners must be registered once"
    assert after["stage"] < 200 and after["rail"] < 200, after
    assert after["timers"] == 0 and after["pending"] == 0, after
    page.click('.ma-row:has(.ma-row-title:text-is("patterns"))')
    page.wait_for_selector(".md h1")
    steady = page.evaluate("window.__memoryAtlasTestHooks.retained()")
    page.click('button:text-is("← Back")')
    page.wait_for_selector(".ma-group")
    page.click('.ma-row:has(.ma-row-title:text-is("patterns"))')
    page.wait_for_selector(".md h1")
    repeat = page.evaluate("window.__memoryAtlasTestHooks.retained()")
    assert repeat["stage"] == steady["stage"], (steady, repeat)
    assert repeat["rail"] == steady["rail"], (steady, repeat)


# --------------------------------------------------------------------------- #
# narrow geometry
# --------------------------------------------------------------------------- #

def test_narrow_layout_stacks_the_rail_above_the_reader_without_clipping(page):
    page.set_viewport_size({"width": 520, "height": 700})
    page.wait_for_selector(".ma-rail details")
    open_source(page, "patterns")
    page.wait_for_selector(".md h1")
    rail = page.locator(".ma-rail").bounding_box()
    stage = page.locator(".ma-stage").bounding_box()
    assert rail["y"] + rail["height"] <= stage["y"] + 1, (rail, stage)
    assert abs(rail["width"] - stage["width"]) <= 1, (rail, stage)
    assert page.locator(".ma-rail .ma-navitem").count() >= 1
    page.click('.ma-rail summary')
    page.wait_for_selector('.ma-rail .ma-navitem[data-source-id]')
    assert page.locator('.ma-rail .ma-navitem[data-source-id]').count() >= 5
    top = page.locator(".ma-top")
    assert top.evaluate("n => n.scrollWidth <= n.clientWidth + 1"), "toolbar clips"
    assert top.bounding_box()["height"] > 44, "the toolbar did not wrap onto a second row"
    for selector in ("#memory-atlas-root", ".ma-body", ".ma-stage", ".ma-gaps"):
        assert page.locator(selector).evaluate(
            "n => n.scrollWidth <= n.clientWidth + 1"), f"{selector} clips horizontally"
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    assert page.locator("#ma-search").bounding_box()["width"] >= 60
    assert_all_requests_matched(page)


def test_wide_layout_returns_to_the_side_rail(page):
    page.set_viewport_size({"width": 520, "height": 700})
    page.wait_for_selector(".ma-rail details")
    page.set_viewport_size({"width": 1100, "height": 722})
    page.wait_for_selector(".ma-rail h2")
    assert page.locator(".ma-rail details").count() == 0
    rail = page.locator(".ma-rail").bounding_box()
    stage = page.locator(".ma-stage").bounding_box()
    assert rail["x"] + rail["width"] <= stage["x"] + 1, (rail, stage)


# --------------------------------------------------------------------------- #
# unreadable catalogue entries
# --------------------------------------------------------------------------- #

def test_a_source_without_a_revision_digest_is_listed_and_disclosed(page):
    row = page.locator('.ma-row:has(.ma-row-title:text-is("unreadable"))')
    assert row.count() == 1, "an unreadable source must stay in the catalogue"
    assert "Content could not be read" in row.inner_text()
    assert "No modification time" in row.inner_text()
    assert "unreadable · file too large" in page.locator(".ma-gaps").inner_text()
    row.click()
    page.wait_for_selector(".ma-strip .ma-title")
    strip = page.locator(".ma-strip").inner_text()
    assert "Content could not be read for the catalogue" in strip
    assert "file too large" in strip
    assert "no revision digest" in strip
    assert "· revision " not in strip
    assert "unknown size" in strip
    page.wait_for_selector(".ma-notice")
    assert "file too large" in page.locator(".ma-notice").inner_text()
    assert_all_requests_matched(page)


def test_no_vh_units_and_no_resize_feedback_loop(page):
    text = WIDGET.read_text(encoding="utf-8")
    assert not re.search(r"\d+(\.\d+)?vh\b", text), "viewport-height units invite resize feedback loops"
    assert "ResizeObserver" not in text
    heights = []
    for _ in range(3):
        heights.append(page.evaluate(
            "document.getElementById('memory-atlas-root').getBoundingClientRect().height"))
        page.wait_for_timeout(60)
    assert len(set(heights)) == 1, f"widget height oscillated: {heights}"
