// Pointer hooks and frame preparation, against fake Playwright server objects.
import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createPointerHooks, InputFramePreparer, STABLE_GAP_MS, PROBE_EVENT_TYPES, unconfirmedInputMessage } from '../../skills/chrome_browser/lib/input.mjs';

function fakeProgress() {
  const waits = [];
  const logs = [];
  return {
    waits,
    logs,
    race: (promise) => promise,
    wait: async (ms) => {
      waits.push(ms);
    },
    log: (line) => logs.push(line),
  };
}

function handleWithRects(rects) {
  const calls = [];
  return {
    calls,
    evaluateInUtility: async (fn, arg) => {
      calls.push(fn.name);
      return rects.shift();
    },
  };
}

test('stability: two host-timed samples with the same box pass; the wait is on the server clock', async () => {
  const hooks = createPointerHooks();
  const progress = fakeProgress();
  const box = { x: 10, y: 20, width: 100, height: 30 };
  const handle = handleWithRects([{ ...box }, { ...box }]);
  assert.equal(await hooks.stability(handle, progress, { name: 'click' }), 'done');
  assert.deepEqual(progress.waits, [STABLE_GAP_MS]);
  assert.deepEqual(handle.calls, ['measureInPage', 'measureInPage']);
});

test('stability: a box that moved is "not stable" (Playwright then retries; nothing is dispatched)', async () => {
  const hooks = createPointerHooks();
  const handle = handleWithRects([{ x: 10, y: 20, width: 100, height: 30 }, { x: 11, y: 20, width: 100, height: 30 }]);
  assert.deepEqual(await hooks.stability(handle, fakeProgress(), { name: 'click' }), { missingState: 'stable' });
});

test('stability: a detached element is reported as such, before and after the wait', async () => {
  const hooks = createPointerHooks();
  assert.equal(await hooks.stability(handleWithRects(['error:notconnected']), fakeProgress()), 'error:notconnected');
  assert.equal(await hooks.stability(handleWithRects([{ x: 0, y: 0, width: 1, height: 1 }, 'error:notconnected']), fakeProgress()), 'error:notconnected');
});

test('the measuring and probing functions are self-contained (Playwright serializes them into the page)', async () => {
  const hooks = createPointerHooks();
  let measured;
  let probed;
  const handle = {
    evaluateInUtility: async (fn) => {
      measured = fn.toString();
      return { x: 0, y: 0, width: 1, height: 1 };
    },
    _evaluateHandleInUtility: async (fn, types) => {
      probed = fn.toString();
      assert.deepEqual(types, PROBE_EVENT_TYPES);
      return { _objectId: 'probe-1', evaluate: async () => [], dispose() {} };
    },
  };
  await hooks.stability(handle, fakeProgress());
  await hooks.beforePerform(handle, fakeProgress(), { name: 'click' });
  for (const source of [measured, probed]) {
    // Runs inside a page: no module scope, only its argument array.
    const fn = new Function(`return (${source});`)();
    assert.equal(typeof fn, 'function');
    assert.doesNotMatch(source, /\bhooks\b|\bprogress\b|gapMs|STABLE_GAP_MS|PROBE_EVENT_TYPES/);
  }
  // The probe, run against a fake injected script, records trusted events and their target.
  const target = { name: 'button' };
  const listeners = new Map();
  const injected = {
    retarget: () => target,
    window: { addEventListener: (type, fn) => listeners.set(type, fn), removeEventListener: (type) => listeners.delete(type) },
  };
  const probe = new Function(`return (${probed});`)()([injected, target, ['mousedown', 'click']]);
  listeners.get('mousedown')({ isTrusted: true, type: 'mousedown', composedPath: () => [{ name: 'span' }, target] });
  listeners.get('click')({ isTrusted: false, type: 'click', composedPath: () => [target] });
  assert.deepEqual(probe.stop(), [{ type: 'mousedown', onTarget: true }]);
  assert.equal(listeners.size, 0);
});

function probeHandle(seen, { throwOnRead = false } = {}) {
  const state = { disposed: false, stops: 0 };
  return {
    state,
    _evaluateHandleInUtility: async () => ({
      _objectId: 'probe',
      evaluate: async () => {
        state.stops += 1;
        if (throwOnRead)
          throw new Error('Execution context was destroyed');
        return seen;
      },
      dispose() {
        state.disposed = true;
      },
    }),
  };
}

test('delivery probe: a trusted event at the target confirms the click', async () => {
  const hooks = createPointerHooks();
  const handle = probeHandle([{ type: 'pointerdown', onTarget: true }, { type: 'click', onTarget: true }]);
  const probe = await hooks.beforePerform(handle, fakeProgress(), { name: 'click' });
  await hooks.afterPerform(handle, fakeProgress(), { name: 'click' }, probe);
  hooks.releaseProbe(probe);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(handle.state.disposed, true);
});

// The verdict is recorded, not thrown: a thrown error is retried (clicked again) by
// Playwright's locator retry; see unit_click_once.test.mjs for that path.
test('delivery probe: no trusted event at the target is BROWSER_INPUT_UNCONFIRMED and is not retried', async () => {
  const hooks = createPointerHooks();
  for (const seen of [[], [{ type: 'mousedown', onTarget: false }]]) {
    const handle = probeHandle(seen);
    const mark = hooks.deliveryMark();
    const probe = await hooks.beforePerform(handle, fakeProgress(), { name: 'click' });
    await hooks.afterPerform(handle, fakeProgress(), { name: 'click' }, probe);
    const [entry] = hooks.deliveriesSince(mark);
    assert.equal(entry.verdict, 'denied');
    assert.match(unconfirmedInputMessage(entry.action, entry.seen), /^BROWSER_INPUT_UNCONFIRMED: .*It was not sent again\./);
  }
});

test('delivery probe: an unreadable probe (the document changed) neither confirms nor denies', async () => {
  const hooks = createPointerHooks();
  const handle = probeHandle(null, { throwOnRead: true });
  const probe = await hooks.beforePerform(handle, fakeProgress(), { name: 'click' });
  const progress = fakeProgress();
  await hooks.afterPerform(handle, progress, { name: 'click' }, probe);
  assert.ok(progress.logs.some((line) => /unreadable/.test(line)));
  assert.deepEqual(hooks.deliveriesSince(0).map((entry) => entry.verdict), ['probe_unreadable']);
});

test('delivery probe applies to click and dblclick only (hover, drag and drop keep upstream semantics)', async () => {
  const hooks = createPointerHooks();
  for (const name of ['hover', 'move and down', 'move and up', 'drop', 'tap'])
    assert.equal(await hooks.beforePerform(probeHandle([]), fakeProgress(), { name }), undefined, name);
  assert.ok(await hooks.beforePerform(probeHandle([]), fakeProgress(), { name: 'dblclick' }));
});

function fakePage() {
  const page = new EventEmitter();
  const mainFrame = { name: 'main' };
  page.mainFrame = () => mainFrame;
  page.shots = 0;
  page.screenshot = async (options) => {
    page.shots += 1;
    page.lastOptions = options;
    return Buffer.from([1, 2, 3]);
  };
  return page;
}

test('frame preparation: one discarded capture per document, again after a main-frame navigation only', async () => {
  const preparer = new InputFramePreparer();
  const page = fakePage();
  assert.equal((await preparer.prepare(page)).prepared, true);
  assert.equal((await preparer.prepare(page)).prepared, false);
  page.emit('framenavigated', { name: 'child' });
  assert.equal((await preparer.prepare(page)).prepared, false);
  page.emit('framenavigated', page.mainFrame());
  assert.equal((await preparer.prepare(page)).prepared, true);
  assert.equal(page.shots, 2);
  assert.deepEqual(page.lastOptions, { type: 'png', caret: 'initial', scale: 'css', timeout: 8000 });
});

test('frame preparation: no frame means no input (BROWSER_INPUT_NOT_READY)', async () => {
  const preparer = new InputFramePreparer({ timeoutMs: 50 });
  const page = fakePage();
  page.screenshot = async () => {
    const error = new Error('Timeout 50ms exceeded');
    error.name = 'TimeoutError';
    throw error;
  };
  await assert.rejects(preparer.prepare(page), (error) => error.code === 'BROWSER_INPUT_NOT_READY' && /no input was sent/.test(error.message));
  page.screenshot = async () => Buffer.alloc(0);
  await assert.rejects(preparer.prepare(page), (error) => error.code === 'BROWSER_INPUT_NOT_READY');
});
