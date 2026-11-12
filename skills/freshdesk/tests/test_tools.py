"""Every tool against the loopback Freshdesk: names for numbers, the send journal, refusals that send nothing."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_freshdesk import KEY, ME, FakeFreshdesk  # noqa: E402
from freshdesk_lib.client import FreshdeskError  # noqa: E402
from freshdesk_lib.reads import Reads  # noqa: E402
from freshdesk_lib.sends import Sends  # noqa: E402
from freshdesk_lib.service import Service, write_settings  # noqa: E402

HOST = "example.freshdesk.com"


@pytest.fixture()
def fake():
    server = FakeFreshdesk()
    server.base = server.start()
    yield server
    server.stop()


@pytest.fixture()
def tools(fake, tmp_path):
    write_settings(tmp_path, {"domain": HOST})
    service = Service(tmp_path, lambda: KEY, base_url=fake.base, timeout=3.0)
    return Reads(service), Sends(service), service


def bodies(fake, ticket_id):
    return [(entry["body_text"], entry["private"], entry["user_id"]) for entry in fake.conversations[ticket_id]]


def test_status_before_setup_says_what_to_save(tmp_path):
    reads = Reads(Service(tmp_path, lambda: ""))
    status = reads.status()
    assert status["ok"] is False and status["code"] == "not_configured" and "example.freshdesk.com" in status["next_step"]


def test_the_probe_names_the_agent_and_which_endpoints_answer(tools):
    reads, _, _ = tools
    status = reads.status(probe=True)
    probe = status["probe"]
    assert probe["agent"]["name"] == "Desk Agent" and probe["agent"]["scope"] == "all tickets"
    assert probe["endpoints"]["tickets"] == "answers" and probe["endpoints"]["canned_responses"] == "admin_only"
    assert probe["endpoints"]["groups"] == "admin_only" and probe["writes_tested"] is False
    assert status["limits"]["total"] == 100


def test_fields_list_custom_statuses_and_required_custom_fields(tools):
    reads, _, _ = tools
    fields = reads.fields()
    labels = [row["label"] for row in fields["standard"]["status"]["choices"]]
    assert "Waiting on Customer" in labels and "Waiting on Third Party" in labels
    refund = next(row for row in fields["custom"] if row["name"] == "cf_refund_reason")
    assert refund["required_for_closure"] is True and {"value": "Other"} in refund["choices"]


def test_find_compiles_names_into_a_search_query(tools, fake):
    reads, _, _ = tools
    open_one = fake.add_ticket(subject="Charged twice", description="two charges", priority=3)
    fake.add_ticket(subject="Thanks", description="all good", status=4)
    found = reads.find(status="open", priority="high", agent="unassigned")
    assert [row["id"] for row in found["tickets"]] == [open_one["id"]]
    assert found["tickets"][0]["status"] == {"id": 2, "label": "Open"}
    assert fake.requests[-1]["query"]["query"] == ['"status:2 AND priority:3 AND agent_id:null"']
    assert "300" in found["note"]


def test_find_by_requester_uses_the_ticket_list(tools, fake):
    reads, _, _ = tools
    fake.add_ticket(subject="A", description="b")
    found = reads.find(requester_email="customer.one@example.com")
    assert fake.requests[-1]["path"] == "/api/v2/tickets" and found["tickets"]
    assert "30 days" in found["note"]


def test_a_ticket_reads_whole_with_authors_named(tools, fake):
    reads, _, _ = tools
    ticket = fake.add_ticket(subject="Refund", description="I was charged twice.", requester_id=501)
    fake.tickets[ticket["id"]]["requester"] = {"name": "Customer One", "email": "customer.one@example.com"}
    fake.add_entry(ticket["id"], body="Sorry about that.", user_id=ME, incoming=False)
    fake.add_entry(ticket["id"], body="Thanks, waiting.", user_id=501, incoming=True, source=1)
    result = reads.ticket(ticket_id=ticket["id"])
    assert result["complete"] is True and result["ticket"]["description"] == "I was charged twice."
    authors = [(e["author"]["kind"], e["author"]["name"], e["text"]) for e in result["entries"]]
    assert authors == [("agent", "Desk Agent", "Sorry about that."), ("contact", "Customer One", "Thanks, waiting.")]


def test_a_long_thread_is_paged_never_cut_silently(tools, fake):
    reads, _, _ = tools
    ticket = fake.add_ticket(subject="Long", description="long thread")
    for number in range(150):
        fake.add_entry(ticket["id"], body=f"{number:03d} " + "x" * 600, user_id=501, incoming=True)
    first = reads.ticket(ticket_id=ticket["id"])
    assert first["complete"] is False and first["next_page"] == 2 and "page=2" in first["note"]
    rest = reads.ticket(ticket_id=ticket["id"], page=first["next_page"])
    seen = [e["text"][:3] for e in first["entries"] + rest["entries"]]
    assert seen == [f"{n:03d}" for n in range(150)]


def test_a_reply_is_journalled_and_a_repeat_sends_nothing(tools, fake):
    _, sends, service = tools
    ticket = fake.add_ticket(subject="Charged twice", description="two charges")
    first = sends.reply(account_id=HOST, ticket_id=ticket["id"], text="We refunded the second charge.",
                        request_id="refund-1001")
    assert first["ok"] is True and first["state"] == "accepted" and first["url"].endswith(f"/a/tickets/{ticket['id']}")
    again = sends.reply(account_id=HOST, ticket_id=ticket["id"], text="We refunded the second charge.",
                        request_id="refund-1001")
    assert again["recorded"] is True and again["state"] == "accepted"
    assert bodies(fake, ticket["id"]) == [("We refunded the second charge.", False, ME)]
    other = sends.reply(account_id=HOST, ticket_id=ticket["id"], text="Different words.", request_id="refund-1001")
    assert other["code"] == "request_id_conflict" and len(fake.conversations[ticket["id"]]) == 1
    store = service.store()
    try:
        assert store.journal("refund-1001")["report_state"] == "pending"
    finally:
        store.close()


def test_a_rate_limited_reply_sent_nothing_and_its_id_is_free(tools, fake):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="A", description="b")
    fake.fault("POST", r"/reply$", "rate", retry_after=3)
    refused = sends.reply(account_id=HOST, ticket_id=ticket["id"], text="Hello", request_id="r-1")
    assert refused["sent"] is False and refused["code"] == "rate_limited" and refused["retry_after"] == 3.0
    assert sends.reply(account_id=HOST, ticket_id=ticket["id"], text="Hello", request_id="r-1")["ok"] is True


def test_an_unanswered_reply_found_by_readback_is_delivered_once(tools, fake):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="Login", description="no email")
    fake.fault("POST", r"/reply$", "drop_after_apply")
    result = sends.reply(account_id=HOST, ticket_id=ticket["id"], text="We resent the sign-in email.", request_id="r-2")
    assert result["ok"] is True and "readback" in result["note"]
    assert len(fake.conversations[ticket["id"]]) == 1


def test_an_earlier_reply_with_the_same_opening_is_no_proof(tools, fake):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="Refund", description="please")
    preamble = "Thank you for writing to us. " * 40  # longer than any prefix a comparison might stop at
    fake.add_entry(ticket["id"], body=preamble + "Refund denied.", user_id=ME, incoming=False, source=0)
    fake.fault("POST", r"/reply$", "server")
    result = sends.reply(account_id=HOST, ticket_id=ticket["id"], text=preamble + "Refund approved.", request_id="p-1")
    assert result["code"] == "uncertain" and result["candidates"] == []


def test_an_unproven_reply_stays_uncertain_and_is_never_resent(tools, fake):
    _, sends, service = tools
    ticket = fake.add_ticket(subject="Slow", description="slow")
    service.timeout = 0.5
    fake.fault("POST", r"/reply$", "slow", seconds=1.5)
    result = sends.reply(account_id=HOST, ticket_id=ticket["id"], text="Answer", request_id="r-3")
    assert result["code"] == "uncertain"
    service.timeout = 3.0
    again = sends.reply(account_id=HOST, ticket_id=ticket["id"], text="Answer", request_id="r-3")
    assert again["recorded"] is True and again["state"] == "uncertain"
    import time
    time.sleep(1.5)
    assert len(fake.conversations[ticket["id"]]) == 1  # the slow original landed; nothing was sent twice


def test_a_private_note_notifies_agents_by_email(tools, fake):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="Refund", description="please")
    result = sends.note(account_id=HOST, ticket_id=ticket["id"], text="Draft: we refund within 5 days.",
                        request_id="n-1", notify=["team.lead@example.net"], audience="notify")
    assert result["ok"] is True and result["state"] == "delivered"
    entry = fake.conversations[ticket["id"]][0]
    assert entry["private"] is True and entry["to_emails"] == ["team.lead@example.net"]
    with pytest.raises(FreshdeskError) as caught:
        sends.note(account_id=HOST, ticket_id=ticket["id"], text="x", request_id="n-2", notify=["Team Lead"],
                   audience="notify")
    assert caught.value.code == "notify_needs_email"


def test_a_note_that_notifies_must_declare_it(tools, fake):
    """The manifest's sends block names notified agents as recipients only for audience=notify calls."""
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="A", description="b")
    with pytest.raises(FreshdeskError) as caught:
        sends.note(account_id=HOST, ticket_id=ticket["id"], text="x", request_id="n-3", notify=["lead@example.net"])
    assert caught.value.code == "audience_notify_required" and fake.conversations[ticket["id"]] == []
    with pytest.raises(FreshdeskError) as caught:
        sends.note(account_id=HOST, ticket_id=ticket["id"], text="x", request_id="n-4", audience="notify")
    assert caught.value.code == "notify_missing"


class FakeHost:
    def __init__(self, state="active", fail=False):
        self.state, self.fail, self.asked = state, fail, []

    def place(self, account_id, conversation_id="", thread_id=""):
        from freshdesk_lib.host import HostUnavailable
        self.asked.append((account_id, conversation_id, thread_id))
        if self.fail:
            raise HostUnavailable("host_unreachable")
        return {"state": self.state, "label": "Freshdesk"}


@pytest.mark.parametrize("state, sent", [("active", True), ("unbound", True), ("silent", False), ("paused", False)])
def test_the_place_is_checked_just_before_each_outward_call(tools, fake, state, sent):
    _, sends, service = tools
    service.host = FakeHost(state)
    ticket = fake.add_ticket(subject="A", description="b")
    result = sends.reply(account_id=HOST, ticket_id=ticket["id"], text="Hello", request_id=f"p-{state}")
    assert result["ok"] is sent and bool(fake.conversations[ticket["id"]]) is sent
    assert service.host.asked == [(HOST, str(ticket["id"]), str(ticket["id"]))]
    if not sent:
        assert result["code"] == f"place_{state}" and result["sent"] is False
        service.host = FakeHost("active")
        assert sends.reply(account_id=HOST, ticket_id=ticket["id"], text="Hello", request_id=f"p-{state}")["ok"]


def test_an_unconfirmed_place_sends_nothing(tools, fake):
    _, sends, service = tools
    service.host = FakeHost(fail=True)
    ticket = fake.add_ticket(subject="A", description="b")
    result = sends.update(account_id=HOST, ticket_id=ticket["id"], priority="Urgent")
    assert result["code"] == "place_unknown" and fake.tickets[ticket["id"]]["priority"] == 1


def test_two_matching_entries_are_candidates_not_proof(tools, fake):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="A", description="b")
    fake.add_entry(ticket["id"], body="Same words.", user_id=ME, incoming=False, private=False, source=0)
    fake.fault("POST", r"/reply$", "drop_after_apply")
    result = sends.reply(account_id=HOST, ticket_id=ticket["id"], text="Same words.", request_id="c-1")
    assert result["code"] == "uncertain" and len(result["candidates"]) == 2


def test_a_chat_ticket_gets_structured_words_with_their_attachment(tools, fake, tmp_path):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="Chat", description="How do I export?", source=7)
    steps = tmp_path / "steps.txt"
    steps.write_text("1. Open settings\n")
    result = sends.reply(account_id=HOST, ticket_id=ticket["id"], text="Here are the steps.", request_id="c-1",
                         attachment_paths=[str(steps)])
    assert result["ok"]
    [entry] = fake.conversations[ticket["id"]]
    assert entry["body_text"] == "Here are the steps." and [a["name"] for a in entry["attachments"]] == ["steps.txt"]
    sent = fake.requests[-1]["body"]
    assert "body" not in sent and '"body_contents"' in sent["structured_body"]  # JSON in the form, not a repr


def test_a_note_edit_checks_the_note_belongs_to_the_ticket(tools, fake):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="A", description="b")
    other = fake.add_ticket(subject="C", description="d")
    note = fake.add_entry(other["id"], body="Elsewhere", user_id=ME, incoming=False, private=True, source=2)
    refused = sends.note(account_id=HOST, ticket_id=ticket["id"], text="Edited", request_id="e-1", edit_note_id=note["id"])
    assert refused["sent"] is False and refused["code"] == "note_not_in_ticket"


def test_a_note_edit_keeps_who_sees_it(tools, fake):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="A", description="b")
    public = fake.add_entry(ticket["id"], body="Shown to the customer", user_id=ME, incoming=False, private=False, source=2)
    refused = sends.note(account_id=HOST, ticket_id=ticket["id"], text="Internal margin is high.", request_id="v-1",
                         edit_note_id=public["id"])  # public defaults to false: the intent was a private text
    assert refused["sent"] is False and refused["code"] == "note_visibility_differs"
    assert fake.conversations[ticket["id"]][0]["body_text"] == "Shown to the customer"
    edited = sends.note(account_id=HOST, ticket_id=ticket["id"], text="Shown, corrected", request_id="v-2",
                        edit_note_id=public["id"], public=True)
    assert edited["ok"] is True and fake.conversations[ticket["id"]][0]["body_text"] == "Shown, corrected"


def test_an_unapplied_note_edit_is_no_proof_and_an_applied_one_is(tools, fake, tmp_path):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="A", description="b")
    note = fake.add_entry(ticket["id"], body="Draft handoff", user_id=ME, incoming=False, private=True, source=2)
    log = tmp_path / "handoff.pdf"
    log.write_bytes(b"%PDF-1.4 handoff")
    fake.fault("PUT", r"/conversations/\d+$", "server")  # refused before it changed anything
    failed = sends.note(account_id=HOST, ticket_id=ticket["id"], text="Draft handoff", request_id="e-1",
                        edit_note_id=note["id"], attachment_paths=[str(log)])
    assert failed["code"] == "uncertain" and fake.conversations[ticket["id"]][0]["attachments"] == []
    fake.fault("PUT", r"/conversations/\d+$", "drop_after_apply")  # applied, answer lost
    applied = sends.note(account_id=HOST, ticket_id=ticket["id"], text="Final handoff", request_id="e-2",
                         edit_note_id=note["id"], attachment_paths=[str(log)])
    assert applied["ok"] is True and "readback" in applied["note"]


def test_a_colleagues_edit_during_a_failed_edit_is_no_proof(tools, fake, tmp_path):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="A", description="b")
    note = fake.add_entry(ticket["id"], body="Draft handoff", user_id=ME, incoming=False, private=True, source=2)
    mine = tmp_path / "mine.txt"
    mine.write_text("mine")

    def colleague_edits():  # same words, a new revision and an attachment, by someone else
        entry = fake.conversations[ticket["id"]][0]
        entry.update(last_edited_at=fake.iso_now(), last_edited_user_id=9002,
                     attachments=[{"id": 72000, "name": "colleague.txt", "size": 9, "content_type": "text/plain"}])

    fake.fault("PUT", r"/conversations/\d+$", "server", meanwhile=colleague_edits)
    result = sends.note(account_id=HOST, ticket_id=ticket["id"], text="Draft handoff", request_id="c-9",
                        edit_note_id=note["id"], attachment_paths=[str(mine)])
    assert result["code"] == "uncertain" and result["candidates"] == []


def test_a_place_made_silent_during_the_reads_holds_the_edit(tools, fake):
    _, sends, service = tools
    ticket = fake.add_ticket(subject="A", description="b")
    note = fake.add_entry(ticket["id"], body="Draft", user_id=ME, incoming=False, private=True, source=2)
    order = []

    class Host:
        def place(self, *_):
            order.append(("place", len(fake.requests)))
            return {"state": "silent", "label": "Freshdesk"}

    service.host = Host()
    held = sends.note(account_id=HOST, ticket_id=ticket["id"], text="Final", request_id="s-1", edit_note_id=note["id"])
    assert held["code"] == "place_silent" and fake.conversations[ticket["id"]][0]["body_text"] == "Draft"
    reads = [r for r in fake.requests if r["path"].endswith("/conversations")]
    assert reads and order[0][1] >= fake.requests.index(reads[-1]) + 1  # the place is checked after the reads


def test_an_email_reply_can_add_recipients_and_outbound_opens_a_ticket(tools, fake):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="Invoice", description="need a copy")
    reply = sends.email(account_id=HOST, op="reply", ticket_id=ticket["id"], text="Copy attached.",
                        cc_emails=["accounts@example.com"], request_id="m-1")
    assert reply["ok"] is True and fake.requests[-1]["body"]["cc_emails"] == ["accounts@example.com"]
    outbound = sends.email(account_id=HOST, op="outbound", to_emails=["customer.two@example.org"],
                           subject="Your order", text="It shipped today.", request_id="m-2", email_config_id=4001)
    assert outbound["ok"] is True and fake.tickets[outbound["ticket_id"]]["status"] == 5
    with pytest.raises(FreshdeskError):
        sends.email(account_id=HOST, op="outbound", to_emails=["a@example.com", "b@example.com"], subject="x",
                    text="y", request_id="m-3", email_config_id=4001)
    with pytest.raises(FreshdeskError) as caught:
        sends.email(account_id=HOST, op="outbound", ticket_id=ticket["id"], to_emails=["a@example.com"],
                    subject="x", text="y", request_id="m-4", email_config_id=4001)
    assert caught.value.code == "ticket_id_not_for_outbound"


@pytest.mark.parametrize("arguments, code", [
    ({"op": "reply", "to_emails": ["colleague@example.net"]}, "to_emails_not_for_reply"),
    ({"op": "outbound", "to_emails": ["a@example.com"], "subject": "x", "email_config_id": 4001,
      "bcc_emails": ["b@example.com"]}, "outbound_option_unsupported"),
    ({"op": "outbound", "to_emails": ["a@example.com"], "subject": "x", "email_config_id": 4001,
      "from_email": "sales@example.com"}, "outbound_option_unsupported"),
    ({"op": "outbound", "to_emails": ["a@example.com"], "subject": "x"}, "email_config_id_missing"),
    ({"op": "reply_to_forward"}, "to_emails_missing"),
    ({"op": "reply_to_forward", "to_emails": ["vendor@example.org"], "cc_emails": ["b@example.com"]},
     "reply_to_forward_option_unsupported"),
])
def test_a_recipient_freshdesk_would_drop_is_refused_before_sending(tools, fake, arguments, code):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="Invoice", description="need a copy")
    sent_before = len(fake.requests)
    with pytest.raises(FreshdeskError) as caught:
        sends.email(account_id=HOST, text="y", request_id="w-1",
                    ticket_id=None if arguments["op"] == "outbound" else ticket["id"], **arguments)
    assert caught.value.code == code and len(fake.requests) == sent_before


def test_an_unanswered_update_whose_readback_fails_is_uncertain(tools, fake):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="Invoice", description="need a copy")
    fake.fault("PUT", rf"/tickets/{ticket['id']}$", "drop_after_apply")
    fake.fault("GET", rf"/tickets/{ticket['id']}$", "server")
    result = sends.update(account_id=HOST, ticket_id=ticket["id"], priority="High")
    assert result["code"] == "uncertain" and result["ok"] is False and fake.tickets[ticket["id"]]["priority"] == 3


def test_update_sets_properties_by_name_and_checks_closure_first(tools, fake):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="Refund", description="please", tags=["vip"])
    updated = sends.update(account_id=HOST, ticket_id=ticket["id"], status="Waiting on Customer", priority="High",
                           group="Billing", agent="me", tags_add=["refund"], tags_remove=["vip"])
    assert updated["status"] == {"id": 6, "label": "Waiting on Customer"} and updated["agent"]["id"] == ME
    assert updated["tags"] == ["refund"]
    blocked = sends.update(account_id=HOST, ticket_id=ticket["id"], status="Resolved")
    assert blocked["code"] == "closure_fields_missing" and blocked["fields"][0]["field"] == "cf_refund_reason"
    assert fake.tickets[ticket["id"]]["status"] == 6
    done = sends.update(account_id=HOST, ticket_id=ticket["id"], status="Resolved",
                        custom_fields={"Refund reason": "duplicate charge"})
    assert done["status"]["label"] == "Resolved" and done["custom_fields"]["cf_refund_reason"] == "Duplicate charge"


def test_a_dependent_fields_lower_levels_can_be_set(tools, fake):
    reads, sends, _ = tools
    listed = {row["name"]: row for row in reads.fields()["custom"]}
    assert (listed["cf_subcategory"]["parent"], listed["cf_item"]["level"]) == ("cf_category", 3)
    ticket = fake.add_ticket(subject="Broken", description="screen")
    done = sends.update(account_id=HOST, ticket_id=ticket["id"],
                        custom_fields={"Category": "Hardware", "cf_subcategory": "Laptop", "Item": "Screen"})
    assert done["ok"] is True
    assert {k: fake.tickets[ticket["id"]]["custom_fields"][k] for k in ("cf_category", "cf_subcategory", "cf_item")} == {
        "cf_category": "Hardware", "cf_subcategory": "Laptop", "cf_item": "Screen"}


def test_an_unknown_choice_refreshes_the_catalog_once_then_lists_choices(tools, fake):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="A", description="b")
    with pytest.raises(FreshdeskError) as caught:
        sends.update(account_id=HOST, ticket_id=ticket["id"], status="Snoozed")
    assert caught.value.code == "choice_unknown"
    assert sum(1 for r in fake.requests if r["path"] == "/api/v2/ticket_fields") == 2


@pytest.mark.parametrize("method, path, body, tool", [
    ("POST", "tickets/7/reply", {"body": "x"}, "freshdesk_reply"),
    ("POST", "/api/v2/tickets/7/notes", {"body": "x"}, "freshdesk_note"),
    ("POST", "tickets/7/forward", {"body": "x"}, "freshdesk_email"),
    ("POST", "tickets/outbound_email", {"email": "a@example.com"}, "freshdesk_email"),
    ("PUT", "conversations/9", {"body": "x"}, "freshdesk_note"),
    ("PUT", "tickets/merge", {"primary_id": 1, "ticket_ids": [2], "note_in_primary": "x"}, "freshdesk_note"),
    ("PUT", "tickets/bulk_update", {"bulk_action": {"ids": [1], "reply": {"body": "x"}}}, "freshdesk_reply"),
    ("POST", "tickets/7/reply.json", {"body": "x"}, "freshdesk_reply"),  # other spellings of the same endpoint
    ("POST", "tickets/7/reply.json.json", {"body": "x"}, "freshdesk_reply"),
    ("POST", "/tickets/7/reply/", {"body": "x"}, "freshdesk_reply"),
])
def test_the_generic_writer_refuses_every_call_that_carries_words(tools, method, path, body, tool):
    _, sends, _ = tools
    with pytest.raises(FreshdeskError) as caught:
        sends.request(account_id=HOST, method=method, path=path, body=body, request_id="g-1")
    assert caught.value.code == "words_need_typed_tool" and tool in caught.value.message


@pytest.mark.parametrize("path", ["tickets//7/reply", "tickets/7/./reply", "tickets/../agents"])
def test_an_odd_spelling_of_a_path_is_refused_before_sending(tools, fake, path):
    _, sends, _ = tools
    before = len(fake.requests)
    with pytest.raises(FreshdeskError) as caught:
        sends.request(account_id=HOST, method="POST", path=path, body={"body": "x"}, request_id="q-1")
    assert caught.value.code == "path_invalid" and len(fake.requests) == before


def test_the_generic_writer_creates_and_merges_without_reporting_words(tools, fake, ):
    _, sends, service = tools
    created = sends.request(account_id=HOST, method="POST", path="tickets", request_id="g-2",
                            body={"email": "customer.two@example.org", "subject": "Phone call", "description": "Called about delivery."})
    assert created["ok"] is True and fake.tickets[created["id"]]["subject"] == "Phone call"
    second = fake.add_ticket(subject="Duplicate", description="same issue")
    merged = sends.request(account_id=HOST, method="PUT", path="tickets/merge", request_id="g-3",
                           body={"primary_id": created["id"], "ticket_ids": [second["id"]]})
    assert merged["ok"] is True and fake.tickets[second["id"]]["status"] == 5
    store = service.store()
    try:
        assert store.journal("g-2")["report_state"] == "none"
    finally:
        store.close()


def test_a_different_helpdesk_in_account_id_is_refused(tools, fake):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="A", description="b")
    with pytest.raises(FreshdeskError) as caught:
        sends.reply(account_id="other.freshdesk.com", ticket_id=ticket["id"], text="x", request_id="x-1")
    assert caught.value.code == "account_mismatch" and fake.conversations[ticket["id"]] == []


def test_find_refuses_to_drop_filters_freshdesk_cannot_combine(tools, fake):
    reads, _, _ = tools
    before = len(fake.requests)
    with pytest.raises(FreshdeskError) as caught:
        reads.find(account_id=HOST, status="Open", requester_email="customer.one@example.com")
    assert caught.value.code == "filters_conflict" and len(fake.requests) == before
    listed = reads.find(account_id=HOST, requester_email="customer.one@example.com", updated_since="2026-01-01T00:00:00Z")
    assert listed["ok"] and listed["note"] is None


def test_a_multi_paragraph_reply_is_found_by_readback(tools, fake):
    _, sends, _ = tools
    ticket = fake.add_ticket(subject="Refund", description="please")
    fake.fault("POST", r"/reply$", "drop_after_apply")
    result = sends.reply(account_id=HOST, ticket_id=ticket["id"], request_id="m-p",
                         text="Thank you for waiting.\n\n- the first charge is refunded\n- the second charge is refunded")
    assert result["ok"] is True and "readback" in result["note"]


def test_an_uncertain_outbound_email_queues_no_receipt(tools, fake):
    _, sends, service = tools
    fake.fault("POST", r"/outbound_email$", "server")
    result = sends.email(account_id=HOST, op="outbound", to_emails=["customer.two@example.org"], subject="Order",
                         text="It shipped.", request_id="o-u", email_config_id=4001)
    assert result["code"] == "uncertain"
    store = service.store()
    try:
        assert store.journal("o-u")["report_state"] == "none"  # no conversation to report it into
    finally:
        store.close()
