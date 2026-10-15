"""Host-supervised Bot API Presence companion, separate from owner-only bridge."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.host import HostClient  # noqa: E402
from bot.runtime import run  # noqa: E402
from bot.transport import BotTransport  # noqa: E402


def main() -> None:
    state = os.environ.get("OUROBOROS_SKILL_STATE_DIR", "")
    if not state or not Path(state).is_absolute() or Path(state).is_symlink():
        raise SystemExit("private skill state unavailable")
    origin = os.environ.get("HOST_SERVICE_URL", "")
    host_token = os.environ.get("HOST_SERVICE_TOKEN", "")
    bot_token = os.environ.get("TELEGRAM_PRESENCE_BOT_TOKEN", "")
    try:
        bot = BotTransport(bot_token, origin, host_token)
        host = HostClient(origin, host_token)
        run(Path(state), bot=bot, host=host)
    except Exception:
        # Never stringify URL/exception: Bot API URLs contain the token. The
        # host retains exit facts and setup/status reports private queue counts.
        raise SystemExit("second Presence bot paused; inspect private state and token separation") from None


if __name__ == "__main__":
    main()
