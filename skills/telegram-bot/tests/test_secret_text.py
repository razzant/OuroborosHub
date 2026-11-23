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
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from telegram_bot import runtime as runtime_mod
from telegram_bot.api import TelegramApiError, TelegramClient
from telegram_bot.events import parse_telegram_update
from telegram_bot.host import PresenceHostError

TOKEN = "1234567890:AAEexampleexampleexampleexampleexam"
# The realistic way a token breaks a URL: pasted with an inner space or tab. urllib refuses such a request line
# (http.client.InvalidURL) before any connection, with the URL, token included, in its message.
TOKENS = {"plain": TOKEN, "space": "1234567890:AAEexample exampleexampleexampleexam",
          "tab": "1234567890:AAEexample\texampleexampleexampleexam"}
_PLUGINS = itertools.count()


class FakeBotApi(ThreadingHTTPServer):
    """Answers like a broken Bot API, per method; every request path (with the token in it) is kept."""

    def __init__(self):
        self.paths = []
        self.answers = {
            "getMe": (401, "application/json", {"ok": False, "error_code": 401, "description": "Unauthorized"}),
            "getUpdates": (502, "text/html", "<html>Bad Gateway</html>"),
            "deleteWebhook": (200, "text/html", "<html>not json</html>"),
            # A provider that echoes the request path, raw and URL-quoted, in its description.
            "sendMessage": "echo",
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
            elif answer == "echo":
                answer = (400, "application/json", {"ok": False, "error_code": 400, "description":
                          f"Bad Request: no route {self.path} ({urllib.parse.quote(self.path, safe='')})"})
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


class _ReportingHost:
    """A Host that records delivery reports (so a failed send's report, with its error, is sent and kept here) and
    refuses every turn, as a submission failure records it."""
    places_version = 1
    delivery_reporting_status = "supported"
    delivery_reporting_version = 1
    presence_explicit_completion = 1

    def __init__(self):
        self.reports = []

    async def discover_delivery_support(self):
        return 1

    async def place_hold(self, account, conversation, thread_id=""):
        return ""

    async def report_delivery(self, payload):
        self.reports.append(payload)

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


async def _errors_of_direct_calls(server, monkeypatch, token=TOKEN):
    """The error text of each client path, as the runtime records it (runtime._error)."""
    texts = {}

    async def record(name, call):
        try:
            await call
        except Exception as exc:  # every path here fails by design
            texts[name] = runtime_mod._error(exc)

    client = TelegramClient(token, api_server=server.base)
    await record("getMe 401 JSON", client.get_me())
    await record("getUpdates 502 HTML", client.get_updates(offset=0, timeout_sec=1))
    await record("200 not JSON", client.delete_webhook())
    await record("sendMessage 400 echoing the path", client.send_message("5", "hi"))
    await record("timeout", client._call("slow", {}, timeout_sec=0.3))
    await record("getFile ok, download 404", client.download_file("p", pathlib.Path(server.scratch) / "p.jpg"))
    document = pathlib.Path(server.scratch) / "d.pdf"
    document.write_bytes(b"%PDF-1.4 synthetic")
    await record("sendDocument upload 502", client.send_document("5", document))
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    await record("connection refused", TelegramClient(token, api_server=f"http://127.0.0.1:{port}").get_me())
    await record("TLS failure", TelegramClient(token, api_server=f"https://127.0.0.1:{server.server_port}").get_me())

    def no_name(*_args, **_kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")
    with monkeypatch.context() as patched:
        patched.setattr(socket, "getaddrinfo", no_name)
        await record("DNS failure", TelegramClient(token, api_server="https://api.telegram.org").get_me())
        await record("DNS failure, file download",
                     TelegramClient(token, api_server="https://api.telegram.org").download_file(
                         "p", pathlib.Path(server.scratch) / "q.jpg"))
    return texts


@pytest.mark.parametrize("kind", list(TOKENS))
def test_the_bot_token_never_reaches_a_reader_through_error_text(tmp_path, monkeypatch, kind):
    token = TOKENS[kind]
    server = FakeBotApi()
    server.scratch = str(tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger = _Logger()
    host = _ReportingHost()

    async def run():
        texts = await _errors_of_direct_calls(server, monkeypatch, token)
        assert len(texts) == 11, texts  # every path failed and recorded its text
        if kind == "plain":
            assert texts["getMe 401 JSON"].endswith("(401): Unauthorized")
            # The echoed path keeps the error's shape, with the token replaced by a fixed marker.
            assert "/bot<token>/sendMessage" in texts["sendMessage 400 echoing the path"]
        else:
            # A token urllib cannot put in a request line: refused here, before any connection; only the type name.
            assert all(text.endswith("InvalidURL") for name, text in texts.items() if not name.startswith("DNS")), texts
        server.answers["deleteWebhook"] = (200, "application/json", {"ok": True, "result": True})
        runtime = runtime_mod.TelegramTransportRuntime(
            state_dir=tmp_path / "state", token_provider=lambda: token, logger=logger, submitter=host,
            client_factory=lambda value: TelegramClient(value, api_server=server.base))
        plugin = _plugin()
        plugin._RUNTIME, plugin._STATE_DIR = runtime, tmp_path / "state"
        held = asyncio.Event()

        async def hold(_seconds):
            await held.wait()
        runtime._pause = hold
        reads = []

        async def route():
            reads.append((await plugin._status_route(None)).body.decode())

        # The poll loop: getMe refused (401), or the token refused here as malformed.
        loop = asyncio.create_task(runtime._poll_loop())
        runtime._run_task = loop
        for _ in range(500):
            if runtime.status_snapshot({})["poll_result"] == "failed":
                break
            await asyncio.sleep(0.01)
        await route()
        assert json.loads(reads[-1])["poll_failure"] == ("token" if kind == "plain" else "token_format")
        # Sends, each with a delivery report for the Host: a message refused with a path-echoing description, and a
        # document upload answered by an HTML gateway page.
        document = tmp_path / "upload.pdf"
        document.write_bytes(b"%PDF-1.4 synthetic")
        runtime._client, runtime._client_token = TelegramClient(token, api_server=server.base), token
        runtime._bot_id = "9"
        reporting = {"version": -1, "origin": {"kind": "tool"}}
        runtime.store.enqueue_outbox("telegram-send:m", {"kind": "message", "chat_id": "5", "text": "hi",
                                                         "_reporting": dict(reporting)})
        runtime.store.enqueue_outbox("telegram-send:d", {"kind": "document", "chat_id": "6", "file_path": str(document),
                                                         "_reporting": dict(reporting)})
        for _ in range(12):  # each row until it ends (a refusal before any call is retried up to its limit)
            with sqlite3.connect(runtime.store.path) as conn:
                conn.execute("UPDATE outbox SET available_at=0 WHERE state='pending'")
            await runtime.process_one_outbox()
            while await runtime.process_one_delivery_report():
                pass
        receipts = [runtime.store.delivery_receipt(f"telegram-send:{name}") for name in ("m", "d")]
        assert all(receipt["state"] in {"failed", "uncertain"} and receipt["error"] for receipt in receipts), receipts
        assert host.reports, "the reporting Host received the failed sends' reports"
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
    if kind == "plain":
        # The token was really in the requests' paths, so a leak had something to leak.
        assert any(f"/bot{token}/" in path for path in server.paths)
        assert any(f"/file/bot{token}/" in path for path in server.paths)
    else:
        assert server.paths == []  # no request with a malformed token ever reached a socket
    secret = token.split(":", 1)[1]
    fragments = [secret, *secret.split()]  # also each piece of a token split by its space or tab
    readable = {"error texts": json.dumps(texts), "receipts": json.dumps(receipts), "status route": "\n".join(reads),
                "Host reports": json.dumps(host.reports), "logs": "\n".join(logger.lines),
                "custody store": _store_text(runtime.store.path)}
    for where, text in readable.items():
        for fragment in fragments:
            assert fragment not in text, (where, fragment)
            assert urllib.parse.quote(fragment, safe="") not in text, (where, "quoted", fragment)
    runtime.store.close()


# Characters urllib cannot write in a request line: control characters (InvalidURL) and a non-ASCII one
# (UnicodeEncodeError). Surrounding whitespace is stripped from the token; these sit inside it.
_UNWRITABLE = {"space": " ", "tab": "\t", "newline": "\n", "carriage-return": "\r", "nul": "\x00",
               "control": "\x01", "del": "\x7f", "non-ascii": "\u00e9"}


@pytest.mark.parametrize("bad", list(_UNWRITABLE.values()), ids=list(_UNWRITABLE))
def test_no_printed_traceback_of_a_refused_token_holds_the_token(bad):
    """Review wave 5, 7a. What Python's default handlers print (sys.excepthook, a thread's excepthook, asyncio's
    "Task exception was never retrieved", logging with exc_info) is the whole chained traceback. A failure while
    handling the refusal, such as a SQLite write in a worker's handler, chains the refusal in turn. urllib's
    InvalidURL quotes the request path, token included, so the refusal must not chain it."""
    token = f"1234567890:AAEexample{bad}exampleexampleexampleexam"
    client = TelegramClient(token, api_server="http://127.0.0.1:9")  # refused before any connection
    with pytest.raises(TelegramApiError) as caught:
        asyncio.run(client.get_me())
    try:
        try:
            raise caught.value
        except TelegramApiError:
            raise sqlite3.OperationalError("database is locked")
    except sqlite3.OperationalError as later:
        printed = "".join(traceback.format_exception(type(later), later, later.__traceback__))
    assert "database is locked" in printed and str(caught.value) in printed
    for fragment in ("AAEexample", "exampleexampleexampleexam"):
        assert fragment not in printed, printed
