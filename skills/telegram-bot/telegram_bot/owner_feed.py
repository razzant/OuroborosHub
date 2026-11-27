"""Owner-bot group feed client and relay TelegramTransport for wire v2.

All Telegram Bot API formatting, chunking, media assembly, album encoding, retry
rules and download verification remain in ``TelegramClient`` (``api.py``).  This
module provides only:

1. ``OwnerFeedClient`` — authenticated Host Service reads for
   ``GET /telegram/feed/identity`` and ``GET /telegram/feed/updates``.
2. ``RelayTelegramTransport`` — the narrow ``TelegramTransport`` implementation
   mapping ``TelegramClient`` calls onto ``POST /telegram/feed/call``,
   ``POST /telegram/feed/upload`` and ``GET /telegram/feed/file``, carrying the
   exact immutable place sideband (``account_id``, ``place_id``,
   ``connected_revision``, ``thread_id``), with no owner bot token, no generic
   URL proxy, no provider polling and strict outcome classification (uncertain
   outcomes are never auto-resent).
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import pathlib
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .api import (
    TelegramApiError,
    TelegramFileTooLarge,
    TelegramRequestNotSent,
    _bot_api_refusal,
    _declared_length,
)
from .host import (
    HOLD_BINDING_CHANGED,
    PresenceHostError,
    PresenceHostHTTPError,
    PresenceHostResponseTooLarge,
    PresenceHostTransport,
    PresenceHostUnreachable,
    UrllibPresenceHostTransport,
    _open_host,
    _whole,
)


SOURCE_STANDALONE = "standalone"
SOURCE_OWNER_BOT = "owner_bot"
OWNER_BOT_SUBDIR = "owner_bot"
OWNER_CONTRIBUTION_ID = "owner_bot"
FEED_CAPABILITY_KEY = "telegram_group_feed"
POLL_CLAIM_CAPABILITY_KEY = "telegram_group_poll_claim"
HOLD_ACCOUNT_MISMATCH = "account_mismatch"
HOLD_RELAY_BUSY = "busy"
HOLD_AUTH_UNAVAILABLE = "auth_unavailable"
PLACE_BINDING_VERSION = 1

RELAY_API_SERVER = "http://relay.invalid"
_BOT_ID_RE = re.compile(r"^[1-9][0-9]*$")
_GROUP_CHAT_ID_RE = re.compile(r"^-[0-9]+$")
_THREAD_ID_RE = re.compile(r"^[1-9][0-9]*$")
_RELAY_METHOD_RE = re.compile(r"^http://relay\.invalid/bot[^/]+/([A-Za-z]+)$")
_RELAY_FILE_RE = re.compile(r"^http://relay\.invalid/file/bot[^/]+/(.+)$")

_RELAY_JSON_METHODS = frozenset({
    "sendMessage",
    "sendChatAction",
    "editMessageText",
    "setMessageReaction",
    "deleteMessage",
    "restrictChatMember",
    "banChatMember",
    "unbanChatMember",
    "getFile",
})
_RELAY_UPLOAD_METHODS = frozenset({
    "sendPhoto",
    "sendDocument",
    "sendVideo",
    "sendAudio",
    "sendVoice",
    "sendAnimation",
    "sendMediaGroup",
})
_UPLOAD_METHOD_FIELD = {
    "sendPhoto": "photo",
    "sendDocument": "document",
    "sendVideo": "video",
    "sendAudio": "audio",
    "sendVoice": "voice",
    "sendAnimation": "animation",
}
_NO_CONNECTION = (socket.gaierror, ConnectionRefusedError)
_NO_ANSWER = (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException)
_HASH_CHUNK = 1024 * 1024
# The relay has to wait for the native provider request before it can answer. Native calls getFile with 35 s,
# buffers the whole download (provider read timeout is 30 s per phase), and uses 120 s for uploads. These are
# finite outer socket waits with headroom, not claims about the provider's total wall time or delivery outcome.
_RELAY_GET_FILE_TIMEOUT = 50.0
_RELAY_DOWNLOAD_TIMEOUT = 90.0
_RELAY_UPLOAD_TIMEOUT = 150.0


def canonical_bot_id(value: Any) -> str:
    """Return exact positive numeric bot ID string, or ``""``."""
    if isinstance(value, bool) or value is None:
        return ""
    text = str(value).strip()
    return text if _BOT_ID_RE.fullmatch(text) else ""


def token_bot_id_prefix(token: str) -> str:
    """Bot ID prefix of a Telegram bot token (``<id>:<secret>``), else ``""``."""
    raw = str(token or "").strip()
    if ":" not in raw:
        return ""
    return canonical_bot_id(raw.split(":", 1)[0])


def owner_state_dir(root_state_dir: Any) -> pathlib.Path:
    """Isolated state directory for the owner-bot group feed profile."""
    return pathlib.Path(root_state_dir) / OWNER_BOT_SUBDIR


def owner_store_path(root_state_dir: Any) -> pathlib.Path:
    return owner_state_dir(root_state_dir) / "custody.sqlite3"


def owner_store_exists(root_state_dir: Any) -> bool:
    return owner_store_path(root_state_dir).is_file()


def normalize_place_binding(
    binding: Any,
    *,
    account_id: str = "",
    conversation_id: str = "",
    thread_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Validate and normalize a wire-v2 ``owner_bot`` place binding object, or return ``None``."""
    if not isinstance(binding, dict):
        return None
    if type(binding.get("version")) is not int or binding["version"] != PLACE_BINDING_VERSION:
        return None
    if binding.get("connector") != "telegram-bot" or binding.get("provider") != "telegram":
        return None
    acct = canonical_bot_id(binding.get("account_id"))
    if not acct:
        return None
    if account_id and acct != canonical_bot_id(account_id):
        return None
    conv = str(binding.get("conversation_id") or "").strip()
    if not _GROUP_CHAT_ID_RE.fullmatch(conv):
        return None
    if conversation_id and conv != str(conversation_id).strip():
        return None
    raw_thread = binding.get("thread_id", "")
    if not isinstance(raw_thread, str):
        return None
    th = raw_thread.strip()
    if th and not _THREAD_ID_RE.fullmatch(th):
        return None
    if thread_id is not None and th != str(thread_id).strip():
        return None
    place_id = binding.get("place_id")
    if not isinstance(place_id, str) or not place_id.strip() or place_id != place_id.strip():
        return None
    rev = binding.get("connected_revision")
    if type(rev) is not int or rev < 0:
        return None
    return {
        "version": PLACE_BINDING_VERSION,
        "connector": "telegram-bot",
        "provider": "telegram",
        "account_id": acct,
        "conversation_id": conv,
        "thread_id": th,
        "place_id": place_id,
        "connected_revision": rev,
    }


def feed_item_place_binding(
    place: Any,
    *,
    account_id: str,
    conversation_id: str,
    thread_id: Optional[str] = "",
) -> Optional[Dict[str, Any]]:
    """Bind a wire-v2 feed item. ``None`` means Telegram did not provide a topic field (a reaction)."""
    if not isinstance(place, dict):
        return None
    place_id = place.get("place_id")
    rev = place.get("connected_revision")
    raw_thread = place.get("thread_id", "")
    if not isinstance(place_id, str) or not place_id.strip() or place_id != place_id.strip():
        return None
    if type(rev) is not int or rev < 0:
        return None
    if not isinstance(raw_thread, str):
        return None
    place_thread = raw_thread.strip()
    if place_thread and not _THREAD_ID_RE.fullmatch(place_thread):
        return None
    event_thread = place_thread if thread_id is None else str(thread_id).strip()
    if place_thread and event_thread != place_thread:
        return None
    effective_thread = event_thread or place_thread
    return normalize_place_binding(
        {
            "version": PLACE_BINDING_VERSION,
            "connector": "telegram-bot",
            "provider": "telegram",
            "account_id": account_id,
            "conversation_id": conversation_id,
            "thread_id": effective_thread,
            "place_id": place_id,
            "connected_revision": rev,
        },
        account_id=account_id,
        conversation_id=conversation_id,
        thread_id=effective_thread,
    )


class OwnerFeedError(RuntimeError):
    """The Host owner-bot feed intake or identity check failed."""


class RelayRefusalError(TelegramRequestNotSent):
    """Host/native relay refused before calling Telegram; nothing was sent.

    Subclasses ``TelegramRequestNotSent`` so outbox send markers are cleared and
    never misclassified as ``uncertain``, while carrying the typed
    ``relay_refusal`` so retries, holds and reactions distinguish relay refusals
    from permanent Telegram 403 refusals.
    """

    def __init__(
        self,
        endpoint: str,
        description: str,
        *,
        relay_refusal: str,
        error_code: int = 403,
        retry_after: Optional[float] = None,
    ):
        super().__init__(endpoint, description, error_code=error_code, retry_after=retry_after)
        self.relay_refusal = str(relay_refusal or "").strip()


@dataclass(frozen=True)
class PollClaimResult:
    """Result of ``POST /telegram/feed/poller`` for standalone bot B poll ownership."""

    status: str  # "owned" | "conflict" | "account_changed" | "unavailable"
    holder: str = ""
    code: str = ""
    error: str = ""


@dataclass(frozen=True)
class OwnerFeedIdentity:
    enabled: bool
    account_id: str = ""
    username: str = ""
    label: str = ""
    identity: Optional[Dict[str, Any]] = None
    owner_bound: bool = False
    reason: str = ""
    same_as_owner_bridge: Optional[bool] = None
    invite_url: str = ""
    connector_token_held: Optional[bool] = None
    group_ids: Optional[Tuple[str, ...]] = None

    @property
    def confirmed(self) -> bool:
        return bool(self.enabled and not self.reason and self.account_id and self.identity is not None)


def _retry_after(payload: Dict[str, Any]) -> Optional[float]:
    params = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    wait = params.get("retry_after")
    return float(wait) if type(wait) in (int, float) and wait >= 0 else None


def _parse_identity(payload: Any) -> OwnerFeedIdentity:
    """Parse wire-v2 Host identity, with optional connector-token status and targeted group membership."""
    if not isinstance(payload, dict):
        raise OwnerFeedError("owner feed identity returned a non-object response")
    enabled = payload.get("enabled") is True
    reason = str(payload.get("reason") or "").strip()
    owner_bound = payload.get("owner_bound") is True
    raw_same = payload.get("same_as_owner_bridge")
    same = raw_same if type(raw_same) is bool else None
    held = payload.get("connector_token_held")
    invite_url = str(payload.get("invite_url") or "").strip()

    top_account_id = canonical_bot_id(payload.get("account_id"))
    verified_identity: Optional[Dict[str, Any]] = None
    verified_id, username, label = "", "", ""
    raw_identity = payload.get("identity")
    if isinstance(raw_identity, dict):
        sends_as = raw_identity.get("sends_as")
        if isinstance(sends_as, dict) and sends_as.get("kind") == "bot":
            parsed_id = canonical_bot_id(sends_as.get("id"))
            raw_label = str(sends_as.get("label") or "").strip()
            if parsed_id and raw_label and (not top_account_id or parsed_id == top_account_id):
                verified_id = parsed_id
                label = raw_label
                username = raw_label[1:] if raw_label.startswith("@") else ""
                verified_identity = {
                    "sends_as": {"id": parsed_id, "label": raw_label, "kind": "bot"},
                }

    account_id = verified_id or top_account_id
    if enabled and (not verified_id or verified_identity is None) and not reason:
        reason = "identity_unconfirmed"
    groups = payload.get("group_ids")
    group_ids = tuple(dict.fromkeys(groups)) if (
        enabled and not reason and verified_identity is not None and isinstance(groups, list)
        and all(isinstance(group, str) and re.fullmatch(r"-[1-9][0-9]*", group) for group in groups)
    ) else None
    return OwnerFeedIdentity(
        enabled=enabled,
        account_id=account_id,
        username=username,
        label=label,
        identity=verified_identity,
        owner_bound=owner_bound,
        reason=reason,
        same_as_owner_bridge=same,
        invite_url=invite_url,
        connector_token_held=held if type(held) is bool else None,
        group_ids=group_ids,
    )


def _parse_relay_error(endpoint: str, status: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Classify a non-2xx relay HTTP response without guessing provider outcome."""
    refusal = payload.get("relay_refusal")
    if isinstance(refusal, str) and refusal.strip():
        desc = str(payload.get("description") or payload.get("error") or refusal).strip()
        raise RelayRefusalError(
            endpoint,
            desc,
            relay_refusal=refusal.strip(),
            error_code=int(payload.get("error_code") or status),
            retry_after=_retry_after(payload),
        )
    outcome = payload.get("relay_outcome")
    if status == 503 and outcome == "not_sent":
        desc = str(payload.get("description") or payload.get("error") or "relay did not send request").strip()
        raise TelegramRequestNotSent(endpoint, desc)
    if outcome == "unknown" or status >= 500:
        desc = str(payload.get("description") or payload.get("error") or f"HTTP {status} relay outcome unknown").strip()
        raise TelegramApiError(endpoint, desc, error_code=0)
    if 400 <= status < 500 and _bot_api_refusal(payload, status):
        return payload
    raise TelegramApiError(endpoint, f"HTTP {status} without a Bot API answer", error_code=0)


class RelayDownloadTimeout(TelegramApiError):
    """This file's relay read timed out; neither the cause nor a Telegram refusal is known."""

    def __init__(self):
        super().__init__("download", "timed out waiting for file bytes (cause unknown)")


def _relay_get_bytes(url: str, headers: Dict[str, str], timeout_sec: float, max_bytes: int) -> bytes:
    """Bounded GET of raw bytes from ``/telegram/feed/file``, from that address only (no redirect is followed)."""
    request = urllib.request.Request(url, method="GET", headers=dict(headers))
    try:
        with _open_host(request, timeout_sec) as response:  # noqa: S310 - fixed loopback URL
            data = response.read(max_bytes + 1)
            declared = _declared_length(response)
            if declared is not None and len(data) < min(declared, max_bytes + 1):
                raise http.client.IncompleteRead(data, declared - len(data))
    except urllib.error.HTTPError as exc:
        try:
            # A redirect's body, or an error body cut short, answers nothing about the file.
            with exc:
                err_data = exc.read(64 * 1024) if exc.code >= 400 else b""
            parsed = json.loads(err_data.decode("utf-8", errors="replace")) if _whole(exc, err_data) else {}
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            refusal = parsed.get("relay_refusal")
            if isinstance(refusal, str) and refusal.strip():
                raise RelayRefusalError(
                    "download",
                    str(parsed.get("description") or refusal),
                    relay_refusal=refusal.strip(),
                    error_code=int(parsed.get("error_code") or exc.code),
                    retry_after=_retry_after(parsed),
                ) from exc
            if exc.code == 503 and parsed.get("relay_outcome") == "not_sent":
                raise TelegramRequestNotSent("download", str(parsed.get("description") or "relay did not send")) from exc
            if exc.code == 504 and parsed.get("relay_outcome") == "file_read_timeout":
                # The native bridge observed a Telegram read timeout after successful metadata lookup.
                raise RelayDownloadTimeout() from exc
            if parsed.get("relay_outcome") == "unknown":
                raise TelegramApiError("download", str(parsed.get("description") or f"HTTP {exc.code}"), error_code=0) from exc
            desc = str(parsed.get("description") or "")
            if exc.code == 400 and "too big" in desc.lower():
                raise TelegramFileTooLarge("download", desc) from exc
        # A redirect (not followed) is no answer about the file.
        # An untyped gateway timeout is not evidence of a Telegram reply or of a timeout reading Telegram bytes.
        code = exc.code if exc.code >= 400 and exc.code != 504 else 0
        raise TelegramApiError("download", f"HTTP {exc.code}", code) from exc
    except TimeoutError as exc:
        # urllib wraps connection and request-write failures in URLError. A bare timeout here
        # is a wait for relay response headers or bytes, for the path from a successful getFile.
        raise RelayDownloadTimeout() from exc
    except _NO_ANSWER as exc:
        raise TelegramApiError("download", type(exc).__name__, error_code=0) from exc
    if len(data) > max_bytes:
        raise TelegramFileTooLarge("download", f"file exceeds {max_bytes} bytes")
    return data


def _confined_file_meta(field: str, file_path: pathlib.Path, allowed_root: pathlib.Path) -> Dict[str, Any]:
    """Verify ``file_path`` resolves inside ``allowed_root`` and hash its bytes from one open handle."""
    root_real = os.path.realpath(allowed_root)
    try:
        resolved = pathlib.Path(os.path.realpath(file_path))
    except OSError as exc:
        raise TelegramRequestNotSent("upload", type(exc).__name__) from exc
    try:
        rel = os.path.relpath(str(resolved), root_real)
    except ValueError as exc:
        raise TelegramRequestNotSent("upload", "file path escapes skill state directory") from exc
    if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        raise TelegramRequestNotSent("upload", "file path escapes skill state directory")
    digest, size = hashlib.sha256(), 0
    try:
        with open(resolved, "rb") as handle:
            while chunk := handle.read(_HASH_CHUNK):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise TelegramRequestNotSent("upload", type(exc).__name__) from exc
    return {
        "field": str(field),
        "path": str(resolved),
        "name": resolved.name,
        "size": size,
        "sha256": digest.hexdigest(),
    }


def _upload_params(fields: Dict[str, str], method: str, file_fields: Sequence[str]) -> Dict[str, Any]:
    """Convert ``TelegramClient`` multipart string fields into strict wire-v2 JSON params."""
    params: Dict[str, Any] = {}
    allowed_keys = (
        {"chat_id", "message_thread_id", "reply_parameters", "media"}
        if method == "sendMediaGroup"
        else {"chat_id", "caption", "parse_mode", "message_thread_id", "reply_parameters"}
    )
    for key, value in fields.items():
        if key not in allowed_keys:
            raise TelegramRequestNotSent(method, f"argument_not_allowed: {key}")
        if key == "reply_parameters":
            try:
                parsed = json.loads(value) if isinstance(value, str) else value
            except ValueError as exc:
                raise TelegramRequestNotSent(method, "invalid reply_parameters") from exc
            if (
                not isinstance(parsed, dict)
                or set(parsed) != {"message_id"}
                or type(parsed.get("message_id")) is not int
                or parsed["message_id"] <= 0
            ):
                raise TelegramRequestNotSent(method, "reply_parameters may carry only positive message_id")
            params[key] = {"message_id": parsed["message_id"]}
            continue
        if key == "media":
            try:
                parsed_media = json.loads(value) if isinstance(value, str) else value
            except ValueError as exc:
                raise TelegramRequestNotSent(method, "invalid media") from exc
            if not isinstance(parsed_media, list) or not parsed_media:
                raise TelegramRequestNotSent(method, "media must be a non-empty list")
            known = set(file_fields)
            clean_media: List[Dict[str, Any]] = []
            for entry in parsed_media:
                if not isinstance(entry, dict) or not set(entry) <= {"type", "media", "caption", "parse_mode"}:
                    raise TelegramRequestNotSent(method, "invalid media entry")
                ref = str(entry.get("media") or "")
                if not ref.startswith("attach://") or ref[len("attach://"):] not in known:
                    raise TelegramRequestNotSent(method, "media must reference an attached file field")
                clean_media.append(dict(entry))
            params[key] = clean_media
            continue
        if key == "message_thread_id":
            if isinstance(value, str) and _THREAD_ID_RE.fullmatch(value.strip()):
                params[key] = int(value.strip())
                continue
            if type(value) is int and value > 0:
                params[key] = value
                continue
            raise TelegramRequestNotSent(method, "invalid message_thread_id")
        params[key] = value
    if method != "sendMediaGroup":
        expected_field = _UPLOAD_METHOD_FIELD.get(method, "")
        if list(file_fields) != [expected_field]:
            raise TelegramRequestNotSent(method, "upload file field mismatch")
    return params


class OwnerFeedClient:
    """Host Service client for ``/telegram/feed/identity`` and ``/telegram/feed/updates``."""

    def __init__(
        self,
        *,
        host_base: str,
        token_provider: Callable[[], str],
        transport: Optional[PresenceHostTransport] = None,
        transport_provider: Optional[Callable[[], Optional[PresenceHostTransport]]] = None,
        bytes_transport: Optional[Callable[[str, Dict[str, str], float, int], Any]] = None,
    ):
        self.host_base = str(host_base).rstrip("/")
        self.token_provider = token_provider
        self._transport = transport
        self._transport_provider = transport_provider
        self._bytes_transport = bytes_transport
        # One observation shared with the standalone runtime, from the ordinary background native identity read.
        # None (no current answer) is distinct from a successful older response that omitted the optional fact.
        self.connection_identity: Optional[OwnerFeedIdentity] = None

    def _headers(self) -> Dict[str, str]:
        token = str(self.token_provider() or "").strip()
        if not token:
            raise OwnerFeedError("presence skill token is unavailable")
        return {"X-Skill-Token": token}

    def _active_transport(self) -> Optional[PresenceHostTransport]:
        if self._transport is not None:
            return self._transport
        if self._transport_provider is not None:
            return self._transport_provider()
        return None

    async def _get_json(self, path_and_query: str, timeout_sec: float) -> Dict[str, Any]:
        url = f"{self.host_base}{path_and_query}"
        headers = self._headers()
        transport = self._active_transport() or UrllibPresenceHostTransport()
        return await transport.request_json("GET", url, headers=headers, payload=None, timeout_sec=timeout_sec)

    async def _post_json(self, path: str, payload: Dict[str, Any], timeout_sec: float) -> Dict[str, Any]:
        url = f"{self.host_base}{path}"
        headers = self._headers()
        transport = self._active_transport() or UrllibPresenceHostTransport()
        return await transport.request_json("POST", url, headers=headers, payload=payload, timeout_sec=timeout_sec)

    async def claim_poller(self, bot_id: str) -> PollClaimResult:
        """Claim standalone B poll ownership via ``POST /telegram/feed/poller``."""
        canonical = canonical_bot_id(bot_id)
        if not canonical:
            return PollClaimResult(
                status="unavailable",
                code="bot_id_invalid",
                error="bot_id must be a positive numeric id",
            )
        try:
            response = await self._post_json("/telegram/feed/poller", {"bot_id": canonical}, timeout_sec=10.0)
        except PresenceHostHTTPError as exc:
            payload = exc.payload if isinstance(exc.payload, dict) else {}
            code = str(payload.get("code") or payload.get("relay_refusal") or "").strip()
            if exc.status_code == 409 and payload.get("ok") is False and code == "account_changed":
                return PollClaimResult(status="account_changed", code="account_changed", error=str(exc))
            return PollClaimResult(
                status="unavailable",
                code=code or f"http_{exc.status_code}",
                error=str(exc),
            )
        except Exception as exc:
            return PollClaimResult(status="unavailable", code="host_unavailable", error=str(exc))
        if not isinstance(response, dict) or response.get("ok") is not True or type(response.get("owned")) is not bool:
            return PollClaimResult(
                status="unavailable",
                code="invalid_claim_response",
                error="poller claim returned an invalid response",
            )
        if response["owned"] is True:
            return PollClaimResult(status="owned")
        holder = str(response.get("holder") or "").strip()
        return PollClaimResult(status="conflict", holder=holder, code="poll_not_owned")

    async def identity(self, *, bot_id: str = "", groups: bool = False) -> OwnerFeedIdentity:
        params = {**({"bot_id": bot_id} if bot_id else {}), **({"groups": "1"} if groups else {})}
        query = "?" + urllib.parse.urlencode(params) if params else ""
        background = not params
        try:
            identity = _parse_identity(await self._get_json(f"/telegram/feed/identity{query}", timeout_sec=10.0))
        except PresenceHostHTTPError as exc:
            refusal = exc.payload.get("relay_refusal") if isinstance(exc.payload, dict) else None
            if exc.status_code == 403 and isinstance(refusal, str) and refusal.strip():
                identity = OwnerFeedIdentity(enabled=False, reason=refusal.strip(), same_as_owner_bridge=None)
            else:
                if background:
                    self.connection_identity = None
                raise OwnerFeedError(str(exc)) from exc
        except (OwnerFeedError, PresenceHostError, OSError, TimeoutError) as exc:
            if background:
                self.connection_identity = None
            raise OwnerFeedError(str(exc)) from exc
        if background:
            self.connection_identity = identity
        return identity

    async def updates(self, account_id: str, *, offset: int, timeout_sec: int = 25) -> List[Dict[str, Any]]:
        account = canonical_bot_id(account_id)
        if not account:
            raise OwnerFeedError("owner feed account_id must be a positive numeric id")
        bounded_timeout = max(0, min(25, int(timeout_sec)))
        query = {
            "account_id": account,
            "offset": max(0, int(offset)),
            "timeout": bounded_timeout,
        }
        try:
            limit = 100  # the Host default and maximum
            while True:
                try:
                    response = await self._get_json(
                        f"/telegram/feed/updates?{urllib.parse.urlencode(query)}",
                        timeout_sec=float(query["timeout"]) + 15.0,
                    )
                    break
                except PresenceHostResponseTooLarge as exc:
                    if limit == 1:
                        raise OwnerFeedError(
                            "A single owner-bot feed item exceeds the Host response read limit; "
                            "its offset was not advanced."
                        ) from exc
                    # Offset is a confirmation. Only the same, already-durable offset may be sent again;
                    # a complete page must be committed locally before any higher offset is requested.
                    limit = max(1, limit // 2)
                    query.update(limit=limit, timeout=0)
        except PresenceHostHTTPError as exc:
            refusal = exc.payload.get("relay_refusal") if isinstance(exc.payload, dict) else None
            if exc.status_code == 403 and isinstance(refusal, str) and refusal.strip():
                raise RelayRefusalError(
                    "feed_updates",
                    str(exc.payload.get("description") or refusal),
                    relay_refusal=refusal.strip(),
                    error_code=int(exc.payload.get("error_code") or exc.status_code),
                    retry_after=_retry_after(exc.payload),
                ) from exc
            raise OwnerFeedError(str(exc)) from exc
        except (PresenceHostError, OSError, TimeoutError) as exc:
            raise OwnerFeedError(str(exc)) from exc
        if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), list):
            raise OwnerFeedError("owner feed updates returned an invalid response")
        return [item for item in response["result"] if isinstance(item, dict)]

    def make_relay_transport(
        self,
        account_id: str,
        allowed_files_root: pathlib.Path,
        *,
        binding: Optional[Dict[str, Any]] = None,
    ) -> "RelayTelegramTransport":
        return RelayTelegramTransport(
            account_id=account_id,
            host_base=self.host_base,
            token_provider=self.token_provider,
            allowed_files_root=allowed_files_root,
            binding=binding,
            host_transport=self._active_transport(),
            bytes_transport=self._bytes_transport,
        )


class RelayTelegramTransport:
    """Narrow ``TelegramTransport`` forwarding ``TelegramClient`` calls to Host relay.

    Every relay call requires an immutable place binding (via ``binding`` or
    ``with_binding(...)``) so ``account_id``, ``place_id``, ``connected_revision``
    and ``thread_id`` are sent as exact sideband fields.

    Intentionally does NOT accept ``progress`` on ``post_multipart[_files]``:
    Hub only posts JSON metadata to the local Host relay and never streams file
    bytes to Telegram itself, so provider upload byte progress is never simulated.
    """

    def __init__(
        self,
        *,
        account_id: str,
        host_base: str,
        token_provider: Callable[[], str],
        allowed_files_root: pathlib.Path,
        binding: Optional[Dict[str, Any]] = None,
        host_transport: Optional[PresenceHostTransport] = None,
        bytes_transport: Optional[Callable[[str, Dict[str, str], float, int], Any]] = None,
    ):
        account = canonical_bot_id(account_id)
        if not account:
            raise ValueError("relay account_id must be a positive numeric bot id")
        normalized: Optional[Dict[str, Any]] = None
        if binding is not None:
            normalized = normalize_place_binding(binding, account_id=account)
            if normalized is None:
                raise ValueError("relay place binding is invalid for account_id")
        self.account_id = account
        self.host_base = str(host_base).rstrip("/")
        self.token_provider = token_provider
        self.allowed_files_root = pathlib.Path(allowed_files_root)
        self.binding = normalized
        self.host_transport = host_transport
        self.bytes_transport = bytes_transport

    def with_binding(self, binding: Dict[str, Any]) -> "RelayTelegramTransport":
        """Return a shallow bound transport carrying the exact immutable place sideband."""
        return RelayTelegramTransport(
            account_id=self.account_id,
            host_base=self.host_base,
            token_provider=self.token_provider,
            allowed_files_root=self.allowed_files_root,
            binding=binding,
            host_transport=self.host_transport,
            bytes_transport=self.bytes_transport,
        )

    def _sideband(self, endpoint: str) -> Dict[str, Any]:
        if self.binding is None:
            raise TelegramRequestNotSent(endpoint, "place_binding_required")
        return {
            "account_id": self.account_id,
            "place_id": self.binding["place_id"],
            "connected_revision": self.binding["connected_revision"],
            "thread_id": self.binding["thread_id"],
        }

    def _check_target_matches_binding(self, method: str, params: Dict[str, Any]) -> None:
        assert self.binding is not None
        if str(params.get("chat_id") or "").strip() != self.binding["conversation_id"]:
            raise TelegramRequestNotSent(method, "binding_conversation_mismatch")
        if method in ("sendMessage", "sendChatAction") or method in _RELAY_UPLOAD_METHODS:
            raw_thread = params.get("message_thread_id")
            call_thread = str(raw_thread) if raw_thread not in (None, "") else ""
            if call_thread != self.binding["thread_id"]:
                raise TelegramRequestNotSent(method, "binding_thread_mismatch")

    def _headers(self) -> Dict[str, str]:
        token = str(self.token_provider() or "").strip()
        if not token:
            raise TelegramRequestNotSent("relay", "presence skill token is unavailable")
        return {"X-Skill-Token": token}

    async def _post_relay(self, route: str, body: Dict[str, Any], timeout_sec: float, endpoint: str) -> Dict[str, Any]:
        """One relay POST through the Host transport every other Host call of this skill uses.

        A typed refusal or Telegram's own answer is read by ``_parse_relay_error``. A connection to the Host that never
        existed sent nothing (``TelegramRequestNotSent``: the call may be made again). Any failure once it existed, a
        lost or unreadable answer included, leaves the provider outcome unknown (a ``TelegramApiError`` without a code).
        """
        headers = self._headers()
        transport = self.host_transport or UrllibPresenceHostTransport()
        try:
            response = await transport.request_json(
                "POST", f"{self.host_base}{route}", headers=headers, payload=body, timeout_sec=timeout_sec,
            )
        except PresenceHostHTTPError as exc:
            return _parse_relay_error(endpoint, exc.status_code, exc.payload if isinstance(exc.payload, dict) else {})
        except PresenceHostUnreachable as exc:
            raise TelegramRequestNotSent(endpoint, str(exc)) from exc
        except _NO_CONNECTION as exc:
            raise TelegramRequestNotSent(endpoint, type(exc).__name__) from exc
        except TelegramApiError:
            raise
        except Exception as exc:
            raise TelegramApiError(endpoint, type(exc).__name__, error_code=0) from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise TelegramApiError(endpoint, "relay success status without an ok Bot API answer", error_code=0)
        return response

    async def post_json(self, url: str, payload: Dict[str, Any], timeout_sec: float) -> Dict[str, Any]:
        match = _RELAY_METHOD_RE.fullmatch(str(url or ""))
        if match is None:
            raise TelegramRequestNotSent("relay", "invalid relay endpoint URL")
        method = match.group(1)
        if method not in _RELAY_JSON_METHODS:
            raise TelegramRequestNotSent(method, "method_not_allowed_on_owner_feed")
        sideband = self._sideband(method)
        if method == "getFile":
            file_id = str(payload.get("file_id") or "").strip() if isinstance(payload, dict) else ""
            if not file_id or set(payload) != {"file_id"}:
                raise TelegramRequestNotSent("getFile", "argument_not_allowed")
            params: Dict[str, Any] = {"file_id": file_id}
        else:
            params = dict(payload)
            if "reply_parameters" in params:
                rp = params["reply_parameters"]
                if not isinstance(rp, dict) or set(rp) != {"message_id"} or type(rp.get("message_id")) is not int:
                    raise TelegramRequestNotSent(method, "reply_parameters may carry only message_id")
            self._check_target_matches_binding(method, params)
        return await self._post_relay(
            "/telegram/feed/call",
            {**sideband, "method": method, "params": params},
            max(timeout_sec, _RELAY_GET_FILE_TIMEOUT) if method == "getFile" else timeout_sec,
            method,
        )

    async def post_multipart(
        self,
        url: str,
        fields: Dict[str, str],
        file_field: str,
        file_path: pathlib.Path,
        timeout_sec: float,
    ) -> Dict[str, Any]:
        return await self.post_multipart_files(url, fields, [(file_field, file_path)], timeout_sec)

    async def post_multipart_files(
        self,
        url: str,
        fields: Dict[str, str],
        files: Sequence[Tuple[str, pathlib.Path]],
        timeout_sec: float,
    ) -> Dict[str, Any]:
        match = _RELAY_METHOD_RE.fullmatch(str(url or ""))
        if match is None:
            raise TelegramRequestNotSent("upload", "invalid relay endpoint URL")
        method = match.group(1)
        if method not in _RELAY_UPLOAD_METHODS:
            raise TelegramRequestNotSent(method, "method_not_allowed_on_owner_feed")
        if not files:
            raise TelegramRequestNotSent(method, "upload requires at least one file")
        sideband = self._sideband(method)
        params = _upload_params(fields, method, [str(field) for field, _ in files])
        self._check_target_matches_binding(method, params)
        file_entries = await asyncio.to_thread(
            lambda: [_confined_file_meta(field, pathlib.Path(path), self.allowed_files_root) for field, path in files]
        )
        return await self._post_relay(
            "/telegram/feed/upload",
            {
                **sideband,
                "method": method,
                "params": params,
                "files": file_entries,
            },
            max(timeout_sec, _RELAY_UPLOAD_TIMEOUT),
            method,
        )

    async def get_bytes(self, url: str, timeout_sec: float, max_bytes: int) -> bytes:
        match = _RELAY_FILE_RE.fullmatch(str(url or ""))
        if match is None:
            raise TelegramApiError("download", "invalid relay file URL", error_code=0)
        file_path = match.group(1)
        if not file_path or "\x00" in file_path or ".." in file_path.split("/"):
            raise TelegramApiError("download", "invalid relay file_path", error_code=0)
        sideband = self._sideband("download")
        headers = self._headers()
        query = urllib.parse.urlencode({
            "account_id": sideband["account_id"],
            "place_id": sideband["place_id"],
            "connected_revision": sideband["connected_revision"],
            "thread_id": sideband["thread_id"],
            "file_path": file_path,
        })
        target_url = f"{self.host_base}/telegram/feed/file?{query}"
        timeout_sec = max(timeout_sec, _RELAY_DOWNLOAD_TIMEOUT)
        if self.bytes_transport is not None:
            result = self.bytes_transport(target_url, headers, timeout_sec, max_bytes)
            data = await result if asyncio.iscoroutine(result) else result
            if not isinstance(data, (bytes, bytearray)):
                raise TelegramApiError("download", "relay returned non-bytes file body", error_code=0)
            if len(data) > max_bytes:
                raise TelegramFileTooLarge("download", f"file exceeds {max_bytes} bytes")
            return bytes(data)
        return await asyncio.to_thread(_relay_get_bytes, target_url, headers, timeout_sec, max_bytes)


__all__ = [
    "FEED_CAPABILITY_KEY",
    "HOLD_ACCOUNT_MISMATCH",
    "HOLD_AUTH_UNAVAILABLE",
    "HOLD_BINDING_CHANGED",
    "HOLD_RELAY_BUSY",
    "OWNER_BOT_SUBDIR",
    "OWNER_CONTRIBUTION_ID",
    "OwnerFeedClient",
    "OwnerFeedError",
    "OwnerFeedIdentity",
    "PLACE_BINDING_VERSION",
    "POLL_CLAIM_CAPABILITY_KEY",
    "PollClaimResult",
    "RELAY_API_SERVER",
    "RelayDownloadTimeout",
    "RelayRefusalError",
    "RelayTelegramTransport",
    "SOURCE_OWNER_BOT",
    "SOURCE_STANDALONE",
    "canonical_bot_id",
    "feed_item_place_binding",
    "normalize_place_binding",
    "owner_state_dir",
    "owner_store_exists",
    "owner_store_path",
    "token_bot_id_prefix",
]
