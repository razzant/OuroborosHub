"""smart_lists — PluginAPI glue for the skill-local list store.

Agent tools and widget routes are thin adapters over ``lists_core.SmartLists``;
both read and write the same authoritative ``store.json`` in the skill state
directory. The skill has no network access and no side effects beyond that
file: nothing is purchased, ordered, scheduled or sent anywhere.

Widget routes always answer with a JSON object and HTTP 200. A refusal is
reported as ``ok: false`` plus a ``warning`` next to the refreshed view, because
a non-2xx status or a top-level ``error`` key makes the host replace the whole
widget state with the error and the lists would disappear from the card.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

try:  # Loaded as a package by the extension loader.
    from . import lists_core
except ImportError:  # Direct import (tests, tooling): resolve the sibling file.
    import importlib.util as _util

    _spec = _util.spec_from_file_location("smart_lists_lists_core", Path(__file__).resolve().parent / "lists_core.py")
    assert _spec is not None and _spec.loader is not None
    lists_core = _util.module_from_spec(_spec)
    _spec.loader.exec_module(lists_core)

ListError = lists_core.ListError

_SERVICE: Optional[Any] = None


def _service() -> Any:
    if _SERVICE is None:
        raise ListError("not_ready", "smart_lists is not registered yet")
    return _SERVICE


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value is True


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------

def _run_tool(args: Dict[str, Any], allowed: Iterable[str], call: Callable[[Dict[str, Any]], Dict[str, Any]]) -> str:
    allowed = tuple(allowed)
    try:
        unknown = sorted(set(args) - set(allowed))
        if unknown:
            raise ListError("invalid_input", f"unsupported arguments {unknown}; allowed: {list(allowed)}")
        payload = {"ok": True, **call(args)}
    except ListError as exc:
        payload = {"ok": False, "error": exc.as_dict()}
    return json.dumps(payload, ensure_ascii=False)


def tool_add(ctx: Any = None, **args: Any) -> str:
    def call(a: Dict[str, Any]) -> Dict[str, Any]:
        # Chat capture is where retries happen, so add never runs without a key.
        if not a.get("request_id"):
            raise ListError("invalid_input", "request_id is required for add; send a fresh id per owner request")
        return _service().add(a.get("group"), a.get("items"), request_id=a.get("request_id"))

    return _run_tool(args, ("group", "items", "request_id"), call)


def tool_read(ctx: Any = None, **args: Any) -> str:
    return _run_tool(args, ("group", "status", "subtree", "limit"), lambda a: _service().read(
        group=a.get("group", ""), status=a.get("status", "open"), subtree=a.get("subtree", True),
        limit=a.get("limit", 100)))


def tool_update(ctx: Any = None, **args: Any) -> str:
    return _run_tool(args, ("entry_id", "text", "due", "clear_due", "request_id"), lambda a: _service().update(
        a.get("entry_id"), text=a.get("text"), due=a.get("due"), clear_due=a.get("clear_due", False),
        request_id=a.get("request_id")))


def tool_complete(ctx: Any = None, **args: Any) -> str:
    return _run_tool(args, ("entry_ids", "done", "request_id"), lambda a: _service().complete(
        a.get("entry_ids"), done=a.get("done", True), request_id=a.get("request_id")))


def tool_move(ctx: Any = None, **args: Any) -> str:
    return _run_tool(args, ("entry_ids", "to_group", "request_id"), lambda a: _service().move(
        a.get("entry_ids"), a.get("to_group"), request_id=a.get("request_id")))


def tool_group(ctx: Any = None, **args: Any) -> str:
    return _run_tool(args, ("action", "group", "name", "parent", "request_id"), lambda a: _service().group(
        a.get("action"), group=a.get("group", ""), name=a.get("name", ""), parent=a.get("parent", ""),
        request_id=a.get("request_id")))


def tool_select(ctx: Any = None, **args: Any) -> str:
    return _run_tool(args, ("group",), lambda a: _service().select(a.get("group")))


_GROUP_REF = "Group id (g_...) or a case-insensitive path such as 'Home / Groceries'."
_ENTRY_IDS = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100,
              "description": "Entry ids (e_...) from add/read results."}
_REQUEST_ID = {
    "type": "string",
    "description": "Optional idempotency key (e.g. a UUID). A retry with the same id and arguments returns the "
                   "first result without applying the change again; the same id with different arguments is refused.",
}
_DUE = {
    "type": "string",
    "description": "Optional. Only an ISO-8601 date-time with an explicit offset (2026-10-01T18:00+03:00 or ...Z) "
                   "is recorded as an instant; any other wording (tomorrow, Friday, 2026-10-01) is stored as raw "
                   "text and never interpreted. No reminder is created.",
}

TOOLS = (
    (
        "add",
        tool_add,
        "Smart Lists: add entries to a list group when the owner clearly intends note capture, even without "
        "the word 'add' (e.g. 'oat milk and AA batteries for Home / Groceries'); never capture items from passing "
        "conversation. Put the owner's wording for each item verbatim in text, one entry per item, keeping "
        "quantities and notes inside text. Repeated text is never merged: it is stored again and listed under "
        "possible_duplicates so you can mention it. Use a fresh request_id per owner request and reuse it only "
        "to retry the same call. If the group is unknown, the error lists existing groups; create one with the "
        "group tool only when the owner asks. Nothing is purchased, ordered or scheduled.",
        {
            "type": "object",
            "properties": {
                "group": {"type": "string", "description": _GROUP_REF},
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "One item, verbatim."},
                            "due": _DUE,
                        },
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
                "request_id": {
                    "type": "string",
                    "description": "Required idempotency key for this owner request (e.g. a UUID). A retry with "
                                   "the same id returns the first result without adding the items again.",
                },
            },
            "required": ["group", "items", "request_id"],
            "additionalProperties": False,
        },
    ),
    (
        "read",
        tool_read,
        "Smart Lists: read the group tree with open/done counts and the entries of one group (with its sub-groups "
        "by default) or of all groups. Read-only.",
        {
            "type": "object",
            "properties": {
                "group": {"type": "string", "description": "Optional. " + _GROUP_REF + " Omit for all groups."},
                "status": {"type": "string", "enum": ["open", "done", "all"], "default": "open"},
                "subtree": {"type": "boolean", "default": True, "description": "Include descendant groups."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "additionalProperties": False,
        },
    ),
    (
        "update",
        tool_update,
        "Smart Lists: replace the text and/or due of one entry when the owner asks to change it. New text is "
        "stored verbatim.",
        {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string", "description": "Entry id (e_...)."},
                "text": {"type": "string", "description": "New text, verbatim. Omit to keep."},
                "due": _DUE,
                "clear_due": {"type": "boolean", "default": False},
                "request_id": _REQUEST_ID,
            },
            "required": ["entry_id"],
            "additionalProperties": False,
        },
    ),
    (
        "complete",
        tool_complete,
        "Smart Lists: mark entries done (e.g. the owner says they bought or finished them), or reopen them with "
        "done=false.",
        {
            "type": "object",
            "properties": {
                "entry_ids": _ENTRY_IDS,
                "done": {"type": "boolean", "default": True},
                "request_id": _REQUEST_ID,
            },
            "required": ["entry_ids"],
            "additionalProperties": False,
        },
    ),
    (
        "move",
        tool_move,
        "Smart Lists: move entries to another group. An entry belongs to exactly one group.",
        {
            "type": "object",
            "properties": {
                "entry_ids": _ENTRY_IDS,
                "to_group": {"type": "string", "description": _GROUP_REF},
                "request_id": _REQUEST_ID,
            },
            "required": ["entry_ids", "to_group"],
            "additionalProperties": False,
        },
    ),
    (
        "group",
        tool_group,
        "Smart Lists: edit the group tree when the owner asks. create needs name (parent optional, blank = top "
        "level); rename needs group and name; move needs group and parent (blank = top level, never under its own "
        "descendant); delete needs group and only removes an empty group. Names are unique among siblings and "
        "cannot contain '/'.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(lists_core.GROUP_ACTIONS)},
                "group": {"type": "string", "description": _GROUP_REF},
                "name": {"type": "string"},
                "parent": {"type": "string", "description": "Parent group for create/move. " + _GROUP_REF},
                "request_id": _REQUEST_ID,
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    ),
    (
        "select",
        tool_select,
        "Smart Lists: read-only selection of the open entries in a group and all of its sub-groups, in tree order, "
        "for example to prepare a shopping run. Returns at most 500 entries with total and truncated fields. "
        "It never buys, orders, reminds or changes anything.",
        {
            "type": "object",
            "properties": {"group": {"type": "string", "description": _GROUP_REF}},
            "required": ["group"],
            "additionalProperties": False,
        },
    ),
)


# ---------------------------------------------------------------------------
# Widget routes
# ---------------------------------------------------------------------------

def _entry_rows(entries: Iterable[Dict[str, Any]], *, completed: bool = False) -> list:
    rows = []
    for entry in entries:
        row = {"id": entry["id"], "item": entry["text"], "group": entry["group_path"],
               "due": (entry.get("due") or {}).get("raw", "")}
        if completed:
            row["completed"] = entry.get("completed_at") or ""
        rows.append(row)
    return rows


def widget_view(*, notice: str = "", warning: str = "") -> Dict[str, Any]:
    try:
        data = _service().overview()
    except ListError as exc:
        return {"ok": False, "notice": "", "warning": exc.message, "stats": {}, "tree_rows": [],
                "open_rows": [], "done_rows": [], "open_note": "", "empty_hint": ""}
    shown = len(data["open"])
    return {
        "ok": not warning,
        "notice": notice,
        "warning": warning,
        "stats": {"groups": len(data["tree"]), "open": data["open_total"], "done": data["done_total"]},
        "tree_rows": [{"group": row["path"], "id": row["id"], "open": row["open"], "done": row["done"]}
                      for row in data["tree"]],
        "open_rows": _entry_rows(data["open"]),
        "done_rows": _entry_rows(data["done"], completed=True),
        "open_note": (f"Showing the first {shown} of {data['open_total']} open entries; use the Subtree tab "
                      "for one group." if data["open_total"] > shown else ""),
        "empty_hint": ("No groups yet. Create one under Edit groups (for example 'Home', then 'Groceries' with "
                       "parent 'Home'), or ask the agent to start a list." if not data["tree"] else ""),
    }


def _describe(result: Dict[str, Any]) -> str:
    op = result.get("op", "")
    if op == "add":
        count = len(result["added"])
        text = f"Added {count} {'entry' if count == 1 else 'entries'} to {result['group']['path']}."
        if result.get("possible_duplicates"):
            text += " The same text is already open there; both were kept."
    elif op == "edit":
        entry = result["entry"]
        text = (f"Updated {entry['id']} ({', '.join(result['changed'])})." if result["changed"]
                else f"No change to {entry['id']}.")
    elif op == "group.delete":
        # A replay after deletion has only the retired id: the journal does not
        # retain private old group names or paths.
        text = f"Deleted empty group {result['deleted'].get('path') or result['deleted']['id']}."
    elif op.startswith("group."):
        verb = {"group.create": "Created", "group.rename": "Renamed", "group.move": "Moved"}.get(op, "Updated")
        group = result["group"]
        text = f"{verb} group {group.get('path') or group['id']} ({group['id']})."
    else:
        text = "Done."
    if result.get("replayed"):
        text += " (Repeated request: nothing new was applied.)"
    return text


def _widget_mutation(call: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        result = call(_service())
    except ListError as exc:
        return widget_view(warning=exc.message)
    return widget_view(notice=_describe(result))


def widget_selection(group: Any) -> Dict[str, Any]:
    try:
        result = _service().select(group)
    except ListError as exc:
        return {"ok": False, "warning": exc.message, "scope": "", "count": 0, "total": 0,
                "truncated": False, "rows": []}
    rows = [{"id": entry["entry_id"], "item": entry["text"], "group": entry["group_path"],
             "due": (entry.get("due") or {}).get("raw", "")} for entry in result["entries"]]
    warning = (f"Showing the first {result['count']} of {result['total']} open entries."
               if result["truncated"] else "")
    return {"ok": True, "warning": warning, "read_only": True, "scope": result["scope"]["path"],
            "count": result["count"], "total": result["total"], "truncated": result["truncated"],
            "rows": rows}


async def _json_body(request: Any) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _query(request: Any, name: str) -> str:
    try:
        return str(request.query_params.get(name) or "")
    except Exception:
        return ""


async def route_view(request: Any) -> Dict[str, Any]:
    return await asyncio.to_thread(widget_view)


async def route_edit(request: Any) -> Dict[str, Any]:
    body = await _json_body(request)
    return await asyncio.to_thread(_widget_mutation, lambda service: service.edit(
        body.get("entry_id"), text=body.get("text") or "", due=body.get("due") or "",
        clear_due=_truthy(body.get("clear_due")), status=body.get("status") or "",
        move_to=body.get("move_to") or "", request_id=body.get("request_id") or ""))


async def route_group(request: Any) -> Dict[str, Any]:
    body = await _json_body(request)
    return await asyncio.to_thread(_widget_mutation, lambda service: service.group(
        body.get("action") or "create", group=body.get("group") or "", name=body.get("name") or "",
        parent=body.get("parent") or "", request_id=body.get("request_id") or ""))


async def route_select(request: Any) -> Dict[str, Any]:
    return await asyncio.to_thread(widget_selection, _query(request, "group"))


ROUTES = (
    ("view", route_view, ("GET",)),
    ("edit", route_edit, ("POST",)),
    ("group", route_group, ("POST",)),
    ("select", route_select, ("GET",)),
)


# Mirrored verbatim in SKILL.md ``ui_tab.render``; a test keeps the two equal.
WIDGET_RENDER: Dict[str, Any] = {
    "kind": "declarative",
    "schema_version": 1,
    "span": 2,
    "components": [
        {"type": "poll", "route": "view", "method": "GET", "target": "lists", "auto_start": True,
         "interval_ms": 30000, "max_ticks": 100, "label": "Refresh lists", "busy_label": "Refreshing…"},
        {"type": "callout", "target": "lists", "tone": "success", "path": "notice", "condition_key": "notice"},
        {"type": "callout", "target": "lists", "tone": "warning", "path": "warning", "condition_key": "warning"},
        {"type": "callout", "target": "lists", "tone": "danger", "path": "error", "condition_key": "error"},
        {"type": "callout", "target": "lists", "tone": "info", "path": "empty_hint", "condition_key": "empty_hint"},
        {"type": "group", "layout": "cluster", "target": "lists", "components": [
            {"type": "metric", "target": "lists", "label": "Groups", "path": "stats.groups"},
            {"type": "metric", "target": "lists", "label": "Open", "path": "stats.open"},
            {"type": "metric", "target": "lists", "label": "Done", "path": "stats.done"},
        ]},
        {"type": "tabs", "target": "lists", "tabs": [
            {"label": "Open", "components": [
                {"type": "table", "target": "lists", "path": "open_rows", "columns": [
                    {"label": "Item", "path": "item"},
                    {"label": "Group", "path": "group"},
                    {"label": "Due", "path": "due"},
                    {"label": "ID", "path": "id"},
                ]},
                {"type": "callout", "target": "lists", "tone": "info", "path": "open_note",
                 "condition_key": "open_note"},
            ]},
            {"label": "Groups", "components": [
                {"type": "table", "target": "lists", "path": "tree_rows", "columns": [
                    {"label": "Group", "path": "group"},
                    {"label": "Open", "path": "open", "presentation": "number"},
                    {"label": "Done", "path": "done", "presentation": "number"},
                    {"label": "ID", "path": "id"},
                ]},
            ]},
            {"label": "Done", "components": [
                {"type": "table", "target": "lists", "path": "done_rows", "columns": [
                    {"label": "Item", "path": "item"},
                    {"label": "Group", "path": "group"},
                    {"label": "Completed (UTC)", "path": "completed"},
                    {"label": "ID", "path": "id"},
                ]},
            ]},
        ]},
        {"type": "tabs", "target": "lists", "tabs": [
            {"label": "Edit entry", "components": [
                {"type": "form", "route": "edit", "method": "POST", "target": "lists", "title": "Edit one entry",
                 "submit_label": "Apply", "busy_label": "Saving…", "columns": 2, "fields": [
                     {"name": "entry_id", "label": "Entry ID", "type": "text", "required": True,
                      "placeholder": "e_…"},
                     {"name": "status", "label": "Status", "type": "select", "options": [
                         {"value": "", "label": "Keep"},
                         {"value": "done", "label": "Mark done"},
                         {"value": "open", "label": "Reopen"},
                     ]},
                     {"name": "text", "label": "New text", "type": "text", "span": 2,
                      "placeholder": "Leave blank to keep"},
                     {"name": "due", "label": "New due", "type": "text", "placeholder": "Leave blank to keep"},
                     {"name": "move_to", "label": "Move to group", "type": "text",
                      "placeholder": "Leave blank to keep"},
                     {"name": "clear_due", "label": "Clear due", "type": "checkbox"},
                 ]},
            ]},
            {"label": "Edit groups", "components": [
                {"type": "form", "route": "group", "method": "POST", "target": "lists",
                 "title": "Create, rename, move or delete a group", "submit_label": "Apply",
                 "busy_label": "Saving…", "columns": 2, "fields": [
                     {"name": "action", "label": "Action", "type": "select", "options": [
                         {"value": "create", "label": "Create"},
                         {"value": "rename", "label": "Rename"},
                         {"value": "move", "label": "Move"},
                         {"value": "delete", "label": "Delete (empty only)"},
                     ]},
                     {"name": "group", "label": "Group", "type": "text",
                      "placeholder": "Existing group (rename, move, delete)"},
                     {"name": "name", "label": "Name", "type": "text", "placeholder": "New name (create, rename)"},
                     {"name": "parent", "label": "Parent", "type": "text",
                      "placeholder": "Blank = top level (create, move)"},
                 ]},
            ]},
            {"label": "Subtree", "components": [
                {"type": "form", "route": "select", "method": "GET", "target": "selection",
                 "title": "Open entries in a group and its sub-groups (read-only)", "submit_label": "Show",
                 "busy_label": "Reading…", "fields": [
                     {"name": "group", "label": "Group", "type": "text", "required": True, "placeholder": "Home"},
                 ]},
                {"type": "callout", "target": "selection", "tone": "warning", "path": "warning",
                 "condition_key": "warning"},
                {"type": "callout", "target": "selection", "tone": "danger", "path": "error",
                 "condition_key": "error"},
                {"type": "kv", "target": "selection", "condition_key": "ok", "fields": [
                    {"label": "Scope", "path": "scope"},
                    {"label": "Shown open entries", "path": "count"},
                    {"label": "Total open entries", "path": "total"},
                ]},
                {"type": "table", "target": "selection", "condition_key": "ok", "path": "rows", "columns": [
                    {"label": "Item", "path": "item"},
                    {"label": "Group", "path": "group"},
                    {"label": "Due", "path": "due"},
                    {"label": "ID", "path": "id"},
                ]},
            ]},
        ]},
    ],
}


def register(api: Any) -> None:
    """PluginAPI entry point: one store, seven tools, four routes, one widget."""
    global _SERVICE
    _SERVICE = lists_core.SmartLists(Path(api.get_state_dir()))
    for name, handler, description, schema in TOOLS:
        api.register_tool(name, handler, description=description, schema=schema, timeout_sec=15)
    for path, handler, methods in ROUTES:
        api.register_route(path, handler, methods=methods)
    api.register_ui_tab("lists", "Smart Lists", icon="📋", render=WIDGET_RENDER)
    api.log("info", "smart_lists registered: local store, 7 tools, 4 routes, declarative widget")


__all__ = ["register"]
