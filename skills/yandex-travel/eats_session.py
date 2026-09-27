"""Yandex Eats browser session: loopback protocol, page policy, session core.

Tool handlers in plugin.py run in short-lived per-call children (the skill has
an isolated Playwright dependency), so they cannot keep a browser open. The
host-supervised companion (scripts/eats_browser.py) owns one visible Chrome
through a driver and serves tool calls over a loopback socket guarded by a
per-start token. This module never imports Playwright and holds no site
wording.

The agent can read and act on eda.yandex.kz pages. Yandex ID pages are
reported as login_required without being read, and other pages are not read.
The agent stops at the assembled cart; checkout is a separate owner-approved
step with no tool here. A generic click can still trigger a transaction if a
site control is misleading, so this is a workflow boundary, not a guarantee.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
import hmac
import json
import os
from pathlib import Path
import secrets
import select
import socket
import time
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

ROOT_URL = "https://eda.yandex.kz/"
SERVICE_HOST = "eda.yandex.kz"
# Yandex ID lives on these labels of Yandex domains (passport.yandex.kz, id.yandex.ru, ...).
YANDEX_DOMAINS = ("yandex.kz", "yandex.ru", "yandex.com", "yandex.by", "yandex.uz", "ya.ru")
IDENTITY_LABELS = frozenset({"passport", "id", "sso", "oauth", "social", "auth"})
ENDPOINT = ("session", "endpoint.json")
PROFILE_DIR = "chrome-profile"
PROFILE_MARKER = ".ouroboros-yandex-eats-profile"

LIVE_OPS = ("open", "observe", "act", "search", "capture", "close")
SCOPES = ("viewport", "page")
SCROLLS = ("down", "up")
ACTIONS = ("click", "fill", "press")
# Declared purpose of a click. Deliberately no checkout, payment,
# order, sign-in or confirmation value.
INTENTS = ("open", "choose", "add_item", "change_quantity", "remove_item", "dismiss", "expand", "focus")
KEYS = ("Enter", "Escape", "ArrowDown", "ArrowUp")
TIMEOUTS = {"open": 90, "observe": 40, "close": 20, "act": 45, "search": 45, "capture": 30}

_ENTRY_KEYS = frozenset({"Enter", "ArrowDown", "ArrowUp"})
_SENSITIVE_HINT = "credentials, one-time codes and payment data are entered only by the owner"
_MAX_TEXT = 200
_MAX_REQUEST = 64 * 1024
_MAX_REPLY = 4 * 1024 * 1024
_AUDIT_LIMIT = 256 * 1024


# --------------------------------------------------------------------------- pages

def page_kind(url: str) -> tuple[str, str]:
    """('service' | 'login' | 'other', host), decided from the URL alone."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return "other", ""
    if parts.scheme == "https" and host == SERVICE_HOST:
        return "service", host
    for domain in YANDEX_DOMAINS:
        if host.endswith("." + domain) and IDENTITY_LABELS & set(host[:-len(domain) - 1].split(".")):
            return "login", host
    return "other", host


def bare_url(url: str) -> str:
    """scheme://host/path: never a query, fragment, port or credentials."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return ""
    if not parts.scheme or not host:
        return f"{parts.scheme}:" if parts.scheme else ""
    return f"{parts.scheme}://{host}{parts.path[:200]}"


def _page(url: str) -> dict[str, str]:
    kind, host = page_kind(url)
    return {"kind": "yandex_id" if kind == "login" else kind, "host": host}


# --------------------------------------------------------------------------- arguments

def _text(value: Any, field: str, *, required: bool = False, limit: int = _MAX_TEXT) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = " ".join(value.split())
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return value


def _only(args: dict[str, Any], allowed: set[str]) -> None:
    extra = sorted(set(args) - allowed)
    if extra:
        raise ValueError(f"unsupported argument(s): {', '.join(extra)}")


def validate_open(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"home"})
    home = args.get("home", False)
    if not isinstance(home, bool):
        raise ValueError("home must be a boolean")
    return {"home": home}


def validate_observe(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, {"query", "scope", "scroll", "wait_ms"})
    scope = args.get("scope") or "viewport"
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    scroll = args.get("scroll") or ""
    if scroll and scroll not in SCROLLS:
        raise ValueError(f"scroll must be one of {', '.join(SCROLLS)}")
    wait_ms = args.get("wait_ms", 0)
    if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or not 0 <= wait_ms <= 10000:
        raise ValueError("wait_ms must be an integer from 0 to 10000")
    return {"query": _text(args.get("query"), "query"), "scope": scope, "scroll": scroll, "wait_ms": wait_ms}


def validate_close(args: dict[str, Any]) -> dict[str, Any]:
    _only(args, set())
    return {}


def validate_capture(args: dict[str, Any]) -> dict[str, str]:
    _only(args, {"observation_id", "element_id"})
    return {"observation_id": _text(args.get("observation_id"), "observation_id", required=True, limit=64),
            "element_id": _text(args.get("element_id"), "element_id", limit=12)}


def validate_act(args: dict[str, Any]) -> dict[str, Any]:
    """Validate an element action before contacting the companion."""
    _only(args, {"action", "observation_id", "element_id", "target_name", "target_role", "intent", "text", "key"})
    action = args.get("action")
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    spec = {
        "action": action,
        "observation_id": _text(args.get("observation_id"), "observation_id", limit=64),
        "element_id": _text(args.get("element_id"), "element_id", limit=12),
        "target_name": _text(args.get("target_name"), "target_name", limit=120),
        "target_role": _text(args.get("target_role"), "target_role", limit=40),
        "intent": args.get("intent") or "", "text": "", "key": "",
    }
    if spec["element_id"] and spec["target_name"]:
        raise ValueError("give element_id or target_name, not both")
    if not (spec["element_id"] or spec["target_name"]):
        raise ValueError(f"{action} needs element_id or target_name")
    if spec["target_role"] and not spec["target_name"]:
        raise ValueError("target_role narrows target_name and needs it")
    if not spec["observation_id"]:
        raise ValueError("an element target needs the observation_id it came from")
    if action == "click":
        if spec["intent"] not in INTENTS:
            raise ValueError(f"intent must be one of {', '.join(INTENTS)}; no dedicated checkout, "
                             "payment, sign-in or order operation is exposed")
    elif spec["intent"]:
        raise ValueError("intent applies to click")
    if action == "fill":
        spec["text"] = _text(args.get("text"), "text")
    elif args.get("text"):
        raise ValueError("text applies to fill")
    if action == "press":
        if args.get("key") not in KEYS:
            raise ValueError(f"key must be one of {', '.join(KEYS)}")
        spec["key"] = args["key"]
    elif args.get("key"):
        raise ValueError("key applies to press")
    return spec


def validate_search(args: dict[str, Any]) -> dict[str, Any]:
    """Validate a structural or explicitly selected search field."""
    _only(args, {"query", "observation_id", "element_id", "press_enter", "trigger_name"})
    press_enter = args.get("press_enter", True)
    if not isinstance(press_enter, bool):
        raise ValueError("press_enter must be a boolean")
    spec = {"query": _text(args.get("query"), "query", required=True),
            "observation_id": _text(args.get("observation_id"), "observation_id", limit=64),
            "element_id": _text(args.get("element_id"), "element_id", limit=12),
            "trigger_name": _text(args.get("trigger_name"), "trigger_name", limit=120),
            "press_enter": press_enter}
    if spec["trigger_name"] and press_enter:
        raise ValueError("trigger_name selects the observed search button; set press_enter=false")
    if spec["element_id"] and not spec["observation_id"]:
        raise ValueError("element_id needs the observation_id it came from")
    return spec


# --------------------------------------------------------------------------- profile

class ProfileRefused(Exception):
    """The Chrome profile directory is not one this skill created."""


def prepare_profile(state_dir: Path) -> Path:
    """The skill's own Chrome profile, created and marked on first open.

    The location stays <state_dir>/chrome-profile until the parent decides on a
    different profile binding. A symlink, or a non-empty directory without the
    marker (for example a copied personal profile), is refused rather than used.
    """
    state = Path(state_dir).resolve()
    state.mkdir(parents=True, exist_ok=True)
    path = state / PROFILE_DIR
    marker = path / PROFILE_MARKER
    if path.is_symlink():
        raise ProfileRefused(f"{PROFILE_DIR} is a symlink; the skill only uses a profile directory it "
                             "created in its own state directory")
    if path.exists() and not path.is_dir():
        raise ProfileRefused(f"{PROFILE_DIR} exists but is not a directory")
    if path.exists() and marker.is_file() and not marker.is_symlink():
        return path
    if path.exists() and any(path.iterdir()):
        raise ProfileRefused(f"{PROFILE_DIR} exists without this skill's marker, so it may be a copied "
                             "personal profile and is not used; profile binding is decided separately")
    path.mkdir(mode=0o700, exist_ok=True)
    marker.write_text(json.dumps({"created_by": "yandex-travel eats companion",
                                  "created": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- element policy

def element_refusal(action: str, element: dict[str, Any], key: str = "") -> tuple[str, str] | None:
    """Refuse an effect on one live element from structure alone (no wording)."""
    if element.get("role") in {"region", "article", "complementary"}:
        return ("capture_only_region", "this region can be captured but cannot be activated; "
                "choose its observed interactive child for an action")
    if element.get("disabled"):
        return ("element_disabled", "the element is disabled or unavailable; choose something else "
                "from the observation or ask the owner")
    if element.get("covered"):
        return ("element_covered", "another layer (for example a dialog) covers the element; "
                "dismiss it or pick from the fresh observation")
    href = element.get("href") or ""
    if action == "click" and href and page_kind(href)[0] != "service":
        return ("link_leaves_service", f"the link leads outside {ROOT_URL}")
    if action == "click" and element.get("form_sensitive"):
        return ("sensitive_form", f"the control belongs to a form with sensitive fields; {_SENSITIVE_HINT}")
    if action in {"fill", "press"} and element.get("sensitive"):
        return ("sensitive_field", _SENSITIVE_HINT)
    if action == "fill" and not element.get("entry"):
        return ("not_text_entry", "fill needs a text field")
    if action == "press" and key in _ENTRY_KEYS and not element.get("entry"):
        return ("not_text_entry", f"{key} is only pressed inside a text field")
    if action == "press" and key == "Enter" and not element.get("search"):
        return ("enter_outside_search", "Enter is only sent to a structural search field because it "
                "can submit forms; click the intended control instead")
    return None


# --------------------------------------------------------------------------- session

class Refusal(Exception):
    """Refused before any input event reached the page."""

    def __init__(self, code: str, reason: str, *, refresh: bool = False,
                 candidates: list[dict[str, Any]] | None = None,
                 observation: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.code, self.reason, self.refresh = code, reason, refresh
        self.candidates, self.observation = candidates, observation


class NotOpen(Exception):
    pass


class SessionLost(Exception):
    pass


class LaunchFailed(Exception):
    pass


def _fingerprint(record: dict[str, Any]) -> dict[str, str]:
    return {key: record.get(key, "") for key in ("role", "name", "in", "content_signature")}


def _public(record: dict[str, Any]) -> dict[str, Any]:
    """Whitelisted element facts; field values never leave the companion."""
    item: dict[str, Any] = {"id": f"e{record['i'] + 1}", "role": record.get("role", ""),
                            "name": record.get("name", "")}
    for key in ("in", "area"):
        if record.get(key):
            item[key] = record[key]
    flags = [flag for flag in ("disabled", "covered", "checked", "expanded", "selected", "focused", "search")
             if record.get(flag)]
    if record.get("sensitive"):
        flags.append("owner_only")
    if flags:
        item["state"] = flags
    href = record.get("href") or ""
    if href:
        kind, host = page_kind(href)
        item["href"] = bare_url(href) if kind == "service" else f"external:{host or '?'}"
    return item


class Session:
    """One visible browser session; every request is served in order."""

    def __init__(self, driver_factory: Callable[[], Any], *, state_dir: Path) -> None:
        self._factory = driver_factory
        self._state_dir = Path(state_dir)
        self._driver: Any = None
        self._latest: dict[str, Any] | None = None
        self._key = "__ouroboros_eats_" + secrets.token_hex(6)
        self._seq = 0
        self._effect = "none"
        self.nonce = secrets.token_hex(3)
        self.launches = 0

    # -- request entry
    def handle(self, op: str, args: Any) -> dict[str, Any]:
        self._effect = "none"
        handlers = {"open": self._open, "observe": self._observe_op, "close": self._close,
                    "act": self._act, "search": self._search, "capture": self._capture}
        try:
            handler = handlers.get(op)
            if handler is None:
                raise Refusal("unknown_operation", f"operation {op!r} is not part of this skill")
            result = handler(dict(args) if isinstance(args, dict) else {})
        except Refusal as exc:
            result = self._refused(exc)
        except ValueError as exc:
            result = {"status": "invalid", "reason": str(exc)}
        except NotOpen:
            result = {"status": "not_open", "reason": "no browser session; call yandex_eats_open"}
        except ProfileRefused as exc:
            result = {"status": "profile_refused", "reason": str(exc)}
        except LaunchFailed as exc:
            result = {"status": "launch_failed", "reason": f"Chrome could not start ({exc}); the owner checks "
                      "that Google Chrome is installed and the skill's Playwright dependency is ready"}
        except SessionLost as exc:
            self._drop()
            result = {"status": "session_lost", "reason": f"{exc}; call yandex_eats_open to start again"}
        except Exception as exc:  # driver/browser failure: report, never guess; raw text may hold URLs
            if self._driver is not None and not self._driver.alive():
                self._drop()
                result = {"status": "session_lost", "reason": "the browser closed or crashed; "
                          "call yandex_eats_open to start again"}
            else:
                result = {"status": "error", "reason": f"{type(exc).__name__} from the browser; "
                          "observe before doing anything else"}
        result.setdefault("effect", self._effect)
        self._audit(op, args, result)
        return result

    def _refused(self, exc: Refusal) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "refused", "code": exc.code, "reason": exc.reason}
        if exc.candidates:
            result["candidates"] = exc.candidates
        if exc.observation is not None:
            result["observation"] = exc.observation
        elif exc.refresh and self._driver is not None:
            with suppress(Exception):
                self._attach_view(result, self._live())
        elif self._latest is not None:
            result["observation_id"] = self._latest["id"]
        return result

    # -- live operations
    def _open(self, args: dict[str, Any]) -> dict[str, Any]:
        options = validate_open(args)
        if self._driver is not None and not self._driver.alive():
            self._drop()
        launched = self._driver is None
        if launched:
            driver = self._factory()  # may raise ProfileRefused before anything starts
            try:
                driver.launch()
            except Exception as exc:
                with suppress(Exception):
                    driver.close()
                raise LaunchFailed(type(exc).__name__) from None
            self._driver = driver
            self.launches += 1
        if launched or options["home"]:
            self._effect = "unknown"
            self._driver.goto(ROOT_URL)  # the only address this skill ever loads
            self._effect = "navigated"
        self._settle(self._driver, 4000)
        result = self._view(self._driver)
        result["launched"] = launched
        return result

    def _observe_op(self, args: dict[str, Any]) -> dict[str, Any]:
        options = validate_observe(args)
        driver = self._live()
        if options["scroll"] and page_kind(driver.url())[0] == "service":
            driver.scroll(options["scroll"])  # scripted viewport scroll: no pointer, key or focus input
            self._effect = "scrolled"
        if options["wait_ms"] or options["scroll"]:
            self._settle(driver, options["wait_ms"] or 1500)
        return self._view(driver, query=options["query"], scope=options["scope"])

    def _close(self, args: dict[str, Any]) -> dict[str, Any]:
        validate_close(args)
        was_open = self._driver is not None
        self._drop()
        return {"status": "closed", "was_open": was_open}

    def _capture(self, args: dict[str, Any]) -> dict[str, Any]:
        """Capture only an agent-selected region; image remains private until reviewed."""
        spec = validate_capture(args)
        driver = self._live()
        if page_kind(driver.url())[0] != "service":
            raise Refusal("outside_service", "capture is available only on the Eats page")
        if self._latest is None or spec["observation_id"] != self._latest["id"] or driver.epoch() != self._latest["epoch"]:
            raise Refusal("stale_observation", "observe this page again before capture", refresh=True)
        record = None
        if spec["element_id"]:
            record = self._resolve(driver, spec["observation_id"], spec["element_id"], "", "")
            if record.get("role") not in {"region", "article", "complementary"}:
                raise Refusal("capture_only_region", "capture a non-actionable observed region or the viewport")
            if record.get("content_signature") == "oversized":
                raise Refusal("region_too_large", "region text exceeds the capture freshness bound; "
                              "capture the viewport or choose a smaller region")
            expected = _fingerprint(record)
            live = driver.inspect(self._key, self._latest["id"], record["i"], expected)
            if live.get("missing") or _fingerprint(live) != expected:
                raise Refusal("stale_element", "the selected region changed; observe again", refresh=True)
        folder = self._state_dir / "captures"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / (secrets.token_hex(12) + ".png")
        try:
            if record is None:
                driver.capture_viewport(path)
            else:
                driver.capture_element(self._key, self._latest["id"], record["i"], path)
            if page_kind(driver.url())[0] != "service" or driver.epoch() != self._latest["epoch"]:
                raise Refusal("outside_service", "the page changed during capture; observe again")
            if record is not None:
                after = driver.inspect(self._key, self._latest["id"], record["i"], expected)
                if after.get("missing") or _fingerprint(after) != expected:
                    raise Refusal("stale_element", "the selected region changed during capture; observe again",
                                  refresh=True)
        except Exception:
            with suppress(OSError):
                path.unlink()
            raise
        return {"status": "captured_private", "path": str(path), "effect": "none",
                "note": "Send this cart image to the owner with send_photo; capture itself is not a delivery receipt."}

    # -- page-scoped actions
    def _require_service(self, driver: Any) -> None:
        kind, host = page_kind(driver.url())
        if kind == "login":
            raise Refusal("login_required", "a Yandex ID page is open; the owner signs in")
        if kind != "service":
            raise Refusal("outside_service", f"the page is on {host or 'an unknown host'}, not {ROOT_URL}")

    def _act(self, args: dict[str, Any]) -> dict[str, Any]:
        spec = validate_act(args)
        driver = self._live()
        self._require_service(driver)
        record = self._resolve(driver, spec["observation_id"], spec["element_id"],
                               spec["target_name"], spec["target_role"])
        live = self._verify(driver, record, spec["action"], spec["key"])
        value = spec["key"] if spec["action"] == "press" else spec["text"]
        epoch = driver.epoch()
        self._perform(driver, record, spec["action"], value)
        self._settle(driver, 4000)
        echo = {"action": spec["action"], **({"intent": spec["intent"]} if spec["intent"] else {}),
                **({"key": spec["key"]} if spec["key"] else {})}
        return self._done(driver, echo, target=live, prior_epoch=epoch)

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        spec = validate_search(args)
        driver = self._live()
        self._require_service(driver)
        if spec["element_id"]:
            record = self._resolve(driver, spec["observation_id"], spec["element_id"], "", "")
        else:
            observation = self._observe(driver)
            if observation is None:
                raise Refusal("outside_service", "the page left the service while it was read", refresh=True)
            fields = [item for item in self._latest["elements"].values()
                      if item.get("search") and not item.get("disabled")]
            if len(fields) != 1:
                code = "ambiguous" if fields else "not_found"
                reason = ("several search fields are visible; pass the element_id of the intended one"
                          if fields else "no structural search field is visible; pass the element_id "
                          "of the text field")
                raise Refusal(code, reason, candidates=[_public(item) for item in fields[:10]],
                              observation=observation)
            record = fields[0]
        live = self._verify(driver, record, "fill", "")
        epoch = driver.epoch()
        self._perform(driver, record, "fill", spec["query"])
        note = ""
        if spec["trigger_name"]:
            self._settle(driver, 800)
            observation = self._observe(driver)
            if observation is None:
                raise Refusal("outside_service", "the page left the service after filling search")
            submit = self._resolve(driver, self._latest["id"], "", spec["trigger_name"], "button")
            self._verify(driver, submit, "click", "")
            self._perform(driver, submit, "click", "", started=True)
        elif spec["press_enter"] and live.get("search"):
            self._perform(driver, record, "press", "Enter", started=True)
        elif spec["press_enter"]:
            note = ("Enter skipped: the field is not structurally a search field; "
                    "pick a suggestion or the site's search control from the observation")
        self._settle(driver, 5000)
        result = self._done(driver, {"action": "search"}, target=live, prior_epoch=epoch)
        if note:
            result["note"] = note
        return result

    # -- helpers
    def _live(self) -> Any:
        if self._driver is None:
            raise NotOpen()
        if not self._driver.alive():
            raise SessionLost("the browser window or tab was closed")
        return self._driver

    def _drop(self) -> None:
        driver, self._driver, self._latest = self._driver, None, None
        if driver is not None:
            with suppress(Exception):
                driver.close()

    def close(self) -> None:
        self._drop()

    def pump(self, seconds: float) -> bool:
        """Let the driver process browser events while the server is idle."""
        if self._driver is None:
            return False
        with suppress(Exception):
            return bool(self._driver.pump(int(seconds * 1000)))
        return False

    def _settle(self, driver: Any, max_ms: int) -> None:
        if page_kind(driver.url())[0] == "service":
            driver.settle(max_ms)
        else:  # nothing is evaluated in pages the skill does not read
            driver.pump(min(max_ms, 2000))

    def _view(self, driver: Any, *, query: str = "", scope: str = "viewport") -> dict[str, Any]:
        """Status for the current page: an observation on the service, a bare page kind elsewhere."""
        observation = self._observe(driver, query=query, scope=scope)
        if observation is not None:
            return {"status": "ok", "observation": observation}
        page = _page(driver.url())
        result: dict[str, Any]
        if page["kind"] == "yandex_id":
            result = {"status": "login_required", "page": page,
                      "reason": "Yandex ID sign-in is open in the visible window. The owner signs in there; "
                      "this skill does not read, fill or capture that page. Observe again afterwards."}
        elif page["kind"] == "service":
            result = {"status": "page_changed", "page": page,
                      "reason": "the page navigated while it was read; observe again"}
        else:
            result = {"status": "outside_service", "page": page,
                      "reason": f"the window shows a page outside {ROOT_URL}, which this skill does not read; "
                      "the owner navigates back, or call yandex_eats_open with home=true"}
        tabs = self._tabs(driver)
        if tabs:
            result["other_tabs"] = tabs
        return result

    def _attach_view(self, result: dict[str, Any], driver: Any) -> None:
        view = self._view(driver)
        if view["status"] == "ok":
            result["observation"] = view["observation"]
        else:
            result["page_status"], result["page"] = view["status"], view["page"]

    def _tabs(self, driver: Any) -> list[dict[str, str]]:
        return [_page(url) for url in list(driver.other_tabs())[:5]]

    def _observe(self, driver: Any, *, query: str = "", scope: str = "viewport") -> dict[str, Any] | None:
        self._latest = None  # ids of any earlier observation stop being actionable here
        if page_kind(driver.url())[0] != "service":
            return None
        for _ in range(2):
            epoch = driver.epoch()
            self._seq += 1
            observation_id = f"{self.nonce}.{epoch}.{self._seq}"
            raw = driver.observe(self._key, observation_id,
                                 {"query": query, "scope": scope, "max_elements": 100, "max_text": 3000})
            if driver.epoch() == epoch:
                break
        url = str(raw.get("url") or "")
        if raw.get("blocked") or page_kind(url)[0] != "service" or page_kind(driver.url())[0] != "service":
            return None  # the document left the service while being read: discard it
        records = list(raw.get("elements") or [])
        self._latest = {"id": observation_id, "epoch": epoch,
                        "elements": {f"e{item['i'] + 1}": item for item in records}}
        observation: dict[str, Any] = {
            "observation_id": observation_id, "url": bare_url(url), "title": str(raw.get("title") or "")[:160],
            "mode": "agent_can_act"}
        if raw.get("modal"):
            observation["modal"] = raw["modal"]
        observation["elements"] = [_public(item) for item in records]
        hidden = int(raw.get("total") or 0) - len(records)
        if hidden > 0:
            observation["elements_not_shown"] = hidden
        observation["text"] = raw.get("text", "")
        if raw.get("text_truncated"):
            observation["text_truncated"] = True
        frames = [_page(origin) for origin in raw.get("frames") or []]
        if frames:
            observation["foreign_frames"] = [f"{frame['kind']}:{frame['host']}" for frame in frames]
            if any(frame["kind"] == "yandex_id" for frame in frames):
                observation["login_frame"] = True  # a Yandex ID frame is shown; its content is unreadable
        if raw.get("scroll"):
            observation["scroll"] = raw["scroll"]
        tabs = self._tabs(driver)
        if tabs:
            observation["other_tabs"] = tabs
        return observation

    def _resolve(self, driver: Any, observation_id: str, element_id: str,
                 target_name: str, target_role: str) -> dict[str, Any]:
        latest = self._latest
        if latest is None or observation_id != latest["id"]:
            raise Refusal("stale_observation", "that observation is not the latest one for this page; "
                          "use the fresh observation returned here", refresh=True)
        if driver.epoch() != latest["epoch"]:
            raise Refusal("stale_observation", "the page navigated after that observation", refresh=True)
        if element_id:
            record = latest["elements"].get(element_id)
            if record is None:
                raise Refusal("unknown_element", f"{element_id} is not in observation {observation_id}")
            return record
        wanted = target_name.casefold()
        matches = [item for item in latest["elements"].values()
                   if " ".join(str(item.get("name", "")).split()).casefold() == wanted
                   and (not target_role or item.get("role") == target_role)]
        if not matches:
            raise Refusal("not_found", "no element in the latest observation has exactly that name; "
                          "observe with a query or choose an element_id")
        if len(matches) > 1:
            raise Refusal("ambiguous", f"{len(matches)} elements match; choose one element_id",
                          candidates=[_public(item) for item in matches[:10]])
        return matches[0]

    def _verify(self, driver: Any, record: dict[str, Any], action: str, key: str) -> dict[str, Any]:
        expected = _fingerprint(record)
        try:
            live = driver.inspect(self._key, self._latest["id"], record["i"], expected)
        except Exception as exc:
            if not driver.alive():
                raise SessionLost("the browser closed while verifying the element") from exc
            raise Refusal("stale_element", "the page changed while the element was verified",
                          refresh=True) from exc
        if live.get("blocked"):
            raise Refusal("outside_service", "the page left the service", refresh=True)
        if live.get("missing"):
            now = live.get("now")
            detail = f"; it now reads {now}" if now else ""
            raise Refusal("stale_element", f"element is {live['missing']} since the observation{detail}",
                          refresh=True)
        if _fingerprint(live) != expected:
            raise Refusal("stale_element", "element changed since the observation", refresh=True)
        refusal = element_refusal(action, live, key)
        if refusal is not None:
            raise Refusal(refusal[0], refusal[1], refresh=refusal[0] == "element_covered")
        return live

    def _perform(self, driver: Any, record: dict[str, Any], action: str, value: str,
                 *, started: bool = False) -> None:
        self._effect = "unknown"
        try:
            driver.perform(self._key, self._latest["id"], record["i"], action, value)
        except LookupError as exc:  # handle vanished before any input was sent
            if not started:
                self._effect = "none"
                raise Refusal("stale_element", "element vanished before the action", refresh=True) from exc
            raise
        self._effect = "performed"

    def _done(self, driver: Any, echo: dict[str, Any], *, target: dict[str, Any],
              prior_epoch: int) -> dict[str, Any]:
        # A click may open a new tab rather than navigating its original page.
        # Adopt it before deciding which document this action returned.
        if not driver.alive():
            raise SessionLost("the browser closed after the action")
        self._effect = "performed"
        result: dict[str, Any] = {"status": "done", "effect": "performed",
                                  "action": {**echo, "element": _fingerprint(target)}}
        if driver.epoch() != prior_epoch:
            result["navigated"] = True
        self._attach_view(result, driver)
        return result

    def _audit(self, op: str, args: Any, result: dict[str, Any]) -> None:
        """Owner-readable action log: closed-set values and hosts only, never typed text or URL queries."""
        if op == "observe":
            return
        entry: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "op": op if op in LIVE_OPS else "unknown",
                                 "status": result.get("status"), "code": result.get("code", ""),
                                 "effect": result.get("effect")}
        if isinstance(args, dict) and op == "act":
            if args.get("action") in ACTIONS:
                entry["action"] = args["action"]
            if args.get("intent") in INTENTS:
                entry["intent"] = args["intent"]
        page = result.get("page") or _page(str((result.get("observation") or {}).get("url") or ""))
        if page.get("host"):
            entry["page"] = f"{page['kind']}:{page['host']}"
        with suppress(OSError):
            path = self._state_dir.joinpath("session", "actions.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > _AUDIT_LIMIT:
                os.replace(path, path.with_name("actions.previous.jsonl"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- transport

def _read_line(conn: socket.socket, limit: int) -> bytes:
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise ValueError("message too large")
    return data.split(b"\n", 1)[0]


def _publish(state_dir: Path, port: int, token: str) -> Path:
    path = Path(state_dir).joinpath(*ENDPOINT)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"port": port, "token": token, "pid": os.getpid()}, stream)
    os.replace(temporary, path)
    return path


def _retract(path: Path, token: str) -> None:
    with suppress(OSError, ValueError):
        if json.loads(path.read_text(encoding="utf-8")).get("token") == token:
            path.unlink()


def _serve_one(conn: socket.socket, session: Session, token: str) -> None:
    conn.settimeout(10)
    try:
        request = json.loads(_read_line(conn, _MAX_REQUEST) or b"null")
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
    except (OSError, ValueError):
        reply: dict[str, Any] = {"status": "invalid", "effect": "none", "reason": "malformed request"}
    else:
        offered = str(request.get("token") or "").encode()
        if not hmac.compare_digest(offered, token.encode()):
            reply = {"status": "refused", "code": "unauthorized", "effect": "none",
                     "reason": "request token does not match this companion"}
        else:
            reply = session.handle(str(request.get("op") or ""), request.get("args"))
    with suppress(OSError):
        conn.sendall(json.dumps(reply, ensure_ascii=False).encode() + b"\n")


def serve(session: Session, state_dir: Path, *, should_stop: Callable[[], bool]) -> None:
    """Serve tool requests one at a time until asked to stop, then close Chrome."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    token = secrets.token_hex(24)
    path: Path | None = None
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        path = _publish(Path(state_dir), listener.getsockname()[1], token)
        while not should_stop():
            pumped = session.pump(0.2)
            ready, _, _ = select.select([listener], [], [], 0.05 if pumped else 0.5)
            if ready:
                conn, _ = listener.accept()
                with conn:
                    _serve_one(conn, session, token)
    finally:
        listener.close()
        if path is not None:
            _retract(path, token)
        session.close()


def call(state_dir: Path, op: str, args: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
    """One tool request to the companion; never raises."""
    timeout = timeout or TIMEOUTS.get(op, 30)
    unavailable = {"status": "companion_unavailable", "effect": "none",
                   "reason": "the browser companion is not running; it starts when the skill is enabled "
                             "(allow a few seconds), otherwise check the skill's companion health"}
    try:
        endpoint = json.loads(Path(state_dir).joinpath(*ENDPOINT).read_text(encoding="utf-8"))
        port, token = int(endpoint["port"]), str(endpoint["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return unavailable
    request = json.dumps({"token": token, "op": op, "args": args}, ensure_ascii=False).encode() + b"\n"
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=3)
    except OSError:
        return unavailable
    try:
        with conn:
            conn.settimeout(timeout)
            conn.sendall(request)
            data = _read_line(conn, _MAX_REPLY)
    except (OSError, ValueError) as exc:
        waited = "no answer within {}s".format(timeout) if isinstance(exc, TimeoutError) else "connection lost"
        return {"status": "error", "effect": "unknown",
                "reason": f"{waited}; the browser may still be busy, observe before anything else"}
    try:
        reply = json.loads(data)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        return {"status": "error", "effect": "unknown", "reason": "malformed companion reply"}
    return reply


@contextmanager
def hold_lock(path: Path, *, wait_sec: float = 10.0) -> Iterator[None]:
    """One companion per state dir (and so per Chrome profile)."""
    try:
        import fcntl
    except ImportError:  # non-POSIX host: the supervisor already runs one companion
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + wait_sec
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("another Yandex Eats companion holds this profile") from None
                time.sleep(0.25)
        yield
