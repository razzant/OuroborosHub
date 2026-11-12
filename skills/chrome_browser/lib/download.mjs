// The bytes of an HTTP download the agent's own click starts, kept as Chrome receives them, while Chrome's own
// download goes on unchanged. Only for a call that asks for it (browser_click's captureDownload), only in the tab
// that call acts in, and only while that call runs.
//
// Playwright never sees a download through the relay: Chrome reports Page.downloadWillBegin and
// Page.downloadProgress on the tab's session, while Playwright listens for Browser.download* on a root session the
// extension does not offer (pinned coreBundle.js, CRBrowser), and Network.getResponseBody has no body for a download.
// What does work through the official extension is Chrome's Fetch domain on the tab's one shared debugger session
// (#294 probe, Chrome for Testing 156, extension 0.5.0): a document response paused at the response stage hands over
// its body with Fetch.getResponseBody, and Fetch.continueRequest then lets that same response go on to Chrome's own
// download. The server is asked once, and nothing is fetched again, fulfilled or failed here.
//
// Playwright owns that Fetch session for its request routing; the host's guard decides requests that reach its route
// (BrowserGuard.routeHandler), not unrouted requests. Chrome keeps one pattern list per session; Fetch.enable replaces it:
//   - Playwright's own Fetch.enable and Fetch.disable for a tab's session are recorded as they pass the relay;
//   - while a capture is open in a tab, the patterns Chrome gets there are Playwright's, unchanged, plus one
//     response-stage pattern for documents (Playwright's Fetch.disable becomes that pattern alone);
//   - when it closes, Chrome gets Playwright's last command again, exactly as Playwright sent it.
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
// How long a capturing call waits after its click for Chrome to begin, and report the end of, its download.
export const DOWNLOAD_SETTLE_MS = 15_000;
export const RESPONSE_STAGE_PATTERN = Object.freeze({ urlPattern: '*', resourceType: 'Document', requestStage: 'Response' });
// What Fetch.enable without patterns means to Chrome: every request, paused at the request stage.
const IMPLICIT_PATTERNS = [{ urlPattern: '*', requestStage: 'Request' }];
// What one capture keeps of a busy tab's document traffic. Past any of these, what was dropped could have been a
// competitor for an address, so no download of that capture is tied to a response (CaptureWindow.report).
export const MAX_RESPONSES = 64;
export const MAX_CHAINS = 256;
export const MAX_BEGINS = 64;

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

// One call's capture, in one tab. What Chrome reported while it was open, and the responses it read.
class CaptureWindow {
  constructor(tabId) {
    this.tabId = tabId;
    this.closed = false;
    this.restored = null;
    this.responses = [];
    this.begins = [];
    this.progress = new Map();
    // networkId -> { urls: the request's addresses so far, complete: its first request was seen here }.
    this.chains = new Map();
    this.pending = new Set();
    // What was not kept because a limit was reached: then no attribution of this capture is certain.
    this.dropped = { responses: 0, begins: 0, chains: 0 };
  }

  get overflow() {
    return this.dropped.responses + this.dropped.begins + this.dropped.chains > 0;
  }

  // own: the event came on the tab's own session, not a frame's or a worker's.
  observe(tabId, own, method, params) {
    if (this.closed)
      return;
    if (method === 'Page.downloadWillBegin') {
      if (this.begins.length < MAX_BEGINS)
        this.begins.push({ tabId, own, guid: String(params?.guid ?? ''), url: String(params?.url ?? ''), frameId: params?.frameId || '', suggestedFilename: String(params?.suggestedFilename ?? '') });
      else
        this.dropped.begins += 1;
    } else if (method === 'Page.downloadProgress' && params?.guid) {
      this.progress.set(String(params.guid), { state: params.state, receivedBytes: params.receivedBytes, totalBytes: params.totalBytes });
    } else if (method === 'Network.requestWillBeSent' && own && tabId === this.tabId && params?.type === 'Document' && params.requestId) {
      // A document request's redirect hops, so its bytes are judged by every address on the way to them. A chain whose
      // first request was not seen here, or one not kept, is incomplete: its bytes are not returned.
      const url = String(params.request?.url ?? '');
      const known = this.chains.get(params.requestId);
      if (!known && this.chains.size >= MAX_CHAINS) {
        this.dropped.chains += 1;
        return;
      }
      if (params.redirectResponse)
        this.chains.set(params.requestId, known ? { urls: [...known.urls, url], complete: known.complete } : { urls: [String(params.redirectResponse.url ?? ''), url], complete: false });
      else
        this.chains.set(params.requestId, { urls: [url], complete: true });
    }
  }

  // A document response of this tab at the response stage: whether to read it. Only a successful response with a
  // body can become a download; a redirect, an error page's failure or an empty answer is passed on unread.
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
      frameId: event.frameId || '',
      chain: [...new Set([...(chain?.urls ?? []), url])],
      // Chrome reports no network request for some responses (a link with the download attribute, as seen with
      // Chrome for Testing 156): then no hop of it can be known.
      networkKnown: !!event.networkId,
      // Every address on the way to it is known only if its first request was seen here and its last hop is this one.
      chainComplete: !!chain?.complete && chain.urls.at(-1) === url,
      contentType: headers.get('content-type') ?? '',
      attachment: /^\s*attachment\b/i.test(headers.get('content-disposition') ?? ''),
      declared: declaredLength(headers),
      transfer: (headers.get('transfer-encoding') ?? '').trim().toLowerCase(),
      encoding: (headers.get('content-encoding') ?? '').trim().toLowerCase(),
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
    else
      record.state = 'reading';
    this.responses.push(record);
    return record;
  }

  // Done once every download this tab began has a final state from Chrome and no read is still running.
  settled() {
    const begun = this.begins.filter((begin) => begin.tabId === this.tabId);
    return !this.pending.size && begun.length > 0 && begun.every((begin) => ['completed', 'canceled'].includes(this.progress.get(begin.guid)?.state));
  }

  // Each download Chrome began, with the response it came from only when that is certain:
  //   - nothing this capture observes was dropped at a limit (a dropped response or download could have shared the
  //     address, leaving one match only by accident);
  //   - Chrome reported the download on the tab's own session, where the responses are read;
  //   - exactly one response read in this tab had its address (or a hop on the way to it), and no other download of
  //     this call had that address;
  //   - Chrome named the frame of both, and it is the same frame;
  //   - every address on the way to that response was seen, so all of them can be judged before the bytes return.
  // Anything less certain is said so and gets no bytes.
  report() {
    const own = this.begins.filter((begin) => begin.tabId === this.tabId);
    const claims = new Map();
    const downloads = own.map((begin) => {
      const entry = { ...begin, response: null, capture: '', native: this.progress.get(begin.guid) ?? null };
      const matches = this.responses.filter((response) => response.chain.includes(begin.url));
      const twins = own.filter((other) => other.url === begin.url);
      if (this.overflow)
        entry.capture = 'overflow';
      else if (!begin.own)
        entry.capture = 'off_session';
      else if (matches.length > 1 || twins.length > 1)
        entry.capture = 'ambiguous';
      else if (!matches.length)
        entry.capture = /^(blob|data|filesystem):/i.test(begin.url) ? 'not_http' : 'not_seen';
      else if (!begin.frameId || !matches[0].frameId)
        entry.capture = 'no_frame';
      else if (begin.frameId !== matches[0].frameId)
        entry.capture = 'other_frame';
      else if (!matches[0].networkKnown)
        entry.capture = 'no_request';
      else if (!matches[0].chainComplete)
        entry.capture = 'chain_incomplete';
      else {
        entry.response = matches[0];
        entry.capture = matches[0].state;
        claims.set(matches[0], (claims.get(matches[0]) ?? 0) + 1);
      }
      return entry;
    });
    for (const entry of downloads) {
      if (entry.response && claims.get(entry.response) > 1) {
        entry.response = null;
        entry.capture = 'ambiguous';
      }
    }
    return {
      downloads,
      elsewhere: this.begins.filter((begin) => begin.tabId !== this.tabId),
      unclaimed: this.responses.filter((response) => !downloads.some((entry) => entry.response === response)).length,
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
    this._window = null;
    this._disposed = false;
    const capture = this;
    const handler = relay._handler;
    const forward = handler.forwardToExtension;
    handler.forwardToExtension = function(method, params, sessionId) {
      const tab = (method === 'Fetch.enable' || method === 'Fetch.disable') && sessionId ? capture._tabOfSession(sessionId) : null;
      if (tab) {
        capture._playwright.set(tab.tabId, { method, params });
        if (capture._armed(tab.tabId))
          return capture._send(tab.tabId, 'Fetch.enable', capture._merged(tab.tabId));
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
  // capture open (one Chrome paused under a pattern just removed) is passed on at once.
  _onResponse(tabId, event) {
    const window = this._armed(tabId);
    const record = window?.consider(event) ?? null;
    const job = (async () => {
      if (record?.state === 'reading') {
        try {
          const answer = await this._send(tabId, 'Fetch.getResponseBody', { requestId: event.requestId });
          const bytes = Buffer.from(String(answer?.body ?? ''), answer?.base64Encoded ? 'base64' : 'utf8');
          // A server can send more than it declared: more than the bound is measured and dropped, never returned.
          if (bytes.length > DOWNLOAD_MAX_BYTES)
            Object.assign(record, { state: 'over_read', size: bytes.length });
          else
            Object.assign(record, { state: 'captured', bytes, size: bytes.length, sha256: sha256(bytes) });
        } catch (error) {
          Object.assign(record, { state: 'read_failed', error: relayed(error?.message || error) });
        }
      }
      try {
        await this._send(tabId, 'Fetch.continueRequest', { requestId: event.requestId });
        if (record)
          record.continued = true;
      } catch (error) {
        if (record)
          Object.assign(record, { continued: false, continueError: relayed(error?.message || error) });
      }
    })();
    if (window) {
      window.pending.add(job);
      void job.then(() => window.pending.delete(job));
    }
    return job;
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

  // Chrome gets Playwright's own last command for the tab again, exactly as Playwright sent it. If Chrome does not
  // confirm it, the response-stage pattern may remain: later document responses there still pause, and each is
  // passed on unread (_onResponse with no capture open).
  async close(window) {
    if (window.closed)
      return;
    window.closed = true;
    if (this._window === window)
      this._window = null;
    const playwright = this._playwright.get(window.tabId);
    try {
      if (playwright?.method === 'Fetch.enable')
        await this._send(window.tabId, 'Fetch.enable', playwright.params);
      else
        await this._send(window.tabId, 'Fetch.disable', playwright?.params);
      window.restored = true;
    } catch (error) {
      window.restored = false;
      window.restoreError = relayed(error?.message || error);
    }
  }

  dispose() {
    this._disposed = true;
    if (this._window)
      this._window.closed = true;
    this._window = null;
  }
}
