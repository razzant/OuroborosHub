"""Small JSON transport with credential-origin confinement and no implicit retries."""
from __future__ import annotations

import ipaddress
import hashlib
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import Enum


@dataclass
class Response:
    status: int
    data: object
    headers: dict


class Effect(Enum):
    UNKNOWN = "possibly_applied"
    RATE_REFUSED = "rate_refused_unsent"
    REJECTED = "rejected_unsent"


def classify(response):
    """One provider classification, shared by reads, comments and closures.

    A readback's classification NEVER describes the preceding mutation. GraphQL
    partial mutation data and server failures remain possibly applied.
    """
    if response is None:
        return Effect.UNKNOWN
    data = response.data if isinstance(response.data, dict) else {}
    rate = (response.status in (403, 429) and (
        response.status == 429 or "retry-after" in response.headers
        or response.headers.get("x-ratelimit-remaining") == "0"
        or "rate limit" in str(data.get("message", "")).lower()))
    errors = data.get("errors")
    result = data.get("data")
    no_data = result is None or isinstance(result, dict) and not any(result.values())
    if response.status == 200 and isinstance(errors, list) and errors and no_data:
        rate = all(isinstance(e, dict) and e.get("type") == "RATE_LIMITED" for e in errors)
    if rate:
        return Effect.RATE_REFUSED
    if response.status in (400, 401, 403, 404, 405, 410, 422):
        return Effect.REJECTED
    return Effect.UNKNOWN


class TransportError(Exception):
    def __init__(self, code, response=None, retry_at=0, *, effect=None):
        super().__init__(code)
        self.code, self.response, self.retry_at = code, response, retry_at
        self.effect = effect or (Effect.RATE_REFUSED if code in ("provider_backoff", "provider_quota_reserve")
                                 else classify(response))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def origin(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
        raise ValueError("invalid_endpoint")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


class HTTP:
    def __init__(self, base, token, *, host=False, readonly=False, test_loopback=False, timeout=30,
                 graphql_url=None, api_version="2026-03-10", ca_bundle=""):
        self.base = base.rstrip("/")
        self.origin = origin(base)
        self.host, self.readonly, self.timeout = host, readonly, timeout
        self.graphql_url = graphql_url or self.base + "/graphql"
        if origin(self.graphql_url) != self.origin:
            raise ValueError("graphql_origin_mismatch")
        self.api_version = api_version
        if host or test_loopback:
            try:
                assert ipaddress.ip_address(self.origin[1]).is_loopback
            except (ValueError, AssertionError):
                raise ValueError("host_requires_literal_loopback") from None
            if self.origin[0] not in ("http", "https"):
                raise ValueError("invalid_loopback_scheme")
        elif self.origin[0] != "https":
            raise ValueError("provider_requires_tls")
        if not token:
            raise ValueError("credential_missing")
        self.auth = token if not isinstance(token, str) else None
        if self.auth and host:
            raise ValueError("host_requires_static_credential")
        self._token = token if self.auth is None else None
        identity = self.auth.cache_identity if self.auth else token
        self.cache_identity = hashlib.sha256((self.base + "\0" + api_version + "\0" + identity).encode()).hexdigest()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                  urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_bundle or None)))

    def url(self, path):
        if path == "/graphql":
            path = self.graphql_url
        url = path if path.startswith(("https://", "http://")) else self.base + "/" + path.lstrip("/")
        if origin(url) != self.origin or (url != self.graphql_url and not urllib.parse.urlsplit(url).path.startswith(
                urllib.parse.urlsplit(self.base).path.rstrip("/") + "/")):
            raise ValueError("cross_origin_or_base_path_refused")
        if any(p in (".", "..") for p in urllib.parse.unquote(urllib.parse.urlsplit(url).path).split("/")):
            raise ValueError("unsafe_path")
        return url

    def request(self, method, path, data=None, headers=None):
        if self.readonly and method != "GET":
            raise ValueError("notifications_credential_is_get_only")
        url = self.url(path)
        if any(key.lower() in ("authorization", "x-skill-token", "host") for key in (headers or {})):
            raise ValueError("credential_header_override_refused")
        token = self.auth.token_for(self) if self.auth else self._token
        request_headers = {"Accept": "application/vnd.github+json", "User-Agent": "ouroboros-github/0.1.3"}
        if self.host:
            request_headers["X-Skill-Token"] = token
        else:
            request_headers["Authorization"] = "Bearer " + token
            request_headers["X-GitHub-Api-Version"] = self.api_version
        request_headers.update(headers or {})
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            try:
                stream = self.opener.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                stream = exc
            with stream:
                raw = stream.read()
                response_headers = {k.lower(): v for k, v in stream.headers.items()}
                try:
                    result = json.loads(raw.decode()) if raw else None
                except (ValueError, UnicodeError):
                    result = None  # never expose an arbitrary error page or reflected secret
                if stream.code == 401 and self.auth:
                    # Invalidate for the NEXT operation. Even a rejected POST is
                    # returned once, never replayed automatically with a new token.
                    self.auth.invalidate(token)
                return Response(stream.code, result, response_headers)
        except (OSError, urllib.error.URLError, TimeoutError):
            raise TransportError("network_outcome_unknown" if method != "GET" else "read_unavailable") from None


def retry_at(response):
    header = response.headers.get("retry-after", "")
    try:
        return time.time() + max(1, float(header))
    except ValueError:
        if header:
            try:
                return parsedate_to_datetime(header).timestamp()
            except (ValueError, TypeError):
                pass
    if response.headers.get("x-ratelimit-remaining") == "0":
        try:
            return max(time.time() + 1, float(response.headers["x-ratelimit-reset"]))
        except (KeyError, ValueError):
            pass
    return time.time() + 60


def next_link(headers):
    for link in headers.get("link", "").split(","):
        match = re.match(r'\s*<([^>]+)>;\s*rel="next"', link)
        if match:
            return match[1]
    return None
