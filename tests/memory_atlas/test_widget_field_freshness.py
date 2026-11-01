"""A value read from a note's front matter (its whole summary) stays true to its note; a value of a record or a
journal line stays as it was read.

The card keeps what it read per mount, so Back and a place opened again show it without another read. A change the
card learns of in a note (Refresh comparing it by revision, or a range refused because the note changed) reads
again everything it kept of that note: the note's own views and each value read from its front matter. An open
value of a note is compared on Refresh in one read that answers its revision and its note's, and only when Refresh
is pressed. A record and a journal line are written once, so Refresh never compares or reads their values again.

Every note here is written by the knowledge producer on the global shelf under ``field-check/``, one note per test.
This file runs after ``test_widget_browser.py`` and changes nothing that file reads.
"""
from __future__ import annotations

import json
import re
import urllib.parse

from conftest import PREFIX
from test_widget_browser import BUSY, _ask, card  # noqa: F401  (card: the fixture that starts the Memory Atlas card)

LABEL = "Synthetic test text, not anyone's memory."
CHUNK = 131072   # code points one text range reads (app/atlas.js)
WRITE = (
    "from ouroboros.knowledge import read_knowledge_note, resolve_knowledge_address, write_knowledge_note\n"
    "address = resolve_knowledge_address(root, TOPIC, 'global')\n"
    "try:\n    revision = read_knowledge_note(address).revision\nexcept FileNotFoundError:\n    revision = None\n"
    "front = '---\\ntitle: Field check\\n' + ('summary: ' + SUMMARY + '\\n' if SUMMARY else '') + '---\\n'\n"
    "result = write_knowledge_note(address, front + '# Field check\\n\\n' + BODY + '\\n', writer='turn',"
    " **({'expected_revision': revision} if revision else {}))\n"
    "assert result.ok or result.reason == 'unchanged', result\n")


def summary(tag):
    """An authored summary past the shelf's inline limit (2,000 characters): a ref that reads whole by its address."""
    return ("Summary %s of the field check, read whole by its address. " % tag) * 40 + LABEL


def write_note(host, topic, text, body="A short body. " + LABEL):
    """The note ``topic`` on the global shelf, written by the knowledge producer with ``text`` as its summary (None:
    its front matter has no summary)."""
    host.producer("TOPIC, SUMMARY, BODY = %r, %r, %r\n" % (topic, text, body) + WRITE)


def whole(c):
    return c.ev("() => document.querySelector('pre.source').textContent")


def shows(c, text):
    """Waits until the open value reads ``text``, word for word."""
    c.frame.wait_for_function("t => { const p = document.querySelector('pre.source'); return !!p && p.textContent === t; }",
                              arg=text, timeout=60000)


def refresh(c):
    """Refresh, pressed as the owner presses it; its line once it has checked."""
    c.click("Refresh: read what changed")
    c.frame.wait_for_function("() => !document.querySelector('.refresh-note').textContent.startsWith('Checking')", timeout=60000)
    return c.ev("() => document.querySelector('.refresh-note').textContent")


def text_reads(c):
    """Every read of the text route the card asks from now on, as (length, address)."""
    seen = []

    def record(request):
        if PREFIX + "text?" in request.url:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(request.url).query)
            seen.append((q.get("length", [""])[0], json.loads(q["address"][0])))
    c.page.on("request", record)
    return seen


def open_whole_summary(c, topic):
    assert c.open([{"v": "knowledge"}, {"v": "note", "scope": "global", "topic": topic, "label": "Field check"}]) == "Field check"
    c.click("Read the whole summary")
    assert c.settle() == "Summary"


def note_shows_summary(c, tag):
    c.frame.wait_for_function("t => { const k = document.querySelector('.kept'); return !!k && k.textContent.startsWith(t); }",
                              arg="Summary %s of the field check" % tag, timeout=60000)


def test_refresh_reads_an_open_summary_again_when_its_note_changed(card, real_host):
    """Refresh with the whole summary open compares it and its note in one read; changed, it reads the new words, and
    the note is read again too (Back shows its new summary). Nothing is read until Refresh is pressed, and a Refresh
    with nothing changed makes that one read alone."""
    topic = "field-check/direct"
    field = {"kind": "note", "scope": "global", "topic": topic, "field": ["summary"]}
    write_note(real_host, topic, summary("A"))
    c = card()
    open_whole_summary(c, topic)
    assert whole(c) == summary("A")
    reads = text_reads(c)
    write_note(real_host, topic, summary("B"))
    c.page.wait_for_timeout(3000)
    assert reads == []   # no polling: nothing is read while no one presses Refresh
    note = refresh(c)
    assert reads[:1] == [("1", field)], reads   # the one comparing read
    assert re.fullmatch(r"This place changed and was read again · checked \d\d:\d\d\.", note), note   # never "Nothing new"
    shows(c, summary("B"))
    assert reads == [("1", field), (str(CHUNK), field)], reads
    note = refresh(c)
    assert note.startswith("Nothing new · checked ") and reads[2:] == [("1", field)], (note, reads)
    assert whole(c) == summary("B")
    c.shot("field-refreshed")
    c.page.keyboard.press("Escape")
    assert c.settle() == "Field check"
    note_shows_summary(c, "B")
    assert reads[3:4] == [(str(CHUNK), {"kind": "note", "scope": "global", "topic": topic})], reads   # read again, not kept
    c.assert_clean()


def test_reopening_the_whole_summary_after_its_note_was_refreshed_reads_the_new_one(card, real_host):
    """The summary read whole, then Back to its note. knowledge_write changes the summary and Refresh reads the note
    again: Read and Source show the new summary, and Read the whole summary reads the new one, not the one the card
    kept."""
    topic = "field-check/parent"
    write_note(real_host, topic, summary("A"))
    c = card()
    open_whole_summary(c, topic)
    assert whole(c) == summary("A")
    c.page.keyboard.press("Escape")
    assert c.settle() == "Field check"
    write_note(real_host, topic, summary("B"))
    note = refresh(c)
    assert re.fullmatch(r"This place changed and was read again · checked \d\d:\d\d\.", note), note
    note_shows_summary(c, "B")
    c.click("Source", exact=True)
    c.settle()
    source = c.ev("() => document.querySelector('pre.source').textContent")   # as stored: the writer folds long lines
    assert "\nsummary: Summary B of the field check" in source and "Summary A of" not in source, source[:400]
    c.click("Read", exact=True)
    c.settle()
    c.click("Read the whole summary")
    assert c.settle() == "Summary"
    assert whole(c) == summary("B")
    c.assert_clean()


def test_a_refresh_that_cannot_check_the_open_summary_keeps_it_and_says_so(card, real_host):
    """The comparing read fails while the chronicle answers: the line says the open value could not be checked and the
    summary stays as it was read, word for word. The next Refresh that can check reads the new one."""
    topic = "field-check/failed"
    write_note(real_host, topic, summary("A"))
    c = card()
    open_whole_summary(c, topic)
    write_note(real_host, topic, summary("B"))

    def busy_check(route):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(route.request.url).query)
        if q.get("length") == ["1"]:   # the comparing read of the open value, nothing else
            route.fulfill(status=503, content_type="application/json", body=BUSY)
            return
        route.continue_()

    c.expected.add(503)
    c.page.route("**/atlas/v2/text?*", busy_check)
    note = refresh(c)
    assert note.startswith("Nothing new · checked ") and note.endswith("Couldn’t check the open note or file: The note store is busy."), note
    assert c.title() == "Summary" and whole(c) == summary("A")   # what was open stays, as it was read
    c.shot("field-check-failed")
    c.page.unroute("**/atlas/v2/text?*")
    note = refresh(c)
    shows(c, summary("B"))
    assert "Couldn’t" not in note, note
    c.assert_clean()


def test_a_summary_taken_out_of_its_note_is_not_shown_after_refresh(card, real_host):
    """The owner edits the note's file by hand and takes its summary out while it is open (knowledge_write keeps a
    summary an overwrite leaves out): Refresh finds the value gone and says so instead of showing what the card kept,
    and Back shows the note as it is now, without one."""
    topic = "field-check/removed"
    write_note(real_host, topic, summary("A"))
    c = card()
    open_whole_summary(c, topic)
    real_host.producer("path = root / 'memory' / 'knowledge' / %r\n" % (topic + ".md")
                       + "path.write_text('---\\ntitle: Field check\\ntype: note\\n---\\n# Field check\\n\\nA short body. '"
                       " + %r + '\\n', encoding='utf-8')\n" % LABEL)
    c.expected.add(404)
    refresh(c)
    c.wait_text("Not found in memory.")
    assert summary("A")[:60] not in c.text()
    c.page.keyboard.press("Escape")
    assert c.settle() == "Field check"
    assert "No summary. Nothing is generated in its place." in c.text()
    c.assert_clean()


def test_reading_the_current_version_of_a_changed_note_reads_its_summary_again(card, real_host):
    """A long note's next range is refused once knowledge_write changed it (revision_mismatch); Read the current version
    reads the note and every value the card kept of it again, so its whole summary is the new one."""
    topic = "field-check/read-on"
    long = "Long body line of the field check. " * 4000 + LABEL   # past one range of 131,072 code points
    write_note(real_host, topic, summary("A"), body=long)
    c = card()
    open_whole_summary(c, topic)
    assert whole(c) == summary("A")
    c.page.keyboard.press("Escape")
    assert c.settle() == "Field check"
    write_note(real_host, topic, summary("B"), body=long + " Changed.")
    c.expected.add(409)   # the refused range is the card's evidence that the note changed
    c.frame.locator('[data-key="read-on"]').click()
    c.wait_text("This changed while you were reading.")
    c.click("Read the current version")
    note_shows_summary(c, "B")
    c.click("Read the whole summary")
    assert c.settle() == "Summary"
    assert whole(c) == summary("B")
    c.assert_clean()


def test_values_of_records_and_journal_lines_stay_as_read(card, real_host):
    """A record and a journal line are written once. Each value open here (a stored quote, as JSON; a journal line as
    stored; a note's first version, as a value and in its own view) stays as it was read after its note changes:
    Refresh compares none of them and reads none of them again."""
    topic = "field-check/pinned"
    write_note(real_host, topic, summary("A"), body="The first version's body. " + LABEL)
    write_note(real_host, topic, summary("B"), body="The second version's body. " + LABEL)
    status, history = _ask(real_host, "list", area="history", address={"kind": "note", "scope": "global", "topic": topic}, limit=20)
    assert status == 200 and len(history["entries"]) >= 2, history
    first = history["entries"][0]
    line, version = first["address"], dict(first["address"], field=["new_content"])
    status, as_stored = _ask(real_host, "text", address=line, length=CHUNK)
    assert status == 200 and as_stored["representation"] == "line", as_stored
    status, first_version = _ask(real_host, "text", address=version, length=CHUNK)
    assert status == 200 and "The first version's body." in first_version["text"], first_version
    quote = {"kind": "record", "id": real_host.ids["weekly"], "field": ["quotes", 0]}
    places = [
        ({"v": "field", "address": quote, "label": "Stored quote"}, lambda c: json.loads(whole(c)) == real_host.ids["quotes"][0]),
        ({"v": "field", "address": line, "label": "Journal line"}, lambda c: whole(c) == as_stored["text"]),
        ({"v": "field", "address": version, "label": "Version as stored"}, lambda c: whole(c) == first_version["text"]),
        ({"v": "version", "address": version, "label": "Field check", "ts": first["fields"]["ts"]},
         lambda c: "The first version's body." in c.ev("() => document.querySelector('.prose').textContent")),
    ]
    c = card()
    for n, (route, same) in enumerate(places):
        c.open([{"v": "knowledge"}, route])
        assert same(c), route
        reads = text_reads(c)
        write_note(real_host, topic, summary("C%d" % n), body="Version C%d's body. " % n + LABEL)
        assert refresh(c).startswith("Nothing new · checked "), route
        assert reads == [] and same(c), (route, reads)
    c.assert_clean()
