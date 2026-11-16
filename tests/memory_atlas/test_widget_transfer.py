"""Memory Atlas on transferred records, through the real host: who wrote the words and on what basis, when they were
written, when they were brought here and what period they tell of — three different facts — with the chapter itself
still the primary content; and that every ordinary record reads as it always did.

The transfers are written by this module's ``transfers`` fixture (``conftest.ensure_transfers``, from
``dev/transfer_fixture.py``) with the core's own producers, in the importer's documented order: the legacy import, the
exact bytes of every source retained, then the records and a part over legacy records with block and stated period bases.
The basic ISO record alone is restored as historical journal data (see the fixture), not newly published. They are
synthetic, nobody's memory. The fixture needs a core with the transfer contract (``metadata.transfer`` and the author
kind ``source_author``); a core without it fails these tests with its own refusal. Nothing is skipped.

This file sorts last on purpose: the records it adds join the host's memory, so every other file runs before they exist.

Each browser case runs in both engines in the two windows the owner uses: 1360x747 in the light theme and 390x844 in
the dark one. The reader cases ask the skill's own routes and need no browser.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
import unicodedata

import pytest

from conftest import DEV, PREFIX
from test_widget_browser import FACT, card  # noqa: F401  (the card fixture is this suite's own)

MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
VIEWS = [pytest.param({"width": 1360, "height": 747}, "light", id="wide-light"),
         pytest.param({"width": 390, "height": 844}, "dark", id="phone-dark")]
ENGINES = pytest.mark.parametrize("engine", ["webkit", "chromium"])
WINDOWS = pytest.mark.parametrize("viewport,theme", VIEWS)

BYLINES = "() => Array.from(document.querySelectorAll('.doc > .byline')).map((p) => p.innerText)"
PROSE = "() => document.querySelector('.doc .prose').innerText"
TREE = "() => document.querySelector('.doc .tree').innerText"
META = "k => { const e = document.querySelector('[data-key=\"' + k + '\"] .top .meta'); return e ? e.textContent : null; }"
ROW_FACTS = "k => { const e = document.querySelector('[data-key=\"' + k + '\"] .facts'); return e ? e.textContent : null; }"
OVERFLOWING = ("() => Array.from(document.querySelectorAll('.doc, .row, .row .facts, .row .facts > *, .byline'))"
               ".filter((e) => e.scrollWidth > e.clientWidth + 1).map((e) => e.className + ' ' + e.scrollWidth + '>' + e.clientWidth)")
PAGE_OVERFLOW = "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
INJECTED = "() => document.querySelectorAll('.atlas img, .atlas script, .atlas [onerror]').length"

NEW_FIELDS = ("byline", "transfer", "written_at", "stated_period", "period_bases")
# What an old installation's helper signed with (the importer of the earlier dialogue file wrote this shape), carried by a
# transfer: the core's exact signature, and the human words the Atlas builds from the same structured facts.
OLD_WRITER_BYLINE = 'legacy_helper (recorded writer "old_consolidator"; attribution "retelling by Light, not lived")'
OLD_WRITER_WORDS = "Retold by an earlier helper · recorded writer old_consolidator · retelling by Light, not lived"


def display_metadata(text):
    return "".join("\\u{" + format(ord(c), "X") + "}" if unicodedata.category(c) in {"Cf", "Cc", "Zl", "Zp"} else c for c in text)


def when(iso):
    """A stored time as the widget words it: ``16 Nov 2026, 10:20``."""
    return f"{int(iso[8:10])} {MON[int(iso[5:7]) - 1]} {iso[:4]}, {iso[11:16]}"


def addr(ident):
    return {"kind": "record", "id": ident}


def get(host, op, **query):
    encoded = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else ",".join(v) if isinstance(v, list) else str(v)
               for k, v in query.items()}
    with urllib.request.urlopen(host.url + PREFIX + op + ("?" + urllib.parse.urlencode(encoded) if encoded else ""), timeout=180) as r:
        return json.loads(r.read())


def doc(host, ident, length=1):
    return get(host, "text", address=addr(ident), length=length)


def route(ident, room, kind="legacy", label="Earlier dialogue"):
    return {"v": "record", "id": ident, "label": label, "hint": {"kind": kind, "room": room}}


def open_record(c, ident, room, kind="legacy", label="Earlier dialogue"):
    return c.open([{"v": "room", "room": room}, route(ident, room, kind, label)])


def an_old_helper_record(host):
    """A historical retelling of the activation import: the old helper kind and no transfer."""
    entries = get(host, "list", area="records", room_id="1", kinds=["legacy"], author_kind="legacy_helper", limit=200)["entries"]
    return next(e for e in entries if e["id"].startswith("legacy-") and "transfer" not in e)


# ---- the reader: the facts exactly as the core states them ------------------------------------------------------------

def test_a_transfer_carries_author_basis_and_three_different_dates_as_the_core_states_them(real_host, transfers):
    ids = real_host.ids
    answer = doc(real_host, ids["tr-source"])
    r = answer["record"]
    assert r["author"] == {"kind": "source_author", "label": "Synthetic Writer", "model": "synthetic-model-a", "basis": "source"}
    assert r["transfer"] == {"operator": "synthetic-operator", "tool": "synthetic-importer 1.0", "task_id": ids["tr-source-task"]}
    assert isinstance(r["byline"], str) and "Synthetic Writer" in r["byline"]
    assert r["written_at"] == "2026-03-03"   # when the source says the words were written
    stated = r["stated_period"]   # the period the words tell of
    assert (stated["start"][:10], stated["end"][:10], stated["incomplete"]) == ("2025-11-10", "2025-11-14", False)
    assert answer["period_basis"] == "stated" and answer["source_period"]["start"][:10] == "2025-11-10"
    assert "(stated by its source)" in answer["period"]
    # ts is the local publication time: a third, different fact, never the writing date or the period
    assert r["ts"][:10] not in ("2026-03-03", "2025-11-10", "2025-11-14") and r["ts"] > "2026-03-03"

    importer = doc(real_host, ids["tr-importer"])
    assert importer["record"]["author"] == {"kind": "source_author", "label": "Importer Stated Writer", "model": None, "basis": "importer"}
    assert importer["record"]["written_at"] == "2026-03-04T09:30:00+00:00" and "stated_period" not in importer["record"]
    # no period stated: the core says so, and never dates the record by its local publication
    assert importer["period"] == "period not stated by its source" and importer["source_period"] is None
    assert importer["period_basis"] == "stated" and importer["record"]["ts"][:10] not in importer["period"]

    unknown_author = doc(real_host, ids["tr-unknown-author"])["record"]
    assert unknown_author["author"] == {"kind": "source_author", "label": None, "model": "synthetic-model-b", "basis": "importer"}
    assert unknown_author.get("written_at") is None and unknown_author["stated_period"]["incomplete"] is True

    nothing = doc(real_host, ids["tr-all-unknown"])
    assert nothing["record"]["author"] == {"kind": "source_author", "label": None, "model": None, "basis": "unknown"}
    assert nothing["record"]["transfer"] == {"operator": None, "tool": None, "task_id": None}
    assert nothing["record"].get("written_at") is None and "stated_period" not in nothing["record"]
    assert nothing["period"] == "period not stated by its source" and nothing["source_period"] is None

    moved = doc(real_host, ids["tr-moved-helper"])["record"]   # the old helper kind, carrying a transfer
    assert moved["author"]["kind"] == "legacy_helper" and moved["transfer"]["operator"] == "synthetic-operator"
    assert moved["author"] == {"kind": "legacy_helper", "writer": "old_consolidator", "attribution": "retelling by Light, not lived"}
    assert moved["written_at"] == "2026-01-15" and moved["byline"] == OLD_WRITER_BYLINE   # the core's exact signature


def test_a_transfer_keeps_its_words_and_the_exact_bytes_of_its_retained_source(real_host, transfers):
    ids = real_host.ids
    names = ("tr-source", "tr-importer", "tr-unknown-author", "tr-all-unknown", "tr-moved-helper", "tr-special", "tr-mix-rows",
             "tr-mix-stated", "tr-period-budget", "tr-period-basic", "tr-period-offset")
    for name in names:
        answer = doc(real_host, ids[name], length=5000)
        stored = answer["stored"]
        assert answer["text"] == stored["text"], name   # the words, with nothing appended, cut or normalised
        refs = stored["source_refs"]
        assert isinstance(refs, list) and refs, name   # a transfer cites the source it came from
        words = stored["text"].encode("utf-8")
        expected = ids["tr-sources"][name]
        assert refs[0]["sha256"] == expected["sha256"] == hashlib.sha256(words).hexdigest(), name
        assert refs[0]["size"] == expected["size"] == len(words), name
    assert doc(real_host, ids["tr-special"], length=5000)["text"] == ids["tr-special-text"]   # markup, tabs, no final newline
    # the retained bytes themselves, read back by the core's own source reader, are the words
    out = real_host.producer(
        "import hashlib\n"
        "from ouroboros.artifacts import read_actor_source_bytes\n"
        "from ouroboros.chronicle_store import ChronicleStore\n"
        "store = ChronicleStore(root)\n"
        # Repeat the real fixture, including historical replay; neither journal bytes nor the saved record may change.
        "before = hashlib.sha256(store.log_path.read_bytes()).hexdigest()\n"
        "historical = store.get('tr-period-basic')\n"
        "sys.path.insert(0, %r)\nimport transfer_fixture\ntransfer_fixture.write(root)\n"
        "assert hashlib.sha256(store.log_path.read_bytes()).hexdigest() == before\n"
        "assert store.get('tr-period-basic') == historical\n"
        "for name in %r:\n"
        "    record = store.get(name)\n"
        "    ref = record['source_refs'][0]\n"
        "    data = read_actor_source_bytes(root, ref['task_id'], ref)\n"
        "    print(json.dumps([name, hashlib.sha256(data).hexdigest(), data == record['text'].encode('utf-8')]))\n" % (str(DEV), list(names)))
    read = {row[0]: row[1:] for row in (json.loads(line) for line in out.strip().splitlines() if line.startswith("["))}
    assert set(read) == set(names)
    for name in names:
        assert read[name] == [ids["tr-sources"][name]["sha256"], True], name


def test_the_mixed_part_reports_the_bases_of_its_members(real_host, transfers):
    """The part is the core's own: a part over an old block record, two transfers with stated periods. Nothing here is constructed at the route boundary."""
    ids = real_host.ids
    members = ids["tr-mix-bases"]   # the base each member is meant to have
    for name, basis in members.items():
        member = doc(real_host, ids[name])
        assert member["period_basis"] == basis, (name, member["period_basis"], member["period"])
        assert member["source_period"], name
    rows = doc(real_host, ids["tr-mix-rows"])   # historical fixture id, now unknown local range
    assert rows["source_period"]["start"][:10] == "2025-12-01"   # source-stated, no local binding
    assert rows["record"]["stated_period"]["start"][:10] == "2025-12-01"
    part = doc(real_host, ids["tr-mix-part"])
    assert part["period_basis"] == "mixed" and set(part["period_bases"]) == {"block", "stated"}, part
    assert part["source_period"]["incomplete"] is False
    assert part["source_period"]["start"][:10] == "2025-11-10" and part["source_period"]["end"][:10] == "2026-04-30"
    assert "(partly block period, partly stated by its source)" in part["period"], part["period"]
    listed = get(real_host, "list", area="relations", address=addr(ids["tr-mix-part"]), relation="members")["entries"]
    assert [e["member"]["id"] for e in listed] == [ids[name] for name in ids["tr-mix-members"]]
    assert {e["member"]["id"]: e["member"]["period_basis"] for e in listed} == {ids[n]: b for n, b in members.items()}
    on_list = next(e for e in get(real_host, "list", area="records", room_id="1", kinds=["part"], limit=200)["entries"]
                   if e["id"] == ids["tr-mix-part"])
    assert (on_list["period_basis"], set(on_list["period_bases"])) == ("mixed", {"block", "stated"})


def test_the_author_filter_lists_the_source_authors_and_leaves_the_old_kinds_apart(real_host, transfers):
    ids = real_host.ids
    found = get(real_host, "list", area="records", author_kind="source_author", limit=500)
    assert found["complete"] and all(e["author"]["kind"] == "source_author" and e["byline"] for e in found["entries"])
    assert {e["id"] for e in found["entries"]} == {ids[n] for n in (
        "tr-mix-rows", "tr-mix-stated", "tr-source", "tr-importer", "tr-unknown-author", "tr-all-unknown", "tr-special",
        "tr-period-basic", "tr-period-offset")}
    helpers = get(real_host, "list", area="records", author_kind="legacy_helper", limit=500)["entries"]
    with_transfer = {e["id"] for e in helpers if "transfer" in e}
    assert with_transfer == {ids["tr-moved-helper"], ids["tr-period-budget"]}
    assert {ids["old-block-synth"], ids["tr-moved-helper"]} <= {e["id"] for e in helpers}
    assert an_old_helper_record(real_host)["author"]["kind"] == "legacy_helper"
    with pytest.raises(urllib.error.HTTPError) as refused:
        get(real_host, "list", area="records", author_kind="importer")
    assert refused.value.code == 400


def test_ordinary_records_carry_no_transfer_fields(real_host, transfers):
    ids = real_host.ids
    for kind in ("page", "part", "note", "legacy"):
        for entry in get(real_host, "list", area="records", room_id="1", kinds=[kind], limit=200)["entries"]:
            if entry["id"] in (ids["tr-source"], ids["tr-importer"], ids["tr-unknown-author"], ids["tr-moved-helper"],
                               ids["tr-special"], ids["tr-mix-rows"], ids["tr-mix-stated"], ids["tr-period-budget"],
                               ids["tr-period-basic"], ids["tr-period-offset"]):
                continue
            for name in ("transfer", "written_at", "stated_period"):
                assert name not in entry, (kind, entry["id"], name)
            assert isinstance(entry.get("byline"), str), (kind, entry["id"])   # a signature for every record
    assert "transfer" not in doc(real_host, ids["p1"])["record"] and "transfer" not in doc(real_host, ids["draft"])["record"]


# ---- the widget -----------------------------------------------------------------------------------------------------

@ENGINES
@WINDOWS
def test_every_transfer_state_reads_author_basis_and_three_dates_apart(card, real_host, transfers, engine, viewport, theme):
    ids = real_host.ids
    c = card(engine=engine, viewport=viewport, theme=theme)
    cases = [   # key, room, the first line, what the second line begins with
        ("tr-source", "1", "Written by Synthetic Writer · model synthetic-model-a · signed in the source", "Written 3 Mar 2026 (as its source states)"),
        ("tr-importer", "1", "Written by Importer Stated Writer · model not stated · stated by the importer", "Written 4 Mar 2026, 09:30 (as its source states)"),
        ("tr-unknown-author", "1", "Author not stated · model synthetic-model-b · stated by the importer", "Written date not stated (as its source states)"),
        ("tr-all-unknown", "legacy", "Author not stated · model not stated · attribution not stated", "Written date not stated (as its source states)"),
    ]
    for key, room, first, written in cases:
        open_record(c, ids[key], room)
        answer = doc(real_host, ids[key])
        lines = c.ev(BYLINES)
        assert lines == [first, written + " · transferred here " + when(answer["record"]["ts"])], (key, lines)
        t = answer["record"]["transfer"]
        assert c.ev(FACT, "Transferred") == (when(answer["record"]["ts"]) + ", by " + (t["operator"] or "operator not recorded") + " with "
                                             + (t["tool"] or "tool not recorded") + ", task " + (t["task_id"] or "not recorded")), key
        assert c.ev(FACT, "Signed as") == answer["record"]["byline"] and c.ev(FACT, "Period") == answer["period"]
        assert c.ev(OVERFLOWING) == [] and c.ev(PAGE_OVERFLOW) <= 0, key
    # a period the source states is the period of the events: the written date and the local date are two other facts
    open_record(c, ids["tr-source"], "1")
    answer = doc(real_host, ids["tr-source"])
    title, lines = c.title(), c.ev(BYLINES)
    assert title == "Main, 10–14 November 2025 (stated by its source)"
    assert "3 Mar 2026" in lines[1] and when(answer["record"]["ts"]) in lines[1] and "10–14" not in lines[1]
    assert c.ev(FACT, "Period") == answer["period"] and answer["period"][:10] == "2025-11-10"
    assert len({"3 Mar 2026", answer["record"]["ts"][:10], "2025-11-10"}) == 3
    # the chapter is what is read first and is the words as stored
    assert c.ev(PROSE).strip() == doc(real_host, ids["tr-source"], length=5000)["text"]
    c.shot("transfer-source")
    c.assert_clean()


@ENGINES
@WINDOWS
def test_a_transferred_helper_is_not_labelled_as_the_earlier_dialogue_file(card, real_host, transfers, engine, viewport, theme):
    ids = real_host.ids
    c = card(engine=engine, viewport=viewport, theme=theme)
    open_record(c, ids["tr-moved-helper"], "1")
    answer = doc(real_host, ids["tr-moved-helper"])
    lines = c.ev(BYLINES)
    assert answer["record"]["byline"] == OLD_WRITER_BYLINE
    assert lines == [OLD_WRITER_WORDS, "Written 15 Jan 2026 (as its source states) · transferred here " + when(answer["record"]["ts"])]
    assert "earlier dialogue file" not in " ".join(lines).lower() and "legacy_helper" not in lines[0]
    assert c.ev(FACT, "Signed as") == OLD_WRITER_BYLINE   # exact, and visible
    assert c.ev(FACT, "Transferred").endswith(", by synthetic-operator with synthetic-importer 1.0, task t-synthetic-import")
    # the historical helpers (no transfer) keep their words and their one line: the activation import's own record, and a
    # retelling shaped like it whose block period is recorded
    old = an_old_helper_record(real_host)
    for ident in (old["id"], ids["old-block-synth"]):
        c.open([{"v": "room", "room": "1"}, route(ident, "1")])
        lines = c.ev(BYLINES)
        assert lines == ["Imported from the earlier dialogue file · " + when(doc(real_host, ident)["record"]["ts"])], lines
        assert c.ev(FACT, "Transferred") is None and c.ev(FACT, "Signed as") is None
    c.assert_clean()


@ENGINES
@WINDOWS
def test_names_operator_and_tool_are_text_long_names_wrap_and_the_words_are_kept(card, real_host, transfers, engine, viewport, theme):
    ids = real_host.ids
    c = card(engine=engine, viewport=viewport, theme=theme)
    dialogs, field_requests = [], []
    c.page.on("request", lambda request: field_requests.append(request.url) if "/atlas/v2/text?" in request.url and '"field"' in urllib.parse.unquote(request.url) else None)
    c.page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    open_record(c, ids["tr-special"], "1")
    lines = c.ev(BYLINES)
    label, model = ids["tr-special-author"]["label"], ids["tr-special-author"]["model"]
    assert display_metadata(label[:280]) + "… (excerpt)" in lines[0]
    assert "model not stated" not in lines[0] and "Author not stated" not in lines[0]
    assert "\\u{202E}" in lines[0] and not any(unicodedata.category(c) == "Cf" for c in lines[0])
    assert all(escape in lines[0] for escape in ("\\u{A}", "\\u{9}", "\\u{85}", "\\u{2028}", "\\u{2029}"))
    assert not any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in lines[0])
    assert "signed in the source" in lines[0] and "(excerpt)" in c.ev(FACT, "Signed as")
    special = ids["tr-special-transfer"]
    transfer = c.ev(FACT, "Transferred")
    assert all(word not in transfer for word in ("operator not recorded", "tool not recorded", "task not recorded"))
    assert transfer.count("(excerpt)") == 3
    assert field_requests == []   # no eager full-metadata fetch
    # Each bounded value opens through its own core-provided address; the field reader returns the original bytes.
    for control, expected in [("Read full author name", label), ("Read full model", model),
                              ("Read full operator", special["operator"]), ("Read full tool", special["tool"]),
                              ("Read full task", special["task_id"])]:
        c.click(control, exact=True)
        c.settle()
        assert c.ev("() => document.querySelector('[aria-label=\"Exact stored text\"]').textContent") == expected
        open_record(c, ids["tr-special"], "1")
    c.click("Read signature source", exact=True)
    c.settle()
    source = c.ev("() => document.querySelector('[aria-label=\"Exact stored text\"]').textContent")
    assert json.loads(source)["label"] == label and json.loads(source)["model"] == model
    open_record(c, ids["tr-special"], "1")
    assert c.ev(INJECTED) == 0 and c.ev("() => window.__atlasXss") is None and dialogs == []   # nothing ran, nothing was built
    assert c.ev(OVERFLOWING) == [] and c.ev(PAGE_OVERFLOW) <= 0   # a name of hundreds of unbroken characters wraps
    assert "<b>tag</b>" in c.ev(PROSE) and c.ev("() => document.querySelectorAll('.doc .prose b').length") == 0
    # the stored record view holds the words exactly (escaped as the tree prints every value)
    c.click("Stored record")
    c.settle()
    assert json.dumps(ids["tr-special-text"], ensure_ascii=False) in c.ev(TREE)
    assert c.ev(INJECTED) == 0 and c.ev("() => window.__atlasXss") is None and dialogs == []
    field_requests.clear()
    # the same names in the room's list
    c.open([{"v": "room", "room": "1"}])
    assert display_metadata(label[:280]) + "… (excerpt)" in c.ev(ROW_FACTS, "record:" + ids["tr-special"]) and c.ev(INJECTED) == 0 and c.ev(OVERFLOWING) == []
    assert c.ev("() => window.__atlasXss") is None and dialogs == []
    c.open([{"v": "story"}])
    story_fact = c.ev(ROW_FACTS, "record:" + ids["tr-special"])
    assert story_fact and "(excerpt)" in story_fact and "Author not stated" not in story_fact
    assert field_requests == []
    c.shot("transfer-special")
    c.assert_clean()


@ENGINES
@WINDOWS
def test_rows_name_the_transfer_and_never_date_it_by_its_local_publication(card, real_host, transfers, engine, viewport, theme):
    ids = real_host.ids
    c = card(engine=engine, viewport=viewport, theme=theme)
    c.open([{"v": "room", "room": "1"}])
    source = c.ev(META, "record:" + ids["tr-source"])
    assert source == "Earlier dialogue · 10–14 Nov 2025 (stated by its source)", source
    assert c.ev(META, "record:" + ids["tr-importer"]) == "Earlier dialogue · period not stated by its source"
    assert c.ev(META, "record:" + ids["tr-unknown-author"]).endswith("(incomplete, stated by its source)")
    assert "Transferred · Written by Synthetic Writer" in c.ev(ROW_FACTS, "record:" + ids["tr-source"])
    assert c.ev(ROW_FACTS, "record:" + ids["tr-importer"]).startswith("Transferred · Written by Importer Stated Writer")
    c.open([{"v": "room", "room": "legacy"}])
    assert c.ev(META, "record:" + ids["tr-all-unknown"]) == "Earlier dialogue · period not stated by its source"
    assert "Transferred · Author not stated · model not stated · attribution not stated" in c.ev(ROW_FACTS, "record:" + ids["tr-all-unknown"])
    # The ordinary core entry budget (unchanged) projects individual period fields. Use its own complete period.
    story = get(real_host, "list", area="story", limit=500)
    assert story["complete"]
    entries = {entry["id"]: entry for entry in story["entries"]}
    projected = entries[ids["tr-period-budget"]]["source_period"]
    assert projected["start"]["ref"] and projected["incomplete"]["ref"]
    assert projected["end"] == "2026-07-31"
    c.open([{"v": "story"}])
    for key in ("tr-period-budget", "tr-period-basic", "tr-period-offset"):
        period = entries[ids[key]]["period"]
        assert isinstance(period, str)
        if key == "tr-period-basic":
            # 3.10's core retains basic dates verbatim; 3.11+ formats them. Both dates must survive, on both runtimes.
            assert period in ("20260701 → 20260731 (stated by its source)",
                              "2026-07-01 00:00 → 2026-07-31 00:00 (stated by its source)")
        elif key == "tr-period-offset":
            assert period == "2026-07-01 21:30 → 2026-08-01 02:30 (stated by its source)"
        else:
            assert "2026-07-01" in period and "2026-07-31" in period
        assert "incomplete" not in period and "unknown" not in period
        # My story spans rooms, so its rows name the room as well as the source-stated period.
        assert c.ev(META, "record:" + ids[key]) == "Earlier dialogue · Main · " + period
    # Basic ISO must retain both dates in the document heading too, without a second ISO parser.
    open_record(c, ids["tr-period-basic"], "1")
    basic = doc(real_host, ids["tr-period-basic"])
    assert basic["source_period"]["start"] == "20260701"
    assert c.title() == "Main, " + basic["period"]
    assert c.ev(FACT, "Period") == basic["period"]
    c.shot("transfer-period-basic")
    open_record(c, ids["tr-period-offset"], "1")
    offset = doc(real_host, ids["tr-period-offset"])
    assert offset["source_period"]["start"] == "2026-07-02T00:30:00+03:00"
    assert offset["source_period"]["end"] == "2026-07-31T23:30:00-03:00"
    assert c.title() == "Main, " + offset["period"]
    assert c.ev(FACT, "Period") == offset["period"]
    c.shot("transfer-period-offset")
    # The projected values keep the core's field addresses; the existing field reader returns the real date and false.
    for field, expected in (("start", "2026-07-01"), ("incomplete", False)):
        address = projected[field]["address"]
        assert address == {"kind": "record", "id": ids["tr-period-budget"], "field": ["metadata", "stated_period", field]}
        answer = get(real_host, "text", address=address)
        assert (json.loads(answer["text"]) if answer["representation"] == "json" else answer["text"]) == expected
        c.open([{"v": "story"}, {"v": "field", "address": address, "label": "Stored period " + field}])
        assert c.ev("() => document.querySelector('[aria-label=\"Exact stored text\"]').textContent") == answer["text"]
    assert c.ev(OVERFLOWING) == []
    c.assert_clean()


@ENGINES
@WINDOWS
def test_the_real_mixed_part_names_each_base_of_its_members(card, real_host, transfers, engine, viewport, theme):
    """The core's own part over an old block record, two transfers with stated periods:
    its title, its row, its Period fact and its three member rows come from the answers the host really gives."""
    ids = real_host.ids
    part = doc(real_host, ids["tr-mix-part"])
    assert part["period_basis"] == "mixed" and set(part["period_bases"]) == {"block", "stated"}
    c = card(engine=engine, viewport=viewport, theme=theme)
    c.open([{"v": "room", "room": "1"}])
    assert c.ev(META, "record:" + ids["tr-mix-part"]) == "Part · 10 Nov 2025 – 30 Apr 2026 (partly block period, partly stated by its source)"
    c.open([{"v": "room", "room": "1"}, route(ids["tr-mix-part"], "1", "part", "Part")])
    assert c.title() == "Main, 10 November 2025 – 30 April 2026 (partly block period, partly stated by its source)"
    assert c.ev(FACT, "Period") == part["period"]   # the core's own words, exactly
    c.wait_text("Folds 3 records")
    old_year = "" if c.ev("() => new Date().getFullYear()") == 2026 else " 2026"
    assert c.ev(META, "record:" + ids["old-block-synth"]) == f"Earlier dialogue · 1–30 Apr{old_year} (block period)"
    assert c.ev(META, "record:" + ids["tr-mix-rows"]) == "Earlier dialogue · 1–2 Dec 2025 (stated by its source)"
    assert c.ev(META, "record:" + ids["tr-mix-stated"]) == "Earlier dialogue · 10–14 Nov 2025 (stated by its source)"
    assert "Transferred · " in c.ev(ROW_FACTS, "record:" + ids["tr-mix-rows"])
    assert "Transferred" not in (c.ev(ROW_FACTS, "record:" + ids["old-block-synth"]) or "")   # the historical helper: no transfer
    assert c.ev(OVERFLOWING) == [] and c.ev(PAGE_OVERFLOW) <= 0
    c.shot("transfer-mixed-part")
    c.assert_clean()


def ordinary_headers(c, host):
    """The one header line of an ordinary record of each kind the Atlas signs: a mind's page, a Light draft, a child's accepted
    draft, the activation import's historical helper and a retelling shaped like it, each in the words it always had."""
    ids, old, out = host.ids, an_old_helper_record(host), {}
    cases = [("p1", "page", "1", "By Ouroboros"), ("draft", "page", "5203", "Draft by Light"),
             ("calib", "page", ids["room:garden-sensors"], "Draft by "),
             (old["id"], "legacy", "1", "Imported from the earlier dialogue file"),
             (ids["old-block-synth"], "legacy", "1", "Imported from the earlier dialogue file")]
    for key, kind, room, begins in cases:
        ident = ids.get(key, key)
        c.open([{"v": "room", "room": room}, route(ident, room, kind, "Record")])
        lines = c.ev(BYLINES)
        assert len(lines) == 1 and lines[0].startswith(begins) and lines[0].endswith(" · " + when(doc(host, ident)["record"]["ts"])), (key, lines)
        assert c.ev(FACT, "Transferred") is None and c.ev(FACT, "Signed as") is None, key
        out[key] = lines[0]
    return out


@ENGINES
@WINDOWS
def test_cutting_the_new_fields_from_the_answers_keeps_the_same_words(card, real_host, transfers, engine, viewport, theme):
    """A shape-compatibility proxy, NOT the old core and no emulation of it: the routes' answers of the new core are cut of the
    fields this change reads (an old core also answered less, e.g. its story entries had no author). It shows only that the
    widget's words for ordinary records do not depend on those fields and that a transfer shown without them claims nothing.
    Running the real old core is the baseline-suite command of the handoff."""
    ids = real_host.ids

    def field_cut(route_):
        response = route_.fetch()
        body = response.json()
        for holder in (body.get("record"), *(body.get("entries") or []), *[(e or {}).get("record") for e in body.get("entries") or []]):
            if isinstance(holder, dict):
                for name in NEW_FIELDS:
                    holder.pop(name, None)
        body.pop("period_bases", None)
        route_.fulfill(response=response, body=json.dumps(body), content_type="application/json")

    ordinary = card(engine=engine, viewport=viewport, theme=theme)
    now = ordinary_headers(ordinary, real_host)
    assert ordinary.ev(INJECTED) == 0
    ordinary.assert_clean()
    cut = card(engine=engine, viewport=viewport, theme=theme)
    for op in ("text", "list", "entries"):
        cut.page.route(f"**/atlas/v2/{op}?*", field_cut)
    assert ordinary_headers(cut, real_host) == now   # byte for byte the same words
    # a transfer seen without the fields: the author's words stay truthful (from `author`), one line, no claim of a transfer
    open_record(cut, ids["tr-source"], "1")
    lines = cut.ev(BYLINES)
    assert len(lines) == 1 and lines[0].startswith("Written by Synthetic Writer · model synthetic-model-a · signed in the source · ")
    assert cut.ev(FACT, "Transferred") is None
    cut.assert_clean()


@ENGINES
def test_projected_story_signature_and_ref_only_fields(card, real_host, transfers, engine):
    """Compatibility shape only: current story with author removed; no claim to run an older core."""
    ids = real_host.ids
    def without_author(route_):
        response = route_.fetch()
        body = response.json()
        for entry in body.get("entries") or []:
            entry.pop("author", None)
        route_.fulfill(response=response, body=json.dumps(body), content_type="application/json")
    c = card(engine=engine, viewport={"width": 1360, "height": 747}, theme="light")
    c.page.route("**/atlas/v2/list?*", without_author)
    c.open([{"v": "story"}])
    facts = c.ev(ROW_FACTS, "record:" + ids["tr-special"])
    assert facts.startswith("Transferred · Core signature: ") and "(excerpt)" in facts
    assert "Author not stated" not in facts and "[object Object]" not in facts
    # Budget/depth projection can omit the preview while retaining the same field address.
    def refs_only(route_):
        response = route_.fetch()
        body = response.json()
        record = body.get("record") or {}
        if record.get("id") == ids["tr-special"]:
            for value in [record["author"]["label"], record["author"]["model"], *record["transfer"].values()]:
                assert value.get("ref") and value.get("address")
                value.pop("preview", None)
        route_.fulfill(response=response, body=json.dumps(body), content_type="application/json")
    c.page.route("**/atlas/v2/text?*", refs_only)
    open_record(c, ids["tr-special"], "1")
    assert c.ev(BYLINES)[0] == "Written by recorded (open full value) · model recorded (open full value) · signed in the source"
    assert c.ev(FACT, "Transferred").count("recorded (open full value)") == 3
    c.click("Read full author name", exact=True)
    c.settle()
    assert c.ev("() => document.querySelector('[aria-label=\"Exact stored text\"]').textContent") == ids["tr-special-author"]["label"]
    c.assert_clean()
