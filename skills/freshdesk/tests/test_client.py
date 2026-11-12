"""The transport and the field catalog against a loopback Freshdesk with invented data."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_freshdesk import KEY, ME, FakeFreshdesk  # noqa: E402
from freshdesk_lib import client as transport  # noqa: E402
from freshdesk_lib.client import Client, FreshdeskError, Response, canonical_host  # noqa: E402
from freshdesk_lib.fields import FieldCatalog  # noqa: E402


@pytest.fixture()
def fake():
    server = FakeFreshdesk()
    server.base = server.start()
    yield server
    server.stop()


def make_client(fake, key=KEY, timeout=5.0):
    return Client("example.freshdesk.com", key, timeout=timeout, base_url=fake.base)


@pytest.mark.parametrize("given", ["example", "Example.Freshdesk.com", "https://example.freshdesk.com/a/tickets/5",
                                   "example.freshdesk.com:443", "  example.freshdesk.com. "])
def test_the_helpdesk_address_is_one_canonical_host(given):
    assert canonical_host(given) == "example.freshdesk.com"


@pytest.mark.parametrize("given", ["", "support.example.com", "example.freshdesk.com.evil.test", "ftp://example.freshdesk.com",
                                   "-bad.freshdesk.com"])
def test_a_custom_or_foreign_domain_is_refused_with_the_reason(given):
    with pytest.raises(FreshdeskError) as caught:
        canonical_host(given)
    assert caught.value.code == "domain_invalid" and "custom domain" in caught.value.message


def test_a_read_returns_data_and_the_rate_limit_facts(fake):
    response = make_client(fake).get("agents/me")
    assert response.status == 200 and response.data["id"] == ME
    assert response.limits["total"] == 100 and response.limits["account_total"] == 100
    assert response.limits["remaining"] == 99


def test_a_wrong_key_is_an_authentication_failure_that_wrote_nothing(fake):
    with pytest.raises(FreshdeskError) as caught:
        make_client(fake, key="not-the-key").get("agents/me")
    assert (caught.value.code, caught.value.status, caught.value.effect) == ("auth_failed", 401, transport.NOT_SENT)


def test_an_admin_only_endpoint_answers_access_denied(fake):
    with pytest.raises(FreshdeskError) as caught:
        make_client(fake).get("canned_response_folders")
    assert (caught.value.code, caught.value.status, caught.value.effect) == ("access_denied", 403, transport.NOT_SENT)


def test_the_rate_limit_is_its_own_effect_with_retry_after(fake):
    fake.fault("POST", r"/reply$", "rate", retry_after=7)
    ticket = fake.add_ticket(subject="Charged twice", description="I see two charges.")
    with pytest.raises(FreshdeskError) as caught:
        make_client(fake).post(f"tickets/{ticket['id']}/reply", {"body": "<p>Hello</p>"})
    assert (caught.value.code, caught.value.effect, caught.value.retry_after) == ("rate_limited", transport.RATE_LIMITED, 7.0)
    assert fake.conversations[ticket["id"]] == []


def test_a_server_error_on_a_write_may_have_taken_effect(fake):
    fake.fault("PUT", r"/tickets/\d+$", "server")
    ticket = fake.add_ticket(subject="A", description="b")
    with pytest.raises(FreshdeskError) as caught:
        make_client(fake).put(f"tickets/{ticket['id']}", {"priority": 3})
    assert (caught.value.code, caught.value.effect) == ("server_error", transport.UNKNOWN)


def test_a_server_error_on_a_read_is_not_an_unknown_write(fake):
    fake.fault("GET", r"/agents/me$", "server")
    with pytest.raises(FreshdeskError) as caught:
        make_client(fake).get("agents/me")
    assert caught.value.effect == transport.NOT_SENT


def test_a_write_applied_but_unanswered_is_unknown_not_failed(fake):
    ticket = fake.add_ticket(subject="Login email", description="The sign-in email never arrives.")
    fake.fault("POST", r"/reply$", "drop_after_apply")
    with pytest.raises(FreshdeskError) as caught:
        make_client(fake).post(f"tickets/{ticket['id']}/reply", {"body": "<p>We resent it.</p>"})
    assert caught.value.effect == transport.UNKNOWN and caught.value.code in ("disconnected", "unreachable")
    # Freshdesk did apply it: this is exactly why an unknown write is never sent again blindly.
    assert [entry["body_text"] for entry in fake.conversations[ticket["id"]]] == ["We resent it."]


def test_a_write_slower_than_the_timeout_is_unknown(fake):
    ticket = fake.add_ticket(subject="A", description="b")
    fake.fault("POST", r"/notes$", "slow", seconds=2)
    with pytest.raises(FreshdeskError) as caught:
        make_client(fake, timeout=0.5).post(f"tickets/{ticket['id']}/notes", {"body": "<p>note</p>"})
    assert (caught.value.code, caught.value.effect) == ("timeout", transport.UNKNOWN)


def test_pages_follow_the_next_link_on_the_same_origin_only(fake):
    for number in range(5):
        fake.add_ticket(subject=f"Ticket {number}", description="text")
    client = make_client(fake)
    page = client.get("tickets", {"per_page": 2, "order_by": "updated_at", "order_type": "asc",
                                  "updated_since": "2026-01-01T00:00:00Z"})
    ids = [row["id"] for row in page.data]
    while page.next_url:
        page = client.next_page(page)
        ids += [row["id"] for row in page.data]
    assert ids == sorted(fake.tickets)
    with pytest.raises(FreshdeskError) as caught:
        client.next_page(Response(200, [], {}, {}, "https://elsewhere.example/api/v2/tickets?page=2"))
    assert caught.value.code == "foreign_link"


@pytest.mark.parametrize("path", ["../secrets", "tickets/1/../../x", "https://elsewhere.example/api/v2/tickets", "tickets?x=1"])
def test_only_api_paths_on_this_helpdesk_are_requested(fake, path):
    with pytest.raises(FreshdeskError) as caught:
        make_client(fake).get(path)
    assert caught.value.code == "path_invalid"


def test_a_note_with_an_attachment_goes_as_multipart(fake):
    ticket = fake.add_ticket(subject="Screenshot", description="see attached")
    fields = [("body", "<p>Here is the log.</p>"), ("private", "true")]
    files = [("attachments[]", "log.txt", b"line one\n", "text/plain")]
    make_client(fake).post(f"tickets/{ticket['id']}/notes", multipart=(fields, files))
    raw = fake.requests[-1]["body"]["_multipart"]
    assert b'name="attachments[]"; filename="log.txt"' in raw and b"line one" in raw


def test_the_field_catalog_resolves_labels_and_ids_both_ways(fake):
    catalog = FieldCatalog.load(make_client(fake))
    assert catalog.resolve("status", "pending") == ("status", 3)
    assert catalog.resolve("status", "Waiting on Customer") == ("status", 6)
    assert catalog.resolve("status", "4") == ("status", 4)
    assert catalog.resolve("agent", "desk agent") == ("responder_id", ME)
    assert catalog.resolve("group", "Billing") == ("group_id", 11)
    assert catalog.resolve("type", "refund") == ("type", "Refund")
    assert catalog.label("status", 7) == "Waiting on Third Party"
    assert catalog.named(2, "priority") == {"id": 2, "label": "Medium"}
    assert catalog.resolve_custom({"Refund reason": "duplicate charge"}) == {"cf_refund_reason": "Duplicate charge"}


def test_an_unknown_choice_returns_the_choices_and_writes_nothing(fake):
    catalog = FieldCatalog.load(make_client(fake))
    with pytest.raises(FreshdeskError) as caught:
        catalog.resolve("status", "Snoozed")
    assert caught.value.code == "choice_unknown"
    assert {row["label"] for row in caught.value.errors[0]["choices"]} >= {"Open", "Pending", "Waiting on Customer"}
    with pytest.raises(FreshdeskError):
        catalog.resolve("status", 99)


def test_closure_gaps_name_the_fields_freshdesk_requires_before_resolving(fake):
    catalog = FieldCatalog.load(make_client(fake))
    ticket = fake.add_ticket(subject="Refund", description="please")
    gaps = catalog.closure_gaps(ticket, {"status": 4})
    assert [gap["field"] for gap in gaps] == ["cf_refund_reason"]
    assert catalog.closure_gaps(ticket, {"status": 4, "custom_fields": {"cf_refund_reason": "Other"}}) == []
