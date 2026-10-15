// The only code this package runs inside a page. The service worker sends this
// function's source through the debugger into an isolated world of the leased
// tab's main document and calls it synchronously with a fixed operation name.
// It installs no listener, timer or observer and never clicks by script.
// It must stay self-contained: nothing outside the function body exists there.

export function pageAgent(op, args) {
  const store = globalThis.__ouroboros_chrome__
    || (globalThis.__ouroboros_chrome__ = { seq: 0, byId: new Map(), byElement: new WeakMap() });

  class Refusal extends Error {
    constructor(code, message) { super(message); this.code = code; }
  }
  const refuse = (code, message) => { throw new Refusal(code, message); };

  const describe = (el) => {
    if (!el || !el.tagName) return 'nothing';
    const id = el.id ? `#${el.id}` : '';
    const cls = typeof el.className === 'string' && el.className.trim()
      ? `.${el.className.trim().split(/\s+/).slice(0, 2).join('.')}` : '';
    return `${el.tagName.toLowerCase()}${id}${cls}`;
  };

  const handleOf = (el) => {
    let n = store.byElement.get(el);
    if (!n) {
      n = ++store.seq;
      store.byElement.set(el, n);
      store.byId.set(n, new WeakRef(el));
    }
    return n;
  };

  const resolve = (target) => {
    if (!target || typeof target !== 'object') refuse('BROWSER_ARGUMENT_INVALID', 'a selector or a handle is required');
    if (target.handle != null) {
      const ref = store.byId.get(target.handle);
      const el = ref && ref.deref();
      if (!el || !el.isConnected) refuse('BROWSER_HANDLE_STALE', 'the element behind this handle is no longer in the document');
      return el;
    }
    let list;
    try {
      list = document.querySelectorAll(target.selector);
    } catch (error) {
      refuse('BROWSER_ARGUMENT_INVALID', `not a valid CSS selector: ${target.selector}`);
    }
    if (target.nth != null) {
      if (!list[target.nth]) refuse('BROWSER_TARGET_NOT_FOUND', `${list.length} element(s) match ${target.selector}; none at index ${target.nth}`);
      return list[target.nth];
    }
    if (!list.length) refuse('BROWSER_TARGET_NOT_FOUND', `nothing matches ${target.selector}`);
    if (list.length > 1) refuse('BROWSER_TARGET_AMBIGUOUS', `${list.length} elements match ${target.selector}; narrow it or pass nth`);
    return list[0];
  };

  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    // Rendered and not fully transparent, the element and every ancestor: display, content-visibility,
    // visibility and opacity:0. A transparent target would take a trusted click the owner cannot see.
    if (typeof el.checkVisibility === 'function')
      return el.checkVisibility({ opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true });
    for (let node = el; node && node.nodeType === 1; node = node.parentElement || (node.getRootNode() || {}).host) {
      const style = getComputedStyle(node);
      if (style.display === 'none' || Number(style.opacity) === 0) return false;
      if (node === el && (style.visibility === 'hidden' || style.visibility === 'collapse')) return false;
    }
    return true;
  };
  const isDisabled = (el) => el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true';
  const isTextField = (el) => {
    if (el.tagName === 'TEXTAREA') return !el.readOnly;
    if (el.tagName === 'INPUT') {
      return !el.readOnly && !['button', 'checkbox', 'color', 'file', 'hidden', 'image', 'radio', 'range', 'reset', 'submit']
        .includes(el.type);
    }
    return el.isContentEditable;
  };
  const isSecret = (el) => el.tagName === 'INPUT' && el.type === 'password';
  const valueOf = (el) => (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT'
    ? String(el.value) : String(el.innerText || ''));
  const clip = (text, max) => (text.length > max ? `${text.slice(0, max)}…` : text);
  const nameOf = (el) => {
    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map((id) => { const node = document.getElementById(id); return node ? node.innerText : ''; }).join(' ');
    const label = el.labels && el.labels.length ? el.labels[0].innerText : '';
    const own = el.tagName === 'INPUT' ? (['button', 'submit', 'reset'].includes(el.type) ? el.value : '') : el.innerText;
    return clip(String(el.getAttribute('aria-label') || labelled || label || el.getAttribute('placeholder')
      || el.getAttribute('alt') || el.getAttribute('title') || own || '').replace(/\s+/g, ' ').trim(), 120);
  };
  const deepActive = () => {
    let active = document.activeElement;
    while (active && active.shadowRoot && active.shadowRoot.activeElement) active = active.shadowRoot.activeElement;
    return active;
  };
  const composedContains = (ancestor, node) => {
    for (let current = node; current; current = current.parentNode || current.host)
      if (current === ancestor) return true;
    return false;
  };

  const operations = {
    observe() {
      return {
        title: document.title, ready_state: document.readyState, visibility: document.visibilityState,
        has_focus: document.hasFocus(), active: describe(deepActive()),
        scroll: { x: scrollX, y: scrollY }, viewport: { width: innerWidth, height: innerHeight },
      };
    },

    measure() {
      const el = resolve(args.target);
      const rect = el.getBoundingClientRect();
      const left = Math.max(rect.left, 0), top = Math.max(rect.top, 0);
      const right = Math.min(rect.right, innerWidth), bottom = Math.min(rect.bottom, innerHeight);
      return {
        rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
        point: right > left && bottom > top ? { x: (left + right) / 2, y: (top + bottom) / 2 } : null,
        visible: isVisible(el), disabled: isDisabled(el),
        pointer_events: getComputedStyle(el).pointerEvents !== 'none',
        scroll: { x: scrollX, y: scrollY }, what: describe(el),
      };
    },

    scroll_into_view() {
      resolve(args.target).scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
      return { scroll: { x: scrollX, y: scrollY } };
    },

    scroll_by() {
      scrollBy({ left: Number(args.delta_x) || 0, top: Number(args.delta_y) || 0, behavior: 'instant' });
      return { scroll: { x: scrollX, y: scrollY } };
    },

    // Which element would actually receive a pointer event at this point?
    hit() {
      const el = resolve(args.target);
      let hit = document.elementFromPoint(args.x, args.y);
      while (hit && hit.shadowRoot) {
        const inner = hit.shadowRoot.elementFromPoint(args.x, args.y);
        if (!inner || inner === hit) break;
        hit = inner;
      }
      const ok = Boolean(hit) && composedContains(el, hit);
      return { ok, receiver: describe(hit) };
    },

    info() {
      const el = resolve(args.target);
      const checkable = el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio');
      return {
        what: describe(el), tag: el.tagName.toLowerCase(), type: el.type || '', text_field: isTextField(el),
        checkable, checked: checkable ? el.checked : null, disabled: isDisabled(el), secret: isSecret(el),
        focused: deepActive() === el || (el.isContentEditable && el.contains(deepActive())),
        length: isTextField(el) ? valueOf(el).length : 0,
      };
    },

    // Selects the field's current content so that the next trusted text input replaces it.
    select_content() {
      const el = resolve(args.target);
      const focused = deepActive() === el || (el.isContentEditable && el.contains(deepActive()));
      if (!focused) return { focused: false };
      if (typeof el.select === 'function') {
        el.select();
      } else {
        const range = document.createRange();
        range.selectNodeContents(el);
        const selection = getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
      }
      return { focused: true, length: valueOf(el).length };
    },

    value() {
      const el = resolve(args.target);
      const checkable = el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio');
      return { value: isSecret(el) ? null : valueOf(el), length: valueOf(el).length, checked: checkable ? el.checked : null };
    },

    text() {
      const el = args.target ? resolve(args.target) : document.body;
      if (el && isSecret(el)) refuse('BROWSER_ARGUMENT_INVALID', 'password fields are not read');
      const text = el ? valueOf(el) : '';
      const requestedStart = args.start === undefined ? 0 : args.start;
      if (!Number.isSafeInteger(requestedStart) || requestedStart < 0) refuse('BROWSER_ARGUMENT_INVALID', 'start must be a nonnegative integer');
      const start = Math.min(requestedStart, text.length);
      const max = Math.min(Math.max(Number(args.max_chars) || 20000, 1), 200000);
      const end = Math.min(start + max, text.length);
      return { text: text.slice(start, end), length: text.length, truncated: end < text.length,
        coverage: { start, end, total_length: text.length, complete: start === 0 && end === text.length } };
    },

    attribute() {
      const el = resolve(args.target);
      if (isSecret(el) && String(args.name).toLowerCase() === 'value') refuse('BROWSER_ARGUMENT_INVALID', 'password fields are not read');
      return { value: el.getAttribute(String(args.name)) };
    },

    condition() {
      let el = null;
      let count = 0;
      if (args.target) {
        if (args.target.handle != null) {
          el = resolve(args.target);
          count = 1;
        } else {
          let list;
          try {
            list = document.querySelectorAll(args.target.selector);
          } catch (error) {
            refuse('BROWSER_ARGUMENT_INVALID', `not a valid CSS selector: ${args.target.selector}`);
          }
          count = list.length;
          el = (args.target.nth != null ? list[args.target.nth] : list[0]) || null;
        }
      }
      let ok;
      switch (args.state) {
        case 'attached': ok = Boolean(el); break;
        case 'detached': ok = !el; break;
        case 'hidden': ok = !el || !isVisible(el); break;
        case 'enabled': ok = Boolean(el) && isVisible(el) && !isDisabled(el); break;
        default: ok = args.target ? Boolean(el) && isVisible(el) : true;
      }
      if (ok && args.text != null) ok = valueOf(el || document.body).includes(String(args.text));
      return { ok, count, ready_state: document.readyState };
    },

    snapshot() {
      const max = Math.min(Math.max(Number(args.max_nodes) || 150, 1), 1000);
      const selector = 'a[href],button,input,select,textarea,summary,[role],[contenteditable=""],'
        + '[contenteditable="true"],[tabindex],[onclick]';
      const elements = [];
      let truncated = false;
      for (const el of document.querySelectorAll(selector)) {
        if (el.type === 'hidden' || !isVisible(el)) continue;
        if (elements.length >= max) { truncated = true; break; }
        const checkable = el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio');
        const entry = { handle: handleOf(el), tag: el.tagName.toLowerCase(), name: nameOf(el) };
        if (el.id) entry.id = el.id;
        if (el.getAttribute('role')) entry.role = el.getAttribute('role');
        if (el.tagName === 'INPUT') entry.type = el.type;
        if (el.tagName === 'A') entry.href = clip(el.getAttribute('href') || '', 200);
        if (isTextField(el) && !isSecret(el)) entry.value = clip(valueOf(el), 200);
        if (checkable) entry.checked = el.checked;
        if (isDisabled(el)) entry.disabled = true;
        elements.push(entry);
      }
      const headings = Array.from(document.querySelectorAll('h1,h2,h3')).filter(isVisible).slice(0, 40)
        .map((el) => `${el.tagName.toLowerCase()}: ${clip(el.innerText.replace(/\s+/g, ' ').trim(), 160)}`);
      return { title: document.title, headings, elements, truncated };
    },

    dom_snapshot() {
      const max = Math.min(Math.max(Number(args.max_nodes) || 400, 1), 3000);
      const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'LINK', 'META', 'HEAD']);
      const lines = [];
      let truncated = false;
      const walk = (el, depth) => {
        if (truncated || skip.has(el.tagName)) return;
        if (lines.length >= max) { truncated = true; return; }
        const own = Array.from(el.childNodes).filter((node) => node.nodeType === 3).map((node) => node.nodeValue)
          .join(' ').replace(/\s+/g, ' ').trim();
        lines.push(`${'  '.repeat(Math.min(depth, 12))}${describe(el)}${own ? ` "${clip(own, 100)}"` : ''}`);
        if (el.tagName === 'SVG' || el.tagName === 'svg') return;
        for (const child of el.children) walk(child, depth + 1);
      };
      if (document.body) walk(document.body, 0);
      return { title: document.title, outline: lines.join('\n'), nodes: lines.length, truncated };
    },
  };

  try {
    if (!Object.prototype.hasOwnProperty.call(operations, op)) refuse('BROWSER_INTERNAL', `unknown page operation ${op}`);
    return { value: operations[op]() };
  } catch (error) {
    if (error instanceof Refusal) return { error: { code: error.code, message: error.message } };
    return { error: { code: 'BROWSER_INTERNAL', message: String((error && error.message) || error) } };
  }
}
