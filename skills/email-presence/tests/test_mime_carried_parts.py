"""Body, body_html, signal.words and attachments share one classification of carried MIME parts.

Every fixture is real wire MIME parsed by the same parser intake uses; nothing asserts source strings.
"""
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

import pytest

from lib.facts import email_facts
from lib.mime import extract_body, parse_message
from lib.store import EmailStore

CRLF = b"\r\n"


def wire(text):
    return text.strip().replace("\n", "\r\n").encode() + CRLF


NAMED_INLINE = wire('''
From: Sender <sender@example.org>
To: bot@example.org
Subject: Monthly export
Message-ID: <named-inline@example.org>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="OUTER"

--OUTER
Content-Type: text/html; charset=utf-8

<p>Hello, the <b>report</b> is attached.</p>
--OUTER
Content-Type: text/plain; name="export.csv"
Content-Disposition: inline; filename="export.csv"

id,secretword
1,zanzibar
--OUTER
Content-Type: text/html; name="page.html"
Content-Disposition: inline; filename="page.html"

<p>Attached page mentions zanzibar</p>
--OUTER
Content-Type: text/plain; name="notes.txt"

named without any disposition: zanzibar
--OUTER--
''')

BOUNCE = wire('''
From: Mail Delivery System <MAILER-DAEMON@example.org>
To: bot@example.org
Subject: Undelivered Mail Returned to Sender
Message-ID: <bounce@example.org>
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="R"

--R
Content-Type: text/plain; charset=us-ascii

Delivery to the following recipient failed permanently.
--R
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.example.org

Final-Recipient: rfc822; gone@example.net
Action: failed
Status: 5.1.1
--R
Content-Type: message/disposition-notification

Reporting-UA: reader.example.net; Mail
Disposition: manual-action/MDN-sent-manually; displayed
--R
Content-Type: message/global

From: inner@example.net
Subject: global inner

inner global text zanzibar
--R
Content-Type: message/rfc822

From: original@example.net
Subject: original

original inner text zanzibar
--R--
''')


def parsed(raw, **options):
    return parse_message(raw, folder="INBOX", uid=1, **options)


def test_named_inline_parts_are_attachments_not_the_sender_body():
    message = parsed(NAMED_INLINE, include_attachment_data=True)
    assert message["body"] == "Hello, the report is attached."
    assert message["body_html"] == "<p>Hello, the <b>report</b> is attached.</p>"
    assert "zanzibar" not in message["body"] and "zanzibar" not in message["body_html"]
    files = {item["file_name"]: item for item in message["attachments"]}
    assert set(files) == {"export.csv", "page.html", "notes.txt"}
    # Their bytes stay useful attachments.
    assert all(item["content_available"] and item["data"] for item in files.values())
    assert files["export.csv"]["data"] == b"id,secretword\r\n1,zanzibar"


def test_a_plain_sender_body_survives_named_text_attachments():
    mail = EmailMessage()
    mail["From"], mail["To"], mail["Subject"] = "a@example.org", "bot@example.org", "Plain"
    mail.set_content("The sender's own words.")
    mail.add_attachment("attached words zanzibar", subtype="plain", filename="a.txt", disposition="inline")
    mail.add_attachment(b"<p>x</p>", maintype="text", subtype="html", filename="b.html")
    message = parsed(mail.as_bytes(policy=SMTP))
    assert message["body"] == "The sender's own words."
    assert "body_html" not in message
    assert [item["file_name"] for item in message["attachments"]] == ["a.txt", "b.html"]


def test_a_single_part_message_remains_its_own_body_even_when_named():
    raw = wire('''
From: a@example.org
To: bot@example.org
Subject: One part
Content-Type: text/plain; name="body.txt"
Content-Disposition: inline; filename="body.txt"

single part body
''')
    message = parsed(raw)
    assert message["body"] == "single part body"
    assert [item["file_name"] for item in message["attachments"]] == ["body.txt"]


def test_reports_and_attached_messages_are_unavailable_never_empty_files(tmp_path):
    message = parsed(BOUNCE, include_attachment_data=True)
    assert message["body"] == "Delivery to the following recipient failed permanently."
    assert "zanzibar" not in message["body"] and "body_html" not in message
    kinds = [item["mime_type"] for item in message["attachments"]]
    assert kinds == ["message/delivery-status", "message/disposition-notification", "message/global", "message/rfc822"]
    for item in message["attachments"]:
        assert item["content_available"] is False and item["error"] == "unsupported_mime_type"
        assert "data" not in item and "size" not in item and "sha256" not in item
    store = EmailStore(tmp_path)
    staged = store.stage_inbound_attachments(message)
    # Only the exact raw source is staged; no 0-byte "available" report file.
    assert [Path(item["path"]).read_bytes() for item in staged] == [BOUNCE]
    for item in message["attachments"]:
        assert item["content_available"] is False and item["source"] == staged[0]["path"]
    assert "full RFC822 source artifact is available" in message["attachment_stage_note"]


def test_signal_words_ignore_carried_text_but_match_the_sender_body(tmp_path):
    store = EmailStore(tmp_path)
    for uid, raw in enumerate((NAMED_INLINE, BOUNCE), 1):
        message = parse_message(raw, folder="INBOX", uid=uid, include_attachment_data=True)
        message["_raw_source"] = raw
        assert store.ingest(message)[1]
        item = store.claim_inbox()
        assert item.context["source_artifact"]["path"]
        assert "signal.words" not in email_facts(item, "bot@example.org", words=["zanzibar"])
        expected = "report" if raw is NAMED_INLINE else "permanently"
        assert "signal.words" in email_facts(item, "bot@example.org", words=[expected])
        store.complete_inbox(item.row_id, item.lease_token)


@pytest.mark.parametrize("disposition", ["attachment", "inline"])
def test_attached_message_subtree_is_not_outer_body(disposition):
    inner = EmailMessage()
    inner["From"], inner["Subject"] = "inner@example.org", "Inner"
    inner.set_content("inner body zanzibar")
    inner.add_alternative("<p>inner html zanzibar</p>", subtype="html")
    outer = EmailMessage()
    outer["From"], outer["To"], outer["Subject"] = "a@example.org", "bot@example.org", "Fwd"
    outer.set_content("See forwarded.")
    outer.add_attachment(inner, disposition=disposition)
    message = parsed(outer.as_bytes(policy=SMTP))
    assert message["body"] == "See forwarded."
    assert "body_html" not in message
    assert [item["mime_type"] for item in message["attachments"]] == ["message/rfc822"]
    assert extract_body(inner) == "inner body zanzibar"  # the attachment itself still reads on its own


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def forwarded(text="Inner words zanzibar", *, sender="vendor@example.net", subject="Your receipt", pdf=PDF):
    inner = EmailMessage()
    inner["From"], inner["To"], inner["Subject"] = sender, "a@example.org", subject
    inner.set_content(text)
    inner.add_alternative(f"<p>{text}</p>", subtype="html")
    if pdf is not None:
        inner.add_attachment(pdf, maintype="application", subtype="pdf", filename="invoice.pdf")
    return inner


def outer_mail(*attached, text="See attached."):
    outer = EmailMessage()
    outer["From"], outer["To"], outer["Subject"] = "a@example.org", "bot@example.org", "Fwd: receipt"
    outer["Message-ID"] = "<outer@example.org>"
    if text is not None:
        outer.set_content(text)
    else:
        outer.make_mixed()
    for item in attached:
        outer.attach(item) if not isinstance(item, EmailMessage) else outer.add_attachment(item)
    return outer.as_bytes(policy=SMTP)


def staged_by_name(message, tmp_path):
    store = EmailStore(tmp_path)
    staged = store.stage_inbound_attachments(message)
    return store, staged, message["attachments"]


def test_forwarded_mail_lists_its_pdf_with_provenance_and_keeps_its_text_out(tmp_path):
    raw = outer_mail(forwarded())
    message = parsed(raw, include_attachment_data=True)
    assert message["body"] == "See attached."
    assert "body_html" not in message
    kinds = [(item["mime_type"], item["file_name"]) for item in message["attachments"]]
    assert kinds == [("message/rfc822", "attachment"), ("application/pdf", "invoice.pdf")]
    container, pdf = message["attachments"]
    assert container["content_available"] is False and "within" not in container
    assert pdf["content_available"] is True and pdf["data"] == PDF
    assert container["container"] == {"mime_type": "message/rfc822", "from": "vendor@example.net", "subject": "Your receipt"}
    _, staged, descriptors = staged_by_name(message, tmp_path)
    staged_pdf = next(item for item in staged if item["filename"] == "invoice.pdf")
    assert Path(staged_pdf["path"]).read_bytes() == PDF
    assert Path(staged[-1]["path"]).read_bytes() == raw  # the exact raw EML is preserved
    assert descriptors[0]["content_available"] is False and descriptors[0]["error"] == "unsupported_mime_type"
    assert descriptors[1]["content_available"] is True and descriptors[1]["container_index"] == 0 and descriptors[0]["container"] == container["container"]
    assert descriptors[1]["sha256"] == pdf["sha256"]


def test_forwarded_mail_without_outer_text_does_not_impersonate_the_forwarder(tmp_path):
    raw = outer_mail(forwarded("I am the inner sender zanzibar"), text=None)
    message = parsed(raw, include_attachment_data=True)
    assert message["sender"] == "a@example.org"
    assert message["body"] == "" and "body_html" not in message
    pdf = message["attachments"][-1]
    assert pdf["file_name"] == "invoice.pdf" and pdf["data"] == PDF
    assert message["attachments"][pdf["container_index"]]["container"]["from"] == "vendor@example.net"
    store = EmailStore(tmp_path)
    message["_raw_source"] = raw
    assert store.ingest(message)[1]
    item = store.claim_inbox()
    assert "signal.words" not in email_facts(item, "bot@example.org", words=["zanzibar"])
    assert any(entry.get("filename") == "invoice.pdf" and entry["content_available"] for entry in item.context["attachments"])


def test_nested_forwarded_mail_keeps_every_level_of_provenance():
    middle = forwarded("middle text zanzibar", sender="middle@example.net", subject="Fwd: inner", pdf=None)
    middle.add_attachment(forwarded(sender="deep@example.net", subject="Deep receipt"))
    message = parsed(outer_mail(middle), include_attachment_data=True)
    assert message["body"] == "See attached."
    kinds = [item["mime_type"] for item in message["attachments"]]
    assert kinds == ["message/rfc822", "message/rfc822", "application/pdf"]
    items = message["attachments"]
    pdf = items[-1]
    assert pdf["data"] == PDF
    # Each file names only its nearest container; that container's own entry names the next one out.
    chain, item = [], pdf
    while "container_index" in item:
        item = items[item["container_index"]]
        chain.append(item["container"]["from"])
    assert chain == ["deep@example.net", "middle@example.net"]
    assert [entry.get("depth") for entry in items] == [None, 1, 2]
    assert items[0]["container"]["subject"] == "Fwd: inner" and items[1]["container_index"] == 0


def test_attachment_marked_multipart_lists_its_leaf_files(tmp_path):
    raw = wire('''
From: a@example.org
To: bot@example.org
Subject: Bundle
Message-ID: <bundle@example.org>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="OUTER"

--OUTER
Content-Type: text/plain; charset=utf-8

Bundle enclosed.
--OUTER
Content-Type: multipart/mixed; boundary="INNER"
Content-Disposition: attachment; filename="bundle"

--INNER
Content-Type: text/plain; charset=utf-8

bundle note zanzibar
--INNER
Content-Type: application/pdf; name="invoice.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQKJSVFT0YK
--INNER--
--OUTER--
''')
    message = parsed(raw, include_attachment_data=True)
    assert message["body"] == "Bundle enclosed." and "zanzibar" not in message["body"]
    items = message["attachments"]
    assert [(item["mime_type"], item["file_name"]) for item in items] == [
        ("multipart/mixed", "bundle"), ("text/plain", "attachment"), ("application/pdf", "invoice.pdf")]
    assert items[0]["content_available"] is False and "data" not in items[0]
    assert items[2]["data"] == b"%PDF-1.4\n%%EOF\n"
    assert items[1]["container_index"] == items[2]["container_index"] == 0
    assert items[0]["container"] == {"mime_type": "multipart/mixed", "file_name": "bundle"}
    _, staged, descriptors = staged_by_name(message, tmp_path)
    assert descriptors[0]["content_available"] is False
    assert descriptors[2]["content_available"] is True and descriptors[descriptors[2]["container_index"]]["container"]["file_name"] == "bundle"
    assert any(Path(item["path"]).read_bytes() == b"%PDF-1.4\n%%EOF\n" for item in staged)


def test_a_named_inline_receipt_is_the_body_only_when_nothing_else_is():
    receipt = wire('''
From: shop@example.org
To: bot@example.org
Subject: Receipt
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="B"

--B
Content-Type: text/html; charset=utf-8; name="receipt.html"
Content-Disposition: inline; filename="receipt.html"

<p>Total paid: 20 USD</p>
--B
Content-Type: application/pdf; name="receipt.pdf"
Content-Disposition: attachment; filename="receipt.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQKJSVFT0YK
--B--
''')
    message = parsed(receipt, include_attachment_data=True)
    assert message["body"] == "Total paid: 20 USD"
    assert message["body_html"] == "<p>Total paid: 20 USD</p>"
    assert [item["file_name"] for item in message["attachments"]] == ["receipt.html", "receipt.pdf"]
    # A blank own text part is no body; the named inline receipt still reads.
    blank_first = receipt.replace(b'--B\r\nContent-Type: text/html',
                                  b'--B\r\nContent-Type: text/plain\r\n\r\n \r\n--B\r\nContent-Type: text/html', 1)
    assert parsed(blank_first)["body"] == "Total paid: 20 USD"
    # An attachment disposition never becomes the body.
    as_attachment = receipt.replace(b"inline; filename=\"receipt.html\"", b"attachment; filename=\"receipt.html\"")
    message = parsed(as_attachment)
    assert message["body"] == "" and "body_html" not in message


def test_regular_html_body_is_kept_beside_named_plain_and_html_parts():
    mail = EmailMessage()
    mail["From"], mail["To"], mail["Subject"] = "a@example.org", "bot@example.org", "Html"
    mail.set_content("<p>Own html</p>", subtype="html")
    mail.add_attachment("named plain zanzibar", subtype="plain", filename="n.txt", disposition="inline")
    mail.add_attachment("<p>named html zanzibar</p>", subtype="html", filename="n.html", disposition="inline")
    message = parsed(mail.as_bytes(policy=SMTP))
    assert message["body"] == "Own html"
    assert message["body_html"].strip() == "<p>Own html</p>"
    assert [item["file_name"] for item in message["attachments"]] == ["n.txt", "n.html"]


def test_report_returning_the_original_lists_its_files_and_keeps_report_parts_unavailable(tmp_path):
    original = forwarded("original text zanzibar", sender="bot@example.org", subject="Sent invoice")
    raw = BOUNCE.replace(b"\r\n--R--", b"\r\n--R\r\nContent-Type: message/rfc822\r\n\r\n"
                         + original.as_bytes(policy=SMTP) + b"\r\n--R--")
    message = parsed(raw, include_attachment_data=True)
    assert message["body"] == "Delivery to the following recipient failed permanently."
    kinds = [item["mime_type"] for item in message["attachments"]]
    assert kinds == ["message/delivery-status", "message/disposition-notification", "message/global",
                     "message/rfc822", "message/rfc822", "application/pdf"]
    for item in message["attachments"][:5]:
        assert item["content_available"] is False and "data" not in item and "size" not in item
    assert message["attachments"][-1]["data"] == PDF
    assert message["attachments"][message["attachments"][-1]["container_index"]]["container"]["subject"] == "Sent invoice"
    _, staged, descriptors = staged_by_name(message, tmp_path)
    assert [item["content_available"] for item in descriptors] == [False] * 5 + [True]
    assert all(Path(item["path"]).stat().st_size for item in staged)  # never a 0-byte "available" file


def blank_then(first, receipt_type, receipt):
    return wire(f'''
From: shop@example.org
To: bot@example.org
Subject: Receipt
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="B"

--B
{first}
--B
Content-Type: {receipt_type}; charset=utf-8; name="receipt"
Content-Disposition: inline; filename="receipt"

{receipt}
--B--
''')


@pytest.mark.parametrize("first", [
    'Content-Type: text/plain; charset=utf-8\n\n',                       # an empty ordinary plain part
    'Content-Type: text/plain; charset=utf-8\n\n   ',
    'Content-Type: text/html; charset=utf-8\n\n<div> <br> </div>',        # markup with no readable text
])
@pytest.mark.parametrize("receipt_type,receipt", [("text/plain", "Total paid: 20 USD"),
                                                  ("text/html", "<p>Total paid: 20 USD</p>")])
def test_a_blank_first_part_never_hides_the_named_inline_receipt(tmp_path, first, receipt_type, receipt):
    message = parsed(blank_then(first, receipt_type, receipt), include_attachment_data=True)
    assert message["body"] == "Total paid: 20 USD"
    assert [item["file_name"] for item in message["attachments"]] == ["receipt"]  # still listed as a file
    store = EmailStore(tmp_path)
    assert store.ingest(message)[1]
    item = store.claim_inbox()
    assert "signal.words" in email_facts(item, "bot@example.org", words=["paid"])


def test_own_readable_text_still_wins_over_a_named_inline_receipt():
    raw = blank_then('Content-Type: text/plain; charset=utf-8\n\nMy own note', "text/plain", "Total paid: 20 USD")
    assert parsed(raw)["body"] == "My own note"
    alternative = wire('''
From: shop@example.org
To: bot@example.org
Subject: Receipt
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="A"

--A
Content-Type: text/plain; charset=utf-8


--A
Content-Type: text/html; charset=utf-8

<p>Own html text</p>
--A--
''')
    assert parsed(alternative)["body"] == "Own html text"


def nested(levels, subject_chars):
    inner = forwarded(sender="level%d@example.net" % levels, subject="S" * subject_chars)
    for level in range(levels - 1, 0, -1):
        outer = EmailMessage()
        outer["From"], outer["To"], outer["Subject"] = "level%d@example.net" % level, "a@example.org", "S" * subject_chars
        outer.set_content("level %d" % level)
        outer.add_attachment(inner)
        inner = outer
    return outer_mail(inner)


def test_deep_nesting_keeps_every_file_and_grows_linearly(tmp_path):
    levels = 80
    raw = nested(levels, 2000)
    message = parsed(raw, include_attachment_data=True)
    items = message["attachments"]
    import json
    size = len(json.dumps([{k: v for k, v in item.items() if k != "data"} for item in items]))
    # One nearest container per file: metadata is proportional to the message, not to depth squared.
    assert size < 3 * len(raw), (size, len(raw))
    assert [item["mime_type"] for item in items] == ["message/rfc822"] * levels + ["application/pdf"]
    pdf = items[-1]
    assert pdf["data"] == PDF and pdf["depth"] == levels
    chain, item = [], pdf
    while "container_index" in item:
        item = items[item["container_index"]]
        chain.append(item["container"]["from"])
    assert chain == ["level%d@example.net" % level for level in range(levels, 0, -1)]
    positions = [item.get("mime_part") for item in items[1:]]
    assert positions == sorted(positions) and len(set(positions)) == len(positions)
    _store, staged, descriptors = staged_by_name(message, tmp_path)
    assert Path(next(entry for entry in staged if entry["filename"] == "invoice.pdf")["path"]).read_bytes() == PDF
    assert Path(staged[-1]["path"]).read_bytes() == raw  # the complete raw EML
    assert descriptors[-1]["container_index"] == pdf["container_index"] and descriptors[-1]["depth"] == levels
    assert _store.ingest(message)[1]
    retained = _store.claim_inbox()
    assert list(retained.attachments) == descriptors == retained.context['attachments']
    chain, item = [], retained.attachments[-1]
    while 'container_index' in item:
        item = retained.attachments[item['container_index']]
        chain.append(item['container']['from'])
    assert chain == ['level%d@example.net' % level for level in range(levels, 0, -1)]


def test_wide_long_header_container_is_retained_once_after_ingest(tmp_path):
    import json
    import sqlite3
    inner = forwarded(subject='S' * 16000, pdf=None)
    for index in range(160):
        inner.add_attachment(PDF, maintype='application', subtype='pdf', filename=f'invoice-{index}.pdf')
    raw = outer_mail(inner)
    message = parsed(raw, include_attachment_data=True)
    store = EmailStore(tmp_path)
    assert store.ingest(message)[1]
    retained = store.claim_inbox()
    descriptors = retained.context['attachments']
    assert len(descriptors) == 161
    assert descriptors[0]['container']['subject'] == 'S' * 16000
    assert all(item['container_index'] == 0 and 'within' not in item and 'container' not in item
               for item in descriptors[1:])
    # Exercise the actual persisted JSON, including source locations added by staging.
    with sqlite3.connect(store.path) as db:
        row = db.execute('SELECT context_json, attachments_json FROM inbox').fetchone()
    assert all(len(value.encode()) < 4 * len(raw) for value in row), (list(map(len, row)), len(raw))
    files = retained.staged_files
    assert len(files) == 161
    assert all(Path(item['path']).read_bytes() == PDF for item in files if item['filename'].endswith('.pdf'))
    assert Path(retained.context['source_artifact']['path']).read_bytes() == raw
    assert retained.body == 'See attached.'
