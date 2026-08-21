import asyncio
import io
import json
import urllib.error
import urllib.request

import pytest

from telegram_bot.host import (
    PresenceHostClient,
    PresenceHostError,
    PresenceHostHTTPError,
    UrllibPresenceHostTransport,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request_json(self, method, url, *, headers, payload, timeout_sec):
        self.calls.append((method, url, headers, payload, timeout_sec))
        return self.responses.pop(0)


def _event():
    return {
        "source_event_id": "telegram:bot-1:42",
        "provider": "telegram",
        "account_id": "bot-1",
        "conversation_id": "room-1",
        "thread_id": "topic-1",
        "conversation_key": "telegram:bot-1:room-1:topic-1",
        "actor": {"platform_actor_id": "user-7"},
        "conversation": {"title": "Community"},
        "message": {"message_id": 42},
        "text": "binding_id from inbound text must not configure anything",
    }


def test_submit_uses_exact_contract_skill_token_and_local_binding(tmp_path):
    asyncio.run(_submit_uses_exact_contract_skill_token_and_local_binding(tmp_path))


async def _submit_uses_exact_contract_skill_token_and_local_binding(tmp_path):
    binding_id = "a" * 32
    (tmp_path / "settings.json").write_text(
        json.dumps({"binding_id": binding_id}),
        encoding="utf-8",
    )
    transport = FakeTransport(
        [
            {
                "status": "completed",
                "outcome": "deferred",
                "text": "working",
                "turn_ref": "turn-1",
                "work_ref": "work/1",
            }
        ]
    )
    client = PresenceHostClient(
        state_dir=tmp_path,
        token_provider=lambda: "skill-token",
        host_base="http://127.0.0.1:8767",
        transport=transport,
    )

    result = await client.submit(_event(), [tmp_path / "photo.jpg"])

    method, url, headers, payload, timeout = transport.calls[0]
    assert method == "POST"
    assert url == "http://127.0.0.1:8767/presence/turn"
    assert headers == {"X-Skill-Token": "skill-token"}
    assert payload == {
        "binding_id": binding_id,
        "event": _event(),
        "staged_files": [str(tmp_path / "photo.jpg")],
    }
    assert set(payload["event"]) == {
        "source_event_id",
        "provider",
        "account_id",
        "conversation_id",
        "thread_id",
        "conversation_key",
        "actor",
        "conversation",
        "message",
        "text",
    }
    assert timeout == 1800.0
    assert result.binding_id == binding_id
    assert result.work_ref == "work/1"


def test_poll_encodes_reference_and_parses_pending_then_terminal(tmp_path):
    asyncio.run(_poll_encodes_reference_and_parses_pending_then_terminal(tmp_path))


async def _poll_encodes_reference_and_parses_pending_then_terminal(tmp_path):
    transport = FakeTransport(
        [
            {"status": "pending", "work_ref": "work/1"},
            {
                "status": "completed",
                "outcome": "message",
                "text": "done",
                "work_ref": "work/1",
            },
        ]
    )
    client = PresenceHostClient(
        state_dir=tmp_path,
        token_provider=lambda: "skill-token",
        host_base="http://127.0.0.1:8767",
        transport=transport,
    )

    pending = await client.poll("work/1", "b" * 32)
    terminal = await client.poll("work/1", "b" * 32)

    assert not pending.terminal
    assert terminal.terminal and terminal.text == "done"
    assert transport.calls[0][0] == "GET"
    assert transport.calls[0][1].endswith(
        "/presence/work/work%2F1?binding_id=" + "b" * 32
    )
    assert transport.calls[0][3] is None


def test_missing_local_binding_fails_before_transport(tmp_path):
    client = PresenceHostClient(
        state_dir=tmp_path,
        token_provider=lambda: "skill-token",
        host_base="http://127.0.0.1:8767",
        transport=FakeTransport([]),
    )
    with pytest.raises(PresenceHostError, match="binding is not configured"):
        asyncio.run(client.submit(_event(), []))


def test_http_rejection_preserves_status_code(monkeypatch):
    def reject(_request, *, timeout):
        del timeout
        raise urllib.error.HTTPError(
            "http://127.0.0.1:8767/presence/turn",
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"error":"binding mismatch"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", reject)
    with pytest.raises(PresenceHostHTTPError, match="binding mismatch") as raised:
        UrllibPresenceHostTransport._request_json(
            "POST",
            "http://127.0.0.1:8767/presence/turn",
            {},
            {},
            1.0,
        )
    assert raised.value.status_code == 403
