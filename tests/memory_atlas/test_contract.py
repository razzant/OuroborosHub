"""memory-atlas.read/2: the routes' checks, the refusals, and the answers the widget relies on.

The first half needs no host: the closed parameter table, the unconnected refusal and how a core refusal
(``MemoryReadError``) becomes this contract's error. The second half asks the real isolated host
(``real_host``) through its own gateway route, ``/api/extensions/memory-atlas/atlas/v2/<op>``, over synthetic
memory written by Ouroboros's producers: PluginAPI 2.1 ``read_memory`` → ``CoreReader`` → routes.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from conftest import (LINKS_MANY, LINKS_SCOPE, LINKS_TARGET, ORDER_LONG, ORDER_SCOPE, ORDER_SHORT, ORDER_TARGET, PREFIX,
                      edit_many_links, ensure_links_order, ensure_many_links)

CONTRACT = "memory-atlas.read/2"
OPS = ("overview", "list", "text", "entries", "changes", "search", "inbound")


def call(routes, reader, op, **query):
    return routes.dispatch(reader, op, {k: str(v) for k, v in query.items()})


# ---- the routes without a host ------------------------------------------------------------------------------

@pytest.mark.parametrize("op,query", [
    ("overview", {}), ("list", {"area": "story"}), ("text", {"address": '{"kind":"record","id":"abc"}'}),
    ("entries", {"ids": '["abc"]'}), ("changes", {}), ("search", {"q": "x"}),
    ("inbound", {"scope": "global", "topic": "people/x"}),
])
def test_unconnected_reader_refuses_every_operation(routes, reader_mod, op, query):
    status, body = call(routes, reader_mod.UnconnectedReader(), op, **query)
    assert status == 503
    assert body == {"contract": CONTRACT, "error": {"code": "reader_not_connected", "message": reader_mod.UnconnectedReader.MESSAGE}}
    assert "source" not in body  # a refusal never claims a source


def test_reader_fault_is_reported_by_kind_without_traceback(routes):
    class Broken:
        mode, label = "core", "x"

        def overview(self):
            raise KeyError("secret path /Users/someone")

    status, body = call(routes, Broken(), "overview")
    assert status == 500 and body["error"] == {"code": "reader_failed", "message": "the memory reader failed (KeyError)"}


@pytest.mark.parametrize("op,query,code", [
    ("overview", {"x": "1"}, "unknown_parameter"),
    ("list", {}, "missing_parameter"),
    ("list", {"area": "everything"}, "invalid_parameter"),
    ("list", {"area": "shelf"}, "missing_parameter"),
    ("list", {"area": "relations", "address": '{"kind":"record","id":"a"}'}, "missing_parameter"),
    ("list", {"area": "rooms", "room_id": "1"}, "unknown_parameter"),
    ("list", {"area": "records", "kinds": "page,secret"}, "invalid_parameter"),
    ("list", {"area": "history", "address": '{"kind":"record","id":"a"}', "kinds": "page"}, "invalid_parameter"),
    ("list", {"area": "shelf", "scope": "elsewhere"}, "invalid_parameter"),
    ("list", {"area": "shelf", "scope": "global", "view": "deleted"}, "invalid_parameter"),
    ("list", {"area": "records", "limit": "501"}, "invalid_parameter"),
    ("list", {"area": "records", "limit": "0"}, "invalid_parameter"),
    ("text", {"address": '{"kind":"note","scope":"global","topic":"../identity"}'}, "invalid_parameter"),
    ("text", {"address": '{"kind":"note","scope":"global","topic":"/etc/passwd"}'}, "invalid_parameter"),
    ("text", {"address": '{"kind":"raw_file","path":"/etc/passwd"}'}, "invalid_parameter"),
    ("text", {"address": '{"kind":"record","id":"a","path":"/etc"}'}, "invalid_parameter"),
    ("text", {"address": '{"kind":"entry","journal":"identity","offset":-1}'}, "invalid_parameter"),
    ("text", {"address": "not json"}, "invalid_parameter"),
    ("text", {"address": '{"kind":"record","id":"a"}', "start": "-1"}, "invalid_parameter"),
    ("text", {"address": '{"kind":"record","id":"a"}', "length": "1048577"}, "invalid_parameter"),
    ("entries", {"ids": '"abc"'}, "invalid_parameter"),
    ("entries", {"ids": json.dumps(["a"] * 21)}, "invalid_parameter"),
    ("search", {"q": "   "}, "invalid_parameter"),
    ("search", {"q": "x" * 201}, "invalid_parameter"),
    ("search", {"q": "a\x00b"}, "invalid_parameter"),
    ("changes", {"sequence": "3"}, "invalid_parameter"),
    ("inbound", {"scope": "global"}, "missing_parameter"),
])
def test_routes_refuse_bad_queries_before_the_reader(routes, op, query, code):
    class MustNotBeCalled:
        mode, label = "core", "x"

        def __getattr__(self, name):
            raise AssertionError("reader called for a refused query")

    status, body = call(routes, MustNotBeCalled(), op, **query)
    assert status == 400 and body["error"]["code"] == code


def test_unknown_route_is_404(routes, reader_mod):
    assert call(routes, reader_mod.UnconnectedReader(), "graph")[0] == 404


def test_unicode_nested_topic_and_continuations_pass_as_typed_values(routes, reader_mod):
    seen = []

    def read_memory(operation, parameters=None):
        seen.append((operation, parameters))
        return {"contract": "ouroboros.memory/1", "operation": operation, "entries": [], "next": None, "complete": True}

    reader = reader_mod.CoreReader(read_memory)
    address = json.dumps({"kind": "note", "scope": "project:garden-sensors", "topic": "заметки/датчики/третий"})
    status, _ = call(routes, reader, "list", area="history", address=address, limit=7, cursor="o120")
    assert status == 200
    assert seen[-1] == ("catalog", {"area": "history", "limit": 7, "cursor": "o120",
                                    "address": {"kind": "note", "scope": "project:garden-sensors", "topic": "заметки/датчики/третий"}})
    call(routes, reader, "list", area="relations", address='{"kind":"record","id":"a"}', relation="source_changes", source=2)
    assert seen[-1][1]["source"] == 2 and seen[-1][1]["relation"] == "source_changes"
    call(routes, reader, "list", area="records", room_id="1", kinds="page,part", status="candidate", unfolded="true")
    assert seen[-1][1] == {"area": "records", "room_id": "1", "kinds": ["page", "part"], "status": ["candidate"], "unfolded": True}


def test_the_empty_room_id_is_the_hosts_room_and_left_out_is_every_room(routes, reader_mod):
    """``room_id`` given as exactly '' reaches the core as '' (the host's room) for records and marks; left out, it is
    not passed (every room). Other malformed ids are refused before the reader, as before."""
    seen = []

    def read_memory(operation, parameters=None):
        seen.append(parameters)
        return {"contract": "ouroboros.memory/1", "operation": operation, "entries": [], "next": None, "complete": True}

    reader = reader_mod.CoreReader(read_memory)
    for area in ("records", "marks"):
        assert call(routes, reader, "list", area=area, room_id="")[0] == 200 and seen[-1]["room_id"] == ""
        assert call(routes, reader, "list", area=area)[0] == 200 and "room_id" not in seen[-1]
    asked = len(seen)
    for bad in ("\n", "a\x00b", "x" * 201):
        status, body = call(routes, reader, "list", area="records", room_id=bad)
        assert status == 400 and body["error"]["code"] == "invalid_parameter", bad
    status, body = call(routes, reader, "list", area="rooms", room_id="")
    assert status == 400 and body["error"]["code"] == "unknown_parameter" and len(seen) == asked


class MemoryReadError(Exception):
    """Shaped like the core's refusal (matched by name and code, so the payload imports nothing from the core)."""

    def __init__(self, code, message, current_revision=None):
        super().__init__(message)
        self.code, self.message, self.current_revision = code, message, current_revision


@pytest.mark.parametrize("code,status", [("invalid_parameter", 400), ("not_found", 404), ("revision_mismatch", 409),
                                         ("position_mismatch", 409), ("unavailable", 503)])
def test_core_refusals_keep_their_code_and_current_revision(routes, reader_mod, code, status):
    def read_memory(operation, parameters=None):
        raise MemoryReadError(code, "the core said so", current_revision="r2" if code == "revision_mismatch" else None)

    got, body = call(routes, reader_mod.CoreReader(read_memory), "text", address='{"kind":"record","id":"a"}', revision="r1")
    assert got == status and body["error"]["code"] == code and body["error"]["message"] == "the core said so"
    assert body["error"].get("current_revision") == ("r2" if code == "revision_mismatch" else None)


def test_links_in_go_on_from_the_place_of_the_note_they_stopped_in(routes, reader_mod):
    """The shelf changes between two steps of links in. The step after goes on from the note it stopped in, found by
    itself in the core's listing; a note that has left goes on from the place where it stood, without its link
    cursor. Never by comparing topics as strings: the listing here is in path order, as the core's is."""
    target = "z-target"
    links = {"a/long": [{"status": "unwritten"}] * 2100, "a-short": [{"status": "present", "address": {"scope": "global", "topic": target}}]}
    shelf, asked = [], []

    def read_memory(operation, parameters=None):
        p = parameters
        if p["area"] == "shelf":
            entries = [{"scope": "global", "topic": t, "address": {"kind": "note", "scope": "global", "topic": t}} for t in shelf]
            return {"contract": "ouroboros.memory/1", "entries": entries, "next": None, "complete": True}
        asked.append((p["address"]["topic"], p.get("cursor")))
        rows, start = links.get(p["address"]["topic"], []), int((p.get("cursor") or "i0")[1:])
        end = min(start + p["limit"], len(rows))
        return {"contract": "ouroboros.memory/1", "entries": rows[start:end], "next": f"i{end}" if end < len(rows) else None,
                "complete": end >= len(rows)}

    reader = reader_mod.CoreReader(read_memory)
    for before, after, expected in ((["a/long", "a-short", target], ["0-new", "a/long", "a-short", target], ("a/long", "i2000")),
                                    (["a/long", "a-short", target], ["a-short", target], ("a-short", None))):
        shelf[:], asked[:] = before, []
        status, first = call(routes, reader, "inbound", scope="global", topic=target)
        assert status == 200 and first["read"]["continuing"] == {"topic": "a/long", "links_read": 2000}
        shelf[:], asked[:] = after, []   # a note written before it, or the long one gone
        status, second = call(routes, reader, "inbound", scope="global", topic=target, cursor=first["next"])
        assert status == 200 and asked[0] == expected, asked
        assert [n["topic"] for n in second["notes"]] == ["a-short"] and second["complete"] is True


def test_an_answer_outside_the_memory_contract_is_refused(routes, reader_mod):
    reader = reader_mod.CoreReader(lambda operation, parameters=None: {"contract": "something/else", "entries": []})
    status, body = call(routes, reader, "list", area="rooms")
    assert status == 500 and body["error"]["code"] == "contract_mismatch"
    other = reader_mod.CoreReader(lambda operation, parameters=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert call(routes, other, "list", area="rooms")[1]["error"]["code"] == "reader_failed"


# ---- the real host: PluginAPI 2.1 read_memory through the skill's own gateway routes ---------------------------

def get(host, op, **query):
    encoded = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else ",".join(v) if isinstance(v, list) else str(v)
               for k, v in query.items()}
    url = host.url + PREFIX + op + ("?" + urllib.parse.urlencode(encoded) if encoded else "")
    try:
        with urllib.request.urlopen(url, timeout=180) as response:
            body = json.loads(response.read())
            assert response.headers["Cache-Control"] == "no-store"
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def ok(host, op, **query):
    status, body = get(host, op, **query)
    assert status == 200, body
    assert body["contract"] == CONTRACT and body["memory_contract"] == "ouroboros.memory/1"
    assert body["source"]["mode"] == "core"
    return body


def record(ident, **extra):
    return {"kind": "record", "id": ident, **extra}


def test_overview_is_this_hosts_memory(real_host):
    body = ok(real_host, "overview")
    ids = real_host.ids
    rooms = {r["room_id"]: r for r in body["rooms"]["entries"]}
    assert rooms[""]["label"] == "Host records without a room" and rooms[""]["records"] >= 1
    assert rooms["1"]["label"] == "Main"
    assert rooms[ids["room:atlas-ru"]]["label"].startswith("Project Атлас памяти")
    assert body["chronicle"]["state"] == "present" and body["chronicle"]["activation"]["id"] == ids["activation"]
    assert body["since_known"] is False and type(body["position"]["sequence"]) is int
    assert [s["scope"] for s in body["shelves"]["entries"]][0] == "global"
    sources = [d["address"] for d in body["documents"]["entries"]]
    assert {"kind": "file", "source": "identity"} in sources and {"kind": "journal", "journal": "project:atlas-ru"} in sources


def test_a_text_over_one_range_reads_whole_by_code_points_from_one_revision(real_host):
    """1,088,000 code points of Cyrillic with one astral emoji per sentence, plus a correction under it."""
    ids = real_host.ids
    first = ok(real_host, "text", address=record(ids["big"]), length=131072)
    total, revision = first["range"]["total"], first["revision"]
    assert first["record"]["chars"] == ids["big_chars"] and first["corrections_shown"] == 1
    assert total > 1_048_576 and first["next"] == {"start": 131072, "revision": revision}
    small, answer = [first["text"]], first
    while answer["next"]:
        answer = ok(real_host, "text", address=record(ids["big"]), start=answer["next"]["start"],
                    revision=answer["next"]["revision"], length=131072)
        assert answer["revision"] == revision and answer["range"]["start"] == sum(len(t) for t in small)
        small.append(answer["text"])
    whole = "".join(small)
    assert len(whole) == total
    large = ok(real_host, "text", address=record(ids["big"]), length=1_048_576)
    rest = ok(real_host, "text", address=record(ids["big"]), start=1_048_576, revision=revision, length=1_048_576)
    assert large["text"] + rest["text"] == whole and rest["complete"] and rest["next"] is None
    unit = whole[:whole.index("Длинная", 1)]
    assert whole[:ids["big_chars"]] == unit * (ids["big_chars"] // len(unit))
    assert whole[ids["big_chars"]:].startswith("\n\n[correction ")
    at = whole.index("🌱")
    assert ok(real_host, "text", address=record(ids["big"]), start=at, length=1)["text"] == "🌱"   # one code point


def test_a_record_reads_as_cited_and_as_now(real_host):
    ids = real_host.ids
    page = ok(real_host, "list", area="relations", address=record(ids["account"]), relation="sources", limit=10)
    assert page["count"] == 5 and page["complete"]
    by_id = {s["id"]: s for s in page["entries"]}
    cadence = by_id[ids["cadence"]]
    assert cadence["revision"] == ids["cadence"] and cadence["current_revision"] != cadence["revision"]
    assert cadence["later_corrections"]["count"] == 1 and len(cadence["sha256"]) == 64
    cited = ok(real_host, "text", address=record(ids["cadence"]), revision=cadence["revision"])
    now = ok(real_host, "text", address=record(ids["cadence"]))
    assert cited["corrections_shown"] == 0 and cited["later_corrections"]["count"] == 1
    assert now["corrections_shown"] == 1 and now["later_corrections"]["count"] == 0
    assert now["text"].startswith(cited["text"]) and "[correction " in now["text"][len(cited["text"]):]
    later = ok(real_host, "list", **cadence["later_corrections"]["next"])
    assert [e["kind"] for e in later["entries"]] == ["correction"]
    part = by_id[ids["gpart"]]
    assert part["kind"] == "part" and part["nested_changes"]["count"] >= 1
    nested = ok(real_host, "list", **part["nested_changes"]["next"], limit=1)
    assert nested["member_versions"] == "not_recorded" and len(nested["entries"]) == 1
    if nested["count"] > 1:
        more = ok(real_host, "list", **part["nested_changes"]["next"], limit=1, cursor=nested["next"])
        assert more["entries"][0]["index"] == 1


def test_entries_show_each_record_as_a_list_does(real_host):
    ids = real_host.ids
    body = ok(real_host, "entries", ids=json.dumps([ids["cadence"], ids["photos"], "no-such-record"]))
    first, second, missing = body["entries"]
    assert missing == {"id": "no-such-record", "missing": True}
    for found in (first, second):
        entry = found["record"]
        assert entry["preview"] and len(entry["preview"]) == min(entry["chars"], 280)
        assert entry["room_label"] and "current_revision" in entry
    listed = ok(real_host, "list", area="records", room_id=ids["room:garden-sensors"], kinds=["page"], limit=50)
    same = next(e for e in listed["entries"] if e["id"] == ids["cadence"])
    assert first["record"]["preview"] == same["preview"]   # own words only, never a correction


def test_the_story_pages_and_refuses_a_cursor_of_another_story(real_host):
    first = ok(real_host, "list", area="story", limit=5)
    assert first["state"] == "active" and first["count"] > 5 and first["next"].startswith("i5:")
    second = ok(real_host, "list", area="story", limit=5, cursor=first["next"])
    assert not {e["id"] for e in first["entries"]} & {e["id"] for e in second["entries"]}
    forged = "i5:" + "0" * 32
    status, body = get(real_host, "list", area="story", limit=5, cursor=forged)
    assert status == 409 and body["error"]["code"] == "position_mismatch"
    account = next(e for e in first["entries"] + second["entries"] if e["kind"] == "account")
    assert account["replaces"]["count"] == 3


def test_drafts_keep_their_recorded_status(real_host):
    ids = real_host.ids
    garden = ok(real_host, "list", area="records", room_id=ids["room:garden-sensors"], kinds=["page", "part"],
                status=["candidate"], limit=200)
    assert [e["id"] for e in garden["entries"]] == [ids["candidate"]]
    status = {}
    for room in ("5203", ids["room:release-flow"], ids["room:garden-sensors"]):
        for e in ok(real_host, "list", area="records", room_id=room, kinds=["page", "part"], limit=200)["entries"]:
            status[e["id"]] = e["status"]
    assert status[ids["draft"]] == "draft" and status[ids["wrong"]] == "rejected" and status[ids["calib"]] == "accepted"
    assert status[ids["draft_part"]] == "draft"
    decisions = ok(real_host, "list", area="history", address=record(ids["wrong"]), kinds=["decision"])
    assert decisions["entries"][0]["accepted"] is False and decisions["entries"][0]["preview"].startswith("The host stamp")


def test_marks_active_released_and_seen_by_meaning(real_host):
    ids = real_host.ids
    marks = {m["id"]: m for m in ok(real_host, "list", area="marks", state="all", limit=50)["entries"]}
    assert marks[ids["mark_released"]]["released"]["ts"] and marks[ids["mark_global"]]["released"] is None
    assert marks[ids["mark_global"]]["scope"] == "global" and marks[ids["mark_view"]]["visibility"] == "meaning"
    active = ok(real_host, "list", area="marks", state="active", limit=50)["entries"]
    assert ids["mark_released"] not in {m["id"] for m in active}


def test_the_hosts_room_lists_by_its_empty_id_on_the_core(real_host):
    """Records and marks of exactly room '' are the host's room: the same records an unfiltered list gives with that
    room id. A core that refuses the empty id fails this."""
    status, body = get(real_host, "list", area="records", room_id="", limit=50)
    assert status == 200, body
    every = ok(real_host, "list", area="records", limit=500)
    assert every["complete"] and body["complete"]
    assert [e["id"] for e in body["entries"]] == [e["id"] for e in every["entries"] if e["room_id"] == ""]
    assert body["entries"][0]["id"] == real_host.ids["activation"]
    marks = ok(real_host, "list", area="marks", room_id="", state="all", limit=500)
    every_mark = ok(real_host, "list", area="marks", state="all", limit=500)
    assert [m["id"] for m in marks["entries"]] == [m["id"] for m in every_mark["entries"] if m["room_id"] == ""]


def test_knowledge_archived_nested_and_long_summary(real_host):
    archived = ok(real_host, "list", area="shelf", scope="global", view="archived", limit=100)
    assert [n["state"] for n in archived["entries"]] == ["archived"] and archived["entries"][0]["archive"]
    shelf = ok(real_host, "list", area="shelf", scope="project:atlas-ru", view="all", limit=25)
    topics, page = [n["topic"] for n in shelf["entries"]], shelf
    while page["next"]:
        page = ok(real_host, "list", area="shelf", scope="project:atlas-ru", view="all", limit=25, cursor=page["next"])
        topics += [n["topic"] for n in page["entries"]]
    assert len(topics) == len(set(topics)) == 65 and "архив/раздел-3/заметка-63" in topics
    big = next(n for n in shelf["entries"] + page["entries"] if n["topic"] == "большая/заметка")
    summary = big["summary"]
    assert summary["ref"] == "text" and summary["chars"] > 2000 and summary["address"]["field"] == ["summary"]
    # The same summary over a short body reads whole by its field address; read from the large note, the field
    # would wait on the core's markdown parse of that note's body (test_widget_browser.py opens that note last).
    small = ok(real_host, "list", area="shelf", scope="global", view="active", limit=100)["entries"]
    short = next(n for n in small if n["topic"] == "атлас/длинное-резюме")["summary"]
    assert short["chars"] == summary["chars"] and short["address"]["field"] == ["summary"]
    whole = ok(real_host, "text", address=short["address"], length=1_048_576)
    assert len(whole["text"]) == short["chars"] and whole["text"].startswith(short["preview"])
    assert "note_revision" in whole and whole["representation"] == "text"
    unsummarised = next(n for n in shelf["entries"] if n["topic"] == "архив/раздел-0/заметка-00")
    assert unsummarised["summary"] is None


def test_a_note_changed_since_its_revision_is_refused_with_the_current_one(real_host):
    note = {"kind": "note", "scope": "global", "topic": "people/mira"}
    current = ok(real_host, "text", address=note, length=10)
    status, body = get(real_host, "text", address=note, start=10, revision="0" * 64)
    assert status == 409 and body["error"]["code"] == "revision_mismatch"
    assert body["error"]["current_revision"] == current["revision"]


def test_search_is_literal_bounded_and_continues_to_the_end(real_host):
    q = "ЗАМЕТКА"   # any letter case, Cyrillic
    step = ok(real_host, "search", q=q)
    hits = {"records": 0, "notes": 0, "files": 0}
    steps = 0
    while True:
        steps += 1
        assert step["read"]["chars"] <= 4 * 1024 * 1024 + 1_048_576   # one request's text budget
        for hit in step["hits"]:
            hits[hit["group"]] += 1
            assert hit["snippet"]["match"].lower() == q.lower()
        if step["complete"]:
            assert step["next"] is None
            break
        assert steps < 60, "search did not finish within the steps memory of this size needs"
        step = ok(real_host, "search", q=q, cursor=step["next"])
    assert hits["records"] >= 120 and hits["notes"] >= 65
    status, body = get(real_host, "search", q="другое", cursor=ok(real_host, "search", q=q)["next"])
    assert status == 400 and body["error"]["code"] == "invalid_parameter"


def test_links_into_a_note_are_read_on_its_shelf(real_host):
    body, notes = ok(real_host, "inbound", scope="global", topic="people/mira"), []
    for _step in range(20):
        notes += [n["topic"] for n in body["notes"]]
        if body["complete"]:
            break
        body = ok(real_host, "inbound", scope="global", topic="people/mira", cursor=body["next"])
    assert body["complete"] and set(notes) >= {"people/lev", "overview"}
    assert body["coverage"] == "the notes on the same shelf"


def test_changes_from_a_baseline(real_host):
    base = ok(real_host, "changes")
    assert base["since_known"] is False
    pos = base["position"]
    same = ok(real_host, "changes", sequence=pos["sequence"], record_id=pos["record_id"])
    assert same["since_known"] is True and same["entries"] == [] and same["complete"]
    status, body = get(real_host, "changes", sequence=pos["sequence"], record_id="not-that-record")
    assert status == 409 and body["error"]["code"] == "position_mismatch"


def test_missing_and_malformed_reads_are_typed(real_host):
    status, body = get(real_host, "text", address=record("no-such-record"))
    assert status == 404 and body["error"]["code"] == "not_found"
    status, body = get(real_host, "list", area="records", cursor="r1")
    assert status == 400 and body["error"]["code"] == "invalid_parameter"


ROW = r"^row:1@2026-10-24T0(7:55|8:05):00\+00:00#[0-9a-f]{12}$"
PERIOD = ("period", "source_period", "period_basis")


def test_a_quoted_page_keeps_each_checked_quote_with_its_chat_row(real_host):
    """The weekly page was written by ``chronicle_write`` over two chat rows: its quotes come back whole, each
    with the row's exact address and the speaker class the host checked, and its covers keep the rows' times."""
    import re

    ids = real_host.ids
    doc = ok(real_host, "text", address=record(ids["weekly"]), length=10)
    quotes = doc["stored"]["quotes"]
    assert quotes == ids["quotes"]   # as the writer passed them: address, text, speaker
    assert [q["speaker"] for q in quotes] == ["human", "ouroboros"]
    assert all(re.match(ROW, q["address"]) for q in quotes) and [q["address"] for q in quotes] == ids["quote_rows"]
    one = ok(real_host, "text", address=record(ids["weekly"], field=["quotes", 0]))
    assert one["representation"] == "json" and json.loads(one["text"]) == quotes[0]
    covers = doc["record"]["covers"]
    assert covers["count"] == 2 and covers["ts_span"]["start"] == "2026-10-24T07:55:00+00:00"
    story = ok(real_host, "list", area="story", limit=50)["entries"]
    entry = next(e for e in story if e["id"] == ids["weekly"])
    assert entry["quotes_count"] == 2
    assert entry["source_period"] == {"start": "2026-10-24T07:55:00+00:00", "end": "2026-10-24T08:05:00+00:00", "incomplete": False}
    # The document carries the same period as the story, at its top level and not again inside its record.
    assert {k: doc[k] for k in PERIOD} == {"period": entry["period"], "source_period": entry["source_period"], "period_basis": "rows"}
    assert not set(PERIOD) & set(doc["record"])


def test_the_canonical_writer_refuses_a_quote_that_is_not_the_rows_words(real_host):
    """The check the block's heading names: a changed word, or the wrong speaker, is refused and nothing is written."""
    out = real_host.producer(
        "from ouroboros.tools.chronicle import _chronicle_write\n"
        "from ouroboros.tools.tool_context import ToolContext\n"
        "from ouroboros.chronicle_store import ChronicleStore\n"
        "ctx = ToolContext(repo_dir=root, drive_root=root, task_id='t-main', current_chat_id=1, is_direct_chat=True)\n"
        "first, last = ids['quote_rows']\n"
        "before = len(ChronicleStore(root).records('1', kinds=['page']))\n"
        "for forged in ({'address': first, 'text': 'Weekly is fine. Thursday after lunch works better for Lev.', 'speaker': 'human'},\n"
        "               {'address': first, 'text': 'Weekly is fine.', 'speaker': 'ouroboros'}):\n"
        "    answer = json.loads(_chronicle_write(ctx, kind='page', room_id='1', text='forged', covers={'from': first, 'to': last}, quotes=[forged]))\n"
        "    print(answer.get('ok'), answer.get('reason'))\n"
        "print(len(ChronicleStore(root).records('1', kinds=['page'])) == before)\n")
    assert out.split() == ["False", "quote_mismatch", "False", "quote_mismatch", "True"], out


def test_links_in_continue_a_long_note_past_one_steps_budget(real_host):
    """A note's 2,501st link is the one into the target: the first step reads 2,000 links, says which note it is
    still reading and stays incomplete; the next continues that note where it stopped and finds the link."""
    ensure_many_links(real_host)
    first = ok(real_host, "inbound", scope=LINKS_SCOPE, topic=LINKS_TARGET)
    assert first["notes"] == [] and first["complete"] is False and first["next"]
    assert first["read"] == {"notes": 0, "links": 2000, "continuing": {"topic": LINKS_MANY, "links_read": 2000}}
    second = ok(real_host, "inbound", scope=LINKS_SCOPE, topic=LINKS_TARGET, cursor=first["next"])
    assert [n["topic"] for n in second["notes"]] == [LINKS_MANY] and second["read"]["continuing"] is None
    assert second["complete"] is True and second["next"] is None
    status, body = get(real_host, "inbound", scope=LINKS_SCOPE, topic="links-check/other", cursor=first["next"])
    assert status == 400 and body["error"]["code"] == "invalid_parameter"   # a cursor answers its own question only


def test_links_in_go_on_in_the_shelfs_own_order(real_host):
    """The core lists a shelf in path order: ``a/long`` (2,100 links) before ``a-short`` (one link into the target),
    though as strings ``a-short`` sorts first. The step after the budget ran out goes on from ``a/long``'s place and
    reaches ``a-short``."""
    ensure_links_order(real_host)
    shelf = ok(real_host, "list", area="shelf", scope=ORDER_SCOPE, view="all", limit=25)
    topics = [n["topic"] for n in shelf["entries"]]
    assert topics == [ORDER_LONG, ORDER_SHORT, ORDER_TARGET] and sorted(topics) != topics and shelf["complete"]
    first = ok(real_host, "inbound", scope=ORDER_SCOPE, topic=ORDER_TARGET)
    assert first["notes"] == [] and first["complete"] is False
    assert first["read"] == {"notes": 0, "links": 2000, "continuing": {"topic": ORDER_LONG, "links_read": 2000}}
    second = ok(real_host, "inbound", scope=ORDER_SCOPE, topic=ORDER_TARGET, cursor=first["next"])
    assert [n["topic"] for n in second["notes"]] == [ORDER_SHORT], second
    assert second["read"] == {"notes": 2, "links": 101, "continuing": None}
    assert second["complete"] is True and second["next"] is None


def test_a_notes_links_continue_one_revision_and_a_changed_note_reads_again_from_the_start(real_host):
    """The core binds a note's links cursor to the note's revision. The note edited after a first
    page refuses the next one, revision_mismatch with the revision now, so two versions are never joined; read again
    from the start, the links are the new version's and continue by its own cursor."""
    ensure_many_links(real_host)
    address = {"kind": "note", "scope": LINKS_SCOPE, "topic": LINKS_MANY}
    try:
        first = ok(real_host, "list", area="relations", address=address, relation="links", limit=200)
        assert first["count"] == 2600 and len(first["entries"]) == 200 and first["next"] == "i200:" + first["revision"]
        now = edit_many_links(real_host)
        assert now != first["revision"] and ok(real_host, "text", address=address, length=1)["revision"] == now
        status, body = get(real_host, "list", area="relations", address=address, relation="links", limit=200, cursor=first["next"])
        assert status == 409 and body["error"] == {"code": "revision_mismatch", "current_revision": now,
                                                   "message": "the note changed since that page: read its links again from the start"}
        again = ok(real_host, "list", area="relations", address=address, relation="links", limit=200)
        assert again["revision"] == now and again["count"] == 2601 and again["entries"][0]["label"] == "the target"
        on = ok(real_host, "list", area="relations", address=address, relation="links", limit=200, cursor=again["next"])
        assert on["entries"][0]["label"] == "w0199" and on["entries"][0]["index"] == 200 and on["next"] == "i400:" + now
    finally:
        ensure_many_links(real_host)


def test_links_in_pass_on_a_note_that_changed_between_two_steps(real_host):
    """A links-in step continues a note by the core's links cursor, bound to the note's revision. The note edited
    between two steps: the next step is refused as the core refused it (revision_mismatch, the revision now), its
    cursor never mixed into the new version. From the start, links in read the new version and find its link."""
    ensure_many_links(real_host)
    try:
        first = ok(real_host, "inbound", scope=LINKS_SCOPE, topic=LINKS_TARGET)
        assert first["notes"] == [] and first["read"]["continuing"] == {"topic": LINKS_MANY, "links_read": 2000}
        now = edit_many_links(real_host)
        status, body = get(real_host, "inbound", scope=LINKS_SCOPE, topic=LINKS_TARGET, cursor=first["next"])
        assert status == 409 and body["error"] == {"code": "revision_mismatch", "current_revision": now,
                                                   "message": "the note changed since that page: read its links again from the start"}
        again = ok(real_host, "inbound", scope=LINKS_SCOPE, topic=LINKS_TARGET)
        assert [n["topic"] for n in again["notes"]] == [LINKS_MANY] and again["read"]["continuing"] is None
        assert again["complete"] is True and again["next"] is None
    finally:
        ensure_many_links(real_host)


def test_a_draft_takes_one_decision_so_its_last_decision_is_current(real_host):
    """The store refuses a second decision on a draft, so its last decision is current. The decision block does not
    depend on this: it reads the decision history to its end (see test_widget_browser.py)."""
    out = real_host.producer(
        "from ouroboros.chronicle_store import ChronicleStore\n"
        "author = {'kind': 'mind', 'task_id': 't-main', 'focus': {'role': 'main chat', 'task_id': 't-main'}}\n"
        "result = ChronicleStore(root).decide(ids['wrong'], True, author, 'A second decision.')\n"
        "print(result.ok, result.reason, list(result.conflict_ids))\n")
    ok_, reason, conflicts = out.split(" ", 2)
    assert ok_ == "False" and reason == "invalid" and "[" in conflicts
    history = ok(real_host, "list", area="history", address=record(real_host.ids["wrong"]), kinds=["decision"], limit=500)
    assert len(history["entries"]) == 1 and history["complete"]


def test_dated_records_carry_the_cores_period_and_nothing_else_does(real_host):
    """The core's periods: page, part, account and retold records carry `period`,
    `source_period` and `period_basis` at the document's top level and on their list entries; other kinds none."""
    ids = real_host.ids

    def doc(key):
        return ok(real_host, "text", address=record(ids[key] if key in ids else key), length=1)

    weekly = doc("weekly")
    assert weekly["period"] == "2026-10-24 07:55 → 2026-10-24 08:05" and weekly["period_basis"] == "rows"
    assert weekly["source_period"] == {"start": "2026-10-24T07:55:00+00:00", "end": "2026-10-24T08:05:00+00:00", "incomplete": False}
    unknown = doc("p1")   # a page published without covered rows' times: unknown, never its write time
    assert (unknown["period"], unknown["source_period"], unknown["period_basis"]) == ("period unknown", None, "rows")
    account = doc("account")   # the union over its sources, one of them dated
    assert account["source_period"]["incomplete"] is True and account["period"].endswith("(incomplete)")
    legacy_id = next(e["id"] for e in ok(real_host, "list", area="story", limit=5)["entries"] if e["kind"] == "legacy")
    legacy = doc(legacy_id)
    assert legacy["period_basis"] == "block" and legacy["source_period"] is None and legacy["period"].endswith("(block period)")
    for key in ("fix_weekly", "note", "mark_global"):
        assert not set(PERIOD) & set(doc(key)), key
    for entry in ok(real_host, "list", area="records", room_id="1", limit=50)["entries"]:
        dated = entry["kind"] in ("page", "part", "account", "legacy", "gap")
        assert (set(PERIOD) <= set(entry)) if dated else not set(PERIOD) & set(entry), entry["kind"]
        if entry["id"] == ids["weekly"]:
            assert {k: entry[k] for k in PERIOD} == {k: weekly[k] for k in PERIOD}
    member = ok(real_host, "list", area="relations", address=record(ids["part"]), relation="members")["entries"][0]["member"]
    assert member["period"] == "period unknown"
    citing = ok(real_host, "list", area="relations", address=record(ids["weekly"]), relation="cited_by")["entries"][0]["account"]
    assert {k: citing[k] for k in PERIOD} == {k: account[k] for k in PERIOD}
    told = ok(real_host, "entries", ids=json.dumps([ids["weekly"], ids["fix_weekly"]]))["entries"]
    assert {k: told[0]["record"][k] for k in PERIOD} == {k: weekly[k] for k in PERIOD}   # copied from the top level
    assert not set(PERIOD) & set(told[1]["record"])
