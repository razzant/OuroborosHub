"""IMAP LIST with literal mailbox names, through real imaplib over a socketpair.

imaplib returns a literal name as a (header, bytes) tuple followed by a separate trailer item: b"" or the
rest of the line, such as extended LIST data. Folder listing and the CREATE parent lookup share one parser.
"""
import imaplib
import re
import socket
import threading
from types import SimpleNamespace

import pytest

from lib.client import MailClient

# Untagged LIST answers by command; literals are written exactly as a server sends them.
LISTING = [
    b'* LIST (\\HasNoChildren) "/" "INBOX"',
    b'* LIST (\\HasChildren) "/" {15}\r\nProjects "2026"',
    b'* LIST (\\HasNoChildren) "/" {13}\r\nProjects/Q1 x ("CHILDINFO" ("SUBSCRIBED"))',
    b'* LIST (\\HasNoChildren \\Sent) "/" ' + MailClient.folder_name("Отправленные").encode(),
    b'* LIST (\\Noselect) NIL Shared',
    b'* LIST (\\HasNoChildren) "/" {7}\r\nA\\B "q"',
]


class Server:
    def __init__(self, answers):
        self.answers, self.commands = answers, []

    def serve(self, sock):
        sock.settimeout(10)
        reader = sock.makefile("rb")
        try:
            sock.sendall(b"* OK fixture ready\r\n")
            while line := reader.readline():
                tag, _, command = line.rstrip(b"\r\n").partition(b" ")
                self.commands.append(command)
                verb = command.split(b" ", 1)[0].upper()
                if verb == b"CAPABILITY":
                    sock.sendall(b"* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n")
                elif verb == b"LIST":
                    for answer in self.answers(command):
                        sock.sendall(answer + b"\r\n")
                elif verb == b"LOGOUT":
                    sock.sendall(b"* BYE\r\n" + tag + b" OK LOGOUT\r\n")
                    return
                sock.sendall(tag + b" OK done\r\n")
        finally:
            sock.close()


class WireIMAP(imaplib.IMAP4):
    def __init__(self, server, host, port):
        self.server = server
        super().__init__(host, port, timeout=10)

    def open(self, host="", port=imaplib.IMAP4_PORT, timeout=None):
        self.host, self.port = host, port
        self.sock, theirs = socket.socketpair()
        self.sock.settimeout(timeout)
        threading.Thread(target=self.server.serve, args=(theirs,), daemon=True).start()
        self.file = self.sock.makefile("rb")


@pytest.fixture
def wired(monkeypatch):
    def connect(answers):
        server = Server(answers)
        monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host, port, *, ssl_context=None, timeout=None:
                            WireIMAP(server, host, port))
        client = MailClient({"EMAIL_IMAP_HOST": "imap.invalid", "EMAIL_USER": "reader@example.invalid",
                             "EMAIL_PASSWORD": "fixture-secret"})
        return SimpleNamespace(server=server, client=client)
    return connect


def test_folder_list_reads_literal_names_trailers_and_ordinary_names(wired):
    mail = wired(lambda _command: LISTING)
    folders = mail.client.mailbox(action="list")["folders"]
    assert folders == [
        r'(\HasNoChildren) "/" "INBOX"',
        r'(\HasChildren) "/" "Projects \"2026\""',
        r'(\HasNoChildren) "/" "Projects/Q1 x"',
        r'(\HasNoChildren \Sent) "/" "Отправленные"',
        r'(\Noselect) NIL Shared',
        r'(\HasNoChildren) "/" "A\\B \"q\""',
    ]


def test_an_empty_listing_and_a_garbled_line_do_not_crash_listing(wired):
    mail = wired(lambda _command: [b'* LIST garbled-without-flags'])
    assert mail.client.mailbox(action="list")["folders"] == ["garbled-without-flags"]
    mail = wired(lambda _command: [])
    assert mail.client.mailbox(action="list")["folders"] == []


def test_create_parent_lookup_uses_the_same_literal_parser(wired):
    def answers(command):
        if command.endswith(b'"Projects \\"2026\\"/Q2/Notes" ""'):
            return [b'* LIST (\\Noselect) "/" ""']  # hierarchy delimiter probe
        if b'"Projects \\"2026\\"/Q2"' in command:
            return []  # missing intermediate folder
        if b'"Projects \\"2026\\""' in command:
            return [b'* LIST (\\HasChildren) "/" {15}\r\nProjects "2026" ("CHILDINFO" ("SUBSCRIBED"))']
        raise AssertionError(command)
    mail = wired(answers)
    prepared = []
    result = mail.client.mailbox(action="create", destination='Projects "2026"/Q2/Notes',
                                 prepare_targets=lambda targets: prepared.append(targets) or {"status": "needs_arguments"})
    assert result == {"status": "needs_arguments"}
    assert prepared == [{"destination": 'Projects "2026"/Q2/Notes',
                         "folder_names": ['Projects "2026"', 'Projects "2026"/Q2']}]
    assert not any(command.upper().startswith(b"CREATE") for command in mail.server.commands)
