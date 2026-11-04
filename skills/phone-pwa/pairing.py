"""One authority for short-lived admission and persistent revocable bearer sessions."""
import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

PAIR_SECONDS = 600
DEVICE_SECONDS = 30 * 86400


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class Pairing:
    def __init__(self, state_dir, clock=time.time):
        self.clock = clock
        self.path = Path(state_dir) / 'devices.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS devices (
                  id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL, label TEXT NOT NULL,
                  created REAL NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
            ''')
        # Pending admission is deliberately per generation: restart invalidates QR codes.
        self.pending = {}

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def issue(self):
        now = self.clock()
        self.pending = {k: v for k, v in self.pending.items() if v[1] > now}
        token = secrets.token_urlsafe(32)
        key = secrets.token_hex(12)
        self.pending[digest(token)] = (key, now + PAIR_SECONDS, now)
        return {'id': key, 'token': token, 'expires': now + PAIR_SECONDS}

    def revoke_pair(self, key):
        self.pending = {k: v for k, v in self.pending.items() if v[0] != key}

    def active_pair(self, token):
        row = self.pending.get(digest(token))
        now = self.clock()
        return bool(row and row[2] <= now < row[1])

    def redeem(self, token, label):
        # One admission window intentionally serves independent browser/PWA stores.
        now = self.clock()
        if not self.active_pair(token):
            raise ValueError('Connection link expired or revoked.')
        key, secret = secrets.token_hex(12), secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('INSERT INTO devices VALUES (?,?,?,?,?,0)',
                       (key, digest(secret), label.strip()[:80] or 'Phone', now, now + DEVICE_SECONDS))
        return key, secret

    def device(self, secret):
        if not secret or len(secret) > 128:
            return None
        with self.connect() as db:
            row = db.execute('SELECT id,label,created,expires FROM devices WHERE hash=? AND revoked=0',
                             (digest(secret),)).fetchone()
        now = self.clock()
        if not row or not row[2] <= now < row[3]:
            return None
        return row[0]

    def revoke(self, key):
        with self.connect() as db:
            db.execute('UPDATE devices SET revoked=1 WHERE id=?', (key,))

    def list_devices(self):
        with self.connect() as db:
            return [dict(zip(('id','label','created','expires','revoked'), row)) for row in
                    db.execute('SELECT id,label,created,expires,revoked FROM devices ORDER BY created DESC')]
