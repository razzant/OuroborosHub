"""Curated Project news: Project roots' own messages wait here until a mind selects what to tell.

The bridge captures, by host row address, each message, photo, video or document a
Project's root task writes in its Project chat (never a helper's row, card, media,
progress line or routing note). Media are kept as references (row address, download
address, artifact reference, file name), never re-sent as attachments. Nothing here
runs a model or a clock: a root mind (a Telegram place focus, an owner turn, a
consciousness wake) reads the waiting items, writes the news in its own words for the
owner and names the items it covers, or omits items with a reason. A caller bound to
an external place acts only while its own source sender is an owner account that
place currently confirms: a place's context never becomes owner authority by itself.
The news is queued in the place outbox for the owner's chat: a connected place there
governs it (Silent or Paused holds it), with no place it is sent as the bridge's own
message. The mirror mode still decides raw Project mirroring (``main`` sends none).
"""
from __future__ import annotations

import json
import time

from .telegram_places import _rows, _tx, enqueue, place_lookup, record_gap
from .telegram_state import _data_dir, _delegated_task, _load_settings

KEEP_PENDING = 500
READ_DESCRIPTION = ("Read Project news waiting for the owner's Telegram: the messages and media Project root tasks "
                    "wrote in their Project chats since the last selection (helpers' rows excluded), oldest first. "
                    "Media come as references, not files. Only the newest 500 wait: 'expired' counts older items "
                    "that left unreviewed, so an empty list is not 'everything reviewed'; each Project's own room "
                    "history (item chat_id) keeps every row.")
READ_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "after": {"type": "integer", "minimum": 0, "description": "Continue after this item id."},
    "limit": {"type": "integer", "minimum": 1, "maximum": 50}}}
PUBLISH_DESCRIPTION = ("Tell the owner on Telegram what matters from waiting Project news, in your own words, naming "
                       "the item ids it covers (publish), or set items aside with a reason (skip). Queued, then "
                       "delivered with receipts; reuse request_id with the same words after an uncertain call.")
PUBLISH_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["action", "source_ids"], "properties": {
    "action": {"type": "string", "enum": ["publish", "skip"]},
    "source_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 50},
    "text": {"type": "string", "description": "publish: the news for the owner (Markdown)."},
    "request_id": {"type": "string", "description": "publish: 1..100 characters, the news item's identity."},
    "reason": {"type": "string", "description": "skip: why these items are not news."}}}


def capture(api, event: dict, kind: str = "message") -> bool:
    """Retain one Project root's own row by its host row address; repeats add nothing."""
    from ouroboros.projects_registry import list_reserved_projects
    from .telegram_mirror import child_event

    task_id = str(event.get("task_id") or "")
    file_ref = event.get("file_ref") if isinstance(event.get("file_ref"), dict) else {}
    text = str(event.get("text") or event.get("caption") or "").strip()
    if kind != "message":
        text = text or f"[{kind}] {event.get('filename') or file_ref.get('path') or ''}".strip()
    if (not text or event.get("is_progress") or event.get("card_row") or event.get("annotation_type")
            or child_event(event)):
        return False
    try:
        chat = int(event.get("chat_id") or 0)
        project = next((p for p in list_reserved_projects(_data_dir(api)) if int(p.get("chat_id") or 0) == chat), None)
    except Exception:
        return False
    if project is None or (kind != "message" and _delegated_task(api, task_id or str(file_ref.get("task_id") or ""))):
        return False
    address = str(event.get("row_address") or "")
    if not address:
        record_gap(api, f"project:{chat}:{task_id}", f"Project {kind} without a host row address: not captured")
        return False
    refs = {"kind": kind, "row_address": address,
            **{key: str(event[key]) for key in ("download_url", "filename", "mime") if event.get(key)},
            **({"file_ref": {k: file_ref[k] for k in ("task_id", "path", "size") if k in file_ref}} if file_ref else {})}
    with _tx(api) as conn:
        fresh = conn.execute("INSERT OR IGNORE INTO news(address,chat_id,project,task_id,ts,text,refs_json,at) "
                             "VALUES (?,?,?,?,?,?,?,?)",
                             (address, chat, str(project.get("name") or project.get("id") or chat), task_id,
                              str(event.get("ts") or ""), text, json.dumps(refs), time.time())).rowcount == 1
        conn.execute("UPDATE news SET state='expired' WHERE state='pending' AND id NOT IN "
                     "(SELECT id FROM news WHERE state='pending' ORDER BY id DESC LIMIT ?)", (KEEP_PENDING,))
    return fresh


def authorize(api, ctx) -> None:
    """Roots only; a root bound to an external place needs its own sender confirmed there now.

    Ordinary owner turns and the mind's own wakes carry no place binding. A Presence-bound
    caller (a place focus, its promoted work) is someone in that place: it may read or
    publish owner news only while its source sender is an owner account that place's
    records confirm at this moment. A bare inherited binding (no source) never qualifies.
    """
    from ouroboros.dialogue_provenance import presence_caller_binding
    from ouroboros.places import PlaceError, get_place, is_owner_account, places_path

    meta = getattr(ctx, "task_metadata", None) or {}
    lineage = (getattr(ctx, "task_contract", None) or {}).get("lineage") or {}
    if (meta.get("parent_task_id") or lineage.get("parent_task_id") or int(getattr(ctx, "task_depth", 0) or 0)
            or str(meta.get("delegation_role") or "") == "subagent"):
        raise ValueError("delegated work reports to its parent; Project news is chosen by a root")
    if presence_caller_binding(ctx) is None:
        return
    presence = meta.get("presence") if isinstance(meta.get("presence"), dict) else {}
    event = presence.get("event") if isinstance(presence.get("event"), dict) else {}
    actor = str((event.get("actor") or {}).get("platform_actor_id") or "")
    root = _data_dir(api)
    try:
        place = get_place(root, str(presence.get("place_id") or "")) if places_path(root).exists() else None
    except PlaceError:
        place = None
    if not actor or place is None or not is_owner_account(place, actor, str(event.get("account_id") or "")):
        raise ValueError("this place's sender is not an owner account the place currently confirms; "
                         "Project news is the owner's")


def read(api, ctx, *, after: int = 0, limit: int = 10) -> str:
    authorize(api, ctx)
    limit = max(1, min(50, int(limit or 10)))
    rows = _rows(api, "SELECT id, project, chat_id, task_id, ts, text, refs_json FROM news WHERE state='pending' "
                 "AND id>? ORDER BY id LIMIT ?", (int(after or 0), limit + 1))
    items = [{**{k: v for k, v in row.items() if k != "refs_json"}, "refs": json.loads(row["refs_json"] or "{}")}
             for row in rows[:limit]]
    counts = {row["state"]: row["n"] for row in _rows(api, "SELECT state, count(*) n FROM news GROUP BY state")}
    return json.dumps({"ok": True, "items": items, "has_more": len(rows) > limit, "pending": counts.get("pending", 0),
                       "expired": counts.get("expired", 0),
                       "coverage": "Only the newest waiting items are kept; 'expired' ones left unreviewed. Each "
                                   "Project's own room history (item chat_id) holds every row."},
                      ensure_ascii=False)


async def publish(api, ctx, *, action: str, source_ids: list, text: str = "", request_id: str = "",
                  reason: str = "") -> str:
    from .telegram_actions import ActionRefused, claim

    authorize(api, ctx)
    ids = sorted(dict.fromkeys(int(i) for i in source_ids or []))
    if not ids or len(ids) > 50:
        raise ValueError("name 1..50 waiting item ids")
    marks = ",".join("?" * len(ids))
    if action == "skip":
        if not str(reason).strip():
            raise ValueError("a reason is required to set items aside")
        with _tx(api) as conn:
            if conn.execute(f"SELECT count(*) FROM news WHERE state='pending' AND id IN ({marks})", ids).fetchone()[0] != len(ids):
                raise ValueError("every item must still be waiting; read the news again")
            conn.executemany("UPDATE news SET state='omitted', decision=? WHERE id=?", [(reason, i) for i in ids])
        return json.dumps({"ok": True, "status": "omitted", "source_ids": ids})
    if action != "publish":
        raise ValueError("action must be publish or skip")
    if not str(text).strip() or not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 100:
        raise ValueError("publishing needs the news text and a stable request_id of 1..100 characters")
    try:
        owner = int(str(_load_settings(api).get("TELEGRAM_CHAT_ID") or "").strip())
    except ValueError:
        raise ValueError("no owner chat is bound") from None
    lane, _place = await place_lookup(api, owner)
    delivery_id = f"news:{request_id}"
    try:
        with _tx(api) as conn:  # the request, its queued words and the items' state commit together
            if claim(conn, delivery_id, {"action": "news", "chat_id": owner, "source_ids": ids, "text": text}, "queued"):
                return json.dumps({"ok": True, "status": "already_queued", "delivery_id": delivery_id})
            if conn.execute(f"SELECT count(*) FROM news WHERE state='pending' AND id IN ({marks})", ids).fetchone()[0] != len(ids):
                raise ValueError("every item must still be waiting; read the news again")
            enqueue(api, delivery_id, update_id=0, chat_id=owner, reply_to=0, task_id=str(getattr(ctx, "task_id", "")),
                    source_event_id="", text=text, reporting=int(lane == "place"), origin="tool",
                    requires_place=False, kind="news", conn=conn)
            conn.executemany("UPDATE news SET state='published', delivery_id=? WHERE id=?", [(delivery_id, i) for i in ids])
    except ActionRefused as exc:
        raise ValueError(str(exc)) from None
    return json.dumps({"ok": True, "status": "queued", "delivery_id": delivery_id, "source_ids": ids,
                       "note": "Queued, not yet delivered; a Silent or Paused owner place holds it."})
