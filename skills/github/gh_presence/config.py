"""Strict local configuration; never fall back to the owner's GitHub token."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

API = "https://api.github.com"
GRAPHQL = API + "/graphql"
API_VERSION = "2026-03-10"
LOGIN_PATTERN = r"[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?"
SECRET_KEYS = ("GITHUB_PRESENCE_WRITER_TOKEN", "GITHUB_PRESENCE_NOTIFICATIONS_TOKEN",
               "GITHUB_PRESENCE_APP_PRIVATE_KEY")
DEFAULT = {"schema_version": 1, "enabled": False, "auth_mode": "pat",
           "app_id": 0, "installation_id": 0, "app_client_id": "",
           "api_base_url": API, "graphql_url": GRAPHQL, "web_base_url": "https://github.com",
           "api_version": API_VERSION, "ca_bundle": "", "private_repository_ids": [], "expected_writer_id": 0,
           "notifications_user_id": 0, "writer_notifications": False,
           "watched_identities": [], "repositories": [], "poll_seconds": 120,
           "overlap_seconds": 120, "reconcile_seconds": 21600, "quota_reserve": 100, "initial_since": ""}


def validate(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError("unknown configuration fields; secrets belong in Settings → Secrets")
    config = {**DEFAULT, **value}
    if config["schema_version"] != 1:
        raise ValueError("unsupported settings schema")
    if config["auth_mode"] not in ("pat", "app"):
        raise ValueError("auth_mode_must_be_pat_or_app")
    for key in ("app_id", "installation_id"):
        if type(config[key]) is not int or config[key] < 0:
            raise ValueError(key + " must be a numeric GitHub ID")
    if not isinstance(config["app_client_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]*", config["app_client_id"]):
        raise ValueError("invalid_app_client_id")
    if config["auth_mode"] == "app":
        if not config["app_id"] or not config["installation_id"]:
            raise ValueError("app_requires_app_id_and_installation_id")
        if config["writer_notifications"]:
            raise ValueError("app_installation_has_no_user_notifications_use_notifications_PAT")
    from .http import origin
    for key in ("api_base_url", "graphql_url", "web_base_url"):
        url = config[key]
        if not isinstance(url, str) or origin(url)[0] != "https":
            raise ValueError("provider_endpoints_require_https")
        parsed = urlsplit(url)
        if parsed.query or any(p in (".", "..") for p in parsed.path.split("/")):
            raise ValueError("invalid_provider_endpoint")
        config[key] = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))
    if config["api_base_url"] != API and (not all(k in value for k in ("graphql_url", "web_base_url", "api_version"))
                                          or origin(config["graphql_url"]) != origin(config["api_base_url"])):
        raise ValueError("GHES_requires_explicit_same_origin_endpoints_and_api_version")
    if origin(config["graphql_url"]) != origin(config["api_base_url"]):
        raise ValueError("graphql_origin_mismatch")
    if not isinstance(config["api_version"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", config["api_version"]):
        raise ValueError("invalid_api_version")
    if not isinstance(config["ca_bundle"], str):
        raise ValueError("ca_bundle_must_be_a_path")
    for key in ("enabled", "writer_notifications"):
        if type(config[key]) is not bool:
            raise ValueError(key + " must be boolean")
    for key in ("expected_writer_id", "notifications_user_id"):
        if type(config[key]) is not int or config[key] < 0:
            raise ValueError(key + " must be a numeric GitHub user ID")
    if config["notifications_user_id"] and config["notifications_user_id"] == config["expected_writer_id"]:
        raise ValueError("the second Notifications identity must differ from the writer")
    if type(config["poll_seconds"]) is not int or config["poll_seconds"] < 60:
        raise ValueError("poll_seconds must be an integer >= 60")
    for key, minimum in (("overlap_seconds", 60), ("reconcile_seconds", 600), ("quota_reserve", 0)):
        if type(config[key]) is not int or config[key] < minimum:
            raise ValueError(key + " must be an integer >= " + str(minimum))
    if not isinstance(config["initial_since"], str):
        raise ValueError("initial_since must be an ISO timestamp or empty")
    if config["initial_since"]:
        try:
            stamp = datetime.fromisoformat(config["initial_since"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError()
        except ValueError:
            raise ValueError("initial_since requires a timezone") from None
    if not isinstance(config["watched_identities"], list) or not isinstance(config["repositories"], list):
        raise ValueError("identities and repositories must be lists")
    seen = set()
    for user in config["watched_identities"]:
        if (not isinstance(user, dict) or set(user) != {"id", "login"}
                or type(user["id"]) is not int or user["id"] <= 0
                or not re.fullmatch(LOGIN_PATTERN, str(user["login"]))):
            raise ValueError("watched identities require numeric id and GitHub login")
        if user["id"] in seen:
            raise ValueError("duplicate watched identity")
        seen.add(user["id"])
    seen = set()
    for repo in config["repositories"]:
        if (not isinstance(repo, dict) or set(repo) != {"id", "full_name"}
                or type(repo["id"]) is not int or repo["id"] <= 0
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(repo["full_name"]))):
            raise ValueError("repositories require numeric id and owner/repo full_name")
        if repo["id"] in seen:
            raise ValueError("duplicate repository ID")
        seen.add(repo["id"])
    private = config["private_repository_ids"]
    if not isinstance(private, list) or any(type(x) is not int or x not in seen for x in private) or len(set(private)) != len(private):
        raise ValueError("private_repository_ids must explicitly name selected numeric repository IDs")
    if config["auth_mode"] == "app" and not 1 <= len(config["repositories"]) <= 500:
        raise ValueError("app_requires_1_to_500_explicit_repositories")
    if config["enabled"] and (not config["expected_writer_id"] or not config["repositories"]):
        raise ValueError("enable only after selecting a writer and repositories")
    return config


def read_config(state_dir):
    path = Path(state_dir) / "settings.json"
    if not path.exists():
        return validate({})
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("settings_unreadable: " + str(exc)) from None


def write_config(state_dir, value):
    config = validate(value)
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix="settings-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, root / "settings.json")
    finally:
        if os.path.exists(filename):
            os.unlink(filename)
    return config


def account_id(user_id, api_base_url=API):
    return "gh1." + api_base_url.rstrip("/").encode().hex() + ".user." + str(user_id)


def connection_account(config):
    """Installation custody differs from the numeric bot author of its effects.

    Keep the existing PAT namespace byte-for-byte for retained custody.
    """
    if config["auth_mode"] == "pat":
        return account_id(config["expected_writer_id"], config["api_base_url"])
    from .http import origin
    scheme, host, port = origin(config["api_base_url"])
    host = "[" + host + "]" if ":" in host else host.encode("idna").decode("ascii")
    server = scheme + "://" + host + (":" + str(port) if port != 443 else "")
    return "gh1." + server.encode().hex() + ".app." + str(config["app_id"]) + ".installation." + str(config["installation_id"])


def writer_http(config, credentials, *, readonly=False):
    if config["auth_mode"] == "app":
        from .auth import InstallationAuth
        token = InstallationAuth(config, credentials.get(SECRET_KEYS[2], ""))
    else:
        token = credentials.get(SECRET_KEYS[0], "")
    return provider_http(config, token, readonly=readonly)


def provider_http(config, token, *, readonly=False):
    from .http import HTTP
    return HTTP(config["api_base_url"], token, readonly=readonly, graphql_url=config["graphql_url"],
                api_version=config["api_version"], ca_bundle=config["ca_bundle"])
