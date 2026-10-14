---
name: chrome_browser
description: Act in your own Chrome through a paired extension - one explicitly shared tab or the agent's own background tab, trusted input without taking focus, and a clean handoff of the live page to you and back.
version: 0.1.0
type: script
runtime: node
license: MIT
permissions: [net, fs, subprocess]
when_to_use: The owner asks to read or act on a page in their real, signed-in Chrome, wants to keep working in another tab meanwhile, or needs to take over the same page for a login, CAPTCHA or decision and hand it back.
model_experience:
  what_model_sees: Seven browser_* tools appear when the host has bound this skill's MCP server to the task. browser_state is one JSON object; actions return a receipt with one outcome per step. Installing the skill alone adds no tool.
  token_effect: Seven small schemas per round while bound. Snapshots, page text and screenshots cost what they contain; a read with cache_key can be searched later without reading the page again.
timeout_sec: 300
scripts:
  - name: mcp_server.mjs
    description: The task's browser executor, an MCP stdio server. The host starts it for one task attempt; it is not run by hand.
  - name: setup.mjs
    description: Owner setup. "status" shows what is configured, "init" creates the private pairing credential, "pair" confirms the extension, "rotate" replaces the credential. It never prints the credential.
install_specs:
  - kind: npm
    package: ws@8.22.0
---

# Chrome browser

This skill lets one Ouroboros task work in the owner's real Chrome without
taking the browser away from them. It has three parts:

- `extension/` — a Chrome extension (Manifest V3) the owner loads once;
- `scripts/mcp_server.mjs` — the executor the host starts for a task attempt;
- `scripts/setup.mjs` — the owner's pairing helper.

It needs an Ouroboros host that binds a skill's MCP server to a task with
`browser_protocol: ouroboros_chrome` and answers the guard described in
[`PROTOCOL.md`](PROTOCOL.md). On a host without that, the skill installs and
does nothing. POSIX hosts only in this version. Node 20 or newer (the scripts
use the global Web Crypto).

**What the permissions mean.** `net`: one loopback WebSocket port and the
host's guard socket. `fs` and `subprocess` are declared because
`browser_execute` runs the script it is given inside the executor's own Node
process: the `vm` realm is not a sandbox, and such a script can reach the file
system and start processes with the rights of that process. The host admits
`browser_execute` only where it allows unrestricted execution; the typed
`browser_action` operations use neither. The skill's own code reads and writes
only its state directory. `OUROBOROS_CHROME_STATE_DIR` names that directory for
an installation (or a disposable test root); whoever sets the executor's
environment chooses it, and the skill does not confine it further.

## What the owner does once

Loading and pairing in personal Chrome are owner gestures, not agent consent.
Ouroboros can prepare the package and private credential; it does not silently
load the extension or enter that credential in the owner's browser.

1. Run `setup.mjs init`. It prints the extension folder and the path of a
   private file, `pairing.json` (mode 600), in the skill's state directory.
2. In `chrome://extensions`, turn on **Developer mode**, choose **Load
   unpacked**, and pick the printed `extension` folder.
3. Open `pairing.json` yourself, copy the value of `credential`, click the
   extension's icon, paste it under **Pairing**, check the port, and choose
   **Save and connect**. Do not paste the credential into a chat: the setup
   script never prints it, and no tool ever returns it.
4. Run `setup.mjs pair`. It waits for that extension to prove the credential
   and records which extension it was; only that one is admitted afterwards.
5. Add the MCP server that `setup.mjs status` prints under `mcp_server` in
   Settings → MCP.

`setup.mjs rotate` replaces the credential and forgets the extension;
**Forget pairing** in the popup removes the extension's copy.

## What happens in a task

- The executor listens on one loopback port (default 47831). The extension
  tries that port when its popup opens and every 30 seconds, so a task that
  just started may wait up to that long for it.
- **Your tab.** Open the tab, click the extension icon, **Share this tab**.
  Its id appears in `browser_state.approved_tabs`; `browser_connect` with that
  `tab_id` attaches. The extension has no permission to list tabs or to read
  their titles and addresses, and no other id can be connected.
- **The agent's own tab.** `browser_connect` with a `url` opens a new tab in
  the background. While the agent holds it, it is closed again on
  `browser_disconnect`, executor exit, a lost connection or a refused
  navigation. Once it has been handed to the owner (`browser_handoff`, Stop
  sharing, Cancel) it is theirs and is never closed by the skill — also not
  after `browser_resume`, on executor exit, a worker restart or Forget pairing.
- While attached, Chrome shows its own "started debugging this browser" bar
  and the extension icon shows **ON** on that tab. **Cancel** on that bar, or
  **Stop sharing** in the popup, ends the session at once and for good.
- Nothing in this skill activates a tab, focuses a window or moves the system
  pointer. Clicks and typing are Chrome's own trusted input events sent to the
  attached tab, which may stay in the background.

## Tools

| Tool | Use |
| --- | --- |
| `browser_state` | Who owns the executor, what is leased, whether it was revoked or detached. |
| `browser_connect` | Exactly `{tab_id}` (a shared tab) or exactly `{url}` (own background tab). |
| `browser_action` | One typed operation, or `sequence` with up to 32 of them. |
| `browser_execute` | A script in the task's persistent realm. Only where the host allows unrestricted execution. |
| `browser_handoff` | Detach now and leave the page, with everything typed into it, to the owner. |
| `browser_resume` | Attach to the handed-off tab again. |
| `browser_disconnect` | End the lease and report whether Chrome confirmed the detach. |

Operations: `observe`, `read`, `get_text`, `get_attribute`, `snapshot`,
`dom_snapshot`, `ax_snapshot`, `screenshot`, `wait`, `navigate`, `click`,
`double_click`, `fill`, `type`, `press`, `check`, `uncheck`, `scroll`,
`cached_text`. A target is `{selector}` (CSS, must match one element, or add
`nth`) or `{handle}` from `snapshot`.
The operation enum excludes `select`, `drag`, `upload`, and `submit`: validating
hosts refuse them at the schema boundary. Direct executor calls additionally
return `BROWSER_OPERATION_UNSUPPORTED` without dispatch. A normal `click` or `press` can
still trigger a page's own form submission. `type` and `fill` verify that the
intended field holds focus after their trusted pointer click, before sending
keyboard input. If the page redirects focus, the click stays dispatched and
the step fails without typing into the other field.

```json
{"operation": "sequence", "args": {"steps": [
  {"operation": "click", "args": {"selector": "#start"}},
  {"operation": "wait",  "args": {"selector": "#step2", "timeout_ms": 8000}},
  {"operation": "fill",  "args": {"selector": "#step2", "text": "value"}},
  {"operation": "read",  "args": {"cache_key": "after"}}
]}}
```

```js
// browser_execute: values in `vars` stay for the next call
vars.snap = await browser.snapshot({});
const links = vars.snap.elements.filter((e) => e.tag === 'a');
await browser.click({ handle: links[0].handle });
return (await browser.read({ cache_key: 'page' })).length;
```

## Reading a receipt

Every step has an outcome. `observed`: a value was read. `not_dispatched`:
nothing reached Chrome. `dispatched`: Chrome acknowledged the input, the effect
was not checked. `confirmed`: the effect was read back. `unknown`: something
may have been sent and the answer is missing — it is never sent again; read
the page before deciding.

Before a pointer action the extension scrolls the target into view, reads its
box twice a short interval apart, and asks the page which element would
receive a pointer event at that point. A hidden, disabled, moving or covered
target is refused with nothing sent — hidden includes fully transparent
(`opacity: 0`) on the element or an ancestor. There is no force option and no
`element.click()` fallback.

## Handoff

`browser_handoff` detaches Chrome's debugger. The page is not reloaded and
nothing typed into it is lost; the owner logs in, solves the CAPTCHA or edits
the form by hand. `browser_resume` attaches again to the same tab and reads the
document afresh; element handles from before are stale, cached text is kept.
A resume the host refuses leaves the tab with the owner, still handed off and
open: resume again later, or end the handoff with `browser_disconnect`.
If `browser_disconnect` races a pending resume, it ends that handoff; a later
refusal cannot make the old tab resumable again.
Never ask the owner for a password or a code in chat.

## Limits you must not hide

- **Requests.** While attached, a request Chrome pauses for the tab waits for
  the host's verdict. Verified on a real Chromium: main-frame documents, form
  posts and fetch/XHR, each with its complete body, and every redirect hop of
  them as a request of its own. Not verified, so not claimed: iframe documents
  and requests from inside iframes, uploads with file parts (reported as an
  incomplete body, which the host refuses), beacons, dedicated and shared
  workers, service workers, WebSockets, and tabs or pop-ups the page opens. Do
  not describe the guard as network containment.
- **The first load of the agent's own tab** happens before the debugger is
  attached: the host admits the URL and then sees the landing URL, but that
  load's individual requests are not judged.
- **Between calls** a request the page starts by itself has no invocation to
  belong to; the host refuses it. A page that keeps talking to its server
  between tool calls will see failures.
- **Navigation after the call.** A click (or key, or check) that asks the page
  to navigate — a link, a form post, a script — is followed inside its step,
  also when Chrome starts that navigation late, for at most 10 seconds. The
  step's `navigation` says what became of it: `committed`, `refused`,
  `same_document`, `abandoned`, `unknown`, or `null` when the input asked for
  none. A navigation the page starts later by itself, when no call is running,
  is refused at its request and does not happen. But one still `unknown` at
  the bound can commit after the call: the host then refuses the new document,
  the lease is released (`navigation_refused`) and the page has already
  changed. Nothing is ever sent again; read the page.
- **A refused document fails its call.** When the host refuses a document the
  page has committed, the call reports an error with the host's code, the step
  keeps its true outcome, and the page is left on that document.
- **Same-document changes** (`#fragment`, `history.pushState`) change only
  `lease.url`.
- **A script that never yields** blocks the executor, including
  `browser_handoff`; the host's Stop still ends the process. The script realm
  is not a sandbox.
- **A script is cut where it waits.** A handoff, a disconnect, a revocation or
  a lost connection ends the running `browser_execute`: the steps it started
  answer (cut), the script gets one turn to catch that and return, and a
  script still waiting then — also on a promise of its own — ends with
  `BROWSER_INVOCATION_CLOSED` (or the host's code for a refused document). The
  script itself cannot be stopped; its `browser` object and `sleep` only
  refuse from then on. `browser.state()` returns a promise, like every other
  member: `await` it.
- **Revoked while attaching.** If you stop sharing, forget the pairing, cancel
  on Chrome's bar or close the tab while it is still being attached, the
  connection is refused and, for the first three, the session is revoked for
  good. A tab the agent was opening then stays open: it is yours.
- **JavaScript dialogs** (`alert`, `confirm`, `beforeunload`) are not
  answered by this skill. Steps are written to report `BROWSER_DIALOG_OPEN`
  while one is open; that path has not been exercised against a real dialog.
- **Not implemented:** `select`, `drag`, `upload`, `submit`; elements inside
  iframes; more than one lease.
- **If the extension is removed or crashes** while attached, Chrome detaches
  the debugger itself and lets paused requests continue. The executor reports
  a lost transport and never claims a confirmed detach.
- **Executor exit is not proof of detach.** Only `debugger_detach_ack: true`
  in `last_closure` is.

## Testing

`tests/regressions.test.mjs` in this package runs the executor, the extension
worker and the setup script against a mocked extension socket, a mocked
`chrome.*` and a disposable guard socket — no Chrome, no pairing:
`node --test --test-force-exit tests/regressions.test.mjs`.

`tests/chrome_browser/` in the hub repository drives the real extension in a
disposable `--headless=new` Chromium. Its pairing step types the credential
into the popup for the owner; it says nothing about a personal Chrome.

The Core registry consumer uses the actual `ToolRegistry`/`MCPManager` with a
disposable installation. Run it from the Hub root with explicit paths to the
Core checkout, its Python environment, full Chromium, and the skill's Node
dependency directory (the directory containing `node_modules`, not
`node_modules` itself):

```sh
OUROBOROS_CHROME_TEST_CORE=/absolute/path/to/core \
OUROBOROS_CHROME_TEST_PYTHON=/absolute/path/to/core/.venv/bin/python \
OUROBOROS_CHROME_TEST_CHROMIUM=/absolute/path/to/Google-Chrome-for-Testing \
OUROBOROS_CHROME_TEST_NODE_ENV=/absolute/path/to/chrome_browser/.ouroboros_env/node \
node tests/chrome_browser/core_integration.mjs
```
