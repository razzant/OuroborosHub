#!/usr/bin/env bash
# Runs the smoke test on a GitHub-hosted runner, after acquire.sh:
#   1. the harness's own browser-free self-checks;
#   2. read-only checks of what Chrome's own sandbox needs from this runner (nothing on the system is changed: where a
#      prerequisite is missing the run ends UNVERIFIED and names it);
#   3. an X display of this run only: a display number nobody uses, a fresh cookie file, its own Xvfb and window
#      manager; a collision ends the run; nothing is taken from an existing display, session or profile;
#   4. smoke.mjs: Chrome for Testing headed with a new profile and the official extension, the skill's MCP stdio server.
# --out receives only the publishable projection (summary.json, report.md, PNGs of recognised synthetic tabs A and B);
# every other record stays in --work on the runner (work/diagnostics).
# Exit codes: 0 PASS, 1 FAIL, 2 UNVERIFIED (a prerequisite or an unreached check), 3 the harness could not run.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
acq=""
out=""
work=""
usage() { echo "usage: run.sh --acq DIR --out NEW-DIR --work NEW-DIR" >&2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --acq) acq="${2:-}"; shift 2 ;;
    --out) out="${2:-}"; shift 2 ;;
    --work) work="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[ -n "$acq" ] && [ -n "$out" ] && [ -n "$work" ] || { usage; exit 2; }
if [ "${GITHUB_ACTIONS:-}" != "true" ] || [ "${RUNNER_ENVIRONMENT:-}" != "github-hosted" ]; then
  echo "run.sh: runs only on a GitHub-hosted runner (GITHUB_ACTIONS=true, RUNNER_ENVIRONMENT=github-hosted)" >&2
  exit 2
fi
for dir in "$out" "$work"; do
  if [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
    echo "run.sh: $dir is not empty; give a new folder (Chrome must start with a new profile, results must be this run's)" >&2
    exit 2
  fi
done
mkdir -p "$out" "$work/diagnostics"
acq="$(cd "$acq" && pwd)"
out="$(cd "$out" && pwd)"
work="$(cd "$work" && pwd)"
diag="$work/diagnostics"
# Nothing of the environment this was started in reaches Chrome, xdotool or the skill's server.
unset DISPLAY XAUTHORITY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR

node="$acq/node/bin/node"
chrome="$acq/cft/chrome-linux64/chrome"
ext="$acq/ext/unpacked"
modules="$acq/closure/node_modules"
skill="$acq/hub/skills/chrome_browser"
for path in "$node" "$chrome" "$ext/manifest.json" "$modules/playwright-core/package.json" "$skill/scripts/server.mjs" "$acq/inputs-verified.json"; do
  [ -e "$path" ] || { echo "run.sh: an input is missing; run bin/acquire.sh first" >&2; exit 2; }
done

# A run that cannot establish its prerequisites ends here with a short, fixed record.
unverified() {
  printf '{\n "schema": 2,\n "test": "hub324-tabhandle-smoke",\n "verdict": "UNVERIFIED",\n "reason": "%s",\n "checks": []\n}\n' "$1" > "$out/summary.json"
  printf '# hub324 tabHandle smoke: UNVERIFIED\n\n%s.\n' "$1" > "$out/report.md"
  echo "run.sh: UNVERIFIED: $1" >&2
  exit 2
}

echo "== self-checks (no browser)"
if ! "$node" "$here/selftest.mjs" > "$diag/selftest.txt" 2>&1; then
  echo "run.sh: the harness's own self-checks failed (details stay on the runner); nothing else was run" >&2
  exit 3
fi
tail -n 1 "$diag/selftest.txt"

echo "== Chrome's prerequisites on this runner (read only)"
if ldd "$chrome" 2>/dev/null | grep -q 'not found'; then
  unverified "prerequisite missing: system libraries Chrome for Testing links against are not installed on this runner"
fi
restricted="$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)"
clone="$(cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null || echo 1)"
max_userns="$(cat /proc/sys/user/max_user_namespaces 2>/dev/null || echo 1)"
if [ "$restricted" = "1" ] || [ "$clone" = "0" ] || [ "$max_userns" = "0" ]; then
  unverified "prerequisite missing: this runner does not let unprivileged processes create user namespaces, which Chrome's own sandbox needs (GitHub-hosted ubuntu-22.04 provides them); nothing on the system was changed"
fi
"$chrome" --version 2>/dev/null | head -n 1 || true

echo "== an X display of this run only"
xauth_file="$work/Xauthority"
display_number=""
for n in $(shuf -i 120-899 -n 40); do
  if [ ! -e "/tmp/.X${n}-lock" ] && [ ! -e "/tmp/.X11-unix/X${n}" ]; then
    display_number="$n"
    break
  fi
done
[ -n "$display_number" ] || unverified "no unused X display number was found on this runner"
display=":${display_number}"
( umask 077 && : > "$xauth_file" )
xauth -q -f "$xauth_file" add "$display" MIT-MAGIC-COOKIE-1 "$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
xvfb_pid=""
wm_pid=""
cleanup() {
  [ -z "$wm_pid" ] || kill "$wm_pid" 2>/dev/null || true
  [ -z "$xvfb_pid" ] || kill "$xvfb_pid" 2>/dev/null || true
}
trap cleanup EXIT
Xvfb "$display" -screen 0 1920x1080x24 -auth "$xauth_file" -nolisten tcp -noreset > "$diag/xvfb.log" 2>&1 &
xvfb_pid=$!
for _ in $(seq 1 50); do
  kill -0 "$xvfb_pid" 2>/dev/null || unverified "the run's own X server did not start (the display number was taken meanwhile)"
  [ -S "/tmp/.X11-unix/X${display_number}" ] && break
  sleep 0.2
done
[ -S "/tmp/.X11-unix/X${display_number}" ] || unverified "the run's own X server did not open its display"
DISPLAY="$display" XAUTHORITY="$xauth_file" xdpyinfo > "$diag/xdpyinfo.txt" 2>&1 || unverified "the run's own X display does not answer with its cookie"
cookie_required="yes"
mkdir -p "$work/no-cookie-home"
if env -i PATH="$PATH" HOME="$work/no-cookie-home" DISPLAY="$display" xdpyinfo > /dev/null 2>&1; then
  unverified "the test display accepts a client without its cookie; Chrome was not started"
fi
DISPLAY="$display" XAUTHORITY="$xauth_file" openbox > "$diag/openbox.log" 2>&1 &
wm_pid=$!
sleep 1
grep -E 'dimensions|depth of root' "$diag/xdpyinfo.txt" | sed 's/^ *//' || true

echo "== smoke run"
set +e
SMOKE_DISPLAY="$display" SMOKE_XAUTHORITY="$xauth_file" SMOKE_COOKIE_REQUIRED="$cookie_required" SMOKE_WM="openbox" \
  "$node" "$here/smoke.mjs" --skill "$skill" --node-modules "$modules" --chrome "$chrome" --ext "$ext" \
  --out "$out" --work "$work" --inputs "$acq/inputs-verified.json"
code=$?
set -e
[ ! -f "$out/report.md" ] || cat "$out/report.md"
exit "$code"
