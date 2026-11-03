"""Encrypted credential custody shared by calendar providers.

The filenames retain the Google-only vault's names so existing installations and
manual-key backups continue to work. New installs generate a private local key;
legacy passphrases remain supported. Entries identify their provider, while old
unmarked entries remain Google grants. Disconnect removes only the selected entry.
"""
from __future__ import annotations
import base64
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from typing import Any, Dict
from ops import ProviderError

TOKENS_FILE = "google_tokens.enc"
SALT_FILE = "google_tokens.salt"
KEY_FILE = "google_tokens.key"

# ── token vault ─────────────────────────────────────────────────────

def _fernet(state_dir: str, passphrase: str):
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except Exception as exc:
        raise ProviderError("unsupported", f"библиотека cryptography недоступна: {exc}")
    if not passphrase:
        path = os.path.join(state_dir, KEY_FILE)
        try:
            with open(path, 'rb') as fh:
                return Fernet(fh.read())
        except FileNotFoundError:
            if os.path.exists(os.path.join(state_dir, TOKENS_FILE)):
                raise ProviderError('auth', 'Для старого хранилища нужен прежний CALENDAR_TOKEN_KEY; токены не изменены')
            key = Fernet.generate_key()
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                with open(path, 'rb') as fh:
                    return Fernet(fh.read())
            with os.fdopen(fd, 'wb') as fh:
                fh.write(key)
            return Fernet(key)
    salt_path = os.path.join(state_dir, SALT_FILE)
    try:
        with open(salt_path, "rb") as fh:
            salt = fh.read()
    except OSError:
        salt = os.urandom(16)
        fd = os.open(salt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(salt)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
    return Fernet(key)


def load_tokens(state_dir: str, passphrase: str) -> Dict[str, Dict[str, Any]]:
    path = os.path.join(state_dir, TOKENS_FILE)
    if not os.path.exists(path):
        return {}
    f = _fernet(state_dir, passphrase)
    with open(path, "rb") as fh:
        blob = fh.read()
    try:
        return json.loads(f.decrypt(blob).decode("utf-8"))
    except Exception:
        raise ProviderError("auth", "не удалось расшифровать токены Google: восстанови прежний CALENDAR_TOKEN_KEY. Если ключ утрачен, сначала сохрани google_tokens.enc и google_tokens.salt из каталога состояния навыка отдельно, затем убери только эти файлы и подключи все Google-аккаунты заново; обычное переподключение не прочитает старое хранилище")


def save_tokens(state_dir: str, passphrase: str, tokens: Dict[str, Dict[str, Any]]) -> None:
    """Explicit whole-vault replacement; ordinary writers merge under the same lock."""
    def replace(fresh):
        fresh.clear()
        fresh.update(tokens)
        return True
    _mutate_tokens(state_dir, passphrase, replace)


def _mutate_tokens(state_dir, passphrase, mutate):
    """Serialize vault read/merge/replace with the existing calendar DB writer.

    No HTTP or schema initialization belongs under this lock. Atomic file replace
    is the vault commit; this empty DB transaction only serializes its writers.
    Readers use the complete old or new ciphertext, never an intermediate file.
    """
    from store import DB_FILENAME
    with closing(sqlite3.connect(os.path.join(state_dir, DB_FILENAME), timeout=5, isolation_level=None)) as conn:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            fresh = load_tokens(state_dir, passphrase)
            if not mutate(fresh):
                return False
            _write_tokens(state_dir, passphrase, fresh)
            return True


def _write_tokens(state_dir, passphrase, tokens):
    f = _fernet(state_dir, passphrase)
    path = os.path.join(state_dir, TOKENS_FILE)
    fd, tmp = tempfile.mkstemp(prefix=TOKENS_FILE + '.', suffix='.tmp', dir=state_dir)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(f.encrypt(json.dumps(tokens).encode("utf-8")))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
