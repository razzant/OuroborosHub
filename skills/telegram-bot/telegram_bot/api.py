"""Bounded Telegram Bot API client with JSON, download, and multipart helpers."""

from __future__ import annotations

import asyncio
import http.client
import json
import mimetypes
import pathlib
import re
import secrets
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from .formatting import prepare_text, prepare_caption, _telegram_html_to_plain


CLOUD_API_SERVER = "https://api.telegram.org"
# The one statement of what Telegram's Bot API serves a bot (getFile, https://core.telegram.org/bots/api#getfile)
# and accepts from it by multipart upload (#sending-files): 10 MB for a photo, 50 MB for any other file.
CLOUD_DOWNLOAD_LIMIT = 20 * 1024 * 1024
CLOUD_UPLOAD_LIMIT = 50 * 1024 * 1024
CLOUD_PHOTO_UPLOAD_LIMIT = 10 * 1024 * 1024
_MAX_UPLOAD_BYTES = CLOUD_UPLOAD_LIMIT
# Message kinds a file is sent as, by Bot API method; the multipart file field is the kind itself.
MEDIA_METHODS = {"photo": "sendPhoto", "document": "sendDocument", "video": "sendVideo", "audio": "sendAudio",
                 "voice": "sendVoice", "animation": "sendAnimation"}
# getFile's words that this file can never be served: above the server's limit, or a file_id that cannot be
# read at all. Decided by Telegram's description, never by its status code: getFile also answers a generic 400
# "wrong file_id or the file is temporarily unavailable", which usually passes and stays a retry.
_FILE_TOO_BIG = re.compile(r"file is too big", re.IGNORECASE)
_FILE_ID_UNREADABLE = re.compile(r"invalid file[ _]?id|wrong remote file identifier|can'?t unserialize", re.IGNORECASE)
# No whole answer arrived: no network, a timeout, a refused or reset connection, or an HTTP exchange that broke off.
# A body cut short raises http.client.IncompleteRead, which is no OSError.
_NO_ANSWER = (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException)


class TelegramApiError(RuntimeError):
    def __init__(self, endpoint: str, description: str, error_code: int = 0, retry_after: Optional[float] = None):
        super().__init__(
            f"Telegram {endpoint} failed ({error_code or 'transport'}): {description}"
        )
        self.endpoint = endpoint
        self.description = description
        self.error_code = int(error_code or 0)
        # Telegram's ResponseParameters.retry_after with a 429: the flood wait it names, in seconds.
        self.retry_after = retry_after


class TelegramFileTooLarge(TelegramApiError):
    """Telegram will never serve this file to a bot: it is above the Bot API download limit."""


class TelegramFileUnavailable(TelegramApiError):
    """Telegram cannot read this file_id at all; no later attempt can change that answer."""


def _file_refusal(exc: TelegramApiError) -> Optional[TelegramApiError]:
    """getFile's refusal as a final answer about this one file, or ``None`` when another attempt may pass."""
    if not exc.error_code:
        return None  # no answer from Telegram says nothing about the file
    for pattern, kind in ((_FILE_TOO_BIG, TelegramFileTooLarge), (_FILE_ID_UNREADABLE, TelegramFileUnavailable)):
        if pattern.search(exc.description):
            return kind(exc.endpoint, exc.description, exc.error_code)
    return None


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

    async def post_multipart_files(
        self, url: str, fields: Dict[str, str], files: Sequence[Tuple[str, pathlib.Path]], timeout_sec: float,
    ) -> Dict[str, Any]: ...


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

    async def post_multipart_files(
        self, url: str, fields: Dict[str, str], files: Sequence[Tuple[str, pathlib.Path]], timeout_sec: float,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self._post_multipart_files, url, fields, files, timeout_sec)

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
        return UrllibTelegramTransport._post_multipart_files(url, fields, [(file_field, file_path)], timeout_sec)

    @staticmethod
    def _post_multipart_files(
        url: str, fields: Dict[str, str], files: Sequence[Tuple[str, pathlib.Path]], timeout_sec: float,
    ) -> Dict[str, Any]:
        boundary = f"----ouroboros-{secrets.token_hex(12)}"
        body = _multipart_files_body(boundary, fields, files)
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
                declared = _declared_length(response)
                if declared is not None and len(data) < min(declared, max_bytes + 1):
                    # urllib returns a body cut below its Content-Length without complaint: the same broken read.
                    raise http.client.IncompleteRead(data, declared - len(data))
        except urllib.error.HTTPError as exc:
            # The file server answered, for example 404 once a file_path expired: getFile runs again next time.
            raise TelegramApiError("download", f"HTTP {exc.code}", exc.code) from exc
        except _NO_ANSWER as exc:
            raise TelegramApiError("download", type(exc).__name__) from exc
        if len(data) > max_bytes:  # larger than getFile said: Telegram will not serve it to a bot either
            raise TelegramFileTooLarge("download", f"file exceeds {max_bytes} bytes")
        return data


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        transport: Optional[TelegramTransport] = None,
        api_server: str = CLOUD_API_SERVER,
    ):
        self._token = str(token or "").strip()
        self._transport = transport or UrllibTelegramTransport()
        self._api_server = api_server.rstrip("/")

    @property
    def download_limit(self) -> int:
        """What this client's ``api_server`` serves a bot through getFile.

        Telegram's cloud, and a self-hosted Bot API server in its default mode, serve 20 MiB. Only a server
        started with ``--local`` lifts that, and it answers with paths on its own disk, which this connector
        does not read; so no larger limit is assumed for any server.
        """
        return CLOUD_DOWNLOAD_LIMIT

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
                # Keep ordinary messages while opting into provider facts that
                # the parser can preserve. The bot must be an admin for reaction
                # updates; Telegram simply omits them otherwise. my_chat_member,
                # the bot's own membership in a chat, needs no admin right.
                "allowed_updates": ["message", "edited_message", "message_reaction", "message_reaction_count",
                                    "channel_post", "edited_channel_post", "my_chat_member"],
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
        try:
            metadata = await self.get_file(file_id)
        except TelegramApiError as exc:
            final = _file_refusal(exc)  # "file is too big", or a file_id Telegram cannot read
            if final is not None:
                raise final from exc
            raise
        try:
            declared_size = int(metadata.get("file_size") or 0)
        except (TypeError, ValueError, OverflowError):
            declared_size = 0  # an unreadable size says nothing, as a missing one
        if declared_size > self.download_limit:
            raise TelegramFileTooLarge(
                "getFile", "file exceeds Telegram Bot API download limit"
            )
        file_path = str(metadata["file_path"]).lstrip("/")
        url = f"{self._api_server}/file/bot{self._token}/{file_path}"
        data = await self._transport.get_bytes(url, 35.0, self.download_limit)
        if declared_size > 0 and len(data) != declared_size:
            # Not the file getFile described, even in a whole HTTP answer: a lost answer, before anything is written.
            raise TelegramApiError("download", f"received {len(data)} bytes, getFile said {declared_size}")
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
        markdown: bool = True,
        topic_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        receipts = []
        for index, chunk in enumerate(prepare_text(str(text), markdown=markdown)):
            receipts.append(
                await self.send_text_chunk(
                    chat_id,
                    chunk["text"],
                    parse_mode=chunk["parse_mode"],
                    topic_id=topic_id,
                    reply_to_message_id=reply_to_message_id if index == 0 else None,
                )
            )
        return receipts

    async def send_text_chunk(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str = "",
        topic_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Send one already prepared chunk; never split or render it again."""
        payload: Dict[str, Any] = {"chat_id": str(chat_id), "text": text}
        if topic_id is not None:
            payload["message_thread_id"] = int(topic_id)
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": int(reply_to_message_id)}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            result = await self._call("sendMessage", payload)
        except TelegramApiError as exc:
            # A definitive negative 400 response permits a plain retry. A timeout
            # or unknown response never triggers a second physical send here.
            if parse_mode != "HTML" or exc.error_code != 400:
                raise
            payload.pop("parse_mode", None)
            payload["text"] = _telegram_html_to_plain(text)
            result = await self._call("sendMessage", payload)
        return {**result, "_delivery": {"text": payload["text"], "format": "html" if payload.get("parse_mode") == "HTML" else "plain"}} if isinstance(result, dict) else {}

    async def send_photo(
        self,
        chat_id: str,
        file_path: pathlib.Path,
        *,
        caption: str = "",
        markdown: bool = True,
        topic_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        return await self.send_media(
            "photo",
            chat_id,
            file_path,
            prepared_caption=prepare_caption(caption, markdown=markdown),
            topic_id=topic_id,
            reply_to_message_id=reply_to_message_id,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: pathlib.Path,
        *,
        caption: str = "",
        markdown: bool = True,
        topic_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        return await self.send_media(
            "document",
            chat_id,
            file_path,
            prepared_caption=prepare_caption(caption, markdown=markdown),
            topic_id=topic_id,
            reply_to_message_id=reply_to_message_id,
        )

    async def moderate(self, action: str, parameters: Dict[str, Any]) -> bool:
        """Apply one model-selected provider operation; the provider checks bot rights."""
        endpoint = {
            "delete_message": "deleteMessage",
            "restrict_member": "restrictChatMember",
            "ban_member": "banChatMember",
            "unban_member": "unbanChatMember",
        }.get(action)
        if endpoint is None:
            raise ValueError("unsupported Telegram moderation action")
        return bool(await self._call(endpoint, parameters))

    async def edit_message_text(
        self, chat_id: str, message_id: int, text: str, *,
        parse_mode: str = "", topic_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        parameters: Dict[str, Any] = {
            "chat_id": str(chat_id), "message_id": int(message_id), "text": str(text),
        }
        if parse_mode:
            parameters["parse_mode"] = str(parse_mode)
        if topic_id is not None:
            parameters["message_thread_id"] = int(topic_id)
        result = await self._call("editMessageText", parameters)
        return result if isinstance(result, dict) else {"result": result}

    async def set_message_reaction(
        self, chat_id: str, message_id: int, reaction: List[Dict[str, Any]],
        *, is_big: bool = False,
    ) -> bool:
        parameters: Dict[str, Any] = {
            "chat_id": str(chat_id), "message_id": int(message_id),
            "reaction": reaction, "is_big": bool(is_big),
        }
        return bool(await self._call("setMessageReaction", parameters))

    async def operation(self, method: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """Run one explicitly supported model-selected message operation."""
        if method == "editMessageText":
            result = await self.edit_message_text(
                str(parameters["chat_id"]), int(parameters["message_id"]),
                str(parameters["text"]), parse_mode=str(parameters.get("parse_mode") or ""),
            )
            return result
        if method == "setMessageReaction":
            value = parameters.get("reaction")
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise ValueError("reaction must be a list of Telegram reaction objects")
            return {"ok": await self.set_message_reaction(
                str(parameters["chat_id"]), int(parameters["message_id"]), value,
                is_big=bool(parameters.get("is_big", False)),
            )}
        raise ValueError("unsupported Telegram operation")

    async def send_media(
        self,
        kind: str,
        chat_id: str,
        file_path: pathlib.Path,
        *,
        prepared_caption: Dict[str, str],
        topic_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Send one file of a ``MEDIA_METHODS`` kind with a frozen caption and exact reply provenance."""
        endpoint = MEDIA_METHODS.get(kind)
        if endpoint is None:
            raise ValueError(f"unsupported Telegram media kind: {kind}")
        fields = {"chat_id": str(chat_id)}
        caption = prepared_caption["text"]
        parse_mode = prepared_caption["parse_mode"]
        if caption:
            fields["caption"] = caption
            if parse_mode:
                fields["parse_mode"] = parse_mode
        if topic_id is not None:
            fields["message_thread_id"] = str(int(topic_id))
        if reply_to_message_id is not None:
            fields["reply_parameters"] = json.dumps(
                {"message_id": int(reply_to_message_id)}
            )

        async def post() -> Dict[str, Any]:
            response = await self._transport.post_multipart(
                self._endpoint(endpoint),
                fields,
                kind,
                pathlib.Path(file_path),
                60.0,
            )
            return self._result(endpoint, response)

        try:
            result = await post()
        except TelegramApiError as exc:
            if not caption or parse_mode != "HTML" or exc.error_code != 400:
                raise
            fields.pop("parse_mode", None)
            fields["caption"] = _telegram_html_to_plain(caption)
            result = await post()
        return {**result, "_delivery": {"text": fields.get("caption", ""), "format": "html" if fields.get("parse_mode") == "HTML" else "plain"}}

    async def send_media_group(
        self,
        chat_id: str,
        items: Sequence[Dict[str, Any]],
        *,
        topic_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """One album: each item ``{kind, file_path, caption: {text, parse_mode}}`` attached as ``attach://fileN``.

        Telegram answers with one Message per item, in order; each carries the caption it was sent with.
        """
        media: List[Dict[str, Any]] = []
        files: List[Tuple[str, pathlib.Path]] = []
        for index, item in enumerate(items):
            entry: Dict[str, Any] = {"type": str(item["kind"]), "media": f"attach://file{index}"}
            caption = item.get("caption") or {}
            if caption.get("text"):
                entry["caption"] = caption["text"]
                if caption.get("parse_mode"):
                    entry["parse_mode"] = caption["parse_mode"]
            media.append(entry)
            files.append((f"file{index}", pathlib.Path(item["file_path"])))
        fields = {"chat_id": str(chat_id)}
        if topic_id is not None:
            fields["message_thread_id"] = str(int(topic_id))
        if reply_to_message_id is not None:
            fields["reply_parameters"] = json.dumps({"message_id": int(reply_to_message_id)})

        async def post(entries: List[Dict[str, Any]]) -> Any:
            if not self._token:
                raise TelegramApiError("sendMediaGroup", "TELEGRAM_PUBLIC_BOT_TOKEN is not configured")
            response = await self._transport.post_multipart_files(
                self._endpoint("sendMediaGroup"), {**fields, "media": json.dumps(entries, ensure_ascii=False)},
                files, 120.0)
            return self._result("sendMediaGroup", response)

        try:
            result = await post(media)
        except TelegramApiError as exc:
            # As for one file: only Telegram's definite 400 to an HTML caption permits a plain resend.
            if exc.error_code != 400 or not any(entry.get("parse_mode") == "HTML" for entry in media):
                raise
            media = [{**{key: value for key, value in entry.items() if key != "parse_mode"},
                      **({"caption": _telegram_html_to_plain(entry["caption"])} if "caption" in entry else {})}
                     for entry in media]
            result = await post(media)
        messages = [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []
        observed = []
        for index, message in enumerate(messages):
            entry = media[index] if index < len(media) else {"caption": message.get("caption", "")}
            observed.append({**message, "_delivery": {"text": entry.get("caption", ""),
                             "format": "html" if entry.get("parse_mode") == "HTML" else "plain"}})
        return observed

    async def send_chat_action(self, chat_id: str, action: str = "typing", *, topic_id: Optional[int] = None) -> bool:
        """A passing status such as "typing…"; Telegram shows it for about 5 seconds or until the bot's next message."""
        payload: Dict[str, Any] = {"chat_id": str(chat_id), "action": str(action)}
        if topic_id is not None:
            payload["message_thread_id"] = int(topic_id)
        return bool(await self._call("sendChatAction", payload, timeout_sec=10.0))

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
            try:
                code = int(payload.get("error_code") or 0)
            except (TypeError, ValueError, OverflowError):
                code = 0  # An unreadable code says nothing, so it is no code.
            parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
            wait = parameters.get("retry_after")
            raise TelegramApiError(
                endpoint,
                str(payload.get("description") or "invalid provider response"),
                code,
                retry_after=float(wait) if type(wait) in (int, float) and wait >= 0 else None,
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
    except _NO_ANSWER as exc:
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


def _declared_length(response: Any) -> Optional[int]:
    """The body length the server declared, or ``None`` when it declared none (chunked, absent or unreadable)."""
    headers = getattr(response, "headers", None) or {}
    if "chunked" in str(headers.get("Transfer-Encoding") or "").lower():
        return None
    try:
        length = int(str(headers.get("Content-Length") or ""))
    except ValueError:
        return None
    return length if length >= 0 else None


def _multipart_body(
    boundary: str,
    fields: Dict[str, str],
    file_field: str,
    file_path: pathlib.Path,
) -> bytes:
    return _multipart_files_body(boundary, fields, [(file_field, file_path)])


def _multipart_files_body(
    boundary: str,
    fields: Dict[str, str],
    files: Sequence[Tuple[str, pathlib.Path]],
) -> bytes:
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
    for file_field, file_path in files:
        path = pathlib.Path(file_path)
        if path.stat().st_size > _MAX_UPLOAD_BYTES:
            raise TelegramApiError("upload", "file exceeds Telegram Bot API upload limit")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        header_name = (
            path.name.replace("\\", "_")
            .replace('"', "_")
            .replace("\r", "_")
            .replace("\n", "_")
        )
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{file_field}"; '
                    f'filename="{header_name}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {mime}\r\n\r\n".encode(),
                path.read_bytes(),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
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
