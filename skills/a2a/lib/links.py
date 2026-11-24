"""Links to other agents, kept in this skill's own state and read wherever they are used.

The tool child, the Settings routes and the companion daemon read ``links.json`` at use time, so a saved,
changed or removed link applies to the next call without a restart and without forwarding a setting into
any process environment (``env_from_settings`` stays empty). The legacy Settings writer rebuilds only its
own keys in ``settings.json``, so it cannot erase links.

A link has an id (the name tools use for it), a label the owner may rename, an optional address with the
password this install presents there, and an optional inbound password the peer presents here. Its identity
is a random uid made with it and kept across renames and password changes: the receiver's principal is
``link:<uid>``, so a link removed and made again under the same id is a new peer that inherits nothing.
Outbound passwords are stored as written (they are needed to log in) and belong to the address they were
saved with. An inbound password is typed by the owner, stored only as an HMAC-SHA256 under one random key of
this install and compared in constant time; it is never shown back.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import urllib.parse
from dataclasses import dataclass, field, fields
from pathlib import Path

from . import storage

LINKS_FILE = "links.json"
KEY_FILE = "link-key"
LOCK_FILE = ".links.lock"
SETTINGS_FILE = "settings.json"
SHARED_PASSWORD = "A2A_SERVER_PASSWORD"
PREVIOUS_SHARED = "A2A_SERVER_PASSWORD_PREVIOUS"  # legacy guard until this version's companion starts
ACTIVE_SHARED = "server-password.active.json"
PRINCIPAL_PREFIX = "link:"
DEFAULT_USERNAME = "ouroboros"
INBOUND_CHOICES = ("keep", "off")
MIN_INBOUND_SECRET = 16  # a security floor for a password typed for a peer, not a judgment of its quality
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
_TEXT_LIMIT = 200


class LinkError(ValueError):
    """A link request that cannot be honoured; the message says why and never contains a secret."""


@dataclass(frozen=True)
class Link:
    id: str
    label: str
    url: str = ""                       # the peer's A2A endpoint ("" = this peer only writes here)
    send_username: str = DEFAULT_USERNAME
    send_secret: str = field(default="", repr=False)    # what this install presents to the peer
    inbound_hash: str = field(default="", repr=False)   # HMAC of what the peer presents here
    ca_file: str = ""                   # a CA bundle that verifies a self-signed HTTPS peer
    uid: str = ""                       # the link's own identity, made with it and never reused

    @property
    def principal(self) -> str:
        """``link:<uid>``: the receiver's identity of this peer (its tasks, arrivals and conversation)."""
        return PRINCIPAL_PREFIX + self.uid if self.uid else ""

    def public(self) -> dict:
        """What may be shown or returned: never a password or its digest."""
        return {"id": self.id, "label": self.label, "url": self.url, "sends_password": bool(self.send_secret),
                "accepts_messages": bool(self.inbound_hash), "ca_file": self.ca_file}


def is_loopback(host: str) -> bool:
    clean = str(host or "").strip().strip("[]").lower()
    if clean == "localhost":
        return True
    try:
        return ipaddress.ip_address(clean).is_loopback
    except ValueError:
        return False


def requires_inbound_auth(host: str, public_url: str = "") -> bool:
    """A public endpoint still requires authentication when a proxy forwards to a loopback bind."""
    return bool(public_url) or not is_loopback(host)


def endpoint(url: str) -> str:
    """``scheme://host:port/path`` with the path exactly as given ("/" when there is none), or "" for anything but
    a plain http(s) address.

    The endpoint is where requests go and what a saved password is bound to: a different scheme, host, port or
    path (a trailing slash included) is a different peer, and an address with user information, a query or a
    fragment is refused.
    """
    raw = str(url or "").strip(" ")
    if any(char.isspace() or not char.isprintable() or char in "\\?#" for char in raw):
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None \
                or parsed.password is not None:
            return ""
        port = parsed.port
        if port == 0 or parsed.netloc.endswith(":"):
            return ""
        host = parsed.hostname.lower()
        if parsed.netloc.startswith("["):
            if not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parsed.netloc):
                return ""
            ipaddress.IPv6Address(host)
            host = f"[{host}]"
        else:
            name = host.encode("idna").decode("ascii").removesuffix(".")
            if len(name) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                         for label in name.split(".")):
                return ""
    except ValueError:
        return ""
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{host}:{port}{parsed.path or '/'}"


def public_url(value) -> str:
    """Keep a configured card address as written; never replace an invalid one with local mode."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not endpoint(value):
        raise LinkError("A2A_PUBLIC_URL must be an absolute HTTP(S) address without user info, query or fragment.")
    return value.strip(" ")


def advertised_url(host: str, port, public: str, tls: bool) -> str:
    if public:
        return public
    host = host.strip("[]")
    host = f"[{host}]" if ":" in host else host
    return f"{'https' if tls else 'http'}://{host}:{port}/"


def _text(value, limit: int = _TEXT_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise LinkError("text fields must not contain control characters")
    return text[:limit]


def load(state_dir) -> list[Link]:
    """Every saved link; a file that cannot be read raises LinkError (readers then accept no link)."""
    try:
        raw = storage.read_json(Path(state_dir) / LINKS_FILE)
    except ValueError as exc:
        raise LinkError(f"saved links are unreadable ({exc})") from None
    if raw is None:
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("links"), list):
        raise LinkError("saved links are not a link list")
    names = {field.name for field in fields(Link)}
    links = []
    for row in raw["links"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not _ID.fullmatch(row["id"]):
            raise LinkError("saved links hold a malformed entry")
        links.append(Link(**{key: str(value) for key, value in row.items() if key in names and value is not None}))
    return links


def get(state_dir, link_id: str) -> Link | None:
    return next((link for link in load(state_dir) if link.id == link_id), None)


def by_principal(state_dir, principal: str) -> Link | None:
    return next((link for link in load(state_dir) if link.uid and link.principal == principal), None)


def _write(state_dir, links: list[Link]) -> None:
    rows = [{field.name: getattr(link, field.name) for field in fields(Link)} for link in links]
    storage.write_json(Path(state_dir) / LINKS_FILE, {"schema": 1, "links": rows}, private=True)


def _key(state_dir, *, create: bool) -> bytes | None:
    """The install's HMAC key; the first creation happens under the links lock, exclusively."""
    path = Path(state_dir) / KEY_FILE
    for _attempt in range(2):
        try:
            return bytes.fromhex(path.read_text(encoding="ascii").strip())
        except FileNotFoundError:
            if not create:
                return None
            try:
                storage.write_bytes(path, secrets.token_bytes(32).hex().encode("ascii"), private=True, exclusive=True)
            except FileExistsError:
                pass  # another writer made it first: read theirs
        except (OSError, ValueError, UnicodeDecodeError):
            raise LinkError("the link key file is unreadable") from None
    raise LinkError("the link key file could not be created")


def secret_digest(key: bytes, secret: str) -> str:
    return hmac.new(key, str(secret).encode("utf-8"), hashlib.sha256).hexdigest()


def load_server_settings(state_dir) -> dict:
    """The skill's saved server settings; a failed read is not an empty form."""
    try:
        values = storage.read_json(Path(state_dir) / SETTINGS_FILE)
    except ValueError as exc:
        raise LinkError(str(exc)) from None
    if values is not None and not isinstance(values, dict):
        raise LinkError("saved A2A settings are not an object")
    return values or {}


def shared_password(value) -> str:
    return str(value or "").strip()  # same normalization as the companion's other server text settings


def active_shared_digest(state_dir) -> str | None:
    """None means an older companion may still be using the previous saved value."""
    try:
        value = storage.read_json(Path(state_dir) / ACTIVE_SHARED)
    except ValueError as exc:
        raise LinkError(str(exc)) from None
    if value is None:
        return None
    if isinstance(value, dict) and isinstance(value.get("digest"), str):
        return value["digest"]
    raise LinkError("the companion's shared password record is unreadable")


def record_active_shared(state_dir, password: str) -> None:
    """Record the companion's startup snapshot under the common writer lock, never the clear secret."""
    if password and uses_secret(state_dir, password):
        raise LinkError("the server's shared password is already assigned to a link")
    digest = secret_digest(_key(state_dir, create=True), password) if password else ""
    storage.write_json(Path(state_dir) / ACTIVE_SHARED, {"digest": digest}, private=True)


def match_secret(state_dir, secret: str) -> Link | None:
    """The link whose inbound password this is. Every stored digest is compared in constant time."""
    if not secret:
        return None
    links = load(state_dir)
    key = _key(state_dir, create=False)
    if key is None:
        return None
    presented, found = secret_digest(key, secret), None
    for link in links:  # a link without its own identity (written by hand) never authenticates anyone
        if link.uid and link.inbound_hash and hmac.compare_digest(presented, link.inbound_hash) and found is None:
            found = link
    return found


def save(state_dir, form: dict, *, shared_passwords=()) -> Link:
    """Add or update one link from the Settings form and return it.

    The form hydrates empty (it edits one link of many), so a blank field keeps what is saved, except that a
    password sent out and a CA file belong to the address they were saved with: a changed address drops them
    unless new ones are given. A new link without an address only receives. ``accept_secret`` sets or replaces
    the password the peer presents here (typed by the owner, at least ``MIN_INBOUND_SECRET`` characters);
    ``inbound``: ``keep`` or ``off`` (this peer cannot write here). Every inbound password must differ from the
    other links', the saved shared password, the companion's last startup value and any explicitly supplied
    shared passwords.
    """
    if not isinstance(form, dict):
        raise LinkError("invalid link form")
    link_id = str(form.get("id") or "").strip()
    if not _ID.fullmatch(link_id):
        raise LinkError("a link id is 1-48 lowercase letters, digits, '-' or '_', starting with a letter or digit")
    inbound = str(form.get("inbound") or "keep").strip() or "keep"
    if inbound not in INBOUND_CHOICES:
        raise LinkError("inbound must be keep or off")
    typed = str(form.get("accept_secret") or "")
    if typed and inbound == "off":
        raise LinkError("a password for writing here and 'it does not write here' exclude each other")
    if typed and len(typed) < MIN_INBOUND_SECRET:
        raise LinkError(f"a password for writing here has at least {MIN_INBOUND_SECRET} characters")
    root = Path(state_dir)
    with storage.locked(root / LOCK_FILE):
        links = load(root)
        existing = next((link for link in links if link.id == link_id), None)
        url = str(form.get("url") or "").strip()
        if url:
            url = endpoint(url)
            if not url:
                raise LinkError("the address must be a plain http(s) URL without user name, query or fragment")
            if any(other.url == url for other in links if other.id != link_id):
                raise LinkError("another link already uses this address")
        ca_file = str(form.get("ca_file") or "").strip()
        if ca_file and not Path(ca_file).is_file():
            raise LinkError("the CA file does not exist")
        # What was saved for the old address never follows the link to a new one.
        kept = existing if existing is not None and (not url or url == existing.url) else None
        if inbound == "off":
            inbound_hash = ""
        elif typed:
            stored = load_server_settings(root)
            active = active_shared_digest(root)
            shared_values = (*shared_passwords, shared_password(stored.get(SHARED_PASSWORD)),
                             shared_password(stored.get(PREVIOUS_SHARED)) if active is None else "")
            inbound_hash = secret_digest(_key(root, create=True), typed)
            if active and hmac.compare_digest(active, inbound_hash) or any(
                    other.inbound_hash == inbound_hash for other in links if other.id != link_id) or any(
                    shared and hmac.compare_digest(typed.encode("utf-8"), str(shared).encode("utf-8"))
                    for shared in shared_values):
                raise LinkError("this inbound password is already used by another link or by the shared server "
                                "password")
        else:
            inbound_hash = existing.inbound_hash if existing else ""
        link = Link(
            id=link_id,
            label=_text(form.get("label")) or (existing.label if existing else link_id),
            url=url or (existing.url if existing else ""),
            send_username=_text(form.get("send_username"), 100) or (existing.send_username if existing else DEFAULT_USERNAME),
            send_secret=str(form.get("send_secret") or "") or (kept.send_secret if kept else ""),
            inbound_hash=inbound_hash,
            ca_file=ca_file or (kept.ca_file if kept else ""),
            uid=(existing.uid if existing and existing.uid else secrets.token_hex(16)),
        )
        _write(root, [link if other.id == link_id else other for other in links] + ([] if existing else [link]))
    return link


def remove(state_dir, link_id: str) -> bool:
    root = Path(state_dir)
    with storage.locked(root / LOCK_FILE):
        links = load(root)
        kept = [link for link in links if link.id != str(link_id or "").strip()]
        if len(kept) == len(links):
            return False
        _write(root, kept)
    return True


def uses_secret(state_dir, secret: str) -> bool:
    """Whether a link already accepts this password (the shared server password must differ from all of them)."""
    return match_secret(state_dir, secret) is not None


def resolve(state_dir, target: str) -> tuple[str, Link | None]:
    """The endpoint a call goes to and the link whose credential it may carry.

    A link id names that link's saved address. An address carries a link's credential only when it is
    exactly that link's saved endpoint; any other address, including one an Agent Card advertises, is
    contacted without credentials.
    """
    target = str(target or "").strip()
    if _ID.fullmatch(target):
        link = get(state_dir, target)
        if link is None:
            raise LinkError(f"no saved link is named {target!r}")
        if not link.url:
            raise LinkError(f"link {target!r} has no address: that peer only writes here")
        return link.url, link
    found = endpoint(target)
    if not found:
        raise LinkError("url must be an http(s) address or the id of a saved link")
    return found, next((link for link in load(state_dir) if link.url == found), None)
