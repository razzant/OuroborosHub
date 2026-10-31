/* Календарь Уробороса — module widget (classic script inside the host's sandboxed frame).
 * Data only through OuroborosWidget.fetch on this skill's own routes; no storage, no parent messages
 * beyond the host bridge. Day view (hour grid + upcoming list) is primary, Week is the second mode.
 * Click on the grid creates, a card edits, drag moves, the bottom edge resizes; narrow screens use
 * buttons and fields instead of drag. Hidden (service) events are hatched busy time until the toggle
 * reveals them; «обычно» (soft) preferences are dotted bands, never solid bookings.
 * «Аккаунты» shows what is connected and ready, signs in to Google through the host's link opener, connects
 * CalDAV/Яндекс with an ordinary form and disconnects. A card shows how an event relates to its copies and offers
 * the explicit link actions; nothing is resolved on its own.
 */
(function () {
    'use strict';

    var ROOT = '/api/extensions/calendar/';
    var POLL_MS = 30000;
    var HOUR_SCALES = [32, 44, 64];
    var GRID_VIEWPORT_PX = 400;
    var HOUR_PX = HOUR_SCALES[1];
    var SNAP_MIN = 15;
    var DAY_START_H = 0;

    var state = {
        view: 'day', date: null, data: null, error: '', loading: false, showHidden: false,
        selectedCals: null, editing: null, drag: null, disposed: false, theme: 'light', poll: null, dirty: false, notice: '',
        hourScale: 1, gridScrollTop: 0, focusNow: true, zooming: false, linksOpen: false, linkError: ''

    };
    var root = document.getElementById('root') || document.body;

    /* ---------- utils ---------- */
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    function localDate(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
    function parseISO(s) {   // wall-clock of the OWNER's zone as the server printed it; the browser zone is irrelevant here
        if (!s) return null;
        var m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(s);
        return m ? new Date(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0), 0, 0) : new Date(s);
    }
    function fmtHM(d) { return pad(d.getHours()) + ':' + pad(d.getMinutes()); }
    function fmtLocalInput(d) { return localDate(d) + 'T' + fmtHM(d); }
    function addDays(dateStr, n) { var d = new Date(dateStr + 'T00:00:00'); d.setDate(d.getDate() + n); return localDate(d); }
    function minutesOf(d) { return d.getHours() * 60 + d.getMinutes(); }
    function el(tag, attrs, children) {
        var node = document.createElement(tag);
        if (attrs) Object.keys(attrs).forEach(function (k) {
            if (k === 'class') node.className = attrs[k];
            else if (k === 'style') node.setAttribute('style', attrs[k]);
            else if (k.indexOf('on') === 0) node.addEventListener(k.slice(2), attrs[k]);
            else if (k === 'text') node.textContent = attrs[k];
            else if (attrs[k] != null) node.setAttribute(k, attrs[k]);
        });
        (children || []).forEach(function (c) { if (c) node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
        return node;
    }
    function api(path, init) {
        return OuroborosWidget.fetch(ROOT + path, init).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (j) {
                if (!r.ok) { var err = new Error(j.message || ('HTTP ' + r.status)); err.body = j; throw err; }   // the body says e.g. «saved»
                return j;
            });
        });
    }
    function post(path, body) { return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); }

    /* ---------- styles ---------- */
    var css = [
        ':root,:root[data-theme="light"]{--bg:#fff;--fg:#1c1c1e;--muted:#6b6f76;--line:#e3e5e8;--soft:#f4f5f7;--accent:#3b6cf6;--accent-bg:#e8effd;--busy:#dfe3ea;--danger:#c0392b;--card:#fff;--ok:#176b49;--warn:#805000;--ok-bg:#e6f4ec;--warn-bg:#fdf3dc;--err-bg:#fdecea}',
        ':root[data-theme="dark"]{--bg:#121316;--fg:#ececef;--muted:#9a9ea6;--line:#2a2d33;--soft:#1b1d22;--accent:#7aa2ff;--accent-bg:#1f2a44;--busy:#2c3038;--danger:#ff7b6b;--card:#181a1f;--ok:#83e499;--warn:#f7cb58;--ok-bg:#17301f;--warn-bg:#3a2f14;--err-bg:#3a1f1c}',
        'html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);font:14px/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}',
        '#root{padding:12px;box-sizing:border-box}',
        '.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}',
        '.bar .title{font-weight:600;font-size:16px;margin-right:auto}',
        'button{background:var(--soft);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:5px 10px;cursor:pointer;font:inherit}',
        'button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{color:var(--danger)}button.on{background:var(--accent-bg);border-color:var(--accent)}',
        ':root[data-theme="dark"] button.primary{color:#0e1116}',   // white on the light accent of the dark palette is only about 2.5:1
        '.grid-scroll{height:' + GRID_VIEWPORT_PX + 'px;max-height:55vh;overflow:auto;border:1px solid var(--line);border-radius:10px;overscroll-behavior:contain;background:var(--card)}',
        '.grid{display:grid;grid-template-columns:48px minmax(0,1fr);--hour-px:44px}',
        '.grid.week{grid-template-columns:48px repeat(7,minmax(0,1fr))}',
        '.hours{position:relative;padding-bottom:40px}.hour{height:var(--hour-px);border-top:1px solid var(--line);font-size:11px;color:var(--muted);padding:2px 4px;box-sizing:border-box}',
        '.col{position:relative;padding-bottom:40px;border-left:1px solid var(--line);cursor:crosshair}.col .hline{height:var(--hour-px);border-top:1px solid var(--line);box-sizing:border-box}',
        '.colhead{font-size:12px;color:var(--muted);text-align:center;padding:6px 0;border-bottom:1px solid var(--line);border-left:1px solid var(--line);background:var(--soft)}.colhead.today{color:var(--accent);font-weight:600}',
        '.corner{border-bottom:1px solid var(--line);background:var(--soft)}',
        '.grid > .corner:first-child,.grid > .colhead{position:sticky;top:0;z-index:3}',
        '.scale{display:flex;align-items:center;gap:3px}.scale span{min-width:38px;text-align:center;color:var(--muted);font-size:12px}',
        '.ev{position:absolute;left:3px;right:6px;border-radius:7px;padding:3px 6px;font-size:12px;box-sizing:border-box;overflow:hidden;background:var(--accent-bg);border-left:3px solid var(--accent);cursor:grab;user-select:none}',
        '.ev.hidden{opacity:.75;border-left-style:dashed}.ev.pending{outline:1px dashed var(--muted)}.ev.conflict{outline:2px solid var(--danger)}',
        '.ev .t{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ev .m{color:var(--muted);font-size:11px}',
        '.ev .rs{position:absolute;left:0;right:0;bottom:0;height:7px;cursor:ns-resize}',
        '.hatch{position:absolute;left:0;right:0;background:repeating-linear-gradient(135deg,var(--busy) 0 6px,transparent 6px 12px);opacity:.9;pointer-events:none}',
        '.usual{position:absolute;left:0;right:0;border-top:2px dotted var(--accent);border-bottom:2px dotted var(--accent);opacity:.7;font-size:11px;color:var(--accent);padding:0 6px;box-sizing:border-box;pointer-events:none}',
        '.now{position:absolute;left:0;right:0;border-top:2px solid #e5333f;pointer-events:none;z-index:2;filter:drop-shadow(0 1px 2px rgba(0,0,0,.35))}',
        '.now::before{content:"";position:absolute;left:0;top:-5px;width:8px;height:8px;border-radius:50%;background:#e5333f}',
        '.allday{display:flex;flex-wrap:wrap;gap:6px;padding:6px 8px;border-bottom:1px solid var(--line);min-height:20px;border-left:1px solid var(--line)}',
        '.chip{background:var(--accent-bg);border-radius:6px;padding:2px 8px;font-size:12px;cursor:pointer}',
        '.list{margin-top:12px}.list h3{font-size:13px;color:var(--muted);margin:8px 0 4px;font-weight:600}.row{display:flex;gap:10px;padding:6px 8px;border-radius:8px;cursor:pointer}.row:hover{background:var(--soft)}.row .when{color:var(--muted);min-width:96px}',
        '.card{position:fixed;inset:0;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;z-index:9}',
        '.card form{background:var(--card);color:var(--fg);border-radius:12px;padding:16px;width:min(440px,94vw);max-height:92vh;overflow:auto;box-shadow:0 10px 40px rgba(0,0,0,.3)}',
        '.card label{display:block;font-size:12px;color:var(--muted);margin:8px 0 2px}.card input,.card select,.card textarea{width:100%;box-sizing:border-box;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}',
        '.card input[type="checkbox"]{width:auto;margin:0 6px 0 0;vertical-align:middle}.card label.inline{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--fg);margin:8px 0 2px}',
        '.card .actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}.card .actions .spacer{flex:1}',
        '.card input:disabled,.card select:disabled,.card textarea:disabled,.acc button:disabled,.linkbox button:disabled,.linkissues button:disabled{opacity:.55;cursor:not-allowed}',
        '.status{font-size:12px;color:var(--muted);margin-top:8px}.err{color:var(--danger)}',
        '.cals{display:flex;flex-wrap:wrap;gap:6px}.cals button{font-size:12px;padding:3px 8px}',
        /* accounts */
        'button.attn{position:relative}button.attn::after{content:"";position:absolute;top:-3px;right:-3px;width:9px;height:9px;border-radius:50%;background:var(--warn);border:2px solid var(--bg)}',
        '.acc,.linkissues{border:1px solid var(--line);border-radius:10px;padding:10px;margin-bottom:10px;background:var(--card)}',
        '.acc h3,.acc h4{margin:0;font-weight:600}.acc h3{font-size:15px}.acc h4{font-size:13px;color:var(--muted);margin:14px 0 4px}',
        '.acc-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}.acc-head h3{margin-right:auto}',
        '.acc details{margin:2px 0 8px}.acc summary{cursor:pointer;font-size:13px;color:var(--muted);padding:2px 0}',
        '.acc-row{display:flex;gap:10px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;padding:8px;border-radius:8px;background:var(--soft);margin-bottom:6px}.acc-row.bad{background:var(--err-bg)}',
        '.acc-main{flex:1 1 220px;min-width:0}.acc-name{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-weight:600;overflow-wrap:anywhere}',
        '.tag{font-size:11px;font-weight:500;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:0 7px}',
        '.acc-sub{font-size:12px;color:var(--muted);overflow-wrap:anywhere;margin-top:2px}.acc-sub.err{color:var(--danger)}',
        '.acc-state{display:flex;align-items:center;gap:6px;margin-top:3px;font-size:13px}.dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--muted)}.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.err{background:var(--danger)}',
        '.acc-actions{display:flex;gap:6px;flex-wrap:wrap;align-items:flex-start}.acc button,.linkissues button,.linkbox button{font-size:12px;padding:3px 9px}.acc button.primary{font-size:13px;padding:5px 12px}',
        '.acc-confirm{flex:1 1 100%;font-size:13px;border-top:1px solid var(--line);padding-top:8px}.acc-confirm p{margin:0 0 8px;overflow-wrap:anywhere}',
        '.msg{display:flex;gap:8px;align-items:flex-start;justify-content:space-between;border-radius:8px;padding:6px 10px;margin:6px 0;font-size:13px;overflow-wrap:anywhere}.msg>span{min-width:0}',
        '.msg.err{background:var(--err-bg);color:var(--danger)}.msg.ok{background:var(--ok-bg);color:var(--ok)}.msg.info{background:var(--soft);color:var(--fg)}',
        '.msg button{flex:none;background:transparent;border-color:transparent;color:inherit}',
        '.f{margin:8px 0}.f[hidden]{display:none}.f label{display:block;font-size:12px;color:var(--muted);margin:0 0 2px}.f .hint{font-size:11px;color:var(--muted);margin-top:2px}',
        '.acc input[type="text"],.acc input[type="url"],.acc input[type="password"]{width:100%;box-sizing:border-box;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}',
        '.seg{display:flex;gap:6px;flex-wrap:wrap}.seg label{display:inline-flex;align-items:center;gap:4px;margin:0;font-size:13px;color:var(--fg);border:1px solid var(--line);border-radius:999px;padding:3px 10px;cursor:pointer}.seg label.on{background:var(--accent-bg);border-color:var(--accent)}',
        '.wait{border:1px dashed var(--line);border-radius:8px;padding:8px;margin-top:8px;font-size:13px;overflow-wrap:anywhere}.wait .acc-actions{margin-top:8px}.wait a{color:var(--accent);align-self:center}',
        /* links between copies */
        '.linkbox{border:1px solid var(--line);border-radius:8px;padding:8px;margin:10px 0 2px;font-size:13px;background:var(--soft)}.linkbox .fact,.linkbox .preview{overflow-wrap:anywhere}',
        '.linkbox ul{margin:6px 0 0;padding-left:18px;color:var(--muted);font-size:12px}.linkbox li{overflow-wrap:anywhere}.linkbox .acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}',
        '.linkbox .confirm{border-top:1px solid var(--line);margin-top:8px;padding-top:8px}.linkbox .confirm p{margin:0 0 8px}.linkbox select{width:auto;max-width:100%;margin-bottom:8px}',
        '.linkissues .linkhead{width:100%;text-align:left}.issue{display:flex;gap:10px;align-items:flex-start;justify-content:space-between;padding:8px 0 0}.issue .grow{flex:1 1 0;min-width:0;overflow-wrap:anywhere}',
        '@media (max-width:600px){.grid.week{grid-template-columns:36px repeat(7,1fr)}.ev{font-size:11px;padding:2px 4px}.bar .title{width:100%}.acc-actions{width:100%}}'
    ].join('\n');
    var styleTag = el('style', { text: css });
    document.head.appendChild(styleTag);

    /* ---------- theme ---------- */
    function applyTheme(t) { state.theme = t === 'dark' ? 'dark' : 'light'; document.documentElement.dataset.theme = state.theme; }
    var offTheme = null;
    if (window.OuroborosWidget && typeof OuroborosWidget.onTheme === 'function') {
        try { offTheme = OuroborosWidget.onTheme(applyTheme); } catch (e) { /* older host: fall back below */ }
    }
    if (!document.documentElement.dataset.theme && window.matchMedia) {
        applyTheme(window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    }

    /* ---------- data ---------- */
    function load() {
        if (state.disposed) return Promise.resolve();
        state.loading = true;
        var q = 'agenda?view=' + state.view + '&date=' + (state.date || '') + (state.showHidden ? '&show_hidden=1' : '') +
            (state.selectedCals ? '&calendars=' + encodeURIComponent(state.selectedCals.length ? state.selectedCals.join(',') : '-') : '');
        return api(q).then(function (d) {
            if (!state.date && state.data && state.data.anchor !== d.anchor) state.focusNow = true;
            state.data = d; state.error = '';
            /* A null date follows the host's current day/week across midnight. Only arrows pin a date. */
        }).catch(function (e) { state.error = e.message || String(e); }).then(function () { state.loading = false; render(); });
    }

    /* ---------- rendering ---------- */
    function render() {
        var previousGrid = root.querySelector('.grid-scroll');
        if (previousGrid && !state.focusNow && !state.zooming) state.gridScrollTop = previousGrid.scrollTop;
        var kept = accKeepFocus();
        root.innerHTML = '';
        var d = state.data;
        var bar = el('div', { class: 'bar' }, [
            el('span', { class: 'title', text: 'Календарь' + (d ? ' · ' + (state.view === 'week' ? 'неделя' : d.days[0].weekday + ' ' + d.anchor) : '') }),
            el('button', { text: '‹', onclick: function () { shift(-1); } }),
            el('button', { text: 'Сегодня', onclick: function () { state.date = null; state.focusNow = true; load(); } }),
            el('button', { text: '›', onclick: function () { shift(1); } }),
            el('button', { text: 'День', class: state.view === 'day' ? 'on' : '', onclick: function () { state.view = 'day'; state.focusNow = true; load(); } }),
            el('button', { text: 'Неделя', class: state.view === 'week' ? 'on' : '', onclick: function () { state.view = 'week'; state.focusNow = true; load(); } }),
            el('div', { class: 'scale', role: 'group', 'aria-label': 'Масштаб временной сетки' }, [
                el('button', { text: '−', title: 'Уменьшить масштаб сетки', 'aria-label': 'Уменьшить масштаб сетки', disabled: state.hourScale === 0 ? 'disabled' : null,
                    onclick: function () { changeScale(-1); } }),
                el('span', { text: Math.round(HOUR_SCALES[state.hourScale] / HOUR_SCALES[1] * 100) + '%' }),
                el('button', { text: '+', title: 'Увеличить масштаб сетки', 'aria-label': 'Увеличить масштаб сетки', disabled: state.hourScale === HOUR_SCALES.length - 1 ? 'disabled' : null,
                    onclick: function () { changeScale(1); } })
            ]),
            el('button', { text: 'Служебные', class: state.showHidden ? 'on' : '', title: 'Показать служебные события и «обычно»', onclick: function () { state.showHidden = !state.showHidden; load(); } }),
            el('button', { text: 'Аккаунты', class: (acc.open ? 'on' : '') + (d && accAttention(d) ? ' attn' : ''), 'aria-expanded': acc.open ? 'true' : 'false',
                title: d && accAttention(d) ? 'Есть аккаунт, которому нужно внимание' : 'Подключённые аккаунты: Google, Яндекс, CalDAV', onclick: accToggle }),
            el('button', { text: '⚙', title: 'Напоминания и синхронизация', class: state.panel ? 'on' : '', onclick: function () { togglePanel(); } }),
            el('button', { text: '+ Событие', class: 'primary', onclick: function () { openCard(null, defaultStart()); } })
        ]);
        root.appendChild(bar);
        if (state.panel) root.appendChild(renderPanel());
        if (acc.open) { root.appendChild(accNode()); accRestoreFocus(kept); }
        if (state.error) root.appendChild(el('div', { class: 'status err', text: 'Ошибка: ' + state.error }));
        if (state.notice) root.appendChild(el('div', { class: 'status', text: state.notice }));
        if (!d) { root.appendChild(el('div', { class: 'status', text: state.loading ? 'Загрузка…' : 'Нет данных' })); return; }
        var issues = renderLinkIssues(d);
        if (issues) root.appendChild(issues);
        root.appendChild(renderCalendarChips(d));
        var scroller = el('div', { class: 'grid-scroll', 'aria-label': 'Временная сетка календаря', tabindex: '0' }, [renderGrid(d)]);
        root.appendChild(scroller);
        if (state.focusNow && scroller.querySelector('.now')) {
            var line = scroller.querySelector('.now');
            scroller.scrollTop += line.getBoundingClientRect().top - scroller.getBoundingClientRect().top - scroller.clientHeight * 0.45;
            state.gridScrollTop = scroller.scrollTop;
            state.focusNow = false;
        } else {
            scroller.scrollTop = state.gridScrollTop;
            state.focusNow = false;
        }
        state.zooming = false;
        if (state.view === 'day') root.appendChild(renderList(d));
        root.appendChild(renderStatus(d));
        if (state.editing) root.appendChild(renderCard());
    }

    function renderCalendarChips(d) {
        var wrap = el('div', { class: 'cals' });
        d.calendars.filter(function (c) { return c.visible; }).forEach(function (c) {
            var on = !state.selectedCals || state.selectedCals.indexOf(c.id) >= 0;
            wrap.appendChild(el('button', { text: c.name, class: on ? 'on' : '', title: c.provider, onclick: function () {
                var all = d.calendars.filter(function (x) { return x.visible; }).map(function (x) { return x.id; });
                var cur = state.selectedCals ? state.selectedCals.slice() : all.slice();
                var i = cur.indexOf(c.id); if (i >= 0) cur.splice(i, 1); else cur.push(c.id);
                state.selectedCals = cur.length === all.length ? null : cur; load();
            } }));
        });
        return wrap;
    }

    function renderGrid(d) {
        var days = d.days;
        var grid = el('div', { class: 'grid' + (state.view === 'week' ? ' week' : ''), style: '--hour-px:' + HOUR_PX + 'px' });
        grid.appendChild(el('div', { class: 'corner' }));
        var todayStr = localDate(ownerNow());
        days.forEach(function (day) {
            grid.appendChild(el('div', { class: 'colhead' + (day.date === todayStr ? ' today' : ''), text: day.weekday + ' ' + day.date.slice(8) + '.' + day.date.slice(5, 7) }));
        });
        grid.appendChild(el('div', { class: 'corner' }));
        days.forEach(function (day) {
            var lane = el('div', { class: 'allday' });
            d.events.filter(function (e) { return e.all_day && e.start.slice(0, 10) <= day.date && day.date < (e.end.slice(0, 10) || e.start.slice(0, 10)); })
                .forEach(function (e) { lane.appendChild(el('span', { class: 'chip', text: e.title, onclick: function () { openCard(e); } })); });
            grid.appendChild(lane);
        });
        var hours = el('div', { class: 'hours' });
        for (var h = DAY_START_H; h < 24; h++) hours.appendChild(el('div', { class: 'hour', text: pad(h) + ':00' }));
        grid.appendChild(hours);
        days.forEach(function (day) { grid.appendChild(renderColumn(d, day)); });
        return grid;
    }

    function yFor(date, dayStr) {
        var dd = date; var top = minutesOf(dd) * HOUR_PX / 60;
        if (localDate(dd) < dayStr) top = 0;
        if (localDate(dd) > dayStr) top = 24 * HOUR_PX;
        return top;
    }

    function renderColumn(d, day) {
        var col = el('div', { class: 'col', 'data-date': day.date });
        for (var h = DAY_START_H; h < 24; h++) col.appendChild(el('div', { class: 'hline' }));
        d.hidden_busy.forEach(function (b) { if (b.all_day || !overlapsDay(b, day.date)) return; place(col, day.date, b.start, b.end, el('div', { class: 'hatch', title: 'занято (служебное)' })); });
        d.usual.forEach(function (u) { if (!overlapsDay(u, day.date)) return; place(col, day.date, u.start, u.end, el('div', { class: 'usual', text: 'обычно · ' + u.title })); });
        var timed = d.events.filter(function (e) { return !e.all_day; });
        var laid = layout(timed.filter(function (e) { return overlapsDay(e, day.date); }), day.date);
        laid.forEach(function (item) {
            var e = item.ev;
            var cls = 'ev' + (e.visibility === 'hidden' ? ' hidden' : '') + ((e.sync_state || '').indexOf('pending') === 0 ? ' pending' : '') + (e.sync_state === 'conflict' ? ' conflict' : '');
            var node = el('div', { class: cls, title: e.title }, [
                el('div', { class: 't', text: e.title }),
                el('div', { class: 'm', text: fmtHM(parseISO(e.start)) + '–' + fmtHM(parseISO(e.end)) + (e.calendar_name ? ' · ' + e.calendar_name : '') + (e.sync_state === 'pending' ? ' · ожидает' : e.sync_state === 'pending_delete' ? ' · удаляется' : e.sync_state === 'conflict' ? ' · конфликт' : '') + linkNote(e) }),
                el('div', { class: 'rs' })
            ]);
            node.style.left = 'calc(' + (item.left * 100) + '% + 3px)';
            node.style.right = 'calc(' + ((1 - item.left - item.width) * 100) + '% + 4px)';
            place(col, day.date, e.start, e.end, node);
            attachDrag(node, e, col);
            col.appendChild(node);
        });
        var now = ownerNow();
        if (localDate(now) === day.date) { var nl = el('div', { class: 'now' }); nl.style.top = (minutesOf(now) * HOUR_PX / 60) + 'px'; col.appendChild(nl); }
        col.addEventListener('click', function (ev) {
            if (ev.target !== col && !ev.target.classList.contains('hline')) return;
            var rect = col.getBoundingClientRect();
            if (ev.clientY - rect.top >= 24 * HOUR_PX) return; // breathing room below 23:55 is not another day
            var mins = Math.floor(((ev.clientY - rect.top) / HOUR_PX * 60) / SNAP_MIN) * SNAP_MIN;
            var start = new Date(day.date + 'T00:00:00'); start.setMinutes(mins);
            openCard(null, start);
        });
        return col;
    }

    function overlapsDay(e, dayStr) { var s = e.start.slice(0, 10), en = e.end.slice(0, 10); return s <= dayStr && dayStr <= en && !(en === dayStr && e.end.slice(11, 16) === '00:00' && s !== dayStr); }

    function place(col, dayStr, startISO, endISO, node) {
        var s = parseISO(startISO), e = parseISO(endISO);
        var top = yFor(s, dayStr), bottom = yFor(e, dayStr);
        if (bottom <= top) bottom = top + HOUR_PX / 4;
        node.style.top = top + 'px'; node.style.height = Math.max(14, bottom - top - 2) + 'px';
        col.appendChild(node);
    }

    function layout(events, dayStr) {
        var items = events.map(function (e) { return { ev: e, s: yFor(parseISO(e.start), dayStr), e: yFor(parseISO(e.end), dayStr), left: 0, width: 1 }; })
            .sort(function (a, b) { return a.s - b.s || a.e - b.e; });
        var clusters = [], cur = [];
        items.forEach(function (it) {
            if (cur.length && it.s >= Math.max.apply(null, cur.map(function (c) { return c.e; }))) { clusters.push(cur); cur = []; }
            cur.push(it);
        });
        if (cur.length) clusters.push(cur);
        clusters.forEach(function (cl) {
            var lanes = [];
            cl.forEach(function (it) {
                var lane = 0; while (lanes[lane] && lanes[lane] > it.s) lane++;
                lanes[lane] = it.e; it.lane = lane;
            });
            cl.forEach(function (it) { it.left = it.lane / lanes.length; it.width = 1 / lanes.length; });
        });
        return items;
    }

    function renderList(d) {
        var box = el('div', { class: 'list' }, [el('h3', { text: 'Ближайшее' })]);
        var now = ownerNow();
        var upcoming = d.events.filter(function (e) { return parseISO(e.end) >= now; }).slice(0, 8);
        if (!upcoming.length) box.appendChild(el('div', { class: 'status', text: 'Дальше сегодня ничего нет' }));
        upcoming.forEach(function (e) {
            box.appendChild(el('div', { class: 'row', onclick: function () { openCard(e); } }, [
                el('span', { class: 'when', text: e.all_day ? 'весь день' : fmtHM(parseISO(e.start)) + '–' + fmtHM(parseISO(e.end)) }),
                el('span', { text: e.title + (e.calendar_name ? ' · ' + e.calendar_name : '') + linkNote(e) })
            ]));
        });
        return box;
    }

    function renderStatus(d) {
        var parts = [];
        d.accounts.forEach(function (a) { if (a.provider !== 'local') parts.push(a.alias + ': ' + (a.status === 'ok' ? 'ок' : a.status)); });
        if (d.sync.pending) parts.push('ожидает синхронизации: ' + d.sync.pending);
        if (d.sync.conflict) parts.push('конфликтов: ' + d.sync.conflict);
        if (d.sync.failed) parts.push('не отправлено: ' + d.sync.failed);
        parts.push('фон: ' + (d.sync.companion || 'неизвестно'));
        return el('div', { class: 'status', text: parts.join(' · ') + ' · ' + d.timezone });
    }

    /* ---------- drag / resize ---------- */
    function attachDrag(node, e, col) {
        var coarse = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
        if (!window.PointerEvent || coarse) { node.addEventListener('click', function () { openCard(e); }); return; }   // touch: fields, not drag (24 A)
        node.addEventListener('pointerdown', function (ev) {
            if (ev.button !== 0) return;
            var resizing = ev.target.classList.contains('rs');
            var startY = ev.clientY, origTop = parseFloat(node.style.top), origH = parseFloat(node.style.height), moved = false;
            node.setPointerCapture(ev.pointerId);
            function onMove(m) {
                var dy = m.clientY - startY; if (Math.abs(dy) > 3) moved = true;
                if (resizing) node.style.height = Math.max(HOUR_PX / 4, origH + dy) + 'px';
                else node.style.top = Math.max(0, origTop + dy) + 'px';
            }
            function onUp(u) {
                node.removeEventListener('pointermove', onMove); node.removeEventListener('pointerup', onUp);
                if (!moved) { openCard(e); return; }
                var s = parseISO(e.start), en = parseISO(e.end);
                var dur = (en - s) / 60000;
                if (resizing) {
                    var newDur = Math.max(SNAP_MIN, Math.round((parseFloat(node.style.height) + 2) / HOUR_PX * 60 / SNAP_MIN) * SNAP_MIN);
                    var ne = new Date(s.getTime() + newDur * 60000);
                    save(e, { start: fmtLocalInput(s), end: fmtLocalInput(ne) });
                } else {
                    var mins = Math.round(parseFloat(node.style.top) / HOUR_PX * 60 / SNAP_MIN) * SNAP_MIN;
                    var ns = new Date(col.getAttribute('data-date') + 'T00:00:00'); ns.setMinutes(mins);
                    var ne2 = new Date(ns.getTime() + dur * 60000);
                    save(e, { start: fmtLocalInput(ns), end: fmtLocalInput(ne2) });
                }
            }
            node.addEventListener('pointermove', onMove); node.addEventListener('pointerup', onUp);
            ev.preventDefault();
        });
    }

    function save(e, changes) {
        /* The sandbox has no dialogs (window.confirm silently returns false), so a drag on a series moves only this
         * occurrence; the whole schedule is changed from the card («Охват изменения»). */
        var series = !!(e.rrule || e.series_id);
        if (series) changes.scope = 'this';
        changes.id = e.id;
        return post('event/update', changes).then(function (r) {
            state.notice = series ? 'Перенесено только это вхождение; всё расписание меняется в карточке события.' : '';
            noteAssignments(r);
            return load();
        }).catch(function (err) { state.error = err.message; render(); });
    }

    function noteAssignments(r) {
        var pend = (r && r.assignments || []).filter(function (a) { return a.status !== 'done'; });
        state.error = pend.length ? (r.status === 'failed' || r.status === 'error' ? 'Не сохранено; ' : 'Сохранено локально; ') + pend.map(function (a) { return (a.calendar_name || a.calendar_id) + ': ' + (a.message || a.status); }).join('; ') : '';
        if (r && r.warning) state.error = (state.error ? state.error + '; ' : '') + r.warning;
        return state.error;
    }

    /* ---------- card ---------- */
    function ownerNow() { return state.data && state.data.now ? parseISO(state.data.now) : new Date(); }
    function defaultStart() { var d = ownerNow(); d.setMinutes(Math.ceil(d.getMinutes() / 30) * 30, 0, 0); if (state.date && state.date !== localDate(d)) { d = new Date(state.date + 'T10:00:00'); } return d; }

    function openCard(ev, start) {
        var s = ev ? parseISO(ev.start) : start;
        var e = ev ? parseISO(ev.end) : new Date(s.getTime() + 60 * 60000);
        var defaultCal = (state.data && (state.data.calendars.filter(function (c) { return c.default; })[0] || state.data.calendars[0]) || {}).id;
        state.editing = {
            id: ev ? ev.id : null, title: ev ? ev.title : '', start: fmtLocalInput(s), end: fmtLocalInput(e), all_day: ev ? !!ev.all_day : false,
            calendars: ev ? [ev.calendar_id] : (defaultCal ? [defaultCal] : []), primary_calendar: ev ? ev.calendar_id : defaultCal,
            hidden: ev ? ev.visibility === 'hidden' : false, availability: ev ? ev.availability : 'busy', location: ev ? (ev.location || '') : '',
            description: '', reminders: [], attendees: [], rrule: ev ? (ev.rrule || '') : '', series: ev ? !!(ev.rrule || ev.series_id) : false,
            orig: ev, loading: !!ev, armed: false,
            grouped: !!(ev && ev.link_group_id), linkState: ev ? ev.link_state || '' : '', link: null, linkUi: {},
            readonly: !!ev && ev.link_state === 'missing'   // a copy the provider removed is kept for the record, not editable
        };
        render();
        if (ev) {
            var token = state.editing;
            api('event/get?id=' + encodeURIComponent(ev.id)).then(function (r) {
                if (state.editing !== token) return;
                var full = r.event || {};
                if (full.conflict && full.conflict.status !== 'not_conflicted') {
                    token.loadError = 'Конфликт с провайдером. В чате попроси показать обе версии события '
                        + (full.conflict.event_id || full.id || ev.id) + ' и выбрать, какую оставить.';
                }
                token.description = full.description || '';
                token.reminders = full.reminders || [];
                token.attendees = (full.attendees || []).map(function (a) { return a.email || String(a); });
                token.location = full.location || token.location;
                token.link = full.link || null;
                token.linkState = full.link_state || token.linkState;
                token.readonly = token.linkState === 'missing';
                var members = (full.assignments || []).map(function (a) { return a.calendar_id; });
                if (members.length) token.calendars = members;
                token.loading = false;
                render();
            }).catch(function (err) { if (state.editing === token) { token.loading = false; token.loadError = err.message; render(); } });
        }
    }

    function renderCard() {
        var f = state.editing;
        var form = el('form');
        function field(label, input) { var l = el('label', { text: label }); return el('div', {}, [l, input]); }
        var title = el('input', { value: f.title, placeholder: 'Название', required: 'required' });
        var start = el('input', { type: 'datetime-local', value: f.start });
        var end = el('input', { type: 'datetime-local', value: f.end });
        var allDay = el('input', { type: 'checkbox' }); allDay.checked = f.all_day;
        /* calendars: checkboxes; the first checked one (default calendar first) gets the full content, the rest get copies per their publish rule */
        var calBox = el('div', { class: 'cals' });
        var calInputs = [];
        var writable = (state.data ? state.data.calendars : []).filter(function (c) { return c.writable; })
            .sort(function (a, b) { return (a.id === f.primary_calendar ? -1 : b.id === f.primary_calendar ? 1 : 0) || (b.default ? 1 : 0) - (a.default ? 1 : 0); });
        writable.forEach(function (c) {
            var cb = el('input', { type: 'checkbox', value: c.id }); cb.checked = f.calendars.indexOf(c.id) >= 0;
            calInputs.push(cb);
            calBox.appendChild(el('label', { style: 'display:inline-flex;align-items:center;gap:4px;margin:0 8px 4px 0;font-size:13px;color:var(--fg)' }, [cb, c.name + ' (' + c.provider + ')']));
        });
        function chosenCals() { return calInputs.filter(function (cb) { return cb.checked; }).map(function (cb) { return cb.value; }); }
        var hidden = el('input', { type: 'checkbox' }); hidden.checked = f.hidden;
        var avail = el('select'); [['busy', 'занято'], ['free', 'не занимает время'], ['soft', 'обычно (предпочтение)']].forEach(function (p) { var o = el('option', { value: p[0], text: p[1] }); if (p[0] === f.availability) o.selected = true; avail.appendChild(o); });
        var loc = el('input', { value: f.location, placeholder: 'Место или ссылка' });
        var desc = el('textarea', { rows: '2', placeholder: 'Описание' }); desc.value = f.description || '';
        var rem = el('input', { value: (f.reminders || []).join(', '), placeholder: 'Напомнить за N минут (например 15, 60)' });
        var remEdited = false; rem.addEventListener('input', function () { remEdited = true; });
        var att = el('input', { value: (f.attendees || []).join(', '), placeholder: 'Участники: email через запятую' });
        var attEdited = false; att.addEventListener('input', function () { attEdited = true; });
        var notify = el('input', { type: 'checkbox' });
        var rrule = el('input', { value: f.rrule, placeholder: 'Повторение RRULE, например FREQ=WEEKLY;BYDAY=MO,WE' });
        var rruleEdited = false; rrule.addEventListener('input', function () { rruleEdited = true; });
        var scopeSel = el('select'); [['this', 'только эта дата'], ['following', 'начиная с этой даты'], ['all', 'всё расписание']].forEach(function (p) { scopeSel.appendChild(el('option', { value: p[0], text: p[1] })); });
        scopeSel.value = f.scope || 'this';
        scopeSel.addEventListener('change', function () { f.scope = scopeSel.value; f.armed = false; });
        var status = el('div', { class: 'status', text: f.loading ? 'Загружаю событие…' : (f.loadError ? 'Не удалось загрузить детали: ' + f.loadError
            : f.readonly ? 'Эта копия удалена у провайдера и не редактируется; доступны только действия со связью.' : '') });
        if (f.loadError) status.className = 'status err';
        var actions = el('div', { class: 'actions' });
        if (f.id) {
            /* two-step delete: the sandbox has no confirm dialog, so the button arms itself first */
            var del = el('button', { type: 'button', class: 'danger', text: f.armed ? 'Точно удалить' + (f.series ? ' (' + scopeSel.options[scopeSel.selectedIndex].text + ')' : '') + '?' : 'Удалить' });
            del.addEventListener('click', function () {
                if (!f.armed) {
                    f.armed = true;
                    del.textContent = 'Точно удалить' + (f.series ? ' (' + scopeSel.options[scopeSel.selectedIndex].text + ')' : '') + '?';
                    setTimeout(function () { if (state.editing === f && f.armed) { f.armed = false; del.textContent = 'Удалить'; } }, 5000);
                    return;
                }
                del.disabled = true; status.textContent = 'Удаляю…'; status.className = 'status';
                post('event/delete', { id: f.id, scope: f.series ? scopeSel.value : 'this', send_updates: notify.checked })
                    .then(function (r) { state.editing = null; noteAssignments(r); return load(); })
                    .catch(function (e) { del.disabled = false; f.armed = false; status.textContent = e.message; status.className = 'status err'; });
            });
            actions.appendChild(del);
        }
        actions.appendChild(el('span', { class: 'spacer' }));
        actions.appendChild(el('button', { type: 'button', text: f.readonly ? 'Закрыть' : 'Отмена', onclick: function () { state.editing = null; render(); } }));
        var saveButton = el('button', { type: 'button', class: 'primary', text: f.id ? 'Сохранить' : 'Создать' });
        actions.appendChild(saveButton);
        // Module frames deliberately do not allow native form submission.
        form.addEventListener('submit', function (ev) { ev.preventDefault(); });
        form.addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter' && !ev.isComposing && ev.target.tagName === 'INPUT' && ev.target.type !== 'checkbox') {
                ev.preventDefault(); saveCard();
            }
        });
        saveButton.addEventListener('click', saveCard);
        function saveCard() {
            if (saveButton.disabled || !form.reportValidity()) return;
            if (f.loading) { status.textContent = 'Подожди: детали события ещё загружаются'; return; }
            var cals = chosenCals();
            if (!cals.length) { status.textContent = 'Выбери хотя бы один календарь'; status.className = 'status err'; return; }
            var body = { title: title.value.trim(), start: allDay.checked ? start.value.slice(0, 10) : start.value.slice(0, 16), end: allDay.checked ? end.value.slice(0, 10) : end.value.slice(0, 16),
                all_day: allDay.checked, hidden: hidden.checked, availability: avail.value,
                location: loc.value, description: desc.value, send_updates: notify.checked };
            if (!f.id || rruleEdited) body.rrule = rrule.value.trim();
            var remList = rem.value.split(',').map(function (x) { return x.trim(); }).filter(Boolean).map(Number).filter(function (n) { return !isNaN(n); });
            var attList = att.value.split(',').map(function (x) { return x.trim(); }).filter(Boolean);
            var p;
            if (f.id) {
                body.id = f.id; body.scope = f.series ? scopeSel.value : 'this';
                /* A card is a snapshot of one occurrence. Unedited times must never
                 * become a time-change command against its master series. */
                if (body.title === f.title) delete body.title;
                if (body.start === f.start && body.end === f.end) {
                    delete body.start; delete body.end;
                }
                if (body.all_day === f.all_day) delete body.all_day;
                if (body.hidden === f.hidden) delete body.hidden;
                if (body.availability === f.availability) delete body.availability;
                if (body.location === f.location) delete body.location;
                if (body.description === f.description) delete body.description;
                if (remEdited) body.reminders = remList;
                if (attEdited) body.attendees = attList;
                var same = cals.length === f.calendars.length && cals.every(function (c) { return f.calendars.indexOf(c) >= 0; });
                if (!same) body.calendars = cals;
                p = post('event/update', body);
            } else {
                body.calendars = cals; body.reminders = remList; body.attendees = attList;
                p = post('event', body);
            }
            saveButton.disabled = true;
            status.textContent = 'Сохраняю…'; status.className = 'status';
            p.then(function (r) {
                var outcome = noteAssignments(r);
                if (r.status === 'failed' || r.status === 'error') {
                    saveButton.disabled = false; status.textContent = outcome || r.message || 'Изменение не применено';
                    status.className = 'status err'; return;
                }
                state.editing = null; state.notice = outcome;
                return load();
            }).catch(function (e) { saveButton.disabled = false; status.textContent = e.message; status.className = 'status err'; });
        }
        if (f.readonly) {
            [title, start, end, allDay, hidden, avail, loc, desc, rem, att, notify, rrule, scopeSel].concat(calInputs).forEach(function (x) { x.disabled = true; });
            saveButton.hidden = true;
            if (del) del.hidden = true;
        }
        form.appendChild(el('div', { class: 'title', text: f.id ? 'Событие' : 'Новое событие', style: 'font-weight:600;font-size:15px' }));
        form.appendChild(field('Название', title));
        form.appendChild(field('Начало', start));
        form.appendChild(field('Конец', end));
        form.appendChild(el('label', { class: 'inline' }, [allDay, ' весь день']));
        form.appendChild(field('Календари (первый — основной, остальные получают копии по своему правилу)', calBox));
        var linkBox = renderLinkBox(f);
        if (linkBox) form.appendChild(linkBox);
        form.appendChild(el('label', { class: 'inline' }, [hidden, ' служебное (распорядок, видно по переключателю)']));
        form.appendChild(field('Занятость', avail));
        form.appendChild(field('Место', loc));
        form.appendChild(field('Описание', desc));
        form.appendChild(field('Участники', att));
        form.appendChild(el('label', { class: 'inline' }, [notify, ' уведомить участников (приглашения / изменения)']));
        form.appendChild(field('Напоминания Уробороса', rem));
        form.appendChild(field('Повторение', rrule));
        if (f.series) form.appendChild(field('Охват изменения', scopeSel));
        form.appendChild(status);
        form.appendChild(actions);
        var overlay = el('div', { class: 'card', onclick: function (ev) { if (ev.target === overlay) { state.editing = null; render(); } } }, [form]);
        setTimeout(function () { title.focus(); }, 0);
        return overlay;
    }

    function shift(n) { state.date = addDays(state.date || localDate(ownerNow()), state.view === 'week' ? 7 * n : n); state.focusNow = true; load(); }

    function changeScale(direction) {
        var next = Math.max(0, Math.min(HOUR_SCALES.length - 1, state.hourScale + direction));
        if (next === state.hourScale) return;
        var scroller = root.querySelector('.grid-scroll');
        var oldHour = HOUR_PX;
        var currentScroll = scroller ? scroller.scrollTop : state.gridScrollTop;
        state.hourScale = next;
        HOUR_PX = HOUR_SCALES[next];
        state.gridScrollTop = currentScroll * HOUR_PX / oldHour;
        state.zooming = true;
        state.focusNow = !!(scroller && scroller.querySelector('.now'));
        var hadKeyboardFocus = document.activeElement && document.activeElement.closest('.scale');
        render();
        if (hadKeyboardFocus) {
            var buttons = root.querySelectorAll('.scale button');
            var chosen = buttons[direction < 0 ? 0 : 1];
            (chosen.disabled ? buttons[direction < 0 ? 1 : 0] : chosen).focus({ preventScroll: true });
        }
    }

    /* ---------- settings panel: reminder rule (19 A) + sync now ---------- */
    function togglePanel() {
        if (state.panel) { state.panel = null; render(); return; }
        state.panel = { loading: true };
        render();
        api('reminders').then(function (r) { state.panel = { data: r, msg: '' }; render(); })
            .catch(function (e) { state.panel = { data: null, msg: e.message }; render(); });
    }

    function renderPanel() {
        var p = state.panel, box = el('div', { class: 'status', style: 'border:1px solid var(--line);border-radius:10px;padding:10px;margin-bottom:10px;color:var(--fg)' });
        if (p.loading) { box.appendChild(el('span', { text: 'Загружаю…' })); return box; }
        var rules = (p.data && p.data.rules) || {}, ch = (p.data && p.data.channel) || {};
        var def = el('input', { value: (rules.default || []).join(', '), placeholder: 'напоминать за N минут, через запятую (пусто — не напоминать)',
            style: 'width:min(320px,100%);padding:4px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit' });
        var msg = el('span', { style: 'margin-left:8px', text: p.msg || '' });
        var saveBtn = el('button', { text: 'Сохранить правило', onclick: function () {
            var mins = def.value.split(',').map(function (x) { return x.trim(); }).filter(Boolean).map(Number).filter(function (n) { return !isNaN(n); });
            post('reminders/save', { action: 'set_default', offsets: mins }).then(function (r) { msg.textContent = (r.status === 'ok' || r.status === 'updated') ? 'сохранено' : (r.message || r.status); })
                .catch(function (e) { msg.textContent = e.message; });
        } });
        var syncBtn = el('button', { text: 'Синхронизировать сейчас', onclick: function () {
            post('sync', {}).then(function (r) { msg.textContent = r.message || 'запрошено'; }).catch(function (e) { msg.textContent = e.message; });
        } });
        box.appendChild(el('div', { style: 'font-weight:600;margin-bottom:6px', text: 'Напоминания Уробороса' }));
        box.appendChild(el('div', {}, ['Правило по умолчанию: ', def]));
        box.appendChild(el('div', { style: 'margin-top:6px', text: 'Канал доставки: ' + (ch.state === 'ready' ? 'готов' : ch.state === 'no_route' ? 'ждёт маршрут ядра (напоминания хранятся)' : (ch.state || 'неизвестно')) }));
        var upcoming = (p.data && p.data.upcoming) || [];
        if (upcoming.length) box.appendChild(el('div', { style: 'margin-top:6px', text: 'Ближайшие: ' + upcoming.slice(0, 5).map(function (u) { return u.fire_at.slice(11, 16) + ' ' + u.title + (u.state !== 'scheduled' ? ' (' + u.state + ')' : ''); }).join(' · ') }));
        box.appendChild(el('div', { class: 'actions', style: 'margin-top:8px' }, [saveBtn, syncBtn, msg]));
        return box;
    }

    /* ---------- accounts: what is connected and ready, Google sign-in, CalDAV/Яндекс form, disconnect ---------- */
    /* render() rebuilds the whole calendar, so this section is one node that is re-attached, never recreated: a
     * half-typed form and its password survive every refresh, and the 30 s poll waits while the form has focus.
     * The password is never read back, rendered or kept in state; its field is emptied as the request leaves.
     * Messages of this section live here, apart from state.error, so a later successful list read or agenda
     * refresh cannot wipe a failure the owner has not read yet. */
    var ACC_POLL_MS = 3000;          // while a Google sign-in is pending
    var ACC_WAIT_MS = 5 * 60000;     // then stop asking by ourselves; «Проверить» still works
    var ACC_OK = { ok: true, connected: true };
    var ACC_STATUS = { ok: 'готов', connected: 'готов', connecting: 'подключается…', auth_failed: 'нужно войти заново', error: 'ошибка' };
    var ACC_PROVIDER = { google: 'Google', yandex: 'Яндекс', caldav: 'CalDAV' };
    var ACC_CAPS = [['create', 'создавать календари'], ['update', 'менять календари'], ['delete', 'удалять календари'], ['sharing', 'делиться доступом']];
    var acc = { open: false, node: null, refs: null, data: null, loading: false, listError: '', seq: 0, msgs: {}, google: null,
        base: '', disc: null, busy: false, timer: 0, checking: false };

    function ago(iso) {
        var t = Date.parse(iso);
        if (isNaN(t)) return '';
        var min = Math.max(0, Math.round((Date.now() - t) / 60000));
        return min < 1 ? 'только что' : min < 60 ? min + ' мин назад' : min < 1440 ? Math.round(min / 60) + ' ч назад' : Math.round(min / 1440) + ' дн. назад';
    }
    function scrub(text, secret) { text = String(text == null ? '' : text); return secret ? text.split(secret).join('•••') : text; }
    function hostOf(url) { try { return new URL(url).host; } catch (e) { return ''; } }
    function accAttention(d) {
        return (d.accounts || []).some(function (a) { return a.provider !== 'local' && !ACC_OK[a.status] && a.status !== 'connecting'; });
    }
    function accTyping() { return !!(acc.node && acc.node.contains(document.activeElement)); }
    function accKeepFocus() {
        var node = document.activeElement;
        /* only when this frame really has the focus: restoring it from the background would take it from the chat composer */
        if (!acc.node || !node || !acc.node.contains(node) || !document.hasFocus()) return null;
        var kept = { node: node, start: null, end: null };
        try { kept.start = node.selectionStart; kept.end = node.selectionEnd; } catch (e) { /* not a text field */ }
        return kept;
    }
    function accRestoreFocus(kept) {
        if (!kept || !kept.node.isConnected) return;
        kept.node.focus({ preventScroll: true });
        try { if (kept.start != null) kept.node.setSelectionRange(kept.start, kept.end); } catch (e) { /* not a text field */ }
    }
    function accNode() { if (!acc.node) { accBuild(); accPaint(); } return acc.node; }
    function accToggle() {
        if (acc.open) { accStop(); acc.open = false; acc.node = null; acc.refs = null; render(); return; }
        acc.open = true;
        render();
        accRefresh();
    }
    function accStop() {
        clearTimeout(acc.timer);
        acc.google = null; acc.disc = null; acc.msgs = {}; acc.seq++; acc.loading = false; acc.checking = false;
    }
    function accMsg(area, kind, text) {
        if (text) acc.msgs[area] = { kind: kind, text: text }; else delete acc.msgs[area];
        accPaint();
    }

    function accRefresh() {
        if (state.disposed) return Promise.resolve();
        var seq = ++acc.seq;
        acc.loading = true;
        accPaint();
        return api('status', { timeoutMs: 20000 }).then(function (d) {
            if (seq === acc.seq) { acc.data = d; acc.listError = ''; }
        }, function (e) {
            if (seq === acc.seq) acc.listError = e.message || String(e);
        }).then(function () {
            if (seq === acc.seq) { acc.loading = false; accPaint(); }
        });
    }

    function accBuild() {
        var r = {};
        function field(id, label, input, hint) {
            input.id = 'acc-' + id;
            return el('div', { class: 'f' }, [el('label', { for: 'acc-' + id, text: label }), input, hint ? el('div', { class: 'hint', text: hint }) : null]);
        }
        r.listMsg = el('div', { class: 'msgs' }); r.googleMsg = el('div', { class: 'msgs' }); r.caldavMsg = el('div', { class: 'msgs' });
        r.list = el('div', { class: 'acc-list' });
        r.wait = el('div', { class: 'acc-wait' });
        r.refresh = el('button', { type: 'button', text: 'Обновить', onclick: function () { accRefresh(); } });
        r.base = el('input', { type: 'url', inputmode: 'url', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', placeholder: 'https://host.example.org/prefix' });
        r.base.value = acc.base;
        r.base.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' && !ev.isComposing) { ev.preventDefault(); accGoogleStart(); } });
        r.googleBtn = el('button', { type: 'button', class: 'primary', text: 'Подключить Google', onclick: accGoogleStart });
        r.googleNote = el('div', { class: 'acc-sub err', hidden: 'hidden', text: 'Клиент Google не настроен: добавь GOOGLE_OAUTH_CLIENT_ID и GOOGLE_OAUTH_CLIENT_SECRET в Settings → Secrets.' });
        r.baseNote = el('div', { class: 'hint' });
        r.caldav = el('input', { type: 'radio', name: 'acc-preset', value: 'caldav' });
        r.yandex = el('input', { type: 'radio', name: 'acc-preset', value: 'yandex' });
        r.caldav.checked = true;
        r.alias = el('input', { type: 'text', autocomplete: 'off', placeholder: 'Например, Рабочий' });
        r.url = el('input', { type: 'url', inputmode: 'url', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', placeholder: 'https://caldav.example.org/dav/' });
        r.login = el('input', { type: 'text', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false' });
        r.pass = el('input', { type: 'password', autocomplete: 'new-password', autocapitalize: 'off', spellcheck: 'false' });
        r.submit = el('button', { type: 'button', class: 'primary', text: 'Подключить', onclick: accSubmit });
        var segCaldav = el('label', {}, [r.caldav, ' Другой CalDAV']), segYandex = el('label', {}, [r.yandex, ' Яндекс']);
        var urlRow = field('url', 'Адрес сервера CalDAV', r.url);
        var passRow = field('pass', 'Пароль', r.pass, 'Пароль не показывается и хранится в зашифрованном хранилище навыка.');
        function preset() {
            var y = r.yandex.checked;
            urlRow.hidden = y;
            segCaldav.className = y ? '' : 'on'; segYandex.className = y ? 'on' : '';
            passRow.firstChild.textContent = y ? 'Пароль приложения Яндекса' : 'Пароль (лучше пароль приложения)';
        }
        r.caldav.addEventListener('change', preset); r.yandex.addEventListener('change', preset);
        preset();
        var form = el('form', { class: 'acc-form', novalidate: 'novalidate', 'aria-label': 'Подключить CalDAV или Яндекс' }, [
            el('div', { class: 'f' }, [el('div', { class: 'seg', role: 'radiogroup', 'aria-label': 'Тип подключения' }, [segCaldav, segYandex])]),
            field('alias', 'Название (необязательно)', r.alias), urlRow, field('login', 'Логин', r.login), passRow,
            el('div', { class: 'acc-actions' }, [r.submit]), r.caldavMsg
        ]);
        // The frame forbids native form submission, so Enter is handled here (as in the event card).
        form.addEventListener('submit', function (ev) { ev.preventDefault(); });
        form.addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter' && !ev.isComposing && ev.target.tagName === 'INPUT' && ev.target.type !== 'radio') { ev.preventDefault(); accSubmit(); }
        });
        acc.refs = r;
        acc.node = el('section', { class: 'acc', 'aria-label': 'Аккаунты' }, [
            el('div', { class: 'acc-head' }, [el('h3', { text: 'Аккаунты' }), r.refresh, el('button', { type: 'button', text: 'Закрыть', onclick: accToggle })]),
            r.listMsg, r.list,
            el('h4', { text: 'Добавить Google' }),
            el('details', {}, [el('summary', { text: 'Адрес установки — только для удалённого доступа' }),
                field('base', 'Адрес установки', r.base, 'Оставь пустым, если Уроборос открыт на этом компьютере. Для удалённой установки укажи адрес вместе с префиксом пути.'), r.baseNote]),
            el('div', { class: 'acc-actions' }, [r.googleBtn]), r.googleNote, r.googleMsg, r.wait,
            el('h4', { text: 'Добавить CalDAV или Яндекс' }), form
        ]);
    }

    function accCaps(caps) {
        if (!caps || typeof caps !== 'object') return '';
        var parts = [];
        ACC_CAPS.forEach(function (c) {
            if (c[0] in caps) parts.push(c[1] + ': ' + (caps[c[0]] === true ? 'да' : caps[c[0]] === false ? 'нет' : 'не проверено'));
        });
        return parts.join(' · ');
    }

    function accRow(a, cals) {
        /* Ready needs a usable status and an adapter: «ok» with no stored credentials still cannot sync. */
        var broken = !!a.adapter_error, ready = !!ACC_OK[a.status] && !broken, tone = ready ? 'ok' : a.status === 'connecting' && !broken ? 'warn' : 'err';
        var title = a.alias || a.login || a.id;
        var ident = [a.login !== title ? a.login : '', a.provider === 'caldav' ? hostOf(a.server_url) : ''].filter(Boolean).join(' · ');
        var mine = cals.filter(function (c) { return c.account_id === a.id; });
        var meta = [a.last_sync ? 'синхронизация ' + ago(a.last_sync) : 'синхронизации ещё не было',
            'календарей: ' + mine.length + (mine.length ? ', для записи: ' + mine.filter(function (c) { return c.writable; }).length : '')];
        var problem = broken ? a.adapter_error : ready ? '' : (a.last_error || '');   // a stale error of a ready account is not shown
        var caps = accCaps(a.capabilities);
        var armed = acc.disc && acc.disc.id === a.id;
        var row = el('div', { class: 'acc-row' + (tone === 'err' ? ' bad' : ''), 'data-id': a.id }, [
            el('div', { class: 'acc-main' }, [
                el('div', { class: 'acc-name' }, [el('span', { text: title }), el('span', { class: 'tag', text: ACC_PROVIDER[a.provider] || a.provider })]),
                ident ? el('div', { class: 'acc-sub', text: ident }) : null,
                el('div', { class: 'acc-state' }, [el('span', { class: 'dot ' + tone }),
                    el('span', { text: broken && ACC_OK[a.status] ? 'не работает' : ACC_STATUS[a.status] || String(a.status || 'неизвестно') })]),
                problem ? el('div', { class: 'acc-sub err', text: problem }) : null,
                el('div', { class: 'acc-sub', text: meta.join(' · ') }),
                caps ? el('div', { class: 'acc-sub caps', title: (a.capabilities && a.capabilities.note) || '', text: 'Возможности: ' + caps }) : null
            ]),
            el('div', { class: 'acc-actions' }, [el('button', { type: 'button', class: 'danger', text: 'Отключить', 'aria-label': 'Отключить ' + title, disabled: acc.disc ? 'disabled' : null,
                onclick: function () {
                    acc.disc = { id: a.id }; accPaint();
                    var go = acc.node.querySelector('.acc-confirm .danger'); if (go) go.focus();
                } })])
        ]);
        if (armed) {
            row.appendChild(el('div', { class: 'acc-confirm' }, [
                el('p', { text: 'Отключить «' + title + '»? Синхронизация остановится. Локальные события не удаляются; ещё не отправленные изменения для этого аккаунта отменяются.'
                    + (a.provider === 'google' ? ' Доступ приложения в самом аккаунте Google здесь не отзывается.' : '') }),
                el('div', { class: 'acc-actions' }, [
                    el('button', { type: 'button', class: 'danger', text: acc.disc.busy ? 'Отключаю…' : 'Отключить', disabled: acc.disc.busy ? 'disabled' : null, onclick: function () { accDisconnect(a); } }),
                    el('button', { type: 'button', text: 'Отмена', disabled: acc.disc.busy ? 'disabled' : null, onclick: function () { acc.disc = null; accPaint(); } })
                ])
            ]));
        }
        return row;
    }

    function accWaitView(g) {
        var expired = g.phase === 'expired';
        /* the host answers «ok» even when a popup blocker swallowed the tab, so «opened» is only a request */
        var note = { opening: 'Открываю вход в браузере…', opened: 'Вход в Google запрошен у браузера. Если вкладка не появилась, откройте ссылку ниже.',
            copied: 'Браузер открыть не удалось — ссылка для входа скопирована. Откройте её вручную.',
            failed: 'Не удалось открыть вход автоматически' + (g.openError ? ' (' + g.openError + ')' : '') + '. Откройте ссылку ниже.' }[g.opened] || '';
        var link = el('a', { href: g.url, target: '_blank', rel: 'noopener', text: 'Открыть вход в Google' });
        link.addEventListener('click', function (ev) { ev.preventDefault(); accOpenAuth(g); });   // inside the click, so the host sees the gesture
        var copy = el('button', { type: 'button', text: 'Скопировать ссылку' });
        copy.addEventListener('click', function () {
            var done = function (t) { copy.textContent = t; };
            if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(g.url).then(function () { done('Ссылка скопирована'); }, function () { done('Не удалось скопировать'); });
            else done('Не удалось скопировать');
        });
        return el('div', { class: 'wait', role: 'status' }, [
            el('div', { text: expired ? 'Вход не подтвердился за 5 минут. Если вы уже вошли, нажмите «Проверить»; иначе начните заново.'
                : 'Ждём завершения входа. Аккаунт появится в списке, только когда Уроборос получит доступ.' }),
            note ? el('div', { class: 'acc-sub', text: note }) : null,
            g.hint ? el('div', { class: 'acc-sub', text: g.hint }) : null,
            g.redirect ? el('div', { class: 'acc-sub', text: 'Адрес возврата: ' + g.redirect }) : null,
            el('div', { class: 'acc-actions' }, [link, copy, el('button', { type: 'button', text: 'Проверить', onclick: function () { accCheck(); } }),
                el('button', { type: 'button', text: expired ? 'Начать заново' : 'Отмена', onclick: function () {
                    clearTimeout(acc.timer); acc.google = null;
                    if (expired) accGoogleStart(); else accMsg('google', 'info', 'Ожидание остановлено. Если вход в Google уже подтверждён, аккаунт всё равно появится в списке.');
                    accPaint();
                } })])
        ]);
    }

    function accPaint() {
        var r = acc.refs;
        if (!acc.node || !r) return;
        [['list', r.listMsg], ['google', r.googleMsg], ['caldav', r.caldavMsg]].forEach(function (pair) {
            var m = acc.msgs[pair[0]], sig = m ? m.kind + '|' + m.text : '';
            if (pair[1].sig === sig) return;
            pair[1].sig = sig; pair[1].textContent = '';
            if (m) pair[1].appendChild(el('div', { class: 'msg ' + m.kind, role: m.kind === 'err' ? 'alert' : 'status' }, [el('span', { text: m.text }),
                el('button', { type: 'button', text: '×', title: 'Скрыть сообщение', 'aria-label': 'Скрыть сообщение', onclick: function () { accMsg(pair[0], '', ''); } })]));
        });
        var d = acc.data, rows = d ? (d.accounts || []).filter(function (a) { return a.provider !== 'local'; }) : [], cals = d ? d.calendars || [] : [];
        var sig = JSON.stringify([rows, cals.map(function (c) { return [c.account_id, c.writable]; }), rows.map(function (a) { return ago(a.last_sync); }),
            acc.listError, acc.loading && !d, acc.disc]);
        if (r.list.sig !== sig) {
            r.list.sig = sig; r.list.textContent = '';
            if (acc.listError) r.list.appendChild(el('div', { class: 'msg err', role: 'alert' }, [
                el('span', { text: 'Не удалось прочитать список аккаунтов: ' + acc.listError + (d ? ' Показаны прежние данные.' : '') }),
                el('button', { type: 'button', text: 'Повторить', onclick: function () { accRefresh(); } })]));
            if (!d && !acc.listError) r.list.appendChild(el('div', { class: 'status', text: 'Загружаю…' }));
            if (d && !rows.length) r.list.appendChild(el('div', { class: 'status', text: 'Внешних аккаунтов пока нет. События хранятся в календарях Уробороса.' }));
            rows.forEach(function (a) { r.list.appendChild(accRow(a, cals)); });
        }
        var g = acc.google, gsig = g ? [g.phase, g.opened, g.openError, g.url].join('|') : '';
        if (r.wait.sig !== gsig) {
            r.wait.sig = gsig; r.wait.textContent = '';
            if (g) r.wait.appendChild(g.phase === 'starting' ? el('div', { class: 'wait', role: 'status', text: 'Готовлю вход в Google…' }) : accWaitView(g));
        }
        r.googleBtn.disabled = !!(g && (g.phase === 'starting' || g.phase === 'awaiting'));
        r.googleNote.hidden = !(d && d.connection_options && d.connection_options.google_client_configured === false);   // unknown is not «no»
        var saved = d && d.settings && d.settings.public_base_url;
        r.baseNote.textContent = saved ? 'Сейчас сохранён адрес: ' + saved + '. Если поле пустое, используется он.' : '';
        r.submit.disabled = acc.busy; r.submit.textContent = acc.busy ? 'Проверяю…' : 'Подключить';
        r.refresh.disabled = acc.loading;
    }

    function accGoogleStates() {
        var out = {};
        ((acc.data && acc.data.accounts) || []).forEach(function (a) { if (a.provider === 'google') out[a.id] = a.status; });
        return out;
    }
    /* Connected means the account list says so: a new Google account, or one that became ready since the sign-in began.
     * That the link was opened, copied or clicked proves nothing. */
    function accGoogleArrived(run) {
        var list = (acc.data && acc.data.accounts) || [];
        for (var i = 0; i < list.length; i++) {
            var a = list[i];
            if (a.provider === 'google' && (!(a.id in run.known) || (run.known[a.id] !== a.status && ACC_OK[a.status]))) return a;
        }
        return null;
    }

    function accGoogleStart() {
        var r = acc.refs, g = acc.google;
        if (!r || (g && (g.phase === 'starting' || g.phase === 'awaiting'))) return;
        acc.base = r.base.value.trim();
        var run = { phase: 'starting' };
        acc.google = run;
        accMsg('google', '', '');
        (acc.data ? Promise.resolve() : accRefresh()).then(function () {
            if (acc.google !== run) return null;
            if (!acc.data) throw new Error('не удалось прочитать список аккаунтов' + (acc.listError ? ': ' + acc.listError : ''));
            run.known = accGoogleStates();   // what was already connected, so only a real change counts as arrival
            return post('oauth/begin', acc.base ? { public_base_url: acc.base } : {});
        }).then(function (res) {
            if (!res || acc.google !== run) return null;
            if (res.status !== 'ok' || !/^https:\/\//i.test(String(res.auth_url || ''))) throw new Error(res.message || 'Уроборос не вернул ссылку для входа');
            run.phase = 'awaiting'; run.since = Date.now(); run.url = res.auth_url; run.redirect = res.redirect_uri || ''; run.hint = res.message || '';
            accSchedule();
            return accOpenAuth(run);
        }).catch(function (e) {
            if (acc.google !== run) return;
            acc.google = null;
            accMsg('google', 'err', 'Вход в Google не начат: ' + e.message);
        });
        accPaint();
    }

    function accOpenAuth(run) {
        var wait = acc.node && acc.node.querySelector('.wait'), onLink = !!(wait && wait.contains(document.activeElement));
        run.opened = 'opening';
        accPaint();
        var open = window.OuroborosWidget && typeof OuroborosWidget.openExternal === 'function'
            ? OuroborosWidget.openExternal(run.url) : Promise.reject(new Error('эта версия Уробороса не открывает ссылки из виджета'));
        return open.then(function (res) { run.opened = res && !res.ok && res.degraded ? 'copied' : 'opened'; },
            function (e) { run.opened = 'failed'; run.openError = e.message; }).then(function () {
            if (acc.google !== run) return;
            accPaint();
            var link = onLink && acc.node.querySelector('.wait a');   // the panel was rebuilt under the keyboard focus
            if (link) link.focus();
        });
    }

    function accSchedule() {
        clearTimeout(acc.timer);
        var g = acc.google;
        if (!g || g.phase !== 'awaiting' || state.disposed) return;
        if (Date.now() - g.since > ACC_WAIT_MS) { g.phase = 'expired'; accPaint(); return; }
        acc.timer = setTimeout(accCheck, ACC_POLL_MS);
    }

    function accCheck() {
        var g = acc.google;
        if (!g || (g.phase !== 'awaiting' && g.phase !== 'expired') || acc.checking || state.disposed) return Promise.resolve();
        acc.checking = true;
        return accRefresh().then(function () {
            acc.checking = false;
            if (acc.google !== g) return;
            var a = acc.data && !acc.listError ? accGoogleArrived(g) : null;
            if (!a) { if (g.phase === 'awaiting') accSchedule(); return; }
            var label = a.alias || a.login || a.id, ready = !!ACC_OK[a.status];
            clearTimeout(acc.timer);
            acc.google = null;
            accMsg('google', ready ? 'ok' : 'err', ready ? 'Google подключён: ' + label + '. Календари появятся после синхронизации.'
                : 'Аккаунт Google «' + label + '» добавлен, но не готов: ' + (ACC_STATUS[a.status] || a.status) + (a.last_error ? ' — ' + a.last_error : '') + '.');
            load();
        });
    }
    function accWake() { if (acc.google && acc.google.phase === 'awaiting' && document.visibilityState !== 'hidden') accCheck(); }

    function accSubmit() {
        var r = acc.refs;
        if (!r || acc.busy) return;
        var yandex = r.yandex.checked;
        var v = { alias: r.alias.value.trim(), url: r.url.value.trim(), login: r.login.value.trim(), password: r.pass.value };
        var bad = !yandex && !/^https?:\/\/[^\s\/?#]+/i.test(v.url) ? [r.url, 'Укажи адрес сервера CalDAV вида https://…']
            : !v.login ? [r.login, 'Укажи логин'] : !v.password ? [r.pass, 'Укажи пароль'] : null;
        if (bad) { accMsg('caldav', 'err', bad[1]); bad[0].focus(); return; }
        var body = { preset: yandex ? 'yandex' : 'caldav', alias: v.alias, login: v.login, password: v.password };
        if (!yandex) body.server_url = v.url;
        r.pass.value = '';   // the password now lives only in this request
        acc.busy = true;
        accMsg('caldav', 'info', 'Проверяю доступ к серверу…');
        post('accounts/connect', body).then(function (res) {
            if (res.status !== 'connected') return accMsg('caldav', 'err', scrub(res.message || 'Сервер не подтвердил подключение', v.password));
            r.alias.value = ''; r.url.value = ''; r.login.value = '';
            accMsg('caldav', 'ok', 'Подключено: ' + (res.label || v.login) + '. Календарей: ' + (res.calendars || []).length + '.');
        }, function (e) {
            accMsg('caldav', 'err', 'Не подключено: ' + scrub(e.message, v.password)
                + (e.body && e.body.saved ? ' Аккаунт сохранён с ошибкой: исправь данные и подключи снова либо отключи его в списке.' : ''));
        }).then(function () { acc.busy = false; accPaint(); accRefresh(); load(); });
    }

    function accDisconnect(a) {
        var d = acc.disc;
        if (!d || d.busy) return;
        d.busy = true;
        accPaint();
        post('accounts/disconnect', { account_id: a.id }).then(function (res) {   // answers 200 even for «error» and «not_found»
            if (res.status === 'updated') accMsg('list', 'ok', res.message || 'Аккаунт отключён.');
            else accMsg('list', 'err', 'Не отключено: ' + (res.message || res.status || 'неизвестный ответ'));
        }, function (e) {
            accMsg('list', 'err', 'Не отключено: ' + e.message);
        }).then(function () { acc.disc = null; accPaint(); accRefresh(); load(); });
    }

    /* ---------- links: how an event relates to its copies, and the explicit actions ---------- */
    /* One chosen source per link; the copies follow it. Nothing here resolves a difference by itself: a copy changed
     * on its own stays «изменена», a copy the provider removed stays listed as removed, and every action says what
     * it will change (the copy, the original or the group) before the owner applies it. */
    var LINK_WORD = { diverged: 'копия изменена', missing: 'копия удалена у провайдера' };
    function linkNote(e) { return e.link_state === 'diverged' ? ' · ' + LINK_WORD.diverged : ''; }

    function renderLinkIssues(d) {
        var items = d.link_issues || [];
        if (!items.length) { state.linkError = ''; return null; }
        var names = {};
        (d.calendars || []).forEach(function (c) { names[c.id] = c.name; });
        var box = el('div', { class: 'linkissues' }, [
            el('button', { type: 'button', class: 'linkhead', 'aria-expanded': state.linksOpen ? 'true' : 'false',
                text: (state.linksOpen ? '▾ ' : '▸ ') + 'Копии связанных событий требуют внимания: ' + items.length,
                onclick: function () { state.linksOpen = !state.linksOpen; render(); } })
        ]);
        if (state.linkError) box.appendChild(el('div', { class: 'status err', role: 'alert', text: state.linkError }));
        if (state.linksOpen) items.forEach(function (it) {
            box.appendChild(el('div', { class: 'issue' }, [
                el('div', { class: 'grow' }, [
                    el('div', { text: it.title || 'Без названия' }),
                    el('div', { class: 'acc-sub', text: [LINK_WORD[it.link_state] || String(it.link_state), names[it.calendar_id]].filter(Boolean).join(' · ') })
                ]),
                el('button', { type: 'button', text: 'Открыть', 'aria-label': 'Открыть: ' + (it.title || 'событие'), onclick: function () { openIssue(it); } })
            ]));
        });
        return box;
    }

    function openIssue(it) {
        state.linkError = '';
        api('event/get?id=' + encodeURIComponent(it.id)).then(function (r) {
            if (!state.disposed && r.event) openCard(r.event);
        }).catch(function (e) {
            if (state.disposed) return;
            state.linkError = 'Не удалось открыть «' + (it.title || it.id) + '»: ' + e.message;
            render();
        });
    }

    function linkModel(f) {
        var info = f.link;
        if (!info) return null;
        var self = String(f.id || '').split('@')[0], me = null;
        (info.members || []).forEach(function (x) { if (x.id === self) me = x; });
        var m = { info: info, self: self, series: !!f.series, isSource: !!info.source_id && info.source_id === self,
            missing: !!(me && me.missing) || f.linkState === 'missing', state: (me && me.state) || f.linkState || 'following' };
        function name(title, calendar) { return '«' + (title || 'Без названия') + '»' + (calendar ? ' (' + calendar + ')' : ''); }
        m.srcLabel = info.source ? name(info.source.title, info.source.calendar_name) : '';
        m.selfLabel = name(f.title, (me && me.calendar_name) || (f.orig && f.orig.calendar_name));
        m.fact = m.isSource ? 'Это событие — источник связи. Его копии в других календарях следуют за ним, пока их не изменили отдельно.'
            : info.source_ambiguous ? 'Источник связи не выбран, поэтому копиям не за чем следовать. Выбери источник на этом событии или на другой копии.'
            : info.source_missing ? 'Источник связи удалён. Копии сохранены, но следовать им не за чем; выбери новый источник.'
            : m.missing ? 'Эта копия удалена у провайдера и больше не считается актуальной; запись сохранена. Источник: ' + m.srcLabel + '.'
            : m.state === 'diverged' ? 'Копия источника ' + m.srcLabel + ', изменённая отдельно: она отличается от него, и правки источника её не перезаписывают, пока не выбрано «Следовать источнику».'
            : 'Копия источника ' + m.srcLabel + '; следует за ним.';
        m.canFollow = !m.missing && m.state === 'diverged' && !!info.source_id && !info.source_missing && !info.source_ambiguous;   // only a copy can differ
        m.canSource = !m.missing && !m.isSource;
        return m;
    }

    function linkPreview(m, action, scope) {
        if (action === 'follow') {
            return 'эта копия, ' + m.selfLabel + ': её содержимое' + (m.series && scope === 'this' ? ' на выбранную дату' : '')
                + ' будет заменено содержимым источника ' + m.srcLabel + '. Источник и остальные копии не меняются.';
        }
        if (action === 'source') {
            var before = m.info.source_ambiguous ? 'Сейчас источник не выбран.' : m.info.source_missing ? 'Прежний источник удалён.' : 'Прежний источник ' + m.srcLabel + ' станет копией.';
            return 'сама группа, а не копия: оригиналом (источником) станет ' + m.selfLabel + '. ' + before
                + ' Содержимое копий сразу не перезаписывается; правки нового источника дальше передаются копиям, которые ему следуют.' + (m.series ? ' Для повторяющегося события это вся серия.' : '');
        }
        return 'только связь: ' + m.selfLabel + ' станет отдельным событием и перестанет получать изменения источника. '
            + (m.isSource ? 'Это источник группы: у остальных копий источника не останется, новый можно выбрать на любой из них. ' : 'Источник и остальные копии не меняются. ')
            + 'Ничего не удаляется у провайдера.' + (m.series ? ' Для повторяющегося события отвязывается вся серия.' : '');
    }

    function linkOutcome(r, action, m) {
        var head = action === 'follow' ? 'Копия ' + m.selfLabel + ' обновлена из источника' : action === 'source' ? 'Источником связи стало ' + m.selfLabel : m.selfLabel + ' отвязано от группы';
        var notes = [], pend = (r.assignments || []).filter(function (a) { return a.status && a.status !== 'done'; });
        if (pend.length) notes.push('провайдер подтвердил не всё: ' + pend.map(function (a) { return (a.calendar_name || a.calendar_id) + ': ' + (a.message || a.status); }).join('; '));
        if ((r.unmatched_occurrences || []).length) notes.push('вхождений без пары в источнике, не изменены: ' + r.unmatched_occurrences.length);
        return head + (notes.length ? '. ' + notes.join('. ') : '') + '.';
    }

    /* The box repaints itself, never the card: arming an action must not rebuild the form and lose unsaved edits. */
    function renderLinkBox(f) {
        if (!f.grouped && !f.link) return null;
        var ui = f.linkUi, box = el('div', { class: 'linkbox', role: 'group', 'aria-label': 'Связь с копиями' });
        function paint() {
            box.textContent = '';
            var m = linkModel(f);
            if (!m) { box.appendChild(el('div', { class: 'fact', text: f.loading ? 'Загружаю связь…' : 'Связь с копиями не удалось прочитать.' })); return; }
            box.appendChild(el('div', { class: 'fact', text: m.fact }));
            var members = m.info.members || [];
            if (members.length > 1) {
                var list = el('ul');
                members.slice(0, 8).forEach(function (x) {
                    list.appendChild(el('li', { text: (x.calendar_name || x.calendar_id) + ' — ' + (x.missing ? LINK_WORD.missing : x.source ? 'источник' : x.state === 'diverged' ? LINK_WORD.diverged : 'копия, следует')
                        + (x.id === m.self ? ' (это событие)' : '') }));
                });
                if (members.length > 8) list.appendChild(el('li', { text: 'и ещё ' + (members.length - 8) }));
                box.appendChild(list);
            }
            if (ui.pending) return confirmPanel(m);
            var acts = el('div', { class: 'acts' });
            function act(action, text) {
                acts.appendChild(el('button', { type: 'button', text: text, onclick: function () { ui.pending = { action: action, scope: 'all' }; ui.error = ''; paint(); box.querySelector('.confirm .primary').focus(); } }));
            }
            if (m.canFollow) act('follow', 'Следовать источнику');
            if (m.canSource) act('source', 'Сделать источником');
            act('detach', 'Отвязать');
            box.appendChild(acts);
        }
        function confirmPanel(m) {
            var p = ui.pending, preview = el('p', { class: 'preview' });
            function say() { preview.textContent = 'Что изменится: ' + linkPreview(m, p.action, p.scope); }
            say();
            var panel = el('div', { class: 'confirm' });
            if (p.action === 'follow' && m.series) {
                var scope = el('select', { 'aria-label': 'Охват' }); [['all', 'всё расписание'], ['this', 'только это вхождение']].forEach(function (o) { scope.appendChild(el('option', { value: o[0], text: o[1] })); });
                scope.value = p.scope;
                scope.addEventListener('change', function () { p.scope = scope.value; say(); });
                panel.appendChild(scope);
            }
            panel.appendChild(preview);
            panel.appendChild(el('div', { class: 'acts' }, [
                el('button', { type: 'button', class: 'primary', text: ui.busy ? 'Применяю…' : 'Применить', disabled: ui.busy ? 'disabled' : null, onclick: function () { apply(m); } }),
                el('button', { type: 'button', text: 'Отмена', disabled: ui.busy ? 'disabled' : null, onclick: function () { ui.pending = null; ui.error = ''; paint(); } })
            ]));
            if (ui.error) panel.appendChild(el('div', { class: 'status err', role: 'alert', text: ui.error }));
            box.appendChild(panel);
        }
        function apply(m) {
            var p = ui.pending;
            if (!p || ui.busy) return;
            ui.busy = true; ui.error = ''; paint();
            /* source and detach address the series master; follow keeps the occurrence the card was opened on */
            post('event/link', { id: p.action === 'follow' ? f.id : m.self, action: p.action, scope: p.action === 'follow' && m.series ? p.scope : 'all' }).then(function (r) {
                if (r.status !== 'ok' && r.status !== 'partial') throw new Error(r.message || 'Уроборос не подтвердил изменение связи');
                if (state.editing === f) state.editing = null;
                state.notice = linkOutcome(r, p.action, m);
                return load();
            }).catch(function (e) {
                ui.busy = false; ui.error = e.message;
                if (state.editing === f) paint();
            });
        }
        paint();
        return box;
    }

    /* ---------- lifecycle ---------- */
    document.addEventListener('visibilitychange', accWake);
    window.addEventListener('focus', accWake);
    if (typeof window.__ouroWidgetOnDispose === 'function') {
        window.__ouroWidgetOnDispose(function () {
            state.disposed = true;
            if (state.poll) clearInterval(state.poll);
            clearTimeout(acc.timer);
            document.removeEventListener('visibilitychange', accWake);
            window.removeEventListener('focus', accWake);
            if (typeof offTheme === 'function') offTheme();
        });
    }
    state.poll = setInterval(function () { if (!state.editing && !accTyping() && !state.disposed) load(); }, POLL_MS);
    load();
})();
