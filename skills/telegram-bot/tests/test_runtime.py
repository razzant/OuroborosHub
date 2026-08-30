import asyncio
import pathlib
import sqlite3

from telegram_bot.api import TelegramClient
from telegram_bot.events import parse_telegram_update
from telegram_bot.host import (
    PresenceHostHTTPError,
    PresenceSubmission,
    PresenceWorkResult,
)
from telegram_bot.runtime import TelegramTransportRuntime


class Logger:
    def log(self, _level, _message):
        pass


class AcceptingSubmitter:
    def __init__(self):
        self.calls = []

    async def submit(self, event, staged_files):
        self.calls.append((event, tuple(staged_files)))
        return PresenceSubmission(
            status="completed",
            outcome="message",
            text="hello back",
            turn_ref="turn-1",
            work_ref="",
            binding_id="b" * 32,
        )

    async def poll(self, work_ref, binding_id):
        return PresenceWorkResult(
            status="completed", outcome="message", work_ref=work_ref
        )


class RuntimeClient(TelegramClient):
    def __init__(self, token):
        self.token = token
        self.downloads = []
        self.sent = []

    async def delete_webhook(self):
        return True

    async def get_me(self):
        return {"id": 9, "username": "presence_bot"}

    async def download_file(self, file_id, destination):
        destination = pathlib.Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"staged")
        self.downloads.append((file_id, destination))
        return destination

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(("message", chat_id, text, kwargs))
        return [{"message_id": 1}]

    async def send_photo(self, chat_id, file_path, **kwargs):
        self.sent.append(("photo", chat_id, str(file_path), kwargs))
        return {"message_id": 2}

    async def send_document(self, chat_id, file_path, **kwargs):
        self.sent.append(("document", chat_id, str(file_path), kwargs))
        return {"message_id": 3}


def test_accepted_submission_stages_then_removes_provider_temp(tmp_path):
    asyncio.run(_accepted_submission_stages_then_removes_provider_temp(tmp_path))


async def _accepted_submission_stages_then_removes_provider_temp(tmp_path):
    submitter = AcceptingSubmitter()
    clients = []

    def factory(token):
        client = RuntimeClient(token)
        clients.append(client)
        return client

    runtime = TelegramTransportRuntime(
        state_dir=tmp_path,
        token_provider=lambda: "token",
        logger=Logger(),
        submitter=submitter,
        client_factory=factory,
    )
    event = parse_telegram_update(
        {
            "update_id": 5,
            "message": {
                "message_id": 2,
                "caption": "photo",
                "from": {"id": 3, "is_bot": False},
                "chat": {"id": 4, "type": "private"},
                "photo": [{"file_id": "p", "file_unique_id": "u", "file_size": 4}],
            },
        },
        bot_account_id="9",
    )
    assert event is not None
    runtime.store.commit_update(5, event)

    assert await runtime.process_one_inbox()
    assert submitter.calls
    staged = submitter.calls[0][1][0]
    assert not staged.exists()
    assert runtime.store.status_snapshot()["inbox_submitted"] == 1
    delivery = runtime.store.claim_outbox()
    assert delivery is not None
    assert delivery.payload["text"] == "hello back"
    assert delivery.payload["chat_id"] == "4"
    assert delivery.payload["reply_to_message_id"] == 2


def test_outbox_worker_delivers_all_supported_provider_kinds(tmp_path):
    asyncio.run(_outbox_worker_delivers_all_supported_provider_kinds(tmp_path))


async def _outbox_worker_delivers_all_supported_provider_kinds(tmp_path):
    clients = []

    def factory(token):
        client = RuntimeClient(token)
        clients.append(client)
        return client

    runtime = TelegramTransportRuntime(
        state_dir=tmp_path,
        token_provider=lambda: "token",
        logger=Logger(),
        submitter=AcceptingSubmitter(),
        client_factory=factory,
    )
    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"p")
    doc = tmp_path / "d.pdf"
    doc.write_bytes(b"d")
    runtime.store.enqueue_outbox(
        "m", {"kind": "message", "chat_id": "1", "text": "hello"}
    )
    runtime.store.enqueue_outbox(
        "p", {"kind": "photo", "chat_id": "1", "file_path": str(photo)}
    )
    runtime.store.enqueue_outbox(
        "d", {"kind": "document", "chat_id": "1", "file_path": str(doc)}
    )

    assert await runtime.process_one_outbox()
    assert await runtime.process_one_outbox()
    assert await runtime.process_one_outbox()
    assert [item[0] for item in clients[0].sent] == ["message", "photo", "document"]
    assert runtime.store.status_snapshot()["outbox_delivered"] == 3


class DeferredSubmitter:
    def __init__(self):
        self.polls = []

    async def submit(self, event, staged_files):
        del event, staged_files
        return PresenceSubmission(
            status="completed",
            outcome="deferred",
            text="",
            turn_ref="turn-5",
            work_ref="work-5",
            binding_id="c" * 32,
        )

    async def poll(self, work_ref, binding_id):
        self.polls.append((work_ref, binding_id))
        return PresenceWorkResult(
            status="completed",
            outcome="message",
            text="late answer",
            work_ref=work_ref,
        )


def test_deferred_work_is_polled_and_late_text_is_queued_once(tmp_path):
    asyncio.run(_deferred_work_is_polled_and_late_text_is_queued_once(tmp_path))


async def _deferred_work_is_polled_and_late_text_is_queued_once(tmp_path):
    submitter = DeferredSubmitter()
    runtime = TelegramTransportRuntime(
        state_dir=tmp_path,
        token_provider=lambda: "token",
        logger=Logger(),
        submitter=submitter,
        client_factory=RuntimeClient,
    )
    event = parse_telegram_update(
        {
            "update_id": 5,
            "message": {
                "message_id": 2,
                "text": "research this",
                "from": {"id": 3, "is_bot": False},
                "chat": {"id": 4, "type": "private"},
            },
        },
        bot_account_id="9",
    )
    assert event is not None
    runtime.store.commit_update(5, event)

    assert await runtime.process_one_inbox()
    assert runtime.store.status_snapshot()["work_waiting"] == 1
    assert await runtime.process_one_work()
    assert submitter.polls == [("work-5", "c" * 32)]
    delivery = runtime.store.claim_outbox()
    assert delivery is not None and delivery.payload["text"] == "late answer"
    assert runtime.store.claim_outbox() is None


class RejectingSubmitter(AcceptingSubmitter):
    async def submit(self, event, staged_files):
        if event["source_event_id"].endswith(":50"):
            raise PresenceHostHTTPError(403, "binding mismatch")
        return await super().submit(event, staged_files)


def test_binding_rejection_fails_inbox_and_does_not_block_conversation(tmp_path):
    asyncio.run(
        _binding_rejection_fails_inbox_and_does_not_block_conversation(tmp_path)
    )


async def _binding_rejection_fails_inbox_and_does_not_block_conversation(tmp_path):
    runtime = TelegramTransportRuntime(
        state_dir=tmp_path,
        token_provider=lambda: "token",
        logger=Logger(),
        submitter=RejectingSubmitter(),
        client_factory=RuntimeClient,
    )
    for update_id in (50, 51):
        event = parse_telegram_update(
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "text": "hello",
                    "from": {"id": 3, "is_bot": False},
                    "chat": {"id": 4, "type": "private"},
                },
            },
            bot_account_id="9",
        )
        assert event is not None
        runtime.store.commit_update(update_id, event)

    assert await runtime.process_one_inbox()
    assert runtime.store.status_snapshot()["inbox_failed"] == 1
    assert await runtime.process_one_inbox()
    assert runtime.store.status_snapshot()["inbox_submitted"] == 1


class FailingDeliveryClient(RuntimeClient):
    async def send_message(self, chat_id, text, **kwargs):
        if text == "never delivers":
            raise RuntimeError("provider rejected delivery")
        return await super().send_message(chat_id, text, **kwargs)


def test_outbox_failure_is_bounded_and_later_delivery_proceeds(tmp_path):
    asyncio.run(_outbox_failure_is_bounded_and_later_delivery_proceeds(tmp_path))


async def _outbox_failure_is_bounded_and_later_delivery_proceeds(tmp_path):
    runtime = TelegramTransportRuntime(
        state_dir=tmp_path,
        token_provider=lambda: "token",
        logger=Logger(),
        submitter=AcceptingSubmitter(),
        client_factory=FailingDeliveryClient,
    )
    runtime.store.enqueue_outbox(
        "failed",
        {"kind": "message", "chat_id": "1", "text": "never delivers"},
    )
    for attempt in range(5):
        assert await runtime.process_one_outbox()
        if attempt < 4:
            with sqlite3.connect(runtime.store.path) as conn:
                conn.execute(
                    "UPDATE outbox SET available_at=0 WHERE delivery_id='failed'"
                )
    assert runtime.store.status_snapshot()["outbox_failed"] == 1

    runtime.store.enqueue_outbox(
        "later",
        {"kind": "message", "chat_id": "1", "text": "later succeeds"},
    )
    assert await runtime.process_one_outbox()
    snapshot = runtime.store.status_snapshot()
    assert snapshot["outbox_failed"] == 1
    assert snapshot["outbox_delivered"] == 1
