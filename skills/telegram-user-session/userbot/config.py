"""Private, offline configuration and one-shot login handoff for the user session.

The caller supplies PluginAPI.get_state_dir() (or OUROBOROS_SKILL_STATE_DIR in
its companion), never a directory inside the installed skill's source tree.
This module does not connect to Telegram and never writes a Telethon session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import errno
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any, Callable, Iterator


CONFIG_NAME = "config.json"
CONFIG_LOCK_NAME = "config.lock"
HANDOFF_NAME = "auth-handoff.json"
CHALLENGE_NAME = "login-challenge.json"
TWO_FA_NAME = "login-2fa-required.json"
CODE_STATUS_NAME = "login-code-status.json"
SESSION_NAMES = ("mtproto.session", "mtproto.session-journal", "mtproto.session-wal", "mtproto.session-shm")
_MAX_JSON_BYTES = 16_384
_ID_MAX = (1 << 63) - 1
_TOPIC_MAX = (1 << 31) - 1
_BINDING = re.compile(r"[0-9a-fA-F]{32}\Z")
_PHONE = re.compile(r"\+[1-9][0-9]{1,14}\Z")
_API_HASH = re.compile(r"[0-9a-fA-F]{32}\Z")
_CODE = re.compile(r"[0-9]{1,32}\Z")


class ConfigError(ValueError):
    """Invalid or unsafe private configuration; messages never contain values."""


def _positive_id(value: Any, name: str) -> int:
    if type(value) is not int or not 0 < value <= _ID_MAX:
        raise ConfigError(f"{name} must be a positive integer ID")
    return value


def _peer_id(value: Any) -> int:
    if type(value) is not int or value == 0 or not -_ID_MAX <= value <= _ID_MAX:
        raise ConfigError("peer ID must be a nonzero integer ID")
    return value


def _topic_id(value: Any) -> int | None:
    if value is not None and (type(value) is not int or not 1 < value <= _TOPIC_MAX):
        raise ConfigError("topic ID must be null or a non-General positive integer ID")
    return value


def _binding_id(value: Any) -> str:
    if not isinstance(value, str) or not _BINDING.fullmatch(value):
        raise ConfigError("binding_id must be exactly 32 hexadecimal characters")
    return value


@dataclass(frozen=True)
class Binding:
    """One exact Host Presence binding; None denotes the General/root topic."""

    peer_id: int
    topic_id: int | None
    binding_id: str

    def __post_init__(self) -> None:
        _peer_id(self.peer_id)
        _topic_id(self.topic_id)
        _binding_id(self.binding_id)


def _binding_key(binding: Binding) -> tuple[int, int]:
    return binding.peer_id, binding.topic_id or 0


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str = field(repr=False)
    phone: str = field(repr=False)
    binding_id: str | None = None
    allowed_peer_ids: tuple[int, ...] = ()
    account_id: int | None = None
    bindings: tuple[Binding, ...] = ()

    def __post_init__(self) -> None:
        _positive_id(self.api_id, "api_id")
        if not isinstance(self.api_hash, str) or not _API_HASH.fullmatch(self.api_hash):
            raise ConfigError("api_hash must be 32 hexadecimal characters")
        if not isinstance(self.phone, str) or not _PHONE.fullmatch(self.phone):
            raise ConfigError("phone must be an E.164 number")
        if self.binding_id is not None:
            _binding_id(self.binding_id)
        if type(self.allowed_peer_ids) is not tuple:
            raise ConfigError("allowed_peer_ids must be a tuple of numeric IDs")
        for peer in self.allowed_peer_ids:
            _peer_id(peer)
        if len(set(self.allowed_peer_ids)) != len(self.allowed_peer_ids):
            raise ConfigError("allowed_peer_ids contains duplicates")
        if len(self.allowed_peer_ids) > 1_000:
            raise ConfigError("allowed_peer_ids exceeds the limit")
        if self.account_id is not None:
            _positive_id(self.account_id, "account_id")
        if type(self.bindings) is not tuple or len(self.bindings) > 1_000:
            raise ConfigError("bindings must be a bounded tuple")
        keys: set[tuple[int, int]] = set()
        for binding in self.bindings:
            if type(binding) is not Binding:
                raise ConfigError("bindings must contain exact Binding entries")
            if binding.peer_id not in self.allowed_peer_ids:
                raise ConfigError("binding peer must be allowlisted")
            key = _binding_key(binding)
            if key in keys:
                raise ConfigError("duplicate peer/topic binding")
            keys.add(key)
        if tuple(sorted(self.bindings, key=_binding_key)) != self.bindings:
            raise ConfigError("bindings must be sorted by peer and topic")

    def binding_for(self, peer_id: int, topic_id: int | None) -> str | None:
        """Return an exact binding, never widening a mapped topic or peer."""
        _peer_id(peer_id)
        _topic_id(topic_id)
        if peer_id not in self.allowed_peer_ids:
            return None
        if self.bindings:
            return next((b.binding_id for b in self.bindings
                         if b.peer_id == peer_id and b.topic_id == topic_id), None)
        # The legacy field can only name one Host conversation unambiguously.
        if len(self.allowed_peer_ids) == 1 and topic_id is None:
            return self.binding_id
        return None


@dataclass(frozen=True)
class LoginSecret:
    code: str | None = field(default=None, repr=False)
    phone_code_hash: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.code is None and self.password is None:
            raise ConfigError("login handoff needs a code or password")
        if self.code is not None and (not isinstance(self.code, str) or not _CODE.fullmatch(self.code)):
            raise ConfigError("code must be numeric")
        if self.phone_code_hash is not None and (
            not isinstance(self.phone_code_hash, str)
            or not 1 <= len(self.phone_code_hash) <= 512
            or any(ord(c) < 32 for c in self.phone_code_hash)
        ):
            raise ConfigError("phone_code_hash has invalid format")
        if self.code is None and self.phone_code_hash is not None:
            raise ConfigError("phone_code_hash requires code")
        if self.password is not None and (
            not isinstance(self.password, str)
            or not 1 <= len(self.password) <= 512
            or any(ord(c) < 32 for c in self.password)
        ):
            raise ConfigError("password has invalid format")


def _private_dir(state_dir: str | os.PathLike[str]) -> Path:
    """Confine all paths to a fixed filename in the caller's private state dir."""
    root = Path(state_dir)
    if not root.is_absolute():
        raise ConfigError("skill state directory must be absolute")
    if root.resolve().is_relative_to(Path(__file__).resolve().parent.parent):
        raise ConfigError("skill state directory cannot be inside skill source")
    if root.is_symlink():
        raise ConfigError("skill state directory may not be a symlink")
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        if not root.is_dir() or root.is_symlink():
            raise ConfigError("skill state directory is not a private directory")
        if os.name == "posix":
            os.chmod(root, 0o700)
    except OSError:
        raise ConfigError("skill state directory unavailable") from None
    return root


def _fixed_file(root: Path, filename: str) -> Path:
    path = root / filename
    if path.is_symlink():
        raise ConfigError("private state file may not be a symlink")
    return path


def _reject_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ConfigError("duplicate JSON key")
        obj[key] = value
    return obj


def _reject_constant(_constant: str) -> None:
    raise ConfigError("non-finite JSON number")


def _read_json(path: Path) -> dict[str, Any] | None:
    _fixed_file(path.parent, path.name)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise ConfigError("private state file unavailable") from None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigError("private state file must be regular")
        if os.name == "posix" and metadata.st_mode & 0o077:
            raise ConfigError("private state file permissions are too broad")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            data = stream.read(_MAX_JSON_BYTES + 1)
    except OSError:
        raise ConfigError("private state file unreadable") from None
    finally:
        if fd != -1:
            os.close(fd)
    if len(data) > _MAX_JSON_BYTES:
        raise ConfigError("private state file exceeds size limit")
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_pairs,
                         parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError):
        raise ConfigError("private state file has invalid JSON") from None
    if type(obj) is not dict:
        raise ConfigError("private state JSON must be an object")
    return obj


def _write_json(root: Path, filename: str, obj: dict[str, Any]) -> None:
    target = _fixed_file(root, filename)
    data = (json.dumps(obj, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > _MAX_JSON_BYTES:
        raise ConfigError("private state file exceeds size limit")
    temporary = root / ("." + filename + "." + secrets.token_hex(16) + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _fixed_file(root, filename)
        os.replace(temporary, target)
        if os.name == "posix":
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError:
        raise ConfigError("private state write failed") from None
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _config_lock(root: Path, timeout_sec: float) -> Iterator[None]:
    """Lock a stable private inode, never the atomically replaced config file.

    Both setup and the companion use this same pathname. An exhausted wait
    fails closed rather than applying a transformation to a stale snapshot.
    """
    if (type(timeout_sec) not in (int, float) or not math.isfinite(timeout_sec)
            or not 0 <= timeout_sec <= 60):
        raise ConfigError("config lock timeout must be between 0 and 60 seconds")
    path = _fixed_file(root, CONFIG_LOCK_NAME)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        raise ConfigError("config lock unavailable") from None
    locked = False
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or (os.name == "posix" and metadata.st_mode & 0o077):
            raise ConfigError("config lock file is unsafe")
        deadline = time.monotonic() + timeout_sec
        if os.name == "nt":
            import msvcrt
            # msvcrt.locking locks from the current offset; reserve byte zero.
            if metadata.st_size == 0:
                os.write(fd, b"\0")
            attempt = lambda: msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            attempt = lambda: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                attempt()
                locked = True
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise ConfigError("config lock unavailable") from None
                if time.monotonic() >= deadline:
                    raise TimeoutError("config lock acquisition timed out") from None
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def load_config(state_dir: str | os.PathLike[str]) -> Config | None:
    """Read strict schema-1 JSON; None means the owner has not configured it."""
    root = _private_dir(state_dir)
    obj = _read_json(_fixed_file(root, CONFIG_NAME))
    if obj is None:
        return None
    required = {"schema", "api_id", "api_hash", "phone", "binding_id", "allowed_peer_ids", "account_id"}
    if set(obj) not in (required, required | {"bindings"}) or type(obj["schema"]) is not int or obj["schema"] != 1:
        raise ConfigError("config schema or keys invalid")
    if type(obj["allowed_peer_ids"]) is not list:
        raise ConfigError("allowed_peer_ids must be a JSON array")
    raw_bindings = obj.get("bindings", [])
    if type(raw_bindings) is not list or len(raw_bindings) > 1_000:
        raise ConfigError("bindings must be a bounded JSON array")
    bindings = []
    for entry in raw_bindings:
        if type(entry) is not dict or set(entry) != {"peer_id", "topic_id", "binding_id"}:
            raise ConfigError("binding entry keys invalid")
        bindings.append(Binding(entry["peer_id"], entry["topic_id"], entry["binding_id"]))
    return Config(
        api_id=obj["api_id"], api_hash=obj["api_hash"], phone=obj["phone"],
        binding_id=obj["binding_id"], allowed_peer_ids=tuple(obj["allowed_peer_ids"]),
        account_id=obj["account_id"], bindings=tuple(bindings),
    )


def save_config(state_dir: str | os.PathLike[str], config: Config) -> None:
    """Atomically replace config, not authenticate changes.

    A Telethon mtproto.session (including SQLite sidecars) freezes the API
    credentials and phone until a separate, controlled offline reset removes
    the session. This function never deletes a session. The parent must stop
    the companion during reset and prevent concurrent login while saving.
    Only the logged-in companion, after get_me(), may set account_id; this
    module cannot establish that fact from a config file alone.
    """
    root = _private_dir(state_dir)
    with _config_lock(root, 5.0):
        _save_config_unlocked(root, config)


def update_config(
    state_dir: str | os.PathLike[str], transform: Callable[[Config | None], Config],
    *, timeout_sec: float = 5.0,
) -> Config:
    """Apply a complete config transformation to the latest disk state atomically.

    The callback must derive its changes from *current*, not a previously
    captured Config; it runs inside the cross-process lock and should be short.
    A timeout or callback error never writes a new configuration.
    """
    root = _private_dir(state_dir)
    with _config_lock(root, timeout_sec):
        new = transform(load_config(root))
        _save_config_unlocked(root, new)
        return new


def _save_config_unlocked(root: Path, config: Config) -> None:
    if type(config) is not Config:
        raise ConfigError("config must be a validated Config")
    try:
        has_session = any(_session_artifact_exists(root / name) for name in SESSION_NAMES)
    except OSError:
        raise ConfigError("session status unavailable") from None
    if has_session:
        existing = load_config(root)
        if existing is None or (
            existing.api_id, existing.api_hash, existing.phone
        ) != (config.api_id, config.api_hash, config.phone):
            raise ConfigError("session exists; credential change requires offline reset")
    _write_json(root, CONFIG_NAME, {
        "schema": 1, "api_id": config.api_id, "api_hash": config.api_hash,
        "phone": config.phone, "binding_id": config.binding_id,
        "allowed_peer_ids": list(config.allowed_peer_ids), "account_id": config.account_id,
        "bindings": [{"peer_id": b.peer_id, "topic_id": b.topic_id, "binding_id": b.binding_id}
                     for b in config.bindings],
    })


def _session_artifact_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def stage_login_secret(
    state_dir: str | os.PathLike[str], *, code: str | None = None,
    phone_code_hash: str | None = None, password: str | None = None,
) -> None:
    """Owner-entered, transient login input; the companion consumes it once.

    Replacing an unconsumed handoff deliberately supersedes it. Never include
    these values in logs, responses, durable jobs or exception messages.
    """
    secret = LoginSecret(code, phone_code_hash, password)
    _write_json(_private_dir(state_dir), HANDOFF_NAME, {
        "schema": 1, "code": secret.code, "phone_code_hash": secret.phone_code_hash,
        "password": secret.password,
    })


def save_login_challenge(state_dir: str | os.PathLike[str], phone_code_hash: str) -> None:
    """Save Telethon's challenge token privately across companion restarts.

    This is not an owner input or an authorization decision. The companion
    writes the value returned by send_code_request; the setup route separately
    stages the human-entered code/password. Clear it after successful login,
    cancellation or offline reset.
    """
    if (not isinstance(phone_code_hash, str) or not 1 <= len(phone_code_hash) <= 512
            or any(ord(c) < 32 for c in phone_code_hash)):
        raise ConfigError("phone_code_hash has invalid format")
    _write_json(_private_dir(state_dir), CHALLENGE_NAME,
                {"schema": 1, "phone_code_hash": phone_code_hash})


def load_login_challenge(state_dir: str | os.PathLike[str]) -> str | None:
    """Return the private challenge token, if a login challenge is active."""
    obj = _read_json(_fixed_file(_private_dir(state_dir), CHALLENGE_NAME))
    if obj is None:
        return None
    if set(obj) != {"schema", "phone_code_hash"} or type(obj["schema"]) is not int or obj["schema"] != 1:
        raise ConfigError("login challenge schema or keys invalid")
    value = obj["phone_code_hash"]
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or any(ord(c) < 32 for c in value):
        raise ConfigError("phone_code_hash has invalid format")
    return value


def clear_login_challenge(state_dir: str | os.PathLike[str]) -> None:
    """Remove the challenge token on success, cancellation or reset."""
    path = _fixed_file(_private_dir(state_dir), CHALLENGE_NAME)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        raise ConfigError("login challenge removal failed") from None


def consume_login_secret(state_dir: str | os.PathLike[str]) -> LoginSecret | None:
    """Atomically claim one handoff and delete its file even on invalid JSON.

    This is logical file deletion, not secure erasure of storage media. A crash
    during a claim may leave a hidden claim file; treat state-dir backups and
    crash recovery as sensitive. Only one supervised companion should consume.
    """
    root = _private_dir(state_dir)
    source = _fixed_file(root, HANDOFF_NAME)
    claimed = root / ("." + HANDOFF_NAME + ".claim-" + secrets.token_hex(16))
    try:
        os.replace(source, claimed)
    except FileNotFoundError:
        return None
    except OSError:
        raise ConfigError("login handoff unavailable") from None
    try:
        obj = _read_json(claimed)
        if obj is None or set(obj) != {"schema", "code", "phone_code_hash", "password"}:
            raise ConfigError("login handoff schema or keys invalid")
        if type(obj["schema"]) is not int or obj["schema"] != 1:
            raise ConfigError("login handoff schema or keys invalid")
        return LoginSecret(obj["code"], obj["phone_code_hash"], obj["password"])
    finally:
        claimed.unlink(missing_ok=True)
