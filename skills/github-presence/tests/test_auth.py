"""Real RSA signatures and loopback HTTP contracts; no live credentials/provider."""
from __future__ import annotations

import base64
import concurrent.futures
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from gh_presence.auth import InstallationAuth
from gh_presence.config import SECRET_KEYS, account_id, connection_account, provider_http, validate, writer_http
from gh_presence.http import HTTP, Effect, TransportError
from gh_presence.provider import GitHub, can_close_issue
from gh_presence.store import Store
from test_transport import CONFIG, REPO


class Clock:
    def __init__(self):
        self.wall, self.mono = time.time(), 1000

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class AppServer:
    def __init__(self, key, clock, prefix=""):
        self.key, self.clock, self.prefix = key, clock, prefix
        self.calls, self.errors, self.claims, self.mints = [], [], [], []
        self.overrides = {}
        self.bot = {"id": 901, "login": "fixture-app[bot]", "type": "Bot"}
        self.permissions = {"metadata": "read", "issues": "write", "pull_requests": "write", "discussions": "read"}
        self.repositories = [{"id": 321, "full_name": "example/project"}]
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def run_request(self):
                data = self.rfile.read(int(self.headers.get("content-length", 0)))
                data = json.loads(data) if data else None
                path = self.path.removeprefix(owner.prefix)
                credential = self.headers.get("Authorization", "")
                owner.calls.append((self.command, self.path, data, dict(self.headers)))
                try:
                    if path == "/app" or path.startswith("/app/installations/"):
                        owner.verify_jwt(credential)
                    elif credential not in ("Bearer notifications-fixture", "Bearer pat-fixture"):
                        assert credential in ["Bearer " + t for t in owner.mints], "unexpected installation credential"
                    result = owner.dispatch(self.command, path, data)
                except Exception as exc:
                    owner.errors.append(type(exc).__name__)
                    result = 500, {"message": "contract assertion failed"}, {}
                if result is None:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                status, value, headers = result
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
            do_GET = do_POST = do_PATCH = run_request
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + prefix
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def verify_jwt(self, credential):
        assert credential.startswith("Bearer ")
        encoded = credential.removeprefix("Bearer ")
        head, payload, signature = encoded.split(".")
        decode = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        self.key.public_key().verify(decode(signature), (head + "." + payload).encode(), padding.PKCS1v15(), hashes.SHA256())
        header, claims = json.loads(decode(head)), json.loads(decode(payload))
        assert header["alg"] == "RS256"
        assert claims["iss"] in ("77", "Iv.fixture-client")
        assert claims["iat"] == int(self.clock.wall) - 60
        assert int(self.clock.wall) < claims["exp"] <= int(self.clock.wall) + 600
        self.claims.append(claims)

    def dispatch(self, method, path, data):
        key = method, path
        if key in self.overrides:
            value = self.overrides[key]
            return value(method, path, data) if callable(value) else value
        if path == "/app":
            return 200, {"id": 77, "client_id": "Iv.fixture-client", "slug": "fixture-app"}, {}
        if path == "/app/installations/88":
            return 200, {"id": 88, "app_id": 77, "app_slug": "fixture-app", "suspended_at": None,
                         "permissions": {**self.permissions, "administration": "write", "contents": "write"}}, {}
        if path == "/app/installations/88/access_tokens":
            assert data == {"repository_ids": [r["id"] for r in self.repositories], "permissions": self.permissions}
            token = "fixture-installation-" + str(len(self.mints) + 1)
            self.mints.append(token)
            return 201, {"token": token, "expires_at": datetime.fromtimestamp(
                self.clock.wall + 3600, timezone.utc).isoformat(), "permissions": self.permissions}, {}
        if path == "/installation/repositories?per_page=100":
            return 200, {"repositories": self.repositories}, {}
        if path == "/users/fixture-app%5Bbot%5D":
            return 200, self.bot, {}
        if path == "/user":
            return 200, {"id": 22, "login": "reader", "type": "User"}, {}
        if path == "/notifications?all=true&per_page=1":
            return 200, [], {}
        if path == "/repos/example/project":
            return 200, {**self.repositories[0], "private": False}, {}
        if path == "/graphql":
            return 200, {"data": {"viewer": None}}, {}
        return 200, {"ok": True}, {}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()

    def setUp(self):
        self.clock = Clock()
        self.server = AppServer(self.key, self.clock)
        self.addCleanup(self.server.close)
        self.tmp = tempfile.TemporaryDirectory(prefix="github-app-contract-")
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.config = validate({"auth_mode": "app", "app_id": 77, "installation_id": 88,
                                "app_client_id": "Iv.fixture-client", "expected_writer_id": 901,
                                "repositories": [REPO], "notifications_user_id": 22})
        # HTTP is permitted only for explicit literal-loopback fixtures. Product
        # validate() continues to require HTTPS for every provider endpoint.
        self.config.update(api_base_url=self.server.url, graphql_url=self.server.url + "/graphql")
        self.auth, self.http = self.client()
        self.provider = GitHub(self.http, self.store, config=self.config)

    def client(self, **changes):
        config = {**self.config, **changes}
        auth = InstallationAuth(config, self.pem, test_loopback=True,
                                clock=lambda: self.clock.wall, monotonic=lambda: self.clock.mono)
        return auth, HTTP(config["api_base_url"], auth, test_loopback=True, graphql_url=config["graphql_url"])

    def tearDown(self):
        self.assertEqual(self.server.errors, [])

    def test_crypto_identity_scope_and_status_separate_bot_from_installation(self):
        self.assertEqual(self.provider.identity(901), self.server.bot)
        self.assertTrue(self.server.claims)
        status = self.store.meta("auth:writer")
        self.assertEqual((status["id"], status["app_id"], status["installation_id"]), (901, 77, 88))
        self.assertEqual(status["permissions"], self.server.permissions)
        self.assertEqual(status["expires_at"], self.auth.expires_at)
        self.assertNotIn("/user", [r[1] for r in self.server.calls])
        serialized = json.dumps(self.store.status())
        self.assertNotIn(self.server.mints[0], serialized)
        self.assertNotIn(self.pem, serialized)

    def test_app_id_issuer_supported_without_client_id(self):
        _, http = self.client(app_client_id="")
        self.assertEqual(http.request("GET", "/resource").status, 200)
        self.assertEqual(self.server.claims[0]["iss"], "77")

    def test_crypto_negative_controls_reject_wrong_key_and_non_RSA_key(self):
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric import ec
        self.http.request("GET", "/resource")
        bearer = self.server.calls[0][3]["Authorization"]
        original = self.server.key
        self.server.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        try:
            with self.assertRaises(InvalidSignature):
                self.server.verify_jwt(bearer)
        finally:
            self.server.key = original
        ec_key = ec.generate_private_key(ec.SECP256R1())
        ec_pem = ec_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()).decode()
        for key in ("", "invalid synthetic key", ec_pem):
            with self.assertRaisesRegex(TransportError, "private_key_invalid"):
                InstallationAuth(self.config, key, test_loopback=True)

    def test_closure_uses_verified_issues_grant_without_fabricated_push(self):
        self.provider.identity(901)
        repo = self.provider.repository(REPO)
        self.assertNotIn("permissions", repo)
        self.assertTrue(can_close_issue(self.provider, repo, {}, 901))
        self.assertFalse(can_close_issue(self.provider, {"id": 999}, {}, 901))
        self.assertFalse(can_close_issue(self.provider, repo, {}, 88))
        self.server.permissions["issues"] = "read"
        self.clock.advance(3601)
        self.assertFalse(can_close_issue(self.provider, repo, {}, 901))

    def test_all_installation_repository_pages_are_verified(self):
        repos = [REPO, {"id": 322, "full_name": "example/second"}]
        self.server.repositories = repos
        self.server.overrides["GET", "/installation/repositories?per_page=100"] = (
            200, {"repositories": [REPO]}, {"Link": '<' + self.server.url + '/installation/repositories?page=2>; rel="next"'})
        self.server.overrides["GET", "/installation/repositories?page=2"] = (200, {"repositories": [repos[1]]}, {})
        _, http = self.client(repositories=repos)
        self.assertEqual(http.request("GET", "/resource").status, 200)
        self.assertIn("/installation/repositories?page=2", [r[1] for r in self.server.calls])
        self.server.overrides["GET", "/installation/repositories?page=2"] = (500, {}, {})
        _, http = self.client(repositories=repos)
        with self.assertRaisesRegex(TransportError, "app_auth_http_500"):
            http.request("POST", "/must-not-send")
        self.assertNotIn("/must-not-send", [r[1] for r in self.server.calls])

    def test_concurrent_expiry_renews_once_and_monotonic_survives_clock_rollback(self):
        self.http.request("GET", "/resource")
        self.clock.advance(3541)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.http.request, "GET", "/resource") for _ in range(16)]
            self.assertTrue(all(f.result().status == 200 for f in futures))
        self.assertEqual(len(self.server.mints), 2)
        self.clock.mono += 3601
        self.clock.wall -= 1000
        self.http.request("GET", "/resource")
        self.assertEqual(len(self.server.mints), 3)

    def test_401_invalidates_next_operation_without_replaying_get_or_post(self):
        for method in ("GET", "POST", "PATCH"):
            self.server.overrides[method, "/resource"] = (401, {"message": "Bad credentials"}, {})
            before = len(self.server.calls)
            self.assertEqual(self.http.request(method, "/resource").status, 401)
            self.assertEqual(sum(r[1] == "/resource" for r in self.server.calls[before:]), 1)
            count = len(self.server.mints)
            self.http.request("GET", "/next-resource")
            self.assertEqual(len(self.server.mints), count + 1)
        latest = self.auth.token_for(self.http)
        self.auth.invalidate(self.server.mints[0])
        self.assertEqual(self.auth.token_for(self.http), latest)

    def test_403_does_not_refresh_or_replay(self):
        self.server.overrides["POST", "/resource"] = (403, {"message": "Forbidden"}, {})
        self.assertEqual(self.http.request("POST", "/resource").status, 403)
        self.http.request("GET", "/next-resource")
        self.assertEqual(len(self.server.mints), 1)

    def test_unknown_mutation_never_replayed_even_after_token_renewal(self):
        for failure in ((503, {"message": "possibly committed"}, {}), None):
            self.server.overrides["POST", "/repos/example/project/issues/1/comments"] = failure
            before = len(self.server.calls)
            with self.assertRaises(TransportError) as error:
                self.provider.send({"repo": REPO, "kind": "comment", "number": 1}, "synthetic body")
            self.assertEqual(error.exception.effect, Effect.UNKNOWN)
            self.clock.advance(3601)
            self.provider.identity(901)
            posts = [r for r in self.server.calls[before:] if r[0] == "POST" and r[1].endswith("/comments")]
            self.assertEqual(len(posts), 1)

    def test_refresh_failure_never_uses_stale_token_and_cools_down(self):
        self.http.request("GET", "/resource")
        self.clock.advance(3541)
        self.server.overrides["POST", "/app/installations/88/access_tokens"] = (401, {"token": "reflected-secret"}, {})
        before = len(self.server.calls)
        for _ in range(3):
            with self.assertRaisesRegex(TransportError, "app_auth_http_401") as err:
                self.http.request("POST", "/resource", {"body": "never sent"})
            self.assertIsNone(err.exception.response)
            self.assertNotIn("reflected-secret", str(err.exception))
        calls = self.server.calls[before:]
        self.assertEqual(sum(r[1].endswith("/access_tokens") for r in calls), 1)
        self.assertFalse(any(r[1] == "/resource" for r in calls))
        self.clock.advance(61)
        del self.server.overrides["POST", "/app/installations/88/access_tokens"]
        self.http.request("GET", "/resource")
        self.assertEqual(len(self.server.mints), 2)

    def test_refresh_updates_visible_expiry_and_auth_failure_status(self):
        self.provider.identity(901)
        old = self.store.meta("auth:writer")["expires_at"]
        self.clock.advance(3601)
        self.provider.get("/resource")
        self.assertNotEqual(self.store.meta("auth:writer")["expires_at"], old)
        self.clock.advance(3601)
        self.server.overrides["GET", "/app"] = (403, {"message": "denied"}, {})
        with self.assertRaisesRegex(TransportError, "app_auth_http_403"):
            self.provider.get("/resource")
        self.assertEqual(self.store.meta("auth:writer")["state"], "invalid")

    def test_graphql_partial_mutation_and_failed_readback_do_not_replay(self):
        self.server.overrides["POST", "/graphql"] = (200, {"data": {"addDiscussionComment": None},
            "errors": [{"type": "INTERNAL", "message": "possibly committed"}]}, {})
        with self.assertRaises(TransportError) as err:
            self.provider.graphql("mutation { addDiscussionComment { comment { id } } }", {}, mutation=True)
        self.assertEqual(err.exception.effect, Effect.UNKNOWN)
        self.assertEqual(sum(r[1] == "/graphql" for r in self.server.calls), 1)
        self.server.overrides["POST", "/repos/example/project/issues/1/comments"] = (201, {"id": 123}, {})
        self.server.overrides["GET", "/repos/example/project/issues/comments/123"] = (401, {}, {})
        route = {"repo": REPO, "kind": "comment", "number": 1}
        created = self.provider.send(route, "synthetic")
        with self.assertRaises(TransportError):
            self.provider.readback(route, created)
        self.provider.identity(901)
        self.assertEqual(sum(r[0] == "POST" and r[1].endswith("/comments") for r in self.server.calls), 1)

    def test_invalid_expiry_and_wrong_identity_scope_refused(self):
        failures = [
            (("GET", "/app"), (200, {"id": 99, "slug": "fixture-app"}, {}), "app_identity"),
            (("GET", "/app/installations/88"), (200, {"id": 88, "app_id": 99}, {}), "app_installation"),
            (("GET", "/users/fixture-app%5Bbot%5D"), (200, {**self.server.bot, "id": 88}, {}), "app_bot"),
            (("GET", "/users/fixture-app%5Bbot%5D"), (200, {**self.server.bot, "type": "User"}, {}), "app_bot"),
            (("GET", "/installation/repositories?per_page=100"), (200, {"repositories": [{"id": 999}]}, {}), "app_token_repositories"),
            (("POST", "/app/installations/88/access_tokens"), (201, {"token": "expired", "expires_at": "2020-01-01T00:00:00Z"}, {}), "app_token_response"),
        ]
        for key, value, code in failures:
            with self.subTest(code=code):
                self.server.overrides = {key: value}
                auth, http = self.client()
                with self.assertRaisesRegex(TransportError, code):
                    http.request("GET", "/resource")
                self.assertEqual(auth._token, "")

    def test_token_pagination_same_origin_and_redirect_never_forward_secrets(self):
        other = AppServer(self.key, self.clock)
        self.addCleanup(other.close)
        for key, value in (
            (("GET", "/app"), (302, {}, {"Location": other.url + "/app"})),
            (("GET", "/installation/repositories?per_page=100"), (200, {"repositories": [REPO]},
                {"Link": '<' + other.url + '/installation/repositories?page=2>; rel="next"'})),
        ):
            self.server.overrides = {key: value}
            _, http = self.client()
            with self.assertRaises(TransportError):
                http.request("GET", "/resource")
            self.assertFalse(other.calls)
        self.server.overrides = {}
        for path in (other.url + "/resource", "http://localhost:" + str(self.server.server.server_port) + "/resource",
                     self.server.url + "/../resource", self.server.url.replace("http:", "https:") + "/resource"):
            before = len(self.server.calls)
            with self.assertRaises(ValueError):
                self.http.request("GET", path)
            self.assertEqual(len(self.server.calls), before)
        with self.assertRaisesRegex(ValueError, "header_override"):
            self.http.request("GET", "/resource", headers={"Authorization": "Bearer wrong"})

    def test_ghes_paths_and_graphql_share_exact_endpoint_binding(self):
        other = AppServer(self.key, self.clock, prefix="/api/v3")
        self.addCleanup(other.close)
        other.overrides["POST", "/api/graphql"] = (200, {"data": {}}, {})
        # The GraphQL endpoint sits outside the REST base on the same origin.
        graphql = other.url.removesuffix("/api/v3") + "/api/graphql"
        auth, http = self.client(api_base_url=other.url, graphql_url=graphql)
        self.assertEqual(http.request("POST", "/graphql", {"query": "query { viewer { login } }"}).status, 200)
        self.assertIn("/api/graphql", [r[1] for r in other.calls])
        self.assertTrue(all(r[3]["X-Github-Api-Version"] == "2026-03-10" for r in other.calls))
        unbound = HTTP(self.server.url, auth, test_loopback=True)
        with self.assertRaisesRegex(ValueError, "endpoint_mismatch"):
            unbound.request("GET", "/resource")

    def test_separate_notifications_PAT_stays_get_only(self):
        reader = GitHub(HTTP(self.server.url, "notifications-fixture", test_loopback=True, readonly=True),
                        self.store, "notifications", config=self.config)
        self.assertEqual(reader.identity(22)["id"], 22)
        reader.get("/notifications?all=true&per_page=1")
        with self.assertRaisesRegex(ValueError, "get_only"):
            reader.http.request("POST", "/notifications")
        self.assertEqual(self.server.mints, [])


class ConfigurationTests(unittest.TestCase):
    def test_namespace_PAT_compatibility_and_installation_separation(self):
        config = validate({"auth_mode": "app", "app_id": 77, "installation_id": 88,
                           "expected_writer_id": 901, "repositories": [REPO]})
        account = connection_account(config)
        self.assertEqual(account, "gh1." + "https://api.github.com".encode().hex() + ".app.77.installation.88")
        self.assertNotEqual(account, connection_account({**config, "installation_id": 89}))
        self.assertNotEqual(account, connection_account({**config, "app_id": 78}))
        self.assertNotEqual(account, connection_account({**config, "api_base_url": "https://ghes.invalid/api/v3"}))
        self.assertEqual(connection_account(CONFIG), account_id(11))
        with self.assertRaisesRegex(ValueError, "no_user_notifications"):
            validate({**config, "writer_notifications": True})
        for repos in ([], [{"id": i + 1, "full_name": "a/r" + str(i)} for i in range(501)]):
            with self.assertRaisesRegex(ValueError, "explicit_repositories"):
                validate({**config, "repositories": repos})

    def test_PAT_works_with_signing_imports_unavailable_no_fallback(self):
        original = __import__
        def no_crypto(name, *args, **kwargs):
            if name.split(".")[0] in ("jwt", "cryptography"):
                raise ImportError("synthetic missing dependency")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=no_crypto):
            http = writer_http(CONFIG, {SECRET_KEYS[0]: "pat-fixture"})
            self.assertIsNone(http.auth)
            with self.assertRaisesRegex(ValueError, "credential_missing"):
                writer_http(CONFIG, {"GITHUB_TOKEN": "must-not-be-used"})
            with self.assertRaisesRegex(TransportError, "app_dependency_missing"):
                writer_http({**CONFIG, "auth_mode": "app"}, {SECRET_KEYS[0]: "no-fallback"})

    def test_manifest_declares_signer_and_key_without_changing_PAT_default(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        manifest = yaml.safe_load((root / "SKILL.md").read_text().split("---", 2)[1])
        self.assertIn("PyJWT[crypto]>=2.13,<3", manifest["dependencies"])
        self.assertEqual(manifest["env_from_settings"], list(SECRET_KEYS))
        self.assertIn("windows", manifest["os"])
        self.assertEqual(validate({})["auth_mode"], "pat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
