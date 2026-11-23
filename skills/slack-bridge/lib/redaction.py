"""Credentials never reach an outside sink: the card, a log line or a report to the Host.

An exception can carry a request header: an HTTP library that refuses a token with an illegal character prints
the whole ``Authorization`` value. ``redact`` removes the configured ``SLACK_BOT_TOKEN``/``SLACK_APP_TOKEN`` in every
printed form (raw, stripped, escaped, and each printable run, longest first), Slack token shapes and any value after
``Bearer`` or ``Authorization`` up to its closing quote (escaped and control characters included, so no tail of a
split token stays), and credential query parameters such as Socket Mode's ``ticket``. Ordinary text (channel names,
Slack error codes, words, URLs) is unchanged, and cleaning twice changes nothing.

Where it runs: the store when it writes error text; the status route over every field the card shows, records of an
older version included; the daemon's log handlers, which also receive Python's own printers of uncaught exceptions;
and every Host report, when it is built and again when a stored one is dispatched. The message posted to Slack and
the report's copy of it (``text``) are never changed.
"""
from __future__ import annotations

import copy
import functools
import logging
import os
import re
import sys
import threading
from typing import Any, Iterable, Mapping, MutableMapping

MARK = "[redacted]"
# The shortest configured-token run treated as a credential fragment.
_MIN_RUN = 6
_TOKEN_KEYS = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
# One character of a printed credential: an escape sequence, a real control character (a token with an illegal
# newline printed raw), or a printable non-separator. A credential continues through all of them.
_CHAR = r"\\x[0-9A-Fa-f]{2}|\\u[0-9A-Fa-f]{4}|\\[^\s'\"]|[\x00-\x1f\x7f]"
# A Slack token and whatever escaped or control characters follow it inside one printed value. It is found anywhere,
# also right after an underscore or a channel ID ("C09A_xoxb-…", "C09Axoxb-…"); only the real prefixes count
# (bot, user, app, refresh, config, client and cookie tokens), and an app token needs its digit, so words such as
# "flexapp-team" stay.
_TOKEN_SHAPE = re.compile(r"(?:xox[a-eprs]-|xapp-\d)(?:" + _CHAR + r"|[A-Za-z0-9._~+/=-])*")
# A credential-like value after a prefix: everything up to a quote, separator or space, including escaped and
# control characters and an earlier ``[redacted]``, so no tail of a cut credential stays. Six or more characters
# (the fragment size that matters); "Bearer token" stays a phrase.
_VALUE_CHAR = r"(?:\[redacted\]|" + _CHAR + r"|[^\s'\",;)\]}\\\[])"
_VALUE = r"(?:\[redacted\]" + _VALUE_CHAR + "*|" + _VALUE_CHAR + "{6,})"
_BEARER = re.compile(r"(?i)\b(bearer)(\s+)" + _VALUE)
_AUTHORIZATION = re.compile(r"(?i)\b(authorization)(['\"]?\s*[:=,]\s*['\"]?)(?!bearer\b)" + _VALUE)
# A credential in a URL query (the Socket Mode ``ticket``, OAuth-style tokens); the rest of the URL stays.
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:ticket|token|access_token|refresh_token|client_secret|signature|sig)=)[^&#\s'\"<>]+"
)
_PRINTABLE_RUNS = re.compile(r"[\x21-\x7e]+")


@functools.lru_cache(maxsize=64)
def _forms(secret: str) -> tuple[str, ...]:
    """Every way the secret may be printed, longest first, then its printable runs."""
    forms = {
        secret,
        repr(secret)[1:-1],
        secret.encode("unicode_escape").decode("ascii"),
        repr(secret.encode("utf-8", "backslashreplace"))[2:-1],
    }
    runs = {run for run in _PRINTABLE_RUNS.findall(secret) if len(run) >= _MIN_RUN}
    return tuple(sorted((form for form in forms | runs if len(form) >= _MIN_RUN), key=len, reverse=True))


@functools.lru_cache(maxsize=64)
def _all_forms(secrets: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({form for secret in secrets for form in _forms(secret)}, key=len, reverse=True))


def configured_secrets(extra: Iterable[str] = ()) -> tuple[str, ...]:
    """The configured Slack tokens of this process's environment, plus ``extra`` values (the route's settings).

    Each value counts as given and without outer whitespace: the client strips a token before using it, so an
    error prints the stripped form.
    """
    values = [os.environ.get(key, "") for key in _TOKEN_KEYS] + [str(value or "") for value in extra]
    forms = (form for value in values for form in (value, value.strip()))
    return tuple(dict.fromkeys(form for form in forms if len(form) >= _MIN_RUN))


def redact(text: Any, secrets: Iterable[str] = (), *, limit: int | None = None) -> str:
    """``text`` without credentials; ``limit`` truncates after redacting, so no cut token survives the cut."""
    clean = str(text or "")
    if clean:
        known = configured_secrets(secrets)
        # Every form of every secret, longest first: a whole token (its stripped form included) goes before any
        # printable run of it, so no run of one form leaves another form's tail behind.
        for form in _all_forms(known):
            clean = clean.replace(form, MARK)
        for secret in known:
            # A text already cut inside a token ends with the token's beginning.
            for run in _PRINTABLE_RUNS.findall(secret):
                for size in range(len(run) - 1, _MIN_RUN - 1, -1):
                    if clean.endswith(run[:size]):
                        clean = clean[:-size] + MARK
                        break
        # After the configured forms: a prefixed value takes the rest of a credential the forms left behind.
        clean = _TOKEN_SHAPE.sub(MARK, clean)
        clean = _BEARER.sub(lambda match: match.group(1) + match.group(2) + MARK, clean)
        clean = _AUTHORIZATION.sub(lambda match: match.group(1) + match.group(2) + MARK, clean)
        clean = _QUERY_SECRET.sub(lambda match: match.group(1) + MARK, clean)
    return clean[:limit] if limit is not None else clean


def error_text(exc: BaseException, secrets: Iterable[str] = (), *, limit: int = 500) -> str:
    """What an exception may say in stored state: its message without credentials, or its class name."""
    return redact(str(exc), secrets, limit=limit) or type(exc).__name__


class _RedactLogErrors(logging.Filter):
    """Clean the formatted record before any handler can write it, including cached traceback text."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None  # A later formatter must not rebuild the raw traceback.
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install_log_redaction() -> None:
    """Install once after the daemon configures logging, using the same live environment as ``redact``.

    Handler filters cover every bridge logger and third-party logger (websockets, httpx, asyncio and others; a
    logger filter would not reach its children's records). Python's own printers of an uncaught exception
    (``sys.excepthook``, ``threading.excepthook``, ``sys.unraisablehook``) and warnings go through the same
    handlers, so nothing a library raises reaches stderr raw. This is scoped to the companion process; importing
    the module changes no logging or hook settings.
    """
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, _RedactLogErrors) for item in handler.filters):
            handler.addFilter(_RedactLogErrors())
    logging.captureWarnings(True)
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_exception
    sys.unraisablehook = _log_unraisable


_UNCAUGHT = logging.getLogger("slack_bridge.uncaught")


def _log_uncaught(kind: type[BaseException], exc: BaseException, tb: Any) -> None:
    _UNCAUGHT.error("Uncaught exception", exc_info=(kind, exc, tb))


def _log_thread_exception(args: Any) -> None:
    if args.exc_type is SystemExit:
        return
    name = getattr(args.thread, "name", "") or "unknown"
    _UNCAUGHT.error("Uncaught exception in thread %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def _log_unraisable(args: Any) -> None:
    message = args.err_msg or "Exception ignored in"
    _UNCAUGHT.error("%s: %r", message, args.object,
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback) if args.exc_type else None)


def log_loop_exception(loop: Any, context: Mapping[str, Any]) -> None:
    """The asyncio loop's exception handler: the same record the default handler writes, through the filters."""
    exc = context.get("exception")
    details = "; ".join(f"{key}={value!r}" for key, value in context.items() if key not in ("message", "exception"))
    logging.getLogger("asyncio").error("%s%s", context.get("message") or "Unhandled exception in event loop",
                                       f" ({details})" if details else "",
                                       exc_info=(type(exc), exc, exc.__traceback__) if isinstance(exc, BaseException) else None)


def _redact_tree(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, MutableMapping):
        for key in list(value):
            value[key] = _redact_tree(value[key], secrets)
        return value
    if isinstance(value, list):
        value[:] = [_redact_tree(item, secrets) for item in value]
        return value
    return value


def redact_status(payload: MutableMapping[str, Any], secrets: Iterable[str] = ()) -> None:
    """Clean every text of a status payload in place: error fields, and the provider's display labels (workspace,
    sends-as) and IDs. Whatever the card shows passes here; ordinary text and enum values are unchanged."""
    _redact_tree(payload, configured_secrets(secrets))


# Identity and routing fields of a Host report pass byte-identical. The Host keys a receipt by (skill, account_id,
# delivery_id, part_id, state) (core presence_delivery._key); changing one would merge two receipts or split one.
# These values came from the Host or from Slack's own answers, so returning them exposes nothing new.
_REPORT_IDENTITY = frozenset({
    "schema_version", "delivery_id", "part_id", "request_id", "state", "provider", "account_id", "conversation_id",
    "channel", "thread_id", "thread_ts", "ts", "format", "origin", "requested_target", "target_resolved", "chunk_count",
    "output_ref", "kind", "method", "operation", "resolution", "effect", "type",
})
# The Host compares a report's text with the turn's reply only for parts that arrived (core presence_runner
# ``_turn_sends``: states "delivered" and "accepted"); there it must stay exact.
_TEXT_MATCHED = frozenset({"delivered", "accepted"})


def _identity(key: Any) -> bool:
    key = str(key)
    return key in _REPORT_IDENTITY or key.endswith(("_id", "_ts"))


def _clean_report_tree(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, MutableMapping):
        for key in list(value):
            if not _identity(key):
                value[key] = _clean_report_tree(value[key], secrets)
        return value
    if isinstance(value, list):
        value[:] = [_clean_report_tree(item, secrets) for item in value]
    return value


def redact_report(payload: Any) -> Any:
    """A Host report with its free text cleaned (the error, notes, labels); identity and routing unchanged.

    ``text`` stays exact for a delivered or accepted part, where the Host matches it against the turn's reply; for a
    failed or uncertain part (nothing or nothing confirmed reached Slack) it is cleaned like any free text. Applied
    when a report is built and again when a stored one is dispatched, so a report queued by an older version leaves
    clean too. Returns a copy; the stored row is not rewritten. Cleaning twice changes nothing.
    """
    if not isinstance(payload, Mapping):
        return payload
    secrets = configured_secrets()
    clean = copy.deepcopy(dict(payload))
    matched = clean.get("state") in _TEXT_MATCHED
    for key in clean:
        if key == "text":
            if not matched and isinstance(clean[key], str):
                clean[key] = redact(clean[key], secrets)
        elif not _identity(key):
            clean[key] = _clean_report_tree(clean[key], secrets)
    return clean
