// Deterministic regressions for the executor, the extension worker and the
// setup script, against a mocked extension socket, a mocked chrome.* and a
// disposable guard socket. No Chrome, no real pairing, no network beyond a
// Unix socket in a temporary directory.
// Run: node --test --test-force-exit tests/regressions.test.mjs

import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { proof } from '../extension/bridge_protocol.mjs';
import { pageAgent } from '../extension/page_agent.js';
import { Executor } from '../lib/executor.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'ocb-'));
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
const until = async (probe, ms = 2000) => {
  const deadline = Date.now() + ms;
  while (!probe()) {
    if (Date.now() > deadline) throw new Error('condition not reached');
    await tick(5);
  }
};
test.after(() => fs.rmSync(TMP, { recursive: true, force: true }));

// ------------------------------------------------------------ guard + executor harness

let guardSeq = 0;
/** A guard that answers what `decide(question)` returns; a returned promise holds the answer back. */
async function startGuard(decide = () => '') {
  const file = path.join(TMP, `g${++guardSeq}.sock`);
  const guard = { file, questions: [], decide };
  guard.server = net.createServer({ allowHalfOpen: true }, (socket) => {
    const chunks = [];
    socket.on('data', (chunk) => chunks.push(chunk));
    socket.on('error', () => {});
    socket.on('end', async () => {
      const question = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      guard.questions.push(question);
      const block = await guard.decide(question);
      question.answered = true;
      socket.end(`${JSON.stringify({ block })}\n`);
    });
  });
  await new Promise((resolve) => guard.server.listen(file, resolve));
  return guard;
}

/** An executor paired with a fake extension socket. `ext.on.<t>` answers what the executor sends. */
async function harness({ decide, env = {} } = {}) {
  const guard = await startGuard(decide);
  const executor = new Executor({
    OUROBOROS_BROWSER_PROTOCOL: 'ouroboros_chrome', OUROBOROS_BROWSER_GUARD: guard.file,
    OUROBOROS_BROWSER_OWNER_TASK_ID: 't', OUROBOROS_BROWSER_OWNER_ATTEMPT: '1', ...env,
  });
  executor.listenerStart = Promise.resolve();
  executor.listenerStatus = 'listening';
  const socket = new EventEmitter();
  const ext = { sent: [], on: {} };
  ext.emit = (...messages) => { for (const message of messages) socket.emit('message', Buffer.from(JSON.stringify(message))); };
  socket.send = (text) => {
    const message = JSON.parse(text);
    ext.sent.push(message);
    const answer = ext.on[message.t];
    if (answer) queueMicrotask(() => answer(message));
  };
  socket.terminate = () => socket.emit('close');
  ext.drop = () => socket.emit('close');
  ext.on.release = (m) => ext.emit({ t: 'released', id: m.id, detached: true, closed_tab: false, error: '' });
  ext.on.close = (m) => ext.emit({ t: 'closed', id: m.id, invocation_id: m.invocation_id });
  ext.attached = (m, extra = {}) => ({
    t: 'attached', id: m.id, ok: true, lease_id: m.lease_id, generation: m.generation, tab_id: '7',
    document_id: 'D1', url: 'https://a.test/', guard_armed: true, created: false, ...extra,
  });
  ext.on.attach = (m) => ext.emit(ext.attached(m));
  executor.onPaired(socket);
  ext.emit({ t: 'tabs', tabs: [{ tab_id: '7' }] });
  const nav = (lease, id = 'L2', document = 'D2') => ({
    t: 'nav', previous: { lease_id: lease.lease_id, generation: lease.generation, document_id: lease.document_id },
    lease_id: id, generation: lease.generation + 1, document_id: document, url: 'https://b.test/', request_id: null,
  });
  const close = () => { socket.emit('close'); guard.server.close(); };
  return { guard, executor, ext, nav, close };
}
const code = async (promise) => promise.then(() => 'RESOLVED', (error) => error.code || String(error));
const receiptOf = (answer) => JSON.parse(answer.text);

// ------------------------------------------------------------ 1. revocation while an attachment is pending

test('a revocation that arrives before the attach answer is final; the late success is refused and released', async () => {
  const h = await harness();
  h.ext.on.attach = (m) => h.ext.emit({ t: 'lost', tab_id: '7', reason: 'owner_revoked', detached: true }, h.ext.attached(m));
  assert.equal(await code(h.executor.connect({ tab_id: '7' }, 'c1')), 'BROWSER_SESSION_REVOKED');
  const state = h.executor.state();
  assert.equal(state.connected, false);
  assert.equal(state.closure.revoked, true);
  assert.deepEqual(state.last_closure, { reason: 'owner_revoked', debugger_detach_ack: true });
  assert.ok(h.ext.sent.some((m) => m.t === 'release' && m.tab_id === '7'), 'the reported attachment is released');
  assert.equal(h.guard.questions.length, 0, 'the host is not asked about a lease that was never adopted');
  assert.equal(await code(h.executor.connect({ tab_id: '7' }, 'c2')), 'BROWSER_SESSION_REVOKED');
  h.close();
});

test('a revocation read in the same turn right after the attach answer is not lost', async () => {
  const h = await harness();
  h.ext.on.attach = (m) => h.ext.emit(h.ext.attached(m), { t: 'lost', tab_id: '7', reason: 'canceled_by_user', detached: true });
  assert.equal(await code(h.executor.connect({ tab_id: '7' }, 'c1')), 'BROWSER_SESSION_REVOKED');
  assert.equal(h.executor.state().connected, false);
  assert.equal(h.executor.state().closure.revoked, true);
  h.close();
});

test('a refused attachment keeps the detach fact the extension reported, not an assumed one', async () => {
  const h = await harness();
  h.ext.on.attach = (m) => h.ext.emit({ t: 'lost', tab_id: '7', reason: 'owner_revoked', detached: true }, {
    t: 'attached', id: m.id, ok: false, tab_id: '7', reason: 'owner_revoked', detached: false,
    error: { code: 'BROWSER_SESSION_REVOKED', message: 'the owner ended the session' },
  });
  assert.equal(await code(h.executor.connect({ tab_id: '7' }, 'c1')), 'BROWSER_SESSION_REVOKED');
  assert.deepEqual(h.executor.state().closure, { revoked: true, detached: false });
  h.close();
});

test('Forget pairing while an opened URL is being attached: the lost message names the lease, and the revocation is final', async () => {
  const h = await harness();
  // The extension reports the revocation of the tab it is opening, then drops the socket: no attach answer comes.
  h.ext.on.attach = (m) => { h.ext.emit({ t: 'lost', tab_id: '12', reason: 'owner_revoked', detached: true, lease_id: m.lease_id }); h.ext.drop(); };
  assert.equal(await code(h.executor.connect({ url: 'https://a.test/' }, 'c1')), 'BROWSER_SESSION_DETACHED');
  assert.equal(h.executor.state().connected, false);
  assert.equal(h.executor.state().closure.revoked, true);
  assert.equal(await code(h.executor.connect({ url: 'https://a.test/' }, 'c2')), 'BROWSER_SESSION_REVOKED');
  h.close();
});

test('a lost message for another tab, naming no pending lease, revokes nothing while an opened URL is attached', async () => {
  const h = await harness();
  h.ext.on.attach = (m) => h.ext.emit({ t: 'lost', tab_id: '9', reason: 'owner_revoked', detached: true }, h.ext.attached(m, { tab_id: '12', created: true }));
  await h.executor.connect({ url: 'https://a.test/' }, 'c1');
  assert.equal(h.executor.state().connected, true);
  assert.equal(h.executor.state().closure.revoked, false);
  h.close();
});

test('stopping another shared tab while one is being attached revokes nothing', async () => {
  const h = await harness();
  h.ext.on.attach = (m) => h.ext.emit({ t: 'lost', tab_id: '9', reason: 'owner_revoked', detached: true }, h.ext.attached(m));
  await h.executor.connect({ tab_id: '7' }, 'c1');
  assert.equal(h.executor.state().connected, true);
  assert.equal(h.executor.state().closure.revoked, false);
  h.close();
});

// ------------------------------------------------------------ attach answer, navigation while attaching

test('a document that commits right after the attach answer is witnessed after the connect question', async () => {
  const h = await harness();
  h.ext.on.attach = (m) => h.ext.emit(h.ext.attached(m), h.nav({ lease_id: m.lease_id, generation: m.generation, document_id: 'D1' }));
  await h.executor.connect({ tab_id: '7' }, 'c1');
  await h.executor.settleGuard();
  assert.deepEqual(h.guard.questions.map((q) => [q.kind, q.state.lease.document_id]), [['primitive', 'D1'], ['document', 'D2']]);
  assert.equal(h.guard.questions[1].previous.lease_id, h.guard.questions[0].state.lease.lease_id);
  assert.equal(h.executor.state().lease.lease_id, 'L2');
  h.close();
});

test('an attach answer for another lease is not adopted', async () => {
  const h = await harness();
  h.ext.on.attach = (m) => h.ext.emit(h.ext.attached(m, { lease_id: 'lother', generation: m.generation + 1 }));
  assert.equal(await code(h.executor.connect({ tab_id: '7' }, 'c1')), 'BROWSER_LEASE_CHANGED');
  assert.equal(h.executor.state().connected, false);
  assert.ok(h.ext.sent.some((m) => m.t === 'release'));
  h.close();
});

test('a refused connection releases the lease also after the tab moved to another document', async () => {
  let h = null;
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  h = await harness({ decide: (q) => (q.kind === 'primitive' && q.operation === 'connect' ? held : '') });
  const connecting = code(h.executor.connect({ tab_id: '7' }, 'c1'));
  await until(() => h.guard.questions.length === 1);
  h.ext.emit(h.nav(h.executor.lease));
  release('BROWSER_POLICY_UNAVAILABLE: not this tab');
  assert.equal(await connecting, 'BROWSER_POLICY_UNAVAILABLE');
  await h.executor.settleGuard();
  assert.equal(h.executor.state().connected, false);
  assert.ok(h.ext.sent.some((m) => m.t === 'release' && m.reason === 'refused'));
  h.close();
});

test('ordinary refused resume stays retryable, but disconnect invalidates the handoff during a refused resume', async () => {
  let releaseGuard;
  let held = false;
  const waitForGuard = new Promise((resolve) => { releaseGuard = resolve; });
  const h = await harness({ decide: (q) => q.operation === 'resume'
    ? (held ? waitForGuard : 'BROWSER_POLICY_UNAVAILABLE: refused resume') : '' });
  try {
    await h.executor.connect({ tab_id: '7' }, 'c1');
    await h.executor.handoffToOwner();
    assert.equal(await code(h.executor.resume('r1')), 'BROWSER_POLICY_UNAVAILABLE');
    assert.equal(h.executor.state().phase, 'handoff', 'an ordinary refusal keeps the handoff retryable');
    held = true;
    const retry = code(h.executor.resume('r2'));
    await until(() => h.guard.questions.some((q) => q.operation === 'resume' && q.invocation_id === 'r2'));
    await h.executor.disconnect();
    releaseGuard('BROWSER_POLICY_UNAVAILABLE: refused resume');
    assert.equal(await retry, 'BROWSER_POLICY_UNAVAILABLE');
    assert.equal(h.executor.state().phase, 'idle');
    assert.equal(h.executor.state().handoff, undefined);
    await h.executor.connect({ tab_id: '7' }, 'c2');
    assert.equal(h.executor.state().phase, 'connected');
  } finally {
    h.close();
  }
});

// ------------------------------------------------------------ 2. the guard barrier

test('the barrier waits for a document question that joined the queue while it was waiting', async () => {
  const releases = [];
  const h = await harness({ decide: (q) => (q.kind === 'primitive' ? '' : new Promise((resolve) => releases.push(resolve))) });
  await h.executor.connect({ tab_id: '7' }, 'c1');
  const lease = h.executor.lease;
  h.ext.emit({ t: 'request', lease_id: lease.lease_id, generation: lease.generation, request: { request_id: '1:a', url: 'https://a.test/x', method: 'GET', complete: true, resource_type: 'XHR', is_main_frame: false } });
  await until(() => releases.length === 1);
  let reported = null;
  const barrier = h.executor.settleGuard().then(() => { reported = h.executor.state(); });
  h.ext.emit(h.nav(lease));
  releases[0]('');
  await until(() => releases.length === 2);
  await tick(20);
  assert.equal(reported, null, 'no state is reported while the new lease is unwitnessed');
  releases[1]('');
  await barrier;
  assert.equal(reported.lease.lease_id, 'L2');
  assert.ok(h.guard.questions.find((q) => q.kind === 'document').answered);
  h.close();
});

// ------------------------------------------------------------ 3. scripts, sleeps and closure

function mcp(env) {
  const child = spawn(process.execPath, [path.join(ROOT, 'scripts', 'mcp_server.mjs')], { env, stdio: ['pipe', 'pipe', 'pipe'] });
  const lines = [];
  let buffer = '';
  let stderr = '';
  child.stdout.on('data', (chunk) => {
    buffer += chunk;
    for (let at = buffer.indexOf('\n'); at >= 0; at = buffer.indexOf('\n')) { lines.push(JSON.parse(buffer.slice(0, at))); buffer = buffer.slice(at + 1); }
  });
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  let exit = null;
  child.on('exit', (status, signal) => { exit = { status, signal }; });
  const call = async (id, name, args) => {
    child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method: 'tools/call', params: { name, arguments: args, _meta: { ouroboros_invocation_id: `i${id}` } } })}\n`);
    await until(() => lines.some((line) => line.id === id) || exit, 5000);
    return lines.find((line) => line.id === id);
  };
  return { child, call, lines, exit: () => exit, stderr: () => stderr };
}
const hostEnv = (extra = {}) => ({
  PATH: process.env.PATH, OUROBOROS_BROWSER_PROTOCOL: 'ouroboros_chrome', OUROBOROS_BROWSER_GUARD: path.join(TMP, 'none.sock'),
  OUROBOROS_BROWSER_OWNER_TASK_ID: 't', OUROBOROS_BROWSER_OWNER_ATTEMPT: '1',
  OUROBOROS_CHROME_STATE_DIR: fs.mkdtempSync(path.join(TMP, 'state-')), ...extra,
});

test('the real MCP server survives a script that drops a sleep, and one that drops a helper waiting on it', async () => {
  const server = mcp(hostEnv());
  const first = await server.call(1, 'browser_execute', { code: 'sleep(1000); return "finished";' });
  assert.equal(JSON.parse(first.result.content[0].text).result, 'finished');
  await tick(150);
  assert.equal(server.exit(), null, `the process must still run: ${server.stderr()}`);
  const second = await server.call(2, 'browser_execute', { code: 'vars.kept = browser; (async () => { await sleep(1000); })(); return "again";' });
  assert.equal(JSON.parse(second.result.content[0].text).result, 'again');
  const third = await server.call(3, 'browser_execute', { code: 'vars.kept.click("#x"); return "late";' });
  assert.equal(JSON.parse(third.result.content[0].text).result, 'late');
  await tick(150);
  assert.equal(server.exit(), null, `the process must still run: ${server.stderr()}`);
  const state = await server.call(4, 'browser_state', {});
  assert.equal(JSON.parse(state.result.content[0].text).connected, false);
  server.child.stdin.end();
});

test('the real MCP server answers JSON that is not a request object with -32600 and keeps running', async () => {
  const server = mcp(hostEnv());
  const invalid = ['null', '7', '"text"', 'true', '[]', '[{"jsonrpc":"2.0","id":1,"method":"ping"}]'];
  for (const line of invalid) server.child.stdin.write(`${line}\n`);
  await until(() => server.lines.length >= invalid.length || server.exit(), 5000).catch(() => {});
  assert.equal(server.exit(), null, `the process must still run: ${server.stderr()}`);
  assert.deepEqual(server.lines.map((line) => [line.id, line.error && line.error.code]), invalid.map(() => [null, -32600]));
  const state = await server.call(1, 'browser_state', {});
  assert.equal(JSON.parse(state.result.content[0].text).connected, false);
  server.child.stdin.end();
});

test('an awaited sleep still sees the refusal when the invocation is closed', async () => {
  const h = await harness();
  await h.executor.connect({ tab_id: '7' }, 'c1');
  const running = h.executor.execute('try { await sleep(20000); return "slept"; } catch (error) { vars.seen = error.code; throw error; }', 'e1');
  await tick(30);
  await h.executor.handoffToOwner();
  const receipt = receiptOf(await running);
  assert.equal(receipt.ok, false);
  assert.equal(receipt.error.code, 'BROWSER_INVOCATION_CLOSED');
  assert.equal(h.executor.vars.seen, 'BROWSER_INVOCATION_CLOSED');
  h.close();
});

test('a handoff ends a script that waits on a promise of its own', async () => {
  const h = await harness({ env: { OUROBOROS_CHROME_EXECUTE_TIMEOUT_MS: '1500' } });
  await h.executor.connect({ tab_id: '7' }, 'c1');
  const started = Date.now();
  const running = h.executor.execute('await new Promise(() => {}); return 1;', 'e1');
  await tick(30);
  await h.executor.handoffToOwner();
  const receipt = receiptOf(await running);
  assert.equal(receipt.error.code, 'BROWSER_INVOCATION_CLOSED');
  assert.ok(Date.now() - started < 1000, 'it does not wait for the execute timeout');
  assert.equal(receipt.state.connected, false);
  h.close();
});

test('a handoff lets a script take the answer of the step it started, then cuts only what still waits', async () => {
  const h = await harness();
  await h.executor.connect({ tab_id: '7' }, 'c1');
  let running = null;
  h.ext.on.step = (m) => { running = m; };
  h.ext.on.release = (m) => {
    // As the extension does: the step it was running is answered before the release.
    if (running) h.ext.emit({ t: 'step_result', id: running.id, lease_id: running.lease_id, generation: running.generation, ok: false, outcome: 'not_dispatched', error: { code: 'BROWSER_LEASE_CHANGED', message: 'released' } });
    h.ext.emit({ t: 'released', id: m.id, detached: true, closed_tab: false, error: '' });
  };
  const answer = h.executor.execute('return await browser.wait({ selector: "#never", timeout_ms: 20000 }).catch((error) => error.code);', 'e1');
  await until(() => running);
  await h.executor.handoffToOwner();
  const receipt = receiptOf(await answer);
  assert.equal(receipt.ok, true, JSON.stringify(receipt));
  assert.equal(receipt.result, 'BROWSER_LEASE_CHANGED');
  assert.deepEqual(receipt.steps.map((step) => [step.operation, step.outcome]), [['wait', 'not_dispatched']]);
  h.close();
});

test('browser.state() in a script waits for the same barrier', async () => {
  const h = await harness();
  await h.executor.connect({ tab_id: '7' }, 'c1');
  const receipt = receiptOf(await h.executor.execute('const state = browser.state(); return [typeof state.then, (await state).connected];', 'e1'));
  assert.deepEqual(receipt.result, ['function', true]);
  h.close();
});

// ------------------------------------------------------------ receipts

/** A script whose document the host refuses after it has returned `value`. */
async function refusedAfterReturn(script) {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const h = await harness({ decide: (q) => (q.kind === 'document' ? held : '') });
  await h.executor.connect({ tab_id: '7' }, 'c1');
  const running = h.executor.execute(script, 'e1');
  await tick(10);
  h.ext.emit(h.nav(h.executor.lease));
  await until(() => h.guard.questions.some((q) => q.kind === 'document'));
  await tick(80);
  release('BROWSER_POLICY_UNAVAILABLE: not that document');
  const answer = await running;
  h.close();
  return receiptOf(answer);
}

test('a partial result of 0 is reported, not dropped', async () => {
  const receipt = await refusedAfterReturn('await sleep(30); return 0;');
  assert.equal(receipt.ok, false);
  assert.equal(receipt.error.code, 'BROWSER_POLICY_UNAVAILABLE');
  assert.ok('partial_result' in receipt);
  assert.equal(receipt.partial_result, 0);
});

test('a partial result that is not JSON still leaves a receipt with the host\'s code and the steps', async () => {
  const receipt = await refusedAfterReturn('await sleep(30); const loop = {}; loop.self = loop; return loop;');
  assert.equal(receipt.ok, false);
  assert.equal(receipt.error.code, 'BROWSER_POLICY_UNAVAILABLE');
  assert.equal('partial_result' in receipt, false);
  assert.ok(receipt.steps.some((step) => step.operation === 'document'));
});

// ------------------------------------------------------------ host, listener, setup

test('a host that does not speak the protocol gets no listener', async () => {
  const executor = new Executor({ OUROBOROS_BROWSER_OWNER_TASK_ID: 't', OUROBOROS_BROWSER_OWNER_ATTEMPT: '1', OUROBOROS_CHROME_STATE_DIR: TMP });
  assert.equal(await code(Promise.resolve().then(() => executor.ensureListener())), 'BROWSER_POLICY_UNAVAILABLE');
  assert.equal(executor.listenerStatus, 'idle');
  const server = mcp(hostEnv({ OUROBOROS_BROWSER_PROTOCOL: 'other' }));
  const answer = await server.call(1, 'browser_handoff', {});
  const body = JSON.parse(answer.result.content[0].text);
  assert.equal(body.error.code, 'BROWSER_POLICY_UNAVAILABLE');
  assert.equal(body.state.pairing.listener, 'idle');
  server.child.stdin.end();
});

test('calls that arrive together wait for the same listener start', async () => {
  const dir = fs.mkdtempSync(path.join(TMP, 'state-'));
  fs.writeFileSync(path.join(dir, 'pairing.json'), JSON.stringify({ credential: 'A'.repeat(64), port: 1, pinned_origin: `chrome-extension://${'a'.repeat(32)}` }), { mode: 0o600 });
  const executor = new Executor({ ...hostEnv(), OUROBOROS_CHROME_STATE_DIR: dir });
  executor.ensureListener();
  await executor.ensureListener();
  // Port 1 cannot be bound and ws may be absent here: either way the second caller returns only once the start is over.
  assert.ok(executor.pairing.detail || executor.listenerStatus !== 'unconfigured', 'the second call returned before the first had finished');
  if (executor.listener) await executor.listener.close();
});

test('setup: the MCP entry is complete and names this Node; a missing dependency is one JSON object', () => {
  const env = { PATH: process.env.PATH, OUROBOROS_CHROME_STATE_DIR: fs.mkdtempSync(path.join(TMP, 'state-')) };
  const run = (...args) => spawnSync(process.execPath, [path.join(ROOT, 'scripts', 'setup.mjs'), ...args], { env, encoding: 'utf8' });
  const facts = JSON.parse(run('init', '--port', '47999').stdout);
  assert.equal(facts.mcp_server.id, 'chrome_browser');
  assert.equal(facts.mcp_server.name, 'chrome_browser');
  assert.equal(facts.mcp_server.enabled, true);
  assert.equal(facts.mcp_server.command, process.execPath);
  assert.equal(JSON.stringify(facts).includes(JSON.parse(fs.readFileSync(facts.credential_file, 'utf8')).credential), false);
  const pair = run('pair', '--wait', '5');
  const answer = JSON.parse(pair.stdout);  // ws absent: refused at once; ws present: no extension answers in 5 s
  assert.equal(answer.ok, false);
  assert.equal(pair.status, 1);
  assert.equal(pair.stderr.includes('    at '), false, 'no stack trace');
});

// ------------------------------------------------------------ page agent

test('a target that is transparent, itself or through an ancestor, is not visible', () => {
  const element = (style, parent = null, extra = {}) => ({
    tagName: 'BUTTON', id: 'go', className: '', nodeType: 1, parentElement: parent, style: { display: 'block', visibility: 'visible', opacity: '1', pointerEvents: 'auto', ...style },
    getBoundingClientRect: () => ({ x: 0, y: 0, left: 0, top: 0, right: 40, bottom: 20, width: 40, height: 20 }),
    matches: () => false, getAttribute: () => null, getRootNode: () => ({}), ...extra,
  });
  const measure = (el) => {
    Object.assign(globalThis, { document: { querySelectorAll: () => [el] }, getComputedStyle: (node) => node.style, innerWidth: 800, innerHeight: 600, scrollX: 0, scrollY: 0 });
    try { return pageAgent('measure', { target: { selector: '#go' } }).value.visible; } finally { delete globalThis.document; delete globalThis.getComputedStyle; }
  };
  assert.equal(measure(element({})), true);
  assert.equal(measure(element({ opacity: '0' })), false);
  assert.equal(measure(element({}, element({ opacity: '0' }))), false);
  let asked = null;
  assert.equal(measure(element({}, null, { checkVisibility: (options) => { asked = options; return false; } })), false);
  assert.equal(asked.opacityProperty, true);
  assert.equal(asked.visibilityProperty, true);
});

// ------------------------------------------------------------ extension worker (mocked chrome.*)

const CREDENTIAL = 'B'.repeat(64);
let workerSeq = 0;
async function worker({ closeAfterPaired = false, session = {} } = {}) {
  const events = {};
  const event = (name) => ({ addListener: (fn) => { (events[name] = events[name] || []).push(fn); }, removeListener: (fn) => { events[name] = (events[name] || []).filter((item) => item !== fn); } });
  const fire = (name, ...args) => (events[name] || []).map((fn) => fn(...args));
  const w = { sent: [], gates: {}, commands: [], detached: 0, removed: [], document: 'D1', server: null, page: {}, onInput: null, session };
  const gate = (name) => { let open; const promise = new Promise((resolve) => { open = resolve; }); w.gates[name] = { promise, open }; return w.gates[name]; };
  globalThis.chrome = {
    runtime: { id: 'ext', getURL: (name) => `chrome-extension://ext/${name}`, onMessage: event('message') },
    action: { setBadgeText: async () => {} },
    storage: {
      local: { get: async () => ({ credential: CREDENTIAL, port: 1 }), set: async () => {}, remove: async () => {} },
      // Kept across a worker restart when a test passes the same object, as chrome.storage.session is.
      session: {
        get: async (keys) => Object.fromEntries([].concat(keys).filter((key) => key in session).map((key) => [key, session[key]])),
        set: async (items) => { Object.assign(session, items); },
        remove: async (keys) => { for (const key of [].concat(keys)) delete session[key]; },
      },
    },
    alarms: { create: async () => {}, onAlarm: event('alarm') },
    tabs: {
      create: async () => ({ id: 7 }), get: async () => ({ status: 'complete' }), remove: async (id) => { w.removed.push(id); },
      onUpdated: event('updated'), onRemoved: event('removed'),
    },
    debugger: {
      attach: async () => {}, detach: async () => { w.detached += 1; },
      sendCommand: async (target, method, params) => {
        w.commands.push(method);
        if (w.gates[method]) await w.gates[method].promise;
        if (method === 'Input.dispatchMouseEvent' && w.onInput) w.onInput(params);
        if (method === 'Page.createIsolatedWorld') return { executionContextId: 1 };
        if (method === 'Page.captureScreenshot') return { data: 'fixture-frame' };
        // The page helper: answers per operation from w.page, as the injected function would.
        if (method === 'Runtime.callFunctionOn') {
          const [{ value: op }, { value: args }] = params.arguments;
          return { result: { value: w.page[op] ? { value: w.page[op](args) } : { error: { code: 'BROWSER_INTERNAL', message: `no ${op}` } } } };
        }
        return method === 'Page.getFrameTree' ? { frameTree: { frame: { id: 'F', loaderId: w.document, url: 'https://a.test/' } } } : {};
      },
      onEvent: event('event'), onDetach: event('detach'),
    },
  };
  globalThis.fetch = async (url) => ({ arrayBuffer: async () => fs.readFileSync(path.join(ROOT, 'extension', path.basename(url))) });
  globalThis.WebSocket = class {
    static OPEN = 1;
    constructor() {
      this.readyState = 0;
      this.listeners = [];
      w.server = this;
      setTimeout(() => { this.readyState = 1; this.onopen(); });
    }
    addEventListener(name, fn) { if (name === 'close') this.listeners.push(fn); }
    deliver(message) { if (this.onmessage) this.onmessage({ data: JSON.stringify(message) }); }
    drop(status = 1000) {
      if (this.readyState === 3) return;
      this.readyState = 3;
      if (this.onclose) this.onclose({ code: status });
      for (const fn of this.listeners) fn({ code: status });
    }
    close() { this.drop(); }
    async send(text) {
      const message = JSON.parse(text);
      // Answers arrive in a later turn, as on a real socket: the worker installs its handler after sending.
      if (message.t === 'hello') { this.nonce = message.nonce_e; setTimeout(() => this.deliver({ t: 'challenge', nonce_s: 'c'.repeat(32) })); return; }
      if (message.t === 'auth') {
        const answer = { t: 'paired', proof: await proof(CREDENTIAL, 'srv', this.nonce, 'c'.repeat(32)), executor_generation: 5 };
        setTimeout(() => { this.deliver(answer); if (closeAfterPaired) this.drop(); });
        return;
      }
      w.sent.push(message);
    }
  };
  await import(`${pathToFileURL(path.join(ROOT, 'extension', 'service_worker.js')).href}?worker=${++workerSeq}`);
  const popup = (message) => new Promise((resolve) => {
    fire('message', message, { id: 'ext', url: 'chrome-extension://ext/popup.html' }, resolve);
  });
  w.popup = popup;
  w.gate = gate;
  w.fire = fire;
  w.fromExecutor = (message) => w.server.deliver(message);
  await tick(60);
  return w;
}
const attachedAnswers = (w) => w.sent.filter((m) => m.t === 'attached');

test('extension: verdict timeouts drain after allowed, refused and released requests', async () => {
  const realSetTimeout = globalThis.setTimeout;
  const realClearTimeout = globalThis.clearTimeout;
  const pending = new Set();
  let armed = 0;
  globalThis.setTimeout = (callback, delay, ...args) => {
    if (delay !== 35000) return realSetTimeout(callback, delay, ...args);
    let handle;
    handle = realSetTimeout((...values) => { pending.delete(handle); callback(...values); }, delay, ...args);
    pending.add(handle);
    armed += 1;
    return handle;
  };
  globalThis.clearTimeout = (handle) => { pending.delete(handle); realClearTimeout(handle); };
  try {
    const w = await worker();
    await w.popup({ type: 'share', tab_id: 7 });
    w.fromExecutor({ t: 'attach', id: 1, lease_id: 'lA', generation: 3, tab_id: '7' });
    await until(() => attachedAnswers(w).length === 1);
    assert.equal(attachedAnswers(w)[0].ok, true);

    const pause = async (requestId) => {
      w.fire('event', { tabId: 7 }, 'Fetch.requestPaused', {
        requestId, resourceType: 'Image', frameId: 'F',
        request: { url: `https://a.test/${requestId}`, method: 'GET', hasPostData: false },
      });
      await until(() => w.sent.some((m) => m.t === 'request' && m.request.request_id === `3:${requestId}`));
      assert.equal(pending.size, 1, `${requestId} arms one verdict timer`);
    };
    await pause('allowed');
    w.fromExecutor({ t: 'verdict', request_id: '3:allowed', block: '' });
    await until(() => w.commands.includes('Fetch.continueRequest'));
    assert.equal(pending.size, 0, 'allowed verdict clears its timer');

    await pause('refused');
    w.fromExecutor({ t: 'verdict', request_id: '3:refused', block: 'BROWSER_REQUEST_BLOCKED: test' });
    await until(() => w.commands.includes('Fetch.failRequest'));
    assert.equal(pending.size, 0, 'refused verdict clears its timer');

    await pause('released');
    w.fromExecutor({ t: 'release', id: 2, tab_id: '7', reason: 'disconnect' });
    await until(() => w.sent.some((m) => m.t === 'released' && m.id === 2));
    assert.equal(pending.size, 0, 'release clears its paused request timer');
    assert.equal(w.commands.filter((method) => method === 'Fetch.failRequest').length, 2, 'each refused request is answered once');
    assert.equal(armed, 3);
  } finally {
    globalThis.setTimeout = realSetTimeout;
    globalThis.clearTimeout = realClearTimeout;
    for (const handle of pending) realClearTimeout(handle);
  }
});

test('extension: Stop sharing while Chrome is still arming the tab refuses the attachment', async () => {
  const w = await worker();
  await w.popup({ type: 'share', tab_id: 7 });
  const fetchEnable = w.gate('Fetch.enable');
  w.fromExecutor({ t: 'attach', id: 1, lease_id: 'lA', generation: 3, tab_id: '7' });
  await until(() => w.commands.includes('Fetch.enable'));
  await w.popup({ type: 'stop', tab_id: 7 });
  fetchEnable.open();
  await until(() => attachedAnswers(w).length === 1);
  const [answer] = attachedAnswers(w);
  assert.equal(answer.ok, false);
  assert.equal(answer.error.code, 'BROWSER_SESSION_REVOKED');
  assert.equal(answer.reason, 'owner_revoked');
  assert.equal(answer.detached, true);
  assert.ok(w.detached >= 1);
  assert.ok(w.sent.some((m) => m.t === 'lost' && m.reason === 'owner_revoked'));
  assert.equal((await w.popup({ type: 'status' })).lease, null);
});

test('extension: Stop sharing during the last command of the attachment refuses it too', async () => {
  const w = await worker();
  await w.popup({ type: 'share', tab_id: 7 });
  const last = w.gate('Page.getFrameTree');
  w.fromExecutor({ t: 'attach', id: 1, lease_id: 'lA', generation: 3, tab_id: '7' });
  await until(() => w.commands.includes('Page.getFrameTree'));
  await w.popup({ type: 'stop', tab_id: 7 });
  last.open();
  await until(() => attachedAnswers(w).length === 1);
  assert.equal(attachedAnswers(w)[0].ok, false);
  assert.equal(attachedAnswers(w)[0].reason, 'owner_revoked');
});

test('extension: Cancel on Chrome\'s bar while attaching an opened tab is a revocation and leaves the tab', async () => {
  const w = await worker();
  const fetchEnable = w.gate('Fetch.enable');
  w.fromExecutor({ t: 'attach', id: 1, lease_id: 'lA', generation: 3, url: 'https://a.test/' });
  await until(() => w.commands.includes('Fetch.enable'));
  w.fire('detach', { tabId: 7 }, 'canceled_by_user');
  fetchEnable.open();
  await until(() => attachedAnswers(w).length === 1);
  assert.equal(attachedAnswers(w)[0].error.code, 'BROWSER_SESSION_REVOKED');
  assert.equal(attachedAnswers(w)[0].reason, 'canceled_by_user');
  assert.deepEqual(w.removed, [], 'the tab is the owner\'s now');
});

test('extension: a document that commits while the tab is being attached is reported under the lease that was asked for', async () => {
  const w = await worker();
  await w.popup({ type: 'share', tab_id: 7 });
  const fetchEnable = w.gate('Fetch.enable');
  w.fromExecutor({ t: 'attach', id: 1, lease_id: 'lA', generation: 3, tab_id: '7' });
  await until(() => w.commands.includes('Fetch.enable'));
  w.document = 'D2';
  w.fire('event', { tabId: 7 }, 'Page.frameNavigated', { frame: { id: 'F', loaderId: 'D2', url: 'https://a.test/next' } });
  fetchEnable.open();
  await until(() => attachedAnswers(w).length === 1);
  const [answer] = attachedAnswers(w);
  assert.equal(answer.ok, true);
  assert.deepEqual([answer.lease_id, answer.generation, answer.document_id], ['lA', 3, 'D2']);
  assert.equal(w.sent.some((m) => m.t === 'nav'), false);
  assert.ok(w.commands.indexOf('Fetch.enable') < w.commands.indexOf('Page.getFrameTree'), 'requests are held before the document is read');
});

test('extension: Forget pairing while an opened tab is still loading leaves that tab to the owner', async () => {
  const w = await worker();
  globalThis.chrome.tabs.get = async () => ({ status: 'loading' });
  w.fromExecutor({ t: 'attach', id: 1, lease_id: 'lA', generation: 3, url: 'https://a.test/' });
  await tick(20);
  await w.popup({ type: 'forget' });
  w.fire('updated', 7, { status: 'complete' });
  await tick(20);
  assert.ok(w.sent.some((m) => m.t === 'lost' && m.tab_id === '7' && m.reason === 'owner_revoked' && m.lease_id === 'lA'), 'the lost message names the lease being made');
  assert.deepEqual(w.removed, [], 'the tab is the owner\'s now: dropping the socket does not close it');
  assert.deepEqual(w.commands, [], 'nothing was attached after its load finished');
});

test('extension: Forget pairing while an opened tab is loading drops its stored claim before answering; a restarted worker leaves it', async () => {
  const session = {};
  const w = await worker({ session });
  globalThis.chrome.tabs.get = async () => ({ status: 'loading' });
  w.fromExecutor({ t: 'attach', id: 1, lease_id: 'lA', generation: 3, url: 'https://a.test/' });
  await until(() => session.created_tab === 7);
  await w.popup({ type: 'forget' });
  assert.equal(session.created_tab, undefined, 'the acknowledged Forget leaves nothing that would close the owner\'s tab');
  // The worker stops before the load ends; the next one runs its start-up clean-up.
  const next = await worker({ session });
  assert.deepEqual(next.removed, [], 'the restarted worker does not close the owner\'s tab');
  w.fire('updated', 7, { status: 'complete' });
  await tick(20);
});

test('extension: an opened tab still the agent\'s when the worker restarts is closed by the new worker', async () => {
  const session = {};
  const w = await worker({ session });
  globalThis.chrome.tabs.get = async () => ({ status: 'loading' });
  w.fromExecutor({ t: 'attach', id: 1, lease_id: 'lA', generation: 3, url: 'https://a.test/' });
  await until(() => session.created_tab === 7);
  const next = await worker({ session });
  assert.deepEqual(next.removed, [7], 'an untransferred agent tab is cleaned up');
  assert.equal(session.created_tab, undefined);
  w.server.drop();
  w.fire('updated', 7, { status: 'complete' });
  await tick(20);
});

test('extension: a resume the host refuses leaves the tab with the owner, open and resumable; a disconnect or Stop ends that', async () => {
  const w = await worker();
  w.fromExecutor({ t: 'attach', id: 1, lease_id: 'lA', generation: 3, url: 'https://a.test/' });
  await until(() => attachedAnswers(w).length === 1);
  const released = (id) => until(() => w.sent.some((m) => m.t === 'released' && m.id === id));
  const resume = async (id, generation) => {
    w.fromExecutor({ t: 'attach', id, lease_id: `l${id}`, generation, tab_id: '7' });
    await until(() => attachedAnswers(w).some((m) => m.id === id));
    return attachedAnswers(w).find((m) => m.id === id);
  };
  w.fromExecutor({ t: 'release', id: 2, tab_id: '7', reason: 'handoff' });
  await released(2);
  // browser_resume: Chrome attaches, the host refuses, and the executor releases it as refused and keeps its handoff.
  assert.equal((await resume(3, 4)).ok, true);
  w.fromExecutor({ t: 'release', id: 4, tab_id: '7', reason: 'refused' });
  await released(4);
  assert.deepEqual(w.removed, [], 'the owner\'s tab stays open');
  assert.equal(w.sent.some((m) => m.t === 'lost'), false, 'no loss is reported for a tab that is still open');
  const again = await resume(5, 5);
  assert.equal(again.ok, true, `the handoff the executor kept can still be resumed: ${JSON.stringify(again)}`);
  // Controls: a resumed lease that is disconnected, or a refused resume the owner then stops, is not resumable.
  w.fromExecutor({ t: 'release', id: 6, tab_id: '7', reason: 'disconnect' });
  await released(6);
  assert.equal((await resume(7, 6)).error.code, 'BROWSER_TAB_NOT_APPROVED');
  const v = await worker();
  v.fromExecutor({ t: 'attach', id: 1, lease_id: 'lA', generation: 3, url: 'https://a.test/' });
  await until(() => attachedAnswers(v).length === 1);
  v.fromExecutor({ t: 'release', id: 2, tab_id: '7', reason: 'handoff' });
  await until(() => v.sent.some((m) => m.t === 'released' && m.id === 2));
  v.fromExecutor({ t: 'attach', id: 3, lease_id: 'l3', generation: 4, tab_id: '7' });
  await until(() => attachedAnswers(v).length === 2);
  v.fromExecutor({ t: 'release', id: 4, tab_id: '7', reason: 'refused' });
  await until(() => v.sent.some((m) => m.t === 'released' && m.id === 4));
  await v.popup({ type: 'stop', tab_id: 7 });
  v.fromExecutor({ t: 'attach', id: 5, lease_id: 'l5', generation: 5, tab_id: '7' });
  await until(() => attachedAnswers(v).length === 3);
  assert.equal(attachedAnswers(v)[2].error.code, 'BROWSER_TAB_NOT_APPROVED');
  assert.deepEqual(v.removed, []);
});

test('extension: a listener that closes right after its proof is not a connection', async () => {
  const w = await worker({ closeAfterPaired: true });
  const status = await w.popup({ type: 'status' });
  assert.equal(status.connected, false);
});

// ------------------------------------------------------------ extension: what an input step follows

/** A worker holding lease lA on tab 7 with invocation I open, and a page helper that finds one clickable button. */
async function leasedWorker() {
  const w = await worker();
  w.page = {
    scroll_into_view: () => ({}), hit: () => ({ ok: true }),
    measure: () => ({ visible: true, disabled: false, pointer_events: true, point: { x: 5, y: 5 }, rect: { x: 0, y: 0, width: 10, height: 10 }, what: 'button#send' }),
  };
  await w.popup({ type: 'share', tab_id: 7 });
  w.fromExecutor({ t: 'attach', id: 1, lease_id: 'lA', generation: 3, tab_id: '7' });
  await until(() => attachedAnswers(w).length === 1);
  w.fromExecutor({ t: 'open', invocation_id: 'I' });
  w.click = (id) => w.fromExecutor({ t: 'step', id, lease_id: 'lA', generation: 3, invocation_id: 'I', step_id: id, operation: 'click', args: { selector: '#send' } });
  w.result = (id) => w.sent.find((m) => m.t === 'step_result' && m.id === id);
  w.requested = (url) => w.fire('event', { tabId: 7 }, 'Page.frameRequestedNavigation', { frameId: 'F', reason: 'formSubmissionPost', url, disposition: 'currentTab' });
  // Chrome starts the navigation: loading, then the Document request is held for the host's verdict.
  w.start = (url, networkId) => {
    w.fire('event', { tabId: 7 }, 'Page.frameStartedLoading', { frameId: 'F' });
    w.fire('event', { tabId: 7 }, 'Fetch.requestPaused', {
      requestId: `r-${networkId}`, resourceType: 'Document', frameId: 'F', networkId, request: { url, method: 'POST', hasPostData: false },
    });
  };
  return w;
}

test('extension: a target that turns transparent, disabled or pointer-inert on its second sample is refused unsent; a stationary one is clicked', async () => {
  const w = await leasedWorker();
  const steady = w.page.measure;
  const helperCalls = () => w.commands.filter((m) => m === 'Runtime.callFunctionOn').length;
  let id = 1;
  for (const [change, expected] of [
    [{ visible: false }, 'BROWSER_TARGET_NOT_VISIBLE'], [{ disabled: true }, 'BROWSER_TARGET_DISABLED'], [{ pointer_events: false }, 'BROWSER_TARGET_OBSCURED'],
  ]) {
    let samples = 0;
    w.page.measure = () => ({ ...steady(), ...(++samples > 1 ? change : {}) });  // same box both times
    const before = helperCalls();
    w.click(++id);
    await until(() => w.result(id), 3000);
    assert.deepEqual([w.result(id).ok, w.result(id).outcome, (w.result(id).error || {}).code], [false, 'not_dispatched', expected]);
    assert.equal(helperCalls() - before, 3, 'scroll and two samples: the flags already read decide, no further page call');
  }
  assert.equal(w.commands.includes('Input.dispatchMouseEvent'), false, 'no input was sent');
  w.page.measure = steady;
  w.click(++id);
  await until(() => w.result(id), 3000);
  assert.deepEqual([w.result(id).ok, w.result(id).outcome], [true, 'dispatched']);
  assert.equal(w.commands.filter((m) => m === 'Input.dispatchMouseEvent').length, 3, 'the stationary visible enabled target is clicked once');
});

test('extension: a form post Chrome starts after the grace is followed by the click that asked for it', async () => {
  const w = await leasedWorker();
  // Announced while the click is handled; started 300 ms later, as a form submission scheduled by the page can be.
  w.onInput = (params) => {
    if (params.type !== 'mouseReleased') return;
    w.requested('https://a.test/echo');
    setTimeout(() => w.start('https://a.test/echo', 'D2'), 300);
  };
  w.click(2);
  const request = await until(() => w.sent.find((m) => m.t === 'request'), 3000).then(() => w.sent.find((m) => m.t === 'request'));
  assert.equal(w.result(2), undefined, 'the click is still open when its request is asked');
  w.fromExecutor({ t: 'verdict', request_id: request.request.request_id, block: '' });
  await until(() => w.commands.includes('Fetch.continueRequest'));
  w.fire('event', { tabId: 7 }, 'Page.frameNavigated', { frame: { id: 'F', loaderId: 'D2', url: 'https://a.test/echo' } });
  w.fire('event', { tabId: 7 }, 'Page.loadEventFired', {});
  w.fire('event', { tabId: 7 }, 'Page.frameStoppedLoading', { frameId: 'F' });
  await until(() => w.result(2), 3000);
  const answer = w.result(2);
  assert.equal(answer.ok, true, JSON.stringify(answer));
  assert.equal(answer.outcome, 'dispatched');
  assert.deepEqual(answer.value.navigation, { status: 'committed', url: 'https://a.test/echo', document_id: 'D2' });
  assert.deepEqual(answer.value.navigated, { url: 'https://a.test/echo', document_id: 'D2' });
  const order = (t) => w.sent.findIndex((m) => m.t === t && (t !== 'step_result' || m.id === 2));
  assert.ok(order('request') < order('nav') && order('nav') < order('step_result'), 'request, then commit, then the step\'s answer');
  assert.equal(w.commands.filter((m) => m === 'Input.dispatchMouseEvent').length, 3, 'one move, one press, one release: nothing is sent again');
});

test('extension: a late-started post the host refuses is the click\'s refusal; the page is kept', async () => {
  const w = await leasedWorker();
  w.onInput = (params) => {
    if (params.type !== 'mouseReleased') return;
    w.requested('https://a.test/blocked');
    setTimeout(() => w.start('https://a.test/blocked', 'D3'), 300);
  };
  w.click(2);
  await until(() => w.sent.some((m) => m.t === 'request'), 3000);
  const request = w.sent.find((m) => m.t === 'request');
  w.fromExecutor({ t: 'verdict', request_id: request.request.request_id, block: 'BROWSER_REQUEST_BLOCKED: test' });
  await until(() => w.commands.includes('Fetch.fulfillRequest'));
  w.fire('event', { tabId: 7 }, 'Page.frameStoppedLoading', { frameId: 'F' });  // Chrome abandons a 204 and keeps the page
  await until(() => w.result(2), 3000);
  const answer = w.result(2);
  assert.equal(answer.ok, true, JSON.stringify(answer));
  assert.deepEqual([answer.value.navigated, answer.value.navigation], [null, { status: 'refused', url: 'https://a.test/blocked' }]);
  assert.equal(w.sent.some((m) => m.t === 'nav'), false);
});

test('extension: a navigation requested after the click, or before it, is not the click\'s and does not hold it', async () => {
  const w = await leasedWorker();
  w.requested('https://a.test/earlier');  // announced before this step's input and never started: not this step's
  w.onInput = (params) => { if (params.type === 'mouseReleased') setTimeout(() => w.requested('https://a.test/later'), 600); };
  const started = Date.now();
  w.click(2);
  await until(() => w.result(2), 3000);
  const elapsed = Date.now() - started;
  assert.deepEqual([w.result(2).ok, w.result(2).value.navigated, w.result(2).value.navigation], [true, null, null]);
  assert.ok(elapsed < 600, `answered before the page asked for the later navigation (${elapsed} ms)`);
});

test('extension: a requested navigation that stays in the document is reported as such', async () => {
  const w = await leasedWorker();
  w.onInput = (params) => {
    if (params.type !== 'mouseReleased') return;
    w.requested('https://a.test/#part');
    setTimeout(() => w.fire('event', { tabId: 7 }, 'Page.navigatedWithinDocument', { frameId: 'F', url: 'https://a.test/#part' }), 200);
  };
  w.click(2);
  await until(() => w.result(2), 3000);
  assert.deepEqual(w.result(2).value.navigation, { status: 'same_document', url: 'https://a.test/#part' });
});

test('extension: a requested navigation that never starts is reported unknown at the bound, and the click is not sent again', async () => {
  const w = await leasedWorker();
  w.onInput = (params) => { if (params.type === 'mouseReleased') w.requested('https://a.test/never'); };
  w.click(2);
  await until(() => w.result(2), 12000);
  const answer = w.result(2);
  assert.deepEqual([answer.ok, answer.outcome, answer.value.navigated], [true, 'dispatched', null]);
  assert.deepEqual(answer.value.navigation, { status: 'unknown', url: 'https://a.test/never' });
  assert.equal(w.commands.filter((m) => m === 'Input.dispatchMouseEvent').length, 3);
});
