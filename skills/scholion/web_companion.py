"""Host-supervised, private web backend for the installed Scholion package.

No model, browser launch, fixed port, global package or medical data copy. The
host owns this process; the owner selects its profile before it opens a socket.
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import threading

from plugin import _binding_id, _connection, _private_json


def main() -> None:
    state = Path(os.environ["OUROBOROS_SKILL_STATE_DIR"]).resolve()
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stopping.set())
    signal.signal(signal.SIGINT, lambda *_args: stopping.set())
    while not stopping.is_set():
        binding = _connection(state)
        if binding:
            break
        stopping.wait(0.25)
    else:
        return
    os.environ.update(binding)
    os.environ["SCHOLION_MANAGED_BY"] = "Ouroboros Hub"
    # Workers use the same isolated installation the host supplied to us.
    import scholion
    site = str(Path(scholion.__file__).resolve().parent.parent)
    os.environ["PYTHONPATH"] = os.pathsep.join([site, os.environ.get("PYTHONPATH", "")])
    from scholion.server import Handler, _Server

    token = secrets.token_urlsafe(32)

    class PrivateHandler(Handler):
        def _deny(self, state_changing):
            if not hmac.compare_digest(self.headers.get("X-Scholion-Bridge", ""), token):
                return "Use the Scholion panel in Ouroboros."
            if _binding_id(_connection(state)) != _binding_id(binding):
                return "The profile connection changed; this backend refuses another profile."
            return super()._deny(state_changing)

        def log_message(self, _format, *_args):
            # Request paths can carry a drug or a gene; do not copy them into host logs.
            return

    server = _Server(("127.0.0.1", 0), PrivateHandler)
    server.timeout = 0.5
    runtime = state / "web-runtime.json"
    _private_json(runtime, {"port": server.server_address[1], "token": token,
                            "pid": os.getpid(), "binding": _binding_id(binding),
                            "module": str(Path(scholion.__file__).resolve())})
    try:
        while not stopping.is_set():
            server.handle_request()
    finally:
        server.server_close()
        try:
            if json.loads(runtime.read_text(encoding="utf-8")).get("token") == token:
                runtime.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
