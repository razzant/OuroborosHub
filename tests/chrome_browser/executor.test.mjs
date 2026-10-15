// Lifecycle of the Node executor against a scripted extension and a real guard
// socket. No browser: each case drives the exact message order that broke.
//
//   node --test tests/chrome_browser/executor.test.mjs

import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';

import { SKILL, sleep, until } from './harness.mjs';

const { Executor } = await import(pathToFileURL(path.join(SKILL, 'lib', 'executor.mjs')).href);

async function guardServer() {
  const dir = fs.mkdtempSync('/tmp/ob-cb-unit-');
  const socketPath = path.join(dir, 'g.sock');
  const guard = { socketPath, facts: [], policy: () => '' };
  const server = net.createServer({ allowHalfOpen: true }, (socket) => {
    const chunks = [];
    socket.on('data', (chunk) => chunks.push(chunk));
    socket.on('end', async () => {
      const facts = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      guard.facts.push(facts);
      const block = await guard.policy(facts);
      socket.end(`${JSON.stringify({ block })}\n`);
    });
    socket.on('error', () => {});
  });
  await new Promise((resolve) => server.listen(socketPath, resolve));
  guard.close = () => new Promise((resolve) => server.close(() => { fs.rmSync(dir, { recursive: true, force: true }); resolve(); }));
  return guard;
}

/** The paired extension socket, scripted: `answer[t](message)` returns the reply, or null for none. */
class FakeExtension extends EventEmitter {
  constructor() {
    super();
    this.sent = [];
    this.answer = {
      close: (message) => ({ t: 'closed', id: message.id, invocation_id: message.invocation_id }),
      release: (message) => ({ t: 'released', id: message.id, detached: true, closed_tab: false, error: '' }),
      attach: (message) => ({
        t: 'attached', id: message.id, ok: true, lease_id: message.lease_id, generation: message.generation,
        tab_id: message.tab_id, document_id: 'D-resumed', url: 'http://fixture/a.html', guard_armed: true, created: false,
      }),
      step: (message) => ({ t: 'step_result', id: message.id, lease_id: message.lease_id, generation: message.generation, ok: true, outcome: 'observed', value: {} }),
    };
  }

  send(text) {
    const message = JSON.parse(text);
    this.sent.push(message);
    const reply = this.answer[message.t];
    if (reply) setImmediate(async () => { const out = await reply(message); if (out) this.push(out); });
  }

  push(message) { this.emit('message', Buffer.from(JSON.stringify(message))); }
  terminate() { this.emit('close'); }
}

async function setup(t) {
  const guard = await guardServer();
  const executor = new Executor({
    OUROBOROS_BROWSER_PROTOCOL: 'ouroboros_chrome', OUROBOROS_BROWSER_GUARD: guard.socketPath,
    OUROBOROS_BROWSER_OWNER_TASK_ID: 'task-unit', OUROBOROS_BROWSER_OWNER_ATTEMPT: '1',
  });
  const ext = new FakeExtension();
  executor.onPaired(ext);
  executor.lease = { lease_id: 'l1', generation: 1, tab_id: '7', document_id: 'D1', url: 'http://fixture/a.html', created: false };
  executor.leaseGeneration = 1;
  t.after(async () => { ext.emit('close'); await guard.close(); });
  return { guard, executor, ext };
}

const nav = { t: 'nav', previous: { lease_id: 'l1', generation: 1, document_id: 'D1' }, lease_id: 'l2', generation: 2,
  document_id: 'D2', url: 'http://fixture/second.html', request_id: '1:r1' };

test('request forwarding preserves explicit body booleans and leaves missing metadata unknown', async (t) => {
  const { guard, executor } = await setup(t);
  for (const [index, body] of [true, false, undefined].entries()) {
    await executor.onRequest({ lease_id: 'l1', generation: 1, request: {
      request_id: `body-${index}`, url: 'http://fixture/resource', method: 'GET',
      post_data: null, complete: true, has_post_data: body, resource_type: 'XHR',
    } });
  }
  assert.deepEqual(guard.facts.filter((fact) => fact.kind === 'request').map((fact) => fact.has_post_data),
    [true, false, null]);
});

test('a document the host refuses after an admitted step fails the call and keeps the step\'s dispatched outcome', async (t) => {
  const { guard, executor, ext } = await setup(t);
  guard.policy = (facts) => (facts.kind === 'document' ? 'BROWSER_REQUEST_GUARD_UNAVAILABLE: guard socket ECONNRESET' : '');
  ext.answer.step = async (message) => {
    ext.push(nav);  // the click's commit reaches the executor before the step's own answer
    await sleep(30);
    return { t: 'step_result', id: message.id, lease_id: message.lease_id, generation: message.generation, ok: true, outcome: 'dispatched', value: { navigated: null } };
  };
  const receipt = await executor.action('click', { selector: '#go' }, 'inv-b');
  const json = JSON.parse(receipt.text);
  assert.equal(receipt.isError, true, receipt.text);
  assert.equal(json.ok, false);
  assert.equal(json.error.code, 'BROWSER_REQUEST_GUARD_UNAVAILABLE');
  assert.match(json.error.message, /committed http:\/\/fixture\/second\.html/);
  assert.deepEqual(json.steps.map((step) => [step.operation, step.outcome, step.error && step.error.code]),
    [['click', 'dispatched', 'BROWSER_REQUEST_GUARD_UNAVAILABLE'], ['document', 'observed', 'BROWSER_REQUEST_GUARD_UNAVAILABLE']]);
  assert.deepEqual([json.state.connected, json.state.lease], [false, null]);
  assert.equal(executor.state().last_closure.reason, 'navigation_refused');
  assert.ok(ext.sent.some((message) => message.t === 'release' && message.reason === 'navigation_refused'));

  // A script that catches the error and returns normally still gets a failed receipt.
  executor.lease = { lease_id: 'l3', generation: 3, tab_id: '7', document_id: 'D3', url: 'http://fixture/a.html', created: false };
  ext.answer.step = async (message) => {
    ext.push({ ...nav, previous: { lease_id: 'l3', generation: 3, document_id: 'D3' }, lease_id: 'l4', generation: 4, document_id: 'D4' });
    await sleep(30);
    return { t: 'step_result', id: message.id, lease_id: message.lease_id, generation: message.generation, ok: true, outcome: 'dispatched', value: {} };
  };
  const caught = await executor.execute('try { await browser.click("#go"); } catch (error) { return "caught"; } return "returned";', 'inv-b2');
  assert.equal(caught.isError, true, caught.text);
  assert.equal(JSON.parse(caught.text).partial_result, 'caught');
});

test('a same-document URL change while the host decides a document refusal does not keep the refused lease', async (t) => {
  const { guard, executor, ext } = await setup(t);
  guard.policy = async (facts) => {
    if (facts.kind !== 'document') return '';
    ext.push({ t: 'url', lease_id: 'l2', generation: 2, url: 'http://fixture/second.html#later' });  // pushState in the new document
    await sleep(50);
    return 'BROWSER_REQUEST_BLOCKED: refused document';
  };
  ext.answer.step = async (message) => {
    ext.push(nav);
    await sleep(30);
    return { t: 'step_result', id: message.id, lease_id: message.lease_id, generation: message.generation, ok: true, outcome: 'confirmed', value: {} };
  };
  const receipt = await executor.action('navigate', { url: 'http://fixture/second.html' }, 'inv-c1');
  const json = JSON.parse(receipt.text);
  assert.deepEqual([json.state.connected, json.state.lease], [false, null], 'the refused lease is released, not kept');
  assert.equal(executor.state().last_closure.reason, 'navigation_refused');
  assert.equal(receipt.isError, true);
  assert.equal(json.error.code, 'BROWSER_REQUEST_BLOCKED');
});

test('a same-document URL change while the host decides a primitive does not refuse it; a new document does', async (t) => {
  const { guard, executor, ext } = await setup(t);
  guard.policy = async (facts) => {
    if (facts.kind === 'primitive' && facts.operation === 'get_text') {
      ext.push({ t: 'url', lease_id: 'l1', generation: 1, url: 'http://fixture/a.html#pushed' });
      await sleep(50);
    }
    return '';
  };
  const permitted = await executor.action('get_text', { selector: 'h1' }, 'inv-c2');
  assert.equal(permitted.isError, false, permitted.text);
  const lease = JSON.parse(permitted.text).state.lease;
  assert.deepEqual([lease.lease_id, lease.generation, lease.url], ['l1', 1, 'http://fixture/a.html#pushed']);

  // Control: a commit of another document while the host decides still refuses, undispatched.
  guard.policy = async (facts) => {
    if (facts.kind === 'primitive' && facts.operation === 'click') { ext.push(nav); await sleep(50); }
    return '';
  };
  const stepsBefore = ext.sent.filter((message) => message.t === 'step').length;
  const refused = await executor.action('click', { selector: '#go' }, 'inv-c3');
  const json = JSON.parse(refused.text);
  assert.equal(json.error.code, 'BROWSER_LEASE_CHANGED', refused.text);
  assert.equal(json.steps[0].outcome, 'not_dispatched');
  assert.equal(ext.sent.filter((message) => message.t === 'step').length, stepsBefore, 'nothing was sent for the old generation');
  // A late URL for the old lease changes nothing.
  ext.push({ t: 'url', lease_id: 'l1', generation: 1, url: 'http://fixture/a.html#stale' });
  await sleep(20);
  assert.deepEqual([executor.lease.lease_id, executor.lease.url], ['l2', 'http://fixture/second.html']);
});

test('a transport that closes while a release waits for its answer is a loss, not an idle executor', async (t) => {
  const { executor, ext } = await setup(t);
  ext.answer.release = () => null;  // the extension never answers
  const disconnecting = executor.disconnect();
  await until(() => ext.sent.some((message) => message.t === 'release'), { what: 'release sent' });
  ext.emit('close');
  await disconnecting;
  const state = executor.state();
  assert.deepEqual([state.transport_lost, state.phase, state.closure.detached], [true, 'closed', false]);
  assert.deepEqual(state.last_closure, { reason: 'disconnect', debugger_detach_ack: false });
  await assert.rejects(executor.connect({ tab_id: '7' }, 'inv-d'), (error) => error.code === 'BROWSER_SESSION_DETACHED');
});

test('a handoff whose tab is gone ends instead of blocking connect and disconnect', async (t) => {
  const { executor, ext } = await setup(t);
  await executor.handoffToOwner();
  assert.equal(executor.state().phase, 'handoff');
  ext.answer.attach = (message) => ({ t: 'attached', id: message.id, ok: false, error: { code: 'BROWSER_TAB_NOT_APPROVED', message: 'the owner has not shared this tab' } });
  await assert.rejects(executor.resume('inv-r'), (error) => error.code === 'BROWSER_TAB_NOT_APPROVED');
  assert.equal(executor.state().phase, 'idle', 'a closed tab leaves no handoff to resume');
  assert.equal(executor.state().handoff, undefined);

  // A live handoff can be ended by disconnect, which tells the extension to stop offering the tab.
  executor.lease = { lease_id: 'l9', generation: 9, tab_id: '8', document_id: 'D9', url: 'http://fixture/a.html', created: false };
  await executor.handoffToOwner();
  await executor.disconnect();
  const state = executor.state();
  assert.deepEqual([state.phase, state.connected, state.closure.detached], ['idle', false, true]);
  assert.deepEqual(state.last_closure, { reason: 'disconnect', debugger_detach_ack: true });
  assert.deepEqual(ext.sent.filter((message) => message.t === 'release').map((message) => [message.tab_id, message.reason]),
    [['7', 'handoff'], ['8', 'handoff'], ['8', 'disconnect']]);
});

test('a refused resume cannot restore a handoff ended by a racing disconnect', async (t) => {
  const { guard, executor, ext } = await setup(t);
  await executor.handoffToOwner();
  let answerResume;
  const held = new Promise((resolve) => { answerResume = resolve; });
  guard.policy = async (facts) => {
    if (facts.operation === 'resume') {
      await held;
      return 'BROWSER_POLICY_UNAVAILABLE: refused resume';
    }
    return '';
  };
  const resuming = executor.resume('inv-race');
  await until(() => guard.facts.some((facts) => facts.operation === 'resume'), { what: 'resume guard question' });
  await executor.disconnect();
  assert.equal(executor.state().phase, 'idle');
  answerResume();
  await assert.rejects(resuming, (error) => error.code === 'BROWSER_POLICY_UNAVAILABLE');
  assert.equal(executor.state().phase, 'idle', 'the old handoff must stay ended');
  assert.equal(executor.state().handoff, undefined);
  executor.approved.add('7');
  await executor.connect({ tab_id: '7' }, 'inv-new');
  assert.equal(executor.state().phase, 'connected', 'a new connection is not blocked by stale handoff');
  assert.ok(ext.sent.some((message) => message.t === 'release' && message.reason === 'disconnect'));
});

test('a late successful attach cannot adopt a handoff withdrawn during resume', async (t) => {
  const { executor, ext, guard } = await setup(t);
  await executor.handoffToOwner();
  let answerAttach;
  const held = new Promise((resolve) => { answerAttach = resolve; });
  ext.answer.attach = async (message) => {
    await held;
    return { t: 'attached', id: message.id, ok: true, lease_id: message.lease_id, generation: message.generation,
      tab_id: message.tab_id, document_id: 'D-late', url: 'http://fixture/a.html', guard_armed: true, created: false };
  };
  const resuming = executor.resume('inv-late');
  await until(() => ext.sent.some((message) => message.t === 'attach'), { what: 'pending resume attachment' });
  await executor.disconnect();
  answerAttach();
  await assert.rejects(resuming, (error) => error.code === 'BROWSER_LEASE_CHANGED');
  assert.equal(executor.state().phase, 'idle');
  assert.equal(executor.state().handoff, undefined);
  assert.equal(executor.state().connected, false);
  assert.equal(guard.facts.length, 0, 'no guard question for the withdrawn attachment');
  assert.ok(ext.sent.some((message) => message.t === 'release' && message.reason === 'refused'), 'late success is released');
});
