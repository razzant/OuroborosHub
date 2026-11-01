"""Connector-local settings shared by the form and incoming event capture."""
import json
from pathlib import Path


def load_settings(state_dir):
    try:
        value = json.loads((Path(state_dir) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def normalize_words(value):
    # Match email-presence: the form uses lines; the saved setting is a list.
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list) or any(not isinstance(word, str) for word in value):
        raise ValueError("Words must be a list of plain strings")
    return [word.strip() for word in value if word.strip()]
