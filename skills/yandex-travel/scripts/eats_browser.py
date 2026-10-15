"""Companion process: keeps one visible Yandex Eats Chrome across tool calls.

The host starts it when the skill is enabled and stops it (SIGTERM, then a
process-tree kill) on disable, unload or Panic. Chrome is launched only when
yandex_eats_open asks for it and is closed when this process stops. It uses
only the skill's own marked profile under the state directory, never a
personal Chrome profile. The session acts only on verified service-page
elements; it has no dedicated checkout or submit operation. A generic click
still carries residual transaction risk.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from eats_driver import PlaywrightDriver  # noqa: E402
from eats_session import Session, hold_lock, prepare_profile, serve  # noqa: E402


def main() -> int:
    raw = os.environ.get("OUROBOROS_SKILL_STATE_DIR", "")
    if not raw:
        print("OUROBOROS_SKILL_STATE_DIR is not set; the Ouroboros host starts this companion",
              file=sys.stderr)
        return 2
    state_dir = Path(raw)
    state_dir.mkdir(parents=True, exist_ok=True)
    stopping: list[int] = []

    def stop(signum: int, _frame: object) -> None:
        stopping.append(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with hold_lock(state_dir / "companion.lock"):
            # The factory runs on the first open only: no profile or Chrome before that.
            session = Session(lambda: PlaywrightDriver(prepare_profile(state_dir)), state_dir=state_dir)
            print("yandex eats companion ready", flush=True)
            serve(session, state_dir, should_stop=lambda: bool(stopping))
    except RuntimeError as exc:
        print(f"yandex eats companion: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
