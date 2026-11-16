"""IMAP search pages against a small in-process IMAP4rev1 server.

The service, MailClient, imaplib and its UTF-8 literal exchange run unchanged.
Only IMAP4_SSL's socket is replaced, by a socketpair to this fixture; nothing
reaches a network or a real mailbox.
"""
import base64
import imaplib
import json
import random
import re
import socket
import threading
import time
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formatdate
from types import SimpleNamespace

import pytest

from lib import ews
from lib import service as service_module
from lib.accounts import AccountStore
from lib.client import MailClient
from lib.service import MailService
from test_accounts import gmail_account


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
OPEN, CLOSE = object(), object()
TOKEN = re.compile(rb'\s*(?:"((?:\\.|[^"\\])*)"|(\()|(\))|([^\s()"]+))')
RECEIPTS = ["SUBJECT", '"Your receipt"']


class Bad(Exception):
    """A command the fixture answers with a tagged BAD, as a server does."""


class Literal(bytes):
    """A synchronizing literal received from the client."""


def imap_date(timestamp):
    moment = time.gmtime(timestamp)
    return "%02d-%s-%04d %02d:%02d:%02d +0000" % (moment.tm_mday, MONTHS[moment.tm_mon - 1], moment.tm_year,
                                                  moment.tm_hour, moment.tm_min, moment.tm_sec)


def tokens(chunks):
    words = []
    for chunk in chunks:
        if isinstance(chunk, Literal):
            words.append(bytes(chunk).decode("utf-8"))
            continue
        position = 0
        while (match := TOKEN.match(chunk, position)) and match.end() > position:
            position = match.end()
            quoted, opened, closed, atom = match.groups()
            if quoted is not None:
                words.append(re.sub(rb'\\(.)', rb'\1', quoted).decode("utf-8"))
            else:
                words.append(OPEN if opened else CLOSE if closed else atom.decode("utf-8"))
        if chunk[position:].strip():
            raise Bad("unparsable command")
    return words


class Folder:
    def __init__(self, validity):
        self.validity, self.next_uid, self.mail = validity, 1, {}

    def add(self, raw, arrived, uid=None):
        uid = self.next_uid if uid is None else uid
        assert uid >= self.next_uid, "UIDs only grow within one UIDVALIDITY"
        self.mail[uid] = (raw, arrived)
        self.next_uid = uid + 1
        return uid


class Server:
    """IMAP4rev1 subset: CAPABILITY, LOGIN, SELECT/EXAMINE, UID SEARCH, UID FETCH, LOGOUT."""

    def __init__(self):
        self.folders = {"INBOX": Folder(7)}
        self.log = []  # (selected folder, command without its tag), literals inline
        self.lock = threading.Lock()
        self.views = {}  # (raw, arrival) -> searchable fields; parsing once keeps the fixture cheap

    def view(self, raw, arrived):
        if (raw, arrived) not in self.views:
            message = message_from_bytes(raw, policy=policy.default)
            self.views[raw, arrived] = {
                "SUBJECT": str(message["Subject"] or "").casefold(), "FROM": str(message["From"] or "").casefold(),
                "HEADER": {name.casefold(): str(value).casefold() for name, value in message.items()},
                "DAY": time.gmtime(arrived)[:3]}
        return self.views[raw, arrived]

    def commands(self, since=0):
        return [command for _, command in self.log[since:]]

    def serve(self, sock):
        sock.settimeout(10)
        reader = sock.makefile("rb")
        selected = None

        def send(*lines):
            sock.sendall(b"".join(line + b"\r\n" for line in lines))
        try:
            send(b"* OK fixture IMAP4rev1 ready")
            while line := reader.readline():
                chunks, wire = [], b""
                while literal := re.search(rb"\{(\d+)\}\r\n\Z", line):
                    sock.sendall(b"+ Ready for literal\r\n")
                    data = reader.read(int(literal.group(1)))
                    chunks += [line[:literal.start()], Literal(data)]
                    wire += line[:literal.end() - 2] + data
                    line = reader.readline()
                chunks.append(line.rstrip(b"\r\n"))
                wire += line.rstrip(b"\r\n")
                with self.lock:
                    words = tokens(chunks)
                    tag, name, args = words[0].encode(), words[1].upper(), words[2:]
                    self.log.append((selected, wire.decode("utf-8").split(" ", 1)[1]))
                    try:
                        if name == "CAPABILITY":
                            send(b"* CAPABILITY IMAP4rev1 MOVE", tag + b" OK CAPABILITY completed")
                        elif name == "LOGIN":
                            send(tag + b" OK LOGIN completed")
                        elif name in ("SELECT", "EXAMINE"):
                            mailbox = MailClient.decode_folder(args[0])
                            mailbox = "INBOX" if mailbox.isascii() and mailbox.upper() == "INBOX" else mailbox
                            folder = self.folders.get(mailbox)
                            selected = mailbox if folder else None
                            if folder is None:
                                send(tag + b" NO [NONEXISTENT] No such mailbox")
                            else:
                                send(b"* %d EXISTS" % len(folder.mail), b"* 0 RECENT",
                                     b"* OK [UIDVALIDITY %d] UIDs valid" % folder.validity,
                                     b"* OK [UIDNEXT %d] Predicted next UID" % folder.next_uid,
                                     tag + (b" OK [READ-ONLY] EXAMINE completed" if name == "EXAMINE"
                                            else b" OK [READ-WRITE] SELECT completed"))
                        elif name == "UID" and selected and args and str(args[0]).upper() == "SEARCH":
                            found = self.search(self.folders[selected], args[1:])
                            send(b"* SEARCH" + b"".join(b" %d" % uid for uid in found), tag + b" OK SEARCH completed")
                        elif name == "UID" and selected and args and str(args[0]).upper() == "FETCH":
                            self.fetch(sock, self.folders[selected], args[1], [str(x).upper() for x in args[2:]])
                            send(tag + b" OK FETCH completed")
                        elif name == "LOGOUT":
                            send(b"* BYE fixture closing", tag + b" OK LOGOUT completed")
                            return
                        else:
                            raise Bad("unsupported command")
                    except Bad as exc:
                        send(tag + b" BAD " + str(exc).encode())
        finally:
            reader.close()
            sock.close()

    @staticmethod
    def fetch(sock, folder, uid_set, items):
        section = "HEADER" if "BODY.PEEK[HEADER]" in items else "" if "BODY.PEEK[]" in items else None
        if section is None or "INTERNALDATE" not in items:
            raise Bad("fixture fetches BODY.PEEK[] or BODY.PEEK[HEADER] with INTERNALDATE")
        wanted = set()
        for part in uid_set.split(","):
            low, _, high = part.partition(":")
            wanted.update(range(int(low), int(high or low) + 1))
        for sequence, uid in enumerate(sorted(folder.mail), 1):
            if uid in wanted:
                raw, arrived = folder.mail[uid]
                data = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n" if section else raw
                sock.sendall(b'* %d FETCH (UID %d INTERNALDATE "%s" FLAGS () BODY[%s] {%d}\r\n' % (
                    sequence, uid, imap_date(arrived).encode(), section.encode(), len(data)) + data + b")\r\n")

    def search(self, folder, args):
        if args and isinstance(args[0], str) and args[0].upper() == "CHARSET":
            if len(args) < 2 or str(args[1]).upper() not in ("UTF-8", "US-ASCII"):
                raise Bad("unsupported charset")
            args = args[2:]
        position = 0

        def take():
            nonlocal position
            if position >= len(args):
                raise Bad("missing search argument")
            position += 1
            return args[position - 1]

        def text():
            value = take()
            if not isinstance(value, str):
                raise Bad("expected a string")
            return value.casefold()

        def key():
            token = take()
            if token is OPEN:
                group = []
                while position < len(args) and args[position] is not CLOSE:
                    group.append(key())
                take()
                return lambda view: all(test(view) for test in group)
            if not isinstance(token, str):
                raise Bad("unexpected parenthesis")
            word = token.upper()
            if word == "ALL":
                return lambda view: True
            if word in ("SUBJECT", "FROM"):
                needle = text()
                return lambda view: needle in view[word]
            if word == "HEADER":
                field, needle = text(), text()
                return lambda view: needle in view["HEADER"].get(field, "")
            if word in ("SINCE", "BEFORE"):
                day, month, year = take().split("-")
                bound = (int(year), MONTHS.index(month.title()) + 1, int(day))
                return (lambda view: view["DAY"] >= bound) if word == "SINCE" else (lambda view: view["DAY"] < bound)
            if word == "NOT":
                inner = key()
                return lambda view: not inner(view)
            if word == "OR":
                left, right = key(), key()
                return lambda view: left(view) or right(view)
            raise Bad("unknown search key " + token)

        tests = []
        while position < len(args):
            tests.append(key())
        if not tests:
            raise Bad("empty search")
        return [uid for uid in sorted(folder.mail) if all(test(self.view(*folder.mail[uid])) for test in tests)]


class WireIMAP(imaplib.IMAP4):
    """imaplib's own client over a socketpair to the fixture server."""

    def __init__(self, server, host, port):
        self.server = server
        super().__init__(host, port, timeout=10)

    def open(self, host="", port=imaplib.IMAP4_PORT, timeout=None):
        self.host, self.port = host, port
        self.sock, theirs = socket.socketpair()
        self.sock.settimeout(timeout)
        threading.Thread(target=self.server.serve, args=(theirs,), daemon=True).start()
        self.file = self.sock.makefile("rb")


def raw_mail(subject, message_id, sent):
    mail = EmailMessage()
    mail["From"] = "Billing <billing@example.invalid>"
    mail["To"] = "reader@example.invalid"
    mail["Subject"] = subject
    mail["Date"] = formatdate(sent)
    mail["Message-ID"] = message_id
    mail.set_content("Body")
    return mail.as_bytes(policy=policy.SMTP)


def fill_inbox(folder, rng):
    """250 receipts among other mail; UIDs have gaps and arrival dates are shuffled."""
    kinds = ["receipt"] * 250 + ["digest"] * 37 + ["sign-in"] * 3
    rng.shuffle(kinds)
    arrivals = rng.sample(range(1_780_000_000, 1_790_000_000, 977), len(kinds))
    made, uid = {"receipt": [], "digest": [], "sign-in": []}, 10
    for kind, arrived in zip(kinds, arrivals):
        uid += rng.choice((1, 1, 2, 5))
        number = len(made[kind])
        subject = {"receipt": f"Your receipt #{number:03d} / Чек {number:03d}", "digest": f"Weekly digest {number}",
                   "sign-in": "New sign-in to your account"}[kind]
        folder.add(raw_mail(subject, f"<{kind}-{number}@example.invalid>", arrived - 60), arrived, uid=uid)
        made[kind].append(uid)
    return made


@pytest.fixture
def mail(tmp_path, monkeypatch):
    server = Server()
    made = fill_inbox(server.folders["INBOX"], random.Random(20261114))
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host, port, *, ssl_context=None, timeout=None: WireIMAP(server, host, port))
    accounts = AccountStore(tmp_path)
    first, second = (accounts.save_imap({"address": address, "password": "fixture", "imap_host": "imap.invalid",
                                         "smtp_host": "smtp.invalid"})["id"]
                     for address in ("reader@example.invalid", "other@example.invalid"))
    service = MailService(SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda _: {}))
    return SimpleNamespace(server=server, service=service, first=first, second=second, made=made)


def traverse(mail, criteria, limit, **arguments):
    pages, token = [], ""
    for _ in range(10):
        page = mail.service.search(account_id=mail.first, criteria=criteria, limit=limit, page_token=token, **arguments)
        pages.append(page)
        assert page["returned"] == len(page["messages"]) and page["order"] == "uid_desc"
        assert page["has_more"] is bool(page["next_page_token"])
        token = page["next_page_token"]
        if not token:
            return pages
    raise AssertionError("the traversal did not end")


def retoken(token, **changes):
    encoded = token[len("imap-page:"):]
    fields = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    fields.update(changes)
    return "imap-page:" + base64.urlsafe_b64encode(json.dumps(fields).encode()).decode().rstrip("=")


@pytest.mark.parametrize("criteria,limit,sizes", [
    (RECEIPTS, 200, [200, 50]),
    (RECEIPTS, 100, [100, 100, 50]),
    (["SUBJECT", "Чек"], 100, [100, 100, 50]),  # A UTF-8 literal search pages the same way.
])
def test_pages_return_every_match_once_newest_uid_first(mail, criteria, limit, sizes):
    pages = traverse(mail, criteria, limit)
    assert [page["returned"] for page in pages] == sizes
    assert all(page["total"] == 250 and page["uidvalidity"] == 7 for page in pages)
    assert [page["has_more"] for page in pages] == [True] * (len(sizes) - 1) + [False]
    seen = [message["uid"] for page in pages for message in page["messages"]]
    assert seen == sorted(mail.made["receipt"], reverse=True)  # each match exactly once, none missing
    dates = [message["internal_date"] for page in pages for message in page["messages"]]
    assert dates != sorted(dates, reverse=True)  # UID order, which the result names, is not date order here
    assert all(message["account_id"] == mail.first and message["message_ref"].startswith("mail:")
               and "body" not in message for page in pages for message in page["messages"])
    commands = mail.server.commands()
    assert sum(command.startswith("UID SEARCH") for command in commands) == len(sizes)
    fetches = [command for command in commands if command.startswith("UID FETCH")]
    assert len(fetches) == 250 and all("BODY.PEEK[HEADER]" in command for command in fetches)
    last = pages[-1]["messages"][-1]
    assert mail.service.read(account_id=mail.first, message_ref=last["message_ref"])["uid"] == min(mail.made["receipt"])


def test_mail_arriving_or_leaving_between_pages_neither_repeats_nor_shifts_them(mail):
    inbox = mail.server.folders["INBOX"]
    first = mail.service.search(account_id=mail.first, criteria=RECEIPTS, limit=100)
    # An imported receipt gets the next UID although its date is the oldest of all.
    arrived = inbox.add(raw_mail("Your receipt #999 / Чек 999", "<late@example.invalid>", 1_700_000_000), 1_700_000_000)
    gone = sorted(mail.made["receipt"])[10]  # Expunged before the page that would hold it.
    del inbox.mail[gone]
    second = mail.service.search(account_id=mail.first, criteria=RECEIPTS, limit=100, page_token=first["next_page_token"])
    third = mail.service.search(account_id=mail.first, criteria=RECEIPTS, limit=100, page_token=second["next_page_token"])
    seen = [message["uid"] for page in (first, second, third) for message in page["messages"]]
    assert seen == sorted(set(mail.made["receipt"]) - {gone}, reverse=True)
    assert (first["total"], second["total"], third["total"]) == (250, 249, 249)
    assert [page["returned"] for page in (first, second, third)] == [100, 100, 49]
    assert third["has_more"] is False and third["next_page_token"] == ""
    fresh = mail.service.search(account_id=mail.first, criteria=RECEIPTS, limit=100)
    assert fresh["messages"][0]["uid"] == arrived and fresh["total"] == 250


def test_a_continuation_is_refused_for_another_account_folder_criteria_or_uidvalidity(mail):
    page = mail.service.search(account_id=mail.first, criteria=RECEIPTS, limit=100)
    token = page["next_page_token"]
    assert token.startswith("imap-page:") and page["has_more"]
    mark = len(mail.server.log)
    for arguments, refusal in (
        ({"account_id": mail.second}, "another account"),
        ({"account_id": mail.first, "folder": "Archive"}, "continues folder 'INBOX'"),
        ({"account_id": mail.first, "criteria": ["SUBJECT", "Чек"]}, "different criteria"),
        ({"account_id": mail.first, "criteria": ["ALL"]}, "different criteria"),
        ({"account_id": mail.first, "page_token": "CAIQAA"}, "next_page_token from the previous email_search page"),
        ({"account_id": mail.first, "page_token": token[:-6]}, "next_page_token from the previous email_search page"),
        ({"account_id": mail.first, "page_token": retoken(token, below=0)}, "next_page_token from the previous"),
        ({"account_id": mail.first, "page_token": retoken(token, top=True)}, "next_page_token from the previous"),
        ({"account_id": mail.first, "page_token": retoken(token, extra=1)}, "next_page_token from the previous"),
    ):
        call = {"criteria": RECEIPTS, "limit": 100, "page_token": token, **arguments}
        with pytest.raises(ValueError, match=re.escape(refusal)):
            mail.service.search(**call)
    assert len(mail.server.log) == mark  # Refused before any IMAP connection.
    # INBOX is the one case-insensitive IMAP name, so the same folder continues.
    same = mail.service.search(account_id=mail.first, folder="inbox", criteria=RECEIPTS, limit=100, page_token=token)
    assert same["returned"] == 100 and same["messages"][0]["uid"] < page["messages"][-1]["uid"]
    mail.server.folders["INBOX"].validity = 8
    mark = len(mail.server.log)
    with pytest.raises(ValueError, match="reports UIDVALIDITY 8, but this page_token was issued under 7"):
        mail.service.search(account_id=mail.first, criteria=RECEIPTS, limit=100, page_token=token)
    commands = mail.server.commands(mark)
    assert 'EXAMINE "INBOX"' in commands and not any(command.startswith("UID ") for command in commands)


def test_a_unicode_folder_continues_under_its_own_name(mail):
    folder = mail.server.folders["Архив/Чеки"] = Folder(44)
    uids = [folder.add(raw_mail(f"Your receipt #{n} / Чек {n}", f"<archived-{n}@example.invalid>", 1_780_000_000 - n),
                       1_780_000_000 - n) for n in range(3)]
    pages = traverse(mail, ["SUBJECT", "Чек"], 2, folder="Архив/Чеки")
    assert [[message["uid"] for message in page["messages"]] for page in pages] == [uids[:0:-1], uids[:1]]
    assert all(page["folder"] == "Архив/Чеки" and page["uidvalidity"] == 44 for page in pages)
    assert 'EXAMINE "&BBAEQARFBDgEMg-/&BCcENQQ6BDg-"' in mail.server.commands()
    with pytest.raises(ValueError, match="continues folder 'Архив/Чеки'"):
        mail.service.search(account_id=mail.first, criteria=["SUBJECT", "Чек"], limit=2,
                            page_token=mail.service.search(account_id=mail.first, folder="Архив/Чеки", criteria=["SUBJECT", "Чек"],
                                                           limit=2)["next_page_token"])


def test_empty_results_report_no_continuation(mail):
    mail.server.folders["Empty"] = Folder(31)
    mark = len(mail.server.log)
    for arguments in ({"criteria": ["SUBJECT", '"no such receipt"']}, {"folder": "Empty"}):
        result = mail.service.search(account_id=mail.first, **arguments)
        assert (result["total"], result["returned"], result["has_more"], result["next_page_token"], result["messages"]) \
            == (0, 0, False, "", [])
        assert "not proof that a message never arrived" in result["search_note"]
    assert not any(command.startswith("UID FETCH") for command in mail.server.commands(mark))


def test_read_names_the_folder_and_both_uidvalidity_values_before_fetching(mail):
    archive = mail.server.folders["Archive"] = Folder(900)
    archive.add(raw_mail("Archived receipt", "<archived@example.invalid>", 1_780_000_000), 1_780_000_060)
    found = mail.service.search(account_id=mail.first, folder="Archive")["messages"][0]
    assert (found["uid"], found["uidvalidity"]) == (1, 900)
    mark = len(mail.server.log)
    with pytest.raises(ValueError) as refused:
        mail.service.read(account_id=mail.first, uid=found["uid"], uidvalidity=found["uidvalidity"])  # INBOX
    assert str(refused.value).startswith("Folder 'INBOX' reports UIDVALIDITY 7, but this request names 900; nothing was read.")
    assert "changed" not in str(refused.value)
    commands = mail.server.commands(mark)
    assert 'EXAMINE "INBOX"' in commands and not any(command.startswith("UID FETCH") for command in commands)
    for arguments in ({"uid": 1, "uidvalidity": 900, "folder": "Archive"}, {"message_ref": found["message_ref"]}):
        assert mail.service.read(account_id=mail.first, **arguments)["message_id"] == "<archived@example.invalid>"


def test_criteria_stay_raw_tokens_and_existing_lookups_still_work(mail):
    mark = len(mail.server.log)
    quoted = mail.service.search(account_id=mail.first, criteria=["SUBJECT", '"New sign-in"'])
    assert sorted(message["uid"] for message in quoted["messages"]) == sorted(mail.made["sign-in"])
    assert 'UID SEARCH SUBJECT "New sign-in"' in mail.server.commands(mark)
    mark = len(mail.server.log)
    with pytest.raises(imaplib.IMAP4.error):  # Never re-quoted: the server receives two search keys.
        mail.service.search(account_id=mail.first, criteria=["SUBJECT", "New sign-in"])
    assert "UID SEARCH SUBJECT New sign-in" in mail.server.commands(mark)
    mark = len(mail.server.log)
    since = mail.service.search(account_id=mail.first, criteria=["SINCE", "1-Jan-2020", "SUBJECT", "Чек", "FROM", "billing@example.invalid"], limit=5)
    assert since["total"] == 250 and since["returned"] == 5
    assert "UID SEARCH CHARSET UTF-8 SINCE 1-Jan-2020 SUBJECT {6}Чек FROM billing@example.invalid" in mail.server.commands(mark)
    with mail.service.target(mail.first) as (_account, _store, client):
        found = client.find_message("<sign-in-2@example.invalid>")
    assert found["uid"] == mail.made["sign-in"][2] and found["body"].strip() == "Body"
    with pytest.raises(ValueError, match="query belongs to Gmail or Exchange search"):
        mail.service.search(account_id=mail.first, query="subject:receipt")


@pytest.mark.parametrize("provider", ["gmail", "ews"])
def test_gmail_and_exchange_keep_their_native_query_and_page_token(tmp_path, monkeypatch, provider):
    calls = []

    class Native:
        def __init__(self, *_arguments):
            pass

        def search(self, **arguments):
            calls.append(arguments)
            return {"folder": arguments["folder"], "messages": [], "next_page_token": "native-2"}
    accounts = AccountStore(tmp_path)
    if provider == "gmail":
        account = gmail_account(accounts)
        monkeypatch.setattr(service_module, "GmailClient", Native)
    else:
        account = accounts.save_ews({"address": "work@example.invalid", "endpoint": "https://mail.example.invalid/EWS/Exchange.asmx",
                                     "username": "delegated-login", "password": "fixture-only"})
        monkeypatch.setattr(ews, "EwsClient", Native)
    service = MailService(SimpleNamespace(get_state_dir=lambda: tmp_path, get_settings=lambda _: {}))
    page = service.search(account_id=account["id"], query="receipt", page_token="native-1", limit=20)
    assert [{key: value for key, value in call.items() if key != "folder"} for call in calls] == [
        {"criteria": None, "limit": 20, "page_token": "native-1", "query": "receipt"}]
    assert page == {"folder": calls[0]["folder"], "messages": [], "next_page_token": "native-2", "account_id": account["id"]}
