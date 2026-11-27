"""One sentence to the owner through the Host Service (`POST /notify`, `notify_version` 1), about mail that did not go out
as queued: Mail keeps each one in the mailbox's own store (EmailStore owner notices) and the companion posts it here
until the host confirms it.

The host writes it as one System row in the owner's chat, which the Telegram skill mirrors; no model turn starts, and
Ouroboros reads it on its next turn. Until the host confirms it (no `notify_owner` grant yet, an older host without
`/notify`, a host that is busy or away) it stays pending, shown in the mailbox's status and receipts, and is posted later.
"""
from __future__ import annotations

import logging
import os

import httpx

from .host_adapter import _SkillToken, _loopback
from .store import EmailStore

log = logging.getLogger("email_presence")
# Pauses before a notice the host did not take is posted again: they grow per notice, up to these ceilings.
_WAIT = {"refused": (600, 6 * 3600), "busy": (60, 3600), "away": (30, 3600)}


def _pause(kind, attempts):
    first, longest = _WAIT[kind]
    return min(longest, first * 2 ** min(int(attempts or 0), 10))


class OwnerNotifier:
    def __init__(self, url="", token="", *, http=None):
        self._url = str(url).rstrip("/")
        self._token = _SkillToken(token)
        self._http = http

    @classmethod
    def environment(cls):
        # The companion receives the host's loopback address and this skill's token, as the sign-in client does.
        return cls(os.environ.get("HOST_SERVICE_URL", ""), os.environ.get("HOST_SERVICE_TOKEN", ""))

    def _call(self, method, path, body=None, *, timeout=10):
        if not self._url or not _loopback(self._url) or not self._token.available:
            raise OSError("The Host Service is not available to Mail")

        def request(client):
            return client.request(method, self._url + path, headers={"x-skill-token": self._token.use_in_request()},
                                  json=body, timeout=timeout, follow_redirects=False)
        if self._http is not None:
            return request(self._http)
        with httpx.Client(trust_env=False) as client:
            return request(client)

    def supported(self):
        """True when the host announces `notify_version` 1, False when it answers without it (an older host keeps the
        notices in Mail's own status), None while it cannot be read."""
        try:
            response = self._call("GET", "/identity", timeout=5)
            answer = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            return None
        if response.status_code != 200 or not isinstance(answer, dict):
            return None
        return answer.get("notify_version") == 1

    def post(self, text, attempts=0):
        """("sent", 0) once the host wrote the row; otherwise (why, seconds to wait) and it is posted again later.

        A 503 says the row is not confirmed: usually no chat writer was running and nothing was written, so it is posted
        again after a pause; in the rare case the host wrote it and lost its answer, the owner sees it twice."""
        try:
            response = self._call("POST", "/notify", {"text": text})
        except (httpx.HTTPError, OSError):
            return "away", _pause("away", attempts)
        if response.status_code == 200:
            return "sent", 0
        if response.status_code == 403:  # no live notify_owner grant for these files yet
            return "refused", _pause("refused", attempts)
        if response.status_code == 429:
            return "busy", _pause("busy", attempts)
        if response.status_code == 400:  # the host refused this very text: posting it again changes nothing
            return "invalid", 0
        return f"http_{response.status_code}", _pause("busy", attempts)


def signed_out(account):
    """A Gmail mailbox that no sign-in serves now: not connected, and not because Mail reserved its own saved sign-in
    while that moves to the host (a transfer neither refused nor withdrawn): nobody signed out then, and the next pass of
    the transfer settles it either way."""
    if account.get("provider") != "gmail" or account.get("connected"):
        return False
    claim = account.get("gmail_transfer") or {}
    return not (claim.get("generation") and not claim.get("refusal") and not claim.get("withdrawn"))


def tell_owner(accounts, notifier):
    """One pass over every mailbox with a store: a signed-out Gmail mailbox gets its waiting notices (EmailStore
    note_waiting); then each due notice that still holds is posted (EmailStore due_notices ends the others unposted).
    Returns how many the host confirmed."""
    supported, confirmed = None, 0
    for account in accounts.list():
        directory = accounts.mail_dir(account)
        if not (directory / "email_presence.sqlite3").exists():
            continue
        store = EmailStore(directory)
        try:
            away = signed_out(account)
            if away:
                store.note_waiting(account.get("address") or account["id"], sending_off=account.get("sending") == "off")
            due = store.due_notices(signed_out=away)
            if not due:
                continue
            if supported is None:
                supported = notifier.supported()
            if not supported:
                continue  # read again on the next pass; an older host leaves them in the status
            for key, notice in due:
                outcome, wait = notifier.post(notice["text"], notice.get("attempts"))
                store.finish_notice(key, outcome, wait, "" if outcome == "sent" else outcome)
                confirmed += outcome == "sent"
                if outcome in ("busy", "away"):
                    break  # the host's lane is full or it is away: the rest wait for the next pass
        finally:
            store.close()
    return confirmed
