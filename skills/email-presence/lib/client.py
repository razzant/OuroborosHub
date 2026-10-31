"""Standard IMAP/SMTP operations shared by mailbox tools and the companion."""
from __future__ import annotations

import imaplib
import base64
import hashlib
import os
import re
import smtplib
import ssl
import time
from contextlib import contextmanager
from email.policy import SMTP

import httpx

from .mime import build_message, parse_message


class MailClient:
    def __init__(self, settings=None):
        self.settings = settings if settings is not None else os.environ
        self._token = ""
        self._expires = 0.0

    def _get(self, key, default=""):
        return str(self.settings.get(key) or default)

    def _access_token(self):
        if self._token and time.time() < self._expires:
            return self._token
        refresh = self._get("EMAIL_OAUTH_REFRESH_TOKEN")
        if refresh:
            # Existing credentials only: this never starts an interactive login.
            response = httpx.post("https://oauth2.googleapis.com/token", data={
                "grant_type": "refresh_token", "refresh_token": refresh,
                "client_id": self._get("EMAIL_OAUTH_CLIENT_ID"),
                "client_secret": self._get("EMAIL_OAUTH_CLIENT_SECRET"),
            }, timeout=30)
            if response.status_code != 200:
                raise RuntimeError(f"OAuth token refresh failed (HTTP {response.status_code})")
            payload = response.json()
            self._token = str(payload.get("access_token") or "")
            self._expires = time.time() + max(0, int(payload.get("expires_in", 3600)) - 60)
        else:
            self._token = self._get("EMAIL_OAUTH_ACCESS_TOKEN")
        if not self._token:
            raise RuntimeError("OAuth access token or refresh credentials are required")
        return self._token

    def _auth(self, client, *, smtp=False):
        user = self._get("EMAIL_SMTP_USER", self._get("EMAIL_USER")) if smtp else self._get("EMAIL_USER")
        if not user:
            raise RuntimeError("EMAIL_USER is required")
        if self._get("EMAIL_AUTH_MODE", "password") == "oauth2":
            value = f"user={user}\x01auth=Bearer {self._access_token()}\x01\x01"
            if smtp:
                client.auth("XOAUTH2", lambda challenge=None: value if challenge is None else "")
            else:
                client.authenticate("XOAUTH2", lambda challenge: b"" if challenge else value.encode())
        else:
            password = self._get("EMAIL_SMTP_PASSWORD", self._get("EMAIL_PASSWORD")) if smtp else self._get("EMAIL_PASSWORD")
            if not password:
                raise RuntimeError("EMAIL_PASSWORD is required for password authentication")
            client.login(user, password)

    @staticmethod
    def _ok(result, operation):
        status, data = result
        if status != "OK":
            raise RuntimeError(f"IMAP {operation} failed ({status})")
        return data

    @contextmanager
    def imap(self, folder=None, *, readonly=True):
        host = self._get("EMAIL_IMAP_HOST")
        if not host:
            raise RuntimeError("EMAIL_IMAP_HOST is required")
        security = self._get("EMAIL_IMAP_SECURITY", "ssl")
        if security not in {"ssl", "starttls"}:
            raise ValueError("EMAIL_IMAP_SECURITY must be ssl or starttls")
        port = int(self._get("EMAIL_IMAP_PORT", 993 if security == "ssl" else 143))
        context = self._tls_context()
        box = (imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=30) if security == "ssl"
               else imaplib.IMAP4(host, port, timeout=30))
        try:
            if security == "starttls":
                self._ok(box.starttls(ssl_context=context), "starttls")
            self._verify_tls(box)
            self._auth(box)
            if folder is not None:
                self._ok(box.select(self.folder_name(folder), readonly=readonly), "select")
            yield box
        finally:
            try:
                box.logout()
            except (OSError, imaplib.IMAP4.error):
                pass

    @staticmethod
    def quote(value):
        value = str(value)
        if "\r" in value or "\n" in value or "\0" in value:
            raise ValueError("IMAP values cannot contain line breaks or NUL")
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

    def _tls_context(self):
        fingerprint = self._get("EMAIL_TLS_FINGERPRINT").replace(":", "").lower()
        if fingerprint:
            if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
                raise ValueError("TLS fingerprint must be the certificate's SHA-256 (64 hexadecimal characters)")
            # Bridge's private self-signed certificate is trusted by its exact
            # digest, checked before any password/token reaches the connection.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        certificate = self._get("EMAIL_TLS_CA")
        return ssl.create_default_context(cadata=certificate) if "-----BEGIN CERTIFICATE-----" in certificate else ssl.create_default_context(cafile=certificate or None)

    def _verify_tls(self, client):
        fingerprint = self._get("EMAIL_TLS_FINGERPRINT").replace(":", "").lower()
        if fingerprint and hashlib.sha256(client.sock.getpeercert(binary_form=True)).hexdigest() != fingerprint:
            raise ssl.SSLCertVerificationError("The server certificate does not match the configured SHA-256 fingerprint")

    @classmethod
    def folder_name(cls, value):
        """IMAP4 modified UTF-7; ASCII ampersands have their own escape."""
        out, chunk = [], []
        def flush():
            if chunk:
                encoded = base64.b64encode("".join(chunk).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
                out.append("&" + encoded + "-")
                chunk.clear()
        for char in str(value):
            if " " <= char <= "~":
                flush()
                out.append("&-" if char == "&" else char)
            else:
                chunk.append(char)
        flush()
        return cls.quote("".join(out))

    @staticmethod
    def decode_folder(value):
        def decode(match):
            raw = match.group(1)
            if not raw:
                return "&"
            raw = raw.replace(",", "/")
            return base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-16-be")
        return re.sub(r"&([A-Za-z0-9+,]*)-", decode, str(value))

    @staticmethod
    def metadata(box):
        def integer(name):
            _, values = box.response(name)
            if not values or not values[0]:
                raise RuntimeError(f"IMAP server omitted {name}")
            return int(values[0])
        return integer("UIDVALIDITY"), integer("UIDNEXT")

    def uids(self, box, criteria):
        if any(not value.isascii() for value in criteria):
            result = self._search_utf8(box, criteria)
        else:
            result = box.uid("SEARCH", None, *criteria)
        data = self._ok(result, "search")
        return sorted(int(x) for x in (data[0] or b"").split())

    @staticmethod
    def _search_utf8(box, criteria):
        """Issue UTF-8 SEARCH literals, including more than one Unicode operand.

        imaplib's public literal property supports only one final literal. Use
        its existing tagged-response parser for the multi-literal wire exchange.
        Each synchronization wait retains the connection's ordinary timeout.
        """
        tag = box._new_tag()
        box.untagged_responses.pop("SEARCH", None)
        pending = tag + b" UID SEARCH CHARSET UTF-8"
        for token in criteria:
            if token.isascii():
                pending += b" " + token.encode("ascii")
                continue
            value = token
            if value.startswith('"') and value.endswith('"'):
                value = re.sub(r'\\([\\"])', r'\1', value[1:-1])
            literal = value.encode("utf-8")
            box.send(pending + b" {" + str(len(literal)).encode() + b"}\r\n")
            while box._get_response() is not None:
                if box.tagged_commands[tag] is not None:
                    return box._untagged_response(*box._command_complete("UID", tag), "SEARCH")
            pending = literal
        box.send(pending + b"\r\n")
        return box._untagged_response(*box._command_complete("UID", tag), "SEARCH")

    def fetch(self, box, folder, uid, validity, *, include_attachment_data=False, headers_only=False):
        body = "BODY.PEEK[HEADER]" if headers_only else "BODY.PEEK[]"
        data = self._ok(box.uid("FETCH", str(int(uid)), f"({body} INTERNALDATE FLAGS)"), "fetch")
        part = next((p for p in data if isinstance(p, tuple) and isinstance(p[1], bytes)), None)
        if part is None:
            raise RuntimeError(f"IMAP UID {uid} disappeared before it could be read")
        message = parse_message(part[1], folder=folder, uid=uid, include_attachment_data=include_attachment_data)
        metadata = b" ".join(p[0] if isinstance(p, tuple) else p for p in data if isinstance(p, (bytes, tuple)))
        date = imaplib.Internaldate2tuple(metadata)
        if date is None:
            raise RuntimeError("IMAP FETCH omitted INTERNALDATE")
        message.update(uidvalidity=validity, internal_date=time.mktime(date),
                       flags=re.findall(rb"FLAGS \(([^)]*)\)", metadata)[0].decode().split()
                       if b"FLAGS (" in metadata else [])
        if headers_only:
            message.pop("body", None)
            message.pop("_raw_source", None)
        return message

    def search(self, *, folder="INBOX", criteria=None, limit=50):
        criteria = criteria or ["ALL"]
        if not isinstance(criteria, list) or not all(isinstance(c, str) and not any(x in c for x in "\r\n\0") for c in criteria):
            raise ValueError("criteria must be an array of IMAP search tokens without line breaks")
        with self.imap(folder) as box:
            validity, _ = self.metadata(box)
            uids = self.uids(box, criteria)
            chosen = uids[-max(1, min(200, int(limit))):]
            messages = [self.fetch(box, folder, uid, validity, headers_only=True) for uid in reversed(chosen)]
        for message in messages:
            message.pop("body", None)
        return {"folder": folder, "uidvalidity": validity, "total": len(uids), "messages": messages}

    def find_message(self, message_id, *, folder="INBOX"):
        found = self.search(folder=folder, criteria=["HEADER", "Message-ID", self.quote(message_id)], limit=1)
        if not found["messages"]:
            return None
        row = found["messages"][0]
        return self.read(uid=row["uid"], uidvalidity=row["uidvalidity"], folder=folder)

    def read(self, *, uid, uidvalidity, folder="INBOX", include_attachment_data=False):
        """Peek only: a read-only session and BODY.PEEK leave every flag unchanged."""
        with self.imap(folder, readonly=True) as box:
            validity, _ = self.metadata(box)
            if validity != int(uidvalidity):
                raise ValueError("Mailbox UIDVALIDITY changed; search again before addressing a message")
            return self.fetch(box, folder, uid, validity, include_attachment_data=include_attachment_data)

    def mark_read(self, *, uid, uidvalidity, folder="INBOX"):
        """Add \\Seen to one message: the explicit write that read() never performs."""
        return self.mailbox(action="flags", folder=folder, uid=uid, uidvalidity=uidvalidity, flags=["\\Seen"])

    def mailbox(self, *, action="list", folder="INBOX", uid=None, uidvalidity=None,
                destination="", flags=None, remove_flags=False):
        with self.imap(None if action in {"list", "create"} else folder, readonly=False) as box:
            if action == "list":
                rows = [x.decode("ascii", errors="replace") for x in self._ok(box.list(), "list") if x]
                return {"folders": [self.decode_folder(row) for row in rows]}
            if action == "create":
                self._ok(box.create(self.folder_name(destination)), "create")
                return {"ok": True}
            validity, _ = self.metadata(box)
            if validity != int(uidvalidity or 0) or not uid or int(uid) < 1:
                raise ValueError("A current UID and UIDVALIDITY from search/read are required")
            if action in {"copy", "move"}:
                if action == "move" and not any(
                    (cap.decode("ascii", errors="ignore") if isinstance(cap, bytes) else str(cap)).upper() == "MOVE"
                    for cap in box.capabilities
                ):
                    raise RuntimeError("Server does not support atomic UID MOVE; use copy and flags explicitly")
                self._ok(box.uid(action.upper(), str(int(uid)), self.folder_name(destination)), action)
            elif action == "flags":
                flags = flags or []
                if not flags or any(not re.fullmatch(r"\\?[A-Za-z0-9_-]+", f) for f in flags):
                    raise ValueError("flags must contain IMAP flag atoms")
                self._ok(box.uid("STORE", str(int(uid)), "-FLAGS.SILENT" if remove_flags else "+FLAGS.SILENT", "(" + " ".join(flags) + ")"), "store")
            else:
                raise ValueError("Supported actions: list, create, copy, move, flags")
            return {"ok": True, "uid": int(uid), "uidvalidity": validity}

    def draft(self, *, to, subject, body="", folder="Drafts", reply_to_message_id="", references=None,
              cc=(), bcc=(), html_body="", body_type="plain", attachments=(), sender=""):
        recipients = [x.strip() for x in (to if isinstance(to, (list, tuple)) else str(to).split(",")) if str(x).strip()]
        cc = [x.strip() for x in (cc if isinstance(cc, (list, tuple)) else str(cc or "").split(",")) if str(x).strip()]
        bcc = [x.strip() for x in (bcc if isinstance(bcc, (list, tuple)) else str(bcc or "").split(",")) if str(x).strip()]
        message = build_message(sender=sender or self._get("EMAIL_USER"), recipients=recipients,
                                subject=subject, body=body, in_reply_to=reply_to_message_id,
                                references=references or [], cc=list(cc or ()), bcc=list(bcc or ()),
                                html_body=html_body, body_type=body_type, attachments=attachments,
                                include_bcc_header=True)
        with self.imap() as box:
            data = self._ok(box.append(self.folder_name(folder), "(\\Draft)",
                                      imaplib.Time2Internaldate(time.time()), message.as_bytes(policy=SMTP)), "append draft")
        return {"ok": True, "message_id": str(message["Message-ID"]), "folder": folder,
                "receipt": [x.decode(errors="replace") for x in data if x]}

    @contextmanager
    def smtp(self):
        host = self._get("EMAIL_SMTP_HOST")
        if not host:
            raise RuntimeError("EMAIL_SMTP_HOST is required")
        port = int(self._get("EMAIL_SMTP_PORT", 587))
        security = self._get("EMAIL_SMTP_SECURITY", "ssl" if port == 465 else "starttls")
        if security not in {"ssl", "starttls"}:
            raise ValueError("EMAIL_SMTP_SECURITY must be ssl or starttls")
        context = self._tls_context()
        client = smtplib.SMTP_SSL(host, port, context=context, timeout=30) if security == "ssl" else smtplib.SMTP(host, port, timeout=30)
        try:
            client.ehlo()
            if security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            self._verify_tls(client)
            self._auth(client, smtp=True)
            yield client
        finally:
            # A failed QUIT after accepted DATA must not turn success into retry.
            client.close()

    def message(self, item):
        recipients = list(getattr(item, "to", ()) or item.recipients)
        return build_message(sender=item.reporting.get("sender") or self._get("EMAIL_DEFAULT_SENDER", self._get("EMAIL_USER")), recipients=recipients,
                             subject=item.subject, body=item.body, in_reply_to=item.in_reply_to,
                             references=item.references, message_id=item.message_id,
                             cc=list(getattr(item, "cc", ()) or ()), bcc=list(getattr(item, "bcc", ()) or ()),
                             html_body=getattr(item, "html_body", ""), body_type=getattr(item, "body_type", "plain"),
                             attachments=list(getattr(item, "attachments", ()) or ()))

    @contextmanager
    def delivery(self):
        with self.smtp() as session:
            yield session

    def transmit(self, session, message, recipients, *, has_bcc=False, reporting=None):
        refused = session.send_message(message, to_addrs=recipients) if has_bcc else session.send_message(message)
        return refused, {}

    def test_connection(self):
        with self.imap():
            pass
        with self.smtp():
            pass
        return {"ok": True, "imap": "authenticated", "smtp": "authenticated"}
