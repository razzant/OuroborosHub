"""A send whose outcome is unknown is never made again, and a queued file goes out as the bytes it had (0.12.1).

Telegram's side is urllib's opener, replaced in-process: the real ``TelegramClient``, its urllib transport
with its error classification, the custody store and the runtime run unchanged. Every request that reached the fake
is recorded with its exact body. Data is synthetic, the API server is ``telegram.invalid``, and nothing leaves the
process. The behavioural tests use only what 0.12.0 already had, so the same file is a negative control on it.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import socket
import sqlite3
import ssl
import stat
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from telegram_bot.api import TelegramClient
from telegram_bot.runtime import TelegramTransportRuntime

from test_plugin import FakeApi
from test_runtime import Logger

SKILL = pathlib.Path(__file__).resolve().parents[1]
CHAT = "-1001234"
FIRST, SECOND = b"F" * 3000 + b"-first-bytes", b"S" * 2000 + b"-second-bytes"
DESCRIPTIONS = {400: "Bad Request: message thread not found", 401: "Unauthorized", 403: "Forbidden: bot was kicked",
                404: "Not Found", 429: "Too Many Requests: retry after 1", 500: "Internal Server Error",
                502: "Bad Gateway"}


class Crash(BaseException):
    """The process ends here: nothing in the runtime catches it."""


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, size: int = -1) -> bytes:
        return self._data if size < 0 else self._data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _parts(request):
    """(fields, files) of one request: JSON fields, or multipart fields and {field: (filename, bytes)}."""
    kind = request.get_header("Content-type") or ""
    if not kind.startswith("multipart/form-data"):
        return json.loads(request.data), {}
    # A file request streams its body as http.client writes it; read here whole, as Telegram receives it.
    data = request.data if isinstance(request.data, bytes) else b"".join(request.data)
    assert int(request.get_header("Content-length")) == len(data)
    boundary = kind.split("boundary=", 1)[1].encode()
    fields, files = {}, {}
    for part in data.split(b"--" + boundary):
        head, found, body = part.partition(b"\r\n\r\n")
        if not found:
            continue
        body = body[:-2] if body.endswith(b"\r\n") else body
        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
        filename = re.search(rb'filename="([^"]*)"', head)
        if filename:
            files[name] = (filename.group(1).decode(), body)
        else:
            fields[name] = body.decode()
    return fields, files


class Wire:
    """Telegram's HTTP side at the opener: records what reached it and answers as scripted.

    Outcomes per method, used up in order (default ``ok``): ``no_dns`` and ``refused`` fail before any connection
    (Telegram received nothing); ``write_broke`` and ``write_timeout`` fail while urllib writes the request, and
    ``lost`` or ``reset`` while it waits for the answer, all after the request reached Telegram; an int is Telegram's
    HTTP error with its JSON description; ``block`` waits for ``gate`` after the request arrived; an exception
    instance is raised after it arrived; a dict is Telegram's ok answer with that dict's ``result``. ``attempted``
    names every method asked for, including those that never connected.
    """

    def __init__(self):
        self.requests = []
        self.attempted = []
        self.answered = []  # (method, result) of every ok answer given
        self.script = {}
        self.gate, self.entered = threading.Event(), threading.Event()
        self._ids = iter(range(700, 10_000))

    def answer(self, method, *outcomes):
        self.script.setdefault(method, []).extend(outcomes)

    def sent(self, method):
        return [(fields, files) for name, fields, files in self.requests if name == method]

    def __call__(self, request, timeout=None):
        method = request.full_url.rsplit("/", 1)[-1]
        self.attempted.append(method)
        queued = self.script.get(method)
        outcome = queued.pop(0) if queued else "ok"
        if outcome == "no_dns":
            raise urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided, or not known"))
        if outcome == "refused":
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        fields, files = _parts(request)
        self.requests.append((method, fields, files))
        if isinstance(outcome, tuple) and outcome[0] == "block":  # wait for the gate, then answer as outcome[1]
            self.entered.set()
            self.gate.wait(10)
            outcome = outcome[1]
        if outcome == "write_broke":
            raise urllib.error.URLError(BrokenPipeError(32, "Broken pipe"))
        if outcome == "write_timeout":
            raise urllib.error.URLError(TimeoutError("The write operation timed out"))
        if outcome == "lost":
            raise TimeoutError("The read operation timed out")
        if outcome == "reset":
            raise ConnectionResetError(54, "Connection reset by peer")
        if isinstance(outcome, int):
            body = json.dumps({"ok": False, "error_code": outcome, "description": DESCRIPTIONS[outcome]}).encode()
            raise urllib.error.HTTPError(request.full_url, outcome, DESCRIPTIONS[outcome], {}, io.BytesIO(body))
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, dict):
            self.answered.append((method, outcome["result"]))
            return _Body(json.dumps({"ok": True, "result": outcome["result"]}).encode())
        if outcome == "block":
            self.entered.set()
            self.gate.wait(10)
        result = self._result(method, fields)
        self.answered.append((method, result))
        return _Body(json.dumps({"ok": True, "result": result}).encode())

    def _result(self, method, fields):
        if method == "getMe":
            return {"id": 9, "is_bot": True, "username": "presence_bot", "first_name": "Presence"}
        if method in {"deleteWebhook", "deleteMessage", "setMessageReaction", "sendChatAction"}:
            return True
        chat = {"id": int(fields["chat_id"]), "type": "supergroup", "title": "Lab"}
        if method == "sendMediaGroup":
            group = f"g{next(self._ids)}"
            return [{"message_id": next(self._ids), "date": 1_800_000_000, "chat": chat, "media_group_id": group,
                     entry["type"]: {"file_id": "sent"}} for entry in json.loads(fields["media"])]
        message = {"message_id": next(self._ids), "date": 1_800_000_000, "chat": chat}
        if method in {"sendMessage", "editMessageText"}:
            message["text"] = fields.get("text", "")
        else:
            message[method.removeprefix("send").lower()] = {"file_id": "sent"}
        return message


class Host:
    """A Host that takes delivery reports (presence_delivery_version 1) and whose places are Active."""
    delivery_reporting_status = "supported"
    presence_explicit_completion = 0

    def __init__(self):
        self.entered, self.release = None, None

    async def discover_delivery_support(self):
        return 1

    async def place_hold(self, account_id, conversation_id, thread_id=""):
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        return ""


class Notes:
    """A logger that keeps what the runtime logs, as (level, message)."""

    def __init__(self):
        self.lines = []

    def log(self, level, message):
        self.lines.append((level, message))


@pytest.fixture
def wire(monkeypatch):
    value = Wire()
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda _opener, request, data=None, timeout=None: value(request, timeout))
    yield value
    value.gate.set()


def _plugin(tmp_path):
    spec = importlib.util.spec_from_file_location(f"telegram_bot_unknown_{time.monotonic_ns()}", SKILL / "plugin.py",
                                                  submodule_search_locations=[str(SKILL)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    api = FakeApi(tmp_path)
    module.register(api)
    return module, {name: handler for name, handler, _ in api.tools}


def _tools(tmp_path):
    return _plugin(tmp_path)[1]


def _runtime(tmp_path, host=None, logger=None):
    return TelegramTransportRuntime(
        state_dir=tmp_path, token_provider=lambda: "token", logger=logger or Logger(), submitter=host or Host(),
        client_factory=lambda token: TelegramClient(token, api_server="http://telegram.invalid"))


def _restart(tmp_path, host=None):
    later = _runtime(tmp_path, host)
    later.store.release_own_leases()  # as run() does first
    return later


def _due(store):
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET available_at=0 WHERE state='pending'")


def _drain(runtime):
    for _ in range(40):
        _due(runtime.store)
        if not asyncio.run(runtime.process_one_outbox()):
            return
    raise AssertionError("the outbox never became quiet")


def _source(tmp_path, name, data=FIRST):
    path = tmp_path / "work" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path


def _copies(tmp_path):
    root = tmp_path / "outbox_files"
    return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []


METHOD = {"message": "sendMessage", "photo": "sendPhoto", "document": "sendDocument", "album": "sendMediaGroup"}


def _queue(tools, tmp_path, kind, request_id, **extra):
    if kind == "message":
        call = {"text": "hello there", "markdown": False}
    elif kind == "album":
        call = {"items": [{"kind": "photo", "file_path": str(_source(tmp_path, f"{request_id}-a.jpg"))},
                          {"kind": "photo", "file_path": str(_source(tmp_path, f"{request_id}-b.jpg"))}]}
    else:
        call = {"file_path": str(_source(tmp_path, f"{request_id}.{'jpg' if kind == 'photo' else 'pdf'}"))}
    answer = tools["telegram_send"](chat_id=CHAT, kind=kind, request_id=request_id, **{**call, **extra})
    assert answer["ok"], answer
    return f"telegram-send:{request_id}"


def _reports(store, delivery_id):
    return [(entry["payload"]["part_id"], entry["payload"]["state"])
            for entry in store.delivery_receipt(delivery_id)["history_reports"]]


# -- Unknown outcomes ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", list(METHOD))
@pytest.mark.parametrize("outcome", ["lost", "reset", "write_broke", "write_timeout", 500, 502])
def test_a_send_whose_outcome_is_unknown_ends_uncertain_and_is_never_made_again(tmp_path, wire, kind, outcome):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, kind, "once")
    follower = _queue(tools, tmp_path, "message", "next")
    wire.answer(METHOD[kind], outcome)
    runtime = _runtime(tmp_path)
    _drain(runtime)
    for _ in range(3):  # restarts; Telegram would now accept anything
        _drain(_restart(tmp_path))
    assert len(wire.sent(METHOD[kind])) == 1 + (kind == "message")  # the row's one call, and the follower's
    receipt = runtime.store.delivery_receipt(delivery)
    assert receipt["state"] == "uncertain" and receipt["provider_receipt"] == {}
    assert runtime.store.delivery_receipt(follower)["state"] == "delivered"  # the chat's next row was not held
    assert _reports(runtime.store, delivery) == [("status", "uncertain")]
    assert runtime.store.status_snapshot()["outbox_uncertain"] == 1


@pytest.mark.parametrize("kind", list(METHOD))
@pytest.mark.parametrize("outcome", ["no_dns", "refused"])
def test_a_request_that_never_left_is_retried_and_sent_once(tmp_path, wire, kind, outcome):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, kind, "once")
    wire.answer(METHOD[kind], outcome)
    runtime = _runtime(tmp_path)
    _drain(runtime)
    assert len(wire.sent(METHOD[kind])) == 1
    assert runtime.store.delivery_receipt(delivery)["state"] == "delivered"


@pytest.mark.parametrize("kind", list(METHOD))
@pytest.mark.parametrize("code", [400, 401, 403, 404, 429])
def test_telegrams_answer_that_it_did_nothing_keeps_the_bounded_retry(tmp_path, wire, kind, code):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, kind, "once")
    wire.answer(METHOD[kind], code)
    runtime = _runtime(tmp_path)
    _drain(runtime)
    assert len(wire.sent(METHOD[kind])) == 2  # refused once, then accepted
    receipt = runtime.store.delivery_receipt(delivery)
    assert receipt["state"] == "delivered" and receipt["attempts"] == 2


def test_after_a_refused_html_text_the_lost_plain_fallback_is_not_sent_again(tmp_path, wire):
    tools = _tools(tmp_path)
    assert tools["telegram_send"](chat_id=CHAT, text="**bold** answer", request_id="html")["ok"]
    wire.answer("sendMessage", 400, "lost")  # the HTML refused, then the plain resend's answer lost
    runtime = _runtime(tmp_path)
    _drain(runtime)
    _drain(_restart(tmp_path))
    first, second = (fields for fields, _files in wire.sent("sendMessage"))
    assert first["parse_mode"] == "HTML" and "parse_mode" not in second and len(wire.sent("sendMessage")) == 2
    assert runtime.store.delivery_receipt("telegram-send:html")["state"] == "uncertain"


def test_a_long_text_ends_at_its_unknown_chunk_and_keeps_the_confirmed_one(tmp_path, wire):
    tools = _tools(tmp_path)
    # Three paragraphs, each under Telegram's 4096 and no two together: one chunk each.
    text = "first part " * 300 + "\n\n" + "second part " * 300 + "\n\n" + "third part " * 300
    assert tools["telegram_send"](chat_id=CHAT, text=text, markdown=False, request_id="long")["ok"]
    wire.answer("sendMessage", "ok", "lost")
    runtime = _runtime(tmp_path)
    _drain(runtime)
    for _ in range(2):
        _drain(_restart(tmp_path))
    chunks = [fields["text"] for fields, _files in wire.sent("sendMessage")]
    assert len(chunks) == 2 and chunks[0].startswith("first part") and chunks[1].startswith("second part")
    receipt = runtime.store.delivery_receipt("telegram-send:long")
    assert receipt["state"] == "uncertain" and len(receipt["confirmed_messages"]) == 1
    assert _reports(runtime.store, "telegram-send:long") == [("0", "delivered"), ("status", "uncertain")]
    status = runtime.store.delivery_receipt("telegram-send:long")["history_reports"][-1]["payload"]
    assert status["message"]["confirmed_parts"] == 1


@pytest.mark.parametrize("kind", ["message", "photo", "album"])
@pytest.mark.parametrize("how", ["crash", "cancel", "lease_expired"])
def test_a_call_interrupted_after_it_reached_telegram_is_never_made_again(tmp_path, wire, kind, how):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, kind, "once")
    runtime = _runtime(tmp_path)
    if how == "crash":
        wire.answer(METHOD[kind], Crash())
        with pytest.raises(Crash):
            asyncio.run(runtime.process_one_outbox())
        later = _restart(tmp_path)
    else:
        wire.answer(METHOD[kind], "block")

        async def interrupted():
            task = asyncio.create_task(runtime.process_one_outbox())
            assert await asyncio.to_thread(wire.entered.wait, 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            wire.gate.set()  # Telegram's yes arrives after the worker stopped waiting

        asyncio.run(interrupted())
        later = _runtime(tmp_path)
        if how == "lease_expired":  # the row is leased again as a stale lease, not released
            with sqlite3.connect(later.store.path) as db:
                db.execute("UPDATE outbox SET state='leased', lease_until=1 WHERE delivery_id=?", (delivery,))
    _drain(later)
    _drain(_restart(tmp_path))
    assert len(wire.sent(METHOD[kind])) == 1
    assert later.store.delivery_receipt(delivery)["state"] == "uncertain"


def test_a_cancel_before_the_call_still_sends_once(tmp_path, wire):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, "message", "once")
    host = Host()
    runtime = _runtime(tmp_path, host)

    async def cancelled_while_reading_the_place():
        host.entered, host.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(runtime.process_one_outbox())
        await asyncio.wait_for(host.entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled_while_reading_the_place())
    host.entered = None
    _drain(_restart(tmp_path, host))
    assert len(wire.sent("sendMessage")) == 1
    assert runtime.store.delivery_receipt(delivery)["state"] == "delivered"


@pytest.mark.parametrize("kind", ["message", "photo", "album"])
@pytest.mark.parametrize("failure", ["store_error", "process_died"])
def test_telegrams_yes_whose_record_was_lost_is_never_followed_by_a_second_send(tmp_path, wire, kind, failure):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, kind, "once")
    runtime = _runtime(tmp_path)
    store = runtime.store
    original, failed = store.checkpoint_outbox, []

    def lose_the_yes(delivery_id, payload, **kwargs):
        if not failed and ({"_sent_messages", "_sent_media"} & set(payload)):
            failed.append(delivery_id)
            raise Crash() if failure == "process_died" else sqlite3.OperationalError("database is locked")
        return original(delivery_id, payload, **kwargs)

    store.checkpoint_outbox = lose_the_yes
    try:
        if failure == "process_died":
            with pytest.raises(Crash):
                asyncio.run(runtime.process_one_outbox())
        else:
            asyncio.run(runtime.process_one_outbox())
    finally:
        del store.checkpoint_outbox
    _drain(_restart(tmp_path))
    _drain(_restart(tmp_path))
    assert failed and len(wire.sent(METHOD[kind])) == 1
    assert store.delivery_receipt(delivery)["state"] == "uncertain"


# -- The bytes of a queued file ------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind,field", [("photo", "photo"), ("document", "document")])
@pytest.mark.parametrize("change", ["rewritten", "removed"])
def test_a_queued_file_is_sent_as_the_bytes_and_name_it_had_when_queued(tmp_path, wire, kind, field, change):
    tools = _tools(tmp_path)
    source = _source(tmp_path, "Report 2026.pdf" if kind == "document" else "view.jpg")
    assert tools["telegram_send"](chat_id=CHAT, kind=kind, file_path=str(source), request_id="file")["ok"]
    if change == "rewritten":
        source.write_bytes(SECOND)
    else:
        source.unlink()
    _drain(_runtime(tmp_path))
    [(_fields, files)] = wire.sent(METHOD[kind])
    assert files[field] == (source.name, FIRST)


def test_an_album_is_sent_as_the_bytes_and_names_its_items_had_when_queued(tmp_path, wire):
    tools = _tools(tmp_path)
    first, second = _source(tmp_path, "a.jpg", FIRST), _source(tmp_path, "b.mp4", SECOND)
    items = [{"kind": "photo", "file_path": str(first)}, {"kind": "video", "file_path": str(second)}]
    assert tools["telegram_send"](chat_id=CHAT, kind="album", items=items, request_id="album")["ok"]
    first.write_bytes(b"changed")
    second.unlink()
    _drain(_runtime(tmp_path))
    [(_fields, files)] = wire.sent("sendMediaGroup")
    assert files == {"file0": ("a.jpg", FIRST), "file1": ("b.mp4", SECOND)}


def test_a_repeated_request_id_keeps_the_first_bytes_and_one_copy(tmp_path, wire):
    tools = _tools(tmp_path)
    source = _source(tmp_path, "note.pdf", FIRST)
    assert tools["telegram_send"](chat_id=CHAT, kind="document", file_path=str(source), request_id="same")["state"] == "queued"
    source.write_bytes(SECOND)
    again = tools["telegram_send"](chat_id=CHAT, kind="document", file_path=str(source), request_id="same")
    assert again["state"] == "already_queued"
    assert [path.read_bytes() for path in _copies(tmp_path)] == [FIRST]
    _drain(_runtime(tmp_path))
    [(_fields, files)] = wire.sent("sendDocument")
    assert files["document"] == ("note.pdf", FIRST)


@pytest.mark.parametrize("end", ["delivered", "uncertain"])
def test_the_copy_stays_with_the_row_and_its_receipt_names_the_bytes_and_their_source(tmp_path, wire, end):
    tools = _tools(tmp_path)
    source = _source(tmp_path, "chart.png", FIRST)
    assert tools["telegram_send"](chat_id=CHAT, kind="photo", file_path=str(source), request_id="chart")["ok"]
    source.write_bytes(SECOND)
    if end == "uncertain":
        wire.answer("sendPhoto", "lost")
    runtime = _runtime(tmp_path)
    _drain(runtime)
    receipt = runtime.store.delivery_receipt("telegram-send:chart")
    assert receipt["state"] == end
    [kept] = receipt["files"]
    assert kept == {"file_name": "chart.png", "source_path": str(source.resolve()), "copy_path": kept["copy_path"],
                    "sha256": hashlib.sha256(FIRST).hexdigest(), "size_bytes": len(FIRST)}
    # Whatever the end, the exact bytes stay with the row's history; the caller's file is never touched.
    assert pathlib.Path(kept["copy_path"]).read_bytes() == FIRST and _copies(tmp_path) == [pathlib.Path(kept["copy_path"])]
    assert source.read_bytes() == SECOND
    if os.name == "posix":  # readable by this account only, whatever the original allowed
        assert stat.S_IMODE(os.stat(kept["copy_path"]).st_mode) == 0o600


def test_a_held_file_row_reads_its_copy_only_when_its_place_lets_it_send(tmp_path, wire, monkeypatch):
    from telegram_bot import outbox_files
    checks = []
    real = outbox_files.verify
    monkeypatch.setattr(outbox_files, "verify", lambda payload: (checks.append(1), real(payload))[1])

    class Paused(Host):
        hold = "Held: Telegram is paused"

        async def place_hold(self, account_id, conversation_id, thread_id=""):
            return self.hold

    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, "photo", "held")
    host = Paused()
    runtime = _runtime(tmp_path, host)
    for _ in range(3):
        _due(runtime.store)
        assert asyncio.run(runtime.process_one_outbox())
    receipt = runtime.store.delivery_receipt(delivery)
    assert receipt["state"] == "held" and checks == [] and wire.sent("sendPhoto") == []
    assert "_call_started" not in runtime.store.outbox_payload(delivery)
    host.hold = ""
    _drain(runtime)
    assert checks == [1] and len(wire.sent("sendPhoto")) == 1
    assert runtime.store.delivery_receipt(delivery)["state"] == "delivered"


def test_a_copy_that_changed_after_queueing_is_never_sent(tmp_path, wire):
    tools = _tools(tmp_path)
    source = _source(tmp_path, "plan.pdf", FIRST)
    assert tools["telegram_send"](chat_id=CHAT, kind="document", file_path=str(source), request_id="plan")["ok"]
    runtime = _runtime(tmp_path)
    copy = pathlib.Path(runtime.store.delivery_receipt("telegram-send:plan")["files"][0]["copy_path"])
    copy.write_bytes(SECOND)
    _drain(runtime)
    receipt = runtime.store.delivery_receipt("telegram-send:plan")
    assert wire.sent("sendDocument") == [] and receipt["state"] == "failed" and "has changed" in receipt["error"]
    assert copy.read_bytes() == SECOND  # kept as found; nothing removes it


@pytest.mark.parametrize("arguments,code", [
    ({"kind": "photo", "caption": "x" * 1100}, "caption_too_long"),
    ({"kind": "voice", "name": "memo.wav"}, "voice_format"),
    ({"kind": "document", "missing": True}, "file_missing"),
    ({"kind": "document", "unreadable": True}, "file_copy_failed"),
    ({"kind": "album", "items": [("audio", "a.mp3"), ("photo", "b.jpg")]}, "album_mixed"),
])
def test_a_refused_send_queues_nothing_and_keeps_no_copy(tmp_path, wire, arguments, code):
    if arguments.get("unreadable") and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads a file whatever its mode")
    tools = _tools(tmp_path)
    source = _source(tmp_path, arguments.get("name", "file.jpg"))
    call = {"chat_id": CHAT, "kind": arguments["kind"], "request_id": "refused", "caption": arguments.get("caption", "")}
    if "items" in arguments:
        call["items"] = [{"kind": kind, "file_path": str(_source(tmp_path, name))} for kind, name in arguments["items"]]
    else:
        call["file_path"] = str(tmp_path / "gone.pdf") if arguments.get("missing") else str(source)
    if arguments.get("unreadable"):
        os.chmod(source, 0)
    try:
        answer = tools["telegram_send"](**call)
    finally:
        os.chmod(source, 0o600)
    assert answer["ok"] is False and answer["code"] == code, answer
    assert _copies(tmp_path) == [] and _runtime(tmp_path).store.delivery_receipt("telegram-send:refused") == {
        "state": "not_found"}


# -- Rows an earlier version queued or attempted ---------------------------------------------------------------------

OLD_LOST = "TelegramApiError: Telegram request failed (transport): TimeoutError"
OLD_URLERROR = "TelegramApiError: Telegram request failed (transport): URLError"


def _old_row(store, delivery_id, kind):
    """A row as 0.12.0's telegram_send queued it: no call marker, no stamp, a file named only by its path."""
    payload = {"kind": kind, "chat_id": CHAT, "markdown": False, "_reporting": {"version": -1, "origin": {"kind": "tool"}}}
    if kind == "message":
        payload.update(text="old words", _rendered_chunks=[{"text": "old words", "parse_mode": ""}])
    elif kind == "photo":
        payload.update(file_path=str(_source(store.path.parent, "old.jpg", SECOND)), caption="",
                       _rendered_caption={"text": "", "parse_mode": ""})
    else:
        paths = [str(_source(store.path.parent, name, SECOND)) for name in ("old-a.jpg", "old-b.jpg")]
        payload.update(items=[{"kind": "photo", "file_path": path, "caption": ""} for path in paths],
                       _rendered_items=[{"kind": "photo", "file_path": path, "caption": {"text": "", "parse_mode": ""}}
                                        for path in paths])
    assert store.enqueue_outbox(delivery_id, payload)
    return payload


@pytest.mark.parametrize("kind", ["message", "photo", "album"])
@pytest.mark.parametrize("attempts,state,last_error,expected", [
    (0, "pending", "", "delivered"),
    (0, "pending", "Held: Telegram is paused", "delivered"),  # a hold counts no attempt
    (1, "pending", "released_at_runtime_start", "uncertain"),
    (1, "pending", "worker_cancelled", "uncertain"),
    (1, "leased", "", "uncertain"),  # an expired lease of 0.12.0
    (1, "pending", OLD_LOST, "uncertain"),
    (1, "pending", OLD_URLERROR, "uncertain"),  # 0.12.0 did not say whether the request was being written
    (1, "pending", "TelegramApiError: Telegram sendMessage failed (502): Bad Gateway", "uncertain"),
    (1, "pending", "Held: Telegram is paused", "uncertain"),  # the hold replaced the counted attempt's error
    (2, "pending", "TelegramApiError: Telegram sendMessage failed (429): Too Many Requests: retry after 1", "uncertain"),
    (1, "pending", "TelegramApiError: Telegram {method} failed (429): Too Many Requests: retry after 1", "text_only"),
    (1, "pending", "TelegramApiError: Telegram {method} failed (400): Bad Request: chat not found", "text_only"),
    (1, "pending", "RuntimeError: telegram token is unavailable", "delivered"),
    (1, "pending", "TelegramApiError: Telegram getMe failed (401): Unauthorized", "delivered"),
])
def test_a_send_an_earlier_version_attempted_is_judged_by_what_its_record_proves(tmp_path, wire, kind, attempts, state,
                                                                                last_error, expected):
    runtime = _runtime(tmp_path)
    _old_row(runtime.store, "telegram-send:old", kind)
    _old_row(runtime.store, "telegram-send:next", "message")  # the chat's next row
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("UPDATE outbox SET attempts=?, state=?, last_error=?, lease_until=?, available_at=0 "
                   "WHERE delivery_id='telegram-send:old'",
                   (attempts, state, last_error.format(method=METHOD[kind]), 1 if state == "leased" else 0))
        if attempts:  # an old file attempt could call only after persisting resolved reporting
            db.execute("UPDATE outbox SET payload_json=json_set(payload_json,'$._reporting.version',0) "
                       "WHERE delivery_id='telegram-send:old'")
    _drain(runtime)
    if expected == "text_only":  # the old error string did not identify the response to the original upload
        expected = "delivered" if kind == "message" else "uncertain"
    receipt = runtime.store.delivery_receipt("telegram-send:old")
    assert receipt["state"] == expected
    assert len(wire.sent(METHOD[kind])) == (expected == "delivered") + (kind == "message")
    assert runtime.store.delivery_receipt("telegram-send:next")["state"] == "delivered"
    if expected == "uncertain":
        assert "send_result_unrecorded" in receipt["error"]


def test_an_earlier_versions_partial_text_keeps_its_confirmed_chunk_and_sends_nothing_more(tmp_path, wire):
    runtime = _runtime(tmp_path)
    payload = _old_row(runtime.store, "telegram-send:old", "message")
    sent = [{"message_id": 61, "date": 1_800_000_000, "text": "first chunk"}]
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("UPDATE outbox SET attempts=1, last_error=?, payload_json=? WHERE delivery_id='telegram-send:old'",
                   (OLD_LOST, json.dumps({**payload, "_sent_messages": sent, "_rendered_chunks": [
                       {"text": "first chunk", "parse_mode": ""}, {"text": "second chunk", "parse_mode": ""}]})))
    _drain(runtime)
    receipt = runtime.store.delivery_receipt("telegram-send:old")
    assert wire.sent("sendMessage") == [] and receipt["state"] == "uncertain"
    assert receipt["confirmed_messages"] == sent


@pytest.mark.parametrize("kind", ["message", "photo", "album"])
def test_an_earlier_versions_send_confirmed_before_the_stop_finishes_delivered_without_a_call(tmp_path, wire, kind):
    runtime = _runtime(tmp_path)
    payload = _old_row(runtime.store, "telegram-send:old", kind)
    confirmed = {"message_id": 62, "date": 1_800_000_000}
    done = ({"_sent_messages": [confirmed]} if kind == "message" else
            {"_sent_media": confirmed} if kind == "photo" else {"_sent_media": [confirmed, {**confirmed, "message_id": 63}]})
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("UPDATE outbox SET attempts=1, state='leased', lease_until=1, payload_json=? "
                   "WHERE delivery_id='telegram-send:old'", (json.dumps({**payload, **done}),))
    _drain(_restart(tmp_path))
    assert wire.sent(METHOD[kind]) == []
    assert runtime.store.delivery_receipt("telegram-send:old")["state"] == "delivered"


def test_an_earlier_versions_queued_file_is_sent_from_its_path_as_before_and_claims_no_proven_bytes(tmp_path, wire):
    """The boundary: 0.12.0 recorded only a path, so its file goes out as the path holds it then, with no digest."""
    runtime = _runtime(tmp_path)
    payload = _old_row(runtime.store, "telegram-send:old", "photo")
    pathlib.Path(payload["file_path"]).write_bytes(b"what the path holds at send time")
    _drain(runtime)
    [(_fields, files)] = wire.sent("sendPhoto")
    assert files["photo"] == ("old.jpg", b"what the path holds at send time")
    receipt = runtime.store.delivery_receipt("telegram-send:old")
    assert receipt["state"] == "delivered" and "files" not in receipt and _copies(tmp_path) == []
    assert "_frozen" not in runtime.store.outbox_payload("telegram-send:old")
    assert pathlib.Path(payload["file_path"]).exists()  # never removed


def test_an_earlier_versions_file_that_vanished_is_never_sent_and_ends_failed(tmp_path, wire):
    """0.10.6 and 0.12.0 read the file only to build the upload, so their raw FileNotFoundError proves no request."""
    runtime = _runtime(tmp_path)
    payload = _old_row(runtime.store, "telegram-send:old", "photo")
    pathlib.Path(payload["file_path"]).unlink()
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("UPDATE outbox SET attempts=1,last_error=?,payload_json=json_set(payload_json, "
                   "'$._reporting.version',0) WHERE delivery_id='telegram-send:old'",
                   (f"FileNotFoundError: [Errno 2] No such file or directory: '{payload['file_path']}'",))
    _drain(runtime)
    receipt = runtime.store.delivery_receipt("telegram-send:old")
    assert wire.sent("sendPhoto") == [] and receipt["state"] == "failed"
    assert "_call_started" not in runtime.store.outbox_payload("telegram-send:old")


@pytest.mark.parametrize("old_error,expected", [(OLD_LOST, "uncertain"),
                                                ("TelegramApiError: Telegram sendMessage failed (429): x", "delivered")])
def test_an_attempt_a_rolled_back_version_made_is_judged_like_an_upgrade(tmp_path, wire, old_error, expected):
    """This version claimed the row first (failing before any call), 0.12.0 then made an attempt after a rollback,
    and this version is back: that attempt proves no effect only by Telegram's own refusal."""
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, "message", "rolled")
    tokenless = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "", logger=Logger(), submitter=Host())
    assert asyncio.run(tokenless.process_one_outbox())  # no token: released for a retry, no call
    now = time.time()
    with sqlite3.connect(tokenless.store.path) as db:  # 0.12.0's claim and release, in its own statements
        assert db.execute("UPDATE outbox SET state='leased', lease_until=?, attempts=attempts+1, last_error='' "
                          "WHERE delivery_id=?", (now + 120, delivery)).rowcount == 1
        db.execute("UPDATE outbox SET state='pending', available_at=0, lease_until=0, last_error=? WHERE delivery_id=?",
                   (old_error, delivery))
    runtime = _runtime(tmp_path)
    _drain(runtime)
    assert runtime.store.delivery_receipt(delivery)["state"] == expected
    assert len(wire.sent("sendMessage")) == (expected == "delivered")


# -- What stays as it was ------------------------------------------------------------------------------------------

def test_edits_and_moderation_keep_their_earlier_handling(tmp_path, wire):
    tools = _tools(tmp_path)
    assert tools["telegram_moderate"](action="delete_message", chat_id=CHAT, message_id=7, request_id="del")["ok"]
    assert tools["telegram_operation"](method="editMessageText", chat_id=CHAT, message_id=8, text="fixed",
                                       request_id="edit-502")["ok"]
    assert tools["telegram_operation"](method="editMessageText", chat_id=CHAT, message_id=9, text="fixed",
                                       request_id="edit-lost")["ok"]
    wire.answer("deleteMessage", "lost")  # moderation retries after a lost answer, as before
    wire.answer("editMessageText", 502, "ok", "lost")  # an edit retries a 5xx and ends uncertain on a lost answer
    runtime = _runtime(tmp_path)
    _drain(runtime)
    assert len(wire.sent("deleteMessage")) == 2
    assert runtime.store.delivery_receipt("telegram-moderate:del")["state"] == "delivered"
    assert len(wire.sent("editMessageText")) == 3
    assert runtime.store.delivery_receipt("telegram-operation:edit-502")["state"] == "delivered"
    assert runtime.store.delivery_receipt("telegram-operation:edit-lost")["state"] == "uncertain"
    stored = [runtime.store.outbox_payload(key) for key in ("telegram-moderate:del", "telegram-operation:edit-502")]
    assert all("_call_started" not in payload and "_attempts_marked" not in payload for payload in stored)


# -- Classification and records (0.12.1 names) -----------------------------------------------------------------------

@pytest.mark.parametrize("reason,proved", [
    (socket.gaierror(8, "nodename nor servname provided"), True),
    (ConnectionRefusedError(61, "Connection refused"), True),
    (BrokenPipeError(32, "Broken pipe"), False),
    (ConnectionResetError(54, "Connection reset by peer"), False),
    (TimeoutError("timed out"), False),
    (OSError(errno.ENETUNREACH, "Network is unreachable"), False),
    (ssl.SSLError("bad record mac"), False),
    ("unknown url type: xyz", False),
])
def test_urllib_failures_prove_nothing_was_sent_only_before_a_connection(monkeypatch, reason, proved):
    from telegram_bot import api

    def refuse(request, timeout=None):
        raise urllib.error.URLError(reason)

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(api.TelegramApiError) as raised:
        api._open_json(urllib.request.Request("http://telegram.invalid/bottoken/sendMessage", data=b"{}"), 1.0)
    assert isinstance(raised.value, api.TelegramRequestNotSent) is proved
    assert raised.value.error_code == 0


def test_an_answer_lost_after_the_request_was_written_is_not_proof(monkeypatch):
    from telegram_bot import api
    for failure in (TimeoutError("read timed out"), ConnectionResetError(54, "reset")):
        monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: (_ for _ in ()).throw(failure))
        with pytest.raises(api.TelegramApiError) as raised:
            api._open_json(urllib.request.Request("http://telegram.invalid/bottoken/sendMessage", data=b"{}"), 1.0)
        assert not isinstance(raised.value, api.TelegramRequestNotSent)


def test_an_upload_that_could_not_be_built_proves_nothing_was_sent(tmp_path, monkeypatch):
    from telegram_bot import api
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda *_args, **_kwargs: pytest.fail("nothing may be sent"))
    with pytest.raises(api.TelegramRequestNotSent):
        api.UrllibTelegramTransport._post_multipart_files(
            "http://telegram.invalid/bottoken/sendPhoto", {"chat_id": CHAT}, [("photo", tmp_path / "gone.jpg")], 1.0)
    big = tmp_path / "big.bin"
    with open(big, "wb") as handle:
        handle.truncate(api.CLOUD_UPLOAD_LIMIT + 1)  # sparse
    with pytest.raises(api.TelegramRequestNotSent):
        api.UrllibTelegramTransport._post_multipart_files(
            "http://telegram.invalid/bottoken/sendDocument", {"chat_id": CHAT}, [("document", big)], 1.0)


def test_a_call_is_stored_as_started_only_while_this_attempt_holds_the_row(tmp_path):
    from telegram_bot.custody import CALL_STARTED, CustodyStore
    store = CustodyStore(tmp_path / "custody.sqlite3")
    assert store.enqueue_outbox("telegram-send:x", {"kind": "message", "chat_id": CHAT, "text": "x"})
    stale = store.claim_outbox(lease_sec=120)
    store.release_own_leases()
    current = store.claim_outbox(lease_sec=120)
    assert current.attempts == stale.attempts + 1
    marked = {**stale.payload, CALL_STARTED: {"part": 0, "method": "sendMessage"}}
    assert not store.start_outbox_call(stale.delivery_id, stale.attempts, marked)  # the stale attempt makes no call
    assert CALL_STARTED not in store.outbox_payload("telegram-send:x")
    assert store.start_outbox_call(current.delivery_id, current.attempts, {**current.payload, CALL_STARTED: {"part": 0}})
    assert store.outbox_payload("telegram-send:x")[CALL_STARTED] == {"part": 0}


def test_each_claim_stamps_the_attempt_this_version_accounts_for(tmp_path, wire):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, "message", "stamp")
    wire.answer("sendMessage", 429)
    runtime = _runtime(tmp_path)
    _drain(runtime)
    payload = runtime.store.outbox_payload(delivery)
    assert payload["_attempts_marked"] == runtime.store.delivery_receipt(delivery)["attempts"] == 2
    assert "_call_started" not in payload  # removed with Telegram's answers: the 429, then the accepted message


# -- Decided by the record before any token, getMe or Host read (review fixes) ---------------------------------------

class Unreachable(Host):
    """A Host that is down: every read fails and is noted, so a test can show that none was made."""

    def __init__(self):
        super().__init__()
        self.asked = []

    async def discover_delivery_support(self):
        self.asked.append("discover_delivery_support")
        raise ConnectionError("Host unreachable")

    async def place_hold(self, account_id, conversation_id, thread_id=""):
        self.asked.append("place_hold")
        raise ConnectionError("Host unreachable")


PREFLIGHT_FAILURES = ["no_token", "getme_refused", "no_network", "host_down"]


def _before_any_call_fails(tmp_path, wire, failure):
    """A runtime whose token, getMe, deleteWebhook or Host fails before a send call could be made."""
    if failure == "getme_refused":
        wire.answer("getMe", *[401] * 6)
    elif failure == "no_network":
        wire.answer("deleteWebhook", *["no_dns"] * 6)
    return TelegramTransportRuntime(
        state_dir=tmp_path, token_provider=lambda: "" if failure == "no_token" else "token", logger=Logger(),
        submitter=Unreachable(), client_factory=lambda token: TelegramClient(token, api_server="http://telegram.invalid"))


@pytest.mark.parametrize("failure", PREFLIGHT_FAILURES)
@pytest.mark.parametrize("kind", ["message", "photo", "album"])
def test_an_earlier_versions_unproven_attempt_ends_uncertain_on_its_first_claim_before_any_preflight(
        tmp_path, wire, kind, failure):
    runtime = _before_any_call_fails(tmp_path, wire, failure)
    _old_row(runtime.store, "telegram-send:old", kind)
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("UPDATE outbox SET attempts=1,last_error=?,payload_json=json_set(payload_json, "
                   "'$._reporting.version',0) WHERE delivery_id='telegram-send:old'", (OLD_LOST,))
    _due(runtime.store)
    assert asyncio.run(runtime.process_one_outbox())  # its first claim
    receipt = runtime.store.delivery_receipt("telegram-send:old")
    assert receipt["state"] == "uncertain" and "send_result_unrecorded" in receipt["error"]
    assert wire.attempted == [] and runtime.submitter.asked == []  # no deleteWebhook, getMe, Host or place read
    _due(runtime.store)
    assert not asyncio.run(runtime.process_one_outbox())  # terminal: nothing is left to claim


def test_an_earlier_versions_partial_text_without_a_token_ends_uncertain_with_its_confirmed_chunk(tmp_path, wire):
    runtime = _before_any_call_fails(tmp_path, wire, "no_token")
    payload = _old_row(runtime.store, "telegram-send:old", "message")
    sent = [{"message_id": 61, "date": 1_800_000_000, "text": "first chunk"}]
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("UPDATE outbox SET attempts=1, last_error=?, payload_json=? WHERE delivery_id='telegram-send:old'",
                   (OLD_LOST, json.dumps({**payload, "_sent_messages": sent, "_rendered_chunks": [
                       {"text": "first chunk", "parse_mode": ""}, {"text": "second chunk", "parse_mode": ""}]})))
    _due(runtime.store)
    assert asyncio.run(runtime.process_one_outbox())
    receipt = runtime.store.delivery_receipt("telegram-send:old")
    assert receipt["state"] == "uncertain" and receipt["confirmed_messages"] == sent
    assert wire.attempted == [] and runtime.submitter.asked == []


@pytest.mark.parametrize("who", ["earlier_version", "this_version"])
@pytest.mark.parametrize("kind", ["message", "photo", "album"])
def test_a_send_whose_every_part_is_stored_finishes_delivered_from_its_record_without_a_token(tmp_path, wire, kind,
                                                                                            who):
    runtime = _before_any_call_fails(tmp_path, wire, "no_token")
    payload = _old_row(runtime.store, "telegram-send:done", kind)
    first, second = {"message_id": 62, "date": 1_800_000_000}, {"message_id": 63, "date": 1_800_000_000}
    done, expected = {
        "message": ({"_sent_messages": [first]}, {"kind": "message", "messages": [first]}),
        "photo": ({"_sent_media": first}, {"kind": "photo", "message": first}),
        "album": ({"_sent_media": [first, second]}, {"kind": "album", "messages": [first, second]}),
    }[kind]
    # The account and origin its parts were reported with; this runtime has no bot to put in their place.
    reporting = {"version": 1, "account_id": "8", "origin": {"kind": "tool", "task_id": "task-1"}}
    stamp = {"_attempts_marked": 1} if who == "this_version" else {}
    with sqlite3.connect(runtime.store.path) as db:  # the process ended after the last part's record, before the row's
        db.execute("UPDATE outbox SET attempts=1, last_error='released_at_runtime_start', payload_json=? "
                   "WHERE delivery_id='telegram-send:done'",
                   (json.dumps({**payload, **done, **stamp, "_reporting": reporting}),))
    _due(runtime.store)
    assert asyncio.run(runtime.process_one_outbox())
    receipt = runtime.store.delivery_receipt("telegram-send:done")
    assert receipt["state"] == "delivered" and receipt["provider_receipt"] == expected
    assert wire.attempted == [] and runtime.submitter.asked == []
    assert runtime.store.outbox_payload("telegram-send:done")["_reporting"] == reporting


# -- An ok answer that names no message (review fixes) -----------------------------------------------------------------

NAMES_NO_MESSAGE = [{}, None, True, {"message_id": "701"}, {"message_id": 0}]
NAMES_NO_MESSAGE_IDS = ["empty", "null", "true", "text_id", "zero_id"]
ALBUM_MESSAGE = {"message_id": 801, "date": 1_800_000_000, "chat": {"id": int(CHAT), "type": "supergroup"},
                 "media_group_id": "g8", "photo": {"file_id": "sent"}}


@pytest.mark.parametrize("kind", list(METHOD))
def test_an_ok_answer_naming_its_message_is_delivered_as_before(tmp_path, wire, kind):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, kind, "once")
    runtime = _runtime(tmp_path)
    _drain(runtime)
    receipt = runtime.store.delivery_receipt(delivery)
    assert receipt["state"] == "delivered" and len(wire.sent(METHOD[kind])) == 1
    kept = receipt["provider_receipt"]
    messages = kept["messages"] if "messages" in kept else [kept["message"]]
    assert len(messages) == 1 + (kind == "album") and all(type(item["message_id"]) is int for item in messages)
    assert _reports(runtime.store, delivery) == [(str(index), "delivered") for index in range(len(messages))]


@pytest.mark.parametrize("result", NAMES_NO_MESSAGE, ids=NAMES_NO_MESSAGE_IDS)
@pytest.mark.parametrize("kind", ["message", "photo", "document"])
def test_an_ok_answer_that_names_no_message_ends_uncertain_and_is_never_sent_again(tmp_path, wire, kind, result):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, kind, "once")
    follower = _queue(tools, tmp_path, "message", "next")
    wire.answer(METHOD[kind], {"result": result})
    runtime = _runtime(tmp_path)
    _drain(runtime)
    for _ in range(2):
        _drain(_restart(tmp_path))
    assert len(wire.sent(METHOD[kind])) == 1 + (kind == "message")  # the row's one call, and the follower's
    receipt = runtime.store.delivery_receipt(delivery)
    assert receipt["state"] == "uncertain" and receipt["provider_receipt"] == {}
    assert "confirmed_messages" not in receipt and "message_id" in receipt["error"]
    assert _reports(runtime.store, delivery) == [("status", "uncertain")]
    assert runtime.store.delivery_receipt(follower)["state"] == "delivered"


@pytest.mark.parametrize("returned,named", [
    ([{}, {}], []),
    ([ALBUM_MESSAGE, {}], [0]),
    ([{}, {**ALBUM_MESSAGE, "message_id": 802}], [1]),
    ([ALBUM_MESSAGE, {**ALBUM_MESSAGE, "message_id": 802}, {}], [0, 1]),
    ([None, {**ALBUM_MESSAGE, "message_id": 802}], [1]),  # entries that are not objects keep their places too
    ([ALBUM_MESSAGE, {**ALBUM_MESSAGE, "message_id": 802}, None], [0, 1]),
], ids=["none_named", "first_named", "second_named", "extra_entry_without_id", "first_not_an_object",
        "extra_entry_not_an_object"])
def test_an_album_answer_without_a_message_per_item_keeps_those_it_names_and_ends_uncertain(tmp_path, wire, returned,
                                                                                           named):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, "album", "once")
    wire.answer("sendMediaGroup", {"result": returned})
    runtime = _runtime(tmp_path)
    _drain(runtime)
    _drain(_restart(tmp_path))
    assert len(wire.sent("sendMediaGroup")) == 1
    receipt = runtime.store.delivery_receipt(delivery)
    assert receipt["state"] == "uncertain" and receipt["provider_receipt"] == {}
    assert [item["message_id"] for item in receipt["confirmed_messages"]] == [returned[i]["message_id"] for i in named]
    assert _reports(runtime.store, delivery) == [(str(i), "delivered") for i in named] + [("status", "uncertain")]
    status = receipt["history_reports"][-1]["payload"]["message"]
    assert status["confirmed_parts"] == len(named) and "for 2 items" in status["error"]


@pytest.mark.parametrize("kind,result", [
    ("message", {}),
    ("photo", {}),
    ("album", [{}, {}]),
    ("album", [ALBUM_MESSAGE, {**ALBUM_MESSAGE, "message_id": 802}, {}]),  # its stored messages alone look complete
], ids=["message", "photo", "album_none_named", "album_extra_entry_without_id"])
def test_an_answer_naming_no_message_permits_no_second_call_when_the_process_ends_after_it(tmp_path, wire, kind,
                                                                                         result):
    """Telegram took the call and answered ok without a message for it, and the process ends at the row's next
    terminal write, whichever that is: the restart makes no second call and claims no delivery."""
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, kind, "once")
    wire.answer(METHOD[kind], {"result": result})
    runtime = _runtime(tmp_path)
    store = runtime.store

    def process_ends(*_args, **_kwargs):
        raise Crash()

    store.mark_delivered = store.mark_outbox_failed = process_ends
    try:
        with pytest.raises(Crash):
            asyncio.run(runtime.process_one_outbox())
    finally:
        del store.mark_delivered, store.mark_outbox_failed
    for _ in range(2):
        _drain(_restart(tmp_path))
    assert len(wire.sent(METHOD[kind])) == 1
    receipt = store.delivery_receipt(delivery)
    assert receipt["state"] == "uncertain" and receipt["provider_receipt"] == {}


@pytest.mark.parametrize("kind", ["message", "photo", "album"])
def test_a_plain_fallback_whose_ok_answer_names_no_message_is_not_sent_again(tmp_path, wire, kind):
    tools = _tools(tmp_path)
    extra = {"text": "**bold** answer", "markdown": True} if kind == "message" else {"caption": "**bold** caption"}
    delivery = _queue(tools, tmp_path, kind, "html", **extra)
    wire.answer(METHOD[kind], 400, {"result": [{}, {}] if kind == "album" else {}})  # HTML refused; plain answer empty
    runtime = _runtime(tmp_path)
    _drain(runtime)
    _drain(_restart(tmp_path))
    first, second = (fields for fields, _files in wire.sent(METHOD[kind]))
    if kind == "album":
        first, second = json.loads(first["media"])[0], json.loads(second["media"])[0]
    assert first["parse_mode"] == "HTML" and "parse_mode" not in second
    assert runtime.store.delivery_receipt(delivery)["state"] == "uncertain"


@pytest.mark.parametrize("kind,stored", [
    ("message", {"_sent_messages": [{}]}),
    ("photo", {"_sent_media": {}}),
    ("album", {"_sent_media": [{}, {}]}),
], ids=["message", "photo", "album"])
def test_an_earlier_versions_stored_answer_that_names_no_message_is_not_taken_as_delivery(tmp_path, wire, kind, stored):
    """0.12.0 kept whatever Telegram's ok carried: such a record is neither proof of a message nor reason to send."""
    runtime = _runtime(tmp_path)
    payload = _old_row(runtime.store, "telegram-send:old", kind)
    with sqlite3.connect(runtime.store.path) as db:
        db.execute("UPDATE outbox SET attempts=1, state='leased', lease_until=1, payload_json=? "
                   "WHERE delivery_id='telegram-send:old'", (json.dumps({**payload, **stored}),))
    _drain(_restart(tmp_path))
    assert wire.sent(METHOD[kind]) == []
    assert runtime.store.delivery_receipt("telegram-send:old")["state"] == "uncertain"


# -- An attempt that lost its lease (review fixes) -----------------------------------------------------------------

@pytest.mark.parametrize("attempts_before", [0, 4], ids=["first_attempt", "fifth_attempt"])
def test_an_attempt_overtaken_by_another_claim_leaves_that_claims_row_alone(tmp_path, wire, attempts_before):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, "message", "once")
    if attempts_before:  # earlier attempts of this version, which Telegram refused
        with sqlite3.connect(tmp_path / "custody.sqlite3") as db:
            db.execute("UPDATE outbox SET attempts=?, payload_json=json_set(payload_json, '$._attempts_marked', ?) "
                       "WHERE delivery_id=?", (attempts_before, attempts_before, delivery))
    host = Host()
    runtime = _runtime(tmp_path, host)
    _due(runtime.store)

    async def overtaken():
        host.entered, host.release = asyncio.Event(), asyncio.Event()
        stale = asyncio.create_task(runtime.process_one_outbox())
        await asyncio.wait_for(host.entered.wait(), 10)  # this attempt reads its place before its call
        runtime.store.release_own_leases()  # meanwhile a reload's runtime starts and claims the row
        taken = runtime.store.claim_outbox(lease_sec=120)
        host.release.set()
        assert await stale
        return taken

    taken = asyncio.run(overtaken())
    with sqlite3.connect(runtime.store.path) as db:
        state, attempts, lease_until, last_error, payload = db.execute(
            "SELECT state, attempts, lease_until, last_error, payload_json FROM outbox WHERE delivery_id=?",
            (delivery,)).fetchone()
    assert (state, attempts, last_error) == ("leased", taken.attempts, "") and lease_until > time.time()
    assert json.loads(payload) == taken.payload and wire.sent("sendMessage") == []
    host.entered = None
    with sqlite3.connect(runtime.store.path) as db:  # the new claim ends without an answer; the row is sent once
        db.execute("UPDATE outbox SET lease_until=1 WHERE delivery_id=?", (delivery,))
    later = _runtime(tmp_path, host)
    _drain(later)
    assert len(wire.sent("sendMessage")) == 1
    assert later.store.delivery_receipt(delivery)["state"] == "delivered"


# -- A voice file's own name (review fixes) ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
@pytest.mark.parametrize("name,target,queued", [("voice.ogg", "blob.bin", True), ("memo.wav", "real.ogg", False)],
                         ids=["voice_name_to_other_file", "other_name_to_voice_file"])
def test_a_voice_files_own_name_is_checked_once_and_its_resolved_bytes_are_queued(tmp_path, wire, name, target,
                                                                                  queued):
    tools = _tools(tmp_path)
    blob = tmp_path / "cache" / target
    blob.parent.mkdir()
    blob.write_bytes(b"OggS" + FIRST)
    link = tmp_path / "work" / name
    link.parent.mkdir()
    link.symlink_to(blob)
    answer = tools["telegram_send"](chat_id=CHAT, kind="voice", file_path=str(link), request_id="voice")
    if not queued:  # the name given decides, as in 0.12.0, whatever file it resolves to
        assert answer["ok"] is False and answer["code"] == "voice_format" and _copies(tmp_path) == []
        return
    assert answer["ok"], answer
    [copy] = _copies(tmp_path)
    assert copy.name == target and copy.read_bytes() == b"OggS" + FIRST  # the resolved file, named as 0.12.0 sent it
    _drain(_runtime(tmp_path))
    [(_fields, files)] = wire.sent("sendVoice")
    assert files["voice"] == (target, b"OggS" + FIRST)


# -- Copies of a send whose queueing raised (review fixes) -----------------------------------------------------------

FAILURES = [Crash, sqlite3.OperationalError]
FAILURE_IDS = ["process_ends", "store_error"]


@pytest.mark.parametrize("failure", FAILURES, ids=FAILURE_IDS)
@pytest.mark.parametrize("kind", ["document", "album"])
def test_a_copy_whose_row_was_committed_before_the_error_stays_and_is_sent(tmp_path, wire, monkeypatch, kind, failure):
    module, tools = _plugin(tmp_path)
    real = module.CustodyStore.enqueue_outbox

    def committed_then_failed(self, delivery_id, payload, *, conditional: bool):
        assert conditional is False
        assert real(self, delivery_id, payload, conditional=conditional)
        raise failure("the answer was lost after the commit")

    with monkeypatch.context() as patch:
        patch.setattr(module.CustodyStore, "enqueue_outbox", committed_then_failed)
        with pytest.raises(failure):
            _queue(tools, tmp_path, kind, "kept")
    copies = _copies(tmp_path)
    assert len(copies) == 1 + (kind == "album") and all(copy.read_bytes() == FIRST for copy in copies)
    for source in (tmp_path / "work").iterdir():
        source.write_bytes(SECOND)  # the caller's files change afterwards
    runtime = _runtime(tmp_path)
    _drain(runtime)
    [(_fields, files)] = wire.sent(METHOD[kind])
    assert len(files) == len(copies) and all(data == FIRST for _name, data in files.values())
    assert runtime.store.delivery_receipt("telegram-send:kept")["state"] == "delivered"


@pytest.mark.parametrize("failure", FAILURES, ids=FAILURE_IDS)
@pytest.mark.parametrize("kind", ["document", "album"])
def test_a_copy_whose_row_was_never_committed_is_removed(tmp_path, wire, monkeypatch, kind, failure):
    module, tools = _plugin(tmp_path)

    def refused(self, delivery_id, payload, *, conditional: bool):
        assert conditional is False
        raise failure("the store took nothing")

    with monkeypatch.context() as patch:
        patch.setattr(module.CustodyStore, "enqueue_outbox", refused)
        with pytest.raises(failure):
            _queue(tools, tmp_path, kind, "lost")
    assert _copies(tmp_path) == []
    assert _runtime(tmp_path).store.delivery_receipt("telegram-send:lost") == {"state": "not_found"}


def test_a_copy_stays_when_the_store_cannot_say_whether_its_row_was_committed(tmp_path, wire, monkeypatch):
    module, tools = _plugin(tmp_path)
    real = module.CustodyStore.enqueue_outbox

    def committed_then_failed(self, delivery_id, payload, *, conditional: bool):
        assert conditional is False
        assert real(self, delivery_id, payload, conditional=conditional)
        raise sqlite3.OperationalError("disk I/O error")

    def unreadable(self, delivery_id):
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as patch:
        patch.setattr(module.CustodyStore, "enqueue_outbox", committed_then_failed)
        patch.setattr(module.CustodyStore, "outbox_payload", unreadable)
        with pytest.raises(sqlite3.OperationalError):
            _queue(tools, tmp_path, "document", "unknown")
    [copy] = _copies(tmp_path)
    assert copy.read_bytes() == FIRST
    runtime = _runtime(tmp_path)
    _drain(runtime)
    [(_fields, files)] = wire.sent("sendDocument")
    assert files["document"] == ("unknown.pdf", FIRST)
    assert runtime.store.delivery_receipt("telegram-send:unknown")["state"] == "delivered"


def test_a_repeated_request_id_whose_queueing_raised_keeps_only_the_first_copy(tmp_path, wire, monkeypatch):
    module, tools = _plugin(tmp_path)
    source = _source(tmp_path, "note.pdf", FIRST)
    assert tools["telegram_send"](chat_id=CHAT, kind="document", file_path=str(source), request_id="same")["ok"]
    source.write_bytes(SECOND)
    real = module.CustodyStore.enqueue_outbox

    def ignored_then_failed(self, delivery_id, payload, *, conditional: bool):
        assert conditional is False
        assert not real(self, delivery_id, payload, conditional=conditional)  # already queued with the first bytes
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(module.CustodyStore, "enqueue_outbox", ignored_then_failed)
        with pytest.raises(sqlite3.OperationalError):
            tools["telegram_send"](chat_id=CHAT, kind="document", file_path=str(source), request_id="same")
    assert [copy.read_bytes() for copy in _copies(tmp_path)] == [FIRST]


# -- Another claim of the row: the stale attempt's writes name its attempt (second review round) ---------------------

class Gate(Host):
    """A Host whose discover or place read stops once, at its ``nth`` call, until the test lets it go; there the place
    read answers ``answer`` (a hold reason, or an exception to raise). Other reads answer at once, Active."""

    def __init__(self, at, nth=1, answer=""):
        super().__init__()
        self.at, self.nth, self.answer, self.seen = at, nth, answer, 0
        self.reached = self.go = None

    def arm(self):  # inside the test's event loop
        self.reached, self.go = asyncio.Event(), asyncio.Event()

    async def _stop(self, read):
        if read != self.at:
            return ""
        self.seen += 1
        if self.seen != self.nth:
            return ""
        self.reached.set()
        await self.go.wait()
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    async def discover_delivery_support(self):
        await self._stop("discover")
        return 1

    async def place_hold(self, account_id, conversation_id, thread_id=""):
        return await self._stop("place")


def _row(store, delivery_id):
    """Everything a claim, its record and its receipt consist of."""
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT state, attempts, lease_until, last_error, payload_json, reports_json, "
                               "provider_receipt_json FROM outbox WHERE delivery_id=?", (delivery_id,)).fetchone())


def _taken_again(store):
    """A reload's runtime starts (its leases go back to the queue) and claims the row."""
    store.release_own_leases()
    return store.claim_outbox(lease_sec=120)


def _finish_once(tmp_path, delivery):
    """The newer claim ends without an answer (its lease expires); the next runtime sends what is left, once."""
    with sqlite3.connect(tmp_path / "custody.sqlite3") as db:
        db.execute("UPDATE outbox SET lease_until=1 WHERE delivery_id=? AND state='leased'", (delivery,))
    later = _runtime(tmp_path)
    _drain(later)
    return later.store.delivery_receipt(delivery)


@pytest.mark.parametrize("ending", ["cancelled", "held", "host_failed", "host_failed_on_fifth_attempt"])
def test_a_stale_attempt_cancelled_held_or_failed_after_another_claim_leaves_that_claims_row_and_receipt(tmp_path, wire,
                                                                                                    ending):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, "message", "once")
    if ending == "host_failed_on_fifth_attempt":  # four earlier attempts of this version, which Telegram refused
        with sqlite3.connect(tmp_path / "custody.sqlite3") as db:
            db.execute("UPDATE outbox SET attempts=4, payload_json=json_set(payload_json, '$._attempts_marked', 4) "
                       "WHERE delivery_id=?", (delivery,))
    answer = ("Held: Telegram is paused" if ending == "held" else
              ConnectionError("Host unreachable") if ending.startswith("host_failed") else "")
    gate = Gate("place", answer=answer)
    runtime = _runtime(tmp_path, gate)
    _due(runtime.store)

    async def interleaved():
        gate.arm()
        stale = asyncio.create_task(runtime.process_one_outbox())
        await asyncio.wait_for(gate.reached.wait(), 10)  # before its first call
        taken = _taken_again(runtime.store)
        before = _row(runtime.store, delivery)
        if ending == "cancelled":
            stale.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stale
        else:
            gate.go.set()
            assert await stale
        return taken, before

    taken, before = asyncio.run(interleaved())
    assert _row(runtime.store, delivery) == before
    assert (before["state"], before["attempts"], before["last_error"]) == ("leased", taken.attempts, "")
    assert json.loads(before["payload_json"]) == taken.payload and wire.sent("sendMessage") == []
    assert _finish_once(tmp_path, delivery)["state"] == "delivered" and len(wire.sent("sendMessage")) == 1


def test_a_stale_attempts_preflight_record_does_not_replace_the_newer_claims_stamp_and_reporting(tmp_path, wire):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, "message", "once")
    gate = Gate("discover")  # the Host read that resolves the row's reporting, just before its preflight record
    runtime = _runtime(tmp_path, gate)
    _due(runtime.store)

    async def interleaved():
        gate.arm()
        stale = asyncio.create_task(runtime.process_one_outbox())
        await asyncio.wait_for(gate.reached.wait(), 10)
        taken = _taken_again(runtime.store)
        before = _row(runtime.store, delivery)
        gate.go.set()
        assert await stale
        return taken, before

    taken, before = asyncio.run(interleaved())
    assert _row(runtime.store, delivery) == before
    kept = json.loads(before["payload_json"])
    assert kept == taken.payload and kept["_attempts_marked"] == taken.attempts
    assert kept["_reporting"]["version"] == -1  # still the newer claim's to resolve
    assert wire.sent("sendMessage") == []
    assert _finish_once(tmp_path, delivery)["state"] == "delivered" and len(wire.sent("sendMessage")) == 1


@pytest.mark.parametrize("ending", ["cancelled", "continued"])
def test_a_stale_attempt_with_a_delivered_chunk_leaves_the_newer_claims_row_and_receipt(tmp_path, wire, ending):
    tools = _tools(tmp_path)
    # Two paragraphs, each under Telegram's 4096 and not both together: one chunk each.
    text = "first part " * 300 + "\n\n" + "second part " * 300
    assert tools["telegram_send"](chat_id=CHAT, text=text, markdown=False, request_id="two")["ok"]
    delivery = "telegram-send:two"
    gate = Gate("place", nth=2)  # the place read before the second chunk: the first is delivered and recorded
    runtime = _runtime(tmp_path, gate)
    _due(runtime.store)

    async def interleaved():
        gate.arm()
        stale = asyncio.create_task(runtime.process_one_outbox())
        await asyncio.wait_for(gate.reached.wait(), 10)
        taken = _taken_again(runtime.store)
        before = _row(runtime.store, delivery)
        if ending == "cancelled":
            stale.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stale
        else:
            gate.go.set()
            assert await stale
        return taken, before

    taken, before = asyncio.run(interleaved())
    assert _row(runtime.store, delivery) == before
    assert (before["state"], before["attempts"]) == ("leased", taken.attempts)
    assert len(json.loads(before["payload_json"])["_sent_messages"]) == 1 and len(wire.sent("sendMessage")) == 1
    receipt = _finish_once(tmp_path, delivery)
    chunks = [fields["text"] for fields, _files in wire.sent("sendMessage")]
    assert receipt["state"] == "delivered" and len(chunks) == 2
    assert chunks[0].startswith("first part") and chunks[1].startswith("second part")
    assert _reports(runtime.store, delivery) == [("0", "delivered"), ("1", "delivered")]


def test_a_stale_attempt_cancelled_while_the_newer_claims_call_is_open_leaves_that_call_to_finish(tmp_path, wire):
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, "message", "once")
    gate = Gate("place")
    runtime = _runtime(tmp_path, gate)
    newer = _runtime(tmp_path)
    wire.answer("sendMessage", "block")  # the newer claim's call reaches Telegram and waits for its answer
    _due(runtime.store)

    async def interleaved():
        gate.arm()
        stale = asyncio.create_task(runtime.process_one_outbox())
        await asyncio.wait_for(gate.reached.wait(), 10)
        runtime.store.release_own_leases()
        current = asyncio.create_task(newer.process_one_outbox())
        assert await asyncio.to_thread(wire.entered.wait, 10)
        before = _row(runtime.store, delivery)
        stale.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stale
        after_cancel = _row(runtime.store, delivery)
        wire.gate.set()
        assert await current
        return before, after_cancel

    before, after_cancel = asyncio.run(interleaved())
    assert after_cancel == before and before["state"] == "leased"
    assert json.loads(before["payload_json"])["_call_started"] == {"part": 0, "method": "sendMessage"}
    receipt = runtime.store.delivery_receipt(delivery)
    assert receipt["state"] == "delivered" and len(wire.sent("sendMessage")) == 1
    assert _reports(runtime.store, delivery) == [("0", "delivered")]


@pytest.mark.parametrize("kind,late", [("message", "ok"), ("photo", "ok"), ("album", "ok"), ("message", "lost"),
                                       ("message", 429)])
def test_a_stale_attempts_late_yes_after_another_claim_ended_its_row_changes_nothing_there(tmp_path, wire, kind, late):
    """The stale attempt's call was open when the row was claimed again; that claim ended it uncertain. Telegram's
    answer then arrives (a yes, a lost answer or a refusal): the stale attempt records nothing in the newer claim's row
    or receipt, and makes no other call. A yes leaves one log line with the delivery, the attempt, the chat and the
    message ids Telegram confirmed, and no words."""
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, kind, "once", **({"caption": "a private caption"} if kind != "message" else {}))
    wire.answer(METHOD[kind], ("block", late))
    notes = Notes()
    runtime = _runtime(tmp_path, logger=notes)

    async def interleaved():
        stale = asyncio.create_task(runtime.process_one_outbox())
        assert await asyncio.to_thread(wire.entered.wait, 10)  # its call reached Telegram
        runtime.store.release_own_leases()
        assert await _runtime(tmp_path).process_one_outbox()  # the newer claim sees the open call: uncertain
        before = _row(runtime.store, delivery)
        wire.gate.set()  # Telegram's yes reaches the stale attempt
        assert await stale
        return before

    before = asyncio.run(interleaved())
    assert _row(runtime.store, delivery) == before
    receipt = runtime.store.delivery_receipt(delivery)
    assert receipt["state"] == "uncertain" and len(wire.sent(METHOD[kind])) == 1
    assert _reports(runtime.store, delivery) == [("status", "uncertain")]
    lines = [(level, line) for level, line in notes.lines if "after another claim took the row" in line]
    if late != "ok":
        assert lines == []
        return
    [(_method, answer)] = [entry for entry in wire.answered if entry[0] == METHOD[kind]]
    confirmed = [message["message_id"] for message in (answer if isinstance(answer, list) else [answer])]
    [(level, line)] = lines
    assert level == "warning" and f"{delivery} attempt 1:" in line and f"message_id {confirmed}" in line
    assert f"in chat {CHAT}" in line and "hello there" not in line and "a private caption" not in line
    assert "token" not in line


def test_a_stale_attempt_that_recorded_its_last_chunk_does_not_finish_the_newer_claims_row(tmp_path, wire):
    """Its last chunk is confirmed and recorded, then another claim takes the row before the attempt marks it
    delivered: that mark changes nothing, and the newer claim finishes the row from its record with no call."""
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, "message", "once")
    runtime = _runtime(tmp_path)
    store, real, seen = runtime.store, runtime.store.checkpoint_outbox, {}

    def recorded_then_taken(delivery_id, payload, **kwargs):
        stored = real(delivery_id, payload, **kwargs)
        if "_sent_messages" in payload and not seen:
            seen["taken"] = _taken_again(store)
            seen["before"] = _row(store, delivery)
        return stored

    store.checkpoint_outbox = recorded_then_taken
    try:
        _due(store)
        assert asyncio.run(runtime.process_one_outbox())
    finally:
        del store.checkpoint_outbox
    assert _row(store, delivery) == seen["before"]
    assert (seen["before"]["state"], seen["before"]["attempts"]) == ("leased", seen["taken"].attempts)
    receipt = _finish_once(tmp_path, delivery)
    assert receipt["state"] == "delivered" and len(wire.sent("sendMessage")) == 1
    assert _reports(store, delivery) == [("0", "delivered")]


def test_send_row_writes_that_name_an_attempt_change_nothing_once_another_claim_holds_the_row(tmp_path):
    from telegram_bot.custody import CustodyStore
    store = CustodyStore(tmp_path / "custody.sqlite3")
    assert store.enqueue_outbox("telegram-send:x", {"kind": "message", "chat_id": CHAT, "text": "x"})
    stale = store.claim_outbox(lease_sec=120)
    current = _taken_again(store)
    before = _row(store, "telegram-send:x")
    report = {"part_id": "0", "state": "delivered"}
    own = ("9", CHAT, 701, "", 1, "9", "", 1, "x", "")
    assert not store.checkpoint_outbox("telegram-send:x", {"stale": True}, report=report, sent=own,
                                       attempts=stale.attempts)
    assert not store.release_outbox("telegram-send:x", reason="worker_cancelled", retry_after_sec=0,
                                    attempts=stale.attempts)
    assert not store.hold_outbox("telegram-send:x", "Held: x", attempts=stale.attempts)
    assert not store.mark_outbox_failed("telegram-send:x", reason="x",
                                        report={"part_id": "status", "state": "uncertain"}, state="uncertain",
                                        attempts=stale.attempts)
    assert not store.mark_delivered("telegram-send:x", provider_receipt={"stale": True},
                                    report={"part_id": "status", "state": "delivered"}, attempts=stale.attempts)
    assert _row(store, "telegram-send:x") == before
    assert store.reaction_target("9", CHAT, 701)["retained"] is False  # no own message remembered either
    # The holding attempt's writes apply, and a caller that names no attempt (edits, moderation, reactions) writes as
    # before.
    assert store.checkpoint_outbox("telegram-send:x", {**current.payload, "held": True}, attempts=current.attempts)
    assert store.outbox_payload("telegram-send:x")["held"] is True
    assert store.release_outbox("telegram-send:x", reason="untyped caller", retry_after_sec=0)
    assert _row(store, "telegram-send:x")["state"] == "pending"


# -- An earlier version's stored answer without a message id is evidence, not a confirmation (second review round) ---

TEXT_MESSAGE = {"message_id": 61, "date": 1_800_000_000, "text": "first chunk"}
PHOTO_MESSAGE = {"message_id": 64, "date": 1_800_000_000, "photo": [{"file_id": "p"}]}
TWO_CHUNKS = [{"text": "first chunk", "parse_mode": ""}, {"text": "second chunk", "parse_mode": ""}]


@pytest.mark.parametrize("history", [True, False], ids=["host_history", "local_receipt_only"])
@pytest.mark.parametrize("kind,stored,confirmed,unnamed", [
    ("message", [{}], [], [{}]),
    ("message", [TEXT_MESSAGE, {}], [TEXT_MESSAGE], [{}]),
    ("message", [TEXT_MESSAGE], [TEXT_MESSAGE], []),  # a confirmed first chunk, as before
    ("photo", {}, [], [{}]),
    ("album", [{}, PHOTO_MESSAGE], [PHOTO_MESSAGE], [{}]),
    ("album", [PHOTO_MESSAGE], [PHOTO_MESSAGE], []),  # one of two returned, as before
], ids=["text_empty", "text_one_named_one_empty", "text_one_named", "photo_empty", "album_one_empty",
        "album_one_of_two"])
def test_an_earlier_versions_answers_without_a_message_id_stay_as_stored_and_are_not_counted_as_confirmed(
        tmp_path, wire, kind, stored, confirmed, unnamed, history):
    runtime = _runtime(tmp_path)
    payload = _old_row(runtime.store, "telegram-send:old", kind)
    record = {**payload, ("_sent_messages" if kind == "message" else "_sent_media"): stored,
              "_reporting": {"version": 1 if history else 0, "account_id": "9", "origin": {"kind": "tool"}}}
    if kind == "message":
        record["_rendered_chunks"] = TWO_CHUNKS
    with sqlite3.connect(runtime.store.path) as db:  # 0.12.0 stopped after keeping these answers
        db.execute("UPDATE outbox SET attempts=1, state='leased', lease_until=1, payload_json=? "
                   "WHERE delivery_id='telegram-send:old'", (json.dumps(record),))
    _drain(_restart(tmp_path))
    receipt = runtime.store.delivery_receipt("telegram-send:old")
    assert receipt["state"] == "uncertain" and wire.sent(METHOD[kind]) == []
    assert receipt["confirmed_messages"] == confirmed
    assert receipt.get("answers_without_message_id", []) == unnamed
    stored_key = "_sent_messages" if kind == "message" else "_sent_media"
    assert runtime.store.outbox_payload("telegram-send:old")[stored_key] == stored  # the bytes as 0.12.0 kept them
    if history:
        status = receipt["history_reports"][-1]["payload"]
        assert status["state"] == "uncertain" and status["message"]["confirmed_parts"] == len(confirmed)
    else:
        assert receipt["history_reports"] == []


# -- A confirmed send whose finishing write fails; album answers keep their places (third review round) -------------

@pytest.mark.parametrize("store_after", ["recovers_at_once", "stays_locked", "unreadable"])
@pytest.mark.parametrize("kind", ["message", "photo", "album"])
def test_a_send_confirmed_on_its_last_attempt_ends_delivered_when_marking_it_delivered_fails(tmp_path, wire, kind,
                                                                                           store_after):
    """Four of Telegram's 429s, then its yes on the fifth and last attempt, recorded; marking the row delivered then
    fails. The row ends delivered from its record, at once or at the claim after this lease, never failed, and nothing
    is sent again. While the store cannot be read at all, nothing more is written."""
    tools = _tools(tmp_path)
    delivery = _queue(tools, tmp_path, kind, "fifth")
    wire.answer(METHOD[kind], 429, 429, 429, 429)
    runtime = _runtime(tmp_path)
    store = runtime.store
    for _ in range(4):
        _due(store)
        assert asyncio.run(runtime.process_one_outbox())
    assert store.delivery_receipt(delivery)["attempts"] == 4 and len(wire.sent(METHOD[kind])) == 4
    real_mark, calls = store.mark_delivered, []

    def unreadable(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    def locked(*args, **kwargs):
        calls.append("mark_delivered")
        if store_after == "unreadable":
            store.outbox_payload = unreadable
        if store_after == "recovers_at_once" and len(calls) > 1:
            return real_mark(*args, **kwargs)
        raise sqlite3.OperationalError("database is locked")

    store.mark_delivered = locked
    try:
        _due(store)
        assert asyncio.run(runtime.process_one_outbox())  # the fifth attempt: Telegram's yes, recorded
    finally:
        del store.mark_delivered
        vars(store).pop("outbox_payload", None)
    row = _row(store, delivery)
    assert calls and len(wire.sent(METHOD[kind])) == 5
    assert row["state"] == ("delivered" if store_after == "recovers_at_once" else "leased") and row["attempts"] == 5
    receipt = _finish_once(tmp_path, delivery)  # the store works again: the claim after the lease, if any, finishes
    assert receipt["state"] == "delivered" and len(wire.sent(METHOD[kind])) == 5
    parts = _reports(store, delivery)
    assert parts == [(str(index), "delivered") for index in range(1 + (kind == "album"))]  # no status failed


@pytest.mark.parametrize("returned,named", [
    ([None, "second"], ["second"]),
    (["first", "second", None], ["first", "second"]),
    (["first", None], ["first"]),
], ids=["first_not_a_message", "extra_entry_not_a_message", "second_not_a_message"])
def test_an_album_answer_keeps_each_messages_place_file_name_and_caption_across_a_restart(tmp_path, wire, returned,
                                                                                       named):
    tools = _tools(tmp_path)
    first, second = _source(tmp_path, "sunrise.jpg", FIRST), _source(tmp_path, "harbour.jpg", SECOND)
    items = [{"kind": "photo", "file_path": str(first), "caption": "Sunrise over the bay"},
             {"kind": "photo", "file_path": str(second), "caption": "The harbour at noon"}]
    assert tools["telegram_send"](chat_id=CHAT, kind="album", items=items, markdown=False, request_id="places")["ok"]
    sent = {"first": {**ALBUM_MESSAGE, "message_id": 811, "caption": "Sunrise over the bay"},
            "second": {**ALBUM_MESSAGE, "message_id": 812, "caption": "The harbour at noon"}}
    wire.answer("sendMediaGroup", {"result": [sent[entry] if entry else None for entry in returned]})
    runtime = _runtime(tmp_path)
    _drain(runtime)
    for _ in range(2):
        _drain(_restart(tmp_path))
    assert len(wire.sent("sendMediaGroup")) == 1
    receipt = runtime.store.delivery_receipt("telegram-send:places")
    assert receipt["state"] == "uncertain"
    confirmed = [message["message_id"] for message in receipt["confirmed_messages"]]
    assert confirmed == [sent[n]["message_id"] for n in named]
    delivered = {entry["payload"]["part_id"]: entry["payload"] for entry in receipt["history_reports"]
                 if entry["payload"]["state"] == "delivered"}
    place = {"first": ("0", "sunrise.jpg", "Sunrise over the bay"),
             "second": ("1", "harbour.jpg", "The harbour at noon")}
    assert {part: (report["message"]["file_name"], report["text"], report["message"]["message_id"])
            for part, report in delivered.items()} == {place[n][0]: (place[n][1], place[n][2], sent[n]["message_id"])
                                                         for n in named}
    status = receipt["history_reports"][-1]["payload"]
    assert status["state"] == "uncertain" and status["message"]["confirmed_parts"] == len(named)
