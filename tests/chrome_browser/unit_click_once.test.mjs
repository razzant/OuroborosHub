// A click whose effect is unknown is sent once, through the pinned Playwright's own pointer path.
//
// Only the page I/O is faked. Frame.click and the element's pointer action are the patched bundle's own
// (the pinned patch applied in memory), driven with the skill's real hooks. A thrown verdict would be
// retried by Playwright's locator retry and click again; the hooks record it instead. See the header of
// skills/chrome_browser/lib/input.mjs. unit_input.test.mjs checks the hooks alone.
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import Module from 'node:module';
import { SKILL, NODE_MODULES, requirePinnedInstall } from './helpers/env.mjs';
import { createPointerHooks, NOT_SENT_AGAIN } from '../../skills/chrome_browser/lib/input.mjs';
import { applyUnifiedDiff } from '../../skills/chrome_browser/lib/patch.mjs';
import { PINS } from '../../skills/chrome_browser/lib/pinned.mjs';

requirePinnedInstall();
const sha256 = (data) => crypto.createHash('sha256').update(data).digest('hex');

let internals = null;
function patchedInternals() {
  if (internals)
    return internals;
  const file = path.join(NODE_MODULES, 'playwright-core', 'lib', 'coreBundle.js');
  const source = fs.readFileSync(file, 'utf8');
  assert.equal(sha256(Buffer.from(source, 'utf8')), PINS.patch.preimage_sha256);
  const patched = applyUnifiedDiff(source, fs.readFileSync(path.join(SKILL, PINS.patch.file), 'utf8'));
  assert.equal(sha256(Buffer.from(patched, 'utf8')), PINS.patch.postimage_sha256, 'exactly the production postimage');
  // Test-only accessor appended after the production bytes; the module is not registered in the cache.
  const mod = new Module(file, null);
  mod.filename = file;
  mod.paths = Module._nodeModulePaths(path.dirname(file));
  mod._compile(`${patched};module.exports.__qa = { Frame, ElementHandle, NonRecoverableDOMError };\n`, file);
  internals = mod.exports;
  return internals;
}

// Like Playwright's progress for one call: whatever the call awaits through race() or wait() ends with a timeout error
// at the call's deadline, so a call that keeps retrying fails instead of spinning (the real wait races the same way). The
// clock is read at every race(): this page answers instantly, so a retry loop may never yield to a timer.
function fakeProgress(deadlineMs = 5000) {
  const logs = [];
  const deadline = Date.now() + deadlineMs;
  const timeout = () => Object.assign(new Error(`Timeout ${deadlineMs}ms exceeded.`), { name: 'TimeoutError' });
  const expired = new Promise((resolve, reject) => setTimeout(() => reject(timeout()), deadlineMs).unref());
  expired.catch(() => {});
  const race = (promise) => (Date.now() >= deadline ? Promise.reject(timeout())
    : Promise.race([Array.isArray(promise) ? Promise.race(promise) : promise, expired]));
  return {
    logs,
    timeout: deadlineMs,
    log: (line) => logs.push(line),
    race,
    wait: (ms) => race(new Promise((resolve) => setTimeout(resolve, Math.min(ms, 5)))),
  };
}

// A Frame and an ElementHandle of the patched bundle whose page I/O answers like a hidden tab
// where the hit-target interceptor sees nothing (it then reports "done", as upstream does).
// moving: the box differs at every sample. missingState: the page says the target lacks that state (hidden, disabled);
// a function is asked when the page is asked, for a state that changes. counts.io is the order of the page's answers.
function clickFixture({ probe, abortClick = false, moving = false, missingState }) {
  const { Frame, ElementHandle, NonRecoverableDOMError } = patchedInternals().__qa;
  const counts = { clicks: 0, statesAsked: [], io: [] };
  let moved = 0;
  const noop = async () => {};
  const page = {
    performActionPreChecks: noop,
    openScope: { race: (promise) => promise },
    frameManager: { waitForSignalsCreatedBy: async (progress, waitAfter, fn) => await fn(progress) },
    keyboard: { ensureModifiers: async () => undefined },
    mouse: {
      click: async () => {
        if (abortClick)
          throw new NonRecoverableDOMError('QA: the action ended before the click went out');
        counts.clicks += 1;
        if (counts.clicks >= 3)
          throw new NonRecoverableDOMError('QA stop: the same click was dispatched a third time');
      },
    },
  };
  const frame = Object.create(Frame.prototype);
  Object.assign(frame, {
    _page: page,
    _parentFrame: null,
    _detachedScope: { race: (promise) => promise, isClosed: () => false },
    _asLocator: (selector) => selector,
    raceAgainstEvaluationStallingEvents: (fn) => fn(),
  });
  const box = { x: 1, y: 2, width: 30, height: 10 };
  const handle = Object.create(ElementHandle.prototype);
  Object.assign(handle, {
    _frame: frame,
    _page: page,
    instrumentation: { onBeforeInputAction: noop },
    dispose: () => {},
    evaluateInUtility: async (fn, arg) => {
      if (fn.name === 'measureInPage') {
        counts.io.push('box');
        return moving ? { ...box, x: box.x + (moved += 1) } : { ...box };
      }
      if (String(fn).includes('scrollIntoView'))
        return 'done';
      if (String(fn).includes('checkElementStates')) {
        counts.statesAsked.push(arg.elementStates);
        counts.io.push('states');
        const missing = typeof missingState === 'function' ? missingState() : missingState;
        return missing ? { missingState: missing } : undefined;
      }
      return undefined; // markTargetElements: nothing to report
    },
    _scrollRectIntoViewIfNeeded: async () => 'done',
    _clickablePoint: async () => ({ point: { x: 10, y: 5 }, box }),
    _checkFrameIsHitTarget: async (progress, point) => ({ framePoint: point }),
    _evaluateHandleInUtility: async (fn) => {
      if (fn.name === 'installProbeInPage') {
        if (probe === 'install-fails')
          throw new Error('Execution context was destroyed');
        return {
          _objectId: 'probe',
          evaluate: async (read) => {
            if (probe === 'unreadable')
              throw new Error('Execution context was destroyed');
            return read({ stop: () => probe });
          },
          dispose() {},
        };
      }
      return { _objectId: 'hit-target', evaluate: async () => 'done', dispose() {} };
    },
  });
  frame.selectors = {
    callOnSelectorHandle: async () => ({
      frameVisible: true,
      result: {
        evaluate: async (read) => read({ log: '', success: true, box }),
        evaluateHandle: async () => handle,
        dispose: () => {},
      },
    }),
  };
  return { Frame, frame, counts };
}

async function locatorClick(probe, { deadlineMs, ...options } = {}) {
  const hooks = createPointerHooks();
  patchedInternals().tools.setPointerActionHooks(hooks);
  const { Frame, frame, counts } = clickFixture({ probe, ...options });
  const progress = fakeProgress(deadlineMs);
  let outcome;
  try {
    await Frame.prototype.click.call(frame, progress, { selector: '#target', strict: true }, { timeout: 5000 });
    outcome = 'resolved';
  } catch (error) {
    outcome = `rejected: ${String(error.message).split('\n')[0]}`;
  }
  const verdicts = typeof hooks.deliveriesSince === 'function' ? hooks.deliveriesSince(0).map((entry) => entry.verdict) : null;
  return { clicks: counts.clicks, outcome, verdicts, log: progress.logs, statesAsked: counts.statesAsked };
}

test('a click the page saw no trusted event for is dispatched once through the real locator retry', async () => {
  const denied = await locatorClick([]);
  const confirmed = await locatorClick([{ type: 'pointerdown', onTarget: true }]);
  assert.deepEqual([confirmed.clicks, confirmed.outcome], [1, 'resolved']);
  assert.equal(denied.clicks, 1, `the click was dispatched ${denied.clicks} times (${denied.outcome})`);
  assert.equal(denied.outcome, 'resolved');
  assert.deepEqual(denied.verdicts, ['denied']);
});

test('a probe that cannot be installed or read back is recorded as unverified, and the click is sent once', async () => {
  const unavailable = await locatorClick('install-fails');
  const unreadable = await locatorClick('unreadable');
  const aborted = await locatorClick('install-fails', { abortClick: true });
  assert.deepEqual([unavailable.clicks, unavailable.outcome], [1, 'resolved']);
  assert.deepEqual([unreadable.clicks, unreadable.outcome], [1, 'resolved']);
  assert.deepEqual(unavailable.verdicts, ['probe_unavailable']);
  assert.deepEqual(unreadable.verdicts, ['probe_unreadable']);
  assert.equal(aborted.clicks, 0);
  assert.deepEqual(aborted.verdicts, [], 'a click that never went out is not reported as sent');
});

// Negative controls on the same path. Nothing is dispatched to a moving, hidden or disabled target: Playwright waits on the
// missing state until the call's deadline and the call ends with its timeout error. The states it asks the page about are
// still every upstream state except "stable", which the host-timed hook replaces. (A covered target is caught inside the
// page by upstream's hit-target interceptor, which a fake page cannot reproduce; the patch leaves it unchanged and pinned.)
test('a moving, hidden or disabled target is never clicked, and every upstream state but "stable" is still checked', async () => {
  const scenarios = { stable: { moving: true }, visible: { missingState: 'visible' }, enabled: { missingState: 'enabled' } };
  for (const [state, fault] of Object.entries(scenarios)) {
    const result = await locatorClick([{ type: 'pointerdown', onTarget: true }], { deadlineMs: 1000, ...fault });
    assert.equal(result.clicks, 0, `${state}: a click was dispatched`);
    assert.match(result.outcome, /^rejected: Timeout 1000ms exceeded/, state);
    assert.ok(result.log.some((line) => line.includes(`element is not ${state}`)), `${state}: the call log names the missing state`);
    // As in upstream's own check, a target that has not settled is refused before the other states are read.
    assert.deepEqual(result.statesAsked[0], state === 'stable' ? undefined : ['visible', 'enabled', 'non-inert'], state);
    assert.deepEqual(result.verdicts, [], `${state}: nothing was sent, so no delivery is reported`);
  }
});

// Upstream's checkElementStates settles first and reads the other states afterwards, and the hit-target interceptor does not
// look at them again. So the states are read after the host's wait too: a control that stays put but turns disabled during that
// wait (validating, say) is not clicked. The wait is what flips the state here. The always-enabled control is the same path.
test('a stationary target that turns disabled during the settling wait is not clicked; one that stays enabled is, once', async () => {
  const run = async (turnsDisabled) => {
    const hooks = createPointerHooks();
    patchedInternals().tools.setPointerActionHooks(hooks);
    let disabled = false;
    const { Frame, frame, counts } = clickFixture({ probe: [{ type: 'pointerdown', onTarget: true }], missingState: () => (disabled ? 'enabled' : undefined) });
    const progress = fakeProgress(500);
    const settle = progress.wait;
    progress.wait = async (ms) => {
      await settle(ms);
      disabled = turnsDisabled;
    };
    let outcome = 'resolved';
    await Frame.prototype.click.call(frame, progress, { selector: '#target', strict: true }, { timeout: 500 }).catch((error) => {
      outcome = String(error.message).split('\n')[0];
    });
    return { counts, outcome, log: progress.logs, verdicts: hooks.deliveriesSince(0).map((entry) => entry.verdict) };
  };
  const enabled = await run(false);
  assert.deepEqual([enabled.counts.clicks, enabled.outcome, enabled.verdicts], [1, 'resolved', ['confirmed']]);
  assert.deepEqual(enabled.counts.io, ['box', 'box', 'states'], 'the states are read after the two samples of the box');
  assert.deepEqual(enabled.counts.statesAsked, [['visible', 'enabled', 'non-inert']]);
  const disabled = await run(true);
  assert.equal(disabled.counts.clicks, 0, 'the click went out to a disabled target');
  assert.match(disabled.outcome, /^Timeout 500ms exceeded/);
  assert.ok(disabled.log.includes('  element is not enabled'), 'the call log names the missing state');
  assert.deepEqual(disabled.verdicts, [], 'nothing was sent, so no delivery is reported');
});

// Playwright keeps one progress object for a call across its retries, and retries a check box after its click. A second
// click in the same call is refused by the hooks, so a state that waits forever ends the call by timeout, not by clicking again.
test('a second click in the same call is not sent: the call waits on it and ends by timeout', async () => {
  const hooks = createPointerHooks();
  patchedInternals().tools.setPointerActionHooks(hooks);
  const { Frame, frame, counts } = clickFixture({ probe: [{ type: 'pointerdown', onTarget: true }] });
  const progress = fakeProgress(1000);
  const target = { selector: '#target', strict: true };
  await Frame.prototype.click.call(frame, progress, target, { timeout: 5000 });
  assert.equal(counts.clicks, 1);
  await assert.rejects(Frame.prototype.click.call(frame, progress, target, { timeout: 5000 }), /Timeout 1000ms exceeded/);
  assert.equal(counts.clicks, 1, 'the second click went out');
  assert.ok(progress.logs.some((line) => line.includes(NOT_SENT_AGAIN)), 'the call log says why');
});
