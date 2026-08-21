from __future__ import annotations

import sqlite3
import time
import json
from pathlib import Path

from lib.events import parse_socket_envelope
from lib.store import BridgeStore


def _payload(
    *,
    envelope_id: str,
    event_id: str,
    channel: str = "C123",
    timestamp: str = "1710000000.001",
    thread_ts: str = "",
    text: str = "hello",
) -> dict:
    event = {
        "type": "message",
        "user": "U_ACTOR",
        "user_team": "T_ACTOR",
        "channel": channel,
        "channel_type": "channel",
        "ts": timestamp,
        "event_ts": "1710000000.002",
        "client_msg_id": "client-123",
        "text": text,
        "files": [
            {
                "id": "F123",
                "name": "brief.pdf",
                "mimetype": "application/pdf",
                "size": 42,
                "url_private": "https://files.slack.com/files-pri/T/F/brief.pdf",
            }
        ],
    }
    if thread_ts:
        event["thread_ts"] = thread_ts
    return {
        "type": "events_api",
        "envelope_id": envelope_id,
        "payload": {
            "event_id": event_id,
            "team_id": "T_WORKSPACE",
            "enterprise_id": "E_ORG",
            "event": event,
        },
    }


def _parse(payload: dict):
    return parse_socket_envelope(payload, bot_user_id="U_BOT")


def test_parser_preserves_exact_message_actor_thread_and_file_provenance() -> None:
    payload = _payload(
        envelope_id="env-1",
        event_id="Ev-1",
        thread_ts="1709999999.900",
    )
    parsed = _parse(payload)

    assert parsed.accepted is True
    event = parsed.event
    assert event is not None
    assert event.envelope_id == "env-1"
    assert event.event_id == "Ev-1"
    assert event.team_id == "T_WORKSPACE"
    assert event.enterprise_id == "E_ORG"
    assert event.actor_user_id == "U_ACTOR"
    assert event.actor_team_id == "T_ACTOR"
    assert event.channel_id == "C123"
    assert event.message_ts == "1710000000.001"
    assert event.thread_ts == "1709999999.900"
    assert event.event_ts == "1710000000.002"
    assert event.client_msg_id == "client-123"
    assert event.text == "hello"
    assert event.files[0].file_id == "F123"
    assert event.files[0].url_private.startswith("https://files.slack.com/")


def test_event_id_dedupes_slack_retry_with_a_new_envelope(tmp_path) -> None:
    store = BridgeStore(tmp_path)
    first = _payload(envelope_id="env-1", event_id="Ev-same")
    retry = _payload(envelope_id="env-2", event_id="Ev-same")

    first_id, first_inserted = store.ingest_envelope(first, _parse(first))
    retry_id, retry_inserted = store.ingest_envelope(retry, _parse(retry))

    assert first_inserted is True
    assert retry_inserted is False
    assert retry_id == first_id
    assert store.status()["inbox_pending"] == 1


def test_ordinary_visible_channel_messages_are_accepted_without_a_second_allowlist() -> (
    None
):
    payload = _payload(envelope_id="env-1", event_id="Ev-1")
    payload["payload"]["event"]["type"] = "message"

    accepted = parse_socket_envelope(payload, bot_user_id="U_BOT")

    assert accepted.accepted is True


def test_app_mention_is_ignored_because_message_events_are_the_single_ingress() -> None:
    payload = _payload(envelope_id="env-mention", event_id="Ev-mention")
    payload["payload"]["event"]["type"] = "app_mention"

    ignored = parse_socket_envelope(payload, bot_user_id="U_BOT")

    assert ignored.accepted is False
    assert ignored.reason == "unsupported_event"


def test_slack_manifest_subscribes_only_to_message_ingress() -> None:
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    scopes = manifest["oauth_config"]["scopes"]["bot"]
    events = manifest["settings"]["event_subscriptions"]["bot_events"]

    assert "app_mentions:read" not in scopes
    assert "app_mention" not in events
    assert events == [
        "message.channels",
        "message.groups",
        "message.im",
        "message.mpim",
    ]


def test_claims_are_fifo_within_thread_and_parallel_across_threads(tmp_path) -> None:
    store = BridgeStore(tmp_path)
    same_1 = _payload(envelope_id="env-1", event_id="Ev-1", timestamp="1")
    same_2 = _payload(
        envelope_id="env-2", event_id="Ev-2", timestamp="2", thread_ts="1"
    )
    other = _payload(
        envelope_id="env-3", event_id="Ev-3", channel="C999", timestamp="3"
    )
    for payload in (same_1, same_2, other):
        store.ingest_envelope(payload, _parse(payload))

    first = store.claim_inbox()
    assert first is not None and first.event_id == "Ev-1"
    parallel = store.claim_inbox()
    assert parallel is not None and parallel.event_id == "Ev-3"

    store.complete_inbox(first.row_id, first.lease_token)
    second = store.claim_inbox()
    assert second is not None and second.event_id == "Ev-2"


def test_expired_lease_is_reclaimed_after_worker_loss(tmp_path) -> None:
    store = BridgeStore(tmp_path)
    payload = _payload(envelope_id="env-1", event_id="Ev-1")
    store.ingest_envelope(payload, _parse(payload))
    first = store.claim_inbox(lease_seconds=0)
    assert first is not None
    time.sleep(0.002)

    reclaimed = store.claim_inbox()
    assert reclaimed is not None
    assert reclaimed.row_id == first.row_id
    assert reclaimed.lease_token != first.lease_token


def test_outbox_request_dedupe_is_immutable_and_chunks_stay_fifo(tmp_path) -> None:
    store = BridgeStore(tmp_path)
    assert (
        store.enqueue_outbox(
            request_id="request-1",
            target="C1",
            thread_ts="thread-1",
            chunks=("first", "second"),
        )
        == 2
    )
    assert (
        store.enqueue_outbox(
            request_id="request-1",
            target="C2",
            thread_ts="different",
            chunks=("replacement", "must", "not", "appear"),
        )
        == 2
    )

    first = store.claim_outbox()
    assert first is not None and first.text == "first"
    assert store.claim_outbox() is None
    store.complete_outbox(first.row_id, first.lease_token)
    second = store.claim_outbox()
    assert second is not None and second.text == "second"
    assert second.target == "C1"


def test_terminal_outbox_failure_releases_later_fifo_item(tmp_path) -> None:
    store = BridgeStore(tmp_path)
    store.enqueue_outbox(
        request_id="request-1", target="C1", thread_ts="thread-1", chunks=("first",)
    )
    store.enqueue_outbox(
        request_id="request-2", target="C1", thread_ts="thread-1", chunks=("second",)
    )

    first = store.claim_outbox()
    assert first is not None and first.text == "first"
    store.fail_outbox(first.row_id, first.lease_token, "permanent failure")

    second = store.claim_outbox()
    assert second is not None and second.text == "second"
    status = store.status()
    assert status["outbox_failed"] == 1
    assert status["last_delivery_error"] == "permanent failure"


def test_database_uses_wal_and_full_sync_for_ack_custody(tmp_path) -> None:
    store = BridgeStore(tmp_path)
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
