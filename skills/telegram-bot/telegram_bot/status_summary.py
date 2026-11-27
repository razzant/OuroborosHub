"""One reading of ``GET status``: the Places line and the Widgets card's first line say the same thing.

``summarize(payload)`` returns ``{"text", "tone", "attention"}``. ``text``/``tone`` are the route's existing Places
keys (the skill contributes a place, no composer chip); ``attention`` lists only what is wrong or needs the owner
now, in the order of consequence: the link, outcomes nobody knows, what was not delivered or not passed on, what is
held, then capability notes. Every item comes from a fact the payload reports; problems older than the custody's
recent window are history in the card's totals, not attention.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping

from .host import HOLD_SKILL_ACCESS, OWNER_HOLD_KINDS, SKILL_ACCESS_REFUSED
from .runtime import BOT_NOT_RUNNING, BOT_TOKEN_MISSING, BOT_UNCONFIRMED

TONE_RANK = {"muted": 0, "ok": 0, "warn": 1, "error": 2}

# The link's state (see _link) -> (first words, tone). Starting and connecting end by themselves; the others say
# what the bot is doing. Only an answer Telegram gave the last completed attempt, for the token set now, is green.
_LINK_PHRASE = {
    "connected": ("Connected", "ok"),
    "connecting": ("Connecting…", "muted"),
    "not_started": ("Starting…", "muted"),
    "waiting_for_token": ("Not set up", "warn"),
    "conflict_with_owner_bot": ("Same bot as personal chat", "warn"),
    "provider_error": ("Connection error", "error"),
    # Telegram answered, and recording the answer failed on this computer: not a connection problem.
    "local_error": ("Error on this computer", "error"),
    "stopped": ("Stopped", "error"),
}

_RUNTIME_ITEM = {
    "waiting_for_token": ("The bot token is missing", "warn"),
    "conflict_with_owner_bot": ("This token belongs to your personal Telegram bot; polling paused", "warn"),
    "provider_error": ("New messages are not being received", "error"),
    "local_error": ("New messages are not being processed", "error"),
    "stopped": ("The bot is not running", "error"),
}

# The owner's own bot in groups (the owner-bot feed, see owner_link). Its group messages are read from Ouroboros on
# this computer, never from Telegram by this skill, so a failure there is never Telegram's or the network's, and a
# feed that answers says only that: the bridge's own link to Telegram is the bridge's to show.
_OWNER_PHRASE = {
    "connected": ("Receiving through Ouroboros", "ok"),
    "connecting": ("Starting…", "muted"),
    "feed_off": ("Not set up", "warn"),
    "waiting": ("Waiting for your bot", "muted"),
    "local_error": ("Error on this computer", "error"),
    "stopped": ("Stopped", "error"),
}
_OWNER_ITEM = {
    "feed_off": ("Groups are off for your own bot", "warn"),
    "local_error": ("New group messages are not being received", "error"),
    "stopped": ("The bot is not running", "error"),
}
# Why the owner's bot is waiting (the runtime's feed_reason); only a refused access is the owner's move.
_OWNER_WAIT = {
    "bridge_not_ready": "Ouroboros's own Telegram connection is not running",
    "identity_unconfirmed": "Ouroboros has not confirmed your bot yet",
    "account_mismatch": "Your own Telegram bot changed",
    "auth_blocked": "Ouroboros refused this skill's access",
    "connector_mismatch": "Ouroboros refused this skill's access",
}
_OWNER_RUNNING = frozenset({"connected", "connecting", "local_error", "waiting"})

# States in which the runtime's workers run and retry by themselves.
_RUNNING = frozenset({"connected", "connecting", "provider_error", "local_error", "waiting_for_token"})


def _link(payload: Mapping[str, Any]) -> str:
    """The link's state, judged for the token configured now, from facts the status already holds.

    ``runtime_state`` may still describe an earlier token (a backoff after its refusal, the pause while no token was
    set); only ``poll_result`` (the last completed attempt for the token set now) and ``connect_unavailable`` (why
    getMe's bot is not offered, read with that same token) speak for the current one:
    no token -> ``waiting_for_token``; a completed failure -> ``provider_error`` (``local_error`` when Telegram
    answered and this computer failed); a completed answer for a confirmed bot -> ``connected``; nothing completed
    yet -> ``connecting``.
    """
    state = str(payload.get("runtime_state") or "")
    if state not in ("polling", "provider_error", "waiting_for_token"):
        return state
    known = "connect" in payload or "connect_unavailable" in payload  # the route always says; bare facts may not
    unavailable = payload.get("connect_unavailable") if payload.get("connect") is None else None
    if unavailable == BOT_TOKEN_MISSING or (state == "waiting_for_token" and not known):
        return "waiting_for_token"
    result = payload.get("poll_result")
    if result == "failed":  # runtime.py writes it together with runtime_state "provider_error"
        return "local_error" if payload.get("poll_failure") == "local" else "provider_error"
    if result == "ok" and unavailable != BOT_UNCONFIRMED:
        return "connected"
    # A request still out for this token, or a token set while the runtime still waits out an earlier one.
    return "connecting"


def link_state(payload: Mapping[str, Any]) -> str:
    """The link's state as the first line judges it (``_link``): the route returns it as ``link``, so every reader
    (the card's Details) words the same state, never a second reading of ``runtime_state``."""
    return _link(payload)


_PROPER_NOUNS = ("Telegram", "Ouroboros")


def _sentence_tail(headline: str) -> str:
    """A headline continuing the first line: lower-case its first word unless it is a name (or a name's possessive)."""
    first = headline.split(" ", 1)[0] if headline else ""
    if not headline or first.split("'", 1)[0] in _PROPER_NOUNS:
        return headline
    return headline[0].lower() + headline[1:]


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _part(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    recent = payload.get("recent_problems")
    value = recent.get(key) if isinstance(recent, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _error_of(part: Mapping[str, Any], state: str, key: str = "") -> Dict[str, Any] | None:
    """The newest error of that state: its own key when the store gives one, else the shared newest error."""
    for error in (part.get(key) if key else None, part.get("last_error")):
        if isinstance(error, Mapping) and error.get("state") == state and error.get("text"):
            return dict(error)
    return None


def _custody_items(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """What the custody store says went wrong or waits: unknown outcomes, sends not delivered, updates Ouroboros
    refused, held sends (the store's own rows, for whichever bot the store belongs to)."""
    items: List[Dict[str, Any]] = []
    outbound, inbound = _part(payload, "outbound"), _part(payload, "inbound")
    unknown = _int(outbound.get("uncertain"))
    if unknown:
        # Outbox rows are messages, files, reactions, moderation and operations: "outgoing item", not "message".
        items.append({"kind": "outcome_unknown", "tone": "warn", "messages": unknown,
                      "headline": f"Outcome unknown for {_plural(unknown, 'outgoing item')}",
                      "at": outbound.get("last_uncertain_at") or None,
                      # Of them, how many Telegram confirmed in part (the receipt's confirmed_messages).
                      "partial": min(unknown, _int(outbound.get("uncertain_partly_confirmed"))),
                      "error": _error_of(outbound, "uncertain", "last_uncertain_error")})

    failed = _int(outbound.get("failed"))
    if failed:
        items.append({"kind": "not_delivered", "tone": "error", "messages": failed,
                      "headline": f"{_plural(failed, 'outgoing item')} not delivered",
                      "at": outbound.get("last_failed_at") or None,
                      "partial": min(failed, _int(outbound.get("failed_partly_confirmed"))),
                      "error": _error_of(outbound, "failed", "last_failed_error")})

    refused = _int(inbound.get("failed"))
    if refused:
        items.append({"kind": "not_handed", "tone": "error", "events": refused,
                      "headline": f"{_plural(refused, 'Telegram update')} not passed to Ouroboros",
                      "at": inbound.get("last_failed_at") or None, "error": _error_of(inbound, "failed")})

    held = _int(payload.get("outbox_held"))
    if held:
        reasons = [reason for reason in payload.get("held_reasons") or [] if isinstance(reason, Mapping)]
        # A hold waits for a condition someone chose (a paused place) or for Ouroboros to answer; only the Host's
        # answers that the skill's access was refused or the place is not connected are the owner's move (their
        # stored kind; a hold kept before 0.13.1 is known by the skill's own refusal note).
        def kind(reason: Mapping[str, Any]) -> str:
            stored = str(reason.get("kind") or "")
            return stored or (HOLD_SKILL_ACCESS if reason.get("note") == SKILL_ACCESS_REFUSED else "")

        def action(reason: Mapping[str, Any]) -> bool:
            return kind(reason) in OWNER_HOLD_KINDS

        owner = any(action(reason) for reason in reasons)
        # The owner's moves lead the reasons, whatever their count; the rest keep the store's order (most first).
        listed = sorted(reasons, key=lambda reason: not action(reason))
        items.append({"kind": "held", "tone": "warn" if owner else "muted", "messages": held,
                      "headline": f"{_plural(held, 'outgoing item')} held",
                      "reasons": [{"reason": str(reason.get("reason") or ""), "count": _int(reason.get("count")),
                                   **({"action": True, "kind": kind(reason)} if action(reason) else {})}
                                  for reason in listed]})
    return items


def _handoff_item(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Updates whose last attempt failed and is retried by itself (custody's own rows, never the runtime's newest
    error): said, never amber. ``stages`` says where those attempts stopped (passing to Ouroboros, downloading an
    attachment, not recorded); a busy deferral that gave its attempt back is not one of them."""
    retrying = payload.get("handoff_retrying")
    waiting = _int(retrying.get("count")) if isinstance(retrying, Mapping) else 0
    if not waiting:
        return []
    stages = {key: _int(retrying.get(key)) for key in ("submit", "download")}
    stages["other"] = max(0, waiting - stages["submit"] - stages["download"])
    # "to be passed to Ouroboros" only when every one recorded a download or handoff failure: an update whose stage
    # was not recorded may already be handled (Ouroboros answered, recording that here failed), so it waits for
    # another attempt, which is all that is known of it.
    waits = "waits" if waiting == 1 else "wait"
    handoff: Dict[str, Any] = {
        "kind": "handoff_waiting", "tone": "muted", "events": waiting, "stages": stages,
        "headline": (f"{_plural(waiting, 'Telegram update')} {waits} for another attempt" if stages["other"]
                     else f"{_plural(waiting, 'Telegram update')} {waits} to be passed to Ouroboros"),
        "at": retrying.get("since") or None, "next_at": retrying.get("next_attempt_at") or None}
    if retrying.get("last_error"):
        handoff["error"] = {"text": str(retrying["last_error"]), "at": retrying.get("last_error_at") or None,
                            "stage": str(retrying.get("last_stage") or "")}
        if retrying.get("last_error_recorded") is False:
            handoff["error"]["unrecorded"] = True  # an earlier version's note: not known to be a failure
        elif retrying.get("last_error_newest") is False:
            handoff["error"]["example"] = True  # which failure is the latest cannot be told: one of them
    return [handoff]


def _by_tone(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # The worst tone leads, so the first line names what colours it; the order of consequence breaks ties.
    return sorted(items, key=lambda item: -TONE_RANK[item["tone"]])


def attention_items(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """What needs attention now: worst tone first, then by consequence; empty when nothing does."""
    items: List[Dict[str, Any]] = []
    link = _link(payload)
    if link in _RUNTIME_ITEM:
        headline, tone = _RUNTIME_ITEM[link]
        item: Dict[str, Any] = {"kind": "link", "detail": link, "tone": tone, "headline": headline}
        if link in ("provider_error", "local_error"):
            if payload.get("provider_ok_at"):
                item["at"] = payload["provider_ok_at"]
            # This poll attempt's own error and side, for the token set now: never another loop's newer error,
            # never an earlier token's.
            if payload.get("poll_result") == "failed":
                item["failure"] = str(payload.get("poll_failure") or "")
                item["stage"] = str(payload.get("poll_stage") or "")
                if payload.get("poll_error"):
                    item["error"] = {"text": str(payload["poll_error"]), "at": payload.get("poll_at") or None}
        items.append(item)
    elif link in ("not_started", "connecting") and payload.get("ready_wait"):
        items.append({"kind": "link", "detail": link, "tone": "muted", "headline": str(payload["ready_wait"])})

    # A working link whose chats Places still cannot offer (the skill's own fixed reasons). A missing token is the
    # link's own item; a bot not confirmed yet ends by itself.
    unavailable = payload.get("connect_unavailable") if payload.get("connect") is None else None
    if link == "connected" and unavailable and unavailable not in (BOT_TOKEN_MISSING, BOT_NOT_RUNNING):
        items.append({"kind": "connect", "tone": "muted" if unavailable == BOT_UNCONFIRMED else "warn",
                      "headline": str(unavailable)})

    items += _custody_items(payload)

    host_checked = payload.get("host_checked") is True
    if host_checked and payload.get("places_version") == 0 and payload.get("binding_state") != "configured":
        items.append({"kind": "binding", "tone": "warn", "headline": "Incoming messages wait for a place binding",
                      "count": _int(payload.get("inbox_waiting"))})
    if host_checked and payload.get("reaction_context") == "update_required":
        items.append({"kind": "reactions", "tone": "warn", "headline": "Reactions are not passed to Ouroboros",
                      "count": _int(payload.get("reactions_withheld"))})

    if link in _RUNNING and not any(item["kind"] == "binding" for item in items):
        items += _handoff_item(payload)
    if link == "connected":
        # A working link whose facts are partly unknown never reads as all clear (connecting never does anyway).
        gaps = []
        if not host_checked:
            # Not asked (no request was made), asked without an answer, or not asked yet.
            gaps.append("host_unasked" if payload.get("host_unasked")
                        else "host_unanswered" if payload.get("delivery_reporting") == "unavailable"
                        else "capabilities_unchecked")
        if not isinstance(payload.get("recent_problems"), Mapping):
            gaps.append("no_recent_window")
        if gaps:
            items.append({"kind": "partial", "tone": "muted", "headline": "State partly known", "gaps": gaps})
    return _by_tone(items)


def _line(phrase: str, tone: str, items: List[Dict[str, Any]], connected: bool) -> Dict[str, Any]:
    """A first line from its link's words and tone and the attention items (already worst first)."""
    if items and tone == "ok":
        tone = "muted"  # green only while nothing is listed
    for item in items:
        if TONE_RANK[item["tone"]] > TONE_RANK[tone]:
            tone = item["tone"]
    if items:
        text = f"{phrase} · {_sentence_tail(items[0]['headline'])}"
        if len(items) > 1:
            rest = len(items) - 1
            text += f" · {rest} more {'needs' if rest == 1 else 'need'} attention"
    elif connected:
        text = f"{phrase} · nothing needs attention"
    else:
        text = phrase
    return {"text": text, "tone": tone, "attention": items}


def summarize(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The first line (Places ``text``), its tone (the worst current condition) and the attention items."""
    link = _link(payload)
    phrase, tone = _LINK_PHRASE.get(link, ("Status unknown", "muted"))
    return _line(phrase, tone, attention_items(payload), link == "connected")


def owner_link(payload: Mapping[str, Any]) -> str:
    """The owner's own bot in groups, from its runtime's facts (runtime.status_snapshot of the owner-bot feed):
    ``connected`` once a feed read answered for the confirmed bot, ``connecting``, ``feed_off``, ``waiting`` (the
    feed's reason says for what), ``local_error`` (reading the feed from Ouroboros failed), ``stopped`` or
    ``unsupported`` (this Ouroboros does not offer it)."""
    state = str(payload.get("runtime_state") or "")
    if state in ("stopped", "unsupported", "feed_off"):
        return state
    if state == "waiting_for_owner_bot":
        return "waiting"
    if state == "provider_error":
        return "local_error"
    if state == "polling" and payload.get("poll_result") == "failed":
        return "local_error"
    if state == "polling" and payload.get("poll_result") == "ok":
        return "connected"
    return "connecting"


def owner_attention(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """What needs attention for the owner's own bot in groups: its link, then its own store's problems."""
    link = owner_link(payload)
    items: List[Dict[str, Any]] = []
    if link in _OWNER_ITEM:
        headline, tone = _OWNER_ITEM[link]
        item: Dict[str, Any] = {"kind": "owner_link", "detail": link, "tone": tone, "headline": headline}
        if link == "local_error" and payload.get("last_error"):
            item["error"] = {"text": str(payload["last_error"]), "at": payload.get("last_error_at") or None}
            if payload.get("provider_ok_at"):
                item["at"] = payload["provider_ok_at"]
        items.append(item)
    elif link == "waiting":
        reason = str(payload.get("feed_reason") or "")
        refused = reason in ("auth_blocked", "connector_mismatch")
        items.append({"kind": "owner_link", "detail": reason or "waiting", "tone": "warn" if refused else "muted",
                      "headline": _OWNER_WAIT.get(reason, "Ouroboros did not answer about your bot just now")})
    if payload.get("store") == "unreadable":
        items.append({"kind": "owner_store", "tone": "warn", "headline": "Could not read this bot's delivery history"})
    items += _custody_items(payload)
    if link in _OWNER_RUNNING:
        items += _handoff_item(payload)
    return _by_tone(items)


def summarize_owner(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The owner's own bot in groups as the card words it: ``link``, its first line and its attention items."""
    link = owner_link(payload)
    phrase, tone = _OWNER_PHRASE.get(link, ("Status unknown", "muted"))
    return {"link": link, **_line(phrase, tone, owner_attention(payload), link == "connected")}


def card_summary(payload: Mapping[str, Any], owner: Mapping[str, Any] | None) -> Dict[str, Any]:
    """The Widgets card's first line and items, for the bots this skill has here.

    Without a standalone token the owner's bot leads, but outstanding or unsuccessful separate-bot records
    still belong to that separate bot. With both, the standalone bot's line leads as before; the owner's bot adds
    its own problems, and its groups being off is then no problem."""
    if owner is None:
        return summarize(payload)
    if _link(payload) == "waiting_for_token":
        extra = _custody_items(payload) + _handoff_item(payload)
        has_history = bool(extra) or any(_int(payload.get(key)) for key in (
            "inbox_waiting", "inbox_leased", "inbox_retained", "inbox_failed", "outbox_waiting", "outbox_held",
            "outbox_leased", "outbox_failed", "outbox_uncertain",
        ))
        if not has_history:
            return {"text": owner["text"], "tone": owner["tone"], "attention": list(owner["attention"])}
        extra = [{**item, "bot": "separate", "headline": f"Separate bot: {_sentence_tail(item['headline'])}"}
                 for item in extra]
        link = owner["link"]
        phrase, tone = _OWNER_PHRASE.get(link, ("Status unknown", "muted"))
        return {**_line(phrase, tone, _by_tone(list(owner["attention"]) + extra), link == "connected"),
                "separate_history": True}
    label = str(owner.get("bot_label") or "")
    who = f"Your bot {label}" if label.startswith("@") else "Your own bot"
    optional = {"feed_off"}
    if owner.get("store") == "none":
        optional.add("bridge_not_ready")  # no owner-bot history to bring beside a working separate connection
    extra = [{**item, "bot": "owner", "headline": f"{who}: {_sentence_tail(item['headline'])}"}
             for item in owner["attention"] if not (item["kind"] == "owner_link" and item["detail"] in optional)]
    link = _link(payload)
    phrase, tone = _LINK_PHRASE.get(link, ("Status unknown", "muted"))
    return _line(phrase, tone, _by_tone(attention_items(payload) + extra), link == "connected")
