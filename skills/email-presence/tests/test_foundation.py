import asyncio
import importlib.util
import json
from email.message import EmailMessage
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest
import yaml
from starlette.testclient import TestClient

from lib.host_adapter import LoopbackPresenceHostAdapter, email_presence_event
from lib.mime import build_message, parse_message
from lib.runtime import EmailRuntime
from lib.store import EmailStore
from test_plugin import _settings_app
from test_transport import rig as _rig

rig = _rig
ACCOUNT = "owner@example.org"


def captured(tmp_path, *, to=ACCOUNT, cc="", sender="alice@example.org", headers=(), subject="Hello", body="A note"):
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, to, subject
    message["Message-ID"] = "<mail@example.org>"
    if cc:
        message["Cc"] = cc
    for name, value in headers:
        message[name] = value
    message.set_content(body)
    parsed = parse_message(message.as_bytes(), folder="INBOX", uid=1, include_attachment_data=True)
    store = EmailStore(tmp_path)
    store.ingest({**parsed, "uidvalidity": 7})
    # Exercise the persisted header/context path after reopening the store.
    return EmailStore(tmp_path).claim_inbox()


@pytest.mark.parametrize("to,cc,headers,direct", [
    (ACCOUNT, "", (), True),
    ("someone@example.org", "OWNER@EXAMPLE.ORG", (), True),
    ("list@example.org", "", (), False),
    (ACCOUNT, "", (("List-Id", "<list.example.org>"),), False),
    (ACCOUNT, "", (("List-Unsubscribe", ""),), False),
    *[(ACCOUNT, "", (("Precedence", value),), False) for value in ("bulk", "list", "junk")],
    (ACCOUNT, "", (("Precedence", "normal"),), True),
])
def test_direct_mail_fact_from_persisted_headers(tmp_path, to, cc, headers, direct):
    item = captured(tmp_path, to=to, cc=cc, headers=headers)
    facts = email_presence_event(item, account_id=ACCOUNT, places=True)["facts"]
    assert "any_message" in facts
    assert ("direct_message" in facts) is direct


@pytest.mark.parametrize("sender,headers,bot", [
    ("alice@example.org", (), False),
    ("alice@example.org", (("Auto-Submitted", "No"),), False),
    ("alice@example.org", (("Auto-Submitted", "auto-replied"),), True),
    ("alice@example.org", (("Auto-Submitted", ""),), True),
    *[("alice@example.org", (("Precedence", value),), True) for value in ("BULK", "list", "junk")],
    ("alice@example.org", (("Precedence", "normal"),), False),
    *[(sender + "@example.org", (), True) for sender in ("noreply", "No-Reply", "no_reply+tag", "do.not.reply")],
    ("noreplying@example.org", (), False),
    ("alice@noreply.example.org", (), False),
])
def test_bot_fact_is_observation_not_sender_filter(tmp_path, sender, headers, bot):
    item = captured(tmp_path, sender=sender, headers=headers)
    event = email_presence_event(item, account_id=ACCOUNT, places=True, identity=True)
    assert "any_message" in event["facts"]
    assert ("from_bot" in event["facts"]) is bot
    assert event["actor"]["authenticated"] is False


@pytest.mark.parametrize("words,subject,body,matches", [
    (["invoice"], "INVOICE ready", "hello", True),
    (["invoice"], "Hello", "(Invoice).", True),
    (["invoice"], "invoices", "preinvoice invoice_2", False),
    (["a+b"], "literal a+b", "", True),
    (["a+b"], "aaab", "", False),
    (["счёт"], "СЧЁТ", "", True),
    ([], "Invoice", "invoice", False),
    (["invoice"], "Nothing", "x" * 100_100 + " invoice", True),
])
def test_words_are_literal_whole_words_in_full_subject_or_body(tmp_path, words, subject, body, matches):
    item = captured(tmp_path, subject=subject, body=body)
    facts = email_presence_event(item, account_id=ACCOUNT, places=True, words=words)["facts"]
    assert ("signal.words" in facts) is matches


@pytest.mark.parametrize("capabilities", [
    {}, {"places_version": 1},
    {"places_version": 1, "presence_identity": 0, "presence_wake_signals": 0},
    {"places_version": 1, "presence_identity": 1, "presence_wake_signals": 1},
])
def test_identity_authentication_and_words_are_negotiated_on_wire_and_status(tmp_path, monkeypatch, capabilities):
    monkeypatch.setenv("EMAIL_USER", ACCOUNT)
    item = captured(tmp_path, subject="Invoice")
    sent = []
    def handle(request):
        if request.url.path == "/identity":
            return httpx.Response(200, json=capabilities)
        payload = json.loads(request.content)
        sent.append(payload["event"])
        return httpx.Response(200, json={"status": "completed", "outcome": "silent"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            host = LoopbackPresenceHostAdapter("a" * 32, host_service_url="http://127.0.0.1:8767",
                                               skill_token="fixture", http_client=http, words=["invoice"])
            runtime = EmailRuntime(EmailStore(tmp_path), object(), host)
            await runtime.discover_support()
            await host.submit(item)
    asyncio.run(run())
    with TestClient(_settings_app(tmp_path, {"EMAIL_USER": ACCOUNT})) as client:
        status = client.get("/status").json()
    expected = {"sends_as": {"id": ACCOUNT, "label": ACCOUNT, "kind": "user"},
                "reads_as": [{"id": ACCOUNT, "label": ACCOUNT, "kind": "user"}]}
    if capabilities.get("presence_identity") == 1:
        assert status["identity"] == sent[0]["identity"] == expected
        assert sent[0]["actor"]["authenticated"] is False
    else:
        assert "identity" not in sent[0]
        assert status["identity"] == expected  # Account facts remain visible before wire capability negotiation.
        assert "authenticated" not in sent[0]["actor"]
    assert ("signal.words" in sent[0].get("facts", [])) is (capabilities.get("presence_wake_signals") == 1)
    assert ("facts" in sent[0]) is bool(capabilities.get("places_version"))
    assert status["connect"] == [{"account_id": ACCOUNT, "conversation_id": "", "label": ACCOUNT}]
    assert status["labels"] == {"direct_message": "Mail addressed to you"}


def registered(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("email_foundation", root / "plugin.py", submodule_search_locations=[str(root)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    calls = []
    class Client:
        settings = {"EMAIL_USER": ACCOUNT}
        def __init__(self, _settings):
            pass
        def close(self):
            pass
        def __getattr__(self, name):
            def call(**kwargs):
                calls.append(name)
                return {"ok": True, "operation": name, "attachments": []}
            return call
    monkeypatch.setattr(plugin, "MailClient", Client)
    tools = {}
    api = SimpleNamespace(get_state_dir=lambda: str(tmp_path), get_settings=lambda keys: {"EMAIL_USER": ACCOUNT},
                          register_tool=lambda name, handler, **kw: tools.update({name: handler}))
    for name in ("register_route", "register_companion_process", "register_settings_section", "register_ui_tab"):
        setattr(api, name, lambda *args, **kwargs: None)
    plugin.register(api)
    return tools, calls


def test_sending_setting_defaults_roundtrips_and_leaves_other_tools_working(tmp_path, monkeypatch):
    tools, calls = registered(tmp_path, monkeypatch)
    with TestClient(_settings_app(tmp_path, {"EMAIL_USER": ACCOUNT})) as client:
        assert client.get("/settings/save").json()["sending"] == "on"
        assert json.loads(tools["email_send"](to=ACCOUNT, body="one", request_id="one"))["ok"]
        assert client.post("/settings/save", json={"sending": "off", "words": ["invoice", "счёт"]}).status_code == 200
        assert json.loads((tmp_path / "settings.json").read_text())["words"] == ["invoice", "счёт"]
        assert client.get("/settings/save").json()["words"] == "invoice\nсчёт"
        assert client.get("/status").json()["readiness"]["sending"] == "off"
        refusal = json.loads(tools["email_send"](to=ACCOUNT, body="two", request_id="two"))
        assert refusal == {"ok": False, "account_id": ACCOUNT, "code": "sending_off", "error":
                           "Sending is off for this mailbox in its connector settings; reading, drafts and tidying keep working"}
        assert EmailStore(tmp_path).receipt("two") is None
        for name, kwargs in (("email_search", {}), ("email_read", {"uid": 1, "uidvalidity": 7}),
                             ("email_mark_read", {"uid": 1, "uidvalidity": 7}),
                             ("email_draft", {"to": ACCOUNT, "subject": "draft", "body": "text"}),
                             ("email_mailbox", {"action": "list"})):
            assert json.loads(tools[name](**kwargs))["ok"]
        assert calls == ["search", "read", "mark_read", "draft", "mailbox"]
        for bad in ({"sending": "maybe"}, {"sending": False}, {"words": [4]}, {"words": {"invoice": True}}):
            assert client.post("/settings/save", json=bad).status_code == 400
        assert client.post("/settings/save", json={"sending": "on", "words": "a+b\n\n invoice "}).status_code == 200
        assert json.loads((tmp_path / "settings.json").read_text())["words"] == ["a+b", "invoice"]
        assert client.get("/status").json()["readiness"]["sending"] == "on"
        assert json.loads(tools["email_send"](to=ACCOUNT, body="two", request_id="two"))["ok"]


def test_sending_off_holds_automatic_and_queued_mail_then_resumes(rig):
    clock, _, client, store, _, runtime = rig
    path = store.state_dir / "settings.json"
    store.enqueue_outbox(request_id="queued", to=[ACCOUNT], body="Queued")
    item = captured(store.state_dir / "incoming")
    runtime._reply(item, ["Automatic"], "final")
    path.write_text(json.dumps({"sending": "off"}))
    assert runtime.process_outbox() and runtime.process_outbox()
    assert not client.sent
    assert store.receipt("queued")["state"] == "held"
    assert store.receipt("queued")["attempts"] == 0
    path.write_text(json.dumps({"sending": "on"}))
    clock[0] += 6
    assert runtime.process_outbox() and runtime.process_outbox()
    assert len(client.sent) == 2


@pytest.mark.parametrize("change", [
    {"to": "elsewhere@example.org"}, {"cc": ["copy@example.org"]}, {"bcc": ["blind@example.org"]},
    {"body": "Changed"}, {"subject": "Changed"}, {"reply_to_message_id": "<other>"},
    {"references": ["<other>"]}, {"html_body": "<p>changed</p>"}, {"body_type": "html"},
    {"attachments": [{"filename": "note.txt", "data": "changed"}]},
])
def test_request_id_reuse_checks_payload_and_returns_original_receipt(tmp_path, monkeypatch, change):
    tools, _ = registered(tmp_path, monkeypatch)
    original = {"to": ACCOUNT, "body": "Hello", "subject": "Subject", "request_id": "stable",
                "attachments": [{"filename": "note.txt", "data": "original"}]}
    first = json.loads(tools["email_send"](**original))
    same = json.loads(tools["email_send"](**original))
    assert same["deduplicated"] and same["receipt"] == first["receipt"]
    different = json.loads(tools["email_send"](**{**original, **change}))
    assert different["ok"] is False and different["code"] == "request_id_conflict"
    assert EmailStore(tmp_path).receipt("stable") == first["receipt"]
    assert sum(EmailStore(tmp_path).status()["outbox"].values()) == 1
    assert json.loads(tools["email_send"](**{**original, **change, "request_id": "new"}))["ok"]


@pytest.mark.parametrize("body", ["An unsigned answer", "Thanks,\nAnton", "I am an assistant."])
def test_outgoing_body_is_authored_without_connector_marks(body):
    message = build_message(sender=ACCOUNT, recipients=["alice@example.org"], subject="Re: Hello", body=body,
                            html_body="<p>" + body + "</p>")
    assert message.get_body(preferencelist=("plain",)).get_content() == body + "\n"
    assert message.get_body(preferencelist=("html",)).get_content() == "<p>" + body + "</p>\n"
    assert message["Auto-Submitted"] is None
    assert set(message.keys()) == {"From", "To", "Subject", "Message-ID", "MIME-Version", "Content-Type"}


def test_manifest_offers_facts_without_default_wakes():
    manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / "SKILL.md").read_text().split("---", 2)[1])
    assert manifest["version"] == "0.9.0"
    place = manifest["contributes"][0]
    assert place["wakes"] == []
    assert place["settings"] == "email_presence"
    assert place["facts"] == ["any_message", "direct_message", "from_bot",
                              {"id": "signal.words", "label": "Words you chose appear (connector setting)"}]
