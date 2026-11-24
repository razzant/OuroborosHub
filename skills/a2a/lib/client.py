"""The A2A wire for the tool child: discover, send and status, over saved links and durable letters.

Usable by the tool child alone: nothing here imports the receiving companion or the host's Presence
protocol. It speaks A2A 0.3 JSON-RPC (``message/send`` with ``blocking: false``, then ``tasks/get``); a
peer that speaks only 1.0 without 0.3 compatibility stays unreachable, as before.

A letter and its uncertain attempt are saved before the first request. An answer slower than one tool call
is read later with ``status``; a status read never sends a message. A handle prepared by this process is
not proof that the asking work received it. Only the host can confirm that from the original call result.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import ssl
import time
import urllib.parse
import uuid
from typing import Any, NamedTuple

from . import letters, links

# The budget plugin.py declares for send and status. The host kills the child at that deadline, which
# also covers interpreter start and imports, so waiting ends START_MARGIN_SEC earlier, leaving room to
# save the letter and return its handle.
TOOL_TIMEOUT_SEC = 130
START_MARGIN_SEC = 20
POLL_INTERVAL_SEC = 2.0
REQUEST_TIMEOUT_SEC = 20.0
CARD_TIMEOUT_SEC = 10
CALL_REF_MAX = 512     # the host's opaque reference to the asking tool call (``ctx.call_ref``), kept as given
TASK_NOT_FOUND = -32001  # A2A JSON-RPC TaskNotFoundError: the peer no longer has the task
_CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")
_FAILED_STATE = {"unsent": "unsent", "refused": "refused", "unknown": "sent_no_handle"}
_REFUSAL_CODES = {-32700, -32600, -32601, -32602, TASK_NOT_FOUND}
_LEGACY_ENV = "legacy:client-env"  # local binding only; a saved Link's generated uid cannot be this value
_UNTIED_TERMINAL = "the peer's task ended without an outcome attributable to this letter"


class Target(NamedTuple):
    endpoint: str
    link: links.Link | None
    auth: tuple[str, str] | None
    verify: Any
    note: str
    legacy_env: bool = False

    @property
    def peer(self) -> str:
        return self.link.id if self.link else self.endpoint

    @property
    def binding(self) -> str:
        return self.link.uid if self.link else (_LEGACY_ENV if self.legacy_env else "")


class Reply(NamedTuple):
    kind: str     # result | unsent (nothing reached the peer) | refused (it said no) | unknown (no clear answer)
    result: Any
    error: str    # a short reason, never a secret
    code: int | None = None
    request_id: str = ""
    rpc_error: dict | None = None


def _target(endpoint: str, link: links.Link | None, *, legacy_secret: str = "") -> Target:
    auth = (link.send_username or links.DEFAULT_USERNAME, link.send_secret) if link and link.send_secret else None
    if legacy_secret:
        auth = (links.DEFAULT_USERNAME, legacy_secret)
    https = endpoint.startswith("https://")
    if https:
        note = "https"
    elif links.is_loopback(urllib.parse.urlsplit(endpoint).hostname or ""):
        note = "loopback"
    else:
        note = "plain http" if auth else "http"  # "plain http": a password went over an unencrypted network link
    verify: Any = True
    if link and link.ca_file and https:
        try:
            verify = ssl.create_default_context(cafile=link.ca_file)
        except (OSError, ssl.SSLError):
            raise links.LinkError(f"the CA file of link {link.id!r} cannot be read") from None
    return Target(endpoint, link, auth, verify, note, bool(legacy_secret))


def _legacy_target(endpoint: str) -> Target | None:
    """Keep the manual 1.5 pair on its exact configured endpoint, without adding a saved link."""
    configured = links.endpoint(os.environ.get("A2A_CLIENT_PEER_URL", ""))
    if not configured or endpoint != configured:
        return None
    secret = os.environ.get("A2A_CLIENT_PASSWORD", "").strip()
    return _target(endpoint, None, legacy_secret=secret) if secret else None


def target(state_dir, url: str) -> Target:
    """Where a new call goes, with the saved credential only for a stable link's exact endpoint."""
    endpoint, link = links.resolve(state_dir, url)
    if link and not link.uid:
        raise links.LinkError(f"save link {link.id!r} in Settings before sending or reading messages")
    return (_legacy_target(endpoint) if link is None else None) or _target(endpoint, link)


def _request(tgt: Target, method: str, *, body=None, timeout: float, transport=None):
    import httpx

    # There is one explicit target per request. Redirects never carry its password elsewhere.
    with httpx.Client(transport=transport, verify=tgt.verify, timeout=timeout, follow_redirects=False) as http:
        return http.request(method, tgt.endpoint, json=body, auth=tgt.auth)


def _rpc(tgt: Target, method: str, params: dict, *, timeout: float, transport=None) -> Reply:
    import httpx

    body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
    try:
        response = _request(tgt, "POST", body=body, timeout=timeout, transport=transport)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        return Reply("unsent", None, f"could not connect ({type(exc).__name__})")
    except httpx.HTTPError as exc:
        return Reply("unknown", None, f"no answer ({type(exc).__name__})")
    status = response.status_code
    if status in (401, 403):
        return Reply("refused", None, f"authentication refused (HTTP {status})")
    if 300 <= status < 400:
        return Reply("unknown", None, f"the peer redirected (HTTP {status}); redirects are not followed, so use the "
                                      "exact address its Agent Card gives")
    http_error = f"HTTP {status}" if status >= 400 else ""
    if http_error and method != "tasks/get":
        return Reply("unknown", None, http_error)
    try:
        data = response.json()
    except ValueError:
        return Reply("unknown", None, http_error or "the answer is not JSON")
    if not isinstance(data, dict) or data.get("jsonrpc") != "2.0" or data.get("id") != body["id"] \
            or ("result" in data) == ("error" in data):
        return Reply("unknown", None, http_error or "no matching JSON-RPC response")
    if "error" in data:
        error = data["error"] if isinstance(data["error"], dict) else {}
        code = error.get("code")
        code = code if type(code) is int else None
        # Only an explicit protocol refusal settles this attempt as a refusal. An internal or unknown
        # error can follow acceptance.
        wire_error = error if code is not None and isinstance(error.get("message"), str) else None
        if http_error:
            return Reply("unknown", None, http_error, request_id=data["id"], rpc_error=wire_error)
        return Reply("refused" if code in _REFUSAL_CODES else "unknown", None, f"JSON-RPC {code}", code,
                     data["id"], wire_error)
    return (Reply("unknown", None, http_error) if http_error else
            Reply("result", data["result"], "", request_id=data["id"]))


def _texts(parts) -> str:
    if not isinstance(parts, list):
        return ""
    return "\n".join(part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str))


def _delivery(value) -> dict | None:
    if isinstance(value, dict) and isinstance(value.get("class"), str):
        return {"class": value["class"], "text": str(value.get("text") or "")}
    return None


def _standard_reply(result: dict, status: dict, message_id: str, sole_letter: bool,
                    follows_question) -> tuple[bool, str, str]:
    """Read this letter's outcome and, for an input request, the peer's question id."""
    history = result.get("history")
    if not isinstance(history, list):
        return False, "", ""
    users = [(index, item.get("messageId")) for index, item in enumerate(history)
             if isinstance(item, dict) and item.get("role") == "user"]
    if not users or users[-1][1] != message_id or any(wire == message_id for _, wire in users[:-1]):
        return False, "", ""
    message, state = status.get("message"), status.get("state")
    reply = message.get("messageId") if isinstance(message, dict) and message.get("role") == "agent" else None
    index = users[-1][0]
    fresh = isinstance(reply, str) and bool(reply) and reply != message_id and not any(
        isinstance(item, dict) and item.get("messageId") == reply for item in history[:index])
    text = _texts(message.get("parts")) if fresh and state in ("completed", "input-required") else ""
    question = reply if text.strip() and state == "input-required" else ""
    if sole_letter and len(users) == 1:
        artifact = _artifact_text(result)
        return True, (text or artifact) if state == "input-required" else (artifact or text), question
    if not text.strip():
        return False, "", ""
    attached = any(
        isinstance(item, dict) and item.get("role") == "agent" and item.get("messageId") == reply
        and _texts(item.get("parts")) == text for item in history[index + 1:])
    # The SDK puts the previous input request immediately before this input but can keep the *new*
    # question (or final answer) in status alone. Join that exact previous question to our saved receipt.
    if not attached and len(users) > 1 and follows_question is not None:
        previous = history[index - 1]
        if isinstance(previous, dict) and previous.get("role") == "agent":
            attached = follows_question(users[-2][1], previous.get("messageId"), _texts(previous.get("parts")))
    return (True, text, question) if attached else (False, "", "")


def _artifact_text(result: dict) -> str:
    artifacts = result.get("artifacts")
    return "\n".join(text for item in artifacts if isinstance(item, dict) and (text := _texts(item.get("parts")))) \
        if isinstance(artifacts, list) else ""


def reading(result: Any, message_id: str, *, sent=False, task_id="", context_id="", sole_letter=False,
            follows_question=None) -> dict:
    """Read the same evidence on send and get. A direct agent Message is the matched send response."""
    if not isinstance(result, dict):
        return {}
    if result.get("kind") == "message" or ("parts" in result and "status" not in result):
        if not sent or result.get("role") != "agent" or not result.get("messageId"):
            return {}
        remote = {"task_id": str(result.get("taskId") or ""), "context_id": str(result.get("contextId") or "")}
        if (task_id and remote["task_id"] not in ("", task_id)
                or context_id and remote["context_id"] not in ("", context_id)):
            return {}
        answer = _texts(result.get("parts"))
        return {"remote": remote, "state": "completed", "answer": answer if answer.strip() else "",
                "delivery": None, "unconfirmed": False}
    remote = {"task_id": str(result.get("id") or ""), "context_id": str(result.get("contextId") or "")}
    if not remote["task_id"] or (task_id and remote["task_id"] != task_id) or (
            context_id and remote["context_id"] != context_id):
        return {}
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    own = metadata.get("ouroboros") if isinstance(metadata.get("ouroboros"), dict) else {}
    answers = own.get("answers") if isinstance(own.get("answers"), dict) else {}
    entry = answers.get(message_id)
    if isinstance(entry, dict) and isinstance(entry.get("state"), str) \
            and entry["state"] in letters.TERMINAL | letters.INTERRUPTED:
        answer = entry.get("answer")
        text = answer if isinstance(answer, str) and answer.strip() and entry["state"] in letters.ANSWERING else ""
        return {"remote": remote, "state": entry["state"], "answer": text,
                "delivery": _delivery(entry.get("delivery")), "unconfirmed": False}
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    state = str(status.get("state") or "")
    if state not in (letters.TERMINAL | letters.INTERRUPTED | {"submitted", "working", "unknown"}):
        return {}
    current = own.get("message_id")
    pending = own.get("pending_message_ids")
    if current == message_id and state not in letters.TERMINAL | letters.INTERRUPTED:
        return {"remote": remote, "state": state, "delivery": _delivery(own.get("delivery"))}
    if isinstance(pending, list) and message_id in pending:
        # A later letter may already be final while this older one is still owed a read.
        return {"remote": remote, "state": "submitted", "delivery": None}
    if state not in letters.TERMINAL | letters.INTERRUPTED:
        if not current:
            return {"remote": remote, "state": state, "delivery": _delivery(own.get("delivery"))}
        return {"remote": remote}
    question_id = ""
    if own:
        # A legacy Ouroboros Task has only its current message binding. A v2 projection
        # explicitly lists pending letters and their separate settled receipts instead.
        tied = current == message_id and not isinstance(pending, list)
        answer = _artifact_text(result) if tied else ""
    elif isinstance(result.get("ouroboros"), dict) and result["ouroboros"].get("client_message_id"):
        # The standalone 1.5 receiver predates SDK metadata and binds the task at the record's top level.
        tied = result["ouroboros"]["client_message_id"] == letters.legacy_client_message_id(remote["task_id"], message_id)
        answer = _artifact_text(result) if tied else ""
    else:
        tied, answer, question_id = _standard_reply(result, status, message_id, sole_letter, follows_question)
    if not tied:
        return {"remote": remote, "unconfirmed": True, "untied_terminal": True}
    # Blank is no answer; any other answer is kept exactly as the peer wrote it, its spaces and newlines included.
    return {"remote": remote, "state": state, "answer": answer if answer.strip() and state in letters.ANSWERING else "",
            "delivery": _delivery(own.get("delivery")), "question_id": question_id, "unconfirmed": False}


def _commit(state_dir, letter: dict, seen: dict, *, error: str = "", failed: str = "", attempt: int = 0,
            stop_unconfirmed: bool = False) -> dict:
    """Commit what a request learned. A task id a reply discloses is indexed first (its remote task, and the
    watch index of confirmed letters); an answer is kept only from a final or interrupted state (an interim
    snapshot's artifact is not this letter's answer)."""
    def change(current: dict) -> dict:
        new: dict = {"last_error": error}
        if attempt:
            if current.get("attempts") != attempt:
                return {}  # a newer, deliberate attempt owns this dispatch record
            dispatch_state = "uncertain" if failed == "sent_no_handle" else (failed or "accepted")
            new["dispatch"] = {"state": dispatch_state, "at": letters.now()}
            if dispatch_state in ("unsent", "refused"):
                new["dispatch"]["retry_safe"] = True   # _begin_send excluded possible earlier acceptance
        if failed and not (current.get("remote") or {}).get("task_id"):
            new["state"] = failed
        if remote := seen.get("remote"):
            known = current.get("remote") or {}
            if any(known.get(key) and remote.get(key) and known[key] != remote[key]
                   for key in ("task_id", "context_id")):
                return {**new, "last_error": "the answer named a different task or context"}
            letters.note_task(state_dir, current, remote.get("task_id") or "")
            if remote.get("task_id") or seen.get("answer"):
                letters.watch(state_dir, current["message_id"])  # never trails the confirmed task id it names
            new["remote"] = remote
        if seen.get("state"):
            new.update(state=seen["state"], delivery=seen.get("delivery"), unconfirmed=bool(seen.get("unconfirmed")))
            if not new["unconfirmed"] and seen.get("answer") and seen["state"] in letters.TERMINAL | letters.INTERRUPTED:
                new["answer"] = {"text": seen["answer"], "at": letters.now()}
                if seen.get("question_id"):
                    new["question_id"] = seen["question_id"]
        elif seen.get("unconfirmed"):
            new["unconfirmed"] = True
            if stop_unconfirmed and seen.get("untied_terminal") and current.get("state") not in (
                    letters.TERMINAL | letters.INTERRUPTED) and not current.get("watch_ended"):
                new["watch_ended"] = {"at": letters.now(), "reason": _UNTIED_TERMINAL}
        return new
    return letters.update(state_dir, letter["message_id"], change)


def _status_call(letter: dict) -> str:
    return f"status(url={json.dumps(letter['url'])}, message_id={json.dumps(letter['message_id'])})"


def _later(letter: dict) -> str:
    """How a later answer reaches the work that asked: brought back by the host, or only read by number."""
    if (letter.get("asked_by") or {}).get("call_ref"):
        return (f"The watch can offer a tied answer back through the original call; the host decides whether it "
                f"reaches the asking work. {_status_call(letter)} also reads it.")
    return ("This host is not told of later answers, so nothing will bring it back: read it later with "
            f"{_status_call(letter)} (this install keeps checking the peer, so "
            f"status(message_id={json.dumps(letter['message_id'])}) alone shows it once it is here).")


def _note(letter: dict) -> str:
    state, error = letter.get("state"), letter.get("last_error") or ""
    retry = (f"Retry with send(url={json.dumps(letter['url'])}, message=<the same text>, "
             f"message_id={json.dumps(letter['message_id'])}).")
    ended = (letter.get("watch_ended") or {}).get("reason")
    if ended and state not in letters.TERMINAL | letters.INTERRUPTED:
        task = (letter.get("remote") or {}).get("task_id")
        snapshot = (f" Read the task snapshot with status(url={json.dumps(letter['url'])}, "
                    f"task_id={json.dumps(task)})." if task else "")
        return (f"Automatic checks ended without a matched answer: {ended}. "
                f"{_status_call(letter)} shows the letter.{snapshot}")
    if letter.get("unconfirmed"):
        return f"The peer has not tied a final outcome to this letter yet. {_later(letter)}"
    if state == "unsent":
        return (f"Nothing reached the peer ({error}). {retry}" if letters.retry_safe(letter) else
                "The latest attempt did not send; an earlier attempt remains unknown. This letter will not be repeated.")
    if state == "saved":
        return f"Saved locally; no send attempt is recorded. {retry}"
    if state == "refused":
        return f"The peer refused the message ({error})."
    if state == "sent_no_handle":
        return f"Send outcome unknown ({error}); the peer may have the message, so this letter will not be repeated."
    if state == "submitted":
        return f"The peer acknowledged the task; an answer is not confirmed. {_later(letter)}"
    if state in letters.INTERRUPTED:
        return ("The peer needs more from you: reply with send(url=..., message=..., task_id=..., context_id=...) "
                "naming this task.")
    if state not in letters.TERMINAL:
        return f"The peer is still working on it. {_later(letter)}"
    return (letter.get("delivery") or {}).get("text") or ""


def _handle(state_dir, letter: dict, *, note: str = "", origin: dict | None = None) -> str:
    remote = letter.get("remote") or {}
    handle = {"message_id": letter["message_id"], "peer": letter.get("peer") or letter.get("endpoint"),
              "url": letter.get("endpoint"), "task_id": remote.get("task_id") or "",
              "context_id": remote.get("context_id") or "", "state": letter.get("state"),
              "delivery": letter.get("delivery"), "link": letter.get("link") or "",
              "status_call": _status_call(letter), "note": note or _note(letter)}
    if letter.get("unconfirmed"):
        handle["unconfirmed"] = True
    dispatch = letter.get("dispatch") or {}
    if dispatch:
        handle["dispatch"] = {"state": dispatch["state"], "at": dispatch["at"]}
    answer = (letter.get("answer") or {}).get("text")
    if answer:
        handle["answer"] = answer
    if origin is not None:  # only the opted-in send tool declares what this call can return inline
        ref = origin.get("call_ref")
        exact_call = ref and ref == (letter.get("asked_by") or {}).get("call_ref")
        handle["late_answer"] = ({"report_id": letter["message_id"], "label": late_label(state_dir, letter),
                                  "text": answer} if exact_call and answer and not letter.get("unconfirmed")
                                 and letter.get("state") in letters.ANSWERING else None)
    late = letter.get("late") or {}
    if late.get("state") in ("accepted", "refused"):
        # The late notice's own fate; a refused notice leaves the answer above as it is.
        handle["late_notice"] = {key: late[key] for key in ("state", "code", "report_ref", "delivery") if late.get(key)}
        if late["state"] == "refused" and not note:
            handle["note"] = ("The host did not take the late notice (it may reject text over its limit). "
                              "The complete answer stays on this letter; automatic delivery is not confirmed.")
    return json.dumps(handle, ensure_ascii=False, indent=2)


@contextmanager
def _reading(state_dir, letter: dict, deadline: float, clock):
    """Defer the watch while this call reads; an ordinary return releases its own lease immediately."""
    message_id, reader = letter["message_id"], uuid.uuid4().hex
    until = time.time() + max(0.0, deadline - clock()) + START_MARGIN_SEC
    current = letters.update(state_dir, message_id, lambda saved: (
        {"waiting_until": until, "reader": reader} if until > float(saved.get("waiting_until") or 0) else None))
    try:
        yield current
    finally:
        letters.update(state_dir, message_id, lambda saved: (
            {"waiting_until": 0, "reader": ""} if saved.get("reader") == reader else None))


def _begin_send(state_dir, letter: dict, *, until: float, link_note: str) -> tuple[dict, int]:
    """Record the attempt as uncertain before POST, with no replay after an interrupted call."""
    attempt = 0

    def change(current):
        nonlocal attempt
        if not letters.retry_safe(current):
            return None  # possible or known peer acceptance; use status instead
        if float((current.get("dispatch") or {}).get("until") or 0) > time.time():
            return None  # another call still owns the send window
        attempt = int(current.get("attempts") or 0) + 1
        letters.note_task(state_dir, current, current.get("task_id") or "")
        return {"state": "sent_no_handle", "attempts": attempt, "link": link_note,
                "last_error": "a send was attempted without a matched receipt yet",
                "dispatch": {"state": "uncertain", "at": letters.now(), "until": until}}

    return letters.update(state_dir, letter["message_id"], change), attempt


def _error(message: str, **facts) -> str:
    return json.dumps({"error": message, **facts}, ensure_ascii=False, indent=2)


def _origin(asked_by) -> dict:
    """Who asked, kept as facts. ``call_ref`` is the host's own reference to the asking tool call
    (``ctx.call_ref``), present only when the host gives one: the late report names it, and the host alone
    resolves it to a room."""
    asked = asked_by if isinstance(asked_by, dict) else {}
    chat = asked.get("chat_id")
    origin = {"task_id": str(asked.get("task_id") or ""),
              "chat_id": chat if isinstance(chat, int) and not isinstance(chat, bool) else None,
              "project_id": str(asked.get("project_id") or "")}
    ref = asked.get("call_ref")
    if isinstance(ref, str) and 0 < len(ref) <= CALL_REF_MAX and ref.isprintable():
        origin["call_ref"] = ref
    return origin


def _same_destination(letter: dict, url: str, tgt: Target | None) -> str:
    """The selector names the original endpoint, or the original link as it now stands."""
    selector = str(url or "").strip()
    if links.endpoint(selector) != letter.get("endpoint") and not (
            tgt and tgt.link and selector == tgt.link.id):
        return (f"this letter went to {letter.get('endpoint')} (url {letter.get('url')!r}); pass that address, or "
                "the same saved link, as url")
    return ""


def _mismatch(letter: dict, text: str, task_id: str, context_id: str) -> str:
    """Why these arguments do not name this letter ("" when they do): its destination, its exact words, and any
    task or context id it was sent with or given."""
    remote = letter.get("remote") or {}
    if text != letter.get("text"):
        return "this letter has different text; pass the exact text it was sent with"
    if task_id and task_id not in (letter.get("task_id"), remote.get("task_id")):
        return "task_id differs from this letter's task"
    if context_id and context_id not in (letter.get("context_id"), remote.get("context_id")):
        return "context_id differs from this letter's context"
    return ""


def _await(state_dir, letter: dict, tgt: Target, deadline: float, *, transport, clock, sleep) -> dict:
    """Poll ``tasks/get`` until the letter settles, is interrupted, or the budget leaves no room for one more poll."""
    while (letter.get("state") not in letters.TERMINAL | letters.INTERRUPTED and not letter.get("watch_ended")
           and (letter.get("remote") or {}).get("task_id") and clock() + POLL_INTERVAL_SEC < deadline):
        sleep(POLL_INTERVAL_SEC)
        letter = _read_task(state_dir, letter, tgt, deadline, transport=transport, clock=clock)
    return letter


def _read_task(state_dir, letter: dict, tgt: Target, deadline: float, *, transport, clock) -> dict:
    remote = letter["remote"]
    task_id = remote["task_id"]
    # Request the full history: an ordinary peer may attach the continuation's own status to it.
    reply = _rpc(tgt, "tasks/get", {"id": task_id},
                 timeout=max(0.001, min(REQUEST_TIMEOUT_SEC, deadline - clock())), transport=transport)
    if reply.kind != "result":
        letter = _commit(state_dir, letter, {}, error=reply.error)
        return (_end_watch(state_dir, letter, "the peer no longer has this task")
                if reply.code == TASK_NOT_FOUND else letter)
    seen = _read_reply(state_dir, letter, reply.result)
    return _commit(state_dir, letter, seen, error="" if seen else "no matching peer task and context came back",
                   stop_unconfirmed=True)


def _read_reply(state_dir, letter: dict, result: Any, *, sent=False) -> dict:
    remote = letter.get("remote") or {}
    expected_task = remote.get("task_id") or letter.get("task_id") or ""
    expected_context = remote.get("context_id") or letter.get("context_id") or ""
    task_id = (result.get("id") if isinstance(result, dict) else "") or ""
    reply_context = result.get("contextId") if isinstance(result, dict) else None
    sole = isinstance(task_id, str) and letters.only_on_task(state_dir, letter, task_id, first_receipt=sent)

    def follows_question(previous, question, text):
        return letters.follows_question(state_dir, letter, expected_task, reply_context, previous, question, text)

    return reading(result, letter["message_id"], sent=sent, task_id=expected_task, context_id=expected_context,
                   sole_letter=sole, follows_question=follows_question)


def _end_watch(state_dir, letter: dict, reason: str) -> dict:
    return letters.update(state_dir, letter["message_id"], lambda _current: {"watch_ended": {
        "at": letters.now(), "reason": reason}})


def _bound_target(state_dir, letter: dict) -> Target | str | None:
    """One immutable binding for watch, manual read and deliberate send.

    A letter sent with no link's credential is read at its own address with none, even if a link has taken that
    address since. A letter sent through a link (``link_uid``, its identity) is read only through that same
    identity at the same address: a rename or a new password keeps it; an address that link no longer names waits
    (None) for it to name it again; a link removed, or an id that now names another agent, ends the wait (its
    reason as a string), and no other identity's credential reads the letter.
    """
    uid = str(letter.get("link_uid") or "")
    endpoint = str(letter.get("endpoint") or "")
    if not links.endpoint(endpoint):
        return "the original address is unavailable"
    if uid == _LEGACY_ENV:
        return _legacy_target(endpoint)  # no credential from a saved link can replace this pair
    if not uid:
        return _target(endpoint, None)
    try:
        link = links.by_principal(state_dir, links.PRINCIPAL_PREFIX + uid)
    except links.LinkError:
        return None  # unreadable links: read nothing now, decide nothing
    if link is None:
        return "the link it was sent through was removed"
    if link.url != endpoint:
        return None  # this link no longer names the saved address; no credential follows it
    try:
        return _target(endpoint, link)
    except links.LinkError:
        return None


def observe(state_dir, letter: dict, *, transport=None, timeout: float = 20.0) -> dict | None:
    """One read of a confirmed letter's task for the companion's watch: ``tasks/get``, nothing sent again.

    None when there is nothing to read safely now (no confirmed task, or its link names another address now);
    a later pass tries again. A peer that no longer has the task, a task it ended about another message, or a
    link that no longer exists ends the wait for good, and the letter says why (``watch_ended``): no answer can
    come for it, and that is never left silent.
    """
    if not (letter.get("remote") or {}).get("task_id"):
        return None
    tgt = _bound_target(state_dir, letter)
    if isinstance(tgt, str):
        return _end_watch(state_dir, letter, tgt)
    if tgt is None:
        return None
    return _read_task(state_dir, letter, tgt, time.monotonic() + timeout, transport=transport,
                      clock=time.monotonic)


def late_label(state_dir, letter: dict) -> str:
    """The peer's display name for a late report: the name the link it was sent through declares now (a rename
    keeps the same peer), else its address. A display name only, never an identity or authority."""
    uid = str(letter.get("link_uid") or "")
    try:
        link = links.by_principal(state_dir, links.PRINCIPAL_PREFIX + uid) if uid and uid != _LEGACY_ENV else None
    except links.LinkError:
        link = None
    return (link.label or link.id) if link is not None else str(letter.get("endpoint") or letter.get("peer") or "")


def discover(state_dir, url: str = "", *, transport=None) -> str:
    """Fetch a peer's Agent Card; with no url, list the saved links (never their passwords)."""
    if not str(url or "").strip():
        try:
            rows = [link.public() for link in links.load(state_dir)]
        except links.LinkError as exc:
            return _error(str(exc))
        return json.dumps({"links": rows, "note": "Pass a link id or an http(s) address as url to send, discover "
                                                   "or check status."}, ensure_ascii=False, indent=2)
    try:
        endpoint = links.endpoint(url)
        tgt = _target(endpoint, None) if endpoint else target(state_dir, url)
    except links.LinkError as exc:
        return _error(str(exc))
    import httpx

    problem = ""
    # Only these two documented GETs can carry a credential, and only for a link selected by name.
    # Neither a bare URL nor a field in the card names a saved link.
    for path in _CARD_PATHS:
        try:
            card_target = tgt._replace(endpoint=tgt.endpoint.removesuffix("/") + path)
            response = _request(card_target, "GET", timeout=CARD_TIMEOUT_SEC, transport=transport)
        except httpx.HTTPError as exc:
            problem = f"could not fetch the agent card ({type(exc).__name__})"
            continue
        if response.status_code == 200:
            try:
                card = response.json()
            except ValueError:
                card = None
            if isinstance(card, dict):
                return json.dumps({
                    "name": card.get("name", ""), "description": card.get("description", ""),
                    "version": card.get("version", ""), "url": card.get("url", ""),
                    "capabilities": card.get("capabilities", {}), "skills": card.get("skills", []),
                    "securitySchemes": card.get("securitySchemes", {}), "security": card.get("security", []),
                    "peer": tgt.peer,
                    "endpoint": tgt.endpoint, "link": tgt.note,
                }, ensure_ascii=False, indent=2)
        problem = f"the agent card answered HTTP {response.status_code}"
    return _error(f"Failed to fetch agent card: {problem}", peer=tgt.peer, link=tgt.note)


def send(state_dir, url: str, message: str, *, task_id: str = "", context_id: str = "", message_id: str = "",
         wait: bool = True, asked_by=None, budget: float | None = None, transport=None,
         clock=time.monotonic, sleep=time.sleep) -> str:
    """Send one letter without blocking for the answer and return its handle.

    ``message_id`` names an existing letter. Only a provably unaccepted one may be sent again, with the same
    url and exact text, so the destination and the words stay visible to the host's checks.
    """
    deadline = clock() + (TOOL_TIMEOUT_SEC - START_MARGIN_SEC if budget is None else budget)
    if not wait:
        deadline = min(deadline, clock() + REQUEST_TIMEOUT_SEC)
    text = str(message or "")
    if message_id and not letters.valid_id(message_id):
        return _error("message_id is 1-128 letters, digits, '.', '_', ':' or '-'", message_id=message_id,
                      late_answer=None)
    origin = _origin(asked_by)
    letter = letters.load(state_dir, message_id) if message_id else None
    if letter is None:
        try:
            tgt = target(state_dir, url)
        except links.LinkError as exc:
            return _error(str(exc), state="unsent", message_id=message_id or "", late_answer=None)
        letter, _created = letters.create(state_dir, {
            "message_id": message_id or letters.new_id(), "url": str(url).strip(), "endpoint": tgt.endpoint,
            "peer": tgt.peer, "link_uid": tgt.binding, "text": text,
            "task_id": str(task_id or ""), "context_id": str(context_id or ""), "asked_by": origin,
            "link": tgt.note})
    # Checked on every path, a lost creation race included: what is sent is exactly what this call names.
    problem = _mismatch(letter, text, task_id, context_id)
    if problem:
        return _error(problem, message_id=letter["message_id"], state=letter.get("state"), late_answer=None)
    tgt = _bound_target(state_dir, letter)
    if not isinstance(tgt, Target):
        return _error(tgt or "the original binding is not available at this address now", message_id=letter["message_id"],
                      state=letter.get("state"), late_answer=None)
    if problem := _same_destination(letter, url, tgt):
        return _error(problem, message_id=letter["message_id"], state=letter.get("state"), late_answer=None)
    if deadline <= clock():
        return _handle(state_dir, letter, origin=origin)
    with _reading(state_dir, letter, deadline, clock) as letter:
        until = time.time() + max(0.0, deadline - clock()) + START_MARGIN_SEC
        letter, attempt = _begin_send(state_dir, letter, until=until, link_note=tgt.note)
        if not attempt:
            return _handle(state_dir, letter, origin=origin)
        letter = _send_once(state_dir, letter, tgt, attempt, deadline, transport=transport, clock=clock)
        if wait:
            letter = _await(state_dir, letter, tgt, deadline, transport=transport, clock=clock, sleep=sleep)
        return _handle(state_dir, letter, origin=origin)


def _send_once(state_dir, letter: dict, tgt: Target, attempt: int, deadline: float, *, transport, clock) -> dict:
    wire = {"messageId": letter["message_id"], "role": "user", "parts": [{"kind": "text", "text": letter["text"]}]}
    if letter.get("task_id"):
        wire["taskId"] = letter["task_id"]
    if letter.get("context_id"):
        wire["contextId"] = letter["context_id"]
    reply = _rpc(tgt, "message/send", {"message": wire, "configuration": {"blocking": False}},
                 timeout=max(0.001, deadline - clock()), transport=transport)
    if reply.kind != "result":
        return _commit(state_dir, letter, {}, error=reply.error, failed=_FAILED_STATE[reply.kind], attempt=attempt)
    seen = _read_reply(state_dir, letter, reply.result, sent=True)
    if not seen:
        return _commit(state_dir, letter, {}, error="the peer gave no usable letter receipt",
                       failed="sent_no_handle", attempt=attempt)
    if not seen.get("state"):
        seen["state"] = "submitted"  # accepted task; no answer tied to this letter yet
    return _commit(state_dir, letter, seen, attempt=attempt)


def status(state_dir, url: str = "", task_id: str = "", message_id: str = "", *, wait: bool = False,
           asked_by=None, budget: float | None = None, transport=None, clock=time.monotonic,
           sleep=time.sleep) -> str:
    """Read a letter, or list the caller's letters, or (with url and task_id) a remote task's current snapshot.

    With ``message_id`` alone the local letter is returned as recorded; passing its url as well asks the
    peer for news (a read: nothing is sent again). A task reused by several letters is read per letter only
    by message id; the task-only form shows what the peer says now.
    """
    deadline = clock() + (TOOL_TIMEOUT_SEC - START_MARGIN_SEC if budget is None else budget)
    if not wait:
        deadline = min(deadline, clock() + REQUEST_TIMEOUT_SEC)
    if not message_id and not str(url or "").strip() and not task_id:
        own = letters.listing(state_dir, _origin(asked_by))
        rows = [{"message_id": item["message_id"], "peer": item.get("peer"), "url": item.get("url"),
                 "state": item.get("state"), "delivery": (item.get("delivery") or {}).get("class", ""),
                 "answered": bool((item.get("answer") or {}).get("text")), "created_at": item.get("created_at"),
                 "task_id": (item.get("remote") or {}).get("task_id", "")} for item in own]
        return json.dumps({"letters": rows, "note": "Letters asked from this task or chat, newest first. Read one "
                                                    "with status(message_id=...)."}, ensure_ascii=False, indent=2)
    if not message_id and str(url or "").strip() and task_id:
        try:
            tgt = target(state_dir, url)
        except links.LinkError as exc:
            return _error(str(exc))
        reply = _rpc(tgt, "tasks/get", {"id": str(task_id)},
                     timeout=max(0.001, min(REQUEST_TIMEOUT_SEC, deadline - clock())),
                     transport=transport)
        if reply.rpc_error is not None:
            return json.dumps({"jsonrpc": "2.0", "id": reply.request_id, "error": reply.rpc_error},
                              ensure_ascii=False, indent=2)
        if reply.kind != "result":
            return _error(f"Request failed: {reply.error}", peer=tgt.peer, link=tgt.note)
        return json.dumps({"jsonrpc": "2.0", "id": reply.request_id, "result": reply.result},
                          ensure_ascii=False, indent=2)
    if not letters.valid_id(message_id):
        return _error("message_id is 1-128 letters, digits, '.', '_', ':' or '-'")
    letter = letters.load(state_dir, message_id)
    if letter is None:
        return _error(f"no letter {message_id!r} was sent from this install")
    if task_id and task_id not in (letter.get("task_id"), (letter.get("remote") or {}).get("task_id")):
        return _error("task_id differs from this letter's task", message_id=message_id)
    if not str(url or "").strip():
        return _handle(state_dir, letter)
    bound = _bound_target(state_dir, letter)
    tgt = bound if isinstance(bound, Target) else None
    if problem := _same_destination(letter, url, tgt):
        if tgt is None and str(url).strip() == letter["url"]:
            return _handle(state_dir, letter, note=bound or "The original link does not name this address now.")
        return _error(problem, message_id=message_id)
    if tgt is None:
        return _handle(state_dir, letter, note=bound or "The original link does not name this address now.")
    if letters.has_outcome(letter) or not (letter.get("remote") or {}).get("task_id") or deadline <= clock():
        return _handle(state_dir, letter)
    with _reading(state_dir, letter, deadline, clock) as letter:
        letter = _read_task(state_dir, letter, tgt, deadline, transport=transport, clock=clock)
        if wait:
            letter = _await(state_dir, letter, tgt, deadline, transport=transport, clock=clock, sleep=sleep)
        return _handle(state_dir, letter)
