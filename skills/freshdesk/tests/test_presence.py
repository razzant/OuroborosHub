"""The companion's Presence protocol against a scripted host and the loopback Freshdesk."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_freshdesk import KEY, FakeFreshdesk  # noqa: E402
from freshdesk_lib.presence import Presence  # noqa: E402
from freshdesk_lib.sends import Sends  # noqa: E402
from freshdesk_lib.service import Service, write_settings  # noqa: E402
from freshdesk_lib.store import dump  # noqa: E402

HOST = "example.freshdesk.com"
CAPABLE = {"places_version": 1, "presence_explicit_completion": 1, "presence_identity": 1,
           "presence_delivery_version": 1, "presence_work_output_version": 1, "presence_continuation_version": 1,
           "presence_continuation_versions": [1, 2]}


class ScriptedHost:
    def __init__(self, identity=None, state="active"):
        self.identity_answer = dict(CAPABLE if identity is None else identity)
        self.state = state
        self.turns, self.reports, self.works = [], [], {}
        self.answers: dict = {}

    def identity(self):
        return self.identity_answer

    def place(self, account_id, conversation_id="", thread_id=""):
        return {"state": self.state, "label": "Freshdesk"}

    def turn(self, payload):
        self.turns.append(payload)
        answer = self.answers.get(payload["event"]["source_event_id"], (200, {"status": "completed", "outcome": "silent"}))
        return answer() if callable(answer) else answer

    def work(self, ref):
        return self.works.get(ref, (404, {}))

    def delivery(self, report):
        self.reports.append(report)
        return 200, {"ok": True, "recorded": True, "duplicate": False}


@pytest.fixture()
def fake():
    server = FakeFreshdesk()
    server.base = server.start()
    yield server
    server.stop()


@pytest.fixture()
def wired(fake, tmp_path):
    write_settings(tmp_path, {"domain": HOST})
    host = ScriptedHost()
    service = Service(tmp_path, lambda: KEY, base_url=fake.base, timeout=3.0, host=host)
    store = service.store()
    presence = Presence(store, host, sends=Sends(service), account=HOST)
    yield presence, host, service, store
    presence.close()
    store.close()


def queue(store, event_id, ticket_id, seq):
    event = {"source_event_id": event_id, "provider": "freshdesk", "account_id": HOST, "conversation_id": str(ticket_id),
             "thread_id": str(ticket_id), "conversation_key": f"freshdesk:{HOST}:{ticket_id}:{ticket_id}",
             "actor": {"platform": "freshdesk", "attribution": "unknown"}, "conversation": {}, "message": {},
             "text": "x", "facts": ["any_message"]}
    store.db.execute("INSERT INTO inbox(source_event_id, ticket_id, seq, event) VALUES(?,?,?,?)",
                     (event_id, str(ticket_id), seq, dump(event)))


def drain(presence):
    for _ in range(50):
        presence.settle()
        if not presence.inflight:
            return
        time.sleep(0.02)


def test_an_older_host_is_a_visible_gap(wired):
    presence, host, _, store = wired
    host.identity_answer = {**CAPABLE, "presence_continuation_versions": [1]}
    capabilities = presence.negotiate()
    assert capabilities["ready"] is False and capabilities["missing"] == ["presence_continuation_versions[2]"]
    assert store.gaps()[0]["code"] == "host_capability_gap"


def test_each_ticket_offers_its_oldest_event_first(wired):
    presence, host, _, store = wired
    queue(store, "a1", 1, 1)
    queue(store, "a2", 1, 2)
    queue(store, "b1", 2, 3)
    presence.dispatch()
    drain(presence)
    assert [t["event"]["source_event_id"] for t in host.turns] == ["a1", "b1"]
    assert {t["continuation_version"] for t in host.turns} == {2} and host.turns[0]["delivery_reporting_version"] == 1
    presence.dispatch()
    drain(presence)
    assert [t["event"]["source_event_id"] for t in host.turns] == ["a1", "b1", "a2"]


def test_a_retry_keeps_the_event_at_the_head_of_its_ticket(wired):
    presence, host, _, store = wired
    queue(store, "a1", 1, 1)
    queue(store, "a2", 1, 2)
    host.answers["a1"] = (429, {"ok": False, "code": "presence_capacity_full", "disposition": "retry", "retry_after": 1})
    presence.dispatch()
    drain(presence)
    presence.dispatch()
    drain(presence)
    assert [t["event"]["source_event_id"] for t in host.turns] == ["a1"]  # a2 waits behind its ticket's head
    row = store.one("SELECT state, due FROM inbox WHERE source_event_id='a1'")
    assert row["state"] == "ready" and row["due"] > time.time()


@pytest.mark.parametrize("answer, state", [
    ((200, {"ok": False, "disposition": "retained", "turn_ref": "t-1"}), "retained"),
    ((409, {"ok": False, "code": "presence_event_identity_conflict", "disposition": "rejected"}), "rejected"),
    ((403, {"ok": False, "code": "place_not_connected", "disposition": "blocked"}), "blocked"),
])
def test_dispositions_are_followed(wired, answer, state):
    presence, host, _, store = wired
    queue(store, "a1", 1, 1)
    host.answers["a1"] = answer
    presence.dispatch()
    drain(presence)
    assert store.one("SELECT state FROM inbox WHERE source_event_id='a1'")["state"] == state


def test_a_released_output_is_posted_once_as_a_private_note_and_reported(wired, fake):
    presence, host, service, store = wired
    ticket = fake.add_ticket(subject="Refund", description="please")
    queue(store, "e1", ticket["id"], 1)
    host.answers["e1"] = (200, {"status": "continuing", "outcome": "deferred", "text": "", "turn_ref": "w-1",
                                "continuation_ref": "w-1", "continuation_version": 2})
    presence.dispatch()
    drain(presence)
    host.works["w-1"] = (200, {"status": "completed", "outcome": "message", "text": "Refund issued today.",
                               "output_ref": "out-1", "outputs": [{"output_ref": "out-1", "outcome": "message",
                                                                   "text": "Refund issued today."}]})
    store.db.execute("UPDATE refs SET due=0")
    presence.poll_refs()
    presence.deliver_outputs()
    presence.deliver_outputs()
    entries = fake.conversations[ticket["id"]]
    assert [(e["body_text"], e["private"]) for e in entries] == [("Refund issued today.", True)]
    presence.report_receipts()
    [report] = host.reports
    assert report["origin"] == {"kind": "automatic", "source_event_id": "e1"}
    assert (report["conversation_id"], report["thread_id"], report["part_id"], report["state"]) == (
        str(ticket["id"]), str(ticket["id"]), "0", "delivered")
    assert report["message"] == {"message_id": str(entries[0]["id"])} and report["text"] == "Refund issued today."


def test_a_tool_reply_is_reported_with_its_task(wired, fake):
    presence, host, service, store = wired
    ticket = fake.add_ticket(subject="A", description="b")
    result = Sends(service).reply({"task_id": "task-7"}, account_id=HOST, ticket_id=ticket["id"], text="Hello",
                                  request_id="r-1")
    assert result["ok"]
    presence.report_receipts()
    [report] = host.reports
    assert report["delivery_id"] == "r-1" and report["state"] == "accepted"
    assert report["origin"] == {"kind": "tool", "task_id": "task-7"}


def test_a_send_with_no_place_reports_nothing(wired, fake):
    presence, host, service, store = wired
    host.state = "unbound"
    ticket = fake.add_ticket(subject="A", description="b")
    assert Sends(service).reply(None, account_id=HOST, ticket_id=ticket["id"], text="Hello", request_id="r-2")["ok"]
    presence.report_receipts()
    assert host.reports == [] and store.journal("r-2")["report_state"] == "none"


def test_a_silent_place_holds_a_released_output(wired, fake):
    presence, host, _, store = wired
    ticket = fake.add_ticket(subject="A", description="b")
    store.db.execute("INSERT INTO outputs(output_ref, ticket_id, text, source_event_id) VALUES('o-2', ?, 'Text', 'e2')",
                     (str(ticket["id"]),))
    host.state = "silent"
    presence.deliver_outputs()
    assert fake.conversations[ticket["id"]] == []
    assert store.one("SELECT state FROM outputs WHERE output_ref='o-2'")["state"] == "held"
    assert json.loads(store.one("SELECT detail FROM outputs WHERE output_ref='o-2'")["detail"])["code"] == "place_silent"
