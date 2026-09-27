"""Playwright driver used only by the companion: one visible Chrome, isolated profile.

Playwright's sync API is bound to the companion's main thread, which is also
the thread that serves requests, so every call below runs in order.

A driver built with `fixtures` is synthetic: every request of its context is
answered from that mapping or aborted, service workers are blocked and host
name resolution is disabled, so nothing reaches the network. Only such a
driver is used by the offline browser tests.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

SERVICE_HOST = "eda.yandex.kz"  # the only origin observer.js reads (mirrors eats_session.SERVICE_HOST)
_ELEMENT = ("([key, obs, index, host]) => {"
            " if (location.protocol !== 'https:' || location.hostname !== host) return null;"
            " const reg = window[key];"
            " return reg && reg.obs === obs ? reg.els[index] || null : null; }")
_TIMEOUT_MS = 8000


def fixture_key(url: str) -> str:
    """scheme://host/path of a request, the key of a synthetic fixture page."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}{parts.path or '/'}"


class PlaywrightDriver:
    def __init__(self, profile_dir: Path, *, fixtures: Mapping[str, str] | None = None,
                 headless: bool = False, channel: str | None = "chrome") -> None:
        self._profile = Path(profile_dir)
        self.synthetic = fixtures is not None
        self._fixtures = {fixture_key(url): str(body) for url, body in (fixtures or {}).items()}
        self._headless = headless
        self._channel = channel
        self._observer = (Path(__file__).resolve().parent / "observer.js").read_text(encoding="utf-8")
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None
        self._closed = True
        self._epoch = 0

    # -- lifecycle
    def launch(self) -> None:
        from playwright.sync_api import sync_playwright

        if not self._profile.is_dir():
            raise FileNotFoundError("the prepared profile directory is missing")
        pw = sync_playwright().start()
        options: dict[str, Any] = {"headless": self._headless, "locale": "ru-RU"}
        if self._channel:
            options["channel"] = self._channel
        if self._headless:
            options["viewport"] = {"width": 1280, "height": 900}
        else:
            options["no_viewport"] = True  # the owner's window keeps its own size
        if self.synthetic:
            options["service_workers"] = "block"
            options["args"] = ["--host-resolver-rules=MAP * ~NOTFOUND"]
        try:
            context = pw.chromium.launch_persistent_context(str(self._profile), **options)
        except Exception:
            pw.stop()
            raise
        try:
            if self.synthetic:
                context.route("**/*", self._fixture_route)  # before any page loads
        except Exception:
            with suppress(Exception):
                context.close()
            pw.stop()
            raise
        self._pw, self._context, self._closed = pw, context, False
        context.on("close", lambda *_: setattr(self, "_closed", True))
        self._adopt(context.pages[0] if context.pages else context.new_page())

    def _fixture_route(self, route: Any) -> None:
        body = self._fixtures.get(fixture_key(route.request.url))
        if body is None:
            route.abort()
        else:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

    def _adopt(self, page: Any) -> None:
        self._page = page
        self._epoch += 1

        def moved(frame: Any) -> None:
            if page is self._page and frame == page.main_frame:
                self._epoch += 1

        page.on("framenavigated", moved)

    def alive(self) -> bool:
        if self._closed or self._context is None:
            return False
        with suppress(Exception):
            if self._page is not None and not self._page.is_closed():
                self._page.wait_for_timeout(1)  # delivers popup and navigation events
            pages = [page for page in self._context.pages if not page.is_closed()]
            if not pages:
                return False
            if self._page is not pages[-1]:
                self._adopt(pages[-1])  # a new tab invalidates ids from the previous one
                with suppress(Exception):
                    self._page.wait_for_load_state("domcontentloaded", timeout=8000)
            return not self._closed and not self._page.is_closed()
        return False

    def pump(self, ms: int) -> bool:
        if self._closed or self._page is None:
            return False
        with suppress(Exception):
            self._page.wait_for_timeout(ms)
            return True
        return False

    def close(self) -> None:
        context, pw = self._context, self._pw
        self._context = self._page = self._pw = None
        self._closed = True
        try:
            if context is not None:
                with suppress(Exception):
                    context.close()
        finally:
            if pw is not None:
                with suppress(Exception):
                    pw.stop()

    # -- page facts
    def url(self) -> str:
        return str(self._page.url)

    def epoch(self) -> int:
        return self._epoch

    def other_tabs(self) -> list[str]:
        return [str(page.url) for page in self._context.pages if page is not self._page and not page.is_closed()]

    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded", timeout=30000)

    # -- observer (reads only https://SERVICE_HOST documents; returns {blocked} elsewhere)
    def _eval(self, args: dict[str, Any], *, retry: bool = False) -> Any:
        args = {**args, "service_host": SERVICE_HOST}
        try:
            return self._page.evaluate(self._observer, args)
        except Exception:
            if not retry:
                raise
            self.settle(2000)  # usually a navigation replaced the document mid-read
            return self._page.evaluate(self._observer, args)

    def observe(self, key: str, obs: str, options: dict[str, Any]) -> dict[str, Any]:
        return self._eval({"op": "observe", "key": key, "obs": obs, **options}, retry=True)

    def inspect(self, key: str, obs: str, index: int, expect: dict[str, str]) -> dict[str, Any]:
        return self._eval({"op": "inspect", "key": key, "obs": obs, "index": index, "expect": expect})

    def settle(self, max_ms: int) -> None:
        with suppress(Exception):
            self._page.wait_for_load_state("domcontentloaded", timeout=10000)
        with suppress(Exception):
            self._eval({"op": "settle", "max_ms": max_ms})

    def scroll(self, direction: str) -> None:
        """Scripted scroll of the scroller under the viewport centre: no pointer, key or focus event."""
        self._eval({"op": "scroll", "direction": direction})

    def capture_element(self, key: str, obs: str, index: int, path: Path) -> None:
        """Privately capture one observed region; never deliver an uninspected image."""
        handle = self._page.evaluate_handle(_ELEMENT, [key, obs, index, SERVICE_HOST])
        element = handle.as_element()
        if element is None:
            handle.dispose()
            raise LookupError("element is no longer registered")
        try:
            element.screenshot(
                path=str(path), timeout=15000,
                mask=[self._page.locator("input"), self._page.locator("textarea"),
                      self._page.locator("select"), self._page.locator("iframe"),
                      self._page.locator('[role~="textbox"],[role~="searchbox"],[role~="combobox"]'),
                      self._page.locator("[contenteditable]")],
                mask_color="#000000",
            )
        finally:
            with suppress(Exception):
                element.dispose()

    def capture_viewport(self, path: Path) -> None:
        """Capture the visible Eats page, with editable fields masked."""
        self._page.screenshot(
            path=str(path), timeout=15000, full_page=False,
            mask=[self._page.locator("input"), self._page.locator("textarea"),
                  self._page.locator("select"), self._page.locator("iframe"),
                  self._page.locator('[role~="textbox"],[role~="searchbox"],[role~="combobox"]'),
                  self._page.locator("[contenteditable]")], mask_color="#000000",
        )

    # -- effects: after the session verified the target and page origin
    def perform(self, key: str, obs: str, index: int, action: str, value: str) -> None:
        handle = self._page.evaluate_handle(_ELEMENT, [key, obs, index, SERVICE_HOST])
        element = handle.as_element()
        if element is None:
            handle.dispose()
            raise LookupError("element is no longer registered")
        try:
            if action == "click":
                element.click(timeout=_TIMEOUT_MS)
            elif action == "fill":
                element.fill(value, timeout=_TIMEOUT_MS)
            elif action == "press":
                element.press(value, timeout=_TIMEOUT_MS)
            else:
                raise ValueError(f"unsupported action {action!r}")
        finally:
            with suppress(Exception):
                element.dispose()
