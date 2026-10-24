/* Account setup and free-participant controls. Host Places remains the sole wake writer. */
(() => {
  const bridge = window.OuroborosWidget;
  const root = document.getElementById('root');
  const style = document.createElement('style');
  style.textContent = `
    :root{--bg:#f7f8f7;--ink:#17231f;--muted:#506057;--edge:#d5ddd8;--field:#fff;--accent:#277450}
    :root[data-theme=dark]{--bg:#171d1a;--ink:#edf3ef;--muted:#b3bfb7;--edge:#414b44;--field:#232b26;--accent:#87cda0}
    *{box-sizing:border-box}body{margin:0;color:var(--ink);background:var(--bg);font:14px/1.5 system-ui,sans-serif}
    #root{padding:20px;overflow-wrap:anywhere}h2{font-size:22px;line-height:1.2;margin:0 0 8px}h3{font-size:16px;margin:0 0 10px}
    p{margin:8px 0}.muted,small{color:var(--muted)}section,details{margin-top:22px}summary{cursor:pointer;font-weight:600;padding:8px 0}
    .fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:12px}
    label{display:flex;flex-direction:column;gap:6px}label.check{flex-direction:row;align-items:flex-start}
    input,select,button{font:inherit;color:var(--ink);max-width:100%}input,select{width:100%;padding:9px;border:1px solid var(--edge);border-radius:7px;background:var(--field);min-width:0}
    input[type=checkbox]{width:auto;flex:none;margin-top:4px}button{padding:8px 12px;border:1px solid var(--edge);border-radius:7px;background:var(--field);cursor:pointer}
    button:disabled{opacity:.55;cursor:default}button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
    .actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.status{color:var(--muted);white-space:pre-wrap}.status[data-error=true]{color:var(--ink);border-left:3px solid #bc6555;padding-left:10px}
    ol{padding-left:23px}li{margin:7px 0}.rule{border-top:1px solid var(--edge);padding-top:18px}.badge{font-size:12px;color:var(--accent);font-weight:650}
  `;
  document.head.append(style);
  root.innerHTML = `
    <header><span class="badge">OWN ACCOUNT · SHARED OUROBOROS MEMORY</span><h2>Telegram, as myself</h2>
      <p class="muted">An independent AI participant, not a public command service.</p>
      <p class="status" id="account" role="status">Reading account status…</p>
      <div class="actions"><button type="button" id="refresh">Check status</button></div></header>
    <section class="rule"><h3>Participation</h3><form id="controls">
      <div class="fields"><label>Transport mode<select name="mode" disabled><option value="participant">Free participant</option><option value="legacy">Legacy scoped transport</option></select></label>
      <label class="check"><input type="checkbox" name="outgoing" disabled><span>Allow outgoing actions<br><small>Telegram rights and current Place controls still apply.</small></span></label></div>
      <div class="actions"><button type="button" data-save-controls disabled>Save controls</button></div><p class="status" data-result role="status"></p></form>
      <p class="muted">Free mode does not require enrolling each correspondent. It does not grant strangers owner authority.</p>
    </section>
    <details id="guide"><summary>Addressed gate, conversation history and setup</summary>
      <h3>Choose what wakes me</h3>
      <p>Open <strong>Settings → Behavior → Consciousness → Places</strong> and select Telegram. The host owns the live wake list; this widget does not keep another copy.</p>
      <ul><li><strong>Direct message:</strong> private correspondence is addressed to me.</li><li><strong>Mention / reply to me:</strong> selective group participation. A plain name in text is not a Telegram mention.</li><li><strong>Any message:</strong> every supported incoming message can wake me, if offered by the connector.</li></ul>
      <p>Messages are captured before the wake decision. Unaddressed messages are heard without starting model work; an active author can receive follow-up context. A later addressed exchange uses the same conversation history, not a new Project.</p>
      <p>Active permits participation. Silent still permits thought, but holds outward actions. Paused hears without starting a new focus. Outgoing actions above is an additional local switch.</p>
      <p>The prepared host UI contribution adds a Places door on connector cards and a read-only conversation reader. Until that core change is applied, use the existing Places settings; a transport log is not a substitute for canonical conversation history.</p>
      <h3>Connect this account once</h3>
      <ol><li>Save the API ID/hash and dedicated phone in Settings → Advanced → Ouroboros · Telegram participant. Blank secret fields keep saved values.</li>
      <li>Request one code below. A request is not proof the code arrived. If necessary use Telegram's explicit next delivery method; do not start a request loop.</li>
      <li>Enter the newest code, and a 2FA password only if requested. A staged login input is not successful authorization; Check status afterwards.</li>
      <li>Connect Telegram in Places using the verified account ID. An empty conversation means account-wide scope. Confirm your own numeric Telegram actor ID there; names and claims are not identity.</li>
      <li>Select Free participant and deliberately enable outgoing actions. Changing transport mode requires turning the skill off and on through Skills; it does not require restarting Ouroboros.</li></ol>
      <h3>Check what actually happened</h3>
      <p>Enabled, authenticated and sent are different facts. Telegram actions have stable intent IDs: queued is not sent; unknown may already have happened and must not be blindly retried. A delivered receipt proves Telegram acknowledgement, not that a person read it.</p>
      <p>Chats, topics, media, sticker packs and admin tools use this account's actual Telegram permissions. They are not invitations to spam, and an admin tool does not make this account an admin. Exclusions and pacing remain under advanced Settings.</p>
    </details>
    <details><summary>Login actions</summary><p class="muted">Use only for this dedicated account. Never copy another person's session or tdata.</p>
      <div class="actions"><button type="button" id="request">Request code</button><button type="button" id="resend">Resend with Telegram's next method</button></div>
      <p class="status" id="code-result" role="status"></p>
      <form id="login"><div class="fields"><label>Newest login code<input type="password" name="code" autocomplete="off" inputmode="numeric"></label>
      <label>2FA password, only if requested<input type="password" name="password" autocomplete="off"></label></div>
      <div class="actions"><button type="button" data-login>Submit private login input</button></div><p class="status" data-result role="status"></p></form>
    </details>`;
  let disposed = false;
  let policyLoaded = false;
  let policyRead = null;
  const active = new Set();
  const cleanups = [];
  const $ = (selector) => root.querySelector(selector);
  const tell = (node, text, error = false) => { if (!disposed) { node.textContent = text; node.dataset.error = String(error); } };
  async function call(path, body) {
    const abort = new AbortController(); active.add(abort);
    try {
      const response = await bridge.fetch(`/api/extensions/telegram-user-session/${path}`, {
        method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : {'Content-Type':'application/json'},
        body: body === undefined ? undefined : JSON.stringify(body), signal: abort.signal, timeoutMs: 20000,
      });
      const value = await response.json();
      if (!response.ok || value.ok === false) throw new Error(value.message || value.error || `Request refused (${response.status})`);
      return value;
    } finally { active.delete(abort); }
  }
  function on(element, name, fn) { element.addEventListener(name, fn); cleanups.push(() => element.removeEventListener(name, fn)); }
  async function status() {
    try {
      const value = await call('participant/status');
      const lines = [value.message || 'Status returned without a description.'];
      if (value.login_attempt?.status) lines.push(`Last login request: ${value.login_attempt.status}${value.login_attempt.reason ? ' / '+value.login_attempt.reason : ''}`);
      if (value.code_delivery?.type) lines.push(`Code delivery method: ${value.code_delivery.type}; next: ${value.code_delivery.next_type || 'not reported'}; timeout: ${value.code_delivery.timeout ?? 'unknown'}`);
      if (value.last_companion_error?.error_type) lines.push(`Earlier companion diagnostic: ${value.last_companion_error.error_type} (${value.last_companion_error.at || 'time unrecorded'})`);
      tell($('#account'), lines.join('\n'));
    } catch (error) { tell($('#account'), error.message, true); }
  }
  function loadPolicy() {
    if (policyRead) return policyRead;
    policyRead = call('participant/policy').then((value) => {
      if(disposed)return;
      const form=$('#controls');form.elements.mode.value=value.mode;
      form.elements.outgoing.checked=value.outgoing==='true';policyLoaded=true;
      form.querySelectorAll('input,select,button').forEach((el)=>{el.disabled=false;});
      tell(form.querySelector('[data-result]'),'');
    }).finally(()=>{policyRead=null;});
    return policyRead;
  }
  on($('#refresh'), 'click', async () => {
    if(!policyLoaded) {
      try { await loadPolicy(); } catch(error) { tell($('#controls [data-result]'),error.message+' — Check status retries this read.',true); }
    }
    await status();
  });
  // Module frames deliberately lack allow-forms. Use the bridge from a button
  // gesture, never native form submission (which the sandbox blocks before submit).
  on($('[data-save-controls]'), 'click', async (event) => {
    event.preventDefault(); const form = $('#controls'); const output = form.querySelector('[data-result]');
    const input = {mode:form.elements.mode.value, outgoing:String(form.elements.outgoing.checked)};
    const fields = [...form.querySelectorAll('input,select,button')]; fields.forEach((el) => {el.disabled=true;});
    tell(output, 'Saving…');
    try {
      const result = await call('participant/policy', input);
      tell(output, result.message + ' A changed transport mode needs a skill off/on before it runs.');
      await status();
    } catch (error) { tell(output, error.message, true); }
    finally { if (!disposed) fields.forEach((el) => {el.disabled=false;}); }
  });
  for (const [id,path] of [['request','setup/request_code'],['resend','setup/resend_code']]) {
    on($('#'+id), 'click', async () => {
      const buttons = [$('#request'),$('#resend')];buttons.forEach((button) => {button.disabled=true;});
      tell($('#code-result'), 'Submitting one request…');
      try { const result = await call(path, {}); tell($('#code-result'), result.message || 'Request accepted locally; check status and Telegram.'); }
      catch (error) { tell($('#code-result'), error.message, true); }
      finally { if (!disposed) buttons.forEach((button) => {button.disabled=false;}); }
    });
  }
  on($('[data-login]'), 'click', async (event) => {
    event.preventDefault();const form=$('#login');const output=form.querySelector('[data-result]');
    const fields=[...form.querySelectorAll('input,button')];fields.forEach((el)=>{el.disabled=true;});
    try {
      const result=await call('setup/login',{code:form.elements.code.value,password:form.elements.password.value});
      if (!disposed) { form.reset();tell(output,result.message || 'Private input staged. Check status for the actual login result.'); }
    } catch(error) { tell(output,error.message,true); }
    finally { if(!disposed)fields.forEach((el)=>{el.disabled=false;}); }
  });
  const disposeTheme = bridge.onTheme?.((theme) => { document.documentElement.dataset.theme=theme; });
  if (disposeTheme) cleanups.push(disposeTheme);
  window.__ouroWidgetOnDispose?.(() => { disposed=true;active.forEach((abort)=>abort.abort());cleanups.forEach((fn)=>fn());root.replaceChildren();style.remove(); });
  loadPolicy().catch((error)=>{
    if(disposed)return;
    tell($('#controls [data-result]'),error.message+' — Check status retries this read.',true);
  });
  status();
})();
