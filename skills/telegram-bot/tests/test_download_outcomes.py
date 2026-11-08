"""No download failure holds a chat: typed Telegram answers decide, a bounded retry ends the rest (0.12.0).

Synthetic updates only. The real TelegramClient classifies Telegram's scripted JSON answers (fake_bot_api); file
bytes also come through the real urllib reader from a local server.
"""
import asyncio
import hashlib
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from telegram_bot.api import TelegramApiError, TelegramClient, TelegramFileTooLarge, UrllibTelegramTransport
from telegram_bot.events import parse_telegram_update
from telegram_bot.host import PresenceHostError
from telegram_bot.runtime import TelegramTransportRuntime

from fake_bot_api import Clock, FakeBotApi, use_clock
from test_runtime import AcceptingSubmitter, Logger

CHAT = -1001234567890
GENERIC_400 = "Bad Request: wrong file_id or the file is temporarily unavailable"


class SizingSubmitter(AcceptingSubmitter):
    """Records the staged files' sizes and digests at submission, before the runtime removes them."""

    def __init__(self):
        super().__init__()
        self.sizes = []
        self.digests = []

    async def submit(self, event, staged_files):
        self.sizes.append([path.stat().st_size for path in staged_files])
        self.digests.append([hashlib.sha256(path.read_bytes()).hexdigest() for path in staged_files])
        return await super().submit(event, staged_files)


def _update(update_id, **message):
    return {"update_id": update_id, "message": {
        "message_id": 100 + update_id, "date": 1_700_000_000 + update_id,
        "from": {"id": 3, "is_bot": False, "first_name": "Ann"},
        "chat": {"id": CHAT, "type": "supergroup", "title": "Test group"}, **message}}


def _document(file_id, size, name="archive.zip"):
    return {"file_id": file_id, "file_unique_id": f"u-{file_id}", "file_name": name, "mime_type": "application/zip",
            "file_size": size}


def _setup(tmp_path, monkeypatch, submitter=None, api=None):
    clock = Clock()
    use_clock(monkeypatch, clock)
    api = api or FakeBotApi()
    submitter = submitter or SizingSubmitter()
    runtime = TelegramTransportRuntime(state_dir=tmp_path, token_provider=lambda: "token", logger=Logger(),
                                       submitter=submitter, client_factory=api.client)
    return clock, api, runtime, submitter


def _commit(runtime, update_id, **message):
    event = parse_telegram_update(_update(update_id, **message), bot_account_id="9", places=True,
                                  bot_username="presence_bot")
    assert event is not None
    runtime.store.commit_update(update_id, event)
    return event


def _next_due(tmp_path, now=0.0):
    """The next time a waiting row becomes due; a later message already due may still wait behind its chat's first."""
    with sqlite3.connect(tmp_path / "custody.sqlite3") as conn:
        return conn.execute("SELECT MIN(available_at) FROM inbox WHERE state='pending' AND available_at>?",
                            (now,)).fetchone()[0]


def _due(tmp_path, update_id):
    with sqlite3.connect(tmp_path / "custody.sqlite3") as conn:
        return conn.execute("SELECT available_at FROM inbox WHERE update_id=?", (update_id,)).fetchone()[0]


def _own_failures(tmp_path):
    with sqlite3.connect(tmp_path / "custody.sqlite3") as conn:
        return [row[0] for row in conn.execute("SELECT own_failures FROM download_failures")]


def _step(runtime, clock, tmp_path):
    """One inbox attempt; when nothing is due yet, the clock moves to the next due row."""
    if not asyncio.run(runtime.process_one_inbox()):
        due = _next_due(tmp_path, clock.now)
        assert due is not None, "nothing left to wait for in the inbox"
        clock.now = due


def test_generic_getfile_400_stays_transient_then_fails_after_the_bound_and_the_chat_moves_on(tmp_path, monkeypatch):
    clock, api, runtime, submitter = _setup(tmp_path, monkeypatch)
    api.refuse("zip", GENERIC_400)
    _commit(runtime, 7, document=_document("zip", 4_000_000))
    _commit(runtime, 8, text="later words of the same chat")
    start = clock.now
    while not submitter.calls:
        _step(runtime, clock, tmp_path)
        tried = len(api.methods("getFile"))
        if clock.now - start < 1800 or tried < 6:
            assert submitter.calls == [], (clock.now - start, tried)  # still transient: the event waits first
        assert clock.now - start < 3600
    event, staged = submitter.calls[0]
    record = event["message"]["attachments"][0]
    assert staged == () and record["status"] == "failed" and record["file_id"] == "zip"
    assert record["note"].startswith("not received after ") and GENERIC_400 in record["note"]
    assert clock.now - start >= 1800 and len(api.methods("getFile")) >= 6
    _step(runtime, clock, tmp_path)
    assert [sent["text"] for sent, _ in submitter.calls] == ["", "later words of the same chat"]


def test_file_is_too_big_is_terminal_at_once_and_the_next_message_flows(tmp_path, monkeypatch):
    clock, api, runtime, submitter = _setup(tmp_path, monkeypatch)
    api.refuse("big", "Bad Request: file is too big")
    _commit(runtime, 7, document=_document("big", 0))  # Telegram did not size it in the update
    _commit(runtime, 8, text="next")
    assert asyncio.run(runtime.process_one_inbox()) and asyncio.run(runtime.process_one_inbox())
    record = submitter.calls[0][0]["message"]["attachments"][0]
    assert (record["status"], record["file_id"]) == ("too_large", "big")
    assert record["note"] == ('Telegram answered "Bad Request: file is too big"; '
                              "the cloud Bot API gives bots files only up to 20 MB")
    assert len(api.methods("getFile")) == 1 and submitter.calls[1][0]["text"] == "next"


def test_an_unreadable_file_id_is_terminal_at_once_and_keeps_its_reference(tmp_path, monkeypatch):
    clock, api, runtime, submitter = _setup(tmp_path, monkeypatch)
    api.refuse("garbled", "Bad Request: invalid file_id")
    _commit(runtime, 7, document=_document("garbled", 900))
    _commit(runtime, 8, text="next")
    assert asyncio.run(runtime.process_one_inbox()) and asyncio.run(runtime.process_one_inbox())
    record = submitter.calls[0][0]["message"]["attachments"][0]
    assert record == {**_document("garbled", 900), "kind": "document", "file_unique_id": "u-garbled",
                      "status": "failed", "note": 'Telegram answered "Bad Request: invalid file_id"'}
    assert submitter.calls[1][0]["text"] == "next"


def test_no_answer_while_telegram_is_unreachable_never_counts_against_the_file(tmp_path, monkeypatch):
    clock, api, runtime, submitter = _setup(tmp_path, monkeypatch)
    api.files["zip"] = b"PK" + bytes(100)
    _commit(runtime, 7, document=_document("zip", 102))
    _commit(runtime, 8, text="next")
    asyncio.run(runtime._ready_client())  # getMe answered before the Mac slept
    api.answers["getFile"] = [ConnectionError("network is down")] * 200
    start = clock.now
    while clock.now - start < 4 * 3600:  # hours of sleep and late Wi-Fi: no poll has answered since
        _step(runtime, clock, tmp_path)
    assert submitter.calls == [] and _own_failures(tmp_path) == [0] and len(api.methods("getFile")) > 50
    api.answers["getFile"] = []  # the network is back
    runtime._provider_ok_at = clock.now
    _step(runtime, clock, tmp_path)
    _step(runtime, clock, tmp_path)
    assert submitter.calls[0][0]["message"]["attachments"][0]["status"] == "staged"
    assert submitter.sizes[0] == [102] and _own_failures(tmp_path) == []  # settled: its failure row is gone


def test_no_answer_while_polls_succeed_counts_and_ends_failed_after_the_bound(tmp_path, monkeypatch):
    clock, api, runtime, submitter = _setup(tmp_path, monkeypatch)
    _commit(runtime, 7, document=_document("slow", 19_000_000))
    _commit(runtime, 8, text="next")
    asyncio.run(runtime._ready_client())
    api.answers["getFile"] = [TimeoutError("read timed out")] * 100  # a ~20 MB file on a link that keeps stalling
    start = clock.now
    while not submitter.calls:
        runtime._provider_ok_at = clock.now  # getUpdates keeps answering meanwhile
        _step(runtime, clock, tmp_path)
        assert clock.now - start < 3600
    record = submitter.calls[0][0]["message"]["attachments"][0]
    assert record["status"] == "failed" and "no answer (TimeoutError)" in record["note"]
    assert clock.now - start >= 1800
    _step(runtime, clock, tmp_path)
    assert submitter.calls[1][0]["text"] == "next"


def test_a_flood_wait_is_honoured_and_counts_nothing(tmp_path, monkeypatch):
    clock, api, runtime, submitter = _setup(tmp_path, monkeypatch)
    api.files["zip"] = b"PK" + bytes(10)
    api.refuse("zip", "Too Many Requests: retry after 40", 429, times=1, retry_after=40)
    _commit(runtime, 7, document=_document("zip", 12))
    before = clock.now
    assert asyncio.run(runtime.process_one_inbox())
    assert submitter.calls == [] and _own_failures(tmp_path) == [0]
    assert _next_due(tmp_path) >= before + 40
    _step(runtime, clock, tmp_path)
    _step(runtime, clock, tmp_path)
    assert submitter.calls[0][0]["message"]["attachments"][0]["status"] == "staged"


def test_a_short_outage_retries_then_stages_the_file(tmp_path, monkeypatch):
    clock, api, runtime, submitter = _setup(tmp_path, monkeypatch)
    api.files["zip"] = b"PK" + bytes(20)
    api.refuse("zip", "Internal Server Error", 500, times=2)
    _commit(runtime, 7, document=_document("zip", 22), caption="the archive")
    while not submitter.calls:
        _step(runtime, clock, tmp_path)
    event, _ = submitter.calls[0]
    assert event["text"] == "the archive" and event["message"]["attachments"][0]["status"] == "staged"
    assert submitter.sizes == [[22]] and len(api.methods("getFile")) == 3


def test_a_settled_record_is_stored_so_a_host_retry_submits_the_same_event(tmp_path, monkeypatch):
    class FlakyHost(SizingSubmitter):
        async def submit(self, event, staged_files):
            if not self.calls and not getattr(self, "failed_once", False):
                self.failed_once = True
                raise PresenceHostError("presence Host request failed: ConnectionRefusedError")
            return await super().submit(event, staged_files)

    clock, api, runtime, submitter = _setup(tmp_path, monkeypatch, FlakyHost())
    api.refuse("garbled", "Bad Request: invalid file_id")
    _commit(runtime, 7, document=_document("garbled", 900))
    while not submitter.calls:
        _step(runtime, clock, tmp_path)
    assert len(api.methods("getFile")) == 1  # the retry did not ask Telegram again
    assert submitter.calls[0][0]["message"]["attachments"][0]["status"] == "failed"


def test_an_album_with_an_oversized_archive_reports_it_stages_the_small_one_and_the_chat_flows(tmp_path, monkeypatch):
    clock, api, runtime, submitter = _setup(tmp_path, monkeypatch)
    album = "13981234567890123"
    api.files["zip-small"] = bytes(4_000_000)
    _commit(runtime, 7, media_group_id=album, document=_document("zip-big", 600_000_000, "archive-full.zip"))
    _commit(runtime, 8, media_group_id=album, caption="исходники", document=_document("zip-small", 4_000_000,
                                                                                          "archive-part.zip"))
    _commit(runtime, 9, text="@presence_bot видишь архивы?", entities=[{"type": "mention", "offset": 0, "length": 13}])
    for _ in range(3):
        assert asyncio.run(runtime.process_one_inbox())
    (large, large_files), (small, small_files), (later, later_files) = submitter.calls
    record = large["message"]["attachments"][0]
    assert large["text"] == "" and large_files == ()
    assert (record["status"], record["file_id"], record["file_size"]) == ("too_large", "zip-big", 600_000_000)
    assert record["note"] == "600 MB; the cloud Bot API gives bots files only up to 20 MB"
    assert small["text"] == "исходники" and small["message"]["attachments"][0]["status"] == "staged"
    assert submitter.sizes[1] == [4_000_000]
    assert [event["message"]["media_group_id"] for event in (large, small)] == [album, album]
    assert [event["message"]["media_group_position"] for event in (large, small)] == [1, 2]
    assert later["text"] == "@presence_bot видишь архивы?" and "mention" in later["facts"]
    assert [fields["file_id"] for fields in api.methods("getFile")] == ["zip-small"]  # never the oversized one
    assert runtime.store.status_snapshot()["inbox_submitted"] == 3


def _paid_photo(file_id, size):
    return {"type": "photo", "photo": [{"file_id": file_id, "file_unique_id": f"u-{file_id}", "width": 90,
                                        "height": 90, "file_size": size}]}


def test_a_flood_wait_mid_message_stops_its_downloads_keeps_the_wait_and_what_was_decided(tmp_path, monkeypatch):
    """Paid media of five files: one fetched, one too big, a 500, then Telegram's 429 asking for 600 s. The fifth file
    is not asked for until the wait has passed, the event waits all 600 s rather than the 500's 2 s backoff, and the
    fetched bytes and the settled record are kept, so no file is fetched or asked about twice."""
    clock, api, runtime, submitter = _setup(tmp_path, monkeypatch)
    for file_id, size in (("got", 30), ("flaky", 40), ("flood", 50), ("fifth", 60)):
        api.files[file_id] = bytes([len(file_id)]) * size
    api.refuse("big", "Bad Request: file is too big")
    api.refuse("flaky", "Internal Server Error", 500, times=1)
    api.refuse("flood", "Too Many Requests: retry after 600", 429, times=1, retry_after=600)
    paid = {"star_count": 5, "paid_media": [_paid_photo("got", 30), _paid_photo("big", 0), _paid_photo("flaky", 40),
                                            _paid_photo("flood", 50), _paid_photo("fifth", 60)]}
    _commit(runtime, 7, paid_media=paid, caption="for subscribers")
    _commit(runtime, 8, text="later words of the same chat")
    start = clock.now
    assert asyncio.run(runtime.process_one_inbox())
    assert [fields["file_id"] for fields in api.methods("getFile")] == ["got", "big", "flaky", "flood"]
    assert submitter.calls == [] and _due(tmp_path, 7) >= start + 600  # Telegram's wait, not the 500's backoff
    with sqlite3.connect(tmp_path / "custody.sqlite3") as conn:
        kept = json.loads(conn.execute("SELECT payload_json FROM inbox WHERE update_id=7").fetchone()[0])
    assert [record.get("status") for record in kept["message"]["attachments"]] == [
        "staged", "too_large", None, None, None]
    assert sorted(_own_failures(tmp_path)) == [0, 1]  # the 500 counts; the flood wait counts nothing
    clock.now = start + 599
    assert not asyncio.run(runtime.process_one_inbox())  # still waiting, and the chat's later message behind it
    clock.now = start + 600
    assert asyncio.run(runtime.process_one_inbox())
    event, _ = submitter.calls[0]
    assert event["text"] == "for subscribers"
    assert [record["status"] for record in event["message"]["attachments"]] == [
        "staged", "too_large", "staged", "staged", "staged"]
    assert submitter.sizes == [[30, 40, 50, 60]]
    assert [fields["file_id"] for fields in api.methods("getFile")][4:] == ["flaky", "flood", "fifth"]
    assert [fields["file_id"] for fields in api.methods("download")] == ["got", "flaky", "flood", "fifth"]
    assert _own_failures(tmp_path) == []
    assert asyncio.run(runtime.process_one_inbox())
    assert submitter.calls[1][0]["text"] == "later words of the same chat"


# -- The real urllib reader ---------------------------------------------------------------------------------------

class FileServer:
    """A Bot API server on 127.0.0.1: each name's bytes served whole or cut short by a closed connection, with a
    Content-Length or chunked, to a GET (a file) or a POST (a JSON answer). Synthetic bytes only."""

    def __init__(self):
        served = self.served = {}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                self._serve()

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._serve()

            def _serve(self):
                body, mode = served[self.path.rsplit("/", 1)[-1]]
                sent = body[: len(body) // 2] if mode.endswith("_cut") else body
                self.send_response(200)
                if mode.startswith("chunked"):  # one chunk, cut in its middle when the connection ends early
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    end = b"" if mode.endswith("_cut") else b"\r\n0\r\n\r\n"
                    self.wfile.write(b"%x\r\n" % len(body) + sent + end)
                else:
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(sent)
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def serve(self, name, body, mode):
        self.served[name] = (body, mode)
        return f"{self.base}/file/bottoken/documents/{name}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture
def file_server():
    server = FileServer()
    yield server
    server.close()


class UrllibFiles(FakeBotApi):
    """Scripted JSON answers as everywhere here; each file's bytes come through the real urllib reader."""

    def __init__(self, server):
        super().__init__()
        self.server = server

    def client(self, token="token"):
        return TelegramClient(token, transport=self, api_server=self.server.base)

    async def get_bytes(self, url, timeout_sec, max_bytes):
        self.calls.append(("download", {"file_id": url.rsplit("/", 1)[-1]}))
        return await UrllibTelegramTransport().get_bytes(url, timeout_sec, max_bytes)


def test_the_urllib_reader_turns_a_body_cut_short_into_a_lost_answer_and_keeps_whole_bodies_exact(file_server):
    """Python's reader raises IncompleteRead, which is no OSError, for a cut chunked body, and returns a body cut below
    its Content-Length without complaint. Each is a lost answer: no code, never an escaped exception or a short file."""
    reader, data = UrllibTelegramTransport(), bytes(range(256)) * 64
    answer = json.dumps({"ok": True, "result": {"file_id": "x", "file_path": "documents/x"}}).encode()
    for mode in ("chunked_cut", "length_cut"):
        with pytest.raises(TelegramApiError) as caught:
            asyncio.run(reader.get_bytes(file_server.serve(f"file-{mode}", data, mode), 5.0, 2 * len(data)))
        assert (type(caught.value), caught.value.error_code, caught.value.description) == (
            TelegramApiError, 0, "IncompleteRead")
        with pytest.raises(TelegramApiError) as caught:
            asyncio.run(reader.post_json(file_server.serve(f"json-{mode}", answer, mode), {}, 5.0))
        assert (type(caught.value), caught.value.error_code) == (TelegramApiError, 0)
    for mode in ("length", "chunked"):  # whole bodies stay exact, up to and including the limit
        assert asyncio.run(reader.get_bytes(file_server.serve(f"file-{mode}", data, mode), 5.0, len(data))) == data
        assert asyncio.run(reader.post_json(file_server.serve(f"json-{mode}", answer, mode), {}, 5.0))["ok"] is True
        with pytest.raises(TelegramFileTooLarge):
            asyncio.run(reader.get_bytes(file_server.serve(f"file-{mode}", data, mode), 5.0, len(data) - 1))


def test_a_file_served_at_another_size_than_getfile_declared_is_a_lost_answer_and_nothing_is_written(tmp_path):
    """getFile's file_size is the file: fewer or more bytes, even in a whole HTTP answer, are no answer with no code,
    before anything is written. An exact size, and a missing, zero, negative or unreadable one, download as before."""
    api, served = FakeBotApi(), bytes(range(200)) * 2
    client = api.client()
    sizes = {"short": 1000, "long": 300, "exact": 400, "missing": None, "zero": 0, "negative": -5, "unreadable": "many"}
    for file_id, size in sizes.items():
        api.files[file_id], api.declared_sizes[file_id] = served, size
    for file_id in ("short", "long"):
        destination = tmp_path / file_id / "file.bin"
        with pytest.raises(TelegramApiError) as caught:
            asyncio.run(client.download_file(file_id, destination))
        assert (type(caught.value), caught.value.error_code, caught.value.description) == (
            TelegramApiError, 0, f"received 400 bytes, getFile said {sizes[file_id]}")
        assert not destination.parent.exists()  # neither the file nor its .tmp, not even the folder
    for file_id in ("exact", "missing", "zero", "negative", "unreadable"):
        destination = tmp_path / file_id / "file.bin"
        assert asyncio.run(client.download_file(file_id, destination)) == destination
        assert destination.read_bytes() == served and sorted(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("mode,declared,last", [
    ("chunked_cut", None, "IncompleteRead"), ("length_cut", None, "IncompleteRead"),
    # A whole HTTP answer, fewer bytes than getFile declared: only that size says the file is short.
    ("length", 4698, "received 4098 bytes, getFile said 4698")])
def test_a_download_cut_short_counts_only_while_polls_answer_then_ends_failed_and_the_chat_moves_on(
        tmp_path, monkeypatch, file_server, mode, declared, last):
    api = UrllibFiles(file_server)
    clock, _, runtime, submitter = _setup(tmp_path, monkeypatch, api=api)
    body = b"PK" + bytes(range(256)) * 16
    api.files["cut"] = body
    if declared is not None:
        api.declared_sizes["cut"] = declared
    file_server.serve("cut", body, mode)
    _commit(runtime, 7, document=_document("cut", declared or len(body)))
    _commit(runtime, 8, text="next")
    asyncio.run(runtime._ready_client())
    start = clock.now
    while clock.now - start < 2 * 3600:  # no poll has answered since: a cut read says nothing about the file
        _step(runtime, clock, tmp_path)
    assert submitter.calls == [] and _own_failures(tmp_path) == [0] and len(api.methods("download")) > 20
    start = clock.now
    while not submitter.calls:  # getUpdates answers again, so each cut read now counts against the file
        runtime._provider_ok_at = clock.now
        _step(runtime, clock, tmp_path)
        assert clock.now - start < 3600
    event, staged = submitter.calls[0]
    record = event["message"]["attachments"][0]
    assert staged == () and record["status"] == "failed" and record["file_id"] == "cut"
    assert record["note"].endswith(f"last: no answer ({last})") and clock.now - start >= 1800
    _step(runtime, clock, tmp_path)
    assert [sent["text"] for sent, _ in submitter.calls] == ["", "next"]


@pytest.mark.parametrize("mode", ["length", "chunked"])
def test_the_real_reader_stages_a_whole_file_unchanged_at_once(tmp_path, monkeypatch, file_server, mode):
    api = UrllibFiles(file_server)
    clock, _, runtime, submitter = _setup(tmp_path, monkeypatch, api=api)
    body = bytes(range(256)) * 300
    api.files["whole"] = body
    file_server.serve("whole", body, mode)
    _commit(runtime, 7, document=_document("whole", len(body)))
    assert asyncio.run(runtime.process_one_inbox())
    assert submitter.calls[0][0]["message"]["attachments"][0]["status"] == "staged"
    assert submitter.digests == [[hashlib.sha256(body).hexdigest()]] and len(api.methods("download")) == 1
