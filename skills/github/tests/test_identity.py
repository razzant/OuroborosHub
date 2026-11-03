"""Identity is a verified credential fact on both status and dispatch payloads."""
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gh_presence.config import validate, write_config
from gh_presence.host import Host
from gh_presence.http import Response
from gh_presence.identity import connector_identity
from gh_presence.outbox import Outbox
from gh_presence.provider import GitHub
from gh_presence.http import TransportError
from gh_presence.store import Store, dump, now
from test_status_route import RecordingAPI
from test_transport import CONFIG, HOST_IDENTITY, event


class IdentityTests(unittest.TestCase):
    def fixture(self, root, advertised, *, app=False, reader=True):
        config = validate({**CONFIG, "notifications_user_id": 22 if reader else 0,
                           **({"auth_mode": "app", "app_id": 77, "installation_id": 88} if app else {})})
        write_config(root, config)
        store = Store(root)
        self.addCleanup(store.close)
        writer = {"state": "valid", "auth_mode": "app" if app else "pat", "id": 11,
                  "login": "fixture-app[bot]" if app else "service-writer",
                  **({"app_id": 77, "installation_id": 88, "slug": "fixture-app",
                      "expires_at": "2099-01-01T00:00:00Z"} if app else {})}
        store.set_meta("auth:writer", writer)
        store.set_meta("auth:notifications", {"state": "valid", "auth_mode": "pat", "id": 22, "login": "reader"})
        capabilities = {**HOST_IDENTITY, **({"presence_identity": advertised} if advertised is not None else {})}
        sent = []
        def request(method, path, payload=None):
            if path == "/identity":
                return Response(200, capabilities, {})
            sent.append(copy.deepcopy(payload["event"]))
            return Response(200, {"status": "completed", "outcome": "silent"}, {})
        host = Host(SimpleNamespace(request=request), store, Outbox(store, config))
        self.addCleanup(host.close)
        host.negotiate()
        with store.transaction() as db:
            db.execute("INSERT INTO inbox(id,event,created) VALUES(?,?,?)", ("event-1", dump(event()), now()))
        spec = importlib.util.spec_from_file_location("github_identity_plugin", ROOT / "plugin.py")
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        api = RecordingAPI(root)
        plugin.register(api)
        def status():
            response = asyncio.run(api.routes["status"](SimpleNamespace(query_params={})))
            self.assertEqual(response.status_code, 200)
            return json.loads(response.body)
        return host, store, sent, capabilities, status

    def test_identity_gate_covers_pat_app_and_distinct_reader_on_status_and_wire(self):
        for advertised in (None, 0, 1):
            for app in (False, True):
                for reader in (False, True):
                    with self.subTest(advertised=advertised, app=app, reader=reader), tempfile.TemporaryDirectory() as tmp:
                        host, _, sent, _, status = self.fixture(Path(tmp), advertised, app=app, reader=reader)
                        host.dispatch()
                        for future in host.inflight:
                            future.result(timeout=5)
                        current = status()
                        host.close()
                        if advertised == 1:
                            login = "fixture-app[bot]" if app else "service-writer"
                            expected = {"sends_as": {"id": "11", "label": login, "kind": "bot" if app else "user"}}
                            if reader:
                                expected["reads_as"] = [{"id": "22", "label": "reader", "kind": "user"}]
                            self.assertEqual(current["identity"], expected)
                            self.assertEqual(sent[0]["identity"], expected)
                        else:
                            self.assertNotIn("identity", current)
                            self.assertNotIn("identity", sent[0])

    def test_cached_dispatch_keeps_snapshot_but_omits_it_for_an_older_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, store, sent, capabilities, _ = self.fixture(Path(tmp), 1)
            host.dispatch()
            for future in host.inflight:
                future.result(timeout=5)
            host.dispatch()  # Settle the first wire request.
            snapshot = store.meta("dispatch:event-1")["event"]["identity"]
            capabilities.pop("presence_identity")
            host.negotiate()
            with store.transaction() as db:
                db.execute("UPDATE inbox SET state='ready',due=0")
            host.dispatch()
            for future in host.inflight:
                future.result(timeout=5)
            self.assertNotIn("identity", sent[-1])
            self.assertEqual(store.meta("dispatch:event-1")["event"]["identity"], snapshot)
            host.close()

    def test_unverified_or_changed_credentials_are_not_declared(self):
        for field, value in (("state", "invalid"), ("id", 99), ("app_id", 99),
                             ("installation_id", 99), ("expires_at", "2000-01-01T00:00:00Z")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                host, store, sent, _, status = self.fixture(Path(tmp), 1, app=True)
                record = store.meta("auth:writer")
                store.set_meta("auth:writer", {**record, field: value})
                self.assertNotIn("identity", status())
                host.dispatch()
                for future in host.inflight:
                    future.result(timeout=5)
                self.assertNotIn("identity", sent[0])
                host.close()

    def test_failed_pat_verification_clears_the_previous_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, _, _, status = self.fixture(Path(tmp), 1)
            provider = GitHub(SimpleNamespace(), store)
            provider.get = lambda path: Response(200, {"id": 11, "login": "service-writer"}, {})
            provider.identity(11)
            self.assertEqual(status()["identity"]["sends_as"]["id"], "11")
            provider.get = lambda path: Response(200, {"id": 99, "login": "someone-else"}, {})
            with self.assertRaises(TransportError):
                provider.identity(11)
            self.assertNotIn("identity", status())

    def test_app_identity_uses_the_resolved_bot_user_and_never_the_app_or_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, _, _, status = self.fixture(Path(tmp), 1, app=True)
            writer = store.meta("auth:writer")
            actor = GitHub(None, None).actor({"user": {
                "id": writer["id"], "login": writer["login"], "type": "Bot"}})
            sender = status()["identity"]["sends_as"]
            self.assertEqual(sender, {"id": actor["platform_actor_id"],
                                      "label": writer["login"], "kind": "bot"})
            for unresolved_id in (None, writer["app_id"], writer["installation_id"], writer["slug"]):
                with self.subTest(unresolved_id=unresolved_id):
                    store.set_meta("auth:writer", {**writer, "id": unresolved_id})
                    self.assertNotIn("identity", status())

    def test_reader_matching_the_writer_is_not_declared_twice(self):
        writer = {"state": "valid", "auth_mode": "pat", "id": 11, "login": "service-writer"}
        config = {**CONFIG, "notifications_user_id": 11}
        identity = connector_identity(config, {"auth:writer": writer, "auth:notifications": writer})
        self.assertEqual(identity, {"sends_as": {"id": "11", "label": "service-writer", "kind": "user"}})


if __name__ == "__main__":
    unittest.main()
