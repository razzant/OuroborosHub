from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


def pytest_configure(config):
    config.addinivalue_line("markers", "serial: process consumer requiring exclusive fixture execution")


def _decode_tool_json(result: Any) -> Any:
    """Decode one registered tool's result, asserting the host's `str` ABI.

    Every assertion in these tests runs through here so a handler that silently
    returns a dict again cannot pass: the host would stringify it with repr()
    instead of JSON.
    """

    assert isinstance(result, str), (
        f"registered tool handlers must return str, got {type(result).__name__}"
    )
    return json.loads(result)


@pytest.fixture
def tool_json():
    """Provide the JSON ABI assertion within this skill test subtree."""
    return _decode_tool_json
