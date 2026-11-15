"""Shared state for tools and the watcher: settings, the client, the agent and field caches, job files."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

from .client import Client, FreshdeskError, canonical_host
from .fields import FieldCatalog
from .store import Store, dump, now_iso

KEY_SETTING = "FRESHDESK_API_KEY"
DEFAULTS = {"domain": "", "poll_seconds": 60, "budget_share": 25}
AGENT_TTL = 600        # seconds a cached /agents/me stays current for tools
FIELDS_TTL = 3600      # seconds a cached field catalog stays current; a choice miss refreshes it at once
CONTACT_TTL = 86400
INLINE_LIMIT = 60_000  # characters of a GET answer returned inline; more goes to a file
SCOPES = {1: "all tickets", 2: "tickets of the agent's groups", 3: "tickets assigned to the agent"}


def read_settings(state_dir) -> dict:
    path = Path(state_dir) / "settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(DEFAULTS)
    if not isinstance(value, dict):
        raise ValueError("settings_unreadable")
    return {**DEFAULTS, **{key: value[key] for key in DEFAULTS if key in value}}


def write_settings(state_dir, values: dict) -> dict:
    current = read_settings(state_dir)
    domain = str(values.get("domain", current["domain"]) or "").strip()
    host = canonical_host(domain) if domain else ""
    with _store(state_dir) as store:
        known = store.meta("host")
        held = store.one("SELECT 1 FROM tickets LIMIT 1") or store.one("SELECT 1 FROM journal LIMIT 1")
        if host and known and known != host and held:
            raise FreshdeskError("host_change_refused", f"This skill already holds state for {known}. Ticket numbers "
                                 "belong to one helpdesk, so a different helpdesk needs its own installation or a "
                                 "reset of this skill's state.")
    try:
        poll = int(values.get("poll_seconds", current["poll_seconds"]))
        share = int(values.get("budget_share", current["budget_share"]))
    except (TypeError, ValueError):
        raise FreshdeskError("settings_invalid", "Poll period and budget share are whole numbers.") from None
    if not 30 <= poll <= 600 or not 5 <= share <= 90:
        raise FreshdeskError("settings_invalid", "Poll period is 30–600 seconds; the background share is 5–90 %.")
    saved = {"domain": host, "poll_seconds": poll, "budget_share": share}
    path = Path(state_dir) / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(json.dumps(saved, indent=2))
        temporary = handle.name
    os.replace(temporary, path)
    return saved


class _store:
    def __init__(self, state_dir):
        self.store = Store(state_dir)

    def __enter__(self):
        return self.store

    def __exit__(self, *_):
        self.store.close()


class Service:
    """One helpdesk. ``key`` is a callable returning the saved API key (the host's grant decides access)."""

    def __init__(self, state_dir, key, *, base_url: str = "", timeout: float = 30.0, host=None):
        """``host``: a ``HostLink`` for the place check before each outward call (``None`` only in unit tests)."""
        self.state_dir = Path(state_dir)
        self._key = key
        self.base_url = base_url
        self.timeout = timeout
        self.host = host

    # -- configuration --------------------------------------------------------------------------------
    def settings(self) -> dict:
        return read_settings(self.state_dir)

    def client(self) -> Client:
        settings = self.settings()
        if not settings["domain"]:
            raise FreshdeskError("not_configured", "No Freshdesk address is saved yet: Settings → Freshdesk.")
        key = self._key() or ""
        if not key.strip():
            raise FreshdeskError("api_key_missing", "No Freshdesk API key is available to this skill: save it in the "
                                 "skill's protected key form and grant the key to the skill.")
        return Client(settings["domain"], key, timeout=self.timeout, base_url=self.base_url)

    def store(self) -> Store:
        return Store(self.state_dir)

    # -- cached facts ----------------------------------------------------------------------------------
    def agent(self, client: Client, store: Store, *, refresh: bool = False) -> dict:
        cached = store.meta("agent")
        if cached and not refresh and cached.get("host") == client.host and time.time() - cached.get("fetched", 0) < AGENT_TTL:
            return cached
        response = client.get("agents/me")
        self.note_limits(store, response.limits)
        data = response.data if isinstance(response.data, dict) else {}
        contact = data.get("contact") or {}
        agent = {"host": client.host, "id": str(data.get("id") or ""), "name": contact.get("name") or "",
                 "email": (contact.get("email") or "").lower(), "occasional": data.get("occasional"),
                 "type": data.get("type"), "ticket_scope": data.get("ticket_scope"),
                 "scope": SCOPES.get(data.get("ticket_scope"), "unknown"), "group_ids": data.get("group_ids") or [],
                 "role_ids": data.get("role_ids") or [], "fetched": time.time()}
        previous = store.meta("agent")
        if previous and previous.get("id") and previous.get("id") != agent["id"] and previous.get("host") == client.host:
            store.set_meta("agent_changed", {"from": previous.get("id"), "to": agent["id"], "at": now_iso()})
        store.set_meta("agent", agent)
        store.set_meta("host", client.host)
        return agent

    def catalog(self, client: Client, store: Store, *, refresh: bool = False) -> FieldCatalog:
        cached = store.meta("ticket_fields")
        if cached and not refresh and cached.get("host") == client.host and time.time() - cached.get("fetched", 0) < FIELDS_TTL:
            return FieldCatalog(cached["fields"])
        response = client.get("ticket_fields")
        self.note_limits(store, response.limits)
        fields = response.data if isinstance(response.data, list) else []
        store.set_meta("ticket_fields", {"host": client.host, "fields": fields, "fetched": time.time()})
        return FieldCatalog(fields)

    def contact(self, client: Client, store: Store, contact_id) -> dict:
        if not contact_id:
            return {}
        row = store.one("SELECT value, fetched FROM contacts WHERE id=?", (str(contact_id),))
        if row and time.time() - row["fetched"] < CONTACT_TTL:
            return json.loads(row["value"])
        try:
            response = client.get(f"contacts/{int(contact_id)}")
            self.note_limits(store, response.limits)
            data = response.data if isinstance(response.data, dict) else {}
            value = {"id": str(contact_id), "name": data.get("name") or "", "email": data.get("email") or "",
                     "company_id": data.get("company_id")}
        except FreshdeskError as exc:
            if exc.code != "not_found":
                raise
            value = {"id": str(contact_id), "name": "", "email": "", "unknown": True}
        store.db.execute("INSERT INTO contacts(id,value,fetched) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET "
                         "value=excluded.value, fetched=excluded.fetched", (str(contact_id), dump(value), time.time()))
        return value

    @staticmethod
    def note_limits(store: Store, limits: dict) -> None:
        if limits:
            store.set_meta("limits", {**limits, "observed_at": now_iso()})

    def agent_names(self, catalog: FieldCatalog) -> dict:
        field = catalog.by_type.get("default_agent") or {}
        choices = field.get("choices") if isinstance(field.get("choices"), dict) else {}
        return {str(value): label for label, value in choices.items()}

    # -- files ------------------------------------------------------------------------------------------
    def job_file(self, job: str, name: str, data: bytes) -> dict:
        safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "-", job)[:80] or "job"
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(name or "file").name)[:120] or "file"
        folder = self.state_dir / "jobs" / f"{safe_job}-{int(time.time() * 1000)}" / "output"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / safe_name
        path.write_bytes(data)
        return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def check_account(self, client: Client, account_id) -> None:
        if account_id not in (None, "") and str(account_id).strip().lower() != client.host:
            raise FreshdeskError("account_mismatch", f"This skill is connected to {client.host}, not {account_id!r}.")
