// In-page observer for the Yandex Eats session. Evaluated by Playwright as
// `page.evaluate(source, args)`. It reads structure (roles, accessible names,
// states, geometry) and never matches site wording: no label, route, or button
// text is known to this file. It reads only an https document on
// args.service_host and returns {blocked: true} anywhere else. It never reads
// field values, editable content, cookies or storage. Elements found by an
// observation are kept in a per-session registry so a later action can
// prove it targets the same node.
async (args) => {
  if (location.protocol !== 'https:' || location.hostname !== args.service_host) return { blocked: true };

  const clean = (value) => {
    let out = '';
    let gap = false;
    for (const ch of String(value == null ? '' : value)) {
      if (ch.trim() === '') { gap = out.length > 0; continue; }
      out += gap ? ' ' + ch : ch;
      gap = false;
    }
    return out;
  };
  const cut = (value, size) => (value.length > size ? value.slice(0, size - 1) + '…' : value);
  const reg = args.key ? (window[args.key] = window[args.key] || { obs: null, els: [] }) : null;
  // Capture roles belong to ONE observation. A later DOM update may turn the
  // same node into an actionable card; inspection still uses the current set.
  let captureOnly = reg ? (reg.captureOnly = reg.captureOnly || new WeakSet()) : new WeakSet();
  if (args.op === 'observe' && reg) captureOnly = reg.captureOnly = new WeakSet();
  const W = window.innerWidth;
  const H = window.innerHeight;

  const INTERACTIVE = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]', '[role=switch]', '[role=tab]',
    '[role=menuitem]', '[role=menuitemcheckbox]', '[role=menuitemradio]', '[role=option]',
    '[role=combobox]', '[role=searchbox]', '[role=textbox]', '[role=spinbutton]', '[role=slider]',
    '[contenteditable=""]', '[contenteditable=true]', '[tabindex]:not([tabindex="-1"])',
  ].join(',');
  // Non-actionable regions are selectable for a focused private image capture.
  const CAPTURE_REGIONS = 'aside,[role=region],[role=complementary],article,section,dialog,[role=dialog]';

  // Text typed or chosen by the owner lives in editable regions and form
  // controls (a textarea's value and a select's options are DOM text); it is never read.
  const EDITABLE = '[contenteditable]:not([contenteditable=false])';
  const ROLE_EDITABLE = '[role~="textbox"],[role~="searchbox"],[role~="combobox"]';
  const SKIP_TEXT = 'script,style,noscript,template,textarea,select,[aria-hidden=true],' + EDITABLE + ',' + ROLE_EDITABLE;
  const POINTER_TAGS = 'div,span,li,img,svg,section,article,label,p,h1,h2,h3,h4,figure,picture';
  const LANDMARKS = 'dialog,[role=dialog],[role=alertdialog],aside,[role=complementary],nav,' +
    '[role=navigation],header,[role=banner],footer,[role=contentinfo],form,[role=search],search';
  const INPUT_ROLES = {
    checkbox: 'checkbox', radio: 'radio', range: 'slider', number: 'spinbutton', search: 'searchbox',
    button: 'button', submit: 'button', reset: 'button', image: 'button',
  };
  const TEXT_TYPES = ['text', 'search', 'email', 'tel', 'url', 'number', 'password'];

  const rendered = (el) => {
    if (!el || !el.isConnected) return false;
    if (el.checkVisibility && !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const inViewport = (rect) => rect.bottom > 0 && rect.right > 0 && rect.top < H && rect.left < W;
  const fullyInViewport = (rect) => rect.top >= 0 && rect.left >= 0 && rect.bottom <= H && rect.right <= W;

  const shown = (el) => !el.checkVisibility || el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  const textCache = new Map();
  // Visible text of a node without form-control or editable content, capped
  // a little above what any caller keeps.
  const textOf = (node) => {
    if (!textCache.has(node)) {
      const parts = [];
      let size = 0;
      if (!node.closest(EDITABLE)) {
        const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
        while (size <= 400 && walker.nextNode()) {
          const parent = walker.currentNode.parentElement;
          if (!parent || parent.closest(SKIP_TEXT) || !shown(parent)) continue;
          const text = clean(walker.currentNode.nodeValue);
          if (text) { parts.push(text); size += text.length + 1; }
        }
      }
      textCache.set(node, clean(parts.join(' ')));
    }
    return textCache.get(node);
  };
  // The display name is short, but a capture must bind the whole visible
  // region text, including cart rows below that name. Editable values remain
  // excluded by the same text policy as observations.
  const signatureOf = (node) => {
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    const skipPainted = 'script,style,noscript,template,textarea,select,' + EDITABLE + ',' + ROLE_EDITABLE;
    let size = 0;
    let hash = 2166136261;
    while (walker.nextNode()) {
      const parent = walker.currentNode.parentElement;
      // aria-hidden removes accessibility, not necessarily painted pixels.
      if (!parent || parent.closest(skipPainted) || !shown(parent)) continue;
      const text = clean(walker.currentNode.nodeValue);
      size += text.length;
      if (size > 1000000) return 'oversized';
      for (let i = 0; i < text.length; i += 1) hash = Math.imul(hash ^ text.charCodeAt(i), 16777619);
    }
    return size + ':' + (hash >>> 0).toString(16);
  };

  const roleToken = (el) => clean(el.getAttribute('role')).split(' ')[0];
  const roleOf = (el) => {
    if (captureOnly.has(el)) return 'region';
    if ((el.tagName === 'ARTICLE' || roleToken(el) === 'article') &&
        getComputedStyle(el).cursor === 'pointer') return 'clickable';
    const explicit = roleToken(el);
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'aside') return 'region';
    if (tag === 'article') return 'article';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') return INPUT_ROLES[el.type] || 'textbox';
    if (el.isContentEditable) return 'textbox';
    return 'clickable';
  };
  const isEntry = (el) => {
    if (el.tagName === 'TEXTAREA') return true;
    if (el.tagName === 'INPUT') return TEXT_TYPES.includes(el.type);
    if (el.isContentEditable) return true;
    const role = roleToken(el);
    return role === 'textbox' || role === 'searchbox';
  };
  const isSearch = (el) => {
    if (!isEntry(el)) return false;
    if (el.type === 'search' || roleToken(el) === 'searchbox') return true;
    if (el.getAttribute('inputmode') === 'search' || el.getAttribute('enterkeyhint') === 'search') return true;
    return !!el.closest('[role=search],search');
  };
  const sensitive = (el) => {
    const tokens = clean(el.getAttribute && el.getAttribute('autocomplete')).toLowerCase().split(' ');
    const inputMode = clean(el.getAttribute && el.getAttribute('inputmode')).toLowerCase();
    return el.type === 'password' || el.type === 'tel' ||
      ['numeric', 'decimal', 'tel'].includes(inputMode) || tokens.some((token) => token.startsWith('cc-') ||
      token === 'one-time-code' || token === 'current-password' || token === 'new-password');
  };
  const formSensitive = (el) => {
    const form = el.form || el.closest('form');
    return !!form && Array.from(form.elements || []).some(sensitive);
  };
  const disabledOf = (el) => !!(el.disabled || el.getAttribute('aria-disabled') === 'true' ||
    el.closest('[aria-disabled=true],fieldset:disabled,[inert]'));

  // Accessible name from labels and attributes; a field's value is never used.
  const nameOf = (el) => {
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return cut(aria, 120);
    const labelledBy = clean(el.getAttribute('aria-labelledby'));
    if (labelledBy) {
      const text = clean(labelledBy.split(' ').map((id) => document.getElementById(id))
        .filter(Boolean).map(textOf).join(' '));
      if (text) return cut(text, 120);
    }
    if (isEntry(el) || el.tagName === 'SELECT') {
      const labels = el.labels ? Array.from(el.labels).map(textOf).join(' ') : '';
      return cut(clean(labels) || clean(el.getAttribute('placeholder')) || clean(el.getAttribute('title')), 120);
    }
    if (el.tagName === 'IMG') return cut(clean(el.alt || el.title), 120);
    let text = textOf(el);
    if (!text) {
      const img = el.querySelector('img[alt]');
      const svgTitle = el.querySelector('svg title');
      text = clean((img && img.alt) || el.getAttribute('title') || (svgTitle && svgTitle.textContent));
    }
    return cut(text, 120);
  };

  // A price footer is not a dish card. Prefer the nearest bounded ancestor
  // with a heading, then a semantic row/card, then the nearest useful text.
  const contextOf = (el, name) => {
    let node = el.parentElement;
    let fallback = '';
    for (let depth = 0; node && node !== document.body && depth < 8; depth += 1, node = node.parentElement) {
      const text = textOf(node);
      if (text.length <= name.length + 2) continue;
      if (text.length > 240) continue;
      const at = name ? text.indexOf(name) : -1;
      const context = cut(clean(at >= 0 ? text.slice(0, at) + ' ' + text.slice(at + name.length) : text), 140);
      if (!fallback && depth < 2 && node.querySelectorAll(INTERACTIVE).length <= 1) fallback = context;
      const headings = node.querySelectorAll('h1,h2,h3,h4,[role=heading]');
      if (!node.matches('main') &&
          (headings.length === 1 || node.matches('article,li,[role=listitem],[role=article]'))) return context;
    }
    return fallback;
  };
  const headingIn = (root) => {
    const heading = root.querySelector('h1,h2,h3,h4,[role=heading]');
    return heading ? cut(textOf(heading), 50) : '';
  };
  const areaOf = (el) => {
    const mark = el.closest(LANDMARKS);
    if (!mark) return '';
    const kind = clean(mark.getAttribute('role')) || mark.tagName.toLowerCase();
    const label = clean(mark.getAttribute('aria-label')) || headingIn(mark);
    return cut(label ? kind + ': ' + label : kind, 60);
  };
  const coveredOf = (el) => {
    const rect = el.getBoundingClientRect();
    if (!inViewport(rect)) return false;
    const x = Math.min(Math.max(rect.left + rect.width / 2, 0), W - 1);
    const y = Math.min(Math.max(rect.top + rect.height / 2, 0), H - 1);
    const hit = document.elementFromPoint(x, y);
    return !!hit && !el.contains(hit) && !hit.contains(el);
  };
  const absoluteHref = (el) => {
    const link = el.closest('a[href]');
    if (!link) return '';
    try { return new URL(link.getAttribute('href'), location.href).href; } catch (error) { return 'invalid:'; }
  };

  const describe = (el, index) => {
    const name = nameOf(el);
    const role = roleOf(el);
    const record = {
      i: index, role, name, in: contextOf(el, name), area: areaOf(el), tag: el.tagName.toLowerCase(),
      entry: isEntry(el), search: isSearch(el), sensitive: sensitive(el), form_sensitive: formSensitive(el),
      disabled: disabledOf(el), covered: coveredOf(el), href: absoluteHref(el),
      input_type: el.tagName === 'INPUT' ? el.type : '',
      focused: document.activeElement === el,
    };
    if (['region', 'article', 'complementary'].includes(role)) record.content_signature = signatureOf(el);
    const checked = el.getAttribute('aria-checked') || (el.checked === true ? 'true' : '');
    if (checked === 'true') record.checked = true;
    if (el.getAttribute('aria-expanded') === 'true') record.expanded = true;
    if (el.getAttribute('aria-selected') === 'true' || el.getAttribute('aria-current')) record.selected = true;
    return record;
  };

  const modalRoot = () => {
    const modals = Array.from(document.querySelectorAll('dialog,[aria-modal=true],[role=alertdialog]'))
      .filter((node) => rendered(node) && (node.tagName !== 'DIALOG' || node.matches(':modal')));
    return modals.length ? modals[modals.length - 1] : null;
  };

  const collect = (root, scopePage) => {
    const wanted = (el) => {
      if (!rendered(el) || el.closest('[aria-hidden=true]')) return false;
      return scopePage || inViewport(el.getBoundingClientRect());
    };
    const found = new Set(Array.from(root.querySelectorAll(INTERACTIVE)).filter(wanted));
    for (const el of root.querySelectorAll(CAPTURE_REGIONS)) {
      if (wanted(el)) {
        if (!el.matches(INTERACTIVE) && getComputedStyle(el).cursor !== 'pointer') captureOnly.add(el);
        found.add(el);
      }
    }
    // Headed divs are capture-only. If a heading has its own wrapper,
    // include its adjacent cart rows without selecting the entire main page.
    for (const heading of root.querySelectorAll('h1,h2,h3,h4,[role=heading]')) {
      let container = heading.parentElement;
      if (!container || container.tagName !== 'DIV') continue;
      const outer = container.parentElement;
      if (outer && outer.tagName === 'DIV' && outer !== root &&
          container.childElementCount <= 2) container = outer;
      if (wanted(container) && getComputedStyle(container).cursor !== 'pointer' &&
          (!container.parentElement || getComputedStyle(container.parentElement).cursor !== 'pointer')) {
        captureOnly.add(container);
        found.add(container);
      }
    }
    // Script-driven clickables (e.g. React cards) expose only a pointer cursor.
    for (const el of root.querySelectorAll(POINTER_TAGS)) {
      if (el.closest(INTERACTIVE) || !wanted(el)) continue;
      if (getComputedStyle(el).cursor !== 'pointer') continue;
      const parent = el.parentElement;
      if (parent && getComputedStyle(parent).cursor === 'pointer') continue;
      const inner = el.querySelector(INTERACTIVE);
      if (inner && textOf(inner) === textOf(el)) continue;
      found.add(el);
    }
    return Array.from(found).map((el) => ({ el, rect: el.getBoundingClientRect() }))
      .sort((a, b) => Math.round(a.rect.top / 8) - Math.round(b.rect.top / 8) || a.rect.left - b.rect.left)
      .map((item) => item.el);
  };

  const textLines = (root, scopePage, limit, query) => {
    const lines = [];
    let current = [];
    let lastTop = null;
    let size = 0;
    let truncated = false;
    const flush = () => {
      const line = clean(current.join(' '));
      current = [];
      if (!line || (query && !line.toLowerCase().includes(query))) return;
      if (size + line.length > limit) { truncated = true; return; }
      lines.push(line);
      size += line.length + 1;
    };
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const text = clean(node.nodeValue);
      const parent = node.parentElement;
      if (!text || !parent || parent.closest(SKIP_TEXT) || !shown(parent)) continue;
      range.selectNodeContents(node);
      const rect = range.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0 || (!scopePage && !inViewport(rect))) continue;
      if (lastTop !== null && Math.abs(rect.top - lastTop) > 4) flush();
      current.push(text);
      lastTop = rect.top;
    }
    flush();
    return { text: lines.join('\n'), truncated };
  };

  const observe = () => {
    const query = clean(args.query).toLowerCase();
    const scopePage = args.scope === 'page' || !!query;
    const modal = modalRoot();
    const root = modal || document.body || document.documentElement;
    let els = collect(root, scopePage);
    let records = els.map((el, index) => describe(el, index));
    if (query) {
      const keep = records.map((record) => (record.name + ' ' + record.in).toLowerCase().includes(query));
      els = els.filter((_, index) => keep[index]);
      records = records.filter((_, index) => keep[index]).map((record, index) => ({ ...record, i: index }));
    }
    const total = records.length;
    const limit = Math.max(1, Math.min(Number(args.max_elements) || 100, 200));
    reg.obs = args.obs;
    reg.els = els.slice(0, limit);
    const text = textLines(root, scopePage, Math.max(200, Math.min(Number(args.max_text) || 3000, 12000)), query);
    const frames = Array.from(document.querySelectorAll('iframe')).filter(rendered).map((frame) => {
      try { return new URL(frame.src, location.href).origin; } catch (error) { return 'unknown'; }
    }).filter((origin) => origin !== location.origin);
    const scroller = document.scrollingElement || document.documentElement;
    return {
      url: location.href, title: cut(clean(document.title), 160),
      modal: modal ? cut(clean(modal.getAttribute('aria-label')) || headingIn(modal) || 'dialog', 80) : '',
      elements: records.slice(0, limit), total, text: text.text, text_truncated: text.truncated,
      frames: Array.from(new Set(frames)).slice(0, 10),
      scroll: { y: Math.round(scroller.scrollTop), max: Math.max(0, Math.round(scroller.scrollHeight - H)) },
    };
  };

  const inspect = () => {
    if (reg.obs !== args.obs) return { missing: 'observation_replaced' };
    const el = reg.els[args.index];
    if (!el) return { missing: 'unknown_element' };
    if (!el.isConnected) return { missing: 'detached' };
    if (!rendered(el)) return { missing: 'hidden' };
    const before = describe(el, args.index);
    const expect = args.expect || {};
    if (before.role !== expect.role || before.name !== expect.name || before.in !== expect.in ||
        (expect.content_signature && before.content_signature !== expect.content_signature)) {
      return { missing: 'changed', now: { role: before.role, name: before.name, in: before.in } };
    }
    // Only a verified element is scrolled; stale handles are refused untouched.
    if (!fullyInViewport(el.getBoundingClientRect()) || before.covered) {
      el.scrollIntoView({ block: 'center', inline: 'nearest' });
      return describe(el, args.index);
    }
    return before;
  };

  const settle = () => new Promise((resolve) => {
    const quiet = Math.max(50, Math.min(Number(args.quiet_ms) || 350, 2000));
    const max = Math.max(quiet, Math.min(Number(args.max_ms) || 4000, 15000));
    let timer = null;
    let observer = null;
    const done = () => { if (observer) observer.disconnect(); clearTimeout(timer); resolve(true); };
    observer = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(done, quiet); });
    observer.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
    timer = setTimeout(done, quiet);
    setTimeout(done, max);
  });

  // Moves the view only: scrollBy dispatches scroll events, never pointer, key or focus input.
  const scroll = () => {
    const scrollable = (node) => ['auto', 'scroll', 'overlay'].includes(getComputedStyle(node).overflowY) &&
      node.scrollHeight > node.clientHeight + 1;
    let node = document.elementFromPoint(W / 2, H / 2);
    while (node && node !== document.body && node !== document.documentElement && !scrollable(node)) {
      node = node.parentElement;
    }
    const inner = node && node !== document.body && node !== document.documentElement;
    const target = inner ? node : (document.scrollingElement || document.documentElement);
    target.scrollBy(0, (args.direction === 'up' ? -1 : 1) * Math.round(H * 0.8));
    return true;
  };

  if (args.op === 'observe') return observe();
  if (args.op === 'inspect') return inspect();
  if (args.op === 'settle') return settle();
  if (args.op === 'scroll') return scroll();
  throw new Error('unknown observer op');
}
