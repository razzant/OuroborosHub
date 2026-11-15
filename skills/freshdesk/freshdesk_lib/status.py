"""The place's status route: one line for the owner, the Connect choice and fact labels in the connector's words."""
from __future__ import annotations

from .service import Service

# Core fact kinds in this connector's words; sent on the status payload, never in the manifest (older hosts drop
# a place entry whose manifest labels them).
LABELS = {
    "new_issue": "New ticket",
    "assigned": "Assigned to this agent",
    "my_thread": "Reply on a ticket this agent wrote in",
    "mention": "Notified on a note",
    "any_message": "Any ticket activity, field changes included",
}


def identity(agent: dict | None) -> dict | None:
    if not agent or not agent.get("id"):
        return None
    label = agent.get("name") or agent.get("email") or f"Agent {agent['id']}"
    if agent.get("email") and agent.get("name"):
        label = f"{agent['name']} <{agent['email']}>"
    return {"sends_as": {"id": str(agent["id"]), "label": label[:200], "kind": "user"}}


def place_status(service: Service, account_id: str = "") -> dict:
    settings = service.settings()
    host = settings["domain"]
    if not host:
        return {"text": "Not set up", "tone": "warn", "title": "Save the helpdesk address in Settings → Freshdesk, "
                "then the agent's API key.", "connect": [], "labels": LABELS}
    if account_id and account_id.strip().lower() != host:
        return {"text": "Another helpdesk", "tone": "bad", "title": f"This skill is connected to {host}.",
                "connect": [], "labels": LABELS}
    store = service.store()
    try:
        agent = store.meta("agent")
        watcher = store.meta("watcher") or {}
        capabilities = store.meta("host_capabilities") or {}
        gaps = store.gaps()
    finally:
        store.close()
    who = (agent or {}).get("name") or "this key's agent"
    payload = {"connect": [{"account_id": host, "conversation_id": "", "label": f"All tickets visible to {who}"}],
               "labels": LABELS}
    if account_id:
        payload["account_id"] = host
    if capabilities.get("presence_identity") == 1 and identity(agent):
        payload["identity"] = identity(agent)
    state = watcher.get("state")
    if gaps:
        first = gaps[0]
        payload.update(text="Needs attention", tone="warn", title=f"{first['code']} ({first['scope']})")
    elif state == "listening":
        payload.update(text="Listening", tone="ok",
                       title=f"Checks {host} every {settings['poll_seconds']} s; last check {watcher.get('last_walk') or 'pending'}.")
    elif state:
        payload.update(text=str(watcher.get("text") or state)[:24], tone="muted", title=str(watcher.get("title") or ""))
    else:
        payload.update(text="Not listening", tone="muted",
                       title="Tools work without a place; connect the place to hear new tickets and replies.")
    return payload
