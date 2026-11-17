"""Shared offline harness for the Anime Studio payload tests.

Every test here runs without network: real sockets are refused, and provider
traffic goes through an in-memory httpx transport whose handler decides each
answer. Nothing in this suite can reach OpenRouter or spend money.
"""
from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import httpx
import pytest

# The installed payload is importable only as the package `anime_studio`: its
# PARENT directory goes on sys.path, exactly as scripts/anime_worker.py does.
_SKILL_DIR = Path(__file__).resolve().parent.parent
if str(_SKILL_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR.parent))

SKILL_DIR = _SKILL_DIR


def _refuse_network(*_args, **_kwargs):
    raise RuntimeError("network access is disabled in the anime_studio tests")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _refuse_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse_network)
    monkeypatch.setattr(socket, "create_connection", _refuse_network)


@pytest.fixture
def no_sleep(monkeypatch):
    """Make asyncio.sleep instant and record the requested delays."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def _instant(delay, *args, **kwargs):
        delays.append(float(delay))
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _instant)
    return delays


@pytest.fixture
def mock_http(monkeypatch):
    """Route every httpx.AsyncClient through a scripted in-memory handler.

    Usage: `requests = mock_http(handler)`; the handler receives an
    httpx.Request and returns an httpx.Response or raises an httpx error.
    The returned list records every request that reached the handler.
    """
    real_client = httpx.AsyncClient

    def _install(handler):
        seen: list[httpx.Request] = []

        def _recording(request):
            seen.append(request)
            return handler(request)

        class _ScriptedClient(real_client):
            def __init__(self, *args, **kwargs):
                kwargs.pop("transport", None)
                super().__init__(*args, transport=httpx.MockTransport(_recording), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _ScriptedClient)
        return seen

    return _install
