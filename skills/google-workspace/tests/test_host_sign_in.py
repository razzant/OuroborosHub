"""Google Workspace against an in-memory fake Ouroboros sign-in service.

The core's Host Service sign-in routes are stood in by a plain object whose answers are
dispatched through ``HostSignIn._call`` — no network server. A fake Google answers the Drive
read and the token endpoint through ``httpx.MockTransport``. Every token is synthetic; nothing
leaves the machine and no real API is called.
"""

import asyncio
import hashlib
import json
import re
import sys
import urllib.error
from pathlib import Path

import httpx
import pytest
import yaml

HERE = Path(__file__).resolve().parent
for path in (str(HERE.parent), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

import auth  # noqa: E402
import client as client_module  # noqa: E402
import plugin  # noqa: E402
import user_oauth  # noqa: E402
from client import GoogleWorkspaceClient  # noqa: E402

SKILL_TOKEN = "synthetic-skill-token"
OWNER = "owner@example.test"
OTHER = "other@example.test"
CLIENT_ID = "settings-client"
CLIENT_SECRET = "settings-secret"
REFRESH = "settings-refresh-token"
ROTATED_REFRESH = "renewed-refresh-fixture"
LEGACY_ACCESS = "legacy-access-token"
HOST_ACCESS = "host-access-token"
GOOGLE_SCOPE = " ".join(["openid", "email", user_oauth.SCOPES["drive"], user_oauth.SCOPES["documents"],
                          user_oauth.SCOPES["spreadsheets"]])
SECRET_SHAPES = re.compile("|".join([REFRESH, ROTATED_REFRESH, CLIENT_SECRET, LEGACY_ACCESS, HOST_ACCESS, SKILL_TOKEN]))


def host_key(subject):
    return "a-" + hashlib.sha256(f"https://accounts.google.com\0{subject}".encode()).hexdigest()[:20]


def calls(host, path):
    return [body for method, route, body in host.requests if route == path]


class FakeHost:
    """The core's Host Service sign-in routes, answered in memory through HostSignIn._call."""

    def __init__(self, sign_in_version=1, handover_version=0, handover_subject="sub-handed-over"):
        self.sign_in_version = sign_in_version
        self.handover_version = handover_version
        self.handover_subject = handover_subject
        self.accounts = {}
        self.requests = []
        self.handover_answer = None
        self.handover_error = None
        self.receipts = {}
        self.identity_body = None
        self.accounts_status = 200
        self.token_expires_at = 4102444800.0
        self.on_handover = None
        self.on_token = None
        self._connections = 0

    def add(self, label, subject, state="signed_in", scope="", verified=True, via="card", consent=None):
        key = host_key(subject)
        self._connections += 1
        row = {"label": label, "subject": subject, "state": state, "scope": scope, "verified": verified, "via": via}
        if verified:
            row["connection_id"] = f"conn-{self._connections}"
            row["consent"] = f"{self._connections:032x}" if consent is None else consent
        self.accounts[key] = row
        return key

    def call(self, method, path, body=None):
        body = body or {}
        self.requests.append((method, path, body))
        if path == "/identity":
            if self.identity_body is not None:
                return self.identity_body
            version = {"sign_in_version": self.sign_in_version} if self.sign_in_version else {}
            if self.handover_version:
                version["sign_in_handover_version"] = self.handover_version
            return {"ok": True, "name": "Ouroboros", **version}
        if not self.sign_in_version:
            raise user_oauth.HostError(404, {"ok": False, "error": "Not Found"})
        if path == "/sign-in/accounts":
            if self.accounts_status != 200:
                raise user_oauth.HostError(self.accounts_status, {"ok": False, "error": "cannot list"})
            extra = ("via", "legacy_generation", "connection_id", "consent") if self.handover_version >= 2 else ("via", "legacy_generation", "connection_id")
            rows = [{"key": key, "label": row["label"], "subject": row["subject"], "verified": row.get("verified", True),
                     "state": row["state"], "scope": row.get("scope", ""), "client_label": "Personal",
                     **{name: row[name] for name in extra if name in row and row[name] != ""}}
                    for key, row in self.accounts.items()]
            return {"ok": True, "services": [{"id": "google", "label": "Google", "accounts": rows}]}
        if path == "/sign-in/token":
            status, payload = self.token(body)
            if status != 200:
                raise user_oauth.HostError(status, payload)
            return payload
        if path == "/sign-in/handover":
            return self.handover(body)
        raise user_oauth.HostError(404, {"ok": False, "error": "Not Found"})

    def token(self, body):
        if body.get("service") != "google":
            return 404, {"ok": False, "error": "unknown service"}
        key = body.get("account")
        row = self.accounts.get(key) if key else next(
            (item for item in self.accounts.values() if item["state"] == "signed_in"), None)
        if row is None:
            return 404, {"ok": False, "error": "name the account"}
        expected_cid = body.get("connection_id")
        if expected_cid and expected_cid != row.get("connection_id"):
            return 409, {"ok": False, "code": "SIGN_IN_REQUIRED", "reason": "connection_changed",
                         "service": "google", "account": key or "", "note": "",
                         "hint": "Select a host account or sign in on the skill's card in Extensions › Skills."}
        expected_consent = body.get("consent")
        if self.handover_version >= 2 and expected_consent and expected_consent != row.get("consent"):
            return 409, {"ok": False, "code": "SIGN_IN_REQUIRED", "reason": "consent_changed",
                         "service": "google", "account": key or "", "note": "", "hint": ""}
        scope = row.get("scope") or ""
        needed = body.get("required_scope")
        if needed and needed not in scope.split():
            return 409, {"ok": False, "code": "SIGN_IN_REQUIRED", "reason": "insufficient_scope",
                         "service": "google", "account": key or "", "note": "",
                         "hint": "Sign in again on the skill's card in Extensions › Skills."}
        if self.on_token is not None:
            self.on_token(key, row, body)
        conn = {"connection_id": row["connection_id"]} if row.get("connection_id") else {}
        consent = {"consent": row["consent"]} if (self.handover_version >= 2 and row.get("consent")) else {}
        return 200, {"ok": True, "access_token": row.get("access_token", HOST_ACCESS), "token_type": "Bearer",
                     "expires_at": self.token_expires_at, "scope": scope, **conn, **consent,
                     "account": {"key": key or "", "label": row["label"]}}

    def handover(self, body):
        if "receipt" in body:
            assert set(body) == {"service", "receipt"}
            query = body["receipt"]
            found = self.receipts.get((query.get("legacy_key"), query.get("legacy_generation")))
            return {"ok": True, "receipt": found}
        if self.handover_error is not None:
            raise self.handover_error
        if self.handover_answer is not None:
            return self.handover_answer
        client = (body.get("clients") or [{}])[0]
        grant = (body.get("grants") or [{}])[0]
        if self.handover_version < 2 and client.get("return_hint") == "unknown":
            return {"ok": True,
                    "clients": [{"client_id": str(client.get("client_id") or "")[-4:], "result": "refused", "reason": "invalid_return"}],
                    "grants": [{"legacy_key": grant["legacy_key"], "result": "refused", "reason": "unknown_client", "exchange": "none"}]}
        if grant.get("require_verified") and not self.handover_subject:
            return {"ok": True, "clients": [{"client_id": "ient", "result": "imported"}],
                    "grants": [{"legacy_key": grant["legacy_key"], "result": "refused", "reason": "identity_unavailable", "exchange": "none"}]}
        tokens = grant.get("tokens") or {}
        verified = bool(self.handover_subject)
        generation = grant["legacy_generation"]
        existing_key = host_key(self.handover_subject) if verified else ""
        if existing_key and existing_key in self.accounts:
            key, result, label, consent = existing_key, "kept_newer", self.accounts[existing_key]["label"], ""
        else:
            key = self.add("Google account from Settings", self.handover_subject or grant["legacy_key"],
                           scope=tokens.get("scope", ""), verified=verified, via="handover")
            self.accounts[key].update(legacy_generation=generation)
            result, label, consent = "imported", "Google account from Settings", self.accounts[key].get("consent", "")
        conn = {"connection_id": self.accounts[key]["connection_id"]} if self.accounts[key].get("connection_id") else {}
        consent_field = {"consent": consent} if (self.handover_version >= 2 and consent) else {}
        row = {"legacy_key": grant["legacy_key"], "legacy_generation": generation,
               "result": result, "state": self.accounts[key]["state"],
               "via": self.accounts[key].get("via", "handover"), **conn, **consent_field,
               "account": {"key": key, "label": label}}
        self.receipts[(grant["legacy_key"], generation)] = row
        if self.on_handover is not None:
            self.on_handover(key, row)
        return {"ok": True, "clients": [{"client_id": "ient", "result": "imported"}], "grants": [row]}


class Api:
    def __init__(self, state_dir, settings):
        self.state_dir = state_dir
        self.settings = settings
        self.settings_status = "ok"
        self.ungranted = set()
        self.settings_reads = 0
        self.logged = []

    def get_state_dir(self):
        return self.state_dir

    def get_settings(self, keys):
        self.settings_reads += 1
        return {key: self.settings.get(key, "") for key in keys if key not in self.ungranted}

    def get_settings_state(self, keys):
        self.settings_reads += 1
        if self.settings_status != "ok":
            return {"status": self.settings_status, "values": {}, "missing": []}
        readable = [key for key in keys if key not in self.ungranted]
        values = {key: self.settings[key] for key in readable
                  if self.settings.get(key) is not None and str(self.settings.get(key)).strip() != ""}
        return {"status": "ok", "values": values, "missing": [key for key in readable if key not in values]}

    def log(self, level, message, **fields):
        self.logged.append({"level": level, "message": message, **fields})


@pytest.fixture
def host():
    return FakeHost()


@pytest.fixture
def google():
    state = {"bearers": [], "refreshes": [], "refuse_once": set(), "scope": GOOGLE_SCOPE, "rotated": None, "on_api": None}

    def respond(request):
        if request.url.host == "oauth2.googleapis.com":
            state["refreshes"].append(request.content.decode())
            issued = {"access_token": LEGACY_ACCESS, "expires_in": 3600, "token_type": "Bearer"}
            if state["scope"] is not None:
                issued["scope"] = state["scope"]
            if state["rotated"] is not None:
                issued["refresh_token"] = state["rotated"]
            return httpx.Response(200, json=issued)
        bearer = (request.headers.get("Authorization") or "").removeprefix("Bearer ")
        state["bearers"].append(bearer)
        if bearer in state["refuse_once"]:
            state["refuse_once"].discard(bearer)
            return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})
        if state["on_api"] is not None:
            return state["on_api"](request)
        return httpx.Response(200, json={"user": {"emailAddress": OWNER, "permissionId": "actor"}})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    state["client"] = client
    yield state
    client.close()


@pytest.fixture
def api(tmp_path):
    return Api(str(tmp_path), {"GOOGLE_OAUTH_CLIENT_ID": CLIENT_ID,
                               "GOOGLE_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
                               "GOOGLE_OAUTH_REFRESH_TOKEN": REFRESH})


@pytest.fixture
def wired(host, api, google, monkeypatch):
    monkeypatch.setattr(auth, "_TOKEN_CACHE", {})
    monkeypatch.setenv("HOST_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HOST_SERVICE_TOKEN", SKILL_TOKEN)
    monkeypatch.setattr(user_oauth.HostSignIn, "_call",
                        lambda self, method, path, body=None, timeout=40: host.call(method, path, body))
    monkeypatch.setattr(plugin, "GoogleWorkspaceClient",
                        lambda **kwargs: GoogleWorkspaceClient(http_client=google["client"], **kwargs))
    return api


def test_an_older_core_keeps_the_skills_own_route(wired, host, google):
    host.sign_in_version = None
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] is True
    assert result["sign_in_source"] == "skill_settings"
    assert len(google["refreshes"]) == 1
    assert google["bearers"] == [LEGACY_ACCESS]
    assert [route for _, route, _ in host.requests] == ["/identity"]
    assert calls(host, "/sign-in/token") == []


def test_a_new_core_uses_the_hosts_token_and_never_refreshes_here(wired, host, google):
    wired.settings.clear()  # No older Settings sign-in on this installation.
    host.add(OWNER, "sub-owner", scope=GOOGLE_SCOPE)
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] is True
    assert result["sign_in_source"] == "ouroboros"
    assert result["account"]["label"] == OWNER
    assert google["refreshes"] == []
    assert google["bearers"] == [HOST_ACCESS]
    [body] = calls(host, "/sign-in/token")
    assert body["required_scope"] == user_oauth.SCOPES["drive"]


@pytest.mark.parametrize("scope, rotated", [
    ("openid email " + user_oauth.SCOPES["drive"], None),
    ("openid https://www.googleapis.com/auth/userinfo.email " + user_oauth.SCOPES["drive"], ROTATED_REFRESH),
])
def test_a_normal_renewal_moves_only_reported_permissions_once(wired, host, google, tmp_path, scope, rotated):
    host.handover_version = 2
    google.update(scope=scope, rotated=rotated)
    card_key = host.add(OTHER, "sub-other", scope=GOOGLE_SCOPE)
    report = json.loads(plugin._make_workspace_accounts(wired)())
    assert report["source"] == "skill_settings" and report["used"] == "settings"
    assert [row["key"] for row in report["accounts"]] == [card_key]
    assert not google["refreshes"]
    status = plugin._make_workspace_auth_status(wired)
    result = json.loads(status("oauth"))
    assert result["verified"] and result["sign_in_source"] == "ouroboros"
    [body] = calls(host, "/sign-in/handover")
    assert body["service"] == "google"
    assert body["clients"][0]["client_id"] == CLIENT_ID
    assert body["clients"][0]["client_secret"] == CLIENT_SECRET
    assert body["clients"][0]["return_hint"] == "unknown"
    [grant] = body["grants"]
    assert grant["require_verified"] is True
    assert grant["legacy_key"] == "settings"
    assert grant["tokens"]["scope"] == scope
    assert grant["tokens"]["access_token"] == LEGACY_ACCESS
    assert grant["tokens"]["refresh_token"] == (rotated or REFRESH)
    assert grant["tokens"]["expires_at"] > 0
    marker = (tmp_path / user_oauth.MARKER_NAME).read_text(encoding="utf-8")
    saved_marker = json.loads(marker)["handover"]
    moved_key = host_key("sub-handed-over")
    moved_cid = host.accounts[moved_key]["connection_id"]
    assert saved_marker["generation"] == grant["legacy_generation"]
    assert saved_marker["connection_id"] == moved_cid and moved_cid
    assert not SECRET_SHAPES.search(marker)
    auth._TOKEN_CACHE.clear()  # the next tool child need not have the first child's cache
    result = json.loads(status("oauth"))
    assert result["verified"] and result["sign_in_source"] == "ouroboros"
    token_calls = calls(host, "/sign-in/token")
    assert [body.get("account") for body in token_calls] == [moved_key, moved_key]
    assert [body.get("connection_id") for body in token_calls] == [moved_cid, moved_cid]
    assert len(google["refreshes"]) == len(calls(host, "/sign-in/handover")) == 1
    assert google["bearers"] == [HOST_ACCESS, HOST_ACCESS]
    assert wired.settings["GOOGLE_OAUTH_REFRESH_TOKEN"] == REFRESH


@pytest.mark.parametrize("scope", [None, user_oauth.SCOPES["drive"],
                                   GOOGLE_SCOPE + " https://www.googleapis.com/auth/calendar"])
def test_an_unmoved_settings_connection_serves_normal_calls(wired, host, google, tmp_path, scope):
    host.handover_version = 2
    google.update(scope=scope, rotated=ROTATED_REFRESH)
    card_key = host.add(OTHER, "sub-other", scope=GOOGLE_SCOPE)
    status = plugin._make_workspace_auth_status(wired)
    result = json.loads(status("oauth"))
    assert result["verified"] and result["sign_in_source"] == "skill_settings"
    assert "Settings is still in use" in result["sign_in_message"]
    assert "cannot write to Settings Secrets" in result["sign_in_message"]
    assert "Only the newly issued access token can be used while it remains valid in this call" in result["sign_in_message"]
    assert "do not send secrets in chat" in result["sign_in_message"]
    assert result["sign_in_notice"] == result["sign_in_message"]
    assert any(entry["level"] == "warning" for entry in wired.logged)
    assert SECRET_SHAPES.search(result["sign_in_message"]) is None
    assert SECRET_SHAPES.search(json.dumps(wired.logged)) is None
    assert google["bearers"] == [LEGACY_ACCESS]
    assert calls(host, "/sign-in/handover") == []
    assert not (tmp_path / user_oauth.MARKER_NAME).exists()
    assert wired.settings["GOOGLE_OAUTH_REFRESH_TOKEN"] == REFRESH

    user_oauth.save_selected_account(wired, card_key)
    result = json.loads(status("oauth"))
    assert result["verified"] and result["sign_in_source"] == "ouroboros"
    [body] = calls(host, "/sign-in/token")
    assert body["account"] == card_key
    assert len(google["refreshes"]) == 1


def test_a_configured_bearer_does_not_start_a_scope_discovery_refresh(wired, host, google):
    host.handover_version = 2
    wired.settings["GOOGLE_OAUTH_ACCESS_TOKEN"] = LEGACY_ACCESS
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] and result["sign_in_source"] == "skill_settings"
    assert google["bearers"] == [LEGACY_ACCESS]
    assert google["refreshes"] == calls(host, "/sign-in/handover") == []


@pytest.mark.parametrize("kept_newer", [False, True])
def test_settled_handover_is_read_after_a_lost_answer(wired, host, google, tmp_path, monkeypatch, kept_newer):
    host.handover_version = 2
    if kept_newer:
        host.add(OWNER, host.handover_subject, scope=GOOGLE_SCOPE)
    handover = host.handover

    def lost_then_unavailable(body):
        if "receipt" in body:
            return handover(body)
        if body.get("recover"):
            raise user_oauth.HostError(503, {"ok": False})
        handover(body)
        raise TimeoutError("no reply")

    monkeypatch.setattr(host, "handover", lost_then_unavailable)
    status = plugin._make_workspace_auth_status(wired)
    first = json.loads(status("oauth"))
    [submit, recovery, lookup] = calls(host, "/sign-in/handover")
    generation = submit["grants"][0]["legacy_generation"]
    assert recovery == {**submit, "recover": True}
    assert lookup == {"service": "google", "receipt": {"legacy_key": "settings", "legacy_generation": generation}}
    marker = json.loads((tmp_path / user_oauth.MARKER_NAME).read_text(encoding="utf-8"))["handover"]
    assert marker["state"] == "moved"
    assert marker["handover_result"] == ("kept_newer" if kept_newer else "imported")
    assert "consent" not in marker and not SECRET_SHAPES.search(json.dumps(marker))
    if kept_newer:
        assert not first["verified"] and "instead of switching mid-flight" in first["message"]
        assert calls(host, "/sign-in/token") == [] and google["bearers"] == []
    else:
        assert first["verified"]
        [token] = calls(host, "/sign-in/token")
        assert token["consent"] == host.receipts[("settings", generation)]["consent"]
    assert json.loads(status("oauth"))["verified"]
    assert len(calls(host, "/sign-in/handover")) == 3
    assert len(google["refreshes"]) == 1


def test_an_unreadable_handover_marker_is_not_guessed_absent(wired, host, google, tmp_path):
    (tmp_path / "google_sign_in.json").write_text("{not json", encoding="utf-8")
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] is False
    assert calls(host, "/sign-in/handover") == []
    assert google["bearers"] == []


def test_several_accounts_need_the_owners_choice(wired, host, google, tmp_path):
    wired.settings.clear()
    host.add(OWNER, "sub-owner", scope=GOOGLE_SCOPE)
    host.add(OTHER, "sub-other", scope=GOOGLE_SCOPE)
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] is False
    assert "several Google accounts" in result["message"]
    (tmp_path / "settings.json").write_text(json.dumps({"DEFAULT_GOOGLE_ACCOUNT": OTHER}), encoding="utf-8")
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] is True
    assert calls(host, "/sign-in/token")[-1]["account"] == host_key("sub-other")


def test_duplicate_labels_need_the_account_key(wired, host, google, tmp_path):
    host.add("shared@example.test", "sub-a", scope=GOOGLE_SCOPE)
    host.add("shared@example.test", "sub-b", scope=GOOGLE_SCOPE)
    (tmp_path / "settings.json").write_text(json.dumps({"DEFAULT_GOOGLE_ACCOUNT": "shared@example.test"}), encoding="utf-8")
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] is False
    assert "key" in result["message"]
    (tmp_path / "settings.json").write_text(json.dumps({"DEFAULT_GOOGLE_ACCOUNT": host_key("sub-b")}), encoding="utf-8")
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] is True
    assert calls(host, "/sign-in/token")[-1]["account"] == host_key("sub-b")


def test_a_single_account_may_stay_implicit(wired, host, google):
    wired.settings.clear()
    key = host.add(OWNER, "sub-owner", scope=GOOGLE_SCOPE)
    json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    [body] = calls(host, "/sign-in/token")
    assert body["account"] == key
    assert body["connection_id"] == host.accounts[key]["connection_id"]


def test_a_refused_bearer_asks_the_host_once_with_it(wired, host, google):
    wired.settings.clear()
    host.add(OWNER, "sub-owner", scope=GOOGLE_SCOPE)
    google["refuse_once"].add(HOST_ACCESS)
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] is True
    bodies = calls(host, "/sign-in/token")
    assert len(bodies) == 2
    assert bodies[0].get("rejected") is None
    assert bodies[1]["rejected"] == HOST_ACCESS
    assert google["bearers"] == [HOST_ACCESS, HOST_ACCESS]


def test_a_missing_permission_is_the_owners_next_step(wired, host, google):
    wired.settings.clear()
    host.add(OWNER, "sub-owner", scope=user_oauth.SCOPES["drive"].replace("/drive", "/drive.readonly"))
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] is False
    assert "not allowed" in result["message"]
    [body] = calls(host, "/sign-in/token")
    assert body["required_scope"] == user_oauth.SCOPES["drive"]


def test_an_unreadable_host_is_not_an_older_core(wired, host, google):
    wired.settings.clear()
    key = host.add(OWNER, "sub-owner", scope=GOOGLE_SCOPE)
    user_oauth.save_selected_account(wired, key)
    host.identity_body = {"unexpected": True}
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] is False and result["configured"] is None
    assert "did not answer" in result["message"]
    assert google["bearers"] == []


def test_host_outage_keeps_the_existing_settings_connection_without_transfer(wired, host, google, tmp_path):
    host.handover_version = 2
    host.identity_body = {"unexpected": True}
    report = json.loads(plugin._make_workspace_accounts(wired)())
    assert report["state"] == user_oauth.UNREACHABLE and report["source"] == "skill_settings"
    assert report["used"] == "settings" and "unknown" in report["message"]
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] and result["sign_in_source"] == "skill_settings"
    assert google["bearers"] == [LEGACY_ACCESS] and len(google["refreshes"]) == 1
    assert [path for _, path, _ in host.requests] == ["/identity", "/identity"]
    assert not (tmp_path / user_oauth.MARKER_NAME).exists()


def test_an_unreadable_account_list_is_not_an_empty_one(wired, host, google):
    wired.settings.clear()
    key = host.add(OWNER, "sub-owner", scope=GOOGLE_SCOPE)
    user_oauth.save_selected_account(wired, key)
    host.accounts_status = 500
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] is False and result["configured"] is None
    assert "sign-in service" in result["message"]
    assert google["bearers"] == []


def test_the_service_account_route_never_asks_the_host(wired, host, google, monkeypatch):
    wired.settings["GOOGLE_SERVICE_ACCOUNT_JSON"] = json.dumps(
        {"type": "service_account", "client_email": "sa@example.test", "private_key": "fixture",
         "token_uri": auth.GOOGLE_TOKEN_URI})
    monkeypatch.setattr(client_module, "get_access_token", lambda **kwargs: "service-token")
    result = json.loads(plugin._make_workspace_auth_status(wired)())
    assert result["verified"] is True
    assert host.requests == []
    assert google["bearers"] == ["service-token"]


def test_no_token_or_secret_reaches_the_logs_or_the_answers(wired, host, google):
    key = host.add(OWNER, "sub-owner", scope=GOOGLE_SCOPE)
    user_oauth.save_selected_account(wired, key)
    answer = plugin._make_workspace_auth_status(wired)("oauth")
    assert json.loads(answer)["verified"]
    assert SECRET_SHAPES.search(answer) is None
    assert SECRET_SHAPES.search(json.dumps(wired.logged)) is None


def test_the_account_choice_route_saves_and_keeps_the_other_settings(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"DEFAULT_FOLDER_ID": "folder1"}), encoding="utf-8")

    class RegisteredApi(Api):
        def __init__(self, state_dir):
            super().__init__(state_dir, {})
            self.routes, self.sections = {}, {}

        def register_tool(self, *args, **kwargs): pass
        def register_ui_tab(self, *args, **kwargs): pass
        def on_unload(self, callback): pass
        def register_route(self, path, handler, methods=("GET",)): self.routes[path] = (handler, methods)
        def register_settings_section(self, section_id, title, schema): self.sections[section_id] = schema

    class Request:
        def __init__(self, method="POST", body=None):
            self.method, self._body = method, body

        async def json(self):
            return self._body

    def decode(response):
        return response if isinstance(response, dict) else json.loads(response.body)

    api = RegisteredApi(str(tmp_path))
    plugin.register(api)
    parts = api.sections["google_workspace"]["components"]
    [form] = [c for c in parts if c.get("route") == "google_account/save"]
    copy = " ".join(c["text"] for c in parts if c.get("type") == "markdown")
    assert "Docs, Sheets and Slides" in copy and "**" not in copy and "`" not in copy
    assert "workspace_accounts" not in copy and "workspace_accounts" in form["fields"][0]["help"]
    field_name = form["fields"][0]["name"]
    handler, methods = api.routes[form["route"]]
    assert methods == ("GET", "POST") and field_name == "DEFAULT_GOOGLE_ACCOUNT"
    assert decode(asyncio.run(handler(Request("GET")))) == {field_name: ""}
    post_body = decode(asyncio.run(handler(Request("POST", {field_name: OWNER}))))
    assert post_body["status"] == "ok" and post_body[field_name] == OWNER
    hydrated = decode(asyncio.run(handler(Request("GET"))))
    assert hydrated == {field_name: OWNER}
    assert decode(asyncio.run(handler(Request("POST", hydrated))))["status"] == "ok"
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8")) == {
        "DEFAULT_FOLDER_ID": "folder1", field_name: OWNER}


def test_saved_account_survives_a_temporary_settings_read_failure(wired, host, google, tmp_path, monkeypatch):
    card_key = host.add(OTHER, "sub-other", scope=GOOGLE_SCOPE)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"DEFAULT_GOOGLE_ACCOUNT": card_key, "DEFAULT_FOLDER_ID": "folder1"}), encoding="utf-8")
    before = path.read_bytes()
    read_text = Path.read_text
    account_form = plugin._make_google_account_save(wired)
    defaults_form = plugin._make_settings_save(wired)
    status = plugin._make_workspace_auth_status(wired)

    class Request:
        def __init__(self, body):
            self.body = body

        async def json(self):
            return self.body

    def unavailable(file, *args, **kwargs):
        if file == path:
            raise OSError("temporary read failure")
        return read_text(file, *args, **kwargs)

    defaults = {"DEFAULT_FOLDER_ID": "folder2", "TEMPLATES_JSON": "{}"}
    with monkeypatch.context() as context:
        context.setattr(Path, "read_text", unavailable)
        report = json.loads(status("oauth"))
        assert not report["verified"] and "settings could not be read" in report["message"]
        for form, body in ((defaults_form, defaults), (account_form, {"DEFAULT_GOOGLE_ACCOUNT": OTHER})):
            response = asyncio.run(form(Request(body)))
            if not isinstance(response, dict):
                assert response.status_code == 503
                response = json.loads(response.body)
            assert response["status"] == "error"
    assert path.read_bytes() == before
    assert calls(host, "/sign-in/token") == google["refreshes"] == []

    asyncio.run(defaults_form(Request(defaults)))
    assert json.loads(path.read_text(encoding="utf-8")) == {**defaults, "DEFAULT_GOOGLE_ACCOUNT": card_key}
    assert json.loads(status("oauth"))["verified"]
    [body] = calls(host, "/sign-in/token")
    assert body["account"] == card_key
    assert not google["refreshes"]


def test_calendar_events_scope_covers_read_and_write():
    events = user_oauth.SCOPES["calendar.events"]
    assert user_oauth.needed_scope("calendar", "GET") == events
    assert user_oauth.needed_scope("calendar", "PATCH") == events
    assert "calendar" not in user_oauth.SCOPES
    assert events in user_oauth.OPTIONAL_SCOPES
    assert "https://www.googleapis.com/auth/calendar" not in user_oauth.OPTIONAL_SCOPES


def test_the_manifest_declares_the_skills_own_scopes():
    text = (HERE.parent / "SKILL.md").read_text(encoding="utf-8")
    front = yaml.safe_load(re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)[1])
    [service] = front["sign_in"]
    assert service["id"] == "google"
    assert service["issuer"] == "https://accounts.google.com"
    assert list(service["scopes"]) == list(user_oauth.REQUIRED_SCOPES)
    assert {name: tuple(words) for name, words in service["scope_spellings"].items()} == user_oauth.SCOPE_SPELLINGS
    assert [item["scope"] for item in service["optional_scopes"]] == list(user_oauth.OPTIONAL_SCOPES)
    assert [item["default"] for item in service["optional_scopes"]] == [True, True, True, False, False]
    assert "https://www.googleapis.com/auth/calendar" not in [item["scope"] for item in service["optional_scopes"]]
    assert service["accounts"] == "many"
    assert service["optional"] is True
    assert service["authorize_params"] == {"access_type": "offline", "prompt": "consent"}
    assert set(service["notes"]) == {"client", "refused", "refresh_rejected"}
    assert all(0 < len(note) <= 600 for note in service["notes"].values())
    assert re.search(r"client_id|client_secret", json.dumps(service)) is None


def test_v1_uses_settings_without_handover_then_v2_moves_and_serves_slides(wired, host, google, tmp_path):
    google["scope"] = "openid email " + user_oauth.SCOPES["drive"]
    google["on_api"] = lambda req: httpx.Response(200, json={"replies": []})
    req_tool = plugin._make_workspace_request(wired)
    assert json.loads(req_tool("slides", "POST", "presentations/deck1:batchUpdate", json_body={"requests": []}, auth_mode="oauth"))["status_code"] == 200
    assert calls(host, "/sign-in/handover") == []
    assert not (tmp_path / user_oauth.MARKER_NAME).exists()
    host.handover_version = 2
    auth._TOKEN_CACHE.clear()
    assert json.loads(req_tool("slides", "GET", "presentations/deck1", auth_mode="oauth"))["status_code"] == 200
    [handover] = calls(host, "/sign-in/handover")
    assert handover["clients"][0]["return_hint"] == "unknown" and handover["grants"][0]["require_verified"] is True
    assert json.loads(req_tool("slides", "GET", "presentations/deck1", auth_mode="oauth"))["status_code"] == 200
    assert [b["required_scope"] for b in calls(host, "/sign-in/token")] == [user_oauth.SCOPES["drive"]] * 2


def test_v2_without_confirmed_subject_keeps_drive_settings(wired, host, google):
    host.handover_version = 2
    host.handover_subject = ""
    google["scope"] = "openid email " + user_oauth.SCOPES["drive"]
    google["on_api"] = lambda req: httpx.Response(200, json={"replies": []})
    req_tool = plugin._make_workspace_request(wired)
    assert json.loads(req_tool("slides", "GET", "presentations/deck1", auth_mode="oauth"))["status_code"] == 200
    auth._TOKEN_CACHE.clear()
    assert json.loads(req_tool("slides", "GET", "presentations/deck1", auth_mode="oauth"))["status_code"] == 200
    assert len(calls(host, "/sign-in/handover")) == 1 and google["bearers"] == [LEGACY_ACCESS, LEGACY_ACCESS]


def test_uncertain_handover_and_withdrawal_do_not_reopen_settings(wired, host, google, tmp_path):
    host.handover_version = 2
    host.handover_answer = {
        "ok": True, "clients": [{"client_id": "ient", "result": "imported"}],
        "grants": [{"legacy_key": "settings", "result": "refused", "reason": "provider_unreachable", "exchange": "unknown"}],
    }
    assert json.loads(plugin._make_workspace_auth_status(wired)("oauth"))["verified"] is False
    marker = json.loads((tmp_path / user_oauth.MARKER_NAME).read_text(encoding="utf-8"))["handover"]
    assert marker["state"] == "pending" and google["bearers"] == []
    (tmp_path / user_oauth.WITHDRAWN_NAME).write_text(json.dumps({
        "v": 1, "accounts": [{"service": "google", "legacy_key": "settings", "legacy_generation": marker["generation"]}],
    }), encoding="utf-8")
    reconciled = user_oauth._reconciled(marker, [], user_oauth._withdrawn_entries(wired))
    assert reconciled["state"] == "account_disconnected" and not user_oauth._legacy_allowed(reconciled)


def test_full_drive_hosted_serves_slides_docs_sheets_and_docs_only_checks_template(wired, host, google):
    wired.settings.clear()
    drive_key = host.add(OWNER, "sub-drive", scope="openid email " + user_oauth.SCOPES["drive"])
    docs_key = host.add(OTHER, "sub-docs", scope="openid email " + user_oauth.SCOPES["documents"])
    google["on_api"] = lambda req: (
        httpx.Response(200, json={"documentId": "doc_blank", "title": "Blank"}) if req.url.host == "docs.googleapis.com"
        else httpx.Response(200, json={"spreadsheetId": "s1", "properties": {"title": "S"}, "sheets": []}) if req.url.host == "sheets.googleapis.com"
        else httpx.Response(200, json={"replies": []}) if req.url.host == "slides.googleapis.com"
        else httpx.Response(200, json={"id": "doc_drive", "name": "Drive doc"}))
    user_oauth.save_selected_account(wired, drive_key)
    assert json.loads(plugin._make_workspace_request(wired)("slides", "GET", "presentations/deck1", auth_mode="oauth"))["status_code"] == 200
    assert json.loads(plugin._make_docs_create(wired)("Blank", auth_mode="oauth"))["document_id"] == "doc_blank"
    assert json.loads(plugin._make_docs_create(wired)("Copy", template_id="t1", auth_mode="oauth"))["document_id"] == "doc_drive"
    assert json.loads(plugin._make_docs_create(wired)("Folder", folder_id="f1", auth_mode="oauth"))["document_id"] == "doc_drive"
    assert json.loads(plugin._make_sheets_info(wired)("s1", auth_mode="oauth"))["spreadsheet_id"] == "s1"
    assert [b["required_scope"] for b in calls(host, "/sign-in/token")] == [user_oauth.SCOPES["drive"]] * 5
    user_oauth.save_selected_account(wired, docs_key)
    with pytest.raises(RuntimeError, match="not allowed"):
        plugin._make_docs_create(wired)("Copy", template_id="t1", auth_mode="oauth")
    with pytest.raises(RuntimeError, match="not allowed"):
        plugin._make_workspace_request(wired)("slides", "GET", "presentations/deck1", auth_mode="oauth")
    assert [b["required_scope"] for b in calls(host, "/sign-in/token")[-2:]] == [
        user_oauth.SCOPES["drive"], user_oauth.SCOPES["presentations"]]


@pytest.mark.parametrize("mode", ["absent", "no_host"])
def test_explicit_account_choice_is_not_replaced_by_settings_on_older_core(wired, host, google, monkeypatch, mode):
    user_oauth.save_selected_account(wired, OWNER)
    if mode == "absent":
        host.sign_in_version = None
    else:
        monkeypatch.delenv("HOST_SERVICE_URL", raising=False)
        monkeypatch.delenv("HOST_SERVICE_TOKEN", raising=False)
    report = json.loads(plugin._make_workspace_accounts(wired)())
    assert report["selected"] == OWNER and report["source"] == "" and report["used"] == ""
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] is False and "not used in its place" in result["message"]
    assert google["refreshes"] == [] and google["bearers"] == []


def test_single_command_keeps_pinned_key_and_connection_across_requests(wired, host, google):
    host.handover_version = 2
    wired.settings.clear()
    first_key = host.add(OWNER, "sub-owner", scope=GOOGLE_SCOPE)
    first_cid = host.accounts[first_key]["connection_id"]
    first_consent = host.accounts[first_key]["consent"]

    def respond_drive(request):
        if request.url.params.get("alt") != "media":
            second_key = host.add(OTHER, "sub-other", scope=GOOGLE_SCOPE)
            user_oauth.save_selected_account(wired, second_key)
            return httpx.Response(200, json={"id": "file1", "name": "notes.txt", "mimeType": "text/plain"})
        return httpx.Response(200, text="pinned content")

    google["on_api"] = respond_drive
    assert json.loads(plugin._make_drive_read_text(wired)("file1", auth_mode="oauth"))["text"] == "pinned content"
    assert [(b["account"], b["connection_id"], b["consent"]) for b in calls(host, "/sign-in/token")] == [
        (first_key, first_cid, first_consent), (first_key, first_cid, first_consent)]


def test_older_core_rotation_and_bearer_only_distinguish_current_token_from_renewal(wired, host, google):
    host.sign_in_version = None
    google["rotated"] = ROTATED_REFRESH
    rotated_res = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert rotated_res["verified"] and "Only the newly issued access token can be used while it remains valid in this call" in rotated_res["sign_in_message"]
    assert "cannot write to Settings Secrets" in rotated_res["sign_in_message"]
    assert "Settings → Secrets" in rotated_res["sign_in_message"] and user_oauth.CARD not in rotated_res["sign_in_message"]
    assert "do not send secrets in chat" in rotated_res["sign_in_message"]
    assert SECRET_SHAPES.search(rotated_res["sign_in_message"]) is None
    bearer_api = Api(wired.get_state_dir(), {"GOOGLE_OAUTH_ACCESS_TOKEN": LEGACY_ACCESS})
    bearer_res = json.loads(plugin._make_workspace_auth_status(bearer_api)("oauth"))
    assert bearer_res["verified"] and bearer_res["refresh_configured"] is False and "until it expires" in bearer_res["sign_in_message"]


def test_kept_newer_handover_stops_initiating_command_then_serves_new_command(wired, host, google):
    host.handover_version = 2
    card_key = host.add(OWNER, "sub-handed-over", scope="openid email " + user_oauth.SCOPES["presentations"])
    card_cid = host.accounts[card_key]["connection_id"]
    card_consent = host.accounts[card_key]["consent"]
    google["scope"] = "openid email " + user_oauth.SCOPES["drive"]
    google["on_api"] = lambda req: httpx.Response(200, json={"replies": []})
    req_tool = plugin._make_workspace_request(wired)
    with pytest.raises(user_oauth.UserOAuthError, match="instead of switching mid-flight"):
        req_tool("slides", "GET", "presentations/deck1", auth_mode="oauth")
    assert calls(host, "/sign-in/token") == []
    assert json.loads(req_tool("slides", "GET", "presentations/deck1", auth_mode="oauth"))["status_code"] == 200
    assert len(calls(host, "/sign-in/handover")) == 1
    assert [(b["account"], b["connection_id"], b["consent"], b["required_scope"]) for b in calls(host, "/sign-in/token")] == [
        (card_key, card_cid, card_consent, user_oauth.SCOPES["presentations"])]


def test_kept_newer_account_needing_sign_in_is_not_reported_as_ready(wired, host, google):
    host.handover_version = 2
    host.add(OWNER, "sub-handed-over", state="sign_in_required", scope=GOOGLE_SCOPE)
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert not result["verified"] and "Sign in again" in result["message"]
    report = json.loads(plugin._make_workspace_accounts(wired)())
    assert report["used"] == "" and "Sign in again" in report["message"]
    assert calls(host, "/sign-in/token") == [] and google["bearers"] == []


def test_transfer_records_result_but_keeps_a_new_settings_choice_for_a_new_command(wired, host, google, tmp_path):
    host.handover_version = 2
    new_account = host.add(OTHER, "sub-other", scope=GOOGLE_SCOPE)
    host.on_handover = lambda *_: wired.settings.update(GOOGLE_OAUTH_REFRESH_TOKEN="updated-settings-fixture")
    status = plugin._make_workspace_auth_status(wired)
    assert not json.loads(status("oauth"))["verified"]
    path = tmp_path / user_oauth.MARKER_NAME
    transfer = json.loads(path.read_text(encoding="utf-8"))["handover"]
    assert transfer["state"] == "moved" and transfer["account"]["key"] == host_key("sub-handed-over")
    assert not json.loads(status("oauth"))["verified"]
    assert calls(host, "/sign-in/token") == [] and google["bearers"] == []
    assert len(calls(host, "/sign-in/handover")) == 1

    user_oauth.save_selected_account(wired, new_account)
    selected = json.loads(status("oauth"))
    assert selected["verified"] and selected["account"]["key"] == new_account
    assert json.loads(path.read_text(encoding="utf-8"))["handover"] == transfer


def test_unverified_host_row_points_to_card_without_using_settings(wired, host, google):
    key = host.add(OWNER, "legacy-settings", scope=GOOGLE_SCOPE, verified=False)
    user_oauth.save_selected_account(wired, key)
    result = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert result["verified"] is False and user_oauth.CARD in result["message"]
    assert calls(host, "/sign-in/token") == [] and google["bearers"] == []


@pytest.mark.parametrize("reason, exchange, echo_generation, state", [
    ("provider_unreachable", "none", True, "kept_legacy"),
    ("provider_unreachable", "resubmit", True, "resubmit"),
    ("refresh_rejected", "unknown", False, "refresh_rejected"),
    ("account_disconnected", "unknown", False, "account_disconnected"),
])
def test_addressed_old_offer_settles_separately_from_current_settings(
        wired, host, google, tmp_path, reason, exchange, echo_generation, state):
    host.handover_version = 2
    creds = plugin._get_oauth_credentials(wired)
    pending = {"state": "pending", "generation": "g-old-offer",
               "settings_fingerprint": user_oauth._settings_fingerprint(creds),
               "refresh_fingerprint": user_oauth._refresh_fingerprint(creds), "direct_fingerprint": ""}
    path = tmp_path / user_oauth.MARKER_NAME
    path.write_text(json.dumps({"handover": pending}), encoding="utf-8")
    if exchange in ("none", "resubmit"):
        wired.settings["GOOGLE_OAUTH_CLIENT_SECRET"] = "later-settings-secret"
    host.receipts[("settings", "g-old-offer")] = {
        "legacy_key": "settings", "result": "refused", "reason": reason, "exchange": exchange,
        **({"legacy_generation": "g-old-offer"} if echo_generation else {}),
    }
    status = plugin._make_workspace_auth_status(wired)
    assert not json.loads(status("oauth"))["verified"]
    saved = json.loads(path.read_text(encoding="utf-8"))["handover"]
    assert saved["state"] == state and saved["reason"] == reason and saved["generation"] == "g-old-offer"
    assert not json.loads(status("oauth"))["verified"]
    assert json.loads(path.read_text(encoding="utf-8"))["handover"] == saved
    assert calls(host, "/sign-in/handover") == [
        {"service": "google", "receipt": {"legacy_key": "settings", "legacy_generation": "g-old-offer"}},
    ]
    assert calls(host, "/sign-in/token") == google["refreshes"] == google["bearers"] == []


@pytest.mark.parametrize("v2", [False, True])
def test_a_new_direct_bearer_does_not_renew_a_rejected_grant(wired, host, google, tmp_path, v2):
    host.sign_in_version = 1 if v2 else None
    host.handover_version = 2 if v2 else 0
    wired.settings["GOOGLE_OAUTH_ACCESS_TOKEN"] = LEGACY_ACCESS
    old_creds = plugin._get_oauth_credentials(wired)
    (tmp_path / user_oauth.MARKER_NAME).write_text(json.dumps({"handover": {
        "state": "refresh_rejected", "refresh_fingerprint": user_oauth._refresh_fingerprint(old_creds),
        "direct_fingerprint": user_oauth._direct_fingerprint(old_creds),
    }}), encoding="utf-8")
    status = plugin._make_workspace_auth_status(wired)
    assert not json.loads(status("oauth"))["verified"]
    new_bearer = "new-direct-fixture"
    wired.settings["GOOGLE_OAUTH_ACCESS_TOKEN"] = new_bearer
    available = json.loads(status("oauth"))
    assert available["verified"] and available["sign_in_source"] == "skill_settings"
    assert not available["refresh_configured"]
    google["refuse_once"].add(new_bearer)
    expired = json.loads(status("oauth"))
    assert not expired["verified"] and "access token alone must be replaced" in expired["message"]
    assert google["bearers"] == [new_bearer, new_bearer]
    assert google["refreshes"] == calls(host, "/sign-in/handover") == calls(host, "/sign-in/token") == []


def test_an_older_core_checks_settings_after_ordinary_renewal(wired, host, google, monkeypatch):
    host.sign_in_version = None
    new_secret = "updated-during-renewal"

    def reply(request):
        if request.url.host == "oauth2.googleapis.com":
            google["refreshes"].append(request.content.decode())
            wired.settings["GOOGLE_OAUTH_CLIENT_SECRET"] = new_secret
            return httpx.Response(200, json={"access_token": LEGACY_ACCESS, "expires_in": 3600})
        google["bearers"].append(request.headers["Authorization"])
        return httpx.Response(200, json={"files": []})

    with httpx.Client(transport=httpx.MockTransport(reply)) as transport:
        monkeypatch.setitem(google, "client", transport)
        tool = plugin._make_drive_list(wired)
        with pytest.raises(RuntimeError, match=r"^drive_list failed: Google OAuth credentials in Settings changed\b"):
            tool(auth_mode="oauth")
        assert google["bearers"] == [] and len(google["refreshes"]) == 1
        assert json.loads(tool(auth_mode="oauth"))["files"] == []
        assert new_secret in google["refreshes"][1]
        assert google["bearers"] == [f"Bearer {LEGACY_ACCESS}"]


def test_pre_exchange_refusal_keeps_settings_and_retries_on_later_ordinary_renewal(wired, host, google, tmp_path, monkeypatch):
    host.handover_version = 2
    now = [1_700_000_000.0]
    monkeypatch.setattr(user_oauth.time, "time", lambda: now[0])
    host.handover_error = user_oauth.HostError(429, {"ok": False, "error": "busy", "handover_started": False})
    status = plugin._make_workspace_auth_status(wired)
    first = json.loads(status("oauth"))
    assert first["verified"] and first["sign_in_source"] == "skill_settings"
    assert "select and save that account" in first["sign_in_message"]
    marker = json.loads((tmp_path / user_oauth.MARKER_NAME).read_text(encoding="utf-8"))["handover"]
    assert marker["state"] == "kept_legacy" and marker["reason"] == "rate_limited"
    assert SECRET_SHAPES.search(json.dumps(marker)) is None

    host.accounts_status = 429
    auth._TOKEN_CACHE.clear()
    second = json.loads(status("oauth"))
    assert second["verified"] and second["sign_in_source"] == "skill_settings"
    assert len(calls(host, "/sign-in/handover")) == 1
    assert calls(host, "/sign-in/accounts") == []
    with pytest.raises(user_oauth.UserOAuthError):
        plugin._make_workspace_accounts(wired)()
    host.accounts_status = 200

    now[0] += user_oauth.RETRY_BASE_SECONDS + 1
    host.handover_error = None
    host.handover_answer = {
        "ok": True, "clients": [{"client_id": "ient", "result": "imported"}],
        "grants": [{"legacy_key": "settings", "result": "refused", "reason": "provider_unreachable", "exchange": "none"}],
    }
    auth._TOKEN_CACHE.clear()
    third = json.loads(status("oauth"))
    assert third["verified"] and third["sign_in_source"] == "skill_settings"
    assert len(calls(host, "/sign-in/handover")) == 2

    now[0] += user_oauth.RETRY_BASE_SECONDS * 2 + 1
    host.handover_answer = None
    auth._TOKEN_CACHE.clear()
    fourth = json.loads(status("oauth"))
    assert fourth["verified"] and fourth["sign_in_source"] == "ouroboros"
    assert len(calls(host, "/sign-in/handover")) == 3


def test_refresh_rejected_uses_real_error_and_unblocks_only_new_credentials(wired, host, google, tmp_path, monkeypatch):
    host.handover_version = 2
    host.add(OTHER, "sub-other", scope=GOOGLE_SCOPE)
    reject_refresh = [True]

    def refresh_or_ok(**kwargs):
        if reject_refresh[0]:
            raise auth.UserRefreshError(400, "invalid_grant")
        return LEGACY_ACCESS

    monkeypatch.setattr(user_oauth, "get_user_access_token", refresh_or_ok)
    first = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert not first["verified"] and calls(host, "/sign-in/token") == []
    assert "select and save that account" in first["message"]
    marker = json.loads((tmp_path / user_oauth.MARKER_NAME).read_text(encoding="utf-8"))["handover"]
    assert marker["state"] == "refresh_rejected" and marker.get("refresh_fingerprint")
    assert SECRET_SHAPES.search(json.dumps(marker)) is None

    host.sign_in_version = None
    same_older = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert not same_older["verified"] and user_oauth.CARD not in same_older["message"] and "Settings → Secrets" in same_older["message"]

    reject_refresh[0] = False
    wired.settings["GOOGLE_OAUTH_CLIENT_SECRET"] = "fixed-settings-secret"
    fixed_older = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert fixed_older["verified"] and fixed_older["sign_in_source"] == "skill_settings"

    # A bare pre-fingerprint refresh_rejected cannot prove current credentials differ.
    (tmp_path / user_oauth.MARKER_NAME).write_text(json.dumps({"handover": {"state": "refresh_rejected"}}), encoding="utf-8")
    bare = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert not bare["verified"]
    assert "cannot reopen the Settings route" in bare["message"]
    assert "Obtain a new working refresh token" not in bare["message"]
    assert "delete" not in bare["message"].lower()


def test_replacing_settings_credentials_never_reopens_pending_moved_or_withdrawn(wired, host, google, tmp_path):
    host.handover_version = 2
    host.add(OTHER, "sub-other", scope=GOOGLE_SCOPE)
    for marker in (
        {"state": "pending", "generation": "g1"},
        {"state": "moved", "generation": "g1", "account": {"key": host_key("sub-old")}, "connection_id": "conn-gone"},
        {"state": "account_disconnected", "generation": "g1"},
    ):
        (tmp_path / user_oauth.MARKER_NAME).write_text(json.dumps({"handover": marker}), encoding="utf-8")
        wired.settings["GOOGLE_OAUTH_REFRESH_TOKEN"] = "new-refresh-" + marker["state"]
        res = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
        assert not res["verified"] and calls(host, "/sign-in/token") == [] and google["bearers"] == []


def test_settings_state_gating_blocks_unknown_or_revoked_without_blocking_clean_explicit_or_moved(wired, host, google, tmp_path):
    host.handover_version = 2
    card_key = host.add(OTHER, "sub-other", scope=GOOGLE_SCOPE)
    card_cid = host.accounts[card_key]["connection_id"]

    for status_name in ("unknown", "access_closed"):
        wired.settings_status = status_name
        report = json.loads(plugin._make_workspace_accounts(wired)())
        assert report["source"] == "" and report["used"] == ""
        res = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
        assert not res["verified"] and res["configured"] is None and calls(host, "/sign-in/token") == []

    # Revoked grant on GOOGLE_OAUTH_REFRESH_TOKEN omits it from both values and missing -> fail closed.
    wired.settings_status = "ok"
    wired.ungranted = {"GOOGLE_OAUTH_REFRESH_TOKEN"}
    revoked = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert not revoked["verified"] and revoked["configured"] is None and calls(host, "/sign-in/token") == []

    # Clean card works even when sibling skills left ungranted client keys in Settings, with zero token keys.
    wired.settings.clear()
    wired.ungranted = {"GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"}
    clean = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert clean["verified"] and clean["configured"] is True and clean["sign_in_source"] == "ouroboros"

    # Explicit choice and settled moved do not need to read Settings Secrets at all.
    wired.settings_status = "unknown"
    wired.ungranted = set()
    reads_before = wired.settings_reads
    (tmp_path / user_oauth.MARKER_NAME).write_text(json.dumps({"handover": {
        "state": "moved", "generation": "g-moved", "account": {"key": card_key}, "connection_id": card_cid,
    }}), encoding="utf-8")
    moved_res = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert moved_res["verified"] and wired.settings_reads == reads_before

    (tmp_path / user_oauth.MARKER_NAME).unlink()
    user_oauth.save_selected_account(wired, card_key)
    explicit_res = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert explicit_res["verified"] and wired.settings_reads == reads_before


def test_v2_consent_pinning_imported_continuity_and_future_command_reconsent(wired, host, google, tmp_path):
    host.handover_version = 2
    # Missing expected consent on v2 refuses before requesting /sign-in/token.
    wired.settings.clear()
    no_consent_key = host.add(OWNER, "sub-no-consent", scope=GOOGLE_SCOPE, consent="")
    missing_res = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert not missing_res["verified"] and calls(host, "/sign-in/token") == []
    host.accounts.pop(no_consent_key)

    # Strict imported receipt pins its original consent; a concurrent re-consent before token aborts with 409.
    wired.settings.update({"GOOGLE_OAUTH_CLIENT_ID": CLIENT_ID,
                           "GOOGLE_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
                           "GOOGLE_OAUTH_REFRESH_TOKEN": REFRESH})
    def concurrent_reconsent(key, row):
        host.accounts[key]["consent"] = "b" * 32
    host.on_handover = concurrent_reconsent
    aborted = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert not aborted["verified"] and "changed or was disconnected" in aborted["message"]
    assert len(calls(host, "/sign-in/token")) == 1
    assert calls(host, "/sign-in/token")[0]["consent"] != "b" * 32

    # Saved moved marker does not pin future new commands to the old finished consent.
    host.on_handover = None
    fresh_next = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert fresh_next["verified"] and calls(host, "/sign-in/token")[-1]["consent"] == "b" * 32

    # Mid-command consent change on step 2 of a multi-request read aborts.
    moved_key = next(iter(host.accounts))
    def flip_consent_mid_command(request):
        if request.url.params.get("alt") != "media":
            host.accounts[moved_key]["consent"] = "c" * 32
            return httpx.Response(200, json={"id": "file1", "name": "notes.txt", "mimeType": "text/plain"})
        return httpx.Response(200, text="unreachable")
    google["on_api"] = flip_consent_mid_command
    with pytest.raises(RuntimeError, match="changed or was disconnected"):
        plugin._make_drive_read_text(wired)("file1", auth_mode="oauth")


def test_v1_multistep_reuses_single_bearer_and_refuses_mid_operation_reissue(wired, host, google):
    wired.settings.clear()
    key = host.add(OWNER, "sub-owner", scope=GOOGLE_SCOPE)
    cid = host.accounts[key]["connection_id"]
    google["on_api"] = lambda req: (
        httpx.Response(200, json={"id": "file1", "name": "notes.txt", "mimeType": "text/plain"})
        if req.url.params.get("alt") != "media" else httpx.Response(200, text="same bearer")
    )
    assert json.loads(plugin._make_drive_read_text(wired)("file1", auth_mode="oauth"))["text"] == "same bearer"
    assert [(b["account"], b["connection_id"]) for b in calls(host, "/sign-in/token")] == [(key, cid), (key, cid)]

    # Step 2 still calls /sign-in/token so a mid-operation disconnect stops the read before step 2.
    saved_row = dict(host.accounts[key])
    def disconnect_after_meta(req):
        if req.url.params.get("alt") != "media":
            host.accounts.pop(key)
            return httpx.Response(200, json={"id": "file1", "name": "notes.txt", "mimeType": "text/plain"})
        return httpx.Response(200, text="must not run")
    google["bearers"].clear()
    google["on_api"] = disconnect_after_meta
    with pytest.raises(RuntimeError, match="drive_read_text failed"):
        plugin._make_drive_read_text(wired)("file1", auth_mode="oauth")
    assert google["bearers"] == [HOST_ACCESS]

    # A different bearer returned on step 2 on v1 (without consent proof) also stops the operation.
    host.accounts[key] = dict(saved_row)
    def swap_bearer_after_meta(req):
        if req.url.params.get("alt") != "media":
            host.accounts[key]["access_token"] = "host-access-token-other"
            return httpx.Response(200, json={"id": "file1", "name": "notes.txt", "mimeType": "text/plain"})
        return httpx.Response(200, text="must not run")
    google["bearers"].clear()
    google["on_api"] = swap_bearer_after_meta
    with pytest.raises(RuntimeError, match="sign-in v1"):
        plugin._make_drive_read_text(wired)("file1", auth_mode="oauth")
    assert google["bearers"] == [HOST_ACCESS]

    # After 401 on v1, /sign-in/token refreshes the host for the next command, but a different bearer stops this command.
    host.accounts[key] = dict(saved_row)
    host.requests.clear()
    google["bearers"].clear()
    google["on_api"] = None
    google["refuse_once"].add(HOST_ACCESS)
    def refresh_to_new_bearer(_k, row, body):
        if body.get("rejected") == row.get("access_token", HOST_ACCESS):
            row["access_token"] = "host-access-token-2"
    host.on_token = refresh_to_new_bearer
    first = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert not first["verified"] and "sign-in v1" in first["message"]
    assert len(calls(host, "/sign-in/token")) == 2 and calls(host, "/sign-in/token")[1]["rejected"] == HOST_ACCESS
    assert google["bearers"] == [HOST_ACCESS]
    second = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert second["verified"] and google["bearers"] == [HOST_ACCESS, "host-access-token-2"]

    # On v2 with matching consent, normal 401 refresh continues working within the same command.
    host.handover_version = 2
    host.on_token = lambda _k, row, body: row.__setitem__("access_token", "host-access-token-3") if body.get("rejected") else None
    google["refuse_once"].add("host-access-token-2")
    v2_res = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert v2_res["verified"] and google["bearers"][-2:] == ["host-access-token-2", "host-access-token-3"]


def test_pending_kept_newer_reconciles_once_via_post_receipt_without_sending_credentials(wired, host, google, tmp_path):
    host.handover_version = 2
    card_key = host.add(OWNER, "sub-owner", scope=GOOGLE_SCOPE, via="card")
    card_cid = host.accounts[card_key]["connection_id"]
    (tmp_path / user_oauth.MARKER_NAME).write_text(
        json.dumps({"handover": {"state": "pending", "generation": "gen-lost-1",
                                 "settings_fingerprint": user_oauth._settings_fingerprint(
                                     plugin._get_oauth_credentials(wired))}}), encoding="utf-8"
    )
    # Refusal / missing receipt keeps pending closed and starts no second exchange.
    no_receipt = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert not no_receipt["verified"] and calls(host, "/sign-in/token") == []
    assert len(calls(host, "/sign-in/handover")) == 1
    assert calls(host, "/sign-in/handover")[0] == {
        "service": "google", "receipt": {"legacy_key": "settings", "legacy_generation": "gen-lost-1"},
    }

    # Valid kept_newer receipt linked to a verified row reconciles in ONE receipt query (not two across lock).
    host.receipts[("settings", "gen-lost-1")] = {
        "legacy_key": "settings", "legacy_generation": "gen-lost-1", "result": "kept_newer",
        "state": "signed_in", "via": "card", "connection_id": card_cid,
        "account": {"key": card_key, "label": OWNER},
    }
    settled = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert settled["verified"] and settled["sign_in_source"] == "ouroboros"
    assert len(calls(host, "/sign-in/handover")) == 2  # +1 for this pass, not +2
    saved = json.loads((tmp_path / user_oauth.MARKER_NAME).read_text(encoding="utf-8"))["handover"]
    assert saved["state"] == "moved" and saved["connection_id"] == card_cid and "consent" not in saved


def test_ordinary_tool_surfaces_rotation_notice_and_rereads_settings_under_lock(wired, host, google):
    host.sign_in_version = None
    google["rotated"] = ROTATED_REFRESH
    google["on_api"] = lambda req: httpx.Response(200, json={"files": []})
    out = json.loads(plugin._make_drive_list(wired)(auth_mode="oauth"))
    assert "Only the newly issued access token can be used while it remains valid in this call" in out["sign_in_notice"]
    assert "Settings → Secrets" in out["sign_in_notice"] and user_oauth.CARD not in out["sign_in_notice"]
    assert "do not send secrets in chat" in out["sign_in_notice"]
    assert SECRET_SHAPES.search(json.dumps(out)) is None
    assert SECRET_SHAPES.search(json.dumps(wired.logged)) is None

    # Re-reading Settings under _transition_lock stops an old command if credentials changed; the next command uses the new ones.
    host.sign_in_version = 1
    host.handover_version = 2
    auth._TOKEN_CACHE.clear()
    route = user_oauth.user_route(wired, scope=user_oauth.SCOPES["drive"],
                                  credentials_reader=lambda: plugin._read_oauth_settings_state(wired))
    wired.settings["GOOGLE_OAUTH_CLIENT_SECRET"] = "updated-before-lock"
    with pytest.raises(user_oauth.UserOAuthError, match="Settings changed"):
        route.legacy_provider(google["client"], None)
    assert calls(host, "/sign-in/handover") == []
    google["on_api"] = None
    next_res = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert next_res["verified"] and calls(host, "/sign-in/handover")[-1]["clients"][0]["client_secret"] == "updated-before-lock"


@pytest.mark.parametrize("host_mode", ["absent", "no_host", "legacy_ready"])
def test_mid_command_settings_or_source_change_stops_old_and_next_uses_new(wired, host, google, tmp_path, monkeypatch, host_mode):
    if host_mode == "absent":
        host.sign_in_version = None
    elif host_mode == "no_host":
        monkeypatch.delenv("HOST_SERVICE_URL", raising=False)
        monkeypatch.delenv("HOST_SERVICE_TOKEN", raising=False)
    else:
        host.sign_in_version = 1
        host.handover_version = 0

    # Ordinary multi-step read: after step 1 succeeds, owner changes optional access token in Settings.
    wired.settings["GOOGLE_OAUTH_ACCESS_TOKEN"] = LEGACY_ACCESS
    def change_settings_after_step1(req):
        if req.url.params.get("alt") != "media":
            wired.settings["GOOGLE_OAUTH_ACCESS_TOKEN"] = "new-settings-bearer"
            return httpx.Response(200, json={"id": "file1", "name": "notes.txt", "mimeType": "text/plain"})
        return httpx.Response(200, text="content")
    google["on_api"] = change_settings_after_step1
    with pytest.raises(RuntimeError, match="Settings changed"):
        plugin._make_drive_read_text(wired)("file1", auth_mode="oauth")
    assert google["bearers"] == [LEGACY_ACCESS]

    # Fresh next command picks up the new Settings credentials cleanly.
    google["bearers"].clear()
    google["on_api"] = lambda req: (
        httpx.Response(200, json={"id": "file1", "name": "notes.txt", "mimeType": "text/plain"})
        if req.url.params.get("alt") != "media" else httpx.Response(200, text="fresh content")
    )
    fresh = json.loads(plugin._make_drive_read_text(wired)("file1", auth_mode="oauth"))
    assert fresh["text"] == "fresh content" and google["bearers"] == ["new-settings-bearer", "new-settings-bearer"]

    # After step 1, owner selects another account: old command stops; next command obeys the new choice.
    card_key = host.add(OTHER, "sub-other", scope=GOOGLE_SCOPE) if host_mode == "legacy_ready" else OTHER
    def select_account_after_step1(req):
        if req.url.params.get("alt") != "media":
            user_oauth.save_selected_account(wired, card_key)
            return httpx.Response(200, json={"id": "file1", "name": "notes.txt", "mimeType": "text/plain"})
        return httpx.Response(200, text="must not run")
    google["bearers"].clear()
    google["on_api"] = select_account_after_step1
    with pytest.raises(RuntimeError, match="selected Google account changed"):
        plugin._make_drive_read_text(wired)("file1", auth_mode="oauth")
    assert google["bearers"] == ["new-settings-bearer"]
    if host_mode == "legacy_ready":
        google["bearers"].clear()
        google["on_api"] = lambda req: (
            httpx.Response(200, json={"id": "file1", "name": "notes.txt", "mimeType": "text/plain"})
            if req.url.params.get("alt") != "media" else httpx.Response(200, text="card content")
        )
        switched = json.loads(plugin._make_drive_read_text(wired)("file1", auth_mode="oauth"))
        assert switched["text"] == "card content" and google["bearers"] == [HOST_ACCESS, HOST_ACCESS]
    else:
        after_select = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
        assert not after_select["verified"] and "not used in its place" in after_select["message"]
        user_oauth.save_selected_account(wired, "")
        for mutate in (
            lambda: (tmp_path / user_oauth.MARKER_NAME).write_text(json.dumps({
                "handover": {"state": "moved", "generation": "g1",
                             "account": {"key": host_key("sub-other")}, "connection_id": "conn-1"},
            }), encoding="utf-8"),
            lambda: (tmp_path / user_oauth.WITHDRAWN_NAME).write_text(json.dumps({
                "v": 1, "accounts": [{"service": "google", "legacy_key": "settings"}],
            }), encoding="utf-8"),
        ):
            (tmp_path / user_oauth.MARKER_NAME).unlink(missing_ok=True)
            (tmp_path / user_oauth.WITHDRAWN_NAME).unlink(missing_ok=True)
            def mutate_after_step1(req, fn=mutate):
                if req.url.params.get("alt") != "media":
                    fn()
                    return httpx.Response(200, json={"id": "file1", "name": "notes.txt", "mimeType": "text/plain"})
                return httpx.Response(200, text="must not run")
            google["bearers"].clear()
            google["on_api"] = mutate_after_step1
            with pytest.raises(RuntimeError, match="drive_read_text failed"):
                plugin._make_drive_read_text(wired)("file1", auth_mode="oauth")
            assert google["bearers"] == ["new-settings-bearer"]


def test_untouched_settings_and_connect_refused_keep_settings_without_account_scan(wired, host, google, tmp_path):
    host.accounts_status = 500
    res = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert res["verified"] and res["sign_in_source"] == "skill_settings"
    assert calls(host, "/sign-in/accounts") == []

    host.handover_version = 2
    host.handover_error = urllib.error.URLError(ConnectionRefusedError("refused"))
    auth._TOKEN_CACHE.clear()
    ok = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert ok["verified"] and ok["sign_in_source"] == "skill_settings"
    assert len(calls(host, "/sign-in/handover")) == 1 and calls(host, "/sign-in/accounts") == []
    marker = json.loads((tmp_path / user_oauth.MARKER_NAME).read_text(encoding="utf-8"))["handover"]
    assert marker["state"] == "kept_legacy" and marker["reason"] == "host_unreachable"

    google["bearers"].clear()
    user_oauth.save_selected_account(wired, OWNER)
    explicit = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert not explicit["verified"] and len(calls(host, "/sign-in/accounts")) == 1 and google["bearers"] == []


def test_bound_pre_exchange_receipt_keeps_settings_and_rechecks_the_connection(wired, host, google, tmp_path, monkeypatch):
    host.handover_version = 2
    host.handover_error = TimeoutError("lost")
    assert not json.loads(plugin._make_workspace_auth_status(wired)("oauth"))["verified"]
    marker = json.loads((tmp_path / user_oauth.MARKER_NAME).read_text(encoding="utf-8"))["handover"]
    assert marker["state"] == "pending" and marker["generation"] and marker["settings_fingerprint"]
    assert SECRET_SHAPES.search(json.dumps(marker)) is None

    host.handover_error = None
    host.requests.clear()
    auth._TOKEN_CACHE.clear()
    google["bearers"].clear()
    host.receipts[("settings", marker["generation"])] = {
        "legacy_key": "settings", "legacy_generation": marker["generation"],
        "result": "refused", "reason": "provider_unreachable", "exchange": "none",
    }
    reopened = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert reopened["verified"] and reopened["sign_in_source"] == "skill_settings"
    assert calls(host, "/sign-in/handover") == [
        {"service": "google", "receipt": {"legacy_key": "settings", "legacy_generation": marker["generation"]}},
    ]
    assert json.loads((tmp_path / user_oauth.MARKER_NAME).read_text(encoding="utf-8"))["handover"]["state"] == "kept_legacy"

    (tmp_path / user_oauth.MARKER_NAME).write_text(json.dumps({"handover": marker}), encoding="utf-8")
    host.receipts[("settings", marker["generation"])]["reason"] = "unknown_client"
    report = json.loads(plugin._make_workspace_accounts(wired)())
    assert report["source"] == "skill_settings" and report["used"] == "settings"
    assert json.loads(plugin._make_workspace_auth_status(wired)("oauth"))["verified"]

    for mutate_settings in (False, True):
        (tmp_path / user_oauth.MARKER_NAME).unlink(missing_ok=True)
        wired.settings["GOOGLE_OAUTH_REFRESH_TOKEN"] = REFRESH
        auth._TOKEN_CACHE.clear()
        google["bearers"].clear()
        host.requests.clear()
        host.receipts.clear()
        step = [0]

        def scripted(body, mutate=mutate_settings):
            if "receipt" in body:
                query = body["receipt"]
                return {"ok": True, "receipt": host.receipts.get((query["legacy_key"], query["legacy_generation"]))}
            step[0] += 1
            gen = body["grants"][0]["legacy_generation"]
            if step[0] == 1:
                host.receipts[("settings", gen)] = {
                    "legacy_key": "settings", "legacy_generation": gen,
                    "result": "refused", "reason": "provider_unreachable", "exchange": "none",
                }
                raise TimeoutError("lost")
            assert body.get("recover") is True
            if mutate:
                wired.settings["GOOGLE_OAUTH_REFRESH_TOKEN"] = "changed-during-handover"
            return {"ok": True, "clients": [], "grants": [
                {"legacy_key": "settings", "result": "refused", "reason": "handover_not_started", "exchange": "unknown"},
            ]}

        with monkeypatch.context() as ctx:
            ctx.setattr(host, "handover", scripted)
            out = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
        assert out["verified"] is (not mutate_settings)
        saved = json.loads((tmp_path / user_oauth.MARKER_NAME).read_text(encoding="utf-8"))["handover"]
        assert saved["state"] == "kept_legacy"
        assert (google["bearers"] == []) is mutate_settings


def test_direct_refused_survives_temporary_settings_read_failure(wired, host, google, tmp_path, monkeypatch):
    host.handover_version = 2

    def refused_unreadable(_body):
        wired.settings_status = "unknown"
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    with monkeypatch.context() as ctx:
        ctx.setattr(host, "handover", refused_unreadable)
        blocked = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert not blocked["verified"] and blocked["configured"] is None and google["bearers"] == []
    saved = json.loads((tmp_path / user_oauth.MARKER_NAME).read_text(encoding="utf-8"))["handover"]
    assert saved["state"] == "kept_legacy" and saved["reason"] == "host_unreachable"

    wired.settings_status = "ok"
    auth._TOKEN_CACHE.clear()
    recovered = json.loads(plugin._make_workspace_auth_status(wired)("oauth"))
    assert recovered["verified"] and recovered["sign_in_source"] == "skill_settings"
    assert google["bearers"] == [LEGACY_ACCESS] and len(calls(host, "/sign-in/handover")) == 1


def test_direct_bearer_blocks_mid_command_refresh_after_first_success(wired, host, google, monkeypatch):
    wired.settings["GOOGLE_OAUTH_ACCESS_TOKEN"] = "manual-bearer"
    google["on_api"] = lambda req: (
        httpx.Response(200, json={"id": "file1", "name": "notes.txt", "mimeType": "text/plain"})
        if req.url.params.get("alt") != "media" else httpx.Response(401, json={"error": {"message": "Expired"}})
    )
    with pytest.raises(RuntimeError, match="switching consent mid-command"):
        plugin._make_drive_read_text(wired)("file1", auth_mode="oauth")
    assert google["refreshes"] == [] and google["bearers"] == ["manual-bearer", "manual-bearer"]

    auth._TOKEN_CACHE.clear()
    google["bearers"].clear()
    issued = ["refreshed-1", "refreshed-2"]

    def respond_seq(request):
        if request.url.host == "oauth2.googleapis.com":
            google["refreshes"].append(request.content.decode())
            return httpx.Response(200, json={"access_token": issued.pop(0), "expires_in": 3600, "token_type": "Bearer"})
        bearer = (request.headers.get("Authorization") or "").removeprefix("Bearer ")
        google["bearers"].append(bearer)
        if request.url.params.get("alt") != "media":
            return httpx.Response(401) if bearer == "manual-bearer" else httpx.Response(
                200, json={"id": "file1", "name": "notes.txt", "mimeType": "text/plain"})
        return httpx.Response(401) if bearer == "refreshed-1" else httpx.Response(200, text="same consent")

    with httpx.Client(transport=httpx.MockTransport(respond_seq)) as seq_http:
        monkeypatch.setitem(google, "client", seq_http)
        ok = json.loads(plugin._make_drive_read_text(wired)("file1", auth_mode="oauth"))
    assert ok["text"] == "same consent" and len(google["refreshes"]) == 2
    assert google["bearers"] == ["manual-bearer", "refreshed-1", "refreshed-1", "refreshed-2"]
