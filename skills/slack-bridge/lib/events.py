from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Mapping, Sequence


# Slack file-object facts a model may see (https://docs.slack.dev/reference/objects/file-object/).
# Credentialed or bulky provider fields - url_private*, permalink_public, thumb_*,
# preview*, shares, initial_comment - are deliberately absent.
_FILE_TEXT_FACTS = (
    "id", "name", "title", "filetype", "pretty_type", "mimetype", "mode", "external_type",
    "external_id", "external_url", "permalink", "file_access",
)
_FILE_FLAG_FACTS = ("is_external", "is_deleted", "is_tombstoned", "is_hidden_by_limit")
# Already carried by the stored declaration and the frozen attachment keys.
DECLARED_FILE_KEYS = frozenset({"id", "name", "mimetype", "size"})


def file_facts(raw: Any) -> dict[str, Any]:
    """Project one Slack file object onto curated facts; malformed values are dropped."""
    if not isinstance(raw, Mapping):
        return {}
    facts: dict[str, Any] = {}
    for key in _FILE_TEXT_FACTS:
        value = raw.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
            facts[key] = str(value).strip()
    if isinstance(raw.get("size"), (str, int, float)) and not isinstance(raw.get("size"), bool):
        facts["size"] = _int(raw["size"])
    for key in _FILE_FLAG_FACTS:
        if isinstance(raw.get(key), bool):
            facts[key] = raw[key]
    return facts


def provider_facts(structured: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve event facts while omitting private file URLs and previews.

    Submitted events and queued-event observations use the same projection.
    """
    facts = dict(structured)
    for key in ("message", "previous_message"):
        carrier = facts.get(key)
        if isinstance(carrier, Mapping) and isinstance(carrier.get("files"), list):
            facts[key] = {**carrier, "files": [file_facts(file) for file in carrier["files"]]}
    return facts


@dataclass(frozen=True)
class SlackFile:
    file_id: str
    name: str
    mimetype: str
    size: int
    url_private: str
    facts: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        # url_private stays in the durable declaration for staging only.
        return {
            "file_id": self.file_id,
            "name": self.name,
            "mimetype": self.mimetype,
            "size": self.size,
            "url_private": self.url_private,
            **{key: value for key, value in self.facts.items() if key not in DECLARED_FILE_KEYS},
        }


@dataclass(frozen=True)
class SlackEvent:
    envelope_id: str
    event_id: str
    team_id: str
    enterprise_id: str
    event_type: str
    subtype: str
    actor_user_id: str
    actor_team_id: str
    channel_id: str
    channel_type: str
    message_ts: str
    thread_ts: str
    event_ts: str
    client_msg_id: str
    text: str
    files: tuple[SlackFile, ...]
    structured: dict[str, Any]

    @property
    def root_thread_ts(self) -> str:
        return self.thread_ts or self.message_ts

    @property
    def ordering_key(self) -> str:
        return f"{self.team_id}:{self.channel_id}:{self.root_thread_ts}"


@dataclass(frozen=True)
class ParsedEnvelope:
    envelope_id: str
    event_id: str
    accepted: bool
    reason: str
    event: SlackEvent | None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _files(raw: Any) -> tuple[SlackFile, ...]:
    """Keep every declared file with an ID, including ones without a private URL.

    Bytes are staged later when Slack serves them; the declaration itself is a
    message fact the model sees either way.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    parsed: list[SlackFile] = []
    for item in raw:
        facts = file_facts(item)
        file_id = facts.get("id", "")
        if not file_id:
            continue
        url = item.get("url_private_download") or item.get("url_private")
        parsed.append(
            SlackFile(
                file_id=file_id,
                name=facts.get("name") or facts.get("title") or file_id,
                mimetype=facts.get("mimetype") or "application/octet-stream",
                size=facts.get("size", 0),
                url_private=url.strip() if isinstance(url, str) else "",
                facts=facts,
            )
        )
    return tuple(parsed)


def _message_content_unchanged(
    message: Mapping[str, Any], previous: Mapping[str, Any]
) -> bool:
    """Recognize a provider revision, not a new message or a semantic edit."""
    if not _text(message.get("ts")) or message.get("ts") != previous.get("ts"):
        return False
    if not any(key in message and key in previous for key in ("text", "blocks", "attachments", "files")):
        return False
    # Slack also emits message_changed when its language detector runs. A new
    # edit timestamp alone is not new content either. Keep every other field in
    # the comparison so unknown provider additions still reach the model.
    bookkeeping = {"language", "edited"}
    current_content = {key: value for key, value in message.items() if key not in bookkeeping}
    previous_content = {key: value for key, value in previous.items() if key not in bookkeeping}
    # Canonical JSON preserves booleans versus numbers, unlike dict equality.
    return (
        json.dumps(current_content, sort_keys=True, separators=(",", ":"))
        == json.dumps(previous_content, sort_keys=True, separators=(",", ":"))
    )


def _mentioned_user_ids(message: Mapping[str, Any]) -> list[str]:
    """Collect explicit provider mention occurrences, never infer an addressee."""
    user_ids = set(re.findall(r"<@([^<>\s|]+)(?:\|[^<>]*)?>", str(message.get("text") or "")))
    blocks = message.get("blocks")
    pending = list(blocks) if isinstance(blocks, list) else []
    while pending:
        element = pending.pop()
        if not isinstance(element, Mapping):
            continue
        if element.get("type") == "user" and isinstance(element.get("user_id"), str):
            user_id = element["user_id"].strip()
            if user_id:
                user_ids.add(user_id)
        children = element.get("elements")
        if isinstance(children, list):
            pending.extend(children)
    return sorted(user_ids)


def parse_socket_envelope(
    payload: Mapping[str, Any],
    *,
    bot_user_id: str = "",
    bot_id: str = "",
    app_id: str = "",
) -> ParsedEnvelope:
    """Parse one Socket Mode envelope without adding policy or prompt text.

    Unsupported envelopes still return a stable classification so the caller can
    durably record them before acknowledging Slack.
    """

    envelope_id = _text(payload.get("envelope_id"))
    wrapper = payload.get("payload")
    wrapper = wrapper if isinstance(wrapper, Mapping) else {}
    event_id = _text(wrapper.get("event_id"))
    if _text(payload.get("type")) != "events_api":
        return ParsedEnvelope(envelope_id, event_id, False, "not_events_api", None)

    event = wrapper.get("event")
    event = event if isinstance(event, Mapping) else {}
    event_type = _text(event.get("type"))
    subtype = _text(event.get("subtype"))
    # Slack wraps changed messages under event.message and deleted messages
    # under previous_message. Normalize only provider shape, retaining both
    # nested objects in structured facts for the model and receipts.
    nested = event.get("message") if isinstance(event.get("message"), Mapping) else {}
    previous = event.get("previous_message") if isinstance(event.get("previous_message"), Mapping) else {}
    message = nested if subtype == "message_changed" else event
    actor_user_id = _text(message.get("user") or event.get("user") or previous.get("user"))
    bot_event_id = _text(message.get("bot_id") or event.get("bot_id") or previous.get("bot_id"))
    app_event_id = _text(message.get("app_id") or event.get("app_id") or previous.get("app_id"))
    if not actor_user_id and bot_event_id:
        actor_user_id = bot_event_id
    if not actor_user_id and app_event_id:
        actor_user_id = app_event_id
    item = event.get("item") if isinstance(event.get("item"), Mapping) else {}
    channel_id = _text(event.get("channel") or message.get("channel") or previous.get("channel") or item.get("channel"))
    message_ts = _text(message.get("ts") or item.get("ts") or event.get("ts") or previous.get("ts"))
    thread_ts = _text(event.get("thread_ts") or message.get("thread_ts") or item.get("thread_ts"))
    event_ts = _text(event.get("event_ts") or wrapper.get("event_time"))
    if subtype == "message_deleted":
        # The outer ts stamps the deletion itself; the deleted message and its
        # original thread are what the event is about.
        message_ts = _text(event.get("deleted_ts") or previous.get("ts"))
        thread_ts = _text(event.get("thread_ts") or previous.get("thread_ts"))
        event_ts = _text(event.get("event_ts") or event.get("ts") or wrapper.get("event_time"))
    files = _files(message.get("files") or event.get("files"))
    structured: dict[str, Any] = {}
    blocks = message.get("blocks") or event.get("blocks")
    if isinstance(blocks, Sequence) and not isinstance(blocks, (str, bytes, bytearray)):
        structured["blocks"] = [dict(item) for item in blocks if isinstance(item, Mapping)]
    if subtype == "message_changed":
        structured.update({"change": "edited", "message": dict(message), "previous_message": dict(previous)})
    elif subtype == "message_deleted":
        structured.update({"change": "deleted", "deleted_ts": _text(event.get("deleted_ts")), "previous_message": dict(previous)})
    if event_type in {"reaction_added", "reaction_removed"}:
        structured["reaction"] = {
            "kind": "added" if event_type == "reaction_added" else "removed",
            "name": _text(event.get("reaction")),
            "user_id": actor_user_id,
            "item": dict(event.get("item")) if isinstance(event.get("item"), Mapping) else {},
        }
    if event_type not in {"message", "reaction_added", "reaction_removed"}:
        return ParsedEnvelope(envelope_id, event_id, False, "unsupported_event", None)
    if ((bot_id and bot_event_id == bot_id) or (app_id and app_event_id == app_id)
            or (bot_user_id and actor_user_id == bot_user_id)):
        return ParsedEnvelope(envelope_id, event_id, False, "self_message", None)
    if not actor_user_id:
        return ParsedEnvelope(envelope_id, event_id, False, "missing_actor_provenance", None)
    if not channel_id or not message_ts:
        return ParsedEnvelope(
            envelope_id, event_id, False, "missing_message_provenance", None
        )
    if subtype == "message_changed" and _message_content_unchanged(nested, previous):
        return ParsedEnvelope(envelope_id, event_id, False, "message_content_unchanged", None)
    text = str(message.get("text") or event.get("text") or "")
    if not text and not files and not structured:
        return ParsedEnvelope(envelope_id, event_id, False, "empty_message", None)

    # These observations enrich accepted events; they must not make an empty
    # event admissible or turn an occurrence into a request to this bot.
    if bot_user_id:
        structured["self_user_id"] = bot_user_id
    if event_type == "message" and subtype != "message_deleted":
        mentioned = _mentioned_user_ids(message)
        if mentioned:
            structured["mentioned_user_ids"] = mentioned
        parent_user_id = _text(message.get("parent_user_id"))
        if parent_user_id:
            structured["parent_user_id"] = parent_user_id

    channel_type = _text(event.get("channel_type"))
    parsed = SlackEvent(
        envelope_id=envelope_id,
        event_id=event_id,
        team_id=_text(wrapper.get("team_id") or event.get("team")),
        enterprise_id=_text(wrapper.get("enterprise_id") or event.get("enterprise")),
        event_type=event_type,
        subtype=subtype,
        actor_user_id=actor_user_id,
        actor_team_id=_text(event.get("user_team") or event.get("team")),
        channel_id=channel_id,
        channel_type=channel_type,
        message_ts=message_ts,
        thread_ts=thread_ts,
        event_ts=event_ts,
        client_msg_id=_text(event.get("client_msg_id")),
        text=text,
        files=files,
        structured=structured,
    )
    return ParsedEnvelope(envelope_id, event_id, True, "accepted", parsed)
