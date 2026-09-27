"""Yandex Eats (Almaty): one persistent, visible browser window.

The Playwright dependency makes the host run these handlers in short-lived
per-call children, so no handler owns the browser. Each validates its
arguments and asks the host-supervised companion (scripts/eats_browser.py),
which launches Chrome on the first open and keeps it between calls. The tools
open the service root, read eda.yandex.kz pages, and act on freshly observed
elements to assemble a cart. The owner signs in and approves any separate
checkout after seeing cart details. Yandex Taxi is deferred and has no tool.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .eats_session import (ACTIONS, INTENTS, KEYS, SCOPES, SCROLLS, TIMEOUTS, call,
                           validate_act, validate_capture, validate_close, validate_observe,
                           validate_open, validate_search)

_api: Any = None
COMPANION = "eats_browser"


def _run(op: str, validate: Callable[[dict[str, Any]], dict[str, Any]], args: dict[str, Any]) -> str:
    try:
        clean = validate(args)
    except ValueError as exc:  # refused here, before the browser is contacted
        return json.dumps({"status": "invalid", "effect": "none", "reason": str(exc)}, ensure_ascii=False)
    return json.dumps(call(Path(_api.get_state_dir()), op, clean), ensure_ascii=False)


def eats_open(ctx: Any = None, **args: Any) -> str:
    return _run("open", validate_open, args)


def eats_observe(ctx: Any = None, **args: Any) -> str:
    return _run("observe", validate_observe, args)


def eats_close(ctx: Any = None, **args: Any) -> str:
    return _run("close", validate_close, args)


def eats_act(ctx: Any = None, **args: Any) -> str:
    return _run("act", validate_act, args)


def eats_search(ctx: Any = None, **args: Any) -> str:
    return _run("search", validate_search, args)


def eats_capture(ctx: Any = None, **args: Any) -> str:
    return _run("capture", validate_capture, args)


TOOLS: tuple[tuple[str, Callable[..., str], str, str, dict[str, Any]], ...] = (
    ("yandex_eats_open", eats_open, "open",
     "Open or reuse the visible Yandex Eats window (it stays open across calls) at https://eda.yandex.kz/ "
     "and return what it shows. The agent selects address, restaurant and dishes from fresh evidence "
     "and assembles the cart. The owner signs in. A Yandex ID page is not read.",
     {"type": "object", "additionalProperties": False, "properties": {
         "home": {"type": "boolean",
                  "description": "load https://eda.yandex.kz/ even if the open window shows another page"}}}),
    ("yandex_eats_observe", eats_observe, "observe",
     "Read the current eda.yandex.kz page: element ids with role, name, surrounding card text and "
     "state, plus visible text; never field values. query filters the whole page; scroll moves the "
     "view without any click or key. Other pages return only their kind and host.",
     {"type": "object", "additionalProperties": False, "properties": {
         "query": {"type": "string", "description": "case-insensitive text to find anywhere on the page"},
         "scope": {"type": "string", "enum": list(SCOPES)},
         "scroll": {"type": "string", "enum": list(SCROLLS)},
         "wait_ms": {"type": "integer", "minimum": 0, "maximum": 10000}}}),
    ("yandex_eats_act", eats_act, "act",
     "Act on one element from the latest observation, then return a fresh observation. Choose the "
     "element from its role, name and surrounding text. Exact names may be ambiguous; use element_id. "
     "Use only for address, restaurant, dish and cart assembly. Stop before checkout, payment or order "
     "confirmation. A generic click can trigger an unexpected transaction; intent is advisory.",
     {"type": "object", "additionalProperties": False, "required": ["action", "observation_id"],
      "properties": {
          "action": {"type": "string", "enum": list(ACTIONS)},
          "observation_id": {"type": "string"}, "element_id": {"type": "string"},
          "target_name": {"type": "string"}, "target_role": {"type": "string"},
          "intent": {"type": "string", "enum": list(INTENTS)},
          "text": {"type": "string"}, "key": {"type": "string", "enum": list(KEYS)}}}),
    ("yandex_eats_search", eats_search, "search",
     "Fill a selected search field; trigger_name is an exact button name the agent chose from the "
     "current UI. After filling, click it only if unique and return the results in one call. "
     "Without it, Enter works only for a structural search field. Never use for secrets.",
     {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
         "query": {"type": "string"}, "observation_id": {"type": "string"},
         "element_id": {"type": "string"}, "press_enter": {"type": "boolean"},
         "trigger_name": {"type": "string"}}}),
    ("yandex_eats_capture", eats_capture, "capture",
     "Capture the visible Eats page or ONE non-actionable region from the latest observation. "
     "Editable fields, selects and iframes are masked; the result is a local path, not an owner delivery. "
     "Send the captured cart directly with send_photo. Login pages cannot be captured.",
     {"type": "object", "additionalProperties": False,
      "required": ["observation_id"], "properties": {
          "observation_id": {"type": "string"}, "element_id": {"type": "string"}}}),
    ("yandex_eats_close", eats_close, "close",
     "Close the visible Yandex Eats window; the skill's own sign-in profile is kept.",
     {"type": "object", "additionalProperties": False, "properties": {}}),
)


def register(api: Any) -> None:
    global _api
    _api = api
    api.register_companion_process(COMPANION)
    for name, handler, op, description, schema in TOOLS:
        api.register_tool(name, handler, description=description, schema=schema,
                          timeout_sec=TIMEOUTS[op] + 15)
