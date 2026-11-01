"""Offline host-child integration fixture, never an installed entrypoint."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys


def main():
    repo, source_skill, temp_root = map(Path, sys.argv[1:])
    drive = temp_root / "drive"
    os.environ.update(OUROBOROS_DATA_DIR=str(drive), OUROBOROS_RUNTIME_MODE="advanced",
                      OUROBOROS_SETTINGS_PATH=str(drive / "settings.json"))
    sys.path.insert(0, str(repo))
    from ouroboros import extension_loader
    from ouroboros.extension_process_runner import dispatch_extension_tool_subprocess, ExtensionProcessError
    from ouroboros.skill_loader import (
        SkillReviewState, find_skill, save_enabled, save_review_state, save_skill_grants,
        requested_skill_permissions, skill_state_dir,
    )
    from ouroboros.tools.registry import ToolContext
    from supervisor import queue, state
    from tests._extension_loader_shared import _mark_isolated_deps_installed

    drive.mkdir(exist_ok=True)
    (drive / "settings.json").write_text(json.dumps({"EMAIL_USER": "mail@example.invalid"}), encoding="utf-8")
    state.init(drive)
    queue.init(drive)
    skills = temp_root / "skills"
    skill = skills / "email-presence"
    shutil.copytree(source_skill, skill, ignore=shutil.ignore_patterns("__pycache__", ".ouroboros_env"))
    shutil.move(skill / "plugin.py", skill / "original_plugin.py")
    # Replace only network-facing client methods. Registration, store, MIME source
    # staging, context injection, JSON return and subprocess transport remain real.
    (skill / "plugin.py").write_text('''
from . import original_plugin

class OfflineMailClient:
    def __init__(self, settings):
        self.settings = settings
    def search(self, **kwargs):
        return {"folder": kwargs["folder"], "uidvalidity": 7, "uids": [4],
                "nested": {"seen": False, "optional": None, "label": "Почта 📬"}}
    def read(self, **kwargs):
        if kwargs["uid"] == 0:
            raise ValueError("fixture UID unavailable")
        return {"folder": kwargs["folder"], "uidvalidity": kwargs["uidvalidity"],
                "uid": kwargs["uid"], "message_id": "<fixture@example.invalid>",
                "body": "Unicode 📬 and 'quoted' text", "attachments": [],
                "_raw_source": b"Subject: fixture\\r\\n\\r\\nbody"}
    def mark_read(self, **kwargs):
        return {"ok": True, "uid": kwargs["uid"], "uidvalidity": kwargs["uidvalidity"], "folder": kwargs["folder"]}
    def mailbox(self, **kwargs):
        return {"folders": [{"name": "INBOX", "selectable": True}, {"name": "Архив", "optional": None}]}
    def draft(self, **kwargs):
        return {"ok": True, "saved": kwargs["body"], "sent": False, "uid": None}
    def test_connection(self):
        return {"ok": True, "imap": True, "smtp": True, "error": None}

def register(api):
    original_plugin.MailClient = OfflineMailClient
    original_plugin.MailService.capture_binding = lambda *args, **kwargs: {"supported": False}
    original_plugin.register(api)
    async def asynchronous(ctx, *, fail=False):
        if fail:
            raise ValueError("fixture async failure")
        return {"task_id": ctx.task_id, "ok": True, "value": None}
    api.register_tool("fixture_async", original_plugin._json_tool(asynchronous),
                      description="Async text-boundary regression fixture.",
                      schema={"type": "object", "properties": {"fail": {"type": "boolean"}}})
''', encoding="utf-8")
    loaded = find_skill(drive, "email-presence", repo_path=str(skills))
    assert loaded is not None
    # These are throwaway fixtures for loading, not installation attestations.
    save_enabled(drive, loaded.name, True)
    save_review_state(drive, loaded.name, SkillReviewState(status="pass", content_hash=loaded.content_hash))
    permissions = requested_skill_permissions(list(loaded.manifest.permissions))
    keys = list(loaded.manifest.env_from_settings)
    save_skill_grants(drive, loaded.name, keys, content_hash=loaded.content_hash,
                      requested_keys=keys, granted_permissions=permissions, requested_permissions=permissions)
    _mark_isolated_deps_installed(drive, loaded)
    loaded = find_skill(drive, loaded.name, repo_path=str(skills))
    error = extension_loader.load_extension(loaded, lambda: {"EMAIL_USER": "mail@example.invalid"}, drive_root=drive)
    assert error is None, error
    try:
        ctx = ToolContext(repo_dir=repo, drive_root=drive, task_id="mail-fixture")
        ctx.task_metadata = {"presence": {"event": {"source_event_id": "fixture-event"}}}

        def invoke(name, **args):
            tool = extension_loader.get_tool(extension_loader.extension_surface_name(loaded.name, name))
            assert tool["out_of_process"]
            raw = dispatch_extension_tool_subprocess(tool, ctx, args)
            assert isinstance(raw, str), (name, type(raw))
            return json.loads(raw)

        assert invoke("email_receipt", request_id="missing") is None
        accounts = invoke("email_accounts")["accounts"]
        assert [account["id"] for account in accounts] == ["mail@example.invalid"]
        assert invoke("email_folders", account_id="mail@example.invalid") == {
            "folders": [{"name": "INBOX", "selectable": True}, {"name": "Архив", "optional": None}],
            "account_id": "mail@example.invalid"}
        assert invoke("email_settings_read", account_id="mail@example.invalid")["code"] == "website_required"
        assert invoke("email_settings", account_id="mail@example.invalid", resource="vacation")["code"] == "website_required"
        sys.path.insert(0, str(skill))
        from lib.store import EmailStore
        EmailStore(skill_state_dir(drive, loaded.name))
        db_path = skill_state_dir(drive, loaded.name) / "email_presence.sqlite3"
        cursor = json.dumps({"uidvalidity": 7, "uid": 99, "activated_at": 1})
        with sqlite3.connect(db_path) as connection:
            connection.execute("INSERT INTO runtime_state VALUES (?, ?, ?)",
                               ('cursor:["mail@example.invalid", "INBOX"]', cursor, 1))
        search = invoke("email_search", criteria=["ALL"])
        assert search == {"folder": "INBOX", "uidvalidity": 7, "uids": [4],
                          "nested": {"seen": False, "optional": None, "label": "Почта 📬"}, "account_id": "mail@example.invalid"}
        message = invoke("email_read", uid=4, uidvalidity=7)
        assert message["body"] == "Unicode 📬 and 'quoted' text"
        assert "_raw_source" not in message
        assert Path(message["source_artifact"]["path"]).read_bytes() == b"Subject: fixture\r\n\r\nbody"
        assert invoke("email_mark_read", uid=4, uidvalidity=7) == {"ok": True, "uid": 4, "uidvalidity": 7, "folder": "INBOX", "account_id": "mail@example.invalid"}
        assert invoke("email_mailbox", action="list") == {"folders": [
            {"name": "INBOX", "selectable": True}, {"name": "Архив", "optional": None}], "account_id": "mail@example.invalid"}
        assert invoke("email_draft", to="reader@example.invalid", subject="Draft", body="Черновик") == {
            "ok": True, "saved": "Черновик", "sent": False, "uid": None, "account_id": "mail@example.invalid", "sender": "mail@example.invalid"}
        assert invoke("email_test_connection") == {"ok": True, "imap": True, "smtp": True, "error": None, "account_id": "mail@example.invalid"}
        assert invoke("email_receipt", request_id="missing") is None
        sent = invoke("email_send", to="reader@example.invalid", body="Hello 📬", request_id="stable")
        again = invoke("email_send", to="reader@example.invalid", body="Hello 📬", request_id="stable")
        assert sent["ok"] and not sent["deduplicated"] and again["deduplicated"]
        receipt = invoke("email_receipt", request_id="stable")
        assert receipt["state"] == "pending"
        assert receipt["provider_message_id"] == sent["receipt"]["provider_message_id"]
        connection = sqlite3.connect(db_path)
        try:
            assert connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
            reporting = json.loads(connection.execute("SELECT reporting_json FROM outbox").fetchone()[0])
            assert reporting["origin"] == {"kind": "tool", "task_id": "mail-fixture", "source_event_id": "fixture-event"}
            assert connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
            assert connection.execute("SELECT value FROM runtime_state WHERE key LIKE 'cursor:%'").fetchall() == [(cursor,)]
        finally:
            connection.close()
        assert invoke("fixture_async") == {"task_id": "mail-fixture", "ok": True, "value": None}
        for name, arguments, expected in (
            ("email_read", {"uid": 0, "uidvalidity": 7}, "fixture UID unavailable"),
            ("fixture_async", {"fail": True}, "fixture async failure"),
        ):
            try:
                invoke(name, **arguments)
            except ExtensionProcessError as exc:
                assert expected in str(exc), str(exc)
            else:
                raise AssertionError("Handler error became a successful result")
        print("verified twelve email tools across the child boundary")
    finally:
        extension_loader.unload_extension(loaded.name)


if __name__ == "__main__":
    main()
