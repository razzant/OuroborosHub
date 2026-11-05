from __future__ import annotations

import asyncio
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
from lib.local_settings import (  # noqa: E402
    NEW_ROOT_CHANNELS, LocalSettingsError, load_local_settings, normalize_channel_ids,
)
from lib.presence import slack_identity  # noqa: E402
from lib.runtime import BridgeRuntime  # noqa: E402
from lib.slack_api import SlackClient  # noqa: E402
from lib.store import BridgeStore  # noqa: E402

log = logging.getLogger("slack_bridge")


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _new_root_channels(settings: dict[str, Any]) -> tuple[list[str], str]:
    """The saved channel list and its error; an invalid list marks no event.

    The form refuses invalid input; a hand-edited file must not stop transport.
    """
    try:
        return normalize_channel_ids(settings.get(NEW_ROOT_CHANNELS)), ""
    except ValueError as exc:
        log.warning("Ignoring %s: %s", NEW_ROOT_CHANNELS, exc)
        return [], str(exc)


async def _run() -> None:
    state_dir = pathlib.Path(os.environ.get("OUROBOROS_SKILL_STATE_DIR") or ".")
    store = BridgeStore(state_dir)
    store.set_runtime(socket_state="starting", companion_pid=os.getpid())
    try:
        settings = load_local_settings(state_dir)
    except LocalSettingsError as exc:
        # Same contract as the extension child: unreadable settings are an
        # explicit failure, never a silently empty configuration.
        store.set_runtime(socket_state="error", last_socket_error=str(exc)[:500])
        raise
    new_root_channels, channels_error = _new_root_channels(settings)
    # What this companion actually uses, for the status route.
    store.set_runtime(new_root_channels=new_root_channels, new_root_channels_error=channels_error)

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
            bot_id=str(auth.get("bot_id") or ""),
            app_id=str(auth.get("app_id") or ""),
            workspace_id=str(auth.get("team_id") or ""),
            workspace_name=str(auth.get("team") or ""),
            socket_state="authorized",
            last_socket_error="",
        )

        runtime = BridgeRuntime(
            store=store,
            slack=slack,
            host=create_host_adapter(str(settings.get("binding_id") or "").strip(), identity=slack_identity(auth)),
            bot_user_id=bot_user_id,
            bot_id=str(auth.get("bot_id") or ""),
            app_id=str(auth.get("app_id") or ""),
            inbound_workers=_bounded_int(
                settings.get("SLACK_INBOUND_WORKERS"), 4, minimum=1, maximum=16
            ),
            outbound_workers=_bounded_int(
                settings.get("SLACK_OUTBOUND_WORKERS"), 2, minimum=1, maximum=8
            ),
            new_root_channels=new_root_channels,
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
