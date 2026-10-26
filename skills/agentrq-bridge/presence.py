"""AgentRQ events and durable Host receipts; no owner-chat entry path."""
from __future__ import annotations

import hashlib
import json
import pathlib
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid


def provider_event(params, mcp_url):
    url = urllib.parse.urlsplit(mcp_url)
    account = urllib.parse.urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path.rstrip("/"), "", ""))
    task = str(params.get("taskId") or params.get("task_id") or "")
    kind = str(params.get("type") or "")
    source = str(params.get("eventId") or params.get("messageId") or params.get("id") or "")
    if not source:
        source = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    facts = ["any_message"] if params.get("text") or params.get("content") else []
    if (kind == "task.created" and params.get("assignee") == "agent") or (kind == "message.created" and params.get("sender") == "human"):
        facts.append("direct_message")
    if params.get("sender") == "agent" or params.get("is_bot") is True:
        facts.append("from_bot")
    return {
        "source_event_id": "agentrq:" + kind + ":" + source, "provider": "agentrq", "account_id": account,
        "conversation_id": task or "workspace", "thread_id": task,
        "conversation_key": f"agentrq:{account}:{task or 'workspace'}",
        "actor": {"platform": "agentrq", "platform_actor_id": str(params.get("senderId") or ""),
                  "display_name": str(params.get("sender") or ""), "is_bot": "from_bot" in facts},
        "conversation": {"platform": "agentrq", "task_id": task},
        "message": dict(params), "text": str(params.get("text") or params.get("content") or params.get("title") or ""),
        "facts": facts,
    }


class PresenceInbox:
    def __init__(self, state_dir, host_base, token, reply):
        self.root = pathlib.Path(state_dir) / "presence"
        self.base, self.token, self.reply = host_base.rstrip("/"), token, reply
        self.lock = threading.RLock()

    def _save(self, path, record):
        temp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    def enqueue(self, event):
        key = json.dumps([event["account_id"], event["source_event_id"]], separators=(",", ":"))
        path = self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                self._save(path, {"event": event, "state": "pending", "outputs": {}})
        return path

    def _request(self, method, route, payload=None):
        request = urllib.request.Request(self.base + route, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"X-Skill-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            payload = json.load(exc)
            if isinstance(payload, dict) and payload.get("disposition"):
                return payload
            raise

    def process(self, path):
        with self.lock:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["state"] in {"completed", "failed", "uncertain"}:
                return
            if record["state"] == "sending":
                record.update(state="uncertain", error="Reply acceptance unknown after restart; inspect AgentRQ before retry")
                self._save(path, record)
                return
        event = record["event"]
        payload = record.get("response") or {}
        if not record.get("terminal"):
            ref = record.get("work_ref") or record.get("turn_ref")
            payload = self._request("GET", "/presence/work/" + urllib.parse.quote(ref, safe="")) if ref else self._request("POST", "/presence/turn", {"event": event})
            record["response"] = payload
            record["turn_ref"] = record.get("turn_ref") or payload.get("turn_ref", "")
            child = payload.get("child_work_ref") or payload.get("work_ref")
            if child and child != record.get("turn_ref"):
                record["work_ref"] = child
            if payload.get("ok") is False:
                record["error"] = str(payload.get("error") or payload.get("disposition") or "Host admission unconfirmed")
                if payload.get("disposition") == "rejected":
                    record["state"] = "failed"
                self._save(path, record)
                return
            status, outcome = payload.get("status"), payload.get("outcome")
            record["terminal"] = status in {"completed", "failed", "cancelled", "interrupted"} and outcome != "deferred"
            if outcome in {"message", "deferred"} and payload.get("text"):
                identity = str(payload.get("output_ref") or ("immediate" if outcome == "deferred" else "final"))
                record["outputs"].setdefault(identity, {"text": payload["text"], "state": "queued"})
            self._save(path, record)  # retain references/output identity before sending
        for output in record["outputs"].values():
            if output["state"] == "delivered":
                continue
            query = urllib.parse.urlencode({key: event[key] for key in ("provider", "account_id", "conversation_id")})
            place = self._request("GET", "/presence/place?" + query)
            if place.get("state") != "active":
                record.update(state="held", error=f"Held: {place.get('label') or 'AgentRQ'} is {place.get('state') or 'unavailable'}")
                self._save(path, record)
                return
            task = event["conversation"]["task_id"]
            if not task:
                record.update(state="held", error="Held: workspace event has no task to reply to")
                self._save(path, record)
                return
            record.update(state="sending", error="")
            self._save(path, record)
            try:
                receipt = self.reply(task, output["text"])
                if receipt.get("error"):
                    raise RuntimeError(str(receipt["error"]))
            except Exception:
                record.update(state="uncertain", error="Reply acceptance unknown; inspect AgentRQ before retry")
                self._save(path, record)
                return
            output.update(state="delivered", receipt=receipt)
            record["state"] = "pending"
            self._save(path, record)
        record["state"] = "completed" if record.get("terminal") else "pending"
        self._save(path, record)

    def process_pending(self):
        for path in sorted(self.root.glob("*.json")):
            try:
                self.process(path)
            except Exception as exc:
                # Source and receipt stay on disk for the next pass; no ACK loss.
                print(f"[listener] Presence retry: {type(exc).__name__}", flush=True)
