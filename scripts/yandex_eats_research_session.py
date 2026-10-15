"""Operator-only reusable research controller; not registered as a skill companion.

Start once with an explicitly selected existing profile, then send individual
agent-chosen operations through the same Session protocol as the installed skill.
This does not install the skill, authorize checkout, or copy browser credentials.
The supervisor/process tool that starts this script owns its process lifetime.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import stat
import sys

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "yandex-travel"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from eats_driver import PlaywrightDriver  # noqa: E402
from eats_session import LIVE_OPS, Session, call, hold_lock, serve  # noqa: E402


def existing_profile(path: Path) -> Path:
    """Accept only an explicitly named, owned private directory, not a link."""
    if path.is_symlink():
        raise ValueError("profile is a symlink")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("profile must be an existing directory owned by this user")
    if info.st_mode & 0o077:
        raise ValueError("profile must be private (mode 0700)")
    return path.resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True,
                        help="private session directory for endpoint and audit, outside Git")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("serve", help="hold one visible browser until stopped")
    start.add_argument("--profile", type=Path, required=True,
                       help="existing owner-selected browser profile; never copied")
    request = sub.add_parser("call", help="one operation on the running browser")
    request.add_argument("op", choices=LIVE_OPS)
    request.add_argument("args", nargs="?", default="{}", help="JSON object, no credentials")
    args = parser.parse_args(argv)
    state = args.state_dir
    if state.is_symlink():
        parser.error("state directory cannot be a symlink")
    if args.command == "call":
        try:
            payload = json.loads(args.args)
            if not isinstance(payload, dict):
                raise ValueError("operation arguments must be a JSON object")
        except ValueError as exc:
            parser.error(str(exc))
        result = call(state, args.op, payload)
        if result.get("status") == "companion_unavailable":
            result["reason"] = ("this research session is not running; start serve once with the same "
                                "private state directory and an explicitly selected profile")
        print(json.dumps(result, ensure_ascii=False))
        successful = (result.get("status") in {"ok", "done", "closed", "captured_private"}
                      and result.get("page_status", "ok") == "ok"
                      and result.get("readiness") != "empty_after_wait")
        return 0 if successful else 1
    try:
        profile = existing_profile(args.profile)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    selected_state = state.resolve()
    if selected_state == profile or selected_state.is_relative_to(profile) or profile.is_relative_to(selected_state):
        parser.error("state directory and selected profile must be separate")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.stat().st_mode & 0o077:
        parser.error("state directory must be private (mode 0700)")
    stopping = False

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with hold_lock(state / "companion.lock"):
        session = Session(lambda: PlaywrightDriver(profile), state_dir=state)
        print("research session ready (Chrome launches on open)", flush=True)
        serve(session, state, should_stop=lambda: stopping)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
