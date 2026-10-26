"""User OAuth lifetime, actor verification and provider-shaped request coverage."""

from concurrent.futures import ThreadPoolExecutor
import json
from urllib.parse import parse_qs

import httpx
import pytest

import auth
import client as client_module
from client import GoogleWorkspaceClient, _validate_resource_id
import plugin


OAUTH = {"client_id": "client-fixture", "client_secret": "secret-fixture", "refresh_token": "refresh-fixture"}


@pytest.fixture(autouse=True)
def clear_token_cache():
    auth._TOKEN_CACHE.clear()
    yield
    auth._TOKEN_CACHE.clear()


def test_refresh_cache_expiry_and_credential_rotation(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    calls = []

    def respond(request):
        assert str(request.url) == auth.GOOGLE_TOKEN_URI
        assert request.method == "POST"
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        calls.append(form)
        return httpx.Response(200, json={"access_token": f"token-{len(calls)}", "expires_in": 3600})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        get = lambda **changes: auth.get_user_access_token(**(OAUTH | changes), http_client=http)
        assert get() == get() == "token-1"
        clock[0] += 3541
        assert get() == "token-2"
        assert get(refresh_token="another-user") == "token-3"
        assert get(client_secret="rotated-secret") == "token-4"
    assert len(calls) == 4
    assert calls[0]["client_id"] == [OAUTH["client_id"]]
    assert calls[0]["client_secret"] == [OAUTH["client_secret"]]
    assert calls[2]["refresh_token"] == ["another-user"]


def test_concurrent_calls_share_one_refresh():
    calls = []

    def respond(request):
        calls.append(request.url)
        return httpx.Response(200, json={"access_token": "shared-token", "expires_in": 3600})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(lambda _: auth.get_user_access_token(**OAUTH, http_client=http), range(16)))
    assert tokens == ["shared-token"] * 16
    assert len(calls) == 1


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_access_token_reused_then_401_refreshes_once(method):
    calls = []

    def respond(request):
        calls.append((request.url.host, request.headers.get("Authorization")))
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600})
        if request.headers["Authorization"] == "Bearer stale-token":
            return httpx.Response(401, json={"error": {"message": "Expired"}})
        return httpx.Response(200, json={"id": "file123"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with GoogleWorkspaceClient(**OAUTH, access_token="stale-token", http_client=http) as client:
            result = client.workspace_request("drive", method, "files", json_body={"name": "Draft"})
            assert result["data"]["id"] == "file123"
            client.workspace_request("drive", "GET", "files/file123")
    assert calls == [("www.googleapis.com", "Bearer stale-token"),
                     ("oauth2.googleapis.com", None),
                     ("www.googleapis.com", "Bearer fresh-token"),
                     ("www.googleapis.com", "Bearer fresh-token")]


def test_second_401_stops_after_one_refresh():
    calls = []

    def respond(request):
        calls.append(request.url.host)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600})
        return httpx.Response(401, json={"error": {"message": "Unauthorized"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with GoogleWorkspaceClient(**OAUTH, access_token="stale-token", http_client=http) as client:
            with pytest.raises(RuntimeError, match="401"):
                client.workspace_request("docs", "POST", "documents", json_body={"title": "Draft"})
    assert calls == ["docs.googleapis.com", "oauth2.googleapis.com", "docs.googleapis.com"]


def test_invalid_refresh_grant_does_not_echo_secrets_or_fall_back():
    calls = []

    def respond(request):
        calls.append(request.url.host)
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": OAUTH["refresh_token"]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with GoogleWorkspaceClient(**OAUTH, raw_sa_info="must-not-use", http_client=http) as client:
            with pytest.raises(RuntimeError, match="invalid_grant") as error:
                client.workspace_request("drive", "GET", "about", query={"fields": "user"})
    assert OAUTH["refresh_token"] not in str(error.value)
    assert calls == ["oauth2.googleapis.com"]


@pytest.mark.parametrize("outcome", ["timeout", "server_error"])
def test_mutative_transport_failure_is_not_retried(outcome):
    calls = []

    def respond(request):
        calls.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("Ambiguous write response", request=request)
        return httpx.Response(503, text="Unavailable")

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with GoogleWorkspaceClient(access_token="fixture-token", http_client=http) as client:
            with pytest.raises((httpx.ReadTimeout, RuntimeError)):
                client.workspace_request("drive", "POST", "files", json_body={"name": "Draft"})
    assert len(calls) == 1


class FakeAPI:
    def __init__(self, settings):
        self.settings = settings
        self.requested = []

    def get_settings(self, keys):
        self.requested.extend(keys)
        return {key: self.settings[key] for key in keys if key in self.settings}


@pytest.mark.parametrize("mode", ["oauth", "service_account"])
def test_auth_status_verifies_actual_actor_and_keeps_file_access_separate(mode, monkeypatch):
    actor = {"emailAddress": "actual-user@example.invalid", "permissionId": "actor123", "displayName": "Actual actor"}
    settings = {"GOOGLE_OAUTH_ACCESS_TOKEN": "fixture-token"}
    if mode == "service_account":
        settings = {"GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps({"type": "service_account", "client_email": "sa@example.invalid",
                    "private_key": "fixture", "token_uri": auth.GOOGLE_TOKEN_URI})}
        monkeypatch.setattr(client_module, "get_access_token", lambda **kwargs: "service-token")

    def respond(request):
        assert str(request.url) == "https://www.googleapis.com/drive/v3/about?fields=user"
        expected = "fixture-token" if mode == "oauth" else "service-token"
        assert request.headers["Authorization"] == f"Bearer {expected}"
        return httpx.Response(200, json={"user": actor})

    api = FakeAPI(settings)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(plugin, "GoogleWorkspaceClient", lambda **kwargs: GoogleWorkspaceClient(http_client=http, **kwargs))
        result = json.loads(plugin._make_workspace_auth_status(api)(mode))
    assert result["configured"] is True and result["verified"] is True
    assert result["status"] == "ready" and result["actor"] == actor
    assert result["resource_access"] == "not_checked"
    if mode == "oauth":
        assert "GOOGLE_SERVICE_ACCOUNT_JSON" not in api.requested
    else:
        assert api.requested == ["GOOGLE_SERVICE_ACCOUNT_JSON"]
        assert result["client_email"] != result["actor"]["emailAddress"]


@pytest.mark.parametrize("response", [httpx.Response(401, text="Expired"), httpx.Response(403, text="Drive API disabled"), httpx.Response(200, json={})])
def test_configured_oauth_does_not_mean_verified(response, monkeypatch):
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as http:
        monkeypatch.setattr(plugin, "GoogleWorkspaceClient", lambda **kwargs: GoogleWorkspaceClient(http_client=http, **kwargs))
        result = json.loads(plugin._make_workspace_auth_status(FakeAPI({"GOOGLE_OAUTH_ACCESS_TOKEN": "expired"}))("oauth"))
    assert result["configured"] is True and result["verified"] is False
    assert result["status"] == "error" and result["actor"] is None


def test_workspace_request_plugin_loads_refresh_grants(monkeypatch):
    settings = {"GOOGLE_OAUTH_" + key.upper(): value for key, value in OAUTH.items()}
    api = FakeAPI(settings)
    seen = []

    def respond(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer fresh"
        seen.append((request.method, str(request.url), json.loads(request.content) if request.content else None))
        return httpx.Response(204) if request.method == "DELETE" else httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(plugin, "GoogleWorkspaceClient", lambda **kwargs: GoogleWorkspaceClient(http_client=http, **kwargs))
        handler = plugin._make_workspace_request(api)
        assert json.loads(handler("drive", "GET", "files/abc", {"fields": "id,name"}, auth_mode="oauth"))["data"] == {"ok": True}
        handler("sheets", "POST", "spreadsheets/abc:batchUpdate", json_body={"requests": []}, auth_mode="oauth")
        assert json.loads(handler("drive", "DELETE", "files/abc", auth_mode="oauth")) == {"status_code": 204}
    assert seen[0] == ("GET", "https://www.googleapis.com/drive/v3/files/abc?fields=id%2Cname", None)
    assert seen[1] == ("POST", "https://sheets.googleapis.com/v4/spreadsheets/abc:batchUpdate", {"requests": []})
    assert set(settings) <= set(api.requested)


def test_slides_and_calendar_requests_reach_their_apis_with_user_oauth(monkeypatch):
    settings = {"GOOGLE_OAUTH_" + key.upper(): value for key, value in OAUTH.items()}
    seen = []
    hide = {"requests": [{"updateSlideProperties": {
        "objectId": "slide7", "slideProperties": {"isSkipped": True}, "fields": "isSkipped"}}]}
    attach = {"attachments": [{"fileUrl": "https://drive.google.com/open?id=deck1", "title": "Deck",
                               "mimeType": "application/vnd.google-apps.presentation"}]}

    def respond(request):
        if request.url.host == "oauth2.googleapis.com":
            assert "scope" not in parse_qs(request.content.decode())
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        seen.append((request.method, str(request.url), json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(plugin, "GoogleWorkspaceClient", lambda **kwargs: GoogleWorkspaceClient(http_client=http, **kwargs))
        handler = plugin._make_workspace_request(FakeAPI(settings))
        handler("slides", "POST", "presentations/deck1:batchUpdate", json_body=hide, auth_mode="oauth")
        handler("calendar", "PATCH", "calendars/primary/events/ev1_20261030T130000Z",
                {"supportsAttachments": "true"}, attach, auth_mode="oauth")
    assert seen == [
        ("POST", "https://slides.googleapis.com/v1/presentations/deck1:batchUpdate", hide),
        ("PATCH", "https://www.googleapis.com/calendar/v3/calendars/primary/events/ev1_20261030T130000Z"
                  "?supportsAttachments=true", attach),
    ]


def test_service_account_asks_for_calendar_scope_only_on_calendar_calls(monkeypatch):
    requested = []

    def fake_token(**kwargs):
        requested.append(kwargs.get("scopes"))
        return "calendar-token" if kwargs.get("scopes") else "base-token"

    monkeypatch.setattr(client_module, "get_access_token", fake_token)
    tokens = []

    def respond(request):
        tokens.append((request.url.host, request.headers["Authorization"]))
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with GoogleWorkspaceClient(raw_sa_info="fixture", http_client=http) as client:
            for service, path in (("drive", "about"), ("slides", "presentations/deck1"),
                                  ("calendar", "calendars/primary/events"), ("docs", "documents/doc1")):
                client.workspace_request(service, "GET", path)
    assert requested == [None, None, auth.CALENDAR_SCOPES, None]
    assert tokens == [("www.googleapis.com", "Bearer base-token"), ("slides.googleapis.com", "Bearer base-token"),
                      ("www.googleapis.com", "Bearer calendar-token"), ("docs.googleapis.com", "Bearer base-token")]


def test_service_account_calendar_401_refreshes_with_the_calendar_scope(monkeypatch):
    requested = []

    def fake_token(**kwargs):
        requested.append((kwargs.get("scopes"), kwargs.get("force_refresh")))
        return "fresh-calendar" if kwargs.get("force_refresh") else "stale-calendar"

    monkeypatch.setattr(client_module, "get_access_token", fake_token)

    def respond(request):
        if request.headers["Authorization"] == "Bearer stale-calendar":
            return httpx.Response(401, json={"error": {"message": "Expired"}})
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with GoogleWorkspaceClient(raw_sa_info="fixture", http_client=http) as client:
            assert client.workspace_request("calendar", "GET", "calendars/primary/events")["data"] == {"items": []}
    assert requested == [(auth.CALENDAR_SCOPES, False), (auth.CALENDAR_SCOPES, True)]


def test_unknown_service_is_rejected_before_any_request():
    def respond(request):
        raise AssertionError("no request expected")

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with GoogleWorkspaceClient(access_token="fixture", http_client=http) as client:
            with pytest.raises(ValueError, match="drive, docs, sheets, slides or calendar"):
                client.workspace_request("gmail", "GET", "users/me/messages")


@pytest.mark.parametrize("link", ["file_123-abc", "https://docs.google.com/document/d/file_123-abc/edit",
    "https://docs.google.com/spreadsheets/d/file_123-abc/edit#gid=3", "https://drive.google.com/file/d/file_123-abc/view?usp=sharing",
    "https://drive.google.com/drive/u/0/folders/file_123-abc", "https://drive.google.com/open?id=file_123-abc"])
def test_common_google_links_normalize_to_resource_id(link):
    assert _validate_resource_id(link) == "file_123-abc"


def test_document_url_reaches_provider_as_id():
    def respond(request):
        assert request.url.path == "/v1/documents/doc123"
        return httpx.Response(200, json={"documentId": "doc123"})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with GoogleWorkspaceClient(access_token="fixture", http_client=http) as client:
            assert client.docs_read("https://docs.google.com/document/d/doc123/edit")["document_id"] == "doc123"


def test_missing_parent_never_creates_an_orphan_document():
    calls = []
    def respond(request):
        calls.append(request)
        assert request.url.path == "/drive/v3/files"
        assert json.loads(request.content)["parents"] == ["missing-folder"]
        return httpx.Response(404, json={"error": {"message": "File not found: missing-folder"}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with GoogleWorkspaceClient(access_token="fixture", http_client=http) as client:
            with pytest.raises(RuntimeError, match="404"):
                client.docs_create("Draft", folder_id="missing-folder")
    assert len(calls) == 1
