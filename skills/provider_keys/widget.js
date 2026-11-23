/* Provider keys widget — v1.0.1
 *
 * A reviewed module widget: a classic script in an opaque-origin sandboxed
 * frame. Its only network is window.OuroborosWidget.fetch, limited by the host
 * to this skill's own routes. It renders GET /api/extensions/provider_keys/snapshot
 * (schema provider_keys.snapshot/1, design/spec.md §3) and asks for a refresh
 * with POST .../refresh.
 *
 * Display law: what the provider did not report is a dash, never $0; money is
 * never added across rows, pockets or sources; Ouroboros's own figures live
 * in Costs. Provider fields are rendered through textContent.
 */
(function () {
    'use strict';

    var BASE = '/api/extensions/provider_keys/';
    var REQUEST_TIMEOUT_MS = 16000;
    var JOIN_TIMEOUT_MS = 14000;
    var JOIN_POLL_MS = 1000;
    var CLOCK_TICK_MS = 30000;

    var root = document.getElementById('root') || document.body.appendChild(document.createElement('div'));
    var snap = null;            // last good snapshot
    var loadFault = '';         // 'unreachable' keeps the last snapshot; 'closed' does not
    var result = { text: '', tone: '' };
    var pending = null;
    var stopped = false, disposed = false;
    var themeOff = null;
    var clockTimer = null;
    var openRows = Object.create(null);
    var drawFault = false;       // set only while the fallback head is drawn

    function now() { return Date.now(); }

    // ---------- style ----------
    var STYLE = [
        ':root{color-scheme:dark;--neutral-rgb:255,255,255;',
        '--text-primary:#efeff2;--text-meta:rgba(255,255,255,.7);--text-secondary:rgba(255,255,255,.58);',
        '--accent:#f45152;--accent-light:#fe7d85;--green:#2fd359;--amber:#f7cb58;--red:#ef4444;',
        '--status-ok-fg:#83e499;--status-warn-fg:#f7cb58;--status-error-fg:#fe9498;',
        '--status-warn-bg:rgba(255,255,255,.08);--status-warn-border:rgba(255,255,255,.1);',
        '--status-error-bg:rgba(255,255,255,.08);--status-error-border:rgba(255,255,255,.1);',
        '--status-neutral-fg:var(--text-meta);--status-neutral-bg:rgba(var(--neutral-rgb),.06);--status-neutral-border:rgba(var(--neutral-rgb),.12);',
        '--warn-word:var(--status-warn-fg);--error-word:var(--status-error-fg);',
        '--divider:rgba(var(--neutral-rgb),.07);--track:rgba(var(--neutral-rgb),.1);--skeleton:rgba(var(--neutral-rgb),.08);',
        '--capsule:rgba(255,255,255,.09);--capsule-hover:rgba(255,255,255,.13);--capsule-shadow:none;',
        '--tray:rgba(var(--neutral-rgb),.045);--focus:#fe7d85;',
        '--font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;',
        '--type-meta:12px;--type-body:14px;--line-meta:1.35;--line-body:1.5;--tap:32px}',
        ':root[data-theme=light]{color-scheme:light;--neutral-rgb:32,35,43;',
        '--text-primary:#20232b;--text-meta:#555d69;--text-secondary:#626977;',
        '--accent:#c93545;--accent-light:#bf2f40;--green:#22c55e;--amber:#f59e0b;',
        '--status-ok-fg:#176b49;--status-warn-fg:#805300;--status-error-fg:#b42332;',
        '--status-warn-bg:rgba(245,158,11,.15);--status-warn-border:rgba(245,158,11,.3);',
        '--status-error-bg:rgba(239,68,68,.12);--status-error-border:rgba(239,68,68,.3);',
        '--warn-word:var(--status-warn-fg);--error-word:var(--status-error-fg);',
        '--capsule:#fff;--capsule-hover:#f5f6f8;--capsule-shadow:0 1px 2px rgba(28,33,51,.08),0 4px 12px rgba(28,33,51,.07);',
        '--tray:rgba(var(--neutral-rgb),.035);--focus:#bf2f40}',
        '@media (pointer:coarse){:root{--tap:44px}}',
        'html,body{margin:0;background:transparent}',
        'body{font:400 var(--type-body)/var(--line-body) var(--font-sans);color:var(--text-primary);-webkit-font-smoothing:antialiased}',
        '#root{box-sizing:border-box;padding:2px 16px 16px}',
        '.pk{container-type:inline-size;display:flex;flex-direction:column;gap:14px;min-width:0}',
        '.pk *{box-sizing:border-box}',
        '.num{font-variant-numeric:tabular-nums}',
        ':focus-visible{outline:2px solid var(--focus);outline-offset:2px}',
        // head
        '.pk-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px}',
        '.pk-summary{display:flex;align-items:baseline;gap:8px;flex:1 1 220px;min-width:0;margin:0;font-weight:600}',
        '.pk-summary .dot{transform:translateY(-1px)}',
        '.pk-side{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-left:auto}',
        '.pk-read{font-size:var(--type-meta);line-height:var(--line-meta);color:var(--text-meta)}',
        '.pk-result{font-size:var(--type-meta);line-height:var(--line-meta);color:var(--text-meta)}',
        '.pk-result[data-tone=warn]{color:var(--warn-word)}',
        '.pk-btn{appearance:none;font:inherit;font-size:var(--type-body);color:var(--text-primary);background:var(--capsule);',
        'box-shadow:var(--capsule-shadow);border:0;border-radius:999px;min-height:max(32px,var(--tap));padding:0 14px;',
        'display:inline-flex;align-items:center;gap:8px;cursor:pointer}',
        '.pk-btn:hover{background:var(--capsule-hover)}',
        '.pk-btn[aria-disabled=true]{cursor:default}',
        '.spin{width:12px;height:12px;border-radius:50%;border:2px solid rgba(var(--neutral-rgb),.22);border-top-color:var(--text-primary);animation:pk-spin .9s linear infinite;flex:none}',
        '@keyframes pk-spin{to{transform:rotate(360deg)}}',
        '@media (prefers-reduced-motion:reduce){.spin{animation:none;border-top-color:rgba(var(--neutral-rgb),.22);border-right-color:var(--text-primary)}.sk{animation:none}}',
        // dots
        '.dot{width:7px;height:7px;border-radius:50%;flex:none;display:inline-block;background:rgba(var(--neutral-rgb),.38)}',
        '.dot[data-level=ok]{background:var(--green)}.dot[data-level=warn]{background:var(--amber)}.dot[data-level=bad]{background:var(--red)}',
        '.dot[data-level=unknown]{background:transparent;box-shadow:inset 0 0 0 1.5px rgba(var(--neutral-rgb),.45)}',
        // callout
        '.pk-callout{margin:0;padding:12px 14px;border-radius:12px;background:var(--status-neutral-bg);box-shadow:inset 0 0 0 1px var(--status-neutral-border);color:var(--text-meta);font-size:var(--type-meta);line-height:var(--line-meta)}',
        '.pk-callout b{display:block;color:var(--text-primary);font-size:var(--type-body);font-weight:600;margin-bottom:2px}',
        '.pk-callout[data-tone=bad]{background:var(--status-error-bg);box-shadow:inset 0 0 0 1px var(--status-error-border)}',
        '.pk-callout[data-tone=bad] b{color:var(--error-word)}',
        '.pk-callout[data-tone=warn]{background:var(--status-warn-bg);box-shadow:inset 0 0 0 1px var(--status-warn-border)}',
        // groups and rows
        '.pk-group{display:flex;flex-direction:column;gap:6px}',
        '.pk-group h3{margin:6px 0 0;font-size:var(--type-body);font-weight:600;line-height:1.3}',
        '.pk-rows{list-style:none;margin:0 -12px;padding:0;display:flex;flex-direction:column}',
        '.pk-row{position:relative;padding:12px;border-radius:12px;display:flex;flex-direction:column;gap:6px;min-width:0}',
        '.pk-row+.pk-row::before{content:"";position:absolute;left:12px;right:12px;top:0;height:1px;background:var(--divider)}',
        '.pk-row[data-tint]::before,.pk-row[data-tint]+.pk-row::before{display:none}',
        '.pk-row[data-tint=bad]{background:var(--status-error-bg);box-shadow:inset 0 0 0 1px var(--status-error-border)}',
        '.pk-row[data-tint=warn]{background:var(--status-warn-bg);box-shadow:inset 0 0 0 1px var(--status-warn-border)}',
        '.pk-row[data-tint]+.pk-row[data-tint]{margin-top:6px}',
        '.pk-rh{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;min-width:0}',
        '.chip{font-size:var(--type-meta);line-height:var(--line-meta);color:var(--status-neutral-fg);background:var(--status-neutral-bg);box-shadow:inset 0 0 0 1px var(--status-neutral-border);border-radius:999px;padding:1px 8px;white-space:nowrap;flex:none}',
        '.pk-name{font-weight:600;min-width:0;flex:0 1 auto}',
        '.pk-status{display:inline-flex;align-items:center;gap:6px;margin-left:auto;font-size:var(--type-meta);line-height:var(--line-meta);color:var(--text-meta);text-align:right}',
        '.pk-status[data-level=warn] .t{color:var(--warn-word)}.pk-status[data-level=bad] .t{color:var(--error-word)}',
        '@container (max-width:440px){.pk-status{margin-left:0;flex-basis:100%;text-align:left}}',
        '.pk-limit{display:flex;flex-direction:column;gap:5px;margin-top:2px}',
        '.pk-limit+.pk-limit{margin-top:6px}',
        '.pk-ll{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}',
        '.pk-ll .left{font-weight:600;white-space:nowrap}',
        '.pk-ll .left[data-level=bad]{color:var(--error-word)}.pk-ll .left[data-level=warn]{color:var(--warn-word)}',
        '.bar{height:6px;border-radius:999px;background:var(--track);overflow:hidden}',
        '.bar>i{display:block;height:100%;border-radius:inherit;background:var(--accent)}',
        '.meta{margin:0;font-size:var(--type-meta);line-height:var(--line-meta);color:var(--text-meta)}',
        '.meta .lbl{color:var(--text-secondary)}',
        '.meta .v{color:var(--text-primary)}',
        '.meta .v[data-level=bad]{color:var(--error-word);font-weight:600}.meta .v[data-level=warn]{color:var(--warn-word);font-weight:600}',
        '.meta[data-tone=warn]{color:var(--warn-word)}.meta[data-tone=bad]{color:var(--error-word)}',
        '.items{display:flex;flex-wrap:wrap;column-gap:12px;row-gap:2px}',
        '.items>span{white-space:nowrap}',
        '.line-dot{display:flex;gap:6px;align-items:baseline}.line-dot .dot{transform:translateY(-1px)}',
        '.tray{margin:2px 0 0;padding:8px 10px;border-radius:10px;background:var(--tray)}',
        // skeleton
        '.sk{display:block;height:10px;border-radius:6px;background:var(--skeleton);animation:pk-pulse 1.4s ease-in-out infinite}',
        '@keyframes pk-pulse{50%{opacity:.45}}',
        // details
        '.pk-det{margin-top:2px}',
        '.pk-det>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:6px;min-height:var(--tap);font-size:var(--type-meta);color:var(--text-primary);border-radius:8px;padding:0 4px;margin:0 -4px}',
        '.pk-det>summary::-webkit-details-marker{display:none}',
        '.pk-det>summary::before{content:"";width:6px;height:6px;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;transform:rotate(-45deg);margin-right:2px;transition:transform .15s}',
        '.pk-det[open]>summary::before{transform:rotate(45deg) translate(-1px,-1px)}',
        '.pk-dl{margin:4px 0 0;display:grid;grid-template-columns:minmax(120px,max-content) 1fr;gap:4px 16px;font-size:var(--type-meta);line-height:var(--line-meta)}',
        '.pk-dl dt{color:var(--text-meta)}.pk-dl dd{margin:0;color:var(--text-primary);min-width:0}',
        '@container (max-width:440px){.pk-dl{grid-template-columns:1fr;gap:0}.pk-dl dd{margin-bottom:6px}}',
        '.pk-foot{display:flex;flex-direction:column;gap:4px}'
    ].join('\n');

    function installStyle() {
        if (document.getElementById('pk-style')) return;
        var s = document.createElement('style');
        s.id = 'pk-style';
        s.textContent = STYLE;
        document.head.appendChild(s);
    }

    // ---------- small helpers ----------
    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined && text !== null) n.textContent = String(text);
        return n;
    }
    function add(parent) {
        for (var i = 1; i < arguments.length; i++) {
            var c = arguments[i];
            if (c === null || c === undefined || c === false) continue;
            parent.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
        }
        return parent;
    }
    function dot(level) {
        var d = el('span', 'dot');
        d.setAttribute('data-level', level || 'unknown');
        d.setAttribute('aria-hidden', 'true');
        return d;
    }
    function isNum(x) { return typeof x === 'number' && isFinite(x); }
    function ms(iso) {
        if (!iso || typeof iso !== 'string') return null;
        var t = Date.parse(iso);
        return isFinite(t) ? t : null;
    }

    // ---------- money ----------
    var USD0 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
    var USD2 = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    var USD3 = new Intl.NumberFormat('en-US', { minimumFractionDigits: 3, maximumFractionDigits: 3 });
    function usdWhole(x) { return '$' + USD0.format(x); }
    function usdCents(x) { return '$' + USD2.format(x); }
    // A spend figure on its own: whole dollars from $1,000 (rounded up, as beside a
    // limit, so the same amount reads the same everywhere), cents below, words for a trace.
    function usd(x) {
        if (!isNum(x)) return '—';
        if (x === 0) return '$0';
        var a = Math.abs(x);
        if (a < 0.01) return 'less than $0.01';
        return a >= 1000 ? usdWhole(Math.ceil(x - 1e-9)) : usdCents(x);
    }
    // Exact for Details: cents, three decimals under a dollar.
    function usdExact(x) {
        if (!isNum(x)) return '—';
        if (x > 0 && x < 0.001) return 'less than $0.001';
        if (x !== 0 && Math.abs(x) < 1) return '$' + USD3.format(x);
        return usdCents(x);
    }
    function isWhole(x) { return isNum(x) && Math.abs(x - Math.round(x)) < 1e-9; }

    // ---------- time ----------
    var F_TIME = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    var F_WDAY = new Intl.DateTimeFormat('en-GB', { weekday: 'short' });
    var F_DAY = new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
    var F_FULL = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    var F_UTC = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: 'UTC' });
    function dayStart(t) { var d = new Date(t); d.setHours(0, 0, 0, 0); return d.getTime(); }
    function dayDiff(t) { return Math.round((dayStart(t) - dayStart(now())) / 86400000); }
    function clock(t) { return F_TIME.format(new Date(t)); }
    // "19:43" today, "yesterday 19:43", "Mon 16 Nov, 19:43" otherwise.
    function when(t) {
        if (t === null) return '—';
        var d = dayDiff(t);
        if (d === 0) return clock(t);
        if (d === -1) return 'yesterday ' + clock(t);
        if (d === 1) return 'tomorrow ' + clock(t);
        return F_DAY.format(new Date(t)) + ', ' + clock(t);
    }
    function ago(t) {
        if (t === null) return 'never';
        var s = Math.max(0, (now() - t) / 1000);
        if (s < 45) return 'just now';
        var m = Math.round(s / 60);
        if (m < 60) return m + ' min ago';
        var h = Math.floor(m / 60);
        if (h < 24) return h + ' h ago';
        return 'on ' + F_DAY.format(new Date(t));
    }
    function resetPhrase(t) {
        var d = dayDiff(t);
        if (d === 0) return 'resets today at ' + clock(t);
        if (d === 1) return 'resets tomorrow at ' + clock(t);
        if (d > 1 && d < 7) return 'resets ' + F_WDAY.format(new Date(t)) + ' at ' + clock(t);
        return 'resets ' + F_DAY.format(new Date(t)) + ' at ' + clock(t);
    }
    function exactWhen(t) { return F_FULL.format(new Date(t)) + ' (' + F_UTC.format(new Date(t)) + ' UTC)'; }

    // ---------- words ----------
    var PERIOD = { daily: 'Daily', weekly: 'Weekly', monthly: 'Monthly', lifetime: 'Lifetime' };
    var ENDPOINT = {
        key: 'Key limit', credits: 'Account credit', models: 'Key check', api_key: 'Key check',
        costs: 'Organization costs', usage: 'Usage report', balance: 'Balance', rate_limits: 'Rate limits'
    };
    var ERROR_REASONS = { key_rejected: 1, api_key_invalid: 1, forbidden: 1, payment_required: 1, rate_limited: 1,
        provider_error: 1, invalid_response: 1, request_timeout: 1, unreachable: 1 };
    var PHASE = { deferred: 'deferred to a later refresh', unfinished: 'did not finish within this refresh',
        not_attempted: 'not attempted yet', retry_wait: 'waiting to retry' };
    var RATE_LIMIT_WORDS = 'No rate-limit figures from the free metadata read';
    function endpointName(e) { return ENDPOINT[e] || (e ? e.charAt(0).toUpperCase() + e.slice(1).replace(/_/g, ' ') : 'Reading'); }
    function rowName(r) {
        var names = [];
        (r.uses || []).forEach(function (u) {
            if (u && u.label && names.indexOf(u.label) < 0) names.push(u.label);
        });
        // A connection's own name first: it is how the owner calls the key.
        names.sort(function (a, b) {
            var ca = useSource(r, a) === 'connection', cb = useSource(r, b) === 'connection';
            return ca === cb ? 0 : (ca ? -1 : 1);
        });
        return names.length ? names.join(' · ') : (r.provider_label || 'Key');
    }
    function useSource(r, label) {
        for (var i = 0; i < (r.uses || []).length; i++) if (r.uses[i].label === label) return r.uses[i].source;
        return '';
    }
    function ageCutoff() {
        return ((snap && snap.refresh && snap.refresh.stale_after_s) || 1800) * 1000;
    }
    function outOfDate(t) { return t !== null && now() - t >= ageCutoff(); }
    function endpointStale(r, endpoint) {
        var m = (r.endpoints || {})[endpoint] || {};
        return m.stale === true || Boolean(m.reason) || outOfDate(ms(m.ok_at));
    }
    function rowAged(r) {
        return outOfDate(ms(r.read && r.read.ok_at)) || Object.values(r.endpoints || {}).some(function (m) {
            return m && outOfDate(ms(m.ok_at));
        });
    }
    function rowStatus(r) {
        var s = r.status || {};
        // An idle tab can outlive the budget window in its last snapshot. Do
        // not carry an old green status (or a limit alarm) into the next one.
        var source = { low_credit: 'credits', key_disabled: 'api_key', unavailable_balance: 'balance' }[s.code];
        var budget = s.budget && r[s.budget];
        if (budget && oldWindow(budget)) return { level: 'warn', text: 'Current budget window not read' };
        if ((source && endpointStale(r, source)) || (budget && budgetStale(r, budget))) {
            return { level: 'warn', text: 'Out of date' };
        }
        if (s.level === 'ok') {
            if (visibleBudgets(r).some(function (pair) { return oldWindow(pair[1]); })) {
                return { level: 'warn', text: 'Current budget window not read' };
            }
            if ((r.read && r.read.stale) || rowAged(r)) return { level: 'warn', text: 'Out of date' };
        }
        return { level: s.level || 'unknown', text: s.text || '—' };
    }

    // ---------- network ----------
    function endRequest(request) {
        pending = null;
        clearTimeout(request.poll);
        clearTimeout(request.deadline);
        request.controller.abort();
    }

    function failRequest(request, error, text) {
        if (pending !== request) return;
        endRequest(request);
        // These are HTTP answers from the widget route, not provider statuses
        // (provider refusals are described in a successful snapshot response).
        var closed = error && [401, 403, 404, 409, 410].indexOf(error.status) !== -1;
        if (closed) snap = null;
        loadFault = closed ? 'closed' : 'unreachable';
        result = { text: closed ? '' : (text || 'Refresh result unknown'), tone: 'warn' };
        render();
    }

    async function readRoute(path, method, request, timeout) {
        var response = await window.OuroborosWidget.fetch(BASE + path, {
            method: method, timeoutMs: timeout, signal: request.controller.signal
        });
        if (!response.ok) throw Object.assign(new Error('Widget request not served'), { status: response.status });
        return response.json();
    }

    function adoptSnapshot(next, reason) {
        if (!next || !Array.isArray(next.rows)) throw new Error('Invalid snapshot');
        var closed = reason === 'access_closed' || (next.settings && next.settings.state === 'access_closed');
        snap = closed ? null : next;
        loadFault = closed ? 'closed' : '';
    }

    function describeOutcome(o, manual) {
        var words = [];
        var notes = { too_soon: 'Refresh interval not reached', waiting: 'No new reading',
            settings_unreadable: 'Settings could not be read', settings_changed: 'Settings changed during the refresh',
            no_readable_keys: 'No keys could be read' };
        if (notes[o.reason] && (manual || o.reason === 'settings_changed' || o.reason === 'settings_unreadable')) words.push(notes[o.reason]);
        if (o.succeeded) words.push(o.succeeded + (o.succeeded === 1 ? ' reading updated' : ' readings updated'));
        if (o.failed) words.push(o.failed + ' failed');
        if (o.deferred) words.push(o.deferred + ' deferred');
        if (o.unfinished) words.push(o.unfinished + ' unfinished');
        if (o.retry_wait) words.push(o.retry_wait + ' waiting to retry');
        return { text: words.join(' · '), tone: o.failed || o.deferred || o.unfinished || o.retry_wait ||
            o.reason === 'settings_changed' || o.reason === 'settings_unreadable' ? 'warn' : '' };
    }

    function waitForSnapshot(request) {
        request.poll = setTimeout(async function () {
            try {
                var next = await readRoute('snapshot', 'GET', request, 4000);
                if (pending !== request) return;
                adoptSnapshot(next);
                if (!loadFault && next.refresh && next.refresh.state === 'refreshing') waitForSnapshot(request);
                else {
                    endRequest(request);
                    // A passive GET reports the snapshot, not the other tab's counts.
                    result = loadFault ? { text: '', tone: '' } : {
                        text: next.refresh && next.refresh.state === 'idle'
                            ? 'Latest snapshot loaded · Refresh result not reported' : 'Refresh result unknown', tone: ''
                    };
                }
                render();
            } catch (error) { failRequest(request, error); }
        }, JOIN_POLL_MS);
    }

    async function refresh(manual) {
        if (pending || stopped) return;
        var request = pending = { controller: new AbortController(), poll: null, deadline: null };
        result = { text: '', tone: '' };
        render();
        request.deadline = setTimeout(function () { failRequest(request); }, REQUEST_TIMEOUT_MS);
        try {
            var reply = await readRoute('refresh' + (manual ? '' : '?mode=auto'), 'POST', request, REQUEST_TIMEOUT_MS);
            if (pending !== request || stopped) return;
            if (!reply || !reply.outcome || typeof reply.outcome.reason !== 'string') throw new Error('Invalid refresh result');
            var o = reply.outcome;
            adoptSnapshot(reply.snapshot, o.reason);
            if (!loadFault && o.reason === 'already_refreshing') {
                clearTimeout(request.deadline);
                request.deadline = setTimeout(function () {
                    if (pending !== request) return;
                    endRequest(request);
                    result = { text: 'Could not confirm that the other refresh finished', tone: 'warn' };
                    render();
                }, JOIN_TIMEOUT_MS);
                result = { text: 'Waiting for another refresh', tone: '' };
                waitForSnapshot(request);
            } else {
                endRequest(request);
                result = loadFault ? { text: '', tone: '' } : describeOutcome(o, manual);
            }
            render();
        } catch (error) { failRequest(request, error); }
    }

    // ---------- render ----------
    function render() {
        var focusKey = document.activeElement && document.activeElement.getAttribute && document.activeElement.getAttribute('data-focus');
        root.querySelectorAll('.pk-det > summary').forEach(function (summary) {
            openRows[summary.getAttribute('data-focus')] = summary.parentNode.open;
        });
        var tree;
        try {
            tree = build();
        } catch (e) {
            // A reading this version cannot draw: say so visibly, keep Refresh, try again next reading.
            tree = el('div', 'pk');
            drawFault = true;
            try { add(tree, head()); } finally { drawFault = false; }
            add(tree, callout('warn', '', 'Try Refresh, or check the provider’s own page.'));
        }
        root.replaceChildren(tree);
        if (focusKey) {
            var target = Array.from(root.querySelectorAll('[data-focus]')).find(function (node) {
                return node.getAttribute('data-focus') === focusKey;
            });
            if (target) target.focus({ preventScroll: true });
        }
        scheduleClock();
    }

    function scheduleClock() {
        clearTimeout(clockTimer);
        if (stopped || !snap || !snap.rows.length || document.visibilityState === 'hidden') return;
        var at = now(), delay = CLOCK_TICK_MS, cutoff = ageCutoff();
        function sooner(t) { if (t !== null && t > at) delay = Math.min(delay, t - at); }
        function expiry(iso) { var t = ms(iso); if (t !== null) sooner(t + cutoff); }
        expiry(snap.refresh && snap.refresh.last_read_at);
        snap.rows.forEach(function (r) {
            if (!r) return;
            expiry(r.read && r.read.ok_at);
            Object.values(r.endpoints || {}).forEach(function (m) { if (m) expiry(m.ok_at); });
            visibleBudgets(r).forEach(function (pair) { sooner(budgetBoundary(pair[1])); });
            (r.budgets_extra || []).forEach(function (b) { sooner(budgetBoundary(b)); });
        });
        clockTimer = setTimeout(render, delay);
    }

    function build() {
        var pk = el('div', 'pk');
        add(pk, head());
        if (!snap) {
            if (loadFault === 'closed') add(pk, callout('bad', 'Provider keys access is unavailable', 'Check that the skill is enabled and still has access in Skills.'));
            else if (loadFault) add(pk, callout('bad', 'The widget could not read its backend', 'Try Refresh when it is available.'));
            else add(pk, skeletonGroup());
            return pk;
        }
        if (loadFault) {
            var date = ms(snap.generated_at);
            add(pk, callout('warn', 'The widget could not read its backend',
                (date === null ? 'Showing the last available snapshot' : 'Showing the snapshot from ' + exactWhen(date)) +
                '. These figures were not refreshed.'));
        }
        var settings = snap.settings || {};
        var rows0 = (snap.rows || []).length;
        if (settings.state === 'settings_unreadable' || settings.state === 'access_closed') {
            add(pk, callout('bad', rows0 ? 'Settings could not be read' : '', 'The widget cannot tell which keys are configured' + (rows0 ? '; the figures below are from earlier.' : '.')));
        }
        var denied = (settings.not_granted || []).filter(Boolean);
        if (denied.length) {
            add(pk, callout('', 'Grant ' + (denied.length === 1 ? 'this key' : 'these keys') + ' to the widget in Skills',
                denied.length + (denied.length === 1 ? ' setting is' : ' settings are') + ' not readable by Provider keys: ' + denied.join(', ') + '.'));
        }
        var rows = snap.rows || [];
        var model = rows.filter(function (r) { return r.group !== 'other'; });
        var other = rows.filter(function (r) { return r.group === 'other'; });
        if (!rows.length && settings.state === 'ok' && !denied.length) {
            add(pk, callout('', '', 'No readable keys are configured. Add a supported key in Settings → Accounts and keys.'));
        }
        var names = Object.create(null);
        rows.forEach(function (r) { names[r.id] = rowName(r); });
        if (model.length) add(pk, group('', model, names));
        if (other.length) add(pk, group('Other keys', other, names));
        add(pk, foot());
        return pk;
    }

    function callout(tone, title, text) {
        var c = el('div', 'pk-callout');
        if (tone) c.setAttribute('data-tone', tone);
        add(c, title ? el('b', '', title) : null, text ? el('span', '', text) : null);
        return c;
    }

    function head() {
        var h = el('div', 'pk-head');
        var sum = el('p', 'pk-summary');
        var level = 'unknown', text = 'Reading provider keys…';
        if (drawFault) { level = 'warn'; text = 'The latest reading could not be drawn'; }
        else if (loadFault) {
            level = snap ? 'warn' : 'bad';
            text = snap ? 'Showing last available readings' : 'Provider keys could not be read';
        }
        else if (snap) {
            var s = snap.summary || {};
            var rows = snap.rows || [];
            var anyRead = rows.some(function (r) { return r.read && r.read.ok_at; });
            var rank = { bad: 3, warn: 2, unknown: 1, ok: 0 };
            var worst = rows.reduce(function (a, r) {
                var st = rowStatus(r);
                return !a || rank[st.level] > rank[a.status.level] ? { row: r, status: st } : a;
            }, null);
            if (!rows.length) {
                level = s.level || 'unknown';
                text = s.text || 'No readable provider keys';
            }
            else if (!anyRead && pending) { level = 'unknown'; text = 'Reading provider keys…'; }
            else {
                level = worst.status.level;
                var original = worst.row.status || {};
                text = level === 'ok' ? 'Provider metadata read' :
                    (s.row_id === worst.row.id && s.text && original.level === level && original.text === worst.status.text
                        ? s.text : rowName(worst.row) + ': ' + worst.status.text);
            }
        }
        add(sum, dot(level), el('span', '', text));
        add(h, sum);

        var side = el('div', 'pk-side');
        if (snap && snap.refresh) {
            var t = ms(snap.refresh.last_read_at);
            if (t !== null) {
                var read = el('span', 'pk-read', 'Last read ' + when(t) + (outOfDate(t) ? ' · out of date' : ''));
                read.title = 'Last successful reading ' + exactWhen(t);
                add(side, read);
            }
        }
        var busy = !drawFault && !!pending;
        var btn = el('button', 'pk-btn');
        btn.type = 'button';
        btn.setAttribute('data-focus', 'refresh');
        if (busy) {
            btn.setAttribute('aria-disabled', 'true');
            btn.setAttribute('aria-busy', 'true');
            add(btn, el('span', 'spin'), 'Refreshing…');
        } else {
            add(btn, 'Refresh');
        }
        btn.addEventListener('click', function () { if (!busy) refresh(true); });
        add(side, btn);
        var res = el('span', 'pk-result', result.text);
        res.setAttribute('aria-live', 'polite');
        res.setAttribute('role', 'status');
        if (result.tone) res.setAttribute('data-tone', result.tone);
        add(side, res);
        add(h, side);
        return h;
    }

    function skeletonGroup() {
        var g = el('div', 'pk-group');
        var ul = el('ul', 'pk-rows');
        for (var i = 0; i < 3; i++) {
            var li = el('li', 'pk-row');
            li.setAttribute('aria-hidden', 'true');
            var rh = el('div', 'pk-rh');
            var a = el('span', 'sk'); a.style.width = '72px'; a.style.height = '18px'; a.style.borderRadius = '999px';
            var b = el('span', 'sk'); b.style.width = (110 + i * 30) + 'px'; b.style.height = '12px';
            add(rh, a, b);
            var c = el('span', 'sk'); c.style.width = '100%'; c.style.height = '6px';
            var d = el('span', 'sk'); d.style.width = '46%';
            add(li, rh, c, d);
            add(ul, li);
        }
        add(g, ul);
        return g;
    }

    function group(title, rows, names) {
        var g = el('section', 'pk-group');
        if (title) add(g, el('h3', '', title));
        var ul = el('ul', 'pk-rows');
        rows.forEach(function (r) { add(ul, row(r, names)); });
        add(g, ul);
        return g;
    }

    function row(r, names) {
        var li = el('li', 'pk-row');
        var status = rowStatus(r), level = status.level;
        if (level === 'bad' || level === 'warn') li.setAttribute('data-tint', level);
        var reading = !(r.read && r.read.ok_at);
        var refreshing = !!(pending && r.read && r.read.refreshing);

        // head: chip, name, status
        var rh = el('div', 'pk-rh');
        add(rh, el('span', 'chip', r.provider_label || r.kind || 'Provider'), el('span', 'pk-name', names[r.id]));
        var st = el('span', 'pk-status');
        st.setAttribute('data-level', level);
        if (refreshing) add(st, el('span', 'spin'));
        else add(st, dot(level));
        add(st, el('span', 't', reading && refreshing ? 'Reading…' : status.text));
        add(rh, st);
        add(li, rh);

        if (reading && refreshing) {
            var a = el('span', 'sk'); a.style.width = '100%'; a.style.height = '6px';
            var b = el('span', 'sk'); b.style.width = '52%';
            add(li, a, b);
            return li;
        }

        var caps = visibleBudgets(r);
        caps.forEach(function (pair) { add(li, limitBlock(r, pair[0], pair[1], caps.length > 1)); });
        add(li, creditLine(r, names));
        add(li, balanceLine(r));
        add(li, spendLine(r));
        add(li, byokLine(r));
        (r.unavailable || []).forEach(function (u) { add(li, unavailableLine(r, u)); });
        add(li, staleLine(r));
        add(li, details(r, names));
        return li;
    }

    var SCOPE = { key_limit: 'Key limit', api_key: 'API key limit', member: 'Member limit',
        workspace_default_member: 'Workspace default member limit', workspace_default_key: 'Workspace default key limit' };
    function scopeWords(scope) {
        if (!scope) return 'Scope not reported';
        if (Object.hasOwn(SCOPE, scope)) return SCOPE[scope];
        var s = String(scope).replace(/_(limit|budget)$/, '').replace(/_/g, ' ');
        return s.charAt(0).toUpperCase() + s.slice(1) + ' limit';
    }
    function budgetBoundary(k) { return ms(k.resets_at) ?? ms(k.calendar_ends_at); }
    function oldWindow(k) {
        var reset = budgetBoundary(k);
        return Boolean(k.window_state) || (reset !== null && reset <= now());
    }
    function budgetStale(r, k) { return k.stale === true || endpointStale(r, 'key'); }
    function visibleBudgets(r) {
        // The backend removes true aliases before projection. Equal numbers
        // and public scopes do not identify the same budget.
        return [['key', r.key_limit], ['effective', r.effective_budget]].filter(function (pair) { return pair[1]; });
    }
    function budgetTitle(kind, k) {
        if (kind === 'effective') return 'Effective budget · ' + scopeWords(k.scope);
        return k.scope === 'key_limit' || k.scope === 'api_key' ? scopeWords(k.scope) : 'Key limit · ' + scopeWords(k.scope);
    }
    function budgetWindow(r, k, exact) {
        var bits = [PERIOD[k.interval] || 'Window not reported'];
        var t = ms(k.resets_at);
        if (t !== null) {
            bits.push(exact ? 'Provider reset: ' + exactWhen(t) :
                (t > now() ? resetPhrase(t) : 'reset ' + when(t)));
        } else if (k.calendar_ends_at) bits.push('Calendar boundary: ' + k.calendar_ends_at.slice(0, 10) + ' 00:00 UTC');
        else if (k.window_state === 'crossed') bits.push('Read crossed a UTC calendar boundary');
        else if (k.interval === 'lifetime') bits.push('Does not reset');
        else if (PERIOD[k.interval]) bits.push('Reset time not reported');
        if (oldWindow(k)) bits.push('Current window not read');
        if (budgetStale(r, k)) {
            var read = ms(((r.endpoints || {}).key || {}).ok_at);
            bits.push('Out of date' + (read === null ? '' : ' · last read ' + (exact ? exactWhen(read) : when(read))));
        }
        if (k.includes_byok === true) bits.push('counts BYOK spend');
        if (exact && k.includes_byok === false) bits.push('excludes BYOK spend');
        return bits;
    }
    function limitBlock(r, kind, k, pair) {
        var box = el('div', 'pk-limit');
        box.setAttribute('data-budget', kind);
        var named = kind === 'effective' || pair || (k.scope && k.scope !== 'key_limit' && k.scope !== 'api_key');
        if (named) add(box, add(el('p', 'meta'), el('span', 'v', budgetTitle(kind, k))));
        var ended = oldWindow(k), spent = ended ? null : k.spent_usd, left = ended ? null : k.remaining_usd;
        var cap = k.limit_usd, meta = el('p', 'meta', budgetWindow(r, k, false).join(' · '));
        if (!isNum(cap)) {
            // Only a provider's assertion is unlimited; unknown is not none.
            var label = named ? 'limit' : 'key limit';
            return add(box, add(el('p', 'meta'), el('span', 'v', k.no_limit === true ? 'No ' + label :
                label.charAt(0).toUpperCase() + label.slice(1) + ' not reported'),
                isNum(spent) ? ' · ' + usd(spent) + ' used' : '', isNum(left) ? ' · ' + usd(left) + ' left' : ''), meta);
        }
        var whole = isWhole(cap);
        var spentText = !isNum(spent) ? '—' : (!whole ? (spent > 0 && spent < 0.01 ? 'less than $0.01' : usdCents(spent)) :
            (spent > 0 && spent < 1 ? 'less than $1' : usdWhole(Math.ceil(spent))));
        var capText = whole ? usdWhole(cap) : usdCents(cap);
        var leftText;
        if (!isNum(left)) leftText = '— left';
        else if (left <= 0) leftText = 'nothing left';
        else leftText = (whole ? (left < 1 ? 'less than $1' : usdWhole(Math.floor(left))) :
            (left < 0.01 ? 'less than $0.01' : usdCents(left))) + ' left';

        var ll = el('div', 'pk-ll');
        var used = el('span', 'num', spentText + ' of ' + capText);
        var lv = el('span', 'left num', leftText);
        var share = isNum(left) && cap > 0 ? left / cap : null;
        if (!budgetStale(r, k)) {
            if (isNum(left) && left <= 0) lv.setAttribute('data-level', 'bad');
            else if (share !== null && share <= 0.2) lv.setAttribute('data-level', 'warn');
        }
        add(ll, used, lv);
        var bar = el('div', 'bar');
        bar.setAttribute('aria-hidden', 'true');
        var fill = el('i');
        var frac = isNum(spent) && cap > 0 ? Math.min(1, Math.max(0, spent / cap)) : 0;
        fill.style.width = (frac * 100).toFixed(2) + '%';
        add(bar, fill);
        add(box, ll, isNum(spent) && cap > 0 ? bar : null, meta);
        return box;
    }

    // Native-currency balances (DeepSeek): one figure per currency, never converted or added.
    var CURRENCY = { USD: '$', CNY: '\u00a5' };
    function money(x, cur) {
        if (!isNum(x)) return '\u2014';
        var sym = CURRENCY[cur];
        var small = x > 0 && x < 0.01;
        var n = (sym || '') + USD2.format(small ? 0.01 : x);
        return (small ? 'less than ' : '') + n + (cur === 'USD' ? '' : ' ' + (cur || ''));
    }
    function balanceLine(r) {
        var list = Array.isArray(r.balances) ? r.balances.filter(function (b) { return b && b.currency; }) : [];
        if (!list.length && r.is_available !== false) return null;
        var stale = endpointStale(r, 'balance');
        var p = el('p', 'meta');
        var items = el('span', 'items');
        add(items, el('span', 'lbl', 'Balance'));
        list.forEach(function (b) {
            var v = el('span', 'v num', money(b.total_balance, b.currency));
            if (!stale && isNum(b.total_balance) && b.total_balance <= 0) v.setAttribute('data-level', 'bad');
            add(items, add(el('span', 'num'), v));
        });
        if (!list.length) add(items, el('span', 'v', '\u2014'));
        add(p, items);
        if (r.is_available === false) {
            var w = el('p', 'meta', stale ? 'Last reading: balance was not available for requests' :
                'The provider reports the balance as not available for requests');
            if (!stale) w.setAttribute('data-tone', 'warn');
            return add(document.createDocumentFragment(), p, w);
        }
        return p;
    }

    function creditLine(r, names) {
        var c = r.credits;
        if (!c) return null;
        var left = c.remaining_usd, total = c.total_usd, stale = c.state === 'stale' || endpointStale(r, 'credits');
        var p = el('p', 'meta');
        add(p, el('span', 'lbl', 'Account credit '));
        var v;
        if (!isNum(left)) v = el('span', 'v num', '—');
        else {
            var txt = left <= 0 ? 'none left' : (left < 0.01 ? 'less than $0.01' : usdCents(left)) + ' left';
            v = el('span', 'v num', txt);
            var share = isNum(total) && total > 0 ? left / total : null;
            if (!stale) {
                if (left <= 0.10 || (share !== null && share <= 0.02)) v.setAttribute('data-level', 'bad');
                else if (left < 1) v.setAttribute('data-level', 'warn');
            }
        }
        add(p, v);
        if (isNum(total)) add(p, el('span', 'num', ' of ' + (isWhole(total) ? usdWhole(total) : usdCents(total)) + ' bought'));
        var shared = (c.shared_with || []).map(function (id) { return names[id]; }).filter(Boolean);
        if (shared.length) add(p, ' · shared with ' + shared.join(', '));
        var creditFailed = (r.unavailable || []).some(function (u) { return u && u.endpoint === 'credits' && ERROR_REASONS[u.reason]; });
        var ep = (r.endpoints || {}).credits;
        var ok = ep ? ms(ep.ok_at) : null;
        if (stale && !creditFailed) {
            add(p, ' · last read ' + when(ok));
        }
        return p;
    }

    function spendLine(r) {
        var s = r.provider_spend;
        if (!s) return null;
        var p = el('p', 'meta');
        var items = el('span', 'items');
        add(items, el('span', 'lbl', (r.provider_label || 'Provider') + ' reports' + sampleDate(s.as_of)));
        [['day', s.day_usd], ['week', s.week_usd], ['month', s.month_usd]].forEach(function (pair) {
            add(items, add(el('span', 'num'), pair[0] + ' ', el('span', 'v', usd(pair[1])), spendWindow(s, pair)));
        });
        add(p, items);
        return p;
    }

    function byokLine(r) {
        var b = r.provider_spend && r.provider_spend.byok;
        if (!b) return null;
        var any = [b.day_usd, b.week_usd, b.month_usd].some(function (x) { return isNum(x) && x > 0; });
        if (!any) return null;
        var p = el('p', 'meta');
        var items = el('span', 'items');
        add(items, el('span', 'lbl', 'BYOK, billed by the upstream provider'));
        [['day', b.day_usd], ['week', b.week_usd], ['month', b.month_usd]].forEach(function (pair) {
            add(items, add(el('span', 'num'), pair[0] + ' ', el('span', 'v', usd(pair[1])), spendWindow(r.provider_spend, pair)));
        });
        add(p, items);
        return p;
    }

    function sampleDate(iso) {
        return typeof iso === 'string' ? ' · read ' + iso.slice(0, 10) + ' UTC' : '';
    }
    function spendWindow(s, pair) {
        if (!isNum(pair[1])) return '';
        var d = (s.period_start_utc || {})[pair[0].toLowerCase()];
        return d ? ' (from ' + d + ' UTC)' : ' (UTC window unclear)';
    }

    function unavailableLine(r, u) {
        if (!u || u.endpoint === 'rate_limits') return null;
        if (ERROR_REASONS[u.reason] || PHASE[u.reason]) {
            var ok = ms(u.ok_at), tried = ms(u.attempt_at);
            var p = el('p', 'meta');
            var org = u.endpoint === 'costs' || u.endpoint === 'usage';
            if (ERROR_REASONS[u.reason] && !u.scope_hint) p.setAttribute('data-tone', !org &&
                (u.reason === 'key_rejected' || u.reason === 'api_key_invalid' || u.reason === 'payment_required') ? 'bad' : 'warn');
            // The status beside the dot may already name this reason; then the line keeps only the times.
            var said = r.status && r.status.code === u.reason;
            var explanation = u.text || PHASE[u.reason] || u.reason;
            if (u.reason === 'provider_error' && isNum(u.status)) explanation += ' (HTTP ' + u.status + ')';
            var bits = [endpointName(u.endpoint) + (said && !u.scope_hint ? '' : ': ' + explanation)];
            bits.push(ok !== null ? 'last read ' + when(ok) : 'never read');
            if (tried !== null) bits.push('tried ' + when(tried));
            var retry = ms(u.retry_at);
            if (retry !== null && retry > now()) bits.push('next try after ' + when(retry));
            add(p, bits.join(' · '));
            return p;
        }
        var text = u.text || '';
        if (!text) return null;
        return el('p', 'meta', text);
    }

    function staleLine(r) {
        if (!r.read || !(r.read.stale || rowAged(r))) return null;
        var errored = (r.unavailable || []).some(function (u) { return ERROR_REASONS[u.reason]; });
        if (errored) return null; // the failed endpoint already says its last read
        var t = ms(r.read.ok_at);
        return el('p', 'meta', 'Some figures are out of date · latest successful read ' + ago(t));
    }

    function details(r, names) {
        var d = el('details', 'pk-det');
        var focusKey = 'det-' + r.id;
        if (openRows[focusKey]) d.open = true;
        var s = el('summary', '', 'Details');
        s.setAttribute('data-focus', focusKey);
        add(d, s);
        var dl = el('dl', 'pk-dl');
        function item(label, value) {
            if (value === null || value === undefined || value === '') return;
            add(dl, el('dt', '', label), el('dd', 'num', value));
        }
        function budgetDetails(label, b) {
            var ended = oldWindow(b), spent = ended ? null : b.spent_usd, left = ended ? null : b.remaining_usd;
            var bits = [];
            if (isNum(b.limit_usd)) {
                bits.push(usdExact(spent) + ' of ' + usdExact(b.limit_usd) + ' used', usdExact(left) + ' left');
            } else {
                bits.push(b.no_limit === true ? 'None (the provider reports no limit)' : 'Limit not reported');
                if (isNum(spent)) bits.push(usdExact(spent) + ' used');
                if (isNum(left)) bits.push(usdExact(left) + ' left');
            }
            item(label, bits.concat(budgetWindow(r, b, true)).join(' · '));
        }
        var caps = visibleBudgets(r);
        caps.forEach(function (pair) { budgetDetails(budgetTitle(pair[0], pair[1]), pair[1]); });
        var ps = r.provider_spend;
        var period = r.key_limit && !oldWindow(r.key_limit) && r.key_limit.includes_byok &&
            { daily: 'day', weekly: 'week', monthly: 'month' }[r.key_limit.interval];
        var field = period && ps && (ps.period_start_utc || {})[period] && period + '_usd';
        if (field && ps && isNum(ps[field]) && ps.byok && isNum(ps.byok[field])) {
            item('Same named spend interval', usdExact(ps[field]) + ' ' + (r.provider_label || 'provider') +
                ' credit · ' + usdExact(ps.byok[field]) + ' BYOK');
        }
        (Array.isArray(r.balances) ? r.balances : []).forEach(function (b) {
            if (!b || !b.currency) return;
            item('Balance (' + b.currency + ')', money(b.total_balance, b.currency) + ' total \u00b7 ' + money(b.granted_balance, b.currency) +
                ' granted \u00b7 ' + money(b.topped_up_balance, b.currency) + ' topped up');
        });
        if (r.is_available === true || r.is_available === false) item('Balance usable', r.is_available ? 'Yes' : 'No');
        (r.budgets_extra || []).forEach(function (b) {
            budgetDetails(b.label || ('Budget · ' + scopeWords(b.scope)), b);
        });
        var c = r.credits;
        if (c) {
            var cbits = [usdExact(c.used_usd) + ' used of ' + usdExact(c.total_usd) + ' bought', usdExact(c.remaining_usd) + ' left'];
            var sh = (c.shared_with || []).map(function (id) { return names[id]; }).filter(Boolean);
            if (sh.length) cbits.push('same account as ' + sh.join(', '));
            item('Account credit', cbits.join(' · '));
        }
        var ps2 = r.provider_spend;
        if (ps2) {
            var life = [usdExact(ps2.lifetime_usd) + ' ' + (r.provider_label || 'provider') + ' credit'];
            if (ps2.byok && isNum(ps2.byok.lifetime_usd)) life.push(usdExact(ps2.byok.lifetime_usd) + ' BYOK');
            item('Lifetime usage', life.join(' · '));
            if (ps2.byok) {
                var byok = [['Day', ps2.byok.day_usd], ['Week', ps2.byok.week_usd], ['Month', ps2.byok.month_usd]];
                // Mixed nonzero windows already make the BYOK line visible on the card.
                if (byok.some(function (p) { return p[1] === 0; }) && !byok.some(function (p) { return p[1] > 0; })) {
                    item('BYOK usage', byok.map(function (p) { return p[0] + ' ' + usdExact(p[1]) + spendWindow(ps2, p); }).join(' · '));
                }
            }
            var asOf = ms(ps2.as_of);
            if (asOf !== null) item('Spend reading', exactWhen(asOf));
        }
        var fr = r.free_requests;
        if (fr) {
            var counts = [];
            if (isNum(fr.used) || isNum(fr.limit)) counts.push((isNum(fr.used) ? USD0.format(fr.used) : '—') + ' of ' + (isNum(fr.limit) ? USD0.format(fr.limit) : '—'));
            if (isNum(fr.remaining)) counts.push(USD0.format(fr.remaining) + ' left');
            if (counts.length) {
                if (fr.interval === 'daily') counts.push('Daily' + (fr.period_start_utc ? ' from ' + fr.period_start_utc + ' UTC' : ' · UTC window unclear'));
                item('Free-model requests', counts.join(' · '));
            }
        }
        (r.facts || []).forEach(function (f) { if (f && f.label) item(f.label, f.value === null || f.value === undefined ? '—' : String(f.value)); });
        (r.unavailable || []).forEach(function (u) {
            if (!u) return;
            var parts = [u.endpoint === 'rate_limits' ? RATE_LIMIT_WORDS : (u.text || PHASE[u.reason] || u.reason || 'Not read')];
            if (isNum(u.status)) parts.push('answered ' + u.status);
            var at = ms(u.attempt_at);
            if (at !== null) parts.push('tried ' + when(at));
            var ok = ms(u.ok_at);
            if (ERROR_REASONS[u.reason]) parts.push(ok !== null ? 'last read ' + when(ok) : 'never read');
            item(endpointName(u.endpoint), parts.join(' · '));
        });
        var eps = r.endpoints || {};
        var reads = Object.keys(eps).map(function (key) {
            var m = eps[key] || {};
            var ok = ms(m.ok_at);
            var t = endpointName(key) + ': ' + (PHASE[m.phase] || (ok !== null ? 'read ' + when(ok) : 'not read'));
            if (PHASE[m.phase] && ok !== null) t += ' · last read ' + when(ok);
            if (m.phase === 'retry_wait' && m.retry_at) t += ' · after ' + when(ms(m.retry_at));
            if (isNum(m.latency_ms)) t += ' (' + Math.round(m.latency_ms) + ' ms)';
            if (m.stale || outOfDate(ok)) t += ' · out of date';
            return t;
        });
        if (reads.length) item('Read', reads.join(' · '));
        if (!dl.firstChild) return null;
        add(d, dl);
        return d;
    }

    function unsupportedList() {
        var list = snap && Array.isArray(snap.unsupported_builtin) ? snap.unsupported_builtin : [];
        return list.map(function (a) { return a && (a.label || a.kind); }).filter(Boolean);
    }

    function foot() {
        var f = el('div', 'pk-foot');
        var absent = (snap.not_configured || []).map(function (a) { return a && (a.label || a.kind); }).filter(Boolean);
        var unsupported = unsupportedList();
        if (unsupported.length) add(f, add(el('p', 'meta'), el('span', 'lbl', 'No built-in balance reader: '), unsupported.join(', ')));
        var disabled = ((snap.settings || {}).disabled || []).map(function (a) { return a.label; });
        if (disabled.length) add(f, add(el('p', 'meta'), el('span', 'lbl', 'Disabled: '), disabled.join(', ')));
        if (absent.length) add(f, add(el('p', 'meta'), el('span', 'lbl', 'Not configured: '), absent.join(', ')));
        add(f, el('p', 'meta', 'Ouroboros records its own usage in Costs.'));
        return f.firstChild ? f : null;
    }

    // ---------- lifecycle ----------
    function stop() {
        stopped = true;
        if (pending) endRequest(pending);
        clearTimeout(clockTimer);
        if (themeOff) { themeOff(); themeOff = null; }
    }

    function start() {
        installStyle();
        if (!themeOff && window.OuroborosWidget && typeof window.OuroborosWidget.onTheme === 'function') {
            themeOff = window.OuroborosWidget.onTheme(function (theme) {
                document.documentElement.dataset.theme = theme === 'light' ? 'light' : 'dark';
            });
        }
        render();
        refresh(false);
    }

    window.addEventListener('pagehide', stop);
    if (typeof window.__ouroWidgetOnDispose === 'function') {
        window.__ouroWidgetOnDispose(function () { disposed = true; stop(); });
    }
    window.addEventListener('pageshow', function () {
        if (disposed || !stopped) return;
        stopped = false;
        start();
    });
    document.addEventListener('visibilitychange', function () {
        if (stopped) return;
        if (document.visibilityState === 'hidden') clearTimeout(clockTimer);
        else {
            if (pending) render();
            else refresh(false);
        }
    });

    start();
})();
