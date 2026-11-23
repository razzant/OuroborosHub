"""Multipart progress and outcome regressions without a real provider or timing-sensitive network."""

import asyncio
import contextlib
import io
import json
import sqlite3
import threading
import urllib.error
import urllib.request
import urllib.response
from types import SimpleNamespace

import pytest

from telegram_bot import api, runtime as runtime_module
from telegram_bot.custody import CustodyStore

from test_unknown_outcomes import CHAT, Host, Wire, _drain, _due, _plugin, _queue, _restart, _runtime, _source, _tools, wire


PIECE = api._UPLOAD_CHUNK
BAD_CAPTION = b'{"ok":false,"error_code":400,"description":"Bad Request: can\'t parse entities"}'


def test_file_counts_follow_chunk_writes_not_framing_or_reads(tmp_path):
    payload = b"a" * (PIECE + 7)
    path = _source(tmp_path, "пример.mp4", payload)
    empty = _source(tmp_path, "empty.txt", b"")
    calls = []
    progress = SimpleNamespace(check_active=lambda: None, wrote=calls.append, sent=lambda: calls.append("sent"))
    with contextlib.ExitStack() as opened:
        parts = api._multipart_parts("boundary", {"caption": "да"}, [("video", path), ("file1", empty)], opened)
        stream = api._multipart_stream(parts, progress)
        head = next(stream)
        assert b'filename="' + path.name.encode() + b'"' in head and "да".encode() in head
        assert next(stream) == payload[:PIECE] and calls == [0, 0]
        assert next(stream) == payload[PIECE:] and calls[-1] == PIECE
        assert b'filename="empty.txt"' in next(stream) and calls[-1] == len(payload)
        assert next(stream) == b"\r\n--boundary--\r\n" and "sent" not in calls
        assert list(stream) == [] and calls[-1] == "sent"


def test_a_file_cut_after_the_length_is_set_cannot_finish_the_multipart_body(tmp_path):
    path = _source(tmp_path, "shrinks.bin", b"s" * (2 * PIECE))
    with contextlib.ExitStack() as opened:
        parts = api._multipart_parts("b", {}, [("document", path)], opened)
        stream = api._multipart_stream(parts, None)
        next(stream)  # framing, while the file is still open at its original size
        path.write_bytes(b"s" * PIECE)
        assert len(next(stream)) == PIECE
        with pytest.raises(OSError, match="ended .* early"):
            next(stream)


def test_an_album_keeps_one_progress_record_and_text_has_none(tmp_path, wire):
    tools = _tools(tmp_path)
    video = b"v" * (2 * PIECE + 7)
    photo = b"jpg-fixture"
    album = _queue(tools, tmp_path, "album", "album", topic_id="7", reply_to_message_id="9", items=[
        {"kind": "video", "file_path": str(_source(tmp_path, "пример.mp4", video))},
        {"kind": "photo", "file_path": str(_source(tmp_path, "photo.jpg", photo))},
    ])
    text = _queue(tools, tmp_path, "message", "text")
    runtime = _runtime(tmp_path)
    queued = runtime.store.delivery_receipt(album)
    assert queued["state"] == "pending" and queued["queued_at"] and "transfer" not in queued
    _drain(runtime)
    [(fields, files)] = wire.sent("sendMediaGroup")
    assert fields["message_thread_id"] == "7" and json.loads(fields["reply_parameters"]) == {"message_id": 9}
    assert files == {"file0": ("пример.mp4", video), "file1": ("photo.jpg", photo)}  # parser checks Content-Length
    reopened = _restart(tmp_path).store
    receipt = reopened.delivery_receipt(album)
    assert receipt["state"] == "delivered"
    assert len(receipt["provider_receipt"]["messages"]) == 2
    assert all(message["message_id"] for message in receipt["provider_receipt"]["messages"])
    transfer = receipt["transfer"]
    assert (transfer["attempt"], transfer["request"], transfer["phase"], transfer["answer"]) == (
        1, 1, "awaiting_response", "ok")
    assert transfer["file_bytes_written"] == transfer["file_bytes_total"] == len(video) + len(photo)
    assert transfer["started_at"] <= transfer["updated_at"] <= transfer["ended_at"]
    assert "transfer" not in reopened.delivery_receipt(text)


def test_registered_receipt_shows_completed_local_writes_while_provider_has_not_answered(tmp_path, wire):
    # Loading the plugin in its own package gives the registered reader a separate store registry and connection.
    module, tools = _plugin(tmp_path)
    data = b"v" * (PIECE + 7)
    _queue(tools, tmp_path, "document", "in-flight", file_path=str(_source(tmp_path, "file.pdf", data)))
    runtime = _runtime(tmp_path)
    assert module.CustodyStore.shared(runtime.store.path) is not runtime.store
    receipt = tools["telegram_receipt"]
    wire.answer("sendDocument", "block")  # after consuming and checking the whole request body

    async def observe():
        task = asyncio.create_task(runtime.process_one_outbox())
        try:
            assert await asyncio.to_thread(wire.entered.wait, 10)
            before = receipt(request_id="in-flight")
            transfer = before["transfer"]
            assert not task.done() and before["state"] == "leased" and before["provider_receipt"] == {}
            assert transfer["phase"] == "awaiting_response"
            assert transfer["file_bytes_written"] == transfer["file_bytes_total"] == len(data)
            assert "answer" not in transfer and "ended_at" not in transfer
        finally:
            wire.gate.set()
            await task

    asyncio.run(observe())
    after = receipt(request_id="in-flight")
    assert after["state"] == "delivered" and after["transfer"]["answer"] == "ok"
    assert after["provider_receipt"]["message"]["message_id"]


@pytest.mark.parametrize("status", [200, 301, 302, 303, 307, 308])
def test_a_conflicting_success_or_redirect_has_no_direct_bot_api_answer(tmp_path, monkeypatch, status):
    delivery = _queue(_tools(tmp_path), tmp_path, "document", "file", caption="**bold**")
    preflight, requests = Wire(), []

    def http_open(_handler, request):
        # Stub the HTTP exchange, not the opener: urllib's real status and redirect handlers run.
        endpoint = request.full_url.rsplit("/", 1)[-1]
        headers = {}
        if endpoint in {"getMe", "deleteWebhook"}:
            code, body = 200, preflight(request).read()
        else:
            requests.append((request.get_method(), endpoint))
            assert (request.get_method(), endpoint) == ("POST", "sendDocument")
            assert sum(map(len, request.data)) == int(request.get_header("Content-length"))
            code, body = status, BAD_CAPTION
            if status != 200:
                headers["location"] = "/next"
                body = json.dumps({"ok": False, "error_code": status, "description": "Redirect"}).encode()
        headers["Content-Length"] = str(len(body))
        response = urllib.response.addinfourl(io.BytesIO(body), headers, request.full_url, code)
        response.msg = "Fixture response"
        return response

    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", http_open)
    _drain(_runtime(tmp_path))
    later = _restart(tmp_path)
    _drain(later)
    assert requests == [("POST", "sendDocument")]
    receipt = later.store.delivery_receipt(delivery)
    assert receipt["state"] == "uncertain" and receipt["provider_receipt"] == {}
    transfer = receipt["transfer"]
    assert transfer["answer"] == "none" and transfer["request"] == 1 and "error_code" not in transfer


@pytest.mark.parametrize("body,declared", [
    pytest.param(b"<html>400 Bad Request</html>", None, id="gateway"),
    pytest.param(BAD_CAPTION.replace(b"400", b"429"), None, id="wrong-code"),
    pytest.param(BAD_CAPTION, len(BAD_CAPTION) + 20, id="valid-json-truncated"),
    pytest.param(BAD_CAPTION + b" " * (64 * 1024), None, id="valid-json-oversize"),
])
def test_a_nonanswer_cannot_trigger_caption_fallback_or_restart_retry(tmp_path, wire, body, declared):
    delivery = _queue(_tools(tmp_path), tmp_path, "document", "file", caption="**bold**")
    headers = {} if declared is None else {"Content-Length": str(declared)}
    fp = io.BytesIO(body)
    wire.answer("sendDocument", urllib.error.HTTPError("http://telegram.invalid", 400, "bad", headers, fp))
    _drain(_runtime(tmp_path))
    runtime = _restart(tmp_path)
    _drain(runtime)
    [(fields, _)] = wire.sent("sendDocument")
    receipt = runtime.store.delivery_receipt(delivery)
    assert fields["parse_mode"] == "HTML" and fp.closed
    assert receipt["state"] == "uncertain" and receipt["provider_receipt"] == {}
    assert (receipt["transfer"]["request"], receipt["transfer"]["phase"], receipt["transfer"]["answer"]) == (
        1, "awaiting_response", "none")


def test_a_complete_telegram_400_allows_one_fresh_plain_request(tmp_path, wire):
    delivery = _queue(_tools(tmp_path), tmp_path, "document", "file", caption="**bold**")
    error = urllib.error.HTTPError("http://telegram.invalid", 400, "bad",
                                   {"Content-Length": str(len(BAD_CAPTION))}, io.BytesIO(BAD_CAPTION))
    wire.answer("sendDocument", error)
    runtime = _runtime(tmp_path)
    _drain(runtime)
    assert [fields.get("parse_mode") for fields, _ in wire.sent("sendDocument")] == ["HTML", None]
    receipt = runtime.store.delivery_receipt(delivery)
    assert receipt["state"] == "delivered" and receipt["provider_receipt"]["message"]["message_id"]
    transfer = receipt["transfer"]
    assert (transfer["attempt"], transfer["request"], transfer["answer"]) == (1, 2, "ok")
    assert "error_code" not in transfer


@pytest.mark.parametrize("reason", ["cancelled", "reclaimed", "cancelled_during_read"])
def test_an_interrupted_attempt_cannot_finish_the_body_or_send_it_again(tmp_path, monkeypatch, wire, reason):
    delivery = _queue(_tools(tmp_path), tmp_path, "document", "film", file_path=str(
        _source(tmp_path, "large.pdf", b"x" * (3 * PIECE))))
    runtime = _runtime(tmp_path)
    other = CustodyStore(runtime.store.path)
    monkeypatch.setattr(runtime_module, "_TRANSFER_RECORD_SECONDS", 0)
    held, release = threading.Event(), threading.Event()
    posts, reached = [], []
    block_on_read = reason == "cancelled_during_read"

    def hold_until_interrupted():
        held.set()
        assert release.wait(10)

    if block_on_read:
        real_parts = api._multipart_parts

        def blocked_second_read(*args):
            parts = real_parts(*args)
            handle, size = parts[1]

            def read(count):
                if handle.tell() == PIECE:
                    hold_until_interrupted()
                return handle.read(count)

            parts[1] = SimpleNamespace(read=read, name=handle.name), size
            return parts

        monkeypatch.setattr(api, "_multipart_parts", blocked_second_read)

    def open_request(request, *, timeout):
        posts.append(request.full_url)
        index, written = -1, 0
        try:
            for index, piece in enumerate(request.data):
                written += len(piece)
                if not block_on_read and index == 2:  # two file writes, of which the generator acknowledged the first
                    hold_until_interrupted()
            raise AssertionError("the complete body was written")
        except runtime_module.LeaseLost:
            reached.append((index, written, int(request.get_header("Content-length"))))
            raise

    monkeypatch.setattr(api, "_open_multipart", open_request)

    async def scenario():
        task = asyncio.create_task(runtime.process_one_outbox())
        try:
            assert await asyncio.to_thread(held.wait, 10)
            transfer = other.delivery_receipt(delivery)["transfer"]
            if reason != "reclaimed":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                other.release_own_leases()
                assert other.claim_outbox(lease_sec=120).attempts == 2
            return transfer
        finally:
            release.set()
            if not task.done():
                await task

    transfer = asyncio.run(scenario())  # asyncio.run also awaits its executor's worker at shutdown
    assert transfer["phase"] == "uploading" and transfer["file_bytes_written"] == PIECE
    [(last_yield, written, length)] = reached
    assert last_yield == (1 if block_on_read else 2)  # the block read after Stop never reached the wire
    assert written < length
    _drain(_restart(tmp_path))
    ended = other.delivery_receipt(delivery)
    assert ended["state"] == "uncertain" and ended["transfer"] == transfer and len(posts) == 1
    assert "ended_at" not in ended["transfer"] and "answer" not in ended["transfer"]


def test_observation_failures_are_throttled_and_ending_does_not_fake_progress(monkeypatch):
    clock = [0.0, 100.0]  # monotonic time, wall clock
    monkeypatch.setattr(runtime_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(time=lambda: clock[1]))
    saved = []

    def busy_then_writable(_delivery, _attempt, record, *, lease_sec):
        saved.append(dict(record))
        if len(saved) < 4:
            raise sqlite3.OperationalError("disk busy")
        return True

    upload = runtime_module._Upload(SimpleNamespace(record_transfer=busy_then_writable), "file", 1)
    upload.begin(100)
    upload.wrote(0)
    clock[:] = [1.0, 110.0]
    upload.wrote(1)
    for count in range(2, 100):
        upload.wrote(count)
    assert [row["file_bytes_written"] for row in saved] == [0, 0, 1]
    clock[:] = [2.0, 120.0]
    upload.ended("none")
    assert (saved[-1]["file_bytes_written"], saved[-1]["updated_at"], saved[-1]["ended_at"]) == (99, 110.0, 120.0)


def test_a_pre_transfer_store_does_not_invent_progress(tmp_path):
    path = tmp_path / "custody.sqlite3"
    original = CustodyStore(path)
    original.enqueue_outbox("telegram-send:old", {"kind": "video", "chat_id": CHAT})
    original.close()
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.execute("ALTER TABLE outbox DROP COLUMN transfer_json")  # the prior schema
    receipt = CustodyStore(path).delivery_receipt("telegram-send:old")
    assert receipt["state"] == "pending" and receipt["queued_at"] and "transfer" not in receipt


@pytest.mark.parametrize("state,error", [
    ("pending", "TelegramApiError: Telegram sendDocument failed (400): HTTP error"),
    ("pending", "TelegramApiError: Telegram sendDocument failed (400): invalid provider response"),
    ("pending", "TelegramApiError: Telegram sendDocument failed (429): Too Many Requests: retry after 1"),
    ("leased", ""),  # the earlier parser removed the marker, then the process died before release
])
def test_a_legacy_marked_file_without_proof_ends_uncertain_before_preflight(tmp_path, wire, state, error):
    delivery = _queue(_tools(tmp_path), tmp_path, "document", "legacy")
    original = _runtime(tmp_path).store
    # 0.12.1's first counted attempt, with its marker cleared. A real 429 and a mismatched HTTP status
    # with the same JSON had the same last_error; the shared stamp says nothing about which reply arrived.
    with sqlite3.connect(original.path) as db:
        db.execute("UPDATE outbox SET attempts=1,state=?,last_error=?,payload_json=json_set(payload_json, "
                   "'$._attempts_marked',1,'$._reporting.version',1) WHERE delivery_id=?", (state, error, delivery))
    original.close()
    later = _restart(tmp_path)
    assert asyncio.run(later.process_one_outbox())
    assert not asyncio.run(later.process_one_outbox()) and wire.attempted == []
    receipt = later.store.delivery_receipt(delivery)
    assert receipt["state"] == "uncertain" and receipt["provider_receipt"] == {} and "transfer" not in receipt


def _resume_after_legacy_file_attempt(original, tmp_path, delivery, payload, error, *, attempts=1, reporting=1):
    # 0.12.1 counted this attempt and kept its last error but never wrote the new file stamp.
    payload = {**payload, "_attempts_marked": attempts, "_reporting": {**payload["_reporting"], "version": reporting}}
    with contextlib.closing(sqlite3.connect(original.path)) as db, db:
        db.execute("UPDATE outbox SET attempts=?,last_error=?,payload_json=? WHERE delivery_id=?",
                   (attempts, error, json.dumps(payload), delivery))
    original.close()
    later = _restart(tmp_path)
    assert asyncio.run(later.process_one_outbox())
    receipt = later.store.delivery_receipt(delivery)
    later.store.close()
    assert not asyncio.run(_restart(tmp_path).process_one_outbox())
    return receipt


@pytest.mark.parametrize("kind,caption,extra,expected_posts", [
    pytest.param("document", "", {}, 1, id="file-empty-html"),
    pytest.param("album", "", {}, 1, id="album-empty-html"),
    pytest.param("document", "**heading**", {}, 0, id="file-nonempty-html"),
    pytest.param("album", "**heading**", {}, 0, id="album-mixed-html"),
    pytest.param("document", "plain", {"markdown": False}, 1, id="file-plain"),
    pytest.param("album", "plain", {"markdown": False}, 1, id="album-plain"),
    pytest.param("document", "", {"missing_text": True}, 0, id="missing-caption-text"),
    pytest.param("album", "", {"malformed_item": True}, 0, id="malformed-album-item"),
    pytest.param("album", "**heading**", {"missing_item": True}, 0, id="incomplete-rendered-album"),
    pytest.param("album", "", {"attempts": 2}, 0, id="only-the-last-error-survived"),
])
def test_legacy_file_body_failure_retries_only_without_possible_caption_fallback(
        tmp_path, wire, kind, caption, extra, expected_posts):
    args = {} if extra.get("markdown", True) else {"markdown": False}
    if kind == "album":
        # Only the last item can trigger HTML fallback in the mixture case.
        captions = ("", "", caption) if extra.get("missing_item") else ("", caption)
        args["items"] = [{"kind": "photo", "file_path": str(_source(tmp_path, f"{index}.jpg")), "caption": text}
                         for index, text in enumerate(captions)]
    else:
        args["caption"] = caption
    delivery = _queue(_tools(tmp_path), tmp_path, kind, "legacy-build", **args)
    original = _runtime(tmp_path).store
    payload = original.outbox_payload(delivery)
    if not caption:
        prepared = ([item["caption"] for item in payload["_rendered_items"]] if kind == "album"
                    else [payload["_rendered_caption"]])
        assert prepared == [{"text": "", "parse_mode": "HTML"}] * (2 if kind == "album" else 1)
    if extra.get("missing_text"):
        del payload["_rendered_caption"]["text"]
    if extra.get("malformed_item"):
        payload["_rendered_items"][-1] = None
    if extra.get("missing_item"):
        payload["_rendered_items"].pop()  # the absent item carried the HTML caption
    receipt = _resume_after_legacy_file_attempt(
        original, tmp_path, delivery, payload, "TelegramRequestNotSent: Telegram upload failed (transport): FileNotFoundError",
        attempts=extra.get("attempts", 1))
    method = "sendMediaGroup" if kind == "album" else "sendDocument"
    assert len(wire.sent(method)) == expected_posts
    if expected_posts:
        assert receipt["state"] == "delivered" and receipt["transfer"]["answer"] == "ok"
    else:
        assert wire.attempted == [] and receipt["state"] == "uncertain" and "transfer" not in receipt


def test_legacy_file_worker_cancelled_before_reporting_resumes_once(tmp_path, wire):
    delivery = _queue(_tools(tmp_path), tmp_path, "document", "legacy-preflight")
    original = _runtime(tmp_path).store
    payload = original.outbox_payload(delivery)
    assert payload["_reporting"]["version"] == -1
    receipt = _resume_after_legacy_file_attempt(original, tmp_path, delivery, payload, "worker_cancelled", reporting=-1)
    assert receipt["state"] == "delivered" and receipt["attempts"] == 2
    assert len(wire.sent("sendDocument")) == 1


@pytest.mark.parametrize("rollback", [False, True])
def test_a_current_refusal_retries_across_hold_and_restart_but_not_an_old_reply_after_rollback(
        tmp_path, wire, monkeypatch, rollback):
    class Paused(Host):
        async def place_hold(self, *args, **kwargs):
            return "Held: Telegram is paused"

    def no_observation(*args, **kwargs):
        raise sqlite3.OperationalError("progress unavailable")

    monkeypatch.setattr(CustodyStore, "record_transfer", no_observation)
    delivery = _queue(_tools(tmp_path), tmp_path, "document", "retry")
    wire.answer("sendDocument", 429)
    run = _runtime(tmp_path)
    assert asyncio.run(run.process_one_outbox())  # genuine current refusal; retry due in 2s
    assert run.store.delivery_receipt(delivery)["state"] == "pending"
    run.store.close()
    held = _restart(tmp_path, Paused())
    _due(held.store)
    assert asyncio.run(held.process_one_outbox())
    receipt = held.store.delivery_receipt(delivery)
    payload = held.store.outbox_payload(delivery)
    assert receipt["state"] == "held" and "transfer" not in receipt
    assert payload["_attempts_marked"] == payload["_file_attempts_marked"] == receipt["attempts"] == 1
    if rollback:
        # The 0.12.1 runtime moves only its stamp, not the file stamp. A hold must not leave the file stamp
        # one count ahead, where it would accidentally attest this later old attempt as current.
        with sqlite3.connect(held.store.path) as db:
            db.execute("UPDATE outbox SET attempts=2,last_error=?,payload_json=json_set(payload_json, "
                       "'$._attempts_marked',2) WHERE delivery_id=?",
                       ("TelegramApiError: Telegram sendDocument failed (400): HTTP error", delivery))
    held.store.close()
    later = _restart(tmp_path)
    _drain(later)
    assert later.store.delivery_receipt(delivery)["state"] == ("uncertain" if rollback else "delivered")
    assert len(wire.sent("sendDocument")) == (1 if rollback else 2)
