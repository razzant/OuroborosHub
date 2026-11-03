"""Mailbox-local switches shared by the tools and companion."""
import json
from pathlib import Path

SENDING_OFF = "Sending is off for this mailbox in its connector settings; reading, drafts and tidying keep working"


def load_settings(state_dir):
    path = Path(state_dir) / "settings.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Email settings must be a JSON object")
    return value


def normalize_words(value):
    # The form uses one line per word; the saved connector setting is a list.
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list) or any(not isinstance(word, str) for word in value):
        raise ValueError("Words must be a list of plain strings")
    return [word.strip() for word in value if word.strip()]
