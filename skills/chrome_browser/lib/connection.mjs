// One explicit connection to the owner's Chrome through the official Playwright Extension.
//
// idle --browser_connect--> pending (relay listening, approval URL handed to the caller)
// pending --owner approves in Chrome--> attaching (CDP over the relay, guard route, arm)
// attaching --guard armed, the chosen tab attached--> ready
// attaching --Chrome refused the tab--> closed (BROWSER_TAB_NOT_ATTACHED)
// any --lost / failed / expired / disconnected--> closed (a new browser_connect is required)
//
// Nothing here starts, finds or reads a browser or a profile. Nothing waits for approval
// inside a tool call, and no action is ever queued for later. A lost connection is not
// re-established and no tab is opened in its place.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export const APPROVAL_TTL_MS = 15 * 60 * 1000;

export function redact(text) {
  return String(text ?? '').replace(/(\/(?:cdp|extension)\/)[0-9a-f-]{36}/gi, '$1<redacted>');
}

// The relay attaches the owner's tab inside Playwright's connectOverCDP and only logs a refusal
// from Chrome (pinned coreBundle.js, BrowserModel.enableAutoAttach: `.catch(logUnhandledError)`),
// so the connect succeeds with no page at all. Chrome's answers to chrome.debugger.attach are kept
// here, in full (only relay addresses are redacted), so that failure can quote what Chrome said.
// Observation only: the command, its answer and the relay's own handling are unchanged, and
// nothing is sent to the extension.
function recordAttachRefusals(relay, refusals) {
  const model = relay?._handler?._model;
  if (!model || typeof model._sendToExtension !== 'function')
    return;
  const send = model._sendToExtension;
  model._sendToExtension = async (method, params) => {
    try {
      return await send(method, params);
    } catch (error) {
      if (method === 'chrome.debugger.attach')
        refusals.push(redact(error?.message || String(error)));
      throw error;
    }
  };
}

// Only protocol values we can name exactly cross into a diagnostic. A WebSocket
// reason is arbitrary text supplied by the peer, even when it looks harmless.
const WS_REASONS = new Map([
  // Playwright Extension 0.4.0 (microsoft/playwright v1.63.0):
  // background.ts disconnect; relayConnection.ts _checkLastTabDetached.
  ['User disconnected', 'user_disconnected'],
  ['All controlled tabs detached', 'all_controlled_tabs_detached'],
  ['Playwright client disconnected', 'playwright_client_disconnected'],
  ['Server stopped', 'server_stopped'],
]);
const DETACH_REASONS = new Set(['target_closed', 'canceled_by_user', 'replaced_with_devtools']);
const safeReason = (value, allowed) => allowed.get?.(value) ?? (allowed.has?.(value) ? value : 'unknown');

// Hooks are local to this relay and only observe. In particular, the socket's close
// listener is installed on an accepted extension socket before upstream installs
// its own listener, which closes CDP and then the browser context.
function observeRelay(relay, connection, generation) {
  const handleExtension = relay._handleExtensionConnection;
  relay._handleExtensionConnection = function(ws) {
    if (!this._extensionConnection)
      ws.on('close', (code, reason) => connection._observeTerminal(generation, 'extension_websocket_close', {
        wsCode: Number.isInteger(code) && code >= 1000 && code <= 4999 ? code : null,
        reason: safeReason(reason?.toString(), WS_REASONS),
      }));
    return handleExtension.call(this, ws);
  };
  const closeCDP = relay._closeCDPConnection;
  relay._closeCDPConnection = function(reason) {
    if (this._cdpConnection?.readyState === 1)
      connection._observeTerminal(generation, 'cdp_close', { reason: safeReason(reason, WS_REASONS) });
    return closeCDP.call(this, reason);
  };
  const handleEvent = relay._handler.handleExtensionEvent;
  relay._handler.handleExtensionEvent = function(method, params) {
    if (method === 'chrome.debugger.onDetach')
      connection._observeDetach(generation, params?.[1]);
    return handleEvent.call(this, method, params);
  };
}

function tabNotAttachedMessage(refusals) {
  if (!refusals.length)
    return 'the owner approved, but no tab was attached and Chrome reported no refusal, so there is no tab to work in and none was opened in its place. Tell the owner; a new browser_connect link lets them approve again';
  const stale = refusals.some((text) => /already attached/i.test(text))
    ? ' The extension still holds an earlier attachment to that tab (seen after its background worker was stopped while the tab was shared), so approving the same tab again fails the same way; no way to share that same tab again was tested.'
    : '';
  return `the owner approved, but Chrome did not let the Playwright Extension attach to the tab they chose (Chrome answered: "${refusals[refusals.length - 1]}"), so there is no tab to work in and none was opened in its place.${stale} Tell the owner: with a new browser_connect link they can share another tab (a tab that was never shared works)`;
}

// Playwright keeps at most this many console messages and page errors per page (pinned
// coreBundle.js, Page.addConsoleMessage and addPageError: ensureArrayLimit(..., 200)).
const RETAINED_RECORDS = 200;
// A document's addresses are asked of the host again for each call that returns its console
// records; past this many they are not all kept, and its records are refused, not guessed at.
export const MAX_DOCUMENT_ADDRESSES = 512;

const unique = (urls) => [...new Set(urls.filter((url) => typeof url === 'string' && url))];

// A request's address and the redirect hops before it, last hop first.
function redirectChain(request) {
  const chain = [];
  for (let hop = request; hop; hop = hop.redirectedFrom())
    chain.push(hop.url());
  return chain;
}

// The frame of a page's request; a service worker's request has none.
function frameOf(request) {
  try {
    return request.frame();
  } catch {
    return null;
  }
}

const withoutFragment = (url) => String(url).split('#')[0];

export class BrowserConnection {
  constructor({ tools, playwright, guard, config, toolList, clientName, approvalTtlMs = APPROVAL_TTL_MS }) {
    this._tools = tools;
    this._playwright = playwright;
    this._guard = guard;
    this._config = config;
    this._toolList = toolList;
    this._clientName = clientName;
    this._approvalTtlMs = approvalTtlMs;
    this._state = 'idle';
    this._reason = '';
    this._generation = 0;
    this._diagnostic = null;
    this._relay = null;
    this._browser = null;
    this._backend = null;
    this._url = '';
    this._approvalTimer = null;
    this._sessionDir = '';
    this._closing = Promise.resolve();
    // Chrome's refusals of chrome.debugger.attach during the current attempt (recordAttachRefusals).
    this._attachRefusals = [];
    // page -> where its documents and retained records came from (_provenance).
    this._pages = new WeakMap();
    // request -> the document and frame chain current when it was made (_onRequest).
    this._requests = new WeakMap();
    // Pages closed since the current tool call began; the server empties it at the start of each call.
    this.closedPages = [];
    this.events = [];
  }

  get state() {
    return this._state;
  }

  get reason() {
    return this._reason;
  }

  get diagnostic() {
    return this._diagnostic;
  }

  _observeTerminal(generation, source, detail = {}) {
    if (!this._current(generation) || this._state === 'idle' || this._state === 'closed' || this._diagnostic?.terminal)
      return;
    this._diagnostic.terminal = { at: Date.now(), source, ...detail };
  }

  _observeDetach(generation, reason) {
    if (!this._current(generation) || this._state === 'idle' || this._state === 'closed' || this._diagnostic?.terminal)
      return;
    this._diagnostic.lastDetach = { at: Date.now(), reason: safeReason(reason, DETACH_REASONS) };
  }

  _diagnosticText() {
    const { generation, terminal, lastDetach } = this._diagnostic ?? {};
    if (!terminal)
      return 'first observed layer unknown; underlying trigger unknown';
    const detail = [terminal.wsCode !== undefined && terminal.wsCode !== null ? `WebSocket code ${terminal.wsCode}` : '',
      terminal.reason ? `peer reason ${terminal.reason}` : ''].filter(Boolean).join(', ');
    const detach = lastDetach ? `; last debugger detach at ${new Date(lastDetach.at).toISOString()} (reason ${lastDetach.reason})` : '';
    return `generation ${generation}: first observed at ${terminal.source} on ${new Date(terminal.at).toISOString()}${detail ? ` (${detail})` : ''}${detach}; underlying trigger unknown`;
  }

  backend() {
    return this._state === 'ready' ? this._backend : null;
  }

  context() {
    return this._state === 'ready' ? this._context : null;
  }

  _event(name, detail = {}) {
    this.events.push({ at: Date.now(), name, ...detail });
    if (this.events.length > 200)
      this.events.shift();
  }

  _current(generation) {
    return generation === this._generation;
  }

  sessionDir() {
    if (!this._sessionDir) {
      this._sessionDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ouroboros-chrome-'));
      fs.mkdirSync(path.join(this._sessionDir, 'output'), { mode: 0o700 });
    }
    return this._sessionDir;
  }

  // The folder upstream writes screenshot copies and console logs to ('' before it exists).
  outputDir() {
    return this._sessionDir ? path.join(this._sessionDir, 'output') : '';
  }

  async connect() {
    if (this._state === 'ready')
      return { status: 'connected' };
    if (this._state === 'pending')
      return { status: 'needs_approval', url: this._url, reused: true };
    if (this._state === 'attaching')
      return { status: 'attaching' };
    await this._closing;
    const generation = ++this._generation;
    this._diagnostic = { generation, terminal: null, lastDetach: null };
    const relay = new this._tools.CDPRelayServer('chrome', undefined, undefined, undefined);
    this._attachRefusals = [];
    recordAttachRefusals(relay, this._attachRefusals);
    observeRelay(relay, this, generation);
    await relay.start();
    if (!this._current(generation)) {
      relay.stop();
      return { status: 'superseded' };
    }
    this._relay = relay;
    this._url = relay.connectPageUrl(this._clientName);
    this._state = 'pending';
    this._reason = '';
    this._event('pending');
    this._approvalTimer = setTimeout(() => this._expire(generation), this._approvalTtlMs);
    this._approvalTimer.unref?.();
    relay.waitForExtension().then(
        () => this._attach(generation, relay),
        () => this._close(generation, 'BROWSER_CONNECT_FAILED', 'the relay stopped before approval', 'relay_failed')).catch(() => {});
    return { status: 'needs_approval', url: this._url, reused: false };
  }

  async _attach(generation, relay) {
    if (!this._current(generation) || this._state !== 'pending')
      return;
    clearTimeout(this._approvalTimer);
    this._state = 'attaching';
    this._event('attaching');
    // A loss, expiry, disconnect or shutdown while attaching bumps the generation.
    const alive = () => this._current(generation) && this._state === 'attaching';
    let backend = null;
    try {
      const browser = await this._playwright.chromium.connectOverCDP(relay.cdpEndpoint(), { isLocal: true, timeout: 0, noDefaults: true });
      if (!alive()) {
        await browser.close().catch(() => {});
        return;
      }
      this._browser = browser;
      browser.on('disconnected', () => this._close(generation, 'BROWSER_CONNECTION_LOST', 'the browser connection closed', 'browser_disconnected'));
      const context = browser.contexts()[0];
      if (!context)
        throw new Error('the extension offered no browser context');
      // Redirect hops are not routed, so what a document and a retained record came from is kept for
      // the checks before output. First, so that nothing the tabs do from here on goes unseen.
      this._watchContext(context);
      // Every request of the attached tabs is decided by the host from here on, the owner's own included.
      await context.route('**/*', this._guard.routeHandler());
      if (!alive())
        return;
      this._event('routed');
      await this._guard.arm(context);
      if (!alive())
        return;
      this._event('armed');
      // An approval whose tab Chrome refused leaves a connection with no page (recordAttachRefusals).
      // It is a failed attach, not a ready connection: a later browser_connect then makes a new link.
      if (!context.pages().length)
        throw Object.assign(new Error(tabNotAttachedMessage(this._attachRefusals)), { code: 'BROWSER_TAB_NOT_ATTACHED' });
      const config = { ...this._config, outputDir: path.join(this.sessionDir(), 'output') };
      backend = new this._tools.BrowserBackend(config, context, this._toolList, { dispose: async () => {} });
      await backend.initialize({ cwd: this.sessionDir(), clientName: this._clientName });
      if (!alive()) {
        await backend.dispose().catch(() => {});
        return;
      }
      // Upstream builds its tab view (a Tab per attached page, the current tab, a listener for later
      // pages) only inside the first page tool (pinned coreBundle.js, Context.ensureTab ->
      // ensureBrowserContext). Until then the checks this server makes before a page tool acts had
      // no current tab, so the first page tool after approval skipped them. Upstream's own
      // initializer runs here instead: it wraps the pages already attached and opens none.
      await backend._context.ensureBrowserContext();
      if (!alive()) {
        await backend.dispose().catch(() => {});
        return;
      }
      backend.once('disconnected', () => this._close(generation, 'BROWSER_CONNECTION_LOST', 'the browser context closed', 'backend_disconnected'));
      this._backend = backend;
      this._context = context;
      this._state = 'ready';
      this._event('ready');
    } catch (error) {
      if (backend && this._backend !== backend)
        await backend.dispose().catch(() => {});
      const code = ['BROWSER_GUARD_NOT_ARMED', 'BROWSER_TAB_NOT_ATTACHED'].includes(error?.code) ? error.code : 'BROWSER_ATTACH_FAILED';
      if (alive())
        this._close(generation, code, this._diagnostic?.terminal ? 'the connection closed while attaching' : redact(error?.message || String(error)), 'attach_failed');
    }
  }

  // The built-in browser's bookkeeping (Ouroboros tools/browser.py, observe_navigation), for every
  // attached tab and whoever navigated it. Playwright continues redirect hops without a route (pinned
  // coreBundle.js, CRNetworkManager._onRequest), so a document's hops become known here, and they stay
  // with that document until another one really replaces it: Playwright reports a response apart from a
  // commit, and a 204, a download or a redirect whose next hop fails leaves the old document in the tab
  // with its old address (FrameManager.frameCommittedNewDocumentNavigation, frameAbortedNavigation).
  _watchContext(context) {
    context.on('page', (page) => this._provenance(page));
    context.on('request', (request) => this._onRequest(request));
    // Counted, not kept: Playwright itself retains these records. See recordDocument.
    context.on('console', (message) => {
      const page = message.page();
      if (page)
        this._provenance(page).console += 1;
    });
    context.on('weberror', (error) => {
      const page = error.page();
      if (page)
        this._provenance(page).errors += 1;
    });
    for (const page of context.pages())
      this._provenance(page);
  }

  // One entry per page:
  //   document   its current main-frame document;
  //   documents  those with a console message or page error that may still be retained, oldest first.
  //              A document is { own: its address and hops, addresses: those and every subframe hop seen
  //                under it, truncated, console and errors: the counts below when it began };
  //   firstOwn   the first document's own addresses, which outlast it (requestTargets);
  //   frames     frame -> redirect chain of the document it now shows, for a document that had hops;
  //   pending    frame -> its latest navigation request (only for the commit that names no request);
  //   console, errors  how many console messages and page errors this connection has seen.
  _provenance(page) {
    let state = this._pages.get(page);
    if (state)
      return state;
    const document = { own: unique([page.url()]), addresses: new Set(unique([page.url()])), truncated: false, console: 0, errors: 0 };
    state = { document, documents: [document], firstOwn: document.own, frames: new Map(), pending: new Map(), console: 0, errors: 0 };
    this._pages.set(page, state);
    const watch = (frame) => frame._eventEmitter.on('navigated', (event) => this._onNavigated(page, state, frame, event));
    page.frames().forEach(watch);
    page.on('frameattached', watch);
    page.once('close', () => this.closedPages.push(page));
    page.on('framedetached', (frame) => {
      state.frames.delete(frame);
      state.pending.delete(frame);
    });
    return state;
  }

  _onRequest(request) {
    const frame = frameOf(request);
    if (!frame)
      return;
    const state = this._provenance(frame.page());
    this._requests.set(request, { document: state.document, frameChain: state.frames.get(frame) ?? [] });
    if (request.isNavigationRequest())
      state.pending.set(frame, request);
  }

  // Playwright's own commit event, which names the document's request when it knows it. Without
  // newDocument it is a same-document navigation; with an error, a navigation that never committed.
  // Neither replaces a document. A commit may name no request (a later navigation was requested just
  // before it, or the document never used the network): the frame's latest navigation request counts
  // if it is for the address that committed.
  _onNavigated(page, state, frame, event) {
    if (event.error || !event.newDocument)
      return;
    const pending = state.pending.get(frame);
    state.pending.delete(frame);
    const request = event.newDocument.request?._object
      ?? (pending && withoutFragment(pending.url()) === withoutFragment(event.url) ? pending : null);
    const chain = redirectChain(request);
    if (frame === page.mainFrame()) {
      // Playwright has already detached the old document's child frames (framedetached, above).
      const own = unique([event.url, ...chain]);
      state.document = { own, addresses: new Set(own), truncated: false, console: state.console, errors: state.errors };
      state.documents.push(state.document);
      this._forgetDocuments(state);
    }
    if (chain.length < 2) {
      state.frames.delete(frame);
      return;
    }
    state.frames.set(frame, chain);
    for (const url of chain) {
      if (state.document.addresses.size < MAX_DOCUMENT_ADDRESSES)
        state.document.addresses.add(url);
      else if (!state.document.addresses.has(url))
        state.document.truncated = true;
    }
  }

  // A document is kept while a console message or a page error of it can still be retained, each kind
  // judged apart: the document began some of that kind and the newest of them is among the last
  // RETAINED_RECORDS counted. A kind that goes quiet keeps its own documents, never another kind's.
  // The first needs no record of its own: it also answers for those from before this connection counted any.
  _forgetDocuments(state) {
    state.documents = state.documents.filter((document, index, all) => {
      const next = all[index + 1];
      if (!next)
        return true;
      const holds = (kind) => (index === 0 || next[kind] > document[kind]) && next[kind] > state[kind] - RETAINED_RECORDS;
      return holds('console') || holds('errors');
    });
  }

  // The addresses a page's content may have come from: its own, its document's, and every redirect hop
  // of the documents it now shows.
  navigationTargets(page) {
    const state = this._provenance(page);
    return unique([page.url(), ...state.document.own, ...[...state.frames.values()].flat()]);
  }

  // The addresses a request Playwright retained came from: the chain to it, and the document and frame
  // that made it, which may be long gone from the tab. A main-frame document request is the new
  // document's own, not its predecessor's. One this connection never saw being made (before it was watching,
  // or in a new tab Playwright had not reported yet) is taken as the first document's, also after that document
  // is forgotten: Playwright keeps requests by its own rules, which the console and error counts that retire
  // documents say nothing about.
  requestTargets(page, request) {
    const chain = redirectChain(request);
    if (request.isNavigationRequest() && frameOf(request) === page.mainFrame())
      return unique(chain);
    const origin = this._requests.get(request);
    const own = origin?.document.own ?? this._provenance(page).firstOwn;
    return unique([...chain, ...own, ...(origin?.frameChain ?? [])]);
  }

  // The document a retained console message or page error was made in. Playwright returns them oldest
  // first and says nothing about their document (a message's location is its script's), but it delivers
  // them to this process in order with the commits, each once, and answers a read after those it has
  // delivered, so the one `fromNewest` places before the newest is the one counted then. kind: 'console'
  // or 'errors'. Those older than anything counted belong to the first document known.
  recordDocument(page, kind, fromNewest) {
    const state = this._provenance(page);
    const counted = state[kind] - 1 - fromNewest;
    return state.documents.findLast((document) => document[kind] <= counted) ?? state.documents[0];
  }

  _expire(generation) {
    if (this._current(generation) && this._state === 'pending')
      this._close(generation, 'BROWSER_APPROVAL_EXPIRED', `the owner did not approve within ${Math.round(this._approvalTtlMs / 60000)} minutes; the link no longer works`, 'approval_expired');
  }

  _close(generation, code, message, source = 'adapter_close') {
    if (!this._current(generation) || this._state === 'closed' || this._state === 'idle')
      return this._closing;
    this._observeTerminal(generation, source);
    this._state = 'closed';
    this._generation += 1;
    this._reason = `${code}: ${message}. ${this._diagnosticText()}.`;
    this._event('closed', { code });
    return this._teardown();
  }

  _teardown() {
    clearTimeout(this._approvalTimer);
    const backend = this._backend;
    const browser = this._browser;
    const relay = this._relay;
    this._backend = null;
    this._browser = null;
    this._relay = null;
    this._context = null;
    this._url = '';
    this._closing = (async () => {
      if (backend)
        await backend.dispose().catch(() => {});
      if (browser)
        await browser.close().catch(() => {});
      relay?.stop();
    })();
    return this._closing;
  }

  async disconnect() {
    if (this._state === 'idle' || this._state === 'closed')
      return { status: 'not_connected' };
    const generation = this._generation;
    this._observeTerminal(generation, 'explicit_disconnect');
    this._generation += 1;
    this._state = 'closed';
    this._reason = 'BROWSER_DISCONNECTED: the connection was ended with browser_disconnect.';
    this._event('closed', { code: 'BROWSER_DISCONNECTED', generation });
    await this._teardown();
    return { status: 'disconnected' };
  }

  async shutdown() {
    this._observeTerminal(this._generation, 'shutdown');
    this._generation += 1;
    if (this._state !== 'idle')
      this._state = 'closed';
    await this._teardown().catch(() => {});
    if (this._sessionDir) {
      fs.rmSync(this._sessionDir, { recursive: true, force: true });
      this._sessionDir = '';
    }
  }

  // The answer every other tool gets while the connection is not ready.
  refusal() {
    switch (this._state) {
      case 'idle':
        return 'BROWSER_NOT_CONNECTED: No Chrome tab is connected. Call browser_connect; it returns a link for the owner to approve in their own Chrome. Nothing was done.';
      case 'pending':
        return 'BROWSER_NEEDS_APPROVAL: The owner has not approved the connection yet (the link from browser_connect, opened in a new tab of their Chrome). Nothing was done and nothing was queued; keep this task and its browser tool session alive while waiting for the owner, then call the tool again after they say they approved. If the connect page reports an unsupported protocol or version, the installed Playwright Extension is not the tested 0.4.0 (relay protocol 2); tell the owner instead of retrying.';
      case 'attaching':
        return 'BROWSER_ATTACHING: The owner approved; the connection is still being set up. Nothing was done; call the tool again in a moment.';
      default:
        return `${this._reason} Nothing was done and no earlier action is repeated. Call browser_connect to ask the owner for a new approval.`;
    }
  }
}
