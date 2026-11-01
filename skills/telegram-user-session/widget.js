/* Telegram connector: two ways in, what the account hears, participation and earlier history.
   Host Places stays the only owner of place state, wakes, notes and conversation history. */
(() => {
  const bridge = window.OuroborosWidget;
  const root = document.getElementById('root');
  const style = document.createElement('style');
  style.textContent = `
    :root{color-scheme:dark;--room:#171719;--object:#1d1d1f;--tray:#262628;--capsule:#303033;--ink:#efeff2;--muted:#b8b8c0;
      --accent:#fe7d85;--chosen:#872236;--chosen-ink:#ffdfe1;--rose:#76162b;--rose-ink:#fcf9f9;--amber:#f7cb58;--green:#83e499;
      --red:#fe9498;--line:#4e4e55;--field:#161618;--rest:0 1px 2px #0006,0 6px 20px #0004}
    :root[data-theme=light]{color-scheme:light;--room:#fafbfc;--object:#fff;--tray:#f3f4f6;--capsule:#fff;--ink:#20232b;--muted:#5d6472;
      --accent:#b52b3b;--chosen:#f9e9ec;--chosen-ink:#b52b3b;--rose:#fdf2f4;--rose-ink:#35272c;--amber:#805000;--green:#176b49;
      --red:#b4232f;--line:#c8ccd4;--field:#fff;--rest:0 1px 2px #1c21330d,0 6px 18px #1c21330f}
    @media (prefers-color-scheme:light){:root:not([data-theme]){color-scheme:light;--room:#fafbfc;--object:#fff;--tray:#f3f4f6;
      --capsule:#fff;--ink:#20232b;--muted:#5d6472;--accent:#b52b3b;--chosen:#f9e9ec;--chosen-ink:#b52b3b;--rose:#fdf2f4;--rose-ink:#35272c;
      --amber:#805000;--green:#176b49;--red:#b4232f;--line:#c8ccd4;--field:#fff;--rest:0 1px 2px #1c21330d,0 6px 18px #1c21330f}}
    *{box-sizing:border-box}
    body{margin:0;background:var(--room);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif}
    #root{padding:20px;overflow-wrap:anywhere}
    button,input,textarea,select{font:inherit;color:inherit}
    button{cursor:pointer;border:0;background:var(--capsule);box-shadow:var(--rest);border-radius:999px;padding:9px 16px;min-height:40px;font-weight:600}
    button:hover{filter:brightness(1.08)}
    button[aria-pressed=true]{background:var(--chosen);color:var(--chosen-ink)}
    button:disabled{cursor:default;color:var(--muted);box-shadow:none;filter:none}
    button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
    h2{font-size:16px;line-height:1.3;margin:0 0 16px;font-weight:600}
    h3{font-size:14px;margin:0 0 8px;font-weight:600}
    p{margin:0 0 12px}
    .meta,small{font-size:12px;line-height:1.4;color:var(--muted)}
    .secondary{color:var(--muted)}
    .top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}
    .strip{display:flex;gap:8px;flex-wrap:wrap}
    .strip.small button{font-size:12px;padding:8px 12px;min-height:36px}
    .profile{display:flex;align-items:center;gap:12px;min-width:0;margin-bottom:16px}
    .profile .who{min-width:0;flex:1}
    .avatar{width:40px;height:40px;display:grid;place-items:center;background:var(--rose);color:var(--rose-ink);border-radius:50%;font-size:20px;flex:none}
    .profile strong{font-size:16px;display:block}
    .profile small{display:block;margin-top:2px}
    .grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px;align-items:start}
    .object{background:var(--object);border-radius:18px;padding:24px;box-shadow:var(--rest);min-width:0}
    .wide{grid-column:1/-1}
    .tray{padding:14px 16px;background:var(--tray);border-radius:12px;margin:12px 0}
    .tray p:last-child{margin-bottom:0}
    .method-picker{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;margin-bottom:20px}
    .method-picker button{text-align:left;border-radius:18px;padding:18px 20px;min-height:84px}
    .method-picker small{display:block;margin-top:6px;color:inherit;font-weight:400}
    .field{display:block;margin-top:16px;font-weight:500}
    .field>span{display:block;margin-bottom:6px}
    .field small{display:block;margin-top:6px;font-weight:400}
    .fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,180px),1fr));gap:0 12px}
    input:not([type=checkbox]),select,textarea{display:block;width:100%;min-width:0;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--field);font-weight:400}
    textarea{resize:vertical;min-height:232px;line-height:1.5}
    input[type=checkbox]{width:19px;height:19px;accent-color:var(--accent);flex:none;margin:0}
    .check{display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:52px;cursor:pointer;padding:6px 0}
    .check+.check{border-top:1px solid var(--tray)}
    .check-text{font-weight:500;min-width:0}
    .check small{display:block;font-weight:400}
    .capline{padding:10px 0}
    .capline strong{display:block;margin-bottom:4px}
    .actionbar{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px;align-items:center}
    .quiet{background:none;box-shadow:none;color:var(--accent);padding:6px 0;min-height:36px}
    .quiet:disabled{color:var(--muted)}
    .hint{font-size:12px;color:var(--muted);margin-top:12px}
    .result{font-size:12px;color:var(--muted);margin:10px 0 0;white-space:pre-wrap;min-height:1px}
    .result[data-error=true]{color:var(--red)}
    .lines{white-space:pre-wrap;margin:0}
    .mark{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:500;white-space:nowrap}
    .mark::before{content:'';width:7px;height:7px;border-radius:50%;background:currentColor}
    .tone-good{color:var(--green)}.tone-warn{color:var(--amber)}.tone-bad{color:var(--red)}.tone-quiet{color:var(--muted)}
    .arrowline{display:flex;flex-wrap:wrap;gap:8px;color:var(--muted);align-items:center;margin:4px 0 12px}
    .arrowline b{font-size:12px;font-weight:500;background:var(--tray);padding:6px 10px;border-radius:999px;color:var(--ink)}
    .row{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:10px 0;min-width:0}
    .row+.row{border-top:1px solid var(--tray)}
    .row>div{min-width:0}
    .row>button,.row>span{flex:none;white-space:nowrap}
    .row .meta{display:block;margin-top:2px}
    details{margin-top:16px}
    summary{cursor:pointer;font-weight:600}
    .warning{color:var(--amber)}
    .inline{display:flex;gap:8px;align-items:flex-end}
    .inline .field{flex:1;margin-top:0}
    .note{white-space:pre-wrap}
    .empty{color:var(--muted);padding:12px 0}
    .plan+.plan{border-top:1px solid var(--tray)}
    .plan .row{flex-wrap:wrap}
    .plan .row>div:first-child{flex:1 1 240px}
    .plan .row>.strip{flex:none}
    .plan .row>div>.mark{display:flex;align-items:flex-start;margin-top:4px;white-space:normal}
    .plan .row>div>.mark::before{flex:none;margin-top:5px}
    .plan details{margin:0 0 10px}
    .plan-lines{margin:8px 0 0;padding-left:18px}
    .plan-lines li+li{margin-top:4px}
    .first-step{margin-bottom:20px}
    .hidden,[hidden]{display:none!important}
    @media (max-width:720px){.grid{grid-template-columns:minmax(0,1fr)}}
    @media (max-width:560px){#root{padding:14px}.object{padding:18px}.method-picker button{padding:16px}}
  `;
  document.head.append(style);

  const SUGGESTED_NOTE = 'Speak as yourself, Ouroboros, an AI. Join in when you have something to add, answer private messages, '
    + 'use reactions, and come back with results you promised. Do not speak for me or take on commitments in my name.\n'
    + 'In chats where you already take part, act on your own. Before a first contact with someone new or joining a new chat, '
    + 'if you are not sure it fits, ask me in Main and wait for my answer. After every new join, send me one short note in Main.\n'
    + 'Moderate only in a chat where this note gives you that role. Telegram admin rights alone do not make you a moderator.';
  const SCENES = [['connect', '1 · Connect'], ['scope', '2 · What I hear'], ['participation', '3 · Participation'], ['history', '4 · History']];
  const KIND = {private: 'Private chat', bot: 'Bot', group: 'Group', supergroup: 'Group', forum: 'Forum group', channel: 'Channel'};
  const state = {
    scene: '', method: 'account', status: null, statusError: '', policy: null, policyError: '', setup: null, setupError: '',
    chats: null, chatsError: '', scope: null, imports: null, importsError: '',
    drafts: {api_id: '', api_hash: '', phone: '', code: '', password: '', note: SUGGESTED_NOTE, addPeer: '',
      importMode: 'receiving', importChats: new Set(), importExclude: new Set(), importFrom: '', importTo: '', importCap: '500'},
    results: {}, busy: new Set(), open: new Set(), controls: {},
  };
  let disposed = false;
  const active = new Set();
  const cleanups = [];
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const $ = (selector) => root.querySelector(selector);

  async function call(path, body) {
    const abort = new AbortController(); active.add(abort);
    try {
      const response = await bridge.fetch(`/api/extensions/telegram-user-session/${path}`, {
        method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : {'Content-Type': 'application/json'},
        body: body === undefined ? undefined : JSON.stringify(body), signal: abort.signal, timeoutMs: 20000,
      });
      const value = await response.json();
      if (!response.ok || value.ok === false) throw new Error(value.message || value.error || `Request refused (${response.status})`);
      return value;
    } finally { active.delete(abort); }
  }
  function on(element, name, fn) { element.addEventListener(name, fn); cleanups.push(() => element.removeEventListener(name, fn)); }
  function result(key, text, error = false) { state.results[key] = {text, error}; paintResult(key); }
  function paintResult(key) {
    const node = root.querySelector(`[data-result="${key}"]`);
    const value = state.results[key];
    if (!node || disposed) return;
    node.textContent = value ? value.text : '';
    node.dataset.error = String(Boolean(value?.error));
  }
  const resultLine = (key) => `<p class="result" data-result="${key}" role="status"></p>`;
  const busy = (key) => state.busy.has(key);

  // ---- data -------------------------------------------------------------
  let waitUntil = 0, waitReason = '', waitKnown = false;
  const shape = (s) => JSON.stringify([s?.account_id, s?.profile, s?.configured, s?.legacy_enrolled, Boolean(s?.participant_queue?.jobs?.held)]);
  async function loadStatus() {
    const before = shape(state.status);
    try {
      const value = await call('participant/status');
      state.status = value; state.statusError = '';
      const cooldown = value.cooldown;
      waitKnown = !!cooldown && Number.isFinite(cooldown.remaining_seconds);
      waitUntil = Date.now() + Math.max(0, cooldown?.remaining_seconds || 0) * 1000;
      waitReason = cooldown?.reason || '';
    } catch (error) { state.statusError = error.message; waitKnown = false; }
    if (!state.scene) { state.scene = state.status?.account_id ? 'scope' : 'connect'; render(); }
    else if (shape(state.status) !== before) render();
    else paintStatus();
  }
  let savedMode = '';
  async function loadPolicy() {
    try {
      state.policy = await call('participant/policy'); state.policyError = '';
      if (!savedMode) savedMode = state.policy.mode;
      if (!state.scope?.dirty) resetScopeDraft();
    } catch (error) { state.policyError = error.message; }
  }
  async function loadSetup() {
    try { state.setup = await call('setup/config'); state.setupError = ''; state.drafts.api_id = state.setup.api_id || state.drafts.api_id; }
    catch (error) { state.setupError = error.message; }
  }
  async function loadChats() {
    try { state.chats = await call('participant/chats'); state.chatsError = ''; if (!state.scope) resetScopeDraft(); }
    catch (error) { state.chatsError = error.message; }
  }
  // The companion answers a requested dialog read later; reread the local list until it has.
  // A re-render keeps unsaved scope choices, the focused field, its caret and open sections.
  async function followRefresh() {
    if (disposed || !state.chats?.refresh?.pending) return;
    const before = JSON.stringify(state.chats);
    await loadChats();
    if (JSON.stringify(state.chats) !== before && ['scope', 'history'].includes(state.scene)) render();
  }
  async function loadImports() {
    try { state.imports = await call('participant/import'); state.importsError = ''; }
    catch (error) { state.importsError = error.message; }
  }
  async function followImports() {
    if (disposed || state.scene !== 'history' || !(state.imports?.plans || []).some((plan) => plan.state === 'running')) return;
    const before = JSON.stringify(state.imports), states = (state.imports?.plans || []).map((plan) => plan.state).join();
    await loadImports();
    if (states !== (state.imports?.plans || []).map((plan) => plan.state).join()) result('plans', '');
    if (JSON.stringify(state.imports) !== before && state.scene === 'history') render();
  }
  const peers = (text) => new Set(String(text || '').split(',').map((x) => x.trim()).filter(Boolean));
  function resetScopeDraft() {
    if (!state.policy) return;
    state.scope = {mode: state.policy.scope || 'all', excluded: peers(state.policy.blocked_peers),
      selected: peers(state.policy.selected_peers), added: new Map(), dirty: false};
  }

  // ---- shared pieces ------------------------------------------------------
  const pressed = (value, current) => `aria-pressed="${value === current}"`;
  function heartbeat() {
    const s = state.status;
    if (state.statusError) return ['tone-bad', 'Status unavailable'];
    if (!s) return ['tone-quiet', 'Reading status…'];
    const c = s.companion_category;
    if (c === 'running') return ['tone-good', 'Running'];
    if (c === 'login_required') return ['tone-warn', 'Login required'];
    if (c === 'provider_retry') return ['tone-warn', 'Waiting for Telegram'];
    if (['ingress_failed', 'login_failed', 'transport_error', 'companion_error', 'session_mismatch', 'config_invalid', 'dependency_missing'].includes(c))
      return ['tone-bad', 'Stopped · ' + c.replace(/_/g, ' ')];
    return ['tone-quiet', s.configured ? 'Not running' : 'Not set up'];
  }
  function identity() {
    const profile = state.status?.profile || {};
    // The bot's own profile lives in the Telegram Bot connector; never show this account's name for it.
    const name = (state.method === 'account' && [profile.first_name, profile.last_name].filter(Boolean).join(' ')) || 'Ouroboros · AI';
    const line = state.method === 'bot' ? 'Telegram bot · the separate Telegram Bot connector'
      : 'Dedicated account · ' + (profile.username ? '@' + profile.username : state.status?.account_id ? 'account ' + state.status.account_id : 'not logged in');
    const [tone, word] = heartbeat();
    return `<div class="profile"><div class="avatar" aria-hidden="true">◌</div><div class="who"><strong>${esc(name)}</strong><small>${esc(line)}</small></div>
      ${state.method === 'account' ? `<span class="mark ${tone}" id="heartbeat">${esc(word)}</span>` : ''}</div>`;
  }
  function statusLines() {
    const s = state.status;
    if (state.statusError) return state.statusError;
    if (!s) return 'Reading account status…';
    const lines = [s.message || ''];
    if (s.login_attempt?.status) lines.push(`Last login request: ${s.login_attempt.status}${s.login_attempt.reason ? ' · ' + s.login_attempt.reason : ''}`);
    if (s.code_delivery?.type) lines.push(`Code sent by ${s.code_delivery.type}; next method ${s.code_delivery.next_type || 'not reported'}. A request is not proof the code arrived.`);
    if (s.last_companion_error?.error_type) lines.push(`Earlier companion stop: ${s.last_companion_error.error_type} (${s.last_companion_error.at || 'time unrecorded'})`);
    return lines.filter(Boolean).join('\n');
  }
  function waitLine() {
    if (!waitKnown) return 'Telegram wait: not checked.';
    const seconds = Math.max(0, Math.ceil((waitUntil - Date.now()) / 1000));
    if (seconds > 0) return `Telegram asked to wait ${seconds}s more (${waitReason}). The queue waits; nothing is resent.`;
    return waitReason ? 'The last Telegram wait has passed. Elapsed time is not delivery.' : 'No Telegram wait in the last status read.';
  }
  function queueLine() {
    const q = state.status?.participant_queue;
    if (!q || q.status === 'not_initialized') return 'Queue not started yet.';
    const jobs = q.jobs || {};
    const parts = [`${jobs.queued || 0} queued`, `${jobs.held || 0} held`, `${jobs.unknown || 0} unknown outcome`];
    if (q.inbound_retrying) parts.push(`${q.inbound_retrying} incoming waiting for a safe retry`);
    if (q.inbox?.held) parts.push(`${q.inbox.held} incoming held`);
    return 'Queue: ' + parts.join(' · ') + '.';
  }
  function placeChip() {
    const place = state.status?.place || {};
    const words = {active: ['tone-good', 'Active'], silent: ['tone-warn', 'Silent'], paused: ['tone-warn', 'Paused']};
    const [tone, word] = words[place.state] || ['tone-quiet', place.state === 'unavailable' ? 'Not connected or unreadable' : 'Not checked yet'];
    const when = place.at ? new Date(place.at) : null;
    return `<span class="mark ${tone}">${esc(word)}</span>${when && !isNaN(when) ? ` <span class="meta">as of ${esc(when.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}))}</span>` : ''}`;
  }
  function paintStatus() {
    if (disposed) return;
    const lines = $('#status-lines'); if (lines) lines.textContent = statusLines();
    const beat = $('#heartbeat'); if (beat) { const [tone, word] = heartbeat(); beat.className = 'mark ' + tone; beat.textContent = word; }
    const wait = $('#wait-line'); if (wait) wait.textContent = waitLine();
    const queue = $('#queue-line'); if (queue) queue.textContent = queueLine();
    const chip = $('#place-chip'); if (chip) chip.innerHTML = placeChip();
  }

  // ---- first step -----------------------------------------------------------
  // A fresh install starts in legacy mode, which serves only chats enrolled for that older
  // transport. Choosing Free participant stays the owner's own step; nothing switches by itself.
  function firstStep() {
    // The choice stays reported only while it is the saved mode; Legacy saved again brings the step back.
    const saved = state.firstStepSaved && state.policy?.mode === 'participant';
    if (!saved && !(state.policy?.mode === 'legacy' && state.status?.legacy_enrolled === false)) return '';
    return `<section class="object first-step"><h2>${saved ? 'Free participant chosen' : 'First step: choose Free participant'}</h2>
      ${saved ? '' : `<p class="secondary">This install is in Legacy scoped transport mode, which serves only chats enrolled for that older transport, and none are enrolled. Until you choose Free participant, the Telegram tools refuse and the account hears nothing.</p>
        <div class="actionbar"><button type="button" data-action="choose-participant" ${busy('first-step') ? 'disabled' : ''}>Choose Free participant</button></div>`}
      ${resultLine('first-step')}<p class="hint">The mode starts after the skill is turned off and on. Outgoing actions keep their own switch under Participation.</p></section>`;
  }

  // ---- scene 1: connect ---------------------------------------------------
  function connectScene() {
    const bot = state.method === 'bot';
    const d = state.drafts;
    const loaded = !!state.setup;
    const how = `<section class="object"><h2>How to connect</h2>
      <div class="method-picker" role="group" aria-label="Connection type">
        <button type="button" data-action="method" data-value="bot" ${pressed('bot', state.method)}>Telegram bot<small>Visible bot label · BotFather token</small></button>
        <button type="button" data-action="method" data-value="account" ${pressed('account', state.method)}>Dedicated account<small>Own AI profile · phone number</small></button>
      </div>${identity()}
      ${bot ? `<p class="secondary">Good for private chats people open with the bot and for groups that add it. A bot can also take part on its own initiative.</p>
        <div class="tray"><p>The bot is a separate connector, the Telegram Bot skill. Put the BotFather token into that skill's settings (<b>TELEGRAM_PUBLIC_BOT_TOKEN</b>), then connect it in Places. This widget never asks for a bot token.</p></div>`
      : `<p class="secondary">An account created for Ouroboros. Its name and bio should say openly that it is an AI; nobody's personal account is used.</p>
        <div class="fields">
          <label class="field"><span>Telegram API ID</span><input data-draft="api_id" inputmode="numeric" autocomplete="off" value="${esc(d.api_id)}" ${loaded ? '' : 'disabled'}></label>
          <label class="field"><span>API hash</span><input data-draft="api_hash" type="password" autocomplete="off" value="${esc(d.api_hash)}" ${loaded ? '' : 'disabled'}></label>
          <label class="field"><span>Dedicated account phone</span><input data-draft="phone" type="password" autocomplete="off" placeholder="+country code…" value="${esc(d.phone)}" ${loaded ? '' : 'disabled'}></label>
        </div>
        <p class="hint">${!loaded && state.setupError ? `<span class="warning">${esc(state.setupError)} Check status retries this read.</span>` : 'API ID and hash come from my.telegram.org. Blank secret fields keep the saved values, which are never shown again. Saving does not log in.'}</p>
        <div class="actionbar"><button type="button" data-action="save-setup" ${loaded && !busy('setup') ? '' : 'disabled'}>${busy('setup') ? 'Saving…' : 'Save account setup'}</button></div>${resultLine('setup')}
        <h3 style="margin-top:20px">Log in</h3>
        <div class="actionbar"><button type="button" data-action="code" data-value="setup/request_code" ${busy('code') ? 'disabled' : ''}>Request login code</button>
          <button type="button" data-action="code" data-value="setup/resend_code" ${busy('code') ? 'disabled' : ''}>Resend with Telegram's next method</button></div>${resultLine('code')}
        <div class="fields">
          <label class="field"><span>Newest login code</span><input data-draft="code" type="password" inputmode="numeric" autocomplete="off" value="${esc(d.code)}"></label>
          <label class="field"><span>2FA password, only if asked</span><input data-draft="password" type="password" autocomplete="off" value="${esc(d.password)}"></label>
        </div>
        <div class="actionbar"><button type="button" data-action="login" ${busy('login') ? 'disabled' : ''}>Submit login input</button></div>${resultLine('login')}
        <p class="hint">The owner enters the code and 2FA password. Each button makes one request; status reads never contact Telegram. Never copy another person's session.</p>`}
    </section>`;
    const changes = `<section class="object"><h2>What changes for the people I talk to</h2>
      <div class="capline"><strong>How I appear</strong><span class="secondary">${bot ? 'Telegram shows a bot label.' : 'Ordinary accounts have no AI label in Telegram, so my profile and my introduction should say I am an AI. The widget does not check the profile.'}</span></div>
      <div class="capline"><strong>What reaches me</strong><span class="secondary">${bot ? 'Messages the bot can see. In groups this depends on privacy mode and rights; it is not a full Telegram archive. The Bot API cannot read earlier history.' : 'Supported events from the chats this account can see, minus the ones you exclude. Earlier history is read separately.'}</span></div>
      <div class="capline"><strong>First contact</strong><span class="secondary">${bot ? 'A bot cannot start a private chat with someone who never opened one.' : 'Technically possible. It follows the participation notes and Telegram\'s anti-spam limits.'}</span></div>
      <div class="tray">${bot ? 'Private chats with the bot reach Places as direct messages; the place\'s wakes decide whether they wake me.' : 'Connecting does not let me write on the owner\'s behalf or moderate any chat.'}</div>
      <p class="meta">Both connectors can run side by side. Either way it is one Ouroboros with the same Places settings.</p>
    </section>`;
    const status = bot ? '' : `<section class="object wide"><h2>Account status</h2><p class="lines" id="status-lines" role="status">${esc(statusLines())}</p>
      <p class="meta" id="wait-line">${esc(waitLine())}</p><div class="actionbar"><button type="button" data-action="refresh-status">Check status</button></div></section>`;
    return `<div class="grid">${how}${changes}${status}</div>`;
  }

  // ---- scene 2: what I hear ---------------------------------------------------
  function listedChats() {
    const rows = (state.chats?.chats || []).map((c) => ({...c}));
    for (const [id, title] of state.scope?.added || []) if (!rows.some((c) => c.id === id)) rows.push({id, title, kind: '', added: true});
    return rows;
  }
  const receivingDraft = (id) => state.scope.mode === 'all' ? !state.scope.excluded.has(id) : state.scope.selected.has(id) && !state.scope.excluded.has(id);
  function chatLabel(c) {
    const parts = [c.added ? 'Not listed yet · added by ID' : KIND[c.kind] || 'Chat'];
    if (c.members) parts.push(`${c.members} members`);
    if (c.username) parts.push('@' + c.username);
    return parts.join(' · ');
  }
  function scopeScene() {
    if (!state.scope) return `<div class="grid"><section class="object"><h2>What the dedicated account hears</h2><p class="${state.policyError ? 'warning' : 'secondary'}">${esc(state.policyError || 'Reading receiving scope…')}</p>
      <div class="actionbar"><button type="button" data-action="reload">Try again</button></div></section></div>`;
    const all = state.scope.mode === 'all';
    const rows = listedChats();
    const receiving = rows.filter((c) => receivingDraft(c.id)).length;
    const refresh = state.chats?.refresh || {};
    const refreshed = refresh.pending ? 'Refresh requested; the companion reads Telegram once while it runs.'
      : refresh.status === 'done' ? `Read from Telegram ${new Date(refresh.at).toLocaleString()} · ${refresh.count} chats${refresh.complete ? '' : ' (first 200)'}`
      : refresh.status === 'flood_wait' ? `Telegram asked to wait ${refresh.retry_after_seconds}s before reading the chat list.`
      : refresh.status === 'failed' ? `The last chat list read failed (${refresh.reason}).` : 'Chats appear here as messages arrive, or refresh the list from Telegram.';
    const list = rows.length ? rows.map((c) => `<label class="check"><span class="check-text">${esc(c.title || 'Chat ' + c.id)}<small>${esc(chatLabel(c))}</small></span>
        <input type="checkbox" data-chat="${esc(c.id)}" ${receivingDraft(c.id) ? 'checked' : ''} aria-label="Receive ${esc(c.title || c.id)}"></label>`).join('')
      : `<p class="empty">No chats listed yet.</p>`;
    const extra = all ? [...state.scope.excluded].filter((id) => !rows.some((c) => c.id === id)).length : [...state.scope.selected].filter((id) => !rows.some((c) => c.id === id)).length;
    const left = `<section class="object"><h2>What the dedicated account hears</h2>
      <div class="strip small" role="group" aria-label="Receiving scope"><button type="button" data-action="scope" data-value="all" ${pressed('all', state.scope.mode)}>All available chats</button>
        <button type="button" data-action="scope" data-value="selected" ${pressed('selected', state.scope.mode)}>Selected chats</button></div>
      <p class="hint">${all ? 'New chats are included too. Untick a chat to stop receiving it.' : 'A new chat is not received until you select it. This is the narrower option.'}</p>
      ${list}
      <div class="tray" id="scope-count">Receiving ${receiving} of ${rows.length} listed chat${rows.length === 1 ? '' : 's'}${extra ? `; ${extra} more ${all ? 'excluded' : 'selected'} by ID` : ''}.</div>
      <div class="inline"><label class="field"><span>Add a chat by numeric ID</span><input data-draft="addPeer" inputmode="numeric" placeholder="-100… or a user ID" value="${esc(state.drafts.addPeer)}"></label>
        <button type="button" data-action="add-peer">Add</button></div>
      <div class="actionbar"><button type="button" data-action="save-scope" ${state.scope.dirty && !busy('scope') ? '' : 'disabled'}>${busy('scope') ? 'Saving…' : 'Save what I hear'}</button>
        <button type="button" class="quiet" data-action="refresh-chats" ${busy('chats') || refresh.pending ? 'disabled' : ''}>Refresh chat list from Telegram</button></div>
      ${resultLine('scope')}<p class="meta">${esc(refreshed)}</p>
    </section>`;
    const right = `<section class="object"><h2>Receiving is not replying to every message</h2>
      <div class="arrowline" aria-label="What happens to a received message"><b>Received</b><span aria-hidden="true">→</span><b>Stored by the host</b><span aria-hidden="true">→</span><b>Available to attention</b></div>
      <p class="secondary">Wake events in Places can call me at once. Everything else stays readable for later; a message does not turn into a paid reply by itself.</p>
      <div class="tray"><strong>What about messages from before?</strong><p class="secondary">${IMPORT_BLOCKED[state.imports?.reason]
        ? `New messages and the old archive are separate. ${IMPORT_BLOCKED[state.imports.reason].scope} I can still read earlier messages in a chat when a conversation needs them.`
        : state.imports?.host_supports_import
        ? 'New messages and the old archive are separate. On the History tab you can import chosen earlier messages once; they wake no one and get no replies. I can also read earlier messages when a conversation needs them.'
        : 'New messages and the old archive are separate. Importing earlier history needs an Ouroboros version that can record it, and this one has not confirmed that; a saved plan reads nothing from Telegram. I can still read earlier messages in a chat when a conversation needs them.'}</p>
        <button type="button" class="quiet" data-action="go" data-value="history">Earlier history →</button></div>
      <p class="meta">Silent and Paused are not storage filters; not receiving a chat is set only here. Excluded chats are also never read or written.</p>
    </section>`;
    return `<div class="grid">${left}${right}</div>`;
  }

  // ---- scene 3: participation -------------------------------------------------
  // The form edits a draft. Until Save, the tray describes the saved switch the companion uses.
  const CONTROLS = ['mode', 'outgoing', 'expected_account_id', 'interval'];
  const controlDraft = () => ({...state.policy, ...state.controls});
  const changedControls = () => state.policy ? CONTROLS.filter((k) => String(controlDraft()[k] ?? '') !== String(state.policy[k] ?? '')) : [];
  function paintControls() {
    if (disposed) return;
    const changed = changedControls();
    const save = $('[data-action="save-controls"]'); if (save) save.disabled = !changed.length || busy('controls');
    const note = $('#controls-unsaved'); if (note) note.hidden = !changed.some((k) => k !== 'outgoing');
  }
  function participationScene() {
    const p = state.policy;
    if (!p) return `<div class="grid"><section class="object"><h2>Participation</h2><p class="${state.policyError ? 'warning' : 'secondary'}">${esc(state.policyError || 'Reading participation controls…')}</p>
      <div class="actionbar"><button type="button" data-action="reload">Try again</button></div></section></div>`;
    const d = controlDraft();
    const changed = changedControls();
    const saved = p.outgoing === 'true', outgoing = d.outgoing === 'true';
    const held = state.status?.participant_queue?.jobs?.held || 0;
    const left = `<section class="object"><h2>Participation</h2>
      <label class="check"><span class="check-text">Outgoing actions<small>Messages, reactions, joins, profile and admin changes</small></span><input type="checkbox" data-control="outgoing" ${outgoing ? 'checked' : ''}></label>
      <div class="tray">${saved ? 'On. Every action still checks the place state and this account\'s Telegram rights first.' : 'Off. I cannot start Telegram actions; ones already queued wait as held until this is on again.'}${outgoing === saved ? '' : ` <strong>Not saved yet.</strong> Saving turns outgoing actions ${outgoing ? 'on' : 'off'}.`}</div>
      <p class="meta" id="queue-line">${esc(queueLine())}</p><p class="meta" id="wait-line">${esc(waitLine())}</p>
      ${held ? `<div class="actionbar"><button type="button" class="quiet" data-action="discard-held" ${busy('held') ? 'disabled' : ''}>Discard ${held} held action${held === 1 ? '' : 's'}</button></div>
        <p class="hint">Held actions were never attempted. When the place is Active and outgoing actions are on, they run once in order.</p>` : ''}${resultLine('held')}
      <details data-section="identity" ${state.open.has('identity') ? 'open' : ''}><summary>Identity, pacing and transport mode</summary><div class="fields">
        <label class="field"><span>Expected account ID</span><input data-control="expected_account_id" inputmode="numeric" value="${esc(d.expected_account_id)}"><small>Optional guard against a wrong session.</small></label>
        <label class="field"><span>Minimum seconds between actions</span><input data-control="interval" type="number" min="1" max="60" value="${esc(d.interval)}"></label>
        <label class="field"><span>Transport mode</span><select data-control="mode"><option value="participant" ${d.mode === 'participant' ? 'selected' : ''}>Free participant</option><option value="legacy" ${d.mode === 'legacy' ? 'selected' : ''}>Legacy scoped transport</option></select><small>A changed mode starts after the skill is turned off and on.</small></label>
      </div></details>
      <div class="actionbar"><button type="button" data-action="save-controls" ${changed.length && !busy('controls') ? '' : 'disabled'}>${busy('controls') ? 'Saving…' : 'Save participation'}</button></div>
      <p class="hint" id="controls-unsaved" ${changed.some((k) => k !== 'outgoing') ? '' : 'hidden'}>Changed settings are not saved yet.</p>${resultLine('controls')}
    </section>`;
    const right = `<section class="object"><h2>How I behave is set in Places</h2>
      <div class="row"><div>Telegram place<span class="meta">Strictest state of this account's places</span></div><span id="place-chip">${placeChip()}</span></div>
      <p class="secondary">Open Settings → Behavior → Consciousness → Places → Telegram. Connect the verified account and leave the conversation empty for the whole account. Active, Silent, Paused, wakes and notes live there.</p>
      <label class="field"><span>Suggested notes for the place</span><textarea data-draft="note" class="note">${esc(state.drafts.note)}</textarea>
        <small>Ordinary owner instructions for me. There is no separate permission system here. Paste them into the place's notes and adjust.</small></label>
      <div class="actionbar"><button type="button" data-action="copy-note">Copy notes</button></div>${resultLine('note')}
      <details data-section="wakes" ${state.open.has('wakes') ? 'open' : ''}><summary class="secondary">Wakes</summary><p class="meta">Suggested: direct message, mention, reply to me. Attention stays in Places and is not duplicated by this connector.</p></details>
    </section>`;
    return `<div class="grid">${left}${right}</div>`;
  }

  // ---- scene 4: history ------------------------------------------------------
  // Why Start and Resume run nothing, as the import route names it; saving a plan, Pause and Cancel stay available.
  // Only a companion started in Free participant mode runs an import, and a saved mode starts with the skill.
  const NEEDS_PARTICIPANT = 'Importing chosen earlier messages once needs Free participant mode, which starts after the skill is turned off and on.';
  const IMPORT_BLOCKED = {
    participant_mode_required: {scope: NEEDS_PARTICIPANT, saved: 'needs Free participant mode', started: 'nothing is read in Legacy scoped transport mode',
      tray: 'Not available in Legacy scoped transport mode, which has no history import. Choose Free participant under Participation; the mode starts after the skill is turned off and on. Until then you can save plans and pause or cancel them; nothing is read from Telegram.'},
    restart_required: {scope: NEEDS_PARTICIPANT, saved: 'Start needs the skill turned off and on', started: 'nothing is read until the skill is turned off and on',
      tray: 'Not available yet: Free participant mode is saved but has not started. It starts after the skill is turned off and on. Until then you can save plans and pause or cancel them; nothing is read from Telegram.'},
    host_source_order_missing: {saved: 'cannot start with this Ouroboros',
      scope: 'Importing earlier history needs an Ouroboros version that keeps messages sent within the same second in their order, and this one does not; a saved plan reads nothing from Telegram.',
      tray: 'Not available with this Ouroboros version: it records imported history but cannot keep messages sent within the same second in their order, so an import could store them out of order. Update Ouroboros. You can save a plan; nothing is read from Telegram for it.'}};
  // Plan states and reasons come from the companion's journal; the words below only name them.
  const IMPORT_PAUSES = {owner: 'by you', interrupted: 'the companion restarted; resume when ready',
    host_support_missing: 'this Ouroboros cannot record imported history', host_auth_blocked: 'Ouroboros refused this connector; check the skill',
    host_source_order_missing: 'this Ouroboros cannot keep messages sent within the same second in their order; update it, then resume',
    presence_auth_blocked: 'Ouroboros refused this connector; check the skill', place_not_connected: 'connect Telegram in Places, then resume',
    place_ambiguous: 'more than one Telegram place matches; fix Places, then resume', places_unreadable: 'Places could not be read; resume later',
    presence_binding_wrong_transport: 'the place belongs to another connector', presence_history_invalid: 'Ouroboros refused the request as malformed',
    host_answer_invalid: 'Ouroboros answered in an unexpected form', host_refused: 'Ouroboros refused the import', import_error: 'it stopped on an internal error'};
  const IMPORT_WAITS = {host_unreachable: 'Ouroboros is not answering', host_unconfirmed: 'Ouroboros did not confirm some records; they go again unchanged',
    host_answer_invalid: 'Ouroboros answered in an unexpected form', presence_rate_limited: 'Ouroboros asked to slow down',
    presence_capacity_full: 'Ouroboros is busy', presence_history_importing: 'Ouroboros is still loading saved history',
    presence_history_unavailable: 'Ouroboros could not record history just now', presence_auth_unavailable: 'Ouroboros could not check the connector',
    host_retry: 'Ouroboros asked to retry', telegram_flood_wait: 'Telegram asked to wait', telegram_unavailable: 'Telegram is not answering',
    live_capture_settling: 'new messages in a chat are still arriving', places_unavailable: 'Places could not be read just now'};
  const CHAT_REASONS = {peer_excluded: 'excluded from receiving', peer_not_selected: 'not selected for receiving', outside_receiving_scope: 'no longer received',
    saved_messages: 'Saved Messages are not imported', chat_unavailable: 'unknown to this session; refresh the chat list', outside_place: 'outside the Telegram place'};
  const HELD = {conflict: 'already recorded with other content', invalid: 'refused by Ouroboros as malformed',
    withheld: 'chat no longer received', rejected: 'outside the Telegram place'};
  // Read but left out while reading; Host-held messages are listed by ID instead. Plans read by earlier
  // versions may count messages they left to new-message delivery; nothing says those reached the conversation.
  const LEFT_OUT = {service: 'service', topic_unresolved: 'without a known forum topic', invalid_source: 'unreadable',
    live_pending: 'left to new-message delivery when read'};
  // A held-back message waits for the owner's Resume, which checks it again.
  const heldWords = (h) => h.state !== 'residual' ? HELD[h.state] || h.state
    : h.reason === 'live_pending' ? 'held back: it was still arriving as a new message when this chat was read; Resume checks again'
    : h.reason === 'receipt_unreadable' ? 'own message held back: the delivery receipts could not be read; Resume checks again'
    : h.receipt === 'refused' ? 'own message held back: its delivery receipt was refused; Resume sends the saved copy'
    : 'own message held back: its delivery receipt is not confirmed yet; Resume checks again';
  // A Resume sends a held-back message once nothing else will deliver it; the line says what became of it.
  const RELEASED = {receipt_refused: 'its delivery receipt had been refused', receipt_absent: 'its delivery receipt was no longer there',
    live_held: 'it never reached the conversation as a new message', live_absent: 'it never reached the conversation as a new message'};
  function releasedWords(r, running) {
    const outcome = r.state === 'recorded' ? 'imported on your Resume'
      : r.state === 'duplicate' ? 'already in the conversation when your Resume sent it'
      : r.state === 'pending' ? 'sent on your Resume, ' + (running ? 'waiting for Ouroboros to confirm' : 'not confirmed by Ouroboros')
      : 'sent on your Resume but not imported: ' + (HELD[r.state] || r.state);
    return `${outcome} (${RELEASED[r.released] || 'its delivery receipt no longer stood for a delivery'})`;
  }
  // Already in the conversation: admitted by the Host from live intake, or delivered by this connector.
  const ALREADY = ['live_captured', 'delivered'];
  const isoDay = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  const chatName = (id) => (state.chats?.chats || []).find((c) => c.id === String(id))?.title || 'Chat ' + id;
  function planLine(plan) {
    const spec = plan.spec || {};
    const started = (plan.chats || []).length;
    const leftOut = new Set((spec.exclude || []).map(String));
    const chats = spec.chats === 'receiving' ? (started ? `All receiving chats (${started})` : 'All receiving chats' + (spec.exclude?.length ? `, ${spec.exclude.length} left out` : ''))
      : (spec.chats || []).filter((id) => !leftOut.has(String(id))).map(chatName).join(', ');
    const day = (value) => new Date(value).toLocaleDateString();
    return `${chats} · ${day(spec.from)} – ${day(new Date(new Date(spec.to) - 1))} · up to ${spec.max_per_chat} each`;
  }
  const sum = (values) => values.reduce((total, value) => total + (value || 0), 0);
  // Read but not imported, as [count, reason]: held by Ouroboros, or left out while reading.
  const notImported = (counts, skipped) => [...Object.keys(HELD).map((k) => [counts[k], HELD[k]]),
    ...Object.entries(skipped).filter(([k]) => !ALREADY.includes(k)).map(([k, n]) => [n, LEFT_OUT[k] || k.replace(/_/g, ' ')])]
    .filter(([n]) => n);
  const notImportedTotal = (plan) => sum(Object.keys(HELD).map((k) => plan.counts?.[k]))
    + sum((plan.chats || []).flatMap((chat) => notImported({}, chat.skipped || {}).map(([n]) => n)));
  function planCounts(plan) {
    const c = plan.counts || {};
    const skipped = (plan.chats || []).map((chat) => chat.skipped || {});
    const already = (c.duplicate || 0) + (c.delivered || 0) + sum(skipped.flatMap((s) => ALREADY.map((k) => s[k])));
    const left = notImportedTotal(plan);
    const parts = [];
    if (c.recorded) parts.push(`${c.recorded} recorded`);
    if (already) parts.push(`${already} already in the conversation`);
    if (c.residual) parts.push(`${c.residual} held back`);
    if (left) parts.push(`${left} not imported`);
    if (c.pending && plan.state !== 'running') parts.push(`${c.pending} not confirmed by Ouroboros`);
    return parts.join(' · ');
  }
  function planStatus(plan, blocked) {
    const chats = plan.chats || [];
    const why = IMPORT_BLOCKED[blocked];
    if (plan.state === 'saved') return ['tone-quiet', 'Saved · ' + (!blocked ? 'not started' : why ? why.saved : 'cannot start with this Ouroboros')];
    if (plan.state === 'running') {
      // Started earlier, but no companion that runs imports has started since: it stays running and reads nothing.
      if (why?.started) return ['tone-warn', 'Started · ' + why.started];
      if (plan.waiting) return ['tone-warn', 'Importing · waiting: ' + (IMPORT_WAITS[plan.waiting.reason] || plan.waiting.reason)];
      const at = chats.findIndex((c) => c.state === 'pending');
      return ['tone-good', at < 0 ? 'Importing · finishing' : `Importing · chat ${at + 1} of ${chats.length}`];
    }
    if (plan.state === 'paused') return ['tone-warn', 'Paused · ' + (IMPORT_PAUSES[plan.reason] || plan.reason || 'resume when ready')];
    if (plan.state === 'partial') {
      const held = plan.counts?.residual || 0;
      return ['tone-warn', `Partial · ${held} message${held === 1 ? '' : 's'} held back`];
    }
    if (plan.state === 'done') {
      // Every chat read is not every message imported: what was left out keeps a finished plan amber.
      const left = notImportedTotal(plan), unread = chats.filter((c) => c.state === 'skipped').length;
      const gaps = [left && `${left} not imported`, unread && `${unread} chat${unread === 1 ? '' : 's'} not read`].filter(Boolean);
      return gaps.length ? ['tone-warn', ['Done', ...gaps].join(' · ')] : ['tone-good', 'Done'];
    }
    return ['tone-quiet', 'Cancelled'];
  }
  function chatOutcome(chat, running) {
    if (chat.state === 'skipped') return CHAT_REASONS[chat.reason] || (String(chat.reason).startsWith('chat_unreadable') ? 'Telegram refused to read it' : chat.reason);
    if (chat.state === 'done') return chat.truncated ? 'stopped at the limit; older messages in these dates were not read' : 'every message in these dates read';
    return running ? 'in progress or waiting its turn' : 'not finished';
  }
  function planDetails(plan) {
    const chats = plan.chats || [];
    if (!chats.length) return '';
    const lines = chats.map((chat) => {
      const c = chat.counts || {};
      const parts = [`${chat.read} read`];
      if (c.recorded) parts.push(`${c.recorded} recorded`);
      const already = (c.duplicate || 0) + (c.delivered || 0) + sum(ALREADY.map((k) => (chat.skipped || {})[k]));
      if (already) parts.push(`${already} already in the conversation`);
      if (c.residual) parts.push(`${c.residual} held back`);
      const left = notImported(c, chat.skipped || {});
      if (left.length) parts.push(`${sum(left.map(([n]) => n))} not imported (${left.length === 1 ? left[0][1] : left.map(([n, why]) => `${n} ${why}`).join(', ')})`);
      return `<li>${esc(chatName(chat.id))}: ${esc(parts.join(' · '))} · ${esc(chatOutcome(chat, plan.state === 'running'))}</li>`;
    });
    // Every held or released message is counted; IDs beyond the listed ones are said, never dropped.
    const more = (n) => n ? [`<li>and ${n} more not listed</li>`] : [];
    const held = (plan.held || []).map((h) => `<li>${esc(chatName(h.chat))}, message ${esc(h.message_id)}: ${esc(heldWords(h))}</li>`)
      .concat(more(plan.held_omitted));
    const released = (plan.released || []).map((r) => `<li>${esc(chatName(r.chat))}, message ${esc(r.message_id)}: ${esc(releasedWords(r, plan.state === 'running'))}</li>`)
      .concat(more(plan.released_omitted));
    const key = 'plan-' + plan.id;
    return `<details data-section="${esc(key)}" ${state.open.has(key) ? 'open' : ''}><summary class="secondary">Chats${held.length || released.length ? ' and held messages' : ''}</summary>
      <ul class="meta plan-lines">${lines.join('')}</ul>${held.length ? `<ul class="meta plan-lines">${held.join('')}</ul>` : ''}${released.length ? `<ul class="meta plan-lines">${released.join('')}</ul>` : ''}</details>`;
  }
  function planActions(plan, ready) {
    const id = esc(plan.id), held = busy('plan:' + plan.id) ? 'disabled' : '';
    const button = (action, label, quiet) => `<button type="button" ${quiet ? 'class="quiet"' : ''} data-action="import-action" data-value="${action}:${id}" ${held}>${label}</button>`;
    if (plan.state === 'saved') return `<div class="strip">${ready ? button('start', 'Start', false) : ''}${button('cancel', 'Cancel', true)}</div>`;
    if (plan.state === 'running') return `<div class="strip">${button('pause', 'Pause', false)}${button('cancel', 'Cancel', true)}</div>`;
    if (plan.state === 'paused' || plan.state === 'partial') return `<div class="strip">${ready ? button('resume', 'Resume', false) : ''}${button('cancel', 'Cancel', true)}</div>`;
    return '';
  }
  function historyScene() {
    const d = state.drafts;
    const imports = state.imports;
    const blocked = imports?.reason || '';
    const ready = !!imports?.host_supports_import && !blocked;
    const availability = !imports ? (state.importsError || 'Reading import support…')
      : ready ? 'This Ouroboros can record imported history. A saved plan never starts by itself: it runs after your explicit Start, and you can pause or cancel it.'
      : IMPORT_BLOCKED[blocked] ? IMPORT_BLOCKED[blocked].tray
      : imports.host_checked_at ? 'Not available: this Ouroboros version cannot record imported history in conversations. You can save a plan; nothing is read from Telegram for it.'
      : 'Not available yet: Ouroboros support has not been confirmed (the companion checks when it starts). You can save a plan; nothing is read from Telegram for it.';
    const receivingChats = (state.chats?.chats || []).filter((c) => c.receiving);
    const picking = d.importMode === 'chosen';
    const choices = receivingChats.length ? receivingChats.map((c) => {
      const set = picking ? d.importChats : d.importExclude;
      const checked = picking ? set.has(c.id) : !set.has(c.id);
      return `<label class="check"><span class="check-text">${esc(c.title || 'Chat ' + c.id)}<small>${esc(chatLabel(c))}</small></span>
        <input type="checkbox" data-import-chat="${esc(c.id)}" ${checked ? 'checked' : ''} aria-label="Import ${esc(c.title || c.id)}"></label>`;
    }).join('') : '<p class="empty">No receiving chats listed yet. Refresh the chat list on What I hear.</p>';
    const plans = imports?.plans || [];
    const rows = plans.map((plan) => {
      const [tone, word] = planStatus(plan, blocked);
      const counts = planCounts(plan);
      const hint = plan.state === 'partial' ? 'Resume checks each held message again: one that has reached the conversation since is skipped, one still on its way stays held, and for the others the copy saved when the chat was read is sent.' : '';
      return `<div class="plan" data-plan="${esc(plan.id)}"><div class="row"><div>${esc(planLine(plan))}<span class="mark ${tone} plan-state">${esc(word)}</span>
        ${counts ? `<span class="meta plan-counts">${esc(counts)}</span>` : ''}${hint ? `<span class="meta plan-hint">${esc(hint)}</span>` : ''}</div>${planActions(plan, ready)}</div>${planDetails(plan)}</div>`;
    }).join('');
    // The route lists the newest plans and every running, paused or partial one; it counts the rest.
    const older = imports?.older_plans || 0;
    const unlisted = older ? `<p class="meta" id="older-plans">${older} older saved, done or cancelled plan${older === 1 ? ' is' : 's are'} not listed.</p>` : '';
    const left = `<section class="object"><h2>Import earlier history</h2>
      <div class="tray"><p class="${ready ? 'secondary' : 'warning'}" id="import-availability">${esc(availability)}</p></div>
      <div class="strip small" role="group" aria-label="Chats to import"><button type="button" data-action="import-mode" data-value="receiving" ${pressed('receiving', d.importMode)}>All receiving chats</button>
        <button type="button" data-action="import-mode" data-value="chosen" ${pressed('chosen', d.importMode)}>Chosen chats</button></div>
      <p class="hint">${picking ? 'Tick the chats to import.' : 'Untick a chat to leave it out. Chats you do not receive are never imported.'}</p>
      ${choices}
      <div class="fields"><label class="field"><span>From</span><input type="date" data-draft="importFrom" value="${esc(d.importFrom)}"></label>
        <label class="field"><span>To, inclusive</span><input type="date" data-draft="importTo" value="${esc(d.importTo)}"></label>
        <label class="field"><span>Messages per chat, at most</span><input type="number" min="1" max="5000" data-draft="importCap" value="${esc(d.importCap)}"></label></div>
      <div class="actionbar"><button type="button" data-action="save-import" ${busy('import') || !imports ? 'disabled' : ''}>${busy('import') ? 'Saving…' : 'Save import plan'}</button></div>${resultLine('import')}
      ${plans.length ? `<h3 style="margin-top:20px">Plans</h3>${rows}${unlisted}${resultLine('plans')}` : ''}
    </section>`;
    const right = `<section class="object"><h2>What an import does</h2>
      <p class="secondary">Old messages go into the conversation history with their authors and dates. They do not wake me, and nobody gets a reply to them.</p>
      <div class="capline"><strong>Telegram bot</strong><span class="secondary">The Bot API cannot read earlier chat history. A bot only receives messages that arrive while it runs.</span></div>
      <div class="capline"><strong>Dedicated account</strong><span class="secondary">Reads the history this account can see, newest first, within the chosen dates and the per-chat limit. Media is described, not downloaded. Its own earlier messages are recorded as sent from this account; messages already in the conversation are skipped.</span></div>
      <div class="capline"><strong>Where to read it</strong><span class="secondary">Ouroboros versions with a Conversations view show the history the host recorded under Places → Telegram → Conversations; other versions do not have it. It is not a browser of all of Telegram.</span></div>
    </section>`;
    return `<div class="grid">${left}${right}</div>`;
  }

  // ---- render and actions -----------------------------------------------------
  function render() {
    if (disposed) return;
    // Open sections, the focused field or button and a field's caret survive a re-render, including a poll's.
    for (const node of root.querySelectorAll('details[data-section]')) node.open ? state.open.add(node.dataset.section) : state.open.delete(node.dataset.section);
    const focused = document.activeElement && root.contains(document.activeElement) ? document.activeElement : null;
    const key = focused && (focused.dataset.draft || focused.dataset.control || focused.dataset.chat || focused.dataset.importChat);
    const button = focused?.matches('button[data-action]') ? [focused.dataset.action, focused.dataset.value ?? ''] : null;
    let caret = null;
    try { if (typeof focused?.selectionStart === 'number') caret = [focused.selectionStart, focused.selectionEnd]; } catch { caret = null; }
    const strip = `<nav class="strip" aria-label="Telegram connector">${SCENES.map(([id, label]) => `<button type="button" data-action="go" data-value="${id}" ${pressed(id, state.scene)}>${label}</button>`).join('')}</nav>`;
    const scene = {connect: connectScene, scope: scopeScene, participation: participationScene, history: historyScene}[state.scene || 'connect']();
    root.innerHTML = `<div class="top">${strip}</div>${firstStep()}${scene}`;
    Object.keys(state.results).forEach(paintResult);
    const note = $('textarea[data-draft="note"]');
    if (note) note.style.height = Math.max(232, note.scrollHeight + 2) + 'px';
    const next = key && root.querySelector(`[data-draft="${key}"],[data-control="${key}"],[data-chat="${key}"],[data-import-chat="${key}"]`);
    if (next) { next.focus(); if (caret) try { next.setSelectionRange(...caret); } catch { /* type without a caret */ } }
    else if (button) [...root.querySelectorAll('button[data-action]')].find((b) => b.dataset.action === button[0] && (b.dataset.value ?? '') === button[1])?.focus();
  }
  function setBusy(key, value) { value ? state.busy.add(key) : state.busy.delete(key); render(); }
  function controlsBody() {
    const draft = controlDraft();
    return Object.fromEntries(CONTROLS.map((name) => [name, String(draft[name] ?? '')]));
  }
  const actions = {
    go(value) { state.scene = value; render(); if (value === 'history') loadImports().then(render); },
    method(value) { state.method = value; render(); },
    async reload() { await Promise.all([loadPolicy(), loadChats(), loadImports()]); render(); },
    async 'refresh-status'() { await Promise.all([loadStatus(), state.setup ? null : loadSetup()]); render(); },
    async 'save-setup'() {
      const d = state.drafts; setBusy('setup', true); result('setup', 'Saving privately…');
      try {
        const value = await call('setup/config', {api_id: d.api_id, api_hash: d.api_hash, phone: d.phone});
        d.api_hash = ''; d.phone = ''; result('setup', value.message);
      } catch (error) { result('setup', error.message, true); }
      finally { setBusy('setup', false); }
    },
    async code(path) {
      setBusy('code', true); result('code', 'Submitting one request…');
      try { result('code', (await call(path, {})).message || 'Request accepted locally; check status and Telegram.'); }
      catch (error) { result('code', error.message, true); }
      finally { setBusy('code', false); loadStatus(); }
    },
    async login() {
      const d = state.drafts; setBusy('login', true);
      try { const value = await call('setup/login', {code: d.code, password: d.password}); d.code = ''; d.password = ''; result('login', value.message + ' Check status for the actual login result.'); }
      catch (error) { result('login', error.message, true); }
      finally { setBusy('login', false); }
    },
    scope(value) { state.scope.mode = value; state.scope.dirty = true; render(); },
    'add-peer'() {
      const id = state.drafts.addPeer.trim();
      if (!/^-?[1-9][0-9]{0,18}$/.test(id)) { result('scope', 'Enter a numeric chat or user ID, for example -1001234567890.', true); render(); return; }
      if (!listedChats().some((c) => c.id === id)) state.scope.added.set(id, '');
      state.scope.mode === 'all' ? state.scope.excluded.delete(id) : state.scope.selected.add(id);
      state.scope.dirty = true; state.drafts.addPeer = ''; result('scope', ''); render();
    },
    async 'save-scope'() {
      setBusy('scope', true); result('scope', 'Saving…');
      try {
        const value = await call('participant/policy', {scope: state.scope.mode, blocked_peers: [...state.scope.excluded].join(','), selected_peers: [...state.scope.selected].join(',')});
        state.scope.dirty = false;
        await Promise.all([loadPolicy(), loadChats()]);
        result('scope', value.message);
      } catch (error) { result('scope', error.message, true); }
      finally { setBusy('scope', false); }
    },
    async 'choose-participant'() {
      setBusy('first-step', true); result('first-step', 'Saving…');
      try {
        const value = await call('participant/policy', {mode: 'participant'});
        state.firstStepSaved = true; state.policy.mode = 'participant';  // Saved as sent; the reload confirms it.
        await loadPolicy(); savedMode = state.policy?.mode;
        result('first-step', value.message);
      } catch (error) { result('first-step', error.message, true); }
      finally { setBusy('first-step', false); }
    },
    async 'refresh-chats'() {
      setBusy('chats', true);
      try { await call('participant/chats', {refresh: true}); result('scope', ''); await loadChats(); }
      catch (error) { result('scope', error.message, true); }
      finally { setBusy('chats', false); }
    },
    async 'save-controls'() {
      const body = controlsBody(); const previous = savedMode; setBusy('controls', true); result('controls', 'Saving…');
      try {
        const value = await call('participant/policy', body);
        Object.assign(state.policy, body); state.controls = {};  // Saved as sent; the reload confirms it.
        await loadPolicy(); savedMode = state.policy?.mode;
        if (savedMode !== 'participant') result('first-step', '');  // An earlier choice's receipt no longer holds.
        result('controls', value.message + (body.mode !== previous ? ' The new transport mode starts after the skill is turned off and on.' : ''));
        loadStatus();
      } catch (error) { result('controls', error.message, true); }
      finally { setBusy('controls', false); }
    },
    async 'discard-held'() {
      setBusy('held', true);
      try { result('held', (await call('participant/held', {action: 'discard'})).message); await loadStatus(); }
      catch (error) { result('held', error.message, true); }
      finally { setBusy('held', false); }
    },
    async 'copy-note'() {
      try { await navigator.clipboard.writeText(state.drafts.note); result('note', 'Copied. Paste it into the Telegram place\'s notes.'); }
      catch (error) { result('note', 'Copy is not available here; select the text and copy it.', true); }
    },
    'import-mode'(value) { state.drafts.importMode = value; render(); },
    async 'save-import'() {
      const d = state.drafts;
      const start = d.importFrom ? new Date(d.importFrom + 'T00:00') : null;
      const end = d.importTo ? new Date(d.importTo + 'T00:00') : null;
      if (!start || !end || isNaN(start) || isNaN(end)) { result('import', 'Choose both dates.', true); return; }
      end.setDate(end.getDate() + 1);
      const body = {action: 'plan', from: start.toISOString(), to: end.toISOString(), max_per_chat: Number(d.importCap),
        chats: d.importMode === 'chosen' ? [...d.importChats] : 'receiving', exclude: d.importMode === 'chosen' ? [] : [...d.importExclude]};
      setBusy('import', true); result('import', 'Saving…');
      try { result('import', (await call('participant/import', body)).message); await loadImports(); }
      catch (error) { result('import', error.message, true); }
      finally { setBusy('import', false); }
    },
    async 'import-action'(value) {
      const [action, id] = String(value).split(':');
      setBusy('plan:' + id, true); result('plans', ''); result('import', '');
      try { result('plans', (await call('participant/import', {action, id})).message); await loadImports(); }
      catch (error) { result('plans', error.message, true); }
      finally { setBusy('plan:' + id, false); }
    },
  };
  // Module frames lack allow-forms: every action is a button handled here, never a form submit.
  on(root, 'click', (event) => {
    const button = event.target.closest('button[data-action]');
    if (!button || !root.contains(button) || button.disabled) return;
    event.preventDefault();
    const action = actions[button.dataset.action];
    if (action) Promise.resolve(action(button.dataset.value)).catch((error) => console.error(error));
  });
  on(root, 'input', (event) => {
    const node = event.target;
    if (node.dataset.draft) state.drafts[node.dataset.draft] = node.value;
    if (node.dataset.control && node.type !== 'checkbox' && state.policy) { state.controls[node.dataset.control] = node.value; paintControls(); }
  });
  on(root, 'change', (event) => {
    const node = event.target;
    if (node.dataset.chat && state.scope) {
      const id = node.dataset.chat;
      if (state.scope.mode === 'all') node.checked ? state.scope.excluded.delete(id) : state.scope.excluded.add(id);
      else { node.checked ? state.scope.selected.add(id) : state.scope.selected.delete(id); if (node.checked) state.scope.excluded.delete(id); }
      state.scope.dirty = true; render();
    } else if (node.dataset.importChat) {
      const id = node.dataset.importChat; const d = state.drafts;
      if (d.importMode === 'chosen') node.checked ? d.importChats.add(id) : d.importChats.delete(id);
      else node.checked ? d.importExclude.delete(id) : d.importExclude.add(id);
    } else if (node.dataset.control === 'outgoing' && state.policy) {
      state.controls.outgoing = String(node.checked); render();
    }
  });

  const today = new Date(); const monthAgo = new Date(today); monthAgo.setDate(today.getDate() - 30);
  state.drafts.importFrom = isoDay(monthAgo); state.drafts.importTo = isoDay(today);
  const statusTimer = setInterval(loadStatus, 20000); cleanups.push(() => clearInterval(statusTimer));
  const refreshTimer = setInterval(followRefresh, 5000); cleanups.push(() => clearInterval(refreshTimer));
  const importTimer = setInterval(followImports, 4000); cleanups.push(() => clearInterval(importTimer));
  const waitTimer = setInterval(() => { const node = $('#wait-line'); if (node && !disposed) node.textContent = waitLine(); }, 1000);
  cleanups.push(() => clearInterval(waitTimer));
  const disposeTheme = bridge.onTheme?.((theme) => { document.documentElement.dataset.theme = theme === 'light' ? 'light' : 'dark'; });
  if (disposeTheme) cleanups.push(disposeTheme);
  window.__ouroWidgetOnDispose?.(() => { disposed = true; active.forEach((abort) => abort.abort()); cleanups.forEach((fn) => fn()); root.replaceChildren(); style.remove(); });
  render();
  Promise.all([loadSetup(), loadPolicy(), loadChats(), loadImports()]).then(() => loadStatus()).then(() => render());
})();
