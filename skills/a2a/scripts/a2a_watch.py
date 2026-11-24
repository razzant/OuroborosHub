"""Late letters: a companion that reads confirmed letters' tasks with no model and brings a late answer back once.

It runs as its own companion (``a2a_letters``), apart from the receiving server, and imports neither the A2A SDK
nor the server, so it works while the inbound listener is stopped. It reads only the index of letters still owed
a read or a report (``letters.watched``), never the archive. A read is ``tasks/get`` on the task the peer
confirmed, after the asking call stopped reading it, at a widening interval capped at an hour. Nothing is ever
sent to the peer again; a letter whose send has an unknown outcome has no task and is never indexed. No deadline
drops a waiting letter: it leaves the index when it settles, when the peer no longer has the task, or when a
final task snapshot does not establish this letter's outcome. The letter keeps that distinction.

When the host offers late results (``/identity`` ``late_result_version: 1``) and the letter keeps the host's
reference to the call that asked (``asked_by.call_ref``, from ``ctx.call_ref``), text the peer tied to that
letter (its answer, or once its ``input-required`` request to clarify, which nothing here acts on) is offered to
the local Host Service: ``POST /late_result`` with exactly
``{call_ref, report_id, label, text}`` (the message id, the peer's display name, the whole text). The host
acknowledges an intact inline tool result or hands the report to the original room. Neither proves the model read
the text. It joins a repeat of the same report. The report is marked uncertain, its label and text fixed,
before it is posted, so a crash, a 5xx or an answer without a ``report_ref`` repeats the identical body; 429 waits
``retry_after_s``; any other answer is the host's final word, kept on the letter as the notice's fate, never as
the answer's. A state that is not final, a final state with no text, ``auth-required`` and an outcome not tied
to the letter are never reported. Without the host's offer or the call's reference nothing is posted anywhere:
the answer stays on the letter, read by message id.
"""
from __future__ import annotations

import importlib
import logging
import os
import pathlib
import sys
import time
import types
import urllib.parse
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("a2a_letters")

VERSION = 1                   # the /identity ``late_result_version`` this companion speaks
PATH = "/late_result"
SCAN_SEC = 30.0               # one pass over the index
READS_PER_SCAN = 20           # peer reads in one pass, at most
TIMEOUT_SEC = 20.0            # one peer read, one host request
STEPS = (15, 30, 60, 120, 300, 600, 1200, 2400, 3600)  # seconds between reads of one letter; the last repeats
FACTS_TTL_SEC = 300.0         # how long the host's /identity answer is reused
SETTLED_NOTICE = frozenset({"accepted", "refused"})
_SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _payload(response) -> Dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class LetterWatch:
    """One companion's watch over its install's letters; ``scan()`` is one synchronous pass."""

    def __init__(self, state_dir, *, client, letters, host_url: str, headers: Callable[[], dict],
                 http: Callable[[], Any], transport: Callable[[], Any] = lambda: None,
                 clock: Callable[[], float] = time.time) -> None:
        self.state_dir = pathlib.Path(state_dir)
        self.client, self.letters = client, letters
        self.host_url, self.headers, self.http = host_url.rstrip("/"), headers, http
        self.transport, self.clock = transport, clock
        self.schedule: Dict[str, tuple] = {}  # message id -> (next read at, step index)
        self._facts: Optional[dict] = None
        self._facts_until = 0.0

    def offered(self) -> bool:
        """Whether this host takes late results, from its /identity (reused for a while, retried soon on failure)."""
        now = self.clock()
        if self._facts is None or now >= self._facts_until:
            try:
                response = self.http().get(self.host_url + "/identity", headers=self.headers(), timeout=TIMEOUT_SEC)
                facts = _payload(response) if response.status_code == 200 else {}
            except Exception:  # noqa: BLE001 - an unreadable host offers nothing now; asked again next pass
                facts = {}
            self._facts, self._facts_until = facts, now + (FACTS_TTL_SEC if facts else SCAN_SEC)
        return self._facts.get("late_result_version") == VERSION

    def scan(self) -> Dict[str, int]:
        """Read the due letters of the index, report what is owed, drop what is settled; counts for logs and tests."""
        counts = {"read": 0, "reported": 0, "settled": 0}
        now, offered = self.clock(), None
        for message_id in self.letters.watched(self.state_dir):
            try:
                letter = self.letters.load(self.state_dir, message_id)
            except ValueError:
                continue  # an unreadable letter keeps its mark; nothing is guessed about it
            if letter is None:
                self.letters.unwatch(self.state_dir, message_id, None)
                continue
            if self._waiting(letter):
                if counts["read"] >= READS_PER_SCAN or not self._due(letter, now):
                    continue
                counts["read"] += 1
                letter = self.client.observe(self.state_dir, letter, transport=self.transport(),
                                             timeout=TIMEOUT_SEC) or letter
                if self._waiting(letter):
                    continue
            self.schedule.pop(message_id, None)
            owed = self._owed(letter)
            if owed == "now":
                offered = self.offered() if offered is None else offered
                if offered:
                    counts["reported"] += self._report(letter)
                    letter = self.letters.load(self.state_dir, message_id) or letter
                    owed = self._owed(letter)
            if owed == "never" and now >= float(letter.get("waiting_until") or 0):
                counts["settled"] += self.letters.unwatch(self.state_dir, message_id, letter)
        return counts

    # --- which letters ---------------------------------------------------------------

    def _waiting(self, letter: dict) -> bool:
        """Still to be read: not final, not interrupted (that waits for this side), and the peer still has it."""
        return (bool((letter.get("remote") or {}).get("task_id"))
                and letter.get("state") not in self.letters.TERMINAL | self.letters.INTERRUPTED
                and not letter.get("watch_ended"))

    def _due(self, letter: dict, now: float) -> bool:
        """The asking call no longer reads it, and its interval has passed (a restart reads it at once)."""
        if now < float(letter.get("waiting_until") or 0):
            return False
        next_at, step = self.schedule.get(letter["message_id"], (now, -1))
        if now < next_at:
            return False
        step = min(step + 1, len(STEPS) - 1)
        self.schedule[letter["message_id"]] = (now + STEPS[step], step)
        return True

    def _owed(self, letter: dict) -> str:
        """``now``, ``later`` or ``never``: offer tied text through the original call if the host has a route.
        The host, not the sending process or a status read, knows whether the exact inline result arrived."""
        late, now = letter.get("late") or {}, self.clock()
        if late.get("state") in SETTLED_NOTICE:
            return "never"
        if late.get("state") != "uncertain":  # an uncertain notice is finished with its fixed body, whatever came after
            answered = (letter.get("state") in self.letters.ANSWERING and not letter.get("unconfirmed")
                        and (letter.get("answer") or {}).get("text") and (letter.get("asked_by") or {}).get("call_ref"))
            if not answered:
                return "never"
        if now < float(late.get("next_at") or 0) or now < float(letter.get("waiting_until") or 0):
            return "later"
        return "now"

    # --- the notice --------------------------------------------------------------------

    def _claim(self, letter: dict) -> Optional[dict]:
        """Mark the notice uncertain before it crosses to the host, rechecked under the letter's lock."""
        claimed = []

        def change(current: dict):
            if self._owed(current) != "now":
                return None
            late = dict(current.get("late") or {})
            attempts = int(late.get("attempts") or 0) + 1
            if "text" not in late:  # fixed at the first attempt: every repeat carries the same label and text
                late.update(label=self.client.late_label(self.state_dir, current), text=current["answer"]["text"])
            late.update(state="uncertain", attempts=attempts, at=self.letters.now(),
                        next_at=self.clock() + STEPS[min(attempts, len(STEPS)) - 1])
            claimed.append(True)
            return {"late": late}

        merged = self.letters.update(self.state_dir, letter["message_id"], change)
        return merged if claimed else None

    def _settle(self, letter: dict, **fields) -> None:
        def change(current: dict):
            late = dict(current.get("late") or {})
            if late.get("state") in SETTLED_NOTICE:
                return None
            late.update(fields, at=self.letters.now())
            return {"late": late}
        self.letters.update(self.state_dir, letter["message_id"], change)

    def _report(self, letter: dict) -> int:
        letter = self._claim(letter)
        if letter is None:
            return 0
        # Exactly these four fields, the same on every repeat: label and text were fixed at the first attempt.
        late = letter["late"]
        body = {"call_ref": letter["asked_by"]["call_ref"], "report_id": letter["message_id"],
                "label": late["label"], "text": late["text"]}
        try:
            response = self.http().post(self.host_url + PATH, json=body, headers=self.headers(), timeout=TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 - it may have landed: the same report is repeated later
            logger.warning("a2a late answer %s: the host's receipt is unknown (%s)", letter["message_id"],
                           type(exc).__name__)
            return 0
        status, answer = response.status_code, _payload(response)
        receipt = answer.get("report_ref")
        if status in (200, 202) and answer.get("ok") is True and isinstance(receipt, str) and receipt:
            delivery = answer.get("delivery")
            self._settle(letter, state="accepted", report_ref=receipt, next_at=0,
                         delivery=delivery if delivery in ("tool_result", "handed_off") else "")
            return 1
        if status == 429:
            wait = answer.get("retry_after_s")
            later = float(wait) if type(wait) in (int, float) and wait > 0 else float(STEPS[-1])
            self._settle(letter, state="retry", next_at=self.clock() + later)
            return 0
        if status >= 500 or status in (200, 202):
            return 0  # no receipt: still uncertain, and only the same report may follow
        self._settle(letter, state="refused", code=str(answer.get("code") or f"HTTP {status}"), next_at=0)
        logger.info("a2a late answer %s: the host declined the notice (HTTP %s)", letter["message_id"], status)
        return 0


# --- the companion ----------------------------------------------------------------------


def _skill_lib(name: str):
    """A module of the skill's own library (``lib/``), imported as a package under a private name."""
    package = "_a2a_letters_payload"
    if package not in sys.modules:
        module = types.ModuleType(package)
        module.__path__ = [str(_SKILL_ROOT)]
        sys.modules[package] = module
    return importlib.import_module(f"{package}.lib.{name}")


class _Token:
    """The skill token, kept out of logs and reprs."""

    __slots__ = ("_value",)

    def __init__(self, raw: str) -> None:
        self._value = raw

    def use_in_request(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<SkillToken:redacted>"

    __str__ = __repr__


def build(environ=os.environ, *, http: Callable[[], Any] = None, transport: Callable[[], Any] = lambda: None,
          clock: Callable[[], float] = time.time) -> LetterWatch:
    """The watch this companion runs, from the host's environment; the skill token goes only to a loopback host."""
    links = _skill_lib("links")
    host_url = str(environ.get("HOST_SERVICE_URL") or "http://127.0.0.1:8767").rstrip("/")
    parts = urllib.parse.urlsplit(host_url)
    if parts.username or parts.password or not links.is_loopback(parts.hostname or ""):
        raise RuntimeError("HOST_SERVICE_URL must be a loopback address without user info; "
                           "refusing to send the skill token elsewhere")
    token = _Token(str(environ.get("HOST_SERVICE_TOKEN") or ""))
    if http is None:
        import httpx

        http = lambda: httpx  # noqa: E731
    return LetterWatch(pathlib.Path(environ.get("OUROBOROS_SKILL_STATE_DIR") or "."), client=_skill_lib("client"),
                       letters=_skill_lib("letters"), host_url=host_url,
                       headers=lambda: {"X-Skill-Token": token.use_in_request()}, http=http,
                       transport=transport, clock=clock)


def main(sleep: Callable[[float], None] = time.sleep) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    watch = build()
    while True:
        try:
            counts = watch.scan()
            if counts["reported"] or counts["settled"]:
                logger.info("a2a letters: %s", counts)
        except Exception as exc:  # noqa: BLE001 - one failed pass must not end the watch
            logger.warning("a2a letters: a pass failed: %s", exc)
        sleep(SCAN_SEC)


if __name__ == "__main__":
    main()
