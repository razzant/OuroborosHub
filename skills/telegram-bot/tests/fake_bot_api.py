"""Telegram's Bot API as scripted JSON answers behind the real TelegramClient (synthetic data only).

The client, its error classification and the runtime run unchanged; only the HTTP exchange is replaced.
"""
from __future__ import annotations

import json
import pathlib
import types
from typing import Any, Dict, List

from telegram_bot.api import TelegramApiError, TelegramClient


class FakeBotApi:
    def __init__(self, *, bot_id: int = 9, username: str = "presence_bot"):
        self.bot = {"id": bot_id, "is_bot": True, "username": username, "first_name": "Presence"}
        self.calls: List[tuple] = []          # (method, fields), in order
        self.files: Dict[str, bytes] = {}     # file_id -> bytes Telegram serves
        self.declared_sizes: Dict[str, Any] = {}  # file_id -> getFile's file_size when not len(bytes); None omits it
        self.get_file_answers: Dict[str, list] = {}  # file_id -> scripted getFile answers, used up in order
        self.always: Dict[str, dict] = {}     # file_id -> a getFile answer given every time
        self.answers: Dict[str, list] = {}    # method -> scripted answers, used up in order
        self.next_message_id = 500

    # The scripted world --------------------------------------------------------------------
    def client(self, token: str = "token") -> TelegramClient:
        return TelegramClient(token, transport=self)

    def refuse(self, file_id: str, description: str, code: int = 400, *, times: int = 0, **parameters: Any) -> None:
        answer = {"ok": False, "error_code": code, "description": description,
                  **({"parameters": parameters} if parameters else {})}
        if times:
            self.get_file_answers.setdefault(file_id, []).extend([answer] * times)
        else:
            self.always[file_id] = answer

    def methods(self, name: str) -> List[dict]:
        return [fields for method, fields in self.calls if method == name]

    # TelegramTransport ----------------------------------------------------------------------
    async def post_json(self, url: str, payload: Dict[str, Any], timeout_sec: float) -> Dict[str, Any]:
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, dict(payload)))
        scripted = self._scripted(method)
        if scripted is not None:
            return scripted
        if method == "getMe":
            return {"ok": True, "result": self.bot}
        if method in {"deleteWebhook", "sendChatAction", "setMessageReaction"}:
            return {"ok": True, "result": True}
        if method == "getUpdates":
            return {"ok": True, "result": []}
        if method == "getFile":
            file_id = str(payload["file_id"])
            queued = self.get_file_answers.get(file_id)
            if queued:
                return queued.pop(0)
            if file_id in self.always:
                return self.always[file_id]
            if file_id not in self.files:
                return {"ok": False, "error_code": 400, "description": "Bad Request: invalid file_id"}
            size = self.declared_sizes.get(file_id, len(self.files[file_id]))
            return {"ok": True, "result": {"file_id": file_id, "file_unique_id": f"u-{file_id}",
                                           **({} if size is None else {"file_size": size}),
                                           "file_path": f"documents/{file_id}"}}
        if method == "sendMessage":
            return {"ok": True, "result": self._message(payload, text=payload.get("text", ""))}
        raise AssertionError(f"unscripted Bot API method {method}")

    async def post_multipart(self, url: str, fields: Dict[str, str], file_field: str, file_path: pathlib.Path,
                             timeout_sec: float) -> Dict[str, Any]:
        method = url.rsplit("/", 1)[-1]
        data = pathlib.Path(file_path).read_bytes()
        self.calls.append((method, {**fields, "_files": {file_field: (pathlib.Path(file_path).name, len(data))}}))
        scripted = self._scripted(method)
        if scripted is not None:
            return scripted
        message = self._message(fields, text=fields.get("caption", ""))
        message[file_field] = {"file_id": f"sent-{message['message_id']}", "file_size": len(data)}
        return {"ok": True, "result": message}

    async def post_multipart_files(self, url: str, fields: Dict[str, str], files, timeout_sec: float) -> Dict[str, Any]:
        method = url.rsplit("/", 1)[-1]
        attached = {name: (pathlib.Path(path).name, pathlib.Path(path).stat().st_size) for name, path in files}
        self.calls.append((method, {**fields, "_files": attached}))
        scripted = self._scripted(method)
        if scripted is not None:
            return scripted
        media = json.loads(fields["media"])
        group = f"album-{self.next_message_id}"
        messages = []
        for entry in media:
            name = entry["media"].removeprefix("attach://")
            assert name in attached, (name, attached)
            message = self._message(fields, text=entry.get("caption", ""))
            message.update({"media_group_id": group, entry["type"]: {"file_id": f"sent-{message['message_id']}"}})
            messages.append(message)
        return {"ok": True, "result": messages}

    async def get_bytes(self, url: str, timeout_sec: float, max_bytes: int) -> bytes:
        file_id = url.rsplit("/", 1)[-1]
        self.calls.append(("download", {"file_id": file_id}))
        scripted = self._scripted("download")
        if scripted is not None:
            raise TelegramApiError("download", scripted["description"], scripted.get("error_code", 0))
        return self.files[file_id]

    # ----------------------------------------------------------------------------------------
    def _scripted(self, method: str):
        queued = self.answers.get(method)
        if not queued:
            return None
        answer = queued.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _message(self, fields: Dict[str, Any], *, text: str) -> Dict[str, Any]:
        self.next_message_id += 1
        chat_id = int(fields["chat_id"])
        message = {"message_id": self.next_message_id, "date": 1_800_000_000, "from": self.bot,
                   "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"}}
        if text:
            message["caption" if "caption" in fields or "media" in fields else "text"] = text
        return message


class Clock:
    """A wall clock the tests move; stands in for ``time`` inside runtime and custody."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def use_clock(monkeypatch, clock: Clock) -> None:
    import time as real

    from telegram_bot import custody, runtime
    fake = types.SimpleNamespace(time=clock, strftime=real.strftime, gmtime=real.gmtime, sleep=real.sleep)
    monkeypatch.setattr(runtime, "time", fake)
    monkeypatch.setattr(custody, "time", fake)
