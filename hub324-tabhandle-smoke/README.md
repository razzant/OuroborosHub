# hub324 tabHandle smoke

An ordinary end-to-end smoke test for [OuroborosHub PR #324](https://github.com/razzant/OuroborosHub/pull/324): skill
`chrome_browser` 0.5.0, where `browser_take_screenshot` can name its tab with a `tabHandle` from `browser_tabs`.
It runs on one GitHub-hosted `ubuntu-22.04` runner, as GitHub isolates it, and changes nothing on that system:

- Chrome for Testing 157.0.8103.50, headed, with a new profile and its own sandbox as shipped;
- the run's own X display: a free display number, a fresh cookie file, its own Xvfb and openbox (a collision ends
  the run); Chrome and xdotool get that `DISPLAY` and `XAUTHORITY` explicitly, nothing inherited;
- the official Playwright Extension 0.5.0 from its Chrome Web Store CRX (the build the skill's `pins.json` records);
- the skill's own MCP stdio server from the PR's files, with `@playwright/mcp` 0.0.84 and `playwright-core`
  1.65.0-alpha-2026-10-30 exactly as pinned;
- two ordinary approvals through the extension's own **Allow & select**, each only on this test's synthetic tab A: the
  harness refuses to click if the page is not the pinned extension's or lists any other tab.

Two synthetic pages are served on `127.0.0.1`: **A**, the owner's tab with a draft, and **B**, a background tab. The
top of each page is a grid that says which tab painted it and carries a 32-bit value with a check byte bound to the
tab (`lib/visual_code.mjs`). Before each capture the tab paints a new random value, so an image matches only that tab
at that moment. A stand-in for the host's request guard (`lib/guard_standin.mjs`, the skill's own wire contract)
lets the browser read only that fixture.

## Checks

| id | what is shown |
|---|---|
| P1-P6 | inputs verified, skill tree the expected one; headed CfT on the run's own display and cookie; `chrome://sandbox` "You are adequately sandboxed." and no switch weakening a check; the approval page is the extension's 0.5.0 and lists only tab A; the skill's server starts with `tabHandle` only on the screenshot tool; first approval ready |
| S1 | A is in front, its draft focused; the owner clicks into it and types with real X11 keystrokes (xdotool) |
| S2, S3 | `browser_tabs new` opens B without activating it; `select` makes A current again; A and B get distinct handles |
| S4, S6 | two captures by B's handle each return B's grid with the value B painted just before the call, while B is hidden |
| S5, S7 | during each capture A stays visible, its draft focused and unchanged; B gets no focus or input |
| S8 | after the captures the owner's keystrokes still land in A's draft, none in B |
| S9 | a screenshot without a handle is A's, with A's current value |
| S10 | once the owner's close of B is confirmed (page gone, target gone, not listed), B's handle answers `BROWSER_TAB_HANDLE_UNKNOWN` with no image |
| S11 | `browser_disconnect`, a new link, the second ordinary approval |
| S12 | A's old handle: `BROWSER_TAB_HANDLE_UNKNOWN`, no image, while A is listed again under a new handle |
| S13 | the closed B's old handle: `BROWSER_TAB_HANDLE_UNKNOWN`, no image, and B is not listed |
| S14, S15, S16 | A's new handle captures A; a screenshot without a handle is A; A was never reloaded and kept its draft |
| I1 | required: B's page reported no switch to visible during either capture |

A refusal is judged only by its error flag, its code and its number of image blocks; an unexpected image is counted,
never stored. Verdict: **PASS** when every required check passes; **FAIL** when a scenario check fails or the skill
itself does not start or connect; **UNVERIFIED** when a prerequisite or the environment is missing or a check was not
reached. Exit codes: 0, 1, 2; 3 when the harness could not run.

## How it runs

On the branch `qa/chrome-324-native` of a repository used for throwaway checks: this folder at the root as
`hub324-tabhandle-smoke/` and `ci/hub324-tabhandle-smoke.yml` at `.github/workflows/hub324-tabhandle-smoke.yml`. A push
to that branch runs, on `ubuntu-22.04`:

```sh
sudo apt-get update -qq
sudo apt-get install -y -qq --no-install-recommends xvfb xauth openbox xdotool x11-utils
bash hub324-tabhandle-smoke/bin/acquire.sh --dest "$RUNNER_TEMP/acq"
bash hub324-tabhandle-smoke/bin/run.sh --acq "$RUNNER_TEMP/acq" --out "$RUNNER_TEMP/out" --work "$RUNNER_TEMP/work"
```

`acquire.sh` and `run.sh` refuse to run unless `GITHUB_ACTIONS=true` and `RUNNER_ENVIRONMENT=github-hosted`.
`acquire.sh --hub-sha <40 hex>` takes another commit only if its `skills/chrome_browser` tree is the expected one.
`run.sh` finally executes (paths under `$RUNNER_TEMP/acq`, display and cookie its own):

```sh
SMOKE_DISPLAY=:<free number> SMOKE_XAUTHORITY=<work>/Xauthority SMOKE_COOKIE_REQUIRED=yes|no SMOKE_WM=openbox \
  node/bin/node hub324-tabhandle-smoke/smoke.mjs --skill hub/skills/chrome_browser --node-modules closure/node_modules \
  --chrome cft/chrome-linux64/chrome --ext ext/unpacked --out <out> --work <work> --inputs inputs-verified.json
```

Chrome gets exactly: `--user-data-dir=<work>/profile --crash-dumps-dir=<work>/crash --no-first-run
--no-default-browser-check --disable-default-apps --disable-sync --disable-background-networking
--disable-component-update --password-store=basic --window-position=0,0 --window-size=1280,960
--force-device-scale-factor=1 --remote-debugging-pipe --disable-extensions-except=<ext> --load-extension=<ext>` and tab
A's loopback address, with the environment `PATH DISPLAY XAUTHORITY HOME TMPDIR XDG_CONFIG_HOME XDG_CACHE_HOME LANG TZ`.
`--remote-debugging-pipe` is the harness's control channel to this test browser (no port); it only opens the approval
link and `chrome://sandbox`, presses Allow & select for tab A, lists targets and closes B (`lib/cdp_pipe.mjs` refuses
anything else).

## Prerequisites (named, never worked around)

- **Unprivileged user namespaces**, which Chrome's own sandbox needs. GitHub-hosted `ubuntu-22.04` provides them;
  where they are restricted (for example Ubuntu 24.04's AppArmor default), `run.sh` ends UNVERIFIED and changes nothing.
- **Chrome's system libraries** (present on GitHub's Ubuntu images); missing ones end the run UNVERIFIED.
- **The Web Store still serving the pinned 0.5.0 CRX** (sha256 `598517ba87d9f2f3b3642483cb6662bc0ea061401c5d3c1eb188a19847c92b0b`);
  otherwise `acquire.sh` stops with exit 3.

## Inputs, official sources and checks (`inputs.json`, `bin/acquire.sh`, `bin/inputs.py`)

| input | official source | checked against |
|---|---|---|
| OuroborosHub `688d392e6df8953b9c266af2dfab7b1cedbcf1ad` | `https://github.com/razzant/OuroborosHub` (git fetch by full SHA) | git tree of `skills/chrome_browser` must be `2b53cec606e91c978dab5144a746463f2ada0c83` before anything else is fetched; `SKILL.md` must say 0.5.0 |
| Node.js 24.16.0 linux-x64 | `https://nodejs.org/dist/v24.16.0/` | `SHASUMS256.txt` there |
| Chrome for Testing 157.0.8103.50 linux64 | `storage.googleapis.com/chrome-for-testing-public` | listed in Google's `known-good-versions-with-downloads.json`; size and md5 equal the archive's storage metadata (sha256 computed and recorded: Google publishes none) |
| Playwright Extension 0.5.0 | Chrome Web Store update service, id `mmlmfjhmonkocbjadbfplnigmagldckm` | CRX sha256 from the skill's `pins.json`; signed `crx_id` and manifest key give the id; three pinned file hashes; CRX3 signatures checked with openssl and recorded |
| `@playwright/mcp` 0.0.84, `playwright-core` and `playwright` 1.65.0-alpha-2026-10-30 | `registry.npmjs.org` tarballs named in the skill's `pins.json` | sha512 integrity and sha1 shasum; unpacked without npm or install scripts; pinned file hashes |

## What leaves the runner

Only the artifact `hub324-tabhandle-smoke-<run id>` with:

- `summary.json`: verdict, reason, checks (fixed sentences, error codes, counts, page event kinds), versions, hashes
  and the roles and values the images read as;
- `report.md`: the same as a table;
- `images/NNN-<label>.png`: only images that read as exactly the expected synthetic tab and value.

The workflow log gets only fixed progress lines, the check statuses and `report.md`. Tool texts, the approval link,
relay addresses, page addresses, guard questions, Chrome's and the server's own output, the profile and the cookie
stay in `<work>` on the runner (`<work>/diagnostics`, relay paths and approval queries redacted) and end with it.

## Limits

One Linux desktop under Xvfb with software rendering; not branded Chrome, not macOS or Windows; the extension is loaded
unpacked from the official CRX bytes (without `_metadata/`, which Chrome refuses unpacked), not installed from the Web
Store. The host is a stand-in on the guard's wire contract, without the Ouroboros core or its Safety model. The owner
is xdotool: real X11 input without human timing. `document.hasFocus()` is recorded, not judged, since Playwright can
emulate page focus; focus is judged by the focused element, visibility and blur events, and where keystrokes land.
Whether Chrome gives a fresh frame of a hidden tab on this path is what S4 and S6 measure.
