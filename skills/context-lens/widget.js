/* Context Lens — module widget.
 *
 * Runs as a classic script inside the host's opaque-origin srcdoc frame, so:
 * no import/export at top level, no storage, no scriptable network. Every
 * request goes through OuroborosWidget.fetch and stays under this skill's own
 * route prefix. Colours and type sizes mirror docs/DESIGN.md by value because
 * the frame cannot reach web/style.css.
 *
 * Layout intent: the horizon, then the instrument. The card is one screen —
 * a fixed 760px frame that scrolls itself — with the horizon row first, four
 * figures, the chart, then list and detail. Everything that explains a number
 * is one disclosure away rather than unrolled at once.
 */
(function () {
    'use strict';

    var ROOT = '/api/extensions/context-lens/';
    var POLL_MS = 60000;
    var ROWS_COLLAPSED = 6;      // compact by default
    var ROWS_STEP = 12;          // one "Show more" press

    /* The horizon is cut on the server, over the retained records, BEFORE the
     * display limit — so the points, the counters, the facets and the coverage
     * line all describe one selection. 'available' means everything this
     * bounded reader still holds, which is not the same as all history. */
    var HORIZONS = [
        { key: '1h', label: '1h', full: 'the last hour' },
        { key: '6h', label: '6h', full: 'the last 6 hours' },
        { key: '24h', label: '24h', full: 'the last 24 hours' },
        { key: '7d', label: '7d', full: 'the last 7 days' },
        { key: 'available', label: 'Available', full: 'everything still in the read window' }
    ];

    var state = {
        data: null,
        error: null,
        loading: false,
        selected: null,
        trajectory: null,
        trajectoryTask: '',
        horizon: 'available',
        filters: { model: 'all', kind: 'all', origin: 'all', mode: 'all' },
        points: [],
        plotted: [],
        stats: null,
        view: null,
        rows: ROWS_COLLAPSED
    };

    var controllers = new Set();
    var timers = [];
    var observers = [];
    var listeners = [];
    var scheduled = [];
    var disposed = false;
    var inFlight = null;
    var inFlightHorizon = null;  // the horizon the in-flight overview asks about
    var dataSeq = 0;             // generation of the current overview request
    var dataController = null;   // so a superseded overview can be cut short
    var trajectorySeq = 0;       // guards against a superseded trajectory answer
    var trajectoryController = null;

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

    function num(value) {
        if (value === null || value === undefined || !isFinite(value)) return '—';
        return Math.round(value).toLocaleString('en-US');
    }

    function compact(value) {
        if (value === null || value === undefined || !isFinite(value)) return '—';
        var n = Math.round(value);
        if (n >= 1000000) return (n / 1000000).toFixed(n >= 10000000 ? 0 : 1) + 'M';
        if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k';
        return String(n);
    }

    function clockTime(ms) {
        if (!ms) return '—';
        var d = new Date(ms);
        var pad = function (v) { return String(v).padStart(2, '0'); };
        return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    }

    function axisTime(ms, span) {
        if (span < 86400000) return clockTime(ms);
        return new Date(ms).toLocaleString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});
    }

    function fullTime(ms) {
        if (!ms) return 'No usable timestamp';
        return new Date(ms).toLocaleString('en-US', { hour12: false });
    }

    /* UTC on purpose: the anchor a horizon is measured back from is a fact about
     * the record, and a local rendering of it would differ from the timestamps
     * the ledger holds. */
    function utcClock(ms) {
        if (!ms) return '—';
        return new Date(ms).toISOString().slice(11, 16) + ' UTC';
    }

    function utcDay(ms) {
        if (!ms) return '—';
        return new Date(ms).toISOString().slice(0, 10) + ' ' + new Date(ms).toISOString().slice(11, 16) + ' UTC';
    }

    function durationLabel(ms) {
        if (!isFinite(ms) || ms < 0) return '—';
        var minutes = Math.round(ms / 60000);
        if (minutes < 1) return 'under a minute';
        if (minutes < 60) return minutes + ' min';
        var hours = Math.floor(minutes / 60);
        if (hours < 24) {
            var rest = minutes % 60;
            return hours + ' h' + (rest ? ' ' + rest + ' min' : '');
        }
        var days = Math.floor(hours / 24);
        var restHours = hours % 24;
        return days + ' d' + (restHours ? ' ' + restHours + ' h' : '');
    }

    function horizonSpec(key) {
        for (var i = 0; i < HORIZONS.length; i += 1) {
            if (HORIZONS[i].key === key) return HORIZONS[i];
        }
        return HORIZONS[HORIZONS.length - 1];
    }

    function bytesLabel(value) {
        if (!value) return '0 B';
        if (value >= 1048576) return (value / 1048576).toFixed(1) + ' MiB';
        if (value >= 1024) return (value / 1024).toFixed(1) + ' KiB';
        return value + ' B';
    }

    /* Presentation only. The exact recorded token is kept as the title and is
     * shown verbatim in the request's technical detail, so nothing the ledger
     * actually holds is replaced by a prettier word. */
    function humanize(token) {
        var text = String(token === null || token === undefined ? '' : token);
        if (!text) return 'unknown';
        var spaced = text.replace(/[_-]+/g, ' ').trim();
        if (!spaced) return text;
        return spaced.charAt(0).toUpperCase() + spaced.slice(1);
    }

    function modeLabel(mode) {
        if (mode === 'max') return 'Max';
        if (mode === 'low') return 'Low';
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

    // ------------------------------------------------------------- requests

    /* The caller may hand in its own controller so it can cut the request short
     * when the answer it would produce has already been superseded. */
    function request(path, controller) {
        controller = controller || new AbortController();
        controllers.add(controller);
        var api = window.OuroborosWidget && window.OuroborosWidget.fetch
            ? window.OuroborosWidget.fetch
            : window.fetch;
        return api(ROOT + path, { signal: controller.signal, timeoutMs: 20000 })
            .then(function (response) {
                if (!response.ok) throw new Error('Request failed with status ' + response.status);
                return response.json();
            })
            .finally(function () { controllers.delete(controller); });
    }

    function abort(controller) {
        if (!controller) return;
        try { controller.abort(); } catch (error) { /* already settled */ }
    }

    function forgetTrajectory() {
        // Bumping the sequence retires any answer still in flight, so a reply
        // for the previous selection can never land on the new one; aborting the
        // controller also stops the request itself, so pressing through a list
        // cannot stack one live fetch per press.
        trajectorySeq += 1;
        abort(trajectoryController);
        trajectoryController = null;
        state.trajectory = null;
        state.trajectoryTask = '';
    }

    /* Every overview request carries a generation. Only the current generation
     * may write to state, so an older answer can never undo a newer click — and
     * a horizon change supersedes the request in flight rather than waiting for
     * it, because that answer describes a span the user has already left. */
    function load(cold) {
        if (disposed) return Promise.resolve();
        var requested = state.horizon;
        if (inFlight) {
            if (requested === inFlightHorizon && !cold) return inFlight;
            abort(dataController);
        }
        var token = dataSeq + 1;
        dataSeq = token;
        inFlightHorizon = requested;
        var controller = new AbortController();
        dataController = controller;
        state.loading = true;
        render();
        inFlight = request('data?limit=1500&horizon=' + encodeURIComponent(requested)
            + (cold ? '&refresh=1' : ''), controller)
            .then(function (payload) {
                if (disposed || token !== dataSeq) return;
                // The user moved the horizon while this was in flight: this
                // answer describes the span they left, so it is not adopted.
                if (requested !== state.horizon) return;
                if (payload && payload.ok) {
                    // The answer states the horizon it actually applied. It is
                    // adopted only when it agrees with what is selected now;
                    // anything else would silently overwrite a live click.
                    var applied = payload.horizon && payload.horizon.selected;
                    if (applied && applied !== state.horizon) return;
                    state.data = payload;
                    state.error = null;
                    if (state.selected) {
                        var fresh = payload.points.filter(function (p) {
                            return p.id === state.selected.id;
                        })[0];
                        if (fresh) {
                            state.selected = fresh;       // same request, current facts
                        } else {
                            state.selected = null;
                            forgetTrajectory();
                        }
                    }
                } else {
                    state.data = null;
                    state.error = (payload && payload.message) || 'Telemetry is unavailable.';
                }
            })
            .catch(function (error) {
                if (disposed || token !== dataSeq) return;
                if (error && error.name === 'AbortError') return;
                state.data = null;
                state.error = 'Could not reach the Context Lens route in this install.';
            })
            .finally(function () {
                // A superseded request must not clear the flags that now belong
                // to the request which replaced it.
                if (token !== dataSeq) return;
                inFlight = null;
                inFlightHorizon = null;
                dataController = null;
                state.loading = false;
                if (!disposed) render();
            });
        return inFlight;
    }

    function loadTrajectory(task) {
        if (!task) return;
        // Defence in depth: the sequence retires a late answer, the controller
        // stops the request that would have produced it.
        trajectorySeq += 1;
        abort(trajectoryController);
        var token = trajectorySeq;
        state.trajectoryTask = task;
        state.trajectory = 'loading';
        var controller = new AbortController();
        trajectoryController = controller;
        render();
        request('trajectory?task=' + encodeURIComponent(task)
            + '&horizon=' + encodeURIComponent(state.horizon), controller)
            .then(function (payload) {
                // A later selection — or a second press for the same task —
                // moved the sequence on; this answer is stale by construction.
                if (disposed || token !== trajectorySeq) return;
                state.trajectory = payload && payload.ok ? payload : null;
                render();
            })
            .catch(function (error) {
                if (disposed || token !== trajectorySeq) return;
                if (error && error.name === 'AbortError') return;
                state.trajectory = null;
                render();
            })
            .finally(function () {
                if (trajectoryController === controller) trajectoryController = null;
            });
    }

    /* Changing the horizon changes the population, so the selection, the
     * trajectory and the paged list all start again from the new answer rather
     * than surviving into a window they may not belong to. */
    function selectHorizon(key) {
        if (key === state.horizon) return;
        state.horizon = key;
        state.selected = null;
        state.rows = ROWS_COLLAPSED;
        forgetTrajectory();
        load(false);
    }

    function select(point) {
        state.selected = point;
        forgetTrajectory();
        if (point && point.task) {
            loadTrajectory(point.task);   // renders on its own
        } else {
            render();
        }
    }

    // ------------------------------------------------------------ filtering

    function applyFilters() {
        var points = (state.data && state.data.points) || [];
        var f = state.filters;
        state.points = points.filter(function (point) {
            if (f.model !== 'all' && point.model !== f.model) return false;
            if (f.kind !== 'all' && point.category !== f.kind) return false;
            if (f.origin !== 'all' && point.source !== f.origin) return false;
            if (f.mode !== 'all' && (point.mode || 'unknown') !== f.mode) return false;
            return true;
        });
        state.plotted = state.points.filter(function (point) {
            return point.state === 'settled' && typeof point.prompt_tokens === 'number' && point.t;
        });

        // Coverage for the view the owner is actually looking at, computed from
        // the same points the chart draws — not from the whole-window counters,
        // which describe a different population as soon as a filter is set.
        var view = {
            total: state.points.length, measured: 0, settledWithoutSize: 0,
            withoutTime: 0, inFlight: 0, unresolved: 0, released: 0
        };
        state.points.forEach(function (point) {
            var sized = typeof point.prompt_tokens === 'number';
            if (point.state === 'settled') {
                if (!sized) view.settledWithoutSize += 1;
                else if (!point.t) view.withoutTime += 1;
                else view.measured += 1;
            } else if (point.state === 'reserved' || point.state === 'dispatched') {
                view.inFlight += 1;
            } else if (point.state === 'unresolved') {
                view.unresolved += 1;
            } else if (point.state === 'released') {
                view.released += 1;
            }
        });
        state.view = view;

        var values = state.plotted.map(function (p) { return p.prompt_tokens; })
            .sort(function (a, b) { return a - b; });
        state.stats = values.length ? {
            count: values.length,
            median: quantile(values, 0.5),
            p95: quantile(values, 0.95),
            peak: values[values.length - 1],
            low: values[0]
        } : { count: 0, median: null, p95: null, peak: null, low: null };
        if (state.rows > Math.max(ROWS_COLLAPSED, state.points.length)) {
            state.rows = Math.max(ROWS_COLLAPSED, state.points.length);
        }
    }

    // -------------------------------------------------------------- drawing

    var CHART_INK = 'rgba(226, 232, 240, 0.55)';
    var ACCENT = '#c93545';
    var ACCENT_LIGHT = '#f07a86';
    var GRID = 'rgba(255, 255, 255, 0.07)';
    var META = 'rgba(255, 255, 255, 0.68)';
    var FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif';
    // Clear space demanded between two measured pieces of chart text. Labels are
    // dropped, never shrunk: every chart string stays at the 12px the rest of
    // the card uses.
    var LABEL_GAP = 6;

    function sizeCanvas(canvas) {
        var ratio = window.devicePixelRatio || 1;
        var width = Math.max(200, Math.round(canvas.clientWidth));
        var height = Math.max(110, Math.round(canvas.clientHeight));
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
        var ctx = canvas.getContext('2d');
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.clearRect(0, 0, width, height);
        return { ctx: ctx, width: width, height: height };
    }

    function niceCeil(value) {
        if (!value || value <= 0) return 1000;
        var magnitude = Math.pow(10, Math.floor(Math.log10(value)));
        var steps = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10];
        for (var i = 0; i < steps.length; i += 1) {
            if (value <= steps[i] * magnitude) return steps[i] * magnitude;
        }
        return 10 * magnitude;
    }

    function emptyChart(box, text) {
        box.ctx.fillStyle = META;
        box.ctx.font = '14px ' + FONT;
        box.ctx.textAlign = 'center';
        box.ctx.textBaseline = 'middle';
        box.ctx.fillText(text, box.width / 2, box.height / 2);
    }

    function drawScatter(canvas) {
        var box = sizeCanvas(canvas);
        var ctx = box.ctx;
        // `top` leaves a whole 12px line above the plot for the axis captions.
        // The topmost tick label sits at yOf(yMax) === pad.top, so a caption
        // drawn inside the plot's own top edge lands on top of it — on a narrow
        // card the two read as one garbled string. CAPTION_Y is that line's
        // centre; the gap to the first tick label is (pad.top - CAPTION_Y).
        var pad = { left: 50, right: 12, top: 30, bottom: 28 };
        var CAPTION_Y = 12;
        var plotW = box.width - pad.left - pad.right;
        var plotH = box.height - pad.top - pad.bottom;
        canvas._hits = [];

        var points = state.plotted;
        if (!points.length || plotW <= 0 || plotH <= 0) {
            emptyChart(box, 'No measured requests in this view');
            return;
        }

        var times = points.map(function (p) { return p.t; });
        var tMin = Math.min.apply(null, times);
        var tMax = Math.max.apply(null, times);
        if (tMax === tMin) { tMax = tMin + 1000; tMin -= 1000; }
        var yMax = niceCeil(state.stats.peak * 1.08);

        var xOf = function (t) { return pad.left + ((t - tMin) / (tMax - tMin)) * plotW; };
        var yOf = function (v) { return pad.top + plotH - (v / yMax) * plotH; };

        ctx.font = '12px ' + FONT;
        ctx.textBaseline = 'middle';

        for (var i = 0; i <= 4; i += 1) {
            var value = (yMax / 4) * i;
            var y = yOf(value);
            ctx.strokeStyle = GRID;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(pad.left, Math.round(y) + 0.5);
            ctx.lineTo(pad.left + plotW, Math.round(y) + 0.5);
            ctx.stroke();
            ctx.fillStyle = META;
            ctx.textAlign = 'right';
            ctx.fillText(compact(value), pad.left - 8, y);
        }

        // Typical and 95%-below drawn as reference rules: spread stated, not implied.
        [['Typical', state.stats.median], ['95% below', state.stats.p95]].forEach(function (entry) {
            if (entry[1] === null || entry[1] > yMax) return;
            var y = Math.round(yOf(entry[1])) + 0.5;
            ctx.save();
            ctx.strokeStyle = 'rgba(240, 122, 134, 0.45)';
            ctx.setLineDash(entry[0] === 'Typical' ? [5, 4] : [2, 4]);
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(pad.left + plotW, y);
            ctx.stroke();
            ctx.restore();
            ctx.fillStyle = 'rgba(240, 122, 134, 0.85)';
            ctx.textAlign = 'left';
            ctx.fillText(entry[0], pad.left + 6, y - 8);
        });

        // Both captions live on their own line above the plot, on opposite
        // sides: nothing else is drawn there, so neither can land on a tick
        // label. The right caption is measured against the left one so the two
        // cannot meet on a very narrow canvas either.
        ctx.fillStyle = META;
        ctx.textAlign = 'left';
        ctx.fillText('input tokens', 2, CAPTION_Y);
        var yCaptionRight = 2 + ctx.measureText('input tokens').width;
        var xCaption = 'recorded time →';
        var captionLeft = box.width - 2 - ctx.measureText(xCaption).width;
        if (captionLeft >= yCaptionRight + LABEL_GAP) {
            ctx.textAlign = 'right';
            ctx.fillText(xCaption, box.width - 2, CAPTION_Y);
        }

        // Time labels are measured, not merely clamped to the card edge: four
        // evenly spaced timestamps do not fit a narrow card, and the previous
        // centre-and-clamp ran the last two into each other. The first and last
        // always survive — they are what states the range; an intermediate one
        // is drawn only when its measured box clears both of them.
        ctx.textAlign = 'center';
        var ticks = [];
        for (var k = 0; k <= 3; k += 1) {
            var t = tMin + ((tMax - tMin) / 3) * k;
            var text = axisTime(t, tMax - tMin);
            var half = ctx.measureText(text).width / 2;
            var cx = Math.min(box.width - 2 - half, Math.max(2 + half, xOf(t)));
            ticks.push({ text: text, x: cx, left: cx - half, right: cx + half });
        }
        var last = ticks[ticks.length - 1];
        var drawn = [ticks[0]];
        var prevRight = ticks[0].right;
        for (var m = 1; m < ticks.length - 1; m += 1) {
            if (ticks[m].left >= prevRight + LABEL_GAP
                && ticks[m].right + LABEL_GAP <= last.left) {
                drawn.push(ticks[m]);
                prevRight = ticks[m].right;
            }
        }
        drawn.push(last);
        drawn.forEach(function (tick) {
            ctx.fillText(tick.text, tick.x, box.height - 15);
        });

        // Independent requests: dots only. Nothing is joined here — two
        // consecutive requests may belong to unrelated tasks.
        points.forEach(function (point) {
            var x = xOf(point.t);
            var y = yOf(Math.min(point.prompt_tokens, yMax));
            var selected = state.selected && state.selected.id === point.id;
            ctx.beginPath();
            ctx.arc(x, y, selected ? 4.5 : 2.6, 0, Math.PI * 2);
            ctx.fillStyle = selected ? ACCENT_LIGHT : CHART_INK;
            ctx.fill();
            if (selected) {
                ctx.beginPath();
                ctx.arc(x, y, 8, 0, Math.PI * 2);
                ctx.strokeStyle = ACCENT;
                ctx.lineWidth = 1.5;
                ctx.stroke();
            }
            canvas._hits.push({ x: x, y: y, point: point });
        });
    }

    function drawTrajectory(canvas, payload) {
        var box = sizeCanvas(canvas);
        var ctx = box.ctx;
        var pad = { left: 46, right: 10, top: 10, bottom: 20 };
        var plotW = box.width - pad.left - pad.right;
        var plotH = box.height - pad.top - pad.bottom;
        var all = [];
        (payload.groups || []).forEach(function (g) { all = all.concat(g.points); });
        (payload.related || []).forEach(function (g) { all = all.concat(g.points); });
        ctx.font = '12px ' + FONT;
        ctx.textBaseline = 'middle';
        if (!all.length || plotW <= 0 || plotH <= 0) {
            emptyChart(box, 'No measured requests for this task');
            return;
        }
        var times = all.map(function (p) { return p.t; }).filter(Boolean);
        var tMin = Math.min.apply(null, times);
        var tMax = Math.max.apply(null, times);
        if (tMax === tMin) { tMax = tMin + 1000; tMin -= 1000; }
        var peak = Math.max.apply(null, all.map(function (p) { return p.prompt_tokens; }));
        var yMax = niceCeil(peak * 1.1);
        var xOf = function (t) { return pad.left + ((t - tMin) / (tMax - tMin)) * plotW; };
        var yOf = function (v) { return pad.top + plotH - (v / yMax) * plotH; };

        for (var i = 0; i <= 2; i += 1) {
            var value = (yMax / 2) * i;
            var y = Math.round(yOf(value)) + 0.5;
            ctx.strokeStyle = GRID;
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(pad.left + plotW, y);
            ctx.stroke();
            ctx.fillStyle = META;
            ctx.textAlign = 'right';
            ctx.fillText(compact(value), pad.left - 8, yOf(value));
        }

        // Other tasks under the same root: points only, never joined.
        (payload.related || []).forEach(function (group) {
            group.points.forEach(function (point) {
                ctx.beginPath();
                ctx.arc(xOf(point.t), yOf(Math.min(point.prompt_tokens, yMax)), 2.2, 0, Math.PI * 2);
                ctx.fillStyle = 'rgba(255, 255, 255, 0.42)';
                ctx.fill();
            });
        });

        // This task's own homogeneous runs: joined inside a group only.
        var shades = ['rgba(226, 232, 240, 0.78)', 'rgba(226, 232, 240, 0.56)', 'rgba(226, 232, 240, 0.4)'];
        (payload.groups || []).forEach(function (group, index) {
            var holdsSelection = state.selected && group.points.some(function (p) {
                return p.id === state.selected.id;
            });
            var ink = holdsSelection ? ACCENT_LIGHT : shades[index % shades.length];
            ctx.strokeStyle = ink;
            ctx.lineWidth = holdsSelection ? 1.8 : 1.2;
            ctx.beginPath();
            group.points.forEach(function (point, position) {
                var x = xOf(point.t);
                var y = yOf(Math.min(point.prompt_tokens, yMax));
                if (position === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.stroke();
            group.points.forEach(function (point) {
                ctx.beginPath();
                ctx.arc(xOf(point.t), yOf(Math.min(point.prompt_tokens, yMax)), 2.8, 0, Math.PI * 2);
                ctx.fillStyle = ink;
                ctx.fill();
            });
        });

        // Same rule as the overview chart: the two ends of the range are
        // measured before they are drawn. They are pushed to the outer edges of
        // the plot, and when even that leaves them overlapping on a narrow card
        // only the newest is drawn rather than two strings printed over each
        // other. Neither is shrunk below the chart's 12px.
        ctx.fillStyle = META;
        var startText = axisTime(tMin, tMax - tMin);
        var endText = axisTime(tMax, tMax - tMin);
        var startWidth = ctx.measureText(startText).width;
        var endWidth = ctx.measureText(endText).width;
        var right = pad.left + plotW;
        if (startWidth + endWidth + LABEL_GAP <= plotW) {
            ctx.textAlign = 'left';
            ctx.fillText(startText, pad.left, box.height - 8);
        }
        ctx.textAlign = 'right';
        ctx.fillText(endText, right, box.height - 8);
    }

    /* One draw per animation frame per chart, and never for a detached node.
     *
     * The observer redraws only when the box it watches actually changed size.
     * A canvas redraw writes no layout, so a same-size notification can only
     * come from something else on the page moving; answering it with another
     * draw is how an observer ends up delivering notifications inside its own
     * callback (the error WebKit reports as "ResizeObserver loop completed with
     * undelivered notifications"). Nothing is caught or silenced here — the
     * redundant work simply is not done. The card's frame height is fixed by the
     * manifest, so the host mounts no auto-height observer above this one. */
    function liveChart(container, canvas, draw) {
        schedule(draw);
        if (typeof ResizeObserver !== 'function') return;
        var pending = false;
        // Seeded from the box as it is now, so the observer's own first
        // notification — which reports exactly that size — costs no second draw.
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

    // ------------------------------------------------------------ rendering

    function statTile(label, value, note, tip) {
        var tile = el('div', 'tile');
        if (tip) tile.title = tip;
        tile.appendChild(el('div', 'tile-value', value));
        tile.appendChild(el('div', 'tile-label', label));
        tile.appendChild(el('div', 'tile-note', note || ' '));
        return tile;
    }

    function addSelect(entry, parent) {
        var picker = el('select', 'control');
        picker.setAttribute('aria-label', entry.label);
        picker.setAttribute('data-focus', 'filter-' + entry.key);
        var options = entry.options.slice();
        // A value that is filtering right now always stays selectable, even if
        // the newest read no longer contains it — otherwise the control would
        // show "All" while a filter is still in force.
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
            state.selected = null;
            forgetTrajectory();
            render();
        });
        var wrap = el('label', 'field');
        wrap.appendChild(el('span', 'field-label', entry.label));
        wrap.appendChild(picker);
        parent.appendChild(wrap);
    }

    /* One row of buttons, one pressed. A segmented control rather than a select
     * because the horizon is the first thing the card is read through, and the
     * four spans plus "Available" fit on one line at this width. */
    function renderHorizon(parent) {
        var bar = el('div', 'horizon');
        var label = el('span', 'field-label', 'Horizon');
        label.id = 'horizon-label';
        bar.appendChild(label);
        var group = el('div', 'segmented');
        group.setAttribute('role', 'group');
        group.setAttribute('aria-labelledby', 'horizon-label');
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
        parent.appendChild(bar);
    }

    /* What the selected horizon really covers. The requested span is never
     * presented as the delivered span: this reader holds a bounded tail of one
     * file, so when the retained data starts later than the cutoff the observed
     * range is stated instead. */
    function horizonSentence() {
        var h = state.data && state.data.horizon;
        if (!h) return '';
        var spec = horizonSpec(h.selected);
        var parts = [];
        var observed = (h.observed_from_ms && h.observed_to_ms)
            ? durationLabel(h.observed_to_ms - h.observed_from_ms) : null;

        if (h.span_ms) {
            parts.push('Requested ' + spec.full + ', measured back from ' + utcClock(h.now_ms));
            if (h.covers_selected_span === true) {
                parts.push('the whole span is inside the read window');
            } else if (h.observed_from_ms) {
                parts.push('the read window only reaches back to ' + utcClock(h.observed_from_ms)
                    + ' (' + observed + ' observed), so this is NOT the full period');
            } else {
                parts.push('nothing in the read window carries a usable time, so no part of the span is confirmed');
            }
        } else {
            parts.push('Everything still in the read window');
            if (h.observed_from_ms && h.observed_to_ms) {
                parts.push(observed + ' observed, ' + utcClock(h.observed_from_ms)
                    + ' → ' + utcClock(h.observed_to_ms));
            }
            parts.push('older history is outside this bounded read and is not claimed');
        }

        if (h.excluded_older_than_cutoff) {
            parts.push(h.excluded_older_than_cutoff + ' retained records fall before the cutoff');
        }
        if (h.unknown_timestamp) {
            parts.push(h.unknown_timestamp + ' record'
                + (h.unknown_timestamp === 1 ? '' : 's')
                + ' carry no usable timestamp, so '
                + (h.span_ms ? 'a timed horizon cannot place them and they are left out'
                    : 'they are counted but cannot be drawn'));
        }
        if (h.ahead_of_anchor) {
            parts.push(h.ahead_of_anchor + ' recorded after the anchor instant');
        }
        return parts.join(' · ') + '.';
    }

    function detailRow(parent, label, value, options) {
        var settings = options || {};
        var row = el('div', 'detail-row');
        var left = el('span', 'detail-label', label);
        if (settings.exact) left.title = 'Recorded as: ' + settings.exact;
        row.appendChild(left);
        var right = el('span', 'detail-value', value);
        if (settings.exact) right.title = 'Recorded as: ' + settings.exact;
        row.appendChild(right);
        parent.appendChild(row);
        if (settings.note) parent.appendChild(el('div', 'detail-note', settings.note));
    }

    function renderTrajectory(card, point) {
        if (state.trajectory === 'loading') {
            card.appendChild(el('p', 'muted meta', 'Loading this task’s trajectory…'));
            return;
        }
        if (!state.trajectory || state.trajectoryTask !== point.task) return;
        var payload = state.trajectory;
        var head = el('div', 'card-head');
        head.appendChild(el('h3', 'sub-title', 'This task over time'));
        var reload = el('button', 'button button-quiet', 'Reload');
        reload.type = 'button';
        reload.setAttribute('data-focus', 'trajectory-reload');
        on(reload, 'click', function () { loadTrajectory(point.task); });
        head.appendChild(reload);
        card.appendChild(head);

        var chart = el('div', 'chart chart-small');
        var canvas = el('canvas', 'canvas');
        canvas.setAttribute('role', 'img');
        canvas.setAttribute('aria-label',
            'Input token sizes over time for this task, grouped by model and work kind. '
            + 'The groups are listed underneath in text form.');
        chart.appendChild(canvas);
        card.appendChild(chart);
        liveChart(chart, canvas, function () { drawTrajectory(canvas, payload); });

        // The same horizon the overview is on, said out loud — a flat line here
        // can mean "this task did not grow" or "the growth is before the
        // cutoff", and those are different answers.
        var scope = payload.horizon && payload.horizon.span_ms
            ? 'Within ' + horizonSpec(payload.horizon.selected).full
            : 'Across everything still in the read window';
        if (payload.own_outside_horizon) {
            scope += ' · ' + payload.own_outside_horizon + ' earlier request'
                + (payload.own_outside_horizon === 1 ? ' of this task is' : 's of this task are')
                + ' outside it';
        }
        card.appendChild(el('p', 'muted meta', scope + '.'));

        var legend = el('ul', 'legend');
        (payload.groups || []).forEach(function (group) {
            var item = el('li', 'legend-item');
            item.appendChild(el('span', 'legend-mark joined'));
            item.appendChild(el('span', null,
                group.model + ' · ' + humanize(group.category) + ' · ' + group.points.length + ' requests'));
            legend.appendChild(item);
        });
        (payload.related || []).forEach(function (group) {
            var item = el('li', 'legend-item');
            item.appendChild(el('span', 'legend-mark loose'));
            item.appendChild(el('span', null,
                'Another task in the same tree · ' + group.model + ' · ' + humanize(group.category)
                + ' · ' + group.points.length + ' requests'));
            legend.appendChild(item);
        });
        if (legend.childNodes.length) card.appendChild(legend);
        var why = el('details', 'inline-details');
        why.appendChild(el('summary', null, 'Why some points are not joined'));
        why.appendChild(el('p', 'muted meta',
            'A line joins only requests of the same model doing the same kind of work inside this one task. '
            + 'Requests from other tasks in the same tree — children, review slots — are drawn as loose points, '
            + 'because joining independent runs would draw a trend that never happened.'));
        card.appendChild(why);
    }

    function renderDetail(parent) {
        var card = el('section', 'card detail-card');
        var point = state.selected;
        if (!point) {
            card.appendChild(el('h2', 'card-title', 'Request detail'));
            card.appendChild(el('p', 'muted body',
                'Select a point on the chart, or a row in the list, to see what was recorded for that request.'));
            parent.appendChild(card);
            return;
        }

        var head = el('div', 'card-head');
        head.appendChild(el('h2', 'card-title', 'Request detail'));
        head.appendChild(el('span', 'muted meta', clockTime(point.t)));
        card.appendChild(head);

        var lead = el('div', 'lead');
        var big = el('div', 'lead-value',
            typeof point.prompt_tokens === 'number' ? num(point.prompt_tokens) : 'not reported');
        lead.appendChild(big);
        lead.appendChild(el('div', 'lead-label', 'reported input tokens'));
        card.appendChild(lead);

        var primary = el('div', 'detail-body');
        detailRow(primary, 'Model', point.model);
        detailRow(primary, 'Mode', modeLabel(point.mode), point.mode ? null : {
            note: 'No context-fit measurement was recorded with this request, so its mode is genuinely unknown '
                + 'rather than guessed.'
        });
        detailRow(primary, 'Work kind', humanize(point.category), { exact: point.category });
        card.appendChild(primary);

        // The trajectory answers "is this task growing?" — the question the
        // detail panel exists for — so it sits above the recorded internals.
        renderTrajectory(card, point);

        var technical = el('details', 'inline-details');
        technical.appendChild(el('summary', null, 'Technical detail'));
        var body = el('div', 'detail-body');
        detailRow(body, 'Recorded at', fullTime(point.t));
        detailRow(body, 'Of which cache reads', num(point.cached_tokens), {
            note: 'Already included in the input number above. Ouroboros normalises provider usage so cache reads '
                + 'and writes are part of the input count; adding them again would double-count.'
        });
        detailRow(body, 'Output tokens', num(point.completion_tokens));
        detailRow(body, 'Cache writes', num(point.cache_write_tokens));
        detailRow(body, 'Provider', point.provider);
        detailRow(body, 'Recorded by', humanize(point.source), { exact: point.source });
        if (point.profile) detailRow(body, 'Context profile', point.profile);
        if (point.basis) detailRow(body, 'Measurement basis', humanize(point.basis), { exact: point.basis });
        if (typeof point.target_total_tokens === 'number') {
            detailRow(body, 'Target total for that round', num(point.target_total_tokens), {
                note: 'The figure recorded with this request. It is not a live window size and no percentage is '
                    + 'derived from it.'
            });
        }
        if (typeof point.capacity_total_tokens === 'number') {
            detailRow(body, 'Capacity total for that round', num(point.capacity_total_tokens));
        }
        if (point.target_miss === true) detailRow(body, 'Target miss', 'Yes, recorded');
        if (point.auto_pass === true) detailRow(body, 'Automatic pass used', 'Yes, recorded');
        detailRow(body, 'States seen', point.states.join(' → ') || point.state);
        if (typeof point.elapsed_sec === 'number') {
            detailRow(body, 'Reserved to settled', point.elapsed_sec.toFixed(2) + ' s', {
                note: 'Wall-clock between the two ledger rows. It includes waiting, so it is not a pure provider '
                    + 'latency.'
            });
        }
        detailRow(body, 'Task group', point.task || 'Not recorded');
        detailRow(body, 'Request key', point.id);
        technical.appendChild(body);
        card.appendChild(technical);

        parent.appendChild(card);
    }

    function renderList(parent) {
        var card = el('section', 'card');
        var head = el('div', 'card-head');
        head.appendChild(el('h2', 'card-title', 'Recent requests'));
        head.appendChild(el('span', 'muted meta', state.points.length + ' match the filter'));
        card.appendChild(head);

        var total = state.points.length;
        var shown = Math.min(state.rows, total);
        if (!total) {
            card.appendChild(el('p', 'muted body', 'No requests match the current filter.'));
            parent.appendChild(card);
            return;
        }

        var list = el('ul', 'list');
        list.setAttribute('aria-label',
            'Requests matching the current filter, newest first. Showing ' + shown + ' of ' + total + '.');
        state.points.slice().reverse().slice(0, shown).forEach(function (point) {
            var item = el('li', 'list-item');
            var button = el('button', 'row');
            button.type = 'button';
            button.setAttribute('data-focus', 'row-' + point.id);
            if (state.selected && state.selected.id === point.id) {
                button.classList.add('row-active');
                button.setAttribute('aria-current', 'true');
            }
            var left = el('span', 'row-main');
            left.appendChild(el('span', 'row-tokens',
                typeof point.prompt_tokens === 'number' ? num(point.prompt_tokens) : 'not reported'));
            left.appendChild(el('span', 'row-model', point.model));
            var right = el('span', 'row-meta',
                clockTime(point.t) + ' · ' + humanize(point.category)
                + ' · ' + modeLabel(point.mode)
                + (point.state === 'settled' ? '' : ' · ' + humanize(point.state)));
            right.title = point.category + ' · ' + point.state;
            button.appendChild(left);
            button.appendChild(right);
            on(button, 'click', function () { select(point); });
            item.appendChild(button);
            list.appendChild(item);
        });
        card.appendChild(list);

        if (total > ROWS_COLLAPSED) {
            var actions = el('div', 'row-actions');
            if (shown < total) {
                var more = el('button', 'button button-quiet',
                    'Show more (' + (total - shown) + ' left)');
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
            card.appendChild(actions);
        }
        parent.appendChild(card);
    }

    function renderHowToRead(parent) {
        var block = el('details', 'card details-card');
        block.appendChild(el('summary', null, 'How to read this'));
        var notes = [
            'The horizon cuts the records this widget holds, not the file. It is measured back from an '
            + 'explicit UTC anchor shown above, and the line under the control says how much of the '
            + 'requested span was actually observed. Picking 7 days does not make seven days of history '
            + 'exist: this reader keeps a bounded tail of one file, so a longer horizon can only ever '
            + 'show what that tail still contains.',
            'Each point is one physical model request that finished and reported its input size. '
            + 'Points are not joined: two consecutive requests can belong to entirely unrelated tasks.',
            'A plateau or a sudden drop in the size of a task’s requests does NOT by itself prove that the '
            + 'context was compacted. It is equally consistent with a shorter round, a different model, a branch '
            + 'of work ending, or a request that reported nothing. A field tying a request to a compaction pass '
            + 'is not available from this ledger — evidence may exist in observability artifacts, which are '
            + 'outside this widget’s scope — so Context Lens never claims one happened.',
            'Requests that never reported an input size are counted, not drawn. Missing is shown as missing, '
            + 'never as zero. Reserved but unfinished requests carry no token estimate anywhere in the ledger, '
            + 'so nothing is estimated for them.',
            'No cost, no window percentage and no per-document contribution is shown, because the ledger holds '
            + 'no exact figure this widget could derive them from without guessing.',
            'Mode comes from the context-fit measurement recorded with a request. Requests without that '
            + 'measurement stay under Unknown and are never assigned a mode.'
        ];
        var list = el('ul', 'notes');
        notes.forEach(function (text) { list.appendChild(el('li', null, text)); });
        block.appendChild(list);
        parent.appendChild(block);
    }

    function coverageSentence() {
        var view = state.view;
        if (!view || !view.total) return 'No requests match the current filter.';
        var parts = [view.measured + ' of ' + view.total + ' requests in this view have a recorded size and time'];
        if (view.settledWithoutSize) parts.push(view.settledWithoutSize + ' finished without one');
        if (view.withoutTime) parts.push(view.withoutTime + ' had no usable timestamp');
        if (view.inFlight) parts.push(view.inFlight + ' still in flight');
        if (view.unresolved) parts.push(view.unresolved + ' unresolved');
        if (view.released) parts.push(view.released + ' released before dispatch');
        return parts.join(' · ');
    }

    function windowSentence() {
        var w = state.data.window;
        var parts = ['Rows read ' + num(w.lines_read)];
        if (w.omitted_prefix_bytes) parts.push('older ' + bytesLabel(w.omitted_prefix_bytes) + ' of the file not read');
        if (w.evicted_records) parts.push(w.evicted_records + ' oldest records dropped from the cache');
        if (w.malformed_lines) parts.push(w.malformed_lines + ' unreadable lines skipped');
        if (w.pending_tail_bytes) parts.push('an unfinished tail row is waiting');
        if (w.discarded_oversize_lines) {
            parts.push(w.discarded_oversize_lines + ' line'
                + (w.discarded_oversize_lines === 1 ? '' : 's')
                + ' too long for one read discarded');
        }
        if (w.rotations_observed) parts.push('ledger replaced ' + w.rotations_observed + ' times while open');
        if (state.data.points_omitted) parts.push(state.data.points_omitted + ' older points not sent');
        return parts.join(' · ');
    }

    function renderCoverageDetails(parent) {
        var data = state.data;
        var c = data.counters;
        var x = c.excluded;
        var block = el('details', 'card details-card');
        block.appendChild(el('summary', null, 'Coverage details'));

        var h = data.horizon;
        if (h) {
            block.appendChild(el('p', 'meta strong-line', 'Horizon'));
            var horizonFacts = ['Selected: ' + horizonSpec(h.selected).full,
                'anchor ' + utcDay(h.now_ms)];
            if (h.cutoff_ms) horizonFacts.push('cutoff ' + utcDay(h.cutoff_ms));
            if (h.observed_from_ms) {
                horizonFacts.push('observed ' + utcDay(h.observed_from_ms)
                    + ' → ' + utcDay(h.observed_to_ms));
            }
            horizonFacts.push(h.records_selected + ' of ' + h.records_retained
                + ' retained records selected');
            if (h.covers_selected_span === false) {
                horizonFacts.push('the selected span is NOT fully covered by the read window');
            }
            if (h.history_truncated_by_source) {
                horizonFacts.push('older rows exist in the file or its archive and were not read, '
                    + 'so no horizon here can claim complete history');
            }
            block.appendChild(el('p', 'muted meta', horizonFacts.join(' · ') + '.'));
        }

        block.appendChild(el('p', 'meta strong-line', 'In this view (current filter)'));
        block.appendChild(el('p', 'muted meta', coverageSentence()));

        block.appendChild(el('p', 'meta strong-line', 'Across the selected horizon, before any filter'));
        var whole = [c.measured + ' of ' + c.physical_attempts + ' attempts reported a size'];
        if (c.settled_without_tokens) whole.push(c.settled_without_tokens + ' finished without one');
        if (c.in_flight) whole.push(c.in_flight + ' in flight');
        if (c.by_state.unresolved) whole.push(c.by_state.unresolved + ' unresolved');
        if (c.by_state.released) whole.push(c.by_state.released + ' released before dispatch');
        block.appendChild(el('p', 'muted meta', whole.join(' · ')));
        block.appendChild(el('p', 'muted meta',
            'These counts cover every filter at once, so they differ from the view figures above whenever a '
            + 'filter is set. They are cut by the horizon first, exactly like the chart.'));

        var excluded = [];
        if (x.baseline_rows) {
            var folded = num(x.folded_attempts) + ' folded attempts in '
                + x.baseline_rows + ' compaction summary rows';
            if (x.baselines_without_header) {
                folded += ' (at least: ' + num(x.folded_attempts_from_groups)
                    + ' of them are counted from group rows whose compaction header is outside this window, '
                    + 'so group rows may be missing too)';
            }
            excluded.push(folded + '. A summary row is a sum over many requests, never a request.');
        }
        if (x.subscription_sessions) {
            excluded.push(x.subscription_sessions + ' harness subscription session totals. Each is one aggregate '
                + 'for a whole delegated session, not a physical request, so it is excluded from the chart and '
                + 'from every statistic here.');
        }
        if (x.external_unmetered) excluded.push(x.external_unmetered + ' external unmetered dispatches.');
        if (x.legacy_rows) excluded.push(x.legacy_rows + ' legacy imported rows.');
        if (x.unknown_kind) {
            excluded.push(x.unknown_kind + ' rows of a kind this version does not recognise. They are excluded '
                + 'rather than guessed into the chart.');
        }
        if (x.attempts_without_state) {
            excluded.push(x.attempts_without_state + ' rows that look like attempts but carry no recognisable '
                + 'state, so they are not treated as ordinary requests.');
        }
        if (data.window.compaction_epoch) {
            excluded.push('The ledger has been compacted (epoch ' + data.window.compaction_epoch + '). Folded '
                + 'attempts were moved to an archive this widget does not read, so they cannot appear as points.');
        }
        if (excluded.length) {
            block.appendChild(el('p', 'meta strong-line', 'Counted but never drawn'));
            var list = el('ul', 'notes');
            excluded.forEach(function (text) { list.appendChild(el('li', null, text)); });
            block.appendChild(list);
        }

        block.appendChild(el('p', 'meta strong-line', 'Read window'));
        block.appendChild(el('p', 'muted meta', windowSentence()));
        parent.appendChild(block);
    }

    function render() {
        if (disposed) return;
        applyFilters();
        var root = document.getElementById('root');
        if (!root) return;

        // Keep the keyboard where it was: a poll, a filter change or a selection
        // rebuilds the tree, and focus must not fall back to the document.
        var active = document.activeElement;
        var focusWanted = active && root.contains(active) && active.getAttribute
            ? active.getAttribute('data-focus') : null;

        clearScheduled();
        observers.forEach(function (observer) { observer.disconnect(); });
        observers = [];
        listeners = listeners.filter(function (entry) {
            if (root.contains(entry[0])) {
                entry[0].removeEventListener(entry[1], entry[2], entry[3]);
                return false;
            }
            return true;
        });
        root.textContent = '';

        var header = el('header', 'header');
        var titles = el('div', 'titles');
        titles.appendChild(el('h1', 'title', 'Context Lens'));
        titles.appendChild(el('p', 'subtitle',
            'Reported input size of model requests, read from the local usage ledger.'));
        header.appendChild(titles);
        var refresh = el('button', 'button', state.loading ? 'Refreshing…' : 'Refresh');
        refresh.type = 'button';
        refresh.disabled = state.loading;
        refresh.setAttribute('data-focus', 'refresh');
        on(refresh, 'click', function () { load(false); });
        header.appendChild(refresh);
        root.appendChild(header);
        // The horizon stays reachable even while the answer is loading or
        // unavailable: a span that returns nothing must still be changeable.
        renderHorizon(root);

        if (state.error) {
            var problem = el('section', 'card');
            problem.appendChild(el('h2', 'card-title', 'Telemetry unavailable'));
            problem.appendChild(el('p', 'body', state.error));
            problem.appendChild(el('p', 'muted meta',
                'Context Lens reads one fixed local file and never reports its location or contents here.'));
            root.appendChild(problem);
            restoreFocus(root, focusWanted);
            return;
        }
        if (!state.data) {
            root.appendChild(el('p', 'muted body', 'Loading telemetry…'));
            restoreFocus(root, focusWanted);
            return;
        }

        root.appendChild(el('p', 'muted meta coverage-line', horizonSentence()));

        var view = state.view;
        var tiles = el('div', 'tiles');
        tiles.appendChild(statTile('Typical input', compact(state.stats.median),
            state.stats.count + ' measured',
            'Median reported input tokens across the requests matching the current filter.'));
        tiles.appendChild(statTile('95% below', compact(state.stats.p95),
            state.stats.count ? 'smallest ' + compact(state.stats.low) : '',
            '95% of the matching measured requests reported an input at or below this size '
            + '(linear-interpolated 95th percentile).'));
        tiles.appendChild(statTile('Peak', compact(state.stats.peak), 'largest in view',
            'The largest reported input among the matching measured requests.'));
        tiles.appendChild(statTile('Data coverage',
            view.total ? Math.round((view.measured / view.total) * 100) + '%' : '—',
            'of this view is measured',
            'Share of matching attempts that finished with both an input size and a usable timestamp. '
            + 'The rest are counted in Coverage details, never estimated.'));
        root.appendChild(tiles);

        var filters = el('div', 'filters');
        var facets = state.data.facets;
        addSelect({ key: 'model', label: 'Model', allLabel: 'All models', options: facets.models, value: state.filters.model }, filters);
        addSelect({ key: 'kind', label: 'Work kind', allLabel: 'All work kinds', options: facets.categories, value: state.filters.kind, human: true }, filters);
        addSelect({ key: 'origin', label: 'Recorded by', allLabel: 'All origins', options: facets.sources, value: state.filters.origin, human: true }, filters);
        // Unknown is a permanent option: "no mode was recorded" is a real answer
        // about this install, not an artefact of what happens to be in view.
        var modes = ['max', 'low'].filter(function (mode) { return facets.modes.indexOf(mode) >= 0; });
        modes.push('unknown');
        addSelect({
            key: 'mode', label: 'Mode', allLabel: 'All modes', options: modes,
            labels: { max: 'Max', low: 'Low', unknown: 'Unknown (not recorded)' },
            value: state.filters.mode
        }, filters);
        root.appendChild(filters);

        var chartCard = el('section', 'card');
        var chartHead = el('div', 'card-head');
        chartHead.appendChild(el('h2', 'card-title', 'Input size over time'));
        chartHead.appendChild(el('span', 'muted meta',
            state.plotted.length + ' drawn of ' + state.points.length + ' in view'));
        chartCard.appendChild(chartHead);
        var chart = el('div', 'chart');
        var canvas = el('canvas', 'canvas');
        canvas.setAttribute('role', 'img');
        canvas.setAttribute('aria-label',
            'Scatter chart of reported input tokens over time for the ' + state.plotted.length
            + ' measured requests in this view. Every one of the ' + state.points.length
            + ' requests matching the current filter can be reached as text in the Recent requests list below, '
            + 'which pages through them.');
        chart.appendChild(canvas);
        var tooltip = el('div', 'tooltip');
        tooltip.hidden = true;
        chart.appendChild(tooltip);
        chartCard.appendChild(chart);
        root.appendChild(chartCard);

        var columns = el('div', 'columns');
        var left = el('div', 'column');
        var right = el('div', 'column');
        renderList(left);
        renderDetail(right);
        columns.appendChild(left);
        columns.appendChild(right);
        root.appendChild(columns);

        // Both closed by default and side by side, so the explanations cost one
        // row of the card rather than two screens of prose.
        var disclosures = el('div', 'disclosures');
        renderHowToRead(disclosures);
        renderCoverageDetails(disclosures);
        root.appendChild(disclosures);

        liveChart(chart, canvas, function () { drawScatter(canvas); });

        var nearest = function (event) {
            var rect = canvas.getBoundingClientRect();
            var x = event.clientX - rect.left;
            var y = event.clientY - rect.top;
            var best = null;
            (canvas._hits || []).forEach(function (hit) {
                var distance = Math.hypot(hit.x - x, hit.y - y);
                if (distance <= 14 && (!best || distance < best.distance)) {
                    best = { hit: hit, distance: distance, rect: rect };
                }
            });
            return best;
        };
        on(canvas, 'mousemove', function (event) {
            var best = nearest(event);
            if (!best) { tooltip.hidden = true; return; }
            tooltip.hidden = false;
            tooltip.textContent = num(best.hit.point.prompt_tokens) + ' input · ' + best.hit.point.model
                + ' · ' + clockTime(best.hit.point.t);
            tooltip.style.left = Math.min(best.rect.width - 20, Math.max(0, best.hit.x)) + 'px';
            tooltip.style.top = Math.max(0, best.hit.y - 34) + 'px';
        });
        on(canvas, 'mouseleave', function () { tooltip.hidden = true; });
        on(canvas, 'click', function (event) {
            var best = nearest(event);
            if (best) select(best.hit.point);
        });

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
            // The frame height is fixed by the manifest, so this document is the
            // one scrolling surface: no inner pane competes with it and the last
            // row of the card is always reachable.
            'html{height:100%;}',
            'html,body{margin:0;padding:0;background:#0d0b0f;max-width:100%;overflow-x:hidden;}',
            'body{min-height:100%;overflow-y:auto;',
            'font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,sans-serif;',
            'color:#e2e8f0;font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased;}',
            '#root{box-sizing:border-box;padding:16px;display:flex;flex-direction:column;gap:12px;max-width:100%;}',
            '*{box-sizing:border-box;min-width:0;}',
            'h1,h2,h3{margin:0;font-weight:600;}',
            '.title{font-size:16px;line-height:1.3;}',
            '.subtitle{margin:2px 0 0;font-size:12px;line-height:1.35;color:rgba(255,255,255,0.68);}',
            '.header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;}.titles{flex:1;}',
            '.button{font:inherit;font-size:13px;min-height:34px;padding:6px 15px;border-radius:999px;',
            'border:1px solid rgba(255,255,255,0.08);background:rgba(255,255,255,0.04);color:#e2e8f0;cursor:pointer;}',
            '.button:hover:not(:disabled){background:rgba(255,255,255,0.08);}',
            '.button:disabled{color:rgba(255,255,255,0.38);cursor:default;}',
            '.button-quiet{min-height:28px;padding:3px 12px;font-size:12px;}',
            '.button:focus-visible,.control:focus-visible,.row:focus-visible,summary:focus-visible,',
            '.segment:focus-visible{outline:2px solid rgba(201,53,69,0.4);outline-offset:2px;}',
            // Horizon: one segmented row, the pressed span carried by the brand
            // red at low weight rather than by a second colour.
            '.horizon{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}',
            '.segmented{display:inline-flex;padding:2px;gap:2px;border-radius:999px;',
            'border:1px solid rgba(255,255,255,0.08);background:rgba(255,255,255,0.03);}',
            '.segment{font:inherit;font-size:13px;line-height:1.3;min-height:28px;padding:3px 13px;',
            'border:0;border-radius:999px;background:transparent;color:rgba(255,255,255,0.68);cursor:pointer;}',
            '.segment:hover{color:#e2e8f0;background:rgba(255,255,255,0.06);}',
            '.segment-on{background:rgba(201,53,69,0.18);color:#e2e8f0;',
            'box-shadow:inset 0 0 0 1px rgba(201,53,69,0.32);}',
            '.coverage-line{margin:-2px 0 0;}',
            '.card{border:1px solid rgba(255,255,255,0.08);background:rgba(255,255,255,0.03);',
            'border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:8px;}',
            '.card-title{font-size:16px;line-height:1.3;}',
            '.sub-title{font-size:14px;line-height:1.3;}',
            '.card-head{display:flex;align-items:baseline;justify-content:space-between;gap:10px;flex-wrap:wrap;}',
            '.muted{color:rgba(255,255,255,0.68);}',
            '.meta{font-size:12px;line-height:1.4;}',
            '.body{font-size:14px;}',
            'p{margin:0;overflow-wrap:anywhere;}',
            '.strong-line{color:#e2e8f0;font-weight:600;margin-top:2px;}',
            '.tiles{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}@media(min-width:660px){.tiles,.filters{grid-template-columns:repeat(4,minmax(0,1fr));}}',
            '.tile{border:1px solid rgba(255,255,255,0.08);background:rgba(255,255,255,0.03);',
            'border-radius:12px;padding:10px 12px;}',
            '.tile-value{font-size:24px;line-height:1.25;font-weight:600;}',
            '.tile-label{font-size:12px;line-height:1.35;color:rgba(255,255,255,0.82);margin-top:1px;}',
            '.tile-note{font-size:12px;line-height:1.3;color:rgba(255,255,255,0.55);min-height:14px;}',
            '.filters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}',
            '.field{display:flex;flex-direction:column;gap:3px;}',
            '.field-label{font-size:12px;line-height:1.35;color:rgba(255,255,255,0.68);}',
            '.control{font:inherit;font-size:14px;min-height:32px;padding:4px 8px;border-radius:8px;max-width:100%;',
            'border:1px solid rgba(255,255,255,0.08);background:rgba(255,255,255,0.04);color:#e2e8f0;}',
            '.chart{position:relative;height:220px;}',
            '.chart-small{height:140px;}',
            '.canvas{width:100%;height:100%;display:block;}',
            '.tooltip{position:absolute;transform:translateX(-50%);pointer-events:none;font-size:12px;',
            'line-height:1.35;padding:5px 8px;border-radius:8px;border:1px solid rgba(255,255,255,0.08);',
            'background:rgba(18,20,26,0.98);color:#e2e8f0;white-space:nowrap;max-width:100%;}',
            '.columns{display:grid;grid-template-columns:minmax(0,1fr);gap:10px;align-items:start;}',
            '@media (min-width:660px){.columns{grid-template-columns:minmax(0,1fr) minmax(0,1fr);}}',
            '.column{display:flex;flex-direction:column;gap:10px;min-width:0;}',
            // No inner scroll pane: the document scrolls, the list grows, and
            // the owner never has to find a second scrollbar to reach a row.
            '.list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px;}',
            '.row-actions{display:flex;gap:8px;flex-wrap:wrap;}',
            '.row{display:flex;width:100%;align-items:baseline;justify-content:space-between;gap:10px;',
            'font:inherit;text-align:left;padding:5px 8px;border-radius:8px;border:1px solid transparent;',
            'background:transparent;color:#e2e8f0;cursor:pointer;}',
            '.row:hover{background:rgba(255,255,255,0.07);}',
            '.row-active{background:rgba(201,53,69,0.12);border-color:rgba(201,53,69,0.25);}',
            '.row-main{display:flex;align-items:baseline;gap:8px;min-width:0;}',
            '.row-tokens{font-size:14px;font-weight:600;white-space:nowrap;}',
            '.row-model{font-size:12px;line-height:1.35;color:rgba(255,255,255,0.54);overflow:hidden;',
            'text-overflow:ellipsis;white-space:nowrap;}',
            '.row-meta{font-size:12px;line-height:1.35;color:rgba(255,255,255,0.68);text-align:right;}',
            '.lead{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;}',
            '.lead-value{font-size:24px;line-height:1.25;font-weight:600;}',
            '.lead-label{font-size:12px;line-height:1.35;color:rgba(255,255,255,0.68);}',
            '.detail-body{display:flex;flex-direction:column;gap:5px;}',
            '.detail-row{display:flex;align-items:baseline;justify-content:space-between;gap:10px;}',
            '.detail-label{font-size:12px;line-height:1.35;color:rgba(255,255,255,0.68);}',
            '.detail-value{font-size:14px;text-align:right;overflow-wrap:anywhere;}',
            '.detail-note{font-size:12px;line-height:1.35;color:rgba(255,255,255,0.6);margin:-2px 0 3px;}',
            '.legend{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:3px;',
            'font-size:12px;line-height:1.35;color:rgba(255,255,255,0.68);}',
            '.legend-item{display:flex;align-items:center;gap:8px;}',
            '.legend-mark{flex:0 0 auto;width:14px;height:2px;border-radius:2px;background:rgba(226,232,240,0.78);}',
            '.legend-mark.loose{width:6px;height:6px;border-radius:999px;background:rgba(255,255,255,0.42);}',
            '.notes{margin:0;padding-left:17px;display:flex;flex-direction:column;gap:5px;',
            'font-size:12px;line-height:1.4;color:rgba(255,255,255,0.68);}',
            '.disclosures{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));',
            'gap:10px;align-items:start;}',
            'summary{cursor:pointer;font-size:14px;line-height:1.35;color:rgba(255,255,255,0.82);}',
            '.details-card{gap:6px;}',
            '.details-card[open]{gap:8px;}',
            '.inline-details{display:flex;flex-direction:column;gap:6px;}',
            '.inline-details>summary{font-size:12px;color:rgba(255,255,255,0.68);}'
        ].join('');
        var style = document.createElement('style');
        style.textContent = css;
        document.head.appendChild(style);
    }

    // -------------------------------------------------------------- startup

    installStyle();
    if (!document.getElementById('root')) {
        var rootNode = document.createElement('div');
        rootNode.id = 'root';
        document.body.appendChild(rootNode);
    }
    render();
    load(false);

    var poll = setInterval(function () {
        if (disposed || document.visibilityState !== 'visible') return;
        // Never redraw under an open dropdown or a keyboard user's hands.
        var root = document.getElementById('root');
        var active = document.activeElement;
        if (root && active && active !== document.body && root.contains(active)) return;
        load(false);
    }, POLL_MS);
    timers.push(poll);

    if (typeof window.__ouroWidgetOnDispose === 'function') {
        window.__ouroWidgetOnDispose(function () {
            disposed = true;
            timers.forEach(clearInterval);
            timers = [];
            clearScheduled();
            observers.forEach(function (observer) { observer.disconnect(); });
            observers = [];
            listeners.forEach(function (entry) {
                entry[0].removeEventListener(entry[1], entry[2], entry[3]);
            });
            listeners = [];
            controllers.forEach(abort);
            controllers.clear();
            dataController = null;
            trajectoryController = null;
        });
    }
})();
