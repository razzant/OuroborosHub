#!/usr/bin/env bash
# Fetches every input of the smoke test from its official source and verifies it, on a GitHub-hosted runner only:
#   1. the OuroborosHub commit (git, by its full SHA); the git tree of skills/chrome_browser must be the expected one
#      (inputs.json) before anything else is fetched;
#   2. Node.js (nodejs.org, SHASUMS256.txt);
#   3. Chrome for Testing (Google's known-good listing and the archive's storage metadata: size and md5);
#   4. the Playwright Extension CRX (Chrome Web Store update service; must equal the sha256 the skill's pins.json records);
#   5. the npm closure of @playwright/mcp (registry tarballs; integrity and shasum from the skill's pins.json).
# Writes only under --dest. Runs nothing it downloaded except `node --version`. Prints only fixed messages.
# Exit codes: 0 verified, 1 a check failed, 2 usage or not a GitHub-hosted runner, 3 the Web Store serves another build.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest=""
hub_sha=""
usage() { echo "usage: acquire.sh --dest DIR [--hub-sha 40-HEX of a commit with the expected skills/chrome_browser tree]" >&2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dest) dest="${2:-}"; shift 2 ;;
    --hub-sha) hub_sha="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[ -n "$dest" ] || { usage; exit 2; }
if [ "${GITHUB_ACTIONS:-}" != "true" ] || [ "${RUNNER_ENVIRONMENT:-}" != "github-hosted" ]; then
  echo "acquire.sh: runs only on a GitHub-hosted runner (GITHUB_ACTIONS=true, RUNNER_ENVIRONMENT=github-hosted)" >&2
  exit 2
fi
[ "$(uname -s)-$(uname -m)" = "Linux-x86_64" ] || { echo "acquire.sh: Linux x86_64 only" >&2; exit 2; }
for tool in curl git python3 unzip tar xz sha256sum; do
  command -v "$tool" > /dev/null || { echo "acquire.sh: $tool is missing on this runner" >&2; exit 2; }
done

inputs="$here/inputs.json"
py() { python3 -I "$here/bin/inputs.py" "$@"; }
get() { py get "$inputs" "$1"; }
fail() { echo "acquire.sh: $1" >&2; [ -z "${2:-}" ] || py problems "$2" >&2 || true; exit "${3:-1}"; }
hub_sha="${hub_sha:-$(get hub.commit)}"
[[ "$hub_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "acquire.sh: the Hub commit must be 40 lower-case hex digits" >&2; exit 2; }

mkdir -p "$dest"
dest="$(cd "$dest" && pwd)"
dl="$dest/downloads"
parts="$dest/verified"
mkdir -p "$dl" "$parts"
fetch() { curl --fail --location --silent --show-error --retry 3 --retry-delay 3 --proto '=https' --proto-redir '=https' -o "$2" "$1"; }

echo "== OuroborosHub $hub_sha"
rm -rf "$dest/hub"
git init -q "$dest/hub"
git -C "$dest/hub" remote add origin "$(get hub.repository)"
git -C "$dest/hub" fetch -q --depth 1 origin "$hub_sha" || fail "the Hub commit could not be fetched"
git -C "$dest/hub" -c advice.detachedHead=false checkout -q --detach FETCH_HEAD
[ "$(git -C "$dest/hub" rev-parse HEAD)" = "$hub_sha" ] || fail "the fetched commit is not the one asked for"
tree="$(git -C "$dest/hub" rev-parse HEAD:skills/chrome_browser)"
pins="$dest/hub/skills/chrome_browser/pins.json"
py hub --inputs "$inputs" --commit "$hub_sha" --tree "$tree" --skill "$dest/hub/skills/chrome_browser" > "$parts/hub.json" \
  || fail "the skill is not the one this smoke test is written for; nothing else was fetched" "$parts/hub.json"
echo "skill tree $tree"

echo "== Node.js"
node_version="$(get node.version)"
node_file="node-v${node_version}-linux-x64.tar.xz"
fetch "https://nodejs.org/dist/v${node_version}/SHASUMS256.txt" "$dl/SHASUMS256.txt"
fetch "https://nodejs.org/dist/v${node_version}/${node_file}" "$dl/$node_file"
line="$(grep -E "^[0-9a-f]{64}  ${node_file//./\\.}\$" "$dl/SHASUMS256.txt")" || fail "$node_file is not listed in SHASUMS256.txt"
(cd "$dl" && printf '%s\n' "$line" | sha256sum --check --status -) || fail "$node_file does not match SHASUMS256.txt"
rm -rf "$dest/node"
mkdir -p "$dest/node"
tar -xJf "$dl/$node_file" -C "$dest/node" --strip-components=1
[ "$("$dest/node/bin/node" --version)" = "v${node_version}" ] || fail "node --version is not v${node_version}"
printf '{"part": "node", "ok": true, "version": "%s", "file": "%s", "sha256": "%s"}\n' "$node_version" "$node_file" "${line%% *}" > "$parts/node.json"

echo "== Chrome for Testing"
cft_version="$(get chrome_for_testing.version)"
cft_url="https://storage.googleapis.com/chrome-for-testing-public/${cft_version}/linux64/chrome-linux64.zip"
fetch "$(get chrome_for_testing.listing)" "$dl/cft-known-good-versions-with-downloads.json"
fetch "https://storage.googleapis.com/storage/v1/b/chrome-for-testing-public/o/${cft_version}%2Flinux64%2Fchrome-linux64.zip" "$dl/cft-object-metadata.json"
fetch "$cft_url" "$dl/chrome-linux64.zip"
py cft --listing "$dl/cft-known-good-versions-with-downloads.json" --meta "$dl/cft-object-metadata.json" --zip "$dl/chrome-linux64.zip" \
  --version "$cft_version" --platform linux64 --url "$cft_url" > "$parts/cft.json" || fail "Chrome for Testing did not verify" "$parts/cft.json"
rm -rf "$dest/cft"
mkdir -p "$dest/cft"
unzip -q "$dl/chrome-linux64.zip" -d "$dest/cft"
[ -x "$dest/cft/chrome-linux64/chrome" ] || fail "chrome-linux64/chrome is missing from the archive"

echo "== Playwright Extension"
ext_id="$(get extension.id)"
fetch "https://clients2.google.com/service/update2/crx?response=redirect&os=linux&arch=x64&os_arch=x86_64&prod=chromecrx&prodchannel=&prodversion=${cft_version}&lang=en-US&acceptformat=crx3&x=id%3D${ext_id}%26installsource%3Dondemand%26uc" "$dl/extension.crx"
set +e
py crx --crx "$dl/extension.crx" --pins "$pins" --out "$dest/ext/unpacked" --source "Chrome Web Store update service" > "$parts/extension.json"
code=$?
set -e
[ "$code" -ne 3 ] || fail "the Web Store no longer serves the tested build (prerequisite: the pinned 0.5.0 CRX); nothing was run" "$parts/extension.json" 3
[ "$code" -eq 0 ] || fail "the extension did not verify" "$parts/extension.json"

echo "== npm closure of @playwright/mcp"
py npm-list --pins "$pins" | while read -r name url; do
  fetch "$url" "$dl/$name"
done
py npm --pins "$pins" --dl "$dl" --out "$dest/closure/node_modules" > "$parts/npm.json" || fail "the npm closure did not verify" "$parts/npm.json"

py merge --out "$dest/inputs-verified.json" "$parts/hub.json" "$parts/node.json" "$parts/cft.json" "$parts/extension.json" "$parts/npm.json" > /dev/null \
  || fail "the inputs did not verify" "$dest/inputs-verified.json"
echo "acquire.sh: every input verified"
