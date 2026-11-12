// The skill's connection, server and route handler, over the real relay and Playwright's own consumers and
// MCP tools (the pinned patch applied in memory). Only Chrome is a stand-in (helpers/fake_chrome.mjs) and the
// host's policy a table: no browser, extension, profile or external address. What Chrome does here is the
// test's statement of it. These tests hold the adapter's decisions against what upstream really delivers.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { NODE_MODULES, requirePinnedInstall, scratchDir } from './helpers/env.mjs';
import { startMockGuard } from './helpers/mock_guard.mjs';
import { retainingIntake, sha256 } from './helpers/mock_intake.mjs';
import { connectFakeChrome } from './helpers/fake_chrome.mjs';
import { parseGuardEndpoint } from '../../skills/chrome_browser/lib/guard.mjs';
import { loadPinnedPlaywright, PINS } from '../../skills/chrome_browser/lib/pinned.mjs';
import { argumentRefusal, createServerCore, describePrivateFiles, describeSavedBody, imageTypeOf, upstreamConfig, MAX_RESPONSE_IMAGE_BYTES, MAX_RESPONSE_BODY_BYTES, NAVIGATION_TIMEOUT_MS } from '../../skills/chrome_browser/lib/server.mjs';
import { MAX_DOCUMENT_ADDRESSES } from '../../skills/chrome_browser/lib/connection.mjs';
import { DOWNLOAD_MAX_BYTES, MAX_BEGINS, MAX_CHAINS, MAX_RESPONSES, RESPONSE_STAGE_PATTERN } from '../../skills/chrome_browser/lib/download.mjs';

requirePinnedInstall();
const TOKEN = 'e'.repeat(64);
const TAB = 7;
const ALLOWED = 'https://allowed.example';
const FORBIDDEN = 'https://forbidden.example';
const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 4]);

const runtime = { ...loadPinnedPlaywright(NODE_MODULES), extension: PINS.extension };
const textOf = (result) => (result.content || []).filter((part) => part.type === 'text').map((part) => part.text).join('\n');
const sectionOf = (result, name) => textOf(result).split(/^### /m).slice(1)
    .find((section) => section.startsWith(`${name}\n`))?.slice(name.length + 1).trim();
const refuseForbidden = (message) => {
  if ('armed' in message)
    return { armed: true };
  if (message.action)
    return { block: '' };
  return { block: message.url.includes('forbidden.example') ? `REFUSED_BY_HOST: ${new URL(message.url).origin}` : '' };
};

async function waitFor(predicate, what) {
  const deadline = Date.now() + 20_000;
  for (;;) {
    const value = await predicate();
    if (value)
      return value;
    if (Date.now() > deadline)
      throw new Error(`not in time: ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

const cleanups = [];
test.afterEach(async () => {
  while (cleanups.length)
    await cleanups.pop()().catch(() => {});
});

// A server with the host's guard (the mock's table) and an owner who approved in a stand-in Chrome.
async function connected({ decide = refuseForbidden, tabs = [{ id: TAB, url: `${ALLOWED}/start`, title: 'Start', windowId: 1, index: 0, active: true }], refuseAttach, guardDelayMs = 0, prepare, onCommand, answer, downloadSettleMs } = {}) {
  const world = {};
  const guard = await startMockGuard({ token: TOKEN, decide: (message) => decide(message, world), delayMs: guardDelayMs });
  cleanups.push(() => guard.close());
  const core = createServerCore({
    loadRuntime: () => runtime,
    guardEnvironment: { configured: true, endpoint: parseGuardEndpoint(guard.endpoint), token: TOKEN },
    framePreparer: { prepare: async (page) => { await prepare?.(page, world); return { prepared: false }; } },
    ...(downloadSettleMs ? { downloadSettleMs } : {}),
  });
  cleanups.push(() => core.shutdown());
  const link = textOf(await core.callTool('browser_connect')).split('\n').find((line) => line.startsWith('chrome-extension://'));
  const chrome = await connectFakeChrome(runtime.utils.ws, new URL(link).searchParams.get('mcpRelayUrl'), { tabs, refuseAttach, onCommand: onCommand && ((...command) => onCommand(world, ...command)), answer });
  cleanups.push(async () => chrome.close());
  Object.assign(world, {
    guard, core, chrome, link,
    call: async (name, args = {}) => await core.callTool(name, args),
    page: () => core.connection.backend()._context.currentTab().page,
    // A round trip through Playwright and the stand-in: everything Chrome said before it has been delivered.
    settle: async () => await world.page().context().cookies(),
    questions: () => guard.messages().filter((message) => message && 'url' in message).map((message) => message.url),
  });
  await waitFor(() => ['ready', 'closed'].includes(core.connection.state), 'connection state');
  return world;
}

test('the route is in place before the guard is armed, and each routed request follows the host\'s answer', async () => {
  let enabledAtArm;
  const raw = 'not an answer\n';
  const world = await connected({
    decide: (message, w) => {
      if ('armed' in message)
        enabledAtArm = w.chrome.cdp('Fetch.enable').length;
      if (message.url?.includes('silent.example'))
        return raw;
      return refuseForbidden(message);
    },
  });
  assert.equal(world.core.connection.state, 'ready');
  assert.ok(enabledAtArm >= 1, 'Playwright asked Chrome to pause requests before the host was told the guard is armed');
  const { chrome, guard } = world;
  const answered = async (count) => await waitFor(() => chrome.fetchAnswers.length >= count && chrome.fetchAnswers, 'Fetch answers');
  chrome.requestChain(TAB, [`${ALLOWED}/api`]);
  assert.deepEqual((await answered(1)).map((a) => a.method), ['Fetch.continueRequest'], 'an empty answer lets the request go');
  chrome.requestChain(TAB, [`${FORBIDDEN}/api`]);
  const refused = (await answered(2))[1];
  assert.deepEqual([refused.method, refused.errorReason], ['Fetch.failRequest', 'BlockedByClient'], 'a refusal fails it as blocked by client');
  chrome.requestChain(TAB, ['https://silent.example/api']);
  const unanswered = (await answered(3))[2];
  assert.deepEqual([unanswered.method, unanswered.errorReason], ['Fetch.failRequest', 'BlockedByClient'], 'no usable answer fails closed');
  chrome.requestChain(TAB, [{ url: `${ALLOWED}/form`, method: 'POST', postData: 'a=1' }]);
  await answered(4);
  const form = guard.messages().find((message) => message?.url === `${ALLOWED}/form`);
  assert.deepEqual([form.method, form.post_data, form.body_present, form.resource_type], ['POST', 'a=1', true, 'fetch'], 'the host sees the request as it is');
  assert.deepEqual(world.core.guard.counters, { allowed: 2, blocked: 1, failed_closed: 1 });
});

test('requests the guard refused while a call ran are reported with it', async () => {
  const { chrome, call } = await connected();
  chrome.onNextNavigate(TAB, ({ url, loaderId }) => {
    chrome.requestChain(TAB, [`${FORBIDDEN}/beacon`]);
    chrome.navigation(TAB, [url], { loaderId });
  });
  const result = await call('browser_navigate', { url: `${ALLOWED}/next` });
  assert.match(textOf(result), /### Requests the guard refused while this call ran\n- GET https:\/\/forbidden\.example\/beacon: REFUSED_BY_HOST/);
});

test('an approval with no tab attached is not ready; nothing replaces the tab, and the next link says why', async () => {
  const chromeAnswer = 'Another debugger is already attached to the tab with id: 7.';
  const { core, chrome, call, link } = await connected({ refuseAttach: new Map([[TAB, chromeAnswer]]) });
  assert.equal(core.connection.state, 'closed');
  assert.match(core.connection.reason, /^BROWSER_TAB_NOT_ATTACHED: .*Another debugger is already attached/);
  const tool = await call('browser_snapshot');
  assert.equal(tool.isError, true);
  assert.match(textOf(tool), /^### Error\nBROWSER_TAB_NOT_ATTACHED/);
  assert.equal(chrome.commands.filter((command) => command.method === 'chrome.tabs.create').length, 0, 'no tab was opened in its place');
  const next = textOf(await call('browser_connect'));
  assert.match(next, /^### Approval needed\nThe previous connection ended: BROWSER_TAB_NOT_ATTACHED/);
  assert.notEqual(next.split('\n').find((line) => line.startsWith('chrome-extension://')), link, 'a new link');
});

test('connection loss keeps the WebSocket observation before Playwright tears down its context', async () => {
  const { core, chrome, call } = await connected();
  chrome.close(1000, 'Playwright client disconnected');
  await waitFor(() => core.connection.state === 'closed', 'WebSocket loss');
  assert.deepEqual(core.connection.diagnostic.terminal.source, 'extension_websocket_close');
  assert.equal(core.connection.diagnostic.terminal.wsCode, 1000);
  assert.equal(core.connection.diagnostic.terminal.reason, 'playwright_client_disconnected');
  assert.match(textOf(await call('browser_snapshot')), /first observed at extension_websocket_close/);
});

test('official extension close reasons survive as safe peer observations without attributing a trigger', async () => {
  for (const [wireReason, expected] of [
    ['User disconnected', 'user_disconnected'],
    ['All controlled tabs detached', 'all_controlled_tabs_detached'],
  ]) {
    const { core, chrome, call } = await connected();
    chrome.close(1000, wireReason);
    await waitFor(() => core.connection.state === 'closed', 'official extension close');
    assert.equal(core.connection.diagnostic.terminal.source, 'extension_websocket_close');
    assert.equal(core.connection.diagnostic.terminal.wsCode, 1000);
    assert.equal(core.connection.diagnostic.terminal.reason, expected);
    const refusal = textOf(await call('browser_snapshot'));
    assert.match(refusal, new RegExp(`generation ${core.connection.diagnostic.generation}: first observed at extension_websocket_close`));
    assert.match(refusal, new RegExp(`peer reason ${expected}`));
    assert.match(refusal, /underlying trigger unknown/);
  }
});

test('unknown and empty socket reasons are never retained or emitted; abnormal closure keeps its code', async () => {
  for (const [close, expectedCode] of [
    [(chrome) => chrome.close(1000, 'plainAlphanumericSecret4821'), 1000],
    [(chrome) => chrome.close(1000, ''), 1000],
    [(chrome) => chrome.terminate(), 1006],
  ]) {
    const { core, chrome, call } = await connected();
    close(chrome);
    await waitFor(() => core.connection.state === 'closed', 'socket close');
    assert.deepEqual(core.connection.diagnostic.terminal.source, 'extension_websocket_close');
    assert.equal(core.connection.diagnostic.terminal.wsCode, expectedCode);
    assert.equal(core.connection.diagnostic.terminal.reason, 'unknown');
    const output = `${JSON.stringify(core.connection.diagnostic)} ${core.connection.reason} ${textOf(await call('browser_snapshot'))} ${textOf(await call('browser_connect'))}`;
    assert.doesNotMatch(output, /plainAlphanumericSecret4821/);
  }
});

test('CDP close and context-only loss state the observed layer, without assigning a trigger', async () => {
  const cdp = await connected();
  cdp.core.connection._relay._closeCDPConnection('plainAlphanumericSecret4821');
  await waitFor(() => cdp.core.connection.state === 'closed', 'CDP close');
  assert.deepEqual(cdp.core.connection.diagnostic.terminal, {
    at: cdp.core.connection.diagnostic.terminal.at, source: 'cdp_close', reason: 'unknown',
  });
  assert.match(cdp.core.connection.reason, /first observed at cdp_close.*underlying trigger unknown/);
  assert.doesNotMatch(cdp.core.connection.reason, /plainAlphanumericSecret4821/);

  const backend = await connected();
  backend.core.connection.backend().emit('disconnected');
  await waitFor(() => backend.core.connection.state === 'closed', 'backend close');
  assert.equal(backend.core.connection.diagnostic.terminal.source, 'backend_disconnected');
  assert.match(backend.core.connection.reason, /underlying trigger unknown/);
});

test('debugger detach is a preceding observation, and removing one of two tabs does not close the connection', async () => {
  const tabs = [
    { id: TAB, url: `${ALLOWED}/start`, title: 'Start', windowId: 1, index: 0, active: true },
    { id: 8, url: `${ALLOWED}/other`, title: 'Other', windowId: 1, index: 1, active: false },
  ];
  const { core, chrome, settle } = await connected({ tabs });
  chrome.detach(TAB, 'plainAlphanumericSecret4821');
  await waitFor(() => core.connection.diagnostic.lastDetach, 'debugger detach');
  assert.equal(core.connection.diagnostic.lastDetach.reason, 'unknown');
  assert.equal(core.connection.state, 'ready');
  chrome.removeTab(TAB);
  await settle();
  assert.equal(core.connection.state, 'ready');
  assert.equal(core.connection.diagnostic.terminal, null);
  chrome.detach(8, 'target_closed');
  await waitFor(() => core.connection.diagnostic.lastDetach?.reason === 'target_closed', 'known detach');
  chrome.terminate();
  await waitFor(() => core.connection.state === 'closed', 'later socket close');
  assert.equal(core.connection.diagnostic.terminal.source, 'extension_websocket_close');
  assert.equal(core.connection.diagnostic.lastDetach.reason, 'target_closed');
  assert.ok(core.connection.diagnostic.lastDetach.at <= core.connection.diagnostic.terminal.at);
  assert.doesNotMatch(JSON.stringify(core.connection.diagnostic), /plainAlphanumericSecret4821/);
});

test('explicit disconnect and shutdown precede teardown echoes; a fresh approval uses a new generation', async () => {
  const { core, chrome, call, link } = await connected();
  const oldRelay = core.connection._relay;
  const oldSocket = oldRelay._extensionConnection._ws;
  await call('browser_disconnect');
  assert.equal(core.connection.diagnostic.terminal.source, 'explicit_disconnect');
  const previous = core.connection.reason;
  oldSocket.emit('close', 1006, Buffer.from('plainAlphanumericSecret4821'));
  assert.equal(core.connection.reason, previous);
  const next = textOf(await call('browser_connect'));
  const newLink = next.split('\n').find((line) => line.startsWith('chrome-extension://'));
  assert.notEqual(newLink, link);
  assert.match(textOf(await call('browser_snapshot')), /BROWSER_NEEDS_APPROVAL.*Nothing was done and nothing was queued/);
  assert.equal(core.connection.diagnostic.terminal, null);
  const newChrome = await connectFakeChrome(runtime.utils.ws, new URL(newLink).searchParams.get('mcpRelayUrl'), {
    tabs: [{ id: TAB, url: `${ALLOWED}/fresh`, title: 'Fresh', windowId: 1, index: 0, active: true }],
  });
  cleanups.push(async () => newChrome.close());
  await waitFor(() => core.connection.state === 'ready', 'new approval');
  oldRelay._handler.handleExtensionEvent('chrome.debugger.onDetach', [{ tabId: TAB }, 'target_closed']);
  oldSocket.emit('close', 1006, Buffer.from('plainAlphanumericSecret4821'));
  assert.equal(core.connection.diagnostic.lastDetach, null);
  assert.equal(core.connection.diagnostic.terminal, null);
  assert.equal(core.connection.state, 'ready');
  assert.match(textOf(await call('browser_snapshot')), /Fresh page content/);

  await core.shutdown();
  assert.equal(core.connection.diagnostic.terminal.source, 'shutdown');
  assert.equal(core.connection.state, 'closed');
  await chrome.close();
});

test('an action sent before the connection vanishes has unknown effect and is never replayed', async () => {
  let sent = 0;
  const world = await connected({ onCommand: (w, method, params) => {
    if (method === 'chrome.debugger.sendCommand' && params?.[1] === 'Page.navigate') {
      sent += 1;
      w.chrome.terminate();
    }
  } });
  const result = await world.call('browser_navigate', { url: `${ALLOWED}/action` });
  assert.equal(sent, 1);
  assert.equal(result.isError, true);
  assert.match(textOf(result), /Connection during call/);
  assert.match(textOf(result), /does not establish all of its effects.*Do not repeat it automatically/);
  assert.equal(sectionOf(result, 'Result'), undefined);
  assert.doesNotMatch(textOf(result), /Nothing was done/);
  assert.equal(world.core.connection.state, 'closed');
  const newCall = await world.call('browser_navigate', { url: `${ALLOWED}/another` });
  assert.match(textOf(newCall), /Nothing was done and no earlier action is repeated/);
  await reapprove(world);
  assert.equal(sent, 1);
});

// Only page I/O is replaced. The pinned BrowserBackend, official browser_evaluate handler,
// Response serialization and Tab.waitForCompletion/waitForTimeout all run unchanged.
// The returned value is page data, absent from the function and its generated-code echo.
const RECEIPT_FUNCTION = '() => { receipt.calls += 1; return receipt.value; }';
function evaluationFixture(world, { beforeReply, settling } = {}) {
  const page = world.page();
  const receipt = { calls: 0, value: 'CONFIRMED_RESULT_273' };
  const events = [];
  page.evaluate = async (fn, arg) => {
    if (arg === RECEIPT_FUNCTION) {
      const answer = await vm.runInNewContext(`(${fn.toString()})(expression)`, { receipt, expression: arg });
      events.push('evaluated');
      await beforeReply?.();
      events.push('replied');
      return answer;
    }
    assert.equal(typeof arg, 'number', 'the next page read is the real Tab.waitForTimeout settling read');
    assert.equal(events.at(-1), 'replied');
    events.push('settling');
    await settling?.();
    return undefined;
  };
  return { receipt, events, call: () => world.call('browser_evaluate', { function: RECEIPT_FUNCTION }) };
}

async function loseConnection(world) {
  world.chrome.close(1000, 'privatePeerReason273');
  await waitFor(() => world.core.connection.state === 'closed', 'settling connection loss');
  await world.core.connection._closing;
}

async function reapprove(world) {
  const link = textOf(await world.call('browser_connect')).split('\n').find((line) => line.startsWith('chrome-extension://'));
  const chrome = await connectFakeChrome(runtime.utils.ws, new URL(link).searchParams.get('mcpRelayUrl'), {
    tabs: [{ id: TAB, url: `${ALLOWED}/fresh`, title: 'Fresh', windowId: 1, index: 0, active: true }],
  });
  cleanups.push(async () => chrome.close());
  await waitFor(() => world.core.connection.state === 'ready', 'reapproval');
  const fresh = await world.call('browser_snapshot');
  assert.equal(fresh.isError, undefined, textOf(fresh));
  assert.match(textOf(fresh), /Fresh page content/);
  assert.equal(chrome.cdp('Runtime.callFunctionOn').some((params) => JSON.stringify(params).includes(RECEIPT_FUNCTION)), false,
      'the old evaluation was not replayed into the new browser context');
  assert.equal(chrome.cdp('Page.navigate').length, 0, 'no old navigation was replayed after approval');
  assert.equal(chrome.commands.filter((command) => command.method === 'chrome.tabs.create').length, 0,
      'no old tab creation was replayed after approval');
}

test('the pinned backend itself retains Result and Error through actual settling teardown', async () => {
  for (const withError of [false, true]) {
    const world = await connected();
    const backend = world.core.connection.backend();
    const fixture = evaluationFixture(world, { settling: async () => {
      if (withError)
        backend._context._pendingUnhandledRejections.push(new Error('AFTER_RESULT_FAILURE_273'));
      await loseConnection(world);
    } });
    const result = await backend.callTool('browser_evaluate', { function: RECEIPT_FUNCTION });
    assert.deepEqual(fixture.events, ['evaluated', 'replied', 'settling']);
    assert.equal(result.isError, withError ? true : undefined);
    assert.equal(sectionOf(result, 'Result'), JSON.stringify(fixture.receipt.value));
    assert.doesNotMatch(sectionOf(result, 'Ran Playwright code'), /CONFIRMED_RESULT_273/);
    if (withError)
      assert.match(sectionOf(result, 'Error'), /AFTER_RESULT_FAILURE_273/);
    assert.equal(backend._context.currentTab(), undefined);
    assert.equal(fixture.receipt.calls, 1);
  }
});

test('an acknowledged Result survives actual pinned settling, including an accompanying backend error', async () => {
  for (const loss of [true, false]) {
    for (const withError of [true, false]) {
      const world = await connected();
      const backend = world.core.connection.backend();
      const fixture = evaluationFixture(world, { settling: async () => {
        if (withError)
          backend._context._pendingUnhandledRejections.push(new Error('AFTER_RESULT_FAILURE_273'));
        if (loss)
          await loseConnection(world);
      } });
      const result = await fixture.call();
      assert.deepEqual(fixture.events, ['evaluated', 'replied', 'settling']);
      assert.equal(result.isError, withError ? true : undefined, textOf(result));
      assert.equal(sectionOf(result, 'Result'), JSON.stringify(fixture.receipt.value), textOf(result));
      assert.doesNotMatch(sectionOf(result, 'Ran Playwright code'), /CONFIRMED_RESULT_273/);
      if (withError)
        assert.match(sectionOf(result, 'Error'), /AFTER_RESULT_FAILURE_273/);
      if (loss) {
        assert.equal(backend._context.currentTab(), undefined, 'upstream really disposed its context');
        assert.match(textOf(result), /Connection during call/);
        assert.match(textOf(result), /Do not repeat it automatically/);
        assert.doesNotMatch(textOf(result), /effect is not unknown|received before the connection ended|privatePeerReason273/);
        await reapprove(world);
      }
      assert.equal(fixture.receipt.calls, 1, 'neither loss nor reapproval replays the evaluation');
    }
  }
});

test('a genuinely lost evaluation reply has no Result, retains the error, and is not replayed on reapproval', async () => {
  const world = await connected();
  const fixture = evaluationFixture(world, { beforeReply: async () => {
    await loseConnection(world);
    throw new Error('EVALUATION_REPLY_LOST_273');
  } });
  const result = await fixture.call();
  assert.equal(fixture.receipt.calls, 1, 'the fixture executed before its reply was lost');
  assert.deepEqual(fixture.events, ['evaluated']);
  assert.equal(result.isError, true);
  assert.equal(sectionOf(result, 'Result'), undefined);
  assert.equal(sectionOf(result, 'Ran Playwright code'), undefined);
  assert.match(sectionOf(result, 'Error'), /EVALUATION_REPLY_LOST_273/);
  assert.match(textOf(result), /Do not repeat it automatically/);
  assert.doesNotMatch(textOf(result), /CONFIRMED_RESULT_273|Nothing was done|effect is not unknown|privatePeerReason273/);
  await reapprove(world);
  assert.equal(fixture.receipt.calls, 1);
});

test('an acknowledged Result from a refused redirect is withheld after teardown and reapproval', async () => {
  for (const withError of [false, true]) {
    const world = await connected();
    const backend = world.core.connection.backend();
    const fixture = evaluationFixture(world, {
      beforeReply: async () => {
        world.chrome.navigation(TAB, [`${ALLOWED}/from`, `${FORBIDDEN}/bounce?secret=273`, `${ALLOWED}/landing`]);
        await world.settle();
      },
      settling: async () => {
        if (withError)
          backend._context._pendingUnhandledRejections.push(new Error('PRIVATE_ERROR_273'));
        await loseConnection(world);
        await reapprove(world); // the next call empties connection.closedPages
      },
    });
    const result = await fixture.call();
    assert.equal(result.isError, true);
    assert.match(textOf(result), /output is withheld.*forbidden\.example\/bounce/);
    assert.match(textOf(result), /Connection during call/);
    assert.doesNotMatch(textOf(result), /CONFIRMED_RESULT_273|PRIVATE_ERROR_273|secret=273|privatePeerReason273/);
    assert.ok(world.questions().includes(`${FORBIDDEN}/bounce?secret=273`), 'the original redirect provenance was checked');
    assert.equal(fixture.receipt.calls, 1);
  }
});

test('a tab-list Result keeps its original title checks after teardown and a new session call', async () => {
  for (const [forbidden, title] of [[true, 'PRIVATE_TITLE_273'], [false, 'READABLE_TITLE_273'], [true, ''], [false, '']]) {
    const world = await connected({ tabs: [
      { id: TAB, url: `${ALLOWED}/start`, title: 'Start', windowId: 1, index: 0, active: true },
      { id: 8, url: `${forbidden ? FORBIDDEN : ALLOWED}/other`, title, windowId: 1, index: 1 },
    ] });
    const backend = world.core.connection.backend();
    let reads = 0;
    // Response._build reads the current tab's local console log after the tabs tool
    // already built its Result. End the old context here, then let a fresh call run.
    backend._context.currentTab()._consoleLog.take = async () => {
      reads += 1;
      await loseConnection(world);
      await reapprove(world);
      assert.deepEqual(world.core.connection.closedPages, []);
      return undefined;
    };
    const result = await world.call('browser_tabs', { action: 'list' });
    assert.equal(reads, 1);
    assert.equal(backend._context.currentTab(), undefined);
    assert.equal(backend._context.tabs().length, 0);
    assert.equal(result.isError, forbidden ? true : undefined, textOf(result));
    if (forbidden) {
      assert.match(textOf(result), /output is withheld.*forbidden\.example\/other/);
      assert.doesNotMatch(textOf(result), /PRIVATE_TITLE_273/);
    } else {
      assert.ok(sectionOf(result, 'Result').includes(`[${title}](${ALLOWED}/other)`), textOf(result));
    }
    assert.match(textOf(result), /Connection during call/);
    assert.doesNotMatch(textOf(result), /privatePeerReason273/);
  }
});

test('a tab opened during a call keeps its output check after teardown, even with an empty title', async () => {
  for (const forbidden of [true, false]) {
    const world = await connected({ onCommand: ({ chrome }, method, params) => {
      const id = params?.[0]?.tabId;
      if (method === 'chrome.debugger.attach' && id >= 900) {
        chrome.onNextNavigate(id, ({ url, loaderId }) => {
          chrome.navigation(id, [url, `${forbidden ? FORBIDDEN : ALLOWED}/landing`], { loaderId });
        });
      }
    } });
    const backend = world.core.connection.backend();
    let reads = 0;
    world.page().context().on('page', (page) => {
      page.once('framenavigated', () => {
        runtime.bundle.tools.Tab.forPage(page)._consoleLog.take = async () => {
          reads += 1;
          await loseConnection(world);
          await reapprove(world);
          return undefined;
        };
      });
    });
    const result = await world.call('browser_tabs', { action: 'new', url: `${ALLOWED}/go` });
    assert.equal(reads, 1);
    assert.equal(backend._context.tabs().length, 0);
    assert.equal(result.isError, forbidden ? true : undefined, textOf(result));
    if (forbidden) {
      assert.match(textOf(result), /output is withheld.*forbidden\.example\/landing/);
      assert.equal(sectionOf(result, 'Result'), undefined);
    } else {
      assert.ok(sectionOf(result, 'Result').includes(`[](${ALLOWED}/landing)`), textOf(result));
    }
    assert.match(textOf(result), /Connection during call/);
    assert.equal(world.chrome.commands.filter((command) => command.method === 'chrome.tabs.create').length, 1);
  }
});

test('loss while attaching records the socket and never becomes ready', async () => {
  for (const [interrupt, code] of [
    [(socket) => socket.terminate(), 1006],
    [(socket) => socket.close(1000, 'plainAlphanumericSecret4821'), 1000],
  ]) {
    let interrupted = false;
    const { core, call, chrome } = await connected({ onCommand: (_world, method, _params, socket) => {
      if (!interrupted && method === 'chrome.debugger.attach') {
        interrupted = true;
        interrupt(socket);
      }
    } });
    assert.equal(interrupted, true);
    assert.equal(core.connection.state, 'closed');
    assert.equal(core.connection.diagnostic.terminal.source, 'extension_websocket_close');
    assert.equal(core.connection.diagnostic.terminal.wsCode, code);
    const answer = textOf(await call('browser_snapshot'));
    assert.match(answer, /first observed at extension_websocket_close/);
    assert.doesNotMatch(`${answer} ${core.connection.reason} ${JSON.stringify(core.connection.diagnostic)}`, /plainAlphanumericSecret4821/);
    assert.equal(chrome.commands.filter((command) => command.method === 'chrome.tabs.create').length, 0);
  }
});

test('removing the last tab is a tab event, not itself a recorded transport loss', async () => {
  const { core, chrome } = await connected();
  chrome.removeTab(TAB);
  await waitFor(() => chrome.tab(TAB) === undefined, 'last tab removed');
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(core.connection.diagnostic.terminal, null);
  assert.equal(core.connection.state, 'ready');
});

test('a host that does not confirm the guard leaves no working connection', async () => {
  const { core, call } = await connected({
    decide: (message) => ('armed' in message ? { armed: false, block: 'BROWSER_SESSION_CLOSED: closing' } : { block: '' }),
  });
  assert.equal(core.connection.state, 'closed');
  assert.match(core.connection.reason, /^BROWSER_GUARD_NOT_ARMED: .*BROWSER_SESSION_CLOSED/);
  assert.match(textOf(await call('browser_snapshot')), /BROWSER_GUARD_NOT_ARMED/);
});

test('an address the host refuses is not opened, by either tool; an allowed one is', async () => {
  const { chrome, call, questions } = await connected();
  const navigate = await call('browser_navigate', { url: 'forbidden.example/x' });
  assert.equal(navigate.isError, true);
  assert.match(textOf(navigate), /^### Error\nREFUSED_BY_HOST.*\nThe address was not opened; the tab was left as it was\./);
  assert.ok(questions().includes(`${FORBIDDEN}/x`), 'judged as upstream would open it');
  assert.deepEqual(chrome.cdp('Page.navigate'), []);
  const opened = await call('browser_tabs', { action: 'new', url: `${FORBIDDEN}/y` });
  assert.equal(opened.isError, true);
  assert.match(textOf(opened), /No tab was opened\./);
  assert.equal(chrome.commands.filter((command) => command.method === 'chrome.tabs.create').length, 0);
  // Positive controls: the same paths reach upstream for an address the host allows.
  chrome.onNextNavigate(TAB, ({ url, loaderId }) => chrome.navigation(TAB, [url], { loaderId }));
  const allowed = await call('browser_navigate', { url: `${ALLOWED}/next` });
  assert.equal(allowed.isError, undefined, textOf(allowed));
  assert.deepEqual(chrome.cdp('Page.navigate').map((params) => params.url), [`${ALLOWED}/next`]);
  await call('browser_tabs', { action: 'new' });
  const created = chrome.commands.filter((command) => command.method === 'chrome.tabs.create');
  assert.equal(created.length, 1);
  assert.equal(created[0].params[0].active, false, 'an agent tab is created inactive');
});

test('a page the task may not read: nothing is done before, output is withheld after, and it can be left', async () => {
  const { chrome, call, settle } = await connected();
  const start = await call('browser_snapshot');
  assert.equal(start.isError, undefined);
  assert.match(textOf(start), /Start page content/, 'the positive control reads the page');
  // The agent's own navigation ends on a page the task may not read, through a redirect.
  chrome.tab(TAB).title = 'SECRET';
  chrome.onNextNavigate(TAB, ({ loaderId, url }) => chrome.navigation(TAB, [url, `${FORBIDDEN}/landing`], { loaderId }));
  const landed = await call('browser_navigate', { url: `${ALLOWED}/go` });
  assert.equal(landed.isError, true);
  assert.match(textOf(landed), /^### Error\nREFUSED_BY_HOST.*\nThe call ran, but its output is withheld: this task may not read https:\/\/forbidden\.example\/landing/);
  assert.doesNotMatch(textOf(landed), /SECRET/);
  // A page tool does nothing there; the check asks about the address and never reads the page.
  const pageCalls = chrome.cdp('Runtime.callFunctionOn').length;
  const click = await call('browser_click', { target: 'e1' });
  assert.equal(click.isError, true);
  assert.match(textOf(click), /Nothing was done: this task may not read https:\/\/forbidden\.example\/landing/);
  assert.equal(chrome.cdp('Runtime.callFunctionOn').length, pageCalls, 'the tool did not run');
  // Going elsewhere is never refused because of the page being left.
  chrome.tab(TAB).title = 'Clean';
  chrome.onNextNavigate(TAB, ({ loaderId, url }) => chrome.navigation(TAB, [url], { loaderId }));
  const left = await call('browser_navigate', { url: `${ALLOWED}/elsewhere` });
  assert.equal(left.isError, undefined, textOf(left));
  await settle();
  assert.match(textOf(await call('browser_snapshot')), /Clean page content/);
});

test('another tab\'s title is judged with its tab; a tab the task may read is listed as before', async () => {
  const shared = { id: TAB, url: `${ALLOWED}/start`, title: 'Shared', windowId: 1, index: 0, active: true };
  const one = await connected({ tabs: [shared, { id: 8, url: `${FORBIDDEN}/private`, title: 'SECRET TITLE', windowId: 1, index: 1 }] });
  await waitFor(() => one.core.connection.backend()._context.tabs().length === 2, 'both tabs');
  const listed = await one.call('browser_tabs', { action: 'list' });
  assert.equal(listed.isError, true);
  assert.doesNotMatch(textOf(listed), /SECRET TITLE/);
  assert.match(textOf(listed), /this task may not read https:\/\/forbidden\.example\/private/);
  const two = await connected({ tabs: [shared, { id: 8, url: `${ALLOWED}/two`, title: 'Second tab', windowId: 1, index: 1 }] });
  await waitFor(() => two.core.connection.backend()._context.tabs().length === 2, 'both tabs');
  const fine = await two.call('browser_tabs', { action: 'list' });
  assert.equal(fine.isError, undefined, textOf(fine));
  assert.match(textOf(fine), /Second tab/);
});

test('a tab title is judged with its tab however the result is formatted: upstream\'s output options are refused, not followed', async () => {
  // JSON escapes a quote and a backslash, so a title looked for in the text of a JSON result would not be found there.
  const shared = { id: TAB, url: `${ALLOWED}/start`, title: 'Shared', windowId: 1, index: 0, active: true };
  const { core, call } = await connected({ tabs: [shared, { id: 8, url: `${FORBIDDEN}/private`, title: 'SECRET "QUOTED" \\ TITLE', windowId: 1, index: 1 }] });
  await waitFor(() => core.connection.backend()._context.tabs().length === 2, 'both tabs');
  const plain = await call('browser_tabs', { action: 'list' });
  assert.equal(plain.isError, true);
  assert.doesNotMatch(textOf(plain), /SECRET/);
  assert.match(textOf(plain), /this task may not read https:\/\/forbidden\.example\/private/);
  const json = await call('browser_tabs', { action: 'list', _meta: { json: true } });
  assert.equal(json.isError, true);
  assert.match(textOf(json), /^### Error\nBROWSER_ARGUMENT_REFUSED: .*"_meta"/);
  assert.doesNotMatch(textOf(json), /SECRET/);
});

// The document that shows is the one the hop produced until another really replaces it.
test('a forbidden hop stays with its document until a new document commits: a response alone retires nothing', async () => {
  const { chrome, call, settle } = await connected();
  const snapshot = async () => {
    await settle();
    return await call('browser_snapshot');
  };
  chrome.navigation(TAB, [`${ALLOWED}/from`, `${FORBIDDEN}/bounce`, `${ALLOWED}/landing`]);
  assert.match(textOf(await snapshot()), /this task may not read https:\/\/forbidden\.example\/bounce/, 'the hop is judged');
  const stillThere = (result, what) => {
    assert.equal(result.isError, true, what);
    assert.match(textOf(result), /https:\/\/forbidden\.example\/bounce/, what);
  };
  // A 204 keeps the old document.
  const noContent = chrome.requestChain(TAB, [`${ALLOWED}/no-content`], { navigation: true });
  chrome.respond(TAB, noContent.requestId, `${ALLOWED}/no-content`, { status: 204 });
  stillThere(await snapshot(), '204 response');
  chrome.failRequest(TAB, noContent.requestId);
  stillThere(await snapshot(), '204 finished');
  // A redirect whose next hop fails never commits.
  const failed = chrome.requestChain(TAB, [`${ALLOWED}/one`, `${ALLOWED}/two`], { navigation: true });
  stillThere(await snapshot(), 'redirect in flight');
  chrome.failRequest(TAB, failed.requestId, 'net::ERR_CONNECTION_REFUSED');
  stillThere(await snapshot(), 'failed redirect');
  // A download is a response with no document.
  const download = chrome.requestChain(TAB, [`${ALLOWED}/file.zip`], { navigation: true });
  chrome.respond(TAB, download.requestId, `${ALLOWED}/file.zip`, { headers: { 'content-disposition': 'attachment' }, mimeType: 'application/zip' });
  chrome.failRequest(TAB, download.requestId, 'net::ERR_ABORTED', { canceled: true });
  stillThere(await snapshot(), 'download');
  // A same-document navigation keeps the document as well.
  chrome.navigatedWithinDocument(TAB, `${ALLOWED}/landing#part`);
  stillThere(await snapshot(), 'same-document navigation');
  // A fresh document that commits clears it: no eternal refusal.
  chrome.navigation(TAB, [`${ALLOWED}/fresh`]);
  const fresh = await snapshot();
  assert.equal(fresh.isError, undefined, textOf(fresh));
});

test('a commit that names no request still carries the hops of the navigation it finished', async () => {
  const { chrome, call, settle, page } = await connected();
  const loaderId = 'L-RACED';
  chrome.requestChain(TAB, [`${ALLOWED}/from`, `${FORBIDDEN}/bounce`, `${ALLOWED}/landing`], { navigation: true, requestId: loaderId });
  chrome.respond(TAB, loaderId, `${ALLOWED}/landing`, { status: 200 });
  // Another navigation requested just before the commit: Playwright then commits with no request known.
  chrome.emit(TAB, 'Page.frameRequestedNavigation', { frameId: `TARGET-${TAB}`, reason: 'scriptInitiated', url: `${ALLOWED}/later`, disposition: 'currentTab' });
  chrome.commit(TAB, `${ALLOWED}/landing`, { loaderId });
  await settle();
  assert.equal(page().url(), `${ALLOWED}/landing`);
  assert.match(textOf(await call('browser_snapshot')), /this task may not read https:\/\/forbidden\.example\/bounce/);
});

test('a subframe\'s hop counts while its document shows, and is gone when the frame moves on or leaves', async () => {
  const { chrome, call, settle } = await connected();
  const frame = 'FRAME-1';
  const attach = () => chrome.attachFrame(TAB, frame);
  const hopInFrame = () => chrome.navigation(TAB, [`${ALLOWED}/ad`, `${FORBIDDEN}/track`, `${ALLOWED}/ad2`], { frameId: frame, parentId: `TARGET-${TAB}` });
  const snapshot = async () => {
    await settle();
    return await call('browser_snapshot');
  };
  attach();
  hopInFrame();
  assert.match(textOf(await snapshot()), /https:\/\/forbidden\.example\/track/, 'shown');
  chrome.navigation(TAB, [`${ALLOWED}/ad3`], { frameId: frame, parentId: `TARGET-${TAB}` });
  assert.equal((await snapshot()).isError, undefined, 'the frame moved on');
  hopInFrame();
  assert.equal((await snapshot()).isError, true, 'shown again');
  chrome.detachFrame(TAB, frame);
  assert.equal((await snapshot()).isError, undefined, 'the frame left');
  attach();
  hopInFrame();
  assert.equal((await snapshot()).isError, true, 'shown in a new frame');
  chrome.navigation(TAB, [`${ALLOWED}/next-document`]);
  assert.equal((await snapshot()).isError, undefined, 'a new main document takes its frames with it');
});

// A console message's script location is not its document, in either direction.
test('history: console messages and page errors are judged by the document that made them', async () => {
  const { chrome, call, settle, questions } = await connected();
  chrome.consoleCall(TAB, 'ALLOWED-ONE', { url: `${ALLOWED}/app.js` });
  chrome.navigation(TAB, [`${ALLOWED}/from`, `${FORBIDDEN}/bounce`, `${ALLOWED}/secret-doc`]);
  chrome.consoleCall(TAB, 'FORBIDDEN-MESSAGE', { url: `${ALLOWED}/app.js` });
  chrome.pageError(TAB, 'FORBIDDEN-ERROR');
  chrome.navigation(TAB, [`${ALLOWED}/public`]);
  chrome.consoleCall(TAB, 'ALLOWED-THREE', { url: `${FORBIDDEN}/library.js` });
  chrome.pageError(TAB, 'ALLOWED-ERROR');
  await settle();
  const before = questions().length;
  const all = await call('browser_console_messages', { all: true });
  const text = textOf(all);
  assert.equal(all.isError, undefined, text);
  for (const kept of ['ALLOWED-ONE', 'ALLOWED-THREE', 'ALLOWED-ERROR'])
    assert.ok(text.includes(kept), `${kept} is allowed history: ${text}`);
  for (const gone of ['FORBIDDEN-MESSAGE', 'FORBIDDEN-ERROR'])
    assert.ok(!text.includes(gone), `${gone} leaked: ${text}`);
  assert.match(text, /Total messages: 3 \(Errors: 1, Warnings: 0\)/, 'upstream counts only what it returns');
  assert.match(text, /Withheld 2 records here: this task may not read https:\/\/forbidden\.example\/bounce .*REFUSED_BY_HOST/);
  const asked = questions().slice(before);
  assert.equal(asked.length, new Set(asked).size, `one decision per address in one call: ${asked}`);
  assert.ok(asked.includes(`${FORBIDDEN}/bounce`), 'the original hop was the question');
  // The current document's messages alone contain nothing to withhold.
  const current = textOf(await call('browser_console_messages'));
  assert.ok(current.includes('ALLOWED-THREE'));
  assert.ok(!current.includes('ALLOWED-ONE'));
  assert.ok(!current.includes('Withheld'));
});

test('history: a message arriving while the host is being asked does not move older records into a later document', async () => {
  let reading = false;
  let arrived = false;
  const { chrome, call, settle } = await connected({
    guardDelayMs: 20,
    decide: (message, world) => {
      // The first question about the records: more messages come while it is being answered.
      if (reading && !arrived && message.url === `${ALLOWED}/start`) {
        arrived = true;
        for (const text of ['LATE-1', 'LATE-2', 'LATE-3', 'LATE-4'])
          world.chrome.consoleCall(TAB, text);
      }
      return refuseForbidden(message);
    },
  });
  chrome.consoleCall(TAB, 'FIRST');
  chrome.navigation(TAB, [`${ALLOWED}/from`, `${FORBIDDEN}/bounce`, `${ALLOWED}/secret-doc`]);
  for (const text of ['FORBIDDEN-A', 'FORBIDDEN-B', 'FORBIDDEN-C'])
    chrome.consoleCall(TAB, text);
  chrome.navigation(TAB, [`${ALLOWED}/public`]);
  chrome.consoleCall(TAB, 'LAST');
  await settle();
  reading = true;
  const text = textOf(await call('browser_console_messages', { all: true }));
  assert.ok(arrived, 'the messages arrived during the check');
  assert.doesNotMatch(text, /FORBIDDEN-/);
  // Upstream counts first, then reads again: the count is the read the late messages came in the middle of.
  assert.match(text, /Total messages: 2 /);
  assert.match(text, /FIRST/);
  assert.match(text, /LAST/);
  assert.match(text, /LATE-4/);
});

test('history: requests are judged by their own chain and the document that made them; the rest stays readable', async () => {
  const { chrome, call, settle, core } = await connected();
  const text = 'text/plain';
  const fetched = (url, body) => {
    const { requestId } = chrome.requestChain(TAB, [url]);
    chrome.respond(TAB, requestId, url, { mimeType: text, headers: { 'content-type': text }, body, type: 'Fetch' });
  };
  fetched(`${ALLOWED}/early`, 'ALLOWED-EARLY-BODY');
  // A document reached through a forbidden hop, and what it fetched from an allowed address.
  chrome.navigation(TAB, [`${ALLOWED}/from`, `${FORBIDDEN}/bounce`, `${ALLOWED}/secret-doc`]);
  fetched(`${ALLOWED}/made-by-secret-doc`, 'MADE-BY-FORBIDDEN-DOCUMENT');
  // Moved on: the document request of the page now shown is its own, whatever preceded it.
  chrome.navigation(TAB, [`${ALLOWED}/public`], { body: 'PUBLIC-DOCUMENT-BODY' });
  // A subresource that went through a forbidden hop on an allowed page.
  const via = chrome.requestChain(TAB, [`${ALLOWED}/redirector`, `${FORBIDDEN}/hop`, `${ALLOWED}/after-hop`]);
  chrome.respond(TAB, via.requestId, `${ALLOWED}/after-hop`, { mimeType: text, headers: { 'content-type': text }, body: 'VIA-FORBIDDEN-HOP', type: 'Fetch' });
  fetched(`${ALLOWED}/late`, 'ALLOWED-LATE-BODY');
  await settle();
  const list = textOf(await call('browser_network_requests', { all: true, static: true }));
  const lines = Object.fromEntries(list.split('\n').filter((line) => /^\d+\. /.test(line)).map((line) => [new URL(line.split(' ')[2]).pathname, Number(line.split('.')[0])]));
  for (const gone of ['/made-by-secret-doc', '/hop', '/after-hop', '/bounce', '/secret-doc'])
    assert.ok(!(gone in lines), `${gone} is listed: ${list}`);
  for (const kept of ['/early', '/public', '/redirector', '/late'])
    assert.ok(kept in lines, `${kept} is missing: ${list}`);
  assert.match(list, /Withheld \d+ records here: this task may not read https:\/\/forbidden\.example\//);
  assert.equal(textOf(await call('browser_network_request', { index: lines['/early'], part: 'response-body' })), '### Result\nALLOWED-EARLY-BODY');
  const docBody = await call('browser_network_request', { index: lines['/public'], part: 'response-body' });
  assert.match(textOf(docBody), /PUBLIC-DOCUMENT-BODY/);
  // Every record upstream kept, including those the list above no longer shows.
  const records = await core.connection.backend()._context.currentTab().requests(true);
  const refusedRecords = records.filter(({ request }) => /\/(made-by-secret-doc|after-hop|hop|bounce|secret-doc)$/.test(request.url()));
  assert.equal(refusedRecords.length, 5);
  for (const { index, request } of refusedRecords) {
    for (const args of [{ index, part: 'response-body' }, { index }]) {
      const result = await call('browser_network_request', args);
      assert.equal(result.isError, true, `${request.url()} ${JSON.stringify(args)}`);
      assert.doesNotMatch(textOf(result), /MADE-BY-FORBIDDEN-DOCUMENT|VIA-FORBIDDEN-HOP/);
      assert.match(textOf(result), /REFUSED_BY_HOST.*\nThis request's record is withheld: this task may not read https:\/\/forbidden\.example\//);
    }
  }
});

test('history: a request record is read from the tab it was checked in, even if another tab becomes current while the host decides', async () => {
  const second = { id: 8, url: `${ALLOWED}/second`, title: 'Second', windowId: 1, index: 1 };
  const { chrome, call, core, settle } = await connected({ tabs: [{ id: TAB, url: `${ALLOWED}/start`, title: 'Start', windowId: 1, index: 0, active: true }, second] });
  const context = core.connection.backend()._context;
  const routed = (id) => chrome.commands.some((command) => command.params?.[1] === 'Fetch.enable' && command.params?.[0]?.tabId === id);
  await waitFor(() => context.tabs().length === 2 && routed(TAB) && routed(second.id), 'both tabs, routed');
  const fetched = (tabId, hops, body) => {
    const { requestId } = chrome.requestChain(tabId, hops);
    chrome.respond(tabId, requestId, hops.at(-1), { mimeType: 'text/plain', headers: { 'content-type': 'text/plain' }, body, type: 'Fetch' });
  };
  // Record 3 of the first tab is allowed. Record 3 of the second is the end of a redirect through a refused hop.
  for (const n of [1, 2, 3])
    fetched(TAB, [`${ALLOWED}/a-${n}`], `A-BODY-${n}`);
  fetched(second.id, [`${ALLOWED}/redirector`, `${FORBIDDEN}/hop`, `${ALLOWED}/after-hop`], 'B-VIA-FORBIDDEN-HOP');
  await settle();
  const read = async () => textOf(await call('browser_network_request', { index: 3, part: 'response-body' }));
  assert.match(await read(), /^### Result\nA-BODY-3(\n|$)/, 'the allowed record is read as before');
  // The host's answer about the first tab's record is on its way when someone makes the other tab current.
  let switching = true;
  const decideTarget = core.guard.decideTarget.bind(core.guard);
  core.guard.decideTarget = async (url) => {
    const block = await decideTarget(url);
    if (switching && url === `${ALLOWED}/a-3`) {
      switching = false;
      await context.selectTab(1);
    }
    return block;
  };
  const raced = await read();
  assert.equal(switching, false, 'the host was asked about the record');
  assert.equal(context.currentTab().page.url(), second.url, 'the other tab became current meanwhile');
  assert.doesNotMatch(raced, /B-VIA-FORBIDDEN-HOP/, 'the record of the other tab, which came through a refused hop, is not returned');
  assert.match(raced, /^### Result\nA-BODY-3(\n|$)/, 'the record that was checked is the one returned');
});

// A second tab in the session, which upstream makes current when the first one closes.
async function withNeighbour(url, options = {}) {
  const world = await connected({ ...options, tabs: [{ id: TAB, url: `${ALLOWED}/start`, title: 'Start', windowId: 1, index: 0, active: true }, { id: 8, url, title: 'NEIGHBOUR', windowId: 1, index: 1 }] });
  const routed = (id) => world.chrome.commands.some((command) => command.params?.[1] === 'Fetch.enable' && command.params?.[0]?.tabId === id);
  await waitFor(() => world.core.connection.backend()._context.tabs().length === 2 && routed(TAB) && routed(8), 'both tabs, routed');
  return world;
}

// Someone closes a tab. When this returns, upstream has heard of it: it left the list, and a neighbour is current.
async function closeTab({ chrome, core }, id) {
  const tabs = () => core.connection.backend()._context.tabs();
  const before = tabs().length;
  chrome.removeTab(id);
  await waitFor(() => tabs().length === before - 1, 'the tab to leave upstream\'s list');
}

// `meanwhile` happens while the host is answering this question, once: its answer arrives only after that.
function whileAsked(guard, method, question, meanwhile) {
  const ask = guard[method].bind(guard);
  let pending = true;
  guard[method] = async (url) => {
    const block = await ask(url);
    if (pending && question(url)) {
      pending = false;
      await meanwhile();
    }
    return block;
  };
}

// The tabs that received any of these CDP commands since a mark.
const reachedSince = ({ chrome }, mark, ...methods) => [...new Set(chrome.commands.slice(mark).filter((command) => methods.includes(command.params?.[1])).map((command) => command.params[0].tabId))];
const KEY = 'Input.dispatchKeyEvent';
const SCRIPT = ['Runtime.evaluate', 'Runtime.callFunctionOn'];

// The agent's new tabs land, through a redirect, on a page the task may not read.
const newTabsLandRefused = (title) => ({ chrome }, method, params) => {
  const id = params?.[0]?.tabId;
  if (method !== 'chrome.debugger.attach' || id < 900)
    return;
  chrome.onNextNavigate(id, ({ url, loaderId }) => {
    chrome.tab(id).title = title;
    chrome.navigation(id, [url, `${FORBIDDEN}/landing`], { loaderId });
  });
};

test('a page tool acts in the tab it was judged for: if that tab closes while the host answers, the call stops there', async () => {
  const isStart = (url) => url === `${ALLOWED}/start`;
  const stopped = (result, what) => {
    assert.equal(result.isError, true, what);
    assert.match(textOf(result), /^### Error\nBROWSER_TAB_CHANGED: .*Nothing was done/, what);
  };
  // The control: nothing closes, and the key reaches the tab that was judged.
  const calm = await withNeighbour(`${ALLOWED}/neighbour`);
  let mark = calm.chrome.commands.length;
  const pressed = await calm.call('browser_press_key', { key: 'a' });
  assert.equal(pressed.isError, undefined, textOf(pressed));
  assert.deepEqual(reachedSince(calm, mark, KEY), [TAB]);

  // A key, while the host answers about the page. The neighbour is one nobody asked about, and the host would refuse it.
  const framedForKey = [];
  const closedForKey = await withNeighbour(`${FORBIDDEN}/private`, { prepare: async (page) => framedForKey.push(page.url()) });
  whileAsked(closedForKey.core.guard, 'decideTarget', isStart, () => closeTab(closedForKey, TAB));
  mark = closedForKey.chrome.commands.length;
  stopped(await closedForKey.call('browser_press_key', { key: 'a' }), 'a key');
  assert.deepEqual(reachedSince(closedForKey, mark, KEY), [], 'the key reached no tab');
  assert.deepEqual(framedForKey, [`${ALLOWED}/start`], 'no frame of the neighbour was captured');
  // The next call is judged for the tab current now, as any call is.
  assert.match(textOf(await closedForKey.call('browser_press_key', { key: 'a' })), /Nothing was done: this task may not read https:\/\/forbidden\.example\/private/);
  assert.deepEqual(reachedSince(closedForKey, mark, KEY), []);

  // A script, while the host answers about running it there.
  const closedForScript = await withNeighbour(`${FORBIDDEN}/private`);
  whileAsked(closedForScript.core.guard, 'checkEvaluate', isStart, () => closeTab(closedForScript, TAB));
  mark = closedForScript.chrome.commands.length;
  stopped(await closedForScript.call('browser_evaluate', { function: '() => document.title' }), 'a script');
  assert.deepEqual(reachedSince(closedForScript, mark, ...SCRIPT), [], 'the script ran in no tab');

  // A key, during the frame captured before input, which can take seconds. A closed tab gives no frame, and that failure's
  // advice is to retry, which would act in the neighbour: the agent is told what really happened.
  const framed = [];
  const closedForFrame = await withNeighbour(`${ALLOWED}/neighbour`, {
    prepare: async (page, world) => {
      framed.push(page.url());
      if (framed.length === 1) {
        await closeTab(world, TAB);
        throw new Error('BROWSER_INPUT_NOT_READY: no frame');
      }
    },
  });
  mark = closedForFrame.chrome.commands.length;
  stopped(await closedForFrame.call('browser_press_key', { key: 'a' }), 'a key after its frame');
  assert.deepEqual(reachedSince(closedForFrame, mark, KEY), []);
  assert.deepEqual(framed, [`${ALLOWED}/start`]);
  // A neighbour the host allows: the next call acts there. The tool is not lost, and nothing is locked.
  const next = await closedForFrame.call('browser_press_key', { key: 'a' });
  assert.equal(next.isError, undefined, textOf(next));
  assert.deepEqual(reachedSince(closedForFrame, mark, KEY), [8]);
  // The control: a frame that fails with the tab still there is still that failure, and no key goes out.
  const noFrame = await withNeighbour(`${ALLOWED}/neighbour`, { prepare: async () => { throw new Error('BROWSER_INPUT_NOT_READY: no frame'); } });
  mark = noFrame.chrome.commands.length;
  assert.match(textOf(await noFrame.call('browser_press_key', { key: 'a' })), /^### Error\nBROWSER_INPUT_NOT_READY: no frame/);
  assert.deepEqual(reachedSince(noFrame, mark, KEY), []);

  // A read, even though the check after the call would withhold it: nothing is read from the neighbour at all.
  const closedForRead = await withNeighbour(`${FORBIDDEN}/private`);
  whileAsked(closedForRead.core.guard, 'decideTarget', isStart, () => closeTab(closedForRead, TAB));
  mark = closedForRead.chrome.commands.length;
  stopped(await closedForRead.call('browser_snapshot'), 'a read');
  assert.deepEqual(reachedSince(closedForRead, mark, ...SCRIPT), []);

  // The same stop for another call that selects a tab meanwhile. Only a client without the host's one-call-at-a-time holder can do this.
  const raced = await withNeighbour(`${FORBIDDEN}/private`);
  whileAsked(raced.core.guard, 'decideTarget', isStart, () => raced.call('browser_tabs', { action: 'select', index: 1 }));
  mark = raced.chrome.commands.length;
  stopped(await raced.call('browser_press_key', { key: 'a' }), 'a concurrent select');
  assert.deepEqual(reachedSince(raced, mark, KEY), []);
});

test('navigation stays in its chosen tab, while leaving a refused page and explicit tab management remain available', async () => {
  // Permission to leave a page does not retarget an already requested navigation when that tab closes.
  const racing = await withNeighbour(`${ALLOWED}/neighbour`);
  whileAsked(racing.core.guard, 'decideTarget', (url) => url === `${ALLOWED}/next`, () => closeTab(racing, TAB));
  racing.chrome.onNextNavigate(8, ({ url, loaderId }) => racing.chrome.navigation(8, [url], { loaderId }));
  let mark = racing.chrome.commands.length;
  const moved = await racing.call('browser_navigate', { url: `${ALLOWED}/next` });
  assert.equal(moved.isError, true, textOf(moved));
  assert.match(textOf(moved), /BROWSER_TAB_CHANGED/);
  assert.deepEqual(reachedSince(racing, mark, 'Page.navigate'), []);
  // A stable tab on a refused page can still navigate away; its old address is not a reason to trap it.
  const escaping = await connected({ tabs: [{ id: TAB, url: `${FORBIDDEN}/old`, title: 'Old', windowId: 1, index: 0, active: true }] });
  escaping.chrome.onNextNavigate(TAB, ({ url, loaderId }) => escaping.chrome.navigation(TAB, [url], { loaderId }));
  mark = escaping.chrome.commands.length;
  const escaped = await escaping.call('browser_navigate', { url: `${ALLOWED}/escaped` });
  assert.equal(escaped.isError, undefined, textOf(escaped));
  assert.deepEqual(reachedSince(escaping, mark, 'Page.navigate'), [TAB]);
  // A page tool acts where the agent went.
  const { chrome, call } = await withNeighbour(`${ALLOWED}/neighbour`);
  await call('browser_tabs', { action: 'select', index: 1 });
  mark = chrome.commands.length;
  assert.equal((await call('browser_press_key', { key: 'a' })).isError, undefined);
  assert.deepEqual(reachedSince({ chrome }, mark, KEY), [8]);
  await call('browser_tabs', { action: 'new' });
  const created = chrome.commands.filter((command) => command.method === 'chrome.debugger.attach').at(-1).params[0].tabId;
  mark = chrome.commands.length;
  assert.equal((await call('browser_press_key', { key: 'b' })).isError, undefined);
  assert.deepEqual(reachedSince({ chrome }, mark, KEY), [created]);
  // Closing its own tab on a page the task may not read is the way out the refusal itself offers: that result is returned.
  const leaving = await withNeighbour(`${ALLOWED}/neighbour`, { onCommand: newTabsLandRefused('LEFT-TITLE') });
  assert.equal((await leaving.call('browser_tabs', { action: 'new', url: `${ALLOWED}/go` })).isError, true, 'withheld while it is current');
  const closed = await leaving.call('browser_tabs', { action: 'close', index: 2 });
  assert.equal(closed.isError, undefined, textOf(closed));
  assert.doesNotMatch(textOf(closed), /LEFT-TITLE/);
});

test('a result is judged with the tabs it came from, also those that closed before it was returned', async () => {
  // A listed tab the task may not read closes once its title is in the result, while the host answers about the current page.
  const listing = await withNeighbour(`${FORBIDDEN}/private`);
  whileAsked(listing.core.guard, 'decideTarget', (url) => url === `${ALLOWED}/start`, () => closeTab(listing, 8));
  const listed = await listing.call('browser_tabs', { action: 'list' });
  assert.equal(listing.core.connection.backend()._context.tabs().length, 1, 'it closed during the call');
  assert.equal(listed.isError, true);
  assert.doesNotMatch(textOf(listed), /NEIGHBOUR/);
  assert.match(textOf(listed), /this task may not read https:\/\/forbidden\.example\/private/);
  // The control: one the task may read is returned as usual, closed or not.
  const allowed = await withNeighbour(`${ALLOWED}/neighbour`);
  whileAsked(allowed.core.guard, 'decideTarget', (url) => url === `${ALLOWED}/start`, () => closeTab(allowed, 8));
  const fine = await allowed.call('browser_tabs', { action: 'list' });
  assert.equal(fine.isError, undefined, textOf(fine));
  assert.match(textOf(fine), /NEIGHBOUR/);

  // The former neighbour becomes current while the host judges A: its rendered title still needs its own check.
  for (const [url, refused] of [[`${FORBIDDEN}/private`, true], [`${ALLOWED}/neighbour`, false]]) {
    const shifted = await withNeighbour(url);
    whileAsked(shifted.core.guard, 'decideTarget', (asked) => asked === `${ALLOWED}/start`, () => closeTab(shifted, TAB));
    const result = await shifted.call('browser_tabs', { action: 'list' });
    assert.equal(shifted.core.connection.backend()._context.tabs().length, 1, 'A closed while its post-check awaited the host');
    assert.equal(result.isError, refused ? true : undefined, textOf(result));
    if (refused) assert.doesNotMatch(textOf(result), /NEIGHBOUR/);
    else assert.match(textOf(result), /NEIGHBOUR/);
  }

  // Closed before the call, it has no part in it, even if another tab now has the same title.
  const earlier = await withNeighbour(`${FORBIDDEN}/private`);
  assert.equal((await earlier.call('browser_tabs', { action: 'list' })).isError, true, 'listed while it was open');
  await closeTab(earlier, 8);
  earlier.chrome.tab(TAB).title = 'NEIGHBOUR';
  const later = await earlier.call('browser_tabs', { action: 'list' });
  assert.equal(later.isError, undefined, textOf(later));
  assert.match(textOf(later), /NEIGHBOUR/);

  // One both opened and closed within the call: the agent's new tab lands on such a page and closes once upstream has listed it.
  const fleeting = await withNeighbour(`${ALLOWED}/neighbour`, { onCommand: newTabsLandRefused('FLEETING-TITLE') });
  const { Tab } = runtime.bundle.tools;
  const headerOfTab = Tab.prototype.headerSnapshot;
  Tab.prototype.headerSnapshot = async function() {
    const header = await headerOfTab.call(this);
    if (this.page.url() === `${FORBIDDEN}/landing`)
      await closeTab(fleeting, fleeting.chrome.commands.filter((command) => command.method === 'chrome.debugger.attach').at(-1).params[0].tabId);
    return header;
  };
  let opened;
  try {
    opened = await fleeting.call('browser_tabs', { action: 'new', url: `${ALLOWED}/go` });
  } finally {
    Tab.prototype.headerSnapshot = headerOfTab;
  }
  assert.equal(fleeting.core.connection.backend()._context.tabs().length, 2, 'it closed during the call');
  assert.equal(opened.isError, true, textOf(opened));
  assert.doesNotMatch(textOf(opened), /FLEETING-TITLE/);
  assert.match(textOf(opened), /this task may not read https:\/\/forbidden\.example\/landing/);

  // The tab a read worked in: its document changes while the host answers, and it closes once upstream has read it. Upstream reads
  // the tabs' headers last and in order (Response._build), so this tab's content and header are in hand when the other header starts.
  const world = await withNeighbour(`${ALLOWED}/neighbour`);
  const [worked, other] = world.core.connection.backend()._context.tabs();
  whileAsked(world.core.guard, 'decideTarget', (url) => url === `${ALLOWED}/start`, async () => {
    world.chrome.tab(TAB).title = '';
    world.chrome.navigation(TAB, [`${ALLOWED}/from`, `${FORBIDDEN}/bounce`, `${ALLOWED}/landing`]);
    await world.settle();
  });
  const workedHeader = worked.headerSnapshot.bind(worked);
  const otherHeader = other.headerSnapshot.bind(other);
  let headerRead;
  const read = new Promise((resolve) => {
    headerRead = resolve;
  });
  worked.headerSnapshot = async () => {
    const header = await workedHeader();
    headerRead();
    return header;
  };
  other.headerSnapshot = async () => {
    await read;
    await closeTab(world, TAB);
    return await otherHeader();
  };
  const snapshot = await world.call('browser_snapshot');
  assert.equal(world.core.connection.backend()._context.currentTab(), other, 'another tab is current when the check runs');
  assert.equal(snapshot.isError, true, textOf(snapshot));
  assert.doesNotMatch(textOf(snapshot), /page content/);
  assert.match(textOf(snapshot), /The call ran, but its output is withheld: this task may not read https:\/\/forbidden\.example\/bounce/);
});

test('a navigation result is judged by its destination even when that tab closes before the result returns', async () => {
  const world = await withNeighbour(`${ALLOWED}/neighbour`);
  const [worked, other] = world.core.connection.backend()._context.tabs();
  world.chrome.onNextNavigate(TAB, ({ url, loaderId }) => {
    world.chrome.tab(TAB).title = '';
    world.chrome.navigation(TAB, [url, `${FORBIDDEN}/bounce`, `${ALLOWED}/landing`], { loaderId });
  });
  const header = worked.headerSnapshot.bind(worked);
  const neighbourHeader = other.headerSnapshot.bind(other);
  let destinationRead;
  const read = new Promise((resolve) => { destinationRead = resolve; });
  worked.headerSnapshot = async () => {
    const result = await header();
    destinationRead();
    return result;
  };
  other.headerSnapshot = async () => {
    await read;
    await closeTab(world, TAB);
    return await neighbourHeader();
  };
  const result = await world.call('browser_navigate', { url: `${ALLOWED}/next` });
  assert.equal(world.core.connection.backend()._context.currentTab(), other);
  assert.equal(result.isError, true, textOf(result));
  assert.match(textOf(result), /The call ran, but its output is withheld: this task may not read https:\/\/forbidden\.example\/bounce/);
});

// What the connection keeps of a page's documents. A scripted history is what Chrome reports, in its order: each
// document commits, then makes its records, named for it ("d7#2" is the second record of the document at /d7).
// `via` puts a refused address among a document's redirect hops.
function play(chrome, documents) {
  for (const { name, via, console: logs = 0, errors = 0 } of documents) {
    chrome.navigation(TAB, via ? [`${ALLOWED}/from`, `${FORBIDDEN}/${via}`, `${ALLOWED}/${name}`] : [`${ALLOWED}/${name}`]);
    for (let n = 1; n <= logs; n += 1)
      chrome.consoleCall(TAB, `${name}#${n}`);
    for (let n = 1; n <= errors; n += 1)
      chrome.pageError(TAB, `${name}#${n}`);
  }
}
const series = (from, to, records = {}) => Array.from({ length: to - from + 1 }, (_, index) => ({ name: `d${from + index}`, ...records }));
const namesOf = (documents) => documents.map(({ name }) => name);
const list = (...names) => names.flat().join(' ');
const shortName = (url) => url.slice(ALLOWED.length + 1);

// The documents the connection keeps for the page, and what Playwright still holds with the document each record is
// attributed to, asked as the server asks (by its place from the newest). However much was forgotten, every held record
// keeps the document that made it, and the document shown is kept.
async function bookkeeping({ core, page, settle }) {
  await settle();
  const state = core.connection._provenance(page());
  const held = {
    console: (await page().consoleMessages({ filter: 'all' })).map((message) => message.text()),
    errors: (await page().pageErrors({ filter: 'all' })).map((error) => error.message),
  };
  for (const [kind, records] of Object.entries(held)) {
    const attributed = records.map((_, index) => shortName(core.connection.recordDocument(page(), kind, records.length - 1 - index).own[0]));
    assert.equal(list(attributed), list(records.map((record) => record.split('#')[0])), `${kind}: each held record is attributed to the document that made it`);
  }
  assert.equal(state.documents.at(-1), state.document, 'the document shown is kept');
  return { documents: state.documents.map((document) => shortName(document.own[0])), held };
}

test('history: a document leaves once Playwright holds none of its records, whichever kind they were', async () => {
  // One record of one kind per navigation, 300 navigations: Playwright holds the last 200. Their documents stay, and the
  // first (it answers for records from before) and the one shown; the other kind never moves.
  for (const kind of ['console', 'errors']) {
    const world = await connected();
    play(world.chrome, [...series(1, 300, { [kind]: 1 }), { name: 'end' }]);
    const { documents, held } = await bookkeeping(world);
    assert.equal(held[kind].length, 200, `Playwright holds 200 ${kind} records`);
    assert.equal(list(documents), list('start', namesOf(series(101, 300)), 'end'), `${kind} only`);
  }
  // No records at all.
  const quiet = await connected();
  play(quiet.chrome, series(1, 30));
  assert.equal(list((await bookkeeping(quiet)).documents), 'start d30');
  // Both kinds at the same pace: the records from before are past as well, so the first goes too.
  const both = await connected();
  play(both.chrome, [...series(1, 300, { console: 1, errors: 1 }), { name: 'end' }]);
  assert.equal(list((await bookkeeping(both)).documents), list(namesOf(series(101, 300)), 'end'));
});

test('history: a document is kept while Playwright can still hold its record: 200 records, and not one more', async () => {
  const world = await connected();
  // `a` made the oldest record. With 199 after it Playwright holds all 200; with one more it drops the oldest (twenty at
  // a time, so it may hold fewer than 200, never more).
  play(world.chrome, [{ name: 'a', console: 1 }, { name: 'f', console: 199 }, { name: 'g' }]);
  let account = await bookkeeping(world);
  assert.equal(account.held.console[0], 'a#1', 'Playwright holds the oldest of 200');
  assert.equal(list(account.documents), 'start a f g');
  world.chrome.consoleCall(TAB, 'g#1');
  play(world.chrome, [{ name: 'h' }]);
  account = await bookkeeping(world);
  assert.ok(!account.held.console.includes('a#1'), 'Playwright dropped the oldest of 201');
  assert.equal(list(account.documents), 'start f g h');
});

test('history: a document stays while either kind of its records is held, however far the other kind has run on', async () => {
  const world = await connected();
  const { chrome, call } = world;
  // f1 made one console message and f2 one page error, each through a refused hop. Then 250 page errors and nothing else:
  // f2's error is no longer held but f1's message is, and a quiet kind keeps its own documents, not those of the other.
  play(chrome, [{ name: 'f1', via: 'bounce1', console: 1 }, { name: 'f2', via: 'bounce2', errors: 1 }, ...series(1, 10, { errors: 25 }), { name: 'x1' }]);
  let account = await bookkeeping(world);
  assert.equal(list(account.documents), list('start', 'f1', namesOf(series(3, 10)), 'x1'));
  let read = textOf(await call('browser_console_messages', { all: true }));
  assert.doesNotMatch(read, /f1#1/, 'the message of the document reached through a refused hop stays withheld');
  assert.match(read, /Withheld 1 record here: this task may not read https:\/\/forbidden\.example\/bounce1/);
  assert.match(read, new RegExp(`Total messages: ${account.held.errors.length} `), 'every other record held is returned');
  // Now 250 console messages: f1's is not held either, and the page errors, quiet from here, keep their documents.
  play(chrome, [...series(11, 20, { console: 25 }), { name: 'x2' }]);
  account = await bookkeeping(world);
  assert.equal(list(account.documents), list(namesOf(series(3, 10)), namesOf(series(13, 20)), 'x2'));
  read = textOf(await call('browser_console_messages', { all: true }));
  assert.doesNotMatch(read, /Withheld/);
  assert.match(read, new RegExp(`Total messages: ${account.held.console.length + account.held.errors.length} `), 'nothing allowed was dropped');
});

test('history: the first document answers for records from before the connection counted, until both kinds are past them', async () => {
  const world = await connected();
  const { core, chrome, page } = world;
  // The stand-in cannot make Playwright hold a record this connection never counted, so one is asked about as the server
  // would ask: its place from the newest is beyond everything counted.
  const fromBefore = (kind, fromNewest) => shortName(core.connection.recordDocument(page(), kind, fromNewest).own[0]);
  // 210 console messages and no page error: the console records from before are past, a page error from before is not.
  play(chrome, [{ name: 'a', console: 210 }, { name: 'b' }]);
  let account = await bookkeeping(world);
  assert.equal(list(account.documents), 'start a b');
  assert.equal(fromBefore('errors', 0), 'start');
  // 199 page errors counted: the newest one from before could still be held (it and the 199 make 200). One more, and none.
  play(chrome, [{ name: 'c', errors: 199 }, { name: 'd' }]);
  account = await bookkeeping(world);
  assert.equal(list(account.documents), 'start a c d');
  assert.equal(fromBefore('errors', 199), 'start');
  chrome.pageError(TAB, 'd#1');
  play(chrome, [{ name: 'e' }]);
  account = await bookkeeping(world);
  assert.equal(list(account.documents), 'a c d e');
});

test('history: a request from before the connection was watching stays the first document\'s, also after that document is forgotten', async () => {
  // The page makes it as Chrome starts reporting the tab's network, while Playwright is still connecting: Playwright keeps it,
  // and this connection, which starts watching once Playwright has connected, never hears of it.
  const seenBeforeWatching = async (first, url, body) => {
    let made = false;
    const world = await connected({
      tabs: [{ id: TAB, url: first, title: 'First', windowId: 1, index: 0, active: true }],
      onCommand: ({ chrome }, method, params) => {
        if (made || method !== 'chrome.debugger.sendCommand' || params?.[1] !== 'Network.enable')
          return;
        made = true;
        const { requestId } = chrome.requestChain(TAB, [url]);
        chrome.respond(TAB, requestId, url, { mimeType: 'text/plain', headers: { 'content-type': 'text/plain' }, body, type: 'Fetch' });
      },
    });
    await world.settle();
    const entry = (await world.core.connection.backend()._context.currentTab().requests(true)).find(({ request }) => request.url() === url);
    assert.ok(entry, 'Playwright keeps it');
    assert.equal(world.core.connection._requests.has(entry.request), false, 'the connection did not see it made');
    return { world, index: entry.index };
  };
  const records = async ({ call }, index) => ({
    list: textOf(await call('browser_network_requests', { all: true, static: true })),
    one: await call('browser_network_request', { index, part: 'response-body' }),
  });

  // The first document may not be read. What it fetched from an allowed address stays withheld when later documents, which may be
  // read, have taken its place: 200 of both kinds of record retire a document, and say nothing of the requests Playwright keeps.
  const made = `${ALLOWED}/made-by-first-document`;
  const forbidden = await seenBeforeWatching(`${FORBIDDEN}/first`, made, 'BODY-OF-THE-FORBIDDEN-DOCUMENT');
  for (const history of [[{ name: 'second' }], [{ name: 'third', console: 200, errors: 200 }, { name: 'fourth' }]]) {
    play(forbidden.world.chrome, history);
    await forbidden.world.settle();
    const { list: listed, one } = await records(forbidden.world, forbidden.index);
    assert.ok(!listed.includes(made), `${history[0].name}: ${listed}`);
    assert.match(listed, /Withheld 1 record here: this task may not read https:\/\/forbidden\.example\/first/);
    assert.equal(one.isError, true, history[0].name);
    assert.doesNotMatch(textOf(one), /BODY-OF-THE-FORBIDDEN-DOCUMENT/);
  }
  assert.equal(list((await bookkeeping(forbidden.world)).documents), 'third fourth', 'the first document was forgotten');

  // The first document may be read, and the one after it may not. It keeps its allowed history, with no authority of the later one.
  const fetched = `${ALLOWED}/fetched-by-first-document`;
  const allowed = await seenBeforeWatching(`${ALLOWED}/first`, fetched, 'BODY-OF-THE-ALLOWED-DOCUMENT');
  play(allowed.world.chrome, [{ name: 'second', via: 'bounce', console: 200, errors: 200 }, { name: 'third' }]);
  assert.equal(list((await bookkeeping(allowed.world)).documents), 'second third', 'the first document was forgotten');
  const { list: listed, one } = await records(allowed.world, allowed.index);
  assert.ok(listed.includes(fetched), listed);
  assert.equal(one.isError, undefined, textOf(one));
  assert.match(textOf(one), /BODY-OF-THE-ALLOWED-DOCUMENT/);
});

test('decisions: one question per address in a call, asked again by the next call, so a changed answer counts', async () => {
  let refuse = false;
  const { chrome, call, settle, questions } = await connected({
    decide: (message) => {
      if ('armed' in message)
        return { armed: true };
      return { block: refuse && message.url === `${ALLOWED}/start` ? 'REFUSED_NOW: policy changed' : '' };
    },
  });
  chrome.consoleCall(TAB, 'FIRST-DOC');
  chrome.navigation(TAB, [`${ALLOWED}/second`]);
  await settle();
  const before = questions().length;
  const first = await call('browser_console_messages', { all: true });
  assert.match(textOf(first), /FIRST-DOC/);
  const asked = questions().slice(before);
  assert.deepEqual([...asked].sort(), [`${ALLOWED}/second`, `${ALLOWED}/start`].sort(), 'each address exactly once, across before, record and after checks');
  refuse = true;
  const second = await call('browser_console_messages', { all: true });
  assert.doesNotMatch(textOf(second), /FIRST-DOC/, 'no answer from the earlier call was kept');
  assert.match(textOf(second), /REFUSED_NOW/);
});

test('a document with too many addresses to check has its console records refused, not guessed at', async () => {
  const { chrome, call, settle, core, page } = await connected();
  chrome.consoleCall(TAB, 'BEFORE-THE-FLOOD');
  await settle();
  // The bound itself, reached through the real frame event: one chain past it is enough here.
  const state = core.connection._provenance(page());
  for (let index = 0; state.document.addresses.size < MAX_DOCUMENT_ADDRESSES; index += 1)
    state.document.addresses.add(`${ALLOWED}/filler-${index}`);
  chrome.attachFrame(TAB, 'FRAME-1');
  chrome.navigation(TAB, [`${ALLOWED}/a`, `${ALLOWED}/b`], { frameId: 'FRAME-1', parentId: `TARGET-${TAB}` });
  await settle();
  assert.equal(state.document.truncated, true);
  const result = textOf(await call('browser_console_messages'));
  assert.doesNotMatch(result, /BEFORE-THE-FLOOD/);
  assert.match(result, /BROWSER_RECORD_ORIGIN_UNKNOWN/);
});

test('bodies: an image arrives as an image, other binary as an embedded resource, text stays text, and no bare path remains', async () => {
  const { chrome, call, settle, core } = await connected();
  const bodies = [
    { path: '/picture.png', headers: { 'content-type': 'image/png' }, body: PNG },
    { path: '/mislabelled', headers: { 'content-type': 'application/octet-stream' }, body: PNG },
    { path: '/report.pdf', headers: { 'content-type': 'application/pdf; version=1' }, body: Buffer.from('%PDF-1.7 binary') },
    { path: '/not-really.png', headers: { 'content-type': 'image/png' }, body: Buffer.from('not an image') },
    { path: '/plain.txt', headers: { 'content-type': 'text/plain' }, body: Buffer.from('READABLE TEXT BODY') },
    { path: '/looks-like-a-path.txt', headers: { 'content-type': 'text/plain' }, body: Buffer.from('output/response-page-text.png') },
  ];
  for (const { path: where, headers, body } of bodies) {
    const { requestId } = chrome.requestChain(TAB, [`${ALLOWED}${where}`]);
    chrome.respond(TAB, requestId, `${ALLOWED}${where}`, { headers, mimeType: headers['content-type'].split(';')[0], body, type: 'Fetch' });
  }
  await settle();
  const results = [];
  for (let index = 1; index <= bodies.length; index += 1)
    results.push(await call('browser_network_request', { index, part: 'response-body' }));
  const [picture, mislabelled, pdf, notReally, plain, looksLike] = results;
  for (const [what, result] of [['picture', picture], ['mislabelled', mislabelled]]) {
    assert.equal(result.isError, undefined, what);
    const image = result.content.find((part) => part.type === 'image');
    assert.deepEqual([image?.mimeType, image?.data], ['image/png', PNG.toString('base64')], `${what}: exact bytes in the protocol's image part`);
    assert.match(textOf(result), /- Response body: the image is attached to this result/, what);
  }
  for (const [what, result, bytes, index, mimeType, declared] of [
    ['pdf', pdf, Buffer.from('%PDF-1.7 binary'), 3, 'application/pdf', 'application/pdf'],
    // Declared an image, but not one: never labelled an image, so a host attaching images by type is not handed it.
    ['notReally', notReally, Buffer.from('not an image'), 4, 'application/octet-stream', 'image/png'],
  ]) {
    assert.equal(result.isError, undefined, what);
    const hash = sha256(bytes);
    assert.deepEqual(result.content.map((part) => part.type), ['text', 'resource'], `${what}: the text, then the resource, last`);
    assert.deepEqual(result.content[1].resource, { uri: `chrome-browser://response-body/request-${index}/${hash}`, mimeType, blob: bytes.toString('base64') }, `${what}: exact bytes`);
    assert.match(textOf(result), new RegExp(`- Response body: ${bytes.length} bytes \\(${declared.replace('/', '\\/')}\\), attached to this result as a binary resource, sha256 ${hash}`), what);
  }
  assert.ok(picture.content.every((part) => part.type !== 'resource'), 'an image stays in the image carrier only');
  // The pinned SDK's own tools/call result check takes the resource as the protocol's BlobResourceContents.
  const sdk = new runtime.utils.Server({ name: 'check', version: '1' }, { capabilities: { tools: {} } });
  sdk.setRequestHandler(runtime.utils.CallToolRequestSchema, async () => pdf);
  assert.deepEqual(await sdk._requestHandlers.get('tools/call')({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'browser_network_request', arguments: {} } }, {}), pdf);
  for (const [what, result] of [['picture', picture], ['pdf', pdf], ['notReally', notReally]])
    assert.doesNotMatch(textOf(result), /output\/response-/, `${what}: no bare private path`);
  assert.equal(textOf(plain), '### Result\nREADABLE TEXT BODY');
  assert.equal(textOf(looksLike), '### Result\noutput/response-page-text.png', 'a text body that looks like a path is page text');
  const saved = fs.readdirSync(core.connection.outputDir());
  assert.ok(saved.some((name) => name.startsWith('response-') && name.endsWith('.png')), 'the raw originals stay where upstream put them');
});

test('saved bodies: formats by content, bounded reads, collision-safe labels, and a body too large to return is described', (t) => {
  assert.equal(imageTypeOf(PNG), 'png');
  assert.equal(imageTypeOf(Buffer.from([0xff, 0xd8, 0xff, 0xe0])), 'jpeg');
  assert.equal(imageTypeOf(Buffer.from('GIF89a..')), 'gif');
  assert.equal(imageTypeOf(Buffer.from('RIFF....WEBPVP8 ')), 'webp');
  for (const other of [Buffer.alloc(0), Buffer.from('<svg/>'), Buffer.from('%PDF'), PNG.subarray(0, 4)])
    assert.equal(imageTypeOf(other), '');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cb-body-'));
  cleanups.push(async () => fs.rmSync(dir, { recursive: true, force: true }));
  const big = path.join(dir, 'big.png');
  fs.writeFileSync(big, Buffer.concat([PNG, Buffer.alloc(MAX_RESPONSE_IMAGE_BYTES)]));
  const huge = path.join(dir, 'huge.bin');
  fs.writeFileSync(huge, Buffer.alloc(MAX_RESPONSE_BODY_BYTES + 1, 7));
  const whole = t.mock.method(fs, 'readFileSync');
  const described = describeSavedBody(big, 'image/png');
  assert.equal(described.image, undefined);
  assert.equal(described.resource, undefined, 'too large for an image is not sent another way');
  assert.match(described.text, /larger than the 20971520 bytes returned as an image/);
  const tooLarge = describeSavedBody(huge, 'application/zip');
  assert.equal(tooLarge.resource, undefined);
  assert.match(tooLarge.text, /^- Response body: 20971521 bytes \(application\/zip\), larger than the 20971520 bytes returned as a binary resource\. It is not returned here/);
  assert.equal(whole.mock.callCount(), 0, 'a body over the bound is never read whole');
  whole.mock.restore();
  // The label names the request and the bytes: different bytes or another request never share one.
  const one = path.join(dir, 'one.bin');
  const two = path.join(dir, 'two.bin');
  fs.writeFileSync(one, 'first body');
  fs.writeFileSync(two, 'second body');
  const labels = [describeSavedBody(one, '', 5), describeSavedBody(two, '', 5), describeSavedBody(one, '', 6)].map((body) => body.resource.uri);
  assert.equal(new Set(labels).size, 3, labels.join(' '));
  for (const [declared, mimeType] of [['', 'application/octet-stream'], ['Application/ZIP; charset=x', 'application/zip'], ['image/avif', 'application/octet-stream'], ['not a type', 'application/octet-stream'], ['a/b\nc', 'application/octet-stream']])
    assert.equal(describeSavedBody(one, declared, 1).resource.mimeType, mimeType, JSON.stringify(declared));
});

test('bodies: a binary resource is not returned when the call\'s output is withheld', async () => {
  const { chrome, call, settle, core } = await connected({ onCommand: ({ chrome: fake }, method, params) => {
    // While Chrome hands over the body, the tab moves on to a page the task may not read.
    if (method === 'chrome.debugger.sendCommand' && params?.[1] === 'Network.getResponseBody')
      fake.navigation(TAB, [`${FORBIDDEN}/moved`]);
  } });
  const { requestId } = chrome.requestChain(TAB, [`${ALLOWED}/archive.zip`]);
  chrome.respond(TAB, requestId, `${ALLOWED}/archive.zip`, { headers: { 'content-type': 'application/zip' }, mimeType: 'application/zip', body: Buffer.from('PK-ARCHIVE-BYTES'), type: 'Fetch' });
  await settle();
  const result = await call('browser_network_request', { index: 1, part: 'response-body' });
  assert.equal(result.isError, true);
  assert.match(textOf(result), /The call ran, but its output is withheld: this task may not read https:\/\/forbidden\.example\/moved/);
  assert.ok(result.content.every((part) => part.type === 'text'), 'no resource');
  assert.doesNotMatch(JSON.stringify(result), new RegExp(Buffer.from('PK-ARCHIVE-BYTES').toString('base64')));
  assert.ok(fs.readdirSync(core.connection.outputDir()).some((name) => name.startsWith('response-') && name.endsWith('.zip')), 'the body was read and saved: only its return was withheld');
});

// What Chrome says while Playwright sets a file input through its buffer branch: the chooser's element, its
// document's frame, and the input the retargeting finds (pinned coreBundle.js, CRPage._onFileChooserOpened,
// _getOwnerFrame and ElementHandle._setInputFiles). The page itself is not run: the test states its answers.
// With state.detached the input is gone from the page when Playwright looks for it.
const fileInputAnswers = (state) => (method, params) => {
  if (method === 'DOM.resolveNode')
    return { object: { type: 'object', subtype: 'node', className: 'HTMLInputElement', objectId: 'node-chooser' } };
  if (method === 'DOM.describeNode')
    return { node: { nodeId: 1, backendNodeId: 1, nodeType: 1, nodeName: 'HTML', localName: 'html', nodeValue: '', frameId: `TARGET-${TAB}` } };
  if (method === 'Runtime.callFunctionOn' && JSON.stringify(params).includes('!!element3.multiple'))
    return { result: { type: 'boolean', value: false } };
  if (method === 'Runtime.callFunctionOn' && JSON.stringify(params).includes('Non-multiple file input'))
    return state.detached ? { result: { type: 'undefined' } } : { result: { type: 'object', subtype: 'node', className: 'HTMLInputElement', objectId: 'node-input' } };
  return undefined;
};

async function withIntake({ admit, tamper } = {}) {
  const work = scratchDir('upload-work');
  const kept = scratchDir('upload-kept');
  fs.writeFileSync(path.join(work, 'photo.png'), PNG);
  fs.writeFileSync(path.join(work, 'secret.txt'), 'NOT FOR THE PAGE');
  const intake = retainingIntake({ dir: kept, roots: { work }, admit: admit ?? ((entry) => (entry.path.endsWith('secret.txt') ? 'READ_BLOCKED: this task may not read that file' : '')) });
  const decide = (message, world) => {
    if (message.action !== 'file_intake')
      return refuseForbidden(message);
    const answer = intake(message);
    return world.tamper && answer.files ? { ...answer, files: answer.files.map((file) => ({ ...file, sha256: sha256(Buffer.from('something else')) })) } : answer;
  };
  const input = {};
  const world = await connected({ decide, answer: fileInputAnswers(input) });
  // The same object decide is handed, so a test can change what the host answers.
  return Object.assign(world, { tamper, input, work, kept, intake, tab: () => world.core.connection.backend()._context.currentTab() });
}

const chooserOf = (tab) => tab.modalStates().find((state) => state.type === 'fileChooser');
const setInputFilesCalls = (chrome) => chrome.cdp('Runtime.callFunctionOn').filter((params) => JSON.stringify(params).includes('setInputFiles'));

test('upload: the host admits and copies each file first; a refusal leaves the chooser open, and the page gets only the copy\'s bytes and name', async () => {
  const world = await withIntake();
  const { chrome, call, settle, work, kept, intake, tab } = world;
  chrome.emit(TAB, 'Page.fileChooserOpened', { frameId: `TARGET-${TAB}`, mode: 'selectSingle', backendNodeId: 42 });
  const chooser = await waitFor(() => chooserOf(tab()), 'the chooser as a modal state');
  // Refused by the host: nothing reaches the page, and the same chooser is still there to use.
  const refused = await call('browser_file_upload', { paths: [path.join(work, 'secret.txt')] });
  assert.equal(refused.isError, true);
  assert.match(textOf(refused), /^### Error\nREAD_BLOCKED: this task may not read that file\nNo file was given to the page; the file chooser is still open\./);
  assert.equal(chooserOf(tab()), chooser);
  // A copy that is not what the host stated: the same.
  world.tamper = true;
  const tampered = await call('browser_file_upload', { paths: [path.join(work, 'photo.png')] });
  assert.match(textOf(tampered), /^### Error\nBROWSER_FILE_INTAKE_MISMATCH: the host's retained copy of file 1 does not have the sha256 the host stated/);
  assert.equal(chooserOf(tab()), chooser);
  world.tamper = false;
  const both = await call('browser_file_upload', { paths: ['a'], files: [{ path: 'b' }] });
  assert.match(textOf(both), /^### Error\nGive the files either as "paths" or as "files", not both\. Nothing was done\./);
  assert.equal(setInputFilesCalls(chrome).length, 0, 'nothing was given to the page');
  // Admitted, but the page lost its input: Playwright's error, and the chooser is back for another try.
  world.input.detached = true;
  const detached = await call('browser_file_upload', { paths: [path.join(work, 'photo.png')] });
  assert.match(textOf(detached), /^### Error\n.*not attached/);
  assert.equal(chooserOf(tab()), chooser, 'restored as upstream restores it');
  world.input.detached = false;
  // Admitted: the root goes to the host as named, the copy's bytes and the original name to the page.
  const given = await call('browser_file_upload', { files: [{ path: 'photo.png', root: 'work' }] });
  assert.equal(given.isError, undefined, textOf(given));
  assert.equal(sectionOf(given, 'Ran Playwright code'), '```js\nawait fileChooser.setFiles([{ name: "photo.png", buffer: hostCopies[0] }])\n```');
  assert.equal(sectionOf(given, 'Result'), `- Gave the page "photo.png": ${PNG.length} bytes, sha256 ${sha256(PNG)}`);
  assert.equal(chooserOf(tab()), undefined, 'the chooser was used');
  assert.deepEqual(intake.asked.map((message) => message.files), [
    [{ path: path.join(work, 'secret.txt') }], [{ path: path.join(work, 'photo.png') }], [{ path: path.join(work, 'photo.png') }], [{ path: 'photo.png', root: 'work' }],
  ], 'paths are asked as { path }, files as named; nothing out of form reached the host');
  const [sent] = setInputFilesCalls(chrome);
  const wire = JSON.stringify(sent);
  assert.ok(wire.includes(PNG.toString('base64')) && wire.includes('"photo.png"') && wire.includes('image/png'), 'Playwright\'s buffer branch: name, type and bytes');
  assert.equal(setInputFilesCalls(chrome).length, 1);
  assert.equal(chrome.cdp('DOM.setFileInputFiles').length, 0, 'never the path branch, which hands Chrome a local path');
  const everything = JSON.stringify(chrome.commands) + JSON.stringify([refused, tampered, detached, given]);
  for (const where of [work, kept])
    assert.ok(!everything.includes(where), `no path under ${where} reaches Chrome or a result`);
  assert.doesNotMatch(everything, /NOT FOR THE PAGE|Tk9UIEZPUiBUSEUgUEFHRQ/);
  await settle();
});

test('upload: an omitted selection cancels as upstream does, and nothing without a chooser asks the host', async () => {
  const { chrome, call, intake, tab } = await withIntake();
  const none = await call('browser_file_upload', { paths: ['photo.png'] });
  assert.match(textOf(none), /^### Error\nError: The tool "browser_file_upload" can only be used when there is related modal state present\./);
  chrome.emit(TAB, 'Page.fileChooserOpened', { frameId: `TARGET-${TAB}`, mode: 'selectSingle', backendNodeId: 42 });
  await waitFor(() => chooserOf(tab()), 'the chooser as a modal state');
  const cancelled = await call('browser_file_upload', {});
  assert.equal(cancelled.isError, undefined, textOf(cancelled));
  assert.equal(sectionOf(cancelled, 'Ran Playwright code'), '```js\nawait fileChooser.setFiles(undefined)\n```');
  assert.equal(chooserOf(tab()), undefined);
  assert.deepEqual(intake.asked, [], 'the host was never asked');
  assert.equal(setInputFilesCalls(chrome).length, 0);
});

test('drop: files are the host\'s copies as { name, buffer }, a refusal drops nothing, and a data-only drop is upstream\'s', async () => {
  const { call, work, intake, tab } = await withIntake();
  // The element and Playwright's pointer path below Locator.drop are not driven here (no page runs); the
  // adapter's payload to it is recorded instead. Its file conversion is the one the upload test exercises.
  const dropped = [];
  tab().targetLocator = async (params) => ({ locator: { drop: async (payload, options) => dropped.push({ payload, options, target: params.target }) }, resolved: "locator('#drop')" });
  const refused = await call('browser_drop', { target: '#drop', paths: [path.join(work, 'secret.txt')] });
  assert.match(textOf(refused), /^### Error\nREAD_BLOCKED: this task may not read that file\nNothing was dropped\./);
  assert.equal(dropped.length, 0);
  const given = await call('browser_drop', { target: '#drop', paths: [path.join(work, 'photo.png')], data: { 'text/plain': 'caption' } });
  assert.equal(given.isError, undefined, textOf(given));
  assert.deepEqual(dropped[0].payload, { files: [{ name: 'photo.png', buffer: PNG }], data: { 'text/plain': 'caption' } });
  assert.ok(Buffer.isBuffer(dropped[0].payload.files[0].buffer));
  assert.equal(sectionOf(given, 'Ran Playwright code'), '```js\nawait page.locator(\'#drop\').drop({ files: [{ name: "photo.png", buffer: hostCopies[0] }], data: {"text/plain":"caption"} });\n```');
  assert.equal(sectionOf(given, 'Result'), `- Gave the page "photo.png": ${PNG.length} bytes, sha256 ${sha256(PNG)}`);
  const asked = intake.asked.length;
  const dataOnly = await call('browser_drop', { target: '#drop', data: { 'text/plain': 'only data' } });
  assert.equal(dataOnly.isError, undefined, textOf(dataOnly));
  assert.deepEqual(dropped[1].payload, { data: { 'text/plain': 'only data' } }, 'upstream\'s own payload');
  assert.equal(intake.asked.length, asked, 'a data-only drop asks the host nothing');
  const tooMany = await call('browser_drop', { target: '#drop', paths: Array.from({ length: 17 }, (_, index) => `/x/${index}`) });
  assert.match(textOf(tooMany), /^### Error\nInvalid arguments for tool "browser_drop"/);
  assert.equal(intake.asked.length, asked);
  assert.equal(dropped.length, 2);
});

// A click that starts a download, as Chrome reports one (#294 probe: Chrome for Testing 156 through the official
// extension 0.5.0): the document request, paused at the request stage while Playwright routes, so the host decides
// it; its response, paused at the response stage only if Chrome was asked to, and passed on when that pause is
// continued; then the navigation ends as a download: the request is aborted for the page, and Chrome reports the
// download on the tab's own session. It waits for each answer Chrome would wait for. What Chrome does is the test's
// statement of it. end: the last download state Chrome reports ('' for none after inProgress, null for no progress).
// responseFrameId / beginFrameId: the frame Chrome names for the response and for the download (null: none);
// beginSession: the download reported on that child session instead of the tab's; announce false: the request itself
// is not reported (as one made before the capture opened), and with networkless also no network id for its response
// (as Chrome for Testing 156 reports a link with the download attribute).
function downloadFlow(world, { tabId = TAB, hops, method = 'GET', postData, status = 200, headers = {}, body = '', guid = `GUID-${hops.at(-1)}`, suggestedFilename = 'file.bin', frameId = `TARGET-${tabId}`, end = 'completed', beginUrl, redirects = [],
  responseFrameId = frameId, beginFrameId = frameId, beginSession, announce = true, networkless = false } = {}) {
  const { chrome } = world;
  const answered = (id) => waitFor(() => chrome.fetchAnswers.find((answer) => answer.requestId === id), `Chrome's pause ${id} answered`);
  return (async () => {
    const steps = hops.map((url, index) => (index ? { url, resourceType: 'Document' } : { url, method, postData, resourceType: 'Document' }));
    const { requestId, lastUrl, paused } = announce ? chrome.requestChain(tabId, steps, { navigation: true, frameId }) : { requestId: networkless ? null : `UNSEEN-${hops.at(-1)}`, lastUrl: hops.at(-1), paused: [] };
    const decisions = await Promise.all(paused.map(answered));
    if (decisions.some((decision) => decision.method === 'Fetch.failRequest')) {
      chrome.failRequest(tabId, requestId, 'net::ERR_BLOCKED_BY_CLIENT');
      return { requestId, decisions, blocked: true };
    }
    // A redirect's own response, if Chrome pauses it, before the response of the last hop.
    const redirectPauses = redirects.map((url) => chrome.responseStage(tabId, requestId, url, { status: 302, headers: { Location: lastUrl }, frameId })).filter(Boolean);
    await Promise.all(redirectPauses.map(answered));
    const responsePause = chrome.responseStage(tabId, requestId, lastUrl, { method, status, headers, body, frameId: responseFrameId });
    if (responsePause)
      await answered(responsePause);
    if (requestId)
      chrome.failRequest(tabId, requestId, 'net::ERR_ABORTED', { canceled: true });
    const size = Buffer.byteLength(body);
    chrome.downloadWillBegin(tabId, { frameId: beginFrameId, sessionId: beginSession, guid, url: beginUrl ?? lastUrl, suggestedFilename });
    if (end !== null)
      chrome.downloadProgress(tabId, { guid, state: 'inProgress', receivedBytes: 0, totalBytes: size });
    if (end)
      chrome.downloadProgress(tabId, { guid, state: end, receivedBytes: end === 'completed' ? size : 0, totalBytes: size });
    return { requestId, decisions, responsePause, redirectPauses };
  })();
}

// A connection whose shown page has loaded, as an owner's page has: upstream's click waits up to 10 s for "load"
// after a navigation request (pinned coreBundle.js, waitForCompletion).
async function downloadReady(options) {
  const world = await connected(options);
  world.chrome.navigation(TAB, [`${ALLOWED}/start`]);
  await world.settle();
  return world;
}

// Stands in for the element upstream's click resolves: its click runs the flows in order, after the click returned
// (within: before it returns, so that everything happens while the capture is surely open).
function clickStartingDownloads(world, ...flows) {
  return clickWith(world, false, flows);
}
function clickWithin(world, ...flows) {
  return clickWith(world, true, flows);
}
function clickWith(world, within, flows) {
  const tab = world.core.connection.backend()._context.currentTab();
  const done = [];
  const run = async () => {
    const out = [];
    for (const flow of flows)
      out.push(await flow());
    return out;
  };
  tab.targetLocator = async () => ({
    locator: { click: async () => { done.push(run()); if (within) await done.at(-1); } },
    selector: '#export',
  });
  return async () => (await Promise.all(done)).flat();
}

const PLAYWRIGHT_FETCH = { handleAuthRequests: true, patterns: [{ urlPattern: '*', requestStage: 'Request' }] };
const WITH_RESPONSES = { ...PLAYWRIGHT_FETCH, patterns: [...PLAYWRIGHT_FETCH.patterns, RESPONSE_STAGE_PATTERN] };
const attachment = (name, bytes, more = {}) => ({ 'Content-Type': 'application/zip', 'Content-Disposition': `attachment; filename="${name}"`, 'Content-Length': bytes.length, ...more });

test('download: a click asked to capture keeps the response once, as Chrome received it, while Chrome\'s own download goes on; the two are reported apart', async () => {
  const world = await downloadReady();
  const { chrome, call, guard } = world;
  assert.deepEqual(chrome.cdp('Fetch.enable'), [PLAYWRIGHT_FETCH], 'Playwright\'s routing, which the guard installed');
  const ZIP = Buffer.concat([Buffer.from([0x50, 0x4b, 0x03, 0x04, 0x00, 0xff, 0x0d, 0x0a]), Buffer.from('draft export')]);
  const finished = clickStartingDownloads(world, () => downloadFlow(world, {
    hops: [`${ALLOWED}/api/drafts/1/export`], method: 'POST', postData: 'format=zip', headers: attachment('draft-1.zip', ZIP), body: ZIP, suggestedFilename: 'draft-1.zip',
  }));
  const result = await call('browser_click', { target: '#export', captureDownload: true });
  const [flow] = await finished();
  assert.equal(result.isError, undefined, textOf(result));
  const hash = sha256(ZIP);
  assert.deepEqual(result.content.filter((part) => part.type === 'resource').map((part) => part.resource),
    [{ uri: `chrome-browser://download/1/${hash}`, mimeType: 'application/zip', blob: ZIP.toString('base64') }], 'exact bytes, labelled by the bytes');
  assert.equal(result.content.at(-1).type, 'resource', 'after every note');
  assert.equal(sectionOf(result, 'Result'), [
    `- Download 1: "draft-1.zip", as Chrome names it, from ${ALLOWED}/api/drafts/1/export (POST 200, application/zip)`,
    `  - Captured copy: ${ZIP.length} bytes, sha256 ${hash}, read once from the response as Chrome received it (not fetched again), attached to this result as a binary resource. It shows what the server sent, not that Chrome's own download finished.`,
    `  - Chrome's own download: completed, as Chrome reported (${ZIP.length} bytes). Where Chrome saved it is not known here.`,
  ].join('\n'));
  // The host decided the request itself, at the request stage, while the capture was open: its body included.
  assert.ok(guard.messages().some((message) => message?.url === `${ALLOWED}/api/drafts/1/export` && message.method === 'POST' && message.post_data === 'format=zip' && message.resource_type === 'document'));
  assert.deepEqual(flow.decisions.map((decision) => decision.method), ['Fetch.continueRequest']);
  // That very response was read once and continued once, by the server alone: nothing failed, fulfilled or asked again.
  assert.deepEqual(chrome.cdp('Fetch.getResponseBody'), [{ requestId: flow.responsePause }]);
  assert.deepEqual(chrome.fetchAnswers.filter((answer) => answer.requestId === flow.responsePause).map((answer) => answer.method), ['Fetch.continueRequest']);
  assert.ok(chrome.fetchAnswers.every((answer) => answer.method === 'Fetch.continueRequest'), 'no failRequest or fulfillRequest');
  assert.equal(chrome.cdp('Network.getResponseBody').length, 0);
  // Chrome got Playwright's patterns plus the response stage for the click, and Playwright's own command back after it.
  assert.deepEqual(chrome.cdp('Fetch.enable'), [PLAYWRIGHT_FETCH, WITH_RESPONSES, PLAYWRIGHT_FETCH]);
  assert.deepEqual(chrome.fetchPatterns(TAB), PLAYWRIGHT_FETCH.patterns);

  // A link through a redirect, with no attachment header (<a download>): read because its length is declared, and
  // its redirect's own response passed on unread. The next capture's label never repeats an earlier one.
  const CSV = Buffer.from('id;name\r\n1;Δ\r\n');
  const again = clickStartingDownloads(world, () => downloadFlow(world, {
    hops: [`${ALLOWED}/files/latest`, `${ALLOWED}/files/report.csv`], redirects: [`${ALLOWED}/files/latest`],
    headers: { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Length': CSV.length }, body: CSV, suggestedFilename: 'report.csv',
  }));
  const second = await call('browser_click', { target: '#export', captureDownload: true });
  const [link] = await again();
  assert.equal(second.isError, undefined, textOf(second));
  assert.deepEqual(second.content.filter((part) => part.type === 'resource').map((part) => part.resource),
    [{ uri: `chrome-browser://download/2/${sha256(CSV)}`, mimeType: 'text/csv', blob: CSV.toString('base64') }]);
  assert.match(sectionOf(second, 'Result'), new RegExp(`^- Download 1: "report\\.csv", as Chrome names it, from ${ALLOWED.replace(/\./g, '\\.')}/files/report\\.csv \\(GET 200, text/csv\\)\n  - Captured copy: ${CSV.length} bytes`));
  assert.deepEqual(chrome.cdp('Fetch.getResponseBody').map((params) => params.requestId), [flow.responsePause, link.responsePause], 'the redirect\'s response was not read');
  assert.deepEqual(link.redirectPauses.map((id) => chrome.fetchAnswers.filter((answer) => answer.requestId === id).map((answer) => answer.method)), [['Fetch.continueRequest']]);
  assert.deepEqual(chrome.cdp('Fetch.enable').slice(-2), [WITH_RESPONSES, PLAYWRIGHT_FETCH]);
});

test('download: the host decides every request while a capture is open, and Playwright\'s own Fetch changes then keep their request stage and come back exactly', async () => {
  const world = await downloadReady({ downloadSettleMs: 300 });
  const { chrome, call, page } = world;
  const context = page().context();
  const finished = clickWithin(world, async () => {
    // Playwright stops routing and routes again while the capture is open: Chrome keeps the response stage throughout,
    // and never Playwright's request stage without Playwright.
    await context.unrouteAll();
    await context.route('**/*', world.core.guard.routeHandler());
    return await downloadFlow(world, { hops: [`${FORBIDDEN}/export.zip`], headers: attachment('x.zip', Buffer.from('x')), body: 'x' });
  });
  const result = await call('browser_click', { target: '#export', captureDownload: true });
  const [flow] = await finished();
  assert.equal(flow.blocked, true);
  assert.deepEqual(flow.decisions.map((decision) => [decision.method, decision.errorReason]), [['Fetch.failRequest', 'BlockedByClient']], 'refused at the request stage, as without a capture');
  assert.equal(chrome.cdp('Fetch.getResponseBody').length, 0);
  assert.deepEqual(chrome.cdp('Fetch.enable'), [PLAYWRIGHT_FETCH, WITH_RESPONSES, { patterns: [RESPONSE_STAGE_PATTERN] }, WITH_RESPONSES, PLAYWRIGHT_FETCH]);
  assert.equal(chrome.cdp('Fetch.disable').length, 0, 'Playwright\'s disable became the response stage alone while the capture was open');
  assert.equal(sectionOf(result, 'Result'), '- No download began in this tab within 0.3 s after the click; nothing was captured.');
  assert.match(sectionOf(result, 'Requests the guard refused while this call ran'), /^- GET https:\/\/forbidden\.example\/export\.zip: REFUSED_BY_HOST/);
  // Afterwards the route still decides, and Chrome pauses nothing at the response stage.
  const { paused } = chrome.requestChain(TAB, [`${FORBIDDEN}/later`]);
  const later = await waitFor(() => chrome.fetchAnswers.find((answer) => answer.requestId === paused[0]), 'the later request decided');
  assert.deepEqual([later.method, later.errorReason], ['Fetch.failRequest', 'BlockedByClient']);
  assert.equal(chrome.responseStage(TAB, 'NOT-ASKED', `${ALLOWED}/page`), null);
  // Without captureDownload a click asks Chrome for nothing more.
  const enables = chrome.cdp('Fetch.enable').length;
  clickStartingDownloads(world);
  const plain = await call('browser_click', { target: '#export' });
  assert.equal(plain.isError, undefined, textOf(plain));
  assert.equal(chrome.cdp('Fetch.enable').length, enables);
  assert.doesNotMatch(textOf(plain), /Download|captured/);
});

test('download: what was not captured is said so with its reason, nothing uncertain gets bytes, and Chrome\'s own state is reported apart', async () => {
  const world = await downloadReady({ downloadSettleMs: 300, tabs: [
    { id: TAB, url: `${ALLOWED}/start`, title: 'Start', windowId: 1, index: 0, active: true },
    { id: 8, url: `${ALLOWED}/other`, title: 'Other', windowId: 1, index: 1, active: false },
  ] });
  const { chrome, call } = world;
  const SAME = `${ALLOWED}/export/same.bin`;
  const PAGE = Buffer.from('<!doctype html><title>Export ready</title>');
  const finished = clickWithin(world,
    () => downloadFlow(world, { hops: [`${ALLOWED}/big.bin`], headers: attachment('big.bin', [], { 'Content-Length': DOWNLOAD_MAX_BYTES + 1 }), body: 'not read', suggestedFilename: 'big.bin', end: 'canceled' }),
    () => downloadFlow(world, { hops: [`${ALLOWED}/stream.csv`], headers: { 'Content-Type': 'text/csv', 'Content-Disposition': 'attachment' }, body: 'a,b', suggestedFilename: 'stream.csv', end: '' }),
    // Chunked, with a Content-Length too: Transfer-Encoding wins in HTTP, so the length bounds nothing.
    () => downloadFlow(world, { hops: [`${ALLOWED}/chunks.csv`], headers: { 'Content-Type': 'text/csv', 'Content-Disposition': 'attachment', 'Transfer-Encoding': 'chunked', 'Content-Length': 3 }, body: 'c,d', suggestedFilename: 'chunks.csv' }),
    // A server that sends more than it declared: what Chrome hands over is measured, and more than the bound is dropped.
    () => downloadFlow(world, { hops: [`${ALLOWED}/liar.bin`], headers: attachment('liar.bin', Buffer.from('ten bytes!')), body: Buffer.alloc(DOWNLOAD_MAX_BYTES + 1, 0x61), suggestedFilename: 'liar.bin' }),
    () => downloadFlow(world, { hops: [`${ALLOWED}/packed.txt`], headers: { 'Content-Type': 'text/plain', 'Content-Encoding': 'gzip', 'Content-Length': 3 }, body: 'gz!', suggestedFilename: 'packed.txt', end: null }),
    () => downloadFlow(world, { hops: [SAME], headers: attachment('same.bin', Buffer.from('one')), body: 'one', guid: 'G-1', suggestedFilename: 'same.bin' }),
    () => downloadFlow(world, { hops: [SAME], headers: attachment('same.bin', Buffer.from('two')), body: 'two', guid: 'G-2', suggestedFilename: 'same (1).bin' }),
    // A page the click opened, read because it might have been a download, and never returned.
    async () => {
      const { requestId, lastUrl, paused } = chrome.requestChain(TAB, [{ url: `${ALLOWED}/ready`, resourceType: 'Document' }], { navigation: true });
      await Promise.all(paused.map((id) => waitFor(() => chrome.fetchAnswers.find((answer) => answer.requestId === id), id)));
      const pause = chrome.responseStage(TAB, requestId, lastUrl, { headers: { 'Content-Type': 'text/html', 'Content-Length': PAGE.length }, body: PAGE });
      assert.ok(pause, 'Chrome paused the page at the response stage');
      await waitFor(() => chrome.fetchAnswers.find((answer) => answer.requestId === pause), 'page continued');
      return { pause };
    },
    async () => {
      // Made by the page itself, and one in another tab: no response of either is seen here.
      chrome.downloadWillBegin(TAB, { guid: 'G-blob', url: `blob:${ALLOWED}/5d2c`, suggestedFilename: 'made-here.bin' });
      chrome.downloadProgress(TAB, { guid: 'G-blob', state: 'completed', receivedBytes: 4, totalBytes: 4 });
      chrome.downloadWillBegin(TAB, { guid: 'G-frame', url: `${ALLOWED}/framed.pdf`, suggestedFilename: 'framed.pdf' });
      chrome.downloadWillBegin(8, { guid: 'G-other', url: `${ALLOWED}/other.zip`, suggestedFilename: 'other.zip' });
      chrome.downloadProgress(8, { guid: 'G-other', state: 'completed', receivedBytes: 1, totalBytes: 1 });
      // Chrome's reports so far have reached the server before the click returns.
      await world.settle();
      return {};
    });
  const result = await call('browser_click', { target: '#export', captureDownload: true });
  const flows = await finished();
  assert.equal(result.isError, undefined, textOf(result));
  assert.ok(result.content.every((part) => part.type !== 'resource'), 'no bytes for any of them');
  assert.ok(JSON.stringify(result).length < 20_000, 'the over-read body is not in the result');
  for (const secret of ['one', 'two', 'gz!', 'a,b', 'c,d', PAGE.toString()])
    assert.doesNotMatch(JSON.stringify(result), new RegExp(Buffer.from(secret).toString('base64').replace(/[+/=]/g, '\\$&')));
  assert.equal(sectionOf(result, 'Result'), [
    `- Download 1: "big.bin", as Chrome names it, from ${ALLOWED}/big.bin (GET 200, application/zip)`,
    `  - Captured copy: not captured: the response declared ${DOWNLOAD_MAX_BYTES + 1} bytes, more than the ${DOWNLOAD_MAX_BYTES} read here. It was passed on unread.`,
    '  - Chrome\'s own download: canceled, as Chrome reported.',
    `- Download 2: "stream.csv", as Chrome names it, from ${ALLOWED}/stream.csv (GET 200, text/csv)`,
    '  - Captured copy: not captured: the response declared no length (a chunked response, for example), so reading it could not be bounded beforehand. It was passed on unread.',
    '  - Chrome\'s own download: still in progress when this call returned (0 of 3 bytes); whether it finishes is not known here.',
    `- Download 3: "chunks.csv", as Chrome names it, from ${ALLOWED}/chunks.csv (GET 200, text/csv)`,
    '  - Captured copy: not captured: the response is sent with Transfer-Encoding chunked, which overrides any Content-Length, so reading it could not be bounded beforehand. It was passed on unread.',
    '  - Chrome\'s own download: completed, as Chrome reported (3 bytes). Where Chrome saved it is not known here.',
    `- Download 4: "liar.bin", as Chrome names it, from ${ALLOWED}/liar.bin (GET 200, application/zip)`,
    `  - Captured copy: not captured: Chrome handed over ${DOWNLOAD_MAX_BYTES + 1} bytes, more than the ${DOWNLOAD_MAX_BYTES} returned here, although the response declared 10. They were dropped, not returned.`,
    `  - Chrome's own download: completed, as Chrome reported (${DOWNLOAD_MAX_BYTES + 1} bytes). Where Chrome saved it is not known here.`,
    `- Download 5: "packed.txt", as Chrome names it, from ${ALLOWED}/packed.txt (GET 200, text/plain)`,
    '  - Captured copy: not captured: the response is content-encoded (gzip), so the length Chrome would hand over is not declared. It was passed on unread.',
    '  - Chrome\'s own download: unknown; Chrome reported no progress for it.',
    `- Download 6: "same.bin", as Chrome names it, from ${ALLOWED}/export/same.bin`,
    '  - Captured copy: not captured: another response or download in this call had the same address, so which bytes are this download\'s is uncertain. Nothing is attached for it.',
    '  - Chrome\'s own download: completed, as Chrome reported (3 bytes). Where Chrome saved it is not known here.',
    `- Download 7: "same (1).bin", as Chrome names it, from ${ALLOWED}/export/same.bin`,
    '  - Captured copy: not captured: another response or download in this call had the same address, so which bytes are this download\'s is uncertain. Nothing is attached for it.',
    '  - Chrome\'s own download: completed, as Chrome reported (3 bytes). Where Chrome saved it is not known here.',
    '- Download 8: "made-here.bin", as Chrome names it, from an address with the blob: scheme',
    '  - Captured copy: not captured: the page made it itself (a blob: or data: address); nothing was received over HTTP. Only HTTP responses are captured.',
    '  - Chrome\'s own download: completed, as Chrome reported (4 bytes). Where Chrome saved it is not known here.',
    `- Download 9: "framed.pdf", as Chrome names it, from ${ALLOWED}/framed.pdf`,
    '  - Captured copy: not captured: no response of it was seen in this tab\'s own session (it may come from a cross-origin frame, a service worker or a request made before this click).',
    '  - Chrome\'s own download: unknown; Chrome reported no progress for it.',
    `- A download began in another tab ("other.zip" from ${ALLOWED}/other.zip); not captured: capture covers only the tab this click acts in.`,
  ].join('\n'));
  // Only what could be bounded was read: the over-reporting body, the two same-address bodies and the page. Every
  // pause was continued once.
  const pauses = flows.map((flow) => flow.responsePause ?? flow.pause).filter(Boolean);
  assert.equal(pauses.length, 8);
  assert.deepEqual(chrome.cdp('Fetch.getResponseBody').map((params) => params.requestId), [pauses[3], pauses[5], pauses[6], pauses[7]]);
  for (const pause of pauses)
    assert.deepEqual(chrome.fetchAnswers.filter((answer) => answer.requestId === pause).map((answer) => answer.method), ['Fetch.continueRequest'], pause);
  assert.deepEqual(chrome.fetchPatterns(TAB), PLAYWRIGHT_FETCH.patterns);
  assert.deepEqual(chrome.fetchPatterns(8), PLAYWRIGHT_FETCH.patterns, 'the other tab never had the response stage');
});

test('download: a copy is judged by its own address and every hop on the way, and none is returned when the call\'s output is withheld', async () => {
  const world = await downloadReady({ onCommand: ({ chrome: fake, moveOn }, method, params) => {
    // While Chrome hands over the body, the tab moves on to a page the task may not read.
    if (moveOn && method === 'chrome.debugger.sendCommand' && params?.[1] === 'Fetch.getResponseBody')
      fake.navigation(TAB, [`${FORBIDDEN}/moved`]);
  } });
  const { chrome, call } = world;
  const BYTES = Buffer.from('PK-SECRET-ARCHIVE');
  // An allowed address that redirects to one the task may not read: the hop is not routed, so the request went out.
  const finished = clickStartingDownloads(world, () => downloadFlow(world, {
    hops: [`${ALLOWED}/export`, `${FORBIDDEN}/archive.zip`], headers: attachment('archive.zip', BYTES), body: BYTES, suggestedFilename: 'archive.zip',
  }));
  const result = await call('browser_click', { target: '#export', captureDownload: true });
  await finished();
  assert.equal(result.isError, undefined, textOf(result));
  assert.equal(sectionOf(result, 'Result'), '- Download 1: withheld: this task may not read https://forbidden.example/archive.zip (its address or a redirect hop on the way to it). REFUSED_BY_HOST: https://forbidden.example');
  assert.ok(result.content.every((part) => part.type !== 'resource'));
  assert.doesNotMatch(JSON.stringify(result), new RegExp(BYTES.toString('base64')));
  // An allowed download, but the page this call worked in became one the task may not read: nothing of it is returned.
  world.moveOn = true;
  const moved = clickStartingDownloads(world, () => downloadFlow(world, { hops: [`${ALLOWED}/fine.zip`], headers: attachment('fine.zip', BYTES), body: BYTES }));
  const withheld = await call('browser_click', { target: '#export', captureDownload: true });
  await moved();
  assert.equal(withheld.isError, true);
  assert.match(textOf(withheld), /The call ran, but its output is withheld: this task may not read https:\/\/forbidden\.example\/moved/);
  assert.ok(withheld.content.every((part) => part.type === 'text'), 'no resource');
  assert.doesNotMatch(JSON.stringify(withheld), new RegExp(BYTES.toString('base64')));
  assert.deepEqual(chrome.fetchPatterns(TAB), PLAYWRIGHT_FETCH.patterns);
});

test('download: no bytes without a certain tie: a frame unnamed or different, a download on a session other than the tab\'s, a request not seen from its start or not reported at all', async () => {
  const world = await downloadReady({ downloadSettleMs: 300 });
  const { chrome, call } = world;
  const bytes = (name) => Buffer.from(`bytes of ${name}`);
  const flow = (name, more = {}) => () => downloadFlow(world, { hops: [`${ALLOWED}/${name}`], headers: attachment(name, bytes(name)), body: bytes(name), suggestedFilename: name, ...more });
  const finished = clickWithin(world,
    flow('control.zip'),
    flow('unnamed-response-frame.zip', { responseFrameId: null }),
    flow('unnamed-download-frame.zip', { beginFrameId: null }),
    flow('other-frame.zip', { responseFrameId: 'FRAME-OTHER' }),
    flow('child-session.zip', { beginSession: 'CHILD-SESSION-1' }),
    flow('unseen-request.zip', { announce: false }),
    flow('download-attribute.txt', { announce: false, networkless: true }),
    async () => { await world.settle(); return {}; });
  const result = await call('browser_click', { target: '#export', captureDownload: true });
  await finished();
  assert.equal(result.isError, undefined, textOf(result));
  assert.deepEqual(result.content.filter((part) => part.type === 'resource').map((part) => part.resource.blob), [bytes('control.zip').toString('base64')], 'only the control');
  for (const name of ['unnamed-response-frame.zip', 'unnamed-download-frame.zip', 'other-frame.zip', 'child-session.zip', 'unseen-request.zip', 'download-attribute.txt'])
    assert.doesNotMatch(JSON.stringify(result), new RegExp(bytes(name).toString('base64').replace(/[+/=]/g, '\\$&')), name);
  const lines = sectionOf(result, 'Result').split('\n');
  const copy = (name) => lines[lines.findIndex((line) => line.includes(`"${name}"`)) + 1];
  assert.match(copy('control.zip'), /^  - Captured copy: \d+ bytes, sha256 [0-9a-f]{64}, read once/);
  for (const name of ['unnamed-response-frame.zip', 'unnamed-download-frame.zip'])
    assert.equal(copy(name), '  - Captured copy: not captured: Chrome did not name the frame of the download or of the response with its address, so the two cannot be tied together. Nothing is attached for it.', name);
  assert.equal(copy('other-frame.zip'), '  - Captured copy: not captured: the response with its address came in another frame than the download, so the two cannot be tied together. Nothing is attached for it.');
  assert.equal(copy('child-session.zip'), '  - Captured copy: not captured: Chrome reported it on a frame\'s or worker\'s own session, not on the tab\'s session where responses are read, so no response can be tied to it. Nothing is attached for it.');
  assert.equal(copy('unseen-request.zip'), '  - Captured copy: not captured: not every address on the way to its response was seen (its request began before this click, or its redirect hops were not all kept), so they cannot all be checked. Nothing is attached for it.');
  assert.equal(copy('download-attribute.txt'), '  - Captured copy: not captured: Chrome reported no network request for its response (as for a link with the download attribute), so the addresses on the way to it cannot be checked. Nothing is attached for it.');
  assert.doesNotMatch(textOf(result), /More came during this click/);
});

test('download: when a limit drops what a capture observes, no download gets bytes, even one left with a single match', async () => {
  const world = await downloadReady({ downloadSettleMs: 300 });
  const { chrome, call } = world;
  const BYTES = Buffer.from('the only kept match');
  const answered = (id) => waitFor(() => chrome.fetchAnswers.find((answer) => answer.requestId === id), id);
  const reported = async () => { await world.settle(); return {}; };
  const expectNoBytes = (result, what, dropped) => {
    assert.equal(result.isError, undefined, textOf(result));
    assert.ok(result.content.every((part) => part.type !== 'resource'), `${what}: no bytes`);
    assert.doesNotMatch(JSON.stringify(result), new RegExp(BYTES.toString('base64')), what);
    const text = sectionOf(result, 'Result');
    assert.match(text, /^- Download 1: "kept\.zip", as Chrome names it, from https:\/\/allowed\.example\/kept\.zip\n  - Captured copy: not captured: more document responses, downloads or requests came during this click than one capture keeps, so a response or download with the same address may have gone unrecorded\. Nothing is attached for it\.\n/, what);
    assert.match(text, new RegExp(`\\n- More came during this click than one capture keeps \\(${MAX_RESPONSES} document responses, ${MAX_BEGINS} downloads, ${MAX_CHAINS} document requests\\): ${dropped}\\. No bytes were attached, since what was dropped could have shared an address\\.$`), what);
  };
  // Responses: the kept one first, then fillers up to the limit; the competitor with the same address is dropped.
  let competitor;
  let finished = clickWithin(world,
    () => downloadFlow(world, { hops: [`${ALLOWED}/kept.zip`], headers: attachment('kept.zip', BYTES), body: BYTES, suggestedFilename: 'kept.zip', end: null }),
    async () => {
      for (let index = 1; index < MAX_RESPONSES; index += 1)
        await answered(chrome.responseStage(TAB, `FILL-${index}`, `${ALLOWED}/fill-${index}`, { headers: { 'Content-Type': 'text/html' } }));
      competitor = chrome.responseStage(TAB, 'COMPETITOR', `${ALLOWED}/kept.zip`, { headers: attachment('kept.zip', Buffer.from('competing bytes')), body: 'competing bytes' });
      await answered(competitor);
      chrome.downloadProgress(TAB, { guid: `GUID-${ALLOWED}/kept.zip`, state: 'completed', receivedBytes: BYTES.length, totalBytes: BYTES.length });
      return {};
    },
    reported);
  expectNoBytes(await call('browser_click', { target: '#export', captureDownload: true }), 'responses', '1 responses passed on unread, 0 downloads and 0 requests not recorded');
  await finished();
  assert.ok(!chrome.cdp('Fetch.getResponseBody').some((params) => params.requestId === competitor), 'the dropped competitor was passed on unread');
  assert.deepEqual(chrome.fetchAnswers.filter((answer) => answer.requestId === competitor).map((answer) => answer.method), ['Fetch.continueRequest']);
  // Downloads: the kept one first, then fillers up to the limit; a second download of the same address is dropped.
  finished = clickWithin(world,
    () => downloadFlow(world, { hops: [`${ALLOWED}/kept.zip`], headers: attachment('kept.zip', BYTES), body: BYTES, suggestedFilename: 'kept.zip' }),
    async () => {
      for (let index = 1; index < MAX_BEGINS; index += 1)
        chrome.downloadWillBegin(TAB, { guid: `FILL-${index}`, url: `blob:${ALLOWED}/fill-${index}`, suggestedFilename: `fill-${index}` });
      chrome.downloadWillBegin(TAB, { guid: 'TWIN', url: `${ALLOWED}/kept.zip`, suggestedFilename: 'kept (1).zip' });
      return {};
    },
    reported);
  expectNoBytes(await call('browser_click', { target: '#export', captureDownload: true }), 'downloads', '0 responses passed on unread, 1 downloads and 0 requests not recorded');
  await finished();
  // Requests: more document requests than are kept, the download's own among those dropped.
  finished = clickWithin(world,
    async () => {
      for (let index = 0; index < MAX_CHAINS; index += 1)
        chrome.emit(TAB, 'Network.requestWillBeSent', { requestId: `CHAIN-${index}`, loaderId: `CHAIN-${index}`, documentURL: `${ALLOWED}/c-${index}`, request: { url: `${ALLOWED}/c-${index}`, method: 'GET', headers: {} }, timestamp: 1, wallTime: 1, initiator: { type: 'other' }, type: 'Document', frameId: 'NO-SUCH-FRAME' });
      return {};
    },
    () => downloadFlow(world, { hops: [`${ALLOWED}/kept.zip`], headers: attachment('kept.zip', BYTES), body: BYTES, suggestedFilename: 'kept.zip' }),
    reported);
  expectNoBytes(await call('browser_click', { target: '#export', captureDownload: true }), 'requests', '0 responses passed on unread, 0 downloads and 1 requests not recorded');
  await finished();
  assert.deepEqual(chrome.fetchPatterns(TAB), PLAYWRIGHT_FETCH.patterns);
});

test('download: one capture at a time, only on browser_click; a failed click waits for nothing, and the capture always closes', async () => {
  const world = await downloadReady({ downloadSettleMs: 1500 });
  const { chrome, call, core } = world;
  const { tools } = await core.listTools();
  assert.deepEqual(tools.filter((tool) => tool.inputSchema.properties?.captureDownload).map((tool) => tool.name), ['browser_click']);
  assert.equal(tools.find((tool) => tool.name === 'browser_click').inputSchema.properties.captureDownload.type, 'boolean');
  // A second capture while the first is open is refused before anything is clicked.
  let clicks = 0;
  const tab = core.connection.backend()._context.currentTab();
  tab.targetLocator = async () => ({ locator: { click: async () => { clicks += 1; } }, selector: '#export' });
  const first = call('browser_click', { target: '#export', captureDownload: true });
  await waitFor(() => chrome.cdp('Fetch.enable').length === 2, 'the first capture open');
  const second = await call('browser_click', { target: '#export', captureDownload: true });
  assert.equal(second.isError, true);
  assert.match(textOf(second), /^### Error\nBROWSER_DOWNLOAD_CAPTURE_BUSY: another call is capturing a download on this connection\nNothing was clicked\./);
  const done = await first;
  assert.equal(clicks, 1);
  assert.equal(sectionOf(done, 'Result'), '- No download began in this tab within 1.5 s after the click; nothing was captured.');
  // A click that fails and began nothing: upstream's error alone, no wait, and Playwright's patterns are back.
  tab.targetLocator = async () => ({ locator: { click: async () => { throw new Error('Timeout 5000ms exceeded.'); } }, selector: '#export' });
  let started = Date.now();
  const failed = await call('browser_click', { target: '#export', captureDownload: true });
  assert.ok(Date.now() - started < 1500, 'no wait for a download after a failed click');
  assert.equal(textOf(failed), '### Error\nError: Timeout 5000ms exceeded.');
  assert.deepEqual(chrome.cdp('Fetch.enable').slice(-2), [WITH_RESPONSES, PLAYWRIGHT_FETCH]);
  // A click that fails after its download began: the error, then what Chrome already reported, and the copy.
  const BYTES = Buffer.from('half-way');
  tab.targetLocator = async () => ({ locator: { click: async () => {
    await downloadFlow(world, { hops: [`${ALLOWED}/late.bin`], headers: attachment('late.bin', BYTES), body: BYTES, suggestedFilename: 'late.bin', end: '' });
    // Chrome's reports so far have reached the server before the click fails.
    await world.settle();
    throw new Error('Timeout 5000ms exceeded.');
  } }, selector: '#export' });
  started = Date.now();
  const late = await call('browser_click', { target: '#export', captureDownload: true });
  assert.ok(Date.now() - started < 1500, 'no wait after a failed click');
  assert.equal(late.isError, true);
  assert.equal(textOf(late), [
    '### Error',
    'Error: Timeout 5000ms exceeded.',
    `- Download 1: "late.bin", as Chrome names it, from ${ALLOWED}/late.bin (GET 200, application/zip)`,
    `  - Captured copy: ${BYTES.length} bytes, sha256 ${sha256(BYTES)}, read once from the response as Chrome received it (not fetched again), attached to this result as a binary resource. It shows what the server sent, not that Chrome's own download finished.`,
    `  - Chrome's own download: still in progress when this call returned (0 of ${BYTES.length} bytes); whether it finishes is not known here.`,
  ].join('\n'));
  assert.deepEqual(late.content.filter((part) => part.type === 'resource').map((part) => part.resource.blob), [BYTES.toString('base64')]);
  assert.deepEqual(chrome.fetchPatterns(TAB), PLAYWRIGHT_FETCH.patterns);
  // Chrome refuses the response stage: nothing is clicked, and its answer is quoted.
  core.connection.downloads()._send = async () => { throw new Error('Fetch.enable: Not allowed'); };
  clicks = 0;
  tab.targetLocator = async () => ({ locator: { click: async () => { clicks += 1; } }, selector: '#export' });
  const unavailable = await call('browser_click', { target: '#export', captureDownload: true });
  assert.match(textOf(unavailable), /^### Error\nBROWSER_DOWNLOAD_CAPTURE_UNAVAILABLE: Chrome did not accept the response-stage interception \(Fetch\.enable: Not allowed\)\nNothing was clicked\./);
  assert.equal(clicks, 0);
});

test('file lines: only generated lines naming a file really in the folder change, and the folder is not listed', (t) => {
  const session = fs.mkdtempSync(path.join(os.tmpdir(), 'cb-output-'));
  cleanups.push(async () => fs.rmSync(session, { recursive: true, force: true }));
  const dir = path.join(session, 'output');
  fs.mkdirSync(dir);
  fs.writeFileSync(path.join(dir, 'page-1.png'), 'x');
  fs.writeFileSync(path.join(dir, 'console-1.log'), 'x');
  fs.writeFileSync(path.join(dir, 'dl.bin'), 'x');
  fs.writeFileSync(path.join(session, 'outside-secret.png'), 'x');
  const listing = t.mock.method(fs, 'readdirSync');
  const lookups = t.mock.method(fs, 'existsSync');
  const rewritten = (lines, extra = []) => describePrivateFiles({ content: [{ type: 'text', text: lines.join('\n') }, ...extra] }, dir).content[0].text.split('\n');
  const image = [{ type: 'image', data: 'AA==', mimeType: 'image/png' }];
  assert.deepEqual(rewritten(['### Result', '- [Screenshot of viewport](output/page-1.png)'], image), ['### Result', '- Screenshot of viewport: the image is attached to this result']);
  assert.deepEqual(rewritten(['### Result', '- [Report](output/page-1.png)']), ['### Result', '- Report: kept in this server\'s private folder, which cannot be opened from here']);
  assert.deepEqual(rewritten(['### Events', '- New console entries: output/console-1.log#L3', '- Downloaded file a.zip to "output/dl.bin"']), ['### Events', '- New console entries: read them with browser_console_messages', '- Downloaded file a.zip: kept in this server\'s private folder, which cannot be opened from here']);
  assert.deepEqual(rewritten(['### Ran Playwright code', '// Screenshot viewport and save it as output/page-1.png', "  path: 'output/page-1.png',"]), ['### Ran Playwright code', '// Screenshot viewport and save it as <server-private file>', "  path: '<server-private file>',"]);
  // Left as they are: a file not in the folder, a path out of it, and page text outside generated sections.
  for (const lines of [
    ['### Result', '- [Report](output/gone.png)'],
    ['### Result', '- [Report](output/../outside-secret.png)'],
    ['### Result', '- [Report](output/..)'],
    ['### Snapshot', '- [Report](output/page-1.png)'],
  ])
    assert.deepEqual(rewritten(lines), lines);
  assert.equal(listing.mock.callCount(), 0, 'the folder is never enumerated');
  const looked = lookups.mock.calls.length;
  // A long result with no generated file line asks the filesystem nothing.
  rewritten(['### Result', ...Array.from({ length: 2000 }, (_, index) => `line ${index} output/page-1.png`)]);
  assert.equal(lookups.mock.calls.length, looked, 'work follows references in the result');
  assert.deepEqual(describePrivateFiles({ content: [{ type: 'text', text: 'x' }] }, ''), { content: [{ type: 'text', text: 'x' }] });
});

test('the tools stay offered, and arguments the server does not take are refused before anything happens', async () => {
  const { chrome, call, core } = await connected();
  const { tools } = await core.listTools();
  assert.equal(tools.length, 25);
  for (const name of ['browser_console_messages', 'browser_network_requests', 'browser_network_request', 'browser_take_screenshot'])
    assert.ok(tools.some((tool) => tool.name === name), `${name} is offered`);
  assert.ok(tools.every((tool) => !tool.inputSchema.properties?.filename), 'no tool advertises a filename');
  assert.ok(tools.every((tool) => !tool.inputSchema.properties?._meta), 'no tool advertises _meta');
  // Files for the page: upstream's "paths" kept, "files" added with an optional root, both bounded.
  for (const name of ['browser_file_upload', 'browser_drop']) {
    const { properties } = tools.find((tool) => tool.name === name).inputSchema;
    assert.deepEqual([properties.paths.maxItems, properties.paths.items, properties.files.maxItems], [16, { type: 'string' }, 16], name);
    assert.deepEqual(properties.files.items, { type: 'object', properties: properties.files.items.properties, required: ['path'], additionalProperties: false }, name);
    assert.deepEqual(Object.keys(properties.files.items.properties), ['path', 'root'], name);
    assert.match(properties.paths.description, /The host reads each file with its own file admission and keeps a copy; the page gets only that copy's bytes and the file's original name\. At most 16 files and 20971520 bytes in one call\./, name);
  }
  assert.match(tools.find((tool) => tool.name === 'browser_drop').description, /At least one of "paths", "files" or "data" must be provided\./);
  // Refusing an argument never takes an advertised one.
  for (const tool of tools) {
    for (const property of Object.keys(tool.inputSchema.properties ?? {}))
      assert.equal(argumentRefusal(tool.name, { [property]: 'x' }), '', `${tool.name} still takes ${property}`);
  }
  const pageCalls = chrome.cdp('Runtime.callFunctionOn').length;
  for (const [name, args] of [
    ['browser_snapshot', { filename: 'x.md' }], ['browser_take_screenshot', { filename: 'x.png' }], ['browser_tabs', { action: 'new', isolatedContext: 'other' }],
    // Not advertised, but upstream reads these from the raw arguments: json, raw and cwd change how a result is formatted.
    ['browser_snapshot', { _meta: { raw: true } }], ['browser_take_screenshot', { _meta: { cwd: '/' } }], ['browser_tabs', { action: 'list', _meta: { json: true } }],
  ]) {
    const result = await call(name, args);
    assert.equal(result.isError, true);
    assert.match(textOf(result), /^### Error\nBROWSER_ARGUMENT_REFUSED/, name);
  }
  assert.equal(chrome.cdp('Runtime.callFunctionOn').length, pageCalls);
  assert.equal(chrome.commands.filter((command) => command.method === 'chrome.tabs.create').length, 0);
});

test('navigation is bounded under the host\'s default tool timeout, and upstream\'s other timeouts stay', async () => {
  assert.ok(NAVIGATION_TIMEOUT_MS + 5000 < 60_000, 'the wait for "load" included, with room for the guard');
  const config = await upstreamConfig(runtime.bundle.tools);
  assert.equal(config.timeouts.navigation, NAVIGATION_TIMEOUT_MS);
  assert.equal(config.timeouts.action, 5000);
  const { core } = await connected();
  assert.equal(core.connection.backend()._context.currentTab().navigationTimeoutOptions.timeout, NAVIGATION_TIMEOUT_MS, 'the tab uses it');
});
