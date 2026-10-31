// The opaque module frame uses only its own routes through the host bridge.
const prefix = '/api/extensions/email-presence/';
const root = document.getElementById('root');
const sheet = document.createElement('link');
sheet.rel = 'stylesheet';
sheet.href = new URL('./accounts.css', import.meta.url).href;
document.head.append(sheet);
const abort = new AbortController();
let disposed = false;
let accounts = [];
let selectedForm = null;
let notice = '';
let noticeError = false;
let authLink = '';
const unsubscribe = window.OuroborosWidget?.onTheme?.(theme => { document.documentElement.dataset.theme = theme; });
window.__ouroWidgetOnDispose?.(() => { disposed = true; abort.abort(); unsubscribe?.(); });

function node(tag, attrs = {}, children = []) {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
        if (key === 'text') element.textContent = value;
        else if (key === 'class') element.className = value;
        else if (key.startsWith('on')) element.addEventListener(key.slice(2), value);
        else if (value !== false && value != null) element.setAttribute(key, String(value));
    }
    for (const child of children) if (child) element.append(child);
    return element;
}

async function request(path, body) {
    const response = await window.OuroborosWidget.fetch(prefix + path, {
        method: body === undefined ? 'GET' : 'POST', signal: abort.signal,
        headers: body === undefined ? {} : {'Content-Type':'application/json'},
        ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
    let value;
    try { value = await response.json(); } catch { throw new Error(`Email service returned HTTP ${response.status}`); }
    if (!response.ok || value.ok === false) throw new Error(value.error || value.message || `HTTP ${response.status}`);
    return value;
}

async function refresh() {
    const result = await request('accounts');
    accounts = result.accounts || [];
    if (!disposed) render();
}

async function act(button, action) {
    button.disabled = true;
    try { await action(); } catch (error) { notice = error.message; noticeError = true; }
    finally { button.disabled = false; if (!disposed) render(); }
}

function button(text, action, cls = '') {
    const item = node('button', {type:'button', text, class:cls});
    item.addEventListener('click', () => act(item, action));
    return item;
}

function field(form, name, label, value = '', type = 'text', options) {
    const input = options ? node('select', {name}) : node(type === 'textarea' ? 'textarea' : 'input', {name, ...(type === 'textarea' ? {} : {type})});
    if (options) for (const entry of options) input.append(node('option', {value:entry.value ?? entry, text:entry.label ?? entry}));
    input.value = value ?? '';
    if (type === 'password') input.autocomplete = 'new-password';
    if (name === 'address' || name === 'username' || name === 'smtp_username') input.autocomplete = 'off';
    const wrapper = node('label', {}, [node('span', {text:label}), input]);
    form.append(wrapper);
    return input;
}

function formData(form) { return Object.fromEntries(new FormData(form)); }

function formAction(form, submit, action) {
    // The opaque host frame intentionally has no allow-forms. Native submission
    // is blocked before a submit event, so buttons/Enter use the owning route.
    const perform = () => { if (!submit.disabled && form.reportValidity()) act(submit, action); };
    submit.addEventListener('click', perform);
    form.addEventListener('submit', event => { event.preventDefault(); perform(); });
    form.addEventListener('keydown', event => {
        if (event.key === 'Enter' && !event.isComposing && event.target.tagName === 'INPUT') {
            event.preventDefault(); perform();
        }
    });
}

function originForReturn() {
    const module = new URL(import.meta.url);
    const marker = module.pathname.indexOf('/api/widget-');
    return module.origin + (marker < 0 ? '' : module.pathname.slice(0, marker));
}

function googleForm() {
    const form = node('form');
    form.append(node('h3', {text:'Connect Google mail'}));
    form.append(node('p', {class:'note', text:'Set and grant your installation’s GOOGLE_OAUTH_CLIENT_ID and, when required, CLIENT_SECRET in Settings. Calendar uses a separate consent. Existing Google project revocation can affect both.'}));
    const fields = node('div', {class:'fields'});
    field(fields, 'login_hint', 'Google account address');
    field(fields, 'origin', 'Ouroboros address for the return', originForReturn(), 'url');
    form.append(fields);
    const full = node('input', {type:'checkbox', name:'permanent_delete'});
    form.append(node('label', {class:'check'}, [full, node('span', {text:'Also request permanent deletion that skips Trash. Ordinary reading, drafting, sending and organizing do not need this permission.'})]));
    const submit = node('button', {type:'button', text:'Prepare Google sign-in', class:'primary'});
    form.append(node('div', {class:'actions'}, [submit, button('Cancel', async () => { selectedForm = null; })]));
    formAction(form, submit, async () => {
            const value = await request('oauth/begin', {...formData(form), permanent_delete:full.checked});
            authLink = value.authorization_url;
            notice = 'Continue in Google, then return here and refresh the account list. The Google page confirms the actual account connected.';
            noticeError = false;
            selectedForm = null;
    });
    return form;
}

function imapForm(account) {
    const config = account?.connection || {};
    const form = node('form');
    form.append(node('h3', {text:account ? 'Edit mail connection' : 'Connect IMAP / SMTP'}));
    form.append(node('p', {class:'note', text:'For Yandex use an app password. For Proton use the local Bridge credentials and its certificate, not your Proton login password. Passwords already saved are never returned to this page.'}));
    const fields = node('div', {class:'fields'});
    field(fields, 'address', 'Mailbox email address', account?.address);
    field(fields, 'label', 'Display label', account?.label);
    field(fields, 'username', 'IMAP login (blank means email address)', config.EMAIL_USER);
    field(fields, 'password', account?.password_configured ? 'App / Bridge password (blank keeps saved)' : 'App / Bridge password', '', 'password');
    field(fields, 'imap_host', 'IMAP host', config.EMAIL_IMAP_HOST);
    field(fields, 'imap_port', 'IMAP port', config.EMAIL_IMAP_PORT || 993, 'number');
    field(fields, 'imap_security', 'IMAP TLS', config.EMAIL_IMAP_SECURITY || 'ssl', 'select', ['ssl','starttls']);
    field(fields, 'smtp_host', 'SMTP host', config.EMAIL_SMTP_HOST);
    field(fields, 'smtp_port', 'SMTP port', config.EMAIL_SMTP_PORT || 587, 'number');
    field(fields, 'smtp_security', 'SMTP TLS', config.EMAIL_SMTP_SECURITY || 'starttls', 'select', ['ssl','starttls']);
    field(fields, 'smtp_username', 'SMTP login (blank means IMAP login)', config.EMAIL_SMTP_USER);
    field(fields, 'smtp_password', 'Separate SMTP password (optional)', '', 'password');
    form.append(fields);
    field(form, 'senders', 'Sender addresses (one per line)', (account?.senders || []).join('\n'), 'textarea');
    field(form, 'sender', 'Default sender (blank means mailbox address)', account?.sender);
    const trust = node('details', {}, [node('summary', {text:'Certificate trust for Proton Bridge or private servers'})]);
    field(trust, 'tls_fingerprint', 'Exact server certificate SHA-256 (optional)', config.EMAIL_TLS_FINGERPRINT);
    field(trust, 'tls_ca', 'Trusted certificate PEM or local CA file (optional)', config.EMAIL_TLS_CA, 'textarea');
    trust.append(node('p', {class:'note', text:'Use a certificate or fingerprint obtained from the server/Bridge you trust. A configured fingerprint is checked before authentication; certificate verification is never silently disabled.'}));
    form.append(trust);
    const submit = node('button', {type:'button', text:account ? 'Save connection' : 'Add account', class:'primary'});
    form.append(node('div', {class:'actions'}, [submit, button('Cancel', async () => { selectedForm = null; })]));
    formAction(form, submit, async () => {
            const data = formData(form);
            if (account) data.account_id = account.id;
            const value = await request('accounts/save', data);
            notice = `Saved ${value.account.address}. Test the connection before relying on it.`;
            noticeError = false;
            selectedForm = null;
            await refresh();
    });
    return form;
}

function preferences(account) {
    const form = node('form');
    form.append(node('h3', {text:`Preferences · ${account.address}`}));
    field(form, 'sender', 'Default sender', account.sender || account.address, 'select', account.senders);
    field(form, 'sending', 'Sending', account.sending || 'on', 'select', ['on','off']);
    form.append(node('p', {class:'note', text:'Sending off holds every send, including direct commands. Reading, drafts and organization remain available. Silent in a connected Place also blocks writes.'}));
    field(form, 'folder', 'Automatic intake folder / Gmail label', account.folder || 'INBOX');
    field(form, 'words', 'Words to notice (one per line)', (account.words || []).join('\n'), 'textarea');
    field(form, 'poll_interval_sec', 'Maximum idle poll interval (seconds)', account.poll_interval_sec || 30, 'number');
    const submit = node('button', {type:'button', text:'Save preferences', class:'primary'});
    form.append(node('div', {class:'actions'}, [submit, button('Cancel', async () => { selectedForm = null; })]));
    formAction(form, submit, async () => {
            await request('accounts/preferences', {...formData(form), account_id:account.id});
            selectedForm = null; notice = 'Preferences saved.'; noticeError = false; await refresh();
    });
    return form;
}

function queueView(account) {
    const queue = account.queue || {};
    const outgoing = {...(queue.outbox || {})};
    const held = queue.outbox_held || 0;
    if (held) {
        outgoing.retry = Math.max(0, (outgoing.retry || 0) - held);
        outgoing.held = held;
    }
    const counts = rows => Object.entries(rows || {}).filter(([, count]) => count > 0)
        .map(([state, count]) => `${count} ${state}`).join(' · ');
    const view = node('div', {class:'delivery-summary'}, [
        node('p', {text:`Inbox: ${counts(queue.inbox) || 'no captured mail'}`}),
        node('p', {text:`Outbox: ${counts(outgoing) || 'no queued mail'}`}),
    ]);
    if (queue.delivery_reports_pending) view.append(node('p', {class:'note', text:`${queue.delivery_reports_pending} delivery reports waiting for host acknowledgement`}));
    const rows = queue.receipts || [];
    if (rows.length) {
        const recent = node('details', {class:'recent-deliveries'}, [node('summary', {text:'Recent deliveries (latest 20)'})]);
        for (const receipt of rows) recent.append(node('div', {class:'receipt'}, [
            node('strong', {text:receipt.state === 'completed' ? 'Accepted by provider' : receipt.state}),
            node('div', {class:'secondary', text:`Request id: ${receipt.request_id}`}),
            node('div', {class:'secondary', text:`Message-ID: ${receipt.provider_message_id}`}),
            receipt.last_error ? node('div', {class:'note', text:receipt.last_error}) : null,
        ]));
        recent.append(node('p', {class:'note', text:'Queued or held is not sent. Uncertain acceptance must be inspected before any new send. Use email_receipt with this account id and request id for complete details.'}));
        view.append(recent);
    }
    return view;
}

function card(account) {
    const card = node('section', {class:'account'});
    card.append(node('div', {class:'address', text:account.label || account.address}),
                node('div', {text:account.address}),
                node('p', {class:'secondary', text:`${account.provider === 'gmail' ? 'Gmail API' : 'IMAP / SMTP'} · ${account.connected ? 'Configured' : 'Disconnected'} · sends as ${account.sender || account.address}` }));
    if (account.runtime_error) card.append(node('p', {class:'notice error', text:`Worker stopped: ${account.runtime_error}. Check the connection or update its configuration.`}));
    const controls = node('div', {class:'actions'});
    if (account.runtime_error && account.connected) controls.append(button('Retry worker', async () => {
        await request(account.environment_config ? 'accounts/resume-legacy' : 'accounts/preferences', {account_id:account.id});
        notice = 'The worker will retry with the current configuration.'; noticeError = false; await refresh();
    }));
    if (account.connected) controls.append(button('Test connection', async () => {
        const result = await request('accounts/test', {account_id:account.id});
        notice = `Authentication check succeeded for ${account.address}. No mail was sent.`; noticeError = false;
        if (result.account?.address && result.account.address !== account.address) throw new Error('Provider reported a different address; inspect this account.');
    }));
    if (!account.environment_config) controls.append(button('Preferences', async () => { selectedForm = {kind:'preferences', account}; }));
    if (account.provider === 'imap' && !account.environment_config) controls.append(button(account.connected ? 'Edit connection' : 'Reconnect', async () => { selectedForm = {kind:'imap', account}; }));
    if (account.provider === 'gmail') controls.append(button(account.connected ? 'Refresh sender addresses' : 'Reconnect Google', async () => {
        if (!account.connected) { selectedForm = {kind:'google'}; return; }
        await request('accounts/refresh-senders', {account_id:account.id}); notice = 'Sender addresses refreshed.'; noticeError = false; await refresh();
    }));
    if (account.environment_config && !account.connected) controls.append(button('Reconnect using Settings', async () => {
        await request('accounts/resume-legacy', {account_id:account.id}); notice = 'Legacy mailbox enabled again.'; noticeError = false; await refresh();
    }));
    if (account.connected) controls.append(button('Disconnect locally', async () => {
        await request('accounts/disconnect', {account_id:account.id}); notice = 'Disconnected locally. Mail history and receipts were retained; Google project grants were not revoked.'; noticeError = false; await refresh();
    }, 'danger'));
    card.append(controls, queueView(account));
    const details = node('details', {}, [node('summary', {text:`${account.senders.length} sender address${account.senders.length === 1 ? '' : 'es'} · account details`})]);
    details.append(node('p', {class:'secondary', text:`Account id: ${account.id}`}),
                   node('pre', {text:account.senders.join('\n')}));
    if (account.provider === 'gmail' && account.senders_status !== 'verified') details.append(node('p', {class:'note', text:'The complete sender list was not verified on this connection. Previously known addresses remain visible; Refresh sender addresses checks Google again.'}));
    if (account.provider === 'gmail') details.append(node('p', {class:'secondary', text:account.scope_status === 'known' ? 'Granted Google scopes:' : 'Google did not report its scope list; operations report actual provider permission failures.'}),
                                                        node('pre', {text:account.scopes.join('\n')}));
    if (account.environment_config) details.append(node('p', {class:'note', text:'This mailbox uses EMAIL_* settings. Use the connector settings for its preferences; disconnect here disables local use but does not erase granted installation settings.'}));
    card.append(details);
    return card;
}

function render() {
    root.replaceChildren();
    root.append(node('div', {class:'toolbar'}, [node('h2', {text:'Email accounts'}), button('Refresh', refresh)]));
    root.append(node('p', {class:'note', text:'Accounts are ordinary tools. Connect a Place separately for incoming mail and presence; an account without a Place is still usable by tasks and permitted autonomous work.'}));
    if (notice) root.append(node('div', {class:`notice${noticeError ? ' error' : ''}`, role:'status', text:notice}));
    if (authLink) root.append(button('Continue in Google', async () => { await window.OuroborosWidget.openExternal(authLink); }, 'primary'));
    root.append(node('div', {class:'actions'}, [button('Add Google mail', async () => { selectedForm = {kind:'google'}; authLink = ''; }),
                                              button('Add IMAP / Proton Bridge', async () => { selectedForm = {kind:'imap'}; })]));
    if (selectedForm) root.append(selectedForm.kind === 'google' ? googleForm() : selectedForm.kind === 'preferences' ? preferences(selectedForm.account) : imapForm(selectedForm.account));
    if (!accounts.length) root.append(node('p', {class:'note', text:'No mail accounts configured yet.'}));
    for (const account of accounts) root.append(card(account));
    root.append(node('p', {class:'note', text:'Credentials stay in this skill’s private local state. Disconnect preserves captured mail and delivery receipts. Removing the skill or explicitly deleting its local data is different.'}));
}

render();
refresh().catch(error => { notice = error.message; noticeError = true; if (!disposed) render(); });
