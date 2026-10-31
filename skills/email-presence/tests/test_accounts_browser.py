"""Actual native-module widget bridge, account routes and OAuth return consumer.

The only substituted boundary is the provider. Run with OUROBOROS_SOURCE_DIR
and OUROBOROS_RUN_MAIL_BROWSER=1. No installed skill or personal account is used.
"""
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from lib.accounts import AccountStore
from test_plugin import _settings_app

SKILL = Path(__file__).resolve().parents[1]
HOST = '''<!doctype html><html><head><meta name="viewport" content="width=device-width">
<script src="/static/theme.js"></script><link rel="stylesheet" href="/static/ui.css">
<style>body{margin:0}iframe{width:100%;height:780px;border:0}</style></head><body>
<section><div class="widgets-card-status"></div><div id="mount"></div></section><script type="module">
import {mountModuleWidget} from '/static/modules/widget_module.js';
ouroTheme.set('light');
window.disposeWidget=await mountModuleWidget(document.getElementById('mount'),
 {skill:'email-presence',tab_id:'email_presence',generation:'fixture'},
 {entry:'ui/accounts.js',entry_type:'module',assets:['ui/accounts.css'],height:780,appearance:'host'},null,new Set());
window.ready=true;
</script></body></html>'''


def core_path():
    if os.environ.get("OUROBOROS_RUN_MAIL_BROWSER") != "1" or not os.environ.get("OUROBOROS_SOURCE_DIR"):
        pytest.skip("set OUROBOROS_SOURCE_DIR and OUROBOROS_RUN_MAIL_BROWSER=1 for the actual widget host")
    return Path(os.environ["OUROBOROS_SOURCE_DIR"])


@contextmanager
def serve(tmp_path, monkeypatch):
    import uvicorn
    core = core_path()
    skill_app = _settings_app(tmp_path, {"GOOGLE_OAUTH_CLIENT_ID": "fixture-google-client"})
    plugin = sys.modules["email_settings_fixture"]
    observed = {"posts": [], "oauth": [], "faults": []}
    async def home(_):
        return HTMLResponse(HOST)
    async def lease(request):
        if request.url.path.endswith("/revoke"):
            return JSONResponse({"ok": True})
        return JSONResponse({"token": "fixture", "base_url": "/api/widget-assets/fixture/", "expires_in": 600, "renew_after": 300})
    async def asset(request):
        path = request.path_params["path"]
        if path not in {"ui/accounts.js", "ui/accounts.css"}:
            return Response(status_code=404)
        return Response((SKILL / path).read_bytes(), media_type="text/javascript" if path.endswith("js") else "text/css",
                        headers={"Access-Control-Allow-Origin": "*"})
    async def language(_):
        return JSONResponse({"language": "en", "entries": {}})
    async def fault(request):
        observed["faults"].append(await request.json())
        return JSONResponse({"ok": True})
    async def record(scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST":
            observed["posts"].append(scope["path"])
        await skill_app(scope, receive, send)
    def google_request(method, url, **kwargs):
        if method == "HEAD":
            assert str(url).endswith("/onboarding/static/provider-return.html")
            return httpx.Response(200)
        if str(url).endswith("/token"):
            observed["oauth"].append(kwargs["data"])
            return httpx.Response(200, json={"access_token": "fixture-access", "refresh_token": "fixture-refresh", "expires_in": 3600,
                                             "scope": "openid email https://www.googleapis.com/auth/gmail.modify"})
        if str(url).endswith("/userinfo"):
            return httpx.Response(200, json={"sub": "provider-subject", "email_verified": True, "email": "google@example.invalid"})
        if str(url).endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "google@example.invalid"})
        if str(url).endswith("/sendAs"):
            return httpx.Response(200, json={"sendAs": []})
        raise AssertionError("Unexpected provider request " + str(url))
    monkeypatch.setattr(httpx, "request", google_request)
    monkeypatch.setattr(plugin.MailClient, "test_connection", lambda _: {"ok": True, "imap": "authenticated", "smtp": "authenticated"})
    app = Starlette(routes=[Route("/", home), Route("/api/widgets/asset-leases", lease, methods=["POST"]),
                           Route("/api/widgets/asset-leases/{action}", lease, methods=["POST"]),
                           Route("/api/widget-assets/fixture/{path:path}", asset), Route("/api/ui/i18n", language),
                           Route("/api/widgets/fault", fault, methods=["POST"]),
                           Mount("/api/extensions/email-presence", record), Mount("/static", StaticFiles(directory=core / "web")),
                           Mount("/onboarding/static", StaticFiles(directory=core / "web"))])
    app = CORSMiddleware(app, allow_origins=["*"])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    try:
        yield "http://127.0.0.1:" + str(sock.getsockname()[1]), observed
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


def screenshot(page, name):
    root = os.environ.get("OUROBOROS_MAIL_EVIDENCE_DIR")
    if root:
        folder = Path(root)
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / name), full_page=True)


def mount(page, origin):
    page.goto(origin)
    page.wait_for_function("window.ready === true")
    iframe = page.locator("iframe")
    assert "allow-same-origin" not in iframe.get_attribute("sandbox")
    frame = iframe.element_handle().content_frame()
    frame.get_by_role("heading", name="Email accounts", exact=True).wait_for()
    return frame


@pytest.mark.parametrize("width", [390, 1000])
def test_accounts_real_frame_forms_aliases_themes_disconnect(tmp_path, monkeypatch, width):
    core_path()
    from playwright.sync_api import sync_playwright, expect
    with serve(tmp_path, monkeypatch) as (origin, observed), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 900})
        try:
            frame = mount(page, origin)
            frame.get_by_role("button", name="Add IMAP / Proton Bridge", exact=True).click()
            frame.get_by_label("Mailbox email address", exact=True).fill("bridge@example.invalid")
            frame.get_by_label("App / Bridge password", exact=True).fill("fixture-password")
            frame.get_by_label("IMAP host", exact=True).fill("127.0.0.1")
            frame.get_by_label("SMTP host", exact=True).fill("127.0.0.1")
            frame.get_by_label("IMAP port", exact=True).fill("1143")
            frame.get_by_label("SMTP port", exact=True).fill("1025")
            frame.locator("select[name=imap_security]").select_option("starttls")
            aliases = [f"long.personal.alias-{n:02}@example.invalid" for n in range(20)]
            frame.get_by_label("Sender addresses (one per line)", exact=True).fill("\n".join(aliases))
            frame.get_by_role("button", name="Add account", exact=True).click()
            expect(frame.get_by_role("status")).to_contain_text("Saved bridge@example.invalid")
            assert "fixture-password" not in frame.locator("body").inner_text()
            frame.get_by_role("button", name="Test connection", exact=True).click()
            expect(frame.get_by_role("status")).to_contain_text("Authentication check succeeded")
            frame.get_by_role("button", name="Preferences", exact=True).click()
            frame.locator("select[name=sender]").select_option(aliases[-1])
            frame.locator("select[name=sending]").select_option("off")
            frame.get_by_label("Automatic intake folder / Gmail label", exact=True).press("Enter")
            expect(frame.get_by_role("status")).to_have_text("Preferences saved.")
            account = AccountStore(tmp_path).resolve()
            assert account["sender"] == aliases[-1] and account["sending"] == "off"
            assert account["config"]["EMAIL_IMAP_SECURITY"] == "starttls" and account["config"]["EMAIL_IMAP_PORT"] == 1143
            assert len(account["senders"]) == 21
            for theme in ("light", "dark"):
                page.evaluate("theme => ouroTheme.set(theme)", theme)
                expect(frame.locator("html")).to_have_attribute("data-theme", theme)
                assert not frame.evaluate("document.documentElement.scrollWidth > innerWidth")
                screenshot(page, f"accounts-{width}-{theme}.png")
            frame.get_by_role("button", name="Disconnect locally", exact=True).click()
            expect(frame.get_by_role("status")).to_contain_text("Disconnected locally")
            expect(frame.get_by_role("button", name="Reconnect", exact=True)).to_be_visible()
            saved = AccountStore(tmp_path).get(account["id"])
            assert not saved["connected"] and "EMAIL_PASSWORD" not in saved["config"]
            assert "/api/extensions/email-presence/accounts/preferences" in observed["posts"]
            assert not observed["faults"], observed["faults"]
            page.evaluate("() => disposeWidget()")
        finally:
            page.close()
            browser.close()


def test_google_return_connects_real_mail_consumer_and_denial_does_not_create_account(tmp_path, monkeypatch):
    core_path()
    from playwright.sync_api import sync_playwright, expect
    with serve(tmp_path, monkeypatch) as (origin, observed), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 900})
        try:
            frame = mount(page, origin)
            frame.get_by_role("button", name="Add Google mail", exact=True).click()
            assert frame.get_by_label("Ouroboros address for the return").input_value() == origin
            frame.get_by_label("Google account address").fill("google@example.invalid")
            frame.get_by_role("button", name="Prepare Google sign-in", exact=True).click()
            expect(frame.get_by_role("button", name="Continue in Google", exact=True)).to_be_visible()
            accounts = AccountStore(tmp_path)
            with accounts._db() as db:
                state = db.execute("SELECT state FROM oauth_pending").fetchone()[0]
            assert not accounts.list(), "Preparing or opening login is not a connected account"
            page.route("https://provider.example.invalid/return", lambda route: route.fulfill(content_type="text/html", body='<a id="continue">Return</a>'))
            page.goto("https://provider.example.invalid/return")
            callback = origin + "/onboarding/static/provider-return.html?" + urlencode({"code": "fixture-code", "state": state})
            page.locator("#continue").evaluate("(node, href) => node.href = href", callback)
            page.locator("#continue").click()
            page.locator('[data-return-status][data-tone="ok"]').wait_for()
            assert accounts.resolve()["id"] == "google:provider-subject"
            assert len(observed["oauth"]) == 1 and observed["oauth"][0]["redirect_uri"] == origin + "/onboarding/static/provider-return.html"
            assert "code=" not in page.url and "state=" not in page.url
            screenshot(page, "mail-google-return.png")
            frame = mount(page, origin)
            expect(frame.locator(".account")).to_contain_text("google@example.invalid")
            frame.get_by_role("button", name="Add Google mail", exact=True).click()
            frame.get_by_role("button", name="Prepare Google sign-in", exact=True).click()
            expect(frame.get_by_role("button", name="Continue in Google", exact=True)).to_be_visible()
            with accounts._db() as db:
                state = db.execute("SELECT state FROM oauth_pending").fetchone()[0]
            page.goto(origin + "/onboarding/static/provider-return.html?" + urlencode({"error": "access_denied", "state": state}))
            page.locator('[data-return-status][data-tone="warn"]').wait_for()
            expect(page.locator("[data-return-detail]")).to_contain_text("Google authorization was not granted")
            assert len(accounts.list()) == 1 and len(observed["oauth"]) == 1
            assert not observed["faults"], observed["faults"]
        finally:
            page.close()
            browser.close()
