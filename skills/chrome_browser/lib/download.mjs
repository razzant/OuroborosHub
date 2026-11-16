// Copies of the HTTP document responses a tab gets while the agent's click runs, kept as Chrome receives them, while
// Chrome goes on with each response unchanged. Only for a call that asks for it (browser_click's captureDownload), only
// in the tab that call acts in, and only for requests that begin while that call runs: that is all that ties them to the
// click (a request a page timer or the owner starts meanwhile looks the same).
//
// A copy is a copy of one response, never a statement about a file Chrome saved. Chrome reports a download
// (Page.downloadWillBegin, Page.downloadProgress) with no id of the request it came from, so the report lists Chrome's
// downloads apart, by Chrome's own name, address and state, and ties no copy to any of them. A copy belongs to one
// network request, by Chrome's own ids for it (a navigation request's id is its loader id, as Playwright tells them
// apart, CRNetworkManager):
//   - its first request was seen (Network.requestWillBeSent) while this capture was open, so every address on the way
//     to it is known and judged; a response whose request began earlier, or that comes with no network request, is
//     not copied;
//   - its bytes are the body of that request's response, paused at the response stage (Fetch.requestPaused with that
//     networkId), read once with Fetch.getResponseBody and continued unchanged;
//   - Chrome then received that response and ended the request with net::ERR_ABORTED (Network.responseReceived, then
//     Network.loadingFailed, for that request id): it did not show the response as a page. Chrome reports that when it
//     hands a navigation's response to its download manager, and also when it refuses the download, so a copy can
//     exist although Chrome saved no file. A response Chrome shows as a page (Page.frameNavigated with its loader id,
//     or Network.loadingFinished), drops while it is held (loadingFailed with no response received) or fails otherwise
//     is not copied, nor one whose end Chrome had not reported when the call stopped waiting.
// Two responses of one click are two copies, each its own, whatever their addresses, names or sizes.
//
// Playwright never sees a download through the relay: Chrome reports Page.downloadWillBegin and
// Page.downloadProgress on the tab's session, while Playwright listens for Browser.download* on a root session the
// extension does not offer (pinned coreBundle.js, CRBrowser), and Network.getResponseBody has no body for a download.
// What does work through the official extension is Chrome's Fetch domain on the tab's one shared debugger session
// (#294 probe, Chrome for Testing 156, extension 0.5.0): a document response paused at the response stage hands over
// its body with Fetch.getResponseBody, and Fetch.continueRequest then lets that same response go on, to Chrome's own
// download or wherever Chrome takes it. The server is asked once, and nothing is fetched again, fulfilled or failed here.
//
// Playwright owns that Fetch session for its request routing; the host's guard decides requests that reach its route
// (BrowserGuard.routeHandler), not unrouted requests. Chrome keeps one pattern list per session; Fetch.enable replaces it:
//   - Playwright's own Fetch.enable and Fetch.disable for a tab's session are recorded as they pass the relay;
//   - while a capture is open in a tab, the patterns Chrome gets there are Playwright's, unchanged, plus one
//     response-stage pattern for documents (Playwright's Fetch.disable becomes that pattern alone);
//   - when it closes, Chrome gets Playwright's last command again, exactly as Playwright sent it, but not while this
//     module still holds a response paused in that tab (a body still being read, or its answer not yet sent): CDP leaves
//     undefined what disabling Fetch does to a body not yet received (Fetch.getResponseBody), and the tab's interception
//     is not changed under a read at all. Until the last held pause is answered, the tab keeps the response-stage pattern
//     with Playwright's latest patterns, also when Playwright changes them meanwhile, and a document response paused
//     there with no capture open is passed on unread. Then Chrome gets Playwright's latest command exactly, unless a
//     capture is open there again (its own close decides) or the connection is closing. Nothing waits for that read: a
//     stopped call and a closing connection return at once.
// Memory: a body is held only while it can still be a copy. A response that cannot be one is passed on unread when that
// is known before reading it (no network request, an incomplete chain, a capture past a limit, a body that cannot be
// bounded); a body already read is dropped, its size and sha256 kept, as soon as Chrome shows that response as a page,
// drops it or fails it; the rest when the call has reported, and a read that ends after its capture closed keeps nothing.
// Request-stage pauses stay Playwright's. A response-stage pause on a tab's own session is this module's, since the
// pinned Playwright never asks for one (it enables only requestStage "Request" and continues no request with
// interceptResponse); it never reaches Playwright, whose handler does not expect it, and it is always continued.
import crypto from 'node:crypto';
import { INTAKE_MAX_BYTES } from './guard.mjs';

// At most this many bytes of a response are returned, the bound of a binary resource in a result. Chrome holds and
// sends the whole body when asked for it, so the bound is applied before asking: only a response that declares its
// length, within the bound, is not sent in chunks (Transfer-Encoding overrides Content-Length) and is not
// content-encoded (its decoded length is not declared) is read. That bounds what is asked for, not what a server that
// sends more than it declared makes Chrome hold: what is handed over is measured again, and more than the bound is
// dropped, never returned.
export const DOWNLOAD_MAX_BYTES = INTAKE_MAX_BYTES;
// How long a capturing call waits after its click for Chrome to report what became of its responses and of the
// downloads it began.
export const DOWNLOAD_SETTLE_MS = 15_000;
export const RESPONSE_STAGE_PATTERN = Object.freeze({ urlPattern: '*', resourceType: 'Document', requestStage: 'Response' });
// What Fetch.enable without patterns means to Chrome: every request, paused at the request stage.
const IMPLICIT_PATTERNS = [{ urlPattern: '*', requestStage: 'Request' }];
// What one capture keeps of a busy tab's document traffic. Past a limit, what is dropped is counted and said so: a
// response is passed on unread and not listed, a download or a document request is not recorded (the response of a
// request not recorded is not copied). What was kept is unaffected.
export const MAX_RESPONSES = 64;
export const MAX_CHAINS = 256;
export const MAX_BEGINS = 64;
// How Chrome ends a navigation request whose response it received and did not show as a page.
const HANDED_ON = 'net::ERR_ABORTED';

export class DownloadCaptureError extends Error {
  constructor(code, message) {
    super(`${code}: ${message}`);
    this.name = 'DownloadCaptureError';
    this.code = code;
  }
}

const sha256 = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const isResponseStage = (params) => params?.responseStatusCode !== undefined || params?.responseErrorReason !== undefined;
const asksForResponses = (command) => command?.method === 'Fetch.enable' && (command.params?.patterns ?? []).some((pattern) => pattern?.requestStage === 'Response');
const relayed = (text) => String(text ?? '').replace(/(\/(?:cdp|extension)\/)[0-9a-f-]{36}/gi, '$1<redacted>').slice(0, 300);

// Response headers as Chrome lists them, by lower-case name; repeated names are joined as HTTP joins them.
function headerMap(list) {
  const map = new Map();
  for (const entry of Array.isArray(list) ? list : []) {
    const name = String(entry?.name ?? '').toLowerCase();
    const value = String(entry?.value ?? '');
    map.set(name, map.has(name) ? `${map.get(name)}, ${value}` : value);
  }
  return map;
}

// The body length a response declares, or null (none, or not one plain number).
function declaredLength(headers) {
  const value = headers.get('content-length');
  if (value === undefined || !/^\s*\d+\s*$/.test(value))
    return null;
  const length = Number(value);
  return Number.isSafeInteger(length) ? length : null;
}

// The DevTools target of a page Playwright shows: the target id of its server-side page. In this process Playwright's
// client maps an API object to its server object (inprocess.ts, clientConnection.toImpl; ElectronApplication.process
// uses it the same way). '' when it cannot be told.
export function targetIdOf(page) {
  try {
    return String(page._connection.toImpl(page)?.delegate?._targetId ?? '');
  } catch {
    return '';
  }
}

// What the report says of one response: 'copy', or why there is none (each a reason server.mjs words).
export function outcomeOf(record) {
  if (['page', 'canceled', 'failed'].includes(record.fate))
    return record.fate;
  if (record.state === 'skipped')
    return record.released;
  if (record.state === 'captured')
    return record.fate === 'handed_on' ? 'copy' : 'unsettled';
  // A read that ended after the capture closed kept nothing.
  if (record.state === 'released')
    return 'late';
  return record.state;
}

// One call's capture, in one tab. What Chrome reported while it was open, and the responses it read.
class CaptureWindow {
  constructor(tabId) {
    this.tabId = tabId;
    this.closed = false;
    this.restored = null;
    this.responses = [];
    this.begins = [];
    this.progress = new Map();
    // networkId -> { urls: the request's addresses so far, complete: its first request was seen here, ended: Chrome
    // ended it (see _end) }.
    this.chains = new Map();
    this.pending = new Set();
    // What was not kept because a limit was reached.
    this.dropped = { responses: 0, begins: 0, chains: 0 };
    // Bytes held now and the most held at once, for the record and the tests.
    this.heldBytes = 0;
    this.peakHeldBytes = 0;
  }

  // A response's bytes are no longer kept (or, while being read, will not be): its size, sha256 and every fact the
  // report says stay.
  _release(record, why) {
    if (record.state === 'reading') {
      record.discard = why;
    } else if (record.state === 'captured') {
      this.heldBytes -= record.bytes?.length ?? 0;
      Object.assign(record, { state: 'released', released: why, bytes: null });
    }
  }

  // A read finished: its bytes are kept only if the response can still be a copy.
  _keep(record, bytes) {
    // A read the report could not wait for ended late: that, not the report, is why it keeps nothing.
    const why = record.discard && record.discard !== 'reported' ? record.discard : (this.closed ? 'late' : record.discard || '');
    if (why) {
      Object.assign(record, { state: 'released', released: why, size: bytes.length, sha256: sha256(bytes) });
      return;
    }
    Object.assign(record, { state: 'captured', bytes, size: bytes.length, sha256: sha256(bytes) });
    this.heldBytes += bytes.length;
    this.peakHeldBytes = Math.max(this.peakHeldBytes, this.heldBytes);
  }

  // One response's bytes are not needed any more (their base64 is in the result).
  dropBytes(record, why) {
    this._release(record, why);
  }

  // The report is made: no bytes are kept any longer, and a read still running keeps none.
  release() {
    for (const record of this.responses)
      this._release(record, 'reported');
  }

  // own: the event came on the tab's own session, not a frame's or a worker's.
  observe(tabId, own, method, params) {
    if (this.closed)
      return;
    if (method === 'Page.downloadWillBegin') {
      if (this.begins.length < MAX_BEGINS)
        this.begins.push({ tabId, guid: String(params?.guid ?? ''), url: String(params?.url ?? ''), suggestedFilename: String(params?.suggestedFilename ?? '') });
      else
        this.dropped.begins += 1;
      return;
    }
    if (method === 'Page.downloadProgress') {
      if (params?.guid)
        this.progress.set(String(params.guid), { state: params.state, receivedBytes: params.receivedBytes, totalBytes: params.totalBytes });
      return;
    }
    if (!own || tabId !== this.tabId)
      return;
    if (method === 'Network.requestWillBeSent' && params?.type === 'Document' && params.requestId) {
      // A document request's redirect hops, so its bytes are judged by every address on the way to them. A chain whose
      // first request was not seen here, or one not kept, is incomplete: its response is not copied.
      const url = String(params.request?.url ?? '');
      const known = this.chains.get(params.requestId);
      if (!known && this.chains.size >= MAX_CHAINS) {
        this.dropped.chains += 1;
        return;
      }
      if (params.redirectResponse)
        this.chains.set(params.requestId, known ? { urls: [...known.urls, url], complete: known.complete, ended: false } : { urls: [String(params.redirectResponse.url ?? ''), url], complete: false, ended: false });
      else
        this.chains.set(params.requestId, { urls: [url], complete: true, ended: false });
    } else if (method === 'Network.responseReceived') {
      this._end(params?.requestId, 'received');
    } else if (method === 'Network.loadingFailed') {
      this._end(params?.requestId, 'failed', params?.errorText);
    } else if (method === 'Network.loadingFinished') {
      this._end(params?.requestId, 'page');
    } else if (method === 'Page.frameNavigated') {
      this._end(params?.frame?.loaderId, 'page');
    }
  }

  // What Chrome did with a response it was handed, by the id of its network request. The first end Chrome reports
  // stands. A response that cannot be a copy any more keeps no bytes from now on.
  _end(networkId, event, errorText) {
    if (!networkId)
      return;
    const chain = this.chains.get(String(networkId));
    if (chain && event !== 'received')
      chain.ended = true;
    for (const record of this.responses) {
      if (record.networkId !== String(networkId) || record.fate)
        continue;
      if (event === 'received') {
        record.received = true;
        continue;
      }
      if (event === 'failed') {
        record.fate = !record.received ? 'canceled' : errorText === HANDED_ON ? 'handed_on' : 'failed';
        record.failure = String(errorText ?? '').slice(0, 100);
      } else {
        record.fate = 'page';
      }
      if (record.fate !== 'handed_on')
        this._release(record, record.fate);
    }
  }

  // A document response of this tab at the response stage: whether to read it. Only a successful response with a
  // body can be a copy; a redirect, an error page's failure or an empty answer is passed on unread.
  consider(event) {
    if (this.closed || event.responseErrorReason !== undefined)
      return null;
    const status = event.responseStatusCode;
    if (!(status >= 200 && status < 300) || status === 204 || status === 205)
      return null;
    if (this.responses.length >= MAX_RESPONSES) {
      this.dropped.responses += 1;
      return null;
    }
    const headers = headerMap(event.responseHeaders);
    const url = String(event.request?.url ?? '');
    const chain = event.networkId ? this.chains.get(event.networkId) : undefined;
    const record = {
      url,
      method: String(event.request?.method ?? ''),
      status,
      networkId: String(event.networkId ?? ''),
      chain: [...new Set([...(chain?.urls ?? []), url])],
      // Every address on the way to it is known only if its first request was seen here and its last hop is this one.
      chainComplete: !!chain?.complete && chain.urls.at(-1) === url,
      contentType: headers.get('content-type') ?? '',
      declared: declaredLength(headers),
      transfer: (headers.get('transfer-encoding') ?? '').trim().toLowerCase(),
      encoding: (headers.get('content-encoding') ?? '').trim().toLowerCase(),
      // What Chrome reported after it was handed the response (see _end).
      received: false,
      fate: '',
      failure: '',
      continued: null,
    };
    if (record.transfer && record.transfer !== 'identity')
      record.state = 'chunked';
    else if (record.encoding && record.encoding !== 'identity')
      record.state = 'encoded';
    else if (record.declared === null)
      record.state = 'unknown_length';
    else if (record.declared > DOWNLOAD_MAX_BYTES)
      record.state = 'too_large';
    // Chrome reports no network request for some responses (a link with the download attribute, as seen with
    // Chrome for Testing 156): then no hop of it, and nothing of what became of it, can be known.
    else if (!record.networkId)
      Object.assign(record, { state: 'skipped', released: 'no_request' });
    else if (!record.chainComplete)
      Object.assign(record, { state: 'skipped', released: 'chain_incomplete' });
    else
      record.state = 'reading';
    this.responses.push(record);
    return record;
  }

  // Done once no read is running, Chrome has ended every document request that began here (showed its response, passed
  // it on elsewhere, or failed it), something went to a download or was not shown, at least as many downloads began in
  // this tab as responses Chrome did not show, and each of those downloads ended. The counts only stop the wait: they
  // tie no download to a response.
  settled() {
    if (this.pending.size || [...this.chains.values()].some((chain) => chain.complete && !chain.ended))
      return false;
    const handedOn = this.responses.filter((record) => record.fate === 'handed_on').length;
    const begun = this.begins.filter((begin) => begin.tabId === this.tabId);
    return (handedOn > 0 || begun.length > 0) && begun.length >= handedOn
      && begun.every((begin) => ['completed', 'canceled'].includes(this.progress.get(begin.guid)?.state));
  }

  // Every response with what became of it, and apart from them Chrome's downloads, with no tie between the two.
  report() {
    return {
      // A response Chrome still held when the call stopped waiting (its body still being read) is said apart: whatever
      // it leads to, a download or a page, comes after this report.
      responses: this.responses.filter((record) => record.continueSentAt).map((record) => ({ record, outcome: outcomeOf(record) })),
      held: this.responses.filter((record) => !record.continueSentAt).map((record) => record.method),
      downloads: this.begins.filter((begin) => begin.tabId === this.tabId).map((begin) => ({ ...begin, native: this.progress.get(begin.guid) ?? null })),
      elsewhere: this.begins.filter((begin) => begin.tabId !== this.tabId),
      dropped: { ...this.dropped },
      restored: this.restored,
    };
  }
}

// One per relay (one connection generation). Installed before Playwright connects, so its first Fetch.enable is seen.
export class DownloadCapture {
  constructor(relay) {
    this._model = relay._handler._model;
    // tabId -> Playwright's last Fetch command for the tab's own session: { method, params }.
    this._playwright = new Map();
    // tabId -> how many response-stage pauses there this module has not answered yet.
    this._held = new Map();
    // Tabs that get Playwright's own command back once no pause there is held (see the header).
    this._deferred = new Set();
    this._window = null;
    this._disposed = false;
    const capture = this;
    const handler = relay._handler;
    const forward = handler.forwardToExtension;
    handler.forwardToExtension = function(method, params, sessionId) {
      const tab = (method === 'Fetch.enable' || method === 'Fetch.disable') && sessionId ? capture._tabOfSession(sessionId) : null;
      if (tab) {
        capture._playwright.set(tab.tabId, { method, params });
        // While a capture is open there, or a pause there is held, Playwright's patterns go with the response stage;
        // Playwright's own command, now this one, comes back exactly when neither is so (close, _answered).
        if (capture._armed(tab.tabId))
          return capture._send(tab.tabId, 'Fetch.enable', capture._merged(tab.tabId));
        if (capture._held.get(tab.tabId)) {
          capture._deferred.add(tab.tabId);
          return capture._send(tab.tabId, 'Fetch.enable', capture._merged(tab.tabId));
        }
      }
      return forward.call(this, method, params, sessionId);
    };
    const onEvent = this._model.onDebuggerEvent;
    this._model.onDebuggerEvent = function(source, method, params) {
      if (capture._observe(source, method, params))
        return;
      return onEvent.call(this, source, method, params);
    };
  }

  _tabOfSession(sessionId) {
    for (const tab of this._model._tabSessions.values()) {
      if (tab.sessionId === sessionId)
        return tab;
    }
    return null;
  }

  _send(tabId, method, params) {
    return this._model._sendToExtension('chrome.debugger.sendCommand', [{ tabId }, method, params]);
  }

  _armed(tabId) {
    const window = this._window;
    return window && !window.closed && window.tabId === tabId ? window : null;
  }

  // Playwright's patterns, unchanged and first, and the response-stage one; Playwright's Fetch.disable keeps none of its own.
  _merged(tabId) {
    const playwright = this._playwright.get(tabId);
    if (playwright?.method !== 'Fetch.enable')
      return { patterns: [RESPONSE_STAGE_PATTERN] };
    const params = playwright.params ?? {};
    const patterns = Array.isArray(params.patterns) && params.patterns.length ? params.patterns : IMPLICIT_PATTERNS;
    return { ...params, patterns: [...patterns, RESPONSE_STAGE_PATTERN] };
  }

  // Returns true for an event that is this module's alone (it is not passed to Playwright).
  _observe(source, method, params) {
    const tab = source?.tabId === undefined ? undefined : this._model._tabSessions.get(source.tabId);
    if (!tab)
      return false;
    const own = !source.sessionId;
    if (own && method === 'Fetch.requestPaused' && isResponseStage(params) && !asksForResponses(this._playwright.get(tab.tabId))) {
      void this._onResponse(tab.tabId, params);
      return true;
    }
    this._window?.observe(tab.tabId, own, method, params);
    return false;
  }

  // Read the body when the open capture wants it, then let the very same response go on. A pause that arrives with no
  // capture open (one Chrome paused under a pattern still in place) is passed on at once.
  _onResponse(tabId, event) {
    const window = this._armed(tabId);
    const record = window?.consider(event) ?? null;
    this._held.set(tabId, (this._held.get(tabId) ?? 0) + 1);
    const job = (async () => {
      try {
        if (record?.state === 'reading') {
          try {
            const answer = await this._send(tabId, 'Fetch.getResponseBody', { requestId: event.requestId });
            const bytes = Buffer.from(String(answer?.body ?? ''), answer?.base64Encoded ? 'base64' : 'utf8');
            // A server can send more than it declared: more than the bound is measured and dropped, never returned.
            if (bytes.length > DOWNLOAD_MAX_BYTES)
              Object.assign(record, { state: 'over_read', size: bytes.length });
            else
              window._keep(record, bytes);
          } catch (error) {
            Object.assign(record, { state: 'read_failed', error: relayed(error?.message || error) });
          }
        }
        if (record)
          record.continueSentAt = Date.now();
        try {
          await this._send(tabId, 'Fetch.continueRequest', { requestId: event.requestId });
          if (record)
            record.continued = true;
        } catch (error) {
          if (record)
            Object.assign(record, { continued: false, continueError: relayed(error?.message || error) });
        }
      } finally {
        this._answered(tabId);
      }
    })();
    if (window) {
      window.pending.add(job);
      void job.then(() => window.pending.delete(job));
    }
    return job;
  }

  // A pause this module held in the tab is answered. Once none is held, Chrome gets Playwright's latest command back,
  // unless a capture opened there since (open; its close decides) or the connection is closing.
  _answered(tabId) {
    const held = (this._held.get(tabId) ?? 1) - 1;
    if (held > 0) {
      this._held.set(tabId, held);
      return;
    }
    this._held.delete(tabId);
    if (this._deferred.delete(tabId) && !this._disposed)
      this._restore(tabId).catch(() => {});
  }

  // Playwright's own last command for the tab, exactly as Playwright sent it.
  _restore(tabId) {
    const playwright = this._playwright.get(tabId);
    return playwright?.method === 'Fetch.enable'
      ? this._send(tabId, 'Fetch.enable', playwright.params)
      : this._send(tabId, 'Fetch.disable', playwright?.params);
  }

  // Opens the capture for the tab with this DevTools target id. Throws DownloadCaptureError; then nothing changed.
  async open(targetId) {
    if (this._disposed)
      throw new DownloadCaptureError('BROWSER_DOWNLOAD_CAPTURE_UNAVAILABLE', 'the connection is closing');
    if (this._window)
      throw new DownloadCaptureError('BROWSER_DOWNLOAD_CAPTURE_BUSY', 'another call is capturing a download on this connection');
    let tab = null;
    for (const candidate of this._model._tabSessions.values()) {
      if (targetId && candidate.targetInfo?.targetId === targetId)
        tab = candidate;
    }
    if (!tab)
      throw new DownloadCaptureError('BROWSER_DOWNLOAD_CAPTURE_UNAVAILABLE', 'the tab this call acts in could not be matched to a tab the extension relays');
    if (asksForResponses(this._playwright.get(tab.tabId)))
      throw new DownloadCaptureError('BROWSER_DOWNLOAD_CAPTURE_UNAVAILABLE', 'Playwright itself intercepts responses in this tab');
    const window = new CaptureWindow(tab.tabId);
    this._window = window;
    // This capture's patterns replace whatever waited; its own close decides again.
    this._deferred.delete(tab.tabId);
    try {
      await this._send(tab.tabId, 'Fetch.enable', this._merged(tab.tabId));
    } catch (error) {
      await this.close(window);
      throw new DownloadCaptureError('BROWSER_DOWNLOAD_CAPTURE_UNAVAILABLE', `Chrome did not accept the response-stage interception (${relayed(error?.message || error)})`);
    }
    return window;
  }

  // Waits up to timeoutMs for the window to settle (see CaptureWindow.settled), less if the call is cancelled or the
  // connection closes. A read still running then finishes on its own; its bytes are not returned.
  async settle(window, timeoutMs, signal) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline && !signal?.aborted && !this._disposed && !window.settled())
      await sleep(25);
  }

  // Chrome gets Playwright's own last command for the tab again, exactly as Playwright sent it, at once or, while a pause
  // there is held, once none is (see the header). If Chrome does not confirm it, the response-stage pattern may remain:
  // later document responses there still pause, and each is passed on unread (_onResponse with no capture open).
  async close(window) {
    if (window.closed)
      return;
    window.closed = true;
    if (this._window === window)
      this._window = null;
    if (this._held.get(window.tabId)) {
      this._deferred.add(window.tabId);
      window.restored = 'deferred';
      return;
    }
    try {
      await this._restore(window.tabId);
      window.restored = true;
    } catch (error) {
      window.restored = false;
      window.restoreError = relayed(error?.message || error);
    }
  }

  dispose() {
    this._disposed = true;
    this._deferred.clear();
    if (this._window)
      this._window.closed = true;
    this._window = null;
  }
}
