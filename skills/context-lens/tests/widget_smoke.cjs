/* Executes widget.js against a minimal DOM stub and asserts what the card does.
 *
 * Not a browser: layout, painting and real events are absent, and the canvas is
 * a recording stub. What this does prove is that every render path actually
 * runs — first paint, a loaded answer, filtering, paging, selection with task
 * focus, a failed refresh over a good answer, the initial error, the three
 * empty states, a theme change, superseded answers, keyboard stepping, a hidden
 * poll and disposal — against the payload shape lens_core produces. Run by
 * tests/test_widget_contract.py when Node exists.
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
let cardHeight = 260;

/* A deliberately crude advance width: what the collision checks need is a width
 * that grows with the string, not the metrics of a font this process lacks. */
const CHAR_WIDTH = 6.6;
function advanceWidth(text) { return String(text).length * CHAR_WIDTH; }

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
        this.open = false;
        this.isConnected = true;
        this.dataset = {};
        this._text = '';
        this.classList = {
            add: (name) => { this.className = (this.className + ' ' + name).trim(); },
            contains: (name) => this.className.split(/\s+/).indexOf(name) >= 0,
        };
    }
    set textContent(value) {
        this.childNodes.forEach((child) => { child.isConnected = false; });
        this._text = String(value);
        this.childNodes = [];
    }
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
    getBoundingClientRect() { return { left: 0, top: 0, width: cardWidth, height: cardHeight }; }
    get clientWidth() { return cardWidth; }
    get clientHeight() { return cardHeight; }
    getContext() {
        this._ctx = this._ctx || {
            texts: [], calls: [], arcs: [], lines: 0, strokes: 0, frames: 0,
            textAlign: 'start', textBaseline: 'alphabetic', fillStyle: '', strokeStyle: '',
            // `calls`, `arcs` and `lines` hold the LAST draw only, so a check
            // reads one frame; `texts` keeps accumulating.
            setTransform() {},
            clearRect() { this.calls = []; this.arcs = []; this.lines = 0; this.frames += 1; },
            beginPath() {}, closePath() {},
            moveTo() {}, lineTo() { this.lines += 1; }, save() {}, restore() {}, setLineDash() {},
            fill() {}, strokeText() {}, measureText: (text) => ({ width: advanceWidth(text) }),
            arc(x, y, r) { this.arcs.push({ x, y, r, fill: this.fillStyle }); },
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
    documentElement: new Node('html'),
    activeElement: null,
    visibilityState: 'visible',
    listeners: {},
    createElement: (tag) => new Node(tag),
    addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); },
    removeEventListener(type, handler) {
        const bucket = this.listeners[type] || [];
        const at = bucket.indexOf(handler);
        if (at >= 0) bucket.splice(at, 1);
    },
    getElementById(id) {
        let found = null;
        this.body.walk((node) => { if (!found && node.id === id) found = node; });
        return found;
    },
};

// ------------------------------------------------------------- fixtures

const READ_AT = 1788778800000;

function point(index, overrides) {
    return Object.assign({
        id: 'a-' + String(index).padStart(16, '0'),
        t: READ_AT - 3600000 + index * 60000,
        state: 'settled',
        model: index % 3 === 0 ? 'vendor/model-b' : 'vendor/model-a',
        provider: 'openrouter',
        category: index % 2 === 0 ? 'task' : 'skill_review',
        source: 'agent.task',
        task: 't-' + String(index % 2).repeat(12),
        root: 'r-000000000000',
        parent: null,
        prompt_tokens: 10000 + index * 900,
        completion_tokens: 500,
        cached_tokens: 4000,
        cache_write_tokens: null,
        mode: index % 4 === 0 ? null : (index % 5 === 0 ? 'nano' : (index % 2 ? 'low' : 'max')),
        profile: 'owner_max',
        basis: 'fresh_route_usage',
        target_total_tokens: 180000,
        capacity_total_tokens: 200000,
        target_miss: null,
        auto_pass: null,
        late_receipt: false,
    }, overrides || {});
}

const POINTS = [];
for (let i = 1; i <= 24; i += 1) POINTS.push(point(i));
POINTS.push(point(25, { state: 'reserved', prompt_tokens: null }));
POINTS.push(point(26, { state: 'settled', prompt_tokens: null }));
POINTS.push(point(27, { prompt_tokens: 0 }));                 // an explicit zero IS measured
const MEASURED = 25;                                           // 24 sized + the zero

function payload(selected, overrides) {
    const span = { '1h': 3600000, '6h': 21600000, '24h': 86400000, '7d': 604800000 }[selected] || null;
    return Object.assign({
        ok: true, available: true,
        snapshot: { id: 's-000000000001' },
        source: {
            kind: 'usage_store', current: true, read_at_ms: READ_AT,
            newest_record_ms: READ_AT - 120000, schema_version: 1, lock_tier: 'enforced',
            read_ms: 40, transaction_ms: 31, sql_steps: 9000, metadata_read: 'sqlite_json', context_unread: 0,
            category_enumeration: 'summary_keys_plus_null_and_empty', legacy_category_coverage: 'not_guaranteed',
        },
        horizon: {
            selected: selected, options: ['1h', '6h', '24h', '7d', 'available'],
            span_ms: span, now_ms: READ_AT, cutoff_ms: span ? READ_AT - span : null,
            selection_complete: true, partial_reasons: [], covered_from_ms: span ? READ_AT - span : null,
            selection_scope: 'enumerated_categories',
            observed_from_ms: null, observed_to_ms: null,
            selected_from_ms: POINTS[0].t, selected_to_ms: POINTS[26].t,
            records_retained: null, records_selected: 30, excluded_older_than_cutoff: null,
            unknown_timestamp: 2, unknown_timestamp_kept: 0, unknown_timestamp_capped: false,
            ahead_of_anchor: 0, reaches_cutoff: null, history_truncated_by_source: false,
            attempts_selected: 27, points_sent: 27,
        },
        limits: { max_rows: 4000, max_points: 4000, max_categories: 64, merge_budget_ms: 400 },
        window: null,
        counters: {
            registered_attempts: 27, sent_attempts: 26, measured: 25, settled_without_tokens: 1,
            by_state: { reserved: 1, dispatched: 0, settled: 26, unresolved: 0, released: 0 },
            excluded: {
                baseline_rows: 1, baseline_header_rows: 0, baseline_group_rows: 1,
                folded_attempts: 40, folded_attempts_from_headers: 0, folded_attempts_from_groups: 0,
                folded_attempts_weighted: 40, baselines_without_header: 0,
                subscription_sessions: 2, external_unmetered: 0, legacy_rows: 0,
                unknown_kind: 0, attempts_without_state: 0,
            },
        },
        facets: {
            models: ['vendor/model-a', 'vendor/model-b'],
            categories: ['skill_review', 'task'],
            sources: ['agent.task'],
            modes: ['max', 'low', 'nano', 'unknown'],
        },
        points: POINTS,
        points_omitted: 0,
    }, overrides || {});
}

// --------------------------------------------------------------- harness

const calls = [];
let responder = null;           // (url, options) -> Promise<Response-like> for the next calls
let disposeHook = null;
let themeListener = null;
let themeUnsubscribed = 0;
let pollFn = null;
const frames = [];

function answer(body, status) {
    return Promise.resolve({ ok: !status || status < 400, status: status || 200, json: () => Promise.resolve(body) });
}

function horizonOf(url) {
    const found = /[?&]horizon=([^&]*)/.exec(url);
    return found ? decodeURIComponent(found[1]) : '';
}

/* Parked answers: a check releases them in any order to land an older answer
 * AFTER a newer click and see which one the card believes. An aborted request
 * is counted but still delivered on purpose: that proves the generation guard,
 * not just the abort, refuses the stale payload. */
const parked = [];
let aborts = 0;
function park(url, options) {
    const signal = options && options.signal;
    if (signal) signal.addEventListener('abort', () => { aborts += 1; });
    return new Promise((resolve) => {
        parked.push(() => resolve({ ok: true, status: 200, json: () => Promise.resolve(payload(horizonOf(url))) }));
    });
}

const sandbox = {
    console,
    Math, JSON, Date, Set, Map, Promise, Error, String, Number, Boolean, Array, Object,
    isFinite, parseInt, parseFloat,
    AbortController,
    setTimeout, clearTimeout,
    setInterval: (fn) => { pollFn = fn; return { fn }; },     // the poll fires only when a check calls it
    clearInterval: () => { pollFn = null; },
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
        assert.ok(options && options.signal, 'every request carries an AbortSignal');
        assert.strictEqual(options.timeoutMs, 20000, 'every request carries the bridge timeout');
        return responder(url, options);
    },
    onTheme(callback) {
        themeListener = callback;
        callback('dark');
        return () => { themeUnsubscribed += 1; themeListener = null; };
    },
};
sandbox.__ouroWidgetOnDispose = (fn) => { disposeHook = fn; };

responder = (url) => answer(payload(horizonOf(url)));

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'widget.js'), 'utf8'), sandbox, { filename: 'widget.js' });

function flushFrames() {
    const pending = frames.splice(0, frames.length);
    pending.forEach((fn) => { if (fn) fn(); });
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function settle() { await tick(); await tick(); await tick(); flushFrames(); }

function root() { return doc.getElementById('root'); }
function text() { return root().text(); }
function rows() { return root().findAll((node) => node.className === 'row' || node.className.startsWith('row ')); }
function canvas() { return root().findAll((node) => node.tagName === 'CANVAS')[0]; }
function button(label) {
    return root().findAll((node) => node.tagName === 'BUTTON' && node.textContent.indexOf(label) >= 0)[0];
}
function disclosure(key) {
    return root().findAll((node) => node.tagName === 'DETAILS' && node.getAttribute('data-open-key') === key)[0];
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
function statusText() { return root().findAll((node) => node.className === 'status-text')[0].textContent; }
function setFilter(key, value) {
    const picker = root().querySelector('[data-focus="filter-' + key + '"]');
    picker.value = value;
    picker.dispatch('change');
    flushFrames();
}

function assertNoOverlappingText(target, where) {
    const drawn = target._ctx.calls;
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

// ------------------------------------------------------------------ checks

(async function main() {
    // First paint: before any answer, and the theme was applied without a rebuild.
    assert.ok(root(), '#root is created');
    assert.ok(text().indexOf('Reading usage') >= 0, 'first paint says it is reading');
    assert.strictEqual(doc.documentElement.dataset.theme, 'dark', 'the bootstrap theme is applied');

    await settle();
    assert.deepStrictEqual(calls, ['/api/extensions/context-lens/data?horizon=24h&limit=4000']);

    // Chart first: no big in-frame title, no KPI tiles, one status line.
    assert.strictEqual(root().findAll((n) => /^H[1-3]$/.test(n.tagName)).length, 0, 'no duplicate in-frame title');
    assert.strictEqual(root().findAll((n) => n.className === 'tile').length, 0, 'no KPI tiles');
    assert.ok(statusText().indexOf('Usage store') >= 0, 'the source is named');
    assert.ok(statusText().indexOf('read ') >= 0, 'the read time is stated');
    assert.ok(statusText().indexOf('2 min before this read') >= 0, 'newest record is stated relative to the read');
    assert.ok(text().indexOf('Last 24 hours') >= 0, 'the coverage line names the horizon');
    assert.ok(text().indexOf('27 requests in view, 25 with a measured input') >= 0, 'measurement coverage is stated');
    assert.ok(text().indexOf('2 rows without a usable time not placed') >= 0, 'unplaceable rows are disclosed');

    // The chart drew one dot per measured point — the zero included — and joined nothing.
    const chart = canvas();
    assert.strictEqual(chart._ctx.arcs.length, MEASURED, 'only settled, sized, timed points are drawn');
    assert.ok(chart._ctx.arcs.every((arc) => arc.r === 2.5), 'every dot is a plain dot without a selection');
    // Tick labels sit right-aligned just left of the plot (pad.left - 8 = 38).
    const gridRules = (target) => target._ctx.calls.filter((call) => call.align === 'right' && call.x === 38).length;
    assert.ok(gridRules(chart) >= 3, 'the y axis has readable ticks');
    assert.strictEqual(chart._ctx.lines, gridRules(chart) + 2, 'only grid rules and the 2 reference rules are lines');
    assert.ok(chart._ctx.calls.filter((call) => call.align === 'right' && call.x === 38)
        .every((call) => /^(0|\d+(\.\d)?k|\d+(\.\d)?M)$/.test(call.text) && !/^(63k|125k|188k)$/.test(call.text)),
    'ticks land on readable steps');
    assert.ok(chart._ctx.calls.some((call) => call.text === 'input tokens'), 'the y axis is labelled');
    assert.ok(chart._ctx.calls.some((call) => call.text === 'usage recorded →'), 'the x axis names its time');
    assert.ok(chart._ctx.calls.some((call) => call.text.indexOf('median ') === 0), 'the median rule is labelled');
    assert.ok(chart._ctx.calls.some((call) => call.text.indexOf('p95 ') === 0), 'the p95 rule is labelled');
    assert.ok(chart.getAttribute('aria-label').indexOf('25 measured requests') >= 0);
    assert.strictEqual(chart.getAttribute('tabindex'), '0', 'the chart is keyboard reachable');
    assertNoOverlappingText(chart, 'overview chart at 620px');

    // Requests and About are closed by default; the list pages to every request.
    assert.strictEqual(disclosure('requests').open, false, 'Requests starts closed');
    assert.strictEqual(disclosure('about').open, false, 'About starts closed');
    assert.strictEqual(rows().length, 6, 'the list is compact');
    button('Show more').dispatch('click');
    flushFrames();
    assert.strictEqual(rows().length, 18);
    button('Show more').dispatch('click');
    flushFrames();
    assert.strictEqual(rows().length, 27, 'every request in view is reachable as text');
    assert.strictEqual(button('Show more'), undefined);
    button('Show less').dispatch('click');
    flushFrames();
    assert.strictEqual(rows().length, 6);

    // About states the counters with sent separated from registered.
    const about = disclosure('about').text();
    assert.ok(about.indexOf('27 requests registered; 26 of them were sent to a provider.') >= 0);
    assert.ok(about.indexOf('1 admitted, not sent yet') >= 0, 'a reserved request is not called sent');
    assert.ok(about.indexOf('2 subscription session totals') >= 0, 'session totals are counted apart');
    assert.ok(about.indexOf('folding 40 older requests') >= 0, 'aggregates are counted, not drawn');
    assert.ok(about.indexOf('never added to the input or turned into a share') >= 0);
    assert.ok(about.indexOf('Complete within the enumerated categories') >= 0);
    assert.ok(about.indexOf('covers eligible physical requests') >= 0);
    assert.ok(about.indexOf('Legacy-only named categories may be absent') >= 0);
    assert.ok(about.indexOf('not all store rows') >= 0, 'complete never implies every store record was read');
    assert.ok(about.indexOf('Complete: every row of this span that the source holds was read.') < 0);

    // Selecting a request: detail plus task focus, from the data already held.
    const before = calls.length;
    const first = rows()[0];                                   // newest first: point 27 (task t-1…)
    doc.activeElement = first;
    first.dispatch('click');
    flushFrames();
    assert.strictEqual(calls.length, before, 'task focus fetches nothing');
    assert.strictEqual(doc.activeElement.getAttribute('data-focus'), first.getAttribute('data-focus'),
        'the keyboard stays on the row that was pressed');
    assert.ok(text().indexOf('Same task: 13 measured requests highlighted in this view') >= 0, text());
    assert.ok(text().indexOf('they are not joined') >= 0, 'the focus line says why nothing is joined');
    let arcs = canvas()._ctx.arcs;
    assert.strictEqual(arcs.filter((arc) => arc.r === 3.5).length, 13, 'the task\'s own points are emphasised');
    assert.strictEqual(arcs.filter((arc) => arc.r === 2.5).length, MEASURED - 13, 'everything else is dimmed');
    assert.strictEqual(canvas()._ctx.lines, gridRules(canvas()) + 2, 'focus draws no connecting line');
    assert.ok(arcs.some((arc) => arc.r === 8), 'the selected point is ringed');

    // A filter hides some of the task: the hidden count is stated, focus survives.
    const picker = root().querySelector('[data-focus="filter-model"]');
    doc.activeElement = picker;
    setFilter('model', 'vendor/model-a');
    assert.strictEqual(doc.activeElement.getAttribute('data-focus'), 'filter-model', 'focus survives the rebuild');
    assert.ok(/Same task: \d+ measured requests? highlighted in this view · \d+ more hidden by the filters/.test(text()),
        'requests of the task hidden by a filter are counted');
    const modeOptions = root().querySelector('[data-focus="filter-mode"]').childNodes.map((o) => o.value);
    assert.deepStrictEqual(modeOptions, ['all', 'max', 'low', 'nano', 'unknown'], 'Nano is offered, Unknown always');
    setFilter('model', 'all');
    doc.activeElement = null;

    // Keyboard on the chart: arrows step, Escape clears.
    const keys = (key) => { const target = canvas(); doc.activeElement = target; target.dispatch('keydown', { key, preventDefault() {} }); flushFrames(); };
    keys('Escape');
    assert.ok(text().indexOf('Same task') < 0, 'Escape clears the selection');
    keys('ArrowRight');
    assert.ok(root().findAll((n) => n.className === 'selection').length === 1, 'ArrowRight selects the oldest point');
    assert.strictEqual(doc.activeElement.getAttribute('data-focus'), 'chart', 'focus stays on the chart');
    keys('End');
    keys('Escape');
    doc.activeElement = null;

    // Disclosures and selection survive a refresh.
    const aboutNode = disclosure('about');
    aboutNode.open = true;
    aboutNode.dispatch('toggle');
    rows()[1].dispatch('click');
    const chosenKey = rows()[1].getAttribute('data-focus');
    button('Refresh').dispatch('click');
    await settle();
    assert.strictEqual(disclosure('about').open, true, 'an opened disclosure stays open across a refresh');
    assert.ok(rows()[1].classList.contains('row-active'), 'the selection survives a refresh');
    assert.strictEqual(rows()[1].getAttribute('data-focus'), chosenKey);

    // A failed refresh keeps the last answer, explicitly stale.
    responder = () => answer({ ok: false, available: false, reason: 'store_busy',
        message: 'The usage store is busy with a write right now. Try again in a moment.' });
    button('Refresh').dispatch('click');
    await settle();
    assert.ok(statusText().indexOf('Showing the read from') >= 0, 'stale data is labelled as such');
    assert.ok(statusText().indexOf('is busy') >= 0, 'the typed reason is visible');
    assert.strictEqual(canvas()._ctx.arcs.length, MEASURED, 'the last answer is still drawn');
    responder = (url) => answer(payload(horizonOf(url)));
    button('Refresh').dispatch('click');
    await settle();
    assert.ok(statusText().indexOf('Showing the read from') < 0, 'a good read clears the stale state');

    // A theme change repaints the canvas and rebuilds nothing.
    const headerBefore = root().childNodes[0];
    const framesBefore = canvas()._ctx.frames;
    themeListener('light');
    flushFrames();
    assert.strictEqual(doc.documentElement.dataset.theme, 'light');
    assert.strictEqual(root().childNodes[0], headerBefore, 'no control was rebuilt');
    assert.strictEqual(disclosure('about').open, true, 'no disclosure was closed');
    assert.ok(canvas()._ctx.frames > framesBefore, 'the chart was repainted');

    // A poll completes while a native SELECT is focused. Distinct timestamps,
    // point ids and values expose a new status over an old chart (including a
    // theme repaint). Refresh remains responsive; the latest good answer wins.
    const focused = root().querySelector('[data-focus="filter-model"]');
    focused.focus();
    const heldChart = canvas();
    const heldStatus = statusText();
    const heldCoverage = root().findAll((n) => n.classList.contains('coverage'))[0].textContent;
    const heldHits = Array.from(heldChart._hits, (hit) => [hit.point.id, hit.point.prompt_tokens, hit.x, hit.y]);
    const heldRows = rows().map((row) => row.text());
    function freshAnswer(index) {
        const data = payload('24h');
        const stamp = READ_AT + index * 60000;
        data.source.read_at_ms = stamp;
        data.source.newest_record_ms = stamp - 60000;
        data.horizon.now_ms = stamp;
        data.horizon.cutoff_ms = stamp - 86400000;
        data.points = [point(index, { t: stamp - 60000, prompt_tokens: index * 1000 })];
        data.horizon.records_selected = data.horizon.attempts_selected = data.horizon.points_sent = 1;
        data.horizon.selected_from_ms = data.horizon.selected_to_ms = stamp - 60000;
        data.counters.registered_attempts = data.counters.sent_attempts = data.counters.measured = 1;
        data.counters.settled_without_tokens = 0;
        data.counters.by_state = { reserved: 0, dispatched: 0, settled: 1, unresolved: 0, released: 0 };
        return data;
    }
    responder = () => answer(freshAnswer(81));
    const beforeFocusedPoll = calls.length;
    pollFn();
    button('Refreshing').dispatch('click');
    assert.strictEqual(calls.length, beforeFocusedPoll + 1, 'Refresh joins the in-flight read');
    await settle();
    assert.strictEqual(statusText(), heldStatus, 'the status still names the displayed read');
    assert.strictEqual(canvas(), heldChart, 'a focused SELECT holds the rebuild');
    assert.strictEqual(doc.activeElement, focused, 'the dropdown remains focused');
    assert.strictEqual(root().findAll((n) => n.classList.contains('coverage'))[0].textContent, heldCoverage);
    assert.deepStrictEqual(rows().map((row) => row.text()), heldRows);
    themeListener('dark');
    flushFrames();
    assert.deepStrictEqual(Array.from(canvas()._hits, (hit) => [hit.point.id, hit.point.prompt_tokens, hit.x, hit.y]),
        heldHits, 'a theme repaint keeps the old points AND old time domain');
    assert.strictEqual(statusText(), heldStatus);
    responder = () => answer(freshAnswer(82));
    assert.strictEqual(button('Refresh').disabled, false);
    button('Refresh').dispatch('click');
    assert.strictEqual(calls.length, beforeFocusedPoll + 2, 'Refresh can start another read while rendering waits');
    await settle();
    assert.strictEqual(statusText(), heldStatus, 'a second pending answer still cannot relabel the old chart');
    // In a browser blur sees the next active element, not the SELECT losing it.
    doc.activeElement = null;
    focused.dispatch('blur');
    flushFrames();
    const freshClock = new Date(READ_AT + 82 * 60000).toLocaleTimeString('en-US', { hour12: false });
    assert.ok(statusText().includes('read ' + freshClock), statusText());
    assert.deepStrictEqual(Array.from(canvas()._hits, (hit) => hit.point.id), [point(82).id]);
    assert.strictEqual(canvas()._hits[0].point.prompt_tokens, 82000);
    assert.ok(text().includes('1 request in view, 1 with a measured input'));
    assert.strictEqual(disclosure('about').open, true);

    // A later failed refresh retains the queued good read but also retains the
    // failure when blur adopts it; it must not resurrect an older good status.
    const failurePicker = root().querySelector('[data-focus="filter-model"]');
    failurePicker.focus();
    responder = () => answer(freshAnswer(83));
    pollFn();
    await settle();
    responder = () => answer({ ok: false, message: 'store temporarily busy' });
    button('Refresh').dispatch('click');
    await settle();
    assert.ok(statusText().includes(freshClock));
    assert.ok(statusText().includes('store temporarily busy'));
    doc.activeElement = null;
    failurePicker.dispatch('blur');
    flushFrames();
    assert.deepStrictEqual(Array.from(canvas()._hits, (hit) => hit.point.id), [point(83).id]);
    assert.ok(statusText().includes('store temporarily busy'), 'adopting the queued read preserves the newer failure');

    // A horizon change drops a queued answer as well as superseding a request.
    const supersededPicker = root().querySelector('[data-focus="filter-model"]');
    supersededPicker.focus();
    responder = () => answer(freshAnswer(84));
    pollFn();
    await settle();
    doc.activeElement = null;

    // Horizon clicks supersede the read in flight; the newest click wins.
    responder = park;
    horizonButton('1h').dispatch('click');
    assert.strictEqual(pressedHorizon(), '1h', 'the control follows the click at once');
    assert.ok(text().indexOf('Reading usage') >= 0, 'the old answer is not shown as the new span');
    horizonButton('7d').dispatch('click');
    assert.strictEqual(aborts, 1, 'the superseded read is aborted');
    assert.ok(calls[calls.length - 1].indexOf('horizon=7d') >= 0);
    parked.shift()();                                          // the 1h answer lands late
    await settle();
    assert.strictEqual(pressedHorizon(), '7d', 'a superseded answer cannot revert the click');
    assert.ok(text().indexOf('Reading usage') >= 0, 'and it is not adopted');
    parked.shift()();
    await settle();
    assert.ok(text().indexOf('Last 7 days') >= 0, 'the answer for the clicked horizon is adopted');
    assert.ok(canvas()._hits.every((hit) => hit.point.id !== point(84).id), 'the queued old horizon is discarded');
    assert.strictEqual(parked.length, 0);

    // Genuine empty: nothing recorded in the span, with the newest record and a way out.
    responder = (url) => answer(payload(horizonOf(url), {
        points: [], horizon: Object.assign(payload('1h').horizon, { records_selected: 0, attempts_selected: 0, points_sent: 0 }),
    }));
    horizonButton('1h').dispatch('click');
    await settle();
    assert.ok(text().indexOf('No requests recorded in the last hour.') >= 0, text());
    assert.ok(text().indexOf('The newest record found is from') >= 0);
    assert.ok(button('Show everything'), 'an empty span offers the whole record');
    assert.strictEqual(canvas()._ctx.arcs.length, 0);

    // Filter empty: data exists, the filters exclude all of it.
    responder = (url) => answer(payload(horizonOf(url)));
    horizonButton('24h').dispatch('click');
    await settle();
    setFilter('model', 'vendor/model-b');
    setFilter('mode', 'nano');
    setFilter('kind', 'skill_review');
    if (canvas()._ctx.arcs.length === 0) {
        assert.ok(text().indexOf('No requests match these filters.') >= 0 || text().indexOf('has a measured input') >= 0);
    }
    setFilter('origin', 'nonexistent-origin');
    assert.ok(text().indexOf('No requests match these filters.') >= 0, 'a filter-empty view says so');
    button('Clear filters').dispatch('click');
    flushFrames();
    assert.strictEqual(canvas()._ctx.arcs.length, MEASURED, 'Clear filters restores the view');

    // Missing measurements: requests exist, none with a size.
    responder = (url) => answer(payload(horizonOf(url), {
        points: [point(1, { state: 'dispatched', prompt_tokens: null }), point(2, { prompt_tokens: null })],
    }));
    button('Refresh').dispatch('click');
    await settle();
    assert.ok(text().indexOf('None of the 2 requests in view has a measured input.') >= 0, text());

    // The historical journal is labelled as such.
    responder = (url) => answer(payload(horizonOf(url), {
        source: { kind: 'legacy_journal', current: false, read_at_ms: READ_AT, newest_record_ms: READ_AT - 4 * 86400000,
            context_unread: 0 },
        horizon: Object.assign(payload('24h').horizon, { selection_complete: false, partial_reasons: ['journal_tail'],
            covered_from_ms: READ_AT - 3600000 }),
    }));
    button('Refresh').dispatch('click');
    await settle();
    assert.ok(statusText().indexOf('Retired usage journal — historical') >= 0, statusText());
    assert.ok(text().indexOf('so this is not the whole span') >= 0, 'an incomplete selection is never presented as the span');

    // A narrow card keeps every chart label apart.
    responder = (url) => answer(payload(horizonOf(url)));
    cardWidth = 320;
    button('Refresh').dispatch('click');
    await settle();
    assertNoOverlappingText(canvas(), 'overview chart at 320px');
    rows()[0].dispatch('click');
    flushFrames();
    assertNoOverlappingText(canvas(), 'focused chart at 320px');
    cardWidth = 620;

    // All-zero measured inputs put both reference rules on the bottom edge.
    // Labels must stay apart AFTER clamping, at both desktop and mobile heights.
    responder = (url) => answer(payload(horizonOf(url), {
        points: [point(1, { prompt_tokens: 0 }), point(2, { prompt_tokens: 0 })],
    }));
    for (const [width, height] of [[620, 260], [320, 200]]) {
        cardWidth = width;
        cardHeight = height;
        button('Refresh').dispatch('click');
        await settle();
        assert.strictEqual(canvas()._hits.length, 2, 'explicit zeros are still drawn');
        const labels = canvas()._ctx.calls.filter((call) => /^(median|p95) /.test(call.text));
        assert.deepStrictEqual(labels.map((call) => call.text).sort(), ['median 0', 'p95 0']);
        assert.ok(Math.abs(labels[0].y - labels[1].y) >= 16, 'clamped zero labels remain separated');
        assert.ok(labels.every((call) => call.y >= 32 && call.y <= height - 30), 'labels remain inside the plot');
        assertNoOverlappingText(canvas(), 'all-zero chart at ' + width + 'px');
    }
    cardWidth = 620;
    cardHeight = 260;

    // The initial error state: no answer yet, a typed message and a retry.
    responder = () => answer({}, 503);
    horizonButton('6h').dispatch('click');
    await settle();
    assert.ok(text().indexOf('Usage could not be read') >= 0);
    assert.ok(text().indexOf('status 503') >= 0, 'the failure is stated, not suppressed');
    assert.ok(button('Try again'), 'a retry is offered');

    // The poll reads only while the page is visible.
    responder = (url) => answer(payload(horizonOf(url)));
    const beforePoll = calls.length;
    doc.visibilityState = 'hidden';
    pollFn();
    assert.strictEqual(calls.length, beforePoll, 'no read while hidden');
    doc.visibilityState = 'visible';
    pollFn();
    assert.strictEqual(calls.length, beforePoll + 1, 'a visible poll reads');
    await settle();

    // Disposal: theme unsubscribed, frames cancelled, listeners and reads released.
    responder = park;
    button('Refresh').dispatch('click');
    const abortsBefore = aborts;
    assert.ok(typeof disposeHook === 'function', 'a dispose hook is registered');
    disposeHook();
    assert.strictEqual(themeUnsubscribed, 1, 'the theme subscription is released');
    assert.strictEqual(frames.filter(Boolean).length, 0, 'no animation frame survives disposal');
    assert.strictEqual(aborts, abortsBefore + 1, 'the read in flight is aborted');
    assert.strictEqual((doc.listeners.visibilitychange || []).length, 0, 'document listeners are removed');
    assert.strictEqual(pollFn, null, 'the poll is cleared');

    console.log('widget smoke: ok');
})().catch((error) => {
    console.error(error && error.stack ? error.stack : error);
    process.exit(1);
});
