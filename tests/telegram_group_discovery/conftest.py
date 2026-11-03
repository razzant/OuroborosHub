"""Real-host/browser qualification for Telegram group discovery (Hub #238).

What it runs: the real current Ouroboros host (a byte-faithful copy of the core candidate), the Telegram bot
plugin payload under test, the plugin's own poll/capture over a controlled loopback Bot API fed raw synthetic
updates, and the owner's browser at Dashboard > Consciousness > Connect a place > Telegram. No installed state,
credential or real Telegram call (``host.py``).

Environment (all read here; nothing is skipped or defaulted past a missing prerequisite, it stops the run):

* ``TG_DISCOVERY_CORE``      core checkout the host runs (read-only source; the copy runs). Required.
* ``TG_DISCOVERY_SKILL``     the plugin payload under test; default ``skills/telegram-bot`` of this checkout.
* ``TG_DISCOVERY_EVIDENCE_DIR``  where JSON facts and screenshots go; default a run directory under /tmp.
* ``TG_DISCOVERY_LEGACY_REF``    Git ref of the published skill the update case starts from (default: Hub 089ea45).
* ``PLAYWRIGHT_BROWSERS_PATH``   must hold webkit (the owner's engine); chromium is optional (``TG_DISCOVERY_ENGINES``).
  Each engine runs against its own fresh host. The plugin is admitted by synthetic fixture records (a ``pass``
  review state and grants keyed to its content hash); no review is performed.

Invocation (dependency-only venv; the launcher scrubs inherited environment, so pass these inside it)::

    python -I -S <core>/scripts/safe_test.py -- env TG_DISCOVERY_CORE=<core> PLAYWRIGHT_BROWSERS_PATH=<cache> \\
        <venv>/bin/python -m pytest tests/telegram_group_discovery -p no:cacheprovider --junitxml=<out>/junit.xml
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
HUB = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from host import GroupDiscoveryHost, legacy_skill  # noqa: E402

CORE = os.environ.get("TG_DISCOVERY_CORE", "")
SKILL = Path(os.environ.get("TG_DISCOVERY_SKILL") or HUB / "skills" / "telegram-bot")
ENGINES = [e for e in os.environ.get("TG_DISCOVERY_ENGINES", "webkit").split(",") if e]
# The owner's desktop and a narrow phone, light and dark: every screenshot is taken in all four.
VIEWS = [(1100, 750, "light"), (1100, 750, "dark"), (390, 844, "light"), (390, 844, "dark")]


def pytest_collection_finish(session):
    """What the selected tests need stops the run before the first test and says what to set; nothing is skipped."""
    if session.config.option.collectonly:
        return
    missing = []
    if not CORE or not (Path(CORE) / "tests" / "candidate_checkout.py").is_file():
        missing.append("set TG_DISCOVERY_CORE to the core candidate checkout (it has tests/candidate_checkout.py)")
    if not (SKILL / "plugin.py").is_file():
        missing.append(f"TG_DISCOVERY_SKILL={SKILL} is not a plugin payload (no plugin.py)")
    try:
        import playwright.sync_api  # noqa: F401
        import starlette  # noqa: F401
    except ImportError as exc:
        missing.append(f"install the dependency-only venv (core pinned requirements + playwright): {exc}")
    if "webkit" not in ENGINES:
        missing.append("TG_DISCOVERY_ENGINES must include webkit: the owner's engine is never downgraded")
    if missing:
        raise pytest.UsageError("tests/telegram_group_discovery: " + "; ".join(missing))


@pytest.fixture(scope="session")
def evidence_dir(tmp_path_factory) -> Path:
    path = Path(os.environ.get("TG_DISCOVERY_EVIDENCE_DIR") or tempfile.mkdtemp(prefix="tgd-evidence-", dir="/tmp"))
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def record(evidence_dir):
    def write(name: str, facts) -> None:
        (evidence_dir / f"{name}.json").write_text(json.dumps(facts, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return write


@pytest.fixture(scope="session")
def playwright_api():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module", params=ENGINES)
def engine(request) -> str:
    """Module-scoped, so every engine gets its own browser and its own fresh host (never one another's leftovers)."""
    return request.param


@pytest.fixture(scope="module")
def browser(engine, playwright_api, record):
    launched = getattr(playwright_api, engine).launch(headless=True)  # an absent engine is an error, not a skip
    record(f"browser-{engine}", {"engine": engine, "version": launched.version,
                                        "browsers_path": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")})
    try:
        yield launched
    finally:
        launched.close()


def retire(started: GroupDiscoveryHost, evidence_dir: Path, name: str) -> None:
    """Stop the host with proof, keep its logs as evidence, then release the run directory (only after a proven stop)."""
    try:
        started.stop()
    finally:
        logs = evidence_dir / f"logs-{name}"
        logs.mkdir(exist_ok=True)
        for kept in ("server.log", "install.log", "owner.json"):
            if (started.root / kept).is_file():
                shutil.copy(started.root / kept, logs / kept)
        (logs / "bot-calls.json").write_text(json.dumps(
            [{"method": c["method"], "token_ok": c["token_ok"], "keys": sorted(c["body"])} for c in started.bot.calls], indent=1))
    if not any(started.root.rglob("OUROBOROS_RETAINED.txt")):
        shutil.rmtree(started.root, ignore_errors=True)


def start_host(skill: Path, **options) -> GroupDiscoveryHost:
    # A short path: the host binds unix-adjacent paths; /tmp keeps them under the socket limit.
    root = Path(tempfile.mkdtemp(prefix="tgd", dir="/tmp"))
    return GroupDiscoveryHost(Path(CORE), root, skill, **options).start()


@pytest.fixture(scope="module")
def host(engine, record, request, evidence_dir):
    started = start_host(SKILL)
    started.engine = engine
    record(f"host-{request.module.__name__}-{engine}", {**started.provenance, "skill": str(SKILL), "run_root": str(started.root)})
    try:
        yield started
    finally:
        retire(started, evidence_dir, f"{request.module.__name__}-{engine}")


@pytest.fixture(scope="module")
def unconfirmed_host(engine, record, request, evidence_dir):
    """A host whose provider cannot yet say who the bot is (``getMe`` fails): the account is unknown."""
    started = start_host(SKILL, confirmed=False)
    started.engine = engine
    record(f"host-{request.module.__name__}-{engine}", {**started.provenance, "skill": str(SKILL), "run_root": str(started.root)})
    try:
        yield started
    finally:
        retire(started, evidence_dir, f"{request.module.__name__}-{engine}")


@pytest.fixture(scope="module")
def legacy_host(engine, record, request, evidence_dir):
    """The plugin as the Hub published it before this work, for the update case."""
    scratch = Path(tempfile.mkdtemp(prefix="tgd-legacy", dir="/tmp"))
    old = legacy_skill(scratch)
    started = start_host(old)
    started.engine = engine
    record(f"host-{request.module.__name__}-{engine}-legacy", {**started.provenance, "skill": str(old), "run_root": str(started.root)})
    try:
        yield started
    finally:
        try:
            retire(started, evidence_dir, f"{request.module.__name__}-{engine}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


class Shots:
    """One state of the page in all four views; the page is returned to the owner's desktop light view."""

    def __init__(self, page, directory: Path, engine: str):
        self.page, self.dir, self.engine = page, directory, engine

    def __call__(self, name: str) -> list:
        written = []
        try:
            for width, height, scheme in VIEWS:
                self.page.set_viewport_size({"width": width, "height": height})
                self.page.emulate_media(color_scheme=scheme)
                self.page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
                path = self.dir / f"{self.engine}-{width}x{height}-{scheme}-{name}.png"
                self.page.screenshot(path=str(path))
                written.append(path.name)
        finally:
            self.page.set_viewport_size({"width": 1100, "height": 750})
            self.page.emulate_media(color_scheme="light")
        return written


@pytest.fixture
def page(browser, evidence_dir, request):
    context = browser.new_context(viewport={"width": 1100, "height": 750}, color_scheme="light")
    opened = context.new_page()
    errors, failed = [], []
    opened.on("pageerror", lambda error: errors.append(str(error)[:500]))
    opened.on("console", lambda message: errors.append("console.error: " + message.text[:300]) if message.type == "error" else None)
    opened.on("response", lambda response: failed.append([response.status, response.url]) if response.status >= 400 else None)
    opened.shots = Shots(opened, evidence_dir, browser.browser_type.name)
    opened.page_errors, opened.failed_responses = errors, failed
    try:
        yield opened
    finally:
        (evidence_dir / f"console-{request.node.name}.json").write_text(
            json.dumps({"errors": errors, "responses_ge_400": failed}, indent=2), encoding="utf-8")
        context.close()
