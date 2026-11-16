"""Widget routes answer structured, redacted JSON for connection failures and malformed bodies."""
import imaplib
import json
import smtplib
import ssl
import sys

import httpx
import pytest
from starlette.testclient import TestClient

from test_account_routes import imap_values
from test_imap_list_literals import WireIMAP
from test_plugin import _settings_app

SECRET = "private-account-password"


class LoginServer:
    """Echoes the submitted login in its refusal, as some servers do, to prove the route never repeats it."""

    def __init__(self, accept):
        self.accept = accept

    def serve(self, sock):
        reader = sock.makefile("rb")
        try:
            sock.sendall(b"* OK fixture ready\r\n")
            while line := reader.readline():
                tag, _, command = line.rstrip(b"\r\n").partition(b" ")
                verb = command.split(b" ", 1)[0].upper()
                if verb == b"CAPABILITY":
                    sock.sendall(b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK done\r\n")
                elif verb == b"LOGIN":
                    sock.sendall(tag + (b" OK logged in\r\n" if self.accept else
                                        b" NO [AUTHENTICATIONFAILED] Invalid credentials: " + command + b"\r\n"))
                elif verb == b"LOGOUT":
                    sock.sendall(b"* BYE\r\n" + tag + b" OK\r\n")
                    return
                else:
                    sock.sendall(tag + b" BAD unsupported\r\n")
        finally:
            sock.close()


class SMTPDenied:
    def __init__(self, *_args, **_kwargs):
        pass

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, **_kwargs):
        return 220, b"ok"

    def login(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, f"5.7.8 rejected {user} {password}".encode())

    def close(self):
        pass


def saved(http):
    return http.post("/accounts/save", json=imap_values()).json()["account"]["id"]


def assert_redacted(response):
    assert response.status_code in (400, 502)
    body = response.json()
    assert body["ok"] is False and SECRET not in json.dumps(body) and "one@example.invalid" not in body["error"]
    return body


@pytest.mark.parametrize("failure,stage", [
    ("login", "imap_auth"), ("refused", "imap_connect"), ("certificate", "imap_tls"), ("smtp_login", "smtp_auth")])
def test_account_test_reports_stage_and_remedy_without_secrets(tmp_path, monkeypatch, failure, stage):
    def imap_ssl(host, port, *, ssl_context=None, timeout=None):
        if failure == "refused":
            raise ConnectionRefusedError(61, "Connection refused")
        if failure == "certificate":
            raise ssl.SSLCertVerificationError("certificate verify failed")
        return WireIMAP(LoginServer(accept=failure == "smtp_login"), host, port)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", imap_ssl)
    monkeypatch.setattr(smtplib, "SMTP", SMTPDenied)
    with TestClient(_settings_app(tmp_path)) as http:
        account_id = saved(http)
        body = assert_redacted(http.post("/accounts/test", json={"account_id": account_id}))
    assert body["code"] == "connection_failed" and body["stage"] == stage
    assert body["remedy"] and body["remedy"] in body["error"]
    if failure == "login":
        assert body["error_type"] == "error:AUTHENTICATIONFAILED"
    if failure == "smtp_login":
        assert body["error_type"] == "SMTPAuthenticationError:535"


def test_oauth_routes_reject_non_object_json_as_structured_400(tmp_path):
    with TestClient(_settings_app(tmp_path)) as http:
        for route in ("/oauth/begin", "/oauth/complete"):
            for value in ([], None, "text", 7):
                response = http.post(route, content=json.dumps(value), headers={"Content-Type": "application/json"})
                assert response.status_code == 400
                assert response.json() == {"ok": False, "error": "Expected a JSON object"}


@pytest.mark.parametrize("route", ["/oauth/begin", "/oauth/complete"])
def test_oauth_transport_failure_is_a_redacted_502(tmp_path, monkeypatch, route):
    app = _settings_app(tmp_path, {"GOOGLE_OAUTH_CLIENT_ID": "client"})
    module = sys.modules["email_settings_fixture.lib.google_auth"]
    def unreachable(*_args, **_kwargs):
        raise httpx.ConnectError("proxy.internal:3128 refused https://oauth2.googleapis.com/token?code=SECRET")
    monkeypatch.setattr(module, "begin", unreachable)
    monkeypatch.setattr(module, "finish", unreachable)
    with TestClient(app) as http:
        response = http.post(route, json={"origin": "http://127.0.0.1:1", "state": "s", "code": "c"})
    assert response.status_code == 502
    body = response.json()
    assert body["ok"] is False and "ConnectError" in body["error"]
    assert "SECRET" not in body["error"] and "proxy.internal" not in body["error"]
