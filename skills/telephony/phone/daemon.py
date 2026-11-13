"""The ``phone_line`` companion: runs the line, the call manager and the control channel.

Environment (set by the host): ``HOST_SERVICE_URL``, ``HOST_SERVICE_TOKEN``,
``OUROBOROS_SKILL_STATE_DIR`` and, once the owner saved and granted it,
``TELEPHONY_SIP_PASSWORD``. The settings document is re-read every few seconds;
a change restarts the line between calls, never during one.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import config as cfg
from .control import ControlServer
from .manager import STOP_SECONDS, CallManager
from .peer import HostClient

log = logging.getLogger("telephony")
RELOAD_CHECK_SECONDS = 3.0


class Daemon:
    def __init__(self, state_dir: Path, host: Optional[HostClient], password: str):
        self.state_dir = state_dir
        self.host = host
        self.password = password
        self.manager: Optional[CallManager] = None
        self._fingerprint = ""
        self._stop = asyncio.Event()
        self._reload_lock = asyncio.Lock()

    async def _start_manager(self, config: Dict[str, Any]) -> None:
        last_dial = self.manager.policy.last_dial_at if self.manager is not None else 0.0
        self.manager = CallManager(self.state_dir, config, self.password, self.host, last_dial_at=last_dial)
        await self.manager.start()
        self._fingerprint = cfg.fingerprint(config)

    async def handle(self, op: str, args: Dict[str, Any]) -> Dict[str, Any]:
        manager = self.manager
        if manager is None:
            return {"ok": False, "code": "PHONE_LINE_NOT_READY", "message": "the line is starting"}
        if op == "status":
            return manager.status()
        if op == "dial":
            return await manager.dial(number=str(args.get("number") or ""), purpose=str(args.get("purpose") or ""),
                                      brief=str(args.get("brief") or ""),
                                      owner_approved=args.get("owner_approved") is True,
                                      live_call_id=str(args.get("live_call_id") or ""),
                                      task_id=str(args.get("task_id") or ""))
        if op == "hangup":
            return await manager.hangup(str(args.get("reason") or "agent_hangup"))
        if op == "dtmf":
            return await manager.dtmf(str(args.get("digits") or ""))
        if op == "reload":
            await self._maybe_reload(force=True)
            return {"ok": True, "status": self.manager.status() if self.manager else None}
        return {"ok": False, "code": "PHONE_CONTROL_UNKNOWN", "message": f"unknown operation {op!r}"}

    async def _maybe_reload(self, force: bool = False) -> None:
        """Restart the line on changed settings (``force``: whatever they are), never during a call. Reloads take
        turns (the periodic check and a control request may meet): each one reads the settings and the line it
        would replace only once the reload lock is its own, so no line another reload just started is ever
        replaced without being stopped."""
        async with self._reload_lock:
            if self._stop.is_set():
                return  # the companion is stopping: no new line
            config = cfg.load(self.state_dir)
            if not force and cfg.fingerprint(config) == self._fingerprint:
                return
            manager = self.manager
            if manager is None:
                await self._start_manager(config)
            else:
                async with manager._lock:  # a dial under way finishes first
                    if self.manager is not manager or manager.busy or self._stop.is_set():
                        return  # never during a call, nor while the last one is still being closed: the next check
                    manager._stopping = True
                    await manager.stop()
                    if self._stop.is_set():
                        return
                    await self._start_manager(config)
        log.info("phone line (re)started from settings")

    async def _check_settings(self) -> None:
        """The periodic check. It may wait for a dial to finish; a SIGTERM meanwhile goes straight on to the stop,
        and the check, still waiting, then finds the companion stopping and gives up."""
        check = asyncio.ensure_future(self._maybe_reload())
        stopping = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({check, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
        if check.done():
            check.result()  # a failed check ends the companion, as it always did
        else:
            check.add_done_callback(_report_late_check)

    async def run(self) -> None:
        control = ControlServer(self.state_dir, self.handle)
        await self._start_manager(cfg.load(self.state_dir))
        await control.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), RELOAD_CHECK_SECONDS)
                except asyncio.TimeoutError:
                    await self._check_settings()
        finally:
            # The supervisor kills the companion 5 s after SIGTERM: the line goes first, within one deadline for
            # all of it, so its BYE or CANCEL, the call's record and the unregistration never wait behind a control
            # request (on Python 3.12 a server's wait_closed waits for its handlers, and a dial waiting for
            # admission can take tens of seconds); the control channel then gets at most a second of its own.
            control.close()
            deadline = time.monotonic() + STOP_SECONDS
            try:
                stopped = []
                while self.manager is not None and self.manager not in stopped:  # and one a reload just started
                    stopped.append(self.manager)
                    await self.manager.stop(deadline)  # joins a reload's stop of the same line
            finally:
                await control.stop()


def _report_late_check(check: asyncio.Task) -> None:
    if not check.cancelled() and check.exception() is not None:
        log.warning("settings check failed while the line stopped: %s", check.exception())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ouroboros telephony line companion")
    parser.add_argument("--state-dir", default=os.environ.get("OUROBOROS_SKILL_STATE_DIR", ""))
    parser.add_argument("--log-level", default=os.environ.get("TELEPHONY_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not args.state_dir:
        log.error("no state directory (OUROBOROS_SKILL_STATE_DIR)")
        return 2
    url, token = os.environ.get("HOST_SERVICE_URL", ""), os.environ.get("HOST_SERVICE_TOKEN", "")
    host = HostClient(url, token) if url and token else None
    password = os.environ.get(cfg.PASSWORD_KEY, "")
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(Daemon(state_dir, host, password).run())
    return 0
