// Ouroboros Chrome Browser — extension service worker.
//
// It connects to the task's Node executor on loopback, proves the standing
// pairing credential, and then performs a fixed set of typed operations on one
// tab through chrome.debugger. It never lists tabs, never activates a tab or
// focuses a window, and exposes no CDP method name to its peer.

import {
  BRIDGE_VERSION, DEFAULT_PORT, OPERATIONS, UNSUPPORTED_OPERATIONS, buildDigest, normalizeCode, proof, randomHex,
} from './bridge_protocol.mjs';
import { pageAgent } from './page_agent.js';

// Every CDP method this extension can send. Nothing else leaves sendCdp().
const CDP_METHODS = new Set([
  'Page.enable', 'Page.getFrameTree', 'Page.createIsolatedWorld', 'Page.navigate', 'Page.captureScreenshot',
  'Network.enable', 'Fetch.enable', 'Fetch.continueRequest', 'Fetch.failRequest', 'Fetch.fulfillRequest',
  'Runtime.callFunctionOn', 'Accessibility.getFullAXTree',
  'Input.dispatchMouseEvent', 'Input.dispatchKeyEvent', 'Input.insertText',
]);

const AGENT_SOURCE = pageAgent.toString();
const WORLD_NAME = 'ouroboros_chrome';
const ALARM = 'ouroboros-chrome-connect';
const POPUP_URL = chrome.runtime.getURL('popup.html');
const STABLE_GAP_MS = 60;
const STABLE_TRIES = 8;
const NAVIGATION_GRACE_MS = 120;
const NAVIGATION_SETTLE_MS = 10000;
const VERDICT_TIMEOUT_MS = 35000;
const INPUT_FRAME_TIMEOUT_MS = 8000;

class BridgeError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

let socket = null;       // authenticated WebSocket, or null
let connecting = null;   // in-flight connection attempt
let executorGeneration = null;
let lastError = '';
const approved = new Set();  // tab ids the owner shared with the connected executor
const ownTabs = new Set();   // background tabs created for the agent that it still holds: the only tabs closed automatically
const handedOff = new Set(); // tabs released to the owner by a handoff: browser_resume may attach them again
let lease = null;            // the one live lease, or null
let attaching = null;        // the attachment being made: {tab_id, lease_id, ended}; `ended` is why the owner or Chrome ended it meanwhile
let openInvocation = null;   // the one invocation whose steps may run, or null

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const newLeaseId = () => `l${randomHex(8)}`;
const isOpen = (ws) => Boolean(ws) && ws.readyState === WebSocket.OPEN;
const send = (message) => {
  if (isOpen(socket)) socket.send(JSON.stringify(message));
};
const sendTabs = () => send({ t: 'tabs', tabs: Array.from(approved, (id) => ({ tab_id: String(id) })) });
const badge = (tabId, text) => chrome.action.setBadgeText({ tabId, text }).catch(() => {});

function sendCdp(tabId, method, params) {
  if (!CDP_METHODS.has(method)) return Promise.reject(new BridgeError('BROWSER_INTERNAL', `${method} is not an allowed command`));
  return chrome.debugger.sendCommand({ tabId }, method, params || {});
}

// The fence, checked before every command: a step may talk to Chrome only
// while its lease is the live one and its invocation is the open one.
function fenced(ctx) {
  const l = ctx.lease;
  if (lease !== l || !l.valid) throw new BridgeError('BROWSER_LEASE_CHANGED', 'the lease was released');
  if (!ctx.follow && (l.lease_id !== ctx.lease_id || l.generation !== ctx.generation))
    throw new BridgeError('BROWSER_LEASE_CHANGED', 'the tab moved to another document');
  if (ctx.invocation_id !== undefined && ctx.invocation_id !== openInvocation)
    throw new BridgeError('BROWSER_INVOCATION_CLOSED', 'the invocation this step belongs to has ended');
  return l;
}
function cdp(ctx, method, params) {
  return sendCdp(fenced(ctx).tab_id, method, params);
}

// ---------------------------------------------------------------- pairing

async function buildOfThisExtension() {
  return buildDigest(async (name) => new Uint8Array(await (await fetch(chrome.runtime.getURL(name))).arrayBuffer()));
}

function nextMessage(ws, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(reject, new Error('the listener did not answer')), timeoutMs);
    const finish = (settle, value) => {
      clearTimeout(timer);
      ws.onmessage = null; ws.onclose = null; ws.onerror = null;
      settle(value);
    };
    ws.onmessage = (event) => {
      try { finish(resolve, JSON.parse(event.data)); } catch (error) { finish(reject, new Error('unreadable answer')); }
    };
    ws.onclose = (event) => finish(reject, new Error(event.code === 4401 ? 'the credential was not accepted' : `closed (${event.code})`));
    ws.onerror = () => {};
  });
}

async function connectOnce() {
  const { credential, port } = await chrome.storage.local.get(['credential', 'port']);
  if (!credential) { lastError = 'No pairing credential stored.'; return false; }
  const ws = new WebSocket(`ws://127.0.0.1:${Number(port) || DEFAULT_PORT}`);
  // Permanent: nextMessage() takes the on* handlers away between handshake messages, and a close in such a gap
  // must not be missed. Before the socket is adopted this does nothing.
  ws.addEventListener('close', () => { onSocketClosed(ws); });
  try {
    await new Promise((resolve, reject) => {
      ws.onopen = resolve;
      ws.onerror = () => reject(new Error('no executor is listening'));
      ws.onclose = () => reject(new Error('no executor is listening'));
    });
    const nonceE = randomHex(16);
    ws.send(JSON.stringify({ t: 'hello', v: BRIDGE_VERSION, nonce_e: nonceE }));
    const challenge = await nextMessage(ws, 5000);
    if (challenge.t !== 'challenge' || !/^[0-9a-f]{32}$/.test(challenge.nonce_s)) throw new Error('unexpected challenge');
    ws.send(JSON.stringify({
      t: 'auth', proof: await proof(credential, 'ext', nonceE, challenge.nonce_s), build: await buildOfThisExtension(),
    }));
    const answer = await nextMessage(ws, 5000);
    if (answer.t === 'refused') throw new Error(answer.code === 'BUILD_MISMATCH'
      ? 'This extension is not the build the installed skill expects. Reload it from the skill folder.' : `refused (${answer.code})`);
    if (answer.t !== 'paired' || answer.proof !== await proof(credential, 'srv', nonceE, challenge.nonce_s))
      throw new Error('the listener on this port is not the paired Ouroboros executor');
    // The setup script's `pair` closes right after its proof: a socket that closed while the proof was checked is not a connection.
    if (!isOpen(ws)) throw new Error('the listener closed the connection after pairing');
    socket = ws;
    executorGeneration = answer.executor_generation;
    lastError = '';
    ws.onmessage = (event) => onServerMessage(ws, event);
    ws.onerror = () => {};
    sendTabs();
    return true;
  } catch (error) {
    lastError = String(error.message || error);
    try { ws.close(); } catch (closeError) { /* already closed */ }
    return false;
  }
}

function tryConnect() {
  if (isOpen(socket)) return Promise.resolve(true);
  if (socket) onSocketClosed(socket);  // closed without an event reaching us: release what it held, then connect anew
  if (!connecting) connecting = connectOnce().finally(() => { connecting = null; });
  return connecting;
}

async function onSocketClosed(ws) {
  if (socket !== ws) return;
  socket = null;
  executorGeneration = null;
  openInvocation = null;
  approved.clear();
  handedOff.clear();
  const orphans = Array.from(ownTabs).filter((id) => !lease || lease.tab_id !== id);
  ownTabs.clear();
  if (lease) await releaseLease(lease, 'socket_closed');
  await Promise.allSettled(orphans.map((id) => chrome.tabs.remove(id)));
}

function dropSocket() {
  const ws = socket;
  if (!ws) return Promise.resolve();
  const done = onSocketClosed(ws);
  try { ws.close(); } catch (error) { /* already closed */ }
  return done;
}

// A refused document request is answered 204, which makes Chrome abandon the
// navigation and keep the current document; any other request simply fails.
function refuseRequest(tabId, requestId, isDocument) {
  return isDocument
    ? sendCdp(tabId, 'Fetch.fulfillRequest', { requestId, responseCode: 204 })
    : sendCdp(tabId, 'Fetch.failRequest', { requestId, errorReason: 'BlockedByClient' });
}

// ---------------------------------------------------------------- lease

function frameUrl(frame) {
  return `${frame.url}${frame.urlFragment || ''}`;
}

function waitTabComplete(tabId, timeoutMs) {
  return new Promise((resolve) => {
    const finish = () => { clearTimeout(timer); chrome.tabs.onUpdated.removeListener(listener); resolve(); };
    const listener = (id, change) => { if (id === tabId && change.status === 'complete') finish(); };
    const timer = setTimeout(finish, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((tab) => { if (tab.status === 'complete') finish(); }, finish);
  });
}

async function handleAttach(message) {
  // A refusal says what is known: the tab, why the owner or Chrome ended the attachment (if they did), and
  // whether the debugger is confirmed detached from it.
  const refuse = (code, text, facts = {}) => send({ t: 'attached', id: message.id, ok: false, error: { code, message: text }, ...facts });
  if (lease || attaching) return refuse('BROWSER_LEASE_CHANGED', 'this extension already holds a lease');
  const pending = { tab_id: null, lease_id: String(message.lease_id), ended: '' };
  attaching = pending;
  try {
    await attachTab(message, pending, refuse);
  } finally {
    if (attaching === pending) attaching = null;
  }
}

const REVOCATIONS = ['owner_revoked', 'canceled_by_user'];
// A `lost` for the tab being attached names the lease asked for: for an opened URL the executor does not know
// the tab yet, and Forget pairing drops the socket before the attachment's answer.
const attachingFact = (tabId) => (attaching && attaching.tab_id === tabId ? { lease_id: attaching.lease_id } : {});

async function attachTab(message, pending, refuse) {
  const ended = (detached) => refuse(
    REVOCATIONS.includes(pending.ended) ? 'BROWSER_SESSION_REVOKED' : 'BROWSER_SESSION_DETACHED',
    REVOCATIONS.includes(pending.ended) ? 'the owner ended the session while the tab was being attached' : `the tab was lost while it was being attached (${pending.ended})`,
    { tab_id: String(pending.tab_id), reason: pending.ended, detached });
  let tabId;
  let created = false;
  try {
    if (typeof message.url === 'string') {
      if (!/^https?:\/\//i.test(message.url)) return refuse('BROWSER_ARGUMENT_INVALID', 'only http and https URLs can be opened');
      // The agent's own tab: created in the background, never activated.
      const tab = await chrome.tabs.create({ url: message.url, active: false });
      tabId = tab.id;
      pending.tab_id = tabId;
      created = true;
      ownTabs.add(tabId);
      await chrome.storage.session.set({ created_tab: tabId });
      await waitTabComplete(tabId, 15000);
    } else {
      tabId = Number(message.tab_id);
      pending.tab_id = tabId;
      created = ownTabs.has(tabId);
      if (!created && !approved.has(tabId) && !handedOff.has(tabId)) return refuse('BROWSER_TAB_NOT_APPROVED', 'the owner has not shared this tab');
    }
  } catch (error) {
    return refuse('BROWSER_INTERNAL', String(error.message || error));
  }
  if (pending.ended) {
    // Stopped by the owner before anything was attached: the tab is theirs now and is not closed.
    if (created) { ownTabs.delete(tabId); chrome.storage.session.remove('created_tab'); }
    return ended(true);
  }
  if (lease || socket === null) {
    if (created) chrome.tabs.remove(tabId).catch(() => {});
    return refuse('BROWSER_LEASE_CHANGED', 'the session changed while the tab was opening');
  }
  const l = {
    tab_id: tabId, lease_id: message.lease_id, generation: message.generation, attached_generation: message.generation,
    valid: true, created, reported: false, handed: handedOff.has(tabId),
    frame_id: '', document_id: '', url: '', context_id: null, context_document: '', loaded_document: '', loading: false,
    nav_requests: 0, nav_requested: '',
    dialog: null, paused: new Map(), documents: new Map(), refused_document: '', chain: Promise.resolve(),
  };
  lease = l;
  const ctx = { lease: l, lease_id: l.lease_id, generation: l.generation };
  try {
    await chrome.debugger.attach({ tabId }, '1.3');
    await chrome.storage.session.set({ attached_tab: tabId });
    // Armed first, read last. From Fetch.enable on every request is held; a document that commits before the
    // answer below replaces the facts of this same lease (onEvent), so what is reported is what the tab shows.
    await cdp(ctx, 'Page.enable');
    await cdp(ctx, 'Network.enable');
    await cdp(ctx, 'Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Request' }] });
    const { frameTree } = await cdp(ctx, 'Page.getFrameTree');
    // The owner's Stop, Cancel on Chrome's bar or a closed tab may have ended it during any of these: no late success.
    fenced(ctx);
    l.frame_id = frameTree.frame.id;
    l.document_id = frameTree.frame.loaderId;
    l.loaded_document = l.document_id;
    l.url = frameUrl(frameTree.frame);
    l.reported = true;
    badge(tabId, 'ON');
    handedOff.delete(tabId);
    send({
      t: 'attached', id: message.id, ok: true, lease_id: l.lease_id, generation: l.generation, tab_id: String(tabId),
      document_id: l.document_id, url: l.url, guard_armed: true, created,
    });
  } catch (error) {
    // Released again here even after an owner's Stop already did: an attach command that was still in flight
    // then may have completed since, and only this detach says what is attached now.
    const result = await releaseLease(l, 'refused');
    if (pending.ended) return ended(result.detached);
    refuse(error.code || 'BROWSER_INTERNAL', String(error.message || error), { tab_id: String(tabId), detached: result.detached });
  }
  return undefined;
}

// Invalidate first; only then talk to Chrome. Detaching leaves the page as it is.
// A tab the agent created is closed on release, unless the owner takes it: after
// a handoff or a revocation it is the owner's, and nothing here closes it again,
// not after a resume, an executor exit, a worker restart or Forget pairing.
async function releaseLease(l, reason) {
  // A resume the executor refuses gives the tab back to the owner, resumable as before; one the owner or Chrome
  // already ended (lease invalid) does not.
  if (reason === 'handoff' || (reason === 'refused' && l.handed && l.valid)) handedOff.add(l.tab_id);
  l.valid = false;
  const toOwner = reason === 'handoff' || reason === 'owner_revoked';
  if (toOwner && l.created) {
    l.created = false;  // synchronous: a socket close racing this release must not close it
    ownTabs.delete(l.tab_id);
  }
  const held = Array.from(l.paused.entries());
  l.paused.clear();
  for (const [, entry] of held) entry.resolve('BROWSER_SESSION_REVOKED: the lease was released');
  await Promise.allSettled(held.map(([, entry]) => refuseRequest(l.tab_id, entry.cdp_id, entry.document)));
  let detached = false;
  let error = '';
  try {
    await chrome.debugger.detach({ tabId: l.tab_id });
    detached = true;
  } catch (failure) {
    error = String(failure.message || failure);
    detached = /not attached|no tab with/i.test(error);
  }
  if (lease === l) lease = null;
  badge(l.tab_id, '');
  await chrome.storage.session.remove('attached_tab');
  let closedTab = false;
  if (l.created) {
    ownTabs.delete(l.tab_id);
    closedTab = await chrome.tabs.remove(l.tab_id).then(() => true, () => false);
  }
  if (l.created || toOwner) await chrome.storage.session.remove('created_tab');
  return { detached, closed_tab: closedTab, error };
}

function handleRelease(message) {
  const l = lease;
  if (!l || String(l.tab_id) !== String(message.tab_id)) {
    handedOff.delete(Number(message.tab_id));  // a handoff the executor ended without resuming
    send({ t: 'released', id: message.id, detached: true, closed_tab: false, error: l ? 'another tab is leased' : '' });
    return;
  }
  // releaseLease() runs up to its first await synchronously: the lease is
  // already invalid when this handler returns, before any queued step runs.
  releaseLease(l, message.reason).then((result) => send({ t: 'released', id: message.id, ...result }));
}

// ---------------------------------------------------------------- page access

async function agent(ctx, op, args) {
  const l = fenced(ctx);
  if (l.dialog) throw new BridgeError('BROWSER_DIALOG_OPEN', `a ${l.dialog.type} dialog is open: ${l.dialog.message}`);
  if (l.context_id == null || l.context_document !== l.document_id) {
    const world = await cdp(ctx, 'Page.createIsolatedWorld', { frameId: l.frame_id, worldName: WORLD_NAME });
    l.context_id = world.executionContextId;
    l.context_document = l.document_id;
  }
  let answer;
  try {
    answer = await cdp(ctx, 'Runtime.callFunctionOn', {
      functionDeclaration: AGENT_SOURCE, executionContextId: l.context_id,
      arguments: [{ value: op }, { value: args || {} }], returnByValue: true, awaitPromise: false,
    });
  } catch (error) {
    if (error instanceof BridgeError) throw error;
    l.context_id = null;
    throw new BridgeError('BROWSER_DOCUMENT_CHANGED', `the document went away: ${error.message}`);
  }
  if (answer.exceptionDetails) throw new BridgeError('BROWSER_INTERNAL', answer.exceptionDetails.text || 'page helper failed');
  const outcome = answer.result && answer.result.value;
  if (!outcome || outcome.error) {
    const failure = (outcome && outcome.error) || { code: 'BROWSER_INTERNAL', message: 'page helper returned nothing' };
    throw new BridgeError(failure.code, failure.message);
  }
  return outcome.value;
}

function targetOf(ctx, args, required = true) {
  const hasSelector = typeof args.selector === 'string' && args.selector !== '';
  const hasHandle = typeof args.handle === 'string' && args.handle !== '';
  if (hasSelector === hasHandle) {
    if (!hasSelector && !required) return null;
    throw new BridgeError('BROWSER_ARGUMENT_INVALID', 'pass exactly one of selector or handle');
  }
  if (hasHandle) {
    const match = /^h(\d+)-(\d+)$/.exec(args.handle);
    if (!match) throw new BridgeError('BROWSER_ARGUMENT_INVALID', 'not a handle from snapshot');
    if (Number(match[1]) !== ctx.lease.generation)
      throw new BridgeError('BROWSER_HANDLE_STALE', 'this handle belongs to an earlier document or attachment');
    return { handle: Number(match[2]) };
  }
  const target = { selector: args.selector };
  if (args.nth != null) {
    if (!Number.isInteger(args.nth) || args.nth < 0) throw new BridgeError('BROWSER_ARGUMENT_INVALID', 'nth must be a non-negative integer');
    target.nth = args.nth;
  }
  return target;
}

const sameRect = (a, b) => ['x', 'y', 'width', 'height'].every((key) => Math.abs(a[key] - b[key]) < 0.5);

// Every sample must show a target that can take the input: the one the point comes from too.
function usable(sample) {
  if (!sample.visible) throw new BridgeError('BROWSER_TARGET_NOT_VISIBLE', `${sample.what} has no visible box`);
  if (sample.disabled) throw new BridgeError('BROWSER_TARGET_DISABLED', `${sample.what} is disabled`);
  if (!sample.pointer_events) throw new BridgeError('BROWSER_TARGET_OBSCURED', `${sample.what} does not take pointer events`);
  if (!sample.point) throw new BridgeError('BROWSER_TARGET_NOT_VISIBLE', `${sample.what} is outside the viewport`);
  return sample;
}

// A newly loaded hidden renderer can acknowledge input without delivering any
// DOM events. In headed Chrome, a frame capture (unlike a matched delay) makes
// the first input deliverable without activating the tab or emulating focus.
// Capture once per document/attachment; discard pixels, never replay input.
async function prepareInputFrame(ctx) {
  const l = fenced(ctx);
  if (l.input_frame_generation === ctx.generation) return;
  const started = Date.now();
  let timer;
  try {
    const shot = await Promise.race([
      cdp(ctx, 'Page.captureScreenshot', { format: 'png' }),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new BridgeError('BROWSER_INPUT_NOT_READY', 'Chrome produced no input frame; no input was dispatched')), INPUT_FRAME_TIMEOUT_MS);
      }),
    ]);
    fenced(ctx);
    if (!shot || typeof shot.data !== 'string' || !shot.data)
      throw new BridgeError('BROWSER_INPUT_NOT_READY', 'Chrome returned no input frame; no input was dispatched');
    l.input_frame_generation = ctx.generation;
    ctx.input_preparation = { kind: 'renderer_frame', elapsed_ms: Date.now() - started };
  } catch (error) {
    fenced(ctx);
    if (error instanceof BridgeError) throw error;
    throw new BridgeError('BROWSER_INPUT_NOT_READY', `Chrome input frame failed: ${error.message || error}; no input was dispatched`);
  } finally {
    clearTimeout(timer);
  }
}

// Current geometry after renderer preparation, read twice a short real-time
// interval apart. No animation-frame wait or foreground activation.
async function stablePoint(ctx, target) {
  await agent(ctx, 'scroll_into_view', { target });
  await prepareInputFrame(ctx);
  let previous = usable(await agent(ctx, 'measure', { target }));
  for (let attempt = 0; attempt < STABLE_TRIES; attempt += 1) {
    await delay(STABLE_GAP_MS);
    const current = usable(await agent(ctx, 'measure', { target }));
    if (sameRect(previous.rect, current.rect)) {
      const hit = await agent(ctx, 'hit', { target, x: current.point.x, y: current.point.y });
      if (!hit.ok) throw new BridgeError('BROWSER_TARGET_OBSCURED', `${hit.receiver} would receive the input instead of ${current.what}`);
      return current.point;
    }
    previous = current;
  }
  throw new BridgeError('BROWSER_TARGET_UNSTABLE', `${previous.what} kept moving`);
}

// Called before a step's first input command: from here on, a navigation the
// main frame is asked to make (Page.frameRequestedNavigation) counts as this step's.
function markInput(ctx) {
  if (ctx.nav_mark !== undefined) return;
  ctx.nav_mark = ctx.lease.nav_requests;
  ctx.url_mark = ctx.lease.url;
  ctx.lease.refused_document = '';
}

// Input may make the page leave its document. Chrome announces a navigation
// the input asks for (a link, a form, a script) while it handles the input,
// but may start it only after a task the page schedules — a form submission
// does. The step follows every navigation announced from its first input
// command until it stops waiting: until it commits and loads, is refused, or
// ends without a commit (at most NAVIGATION_SETTLE_MS after the grace). So the
// document change is reported while the step — and its invocation — is still open.
async function settleNavigation(ctx) {
  const l = ctx.lease;
  ctx.follow = true;
  await delay(NAVIGATION_GRACE_MS);
  const deadline = Date.now() + NAVIGATION_SETTLE_MS;
  const requested = () => l.nav_requests > ctx.nav_mark;
  const pendingDocument = () => (requested() && l.nav_requested !== '') || l.loading
    || Array.from(l.paused.values()).some((entry) => entry.document);
  while (lease === l && l.valid && l.generation === ctx.generation && pendingDocument() && Date.now() < deadline) await delay(40);
  while (lease === l && l.valid && l.generation !== ctx.generation && l.loaded_document !== l.document_id && Date.now() < deadline) await delay(40);
  fenced(ctx);
  if (l.generation !== ctx.generation) {
    const navigated = { url: l.url, document_id: l.document_id };
    return { navigated, navigation: { status: 'committed', ...navigated } };
  }
  let navigation = null;
  if (l.refused_document) navigation = { status: 'refused', url: l.refused_document };
  else if (pendingDocument()) navigation = { status: 'unknown', url: l.nav_requested || null };
  else if (requested()) navigation = l.url !== ctx.url_mark ? { status: 'same_document', url: l.url } : { status: 'abandoned' };
  return { navigated: null, navigation };
}

async function pointerClick(ctx, point, clicks) {
  const at = { x: point.x, y: point.y };
  markInput(ctx);
  ctx.effect = 'partial';
  // A hidden tab aligns pointer moves to animation frames it does not produce,
  // so Chrome acknowledges a lone move only after seconds. The press that
  // follows flushes it: send the move, do not wait for it before pressing.
  const moved = cdp(ctx, 'Input.dispatchMouseEvent', { type: 'mouseMoved', ...at, button: 'none' }).catch(() => null);
  for (let count = 1; count <= clicks; count += 1) {
    await cdp(ctx, 'Input.dispatchMouseEvent', { type: 'mousePressed', ...at, button: 'left', buttons: 1, clickCount: count });
    await cdp(ctx, 'Input.dispatchMouseEvent', { type: 'mouseReleased', ...at, button: 'left', buttons: 0, clickCount: count });
  }
  await Promise.race([moved, delay(1000)]);
  ctx.effect = 'sent';
}

const NAMED_KEYS = {
  Enter: { code: 'Enter', vk: 13, text: '\r' }, Tab: { code: 'Tab', vk: 9 }, Escape: { code: 'Escape', vk: 27 },
  Backspace: { code: 'Backspace', vk: 8 }, Delete: { code: 'Delete', vk: 46 }, Space: { key: ' ', code: 'Space', vk: 32, text: ' ' },
  ArrowLeft: { code: 'ArrowLeft', vk: 37 }, ArrowUp: { code: 'ArrowUp', vk: 38 }, ArrowRight: { code: 'ArrowRight', vk: 39 },
  ArrowDown: { code: 'ArrowDown', vk: 40 }, Home: { code: 'Home', vk: 36 }, End: { code: 'End', vk: 35 },
  PageUp: { code: 'PageUp', vk: 33 }, PageDown: { code: 'PageDown', vk: 34 },
};
const MODIFIERS = { Alt: 1, Control: 2, Meta: 4, Shift: 8 };

function keyFacts(name) {
  if (NAMED_KEYS[name]) return { key: name, ...NAMED_KEYS[name] };
  if ([...name].length !== 1) throw new BridgeError('BROWSER_ARGUMENT_INVALID', `unknown key ${name}`);
  const upper = name.toUpperCase();
  if (/^[A-Z]$/.test(upper)) return { key: name, code: `Key${upper}`, vk: upper.charCodeAt(0), text: name };
  if (/^[0-9]$/.test(name)) return { key: name, code: `Digit${name}`, vk: name.charCodeAt(0), text: name };
  return { key: name, code: '', vk: 0, text: name };
}

async function pressKey(ctx, combination) {
  const parts = String(combination).split('+');
  const facts = keyFacts(parts.pop() || '+');
  let modifiers = 0;
  for (const part of parts) {
    if (!MODIFIERS[part]) throw new BridgeError('BROWSER_ARGUMENT_INVALID', `unknown modifier ${part}`);
    modifiers |= MODIFIERS[part];
  }
  const text = modifiers & ~MODIFIERS.Shift ? undefined : facts.text;
  const base = { key: facts.key, code: facts.code, windowsVirtualKeyCode: facts.vk, nativeVirtualKeyCode: facts.vk, modifiers };
  await prepareInputFrame(ctx);
  markInput(ctx);
  ctx.effect = 'partial';
  await cdp(ctx, 'Input.dispatchKeyEvent', { type: text ? 'keyDown' : 'rawKeyDown', ...base, text, unmodifiedText: text });
  await cdp(ctx, 'Input.dispatchKeyEvent', { type: 'keyUp', ...base });
  ctx.effect = 'sent';
}

function bounded(value, fallback, max) {
  if (value == null) return fallback;
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw new BridgeError('BROWSER_ARGUMENT_INVALID', 'a duration must be a non-negative number');
  return Math.min(value, max);
}

async function focusField(ctx, args) {
  const target = targetOf(ctx, args);
  const info = await agent(ctx, 'info', { target });
  if (!info.text_field) throw new BridgeError('BROWSER_ARGUMENT_INVALID', `${info.what} is not an editable text field`);
  await pointerClick(ctx, await stablePoint(ctx, target), 1);
  // The click was already dispatched. A page may redirect focus synchronously;
  // never send keyboard input to the field it chose instead.
  const after = await agent(ctx, 'info', { target });
  if (!after.focused) throw new BridgeError('BROWSER_TARGET_NOT_VISIBLE', 'the intended field did not take focus after the click; nothing was typed');
  return target;
}

const STEPS = {
  async observe(ctx) {
    const l = ctx.lease;
    return { ...(await agent(ctx, 'observe')), url: l.url, document_id: l.document_id, tab_id: String(l.tab_id) };
  },
  read: (ctx, args) => agent(ctx, 'text', { start: args.start, max_chars: args.max_chars }),
  get_text: (ctx, args) => agent(ctx, 'text', { target: targetOf(ctx, args), start: args.start, max_chars: args.max_chars }),
  async get_attribute(ctx, args) {
    if (typeof args.name !== 'string' || !args.name) throw new BridgeError('BROWSER_ARGUMENT_INVALID', 'name is required');
    return agent(ctx, 'attribute', { target: targetOf(ctx, args), name: args.name });
  },
  async snapshot(ctx, args) {
    const result = await agent(ctx, 'snapshot', { max_nodes: args.max_nodes });
    for (const element of result.elements) element.handle = `h${ctx.lease.generation}-${element.handle}`;
    return { url: ctx.lease.url, ...result };
  },
  dom_snapshot: async (ctx, args) => ({ url: ctx.lease.url, ...(await agent(ctx, 'dom_snapshot', { max_nodes: args.max_nodes })) }),
  async ax_snapshot(ctx, args) {
    const max = Math.min(Math.max(Number(args.max_nodes) || 300, 1), 2000);
    const { nodes } = await cdp(ctx, 'Accessibility.getFullAXTree', {});
    const byId = new Map(nodes.map((node) => [node.nodeId, node]));
    const lines = [];
    let truncated = false;
    const walk = (node, depth) => {
      if (!node || truncated) return;
      let next = depth;
      if (!node.ignored) {
        const role = node.role && node.role.value;
        const name = node.name && node.name.value;
        if (role && !['none', 'generic', 'InlineTextBox'].includes(role) && (name || !['StaticText', 'paragraph'].includes(role))) {
          if (lines.length >= max) { truncated = true; return; }
          const value = node.value && node.value.value;
          lines.push(`${'  '.repeat(Math.min(depth, 12))}${role}${name ? ` "${String(name).slice(0, 120)}"` : ''}${value ? ` = ${String(value).slice(0, 80)}` : ''}`);
          next = depth + 1;
        }
      }
      for (const child of node.childIds || []) walk(byId.get(child), next);
    };
    walk(nodes.find((node) => !node.parentId) || nodes[0], 0);
    return { url: ctx.lease.url, outline: lines.join('\n'), nodes: lines.length, truncated };
  },
  async screenshot(ctx, args) {
    const format = args.format === 'jpeg' ? 'jpeg' : 'png';
    const params = { format };
    if (format === 'jpeg') params.quality = Math.min(Math.max(Number(args.quality) || 80, 1), 100);
    const target = targetOf(ctx, args, false);
    if (target) {
      await agent(ctx, 'scroll_into_view', { target });
      const box = await agent(ctx, 'measure', { target });
      if (!box.visible) throw new BridgeError('BROWSER_TARGET_NOT_VISIBLE', `${box.what} has no visible box`);
      params.clip = { x: box.rect.x + box.scroll.x, y: box.rect.y + box.scroll.y, width: box.rect.width, height: box.rect.height, scale: 1 };
    }
    // Bounded: a tab Chrome is not painting may never produce a frame, and the
    // tab is not activated to make it paint.
    const shot = await Promise.race([
      cdp(ctx, 'Page.captureScreenshot', params),
      delay(bounded(args.timeout_ms, 8000, 20000)).then(() => null),
    ]);
    if (!shot) throw new BridgeError('BROWSER_SCREENSHOT_UNAVAILABLE', 'Chrome produced no frame for this tab in time');
    return { mime: `image/${format}`, data: shot.data, clipped: Boolean(target) };
  },
  async wait(ctx, args) {
    ctx.follow = true;  // read-only polling may outlive a document change
    const state = args.state == null ? 'visible' : args.state;
    if (!['attached', 'detached', 'visible', 'hidden', 'enabled'].includes(state))
      throw new BridgeError('BROWSER_ARGUMENT_INVALID', 'state must be attached, detached, visible, hidden or enabled');
    const timeout = bounded(args.timeout_ms, 5000, 30000);
    const started = Date.now();
    let polls = 0;
    for (;;) {
      let result = null;
      try {
        const target = targetOf(ctx, args, false);
        if (!target && args.text == null) throw new BridgeError('BROWSER_ARGUMENT_INVALID', 'wait needs a selector, a handle or text');
        result = await agent(ctx, 'condition', { target, state, text: args.text });
      } catch (error) {
        if (error.code !== 'BROWSER_DOCUMENT_CHANGED') throw error;
      }
      polls += 1;
      if (result && result.ok) return { satisfied: true, elapsed_ms: Date.now() - started, polls, count: result.count };
      if (Date.now() - started >= timeout)
        throw new BridgeError('BROWSER_WAIT_TIMEOUT', `condition not met after ${timeout} ms (${polls} observations)`);
      await delay(100);
    }
  },
  async navigate(ctx, args) {
    const l = ctx.lease;
    if (typeof args.url !== 'string' || !/^https?:\/\//i.test(args.url))
      throw new BridgeError('BROWSER_ARGUMENT_INVALID', 'url must be an absolute http or https URL');
    const deadline = Date.now() + bounded(args.timeout_ms, 15000, 60000);
    l.refused_document = '';
    ctx.effect = 'partial';
    const result = await cdp(ctx, 'Page.navigate', { url: args.url });
    ctx.effect = 'sent';
    if (result.errorText) {
      // A refused document is answered 204: Chrome reports an aborted navigation and keeps the page.
      throw new BridgeError(l.refused_document ? 'BROWSER_REQUEST_BLOCKED' : 'BROWSER_NAVIGATION_FAILED',
        l.refused_document ? `the host refused ${l.refused_document}; the page was left as it is` : `${result.errorText} for ${args.url}`);
    }
    if (!result.loaderId) {
      ctx.outcome = 'confirmed';
      return { url: l.url, document_id: l.document_id, same_document: true };
    }
    ctx.follow = true;  // this step follows the document change it caused
    while (Date.now() < deadline && lease === l && l.valid && l.loaded_document !== result.loaderId) await delay(40);
    fenced(ctx);
    const committed = l.document_id === result.loaderId;
    if (committed) ctx.outcome = 'confirmed';
    return { url: l.url, document_id: l.document_id, committed, loaded: l.loaded_document === result.loaderId };
  },
  async click(ctx, args) {
    const point = await stablePoint(ctx, targetOf(ctx, args));
    await pointerClick(ctx, point, 1);
    return { point, ...(await settleNavigation(ctx)) };
  },
  async double_click(ctx, args) {
    const point = await stablePoint(ctx, targetOf(ctx, args));
    await pointerClick(ctx, point, 2);
    return { point, ...(await settleNavigation(ctx)) };
  },
  async fill(ctx, args) {
    if (typeof args.text !== 'string') throw new BridgeError('BROWSER_ARGUMENT_INVALID', 'text must be a string');
    const target = await focusField(ctx, args);
    const selected = await agent(ctx, 'select_content', { target });
    if (!selected.focused) throw new BridgeError('BROWSER_TARGET_NOT_VISIBLE', 'the field did not take focus after the click; nothing was typed');
    ctx.effect = 'partial';
    if (args.text) await cdp(ctx, 'Input.insertText', { text: args.text });
    else if (selected.length) await pressKey(ctx, 'Delete');
    ctx.effect = 'sent';
    const after = await agent(ctx, 'value', { target });
    if (after.value === args.text || (after.value === null && after.length === args.text.length)) ctx.outcome = 'confirmed';
    return { value: after.value, length: after.length };
  },
  async type(ctx, args) {
    if (typeof args.text !== 'string') throw new BridgeError('BROWSER_ARGUMENT_INVALID', 'text must be a string');
    const target = await focusField(ctx, args);
    for (const character of args.text) {
      if (character === '\n') await pressKey(ctx, 'Enter');
      else if (keyFacts(character).vk) await pressKey(ctx, character);
      else { ctx.effect = 'partial'; await cdp(ctx, 'Input.insertText', { text: character }); ctx.effect = 'sent'; }
    }
    const settled = await settleNavigation(ctx);
    if (settled.navigated) return settled;
    const after = await agent(ctx, 'value', { target }).catch(() => ({ value: null, length: null }));
    return { value: after.value, length: after.length, ...settled };
  },
  async press(ctx, args) {
    if (typeof args.key !== 'string' || !args.key) throw new BridgeError('BROWSER_ARGUMENT_INVALID', 'key is required');
    const target = targetOf(ctx, args, false);
    if (target) await pointerClick(ctx, await stablePoint(ctx, target), 1);
    await pressKey(ctx, args.key);
    return { key: args.key, ...(await settleNavigation(ctx)) };
  },
  check: (ctx, args) => setChecked(ctx, args, true),
  uncheck: (ctx, args) => setChecked(ctx, args, false),
  async scroll(ctx, args) {
    const target = targetOf(ctx, args, false);
    ctx.effect = 'partial';
    const result = target ? await agent(ctx, 'scroll_into_view', { target })
      : await agent(ctx, 'scroll_by', { delta_x: args.delta_x, delta_y: args.delta_y });
    ctx.effect = 'sent';
    return result;
  },
};
STEPS.goto = STEPS.navigate;
STEPS.open_url = STEPS.navigate;

async function setChecked(ctx, args, wanted) {
  const target = targetOf(ctx, args);
  const info = await agent(ctx, 'info', { target });
  if (!info.checkable) throw new BridgeError('BROWSER_ARGUMENT_INVALID', `${info.what} is not a checkbox or radio button`);
  if (info.checked === wanted) return { checked: wanted, changed: false };
  await pointerClick(ctx, await stablePoint(ctx, target), 1);
  const settled = await settleNavigation(ctx);
  if (settled.navigated) return settled;
  const after = await agent(ctx, 'value', { target });
  if (after.checked === wanted) ctx.outcome = 'confirmed';
  return { checked: after.checked, changed: after.checked === wanted, ...settled };
}

function handleStep(message) {
  const answer = (body) => send({ t: 'step_result', id: message.id, lease_id: message.lease_id, generation: message.generation, ...body });
  const l = lease;
  if (typeof message.invocation_id !== 'string' || message.invocation_id !== openInvocation) {
    answer({ ok: false, outcome: 'not_dispatched', error: { code: 'BROWSER_INVOCATION_CLOSED', message: 'this step belongs to an invocation that is not open' } });
    return;
  }
  if (!l || !l.valid || l.lease_id !== message.lease_id || l.generation !== message.generation) {
    answer({ ok: false, outcome: 'not_dispatched', error: { code: 'BROWSER_LEASE_CHANGED', message: 'this step was issued for another lease' } });
    return;
  }
  // Steps of one lease run strictly one after another. Each re-checks lease and
  // invocation when its turn comes and before every command, so a release or a
  // close that arrived meanwhile stops it unsent.
  l.chain = l.chain.then(async () => {
    const ctx = {
      lease: l, lease_id: message.lease_id, generation: message.generation, invocation_id: message.invocation_id,
      effect: 'none', outcome: '', follow: false,
    };
    try {
      fenced(ctx);
      const operation = message.operation;
      if (UNSUPPORTED_OPERATIONS.includes(operation)) throw new BridgeError('BROWSER_OPERATION_UNSUPPORTED', `${operation} is not implemented in this version`);
      if (!OPERATIONS.includes(operation) || !STEPS[operation]) throw new BridgeError('BROWSER_OPERATION_UNSUPPORTED', `unknown operation ${operation}`);
      const args = message.args && typeof message.args === 'object' ? message.args : {};
      const value = await STEPS[operation](ctx, args);
      if (ctx.input_preparation && value && typeof value === 'object') value.input_preparation = ctx.input_preparation;
      answer({ ok: true, outcome: ctx.outcome || (ctx.effect === 'none' ? 'observed' : 'dispatched'), value });
    } catch (error) {
      const outcome = ctx.effect === 'none' ? 'not_dispatched' : ctx.effect === 'partial' ? 'unknown' : 'dispatched';
      answer({ ok: false, outcome, error: { code: error.code || 'BROWSER_INTERNAL', message: String(error.message || error) } });
    }
  });
}

// ---------------------------------------------------------------- requests

function bodyFacts(request) {
  // CDP omits hasPostData when there is no body. Contradictory or malformed
  // facts must not acquire the new idle bodyless-resource authority.
  if ((request.hasPostData === undefined || request.hasPostData === false)
      && request.postData === undefined && request.postDataEntries === undefined)
    return { post_data: null, has_post_data: false, complete: true };
  const inline = typeof request.postData === 'string' ? request.postData : null;
  const entries = request.postDataEntries;
  if (!Array.isArray(entries) || !entries.length || !entries.every((entry) => typeof entry.bytes === 'string'))
    return { post_data: inline, has_post_data: request.hasPostData === true ? true : null, complete: false };
  try {
    const parts = entries.map((entry) => Uint8Array.from(atob(entry.bytes), (character) => character.charCodeAt(0)));
    const bytes = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
    let offset = 0;
    for (const part of parts) { bytes.set(part, offset); offset += part.length; }
    return { post_data: new TextDecoder('utf-8', { fatal: true }).decode(bytes), has_post_data: true, complete: true };
  } catch (error) {
    return { post_data: inline, has_post_data: request.hasPostData === true ? true : null, complete: false };
  }
}

async function onRequestPaused(l, params) {
  const requestId = params.requestId;
  const isDocument = params.resourceType === 'Document';
  if (!l.valid) {
    refuseRequest(l.tab_id, requestId, isDocument).catch(() => {});
    return;
  }
  // Chrome numbers paused requests per attachment; the attachment's first
  // generation makes the id unique for the whole executor.
  const factId = `${l.attached_generation}:${requestId}`;
  // The destination as the page will commit it, fragment included.
  const url = `${params.request.url}${params.request.urlFragment || ''}`;
  const isMainFrame = params.frameId === l.frame_id;
  let verdictTimer;
  let block;
  try {
    block = await new Promise((resolve) => {
      l.paused.set(factId, { resolve, document: isDocument, cdp_id: requestId });
      verdictTimer = setTimeout(() => resolve('BROWSER_REQUEST_GUARD_UNAVAILABLE: no verdict arrived'), VERDICT_TIMEOUT_MS);
      send({
        t: 'request', lease_id: l.lease_id, generation: l.generation,
        request: {
          request_id: factId, url, method: params.request.method, ...bodyFacts(params.request),
          resource_type: params.resourceType, is_main_frame: isMainFrame,
        },
      });
    });
  } finally {
    clearTimeout(verdictTimer);
  }
  if (!l.paused.delete(factId)) return;  // released meanwhile: releaseLease() already refused it
  if (block === '') {
    // CDP's own identity of the coming document: its network id is the loader id.
    if (isMainFrame && isDocument && params.networkId) l.documents.set(params.networkId, { request_id: factId, url });
    sendCdp(l.tab_id, 'Fetch.continueRequest', { requestId }).catch(() => {});
  } else {
    if (isMainFrame && isDocument) l.refused_document = url;
    refuseRequest(l.tab_id, requestId, isDocument).catch(() => {});
  }
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  const l = lease;
  if (!l || source.tabId !== l.tab_id || source.sessionId) return;
  if (method === 'Fetch.requestPaused') {
    onRequestPaused(l, params);
  } else if (method === 'Page.frameNavigated' && !params.frame.parentId) {
    if (!l.valid || params.frame.loaderId === l.document_id) return;
    l.nav_requested = '';
    // Not reported yet: the attachment's answer reads the frame after this, under the lease it was asked for.
    if (!l.reported) { l.documents.clear(); l.context_id = null; l.dialog = null; return; }
    const previous = { lease_id: l.lease_id, generation: l.generation, document_id: l.document_id };
    const admitted = l.documents.get(params.frame.loaderId);
    l.documents.clear();
    l.lease_id = newLeaseId();
    l.generation += 1;
    l.frame_id = params.frame.id;
    l.document_id = params.frame.loaderId;
    l.url = frameUrl(params.frame);
    l.context_id = null;
    l.dialog = null;
    send({
      t: 'nav', previous, lease_id: l.lease_id, generation: l.generation, document_id: l.document_id, url: l.url,
      request_id: admitted ? admitted.request_id : null,
    });
  } else if (method === 'Page.frameRequestedNavigation' && params.frameId === l.frame_id) {
    // The main frame was asked to navigate. Pending until Chrome starts loading it or it stays in this document.
    if (params.disposition === 'currentTab' && !/^javascript:/i.test(String(params.url))) {
      l.nav_requests += 1;
      l.nav_requested = String(params.url);
    }
  } else if (method === 'Page.navigatedWithinDocument' && params.frameId === l.frame_id) {
    l.nav_requested = '';
    l.url = params.url;
    send({ t: 'url', lease_id: l.lease_id, generation: l.generation, url: l.url });
  } else if (method === 'Page.loadEventFired') {
    l.loaded_document = l.document_id;
  } else if (method === 'Page.frameStartedLoading' && params.frameId === l.frame_id) {
    l.loading = true;
    l.nav_requested = '';
  } else if (method === 'Page.frameStoppedLoading' && params.frameId === l.frame_id) {
    l.loading = false;
  } else if (method === 'Page.javascriptDialogOpening') {
    l.dialog = { type: params.type, message: String(params.message || '').slice(0, 200) };
  } else if (method === 'Page.javascriptDialogClosed') {
    l.dialog = null;
  }
});

chrome.debugger.onDetach.addListener((source, reason) => {
  const l = lease;
  if (!l || source.tabId !== l.tab_id || !l.valid) return;
  l.valid = false;
  const pending = attachingFact(l.tab_id);
  if (attaching && attaching.tab_id === l.tab_id && !attaching.ended) attaching.ended = String(reason);
  for (const entry of l.paused.values()) entry.resolve('BROWSER_SESSION_DETACHED: the debugger was detached');
  l.paused.clear();
  lease = null;
  badge(l.tab_id, '');
  chrome.storage.session.remove('attached_tab');
  // Cancel on Chrome's bar is the owner taking the tab; a closed tab is gone. Neither is closed later.
  if (l.created) { l.created = false; ownTabs.delete(l.tab_id); chrome.storage.session.remove('created_tab'); }
  send({ t: 'lost', tab_id: String(l.tab_id), reason: String(reason), detached: true, ...pending });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  ownTabs.delete(tabId);
  if (handedOff.delete(tabId)) send({ t: 'lost', tab_id: String(tabId), reason: 'target_closed', detached: true });
  if (approved.delete(tabId)) sendTabs();
});

// ---------------------------------------------------------------- executor messages

function onServerMessage(ws, event) {
  if (socket !== ws) return;
  let message;
  try { message = JSON.parse(event.data); } catch (error) { return; }
  switch (message.t) {
    case 'ping': send({ t: 'pong' }); break;
    case 'attach': handleAttach(message); break;
    case 'step': handleStep(message); break;
    case 'release': handleRelease(message); break;
    case 'open':
      openInvocation = typeof message.invocation_id === 'string' ? message.invocation_id : null;
      break;
    case 'close': {
      // Synchronous: queued steps of this invocation are dead before anything else runs.
      if (openInvocation === message.invocation_id) openInvocation = null;
      const drained = lease ? lease.chain : Promise.resolve();
      drained.then(() => send({ t: 'closed', id: message.id, invocation_id: message.invocation_id }));
      break;
    }
    case 'verdict': {
      const entry = lease && lease.paused.get(message.request_id);
      if (entry) entry.resolve(typeof message.block === 'string' ? message.block : 'BROWSER_POLICY_UNAVAILABLE: unreadable verdict');
      break;
    }
    default: break;
  }
}

// ---------------------------------------------------------------- owner (popup)

async function ownerStop(tabId) {
  const l = lease;
  const pending = attachingFact(tabId);
  approved.delete(tabId);
  handedOff.delete(tabId);
  // Also for a tab that is still being attached: its attachment is refused, whatever Chrome answers later. A tab
  // the agent opened is the owner's from here, also when Forget pairing drops the socket right after this, and
  // its stored record is gone before the owner is answered: a worker that restarts later must not close it.
  let opened = false;
  if (attaching && attaching.tab_id === tabId) {
    attaching.ended = 'owner_revoked';
    opened = ownTabs.delete(tabId);
  }
  let detached = true;
  if (l && l.tab_id === tabId) detached = (await releaseLease(l, 'owner_revoked')).detached;
  if (opened) await chrome.storage.session.remove('created_tab');
  send({ t: 'lost', tab_id: String(tabId), reason: 'owner_revoked', detached, ...pending });
  sendTabs();
}

async function status() {
  const { credential, port } = await chrome.storage.local.get(['credential', 'port']);
  return {
    connected: isOpen(socket), has_credential: Boolean(credential), port: Number(port) || DEFAULT_PORT,
    executor_generation: executorGeneration, error: lastError, approved: Array.from(approved),
    lease: lease ? { tab_id: lease.tab_id, created: lease.created } : null,
  };
}

const OWNER_ACTIONS = {
  status: async () => { await tryConnect(); },
  save: async (message) => {
    const credential = normalizeCode(message.credential);
    const port = Number(message.port);
    if (credential.length < 32) throw new Error('That is not a pairing credential.');
    if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('The port must be between 1024 and 65535.');
    await dropSocket();
    await chrome.storage.local.set({ credential, port });
    await tryConnect();
  },
  forget: async () => {
    if (lease) await ownerStop(lease.tab_id);
    else if (attaching && attaching.tab_id !== null) await ownerStop(attaching.tab_id);
    await chrome.storage.local.remove(['credential']);
    await dropSocket();
    lastError = 'No pairing credential stored.';
  },
  share: async (message) => {
    if (!isOpen(socket)) throw new Error('No Ouroboros task is connected right now. Sharing applies to a running task.');
    if (!Number.isInteger(message.tab_id)) throw new Error('No tab to share.');
    approved.add(message.tab_id);
    sendTabs();
  },
  stop: async (message) => ownerStop(message.tab_id),
};

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || !String(sender.url || '').startsWith(POPUP_URL) || !message || !OWNER_ACTIONS[message.type])
    return false;
  OWNER_ACTIONS[message.type](message).then(
    async () => respond({ ok: true, ...(await status()) }),
    async (error) => respond({ ok: false, message: String(error.message || error), ...(await status()) }),
  );
  return true;
});

// ---------------------------------------------------------------- lifecycle

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) tryConnect();
});

(async () => {
  // A worker that restarts has lost its lease: nothing may stay attached, and
  // a tab the previous worker created for the agent and still held is closed.
  // A tab handed to the owner is no longer recorded here.
  const { attached_tab: attached, created_tab: created } = await chrome.storage.session.get(['attached_tab', 'created_tab']);
  if (attached != null) await chrome.debugger.detach({ tabId: attached }).catch(() => {});
  if (created != null) await chrome.tabs.remove(created).catch(() => {});
  await chrome.storage.session.remove(['attached_tab', 'created_tab']);
  await chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
  tryConnect();
})();
