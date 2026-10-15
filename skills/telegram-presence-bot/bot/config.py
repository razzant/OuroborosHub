"""Owner-chosen exact Bot API Presence mappings; no token in this file."""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

_BINDING = re.compile(r"[0-9a-fA-F]{32}\Z")


def validate(value: Any) -> dict:
    if (type(value) is not dict or set(value) != {"schema", "bindings", "download_video", "download_documents"}
            or value["schema"] != 1 or type(value["bindings"]) is not list
            or len(value["bindings"]) > 1000 or type(value["download_video"]) is not bool
            or type(value["download_documents"]) is not bool):
        raise ValueError("invalid Presence bot configuration")
    seen = set()
    for item in value["bindings"]:
        if type(item) is not dict or set(item) != {"chat_id", "topic_id", "binding_id"}:
            raise ValueError("invalid exact chat/topic mapping")
        chat, topic, binding = item["chat_id"], item["topic_id"], item["binding_id"]
        if (type(chat) is not int or chat == 0 or abs(chat) >= 2**63
                or (topic is not None and (type(topic) is not int or not 1 < topic < 2**31))
                or type(binding) is not str or not _BINDING.fullmatch(binding)
                or (chat, topic) in seen):
            raise ValueError("invalid or duplicate exact chat/topic mapping")
        seen.add((chat, topic))
    return value


def empty() -> dict:
    return {"schema": 1, "bindings": [], "download_video": False, "download_documents": False}


def load(root: Path) -> dict:
    path = Path(root) / "config.json"
    if path.is_symlink():
        raise ValueError("private configuration redirected")
    if not path.exists():
        return empty()
    data = path.read_bytes()
    if len(data) > 100_000:
        raise ValueError("private configuration too large")
    return validate(json.loads(data))


def save(root: Path, config: dict) -> None:
    root = Path(root)
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("private configuration root unavailable")
    config = validate(config)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "config.json"
    if target.is_symlink():
        raise ValueError("private configuration redirected")
    temp = root / (".config-" + secrets.token_hex(16))
    try:
        with temp.open("xb") as stream:
            if os.name != "nt":
                os.chmod(temp, 0o600)
            stream.write(json.dumps(config, sort_keys=True, separators=(",", ":")).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def binding_for(config: dict, chat: int, topic: int | None) -> str | None:
    return next((item["binding_id"] for item in config["bindings"]
                 if item["chat_id"] == chat and item["topic_id"] == topic), None)
