"""Every registered tool speaks the host's `str` ABI, in valid JSON."""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from test_workers_plugin import _Api, _load_plugin

from lib.tool_results import json_tool, to_tool_json


def _wants_ctx(handler) -> bool:
    """Mirror of ouroboros.extension_process_runner._handler_wants_ctx.

    The host reads this from the RAW handler object passed to register_tool, so
    the wrapper must keep reporting exactly what the unwrapped handler reported.
    """

    try:
        params = list(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):
        return True
    if not params:
        return False
    first = params[0]
    if first.kind == first.VAR_POSITIONAL:
        return True
    if first.kind in (first.POSITIONAL_ONLY, first.POSITIONAL_OR_KEYWORD):
        return first.name in {"ctx", "context", "_ctx", "tool_context"}
    return False


def test_sync_and_async_results_are_serialized_without_changing_the_signature():
    def sync_handler(ctx=None, *, value: str = ""):
        return {"ok": True, "value": value, "ctx": ctx}

    async def async_handler(**params):
        return {"ok": True, "params": params}

    wrapped_sync = json_tool(sync_handler)
    wrapped_async = json_tool(async_handler)

    assert not inspect.iscoroutinefunction(wrapped_sync)
    assert inspect.iscoroutinefunction(wrapped_async)
    assert _wants_ctx(wrapped_sync) is _wants_ctx(sync_handler) is True
    assert _wants_ctx(wrapped_async) is _wants_ctx(async_handler) is False
    assert inspect.signature(wrapped_sync) == inspect.signature(sync_handler)

    assert json.loads(wrapped_sync(None, value="x")) == {"ok": True, "value": "x", "ctx": None}
    assert json.loads(asyncio.run(wrapped_async(a=1))) == {"ok": True, "params": {"a": 1}}


def test_handler_exceptions_still_reach_the_host_instead_of_becoming_a_result():
    def explode(**_params):
        raise RuntimeError("provider exploded")

    with pytest.raises(RuntimeError, match="provider exploded"):
        json_tool(explode)()


def test_unserializable_values_never_break_the_str_abi():
    class Opaque:
        def __str__(self) -> str:
            return "opaque"

    assert json.loads(to_tool_json({"value": Opaque()})) == {"value": "opaque"}


def test_every_registered_tool_returns_json_text_for_success_and_failure(tool_json, tmp_path):
    module = _load_plugin()
    api = _Api(tmp_path)
    module.register(api)

    # The host decides the ctx calling convention from each registered handler.
    assert {name: _wants_ctx(handler) for name, (handler, _meta) in api.tools.items()} == {
        "slack_send": True,
        "slack_api": True,
        "slack_file_upload": True,
        "slack_file_download": False,
        "slack_receipt": False,
        "slack_join": True,
        "slack_message_edit": True,
        "slack_message_delete": True,
        "slack_reaction_add": True,
        "slack_reaction_remove": True,
        "slack_pin_add": True,
        "slack_pin_remove": True,
        "slack_bookmark_add": True,
        "slack_bookmark_remove": True,
        "slack_user_info": False,
        "slack_conversation_info": False,
        "slack_history": False,
        "slack_thread": False,
        "slack_reactions": False,
        "slack_list_conversations": False,
        "slack_list_users": False,
        "slack_lookup_user_email": False,
        "slack_members": False,
        "slack_resolve": False,
    }

    queued = tool_json(api.tools["slack_send"][0](channel_or_user="C1", text="hi", request_id="json-1"))
    assert queued["state"] == "queued"

    # Failure results are JSON too, not a Python repr of an error dict.
    missing_text = tool_json(api.tools["slack_send"][0](channel_or_user="C1", text=""))
    assert missing_text == {"ok": False, "error": "text is required"}

    missing_file = tool_json(api.tools["slack_file_upload"][0](file_path=str(tmp_path / "absent.txt")))
    assert missing_file["ok"] is False and "regular file" in missing_file["error"]

    rejected = tool_json(asyncio.run(api.tools["slack_api"][0](path="chat.postMessage", body={"token": "s3cret"})))
    assert rejected["ok"] is False and "token" in rejected["error"]["message"]

    unknown = tool_json(api.tools["slack_receipt"][0](request_id="never-queued"))
    assert unknown == {"state": "not_found", "request_id": "never-queued"}

    # A provider read failure (no bot token configured here) is still JSON text.
    denied = tool_json(asyncio.run(api.tools["slack_user_info"][0](user_id="U1")))
    assert denied["ok"] is False and denied["error"]["code"] == "configuration_or_argument"
