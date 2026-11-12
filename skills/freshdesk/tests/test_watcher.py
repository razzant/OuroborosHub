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
from freshdesk_lib.watcher import Budget, baseline, disconnect, process, walk  # noqa: E402

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


def test_a_bulk_update_within_two_seconds_is_walked_to_the_end(fake, desk):
    fake.tick = lambda seconds=1: fake.clock  # 230 tickets written in one second, long before the connection
    tickets = [fake.add_ticket(subject=f"T{n}", description="x") for n in range(230)]
    del fake.tick
    connect(fake, desk)
    first, second = fake.iso_now(), fake.iso_now()
    for n, ticket in enumerate(tickets):  # an automation touches all of them across two seconds
        fake.tickets[ticket["id"]]["updated_at"] = first if n < 150 else second
    client, store = desk.client(), desk.store()
    try:
        budget = plenty(store)
        assert walk(client, store, budget)["complete"]
        assert store.one("SELECT COUNT(*) AS n FROM dirty")["n"] == 230
        assert budget.spent < 30  # a few pages, never the same second over and over
    finally:
        store.close()


def test_an_outbound_email_this_agent_sent_is_not_news_but_the_answer_is(fake, desk):
    connect(fake, desk)
    sent = Sends(desk).email(account_id=HOST, op="outbound", to_emails=["customer.two@example.org"],
                             subject="Your order", text="Your order has shipped.", request_id="o-1", email_config_id=4001)
    assert sent["ok"]
    made = Sends(desk).request(account_id=HOST, method="POST", path="tickets", request_id="t-1",
                               body={"email": "customer.one@example.com", "subject": "Phone call",
                                     "description": "Logged from a call.", "responder_id": ME})
    assert made["ok"]
    assert cycle(desk) == []  # neither ticket this connector opened comes back as the customer's new ticket
    answer = fake.add_entry(sent["ticket_id"], body="Thanks!", user_id=502, incoming=True, source=1)
    [event] = cycle(desk)
    assert facts([event]) == [(f"{sent['ticket_id']}:entry:{answer['id']}", ["any_message", "my_thread"])]
    opening = event["conversation"]["thread_history"]["data"]["messages"][0]
    assert (opening["actor_id"], opening["display_name"]) == (str(ME), "Desk Agent")


def test_a_teammates_outbound_email_is_not_the_customers_words(fake, desk):
    connect(fake, desk)
    ticket = fake.add_ticket(subject="Renewal", description="Your plan renews next week.", requester_id=502,
                             source=10, responder_id=LEAD)
    [event] = cycle(desk)
    assert event["source_event_id"].endswith(f"{ticket['id']}:created") and event["facts"] == ["any_message", "new_issue"]
    assert event["actor"] == {"platform": "freshdesk", "attribution": "unknown"} and event["message"]["incoming"] is False
    opening = event["conversation"]["thread_history"]["data"]["messages"][0]
    assert (opening["actor_id"], opening["display_name"]) == ("", "The helpdesk (outbound email)")


def test_a_ticket_deleted_before_it_was_read_does_not_hold_up_the_others(fake, desk):
    connect(fake, desk)
    spam = fake.add_ticket(subject="Win a prize", description="click")
    real = fake.add_ticket(subject="Charged twice", description="two charges")
    client, store = desk.client(), desk.store()
    try:
        baseline(client, store)
        budget = plenty(store)
        assert walk(client, store, budget)["complete"]
        del fake.tickets[spam["id"]]  # deleted for good between the list and the read
        process(desk, client, store, budget, capabilities={"presence_identity": 1})
        events = [json.loads(r["event"]) for r in store.rows("SELECT event FROM inbox ORDER BY seq")]
        assert [e["conversation_id"] for e in events] == [str(real["id"])]
        assert store.one("SELECT COUNT(*) AS n FROM dirty")["n"] == 0
        skipped = store.meta("skipped_tickets")
        assert (skipped["count"], skipped["last"]) == (1, str(spam["id"]))
        assert store.gaps() == []  # a fact in freshdesk_status, not a lasting "needs attention"
    finally:
        store.close()


def test_a_burst_with_fractional_seconds_is_walked_to_the_end(fake, desk):
    fake.tick = lambda seconds=1: fake.clock
    tickets = [fake.add_ticket(subject=f"T{n}", description="x") for n in range(150)]
    del fake.tick
    connect(fake, desk)
    moment = fake.iso_now()[:-1] + ".017Z"  # Freshdesk's reference shows milliseconds in some answers
    for ticket in tickets:
        fake.tickets[ticket["id"]]["updated_at"] = moment
    client, store = desk.client(), desk.store()
    try:
        budget = plenty(store)
        assert walk(client, store, budget)["complete"]
        assert store.one("SELECT COUNT(*) AS n FROM dirty")["n"] == 150 and budget.spent < 30
    finally:
        store.close()


def test_a_colleagues_edit_of_a_note_this_skill_wrote_is_news(fake, desk):
    connect(fake, desk)
    ticket = fake.add_ticket(subject="Refund", description="please")
    assert len(cycle(desk)) == 1
    sends = Sends(desk)
    note = sends.note(account_id=HOST, ticket_id=ticket["id"], text="Handing over", request_id="n-1")
    assert note["ok"] and cycle(desk) == []  # its own note is not news
    assert sends.note(account_id=HOST, ticket_id=ticket["id"], text="Handing over to billing", request_id="n-2",
                      edit_note_id=note["id"])["ok"]
    assert cycle(desk) == []  # nor its own edit
    edited_at = fake.iso_now()
    entry = fake.conversations[ticket["id"]][0]
    entry.update(body_text="STOP: legal hold", body="<div>STOP: legal hold</div>", last_edited_at=edited_at,
                 updated_at=edited_at, last_edited_user_id=LEAD, to_emails=["desk.agent@example.net"])
    fake.tickets[ticket["id"]]["updated_at"] = edited_at
    [event] = cycle(desk)
    assert event["source_event_id"].endswith(f":entry:{note['id']}:edit:{edited_at}")
    assert event["actor"] == {"platform_actor_id": str(LEAD), "display_name": "Team Lead"}
    assert event["facts"] == ["any_message", "my_thread", "mention"] and event["text"] == "STOP: legal hold"


def test_editing_a_colleagues_note_makes_the_ticket_one_this_agent_wrote_in(fake, desk):
    connect(fake, desk)
    ticket = fake.add_ticket(subject="Refund", description="please")
    note = fake.add_entry(ticket["id"], body="Customer called", user_id=LEAD, incoming=False, private=True, source=2)
    assert len(cycle(desk)) == 2
    assert Sends(desk).note(account_id=HOST, ticket_id=ticket["id"], text="Customer called twice", request_id="n-9",
                            edit_note_id=note["id"])["ok"]
    assert cycle(desk) == []  # its own edit is not news
    answer = fake.add_entry(ticket["id"], body="Any update?", user_id=501, incoming=True, source=1)
    assert facts(cycle(desk)) == [(f"{ticket['id']}:entry:{answer['id']}", ["any_message", "my_thread"])]


def test_this_skills_own_property_change_is_not_news_but_a_colleagues_is(fake, desk):
    connect(fake, desk)
    ticket = fake.add_ticket(subject="Refund", description="please", status=2)
    assert len(cycle(desk)) == 1
    answer = fake.add_entry(ticket["id"], body="Any news?", user_id=501, incoming=True, source=1)
    assert Sends(desk).update(account_id=HOST, ticket_id=ticket["id"], agent="me", status="Pending")["ok"]
    events = cycle(desk)  # the customer's reply that came just before still arrives; the own change does not
    assert facts(events) == [(f"{ticket['id']}:entry:{answer['id']}", ["any_message"])]
    fake.tickets[ticket["id"]].update(priority=4, updated_at=fake.iso_now())
    [change] = cycle(desk)
    assert change["message"]["non_text"][0]["description"] == "Priority: Low → Urgent"


def test_a_colleagues_change_in_the_same_minute_is_not_absorbed(fake, desk):
    connect(fake, desk)
    ticket = fake.add_ticket(subject="Refund", description="please", status=2)
    assert len(cycle(desk)) == 1
    fake.tickets[ticket["id"]].update(responder_id=ME, priority=4, updated_at=fake.iso_now())  # a colleague
    assert Sends(desk).update(account_id=HOST, ticket_id=ticket["id"], status="Pending")["ok"]  # this skill
    [change] = cycle(desk)
    assert change["facts"] == ["assigned"]
    assert change["message"]["non_text"][0]["description"] == "Priority: Low → Urgent; Agent: — → Desk Agent"


def test_taking_an_older_ticket_never_read_does_not_wake_as_assigned(fake, desk):
    old = fake.add_ticket(subject="Backlog", description="from before")
    long_ago = "2026-01-01T00:00:00Z"
    fake.tickets[old["id"]].update(created_at=long_ago, updated_at=long_ago)
    connect(fake, desk)
    assert Sends(desk).update(account_id=HOST, ticket_id=old["id"], agent="me", status="Pending")["ok"]
    assert cycle(desk) == []  # its own assignment of a ticket it had not read is not "first seen assigned"
    fake.tickets[old["id"]].update(priority=3, updated_at=fake.iso_now())
    [change] = cycle(desk)
    assert change["message"]["non_text"][0]["description"] == "Priority: Low → High" and change["facts"] == []


def test_an_own_change_made_while_the_ticket_is_processed_is_not_reversed(fake, desk, monkeypatch):
    import freshdesk_lib.watcher as watcher_module
    connect(fake, desk)
    ticket = fake.add_ticket(subject="Refund", description="please", status=2)
    assert len(cycle(desk)) == 1
    fake.add_entry(ticket["id"], body="Any news?", user_id=501, incoming=True, source=1)
    reading = watcher_module._conversation

    def read_then_update(client, store, budget, ticket_id):
        entries = reading(client, store, budget, ticket_id)
        if ticket_id == str(ticket["id"]):  # this skill changes the ticket while the watcher reads its thread
            assert Sends(desk).update(account_id=HOST, ticket_id=ticket["id"], status="Pending")["ok"]
        return entries

    monkeypatch.setattr(watcher_module, "_conversation", read_then_update)
    assert [e["facts"] for e in cycle(desk)] == [["any_message"]]  # the reply; no reversed status change
    monkeypatch.setattr(watcher_module, "_conversation", reading)
    assert cycle(desk) == []  # nor the own change afterwards


def test_a_change_through_the_generic_writer_is_not_news(fake, desk):
    connect(fake, desk)
    ticket = fake.add_ticket(subject="Refund", description="please", status=2)
    assert len(cycle(desk)) == 1
    done = Sends(desk).request(account_id=HOST, method="PUT", path=f"tickets/{ticket['id']}", body={"status": 3},
                               request_id="g-1")
    assert done["ok"] and cycle(desk) == []


def test_an_edit_made_while_no_place_was_connected_is_history(fake, desk):
    connect(fake, desk)
    ticket = fake.add_ticket(subject="A", description="b")
    note = fake.add_entry(ticket["id"], body="Draft", user_id=LEAD, incoming=False, private=True, source=2)
    assert len(cycle(desk)) == 2
    store = desk.store()
    try:
        assert disconnect(store) is True
    finally:
        store.close()
    edited_at = fake.iso_now()
    fake.conversations[ticket["id"]][0].update(body_text="Final", body="<div>Final</div>", last_edited_at=edited_at,
                                               last_edited_user_id=LEAD)
    fake.tickets[ticket["id"]]["updated_at"] = edited_at
    fake.settle()
    assert cycle(desk) == []  # the new connection's cutoff makes that edit history
    assert note["id"]


def test_a_removed_place_ends_the_events_it_had_not_handed_over(fake, desk):
    connect(fake, desk)
    fake.add_ticket(subject="One", description="a")
    fake.add_ticket(subject="Two", description="b")
    client, store = desk.client(), desk.store()
    try:
        baseline(client, store)
        budget = plenty(store)
        walk(client, store, budget)
        process(desk, client, store, budget, capabilities={"presence_identity": 1})
        first = store.one("SELECT source_event_id FROM inbox ORDER BY seq")["source_event_id"]
        store.db.execute("UPDATE inbox SET state='blocked' WHERE source_event_id=?", (first,))
        store.gap(f"event:{first}", "place_not_connected")
        assert disconnect(store) is True
        assert {r["state"] for r in store.rows("SELECT state FROM inbox")} == {"ended"}
        assert store.meta("connection_ended")["events_not_offered"] == 2 and store.gaps() == []
    finally:
        store.close()


def test_a_reconnected_place_starts_a_new_connection(fake, desk):
    connect(fake, desk)
    ticket = fake.add_ticket(subject="Before", description="heard while connected")
    assert len(cycle(desk)) == 1
    store = desk.store()
    try:
        assert disconnect(store) is True and disconnect(store) is False
    finally:
        store.close()
    gap = fake.add_ticket(subject="During", description="written while no place was connected")
    fake.add_entry(ticket["id"], body="A reply while disconnected", user_id=501, incoming=True, source=1)
    fake.settle()
    assert cycle(desk) == []  # what happened while disconnected is history for the new connection
    fake.settle()
    after = fake.add_entry(gap["id"], body="A reply after reconnecting", user_id=501, incoming=True, source=1)
    assert facts(cycle(desk)) == [(f"{gap['id']}:entry:{after['id']}", ["any_message"])]


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
