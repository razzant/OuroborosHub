/* Check-in card (module widget).
 *
 * It reads this skill's own `status` route and posts only to this skill's own routes, through
 * the host bridge (OuroborosWidget.fetch): no other network, no postMessage, no storage.
 *
 * The “I'm here” button is bound to the exact deadline it shows (agreement id, UTC instant and
 * label) as captured when the card loaded or when the owner pressed Refresh. The periodic status
 * read updates the text, never a button's deadline; a submitted deadline stays on its button (a
 * repeat changes nothing) until Refresh. A refused or failed request is shown and never retried.
 */
(() => {
    'use strict';
    const BASE = '/api/extensions/check-in/';
    const POLL_MS = 30000;
    const READ_TIMEOUT_MS = 20000;
    const WRITE_TIMEOUT_MS = 30000;
    const ROWS = [
        ['agreement', 'Agreement'], ['today', 'Today’s deadline'], ['next_unanswered', 'Next unanswered'],
        ['streak', 'Missed check-in'], ['last_checkin', 'Last check-in'], ['contact_stage', 'Contact stage'],
        ['mail', 'Mail server'], ['wakes', 'Wakes'], ['attempt', 'Last contact attempt'],
        ['activation', 'Server activation'],
    ];
    const STYLE = `
:root { color-scheme: light; --bg: #ffffff; --fg: #1d2330; --muted: #5d6677; --line: #d9dee7;
  --accent: #2459d6; --accent-fg: #ffffff; --soft: #f3f5f9; --warn-bg: #fff4d6; --warn-fg: #6b4a00;
  --danger-bg: #fde3e3; --danger-fg: #8a1c1c; --ok-fg: #1f6b3a; }
:root[data-theme="dark"] { color-scheme: dark; --bg: #171b22; --fg: #e6e9ef; --muted: #9aa3b2; --line: #2e3542;
  --accent: #5b8cff; --accent-fg: #0d1117; --soft: #212733; --warn-bg: #3a3014; --warn-fg: #f3d27a;
  --danger-bg: #3d1d1f; --danger-fg: #ffb4b4; --ok-fg: #7bd88f; }
html, body { margin: 0; background: var(--bg); color: var(--fg); }
#root { box-sizing: border-box; padding: 14px 16px; font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
#root * { box-sizing: border-box; }
.head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
h2 { margin: 0; font-size: 16px; }
.headline { margin: 6px 0 10px; }
.notice { margin: 8px 0; padding: 8px 10px; border-radius: 6px; }
.notice.warn { background: var(--warn-bg); color: var(--warn-fg); }
.notice.danger { background: var(--danger-bg); color: var(--danger-fg); }
.checkin { margin: 10px 0 12px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--soft); }
.checkin p { margin: 6px 0 0; }
button { font: inherit; border-radius: 6px; border: 1px solid var(--line); background: var(--bg); color: var(--fg);
  padding: 6px 12px; cursor: pointer; }
button:disabled { opacity: .55; cursor: default; }
button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
button.primary { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); font-weight: 600;
  padding: 10px 14px; max-width: 100%; text-align: left; }
.row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 8px; }
.muted { color: var(--muted); }
.ok { color: var(--ok-fg); }
.status.danger { color: var(--danger-fg); }
.status.ok { color: var(--ok-fg); }
dl { display: grid; grid-template-columns: minmax(120px, max-content) 1fr; gap: 4px 12px; margin: 0; }
dt { color: var(--muted); }
dd { margin: 0; overflow-wrap: anywhere; }
details { margin-top: 12px; border-top: 1px solid var(--line); padding-top: 8px; }
summary { cursor: pointer; font-weight: 600; }
label { display: inline-flex; gap: 6px; align-items: center; }
input[type="text"] { font: inherit; padding: 5px 8px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--bg); color: var(--fg); min-width: 170px; }
.control { margin-top: 10px; }
.foot { margin-top: 12px; font-size: 12.5px; }
`;

    const root = document.getElementById('root');
    const bridge = window.OuroborosWidget;
    const state = {
        view: null,                      // the latest status read
        readError: '',
        bound: { main: null, next: null },   // what each button posts; set on load / Refresh only
        submitted: { main: '', next: '' },   // server message after submitting that button's deadline
        confirmNext: false,
        busy: '',                        // the request in flight
        checkinFeedback: { text: '', tone: '' },
        controlFeedback: { text: '', tone: '' },
        pauseUntil: '', cancelConfirm: false, armConfirm: false, controlsOpen: false,
    };
    let disposed = false;
    let timer = null;
    let themeOff = () => {};

    function el(tag, props, ...children) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(props || {})) {
            if (key === 'class') node.className = value;
            else if (key === 'text') node.textContent = value;
            else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
            else if (value === true) node.setAttribute(key, '');
            else if (value !== false && value !== null && value !== undefined) node.setAttribute(key, String(value));
        }
        for (const child of children) if (child) node.append(child);
        return node;
    }

    const key = (target) => (target ? `${target.agreement_id}|${target.for_deadline}` : '');

    // What submitting a bound deadline would also close, per the latest status read (a button keeps
    // its deadline, but a miss before it can open or close meanwhile): the latest missed deadline's
    // label, '' when nothing is missed, null when the status no longer shows that deadline.
    function alsoCloses(bound, current) {
        return current && key(current) === key(bound) ? current.also_closes || '' : null;
    }

    // ------------------------------------------------------------------ requests

    async function call(route, method, body) {
        const init = { method, timeoutMs: method === 'GET' ? READ_TIMEOUT_MS : WRITE_TIMEOUT_MS };
        if (method !== 'GET') {
            init.headers = { 'Content-Type': 'application/json' };
            init.body = JSON.stringify(body || {});
        }
        const response = await bridge.fetch(BASE + route, init);
        const data = await response.json().catch(() => ({}));
        return { ok: response.ok && !(data && data.error), status: response.status, data: data || {} };
    }

    async function read() {
        try {
            const answer = await call('status', 'GET');
            if (!answer.ok) throw new Error(answer.data.error || `HTTP ${answer.status}`);
            state.view = answer.data;
            state.readError = '';
        } catch (error) {
            state.readError = `The status could not be read (${error.message || error}). The buttons keep the `
                + 'deadline they show; press Refresh to try again.';
        }
    }

    // A write is sent once. A refusal is shown as the server worded it; no answer means the
    // outcome is unknown, and nothing is retried.
    async function write(route, body, feedbackKey) {
        state.busy = route;
        state[feedbackKey] = { text: 'Working…', tone: 'muted' };
        render();
        let result = null;
        try {
            const answer = await call(route, 'POST', body);
            if (answer.ok) {
                result = answer.data;
                state[feedbackKey] = { text: answer.data.message || 'Done.', tone: 'ok' };
            } else {
                state[feedbackKey] = { text: answer.data.error || `Refused (HTTP ${answer.status}).`, tone: 'danger' };
            }
        } catch (error) {
            state[feedbackKey] = {
                text: `No answer (${error.message || error}): it may or may not have been recorded. Press Refresh `
                    + 'to see the current state; nothing is sent again by itself.',
                tone: 'danger',
            };
        } finally {
            state.busy = '';
        }
        if (disposed) return null;
        await read();
        render();
        return result;
    }

    async function refresh() {
        if (state.busy) return;
        state.busy = 'status';
        render();
        await read();
        state.busy = '';
        if (state.view) {
            state.bound = { main: state.view.checkin_target || null, next: state.view.next_target || null };
            state.submitted = { main: '', next: '' };
            state.confirmNext = false;
            state.checkinFeedback = { text: '', tone: '' };
        }
        render();
    }

    async function checkIn(which) {
        const target = state.bound[which];
        if (!target || state.busy) return;
        const result = await write('checkin', {
            agreement_id: target.agreement_id, for_deadline: target.for_deadline, deadline_label: target.deadline_label,
        }, 'checkinFeedback');
        if (result) state.submitted[which] = result.message || 'Done.';
        state.confirmNext = false;
        render();
    }

    async function control(route, body, rebind) {
        if (state.busy) return;
        const result = await write(route, body, 'controlFeedback');
        if (result && rebind) {
            // Pausing, resuming or cancelling is the owner's explicit change of the agreement:
            // the buttons move to what the status shows now.
            const feedback = state.controlFeedback;
            await refresh();
            state.controlFeedback = feedback;
        }
        if (result) { state.pauseUntil = ''; state.cancelConfirm = false; state.armConfirm = false; }
        render();
    }

    function schedule() {
        if (disposed) return;
        timer = setTimeout(async () => {
            timer = null;
            if (disposed) return;
            if (!state.busy) {
                await read();
                render();
            }
            schedule();
        }, POLL_MS);
    }

    // ------------------------------------------------------------------ rendering

    const regions = {};

    function patch(name, signature, build) {
        const region = regions[name];
        if (region.dataset.sig === signature) return;
        region.dataset.sig = signature;
        region.replaceChildren(...build());
    }

    function mainLabel(target) {
        if (target.kind === 'today') return `I’m here — today, ${target.deadline_label}`;
        if (target.kind === 'missed') return `I’m here — close the missed check-in for ${target.deadline_label}`;
        return `I’m here — check in for ${target.deadline_label}`;
    }

    function status(feedback) {
        return el('p', { class: `status ${feedback.tone || ''}`, role: 'status', 'aria-live': 'polite',
            text: feedback.text });
    }

    function buildCheckin() {
        const view = state.view || {};
        const busy = Boolean(state.busy);
        const { main, next } = state.bound;
        const nodes = [];
        if (main) {
            nodes.push(el('button', { class: 'primary', type: 'button', 'data-action': 'checkin-main', disabled: busy,
                onclick: () => checkIn('main') }, mainLabel(main)));
            const closes = main.kind === 'today' && alsoCloses(main, view.checkin_target);
            if (closes) nodes.push(el('p', { class: 'muted', text: `Also closes the missed check-in for ${closes}.` }));
            if (state.submitted.main) {
                nodes.push(el('p', { class: 'ok', text: '✓ Submitted for this deadline; pressing again changes '
                    + 'nothing. Refresh moves the button to the current deadline.' }));
            }
        } else {
            nodes.push(el('p', { class: 'muted', text: view.rows && view.rows.today && view.rows.today !== '—'
                ? `Nothing to check in for now. Today’s deadline: ${view.rows.today}.`
                : 'Nothing to check in for now.' }));
        }
        const changed = state.view && (key(main) !== key(view.checkin_target) || key(next) !== key(view.next_target))
            && !(state.submitted.main && !view.checkin_target);
        if (changed) {
            const now = view.checkin_target ? ` It now shows ${view.checkin_target.deadline_label}.` : '';
            nodes.push(el('p', { class: 'notice warn', 'data-notice': 'rebind', text: 'The status changed since these '
                + `buttons were set; they still check in for the deadline they show.${now} Press Refresh to update them.` }));
        }
        if (next) {
            if (state.confirmNext) {
                // Like any dated check-in, it closes whatever was missed before that deadline.
                const closes = alsoCloses(next, view.next_target);
                const also = closes ? ` and also closes the missed check-in for ${closes}.`
                    : closes === '' ? '.' : '; a check-in missed before it is closed too.';
                nodes.push(el('p', { 'data-confirm': 'next', text: `Check in now for ${next.deadline_label}? This `
                    + `answers that deadline before it comes${also}` }));
                nodes.push(el('div', { class: 'row' },
                    el('button', { type: 'button', 'data-action': 'checkin-next-confirm', disabled: busy,
                        onclick: () => checkIn('next') }, 'Confirm early check-in'),
                    el('button', { type: 'button', 'data-action': 'checkin-next-cancel', disabled: busy,
                        onclick: () => { state.confirmNext = false; render(); } }, 'Not now')));
            } else {
                nodes.push(el('div', { class: 'row' }, el('button', { type: 'button', 'data-action': 'checkin-next',
                    disabled: busy, onclick: () => { state.confirmNext = true; render(); } },
                `Check in early for the next deadline: ${next.deadline_label}`)));
            }
            if (state.submitted.next) nodes.push(el('p', { class: 'ok', text: '✓ Early check-in submitted.' }));
        }
        nodes.push(status(state.checkinFeedback));
        return nodes;
    }

    function buildRows() {
        const rows = (state.view && state.view.rows) || {};
        return [el('dl', {}, ...ROWS.flatMap(([name, label]) => [el('dt', { text: label }),
            el('dd', { 'data-row': name, text: rows[name] === undefined ? '—' : String(rows[name]) })]))];
    }

    function buildControls() {
        const controls = (state.view && state.view.controls) || {};
        const busy = Boolean(state.busy);
        const parts = [];
        if (controls.pause) {
            parts.push(el('div', { class: 'control row' },
                el('label', {}, 'Pause until (local time)', el('input', { type: 'text', placeholder: 'YYYY-MM-DD HH:MM',
                    'data-input': 'until_local', value: state.pauseUntil,
                    oninput: (event) => { state.pauseUntil = event.target.value; } })),
                el('button', { type: 'button', disabled: busy, onclick: () => control('pause',
                    { until_local: state.pauseUntil }, true) }, 'Pause')));
            parts.push(el('p', { class: 'muted', text: 'Daily agreements only, in the agreement’s timezone. A pause is '
                + 'not a check-in and turns the contact stage off.' }));
        }
        if (controls.resume) {
            parts.push(el('div', { class: 'control row' }, el('button', { type: 'button', disabled: busy,
                onclick: () => control('resume', {}, true) }, 'Resume')));
        }
        if (controls.disarm) {
            parts.push(el('div', { class: 'control row' }, el('button', { type: 'button', disabled: busy,
                onclick: () => control('disarm', {}, false) }, 'Turn contact stage off')));
        }
        if (controls.arm) {
            parts.push(el('div', { class: 'control row' },
                el('label', {}, el('input', { type: 'checkbox', checked: state.armConfirm,
                    onchange: (event) => { state.armConfirm = event.target.checked; } }),
                'If I miss a check-in and don’t answer the reminder, Ouroboros may write once to my contact'),
                el('button', { type: 'button', disabled: busy, onclick: () => control('arm',
                    { confirm: state.armConfirm }, false) }, 'Arm contact stage')));
        }
        if (controls.cancel) {
            parts.push(el('div', { class: 'control row' },
                el('label', {}, el('input', { type: 'checkbox', checked: state.cancelConfirm,
                    onchange: (event) => { state.cancelConfirm = event.target.checked; } }),
                'Yes, end the agreement (this is not a check-in)'),
                el('button', { type: 'button', disabled: busy, onclick: () => control('cancel',
                    { confirm: state.cancelConfirm }, true) }, 'Cancel agreement')));
        }
        if (!parts.length) parts.push(el('p', { class: 'muted', text: 'No agreement to pause or cancel.' }));
        const details = el('details', { open: state.controlsOpen,
            ontoggle: (event) => { state.controlsOpen = event.target.open; } },
        el('summary', { text: 'Pause, cancel and contact stage' }), ...parts, status(state.controlFeedback));
        return [details];
    }

    function render() {
        if (disposed) return;
        const view = state.view;
        regions.headline.textContent = view ? view.headline || '' : 'Loading…';
        regions.refresh.disabled = Boolean(state.busy);
        patch('notices', JSON.stringify([state.readError, view && view.warning, view && view.alert]), () => [
            state.readError && el('p', { class: 'notice danger', text: state.readError }),
            view && view.alert && el('p', { class: 'notice danger', 'data-notice': 'alert', text: view.alert }),
            view && view.warning && el('p', { class: 'notice warn', 'data-notice': 'warning', text: view.warning }),
        ].filter(Boolean));
        patch('checkin', JSON.stringify([state.bound, state.submitted, state.confirmNext, state.busy,
            state.checkinFeedback, view && view.checkin_target, view && view.next_target, view && view.rows
            && view.rows.today]), buildCheckin);
        patch('rows', JSON.stringify(view && view.rows), buildRows);
        patch('controls', JSON.stringify([view && view.controls, state.busy, state.controlFeedback]), buildControls);
        patch('foot', JSON.stringify(view && view.limits), () => [
            el('p', { class: 'muted', text: (view && view.limits) || '' }),
            el('p', { class: 'muted', text: 'Set up or change the agreement in chat. The contact and the mail server '
                + 'are in Settings → Check-in, which also has “Check in for displayed deadline”. Stop on a wake task '
                + 'stops that task only; cancelling ends the agreement; disabling the skill removes this card and '
                + 'the tools; Panic stops everything, and the contact stage stays off until you arm it again. After '
                + 'a restart Ouroboros tries once to tell you in chat that the contact stage turned off; that notice '
                + 'may not arrive, so this card is the reliable view. A message already handed to the mail server '
                + 'cannot be recalled.' }),
        ]);
    }

    function mount() {
        const style = document.createElement('style');
        style.textContent = STYLE;
        document.head.append(style);
        regions.refresh = el('button', { type: 'button', 'data-action': 'refresh', onclick: () => refresh() }, 'Refresh');
        regions.headline = el('p', { class: 'headline', 'data-region': 'headline' });
        for (const name of ['notices', 'checkin', 'rows', 'controls', 'foot']) {
            regions[name] = el('div', { 'data-region': name, class: name === 'checkin' ? 'checkin' : name });
        }
        root.replaceChildren(el('div', { class: 'head' }, el('h2', { text: 'Check-in' }), regions.refresh),
            regions.headline, regions.notices, regions.checkin, regions.rows, regions.controls, regions.foot);
        render();
    }

    if (!root) return;
    if (!bridge || typeof bridge.fetch !== 'function') {
        root.textContent = 'The Check-in card needs the Ouroboros widget bridge.';
        return;
    }
    if (typeof bridge.onTheme === 'function') {
        themeOff = bridge.onTheme((theme) => { document.documentElement.dataset.theme = theme; });
    }
    if (typeof window.__ouroWidgetOnDispose === 'function') {
        window.__ouroWidgetOnDispose(() => {
            disposed = true;
            if (timer !== null) clearTimeout(timer);
            timer = null;
            themeOff();
        });
    }
    mount();
    refresh().then(schedule);
})();
