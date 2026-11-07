"""The settings route hydrates the Settings form without secrets and keeps an untouched password.

CREATING_SKILLS ("Hydration"): the host GETs a form's own route before rendering it and pre-fills the fields from the
answer; a 404/405 locks the form behind "Edit anyway", after which Save sends every field. Password fields are never
pre-filled, so an empty password on save means "unchanged". Before 1.5.2 the route took POST only and replaced the
whole document, so a save erased the stored A2A_SERVER_PASSWORD and a non-loopback server refused to start.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import pathlib
import sys

SKILL = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE = "a2a_settings_route_skill"


def _plugin():
    if _PACKAGE not in sys.modules:
        package = importlib.util.module_from_spec(importlib.util.spec_from_loader(_PACKAGE, loader=None, is_package=True))
        package.__path__ = [str(SKILL)]
        sys.modules[_PACKAGE] = package
    return importlib.import_module(f"{_PACKAGE}.plugin")


class _Api:
    def __init__(self, root: pathlib.Path):
        self.root = root

    def get_state_dir(self) -> str:
        return str(self.root)


class _Request:
    def __init__(self, method: str, body=None):
        self.method = method
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _call(tmp_path, method, body=None):
    response = asyncio.run(_plugin()._make_settings_save(_Api(tmp_path))(_Request(method, body)))
    return response.status_code, json.loads(response.body)


def _stored(tmp_path) -> dict:
    return json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))


def _store(tmp_path, values: dict) -> None:
    (tmp_path / "settings.json").write_text(json.dumps(values), encoding="utf-8")


def test_get_hydrates_the_stored_values_as_strings_and_never_the_password(tmp_path):
    assert _call(tmp_path, "GET") == (200, {})  # nothing stored yet: every field shows its default
    _store(tmp_path, {"A2A_HOST": "0.0.0.0", "A2A_PORT": 18800, "A2A_SERVER_PASSWORD": "s3cret",
                      "A2A_TASK_TTL_HOURS": 24})
    status, values = _call(tmp_path, "GET")
    assert status == 200 and values == {"A2A_HOST": "0.0.0.0", "A2A_PORT": "18800"}


def test_an_untouched_password_survives_a_save_and_unsent_keys_stay(tmp_path):
    _store(tmp_path, {"A2A_HOST": "0.0.0.0", "A2A_AGENT_NAME": "Old name", "A2A_SERVER_PASSWORD": "s3cret"})
    status, answer = _call(tmp_path, "POST", {"A2A_HOST": "0.0.0.0", "A2A_PORT": "19000", "A2A_SERVER_PASSWORD": ""})
    assert status == 200 and answer["ok"] is True
    assert _stored(tmp_path) == {"A2A_HOST": "0.0.0.0", "A2A_AGENT_NAME": "Old name", "A2A_SERVER_PASSWORD": "s3cret",
                                 "A2A_PORT": "19000"}


def test_a_typed_password_replaces_the_saved_one(tmp_path):
    _store(tmp_path, {"A2A_SERVER_PASSWORD": "s3cret"})
    assert _call(tmp_path, "POST", {"A2A_SERVER_PASSWORD": "n3w-secret"})[0] == 200
    assert _stored(tmp_path) == {"A2A_SERVER_PASSWORD": "n3w-secret"}


def test_an_invalid_payload_changes_nothing(tmp_path):
    _store(tmp_path, {"A2A_HOST": "127.0.0.1"})
    for body in (["not", "a", "form"], ValueError("not JSON")):
        status, answer = _call(tmp_path, "POST", body)
        assert status == 400 and answer["ok"] is False
    assert _stored(tmp_path) == {"A2A_HOST": "127.0.0.1"}


def test_the_route_is_registered_for_the_hydration_read():
    source = (SKILL / "plugin.py").read_text(encoding="utf-8")
    assert 'register_route("settings/save", handler=_make_settings_save(api), methods=("GET", "POST"))' in source
