/* Telegram Transport — Widgets card.
 *
 * Viewer sentence: the owner glances at this card and within a second knows whether the Telegram bot works and
 * whether anything needs them; if something does, they see what, since when, what it means and what to do,
 * without decoding counters. The management group ID is set here too.
 *
 * The first line is the status route's own `text`/`tone` (the same words Places shows); the route also lists
 * the attention items in order of consequence (telegram_bot/status_summary.py). This file turns those facts into words.
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

        /* Replace only the freshness line; a keyboard reader on Refresh stays on the new one. */
        function swapFresh(old) {
            var focused = old.contains(document.activeElement);
            var line = freshNode();
            old.replaceWith(line);
            var button = focused ? line.querySelector('button') : null;
            if (button) button.focus();
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
                        if (stale) swapFresh(stale);
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
                if (old) swapFresh(old);
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
            var started = Date.now();
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
                    // A status answer counts as read only when it is one: an object with the string text and tone
                    // every transport status carries for Places. Anything else (null, [], {}) is a failed read, so
                    // the last readable snapshot and the editor stay and the card says the read failed.
                    if (!data || typeof data !== 'object' || Array.isArray(data)
                        || typeof data.text !== 'string' || typeof data.tone !== 'string') {
                        throw new Error('The status answer was not a readable status');
                    }
                    state.data = data;
                    state.readAt = Date.now();
                    // When the read that produced this data started: a card can tell data older than its own save.
                    state.dataStartedAt = started;
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
    var BASE = '/api/extensions/telegram-bot/';

    /* The old card's explanation, kept whole. */
    var BOT_DIFFERENCES = 'Telegram labels this account as a bot. People reach it in a private chat they opened '
        + 'themselves and in groups or channels it was added to; group coverage depends on privacy mode and admin '
        + 'rights. A bot cannot start a private chat with someone who never opened one. The Bot API cannot read '
        + 'earlier chat history: only messages that arrive while it runs are received. A dedicated ordinary account '
        + 'is the separate Telegram participant connector.';
    var GROUP_HINT = 'Marks this room in event facts. Work instructions stay in the place notes; no owner permissions are granted.';

    var LINK_LINES = {
        waiting_for_token: [
            'The bot needs its token to connect to Telegram.',
            { strong: 'Add TELEGRAM_PUBLIC_BOT_TOKEN', text: 'in Settings → Accounts and keys and grant it to Telegram Transport in Extensions.' }
        ],
        provider_error: [
            'The bot’s last request for new messages failed, so nothing new arrives. It retries by itself, at least once a minute.',
            { strong: 'If this stays,', text: 'the error text below says what failed.' }
        ],
        stopped: [
            'The bot’s process ended; nothing is received or sent.',
            { strong: 'Turn this skill off and on', text: 'in Extensions to start it.' }
        ]
    };

    /* A failed poll attempt, by the side it failed on (the route's poll_failure): only a matching cause gets its advice. */
    var POLL_LINES = {
        token: [
            'Telegram refused the bot token, so nothing new arrives. The bot asks again by itself, at least once a minute.',
            { strong: 'Check TELEGRAM_PUBLIC_BOT_TOKEN', text: 'in Settings → Accounts and keys: it may be revoked or mistyped.' }
        ],
        network: [
            'The bot got no answer from Telegram, so nothing new arrives. It retries by itself, at least once a minute.',
            { strong: 'If this stays,', text: 'check this computer’s network connection.' }
        ],
        telegram: [
            'Telegram answered the bot’s request with an error, so nothing new arrives. It retries by itself, at least once a minute.',
            { strong: 'If this stays,', text: 'the error text below is Telegram’s own explanation.' }
        ],
        local: [
            'Telegram answered, but the bot stopped while recording the new messages on this computer, so they are not processed yet. It retries by itself, at least once a minute.',
            { strong: 'Not a token or network problem:', text: 'the error text below names what failed here.' }
        ]
    };

    function itemFor(item) {
        var out = { kind: item.kind, detail: item.detail || '', tone: item.tone, headline: item.headline, at: item.at || null, lines: [] };
        var error = item.error && item.error.text ? item.error : null;
        var one = count(item.messages) + count(item.events) === 1;
        var them = one ? 'it' : 'them', notRetried = one ? 'it is not retried.' : 'they are not retried.';
        if (item.kind === 'link') {
            var polled = item.detail === 'provider_error' || item.detail === 'local_error';
            out.lines = ((polled && POLL_LINES[item.failure]) || LINK_LINES[item.detail] || []).slice();
            if (polled) out.atLabel = 'last successful answer';
            if (error) out.error = { label: 'Last error', text: error.text, at: error.at };
        } else if (item.kind === 'connect') {
            out.lines = ['Places can\u2019t offer this bot\u2019s chats to connect right now.'];
        } else if (item.kind === 'outcome_unknown') {
            out.atLabel = 'last';
            // partial: how many of them Telegram confirmed in part (an album's incomplete answer, a long message's
            // first parts); their answer was not lost, the rest is unknown.
            var partial = Math.min(count(item.partial), count(item.messages));
            out.lines = partial && partial === count(item.messages)
                ? ['Telegram confirmed part of ' + (one ? 'it' : 'each') + ', but nobody knows whether the rest arrived. '
                    + 'Nothing sends ' + them + ' again by itself.']
                : ['Telegram\u2019s answer to the send was lost, so nobody knows whether ' + (one ? 'it' : 'they') + ' arrived. '
                    + 'Nothing sends ' + them + ' again by itself.'
                    + (partial ? ' Telegram confirmed part of ' + plural(partial, 'item') + '.' : '')];
            out.lines.push(RECEIPT_CHECK);
            if (error) out.error = { label: 'What happened', text: error.text, at: error.at };
        } else if (item.kind === 'not_delivered') {
            out.atLabel = 'last';
            var sentPart = Math.min(count(item.partial), count(item.messages));
            out.lines = [(one ? 'It' : 'They') + ' could not be delivered; ' + notRetried];
            if (sentPart) {
                out.lines.push(one ? 'Telegram confirmed part of it before the failure.'
                    : 'Telegram confirmed part of ' + plural(sentPart, 'item') + ' before the failure.');
                out.lines.push({ strong: 'Fix the cause,', text: 'then check the receipt (telegram_receipt, confirmed_messages) '
                    + 'and ask Ouroboros to send only what is missing.' });
            } else {
                out.lines.push({ strong: 'Fix the cause,', text: 'then ask Ouroboros to send ' + them + ' again if needed.' });
            }
            if (error) out.error = { label: 'Error text', text: error.text, at: error.at };
        } else if (item.kind === 'not_handed') {
            out.atLabel = 'last';
            out.lines = [
                'Ouroboros refused ' + them + ' for good; ' + notRetried,
                { strong: 'If this repeats,', text: 'check the task history in Ouroboros.' }
            ];
            if (error) out.error = { label: 'Error text', text: error.text, at: error.at };
        } else if (item.kind === 'held') {
            // The owner's move leads (the route sorts it first; kept first here too), so the amber dot always has its reason.
            var reasons = (item.reasons || []).filter(function (reason) { return reason.action; })
                .concat((item.reasons || []).filter(function (reason) { return !reason.action; }));
            reasons.slice(0, 4).forEach(function (reason) {
                if (reason.reason) out.lines.push({ strong: reason.action ? 'Needs you:' : 'Reason:', text: heldReason(reason) + (reasons.length > 1 ? ' (' + num(count(reason.count)) + ')' : '') });
            });
            if (reasons.length > 4) out.lines.push('…and ' + plural(reasons.length - 4, 'more reason') + ' (Details › Queues counts all held items).');
            out.lines.push('Kept back until the reason clears, then sent by ' + (one ? 'itself.' : 'themselves.'));
        } else if (item.kind === 'binding') {
            out.lines = [
                'This Ouroboros predates Places, so incoming messages wait for a place binding'
                    + (count(item.count) ? ' (' + num(count(item.count)) + ' waiting).' : '.'),
                { strong: 'Update Ouroboros', text: 'and connect Telegram in Circles.' }
            ];
        } else if (item.kind === 'reactions') {
            out.lines = [
                'This Ouroboros can’t take Telegram reactions as context, so they are skipped'
                    + (count(item.count) ? ' (' + num(count(item.count)) + ' so far)' : '') + '. Messages work as usual.',
                { strong: 'Update Ouroboros,', text: 'then turn this skill off and on in Extensions.' }
            ];
        } else if (item.kind === 'handoff_waiting') {
            out.atLabel = 'since';
            // Where the last attempts stopped (the route's stages): only "submit" is a failed handoff to Ouroboros.
            var stages = item.stages || {}, total = count(item.events);
            var said = [[count(stages.submit), 'passing to Ouroboros failed'],
                [count(stages.download), 'downloading an attachment from Telegram failed'],
                [count(stages.other), 'the last attempt failed']].filter(function (pair) { return pair[0]; });
            var why = said.length === 1 && said[0][0] === total ? said[0][1]
                : said.map(function (pair) { return pair[1] + ' (' + num(pair[0]) + ')'; }).join(', ');
            out.lines = [(why ? why.charAt(0).toUpperCase() + why.slice(1) + '; t' : 'T') + 'he bot keeps trying by itself'
                + (item.next_at ? ', next at ' + K.clock(item.next_at) + '.' : '.')];
            if (error) out.error = { label: 'Last error', text: error.text, at: error.at };
        } else if (item.kind === 'partial') {
            out.lines = (item.gaps || []).map(function (gap) { return PARTIAL_LINES[gap]; }).filter(Boolean);
        }
        if (!error) delete out.error;
        out.lines = out.lines.filter(Boolean);
        return out;
    }

    /* A long message or an album can arrive in part: what reached the chat decides what is still to send. */
    /* What reached the chat decides what is still to send: the receipt lists each part Telegram confirmed. */
    var RECEIPT_CHECK = { strong: 'Check the Telegram chat and the receipt first:', text: 'telegram_receipt lists what Telegram '
        + 'confirmed (confirmed_messages); only you decide whether Ouroboros sends what is missing.' };

    var PARTIAL_LINES = {
        host_unanswered: 'Ouroboros did not answer the bot’s capability check, so reactions, delivery history and the place binding are not confirmed. The bot asks again by itself.',
        capabilities_unchecked: 'Ouroboros has not been asked about reactions, delivery history and the place binding yet.',
        no_recent_window: 'The custody store was not read, so recent problems are unknown.'
    };

    /* The route's typed kind, never the words: a refused skill's own note still names the old Skills page. */
    function heldReason(reason) {
        return reason.kind === 'skill_access'
            ? 'Ouroboros refused this skill\u2019s access; check it on Telegram Transport\u2019s card in Extensions.' : reason.reason;
    }

    function parts(list) { return list.filter(function (part) { return part && part.text; }); }
    function counts(pairs) {
        if (pairs.some(function (pair) { return !K.known(pair[0]); })) return 'unknown: not read';
        var shown = pairs.filter(function (pair) { return count(pair[0]); })
            .map(function (pair) { return num(count(pair[0])) + ' ' + pair[1]; });
        return shown.length ? shown.join(' · ') : 'none';
    }
    function rawText(text) { return { raw: String(text) }; }
    function row(label, value, title) { return value === undefined || value === null || value === '' ? null : [label, value, title]; }

    var RUNTIME_WORD = {
        polling: 'receiving (polling Telegram)', not_started: 'starting', waiting_for_token: 'waiting for the bot token',
        provider_error: 'requests failing, retrying', stopped: 'stopped'
    };
    var SOURCE_WORD = { polling: 'reading updates', outbound: 'sending', inbound: 'passing updates to Ouroboros' };
    var REPORTING_WORD = {
        supported: 'recorded in Ouroboros history', unsupported: 'not recorded: this Ouroboros doesn’t support it',
        unavailable: 'not recorded right now: Ouroboros didn’t answer', unknown: 'not checked yet'
    };

    function capability(value, checked) {
        if (!checked) return 'not checked yet';
        return value === 'available' ? 'passed to Ouroboros' : 'needs an Ouroboros update';
    }

    /* ---------- the management group setting: one persistent node, so typing survives refreshes ---------- */
    var form = { node: null, input: null, result: null, saved: null, busy: false };
    /* The saved group ID while the status can't be read: GET settings/save, once (state idle|reading|read|failed). */
    var stored = { state: 'idle', value: '' };

    function readStored(kit) {
        if (stored.state !== 'idle') return;
        stored.state = 'reading';
        kit.request(BASE + 'settings/save', { method: 'GET', headers: { Accept: 'application/json' }, timeoutMs: 20000 })
            .then(function (response) {
                if (!response.ok) throw new Error('HTTP ' + response.status);
                return response.json();
            }).then(function (body) {
                stored.value = String(body && body.management_group_id || '');
                stored.state = 'read';
            }).catch(function () {
                stored.state = 'failed';
            }).then(function () { kit.repaint(); });
    }

    /* `known` false: the saved value is not read (yet), so the field neither shows nor claims one. */
    function groupForm(kit, data, known, state) {
        var current = String(data.management_group_id || '');
        // A value this card saved stays until a status read that started after the save answers: older data (kept
        // through a failed read, or a read already out) would show the previous value and a second Save would undo it.
        if (form.confirmedAt && !(state && state.dataStartedAt > form.confirmedAt)) {
            current = form.confirmedValue;
            known = true;
        }
        var note = known === false ? (stored.state === 'failed' ? ' · saved value not read' : ' · reading…')
            : current ? ' · ' + current : ' · not set';
        if (form.node) {
            if (known !== false && form.saved !== current && document.activeElement !== form.input) {
                form.input.value = current;
                form.saved = current;
            }
            form.summaryNote.textContent = note;
            return form.node;
        }
        var body = K.el('div', 'tc-form');
        var label = K.el('label', '', 'Management group ID');
        label.setAttribute('for', 'tc-mgid');
        var input = K.el('input', 'tc-input');
        input.id = 'tc-mgid';
        input.type = 'text';
        input.autocomplete = 'off';
        input.placeholder = 'e.g. -1001234567890';
        input.value = known === false ? '' : current;
        input.setAttribute('data-focus', 'mgid');
        var save = K.el('button', 'tc-btn', 'Save');
        save.type = 'button';
        save.setAttribute('data-focus', 'mgid-save');
        var result = K.el('p', 'tc-result');
        result.setAttribute('role', 'status');
        var hint = K.el('p', 'tc-note', GROUP_HINT);
        K.add(body, label, K.add(K.el('div', 'tc-row'), input, save), result, hint);

        function show(text, tone) {
            result.textContent = text;
            if (tone) result.setAttribute('data-tone', tone); else result.removeAttribute('data-tone');
        }
        function submit() {
            if (form.busy) return;
            var value = input.value.trim();
            if (value && !/^-[0-9]+$/.test(value)) {
                show('Not saved: a group ID is a negative number, like -1001234567890.', 'error');
                return;
            }
            form.busy = true;
            save.disabled = true;
            show('Saving…');
            kit.request(BASE + 'settings/save', {
                method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                body: JSON.stringify({ management_group_id: value }), timeoutMs: 20000
            }).then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (body) {
                    if (!response.ok || !body || body.ok !== true) {
                        throw new Error(body && body.error ? body.error : 'HTTP ' + response.status);
                    }
                });
            }).then(function () {
                form.saved = value;
                form.confirmedValue = value;
                form.confirmedAt = Date.now();
                stored.value = value;
                stored.state = 'read';
                show(value ? 'Saved.' : 'Saved: no management group.', 'ok');
                kit.read();
            }).catch(function (error) {
                show('Not saved: ' + String(error && error.message || error), 'error');
            }).then(function () {
                form.busy = false;
                save.disabled = false;
            });
        }
        save.addEventListener('click', submit);
        input.addEventListener('keydown', function (event) { if (event.key === 'Enter') submit(); });
        input.addEventListener('input', function () { show(''); });

        var node = K.el('details', 'tc-fold tc-setting');
        var summary = K.el('summary', '', 'Management group');
        var summaryNote = K.el('span', 'tc-sum-note', note);
        K.add(summary, summaryNote);
        summary.setAttribute('data-focus', 'fold:mgid');
        K.add(node, summary, body);
        form.node = node;
        form.input = input;
        form.result = result;
        form.summaryNote = summaryNote;
        form.saved = known === false ? null : current;
        return node;
    }

    function toModel(data, state, kit) {
        var now = Date.now();
        var items = (Array.isArray(data.attention) ? data.attention : []).map(itemFor);
        var recent = data.recent_problems || {};
        var outbound = recent.outbound || {}, inbound = recent.inbound || {};
        var sendsAs = data.identity && data.identity.sends_as ? data.identity.sends_as.label : '';
        var facts = [];
        var bot = sendsAs || (data.bot_label && !/^not (connected|configured)$/.test(data.bot_label) ? data.bot_label : '');
        // A token getMe has not confirmed yet: the label shown is the earlier bot's.
        var unconfirmed = data.bot_unconfirmed === true;
        if (bot) facts.push(['Bot', bot + (unconfirmed ? ' (not confirmed for the current token)' : '')]);
        if (data.last_event_at) facts.push(['Last update', K.clock(data.last_event_at, now), K.exact(data.last_event_at)]);
        if (data.last_delivery_at) facts.push(['Last delivery', K.clock(data.last_delivery_at, now), K.exact(data.last_delivery_at)]);

        // Unknown is not zero: without the custody counts the card says so instead of "nothing yet".
        var countsKnown = K.known(data.inbox_submitted) && K.known(data.outbox_delivered);
        var anyTraffic = count(data.inbox_submitted) + count(data.inbox_waiting) + count(data.inbox_leased)
            + count(data.inbox_failed) + count(data.reactions_withheld) + count(data.outbox_delivered)
            + count(data.outbox_waiting) + count(data.outbox_leased) + count(data.outbox_failed) + count(data.outbox_uncertain);
        if (!countsKnown) facts.push(['Totals', 'unknown: the custody counts were not read']);
        if (countsKnown && anyTraffic) {
            var inWaiting = count(data.inbox_waiting) + count(data.inbox_leased);
            facts.push(['Incoming', parts([
                { text: num(count(data.inbox_submitted)) + ' passed to Ouroboros' },
                { text: inWaiting ? num(inWaiting) + ' waiting' : '' },
                { text: count(data.inbox_failed) ? num(count(data.inbox_failed)) + ' refused'
                    + (inbound.last_failed_at ? ', last ' + K.clock(inbound.last_failed_at, now) : '') : '', dim: true }
            ])]);
            // outbox_waiting includes the held rows; leased rows are the ones being sent now.
            var queued = Math.max(0, count(data.outbox_waiting) - count(data.outbox_held));
            facts.push(['Outgoing', parts([
                { text: num(count(data.outbox_delivered)) + ' delivered' },
                { text: count(data.outbox_leased) ? num(count(data.outbox_leased)) + ' sending' : '' },
                { text: queued ? num(queued) + ' waiting' : '' },
                { text: count(data.outbox_held) ? num(count(data.outbox_held)) + ' held' : '' },
                { text: count(data.outbox_failed) ? num(count(data.outbox_failed)) + ' not delivered'
                    + (outbound.last_failed_at ? ', last ' + K.clock(outbound.last_failed_at, now) : '') : '', dim: true },
                { text: count(data.outbox_uncertain) ? num(count(data.outbox_uncertain)) + ' outcome unknown'
                    + (outbound.last_uncertain_at ? ', last ' + K.clock(outbound.last_uncertain_at, now) : '') : '', dim: true }
            ])]);
        }
        return {
            status: { text: data.text || 'Status unknown', tone: data.tone || 'muted' },
            items: items,
            facts: facts,
            factsNote: !countsKnown ? '' : anyTraffic ? 'Totals since install; waiting, sending and held are the queue now.' : 'No messages received or sent yet.',
            extra: groupForm(kit, data, true, state),
            details: { title: 'Details', groups: detailGroups(data, now) }
        };
    }

    function detailGroups(data, now) {
        // Without the custody counts "none yet" would be a guess: the times stay unknown (—).
        var countsKnown = K.known(data.inbox_submitted) && K.known(data.outbox_delivered);
        var sendsAs = data.identity && data.identity.sends_as ? data.identity.sends_as : null;
        var checked = data.host_checked === true;
        var groups = [];
        groups.push({
            title: 'Bot',
            rows: [
                row('State', data.runtime_state === 'polling' && data.poll_result !== 'ok'
                    ? (data.poll_result === 'failed' ? 'retrying after a failed request' : 'connecting: no answer from Telegram yet')
                    : RUNTIME_WORD[data.runtime_state] || data.runtime_state),
                row('Bot', data.bot_label),
                row('Sends as', sendsAs ? sendsAs.label + (sendsAs.id ? ' (' + sendsAs.id + ')' : '') : ''),
                row('Last successful getUpdates answer', K.clock(data.provider_ok_at, now), K.exact(data.provider_ok_at)),
                row('Last update', K.clock(data.last_event_at, now) || (countsKnown ? 'none yet' : '—'), K.exact(data.last_event_at)),
                row('Last delivery', K.clock(data.last_delivery_at, now) || (countsKnown ? 'none yet' : '—'), K.exact(data.last_delivery_at)),
                row('Telegram offset', K.known(data.telegram_offset) ? String(data.telegram_offset) : '—'),
                row('Chats to connect', Array.isArray(data.connect)
                    ? (data.connect.length ? plural(data.connect.length, 'chat') + ' offered in Places' : 'no chats available')
                    : (data.connect_unavailable || 'unavailable'))
            ].filter(Boolean)
        });
        groups.push({
            title: 'Ouroboros',
            rows: [
                row('Delivery history', REPORTING_WORD[data.delivery_reporting] || data.delivery_reporting),
                row('Reactions in Host context', capability(data.reaction_context, checked)),
                row('Reaction history', capability(data.reaction_history, checked)),
                row('Place binding', data.binding_state === 'configured' ? 'saved'
                    : !checked ? 'none saved (not checked whether this Ouroboros needs one)'
                        : data.places_version ? 'none saved (Places connects it)' : 'none saved; this Ouroboros needs one'),
                row('Early answers', !checked ? 'not checked yet'
                    : data.presence_continuation_version === 2 ? 'supported' : 'each turn waits for its end'),
                row('Delivery reports', counts([[data.delivery_reports_pending, 'waiting'], [data.delivery_reports_held, 'held']]))
            ].filter(Boolean)
        });
        groups.push({
            title: 'Queues',
            rows: [
                row('Incoming updates', counts([
                    [data.inbox_waiting, 'waiting'], [data.inbox_leased, 'being passed on'],
                    [data.inbox_retained, 'following a reply'], [data.inbox_submitted, 'passed to Ouroboros'],
                    [data.inbox_failed, 'refused by Ouroboros'], [data.reactions_withheld, 'reactions skipped']
                ])),
                row('Outgoing', counts([
                    [data.outbox_waiting, 'waiting'], [data.outbox_held, 'of them held'], [data.outbox_leased, 'sending'],
                    [data.outbox_delivered, 'delivered'], [data.outbox_failed, 'not delivered'],
                    [data.outbox_uncertain, 'outcome unknown']
                ])),
                row('Ouroboros work', counts([
                    [data.work_waiting, 'waiting'], [data.work_leased, 'being read'], [data.work_terminal, 'finished']
                ])),
                row('Group history unreadable', count(data.group_backfill_unreadable) ? num(count(data.group_backfill_unreadable)) : '')
            ].filter(Boolean),
            note: 'Counts not listed are zero. Totals since install; waiting, sending and held are the queue now.'
        });
        var errorRows = data.last_error ? [
            row('When', K.clock(data.last_error_at, now) || 'before this process started', K.exact(data.last_error_at)),
            row('While', SOURCE_WORD[data.last_error_source] || ''),
            row('Text', rawText(data.last_error))
        ].filter(Boolean) : [];
        groups.push({
            title: 'Last error',
            rows: errorRows,
            note: data.last_error ? 'Kept until the bot confirms its token again or the skill is reloaded; a restart of the bot keeps it.'
                : 'No error recorded since the skill was loaded.'
        });
        groups.push({ title: 'About a Telegram bot', rows: [], note: BOT_DIFFERENCES });
        return groups;
    }

    var kit = K.mount({
        url: BASE + 'status',
        intervalMs: 10000,
        unreadable: 'Can’t read the Telegram Transport status',
        toModel: function (data, state) { return toModel(data, state, kit); },
        // The status is unreadable, but the setting is its own route: the editor and the explanations stay.
        unread: function () {
            readStored(kit);
            return {
                extra: groupForm(kit, { management_group_id: stored.value }, stored.state === 'read', kit.state),
                details: { title: 'Details', groups: [{ title: 'About a Telegram bot', rows: [], note: BOT_DIFFERENCES }] }
            };
        }
    });
})();
