"""Google PKCE connection owned by this mail skill, using the host return page."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx

from .accounts import AccountError

SCOPES = ("openid", "email", "https://www.googleapis.com/auth/gmail.modify",
          "https://www.googleapis.com/auth/gmail.settings.basic")
FULL_MAIL_SCOPE = "https://mail.google.com/"
RETURN_PAGE = "/onboarding/static/provider-return.html"


def _request(method, url, *, http=None, **kwargs):
    request = http.request if http is not None else httpx.request
    response = request(method, url, timeout=30, **kwargs)
    if response.status_code >= 400:
        # Provider bodies may echo credentials or private input. Do not expose them.
        raise AccountError(f"Google connection failed (HTTP {response.status_code}); check the client and granted permissions")
    return response.json()


def begin(store, settings, *, origin, login_hint="", permanent_delete=False, http=None, verify_return=True):
    client_id = str(settings.get("GOOGLE_OAUTH_CLIENT_ID") or settings.get("EMAIL_OAUTH_CLIENT_ID") or "").strip()
    client_secret = str(settings.get("GOOGLE_OAUTH_CLIENT_SECRET") or settings.get("EMAIL_OAUTH_CLIENT_SECRET") or "").strip()
    if not client_id:
        raise AccountError("Set and grant GOOGLE_OAUTH_CLIENT_ID in Settings before connecting Google")
    parsed = urlsplit(str(origin).rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
        raise AccountError("Use the application's public HTTP(S) address, including its deployment prefix if any")
    redirect = str(origin).rstrip("/") + RETURN_PAGE
    if verify_return:
        request = http.request if http is not None else httpx.request
        probe = request("HEAD", redirect, timeout=15)
        if probe.status_code not in {200, 401}:
            raise AccountError("This Ouroboros address does not serve the sign-in return page; update the host or correct its public address")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    envelope = {"v": 1, "skill": "email-presence", "nonce": secrets.token_urlsafe(32)}
    state = base64.urlsafe_b64encode(json.dumps(envelope, separators=(",", ":")).encode()).decode().rstrip("=")
    scopes = list(SCOPES) + ([FULL_MAIL_SCOPE] if permanent_delete else [])
    store.begin_oauth(state, {"verifier": verifier, "client_id": client_id, "client_secret": client_secret,
                              "redirect_uri": redirect, "scopes": scopes})
    query = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(scopes),
             "access_type": "offline", "prompt": "consent", "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256"}
    if login_hint:
        query["login_hint"] = str(login_hint)
    return {"ok": True, "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(query),
            "redirect_uri": redirect, "scopes": scopes,
            "message": "Authorize this mailbox in Google. Calendar is a separate connection using the same client."}


def finish(store, *, state, code="", error="", http=None):
    pending = store.consume_oauth(str(state))
    if error:
        return {"ok": False, "code": "authorization_declined", "error": "Google authorization was not granted; no account was connected."}
    if not code:
        raise AccountError("Google did not return an authorization code; start connection again")
    body = {"client_id": pending["client_id"], "code": str(code), "code_verifier": pending["verifier"],
            "grant_type": "authorization_code", "redirect_uri": pending["redirect_uri"]}
    if pending["client_secret"]:
        body["client_secret"] = pending["client_secret"]
    tokens = _request("POST", "https://oauth2.googleapis.com/token", http=http, data=body)
    access = str(tokens.get("access_token") or "")
    if not access:
        raise AccountError("Google did not return an access token")
    headers = {"Authorization": "Bearer " + access}
    user = _request("GET", "https://openidconnect.googleapis.com/v1/userinfo", http=http, headers=headers)
    profile = _request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile", http=http, headers=headers)
    subject = str(user.get("sub") or "")
    address = str(profile.get("emailAddress") or "")
    if not subject or not address or user.get("email_verified") is not True:
        raise AccountError("Google did not confirm this account's identity")
    account_id = "google:" + subject
    previous = None
    try:
        previous = store.get(account_id)
    except AccountError:
        pass
    old_tokens = (previous or {}).get("tokens") or {}
    same_client = (previous or {}).get("connected") and ((previous or {}).get("oauth") or {}).get("client_id") == pending["client_id"]
    refresh = str(tokens.get("refresh_token") or (old_tokens.get("refresh_token") if same_client else "") or "")
    if not refresh:
        raise AccountError("Google did not grant offline access; reconnect with consent to allow token refresh")
    scopes = str(tokens["scope"]).split() if "scope" in tokens else None
    record = {"id": account_id, "provider": "gmail", "address": address,
              "label": (previous or {}).get("label") or address, "senders": [address], "sender": address, "config": {},
              "oauth": {"client_id": pending["client_id"], "client_secret": pending["client_secret"]},
              "tokens": {"access_token": access, "refresh_token": refresh,
                         "expires_at": time.time() + int(tokens.get("expires_in") or 3600)},
              "scopes": scopes, "sending": (previous or {}).get("sending", "on"),
              "folder": (previous or {}).get("folder", "INBOX"), "words": (previous or {}).get("words", []),
              "poll_interval_sec": (previous or {}).get("poll_interval_sec", 30)}
    # sendAs listing is a capability of gmail.modify/settings.basic. A partial
    # grant may omit it; do not turn a successful mailbox login into a false failure.
    try:
        aliases = _request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/settings/sendAs", http=http, headers=headers)
        record["senders"] = list(dict.fromkeys([address] + [item["sendAsEmail"] for item in aliases.get("sendAs", [])
                                             if item.get("sendAsEmail") and item.get("verificationStatus") == "accepted"]))
        record.update(senders_status="verified", senders_checked_at=time.time())
    except (AccountError, httpx.HTTPError, ValueError, AttributeError, TypeError):
        record["senders"] = list(dict.fromkeys([address] + list((previous or {}).get("senders") or [])))
        record["senders_status"] = "last_known" if previous else "unavailable_with_current_grant"
        record["senders_checked_at"] = (previous or {}).get("senders_checked_at")
    if (previous or {}).get("sender") in record["senders"]:
        record["sender"] = previous["sender"]
    account = store.save(record)
    return {"ok": True, "account_id": account_id, "address": address, "account": store.public(account),
            "message": "Mailbox connected. Local disconnect preserves mail history; revoking Google project access can also disconnect Calendar."}
