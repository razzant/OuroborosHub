from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class SlackFile:
    file_id: str
    name: str
    mimetype: str
    size: int
    url_private: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "mimetype": self.mimetype,
            "size": self.size,
            "url_private": self.url_private,
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
    except (TypeError, ValueError):
        return 0


def _files(raw: Any) -> tuple[SlackFile, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    parsed: list[SlackFile] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        file_id = _text(item.get("id"))
        url_private = _text(item.get("url_private_download") or item.get("url_private"))
        if not file_id or not url_private:
            continue
        parsed.append(
            SlackFile(
                file_id=file_id,
                name=_text(item.get("name") or item.get("title") or file_id),
                mimetype=_text(item.get("mimetype") or "application/octet-stream"),
                size=_int(item.get("size")),
                url_private=url_private,
            )
        )
    return tuple(parsed)


def parse_socket_envelope(
    payload: Mapping[str, Any],
    *,
    bot_user_id: str = "",
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
    actor_user_id = _text(event.get("user"))
    channel_id = _text(event.get("channel"))
    message_ts = _text(event.get("ts"))
    files = _files(event.get("files"))

    if event_type != "message":
        return ParsedEnvelope(envelope_id, event_id, False, "unsupported_event", None)
    if subtype in {"bot_message", "message_changed", "message_deleted"}:
        return ParsedEnvelope(envelope_id, event_id, False, "unsupported_subtype", None)
    if event.get("bot_id") or not actor_user_id:
        return ParsedEnvelope(envelope_id, event_id, False, "non_human_actor", None)
    if bot_user_id and actor_user_id == bot_user_id:
        return ParsedEnvelope(envelope_id, event_id, False, "self_message", None)
    if not channel_id or not message_ts:
        return ParsedEnvelope(
            envelope_id, event_id, False, "missing_message_provenance", None
        )
    if not _text(event.get("text")) and not files:
        return ParsedEnvelope(envelope_id, event_id, False, "empty_message", None)

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
        thread_ts=_text(event.get("thread_ts")),
        event_ts=_text(event.get("event_ts") or wrapper.get("event_time")),
        client_msg_id=_text(event.get("client_msg_id")),
        text=str(event.get("text") or ""),
        files=files,
    )
    return ParsedEnvelope(envelope_id, event_id, True, "accepted", parsed)
