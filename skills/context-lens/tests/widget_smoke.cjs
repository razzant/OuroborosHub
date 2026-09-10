/* Executes widget.js against a minimal DOM stub and asserts what the card does.
 *
 * Not a browser: layout, painting and real events are absent, and the canvas is
 * a recording stub. What this does prove is that every render path actually
 * runs — first paint, loaded data, filtering, paging, selection, trajectory,
 * the superseded-answer guard and disposal — against the exact payload shape
 * lens_core produces. Run by tests/test_widget_contract.py when Node exists.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const ROOT = path.resolve(__dirname, '..');

// ----------------------------------------------------------------- DOM stub

/* The card width every stub canvas reports. Mutable so a check can re-render
 * the same card at a narrow viewport, which is where chart labels collide. */
let cardWidth = 620;

/* A deliberately crude advance width: the chart draws everything at 12px, and
 * what the collision checks need is a width that grows with the string, not the
 * real metrics of a font this process does not have. */
const CHAR_WIDTH = 6.6;
function advanceWidth(text) { return String(text).length * CHAR_WIDTH; }

/* The horizontal extent a recorded fillText call actually covers, given the
 * textAlign that was in force when it ran. */
function textBox(call) {
    const w = advanceWidth(call.text);
    if (call.align === 'right') return { left: call.x - w, right: call.x };
    if (call.align === 'center') return { left: call.x - w / 2, right: call.x + w / 2 };
    return { left: call.x, right: call.x + w };
}

class Node {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.childNodes = [];
        this.attributes = {};
        this.style = {};
        this.listeners = {};
        this.className = '';
        this.hidden = false;
        this.disabled = false;
        this.isConnected = true;
        this._text = '';
        this.classList = {
            add: (name) => { this.className = (this.className + ' ' + name).trim(); },
            contains: (name) => this.className.split(/\s+/).indexOf(name) >= 0,
        };
    }
    set textContent(value) { this._text = String(value); this.childNodes = []; }
    get textContent() {
        return this.childNodes.length
            ? this.childNodes.map((child) => child.textContent).join('')
            : this._text;
    }
    appendChild(child) { this.childNodes.push(child); return child; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
    addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
    removeEventListener(type, handler) {
        const bucket = this.listeners[type] || [];
        const at = bucket.indexOf(handler);
        if (at >= 0) bucket.splice(at, 1);
    }
    dispatch(type, event) { (this.listeners[type] || []).slice().forEach((fn) => fn(event || {})); }
    contains(other) {
        if (other === this) return true;
        return this.childNodes.some((child) => child.contains && child.contains(other));
    }
    focus() { doc.activeElement = this; }
    getBoundingClientRect() { return { left: 0, top: 0, width: cardWidth, height: 196 }; }
    get clientWidth() { return cardWidth; }
    get clientHeight() { return 196; }
    getContext() {
        this._ctx = this._ctx || {
            texts: [], calls: [], arcs: [], strokes: 0,
            textAlign: 'start', textBaseline: 'alphabetic',
            // `calls` holds the LAST draw only, so a collision check reads one
            // frame; `texts` keeps accumulating, as the older checks expect.
            setTransform() {}, clearRect() { this.calls = []; },
            beginPath() {}, closePath() {},
            moveTo() {}, lineTo() {}, save() {}, restore() {}, setLineDash() {},
            fill() {}, measureText: (text) => ({ width: advanceWidth(text) }),
            arc(x, y, r) { this.arcs.push({ x, y, r }); },
            stroke() { this.strokes += 1; },
            fillText(text, x, y) {
                this.texts.push(String(text));
                this.calls.push({ text: String(text), x: x, y: y, align: this.textAlign });
            },
        };
        return this._ctx;
    }
    walk(visit) {
        visit(this);
        this.childNodes.forEach((child) => child.walk && child.walk(visit));
    }
    querySelector(selector) {
        const match = /^\[data-focus="(.*)"\]$/.exec(selector);
        if (!match) throw new Error('stub supports only [data-focus=…]: ' + selector);
        let found = null;
        this.walk((node) => { if (!found && node.getAttribute('data-focus') === match[1]) found = node; });
        return found;
    }
    findAll(predicate) {
        const out = [];
        this.walk((node) => { if (predicate(node)) out.push(node); });
        return out;
    }
    text() {
        const parts = [];
        this.walk((node) => { if (node._text) parts.push(node._text); });
        return parts.join(' ');
    }
}

const doc = {
    head: new Node('head'),
    body: new Node('body'),
    activeElement: null,
    visibilityState: 'visible',
    createElement: (tag) => new Node(tag),
    getElementById(id) {
        let found = null;
        this.body.walk((node) => { if (!found && node.id === id) found = node; });
        return found;
    },
};

// ------------------------------------------------------------- fixtures

function point(index, overrides) {
    return Object.assign({
        id: 'a-' + String(index).padStart(16, '0'),
        seq: index,
        t: 1788775200000 + index * 60000,
        state: 'settled',
        states: ['reserved', 'dispatched', 'settled'],
        model: index % 3 === 0 ? 'vendor/model-b' : 'vendor/model-a',
        provider: 'openrouter',
        category: index % 2 === 0 ? 'task' : 'skill_review',
        source: 'llm',
        task: 't-' + String(index % 2).repeat(12),
        root: 't-000000000000',
        parent: null,
        prompt_tokens: 10000 + index * 900,
        completion_tokens: 500,
        cached_tokens: 4000,
        cache_write_tokens: null,
        mode: index % 4 === 0 ? null : (index % 2 ? 'low' : 'max'),
        profile: 'owner_max',
        basis: 'fresh_route_usage',
        target_total_tokens: 180000,
        capacity_total_tokens: 200000,
        target_miss: null,
        auto_pass: null,
        elapsed_sec: 4.25,
    }, overrides || {});
}

const POINTS = [];
for (let i = 1; i <= 24; i += 1) POINTS.push(point(i));
POINTS.push(point(25, { state: 'reserved', prompt_tokens: null, states: ['reserved'] }));
POINTS.push(point(26, { state: 'settled', prompt_tokens: null }));
POINTS.push(point(27, { t: null }));

const DATA = {
    ok: true, available: true,
    window: {
        first_seq: 1, last_seq: 27, lines_read: 81, malformed_lines: 2,
        omitted_prefix_bytes: 4096, pending_tail_bytes: 0, evicted_records: 0,
        rotations_observed: 1, max_bytes_per_refresh: 4194304, max_records: 5000,
        compaction_epoch: 3, compaction_folded_attempts: 120,
    },
    counters: {
        physical_attempts: 27, measured: 24, settled_without_tokens: 1, in_flight: 1,
        by_state: { reserved: 1, dispatched: 0, settled: 26, unresolved: 0, released: 0 },
        excluded: {
            baseline_rows: 3, baseline_header_rows: 1, baseline_group_rows: 2,
            folded_attempts: 120, folded_attempts_from_headers: 120,
            folded_attempts_from_groups: 0, baselines_without_header: 0,
            subscription_sessions: 2, external_unmetered: 1, legacy_rows: 0,
            unknown_kind: 1, attempts_without_state: 1,
        },
    },
    facets: {
        models: ['vendor/model-a', 'vendor/model-b'],
        categories: ['skill_review', 'task'],
        sources: ['llm'],
        modes: ['max', 'low', 'unknown'],
    },
    points: POINTS,
    points_omitted: 5,
};

const TRAJECTORY = {
    ok: true, available: true, task: 't-111111111111', root: 't-000000000000',
    groups: [{
        model: 'vendor/model-a', category: 'skill_review', joined: true,
        points: [POINTS[0], POINTS[2], POINTS[4]],
    }],
    related: [{
        task: 't-000000000000', model: 'vendor/model-b', category: 'task', joined: false,
        points: [POINTS[5]],
    }],
};

/* The same shape over a span of days. `axisTime` switches to the long
 * "Sep 8, 14:42" form above 24h, which is what makes the two ends of the small
 * trajectory chart wide enough to meet on a narrow card. */
const DAY = 86400000;
const WIDE_SPAN_TRAJECTORY = {
    ok: true, available: true, task: 't-111111111111', root: 't-000000000000',
    groups: [{
        model: 'vendor/model-a', category: 'skill_review', joined: true,
        points: [
            point(1, { t: 1788775200000 }),
            point(2, { t: 1788775200000 + DAY }),
            point(3, { t: 1788775200000 + 3 * DAY }),
        ],
    }],
    related: [],
};

// --------------------------------------------------------------- harness

const calls = [];
let trajectoryResolvers = [];
let trajectoryPayload = TRAJECTORY;
let disposeHook = null;
const frames = [];

/* The overview answer resolves immediately by default. Turning `deferData` on
 * parks every further overview answer in `dataResolvers`, so a test can land an
 * older answer AFTER a newer click and see which one the card believes. An
 * aborted request is counted but still delivered on purpose: that proves the
 * generation guard, not just the abort, refuses the stale payload. */
let deferData = false;
const dataResolvers = [];
let dataAborts = 0;

function horizonOf(url) {
    const found = /[?&]horizon=([^&]*)/.exec(url);
    return found ? decodeURIComponent(found[1]) : '';
}

function dataFor(url) {
    const selected = horizonOf(url);
    const times = POINTS.map((p) => p.t).filter(Boolean);
    return Object.assign({}, DATA, {
        horizon: {
            selected: selected,
            options: ['1h', '6h', '24h', '7d', 'available'],
            span_ms: selected === 'available' ? null : 3600000,
            now_ms: 1788778800000,
            cutoff_ms: selected === 'available' ? null : 1788775200000,
            observed_from_ms: Math.min.apply(null, times),
            observed_to_ms: Math.max.apply(null, times),
            selected_from_ms: Math.min.apply(null, times),
            selected_to_ms: Math.max.apply(null, times),
            records_retained: POINTS.length,
            records_selected: POINTS.length,
            excluded_older_than_cutoff: 0,
            unknown_timestamp: 1,
            unknown_timestamp_kept: 1,
            ahead_of_anchor: 0,
            covers_selected_span: false,
            history_truncated_by_source: true,
        },
    });
}

const sandbox = {
    console,
    Math, JSON, Date, Set, Map, Promise, Error, String, Number, Boolean, Array, Object,
    isFinite, parseInt, parseFloat,
    AbortController,
    setTimeout, clearTimeout,
    setInterval: (fn) => ({ fn }),         // the poll must never fire under test
    clearInterval: () => {},
    requestAnimationFrame(fn) { frames.push(fn); return frames.length; },
    cancelAnimationFrame(id) { frames[id - 1] = null; },
    ResizeObserver: function ResizeObserverStub(handler) {
        this.handler = handler;
        this.observe = function () {};
        this.disconnect = function () {};
    },
    document: doc,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.devicePixelRatio = 2;
sandbox.OuroborosWidget = {
    fetch(url, options) {
        calls.push(url);
        if (url.indexOf('trajectory?') >= 0) {
            const payload = trajectoryPayload;
            return new Promise((resolve) => {
                trajectoryResolvers.push(() => resolve({ ok: true, json: () => Promise.resolve(payload) }));
            });
        }
        if (deferData) {
            const signal = options && options.signal;
            if (signal) signal.addEventListener('abort', () => { dataAborts += 1; });
            const payload = dataFor(url);
            return new Promise((resolve) => {
                dataResolvers.push(() => resolve({ ok: true, json: () => Promise.resolve(payload) }));
            });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve(DATA) });
    },
};
sandbox.__ouroWidgetOnDispose = (fn) => { disposeHook = fn; };

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'widget.js'), 'utf8'), sandbox,
    { filename: 'widget.js' });

function flushFrames() {
    const pending = frames.splice(0, frames.length);
    pending.forEach((fn) => { if (fn) fn(); });
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function root() { return doc.getElementById('root'); }
function rows() { return root().findAll((node) => node.className === 'row' || node.className.startsWith('row ')); }
function button(label) {
    return root().findAll((node) => node.tagName === 'BUTTON' && node.textContent.indexOf(label) >= 0)[0];
}
function horizonButton(key) {
    const found = root().querySelector('[data-focus="horizon-' + key + '"]');
    assert.ok(found, 'horizon button ' + key + ' exists');
    return found;
}
function pressedHorizon() {
    const on = root().findAll((node) => {
        const focus = node.getAttribute('data-focus');
        return focus && focus.indexOf('horizon-') === 0 && node.getAttribute('aria-pressed') === 'true';
    });
    assert.strictEqual(on.length, 1, 'exactly one horizon is pressed');
    return on[0].getAttribute('data-focus').slice('horizon-'.length);
}

// ------------------------------------------------------------------ checks

(async function main() {
    // First paint happens before any answer arrives.
    assert.ok(root(), '#root is created');
    assert.ok(root().text().indexOf('Loading telemetry') >= 0, 'first paint says it is loading');

    await tick();
    await tick();
    flushFrames();

    assert.deepStrictEqual(calls, ['/api/extensions/context-lens/data?limit=1500&horizon=available']);
    assert.strictEqual(rows().length, 6, 'the list opens compact');
    assert.ok(root().text().indexOf('27 match the filter') >= 0, 'the whole filtered population is stated');

    // The chart drew one dot per measured point and no more.
    const canvas = root().findAll((node) => node.tagName === 'CANVAS')[0];
    assert.strictEqual(canvas._ctx.arcs.length, 24, 'only settled, sized, timed points are drawn');
    assert.ok(canvas.getAttribute('aria-label').indexOf('24 measured requests') >= 0);
    assert.ok(canvas.getAttribute('aria-label').indexOf('27') >= 0, 'the label names the reachable total');
    assert.ok(canvas._ctx.texts.indexOf('input tokens') >= 0, 'the axis is labelled');

    // Tiles read from the filtered view: 24 of 27 measured.
    const tiles = root().findAll((node) => node.className === 'tile');
    assert.strictEqual(tiles.length, 4);
    assert.ok(tiles[3].text().indexOf('89%') >= 0, 'coverage is 24/27 of THIS view, not the counters');
    assert.ok(tiles[0].text().indexOf('Typical input') >= 0);

    // Paging reaches every filtered request, then folds back.
    button('Show more').dispatch('click');
    flushFrames();
    assert.strictEqual(rows().length, 18, 'Show more pages by a step');
    button('Show more').dispatch('click');
    flushFrames();
    assert.strictEqual(rows().length, 27, 'every filtered request is reachable as text');
    assert.strictEqual(button('Show more'), undefined, 'the control disappears at the end');
    button('Show less').dispatch('click');
    flushFrames();
    assert.strictEqual(rows().length, 6);

    // Selecting a row shows the detail and asks for that task's trajectory.
    const before = calls.length;
    rows()[0].dispatch('click');
    assert.strictEqual(calls.length, before + 1, 'selection loads the trajectory promptly');
    assert.ok(calls[calls.length - 1].indexOf('trajectory?task=t-') >= 0);
    assert.ok(root().text().indexOf('reported input tokens') >= 0, 'the lead is the input size');
    assert.ok(root().text().indexOf('Technical detail') >= 0, 'internals stay behind a disclosure');

    // A second press supersedes the first answer: only the newest is painted.
    const reload = button('Reload');
    assert.ok(!reload, 'no trajectory is painted while it is still loading');
    rows()[1].dispatch('click');                       // re-selects, bumping the guard
    const stale = trajectoryResolvers.shift();
    stale();                                           // the FIRST answer lands late
    await tick();
    assert.ok(!root().text().includes('Another task in the same tree'),
        'a superseded trajectory answer is discarded');
    trajectoryResolvers.shift()();                     // the newest answer lands
    await tick();
    flushFrames();
    assert.ok(root().text().indexOf('This task over time') >= 0, 'the current trajectory is painted');
    assert.ok(root().text().indexOf('Another task in the same tree') >= 0, 'related runs are named, not joined');

    // Focus survives the rebuild that a filter change causes.
    const picker = root().querySelector('[data-focus="filter-model"]');
    doc.activeElement = picker;
    picker.value = 'vendor/model-b';
    picker.dispatch('change');
    flushFrames();
    assert.strictEqual(doc.activeElement.getAttribute('data-focus'), 'filter-model',
        'the keyboard stays on the control that was just used');
    assert.ok(rows().length > 0 && rows().length <= 6, 'the list folds back to compact on a filter change');
    root().findAll((node) => node.className === 'row-model')
        .forEach((node) => assert.strictEqual(node.textContent, 'vendor/model-b'));

    // Unknown mode is offered even though only max/low are in the filtered view.
    const modeOptions = root().querySelector('[data-focus="filter-mode"]')
        .childNodes.map((option) => option.value);
    assert.ok(modeOptions.indexOf('unknown') >= 0, 'Unknown is a permanent option');

    // A horizon click made while an answer is in flight is what wins.
    deferData = true;
    const beforeHorizon = calls.length;
    horizonButton('1h').dispatch('click');
    flushFrames();
    assert.strictEqual(calls.length, beforeHorizon + 1, 'the click asks for the new horizon');
    assert.ok(calls[calls.length - 1].indexOf('horizon=1h') >= 0);
    assert.strictEqual(pressedHorizon(), '1h', 'the control follows the click at once');

    // Second click, before the first answer has landed at all.
    horizonButton('7d').dispatch('click');
    flushFrames();
    assert.strictEqual(dataAborts, 1, 'the superseded overview request is aborted');
    assert.strictEqual(calls.length, beforeHorizon + 2, 'the newer click issues its own request');
    assert.ok(calls[calls.length - 1].indexOf('horizon=7d') >= 0,
        'the last requested URL follows the user, not the request in flight');

    // The older 1h answer lands late anyway; it must change nothing. A repaint
    // driven by an unrelated control shows whatever it did write to state.
    dataResolvers.shift()();
    await tick();
    await tick();
    flushFrames();
    const repaint = root().querySelector('[data-focus="filter-model"]');
    repaint.value = 'all';
    repaint.dispatch('change');
    flushFrames();
    assert.strictEqual(pressedHorizon(), '7d', 'a superseded answer cannot revert the click');
    assert.ok(root().text().indexOf('Requested the last hour') < 0,
        'the superseded payload is not adopted');
    assert.ok(calls[calls.length - 1].indexOf('horizon=7d') >= 0, 'and it re-requests nothing');

    // The answer the user actually asked for is adopted.
    dataResolvers.shift()();
    await tick();
    await tick();
    flushFrames();
    assert.strictEqual(pressedHorizon(), '7d');
    assert.ok(root().text().indexOf('Requested the last 7 days') >= 0,
        'the coverage line describes the horizon that was clicked');
    assert.strictEqual(dataResolvers.length, 0, 'no overview request was left stacked');
    deferData = false;

    // No two pieces of chart text overlap, on a wide card or a narrow one.
    //
    // A canvas clips nothing and reports no layout, so two strings drawn near
    // the same spot simply print over each other — which is exactly how the
    // y-axis caption met the topmost tick label, and how the last two time
    // labels met each other and the right-hand caption. Every fillText call of
    // one frame is compared against every other: two of them collide when their
    // measured boxes overlap horizontally and their baselines are closer
    // together than the 12px line the chart draws at.
    function assertNoOverlappingText(canvas, where) {
        const drawn = canvas._ctx.calls;
        assert.ok(drawn.length > 0, where + ' drew some text');
        for (let a = 0; a < drawn.length; a += 1) {
            for (let b = a + 1; b < drawn.length; b += 1) {
                if (Math.abs(drawn[a].y - drawn[b].y) >= 12) continue;
                const boxA = textBox(drawn[a]);
                const boxB = textBox(drawn[b]);
                assert.ok(boxA.right <= boxB.left || boxB.right <= boxA.left,
                    where + ': "' + drawn[a].text + '" overlaps "' + drawn[b].text + '"');
            }
        }
    }

    // Repaints both charts at `width`: the overview, plus the trajectory that
    // selecting a request opens under it.
    async function paintCharts(width) {
        cardWidth = width;
        const control = root().querySelector('[data-focus="filter-model"]');
        control.value = 'all';
        control.dispatch('change');
        flushFrames();
        rows()[0].dispatch('click');
        trajectoryResolvers.shift()();
        await tick();
        flushFrames();
        const canvases = root().findAll((node) => node.tagName === 'CANVAS');
        assert.strictEqual(canvases.length, 2,
            'both the overview and the trajectory are painted at ' + width + 'px');
        return canvases;
    }

    const wide = await paintCharts(620);
    assertNoOverlappingText(wide[0], 'overview chart at 620px');
    assertNoOverlappingText(wide[1], 'trajectory chart at 620px');
    assert.ok(wide[0]._ctx.calls.some((call) => call.text === 'input tokens'),
        'the y axis keeps its caption');
    assert.ok(wide[0]._ctx.calls.some((call) => call.text === 'recorded time →'),
        'the x axis keeps its caption');

    const narrow = await paintCharts(320);
    assertNoOverlappingText(narrow[0], 'overview chart at 320px');
    assertNoOverlappingText(narrow[1], 'trajectory chart at 320px');
    const timeLabels = (canvas) => canvas._ctx.calls.filter((call) => call.align === 'center');
    assert.ok(timeLabels(narrow[0]).length >= 2,
        'the first and last time label are kept however narrow the card is');
    assert.ok(timeLabels(wide[0]).length >= timeLabels(narrow[0]).length,
        'a narrower card drops time labels, it never gains them');

    // The trajectory chart states the two ends of a multi-day span, where the
    // long date form is wide enough for the pair to meet on a narrow card.
    trajectoryPayload = WIDE_SPAN_TRAJECTORY;
    const spanning = await paintCharts(200);
    assertNoOverlappingText(spanning[1], 'multi-day trajectory at 200px');
    assert.ok(spanning[1]._ctx.calls.some((call) => /^[A-Z][a-z]{2} \d/.test(call.text)),
        'the trajectory still states when the span ends');
    trajectoryPayload = TRAJECTORY;
    cardWidth = 620;

    // Disposal cancels the pending frames and releases every listener.
    assert.ok(typeof disposeHook === 'function', 'a dispose hook is registered');
    disposeHook();
    const live = frames.filter(Boolean).length;
    assert.strictEqual(live, 0, 'no animation frame survives disposal');

    console.log('widget smoke: ok');
})().catch((error) => {
    console.error(error && error.stack ? error.stack : error);
    process.exit(1);
});
