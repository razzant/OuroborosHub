"""GitHub App installation credentials; no disk token cache or user fallback."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime
from urllib.parse import quote

from .http import HTTP, Effect, TransportError, classify, next_link, retry_at


class InstallationAuth:
    def __init__(self, config, private_key, *, test_loopback=False, clock=time.time, monotonic=time.monotonic):
        # Import only for App mode. PAT collection and plugin registration do
        # not depend on the optional signing libraries being installed.
        try:
            import jwt
            from cryptography.exceptions import UnsupportedAlgorithm
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
        except ImportError:
            raise TransportError("app_dependency_missing_install_PyJWT_crypto") from None
        try:
            key = serialization.load_pem_private_key(private_key.encode(), password=None)
            if not isinstance(key, RSAPrivateKey) or key.key_size < 2048:
                raise ValueError()
        except (ValueError, TypeError, AttributeError, UnsupportedAlgorithm):
            raise TransportError("app_private_key_invalid") from None
        self._jwt, self._key = jwt, key
        self.base = config["api_base_url"].rstrip("/")
        self.app_id, self.installation_id = config["app_id"], config["installation_id"]
        self.client_id = config["app_client_id"]
        self.writer_id = config["expected_writer_id"]
        self.repositories = sorted(r["id"] for r in config["repositories"])
        if not self.app_id or not self.installation_id or not 1 <= len(self.repositories) <= 500:
            raise TransportError("app_configuration_incomplete")
        self.options = {"graphql_url": config["graphql_url"], "api_version": config["api_version"],
                        "ca_bundle": config["ca_bundle"], "test_loopback": test_loopback}
        public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.cache_identity = hashlib.sha256(public + json.dumps(
            [self.base, self.options, self.app_id, self.installation_id, self.repositories], sort_keys=True).encode()).hexdigest()
        self.clock, self.monotonic = clock, monotonic
        self._lock = threading.RLock()
        self._token, self._deadline, self._expires = "", 0, 0
        self._retry_deadline, self._retry_at = 0, 0
        self._failure = "app_token_unavailable"
        self.slug, self.bot_login, self.expires_at, self.permissions = "", "", "", {}

    def _http(self, token):
        return HTTP(self.base, token, **self.options)

    def _json(self, http, method, path, data=None, expected=200):
        response = http.request(method, path, data)
        if response.status != expected or not isinstance(response.data, dict):
            # Token endpoint errors may reflect a JWT/key/token. Never attach
            # their body or headers to durable diagnostics or caller errors.
            rate = classify(response) == Effect.RATE_REFUSED
            raise TransportError("app_auth_rate_refused" if rate else "app_auth_http_" + str(response.status),
                                 retry_at=retry_at(response), effect=Effect.RATE_REFUSED if rate else Effect.UNKNOWN)
        return response

    def _refresh(self):
        issued = int(self.clock())
        signed = self._jwt.encode({"iat": issued - 60, "exp": issued + 540,
                                   "iss": self.client_id or str(self.app_id)}, self._key, algorithm="RS256")
        app_http = self._http(signed)
        app = self._json(app_http, "GET", "/app").data
        if (app.get("id") != self.app_id or not re.fullmatch(r"[A-Za-z0-9_-]+", str(app.get("slug", "")))
                or self.client_id and app.get("client_id") != self.client_id):
            raise TransportError("app_identity_mismatch")
        installation = self._json(app_http, "GET", "/app/installations/" + str(self.installation_id)).data
        if (installation.get("id") != self.installation_id or installation.get("app_id") != self.app_id
                or installation.get("app_slug") != app["slug"]):
            raise TransportError("app_installation_mismatch")
        if installation.get("suspended_at"):
            raise TransportError("app_installation_suspended")
        granted = installation.get("permissions") or {}
        if not isinstance(granted, dict):
            raise TransportError("app_installation_permissions_invalid")
        # Restrict both repository selection and permissions even if the App
        # registration grants additional access. Read-only grants stay read-only.
        permissions = {k: granted[k] for k in ("issues", "pull_requests", "discussions", "metadata")
                       if granted.get(k) in ("read", "write")}
        permissions["metadata"] = "read"
        minted = self._json(app_http, "POST", "/app/installations/" + str(self.installation_id) + "/access_tokens",
                            {"repository_ids": self.repositories, "permissions": permissions}, expected=201).data
        token = minted.get("token")
        try:
            stamp = datetime.fromisoformat(minted["expires_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError()
            expires = stamp.timestamp()
            if (not isinstance(token, str) or not token or not token.isascii()
                    or any(ord(c) <= 32 or ord(c) == 127 for c in token)
                    or not self.clock() + 60 < expires <= self.clock() + 3660):
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise TransportError("app_token_response_invalid") from None
        actual_permissions = minted.get("permissions")
        if not isinstance(actual_permissions, dict) or {"metadata": "read", **actual_permissions} != permissions:
            raise TransportError("app_token_permissions_mismatch")
        installation_http = self._http(token)
        seen, repository_ids = set(), set()
        path = "/installation/repositories?per_page=100"
        while path:
            if path in seen or len(seen) >= 5:
                raise TransportError("app_repository_pagination_invalid")
            seen.add(path)
            response = self._json(installation_http, "GET", path)
            rows = response.data.get("repositories")
            if not isinstance(rows, list) or any(not isinstance(r, dict) or type(r.get("id")) is not int for r in rows):
                raise TransportError("app_repository_response_invalid")
            repository_ids.update(r["id"] for r in rows)
            path = next_link(response.headers)
        if repository_ids != set(self.repositories):
            raise TransportError("app_token_repositories_mismatch")
        # Resolve the documented App bot identity, never the installation
        # owner, App ID or a configured arbitrary username.
        bot = self._json(installation_http, "GET", "/users/" + quote(app["slug"] + "[bot]", safe="")).data
        self.check_bot(bot, app["slug"], self.writer_id)
        remaining = expires - self.clock() - 60
        if remaining <= 0:
            raise TransportError("app_token_expired_during_verification")
        self._token, self._expires = token, expires
        self._deadline = self.monotonic() + min(3540, remaining)
        self.slug, self.bot_login = app["slug"], bot["login"]
        self.expires_at, self.permissions = minted["expires_at"], permissions

    @staticmethod
    def check_bot(bot, slug, expected):
        if (type(bot.get("id")) is not int or bot["id"] != expected or bot.get("type") != "Bot"
                or bot.get("login", "").lower() != (slug + "[bot]").lower()):
            raise TransportError("app_bot_identity_mismatch")

    def token_for(self, http):
        if (http.base != self.base or http.graphql_url != self.options["graphql_url"]
                or http.api_version != self.options["api_version"] or http.host):
            raise ValueError("app_credential_endpoint_mismatch")
        with self._lock:
            if self._token and self.monotonic() < self._deadline and self.clock() < self._expires - 60:
                return self._token
            if self.monotonic() < self._retry_deadline:
                raise TransportError(self._failure, retry_at=self._retry_at)
            self._token = ""
            try:
                self._refresh()
            except (TransportError, ValueError, OSError) as exc:
                code = getattr(exc, "code", "unavailable")
                self._failure = code if code.startswith("app_") else "app_auth_" + code
                delay = max(60, getattr(exc, "retry_at", 0) - self.clock())
                self._retry_at, self._retry_deadline = self.clock() + delay, self.monotonic() + delay
                raise TransportError(self._failure, retry_at=self._retry_at,
                                     effect=getattr(exc, "effect", Effect.UNKNOWN)) from None
            return self._token

    def status(self):
        with self._lock:
            return {"state": "valid" if self._token else "invalid", "auth_mode": "app",
                    "id": self.writer_id, "login": self.bot_login, "app_id": self.app_id,
                    "installation_id": self.installation_id, "expires_at": self.expires_at,
                    "permissions": dict(self.permissions)}

    def invalidate(self, token):
        with self._lock:
            # A late 401 from an older generation must not evict its replacement.
            if token == self._token:
                self._token = ""
