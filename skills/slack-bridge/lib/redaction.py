"""Credentials never reach stored error text, status, logs or delivery-report errors.

An exception can carry a request header: an HTTP library that refuses a token with an illegal character prints
the whole ``Authorization`` value. ``redact`` removes Slack token shapes, any value after ``Bearer`` or
``Authorization``, and the configured ``SLACK_BOT_TOKEN``/``SLACK_APP_TOKEN`` in every printed form (raw, escaped,
and each printable run of them, so a token printed with ``\\n`` in place of a newline leaves no fragment). Ordinary
text (channel names, Slack error codes, words) is unchanged. The store applies it when it writes error text; the
status route applies it again to every free-text error field, so records written by an older version are clean too.
The daemon's log handlers clean messages and tracebacks; reports clean errors when their payload is built.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable, MutableMapping

MARK = "[redacted]"
# The shortest configured-token run treated as a credential fragment.
_MIN_RUN = 6
_TOKEN_KEYS = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
# A Slack token and whatever escaped characters follow it inside one printed value.
_TOKEN_SHAPE = re.compile(
    r"\b(?:xox[a-z]|xapp)-(?:\\x[0-9A-Fa-f]{2}|\\u[0-9A-Fa-f]{4}|\\[nrt0]|[A-Za-z0-9._~+/=-])+"
)
# A credential-like value (6 or more characters, the fragment size that matters); "Bearer token" stays a phrase.
_VALUE = r"(?!\[redacted\])[^\s'\",;)\]}]{6,}"
_BEARER = re.compile(r"(?i)\b(bearer)(\s+)" + _VALUE)
_AUTHORIZATION = re.compile(r"(?i)\b(authorization)(['\"]?\s*[:=,]\s*['\"]?)(?!bearer\b)" + _VALUE)
_PRINTABLE_RUNS = re.compile(r"[\x21-\x7e]+")


def _forms(secret: str) -> list[str]:
    """Every way the secret may be printed, longest first, then its printable runs."""
    forms = {
        secret,
        repr(secret)[1:-1],
        secret.encode("unicode_escape").decode("ascii"),
        repr(secret.encode("utf-8", "backslashreplace"))[2:-1],
    }
    runs = {run for run in _PRINTABLE_RUNS.findall(secret) if len(run) >= _MIN_RUN}
    return sorted((form for form in forms | runs if len(form) >= _MIN_RUN), key=len, reverse=True)


def configured_secrets(extra: Iterable[str] = ()) -> tuple[str, ...]:
    """The configured Slack tokens of this process's environment, plus ``extra`` values (the route's settings)."""
    values = [os.environ.get(key, "") for key in _TOKEN_KEYS] + [str(value or "") for value in extra]
    return tuple(dict.fromkeys(value for value in values if len(value) >= _MIN_RUN))


def redact(text: Any, secrets: Iterable[str] = (), *, limit: int | None = None) -> str:
    """``text`` without credentials; ``limit`` truncates after redacting, so no cut token survives the cut."""
    clean = str(text or "")
    if clean:
        for secret in configured_secrets(secrets):
            for form in _forms(secret):
                clean = clean.replace(form, MARK)
            # A text already cut inside a token ends with the token's beginning.
            for run in _PRINTABLE_RUNS.findall(secret):
                for size in range(len(run) - 1, _MIN_RUN - 1, -1):
                    if clean.endswith(run[:size]):
                        clean = clean[:-size] + MARK
                        break
        clean = _TOKEN_SHAPE.sub(MARK, clean)
        clean = _BEARER.sub(lambda match: match.group(1) + match.group(2) + MARK, clean)
        clean = _AUTHORIZATION.sub(lambda match: match.group(1) + match.group(2) + MARK, clean)
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

    Handler filters cover every bridge logger and HTTP-library child logger (logger filters do not propagate
    to children). This is scoped to the companion process; importing the module changes no logging settings.
    """
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, _RedactLogErrors) for item in handler.filters):
            handler.addFilter(_RedactLogErrors())


# Free-text error fields of ``GET status`` (top level, and the ``text`` of structured errors).
_TEXT_FIELDS = ("last_socket_error", "last_delivery_error", "last_report_error", "local_settings_error",
                "new_root_channels_error", "store_error")
_ERROR_KEYS = ("last_error", "last_failed_error", "last_uncertain_error")


def _clean_error(value: Any, secrets: tuple[str, ...]) -> None:
    if isinstance(value, MutableMapping) and isinstance(value.get("text"), str):
        value["text"] = redact(value["text"], secrets)


def redact_status(payload: MutableMapping[str, Any], secrets: Iterable[str] = ()) -> None:
    """Clean every free-text error field of a status payload in place."""
    known = configured_secrets(secrets)
    for key in _TEXT_FIELDS:
        if isinstance(payload.get(key), str):
            payload[key] = redact(payload[key], known)
    _clean_error(payload.get("last_error"), known)
    link = payload.get("link")
    if isinstance(link, MutableMapping) and isinstance(link.get("error"), str):
        link["error"] = redact(link["error"], known)
    retrying = payload.get("inbox_retrying")
    if isinstance(retrying, MutableMapping):
        _clean_error(retrying.get("last_error"), known)
    recent = payload.get("recent_problems")
    if isinstance(recent, MutableMapping):
        for part in recent.values():
            if isinstance(part, MutableMapping):
                for key in _ERROR_KEYS:
                    _clean_error(part.get(key), known)
    for reason in payload.get("held_reasons") or ():
        if isinstance(reason, MutableMapping):
            for key in ("reason", "note"):
                if isinstance(reason.get(key), str):
                    reason[key] = redact(reason[key], known)
