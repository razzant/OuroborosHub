"""Check-in state: one SQLite file shared by the server and every task worker.

Every mutation runs inside ``BEGIN IMMEDIATE``, so admission decisions (a notice
claim, a contact attempt claim, cancel, check-in) are serialized across processes.
Times are stored as UTC ISO strings; ``now`` is always passed in by the caller.

Vocabulary
----------
* agreement — the owner's promise (``once`` deadline or ``daily`` time) in an
  explicit IANA timezone. ``revision`` changes on setup/pause/resume/cancel.
* episode — one missed-check-in streak. Several missed daily deadlines in a row
  stay ONE episode (``first_due_utc`` the oldest, ``last_due_utc`` the newest, also
  when one late wake finds several); it closes on check-in, cancel, pause or replacement.
* check-in — an ordinary check-in counts for TODAY's deadline (the one on the same local
  date, in the agreement's timezone) while that deadline is still ahead, and closes what
  was already missed (an open episode or a passed deadline no wake has processed). It
  never answers a deadline of a later day. A dated check-in names the current agreement
  and one exact deadline (the latest that passed or the next one, as ``status`` shows
  it). Either answers a deadline once: ``last_answered_due_utc`` records the latest
  answered deadline, so a repeat changes nothing.
* notice — the owner notice for one missed deadline (host ``/notify``); only a
  confirmed notice starts grace, measured from the confirmed notice time.
* grace — planned at most ONCE per episode: the first confirmed notice that finds
  the contact stage armed records ``contact_notice_at`` and ``grace_ends_at``.
  Later notices of the same streak (the next daily deadlines) are still sent to the
  owner but never move, re-plan or reopen that grace, so a stopped, deleted or late
  grace wake means no contact for the streak.
* contact stage — ``arm`` ties the optional contact message to the current
  agreement, contact revision and server activation epoch, under a fresh
  ``arm_id``. Arming is refused while a streak is open, and pause, cancel,
  replacement, a contact change or a restart turn it off. At most one attempt per
  episode; an attempt is claimed before any network I/O, records the epoch,
  contact revision and ``arm_id`` it was admitted under, and is never retried.
* lease — whether the holder of the RECORDED activation epoch is alive. The server
  passes a probe of that epoch's own lock (``lease.epoch_alive``), which runs inside
  the write transaction with the epoch read there, so no activation commits between
  the read and the probe; a new holder never vouches for an earlier epoch. Tests may
  pass a plain bool instead ("the recorded epoch's holder is alive").
* grace wake — only the one-shot schedule the agent reported for this exact
  episode, firing at this episode's grace end, may open the contact window, and
  only the task that opened it (its schedule id and task id, both recorded) may
  re-enter it, send or decline.
"""

from __future__ import annotations

import os
import pathlib
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import mailer

UTC = timezone.utc
# ``lease_alive``: a probe of the recorded epoch's holder (the server), or a plain bool (tests).
LeaseProbe = Union[bool, Callable[[str], bool]]
SCHEMA_VERSION = "4"
GRACE_RANGE = (1, 1440)
CUTOFF_RANGE = (15, 4320)
MAX_AHEAD = timedelta(days=90)
MIN_AHEAD = timedelta(seconds=60)
WAKE_OVERDUE = timedelta(minutes=10)
ATTEMPT_UNKNOWN_AFTER = timedelta(minutes=10)
# How far the host-recorded due time of a grace wake may be from this episode's grace end.
GRACE_DUE_TOLERANCE = timedelta(seconds=120)
TERMINAL_CONTACT = ("attempted", "declined")
# Registered tool names are ``ext_<len>_<token>_<name>``; for the skill "check-in" that is this
# prefix (``extension_surface_name("check-in", name)`` in the core; a test pins the equality).
TOOL_PREFIX = "ext_10_r_check-in_"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agreement (
  id TEXT PRIMARY KEY, status TEXT NOT NULL, kind TEXT NOT NULL, timezone TEXT NOT NULL,
  deadline_utc TEXT, daily_time TEXT, next_due_utc TEXT,
  grace_minutes INTEGER NOT NULL, cutoff_minutes INTEGER NOT NULL,
  guidance TEXT NOT NULL, owner_request TEXT NOT NULL,
  revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  paused_until TEXT, last_checkin_at TEXT, ended_at TEXT, end_reason TEXT, last_answered_due_utc TEXT);
CREATE TABLE IF NOT EXISTS episode (
  id TEXT PRIMARY KEY, agreement_id TEXT NOT NULL, agreement_revision INTEGER NOT NULL,
  state TEXT NOT NULL, opened_at TEXT NOT NULL, first_due_utc TEXT NOT NULL,
  last_due_utc TEXT NOT NULL, missed_count INTEGER NOT NULL,
  notice_state TEXT NOT NULL, notice_due_utc TEXT, notice_stale INTEGER NOT NULL DEFAULT 0,
  notice_claimed_at TEXT, notice_at TEXT, notice_detail TEXT,
  contact_state TEXT NOT NULL, contact_notice_at TEXT, grace_ends_at TEXT, window_opened_at TEXT,
  window_schedule_id TEXT, window_task_id TEXT,
  decision_reason TEXT, closed_at TEXT, close_reason TEXT);
CREATE TABLE IF NOT EXISTS attempt (
  id TEXT PRIMARY KEY, episode_id TEXT NOT NULL UNIQUE, agreement_id TEXT NOT NULL,
  agreement_revision INTEGER NOT NULL, state TEXT NOT NULL, claimed_at TEXT NOT NULL,
  finished_at TEXT, pid INTEGER, epoch TEXT, contact_revision TEXT, arm_id TEXT,
  schedule_id TEXT, task_id TEXT, recipient_label TEXT,
  subject TEXT, body TEXT, reason TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS wake (
  id INTEGER PRIMARY KEY AUTOINCREMENT, arrived_at TEXT NOT NULL, kind TEXT NOT NULL,
  agreement_id TEXT, task_id TEXT, schedule_id TEXT, matched_registration INTEGER NOT NULL,
  outcome TEXT);
CREATE TABLE IF NOT EXISTS registration (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, schedule_id TEXT NOT NULL,
  agreement_id TEXT NOT NULL, episode_id TEXT, reported_at TEXT NOT NULL, task_id TEXT);
CREATE TABLE IF NOT EXISTS event (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
"""


class CheckinError(Exception):
    """A refused request; ``code`` is stable, ``message`` is for the model and owner."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- time

def iso(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    value = datetime.fromisoformat(str(text))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def zone(name: object) -> ZoneInfo:
    text = str(name or "").strip()
    if not text or "/" not in text and text != "UTC":
        raise CheckinError("timezone_invalid", "timezone must be an explicit IANA name such as Europe/Moscow")
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        raise CheckinError("timezone_invalid", f"unknown IANA timezone {text!r}") from None


def local_kind(naive: datetime, tz: ZoneInfo) -> str:
    """``ok``, ``gap`` (does not exist that day) or ``ambiguous`` (happens twice)."""
    first, second = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
    if first.utcoffset() == second.utcoffset():
        return "ok"
    back = first.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    return "gap" if back != naive else "ambiguous"


def local_instant(day: date, hh: int, mm: int, tz: ZoneInfo) -> datetime:
    """The UTC instant of a local wall time; gap times move forward, ambiguous take the first."""
    naive = datetime(day.year, day.month, day.day, hh, mm)
    return naive.replace(tzinfo=tz, fold=0).astimezone(UTC)


def daily_at_or_after(t: datetime, tz: ZoneInfo, hh: int, mm: int) -> datetime:
    start = t.astimezone(tz).date()
    for offset in range(-1, 4):
        candidate = local_instant(start + timedelta(days=offset), hh, mm, tz)
        if candidate >= t:
            return candidate
    raise AssertionError("no daily occurrence found")  # pragma: no cover


def daily_after(t: datetime, tz: ZoneInfo, hh: int, mm: int) -> datetime:
    return daily_at_or_after(t + timedelta(seconds=1), tz, hh, mm)


def daily_before(t: datetime, tz: ZoneInfo, hh: int, mm: int) -> datetime:
    """The latest daily occurrence strictly before ``t``."""
    start = t.astimezone(tz).date()
    for offset in range(1, -4, -1):
        candidate = local_instant(start + timedelta(days=offset), hh, mm, tz)
        if candidate < t:
            return candidate
    raise AssertionError("no daily occurrence found")  # pragma: no cover


def is_daily_occurrence(t: datetime, tz: ZoneInfo, hh: int, mm: int) -> bool:
    """Whether ``t`` is exactly one of the daily deadlines (gap moved forward, first of a repeat)."""
    day = t.astimezone(tz).date()
    return any(local_instant(day + timedelta(days=k), hh, mm, tz) == t for k in (-1, 0, 1))


def parse_hhmm(text: object) -> Tuple[int, int]:
    raw = str(text or "").strip()
    try:
        parsed = datetime.strptime(raw, "%H:%M")
    except ValueError:
        raise CheckinError("time_invalid", "daily_time must be HH:MM (24-hour local time)") from None
    return parsed.hour, parsed.minute


def parse_local(text: object, field: str) -> datetime:
    raw = str(text or "").strip().replace("T", " ")
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        raise CheckinError("time_invalid", f"{field} must be local 'YYYY-MM-DD HH:MM'") from None


def _instant(text: object) -> Optional[datetime]:
    """A host-written ISO instant (``Z`` or an offset); None when absent or unparseable."""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return parse_iso(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        return None


def fmt_local(text: Optional[str], tz_name: str) -> str:
    value = parse_iso(text)
    if value is None:
        return ""
    return value.astimezone(zone(tz_name)).strftime("%Y-%m-%d %H:%M")


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def deadline_label(text: Optional[str], tz_name: str) -> str:
    """One deadline for people, unambiguous across clock changes: local date and time, the
    timezone, the weekday and the UTC offset — for example
    ``2026-10-06 21:00 Europe/Berlin (Tue, UTC+02:00)``. A dated check-in from Settings must
    send back exactly this text for the deadline it names."""
    value = parse_iso(text)
    if value is None:
        return ""
    local = value.astimezone(zone(tz_name))
    offset = int(local.utcoffset().total_seconds() // 60)
    sign, offset = ("+" if offset >= 0 else "-"), abs(offset)
    return (f"{local:%Y-%m-%d %H:%M} {tz_name} ({_WEEKDAYS[local.weekday()]}, "
            f"UTC{sign}{offset // 60:02d}:{offset % 60:02d})")


def _explicit_instant(text: str) -> Optional[datetime]:
    """An ISO instant that names its offset (``Z`` or ``+00:00``); None otherwise."""
    raw = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value.astimezone(UTC) if value.tzinfo is not None else None


def parse_target(agreement_id: object, for_deadline: object) -> Optional[Tuple[str, datetime]]:
    """``None`` for an ordinary check-in (neither given); the checked pair for a dated one.

    Giving either one makes it a dated check-in: an incomplete, empty or malformed pair is
    refused, never treated as an ordinary check-in — not even when both are empty, since a
    target that was meant but came through empty must not answer some other deadline.
    """
    if agreement_id is None and for_deadline is None:
        return None
    wanted = str(agreement_id or "").strip()
    raw = str(for_deadline or "").strip()
    if not wanted or not raw:
        raise CheckinError("target_incomplete", "A dated check-in needs both agreement_id and for_deadline (the "
                                                "exact UTC deadline that status shows); nothing was changed. For "
                                                "an ordinary check-in omit both (an empty value counts as given).")
    if len(wanted) > 64 or len(raw) > 64:
        raise CheckinError("target_invalid", "agreement_id or for_deadline is too long; nothing was changed.")
    instant = _explicit_instant(raw)
    if instant is None:
        raise CheckinError("target_invalid", "for_deadline must be the exact UTC instant that status shows, for "
                                             "example 2026-10-06T19:00:00+00:00; nothing was changed.")
    return wanted, instant


def _bounded_int(value: object, field: str, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise CheckinError("value_invalid", f"{field} must be a whole number {lo}-{hi}") from None
    if not lo <= number <= hi:
        raise CheckinError("value_invalid", f"{field} must be {lo}-{hi}")
    return number


def _text(value: object, field: str, limit: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise CheckinError("value_invalid", f"{field} is required")
    if len(text) > limit:
        raise CheckinError("value_invalid", f"{field} must be at most {limit} characters")
    return text


def mask_email(address: str) -> str:
    if "@" not in address:
        return ""
    local, domain = address.split("@", 1)
    return f"{local[:1]}***@{domain}"


# --------------------------------------------------------------------------- text

def wake_objective(kind: str, agreement_id: str, episode_id: str = "") -> str:
    ids = f"agreement_id='{agreement_id}'" + (f", episode_id='{episode_id}'" if episode_id else "")
    return (
        f"Check-in wake ({kind}) for {ids}. Call the Check-in tool `{TOOL_PREFIX}due` (if it is not in "
        f"your tool list, load it with enable_tools) with wake_kind='{kind}', {ids}, then follow the "
        "`next_step` it returns exactly. Do not contact anyone unless that tool opens a contact window. "
        "If you do write to the contact, state only the facts the tool returns: no diagnosis, no claim or "
        "hint of danger, no invented personal facts, no quotes from the owner's private conversations, no "
        "promise of further messages. If the Check-in tools are unavailable (the skill is disabled), stop "
        "and say so briefly. This is a care check-in, not an emergency service."
    )


# Fixed host notices are plain English and state only facts the store knows. The message to the
# contact is written by the agent, in whatever language the owner's guidance and the contact need.
_NOTICE_MISSED = ("Check-in missed: you agreed to check in by {due} ({tz}). Press “I'm here” in the Check-in "
                  "widget or tell Ouroboros you are fine. {contact}")
_CONTACT_ARMED = "If you don't, after about {grace} min Ouroboros may write once to {name}."
_CONTACT_USED = ("No new message to your contact for this deadline: this missed streak already had its one "
                 "contact decision.")
_CONTACT_OFF = "The contact stage is off: no message to anyone else for this deadline."
_NOTICE_LATE = ("Check-in deadline {due} ({tz}) passed and was noticed {late} late. No new contact is made "
                "for a deadline noticed this late. Please check in.")
_FOOTER = ("\n\n—\nSent automatically by Ouroboros (an AI agent) under a check-in agreement its owner set up. "
           "Not an emergency service. No further message about this missed check-in will follow.")
# The agent's text plus the fixed footer must fit the mailer's body limit.
MAX_AGENT_BODY = mailer.MAX_BODY - len(_FOOTER)
_CONTACT_PENDING = ("As planned after the first reminder of this missed streak, Ouroboros may still write once to "
                    "{name} after {at}.")
_CONTACT_PASSED = ("No new message to your contact for this deadline: this missed streak's one contact window was "
                   "already planned and has passed.")
_RESTART_NOTICE = ("Check-in: Ouroboros restarted or the skill reloaded, so the contact stage is now off. No message "
                   "will go to {name} about a missed check-in until you arm it again in the Check-in widget.")


def _duration(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} d"


# --------------------------------------------------------------------------- store

class Store:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)

    # ---- plumbing

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
            os.close(fd)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        with self._tx() as con:
            for statement in filter(None, (part.strip() for part in _SCHEMA.split(";"))):
                con.execute(statement)
            found = self._kv(con, "schema_version")
            if found and found not in ("2", "3", SCHEMA_VERSION):
                raise CheckinError("schema_mismatch", f"state schema {found} is not supported")
            if found == "2":
                self._migrate_v2(con)
            if found in ("2", "3"):
                self._migrate_v3(con)
            self._set(con, "schema_version", SCHEMA_VERSION)

    @staticmethod
    def _migrate_v2(con: sqlite3.Connection) -> None:
        """v2 → v3: keep the notice that started an episode's one grace apart from later notices.

        v2 re-planned grace from every confirmed notice, so an episode still ``waiting`` or in
        its ``window`` has ``grace_ends_at`` set from exactly its confirmed ``notice_at``; that
        observed time is copied. Everywhere else the time was not kept and stays unknown (NULL):
        it is never invented, and a grace wake for such an episode opens no window.
        """
        columns = {row["name"] for row in con.execute("PRAGMA table_info(episode)")}
        if "contact_notice_at" not in columns:
            con.execute("ALTER TABLE episode ADD COLUMN contact_notice_at TEXT")
        con.execute("UPDATE episode SET contact_notice_at=notice_at WHERE contact_notice_at IS NULL "
                    "AND grace_ends_at IS NOT NULL AND notice_state='confirmed' AND notice_at IS NOT NULL "
                    "AND contact_state IN ('waiting', 'window')")

    @staticmethod
    def _migrate_v3(con: sqlite3.Connection) -> None:
        """v3 → v4: remember the latest deadline a check-in answered.

        Earlier versions did not record it, so it stays unknown (NULL) for existing agreements:
        never inferred from ``next_due_utc``, which the deadline wake also advances.
        """
        columns = {row["name"] for row in con.execute("PRAGMA table_info(agreement)")}
        if "last_answered_due_utc" not in columns:
            con.execute("ALTER TABLE agreement ADD COLUMN last_answered_due_utc TEXT")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(str(self.path), timeout=15.0, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()

    @staticmethod
    def _kv(con: sqlite3.Connection, key: str, default: str = "") -> str:
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    @staticmethod
    def _set(con: sqlite3.Connection, key: str, value: object) -> None:
        con.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, "" if value is None else str(value)))

    @staticmethod
    def _event(con: sqlite3.Connection, now: datetime, kind: str, detail: str = "") -> None:
        con.execute("INSERT INTO event(ts, kind, detail) VALUES(?, ?, ?)", (iso(now), kind, detail[:300]))
        con.execute("DELETE FROM event WHERE id <= (SELECT MAX(id) FROM event) - 1000")

    @staticmethod
    def _current(con: sqlite3.Connection) -> Optional[sqlite3.Row]:
        return con.execute(
            "SELECT * FROM agreement WHERE status IN ('active','paused') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    @staticmethod
    def _open_episode(con: sqlite3.Connection, agreement_id: str) -> Optional[sqlite3.Row]:
        return con.execute("SELECT * FROM episode WHERE agreement_id=? AND state='open'", (agreement_id,)).fetchone()

    def _close_episode(self, con: sqlite3.Connection, agreement_id: str, reason: str, now: datetime) -> None:
        con.execute("UPDATE episode SET state='closed', closed_at=?, close_reason=? WHERE agreement_id=? AND state='open'",
                    (iso(now), reason, agreement_id))

    def _disarm(self, con: sqlite3.Connection, reason: str, now: datetime) -> None:
        if self._kv(con, "arm_armed") == "1":
            self._set(con, "arm_armed", "")
            self._event(con, now, "contact_stage_off", reason)

    def _maybe_resume(self, con: sqlite3.Connection, ag: Optional[sqlite3.Row], now: datetime) -> Optional[sqlite3.Row]:
        if ag is None or ag["status"] != "paused":
            return ag
        until = parse_iso(ag["paused_until"])
        if until is None or now < until:
            return ag
        next_due = self._due_after_pause(ag, until)
        con.execute("UPDATE agreement SET status='active', paused_until=NULL, revision=revision+1, next_due_utc=?, "
                    "updated_at=? WHERE id=?", (iso(next_due), iso(now), ag["id"]))
        self._event(con, now, "resumed", "pause ended")
        return con.execute("SELECT * FROM agreement WHERE id=?", (ag["id"],)).fetchone()

    @staticmethod
    def _due_after_pause(ag: sqlite3.Row, at: datetime) -> datetime:
        """The first deadline after ``at``, but never one already answered before the pause."""
        hh, mm = parse_hhmm(ag["daily_time"])
        computed = daily_after(at, zone(ag["timezone"]), hh, mm)
        kept = parse_iso(ag["next_due_utc"])
        return computed if kept is None else max(computed, kept)

    # ---- activation (written only by the server-side lease task)

    def activate(self, epoch: str, pid: int, now: datetime) -> Dict[str, Any]:
        """Record a new activation epoch (which turns any earlier arm off).

        Returns ``announce`` with the owner notice text when the contact stage was armed under
        the previous activation and no notice about that arm was claimed yet. The claim is
        written here, before any network I/O, so the caller posts that notice at most once per
        arm and never retries it (an unknown outcome stays unknown). Nothing is announced for a
        first activation or a stage that was already off.
        """
        with self._tx() as con:
            was_armed, _ = self._arm_state(con, self._current(con), True)   # still the old epoch here
            arm_id = self._kv(con, "arm_id")
            announce = was_armed and bool(arm_id) and self._kv(con, "restart_notice_arm_id") != arm_id
            self._set(con, "activation_epoch", epoch)
            self._set(con, "activation_pid", pid)
            self._set(con, "activation_started_at", iso(now))
            self._event(con, now, "activated", f"pid {pid}")
            if not announce:
                return {"announce": False}
            self._set(con, "restart_notice_arm_id", arm_id)
            self._set(con, "restart_notice_state", "claimed")
            self._event(con, now, "restart_notice_claimed", arm_id)
            return {"announce": True, "text": _RESTART_NOTICE.format(name=self._kv(con, "contact_name"))}

    def record_restart_notice(self, *, outcome: str, now: datetime) -> None:
        with self._tx() as con:
            self._set(con, "restart_notice_state", outcome)
            self._event(con, now, "restart_notice_" + outcome)

    # ---- configuration

    def save_contact(self, *, name: object, email: object, consent: bool, remove: bool, now: datetime) -> Dict[str, Any]:
        with self._tx() as con:
            revision = int(self._kv(con, "contact_revision", "0") or 0) + 1
            if remove:
                for key in ("contact_name", "contact_email", "contact_consent", "contact_consent_at"):
                    self._set(con, key, "")
                self._set(con, "contact_revision", revision)
                self._disarm(con, "contact removed", now)
                self._event(con, now, "contact_removed")
                return {"message": "Contact removed. The contact stage is off."}
            try:
                clean_name = mailer.clean_name(name)
                clean_email = mailer.clean_address(email)
            except mailer.MessageInvalid as exc:
                raise CheckinError("contact_invalid", f"contact {exc}") from None
            if not consent:
                raise CheckinError("consent_required", "Tick the box confirming this person agreed to be contacted.")
            self._set(con, "contact_name", clean_name)
            self._set(con, "contact_email", clean_email)
            self._set(con, "contact_consent", "1")
            self._set(con, "contact_consent_at", iso(now))
            self._set(con, "contact_revision", revision)
            self._disarm(con, "contact changed", now)
            self._event(con, now, "contact_saved", f"revision {revision}")
        return {"message": f"Saved contact {clean_name}. The contact stage is off until you arm it again."}

    def contact_form(self) -> Dict[str, str]:
        with self._tx() as con:
            return {"contact_name": self._kv(con, "contact_name"), "contact_email": self._kv(con, "contact_email")}

    def save_smtp(self, *, host: object, port: object, security: object, username: object, password: object,
                  clear_password: bool, from_addr: object, now: datetime) -> Dict[str, Any]:
        try:
            clean_host = mailer.clean_host(host)
            clean_from = mailer.clean_address(from_addr)
        except mailer.MessageInvalid as exc:
            raise CheckinError("mail_invalid", f"mail server or sender address {exc}") from None
        clean_port = _bounded_int(port, "port", 1, 65535)
        mode = str(security or "").strip()
        if mode not in mailer.SECURITY_MODES:
            raise CheckinError("mail_invalid", "security must be ssl (implicit TLS) or starttls")
        user = _text(username, "username", 254)
        if any(ord(c) < 32 for c in user):
            raise CheckinError("mail_invalid", "username must be one line")
        secret = str(password or "")
        if len(secret) > 512 or any(c in secret for c in "\r\n"):
            raise CheckinError("mail_invalid", "password must be one line of at most 512 characters")
        with self._tx() as con:
            self._set(con, "smtp_host", clean_host)
            self._set(con, "smtp_port", clean_port)
            self._set(con, "smtp_security", mode)
            self._set(con, "smtp_username", user)
            self._set(con, "smtp_from", clean_from)
            if clear_password:
                self._set(con, "smtp_password", "")
            elif secret:
                self._set(con, "smtp_password", secret)
            # Every save is a new configuration: an earlier connection test no longer describes it.
            self._set(con, "smtp_revision", int(self._kv(con, "smtp_revision", "0") or 0) + 1)
            self._event(con, now, "mail_saved", clean_host)
        return {"message": "Mail server saved. Use “Test saved mail server” to check the connection (it sends nothing)."}

    def mail_test_config(self) -> Dict[str, Any]:
        """The saved server settings for the owner's connection test, with their revision."""
        with self._tx() as con:
            if not self._mail_ready(con):
                raise CheckinError("mail_missing", "Save the mail server (and the password if a username is set) first.")
            return {"revision": self._kv(con, "smtp_revision", "0") or "0", "host": self._kv(con, "smtp_host"),
                    "port": self._kv(con, "smtp_port"), "security": self._kv(con, "smtp_security"),
                    "username": self._kv(con, "smtp_username"), "password": self._kv(con, "smtp_password")}

    def record_mail_test(self, *, revision: str, state: str, code: str, now: datetime) -> None:
        with self._tx() as con:
            self._set(con, "mail_test_revision", revision)
            self._set(con, "mail_test_state", state)
            self._set(con, "mail_test_code", code)
            self._set(con, "mail_test_at", iso(now))
            self._event(con, now, "mail_test_" + state, code)

    def _mail_test(self, con: sqlite3.Connection) -> Dict[str, Any]:
        """The last connection test, and whether it was run on the configuration saved now."""
        at = self._kv(con, "mail_test_at")
        if not at:
            return {"state": "never", "current": False, "at": None, "code": ""}
        current = self._kv(con, "mail_test_revision") == (self._kv(con, "smtp_revision", "0") or "0")
        return {"state": self._kv(con, "mail_test_state"), "current": current, "at": at,
                "code": self._kv(con, "mail_test_code")}

    @staticmethod
    def mail_test_text(test: Dict[str, Any], tz: str = "UTC") -> str:
        if test.get("state") == "never":
            return "not tested"
        if not test.get("current"):
            return "not tested since the last change"
        when = f"{fmt_local(test.get('at'), tz)} ({tz})"
        if test.get("state") == "ok":
            return f"connection and login OK at {when} (nothing sent; not proof of delivery)"
        return f"connection test FAILED at {when} ({test.get('code')})"

    def smtp_form(self) -> Dict[str, str]:
        with self._tx() as con:
            return {
                "smtp_host": self._kv(con, "smtp_host"),
                "smtp_port": self._kv(con, "smtp_port") or "465",
                "smtp_security": self._kv(con, "smtp_security") or "ssl",
                "smtp_username": self._kv(con, "smtp_username"),
                "smtp_from": self._kv(con, "smtp_from"),
                "password_status": "Saved" if self._kv(con, "smtp_password") else "Not saved",
                "mail_test_status": self.mail_test_text(self._mail_test(con)),
            }

    def _mail_ready(self, con: sqlite3.Connection) -> bool:
        if not (self._kv(con, "smtp_host") and self._kv(con, "smtp_port") and self._kv(con, "smtp_from")):
            return False
        return not self._kv(con, "smtp_username") or bool(self._kv(con, "smtp_password"))

    def _contact_ready(self, con: sqlite3.Connection) -> bool:
        return bool(self._kv(con, "contact_email") and self._kv(con, "contact_name")
                    and self._kv(con, "contact_consent") == "1")

    def _lease_ok(self, con: sqlite3.Connection, lease_alive: LeaseProbe) -> bool:
        """Whether the holder of the activation epoch recorded NOW is alive (see ``lease``)."""
        if callable(lease_alive):
            epoch = self._kv(con, "activation_epoch")
            return bool(epoch) and lease_alive(epoch) is True
        return bool(lease_alive)

    def _arm_state(self, con: sqlite3.Connection, ag: Optional[sqlite3.Row], lease_alive: LeaseProbe) -> Tuple[bool, str]:
        if self._kv(con, "arm_armed") != "1":
            return False, "off"
        if ag is None or ag["id"] != self._kv(con, "arm_agreement_id"):
            return False, "agreement_changed"
        if self._kv(con, "contact_revision") != self._kv(con, "arm_contact_revision") or not self._contact_ready(con):
            return False, "contact_changed"
        if not self._kv(con, "activation_epoch") or self._kv(con, "activation_epoch") != self._kv(con, "arm_epoch"):
            return False, "restarted"
        if not self._lease_ok(con, lease_alive):
            return False, "not_active"
        if not self._mail_ready(con):
            return False, "mail_not_configured"
        return True, "armed"

    # ---- agreement lifecycle

    def setup(self, *, kind: object, timezone_name: object, deadline_local: object, daily_time: object,
              grace_minutes: object, cutoff_minutes: object, guidance: object, owner_request: object,
              now: datetime) -> Dict[str, Any]:
        kind = str(kind or "").strip()
        if kind not in ("once", "daily"):
            raise CheckinError("kind_invalid", "kind must be 'once' or 'daily'")
        tz = zone(timezone_name)
        grace = _bounded_int(60 if grace_minutes in (None, "") else grace_minutes, "grace_minutes", *GRACE_RANGE)
        cutoff = _bounded_int(360 if cutoff_minutes in (None, "") else cutoff_minutes,
                              "lateness_cutoff_minutes", *CUTOFF_RANGE)
        guidance_text = _text(guidance, "contact_guidance", 1000)
        request = _text(owner_request, "owner_request", 500, required=True)
        warnings: List[str] = []
        deadline: Optional[datetime] = None
        hhmm: Optional[str] = None
        if kind == "once":
            naive = parse_local(deadline_local, "deadline_local")
            shape = local_kind(naive, tz)
            if shape != "ok":
                raise CheckinError("time_invalid", f"{naive:%Y-%m-%d %H:%M} is {'skipped' if shape == 'gap' else 'repeated'} "
                                   f"by a clock change in {tz.key}; choose another time")
            deadline = naive.replace(tzinfo=tz).astimezone(UTC)
            if not now + MIN_AHEAD <= deadline <= now + MAX_AHEAD:
                raise CheckinError("time_invalid", "the deadline must be at least 1 minute and at most 90 days ahead")
            next_due = deadline
        else:
            hh, mm = parse_hhmm(daily_time)
            hhmm = f"{hh:02d}:{mm:02d}"
            today = now.astimezone(tz).date()
            odd = [(today + timedelta(days=i), local_kind(datetime(*(today + timedelta(days=i)).timetuple()[:3], hh, mm), tz))
                   for i in range(400)]
            gaps = [d for d, shape in odd if shape == "gap"]
            repeats = [d for d, shape in odd if shape == "ambiguous"]
            if gaps:
                warnings.append(f"{hhmm} does not exist on {gaps[0]:%Y-%m-%d} in {tz.key}; that day's deadline moves "
                                "forward by the clock change and the scheduled wake may not fire, so a miss would be "
                                "noticed late. Prefer a time outside the clock change.")
            if repeats:
                warnings.append(f"{hhmm} happens twice on {repeats[0]:%Y-%m-%d} in {tz.key}; the first occurrence counts.")
            next_due = daily_after(now, tz, hh, mm)
        agreement_id = "ag-" + secrets.token_hex(5)
        with self._tx() as con:
            old = self._current(con)
            cleanup: List[str] = []
            if old is not None:
                con.execute("UPDATE agreement SET status='replaced', ended_at=?, end_reason='replaced', "
                            "revision=revision+1, updated_at=? WHERE id=?", (iso(now), iso(now), old["id"]))
                self._close_episode(con, old["id"], "replaced", now)
                cleanup = [r["schedule_id"] for r in con.execute(
                    "SELECT DISTINCT schedule_id FROM registration WHERE agreement_id=?", (old["id"],))]
            self._disarm(con, "agreement replaced", now)
            con.execute(
                "INSERT INTO agreement(id, status, kind, timezone, deadline_utc, daily_time, next_due_utc, grace_minutes, "
                "cutoff_minutes, guidance, owner_request, revision, created_at, updated_at) "
                "VALUES(?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (agreement_id, kind, tz.key, iso(deadline), hhmm, iso(next_due), grace, cutoff, guidance_text,
                 request, iso(now), iso(now)))
            self._event(con, now, "setup", f"{agreement_id} {kind}")
        if kind == "once":
            plan = {"run_at": iso(deadline), "relation": "independent",
                    "objective": wake_objective("deadline", agreement_id)}
        else:
            plan = {"cron": f"{int(hhmm[3:])} {int(hhmm[:2])} * * *", "timezone": tz.key, "relation": "independent",
                    "objective": wake_objective("deadline", agreement_id)}
        return {
            "agreement_id": agreement_id,
            "revision": 1,
            "first_deadline_local": fmt_local(iso(next_due), tz.key),
            "timezone": tz.key,
            "warnings": warnings,
            "replaced_agreement_id": old["id"] if old is not None else None,
            "cleanup_schedule_ids": cleanup,
            "contact_stage": "off (arm it separately; replacing an agreement always turns it off)",
            "wake_plan": plan,
        }

    def checkin(self, *, source: str, note: object, now: datetime, agreement_id: object = None,
                for_deadline: object = None, expect_label: Optional[str] = None) -> Dict[str, Any]:
        """The owner's explicit check-in.

        Ordinary (no target), daily: while today's deadline (``_today_deadline``) is still
        ahead, it answers that deadline exactly as a dated check-in naming it would (closing
        anything missed before it; a repeat changes nothing). Otherwise it closes only what was
        already missed — the open episode and any passed deadline no wake has processed yet —
        and never answers a deadline of a later day. One-time: completes the agreement.
        Dated (``agreement_id`` + ``for_deadline``): answers exactly that deadline of the
        current agreement; see ``_checkin_for``. ``expect_label`` (from the Settings form) must
        equal the server's ``deadline_label`` of that deadline. Every refusal happens before
        anything is written.
        """
        detail = _text(note, "note", 300)
        target = parse_target(agreement_id, for_deadline)
        with self._tx() as con:
            current = self._current(con)
            if target is not None and (current is None or current["id"] != target[0]):
                # Another agreement's deadline: an already completed one-time agreement's no-op, or
                # a refusal. Decided before the current agreement's ended pause is resumed, so that
                # “nothing changed” is true of every row.
                return self._checkin_for(con, current, target, expect_label, source, detail, now)
            ag = self._maybe_resume(con, current, now)
            if target is not None:
                return self._checkin_for(con, ag, target, expect_label, source, detail, now)
            if ag is None:
                raise CheckinError("no_agreement", "There is no active check-in agreement.")
            tz = ag["timezone"]
            episode = self._open_episode(con, ag["id"])
            if ag["kind"] == "once":
                return self._complete_once(con, ag, episode, source, detail, now)
            due = parse_iso(ag["next_due_utc"])
            overdue = ag["status"] == "active" and due is not None and due <= now
            if ag["status"] != "active":
                self._close_episode(con, ag["id"], "checkin", now)
                con.execute("UPDATE agreement SET last_checkin_at=?, updated_at=? WHERE id=?",
                            (iso(now), iso(now), ag["id"]))
                self._event(con, now, "checkin", f"{source}: {detail}")
                return {"message": f"Checked in. The agreement is paused until {deadline_label(ag['paused_until'], tz)}; "
                                   "no deadline is checked during a pause.", "changed": True, "closed": None}
            today = self._today_deadline(ag, now)
            if today is not None and today > now:
                # Today's deadline is still ahead: an ordinary check-in counts for it (and closes
                # anything missed before it), exactly as a dated check-in naming it would.
                return self._checkin_for(con, ag, (ag["id"], today), None, source, detail, now, today=True)
            if episode is None and not overdue:
                return self._nothing_to_answer(con, ag, today, source, detail, now)
            hh, mm = parse_hhmm(ag["daily_time"])
            zi = zone(tz)
            latest = daily_before(now + timedelta(seconds=1), zi, hh, mm)      # latest deadline at or before now
            answered = parse_iso(ag["last_answered_due_utc"])
            next_due = daily_after(now, zi, hh, mm) if overdue else due
            self._close_episode(con, ag["id"], "checkin", now)
            con.execute("UPDATE agreement SET next_due_utc=?, last_answered_due_utc=?, last_checkin_at=?, updated_at=? "
                        "WHERE id=?", (iso(next_due), iso(latest if answered is None else max(answered, latest)),
                                       iso(now), iso(now), ag["id"]))
            closed = self._closed_text(episode, (due, latest) if overdue else None, tz)
            self._event(con, now, "checkin", f"{source}: {detail}")
            message = (f"Checked in: {closed}. The next deadline still stands: {deadline_label(iso(next_due), tz)}; "
                       "an ordinary check-in counts only for a deadline on the same day.")
            return {"message": message + self._attempt_note(con, episode), "changed": True,
                    "closed": closed, "next_deadline_utc": iso(next_due)}

    @staticmethod
    def _today_deadline(ag: sqlite3.Row, now: datetime) -> Optional[datetime]:
        """Today's deadline of an active daily agreement: its occurrence on ``now``'s local date
        in the agreement's timezone (a gap time moved forward, a repeated time's first
        occurrence). None for a one-time or paused agreement, or when today's time came before
        the agreement existed."""
        if ag["kind"] != "daily" or ag["status"] != "active":
            return None
        zi = zone(ag["timezone"])
        hh, mm = parse_hhmm(ag["daily_time"])
        when = local_instant(now.astimezone(zi).date(), hh, mm, zi)
        created = parse_iso(ag["created_at"])
        return None if created is not None and when <= created else when

    def _nothing_to_answer(self, con: sqlite3.Connection, ag: sqlite3.Row, today: Optional[datetime], source: str,
                           detail: str, now: datetime) -> Dict[str, Any]:
        """An ordinary daily check-in with nothing missed and no deadline left today: noted as
        presence (``last_checkin_at``), and it answers nothing — the next deadline is another
        day's and stays due."""
        tz = ag["timezone"]
        con.execute("UPDATE agreement SET last_checkin_at=?, updated_at=? WHERE id=?", (iso(now), iso(now), ag["id"]))
        self._event(con, now, "checkin_nothing_due_today", f"{source}: {detail}")
        answered = parse_iso(ag["last_answered_due_utc"])
        if today is None:
            state = "There is no deadline left today"
        elif answered is not None and answered >= today:
            state = f"Today's deadline {deadline_label(iso(today), tz)} is already checked in"
        else:
            state = f"Today's deadline {deadline_label(iso(today), tz)} has passed and nothing is missed"
        return {"message": (f"Noted. {state}, so this check-in answered nothing new. The next deadline still stands: "
                            f"{deadline_label(ag['next_due_utc'], tz)}. An ordinary check-in counts only for a "
                            "deadline on the same day; to check in early for that one, confirm its date to "
                            "Ouroboros or use Settings → Check-in → “Check in for displayed deadline”."),
                "changed": False, "closed": None, "next_deadline_utc": ag["next_due_utc"]}

    @staticmethod
    def _closed_text(episode: Optional[sqlite3.Row], overdue: Optional[Tuple[datetime, datetime]], tz: str) -> str:
        """What a check-in closed: the open episode's missed deadlines and the passed deadlines
        (``overdue``: first and last) that no wake had processed yet."""
        parts = []
        if episode is not None:
            first, last = deadline_label(episode["first_due_utc"], tz), deadline_label(episode["last_due_utc"], tz)
            parts.append(f"closed the missed check-in for {first}" if first == last
                         else f"closed the missed check-ins from {first} to {last}")
        if overdue is not None and overdue[0] <= overdue[1]:
            first, last = deadline_label(iso(overdue[0]), tz), deadline_label(iso(overdue[1]), tz)
            parts.append(f"answered the passed deadline {first} (no wake had processed it yet)" if first == last
                         else f"answered the passed deadlines from {first} to {last} (no wake had processed them yet)")
        return " and ".join(parts)

    @staticmethod
    def _attempt_note(con: sqlite3.Connection, episode: Optional[sqlite3.Row]) -> str:
        if episode is None:
            return ""
        attempt = con.execute("SELECT state FROM attempt WHERE episode_id=?", (episode["id"],)).fetchone()
        if attempt is not None and attempt["state"] in ("sending", "accepted", "uncertain"):
            return " Note: a message to your contact was already handed to the mail server (or may have been); it cannot be recalled."
        return ""

    def _complete_once(self, con: sqlite3.Connection, ag: sqlite3.Row, episode: Optional[sqlite3.Row], source: str,
                       detail: str, now: datetime, dated: bool = False) -> Dict[str, Any]:
        label = deadline_label(ag["deadline_utc"], ag["timezone"])
        self._close_episode(con, ag["id"], "checkin", now)
        con.execute("UPDATE agreement SET status='completed', ended_at=?, end_reason='checked_in', last_checkin_at=?, "
                    "last_answered_due_utc=?, updated_at=? WHERE id=?",
                    (iso(now), iso(now), ag["deadline_utc"], iso(now), ag["id"]))
        self._disarm(con, "agreement completed", now)
        self._event(con, now, "checkin", f"{source}{' (dated)' if dated else ''}: {detail}")
        message = (f"Checked in for {label}. The one-time agreement is complete; delete its wake schedule if it is "
                   "still pending.")
        return {"message": message + self._attempt_note(con, episode), "changed": True,
                "answered_deadline_utc": ag["deadline_utc"], "next_deadline_utc": None}

    def _checkin_for(self, con: sqlite3.Connection, ag: Optional[sqlite3.Row], target: Tuple[str, datetime],
                     expect_label: Optional[str], source: str, detail: str, now: datetime,
                     today: bool = False) -> Dict[str, Any]:
        """A dated check-in: the current agreement and one exact deadline, named by the owner.

        Allowed: the latest daily deadline that passed or the next one at or after now (for a
        one-time agreement, its deadline). It closes the open episode (whose missed deadlines
        are all at or before it) and answers every deadline up to it; the next deadline after
        it stands. A deadline already answered is a no-op, decided before anything is written.
        ``today``: an ordinary check-in counting for today's deadline (only the wording differs).
        """
        wanted, due = target
        tz = ag["timezone"] if ag is not None else ""
        if ag is None or ag["id"] != wanted:
            old = con.execute("SELECT * FROM agreement WHERE id=?", (wanted,)).fetchone()
            if (old is not None and old["kind"] == "once" and old["end_reason"] == "checked_in"
                    and parse_iso(old["deadline_utc"]) == due and self._label_ok(expect_label, due, old["timezone"])):
                return {"message": f"Already checked in for {deadline_label(iso(due), old['timezone'])}; that one-time "
                                   "agreement is complete. Nothing changed.", "changed": False,
                        "answered_deadline_utc": iso(due), "next_deadline_utc": None}
            raise CheckinError("agreement_mismatch", "This dated check-in names an agreement that is not the current "
                                                     "one (it was replaced, cancelled or completed); nothing was "
                                                     "changed. Press Refresh on the Check-in card or reload Settings → "
                                                     "Check-in, or ask Ouroboros for the current status.")
        label = deadline_label(iso(due), tz)
        named = f"today's deadline {label}" if today else label
        if not self._label_ok(expect_label, due, tz):
            raise CheckinError("label_mismatch", f"The displayed deadline does not match the one submitted ({label}); "
                                                 "nothing was changed. Press Refresh on the Check-in card or reload "
                                                 "Settings → Check-in.")
        episode = self._open_episode(con, ag["id"])
        if ag["kind"] == "once":
            if parse_iso(ag["deadline_utc"]) != due:
                raise CheckinError("target_invalid", f"{label} is not this agreement's deadline "
                                                     f"({deadline_label(ag['deadline_utc'], tz)}); nothing was changed.")
            return self._complete_once(con, ag, episode, source, detail, now, dated=True)
        if ag["status"] != "active":
            raise CheckinError("paused", f"The agreement is paused until {deadline_label(ag['paused_until'], tz)}; no "
                                         "deadline is checked during a pause, so there is nothing to check in for. "
                                         "Nothing was changed.")
        zi = zone(tz)
        hh, mm = parse_hhmm(ag["daily_time"])
        if not is_daily_occurrence(due, zi, hh, mm):
            raise CheckinError("target_invalid", f"{label} is not one of this agreement's daily deadlines "
                                                 f"({ag['daily_time']} {tz}); nothing was changed.")
        # At a deadline's exact instant it is the next one (at or after now); the latest that
        # passed is the one before it. A deadline from before this agreement existed is not one of its own.
        latest, upcoming = daily_before(now, zi, hh, mm), daily_at_or_after(now, zi, hh, mm)
        created = parse_iso(ag["created_at"])
        allowed = [(when, what) for when, what in ((latest, "the latest that passed"), (upcoming, "the next one"))
                   if created is None or when > created]
        last_missed = parse_iso(episode["last_due_utc"]) if episode is not None else None
        if due not in [when for when, _ in allowed] or (last_missed is not None and last_missed > due):
            choices = " or ".join(f"{deadline_label(iso(when), tz)} ({what})" for when, what in allowed)
            raise CheckinError("target_stale", f"{label} is not a deadline you can check in for now: only "
                                               f"{choices}. Nothing was changed. Press Refresh on the Check-in "
                                               "card or reload Settings → Check-in for the current deadline.")
        answered = parse_iso(ag["last_answered_due_utc"])
        current = parse_iso(ag["next_due_utc"])
        if answered is not None and due <= answered:
            return {"message": f"Already checked in for {named}; nothing changed. Next deadline: "
                               f"{deadline_label(ag['next_due_utc'], tz)}.", "changed": False,
                    "answered_deadline_utc": iso(due), "next_deadline_utc": ag["next_due_utc"]}
        next_due = daily_after(due, zi, hh, mm)
        next_due = next_due if current is None else max(current, next_due)
        # Earlier deadlines no wake processed yet (from next_due up to the named one) are
        # answered together with it: the owner is evidently here.
        overdue = (current, daily_before(due, zi, hh, mm)) if current is not None and current < due else None
        self._close_episode(con, ag["id"], "checkin", now)
        con.execute("UPDATE agreement SET next_due_utc=?, last_answered_due_utc=?, last_checkin_at=?, updated_at=? "
                    "WHERE id=?", (iso(next_due), iso(due), iso(now), iso(now), ag["id"]))
        self._event(con, now, "checkin", f"{source} for {iso(due)}: {detail}")
        when = "early" if due > now else "late"
        closed = self._closed_text(episode, overdue, tz)
        message = (f"Checked in for {named} ({when}). " + (closed[0].upper() + closed[1:] + ". " if closed else "")
                   + f"Next deadline: {deadline_label(iso(next_due), tz)}.")
        return {"message": message + self._attempt_note(con, episode), "changed": True, "closed": closed or None,
                "answered_deadline_utc": iso(due), "next_deadline_utc": iso(next_due)}

    @staticmethod
    def _label_ok(expect_label: Optional[str], due: datetime, tz: str) -> bool:
        return expect_label is None or str(expect_label).strip() == deadline_label(iso(due), tz)

    def pause(self, *, until_local: object, source: str, now: datetime) -> Dict[str, Any]:
        with self._tx() as con:
            ag = self._maybe_resume(con, self._current(con), now)
            if ag is None or ag["status"] != "active":
                raise CheckinError("not_active", "Only an active agreement can be paused.")
            if ag["kind"] != "daily":
                raise CheckinError("not_daily", "A one-time agreement cannot be paused; replace or cancel it instead.")
            tz = zone(ag["timezone"])
            until = parse_local(until_local, "until_local").replace(tzinfo=tz, fold=0).astimezone(UTC)
            if not now < until <= now + MAX_AHEAD:
                raise CheckinError("time_invalid", "pause end must be in the future and at most 90 days ahead")
            con.execute("UPDATE agreement SET status='paused', paused_until=?, revision=revision+1, updated_at=? WHERE id=?",
                        (iso(until), iso(now), ag["id"]))
            # A pause is not a check-in: it closes the streak only together with the contact
            # stage, so a later miss cannot reach the contact again without a new owner arm.
            self._close_episode(con, ag["id"], "paused", now)
            self._disarm(con, "paused", now)
            self._event(con, now, "paused", source)
        return {"message": f"Paused until {fmt_local(iso(until), tz.key)} ({tz.key}). A pause is not a check-in. "
                           "The contact stage is now off; arm it again after the pause if you still want it."}

    def resume(self, *, source: str, now: datetime) -> Dict[str, Any]:
        with self._tx() as con:
            ag = self._current(con)
            if ag is None or ag["status"] != "paused":
                raise CheckinError("not_paused", "The agreement is not paused.")
            next_due = self._due_after_pause(ag, now)
            con.execute("UPDATE agreement SET status='active', paused_until=NULL, revision=revision+1, next_due_utc=?, "
                        "updated_at=? WHERE id=?", (iso(next_due), iso(now), ag["id"]))
            self._event(con, now, "resumed", source)
        return {"message": f"Resumed. Next deadline: {deadline_label(iso(next_due), ag['timezone'])}.",
                "next_deadline_utc": iso(next_due)}

    def cancel(self, *, source: str, now: datetime) -> Dict[str, Any]:
        with self._tx() as con:
            ag = self._current(con)
            if ag is None:
                raise CheckinError("no_agreement", "There is no active check-in agreement.")
            con.execute("UPDATE agreement SET status='cancelled', ended_at=?, end_reason='cancelled', revision=revision+1, "
                        "updated_at=? WHERE id=?", (iso(now), iso(now), ag["id"]))
            self._close_episode(con, ag["id"], "cancelled", now)
            self._disarm(con, "agreement cancelled", now)
            cleanup = [r["schedule_id"] for r in con.execute(
                "SELECT DISTINCT schedule_id FROM registration WHERE agreement_id=?", (ag["id"],))]
            self._event(con, now, "cancelled", source)
        return {"message": "Agreement cancelled (this is not a check-in). Its scheduled wakes should be deleted.",
                "cleanup_schedule_ids": cleanup}

    def arm(self, *, source: str, statement: object, now: datetime, lease_alive: LeaseProbe) -> Dict[str, Any]:
        words = _text(statement, "owner_request", 500, required=True)
        with self._tx() as con:
            ag = self._maybe_resume(con, self._current(con), now)
            if ag is None:
                raise CheckinError("no_agreement", "Set up an agreement first.")
            if ag["status"] == "paused":
                raise CheckinError("paused", "The agreement is paused, and a pause keeps the contact stage off. Arm it "
                                             "again after the pause ends.")
            if not self._contact_ready(con):
                raise CheckinError("contact_missing", "Configure one contact and confirm their consent in Settings → Check-in first.")
            if not self._mail_ready(con):
                raise CheckinError("mail_missing", "Configure the mail server in Settings → Check-in first.")
            epoch = self._kv(con, "activation_epoch")
            if not epoch or not self._lease_ok(con, lease_alive):
                raise CheckinError("not_active", "The Check-in skill is not active in the server right now; try again after it loads.")
            if self._open_episode(con, ag["id"]) is not None:
                raise CheckinError("streak_open", "A check-in is currently missed. Check in first (press “I'm here” or "
                                                  "tell Ouroboros), then arm the contact stage.")
            due = parse_iso(ag["next_due_utc"])
            if due is not None and due <= now:
                # The deadline passed but its wake has not run yet (delayed, asleep or queued):
                # arming now would let that late wake escalate a check-in already missed.
                raise CheckinError("deadline_passed", "A check-in deadline has passed and was not answered yet. Check "
                                                      "in first (press “I'm here” or tell Ouroboros), then arm the "
                                                      "contact stage.")
            self._set(con, "arm_id", "arm-" + secrets.token_hex(6))
            self._set(con, "arm_armed", "1")
            self._set(con, "arm_agreement_id", ag["id"])
            self._set(con, "arm_contact_revision", self._kv(con, "contact_revision"))
            self._set(con, "arm_epoch", epoch)
            self._set(con, "arm_at", iso(now))
            self._set(con, "arm_source", source)
            self._set(con, "arm_statement", words)
            self._event(con, now, "contact_stage_armed", source)
            name = self._kv(con, "contact_name")
            tested = self._mail_test(con)
        hint = "" if tested["current"] and tested["state"] == "ok" else (
            " The saved mail server has not passed a connection test; “Test saved mail server” in Settings → Check-in "
            "checks it without sending anything.")
        return {"message": f"Contact stage armed for {name}. It turns off if Ouroboros restarts, the skill reloads, "
                           "the contact changes, or the agreement is paused, replaced or cancelled." + hint}

    def disarm(self, *, source: str, now: datetime) -> Dict[str, Any]:
        with self._tx() as con:
            self._disarm(con, f"turned off by {source}", now)
        return {"message": "Contact stage is off."}

    # ---- wakes

    def register_wake(self, *, kind: str, schedule_id: object, agreement_id: object, episode_id: object,
                      task_id: str, now: datetime) -> Dict[str, Any]:
        sid = _text(schedule_id, "schedule_id", 160, required=True)
        if any(not (c.isalnum() or c in "-_.:") for c in sid):
            raise CheckinError("value_invalid", "schedule_id must be the id from FOLLOWUP_SCHEDULED")
        with self._tx() as con:
            ag = self._current(con)
            if ag is None or ag["id"] != str(agreement_id or ""):
                raise CheckinError("stale_agreement", "That agreement is not the current one; delete this schedule.")
            ep_id = ""
            if kind == "grace":
                episode = self._open_episode(con, ag["id"])
                if episode is None or episode["id"] != str(episode_id or "") or episode["contact_state"] != "waiting":
                    raise CheckinError("stale_episode", "No episode is waiting for a grace wake; delete this schedule.")
                ep_id = episode["id"]
                earlier = [r["schedule_id"] for r in con.execute(
                    "SELECT schedule_id FROM registration WHERE kind='grace' AND episode_id=?", (ep_id,))]
                if sid in earlier:
                    return {"message": f"Grace wake {sid} was already recorded for this streak; nothing changed."}
                if earlier:
                    # One grace per streak: a stopped or deleted grace wake is never replaced.
                    raise CheckinError("grace_already_registered", "This streak already has its one grace wake "
                                       f"({earlier[0]}); a second one is never accepted. Delete this schedule.")
            elif kind != "deadline":
                raise CheckinError("value_invalid", "kind must be 'deadline' or 'grace'")
            if con.execute("SELECT 1 FROM registration WHERE schedule_id=? AND kind<>?", (sid, kind)).fetchone():
                raise CheckinError("value_invalid", "That schedule id was already reported for the other wake kind; "
                                                    "a grace wake needs its own one-shot schedule.")
            con.execute("INSERT INTO registration(kind, schedule_id, agreement_id, episode_id, reported_at, task_id) "
                        "VALUES(?, ?, ?, ?, ?, ?)", (kind, sid, ag["id"], ep_id or None, iso(now), task_id))
            self._event(con, now, "wake_registered", f"{kind} {sid}")
        return {"message": f"Recorded your report that {kind} wake {sid} is registered. This is agent-reported and "
                           "not verified; the status shows when it actually arrives."}

    def due(self, *, wake_kind: str, agreement_id: object, schedule_id: str, task_id: str, now: datetime,
            lease_alive: LeaseProbe, episode_id: object = "", schedule_due_at: str = "") -> Dict[str, Any]:
        """Record a wake and decide. A missed deadline claims the owner notice in this transaction.

        ``schedule_id`` and ``schedule_due_at`` are the host's own occurrence facts for the calling
        task (empty outside a scheduled task); a grace wake must match its registration exactly.
        """
        if wake_kind not in ("deadline", "grace"):
            raise CheckinError("value_invalid", "wake_kind must be 'deadline' or 'grace'")
        with self._tx() as con:
            ag = self._maybe_resume(con, self._current(con), now)
            wanted = str(agreement_id or "")
            matched = bool(schedule_id) and con.execute(
                "SELECT 1 FROM registration WHERE schedule_id=? AND agreement_id=? AND kind=?",
                (schedule_id, wanted or (ag["id"] if ag is not None else ""), wake_kind)).fetchone() is not None
            con.execute("INSERT INTO wake(arrived_at, kind, agreement_id, task_id, schedule_id, matched_registration) "
                        "VALUES(?, ?, ?, ?, ?, ?)", (iso(now), wake_kind, wanted or None, task_id, schedule_id or None,
                                                     1 if matched else 0))
            wake_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            con.execute("DELETE FROM wake WHERE id <= ? - 300", (wake_id,))
            if ag is None or (wanted and wanted != ag["id"]):
                cleanup = [schedule_id] if schedule_id else []
                outcome = {"action": "none", "reason": "No current agreement matches this wake (it was cancelled, "
                           "completed or replaced).", "cleanup_schedule_ids": cleanup,
                           "next_step": "Nothing to do. If this wake came from a schedule, delete that schedule with "
                                        "manage_schedules(action='delete') giving this reason."}
                return self._wake_outcome(con, wake_id, outcome)
            if ag["status"] == "paused":
                return self._wake_outcome(con, wake_id, {
                    "action": "none", "reason": f"Paused until {fmt_local(ag['paused_until'], ag['timezone'])}.",
                    "next_step": "Nothing to do."})
            episode = self._open_episode(con, ag["id"])
            if wake_kind == "grace":
                return self._wake_outcome(con, wake_id, self._grace(
                    con, ag, episode, now, lease_alive, episode_id=str(episode_id or ""), schedule_id=schedule_id,
                    schedule_due_at=schedule_due_at, task_id=task_id))
            return self._wake_outcome(con, wake_id, self._deadline(con, ag, episode, now, lease_alive))

    def _wake_outcome(self, con: sqlite3.Connection, wake_id: int, outcome: Dict[str, Any]) -> Dict[str, Any]:
        con.execute("UPDATE wake SET outcome=? WHERE id=?", (outcome.get("action"), wake_id))
        return outcome

    def _deadline(self, con, ag, episode, now: datetime, lease_alive: LeaseProbe) -> Dict[str, Any]:
        tz = zone(ag["timezone"])
        due_at = parse_iso(ag["next_due_utc"])
        if ag["kind"] == "once" and episode is not None:
            return {"action": "none", "reason": "This one-time deadline was already handled.",
                    "next_step": "Nothing to do."}
        if due_at is None or now < due_at:
            when = fmt_local(ag["next_due_utc"], ag["timezone"]) if due_at else ""
            return {"action": "none", "reason": f"Not due yet{(' (next deadline ' + when + ')') if when else ''}.",
                    "next_step": "Nothing to do."}
        # ``due_at`` is the OLDEST deadline this wake answers, ``newest`` the latest one at or before
        # now. The streak's latest missed deadline is the newest: it is what the card and a dated
        # check-in name, so an older one would be refused as no longer current at every press.
        missed, newest = 1, due_at
        if ag["kind"] == "daily":
            hh, mm = parse_hhmm(ag["daily_time"])
            probe = daily_after(due_at, tz, hh, mm)
            while probe <= now and missed < 1000:
                missed, newest = missed + 1, probe
                probe = daily_after(probe, tz, hh, mm)
            con.execute("UPDATE agreement SET next_due_utc=? WHERE id=?", (iso(probe), ag["id"]))
        else:
            con.execute("UPDATE agreement SET next_due_utc=NULL WHERE id=?", (ag["id"],))
        # Lateness is measured from the OLDEST deadline this wake answers (conservative on
        # purpose): when an earlier deadline got no wake at all (asleep, queue, deleted row),
        # even an on-time wake for the newest one is a late catch-up — the owner is told, nobody
        # is contacted for it, and the next deadline is judged on its own.
        stale = now - due_at > timedelta(minutes=int(ag["cutoff_minutes"]))
        armed, arm_reason = self._arm_state(con, ag, lease_alive)
        if episode is None:
            episode_id = "ep-" + secrets.token_hex(5)
            con.execute(
                "INSERT INTO episode(id, agreement_id, agreement_revision, state, opened_at, first_due_utc, last_due_utc, "
                "missed_count, notice_state, contact_state) VALUES(?, ?, ?, 'open', ?, ?, ?, ?, 'none', 'not_armed')",
                (episode_id, ag["id"], ag["revision"], iso(now), iso(due_at), iso(newest), missed))
            contact_state = "not_armed"
        else:
            episode_id = episode["id"]
            con.execute("UPDATE episode SET missed_count=missed_count+?, last_due_utc=? WHERE id=?",
                        (missed, iso(newest), episode_id))
            contact_state = episode["contact_state"]
        grace_end = parse_iso(episode["grace_ends_at"]) if episode is not None else None
        con.execute("UPDATE episode SET notice_state='sending', notice_due_utc=?, notice_stale=?, notice_claimed_at=?, "
                    "notice_at=NULL, notice_detail=NULL WHERE id=?", (iso(due_at), 1 if stale else 0, iso(now), episode_id))
        due_text = fmt_local(iso(due_at), ag["timezone"])
        if stale:
            text = _NOTICE_LATE.format(due=due_text, tz=ag["timezone"], late=_duration(now - due_at))
        else:
            if contact_state in TERMINAL_CONTACT:
                contact = _CONTACT_USED
            elif grace_end is not None:            # this streak's one grace was already planned
                pending = (armed and contact_state in ("waiting", "window")
                           and now <= grace_end + timedelta(minutes=int(ag["cutoff_minutes"])))
                contact = (_CONTACT_PENDING.format(name=self._kv(con, "contact_name"),
                                                   at=fmt_local(iso(grace_end), ag["timezone"]))
                           if pending else _CONTACT_PASSED)
            elif armed:
                contact = _CONTACT_ARMED.format(grace=ag["grace_minutes"], name=self._kv(con, "contact_name"))
            else:
                contact = _CONTACT_OFF
            text = _NOTICE_MISSED.format(due=due_text, tz=ag["timezone"], contact=contact)
        self._event(con, now, "deadline_missed", f"{episode_id} stale={stale} missed={missed}")
        return {"action": "notify_owner", "episode_id": episode_id, "notice_due_utc": iso(due_at),
                "notice_text": text, "stale": stale, "missed_deadlines": missed,
                "contact_stage": arm_reason if not armed else "armed"}

    def notice_result(self, *, episode_id: str, notice_due_utc: str, outcome: str, detail: str, now: datetime,
                      lease_alive: LeaseProbe) -> Dict[str, Any]:
        """Record the owner notice's outcome; the first confirmed, armed notice plans the streak's one grace."""
        with self._tx() as con:
            ep = con.execute("SELECT * FROM episode WHERE id=?", (episode_id,)).fetchone()
            if ep is None or ep["notice_state"] != "sending" or ep["notice_due_utc"] != notice_due_utc:
                return {"action": "none", "reason": "This notice was superseded.", "next_step": "Nothing to do."}
            ag = con.execute("SELECT * FROM agreement WHERE id=?", (ep["agreement_id"],)).fetchone()
            planned = bool(ep["grace_ends_at"])   # set once per episode, never cleared or moved
            if outcome != "confirmed":
                con.execute("UPDATE episode SET notice_state=?, notice_detail=? WHERE id=?", (outcome, detail[:120], episode_id))
                if ep["contact_state"] not in TERMINAL_CONTACT and not planned:
                    con.execute("UPDATE episode SET contact_state='blocked_notice' WHERE id=?", (episode_id,))
                self._event(con, now, "notice_not_confirmed", f"{episode_id} {outcome}")
                return {"action": "owner_notice_not_confirmed", "episode_id": episode_id, "notice": outcome,
                        "reason": ("The owner notice was not confirmed, so no contact stage opens for this missed deadline."
                                   + (" This streak's grace, planned after an earlier confirmed notice, is unchanged."
                                      if planned else "")),
                        "next_step": "Do not schedule a grace wake and do not contact anyone. In your reply, tell the owner "
                                     "that the check-in deadline passed and the notice could not be confirmed; do not "
                                     "retry the notice."}
            con.execute("UPDATE episode SET notice_state='confirmed', notice_at=?, notice_detail=NULL WHERE id=?",
                        (iso(now), episode_id))
            current = self._current(con)
            live = current is not None and ag is not None and current["id"] == ag["id"] and ep["state"] == "open"
            armed, reason = self._arm_state(con, current, lease_alive)
            state = ep["contact_state"]
            if not live:
                state_note = "The agreement changed meanwhile; nothing else to do."
            elif state in TERMINAL_CONTACT:
                state_note = "The contact stage was already used in this streak; it will not open again until a check-in."
            elif planned:
                state_note = (f"This streak's one grace wake was already planned (grace ends "
                              f"{fmt_local(ep['grace_ends_at'], ag['timezone'])}); a later deadline never re-plans it.")
            elif ep["notice_stale"]:
                state = "stale"
                state_note = "The deadline was noticed too late; no contact for it."
            elif not armed:
                state = "not_armed"
                state_note = f"The contact stage is not armed ({reason}); nobody will be contacted."
            else:
                state = "waiting"
                state_note = ""
            new_grace = live and not planned and state == "waiting"
            grace_end = now + timedelta(minutes=int(ag["grace_minutes"])) if new_grace else None
            if new_grace:
                con.execute("UPDATE episode SET contact_state='waiting', contact_notice_at=?, grace_ends_at=? WHERE id=?",
                            (iso(now), iso(grace_end), episode_id))
            elif live and not planned and state not in TERMINAL_CONTACT:
                con.execute("UPDATE episode SET contact_state=? WHERE id=?", (state, episode_id))
            self._event(con, now, "notice_confirmed", f"{episode_id} contact={state} grace_planned={new_grace}")
            if new_grace:
                return {"action": "owner_notified", "episode_id": episode_id, "agreement_id": ag["id"],
                        "notice_at": iso(now), "grace_ends_at": iso(grace_end),
                        "next_step": {
                            "do": "Register exactly this one-shot grace wake with schedule_followup, then report it with "
                                  "wake_registered(kind='grace', schedule_id=<id>, agreement_id, episode_id). Nothing else.",
                            "schedule_followup": {"run_at": iso(grace_end), "relation": "independent",
                                                  "objective": wake_objective("grace", ag["id"], episode_id)},
                        }}
            return {"action": "owner_notified", "episode_id": episode_id, "notice_at": iso(now),
                    "contact_stage": state, "reason": state_note, "next_step": "Nothing else to schedule."}

    def _grace(self, con, ag, episode, now: datetime, lease_alive: LeaseProbe, *, episode_id: str, schedule_id: str,
               schedule_due_at: str, task_id: str) -> Dict[str, Any]:
        cleanup = [schedule_id] if schedule_id else []
        if not episode_id:
            return {"action": "none", "reason": "A grace wake must pass the episode_id written in its objective.",
                    "next_step": "Call due again with wake_kind='grace', agreement_id and episode_id from the objective."}
        if episode is None or episode["id"] != episode_id:
            return {"action": "none", "reason": "The missed check-in this grace wake was for is no longer open (the owner "
                    "checked in, or the agreement or streak changed).", "cleanup_schedule_ids": cleanup,
                    "next_step": "Nothing to do. If this wake came from a schedule, delete that schedule with "
                                 "manage_schedules(action='delete') giving this reason."}
        bound = bool(schedule_id) and con.execute(
            "SELECT 1 FROM registration WHERE kind='grace' AND schedule_id=? AND agreement_id=? AND episode_id=?",
            (schedule_id, ag["id"], episode["id"])).fetchone() is not None
        if not bound:
            return {"action": "none", "reason": "Only the grace wake registered for this streak can open the contact "
                    "window, and this task is not it. Nothing changed.", "next_step": "Nothing to do."}
        state = episode["contact_state"]
        if state in TERMINAL_CONTACT:
            return {"action": "none", "reason": f"The contact stage was already used in this streak ({state}).",
                    "next_step": "Nothing to do."}
        if state not in ("waiting", "window"):
            return {"action": "none", "reason": f"The contact stage is not open ({state}).", "next_step": "Nothing to do."}
        grace_end = parse_iso(episode["grace_ends_at"])
        fired_for = _instant(schedule_due_at)
        if grace_end is None or fired_for is None or abs(fired_for - grace_end) > GRACE_DUE_TOLERANCE:
            return {"action": "none", "reason": "This wake's scheduled time does not match this streak's grace end, so "
                    "it cannot open the contact window. Nothing changed.", "next_step": "Nothing to do."}
        if not task_id:
            return {"action": "none", "reason": "This wake has no task id, so it cannot hold the contact window. "
                    "Nothing changed.", "next_step": "Nothing to do."}
        if state == "window" and (episode["window_schedule_id"] != schedule_id or episode["window_task_id"] != task_id):
            return {"action": "none", "reason": "The contact window was opened by another wake task; only that task "
                    "may use it. Nothing changed.", "next_step": "Nothing to do."}
        if now < grace_end:
            return {"action": "none", "reason": f"Grace has not ended (ends {fmt_local(episode['grace_ends_at'], ag['timezone'])}).",
                    "next_step": "Nothing to do now; the registered grace wake will come back."}
        if not episode["contact_notice_at"]:
            # Only possible for an episode carried over from schema v2 whose reminder time was not kept.
            return {"action": "none", "reason": "This streak's reminder time is not on record (state from an older "
                    "version), so the contact window does not open.", "next_step": "Nothing to do."}
        if now - grace_end > timedelta(minutes=int(ag["cutoff_minutes"])):
            con.execute("UPDATE episode SET contact_state='stale' WHERE id=?", (episode["id"],))
            return {"action": "none", "reason": "This grace wake arrived after the lateness cutoff; no contact.",
                    "next_step": "Nothing to do. Tell the owner in your reply that the contact stage was skipped as stale."}
        armed, reason = self._arm_state(con, ag, lease_alive)
        if not armed:
            con.execute("UPDATE episode SET contact_state='not_armed' WHERE id=?", (episode["id"],))
            return {"action": "none", "reason": f"The contact stage is not armed now ({reason}); nobody will be contacted.",
                    "next_step": "Nothing to do. Tell the owner in your reply that no contact was made and why."}
        if state == "waiting":
            con.execute("UPDATE episode SET contact_state='window', window_opened_at=?, window_schedule_id=?, "
                        "window_task_id=? WHERE id=?", (iso(now), schedule_id, task_id, episode["id"]))
        tz = ag["timezone"]
        facts = {
            "agreement": ("one-time check-in by " + fmt_local(episode["first_due_utc"], tz)) if ag["kind"] == "once"
            else f"daily check-in by {ag['daily_time']}",
            "timezone": tz,
            "missed_deadlines": int(episode["missed_count"]),
            "first_missed_deadline_local": fmt_local(episode["first_due_utc"], tz),
            "owner_notified_at_local": fmt_local(episode["contact_notice_at"], tz),
            "last_checkin_local": fmt_local(ag["last_checkin_at"], tz) or "never",
            "no_checkin_since_notice": True,
        }
        if (episode["notice_state"] == "confirmed" and episode["notice_at"]
                and episode["notice_at"] != episode["contact_notice_at"]):
            facts["latest_reminder_local"] = fmt_local(episode["notice_at"], tz)
        return {
            "action": "contact_window",
            "episode_id": episode["id"],
            "agreement_revision": ag["revision"],
            "facts": facts,
            "contact_name": self._kv(con, "contact_name"),
            "contact_guidance": ag["guidance"] or "(no extra guidance from the owner)",
            "next_step": (
                "Decide now, in this task. Either call send_contact(episode_id, agreement_revision, subject, body, "
                "reason) with your own short message to the contact, or decline_contact(episode_id, "
                "agreement_revision, reason) if you have concrete evidence the owner is present or their guidance "
                "says not to. Write in the language the owner's guidance asks for or the contact will understand; "
                "say that you are Ouroboros, the AI agent the owner set this up with. State only the facts above: no "
                "diagnosis, no claim or hint of danger, no invented personal facts, no quotes from private "
                "conversations, no promise of further messages. A short fixed English footer is appended. One "
                "attempt only; never use another tool to reach this person."),
        }

    # ---- contact

    def admit_send(self, *, episode_id: object, agreement_revision: object, subject: object, body: object,
                   reason: object, schedule_id: str, task_id: str, pid: int, now: datetime,
                   lease_alive: LeaseProbe) -> Dict[str, Any]:
        try:
            clean_subject = mailer.clean_subject(subject)
            clean_body = mailer.clean_body(body)
        except mailer.MessageInvalid as exc:
            raise CheckinError("message_invalid", str(exc)) from None
        if len(clean_body) > MAX_AGENT_BODY:
            raise CheckinError("message_invalid", f"body must be at most {MAX_AGENT_BODY} characters (a fixed "
                                                  f"{len(_FOOTER)}-character footer is appended); nothing was sent")
        why = _text(reason, "reason", 500, required=True)
        with self._tx() as con:
            ag = self._current(con)
            ep = con.execute("SELECT * FROM episode WHERE id=?", (str(episode_id or ""),)).fetchone()
            if ag is None or ep is None or ep["agreement_id"] != ag["id"] or ag["status"] != "active":
                raise CheckinError("stale", "No such open episode for the current agreement; nothing was sent.")
            if str(agreement_revision) != str(ag["revision"]) or ep["agreement_revision"] != ag["revision"]:
                raise CheckinError("stale", "The agreement changed since this episode opened; nothing was sent.")
            if ep["state"] != "open":
                raise CheckinError("stale", f"The episode is closed ({ep['close_reason']}); nothing was sent.")
            if con.execute("SELECT 1 FROM attempt WHERE episode_id=?", (ep["id"],)).fetchone() is not None:
                raise CheckinError("already_attempted", "A contact attempt already exists for this streak; it is never repeated.")
            if ep["contact_state"] != "window":
                raise CheckinError("window_closed", f"The contact window is not open ({ep['contact_state']}); call due first.")
            if (not schedule_id or ep["window_schedule_id"] != schedule_id
                    or not task_id or ep["window_task_id"] != task_id):
                raise CheckinError("not_window_wake", "Only the grace wake task that opened the contact window may "
                                                      "send; nothing was sent.")
            grace_end = parse_iso(ep["grace_ends_at"])
            if grace_end is None or now < grace_end or now - grace_end > timedelta(minutes=int(ag["cutoff_minutes"])):
                raise CheckinError("window_closed", "Outside the contact window; nothing was sent.")
            armed, arm_reason = self._arm_state(con, ag, lease_alive)
            if not armed:
                raise CheckinError("not_armed", f"The contact stage is not armed ({arm_reason}); nothing was sent.")
            attempt_id = "at-" + secrets.token_hex(5)
            final_body = clean_body + _FOOTER
            name = self._kv(con, "contact_name")
            mail = {"host": self._kv(con, "smtp_host"), "port": self._kv(con, "smtp_port"),
                    "security": self._kv(con, "smtp_security"), "username": self._kv(con, "smtp_username"),
                    "password": self._kv(con, "smtp_password"), "from_addr": self._kv(con, "smtp_from"),
                    "to_addr": self._kv(con, "contact_email"), "to_name": name,
                    "subject": clean_subject, "body": final_body}
            # Build the exact message (and check the server settings) BEFORE claiming the one
            # attempt: anything the mailer would refuse locally must not consume the streak.
            try:
                mailer.build_message(from_addr=mail["from_addr"], to_addr=mail["to_addr"], to_name=name,
                                     subject=clean_subject, body=final_body)
                mailer.server_settings(mail["host"], mail["port"], mail["security"])
            except Exception as exc:  # whatever the mailer would refuse locally, refused here unclaimed
                detail = str(exc) if isinstance(exc, mailer.MessageInvalid) else type(exc).__name__
                raise CheckinError("message_invalid", f"this message cannot be sent as written ({detail}); nothing "
                                                      "was sent and the attempt is still available") from None
            # The admission binding the recheck before DATA compares against: this activation,
            # this contact revision and this arm (a re-arm mints a new arm_id).
            con.execute(
                "INSERT INTO attempt(id, episode_id, agreement_id, agreement_revision, state, claimed_at, pid, epoch, "
                "contact_revision, arm_id, schedule_id, task_id, recipient_label, subject, body, reason) "
                "VALUES(?, ?, ?, ?, 'sending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, ep["id"], ag["id"], ag["revision"], iso(now), pid, self._kv(con, "activation_epoch"),
                 self._kv(con, "contact_revision"), self._kv(con, "arm_id"), schedule_id, task_id, name,
                 clean_subject, final_body, why))
            con.execute("UPDATE episode SET contact_state='attempted', decision_reason=? WHERE id=?", (why, ep["id"]))
            self._event(con, now, "contact_claimed", attempt_id)
            mail["port"] = int(mail["port"])
            return {"attempt_id": attempt_id, "mail": mail}

    def still_admissible(self, *, attempt_id: str, lease_alive: LeaseProbe) -> Tuple[bool, str]:
        with self._tx() as con:
            at = con.execute("SELECT * FROM attempt WHERE id=?", (attempt_id,)).fetchone()
            ag = self._current(con)
            if at is None or at["state"] != "sending":
                return False, "attempt_not_sending"
            if ag is None or ag["id"] != at["agreement_id"] or ag["revision"] != at["agreement_revision"] or ag["status"] != "active":
                return False, "agreement_changed"
            ep = con.execute("SELECT state FROM episode WHERE id=?", (at["episode_id"],)).fetchone()
            if ep is None or ep["state"] != "open":
                return False, "episode_closed"
            # Compare against the binding captured at admission, not merely "armed now":
            # a restart plus a re-arm, or a contact change plus a re-arm, must not
            # authorize a message claimed under the old activation or recipient.
            if not at["epoch"] or at["epoch"] != self._kv(con, "activation_epoch"):
                return False, "restarted"
            if at["contact_revision"] != self._kv(con, "contact_revision"):
                return False, "contact_changed"
            if not at["arm_id"] or at["arm_id"] != self._kv(con, "arm_id"):
                return False, "rearmed"
            armed, reason = self._arm_state(con, ag, lease_alive)
            return (True, "ok") if armed else (False, reason)

    def finish_attempt(self, *, attempt_id: str, state: str, detail: str, now: datetime) -> None:
        with self._tx() as con:
            con.execute("UPDATE attempt SET state=?, detail=?, finished_at=? WHERE id=? AND state='sending'",
                        (state, detail[:160], iso(now), attempt_id))
            self._event(con, now, "contact_" + state, attempt_id)

    def decline(self, *, episode_id: object, agreement_revision: object, reason: object, schedule_id: str,
                task_id: str, now: datetime) -> Dict[str, Any]:
        why = _text(reason, "reason", 500, required=True)
        with self._tx() as con:
            ag = self._current(con)
            ep = con.execute("SELECT * FROM episode WHERE id=?", (str(episode_id or ""),)).fetchone()
            if ag is None or ep is None or ep["agreement_id"] != ag["id"] or ep["state"] != "open":
                raise CheckinError("stale", "No such open episode for the current agreement.")
            if str(agreement_revision) != str(ag["revision"]):
                raise CheckinError("stale", "The agreement changed since this episode opened.")
            if ep["contact_state"] != "window":
                raise CheckinError("window_closed", f"Nothing to decline ({ep['contact_state']}).")
            if (not schedule_id or ep["window_schedule_id"] != schedule_id
                    or not task_id or ep["window_task_id"] != task_id):
                raise CheckinError("not_window_wake", "Only the grace wake task that opened the contact window "
                                                      "decides.")
            con.execute("UPDATE episode SET contact_state='declined', decision_reason=? WHERE id=?", (why, ep["id"]))
            self._event(con, now, "contact_declined", ep["id"])
        return {"message": "Recorded: no contact for this streak. Tell the owner what you saw and ask them to check in."}

    # ---- status

    def status(self, *, now: datetime, lease_alive: LeaseProbe) -> Dict[str, Any]:
        with self._tx() as con:
            ag = self._maybe_resume(con, self._current(con), now)
            alive = self._lease_ok(con, lease_alive)
            armed, arm_reason = self._arm_state(con, ag, alive)
            out: Dict[str, Any] = {
                "ok": True,
                "now": iso(now),
                "agreement": None,
                "episode": None,
                "contact": {
                    "configured": self._contact_ready(con),
                    "name": self._kv(con, "contact_name"),
                    "email": mask_email(self._kv(con, "contact_email")),
                },
                "mail": {"configured": self._mail_ready(con), "host": self._kv(con, "smtp_host"),
                         "security": self._kv(con, "smtp_security"), "test": self._mail_test(con)},
                "contact_stage": {"armed": armed, "state": arm_reason, "armed_at": self._kv(con, "arm_at") or None},
                "activation": {"lease_alive": alive, "started_at": self._kv(con, "activation_started_at") or None},
                "expected_wake": None,
                "last_wake": None,
                "last_attempt": None,
            }
            last = con.execute("SELECT * FROM wake ORDER BY id DESC LIMIT 1").fetchone()
            if last is not None:
                out["last_wake"] = {"arrived_at": last["arrived_at"], "kind": last["kind"], "outcome": last["outcome"],
                                    "via_registered_schedule": bool(last["matched_registration"])}
            attempt = con.execute("SELECT * FROM attempt ORDER BY claimed_at DESC LIMIT 1").fetchone()
            if attempt is not None:
                state = attempt["state"]
                if state == "sending" and now - parse_iso(attempt["claimed_at"]) > ATTEMPT_UNKNOWN_AFTER:
                    state = "unknown"
                # The newest attempt of any streak or agreement: the ids say which one it belongs to.
                out["last_attempt"] = {"state": state, "episode_id": attempt["episode_id"],
                                       "agreement_id": attempt["agreement_id"], "claimed_at": attempt["claimed_at"],
                                       "finished_at": attempt["finished_at"], "to": attempt["recipient_label"],
                                       "subject": attempt["subject"], "body": attempt["body"],
                                       "reason": attempt["reason"], "detail": attempt["detail"]}
            if out["last_attempt"] is not None:
                # The newest attempt may belong to an earlier streak or a replaced agreement.
                ep_open = self._open_episode(con, ag["id"]) if ag is not None else None
                out["last_attempt"]["for_open_streak"] = (ep_open is not None
                                                          and out["last_attempt"]["episode_id"] == ep_open["id"])
            if ag is None:
                return out
            tz = ag["timezone"]
            out["agreement"] = {
                "id": ag["id"], "kind": ag["kind"], "status": ag["status"], "timezone": tz, "revision": ag["revision"],
                "deadline_local": fmt_local(ag["deadline_utc"], tz) or None, "daily_time": ag["daily_time"],
                "next_deadline_local": fmt_local(ag["next_due_utc"], tz) or None, "next_deadline_utc": ag["next_due_utc"],
                "grace_minutes": ag["grace_minutes"], "lateness_cutoff_minutes": ag["cutoff_minutes"],
                "paused_until_local": fmt_local(ag["paused_until"], tz) or None,
                "last_checkin_local": fmt_local(ag["last_checkin_at"], tz) or None,
                "has_contact_guidance": bool(ag["guidance"]),
            }
            ep = self._open_episode(con, ag["id"])
            out["agreement"].update(self._dated(ag, ep, now))
            if ep is not None:
                contact_state = ep["contact_state"]
                grace_end = parse_iso(ep["grace_ends_at"])
                if grace_end is not None and now - grace_end > timedelta(minutes=int(ag["cutoff_minutes"])):
                    if contact_state == "window":
                        contact_state = "window_expired"   # opened, never decided (for example the wake was stopped)
                    elif contact_state == "waiting":
                        contact_state = "grace_expired"    # the grace wake never arrived; it is never re-planned
                out["episode"] = {
                    "id": ep["id"], "missed_deadlines": ep["missed_count"],
                    "first_missed_local": fmt_local(ep["first_due_utc"], tz),
                    "last_missed_local": fmt_local(ep["last_due_utc"], tz),
                    "notice": ep["notice_state"], "notice_at_local": fmt_local(ep["notice_at"], tz) or None,
                    "contact_notice_at_local": fmt_local(ep["contact_notice_at"], tz) or None,
                    "contact_state": contact_state,
                    "grace_ends_local": fmt_local(ep["grace_ends_at"], tz) or None,
                    "decision_reason": ep["decision_reason"],
                }
            expected = self._expected_wake(con, ag, ep, now)
            if expected is not None:
                kind, at, scope = expected
                registered = [r["schedule_id"] for r in con.execute(
                    "SELECT schedule_id FROM registration WHERE kind=? AND agreement_id=? AND (episode_id IS ? OR ?='deadline') "
                    "ORDER BY id DESC LIMIT 3", (kind, ag["id"], scope, kind))]
                arrived = con.execute("SELECT 1 FROM wake WHERE kind=? AND agreement_id=? AND arrived_at>=?",
                                      (kind, ag["id"], iso(at))).fetchone() is not None
                overdue = (not arrived) and now > at + WAKE_OVERDUE
                out["expected_wake"] = {
                    "kind": kind, "at_local": fmt_local(iso(at), tz), "agent_reported_schedule_ids": registered,
                    "overdue": overdue,
                    "note": ("Expected wake has not arrived: the computer may be asleep or off, the queue busy, or the "
                             "schedule disabled or deleted. Outcome unknown; nothing is guaranteed.") if overdue else
                            ("No wake was reported as registered; nothing will check this deadline." if not registered else
                             "Registration is agent-reported, not verified."),
                }
            return out

    @staticmethod
    def _dated(ag: sqlite3.Row, ep: Optional[sqlite3.Row], now: datetime) -> Dict[str, Any]:
        """The dated view of an agreement: today's deadline (what an ordinary check-in counts
        for while it is ahead), the calendar-nearest deadline a dated check-in can answer now,
        the next unanswered deadline, and the latest missed one. ``answered`` is None when an
        earlier version left it unknown."""
        tz = ag["timezone"]

        def dated(when: Optional[datetime]) -> Optional[Dict[str, Any]]:
            if when is None:
                return None
            text = iso(when)
            return {"due_utc": text, "due_local": fmt_local(text, tz), "label": deadline_label(text, tz)}

        current = parse_iso(ag["next_due_utc"])
        answered_until = parse_iso(ag["last_answered_due_utc"])
        nearest = answered = next_unanswered = None
        if ag["status"] == "active" and ag["kind"] == "once":
            nearest, answered = parse_iso(ag["deadline_utc"]), False
            next_unanswered = nearest if nearest is not None and nearest > now else None
        elif ag["status"] == "active":
            hh, mm = parse_hhmm(ag["daily_time"])
            nearest = daily_at_or_after(now, zone(tz), hh, mm)
            created = parse_iso(ag["created_at"])
            if created is not None and nearest <= created:     # set up at that very deadline: not its own
                nearest = daily_after(nearest, zone(tz), hh, mm)
            if answered_until is not None and nearest <= answered_until:
                answered = True
            elif answered_until is None and current is not None and current > nearest > now:
                answered = None        # answered under an earlier version that did not record it
            else:
                answered = False
            next_unanswered = nearest if current is None else max(current, nearest)
        latest_missed = None
        if ep is not None:
            latest_missed = dict(dated(parse_iso(ep["last_due_utc"])) or {}, processed=True)
        if ag["status"] == "active" and current is not None and current <= now:
            if ag["kind"] == "daily":
                hh, mm = parse_hhmm(ag["daily_time"])
                last = daily_before(now + timedelta(seconds=1), zone(tz), hh, mm)
            else:
                last = current
            latest_missed = dict(dated(last) or {}, processed=False)   # passed; no wake has processed it yet
        near = dated(nearest)
        if near is not None:
            near.update({"agreement_id": ag["id"], "timezone": tz, "answered": answered})
        today = dated(Store._today_deadline(ag, now))
        if today is not None:
            when = parse_iso(today["due_utc"])
            missed_today = ep is not None and parse_iso(ep["last_due_utc"]) >= when
            if answered_until is not None and when <= answered_until:
                today_answered = True
            elif answered_until is None and current is not None and current > when and not missed_today:
                today_answered = None  # answered under an earlier version that did not record it
            else:
                today_answered = False
            today.update({"agreement_id": ag["id"], "timezone": tz, "answered": today_answered,
                          "passed": when <= now})
        return {"next_deadline_label": deadline_label(ag["next_due_utc"], tz) or None,
                "last_answered_deadline_utc": ag["last_answered_due_utc"],
                "today_deadline": today, "nearest_deadline": near,
                "next_unanswered_deadline": dated(next_unanswered), "latest_missed": latest_missed}

    def checkin_form(self, *, now: datetime) -> Dict[str, str]:
        """Values for the Settings form “Check in for displayed deadline” (read on each load).

        The proposal is the calendar-nearest deadline (a one-time agreement's own deadline),
        never moved forward by itself: repeating a submit answers the same deadline again
        (a no-op), and only a reload proposes the next one.
        """
        with self._tx() as con:
            ag = self._maybe_resume(con, self._current(con), now)
            empty = {"agreement_id": "", "for_deadline": "", "deadline_label": ""}
            if ag is None:
                return {**empty, "deadline_status": "No active agreement: nothing to check in for."}
            if ag["status"] != "active":
                return {**empty, "deadline_status": f"Paused until {deadline_label(ag['paused_until'], ag['timezone'])}: "
                                                    "nothing to check in for."}
            dated = self._dated(ag, self._open_episode(con, ag["id"]), now)
            near, missed = dated["nearest_deadline"], dated["latest_missed"]
            if near is None:
                return {**empty, "deadline_status": "No deadline to check in for."}
            if near["answered"] is True:
                state = "Already checked in for this deadline; submitting again changes nothing."
            else:
                if near["answered"] is None:
                    state = ("Not known whether this deadline was answered (recorded by an earlier version); "
                             "submitting checks in for it.")
                elif parse_iso(near["due_utc"]) <= now:
                    state = "This deadline is due now: submitting checks in for it."
                else:
                    state = "Not checked in yet: submitting checks in early for this deadline."
                if missed and missed["due_utc"] != near["due_utc"]:
                    state += f" It also closes the missed check-in for {missed['label']}."
            return {"agreement_id": ag["id"], "for_deadline": near["due_utc"], "deadline_label": near["label"],
                    "deadline_status": state}

    @staticmethod
    def _expected_wake(con, ag, ep, now: datetime) -> Optional[Tuple[str, datetime, Optional[str]]]:
        if ag["status"] != "active":
            return None
        grace_end = parse_iso(ep["grace_ends_at"]) if ep is not None else None
        if (ep is not None and ep["contact_state"] == "waiting" and grace_end is not None
                and now - grace_end <= timedelta(minutes=int(ag["cutoff_minutes"]))):
            return "grace", grace_end, ep["id"]
        due = parse_iso(ag["next_due_utc"])
        if due is None:
            return None
        return "deadline", due, None
