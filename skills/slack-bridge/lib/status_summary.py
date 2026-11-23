"""One reading of ``GET status``: the Places line and the Widgets card's first line say the same thing.

``summarize(payload)`` returns ``{"text", "tone", "attention"}``. ``text``/``tone`` are the route's existing
Places keys; ``attention`` lists only what is wrong or needs the owner now. Order: worst tone first (error, then
warn, then muted); within one tone, by consequence: the link, outcomes nobody knows, what was not delivered, what
was not handled, what is held, notes (capabilities, settings, hand-offs waiting), what is only partly known. The
first line names the first item, so its dot and its words agree, and adds "· N more need attention". Every item is
built from a fact the status payload already reports; nothing here guesses a state the store does not record.
Problems older than the store's recent window are history, shown by the card's totals, not attention.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

TONE_RANK = {"muted": 0, "ok": 0, "warn": 1, "error": 2}
# Within one tone, items keep this order of consequence.
CONSEQUENCE = ("link", "outcome_unknown", "not_delivered", "not_handled", "held",
               "reactions", "new_root_channels", "handoff_waiting", "partial")

_LINK_PHRASE = {
    "connected": ("Connected", "ok"),
    "starting": ("Connecting to Slack…", "muted"),
    "reconnecting": ("Reconnecting to Slack…", "muted"),
    "disconnected": ("Disconnected", "error"),
    "not_configured": ("Not set up", "warn"),
    "error": ("Connection error", "error"),
}

# (headline, tone) of the link item, by link.detail. Starting and reconnecting end by themselves: no item.
_LINK_ITEM = {
    "host_adapter_inactive": ("Slack events are not passed to Ouroboros", "warn"),
    "companion_not_running": ("The bridge process is not running", "error"),
    "disconnected": ("The bridge closed its Slack connection", "error"),
    "not_started": ("The bridge has not started", "error"),
    "missing_tokens": ("Slack tokens are missing", "warn"),
    "missing_bot_token": ("The Slack bot token is missing", "warn"),
    "missing_app_token": ("The Slack app token is missing", "warn"),
    "unreadable_settings": ("The bridge settings can't be read", "error"),
    "invalid_binding_id": ("The saved place binding is invalid", "error"),
    "corrupt_runtime_state": ("The bridge's saved state can't be read", "error"),
    "socket_error": ("The Slack connection failed", "error"),
    "companion_exited_error": ("The bridge stopped after an error", "error"),
    "store_unreadable": ("The bridge's queue store can't be read", "error"),
}

HELD_PREFIX = "Held:"
# lib/socket_mode.py _FATAL_AUTH_ERRORS, repeated here so this module stays import-light (a test keeps them equal).
AUTH_REFUSALS = frozenset({
    "invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired", "missing_scope",
    "invalid_app_token", "invalid_token", "not_allowed_token_type",
})
# How SlackApiError prints itself (lib/slack_api.py) when the daemon stores str(exc) instead of the bare code.
_SLACK_ERROR_PREFIX = "Slack API error: "


def auth_refused(text: str) -> bool:
    """True when a stored connection error is Slack refusing the tokens, by the skill's own codes and format."""
    code = str(text or "").strip()
    if code.startswith(_SLACK_ERROR_PREFIX):
        code = code[len(_SLACK_ERROR_PREFIX):].strip()
    return code in AUTH_REFUSALS
# lib/host_adapter.py SKILL_ACCESS_REFUSED, repeated here so this module imports nothing from the adapter.
SKILL_ACCESS_REFUSED = "Held: Check this skill's access in Skills."
# The hold notes the skill writes that wait for a condition clearing by itself or by someone's choice: the Host did
# not answer (lib/host_adapter.py PLACE_POLICY_UNREAD), a reaction's history waits for an update (lib/runtime.py),
# and a place someone paused or silenced ("Held: <place> is paused"). The skill's other hold notes are the owner's
# move: its own access refusal, and a place Ouroboros says is not connected (the Host's own words after "Held:").
_NEUTRAL_HOLDS = frozenset({"Held: It retries by itself.", "Held: Ouroboros records this reaction after an update."})
_CHOSEN_HOLD = re.compile(r"Held: .+ is (paused|silent)")


def owner_hold(note: str) -> bool:
    """True when a hold note waits for the owner's move rather than a condition that clears by itself."""
    note = str(note or "")
    return note.startswith(HELD_PREFIX) and note not in _NEUTRAL_HOLDS and not _CHOSEN_HOLD.fullmatch(note)
_PROPER_NOUNS = ("Slack", "Ouroboros")


def _sentence_tail(headline: str) -> str:
    """A headline continuing the first line: lower-case its first word unless it is a name."""
    if not headline or headline.split(" ", 1)[0] in _PROPER_NOUNS:
        return headline
    return headline[0].lower() + headline[1:]


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _units(messages: int, actions: int, events: int = 0) -> str:
    parts = []
    if messages:
        parts.append(_plural(messages, "message"))
    if actions:
        parts.append(_plural(actions, "action"))
    if events:
        parts.append(_plural(events, "event"))
    return " and ".join(parts) if len(parts) < 3 else f"{parts[0]}, {parts[1]} and {parts[2]}"


def _latest(*stamps: Any) -> str | None:
    known = [stamp for stamp in stamps if isinstance(stamp, str) and stamp]
    return max(known) if known else None


def _newest_error(*errors: Any) -> dict[str, Any] | None:
    known = [error for error in errors if isinstance(error, Mapping) and error.get("at")]
    return dict(max(known, key=lambda error: str(error["at"]))) if known else None


def _problem(recent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = recent.get(key)
    return value if isinstance(value, Mapping) else {}


def _state_error(part: Mapping[str, Any], state: str, count: int) -> Mapping[str, Any] | None:
    """The queue's newest error of ``state`` in the recent window, only when that queue counts such a problem now.

    Older payloads carry one ``last_error`` per queue; it is used only when its own state matches.
    """
    if not count:
        return None
    error = part.get(f"last_{state}_error")
    if f"last_{state}_error" not in part:
        error = part.get("last_error")
    return error if isinstance(error, Mapping) and error.get("state") == state else None


def attention_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What needs attention now: worst tone first, then by consequence within a tone; empty when nothing does."""
    items: list[dict[str, Any]] = []
    link = payload.get("link") if isinstance(payload.get("link"), Mapping) else {}
    detail = str(link.get("detail") or "")
    if detail in _LINK_ITEM:
        headline, tone = _LINK_ITEM[detail]
        item: dict[str, Any] = {"kind": "link", "detail": detail, "tone": tone, "headline": headline}
        if link.get("error"):
            item["error"] = {"text": str(link["error"])}
        if detail in {"socket_error", "companion_exited_error"}:
            # Token advice belongs only to an auth refusal; any other error is shown as itself.
            item["auth"] = auth_refused(str(link.get("error") or ""))
            if item["auth"]:
                item["headline"] = "Slack refused the bridge's tokens"
        if detail == "host_adapter_inactive" and _int(payload.get("inbox_pending")):
            item["count"] = _int(payload.get("inbox_pending"))
        items.append(item)

    recent = payload.get("recent_problems") if isinstance(payload.get("recent_problems"), Mapping) else {}
    inbound, text, mutations = (_problem(recent, key) for key in ("inbound", "outbound_text", "mutations"))

    unknown = _units(_int(text.get("uncertain_messages")), _int(mutations.get("uncertain")), _int(inbound.get("uncertain")))
    if unknown:
        items.append({
            "kind": "outcome_unknown", "tone": "warn", "headline": f"Outcome unknown for {unknown}",
            "messages": _int(text.get("uncertain_messages")), "actions": _int(mutations.get("uncertain")),
            "partly_delivered": _int(text.get("uncertain_messages_partly_delivered")),
            "events": _int(inbound.get("uncertain")),
            "at": _latest(text.get("last_uncertain_at"), mutations.get("last_uncertain_at"), inbound.get("last_uncertain_at")),
            "error": _newest_error(_state_error(text, "uncertain", _int(text.get("uncertain_messages"))),
                                   _state_error(mutations, "uncertain", _int(mutations.get("uncertain"))),
                                   _state_error(inbound, "uncertain", _int(inbound.get("uncertain")))),
        })

    failed = _units(_int(text.get("failed_messages")), _int(mutations.get("failed")))
    if failed:
        items.append({
            "kind": "not_delivered", "tone": "error", "headline": f"{failed} not delivered",
            "messages": _int(text.get("failed_messages")), "actions": _int(mutations.get("failed")),
            "partly_delivered": _int(text.get("failed_messages_partly_delivered")),
            "at": _latest(text.get("last_failed_at"), mutations.get("last_failed_at")),
            "error": _newest_error(_state_error(text, "failed", _int(text.get("failed_messages"))),
                                   _state_error(mutations, "failed", _int(mutations.get("failed")))),
        })

    if _int(inbound.get("failed")):
        count = _int(inbound.get("failed"))
        items.append({
            "kind": "not_handled", "tone": "error", "headline": f"{_plural(count, 'Slack event')} not handled",
            "events": count, "at": inbound.get("last_failed_at"),
            "error": _newest_error(_state_error(inbound, "failed", count)),
        })

    held = _int(payload.get("outbox_held"))
    if held:
        chunks = payload.get("outbound_text_chunks") if isinstance(payload.get("outbound_text_chunks"), Mapping) else {}
        messages = payload.get("outbound_messages") if isinstance(payload.get("outbound_messages"), Mapping) else {}
        held_messages = _int(messages.get("held"))
        held_actions = max(0, held - _int(chunks.get("held")))
        note = str(payload.get("last_delivery_error") or "")
        listed = [reason for reason in payload.get("held_reasons") or [] if isinstance(reason, Mapping)]
        if not listed and note.startswith(HELD_PREFIX):
            # An older status without the list: the newest stored note is a hold's reason only with the prefix.
            listed = [{"reason": note[len(HELD_PREFIX):].strip(), "note": note, "count": 0}]
        # A hold waits for a condition: a place someone paused or silenced, or Ouroboros answering; those stay
        # neutral. A reason only the owner can clear (the skill's own access refused, a place not connected) makes
        # the item amber and is listed first.
        owner = any(owner_hold(str(reason.get("note") or "")) for reason in listed)
        listed.sort(key=lambda reason: not owner_hold(str(reason.get("note") or "")))
        item = {"kind": "held", "tone": "warn" if owner else "muted",
                "headline": f"{_units(held_messages, held_actions) or _plural(held, 'item')} held",
                "messages": held_messages, "actions": held_actions,
                "reasons": [{"reason": str(reason.get("reason") or ""), "count": _int(reason.get("count"))}
                            for reason in listed]}
        if listed:
            item["reason"] = item["reasons"][0]["reason"]
        items.append(item)

    # "unavailable": Ouroboros did not answer the bridge's capability check, so "update_required" is a guess (D8).
    host_unanswered = payload.get("history_reporting_state") == "unavailable"
    if payload.get("reaction_context_state") == "update_required" and not host_unanswered:
        items.append({"kind": "reactions", "tone": "warn", "headline": "Reactions are not passed to Ouroboros",
                      "count": _int(payload.get("reactions_withheld"))})

    if str(payload.get("new_root_channels_error") or ""):
        items.append({"kind": "new_root_channels", "tone": "warn",
                      "headline": "The new-root channel list was ignored",
                      "error": {"text": str(payload["new_root_channels_error"])}})

    retrying = payload.get("inbox_retrying") if isinstance(payload.get("inbox_retrying"), Mapping) else {}
    if _int(retrying.get("count")) and detail != "host_adapter_inactive":
        count = _int(retrying.get("count"))
        last = retrying.get("last_error") if isinstance(retrying.get("last_error"), Mapping) else None
        items.append({"kind": "handoff_waiting", "tone": "muted", "events": count,
                      "headline": f"{_plural(count, 'Slack event')} {'waits' if count == 1 else 'wait'} to be passed to Ouroboros",
                      "at": last.get("at") if last else None,
                      "error": {"text": str(last.get("text") or ""), "at": last.get("at")} if last else None})

    # A connected bridge whose facts are partly unknown never reads as all clear; an unanswered process probe is
    # said whatever the link state, since every other link line assumes the probe answered.
    gaps = []
    if "companion_alive" in link and link.get("companion_alive") is None and detail != "store_unreadable":
        gaps.append("process_unknown")
    if link.get("state") == "connected":
        if host_unanswered:
            gaps.append("host_unanswered")
        elif payload.get("reaction_context_state") == "unknown":
            gaps.append("capabilities_unchecked")
        if not recent:
            gaps.append("no_recent_window")
    if gaps:
        items.append({"kind": "partial", "tone": "muted", "headline": "State partly known", "gaps": gaps})
    # The worst tone leads, so the first line names what colours it; the order of consequence breaks ties.
    return sorted(items, key=lambda item: (-TONE_RANK[item["tone"]], CONSEQUENCE.index(item["kind"])))


def summarize(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The first line (Places ``text``), its tone (the worst current condition) and the attention items."""
    link = payload.get("link") if isinstance(payload.get("link"), Mapping) else {}
    phrase, tone = _LINK_PHRASE.get(str(link.get("state") or ""), ("Status unknown", "muted"))
    if link.get("detail") == "store_unreadable":
        phrase = "Status unknown"  # without the store nothing is known about the link itself
    items = attention_items(payload)
    if items and tone == "ok":
        tone = "muted"  # green only while nothing is listed
    for item in items:
        if TONE_RANK[item["tone"]] > TONE_RANK[tone]:
            tone = item["tone"]
    if items:
        first = items[0]["headline"]
        text = f"{phrase} · {_sentence_tail(first)}"
        if len(items) > 1:
            # Self-explanatory on the card and on the Places sheet alike: what the count counts.
            more = len(items) - 1
            text += f" · {more} more {'needs' if more == 1 else 'need'} attention"
    elif link.get("state") == "connected":
        text = f"{phrase} · nothing needs attention"
    else:
        text = phrase
    return {"text": text, "tone": tone, "attention": items}
