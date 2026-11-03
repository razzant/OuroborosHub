#!/bin/bash
# Run the qualification under the core's safe_test launcher (it scrubs inherited environment, so every
# input is passed inside it). Usage: run.sh <evidence-dir> [plugin-payload-dir]
#   TG_DISCOVERY_CORE  core candidate checkout (required)   TG_DISCOVERY_VENV  dependency-only venv (required)
#   PLAYWRIGHT_BROWSERS_PATH  cache holding webkit (required)
#   TG_DISCOVERY_ENGINES (default webkit; e.g. webkit,chromium)   TG_DISCOVERY_LEGACY_REF (default Hub 089ea45)
# Opt-in only: no default CI job collects this directory's host/browser cases.
set -u
HUB="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"
SKILL="${2:-$HUB/skills/telegram-bot}"
: "${TG_DISCOVERY_CORE:?set TG_DISCOVERY_CORE}" "${TG_DISCOVERY_VENV:?set TG_DISCOVERY_VENV}" "${PLAYWRIGHT_BROWSERS_PATH:?set PLAYWRIGHT_BROWSERS_PATH}"
cd "$HUB" || exit 2
# safe_test scrubs inherited environment: every input the tests read is passed explicitly inside it.
INPUTS=(TG_DISCOVERY_CORE="$TG_DISCOVERY_CORE" TG_DISCOVERY_SKILL="$SKILL" TG_DISCOVERY_EVIDENCE_DIR="$OUT"
        PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_PATH")
[ -n "${TG_DISCOVERY_ENGINES:-}" ] && INPUTS+=(TG_DISCOVERY_ENGINES="$TG_DISCOVERY_ENGINES")
[ -n "${TG_DISCOVERY_LEGACY_REF:-}" ] && INPUTS+=(TG_DISCOVERY_LEGACY_REF="$TG_DISCOVERY_LEGACY_REF")
exec python3 -I -S "$TG_DISCOVERY_CORE/scripts/safe_test.py" --temp-parent /tmp -- env "${INPUTS[@]}" \
  "$TG_DISCOVERY_VENV/bin/python" -B -m pytest -c /dev/null --rootdir "$HUB" "$HUB/tests/telegram_group_discovery" -p no:cacheprovider -v \
  --junitxml="$OUT/junit.xml"
