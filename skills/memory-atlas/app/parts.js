/* Building blocks that continue: a list that shows more, a text that reads on, a stored value with its refs.
 *
 * Nothing here caps what can be reached. A list keeps its pages in a state object the caller holds (so going
 * Back and returning keeps what was read); "Show more" appends in place without re-rendering what is shown.
 * A text keeps every range it read from one revision; a source that changed in between stops the reading and
 * says so instead of joining two versions. Offsets are code points, as the core counts them.
 */
import { h } from './markdown.js';

/** The UTF-16 index of code point `cp` in `s` (or s.length). */
export function cpIndex(s, cp) {
  let i = 0, n = 0;
  while (n < cp && i < s.length) { const c = s.charCodeAt(i); i += c >= 0xd800 && c < 0xdc00 && i + 1 < s.length ? 2 : 1; n++; }
  return i;
}
export function cpSlice(s, from, to) { const a = cpIndex(s, from); return to == null ? s.slice(a) : s.slice(a, a + cpIndex(s.slice(a), to - from)); }
export function cpLength(s) { let n = 0; for (let i = 0; i < s.length; i++) { const c = s.charCodeAt(i); if (!(c >= 0xdc00 && c < 0xe000 && i > 0)) { n++; } } return n; }

/** A stored or derived value's words: a string itself, a ref or derived value its preview, marked as cut. */
export function valueText(v) {
  if (v == null) { return ''; }
  if (typeof v === 'string') { return v; }
  if (typeof v === 'object' && typeof v.preview === 'string') { return v.preview + '…'; }
  return String(v);
}
export function isCut(v) { return !!(v && typeof v === 'object' && typeof v.preview === 'string'); }
export function count(v) { return Array.isArray(v) ? v.length : v && typeof v.count === 'number' ? v.count : 0; }
export const fmt = (n) => Number(n || 0).toLocaleString('en-US');

/* ---- a list that continues ------------------------------------------------------------------------------ */
const CHANGED = ['revision_mismatch', 'position_mismatch'];   // the core's refusal of a cursor over a list that changed

/**
 * `pager({state, fetch, row, button, unit, empty, filter, onPage})` → node.
 * - `state` persists across renders: {entries, next, complete, examined, loaded, changed}.
 * - `fetch(cursor)` resolves a page {entries, next, complete}; `row(entry)` makes a node or null (filtered out).
 * - A page with no rows of its own still continues: the line says how far the read went, and a page's `note`
 *   (what the step is still in the middle of) is said beside it.
 * - A list that changed since its cursor was issued (the core refuses it: `revision_mismatch`, `position_mismatch`)
 *   is never asked that cursor again. What was read stays, marked as the earlier version, until the list read
 *   again from its start has its first page; that page then replaces it, so two versions are never joined. Any
 *   other failure keeps the cursor, and Try again asks it again.
 */
export function pager(o) {
  const st = o.state;
  const box = h('div', { class: 'pager' });
  const rows = h('div', { class: o.rowsClass || 'rows' });
  const foot = h('div', { class: 'pager-foot' });
  box.append(rows, foot);
  let busy = false;
  function addRows(entries) { entries.forEach((e) => { const n = o.row(e); if (n) { rows.appendChild(n); st.shown = (st.shown || 0) + 1; } }); }
  function drawFoot(error) {
    foot.textContent = '';
    if (st.changed) {
      foot.appendChild(h('div', { class: 'notice' }, h('p', null, h('b', null, 'This list changed while you were reading it. '),
        st.shown ? 'What is shown is the earlier version, up to here; it is not joined to the new one.' : null),
        error ? h('p', { class: 'meta' }, 'Couldn’t read it again: ' + (error.message || error.code || 'error')) : null,
        o.button(error ? 'Try again' : 'Read again from the start', again, { key: o.key ? o.key + ':again' : null })));
      return;
    }
    if (error) {
      foot.append(h('p', { class: 'meta' }, 'Couldn’t read more: ' + (error.message || error.code || 'error')), o.button('Try again', more));
      return;
    }
    if (!st.loaded) { return; }
    if (!st.shown && st.complete) { foot.appendChild(h('p', { class: 'meta empty' }, o.empty || 'Nothing here.')); return; }
    if (st.complete) { return; }
    const said = st.shown ? fmt(st.shown) + ' shown' : 'None among the ' + fmt(st.examined) + ' ' + (o.unit || 'read') + ' so far';
    foot.append(h('span', { class: 'meta' }, said + (st.note ? '. ' + st.note : '')), o.button(st.shown ? 'Show more' : 'Look further', more, { key: o.key ? o.key + ':more' : null }));
  }
  const more = () => read(false);
  const again = () => read(true);
  async function read(fresh) {
    if (busy) { return; }
    busy = true;
    const btn = foot.querySelector('button');
    if (btn) { btn.disabled = true; btn.lastChild.textContent = 'Reading…'; }
    try {
      const page = await o.fetch(fresh ? null : st.next);
      if (fresh) { rows.textContent = ''; st.entries = []; st.examined = 0; st.shown = 0; st.changed = false; }
      const first = rows.childElementCount;
      st.entries.push(...page.entries);
      st.examined = (st.examined || 0) + (page.examined != null ? page.examined : page.entries.length);
      st.next = page.next; st.complete = !!page.complete || !page.next; st.loaded = true; st.note = page.note || null;
      addRows(page.entries);
      drawFoot();
      if (o.onPage) { o.onPage(page); }
      const firstNew = rows.children[first];
      if (btn && firstNew && firstNew.focus) { firstNew.focus({ preventScroll: true }); }
    } catch (error) {
      if (error && CHANGED.includes(error.code) && st.loaded) { st.changed = true; drawFoot(); } else { drawFoot(error); }
    } finally { busy = false; }
  }
  if (!st.entries) { st.entries = []; }
  if (st.loaded) { st.shown = 0; addRows(st.entries); drawFoot(); } else { st.shown = 0; more(); }
  box.more = more;
  return box;
}

/** A pager's state from its first page already read (so a view can decide on the first page first). */
export function seeded(page) {
  return { entries: page.entries.slice(), next: page.next, complete: !!page.complete || !page.next, loaded: true,
    examined: page.examined != null ? page.examined : page.entries.length };
}

/* ---- a text read by ranges -------------------------------------------------------------------------------- */
/**
 * `reading({state, fetch, draw, button})` → node. `fetch(start, revision)` resolves a core document answer;
 * `draw(text, answer)` paints what has been read (called again after each range). `state` keeps
 * {text, answer, next, revision, total, changed}.
 */
export function reading(o) {
  const st = o.state;
  const body = h('div');
  const foot = h('div', { class: 'reader-foot' });
  const box = h('div', { class: 'reading' }, body, foot);
  function paint() {
    body.textContent = '';
    body.appendChild(o.draw(st.text, st.answer));
    foot.textContent = '';
    if (st.changed) {
      foot.appendChild(h('div', { class: 'notice' }, h('p', null, h('b', null, 'This changed while you were reading. '),
        'What is shown is the earlier version, up to here; it is not joined to the new one.'),
        o.button('Read the current version', () => { o.restart(); })));
      return;
    }
    if (st.next) {
      foot.append(h('span', { class: 'meta' }, fmt(st.read) + ' of ' + fmt(st.total) + ' characters'),
        o.button('Read on', on, { key: 'read-on' }));
    }
  }
  async function on() {
    const btn = foot.querySelector('button');
    if (btn) { btn.disabled = true; btn.lastChild.textContent = 'Reading…'; }
    try {
      const answer = await o.fetch(st.next.start, st.next.revision);
      st.text += answer.text; st.read = answer.range.end; st.next = answer.next; st.total = answer.range.total;
      paint();
    } catch (error) {
      if (error && error.code === 'revision_mismatch') { st.changed = true; st.next = null; paint(); return; }
      foot.textContent = '';
      foot.append(h('p', { class: 'meta' }, 'Couldn’t read on: ' + (error.message || error.code)), o.button('Try again', on));
    }
  }
  paint();
  return box;
}
export function readingState(answer) {
  return { answer, text: answer.text, read: answer.range.end, total: answer.range.total, next: answer.next, changed: false };
}

/* ---- a stored value, its long parts as refs that read whole ------------------------------------------------ */
/** JSON-like text of a projected value; a ref is its preview and a control that opens the whole value. */
export function storedTree(value, open) {
  const out = h('div', { class: 'source tree', role: 'group', 'aria-label': 'Stored record' });
  function line(depth, parts) { out.appendChild(h('div', { class: 'tl', style: 'padding-left:' + (depth * 2) + 'ch' }, parts)); }
  function scalar(v) { return JSON.stringify(v); }
  function walk(v, depth, key, last) {
    const k = key == null ? [] : [h('span', { class: 'tk' }, JSON.stringify(key) + ': ')];
    const comma = last ? '' : ',';
    if (v && typeof v === 'object' && !Array.isArray(v) && v.ref) {
      const size = v.ref === 'list' ? fmt(v.count) + ' items' : v.ref === 'object' ? fmt(v.keys) + ' keys' : v.chars != null ? fmt(v.chars) + ' characters' : 'more';
      const shown = v.ref === 'text' ? scalar(v.preview) : v.ref === 'list' ? '[' + (v.head || []).map((x) => JSON.stringify(x)).join(', ') + ', …]' : '…';
      line(depth, [...k, h('span', { class: 'excerpt' }, shown), ' ',
        h('button', { type: 'button', class: 'link small', on: { click: () => open(v.address, key) } }, 'Excerpt · ' + size + ' · Read whole'), comma]);
      return;
    }
    if (Array.isArray(v)) {
      if (!v.length) { line(depth, [...k, '[]' + comma]); return; }
      line(depth, [...k, '[']);
      v.forEach((x, i) => walk(x, depth + 1, null, i === v.length - 1));
      line(depth, [']' + comma]);
      return;
    }
    if (v && typeof v === 'object') {
      const keys = Object.keys(v);
      if (!keys.length) { line(depth, [...k, '{}' + comma]); return; }
      line(depth, [...k, '{']);
      keys.forEach((x, i) => walk(v[x], depth + 1, x, i === keys.length - 1));
      line(depth, ['}' + comma]);
      return;
    }
    line(depth, [...k, scalar(v) + comma]);
  }
  walk(value, 0, null, true);
  return out;
}
