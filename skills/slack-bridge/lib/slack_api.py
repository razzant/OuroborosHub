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
    return clean[:180] or fallback


class SlackClient:
    """Persistent async Slack HTTP client with explicit close ownership."""

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.bot_token = str(bot_token or "").strip()
        self.app_token = str(app_token or "").strip()
        if not self.bot_token:
            raise SlackConfigurationError("SLACK_BOT_TOKEN is missing")
        if not self.app_token:
            raise SlackConfigurationError("SLACK_APP_TOKEN is missing")
        if not self.bot_token.startswith("xoxb-"):
            raise SlackConfigurationError("SLACK_BOT_TOKEN must be a bot token")
        if not self.app_token.startswith("xapp-"):
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
        if self._closed:
            raise RuntimeError("SlackClient is closed")
        response = await self._http.post(
            f"https://slack.com/api/{endpoint}",
            headers=self._headers(token),
            json=dict(payload),
        )
        retry_after = 0.0
        try:
            retry_after = float(response.headers.get("retry-after") or 0)
        except ValueError:
            retry_after = 0.0
        if response.status_code != 200:
            raise SlackApiError(
                f"http_{response.status_code}",
                status_code=response.status_code,
                retry_after=retry_after,
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

    async def auth_test(self) -> dict[str, Any]:
        return await self._post("auth.test", {}, token=self.bot_token)

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
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": str(channel), "text": str(text)}
        if thread_ts:
            payload["thread_ts"] = str(thread_ts)
        return await self._post("chat.postMessage", payload, token=self.bot_token)

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
        destination.mkdir(parents=True, exist_ok=True)
        staged: list[StagedSlackFile] = []
        total = 0
        for index, item in enumerate(files):
            if index >= max_files:
                raise SlackApiError("too_many_files")
            file_id = str(item.get("file_id") or "").strip()
            url = str(item.get("url_private") or "").strip()
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise SlackApiError("invalid_private_file_url")
            declared_size = max(0, int(item.get("size") or 0))
            if declared_size and total + declared_size > max_total_bytes:
                raise SlackApiError("file_batch_too_large")
            filename = _safe_filename(
                str(item.get("name") or ""), file_id or f"file-{index}"
            )
            path = destination / f"{index:02d}-{filename}"
            part = path.with_name(path.name + f".part.{os.getpid()}")
            try:
                async with self._http.stream(
                    "GET",
                    url,
                    headers={"Authorization": f"Bearer {self.bot_token}"},
                ) as response:
                    if response.status_code != 200:
                        raise SlackApiError(
                            f"file_http_{response.status_code}",
                            status_code=response.status_code,
                        )
                    written = 0
                    with part.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            written += len(chunk)
                            if total + written > max_total_bytes:
                                raise SlackApiError("file_batch_too_large")
                            handle.write(chunk)
                os.replace(part, path)
            finally:
                if part.exists():
                    part.unlink()
            total += written
            staged.append(
                StagedSlackFile(
                    file_id=file_id,
                    name=filename,
                    mimetype=str(item.get("mimetype") or "application/octet-stream"),
                    size=written,
                    path=str(path),
                )
            )
        return tuple(staged)
