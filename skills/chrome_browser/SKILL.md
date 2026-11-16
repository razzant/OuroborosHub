---
name: chrome_browser
title: Chrome (shared tab)
description: Work in the owner's own Chrome through Microsoft's official Playwright Extension - the tab they share plus background tabs for the same work, trusted input in background tabs, and handback by plain conversation.
version: 0.4.0
type: script
runtime: node
mcp_servers: [{"id":"chrome_browser","title":"Chrome (shared tab)","runtime":"node","entry":"scripts/server.mjs","task_session":true,"browser_policy":true}]
os: [darwin, linux, win32]
license: MIT AND Apache-2.0
permissions: [net, fs, subprocess]
when_to_use: The owner wants work done in their own signed-in Chrome - in a tab they share and in background tabs for the same work - while they keep using the browser, and takes the page over by saying so when a login, a code or a decision is theirs.
model_experience:
  what_model_sees: Once the owner chooses Connect tools on this skill (or asks Ouroboros to connect it; older hosts import the server by hand), 25 tools named mcp_chrome_browser__*. Two are this skill's (browser_connect, browser_disconnect) and 23 are the official Playwright MCP 0.0.84 tools (browser_snapshot, browser_click, browser_type, browser_fill_form, browser_tabs, browser_take_screenshot and others). Action results are short Playwright markdown with the Playwright code that ran, the page address and title when they changed, and any dialog. browser_click also takes captureDownload; where the host accepts binary resources, copies of the HTTP responses its tab gets while that click runs can come back as embedded resources, listed apart from Chrome's own downloads, and the result says why when one does not; on other hosts it refuses before clicking. browser_snapshot returns the page's accessibility snapshot inline, and screenshots arrive as image content. Installing the skill alone adds no tool.
  token_effect: About 25 tool schemas wherever the host loads this server's tools. Each snapshot costs what the page's accessibility tree contains; screenshots cost an image.
timeout_sec: 60
scripts:
  - name: setup.mjs
    description: "status - checks the pinned dependency and patch and describes Connect tools. Also prints the manual MCP import fallback for older hosts. Writes nothing, starts nothing."
  - name: server.mjs
    description: The MCP stdio server. The host's task MCP holder starts it from the connected Settings entry; it is not run with skill_exec.
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
   This release was tested with the Web Store build of version 0.5.0 (relay protocol 2);
   earlier releases with 0.4.0 built from Microsoft's source. The server does not check
   the installed version; the extension's connect page refuses a protocol it does not support.
2. Enable the skill and approve its requested permissions. Ouroboros installs the
   pinned dependency; hosts with automatic npm preparation also supply Node/npm if
   absent. This downloads no browser. A dependency failure is not a connected skill.
3. Choose **Connect tools** on the skill card, or ask Ouroboros **“Connect Chrome's
   tools”**. Confirm the displayed changes. Ouroboros configures the server and lists
   its tools; you do not copy JSON, run commands or edit MCP switches. If MCP was off,
   the confirmation names other saved servers that would also start. An existing row
   with the same Server ID, such as one imported by hand for an earlier version, is
   reported, never silently replaced: remove that row under **Settings → Advanced → MCP
   Servers**, save, then choose Connect tools again.
4. When a task needs Chrome, open its one-time link in the intended profile, select
   the tab and choose **Allow & select**. This approval is separate from installing
   the tools and remains necessary for each new server session.

The saved connection follows the installed skill and runtime paths. Disabling or
removing the skill leaves its MCP settings row but makes the bound tools unavailable;
turn that row off under Settings → Advanced → MCP Servers if you want it off too.
An accepted skill update can reuse the connection; an unaccepted update cannot run.
Connect tools needs a host that supports the `mcp_servers` manifest declaration.
Older hosts may install this skill without offering that action; they need npm on
PATH and use the manual fallback below. Do not claim the browser is connected from a dependency or catalog
check alone.

### Older hosts: manual fallback

Ask Ouroboros to run `setup.mjs status` (with `skill_exec`). It checks the pinned
files and patch, and prints `mcp_import_document`, derived from this manifest:

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
`browser_connect` is called. This fallback reads this skill's single-line JSON
`mcp_servers` declaration, not general YAML. An unsupported format or invalid field
produces `CHROME_BROWSER_DECLARATION_INVALID` with a reason and nothing to import;
this does not imply that the host rejects every other YAML spelling.

1. **Import.** In **Settings → Advanced → MCP Servers** choose **Import mcp.json**, paste
   the whole document and choose **Preview**. It reads `Add (disabled)` and `Server ID
   chrome_browser`, and says what both flags mean. Choose **Apply 1 to draft**: nothing is
   saved or started yet. If it says `Not imported: browser_policy, task_session`, this
   Ouroboros cannot run the server yet: stop and report that sentence. `mcp_server_entry`
   in the same report is one saved settings row, for a tool that writes settings itself;
   Import refuses that shape.
2. **Enable.** The new server is off. Turn on its **Enabled** box and **Enable MCP client**.
3. **Save.** Choose **Save Settings**. Ouroboros lists the tools through a short-lived
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
   Keep the **same task and browser tool session alive** while the owner approves. Use a
   warm wait supported by the current role. A child that cannot wait for the owner should
   relay the pending step to its parent without finalizing; the root owner-facing task
   waits where supported. If no warm wait is available, explain that ending the task
   ends this link. A later task needs a new connection and approval.
3. **Work.** `browser_snapshot` returns the page's accessibility tree with element refs;
   act with `browser_click`, `browser_type`, `browser_fill_form` (also for checkboxes and
   radio buttons), `browser_select_option`, `browser_press_key`, `browser_navigate`.
   Action results are short: the Playwright code that ran (for example
   `await page.getByRole('button', { name: 'Save' }).click();`), the page address and title
   when they changed, and any dialog. A failed action returns its error. Call
   `browser_snapshot` to see the page afterwards. Results point at no file: a screenshot, or a
   response body from `browser_network_request` that is an image, is attached as an image;
   another binary body of at most 20 MiB is attached as an embedded resource (an MCP blob with
   its media type, named in the text with its sha256) where the host accepts binary resources
   (see "Downloads"), and otherwise, or when larger, is described and not returned; and console
   output is read with `browser_console_messages`. To give a page files,
   use `browser_file_upload` once its file chooser is open, or `browser_drop`, with `paths` or
   `files` (`{path, root, bucket, skill_name}` as `read_file` takes them): the host admits and
   copies each file, and the page gets only that copy's bytes and the original file name. The host
   checks the file, not its recipient: take a fresh `browser_snapshot` and check the page against
   the task's intended recipient before sending. To get the file a link or form downloads, call
   `browser_click` with `captureDownload: true` (see "Downloads"). For other
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
   After reapproval, take a fresh `browser_snapshot` and reassess the page. A command
   sent before a lost reply may already have acted; never replay it automatically.

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
| `BROWSER_CONNECTION_LOST` | The generation, first observed closing layer, its time and a finite WebSocket code when available are reported. The extension's exact peer reasons `User disconnected` and `All controlled tabs detached` (the same strings in 0.4.0 and in the 0.5.0 Web Store build) have safe names; they do not establish the underlying trigger. A preceding debugger detach is shown separately; a detach or tab removal alone is not transport loss. Other peer supplied reasons outside a short exact allowlist are shown as unknown. | New `browser_connect`, then fresh `browser_snapshot` and reassess. Do not replay an action whose reply was lost. |
| `BROWSER_CONNECTION_CHANGED` | A different connection generation replaced the one used by this call. The result distinguishes refusal before dispatch from a dispatched call whose effects require checking. | After approval take a fresh snapshot; never replay an earlier action automatically. |
| `BROWSER_APPROVAL_EXPIRED` | Not approved within 15 minutes. | New `browser_connect`. |
| `BROWSER_DISCONNECTED` | Ended with `browser_disconnect`. | New `browser_connect` if needed. |
| `BROWSER_GUARD_REQUIRED` / `_INVALID` / `_NOT_ARMED` | The host's request guard is not configured (`_REQUIRED`); the host set it up without a usable address or token (`_INVALID`, reported by `browser_connect` before any link is made); or it did not confirm the route after approval (`_NOT_ARMED`). | Report it; it is a setup or host problem. |
| `BROWSER_INPUT_NOT_READY` | Chrome produced no frame for the tab; no input was sent. | Retry once later or report. |
| `BROWSER_INPUT_UNCONFIRMED` | Chrome accepted a click but the page saw no trusted event at the target. It was not sent again. | `browser_snapshot`, then decide. |
| `BROWSER_INPUT_NOT_VERIFIED` | A click went out once, but its delivery could not be checked (the page refused the check, or the document changed, for example because the click navigated). It was not sent again. | `browser_snapshot` to see its effect. |
| `BROWSER_TAB_CHANGED` | The tab this call was checked for closed, or stopped being current, while the host was being asked. Nothing was done, in it or in the tab current now. | `browser_snapshot` to see which tab is current, then decide. |
| `BROWSER_RECORD_ORIGIN_UNKNOWN` | A page made more redirects than are kept for checking (512 addresses per document), so its console entries cannot be judged and are withheld. | Read the page itself; a new document starts fresh. |
| `BROWSER_FILE_INTAKE_INVALID` / `_MISMATCH` / `_NAME_UNSUPPORTED` | An empty string in `paths`, or a NUL in a path or root (`_INVALID`, not sent to the host; more than 16 files or an empty `files[].path` are refused earlier by the tool's schema, and naming no file is upstream's own cancel or data-only drop); or the host's retained copy is not the size or sha256 it stated, or cannot be read (`_MISMATCH`); or a file's own name has a backslash or a control character, which this server does not give a page (`_NAME_UNSUPPORTED`; the file and its name are not changed). The host's own refusal of a file is shown verbatim. The page got nothing, and an open file chooser stays open. | Name readable files, at most 16 and 20 MiB together; report a mismatch; for an unsupported name, ask the owner how to rename or replace the file. |
| `BROWSER_GUARD_PROTOCOL` | The host's file-intake response is malformed. No file bytes reached the page. | Report the protocol mismatch; do not bypass admission. |
| `BROWSER_FILE_CHOOSER_GONE` | The file chooser closed while the host was reading the files. The page got nothing. | `browser_snapshot`, then open the chooser again if needed. |
| `BROWSER_DOWNLOAD_CAPTURE_UNSUPPORTED` | `captureDownload` on a host that did not answer `capabilities.embedded_resources: true` when the guard was armed (an older Ouroboros, or a session no task owns). Nothing was clicked or changed. | Click without `captureDownload`; say that whether Chrome saved a file is unknown until the owner checks their Chrome downloads. |
| `BROWSER_DOWNLOAD_CAPTURE_BUSY` / `_UNAVAILABLE` | `captureDownload` while another capturing click runs on this connection (`_BUSY`); or the tab could not be matched to the extension's tab, or Chrome refused the response-stage interception, whose answer is quoted (`_UNAVAILABLE`). Nothing was clicked. | Wait for the other call, or click without `captureDownload` as above. |
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

## Downloads

Chrome saves a download where the owner's Chrome saves downloads (or asks, if they set it to);
this server cannot change that, and Playwright never sees the download. When the agent expects a
click to start one - a link or a form that returns a file - it calls `browser_click` with
`captureDownload: true`. A copy can be returned only to a host whose guard, when armed, answered
`capabilities.embedded_resources: true` (a task owns the session and the host takes embedded
resources as binary output; that is support for the format, not a promise each copy is stored).
Without it - an older Ouroboros, or a session no task owns - the call refuses with
`BROWSER_DOWNLOAD_CAPTURE_UNSUPPORTED` before anything is changed or clicked, and a binary
`browser_network_request` body is described, as before, instead of attached. For that click only,
in the tab it acts in:

- Chrome pauses that tab's document responses once more, after their headers arrive (Fetch,
  response stage, on the tab's one debugger session, which Playwright shares). The patterns Chrome
  holds there are Playwright's own, unchanged, plus that one; afterwards Chrome gets Playwright's last
  command back exactly. Requests that Playwright routes still reach the host's guard at the
  request stage, including while Playwright changes its own interception; the unrouted cases
  below remain exceptions. If Chrome is still handing over a body when the call ends, the tab keeps
  that pattern with Playwright's latest ones until the body has come (CDP leaves undefined what
  disabling Fetch does before then); other document responses there are passed on unread meanwhile,
  and no call or disconnect waits for it.
- A successful response that declares its length, at most 20 MiB, is not sent in chunks
  (`Transfer-Encoding` overrides any `Content-Length`) and is not content-encoded is read once
  (`Fetch.getResponseBody`), then the same response is continued. It is never fetched again, failed or
  fulfilled, so Chrome gets the server's one response. Any other response is continued
  unread, since reading it cannot be bounded beforehand (Chrome hands over a whole body at once). That
  bounds what is asked for, not what a server sending more than it declared makes Chrome hold: what
  Chrome hands over is measured again, and more than 20 MiB is dropped and never returned.
  Bodies are buffered whole, not streamed. Pages loaded meanwhile are read too,
  since a file without `Content-Disposition` looks the same at that point: at most 64 responses of
  20 MiB each (1.25 GiB) in the worst case. A body is dropped, its size and sha256 kept, as soon as
  it cannot be a copy, the rest once the call has answered; a read that ends after the call stopped
  waiting keeps nothing.
- What comes back is a **copy of the HTTP response**, not Chrome's file. A response is copied when
  its request began while the call ran (its first address was seen, so every address on the way is
  known) and Chrome then received it and ended that request without showing it as a page
  (`Network.responseReceived`, then `Network.loadingFailed` with `net::ERR_ABORTED`, by that request's
  id). Chrome reports that when it hands a response to its downloads, also one it then refuses to
  save, so **a copy can come back although Chrome saved no file**. A response Chrome
  shows as a page, drops while it is held or fails otherwise is not copied, nor one whose end Chrome
  has not reported within the wait. Two such responses of one click are two copies,
  whatever their addresses, names or sizes. The address and every hop are judged by the guard, as a
  page's are, before anything of a response is returned.
- The server waits at most 15 s, less only once Chrome has ended each document request begun while
  the call ran, a response was passed on unshown or a download began, and as many downloads as such
  responses have begun and ended: a click that only loads a page waits all 15 s.

The result lists each response (method, status, address, media type) with its **copy**
(`N bytes, sha256 ...`, attached as an embedded resource `chrome-browser://download/<n>/<sha256>`
with its media type; an `image/*` type is labelled `application/octet-stream`) or why it was not
copied, and apart from them **Chrome's downloads** in that tab while the call waited, by Chrome's
name and address: `completed` or `canceled` as Chrome reported it, `still in progress` when the call
returned, or `unknown` when Chrome reported nothing. Chrome does not report which response a
download comes from, so no download is tied to a copy, even with one of each, and where Chrome
saved a file is not known here. A response still being read when the wait ends is said
apart (Chrome gets it once read; whether the click made it is not known). After a click that did not
fail, a result without a copy says it was sent, may still lead to a page or a download, and must not
be repeated; after a failed click the result is the error, then what had already come if anything had,
and it does not say whether the click went out.

The copies travel base64-encoded in one JSON-RPC line, about 4/3 of their size, and a line holds at
most the runtime's longest string (536,870,888 characters with 64-bit Node 24, a little over 19 copies
of 20 MiB): copies past that are listed as not attached, with size and sha256, and not returned. The
stdio client of the MCP TypeScript SDK in the pinned playwright-core refuses lines over its default
`maxBufferSize` of 10 MiB (a 12 MiB copy closed this skill's test connection); Ouroboros reads with
the Python MCP SDK's stdio client, which has no such byte bound; that does not promise how fast, or
whether, a large result arrives: the host's own transport timeout can still end the call first. Other
clients were not checked.

Not copied: downloads the page makes itself (`blob:`, `data:`; listed as Chrome reports them, no page
instrumentation), downloads in another tab or a popup the click opened (named when Chrome says
so), responses seen only in a cross-origin frame's or a service worker's session, a response whose
request began before the click, responses over the bounds above, and a link with the `download`
attribute: Chrome for Testing 156 reports its response with no network request, so neither its
addresses nor what became of it can be known. That request also carries no network
id, which Playwright continues without its route (pinned `coreBundle.js`,
`CRNetworkManager._onRequestPaused`), so the host's guard is not asked about it before it is sent;
that is not new with this capture, and only its address is judged before anything is returned. A
response the page or the owner causes in the same tab during that click (a timer, a refresh, their
own click) cannot be told apart from the agent's, and a navigation the page replaced after its
response came is copied too; each copy names its own request, address and type.
Real browser: a headed macOS run on 15 Nov 2026 (Chrome for Testing 157.0.8103.50,
Web Store extension 0.5.0 loaded unpacked) exercised this implementation with the host's actual
file intake and artifact projection, a synthetic task context and local fixtures. Upload, chooser
refusal then retry, drop, editor save/reopen, GET, POST, cookie session, redirect, 12 and 20 MiB
copies and a sandboxed link Chrome refused to save passed byte checks. A second same-address
request in another frame began during the first response's body read; the later HTML was not
returned as its file. Chunked, gzip, 21 MiB, `download`-attribute and `blob:` got no copy. The run
passed all 12 scenarios; its source responses, retained artifacts and request timings were checked
separately. Earlier failed checks and their offline re-judgment remain in the private test record.
This is not a full Main-loop or live Safety-model check. Stable Chrome, a Web Store install,
Windows, Linux, owner credentials and real third-party sites were not tested.

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
hop's request has already been sent by then. Tab-list output is checked by the titles and addresses it contains, including empty-title tabs and tabs retained for that call after teardown or reconnection. Moving to another page or tab
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
  consumers with a stand-in for Chrome. Those checks did not use stable Chrome, the pinned 157
  browser or the Web Store extension build; the download checks above used the Web Store build
  0.5.0. Windows/Linux desktops, hardware input and real signed-in sites were not tested. The
  headed evidence includes corrected interpretation of saved focus records, not a clean pass
  of every original test assertion or proof of all possible focus changes.
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
  outside the guard) and `browser_close` (replaced by `browser_disconnect`).
- **Files for a page come only through the host.** `browser_file_upload` and `browser_drop`
  ask the host's file intake (at most 16 files and 20 MiB per call) and give the page the
  retained copies, never a path; this server opens no path the agent names. If the host does
  not answer the intake in the agreed form, nothing is given and the chooser stays open. Real
  headless extension checks covered Unicode/multiple files, refused-then-valid chooser reuse,
  page drop, and a PNG saved and reopened in TinyMCE 8.10.1. Its HTTP ZIP exports, captured under the
  earlier rule that tied a copy to one download, became Core task artifacts and remained readable after disconnect. These file operations require a Core
  with task-bound `file_intake` and embedded-resource artifact registration; older Core versions
  do not gain those capabilities from installing this skill: their guard refuses the intake with its
  own reason (shown verbatim; nothing reaches the page), and captures refuse before the click.
- **Child process:** the only one is Playwright's own `sw_vers -productVersion` on macOS
  when it connects (for its user-agent string). The skill starts no browser or helper.
- **Files:** a private session folder in the system temp directory, removed when the
  server exits normally (its input ends, or SIGTERM, SIGINT or SIGHUP). A killed or crashed
  server leaves the folder behind, and nothing removes it later. Upstream keeps a copy of each
  screenshot there, a log of the pages' console messages, and each binary response body
  read with `browser_network_request`. Results do not point at these files: they say the
  screenshot or image is attached, that a binary body is attached as a resource or is not
  returned, or that new console
  entries are read with `browser_console_messages`. Also Playwright's temporary artifacts
  folder (removed on disconnect) and, on disconnect, Playwright's attempt to delete a browser
  descriptor it never created under `~/Library/Caches/ms-playwright/b`.
- **Downloads** are not redirected: the connection sets no download folder
  (`connectOverCDP` with `noDefaults` keeps the browser's own download handling), so
  Chrome saves a page's download where it saves the owner's, never in the session folder;
  in the headless QA such files stayed after the session ended. A click's result mentions
  downloads only with `captureDownload` (see "Downloads").

## Provenance

See [`ATTRIBUTION.md`](ATTRIBUTION.md) and [`pins.json`](pins.json). The patch and its
licence notices are in [`patches/`](patches/).
