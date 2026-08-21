from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import signal
import sys
from typing import Any

SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from lib.host_adapter import create_host_adapter  # noqa: E402
from lib.runtime import BridgeRuntime  # noqa: E402
from lib.slack_api import SlackClient  # noqa: E402
from lib.store import BridgeStore  # noqa: E402

log = logging.getLogger("slack_bridge")


def _load_local_settings(state_dir: pathlib.Path) -> dict[str, Any]:
    path = state_dir / "settings.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Slack Bridge settings are unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Slack Bridge settings must be a JSON object")
    return value


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


async def _run() -> None:
    state_dir = pathlib.Path(os.environ.get("OUROBOROS_SKILL_STATE_DIR") or ".")
    store = BridgeStore(state_dir)
    settings = _load_local_settings(state_dir)
    store.set_runtime(socket_state="starting", companion_pid=os.getpid())

    try:
        slack = SlackClient(
            os.environ.get("SLACK_BOT_TOKEN", ""),
            os.environ.get("SLACK_APP_TOKEN", ""),
        )
    except Exception as exc:
        store.set_runtime(socket_state="error", last_socket_error=str(exc))
        raise

    runtime: BridgeRuntime | None = None
    try:
        auth = await slack.auth_test()
        bot_user_id = str(auth.get("user_id") or "")
        if not bot_user_id:
            raise RuntimeError("Slack auth.test did not return a bot user ID")
        store.set_runtime(
            bot_user_id=bot_user_id,
            workspace_id=str(auth.get("team_id") or ""),
            workspace_name=str(auth.get("team") or ""),
            socket_state="authorized",
            last_socket_error="",
        )

        runtime = BridgeRuntime(
            store=store,
            slack=slack,
            host=create_host_adapter(str(settings.get("binding_id") or "").strip()),
            bot_user_id=bot_user_id,
            inbound_workers=_bounded_int(
                settings.get("SLACK_INBOUND_WORKERS"), 4, minimum=1, maximum=16
            ),
            outbound_workers=_bounded_int(
                settings.get("SLACK_OUTBOUND_WORKERS"), 2, minimum=1, maximum=8
            ),
        )

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stop.set)
            except (NotImplementedError, RuntimeError):
                pass

        runtime_task = asyncio.create_task(runtime.run(), name="slack-bridge-runtime")
        stop_task = asyncio.create_task(stop.wait(), name="slack-bridge-stop")
        done, _pending = await asyncio.wait(
            {runtime_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if stop_task in done:
            await runtime.close()
            await asyncio.gather(runtime_task, return_exceptions=True)
        else:
            stop_task.cancel()
            await runtime_task
    except Exception as exc:
        store.set_runtime(socket_state="error", last_socket_error=str(exc)[:500])
        raise
    finally:
        if runtime is not None:
            await runtime.close()
        else:
            await slack.aclose()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("SLACK_BRIDGE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
