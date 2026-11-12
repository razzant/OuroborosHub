"""The polling walk and the events it reports, against the loopback Freshdesk with invented data."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_freshdesk import KEY, ME, FakeFreshdesk  # noqa: E402
from freshdesk_lib.sends import Sends  # noqa: E402
from freshdesk_lib.service import Service, write_settings  # noqa: E402
from freshdesk_lib.watcher import Budget, baseline, process, walk  # noqa: E402

HOST = "example.freshdesk.com"
LEAD = 9002


@pytest.fixture()
def fake():
    server = FakeFreshdesk()
    server.base = server.start()
    yield server
    server.stop()


@pytest.fixture()
def desk(fake, tmp_path):
    write_settings(tmp_path, {"domain": HOST})
    service = Service(tmp_path, lambda: KEY, base_url=fake.base, timeout=3.0)
    return service


def plenty(store):
    budget = Budget(store, share=25, poll_seconds=60)
    budget.left = 10_000
    return budget


def cycle(service, capabilities=None):
    """One watcher cycle; returns the events it queued, in order."""
    client, store = service.client(), service.store()
    try:
        baseline(client, store)
        budget = plenty(store)
        assert walk(client, store, budget)["complete"]
        process(service, client, store, budget, capabilities=capabilities or {"presence_identity": 1})
        rows = store.rows("SELECT event FROM inbox WHERE state='ready' ORDER BY seq")
        store.db.execute("UPDATE inbox SET state='accepted'")
        return [json.loads(r["event"]) for r in rows]
    finally:
        store.close()


def facts(events):
    return [(e["source_event_id"].split(f"{HOST}:")[1], e["facts"]) for e in events]


def connect(fake, service):
    """Connect a second after the existing history (the cutoff is inclusive: a message written in the very second
    of the connection counts as new rather than being lost), then leave a second before new changes."""
    fake.settle()
    assert cycle(service) == []
    fake.settle()


def test_history_before_the_connection_is_never_an_event(fake, desk):
    old = fake.add_ticket(subject="Old question", description="from last week")
    fake.add_entry(old["id"], body="An old reply", user_id=ME, incoming=False)
    connect(fake, desk)
    assert cycle(desk) == []


def test_a_new_ticket_and_the_replies_on_it(fake, desk):
    connect(fake, desk)
    ticket = fake.add_ticket(subject="Charged twice", description="I see two charges.", requester_id=501)
    events = cycle(desk)
    assert facts(events) == [(f"{ticket['id']}:created", ["any_message", "new_issue"])]
    created = events[0]
    assert created["conversation_id"] == created["thread_id"] == str(ticket["id"])
    assert created["conversation_key"] == f"freshdesk:{HOST}:{ticket['id']}:{ticket['id']}"
    assert created["actor"]["authenticated"] is False and created["actor"]["display_name"] == "Customer One"
    assert created["text"] == "Charged twice\n\nI see two charges."
    assert created["identity"]["sends_as"] == {"id": str(ME), "label": "Desk Agent <desk.agent@example.net>", "kind": "user"}
    first = fake.add_entry(ticket["id"], body="Any news?", user_id=501, incoming=True, source=1)
    events = cycle(desk)
    assert facts(events) == [(f"{ticket['id']}:entry:{first['id']}", ["any_message"])]
    assert events[0]["message"]["sent_at"].endswith("+00:00") and events[0]["message"]["source_order"] == first["id"]


def test_own_sends_are_not_reported_and_make_the_ticket_mine(fake, desk):
    connect(fake, desk)
    ticket = fake.add_ticket(subject="Login", description="no email")
    cycle(desk)
    reply = Sends(desk).reply(account_id=HOST, ticket_id=ticket["id"], text="We resent it.", request_id="r-1")
    assert reply["ok"]
    assert cycle(desk) == []
    answer = fake.add_entry(ticket["id"], body="Got it, thanks", user_id=501, incoming=True, source=1)
    assert facts(cycle(desk)) == [(f"{ticket['id']}:entry:{answer['id']}", ["any_message", "my_thread"])]


def test_a_note_that_notifies_this_agent_is_a_mention(fake, desk):
    connect(fake, desk)
    ticket = fake.add_ticket(subject="Refund", description="please")
    cycle(desk)
    note = fake.add_entry(ticket["id"], body="Please check this one", user_id=LEAD, incoming=False, private=True,
                          source=2, to_emails=["desk.agent@example.net"])
    [event] = cycle(desk)
    assert event["facts"] == ["any_message", "mention"] and event["message"]["private"] is True
    assert event["actor"] == {"platform_actor_id": str(LEAD), "display_name": "Team Lead"}
    assert event["message"]["message_id"] == str(note["id"])


def test_assignment_wakes_and_other_changes_are_background(fake, desk):
    connect(fake, desk)
    ticket = fake.add_ticket(subject="Refund", description="please", status=2)
    cycle(desk)
    fake.tickets[ticket["id"]].update(status=3, updated_at=fake.iso_now())
    [change] = cycle(desk)
    assert change["facts"] == [] and change["text"] == ""
    assert change["message"]["non_text"][0]["description"] == "Status: Open → Pending"
    assert change["actor"] == {"platform": "freshdesk", "attribution": "unknown"}
    fake.tickets[ticket["id"]].update(responder_id=ME, group_id=11, updated_at=fake.iso_now())
    [assigned] = cycle(desk)
    assert assigned["facts"] == ["assigned"]
    assert "Agent: — → Desk Agent" in assigned["message"]["non_text"][0]["description"]


def test_an_older_ticket_first_seen_assigned_to_this_agent_says_so(fake, desk):
    old = fake.add_ticket(subject="Backlog", description="from before")
    long_ago = "2026-01-01T00:00:00Z"  # untouched for long before the connection: the first walk does not see it
    fake.tickets[old["id"]].update(created_at=long_ago, updated_at=long_ago)
    connect(fake, desk)
    fake.tickets[old["id"]].update(responder_id=ME, updated_at=fake.iso_now())
    [event] = cycle(desk)
    assert event["facts"] == ["assigned"]
    assert "first seen now" in event["message"]["non_text"][0]["description"]


def test_an_edited_note_is_a_new_revision_and_nothing_repeats(fake, desk):
    connect(fake, desk)
    ticket = fake.add_ticket(subject="A", description="b")
    note = fake.add_entry(ticket["id"], body="Draft one", user_id=LEAD, incoming=False, private=True, source=2)
    assert len(cycle(desk)) == 2
    edited_at = fake.iso_now()
    fake.conversations[ticket["id"]][0].update(body_text="Draft two", body="<div>Draft two</div>",
                                               last_edited_at=edited_at, updated_at=edited_at)
    fake.tickets[ticket["id"]]["updated_at"] = edited_at
    [event] = cycle(desk)
    assert event["source_event_id"].endswith(f":entry:{note['id']}:edit:{edited_at}")
    assert event["text"] == "Draft two" and event["message"]["edited_at"]
    assert cycle(desk) == []


def test_a_walk_anchored_by_time_reads_every_changed_ticket(fake, desk):
    connect(fake, desk)
    tickets = [fake.add_ticket(subject=f"T{n}", description="x") for n in range(230)]
    events = cycle(desk)
    assert sorted(int(e["conversation_id"]) for e in events) == sorted(t["id"] for t in tickets)
    lists = [r for r in fake.requests if r["path"] == "/api/v2/tickets" and r["query"].get("updated_since")]
    assert all(r["query"]["page"] == ["1"] for r in lists[1:])  # the next page is asked by time, not offset


def test_a_small_budget_leaves_work_for_the_next_cycle(fake, desk):
    connect(fake, desk)
    for n in range(5):
        fake.add_ticket(subject=f"T{n}", description="x")
    client, store = desk.client(), desk.store()
    try:
        budget = Budget(store, share=25, poll_seconds=60)
        budget.left = 5
        walk(client, store, budget)
        process(desk, client, store, budget, capabilities={})
        assert store.one("SELECT COUNT(*) AS n FROM dirty")["n"] > 0
    finally:
        store.close()
    assert len(cycle(desk)) == 5
