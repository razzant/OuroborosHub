"""Freshdesk through a real core's Presence routes (the suite's CI job pins the core; locally an opt-in).

    OUROBOROS_PRESENCE_CORE_ROOT=/path/to/ouroboros python -m pytest -q -rs tests/test_freshdesk_core_consumer.py

Without that variable this is reported as skipped, never as passed. The connector runs unchanged (watcher walk,
event building, Presence protocol, tools with their place check, receipts, late outputs) against the loopback
Freshdesk of its own tests; core's Host Service app answers in process for a connected helpdesk place. Only
skill-token authentication, the focus allowance and the model are stubbed: core's ``run_presence_turn`` builds and
runs each turn. Every run is a subprocess whose HOME, data and settings are temporary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "freshdesk"
CORE_ENV = "OUROBOROS_PRESENCE_CORE_ROOT"
TOKEN = "fixture-skill-token"
HOST = "example.freshdesk.com"


def _run(tmp_path, scenario):
    core = os.environ.get(CORE_ENV, "").strip()
    if not core:
        pytest.skip(f"{CORE_ENV} is not set; the core Presence routes were not exercised")
    env = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home"),
        "OUROBOROS_APP_ROOT": str(tmp_path / "app"), "OUROBOROS_DATA_DIR": str(tmp_path / "data"),
        "OUROBOROS_SETTINGS_PATH": str(tmp_path / "data" / "settings.json"), "OUROBOROS_REPO_DIR": str(tmp_path / "repo"),
        "PYTHONPATH": os.pathsep.join([str(Path(core).resolve()), str(SKILL), str(SKILL / "tests")]),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
    }
    for name in ("home", "app", "data", "repo"):
        (tmp_path / name).mkdir(exist_ok=True)
    run = subprocess.run([sys.executable, __file__, str(tmp_path), scenario], env=env, cwd=tmp_path,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-4000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_events_reach_core_rooms_and_late_outputs_become_private_notes(tmp_path):
    facts = _run(tmp_path, "flow")
    ticket = facts["ticket"]
    # Core accepted every event; the property change was heard without a focus, the others woke one.
    assert set(facts["inbox"].values()) == {"accepted"}
    assert [(t["source"].split(f"{HOST}:")[1], t["facts"]) for t in facts["turns"]] == [
        (f"{ticket}:created", ["any_message", "new_issue"]),
        # The first answer was posted as this agent's private note, so the colleague's note is on a ticket it wrote in.
        (f"{ticket}:entry:{facts['note']}", ["any_message", "my_thread", "mention"])]
    # One ticket, one room: the thread is the ticket, so core's key is freshdesk:<host>:<id>:<id>.
    assert {t["chat_id"] for t in facts["turns"]} == {facts["room"]}
    # What the mind answered after each turn reached the ticket once, as a private note, never as an email.
    assert facts["notes"] == [[f"Looked at {ticket}", True], [f"Looked at {ticket}", True]]
    # Core recorded every receipt: the tool's reply and both automatic notes.
    assert facts["reports"] == {"accepted": 1, "delivered": 2} and facts["report_statuses"] == [200, 200, 200]
    assert facts["reply"]["ok"] is True and facts["place"]["state"] == "active"


def test_a_silent_place_holds_the_tools_before_freshdesk_is_called(tmp_path):
    facts = _run(tmp_path, "silent")
    assert facts["reply"]["code"] == "place_silent" and facts["reply"]["sent"] is False
    assert facts["entries"] == 0


def _drive(root: Path, scenario: str) -> dict:
    """Run the connector against core's app; return what each side recorded, as JSON-safe data."""
    import time

    from starlette.testclient import TestClient

    from fake_freshdesk import KEY, FakeFreshdesk
    from freshdesk_lib.host import HostLink
    from freshdesk_lib.presence import Presence
    from freshdesk_lib.sends import Sends
    from freshdesk_lib.service import Service, write_settings
    from freshdesk_lib.watcher import Budget, baseline, process, walk
    from ouroboros import places, presence_admission, presence_context
    from ouroboros.chronicle_store import ChronicleStore
    from ouroboros.gateway import host_service
    from ouroboros.presence_runner import presence_chat_id, run_presence_turn
    from ouroboros.task_results import write_task_result

    data, repo = root / "data", root / "repo"
    ChronicleStore(data).ensure_activated()
    places.connect_place(data, connector="freshdesk", provider="freshdesk", label="Freshdesk", account_id=HOST,
                         conversation_id="", wakes=["new_issue", "assigned", "my_thread", "mention"],
                         state="silent" if scenario == "silent" else "active")
    presence_admission.focus_enabled = lambda _root: True
    presence_admission.focus_allowance = lambda _root: {"status": "available", "remaining_usd": 2, "settled_usd": 1}
    presence_context._own_work_section = lambda *_args: ""

    async def authenticated(_ctx, raw_token, permission=""):
        assert raw_token == TOKEN
        return "freshdesk", {}
    host_service._authenticated = authenticated
    turns = []

    class Model:
        """The agent seam of core's runner: intake opens as the agent loop opens it, then a final answer."""

        def handle_task(self, task):
            event = task["metadata"]["presence"]["event"]
            opened = getattr(self, "intake_opened_callback", None)
            if opened is not None:
                opened(task)
            turns.append({"source": event["source_event_id"], "facts": event.get("facts"),
                          "chat_id": int(task["chat_id"])})
            answer = f"Looked at {event['conversation_id']}"
            # The agent loop gives a final its canonical output identity (presence_runner.terminal_output); the
            # stub writes the same metadata key, so the work view carries an output_ref the transport can deduplicate.
            write_task_result(data, task["id"], "completed", metadata={
                **task["metadata"], "presence_outcome": "message", "presence_result_text": answer,
                "presence_output_ref": f"{task['id']}:final"}, terminal_origin="model_final", result=answer)
            return [{"type": "presence_result", "outcome": "message", "text": answer}]

    app = host_service.create_host_service_app(data, presence_runner=lambda **kwargs: run_presence_turn(
        repo_dir=repo, drive_root=data, agent_factory=lambda **_kwargs: Model(), **kwargs))
    web = TestClient(app)
    report_statuses = []

    def transport(method, path, headers, body, params):
        response = web.request(method, path, headers=headers, json=body, params=params)
        if path == "/presence/delivery":
            report_statuses.append(response.status_code)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {}

    fake = FakeFreshdesk()
    fake.base = fake.start()
    state = root / "skill-state"
    write_settings(state, {"domain": HOST})
    host = HostLink(lambda: TOKEN, transport=transport)
    service = Service(state, lambda: KEY, base_url=fake.base, timeout=10.0, host=host)
    store = service.store()
    presence = Presence(store, host, sends=Sends(service), account=HOST)
    out: dict = {"place": host.place(HOST, "", "")}
    try:
        capabilities = presence.negotiate()
        assert capabilities["ready"], capabilities

        def cycle():
            client = service.client()
            baseline(client, store)
            budget = Budget(store, share=25, poll_seconds=60)
            budget.left = 10_000
            walk(client, store, budget)
            process(service, client, store, budget, capabilities=capabilities)
            for _ in range(400):
                store.db.execute("UPDATE refs SET due=0 WHERE state='pending'")
                presence.dispatch()
                time.sleep(0.05)
                presence.settle()
                presence.poll_refs(limit=20)
                presence.deliver_outputs()
                presence.report_receipts()
                busy = store.one("SELECT (SELECT COUNT(*) FROM inbox WHERE state IN ('ready','submitting')) + "
                                 "(SELECT COUNT(*) FROM refs WHERE state='pending') + "
                                 "(SELECT COUNT(*) FROM outputs WHERE state='queued') AS n")["n"]
                if not busy and not presence.inflight:
                    return

        fake.settle()
        cycle()
        fake.settle()
        if scenario == "silent":
            ticket = fake.add_ticket(subject="Charged twice", description="Two charges this month.")
            out["reply"] = Sends(service).reply(None, account_id=HOST, ticket_id=ticket["id"], text="Hello",
                                                request_id="r-silent")
            out["entries"] = len(fake.conversations[ticket["id"]])
            return out
        ticket = fake.add_ticket(subject="Charged twice", description="Two charges this month.")
        cycle()
        fake.settle()
        fake.tickets[ticket["id"]].update(status=3, updated_at=fake.iso_now())
        cycle()
        fake.settle()
        note = fake.add_entry(ticket["id"], body="Can you check the refund?", user_id=9002, incoming=False,
                              private=True, source=2, to_emails=["desk.agent@example.net"])
        cycle()
        out["reply"] = Sends(service).reply({"task_id": "task-1"}, account_id=HOST, ticket_id=ticket["id"],
                                            text="We are on it.", request_id="r-flow")
        presence.report_receipts()
        out.update(ticket=ticket["id"], note=note["id"], turns=turns,
                   room=presence_chat_id(f"freshdesk:{HOST}:{ticket['id']}:{ticket['id']}"),
                   inbox={r["source_event_id"]: r["state"] for r in store.rows("SELECT * FROM inbox")},
                   notes=[[e["body_text"], e["private"]] for e in fake.conversations[ticket["id"]]
                          if e["source"] == 2 and e["user_id"] != 9002],
                   reports={}, report_statuses=report_statuses)
        for row in store.rows("SELECT state FROM journal WHERE report_state='done'"):
            out["reports"][row["state"]] = out["reports"].get(row["state"], 0) + 1
        return out
    finally:
        presence.close()
        store.close()
        fake.stop()


if __name__ == "__main__":
    print(json.dumps(_drive(Path(sys.argv[1]), sys.argv[2]), ensure_ascii=False, default=str))
