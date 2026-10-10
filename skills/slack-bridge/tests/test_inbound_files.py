"""Inbound attachments through the real parser, store, worker, Slack client and Host adapter.

Provider and Host traffic is ``httpx.MockTransport``. File objects follow the shapes in
https://docs.slack.dev/reference/objects/file-object/ (synthetic, not a recorded incident
payload): an external Google Drive file whose ``url_private`` names the external host, a
Slack-hosted PDF, and an ID-only declaration with ``file_access: check_file_info``.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from lib.events import parse_socket_envelope
from lib.host_adapter import LoopbackPresenceHostAdapter, _completed_reference
from lib.runtime import InboundWorker
from lib.slack_api import SlackApiError, SlackClient
from lib.store import BridgeStore

BINDING = "b" * 32
FILE_HOST = "https://files.slack.com/files-pri/T1-"
DRIVE = {
    "id": "F_DRIVE", "name": "Roadmap", "title": "Roadmap", "mimetype": "application/vnd.google-apps.document",
    "filetype": "gdoc", "pretty_type": "Google Docs", "mode": "external", "is_external": True,
    "external_type": "gdrive", "external_id": "doc-1",
    "external_url": "https://docs.google.com/document/d/doc-1/edit",
    "url_private": "https://docs.google.com/document/d/doc-1/edit",
    "permalink": "https://example.slack.com/files/U1/F_DRIVE/roadmap",
    "permalink_public": "https://slack-files.com/T1-F_DRIVE-0123456789", "thumb_64": "https://files.slack.com/thumb",
}
STUB = {"id": "F_STUB", "file_access": "check_file_info", "mode": "hidden_by_limit", "is_hidden_by_limit": True}
PRIVATE_KEYS = {"url_private", "url_private_download", "permalink_public", "thumb_64", "preview"}


def _slack_file(file_id: str, name: str = "brief.pdf", size: int = 5) -> dict:
    return {
        "id": file_id, "name": name, "title": name, "mimetype": "application/pdf", "filetype": "pdf",
        "size": size, "mode": "hosted", "url_private": f"{FILE_HOST}{file_id}/{name}",
        "url_private_download": f"{FILE_HOST}{file_id}/download/{name}",
        "permalink": f"https://example.slack.com/files/U1/{file_id}/{name}",
        "permalink_public": f"https://slack-files.com/T1-{file_id}-secret", "preview": "%PDF preview",
    }


def _envelope(event_id: str, *, ts: str, files=(), text: str = "", thread_ts: str = "",
              channel: str = "C1", channel_type: str = "channel") -> dict:
    event = {"type": "message", "user": "U1", "channel": channel, "channel_type": channel_type,
             "ts": ts, "text": text}
    if files:
        event["files"] = list(files)
    if thread_ts:
        event["thread_ts"] = thread_ts
    return {"type": "events_api", "envelope_id": f"env-{event_id}",
            "payload": {"event_id": event_id, "team_id": "T1", "event": event}}


class _Provider:
    """Slack Web API plus file host; every request is recorded."""

    def __init__(self, files: dict | None = None) -> None:
        self.files = dict(files or {})
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "slack.com":
            if request.url.path.endswith("users.info"):
                return httpx.Response(200, json={"ok": True, "user": {"id": request.url.params["user"]}})
            return httpx.Response(200, json={"ok": True, "channel": {"id": request.url.params["channel"]}})
        answers = self.files.get(request.url.path)
        if answers is None:
            return httpx.Response(200, content=b"never requested")
        answer = answers.pop(0) if isinstance(answers, list) else answers
        return answer(request) if callable(answer) else answer

    def file_requests(self) -> list[tuple[str, str]]:
        return [(item.url.host, item.url.path) for item in self.requests if item.url.host != "slack.com"]


class _Host:
    """Loopback Presence route; ``lose`` drops that many turn replies after receipt."""

    def __init__(self) -> None:
        self.turns: list[dict] = []
        self.lose = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/identity":
            return httpx.Response(200, json={"ok": True})
        self.turns.append(json.loads(request.content))
        if self.lose:
            self.lose -= 1
            raise httpx.ReadTimeout("reply lost", request=request)
        return httpx.Response(200, json={"ok": True, "status": "completed", "outcome": "silent",
                                         "text": "", "turn_ref": "turn", "work_ref": ""})

    def events(self) -> list[dict]:
        return [turn["event"] for turn in self.turns]


class _Bridge:
    def __init__(self, root: Path, provider: _Provider, host: _Host) -> None:
        self.root, self.provider, self.host = root, provider, host
        self.store = BridgeStore(root)

    async def run(self, steps: int = 1) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.provider)) as slack_http, \
                httpx.AsyncClient(transport=httpx.MockTransport(self.host)) as host_http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=slack_http)
            adapter = LoopbackPresenceHostAdapter(binding_id=BINDING, host_service_url="http://127.0.0.1:8767",
                                                  skill_token="skill-token", http_client=host_http)
            worker = InboundWorker(self.store, slack, adapter, staged_root=self.root / "staged")
            for _ in range(steps):
                await worker.process_once()

    def ingest(self, *envelopes: dict) -> None:
        for envelope in envelopes:
            self.store.ingest_envelope(envelope, parse_socket_envelope(envelope, bot_user_id="U_BOT"))

    def rows(self) -> dict[str, sqlite3.Row]:
        with sqlite3.connect(self.store.path) as db:
            db.row_factory = sqlite3.Row
            return {row["event_id"]: row for row in db.execute("SELECT * FROM inbox ORDER BY id")}

    def release(self, event_id: str) -> None:
        """Let one retried row's own backoff elapse; other rows keep their schedule."""
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE inbox SET available_at=0 WHERE event_id=?", (event_id,))


def _ok(body: bytes = b"bytes"):
    return lambda _request: httpx.Response(200, content=body)


def _attachments(event: dict) -> list[dict]:
    return event["message"]["attachments"]


def test_external_file_message_reaches_host_with_facts_and_its_thread_moves_on(tmp_path):
    provider, host = _Provider(), _Host()
    bridge = _Bridge(tmp_path, provider, host)
    bridge.ingest(
        _envelope("Ev-1", ts="1.0", files=[DRIVE]),
        _envelope("Ev-2", ts="2.0", thread_ts="1.0", text="did you see it?"),
        _envelope("Ev-3", ts="3.0", thread_ts="1.0", text="anyone?"),
    )
    asyncio.run(bridge.run(steps=3))

    assert [event["source_event_id"] for event in host.events()] == ["Ev-1", "Ev-2", "Ev-3"]
    first = host.turns[0]
    assert first["staged_files"] == []
    assert first["event"]["text"] == "" and first["event"]["actor"]["platform_actor_id"] == "U1"
    [drive] = _attachments(first["event"])
    assert drive == {
        "file_id": "F_DRIVE", "file_name": "Roadmap", "mime_type": "application/vnd.google-apps.document",
        "file_size": 0, "title": "Roadmap", "filetype": "gdoc", "pretty_type": "Google Docs", "mode": "external",
        "external_type": "gdrive", "external_id": "doc-1", "external_url": DRIVE["external_url"],
        "permalink": DRIVE["permalink"], "is_external": True, "content_available": False,
        "stage_error": "private_file_host_not_allowed", "stage_error_details": {"host": "docs.google.com"},
    }
    # The credential never left Slack and no file host was asked at all.
    assert provider.file_requests() == []
    assert {row["state"] for row in bridge.rows().values()} == {"delivered"}
    assert not (tmp_path / "staged").exists()


def test_mixed_declarations_keep_order_and_submit_only_staged_bytes(tmp_path):
    pdf = _slack_file("F_PDF", name="Roadmap")
    provider = _Provider({"/files-pri/T1-F_PDF/download/Roadmap": _ok(b"%PDF1")})
    host = _Host()
    bridge = _Bridge(tmp_path, provider, host)
    bridge.ingest(_envelope("Ev-1", ts="1.0", text="three files", files=[DRIVE, pdf, STUB]))
    asyncio.run(bridge.run())

    [turn] = host.turns
    drive, staged, stub = _attachments(turn["event"])
    assert [drive["file_id"], staged["file_id"], stub["file_id"]] == ["F_DRIVE", "F_PDF", "F_STUB"]
    assert [item["content_available"] for item in (drive, staged, stub)] == [False, True, False]
    assert turn["staged_files"] == [str(tmp_path / "staged" / "Ev-1" / "01-Roadmap")]
    assert staged["staged_as"] == "01-Roadmap" and "stage_error" not in staged
    assert Path(turn["staged_files"][0]).read_bytes() == b"%PDF1"
    assert stub == {"file_id": "F_STUB", "file_name": "F_STUB", "mime_type": "application/octet-stream",
                    "file_size": 0, "mode": "hidden_by_limit", "file_access": "check_file_info",
                    "is_hidden_by_limit": True, "content_available": False,
                    "stage_error": "missing_private_file_url", "stage_error_details": {}}
    assert provider.file_requests() == [("files.slack.com", "/files-pri/T1-F_PDF/download/Roadmap")]
    assert not any(PRIVATE_KEYS & set(item) for item in (drive, staged, stub))


def test_eleven_declarations_are_all_described_and_ten_are_fetched(tmp_path):
    files = [_slack_file(f"F{index:02d}", name=f"f{index}.pdf", size=1) for index in range(11)]
    provider = _Provider({f"/files-pri/T1-F{index:02d}/download/f{index}.pdf": _ok(b"x") for index in range(11)})
    host = _Host()
    bridge = _Bridge(tmp_path, provider, host)
    bridge.ingest(_envelope("Ev-1", ts="1.0", files=files))
    asyncio.run(bridge.run())

    attachments = _attachments(host.events()[0])
    assert [item["file_id"] for item in attachments] == [file["id"] for file in files]
    assert [item["content_available"] for item in attachments] == [True] * 10 + [False]
    assert attachments[10]["stage_error"] == "too_many_files"
    assert len(host.turns[0]["staged_files"]) == 10 and len(provider.file_requests()) == 10


def test_byte_budget_counts_accepted_bytes_and_a_smaller_sibling_still_stages(tmp_path):
    async def run():
        provider = _Provider({
            "/files-pri/T1-F_BIG/download/big.bin": _ok(b"0123456789AB"),
            "/files-pri/T1-F_SMALL/download/small.bin": _ok(b"0123"),
        })
        files = [{"file_id": "F_DECLARED", "name": "huge.bin", "size": 11,
                  "url_private": f"{FILE_HOST}F_DECLARED/download/huge.bin"},
                 {"file_id": "F_BIG", "name": "big.bin", "size": 0,
                  "url_private": f"{FILE_HOST}F_BIG/download/big.bin"},
                 {"file_id": "F_SMALL", "name": "small.bin", "size": 4,
                  "url_private": f"{FILE_HOST}F_SMALL/download/small.bin"}]
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            records = await slack.stage_inbound_files(files, destination=tmp_path / "staged", max_total_bytes=10)
        assert [record["stage_error"] for record in records[:2]] == ["file_batch_too_large"] * 2
        assert records[2]["path"] == str(tmp_path / "staged" / "02-small.bin") and records[2]["size"] == 4
        # The declared overflow was never requested; the streamed one left no partial file.
        assert [path for _host, path in provider.file_requests()] == [
            "/files-pri/T1-F_BIG/download/big.bin", "/files-pri/T1-F_SMALL/download/small.bin"]
        assert sorted(path.name for path in (tmp_path / "staged").iterdir()) == ["02-small.bin"]

    asyncio.run(run())


def test_declared_oversize_inbound_file_is_described_without_a_request(tmp_path):
    big = _slack_file("F_BIG", size=60 * 1024 * 1024)
    provider, host = _Provider(), _Host()
    bridge = _Bridge(tmp_path, provider, host)
    bridge.ingest(_envelope("Ev-1", ts="1.0", files=[big]))
    asyncio.run(bridge.run())
    assert _attachments(host.events()[0])[0]["stage_error"] == "file_batch_too_large"
    assert provider.file_requests() == []


@pytest.mark.parametrize("answer,code,details", [
    (httpx.Response(403), "file_http_403", {}),
    (httpx.Response(404), "file_http_404", {}),
    (httpx.Response(410), "file_http_410", {}),
    (httpx.Response(302, headers={"Location": "https://example.slack.com/?redir=login"}),
     "file_redirect_refused_302", {"redirect_host": "example.slack.com"}),
    (httpx.Response(302, headers={"Location": "https://[broken/login"}), "file_redirect_refused_302",
     {"redirect_host": ""}),
])
def test_completed_file_refusal_is_one_request_and_a_negative_fact(tmp_path, answer, code, details):
    pdf = _slack_file("F_PDF")
    provider = _Provider({"/files-pri/T1-F_PDF/download/brief.pdf": answer})
    host = _Host()
    bridge = _Bridge(tmp_path, provider, host)
    bridge.ingest(_envelope("Ev-1", ts="1.0", text="here", files=[pdf]),
                  _envelope("Ev-2", ts="2.0", thread_ts="1.0", text="follow-up"))
    asyncio.run(bridge.run(steps=2))

    [attachment] = _attachments(host.events()[0])
    assert attachment["stage_error"] == code and attachment["stage_error_details"] == details
    assert attachment["content_available"] is False
    assert [event["source_event_id"] for event in host.events()] == ["Ev-1", "Ev-2"]
    assert provider.file_requests() == [("files.slack.com", "/files-pri/T1-F_PDF/download/brief.pdf")]


def test_malformed_private_url_is_a_refusal_not_a_poison_retry(tmp_path):
    broken = {**_slack_file("F_BAD"), "url_private_download": "https://[files.slack.com/x", "url_private": ""}
    provider, host = _Provider(), _Host()
    bridge = _Bridge(tmp_path, provider, host)
    bridge.ingest(_envelope("Ev-1", ts="1.0", files=[broken]))
    asyncio.run(bridge.run())
    assert _attachments(host.events()[0])[0]["stage_error"] == "invalid_private_file_url"
    assert provider.file_requests() == []


def test_url_httpx_cannot_build_is_a_refusal_and_its_sibling_still_stages(tmp_path):
    broken = {**_slack_file("F_BAD"), "url_private_download": f"{FILE_HOST}F_BAD/download/a\x01b.pdf"}
    good = _slack_file("F_PDF")
    provider = _Provider({"/files-pri/T1-F_PDF/download/brief.pdf": _ok(b"%PDF1")})
    host = _Host()
    bridge = _Bridge(tmp_path, provider, host)
    bridge.ingest(_envelope("Ev-1", ts="1.0", files=[broken, good]))
    asyncio.run(bridge.run())

    bad, staged = _attachments(host.events()[0])
    assert bad["stage_error"] == "invalid_private_file_url" and bad["content_available"] is False
    assert staged["content_available"] is True
    assert provider.file_requests() == [("files.slack.com", "/files-pri/T1-F_PDF/download/brief.pdf")]
    assert bridge.rows()["Ev-1"]["state"] == "delivered"


def test_unnamed_files_stage_under_a_bounded_safe_name(tmp_path):
    long_id = "F" + "L" * 300
    dotted, unnamed = _slack_file(long_id, name="...."), _slack_file("....", name="")
    unnamed["url_private_download"] = f"{FILE_HOST}dots/download/file"
    provider = _Provider({f"/files-pri/T1-{long_id}/download/....": _ok(b"long"),
                          "/files-pri/T1-dots/download/file": _ok(b"dots")})
    host = _Host()
    bridge = _Bridge(tmp_path, provider, host)
    bridge.ingest(_envelope("Ev-1", ts="1.0", files=[dotted, unnamed]))
    asyncio.run(bridge.run())

    # Neither the raw ID nor the dots become a path component: the ID is
    # bounded like a name and an ID with nothing left falls back to the ordinal.
    staged = [Path(path) for path in host.turns[0]["staged_files"]]
    assert [path.name for path in staged] == ["00-" + long_id[:180], "01-file-1"]
    assert [path.read_bytes() for path in staged] == [b"long", b"dots"]
    assert all(item["content_available"] for item in _attachments(host.events()[0]))


def _transient(kind: str):
    def answer(request: httpx.Request) -> httpx.Response:
        if kind == "network":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(int(kind))
    return answer


@pytest.mark.parametrize("kind", ["408", "429", "503", "network", "disk"])
def test_retryable_staging_holds_only_its_thread_and_a_later_200_stages_bytes(tmp_path, kind):
    first, second = _slack_file("F_A", name="a.pdf"), _slack_file("F_B", name="b.pdf")
    failing = _ok(b"B-bytes") if kind == "disk" else [_transient(kind), _ok(b"B-bytes")]
    provider = _Provider({"/files-pri/T1-F_A/download/a.pdf": _ok(b"A-bytes"),
                          "/files-pri/T1-F_B/download/b.pdf": failing})
    host = _Host()
    bridge = _Bridge(tmp_path, provider, host)
    if kind == "disk":
        (tmp_path / "staged").write_text("not a directory")
    bridge.ingest(
        _envelope("Ev-1", ts="1.0", text="two files", files=[second] if kind == "disk" else [first, second]),
        _envelope("Ev-2", ts="2.0", thread_ts="1.0", text="same thread"),
        _envelope("Ev-3", ts="3.0", channel="D9", channel_type="im", text="unrelated DM"),
    )
    asyncio.run(bridge.run(steps=3))

    # The retry keeps its real backoff: the follower waits, the unrelated DM is answered.
    rows = bridge.rows()
    assert [event["source_event_id"] for event in host.events()] == ["Ev-3"]
    assert rows["Ev-1"]["state"] == "pending" and rows["Ev-1"]["staged_files_json"] == "[]"
    assert rows["Ev-1"]["available_at"] > rows["Ev-1"]["updated_at"]
    assert rows["Ev-2"]["state"] == "pending" and rows["Ev-3"]["state"] == "delivered"

    if kind == "disk":
        (tmp_path / "staged").unlink()
    bridge.release("Ev-1")
    asyncio.run(bridge.run(steps=2))
    assert [event["source_event_id"] for event in host.events()] == ["Ev-3", "Ev-1", "Ev-2"]
    staged = host.turns[1]["staged_files"]
    assert [Path(path).read_bytes() for path in staged] == (
        [b"B-bytes"] if kind == "disk" else [b"A-bytes", b"B-bytes"])
    assert all(item["content_available"] for item in _attachments(host.turns[1]["event"]))


def test_full_negative_checkpoint_survives_reopen_and_a_lost_host_reply(tmp_path):
    pdf = _slack_file("F_PDF")
    provider = _Provider({"/files-pri/T1-F_PDF/download/brief.pdf": [httpx.Response(404)]})
    host = _Host()
    host.lose = 1
    bridge = _Bridge(tmp_path, provider, host)
    bridge.ingest(_envelope("Ev-1", ts="1.0", files=[DRIVE, pdf, STUB]))
    asyncio.run(bridge.run())

    checkpoint = json.loads(bridge.rows()["Ev-1"]["staged_files_json"])
    assert [record["stage_error"] for record in checkpoint] == [
        "private_file_host_not_allowed", "file_http_404", "missing_private_file_url"]
    assert all(record["path"] == "" for record in checkpoint)
    assert bridge.rows()["Ev-1"]["state"] == "pending" and bridge.rows()["Ev-1"]["host_reference"] == ""

    reopened = _Bridge(tmp_path, provider, host)
    reopened.release("Ev-1")
    asyncio.run(reopened.run())
    assert len(provider.file_requests()) == 1
    assert host.turns[0] == host.turns[1]
    assert reopened.rows()["Ev-1"]["state"] == "delivered"


def test_host_reference_without_staging_neither_stages_nor_resubmits(tmp_path):
    provider, host = _Provider(), _Host()
    bridge = _Bridge(tmp_path, provider, host)
    bridge.ingest(_envelope("Ev-1", ts="1.0", files=[_slack_file("F_PDF")]))
    item = bridge.store.claim_inbox()
    bridge.store.set_host_reference(item.row_id, item.lease_token, _completed_reference({"outcome": "silent"}))
    bridge.store.retry_inbox(item.row_id, item.lease_token, "lost", delay_seconds=0)
    asyncio.run(bridge.run())
    assert provider.file_requests() == [] and host.turns == []
    assert bridge.rows()["Ev-1"]["state"] == "delivered"


def test_legacy_rows_drain_with_the_facts_they_kept(tmp_path):
    provider = _Provider({"/files-pri/T1-F_OLD/download/brief.pdf": _ok(b"never")})
    host = _Host()
    bridge = _Bridge(tmp_path, provider, host)
    legacy_path = tmp_path / "staged" / "Ev-1" / "00-brief.pdf"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(b"%PDF-old")
    bridge.ingest(_envelope("Ev-1", ts="1.0", files=[_slack_file("F_OLD")]),
                  _envelope("Ev-2", ts="2.0", files=[DRIVE]),
                  _envelope("Ev-3", ts="3.0", files=[{"id": "F_ID_ONLY"}]))
    # Pre-upgrade rows: five-key declarations, a path-only checkpoint, and a file-only
    # ID stub that the old parser stored as ignored.
    def declaration(file_id: str, url: str) -> str:
        return json.dumps([{"file_id": file_id, "name": "brief.pdf", "mimetype": "application/pdf",
                            "size": 5, "url_private": url}])

    with sqlite3.connect(bridge.store.path) as db:
        db.execute("UPDATE inbox SET files_json=?, staged_files_json=? WHERE event_id='Ev-1'", (
            declaration("F_OLD", f"{FILE_HOST}F_OLD/download/brief.pdf"),
            json.dumps([{"file_id": "F_OLD", "name": "brief.pdf", "mimetype": "application/pdf",
                         "size": 8, "path": str(legacy_path)}])))
        db.execute("UPDATE inbox SET files_json=? WHERE event_id='Ev-2'", (declaration("F_DRIVE", DRIVE["url_private"]),))
        db.execute("UPDATE inbox SET state='ignored', ignored_reason='empty_message', files_json='[]' "
                   "WHERE event_id='Ev-3'")
    asyncio.run(bridge.run(steps=3))

    old, drive = host.turns
    assert old["staged_files"] == [str(legacy_path)] and _attachments(old["event"])[0]["content_available"]
    # The old parser kept no external locator; the drained row says what it can.
    assert _attachments(drive["event"]) == [{
        "file_id": "F_DRIVE", "file_name": "brief.pdf", "mime_type": "application/pdf", "file_size": 5,
        "content_available": False, "stage_error": "private_file_host_not_allowed",
        "stage_error_details": {"host": "docs.google.com"}}]
    assert provider.file_requests() == []
    assert bridge.rows()["Ev-3"]["state"] == "ignored"


def test_file_only_id_stub_and_malformed_file_fields_are_accepted_without_errors():
    parsed = parse_socket_envelope(_envelope("Ev-1", ts="1.0", files=[
        {"id": "F1", "name": {"nested": True}, "size": float("inf"), "is_external": "yes",
         "external_url": ["https://example.org"], "url_private": None},
        {"name": "no id"}, "not a file",
    ]))
    assert parsed.accepted and parsed.event is not None
    [file] = parsed.event.files
    assert file.as_dict() == {"file_id": "F1", "name": "F1", "mimetype": "application/octet-stream",
                              "size": 0, "url_private": ""}


def test_edit_and_delete_project_known_file_leaves_and_keep_raw_storage(tmp_path):
    pdf = _slack_file("F_PDF")
    previous = {"type": "message", "user": "U1", "ts": "1.0", "text": "old", "files": [pdf, DRIVE]}
    current = {**previous, "text": "new", "files": [pdf]}
    edited = {"type": "events_api", "envelope_id": "env-edit", "payload": {"event_id": "Ev-edit", "team_id": "T1",
              "event": {"type": "message", "subtype": "message_changed", "channel": "C1", "ts": "1.5",
                        "message": current, "previous_message": previous}}}
    deleted = {"type": "events_api", "envelope_id": "env-del", "payload": {"event_id": "Ev-del", "team_id": "T1",
               "event": {"type": "message", "subtype": "message_deleted", "channel": "C2", "ts": "1.6",
                         "deleted_ts": "1.0", "previous_message": previous}}}
    provider = _Provider({"/files-pri/T1-F_PDF/download/brief.pdf": _ok(b"%PDF")})
    host = _Host()
    bridge = _Bridge(tmp_path, provider, host)
    bridge.ingest(edited, deleted)
    asyncio.run(bridge.run(steps=2))

    edit, delete = host.events()
    facts = edit["message"]["provider_facts"]
    assert facts["change"] == "edited" and facts["message"]["text"] == "new"
    assert [file["id"] for file in facts["previous_message"]["files"]] == ["F_PDF", "F_DRIVE"]
    assert facts["previous_message"]["files"][1]["external_url"] == DRIVE["external_url"]
    leaves = [*facts["message"]["files"], *facts["previous_message"]["files"],
              *delete["message"]["provider_facts"]["previous_message"]["files"]]
    assert leaves and not any(PRIVATE_KEYS & set(file) for file in leaves)
    # Availability belongs to the current declaration, never to historical files.
    assert not any("content_available" in file for file in leaves)
    assert _attachments(edit)[0]["content_available"] is True and _attachments(delete) == []
    for text in (json.dumps(edit), json.dumps(delete)):
        assert "files-pri" not in text and "slack-files.com" not in text
    assert "slack-files.com" in bridge.rows()["Ev-edit"]["raw_json"]
    assert "slack-files.com" in bridge.rows()["Ev-edit"]["structured_json"]


def test_strict_staging_stops_at_the_first_refusal(tmp_path):
    async def run(second_answer):
        provider = _Provider({"/files-pri/T1-F_B/download/b.pdf": second_answer})
        files = [{"file_id": "F_A", "name": "a.pdf", "url_private": DRIVE["url_private"]},
                 {"file_id": "F_B", "name": "b.pdf", "url_private": f"{FILE_HOST}F_B/download/b.pdf"}]
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            with pytest.raises(SlackApiError) as refusal:
                await slack.stage_private_files(files, destination=tmp_path / "strict")
        assert refusal.value.error == "private_file_host_not_allowed"
        assert refusal.value.details == {"host": "docs.google.com"}
        assert provider.file_requests() == []
        assert not (tmp_path / "strict").exists()

    asyncio.run(run(_ok(b"B")))
    asyncio.run(run(httpx.Response(503)))


@pytest.mark.parametrize("name", [
    ("Квартальный отчёт " * 20) + ("x" * 300) + ".pdf",
    # The 180-character cut lands on a space or a dot; Host trims staged paths.
    "a" * 179 + " tail.pdf",
    "a" * 179 + ".pdf",
], ids=["long-unicode", "cut-at-space", "cut-at-dot"])
def test_long_provider_names_stage_within_one_path_component(tmp_path, name):
    async def run():
        provider = _Provider({"/files-pri/T1-F_LONG/download/long.pdf": _ok(b"long")})
        file = {"file_id": "F_LONG", "name": name, "size": 4, "url_private": f"{FILE_HOST}F_LONG/download/long.pdf"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            [record] = await slack.stage_inbound_files([file], destination=tmp_path / "in")
            [strict] = await slack.stage_private_files([file], destination=tmp_path / "strict")
        for path in (Path(record["path"]), Path(strict.path)):
            assert path.read_bytes() == b"long" and path.name.startswith("00-")
            assert len(path.name) <= 183 and len(path.name + ".part.4194304") < 255
            assert path.name.isascii() and path.name == path.name.rstrip(" .")

    asyncio.run(run())


def test_host_receives_each_staged_path_exactly_as_written(tmp_path):
    edge = {**_slack_file("F_EDGE", name="a" * 179 + " tail.pdf"),
            "url_private_download": f"{FILE_HOST}F_EDGE/download/edge.pdf"}
    provider = _Provider({"/files-pri/T1-F_EDGE/download/edge.pdf": _ok(b"edge")})
    host = _Host()
    bridge = _Bridge(tmp_path, provider, host)
    legacy_path = tmp_path / "staged" / "Ev-2" / "00-brief.pdf "
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(b"%PDF-old")
    bridge.ingest(_envelope("Ev-1", ts="1.0", files=[edge]),
                  _envelope("Ev-2", ts="2.0", files=[_slack_file("F_OLD"), _slack_file("F_BLANK")]))
    # A pre-upgrade checkpoint keeps the path it actually wrote, trailing space
    # included; whitespace alone is still no staged path.
    with sqlite3.connect(bridge.store.path) as db:
        db.execute("UPDATE inbox SET staged_files_json=? WHERE event_id='Ev-2'", (json.dumps([
            {"file_id": "F_OLD", "name": "brief.pdf ", "mimetype": "application/pdf", "size": 8,
             "path": str(legacy_path)},
            {"file_id": "F_BLANK", "name": "brief.pdf", "mimetype": "application/pdf", "size": 0, "path": "  "},
        ]),))
    asyncio.run(bridge.run(steps=2))

    new, old = host.turns
    [path] = new["staged_files"]
    assert Path(path).name == "00-" + "a" * 179 and Path(path).read_bytes() == b"edge"
    assert _attachments(new["event"])[0]["staged_as"] == Path(path).name
    assert old["staged_files"] == [str(legacy_path)]
    assert [(item.get("staged_as"), item["content_available"]) for item in _attachments(old["event"])] == [
        ("00-brief.pdf ", True), (None, False)]
    assert provider.file_requests() == [("files.slack.com", "/files-pri/T1-F_EDGE/download/edge.pdf")]
