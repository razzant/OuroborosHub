#!/usr/bin/env node
// Smoke test for OuroborosHub PR #324 (skill chrome_browser 0.5.0: a screenshot that names its tab by "tabHandle").
//
// Real Chrome for Testing, headed on this run's own X display, a new profile, the official Playwright Extension 0.5.0
// from its Web Store CRX, and the skill's own MCP stdio server from the PR's files. The skill is driven only through
// tools/call; the two approvals go through the extension's own "Allow & select" for this test's synthetic tab A only.
//
// Ordinary A/B smoke, in this order:
//   - the owner's tab A is in front with a typed draft; B is opened in the background and A made current again;
//   - two captures by B's handle each return B's grid with the value B painted just before the call (fresh, B's own),
//     while A keeps visibility, focus target and draft, and the owner's later keystrokes still land in A;
//   - a screenshot without a handle is A's;
//   - once the owner's close of B is confirmed, B's handle gives an error and no image;
//   - after browser_disconnect and the second ordinary approval: A's old handle (A survived, under a new handle) and
//     B's old handle (B is gone) give an error and no image; A's new handle and a plain screenshot give A.
// Published (--out): summary.json and report.md built only from fixed sentences, codes, counts, versions, hashes and
// roles, plus the PNGs that read as the expected synthetic tab. Everything else stays in --work/diagnostics.
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { ChromePipe } from './lib/cdp_pipe.mjs';
import { random32, startFixture } from './lib/fixture.mjs';
import { startGuard } from './lib/guard_standin.mjs';
import { McpClient } from './lib/mcp_client.mjs';
import { codeOf, forbiddenFlags, HANDLE_FORM, imagesOf, parseConnectLink, redact, tabsWithHandles, textOf } from './lib/parse.mjs';
import { decodePng } from './lib/png.mjs';
import { decodeGrid, hex32 } from './lib/visual_code.mjs';
import { X11 } from './lib/x11.mjs';

const EXTENSION_ID = 'mmlmfjhmonkocbjadbfplnigmagldckm';
const EXTENSION_VERSION = '0.5.0';
const SKILL_VERSION = '0.5.0';
const CLIENT_BANNER = '"Ouroboros" is trying to connect to the Playwright Extension.';
const CONNECT_PAGE = `chrome-extension://${EXTENSION_ID}/connect.html`;
const WINDOW = { width: 1280, height: 960 };
const CALL_TIMEOUT_MS = 90_000;
const RUN_TIMEOUT_MS = 20 * 60_000;
const TEXT1 = 'draft one ';
const TEXT2 = 'and two';
const UNKNOWN_HANDLE = 'BROWSER_TAB_HANDLE_UNKNOWN';
// The page event kinds the fixture writes (fixture.mjs); only these words of a page's record are published.
const EVENT_KINDS = new Set(['load', 'sse-open', 'sse-error', 'window-focus', 'window-blur', 'visibility', 'focusin', 'focusout',
  'pagehide', 'pageshow', 'resize', 'keydown', 'pointerdown', 'input', 'paint', 'focus-draft', 'report-failed']);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const sha256 = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
const readJson = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));

// [id, kind, required, what it establishes]
const CHECKS = [
  ['P1', 'precondition', true, 'Inputs were fetched from their official sources and verified (acquire.sh)'],
  ['P2', 'precondition', true, "A headed Chrome for Testing of the pinned version runs on this run's own X display and cookie"],
  ['P3', 'precondition', true, "Chrome's own sandbox is on as shipped (chrome://sandbox) and no switch weakening a sandbox, web, extension or automation check was given"],
  ['P4', 'precondition', true, 'The approval page belongs to the official Playwright Extension 0.5.0 and lists only this test\'s tab A'],
  ['P5', 'precondition', true, "The skill's MCP stdio server starts from the tested files, and only browser_take_screenshot takes tabHandle"],
  ['P6', 'precondition', true, 'First ordinary approval: Allow & select on tab A, and the tools become ready'],
  ['S1', 'scenario', true, 'Baseline: tab A is in front, its draft is focused and holds what the owner typed with real keys'],
  ['S2', 'scenario', true, 'browser_tabs new opens B in the background, select makes A current again, and A is untouched'],
  ['S3', 'scenario', true, 'browser_tabs list gives A and B distinct handles of the documented form'],
  ['S4', 'scenario', true, "Capture 1 by B's handle returns B's grid with the value B painted just before the call"],
  ['S5', 'scenario', true, 'During capture 1 tab A kept its visibility, focus target and draft, and B got no focus or input'],
  ['S6', 'scenario', true, "Capture 2 by the same handle returns B's next value: the frame changes and is fresh"],
  ['S7', 'scenario', true, 'During capture 2 tab A kept its visibility, focus target and draft, and B got no focus or input'],
  ['S8', 'scenario', true, "After the captures the owner's real keystrokes still land in A's draft, and none in B"],
  ['S9', 'scenario', true, "A screenshot without a handle is the current tab A, with A's current value"],
  ['S10', 'scenario', true, "After the owner's close of B was confirmed, B's handle answers BROWSER_TAB_HANDLE_UNKNOWN with no image"],
  ['S11', 'scenario', true, 'browser_disconnect, then the second ordinary approval on a new link'],
  ['S12', 'scenario', true, "After reapproval A's old handle gives BROWSER_TAB_HANDLE_UNKNOWN with no image, while A is listed under a new handle"],
  ['S13', 'scenario', true, "After reapproval the closed B's old handle gives BROWSER_TAB_HANDLE_UNKNOWN with no image, and B is not listed"],
  ['S14', 'scenario', true, "A's new handle captures A with A's current value"],
  ['S15', 'scenario', true, 'A screenshot without a handle after reapproval is tab A'],
  ['S16', 'scenario', true, 'Tab A was never reloaded and its draft survived the reconnection'],
  ['I1', 'scenario', false, "B's own page reported itself hidden throughout both captures (informational)"],
];

function parseArgs(argv) {
  const options = {};
  const flags = { '--skill': 'skill', '--node-modules': 'nodeModules', '--chrome': 'chrome', '--ext': 'ext', '--out': 'out', '--work': 'work', '--inputs': 'inputs' };
  for (let i = 0; i < argv.length; i += 2) {
    const name = flags[argv[i]];
    if (!name || argv[i + 1] === undefined)
      throw new Error(`unknown argument or missing value: ${argv[i]}`);
    options[name] = path.resolve(argv[i + 1]);
  }
  for (const [flag, name] of Object.entries(flags)) {
    if (!options[name])
      throw new Error(`${flag} is required`);
  }
  return options;
}

// The owner's click target in A's draft, in screen pixels, from A's own report of its window and draft box.
function draftPoint(state) {
  const { screenX, screenY, outerWidth, outerHeight, innerWidth, innerHeight } = state.screen;
  const border = Math.max(0, Math.round((outerWidth - innerWidth) / 2));
  const left = screenX + border;
  const top = screenY + Math.max(0, outerHeight - innerHeight - border);
  const rect = state.draft.rect;
  return { x: Math.round(left + rect.left + rect.width / 2), y: Math.round(top + rect.top + rect.height / 2) };
}

// Events in A that would mean the owner's tab lost the front or the focus, or went away.
const badForA = (events) => events.filter((event) => ['window-blur', 'focusout', 'pagehide'].includes(event.kind)
  || (event.kind === 'visibility' && event.vis !== 'visible'));
// Events in B that would mean B took the focus or got input.
const focusOrInputForB = (events) => events.filter((event) => ['window-focus', 'keydown', 'pointerdown'].includes(event.kind));
// The published form of a page record: its event kinds only, from the fixture's own fixed list.
const kinds = (events) => events.filter((event) => EVENT_KINDS.has(event.kind)).map((event) => (event.kind === 'visibility' ? `visibility:${event.vis === 'visible' ? 'visible' : 'hidden'}` : event.kind));

// Read in the extension's approval page (connect.html of 0.5.0), and only there. It returns no address or title of any
// tab: each listed row is classified as this test's tab A, this approval page itself, or unexpected.
const APPROVAL_PAGE = (urlA, titleA) => `(() => {
  const rows = [...document.querySelectorAll('.tab-item')].map((row) => ({ row, title: ((row.querySelector('.tab-title') || {}).textContent || ''), url: ((row.querySelector('.tab-url') || {}).textContent || '') }));
  const kinds = rows.map((r) => r.url === ${JSON.stringify(urlA)} && r.title === ${JSON.stringify(titleA)} ? 'A'
    : r.url.startsWith(${JSON.stringify(CONNECT_PAGE)}) ? 'this-approval-page' : 'unexpected');
  const banner = [...document.querySelectorAll('.status-banner')].map((node) => node.innerText.trim()).join(' | ');
  const identity = { id: chrome.runtime.id, name: chrome.runtime.getManifest().name, version: chrome.runtime.getManifest().version };
  const base = { kinds, identity, client_shown: banner.includes(${JSON.stringify(CLIENT_BANNER)}), warning_shown: !!document.querySelector('.warning-banner') };
  if (!rows.length) return { ...base, wait: true };
  if (kinds.includes('unexpected')) return { ...base, refuse: 'unexpected-tab-listed' };
  const a = rows.filter((r, i) => kinds[i] === 'A');
  if (a.length !== 1) return { ...base, refuse: 'tab-A-not-listed-once' };
  const button = [...a[0].row.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Allow & select');
  if (!button) return { ...base, refuse: 'no-allow-button-for-A' };
  button.scrollIntoView({ block: 'center' });
  const r = button.getBoundingClientRect();
  return { ...base, x: r.left + r.width / 2, y: r.top + r.height / 2 };
})()`;

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const { out, work } = options;
  const diag = path.join(work, 'diagnostics');
  for (const dir of [out, path.join(out, 'images'), diag])
    fs.mkdirSync(dir, { recursive: true });
  const profile = path.join(work, 'profile');
  if (fs.existsSync(profile) && fs.readdirSync(profile).length)
    throw new Error('the profile folder already has files: Chrome must start with a new profile');
  for (const dir of ['profile', 'home', 'tmp', 'crash', 'mcp-home', 'mcp-tmp'])
    fs.mkdirSync(path.join(work, dir), { recursive: true });

  // This run's own display and cookie (run.sh); nothing else is accepted.
  const display = process.env.SMOKE_DISPLAY || '';
  const xauthority = process.env.SMOKE_XAUTHORITY || '';

  const started = new Date();
  const timeline = fs.createWriteStream(path.join(diag, 'timeline.jsonl'), { flags: 'a' });
  const callsLog = fs.createWriteStream(path.join(diag, 'calls.jsonl'), { flags: 'a' });
  timeline.on('error', () => {});
  callsLog.on('error', () => {});
  let logsOpen = true;
  // Local record with details; the workflow log gets only the fixed `line`.
  const note = (line, detail = {}) => {
    if (logsOpen)
      timeline.write(`${JSON.stringify({ at: new Date().toISOString(), line, ...detail })}\n`);
    process.stdout.write(`${line}\n`);
  };

  const checks = new Map(CHECKS.map(([id, kind, required, title]) => [id, { id, kind, required, title, status: 'UNVERIFIED', detail: 'not reached', evidence: {} }]));
  const setCheck = (id, status, detail, evidence = {}) => {
    Object.assign(checks.get(id), { status, detail, evidence });
    note(`check ${id} ${status}`);
  };
  // Every condition is [ok, a fixed sentence]; the check passes only if all hold.
  const judge = (id, conditions, evidence = {}) => {
    const failed = conditions.filter(([ok]) => !ok).map(([, what]) => what);
    setCheck(id, failed.length ? 'FAIL' : 'PASS', failed.length ? `not so: ${failed.join('; ')}` : conditions.map(([, what]) => what).join('; '), evidence);
    return !failed.length;
  };
  const published = { schema: 2, test: 'hub324-tabhandle-smoke', started: started.toISOString(), versions: {}, sha: {}, environment: {}, images: [] };
  const local = { handles: {}, connections: {}, decoded: [], errors: [] };

  let fixture = null;
  let guard = null;
  let chrome = null;
  let mcp = null;
  let callCount = 0;
  let finished = null;

  async function finish() {
    if (finished)
      return finished;
    clearTimeout(watchdog);
    published.duration_s = Math.round((Date.now() - started.getTime()) / 1000);
    published.calls = callCount;
    published.checks = [...checks.values()];
    const required = published.checks.filter((check) => check.required);
    const skillFault = ['P5', 'P6'].filter((id) => checks.get(id).status === 'FAIL' && (id === 'P5' || checks.get(id).evidence.skill_fault === true));
    const failedScenario = required.filter((check) => check.kind === 'scenario' && check.status === 'FAIL').map((check) => check.id);
    const environment = required.filter((check) => check.kind === 'precondition' && check.status !== 'PASS' && !skillFault.includes(check.id));
    const open = required.filter((check) => check.status !== 'PASS');
    if (skillFault.length || failedScenario.length) {
      published.verdict = 'FAIL';
      published.reason = `failed: ${[...skillFault, ...failedScenario].join(', ')}`;
    } else if (environment.length) {
      published.verdict = 'UNVERIFIED';
      published.reason = `the test environment was not established: ${environment.map((check) => `${check.id} ${check.status}`).join(', ')}`;
    } else if (open.length) {
      published.verdict = 'UNVERIFIED';
      published.reason = `not established: ${open.map((check) => check.id).join(', ')}`;
    } else {
      published.verdict = 'PASS';
      published.reason = 'every required check passed';
    }
    published.guard = guard ? { ...guard.counts } : null;
    fs.writeFileSync(path.join(out, 'summary.json'), `${JSON.stringify(published, null, 1)}\n`);
    fs.writeFileSync(path.join(out, 'report.md'), report(published));
    fs.writeFileSync(path.join(diag, 'local-details.json'), `${JSON.stringify({ ...local, guard_refused: guard ? guard.refusedUrls.slice(0, 50).map(redact) : null }, null, 1)}\n`);
    note(`verdict ${published.verdict}`);
    logsOpen = false;
    await new Promise((resolve) => timeline.end(resolve));
    await new Promise((resolve) => callsLog.end(resolve));
    finished = published.verdict;
    return finished;
  }

  const watchdog = setTimeout(() => {
    note('watchdog: the run did not finish in time');
    local.errors.push('watchdog');
    try {
      chrome?.child.kill('SIGKILL');
      mcp?.child.kill('SIGKILL');
    } catch {}
    finish().finally(() => process.exit(3));
  }, RUN_TIMEOUT_MS);
  watchdog.unref();

  // A step of the scenario. If it throws, its checks that were not reached say so, and the next step that does not
  // depend on it still runs. The error itself stays in the local diagnostics.
  async function phase(name, ids, body) {
    note(`phase ${name}`);
    try {
      await body();
      return true;
    } catch (error) {
      local.errors.push(`${name}: ${redact(String(error?.stack || error))}`);
      note(`phase ${name} stopped (details in the local diagnostics)`);
      for (const id of ids) {
        if (checks.get(id).detail === 'not reached')
          setCheck(id, 'UNVERIFIED', `the step "${name}" stopped before this check`);
      }
      return false;
    }
  }

  // One tools/call. Its text stays local (redacted); the caller gets the code, the error flag, the image count and,
  // only for a call that is meant to return an image, the image items in memory.
  async function call(name, args = {}, label = name, { image = false } = {}) {
    const n = ++callCount;
    const t0 = Date.now();
    let result = null;
    let error = null;
    try {
      result = await mcp.callTool(name, args, CALL_TIMEOUT_MS);
    } catch (failure) {
      error = String(failure?.message || failure);
    }
    const ms = Date.now() - t0;
    const raw = result ? textOf(result) : '';
    const items = result ? imagesOf(result) : [];
    const isError = result?.isError === true;
    const code = codeOf(raw) ?? (error ? 'NO_ANSWER' : null);
    if (logsOpen)
      callsLog.write(`${JSON.stringify({ n, label, name, args, ms, isError, code, error: error ? redact(error) : null, images: items.length, text: redact(raw) })}\n`);
    note(`call ${n} ${name} ${label}: ${isError || error ? `error ${code ?? 'without a code'}` : 'ok'}, ${items.length} image(s), ${ms} ms`);
    return { n, label, ms, isError, failed: isError || !!error, code, raw, imageCount: items.length, imageItems: image ? items : [] };
  }

  // A call meant to return tab `role` with `value`: the image is decoded in memory; only one that reads as exactly that
  // synthetic tab and value is written (to the published images). Any other image is described, never stored.
  function expectedImage(shot, role, value) {
    if (shot.imageItems.length !== 1)
      return { count: shot.imageCount, ok: false };
    const item = shot.imageItems[0];
    const view = { count: 1, format: item.mimeType === 'image/png' ? 'png' : 'other', ok: false, role: null, value: null };
    if (view.format !== 'png')
      return view;
    try {
      const bytes = Buffer.from(String(item.data || ''), 'base64');
      const decoded = decodeGrid(decodePng(bytes));
      Object.assign(view, { role: decoded.role, value: hex32(decoded.value), size: decoded.size });
      local.decoded.push({ call: shot.n, label: shot.label, ok: decoded.ok, role: decoded.role, value: hex32(decoded.value), unreadable: decoded.unreadable, worst: decoded.worst, size: decoded.size });
      if (decoded.ok && decoded.role === role && decoded.value === (value >>> 0)) {
        view.ok = true;
        view.sha256 = sha256(bytes);
        view.file = `images/${String(shot.n).padStart(3, '0')}-${shot.label}.png`;
        fs.writeFileSync(path.join(out, view.file), bytes);
        published.images.push({ file: view.file, call: shot.n, role, value: view.value, sha256: view.sha256, size: decoded.size });
      }
    } catch {
      view.format = 'unreadable';
    }
    shot.imageItems = [];
    return view;
  }

  const pageState = async (role, timeoutMs = 10_000) => {
    const seq = fixture.push(role, { cmd: 'report' });
    return await fixture.waitUntil(() => fixture.reports.find((r) => r.role === role && r.kind === 'state' && r.seq === seq), timeoutMs, `a report from tab ${role}`);
  };
  const paint = async (role, value, timeoutMs = 10_000) => {
    const seq = fixture.push(role, { cmd: 'paint', value });
    return await fixture.waitUntil(() => fixture.reports.find((r) => r.role === role && r.kind === 'painted' && r.seq === seq), timeoutMs, `tab ${role} to paint`);
  };
  const focusDraft = async () => {
    const seq = fixture.push('A', { cmd: 'focus-draft' });
    return await fixture.waitUntil(() => fixture.reports.find((r) => r.role === 'A' && r.seq === seq), 10_000, 'tab A to focus its draft');
  };
  const eventsBetween = (role, from, to) => fixture.events(role).filter((event) => event.t >= from && event.t <= to);
  const poll = async (probe, timeoutMs, intervalMs = 500) => {
    const end = Date.now() + timeoutMs;
    for (;;) {
      const value = await probe();
      if (value || Date.now() > end)
        return value || null;
      await sleep(intervalMs);
    }
  };

  const shotArgs = { extra: {} };
  const screenshot = (args, label, wantImage) => call('browser_take_screenshot', { ...shotArgs.extra, ...args }, label, { image: wantImage });
  // A refusal is judged by its error flag, its code and the number of images only.
  const refusal = (shot) => [
    [shot.isError, 'the call answered an error'],
    [shot.code === UNKNOWN_HANDLE, `the code is ${UNKNOWN_HANDLE}`],
    [shot.imageCount === 0, 'no image came back'],
  ];
  const refusalEvidence = (shot) => ({ call: shot.n, isError: shot.isError, code: shot.code, images: shot.imageCount });
  // A capture is judged by its pixels; an image in another format is not read, which leaves the check unverified.
  const judgeCapture = (id, shot, role, value, handle, extra = [], evidence = {}) => {
    const view = expectedImage(shot, role, value);
    const record = { call: shot.n, ms: shot.ms, code: shot.code, image: { count: view.count, format: view.format ?? null, role: view.role ?? null, value: view.value ?? null, file: view.file ?? null }, ...evidence };
    if (view.count === 1 && view.format === 'other') {
      setCheck(id, 'UNVERIFIED', 'the image is not a PNG, so this harness did not read its pixels', record);
      return view;
    }
    judge(id, [
      [!shot.failed, 'the call succeeded'],
      [view.count === 1, 'exactly one image came back'],
      [view.ok, `the image reads as tab ${role} showing ${hex32(value)}, the value painted just before the call`],
      ...(handle ? [[shot.raw.includes(`Tab captured: tabHandle ${handle}`), 'the result names the captured handle']] : [[!shot.raw.includes('Tab captured:'), 'the result has no handle note']]),
      ...extra,
    ], record);
    return view;
  };

  // State the steps share.
  const links = [];
  const handles = { A1: null, B1: null, A2: null };
  let aInstance = null;
  let typed1 = false;
  let typed2 = false;
  let x11 = null;

  // The only pages the harness may open, attach to or close in its test browser.
  const permitPage = (url, action) => {
    const value = String(url ?? '');
    const sandboxPage = value === 'chrome://sandbox' || value === 'chrome://sandbox/';
    const approvalPage = value.startsWith(`${CONNECT_PAGE}?`);
    if (action === 'open' || action === 'attach')
      return sandboxPage || (approvalPage && links.includes(value));
    if (action === 'close')
      return sandboxPage || (!!fixture && value === fixture.urlB);
    return false;
  };

  // The owner opens the link in a new tab of this test browser and chooses Allow & select on tab A, and only if the
  // page is the pinned extension's and lists nothing but tab A (and itself).
  async function approve(link) {
    const begun = Date.now();
    const { targetId } = await chrome.send('Target.createTarget', { url: link });
    const { sessionId } = await chrome.send('Target.attachToTarget', { targetId, flatten: true });
    let page = null;
    let gone = false;
    try {
      while (Date.now() - begun < 25_000) {
        try {
          page = await chrome.evaluate(sessionId, APPROVAL_PAGE(fixture.urlA, fixture.titleA));
        } catch {
          page = null;
        }
        if (page && (page.x !== undefined || page.refuse))
          break;
        await sleep(250);
      }
      const view = { kinds: page?.kinds ?? [], identity: page?.identity ?? null, client_shown: page?.client_shown ?? false, warning_shown: page?.warning_shown ?? false };
      const identityOk = view.identity?.id === EXTENSION_ID && view.identity?.version === EXTENSION_VERSION && view.identity?.name === 'Playwright Extension';
      if (!page || page.x === undefined)
        return { ok: false, stage: page?.refuse ?? 'page-not-ready', ...view, identityOk, ms: Date.now() - begun };
      if (!identityOk || !view.client_shown)
        return { ok: false, stage: identityOk ? 'client-not-shown' : 'not-the-pinned-extension', ...view, identityOk, ms: Date.now() - begun };
      await chrome.click(sessionId, page.x, page.y);
      for (let i = 0; i < 100 && !gone; i++) {
        await sleep(150);
        gone = !(await chrome.targetsNow()).some((target) => target.targetId === targetId);
      }
      return { ok: gone, stage: gone ? 'approved' : 'approval-page-still-open', ...view, identityOk, ms: Date.now() - begun };
    } finally {
      if (!gone)
        await chrome.send('Target.detachFromTarget', { sessionId }).catch(() => {});
    }
  }

  async function waitReady(label) {
    const begun = Date.now();
    let last = null;
    while (Date.now() - begun < 45_000) {
      last = await call('browser_tabs', { action: 'list' }, `${label}-ready`);
      if (!last.failed)
        return { ok: true, list: last, ms: Date.now() - begun };
      if (!['BROWSER_NEEDS_APPROVAL', 'BROWSER_ATTACHING'].includes(last.code))
        return { ok: false, list: last, ms: Date.now() - begun };
      await sleep(500);
    }
    return { ok: false, list: last, ms: Date.now() - begun };
  }

  // browser_connect, the approval, then the tools are ready. The link itself never leaves memory.
  async function connectAndApprove(label) {
    const connect = await call('browser_connect', {}, `${label}-connect`);
    const record = { label, ok: false, connect_call: connect.n };
    if (connect.failed)
      return Object.assign(record, { stage: 'connect-refused', code: connect.code, skill_fault: true });
    let link;
    try {
      link = parseConnectLink(connect.raw, EXTENSION_ID);
    } catch {
      return Object.assign(record, { stage: 'link-unusable', skill_fault: true });
    }
    links.push(link.href);
    record.new_link = links.length === 1 || links[links.length - 1] !== links[links.length - 2];
    record.previous_code = codeOf(connect.raw.split('\n').find((line) => line.startsWith('The previous connection ended:')) || '');
    record.approval = await approve(link.href);
    if (!record.approval.ok)
      return Object.assign(record, { stage: record.approval.stage });
    const ready = await waitReady(label);
    Object.assign(record, { ready_call: ready.list?.n ?? null, ready_ms: ready.ms, ok: ready.ok, stage: ready.ok ? 'ready' : 'not-ready', code: ready.ok ? null : ready.list?.code ?? null });
    record.tabs = ready.ok ? tabsWithHandles(ready.list.raw) : [];
    return record;
  }

  async function typeIntoDraft(text, expected) {
    const typedAt = Date.now();
    const typedResult = x11.type(text);
    let state = null;
    for (let i = 0; i < 20; i++) {
      state = (await pageState('A')).state;
      if (state.draft?.value === expected)
        break;
      await sleep(250);
    }
    const keys = fixture.events('A').filter((event) => event.kind === 'keydown' && event.t >= typedAt);
    return { state, status: typedResult.status, keys: keys.length, trusted: keys.filter((event) => event.trusted && event.target === 'draft').length };
  }
  const draftNow = () => (typed2 ? TEXT1 + TEXT2 : typed1 ? TEXT1 : '');

  try {
    // ---- setup: inputs, loopback fixture, stand-in guard, Chrome, its sandbox, the skill's server ------------------------
    const ready = await phase('setup', ['P1', 'P2', 'P3', 'P5'], async () => {
      const inputs = options.inputs && fs.existsSync(options.inputs) ? readJson(options.inputs) : null;
      const parts = inputs?.parts ?? {};
      const skillVersion = (/^version:\s*(\S+)\s*$/m.exec(fs.readFileSync(path.join(options.skill, 'SKILL.md'), 'utf8')) || [])[1] ?? null;
      published.versions = {
        skill: skillVersion,
        chrome_for_testing: parts.cft?.version ?? null,
        extension: parts.extension?.version ?? null,
        node: process.version,
        npm: Object.fromEntries(Object.entries(parts.npm?.packages ?? {}).map(([name, entry]) => [name, entry.version])),
      };
      published.sha = {
        hub_commit: parts.hub?.commit ?? null,
        skill_tree: parts.hub?.tree ?? null,
        skill_tree_is_expected: parts.hub?.tree_matches_pin ?? null,
        chrome_for_testing_zip_sha256: parts.cft?.sha256 ?? null,
        chrome_for_testing_md5_matches_google: parts.cft ? parts.cft.md5_base64 === parts.cft.gcs_md5_base64 : null,
        extension_crx_sha256: parts.extension?.sha256 ?? null,
        extension_developer_signature_verified: parts.extension?.developer_signature_verified ?? null,
        node_tarball_sha256: parts.node?.sha256 ?? null,
        npm_integrity_ok: Object.fromEntries(Object.entries(parts.npm?.packages ?? {}).map(([name, entry]) => [name, entry.integrity_ok === true && entry.shasum_ok === true])),
      };
      if (!inputs)
        setCheck('P1', 'UNVERIFIED', 'no record of verified inputs was given');
      else if (inputs.ok === true && parts.hub?.tree_matches_pin === true)
        setCheck('P1', 'PASS', 'every input matched its pin or its publisher\'s checksum, and the skill tree is the expected one');
      else
        setCheck('P1', 'FAIL', `the inputs did not verify (${(inputs.problems || []).length} problem(s), listed in the local record)`);

      const run = crypto.randomBytes(5).toString('hex');
      const token = crypto.randomBytes(32).toString('hex');
      fixture = await startFixture({ run, logFile: path.join(diag, 'fixture.jsonl') });
      guard = await startGuard({ token, allowOrigin: fixture.origin, logFile: path.join(diag, 'guard.jsonl') });

      const displayOwn = /^:\d+$/.test(display) && xauthority.startsWith(`${work}${path.sep}`) && fs.existsSync(xauthority);
      const chromeArgs = [
        `--user-data-dir=${profile}`,
        `--crash-dumps-dir=${path.join(work, 'crash')}`,
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-default-apps',
        '--disable-sync',
        '--disable-background-networking',
        '--disable-component-update',
        '--password-store=basic',
        '--window-position=0,0',
        `--window-size=${WINDOW.width},${WINDOW.height}`,
        '--force-device-scale-factor=1',
        '--remote-debugging-pipe',
        `--disable-extensions-except=${options.ext}`,
        `--load-extension=${options.ext}`,
        fixture.urlA,
      ];
      // Chrome's whole environment, explicitly: this run's display and cookie, folders inside --work, nothing inherited.
      const chromeEnv = {
        PATH: process.env.PATH || '/usr/bin:/bin',
        DISPLAY: display,
        XAUTHORITY: xauthority,
        HOME: path.join(work, 'home'),
        TMPDIR: path.join(work, 'tmp'),
        XDG_CONFIG_HOME: path.join(work, 'home', '.config'),
        XDG_CACHE_HOME: path.join(work, 'home', '.cache'),
        LANG: 'C.UTF-8',
        TZ: 'UTC',
      };
      const flags = forbiddenFlags(chromeArgs);
      published.environment = {
        os: fs.existsSync('/etc/os-release') ? (/^PRETTY_NAME="?([^"\n]*)"?$/m.exec(fs.readFileSync('/etc/os-release', 'utf8')) || [])[1] ?? null : null,
        kernel: `${os.release()} ${os.arch()}`,
        display_is_this_runs_own: displayOwn,
        display_requires_its_cookie: process.env.SMOKE_COOKIE_REQUIRED === 'yes',
        chrome_env_keys: Object.keys(chromeEnv),
        window_manager: process.env.SMOKE_WM || 'none',
        window: `${WINDOW.width}x${WINDOW.height}`,
        sandbox: 'as shipped by Chrome; nothing on the runner changed',
      };
      if (!displayOwn) {
        setCheck('P2', 'UNVERIFIED', 'this run has no display and cookie of its own (run.sh creates them); Chrome was not started');
        throw new Error('no own display');
      }
      chrome = ChromePipe.launch({ executable: options.chrome, args: chromeArgs, env: chromeEnv, logFile: path.join(diag, 'chrome.log'), cdpLogFile: path.join(diag, 'cdp.jsonl'), permitPage });
      let version = null;
      for (const end = Date.now() + 30_000; !version && Date.now() < end && !chrome.exit;) {
        try {
          version = await chrome.send('Browser.getVersion', {}, undefined, 5_000);
        } catch {
          await sleep(500);
        }
      }
      if (!version) {
        setCheck('P2', 'UNVERIFIED', 'Chrome did not start or did not answer on its pipe');
        throw new Error('Chrome did not start');
      }
      published.versions.chrome = String(version.product);
      const expectedChrome = published.versions.chrome_for_testing;
      judge('P2', [
        [displayOwn, 'Chrome got this run\'s own DISPLAY and XAUTHORITY, set explicitly'],
        [!/Headless/i.test(`${version.product} ${version.userAgent}`), 'the browser is headed (no HeadlessChrome)'],
        [!!expectedChrome && String(version.product).endsWith(`/${expectedChrome}`), `the product is Chrome ${expectedChrome}`],
      ], { product: String(version.product) });

      let sandboxText = '';
      const sandboxPage = await chrome.send('Target.createTarget', { url: 'chrome://sandbox' });
      const { sessionId } = await chrome.send('Target.attachToTarget', { targetId: sandboxPage.targetId, flatten: true });
      for (let i = 0; i < 40 && !/sandboxed/i.test(sandboxText); i++) {
        await sleep(250);
        try {
          sandboxText = String(await chrome.evaluate(sessionId, 'document.body ? document.body.innerText : ""'));
        } catch {
          sandboxText = '';
        }
      }
      await chrome.send('Target.closeTarget', { targetId: sandboxPage.targetId }).catch(() => {});
      fs.writeFileSync(path.join(diag, 'chrome-sandbox.txt'), `${sandboxText}\n`);
      const adequate = /You are adequately sandboxed/i.test(sandboxText) && !/not adequately/i.test(sandboxText);
      if (flags.length)
        setCheck('P3', 'FAIL', 'a forbidden switch was given to Chrome');
      else
        setCheck('P3', adequate ? 'PASS' : 'UNVERIFIED', adequate ? 'chrome://sandbox says "You are adequately sandboxed."; no forbidden switch was given' : 'chrome://sandbox does not say "You are adequately sandboxed." (prerequisite: a runner where Chrome\'s own sandbox works unchanged)');
      if (checks.get('P3').status !== 'PASS')
        throw new Error('the scenario does not run on a browser without its own sandbox');

      await fixture.waitUntil(() => fixture.reports.some((r) => r.role === 'A' && r.kind === 'load') && fixture.listening('A'), 30_000, 'tab A to load and listen');
      aInstance = fixture.reports.find((r) => r.role === 'A' && r.kind === 'load').instance;

      const serverEnv = {
        PATH: process.env.PATH || '/usr/bin:/bin',
        HOME: path.join(work, 'mcp-home'),
        TMPDIR: path.join(work, 'mcp-tmp'),
        LANG: 'C.UTF-8',
        OUROBOROS_CHROME_NODE_MODULES: options.nodeModules,
        OUROBOROS_BROWSER_GUARD: guard.endpoint,
        OUROBOROS_BROWSER_GUARD_TOKEN: token,
      };
      mcp = McpClient.start({ command: process.execPath, args: [path.join(options.skill, 'scripts', 'server.mjs')], env: serverEnv, cwd: work, stderrFile: path.join(diag, 'mcp-stderr.log') });
      let init = null;
      let tools = [];
      try {
        init = await mcp.initialize();
        tools = await mcp.listTools();
      } catch (error) {
        local.errors.push(`MCP start: ${redact(error.message)}`);
      }
      fs.writeFileSync(path.join(diag, 'tools-list.json'), `${JSON.stringify(tools, null, 1)}\n`);
      const names = tools.map((tool) => tool.name);
      const withHandle = tools.filter((tool) => tool.inputSchema?.properties?.tabHandle).map((tool) => tool.name);
      const shotTool = tools.find((tool) => tool.name === 'browser_take_screenshot');
      if (shotTool?.inputSchema?.properties?.type)
        shotArgs.extra = { type: 'png' };
      published.versions.mcp_server = init?.serverInfo?.version ? String(init.serverInfo.version) : null;
      judge('P5', [
        [init?.serverInfo?.name === 'chrome_browser', 'the server names itself chrome_browser'],
        [String(init?.serverInfo?.version ?? '').endsWith('+chrome_browser'), 'its version is upstream\'s plus "+chrome_browser"'],
        [['browser_connect', 'browser_disconnect', 'browser_tabs', 'browser_take_screenshot'].every((name) => names.includes(name)), 'it lists browser_connect, browser_disconnect, browser_tabs and browser_take_screenshot'],
        [withHandle.length === 1 && withHandle[0] === 'browser_take_screenshot', 'only browser_take_screenshot takes tabHandle'],
        [shotTool?.inputSchema?.properties?.tabHandle?.type === 'string' && !(shotTool.inputSchema.required || []).includes('tabHandle'), 'tabHandle is an optional string'],
        [skillVersion === SKILL_VERSION, `SKILL.md says version ${SKILL_VERSION}`],
      ], { tools: names.length });
      if (!init || !tools.length)
        throw new Error('the skill\'s MCP server did not start');
    });

    // ---- approval 1 ------------------------------------------------------------------------------------------------------
    const approved = ready && await phase('approval-1', ['P4', 'P6'], async () => {
      const first = await connectAndApprove('connect-1');
      local.connections.first = first;
      const page = first.approval ?? null;
      if (!page) {
        setCheck('P4', 'UNVERIFIED', 'the approval page was not reached');
      } else {
        judge('P4', [
          [page.identityOk, `the page belongs to ${EXTENSION_ID} version ${EXTENSION_VERSION}`],
          [page.client_shown, 'it names the client "Ouroboros"'],
          [page.kinds.length > 0 && page.kinds.every((kind) => kind === 'A' || kind === 'this-approval-page') && page.kinds.filter((kind) => kind === 'A').length === 1, 'it lists this test\'s tab A once and no other tab'],
        ], { listed: page.kinds, warning_shown: page.warning_shown, stage: page.stage });
      }
      if (first.ok)
        setCheck('P6', 'PASS', 'Allow & select on tab A; the tools became ready', { connect_call: first.connect_call, ready_call: first.ready_call, approval_ms: page?.ms ?? null, ready_ms: first.ready_ms });
      else
        setCheck('P6', first.skill_fault ? 'FAIL' : 'UNVERIFIED', `the first approval did not complete (${first.stage}${first.code ? `, ${first.code}` : ''})`, { skill_fault: !!first.skill_fault, stage: first.stage, code: first.code ?? null });
      if (!first.ok)
        throw new Error('the first approval did not complete');
    });

    if (approved) {
      // ---- the owner: front tab A, a real click into the draft, real keystrokes -------------------------------------------
      await phase('owner-baseline', ['S1'], async () => {
        x11 = new X11({ display, xauthority, wm: process.env.SMOKE_WM });
        if (!x11.available) {
          await focusDraft();
          setCheck('S1', 'UNVERIFIED', 'xdotool is not available, so the owner\'s real keystrokes cannot be sent; the page focused its own draft');
          return;
        }
        const window = x11.chromeWindow(chrome.pid);
        if (window)
          x11.activate(window.id);
        await sleep(400);
        const before = await pageState('A');
        const clickedAt = Date.now();
        x11.click(...Object.values(draftPoint(before.state)));
        await sleep(600);
        await pageState('A');
        const hit = fixture.events('A').some((event) => event.kind === 'pointerdown' && event.t >= clickedAt && event.trusted === true && event.target === 'draft');
        if (!hit)
          await focusDraft();
        const typed = await typeIntoDraft(TEXT1, TEXT1);
        typed1 = typed.state.draft?.value === TEXT1;
        judge('S1', [
          [typed.state.visibility === 'visible', 'A is visible'],
          [typed.state.active === 'draft', 'the draft is the focused element'],
          [typed1, `the draft holds "${TEXT1}"`],
          [typed.trusted >= TEXT1.length, 'A saw a trusted keydown in the draft for every typed character'],
        ], { window_found: !!window, owner_click_hit_draft: hit, keys: typed.keys, trusted_keys: typed.trusted, has_focus: typed.state.hasFocus });
      });

      // ---- B in the background, A current again --------------------------------------------------------------------------
      await phase('background-tab', ['S2', 'S3'], async () => {
        const openedAt = Date.now();
        const opened = await call('browser_tabs', { action: 'new', url: fixture.urlB }, 'tabs-new-B');
        const bLoad = await fixture.waitUntil(() => fixture.reports.find((r) => r.role === 'B' && r.kind === 'load'), 30_000, 'tab B to load').catch(() => null);
        const bListening = bLoad ? await fixture.waitUntil(() => fixture.listening('B', bLoad.instance), 15_000, 'tab B to listen').then(() => true, () => false) : false;
        let tabs = tabsWithHandles(opened.raw);
        if (!tabs.some((tab) => tab.url === fixture.urlA))
          tabs = tabsWithHandles((await call('browser_tabs', { action: 'list' }, 'tabs-list-after-new')).raw);
        const aIndex = tabs.find((tab) => tab.url === fixture.urlA)?.index;
        const selected = aIndex === undefined ? null : await call('browser_tabs', { action: 'select', index: aIndex }, 'tabs-select-A');
        const listed = await call('browser_tabs', { action: 'list' }, 'tabs-list-1');
        tabs = tabsWithHandles(listed.raw);
        const tabA = tabs.find((tab) => tab.url === fixture.urlA);
        const tabB = tabs.find((tab) => tab.url === fixture.urlB);
        handles.A1 = tabA?.handle ?? null;
        handles.B1 = tabB?.handle ?? null;
        local.handles.connection_1 = { A: handles.A1, B: handles.B1 };
        const a = await pageState('A');
        const b = bListening ? await pageState('B') : null;
        const aEvents = eventsBetween('A', openedAt, a.state.t);
        const bEvents = b ? eventsBetween('B', 0, b.state.t) : [];
        judge('S2', [
          [!opened.failed, 'browser_tabs new succeeded'],
          [bListening, 'tab B loaded its page'],
          [!!selected && !selected.failed, 'browser_tabs select of A succeeded'],
          [tabA?.current === true, 'A is the current tab of the list'],
          [b?.state.visibility === 'hidden', 'B is hidden'],
          [!bEvents.some((event) => event.vis === 'visible'), 'B was never visible'],
          [a.state.visibility === 'visible', 'A is visible'],
          [a.state.active === 'draft', 'A\'s draft keeps the focus'],
          [a.state.draft?.value === draftNow(), 'A\'s draft is unchanged'],
          [badForA(aEvents).length === 0, 'A had no blur, focusout, hidden or pagehide event'],
        ], { calls: [opened.n, selected?.n ?? null, listed.n], codes: [opened.code, selected?.code ?? null, listed.code], a_events: kinds(aEvents), b_events: kinds(bEvents) });
        judge('S3', [
          [!!handles.A1 && HANDLE_FORM.test(handles.A1), 'A has a handle tab-<32 hex>'],
          [!!handles.B1 && HANDLE_FORM.test(handles.B1), 'B has a handle tab-<32 hex>'],
          [!!handles.A1 && handles.A1 !== handles.B1, 'the handles differ'],
        ], { call: listed.n, roles_listed: tabs.map((tab) => (tab.url === fixture.urlA ? 'A' : tab.url === fixture.urlB ? 'B' : 'other')) });
      });

      // ---- two captures of B by its handle, A in front --------------------------------------------------------------------
      await phase('captures', ['S4', 'S5', 'S6', 'S7', 'I1'], async () => {
        if (!handles.B1) {
          for (const id of ['S4', 'S5', 'S6', 'S7', 'I1'])
            setCheck(id, checks.get('S3').status === 'FAIL' ? 'FAIL' : 'UNVERIFIED', 'there is no handle for B to capture by');
          return;
        }
        const undisturbed = (r) => [
          [r.postA.state.visibility === 'visible', 'A stayed visible'],
          [r.postA.state.active === 'draft', 'A\'s draft kept the focus'],
          [r.postA.state.draft?.value === draftNow(), 'A\'s draft is unchanged'],
          [r.postA.state.instance === aInstance, 'A is the same document'],
          [badForA(r.aEvents).length === 0, 'A had no blur, focusout, hidden or pagehide event during the call'],
          [r.postB.state.visibility === 'hidden', 'B is still a hidden background tab after the call'],
          [focusOrInputForB(r.bEvents).length === 0, 'B got no focus and no input during the call'],
        ];
        const used = [fixture.initial.B];
        const rounds = [];
        for (const round of [1, 2]) {
          let value;
          do {
            value = random32();
          } while (used.includes(value));
          used.push(value);
          const painted = await paint('B', value);
          await pageState('A');
          await pageState('B');
          const t0 = Date.now();
          const shot = await screenshot({ tabHandle: handles.B1 }, `capture-B-${round}`, true);
          const postA = await pageState('A');
          const postB = await pageState('B');
          const r = { value, shot, postA, postB, aEvents: eventsBetween('A', t0, postA.state.t), bEvents: eventsBetween('B', t0, postB.state.t) };
          rounds.push(r);
          const events = { a_events: kinds(r.aEvents), b_events: kinds(r.bEvents), painted_while: painted.state.visibility === 'hidden' ? 'hidden' : 'visible' };
          const hiddenWhenPainted = [painted.state.visibility === 'hidden', 'B was hidden when it painted the value'];
          if (round === 1) {
            judgeCapture('S4', shot, 'B', value, handles.B1, [hiddenWhenPainted], events);
            judge('S5', undisturbed(r), { call: shot.n, ...events });
          } else {
            // A value painted only now, different from every earlier one: reading it proves a new frame of B.
            judgeCapture('S6', shot, 'B', value, handles.B1, [hiddenWhenPainted], events);
            judge('S7', undisturbed(r), { call: shot.n, ...events });
          }
        }
        judge('I1', [[rounds.every((r) => !r.bEvents.some((event) => event.kind === 'visibility' && event.vis === 'visible')), 'no visibilitychange to visible reached B during a capture']], { b_events: rounds.map((r) => kinds(r.bEvents)) });
      });

      // ---- the owner keeps typing --------------------------------------------------------------------------------------------
      await phase('owner-typing', ['S8'], async () => {
        if (!x11?.available || !typed1) {
          setCheck('S8', 'UNVERIFIED', 'no real keystrokes could be sent (xdotool missing or the baseline was not typed)');
          return;
        }
        const typed = await typeIntoDraft(TEXT2, TEXT1 + TEXT2);
        typed2 = typed.state.draft?.value === TEXT1 + TEXT2;
        const bKeys = fixture.events('B').filter((event) => event.kind === 'keydown');
        judge('S8', [
          [typed2, `A's draft reads "${TEXT1 + TEXT2}"`],
          [typed.trusted >= TEXT2.length, 'every new keystroke reached A\'s draft as a trusted event'],
          [bKeys.length === 0, 'B received no keystroke'],
          [typed.state.visibility === 'visible', 'A is visible'],
        ], { keys: typed.keys, trusted_keys: typed.trusted, b_keys: bKeys.length, has_focus: typed.state.hasFocus });
      });

      // ---- a screenshot without a handle -------------------------------------------------------------------------------------
      await phase('plain-screenshot', ['S9'], async () => {
        const value = random32();
        await paint('A', value);
        judgeCapture('S9', await screenshot({}, 'capture-current-1', true), 'A', value, null);
      });

      // ---- the owner closes B; the handle is asked about only once the close is confirmed -------------------------------------
      await phase('close-B', ['S10'], async () => {
        if (!handles.B1) {
          setCheck('S10', 'UNVERIFIED', 'there is no handle for B to ask about');
          return;
        }
        const bTarget = (await chrome.targetsNow()).find((target) => target.type === 'page' && target.url === fixture.urlB);
        if (!bTarget)
          throw new Error('tab B is not among the test browser\'s targets');
        await chrome.send('Target.closeTarget', { targetId: bTarget.targetId });
        const pageGone = await fixture.waitUntil(() => !fixture.listening('B'), 15_000, 'tab B to go').then(() => true, () => false);
        const targetGone = !!(await poll(async () => !(await chrome.targetsNow()).some((target) => target.targetId === bTarget.targetId), 15_000));
        const unlisted = await poll(async () => {
          const list = await call('browser_tabs', { action: 'list' }, 'tabs-list-after-close');
          return !list.failed && !tabsWithHandles(list.raw).some((tab) => tab.url === fixture.urlB) ? list : null;
        }, 20_000);
        if (!(pageGone && targetGone && unlisted)) {
          setCheck('S10', 'UNVERIFIED', 'the close of B was not confirmed, so its handle was not asked about', { page_gone: pageGone, target_gone: targetGone, unlisted: !!unlisted });
          return;
        }
        const shot = await screenshot({ tabHandle: handles.B1 }, 'capture-B-after-close', false);
        judge('S10', refusal(shot), { ...refusalEvidence(shot), page_gone: pageGone, target_gone: targetGone, unlisted_call: unlisted.n });
      });

      // ---- browser_disconnect and the second ordinary approval -------------------------------------------------------------
      await phase('reconnect', ['S11', 'S12', 'S13', 'S14', 'S15', 'S16'], async () => {
        const disconnect = await call('browser_disconnect', {}, 'disconnect');
        await sleep(2_000);
        const afterDisconnect = await call('browser_tabs', { action: 'list' }, 'tabs-list-after-disconnect');
        const second = await connectAndApprove('connect-2');
        local.connections.second = second;
        judge('S11', [
          [!disconnect.failed && disconnect.raw.includes('### Disconnected'), 'browser_disconnect answered "Disconnected"'],
          [afterDisconnect.failed && afterDisconnect.code === 'BROWSER_DISCONNECTED', 'a tool call after it answered BROWSER_DISCONNECTED'],
          [second.new_link === true, 'browser_connect gave a new link'],
          [second.approval?.identityOk === true && (second.approval?.kinds ?? []).every((kind) => kind === 'A' || kind === 'this-approval-page'), 'the approval page was the pinned extension\'s, listing only tab A'],
          [second.ok, 'Allow & select on tab A; the tools became ready'],
        ], { calls: [disconnect.n, afterDisconnect.n, second.connect_call, second.ready_call ?? null], codes: [disconnect.code, afterDisconnect.code, second.code ?? null], stage: second.stage, previous_code: second.previous_code ?? null, listed: second.approval?.kinds ?? null });
        if (!second.ok) {
          for (const id of ['S12', 'S13', 'S14', 'S15', 'S16'])
            setCheck(id, second.skill_fault ? 'FAIL' : 'UNVERIFIED', `there was no second connection (${second.stage})`);
          return;
        }
        const tabA2 = second.tabs.find((tab) => tab.url === fixture.urlA);
        handles.A2 = tabA2?.handle ?? null;
        local.handles.connection_2 = { A: handles.A2 };
        if (handles.A1) {
          const oldA = await screenshot({ tabHandle: handles.A1 }, 'capture-old-handle-A', false);
          judge('S12', [
            ...refusal(oldA),
            [!!tabA2, 'A is listed in the new connection'],
            [!!handles.A2 && HANDLE_FORM.test(handles.A2) && handles.A2 !== handles.A1, 'A has a new handle there'],
          ], refusalEvidence(oldA));
        } else {
          setCheck('S12', 'UNVERIFIED', 'the first connection gave no handle for A to ask about');
        }
        if (handles.B1) {
          const oldB = await screenshot({ tabHandle: handles.B1 }, 'capture-old-handle-B', false);
          judge('S13', [...refusal(oldB), [!second.tabs.some((tab) => tab.url === fixture.urlB), 'B is not listed']], refusalEvidence(oldB));
        } else {
          setCheck('S13', 'UNVERIFIED', 'the first connection gave no handle for B to ask about');
        }
        const value = random32();
        await paint('A', value);
        if (handles.A2)
          judgeCapture('S14', await screenshot({ tabHandle: handles.A2 }, 'capture-new-handle-A', true), 'A', value, handles.A2);
        else
          setCheck('S14', 'FAIL', 'not so: the new connection listed no handle for A');
        judgeCapture('S15', await screenshot({}, 'capture-current-2', true), 'A', value, null);
        const aNow = await pageState('A');
        const aLoads = new Set(fixture.reports.filter((r) => r.role === 'A' && r.kind === 'load').map((r) => r.instance));
        judge('S16', [
          [aNow.state.instance === aInstance, 'A is the document it was at the start'],
          [aLoads.size === 1, 'A loaded only once'],
          [aNow.state.draft?.value === draftNow(), 'A\'s draft holds everything the owner typed'],
        ], { draft_length: aNow.state.draft?.value.length ?? null, has_focus: aNow.state.hasFocus, visibility: aNow.state.visibility === 'visible' ? 'visible' : 'hidden' });
      });
    }
  } catch (error) {
    local.errors.push(redact(String(error?.stack || error)));
    note('error (details in the local diagnostics)');
  } finally {
    try {
      if (mcp && !mcp.exit)
        await call('browser_disconnect', {}, 'disconnect-final');
    } catch {}
    try {
      local.mcp_server_exit = mcp ? await mcp.close() : null;
    } catch {}
    try {
      local.chrome_exit = chrome ? await chrome.close() : null;
    } catch {}
    await fixture?.close().catch(() => {});
    await guard?.close().catch(() => {});
    await chrome?.endLog().catch(() => {});
  }
  return await finish();
}

// report.md: the published summary as a table, nothing else.
function report(summary) {
  const cell = (text) => String(text ?? '').replace(/\|/g, '\\|').replace(/\n/g, ' ');
  const rows = summary.checks.map((check) => `| ${check.id} | ${check.required ? check.kind : 'informational'} | **${check.status}** | ${cell(check.title)} | ${cell(check.detail)} |`);
  const images = summary.images.map((image) => `- \`${image.file}\` (call ${image.call}): tab ${image.role}, value ${image.value}, sha256 ${image.sha256}`);
  const versions = Object.entries(summary.versions || {}).filter(([, value]) => typeof value === 'string').map(([name, value]) => `${name} ${value}`).join(', ');
  return `# hub324 tabHandle smoke: ${summary.verdict}

${summary.reason}.

- OuroborosHub commit ${summary.sha?.hub_commit ?? 'unknown'}, skills/chrome_browser tree ${summary.sha?.skill_tree ?? 'unknown'}${summary.sha?.skill_tree_is_expected === true ? ' (the expected tree)' : ''}.
- Versions: ${versions || 'unknown'}.
- Extension CRX sha256 ${summary.sha?.extension_crx_sha256 ?? 'unknown'}; Chrome for Testing zip sha256 ${summary.sha?.chrome_for_testing_zip_sha256 ?? 'unknown'}.
- ${summary.calls ?? 0} tool calls in ${summary.duration_s ?? '?'} s.

| id | kind | status | check | detail |
|---|---|---|---|---|
${rows.join('\n')}

## Images

Only images that read as the expected synthetic tab and value are kept; each came back in a \`tools/call\` result.

${images.length ? images.join('\n') : '- none'}
`;
}

main().then((verdict) => {
  process.exitCode = verdict === 'PASS' ? 0 : verdict === 'FAIL' ? 1 : 2;
}, () => {
  process.stderr.write('hub324-tabhandle-smoke could not run (details in the local diagnostics, if any)\n');
  process.exitCode = 3;
});
