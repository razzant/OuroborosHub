"""The bot token never reaches a reader through error text (review S4-1).

Telegram's Bot API carries the token in the URL path (``/bot<token>/<method>``, ``/file/bot<token>/<path>``). Every
path that records or returns error text is driven with a synthetic token against a loopback fake Bot API (no Telegram,
no real service): connection refused, DNS failure, TLS failure, timeout, HTTP errors with and without a JSON body, a
non-JSON answer, getFile and the file download, a multipart upload, a Host submission error and a local SQLite error.
The token must appear in none of the error texts, the status route, the receipts, the logs or the custody store.
"""
import asyncio
import importlib.util
import itertools
import json
import pathlib
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from telegram_bot import runtime as runtime_mod
from telegram_bot.api import TelegramApiError, TelegramClient
from telegram_bot.events import parse_telegram_update
from telegram_bot.host import PresenceHostError

TOKEN = "1234567890:AAEexampleexampleexampleexampleexam"
_PLUGINS = itertools.count()


class FakeBotApi(ThreadingHTTPServer):
    """Answers like a broken Bot API, per method; every request path (with the token in it) is kept."""

    def __init__(self):
        self.paths = []
        self.answers = {
            "getMe": (401, "application/json", {"ok": False, "error_code": 401, "description": "Unauthorized"}),
            "getUpdates": (502, "text/html", "<html>Bad Gateway</html>"),
            "deleteWebhook": (200, "text/html", "<html>not json</html>"),
            "sendMessage": (500, "application/json", {"ok": False, "error_code": 500, "description": "Internal"}),
            "sendDocument": (502, "text/html", "<html>Bad Gateway</html>"),
            "getFile": (200, "application/json", {"ok": True, "result": {"file_id": "p", "file_path": "photos/p.jpg",
                                                                          "file_size": 4}}),
            "slow": "sleep",
        }
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_port}"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self):
        self.server.paths.append(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path.startswith("/file/"):
            status, mime, body = 404, "text/plain", "missing"
        else:
            answer = self.server.answers.get(self.path.rsplit("/", 1)[-1], (404, "text/plain", "no route"))
            if answer == "sleep":
                time.sleep(1.0)
                answer = (200, "application/json", {"ok": True, "result": True})
            status, mime, body = answer
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass  # the client gave up (the timeout case)

    do_GET = do_POST = _answer


class _Logger:
    def __init__(self):
        self.lines = []

    def log(self, level, message):
        self.lines.append(f"{level}: {message}")


class _RefusingHost:
    """A Host whose every answer is an error, as a submission failure records it."""
    places_version = 1
    delivery_reporting_status = "unsupported"

    async def submit(self, event, staged_files):
        raise PresenceHostError("presence Host request failed: ConnectionResetError")

    async def poll(self, work_ref, binding_id):
        raise AssertionError("nothing was admitted")


def _plugin():
    root = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(f"telegram_secret_text_{next(_PLUGINS)}", root / "plugin.py",
                                                  submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _store_text(path):
    """Every text value the custody store holds, as one string."""
    with sqlite3.connect(path) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return "\n".join(str(value) for table in tables for row in conn.execute(f"SELECT * FROM {table}")
                         for value in row if isinstance(value, str))


async def _errors_of_direct_calls(server, monkeypatch):
    """The error text of each client path, as the runtime records it (runtime._error)."""
    texts = {}

    async def record(name, call):
        try:
            await call
        except Exception as exc:  # every path here fails by design
            texts[name] = runtime_mod._error(exc)

    client = TelegramClient(TOKEN, api_server=server.base)
    await record("getMe 401 JSON", client.get_me())
    await record("getUpdates 502 HTML", client.get_updates(offset=0, timeout_sec=1))
    await record("200 not JSON", client.delete_webhook())
    await record("sendMessage 500 JSON", client.send_message("5", "hi"))
    await record("timeout", client._call("slow", {}, timeout_sec=0.3))
    await record("getFile ok, download 404", client.download_file("p", pathlib.Path(server.scratch) / "p.jpg"))
    document = pathlib.Path(server.scratch) / "d.pdf"
    document.write_bytes(b"%PDF-1.4 synthetic")
    await record("sendDocument upload 502", client.send_document("5", document))
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    await record("connection refused", TelegramClient(TOKEN, api_server=f"http://127.0.0.1:{port}").get_me())
    await record("TLS failure", TelegramClient(TOKEN, api_server=f"https://127.0.0.1:{server.server_port}").get_me())

    def no_name(*_args, **_kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")
    with monkeypatch.context() as patched:
        patched.setattr(socket, "getaddrinfo", no_name)
        await record("DNS failure", TelegramClient(TOKEN, api_server="https://api.telegram.org").get_me())
        await record("DNS failure, file download",
                     TelegramClient(TOKEN, api_server="https://api.telegram.org").download_file(
                         "p", pathlib.Path(server.scratch) / "q.jpg"))
    return texts


def test_the_bot_token_never_reaches_a_reader_through_error_text(tmp_path, monkeypatch):
    server = FakeBotApi()
    server.scratch = str(tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger = _Logger()

    async def run():
        texts = await _errors_of_direct_calls(server, monkeypatch)
        assert len(texts) == 11, texts  # every path failed and recorded its text
        assert texts["getMe 401 JSON"].endswith("(401): Unauthorized")
        server.answers["deleteWebhook"] = (200, "application/json", {"ok": True, "result": True})
        runtime = runtime_mod.TelegramTransportRuntime(
            state_dir=tmp_path / "state", token_provider=lambda: TOKEN, logger=logger, submitter=_RefusingHost(),
            client_factory=lambda token: TelegramClient(token, api_server=server.base))
        plugin = _plugin()
        plugin._RUNTIME, plugin._STATE_DIR = runtime, tmp_path / "state"
        held = asyncio.Event()

        async def hold(_seconds):
            await held.wait()
        runtime._pause = hold
        reads = []

        async def route():
            reads.append((await plugin._status_route(None)).body.decode())

        # The poll loop: getMe refused (401), recorded as the link's failure and the newest error.
        loop = asyncio.create_task(runtime._poll_loop())
        runtime._run_task = loop
        for _ in range(500):
            if runtime.status_snapshot({})["poll_result"] == "failed":
                break
            await asyncio.sleep(0.01)
        await route()
        assert json.loads(reads[-1])["poll_failure"] == "token"
        # Sends: a message refused with a JSON body, a document upload answered by an HTML gateway page.
        document = tmp_path / "upload.pdf"
        document.write_bytes(b"%PDF-1.4 synthetic")
        runtime._client, runtime._client_token = TelegramClient(TOKEN, api_server=server.base), TOKEN
        runtime._bot_id = "9"
        runtime.store.enqueue_outbox("telegram-send:m", {"kind": "message", "chat_id": "5", "text": "hi"})
        runtime.store.enqueue_outbox("telegram-send:d", {"kind": "document", "chat_id": "6", "file_path": str(document)})
        assert await runtime.process_one_outbox() and await runtime.process_one_outbox()
        receipts = [runtime.store.delivery_receipt(f"telegram-send:{name}") for name in ("m", "d")]
        assert all(receipt["error"] for receipt in receipts)
        # An update with a photo: getFile answers, the download is refused; then the Host refuses the handoff.
        event = parse_telegram_update({"update_id": 7, "message": {
            "message_id": 2, "caption": "photo", "from": {"id": 3, "is_bot": False}, "chat": {"id": 4, "type": "private"},
            "photo": [{"file_id": "p", "file_unique_id": "u", "file_size": 4}]}}, bot_account_id="9")
        runtime.store.commit_update(7, event)
        assert await runtime.process_one_inbox()
        await route()
        # A local SQLite failure while recording Telegram's answer.
        runtime._last_error = runtime_mod._error(sqlite3.OperationalError("database is locked"))
        await route()
        runtime._stopping = True
        held.set()
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        return texts, receipts, reads, runtime

    try:
        texts, receipts, reads, runtime = asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
    # The token was really in the requests' paths, so a leak had something to leak.
    assert any(f"/bot{TOKEN}/" in path for path in server.paths)
    assert any(f"/file/bot{TOKEN}/" in path for path in server.paths)
    secret = TOKEN.split(":", 1)[1]
    readable = {"error texts": json.dumps(texts), "receipts": json.dumps(receipts), "status route": "\n".join(reads),
                "logs": "\n".join(logger.lines), "custody store": _store_text(runtime.store.path)}
    for where, text in readable.items():
        assert secret not in text, where
    runtime.store.close()
