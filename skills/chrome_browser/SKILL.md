---
name: chrome_browser
title: Chrome (shared tab)
description: Work in the owner's own Chrome through Microsoft's official Playwright Extension - the tab they share plus background tabs for the same work, trusted input in background tabs, and handback by plain conversation.
version: 0.2.0
type: script
runtime: node
os: [darwin, linux, win32]
license: MIT AND Apache-2.0
permissions: [net, fs, subprocess]
when_to_use: The owner wants work done in their own signed-in Chrome - in a tab they share and in background tabs for the same work - while they keep using the browser, and takes the page over by saying so when a login, a code or a decision is theirs.
model_experience:
  what_model_sees: Once the owner has imported this skill's server under Settings → Advanced → MCP Servers and enabled it, 25 tools named mcp_chrome_browser__*. Two are this skill's (browser_connect, browser_disconnect) and 23 are the official Playwright MCP 0.0.84 tools (browser_snapshot, browser_click, browser_type, browser_fill_form, browser_tabs, browser_take_screenshot and others). Action results are short Playwright markdown with the Playwright code that ran, the page address and title when they changed, and any dialog. browser_snapshot returns the page's accessibility snapshot inline, and screenshots arrive as image content. Installing the skill alone adds no tool.
  token_effect: About 25 tool schemas wherever the host loads this server's tools. Each snapshot costs what the page's accessibility tree contains; screenshots cost an image.
timeout_sec: 60
scripts:
  - name: setup.mjs
    description: "status - checks the pinned dependency and patch, prints the document for Settings → Import mcp.json and the owner's steps. Writes nothing, starts nothing."
  - name: server.mjs
    description: The MCP stdio server. The host's task MCP holder starts it from the imported Settings entry; it is not run with skill_exec.
install_specs:
  - kind: npm
    package: "@playwright/mcp@0.0.84"
---

# Chrome (shared tab)

One Ouroboros task can work in the tab the owner shares and in background tabs it
opens for the same work. Connecting requires approval and activates the selected
tab; later handback happens by conversation, without a technical handoff lock.
The skill uses Microsoft's unchanged **Playwright Extension** and the official
**Playwright MCP server** (`@playwright/mcp` 0.0.84 with `playwright-core`
1.65.0-alpha-2026-10-30). An exact, pinned patch adapts the server:

1. **Background work.** Tabs the agent opens are created without being activated,
   selecting a tab does not bring it to front, and clicks and checkboxes work in a
   tab that is not visible (see "Input in a background tab").
2. **Explicit connection.** The server never starts, finds or reads a browser or a
   profile. `browser_connect` returns a one-time link; the owner opens it in their
   own Chrome and picks the tab to share.
3. **Handback by conversation.** There is no handoff lock and no turn-taking. When the
   owner wants to act in the page, the agent stops sending actions until they say
   they are done, then reads the page afresh.

## Setup (the owner, once)

1. In the Chrome profile to share, install **Playwright Extension** by Microsoft from
   the Chrome Web Store:
   <https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm>.
   This skill was tested with version 0.4.0 (relay protocol 2).
2. Enable the skill. Ouroboros installs its one declared npm dependency,
   `@playwright/mcp@0.0.84`, which brings exactly `playwright-core` and `playwright`
   1.65.0-alpha-2026-10-30. npm must be on PATH. No browser is downloaded.
3. Run `setup.mjs status` (with `skill_exec`). It checks every pinned file and the
   patch, and prints `mcp_import_document`, ready to paste into Ouroboros:

   ```json
   {"mcpServers": {"chrome_browser": {"type": "stdio",
    "command": "<the node it ran with>", "args": ["<skill>/scripts/server.mjs"],
    "task_session": true, "browser_policy": true}}}
   ```

   Both flags are host features: `task_session` keeps one server process for a task's
   tool loop, so a connection survives between calls; `browser_policy` gives that
   process the host's request guard. On a host without them, or with the guard missing,
   `browser_connect` refuses (`BROWSER_GUARD_REQUIRED`) and nothing is contacted.
   `"ready": true` from `setup.mjs status` covers only the pinned dependency and the
   patch; whether the host runs the server with both features shows when
   `browser_connect` is called.
4. **Import.** In **Settings → Advanced → MCP Servers** choose **Import mcp.json**, paste
   the whole document and choose **Preview**. It reads `Add (disabled)` and `Server ID
   chrome_browser`, and says what both flags mean. Choose **Apply 1 to draft**: nothing is
   saved or started yet. If it says `Not imported: browser_policy, task_session`, this
   Ouroboros cannot run the server yet: stop and report that sentence. `mcp_server_entry`
   in the same report is one saved settings row, for a tool that writes settings itself;
   Import refuses that shape.
5. **Enable.** The new server is off. Turn on its **Enabled** box and **Enable MCP client**.
6. **Save.** Choose **Save Settings**. Ouroboros lists the tools through a short-lived
   server process that connects to no browser. The card may still say **Not listed yet**
   after that background listing finishes: choose **Refresh saved catalog** or reload
   Settings to see the 25 tools. Keep the Server ID: it names every tool. The display
   name can be changed.

## Using it (for the agent)

1. **Connect.** Call `browser_connect`. It answers at once with a one-time link
   (`chrome-extension://mmlmfjhmonkocbjadbfplnigmagldckm/connect.html?...`). Give it
   to the owner only, in this conversation: it is a local capability for their
   browser, not a diagnostic to paste elsewhere.
2. **Approval.** Ask the owner to open their own Chrome, paste the link into a **new
   tab**, pick the tab to share and choose **Allow & select**. Chrome switches to that
   tab once - that is the agreed activation. `browser_tabs` never activates a tab; a popup
   a page opens can (see "Limits you must not hide"). Until the owner says they approved,
   other tools answer `BROWSER_NEEDS_APPROVAL`; nothing is queued and nothing happens later
   by itself.
   Calling `browser_connect` again while waiting returns the same link; it stops working
   when the server session ends or after 15 minutes without approval. If Chrome refuses the
   chosen tab, tools answer `BROWSER_TAB_NOT_ATTACHED` with Chrome's reason; the next
   `browser_connect` says why the last attempt ended and gives a new link.
3. **Work.** `browser_snapshot` returns the page's accessibility tree with element refs;
   act with `browser_click`, `browser_type`, `browser_fill_form` (also for checkboxes and
   radio buttons), `browser_select_option`, `browser_press_key`, `browser_navigate`.
   Action results are short: the Playwright code that ran (for example
   `await page.getByRole('button', { name: 'Save' }).click();`), the page address and title
   when they changed, and any dialog. A failed action returns its error. Call
   `browser_snapshot` to see the page afterwards. Results point at no file: a screenshot, or a
   response body from `browser_network_request` that is an image, is attached as an image;
   another binary body is described and not returned; and console output is read with
   `browser_console_messages`. For other
   pages of the same work, open background tabs with `browser_tabs` action `new` (optionally
   with a `url`); `select` changes which tab the agent acts in, not which tab the owner sees.
4. **Handback.** When the owner says they will do something themselves - a login, a
   code, a payment, a decision - stop sending actions. When they say they are done,
   call `browser_snapshot` before anything else: their typing, login and unsaved text
   are in the page. Never ask for a password or a code in chat.
5. **Dialogs.** An `alert`/`confirm` the page shows blocks the agent's page tools
   until someone answers it. The owner may answer it in Chrome; answer it with
   `browser_handle_dialog` only when that is clearly the agent's to decide.
6. **End.** `browser_disconnect` ends the connection when the work is done or the owner
   asks. Every tab stays open with what was typed into it. A connection that was lost
   is never re-established by itself: ask for a new approval with `browser_connect`.

Only tabs opened with `browser_tabs` action `new` can be closed by the agent. The
owner's shared tab and tabs a page opened stay open.

## What the owner should know

- The connect page warns that the connection reaches the whole browser profile:
  signed-in sessions and cookies of every site, in any tab the agent opens. That is
  accurate. Other existing tabs are not attached, listed or changed.
- The connect page also shows a "bypass" token (`PLAYWRIGHT_MCP_EXTENSION_TOKEN=...`)
  that lets a Playwright client connect without this dialog. This skill never uses it:
  every connection needs the owner's approval. The owner should not share that token.
- The shared tab and the agent's tabs sit in a tab group named "Playwright ·
  Ouroboros". Dragging a tab into the group hands it to the agent; dragging it out, or
  closing the group's last tab, takes it back (the connection ends with the last tab).
- Chrome may show its own debugging notification while the extension is attached.
  Page captures do not show browser chrome. The headed check observed no viewport-height
  change on attachment; the notification's appearance and wording were not qualified.
- If the extension's background worker is stopped while a tab is shared, the connection is
  lost and Chrome keeps the extension attached to that tab, so sharing that same tab again
  fails. Another tab can be shared. No way to share that same tab again was tested. This
  was seen in headless tests after a forced stop; the worker was not stopped by 150
  seconds of idle.

## Errors and what they mean

| Code | Meaning | What to do |
|---|---|---|
| `BROWSER_NOT_CONNECTED` | No connection yet. | `browser_connect`. |
| `BROWSER_NEEDS_APPROVAL` | Link given, owner has not approved. Nothing was done or queued. | Wait for the owner. |
| `BROWSER_ATTACHING` | Approved; setting up. | Call again shortly. |
| `BROWSER_TAB_NOT_ATTACHED` | The owner approved, but Chrome refused to attach the chosen tab (its answer is quoted), for example because the extension still holds an attachment from before its worker restarted. Nothing was opened instead. | Tell the owner. With a new `browser_connect` link they can share another tab. Approving the same tab again fails the same way; no way to share it again was tested. |
| `BROWSER_CONNECTION_LOST` | Extension disconnected, tabs closed, extension stopped or Chrome quit. | New `browser_connect`. |
| `BROWSER_APPROVAL_EXPIRED` | Not approved within 15 minutes. | New `browser_connect`. |
| `BROWSER_DISCONNECTED` | Ended with `browser_disconnect`. | New `browser_connect` if needed. |
| `BROWSER_GUARD_REQUIRED` / `_INVALID` / `_NOT_ARMED` | The host's request guard is not configured (`_REQUIRED`); the host set it up without a usable address or token (`_INVALID`, reported by `browser_connect` before any link is made); or it did not confirm the route after approval (`_NOT_ARMED`). | Report it; it is a setup or host problem. |
| `BROWSER_INPUT_NOT_READY` | Chrome produced no frame for the tab; no input was sent. | Retry once later or report. |
| `BROWSER_INPUT_UNCONFIRMED` | Chrome accepted a click but the page saw no trusted event at the target. It was not sent again. | `browser_snapshot`, then decide. |
| `BROWSER_INPUT_NOT_VERIFIED` | A click went out once, but its delivery could not be checked (the page refused the check, or the document changed, for example because the click navigated). It was not sent again. | `browser_snapshot` to see its effect. |
| `BROWSER_TAB_CHANGED` | The tab this call was checked for closed, or stopped being current, while the host was being asked. Nothing was done, in it or in the tab current now. | `browser_snapshot` to see which tab is current, then decide. |
| `BROWSER_RECORD_ORIGIN_UNKNOWN` | A page made more redirects than are kept for checking (512 addresses per document), so its console entries cannot be judged and are withheld. | Read the page itself; a new document starts fresh. |
| `BROWSER_ARGUMENT_REFUSED` | `filename` (results come inline), `isolatedContext` (owner's profile only) or `_meta` (upstream's output options `cwd`, `raw` and `json`; results always come in the advertised form). | Drop the argument. |
| `CHROME_BROWSER_NOT_INSTALLED` / `_PIN_MISMATCH` / `_PATCH_FAILED` | The dependency is missing or not exactly the pinned files. | Re-enable the skill; check `setup.mjs status`. |

A refusal from the host's guard is shown verbatim (for example
`BROWSER_LOCAL_READONLY_BLOCKED: ...` for `browser_evaluate`). A request the guard
refuses fails in the page with `net::ERR_BLOCKED_BY_CLIENT`, and the tool result lists
`method origin+path: reason` for refusals during that call. The guard also decides
every address `browser_navigate` or `browser_tabs` `new` would open, before it is
opened (a new tab without a `url` opens `about:blank`): the same decision the built-in
browser makes, including for `javascript:`, `data:` and `about:` addresses, which send
no request a route could judge. Upstream Playwright itself refuses `file:` addresses.

## Input in a background tab

Upstream Playwright waits for the target to stay put across two animation frames,
and a hidden tab runs none, so its clicks time out there. This server keeps every other
upstream check - visible, enabled, not inert, scrolled into view, a clickable point,
and the hit-target test that the element itself would receive the input - and replaces
only that wait: the element's box is read twice, 60 ms apart on the server's clock. The
other states are read after that wait, as upstream reads them after its own, so a control
that stays put but turns disabled during it is not clicked.
A covered, moving, disabled or hidden target is not clicked; Playwright retries until
its 5 s action timeout and then reports why. There is no force option.

Before the first input into each document the server captures one frame of the tab and
discards it: a newly loaded hidden renderer in headed Chrome can otherwise accept input
without delivering it. After a click it asks the page whether a trusted pointer event
reached the target; if none did, the click is reported as `BROWSER_INPUT_UNCONFIRMED`
and never repeated. If that check could not be installed or read back (for example the
click navigated), the result says `BROWSER_INPUT_NOT_VERIFIED`: the click went out once
and its delivery is unknown. Checking and unchecking also verify the new state
(upstream). A click goes out at most once per call: if Playwright would send it again in
the same call (it does for a checkbox replaced right after the click that still shows the
old state), the call ends with Playwright's timeout error instead, and the click is not
repeated.

## Request guard

Every request that Playwright routes for the attached tabs - the agent's and the
owner's own browsing in them alike - is decided by the host's guard before it is sent.
This is a floor, not network containment.

What the agent reads follows the built-in browser's rule. Before a page tool acts in a
tab, and before any result is returned, the page's address and every redirect hop
recorded for the documents it now shows, subframes included - whoever navigated it, the
agent or the owner - go to the guard as addresses this task may read. If one is refused,
nothing is done in that page, or the result is replaced by the guard's reason; a tab title
in a tab list is judged with its tab. A hop counts until a new document really replaces the
one it led to: a 204, a download or a redirect that fails leaves the old document in the tab,
and the hop with it, and a new document clears it. This withholds what reaches the agent: a
hop's request has already been sent by then. Moving to another page or tab
(`browser_navigate`, going back, `browser_tabs`) is never refused because of the page being
left.

Closing a tab hands "current" to a neighbour, and the owner or a page can do that at any moment.
An operation on the current tab, including navigation or going back, keeps the tab it was
checked for: if that tab closes during the checks, the call ends with `BROWSER_TAB_CHANGED`
and nothing is done. Navigation can still leave a refused page; it cannot silently switch to
its neighbour. Explicit tab selection, creation and closure remain available. A result is
also judged by the tab the operation worked in and by a listed tab, even if they closed before
it returned. A close before a call makes that call's check and action apply to the neighbour.
The check cannot hold a page still: a document that commits between the check and the action
gets the action, and the result is then judged by that document.

Playwright keeps a tab's console entries, page errors and network requests across its
documents and returns them on request. They are judged by where they came from, not by the
page shown now: a console entry or page error by the document that made it (its address and
hops, and any subframe hop seen under it; a message's script location says nothing about
that), and a request by its own redirect chain and the document and frame that made it. A
tab's own document request belongs to the document it opens. `browser_console_messages` and
`browser_network_requests` leave out what this task may not read and say how many records they
withheld, and `browser_network_request` refuses that one record; the rest of the history stays
readable. Each address is asked once per call and asked again by the next call, so a changed
answer counts at once. A record made before this connection was watching (the moment of
approval), or a request a new tab made before Playwright reported that tab, is taken as the first
document's, however long ago that document was replaced. A record belongs to the document that was current
when Playwright delivered it, in the order of the commits. A page being left can still log or
send something while it unloads (a `pagehide` or `unload` handler, a beacon); if Chrome reports
that after the next document's commit, it is judged as the next document's. Whether Chrome does
so is not shown by this skill's tests, whose stand-in Chrome reports only what a test states: it
is a stated limit, not a known leak.

Not judged as requests:

- redirect hops (Playwright continues them without a route; their addresses are judged
  before output instead, as above, after the request went out);
- WebSockets;
- requests Playwright cannot associate with a frame or worker, and requests Chrome
  sends before the extension attached a tab (a popup's or new tab's first load; for
  such a document, for a document Chrome restores from its back/forward cache, and for the
  owner's tab as it was when shared, only the page's own address is known to the check above,
  not the hops before it);
- service-worker requests: Playwright routes them only for workers it attaches; whether
  the extension relay attaches any was not verified;
- CORS preflights go to the real server unrouted (the actual request is routed). Stock
  Playwright answers them itself with a permissive 204 while any route exists; this
  server's patch keeps the server's own CORS answer;
- what a page sends as it is being left. In one headless check (Chrome for Testing 153, the
  agent navigating from a page the host refuses to an allowed page on another site), three
  beacon POSTs from its `pagehide` and `unload` handlers reached their servers with the old
  page's `Origin`, after the new page's first requests. The guard was not asked, Chrome's
  debugger reported none of them and the record tools returned none. That is one observed
  case, not a proven leak or a rule for other navigations.

Bodies: an upload's file parts are not exposed by Chrome, so such a body is reported as
present but unreadable. Requests in the moment between attaching a tab and installing
the route are not judged; the agent cannot act in that moment.

## Limits you must not hide

- **Bounded browser checks.** Headless checks and one headed session on macOS 26.4 used
  Chrome for Testing 153, disposable profiles, local fixtures and extension 0.4.0 built
  from Microsoft's source. Hidden trusted input, inactive agent-created tabs, image delivery
  and same-page handback were observed. Headless checks also exercised redirect commitment
  and historical-record refusals. Tab-closure races were checked through Playwright's pinned
  consumers with a stand-in for Chrome. Stable Chrome, the pinned 157 browser, the Web Store
  extension binary, Windows/Linux desktops, hardware input and real signed-in sites were
  not tested. The headed evidence includes corrected interpretation of saved focus records,
  not a clean pass of every original test assertion or proof of all possible focus changes.
- **Popups can bring Chrome forward.** In both headless and headed CfT 153, trusted clicks
  in a hidden tab on `target=_blank` and `window.open` opened active tabs and switched the
  simulated owner's tab. In the headed session they also brought the Chrome application
  forward. `browser_tabs new` stayed inactive. This skill does not block page-opened popups,
  change the extension or switch anything back; it cannot promise uninterrupted foreground work.
- **One connection per server process.** A host timeout, restart or cold resume starts
  a new process, which needs a new approval.
- **Navigation is bounded for the host's default timeout.** A navigation gives up at 45 s, plus
  at most 5 s waiting for "load", instead of upstream's 60 s and 5 s, so under the host's default
  60 s `MCP_TOOL_TIMEOUT_SEC` the agent gets Playwright's error on a live connection. If that
  setting is lowered below about 50 s, the host's timeout comes first and the approval is lost.
  Other calls are not covered by this bound.
- **Not offered:** `browser_run_code_unsafe` (it runs arbitrary code inside this server,
  outside the guard) and `browser_close` (replaced by `browser_disconnect`). Local files
  can be uploaded only from this server's private session folder, so in practice not.
- **Child process:** the only one is Playwright's own `sw_vers -productVersion` on macOS
  when it connects (for its user-agent string). The skill starts no browser or helper.
- **Files:** a private session folder in the system temp directory, removed when the
  server exits normally (its input ends, or SIGTERM, SIGINT or SIGHUP). A killed or crashed
  server leaves the folder behind, and nothing removes it later. Upstream keeps a copy of each
  screenshot there, a log of the pages' console messages, and each binary response body
  read with `browser_network_request`. Results do not point at these files: they say the
  screenshot or image is attached, that a binary body is not returned, or that new console
  entries are read with `browser_console_messages`. Also Playwright's temporary artifacts
  folder (removed on disconnect) and, on disconnect, Playwright's attempt to delete a browser
  descriptor it never created under `~/Library/Caches/ms-playwright/b`.
- **Downloads** are not taken over: the connection sets no download folder
  (`connectOverCDP` with `noDefaults` keeps the browser's own download handling), so
  Chrome saves a page's download where it saves the owner's downloads, and the session
  folder never holds it. In the headless QA, a download the agent's click started and
  one the owner started both landed in Chrome's default Downloads folder and stayed after
  the session ended. The result of the click does not mention the download.

## Provenance

See [`ATTRIBUTION.md`](ATTRIBUTION.md) and [`pins.json`](pins.json). The patch and its
licence notices are in [`patches/`](patches/).
