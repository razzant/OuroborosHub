"""The skill's own bounded reads over ``read_memory``: literal search and the links into one note.

Neither is a core operation, so each states what it read and continues by its own cursor; one request
reads at most one catalog page and ``TEXT_BUDGET`` code points of text, never all memory. Matching is
literal and ignores letter case (``re.IGNORECASE`` over the text as the core returns it).

Search goes through three groups in a fixed order, each by the core's own catalog pages:

1. ``records``: the own words of every chronicle record in publication order (``catalog records``):
   the list's preview when it holds all of them, else the record's text read by ranges up to its own
   words (``document``), bound to one revision;
2. ``notes``: the whole source of every knowledge note on every shelf (``catalog shelves``, ``shelf``,
   ``document``), and its topic;
3. ``files``: identity, the World profile, the scratchpad and each Project's workpad (``documents``).

Journals (earlier versions) are not searched. One hit per item: its first match.

The cursor is this module's own (base64 JSON): it names the group, the core cursor of the page being
read, the next item in it and, inside a long text, the next start and its revision. It is bound to the
query, so a cursor of another query is refused.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .atlas_reader import BadRequest, ReaderError

TEXT_BUDGET = 4 * 1024 * 1024  # code points of text one request scans
RANGE = 1_048_576  # one document range
HITS_MAX = 40  # a request stops after this many hits
PREVIEW_CHARS = 280  # the core's list preview
PAGES = {"records": 50, "notes": 25, "files": 50, "inbound": 25}
LINK_BUDGET = 2000  # links one request reads while looking for links into a note (pages of up to 500)
BEFORE, AFTER = 60, 120
GROUPS = ("records", "notes", "files")
SCOPE = ("the own words of every chronicle record, the whole source of every knowledge note on every shelf, "
         "and identity, World, the scratchpad and each Project's workpad; earlier versions in journals are not searched")


def _key(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]


def _encode(state: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(state, separators=(",", ":")).encode("utf-8")).decode("ascii")


def _decode(cursor: Optional[str], key: str) -> Optional[Dict[str, Any]]:
    if cursor is None:
        return None
    try:
        state = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (ValueError, UnicodeError):
        state = None
    if not isinstance(state, dict) or state.get("k") != key:
        raise BadRequest("cursor is the `next` an earlier answer to this question returned", code="invalid_parameter")
    return state


def _snippet(text: str, match: "re.Match[str]") -> Dict[str, Any]:
    start, end = match.start(), match.end()
    return {"before": text[max(0, start - BEFORE):start], "match": text[start:end],
            "after": text[end:end + AFTER], "clipped_start": start > BEFORE, "clipped_end": end + AFTER < len(text)}


class _Scan:
    """One request's work: hits, what was read, and the text budget left."""

    def __init__(self, reader: Any, pattern: "re.Pattern[str]", overlap: int) -> None:
        self.reader, self.pattern, self.overlap = reader, pattern, overlap
        self.hits: List[Dict[str, Any]] = []
        self.read = {"records": 0, "notes": 0, "files": 0, "chars": 0, "texts_cut": 0}
        self.budget = TEXT_BUDGET

    def full(self) -> bool:
        return len(self.hits) >= HITS_MAX or self.budget <= 0

    def text(self, address: Mapping[str, Any], start: int, revision: Optional[str], stop: Optional[int] = None
             ) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[int, str]]]:
        """Scan ``address`` from ``start`` (up to ``stop`` code points) within the budget.

        Returns ``(snippet or None, (next start, revision) when the budget ran out first, else None)``. A text
        that changed since ``revision`` was named is read again from its start at its current revision.
        """
        restarts = 0
        while True:
            length = min(RANGE, max(self.budget, self.overlap + 1))
            if stop is not None:
                length = min(length, max(stop - start, 1))
            params: Dict[str, Any] = {"address": dict(address), "start": start, "length": length}
            if revision:
                params["revision"] = revision
            try:
                answer = self.reader.call("document", params)
            except ReaderError as exc:
                if exc.code != "revision_mismatch" or restarts >= 2:
                    raise
                restarts += 1
                start, revision = 0, None  # never a range of one version beside a range of another
                continue
            text = answer.get("text") or ""
            revision = answer.get("revision")
            self.budget -= len(text)
            self.read["chars"] += len(text)
            match = self.pattern.search(text)
            if match:
                return _snippet(text, match), None
            end = answer["range"]["end"]
            done = answer.get("complete") or (stop is not None and end >= stop)
            if done:
                return None, None
            start = max(end - self.overlap, start + 1)  # a match across the range's edge is still found
            if self.budget <= 0:
                return None, (start, revision)


def _record_item(scan: _Scan, entry: Mapping[str, Any], start: int, revision: Optional[str]):
    """A record's own words: the list preview when it holds them all, else its text up to their length."""
    chars = int(entry.get("chars") or 0)
    if chars <= PREVIEW_CHARS and start == 0:
        match = scan.pattern.search(str(entry.get("preview") or ""))
        return (_snippet(str(entry.get("preview") or ""), match) if match else None), None
    return scan.text(entry["address"], start, revision, stop=chars)


def _note_item(scan: _Scan, entry: Mapping[str, Any], start: int, revision: Optional[str]):
    if start == 0:
        match = scan.pattern.search(str(entry.get("topic") or ""))
        if match:
            return _snippet(str(entry.get("topic")), match), None
    return scan.text(entry["address"], start, revision)


def _file_item(scan: _Scan, entry: Mapping[str, Any], start: int, revision: Optional[str]):
    return scan.text(entry["address"], start, revision)


def _page(reader: Any, group: str, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The catalog page ``state`` points at in ``group``; None when the group has no further shelf."""
    limit = PAGES[group]
    if group == "records":
        return reader.catalog("records", limit=limit, **({"cursor": state["pc"]} if state.get("pc") else {}))
    if group == "files":
        page = reader.catalog("documents", limit=limit, **({"cursor": state["pc"]} if state.get("pc") else {}))
        page["entries"] = [entry for entry in page["entries"] if entry["address"]["kind"] == "file" and entry.get("exists")]
        return page
    while True:  # notes: shelf by shelf, in scope order
        if not state.get("scope"):
            shelves = reader.catalog("shelves", limit=500, **({"cursor": state["sc"]} if state.get("sc") else {}))
            later = [row for row in shelves["entries"] if row.get("present") and row["scope"] > state.get("last", "")]
            if not later:
                if shelves["complete"]:
                    return None
                state["sc"], state["last"] = shelves["next"], shelves["entries"][-1]["scope"]
                continue
            state.update(scope=later[0]["scope"], pc=None, i=0)
        return reader.catalog("shelf", scope=state["scope"], view="all", limit=limit,
                              **({"cursor": state["pc"]} if state.get("pc") else {}))


def _advance(group: str, state: Dict[str, Any], page: Mapping[str, Any]) -> Optional[str]:
    """After a page is read whole: the next page of the group, or the next group (None after the last)."""
    state.update(i=0, off=0, rev=None)
    if not page["complete"]:
        state["pc"] = page["next"]
        return group
    state["pc"] = None
    if group == "notes":
        state.update(last=state.pop("scope"))
        return group
    following = GROUPS.index(group) + 1
    return GROUPS[following] if following < len(GROUPS) else None


def search(reader: Any, q: str, cursor: Optional[str]) -> Dict[str, Any]:
    key = _key("search", q)
    state = _decode(cursor, key) or {"k": key, "g": "records", "pc": None, "i": 0, "off": 0, "rev": None}
    pattern = re.compile(re.escape(q), re.IGNORECASE)
    scan = _Scan(reader, pattern, overlap=max(len(q) - 1, 0))
    group: Optional[str] = state["g"]
    items = {"records": _record_item, "notes": _note_item, "files": _file_item}
    pages = 0
    while group is not None and not scan.full() and pages < 1:  # one catalog page per request
        page = _page(reader, group, state)
        if page is None:  # no further shelf
            group = "files"
            state.update(pc=None, i=0, off=0, rev=None)
            continue
        pages += 1
        entries = page["entries"]
        while state["i"] < len(entries) and not scan.full():
            entry = entries[state["i"]]
            snippet, pending = items[group](scan, entry, state.get("off", 0), state.get("rev"))
            if pending is not None:
                state["off"], state["rev"] = pending
                scan.read["texts_cut"] += 1
                break
            scan.read[group] += 1
            if snippet:
                scan.hits.append({"group": group, "item": entry, "snippet": snippet})
            state.update(i=state["i"] + 1, off=0, rev=None)
        if state["i"] >= len(entries):
            group = _advance(group, state, page)
    state["g"] = group
    complete = group is None
    return {"q": q, "match": "literal, any letter case", "scope": SCOPE, "hits": scan.hits,
            "read": scan.read, "complete": complete, "next": None if complete else _encode(state)}


def inbound(reader: Any, scope: str, topic: str, cursor: Optional[str]) -> Dict[str, Any]:
    """Notes on the same shelf whose links resolve to ``scope``/``topic``, a bounded step at a time.

    One request reads one shelf page (``PAGES["inbound"]`` notes) and at most ``LINK_BUDGET`` links in all. A
    note whose links run past the budget is continued by the next request from the exact link page it stopped
    at (``read.continuing`` names it): the answer stays incomplete until every link of every note was checked,
    so "no note links here" is never said of a shelf read in part.

    That request reads the same shelf page again (the core cursor that listed it) and goes on from the note's
    place in it, in the core's own order (path order, which is not the order of the topics as strings). A note
    that left the shelf since is passed over from the place where it stood.
    """
    key = _key("inbound", scope, topic)
    state = _decode(cursor, key) or {"k": key, "pc": None, "t": None, "i": 0, "lc": None, "ln": 0}
    if (not isinstance(state.get("ln", 0), int) or not isinstance(state.get("i", 0), int) or state.get("i", 0) < 0
            or state.get("t") is not None and not isinstance(state["t"], str)):
        raise BadRequest("cursor is the `next` an earlier answer to this question returned", code="invalid_parameter")
    page = reader.catalog("shelf", scope=scope, view="all", limit=PAGES["inbound"],
                          **({"cursor": state["pc"]} if state.get("pc") else {}))
    entries, start, resume = page["entries"], 0, state.get("t")
    if resume is not None:
        topics = [entry["topic"] for entry in entries]
        if resume in topics:
            start = topics.index(resume)
        else:
            start, resume = min(state.get("i", 0), len(entries)), None
    found, checked, budget, links_read = [], 0, LINK_BUDGET, 0
    for index, entry in enumerate(entries[start:], start):
        if entry["scope"] == scope and entry["topic"] == topic:
            continue
        mid = resume is not None and entry["topic"] == resume
        links_cursor, so_far = (state.get("lc"), state.get("ln", 0)) if mid else (None, 0)
        resume, hit, done = None, False, False
        while True:
            if budget <= 0:
                state.update(t=entry["topic"], i=index, lc=links_cursor, ln=so_far)   # this note goes on in the next step
                return _inbound_answer(scope, topic, found, checked, links_read, page, state, stopped=True,
                                       continuing=(entry["topic"], so_far) if so_far else None)
            try:
                links = reader.catalog("relations", address=dict(entry["address"]), relation="links", limit=min(500, budget),
                                       **({"cursor": links_cursor} if links_cursor else {}))
            except ReaderError as exc:
                if exc.code != "not_found":
                    raise
                done = True   # the note left the shelf since its page was listed
                break
            budget -= len(links["entries"]) or 1
            links_read += len(links["entries"])
            so_far += len(links["entries"])
            hit = any(link.get("status") == "present" and link.get("address") and link["address"].get("scope") == scope
                      and link["address"].get("topic") == topic for link in links["entries"])
            if hit or links["complete"]:
                done = True
                break
            links_cursor = links["next"]
        if done:
            checked += 1
            if hit:
                found.append(entry)
    state.update(pc=page["next"], t=None, i=0, lc=None, ln=0)
    return _inbound_answer(scope, topic, found, checked, links_read, page, state, stopped=False, continuing=None)


def _inbound_answer(scope, topic, found, checked, links_read, page, state, *, stopped, continuing):
    complete = not stopped and page["complete"]
    return {"scope": scope, "topic": topic, "notes": found,
            "read": {"notes": checked, "links": links_read,
                     "continuing": {"topic": continuing[0], "links_read": continuing[1]} if continuing else None},
            "coverage": "the notes on the same shelf", "complete": complete,
            "next": None if complete else _encode(state)}
