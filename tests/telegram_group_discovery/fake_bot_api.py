"""A controlled Telegram Bot API on loopback, fed raw updates by the test.

It speaks only what the plugin's ``TelegramClient`` calls: ``getMe``, ``deleteWebhook`` and ``getUpdates`` (an
offset acknowledges every update below it, as Telegram does), and it records, never delivers, anything else
(``sendMessage``, moderation, files). Nothing here knows the plugin's tables: a test waits on the public
acknowledgement, which the plugin commits in the same transaction as the update it accepted.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT_ID = 9100100200
BOT_USERNAME = "presence_group_test_bot"
TOKEN = f"{BOT_ID}:SYNTHETIC-fixture-token-not-a-secret"


class FakeBotApi:
    def __init__(self):
        self.cond = threading.Condition()
        self.queue: list[dict] = []
        self.get_me_fails = False   # the provider cannot confirm who the bot is: the account is unknown
        self.acked = 0              # the largest offset the plugin has sent: every update below it is committed
        self.calls: list[dict] = []   # {method, token_ok, body}
        self.sends: list[dict] = []   # anything that would leave the machine: must stay empty in these cases
        self._next_update = 1000
        self._next_message = 5000
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):  # noqa: N802 - stdlib callback
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                parts = self.path.lstrip("/").split("/")
                token = parts[0][3:] if parts and parts[0].startswith("bot") else ""
                method = parts[1] if len(parts) > 1 else ""
                answer = fake._answer(method, token, body)
                data = json.dumps(answer, ensure_ascii=False).encode("utf-8")
                self.send_response(200 if answer.get("ok") else int(answer.get("error_code") or 400))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, name="fake-bot-api", daemon=True)
        self.thread.start()

    # --- the provider side -----------------------------------------------------------------------------
    def _answer(self, method: str, token: str, body: dict) -> dict:
        with self.cond:
            ok = token == TOKEN
            self.calls.append({"method": method, "token_ok": ok, "body": body})
            self.cond.notify_all()
        if not ok:
            return {"ok": False, "error_code": 401, "description": "Unauthorized"}
        if method == "getMe":
            if self.get_me_fails:
                return {"ok": False, "error_code": 502, "description": "Bad Gateway (synthetic)"}
            return {"ok": True, "result": {"id": BOT_ID, "is_bot": True, "first_name": "Presence Group Test",
                                           "username": BOT_USERNAME}}
        if method == "deleteWebhook":
            return {"ok": True, "result": True}
        if method == "getUpdates":
            return {"ok": True, "result": self._updates(int(body.get("offset") or 0), float(body.get("timeout") or 1))}
        with self.cond:
            self.sends.append({"method": method, "body": body})
            self._next_message += 1
            return {"ok": True, "result": {"message_id": self._next_message, "date": 1,
                                           "chat": {"id": int(body.get("chat_id") or 0), "type": "private"}}}

    def _updates(self, offset: int, timeout: float) -> list[dict]:
        with self.cond:
            self.acked = max(self.acked, offset)
            self.queue = [update for update in self.queue if update["update_id"] >= offset]
            self.cond.notify_all()
            if not self.queue:
                # Held briefly like Telegram's long poll, ended early by a pushed update; never a bare sleep.
                self.cond.wait_for(lambda: any(update["update_id"] >= offset for update in self.queue),
                                   timeout=min(1.0, timeout))
            return [update for update in self.queue if update["update_id"] >= offset][:100]

    # --- the test side ---------------------------------------------------------------------------------
    def push(self, update: dict) -> int:
        """Queue one raw Bot API update (its ``update_id`` assigned when absent); returns the id."""
        with self.cond:
            if "update_id" not in update:
                self._next_update += 1
                update = {"update_id": self._next_update, **update}
            self.queue.append(update)
            self._next_update = max(self._next_update, update["update_id"])
            self.cond.notify_all()
            return update["update_id"]

    def wait_acked(self, update_id: int, timeout: float = 90.0) -> None:
        """Until the plugin has committed ``update_id`` (its next getUpdates names a larger offset)."""
        with self.cond:
            if not self.cond.wait_for(lambda: self.acked > update_id, timeout=timeout):
                raise TimeoutError(f"the plugin never acknowledged update {update_id} (acked offset {self.acked}, "
                                   f"calls {[c['method'] for c in self.calls][-8:]})")

    def wait_call(self, method: str, timeout: float = 90.0) -> None:
        with self.cond:
            if not self.cond.wait_for(lambda: any(c["method"] == method and c["token_ok"] for c in self.calls),
                                      timeout=timeout):
                raise TimeoutError(f"the plugin never called {method}: {[c['method'] for c in self.calls]}")

    def count(self, method: str) -> int:
        with self.cond:
            return sum(1 for call in self.calls if call["method"] == method)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
