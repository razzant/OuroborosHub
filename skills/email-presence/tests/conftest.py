"""Offline mail tests must not fall through a missing mock into a real account."""
import imaplib
import smtplib

import httpx
import pytest


@pytest.fixture(autouse=True)
def no_provider_network(monkeypatch):
    def refused(*_args, **_kwargs):
        raise AssertionError("This offline test attempted an unmocked network connection")
    async def refused_async(*_args, **_kwargs):
        refused()
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refused)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refused_async)
    monkeypatch.setattr(imaplib.IMAP4, "open", refused)
    monkeypatch.setattr(smtplib.SMTP, "connect", refused)
