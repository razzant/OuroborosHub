/* Slack Bridge — Widgets card.
 *
 * Viewer sentence: the owner glances at this card and within a second knows whether the Slack link works and
 * whether anything needs them; if something does, they see what, since when, what it means and what to do,
 * without decoding counters.
 *
 * The first line is the status route's own `text`/`tone` (the same words Places shows); the route also lists
 * the attention items in order of consequence (lib/status_summary.py). This file turns those facts into words.
 */
(function () {
    'use strict';

/* transport-kit:begin — shared by skills/slack-bridge/widget.js and skills/telegram-bot/widget.js.
 * Keep the two copies byte-identical; each skill's tests compare them. */
var TransportKit = (function () {
    var STYLE = [
        ':root{color-scheme:dark;--neutral-rgb:255,255,255;',
        '--text-primary:#efeff2;--text-meta:rgba(255,255,255,.7);--text-secondary:rgba(255,255,255,.58);',
        '--green:#2fd359;--amber:#f7cb58;--red:#ef4444;--focus:#fe7d85;',
        '--status-ok-fg:#83e499;--status-warn-fg:#f7cb58;--status-error-fg:#fe9498;',
        '--status-warn-bg:rgba(255,255,255,.08);--status-warn-border:rgba(255,255,255,.1);',
        '--status-error-bg:rgba(255,255,255,.08);--status-error-border:rgba(255,255,255,.1);',
        '--tray:rgba(var(--neutral-rgb),.045);--divider:rgba(var(--neutral-rgb),.07);--skeleton:rgba(var(--neutral-rgb),.08);',
        '--capsule:rgba(255,255,255,.09);--capsule-hover:rgba(255,255,255,.13);--capsule-shadow:0 1px 2px rgba(0,0,0,.4),0 6px 20px rgba(0,0,0,.28);',
        '--field:rgba(var(--neutral-rgb),.06);--field-border:rgba(var(--neutral-rgb),.14);',
        '--font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;',
        '--font-mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;',
        '--type-meta:12px;--type-body:14px;--line-meta:1.4;--line-body:1.45;--tap:24px}',
        ':root[data-theme=light]{color-scheme:light;--neutral-rgb:32,35,43;',
        '--text-primary:#20232b;--text-meta:#555d69;--text-secondary:#626977;',
        '--green:#22c55e;--amber:#f59e0b;--red:#ef4444;--focus:#bf2f40;',
        '--status-ok-fg:#176b49;--status-warn-fg:#805300;--status-error-fg:#b42332;',
        '--status-warn-bg:rgba(245,158,11,.15);--status-warn-border:rgba(245,158,11,.3);',
        '--status-error-bg:rgba(239,68,68,.12);--status-error-border:rgba(239,68,68,.3);',
        '--tray:rgba(var(--neutral-rgb),.035);',
        '--capsule:#fff;--capsule-hover:#f5f6f8;--capsule-shadow:0 1px 2px rgba(28,33,51,.08),0 4px 12px rgba(28,33,51,.07);',
        '--field:#fff;--field-border:rgba(var(--neutral-rgb),.18)}',
        '@media (pointer:coarse){:root{--tap:44px}}',
        'html,body{margin:0;background:transparent}',
        'body{font:400 var(--type-body)/var(--line-body) var(--font-sans);color:var(--text-primary);',
        '-webkit-font-smoothing:antialiased;overflow-wrap:break-word;word-break:normal;hyphens:manual}',
        '#root{box-sizing:border-box;padding:2px 16px 16px}',
        '.tc{container-type:inline-size;display:flex;flex-direction:column;gap:14px;min-width:0}',
        '.tc *{box-sizing:border-box}',
        '.tc p{margin:0}',
        '.num{font-variant-numeric:tabular-nums}',
        ':focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:4px}',
        /* the first line and its freshness */
        '.tc-head{display:flex;flex-direction:column;gap:2px;min-width:0}',
        '.tc-status{display:flex;align-items:flex-start;gap:8px;font-weight:600;font-size:var(--type-body);line-height:1.4;min-width:0}',
        '.tc-status .dot{margin-top:6px}',
        '.tc-fresh{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 6px;margin-left:15px;',
        'font-size:var(--type-meta);line-height:var(--line-meta);color:var(--text-meta)}',
        '.tc-fresh[data-stale] .tc-fresh-t{color:var(--status-warn-fg)}',
        '.tc-sep{color:var(--text-secondary)}',
        /* dots: the status mark family (7 px, saturated hue; a ring when not confirmed) */
        '.dot{width:7px;height:7px;border-radius:50%;flex:none;display:inline-block;background:rgba(var(--neutral-rgb),.38)}',
        '.dot[data-tone=ok]{background:var(--green)}.dot[data-tone=warn]{background:var(--amber)}.dot[data-tone=error]{background:var(--red)}',
        '.dot[data-tone=unknown]{background:transparent;box-shadow:inset 0 0 0 1.5px rgba(var(--neutral-rgb),.55)}',
        /* quiet text control */
        '.tc-link{appearance:none;border:0;background:none;font:inherit;color:var(--text-primary);cursor:pointer;',
        'padding:4px 2px;margin:-4px -2px;min-height:var(--tap);text-decoration:underline;text-underline-offset:2px;',
        'text-decoration-color:rgba(var(--neutral-rgb),.35)}',
        '.tc-link:hover{text-decoration-color:currentColor}',
        '.tc-link[aria-busy=true]{cursor:default;color:var(--text-meta);text-decoration:none}',
        /* attention items: one tinted tray each, worst first */
        '.tc-items{list-style:none;margin:0;padding:0 12px;border-radius:12px;background:var(--tray);display:flex;flex-direction:column}',
        '.tc-items[data-tone=error]{background:var(--status-error-bg)}.tc-items[data-tone=warn]{background:var(--status-warn-bg)}',
        '.tc-item{padding:10px 0;display:flex;flex-direction:column;gap:3px;min-width:0}',
        '.tc-item+.tc-item{border-top:1px solid var(--divider)}',
        '.tc-ih{display:flex;flex-wrap:wrap;align-items:flex-start;gap:2px 8px;min-width:0}',
        '.tc-ih .dot{margin-top:6px}',
        '.tc-ih-t{flex:1 1 150px;min-width:0;font-weight:600;font-size:var(--type-body);line-height:1.4}',
        '.tc-ih-at{margin-left:auto;font-size:var(--type-meta);line-height:1.6;color:var(--text-meta);white-space:nowrap}',
        '.tc-ib{margin-left:15px;display:flex;flex-direction:column;gap:4px;font-size:var(--type-meta);line-height:var(--line-meta);color:var(--text-meta)}',
        '.tc-ib .tc-strong{color:var(--text-primary)}',
        '.tc-ib-bare{margin-left:0}',
        /* small folds (an error's own text, details) */
        '.tc-fold>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:6px;min-height:var(--tap);',
        'font-size:var(--type-meta);line-height:var(--line-meta);color:var(--text-primary);border-radius:6px;padding:0 4px;margin:0 -4px}',
        '.tc-fold>summary::-webkit-details-marker{display:none}',
        '.tc-fold>summary::before{content:"";width:5px;height:5px;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;',
        'transform:rotate(-45deg);margin:0 2px 0 1px;transition:transform .15s;flex:none}',
        '.tc-fold[open]>summary::before{transform:rotate(45deg) translate(-1px,-1px)}',
        '.tc-fold>summary .tc-sum-note{color:var(--text-meta)}',
        '.tc-raw{margin:2px 0 0;font:12px/1.45 var(--font-mono);color:var(--text-secondary);white-space:pre-wrap;overflow-wrap:anywhere}',
        /* quiet totals */
        '.tc-facts{display:grid;grid-template-columns:minmax(64px,max-content) minmax(0,1fr);gap:4px 14px;margin:0;',
        'font-size:var(--type-meta);line-height:var(--line-meta)}',
        '.tc-facts dt{color:var(--text-meta);min-width:0}',
        '.tc-facts dd{margin:0;min-width:0;color:var(--text-primary)}',
        '.tc-facts dd [data-tone=warn]{color:var(--status-warn-fg)}.tc-facts dd [data-tone=error]{color:var(--status-error-fg)}',
        '.tc-facts dd .tc-dim{color:var(--text-meta)}',
        '.tc-note{font-size:var(--type-meta);line-height:var(--line-meta);color:var(--text-secondary)}',
        '.tc-block{display:flex;flex-direction:column;gap:6px;min-width:0}',
        /* details */
        '.tc-det,.tc-setting{border-top:1px solid var(--divider);padding-top:6px;margin-top:-2px}',
        '.tc-setting+.tc-det{border-top:0;padding-top:0;margin-top:-12px}',
        '.tc-setting[open]>.tc-form{padding:6px 0 4px}',
        '.tc-det-body{display:flex;flex-direction:column;gap:12px;padding:6px 0 2px}',
        '.tc-group{display:flex;flex-direction:column;gap:4px;min-width:0}',
        '.tc-group h4{margin:0;font-size:var(--type-meta);line-height:var(--line-meta);font-weight:600;color:var(--text-primary)}',
        '.tc-group .tc-facts{grid-template-columns:minmax(0,36%) minmax(0,1fr)}',
        '.tc-facts dd .tc-raw{margin:0}',
        '.tc-group p{font-size:var(--type-meta);line-height:var(--line-meta);color:var(--text-meta)}',
        '@container (max-width:290px){.tc-facts,.tc-group .tc-facts{grid-template-columns:minmax(0,1fr);gap:0}.tc-facts dd{margin-bottom:4px}}',
        /* forms (a skill's own setting on the card) */
        '.tc-form{display:flex;flex-direction:column;gap:6px;min-width:0}',
        '.tc-form label{font-size:var(--type-meta);line-height:var(--line-meta);font-weight:600;color:var(--text-primary)}',
        '.tc-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}',
        '.tc-input{flex:1 1 150px;min-width:0;font:inherit;font-size:var(--type-body);color:var(--text-primary);background:var(--field);',
        'border:1px solid var(--field-border);border-radius:10px;padding:6px 10px;min-height:max(32px,var(--tap))}',
        '@media (pointer:coarse){.tc-input{font-size:16px}}',
        '.tc-btn{appearance:none;font:inherit;font-size:var(--type-body);color:var(--text-primary);background:var(--capsule);',
        'box-shadow:var(--capsule-shadow);border:0;border-radius:999px;min-height:max(32px,var(--tap));padding:0 14px;cursor:pointer}',
        '.tc-btn:hover{background:var(--capsule-hover)}',
        '.tc-btn[disabled]{cursor:default;opacity:1;color:var(--text-meta)}',
        '.tc-result{font-size:var(--type-meta);line-height:var(--line-meta);color:var(--text-meta)}',
        '.tc-result[data-tone=ok]{color:var(--status-ok-fg)}.tc-result[data-tone=error]{color:var(--status-error-fg)}',
        '.tc-result[data-tone=warn]{color:var(--status-warn-fg)}',
        /* loading */
        '.sk{display:block;height:10px;border-radius:6px;background:var(--skeleton);animation:tc-pulse 1.4s ease-in-out infinite}',
        '@keyframes tc-pulse{50%{opacity:.45}}',
        '@media (prefers-reduced-motion:reduce){.sk{animation:none}.tc-fold>summary::before{transition:none}}'
    ].join('\n');

    var NO_BRIDGE = 'The widget bridge is unavailable, so this card can’t reach Ouroboros';
    var NUMBER = new Intl.NumberFormat('en-US');
    var F_TIME = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit' });
    var F_DAY = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short' });
    var F_YEAR = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
    var F_FULL = new Intl.DateTimeFormat('en-GB', {
        day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
        timeZoneName: 'short'
    });

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }
    function add(parent) {
        for (var i = 1; i < arguments.length; i++) {
            var child = arguments[i];
            if (child === null || child === undefined || child === false) continue;
            parent.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
        }
        return parent;
    }
    function count(value) {
        return typeof value === 'number' && isFinite(value) && value > 0 ? Math.floor(value) : 0;
    }
    function known(value) { return typeof value === 'number' && isFinite(value); }
    function num(value) { return known(value) ? NUMBER.format(value) : '—'; }
    function plural(n, word, many) { return num(n) + ' ' + (n === 1 ? word : (many || word + 's')); }
    function toMs(value) {
        if (typeof value === 'number' && isFinite(value) && value > 0) return value < 1e12 ? value * 1000 : value;
        if (typeof value === 'string' && value) {
            var parsed = Date.parse(value);
            return isFinite(parsed) ? parsed : null;
        }
        return null;
    }
    function sameDay(a, b) {
        return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
    }
    /* A moment in the reader's clock: "22:00", "yesterday 22:00", "21 Nov, 22:00"; never a raw ISO stamp. */
    function clock(value, nowMs) {
        var ms = toMs(value);
        if (ms === null) return '';
        var at = new Date(ms), now = new Date(nowMs || Date.now());
        var yesterday = new Date(now.getTime());
        yesterday.setDate(now.getDate() - 1);
        if (sameDay(at, now)) return F_TIME.format(at);
        if (sameDay(at, yesterday)) return 'yesterday ' + F_TIME.format(at);
        if (at.getFullYear() === now.getFullYear()) return F_DAY.format(at) + ', ' + F_TIME.format(at);
        return F_YEAR.format(at);
    }
    function exact(value) {
        var ms = toMs(value);
        return ms === null ? '' : F_FULL.format(new Date(ms));
    }
    function ago(ms, nowMs) {
        var s = Math.max(0, ((nowMs || Date.now()) - ms) / 1000);
        if (s < 10) return 'just now';
        if (s < 60) return Math.floor(s / 10) * 10 + ' s ago';
        var m = Math.floor(s / 60);
        if (m < 60) return m + ' min ago';
        var h = Math.floor(m / 60);
        if (h < 24) return h + ' h ago';
        return 'on ' + clock(ms, nowMs);
    }
    function dot(tone) {
        var node = el('span', 'dot');
        node.setAttribute('data-tone', tone || 'muted');
        node.setAttribute('aria-hidden', 'true');
        return node;
    }
    function normalTone(tone) {
        return tone === 'ok' || tone === 'warn' || tone === 'error' || tone === 'unknown' ? tone : 'muted';
    }

    /* A fold whose open state survives repaints (keyed). */
    function fold(state, key, summary, body, note) {
        var node = el('details', 'tc-fold');
        if (state.open[key]) node.open = true;
        node.setAttribute('data-key', key);
        var s = el('summary', '', summary);
        if (note) add(s, el('span', 'tc-sum-note', note));
        s.setAttribute('data-focus', 'fold:' + key);
        add(node, s);
        node.addEventListener('toggle', function () { state.open[key] = node.open; });
        add(node, body);
        return node;
    }

    function factsList(rows) {
        var dl = el('dl', 'tc-facts');
        rows.forEach(function (row) {
            if (!row) return;
            var dd = el('dd', 'num');
            var value = row[1];
            if (Array.isArray(value)) {
                value.forEach(function (part, index) {
                    if (index) add(dd, el('span', 'tc-sep', ' · '));
                    var span = el('span', part.dim ? 'tc-dim' : '', part.text);
                    if (part.tone) span.setAttribute('data-tone', part.tone);
                    add(dd, span);
                });
            } else if (value && typeof value.raw === 'string') {
                add(dd, el('span', 'tc-raw', value.raw));
            } else {
                dd.textContent = value === undefined || value === null || value === '' ? '—' : String(value);
            }
            if (row[2]) dd.title = row[2];
            add(dl, el('dt', '', row[0]), dd);
        });
        return dl;
    }

    function itemNode(state, item, nowMs, bare) {
        var li = el('li', 'tc-item');
        li.setAttribute('data-tone', normalTone(item.tone));
        li.setAttribute('data-kind', item.kind || '');
        // The first line already says this headline: the item adds only its time and explanation.
        var head = bare ? add(el('div', 'tc-ih tc-ih-bare'))
            : add(el('div', 'tc-ih'), dot(normalTone(item.tone)), el('span', 'tc-ih-t', item.headline));
        if (item.at) {
            var at = el('span', 'tc-ih-at num', item.atLabel ? item.atLabel + ' ' + clock(item.at, nowMs) : clock(item.at, nowMs));
            at.title = exact(item.at);
            add(head, at);
        }
        if (head.childNodes.length) add(li, head);
        var body = el('div', 'tc-ib');
        if (bare) body.classList.add('tc-ib-bare');
        (item.lines || []).forEach(function (line) {
            if (!line) return;
            if (typeof line === 'string') add(body, el('p', '', line));
            else add(body, add(el('p', ''), el('span', 'tc-strong', line.strong), line.text ? ' ' + line.text : ''));
        });
        if (item.error && item.error.text) {
            var raw = el('p', 'tc-raw', item.error.text);
            if (item.error.at) raw.title = exact(item.error.at);
            add(body, fold(state, 'err:' + (item.kind || '') + ':' + (item.detail || ''), item.error.label || 'Error text', raw));
        }
        if (body.childNodes.length) add(li, body);
        return li;
    }

    /*
     * model = {
     *   status: {text, tone}, items: [{kind, tone, headline, at, atLabel, lines, error: {label, text, at}}],
     *   facts: [[label, value]], factsNote, extra: Node, details: {title, groups: [{title, rows, note}]}
     * }
     */
    function render(root, state, model, fresh) {
        var nowMs = Date.now();
        var tc = el('div', 'tc');
        var head = el('div', 'tc-head');
        var status = add(el('p', 'tc-status'), dot(normalTone(model.status.tone)), el('span', 'tc-st-text', model.status.text));
        status.setAttribute('role', 'status');
        add(head, status, fresh);
        add(tc, head);

        if (model.items && model.items.length) {
            var list = el('ul', 'tc-items');
            list.setAttribute('aria-label', 'Needs attention');
            // One tint only when every item shares it; mixed lists stay neutral and each dot carries its tone.
            var tones = model.items.map(function (i) { return normalTone(i.tone); });
            if (tones.every(function (t) { return t === tones[0]; }) && (tones[0] === 'error' || tones[0] === 'warn')) {
                list.setAttribute('data-tone', tones[0]);
            }
            var said = String(model.status.text || '').toLowerCase();
            model.items.forEach(function (item, index) {
                var bare = index === 0 && item.headline && said.indexOf(String(item.headline).toLowerCase()) >= 0
                    && !item.at;
                add(list, itemNode(state, item, nowMs, bare));
            });
            add(tc, list);
        }
        if (model.facts && model.facts.length) {
            var block = add(el('div', 'tc-block'), factsList(model.facts));
            if (model.factsNote) add(block, el('p', 'tc-note', model.factsNote));
            add(tc, block);
        }
        // The editor is one persistent node: it keeps its place in the next card (see below).
        var slot = model.extra ? document.createComment('extra') : null;
        if (slot) add(tc, slot);
        if (model.details && model.details.groups.length) {
            var inner = el('div', 'tc-det-body');
            model.details.groups.forEach(function (group) {
                if (!group) return;
                var g = add(el('section', 'tc-group'), el('h4', '', group.title));
                if (group.rows && group.rows.length) add(g, factsList(group.rows));
                if (group.note) add(g, el('p', '', group.note));
                add(inner, g);
            });
            var det = fold(state, 'details', model.details.title || 'Details', inner);
            det.classList.add('tc-det');
            add(tc, det);
        }
        var focused = document.activeElement && document.activeElement.getAttribute
            ? document.activeElement.getAttribute('data-focus') : null;
        var current = root.firstElementChild;
        if (slot && current && current.classList.contains('tc') && model.extra.parentNode === current) {
            // Repaint around the editor without moving it: a moved node loses focus, selection and composition, so
            // the status and attention change under the reader's typing while their input stays exactly as it is.
            Array.prototype.slice.call(current.childNodes).forEach(function (child) {
                if (child !== model.extra) current.removeChild(child);
            });
            var after = false;
            Array.prototype.slice.call(tc.childNodes).forEach(function (child) {
                if (child === slot) { after = true; return; }
                if (after) current.appendChild(child); else current.insertBefore(child, model.extra);
            });
        } else {
            if (slot) slot.replaceWith(model.extra);
            root.replaceChildren(tc);
        }
        if (focused) {
            var again = root.querySelector('[data-focus="' + focused + '"]');
            if (again && again !== document.activeElement) again.focus();
        }
    }

    function loading(root) {
        var tc = el('div', 'tc');
        add(tc, add(el('div', 'tc-head'),
            add(el('p', 'tc-status'), dot('unknown'), el('span', 'tc-st-text', 'Reading status…')),
            add(el('div', 'tc-block'), el('span', 'sk'), el('span', 'sk'))));
        tc.querySelector('.tc-block').style.marginTop = '8px';
        tc.querySelectorAll('.sk')[1].style.width = '60%';
        root.replaceChildren(tc);
    }

    function installStyle() {
        var style = document.createElement('style');
        style.textContent = STYLE;
        document.head.appendChild(style);
    }

    function applyTheme(theme) {
        document.documentElement.dataset.theme = theme === 'light' ? 'light' : 'dark';
    }

    /*
     * Live card: polls `url` every `intervalMs` while the page is visible, with no tick limit. A failed read keeps
     * the last data and says so; Refresh works at any moment. `toModel(data)` returns the render model.
     */
    function mount(options) {
        var root = document.getElementById('root') || document.body;
        var bridge = window.OuroborosWidget && typeof window.OuroborosWidget === 'object' ? window.OuroborosWidget : null;
        // Requests go only through the host bridge; without it the card says so rather than reaching out itself.
        var request = bridge && typeof bridge.fetch === 'function' ? bridge.fetch.bind(bridge) : null;
        var send = request || function () { return Promise.reject(new Error(NO_BRIDGE)); };
        var disposers = [];
        var state = { open: Object.create(null), data: null, readAt: 0, failedAt: 0, failure: '', busy: false, signature: '' };
        var timer = null, ticker = null, inflight = null, disposed = false;
        var interval = options.intervalMs || 10000;

        installStyle();
        if (bridge && typeof bridge.onTheme === 'function') {
            var off = bridge.onTheme(applyTheme);
            if (typeof off === 'function') disposers.push(off);
        } else if (typeof window.matchMedia === 'function') {
            var query = window.matchMedia('(prefers-color-scheme: light)');
            applyTheme(query.matches ? 'light' : 'dark');
        }

        function freshNode() {
            var line = el('p', 'tc-fresh');
            var now = Date.now();
            var text;
            var stale = false;
            if (state.busy && !state.data) text = 'Reading…';
            else if (state.failedAt && state.failedAt >= state.readAt) {
                stale = true;
                text = state.data ? 'Couldn’t refresh · showing ' + clock(state.readAt, now) : 'Couldn’t read the status';
            } else if (state.readAt) {
                stale = now - state.readAt > interval * 3 + 5000;
                text = 'Updated ' + ago(state.readAt, now);
            } else text = '';
            var t = el('span', 'tc-fresh-t', text);
            if (state.readAt) t.title = 'Read ' + exact(state.readAt);
            if (state.failure && stale) t.title = state.failure;
            if (stale) line.setAttribute('data-stale', '');
            add(line, t, text ? el('span', 'tc-sep', '·') : null);
            var btn = el('button', 'tc-link', state.busy ? 'Refreshing…' : (stale ? 'Try again' : 'Refresh'));
            btn.type = 'button';
            btn.setAttribute('data-focus', 'refresh');
            if (state.busy) btn.setAttribute('aria-busy', 'true');
            btn.addEventListener('click', function () { if (!state.busy) read(); });
            add(line, btn);
            return line;
        }

        function paint(force) {
            if (disposed) return;
            if (!state.data) {
                if (state.failedAt) {
                    // The status is unreadable; what the card can still offer without it (an editor, explanations)
                    // stays: options.unread(state) adds it to the model.
                    var unread = {
                        status: { text: request ? options.unreadable || 'Can’t read the status' : NO_BRIDGE, tone: 'error' },
                        items: [], facts: []
                    };
                    if (typeof options.unread === 'function') {
                        var more = options.unread(state) || {};
                        unread.extra = more.extra || null;
                        unread.details = more.details || null;
                    }
                    var unreadSignature = 'unread:' + state.failedAt + ':' + JSON.stringify(unread);
                    if (!force && unreadSignature === state.signature) {
                        var stale = root.querySelector('.tc-fresh');
                        if (stale) stale.replaceWith(freshNode());
                        return;
                    }
                    state.signature = unreadSignature;
                    render(root, state, unread, freshNode());
                } else loading(root);
                return;
            }
            var model = options.toModel(state.data, state);
            // Only the model decides a rebuild; a read in flight or a failed read changes the freshness line alone.
            // Typing does not hold a rebuild back: render() keeps the editor node in place.
            var signature = JSON.stringify(model);
            if (!force && (signature === state.signature || state.pointerDown)) {
                // Never rebuild under the reader's press; the next paint after they let go catches up.
                if (state.pointerDown) state.signature = '';
                var old = state.pointerDown ? null : root.querySelector('.tc-fresh');
                if (old) old.replaceWith(freshNode());
                return;
            }
            state.signature = signature;
            render(root, state, model, freshNode());
        }

        function schedule() {
            clearTimeout(timer);
            if (disposed || document.hidden) return;
            timer = setTimeout(read, interval);
        }

        function read() {
            if (disposed) return;
            if (inflight) inflight.abort();
            var controller = typeof AbortController === 'function' ? new AbortController() : null;
            inflight = controller;
            state.busy = true;
            paint();
            send(options.url, { method: 'GET', headers: { Accept: 'application/json' }, signal: controller ? controller.signal : undefined, timeoutMs: 20000 })
                .then(function (response) {
                    if (!response.ok) throw new Error('HTTP ' + response.status);
                    return response.json();
                })
                .then(function (data) {
                    if (disposed || inflight !== controller) return;
                    state.data = data;
                    state.readAt = Date.now();
                    state.failure = '';
                })
                .catch(function (error) {
                    if (disposed || inflight !== controller) return;
                    state.failedAt = Date.now();
                    state.failure = String(error && error.message || error);
                })
                .then(function () {
                    if (disposed || inflight !== controller) return;
                    inflight = null;
                    state.busy = false;
                    paint();
                    schedule();
                });
        }

        function onVisibility() {
            if (document.hidden) { clearTimeout(timer); return; }
            read();
        }
        document.addEventListener('visibilitychange', onVisibility);
        function onFocusOut() { setTimeout(function () { if (!state.busy) paint(); }, 0); }
        function onPointerDown() { state.pointerDown = true; }
        function onPointerUp() {
            setTimeout(function () { state.pointerDown = false; if (!state.busy) paint(); }, 0);
        }
        root.addEventListener('focusout', onFocusOut);
        root.addEventListener('pointerdown', onPointerDown, true);
        window.addEventListener('pointerup', onPointerUp, true);
        window.addEventListener('pointercancel', onPointerUp, true);
        disposers.push(function () {
            root.removeEventListener('focusout', onFocusOut);
            root.removeEventListener('pointerdown', onPointerDown, true);
            window.removeEventListener('pointerup', onPointerUp, true);
            window.removeEventListener('pointercancel', onPointerUp, true);
        });
        disposers.push(function () { document.removeEventListener('visibilitychange', onVisibility); });
        ticker = setInterval(function () { if (!state.busy) paint(); }, 10000);
        disposers.push(function () { clearInterval(ticker); clearTimeout(timer); if (inflight) inflight.abort(); });

        if (typeof window.__ouroWidgetOnDispose === 'function') {
            window.__ouroWidgetOnDispose(function () {
                disposed = true;
                disposers.splice(0).forEach(function (fn) { try { fn(); } catch (_e) { /* ignore */ } });
            });
        }
        loading(root);
        read();
        return { repaint: function () { paint(true); }, read: read, state: state, request: send, bridged: !!request };
    }

    return {
        el: el, add: add, count: count, known: known, num: num, plural: plural, clock: clock, exact: exact,
        ago: ago, toMs: toMs, fold: fold, mount: mount
    };
})();
/* transport-kit:end */

    var K = TransportKit;
    var count = K.count, num = K.num, plural = K.plural;

    var LINK_LINES = {
        host_adapter_inactive: [
            'Messages from Slack are kept here but not passed on: when the bridge started, Ouroboros did not confirm a place for it.',
            { strong: 'Connect Slack in Circles,', text: 'then in Extensions turn Slack Bridge off and on.' }
        ],
        companion_not_running: [
            'The bridge’s background process stopped without closing its connection, so nothing is received or sent.',
            { strong: 'Ouroboros restarts it by itself; if this stays for more than a minute,', text: 'in Extensions turn Slack Bridge off and on to start it again.' }
        ],
        disconnected: [
            'The bridge closed its connection and stopped. Nothing is received or sent until it runs again.',
            { strong: 'In Extensions, turn Slack Bridge off and on', text: 'to start it again.' }
        ],
        not_started: [
            'Both tokens are set, but the bridge process has not run yet.',
            { strong: 'If this stays,', text: 'in Extensions turn Slack Bridge off and on to start it.' }
        ],
        missing_tokens: [
            'The bridge needs a bot token and an app token to connect to Slack.',
            { strong: 'Add SLACK_BOT_TOKEN and SLACK_APP_TOKEN', text: 'in Settings → Accounts and keys and grant them to Slack Bridge in Extensions.' }
        ],
        missing_bot_token: [
            'The bridge needs a bot token to read and send messages.',
            { strong: 'Add SLACK_BOT_TOKEN', text: 'in Settings → Accounts and keys and grant it to Slack Bridge in Extensions.' }
        ],
        missing_app_token: [
            'The bridge needs an app-level token to open its Socket Mode connection.',
            { strong: 'Add SLACK_APP_TOKEN', text: 'in Settings → Accounts and keys and grant it to Slack Bridge in Extensions.' }
        ],
        unreadable_settings: [
            'This skill’s settings file can’t be read, so the bridge can’t start.',
            { strong: 'Save Slack Bridge’s settings again', text: 'from its card in Extensions, or remove the damaged file.' }
        ],
        invalid_binding_id: [
            'The place binding saved in this skill’s settings is not valid.',
            { strong: 'Reconnect Slack', text: 'in Circles.' }
        ],
        corrupt_runtime_state: [
            'The bridge’s saved connection state can’t be read.',
            { strong: 'In Extensions, turn Slack Bridge off and on;', text: 'the bridge writes it again when it starts.' }
        ],
        socket_error: [
            'The connection attempt failed with the error below. The bridge tries again by itself, at least every 30 seconds.',
            { strong: 'If this stays,', text: 'in Extensions turn Slack Bridge off and on.' }
        ],
        companion_exited_error: [
            'The bridge’s process stopped after the error below and is not running. Ouroboros restarts it by itself a few times.',
            { strong: 'If this stays,', text: 'in Extensions turn Slack Bridge off and on.' }
        ]
    };
    // Only when Slack refused the tokens (the rule's `auth`, from the skill's own refusal codes).
    var AUTH_LINES = {
        socket_error: [
            'Slack did not accept the bridge’s tokens, so it cannot connect.',
            { strong: 'Check SLACK_BOT_TOKEN and SLACK_APP_TOKEN', text: 'in Settings → Accounts and keys and the Slack app' + '’s scopes, then in Extensions turn Slack Bridge off and on.' }
        ],
        companion_exited_error: [
            'Slack did not accept the bridge’s tokens, and the bridge stopped.',
            { strong: 'Check SLACK_BOT_TOKEN and SLACK_APP_TOKEN', text: 'in Settings → Accounts and keys, then in Extensions turn Slack Bridge off and on.' }
        ]
    };

    function itemFor(item, data) {
        var out = { kind: item.kind, detail: item.detail || '', tone: item.tone, headline: item.headline, at: item.at || null, lines: [] };
        var error = item.error && item.error.text ? item.error : null;
        var one = count(item.messages) + count(item.actions) + count(item.events) === 1;
        var them = one ? 'it' : 'them', are = one ? 'is' : 'are';
        if (item.kind === 'link') {
            out.lines = ((item.auth && AUTH_LINES[item.detail]) || LINK_LINES[item.detail] || []).slice();
            if (item.detail === 'host_adapter_inactive' && count(item.count)) {
                out.lines.splice(1, 0, plural(count(item.count), 'Slack event') + ' wait here.');
            }
            if (error) out.error = { label: item.auth ? 'Slack’s answer' : 'Error text', text: error.text };
        } else if (item.kind === 'outcome_unknown') {
            out.atLabel = 'last';
            out.lines = [
                'Sending started, then the connection broke, so nobody knows whether Slack has ' + them + '. Nothing sends ' + them + ' again by itself.',
                { strong: 'Check whether ' + (one ? 'it' : 'they') + ' arrived in the Slack conversation;', text: 'only you can decide to send ' + them + ' again.' },
                partsLine(item.partly_delivered)
            ];
            if (error) out.error = { label: 'What happened' + actionWord(error.operation), text: error.text, at: error.at };
        } else if (item.kind === 'not_delivered') {
            out.atLabel = 'last';
            out.lines = [
                (one ? 'It' : 'They') + ' could not be delivered and ' + (one ? 'is' : 'are') + ' not retried.',
                count(item.partly_delivered)
                    ? { strong: 'Fix the cause,', text: 'then ask Ouroboros to send only what is missing.' }
                    : { strong: 'Fix the cause,', text: 'then ask Ouroboros to send again if needed.' },
                partsLine(item.partly_delivered)
            ];
            if (error) out.error = { label: 'Error text' + actionWord(error.operation), text: error.text, at: error.at };
        } else if (item.kind === 'not_handled') {
            out.atLabel = 'last';
            out.lines = [
                'Ouroboros’s turn for ' + them + ' failed or was refused; ' + (one ? 'it is' : 'they are') + ' not retried.',
                { strong: 'If this repeats,', text: 'check the task history in Ouroboros.' }
            ];
            if (error) out.error = { label: 'Error text', text: error.text, at: error.at };
        } else if (item.kind === 'held') {
            var reasons = Array.isArray(item.reasons) && item.reasons.length ? item.reasons
                : (item.reason ? [{ reason: item.reason, count: 0 }] : []);
            reasons.slice(0, 4).forEach(function (reason) {
                if (reason.reason) out.lines.push({ strong: 'Reason:', text: heldReason(reason.reason)
                    + (reasons.length > 1 && count(reason.count) ? ' (' + num(count(reason.count)) + ')' : '') });
            });
            if (reasons.length > 4) out.lines.push('…and ' + plural(reasons.length - 4, 'more reason') + '.');
            out.lines.push('Kept back until the reason clears, then sent by ' + (one ? 'itself.' : 'themselves.'));
        } else if (item.kind === 'reactions') {
            out.lines = [
                'This Ouroboros can’t take Slack reactions as context, so they are skipped'
                    + (count(item.count) ? ' (' + num(count(item.count)) + ' so far)' : '') + '. Messages work as usual.',
                { strong: 'Update Ouroboros,', text: 'then in Extensions turn Slack Bridge off and on (that also rechecks a current one).' }
            ];
        } else if (item.kind === 'new_root_channels') {
            out.lines = [
                'A saved channel ID is not valid, so the whole list is ignored and no post carries the new-root signal.',
                { strong: 'Fix the IDs in Slack Bridge’s settings', text: '(its card in Extensions), then turn Slack Bridge off and on there.' }
            ];
            if (error) out.error = { label: 'Error text', text: error.text };
        } else if (item.kind === 'handoff_waiting') {
            out.atLabel = 'last try';
            out.lines = ['Ouroboros has not taken ' + them + ' yet; the bridge keeps trying by itself.'];
            if (error) out.error = { label: 'Last error', text: error.text, at: error.at };
        } else if (item.kind === 'partial') {
            out.lines = (item.gaps || []).map(function (gap) { return PARTIAL_LINES[gap]; }).filter(Boolean);
        }
        if (!error) delete out.error;
        out.lines = out.lines.filter(Boolean);
        return out;
    }

    /* Long messages go out in parts; parts Slack already confirmed must not be sent twice. */
    function partsLine(partly) {
        var n = count(partly);
        if (!n) return null;
        return (n === 1 ? '1 of them was' : num(n) + ' of them were') + ' delivered in part: the first parts are already in the'
            + ' conversation. Ask Ouroboros to check slack_receipt and send only the missing parts.';
    }

    var PARTIAL_LINES = {
        host_unanswered: 'Ouroboros did not answer the bridge’s capability check, so reactions and delivery history are not confirmed. It is asked again when the bridge restarts.',
        capabilities_unchecked: 'Ouroboros has not been asked about reactions and delivery history yet.',
        no_recent_window: 'This status has no recent-problem window, so recent failures cannot be told from old ones.'
    };

    /* The skill's own fixed refusal note still names the old Skills page; say where that is now. */
    function heldReason(text) {
        return text === 'Check this skill\'s access in Skills.' ? 'Ouroboros refused this skill\u2019s access; check it on Slack Bridge\u2019s card in Extensions.' : text;
    }

    /* The Slack action behind a failed or unknown outcome, in words (the queue's kinds, lib/runtime.py). */
    var ACTION_WORD = {
        upload_file: 'file upload', update_message: 'message edit', delete_message: 'message deletion',
        reaction_add: 'adding a reaction', reaction_remove: 'removing a reaction', pin_add: 'pinning',
        pin_remove: 'unpinning', bookmark_add: 'adding a bookmark', bookmark_remove: 'removing a bookmark',
        join_conversation: 'joining a channel', generic_api: 'Slack API call'
    };
    function actionWord(operation) {
        if (!operation) return '';
        return ' (' + (ACTION_WORD[operation] || 'Slack action') + ')';
    }

    function parts(list) { return list.filter(function (part) { return part && part.text; }); }

    var LINK_WORD = {
        connected: 'connected', starting: 'starting', reconnecting: 'reconnecting',
        disconnected: 'disconnected', not_configured: 'not set up', error: 'error'
    };
    var SOCKET_WORD = {
        connected: 'connected', starting: 'starting', authorized: 'signed in, connecting', connecting: 'connecting',
        reconnecting: 'reconnecting', disconnected: 'disconnected', stopped: 'stopped', error: 'error'
    };
    var REACTION_WORD = {
        available: 'passed to Ouroboros', update_required: 'skipped: this Ouroboros can’t take them', unknown: 'not checked yet'
    };
    var HISTORY_WORD = {
        supported: 'recorded in Ouroboros history', unsupported: 'not recorded: this Ouroboros doesn’t support it',
        unavailable: 'not recorded right now: Ouroboros didn’t answer'
    };
    var BINDING_WORD = {
        configured: 'saved', missing: 'none saved (Places connects it)', invalid: 'invalid', unreadable: 'settings file unreadable'
    };

    function toModel(data) {
        var now = Date.now();
        var link = data.link || {};
        var items = (Array.isArray(data.attention) ? data.attention : []).map(function (item) { return itemFor(item, data); });
        var messages = data.outbound_messages || {};
        var recent = data.recent_problems || {};
        var inbound = recent.inbound || {}, text = recent.outbound_text || {}, mutations = recent.mutations || {};
        var sendsAs = data.identity && data.identity.sends_as ? data.identity.sends_as.label : '';
        var facts = [];
        if (data.workspace_name) facts.push(['Workspace', data.workspace_name]);
        if (sendsAs) facts.push(['Sends as', sendsAs]);
        if (link.state === 'connected' && link.since) facts.push(['Connected', 'since ' + K.clock(link.since, now), K.exact(link.since)]);

        // Unknown is not zero: without the queue counts the card says so instead of "nothing yet".
        var countsKnown = K.known(data.inbox_delivered) && K.known(data.outbox_delivered);
        var anyTraffic = count(data.inbox_delivered) + count(data.inbox_pending) + count(data.inbox_leased)
            + count(data.inbox_failed) + count(data.inbox_ignored) + count(data.outbox_delivered) + count(data.outbox_pending)
            + count(data.outbox_leased) + count(data.outbox_failed) + count(data.outbox_uncertain);
        if (!countsKnown) facts.push(['Totals', 'unknown: the queue counts were not read']);
        if (countsKnown && anyTraffic) {
            var inProgress = count(data.inbox_pending) + count(data.inbox_leased);
            facts.push(['Incoming', parts([
                { text: num(count(data.inbox_delivered)) + ' handled' },
                { text: inProgress ? num(inProgress) + ' not finished' : '' },
                { text: count(data.inbox_failed) ? num(count(data.inbox_failed)) + ' not handled'
                    + (inbound.last_failed_at ? ', last ' + K.clock(inbound.last_failed_at, now) : '') : '', dim: true }
            ])]);
            facts.push(['Messages', parts([
                { text: num(count(messages.delivered)) + ' sent' },
                { text: count(messages.leased) ? num(count(messages.leased)) + ' sending' : '' },
                { text: count(messages.waiting) ? num(count(messages.waiting)) + ' waiting' : '' },
                { text: count(messages.held) ? num(count(messages.held)) + ' held' : '' },
                { text: count(messages.failed) ? num(count(messages.failed)) + ' not delivered'
                    + (text.last_failed_at ? ', last ' + K.clock(text.last_failed_at, now) : '') : '', dim: true },
                { text: count(messages.uncertain) ? num(count(messages.uncertain)) + ' outcome unknown'
                    + (text.last_uncertain_at ? ', last ' + K.clock(text.last_uncertain_at, now) : '') : '', dim: true }
            ])]);
            var actions = count(data.mutations_delivered) + count(data.mutations_pending) + count(data.mutations_failed) + count(data.mutations_uncertain);
            if (actions) {
                facts.push(['Actions', parts([
                    { text: num(count(data.mutations_delivered)) + ' done' },
                    { text: count(data.mutations_pending) ? num(count(data.mutations_pending)) + ' waiting' : '' },
                    { text: count(data.mutations_failed) ? num(count(data.mutations_failed)) + ' not done'
                        + (mutations.last_failed_at ? ', last ' + K.clock(mutations.last_failed_at, now) : '') : '', dim: true },
                    { text: count(data.mutations_uncertain) ? num(count(data.mutations_uncertain)) + ' outcome unknown'
                        + (mutations.last_uncertain_at ? ', last ' + K.clock(mutations.last_uncertain_at, now) : '') : '', dim: true }
                ])]);
            }
        }
        var factsNote = !countsKnown ? '' : anyTraffic
            ? 'Totals since install; not finished, sending and waiting are the queue now.'
            : 'Nothing received or sent yet.';

        return {
            status: { text: data.text || 'Status unknown', tone: data.tone || 'muted' },
            items: items,
            facts: facts,
            factsNote: factsNote,
            details: { title: 'Details', groups: detailGroups(data, now) }
        };
    }

    /* Queue counters in words; zeros are left out (the group's note says so). */
    function counts(pairs) {
        if (pairs.some(function (pair) { return !K.known(pair[0]); })) return 'unknown: not read';
        var shown = pairs.filter(function (pair) { return count(pair[0]); })
            .map(function (pair) { return num(count(pair[0])) + ' ' + pair[1]; });
        return shown.length ? shown.join(' · ') : 'none';
    }
    function rawText(text) { return { raw: String(text) }; }

    function row(label, value, title) { return value === undefined || value === null || value === '' ? null : [label, value, title]; }

    function detailGroups(data, now) {
        var link = data.link || {};
        var sendsAs = data.identity && data.identity.sends_as ? data.identity.sends_as : null;
        var lastError = data.last_error || null;
        var groups = [];
        groups.push({
            title: 'Connection',
            rows: [
                row('Link', LINK_WORD[link.state] || link.state || '—'),
                row('Socket Mode', SOCKET_WORD[data.socket_state] || String(data.socket_state || '—')),
                row('Bridge process', link.companion_alive ? 'running' : 'not running'),
                row('Connected since', link.since ? K.clock(link.since, now) : '', K.exact(link.since)),
                row('Last Slack event', K.clock(data.last_event_at, now), K.exact(data.last_event_at)),
                row('Last connection note', data.last_socket_error ? rawText(data.last_socket_error) : ''),
                row('Tokens', (data.has_bot_token ? 'bot token set' : 'bot token missing') + ' · ' + (data.has_app_token ? 'app token set' : 'app token missing'))
            ].filter(Boolean)
        });
        groups.push({
            title: 'Slack app',
            rows: [
                row('Workspace', data.workspace_name ? data.workspace_name + (data.workspace_id ? ' (' + data.workspace_id + ')' : '') : data.workspace_id),
                row('Sends as', sendsAs ? sendsAs.label + (sendsAs.id ? ' (' + sendsAs.id + ')' : '') : ''),
                row('Bot user', data.bot_user_id),
                row('Bot ID', data.bot_id),
                row('App ID', data.app_id)
            ].filter(Boolean),
            note: data.workspace_name || data.workspace_id ? '' : 'Known after the bridge first signs in to Slack.'
        });
        groups.push({
            title: 'Ouroboros',
            rows: [
                row('Events passed on', data.host_adapter_state === 'active' ? 'yes'
                    : data.host_adapter_state ? 'no: no place was confirmed when the bridge started' : 'not checked yet'),
                row('Place binding', BINDING_WORD[data.binding_state] || data.binding_state),
                row('Reactions', REACTION_WORD[data.reaction_context_state] || data.reaction_context_state),
                row('Reactions skipped', count(data.reactions_withheld) ? num(count(data.reactions_withheld)) : ''),
                row('Delivery history', HISTORY_WORD[data.history_reporting_state] || data.history_reporting_state),
                row('Delivery reports', !K.known(data.delivery_reports_acked) ? '—' : num(count(data.delivery_reports_acked)) + ' recorded'
                    + (count(data.delivery_reports_pending) ? ' · ' + num(count(data.delivery_reports_pending)) + ' waiting' : '')),
                row('Last report note', data.last_report_error ? rawText(data.last_report_error) : '')
            ].filter(Boolean),
            note: [data.history_reporting_limitation, data.reaction_context_limitation, data.reaction_history_limitation]
                .filter(Boolean).join(' ')
        });
        groups.push({
            title: 'Queues',
            rows: [
                row('Incoming events', counts([
                    [data.inbox_pending, 'waiting or in progress'], [data.inbox_leased, 'being passed on'],
                    [data.inbox_retained, 'following a reply'], [data.inbox_delivered, 'handled'],
                    [data.inbox_failed, 'not handled'], [data.inbox_ignored, 'ignored']
                ])),
                row('Outgoing parts and actions', counts([
                    [data.outbox_pending, 'waiting'], [data.outbox_held, 'of them held'],
                    [data.outbox_leased, 'sending'], [data.outbox_delivered, 'delivered'],
                    [data.outbox_failed, 'not delivered'], [data.outbox_uncertain, 'outcome unknown']
                ])),
                row('Of them, actions', counts([
                    [data.mutations_pending, 'waiting'], [data.mutations_delivered, 'done'],
                    [data.mutations_failed, 'not done'], [data.mutations_uncertain, 'outcome unknown']
                ]))
            ].filter(Boolean),
            note: 'Counts not listed are zero. Totals since install. Actions are uploads, edits, reactions and other changes in Slack. A long message is sent in parts of up to '
                + num((data.units && data.units.outbox_chunk_max_chars) || 3900) + ' characters; the queue counts parts. Ignored events are routine: the bridge’s own messages, unchanged edits and events it does not use.'
        });
        var settingsRows = [
            row('New-root channels', Array.isArray(data.new_root_channels) && data.new_root_channels.length
                ? data.new_root_channels.join(', ') : (Array.isArray(data.new_root_channels) ? 'none' : '')),
            row('Channel list problem', data.new_root_channels_error ? rawText(data.new_root_channels_error) : ''),
            row('Settings file problem', data.local_settings_error ? rawText(data.local_settings_error) : '')
        ].filter(Boolean);
        if (settingsRows.length) groups.push({ title: 'Settings', rows: settingsRows });
        var problemRows = [];
        if (lastError) {
            problemRows.push(row('When', K.clock(lastError.at, now), K.exact(lastError.at)));
            problemRows.push(row('What', [lastError.direction, lastError.kind, lastError.operation].filter(Boolean).join(' · ')
                + (lastError.state === 'uncertain' ? ' · outcome unknown' : lastError.state === 'failed' ? ' · not delivered' : '')));
            problemRows.push(row('Text', rawText(lastError.text)));
        }
        if (!lastError || data.last_delivery_error !== lastError.text) {
            problemRows.push(row('Newest queue note', data.last_delivery_error ? rawText(data.last_delivery_error) : 'none'));
        }
        groups.push({
            title: 'Last problem',
            rows: problemRows.filter(Boolean),
            note: lastError ? '' : 'No delivery has failed or ended with an unknown outcome since install.'
        });
        return groups;
    }

    K.mount({
        url: '/api/extensions/slack-bridge/status',
        intervalMs: 10000,
        unreadable: 'Can’t read the Slack Bridge status',
        toModel: toModel
    });
})();
