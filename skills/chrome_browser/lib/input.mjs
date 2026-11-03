// Background-safe input for the patched Playwright pointer actions.
//
// Playwright's own "stable" check waits for the page's requestAnimationFrame, which never
// runs in a hidden tab, so a click there times out. These hooks keep every other upstream
// actionability check (visible, enabled, non-inert, scrolling, clickable point, hit target)
// and replace only that wait: the element's box is read twice, a fixed interval apart on the
// server's own clock. A box that moved means "not stable", and Playwright retries until its
// action timeout; nothing is dispatched to a moving, covered, disabled or hidden target. As in
// upstream's own check, the other states are read after that wait, so a target that turned
// disabled during it is not clicked.
//
// After a click, the page is asked whether a trusted pointer event actually reached the
// target. Chrome can acknowledge input to a hidden renderer that never delivers it; that case
// is reported (BROWSER_INPUT_UNCONFIRMED) and the click is never sent again. The verdict is
// recorded, not thrown: Playwright's locator retry (Frame.retryWithProgressAndTimeouts) treats
// any error that is not its own non-retriable kind as "try again" and would click a second
// time. A probe that could not be installed or read back is recorded too, so the result can
// say the click went out once with its delivery unverified instead of passing as confirmed.
//
// A click goes out at most once per Playwright call. Playwright keeps one progress object for a
// call across all its retries, and ElementHandle._setChecked does retry after a click: when the
// state read right after it finds the element detached (a plain, retriable error), it looks the
// target up again and clicks once more if the new element still has the old state. With a
// selector target that is a second click whose first effect is unknown. The stability hook,
// which runs before every dispatch, refuses it by reporting a state Playwright waits on; the call
// then ends with Playwright's own timeout error and its call log says why. It cannot return an
// unknown result instead: Playwright's assertDone is empty, so the call would end as a success.
//
// Design source: the background input preparation, real-time geometry sampling and
// "never replay an unknown input" rules come from the chrome_browser contribution by
// Andrei Kaznacheev (kazzand) in OuroborosHub PR #119 (extension/service_worker.js:
// prepareInputFrame, stablePoint, pointerClick). The code here is a re-implementation for
// Playwright's ElementHandle, not a copy of that file.

export const STABLE_GAP_MS = 60;
export const FRAME_PREP_TIMEOUT_MS = 8_000;
export const PROBED_ACTIONS = new Set(['click', 'dblclick']);
export const PROBE_EVENT_TYPES = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'dblclick', 'auxclick', 'contextmenu'];
// Delivery verdicts: only "confirmed" means the page saw a trusted event at the target.
export const DELIVERY = Object.freeze({
  confirmed: 'confirmed',
  denied: 'denied',
  probeUnavailable: 'probe_unavailable',
  probeUnreadable: 'probe_unreadable',
});
const DELIVERY_LOG_SIZE = 50;
// Logged by Playwright as "element is not <this>" on each retry it then waits through.
export const NOT_SENT_AGAIN = 'clicked again in this call: a click already went out and its effect is unknown, so it is not repeated';

// Same comparison as Playwright's injected _checkElementIsStable: exact equality.
export function sameRect(a, b) {
  return a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height;
}

// Runs in the page's utility world (serialized by Playwright); it may not use closure state.
function measureInPage([injected, node]) {
  const element = injected.retarget(node, 'no-follow-label');
  if (!element || !element.isConnected)
    return 'error:notconnected';
  const rect = element.getBoundingClientRect();
  return { x: rect.left, y: rect.top, width: rect.width, height: rect.height };
}

// Runs in the page's utility world. Records trusted pointer events and whether each one's
// composed path contained the target, until stop() is called.
function installProbeInPage([injected, node, types]) {
  const element = injected.retarget(node, 'button-link') || node;
  const seen = [];
  const listener = (event) => {
    if (!event.isTrusted)
      return;
    const path = typeof event.composedPath === 'function' ? event.composedPath() : [];
    seen.push({ type: event.type, onTarget: path.includes(element) });
  };
  for (const type of types)
    injected.window.addEventListener(type, listener, true);
  let stopped = false;
  return {
    stop() {
      if (!stopped) {
        stopped = true;
        for (const type of types)
          injected.window.removeEventListener(type, listener, true);
      }
      return seen;
    },
  };
}

export function unconfirmedInputMessage(actionName, seen) {
  const elsewhere = seen.length ? ' (trusted events were seen elsewhere on the page)' : '';
  return `BROWSER_INPUT_UNCONFIRMED: Chrome accepted the ${actionName}, but the page recorded no trusted pointer event at the target${elsewhere}. It was not sent again. Take a fresh browser_snapshot before deciding what to do next.`;
}

export function unverifiedInputMessage(actionName, verdict) {
  const why = verdict === DELIVERY.probeUnavailable
    ? 'the page did not accept the delivery check before it'
    : 'the delivery check could not be read back after it (the document may have changed, for example because the click navigated)';
  return `BROWSER_INPUT_NOT_VERIFIED: the ${actionName} was sent once and not repeated, but whether it reached the target is unknown: ${why}. Take a fresh browser_snapshot to see its effect before deciding what to do next.`;
}

export function createPointerHooks({ gapMs = STABLE_GAP_MS } = {}) {
  const deliveries = [];
  let deliverySeq = 0;
  // Progress objects (one per Playwright call) in which a probed click already went out.
  const sent = new WeakSet();
  const record = (action, verdict, seen = []) => {
    deliverySeq += 1;
    deliveries.push({ seq: deliverySeq, action: action.name, verdict, seen });
    if (deliveries.length > DELIVERY_LOG_SIZE)
      deliveries.shift();
  };
  return {
    // For the tool result: verdicts recorded after a mark taken before the call.
    deliveryMark: () => deliverySeq,
    deliveriesSince: (mark) => deliveries.filter((entry) => entry.seq > mark),

    async stability(handle, progress, action) {
      if (action && PROBED_ACTIONS.has(action.name) && sent.has(progress))
        return { missingState: NOT_SENT_AGAIN };
      const measure = () => progress.race(handle.evaluateInUtility(measureInPage, {}));
      const first = await measure();
      if (typeof first === 'string')
        return first;
      progress.log(`  sampling the element's box twice, ${gapMs}ms apart on the server clock`);
      await progress.wait(gapMs);
      const second = await measure();
      if (typeof second === 'string')
        return second;
      if (!sameRect(first, second))
        return { missingState: 'stable' };
      return 'done';
    },

    async beforePerform(handle, progress, action) {
      if (!PROBED_ACTIONS.has(action.name))
        return undefined;
      let probe;
      try {
        probe = await progress.race(handle._evaluateHandleInUtility(installProbeInPage, PROBE_EVENT_TYPES));
      } catch {
        probe = undefined;
      }
      if (!probe || typeof probe !== 'object' || !probe._objectId) {
        // The click still goes out once. It is recorded as unverified in afterPerform, which the
        // patched code reaches only after the click was performed (an aborted action sends nothing).
        progress.log('  delivery probe unavailable; the click proceeds without it');
        return { handle: null, released: false };
      }
      return { handle: probe, released: false };
    },

    // Never throws for a verdict: Playwright would retry a thrown click (see the header). Reached
    // only once the click went out and upstream's hit-target check passed; a click that check
    // caught was blocked in the page and Playwright may still retry it.
    async afterPerform(handle, progress, action, probe) {
      sent.add(progress);
      if (!probe.handle) {
        record(action, DELIVERY.probeUnavailable);
        return;
      }
      let seen;
      try {
        seen = await progress.race(probe.handle.evaluate((p) => p.stop()));
      } catch {
        seen = undefined;
      }
      if (!Array.isArray(seen)) {
        // The document may have changed under the click: delivery can be neither shown nor denied.
        progress.log('  delivery probe unreadable after the input (the document may have changed)');
        record(action, DELIVERY.probeUnreadable);
        return;
      }
      if (seen.some((event) => event.onTarget)) {
        progress.log(`  trusted ${[...new Set(seen.filter((event) => event.onTarget).map((event) => event.type))].join(', ')} reached the target`);
        record(action, DELIVERY.confirmed, seen);
        return;
      }
      progress.log('  no trusted pointer event reached the target; the input is not sent again');
      record(action, DELIVERY.denied, seen);
    },

    releaseProbe(probe) {
      if (!probe || probe.released)
        return;
      probe.released = true;
      if (!probe.handle)
        return;
      Promise.resolve()
          .then(() => probe.handle.evaluate((p) => p.stop()))
          .catch(() => {})
          .finally(() => {
            try {
              probe.handle.dispose();
            } catch {
              // already gone with its context
            }
          });
    },
  };
}

export class InputNotReadyError extends Error {
  constructor(detail) {
    super(`BROWSER_INPUT_NOT_READY: Chrome produced no frame for this tab${detail ? ` (${detail})` : ''}; no input was sent.`);
    this.name = 'InputNotReadyError';
    this.code = 'BROWSER_INPUT_NOT_READY';
  }
}

// Before the first input into each document, capture one frame of the target tab and
// discard the pixels. A newly loaded hidden renderer in headed Chrome can otherwise accept
// input without delivering DOM events (observed by the PR #119 author; not reproduced in
// headless Chrome). This is preparation, not proof: delivery is checked separately.
export class InputFramePreparer {
  constructor({ timeoutMs = FRAME_PREP_TIMEOUT_MS } = {}) {
    this._timeoutMs = timeoutMs;
    this._documents = new WeakMap();
    this._prepared = new WeakMap();
  }

  _documentSeq(page) {
    if (!this._documents.has(page)) {
      this._documents.set(page, 0);
      page.on('framenavigated', (frame) => {
        if (frame === page.mainFrame())
          this._documents.set(page, this._documents.get(page) + 1);
      });
    }
    return this._documents.get(page);
  }

  async prepare(page) {
    const seq = this._documentSeq(page);
    if (this._prepared.get(page) === seq)
      return { prepared: false };
    const started = Date.now();
    let pixels;
    try {
      pixels = await page.screenshot({ type: 'png', caret: 'initial', scale: 'css', timeout: this._timeoutMs });
    } catch (error) {
      throw new InputNotReadyError(error?.name === 'TimeoutError' ? `no frame within ${this._timeoutMs} ms` : 'capture failed');
    }
    if (!pixels || !pixels.length)
      throw new InputNotReadyError('empty capture');
    this._prepared.set(page, seq);
    return { prepared: true, elapsed_ms: Date.now() - started };
  }
}
