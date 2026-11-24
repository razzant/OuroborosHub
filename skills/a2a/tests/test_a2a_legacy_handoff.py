"""A receipt saved by 1.5 for a turn that handed its work to a child: today's ordinary read returns the child's answer.

The receipt is written as the 1.5 serializer wrote it (``PresenceTransport._checkpoint`` of a2a 1.5.1-1.5.3): the
source event, the host's last answer kept whole as ``response``, the first ``turn_ref`` and the child as
``work_ref``. That answer is the host's legacy ``/presence/turn`` body (``status: completed``, ``outcome: deferred``,
its own ``turn_ref`` and the child's ``work_ref``): the turn itself has ended. Only the child is read; the finished
turn is not read again and nothing is offered. The host is an in-memory adapter with fictional references and text.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import urlsplit

SKILL = Path(__file__).resolve().parents[1]
SOURCE = "a2a:fictional-legacy-letter"
TURN, CHILD = "turn-fictional-1", "work-fictional-child"


class _Answer:
    def __init__(self, status_code, payload):
        self.status_code, self._payload = status_code, payload

    def json(self):
        return self._payload


class _Host:
    """The Host Service's work reads, in memory: the child has finished with its answer."""

    def __init__(self):
        self.requests = []

    def get(self, url, headers=None, timeout=None):
        path = urlsplit(url).path
        self.requests.append(("GET", path))
        if path == f"/presence/work/{CHILD}":
            return _Answer(200, {"ok": True, "status": "completed", "outcome": "message",
                                 "text": "Fictional child answer.", "output_ref": "output-fictional-1",
                                 "work_ref": CHILD, "task_id": CHILD, "child_work_ref": ""})
        return _Answer(404, {"ok": False, "code": "presence_work_not_found"})

    def post(self, url, json=None, headers=None, timeout=None):
        self.requests.append(("POST", urlsplit(url).path))
        return _Answer(500, {"ok": False})


def test_a_15_handoff_receipt_reads_the_existing_child_answer(tmp_path):
    spec = importlib.util.spec_from_file_location("a2a_presence_legacy_handoff", SKILL / "scripts/a2a_presence.py")
    presence = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(presence)
    host = _Host()
    transport = presence.PresenceTransport(tmp_path, "http://127.0.0.1:8767", lambda: {"X-Skill-Token": "fictional"},
                                           timeout=60, poll_interval=0.01, http=host,
                                           facts=lambda: {"presence_attention_version": 1, "places_version": 1})
    event = {"source_event_id": SOURCE, "provider": "a2a", "account_id": "server", "conversation_id": "context-1",
             "thread_id": "", "conversation_key": "a2a:server:context-1",
             "actor": {"platform": "a2a", "platform_actor_id": "", "is_bot": True},
             "conversation": {"platform": "a2a"}, "message": {"message_id": SOURCE},
             "text": "A fictional question", "facts": ["direct_message", "from_bot"]}
    handoff = {"ok": True, "status": "completed", "outcome": "deferred", "text": "", "turn_ref": TURN,
               "work_ref": CHILD, "delivery_reporting_version": 0}
    receipt = transport._path(SOURCE)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"event": event, "response": handoff, "turn_ref": TURN, "work_ref": CHILD}),
                       encoding="utf-8")

    view = transport.read(SOURCE)

    assert (view["status"], view["outcome"], view["text"]) == ("completed", "message", "Fictional child answer.")
    assert host.requests == [("GET", f"/presence/work/{CHILD}")]  # the child only; the ended turn is not read again
