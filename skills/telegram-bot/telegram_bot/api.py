"""Bounded Telegram Bot API client with JSON, download, and multipart helpers."""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import inspect
import json
import mimetypes
import os
import pathlib
import re
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Protocol, Sequence, Tuple

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
# The reasons urllib can give only before any connection exists, so before any byte of the request left: the server's
# name did not resolve, or it refused the connection. Every other reason may come while the request is written.
_NO_CONNECTION = (socket.gaierror, ConnectionRefusedError)
# A file is written to the connection in pieces of this size. The request's socket timeout bounds each piece, so a
# slow upload that keeps moving is never cut by a deadline for the whole body (one write of it all made it one).
_UPLOAD_CHUNK = 64 * 1024


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


class TelegramTokenMalformed(TelegramApiError):
    """The configured token cannot form a request line (a space, a tab, a control or a non-ASCII character), so the
    request was refused here before any connection. Only the exception's type name is kept: its own message quotes
    the URL, token included. Still a TelegramApiError without a code, so senders treat it as before."""


def without_token(text: str, token: str) -> str:
    """``text`` with the bot token, as written or URL-quoted, replaced by a fixed marker: a provider's description
    (or a proxy in front of it) may echo the request path ``/bot<token>/<method>``."""
    if not token:
        return text
    for form in {token, urllib.parse.quote(token, safe=""), urllib.parse.quote(token)}:
        text = text.replace(form, "<token>")
    return text


class TelegramFileTooLarge(TelegramApiError):
    """Telegram will never serve this file to a bot: it is above the Bot API download limit."""


class TelegramFileUnavailable(TelegramApiError):
    """Telegram cannot read this file_id at all; no later attempt can change that answer."""


class TelegramRequestNotSent(TelegramApiError):
    """No byte of the request left this machine, so Telegram cannot have acted on it.

    Only two stages prove it: the upload could not be built (its file was unreadable or over the limit), or there was
    no connection at all (the name did not resolve, or the connection was refused). Any failure once a connection
    exists, while the request is written or its answer awaited, is a plain ``TelegramApiError``: Telegram may have
    acted.
    """


def _file_refusal(exc: TelegramApiError) -> Optional[TelegramApiError]:
    """getFile's refusal as a final answer about this one file, or ``None`` when another attempt may pass."""
    if not exc.error_code:
        return None  # no answer from Telegram says nothing about the file
    for pattern, kind in ((_FILE_TOO_BIG, TelegramFileTooLarge), (_FILE_ID_UNREADABLE, TelegramFileUnavailable)):
        if pattern.search(exc.description):
            return kind(exc.endpoint, exc.description, exc.error_code)
    return None


class UploadProgress(Protocol):
    """What one multipart request reports while it runs, called from the transport's worker thread.

    ``begin`` once its files are open, before any connection. ``check_active`` raises if the attempt was stopped,
    without recording a write. ``wrote`` with 0 once the request line and headers are written, then after each
    nonfinal body write returns, with the file bytes so far (not form fields or framing). ``sent`` once the write
    of the closing boundary returns: the whole request is with the network.
    ``ended`` with ``"ok"``, ``"refused"`` and Telegram's ``error_code``, or ``"none"`` when no Bot API answer was
    read. ``begin`` and ``wrote`` may raise to stop the request where it stands; ``ended`` must not raise.
    """

    def begin(self, file_bytes_total: int) -> None: ...

    def check_active(self) -> None: ...

    def wrote(self, file_bytes_written: int) -> None: ...

    def sent(self) -> None: ...

    def ended(self, answer: str, error_code: int = 0) -> None: ...


class TelegramTransport(Protocol):
    """A transport may opt into observing multipart sends by accepting a ``progress`` keyword."""

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
        progress: Optional[UploadProgress] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._post_multipart,
            url,
            fields,
            file_field,
            file_path,
            timeout_sec,
            progress,
        )

    async def get_bytes(self, url: str, timeout_sec: float, max_bytes: int) -> bytes:
        return await asyncio.to_thread(self._get_bytes, url, timeout_sec, max_bytes)

    async def post_multipart_files(
        self, url: str, fields: Dict[str, str], files: Sequence[Tuple[str, pathlib.Path]], timeout_sec: float,
        progress: Optional[UploadProgress] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self._post_multipart_files, url, fields, files, timeout_sec, progress)

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
        progress: Optional[UploadProgress] = None,
    ) -> Dict[str, Any]:
        return UrllibTelegramTransport._post_multipart_files(
            url, fields, [(file_field, file_path)], timeout_sec, progress)

    @staticmethod
    def _post_multipart_files(
        url: str, fields: Dict[str, str], files: Sequence[Tuple[str, pathlib.Path]], timeout_sec: float,
        progress: Optional[UploadProgress] = None,
    ) -> Dict[str, Any]:
        """One multipart request, its files streamed from disk under a fixed Content-Length.

        Each file is open and within the limit before any connection, so an unreadable or oversized one sends nothing.
        Each body write has its own socket timeout of ``timeout_sec``. Reading the answer uses the same value as an
        inactivity timeout, not a deadline for receiving the whole reply.
        """
        boundary = f"----ouroboros-{secrets.token_hex(12)}"
        with contextlib.ExitStack() as opened:
            try:
                parts = _multipart_parts(boundary, fields, files, opened)
            except OSError as exc:
                raise TelegramRequestNotSent("upload", type(exc).__name__) from exc  # unread file: nothing was sent
            file_bytes = sum(part[1] for part in parts if not isinstance(part, bytes))
            if progress is not None:
                progress.begin(file_bytes)  # it may stop the request here, before any connection
            request = urllib.request.Request(
                url,
                data=_multipart_stream(parts, progress),
                method="POST",
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(file_bytes + sum(len(part) for part in parts if isinstance(part, bytes))),
                    "Accept": "application/json",
                },
            )
            answer, code = "none", 0
            try:
                payload = _open_json(request, timeout_sec, strict=True)
                answer, code = ("ok", 0) if payload.get("ok") is True else ("refused", payload["error_code"])
                return payload
            finally:
                if progress is not None:
                    progress.ended(answer, code)

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
        progress: Optional[UploadProgress] = None,
    ) -> Dict[str, Any]:
        """Send one file of a ``MEDIA_METHODS`` kind with a frozen caption and exact reply provenance.

        ``progress`` observes each request: the first, and the plain-caption one after Telegram refused HTML.
        """
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

        url = self._endpoint(endpoint)
        transport_post = self._transport.post_multipart
        observed = _observed(transport_post, progress)

        async def post() -> Dict[str, Any]:
            response = await transport_post(
                url,
                fields,
                kind,
                pathlib.Path(file_path),
                60.0,
                **observed,
            )
            return self._result(endpoint, response, self._token)

        try:
            result = await post()
        except TelegramApiError as exc:
            if not caption or parse_mode != "HTML" or exc.error_code != 400:
                raise
            fields.pop("parse_mode", None)
            fields["caption"] = _telegram_html_to_plain(caption)
            result = await post()
        # As for text: an answer that is not a Message object names no message (the outbox ends such a send uncertain).
        return {**result, "_delivery": {"text": fields.get("caption", ""), "format": "html" if fields.get("parse_mode") == "HTML" else "plain"}} if isinstance(result, dict) else {}

    async def send_media_group(
        self,
        chat_id: str,
        items: Sequence[Dict[str, Any]],
        *,
        topic_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        progress: Optional[UploadProgress] = None,
    ) -> List[Dict[str, Any]]:
        """One album: each item ``{kind, file_path, caption: {text, parse_mode}}`` attached as ``attach://fileN``.

        Telegram answers with one Message per item, in order; each carries the caption it was sent with. Every entry of
        the answer keeps its place: one that is not an object comes back as ``{}``, which names no message, so the
        outbox sees the answer's own length and which item each message is for.
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

        url = self._endpoint("sendMediaGroup")
        transport_post = self._transport.post_multipart_files
        observed = _observed(transport_post, progress)

        async def post(entries: List[Dict[str, Any]]) -> Any:
            response = await transport_post(
                url, {**fields, "media": json.dumps(entries, ensure_ascii=False)},
                files, 120.0, **observed)
            return self._result("sendMediaGroup", response, self._token)

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
        observed = []
        for index, message in enumerate(result if isinstance(result, list) else []):
            if not isinstance(message, dict):
                observed.append({})
                continue
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
        return self._result(endpoint, response, self._token)

    def _endpoint(self, endpoint: str) -> str:
        if not self._token:
            raise TelegramApiError(
                endpoint, "TELEGRAM_PUBLIC_BOT_TOKEN is not configured"
            )
        return f"{self._api_server}/bot{self._token}/{endpoint}"

    @staticmethod
    def _result(endpoint: str, response: Dict[str, Any], token: str = "") -> Any:
        """Telegram's result, or its refusal as a TelegramApiError whose description never carries ``token``."""
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
                without_token(str(payload.get("description") or "invalid provider response"), token),
                code,
                retry_after=float(wait) if type(wait) in (int, float) and wait >= 0 else None,
            )
        return response.get("result")


class _NoUploadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a later request (even a GET) cannot supply the POST's answer


def _open_multipart(request: urllib.request.Request, *, timeout: float) -> Any:
    # Replace only redirect handling; keep urllib's default HTTPS verification and proxy discovery.
    return urllib.request.build_opener(_NoUploadRedirect()).open(request, timeout=timeout)


def _open_json(request: urllib.request.Request, timeout_sec: float, *, strict: bool = False) -> Dict[str, Any]:
    """Telegram's JSON answer to ``request``.

    ``strict`` (file sends) only reads the original POST's answer: ``ok`` true to a success status, or Telegram's own
    error to an error status, ``ok`` false with a ``description`` and an integer ``error_code`` equal to that status.
    Anything else, such as a gateway's HTML error page, a cut body or an answer contradicting its status, says
    nothing about what Telegram did, so it is an error without a code: never a refusal that permits another request.
    """
    open_request = _open_multipart if strict else urllib.request.urlopen
    malformed = ""
    try:
        with open_request(request, timeout=timeout_sec) as response:
            data = _read_answer(response, 2 * 1024 * 1024, require_whole=strict)
    except urllib.error.HTTPError as exc:
        try:
            with exc:
                data = (_read_answer(exc, 64 * 1024, require_whole=True) if strict else exc.read(64 * 1024))
            payload = json.loads(data.decode("utf-8", errors="strict" if strict else "replace"))
        except _NO_ANSWER + (ValueError, TelegramApiError):
            if strict:
                raise TelegramApiError("request", f"HTTP {exc.code} without a Bot API answer") from exc
            payload = {"ok": False, "error_code": exc.code, "description": "HTTP error"}
        if strict and not _bot_api_refusal(payload, exc.code):
            raise TelegramApiError("request", f"HTTP {exc.code} without a Bot API answer") from exc
        return payload
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, _NO_CONNECTION):
            raise TelegramRequestNotSent("request", f"URLError ({type(exc.reason).__name__})") from exc
        # urllib also wraps a failure while it writes the request in URLError, and a write can break after the bytes
        # have left this machine: that says nothing about the effect.
        raise TelegramApiError("request", f"URLError ({type(exc.reason).__name__})") from exc
    except (http.client.InvalidURL, UnicodeEncodeError) as exc:
        # Only the URL can make this request line invalid: its fixed server and method are valid, so the token
        # cannot be written in it. Nothing was sent; the message keeps the type name only.
        malformed = type(exc).__name__
    except _NO_ANSWER as exc:
        raise TelegramApiError("request", type(exc).__name__) from exc
    if malformed:
        # Raised after the handler, so it chains nothing: InvalidURL's own message quotes the URL, token included, and
        # every traceback printer (sys.excepthook, asyncio's "never retrieved", logging) prints a chained exception.
        raise TelegramTokenMalformed("request", malformed)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelegramApiError("request", "provider returned invalid JSON") from exc
    if strict and not (isinstance(payload, dict) and payload.get("ok") is True):
        raise TelegramApiError("request", "success status without an ok Bot API answer")
    return (
        payload
        if isinstance(payload, dict)
        else {"ok": False, "description": "invalid response"}
    )


def _read_answer(response: Any, limit: int, *, require_whole: bool) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise TelegramApiError("request", f"response exceeds {limit} bytes")
    declared = _declared_length(response) if require_whole else None
    if declared is not None and len(data) != declared:
        raise TelegramApiError("request", f"answer has {len(data)} of {declared} declared bytes")
    return data


def _bot_api_refusal(payload: Any, status: int) -> bool:
    """Telegram's own error answer to an HTTP error ``status``; any other body there is no answer from it."""
    return (400 <= status < 600 and isinstance(payload, dict) and payload.get("ok") is False
            and type(payload.get("error_code")) is int
            and payload["error_code"] == status and isinstance(payload.get("description"), str))


def _observed(post: Any, progress: Optional[UploadProgress]) -> Dict[str, Any]:
    """Bind the optional keyword before sending; never retry a POST after a ``TypeError`` from the transport."""
    if progress is not None:
        try:
            inspect.signature(post).bind_partial(progress=progress)
        except (TypeError, ValueError):
            pass  # legacy transport (or an unreadable signature): the ordinary call still works, without progress
        else:
            return {"progress": progress}
    return {}


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


def _multipart_parts(
    boundary: str,
    fields: Dict[str, str],
    files: Sequence[Tuple[str, pathlib.Path]],
    opened: contextlib.ExitStack,
) -> List[Any]:
    """The multipart body, byte for byte as 0.12.1 joined it: framing as ``bytes``, each file as (open handle, size).

    The size is the open file's own, so the Content-Length and the bytes read agree. Each file is opened here, before
    any connection: ``OSError`` or the upload limit means nothing was sent.
    """
    framing = b"".join(
        f"--{boundary}\r\n".encode() + f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        + str(value).encode("utf-8") + b"\r\n"
        for name, value in fields.items()
    )
    parts: List[Any] = []
    for file_field, file_path in files:
        path = pathlib.Path(file_path)
        handle = opened.enter_context(open(path, "rb"))
        size = os.fstat(handle.fileno()).st_size
        if size > _MAX_UPLOAD_BYTES:
            raise TelegramRequestNotSent("upload", "file exceeds Telegram Bot API upload limit")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        header_name = (
            path.name.replace("\\", "_")
            .replace('"', "_")
            .replace("\r", "_")
            .replace("\n", "_")
        )
        framing += (
            f"--{boundary}\r\n".encode()
            + f'Content-Disposition: form-data; name="{file_field}"; filename="{header_name}"\r\n'.encode("utf-8")
            + f"Content-Type: {mime}\r\n\r\n".encode()
        )
        parts += [framing, (handle, size)]
        framing = b"\r\n"
    parts.append(framing + f"--{boundary}--\r\n".encode())
    return parts


def _multipart_stream(parts: Sequence[Any], progress: Optional[UploadProgress]) -> Iterator[bytes]:
    """The body as http.client writes it: each item yielded is one ``sendall``.

    The code after a ``yield`` runs when http.client asks for the next item, once the write of this one returned. So
    a file piece is counted only after its write, and ``sent`` comes only after the closing boundary's write. A file
    that ends before its size stops the request mid-body, which says nothing about what Telegram did.
    """
    written, last = 0, len(parts) - 1
    if progress is not None:
        progress.wrote(0)  # the request line and headers are written
    for index, part in enumerate(parts):
        if isinstance(part, bytes):
            if progress is not None:
                progress.check_active()
            yield part
            if progress is not None and index < last:  # after the closing boundary the request is whole
                progress.wrote(written)
            continue
        handle, left = part
        while left:
            piece = handle.read(min(_UPLOAD_CHUNK, left))
            if progress is not None:
                progress.check_active()  # the sender may have stopped while the disk read blocked
            if not piece:
                raise OSError(f"{pathlib.Path(handle.name).name} ended {left} bytes early while it was sent")
            yield piece
            left -= len(piece)
            written += len(piece)
            if progress is not None:
                progress.wrote(written)
    if progress is not None:
        progress.sent()


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
