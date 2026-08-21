"""Bounded Telegram Bot API client with JSON, download, and multipart helpers."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import pathlib
import secrets
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Protocol


_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class TelegramApiError(RuntimeError):
    def __init__(self, endpoint: str, description: str, error_code: int = 0):
        super().__init__(
            f"Telegram {endpoint} failed ({error_code or 'transport'}): {description}"
        )
        self.endpoint = endpoint
        self.description = description
        self.error_code = int(error_code or 0)


class TelegramTransport(Protocol):
    async def post_json(
        self, url: str, payload: Dict[str, Any], timeout_sec: float
    ) -> Dict[str, Any]: ...

    async def post_multipart(
        self,
        url: str,
        fields: Dict[str, str],
        file_field: str,
        file_path: pathlib.Path,
        timeout_sec: float,
    ) -> Dict[str, Any]: ...

    async def get_bytes(
        self, url: str, timeout_sec: float, max_bytes: int
    ) -> bytes: ...


class UrllibTelegramTransport:
    async def post_json(
        self, url: str, payload: Dict[str, Any], timeout_sec: float
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self._post_json, url, payload, timeout_sec)

    async def post_multipart(
        self,
        url: str,
        fields: Dict[str, str],
        file_field: str,
        file_path: pathlib.Path,
        timeout_sec: float,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._post_multipart,
            url,
            fields,
            file_field,
            file_path,
            timeout_sec,
        )

    async def get_bytes(self, url: str, timeout_sec: float, max_bytes: int) -> bytes:
        return await asyncio.to_thread(self._get_bytes, url, timeout_sec, max_bytes)

    @staticmethod
    def _post_json(
        url: str, payload: Dict[str, Any], timeout_sec: float
    ) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        return _open_json(request, timeout_sec)

    @staticmethod
    def _post_multipart(
        url: str,
        fields: Dict[str, str],
        file_field: str,
        file_path: pathlib.Path,
        timeout_sec: float,
    ) -> Dict[str, Any]:
        boundary = f"----ouroboros-{secrets.token_hex(12)}"
        body = _multipart_body(boundary, fields, file_field, file_path)
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
                "Accept": "application/json",
            },
        )
        return _open_json(request, timeout_sec)

    @staticmethod
    def _get_bytes(url: str, timeout_sec: float, max_bytes: int) -> bytes:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                data = response.read(max_bytes + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramApiError("download", type(exc).__name__) from exc
        if len(data) > max_bytes:
            raise TelegramApiError("download", f"file exceeds {max_bytes} bytes")
        return data


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        transport: Optional[TelegramTransport] = None,
        api_server: str = "https://api.telegram.org",
    ):
        self._token = str(token or "").strip()
        self._transport = transport or UrllibTelegramTransport()
        self._api_server = api_server.rstrip("/")

    async def get_me(self) -> Dict[str, Any]:
        return await self._call("getMe", {})

    async def delete_webhook(self) -> bool:
        return bool(await self._call("deleteWebhook", {"drop_pending_updates": False}))

    async def get_updates(
        self, *, offset: int, timeout_sec: int = 25
    ) -> List[Dict[str, Any]]:
        result = await self._call(
            "getUpdates",
            {
                "offset": max(0, int(offset)),
                "limit": 100,
                "timeout": max(1, min(50, int(timeout_sec))),
                "allowed_updates": ["message"],
            },
            timeout_sec=float(timeout_sec) + 15.0,
        )
        return (
            [item for item in result if isinstance(item, dict)]
            if isinstance(result, list)
            else []
        )

    async def get_file(self, file_id: str) -> Dict[str, Any]:
        result = await self._call("getFile", {"file_id": str(file_id)})
        if not isinstance(result, dict) or not result.get("file_path"):
            raise TelegramApiError("getFile", "response has no file_path")
        return result

    async def download_file(
        self, file_id: str, destination: pathlib.Path
    ) -> pathlib.Path:
        metadata = await self.get_file(file_id)
        declared_size = int(metadata.get("file_size") or 0)
        if declared_size > _MAX_DOWNLOAD_BYTES:
            raise TelegramApiError(
                "getFile", "file exceeds Telegram Bot API download limit"
            )
        file_path = str(metadata["file_path"]).lstrip("/")
        url = f"{self._api_server}/file/bot{self._token}/{file_path}"
        data = await self._transport.get_bytes(url, 35.0, _MAX_DOWNLOAD_BYTES)
        destination = pathlib.Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_bytes(data)
        temporary.replace(destination)
        return destination

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        topic_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        receipts = []
        for index, chunk in enumerate(_split_text(str(text), 4000)):
            payload: Dict[str, Any] = {"chat_id": str(chat_id), "text": chunk}
            if topic_id is not None:
                payload["message_thread_id"] = int(topic_id)
            if index == 0 and reply_to_message_id is not None:
                payload["reply_parameters"] = {"message_id": int(reply_to_message_id)}
            result = await self._call("sendMessage", payload)
            receipts.append(result if isinstance(result, dict) else {})
        return receipts

    async def send_photo(
        self,
        chat_id: str,
        file_path: pathlib.Path,
        *,
        caption: str = "",
        topic_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        return await self._send_file(
            "sendPhoto",
            "photo",
            chat_id,
            file_path,
            caption=caption,
            topic_id=topic_id,
            reply_to_message_id=reply_to_message_id,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: pathlib.Path,
        *,
        caption: str = "",
        topic_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        return await self._send_file(
            "sendDocument",
            "document",
            chat_id,
            file_path,
            caption=caption,
            topic_id=topic_id,
            reply_to_message_id=reply_to_message_id,
        )

    async def _send_file(
        self,
        endpoint: str,
        file_field: str,
        chat_id: str,
        file_path: pathlib.Path,
        *,
        caption: str,
        topic_id: Optional[int],
        reply_to_message_id: Optional[int],
    ) -> Dict[str, Any]:
        fields = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = str(caption)
        if topic_id is not None:
            fields["message_thread_id"] = str(int(topic_id))
        if reply_to_message_id is not None:
            fields["reply_parameters"] = json.dumps(
                {"message_id": int(reply_to_message_id)}
            )
        response = await self._transport.post_multipart(
            self._endpoint(endpoint),
            fields,
            file_field,
            pathlib.Path(file_path),
            60.0,
        )
        return self._result(endpoint, response)

    async def _call(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        *,
        timeout_sec: float = 35.0,
    ) -> Any:
        if not self._token:
            raise TelegramApiError(
                endpoint, "TELEGRAM_PUBLIC_BOT_TOKEN is not configured"
            )
        try:
            response = await self._transport.post_json(
                self._endpoint(endpoint), payload, timeout_sec
            )
        except TelegramApiError:
            raise
        except Exception as exc:
            raise TelegramApiError(endpoint, type(exc).__name__) from exc
        return self._result(endpoint, response)

    def _endpoint(self, endpoint: str) -> str:
        if not self._token:
            raise TelegramApiError(
                endpoint, "TELEGRAM_PUBLIC_BOT_TOKEN is not configured"
            )
        return f"{self._api_server}/bot{self._token}/{endpoint}"

    @staticmethod
    def _result(endpoint: str, response: Dict[str, Any]) -> Any:
        if not isinstance(response, dict) or not response.get("ok"):
            payload = response if isinstance(response, dict) else {}
            raise TelegramApiError(
                endpoint,
                str(payload.get("description") or "invalid provider response"),
                int(payload.get("error_code") or 0),
            )
        return response.get("result")


def _open_json(request: urllib.request.Request, timeout_sec: float) -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            data = response.read(2 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(64 * 1024).decode("utf-8", errors="replace"))
        except Exception:
            payload = {"ok": False, "error_code": exc.code, "description": "HTTP error"}
        return payload
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramApiError("request", type(exc).__name__) from exc
    if len(data) > 2 * 1024 * 1024:
        raise TelegramApiError("request", "response exceeds 2 MiB")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelegramApiError("request", "provider returned invalid JSON") from exc
    return (
        payload
        if isinstance(payload, dict)
        else {"ok": False, "description": "invalid response"}
    )


def _multipart_body(
    boundary: str,
    fields: Dict[str, str],
    file_field: str,
    file_path: pathlib.Path,
) -> bytes:
    path = pathlib.Path(file_path)
    if path.stat().st_size > _MAX_UPLOAD_BYTES:
        raise TelegramApiError("upload", "file exceeds Telegram Bot API upload limit")
    content = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    header_name = (
        path.name.replace("\\", "_")
        .replace('"', "_")
        .replace("\r", "_")
        .replace("\n", "_")
    )
    chunks: List[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{header_name}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks)


def _split_text(text: str, limit: int) -> List[str]:
    if len(text) <= limit:
        return [text]
    remaining = text
    chunks = []
    while remaining:
        cut = min(limit, len(remaining))
        if cut < len(remaining):
            boundary = max(remaining.rfind("\n", 0, cut), remaining.rfind(" ", 0, cut))
            if boundary > limit // 2:
                cut = boundary
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    return chunks or [""]
