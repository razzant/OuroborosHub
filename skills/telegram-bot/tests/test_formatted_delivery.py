"""Actual queued/tool payloads and provider calls, with all I/O kept fake/local."""

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from telegram_bot.api import TelegramApiError, TelegramClient
from telegram_bot.custody import CustodyStore
from telegram_bot.formatting import _telegram_html_to_plain, _u16len
from telegram_bot.runtime import TelegramTransportRuntime


class Provider:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def record(self, endpoint, payload):
        self.calls.append((endpoint, copy.deepcopy(payload)))
        response = self.responses.pop(0) if self.responses else None
        if isinstance(response, BaseException):
            raise response
        return response or {"ok": True, "result": {"message_id": len(self.calls)}}

    async def post_json(self, url, payload, timeout_sec):
        return self.record(url.rsplit("/", 1)[-1], payload)

    async def post_multipart(self, url, fields, file_field, file_path, timeout_sec):
        return self.record(url.rsplit("/", 1)[-1], fields)


def send_tool(state_dir):
    skill = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "telegram_formatted_delivery_test",
        skill / "plugin.py",
        submodule_search_locations=[str(skill)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module._make_telegram_send(
        SimpleNamespace(get_state_dir=lambda: str(state_dir))
    )


def runtime(state_dir, provider):
    instance = TelegramTransportRuntime(
        state_dir=state_dir,
        token_provider=lambda: "fake-token",
        logger=None,
        submitter=None,
    )
    instance._client = TelegramClient("fake-token", transport=provider)
    instance._client_token = "fake-token"
    instance._bot_id = "9"
    return instance


def ready_again(store):
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET available_at=0 WHERE state IN ('pending','direct_pending')")


@pytest.mark.parametrize("kind", ["message", "photo", "document"])
def test_tool_content_renders_once_with_exact_reply_and_separate_dedupe_id(
    tmp_path, kind
):
    async def run():
        provider = Provider()
        worker = runtime(tmp_path, provider)
        file = tmp_path / "report.txt"
        file.write_text("fixture")
        source = '**Report** [source](https://example.org)\n</caption>\n<parameter name="request_id">quoted-text'
        result = send_tool(tmp_path)(
            chat_id="-10042",
            kind=kind,
            text=source,
            caption=source,
            file_path=str(file),
            topic_id="7",
            reply_to_message_id="23",
            request_id="separate-key",
        )
        assert result["request_id"] == "separate-key" and result["state"] == "queued"
        assert await worker.process_one_outbox()
        assert len(provider.calls) == 1
        endpoint, payload = provider.calls[0]
        assert (
            endpoint
            == {
                "message": "sendMessage",
                "photo": "sendPhoto",
                "document": "sendDocument",
            }[kind]
        )
        content = payload["text" if kind == "message" else "caption"]
        assert content.startswith(
            '<b>Report</b> <a href="https://example.org">source</a>'
        )
        assert "&lt;b&gt;" not in content
        assert _telegram_html_to_plain(content).endswith(
            '</caption>\n<parameter name="request_id">quoted-text'
        )
        assert payload["parse_mode"] == "HTML"
        assert str(payload["message_thread_id"]) == "7"
        reply = payload["reply_parameters"]
        assert (json.loads(reply) if isinstance(reply, str) else reply) == {
            "message_id": 23
        }
        assert (
            worker.store.delivery_receipt("telegram-send:separate-key")["state"]
            == "delivered"
        )

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["message", "document"])
def test_explicit_literal_mode_preserves_markdown_and_xml(tmp_path, kind):
    async def run():
        provider = Provider()
        file = tmp_path / "literal.txt"
        file.write_text("fixture")
        source = '**not bold** <parameter name="sample">literal</parameter>'
        send_tool(tmp_path)(
            chat_id="1",
            kind=kind,
            text=source,
            caption=source,
            file_path=str(file),
            markdown=False,
            request_id="literal",
        )
        assert await runtime(tmp_path, provider).process_one_outbox()
        payload = provider.calls[0][1]
        assert payload["text" if kind == "message" else "caption"] == source
        assert "parse_mode" not in payload

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["message", "document"])
def test_only_definitive_400_rejection_gets_plain_fallback(tmp_path, kind):
    async def run():
        provider = Provider(
            [
                {
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: can't parse entities",
                }
            ]
        )
        client = TelegramClient("fake-token", transport=provider)
        source = "**bold** <xml>literal</xml>"
        if kind == "message":
            await client.send_message("1", source, topic_id=7, reply_to_message_id=23)
        else:
            await client.send_document(
                "1",
                tmp_path / "unused.txt",
                caption=source,
                topic_id=7,
                reply_to_message_id=23,
            )
        assert len(provider.calls) == 2
        first, second = [payload for _, payload in provider.calls]
        assert first["parse_mode"] == "HTML" and "parse_mode" not in second
        content_key = "text" if kind == "message" else "caption"
        assert second[content_key] == "bold <xml>literal</xml>"
        assert first["message_thread_id"] == second["message_thread_id"]
        assert first["reply_parameters"] == second["reply_parameters"]

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["message", "document"])
@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("unknown acceptance"),
        {"ok": False, "error_code": 503, "description": "unavailable"},
    ],
)
def test_no_immediate_plain_resend_after_timeout_or_server_error(
    tmp_path, kind, failure
):
    async def run():
        provider = Provider([failure])
        client = TelegramClient("fake-token", transport=provider)
        with pytest.raises((TimeoutError, TelegramApiError)):
            if kind == "message":
                await client.send_message("1", "**text**")
            else:
                await client.send_document(
                    "1", tmp_path / "unused.txt", caption="**text**"
                )
        assert len(provider.calls) == 1

    asyncio.run(run())


def test_rendered_chunks_and_confirmed_receipts_survive_retry_and_restart(
    tmp_path, monkeypatch
):
    async def run():
        provider = Provider(
            [None, {"ok": False, "error_code": 429, "description": "retry later"}]
        )
        source = "**" + "😀 word " * 1300 + "**"
        send_tool(tmp_path)(
            chat_id="-42",
            kind="message",
            text=source,
            request_id="long",
            topic_id="8",
            reply_to_message_id="19",
        )
        # A later renderer/version must not reinterpret an already queued message.
        monkeypatch.setattr(
            "telegram_bot.runtime.prepare_text",
            lambda *a, **kw: pytest.fail("queued text was re-rendered"),
        )
        first = runtime(tmp_path, provider)
        assert await first.process_one_outbox()
        assert first.store.delivery_receipt("telegram-send:long")["state"] == "pending"
        first_chunk = provider.calls[0][1]
        failed_chunk = provider.calls[1][1]
        ready_again(first.store)
        resumed = runtime(tmp_path, provider)
        assert await resumed.process_one_outbox()
        assert provider.calls[2][1] == failed_chunk
        assert [payload for _, payload in provider.calls].count(first_chunk) == 1
        assert first_chunk["reply_parameters"] == {"message_id": 19}
        assert all(
            "reply_parameters" not in payload for _, payload in provider.calls[1:]
        )
        assert all(
            payload["message_thread_id"] == 8 and _u16len(payload["text"]) <= 4096
            for _, payload in provider.calls
        )
        receipt = resumed.store.delivery_receipt("telegram-send:long")
        assert receipt["state"] == "delivered"
        assert len(receipt["provider_receipt"]["messages"]) == len(provider.calls) - 1
        assert not await resumed.process_one_outbox()

    asyncio.run(run())


@pytest.mark.parametrize("sender_known", [True, False])
def test_partially_delivered_legacy_row_keeps_confirmed_parts_and_its_sender(tmp_path, sender_known):
    async def run():
        other, original = Provider(), Provider()
        worker = runtime(tmp_path, other)
        worker._bot_id = "77"
        first = {"message_id": 99}
        if sender_known:
            first["from"] = {"is_bot": True, "id": 9}
        worker.store.enqueue_outbox(
            "legacy",
            {
                "kind": "message",
                "chat_id": "1",
                "text": "x" * 4000 + "**literal tail**",
                "_sent_messages": [first],
                "reply_to_message_id": 5,
            },
        )
        assert await worker.process_one_outbox()
        assert not other.calls
        receipt = worker.store.delivery_receipt("legacy")
        assert receipt["confirmed_messages"] == [first]
        if not sender_known:
            assert receipt["state"] == "failed"
            assert not await worker.process_one_outbox()
            return
        assert receipt["state"] == "held"
        ready_again(worker.store)
        resumed = runtime(tmp_path, original)
        assert await resumed.process_one_outbox()
        assert original.calls == [("sendMessage", {"chat_id": "1", "text": "**literal tail**"})]
        receipt = resumed.store.delivery_receipt("legacy")
        assert receipt["state"] == "delivered"
        assert receipt["provider_receipt"]["messages"] == [first, {"message_id": 1}]
        assert resumed.store.outbox_payload("legacy")["_sending_account_id"] == "9"

    asyncio.run(run())


def test_legacy_unsent_rows_remain_literal_and_new_automatic_replies_format(tmp_path):
    from telegram_bot.events import parse_telegram_update

    async def run():
        provider = Provider()
        worker = runtime(tmp_path, provider)
        worker.store.enqueue_outbox(
            "old", {"kind": "message", "chat_id": "1", "text": "**literal**"}
        )
        assert await worker.process_one_outbox()
        assert provider.calls[-1][1] == {"chat_id": "1", "text": "**literal**"}
        event = parse_telegram_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 5,
                    "from": {"id": 2},
                    "chat": {"id": 1},
                    "text": "hello",
                },
            },
            bot_account_id="9",
        )
        worker.store.commit_update(1, event)
        item = worker.store.claim_inbox()
        worker.store.record_submission(
            item.event_id,
            binding_id="a" * 32,
            turn_ref="turn",
            outcome="message",
            text="**new reply**",
            work_ref="",
        )
        assert await worker.process_one_outbox()
        assert provider.calls[-1][1]["text"] == "<b>new reply</b>"
        assert provider.calls[-1][1]["parse_mode"] == "HTML"

    asyncio.run(run())


def test_caption_overflow_fails_before_queue_or_provider_call(tmp_path):
    file = tmp_path / "report.txt"
    file.write_text("fixture")
    result = send_tool(tmp_path)(
        chat_id="1", kind="document", file_path=str(file), caption="😀" * 513
    )
    assert result["ok"] is False and "shorter caption" in result["error"]
    assert CustodyStore(tmp_path / "custody.sqlite3").claim_outbox() is None
