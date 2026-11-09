// The Playwright Extension's side of the relay, and just enough of Chrome's DevTools protocol behind it,
// so the real relay, Playwright's own Chromium consumers, its MCP tools and this skill's connection and
// server run with no browser. It renders no page: the test states what Chrome would report, in Chrome's
// order, and reads back the commands Playwright sent.
//
// Written from the stand-in in the integration's QA (helpers/mock_extension.mjs, connectRelayExtension;
// its relay protocol notes came from the pinned extension build) and the pinned CRPage and CRNetworkManager
// in playwright-core's lib/coreBundle.js. This is evidence about the adapter's bookkeeping over upstream's
// consumers, not about Chrome: the events are the test's statement of what Chrome does.

const pageTitle = (tab) => tab.title ?? '';

// answer(method, params, tabId): a DevTools result the test states for one command, or undefined for the default.
export async function connectFakeChrome(WebSocket, relayUrl, { tabs = [], refuseAttach = new Map(), onCommand, answer } = {}) {
  const socket = new WebSocket(relayUrl);
  const commands = [];
  const known = new Map(tabs.map((tab) => [tab.id, { ...tab }]));
  const fetchAnswers = [];
  const bodies = new Map();
  // The Fetch patterns Chrome holds for each tab's own session (each Fetch.enable replaces them), and the bodies of
  // responses paused at the response stage, by their pause id.
  const fetchPatterns = new Map();
  const pausedBodies = new Map();
  let nextId = 1;
  await new Promise((resolve, reject) => {
    socket.once('open', resolve);
    socket.once('error', reject);
  });
  const send = (message) => socket.send(JSON.stringify(message));
  const emit = (tabId, method, params) => send({ method: 'chrome.debugger.onEvent', params: [{ tabId }, method, params] });
  const frameOf = (tab, { id = `TARGET-${tab.id}`, url = tab.url, loaderId = `LOADER-${nextId++}`, parentId } = {}) => {
    let origin = 'null';
    try {
      origin = new URL(url).origin;
    } catch {
      // about:blank and the like
    }
    return { id, ...(parentId ? { parentId } : {}), loaderId, url, domainAndRegistry: '', securityOrigin: origin, mimeType: 'text/html', secureContextType: 'InsecureScheme', crossOriginIsolatedContextType: 'NotIsolated', gatedAPIFeatures: [] };
  };
  // Playwright evaluates in a utility world it creates by name; each document needs one.
  const worlds = new Map();
  const mainWorlds = new Map();
  const announceWorlds = (tabId, frameId) => {
    const main = nextId++;
    mainWorlds.set(tabId, main);
    emit(tabId, 'Runtime.executionContextCreated', { context: { id: main, origin: '', name: '', uniqueId: `main-${main}`, auxData: { frameId, isDefault: true, type: 'default' } } });
    const name = worlds.get(tabId);
    if (name)
      emit(tabId, 'Runtime.executionContextCreated', { context: { id: nextId++, origin: '', name, uniqueId: `utility-${nextId}`, auxData: { frameId, isDefault: false, type: 'isolated' } } });
  };
  socket.on('message', (data) => {
    const message = JSON.parse(data.toString());
    if (message.id === undefined || !message.method)
      return;
    commands.push({ method: message.method, params: message.params });
    // What the page does the moment Chrome receives a command, stated by the test (before the command's answer).
    onCommand?.(message.method, message.params, { terminate: () => socket.terminate(), close: (code, reason) => socket.close(code, reason) });
    const tabId = message.params?.[0]?.tabId;
    if (message.method === 'chrome.debugger.attach' && refuseAttach.has(tabId)) {
      send({ id: message.id, error: refuseAttach.get(tabId) });
      return;
    }
    let result = {};
    if (message.method === 'chrome.tabs.create') {
      const props = message.params?.[0] || {};
      const tab = { id: 900 + known.size, url: props.url || 'about:blank', title: '', active: props.active !== false, windowId: 1, index: known.size };
      known.set(tab.id, tab);
      result = tab;
    } else if (message.method === 'chrome.tabs.remove') {
      setImmediate(() => removeTab(message.params[0]));
    } else if (message.method === 'chrome.debugger.sendCommand') {
      const tab = known.get(tabId) || { id: tabId, url: 'about:blank', title: '' };
      const [, method, params] = message.params;
      const stated = answer?.(method, params, tabId);
      if (stated !== undefined) {
        result = stated;
      } else if (method === 'Target.getTargetInfo') {
        result = { targetInfo: { targetId: `TARGET-${tabId}`, type: 'page', title: pageTitle(tab), url: tab.url, attached: true, canAccessOpener: false, browserContextId: 'CTX' } };
      } else if (method === 'Page.getFrameTree') {
        result = { frameTree: { frame: frameOf(tab), childFrames: [] } };
      } else if (method === 'Page.createIsolatedWorld') {
        worlds.set(tabId, params.worldName);
        result = { executionContextId: nextId++ };
        setImmediate(() => announceWorlds(tabId, params.frameId));
      } else if (method === 'Page.navigate') {
        // The test states what follows; a navigation it did not script answers as aborted, at once.
        result = known.get(tabId)?.onNavigate?.(params) ?? { frameId: `TARGET-${tabId}`, errorText: 'net::ERR_ABORTED' };
      } else if (method === 'Runtime.evaluate') {
        result = { result: { type: 'object', objectId: `object-${nextId++}` } };
      } else if (method === 'Runtime.callFunctionOn') {
        if (!params.returnByValue)
          result = { result: { type: 'object', objectId: `object-${nextId++}` } };
        else if (JSON.stringify(params).includes('ariaSnapshotJSON'))
          result = { result: { type: 'object', value: {
            o: [
              { k: 'json', v: { a: [{ o: [{ k: 'role', v: 'heading' }, { k: 'name', v: `${pageTitle(tab)} page content` }, { k: 'children', v: { a: [], id: 3 } }, { k: 'props', v: { o: [], id: 4 } }], id: 5 }], id: 2 } },
              { k: 'iframeRefs', v: { a: [], id: 6 } },
              { k: 'iframeDepths', v: { o: [], id: 7 } },
            ],
            id: 1,
          } } };
        else
          result = { result: { type: 'string', value: pageTitle(tab) } };
      } else if (method === 'Storage.getCookies') {
        result = { cookies: [] };
      } else if (method === 'Network.getResponseBody') {
        const body = bodies.get(params.requestId);
        if (!body) {
          send({ id: message.id, error: 'No resource with given identifier found' });
          return;
        }
        result = { body: body.toString('base64'), base64Encoded: true };
      } else if (method === 'Fetch.continueRequest' || method === 'Fetch.failRequest' || method === 'Fetch.fulfillRequest') {
        fetchAnswers.push({ method, requestId: params.requestId, errorReason: params.errorReason });
      } else if (method === 'Fetch.enable' && !message.params[0].sessionId) {
        // Without patterns, every request at the request stage.
        fetchPatterns.set(tabId, params?.patterns?.length ? params.patterns : [{ urlPattern: '*', requestStage: 'Request' }]);
      } else if (method === 'Fetch.disable' && !message.params[0].sessionId) {
        fetchPatterns.delete(tabId);
      } else if (method === 'Fetch.getResponseBody') {
        const body = pausedBodies.get(params.requestId);
        if (!body) {
          send({ id: message.id, error: 'Can only get response body on requests captured after headers received.' });
          return;
        }
        result = { body: body.toString('base64'), base64Encoded: true };
      }
    }
    send({ id: message.id, result });
  });
  for (const tab of known.values())
    send({ method: 'chrome.tabs.onCreated', params: [tab] });
  send({ method: 'extension.initialized', params: [] });
  const tabOf = (tabId) => known.get(tabId);
  // Whether Chrome pauses a request of this resource type at this stage in this tab ('*' patterns only, as used here).
  const pauses = (tabId, stage, resourceType) => (fetchPatterns.get(tabId) ?? []).some((pattern) =>
    (pattern.requestStage ?? 'Request') === stage && (!pattern.resourceType || pattern.resourceType === resourceType));

  // What Chrome reports for one request, or for each hop of a redirect chain. Each entry of `hops` is a URL
  // or { url, method, postData, resourceType }. `frameId` defaults to the tab's main frame. With navigation:
  // true the requests are the frame's document request (requestId === loaderId). A pause goes with each hop
  // once Playwright has asked for them (its route is installed): that is the only way it learns of a request.
  const requestChain = (tabId, hops, { frameId = `TARGET-${tabId}`, navigation = false, requestId = `REQ-${nextId++}`, redirectStatus = 302 } = {}) => {
    let previous = null;
    const paused = [];
    for (const hop of hops) {
      const { url, method = 'GET', postData, resourceType = navigation ? 'Document' : 'Fetch' } = typeof hop === 'string' ? { url: hop } : hop;
      const request = { url, method, headers: {}, ...(postData ? { hasPostData: true, postDataEntries: [{ bytes: Buffer.from(postData).toString('base64') }] } : {}), initialPriority: 'High', referrerPolicy: 'no-referrer' };
      emit(tabId, 'Network.requestWillBeSent', {
        requestId, loaderId: navigation ? requestId : 'SUB', documentURL: url, request, timestamp: 1, wallTime: 1, initiator: { type: 'other' }, type: resourceType, frameId,
        ...(previous ? { redirectResponse: response(previous, redirectStatus), redirectHasExtraInfo: false } : {}),
      });
      if (pauses(tabId, 'Request', resourceType)) {
        paused.push(`PAUSED-${nextId++}`);
        emit(tabId, 'Fetch.requestPaused', { requestId: paused[paused.length - 1], networkId: requestId, request, frameId, resourceType });
      }
      previous = url;
    }
    return { requestId, lastUrl: previous, paused };
  };
  // A response at the response stage, if the tab's patterns ask for it: the pause id, or null when Chrome would not
  // pause it. Its body is what Fetch.getResponseBody hands over for that pause. frameId null: Chrome names no frame;
  // networkId null: Chrome reports no network request for it (seen for a link with the download attribute).
  const responseStage = (tabId, networkId, url, { method = 'GET', status = 200, headers = {}, body, frameId = `TARGET-${tabId}`, resourceType = 'Document' } = {}) => {
    if (!pauses(tabId, 'Response', resourceType))
      return null;
    const pauseId = `PAUSED-${nextId++}`;
    if (body !== undefined)
      pausedBodies.set(pauseId, Buffer.from(body));
    emit(tabId, 'Fetch.requestPaused', {
      requestId: pauseId, ...(networkId === null ? {} : { networkId }), request: { url, method, headers: {}, initialPriority: 'High', referrerPolicy: 'no-referrer' }, ...(frameId === null ? {} : { frameId }), resourceType,
      responseStatusCode: status, responseStatusText: status === 200 ? 'OK' : String(status), responseHeaders: Object.entries(headers).map(([name, value]) => ({ name, value: String(value) })),
    });
    return pauseId;
  };
  const response = (url, status, headers = {}, mimeType = 'text/html') => ({ url, status, statusText: status === 200 ? 'OK' : String(status), headers, mimeType, connectionReused: false, connectionId: 0, encodedDataLength: 0, securityState: 'unknown', protocol: 'http/1.1' });
  const respond = (tabId, requestId, url, { status = 200, headers = {}, mimeType, body, frameId = `TARGET-${tabId}`, type = 'Document' } = {}) => {
    if (body !== undefined)
      bodies.set(requestId, Buffer.from(body));
    emit(tabId, 'Network.responseReceived', { requestId, loaderId: requestId, timestamp: 2, type, response: response(url, status, headers, mimeType), hasExtraInfo: false, frameId });
    emit(tabId, 'Network.loadingFinished', { requestId, timestamp: 3, encodedDataLength: 0 });
  };
  const failRequest = (tabId, requestId, errorText = 'net::ERR_ABORTED', { canceled = false, type = 'Document' } = {}) =>
    emit(tabId, 'Network.loadingFailed', { requestId, timestamp: 3, type, errorText, canceled });
  // A document replaces the frame's old one only here: Page.frameNavigated (pinned CRPage._onFrameNavigated).
  const commit = (tabId, url, { frameId = `TARGET-${tabId}`, loaderId, parentId } = {}) => {
    const tab = tabOf(tabId);
    const frame = frameOf(tab || { id: tabId, url }, { id: frameId, url, loaderId: loaderId ?? `LOADER-${nextId++}`, parentId });
    if (tab && frameId === `TARGET-${tabId}`)
      tab.url = url;
    // A subframe's new document leaves the other frames' execution contexts alone.
    if (!parentId)
      emit(tabId, 'Runtime.executionContextsCleared', {});
    emit(tabId, 'Page.frameNavigated', { frame, type: 'Navigation' });
    announceWorlds(tabId, frameId);
    emit(tabId, 'Page.lifecycleEvent', { frameId, loaderId: frame.loaderId, name: 'DOMContentLoaded', timestamp: 4 });
    emit(tabId, 'Page.lifecycleEvent', { frameId, loaderId: frame.loaderId, name: 'load', timestamp: 4 });
  };
  const navigatedWithinDocument = (tabId, url, { frameId = `TARGET-${tabId}` } = {}) => {
    const tab = tabOf(tabId);
    if (tab && frameId === `TARGET-${tabId}`)
      tab.url = url;
    emit(tabId, 'Page.navigatedWithinDocument', { frameId, url });
  };
  const attachFrame = (tabId, frameId, { parentFrameId = `TARGET-${tabId}` } = {}) => emit(tabId, 'Page.frameAttached', { frameId, parentFrameId });
  const detachFrame = (tabId, frameId) => emit(tabId, 'Page.frameDetached', { frameId, reason: 'remove' });
  // url: the script that logged it, which says nothing about the document it ran in.
  const consoleCall = (tabId, text, { type = 'log', url = 'https://cdn.example/app.js' } = {}) =>
    emit(tabId, 'Runtime.consoleAPICalled', { type, args: [{ type: 'string', value: text }], executionContextId: mainWorlds.get(tabId), timestamp: Date.now(), stackTrace: { callFrames: [{ functionName: '', scriptId: '1', url, lineNumber: 1, columnNumber: 1 }] } });
  const pageError = (tabId, text) =>
    emit(tabId, 'Runtime.exceptionThrown', { timestamp: Date.now(), exceptionDetails: { exceptionId: nextId++, text: 'Uncaught', lineNumber: 1, columnNumber: 1, exception: { type: 'object', subtype: 'error', className: 'Error', description: `Error: ${text}\n    at fixture.js:1:1` }, stackTrace: { callFrames: [] } } });
  // A whole document navigation as Chrome reports it: each hop, the final response, the commit.
  const navigation = (tabId, hops, { loaderId = `LOADER-${nextId++}`, frameId = `TARGET-${tabId}`, parentId, status = 200, headers = { 'content-type': 'text/html' }, mimeType = 'text/html', body = '<!doctype html>' } = {}) => {
    const { lastUrl } = requestChain(tabId, hops, { navigation: true, requestId: loaderId, frameId });
    respond(tabId, loaderId, lastUrl, { status, headers, mimeType, body, frameId });
    commit(tabId, lastUrl, { loaderId, frameId, parentId });
    return { frameId, loaderId };
  };
  // Answers the next Page.navigate of this tab with a navigation the test scripted.
  const onNextNavigate = (tabId, run) => {
    tabOf(tabId).onNavigate = (params) => {
      tabOf(tabId).onNavigate = undefined;
      const loaderId = `LOADER-${nextId++}`;
      setImmediate(() => run({ url: params.url, loaderId }));
      return { frameId: `TARGET-${tabId}`, loaderId };
    };
  };
  // A tab the owner or the page closes, as the pinned relay consumes it (ExtensionProtocolV2, chrome.tabs.onRemoved).
  const removeTab = (tabId) => {
    known.delete(tabId);
    send({ method: 'chrome.tabs.onRemoved', params: [tabId, { windowId: 1, isWindowClosing: false }] });
  };
  return {
    commands, fetchAnswers, emit, removeTab, tab: tabOf, requestChain, respond, failRequest, commit, navigation, onNextNavigate, navigatedWithinDocument, attachFrame, detachFrame, consoleCall, pageError,
    responseStage, fetchPatterns: (tabId) => fetchPatterns.get(tabId) ?? null,
    // frameId null: Chrome names no frame. sessionId: reported on a frame's or worker's own session, not the tab's.
    downloadWillBegin: (tabId, { sessionId, frameId = `TARGET-${tabId}`, ...params }) => send({ method: 'chrome.debugger.onEvent',
      params: [{ tabId, ...(sessionId ? { sessionId } : {}) }, 'Page.downloadWillBegin', { ...(frameId === null ? {} : { frameId }), ...params }] }),
    downloadProgress: (tabId, params) => emit(tabId, 'Page.downloadProgress', params),
    detach: (tabId, reason) => send({ method: 'chrome.debugger.onDetach', params: [{ tabId }, reason] }),
    cdp: (method) => commands.filter((command) => command.method === 'chrome.debugger.sendCommand' && command.params?.[1] === method).map((command) => command.params?.[2]),
    close: (code = 1000, reason = 'fake Chrome closed') => socket.close(code, reason),
    terminate: () => socket.terminate(),
  };
}
