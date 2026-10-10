/* Context Lens — module widget.
 *
 * Runs as a classic script inside the host's opaque-origin srcdoc frame, so:
 * no import/export at top level, no storage, no scriptable network. Every
 * request goes through OuroborosWidget.fetch and stays under this skill's own
 * route prefix. Colours and type sizes mirror docs/DESIGN.md / web/ui.css by
 * value, as two named palettes (dark and light), because the frame cannot reach
 * the host stylesheet; the host's resolved theme arrives through the optional
 * OuroborosWidget.onTheme bridge.
 *
 * Layout intent: the chart first. One short line says where the data came from
 * and how fresh the read is, one row holds the horizon and the filters, one
 * line says what the selection covers, then the chart. The request list and
 * every explanation sit behind two closed disclosures underneath.
 */
(function () {
    'use strict';

    var ROOT = '/api/extensions/context-lens/';
    var POLL_MS = 60000;
    var REQUEST_TIMEOUT_MS = 20000;
    var POINT_LIMIT = 4000;      // the server's own ceiling: draw the whole selection
    var ROWS_COLLAPSED = 6;      // compact by default
    var ROWS_STEP = 12;          // one "Show more" press
    var HIT_RADIUS = 16;         // nearest-point hit layer, wider than the dot

    /* The horizon is cut on the server. 'available' covers the enumerated
     * category streams, newest first, up to the server's row bound. Legacy-only
     * named categories may be absent; About states this limit. */
    var HORIZONS = [
        { key: '1h', label: '1h', full: 'the last hour' },
        { key: '6h', label: '6h', full: 'the last 6 hours' },
        { key: '24h', label: '24h', full: 'the last 24 hours' },
        { key: '7d', label: '7d', full: 'the last 7 days' },
        { key: 'available', label: 'All', full: 'the available data' }
    ];

    var STATE_LABELS = {
        settled: 'finished, usage recorded',
        dispatched: 'sent, awaiting usage',
        unresolved: 'sent, outcome unresolved',
        reserved: 'admitted, not sent yet',
        released: 'released without being sent'
    };

    var state = {
        data: null,              // the last good answer for the current horizon
        receivedAt: 0,
        error: null,             // { message } of the most recent failed read
        loading: false,
        horizon: '24h',
        filters: { model: 'all', kind: 'all', origin: 'all', mode: 'all' },
        selectedId: null,
        rows: ROWS_COLLAPSED,
        open: { requests: false, about: false, technical: false },
        // Derived by derive(): always from state.data and the filters together.
        points: [],
        plotted: [],
        stats: null,
        view: null,
        focus: null
    };

    var controllers = new Set();
    var requestTimers = new Set();
    var timers = [];
    var observers = [];
    var listeners = [];
    var scheduled = [];
    var chartDraws = [];
    var disposed = false;
    var inFlight = null;
    var inFlightHorizon = null;  // the horizon the in-flight request asks about
    var dataSeq = 0;             // generation of the current request
    var dataController = null;
    var pendingData = null;     // a good answer adopted only with its full render
    var pendingRender = false;
    var nodes = {};              // live nodes a status change updates in place
    var chartColors = {};

    // ---------------------------------------------------------------- utils

    function on(target, type, handler, options) {
        target.addEventListener(type, handler, options);
        listeners.push([target, type, handler, options]);
    }

    /* One animation frame per request, all cancellable: a draw scheduled for a
     * canvas that the next render detaches must never run. */
    function schedule(fn) {
        var id = requestAnimationFrame(function () {
            var at = scheduled.indexOf(id);
            if (at >= 0) scheduled.splice(at, 1);
            if (!disposed) fn();
        });
        scheduled.push(id);
        return id;
    }

    function clearScheduled() {
        scheduled.forEach(function (id) { cancelAnimationFrame(id); });
        scheduled = [];
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function isNumber(value) {
        return typeof value === 'number' && isFinite(value);
    }

    function num(value) {
        if (!isNumber(value)) return '—';
        return Math.round(value).toLocaleString('en-US');
    }

    function compact(value) {
        if (!isNumber(value)) return '—';
        var n = Math.round(value);
        if (n >= 1000000) return (n / 1000000).toFixed(n >= 10000000 ? 0 : 1) + 'M';
        if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k';
        return String(n);
    }

    function plural(count, one, many) {
        return num(count) + ' ' + (count === 1 ? one : (many || one + 's'));
    }

    /* One clock everywhere: this device's local time. */
    function pad2(value) { return String(value).padStart(2, '0'); }

    function clock(ms) {
        if (!isNumber(ms)) return '—';
        var d = new Date(ms);
        return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
    }

    function shortClock(ms) {
        var d = new Date(ms);
        return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
    }

    function dayTime(ms) {
        if (!isNumber(ms)) return '—';
        return new Date(ms).toLocaleString('en-US', {
            month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false
        });
    }

    function fullTime(ms) {
        if (!isNumber(ms)) return 'no usable time';
        return new Date(ms).toLocaleString('en-US', { hour12: false });
    }

    /* A moment near the reference reads as a clock time, an older one with its date. */
    function when(ms, reference) {
        if (!isNumber(ms)) return '—';
        return Math.abs((reference || ms) - ms) < 20 * 3600000 ? clock(ms) : dayTime(ms);
    }

    function duration(ms) {
        if (!isNumber(ms) || ms < 0) return '—';
        var minutes = Math.round(ms / 60000);
        if (minutes < 1) return 'under a minute';
        if (minutes < 60) return minutes + ' min';
        var hours = Math.floor(minutes / 60);
        if (hours < 48) {
            var rest = minutes % 60;
            return hours + ' h' + (rest && hours < 10 ? ' ' + rest + ' min' : '');
        }
        return Math.floor(hours / 24) + ' d';
    }

    function axisTime(ms, span) {
        if (span <= 26 * 3600000) return shortClock(ms);
        var d = new Date(ms);
        var day = d.toLocaleString('en-US', { month: 'short', day: 'numeric' });
        return span <= 8 * 86400000 ? day + ' ' + shortClock(ms) : day;
    }

    function horizonSpec(key) {
        for (var i = 0; i < HORIZONS.length; i += 1) {
            if (HORIZONS[i].key === key) return HORIZONS[i];
        }
        return HORIZONS[HORIZONS.length - 1];
    }

    /* Presentation only. The exact recorded token is kept as the title and is
     * shown verbatim in the request's technical detail. */
    function humanize(token) {
        var text = String(token === null || token === undefined ? '' : token);
        if (!text) return 'unknown';
        var spaced = text.replace(/[_.-]+/g, ' ').trim();
        if (!spaced) return text;
        return spaced.charAt(0).toUpperCase() + spaced.slice(1);
    }

    function modeLabel(mode) {
        if (mode === 'max') return 'Max';
        if (mode === 'low') return 'Low';
        if (mode === 'nano') return 'Nano';
        return 'Unknown';
    }

    function quantile(sorted, fraction) {
        if (!sorted.length) return null;
        if (sorted.length === 1) return sorted[0];
        var position = Math.max(0, Math.min(1, fraction)) * (sorted.length - 1);
        var low = Math.floor(position);
        var high = Math.min(low + 1, sorted.length - 1);
        var weight = position - low;
        return sorted[low] * (1 - weight) + sorted[high] * weight;
    }

    function measured(point) {
        return point.state === 'settled' && isNumber(point.prompt_tokens) && isNumber(point.t);
    }

    // ------------------------------------------------------------- requests

    function abort(controller) {
        if (!controller) return;
        try { controller.abort(); } catch (error) { /* already settled */ }
    }

    /* Every request carries its own controller and a timeout; dispose aborts
     * whatever is still open. */
    function request(path, controller) {
        var api = window.OuroborosWidget && typeof window.OuroborosWidget.fetch === 'function'
            ? window.OuroborosWidget.fetch
            : window.fetch;
        controllers.add(controller);
        var expired = false;
        var timer = setTimeout(function () { expired = true; abort(controller); }, REQUEST_TIMEOUT_MS);
        requestTimers.add(timer);
        var started;
        try {
            started = Promise.resolve(api(ROOT + path, { signal: controller.signal, timeoutMs: REQUEST_TIMEOUT_MS }));
        } catch (error) {
            started = Promise.reject(error);
        }
        return started
            .then(function (response) {
                if (!response.ok) {
                    var failure = new Error('status');
                    failure.status = response.status;
                    throw failure;
                }
                return response.json();
            })
            .catch(function (error) {
                if (expired) {
                    var late = new Error('timeout');
                    late.timeout = true;
                    throw late;
                }
                throw error;
            })
            .finally(function () {
                clearTimeout(timer);
                requestTimers.delete(timer);
                controllers.delete(controller);
            });
    }

    function failureMessage(error) {
        if (error && (error.timeout || /timed out/i.test(String(error.message || '')))) {
            return 'The Context Lens route did not answer within 20 seconds.';
        }
        if (error && error.status) {
            return 'The Context Lens route answered with status ' + error.status + '.';
        }
        return 'Could not reach the Context Lens route in this install.';
    }

    /* Every read carries a generation. Only the current generation may write to
     * state, so an older answer can never undo a newer click — and a horizon
     * change supersedes the read in flight rather than waiting for it. */
    function load() {
        if (disposed) return Promise.resolve();
        var requested = state.horizon;
        if (inFlight && requested === inFlightHorizon) return inFlight;
        abort(dataController);
        var token = dataSeq + 1;
        dataSeq = token;
        inFlightHorizon = requested;
        var controller = new AbortController();
        dataController = controller;
        state.loading = true;
        paintStatus();
        inFlight = request('data?horizon=' + encodeURIComponent(requested) + '&limit=' + POINT_LIMIT,
            controller)
            .then(function (payload) {
                if (disposed || token !== dataSeq || requested !== state.horizon) return;
                if (payload && payload.ok) {
                    // The answer states the horizon it actually applied; it is
                    // adopted only when that is still the one selected.
                    var applied = payload.horizon && payload.horizon.selected;
                    if (applied && applied !== state.horizon) return;
                    pendingData = { data: payload, receivedAt: Date.now() };
                    state.error = null;
                } else {
                    // A typed refusal from the route. Any earlier answer stays on
                    // screen, explicitly marked as no longer current.
                    state.error = { message: (payload && payload.message) || 'Telemetry is unavailable.' };
                }
            })
            .catch(function (error) {
                if (disposed || token !== dataSeq) return;
                if (error && error.name === 'AbortError' && !error.timeout) return;
                state.error = { message: failureMessage(error) };
            })
            .finally(function () {
                // A superseded request must not clear the flags that now belong
                // to the request which replaced it.
                if (token !== dataSeq) return;
                inFlight = null;
                inFlightHorizon = null;
                dataController = null;
                state.loading = false;
                if (!disposed) renderSoon();
            });
        return inFlight;
    }

    /* Changing the horizon changes the population, so the selection and the
     * paged list start again from the new answer, and the old answer is not
     * shown as if it described the new span. */
    function selectHorizon(key) {
        if (key === state.horizon) return;
        state.horizon = key;
        pendingData = null;
        state.data = null;
        state.error = null;
        state.selectedId = null;
        state.rows = ROWS_COLLAPSED;
        render();
        load();
    }

    function selectPoint(id) {
        state.selectedId = id || null;
        render();
    }

    function clearFilters() {
        state.filters = { model: 'all', kind: 'all', origin: 'all', mode: 'all' };
        state.rows = ROWS_COLLAPSED;
        render();
    }

    // ------------------------------------------------------------ deriving

    function selectedPoint() {
        if (!state.selectedId || !state.data) return null;
        var points = state.data.points || [];
        for (var i = 0; i < points.length; i += 1) {
            if (points[i].id === state.selectedId) return points[i];
        }
        return null;
    }

    function derive() {
        var all = (state.data && state.data.points) || [];
        var f = state.filters;
        state.points = all.filter(function (point) {
            if (f.model !== 'all' && point.model !== f.model) return false;
            if (f.kind !== 'all' && point.category !== f.kind) return false;
            if (f.origin !== 'all' && point.source !== f.origin) return false;
            if (f.mode !== 'all' && (point.mode || 'unknown') !== f.mode) return false;
            return true;
        });
        state.plotted = state.points.filter(measured);

        // Coverage of the view the owner is looking at, from the same points
        // the chart draws — not from the unfiltered counters.
        var view = { total: state.points.length, measured: state.plotted.length,
            withoutSize: 0, withoutTime: 0, notFinished: 0 };
        state.points.forEach(function (point) {
            if (point.state === 'settled') {
                if (!isNumber(point.prompt_tokens)) view.withoutSize += 1;
                else if (!isNumber(point.t)) view.withoutTime += 1;
            } else {
                view.notFinished += 1;
            }
        });
        state.view = view;

        var values = state.plotted.map(function (p) { return p.prompt_tokens; })
            .sort(function (a, b) { return a - b; });
        state.stats = {
            count: values.length,
            median: quantile(values, 0.5),
            p95: quantile(values, 0.95),
            peak: values.length ? values[values.length - 1] : null
        };

        // Task focus: the selected request's task, from this same answer and
        // these same filters. Nothing is fetched, and nothing is joined.
        var chosen = selectedPoint();
        state.focus = null;
        if (chosen && chosen.task) {
            var drawn = new Set(state.plotted);
            var inView = state.plotted.filter(function (p) { return p.task === chosen.task; });
            var hidden = all.filter(function (p) {
                return p.task === chosen.task && measured(p) && !drawn.has(p);
            });
            state.focus = { task: chosen.task, inView: inView.length, hidden: hidden.length };
        }

        var total = state.points.length;
        if (state.rows > Math.max(ROWS_COLLAPSED, total)) state.rows = Math.max(ROWS_COLLAPSED, total);
    }

    // -------------------------------------------------------------- drawing

    var CHART_TOKENS = ['chart-text', 'grid', 'point', 'point-dim', 'focus', 'selected', 'reference', 'ring'];
    var DARK_CHART = {
        'chart-text': '#a8b0bd', grid: 'rgba(255,255,255,0.10)', point: 'rgba(226,232,240,0.55)',
        'point-dim': 'rgba(226,232,240,0.16)', focus: '#f07a86', selected: '#f07a86',
        reference: 'rgba(168,176,189,0.75)', ring: '#0d0b0f'
    };
    var FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif';
    // Clear space demanded between two measured pieces of chart text. Labels are
    // dropped, never shrunk: every chart string stays at 12px.
    var LABEL_GAP = 6;

    /* Canvas inks come from the same named tokens the stylesheet declares, read
     * at draw time, so a theme change is a repaint and nothing more. */
    function readChartColors() {
        var css = typeof getComputedStyle === 'function' ? getComputedStyle(document.documentElement) : null;
        CHART_TOKENS.forEach(function (name) {
            var value = css && css.getPropertyValue ? String(css.getPropertyValue('--lens-' + name) || '').trim() : '';
            chartColors[name] = value || DARK_CHART[name];
        });
    }

    function sizeCanvas(canvas) {
        var ratio = window.devicePixelRatio || 1;
        var width = Math.max(200, Math.round(canvas.clientWidth));
        var height = Math.max(120, Math.round(canvas.clientHeight));
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
        var ctx = canvas.getContext('2d');
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.clearRect(0, 0, width, height);
        return { ctx: ctx, width: width, height: height };
    }

    /* A readable tick step (1, 2 or 5 x 10^n) for about four intervals, so the
     * axis says 50k / 100k / 150k rather than 63k / 125k / 188k. */
    function niceStep(peak) {
        var raw = (peak > 0 ? peak : 1000) / 4;
        var magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
        var fraction = raw / magnitude;
        var nice = fraction < 1.5 ? 1 : fraction < 3 ? 2 : fraction < 7 ? 5 : 10;
        return Math.max(1, nice * magnitude);
    }

    /* The x domain says what was read: the whole horizon when the selection is
     * complete, from the oldest row read when a bound bit, up to the read
     * instant for the live store — so an idle stretch shows as empty space
     * rather than being stretched away. */
    function timeDomain() {
        var data = state.data;
        var h = data.horizon || {};
        var times = state.plotted.map(function (p) { return p.t; });
        var lo = Math.min.apply(null, times);
        var hi = Math.max.apply(null, times);
        if (h.selection_complete && isNumber(h.cutoff_ms)) lo = Math.min(lo, h.cutoff_ms);
        else if (isNumber(h.covered_from_ms)) lo = Math.min(lo, h.covered_from_ms);
        if (data.source && data.source.current && isNumber(h.now_ms)) hi = Math.max(hi, h.now_ms);
        if (hi - lo < 60000) { lo -= 30000; hi += 30000; }
        return { lo: lo, hi: hi };
    }

    function drawScatter(canvas) {
        var box = sizeCanvas(canvas);
        var ctx = box.ctx;
        // `top` leaves a whole 12px line above the plot for the two captions,
        // so neither can land on the topmost tick label.
        var pad = { left: 46, right: 12, top: 26, bottom: 24 };
        var CAPTION_Y = 10;
        var plotW = box.width - pad.left - pad.right;
        var plotH = box.height - pad.top - pad.bottom;
        canvas._hits = [];
        var points = state.plotted;
        if (!points.length || plotW <= 0 || plotH <= 0) return;

        var domain = timeDomain();
        var step = niceStep(state.stats.peak);
        var yMax = Math.max(step, Math.ceil((state.stats.peak * 1.02) / step) * step);
        var xOf = function (t) { return pad.left + ((t - domain.lo) / (domain.hi - domain.lo)) * plotW; };
        var yOf = function (v) { return pad.top + plotH - (v / yMax) * plotH; };

        ctx.font = '12px ' + FONT;
        ctx.textBaseline = 'middle';
        ctx.lineWidth = 1;
        for (var i = 0; i * step <= yMax; i += 1) {
            var value = step * i;
            var y = Math.round(yOf(value)) + 0.5;
            ctx.strokeStyle = chartColors.grid;
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(pad.left + plotW, y);
            ctx.stroke();
            ctx.fillStyle = chartColors['chart-text'];
            ctx.textAlign = 'right';
            ctx.fillText(compact(value), pad.left - 8, y);
        }

        ctx.fillStyle = chartColors['chart-text'];
        ctx.textAlign = 'left';
        ctx.fillText('input tokens', 2, CAPTION_Y);
        var leftEnd = 2 + ctx.measureText('input tokens').width;
        var xCaption = 'usage recorded →';
        if (box.width - 2 - ctx.measureText(xCaption).width >= leftEnd + LABEL_GAP) {
            ctx.textAlign = 'right';
            ctx.fillText(xCaption, box.width - 2, CAPTION_Y);
        }

        // Time labels are measured: the first and last always survive, an
        // intermediate one only when its box clears both neighbours.
        ctx.textAlign = 'center';
        var span = domain.hi - domain.lo;
        var ticks = [];
        for (var k = 0; k <= 3; k += 1) {
            var t = domain.lo + (span / 3) * k;
            var text = axisTime(t, span);
            var half = ctx.measureText(text).width / 2;
            var cx = Math.min(box.width - 2 - half, Math.max(2 + half, xOf(t)));
            ticks.push({ text: text, x: cx, left: cx - half, right: cx + half });
        }
        var last = ticks[ticks.length - 1];
        var drawn = [ticks[0]];
        var prevRight = ticks[0].right;
        for (var m = 1; m < ticks.length - 1; m += 1) {
            if (ticks[m].left >= prevRight + LABEL_GAP && ticks[m].right + LABEL_GAP <= last.left) {
                drawn.push(ticks[m]);
                prevRight = ticks[m].right;
            }
        }
        if (last.left >= prevRight + LABEL_GAP) drawn.push(last);
        drawn.forEach(function (tick) { ctx.fillText(tick.text, tick.x, box.height - 10); });

        // Median and p95 of exactly these points, as quiet rules under the dots.
        // Their labels are drawn after the dots, on a halo of the surface
        // colour, inside the plot's right edge. Clamp first, then separate the
        // labels, including coincident rules at the bottom of an all-zero plot.
        var rules = [];
        if (isNumber(state.stats.p95)) rules.push({ label: 'p95 ' + compact(state.stats.p95), y: yOf(state.stats.p95), dash: [2, 3] });
        if (isNumber(state.stats.median)) rules.push({ label: 'median ' + compact(state.stats.median), y: yOf(state.stats.median), dash: [5, 4] });
        rules.forEach(function (rule) {
            var y = Math.round(rule.y) + 0.5;
            ctx.save();
            ctx.strokeStyle = chartColors.reference;
            ctx.setLineDash(rule.dash);
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(pad.left + plotW, y);
            ctx.stroke();
            ctx.restore();
        });

        // One dot per measured request. Nothing is ever joined: neighbouring
        // dots can belong to unrelated tasks, and even one task's requests can
        // be parallel review slots, other providers or other sources.
        var focus = state.focus;
        var chosen = state.selectedId;
        var background = [];
        var foreground = [];
        points.forEach(function (point) {
            var hit = { x: xOf(point.t), y: yOf(Math.min(point.prompt_tokens, yMax)), point: point };
            canvas._hits.push(hit);
            if (focus && point.task === focus.task) foreground.push(hit);
            else background.push(hit);
        });
        background.forEach(function (hit) {
            ctx.beginPath();
            ctx.arc(hit.x, hit.y, 2.5, 0, Math.PI * 2);
            ctx.fillStyle = focus ? chartColors['point-dim'] : chartColors.point;
            ctx.fill();
        });
        foreground.forEach(function (hit) {
            ctx.beginPath();
            ctx.arc(hit.x, hit.y, 3.5, 0, Math.PI * 2);
            ctx.fillStyle = chartColors.focus;
            ctx.fill();
        });
        var labelTop = pad.top + 6;
        var labelBottom = pad.top + plotH - 6;
        rules.forEach(function (rule) {
            rule.labelY = Math.max(labelTop, Math.min(labelBottom, rule.y - 8));
        });
        if (rules.length === 2 && rules[1].labelY - rules[0].labelY < 16) {
            rules[1].labelY = Math.min(labelBottom, rules[0].labelY + 16);
            rules[0].labelY = rules[1].labelY - 16;
        }
        rules.forEach(function (rule) {
            ctx.save();
            ctx.textAlign = 'right';
            ctx.lineJoin = 'round';
            ctx.lineWidth = 4;
            ctx.strokeStyle = chartColors.ring;
            ctx.strokeText(rule.label, pad.left + plotW - 2, rule.labelY);
            ctx.fillStyle = chartColors['chart-text'];
            ctx.fillText(rule.label, pad.left + plotW - 2, rule.labelY);
            ctx.restore();
        });
        canvas._hits.forEach(function (hit) {
            if (hit.point.id !== chosen) return;
            ctx.beginPath();
            ctx.arc(hit.x, hit.y, 5, 0, Math.PI * 2);
            ctx.fillStyle = chartColors.selected;
            ctx.fill();
            ctx.lineWidth = 2;
            ctx.strokeStyle = chartColors.ring;
            ctx.stroke();
            ctx.beginPath();
            ctx.arc(hit.x, hit.y, 8, 0, Math.PI * 2);
            ctx.lineWidth = 1.5;
            ctx.strokeStyle = chartColors.selected;
            ctx.stroke();
        });
    }

    /* One draw per animation frame per chart, and never for a detached node.
     * The observer redraws only when the observed box actually changed size: a
     * canvas repaint writes no layout, so answering a same-size notification
     * would only feed the observer its own work. The chart box has a fixed CSS
     * height, so a redraw can never move the card's height either. */
    function liveChart(container, canvas, draw) {
        chartDraws.push(function () { if (canvas.isConnected !== false) draw(); });
        schedule(draw);
        if (typeof ResizeObserver !== 'function') return;
        var pending = false;
        var lastW = Math.round(container.clientWidth || 0);
        var lastH = Math.round(container.clientHeight || 0);
        var observer = new ResizeObserver(function () {
            var width = Math.round(container.clientWidth || 0);
            var height = Math.round(container.clientHeight || 0);
            if (width === lastW && height === lastH) return;
            lastW = width;
            lastH = height;
            if (pending) return;
            pending = true;
            schedule(function () {
                pending = false;
                if (canvas.isConnected !== false) draw();
            });
        });
        observer.observe(container);
        observers.push(observer);
    }

    function repaintCharts() {
        chartDraws.forEach(function (draw) { schedule(draw); });
    }

    // ------------------------------------------------------------ rendering

    function sourceStatus() {
        var data = state.data;
        if (!data) {
            if (state.error) return { tone: 'error', text: state.error.message };
            return { tone: 'neutral', text: 'Reading usage…' };
        }
        var source = data.source || {};
        var parts = [];
        if (state.error) {
            parts.push('Showing the read from ' + clock(source.read_at_ms));
            parts.push('the latest refresh failed: ' + state.error.message);
            return { tone: 'warn', text: parts.join(' · ') };
        }
        if (source.kind === 'usage_store') {
            parts.push('Usage store');
            parts.push('read ' + clock(source.read_at_ms));
        } else {
            parts.push('Retired usage journal — historical, this install has no usage store');
        }
        if (isNumber(source.newest_record_ms)) {
            var gap = source.read_at_ms - source.newest_record_ms;
            parts.push(source.kind === 'usage_store' && gap >= 0
                ? 'newest record ' + (gap < 60000 ? 'under a minute' : duration(gap)) + ' before this read'
                : 'newest record ' + dayTime(source.newest_record_ms));
        } else {
            parts.push('no timed record found');
        }
        if (state.loading) parts.push('refreshing…');
        return { tone: source.kind === 'usage_store' ? 'ok' : 'neutral', text: parts.join(' · ') };
    }

    /* Status changes (a read starting, a refresh failing) update two nodes in
     * place: no rebuild, so an open dropdown or a focused row is undisturbed. */
    function paintStatus() {
        if (!nodes.status || !nodes.refresh) return;
        var status = sourceStatus();
        nodes.dot.className = 'dot dot-' + status.tone;
        nodes.statusText.textContent = status.text;
        nodes.refresh.textContent = state.loading ? 'Refreshing…' : 'Refresh';
        // Never disabled: a focused control that becomes disabled drops the
        // keyboard, and a second press while reading joins the same read.
        nodes.refresh.setAttribute('aria-busy', state.loading ? 'true' : 'false');
    }

    function renderHeader(root) {
        var header = el('div', 'header');
        var status = el('p', 'status');
        var dot = el('span', 'dot');
        dot.setAttribute('aria-hidden', 'true');
        var text = el('span', 'status-text');
        status.appendChild(dot);
        status.appendChild(text);
        header.appendChild(status);
        var refresh = el('button', 'button', 'Refresh');
        refresh.type = 'button';
        refresh.setAttribute('data-focus', 'refresh');
        on(refresh, 'click', function () { load(); });
        header.appendChild(refresh);
        root.appendChild(header);
        nodes = { status: status, dot: dot, statusText: text, refresh: refresh };
        paintStatus();
    }

    function addSelect(entry, parent) {
        var picker = el('select', 'control');
        picker.setAttribute('aria-label', entry.label);
        picker.setAttribute('data-focus', 'filter-' + entry.key);
        var options = entry.options.slice();
        // A value that is filtering right now always stays selectable, even if
        // the newest answer no longer contains it.
        if (entry.value !== 'all' && options.indexOf(entry.value) < 0) options.push(entry.value);
        ['all'].concat(options).forEach(function (option) {
            var label = option === 'all'
                ? entry.allLabel
                : (entry.labels && entry.labels[option]) || (entry.human ? humanize(option) : option);
            var opt = el('option', null, label);
            opt.value = option;
            if (entry.human && option !== 'all') opt.title = option;
            if (option === entry.value) opt.selected = true;
            picker.appendChild(opt);
        });
        on(picker, 'change', function () {
            state.filters[entry.key] = picker.value;
            state.rows = ROWS_COLLAPSED;
            render();
        });
        on(picker, 'blur', function () { if (pendingRender) render(); });
        parent.appendChild(picker);
    }

    function renderToolbar(root) {
        var bar = el('div', 'toolbar');
        var group = el('div', 'segmented');
        group.setAttribute('role', 'group');
        group.setAttribute('aria-label', 'Horizon');
        HORIZONS.forEach(function (entry) {
            var button = el('button', 'segment', entry.label);
            button.type = 'button';
            button.setAttribute('data-focus', 'horizon-' + entry.key);
            button.setAttribute('aria-pressed', entry.key === state.horizon ? 'true' : 'false');
            button.title = 'Show ' + entry.full;
            if (entry.key === state.horizon) button.classList.add('segment-on');
            on(button, 'click', function () { selectHorizon(entry.key); });
            group.appendChild(button);
        });
        bar.appendChild(group);

        var filters = el('div', 'filters');
        var facets = (state.data && state.data.facets) || { models: [], categories: [], sources: [], modes: [] };
        addSelect({ key: 'model', label: 'Model', allLabel: 'All models', options: facets.models,
            value: state.filters.model }, filters);
        addSelect({ key: 'kind', label: 'Work kind', allLabel: 'All work kinds', options: facets.categories,
            value: state.filters.kind, human: true }, filters);
        addSelect({ key: 'origin', label: 'Recorded by', allLabel: 'All origins', options: facets.sources,
            value: state.filters.origin, human: true }, filters);
        // Unknown is a permanent option: "no mode was recorded" is a real answer
        // about this install, not an artefact of what happens to be in view.
        var modes = ['max', 'low', 'nano'].filter(function (mode) { return facets.modes.indexOf(mode) >= 0; });
        modes.push('unknown');
        addSelect({ key: 'mode', label: 'Mode', allLabel: 'All modes', options: modes,
            labels: { max: 'Max', low: 'Low', nano: 'Nano', unknown: 'Unknown (not recorded)' },
            value: state.filters.mode }, filters);
        bar.appendChild(filters);
        root.appendChild(bar);
    }

    /* One line: what the selection covers and how much of the view is measured.
     * Completeness, the point cap and measurement are separate facts and are
     * stated separately. */
    function coverageSentence() {
        var data = state.data;
        var h = data.horizon || {};
        var spec = horizonSpec(h.selected);
        var view = state.view;
        var parts = [spec.key === 'available' ? 'Available requests' : 'Last ' + spec.full.replace('the last ', '')];
        parts.push(plural(view.total, 'request') + ' in view, ' + num(view.measured) + ' with a measured input');
        if (h.selection_complete === false) {
            var from = isNumber(h.covered_from_ms) ? ' (from ' + when(h.covered_from_ms, h.now_ms) + ')' : '';
            if ((h.partial_reasons || []).indexOf('journal_tail') >= 0) {
                parts.push('the journal’s retained rows start later' + from + ', so this is not the whole span');
            } else if ((h.partial_reasons || []).indexOf('row_cap') >= 0) {
                parts.push('only the newest ' + num(data.limits && data.limits.max_rows) + ' rows were read' + from
                    + ', so this is not the whole span');
            } else {
                parts.push('the read stopped at a bound' + from + ', so this is not the whole span');
            }
        }
        if (data.points_omitted) parts.push(plural(data.points_omitted, 'older request') + ' not sent');
        if (h.unknown_timestamp) {
            parts.push(plural(h.unknown_timestamp, 'row') + ' without a usable time not placed');
        }
        return parts.join(' · ');
    }

    function chartMessage(box, title, body, action) {
        var message = el('div', 'chart-message');
        message.appendChild(el('p', 'message-title', title));
        if (body) message.appendChild(el('p', 'meta muted', body));
        if (action) {
            var button = el('button', 'button button-quiet', action.label);
            button.type = 'button';
            button.setAttribute('data-focus', action.focus);
            on(button, 'click', action.run);
            message.appendChild(button);
        }
        box.appendChild(message);
    }

    function emptyStateFor(box) {
        var data = state.data;
        if (!data) {
            if (state.error) {
                chartMessage(box, 'Usage could not be read', state.error.message,
                    { label: 'Try again', focus: 'retry', run: function () { load(); } });
            } else {
                chartMessage(box, 'Reading usage…', null, null);
            }
            return true;
        }
        if (state.plotted.length) return false;
        var h = data.horizon || {};
        var source = data.source || {};
        if (!(data.points || []).length) {
            var spec = horizonSpec(h.selected);
            var newest = isNumber(source.newest_record_ms)
                ? 'The newest record found is from ' + dayTime(source.newest_record_ms) + '.'
                : 'No timed record was found by this reader.';
            if (h.records_selected) {
                newest += ' ' + plural(h.records_selected, 'other row') + ' (session totals, aggregates) '
                    + 'are counted in About this data.';
            }
            chartMessage(box, 'No requests recorded in ' + spec.full + '.', newest,
                spec.key === 'available' ? null
                    : { label: 'Show everything', focus: 'show-all', run: function () { selectHorizon('available'); } });
            return true;
        }
        if (!state.points.length) {
            chartMessage(box, 'No requests match these filters.', null,
                { label: 'Clear filters', focus: 'clear-filters', run: clearFilters });
            return true;
        }
        chartMessage(box, 'None of the ' + plural(state.points.length, 'request') + ' in view has a measured input.',
            'Requests still in flight, released, unresolved or finished without a reported size are listed under '
            + 'Requests and counted in About this data, never drawn as zero.', null);
        return true;
    }

    function renderChart(root) {
        var chart = el('div', 'chart');
        var canvas = el('canvas', 'canvas');
        canvas.setAttribute('role', 'img');
        canvas.setAttribute('data-focus', 'chart');
        chart.appendChild(canvas);
        var tooltip = el('div', 'tooltip');
        tooltip.hidden = true;
        chart.appendChild(tooltip);
        root.appendChild(chart);
        var empty = emptyStateFor(chart);
        canvas.setAttribute('aria-label', empty
            ? 'Input size chart: nothing to draw.'
            : 'Scatter chart of reported input tokens against the time usage was recorded, for the '
                + plural(state.plotted.length, 'measured request') + ' in view. Median '
                + compact(state.stats.median) + ', 95th percentile ' + compact(state.stats.p95)
                + '. Arrow keys step through the requests; every request in view is also listed under Requests.');
        liveChart(chart, canvas, function () { drawScatter(canvas); });
        if (empty) return;
        canvas.setAttribute('tabindex', '0');

        var nearest = function (event) {
            var rect = canvas.getBoundingClientRect();
            var x = event.clientX - rect.left;
            var y = event.clientY - rect.top;
            var best = null;
            (canvas._hits || []).forEach(function (hit) {
                var distance = Math.hypot(hit.x - x, hit.y - y);
                if (distance <= HIT_RADIUS && (!best || distance < best.distance)) {
                    best = { hit: hit, distance: distance, rect: rect };
                }
            });
            return best;
        };
        on(canvas, 'pointermove', function (event) {
            var best = nearest(event);
            if (!best) { tooltip.hidden = true; return; }
            var point = best.hit.point;
            tooltip.hidden = false;
            tooltip.textContent = num(point.prompt_tokens) + ' input · ' + point.model + ' · '
                + humanize(point.category) + ' · ' + clock(point.t);
            var left = Math.min(best.rect.width - 8, Math.max(8, best.hit.x));
            tooltip.style.left = left + 'px';
            tooltip.style.top = Math.max(0, best.hit.y - 36) + 'px';
        });
        on(canvas, 'pointerleave', function () { tooltip.hidden = true; });
        on(canvas, 'click', function (event) {
            var best = nearest(event);
            if (best) selectPoint(best.hit.point.id);
        });
        on(canvas, 'keydown', function (event) {
            var ordered = state.plotted.slice().sort(function (a, b) { return a.t - b.t; });
            if (!ordered.length) return;
            var at = -1;
            for (var i = 0; i < ordered.length; i += 1) {
                if (ordered[i].id === state.selectedId) { at = i; break; }
            }
            var next = null;
            if (event.key === 'ArrowRight') next = ordered[at < 0 ? 0 : Math.min(ordered.length - 1, at + 1)];
            else if (event.key === 'ArrowLeft') next = ordered[at < 0 ? ordered.length - 1 : Math.max(0, at - 1)];
            else if (event.key === 'Home') next = ordered[0];
            else if (event.key === 'End') next = ordered[ordered.length - 1];
            else if (event.key === 'Escape' && state.selectedId) { event.preventDefault(); selectPoint(null); return; }
            if (!next) return;
            event.preventDefault();
            selectPoint(next.id);
            announce(num(next.prompt_tokens) + ' input tokens, ' + next.model + ', ' + clock(next.t));
        });
    }

    function announce(text) {
        if (nodes.live) nodes.live.textContent = text;
    }

    function detailRow(parent, label, value, options) {
        var settings = options || {};
        var row = el('div', 'detail-row');
        var left = el('span', 'detail-label', label);
        var right = el('span', 'detail-value', value);
        if (settings.exact) right.title = 'Recorded as: ' + settings.exact;
        row.appendChild(left);
        row.appendChild(right);
        parent.appendChild(row);
        if (settings.note) parent.appendChild(el('p', 'detail-note', settings.note));
    }

    function bindDisclosure(details, key) {
        details.open = !!state.open[key];
        details.setAttribute('data-open-key', key);
        on(details, 'toggle', function () { state.open[key] = !!details.open; });
    }

    function renderSelection(root) {
        var point = selectedPoint();
        if (!point) return;
        var section = el('section', 'selection');
        section.setAttribute('aria-label', 'Selected request');

        var lead = el('div', 'selection-lead');
        lead.appendChild(el('span', 'lead-value',
            isNumber(point.prompt_tokens) ? num(point.prompt_tokens) : 'No size reported'));
        lead.appendChild(el('span', 'meta muted', isNumber(point.prompt_tokens) ? 'input tokens' : ''));
        var close = el('button', 'button button-quiet', 'Clear');
        close.type = 'button';
        close.setAttribute('data-focus', 'clear-selection');
        close.setAttribute('aria-label', 'Clear the selected request');
        on(close, 'click', function () { selectPoint(null); });
        lead.appendChild(close);
        section.appendChild(lead);

        var facts = [point.model, humanize(point.category), modeLabel(point.mode) + ' mode',
            'usage recorded ' + when(point.t, state.data.source && state.data.source.read_at_ms)];
        if (point.state !== 'settled') facts.push(STATE_LABELS[point.state] || point.state);
        section.appendChild(el('p', 'meta muted', facts.join(' · ')));

        var focus = state.focus;
        if (focus) {
            var line = 'Same task: ' + plural(focus.inView, 'measured request') + ' highlighted in this view';
            if (focus.hidden) line += ' · ' + num(focus.hidden) + ' more hidden by the filters';
            if (state.data.points_omitted) line += ' · older requests beyond the point cap are not loaded';
            line += '. They share a task; they are not joined, because one task can run parallel reviews, '
                + 'providers or sources that are not one growing context.';
            section.appendChild(el('p', 'meta focus-line', line));
        } else if (!point.task) {
            section.appendChild(el('p', 'meta muted', 'No task was recorded with this request, so nothing else is highlighted.'));
        }

        var technical = el('details', 'inline-details');
        bindDisclosure(technical, 'technical');
        var summary = el('summary', null, 'Technical detail');
        summary.setAttribute('data-focus', 'technical');
        technical.appendChild(summary);
        var body = el('div', 'detail-body');
        detailRow(body, 'Usage recorded or last updated', fullTime(point.t), {
            note: 'The time of the latest accounting write for this request. A late receipt or a price '
                + 'refinement moves it, so it is neither the send time nor a latency.'
        });
        detailRow(body, 'State', point.state + ' — ' + (STATE_LABELS[point.state] || 'unknown'));
        detailRow(body, 'Output tokens', num(point.completion_tokens));
        detailRow(body, 'Cache reads (reported)', num(point.cached_tokens));
        detailRow(body, 'Cache writes (reported)', num(point.cache_write_tokens), {
            note: 'As the provider reported them. For some providers they are already part of the input number, '
                + 'for others that is not established, so they are never added to it or turned into a share.'
        });
        detailRow(body, 'Provider', point.provider);
        detailRow(body, 'Recorded by', humanize(point.source), { exact: point.source });
        detailRow(body, 'Work kind', humanize(point.category), { exact: point.category });
        detailRow(body, 'Mode', modeLabel(point.mode), point.mode ? null : {
            note: 'No context-fit measurement was recorded with this request, so its mode is unknown rather than guessed.'
        });
        if (point.profile) detailRow(body, 'Context profile', point.profile);
        if (point.basis) detailRow(body, 'Measurement basis', humanize(point.basis), { exact: point.basis });
        if (isNumber(point.target_total_tokens)) {
            detailRow(body, 'Target total for that round', num(point.target_total_tokens), {
                note: 'Recorded with this request. It is not a live window size and no percentage is derived from it.'
            });
        }
        if (isNumber(point.capacity_total_tokens)) detailRow(body, 'Capacity total for that round', num(point.capacity_total_tokens));
        if (point.target_miss === true) detailRow(body, 'Target miss', 'Yes, recorded');
        if (point.auto_pass === true) detailRow(body, 'Automatic pass used', 'Yes, recorded');
        if (point.late_receipt) detailRow(body, 'Settled by a late receipt', 'Yes, recorded');
        detailRow(body, 'Task key', point.task || 'Not recorded');
        detailRow(body, 'Request key', point.id);
        technical.appendChild(body);
        section.appendChild(technical);
        root.appendChild(section);
    }

    function renderRequests(root) {
        var details = el('details', 'disclosure');
        bindDisclosure(details, 'requests');
        var total = state.points.length;
        var summary = el('summary', null, 'Requests · ' + num(total) + ' in view');
        summary.setAttribute('data-focus', 'requests');
        details.appendChild(summary);
        if (!total) {
            details.appendChild(el('p', 'meta muted', 'No requests match the current view.'));
            root.appendChild(details);
            return;
        }
        var shown = Math.min(state.rows, total);
        var list = el('ul', 'list');
        list.setAttribute('aria-label', 'Requests in view, newest first. Showing ' + shown + ' of ' + total + '.');
        state.points.slice().reverse().slice(0, shown).forEach(function (point) {
            var item = el('li', 'list-item');
            var button = el('button', 'row');
            button.type = 'button';
            button.setAttribute('data-focus', 'row-' + point.id);
            if (point.id === state.selectedId) {
                button.classList.add('row-active');
                button.setAttribute('aria-current', 'true');
            }
            var left = el('span', 'row-main');
            left.appendChild(el('span', 'row-tokens',
                isNumber(point.prompt_tokens) ? num(point.prompt_tokens) : 'no size'));
            left.appendChild(el('span', 'row-model', point.model));
            var right = el('span', 'row-meta', clock(point.t) + ' · ' + humanize(point.category)
                + ' · ' + modeLabel(point.mode) + (point.state === 'settled' ? '' : ' · ' + point.state));
            right.title = point.category + ' · ' + point.state;
            button.appendChild(left);
            button.appendChild(right);
            on(button, 'click', function () { selectPoint(point.id); });
            item.appendChild(button);
            list.appendChild(item);
        });
        details.appendChild(list);
        if (total > ROWS_COLLAPSED) {
            var actions = el('div', 'row-actions');
            if (shown < total) {
                var more = el('button', 'button button-quiet', 'Show more (' + (total - shown) + ' left)');
                more.type = 'button';
                more.setAttribute('data-focus', 'rows-more');
                on(more, 'click', function () {
                    state.rows = Math.min(total, state.rows + ROWS_STEP);
                    render();
                });
                actions.appendChild(more);
            }
            if (shown > ROWS_COLLAPSED) {
                var less = el('button', 'button button-quiet', 'Show less');
                less.type = 'button';
                less.setAttribute('data-focus', 'rows-less');
                on(less, 'click', function () {
                    state.rows = ROWS_COLLAPSED;
                    render();
                });
                actions.appendChild(less);
            }
            details.appendChild(actions);
        }
        root.appendChild(details);
    }

    function aboutBlock(parent, title, lines) {
        if (!lines.length) return;
        parent.appendChild(el('p', 'meta strong-line', title));
        var list = el('ul', 'notes');
        lines.forEach(function (text) { list.appendChild(el('li', null, text)); });
        parent.appendChild(list);
    }

    function renderAbout(root) {
        var details = el('details', 'disclosure');
        bindDisclosure(details, 'about');
        var summary = el('summary', null, 'About this data');
        summary.setAttribute('data-focus', 'about');
        details.appendChild(summary);
        var data = state.data;
        if (data) {
            var source = data.source || {};
            var h = data.horizon || {};
            var c = data.counters || {};
            var x = c.excluded || {};
            var lines = [];
            if (source.kind === 'usage_store') {
                lines.push('The install’s usage store, read read-only in one short transaction'
                    + (isNumber(source.transaction_ms) ? ' (' + source.transaction_ms + ' ms)' : '')
                    + '. It keeps one row per request and updates that row as accounting completes.');
                lines.push('Categories come from the store’s category summaries, plus unnamed categories. '
                    + 'Legacy-only named categories may be absent: their rows can be missing from counts and '
                    + 'the newest-record time. These facts describe the enumerated categories, not all store rows.');
            } else {
                lines.push('The retired usage journal. Core stopped writing it when the usage store was introduced; '
                    + 'this install has no store file, so this is the journal’s retained tail and may be historical.');
            }
            lines.push('Read at ' + fullTime(source.read_at_ms) + '. Newest record: '
                + (isNumber(source.newest_record_ms) ? fullTime(source.newest_record_ms) : 'none with a usable time')
                + '. An old newest record can simply mean nothing ran since.');
            if (source.context_unread) {
                lines.push(plural(source.context_unread, 'row') + ' carried metadata too large or unreadable to read, '
                    + 'so their mode is Unknown.');
            }
            aboutBlock(details, 'Source', lines);

            lines = [];
            if (isNumber(h.cutoff_ms)) {
                lines.push('Horizon: ' + horizonSpec(h.selected).full + ', ' + fullTime(h.cutoff_ms) + ' to '
                    + fullTime(h.now_ms) + '.');
            } else {
                lines.push('Horizon: available requests, newest first, up to the read bound.');
            }
            if (h.selection_complete === true) {
                lines.push(source.kind === 'usage_store'
                    ? 'Complete within the enumerated categories: every row in this span with a usable time was read. '
                        + 'Core summarises physical-request categories, so this covers eligible physical requests; '
                        + 'it does not guarantee complete legacy counts.'
                    : 'Complete for the retained journal: every row of this span with a usable time was read.');
            } else if (h.selection_complete === false) {
                lines.push('Not complete: ' + partialText(h, data) + '.');
            } else {
                lines.push('Selection completeness is unknown.');
            }
            lines.push(plural(h.records_selected || 0, 'row') + ' selected, '
                + plural(h.attempts_selected || 0, 'request') + ' among them'
                + (isNumber(h.selected_from_ms) ? ', recorded ' + fullTime(h.selected_from_ms) + ' to '
                    + fullTime(h.selected_to_ms) : '') + '.');
            if (data.points_omitted) lines.push(plural(data.points_omitted, 'older request') + ' beyond the point cap were not sent.');
            if (h.unknown_timestamp) {
                lines.push(plural(h.unknown_timestamp, 'row') + (h.unknown_timestamp_capped ? ' or more' : '')
                    + ' carry no usable time, so no horizon can place them.');
            }
            if (h.ahead_of_anchor) lines.push(plural(h.ahead_of_anchor, 'row') + ' recorded after the read instant.');
            aboutBlock(details, 'Selection', lines);

            lines = [num(c.registered_attempts) + ' requests registered; ' + num(c.sent_attempts)
                + ' of them were sent to a provider.'];
            ['settled', 'dispatched', 'unresolved', 'reserved', 'released'].forEach(function (key) {
                var count = c.by_state ? c.by_state[key] : 0;
                if (count) lines.push(num(count) + ' ' + STATE_LABELS[key] + (key === 'settled'
                    ? ' — ' + num(c.measured) + ' with an input size, ' + num(c.settled_without_tokens) + ' without'
                    : ''));
            });
            aboutBlock(details, 'Requests in the selection, before filters', lines);

            lines = [];
            if (x.subscription_sessions) lines.push(plural(x.subscription_sessions, 'subscription session total')
                + ' — one aggregate per delegated session, not a request.');
            if (x.external_unmetered) lines.push(plural(x.external_unmetered, 'external unmetered dispatch', 'external unmetered dispatches') + '.');
            if (x.baseline_rows) lines.push(plural(x.baseline_rows, 'compaction aggregate row') + ' folding '
                + num(x.folded_attempts) + ' older requests' + (x.baselines_without_header ? ' at least' : '')
                + ' — a sum over many requests, never one request.');
            if (x.legacy_rows) lines.push(plural(x.legacy_rows, 'legacy imported row') + '.');
            if (x.unknown_kind) lines.push(plural(x.unknown_kind, 'row') + ' of a kind this version does not recognise.');
            if (x.attempts_without_state) lines.push(plural(x.attempts_without_state, 'request row')
                + ' without a recognisable state.');
            aboutBlock(details, 'Counted, never drawn', lines);
        }
        aboutBlock(details, 'How to read this', [
            'One dot is one physical model request that finished with a reported input size. Missing sizes are '
            + 'counted, never drawn as zero; an explicit zero is drawn.',
            'The time axis is when usage was recorded or last updated. Late accounting can move a request later; '
            + 'it is not the send time and the gap between writes is not a latency.',
            'Median and p95 describe exactly the dots in view and move with the filters.',
            'Selecting a request highlights the other requests of the same task. They are never joined: '
            + 'one task can run parallel reviews, providers or sources, and a shared model and work kind does not '
            + 'prove one growing context.',
            'Cache reads and writes are shown as the provider reported them, never added to the input or turned into a share.',
            'Mode comes from the context-fit measurement recorded with a request; without one it stays Unknown.',
            'No cost, no window-fill percentage, no per-section contribution and no compaction cause is shown: the '
            + 'record holds no exact fact to derive them from. A drop in size is consistent with compaction, a '
            + 'shorter round, another model or a branch of work ending alike.'
        ]);
        if (data && data.limits) {
            var limits = data.limits;
            var bounds = [];
            if (isNumber(limits.max_rows)) bounds.push('at most ' + num(limits.max_rows) + ' rows per read');
            if (isNumber(limits.max_points)) bounds.push('at most ' + num(limits.max_points) + ' points per answer');
            if (isNumber(limits.max_bytes_per_refresh)) bounds.push('at most ' + num(limits.max_bytes_per_refresh / 1048576) + ' MiB of the journal per read');
            if (bounds.length) aboutBlock(details, 'Bounds', [bounds.join(', ') + '. A bound that bites is named above.']);
        }
        root.appendChild(details);
    }

    function partialText(h, data) {
        var reasons = h.partial_reasons || [];
        var from = isNumber(h.covered_from_ms) ? ' It reaches back to ' + fullTime(h.covered_from_ms) : '';
        var words = reasons.map(function (reason) {
            if (reason === 'row_cap') return 'the newest ' + num(data.limits && data.limits.max_rows) + ' rows were read and more exist';
            if (reason === 'journal_tail') return 'the journal’s retained rows do not reach back that far';
            if (reason === 'category_cap') return 'more work kinds exist than one read follows';
            if (reason === 'byte_cap') return 'the metadata read bound was reached';
            if (reason === 'time_budget' || reason === 'step_budget') return 'the read reached its time bound';
            return 'a read bound was reached';
        });
        return (words.join('; ') || 'a read bound was reached') + '.' + from;
    }

    /* A rebuild while a native dropdown is open would close it under the
     * owner's hands, so such a render waits for that control to lose focus. */
    function renderSoon() {
        var active = document.activeElement;
        var root = document.getElementById('root');
        if (active && root && root.contains(active) && active.tagName === 'SELECT') {
            pendingRender = true;
            paintStatus();
            return;
        }
        render();
    }

    function render() {
        if (disposed) return;
        pendingRender = false;
        // Status, derived values and the chart must describe one answer. While
        // a SELECT holds the rebuild, theme/resize paints still use the old one.
        if (pendingData) {
            state.data = pendingData.data;
            state.receivedAt = pendingData.receivedAt;
            pendingData = null;
            var still = (state.data.points || []).some(function (p) { return p.id === state.selectedId; });
            if (!still) state.selectedId = null;
        }
        derive();
        var root = document.getElementById('root');
        if (!root) return;

        // Keep the keyboard where it was: a rebuild must not drop focus.
        var active = document.activeElement;
        var focusWanted = active && root.contains(active) && active.getAttribute
            ? active.getAttribute('data-focus') : null;

        clearScheduled();
        observers.forEach(function (observer) { observer.disconnect(); });
        observers = [];
        chartDraws = [];
        listeners = listeners.filter(function (entry) {
            if (root.contains(entry[0])) {
                entry[0].removeEventListener(entry[1], entry[2], entry[3]);
                return false;
            }
            return true;
        });
        root.textContent = '';

        renderHeader(root);
        renderToolbar(root);
        if (state.data) root.appendChild(el('p', 'meta muted coverage', coverageSentence()));
        renderChart(root);
        if (state.data) {
            renderSelection(root);
            renderRequests(root);
        }
        renderAbout(root);
        var live = el('p', 'sr-only');
        live.setAttribute('aria-live', 'polite');
        root.appendChild(live);
        nodes.live = live;
        restoreFocus(root, focusWanted);
    }

    function restoreFocus(root, key) {
        if (!key) return;
        var next = root.querySelector('[data-focus="' + key + '"]');
        if (!next || next.disabled) return;
        try { next.focus({ preventScroll: true }); } catch (error) { /* older engine */ }
    }

    // ---------------------------------------------------------------- style

    function installStyle() {
        var css = [
            // Two named palettes, by value from web/ui.css. Dark is the default
            // for hosts without the theme bridge.
            ':root{color-scheme:dark;--lens-text:#e2e8f0;--lens-meta:rgba(255,255,255,0.68);',
            '--lens-secondary:rgba(255,255,255,0.54);--lens-disabled:rgba(255,255,255,0.38);',
            '--lens-border:rgba(255,255,255,0.10);--lens-control:rgba(255,255,255,0.04);',
            '--lens-hover:rgba(255,255,255,0.08);--lens-surface:rgba(255,255,255,0.03);',
            '--lens-accent-bg:rgba(201,53,69,0.18);--lens-accent-ring:rgba(201,53,69,0.40);',
            '--lens-focus-ring:rgba(201,53,69,0.55);--lens-tooltip:rgba(18,20,26,0.98);',
            '--lens-ok:#6ee7b7;--lens-warn:#fcd34d;--lens-error:#fca5a5;',
            '--lens-chart-text:#a8b0bd;--lens-grid:rgba(255,255,255,0.10);--lens-point:rgba(226,232,240,0.55);',
            '--lens-point-dim:rgba(226,232,240,0.16);--lens-focus:#f07a86;--lens-selected:#f07a86;',
            '--lens-reference:rgba(168,176,189,0.75);--lens-ring:#0d0b0f;}',
            ':root[data-theme=light]{color-scheme:light;--lens-text:#20232b;--lens-meta:#555d69;',
            '--lens-secondary:#626977;--lens-disabled:#89909b;',
            '--lens-border:rgba(32,35,43,0.12);--lens-control:rgba(32,35,43,0.03);',
            '--lens-hover:rgba(32,35,43,0.06);--lens-surface:rgba(32,35,43,0.025);',
            '--lens-accent-bg:rgba(201,53,69,0.10);--lens-accent-ring:rgba(168,44,59,0.35);',
            '--lens-focus-ring:#a82c3b;--lens-tooltip:#ffffff;',
            '--lens-ok:#176b49;--lens-warn:#805300;--lens-error:#b42332;',
            '--lens-chart-text:#4a515d;--lens-grid:rgba(32,35,43,0.14);--lens-point:rgba(32,35,43,0.50);',
            '--lens-point-dim:rgba(32,35,43,0.13);--lens-focus:#a82c3b;--lens-selected:#a82c3b;',
            '--lens-reference:rgba(74,81,93,0.75);--lens-ring:#ffffff;}',
            // Transparent: the host's own card surface shows through, in both
            // themes. No viewport heights: the card follows its content.
            'html,body{margin:0;padding:0;background:transparent;}',
            'body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,sans-serif;',
            'color:var(--lens-text);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased;}',
            '#root{box-sizing:border-box;padding:12px 14px 14px;display:flex;flex-direction:column;gap:10px;max-width:100%;}',
            '*{box-sizing:border-box;min-width:0;}',
            'p{margin:0;overflow-wrap:anywhere;}',
            '.meta{font-size:12px;line-height:1.4;}',
            '.muted{color:var(--lens-meta);}',
            '.sr-only{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);border:0;}',
            '.header{display:flex;align-items:center;justify-content:space-between;gap:12px;}',
            '.status{display:flex;align-items:baseline;gap:8px;font-size:12px;line-height:1.4;color:var(--lens-meta);flex:1;}',
            '.dot{flex:0 0 auto;width:8px;height:8px;border-radius:999px;background:var(--lens-disabled);transform:translateY(-1px);}',
            '.dot-ok{background:var(--lens-ok);}.dot-warn{background:var(--lens-warn);}.dot-error{background:var(--lens-error);}',
            '.button{font:inherit;font-size:13px;line-height:1.3;min-height:30px;padding:4px 13px;border-radius:999px;',
            'border:1px solid var(--lens-border);background:var(--lens-control);color:var(--lens-text);cursor:pointer;}',
            '.button:hover:not(:disabled){background:var(--lens-hover);}',
            '.button:disabled{color:var(--lens-disabled);cursor:default;}',
            '.button-quiet{min-height:26px;padding:2px 11px;font-size:12px;}',
            '.button:focus-visible,.control:focus-visible,.row:focus-visible,summary:focus-visible,',
            '.segment:focus-visible,.canvas:focus-visible{outline:2px solid var(--lens-focus-ring);outline-offset:2px;}',
            '.toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;}',
            '.segmented{display:inline-flex;flex:0 0 auto;padding:2px;gap:2px;border-radius:999px;',
            'border:1px solid var(--lens-border);background:var(--lens-surface);}',
            '.segment{font:inherit;font-size:13px;line-height:1.3;min-height:28px;min-width:36px;padding:3px 10px;',
            'border:0;border-radius:999px;background:transparent;color:var(--lens-meta);cursor:pointer;}',
            '.segment:hover{color:var(--lens-text);background:var(--lens-hover);}',
            '.segment-on{background:var(--lens-accent-bg);color:var(--lens-text);box-shadow:inset 0 0 0 1px var(--lens-accent-ring);}',
            '.filters{flex:1 1 360px;display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:8px;}',
            '.control{font:inherit;font-size:13px;min-height:30px;width:100%;padding:3px 8px;border-radius:8px;',
            'border:1px solid var(--lens-border);background:var(--lens-control);color:var(--lens-text);',
            'text-overflow:ellipsis;}',
            '.coverage{margin-top:-2px;}',
            '.chart{position:relative;height:260px;overflow:hidden;}',
            '@media (max-width:640px){.chart{height:220px;}}',
            '@media (max-width:400px){.chart{height:200px;}}',
            '.canvas{width:100%;height:100%;display:block;border-radius:6px;}',
            '.chart-message{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;',
            'justify-content:center;gap:6px;padding:12px;text-align:center;}',
            '.message-title{font-size:14px;line-height:1.4;font-weight:600;}',
            '.tooltip{position:absolute;transform:translateX(-50%);pointer-events:none;font-size:12px;line-height:1.35;',
            'padding:5px 8px;border-radius:8px;border:1px solid var(--lens-border);background:var(--lens-tooltip);',
            'color:var(--lens-text);max-width:min(280px,90%);}',
            '.selection{display:flex;flex-direction:column;gap:4px;padding:10px 12px;border-radius:10px;',
            'background:var(--lens-surface);border:1px solid var(--lens-border);}',
            '.selection-lead{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;}',
            '.selection-lead .button{margin-left:auto;}',
            '.lead-value{font-size:16px;line-height:1.3;font-weight:600;}',
            '.focus-line{color:var(--lens-text);}',
            '.disclosure{border-top:1px solid var(--lens-border);padding-top:8px;}',
            'summary{cursor:pointer;font-size:14px;line-height:1.4;color:var(--lens-text);}',
            '.disclosure[open]>summary{margin-bottom:8px;}',
            '.inline-details>summary{font-size:12px;color:var(--lens-meta);}',
            '.inline-details[open]>summary{margin-bottom:6px;}',
            '.detail-body{display:flex;flex-direction:column;gap:4px;}',
            '.detail-row{display:flex;align-items:baseline;justify-content:space-between;gap:10px;}',
            '.detail-label{font-size:12px;line-height:1.4;color:var(--lens-meta);}',
            '.detail-value{font-size:14px;text-align:right;overflow-wrap:anywhere;}',
            '.detail-note{font-size:12px;line-height:1.4;color:var(--lens-meta);margin:-2px 0 4px;}',
            '.list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px;}',
            '.row{display:flex;width:100%;align-items:baseline;justify-content:space-between;gap:10px;font:inherit;',
            'text-align:left;padding:5px 8px;border-radius:8px;border:1px solid transparent;background:transparent;',
            'color:var(--lens-text);cursor:pointer;}',
            '.row:hover{background:var(--lens-hover);}',
            '.row-active{background:var(--lens-accent-bg);border-color:var(--lens-accent-ring);}',
            '.row-main{display:flex;flex:1 1 auto;align-items:baseline;gap:8px;min-width:0;}',
            '.row-tokens{flex:0 0 auto;font-size:14px;font-weight:600;white-space:nowrap;}',
            '.row-model{font-size:12px;line-height:1.35;color:var(--lens-secondary);overflow:hidden;',
            'text-overflow:ellipsis;white-space:nowrap;}',
            '.row-meta{flex:0 1 auto;font-size:12px;line-height:1.35;color:var(--lens-meta);text-align:right;}',
            '@media (max-width:480px){.row{flex-direction:column;align-items:stretch;gap:1px;}.row-meta{text-align:left;}}',
            '.row-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px;}',
            '.strong-line{color:var(--lens-text);font-weight:600;margin-top:6px;}',
            '.notes{margin:4px 0 0;padding-left:17px;display:flex;flex-direction:column;gap:4px;',
            'font-size:12px;line-height:1.4;color:var(--lens-meta);}'
        ].join('');
        var style = document.createElement('style');
        style.textContent = css;
        document.head.appendChild(style);
    }

    // -------------------------------------------------------------- startup

    installStyle();
    readChartColors();
    var offTheme = window.OuroborosWidget && typeof window.OuroborosWidget.onTheme === 'function'
        ? window.OuroborosWidget.onTheme(function (theme) {
            if (disposed) return;
            document.documentElement.dataset.theme = theme === 'light' ? 'light' : 'dark';
            readChartColors();
            // A theme change repaints the canvas; it never rebuilds controls,
            // moves focus or closes a disclosure.
            repaintCharts();
        }) : null;
    if (!document.getElementById('root')) {
        var rootNode = document.createElement('div');
        rootNode.id = 'root';
        document.body.appendChild(rootNode);
    }
    render();
    load();

    var poll = setInterval(function () {
        if (disposed || document.visibilityState !== 'visible') return;
        load();
    }, POLL_MS);
    timers.push(poll);
    // Coming back to a page that was hidden for longer than one poll reads at
    // once instead of showing an old answer until the next tick.
    on(document, 'visibilitychange', function () {
        if (disposed || document.visibilityState !== 'visible') return;
        if (!state.loading && Date.now() - state.receivedAt >= POLL_MS) load();
    });

    if (typeof window.__ouroWidgetOnDispose === 'function') {
        window.__ouroWidgetOnDispose(function () {
            disposed = true;
            if (typeof offTheme === 'function') offTheme();
            timers.forEach(clearInterval);
            timers = [];
            clearScheduled();
            observers.forEach(function (observer) { observer.disconnect(); });
            observers = [];
            chartDraws = [];
            listeners.forEach(function (entry) {
                entry[0].removeEventListener(entry[1], entry[2], entry[3]);
            });
            listeners = [];
            requestTimers.forEach(clearTimeout);
            requestTimers.clear();
            controllers.forEach(abort);
            controllers.clear();
            dataController = null;
            pendingData = null;
            inFlight = null;
        });
    }
})();
