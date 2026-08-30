from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from lib.host_adapter import (
    HostBindingTerminalError,
    HostContractError,
    LoopbackPresenceHostAdapter,
    normalize_binding_id,
    slack_presence_event,
)
from lib.store import InboxItem


def _item() -> InboxItem:
    return InboxItem(
        row_id=1,
        lease_token="lease",
        envelope_id="env-1",
        event_id="Ev-1",
        ordering_key="T1:C1:10.0",
        team_id="T1",
        enterprise_id="E1",
        event_type="message",
        subtype="file_share",
        actor_user_id="U1",
        actor_team_id="T-ACTOR",
        channel_id="C1",
        channel_type="channel",
        message_ts="10.0",
        thread_ts="",
        event_ts="10.1",
        client_msg_id="client-1",
        text="/status is ordinary conversation text",
        files=(
            {
                "file_id": "F1",
                "name": "brief.pdf",
                "mimetype": "application/pdf",
                "size": 42,
                "url_private": "https://files.slack.com/files-pri/T/F/brief.pdf",
            },
        ),
        staged_files=(
            {
                "file_id": "F1",
                "name": "brief.pdf",
                "mimetype": "application/pdf",
                "size": 42,
                "path": "/state/staged/brief.pdf",
            },
        ),
        host_reference="",
        attempts=1,
    )


def _adapter(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LoopbackPresenceHostAdapter(
        binding_id="a" * 32,
        host_service_url="http://127.0.0.1:8767",
        skill_token="skill-token",
        http_client=http,
    )
    return adapter, http


def test_exact_slack_facts_map_to_frozen_generic_event() -> None:
    event = slack_presence_event(_item())

    assert set(event) == {
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
    assert event["source_event_id"] == "Ev-1"
    assert event["provider"] == "slack"
    assert event["account_id"] == "T1"
    assert event["conversation_id"] == "C1"
    assert event["thread_id"] == "10.0"
    assert event["conversation_key"] == "slack:T1:C1:10.0"
    assert event["actor"] == {
        "platform": "slack",
        "platform_actor_id": "U1",
        "actor_team_id": "T-ACTOR",
    }
    assert event["message"]["message_id"] == "10.0"
    assert event["message"]["attachments"][0]["file_id"] == "F1"
    assert event["text"] == "/status is ordinary conversation text"


def test_completed_turn_posts_exact_contract_and_delivers_nonempty_text() -> None:
    async def run() -> None:
        observed = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed["method"] = request.method
            observed["path"] = request.url.path
            observed["token"] = request.headers.get("x-skill-token")
            observed["json"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "outcome": "message",
                    "text": "host reply",
                    "turn_ref": "turn-1",
                    "work_ref": "",
                },
            )

        adapter, http = _adapter(handler)
        reference = await adapter.submit(_item())
        assert (await adapter.status(reference)).state == "ready"
        assert (await adapter.deliver(reference)).texts == ("host reply",)
        assert observed == {
            "method": "POST",
            "path": "/presence/turn",
            "token": "skill-token",
            "json": {
                "binding_id": "a" * 32,
                "event": slack_presence_event(_item()),
                "staged_files": ["/state/staged/brief.pdf"],
            },
        }
        await adapter.aclose()
        await http.aclose()

    asyncio.run(run())


def test_deferred_work_retains_reference_polls_until_terminal_and_delivers_once() -> (
    None
):
    async def run() -> None:
        polls = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "outcome": "deferred",
                        "text": "working",
                        "work_ref": "work/1",
                    },
                )
            polls.append(request)
            if len(polls) == 1:
                return httpx.Response(
                    200,
                    json={"status": "pending", "work_ref": "work/1"},
                )
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "outcome": "message",
                    "text": "late reply",
                    "turn_ref": "turn-2",
                    "work_ref": "work/1",
                },
            )

        adapter, http = _adapter(handler)
        reference = await adapter.submit(_item())
        assert reference.startswith("deferred:")
        pending = await adapter.status(reference)
        ready = await adapter.status(reference)
        assert pending.state == "pending" and pending.texts == ("working",)
        assert ready.state == "ready" and ready.texts == ("working",)
        assert (await adapter.deliver(reference)).texts == ("late reply",)
        assert len(polls) == 2
        assert polls[-1].url.params["binding_id"] == "a" * 32
        assert b"/presence/work/work%2F1" in polls[-1].url.raw_path
        await adapter.aclose()
        await http.aclose()

    asyncio.run(run())


def test_empty_completed_text_produces_no_provider_delivery() -> None:
    async def run() -> None:
        adapter, http = _adapter(
            lambda _request: httpx.Response(
                200,
                json={
                    "status": "completed",
                    "outcome": "silent",
                    "text": "  ",
                    "turn_ref": "turn-1",
                    "work_ref": "",
                },
            )
        )
        reference = await adapter.submit(_item())
        assert (await adapter.deliver(reference)).texts == ()
        await adapter.aclose()
        await http.aclose()

    asyncio.run(run())


def test_presence_token_is_never_allowed_to_leave_loopback() -> None:
    with pytest.raises(HostContractError, match="loopback"):
        LoopbackPresenceHostAdapter(
            binding_id="a" * 32,
            host_service_url="https://example.com",
            skill_token="skill-token",
        )


@pytest.mark.parametrize("status_code", [403, 404])
def test_binding_rejection_is_a_terminal_host_error(status_code: int) -> None:
    async def run() -> None:
        adapter, http = _adapter(
            lambda _request: httpx.Response(status_code, json={"error": "binding"})
        )
        with pytest.raises(HostBindingTerminalError, match=f"HTTP {status_code}"):
            await adapter.submit(_item())
        await adapter.aclose()
        await http.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "value",
    ["binding-1", "A" * 32, "a" * 31, "a" * 33, "g" * 32],
)
def test_binding_id_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(HostContractError, match="32 lowercase hexadecimal"):
        normalize_binding_id(value)


def test_binding_id_accepts_canonical_lowercase_hex() -> None:
    assert normalize_binding_id("0123456789abcdef" * 2) == "0123456789abcdef" * 2
