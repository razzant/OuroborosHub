"""User OAuth: host-managed sign-in or the existing manual Settings connection.

An ordinary renewal may prove that a legacy grant fits the skill's declaration. Claim
its one transfer before sending it to the host; only the host provides bearers after a
confirmed move. An uncertain transfer does not reopen Settings. The marker has no secrets.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import mkstemp
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4

try:
    from .auth import DIRECT_BEARER_CONSENT_ERROR, UserRefreshError, get_user_access_token
    from .host_sign_in import ABSENT, HANDOVER_TIMEOUT, NO_HOST, READY, SERVICE, UNREACHABLE, HostError, HostSignIn
except ImportError:
    from auth import DIRECT_BEARER_CONSENT_ERROR, UserRefreshError, get_user_access_token
    from host_sign_in import ABSENT, HANDOVER_TIMEOUT, NO_HOST, READY, SERVICE, UNREACHABLE, HostError, HostSignIn

# The permissions this skill's tools use, as Google states them. calendar.events covers reading
# AND changing events; the broader calendar scope is a service-account concern, not user OAuth.
SCOPES = {
    "drive": "https://www.googleapis.com/auth/drive",
    "documents": "https://www.googleapis.com/auth/documents",
    "spreadsheets": "https://www.googleapis.com/auth/spreadsheets",
    "presentations": "https://www.googleapis.com/auth/presentations",
    "calendar.events": "https://www.googleapis.com/auth/calendar.events",
}
REQUIRED_SCOPES = ("openid", "email")
OPTIONAL_SCOPES = (SCOPES["drive"], SCOPES["documents"], SCOPES["spreadsheets"],
                   SCOPES["presentations"], SCOPES["calendar.events"])
SCOPE_SPELLINGS = {"email": ("https://www.googleapis.com/auth/userinfo.email",)}
IDENTITY_SCOPES = frozenset((*REQUIRED_SCOPES, *SCOPE_SPELLINGS["email"]))
HANDOVER_SCOPES = IDENTITY_SCOPES.union(OPTIONAL_SCOPES)
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 3600
LEGACY_WAIT = ("The existing Settings sign-in remains in use. It can move only when a normal renewal "
               "reports permissions that fit Google Workspace.")
SETTINGS_KEY = "DEFAULT_GOOGLE_ACCOUNT"
MARKER_NAME = "google_sign_in.json"
WITHDRAWN_NAME = "sign_in_withdrawn.json"
LEGACY_KEY = "settings"
CARD = "Extensions › Skills"
HOST_DID_NOT_ANSWER = "Ouroboros's sign-in service did not answer."
TERMINAL_HANDOVER_REFUSALS = ("account_disconnected", "refresh_rejected")


class UserOAuthError(RuntimeError):
    """The user OAuth route cannot serve this call; the text carries the owner's next step."""


class SettingsStateUnknownError(UserOAuthError):
    """Granted Settings Secrets could not be confirmed readable or absent."""


class AccountStateUnknownError(UserOAuthError):
    """The host's account or transfer state could not be checked."""


class Route:
    """What the user OAuth route decided for one call."""

    def __init__(self, kind: str, provider: Optional[Callable[[Optional[str]], str]] = None,
                 source: str = "", message: str = ""):
        self.kind = kind            # "host" or "settings"
        self.provider = provider    # (rejected bearer) -> access token, on the host path
        self.source = source        # "ouroboros" or "skill_settings"
        self.message = message
        self.facts: Dict[str, Any] = {}
        self.credentials: Dict[str, Optional[str]] = {}
        self.legacy_provider: Optional[Callable[..., str]] = None


def needed_scope(service: str, method: str = "GET") -> str:
    """The permission an operation needs, as the provider states it.

    No scope stands in for another: ``drive.readonly`` is not ``drive``. Calendar events use
    ``calendar.events`` for both reading and changing them; the method does not widen it to the
    broader ``calendar`` scope, which the user route never requests.
    """
    name = str(service or "").strip().lower()
    if name == "drive":
        return SCOPES["drive"]
    if name == "docs":
        return SCOPES["documents"]
    if name == "sheets":
        return SCOPES["spreadsheets"]
    if name == "slides":
        return SCOPES["presentations"]
    if name == "calendar":
        return SCOPES["calendar.events"]
    return ""


_SUFFICIENT_SCOPES = {
    SCOPES["documents"]: (SCOPES["documents"], SCOPES["drive"]),
    SCOPES["spreadsheets"]: (SCOPES["spreadsheets"], SCOPES["drive"]),
    SCOPES["presentations"]: (SCOPES["presentations"], SCOPES["drive"]),
}


def _accepted_scopes(scope: str) -> Tuple[str, ...]:
    word = str(scope or "").strip()
    return _SUFFICIENT_SCOPES.get(word, (word,) if word else ())


def _pick_required_scope(granted: Iterable[str], primary: str) -> str:
    accepted = _accepted_scopes(primary)
    if not accepted:
        return ""
    granted_set = set(granted)
    for candidate in accepted:
        if candidate in granted_set:
            return candidate
    return accepted[0]


def _selected_without_host_message(
    wanted: str,
    state: str,
    recorded: Optional[Dict[str, Any]] = None,
    withdrawn: Optional[List[Dict[str, Any]]] = None,
) -> str:
    host_note = ("this process has no Ouroboros sign-in service"
                 if state == NO_HOST else "this Ouroboros does not hold sign-ins")
    if recorded is not None and not _needs_settings_proof(recorded, withdrawn):
        return (f"Google account {wanted!r} is selected, but {host_note}. "
                "The Settings connection is not used in its place, and clearing the account choice "
                "will not reopen a retired or unconfirmed Settings connection. "
                "Use an Ouroboros that holds sign-ins.")
    return (f"Google account {wanted!r} is selected, but {host_note}. "
            "The Settings connection is not used in its place. "
            "Clear the account choice in the skill's settings to use Settings Secrets.")


def _log_warning(api, message: str) -> None:
    try:
        api.log("warning", message)
    except Exception:
        pass


_CREDENTIAL_KEYS = ("access_token", "client_id", "client_secret", "refresh_token")


def _normalized_credentials(credentials: Optional[Dict[str, Optional[str]]]) -> Dict[str, Optional[str]]:
    raw = credentials or {}
    return {key: _clean_id(raw.get(key)) or None for key in _CREDENTIAL_KEYS}


def _settings_changed_message() -> str:
    return (
        "Google OAuth credentials in Settings changed while this command was in progress, "
        "so this operation stopped. Check whether any earlier write already took effect "
        "before starting a new command."
    )


def _make_credentials_reader(
    credentials: Optional[Dict[str, Optional[str]]],
    credentials_reader: Optional[Callable[[], Tuple[Dict[str, Optional[str]], str]]],
) -> Callable[[], Tuple[Dict[str, Optional[str]], str]]:
    if credentials_reader is not None:
        def reader() -> Tuple[Dict[str, Optional[str]], str]:
            raw, cred_state = credentials_reader()
            return _normalized_credentials(raw), cred_state
        return reader
    snapshot = _normalized_credentials(credentials)
    return lambda: (dict(snapshot), "ok")


def _rotation_warning(base: str, *, has_card: bool) -> str:
    repair = (
        f"Sign in on the skill's card in {CARD} and select and save that account in the skill's settings, "
        "or obtain a new working refresh token and enter it as GOOGLE_OAUTH_REFRESH_TOKEN in Settings → Secrets "
        "(do not send secrets in chat)."
        if has_card else
        "Obtain a new working refresh token and enter it as GOOGLE_OAUTH_REFRESH_TOKEN in Settings → Secrets "
        "(do not send secrets in chat)."
    )
    return (
        f"{base} Google replaced the refresh token in this exchange; the skill cannot write to Settings Secrets "
        "and does not store refresh tokens in skill state. Only the newly issued access token can be used while "
        "it remains valid in this call; a later renewal or new process will not inherit the replacement refresh "
        f"token or in-memory cache. {repair}"
    )


def _settings_only_route(
    api,
    read_credentials: Callable[[], Tuple[Dict[str, Optional[str]], str]],
    credentials: Dict[str, Optional[str]],
    state: str,
) -> Route:
    base = ("This process has no Ouroboros sign-in service; the skill uses its own Settings route."
            if state == NO_HOST else
            "This Ouroboros does not hold sign-ins; the skill uses its own Settings route.")
    pinned_creds = _normalized_credentials(credentials)
    effective_creds = _settings_token_credentials(_marker(api), pinned_creds)
    route = Route("settings", source="skill_settings", message=base)
    route.credentials = dict(effective_creds)
    route.facts["refresh_configured"] = _has_refresh(effective_creds)
    if not _has_refresh(effective_creds) and effective_creds.get("access_token"):
        route.facts["sign_in_message"] = (
            f"{base} A short-lived Settings access token is in use without usable refresh credentials; "
            "it works only until it expires."
        )

    def check_settings() -> Dict[str, Any]:
        if selected_account(api):
            raise UserOAuthError("The selected Google account changed. Check whether any earlier write already took effect before starting a new command.")
        latest = _marker(api)
        withdrawn = _withdrawn_entries(api)
        if not _needs_settings_proof(latest, withdrawn):
            raise UserOAuthError(_transition_message(latest, withdrawn, host_state=state))
        current_creds, cred_state = read_credentials()
        if cred_state != "ok":
            raise SettingsStateUnknownError(_settings_unknown_message(cred_state, host_ready=False))
        if _normalized_credentials(current_creds) != pinned_creds:
            raise UserOAuthError(_settings_changed_message())
        if not _has_settings(pinned_creds) or not _legacy_allowed(latest, withdrawn, pinned_creds):
            raise UserOAuthError(_transition_message(latest, withdrawn, host_state=state))
        if _settings_token_credentials(latest, pinned_creds) != effective_creds:
            raise UserOAuthError(_settings_changed_message())
        return latest

    def renewed(tokens: Dict[str, Any]) -> str:
        check_settings()  # Only an actual exchange reaches this callback; cache hits do not.
        if tokens["refresh_token"] != effective_creds.get("refresh_token"):
            warning = _rotation_warning(base, has_card=False)
            route.facts["sign_in_message"] = warning
            route.facts["sign_in_notice"] = warning
            _log_warning(api, warning)
        return tokens["access_token"]

    def provide(http_client, rejected, *, direct_committed: bool = False,
                on_source: Optional[Callable[[str], None]] = None):
        with _transition_lock(api):
            latest = check_settings()
            if direct_committed and rejected is not None and _has_refresh(effective_creds):
                raise UserOAuthError(DIRECT_BEARER_CONSENT_ERROR)
            try:
                return get_user_access_token(
                    **effective_creds, http_client=http_client, force_refresh=rejected is not None,
                    on_refresh=renewed, allow_refresh=not direct_committed, on_source=on_source,
                )
            except UserRefreshError as exc:
                if not exc.rejected:
                    raise
                failed = _refresh_refusal(latest, pinned_creds)
                _write_marker(api, failed)
                raise UserOAuthError(_transition_message(failed, host_state=state)) from None

    route.legacy_provider = provide
    return route


def _settings_during_host_outage(api, read_credentials, wanted: str):
    if wanted:
        raise AccountStateUnknownError(
            f"{HOST_DID_NOT_ANSWER} The selected Google account is unavailable; Settings is not used in its place."
        )
    recorded, withdrawn = _marker(api), _withdrawn_entries(api)
    if not _needs_settings_proof(recorded, withdrawn):
        raise AccountStateUnknownError(f"{HOST_DID_NOT_ANSWER} {_transition_message(recorded, withdrawn)}")
    creds, cred_state = read_credentials()
    if cred_state != "ok":
        raise SettingsStateUnknownError(
            f"{HOST_DID_NOT_ANSWER} {_settings_unknown_message(cred_state, host_ready=False)}"
        )
    if not _use_legacy("", creds, recorded, withdrawn):
        raise AccountStateUnknownError(f"{HOST_DID_NOT_ANSWER} No eligible Settings connection was confirmed. Try again when the host answers.")
    return recorded, creds


def user_route(api, credentials: Optional[Dict[str, Optional[str]]] = None, scope: str = "", *,
               credentials_reader: Optional[Callable[[], Tuple[Dict[str, Optional[str]], str]]] = None) -> Route:
    """The route for one call: the host's sign-in, or the skill's own Settings route."""
    read_credentials = _make_credentials_reader(credentials, credentials_reader)
    host = HostSignIn()
    state = host.state()
    if state == READY:
        return _host_route(api, host, read_credentials, scope)
    if state == UNREACHABLE:
        recorded, creds = _settings_during_host_outage(api, read_credentials, selected_account(api))
        return _legacy_route(api, host, read_credentials, creds, scope, recorded)
    if state in (ABSENT, NO_HOST):
        wanted = selected_account(api)
        recorded = _marker(api)
        withdrawn = _withdrawn_entries(api)
        if wanted:
            raise UserOAuthError(_selected_without_host_message(wanted, state, recorded, withdrawn))
        if not _needs_settings_proof(recorded, withdrawn):
            raise UserOAuthError(_transition_message(recorded, withdrawn, host_state=state))
        creds, cred_state = read_credentials()
        if cred_state in ("unknown", "access_closed"):
            raise SettingsStateUnknownError(_settings_unknown_message(cred_state, host_ready=False))
        if not _legacy_allowed(recorded, withdrawn, creds):
            raise UserOAuthError(_transition_message(recorded, withdrawn, host_state=state))
        return _settings_only_route(api, read_credentials, creds, state)
    raise UserOAuthError(
        "Ouroboros's sign-in service did not answer, so the user OAuth route is undecided. "
        "No credential was used and nothing was guessed; try again when the host answers."
    )


def account_report(api, credentials: Optional[Dict[str, Optional[str]]] = None, *,
                   credentials_reader: Optional[Callable[[], Tuple[Dict[str, Optional[str]], str]]] = None) -> Dict[str, Any]:
    """What Ouroboros holds for this skill, and the account the skill would use."""
    read_credentials = _make_credentials_reader(credentials, credentials_reader)
    host = HostSignIn()
    state = host.state()
    report: Dict[str, Any] = {"state": state, "source": "", "accounts": [],
                              "selected": selected_account(api), "used": "", "message": ""}
    if state == READY:
        if not report["selected"]:
            recorded, withdrawn = _reconcile_saved(api, host)
        accounts = _host_accounts(host)
        report["accounts"] = [{key: row.get(key) for key in
                                ("key", "label", "subject", "verified", "state", "reason", "scope", "client_label")
                                if key in row} for row in accounts]
        if report["selected"]:
            selection = _selection(accounts, report["selected"])
        else:
            if recorded.get("state") == "moved" and not _clean_id(recorded.get("connection_id")):
                recorded, withdrawn = _reconcile_saved(api, host, accounts)
            if not _needs_settings_proof(recorded, withdrawn):
                selection = _host_selection(accounts, "", recorded, withdrawn)
                if not selection.get("reason") and _move_awaits_implicit_choice(recorded):
                    try:
                        _confirm_implicit_move(recorded, read_credentials)
                    except UserOAuthError as exc:
                        report["message"] = str(exc)
                        return report
            else:
                creds, cred_state = read_credentials()
                if cred_state != "ok":
                    report["message"] = _settings_unknown_message(cred_state, host_ready=True)
                    return report
                if _use_legacy("", creds, recorded, withdrawn):
                    report.update(source="skill_settings", used=LEGACY_KEY, message=_legacy_message(recorded, creds, host))
                    return report
                selection = _host_selection(accounts, "", recorded, withdrawn, creds)
        report["source"] = "ouroboros"
        report["used"] = selection.get("used", "")
        report["message"] = selection.get("reason") or "Ouroboros holds the selected Google account for this skill."
        return report
    if state == UNREACHABLE:
        try:
            recorded, creds = _settings_during_host_outage(api, read_credentials, report["selected"])
        except UserOAuthError as exc:
            report["message"] = str(exc)
        else:
            report.update(source="skill_settings", used=LEGACY_KEY, message=_legacy_message(recorded, creds, host))
        return report
    recorded = _marker(api)
    withdrawn = _withdrawn_entries(api)
    if report["selected"]:
        report["message"] = _selected_without_host_message(report["selected"], state, recorded, withdrawn)
        return report
    if not _needs_settings_proof(recorded, withdrawn):
        report["message"] = _transition_message(recorded, withdrawn, host_state=state)
        return report
    creds, cred_state = read_credentials()
    if cred_state in ("unknown", "access_closed"):
        report["message"] = _settings_unknown_message(cred_state, host_ready=False)
        return report
    if not _legacy_allowed(recorded, withdrawn, creds):
        report["message"] = _transition_message(recorded, withdrawn, host_state=state)
        return report
    report["source"] = "skill_settings"
    report["used"] = LEGACY_KEY if _has_settings(creds) else ""
    report["message"] = ("This process has no Ouroboros sign-in service; the skill uses its own Settings route."
                          if state == NO_HOST else
                          "This Ouroboros does not hold sign-ins; the skill uses its own Settings route.")
    return report


def read_local_settings(api) -> Dict[str, Any]:
    """An absent document is new settings; an unreadable document is not."""
    try:
        data = json.loads(_settings_path(api).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise SettingsStateUnknownError("Google Workspace settings could not be read. Try again; no account choice was inferred.") from None
    if not isinstance(data, dict):
        raise SettingsStateUnknownError("Google Workspace settings must contain a JSON object; no account choice was inferred.")
    return data


def selected_account(api) -> str:
    """The account the owner chose, if any."""
    value = read_local_settings(api).get(SETTINGS_KEY, "")
    if not isinstance(value, str):
        raise UserOAuthError("The saved Google account choice is not text. Save an account choice in the skill's settings.")
    return value.strip()


def save_local_settings(api, changes: Dict[str, str]) -> None:
    """Merge either settings form without replacing the other form's choices."""
    try:
        with _transition_lock(api):
            data = read_local_settings(api)
            data.update(changes)
            _atomic_write(_settings_path(api), json.dumps(data, indent=2))
    except OSError:
        raise UserOAuthError("Google Workspace settings could not be saved. Try again.") from None


def save_selected_account(api, value: str) -> str:
    chosen = value.strip()
    save_local_settings(api, {SETTINGS_KEY: chosen})
    return chosen


def _host_route(
    api,
    host: HostSignIn,
    read_credentials: Callable[[], Tuple[Dict[str, Optional[str]], str]],
    scope: str,
) -> Route:
    wanted = selected_account(api)
    if not wanted:
        recorded, withdrawn = _reconcile_saved(api, host)
        if _needs_settings_proof(recorded, withdrawn):
            with _transition_lock(api):
                wanted = selected_account(api)
                if not wanted:
                    recorded = _marker(api)
                    withdrawn = _withdrawn_entries(api)
                    if _needs_settings_proof(recorded, withdrawn):
                        creds, cred_state = read_credentials()
                        if cred_state != "ok":
                            raise SettingsStateUnknownError(_settings_unknown_message(cred_state, host_ready=True))
                        if _use_legacy("", creds, recorded, withdrawn):
                            return _legacy_route(api, host, read_credentials, creds, scope, recorded)
                        if not _legacy_allowed(recorded, withdrawn, creds):
                            raise UserOAuthError(_transition_message(recorded, withdrawn))
                    elif recorded.get("state") not in ("pending", "moved"):
                        raise UserOAuthError(_transition_message(recorded, withdrawn))
        elif recorded.get("state") not in ("pending", "moved"):
            raise UserOAuthError(_transition_message(recorded, withdrawn))
    accounts = _host_accounts(host)
    if wanted:
        selection = _selection(accounts, wanted)
    else:
        recorded = _marker(api)
        withdrawn = _withdrawn_entries(api)
        if recorded.get("state") == "moved" and not _clean_id(recorded.get("connection_id")):
            recorded, withdrawn = _reconcile_saved(api, host, accounts)
        if _move_awaits_implicit_choice(recorded):
            with _transition_lock(api):
                wanted = selected_account(api)
                if not wanted:
                    recorded = _marker(api)
                    withdrawn = _withdrawn_entries(api)
                    if (_move_awaits_implicit_choice(recorded)
                            and not _host_selection(accounts, "", recorded, withdrawn).get("reason")):
                        recorded = _confirm_implicit_move(recorded, read_credentials)
                        _write_marker(api, recorded)
        if wanted:
            selection = _selection(accounts, wanted)
        elif not _needs_settings_proof(recorded, withdrawn):
            selection = _host_selection(accounts, "", recorded, withdrawn)
        else:
            creds, cred_state = read_credentials()
            if cred_state != "ok":
                raise SettingsStateUnknownError(_settings_unknown_message(cred_state, host_ready=True))
            if _use_legacy("", creds, recorded, withdrawn):
                return _legacy_route(api, host, read_credentials, creds, scope, recorded)
            selection = _host_selection(accounts, "", recorded, withdrawn, creds)
    if selection.get("reason"):
        error = (AccountStateUnknownError if not wanted and recorded.get("state") == "pending"
                 else UserOAuthError)
        raise error(selection["reason"])
    required = _pick_required_scope(selection.get("scope") or (), scope)
    provider, facts = _provider(host, selection.get("account", ""), required,
                                connection_id=selection.get("connection_id", ""),
                                consent=selection.get("consent", ""))
    route = Route("host", provider=provider, source="ouroboros",
                  message="Ouroboros holds this skill's Google sign-in.")
    route.facts = facts
    return route


def _provider(host: HostSignIn, account: str, scope: str, *, min_valid: int = 120,
              connection_id: str = "", consent: str = "") -> Tuple[Callable[[Optional[str]], str], Dict[str, Any]]:
    facts: Dict[str, Any] = {}
    pinned_account = _clean_id(account)
    pinned_cid = _clean_id(connection_id)
    pinned_consent = _clean_id(consent)
    require_consent = host.handover_version >= 2
    pinned_bearer = ""

    def provide(rejected: Optional[str] = None) -> str:
        nonlocal pinned_bearer
        if not pinned_account or not pinned_cid:
            raise UserOAuthError(
                "This Google account in Ouroboros has no confirmed connection. "
                f"Sign in on the skill's card in {CARD}, then select and save that account in the skill's settings."
            )
        if require_consent and not pinned_consent:
            raise UserOAuthError(
                "This Google account in Ouroboros has no confirmed consent reference. "
                f"Sign in on the skill's card in {CARD} and select and save that account in the skill's settings."
            )
        try:
            body = host.token(pinned_account, connection_id=pinned_cid, consent=pinned_consent,
                              rejected=rejected, required_scope=scope, min_valid=min_valid)
        except HostError as exc:
            raise UserOAuthError(_host_message(exc, scope)) from None
        token = str(body.get("access_token") or "")
        if not token:
            raise UserOAuthError("Ouroboros's sign-in service answered without an access token.")
        returned_account = body.get("account") if isinstance(body.get("account"), dict) else {}
        if _clean_id(returned_account.get("key")) != pinned_account:
            raise UserOAuthError(_connection_changed_message())
        if _clean_id(body.get("connection_id")) != pinned_cid:
            raise UserOAuthError(_connection_changed_message())
        if (require_consent or pinned_consent) and _clean_id(body.get("consent")) != pinned_consent:
            raise UserOAuthError(_connection_changed_message())
        if not require_consent and not pinned_consent:
            if pinned_bearer and token != pinned_bearer:
                raise UserOAuthError(
                    "This Ouroboros (sign-in v1) does not provide consent continuity and issued a different "
                    "access token mid-operation, so this operation stopped. Check whether any earlier write "
                    "already took effect before starting a new command."
                )
            pinned_bearer = token
        facts["account"] = dict(returned_account)
        facts["granted_scope"] = str(body.get("scope") or "").split()
        return token

    return provide, facts


def _host_accounts(host: HostSignIn) -> List[Dict[str, Any]]:
    try:
        rows = host.accounts()
    except HostError as exc:
        raise AccountStateUnknownError(_host_message(exc)) from None
    except (OSError, ValueError) as exc:
        raise AccountStateUnknownError(f"Ouroboros's sign-in service could not be read ({exc}); nothing is guessed.") from None
    if rows is None:
        raise AccountStateUnknownError(
            "Ouroboros's sign-in service does not list a Google service for this skill. "
            "Nothing is guessed; the skill's own route stays closed on this Ouroboros."
        )
    return rows


def _has_refresh(credentials: Dict[str, Optional[str]]) -> bool:
    return all(credentials.get(key) for key in ("client_id", "client_secret", "refresh_token"))


def _has_settings(credentials: Dict[str, Optional[str]]) -> bool:
    return bool(credentials.get("access_token")) or _has_refresh(credentials)


def _clean_id(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _refresh_fingerprint(credentials: Optional[Dict[str, Optional[str]]]) -> str:
    if not credentials or not _has_refresh(credentials):
        return ""
    payload = json.dumps(
        [credentials["client_id"], credentials["client_secret"], credentials["refresh_token"]],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _direct_fingerprint(credentials: Dict[str, Optional[str]]) -> str:
    access = _clean_id(credentials.get("access_token"))
    return hashlib.sha256(access.encode("utf-8")).hexdigest() if access else ""


def _refresh_refusal(recorded: Dict[str, Any], credentials: Dict[str, Optional[str]]) -> Dict[str, Any]:
    return {**recorded, "state": "refresh_rejected", "refresh_fingerprint": _refresh_fingerprint(credentials),
            "direct_fingerprint": _direct_fingerprint(credentials)}


def _new_direct_after_refusal(recorded: Dict[str, Any], credentials: Dict[str, Optional[str]]) -> bool:
    if recorded.get("state") != "refresh_rejected" or not recorded.get("refresh_fingerprint"):
        return False
    refresh = _refresh_fingerprint(credentials)
    direct = _direct_fingerprint(credentials)
    return ((not refresh or refresh == recorded["refresh_fingerprint"])
            and "direct_fingerprint" in recorded and bool(direct)
            and direct != recorded["direct_fingerprint"])


def _settings_token_credentials(recorded: Dict[str, Any], credentials: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
    if _new_direct_after_refusal(recorded, credentials):
        return {key: credentials.get(key) if key == "access_token" else None for key in _CREDENTIAL_KEYS}
    return credentials


def _settings_fingerprint(credentials: Optional[Dict[str, Optional[str]]]) -> str:
    if not credentials:
        return ""
    norm = _normalized_credentials(credentials)
    payload = json.dumps([norm[key] or "" for key in _CREDENTIAL_KEYS], separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_connect_refused(exc: BaseException) -> bool:
    return (isinstance(exc, ConnectionRefusedError)
            or (isinstance(exc, urllib.error.URLError)
                and not isinstance(exc, urllib.error.HTTPError)
                and isinstance(exc.reason, ConnectionRefusedError)))


def _handover_fact(payload: Any, generation: str, *, source: str) -> Tuple[str, Dict[str, Any]]:
    """Interpret only the answer to our exact receipt query or our sole submitted offer."""
    if not (generation and isinstance(payload, dict) and payload.get("ok") is True
            and payload.get("service") in (None, SERVICE)):
        return "", {}
    if source == "receipt":
        row = payload.get("receipt")
    else:
        grants = payload.get("grants")
        row = grants[0] if isinstance(grants, list) and len(grants) == 1 else None
    if not (isinstance(row, dict) and row.get("service") in (None, SERVICE)
            and row.get("legacy_key") == LEGACY_KEY):
        return "", {}
    echoed = row.get("legacy_generation")
    if echoed not in (None, generation):
        return "", {}
    outcome = row.get("result")
    if outcome in ("imported", "already_present", "kept_newer"):
        account = row.get("account")
        if (echoed == generation and isinstance(account, dict) and _clean_id(account.get("key"))
                and _clean_id(row.get("connection_id"))):
            return "moved", row
        return "", {}
    if outcome != "refused":
        return "", {}
    reason, exchange = row.get("reason"), row.get("exchange")
    if reason in TERMINAL_HANDOVER_REFUSALS:
        # The addressed read may omit the generation for these final refusals.
        # The sole grant reply is also bound by the request; an account list is neither.
        return reason, row
    if source == "receipt" and reason == "provider_approval_required" and exchange == "unknown":
        return reason, row
    if source == "submission" or (source == "receipt" and echoed == generation):
        if exchange == "none":
            return "kept_legacy", row
        if exchange == "resubmit":
            return "resubmit", row
    if source == "submission" and exchange == "saved":
        return "recover", row
    return "", {}


def _kept_legacy_marker(generation: str, reason: Any, fp: str, *, prior_attempts: int = 0) -> Dict[str, Any]:
    attempts = max(0, prior_attempts) + 1
    delay = min(RETRY_BASE_SECONDS * (2 ** min(attempts - 1, 6)), RETRY_MAX_SECONDS)
    return {
        "state": "kept_legacy",
        "generation": generation,
        "reason": str(reason or "refused"),
        "credential_fingerprint": fp,
        "attempts": attempts,
        "retry_after": time.time() + delay,
    }


def _settled_marker(attempt: Dict[str, Any], state: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """Record the historic outcome without deciding whether a new or old command can use it."""
    if state == "moved":
        return _moved_marker(attempt, _clean_id(row["account"]["key"]), _clean_id(row["connection_id"]), row["result"])
    if state == "kept_legacy":
        prior = attempt.get("prior_attempts")
        marker = _kept_legacy_marker(attempt["generation"], row.get("reason"),
                                     _clean_id(attempt.get("refresh_fingerprint")),
                                     prior_attempts=prior if isinstance(prior, int) else 0)
        return {**marker, "settings_fingerprint": _clean_id(attempt.get("settings_fingerprint"))}
    return {**attempt, "state": state, "reason": _clean_id(row.get("reason"))}


def _is_withdrawn(recorded: Dict[str, Any], entries: List[Dict[str, Any]]) -> bool:
    generation = _clean_id(recorded.get("generation"))
    cid = _clean_id(recorded.get("connection_id"))
    for entry in entries:
        if entry.get("service") != SERVICE:
            continue
        entry_cid = _clean_id(entry.get("connection_id"))
        if cid and entry_cid == cid:
            return True
        entry_gen = _clean_id(entry.get("legacy_generation"))
        if generation and entry_gen == generation:
            return True
        if entry.get("legacy_key") == LEGACY_KEY and (not cid or not entry_cid or entry_cid == cid):
            return True
    return False


def _legacy_allowed(recorded: Dict[str, Any], withdrawn: Optional[List[Dict[str, Any]]] = None,
                    credentials: Optional[Dict[str, Optional[str]]] = None) -> bool:
    if withdrawn and _is_withdrawn(recorded, withdrawn):
        return False
    # A draft marker with this outcome was written before sending anything.
    state = recorded.get("state")
    if not recorded or (state, recorded.get("reason")) == ("skipped", "scope_unconfirmed"):
        return True
    if state == "kept_legacy":
        if "settings_fingerprint" not in recorded:  # an already settled marker from an earlier skill
            return True
        expected = _clean_id(recorded.get("settings_fingerprint"))
        return bool(expected and credentials is not None and _settings_fingerprint(credentials) == expected)
    if state == "refresh_rejected":
        rejected_fp = _clean_id(recorded.get("refresh_fingerprint"))
        if not rejected_fp or credentials is None:
            return False
        refresh = _refresh_fingerprint(credentials)
        return bool((refresh and refresh != rejected_fp) or _new_direct_after_refusal(recorded, credentials))
    return False


def _needs_settings_proof(recorded: Dict[str, Any], withdrawn: Optional[List[Dict[str, Any]]] = None) -> bool:
    if withdrawn and _is_withdrawn(recorded, withdrawn):
        return False
    state = recorded.get("state")
    if not recorded or state == "kept_legacy" or (state, recorded.get("reason")) == ("skipped", "scope_unconfirmed"):
        return True
    if state == "refresh_rejected":
        return bool(_clean_id(recorded.get("refresh_fingerprint")))
    return False


def _use_legacy(wanted, credentials, recorded, withdrawn=None) -> bool:
    return not wanted and _has_settings(credentials) and _legacy_allowed(recorded, withdrawn, credentials)


def _settings_unknown_message(cred_state: str, *, host_ready: bool) -> str:
    if cred_state == "access_closed":
        if host_ready:
            return ("Google Workspace cannot read its granted Settings Secrets right now (access closed or "
                    "ungranted), so no implicit host account was chosen. Grant the skill's Google OAuth "
                    f"Settings Secrets, or sign in on the skill's card in {CARD} and select and save that "
                    "account in the skill's settings.")
        return "Google Workspace cannot read its granted Settings Secrets right now (access closed or ungranted); no credential was used."
    if host_ready:
        if cred_state == "unverified":
            return ("This Ouroboros could not confirm whether granted Settings Secrets hold a Google OAuth "
                    "connection (get_settings_state is unavailable), so no implicit host account was chosen. "
                    "Select and save an account in the skill's settings.")
        return ("Google Workspace could not read Settings Secrets right now, so no implicit host account was "
                "chosen. Try again, or select and save an account in the skill's settings.")
    return "Google Workspace could not read Settings Secrets right now, so no credential was used. Try again."


def _legacy_message(recorded: Dict[str, Any], credentials: Optional[Dict[str, Optional[str]]] = None,
                    host: Optional[HostSignIn] = None) -> str:
    switch_step = f"sign in on the skill's card in {CARD}, then select and save that account in the skill's settings"
    if credentials is not None:
        credentials = _settings_token_credentials(recorded, credentials)
    if host is not None and host.state() == UNREACHABLE:
        note = f"{HOST_DID_NOT_ANSWER} Settings is still in use; the host's accounts are unknown."
        if credentials is not None and not _has_refresh(credentials):
            note += " The Settings access token works only until it expires."
        return note
    if credentials is not None and not _has_refresh(credentials):
        return ("A short-lived Settings access token is in use without refresh credentials. "
                f"It works until it expires and cannot renew or move automatically; {switch_step}.")
    if recorded.get("state") == "kept_legacy":
        reason = _clean_id(recorded.get("reason"))
        retry_note = " It can try again on a later ordinary renewal."
        if reason == "host_unreachable":
            return ("Ouroboros's sign-in service refused the local connection before starting the transfer. "
                    f"Settings is still in use.{retry_note} To switch now, {switch_step}.")
        if reason and reason != "unknown_client":
            return f"Ouroboros refused the transfer before taking the grant ({reason}). Settings is still in use.{retry_note} To switch now, {switch_step}."
        return f"Ouroboros refused the old client before taking the grant. Settings is still in use.{retry_note} To switch now, {switch_step}."
    if host is not None and host.handover_version < 2:
        return ("This Ouroboros does not yet support confirmed Settings handover. "
                f"Settings is still in use; {switch_step}.")
    return LEGACY_WAIT


def _connection_changed_message() -> str:
    return ("The Google connection or consent changed or was disconnected in Ouroboros. "
            "The Settings copy is not used. Check whether any earlier write already took effect before "
            f"starting a new command. Sign in on the skill's card in {CARD} if needed, then select and "
            "save that account in the skill's settings.")


def _transition_message(recorded, withdrawn: Optional[List[Dict[str, Any]]] = None, *,
                        host_state: str = READY) -> str:
    state = recorded.get("state")
    if withdrawn and _is_withdrawn(recorded, withdrawn):
        state = "account_disconnected"
    detail = {
        "pending": "The transfer's outcome has not been confirmed.",
        "kept_legacy": "The transfer stopped before exchange, but the Settings connection no longer matches the offer.",
        "resubmit": "The transfer stopped before exchange, but the old Settings connection cannot be used.",
        "refresh_rejected": "Google rejected the old Settings refresh grant.",
        "account_disconnected": "The Google account was disconnected in Ouroboros.",
        "provider_approval_required": "The saved transfer needs provider approval under the current skill declaration.",
    }.get(state, "The old Settings sign-in is no longer the active route.")
    has_rejected_fp = state == "refresh_rejected" and bool(_clean_id(recorded.get("refresh_fingerprint")))
    if host_state in (ABSENT, NO_HOST):
        if has_rejected_fp:
            return (f"{detail} That rejected Settings copy is not used. "
                    "In Settings → Secrets, enter a new working refresh credential or a new short-lived access token for the next command (do not send secrets in chat).")
        if state == "refresh_rejected":
            return (f"{detail} This older rejection record does not identify which Settings credentials were rejected, "
                    "so updating Settings Secrets alone cannot reopen the Settings route on an Ouroboros without sign-ins. "
                    "Use an Ouroboros that holds sign-ins to connect a Google account.")
        return (f"{detail} This Ouroboros does not hold sign-ins, and the old Settings copy "
                "is not reopened automatically.")
    if has_rejected_fp:
        return (f"{detail} That rejected Settings copy is not used. Sign in on the skill's card in {CARD} "
                "and select and save that account in the skill's settings, or enter a new working refresh "
                "credential or a new short-lived access token for the next command in Settings → Secrets.")
    return (f"{detail} The Settings copy is not used. Sign in on the skill's card in {CARD} if needed, "
            "then select and save that account in the skill's settings.")


def _selected_row(row: Dict[str, Any]) -> Dict[str, Any]:
    key = _clean_id(row.get("key"))
    cid = _clean_id(row.get("connection_id"))
    consent = _clean_id(row.get("consent"))
    if row.get("verified") is not True or not key or not cid:
        return {"account": "", "used": "", "connection_id": "", "consent": "", "scope": [],
                "reason": ("This Google account in Ouroboros has no confirmed connection. "
                           f"Sign in on the skill's card in {CARD}, then select and save that account "
                           "in the skill's settings."),
                "facts": {}}
    if row.get("state") != "signed_in":
        return {"account": "", "used": "", "facts": {}, "reason": _host_sign_in_needed()}
    raw_scope = row.get("scope")
    granted = ([str(word) for word in raw_scope] if isinstance(raw_scope, (list, tuple))
               else str(raw_scope or "").split())
    return {"account": key, "used": key, "connection_id": cid, "consent": consent,
            "scope": granted, "reason": "", "facts": {}}


def _reconcile_saved(api, host: HostSignIn, accounts: Iterable[Dict[str, Any]] = ()):
    """Preserve addressed facts even when the account list or current Settings cannot be used."""
    receipts: Dict[str, Optional[Dict[str, Any]]] = {}

    def resolve(recorded, withdrawn):
        generation = _clean_id(recorded.get("generation"))
        receipt = None
        if (recorded.get("state") == "pending" and generation and host.handover_version >= 2
                and not _is_withdrawn(recorded, withdrawn)):
            if generation not in receipts:
                receipts[generation] = host.handover_receipt(LEGACY_KEY, generation)
            receipt = receipts[generation]
        return _reconciled(recorded, accounts, withdrawn, receipt)

    recorded, withdrawn = _marker(api), _withdrawn_entries(api)
    if resolve(recorded, withdrawn) == recorded:
        return recorded, withdrawn
    with _transition_lock(api):
        recorded, withdrawn = _marker(api), _withdrawn_entries(api)
        settled = resolve(recorded, withdrawn)
        if settled != recorded:
            _write_marker(api, settled)
        return settled, withdrawn


def _host_sign_in_needed() -> str:
    return f"This Google account needs attention. Sign in again on the skill's card in {CARD}."


def _moved_marker(recorded: Dict[str, Any], key: str, cid: str, result: str) -> Dict[str, Any]:
    # Keep the historic result, not a consent that a later command could reuse.
    return {"state": "moved", "generation": recorded["generation"], "account": {"key": key},
            "connection_id": cid, "handover_result": result,
            "settings_fingerprint": _clean_id(recorded.get("settings_fingerprint"))}


def _move_awaits_implicit_choice(recorded: Dict[str, Any]) -> bool:
    return recorded.get("state") == "moved" and "settings_fingerprint" in recorded


def _confirm_implicit_move(recorded: Dict[str, Any], read_credentials) -> Dict[str, Any]:
    expected = _clean_id(recorded.get("settings_fingerprint"))
    if expected:
        creds, cred_state = read_credentials()
        if cred_state != "ok":
            raise SettingsStateUnknownError(
                f"The transfer was recorded. {_settings_unknown_message(cred_state, host_ready=True)}"
            )
        if _has_settings(creds) and _settings_fingerprint(creds) == expected:
            return {key: value for key, value in recorded.items() if key != "settings_fingerprint"}
    raise UserOAuthError(
        "The transfer was recorded, but Settings no longer match the connection that was transferred. "
        "Select and save the Google account to use in the skill's settings."
    )


def _reconciled(recorded, accounts, withdrawn: Optional[List[Dict[str, Any]]] = None,
                receipt: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    generation = _clean_id(recorded.get("generation"))
    state = recorded.get("state")
    if state == "pending" and generation:
        if withdrawn and _is_withdrawn(recorded, withdrawn):
            return {**recorded, "state": "account_disconnected"}
        outcome, row = _handover_fact(receipt, generation, source="receipt")
        if outcome:
            settled = _settled_marker(recorded, outcome, row)
            if withdrawn and _is_withdrawn(settled, withdrawn):
                return {**settled, "state": "account_disconnected"}
            return settled
    if state == "moved" and generation and not _clean_id(recorded.get("connection_id")):
        if withdrawn and _is_withdrawn(recorded, withdrawn):
            return {**recorded, "state": "account_disconnected"}
        account = recorded.get("account")
        key = _clean_id(account.get("key") if isinstance(account, dict) else None)
        if key:
            matches = [row for row in accounts if row.get("legacy_generation") == generation
                       and row.get("via") == "handover" and row.get("key") == key
                       and row.get("verified") is True and _clean_id(row.get("connection_id"))]
            if len(matches) == 1:
                bound = {**recorded, "connection_id": _clean_id(matches[0].get("connection_id"))}
                if withdrawn and _is_withdrawn(bound, withdrawn):
                    return {**recorded, "state": "account_disconnected"}
                return bound
    return recorded


def _host_selection(accounts, wanted, recorded, withdrawn: Optional[List[Dict[str, Any]]] = None,
                    credentials: Optional[Dict[str, Optional[str]]] = None) -> Dict[str, Any]:
    if wanted:
        return _selection(accounts, wanted)
    if _legacy_allowed(recorded, withdrawn, credentials):
        return _selection(accounts, "")
    if recorded.get("state") != "moved":
        return {"account": "", "used": "", "reason": _transition_message(recorded, withdrawn)}
    if withdrawn and _is_withdrawn(recorded, withdrawn):
        return {"account": "", "used": "", "reason": _transition_message({"state": "account_disconnected"})}
    account = recorded.get("account")
    key = _clean_id(account.get("key") if isinstance(account, dict) else None)
    cid = _clean_id(recorded.get("connection_id"))
    if not accounts or not key or not cid:
        return {"account": "", "used": "", "reason": _transition_message(recorded, withdrawn)}
    matches = [row for row in accounts if row.get("key") == key and _clean_id(row.get("connection_id")) == cid]
    if len(matches) != 1:
        return {"account": "", "used": "", "reason": _connection_changed_message()}
    # The confirmed transfer preserves its verified connection even when another is listed.
    return _selected_row(matches[0])


def _selection(accounts: List[Dict[str, Any]], wanted: str) -> Dict[str, Any]:
    """Which account the skill uses: the owner's unambiguous choice, else the single usable one.

    An exact account key is unambiguous. A label or email must name exactly one account: two
    accounts sharing a label are never resolved by taking the first. Several usable accounts
    need an explicit choice; a single usable account may stay implicit (the host picks it).
    """
    rows = accounts
    if wanted:
        by_key = [row for row in rows if str(row.get("key") or "") == wanted]
        if len(by_key) == 1:
            return _selected_row(by_key[0])
        matches = [row for row in rows
                   if wanted in (str(row.get("label") or ""), str(row.get("subject") or ""))]
        if len(matches) == 1:
            return _selected_row(matches[0])
        if len(matches) > 1:
            return {"account": "", "used": "", "facts": {},
                    "reason": f"Several Google accounts match {wanted!r}; name the account by its "
                              f"key (workspace_accounts lists the keys)."}
        return {"account": "", "used": "", "facts": {},
                "reason": f"Ouroboros holds no Google account matching {wanted!r}. "
                          f"Run workspace_accounts to see the accounts it holds."}
    usable = [row for row in rows if str(row.get("state") or "") == "signed_in"]
    if len(usable) == 1:
        return _selected_row(usable[0])
    if not usable:
        reasons = sorted({str(row.get("reason") or "not signed in") for row in rows})
        detail = f" ({', '.join(reasons)})" if reasons else ""
        return {"account": "", "used": "", "facts": {},
                "reason": f"Ouroboros holds no signed-in Google account for this skill{detail}. "
                          f"Sign in on the skill's card in {CARD}."}
    return {"account": "", "used": "", "facts": {},
            "reason": "Ouroboros holds several Google accounts for this skill; run workspace_accounts "
                      "and save the chosen account's key in the skill's settings."}


def _legacy_route(
    api,
    host: HostSignIn,
    read_credentials: Callable[[], Tuple[Dict[str, Optional[str]], str]],
    credentials: Dict[str, Optional[str]],
    scope: str,
    recorded: Dict[str, Any],
) -> Route:
    pinned_creds = _normalized_credentials(credentials)
    effective_creds = _settings_token_credentials(recorded, pinned_creds)
    route = Route("settings", source="skill_settings", message=_legacy_message(recorded, pinned_creds, host))
    route.credentials = dict(effective_creds)
    route.facts["sign_in_message"] = route.message
    route.facts["refresh_configured"] = _has_refresh(effective_creds)
    hosted = None
    host_facts: Dict[str, Any] = {}

    def report_host_token(token):
        route.facts.update(host_facts, sign_in_source="ouroboros")
        route.facts.pop("sign_in_message", None)
        route.facts.pop("sign_in_notice", None)
        return token

    def verify_admitted(marker_doc: Dict[str, Any]) -> None:
        if selected_account(api):
            raise UserOAuthError("The selected Google account changed. Check whether any earlier write already took effect before starting a new command.")
        withdrawn = _withdrawn_entries(api)
        if _is_withdrawn(marker_doc, withdrawn):
            raise UserOAuthError(_transition_message({"state": "account_disconnected"}, withdrawn))
        now_creds, cred_state = read_credentials()
        if cred_state != "ok":
            raise SettingsStateUnknownError(_settings_unknown_message(cred_state, host_ready=True))
        if _normalized_credentials(now_creds) != pinned_creds:
            raise UserOAuthError(_settings_changed_message())

    def keep_settings(tokens, message, current_creds, *, verify_marker: Optional[Dict[str, Any]] = None):
        if verify_marker is not None:
            verify_admitted(verify_marker)
        if tokens["refresh_token"] != current_creds.get("refresh_token"):
            warning = _rotation_warning(message, has_card=host.state() == READY)
            route.facts["sign_in_message"] = warning
            route.facts["sign_in_notice"] = warning
            _log_warning(api, warning)
            return tokens["access_token"]
        route.facts["sign_in_message"] = message
        return tokens["access_token"]

    def renewed(tokens, latest, current_creds):
        nonlocal hosted, host_facts
        if host.state() != READY or host.handover_version < 2:
            return keep_settings(tokens, _legacy_message(latest, current_creds, host), current_creds, verify_marker=latest)
        fp = _refresh_fingerprint(current_creds)
        same_kept = (latest.get("state") == "kept_legacy"
                     and bool(_clean_id(latest.get("credential_fingerprint")))
                     and latest.get("credential_fingerprint") == fp)
        if same_kept:
            retry_after = latest.get("retry_after")
            if isinstance(retry_after, (int, float)) and time.time() < retry_after:
                return keep_settings(tokens, _legacy_message(latest, current_creds, host), current_creds, verify_marker=latest)
        reported = tokens.get("scope")
        words = reported.split() if isinstance(reported, str) else []
        if not words:
            return keep_settings(tokens, "Google did not report the permissions at renewal. Settings is still in use.", current_creds, verify_marker=latest)
        accepted = _accepted_scopes(scope)
        reported_scopes = set(words)
        if not IDENTITY_SCOPES.intersection(reported_scopes):
            return keep_settings(tokens, "Google did not report a permission to verify the account. Settings is still in use.", current_creds, verify_marker=latest)
        if (reported_scopes - HANDOVER_SCOPES
                or (accepted and not any(candidate in reported_scopes for candidate in accepted))
                or str(tokens["token_type"]).lower() != "bearer"
                or any(not 33 <= ord(char) <= 126 for char in tokens["access_token"])
                or tokens["expires_at"] <= time.time() + HANDOVER_TIMEOUT):
            return keep_settings(tokens, "The renewed token does not meet the transfer requirements. Settings is still in use.", current_creds, verify_marker=latest)
        grant_tokens = {key: tokens[key] for key in ("refresh_token", "access_token", "expires_at")}
        prior_attempts = int(latest.get("attempts") or 0) if same_kept else 0
        verify_admitted(latest)
        row = _handover(api, host, current_creds, {**grant_tokens, "scope": " ".join(words)},
                        prior_attempts=prior_attempts, verify_reopen=verify_admitted)
        if row is None:
            return keep_settings(tokens, _legacy_message(_marker(api), current_creds, host), current_creds)
        saved = _marker(api)
        verify_admitted(saved)
        if row.get("state") != "signed_in":
            raise UserOAuthError(_host_sign_in_needed())
        outcome = row.get("result")
        receipt_consent = _clean_id(row.get("consent"))
        if outcome == "kept_newer" or not receipt_consent:
            raise UserOAuthError(
                "Ouroboros already holds a confirmed Google sign-in for this account; the old Settings "
                "connection is retired and this operation stopped instead of switching mid-flight to "
                "another consent. Check whether any earlier write already took effect before starting "
                "a new command (which will use the host account, or choose another account in the "
                "skill's settings)."
            )
        account = row.get("account")
        key = _clean_id(account.get("key") if isinstance(account, dict) else None)
        cid = _clean_id(row.get("connection_id"))
        required = _pick_required_scope(words, scope)
        bound_provider, facts = _provider(host, key, required, min_valid=0,
                                          connection_id=cid, consent=receipt_consent)
        token = bound_provider(None)
        verify_admitted(saved)
        saved.pop("settings_fingerprint", None)
        _write_marker(api, saved)
        hosted, host_facts = bound_provider, facts
        return report_host_token(token)

    def provide(http_client, rejected, *, direct_committed: bool = False,
                on_source: Optional[Callable[[str], None]] = None):
        if hosted is not None:
            return report_host_token(hosted(rejected))
        with _transition_lock(api):
            if selected_account(api):
                raise UserOAuthError("The selected Google account changed. Check whether any earlier write already took effect before starting a new command.")
            latest = _marker(api)
            withdrawn = _withdrawn_entries(api)
            if not _needs_settings_proof(latest, withdrawn):
                raise UserOAuthError(_transition_message(latest, withdrawn))
            current_creds, cred_state = read_credentials()
            if cred_state != "ok":
                raise SettingsStateUnknownError(_settings_unknown_message(cred_state, host_ready=True))
            if _normalized_credentials(current_creds) != pinned_creds:
                raise UserOAuthError(_settings_changed_message())
            if not _has_settings(pinned_creds) or not _legacy_allowed(latest, withdrawn, pinned_creds):
                raise UserOAuthError(_transition_message(latest, withdrawn))
            if _settings_token_credentials(latest, pinned_creds) != effective_creds:
                raise UserOAuthError(_settings_changed_message())
            if direct_committed and rejected is not None and _has_refresh(effective_creds):
                raise UserOAuthError(DIRECT_BEARER_CONSENT_ERROR)
            if latest.get("state") == "kept_legacy":
                route.facts["sign_in_message"] = _legacy_message(latest, pinned_creds, host)
            try:
                return get_user_access_token(
                    **effective_creds, http_client=http_client, force_refresh=rejected is not None,
                    on_refresh=lambda tokens: renewed(tokens, latest, pinned_creds),
                    allow_refresh=not direct_committed, on_source=on_source)
            except UserRefreshError as exc:
                if not exc.rejected:
                    raise
                failed = _refresh_refusal(latest, pinned_creds)
                _write_marker(api, failed)
                raise UserOAuthError(_transition_message(failed)) from None

    route.legacy_provider = provide
    return route


def _handover(api, host: HostSignIn, credentials, tokens, *, prior_attempts: int = 0,
              verify_reopen: Optional[Callable[[Dict[str, Any]], None]] = None) -> Optional[Dict[str, Any]]:
    # Caller holds the transition lock. Persist the claim before sending. Never re-POST a normal
    # handover; at most one in-memory recover=True call may settle a lost reply or saved exchange.
    generation = uuid4().hex
    attempt: Dict[str, Any] = {
        "state": "pending", "generation": generation,
        "settings_fingerprint": _settings_fingerprint(credentials),
        "refresh_fingerprint": _refresh_fingerprint(credentials),
        "direct_fingerprint": _direct_fingerprint(credentials),
        **({"prior_attempts": prior_attempts} if prior_attempts > 0 else {}),
    }
    _write_marker(api, attempt)
    client_id = credentials["client_id"]
    grant = {"legacy_key": LEGACY_KEY, "legacy_generation": generation,
             "label": "Google account from Settings", "client_id": client_id,
             "require_verified": True, "tokens": tokens}
    clients = [{"client_id": client_id, "client_secret": credentials["client_secret"],
                "label_hint": "Google Workspace settings", "return_hint": "unknown"}]

    def record(outcome: str, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        saved = _settled_marker(attempt, outcome, row)
        _write_marker(api, saved)
        if outcome == "moved":
            return row
        if outcome == "kept_legacy":
            if _is_withdrawn(saved, _withdrawn_entries(api)):
                disconnected = {**saved, "state": "account_disconnected"}
                _write_marker(api, disconnected)
                raise UserOAuthError(_transition_message(disconnected))
            if verify_reopen is not None:
                verify_reopen(saved)
            return None
        raise UserOAuthError(_transition_message(saved))

    def lookup_doubt() -> Optional[Dict[str, Any]]:
        receipt = host.handover_receipt(LEGACY_KEY, generation)
        outcome, row = _handover_fact(receipt, generation, source="receipt")
        if not outcome:
            raise UserOAuthError(_transition_message(attempt))
        return record(outcome, row)

    try:
        answer = host.handover(clients, [grant])
    except HostError as exc:
        if exc.status == 429 and isinstance(exc.body, dict) and exc.body.get("handover_started") is False:
            return record("kept_legacy", {"reason": "rate_limited"})
        if exc.status < 500 and exc.status != 408:
            return lookup_doubt()
    except (OSError, ValueError) as exc:
        if _is_connect_refused(exc):
            return record("kept_legacy", {"reason": "host_unreachable"})
    else:
        outcome, row = _handover_fact(answer, generation, source="submission")
        if not outcome:
            return lookup_doubt()
        if outcome != "recover":
            return record(outcome, row)

    try:
        answer = host.handover(clients, [grant], recover=True)
    except (HostError, OSError, ValueError):
        return lookup_doubt()
    outcome, row = _handover_fact(answer, generation, source="recovery")
    return record(outcome, row) if outcome else lookup_doubt()


@contextmanager
def _transition_lock(api):
    # Serialize a legacy renewal/transfer with either settings form across extension
    # processes. SQLite releases the lock on exit; the JSON documents own their data.
    path = Path(api.get_state_dir()) / "google_sign_in.lock.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(path, timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE")
            yield
    except sqlite3.Error:
        raise UserOAuthError(
            "The Google Settings sign-in or local settings lock is busy (bounded 5 s wait) "
            "or unavailable. Try again."
        ) from None


def _marker(api) -> Dict[str, Any]:
    path = Path(api.get_state_dir()) / MARKER_NAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise UserOAuthError(f"The sign-in handover record could not be read ({exc}); nothing is guessed.") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise UserOAuthError("The sign-in handover record is unreadable; nothing is guessed.") from None
    recorded = data.get("handover") if isinstance(data, dict) else None
    if not isinstance(recorded, dict) or not isinstance(recorded.get("state"), str) or not recorded["state"]:
        raise UserOAuthError("The sign-in handover record is unreadable; nothing is guessed.")
    return recorded


def _write_marker(api, recorded: Dict[str, Any]) -> None:
    path = Path(api.get_state_dir()) / MARKER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps({"handover": recorded}, indent=2))


def _withdrawn_entries(api) -> List[Dict[str, Any]]:
    path = Path(api.get_state_dir()) / WITHDRAWN_NAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise UserOAuthError(f"The sign-in withdrawal record could not be read ({exc}); nothing is guessed.") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise UserOAuthError("The sign-in withdrawal record is unreadable; nothing is guessed.") from None
    accounts = data.get("accounts") if isinstance(data, dict) and data.get("v") == 1 else None
    if not isinstance(accounts, list) or any(not isinstance(item, dict) for item in accounts):
        raise UserOAuthError("The sign-in withdrawal record is unreadable; nothing is guessed.")
    return [item for item in accounts if item.get("service") == SERVICE]


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _settings_path(api) -> Path:
    return Path(api.get_state_dir()) / "settings.json"


def _host_message(exc: HostError, scope: str = "") -> str:
    body = exc.body if isinstance(exc.body, dict) else {}
    hint, error, reason = str(body.get("hint") or ""), str(body.get("error") or ""), str(body.get("reason") or "")
    if exc.status == 409 and reason in ("connection_changed", "consent_changed"):
        parts = [_connection_changed_message()]
    elif hint:
        parts = [hint]
    elif error:
        parts = [f"Ouroboros's sign-in service refused the request (HTTP {exc.status}): {error}"]
    else:
        parts = [f"Ouroboros's sign-in service refused the request (HTTP {exc.status})."]
    if body.get("note"):
        parts.append(str(body["note"]))
    if exc.status == 409 and scope and reason in ("", "insufficient_scope"):
        overlap = "; full Google Drive also covers this operation" if scope in _SUFFICIENT_SCOPES else ""
        parts.append(f"This account is not allowed to use this operation (scope {scope}{overlap}).")
    return " ".join(parts)
