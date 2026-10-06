---
name: check-in
description: Opt-in check-in agreement. If you miss a check-in you promised, Ouroboros first tells you; only if you armed it may it then write once to one consenting contact.
version: 0.1.4
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [tool, route, widget, net, supervised_task, notify_owner]
timeout_sec: 120
when_to_use: >-
  The owner asks Ouroboros to watch over an explicit check-in promise ("if I
  don't check in by Sunday 18:00", "check on me every day by 21:00"), to check
  in, pause, cancel, or to arm/disarm the optional message to one trusted
  contact. Also when a scheduled "Check-in wake" task starts.
model_experience:
  what_model_sees: >-
    Eight check-in tools (status, setup, wake_registered, due, checkin, control,
    send_contact, decline_contact) with compact JSON results that always name
    the next step. Wakes are ordinary scheduled root tasks created with the
    built-in schedule_followup; their objective names the `due` tool.
  token_effect: >-
    Eight small tool schemas while enabled. A wake costs one short model turn
    (usually one or two tool calls); a daily agreement costs one such turn per
    day plus at most one grace turn per missed streak.
---

# Check-in

A care agreement the owner sets up explicitly: "I will check in by this
time". If the check-in does not happen, Ouroboros tells the owner. If — and
only if — the owner configured one contact who agreed, and armed the contact
stage, a live Ouroboros turn may afterwards write **one** message to that
contact. The agent decides whether to write and what to say, within the limits
below; the code only enforces recipient, timing, revision and attempt limits.

This is **not** a medical, emergency or safety service. Silence is not proof
of danger, and a sent message is not proof that anyone read it. While the
computer running Ouroboros is asleep or off, or when no model, budget or mail
server is available, nothing happens — there is no guarantee of any action.

Idea and original proposal: @Glassscale in razzant/ouroboros#1221 (see
`ATTRIBUTION.md`).

## What counts as a check-in

Only an explicit act by the owner, of one of two kinds:

- **Ordinary** — the owner writes to Ouroboros ("I'm here", "I'm fine") and
  the agent, in that same turn, calls `checkin` quoting the owner's words
  from that message. It counts for **today's** deadline (the one on today's
  date in the agreement's timezone) while that deadline is still ahead, and
  it closes anything already missed: the open missed-check-in streak and a
  deadline that passed before any wake processed it. It never answers a
  later day's deadline, and a repeat changes nothing. Missed Monday 21:00:
  "I'm back" at 21:10 closes Monday; again at 21:11 answers nothing
  (Tuesday 21:00 is still due); "I'm here" on Tuesday at 20:00 counts for
  Tuesday 21:00. For a deadline shortly after midnight, an ordinary check-in
  counts for it only from midnight on.
- **Dated** — one exact deadline of the current agreement, chosen by the
  owner: the Check-in card's buttons, Settings → Check-in → **Check in for
  displayed deadline** (the form shows the nearest deadline when Settings
  loads), or in chat once the owner names or confirms that deadline
  (`checkin` with `agreement_id` and `for_deadline`, see the agent
  workflow). It can name only the next deadline (at or after now) or the
  latest one that passed. It answers that deadline and closes any missed
  check-in before it; the deadline after it still stands. Repeating it
  changes nothing. A target of a replaced, cancelled or completed agreement,
  a deadline that is no longer one of those two, an incomplete pair, or a
  label that does not match is refused before anything is written.

The **Check-in card** (Widgets) shows today's deadline. Its **I'm here**
button names the deadline it checks in for (today's, or a missed one) and
sends exactly that deadline. It never moves to another deadline by itself:
after a submit it keeps the same deadline (pressing again changes nothing),
and **Refresh** moves it to what the status shows now. Once today's deadline
is done, **Check in early for the next deadline** asks for confirmation
first; like any dated check-in it also closes a check-in missed before that
deadline, and the confirmation names the missed one the status shows. A
refused or unanswered request is shown and never retried.

Ordinary activity, task Stop, cancelling or pausing the agreement, disabling
the skill and Panic are **never** counted as "the owner is fine".

## Agreements

- **once** — one deadline: local `YYYY-MM-DD HH:MM` in an explicit IANA
  timezone. A check-in (ordinary or dated) before or after it completes the
  agreement.
- **daily** — a local `HH:MM` every day in an explicit IANA timezone. An
  ordinary check-in counts for today's deadline while it is ahead; a later
  day's deadline needs a dated check-in. A check-in after a missed deadline
  answers the missed one and closes the streak; the next deadline still
  stands, and checking in again changes nothing (missed 21:00, "I'm back" at
  21:10, again at 21:11 → tomorrow 21:00 is still due). Deadlines are shown
  with date, timezone, weekday and UTC offset, for example `2026-10-06 21:00
  Europe/Berlin (Tue, UTC+02:00)`. A time inside a daylight-saving gap is
  moved forward by the gap; an ambiguous time uses its first occurrence —
  `setup` warns about both, and a dated check-in names that same instant.
- `grace_minutes` (1–1440): after the owner was actually notified, how long to
  wait before the contact stage may open. Grace starts at the confirmed notice
  time, never at the old deadline. It is planned **once per missed streak**: by
  the first confirmed, on-time reminder (see the cutoff below) that finds the
  contact stage armed. Reminders for
  later deadlines of the same streak still reach the owner, but never move,
  re-plan or reopen that grace (with a long grace, tomorrow's reminder can
  arrive before it ends; the grace wake still opens on time).
- `lateness_cutoff_minutes` (15–4320): a wake arriving later than this after
  the **oldest deadline it answers** only tells the owner that it was noticed
  late; it never contacts anyone and never replays missed days in a burst.
  This is deliberately conservative: if an earlier deadline got no wake at all
  (for example the computer was asleep at Monday's deadline), Tuesday's wake is
  a late catch-up even when it fires on time, so nobody is contacted for it;
  the next deadline after that is judged on its own. Such a wake still
  records every deadline it found missed: the streak runs from the oldest to
  the newest, and **I'm here** names the newest.
- One active agreement at a time; a new `setup` replaces the old one and turns
  the contact stage off.

Several missed daily deadlines in a row are **one streak**: at most one
contact attempt per streak, until the owner checks in (or replaces the
agreement and arms again). **Pause** (daily only) is not a check-in: it ends
the streak and turns the contact stage off, so after the pause nobody is
contacted unless the owner arms again. A dated check-in is refused during a
pause. Pause and resume never move the next deadline earlier, so a deadline
answered early stays answered.

## Contact stage (optional, off by default)

1. Owner, in Settings → Check-in: one contact (name + email) with the box
   confirming the contact agreed, and their own SMTP server (implicit TLS or
   STARTTLS, certificate verified; password write-only).
2. Owner arms it: the Check-in card's **Arm contact stage**, or by writing to
   Ouroboros (agent: `control` action `arm_contact` quoting the owner's words
   from that message). Arming is refused without a consenting contact and a
   mail server, while a check-in is missed or a deadline has passed that no
   wake has processed yet (check in first), and during a pause (arm again
   after it ends).
   **Test saved mail server** in Settings → Check-in connects with a verified
   TLS certificate and logs in, then disconnects: no recipient, nothing sent.
   It checks connectivity and login, not delivery; it runs only when the owner
   presses it, and saving the server again clears the result.
3. Arming is tied to this agreement, this contact revision and this
   Ouroboros activation. Changing the contact, pausing, replacing or
   cancelling the agreement, disabling/re-enabling the skill or restarting
   Ouroboros turns it off; it is never re-armed silently. When a restart or
   reload turns an armed stage off, the skill makes one best-effort attempt
   at an owner notice about it (claimed before the request, at most once per
   arm, never retried; at startup the host may not accept it yet, so it can
   stay unconfirmed). The widget is the reliable view. A
   message already claimed is re-checked against exactly that binding before
   DATA and again right after the mail server answers 354, immediately before
   its text is sent; if that check fails, the connection is dropped and the
   text never leaves.

## Agent workflow

Tool names below are short. Their registered names are
`ext_10_r_check-in_<name>` — for example `ext_10_r_check-in_due`,
`ext_10_r_check-in_setup` (load them with `enable_tools` if they are not in
your tool list).

**Owner actions** (`setup`, `checkin`, `control`) work only in a turn the
owner started with their own message: the host stamps such a turn, and the
`owner_request` / `owner_message` you pass must quote words from that
message. Scheduled wakes, background-consciousness and API-submitted tasks,
delegated subagents and Presence conversations are refused. This checks
provenance, not intent: never call them on your own inference.

**Setup.** Ask for anything missing — kind, time, timezone (never guess it),
grace, what the contact may and may not be told. Then, in the turn where the
owner confirms:

1. `setup(...)`. It returns `wake_plan`.
2. Call the built-in `schedule_followup` with exactly the `wake_plan`
   arguments (`relation` is `independent` on purpose: the wake must not
   inherit this conversation's budget or Stop).
3. `wake_registered(kind="deadline", schedule_id=<id from FOLLOWUP_SCHEDULED>,
   agreement_id=...)`. This is your report, not a verification; the widget
   says so.
4. If `setup` listed `cleanup_schedule_ids`, delete those rows with
   `manage_schedules(action="delete", ...)`.
5. Tell the owner the first deadline, the limits above, and that the contact
   stage is off until armed.

**Deadline wake** (a task whose objective starts with "Check-in wake
(deadline)"): call `due(wake_kind="deadline", agreement_id=...)`. The tool
records the arrival and, if the deadline was missed, notifies the owner itself
through the host notice. Follow `next_step`: if it contains a
`schedule_followup` plan, register exactly that one-shot grace wake and report
it with `wake_registered(kind="grace", ...)`. Otherwise do nothing more.

**Grace wake** ("Check-in wake (grace)"): call `due(wake_kind="grace",
agreement_id=..., episode_id=...)` with both ids from the objective. Only the
one-shot schedule reported for this exact streak, firing at its grace end,
can open the window; a wake from anything else, or for an earlier streak,
gets `action: "none"`. Only `action: "contact_window"` allows contact, and
only in this same wake task. Then decide:

- Write with `send_contact(episode_id, agreement_revision, subject, body,
  reason)` — or
- `decline_contact(episode_id, agreement_revision, reason)` when you have
  concrete evidence the owner is present (for example, a message from the
  owner after the notice time in your own context) or the owner's guidance
  says not to. Name the evidence in `reason`.

**Writing to the contact.** Use the owner's `contact_guidance`, and write in
the language it asks for or the contact will understand. State only facts the
tool gave you: there was a check-in agreement, its deadline passed, the owner
was reminded at the given time and has not checked in since. Say that you are
Ouroboros, the AI agent the owner set this up with. Do not diagnose, do not
suggest danger or that the silence is unusual, do not invent personal facts,
do not quote the owner's private conversations, do not promise further
messages (the wake objective repeats this). Never contact anyone else and
never use another email or messaging tool for this purpose — `send_contact`
is the only path, and it is bound to the configured contact. The tool
appends a short fixed English footer saying the message is not an emergency
service, so the body you write may be at most 3,821 characters; the whole
message is checked before the attempt is claimed, so a refused text does not
use up the attempt. Fixed owner notices are plain English too.

**After sending.** One attempt per streak, never retried automatically. An
`uncertain` result means the message may or may not have been delivered; do
not send again. A message handed to the mail server cannot be recalled.

**Check-in in chat** (owner-started turn only, see above).
`checkin(owner_message)` is the ordinary check-in: it counts for
`agreement.today_deadline` while that is ahead and closes anything missed.
The result names the deadline it answered (or that it answered nothing new)
and the next one; tell the owner exactly that. When the owner wants to check
in early for a later day's deadline ("I'll be offline all tomorrow"), read
`status` and tell the owner the exact `agreement.nearest_deadline.label`.
Only when the owner names or confirms that deadline in their own message,
call `checkin(owner_message, agreement_id, for_deadline)` with `agreement_id`
and `due_utc` from that same `nearest_deadline` (or, for a late check-in,
`latest_missed`). Give both; for an ordinary check-in omit both (an empty
value counts as given and is refused). Never choose a deadline yourself.
A repeat answers nothing new. On `target_stale`, `agreement_mismatch` or
`target_invalid` nothing changed: read `status` again and ask.

**Owner controls** (owner-started turn only, see above). `control` with
`pause` (daily only, until a local time; turns the contact stage off),
`resume`, `cancel`, `arm_contact`, `disarm_contact`. Pause and resume keep
the existing daily wake (wakes during a pause do nothing), so nothing is
rescheduled. After `cancel`, delete the registered wake rows with
`manage_schedules`. The widget offers the same controls to the owner without
any agent (the Check-in card is a small module widget: it reads the
`status` route and posts only to this skill's own routes). Its **Last
contact attempt** row (and `for_open_streak` in
`status.last_attempt`) says whether the newest attempt is about the
check-in missed now or an earlier one.

## Stop, cancel, disable, Panic

- **Stop** on a wake task stops that task only; the agreement and the streak
  stay. A stopped (or deleted, or too late) grace wake is not re-scheduled —
  not even by the next day's reminder — so nobody is contacted for that
  streak; after the owner's next check-in a new streak starts fresh.
- **Cancel agreement** ends the agreement and the contact stage; it is not a
  check-in.
- **Disabling the skill** removes the tools and widget; scheduled wakes still
  start and must report that the skill is unavailable.
- **Panic** stops everything; after restart the contact stage is off until
  re-armed. This skill never turns Background Consciousness on and never
  restarts Ouroboros: it describes the agreement, the host decides the rest.
