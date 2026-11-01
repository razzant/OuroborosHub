import hashlib
import imaplib
import smtplib
import ssl
from email.message import EmailMessage

import pytest

from lib.client import MailClient


def test_fetch_reads_flags_and_internaldate_after_the_body_literal():
    message = EmailMessage()
    message["From"] = "sender@example.invalid"
    message["Message-ID"] = "<trailing@example.invalid>"
    message["Subject"] = "Trailing metadata"
    message.set_content("Body")
    raw = message.as_bytes()
    class Box:
        def uid(self, command, *args):
            assert command == "FETCH"
            return "OK", [(b"1 (BODY[] {" + str(len(raw)).encode() + b"}", raw),
                           b' INTERNALDATE "31-Oct-2026 12:00:00 +0000" FLAGS (\\Seen \\Flagged))']
    result = MailClient().fetch(Box(), "INBOX", 1, 7)
    assert result["flags"] == ["\\Seen", "\\Flagged"]
    assert result["internal_date"] > 1_700_000_000
    assert "Body" in result["body"]


def test_metadata_fetch_does_not_request_or_retain_message_body():
    commands = []
    class Box:
        def uid(self, command, *args):
            commands.append(args)
            assert "BODY.PEEK[HEADER]" in args[1] and "BODY.PEEK[]" not in args[1]
            raw = b"From: reader@example.invalid\r\nSubject: headers only\r\nMessage-ID: <metadata>\r\n\r\n"
            return "OK", [(b'1 (FLAGS () INTERNALDATE "31-Oct-2026 12:00:00 +0000" BODY[HEADER] {99}', raw), b")"]
    row = MailClient().fetch(Box(), "INBOX", 1, 7, headers_only=True)
    assert len(commands) == 1 and "body" not in row and "_raw_source" not in row
    assert row["message_id"] == "<metadata>"


def test_unicode_search_supports_multiple_synchronizing_literals():
    class Box:
        def __init__(self):
            self.sent = []
            self.tagged_commands = {b"T1": None}
            self.untagged_responses = {}
        def _new_tag(self): return b"T1"
        def send(self, value): self.sent.append(value)
        def _get_response(self): return None
        def _command_complete(self, command, tag):
            assert command == "UID" and tag == b"T1"
            return "OK", [b"2 7"]
        def _untagged_response(self, status, data, name):
            assert name == "SEARCH"
            return status, data
    box = Box()
    assert MailClient().uids(box, ["OR", "SUBJECT", '"счёт"', "TEXT", '"ещё"']) == [2, 7]
    assert box.sent == [b"T1 UID SEARCH CHARSET UTF-8 OR SUBJECT {8}\r\n",
                        "счёт".encode() + b" TEXT {6}\r\n", "ещё".encode() + b"\r\n"]


def test_modified_utf7_folders_round_trip_and_ascii_stays_compatible():
    for value in ("INBOX", "Архив", "工作 & личное", "Travel/📬"):
        wire = MailClient.folder_name(value)
        assert wire.isascii()
        assert MailClient.decode_folder(wire[1:-1]) == value
    assert MailClient.folder_name("A&B") == '"A&-B"'


@pytest.mark.parametrize("correct", [True, False])
def test_bridge_starttls_and_exact_pin_are_checked_before_imap_and_smtp_auth(monkeypatch, correct):
    certificate = b"synthetic DER certificate"
    fingerprint = hashlib.sha256(certificate if correct else b"another certificate").hexdigest()
    events = []
    class Sock:
        def getpeercert(self, *, binary_form):
            assert binary_form
            return certificate
    class IMAP:
        def __init__(self, host, port, timeout):
            assert (host, port) == ("127.0.0.1", 1143)
            self.sock = Sock()
        def starttls(self, *, ssl_context):
            events.append("imap_tls")
            assert ssl_context.verify_mode == ssl.CERT_NONE  # Exact pin owns trust in this explicit mode.
            return "OK", []
        def login(self, user, password): events.append("imap_auth")
        def logout(self): return "BYE", []
    class SMTP:
        def __init__(self, host, port, timeout):
            assert (host, port) == ("127.0.0.1", 1025)
            self.sock = Sock()
        def ehlo(self): events.append("ehlo")
        def starttls(self, *, context): events.append("smtp_tls")
        def login(self, user, password): events.append("smtp_auth")
        def close(self): events.append("close")
    monkeypatch.setattr(imaplib, "IMAP4", IMAP)
    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    client = MailClient({"EMAIL_USER": "fixture", "EMAIL_PASSWORD": "fixture-password", "EMAIL_IMAP_HOST": "127.0.0.1",
                         "EMAIL_SMTP_HOST": "127.0.0.1", "EMAIL_IMAP_PORT": 1143, "EMAIL_SMTP_PORT": 1025,
                         "EMAIL_IMAP_SECURITY": "starttls", "EMAIL_SMTP_SECURITY": "starttls", "EMAIL_TLS_FINGERPRINT": fingerprint})
    if correct:
        assert client.test_connection()["ok"]
        assert events.index("imap_tls") < events.index("imap_auth")
        assert events.index("smtp_tls") < events.index("smtp_auth")
    else:
        with pytest.raises(ssl.SSLCertVerificationError):
            with client.imap(): pass
        with pytest.raises(ssl.SSLCertVerificationError):
            with client.smtp(): pass
        assert "imap_auth" not in events and "smtp_auth" not in events


def test_default_tls_retains_system_certificate_and_hostname_verification():
    context = MailClient()._tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    with pytest.raises(ValueError, match="SHA-256"):
        MailClient({"EMAIL_TLS_FINGERPRINT": "not-a-pin"})._tls_context()
