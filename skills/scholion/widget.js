/* Reuse the installed Scholion application through Ouroboros's own fetch bridge.
 * No direct network, second profile, source checkout or model is needed. */
(async () => {
  'use strict';
  const bridge = window.OuroborosWidget;
  const base = '/api/extensions/scholion/';
  const originalFetch = bridge.fetch.bind(bridge);
  const root = document.getElementById('root');
  let lang = 'en', messages = {}, binding = '', disposed = false, refreshTimer;
  const assetURLs = [];
  const assets = {};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const tr = key => messages[key] || key;
  const styles = document.createElement('style');
  styles.textContent = `:root{color-scheme:light dark;font-family:system-ui,sans-serif}*{box-sizing:border-box}body{margin:0;background:var(--cr-bg,#f7f7f4);color:var(--cr-ink,#252525)}body[data-hub-theme=dark]{background:#171a1e;color:#eef0f2}.hub-start{max-width:820px;margin:auto;padding:24px}.hub-start h1{font-size:28px;margin:0 0 12px}.hub-start p{line-height:1.5}.hub-actions{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}.hub-btn{font:inherit;padding:10px 16px;border:1px solid #8888;border-radius:8px;cursor:pointer;background:transparent;color:inherit}.hub-btn.primary{background:#80603d;color:#fff;border-color:#80603d}.hub-start input,.hub-source input{font:inherit;padding:10px;width:100%;border:1px solid #8888;border-radius:6px;color:inherit;background:transparent}.hub-start summary{cursor:pointer;padding:12px 0}.hub-path{overflow-wrap:anywhere;font-size:13px}.hub-start label{display:block;margin:12px 0}.hub-status{white-space:pre-wrap;overflow-wrap:anywhere;min-height:1.5em}.hub-error{color:#c45a58}.hub-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 16px;border-bottom:1px solid #8885;font-size:13px}.hub-toolbar button{font:inherit}.hub-source{max-width:min(680px,95vw);border:1px solid #8888;border-radius:12px;padding:24px;background:Canvas;color:CanvasText}.hub-source::backdrop{background:#0007}.hub-source p{white-space:pre-wrap}.hub-source menu{display:flex;gap:12px;justify-content:flex-end;padding:0}@media(max-width:520px){.hub-start{padding:16px}.hub-actions{display:grid}.hub-toolbar{flex-wrap:wrap}}`;
  document.head.append(styles);
  const offTheme = bridge.onTheme?.(theme => {
    document.documentElement.dataset.theme = theme;
    document.body.dataset.hubTheme = theme;
  });
  window.__ouroWidgetOnDispose?.(() => {
    disposed = true; clearInterval(refreshTimer); offTheme?.();
    assetURLs.forEach(url => URL.revokeObjectURL(url));
  });
  async function json(route, payload) {
    const response = await originalFetch(base + route, payload === undefined ? {} : {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
    });
    const data = await response.json();
    if (response.ok && data.canceled) return data;
    if (!response.ok || data.ok === false || data.error) throw new Error(data.error || response.statusText);
    return data;
  }
  function notice(error) {
    const status = document.getElementById('hub-status');
    if (status) { status.classList.add('hub-error'); status.textContent = error.message || String(error); }
  }
  function dialog(text, confirm = false, url = '') {
    return new Promise(resolve => {
      const box = document.createElement('dialog'); box.className = 'hub-source';
      box.innerHTML = `<p>${esc(text)}</p>${url ? `<input readonly value="${esc(url)}" aria-label="URL">` : ''}<menu><button class="hub-btn" data-close>${esc(tr('close'))}</button>${confirm ? `<button class="hub-btn primary" data-confirm>${esc(tr('confirm'))}</button>` : ''}</menu>`;
      document.body.append(box);
      const finish = value => { box.close(); box.remove(); resolve(value); };
      box.querySelector('[data-close]').onclick = () => finish(false);
      box.querySelector('[data-confirm]')?.addEventListener('click', () => finish(true));
      box.oncancel = event => { event.preventDefault(); finish(false); };
      box.querySelector('input')?.addEventListener('click', event => event.target.select());
      box.showModal();
    });
  }
  async function welcome() {
    const state = await json('state?lang=' + lang);
    messages = state.messages; lang = state.lang;
    document.documentElement.lang = lang;
    root.innerHTML = `<section class="hub-start"><div style="float:right"><button class="hub-btn" id="hub-lang">${lang === 'en' ? 'RU' : 'EN'}</button></div><h1>Scholion</h1><p>${esc(tr('intro'))}</p><p>${esc(tr('privacy'))}</p>${state.ready ? `<p>${state.read_error ? esc(state.hint) : esc(tr('ready')) + ' · ' + esc(state.markers) + ' ' + esc(tr('markers'))}</p><div class="hub-actions"><button class="hub-btn primary" id="hub-open">${esc(tr('open'))}</button><button class="hub-btn" id="hub-labs">${esc(tr('add_labs'))}</button></div>` : `<h2>${esc(tr('first'))}</h2><div class="hub-actions"><button class="hub-btn primary" id="hub-create">${esc(tr('create'))}</button></div><details id="hub-connect"><summary>${esc(tr('existing'))}</summary><p>${esc(tr('connect_help'))}</p><button class="hub-btn" id="hub-pick">${esc(tr('choose'))}</button><label for="hub-profile">${esc(tr('folder_path'))}</label><input id="hub-profile" autocomplete="off"><div class="hub-actions"><button class="hub-btn" id="hub-preview">${esc(tr('check_folder'))}</button></div><div id="hub-preview-result"></div></details>`}<p id="hub-status" class="hub-status" role="status"></p><details><summary>${esc(tr('settings'))}</summary><p>${esc(tr('same_profile'))}</p>${['data_dir','profile_dir','labs_dir','genome_dir','wearables_dir'].map(key => `<p class="hub-path"><strong>${esc(tr(key))}</strong><br>${esc(state[key])}</p>`).join('')}<p class="hub-path">ID: ${esc(state.subject_id)}</p></details><p><small>${esc(tr('caveat'))}</small></p></section>`;
    document.getElementById('hub-lang').onclick = () => { lang = lang === 'en' ? 'ru' : 'en'; welcome().catch(notice); };
    const busy = (id, action) => {
      const button = document.getElementById(id); if (!button) return;
      button.onclick = async () => {
        button.disabled = true; document.getElementById('hub-status').textContent = tr('working');
        try { await action(); } catch (error) { notice(error); }
        finally { if (button.isConnected) button.disabled = false; }
      };
    };
    busy('hub-create', async () => { await json('init', {}); await openApp('web.tab.labs'); });
    busy('hub-open', () => openApp());
    busy('hub-labs', () => openApp('web.tab.labs'));
    busy('hub-pick', async () => {
      const choice = await json('choose', {});
      if (choice.path) document.getElementById('hub-profile').value = choice.path;
      document.getElementById('hub-status').textContent = choice.reason || '';
    });
    busy('hub-preview', async () => {
      const path = document.getElementById('hub-profile').value;
      const result = await json('connect', {path});
      const area = document.getElementById('hub-preview-result');
      area.innerHTML = `<p class="hub-path">${esc(tr('found'))}<br>${esc(result.profile)}</p><button class="hub-btn primary" id="hub-confirm">${esc(tr('connect'))}</button>`;
      document.getElementById('hub-status').textContent = '';
      document.getElementById('hub-confirm').onclick = async event => {
        event.target.disabled = true;
        try { await json('connect', {path, confirm:result.confirm}); await openApp(); }
        catch (error) { notice(error); event.target.disabled = false; }
      };
    });
  }
  const webURL = path => base + 'web?path=' + encodeURIComponent(path) + '&binding=' + encodeURIComponent(binding);
  async function appFetch(path, init = {}) {
    if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//')) throw new Error('Only local Scholion routes are available.');
    const headers = new Headers(init.headers || {}); headers.set('X-Scholion-Profile', binding);
    return originalFetch(webURL(path), {...init, headers});
  }
  async function openApp(initialTab) {
    const launch = await json('launch', {}); binding = launch.binding;
    let response;
    for (let attempt = 0; attempt < 20 && !disposed; attempt++) {
      response = await appFetch('/');
      if (response.ok) break;
      if (response.status !== 503) throw new Error((await response.json()).error);
      await new Promise(resolve => setTimeout(resolve, 400));
    }
    if (!response?.ok || disposed) throw new Error(tr('unavailable'));
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    const scripts = [...page.querySelectorAll('script')]; scripts.forEach(script => script.remove());
    // An opaque module frame cannot fetch /dna.svg itself. Keep even passive
    // assets on the authenticated bridge, including on a remote host.
    for (const element of page.querySelectorAll('[src]')) {
      const source = element.getAttribute('src');
      if (source.startsWith('/') && !source.startsWith('//')) {
        const url = URL.createObjectURL(await (await appFetch(source)).blob());
        assetURLs.push(url); element.setAttribute('src', url);
      }
    }
    // The anatomical silhouettes are referenced later by SVG masks, not by
    // the initial HTML. Give the unchanged body renderer local Blob assets.
    for (const source of ['/body-male.webp', '/body-female.webp']) {
      const file = await appFetch(source);
      if (!file.ok) throw new Error(tr('unavailable'));
      const url = URL.createObjectURL(await file.blob());
      assets[source] = url; assetURLs.push(url);
    }
    for (const css of page.querySelectorAll('link[rel=stylesheet], style')) {
      const style = document.createElement('style');
      style.textContent = css.tagName === 'STYLE' ? css.textContent : await (await appFetch(css.getAttribute('href'))).text();
      document.head.append(style);
    }
    root.innerHTML = page.body.innerHTML;
    const toolbar = document.createElement('div'); toolbar.className = 'hub-toolbar';
    toolbar.innerHTML = `<span>${esc(tr('same_profile'))}</span><button class="hub-btn" id="hub-data">${esc(tr('settings'))}</button>`;
    root.prepend(toolbar);
    document.getElementById('hub-data').onclick = async () => {
      try {
        const state = await json('state?lang=' + lang);
        await dialog(['profile_dir','labs_dir','genome_dir','wearables_dir'].map(key => tr(key) + ':\n' + state[key]).join('\n\n'));
      } catch (error) { await dialog(error.message); }
    };
    window.ScholionHost = {lang, initialTab, assets, confirm:message => dialog(message, true), setLang:code => { lang = code; }};
    window.fetch = appFetch;
    // The host forbids native form navigation. Dispatch the application's
    // existing JS submit handlers without asking the frame to navigate.
    const submit = (form, button) => {
      if (form.reportValidity()) form.dispatchEvent(new SubmitEvent('submit', {
        bubbles:true, cancelable:true, submitter:button || null
      }));
    };
    document.addEventListener('click', event => {
      const button = event.target.closest('button, input[type=submit]');
      if (button?.form && button.type === 'submit') {
        event.preventDefault(); submit(button.form, button);
      }
    }, true);
    document.addEventListener('keydown', event => {
      const input = event.target;
      if (event.key === 'Enter' && input.tagName === 'INPUT' && input.form) {
        event.preventDefault(); submit(input.form, input.form.querySelector('[type=submit]'));
      }
    }, true);
    for (const source of scripts) {
      const script = document.createElement('script');
      script.textContent = source.hasAttribute('src') ? await (await appFetch(source.getAttribute('src'))).text() : source.textContent;
      document.body.append(script);
    }
    document.addEventListener('click', async event => {
      const link = event.target.closest('a[href]'); if (!link) return;
      const href = link.getAttribute('href');
      if (!href.startsWith('/') || href.startsWith('//')) return;
      event.preventDefault(); event.stopPropagation();
      try {
        if (href.startsWith('/') && !href.startsWith('//')) {
          if (link.hasAttribute('download')) {
            const file = await appFetch(href);
            if (!file.ok) throw new Error((await file.json()).error || file.statusText);
            const disposition = file.headers.get('content-disposition') || '';
            const name = /filename="?([^";]+)/i.exec(disposition)?.[1] || 'scholion-export.txt';
            await bridge.download(name, await file.blob());
          }
          else {
            const content = await appFetch(href);
            const type = content.headers.get('content-type') || '';
            if (type.includes('text/html')) {
              const doc = new DOMParser().parseFromString(await content.text(), 'text/html');
              await dialog(doc.body.textContent);
            } else await dialog(await content.text());
          }
        }
      } catch (error) { await dialog(error.message); }
    }, true);
    refreshTimer = setInterval(() => { if (!disposed) window.ScholionHost?.refresh?.(); }, 5000);
  }
  try { await welcome(); }
  catch (error) { root.textContent = 'Scholion could not start: ' + error.message; }
})();
