from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx


class SlackConfigurationError(RuntimeError):
    pass


TEXT_FORMATS = ("markdown", "mrkdwn", "plain")

# The bot credential is attached to private-file reads, so the set of hosts that
# may receive it is an exact allowlist, not "any HTTPS URL the payload named".
# Slack documents private file bytes at exactly this host:
# https://docs.slack.dev/messaging/working-with-files/ shows
# "url_private": "https://files.slack.com/files-pri/T.../hello.txt" and
# "url_private_download": "https://files.slack.com/files-pri/T.../download/...".
# Exact membership is also what rejects IP literals, suffix/prefix lookalikes
# such as files.slack.com.example.org, unicode homograph hostnames, and
# trailing-dot forms.
SLACK_PRIVATE_FILE_HOSTS = frozenset({"files.slack.com"})
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
# Transport failures after a request may have reached Slack: a write behind one
# of them may already be applied, so it is reported uncertain, never resent.
_RESPONSE_LOST_ERRORS = (
    httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError,
    httpx.DecodingError, TimeoutError,
)


def normalize_text_format(value: str) -> str:
    if value not in TEXT_FORMATS:
        raise SlackConfigurationError("text_format must be markdown, mrkdwn, or plain")
    return value


class SlackApiError(RuntimeError):
    def __init__(
        self,
        error: str,
        *,
        status_code: int = 0,
        retry_after: float = 0.0,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"Slack API error: {error}")
        self.error = str(error)
        self.status_code = int(status_code)
        self.retry_after = float(retry_after)
        self.details = dict(details or {})


class SlackMutationUncertain(SlackApiError):
    """A provider write may have been accepted before the response was lost."""

    def __init__(self, error: str, *, status_code: int = 0, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(error, status_code=status_code, details=details)
        self.uncertain = True


def mutation_may_have_applied(error: BaseException) -> bool:
    """Classify one provider result, without treating a timeout as retry consent.

    Rate limiting is an explicit refusal. Connection/pool failures occur before
    sending the request. A lost or malformed response, server failure, or Slack's
    internal/timeout response does not establish whether the mutation happened.
    This is an uncertainty boundary, not a provider exactly-once guarantee.
    """
    if isinstance(error, SlackMutationUncertain):
        return True
    if isinstance(error, _RESPONSE_LOST_ERRORS):
        return True
    if not isinstance(error, SlackApiError):
        return False
    if error.status_code == 429 or error.error == "ratelimited":
        return False
    return error.status_code >= 500 or error.status_code == 408 or error.error in {
        "invalid_json", "invalid_response", "unknown_error", "fatal_error",
        "internal_error", "request_timeout", "service_unavailable",
    }


@dataclass(frozen=True)
class StagedSlackFile:
    file_id: str
    name: str
    mimetype: str
    size: int
    path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "mimetype": self.mimetype,
            "size": self.size,
            "path": self.path,
        }


def chunk_message(text: str, max_length: int = 3900) -> list[str]:
    """Split text into Slack-safe chunks without dropping any characters."""

    text = str(text or "")
    if not text:
        return []
    if max_length < 32:
        raise ValueError("max_length must be at least 32")
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_length:
        window = remaining[:max_length]
        boundary = max(window.rfind("\n"), window.rfind(" "))
        if boundary < max_length // 2:
            cut = max_length
        else:
            cut = boundary + 1
        chunk = remaining[:cut]
        chunks.append(chunk)
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _safe_filename(name: str, fallback: str) -> str:
    leaf = pathlib.PurePath(str(name or "")).name
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "_", leaf).strip(" .")
    # Trim again after the cut: Host strips the staged paths it opens.
    return clean[:180].rstrip(" .") or fallback


def private_file_host(url: str) -> str:
    """Return the allowlisted Slack host of one private-file URL, or refuse.

    Refuses anything that is not plain HTTPS on a documented Slack private-file
    host: another scheme, embedded userinfo (``https://files.slack.com@evil``),
    a non-443 port, a missing host, and - through exact allowlist membership -
    IP literals and lookalike hostnames.
    """

    raw = str(url or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise SlackApiError("invalid_private_file_url") from None
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not parsed.hostname
        or port not in (None, 443)
    ):
        raise SlackApiError("invalid_private_file_url")
    if parsed.hostname not in SLACK_PRIVATE_FILE_HOSTS:
        raise SlackApiError(
            "private_file_host_not_allowed", details={"host": parsed.hostname}
        )
    return parsed.hostname


def _url_host(value: Any) -> str:
    try:
        return urlsplit(str(value or "")).hostname or ""
    except ValueError:
        return ""


def _declared_size(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


class SlackClient:
    """Persistent async Slack HTTP client with explicit close ownership."""

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        require_app_token: bool = True,
    ) -> None:
        self.bot_token = str(bot_token or "").strip()
        self.app_token = str(app_token or "").strip()
        if not self.bot_token:
            raise SlackConfigurationError("SLACK_BOT_TOKEN is missing")
        if require_app_token and not self.app_token:
            raise SlackConfigurationError("SLACK_APP_TOKEN is missing")
        if not self.bot_token.startswith("xoxb-"):
            raise SlackConfigurationError("SLACK_BOT_TOKEN must be a bot token")
        if self.app_token and not self.app_token.startswith("xapp-"):
            raise SlackConfigurationError("SLACK_APP_TOKEN must be an app-level token")
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            trust_env=False,
            follow_redirects=True,
        )
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "SlackClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def _post(
        self,
        endpoint: str,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> dict[str, Any]:
        return await self._request("POST", endpoint, payload, token=token)

    async def _post_form(
        self, endpoint: str, payload: Mapping[str, Any], *, token: str,
    ) -> dict[str, Any]:
        return await self._request("POST", endpoint, payload, token=token, form=True)

    async def _get(
        self,
        endpoint: str,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> dict[str, Any]:
        return await self._request("GET", endpoint, payload, token=token)

    async def _mutate(
        self, endpoint: str, payload: Mapping[str, Any], *, method: str = "POST",
    ) -> dict[str, Any]:
        try:
            return await self._request(method, endpoint, payload, token=self.bot_token)
        except Exception as exc:
            if mutation_may_have_applied(exc):
                raise SlackMutationUncertain(
                    exc.error if isinstance(exc, SlackApiError) else type(exc).__name__,
                    status_code=exc.status_code if isinstance(exc, SlackApiError) else 0,
                    details=exc.details if isinstance(exc, SlackApiError) else None,
                ) from exc
            raise

    async def _request(
        self,
        method: str,
        endpoint: str,
        payload: Mapping[str, Any],
        *,
        token: str,
        form: bool = False,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("SlackClient is closed")
        headers = self._headers(token)
        arguments = {"json": dict(payload)}
        if form:
            headers.pop("Content-Type", None)
            arguments = {"data": dict(payload)}
        if method == "GET":
            headers.pop("Content-Type", None)
            arguments = {"params": dict(payload)}
        response = await self._http.request(
            method,
            f"https://slack.com/api/{endpoint}",
            headers=headers,
            **arguments,
        )
        retry_after = 0.0
        try:
            retry_after = float(response.headers.get("retry-after") or 0)
        except ValueError:
            retry_after = 0.0
        if response.status_code != 200:
            try:
                details = response.json()
            except ValueError:
                details = {}
            details = details if isinstance(details, dict) else {}
            raise SlackApiError(
                str(details.get("error") or f"http_{response.status_code}"),
                status_code=response.status_code,
                retry_after=retry_after,
                details=details,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise SlackApiError(
                "invalid_json", status_code=response.status_code
            ) from exc
        if not isinstance(data, dict) or not data.get("ok"):
            error = (
                str(data.get("error") or "unknown_error")
                if isinstance(data, dict)
                else "invalid_response"
            )
            raise SlackApiError(
                error,
                status_code=response.status_code,
                retry_after=retry_after,
                details=data if isinstance(data, dict) else {},
            )
        return data

    @staticmethod
    def normalize_method_path(method: str, path: str) -> tuple[str, str]:
        """Validate a generic Web API operation without allowing URL escape."""
        selected = str(method or "").strip().upper()
        if selected not in {"GET", "POST"}:
            raise SlackConfigurationError("generic Slack API method must be GET or POST")
        endpoint = str(path or "").strip()
        if endpoint.startswith("https://") or endpoint.startswith("http://"):
            raise SlackConfigurationError("generic Slack API path must stay on slack.com/api")
        endpoint = endpoint.removeprefix("/api/").removeprefix("/api/").strip("/")
        if not endpoint or ".." in endpoint or any(char.isspace() for char in endpoint):
            raise SlackConfigurationError("generic Slack API path is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", endpoint):
            raise SlackConfigurationError("generic Slack API path must be one Slack Web API method")
        return selected, endpoint

    async def generic_request(self, *, method: str, path: str,
                              params: Mapping[str, Any] | None = None,
                              body: Mapping[str, Any] | None = None,
                              effect: str = "read") -> dict[str, Any]:
        selected, endpoint = self.normalize_method_path(method, path)
        if effect not in {"read", "write"}:
            raise SlackConfigurationError("effect must be read or write")
        payload = dict(params or {}) if selected == "GET" else dict(body or {})
        if "token" in payload:
            raise SlackConfigurationError("generic Slack API payload must not include token")
        if effect == "write":
            return await self._mutate(endpoint, payload, method=selected)
        return await self._request(selected, endpoint, payload, token=self.bot_token)

    async def auth_test(self) -> dict[str, Any]:
        return await self._post("auth.test", {}, token=self.bot_token)

    async def user_info(self, user_id: str) -> dict[str, Any]:
        user_id = str(user_id or "").strip()
        if not user_id:
            raise SlackConfigurationError("user_id is required")
        response = await self._get(
            "users.info", {"user": user_id, "include_locale": True}, token=self.bot_token
        )
        user = response.get("user")
        if not isinstance(user, dict) or user.get("id") != user_id:
            raise SlackApiError("user_identity_mismatch")
        return user

    async def conversation_info(self, channel_id: str) -> dict[str, Any]:
        channel_id = str(channel_id or "").strip()
        if not channel_id:
            raise SlackConfigurationError("channel_id is required")
        response = await self._get(
            "conversations.info", {"channel": channel_id, "include_locale": True},
            token=self.bot_token,
        )
        conversation = response.get("channel")
        if not isinstance(conversation, dict) or conversation.get("id") != channel_id:
            raise SlackApiError("conversation_identity_mismatch")
        return conversation

    @staticmethod
    def _page_limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise SlackConfigurationError("limit must be an integer between 1 and 200")
        return limit

    async def list_conversations(
        self, *, cursor: str = "", limit: int = 100,
        types: Sequence[str] = ("public_channel", "private_channel", "mpim", "im"),
        exclude_archived: bool = True,
    ) -> dict[str, Any]:
        """Return one explicit conversations.list page without guessing names."""
        payload: dict[str, Any] = {
            "limit": self._page_limit(limit),
            "exclude_archived": bool(exclude_archived),
        }
        if isinstance(types, str):
            types = (types,)
        selected = [str(item).strip() for item in types if str(item).strip()]
        if selected:
            payload["types"] = ",".join(selected)
        if cursor:
            payload["cursor"] = str(cursor)
        response = await self._get("conversations.list", payload, token=self.bot_token)
        channels = response.get("channels")
        if not isinstance(channels, list):
            raise SlackApiError("missing_channels")
        metadata = response.get("response_metadata") or {}
        next_cursor = str(metadata.get("next_cursor") or "").strip()
        return {
            **response,
            "source": "conversations.list",
            "channels": channels,
            "next_cursor": next_cursor or None,
            "complete": not bool(next_cursor or response.get("has_more")),
        }

    async def list_users(self, *, cursor: str = "", limit: int = 100,
                         include_locale: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {"limit": self._page_limit(limit), "include_locale": bool(include_locale)}
        if cursor:
            payload["cursor"] = str(cursor)
        response = await self._get("users.list", payload, token=self.bot_token)
        members = response.get("members")
        if not isinstance(members, list):
            raise SlackApiError("missing_members")
        metadata = response.get("response_metadata") or {}
        next_cursor = str(metadata.get("next_cursor") or "").strip()
        return {
            **response,
            "source": "users.list",
            "members": members,
            "next_cursor": next_cursor or None,
            "complete": not bool(next_cursor or response.get("has_more")),
        }

    async def lookup_user_by_email(self, email: str) -> dict[str, Any]:
        email = str(email or "").strip()
        if not email or "@" not in email:
            raise SlackConfigurationError("email must be a valid address")
        response = await self._get("users.lookupByEmail", {"email": email}, token=self.bot_token)
        user = response.get("user")
        if not isinstance(user, dict) or not user.get("id"):
            raise SlackApiError("missing_user")
        return user

    async def conversation_members(self, channel_id: str, *, cursor: str = "", limit: int = 100) -> dict[str, Any]:
        channel_id = str(channel_id or "").strip()
        if not channel_id:
            raise SlackConfigurationError("channel_id is required")
        payload: dict[str, Any] = {"channel": channel_id, "limit": self._page_limit(limit)}
        if cursor:
            payload["cursor"] = str(cursor)
        response = await self._get("conversations.members", payload, token=self.bot_token)
        members = response.get("members")
        if not isinstance(members, list):
            raise SlackApiError("missing_members")
        metadata = response.get("response_metadata") or {}
        next_cursor = str(metadata.get("next_cursor") or "").strip()
        return {
            **response,
            "source": "conversations.members",
            "channel_id": channel_id,
            "members": members,
            "next_cursor": next_cursor or None,
            "complete": not bool(next_cursor or response.get("has_more")),
        }

    async def join_conversation(self, channel_id: str) -> dict[str, Any]:
        channel_id = str(channel_id or "").strip()
        if not channel_id:
            raise SlackConfigurationError("channel_id is required")
        return await self._mutate("conversations.join", {"channel": channel_id})

    async def read_messages(
        self, channel_id: str, *, thread_ts: str = "", cursor: str = "",
        limit: int = 50, oldest: str = "", latest: str = "", inclusive: bool = False,
    ) -> dict[str, Any]:
        """One provider page, retaining all message text and continuation facts."""
        channel_id = str(channel_id or "").strip()
        if not channel_id:
            raise SlackConfigurationError("channel_id is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise SlackConfigurationError("limit must be an integer between 1 and 200")
        payload: dict[str, Any] = {"channel": channel_id, "limit": limit, "inclusive": bool(inclusive)}
        for key, value in (("cursor", cursor), ("oldest", oldest), ("latest", latest)):
            if value:
                payload[key] = str(value)
        endpoint = "conversations.replies" if thread_ts else "conversations.history"
        if thread_ts:
            payload["ts"] = str(thread_ts)
        response = await self._get(endpoint, payload, token=self.bot_token)
        messages = response.get("messages")
        if not isinstance(messages, list):
            raise SlackApiError("missing_messages")
        metadata = response.get("response_metadata") or {}
        next_cursor = str(metadata.get("next_cursor") or "").strip()
        has_more = bool(response.get("has_more") or next_cursor)
        result = {
            **response, "source": endpoint, "channel_id": channel_id,
            "thread_ts": thread_ts, "has_more": has_more,
            "next_cursor": next_cursor or None, "complete": not has_more,
        }
        if has_more and not next_cursor:
            # Slack also exposes time-range pagination. Retain its raw response
            # and disclose the missing cursor instead of claiming completeness.
            result["continuation_note"] = (
                "Slack reported more results without a cursor. Continue with an explicit "
                "oldest/latest range using the returned timestamps; this page is incomplete."
            )
        return result

    async def open_socket_url(self) -> str:
        data = await self._post("apps.connections.open", {}, token=self.app_token)
        url = str(data.get("url") or "").strip()
        if not url.startswith("wss://"):
            raise SlackApiError("missing_socket_url", details=data)
        return url

    async def open_direct_message(self, user_id: str) -> str:
        data = await self._post(
            "conversations.open", {"users": str(user_id)}, token=self.bot_token
        )
        channel = data.get("channel") if isinstance(data, dict) else None
        channel_id = str(channel.get("id") or "") if isinstance(channel, dict) else ""
        if not channel_id:
            raise SlackApiError("missing_dm_channel", details=data)
        return channel_id

    async def post_message(
        self,
        *,
        channel: str,
        text: str,
        thread_ts: str = "",
        text_format: str = "markdown",
    ) -> dict[str, Any]:
        selected = normalize_text_format(text_format)
        payload: dict[str, Any] = {"channel": str(channel)}
        if selected == "markdown":
            payload["markdown_text"] = str(text)
        else:
            payload.update(text=str(text), mrkdwn=selected == "mrkdwn")
        if thread_ts:
            payload["thread_ts"] = str(thread_ts)
        return await self._mutate("chat.postMessage", payload)

    async def update_message(self, *, channel: str, ts: str, text: str = "",
                             blocks: Sequence[Mapping[str, Any]] | None = None,
                             text_format: str = "markdown") -> dict[str, Any]:
        channel, ts = str(channel or "").strip(), str(ts or "").strip()
        if not channel or not ts:
            raise SlackConfigurationError("channel and ts are required")
        selected = normalize_text_format(text_format)
        payload: dict[str, Any] = {"channel": channel, "ts": ts}
        if blocks is not None:
            payload["blocks"] = list(blocks)
        if selected == "markdown":
            payload["markdown_text"] = str(text)
        else:
            payload.update(text=str(text), mrkdwn=selected == "mrkdwn")
        return await self._mutate("chat.update", payload)

    async def delete_message(self, *, channel: str, ts: str) -> dict[str, Any]:
        channel, ts = str(channel or "").strip(), str(ts or "").strip()
        if not channel or not ts:
            raise SlackConfigurationError("channel and ts are required")
        return await self._mutate("chat.delete", {"channel": channel, "ts": ts})

    async def reaction(self, *, channel: str, ts: str, name: str, add: bool = True) -> dict[str, Any]:
        channel, ts, name = str(channel or "").strip(), str(ts or "").strip(), str(name or "").strip()
        if not channel or not ts or not name:
            raise SlackConfigurationError("channel, ts and name are required")
        endpoint = "reactions.add" if add else "reactions.remove"
        return await self._mutate(endpoint, {"channel": channel, "timestamp": ts, "name": name})

    async def pin(self, *, channel: str, ts: str, add: bool = True) -> dict[str, Any]:
        channel, ts = str(channel or "").strip(), str(ts or "").strip()
        if not channel or not ts:
            raise SlackConfigurationError("channel and ts are required")
        endpoint = "pins.add" if add else "pins.remove"
        return await self._mutate(endpoint, {"channel": channel, "timestamp": ts})

    async def bookmark(self, *, channel: str, bookmark_id: str = "", title: str = "",
                       link: str = "", emoji: str = "", add: bool = True) -> dict[str, Any]:
        channel = str(channel or "").strip()
        if not channel:
            raise SlackConfigurationError("channel is required")
        if add:
            if not title or not link:
                raise SlackConfigurationError("title and link are required")
            payload = {"channel_id": channel, "title": str(title), "type": "link", "link": str(link)}
            if emoji:
                payload["emoji"] = str(emoji)
            return await self._mutate("bookmarks.add", payload)
        bookmark_id = str(bookmark_id or "").strip()
        if not bookmark_id:
            raise SlackConfigurationError("bookmark_id is required")
        return await self._mutate("bookmarks.remove", {"channel_id": channel, "bookmark_id": bookmark_id})

    async def file_info(self, file_id: str) -> dict[str, Any]:
        file_id = str(file_id or "").strip()
        if not file_id:
            raise SlackConfigurationError("file_id is required")
        response = await self._get("files.info", {"file": file_id}, token=self.bot_token)
        value = response.get("file")
        if not isinstance(value, dict):
            raise SlackApiError("missing_file")
        return value

    async def download_file(self, file_id: str, *, destination: pathlib.Path) -> StagedSlackFile:
        info = await self.file_info(file_id)
        url = str(info.get("url_private_download") or info.get("url_private") or "").strip()
        if not url:
            raise SlackApiError("missing_private_file_url")
        # Keep provider identity in the artifact path so two files with the
        # same display name cannot replace each other's immutable bytes.
        destination = pathlib.Path(destination) / _safe_filename(file_id, "file")
        staged = await self.stage_private_files(
            [{"file_id": file_id, "name": info.get("name") or info.get("title") or file_id,
              "mimetype": info.get("mimetype"), "size": info.get("size"), "url_private": url}],
            destination=destination,
            max_files=1,
        )
        return staged[0]

    async def upload_file(self, *, path: pathlib.Path, filename: str, title: str = "",
                          channel: str = "", thread_ts: str = "", initial_comment: str = "") -> dict[str, Any]:
        """Upload immutable bytes through Slack's current External Upload API."""
        try:
            data = path.read_bytes()
        except OSError as exc:
            # This read precedes every provider request. Do not classify an
            # OSError from any later upload phase as a local refusal.
            raise SlackConfigurationError(f"could not read upload input: {exc}") from exc
        if not data:
            raise SlackConfigurationError("file must not be empty")
        request = await self._post_form("files.getUploadURLExternal", {
            "filename": str(filename), "length": str(len(data)),
        }, token=self.bot_token)
        upload_url = str(request.get("upload_url") or "").strip()
        file_id = str(request.get("file_id") or "").strip()
        if not upload_url or not file_id:
            raise SlackApiError("missing_upload_url", details=request)
        try:
            response = await self._http.post(upload_url, content=data, headers={"Content-Type": "application/octet-stream"})
        except (httpx.HTTPError, TimeoutError) as exc:
            raise SlackMutationUncertain("upload_bytes_transport_uncertain") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise SlackApiError(f"upload_bytes_http_{response.status_code}", status_code=response.status_code)
        payload: dict[str, Any] = {"files": [{"id": file_id, "title": str(title or filename)}]}
        if channel:
            payload["channel_id"] = str(channel)
        if thread_ts:
            payload["thread_ts"] = str(thread_ts)
        if initial_comment:
            payload["initial_comment"] = str(initial_comment)
        # Completion shares the file; an unknown result must not start a fresh upload.
        return await self._mutate("files.completeUploadExternal", payload)

    async def resolve_target(self, target: str) -> str:
        clean = str(target or "").strip()
        if clean.startswith(("U", "W")):
            return await self.open_direct_message(clean)
        if clean.startswith("#") or clean.startswith("@"):
            raise SlackConfigurationError(
                "Use a Slack channel ID or member ID; names are not stable identifiers"
            )
        if not clean:
            raise SlackConfigurationError("Slack target is required")
        return clean

    async def stage_private_files(
        self,
        files: Sequence[Mapping[str, Any]],
        *,
        destination: pathlib.Path,
        max_files: int = 10,
        max_total_bytes: int = 50 * 1024 * 1024,
    ) -> tuple[StagedSlackFile, ...]:
        """Stage every file or raise the first refusal before touching the next file."""
        staged = await self._stage_batch(
            files, destination=destination, strict=True,
            max_files=max_files, max_total_bytes=max_total_bytes,
        )
        return tuple(item for item in staged if isinstance(item, StagedSlackFile))

    async def stage_inbound_files(
        self,
        files: Sequence[Mapping[str, Any]],
        *,
        destination: pathlib.Path,
        max_files: int = 10,
        max_total_bytes: int = 50 * 1024 * 1024,
    ) -> tuple[dict[str, Any], ...]:
        """Return one outcome per declared file, in declared order.

        A staged file keeps its path. A refusal this attempt observed - a URL the
        bot credential may not read, the batch limits, a redirect, or a completed
        HTTP answer other than 408, 429 or 5xx - becomes a blank path with its
        reason. Conditions a retry may change (408, 429, 5xx, transport, local
        write) raise and leave the whole batch to the caller's retry.
        """
        outcomes = await self._stage_batch(
            files, destination=destination, strict=False,
            max_files=max_files, max_total_bytes=max_total_bytes,
        )
        records: list[dict[str, Any]] = []
        for item, outcome in zip(files, outcomes):
            if isinstance(outcome, StagedSlackFile):
                records.append(outcome.as_dict())
                continue
            records.append({
                "file_id": str(item.get("file_id") or ""),
                "name": str(item.get("name") or ""),
                "mimetype": str(item.get("mimetype") or ""),
                "size": _declared_size(item.get("size")),
                "path": "",
                "stage_error": outcome.error,
                "stage_error_details": dict(outcome.details),
            })
        return tuple(records)

    async def _stage_batch(
        self,
        files: Sequence[Mapping[str, Any]],
        *,
        destination: pathlib.Path,
        strict: bool,
        max_files: int,
        max_total_bytes: int,
    ) -> list[StagedSlackFile | SlackApiError]:
        # One loop owns the declared ordinal, the NN-name path and the accepted
        # byte budget, so strict and inbound staging cannot drift apart.
        outcomes: list[StagedSlackFile | SlackApiError] = []
        total = 0
        for index, item in enumerate(files):
            outcome = await self._stage_file(
                index, item, destination=destination, strict=strict,
                max_files=max_files, remaining_bytes=max_total_bytes - total,
            )
            if isinstance(outcome, SlackApiError):
                if strict:
                    raise outcome
            else:
                total += outcome.size
            outcomes.append(outcome)
        return outcomes

    async def _stage_file(
        self,
        index: int,
        item: Mapping[str, Any],
        *,
        destination: pathlib.Path,
        strict: bool,
        max_files: int,
        remaining_bytes: int,
    ) -> StagedSlackFile | SlackApiError:
        """Stage one file, return this attempt's refusal, or raise a retryable error."""
        if index >= max_files:
            return SlackApiError("too_many_files")
        file_id = str(item.get("file_id") or "").strip()
        url = str(item.get("url_private") or "").strip()
        if not url and not strict:
            # Inbound names the absent locator; strict keeps its URL refusal below.
            return SlackApiError("missing_private_file_url")
        try:
            # Refuse before the credential is built, not after the request.
            private_file_host(url)
            # httpx refuses some URLs urlsplit admits (control characters,
            # length); no request can exist for them, so no retry changes them.
            httpx.URL(url)
        except httpx.InvalidURL:
            return SlackApiError("invalid_private_file_url")
        except SlackApiError as refusal:
            return refusal
        declared_size = _declared_size(item.get("size"))
        if declared_size and declared_size > remaining_bytes:
            return SlackApiError("file_batch_too_large")
        # The fallback goes through the same leaf, charset and length bound.
        filename = _safe_filename(
            str(item.get("name") or ""), _safe_filename(file_id, f"file-{index}")
        )
        path = destination / f"{index:02d}-{filename}"
        part = path.with_name(path.name + f".part.{os.getpid()}")
        refusal: SlackApiError | None = None
        written = 0
        try:
            async with self._http.stream(
                "GET",
                url,
                headers={"Authorization": f"Bearer {self.bot_token}"},
                # The shared client follows redirects; an authenticated file
                # read must not. A redirect is where the bot token would
                # otherwise be re-sent to another origin, and an unauthorized
                # url_private read redirects to a login page whose HTML would
                # silently be staged as the file's bytes.
                follow_redirects=False,
            ) as response:
                status = response.status_code
                if status in _REDIRECT_STATUS_CODES:
                    return SlackApiError(
                        f"file_redirect_refused_{status}",
                        status_code=status,
                        details={"redirect_host": _url_host(response.headers.get("location"))},
                    )
                if status in {408, 429} or status >= 500:
                    raise SlackApiError(f"file_http_{status}", status_code=status)
                if status != 200:
                    return SlackApiError(f"file_http_{status}", status_code=status)
                destination.mkdir(parents=True, exist_ok=True)
                with part.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > remaining_bytes:
                            refusal = SlackApiError("file_batch_too_large")
                            break
                        handle.write(chunk)
            if refusal is not None:
                return refusal
            os.replace(part, path)
        finally:
            if part.exists():
                part.unlink()
        return StagedSlackFile(
            file_id=file_id,
            name=filename,
            mimetype=str(item.get("mimetype") or "application/octet-stream"),
            size=written,
            path=str(path),
        )
