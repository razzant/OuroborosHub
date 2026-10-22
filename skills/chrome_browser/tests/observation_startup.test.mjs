import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { pageAgent } from '../extension/page_agent.js';
import { BrowserError, Executor } from '../lib/executor.mjs';
import { Listener } from '../lib/pairing.mjs';

const host = () => new Executor({
  OUROBOROS_BROWSER_PROTOCOL: 'ouroboros_chrome',
  OUROBOROS_BROWSER_OWNER_TASK_ID: 'test', OUROBOROS_BROWSER_OWNER_ATTEMPT: '1',
});

test('the same executor re-reads temporary pairing after an unpaired first state', async () => {
  const dir = fs.mkdtempSync(path.join(path.dirname(fileURLToPath(import.meta.url)), '.pairing-test-'));
  const executor = new Executor({ ...host().env, OUROBOROS_CHROME_STATE_DIR: dir });
  const originalStart = Listener.prototype.start;
  let starts = 0;
  let finish;
  Listener.prototype.start = async function () {
    starts += 1;
    await new Promise((resolve) => { finish = resolve; });
    return 'listening';
  };
  try {
    await executor.ensureListener();
    assert.equal(executor.state().pairing.credential_present, false);
    const file = path.join(dir, 'pairing.json');
    const pairing = { credential: 'A'.repeat(64), port: 47831 };
    fs.writeFileSync(file, JSON.stringify(pairing), { mode: 0o600 });
    await executor.ensureListener();
    assert.equal(executor.state().pairing.credential_present, true);
    assert.equal(executor.state().pairing.origin_pinned, false);
    fs.writeFileSync(file, JSON.stringify({ ...pairing, pinned_origin: `chrome-extension://${'a'.repeat(32)}` }), { mode: 0o600 });
    const first = executor.ensureListener();
    const second = executor.ensureListener();
    const deadline = Date.now() + 2000;
    while (!starts && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 5));
    assert.equal(starts, 1);
    finish();
    await Promise.all([first, second]);
    assert.equal(executor.state().pairing.listener, 'listening');
    await executor.ensureListener();
    assert.equal(starts, 1);
  } finally {
    Listener.prototype.start = originalStart;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('a rejected start without a listener can retry', async () => {
  const executor = host();
  let starts = 0;
  executor.startListener = async () => {
    if (++starts === 1) throw new Error('temporary start failure');
    executor.listener = { server: {} };
    executor.listenerStatus = 'listening';
  };
  await assert.rejects(executor.ensureListener(), /temporary start failure/);
  await executor.ensureListener();
  assert.equal(starts, 2);
  assert.equal(executor.state().pairing.listener, 'listening');
});

test('revoked and transport-lost executors remain closed without replaying an action', async () => {
  for (const [flag, expected] of [['revoked', 'BROWSER_SESSION_REVOKED'], ['transportLost', 'BROWSER_SESSION_DETACHED']]) {
    const executor = host();
    if (flag === 'revoked') executor.closure.revoked = true;
    else executor.transportLost = true;
    let starts = 0;
    let sent = 0;
    executor.startListener = async () => { starts += 1; };
    executor.guarded = async (build) => { build(); return ''; };
    executor.rpc = async () => { sent += 1; return {}; };
    await executor.ensureListener();
    await assert.rejects(executor.connect({ url: 'https://example.test/' }, 'connect'), (error) => error.code === expected);
    const invocation = { closing: false, stepSeq: 0, steps: [], pending: new Set() };
    await assert.rejects(executor.runPrimitive(invocation, 'read', {}), (error) => error.code === expected);
    assert.equal(executor.state().phase, 'closed');
    assert.equal(starts, 0);
    assert.equal(sent, 0);

    const afterUnpaired = host();
    let retries = 0;
    afterUnpaired.startListener = async () => { retries += 1; };
    await afterUnpaired.ensureListener();
    if (flag === 'revoked') afterUnpaired.closure.revoked = true;
    else afterUnpaired.transportLost = true;
    await afterUnpaired.ensureListener();
    assert.equal(retries, 1, 'a closed executor must not retry an unconfigured listener');
  }
});

test('a listening listener is never started twice', async () => {
  const executor = host();
  let starts = 0;
  executor.startListener = async () => { starts += 1; executor.listener = { server: {} }; executor.listenerStatus = 'listening'; };
  await executor.ensureListener();
  await executor.ensureListener();
  assert.equal(starts, 1);
});

test('a busy port is BROWSER_PORT_BUSY now and tried again on the next call', async () => {
  // Another task's executor holds the port; when it releases the port, this executor's next call binds it.
  const dir = fs.mkdtempSync(path.join(path.dirname(fileURLToPath(import.meta.url)), '.pairing-test-'));
  fs.writeFileSync(path.join(dir, 'pairing.json'),
    JSON.stringify({ credential: 'A'.repeat(64), port: 47831, pinned_origin: `chrome-extension://${'a'.repeat(32)}` }), { mode: 0o600 });
  const executor = new Executor({ ...host().env, OUROBOROS_CHROME_STATE_DIR: dir });
  const originalStart = Listener.prototype.start;
  const outcomes = ['busy', 'listening'];
  let starts = 0;
  Listener.prototype.start = async function () { starts += 1; return outcomes.shift() || 'listening'; };
  try {
    await executor.ensureListener();
    assert.equal(executor.state().pairing.listener, 'busy');
    await assert.rejects(executor.waitPaired(), (error) => error.code === 'BROWSER_PORT_BUSY' && /tried again/.test(error.message));
    await executor.ensureListener();
    assert.equal(starts, 2, 'a busy port is tried again');
    assert.equal(executor.state().pairing.listener, 'listening');
    await executor.ensureListener();
    assert.equal(starts, 2, 'a listening listener is kept');
  } finally {
    Listener.prototype.start = originalStart;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('page text accepts an addressable window and reports exact coverage', () => {
  globalThis.document = { body: { tagName: 'BODY', innerText: 'abcdefghijkl' } };
  try {
    const value = pageAgent('text', { start: 4, max_chars: 3 }).value;
    assert.equal(value.text, 'efg');
    assert.equal(value.length, 12);
    assert.deepEqual(value.coverage, { start: 4, end: 7, total_length: 12, complete: false });
  } finally { delete globalThis.document; }
});

test('default text stays at 20000 characters and later offsets remain addressable', () => {
  globalThis.document = { body: { tagName: 'BODY', innerText: 'a'.repeat(20000) + 'needle' + 'z'.repeat(18434) } };
  try {
    const first = pageAgent('text', {}).value;
    assert.equal(first.text.length, 20000);
    assert.equal(first.length, 38440);
    assert.deepEqual(first.coverage, { start: 0, end: 20000, total_length: 38440, complete: false });
    const later = pageAgent('text', { start: 19998, max_chars: 12 }).value;
    assert.equal(later.text, 'aaneedlezzzz');
    assert.deepEqual(later.coverage, { start: 19998, end: 20010, total_length: 38440, complete: false });
  } finally { delete globalThis.document; }
});

test('an explicit capture keeps the larger window while the read returns its requested small slice', async () => {
  const executor = host();
  const lease = { lease_id: 'l', generation: 1, tab_id: 'tab-1', document_id: 'doc-1', url: 'https://example.test/one' };
  executor.usableLease = () => lease;
  executor.isCurrent = () => true;
  executor.guarded = async (build) => { build(); return ''; };
  executor.settleGuard = async () => {};
  let sent;
  executor.rpc = async (message) => {
    sent = message;
    globalThis.document = { body: { tagName: 'BODY', innerText: 'abcdefghijklmno' } };
    try {
      return { ok: true, outcome: 'observed', lease_id: 'l', generation: 1,
        value: pageAgent('text', { start: message.args.start, max_chars: message.args.max_chars }).value };
    } finally { delete globalThis.document; }
  };
  const invocation = { id: 'read-source', closing: false, stepSeq: 0, steps: [], pending: new Set() };
  const value = await executor.runPrimitive(invocation, 'read', { cache_key: 'raw', max_chars: 3, capture_chars: 10 });
  assert.deepEqual(sent.args, { cache_key: 'raw', max_chars: 10, capture_chars: 10, start: 0 });
  assert.equal(value.text, 'abc');
  assert.deepEqual(value.coverage, { start: 0, end: 3, total_length: 15, complete: false });
  assert.deepEqual(value.cached.coverage, { start: 0, end: 10, total_length: 15, complete: false });
  assert.deepEqual(value.cached.source, { observation_id: 'read-source:1', operation: 'read',
    executor_generation: executor.generation, lease_id: 'l', generation: 1,
    tab_id: 'tab-1', document_id: 'doc-1', url: 'https://example.test/one' });
  value.cached.source.document_id = 'script-mutated';
  lease.document_id = 'doc-2';
  assert.equal(executor.cachedText({ cache_key: 'raw' }).source.document_id, 'doc-1');
  value.cached.coverage.end = 0; // a script can edit its result without changing the stored record
  assert.equal(executor.cachedText({ cache_key: 'raw', start: 7, max_chars: 3 }).text, 'hij');
  const later = await executor.runPrimitive(invocation, 'read', { start: 7, max_chars: 3 });
  assert.equal(later.text, 'hij');
  assert.deepEqual(later.coverage, { start: 7, end: 10, total_length: 15, complete: false });
  assert.throws(() => executor.cachedText({ cache_key: 'raw', start: 11 }), (error) => error instanceof BrowserError && error.code === 'BROWSER_CACHE_MISS');
  const found = executor.cachedText({ cache_key: 'raw', find: 'missing' });
  assert.equal(found.complete, false);
  assert.deepEqual(found.coverage, { start: 0, end: 10, total_length: 15, complete: false });

  await executor.runPrimitive(invocation, 'get_text', {
    selector: '#result', start: 4, max_chars: 2, capture_chars: 7, cache_key: 'target',
  });
  assert.deepEqual(sent.args, {
    selector: '#result', start: 4, max_chars: 7, capture_chars: 7, cache_key: 'target',
  });
});

test('nonzero cached windows report source coverage and refuse uncaptured offsets', () => {
  const executor = host();
  const cached = executor.remember('window', { text: 'fghijkl', length: 15, coverage: { start: 5, end: 12, total_length: 15, complete: false } });
  assert.deepEqual(cached.coverage, { start: 5, end: 12, total_length: 15, complete: false });
  assert.equal(executor.cachedText({ cache_key: 'window', start: 10, max_chars: 2 }).text, 'kl');
  assert.equal(executor.cachedText({ cache_key: 'window' }).start, 5);
  for (const start of [4, 12, 13])
    assert.throws(() => executor.cachedText({ cache_key: 'window', start }), (error) => error.code === 'BROWSER_CACHE_MISS');
});

test('invalid start and capture refuse before dispatch', async () => {
  const executor = host();
  executor.usableLease = () => ({ lease_id: 'l', generation: 1 });
  executor.isCurrent = () => true;
  executor.guarded = async (build) => { build(); return ''; };
  let sent = 0;
  executor.rpc = async () => { sent += 1; return {}; };
  for (const args of [{ start: -1 }, { start: 1.5 }, { cache_key: 'x', capture_chars: 200001 },
    { cache_key: 'x', max_chars: 10, capture_chars: 5 }, { capture_chars: 20000 }]) {
    const invocation = { closing: false, stepSeq: 0, steps: [], pending: new Set() };
    await assert.rejects(executor.runPrimitive(invocation, 'read', args), (error) => error.code === 'BROWSER_ARGUMENT_INVALID');
    assert.equal(invocation.steps[0].outcome, 'not_dispatched');
  }
  assert.equal(sent, 0);
});

test('historical string cache values remain readable', () => {
  const executor = host();
  executor.cache.set('old', 'abcdefgh');
  assert.equal(executor.cachedText({ cache_key: 'old', start: 3, max_chars: 2 }).text, 'de');
  assert.equal(executor.cachedText({ cache_key: 'old', start: 100 }).text, '');
  assert.deepEqual(executor.cachedText({ cache_key: 'old', start: 1.5, max_chars: 2 }), {
    cache_key: 'old', length: 8, start: 1.5, text: 'bc', truncated: true,
    coverage: { start: 0, end: 8, total_length: 8, complete: true }, complete: true,
  });
});

test('browser_execute is bounded to 50 s by default, below the host per-call limit; an override is clamped to 600 s', () => {
  assert.equal(host().executeTimeout, 50000);
  assert.equal(new Executor({ ...host().env, OUROBOROS_CHROME_EXECUTE_TIMEOUT_MS: '1500' }).executeTimeout, 1500);
  assert.equal(new Executor({ ...host().env, OUROBOROS_CHROME_EXECUTE_TIMEOUT_MS: '900000' }).executeTimeout, 600000);
  assert.equal(new Executor({ ...host().env, OUROBOROS_CHROME_EXECUTE_TIMEOUT_MS: 'soon' }).executeTimeout, 50000);
});
