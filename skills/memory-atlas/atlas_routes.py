"""Routes of the ``memory-atlas.read/2`` contract, independent of the web framework.

``dispatch(reader, op, query)`` checks a query string against one closed table, turns it into the
typed parameters of a reader call and returns ``(status, body)``. Every body names the contract; a
success also names its source (``mode`` and ``label``) so the widget can never present missing data
as the owner's memory. Errors carry a stable ``code`` and a readable ``message``, never a traceback;
a source that changed between two reads adds its ``current_revision``.

Lists and texts continue: a list answer's ``next`` goes back as ``cursor``, a text answer's
``next`` as ``start`` and ``revision``. Nothing here caps what can be reached.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .atlas_reader import ENTRIES_MAX, BadRequest, ReaderError

CONTRACT = "memory-atlas.read/2"
ROUTE_PREFIX = "atlas/v2/"

AREAS = ("rooms", "records", "story", "marks", "shelves", "shelf", "documents", "history", "relations")
RECORD_KINDS = ("page", "part", "note", "correction", "decision", "mark", "mark_view", "mark_release",
                "account", "selection", "legacy", "gap")
HISTORY_KINDS = ("correction", "decision", "selection", "mark_view", "mark_release")
STATUSES = ("final", "draft", "candidate", "accepted", "rejected")
AUTHOR_KINDS = ("mind", "helper", "host", "legacy_helper")
RELATIONS = ("members", "sources", "source_changes", "cited_by", "links")
ADDRESS_KINDS = ("record", "note", "file", "journal", "entry")

# Which query parameters each area accepts beside ``area``, ``cursor`` and ``limit``.
AREA_PARAMS: Dict[str, Tuple[str, ...]] = {
    "rooms": (), "story": (), "shelves": (), "documents": (),
    "records": ("room_id", "kinds", "status", "author_kind", "unfolded"),
    "marks": ("room_id", "state"),
    "shelf": ("scope", "view"),
    "history": ("address", "kinds"),
    "relations": ("address", "relation", "source"),
}
PARAMS: Dict[str, Tuple[str, ...]] = {
    "overview": (),
    "list": ("area", "cursor", "limit", "room_id", "kinds", "status", "author_kind", "unfolded", "state", "scope",
             "view", "address", "relation", "source"),
    "text": ("address", "revision", "start", "length"),
    "entries": ("ids",),
    "changes": ("sequence", "record_id", "limit"),
    "search": ("q", "cursor"),
    "inbound": ("scope", "topic", "cursor"),
}
REQUIRED: Dict[str, Tuple[str, ...]] = {"list": ("area",), "text": ("address",), "entries": ("ids",),
                                        "search": ("q",), "inbound": ("scope", "topic")}
OPS = tuple(PARAMS)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,199}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MAX_TEXT, MAX_QUERY, MAX_CURSOR, MAX_ADDRESS = 512, 200, 4096, 2048
MAX_LENGTH = 1_048_576


def _text(name: str, value: Any, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or _CONTROL.search(value) or "\n" in value:
        raise BadRequest(f"{name} must be 1 to {limit} printable characters", code="invalid_parameter")
    return value


def _ident(name: str, value: Any) -> str:
    """A record or room id as the chronicle stores it (any printable text): the core decides whether it exists."""
    return _text(name, value, 200)


def _room(value: Any) -> str:
    """A room id, or exactly ``""``: the host's room, whose records have no chat room. Leaving ``room_id`` out is
    every room; the empty id is that one room and never stands for every room."""
    return "" if value == "" else _ident("room_id", value)


def _whole(name: str, value: Any, low: int, high: Optional[int] = None) -> int:
    text = _text(name, value, 12)
    if not text.isdigit() or int(text) < low or (high is not None and int(text) > high):
        raise BadRequest(f"{name} must be a whole number from {low}" + (f" to {high}" if high else ""),
                         code="invalid_parameter")
    return int(text)


def _choices(name: str, value: Any, allowed: Tuple[str, ...]) -> List[str]:
    items = _text(name, value).split(",")
    if not all(item in allowed for item in items):
        raise BadRequest(f"{name} is a comma-separated list drawn from " + ", ".join(allowed), code="invalid_parameter")
    return items


def _choice(name: str, value: Any, allowed: Tuple[str, ...]) -> str:
    if value not in allowed:
        raise BadRequest(f"{name} must be one of " + ", ".join(allowed), code="invalid_parameter")
    return value


def _topic(value: Any) -> str:
    value = _text("topic", value)
    if value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/")):
        raise BadRequest("topic must be a relative path inside one shelf", code="invalid_parameter")
    return value


def _scope(value: Any) -> str:
    value = _text("scope", value, 220)
    if value != "global" and not (value.startswith("project:") and _ID.match(value[len("project:"):] or "-")):
        raise BadRequest("scope must be global or project:<project id>", code="invalid_parameter")
    return value


def _field(value: Any) -> List[Any]:
    if not isinstance(value, list) or len(value) > 32 or not all(
            (isinstance(key, str) and 0 < len(key) <= 200) or (type(key) is int and key >= 0) for key in value):
        raise BadRequest("an address field is a list of keys and list indices", code="invalid_parameter")
    return list(value)


def _address(value: Any) -> Dict[str, Any]:
    """One generic memory address (JSON in the query), checked field by field; the core checks it again."""
    try:
        raw = json.loads(_text("address", value, MAX_ADDRESS))
    except ValueError:
        raise BadRequest("address is a JSON object", code="invalid_parameter") from None
    if not isinstance(raw, dict) or raw.get("kind") not in ADDRESS_KINDS:
        raise BadRequest("address kind is one of " + ", ".join(ADDRESS_KINDS), code="invalid_parameter")
    kind = raw["kind"]
    allowed = {"record": ("id", "field"), "note": ("scope", "topic", "field"), "file": ("source",),
               "journal": ("journal",), "entry": ("journal", "offset", "field")}[kind]
    unknown = sorted(set(raw) - {"kind", *allowed})
    if unknown:
        raise BadRequest(f"unknown address field: {unknown[0]}", code="invalid_parameter")
    out: Dict[str, Any] = {"kind": kind}
    if kind == "record":
        out["id"] = _ident("address id", raw.get("id"))
    elif kind == "note":
        out.update(scope=_scope(raw.get("scope")), topic=_topic(raw.get("topic")))
    elif kind == "file":
        out["source"] = _text("address source", raw.get("source"), 220)
    else:
        out["journal"] = _text("address journal", raw.get("journal"), 240)
    if kind == "entry":
        offset = raw.get("offset")
        if type(offset) is not int or offset < 0:
            raise BadRequest("an entry offset is a whole number", code="invalid_parameter")
        out["offset"] = offset
        out["field"] = _field(raw.get("field", []))
    elif "field" in raw:
        out["field"] = _field(raw["field"])
    return out


def _cursor(value: Any) -> str:
    return _text("cursor", value, MAX_CURSOR)


def _list(query: Mapping[str, Any]) -> Dict[str, Any]:
    area = _choice("area", query.get("area"), AREAS)
    extra = sorted(set(query) - {"area", "cursor", "limit", *AREA_PARAMS[area]})
    if extra:
        raise BadRequest(f"{extra[0]} does not apply to area {area}", code="unknown_parameter")
    params: Dict[str, Any] = {}
    if "cursor" in query:
        params["cursor"] = _cursor(query["cursor"])
    if "limit" in query:
        params["limit"] = _whole("limit", query["limit"], 1, 500)
    if "room_id" in query:
        params["room_id"] = _room(query["room_id"])
    if "kinds" in query:
        params["kinds"] = _choices("kinds", query["kinds"], HISTORY_KINDS if area == "history" else RECORD_KINDS)
    if "status" in query:
        params["status"] = _choices("status", query["status"], STATUSES)
    if "author_kind" in query:
        params["author_kind"] = _choice("author_kind", query["author_kind"], AUTHOR_KINDS)
    if "unfolded" in query:
        params["unfolded"] = _choice("unfolded", query["unfolded"], ("true", "false")) == "true"
    if "state" in query:
        params["state"] = _choice("state", query["state"], ("active", "released", "all"))
    if "scope" in query:
        params["scope"] = _scope(query["scope"])
    if "view" in query:
        params["view"] = _choice("view", query["view"], ("active", "archived", "all"))
    if "address" in query:
        params["address"] = _address(query["address"])
    if "relation" in query:
        params["relation"] = _choice("relation", query["relation"], RELATIONS)
    if "source" in query:
        params["source"] = _whole("source", query["source"], 0)
    for name in {"shelf": ("scope",), "history": ("address",), "relations": ("address", "relation")}.get(area, ()):
        if name not in params:
            raise BadRequest(f"missing required parameter: {name}", code="missing_parameter")
    return {"area": area, **params}


def _validated(op: str, query: Mapping[str, Any]) -> Dict[str, Any]:
    unknown = sorted(set(query) - set(PARAMS[op]))
    if unknown:
        raise BadRequest(f"unknown parameter: {unknown[0]}", code="unknown_parameter")
    for name in REQUIRED.get(op, ()):
        if not query.get(name):
            raise BadRequest(f"missing required parameter: {name}", code="missing_parameter")
    if op == "list":
        return _list(query)
    if op == "text":
        params: Dict[str, Any] = {"address": _address(query["address"])}
        if "revision" in query:
            params["revision"] = _ident("revision", query["revision"])
        if "start" in query:
            params["start"] = _whole("start", query["start"], 0)
        if "length" in query:
            params["length"] = _whole("length", query["length"], 1, MAX_LENGTH)
        return params
    if op == "entries":
        try:
            ids = json.loads(_text("ids", query["ids"], 240 * ENTRIES_MAX))
        except ValueError:
            raise BadRequest("ids is a JSON list of record ids", code="invalid_parameter") from None
        if not isinstance(ids, list) or not 0 < len(ids) <= ENTRIES_MAX:
            raise BadRequest(f"ids is a JSON list of 1 to {ENTRIES_MAX} record ids", code="invalid_parameter")
        return {"ids": [_ident("ids", ident) for ident in ids]}
    if op == "changes":
        if ("sequence" in query) != ("record_id" in query):
            raise BadRequest("a change position names both sequence and record_id", code="invalid_parameter")
        out: Dict[str, Any] = {"position": None, "limit": None}
        if "sequence" in query:
            out["position"] = {"sequence": _whole("sequence", query["sequence"], 0),
                               "record_id": _ident("record_id", query["record_id"])}
        if "limit" in query:
            out["limit"] = _whole("limit", query["limit"], 1, 500)
        return out
    if op == "search":
        q = _text("q", query["q"], MAX_QUERY).strip()
        if not q:
            raise BadRequest("q must hold a character other than space", code="invalid_parameter")
        return {"q": q, "cursor": _cursor(query["cursor"]) if "cursor" in query else None}
    if op == "inbound":
        return {"scope": _scope(query["scope"]), "topic": _topic(query["topic"]),
                "cursor": _cursor(query["cursor"]) if "cursor" in query else None}
    return {}


def error_body(code: str, message: str, current_revision: Optional[str] = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if current_revision:
        error["current_revision"] = current_revision
    return {"contract": CONTRACT, "error": error}


def dispatch(reader: Any, op: str, query: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Answer one contract operation; never raises."""
    if op not in PARAMS:
        return 404, error_body("route_not_found", "route not found")
    try:
        q = _validated(op, query)
        if op == "overview":
            payload = reader.overview()
        elif op == "list":
            payload = reader.list(q.pop("area"), **q)
        elif op == "text":
            payload = reader.text(**q)
        elif op == "entries":
            payload = reader.entries(q["ids"])
        elif op == "changes":
            payload = reader.changes(q["position"], q["limit"])
        elif op == "search":
            payload = reader.search(q["q"], q["cursor"])
        else:
            payload = reader.inbound(q["scope"], q["topic"], q["cursor"])
    except ReaderError as exc:
        return exc.status, error_body(exc.code, exc.message, exc.current_revision)
    except Exception as exc:  # a reader fault is reported by kind, never as a traceback
        return 500, error_body("reader_failed", f"the memory reader failed ({type(exc).__name__})")
    body = {"contract": CONTRACT,
            "source": {"mode": str(getattr(reader, "mode", "")), "label": str(getattr(reader, "label", ""))}}
    body.update(payload)
    return 200, body
