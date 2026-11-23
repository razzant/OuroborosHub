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
    "provider_error": ("Connection error", "error"),
    # Telegram answered, and recording the answer failed on this computer: not a connection problem.
    "local_error": ("Error on this computer", "error"),
    "stopped": ("Stopped", "error"),
}

_RUNTIME_ITEM = {
    "waiting_for_token": ("The bot token is missing", "warn"),
    "provider_error": ("New messages are not being received", "error"),
    "local_error": ("New messages are not being processed", "error"),
    "stopped": ("The bot is not running", "error"),
}

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


_PROPER_NOUNS = ("Telegram", "Ouroboros")


def _sentence_tail(headline: str) -> str:
    """A headline continuing the first line: lower-case its first word unless it is a name."""
    if not headline or headline.split(" ", 1)[0] in _PROPER_NOUNS:
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
                if payload.get("poll_error"):
                    item["error"] = {"text": str(payload["poll_error"]), "at": payload.get("poll_at") or None}
        items.append(item)

    # A working link whose chats Places still cannot offer (the skill's own fixed reasons). A missing token is the
    # link's own item; a bot not confirmed yet ends by itself.
    unavailable = payload.get("connect_unavailable") if payload.get("connect") is None else None
    if link == "connected" and unavailable and unavailable not in (BOT_TOKEN_MISSING, BOT_NOT_RUNNING):
        items.append({"kind": "connect", "tone": "muted" if unavailable == BOT_UNCONFIRMED else "warn",
                      "headline": str(unavailable)})

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

    host_checked = payload.get("host_checked") is True
    if host_checked and payload.get("places_version") == 0 and payload.get("binding_state") != "configured":
        items.append({"kind": "binding", "tone": "warn", "headline": "Incoming messages wait for a place binding",
                      "count": _int(payload.get("inbox_waiting"))})
    if host_checked and payload.get("reaction_context") == "update_required":
        items.append({"kind": "reactions", "tone": "warn", "headline": "Reactions are not passed to Ouroboros",
                      "count": _int(payload.get("reactions_withheld"))})

    # Updates whose last attempt failed and is retried by itself (custody's own rows, never the runtime's newest
    # error): said, never amber. ``stages`` says where those attempts stopped (passing to Ouroboros, downloading from
    # Telegram, not recorded); a busy deferral that gave its attempt back is not one of them.
    retrying = payload.get("handoff_retrying")
    waiting = _int(retrying.get("count")) if isinstance(retrying, Mapping) else 0
    if waiting and link in _RUNNING and not any(item["kind"] == "binding" for item in items):
        stages = {key: _int(retrying.get(key)) for key in ("submit", "download")}
        stages["other"] = max(0, waiting - stages["submit"] - stages["download"])
        handoff: Dict[str, Any] = {
            "kind": "handoff_waiting", "tone": "muted", "events": waiting, "stages": stages,
            "headline": f"{_plural(waiting, 'Telegram update')} {'waits' if waiting == 1 else 'wait'} to be passed to Ouroboros",
            "at": retrying.get("since") or None, "next_at": retrying.get("next_attempt_at") or None}
        if retrying.get("last_error"):
            handoff["error"] = {"text": str(retrying["last_error"]), "at": None,
                                "stage": str(retrying.get("last_stage") or "")}
        items.append(handoff)
    if link == "connected":
        # A working link whose facts are partly unknown never reads as all clear (connecting never does anyway).
        gaps = []
        if not host_checked:
            gaps.append("host_unanswered" if payload.get("delivery_reporting") == "unavailable" else "capabilities_unchecked")
        if not isinstance(payload.get("recent_problems"), Mapping):
            gaps.append("no_recent_window")
        if gaps:
            items.append({"kind": "partial", "tone": "muted", "headline": "State partly known", "gaps": gaps})
    # The worst tone leads, so the first line names what colours it; the order of consequence breaks ties.
    return sorted(items, key=lambda item: -TONE_RANK[item["tone"]])


def summarize(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The first line (Places ``text``), its tone (the worst current condition) and the attention items."""
    link = _link(payload)
    phrase, tone = _LINK_PHRASE.get(link, ("Status unknown", "muted"))
    items = attention_items(payload)
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
    elif link == "connected":
        text = f"{phrase} · nothing needs attention"
    else:
        text = phrase
    return {"text": text, "tone": tone, "attention": items}
