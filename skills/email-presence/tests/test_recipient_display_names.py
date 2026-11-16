"""Bare envelope targets reach the Host; display names reach the headers through the non-target display_names.

display_names is header text for recipients already listed in to/cc/bcc. It never adds a destination, the
Host projects only the bare addresses, and replays of a queued request keep the accepted header names.
"""
import json
from email import message_from_bytes
from email.policy import SMTP, default

import pytest

from lib.client import MailClient
from lib.mime import MailAddressError, named_mailboxes, named_recipient_groups
from lib.service import MailService
from test_conversation_preparation import context
from test_delivery_custody_contracts import service_and_store
from test_reply_all_envelope import registered
from test_write_targets import published_targets

pytest_plugins = ["test_binding_core"]

SOURCE, ROOT = "<named-source@local.invalid>", "<named-root@local.invalid>"


def wire_message(store, request_id, address="owner@example.invalid"):
    item = store.queued_message(request_id)
    return item, MailClient({"EMAIL_USER": address}).message(item)


def test_named_input_prepares_bare_targets_and_names_then_queues_named_headers(tmp_path):
    account, service, store = service_and_store(tmp_path)
    call = registered(service, "email_send")
    prepared = call(dict(account_id=account["id"], to='"Doe, John" <john@example.invalid>, Ann <ann@example.invalid>',
                         cc=['"Ли, Сью" <sue@example.invalid>'], body="Hi", request_id="names"))
    retry = prepared["retry_arguments"]
    assert retry["to"] == ["john@example.invalid", "ann@example.invalid"] and retry["cc"] == ["sue@example.invalid"]
    assert retry["display_names"] == {"john@example.invalid": "Doe, John", "ann@example.invalid": "Ann",
                                      "sue@example.invalid": "Ли, Сью"}
    assert call(retry)["state"] == "pending"
    item, message = wire_message(store, "names")
    assert item.envelope_recipients == ["john@example.invalid", "ann@example.invalid", "sue@example.invalid"]
    assert [(a.display_name, a.addr_spec) for a in message["To"].addresses] == [
        ("Doe, John", "john@example.invalid"), ("Ann", "ann@example.invalid")]
    assert [(a.display_name, a.addr_spec) for a in message["Cc"].addresses] == [("Ли, Сью", "sue@example.invalid")]


def test_host_projects_only_bare_addresses_never_display_names(tmp_path):
    account, service, _store = service_and_store(tmp_path)
    retry = service.send(account_id=account["id"], to='"Doe, John" <john@example.invalid>', body="Hi",
                         request_id="projection")["retry_arguments"]
    projected = published_targets("email_send", retry)
    values = {value for group in projected.values() for value in group}
    assert "john@example.invalid" in values and not any("Doe" in value for value in values)


@pytest.mark.parametrize("names,error", [
    ({"stranger@example.invalid": "Added"}, "only name a recipient"),
    ({"john@example.invalid": "Line\r\nBcc: x@example.invalid"}, "one line"),
    ({"john@example.invalid": ""}, "one line"),
    (["john@example.invalid"], "must be an object"),
])
def test_display_names_cannot_add_recipients_or_header_lines(tmp_path, names, error):
    account, service, store = service_and_store(tmp_path)
    with pytest.raises(ValueError, match=error):
        service.send(account_id=account["id"], to="john@example.invalid", body="Hi", request_id="bad", display_names=names)
    assert store.receipt("bad") is None


def test_explicit_names_override_and_bare_calls_stay_one_step(tmp_path):
    account, service, store = service_and_store(tmp_path)
    plain = service.send(account_id=account["id"], to="john@example.invalid", body="Hi", request_id="plain")
    assert plain["state"] == "pending"  # an ordinary bare send still queues in one call
    named = service.send(account_id=account["id"], to="john@example.invalid", body="Hi", request_id="named",
                         display_names={"JOHN@example.invalid": "Johnny"})
    assert named["state"] == "pending"  # canonical arguments with explicit names also queue in one call
    _item, message = wire_message(store, "named")
    assert str(message["To"]) == "Johnny <john@example.invalid>"
    conflict = service.send(account_id=account["id"], to="john@example.invalid", body="Hi", request_id="named",
                            display_names={"john@example.invalid": "Someone else"})
    assert conflict["code"] == "request_id_conflict"
    assert service.send(account_id=account["id"], to="john@example.invalid", body="Hi", request_id="named")["deduplicated"]


@pytest.mark.parametrize("kind", ["send", "draft"])
def test_reply_all_keeps_source_display_names_and_never_copies_bcc(tmp_path, kind):
    source = {"message_id": SOURCE, "references": [ROOT], "sender": "boss@example.invalid", "sender_name": "Boss, The",
              "recipients": ["owner@example.invalid", "peer@example.invalid"], "cc": ["copy@example.invalid"],
              "headers": {"from": ['"Boss, The" <boss@example.invalid>'],
                          "to": ["Owner <owner@example.invalid>, \"Peer, A\" <peer@example.invalid>"],
                          "cc": ["Копия <copy@example.invalid>"]},
              "bcc": ["hidden@example.invalid"]}
    account, service, store, calls = context(tmp_path, source)
    call = registered(service, "email_" + kind)
    prepared = call(dict(account_id=account["id"], to="", subject="Re", body="Answer", reply_to_message_id=SOURCE,
                         reply_all=True, **({"request_id": "reply"} if kind == "send" else {})))
    retry = prepared["retry_arguments"]
    assert retry["to"] == "boss@example.invalid" and retry["cc"] == ["peer@example.invalid", "copy@example.invalid"]
    assert retry["display_names"] == {"boss@example.invalid": "Boss, The", "peer@example.invalid": "Peer, A",
                                      "copy@example.invalid": "Копия"}
    assert "hidden@example.invalid" not in json.dumps(retry)
    done = call(retry)
    assert done["ok"]
    if kind == "send":
        _item, message = wire_message(store, "reply")
        to, cc = str(message["To"]), str(message["Cc"])
        # An exact repeat of the original Reply All call replays the queued item.
        repeat = call(dict(account_id=account["id"], to="", subject="Re", body="Answer", reply_to_message_id=SOURCE,
                           reply_all=True, request_id="reply", reply_all_fingerprint=retry["reply_all_fingerprint"]))
        assert repeat["deduplicated"]
    else:
        saved = calls[-1][1]
        to, cc = saved["to"], ", ".join(saved["cc"])
    assert to == '"Boss, The" <boss@example.invalid>'
    assert cc == '"Peer, A" <peer@example.invalid>, Копия <copy@example.invalid>'


def test_rows_queued_before_display_names_still_replay(tmp_path):
    """A 0.10.10 row holds bare spellings; an older row may hold named ones with a named-group fingerprint."""
    account, service, store = service_and_store(tmp_path)
    store.enqueue_outbox(request_id="legacy-bare", to=["john@example.invalid"], body="Hi",
                         reporting={"version": -1, "account_id": account["id"], "sender": account["address"]})
    same = service.send(account_id=account["id"], to="john@example.invalid", body="Hi", request_id="legacy-bare")
    assert same["deduplicated"]
    store.enqueue_outbox(request_id="legacy-named", to=['"Doe, John" <john@example.invalid>'], body="Hi",
                         reporting={"version": -1, "account_id": account["id"], "sender": account["address"]})
    for to in ("john@example.invalid", '"Doe, John" <john@example.invalid>'):
        replay = service.send(account_id=account["id"], to=to, body="Hi", request_id="legacy-named")
        assert replay["deduplicated"], replay
    item, message = wire_message(store, "legacy-named")
    assert str(message["To"]) == '"Doe, John" <john@example.invalid>'


@pytest.mark.parametrize("spelling", ["bare", "named"])
def test_reply_all_rows_fingerprinted_before_display_names_still_replay(tmp_path, spelling):
    account, service, store, _calls = context(tmp_path, None)
    to = ['"Doe, John" <john@example.invalid>'] if spelling == "named" else ["john@example.invalid"]
    stored = store.enqueue_outbox(request_id="old-reply", to=to, body="Answer", subject="Re", in_reply_to=SOURCE,
                                  references=[ROOT], reporting={"version": -1, "account_id": account["id"],
                                                                "sender": account["address"]})
    assert stored
    item = store.queued_message("old-reply")
    fingerprint = MailService._reply_fingerprint(account["id"], "old-reply", SOURCE, ([], [], []), (item.to, item.cc, item.bcc))
    with store._connect() as db:
        db.execute("UPDATE outbox SET reporting_json=? WHERE request_id='old-reply'",
                   (json.dumps({**item.reporting, "reply_all_fingerprint": fingerprint}),))
    replay = service.send(account_id=account["id"], to="", body="Answer", subject="Re", reply_to_message_id=SOURCE,
                          references=[ROOT], reply_all=True, request_id="old-reply")
    assert replay.get("deduplicated"), replay


def test_a_damaged_source_header_only_loses_its_names(tmp_path):
    source = {"message_id": SOURCE, "references": [ROOT], "sender": "boss@example.invalid",
              "recipients": ["owner@example.invalid", "peer@example.invalid"],
              "headers": {"to": ['broken <<<, "Peer, A" <peer@example.invalid'], "from": ["boss@example.invalid"]}}
    account, service, _store, _calls = context(tmp_path, source)
    retry = service.send(account_id=account["id"], to="", body="Answer", reply_to_message_id=SOURCE, reply_all=True,
                         request_id="damaged")["retry_arguments"]
    assert retry["to"] == "boss@example.invalid" and retry["cc"] == ["peer@example.invalid"]
    assert "display_names" not in retry
    assert service.send(**retry)["state"] == "pending"


def test_mixed_case_canonical_address_keeps_its_name(tmp_path):
    account, service, store = service_and_store(tmp_path)
    retry = service.send(account_id=account["id"], to='"Doe, John" <John.Doe@Example.invalid>', body="Hi",
                         request_id="case")["retry_arguments"]
    assert retry["to"] == "John.Doe@Example.invalid" and retry["display_names"] == {"John.Doe@Example.invalid": "Doe, John"}
    assert service.send(**retry)["state"] == "pending"
    _item, message = wire_message(store, "case")
    assert str(message["To"]) == '"Doe, John" <John.Doe@Example.invalid>'


ENCODED_EVIL = "=?utf-8?b?ZXZpbEBleGFtcGxlLmludmFsaWQ=?="  # RFC 2047 spelling of evil@example.invalid


def test_the_final_header_check_refuses_a_name_that_would_become_a_recipient():
    # The serializer decodes this text into a bare address: the written header would deliver elsewhere.
    with pytest.raises(MailAddressError, match="change the recipients"):
        named_recipient_groups((["john@example.invalid"], [], []), {"john@example.invalid": ENCODED_EVIL})
    with pytest.raises(MailAddressError, match="cannot be written"):
        named_mailboxes(["not an address"], {"not an address": "Name"})
    assert named_recipient_groups((["john@example.invalid"], [], ["b@example.invalid"]), {}) == (
        ["john@example.invalid"], [], ["b@example.invalid"])


@pytest.mark.parametrize("kind", ["send", "draft"])
@pytest.mark.parametrize("name,error", [(ENCODED_EVIL, "read back unchanged"), ("Line Bcc: x", "one line"),
                                        ("x\x85y", "one line")])
def test_unrepresentable_explicit_names_are_refused_before_any_effect(tmp_path, kind, name, error):
    account, service, store, calls = context(tmp_path, None)
    extra = {"request_id": "bad"} if kind == "send" else {"subject": "S", "folder": "Drafts"}
    with pytest.raises(ValueError, match=error):
        getattr(service, kind)(account_id=account["id"], to="john@example.invalid", body="Hi",
                               display_names={"john@example.invalid": name}, **extra)
    assert store.receipt("bad") is None and not [call for call in calls if call[0] == "draft"]


NAMES = ['x <evil@example.invalid>, y', 'Team: a@example.invalid, b@example.invalid;', '"evil@example.invalid"',
         'Ли, Сью \U0001F600', 'quote " back \\ slash', '(comment) name', "Long " * 240]


@pytest.mark.parametrize("name", NAMES)
def test_header_like_and_long_names_stay_text_and_never_change_the_envelope(tmp_path, name):
    account, service, store = service_and_store(tmp_path)
    result = service.send(account_id=account["id"], to="john@example.invalid", cc=["ann@example.invalid"], body="Hi",
                          request_id="odd", display_names={"john@example.invalid": name})
    assert result["state"] == "pending", result
    item, message = wire_message(store, "odd")
    assert item.envelope_recipients == ["john@example.invalid", "ann@example.invalid"]
    wire = message_from_bytes(message.as_bytes(policy=SMTP), policy=default)
    assert [(a.display_name, a.addr_spec) for a in wire["To"].addresses] == [(" ".join(name.split()), "john@example.invalid")]
    assert [a.addr_spec for a in wire["Cc"].addresses] == ["ann@example.invalid"]


def test_a_source_name_a_header_cannot_carry_is_left_out_not_sent(tmp_path):
    source = {"message_id": SOURCE, "references": [ROOT], "sender": "boss@example.invalid",
              "sender_name": "Boss Bcc: hidden@example.invalid", "recipients": ["owner@example.invalid"],
              "headers": {"from": ["boss@example.invalid"]}}
    account, service, store, _calls = context(tmp_path, source)
    retry = service.send(account_id=account["id"], to="", body="Answer", reply_to_message_id=SOURCE, reply_all=True,
                         request_id="srcname")["retry_arguments"]
    assert "display_names" not in retry
    assert service.send(**retry)["state"] == "pending"
    item, _message = wire_message(store, "srcname")
    assert item.envelope_recipients == ["boss@example.invalid"]


def test_a_draft_keeps_bcc_display_names_in_its_saved_header(tmp_path):
    account, service, _store, calls = context(tmp_path, None)
    result = service.draft(account_id=account["id"], to="john@example.invalid", bcc=["hidden@example.invalid"],
                           body="Hi", subject="S", folder="Drafts",
                           display_names={"hidden@example.invalid": "Hidden, Person"})
    assert result["ok"], result
    saved = calls[-1][1]
    assert saved["to"] == "john@example.invalid" and saved["bcc"] == ['"Hidden, Person" <hidden@example.invalid>']


ENCODED_SOURCE = (
    "From: =?utf-8?q?Boss=2C_The?= <boss@example.invalid>\r\n"
    "To: owner@example.invalid, =?utf-8?q?Peer=2C_A?= <peer@example.invalid>\r\n"
    "Cc: =?utf-8?b?0JjQstCw0L3QvtCyLCDQmNCy0LDQvQ==?= <copy@example.invalid>\r\n"
    f"Subject: Plan\r\nMessage-ID: {SOURCE}\r\nReferences: {ROOT}\r\n\r\nBody\r\n").encode()
ENCODED_NAMES = {"boss@example.invalid": "Boss, The", "peer@example.invalid": "Peer, A", "copy@example.invalid": "Иванов, Иван"}


@pytest.mark.parametrize("legacy", [False, True])
def test_encoded_names_with_commas_survive_parse_stored_read_reply_all_and_mime(tmp_path, legacy):
    from lib.mime import parse_message
    source = parse_message(ENCODED_SOURCE, folder="INBOX", uid=1)
    assert source["address_names"] == ENCODED_NAMES and source["sender_name"] == "Boss, The"
    if legacy:  # a read stored by an earlier version has only the decoded header text
        source.pop("address_names")
    account, service, store, calls = context(tmp_path, source)
    call = registered(service, "email_send")
    args = dict(account_id=account["id"], to="", subject="Re: Plan", body="Answer", reply_to_message_id=SOURCE,
                reply_all=True, request_id="encoded")
    retry = call(args)["retry_arguments"]
    assert calls == [("read", SOURCE)]
    stored = store.message_by_id(SOURCE)  # every later step reads this stored copy, not the provider
    assert ("address_names" in stored) is not legacy
    # Bare envelope targets are the same either way; only the names differ.
    assert retry["to"] == "boss@example.invalid" and retry["cc"] == ["peer@example.invalid", "copy@example.invalid"]
    names = retry.get("display_names", {})
    if legacy:  # the decoded text cannot separate "Peer, A"; that name is left out, never misparsed into a target
        assert set(names) <= set(ENCODED_NAMES) and all(names[key] == ENCODED_NAMES[key] for key in names)
    else:
        assert names == ENCODED_NAMES
    assert call(retry)["state"] == "pending"
    item, message = wire_message(store, "encoded")
    assert item.envelope_recipients == ["boss@example.invalid", "peer@example.invalid", "copy@example.invalid"]
    sent = {a.addr_spec: a.display_name for header in ("To", "Cc") for a in message[header].addresses}
    assert sent == {key: names.get(key, "") for key in ENCODED_NAMES}
    repeat = call({**args, "reply_all_fingerprint": retry["reply_all_fingerprint"]})
    assert repeat["deduplicated"] and [entry for entry in calls if entry[0] == "read"] == [("read", SOURCE)]


def test_captured_mail_keeps_structured_names_and_8bit_names_store_as_text(tmp_path):
    from lib.mime import parse_message
    from lib.store import EmailStore
    raw = ENCODED_SOURCE.replace(b"To: owner@example.invalid,", b'To: "Jos\xe9 A" <owner@example.invalid>,')
    store = EmailStore(tmp_path)
    assert store.ingest(parse_message(raw, folder="INBOX", uid=1))[1]
    names = store.message_by_id(SOURCE)["address_names"]
    assert {key: names[key] for key in ENCODED_NAMES} == ENCODED_NAMES
    assert names["owner@example.invalid"] == "Jos\ufffd A"  # undecodable octets read as text, as the headers do
    json.dumps(names).encode("utf-8")  # no surrogate escapes reach storage
