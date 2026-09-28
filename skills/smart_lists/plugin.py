"""smart_lists — PluginAPI glue for the skill-local list store.

Agent tools and widget routes are thin adapters over ``lists_core.SmartLists``;
both read and write the same authoritative ``store.json`` in the skill state
directory (plus its content-free sentinel and the ``backups/`` folder that
exports and pre-restore copies go to). The skill has no network access and no
other side effects: nothing is purchased, ordered, scheduled or sent anywhere.

Widget forms use their own result targets, so a top-level ``error`` on refusal
makes the form fail visibly without replacing the lists table. The poll refreshes
the table after a successful mutation. The ``export`` download raises on refusal, so the host's
HTTP error makes the download fail visibly instead of saving a file that is not
an export. The download button passes ``?filename=smart-lists-export.json``
because the host names a widget download after that query parameter (else
after the last URL path segment, here ``export``); the route ignores it.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

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

MAX_TOOL_RESULT_BYTES = 14500  # Leave room below the host's 15k result cap.
_TRIMMABLE = ("possible_duplicates", "groups", "groups_included", "entries", "added", "changed",
              "moved", "deleted", "undeleted", "erased", "unchanged", "backups")
_KEEP_ONE = {"entries", "added", "changed", "moved", "deleted", "undeleted", "erased"}


def _tool_json(payload: Dict[str, Any]) -> str:
    """Return whole JSON under the host cap, with explicit omitted-row counts."""
    result = copy.deepcopy(payload)

    def encode() -> str:
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    rendered = encode()
    if len(rendered.encode("utf-8")) <= MAX_TOOL_RESULT_BYTES:
        return rendered
    counts = {key: {"total": len(result[key]), "shown": len(result[key]), "truncated": False}
              for key in _TRIMMABLE if isinstance(result.get(key), list)}
    result["result_counts"] = counts
    result["output_truncated"] = True
    result["truncated_fields"] = []
    while (size := len(encode().encode("utf-8"))) > MAX_TOOL_RESULT_BYTES:
        key = next((name for name in _TRIMMABLE if isinstance(result.get(name), list) and result[name]
                    and (name not in _KEEP_ONE or len(result[name]) > 1)), None)
        if key is not None:
            rows = result[key]
            average = max(1, len(json.dumps(rows, ensure_ascii=False).encode("utf-8")) // len(rows))
            drop = min(len(rows) - int(key in _KEEP_ONE),
                       max(1, (size - MAX_TOOL_RESULT_BYTES + average - 1) // average))
            del rows[-drop:]
            counts[key]["shown"] -= drop
            counts[key]["truncated"] = True
            if key == "entries" and "offset" in result:
                result["next_offset"] = result["offset"] + len(result["entries"])
                result["truncated"] = result["next_offset"] < result["total"]
                if "count" in result:
                    result["count"] = len(result["entries"])
            if key == "groups":
                result["groups_truncated"] = True
            continue
        # A path can contain hundreds of group names. Bound scalar values too.
        strings = []

        def visit(node: Any, path: str = "") -> None:
            if isinstance(node, dict):
                for field, value in node.items():
                    if isinstance(value, str) and field not in {"id", "op", "sha256", "fingerprint"} and len(value) > 128:
                        strings.append((node, field, value, f"{path}.{field}" if path else field))
                    elif isinstance(value, (dict, list)) and field not in {"result_counts", "truncated_fields"}:
                        visit(value, f"{path}.{field}" if path else field)
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    if isinstance(value, (dict, list)):
                        visit(value, f"{path}[{index}]")

        visit(result)
        if not strings:
            break
        parent, field, value, path = max(strings, key=lambda pair: len(pair[2].encode("utf-8")))
        parent[field] = value[:max(64, len(value) // 2)] + "…"
        if path not in result["truncated_fields"]:
            result["truncated_fields"].append(path)
    return encode()


def _run_tool(args: Dict[str, Any], allowed: Iterable[str], call: Callable[[Dict[str, Any]], Dict[str, Any]]) -> str:
    allowed = tuple(allowed)
    try:
        unknown = sorted(set(args) - set(allowed))
        if unknown:
            raise ListError("invalid_input", f"unsupported arguments {unknown}; allowed: {list(allowed)}")
        payload = {"ok": True, **call(args)}
    except ListError as exc:
        payload = {"ok": False, "error": exc.as_dict()}
    return _tool_json(payload)


def tool_add(ctx: Any = None, **args: Any) -> str:
    def call(a: Dict[str, Any]) -> Dict[str, Any]:
        # Chat capture is where retries happen, so add never runs without a key.
        if not a.get("request_id"):
            raise ListError("invalid_input", "request_id is required for add; send a fresh id per owner request")
        return _service().add(a.get("group"), a.get("items"), request_id=a.get("request_id"))

    return _run_tool(args, ("group", "items", "request_id"), call)


def tool_read(ctx: Any = None, **args: Any) -> str:
    return _run_tool(args, ("group", "status", "subtree", "limit", "offset"), lambda a: _service().read(
        group=a.get("group", ""), status=a.get("status", "open"), subtree=a.get("subtree", True),
        limit=a.get("limit", 100), offset=a.get("offset", 0)))


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
    return _run_tool(args, ("group", "limit", "offset"), lambda a: _service().select(
        a.get("group"), limit=a.get("limit", lists_core.MAX_READ_LIMIT), offset=a.get("offset", 0)))


def tool_delete(ctx: Any = None, **args: Any) -> str:
    return _run_tool(args, ("entry_ids", "action", "request_id"), lambda a: _service().delete(
        a.get("entry_ids"), action=a.get("action", "delete"), request_id=a.get("request_id")))


def tool_store(ctx: Any = None, **args: Any) -> str:
    def call(a: Dict[str, Any]) -> Dict[str, Any]:
        action, service = a.get("action"), _service()
        if action == "status":
            return service.status()
        if action == "init":
            return service.init(replace=a.get("replace", False))
        if action == "export":
            return service.export()
        if action == "restore":
            return service.restore(file=a.get("file"), replace=a.get("replace", False))
        raise ListError("invalid_input", f"action must be one of {list(lists_core.STORE_ACTIONS)}")

    return _run_tool(args, ("action", "file", "replace"), call)


_GROUP_REF = "Group id (g_...) or a case-insensitive path such as 'Home / Groceries'."
_ENTRY_IDS = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100,
              "description": "Entry ids (e_...) from add/read results."}
_REQUEST_ID = {
    "type": "string",
    "description": "Optional idempotency key (e.g. a UUID). A retry with the same id and arguments returns the "
                   "first result without applying the change again; the same id with different arguments is refused "
                   "(request_conflict), and so is an id retired from the replay journal (request_expired; rarely a "
                   "new id collides with one, then nothing was applied and a new id should be used).",
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
        f"possible_duplicates (at most {lists_core.MAX_DUPLICATE_IDS} matching ids per new entry, with total and "
        "truncated) so you can mention it. Use a fresh request_id per owner request and reuse it only to retry the "
        "same call. If the group is unknown, the error lists existing groups; create one with the "
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
                                   "the same id returns the first result without adding the items again. "
                                   "request_expired means nothing was added: read the list, and if the items "
                                   "are really missing, add them with a new id.",
                },
            },
            "required": ["group", "items", "request_id"],
            "additionalProperties": False,
        },
    ),
    (
        "read",
        tool_read,
        "Smart Lists: read the group tree with open/done/deleted counts and the entries of one group (with its "
        "sub-groups by default) or of all groups. status=all means open and done; deleted lists the trash. "
        "Read-only.",
        {
            "type": "object",
            "properties": {
                "group": {"type": "string", "description": "Optional. " + _GROUP_REF + " Omit for all groups."},
                "status": {"type": "string", "enum": ["open", "done", "deleted", "all"], "default": "open"},
                "subtree": {"type": "boolean", "default": True, "description": "Include descendant groups."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "offset": {"type": "integer", "minimum": 0, "maximum": 5000, "default": 0},
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
        "descendant); delete needs group and only removes an empty group (no entries, deleted ones included). "
        "Names are unique among siblings and cannot contain '/'.",
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
        "for example to prepare a shopping run. Returns at most 500 entries with total, truncated and "
        "next_offset; pass next_offset as offset to fetch the next page. "
        "It never buys, orders, reminds or changes anything.",
        {
            "type": "object",
            "properties": {"group": {"type": "string", "description": _GROUP_REF},
                           "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 500},
                           "offset": {"type": "integer", "minimum": 0, "maximum": 5000, "default": 0}},
            "required": ["group"],
            "additionalProperties": False,
        },
    ),
    (
        "delete",
        tool_delete,
        "Smart Lists: delete entries the owner wants gone (a wrong or unwanted item; use complete for finished "
        "ones). action=delete moves them to the trash, where they are hidden but kept; undo brings them back with "
        "their status and group; erase permanently removes entries that are already deleted and cannot be undone. "
        "Deleted entries cannot be edited, completed or moved until undone.",
        {
            "type": "object",
            "properties": {
                "entry_ids": _ENTRY_IDS,
                "action": {"type": "string", "enum": list(lists_core.ENTRY_ACTIONS), "default": "delete"},
                "request_id": _REQUEST_ID,
            },
            "required": ["entry_ids"],
            "additionalProperties": False,
        },
    ),
    (
        "store",
        tool_store,
        "Smart Lists: the whole store. status: state (ready, uninitialized, missing, unreadable or mismatch), "
        "counts, last export (export_unrecorded when one could not be recorded), replay-journal figures and files "
        "in the backups folder. init: start an empty store; a tool returning "
        "store_not_initialized means this installation has none, so first ask the owner whether they have an "
        "export to restore. export: write a complete, checksummed JSON export into the backups folder and return "
        "its path (export_recorded=false plus a warning if only recording it in the sentinel failed; the file is "
        "still complete); uninstalling the skill or reinstalling Ouroboros deletes that folder, so offer to copy "
        "the file somewhere durable (the widget's Download button saves one to Downloads). restore: load an export (file = "
        "absolute path, or a file name from status backups). An existing store is replaced only with replace=true, "
        "after being copied to the backups folder; without it the refusal shows what would be replaced.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(lists_core.STORE_ACTIONS)},
                "file": {"type": "string", "description": "restore: absolute path of a .json export, or a bare "
                                                          "file name from the backups folder."},
                "replace": {"type": "boolean", "default": False,
                            "description": "init/restore: replace an existing (or missing) store; the current file "
                                           "is copied to the backups folder first. Only when the owner confirms."},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    ),
)


# ---------------------------------------------------------------------------
# Widget routes
# ---------------------------------------------------------------------------

def _entry_rows(entries: Iterable[Dict[str, Any]], stamp: Optional[Tuple[str, str]] = None) -> list:
    rows = []
    for entry in entries:
        row = {"id": entry["id"], "item": entry["text"], "group": entry["group_path"],
               "due": (entry.get("due") or {}).get("raw", "")}
        if stamp:
            row[stamp[0]] = entry.get(stamp[1]) or ""
        rows.append(row)
    return rows


# Owner-facing wording for the widget; the tools return the agent-facing message.
_STATE_HINTS = {
    "uninitialized": "No list store exists here yet. If you had lists before (for example before a reinstall), "
                     "paste your export in the Store tab and choose Restore; otherwise choose Start a new empty "
                     "store there.",
    "missing": "The list store file is missing although lists were saved here before. Nothing was replaced with "
               "an empty list. Restore an export in the Store tab, or start over there with Replace checked.",
    "mismatch": "The list store file is older than, or different from, the last saved version, so it was left "
                "untouched. Restore the export you want in the Store tab with Replace checked (the current file "
                "is backed up first).",
    "unreadable": "The list store file could not be read and was left untouched. Restore an export in the Store "
                  "tab with Replace checked (the damaged file is backed up first).",
}


def _store_view(info: Dict[str, Any]) -> Dict[str, Any]:
    state, counts = info["state"], info.get("counts")
    last_export = info.get("last_export") or {}
    if last_export.get("at") and info.get("generation") is not None:
        since = max(0, info["generation"] - last_export["generation"])
        export_text = f"{last_export['at']} ({since} {'change' if since == 1 else 'changes'} since)"
    else:
        export_text = "Never"
    unrecorded = info.get("export_unrecorded")
    export_warning = lists_core.export_unrecorded_warning(unrecorded) if unrecorded else ""
    if unrecorded:
        export_text += f"; a later export at {unrecorded['at']} was delivered but not recorded"
    hint = _STATE_HINTS.get(state, "")
    if state == "ready":
        hint = ("The lists and in-folder exports disappear together on uninstall or reinstall. Download a full "
                "export after changes and save it outside the Ouroboros data folder. Last export only records "
                "creation here, not that you saved a durable copy.")
    return {
        "state": state,
        "contents": ("{groups} groups, {entries} entries ({open} open, {done} done, {deleted} deleted)".format(**counts)
                     if counts else "—"),
        "last_written": info.get("last_written_at") or "—",
        "last_export": export_text,
        "backups": f"{info.get('backups_total', 0)} file(s) in {info.get('backup_dir', '')}",
        "exportable": state == "ready",
        "hint": hint,
        "export_warning": export_warning,
    }


def widget_view(*, notice: str = "", warning: str = "") -> Dict[str, Any]:
    try:
        store = _store_view(_service().status())  # a missing or unreadable store is a state here, not an error
    except ListError as exc:
        store = {"state": exc.code, "exportable": False, "hint": exc.message}
    try:
        data = _service().overview()
    except ListError as exc:
        return {"ok": False, "notice": notice, "warning": warning or _STATE_HINTS.get(store["state"], exc.message),
                "store": store, "stats": {}, "tree_rows": [], "open_rows": [], "done_rows": [], "deleted_rows": [],
                "open_note": "", "empty_hint": ""}
    shown = len(data["open"])
    return {
        "ok": not warning,  # a refused change; an unrecorded export is reported but refused nothing
        "notice": notice,
        "warning": warning or store.get("export_warning", ""),
        "store": store,
        "stats": {"groups": len(data["tree"]), "open": data["open_total"], "done": data["done_total"],
                  "deleted": data["deleted_total"]},
        "tree_rows": [{"group": row["path"], "id": row["id"], "open": row["open"], "done": row["done"],
                       "deleted": row["deleted"]} for row in data["tree"]],
        "open_rows": _entry_rows(data["open"]),
        "done_rows": _entry_rows(data["done"], ("completed", "completed_at")),
        "deleted_rows": _entry_rows(data["deleted"], ("deleted", "deleted_at")),
        "open_note": (f"Showing the first {shown} of {data['open_total']} open entries; use the Subtree tab "
                      "for one group." if data["open_total"] > shown else ""),
        "empty_hint": ("No groups yet. Create one under Edit groups (for example 'Home', then 'Groceries' with "
                       "parent 'Home'), or ask the agent to start a list." if not data["tree"] else ""),
    }


def _describe(result: Dict[str, Any]) -> str:
    op = result.get("op", "")
    if op == "add":
        count = len(result["added"])
        group = result["group"]
        text = f"Added {count} {'entry' if count == 1 else 'entries'} to {group.get('path') or group['id']}."
        if result.get("possible_duplicates"):
            text += " The same text is already open there; both were kept."
    elif op == "edit":
        entry = result["entry"]
        text = (f"Updated {entry['id']} ({', '.join(result['changed'])})." if result["changed"]
                else f"No change to {entry['id']}.")
    elif op in lists_core.ENTRY_ACTIONS:
        ids = ", ".join(entry["id"] for entry in result[lists_core._ACTION_RESULT_KEYS[op]]) or "nothing"
        text = {"delete": f"Deleted {ids} (kept in the Deleted tab; choose Undo delete to bring it back).",
                "undo": f"Brought back {ids}.",
                "erase": f"Erased {ids} permanently."}[op]
        if result.get("unchanged"):
            text += f" Already in that state: {', '.join(result['unchanged'])}."
    elif op in ("init", "restore"):
        counts = result["counts"]
        text = ("Started a new empty list store." if op == "init" else
                f"Restored {counts['groups']} groups and {counts['entries']} entries.")
        if result.get("backup"):
            text += f" The previous store file was copied to {result['backup']}."
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
        return {**widget_view(warning=exc.message), "error": exc.message}
    return widget_view(notice=_describe(result))


def widget_selection(group: Any) -> Dict[str, Any]:
    try:
        result = _service().select(group)
    except ListError as exc:
        return {"ok": False, "error": exc.message, "warning": exc.message, "scope": "", "count": 0, "total": 0,
                "truncated": False, "rows": []}
    rows = [{"id": entry["entry_id"], "item": entry["text"], "group": entry["group_path"],
             "due": (entry.get("due") or {}).get("raw", "")} for entry in result["entries"]]
    warning = (f"Showing the first {result['count']} of {result['total']} open entries."
               if result["truncated"] else "")
    return {"ok": True, "warning": warning, "read_only": True, "scope": result["scope"]["path"],
            "count": result["count"], "total": result["total"], "truncated": result["truncated"],
            "rows": rows}


def widget_store(body: Dict[str, Any]) -> Dict[str, Any]:
    action, pasted = body.get("action") or "restore", body.get("export_json") or ""
    replace = _truthy(body.get("replace"))

    def call(service: Any) -> Dict[str, Any]:
        if action == "init":
            if pasted:
                raise ListError("invalid_input", "Starting a new empty store takes no export text; clear it or "
                                                 "choose Restore.")
            return service.init(replace=replace)
        if action == "restore":
            if not pasted:
                raise ListError("invalid_input", "Paste the contents of a Smart Lists export file first.")
            return service.restore(text=pasted, replace=replace)
        raise ListError("invalid_input", "action must be init or restore")

    return _widget_mutation(call)


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


async def route_store(request: Any) -> Dict[str, Any]:
    body = await _json_body(request)
    return await asyncio.to_thread(widget_store, body)


async def route_export(request: Any) -> Dict[str, Any]:
    # The complete export document is the response body the widget downloads.
    return await asyncio.to_thread(lambda: _service().export_document())


ROUTES = (
    ("view", route_view, ("GET",)),
    ("edit", route_edit, ("POST",)),
    ("group", route_group, ("POST",)),
    ("select", route_select, ("GET",)),
    ("store", route_store, ("POST",)),
    ("export", route_export, ("GET",)),
)


EXPORT_FILENAME = "smart-lists-export.json"

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
            {"type": "metric", "target": "lists", "label": "Deleted", "path": "stats.deleted"},
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
                    {"label": "Deleted", "path": "deleted", "presentation": "number"},
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
            {"label": "Deleted", "components": [
                {"type": "table", "target": "lists", "path": "deleted_rows", "columns": [
                    {"label": "Item", "path": "item"},
                    {"label": "Group", "path": "group"},
                    {"label": "Deleted (UTC)", "path": "deleted"},
                    {"label": "ID", "path": "id"},
                ]},
            ]},
        ]},
        {"type": "tabs", "target": "lists", "tabs": [
            {"label": "Edit entry", "components": [
                {"type": "form", "route": "edit", "method": "POST", "target": "edit_result", "title": "Edit one entry",
                 "submit_label": "Apply", "busy_label": "Saving…", "columns": 2, "fields": [
                     {"name": "entry_id", "label": "Entry ID", "type": "text", "required": True,
                      "placeholder": "e_…"},
                     {"name": "status", "label": "Status", "type": "select", "options": [
                         {"value": "", "label": "Keep"},
                         {"value": "done", "label": "Mark done"},
                         {"value": "open", "label": "Reopen"},
                         {"value": "delete", "label": "Delete (can be undone)"},
                         {"value": "undo", "label": "Undo delete"},
                         {"value": "erase", "label": "Erase deleted entry for good"},
                     ]},
                     {"name": "text", "label": "New text", "type": "text", "span": 2,
                      "placeholder": "Leave blank to keep"},
                     {"name": "due", "label": "New due", "type": "text", "placeholder": "Leave blank to keep"},
                     {"name": "move_to", "label": "Move to group", "type": "text",
                      "placeholder": "Leave blank to keep"},
                     {"name": "clear_due", "label": "Clear due", "type": "checkbox"},
                 ]},
                {"type": "callout", "target": "edit_result", "tone": "danger", "path": "error",
                 "condition_key": "error"},
                {"type": "callout", "target": "edit_result", "tone": "success", "path": "notice",
                 "condition_key": "notice"},
            ]},
            {"label": "Edit groups", "components": [
                {"type": "form", "route": "group", "method": "POST", "target": "group_result",
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
                {"type": "callout", "target": "group_result", "tone": "danger", "path": "error",
                 "condition_key": "error"},
                {"type": "callout", "target": "group_result", "tone": "success", "path": "notice",
                 "condition_key": "notice"},
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
            {"label": "Store", "components": [
                {"type": "callout", "target": "lists", "tone": "warning", "path": "store.export_warning",
                 "condition_key": "store.export_warning"},
                {"type": "callout", "target": "lists", "tone": "info", "path": "store.hint",
                 "condition_key": "store.hint"},
                {"type": "kv", "target": "lists", "fields": [
                    {"label": "State", "path": "store.state"},
                    {"label": "Contents", "path": "store.contents"},
                    {"label": "Last change (UTC)", "path": "store.last_written"},
                    {"label": "Last export (UTC)", "path": "store.last_export"},
                    {"label": "Backups", "path": "store.backups"},
                ]},
                {"type": "file", "target": "lists", "route": "export", "query": {"filename": EXPORT_FILENAME},
                 "condition_key": "store.exportable", "label": "Download full export (JSON)",
                 "filename": EXPORT_FILENAME},
                {"type": "form", "route": "store", "method": "POST", "target": "store_result",
                 "title": "Restore an export or start a new store", "submit_label": "Apply",
                 "busy_label": "Working…", "fields": [
                     {"name": "action", "label": "Action", "type": "select", "options": [
                         {"value": "restore", "label": "Restore pasted export"},
                         {"value": "init", "label": "Start a new empty store"},
                     ]},
                     {"name": "export_json", "label": "Export", "type": "textarea",
                      "placeholder": "Paste the whole content of a Smart Lists export file"},
                     {"name": "replace", "label": "Replace the current store (it is copied to Backups first)",
                      "type": "checkbox"},
                 ]},
                {"type": "callout", "target": "store_result", "tone": "danger", "path": "error",
                 "condition_key": "error"},
                {"type": "callout", "target": "store_result", "tone": "success", "path": "notice",
                 "condition_key": "notice"},
            ]},
        ]},
    ],
}


def register(api: Any) -> None:
    """PluginAPI entry point: one store, nine tools, six routes, one widget."""
    global _SERVICE
    _SERVICE = lists_core.SmartLists(Path(api.get_state_dir()))
    for name, handler, description, schema in TOOLS:
        api.register_tool(name, handler, description=description, schema=schema, timeout_sec=15)
    for path, handler, methods in ROUTES:
        api.register_route(path, handler, methods=methods)
    api.register_ui_tab("lists", "Smart Lists", icon="📋", render=WIDGET_RENDER)
    api.log("info", "smart_lists registered: local store, 9 tools, 6 routes, declarative widget")


__all__ = ["register"]
