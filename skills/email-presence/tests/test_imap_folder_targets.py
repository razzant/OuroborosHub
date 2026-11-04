"""Check each registered IMAP operation through the Host guard before dispatch and retry."""
from contextlib import contextmanager

import pytest

from lib.client import MailClient
from lib.service import MailService
from test_declared_target_preparation import published_targets
from test_write_targets import host_guard, tools_for


class RecordingBox:
    capabilities = (b"IMAP4rev1", b"MOVE")

    def __init__(self, delimiter=b'"/"'):
        self.delimiter = delimiter
        self.sessions = []
        self.list_calls = []
        self.writes = []

    @contextmanager
    def open(self, folder=None, *, readonly=True):
        self.sessions.append((folder, readonly))
        yield self

    def list(self, *args):
        self.list_calls.append(args)
        return "OK", [b'(\\Noselect) ' + self.delimiter + b' ""'] if args else [b'() "/" Legal']

    def response(self, key):
        return key, [b"7" if key == "UIDVALIDITY" else b"5"]

    def uid(self, *args):
        self.writes.append(args)
        return "OK", [b"done"]

    def create(self, name):
        self.writes.append(("CREATE", name))
        return "OK", [b"done"]


def guarded_calls(tmp_path, monkeypatch, tool, scopes, *, delimiter=b'"/"', box=None):
    box = box or RecordingBox(delimiter)
    client = MailClient({})
    client.imap = box.open
    account, call = tools_for(tmp_path, "imap", client)
    bindings = [("", "active"), *scopes] if scopes is not None else []
    guard = host_guard(tmp_path, monkeypatch, tool, account, bindings)

    def run(args):
        args = {"account_id": account, **args}
        for _ in range(2):
            if refusal := guard(args):
                return args, refusal
            result = call(tool, args)
            if result.get("status") != "needs_arguments":
                assert not result.get("error") and result.get("ok", True)
                return args, None
            assert not box.writes
            args = result["retry_arguments"]
        pytest.fail("Canonical arguments should be complete on the first retry")

    return account, box, run


@pytest.mark.parametrize("reference", [False, True], ids=["legacy", "mail-ref"])
@pytest.mark.parametrize(("action", "restricted", "must_deny"), [
    ("copy", "Legal", False), ("copy", "Archive", True), ("copy", "", False),
    ("move", "Legal", True), ("move", "Archive", True), ("move", "", False),
    ("flags", "Legal", True), ("flags", "", False),
    ("mark_read", "Legal", True), ("mark_read", "", False),
])
def test_imap_only_changed_folders_go_to_the_host(tmp_path, monkeypatch, reference, action, restricted, must_deny):
    tool = "email_mark_read" if action == "mark_read" else "email_mailbox"
    account, box, run = guarded_calls(tmp_path, monkeypatch, tool,
        [(restricted, "silent" if restricted == "Archive" else "paused")] if restricted else [])
    native = {"uid": 4, "uidvalidity": 7, "folder": "Legal"}
    args = {"message_ref": MailService.wrap_ref({"id": account}, native), "folder": "Decoy"} if reference else native
    if action != "mark_read":
        args = {**args, "action": action}
    if action in {"copy", "move"}:
        args["destination"] = "Archive"
    if action == "flags":
        args.update(flags=[r"\Deleted"], remove_flags=True)
    used, refusal = run(args)
    assert bool(refusal) == must_deny
    visible = published_targets(tool, used)
    assert "folder" not in visible
    if must_deny:
        assert ("Silent" if restricted == "Archive" else "Paused") in refusal
        assert restricted in {v for values in visible.values() for v in values}
        assert box.writes == []
        return
    if reference:
        assert used["message_ref"] == args["message_ref"]
    assert visible.get("folder_names", []) == ([] if action == "copy" else ["Legal"])
    assert box.sessions == [("Legal", action == "copy")]
    expected = (action.upper(), "4", '"Archive"') if action in {"copy", "move"} else (
        "STORE", "4", "-FLAGS.SILENT", r"(\Deleted)") if action == "flags" else (
        "STORE", "4", "+FLAGS.SILENT", r"(\Seen)")
    assert box.writes == [expected]


@pytest.mark.parametrize(("delimiter", "child", "held", "denied"), [
    (b'"."', "Legal.2026", "Legal", True), (b'"."', "Legal.2026", "Legal.2026", True),
    (b'"."', "Legal.2026", "", False), (b"NIL", "Legal/2026", "Legal", False),
    (b'"/"', "inbox/Receipts", "INBOX", True), (b'"/"', "inbox/Receipts", "", False),
])
def test_create_declares_parent_only_in_the_reported_hierarchy(tmp_path, monkeypatch, delimiter, child, held, denied):
    _, box, run = guarded_calls(tmp_path, monkeypatch, "email_mailbox", [(held, "paused")] if held else [], delimiter=delimiter)
    used, refusal = run({"action": "create", "destination": child, "folder": "Irrelevant"})
    assert bool(refusal) == denied
    assert box.writes == ([] if denied else [("CREATE", MailClient.folder_name(child))])
    if held != child:
        assert box.list_calls and set(box.list_calls) == {(MailClient.folder_name(child), '""')}
    if delimiter == b"NIL":
        assert not used.get("folder_names")


class HierarchyBox(RecordingBox):
    def __init__(self, responses, delimiter=b'"/"'):
        super().__init__(delimiter)
        self.responses = responses

    def list(self, reference, pattern):
        if pattern == '""':
            return super().list(reference, pattern)
        assert reference == '""'
        self.list_calls.append((reference, pattern))
        return self.responses.get(pattern, ("OK", [None]))


@pytest.mark.parametrize(("scopes", "parent_exists", "denied"), [
    ([("Legal", "paused")], False, True),
    ([("Legal", "paused"), ("Legal/2026", "active")], True, False),
    ([("Legal/2026", "paused")], True, True),
    ([("Legal/2026", "silent")], False, True),
    (None, False, False),
])
def test_create_checks_only_materialized_names_and_their_parent(tmp_path, monkeypatch, scopes, parent_exists, denied):
    # RFC 3501 6.3.3: one CREATE can add every missing intermediate name.
    box = HierarchyBox({'"Legal/2026"': ("OK", [b'() "/" Legal/2026'])} if parent_exists else {})
    _, _, run = guarded_calls(tmp_path, monkeypatch, "email_mailbox", scopes, box=box)
    used, refusal = run({"action": "create", "destination": "Legal/2026/Q4"})
    assert bool(refusal) == denied
    assert used["folder_names"] == (["Legal/2026"] if parent_exists else ["Legal", "Legal/2026"])
    assert box.writes == ([] if denied else [("CREATE", '"Legal/2026/Q4"')])


def test_create_stops_at_first_existing_ancestor_with_provider_delimiter(tmp_path, monkeypatch):
    box = HierarchyBox({'"Projects.2026"': ("OK", [b'() "." "Projects.2026"'])}, b'"."')
    _, _, run = guarded_calls(tmp_path, monkeypatch, "email_mailbox", [("Projects", "paused")], box=box)
    child = "Projects.2026.Q4.Week1."
    used, refusal = run({"action": "create", "destination": child})
    assert not refusal and used["folder_names"] == ["Projects.2026", "Projects.2026.Q4", child[:-1]]
    assert box.writes == [("CREATE", MailClient.folder_name(child))]


@pytest.mark.parametrize(("parent", "rows", "exists"), [
    ("Legal/20%", [b'() "/" "Legal/2026"'], False),  # LIST patterns can match another mailbox.
    ("Legal/20*", [b'() "/" Legal/2026', b'() "/" "Legal/20*"'], True),
    ("Legal/2026", [b'(\\Noselect) "/" Legal/2026'], True),  # Existing hierarchy node can have children.
    ("Legal/2026", [b'(\\Noselect \\NonExistent) "/" "Legal/2026"'], False),
    ('Legal/台北 & "Q"\\R', [(b'() "/" {23}', b'Legal/&U,BTFw- &- "Q"\\R'), b''], True),
    ('Legal/台北 & "Q"\\R', [b'() "/" "Legal/&U,BTFw- &- \\"Q\\"\\\\R"'], True),
])
def test_exact_parent_identity_in_list_replies(tmp_path, monkeypatch, parent, rows, exists):
    box = HierarchyBox({MailClient.folder_name(parent): ("OK", rows)})
    _, _, run = guarded_calls(tmp_path, monkeypatch, "email_mailbox", [("Legal", "paused")], box=box)
    child = parent + "/Q4"
    used, refusal = run({"action": "create", "destination": child})
    assert bool(refusal) != exists
    assert ("Legal" in used["folder_names"]) != exists
    assert box.writes == ([("CREATE", MailClient.folder_name(child))] if exists else [])


@pytest.mark.parametrize("status", ["NO", "BAD"])
def test_failed_parent_list_is_not_an_absent_mailbox(tmp_path, monkeypatch, status):
    box = HierarchyBox({'"Legal/2026"': (status, [b"cannot list this parent"])})
    _, _, run = guarded_calls(tmp_path, monkeypatch, "email_mailbox", None, box=box)
    with pytest.raises(RuntimeError, match=f"parent lookup failed \\({status}\\)"):
        run({"action": "create", "destination": "Legal/2026/Q4"})
    assert not box.writes


def test_inconsistent_parent_delimiter_cannot_hide_the_affected_parent(tmp_path, monkeypatch):
    box = HierarchyBox({'"Legal/2026"': ("OK", [b'() NIL "Legal/2026"'])})
    _, _, run = guarded_calls(tmp_path, monkeypatch, "email_mailbox", None, box=box)
    with pytest.raises(RuntimeError, match="inconsistent hierarchy delimiters"):
        run({"action": "create", "destination": "Legal/2026/Q4"})
    assert not box.writes


def test_retry_checks_again_if_parent_was_removed(tmp_path, monkeypatch):
    box = HierarchyBox({'"Legal/2026"': ("OK", [b'() "/" Legal/2026'])})
    client = MailClient({})
    client.imap = box.open
    account, call = tools_for(tmp_path, "imap", client)
    tool = "email_mailbox"
    guard = host_guard(tmp_path, monkeypatch, tool, account, [("", "active"), ("Legal", "paused")])
    args = {"account_id": account, "action": "create", "destination": "Legal/2026/Q4"}
    assert not guard(args)
    args = call(tool, args)["retry_arguments"]
    assert args["folder_names"] == ["Legal/2026"] and not box.writes
    box.responses.clear()
    assert not guard(args)
    args = call(tool, args)["retry_arguments"]
    assert args["folder_names"] == ["Legal", "Legal/2026"] and "Paused" in guard(args)
    assert not box.writes


@pytest.mark.parametrize(("source", "restricted", "deny"), [
    ("iNbOx", "INBOX", True), ("iNbOx", "", False), ("Projects", "PROJECTS", False)])
def test_flags_canonicalize_only_the_distinguished_inbox(tmp_path, monkeypatch, source, restricted, deny):
    _, box, run = guarded_calls(tmp_path, monkeypatch, "email_mark_read", [(restricted, "silent")] if restricted else [])
    used, refusal = run({"uid": 4, "uidvalidity": 7, "folder": source})
    assert bool(refusal) == deny
    assert used["folder_names"] == (["INBOX"] if source == "iNbOx" else [source])
    assert len(box.writes) == (0 if deny else 1)


def test_copy_checks_destination_inbox_spelling_and_validity(tmp_path, monkeypatch):
    _, box, run = guarded_calls(tmp_path, monkeypatch, "email_mailbox", [("INBOX", "paused")])
    used, refusal = run({"action": "copy", "folder": "Legal", "uid": 4, "uidvalidity": 7, "destination": "Inbox"})
    assert used["destination"] == "INBOX" and "Paused" in refusal and not box.writes
    with pytest.raises(ValueError, match="UIDVALIDITY"):
        run({"action": "copy", "folder": "Legal", "uid": 4, "uidvalidity": 6, "destination": "Archive"})
    assert box.sessions == [("Legal", True)] and not box.writes


@pytest.mark.parametrize("tool", ["email_folders", "email_mailbox"])
def test_folder_listing_keeps_provider_read_only(tmp_path, monkeypatch, tool):
    _, box, run = guarded_calls(tmp_path, monkeypatch, tool, [("Legal", "paused")])
    _, refusal = run({"action": "list"} if tool == "email_mailbox" else {})
    assert not refusal and box.sessions == [(None, True)] and box.list_calls == [()] and not box.writes
