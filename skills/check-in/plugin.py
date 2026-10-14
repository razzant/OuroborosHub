"""Check-in extension: agent tools, owner widget and settings, and the activation lease.

The skill never schedules anything itself. The agent registers wakes with the
built-in ``schedule_followup``; a wake is an ordinary root task that calls ``due``.
The server process holds an OS lock for as long as the skill is loaded, picks a
fresh activation epoch, locks that epoch's own file and only then records the
epoch; liveness is always probed on the recorded epoch's file. Arming the contact
stage binds to that epoch, so a restart or reload turns the contact stage off until
the owner arms it again, and a new holder never vouches for the epoch before it. When
that happens to a stage that was armed, the activation task makes one best-effort
attempt at an owner notice about it (claimed before the request, never retried; at
startup the host may not accept it yet, so it can go unconfirmed); the widget is the
reliable view.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import secrets
import threading
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable, Dict

from starlette.responses import JSONResponse

from . import host, lease, mailer
from .store import MAX_AGENT_BODY, CheckinError, Store, fmt_local

UTC = timezone.utc

_SEND_RESULT = {
    "accepted": "Check-in: a message about your missed check-in was accepted by your mail server for {name}. "
                "It cannot be recalled and will not be repeated.",
    "uncertain": "Check-in: the message to {name} may or may not have been delivered (no clear answer from "
                 "the mail server). It will not be retried.",
    "not_sent": "Check-in: the message to {name} was not sent ({code}). It will not be retried automatically.",
    "aborted_before_data": "Check-in: the message to {name} was not sent: just before sending, the agreement or "
                           "contact stage changed or could not be confirmed ({code}). It will not be retried.",
}

_ARM_TEXT = {
    "armed": "armed",
    "off": "off (not armed)",
    "agreement_changed": "off — the agreement was replaced or ended since arming",
    "contact_changed": "off — the contact changed since arming",
    "restarted": "off — Ouroboros restarted or the skill reloaded since arming; arm again to turn it on",
    "not_active": "off — the skill is not active in the server right now",
    "mail_not_configured": "off — the mail server is not configured",
}

_EPISODE_TEXT = {
    "not_armed": "contact stage not armed",
    "waiting": "waiting for grace to end",
    "window": "contact window open (agent deciding)",
    "window_expired": "contact window opened but never decided (no message was claimed)",
    "grace_expired": "the grace wake never arrived (stopped, deleted or late); no contact for this streak",
    "attempted": "contact attempted",
    "declined": "agent declined to contact",
    "stale": "skipped as too late",
    "blocked_notice": "blocked: owner notice not confirmed",
}

_LIMITS = (
    "Not an emergency or medical service. Nothing happens while this computer is asleep or off, or "
    "without a model, budget or mail server. Only “I’m here”, a dated check-in or telling Ouroboros counts as a "
    "check-in. Stop on a task, cancelling, pausing, disabling the skill and Panic never count as “fine”."
)

# The target fields of the Settings form “Check in for displayed deadline” (the ``checkin`` route);
# the form also shows ``deadline_status``, which the server ignores.
_TARGET_FIELDS = ("agreement_id", "for_deadline", "deadline_label")

# The Check-in card is a small module widget (``widget.js``): its “I'm here” button posts exactly
# the deadline it shows (agreement id, UTC instant and label), which a declarative action, whose
# body is fixed, cannot do. It reads only the ``status`` route and posts only to this skill's own
# routes, through the host's bridge. ``auto``: a cheap status card (one GET every 30 s while shown).
WIDGET_RENDER = {"kind": "module", "entry": "widget.js", "start": "auto", "appearance": "host", "span": 2}

SETTINGS_SCHEMA = {
    "components": [
        {"type": "markdown", "text": (
            "Check in for one dated deadline. When Settings loads, this form shows the nearest deadline of your "
            "current agreement (after today's has passed, that is tomorrow's); submitting answers exactly that "
            "deadline, and submitting again changes nothing. It never moves on to the next deadline by itself: "
            "reload Settings to see a newer one. A form left open after the agreement was replaced, or after its "
            "deadline is no longer current, is refused and changes nothing. “I’m here” on the Check-in card checks "
            "in for today's deadline or closes a missed one.")},
        {"type": "form", "id": "checkin_for", "route": "checkin", "method": "POST",
         "submit_label": "Check in for displayed deadline", "busy_label": "Checking in…", "fields": [
             # Read-only; the long texts are text areas so the whole deadline and status stay readable.
             {"name": "deadline_label", "label": "Check in for", "type": "textarea", "disabled": True,
              "default": "not loaded", "help": "As loaded. The server checks it against the UTC deadline."},
             {"name": "deadline_status", "label": "Status (at form load)", "type": "textarea", "disabled": True,
              "default": "not loaded"},
             {"name": "for_deadline", "label": "Deadline (UTC)", "type": "text", "disabled": True},
             {"name": "agreement_id", "label": "Agreement", "type": "text", "disabled": True},
         ]},
        {"type": "markdown", "text": (
            "One contact who has agreed to receive a message if you miss a check-in, and the mail server used "
            "to send it (your own account; TLS with certificate checks is required). Saving the contact always "
            "turns the contact stage off; arm it again in the Check-in widget. To try it safely, first enter "
            "your own address as the contact. The password is write-only: leave it empty to keep the saved one.")},
        {"type": "form", "id": "contact", "route": "settings/contact", "method": "POST", "submit_label": "Save contact",
         "fields": [
             {"name": "contact_name", "label": "Contact name", "type": "text", "placeholder": "Masha"},
             {"name": "contact_email", "label": "Contact email", "type": "text", "placeholder": "name@example.org"},
             {"name": "contact_consent", "label": "This person agreed to receive check-in messages from my Ouroboros",
              "type": "checkbox", "help": "Required each time you save a contact."},
             {"name": "remove_contact", "label": "Remove the contact instead", "type": "checkbox"},
         ]},
        {"type": "form", "id": "mail", "route": "settings/mail", "method": "POST", "submit_label": "Save mail server",
         "fields": [
             {"name": "smtp_host", "label": "SMTP host", "type": "text", "placeholder": "smtp.example.org"},
             {"name": "smtp_port", "label": "Port", "type": "number", "min": 1, "max": 65535, "step": 1},
             {"name": "smtp_security", "label": "Security", "type": "select", "options": [
                 {"value": "ssl", "label": "Implicit TLS (usually 465)"},
                 {"value": "starttls", "label": "STARTTLS (usually 587)"},
             ]},
             {"name": "smtp_username", "label": "Username", "type": "text"},
             {"name": "password_status", "label": "Saved password (at form load)", "type": "text", "disabled": True,
              "default": "Not saved"},
             {"name": "smtp_password", "label": "Password", "type": "password",
              "help": "Leave empty to keep the saved password."},
             {"name": "clear_password", "label": "Clear the saved password", "type": "checkbox"},
             {"name": "smtp_from", "label": "From address", "type": "text", "placeholder": "you@example.org"},
             {"name": "mail_test_status", "label": "Last connection test (at form load)", "type": "text",
              "disabled": True, "default": "not tested"},
         ]},
        {"type": "markdown", "text": (
            "Test saved mail server connects to the saved server with a verified TLS certificate and logs in, "
            "then disconnects. It names no recipient and sends nothing, so it checks the connection and login "
            "only, not delivery. It runs only when you press it, on a budget of about 15 seconds: each wait for "
            "the server is cut to what is left of it, but looking up the server's name, or a server that answers "
            "very slowly bit by bit, can take longer. Saving the server again clears the result.")},
        {"type": "action", "id": "mail_test", "route": "settings/mail/test", "method": "POST",
         "label": "Test saved mail server", "busy_label": "Testing…"},
    ],
}


def _now() -> datetime:
    return datetime.now(UTC)


def _dump(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _refused(code: str, message: str) -> str:
    return _dump({"ok": False, "code": code, "message": message})


def _meta(ctx: Any) -> Dict[str, Any]:
    value = getattr(ctx, "task_metadata", None)
    return value if isinstance(value, dict) else {}


def _caller_refusal(ctx: Any, *, owner_turn: bool) -> str:
    """Who may call: never Presence or delegated children; owner actions never from a scheduled wake.

    The positive half of the owner-turn rule (the owner door's stamp) is ``_owner_words_refusal``.
    """
    meta = _meta(ctx)
    contract = getattr(ctx, "task_contract", None)
    contract = contract if isinstance(contract, dict) else {}
    if "presence" in meta or "presence_binding_authority" in meta or "capability_ceiling" in contract:
        return "A Presence conversation cannot use Check-in."
    lineage = contract.get("lineage") if isinstance(contract.get("lineage"), dict) else {}
    if "subagent" in {str(meta.get("delegation_role") or ""), str(contract.get("delegation_role") or ""),
                      str(lineage.get("delegation_role") or "")}:
        return "A delegated subagent cannot use Check-in; report to your parent instead."
    if owner_turn and isinstance(meta.get("schedule_occurrence"), dict):
        return ("A scheduled wake cannot act for the owner (check in, set up, cancel, pause or arm). "
                "Tell the owner instead.")
    return ""


_QUOTES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u00ab": '"',
                         "\u00bb": '"'})


def _normalized(text: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).translate(_QUOTES).casefold().split())


def _owner_words_refusal(ctx: Any, words: Any) -> str:
    """Owner actions need the owner door's stamp on this run, and the owner's words from it.

    Owner routing alone writes ``task_metadata.origin_message_ref`` (with the message text in
    ``origin_message_text``) for a turn the owner started; the core reads the same stamp as
    ``run_origin(...).owner_ingress``. Schedules and the task API strip it, so scheduled,
    background-consciousness and API-submitted runs never carry it, and delegated children
    are refused separately. The quoted words must occur in that stamped message: a provenance
    cross-check against the host's record, not an authentication of the text. A model inside an
    owner-started run can still misuse a quote; the tool descriptions forbid acting on inference.
    """
    meta = _meta(ctx)
    door = meta.get("origin_message_ref")
    door_text = meta.get("origin_message_text")
    if not (isinstance(door, dict) and door) or not isinstance(door_text, str) or not door_text.strip():
        return ("Only a turn the owner started with their own message can do this; this run was not (for example a "
                "scheduled, background or API task). Tell the owner to use the Check-in widget or to ask you in chat.")
    chat_ids = (door.get("chat_id"), getattr(ctx, "current_chat_id", None))
    if any(isinstance(value, int) and not isinstance(value, bool) and value < 0 for value in chat_ids):
        return "A synthetic (agent-to-agent) conversation cannot act for the owner."
    quoted = _normalized(words)
    if not quoted or quoted not in _normalized(door_text):
        return ("Quote the owner's own words from the message that started this turn; they must appear in it. If the "
                "owner said it earlier, ask them to confirm now, or point them to the Check-in widget.")
    return ""


def _occurrence(ctx: Any) -> Dict[str, Any]:
    """The host-minted ``schedule_occurrence`` of a scheduled task, else an empty dict."""
    occurrence = _meta(ctx).get("schedule_occurrence")
    return occurrence if isinstance(occurrence, dict) else {}


def _schedule_id(ctx: Any) -> str:
    return str(_occurrence(ctx).get("schedule_id") or "")[:160]


def _task_id(ctx: Any) -> str:
    return str(getattr(ctx, "task_id", "") or "")[:80]


def _guard(fn: Callable[..., Dict[str, Any]], *, owner_turn: bool, owner_words: str = "") -> Callable[..., str]:
    """``owner_words`` names the argument carrying the owner's quoted words for an owner action."""
    def handler(ctx: Any, **kwargs: Any) -> str:
        refusal = _caller_refusal(ctx, owner_turn=owner_turn)
        if not refusal and owner_words:
            if not str(kwargs.get(owner_words) or "").strip():
                return _refused("value_invalid", f"{owner_words} (the owner's own words asking for this) is required")
            refusal = _owner_words_refusal(ctx, kwargs.get(owner_words))
        if refusal:
            return _refused("caller_refused", refusal)
        try:
            return _dump({"ok": True, **fn(ctx, **kwargs)})
        except CheckinError as exc:
            return _refused(exc.code, exc.message)
    handler.__name__ = getattr(fn, "__name__", "handler")
    return handler


def _target(kind: str, agreement_id: str, entry: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    return {"kind": kind, "agreement_id": agreement_id, "for_deadline": entry["due_utc"],
            "deadline_label": entry["label"], **extra}


def _targets(ag: Dict[str, Any]) -> Any:
    """What the card's buttons are bound to (as a dated check-in of that exact deadline).

    ``checkin_target`` — the main “I'm here”: today's deadline while it is ahead and unanswered
    (it also closes anything missed before it), otherwise the latest missed deadline; for a
    one-time agreement its deadline. ``next_target`` — the separate early check-in for the next
    deadline when that is another day's; like any dated check-in it also closes what was missed
    before it. ``also_closes`` names the latest missed deadline either one would close (None when
    nothing is missed). Both None while paused or with nothing to answer.
    """
    if ag["status"] != "active":
        return None, None
    today, near, missed = ag.get("today_deadline"), ag.get("nearest_deadline"), ag.get("latest_missed")
    if ag["kind"] == "once":
        return (_target("once", ag["id"], near) if near else None), None
    main = None
    if today and not today["passed"] and today.get("answered") is not True:
        main = _target("today", ag["id"], today, also_closes=missed["label"] if missed else None)
    elif missed:
        main = _target("missed", ag["id"], missed)
    early = None
    if (near and near.get("answered") is not True and (today is None or near["due_utc"] != today["due_utc"])
            and (main is None or main["for_deadline"] != near["due_utc"])):
        early = _target("next", ag["id"], near, also_closes=missed["label"] if missed else None)
    return main, early


def _today_row(ag: Dict[str, Any]) -> str:
    if ag["status"] == "paused":
        return "— (paused)"
    if ag["kind"] == "once":
        return "—"
    today = ag.get("today_deadline")
    if today is None:
        return "no deadline left today"
    if today.get("answered") is True:
        state = "checked in"
    elif today.get("answered") is None:
        state = "unknown (recorded by an earlier version)"
    else:
        state = "passed without a check-in" if today["passed"] else "not checked in yet"
    return f"{today['label']} · {state}"


def widget_view(status: Dict[str, Any]) -> Dict[str, Any]:
    """Owner-facing strings and button targets for the Check-in card (no secrets, masked email)."""
    ag, ep = status.get("agreement"), status.get("episode")
    stage, contact = status.get("contact_stage") or {}, status.get("contact") or {}
    expected, last_wake, attempt = status.get("expected_wake"), status.get("last_wake"), status.get("last_attempt")
    warnings, alerts = [], []
    tz = ag["timezone"] if ag else "UTC"

    def local(value: Any) -> str:
        return f"{fmt_local(value, tz)} ({tz})" if value else "—"

    if ag is None:
        headline = ("No check-in agreement. Ask Ouroboros in chat to set one up, for example: "
                    "“check on me every day by 21:00 Europe/Berlin”.")
        agreement_row = today_row = next_row = last_checkin_row = "—"
    else:
        if ag["kind"] == "daily":
            what = f"daily check-in by {ag['daily_time']} ({tz})"
        else:
            what = f"one-time check-in by {ag['deadline_local']} ({tz})"
        headline = what[0].upper() + what[1:] + "."
        upcoming = ag.get("next_unanswered_deadline")
        if ag["status"] == "paused":
            headline += f" Paused until {ag['paused_until_local']}."
        elif upcoming:
            headline += f" Next unanswered deadline {upcoming['label']}."
        agreement_row = (f"{what} · grace {ag['grace_minutes']} min after the reminder · lateness cutoff "
                         f"{ag['lateness_cutoff_minutes']} min · {ag['status']} · revision {ag['revision']} · "
                         f"id {ag['id']}")
        today_row = _today_row(ag)
        next_row = upcoming["label"] if upcoming else ("— (paused)" if ag["status"] == "paused" else "—")
        last_checkin_row = f"{ag['last_checkin_local']} ({tz})" if ag.get("last_checkin_local") else "never"
    missed = (ag or {}).get("latest_missed")
    if ep is None:
        streak_row = "none"
        if missed and not missed.get("processed"):
            streak_row = f"deadline {missed['label']} passed; no wake has processed it yet"
            warnings.append("A check-in deadline passed. Press “I’m here” on this card if you are fine.")
    else:
        streak_row = (f"{ep['missed_deadlines']} missed since {ep['first_missed_local']} · reminder "
                      f"{ep['notice']}{(' at ' + ep['notice_at_local']) if ep.get('notice_at_local') else ''} · "
                      f"{_EPISODE_TEXT.get(ep['contact_state'], ep['contact_state'])}"
                      f"{(' until ' + ep['grace_ends_local']) if ep['contact_state'] == 'waiting' else ''}")
        warnings.append("A check-in was missed. Press “I’m here” on this card if you are fine.")
    if not contact.get("configured"):
        stage_row = "off — no consenting contact configured (Settings → Check-in)"
    else:
        stage_row = f"{_ARM_TEXT.get(stage.get('state'), stage.get('state'))} · contact {contact.get('name')} ({contact.get('email')})"
        if stage.get("state") == "restarted":
            warnings.append("The contact stage turned off after a restart or reload. Arm it again if you still want it.")
    wake_parts = []
    if expected:
        ids = ", ".join(expected.get("agent_reported_schedule_ids") or []) or "none reported"
        wake_parts.append(f"expected {expected['kind']} wake {expected['at_local']} · schedule {ids} (agent-reported)")
        if expected.get("overdue") or not expected.get("agent_reported_schedule_ids"):
            warnings.append(expected["note"])
    if last_wake:
        wake_parts.append(f"last arrival {local(last_wake['arrived_at'])} ({last_wake['kind']}, "
                          f"{'registered schedule' if last_wake['via_registered_schedule'] else 'not from a reported schedule'})")
    if attempt is None:
        attempt_row = "none"
    else:
        # The newest attempt may belong to an earlier streak or a replaced agreement: only the
        # open streak's own attempt is about the check-in missed now.
        current = attempt.get("for_open_streak") is True
        attempt_row = (f"{'this missed check-in' if current else 'historical (an earlier missed check-in)'} · "
                       f"{attempt['state']} · to {attempt['to']} · started {local(attempt['claimed_at'])} · "
                       f"“{attempt['subject']}”")
        in_doubt = attempt["state"] in ("uncertain", "unknown", "sending")
        if current and in_doubt:
            alerts.append("The message to your contact about this missed check-in is in progress or has an unknown "
                          "outcome; it is never retried automatically.")
        elif current and attempt["state"] == "accepted":
            alerts.append(f"A message about this missed check-in was handed to the mail server for {attempt['to']} "
                          "(accepted for delivery; not proof that it was read).")
        elif in_doubt:
            warnings.append(f"A message to your contact about an earlier missed check-in (started "
                            f"{local(attempt['claimed_at'])}) is in progress or has an unknown outcome; it is never "
                            "retried automatically.")
    mail = status.get("mail") or {}
    if not mail.get("configured"):
        mail_row = "not configured (Settings → Check-in)"
    else:
        mail_row = (f"{mail.get('host')} ({mail.get('security')}) · "
                    f"{Store.mail_test_text(mail.get('test') or {'state': 'never'}, tz)}")
    activation = status.get("activation") or {}
    activation_row = (f"active since {local(activation.get('started_at'))}" if activation.get("lease_alive")
                      else "not active in the server (the contact stage cannot be armed)")
    checkin_target, next_target = _targets(ag) if ag is not None else (None, None)
    active = ag is not None and ag["status"] == "active"
    return {
        "headline": headline,
        "warning": " ".join(warnings),
        "alert": " ".join(alerts),
        "rows": {"agreement": agreement_row, "today": today_row, "next_unanswered": next_row,
                 "streak": streak_row, "last_checkin": last_checkin_row,
                 "contact_stage": stage_row, "mail": mail_row, "wakes": " · ".join(wake_parts) or "none yet",
                 "attempt": attempt_row, "activation": activation_row},
        "checkin_target": checkin_target,
        "next_target": next_target,
        "controls": {"pause": active and ag["kind"] == "daily",
                     "resume": ag is not None and ag["status"] == "paused",
                     "cancel": ag is not None,
                     "arm": active and not stage.get("armed"),
                     "disarm": bool(stage.get("armed"))},
        "limits": _LIMITS,
    }


def register(api: Any) -> None:
    state_dir = pathlib.Path(api.get_state_dir())
    state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(state_dir / "checkin.sqlite3")
    store.init()
    lease_path = state_dir / "activation.lock"
    holder: Dict[str, Any] = {}

    def alive(epoch: str) -> bool:
        """The store's lease probe: is the holder of exactly this (recorded) epoch alive?"""
        return lease.epoch_alive(state_dir, epoch)

    # ---- tools

    def t_status(ctx: Any) -> Dict[str, Any]:
        return store.status(now=_now(), lease_alive=alive)

    def t_setup(ctx: Any, kind: str = "", timezone: str = "", deadline_local: str = "", daily_time: str = "",
                grace_minutes: Any = 60, lateness_cutoff_minutes: Any = 360, contact_guidance: str = "",
                owner_request: str = "") -> Dict[str, Any]:
        result = store.setup(kind=kind, timezone_name=timezone, deadline_local=deadline_local, daily_time=daily_time,
                             grace_minutes=grace_minutes, cutoff_minutes=lateness_cutoff_minutes,
                             guidance=contact_guidance, owner_request=owner_request, now=_now())
        result["next_step"] = (
            "Call schedule_followup with exactly the wake_plan arguments, then wake_registered(kind='deadline', "
            "schedule_id=<id from FOLLOWUP_SCHEDULED>, agreement_id). Delete every cleanup_schedule_ids row with "
            "manage_schedules. Then tell the owner the first deadline, any warnings, the limits, and that the "
            "contact stage is off until armed.")
        return result

    def t_wake_registered(ctx: Any, kind: str = "", schedule_id: str = "", agreement_id: str = "",
                          episode_id: str = "") -> Dict[str, Any]:
        return store.register_wake(kind=kind, schedule_id=schedule_id, agreement_id=agreement_id,
                                   episode_id=episode_id, task_id=_task_id(ctx), now=_now())

    def t_due(ctx: Any, wake_kind: str = "", agreement_id: str = "", episode_id: str = "") -> Dict[str, Any]:
        plan = store.due(wake_kind=wake_kind, agreement_id=agreement_id, episode_id=episode_id,
                         schedule_id=_schedule_id(ctx), schedule_due_at=str(_occurrence(ctx).get("due_at") or ""),
                         task_id=_task_id(ctx), now=_now(), lease_alive=alive)
        if plan.get("action") != "notify_owner":
            return plan
        try:
            notice = host.post_notice(api.get_skill_token(), plan["notice_text"])
        except Exception:  # a missing token or host refusal is reported, never retried here
            notice = {"outcome": "refused", "http_status": None, "code": "token_unavailable"}
        detail = f"http {notice.get('http_status')} {notice.get('code') or ''}".strip()
        result = store.notice_result(episode_id=plan["episode_id"], notice_due_utc=plan["notice_due_utc"],
                                     outcome=notice["outcome"], detail=detail, now=_now(), lease_alive=alive)
        result.update({"missed_deadlines": plan["missed_deadlines"], "stale": plan["stale"],
                       "owner_notice": notice["outcome"], "owner_notice_text": plan["notice_text"]})
        return result

    def t_checkin(ctx: Any, owner_message: str = "", agreement_id: Any = None, for_deadline: Any = None) -> Dict[str, Any]:
        return store.checkin(source="chat", note=owner_message, now=_now(), agreement_id=agreement_id,
                             for_deadline=for_deadline)

    def t_control(ctx: Any, action: str = "", owner_request: str = "", until_local: str = "") -> Dict[str, Any]:
        now = _now()
        if action == "pause":
            return store.pause(until_local=until_local, source="chat", now=now)
        if action == "resume":
            return store.resume(source="chat", now=now)
        if action == "cancel":
            result = store.cancel(source="chat", now=now)
            result["next_step"] = "Delete every cleanup_schedule_ids row with manage_schedules(action='delete')."
            return result
        if action == "arm_contact":
            return store.arm(source="chat", statement=owner_request, now=now, lease_alive=alive)
        if action == "disarm_contact":
            return store.disarm(source="chat", now=now)
        raise CheckinError("value_invalid", "action must be pause, resume, cancel, arm_contact or disarm_contact")

    def t_send_contact(ctx: Any, episode_id: str = "", agreement_revision: Any = "", subject: str = "",
                       body: str = "", reason: str = "") -> Dict[str, Any]:
        if not _schedule_id(ctx):
            raise CheckinError("not_a_wake", "send_contact works only inside the scheduled grace wake task.")
        claim = store.admit_send(episode_id=episode_id, agreement_revision=agreement_revision, subject=subject,
                                 body=body, reason=reason, schedule_id=_schedule_id(ctx), task_id=_task_id(ctx),
                                 pid=os.getpid(), now=_now(), lease_alive=alive)
        attempt_id = claim["attempt_id"]
        try:
            outcome = mailer.send_message(
                recheck=lambda: store.still_admissible(attempt_id=attempt_id, lease_alive=alive)[0],
                timeout=15.0, **claim["mail"])
        except Exception:  # never lose the claim: an unexpected failure is an unknown outcome
            outcome = {"state": "uncertain", "code": "unexpected_error", "smtp_code": None}
        store.finish_attempt(attempt_id=attempt_id, state=str(outcome["state"]),
                             detail=f"{outcome['code']} {outcome.get('smtp_code') or ''}".strip(), now=_now())
        text = _SEND_RESULT[str(outcome["state"])].format(name=claim["mail"]["to_name"], code=outcome["code"])
        try:
            notice = host.post_notice(api.get_skill_token(), text)["outcome"]
        except Exception:
            notice = "refused"
        return {"attempt_id": attempt_id, "state": outcome["state"], "code": outcome["code"],
                "owner_notice": notice,
                "next_step": ("Tell the owner exactly what happened in your reply. Never send again for this streak; "
                              "an accepted message means the mail server took it, not that it was read.")}

    def t_decline(ctx: Any, episode_id: str = "", agreement_revision: Any = "", reason: str = "") -> Dict[str, Any]:
        return store.decline(episode_id=episode_id, agreement_revision=agreement_revision, reason=reason,
                             schedule_id=_schedule_id(ctx), task_id=_task_id(ctx), now=_now())

    text_prop = {"type": "string"}
    api.register_tool("status", _guard(t_status, owner_turn=False), timeout_sec=20, description=(
        "Check-in skill: current agreement, missed-check-in streak, contact stage, expected wakes and last contact "
        "attempt (for_open_streak says whether it is about the open streak or an earlier one). "
        "agreement.today_deadline is what an ordinary check-in counts for while it is ahead; "
        "agreement.nearest_deadline gives the agreement_id, due_utc and label a dated check-in needs; "
        "next_unanswered_deadline and latest_missed are listed separately. Read-only; never reveals the "
        "contact's full address or the mail password."),
        schema={"type": "object", "properties": {}, "additionalProperties": False})
    api.register_tool("setup", _guard(t_setup, owner_turn=True, owner_words="owner_request"), timeout_sec=20,
                      description=(
        "Create or replace the owner's check-in agreement. Only in a turn the owner started, on their explicit "
        "request, quoting their words from that message; never guess the timezone. Returns wake_plan for "
        "schedule_followup. Replacing turns the contact stage off."),
        schema={"type": "object", "additionalProperties": False,
                "required": ["kind", "timezone", "owner_request"],
                "properties": {
                    "kind": {"type": "string", "enum": ["once", "daily"]},
                    "timezone": {"type": "string", "description": "Explicit IANA timezone, e.g. Europe/Moscow."},
                    "deadline_local": {"type": "string", "description": "once: local 'YYYY-MM-DD HH:MM'."},
                    "daily_time": {"type": "string", "description": "daily: local 'HH:MM'."},
                    "grace_minutes": {"type": "integer", "minimum": 1, "maximum": 1440,
                                      "description": "Wait after the confirmed reminder before contact (default 60)."},
                    "lateness_cutoff_minutes": {"type": "integer", "minimum": 15, "maximum": 4320,
                                                "description": "Wakes later than this only tell the owner (default 360)."},
                    "contact_guidance": {"type": "string", "description": "Owner's words on what the contact may/may not be told."},
                    "owner_request": {"type": "string", "description": "The owner's own words asking for this "
                                      "agreement, quoted from the message that started this turn."},
                }})
    api.register_tool("wake_registered", _guard(t_wake_registered, owner_turn=False), timeout_sec=20, description=(
        "Report the schedule id you just registered with schedule_followup for a deadline or grace wake. Recorded "
        "as agent-reported, not verified."),
        schema={"type": "object", "additionalProperties": False, "required": ["kind", "schedule_id", "agreement_id"],
                "properties": {"kind": {"type": "string", "enum": ["deadline", "grace"]}, "schedule_id": text_prop,
                               "agreement_id": text_prop, "episode_id": {"type": "string", "description": "grace only"}}})
    api.register_tool("due", _guard(t_due, owner_turn=False), timeout_sec=45, description=(
        "Call first in a 'Check-in wake' task. Records the wake; if a deadline was missed it notifies the owner "
        "itself and returns the next step; for a grace wake it may open the one-time contact window."),
        schema={"type": "object", "additionalProperties": False, "required": ["wake_kind"],
                "properties": {"wake_kind": {"type": "string", "enum": ["deadline", "grace"]},
                               "agreement_id": text_prop,
                               "episode_id": {"type": "string", "description": "grace: the episode_id in the objective"}}})
    api.register_tool("checkin", _guard(t_checkin, owner_turn=True, owner_words="owner_message"), timeout_sec=20,
                      description=(
        "Record the owner's explicit check-in ('I'm fine', 'I'm back') in a turn the owner started, quoting their "
        "words from that message. Never on your own inference (activity is not a check-in) and never from a "
        "scheduled wake. Ordinary: omit both agreement_id and for_deadline (an empty value counts as given and "
        "is refused); it counts for TODAY's deadline while it is still ahead (status "
        "agreement.today_deadline) and closes anything already missed; it never answers a later day's deadline, "
        "and a repeat changes nothing. Dated: give BOTH agreement_id and for_deadline, exactly as status shows them "
        "in agreement.nearest_deadline (for example tomorrow's, early) or latest_missed, and only after the owner "
        "named or confirmed that exact deadline (tell them its label); a repeat changes nothing, and a stale or "
        "partial target is refused without any change. Tell the owner the deadline the result names."),
        schema={"type": "object", "additionalProperties": False, "required": ["owner_message"],
                "properties": {
                    "owner_message": {"type": "string", "description": "The owner's own words, quoted from the "
                                      "message that started this turn."},
                    "agreement_id": {"type": "string", "description": "Dated check-in only: the current agreement's "
                                     "id from status, together with for_deadline. Omit both for an ordinary "
                                     "check-in; never send an empty value."},
                    "for_deadline": {"type": "string", "description": "Dated check-in only: the exact UTC deadline "
                                     "from status (due_utc), e.g. 2026-10-06T19:00:00+00:00. Omit both for an "
                                     "ordinary check-in."},
                }})
    api.register_tool("control", _guard(t_control, owner_turn=True, owner_words="owner_request"), timeout_sec=20,
                      description=(
        "Owner's explicit request in a turn they started: pause (daily only, until_local 'YYYY-MM-DD HH:MM'; turns "
        "the contact stage off), resume, cancel (not a check-in), arm_contact, disarm_contact. Quote the owner's "
        "words from that message."),
        schema={"type": "object", "additionalProperties": False, "required": ["action", "owner_request"],
                "properties": {"action": {"type": "string",
                                          "enum": ["pause", "resume", "cancel", "arm_contact", "disarm_contact"]},
                               "owner_request": text_prop, "until_local": text_prop}})
    api.register_tool("send_contact", _guard(t_send_contact, owner_turn=False), timeout_sec=180, description=(
        "Only after due returned action=contact_window in this grace wake: send your one message to the "
        "configured contact (the recipient is fixed; you cannot choose it). One attempt per streak, never "
        "retried. Facts only, no diagnosis or claims of danger."),
        schema={"type": "object", "additionalProperties": False,
                "required": ["episode_id", "agreement_revision", "subject", "body", "reason"],
                "properties": {"episode_id": text_prop, "agreement_revision": {"type": "integer"},
                               "subject": {"type": "string", "maxLength": mailer.MAX_SUBJECT},
                               "body": {"type": "string", "maxLength": MAX_AGENT_BODY,
                                        "description": "Plain text; a fixed footer is appended."},
                               "reason": {"type": "string", "description": "Why you decided to write now."}}})
    api.register_tool("decline_contact", _guard(t_decline, owner_turn=False), timeout_sec=20, description=(
        "Only in the grace wake that opened the contact window: decide not to contact anyone for this streak, "
        "naming the concrete evidence (for example the owner wrote after the reminder) or the owner's guidance."),
        schema={"type": "object", "additionalProperties": False, "required": ["episode_id", "agreement_revision", "reason"],
                "properties": {"episode_id": text_prop, "agreement_revision": {"type": "integer"}, "reason": text_prop}})

    # ---- owner routes (server process, owner session)

    def route(fn: Callable[[Dict[str, Any], str], Dict[str, Any]]) -> Callable[..., Any]:
        """One owner route. The host also serves HEAD wherever GET is declared, so every method
        other than POST only reads (the handlers branch on ``method != "POST"``). A POST must
        carry one JSON object: anything else is refused before the handler runs, never read as
        ``{}`` (which ``checkin`` would take as an ordinary check-in)."""
        async def handler(request: Any) -> JSONResponse:
            method = str(request.method or "").upper()
            body: Any = {}
            if method == "POST":
                try:
                    body = await request.json()
                except Exception:
                    body = None
                if not isinstance(body, dict):
                    return JSONResponse({"error": "The request body must be one JSON object; nothing was changed.",
                                         "code": "body_invalid"}, status_code=409)
            try:
                payload = await asyncio.to_thread(fn, body, method)
            except CheckinError as exc:
                return JSONResponse({"error": exc.message, "code": exc.code}, status_code=409)
            except Exception as exc:  # typed name only: no request data or secrets in the log
                api.log("warning", f"check-in route {method} failed: {type(exc).__name__}")
                return JSONResponse({"error": "Check-in could not complete this request; see the server log."},
                                    status_code=500)
            return JSONResponse(payload)
        return handler

    def r_status(_body: Dict[str, Any], _method: str) -> Dict[str, Any]:
        return widget_view(store.status(now=_now(), lease_alive=alive))

    def r_checkin(body: Dict[str, Any], method: str) -> Dict[str, Any]:
        """GET (and HEAD): the Settings form's values. POST without any target field: an ordinary
        check-in. POST with any target field: a dated check-in of the deadline the Check-in card
        or the Settings form displayed, which needs all three (agreement, exact UTC deadline and
        the label it was displayed with)."""
        if method != "POST":
            return store.checkin_form(now=_now())
        if not any(key in body for key in _TARGET_FIELDS):
            return store.checkin(source="route", note="I'm here (no target)", now=_now())
        if not any(str(body.get(key) or "").strip() for key in _TARGET_FIELDS):
            raise CheckinError("nothing_to_check_in", "This form showed no deadline when Settings loaded, so "
                                                      "nothing was checked in. Reload Settings → Check-in.")
        # Missing fields become empty text, never "absent": a partial form is refused, not ordinary.
        target = {key: str(body.get(key) or "") for key in _TARGET_FIELDS}
        return store.checkin(source="dated", note="Check in for a displayed deadline (widget or Settings)", now=_now(),
                             agreement_id=target["agreement_id"], for_deadline=target["for_deadline"],
                             expect_label=target["deadline_label"])

    def r_resume(_body: Dict[str, Any], _method: str) -> Dict[str, Any]:
        return store.resume(source="widget", now=_now())

    def r_pause(body: Dict[str, Any], _method: str) -> Dict[str, Any]:
        result = store.pause(until_local=body.get("until_local"), source="widget", now=_now())
        result["message"] += " Ouroboros keeps its wake schedule; wakes during the pause do nothing."
        return result

    def r_cancel(body: Dict[str, Any], _method: str) -> Dict[str, Any]:
        if body.get("confirm") is not True:
            raise CheckinError("confirm_required", "Tick the box to confirm cancelling the agreement.")
        result = store.cancel(source="widget", now=_now())
        result["message"] += " Ask Ouroboros to delete the scheduled wakes, or they will just report “no agreement”."
        return result

    def r_arm(body: Dict[str, Any], _method: str) -> Dict[str, Any]:
        if body.get("confirm") is not True:
            raise CheckinError("confirm_required", "Tick the box to confirm arming the contact stage.")
        return store.arm(source="widget", statement="Armed from the Check-in widget", now=_now(), lease_alive=alive)

    def r_disarm(_body: Dict[str, Any], _method: str) -> Dict[str, Any]:
        return store.disarm(source="widget", now=_now())

    def r_contact(body: Dict[str, Any], method: str) -> Dict[str, Any]:
        if method != "POST":
            return store.contact_form()
        return store.save_contact(name=body.get("contact_name"), email=body.get("contact_email"),
                                  consent=body.get("contact_consent") is True,
                                  remove=body.get("remove_contact") is True, now=_now())

    def r_mail(body: Dict[str, Any], method: str) -> Dict[str, Any]:
        if method != "POST":
            return store.smtp_form()
        return store.save_smtp(host=body.get("smtp_host"), port=body.get("smtp_port"),
                               security=body.get("smtp_security"), username=body.get("smtp_username"),
                               password=body.get("smtp_password"), clear_password=body.get("clear_password") is True,
                               from_addr=body.get("smtp_from"), now=_now())

    api.register_route("status", route(r_status), methods=("GET",))
    # GET serves the Settings form's current values (the host reads a form's route on each Settings load).
    api.register_route("checkin", route(r_checkin), methods=("GET", "POST"))
    for path, fn in (("resume", r_resume), ("pause", r_pause), ("cancel", r_cancel), ("arm", r_arm),
                     ("disarm", r_disarm)):
        api.register_route(path, route(fn), methods=("POST",))

    def r_mail_test(_body: Dict[str, Any], _method: str) -> Dict[str, Any]:
        """Only when the owner presses the button: TLS + EHLO + login on the SAVED settings, then QUIT."""
        config = store.mail_test_config()
        result = mailer.test_connection(host=config["host"], port=config["port"], security=config["security"],
                                        username=config["username"], password=config["password"])
        state, code = str(result["state"]), str(result["code"])
        store.record_mail_test(revision=config["revision"], state=state, code=code, now=_now())
        if state != "ok":
            smtp = f", SMTP {result['smtp_code']}" if result.get("smtp_code") else ""
            raise CheckinError("mail_test_failed", f"Mail server test failed ({code}{smtp}). Nothing was sent.")
        login = "logged in" if code == "logged_in" else "connected (no username saved, so no login)"
        return {"message": f"Mail server test OK: verified TLS to {config['host']}:{config['port']}, {login}. "
                           "Nothing was sent; this checks the connection, not delivery."}

    api.register_route("settings/contact", route(r_contact), methods=("GET", "POST"))
    api.register_route("settings/mail", route(r_mail), methods=("GET", "POST"))
    api.register_route("settings/mail/test", route(r_mail_test), methods=("POST",))
    api.register_ui_tab("checkin", "Check-in", icon="🫶", render=WIDGET_RENDER)
    api.register_settings_section("checkin", "Check-in", schema=SETTINGS_SCHEMA)

    # ---- activation lease (started by the host only in the server process)

    def announce_restart(text: str) -> None:
        """The one owner notice that a restart turned an armed contact stage off.

        ``store.activate`` already claimed it, so whatever happens here it is never posted again;
        an unknown or refused outcome is only recorded. Runs on its own thread: the lease and the
        task's cancellation never wait for this bounded request.
        """
        try:
            outcome = host.post_notice(api.get_skill_token(), text, timeout=10.0)["outcome"]
        except Exception:  # a missing token: nothing was sent
            outcome = "refused"
        try:
            store.record_restart_notice(outcome=str(outcome), now=_now())
        except Exception as exc:
            api.log("warning", f"check-in restart notice outcome not recorded: {type(exc).__name__}")

    async def hold_activation() -> None:
        current = lease.Lease(lease_path)
        holder["lease"] = current
        bound = None
        try:
            for _ in range(40):
                if current.try_acquire():
                    break
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError(f"check-in activation lease could not be taken ({current.last_error or 'unknown'})")
            # The new epoch's own lock is held BEFORE the epoch is recorded: until then the state
            # still names the old epoch, whose lock nobody holds, so nothing bound to it passes.
            epoch = secrets.token_hex(8)
            lease.remove_epoch_locks(state_dir)
            bound = lease.Lease(lease.epoch_lock_path(state_dir, epoch))
            holder["epoch_lease"] = bound
            if not bound.try_acquire():
                raise RuntimeError(f"check-in epoch lease could not be taken ({bound.last_error or 'unknown'})")
            started = await asyncio.to_thread(store.activate, epoch, os.getpid(), _now())
            api.log("info", "check-in activation lease held")
            if started.get("announce"):
                threading.Thread(target=announce_restart, args=(started["text"],), name="check-in-restart-notice",
                                 daemon=True).start()
            await asyncio.Event().wait()  # no loop: hold until the host cancels this task
        finally:
            if bound is not None:
                bound.release()           # first: the recorded epoch stops being vouched for
            current.release()

    def release_activation() -> None:
        for key in ("epoch_lease", "lease"):
            current = holder.pop(key, None)
            if current is not None:
                current.release()

    if lease.supported():
        api.register_supervised_task("activation", hold_activation, restart_policy="on_failure", max_restarts=3,
                                     backoff_seconds=2.0)
    api.on_unload(release_activation)
