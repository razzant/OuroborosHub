# chrome_browser wire contract (protocol `ouroboros_chrome`, version 1)

Three hops. The Node executor and the extension in this package implement
exactly what is written here.

```text
Ouroboros host ──MCP stdio (JSON-RPC)──▶ Node executor ──loopback WebSocket──▶ MV3 extension ──chrome.debugger──▶ one tab
      ▲                                      │
      └──── guard: private Unix socket ◀─────┘   (every primitive, every paused request, every document commit)
```

One executor per task attempt, one lease at a time, POSIX host. Windows is not
claimed.

## 1. Host → executor

### 1.1 Launch and environment

The host starts the declared script with the declared runtime and exactly one
argument: `node <absolute path>/scripts/mcp_server.mjs`.

| Variable (set by the host) | Meaning |
| --- | --- |
| `OUROBOROS_BROWSER_PROTOCOL` | Must equal `ouroboros_chrome`; otherwise every tool refuses, and no port is bound and no credential read. |
| `OUROBOROS_BROWSER_GUARD` | Path of the host's private Unix socket. Absent: no lease is attached and no primitive is dispatched. |
| `OUROBOROS_BROWSER_OWNER_TASK_ID` | Required; reported verbatim as `owner.task_id`. Absent or empty: every tool refuses with `BROWSER_POLICY_UNAVAILABLE`. |
| `OUROBOROS_BROWSER_OWNER_ATTEMPT` | Required; reported verbatim, as a string, as `owner.attempt`. Absent or empty: every tool refuses with `BROWSER_POLICY_UNAVAILABLE`. |

The executor finds its private state directory (the pairing credential, §3.2)
in this order: `OUROBOROS_CHROME_STATE_DIR`, `OUROBOROS_SKILL_STATE_DIR`,
then `<skill dir>/../../../state/skills/<skill dir name>` when that `state`
directory exists. **Open point for the host:** the bound MCP process does not
receive `OUROBOROS_SKILL_STATE_DIR` today; `skill_exec` scripts do. The
override names an installation's (or a test's) own directory: it is chosen by
whoever sets the executor's environment, and the package does not confine it.
`setup.mjs status` prints the MCP server entry (`id`, `name`, `enabled`,
`command` — the absolute path of the Node that ran it, 20 or newer — `args`,
`env`).

`OUROBOROS_CHROME_EXECUTE_TIMEOUT_MS` (default `120000`) bounds one
`browser_execute`.

### 1.2 MCP

JSON-RPC 2.0, one message per line. Methods: `initialize`,
`notifications/initialized`, `ping`, `tools/list`, `tools/call`. A line that
is not JSON is answered `-32700`; JSON that is not an object (`null`, a
number, a string, a batch array) is answered `-32600` with `id:null`. Requests are
served concurrently. `initialize` and `tools/list` open no socket and read no
credential; the loopback port is bound on the first `tools/call` of a host
that passed the §1.1 checks, once, whatever number of calls arrive together.

`browser_connect`, `browser_resume`, `browser_action` and `browser_execute`
need the host-generated `params._meta.ouroboros_invocation_id` (non-empty
string) and are refused undispatched without it. `browser_state`,
`browser_handoff` and `browser_disconnect` ask the guard nothing and do not
read it.

| Tool | Arguments (exact) | Result |
| --- | --- | --- |
| `browser_state` | `{}` | Exactly one `text` item: the state object (§1.3). |
| `browser_connect` | exactly `{"tab_id": "<id>"}` or exactly `{"url": "<absolute url>"}` | One `text` item: post-call state. |
| `browser_action` | `{"operation": "<name>", "args": {…}}` — a §2 operation, `sequence` or `cached_text` | One `text` item: receipt (§1.6); a screenshot adds one `image` item. |
| `browser_execute` | `{"code": "<script>"}` | One `text` item: receipt (§1.6); screenshots add `image` items. |
| `browser_handoff` | `{}` | One `text` item: post-call state. Out of band (§3.4). |
| `browser_resume` | `{}` | One `text` item: post-call state. |
| `browser_disconnect` | `{}` | One `text` item: post-call state. Out of band (§3.4). |

A refused or failed call has `isError: true`; its text item is JSON with
`error: {code, message}` and, where it exists, `state`. That `state` waits for
the same barrier as `browser_state` (§1.4, Ordering).

* `{"tab_id"}` selects a tab the owner shared in the extension popup. The
  executor knows only ids the owner shared; any other id is
  `BROWSER_TAB_NOT_APPROVED`. The extension never lists tabs.
* `{"url"}` makes the extension create its own background tab
  (`chrome.tabs.create({url, active: false})`), wait for it to load, and
  attach. The host admits the URL before dispatch; the landing URL is in the
  connect callback (§1.4). **The requests of that first load — redirect hops
  and subresources included — happen before the debugger is attached and are
  not sent to the guard.** While the agent holds it, the tab is closed again on
  `browser_disconnect`, executor exit, transport loss, a refused connect and a
  refused document (`navigation_refused`). Once it has been the owner's — a
  `browser_handoff`, *Stop sharing*, *Forget pairing* or *Cancel* on Chrome's
  bar — the package never closes it: not after `browser_resume` and a later
  `browser_disconnect`, not on executor exit, transport loss, a restart of the
  extension's worker or *Forget pairing*. Closing it is the owner's choice.

### 1.3 State object

```json
{
  "protocol": "ouroboros_chrome",
  "version": 1,
  "owner": {"task_id": "…", "attempt": "…"},
  "executor_generation": 193847562910,
  "connected": true,
  "lease": {
    "lease_id": "l3f9c…", "generation": 1, "tab_id": "1234567",
    "document_id": "A1B2…", "url": "https://example.org/", "guard_armed": true
  },
  "closure": {"revoked": false, "detached": false}
}
```

* `executor_generation` — integer ≥ 0, random per Node process (and so per
  script realm).
* `connected` is `true` only while a debugger attachment is live and armed.
  `connected:false` ⇒ `lease:null`. A never-connected executor answers
  `connected:false, lease:null, closure:{revoked:false,detached:false}`.
* `lease.lease_id` (string) and `lease.generation` (integer ≥ 0, strictly
  increasing within an executor) change together on **every** lease
  transition: `browser_connect`, `browser_resume`, and every cross-document
  commit of the main frame, whatever caused it (explicit `navigate`, a click,
  the page itself). `tab_id` and `executor_generation` stay.
* `lease.tab_id` — Chrome's tab id as a decimal string.
* `lease.document_id` — CDP loader id of the committed main document. A
  same-document navigation (fragment, `history.pushState`) changes only
  `url`; `lease_id`, `generation` and `document_id` stay. Core refreshes this
  observed URL under the current actor policy without replacing the lease.
* `lease.url` — committed main-frame URL, never truncated.
* `lease.guard_armed` — request interception is enabled on this attachment and
  a guard socket is configured. A lease is never reported without it.
* `closure.revoked` — the owner ended it: *Stop sharing* or *Forget pairing*
  in the popup, or *Cancel* on Chrome's debugging bar. Sticky for this executor.
* `closure.detached` — the extension confirmed that the last lease's debugger
  is detached and nothing is attached now. `true` after `browser_handoff`,
  `browser_disconnect`, an owner revocation and a debugger-side detach;
  `false` again after the next successful `browser_connect`/`browser_resume`.
  A lost transport is **not** a confirmation: it leaves `detached:false`
  with `transport_lost:true` (below) and the executor refuses to attach again.

Additional members (informative):

```json
"phase": "idle | connected | handoff | closed",
"pairing": {"paired": true, "port": 47831, "listener": "idle | listening | busy | unconfigured | released",
            "credential_present": true, "origin_pinned": true, "failed_attempts": 0},
"approved_tabs": [{"tab_id": "1234567"}],
"handoff": {"tab_id": "1234567", "previous_lease_id": "l3f9c…"},
"transport_lost": false,
"last_closure": {"reason": "handoff | disconnect | refused | owner_revoked | canceled_by_user | target_closed | transport_lost | navigation_refused | shutdown | replaced_with_devtools",
                 "debugger_detach_ack": true, "detail": "…"}
```

No credential, proof or nonce appears in any tool result, log line or guard
question.
An unconfigured listener start (missing pairing file or pinned origin, or a
start error before a listener exists) can be retried by a later tool call in
the same executor generation. Concurrent callers share one start attempt.
A listening listener is not rebound. A busy port (`BROWSER_PORT_BUSY`) leaves
no listener behind: the next tool call tries the port again. When a lease ends
with no handoff to resume (disconnect, a refused document, a refused
connection) the executor closes its listener (`pairing.listener:"released"`)
so another task's executor can bind the port; the next connect binds it again.
Revoked and transport-lost executors do not retry an unconfigured start or
replay a browser action.

### 1.4 Guard socket

One question per connection: connect, write one JSON object and `\n`,
half-close, read one JSON object and `\n`. Only `{"block": ""}` grants. Any
other answer, a malformed one, a closed connection or 30 s of silence refuses.
`state` is always the seven contract members of §1.3 as they are at that
moment, never the additional members.

**Primitive** — before each step is sent to the extension:

```json
{"kind": "primitive", "invocation_id": "…", "step_id": 1,
 "operation": "click", "args": {"selector": "#go"}, "state": {…}}
```

`args` is exactly the object the caller passed.

**Step numbers.** Every question of one invocation — primitive, request and
document alike — gets the next `step_id`: a positive integer, strictly
increasing in the order the questions are asked. All questions of the executor
go through one queue, one at a time, and each is built when its turn comes, so
its `state` is current. Requests and documents also carry `causal_step_id`,
the `step_id` of the primitive that was running (or ran last) in that
invocation, or `null`; it is information, not an ordering key.

**Connect / resume** — after the debugger is attached and armed, before the
tool returns. `state` holds the newly armed lease:

```json
{"kind": "primitive", "invocation_id": "…", "step_id": 1,
 "operation": "connect", "args": {"url": "https://example.org/"}, "state": {…}}
```

`operation` is `connect` or `resume`; `args` equals the tool arguments
(`{}` for resume). The call is an invocation of its own: this question is its
step 1, and requests paused before the tool returns continue its numbering. A
refusal releases the lease at once (and closes a tab the executor created),
also when the tab has committed another document since.

The lease is adopted in the turn the extension's answer is read, and only
when that answer names the `lease_id` and `generation` the executor asked
for. `state` in this question is the lease as it was armed; a document that
commits right after is a **Document change** question behind it, with that
lease as `previous`. A document that commits while the tab is still being
attached is not a transition: the answer reports it as the document of the
lease that was asked for. Requests Chrome pauses before the executor has
adopted the lease are refused.

An owner's revocation (*Stop sharing*, *Forget pairing*, *Cancel*) or a lost
tab that reaches either side while the attachment is pending is final: no
lease is adopted, the host is asked nothing, an attachment the extension
reported all the same is released, and the call fails with
`BROWSER_SESSION_REVOKED` (`closure.revoked:true`, sticky) or
`BROWSER_SESSION_DETACHED`. `closure.detached` is what the extension
confirmed last, not an assumption. A tab the extension opened for that
attachment is the owner's from the revocation on (§1.2): *Forget pairing*,
which also drops the socket, does not close it.

**Paused request** — while Chrome holds the request:

```json
{"kind": "request", "invocation_id": "…", "step_id": 2, "causal_step_id": 1,
 "request_id": "4:interception-job-12.0", "url": "https://…", "method": "POST",
 "post_data": "a=1", "has_post_data": true, "complete": true,
 "resource_type": "Document", "is_main_frame": true, "target_tab_id": "1234567",
 "state": {…}}
```

* `invocation_id` is the invocation open when the request is judged; with
  none open, `invocation_id`, `step_id` and `causal_step_id` are `null`.
* `complete` is `true` only when the whole body is in `post_data`, or there is
  no body (`post_data: null`). File parts, non-UTF-8 bytes and a body Chrome
  does not hand over are `complete:false`.
  `has_post_data` is an explicit CDP-derived presence fact: `false` only for
  no body, `true` for body-bearing requests, `null` for malformed/unknown facts.
* `resource_type` is CDP's `Fetch.requestPaused.resourceType`.
  `is_main_frame` is `true` only when the paused request's CDP `frameId`
  equals the leased tab's main frame id; it is never inferred from the URL.
  `target_tab_id` is the leased tab. All three are always present.
* `request_id` is `<generation the debugger was attached at>:<CDP request id>`:
  unique within an executor, also after `browser_resume`.
* `url` is the request URL with its fragment, if any: for a `Document`
  request it is exactly the `lease.url` the document will commit under.
* `resource_type` is what CDP reports; `fetch()` and `XMLHttpRequest` both
  arrive as `XHR`.
* A redirect hop is a new paused request with its own `request_id`.
* A refused `Document` request is answered `204`, so Chrome abandons the
  navigation and keeps the current document; any other refused request fails
  with `BlockedByClient`.

With no active invocation, a compatible host may admit only a current armed
lease's complete bodyless GET/HEAD subresources (`has_post_data:false`), still
checking current task holds, binding and URL/request policy at arrival and after
assessment. All Documents, bodies, unknown resource types and stale non-null
invocation IDs refuse. A main-frame-origin Fetch/XHR remains a subresource;
`is_main_frame` describes its origin frame, not navigation. No idle callback
adopts a document or authorizes another action. GET/HEAD are not a semantic
non-mutation guarantee. Older invocation-only hosts continue to refuse idle reads.

**Document change** — on the actual `Page.frameNavigated` of the main frame:

```json
{"kind": "document", "invocation_id": "…", "step_id": 3, "causal_step_id": 1,
 "request_id": "4:interception-job-12.0",
 "previous": {"lease_id": "l3f9c…", "generation": 1, "document_id": "A1B2…"},
 "state": {…new lease…}}
```

* Emitted once per commit. `state` is the new lease (new `lease_id`, higher
  `generation`, new `document_id`, committed `url`; same owner, executor and
  tab). The earlier `Document` request question carried the old lease.
* `step_id` is the next step number of the open invocation.
  `invocation_id`, `step_id` and `causal_step_id` are `null` when none is
  open.
* `request_id` is the granted main-frame `Document` request this commit came
  from. The match is CDP's own: the paused request's `networkId` equals the
  committed frame's `loaderId` (after redirects: the last granted hop of that
  network request). It is `null` when Chrome showed no such request (for
  example `about:blank`); the URL is never used to guess it.
* Ordering: the question is asked before the step that caused the commit
  returns its result, before any later primitive or request question (they
  wait for the answer), and before `browser_state` shows the new lease
  (`browser_state` waits too). Nothing adopts an unwitnessed lease. The wait
  is a barrier, not a snapshot: whoever reports state — a control tool, a
  receipt, an error, `browser.state()` in a script — waits until the queue has
  no question left, including one that joined it during the wait.
* A refusal releases the lease, also when the page has committed a later
  document meanwhile: the debugger is detached and the state becomes
  `connected:false`, `last_closure.reason:"navigation_refused"`. The commit
  has already happened: the page stays on the refused document (a tab the
  agent still held is closed, §1.2). The invocation the question belongs to
  fails with the guard's code — its receipt is `ok:false` even when every
  step answered or the script caught the error — and the step that was running
  keeps the outcome Chrome reported (`dispatched`, `confirmed` or `unknown`).
  A commit with no open invocation is refused the same way, with no receipt
  to carry it.

### 1.5 Invocations and the script realm

* One `vm` context lives as long as the process. It is not a security
  boundary; the host admits `browser_execute` only under unrestricted agency.
* The script body runs as `async (browser, vars, console, sleep) => { … }`.
  `vars` persists between invocations. `browser` is a new object per
  invocation, closed over that invocation; it is not a global. Every member
  of `browser` returns a promise, `browser.state()` included.
* `sleep(ms)` (at most 30 s) rejects with `BROWSER_INVOCATION_CLOSED` when the
  invocation is closed. The executor observes that rejection itself, so a
  sleep, or a step, the script never awaited does not end the process; a
  caller that awaits it still sees it. A refusal of the executor that reaches
  a promise the script dropped is written to stderr as
  `{"event":"dropped_refusal","code":"…"}`; any other unhandled rejection
  ends the process as Node does.
* Steps of one invocation run one at a time, in call order, whatever the
  script awaits (`Promise.all` does not interleave them).
* An invocation closes when its script returns or throws, on timeout, and on
  `browser_handoff`, `browser_disconnect`, revocation or loss. Closing, in
  this order: the executor refuses new steps; it sends `close` to the
  extension, which drops its queued steps of that invocation and stops the
  running one at its next command; it collects their answers and the `closed`
  acknowledgement; the host witnesses any document change; the receipt is
  built. A later call through a closed `browser` object — a kept reference,
  an un-awaited promise — is `BROWSER_INVOCATION_CLOSED`: nothing is asked,
  nothing is sent. A step that already reached Chrome keeps its true outcome
  (`dispatched`, or `unknown` when it was cut part-way). A `browser_execute`
  closed from outside ends once the steps its script started have answered
  and the script has had one turn to take those answers: what it returns
  then is its `result` (or `partial_result`). A script still waiting then,
  on whatever promise, ends with `BROWSER_INVOCATION_CLOSED` (or the guard's
  code for a refused document); the script itself is not stopped, its
  `browser` and `sleep` only refuse.
* The page receives no callbacks, timers, observers or listeners from this
  package. Page-side work is one synchronous fixed function call in an
  isolated world; waiting is polled from the extension.
* `browser_connect`, `browser_resume`, `browser_action` and `browser_execute` run one at a time, in arrival order.
  `browser_state`, `browser_handoff` and `browser_disconnect` do not queue.

### 1.6 Receipt

```json
{"ok": true, "invocation_id": "…", "result": …, "logs": ["…"],
 "steps": [{"step_id": 1, "operation": "click", "outcome": "dispatched"},
           {"step_id": 2, "operation": "fill", "outcome": "not_dispatched",
            "error": {"code": "BROWSER_TARGET_OBSCURED", "message": "…"}}],
 "blocked_requests": ["<reason> — GET https://…"],
 "state": {…}}
```

| Outcome | Meaning |
| --- | --- |
| `observed` | Read-only step; a value was read. |
| `not_dispatched` | No input or navigation command reached Chrome for this step. |
| `dispatched` | Every command of the step was acknowledged by Chrome; the effect was not checked. |
| `confirmed` | Dispatched, and the stated effect was read back (field value, checkbox state, committed document). |
| `unknown` | Something may have been sent and the answer is missing or partial. Never repeated automatically. |

Every admitted step is in `steps`, also after an error, a timeout, a handoff
or a lost transport. A document the host refused (§1.4) makes the receipt
`ok:false` with the guard's code; what the script returned, if anything, is in
`partial_result` — whenever something was returned, also `0`, `false` or
`null`; a value that is not JSON (a cycle, a BigInt) is left out and the error
message says so. A document change appears there as
`{"step_id": n, "operation": "document", "outcome": "observed", "url": "…", "request_id": "…"}`.
Every `unknown` step is reported with `BROWSER_ACTION_OUTCOME_UNKNOWN`. A
failed `sequence` adds `partial_result`: the values of the steps before the
failure.

## 2. Operations

`observe`, `read`, `get_text`, `get_attribute`, `snapshot`, `dom_snapshot`,
`ax_snapshot`, `screenshot`, `wait`, `navigate` (`goto` and `open_url` are the
same operation), `click`, `double_click`, `fill`, `type`, `press`, `check`,
`uncheck`, `scroll`.

Refused as `BROWSER_OPERATION_UNSUPPORTED` in this version, without asking the
guard: `select`, `drag`, `upload`, `submit`.
These names are excluded from the operation enum; this contract does not
provide native selection, dragging, file upload or a standalone submit action.
An allowed `click` or `press` can still cause the page to submit its own form.
After the trusted click used by `fill` or `type`, the extension checks that the
intended field actually has focus. A page redirecting focus leaves the click
dispatched, but no keyboard input is sent to the other field.

Two operations are the executor's own:

* `sequence` — `{"steps": [{"operation": "…", "args": {…}}, …]}`, 1 to 32
  steps, no nesting. The steps run in order in one invocation; each asks the
  guard as its own primitive with exactly its `operation` and `args`; the
  first failure ends the sequence and later steps are neither asked nor sent.
  There is no question for the wrapper itself.
* `cached_text` — `{"cache_key": "…", "find"?: "…", "start"?: n, "max_chars"?: n}`.
  Any read except `screenshot` may carry `cache_key` (1–64 of `A-Za-z0-9_.-`);
  the executor keeps what it captured as text (at most 32 keys, 2,000,000
  characters each). `read` and `get_text` accept zero-based `start` and
  `max_chars` (default 20,000, cap 200,000); their result retains `text`,
  `length` and `truncated` and adds `coverage:{start,end,total_length,complete}`.
  The offsets use JavaScript string characters and `end` is exclusive.
  With `cache_key`, an optional `capture_chars` from `max_chars` through
  200,000 captures a larger window into the cache while returning only
  `max_chars` text. This is opt-in; the default read still returns and caches
  at most 20,000 characters. `cached_text` returns a slice at an absolute
  source offset or the lines containing `find`; it reports the captured
  coverage and `complete`. A start outside captured coverage returns
  `BROWSER_CACHE_MISS`, not empty text. A find with no matches and
  `complete:false` proves absence only within the reported window. Historical
  string cache entries remain readable. `cached_text` asks the guard as a
  primitive, needs no lease, and sends nothing to the browser. Cached text
  survives navigation and handoff; element handles do not.

`read`/`get_text` cache records also identify the original observation (invocation/step,
executor, lease generation, tab, document and observed URL). Later cache reads
retain that source identity; a fresh page read never silently extends it.
Coverage is rendered DOM text, not proof that a virtualized site exposed a whole file.

Before the first pointer/keyboard input in a document/attachment, the extension
requests one bounded renderer frame through the existing `Page.captureScreenshot`
command, discards its pixels, and then measures/hit-tests the target afresh. This
prepares hidden-tab input without activation, focus emulation or a sacrificial
click. The first input result carries `input_preparation:{kind:"renderer_frame",
elapsed_ms:...}`; later inputs reuse preparation until a new document or attachment.
No frame within eight seconds, changed lease or revocation refuses before input.
An input acknowledgement is still not proof of application effect; check the
selected postcondition. Preparation does not cause an automatic replay.

After an input step (`click`, `double_click`, `type`, `press`, `check`,
`uncheck`) the step follows the navigation its input asked for. Chrome
announces a navigation the main frame is asked to make — a link, a form
submission, a script — as `Page.frameRequestedNavigation` while it handles the
input, but may start it only later: a form submission waits for a task the
page schedules, often longer than any fixed pause. Every such announcement
(current tab, not `javascript:`) that reaches the extension from the step's
first input command until the step stops waiting is the step's. After a
120 ms grace the step waits until that navigation has started and ended —
committed and loaded, refused, or ended without a commit or within the
document — and, as before, for as long as the main frame is loading or a
`Document` request is held; at most 10 s after the grace. A step whose input
asked for nothing answers after the grace. Its request is judged inside the step, and its commit is witnessed
before the step answers. The value carries:

* `navigated: {url, document_id}` when a new document committed, else `null`;
* `navigation`: `null` when the input asked for no navigation, or
  `{"status": "committed", "url", "document_id"}`,
  `{"status": "refused", "url"}` (the host refused the `Document` request;
  Chrome kept the page; the receipt's `blocked_requests` names it),
  `{"status": "same_document", "url"}`,
  `{"status": "abandoned"}` (Chrome ended it without a commit: a download, a
  `204`, a cancelled navigation), or `{"status": "unknown", "url"}` (still not
  ended at the bound).

The step's own outcome stays what its commands were (`dispatched`); nothing is
sent again in any case. `unknown` is not a proof either way: that navigation
can still commit after the step, or the call, has returned. Such a commit is
witnessed with whatever invocation is open then, or none; with none the host
refuses it and the lease is released (`navigation_refused`) while the page has
already changed. A navigation the page asks for later by itself (a timer, a
reply to its own request) is not the step's: it is judged with whatever
invocation is open then, and refused when none is.

No operation takes a CDP method name or caller-supplied page JavaScript. The
extension sends only the CDP methods listed in `service_worker.js`
(`CDP_METHODS`); `Target.*`, `Browser.*`, `Storage.*`, `Profiler.*` and
`Fetch.disable` are not among them.

## 3. Executor ⇄ extension

### 3.1 Listener

`ws://127.0.0.1:<port>`, port from the state file (default `47831`). A busy
port is reported (`pairing.listener:"busy"`), never replaced, and tried again
on the next call. The upgrade is
answered with a bare `403` unless `Origin` equals the origin pinned in the
state file. One extension connection at a time. Before authentication the
executor sends nothing but a random nonce.

### 3.2 Standing credential and pairing

* `scripts/setup.mjs init` creates, once per installation, a random 256-bit
  credential in `<state dir>/pairing.json` (mode `0600`, directory `0700`);
  run again it keeps the credential and only changes `--port`. It prints the
  file's path, never its content.
* The owner pastes the credential into the extension popup; the extension
  keeps it in `chrome.storage.local`.
* `scripts/setup.mjs pair` creates no credential (without one it fails and
  asks for `init`). It waits for that extension to complete the handshake
  below and pins its `Origin` in the state file. Executors accept only that
  origin.
* `setup.mjs rotate` replaces the credential and clears the pinned origin;
  *Forget pairing* in the popup deletes the extension's copy. Either ends the
  pairing; a live session whose extension forgets is closed and revoked.

Handshake, each connection, fresh nonces:

```text
ext → {"t":"hello","v":1,"nonce_e":"<32 hex>"}
srv → {"t":"challenge","nonce_s":"<32 hex>"}
ext → {"t":"auth","proof":HMAC-SHA256(credential,"ext|nonce_e|nonce_s"),"build":"<digest>"}
srv → {"t":"paired","proof":HMAC-SHA256(credential,"srv|nonce_e|nonce_s"),"executor_generation":n}
```

* A wrong proof closes the socket with code 4401 and no text.
* `build` is the SHA-256 over this package's extension files (the list is in
  `extension/bridge_protocol.mjs`). A valid proof from another build gets
  `{"t":"refused","code":"BUILD_MISMATCH"}`: reload the unpacked extension.
  This digest proves the files read from disk at handshake, not the code already
  loaded in an older worker. After every package update, the owner must Reload
  the unpacked extension. A new `input_preparation` receipt plus trusted-event
  and page-effect checks establish the tested loaded behavior; the digest alone
  does not.
* The extension verifies the server's proof and drops a listener that cannot
  give it. A listener that closes while its proof is checked (the setup
  script's `pair` does) is not a connection; the extension reports
  `connected` only for an open socket.
* The extension tries the port when its popup opens, every 30 s
  (`chrome.alarms`), and after a connection closes on its own at 1, 2, 4, 8
  and 16 s (a bounded retry, not after Forget pairing); an executor that just
  started may wait up to 30 s.

### 3.3 Messages after pairing

Lease-scoped messages carry `lease_id` and `generation`; each side compares
them before acting and before believing an answer.

| Direction | Message |
| --- | --- |
| ext → srv | `{"t":"tabs","tabs":[{"tab_id":"…"}]}` — the full owner-shared set, on pairing and on every change |
| srv → ext | `{"t":"attach","id":n,"lease_id":"…","generation":n,"tab_id":"…"}` or `…,"url":"…"}` |
| ext → srv | `{"t":"attached","id":n,"ok":true,"lease_id":"…","generation":n,"tab_id":"…","document_id":"…","url":"…","guard_armed":true,"created":false}` — `lease_id` and `generation` are the ones asked for |
| ext → srv | `{"t":"attached","id":n,"ok":false,"error":{…},"tab_id":"…","reason":"owner_revoked | canceled_by_user | target_closed | …","detached":true}` — `tab_id` once the tab is known; `reason` only when the owner or Chrome ended the attachment while it was being made; `detached` is the detach confirmed after that |
| srv → ext | `{"t":"open","invocation_id":"…"}` — the one invocation whose steps may run |
| srv → ext | `{"t":"step","id":n,"lease_id":"…","generation":n,"invocation_id":"…","step_id":n,"operation":"…","args":{…}}` |
| srv → ext | `{"t":"close","id":n,"invocation_id":"…"}` |
| ext → srv | `{"t":"closed","id":n,"invocation_id":"…"}` — sent after the answers of every step it stopped |
| ext → srv | `{"t":"step_result","id":n,"lease_id":"…","generation":n,"outcome":"…","ok":true,"value":…,"error":{…}}` |
| ext → srv | `{"t":"nav","previous":{"lease_id":"…","generation":n,"document_id":"…"},"lease_id":"…","generation":n,"document_id":"…","url":"…","request_id":"…"}` |
| ext → srv | `{"t":"url","lease_id":"…","generation":n,"url":"…"}` — same-document change |
| ext → srv | `{"t":"request","lease_id":"…","generation":n,"request":{"request_id":"…","url":"…","method":"…","post_data":…,"has_post_data":false,"complete":true,"resource_type":"…","is_main_frame":false}}` |
| srv → ext | `{"t":"verdict","request_id":"…","block":""}` |
| srv → ext | `{"t":"release","id":n,"tab_id":"…","reason":"handoff | disconnect | shutdown | refused | navigation_refused"}` — for a tab without a lease (a handoff ended by `browser_disconnect`) it only withdraws that tab from resume; `refused` for the lease a `browser_resume` was making gives the tab back to the owner, resumable as before |
| ext → srv | `{"t":"released","id":n,"detached":true,"closed_tab":false,"error":"…"}` |
| ext → srv | `{"t":"lost","tab_id":"…","reason":"owner_revoked | canceled_by_user | target_closed | replaced_with_devtools","detached":true}` — also `target_closed` when a handed-off tab is closed; `"lease_id":"…"` is added while that tab is still being attached, naming the lease asked for |
| both | `{"t":"ping"}` / `{"t":"pong"}` |

A step whose `invocation_id` is not the open invocation is answered
`not_dispatched` with `BROWSER_INVOCATION_CLOSED`; a step whose
`lease_id`/`generation` is not the extension's current valid lease is answered
`not_dispatched` with `BROWSER_LEASE_CHANGED`. Both checks run again when a
queued step's turn comes and before every command of a running step. `wait` alone follows a
lease transition, because it only reads. An answer for another lease than the
one the executor sent is recorded `unknown`.

### 3.4 Release, handoff and loss

* `browser_handoff` and `browser_disconnect` are handled on arrival: the
  executor marks the lease unusable, closes the running invocation, and sends
  `release` in the same turn — not behind queued steps.
* On `release` the extension invalidates the lease before anything else, so
  the next queued step and the next command of a running step are refused;
  it fails the requests it holds paused; then it detaches the debugger.
  Detaching does not reload the page: the DOM and unsaved fields stay.
* `released.detached:true` (and `lost.detached:true`) is the only evidence
  that Chrome's debugger was detached. Executor exit and stdio EOF are not
  that evidence. On EOF the executor sends `release` if it holds a lease,
  waits at most 1.5 s, and writes one stderr line
  `{"event":"shutdown","debugger_detach_ack":true|false|null}` — `null` when
  no lease was held (idle, or during a handoff), so there was nothing to detach.
* When the socket closes the extension invalidates the lease and detaches on
  its own. Nothing re-attaches without `browser_resume` or `browser_connect`.
  A socket that closes while a `release` still waits for its answer is a lost
  transport too (`transport_lost:true`, no detach claimed).
* A handoff ends with `browser_resume`, with `browser_disconnect` (the tab
  stays with the owner and is withdrawn from resume), or when the owner closes
  the tab (`lost`, `target_closed`) or stops sharing it. A resume refused with
  `BROWSER_TAB_NOT_APPROVED` ends it too (`last_closure.reason:"target_closed"`).
  A resume the host refuses, or one the executor does not adopt, does not end
  it: the extension releases the lease (`refused`) and keeps the tab with the
  owner, open and resumable; the state stays `phase:"handoff"`.
  A concurrent `browser_disconnect` invalidates that handoff immediately; a
  late refused resume cannot restore it or block a new connect.

## 4. Error codes

`BROWSER_POLICY_UNAVAILABLE`, `BROWSER_REQUEST_GUARD_UNAVAILABLE`,
`BROWSER_NOT_PAIRED`, `BROWSER_PORT_BUSY`, `BROWSER_TAB_NOT_APPROVED`,
`BROWSER_SESSION_REVOKED`, `BROWSER_SESSION_DETACHED`,
`BROWSER_HANDOFF_ACTIVE`, `BROWSER_LEASE_CHANGED`,
`BROWSER_INVOCATION_CLOSED`, `BROWSER_EXECUTE_TIMEOUT`,
`BROWSER_SCRIPT_ERROR`, `BROWSER_OPERATION_UNSUPPORTED`,
`BROWSER_ARGUMENT_INVALID`, `BROWSER_TARGET_NOT_FOUND`,
`BROWSER_INPUT_NOT_READY`, `BROWSER_CACHE_MISS`,
`BROWSER_TARGET_AMBIGUOUS`, `BROWSER_TARGET_NOT_VISIBLE`,
`BROWSER_TARGET_DISABLED`, `BROWSER_TARGET_UNSTABLE`,
`BROWSER_TARGET_OBSCURED`, `BROWSER_HANDLE_STALE`,
`BROWSER_DOCUMENT_CHANGED`, `BROWSER_DIALOG_OPEN`, `BROWSER_WAIT_TIMEOUT`,
`BROWSER_NAVIGATION_FAILED`, `BROWSER_REQUEST_BLOCKED`,
`BROWSER_SCREENSHOT_UNAVAILABLE`, `BROWSER_ACTION_OUTCOME_UNKNOWN`,
`BROWSER_INTERNAL`. A guard refusal is passed through with the host's text.
