"""One strict reader for this skill's own ``settings.json``.

The extension child and the companion process both read the same file, so they
share one contract: an absent file means "not configured yet" and is the only
condition that yields an empty mapping. A file that exists but cannot be read,
is not JSON, or is not a JSON object is a typed error, never a silent empty
settings dict that would look like a missing Presence binding and let a save
overwrite bytes nobody could read. Both also share one parser for the
new-root channel list.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

SETTINGS_FILENAME = "settings.json"
# Channels whose new top-level posts carry ``signal.new_root``; absent means none.
NEW_ROOT_CHANNELS = "SLACK_NEW_ROOT_CHANNELS"
NEW_ROOT_CHANNELS_MAX = 100
# The exact conversation-ID form ``slack_resolve`` recognizes, with a length bound.
_CHANNEL_ID = re.compile(r"[CGD][A-Z0-9]{8,20}")


class LocalSettingsError(RuntimeError):
    """``settings.json`` exists but is unreadable, not JSON, or not an object."""


def normalize_channel_ids(value: Any) -> list[str]:
    """Exact Slack conversation IDs from text or a list, in first-seen order.

    Commas, spaces and newlines separate entries. A name, a ``#name`` or any
    other token is refused whole, never looked up or guessed into an ID.
    """

    if value is None:
        return []
    if isinstance(value, str):
        tokens = value.replace(",", " ").split()
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        tokens = [token for item in value for token in item.replace(",", " ").split()]
    else:
        raise ValueError(f"{NEW_ROOT_CHANNELS} must be text or a list of Slack channel IDs")
    for token in tokens:
        if not _CHANNEL_ID.fullmatch(token):
            hint = "use its channel ID, not its name" if token.startswith("#") else "names are not accepted"
            raise ValueError(f"{NEW_ROOT_CHANNELS}: {token[:80]!r} is not a Slack channel ID "
                             f"such as C0123456789 ({hint})")
    channels = list(dict.fromkeys(tokens))
    if len(channels) > NEW_ROOT_CHANNELS_MAX:
        raise ValueError(f"{NEW_ROOT_CHANNELS} accepts at most {NEW_ROOT_CHANNELS_MAX} channel IDs")
    return channels


def settings_path(state_dir: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(state_dir) / SETTINGS_FILENAME


def load_local_settings(state_dir: pathlib.Path | str) -> dict[str, Any]:
    """Return the saved settings object, or ``{}`` only when the file is absent."""

    path = settings_path(state_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise LocalSettingsError(
            f"{SETTINGS_FILENAME} exists but could not be read: {exc}"
        ) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LocalSettingsError(
            f"{SETTINGS_FILENAME} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise LocalSettingsError(f"{SETTINGS_FILENAME} must contain a JSON object")
    return value
