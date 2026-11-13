"""Tests import the payload as the companion does (``phone`` package) and the plugin as the host does
(``telephony.plugin``). Protocol pins import an Ouroboros core checkout read-only, the one TELEPHONY_CORE_REPO
names and no other; without it those tests are skipped. TELEPHONY_REQUIRE_CORE=1 (the Hub's CI sets it) turns
each such skip into a failure: no core, a core without the live call doors (or the starlette they run on), and
no uvicorn or websockets for the bridge tests then fail the run."""
import asyncio
import importlib
import os
import pathlib
import sys
import time
from typing import Optional

import pytest

# The asserts inside these helper modules explain themselves in a failure, as they do in a test module.
pytest.register_assert_rewrite("sipfake", "hubfake")

SKILL = pathlib.Path(__file__).resolve().parent.parent
SKILLS = SKILL.parent
sys.path.insert(0, str(SKILL))
from phone import sip_ua  # noqa: E402  (after the path insert above)
REQUIRE_CORE = os.environ.get("TELEPHONY_REQUIRE_CORE") == "1"


def _core_repo() -> Optional[pathlib.Path]:
    value = os.environ.get("TELEPHONY_CORE_REPO", "").strip()
    return pathlib.Path(value).expanduser() if value else None


CORE_REPO = _core_repo()


def unavailable(reason: str):
    """A core pin that cannot run: a skip on a developer's machine, a failure where the core is required."""
    if REQUIRE_CORE:
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


def core_available() -> bool:
    found = CORE_REPO is not None and (CORE_REPO / "ouroboros" / "call_protocol.py").exists()
    if not found and REQUIRE_CORE:
        pytest.fail(f"TELEPHONY_REQUIRE_CORE is set but TELEPHONY_CORE_REPO ({CORE_REPO or 'not set'}) names no "
                    "Ouroboros core", pytrace=False)
    return found


@pytest.fixture(scope="session")
def core():
    if not core_available():
        unavailable("Ouroboros core checkout not available (set TELEPHONY_CORE_REPO)")
    if str(CORE_REPO) not in sys.path:
        sys.path.append(str(CORE_REPO))
    import ouroboros.call_protocol as call_protocol
    return call_protocol


@pytest.fixture(scope="session")
def host_doors(core):
    """The host's peer pump and hub (Ouroboros 7.10+); skipped on an older core checkout."""
    try:
        import ouroboros.call_peers  # noqa: F401
        import ouroboros.gateway.call_ws  # noqa: F401
    except ImportError as exc:
        unavailable(f"core checkout has no live call doors: {exc}")
    return core


@pytest.fixture(scope="session")
def host_server(host_doors):
    """What the bridge tests serve those doors with: uvicorn and its WebSocket protocol (websockets)."""
    for name in ("uvicorn", "websockets"):
        try:
            importlib.import_module(name)
        except ImportError as exc:
            unavailable(f"the bridge tests need {name}: {exc}")
    return host_doors


def import_plugin():
    if str(SKILLS) not in sys.path:
        sys.path.insert(0, str(SKILLS))
    return importlib.import_module("telephony.plugin")


# -- SIP timers shortened for the retransmission tests (test_sip_ua.py defines its own `fast` with longer limits) ----

@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(sip_ua, "T1", 0.05)
    monkeypatch.setattr(sip_ua, "T2", 0.2)
    monkeypatch.setattr(sip_ua, "TRANSACTION_TIMEOUT", 0.6)
    monkeypatch.setattr(sip_ua, "LATE_2XX_GUARD_SECONDS", 1.5)


# -- Asyncio helpers for the scenarios ---------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


async def until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())
