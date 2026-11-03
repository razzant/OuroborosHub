#!/usr/bin/env python3
"""Validate against dependency-only copies of an explicitly selected host source.

No installation, review, grant, publication, server startup or model call. Set up
private HOME/data before importing *any* host module. Copy only Python modules
actually imported to a disposable directory; never put the serving source on
sys.path. The manifest and registration implementation remain unmodified.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-source", required=True, type=Path)
    args = parser.parse_args()
    source = args.host_source.resolve()
    payload = Path(__file__).resolve().parents[1]
    original_cwd = Path.cwd()
    copied = {}
    with tempfile.TemporaryDirectory(prefix="github-host-check-") as scratch:
        root = Path(scratch).resolve()
        for folder in ("home", "data", "repo", "state", "tmp"):
            (root / folder).mkdir()
        # A fresh dependency-only runtime; inherited native credentials and host
        # control variables never enter the host module import environment.
        os.environ.clear()
        os.environ.update({"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
                           "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
                           "OUROBOROS_DATA_DIR": str(root / "data"), "OUROBOROS_REPO_DIR": str(root / "repo"),
                           "OUROBOROS_SETTINGS_PATH": str(root / "data/settings.json"),
                           "OUROBOROS_EXTENSION_PROCESS_CHILD": "1", "OUROBOROS_IN_WORKER": "1",
                           "PYTHONDONTWRITEBYTECODE": "1"})
        os.chdir(root / "repo")
        tempfile.tempdir = str(root / "tmp")
        sys.dont_write_bytecode = True
        class SourceCopies(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname != "ouroboros" and not fullname.startswith("ouroboros."):
                    return None
                relative = Path(*fullname.split("."))
                candidate = source / relative.with_suffix(".py")
                package = False
                if not candidate.is_file():
                    candidate = source / relative / "__init__.py"
                    package = True
                if not candidate.is_file():
                    return None
                destination = root / "repo" / candidate.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                content = candidate.read_bytes()
                destination.write_bytes(content)
                copied[str(candidate.relative_to(source))] = {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                return importlib.util.spec_from_file_location(fullname, destination,
                        submodule_search_locations=[str(destination.parent)] if package else None)
        sys.meta_path.insert(0, SourceCopies())
        def no_network(*args, **kwargs):
            raise AssertionError("registration check must not open a network connection")
        socket.create_connection = no_network
        original_connect = sqlite3.connect
        def isolated_sqlite(database, *args, **kwargs):
            if database != ":memory:" and not Path(database).resolve().is_relative_to(root):
                raise AssertionError("SQLite path outside isolated test root")
            return original_connect(database, *args, **kwargs)
        sqlite3.connect = isolated_sqlite
        try:
            from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
            from ouroboros.extension_ui_validation import validate_contributions
            from ouroboros.extension_plugin_api import PluginAPIImpl
            from ouroboros.extension_registry_state import _PluginAPIConfig
            from ouroboros.presence_delivery import validate_delivery
            from ouroboros.presence_observations import validate_transport_queue
            manifest = parse_skill_manifest_text((payload / "SKILL.md").read_text())
            contributions, problems = validate_contributions(manifest, skill_dir=payload)
            assert len(contributions) == 1 and not problems, (contributions, problems)
            config = _PluginAPIConfig(skill_name=manifest.name, permissions=manifest.permissions,
                    env_allowlist=manifest.env_from_settings, state_dir=root / "state",
                    settings_reader=lambda: {}, drive_root=root / "data", skill_dir=payload,
                    companion_processes=manifest.companion_processes, plugin_api_generation="2.0")
            api = PluginAPIImpl(config)
            spec = importlib.util.spec_from_file_location("github_candidate", payload / "plugin.py")
            plugin = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(plugin)
            plugin.register(api)
            registrations = api._staged
            names = [row["name"].rsplit("_", 1)[-1] for row in registrations.tools.values()]
            assert len(registrations.tools) == 3
            assert len(registrations.routes) == 3
            assert len(registrations.settings_sections) == 1
            assert not registrations.ui_tabs
            assert registrations.companion_names == ["github_poller"]
            assert not registrations.companion_spawns  # catalog only, never enabled
            from starlette.requests import Request
            status_handler = registrations.routes["/api/extensions/github/status"]["handler"]
            async def inspect_status():
                response = await status_handler(Request({"type": "http", "method": "GET", "path": "/status", "query_string": b"", "headers": []}))
                assert response.status_code == 200, response.body
                assert not (root / "state/transport.sqlite3").exists(), "status created a database"
            asyncio.run(inspect_status())
            for item in contributions:
                assert "/api/extensions/github/" + item["source"]["route"] in registrations.routes
                assert "github:" + item["settings"] in registrations.settings_sections
            receipt = {"schema_version": 1, "delivery_id": "tool:fixture", "part_id": "0", "state": "delivered",
                       "provider": "github", "account_id": "fixture-account", "conversation_id": "repo.321", "thread_id": "issue.1",
                       "text": "synthetic reply", "format": "markdown", "message": {"provider_id": 701},
                       "origin": {"kind": "tool", "source_event_id": "fixture-event"}}
            validate_delivery(receipt)
            # Feed actual connector-produced uncertain/reconciled reports into
            # the real host validator, not a lookalike loopback schema.
            sys.path.insert(0, str(payload / "tests"))
            from test_transport import Case, WRITER, CONFIG
            case = Case()
            case.setUp()
            try:
                case.provider.fail_post = True
                case.send_tool()
                case.advance()
                case.sender.reconcile_uncertain()
                part = case.store.one("SELECT * FROM parts")
                case.provider.rows.append({"id": 701, "user": WRITER, "body": part["body"],
                                           "issue_url": case.provider.http.base + "/repos/example/project/issues/1"})
                with case.store.transaction() as db:
                    db.execute("UPDATE outbox SET due=0")
                case.sender.reconcile_uncertain()
                reports = [validate_delivery(json.loads(r["value"])) for r in case.store.rows("SELECT value FROM reports")]
                assert {r["state"] for r in reports} == {"uncertain", "delivered"}
                from ouroboros.presence_delivery import _key
                assert len({_key("github", r) for r in reports}) == 2
                # Exact source-backed discovery, through the real place resolver.
                # These are temporary synthetic Places, never the host's Places.
                from test_repairs import DiscoveryTests
                from gh_presence.discovery import public_subject, discovery_event
                notification = DiscoveryTests().notification()
                repo, target, route, history = public_subject(case.provider, notification)
                discovery = discovery_event("fixture-notification", 1, None, config=CONFIG,
                            notification=notification, recipient=22, repo=repo, target=target, route=route, history=history)
                from ouroboros.presence_admission import admit_presence_turn, PresenceAdmissionError
                from ouroboros.places import connect_place, edit_place, load_places, wake_matches
                address = {k: discovery[k] for k in ("provider", "account_id", "conversation_id", "thread_id")}
                def admit():
                    return admit_presence_turn(drive_root=root / "data", authenticated_transport_skill="github", **address)
                try:
                    admit()
                except PresenceAdmissionError as exc:
                    assert exc.code == "place_not_connected"
                else:
                    raise AssertionError("unconnected discovery was admitted")
                place = connect_place(root / "data", connector="github", provider="github",
                                      account_id=discovery["account_id"], conversation_id="", wakes=["any_message"])
                edit_place(root / "data", place.place_id, "set_state", author="owner", state="silent")
                admitted = admit()
                assert admitted.origin.conversation_id == "*" and admitted.place.state == "silent"
                assert admitted.destination.conversation_id == discovery["conversation_id"]
                assert wake_matches(admitted.place, discovery["facts"])
                assert len(load_places(root / "data")) == 1  # no per-repository enrollment
            finally:
                case.tearDown()
            queue_observation = {"schema_version": 1, "source": "fixture", "observed_at": "2026-10-23T00:00:00+00:00",
                                      "conversation_key": "github:fixture-account:repo.321:issue.1", "after_source_event_id": "fixture-event",
                                      "complete": True, "pending_count": 1, "omitted_count": 0,
                                      "events": [{"source_event_id": "arrival", "text": "synthetic", "text_chars": 9}],
                                      "held_intents": [{"delivery_id": "tool:fixture", "source_event_id": "fixture-event",
                                                       "state": "needs_reconsideration", "observation": {"current_fingerprint": "fixture"}}]}
            validate_transport_queue(queue_observation,
                                     "github:fixture-account:repo.321:issue.1", "fixture-event")
            from ouroboros.task_store import patch_document
            from ouroboros.task_results import load_task_result, stamp_task_result_schema
            from ouroboros.presence_observations import record_transport_queue, transport_queue_observation
            source_event = {"source_event_id": "fixture-event", "conversation_key": "github:fixture-account:repo.321:issue.1", "conversation": {}}
            patch_document(root / "data", "fixture-work", lambda _: stamp_task_result_schema({
                "task_id": "fixture-work", "status": "completed", "output_ref": "retained-output",
                "metadata": {"presence": {"binding_id": "fixture-binding", "transport_skill": "github", "event": source_event}}}))
            assert record_transport_queue(root / "data", "fixture-work", "fixture-binding", queue_observation)["status"] == "recorded"
            assert record_transport_queue(root / "data", "fixture-work", "fixture-binding", queue_observation)["status"] == "duplicate"
            observed = transport_queue_observation(root / "data", "fixture-work", SimpleNamespace(**source_event))
            assert observed["status"] == "available" and observed["snapshot"]["held_intents"] == queue_observation["held_intents"]
            saved_work = load_task_result(root / "data", "fixture-work")
            assert saved_work["status"] == "completed" and saved_work["output_ref"] == "retained-output"
            try:
                record_transport_queue(root / "data", "fixture-work", "fixture-binding", {**queue_observation, "after_source_event_id": "foreign-source"})
            except ValueError:
                pass
            else:
                raise AssertionError("host accepted a mismatched source observation")
            from ouroboros.places import WAKE_FACTS
            print(json.dumps({"manifest": manifest.name, "contributions": len(contributions), "problems": problems,
                              "tools": sorted(registrations.tools), "routes": sorted(registrations.routes),
                              "settings": sorted(registrations.settings_sections), "widgets": sorted(registrations.ui_tabs),
                              "companions_catalogued_only": registrations.companion_names,
                              "real_delivery_and_queue_validators": "passed",
                              "real_queue_persistence_and_readback": "passed; terminal work remains terminal, no wake proved",
                              "status_noncreating_consumer": "passed",
                              "real_discovery_place_admission": "passed; owner fixture with blank conversation maps to account-wide *, Silent remains Silent; Main not exercised",
                              "full_watch_readiness": "not established by registration; explicit identity advertisement and host consumer qualification required",
                              "pending_host_facts": sorted({"assigned", "review_requested"} - set(WAKE_FACTS))}, indent=2))
        finally:
            print(json.dumps({"host_modules_copied_on_import": len(copied), "source_bytes": sum(x["bytes"] for x in copied.values()),
                              "sources": copied}, indent=2))
            os.chdir(original_cwd)


if __name__ == "__main__":
    main()
