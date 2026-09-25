/* Календарь Уробороса — module widget (classic script inside the host's sandboxed frame).
 * Data only through OuroborosWidget.fetch on this skill's own routes; no storage, no parent messages
 * beyond the host bridge. Day view (hour grid + upcoming list) is primary, Week is the second mode.
 * Click on the grid creates, a card edits, drag moves, the bottom edge resizes; narrow screens use
 * buttons and fields instead of drag. Hidden (service) events are hatched busy time until the toggle
 * reveals them; «обычно» (soft) preferences are dotted bands, never solid bookings.
 */
(function () {
    'use strict';

    var ROOT = '/api/extensions/calendar/';
    var POLL_MS = 30000;
    var HOUR_PX = 44;
    var SNAP_MIN = 15;
    var DAY_START_H = 0;

    var state = {
        view: 'day', date: null, data: null, error: '', loading: false, showHidden: false,
        selectedCals: null, editing: null, drag: null, disposed: false, theme: 'light', poll: null, dirty: false, notice: ''

    };
    var root = document.getElementById('root') || document.body;

    /* ---------- utils ---------- */
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    function localDate(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
    function parseISO(s) { return s ? new Date(s) : null; }
    function fmtHM(d) { return pad(d.getHours()) + ':' + pad(d.getMinutes()); }
    function fmtLocalInput(d) { return localDate(d) + 'T' + fmtHM(d); }
    function withOffset(local) {   // 'YYYY-MM-DDTHH:MM' typed/dragged in the browser zone → ISO with that zone's offset
        if (!local) return local;
        var d = new Date(local); if (isNaN(d.getTime())) return local;
        var off = -d.getTimezoneOffset(), sign = off >= 0 ? '+' : '-', a = Math.abs(off);
        return local.slice(0, 16) + sign + pad(Math.floor(a / 60)) + ':' + pad(a % 60);
    }
    function addDays(dateStr, n) { var d = new Date(dateStr + 'T00:00:00'); d.setDate(d.getDate() + n); return localDate(d); }
    function minutesOf(d) { return d.getHours() * 60 + d.getMinutes(); }
    function el(tag, attrs, children) {
        var node = document.createElement(tag);
        if (attrs) Object.keys(attrs).forEach(function (k) {
            if (k === 'class') node.className = attrs[k];
            else if (k === 'style') node.setAttribute('style', attrs[k]);
            else if (k.indexOf('on') === 0) node.addEventListener(k.slice(2), attrs[k]);
            else if (k === 'text') node.textContent = attrs[k];
            else node.setAttribute(k, attrs[k]);
        });
        (children || []).forEach(function (c) { if (c) node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
        return node;
    }
    function api(path, init) {
        return OuroborosWidget.fetch(ROOT + path, init).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (j) { if (!r.ok) throw new Error(j.message || ('HTTP ' + r.status)); return j; });
        });
    }
    function post(path, body) { return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); }

    /* ---------- styles ---------- */
    var css = [
        ':root{--bg:#fff;--fg:#1c1c1e;--muted:#6b6f76;--line:#e3e5e8;--soft:#f4f5f7;--accent:#3b6cf6;--accent-bg:#e8effd;--busy:#dfe3ea;--danger:#c0392b;--card:#fff}',
        ':root[data-theme="dark"]{--bg:#121316;--fg:#ececef;--muted:#9a9ea6;--line:#2a2d33;--soft:#1b1d22;--accent:#7aa2ff;--accent-bg:#1f2a44;--busy:#2c3038;--danger:#ff7b6b;--card:#181a1f}',
        'html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);font:14px/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}',
        '#root{padding:12px;box-sizing:border-box}',
        '.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}',
        '.bar .title{font-weight:600;font-size:16px;margin-right:auto}',
        'button{background:var(--soft);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:5px 10px;cursor:pointer;font:inherit}',
        'button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{color:var(--danger)}button.on{background:var(--accent-bg);border-color:var(--accent)}',
        '.grid{display:grid;grid-template-columns:48px 1fr;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--card)}',
        '.grid.week{grid-template-columns:48px repeat(7,1fr)}',
        '.hours{position:relative}.hour{height:' + HOUR_PX + 'px;border-top:1px solid var(--line);font-size:11px;color:var(--muted);padding:2px 4px;box-sizing:border-box}',
        '.col{position:relative;border-left:1px solid var(--line);cursor:crosshair}.col .hline{height:' + HOUR_PX + 'px;border-top:1px solid var(--line);box-sizing:border-box}',
        '.colhead{font-size:12px;color:var(--muted);text-align:center;padding:6px 0;border-bottom:1px solid var(--line);border-left:1px solid var(--line);background:var(--soft)}.colhead.today{color:var(--accent);font-weight:600}',
        '.corner{border-bottom:1px solid var(--line);background:var(--soft)}',
        '.ev{position:absolute;left:3px;right:6px;border-radius:7px;padding:3px 6px;font-size:12px;box-sizing:border-box;overflow:hidden;background:var(--accent-bg);border-left:3px solid var(--accent);cursor:grab;user-select:none}',
        '.ev.hidden{opacity:.75;border-left-style:dashed}.ev.pending{outline:1px dashed var(--muted)}.ev.conflict{outline:2px solid var(--danger)}',
        '.ev .t{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ev .m{color:var(--muted);font-size:11px}',
        '.ev .rs{position:absolute;left:0;right:0;bottom:0;height:7px;cursor:ns-resize}',
        '.hatch{position:absolute;left:0;right:0;background:repeating-linear-gradient(135deg,var(--busy) 0 6px,transparent 6px 12px);opacity:.9;pointer-events:none}',
        '.usual{position:absolute;left:0;right:0;border-top:2px dotted var(--accent);border-bottom:2px dotted var(--accent);opacity:.7;font-size:11px;color:var(--accent);padding:0 6px;box-sizing:border-box;pointer-events:none}',
        '.now{position:absolute;left:0;right:0;border-top:2px solid var(--danger);pointer-events:none}',
        '.allday{display:flex;flex-wrap:wrap;gap:6px;padding:6px 8px;border-bottom:1px solid var(--line);min-height:20px;border-left:1px solid var(--line)}',
        '.chip{background:var(--accent-bg);border-radius:6px;padding:2px 8px;font-size:12px;cursor:pointer}',
        '.list{margin-top:12px}.list h3{font-size:13px;color:var(--muted);margin:8px 0 4px;font-weight:600}.row{display:flex;gap:10px;padding:6px 8px;border-radius:8px;cursor:pointer}.row:hover{background:var(--soft)}.row .when{color:var(--muted);min-width:96px}',
        '.card{position:fixed;inset:0;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;z-index:9}',
        '.card form{background:var(--card);color:var(--fg);border-radius:12px;padding:16px;width:min(440px,94vw);max-height:92vh;overflow:auto;box-shadow:0 10px 40px rgba(0,0,0,.3)}',
        '.card label{display:block;font-size:12px;color:var(--muted);margin:8px 0 2px}.card input,.card select,.card textarea{width:100%;box-sizing:border-box;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}',
        '.card input[type="checkbox"]{width:auto;margin:0 6px 0 0;vertical-align:middle}.card label.inline{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--fg);margin:8px 0 2px}',
        '.card .actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}.card .actions .spacer{flex:1}',
        '.status{font-size:12px;color:var(--muted);margin-top:8px}.err{color:var(--danger)}',
        '.cals{display:flex;flex-wrap:wrap;gap:6px}.cals button{font-size:12px;padding:3px 8px}',
        '@media (max-width:600px){.grid.week{grid-template-columns:36px repeat(7,1fr)}.ev{font-size:11px;padding:2px 4px}.bar .title{width:100%}}'
    ].join('\n');
    var styleTag = el('style', { text: css });
    document.head.appendChild(styleTag);

    /* ---------- theme ---------- */
    function applyTheme(t) { state.theme = t === 'dark' ? 'dark' : 'light'; document.documentElement.dataset.theme = state.theme; }
    if (window.OuroborosWidget && typeof OuroborosWidget.onTheme === 'function') {
        try { OuroborosWidget.onTheme(applyTheme); } catch (e) { /* older host: fall back below */ }
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
            state.data = d; state.error = ''; if (!state.date) state.date = d.anchor;
        }).catch(function (e) { state.error = e.message || String(e); }).then(function () { state.loading = false; render(); });
    }

    /* ---------- rendering ---------- */
    function render() {
        root.innerHTML = '';
        var d = state.data;
        var bar = el('div', { class: 'bar' }, [
            el('span', { class: 'title', text: 'Календарь' + (d ? ' · ' + (state.view === 'week' ? 'неделя' : d.days[0].weekday + ' ' + d.anchor) : '') }),
            el('button', { text: '‹', onclick: function () { shift(-1); } }),
            el('button', { text: 'Сегодня', onclick: function () { state.date = null; load(); } }),
            el('button', { text: '›', onclick: function () { shift(1); } }),
            el('button', { text: 'День', class: state.view === 'day' ? 'on' : '', onclick: function () { state.view = 'day'; load(); } }),
            el('button', { text: 'Неделя', class: state.view === 'week' ? 'on' : '', onclick: function () { state.view = 'week'; load(); } }),
            el('button', { text: 'Служебные', class: state.showHidden ? 'on' : '', title: 'Показать служебные события и «обычно»', onclick: function () { state.showHidden = !state.showHidden; load(); } }),
            el('button', { text: '⚙', title: 'Напоминания и синхронизация', class: state.panel ? 'on' : '', onclick: function () { togglePanel(); } }),
            el('button', { text: '+ Событие', class: 'primary', onclick: function () { openCard(null, defaultStart()); } })
        ]);
        root.appendChild(bar);
        if (state.panel) root.appendChild(renderPanel());
        if (state.error) root.appendChild(el('div', { class: 'status err', text: 'Ошибка: ' + state.error }));
        if (state.notice) root.appendChild(el('div', { class: 'status', text: state.notice }));
        if (!d) { root.appendChild(el('div', { class: 'status', text: state.loading ? 'Загрузка…' : 'Нет данных' })); return; }
        root.appendChild(renderCalendarChips(d));
        root.appendChild(renderGrid(d));
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
        var grid = el('div', { class: 'grid' + (state.view === 'week' ? ' week' : '') });
        grid.appendChild(el('div', { class: 'corner' }));
        var todayStr = localDate(new Date());
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
                el('div', { class: 'm', text: fmtHM(parseISO(e.start)) + '–' + fmtHM(parseISO(e.end)) + (e.calendar_name ? ' · ' + e.calendar_name : '') + (e.sync_state === 'pending' ? ' · ожидает' : e.sync_state === 'pending_delete' ? ' · удаляется' : e.sync_state === 'conflict' ? ' · конфликт' : '') }),
                el('div', { class: 'rs' })
            ]);
            node.style.left = 'calc(' + (item.left * 100) + '% + 3px)';
            node.style.right = 'calc(' + ((1 - item.left - item.width) * 100) + '% + 4px)';
            place(col, day.date, e.start, e.end, node);
            attachDrag(node, e, col);
            col.appendChild(node);
        });
        var now = new Date();
        if (localDate(now) === day.date) { var nl = el('div', { class: 'now' }); nl.style.top = (minutesOf(now) * HOUR_PX / 60) + 'px'; col.appendChild(nl); }
        col.addEventListener('click', function (ev) {
            if (ev.target !== col && !ev.target.classList.contains('hline')) return;
            var rect = col.getBoundingClientRect();
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
        var now = new Date();
        var upcoming = d.events.filter(function (e) { return parseISO(e.end) >= now; }).slice(0, 8);
        if (!upcoming.length) box.appendChild(el('div', { class: 'status', text: 'Дальше сегодня ничего нет' }));
        upcoming.forEach(function (e) {
            box.appendChild(el('div', { class: 'row', onclick: function () { openCard(e); } }, [
                el('span', { class: 'when', text: e.all_day ? 'весь день' : fmtHM(parseISO(e.start)) + '–' + fmtHM(parseISO(e.end)) }),
                el('span', { text: e.title + (e.calendar_name ? ' · ' + e.calendar_name : '') })
            ]));
        });
        return box;
    }

    function renderStatus(d) {
        var parts = [];
        d.accounts.forEach(function (a) { if (a.provider !== 'local') parts.push(a.alias + ': ' + (a.status === 'ok' ? 'ок' : a.status)); });
        if (d.sync.pending) parts.push('ожидает синхронизации: ' + d.sync.pending);
        if (d.sync.conflict) parts.push('конфликтов: ' + d.sync.conflict);
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
                    save(e, { start: withOffset(fmtLocalInput(s)), end: withOffset(fmtLocalInput(ne)) });
                } else {
                    var mins = Math.round(parseFloat(node.style.top) / HOUR_PX * 60 / SNAP_MIN) * SNAP_MIN;
                    var ns = new Date(col.getAttribute('data-date') + 'T00:00:00'); ns.setMinutes(mins);
                    var ne2 = new Date(ns.getTime() + dur * 60000);
                    save(e, { start: withOffset(fmtLocalInput(ns)), end: withOffset(fmtLocalInput(ne2)) });
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
        state.error = pend.length ? 'Сохранено локально; ' + pend.map(function (a) { return (a.calendar_name || a.calendar_id) + ': ' + (a.message || a.status); }).join('; ') : '';
        if (r && r.warning) state.error = (state.error ? state.error + '; ' : '') + r.warning;
    }

    /* ---------- card ---------- */
    function defaultStart() { var d = new Date(); d.setMinutes(Math.ceil(d.getMinutes() / 30) * 30, 0, 0); if (state.date && state.date !== localDate(d)) { d = new Date(state.date + 'T10:00:00'); } return d; }

    function openCard(ev, start) {
        var s = ev ? parseISO(ev.start) : start;
        var e = ev ? parseISO(ev.end) : new Date(s.getTime() + 60 * 60000);
        var defaultCal = (state.data && (state.data.calendars.filter(function (c) { return c.default; })[0] || state.data.calendars[0]) || {}).id;
        state.editing = {
            id: ev ? ev.id : null, title: ev ? ev.title : '', start: fmtLocalInput(s), end: fmtLocalInput(e), all_day: ev ? !!ev.all_day : false,
            calendars: ev ? [ev.calendar_id] : (defaultCal ? [defaultCal] : []), primary_calendar: ev ? ev.calendar_id : defaultCal,
            hidden: ev ? ev.visibility === 'hidden' : false, availability: ev ? ev.availability : 'busy', location: ev ? (ev.location || '') : '',
            description: '', reminders: [], attendees: [], rrule: ev ? (ev.rrule || '') : '', series: ev ? !!(ev.rrule || ev.series_id) : false,
            orig: ev, loading: !!ev, armed: false
        };
        render();
        if (ev) {
            var token = state.editing;
            api('event/get?id=' + encodeURIComponent(ev.id)).then(function (r) {
                if (state.editing !== token) return;
                var full = r.event || {};
                token.description = full.description || '';
                token.reminders = full.reminders || [];
                token.attendees = (full.attendees || []).map(function (a) { return a.email || String(a); });
                token.location = full.location || token.location;
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
        var scopeSel = el('select'); [['this', 'только эта дата'], ['following', 'начиная с этой даты'], ['all', 'всё расписание']].forEach(function (p) { scopeSel.appendChild(el('option', { value: p[0], text: p[1] })); });
        var status = el('div', { class: 'status', text: f.loading ? 'Загружаю событие…' : (f.loadError ? 'Не удалось загрузить детали: ' + f.loadError : '') });
        if (f.loadError) status.className = 'status err';
        var actions = el('div', { class: 'actions' });
        if (f.id) {
            /* two-step delete: the sandbox has no confirm dialog, so the button arms itself first */
            var del = el('button', { type: 'button', class: 'danger', text: f.armed ? 'Точно удалить' + (f.series ? ' (' + scopeSel.options[scopeSel.selectedIndex].text + ')' : '') + '?' : 'Удалить' });
            del.addEventListener('click', function () {
                if (!f.armed) { f.armed = true; render(); setTimeout(function () { if (state.editing === f && f.armed) { f.armed = false; render(); } }, 5000); return; }
                del.disabled = true; status.textContent = 'Удаляю…'; status.className = 'status';
                post('event/delete', { id: f.id, scope: f.series ? scopeSel.value : 'this', send_updates: notify.checked })
                    .then(function (r) { state.editing = null; noteAssignments(r); return load(); })
                    .catch(function (e) { del.disabled = false; f.armed = false; status.textContent = e.message; status.className = 'status err'; });
            });
            actions.appendChild(del);
        }
        actions.appendChild(el('span', { class: 'spacer' }));
        actions.appendChild(el('button', { type: 'button', text: 'Отмена', onclick: function () { state.editing = null; render(); } }));
        actions.appendChild(el('button', { type: 'submit', class: 'primary', text: f.id ? 'Сохранить' : 'Создать' }));
        form.addEventListener('submit', function (ev) {
            ev.preventDefault();
            var cals = chosenCals();
            if (!cals.length) { status.textContent = 'Выбери хотя бы один календарь'; status.className = 'status err'; return; }
            var body = { title: title.value.trim(), start: allDay.checked ? start.value.slice(0, 10) : withOffset(start.value), end: allDay.checked ? end.value.slice(0, 10) : withOffset(end.value),
                all_day: allDay.checked, hidden: hidden.checked, availability: avail.value,
                location: loc.value, description: desc.value, rrule: rrule.value.trim(), send_updates: notify.checked };
            var remList = rem.value.split(',').map(function (x) { return x.trim(); }).filter(Boolean).map(Number).filter(function (n) { return !isNaN(n); });
            var attList = att.value.split(',').map(function (x) { return x.trim(); }).filter(Boolean);
            var p;
            if (f.id) {
                body.id = f.id; body.scope = f.series ? scopeSel.value : 'this';
                if (remEdited) body.reminders = remList;
                if (attEdited) body.attendees = attList;
                var same = cals.length === f.calendars.length && cals.every(function (c) { return f.calendars.indexOf(c) >= 0; });
                if (!same) body.calendars = cals;
                p = post('event/update', body);
            } else {
                body.calendars = cals; body.reminders = remList; body.attendees = attList;
                p = post('event', body);
            }
            status.textContent = 'Сохраняю…'; status.className = 'status';
            p.then(function (r) { state.editing = null; noteAssignments(r); return load(); })
                .catch(function (e) { status.textContent = e.message; status.className = 'status err'; });
        });
        form.appendChild(el('div', { class: 'title', text: f.id ? 'Событие' : 'Новое событие', style: 'font-weight:600;font-size:15px' }));
        form.appendChild(field('Название', title));
        form.appendChild(field('Начало', start));
        form.appendChild(field('Конец', end));
        form.appendChild(el('label', { class: 'inline' }, [allDay, ' весь день']));
        form.appendChild(field('Календари (первый — основной, остальные получают копии по своему правилу)', calBox));
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

    function shift(n) { state.date = addDays(state.date || localDate(new Date()), state.view === 'week' ? 7 * n : n); load(); }

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

    /* ---------- lifecycle ---------- */
    if (typeof window.__ouroWidgetOnDispose === 'function') {
        window.__ouroWidgetOnDispose(function () { state.disposed = true; if (state.poll) clearInterval(state.poll); });
    }
    state.poll = setInterval(function () { if (!state.editing && !state.disposed) load(); }, POLL_MS);
    load();
})();
