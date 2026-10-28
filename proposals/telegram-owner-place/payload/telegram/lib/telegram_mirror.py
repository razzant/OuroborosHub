"""Which host events reach the owner's Telegram chat.

Classic modes keep their meaning: ``all`` mirrors every published chat event to the
pinned chat, ``telegram_only`` mirrors Telegram-originated conversations plus the
owner's reminders and skill notices. The connected mode ``main`` is for an owner whose
Telegram chat is a place: Ouroboros's own Main replies, files, links and questions (web-
or Telegram-originated, never the owner's own rows), a deliberate Main notice, questions
from Projects, and nothing else. The destination decides before the origin: a Project's
own room never mirrors (only its questions), even when its work began from Telegram;
Project lifecycle and completion rows the host mirrors into Main stay out; helper
(child) rows, cards, media and links stay out by their typed lineage or their producer's
task record; rows of a place room stay out (its outbox already delivered them). Project
news then reaches the owner only as a mind chooses it (``telegram_news``).
"""
from __future__ import annotations

from typing import Any, Dict

from ouroboros.contracts.chat_id_policy import HIDDEN_CHAT_ID, WEB_UI_CHAT_ID

MODE_ALL, MODE_TELEGRAM_ONLY, MODE_MAIN = "all", "telegram_only", "main"
MODES = (MODE_ALL, MODE_TELEGRAM_ONLY, MODE_MAIN)
# Project lifecycle and completion rows the host pins into Main: Project output, not Main speech.
# A root's deliberate Main notice (``main_notice``) is Main speech and stays.
PROJECT_MIRRORS_IN_MAIN = frozenset({"project_started", "project_handoff", "project_completion_summary"})


def mirror_mode(settings: Dict[str, Any]) -> str:
    mode = str(settings.get("TELEGRAM_MIRROR_MODE") or MODE_ALL).strip().lower()
    return mode if mode in MODES else MODE_ALL


def child_event(event: Dict[str, Any]) -> bool:
    """A delegated helper's row or card, by its typed lineage."""
    task = str(event.get("task_id") or "")
    root = str(event.get("root_task_id") or "")
    return bool(event.get("subagent_event") or event.get("parent_task_id")
                or str(event.get("delegation_role") or "").strip().lower() == "subagent"
                or (root and task and root != task))


def _project_chat(api, chat_id: int) -> bool | None:
    """Whether ``chat_id`` is a registered Project room; ``None`` when the registry is unreadable."""
    from ouroboros.projects_registry import list_reserved_projects
    from .telegram_state import _data_dir

    try:
        return any(int(row.get("chat_id") or 0) == chat_id for row in list_reserved_projects(_data_dir(api)))
    except Exception:
        return None


def _main_target(settings: Dict[str, Any], event: Dict[str, Any], api, pinned: int, *, question: bool) -> int:
    from .telegram_state import _delegated_task

    transport = event.get("transport") if isinstance(event.get("transport"), dict) else {}
    telegram = transport.get("kind") == "telegram"
    if (event.get("role") == "user" or child_event(event)
            or event.get("system_type") == "relay_entry"  # a relay quotes the owner's own words
            or (event.get("annotation_type") == "routing_ack" and not telegram)):
        return 0
    if api is not None and _delegated_task(api, str(event.get("task_id") or "")):
        return 0  # the producer's own record says helper, though the event names only its task
    try:
        chat = int(event.get("chat_id") or 0)
    except (TypeError, ValueError):
        return 0
    if "chat_id" in event and chat != WEB_UI_CHAT_ID:
        if chat == HIDDEN_CHAT_ID or api is None:
            return 0
        project = _project_chat(api, chat)
        if project is not False:  # a Project's own room (or an unreadable registry): never its raw rows
            return pinned if question and project is True else 0
    if event.get("system_type") in PROJECT_MIRRORS_IN_MAIN:
        return 0
    return pinned if telegram or chat == WEB_UI_CHAT_ID else 0


def target_chat(settings: Dict[str, Any], event: Dict[str, Any], api=None, *, question: bool = False) -> int:
    """The Telegram chat an outbound host event goes to, or 0."""
    if api is not None:
        from .telegram_places import room_event

        if room_event(api, event):
            return 0  # a Place room's rows are delivered by its own outbox, once
    mode = mirror_mode(settings)
    configured = str(settings.get("TELEGRAM_CHAT_ID") or "").strip()
    transport = event.get("transport") if isinstance(event.get("transport"), dict) else {}
    if configured:
        try:
            chat_id = int(configured)
        except ValueError:
            return 0
        if mode == MODE_MAIN:
            return _main_target(settings, event, api, chat_id, question=question)
        if mode == MODE_ALL:
            # Mirror everything (web UI + Telegram) to the pinned chat
            return chat_id
        # telegram_only: only forward events that originate from Telegram transport
        if transport.get("kind") == "telegram":
            return chat_id
        # ...and the owner's notes and skill notices: System rows written for the
        # owner, which an owner who left the web should still get on the phone.
        if event.get("role") == "system" and event.get("system_type") in {"reminder", "skill_notice"}:
            return chat_id
        return 0
    # No pinned chat configured — only forward events that originate from
    # a Telegram transport conversation so local UI events are never leaked.
    if transport.get("kind") != "telegram":
        return 0
    try:
        return int(transport.get("conversation_id") or 0)
    except (TypeError, ValueError):
        return 0
