import asyncio
import json
import pathlib
import sqlite3

import pytest

from telegram_bot.api import TelegramApiError, TelegramClient
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
        self.operations = []

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

    async def send_text_chunk(self, chat_id, text, **kwargs):
        return (await self.send_message(chat_id, text, **kwargs))[0]

    async def send_media(self, kind, chat_id, file_path, *, prepared_caption, **kwargs):
        method = self.send_photo if kind == "photo" else self.send_document
        return await method(
            chat_id, file_path, caption=prepared_caption["text"], **kwargs
        )

    async def send_photo(self, chat_id, file_path, **kwargs):
        self.sent.append(("photo", chat_id, str(file_path), kwargs))
        return {"message_id": 2}

    async def send_document(self, chat_id, file_path, **kwargs):
        self.sent.append(("document", chat_id, str(file_path), kwargs))
        return {"message_id": 3}

    async def operation(self, method, parameters):
        self.operations.append((method, parameters))
        return {"message_id": parameters["message_id"], "method": method}


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


def test_outbox_worker_delivers_own_edit_and_reaction_operations(tmp_path):
    asyncio.run(_outbox_worker_delivers_own_edit_and_reaction_operations(tmp_path))


async def _outbox_worker_delivers_own_edit_and_reaction_operations(tmp_path):
    clients = []

    def factory(token):
        client = RuntimeClient(token)
        clients.append(client)
        return client

    runtime = TelegramTransportRuntime(
        state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(),
        submitter=AcceptingSubmitter(), client_factory=factory,
    )
    runtime.store.enqueue_outbox(
        "edit", {"kind": "operation", "chat_id": "1", "method": "editMessageText",
                  "parameters": {"chat_id": "1", "message_id": 8, "text": "fixed"}, "text": "fixed"},
    )
    runtime.store.enqueue_outbox(
        "reaction", {"kind": "operation", "chat_id": "1", "method": "setMessageReaction",
                      "parameters": {"chat_id": "1", "message_id": 8, "reaction": []}},
    )
    assert await runtime.process_one_outbox()
    assert await runtime.process_one_outbox()
    assert [item[0] for item in clients[0].operations] == ["editMessageText", "setMessageReaction"]
    assert runtime.store.status_snapshot()["outbox_delivered"] == 2


def test_operation_checkpoint_survives_cancel_without_second_provider_call(tmp_path):
    asyncio.run(_operation_checkpoint_survives_cancel_without_second_provider_call(tmp_path))


async def _operation_checkpoint_survives_cancel_without_second_provider_call(tmp_path):
    provider = RuntimeClient("token")
    runtime = TelegramTransportRuntime(
        state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(),
        submitter=AcceptingSubmitter(), client_factory=lambda _token: provider,
    )
    runtime.store.enqueue_outbox("telegram-operation:once", {
        "kind": "operation", "chat_id": "1", "method": "editMessageText",
        "parameters": {"chat_id": "1", "message_id": 8, "text": "fixed"}, "text": "fixed",
    })
    mark_delivered = runtime.store.mark_delivered
    def interrupt(*_args, **_kwargs):
        raise asyncio.CancelledError()
    runtime.store.mark_delivered = interrupt
    try:
        try:
            await runtime.process_one_outbox()
        except asyncio.CancelledError:
            pass
        assert runtime.store.outbox_payload("telegram-operation:once")["_operation_result"]["message_id"] == 8
    finally:
        runtime.store.mark_delivered = mark_delivered
    assert await runtime.process_one_outbox()
    assert runtime.store.delivery_receipt("telegram-operation:once")["state"] == "delivered"
    assert len(provider.operations) == 1


def test_ambiguous_operation_is_uncertain_and_never_auto_replayed(tmp_path):
    async def run():
        class LostResponse(RuntimeClient):
            async def operation(self, method, parameters):
                self.operations.append((method, parameters))
                raise TelegramApiError(method, "response lost")
        provider = LostResponse("token")
        runtime = TelegramTransportRuntime(
            state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(),
            submitter=AcceptingSubmitter(), client_factory=lambda _token: provider,
        )
        runtime.store.enqueue_outbox("telegram-operation:uncertain", {
            "kind": "operation", "chat_id": "1", "method": "setMessageReaction",
            "parameters": {"chat_id": "1", "message_id": 8, "reaction": []},
        })
        assert await runtime.process_one_outbox()
        assert runtime.store.delivery_receipt("telegram-operation:uncertain")["state"] == "uncertain"
        assert runtime.store.status_snapshot()["outbox_uncertain"] == 1
        assert runtime.store.status_snapshot()["outbox_failed"] == 0
        assert not await runtime.process_one_outbox()
        assert len(provider.operations) == 1
    asyncio.run(run())


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
    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    async def submit(self, event, staged_files):
        if event["source_event_id"].endswith(":50"):
            raise PresenceHostHTTPError(403, "binding mismatch", self.payload)
        return await super().submit(event, staged_files)


def test_typed_rejection_fails_inbox_and_does_not_block_conversation(tmp_path):
    asyncio.run(
        _typed_rejection_fails_inbox_and_does_not_block_conversation(tmp_path)
    )


async def _typed_rejection_fails_inbox_and_does_not_block_conversation(tmp_path):
    rejected = {"ok": False, "error": "presence event does not match its owner-created binding",
                "code": "presence_origin_mismatch", "disposition": "rejected"}
    runtime = TelegramTransportRuntime(
        state_dir=tmp_path,
        token_provider=lambda: "token",
        logger=Logger(),
        submitter=RejectingSubmitter(rejected),
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
    with sqlite3.connect(runtime.store.path) as conn:
        receipt = conn.execute(
            "SELECT host_receipt_json FROM inbox WHERE event_id='telegram:9:50'").fetchone()[0]
    assert json.loads(receipt) == {"http_status": 403, "response": rejected}
    assert await runtime.process_one_inbox()
    assert runtime.store.status_snapshot()["inbox_submitted"] == 1


@pytest.mark.parametrize("status_code,payload", [
    (403, {"error": "binding mismatch"}),  # legacy Host: no typed disposition
    (404, {"detail": "Not Found"}),
    (403, {"ok": False, "code": "presence_auth_blocked", "disposition": "blocked"}),
    (409, {"ok": False, "code": "presence_start_unwritable", "disposition": "retry"}),
    (409, {"ok": False, "code": "presence_attempt_outcome_unknown", "disposition": "retained"}),
])
def test_untyped_blocked_and_retry_refusals_keep_the_event_first(tmp_path, status_code, payload):
    async def run():
        runtime = TelegramTransportRuntime(
            state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(),
            submitter=RejectingSubmitter(payload), client_factory=RuntimeClient,
        )
        runtime.submitter.calls.clear()
        for update_id in (50, 51):
            runtime.store.commit_update(update_id, parse_telegram_update({
                "update_id": update_id,
                "message": {"message_id": update_id, "text": "hello", "from": {"id": 3},
                            "chat": {"id": 4, "type": "private"}},
            }, bot_account_id="9"))
        for _ in range(3):
            with sqlite3.connect(runtime.store.path) as conn:
                conn.execute("UPDATE inbox SET available_at=0")
            assert await runtime.process_one_inbox()
            snapshot = runtime.store.status_snapshot()
            # A retained receipt without its original turn is unusable, so it retries too.
            assert snapshot["inbox_waiting"] == 2 and snapshot["inbox_failed"] == 0
            assert snapshot["inbox_submitted"] == 0 and snapshot["work_waiting"] == 0
        assert runtime.submitter.calls == []  # the later event never overtook the first

    asyncio.run(run())


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


def test_early_outbound_delivery_while_host_turn_is_still_running(tmp_path):
    async def run():
        from telegram_bot.custody import CustodyStore

        admitted, finish = asyncio.Event(), asyncio.Event()

        class SlowHost(AcceptingSubmitter):
            async def submit(self, event, staged_files):
                admitted.set()
                await finish.wait()
                return PresenceSubmission(
                    "completed", "silent", "", "turn-1", "", "b" * 32
                )

        runtime = TelegramTransportRuntime(
            state_dir=tmp_path,
            token_provider=lambda: "token",
            logger=Logger(),
            submitter=SlowHost(),
            client_factory=RuntimeClient,
        )
        event = parse_telegram_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 2,
                    "text": "read this",
                    "from": {"id": 3},
                    "chat": {"id": 4, "type": "private"},
                },
            },
            bot_account_id="9",
        )
        runtime.store.commit_update(1, event)
        turn = asyncio.create_task(runtime.process_one_inbox())
        try:
            await asyncio.wait_for(admitted.wait(), 1)
            # An extension tool in the LLM worker writes the same durable outbox.
            CustodyStore(tmp_path / "custody.sqlite3").enqueue_outbox(
                "telegram-send:ack",
                {
                    "kind": "message",
                    "chat_id": "4",
                    "text": "Reading it now",
                    "reply_to_message_id": 2,
                },
            )
            assert await runtime.process_one_outbox()
            assert not turn.done()
            assert runtime._client.sent[0][2] == "Reading it now"
            assert (
                runtime.store.delivery_receipt("telegram-send:ack")["state"]
                == "delivered"
            )
        finally:
            finish.set()
            await turn
        assert runtime.store.claim_outbox() is None

    asyncio.run(run())


def test_tool_delivered_does_not_echo_explanatory_text(tmp_path):
    async def run():
        class Host(AcceptingSubmitter):
            async def submit(self, event, staged_files):
                return PresenceSubmission(
                    "completed",
                    "tool_delivered",
                    "Already sent via tool",
                    "turn-1",
                    "",
                    "b" * 32,
                )

        runtime = TelegramTransportRuntime(
            state_dir=tmp_path,
            token_provider=lambda: "token",
            logger=Logger(),
            submitter=Host(),
            client_factory=RuntimeClient,
        )
        event = parse_telegram_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 2,
                    "text": "hi",
                    "from": {"id": 3},
                    "chat": {"id": 4, "type": "private"},
                },
            },
            bot_account_id="9",
        )
        runtime.store.commit_update(1, event)
        assert await runtime.process_one_inbox()
        assert runtime.store.claim_outbox() is None

    asyncio.run(run())


def test_long_text_retry_keeps_confirmed_chunks_after_restart(tmp_path):
    async def run():
        class FlakyClient(RuntimeClient):
            def __init__(self, token):
                super().__init__(token)
                self.failed = False

            async def send_message(self, chat_id, text, **kwargs):
                if text == "b" * 100 and not self.failed:
                    self.failed = True
                    raise RuntimeError("second chunk temporary failure")
                return await super().send_message(chat_id, text, **kwargs)

        client = FlakyClient("token")

        def runtime():
            return TelegramTransportRuntime(
                state_dir=tmp_path,
                token_provider=lambda: "token",
                logger=Logger(),
                submitter=AcceptingSubmitter(),
                client_factory=lambda token: client,
            )

        first = runtime()
        first.store.enqueue_outbox(
            "long", {"kind": "message", "chat_id": "1", "text": "a" * 4096 + "b" * 100}
        )
        assert await first.process_one_outbox()
        assert [entry[2] for entry in client.sent] == ["a" * 4096]
        with sqlite3.connect(first.store.path) as conn:
            conn.execute("UPDATE outbox SET available_at=0 WHERE delivery_id='long'")
        resumed = runtime()
        assert await resumed.process_one_outbox()
        assert [entry[2] for entry in client.sent] == ["a" * 4096, "b" * 100]
        assert (
            len(resumed.store.delivery_receipt("long")["provider_receipt"]["messages"])
            == 2
        )

    asyncio.run(run())


def test_restarted_runtime_resumes_a_leased_inbox_row_immediately(tmp_path):
    async def run():
        from telegram_bot.custody import CustodyStore

        event = parse_telegram_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 2,
                    "text": "answer me",
                    "from": {"id": 3},
                    "chat": {"id": 4, "type": "private"},
                },
            },
            bot_account_id="9",
        )
        # A previous process leased the event for a Host turn and then died.
        dead = CustodyStore(tmp_path / "custody.sqlite3")
        dead.commit_update(1, event)
        assert dead.claim_inbox(lease_sec=1860.0) is not None

        class StopAfterPoll(RuntimeClient):
            async def get_updates(self, *, offset, timeout_sec):
                await asyncio.sleep(0.05)
                return []

        submitter = AcceptingSubmitter()
        runtime = TelegramTransportRuntime(
            state_dir=tmp_path,
            token_provider=lambda: "token",
            logger=Logger(),
            submitter=submitter,
            client_factory=StopAfterPoll,
        )
        task = asyncio.create_task(runtime.run())
        try:
            for _ in range(100):
                if runtime.store.status_snapshot()["inbox_submitted"] == 1:
                    break
                await asyncio.sleep(0.02)
        finally:
            runtime.stop()
            await asyncio.gather(task, return_exceptions=True)
        assert [call[0]["text"] for call in submitter.calls] == ["answer me"]
        assert runtime.store.status_snapshot()["inbox_submitted"] == 1

    asyncio.run(run())
