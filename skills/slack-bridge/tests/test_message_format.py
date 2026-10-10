from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from lib.runtime import OutboundWorker
from lib.slack_api import SlackClient, SlackConfigurationError
from lib.store import BridgeStore


@pytest.mark.parametrize("text_format", ["markdown", "mrkdwn", "plain"])
def test_provider_payload_uses_one_explicit_format_without_rewriting(text_format):
    async def run():
        text = "**bold** and *italic*\n`inline`\n```python\nprint('**literal**')\n```\n[reference](https://example.org)\n- first\n- second"
        calls = []
        def provider(request):
            assert request.url.path == "/api/chat.postMessage"
            assert request.method == "POST" and not request.url.query
            assert request.headers["Content-Type"] == "application/json; charset=utf-8"
            body = json.loads(request.content)
            calls.append(body)
            assert body["channel"] == "D_TEST" and body["thread_ts"] == "123.456"
            assert "blocks" not in body
            if text_format == "markdown":
                assert body["markdown_text"] == text
                assert "text" not in body and "mrkdwn" not in body
            else:
                assert body["text"] == text and body["mrkdwn"] is (text_format == "mrkdwn")
                assert "markdown_text" not in body
            return httpx.Response(200, json={"ok": True, "ts": "123.457"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            result = await slack.post_message(channel="D_TEST", text=text, thread_ts="123.456", text_format=text_format)
        assert result["ts"] == "123.457" and len(calls) == 1
    asyncio.run(run())


def test_existing_outbox_rows_keep_mrkdwn_after_schema_migration(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        store.enqueue_outbox(request_id="old", target="D_TEST", thread_ts="123.456", chunks=["*legacy bold*"])
        with sqlite3.connect(store.path) as db:
            db.execute("ALTER TABLE outbox DROP COLUMN text_format")
        migrated = BridgeStore(tmp_path)
        calls = []
        def provider(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "ts": "123.457"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            assert await OutboundWorker(migrated, slack).process_once()
        assert calls == [{"channel": "D_TEST", "text": "*legacy bold*", "mrkdwn": True, "thread_ts": "123.456"}]
        assert migrated.status()["outbox_delivered"] == 1
    asyncio.run(run())


def test_retry_and_repeated_request_id_preserve_exact_format_and_payload(tmp_path):
    async def run():
        store = BridgeStore(tmp_path)
        store.enqueue_outbox(request_id="once", target="D_TEST", thread_ts="123.456", chunks=["**original**"])
        calls = []
        def provider(request):
            calls.append(json.loads(request.content))
            if len(calls) == 1:
                return httpx.Response(429, json={"ok": False, "error": "ratelimited"})
            return httpx.Response(200, json={"ok": True, "ts": "123.457"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            worker = OutboundWorker(store, slack)
            assert await worker.process_once()
            assert len(calls) == 1, "A timeout must not cause an immediate format fallback send"
            # A retry of the logical send cannot change already-recorded content.
            store.enqueue_outbox(request_id="once", target="C_OTHER", thread_ts="999", chunks=["changed"], text_format="plain")
            with sqlite3.connect(store.path) as db:
                db.execute("UPDATE outbox SET available_at=0")
            reopened = BridgeStore(tmp_path)
            assert await OutboundWorker(reopened, slack).process_once()
        assert calls == [{"channel": "D_TEST", "markdown_text": "**original**", "thread_ts": "123.456"}] * 2
        assert reopened.status()["outbox_delivered"] == 1
    asyncio.run(run())


def test_invalid_format_refused_before_queue_or_provider_call(tmp_path):
    store = BridgeStore(tmp_path)
    with pytest.raises(SlackConfigurationError, match="text_format"):
        store.enqueue_outbox(request_id="bad", target="D_TEST", thread_ts="", chunks=["text"], text_format="html")
    assert store.status()["outbox_pending"] == 0
    async def run():
        calls = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r))) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            with pytest.raises(SlackConfigurationError):
                await slack.post_message(channel="D_TEST", text="text", text_format="html")
        assert calls == []
    asyncio.run(run())
