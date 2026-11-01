/* Memory Atlas, arrangement B: rooms first, the list of places beside the reader; on a phone the list is the
 * first screen and a place or record opens after a choice.
 *
 * Every word comes from a memory-atlas.read/2 answer through `client` (client.js), which carries the host's
 * own ouroboros.memory/1 reads. The frame interprets nothing about the chronicle: statuses, corrections,
 * folds, selections and an account's sources arrive as facts and are only laid out here. Lists continue with
 * Show more, texts with Read on (one revision), and Refresh re-reads only what changed. Text is always built
 * as text nodes (markdown.js).
 */
import { h, markdown, paragraphs } from './markdown.js';
import * as W from './words.js';
import { pager, seeded, reading, readingState, storedTree, valueText, isCut, count, fmt, cpIndex, cpLength } from './parts.js';

const NARROW_PX = 560;
const LOADING_DELAY_MS = 150;
const CHUNK = 131072;             // code points one text range reads
const COMPARE_MAX = 2000000;      // code points per side a compare reads
const DIFF_CELLS = 4000000;       // line pairs a compare weighs
const STORY_KINDS = ['page', 'part', 'account', 'legacy', 'gap', 'note'];
const DECISION_PAGES = 4;       // pages of 500 decisions one look reads before it says how far it got
const SHELF_CHOICES = 4;        // shelves the Shelf choice shows (DESIGN: up to four fill a row); More shelves lists all

const ICONS = { search: 'M10.5 18a7.5 7.5 0 1 1 5.3-2.2L21 21', back: 'M15 5l-7 7 7 7', copy: 'M9 9h10v10H9zM5 15V5h10',
  refresh: 'M20 12a8 8 0 1 1-2.34-5.66M20 4v5h-5' };
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('width', '16'); svg.setAttribute('height', '16');
  svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', ICONS[name]); p.setAttribute('fill', 'none'); p.setAttribute('stroke', 'currentColor');
  p.setAttribute('stroke-width', '2'); p.setAttribute('stroke-linecap', 'round'); p.setAttribute('stroke-linejoin', 'round');
  svg.appendChild(p);
  return svg;
}
function meta(text, cls) { return h('span', { class: 'meta' + (cls ? ' ' + cls : '') }, text); }
function word(text, tone) { return h('span', { class: 'word ' + (tone || 'plain') }, text); }
const plural = (n, one, many) => fmt(n) + ' ' + (n === 1 ? one : many || one + 's');
const readToEnd = (page) => !!page.complete || !page.next;   // a list read to its end, as a pager counts it

/**
 * A composed record text split for reading: its own words, then each correction under its signature. The
 * core composes `words\n\n[correction <id> by mind (...), <date>]\n<text>…`; the split follows the counts the
 * core gives (`chars`, each correction's `chars`) and checks every signature names its correction. Anything
 * that does not match is shown whole as composed, never guessed.
 */
function splitComposed(text, ownChars, fixes) {
  const ownEnd = cpIndex(text, ownChars);
  const out = { own: text.slice(0, ownEnd), blocks: [], rest: '' };
  let pos = ownEnd;
  for (const fix of fixes) {
    if (pos >= text.length) { return out; }
    if (text.slice(pos, pos + 2) !== '\n\n') { return null; }
    const nl = text.indexOf('\n', pos + 2);
    if (nl < 0) { out.rest = text.slice(pos); return out; }
    const line = text.slice(pos + 2, nl);
    if (!line.startsWith('[correction ' + fix.id + ' by mind (') || !line.endsWith(']')) { return null; }
    const bodyStart = nl + 1, bodyEnd = bodyStart + cpIndex(text.slice(bodyStart), fix.chars || 0);
    if (cpLength(text.slice(bodyStart, bodyEnd)) < (fix.chars || 0)) { out.rest = text.slice(pos); return out; }
    out.blocks.push({ fix, line, body: text.slice(bodyStart, bodyEnd) });
    pos = bodyEnd;
  }
  out.rest = text.slice(pos);   // corrections past the ones listed stay as composed, word for word
  return out;
}

/** A note's source without its front matter, for reading; the Source view keeps every byte. */
function noteBody(text) {
  if (!text.startsWith('---\n')) { return text; }
  const end = text.indexOf('\n---\n', 4);
  return end < 0 ? text : text.slice(end + 5);
}
function dropTitle(text) { return text.replace(/^\s*# .*\n+/, ''); }

export function mountAtlas(root, { client, kit = true } = {}) {
  let overview = null, stack = [], scrolls = [], navScroll = 0, renderToken = 0, disposed = false, announcer = null;
  let pendingFocus = null, position = null, refreshing = false, refreshNote = '', shown = {};
  const cache = new Map();    // answers (promises), per mount
  const states = new Map();   // list and text states, so Back keeps what was read
  const frame = h('div', { class: 'atlas' + (kit ? '' : ' plain') });
  root.textContent = '';
  root.appendChild(frame);

  /* ---- data: one cache per mount; a failed read is forgotten so Try again reads again ------------------- */
  function load(key, fn) {
    if (!cache.has(key)) { cache.set(key, fn().catch((e) => { cache.delete(key); throw e; })); }
    return cache.get(key);
  }
  function forget(test) {
    [...cache.keys()].forEach((k) => { if (test(k.replace(/^first:/, ''))) { cache.delete(k); } });
    [...states.keys()].forEach((k) => { if (test(k)) { states.delete(k); } });
  }
  /** The `forget` test for everything cached of the note or file `address` names: its views and lists and every value
   * read from its front matter, so a change found through any of them reads all of them again. */
  function heldBy(address) {
    if (address.kind === 'file') { return (k) => k.startsWith('file:' + address.source); }
    const own = 'note:' + address.scope + '|' + address.topic;
    return (k) => {
      if (k.startsWith(own)) { return true; }
      if (!k.startsWith('field:')) { return false; }
      const a = JSON.parse(k.slice(6));
      return a.kind === 'note' && a.scope === address.scope && a.topic === address.topic;
    };
  }
  /** A list's state with its first page read (a view decides on that page before it draws). */
  function firstPage(key, fetch) {
    const s = states.get(key);
    if (s && s.loaded) { return Promise.resolve(s); }
    return load('first:' + key, () => fetch(null)).then((page) => { const fresh = seeded(page); states.set(key, fresh); return fresh; });
  }
  const list = (area, params) => (cursor) => client.list(area, Object.assign({}, params, cursor ? { cursor } : {}));
  function readState(key, fetchFirst) {
    const s = states.get(key);
    if (s) { return Promise.resolve(s); }
    return load('first:' + key, fetchFirst).then((answer) => { const fresh = readingState(answer); states.set(key, fresh); return fresh; });
  }

  /* ---- navigation: a stack per mount; nothing is stored outside it -------------------------------------- */
  const narrow = () => window.innerWidth < NARROW_PX;
  const current = () => stack[stack.length - 1];
  function scroller() { return frame.querySelector('.pane .scroll') || frame.querySelector('.scroll'); }
  // WebKit does not focus a button on click, so the row that was pressed is remembered as well as the focused one.
  let pressedKey = null;
  frame.addEventListener('click', (e) => { const k = e.target.closest && e.target.closest('[data-key]'); pressedKey = k ? k.dataset.key : null; }, true);
  function rememberFocus() {
    const a = document.activeElement;
    const key = (a && frame.contains(a) && a.dataset && a.dataset.key) || pressedKey;
    if (stack.length && key) { current().returnKey = key; }
    pressedKey = null;
  }
  function go(route) {
    rememberFocus();
    scrolls[stack.length - 1] = scroller() ? scroller().scrollTop : 0;
    stack.push(route);
    pendingFocus = { title: true };
    render(0);
  }
  function back() {
    if (stack.length < 2) { return; }
    stack.pop();
    pendingFocus = current().returnKey ? { key: current().returnKey } : { title: true };
    render(scrolls[stack.length - 1] || 0);
  }
  function reset(routes) { rememberFocus(); stack = routes.slice(); scrolls = []; pendingFocus = { title: true }; render(0); }
  function replaceTop(route) { stack[stack.length - 1] = Object.assign({}, current(), route); render(0); }
  /* A place chosen in the list: beside the reader it replaces what is open; on a phone it opens over the list. */
  function choose(route) { if (narrow()) { go(route); } else { reset([route]); } }

  const rooms = () => (overview && overview.rooms.entries) || [];
  function roomLabel(id) {
    const r = rooms().find((x) => x.room_id === id);
    if (r) { return valueText(r.label); }
    return id === '' ? 'Host records without a room' : 'room ' + id;
  }
  const roomTitle = (id, label) => W.roomName(label != null ? valueText(label) : roomLabel(id)).name;
  function shelfLabel(scope) {
    if (scope === 'global') { return 'Global'; }
    const s = ((overview && overview.shelves.entries) || []).find((x) => x.scope === scope);
    return (s && s.name) || scope.replace(/^project:/, 'Project ');
  }
  function routeLabel(route) {
    switch (route.v) {
      case 'home': return 'Places';
      case 'story': return 'My story';
      case 'room': return roomTitle(route.room, route.label);
      case 'record': case 'note': return route.label || (route.v === 'note' ? route.topic : 'Record');
      case 'compare': return 'Versions';
      case 'version': return route.label || 'Version';
      case 'field': return route.label || 'Stored value';
      case 'quote': return route.label || 'Quote';
      case 'knowledge': return 'Knowledge';
      case 'shelves': return 'Shelves';
      case 'marks': return 'Marks';
      case 'doc': case 'journal': return route.label || 'File';
      case 'sources': return 'Other sources';
      case 'search': return 'Search';
      default: return '';
    }
  }
  function activeKey() {
    for (let i = stack.length - 1; i >= 0; i--) {
      const r = stack[i];
      if (r.v === 'room') { return 'room:' + r.room; }
      if (r.v === 'story') { return 'story'; }
      if (r.v === 'knowledge' || r.v === 'shelves' || r.v === 'note' || r.v === 'compare') { return 'knowledge'; }
      if (r.v === 'marks') { return 'marks'; }
      if (r.v === 'doc' && r.source === 'identity') { return 'doc:identity'; }
      if (r.v === 'sources' || r.v === 'doc' || r.v === 'journal') { return 'sources'; }
      if (r.v === 'record' && r.hint) { return r.hint.kind === 'mark' ? 'marks' : 'room:' + r.hint.room; }
    }
    return '';
  }

  /* Routes carry the words a Back button and the list need, from the row that opened them. */
  function recordRoute(e, extra) {
    const id = e.id || (e.address && e.address.id);
    return Object.assign({ v: 'record', id, label: W.kindWord(e.kind) + ', ' + roomTitle(e.room_id, e.label),
      hint: { kind: e.kind, room: e.room_id } }, extra || {});
  }
  function noteRoute(n) { return { v: 'note', scope: n.scope, topic: n.topic, label: valueText(n.title) || n.topic }; }

  /* ---- shared building blocks ----------------------------------------------------------------------------- */
  function searchField(value) {
    const input = h('input', { type: 'search', class: 'ui-control', 'aria-label': 'Search memory text', placeholder: 'Search memory', autocomplete: 'off', spellcheck: 'false' });
    input.value = value || '';
    input.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' || !input.value.trim()) { return; }
      e.preventDefault();
      const q = input.value.trim();
      if (current() && current().v === 'search') { replaceTop({ v: 'search', q: q }); } else { go({ v: 'search', q: q }); }
    });
    return h('label', { class: 'search' }, icon('search'), input);
  }
  function rowButton(key, onClick, children, extra) {
    return h('button', Object.assign({ type: 'button', class: 'row', 'data-key': key, on: { click: onClick } }, extra || {}), children);
  }
  function previewOf(e) { return (e.preview || '') + ((e.chars || 0) > cpLength(e.preview || '') ? '…' : ''); }
  function entryRow(e, where) {
    const kind = W.kindWord(e.kind);
    const label = where === 'room' ? kind : kind + ' · ' + roomTitle(e.room_id, e.label);
    const when = W.DATED.includes(e.kind) ? W.periodWords(e, false) || '' : e.ts ? W.day(e.ts) : '';   // a period is coverage, a write time is not
    const top = [meta(label + (when ? ' · ' + when : ''))];
    const sw = W.statusWord(e.status);
    if (sw) { top.push(h('span', { class: 'right' }, word(sw))); }
    const facts = [];
    const fixes = typeof e.corrections === 'number' ? e.corrections : e.fixes_count || 0;
    if (fixes) { facts.push(fixes === 1 ? 'Corrected' : 'Corrected ' + fmt(fixes) + ' times'); }
    if (e.folded_into) { facts.push('In a part'); }
    if (e.kind === 'part' && e.covers && e.covers.member_ids) { facts.push('Folds ' + plural(count(e.covers.member_ids), 'record')); }
    if (e.kind === 'account') {
      const cites = typeof e.sources === 'number' ? e.sources : e.sources_count;   // a list's count, or the story's
      if (typeof cites === 'number') { facts.push('Cites ' + plural(cites, 'source')); }
      if (e.replaces && e.replaces.count) { facts.push('Tells ' + plural(e.replaces.count, 'record') + ' in their place'); }
      if (e.selection && typeof e.selection === 'object') { facts.push(e.selection.shown ? 'Selected into My story' : 'Withdrawn from My story'); }
    }
    if (e.told_by) { facts.push('Told in My story by an account'); }
    return rowButton('record:' + e.id, () => go(recordRoute(e)), [
      h('div', { class: 'top' }, top),
      h('p', { class: 'text clamp3' }, previewOf(e) || (e.kind === 'activation' ? 'The chronicle’s activation receipt.' : '')),   // a receipt has no words of its own
      facts.length ? h('div', { class: 'facts' }, facts.map((f) => word(f))) : null]);
  }
  function markRow(m) {
    const where = m.scope === 'global' ? 'Global' : roomTitle(m.room_id);
    const released = m.released && typeof m.released === 'object' ? m.released : null;
    return rowButton('record:' + m.id, () => go(recordRoute(m)), [
      h('p', { class: 'text' }, previewOf(m)),
      h('div', { class: 'facts' }, meta(released ? 'Released ' + W.day(released.ts) + ' · ' + where : where + ' · tied to ' + W.anchorText(m.anchor)))], { class: 'row mark-row' });
  }
  function noteRow(n, showPath) {
    const facts = [];
    if (n.state === 'archived') { facts.push(word('Archived' + (n.archive && n.archive.at ? ' ' + W.day(n.archive.at) : '') + (n.archive && n.archive.reason ? ' · ' + valueText(n.archive.reason) : ''))); }
    if (n.parse_error || n.read_error) { facts.push(word('Metadata unreadable · shown as stored')); }
    return rowButton('note:' + n.scope + '|' + n.topic, () => go(noteRoute(n)), [
      h('div', { class: 'top' }, h('span', { class: 'title' }, valueText(n.title) || n.topic), showPath === false ? null : meta(n.topic)),
      n.summary ? h('p', { class: 'text sec clamp2' }, valueText(n.summary)) : h('p', { class: 'text' }, meta('No summary')),
      facts.length ? h('div', { class: 'facts' }, facts) : null]);
  }
  function button(label, onClick, opts) {
    const o = opts || {};
    return h('button', { type: 'button', class: 'btn ' + (o.quiet ? 'btn-ghost' : 'btn-default') + ' compact', 'aria-label': o.aria, 'aria-pressed': o.pressed == null ? null : String(!!o.pressed), title: o.title, 'data-key': o.key, on: { click: onClick } },
      o.icon ? icon(o.icon) : null, h('span', null, label));
  }
  function segment(groupLabel, choices) {
    return h('div', { class: 'seg', role: 'group', 'aria-label': groupLabel }, choices.map((c) => button(c.label, c.on, { pressed: c.chosen })));
  }
  function link(label, onClick, cls) { return h('button', { type: 'button', class: 'link' + (cls ? ' ' + cls : ''), on: { click: onClick } }, label); }
  function recLink(e, label) { return link(label || W.kindWord(e.kind) + ', ' + roomTitle(e.room_id, e.label), () => go(recordRoute(e))); }
  function announce(text) { if (announcer) { announcer.textContent = ''; setTimeout(() => { if (announcer) { announcer.textContent = text; } }, 30); } }
  function copyButton(text, label) { const b = button(label, () => copy(text, b, label), { icon: 'copy', title: text }); return b; }
  function copy(text, b, label) {
    const say = b.lastChild;
    function done(ok) {
      say.textContent = ok ? 'Copied' : 'Select and copy';
      announce(ok ? 'Address copied' : 'Copy is blocked here: the address is selected, press Command C');
      if (!ok) { const code = frame.querySelector('.addr code'); if (code) { const sel = getSelection(), rg = document.createRange(); rg.selectNodeContents(code); sel.removeAllRanges(); sel.addRange(rg); } }
      setTimeout(() => { say.textContent = label; }, 1800);
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(text).then(() => done(true), () => done(false)); } else { done(false); }
    } catch (e) { done(false); }
  }
  function addressBlock(text, heading, hint) {
    if (!text) { return null; }
    return h('section', { class: 'block' }, h('h2', { class: 'h-group' }, heading || 'Address'),
      h('div', { class: 'addr' }, h('code', null, text), copyButton(text, 'Copy')),
      hint === false ? null : h('p', { class: 'meta note-line' }, 'Paste it into a chat to talk about exactly this.'));
  }
  function viewTitle(text, attrs) { return h('h1', Object.assign({ tabindex: '-1', 'data-view-title': '' }, attrs || {}), text); }
  function section(title, body, extra) { return h('div', { class: 'section' }, h('h2', { class: 'h-group' }, title), extra || null, body); }
  function block(title, ...body) { return h('section', { class: 'block' }, h('h2', { class: 'h-group' }, title), ...body); }
  /** A list section from its state: rows, then Show more until the area is complete. */
  function listNode(st, fetch, row, o) {
    return pager(Object.assign({ state: st, fetch, row, button: (label, fn, opts) => button(label, fn, opts) }, o || {}));
  }
  function textReader(st, fetch, draw, restart) {
    return reading({ state: st, fetch, draw, restart, button: (label, fn, opts) => button(label, fn, opts) });
  }

  /* ---- views: rooms and the story -------------------------------------------------------------------------- */
  async function viewRoom(route) {
    const id = route.room, box = h('div');
    const name = W.roomName(valueText(route.label != null ? route.label : roomLabel(id)));
    const facts = rooms().find((r) => r.room_id === id);
    box.appendChild(h('div', { class: 'room-head' }, h('p', { class: 'kicker' },
      meta(name.name.startsWith('Project ') ? 'Project room' : id === '1' ? 'Main room' : id === '' ? 'Host' : 'Room', name.name.startsWith('Project ') ? 'kind-project' : ''),
      facts ? meta(plural(facts.records, 'record')) : null, name.chat ? meta('chat ' + name.chat) : null), viewTitle(name.name, { title: name.full })));
    if (id === '') { box.appendChild(await hostRecords(facts)); return { node: box }; }
    const roomKey = 'room:' + id;
    const [cand, story, decisions, marks] = await Promise.all([
      firstPage(roomKey + ':cand', list('records', { room_id: id, kinds: ['page', 'part'], status: ['candidate'], limit: 200 })),
      firstPage(roomKey + ':story', list('records', { room_id: id, kinds: STORY_KINDS, unfolded: true, limit: 50 })),
      firstPage(roomKey + ':decisions', list('records', { room_id: id, kinds: ['decision'], limit: 50 })),
      firstPage(roomKey + ':marks', list('marks', { room_id: id, state: 'all', limit: 50 }))]);
    // What is not settled comes first: a candidate does not act until it is decided.
    if (cand.entries.length || !cand.complete) {
      box.appendChild(section('Candidate pages and parts awaiting a decision', listNode(cand,
        list('records', { room_id: id, kinds: ['page', 'part'], status: ['candidate'], limit: 200 }), (e) => entryRow(e, 'room'),
        { unit: 'pages and parts read', empty: 'None awaiting a decision.', key: roomKey + ':cand' })));
    }
    // The room's story as the chronicle keeps it: candidates and rejected drafts stand apart from it.
    box.appendChild(section('Story in this room', listNode(story, list('records', { room_id: id, kinds: STORY_KINDS, unfolded: true, limit: 50 }),
      (e) => (e.status === 'candidate' || e.status === 'rejected' ? null : entryRow(e, 'room')),
      { unit: 'records read', empty: 'No records in this room yet.', key: roomKey + ':story' })));
    if (decisions.entries.length || !decisions.complete) {
      box.appendChild(section('Decisions on drafts', listNode(decisions, list('records', { room_id: id, kinds: ['decision'], limit: 50 }), (d) =>
        rowButton('decision:' + d.id, () => go({ v: 'record', id: d.target_id, focus: d.id, label: 'Draft, ' + name.name, hint: { kind: 'page', room: id } }), [
          h('div', { class: 'top' }, meta((d.accepted ? 'Accepted ' : 'Rejected ') + W.day(d.ts) + (d.signer ? ' · ' + d.signer : ''))),
          h('p', { class: 'text clamp2' }, '“' + previewOf(d) + '”')]), { unit: 'decisions read', key: roomKey + ':decisions' })));
    }
    if (marks.entries.length || !marks.complete) {
      box.appendChild(section('Marks written here', listNode(marks, list('marks', { room_id: id, state: 'all', limit: 50 }), markRow,
        { unit: 'marks read', key: roomKey + ':marks' })));
    }
    return { node: box };
  }
  /** The host's room is the room whose id is exactly '': its records, every kind, a page at a time. A core that does
   * not list that room refuses the empty id (invalid_parameter); then the activation receipt and the count stand. */
  async function hostRecords(facts) {
    const params = { room_id: '', limit: 50 };
    try {
      const st = await firstPage('room::records', list('records', params));
      return section('Records the host wrote', listNode(st, list('records', params), (e) => entryRow(e, 'room'),
        { unit: 'records read', empty: 'No records here yet.', key: 'room::records' }));
    } catch (e) {
      if (!e || e.code !== 'invalid_parameter') { throw e; }
    }
    const act = overview.chronicle && overview.chronicle.activation;
    const n = facts ? facts.records : 0;
    return h('div', { class: 'section' }, h('h2', { class: 'h-group' }, 'Records the host wrote'),
      h('div', { class: 'rows' }, act ? rowButton('record:' + act.id, () => go({ v: 'record', id: act.id, label: 'Activation', hint: { kind: 'activation', room: '' } }),
        [h('div', { class: 'top' }, meta('Activation · ' + W.day(act.ts))), h('p', { class: 'text' }, 'The chronicle’s activation receipt.')]) : null),
      n > (act ? 1 : 0) ? h('p', { class: 'meta note-line' }, plural(n - (act ? 1 : 0), 'more host record') + ' cannot be listed here: this Ouroboros does not list the host’s room by its empty id. Each is still read by its address.') : null);
  }

  async function viewStory(route) {
    const fetch = list('story', { limit: 50 });
    const guarded = (cursor) => fetch(cursor).catch((e) => {
      if (e && e.code === 'position_mismatch' && !disposed) {   // the story changed between pages: read it again, never mix
        forget((k) => k.startsWith('story'));
        replaceTop({ changed: true });
      }
      throw e;
    });
    let st;
    try { st = await firstPage('story', fetch); } catch (e) { if (e && e.code === 'unavailable') { return { node: storyState(e.message) }; } throw e; }
    const first = cache.get('first:story');
    const answer = first ? await first : {};
    const box = h('div', { class: 'doc wide' }, viewTitle('My story'),
      h('p', { class: 'byline meta' }, 'The story as it stands across rooms: parts stand for the records they fold, and an account Ouroboros selected tells the records it names.'));
    if (route.changed) { box.appendChild(h('div', { class: 'notice' }, h('p', null, 'My story changed while it was being read, so it was read again from the start.'))); }
    if (answer.state && answer.state !== 'active') {
      box.appendChild(h('p', { class: 'meta empty' }, answer.state === 'absent' ? 'There is no chronicle in this installation yet.' : 'The chronicle is not activated yet, so there is no story to show.'));
      return { node: box };
    }
    box.appendChild(h('div', { class: 'bleed spaced' }, listNode(st, guarded, (e) => entryRow(e),
      { unit: 'entries read', empty: 'Nothing stands in My story yet.', key: 'story' })));
    return { node: box };
  }
  function storyState(message) {
    return h('div', { class: 'doc' }, viewTitle('My story'), h('div', { class: 'notice' }, h('p', null, h('b', null, 'My story can’t be read right now. '), message || '')));
  }

  /* ---- a record ---------------------------------------------------------------------------------------- */
  async function viewRecord(route) {
    const address = { kind: 'record', id: route.id };
    const key = 'rec:' + route.id + ':' + (route.rev || '');
    const st = await readState(key, () => client.text(address, { revision: route.rev || undefined, length: CHUNK }));
    const doc0 = st.answer, r = doc0.record;
    if (r.kind === 'mark') { return viewMark(doc0, st.text); }
    const src = !!route.source;
    const fixes = doc0.corrections_shown ? (await load('rec:' + route.id + ':fixes', () => client.list('history', { address, kinds: ['correction'], limit: 500 }))).entries.slice(0, doc0.corrections_shown) : [];
    const roomName = W.roomName(doc0.room_label || roomLabel(r.room_id));
    const dated = doc0.source_period ? W.periodWords(doc0, true) : '';   // a title takes the span in words; the text stays in Facts
    let heading = dated && r.kind !== 'account' ? { name: roomName.name + ', ' + dated, full: roomName.full } : roomName;
    if (r.kind === 'account') {   // an account tells across rooms: it is named by the rooms of what it cites
      const cited = await firstPage('rec:' + route.id + ':sources', list('relations', { address, relation: 'sources', limit: 10 }));
      const names = [...new Set(cited.entries.filter((s) => !s.missing).map((s) => roomTitle(s.room_id)))];
      heading = { name: cited.complete && names.length ? 'Across ' + W.andList(names) : 'An account across rooms', full: roomName.full };
    }
    const doc = h('article', { class: 'doc' });
    const kicker = h('p', { class: 'kicker' }, meta(W.kindWord(r.kind)));
    const sw = W.statusWord(r.status);
    if (sw) { kicker.appendChild(word(sw)); }
    doc.appendChild(kicker);
    doc.appendChild(viewTitle(heading.name, { title: heading.full }));
    doc.appendChild(h('p', { class: 'byline meta' }, W.byline(r.author, r.signer) + ' · ' + W.when(r.ts)));
    doc.appendChild(segment('Show', [
      { label: 'Read', chosen: !src, on: () => replaceTop({ source: false }) },
      { label: 'Stored record', chosen: src, on: () => replaceTop({ source: true }) }]));
    const later = doc0.later_corrections || { count: 0 };
    if (route.rev && later.count) {
      doc.appendChild(h('div', { class: 'notice' },
        h('p', null, h('b', null, 'An earlier version. '), plural(later.count, 'later correction') + (later.count === 1 ? ' is' : ' are') + ' not part of it.'),
        link('Read it as it stands now', () => replaceTop({ rev: null, focus: null }))));
    }
    const addr = addressBlock(W.addressText(address, { revision: route.rev }));
    if (src) {
      doc.appendChild(storedTree(doc0.stored, (fieldAddress, keyName) => go({ v: 'field', address: fieldAddress, label: 'Stored ' + (keyName || 'value') })));
      doc.appendChild(h('p', { class: 'meta note-line' }, 'The record as stored. A long value shows its first words; Read whole opens all of it.'));
      doc.appendChild(addr);
      return { node: doc, address: W.addressText(address, { revision: route.rev }) };
    }
    doc.appendChild(textReader(st, (start, revision) => client.text(address, { start, revision, length: CHUNK }), (text) => {
      const parts = splitComposed(text, r.chars || 0, fixes);
      const out = h('div');
      if (!parts) { out.appendChild(h('div', { class: 'prose' }, paragraphs(text))); return out; }
      out.appendChild(h('div', { class: 'prose' }, paragraphs(parts.own)));
      parts.blocks.forEach((b) => {
        const focused = route.focus === b.fix.id;
        out.appendChild(h('div', { class: 'correction', id: 'fix-' + b.fix.id, 'data-focus': focused ? '' : null, title: b.line },
          focused ? h('p', { class: 'opened word crimson' }, 'The change you opened') : null,
          h('span', { class: 'sig' }, 'Correction by Ouroboros' + (b.fix.signer ? ' (' + b.fix.signer + ')' : '') + ' · ' + W.dayYear(b.fix.ts)),
          paragraphs(b.body)));
      });
      if (parts.rest) { out.appendChild(h('div', { class: 'prose' }, paragraphs(parts.rest))); }
      return out;
    }, () => { forget((k) => k.startsWith('rec:' + route.id)); render(0); }));
    const quoted = await quotesBlock(address, doc0);
    if (quoted) { doc.appendChild(quoted); }
    if (['draft', 'candidate', 'accepted', 'rejected'].includes(r.status)) { doc.appendChild(await decisionBlock(address, r, route)); }
    if (r.kind === 'part') { doc.appendChild(await membersBlock(address)); }
    if (r.kind === 'account') { doc.appendChild(await accountBlock(address, r)); }
    doc.appendChild(await factsBlock(address, r, doc0));
    doc.appendChild(addr);
    shown = { kind: 'record', id: route.id, revision: doc0.revision };
    return { node: doc, address: W.addressText(address, { revision: route.rev }) };
  }

  /* ---- the words a page or part quotes, each checked by the host against its chat row when it was written ---- */
  /** The stored quotes as the record answer projects them; a list past the answer's budget is read whole. */
  async function quotesOf(address, stored) {
    const q = stored && stored.quotes;
    if (Array.isArray(q)) { return { items: q, total: q.length, whole: null }; }
    if (q && q.ref === 'list') { return { items: q.head || [], total: q.count, whole: q.address }; }
    if (q && q.ref && q.address) {
      const all = await load('quotes:' + address.id, () => wholeText(q.address));
      let items = null;
      try { items = all.cut ? null : JSON.parse(all.text); } catch (e) { items = null; }
      return Array.isArray(items) ? { items, total: items.length, whole: null } : { items: [], total: null, whole: q.address };
    }
    return { items: [], total: 0, whole: null };
  }
  /** One stored quote's facts, whatever the answer projected: its words, speaker class and chat row. */
  function quoteParts(q) {
    const at = q && typeof q.address === 'string' ? q.address : '';
    const speaker = q && typeof q.speaker === 'string' ? q.speaker : '';
    return { text: valueText(q && q.text), cut: isCut(q && q.text), whole: q && q.text && q.text.address, speaker, address: at,
      row: W.parseRow(at), voice: speaker === 'human' ? 'person' : speaker === 'ouroboros' ? 'ouroboros' : 'other' };
  }
  const quoteWhere = (p) => (p.row ? roomTitle(p.row.room) + ' · ' + W.when(p.row.ts) : 'Row address not recorded');
  async function quotesBlock(address, doc0) {
    const found = await quotesOf(address, doc0.stored);
    if (!found.items.length && !found.whole) { return null; }
    const box = block('Words it quotes, checked against the chat', ...found.items.map((q, i) => {
      const p = quoteParts(q);
      return h('button', { type: 'button', class: 'quote ' + p.voice, 'data-key': 'quote:' + address.id + ':' + i,
        on: { click: () => go({ v: 'quote', id: address.id, index: i, label: W.speakerWord(p.speaker) }) } },
        h('span', { class: 'who' }, W.speakerWord(p.speaker)), h('p', null, '“' + p.text + '”'), h('span', { class: 'src meta' }, quoteWhere(p)));
    }));
    if (found.whole) {
      box.appendChild(h('p', { class: 'meta note-line' }, found.total != null ? plural(found.total - found.items.length, 'more quote') + '. ' : '',
        link('Read every quote as stored', () => go({ v: 'field', address: found.whole, label: 'Quotes' }), 'small')));
    }
    return box;
  }
  /** A quote's source: who said it, in which room and when (the row's own address), the host's check, the quote as
   * stored and the exact read of that one chat row to paste into a chat. */
  async function viewQuote(route) {
    const address = { kind: 'record', id: route.id };
    const st = await readState('rec:' + route.id + ':', () => client.text(address, { length: CHUNK }));
    const doc0 = st.answer, r = doc0.record;
    const q = (await quotesOf(address, doc0.stored)).items[route.index];
    if (!q) { return { node: h('div', { class: 'doc' }, viewTitle('Quote'), h('p', { class: 'meta empty' }, 'This record holds no quote at that place.')) }; }
    const p = quoteParts(q), call = W.rowAddressText(p.address);
    const doc = h('article', { class: 'doc' });
    doc.appendChild(h('p', { class: 'kicker' }, meta('Quote')));
    doc.appendChild(viewTitle(W.speakerWord(p.speaker) + (p.row ? ', ' + roomTitle(p.row.room) : '')));
    doc.appendChild(h('p', { class: 'byline meta' }, p.row ? 'The chat row of ' + W.when(p.row.ts) : 'Its chat row has no readable address'));
    doc.appendChild(h('div', { class: 'quote ' + p.voice }, h('p', null, '“' + p.text + '”')));
    if (p.cut && p.whole) { doc.appendChild(h('p', { class: 'meta note-line' }, link('Read the whole quote', () => go({ v: 'field', address: p.whole, label: 'Quote' }), 'small'))); }
    const facts = h('dl', { class: 'facts-grid spaced' });
    const fact = (k, v) => { facts.appendChild(h('dt', null, k)); facts.appendChild(h('dd', null, v)); };
    if (p.row) { fact('Said in', link(roomTitle(p.row.room), () => choose({ v: 'room', room: p.row.room }))); }
    fact('Quoted by', recLink(r, W.kindWord(r.kind) + ', ' + roomTitle(r.room_id, doc0.room_label)));
    fact('Checked', 'Before the ' + W.kindWord(r.kind).toLowerCase() + ' was published, the host checked these are the exact words of that row (bold and italic marks aside), ' + W.spokenBy(p.speaker) + '; it publishes no quote that is not.');
    fact('Stored', link('The quote as stored', () => go({ v: 'field', address: { kind: 'record', id: r.id, field: ['quotes', route.index] }, label: 'Stored quote' }), 'small'));
    doc.appendChild(facts);
    if (call) { doc.appendChild(addressBlock(call, 'Address of the row')); }
    return { node: doc, address: call || null };
  }

  /** A draft's decisions in publication order, from where `before` stopped, at most DECISION_PAGES pages per look.
   * The core takes the latest decision as the acting one, so the last one read is current only once the history
   * is complete; until then the block says how many it read and reads further when asked. */
  async function decisionsFrom(address, before) {
    const out = Object.assign({ last: null, read: 0, complete: false, next: null }, before || {});
    for (let n = 0; n < DECISION_PAGES; n++) {
      const page = await client.list('history', Object.assign({ address, kinds: ['decision'], limit: 500 }, out.next ? { cursor: out.next } : {}));
      out.read += page.entries.length;
      if (page.entries.length) { out.last = page.entries[page.entries.length - 1]; }
      out.next = page.next;
      if (page.complete || !page.next) { return Object.assign(out, { complete: true, next: null }); }
    }
    return out;
  }
  async function decisionBlock(address, r, route) {
    const key = 'rec:' + r.id + ':decisions';
    const box = h('section', { class: 'block' }, h('h2', { class: 'h-group' }, 'Decision'));
    const after = r.status === 'draft' ? 'No decision yet. Until Ouroboros decides, this draft acts like its own page and stands in the story.'
      : r.status === 'candidate' ? 'Waiting for a decision. A candidate does not act yet: it stands in no story until it is accepted.'
      : r.status === 'rejected' ? 'A rejected draft no longer stands in the story; its words stay readable here.' : '';
    function draw(found, error) {
      while (box.childNodes.length > 1) { box.lastChild.remove(); }
      const dec = found.complete ? found.last : null;
      const focused = dec && route.focus === dec.id;
      const further = button('Read further', async () => {
        further.disabled = true; further.lastChild.textContent = 'Reading…';
        try { const more = await decisionsFrom(address, found); states.set(key, more); draw(more); } catch (e) { draw(found, e); }
      }, { key: 'decisions-more' });
      box.appendChild(h('div', { class: 'tray', 'data-focus': focused ? '' : null },
        focused ? h('p', { class: 'opened word crimson' }, 'The decision you opened') : null,
        dec ? h('p', null, (dec.accepted ? 'Accepted' : 'Rejected') + ' by Ouroboros on ' + W.dayYear(dec.ts) + '. ', h('span', { class: 'sec' }, '“' + previewOf(dec) + '”')) : null,
        dec && dec.chars > cpLength(dec.preview || '') ? link('Read the whole reason', () => go(recordRoute(dec))) : null,
        found.complete ? null : h('p', { class: 'meta' }, 'Read ' + plural(found.read, 'decision') + ' of its history so far; the current one comes later. ', further),
        error ? h('p', { class: 'meta' }, 'Couldn’t read further: ' + (error.message || error.code)) : null,
        after ? h('p', { class: dec ? 'meta' : '' }, after) : null));
    }
    const found = states.get(key) || await load(key, () => decisionsFrom(address));
    states.set(key, found);
    draw(found);
    return box;
  }

  async function membersBlock(address) {
    const params = { address, relation: 'members', limit: 25 };
    const st = await firstPage('rec:' + address.id + ':members', list('relations', params));
    const first = await cache.get('first:rec:' + address.id + ':members');
    return block('Folds ' + plural(first.count || st.entries.length, 'record'),
      h('div', { class: 'bleed' }, listNode(st, list('relations', params), (m) => (m.missing
        ? h('div', { class: 'row still' }, h('p', { class: 'text' }, meta('Not in this chronicle: ' + m.id)))
        : entryRow(m.member, 'room')), { unit: 'members read', key: 'members' })),
      h('p', { class: 'meta note-line' }, 'A part keeps its members by id, not by version: a member corrected later shows the correction on its own page.'));
  }

  async function accountBlock(address, r) {
    const box = h('section', { class: 'block' });
    const sel = r.selection && typeof r.selection === 'object' ? r.selection : null;
    if (sel) {
      const ids = Array.isArray(sel.replaces) ? sel.replaces : sel.replaces && sel.replaces.head ? sel.replaces.head : [];
      const total = count(sel.replaces);
      box.appendChild(h('h2', { class: 'h-group' }, (sel.shown ? 'Selected into My story on ' : 'Withdrawn from My story on ') + W.day(sel.ts)));
      box.appendChild(h('div', { class: 'tray' }, h('p', null, sel.shown
        ? 'Ouroboros chose to tell ' + (total === 1 ? 'this record' : 'these ' + fmt(total) + ' records') + ' through this account in My story. They stay whole in their rooms.'
        : 'Not shown in My story.'), sel.reason ? h('p', { class: 'sec' }, '“' + valueText(sel.reason) + '”') : null));
      if (ids.length) {
        const found = await load('entries:' + ids.join(','), () => client.entries(ids));
        box.appendChild(h('div', { class: 'rows bleed spaced' }, found.entries.map((x) => (x.missing
          ? h('div', { class: 'row still' }, h('p', { class: 'text' }, meta('Not in this chronicle: ' + x.id)))
          : entryRow(Object.assign({}, x.record, { label: x.record.room_label }))))));
      }
      if (total > ids.length && sel.replaces.address) {
        box.appendChild(h('p', { class: 'meta note-line' }, plural(total - ids.length, 'more record') + ' named. ', link('Read the whole list', () => go({ v: 'field', address: sel.replaces.address, label: 'Records it tells' }), 'small')));
      }
    }
    const params = { address, relation: 'sources', limit: 10 };
    const st = await firstPage('rec:' + address.id + ':sources', list('relations', params));
    const first = await cache.get('first:rec:' + address.id + ':sources');
    box.appendChild(h('h2', { class: 'h-group gap-top' }, 'Sources, as it cited them (' + fmt(first.count) + ')'));
    box.appendChild(listNode(st, list('relations', params), sourceRow, { rowsClass: 'srcrows', unit: 'sources read', key: 'sources' }));
    box.appendChild(h('details', { class: 'meta exact' }, h('summary', null, 'Exact citations'),
      h('div', { class: 'source' }, st.entries.map((s) => s.kind + ' ' + s.id + '\n  revision ' + s.revision + '\n  sha256 ' + s.sha256 + (s.status ? '\n  status then ' + s.status : '') + '\n').join('\n')),
      st.complete ? null : h('p', { class: 'meta' }, 'Of the sources read so far.')));
    return box;
  }
  function sourceRow(s) {
    const lines = [h('div', null, h('span', { class: 'strong' }, W.kindWord(s.kind) + ' · ' + roomTitle(s.room_id)))];
    let cited = s.revision === s.id ? 'Cited: the original words' : 'Cited: a corrected version';
    if (s.status && s.status !== 'final') { cited += ', a ' + s.status + ' then'; }
    lines.push(meta(cited));
    const later = s.later_corrections || { count: 0 };
    if (later.count) { lines.push(h('div', { class: 'change' }, word((later.count === 1 ? 'Corrected' : 'Corrected ' + fmt(later.count) + ' times') + ' after this account; not in its text', 'info'))); }
    if (s.status_now && s.status && s.status_now !== s.status) { lines.push(h('div', { class: 'change' }, word('Now ' + s.status_now, 'info'))); }
    const nested = s.nested_changes || { count: 0 };
    if (nested.count) {
      const holder = h('div', { class: 'change' });
      holder.appendChild(word(plural(nested.count, 'change') + ' inside it since', 'info'));
      holder.appendChild(h('p', { class: 'meta' }, s.kind === 'part' ? 'The part did not record member versions, so this says nothing about what the account read.' : 'Changes in the records it cites.'));
      const open = link('Show them', () => {
        open.remove();
        const key = 'rec:' + s.id + ':nested:' + (nested.next.address && nested.next.address.id) + ':' + nested.next.source;
        if (!states.get(key)) { states.set(key, { entries: [] }); }
        holder.appendChild(listNode(states.get(key), (cursor) => client.continue(Object.assign({}, nested.next, { limit: 25 }, cursor ? { cursor } : {})), (c) =>
          h('div', { class: 'nested' }, word('A ' + W.kindWord(c.kind).toLowerCase() + ' was ' + (c.event.kind === 'correction' ? 'corrected' : 'decided') + ' on ' + W.day(c.event.ts), 'info'),
            link('Open it', () => go(recordRoute({ id: c.source_id, kind: c.kind, room_id: c.room_id }, { focus: c.event.id })), 'small')), { rowsClass: 'nested-list', unit: 'changes read' }));
      }, 'small');
      holder.appendChild(open);
      lines.push(holder);
    }
    if (s.missing) { lines.push(word('Not in this chronicle: its words cannot be read here', 'err')); }
    if (!s.revision_known && !s.missing) { lines.push(word('The cited revision is not one this chronicle holds', 'err')); }
    const acts = h('div', { class: 'acts' },
      !s.missing && s.revision_known ? button('Read as cited', () => go(recordRoute(s, { rev: s.revision }))) : null,
      !s.missing && (s.current_revision !== s.revision || (s.status_now && s.status_now !== s.status)) ? button('Read now', () => go(recordRoute(s)), { quiet: true }) : null);
    return h('div', { class: 'srcrow' }, h('div', null, lines), acts);
  }

  async function factsBlock(address, r, doc0) {
    const facts = h('dl', { class: 'facts-grid' });
    const fact = (k, v) => { facts.appendChild(h('dt', null, k)); facts.appendChild(h('dd', null, v)); };
    if ('period' in doc0) { fact('Period', W.periodText(doc0)); }
    if (typeof r.room_id === 'string') { fact('Room', link(W.roomName(doc0.room_label || roomLabel(r.room_id)).name, () => choose({ v: 'room', room: r.room_id }))); }   // '' is the host's room
    if (r.folded_into) { fact('Part of', recLink({ id: r.folded_into, kind: 'part', room_id: r.room_id }, 'The part that folds it')); }
    if (doc0.told_by) { fact('Told in My story by', recLink({ id: doc0.told_by, kind: 'account', room_id: r.room_id }, 'An account')); }
    if (r.target_id) { fact(r.kind === 'correction' ? 'Corrects' : r.kind === 'decision' ? 'Decides on' : 'On', recLink({ id: r.target_id, kind: 'record', room_id: r.room_id }, 'The record it names')); }
    if (r.kind !== 'account' && ['page', 'part', 'legacy', 'gap', 'note'].includes(r.kind)) {
      const params = { address, relation: 'cited_by', limit: 100 };
      const st = await firstPage('rec:' + r.id + ':citedby', list('relations', params));
      if (st.entries.length || !st.complete) {
        fact('Cited by', listNode(st, list('relations', params), (c) => {
          const earlier = c.cited && c.cited.revision && c.cited.revision !== doc0.current_revision;
          return h('div', { class: 'cite' }, recLink(c.account, ['Account', roomTitle(c.account.room_id), W.periodWords(c.account, false)].filter(Boolean).join(', ')),
            earlier ? meta(' · cited an earlier version, ') : null,
            earlier ? link('read that version', () => go(recordRoute(r, { rev: c.cited.revision })), 'small') : null);
        }, { rowsClass: 'cites', unit: 'accounts checked', key: 'citedby' }));
      }
    }
    return facts.childNodes.length ? block('Facts and links', facts) : h('div');
  }

  function viewMark(doc0, text) {
    const m = doc0.record, address = { kind: 'record', id: m.id };
    const released = m.released && typeof m.released === 'object' ? m.released : null;
    const doc = h('article', { class: 'doc' });
    doc.appendChild(h('p', { class: 'kicker' }, meta('Mark'), released ? word('Released ' + W.day(released.ts)) : word(m.scope === 'global' ? 'Global' : 'In ' + roomTitle(m.room_id))));
    doc.appendChild(viewTitle(text, { class: 'mark-title' }));
    doc.appendChild(h('p', { class: 'byline meta' }, W.byline(m.author, m.signer) + ' · ' + W.when(m.ts)));
    const facts = h('dl', { class: 'facts-grid spaced' });
    const fact = (k, v) => { facts.appendChild(h('dt', null, k)); facts.appendChild(h('dd', null, v)); };
    fact('Scope', m.scope === 'global' ? 'Global: every room' : roomTitle(m.room_id) + ' only');
    if (m.anchor) { fact('Tied to', W.anchorText(m.anchor)); }
    const view = m.view && typeof m.view === 'object' ? m.view : null;
    fact('Shows', (view ? view.visibility : m.visibility) === 'meaning' ? 'its meaning only' : 'its full words');
    if (view) { fact('View chosen', W.day(view.ts) + ' · “' + previewOf(view) + '”'); }
    if (m.target_ref && m.target_ref.id) { fact('On', recLink({ id: m.target_ref.id, kind: 'record', room_id: m.room_id }, 'The record it marks')); }
    if (m.quote) { fact('Quote', '“' + valueText(m.quote) + '”'); }
    doc.appendChild(facts);
    if (released) { doc.appendChild(block('Released', h('div', { class: 'tray' }, h('p', null, W.dayYear(released.ts) + '. '), h('p', { class: 'sec' }, '“' + previewOf(released) + '”')))); }
    if (!doc0.complete) { doc.appendChild(h('p', { class: 'meta note-line' }, 'Its words are longer than shown here. ', link('Read them whole', () => go({ v: 'field', address: { kind: 'record', id: m.id, field: ['text'] }, label: 'Mark' }), 'small'))); }
    doc.appendChild(addressBlock(W.addressText(address)));
    shown = { kind: 'record', id: m.id, revision: doc0.revision };
    return { node: doc, address: W.addressText(address) };
  }

  /** One stored value (a field of a record, note or journal line), read whole by ranges, exactly as stored. A record
   * and a journal line are written once, so a value read from one stays as read. A value of a note's front matter
   * changes with its note: Refresh compares the value and the note by revision. */
  async function viewField(route) {
    const key = 'field:' + JSON.stringify(route.address), ofNote = route.address.kind === 'note';
    const st = await readState(key, () => client.text(route.address, { length: CHUNK }));
    if (ofNote) { shown = { kind: 'note', address: route.address, revision: st.answer.revision, note: st.answer.note_revision }; }
    const doc = h('article', { class: 'doc' });
    doc.appendChild(h('p', { class: 'kicker' }, meta(st.answer.representation === 'json' ? 'Stored value · JSON' : 'Stored value'), meta(JSON.stringify(route.address.field || []), 'mono')));
    doc.appendChild(viewTitle(route.label || 'Stored value'));
    doc.appendChild(textReader(st, (start, revision) => client.text(route.address, { start, revision, length: CHUNK }),
      (text) => h('pre', { class: 'source', 'aria-label': 'Exact stored text' }, text), () => { forget(ofNote ? heldBy(route.address) : (k) => k === key); render(0); }));
    return { node: doc };
  }

  /* ---- knowledge ------------------------------------------------------------------------------------------- */
  async function viewNote(route) {
    const address = { kind: 'note', scope: route.scope, topic: route.topic };
    const key = 'note:' + route.scope + '|' + route.topic;
    const [st, links] = await Promise.all([
      readState(key, () => client.text(address, { length: CHUNK })),
      load(key + ':links', () => client.list('relations', { address, relation: 'links', limit: 200 }))]);
    const n = st.answer.note, src = !!route.source;
    shown = { kind: 'note', address, revision: st.answer.revision };
    const doc = h('article', { class: 'doc' });
    const kicker = h('p', { class: 'kicker' }, meta(n.scope === 'global' ? 'Knowledge' : 'Project knowledge · ' + shelfLabel(n.scope)), meta(n.topic, 'mono'));
    if (n.state === 'archived') { kicker.appendChild(word('Archived')); }
    doc.appendChild(kicker);
    doc.appendChild(viewTitle(valueText(n.title) || n.topic));
    doc.appendChild(h('p', { class: 'byline meta' }, (n.path || '') + ' · ' + fmt(st.answer.bytes) + ' bytes'));
    if (n.state === 'archived' && n.archive) {
      doc.appendChild(h('div', { class: 'notice' }, h('p', null, h('b', null, 'Archived' + (n.archive.at ? ' ' + W.dayYear(n.archive.at) : '') + '. '), valueText(n.archive.reason || '')), h('p', { class: 'meta' }, 'Archived notes stay readable and keep their address.')));
    }
    const broken = n.parse_error || n.read_error || n.archive_error;
    if (broken) {
      doc.appendChild(h('div', { class: 'notice' }, h('p', null, h('b', null, 'Its metadata could not be read. '), 'The note is shown exactly as stored; no title or summary is guessed.'), h('p', { class: 'meta mono clip' }, broken)));
    }
    doc.appendChild(segment('Show', [
      { label: 'Read', chosen: !src, on: () => replaceTop({ source: false }) },
      { label: 'Source', chosen: src, on: () => replaceTop({ source: true }) }]));
    const restart = () => { forget(heldBy(address)); render(0); };
    const more = (start, revision) => client.text(address, { start, revision, length: CHUNK });
    if (src) {
      doc.appendChild(textReader(st, more, (text) => h('pre', { class: 'source', 'aria-label': 'Exact source' }, text), restart));
      doc.appendChild(addressBlock(W.addressText(address)));
      return { node: doc, address: W.addressText(address) };
    }
    if (!broken) {
      const s = n.summary;
      doc.appendChild(block('Summary, as written', h('div', { class: 'tray' }, s ? h('p', { class: 'kept' }, valueText(s)) : h('p', { class: 'meta' }, 'No summary. Nothing is generated in its place.'),
        isCut(s) && s.address ? link('Read the whole summary', () => go({ v: 'field', address: s.address, label: 'Summary' }), 'small') : null)));
    }
    const byTarget = {};
    links.entries.forEach((l) => { byTarget[valueText(l.target)] = l; });
    const linkNode = (label, dest) => {
      const l = byTarget[dest];
      if (l && l.status === 'present' && l.address) { return h('button', { type: 'button', class: 'navlink', on: { click: () => go({ v: 'note', scope: l.address.scope, topic: l.address.topic, label: label }) } }, label); }
      if (l && l.status === 'unwritten') { return h('span', { class: 'gaplink' }, label, h('small', null, ' (not written yet)')); }
      return h('span', null, label);
    };
    doc.appendChild(textReader(st, more, (text) => h('div', { class: 'prose' }, markdown(dropTitle(broken ? text : noteBody(text)), linkNode)), restart));
    // Links in: the skill's own read of this shelf, a page of notes at a time.
    const inKey = key + ':inbound';
    if (!states.get(inKey)) { states.set(inKey, { entries: [] }); }
    const reading = (a) => (a.read.continuing ? 'Still checking the links of “' + a.read.continuing.topic + '”: ' + fmt(a.read.continuing.links_read) + ' read so far' : null);
    doc.appendChild(block('Linked from', listNode(states.get(inKey), (cursor) => client.inbound(route.scope, route.topic, cursor).then((a) => ({ entries: a.notes, next: a.next, complete: a.complete, examined: a.read.notes, note: reading(a) })),
      (x) => noteRow(x), { rowsClass: 'rows bleed', unit: 'other notes on this shelf checked', empty: 'No note on this shelf links here.', key: 'inbound' })));
    if (links.entries.length) {
      const ls = states.get(key + ':linkstate') || seeded(links);
      states.set(key + ':linkstate', ls);
      doc.appendChild(block('Links to', listNode(ls, list('relations', { address, relation: 'links', limit: 200 }), (l) => h('div', { class: 'linkrow' },
        l.status === 'present' && l.address ? link(valueText(l.label), () => go({ v: 'note', scope: l.address.scope, topic: l.address.topic, label: valueText(l.label) })) : h('span', null, valueText(l.label)),
        meta('  ' + (l.address ? l.address.topic : valueText(l.target)) + (l.status === 'unwritten' ? ' · not written yet' : l.status === 'external' ? ' · outside memory' : ''))),
      { rowsClass: 'links', unit: 'links read' })));
    }
    const hist = { address, limit: 20 };
    const hs = await firstPage(key + ':versions', list('history', hist));
    if (hs.entries.length || !hs.complete) {
      doc.appendChild(block('Versions, oldest first', h('div', { class: 'bleed' }, listNode(hs, list('history', hist), (row) => versionRow(row, valueText(n.title) || n.topic), { unit: 'journal lines read', key: 'versions' }))));
    }
    doc.appendChild(addressBlock(W.addressText(address)));
    return { node: doc, address: W.addressText(address) };
  }

  /** One journal line that holds a version (knowledge, identity, World): read it, or compare it with the one before. */
  function versionRow(row, title) {
    if (row.unreadable) { return unreadableRow(row); }
    const f = row.fields || {}, at = row.address;
    const before = f.old_content && (typeof f.old_content !== 'string' || f.old_content.length);
    const facts = [];
    if (f.mode) { facts.push(meta(String(f.mode))); }
    const size = f.new_chars != null ? f.new_chars : f.new_len;
    if (size != null) { facts.push(meta(plural(size, 'character'))); }
    if (f.delta && typeof f.delta === 'object') {
      if (f.delta.summary_changed) { facts.push(meta('summary changed')); }
      if (f.delta.body_changed) { facts.push(meta('body changed')); }
    }
    const field = (name) => Object.assign({}, at, { field: [name] });
    return h('div', { class: 'row still' }, h('div', { class: 'top' }, h('span', { class: 'title' }, W.when(f.ts)), meta(String(f.writer || f.task_id || f.source_type || ''))),
      h('div', { class: 'facts' }, facts,
        f.new_content != null ? link('Read this version', () => go({ v: 'version', address: field('new_content'), label: title, ts: f.ts }), 'small') : null,
        before && f.new_content != null ? link('Compare with the version before', () => go({ v: 'compare', a: field('old_content'), b: field('new_content'), title, ts: f.ts }), 'small') : null));
  }
  function unreadableRow(row) {
    return h('div', { class: 'row still' }, h('p', { class: 'text' }, meta('A line that is not readable JSON (' + plural(row.bytes, 'byte') + ').')),
      link('Read it as stored', () => go({ v: 'field', address: row.address, label: 'Journal line' }), 'small'));
  }

  async function viewVersion(route) {
    const key = 'version:' + JSON.stringify(route.address);
    const st = await readState(key, () => client.text(route.address, { length: CHUNK }));
    const src = !!route.source;
    const doc = h('article', { class: 'doc' });
    doc.appendChild(h('p', { class: 'kicker' }, meta('Version'), meta(W.when(route.ts))));
    doc.appendChild(viewTitle(route.label || 'Version'));
    doc.appendChild(segment('Show', [
      { label: 'Read', chosen: !src, on: () => replaceTop({ source: false }) },
      { label: 'Source', chosen: src, on: () => replaceTop({ source: true }) }]));
    doc.appendChild(textReader(st, (start, revision) => client.text(route.address, { start, revision, length: CHUNK }),
      (text) => (src ? h('pre', { class: 'source' }, text) : h('div', { class: 'prose' }, markdown(dropTitle(noteBody(text))))), () => { forget((k) => k === key); render(0); }));
    doc.appendChild(h('p', { class: 'meta note-line' }, 'Kept whole in the journal line that recorded it.'));
    return { node: doc };
  }

  async function wholeText(address) {
    let answer = await client.text(address, { length: 1048576 });
    let text = answer.text;
    while (answer.next) {
      if (cpLength(text) >= COMPARE_MAX) { return { text, cut: true, total: answer.range.total }; }
      answer = await client.text(address, { start: answer.next.start, revision: answer.next.revision, length: 1048576 });
      text += answer.text;
    }
    return { text, cut: false, total: answer.range.total };
  }
  async function viewCompare(route) {
    const [A, B] = await Promise.all([load('whole:' + JSON.stringify(route.a), () => wholeText(route.a)), load('whole:' + JSON.stringify(route.b), () => wholeText(route.b))]);
    const doc = h('article', { class: 'doc' });
    doc.appendChild(h('p', { class: 'kicker' }, meta('Versions')));
    doc.appendChild(viewTitle(route.title || 'Versions'));
    doc.appendChild(h('p', { class: 'byline meta' }, 'The version before, and the one written ' + W.when(route.ts)));
    const a = A.text.split('\n'), b = B.text.split('\n');
    if (A.cut || B.cut || a.length * b.length > DIFF_CELLS) {
      doc.appendChild(h('div', { class: 'notice' }, h('p', null, h('b', null, 'Too long to compare line by line here. '), 'Both versions are kept whole; read each one.'),
        h('p', null, link('The version before', () => go({ v: 'version', address: route.a, label: route.title })), '  ·  ', link('This version', () => go({ v: 'version', address: route.b, label: route.title, ts: route.ts })))));
      return { node: doc };
    }
    const L = [];
    for (let i = 0; i <= a.length; i++) { L.push(new Uint32Array(b.length + 1)); }
    for (let i = a.length - 1; i >= 0; i--) { for (let j = b.length - 1; j >= 0; j--) { L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]); } }
    const out = h('div', { class: 'diff', 'aria-label': 'Changes between the two versions' });
    let i = 0, j = 0;
    while (i < a.length || j < b.length) {
      if (i < a.length && j < b.length && a[i] === b[j]) { if (a[i].trim()) { out.appendChild(h('div', { class: 'same' }, '  ' + a[i])); } i++; j++; }
      else if (j < b.length && (i >= a.length || L[i][j + 1] >= L[i + 1][j])) { out.appendChild(h('div', { class: 'add' }, '+ ' + b[j])); j++; }
      else { out.appendChild(h('div', { class: 'del' }, '− ' + a[i])); i++; }
    }
    doc.appendChild(out);
    doc.appendChild(h('p', { class: 'meta note-line' }, 'Both versions are kept whole in the journal, so this compares full texts.'));
    return { node: doc };
  }

  async function viewKnowledge(route) {
    const scope = route.scope || 'global', view = route.view || 'active';
    const key = 'shelf:' + scope + ':' + view, params = { scope, view, limit: 100 };
    const st = await firstPage(key, list('shelf', params));
    const wrap = h('div', { class: 'doc wide' });
    wrap.appendChild(h('p', { class: 'kicker' }, meta('Shelf: ' + shelfLabel(scope))));
    wrap.appendChild(viewTitle('Knowledge'));
    const overviewNote = st.entries.find((x) => x.topic === 'overview');
    if (overviewNote) {
      wrap.appendChild(rowButton('note:' + scope + '|overview', () => go(noteRoute(overviewNote)), [
        h('div', { class: 'top' }, h('span', { class: 'title' }, 'Overview'), meta('always active')),
        overviewNote.summary ? h('p', { class: 'text sec' }, valueText(overviewNote.summary)) : h('p', { class: 'text' }, meta('No summary'))], { class: 'row overview' }));
    }
    const controls = h('div', { class: 'controls' }, segment('Which notes', [
      { label: 'Active', chosen: view === 'active', on: () => replaceTop({ view: 'active' }) },
      { label: 'Archived', chosen: view === 'archived', on: () => replaceTop({ view: 'archived' }) },
      { label: 'All', chosen: view === 'all', on: () => replaceTop({ view: 'all' }) }]));
    const fi = h('input', { type: 'search', class: 'ui-control', 'aria-label': 'Filter the notes read so far by title, path or summary', placeholder: 'Filter notes', autocomplete: 'off' });
    fi.value = route.filter || '';
    fi.addEventListener('input', () => { current().filter = fi.value; const old = wrap.querySelector('.shelf'); old.replaceWith(shelfList()); });
    controls.appendChild(h('label', { class: 'search filter' }, icon('search'), fi));
    // The global shelf or one project's own shelf, never every project stacked: the first few shelves read, the
    // open one always among them, and More shelves for the rest.
    const read = overview.shelves || { entries: [] }, present = (read.entries || []).filter((s) => s.present);
    const open = present.find((s) => s.scope === scope) || { scope };
    let choices = present.slice(0, SHELF_CHOICES);
    if (!choices.includes(open)) { choices = choices.slice(0, SHELF_CHOICES - 1).concat([open]); }
    const more = !readToEnd(read) || present.some((s) => !choices.includes(s));
    if (choices.length > 1 || more) {
      controls.appendChild(segment('Shelf', choices.map((s) => ({ label: shelfLabel(s.scope), chosen: s.scope === scope, on: () => replaceTop({ scope: s.scope, view: 'active', filter: '' }) }))));
    }
    if (more) { controls.appendChild(button('More shelves', () => go({ v: 'shelves' }), { key: 'shelves-more' })); }
    wrap.appendChild(controls);
    function shelfList() {
      const f = (current().filter || '').toLowerCase();
      let last = null;
      const box = h('div', { class: 'shelf bleed' });
      box.appendChild(listNode(st, list('shelf', params), (n) => {
        if (overviewNote && n.topic === 'overview' && n.scope === scope) { return null; }   // shown above; a later page keeps it in its row
        if (f && ((valueText(n.title) + ' ' + n.topic + ' ' + valueText(n.summary)).toLowerCase().indexOf(f) < 0)) { return null; }
        const g = n.topic.indexOf('/') > 0 ? n.topic.split('/')[0] : '(top level)';
        const frag = document.createDocumentFragment();
        if (g !== last) { frag.appendChild(h('h2', { class: 'h-group month' }, g)); last = g; }
        frag.appendChild(noteRow(n));
        return frag;
      }, { unit: 'notes read', empty: f ? 'No note title, path or summary read so far contains “' + current().filter + '”.' : view === 'archived' ? 'No archived notes on this shelf.' : 'No notes here.', key: key }));
      if (f && !st.complete) { box.appendChild(h('p', { class: 'meta note-line' }, 'The filter looks only at the notes read so far.')); }
      return box;
    }
    wrap.appendChild(shelfList());
    wrap.appendChild(addressBlock("knowledge_list(scope='" + scope + "')", 'Address', false));
    return { node: wrap };
  }

  /** Every shelf, a page at a time from where the overview's first page stopped; a shelf with notes opens them. */
  async function viewShelves() {
    if (!states.get('shelves')) { states.set('shelves', seeded(overview.shelves)); }
    const fetch = (cursor) => client.list('shelves', Object.assign({ limit: 100 }, cursor ? { cursor } : {})).then((page) => {
      const known = overview.shelves;
      if (known.next === cursor) {   // the page that follows what the list of places holds: names and the count follow it
        known.entries = known.entries.concat(page.entries); known.next = page.next; known.complete = readToEnd(page);
        redrawNav();
      }
      return page;
    });
    return { node: h('div', { class: 'doc wide' }, viewTitle('Shelves'),
      h('p', { class: 'byline meta' }, 'The global shelf, then each Project’s own.'),
      h('div', { class: 'bleed spaced' }, listNode(states.get('shelves'), fetch, (s) => (s.present
        ? rowButton('shelf:' + s.scope, () => go({ v: 'knowledge', scope: s.scope }), [h('div', { class: 'top' }, h('span', { class: 'title' }, shelfLabel(s.scope)), meta(s.scope))])
        : h('div', { class: 'row still' }, h('div', { class: 'top' }, h('span', { class: 'title' }, shelfLabel(s.scope)), meta('no notes yet')))),
      { unit: 'shelves read', key: 'shelves' }))) };
  }

  async function viewMarks() {
    const act = await firstPage('marks:active', list('marks', { state: 'active', limit: 50 }));
    const rel = await firstPage('marks:released', list('marks', { state: 'released', limit: 50 }));
    return { node: h('div', { class: 'doc wide' }, viewTitle('Marks'),
      h('p', { class: 'byline meta' }, 'What Ouroboros marked as mattering, with where each one applies and what it is tied to.'),
      block('Active', h('div', { class: 'bleed' }, listNode(act, list('marks', { state: 'active', limit: 50 }), markRow, { unit: 'marks read', empty: 'No active marks.', key: 'marks-a' }))),
      block('Released', h('div', { class: 'bleed' }, listNode(rel, list('marks', { state: 'released', limit: 50 }), markRow, { unit: 'marks read', empty: 'No released marks.', key: 'marks-r' })))) };
  }

  /* ---- plain sources ------------------------------------------------------------------------------------------ */
  function docTitle(d) {
    const m = /^Project (.+) (workpad|journal)$/.exec(d.title || '');
    if (!m) { return d.title; }
    const s = ((overview.shelves && overview.shelves.entries) || []).find((x) => x.project_id === m[1]);
    return (s && s.name ? s.name : m[1]) + ' · ' + m[2];
  }
  function docRoute(d) {
    return d.address.kind === 'journal' ? { v: 'journal', journal: d.address.journal, label: docTitle(d) }
      : { v: 'doc', source: d.address.source, label: docTitle(d) };
  }
  async function viewSources() {
    if (!states.get('documents')) { states.set('documents', seeded(overview.documents)); }
    return { node: h('div', { class: 'doc wide' }, viewTitle('Other sources'),
      h('div', { class: 'bleed spaced' }, listNode(states.get('documents'), list('documents', { limit: 100 }), (d) => (d.exists
        ? rowButton('doc:' + d.key, () => go(docRoute(d)), [h('div', { class: 'top' }, h('span', { class: 'title' }, docTitle(d)), meta(d.path || '')), h('p', { class: 'text sec' }, d.representation === 'jsonl' ? 'An append-only journal' : String(d.representation || ''))])
        : h('div', { class: 'row still' }, h('div', { class: 'top' }, h('span', { class: 'title' }, docTitle(d)), meta('not written yet')))), { unit: 'sources read' })),
      h('p', { class: 'meta note-line wide-gap' }, 'Raw chat, mail, settings, secrets and tool logs are not memory and are not shown.')) };
  }

  async function viewDoc(route) {
    const address = { kind: 'file', source: route.source }, key = 'file:' + route.source, src = !!route.source_view;
    const st = await readState(key, () => client.text(address, { length: CHUNK }));
    const a = st.answer;
    shown = { kind: 'file', address, revision: a.revision };
    const doc = h('article', { class: 'doc' });
    doc.appendChild(h('p', { class: 'kicker' }, meta('File'), meta(a.path || '', 'mono')));
    doc.appendChild(viewTitle(route.label || a.title));
    doc.appendChild(h('p', { class: 'byline meta' }, fmt(a.bytes) + ' bytes' + (a.representation && a.representation !== 'markdown' ? ' · ' + a.representation : '')));
    doc.appendChild(segment('Show', [
      { label: 'Read', chosen: !src, on: () => replaceTop({ source_view: false }) },
      { label: 'Source', chosen: src, on: () => replaceTop({ source_view: true }) }]));
    doc.appendChild(textReader(st, (start, revision) => client.text(address, { start, revision, length: CHUNK }),
      (text) => (src ? h('pre', { class: 'source', 'aria-label': 'Exact source' }, text) : h('div', { class: 'prose' }, markdown(dropTitle(text)))),
      () => { forget((k) => k === key); render(0); }));
    if (a.journal) {
      const params = { address, limit: 20 };
      const hs = await firstPage(key + ':history', list('history', params));
      doc.appendChild(block('Its journal, oldest first', h('div', { class: 'bleed' }, listNode(hs, list('history', params),
        (row) => (row.fields && 'new_content' in row.fields ? versionRow(row, route.label || a.title) : journalRow(row)), { unit: 'journal lines read', empty: 'Nothing recorded yet.' }))));
    }
    doc.appendChild(addressBlock(a.path, 'Path', false));
    return { node: doc, address: a.path };
  }

  function journalRow(row) {
    if (row.unreadable) { return unreadableRow(row); }
    const f = row.fields || {};
    const text = f.text != null ? f.text : f.content != null ? f.content : f.summary != null ? f.summary : null;
    const known = ['ts', 'text', 'content', 'summary'];
    const other = Object.keys(f).filter((k) => !known.includes(k) && (typeof f[k] === 'string' || typeof f[k] === 'number') && String(f[k]).length < 60).slice(0, 4);
    return h('div', { class: 'row still' }, h('div', { class: 'top' }, meta(W.when(f.ts)), other.length ? meta(other.map((k) => k + ' ' + f[k]).join(' · ')) : null),
      text != null ? h('p', { class: 'text kept' }, valueText(text)) : null,
      h('div', { class: 'facts' }, isCut(text) && text.address ? link('Read whole', () => go({ v: 'field', address: text.address, label: 'Journal entry' }), 'small') : null,
        link('The line as stored', () => go({ v: 'field', address: row.address, label: 'Journal line' }), 'small')));
  }

  async function viewJournal(route) {
    const address = { kind: 'journal', journal: route.journal }, params = { address, limit: 50 };
    const st = await firstPage('journal:' + route.journal, list('history', params));
    return { node: h('div', { class: 'doc wide' }, h('p', { class: 'kicker' }, meta('Journal')), viewTitle(route.label || route.journal),
      h('p', { class: 'byline meta' }, 'An append-only journal, oldest first.'),
      h('div', { class: 'bleed spaced' }, listNode(st, list('history', params), journalRow, { unit: 'lines read', empty: 'Nothing recorded yet.' }))) };
  }

  /* ---- search: the skill's own bounded read, a step at a time ------------------------------------------- */
  async function viewSearch(route) {
    const key = 'search:' + route.q;
    let st = states.get(key);
    if (!st) {
      const first = await load('first:' + key, () => client.search(route.q, null));
      st = { steps: [first], next: first.next, complete: first.complete };
      states.set(key, st);
    }
    const box = h('div', { class: 'doc wide' });
    box.appendChild(h('div', { class: 'search-again' }, searchField(route.q)));
    const head = h('p', { class: 'byline meta' });
    const groups = h('div');
    const foot = h('div', { class: 'pager-foot' });
    const names = { records: 'Records', notes: 'Knowledge', files: 'Other sources' };
    const sections = {};
    const snip = (s) => h('p', { class: 'text sec clamp3' }, (s.clipped_start ? '…' : '') + s.before, h('mark', null, s.match), s.after + (s.clipped_end ? '…' : ''));
    function hitRow(hit) {
      const it = hit.item;
      if (hit.group === 'records') {
        const target = it.kind === 'correction' || it.kind === 'decision' ? { id: it.target_id, kind: 'record', room_id: it.room_id } : it;
        return rowButton('hit:' + it.id, () => go(recordRoute(target, it.kind === 'correction' || it.kind === 'decision' ? { focus: it.id } : null)),
          [h('div', { class: 'top' }, meta([W.kindWord(it.kind), roomTitle(it.room_id), W.DATED.includes(it.kind) ? W.periodWords(it, false) : W.day(it.ts)].filter(Boolean).join(' · ')), W.statusWord(it.status) ? h('span', { class: 'right' }, word(W.statusWord(it.status))) : null), snip(hit.snippet)]);
      }
      if (hit.group === 'notes') {
        return rowButton('hit:' + it.scope + '|' + it.topic, () => go(noteRoute(it)), [h('div', { class: 'top' }, h('span', { class: 'title' }, valueText(it.title) || it.topic), meta(it.topic + (it.state === 'archived' ? ' · archived' : ''))), snip(hit.snippet)]);
      }
      return rowButton('hit:' + it.key, () => go(docRoute(it)), [h('div', { class: 'top' }, h('span', { class: 'title' }, docTitle(it))), snip(hit.snippet)]);
    }
    function add(step) {
      step.hits.forEach((hit) => {
        if (!sections[hit.group]) {
          sections[hit.group] = h('div', { class: 'rows bleed' });
          groups.appendChild(h('section', { class: 'block' }, h('h2', { class: 'h-group' }, names[hit.group]), sections[hit.group]));
        }
        sections[hit.group].appendChild(hitRow(hit));
      });
    }
    function sayHead() {
      const total = st.steps.reduce((n, s) => n + s.hits.length, 0);
      const read = st.steps.reduce((acc, s) => { Object.keys(s.read).forEach((k) => { acc[k] = (acc[k] || 0) + s.read[k]; }); return acc; }, {});
      head.textContent = plural(total, 'match', 'matches') + ' for “' + route.q + '” · literal, any letter case · ' +
        (st.complete ? 'searched all of it: ' : 'read so far: ') + plural(read.records || 0, 'record') + ', ' + plural(read.notes || 0, 'note') + ', ' + plural(read.files || 0, 'file') + '.';
      foot.textContent = '';
      if (!st.complete) { foot.append(h('span', { class: 'meta' }, 'More memory is left to search.'), button('Search further', further, { key: 'search-more' })); }
    }
    async function further() {
      const btn = foot.querySelector('button');
      if (btn) { btn.disabled = true; btn.lastChild.textContent = 'Searching…'; }
      try {
        const step = await client.search(route.q, st.next);
        st.steps.push(step); st.next = step.next; st.complete = step.complete;
        add(step); sayHead();
      } catch (e) {
        foot.textContent = '';
        foot.append(h('p', { class: 'meta' }, 'Couldn’t search further: ' + (e.message || e.code)), button('Try again', further));
      }
    }
    st.steps.forEach(add);
    sayHead();
    const scope = st.steps[0].scope;
    box.appendChild(viewTitle('Search for “' + route.q + '”', { class: 'sr' }));
    box.append(head, groups, foot, h('p', { class: 'meta note-line wide-gap' }, 'Searches ' + scope + '.'));
    return { node: box };
  }

  function viewFor(route) {
    switch (route.v) {
      case 'room': return viewRoom(route);
      case 'story': return viewStory(route);
      case 'record': return viewRecord(route);
      case 'field': return viewField(route);
      case 'quote': return viewQuote(route);
      case 'note': return viewNote(route);
      case 'version': return viewVersion(route);
      case 'compare': return viewCompare(route);
      case 'knowledge': return viewKnowledge(route);
      case 'shelves': return viewShelves();
      case 'marks': return viewMarks();
      case 'doc': return viewDoc(route);
      case 'journal': return viewJournal(route);
      case 'sources': return viewSources();
      case 'search': return viewSearch(route);
      default: return Promise.resolve({ node: h('p', { class: 'meta empty' }, 'Unknown place.') });
    }
  }

  /* ---- the list of places (B) ----------------------------------------------------------------------------- */
  function nav(active) {
    function item(label, route, sub, key, title) {
      return h('button', { type: 'button', class: 'row', 'data-key': 'nav:' + key, title: title || null, 'aria-current': active === key ? 'true' : null, on: { click: () => choose(route) } },
        h('div', { class: 'top' }, h('span', { class: 'title' }, label)), sub ? h('div', { class: 'facts' }, meta(sub)) : null);
    }
    const o = overview;
    const list = h('nav', { class: 'nav', 'aria-label': 'Places in memory' });
    if (o.source.mode !== 'core') {   // never below the fold: what is shown is not this installation's memory
      list.appendChild(h('p', { class: 'source-note meta', role: 'note' }, 'Not read from Ouroboros: ' + o.source.label));
    }
    list.appendChild(h('div', { class: 'nav-top' }, searchField(''),
      h('button', { type: 'button', class: 'btn btn-ghost compact icon-only', 'aria-label': 'Refresh: read what changed', title: 'Refresh', 'data-key': 'nav:refresh', disabled: refreshing ? true : null, on: { click: refresh } }, icon('refresh'))));
    if (refreshNote) { list.appendChild(h('p', { class: 'meta refresh-note', role: 'status' }, refreshNote)); }
    list.appendChild(h('h2', { class: 'h-group' }, 'Story'));
    list.appendChild(item('My story', { v: 'story' }, 'across rooms, as it stands', 'story'));
    list.appendChild(h('h2', { class: 'h-group' }, 'Rooms'));
    if (o.chronicle && o.chronicle.state === 'absent') { list.appendChild(h('p', { class: 'meta nav-empty' }, 'No chronicle in this installation yet.')); }
    const all = rooms(), host = all.filter((r) => r.room_id === ''), ordered = all.filter((r) => r.room_id !== '').concat(host);
    ordered.forEach((r) => {
      const name = W.roomName(valueText(r.label));
      list.appendChild(item(name.name, { v: 'room', room: r.room_id, label: r.label }, plural(r.records, 'record'), 'room:' + r.room_id, name.full));
    });
    if (!o.rooms.complete) {
      list.appendChild(h('div', { class: 'nav-more' }, button('More rooms', async () => {
        const page = await client.list('rooms', { cursor: o.rooms.next, limit: 200 });
        o.rooms.entries = o.rooms.entries.concat(page.entries); o.rooms.next = page.next; o.rooms.complete = page.complete;
        redrawNav();
      })));
    }
    list.appendChild(h('h2', { class: 'h-group' }, 'Kept across rooms'));
    const shelves = (o.shelves.entries || []).filter((s) => s.present).length;   // of the shelves read so far
    list.appendChild(item('Knowledge', { v: 'knowledge' }, readToEnd(o.shelves) ? plural(shelves, 'shelf', 'shelves') : fmt(shelves) + '+ shelves', 'knowledge'));
    list.appendChild(item('Marks', { v: 'marks' }, 'active and released', 'marks'));
    const identity = (o.documents.entries || []).find((d) => d.address.kind === 'file' && d.address.source === 'identity');
    if (identity) { list.appendChild(item('Identity', { v: 'doc', source: 'identity', label: 'Identity' }, identity.exists ? 'with its versions' : 'not written yet', 'doc:identity')); }
    list.appendChild(item('Other sources', { v: 'sources' }, 'scratchpad, World, Project files', 'sources'));
    return list;
  }
  function redrawNav() {
    const old = frame.querySelector('.nav');
    if (!old) { return; }
    const top = old.scrollTop, fresh = nav(narrow() && current().v === 'home' ? '' : activeKey());
    old.replaceWith(fresh);
    fresh.scrollTop = top;
  }

  /* ---- Refresh: ask the chronicle what was written since; re-read only what that touches ------------------ */
  async function refresh() {
    if (refreshing || !overview || disposed) { return; }
    refreshing = true;
    refreshNote = 'Checking for changes…';
    redrawNav();
    const since = W.clock(new Date());
    let changed = [], again = false, pos = position;
    try {
      if (!pos || !pos.record_id) { again = true; } else {
        for (let round = 0; round < 10; round++) {
          const a = await client.changes(pos, 500);
          if (!a.since_known) { again = true; break; }
          changed = changed.concat(a.entries); pos = a.position;
          if (a.complete) { break; }
          if (round === 9) { again = true; }
        }
      }
    } catch (e) {
      if (e && e.code === 'position_mismatch') { again = true; } else {
        refreshing = false; refreshNote = 'Couldn’t check for changes: ' + (e.message || e.code); redrawNav(); return;
      }
    }
    let fresh;
    try { fresh = await client.overview(); } catch (e) {
      refreshing = false; refreshNote = 'Couldn’t check for changes: ' + (e.message || e.code); redrawNav(); return;
    }
    if (disposed) { return; }
    overview = fresh;
    position = again ? fresh.position : pos;
    const route = current();
    let touched = again;
    if (again) {
      cache.clear(); states.clear();
    } else if (changed.length) {
      const roomsHit = new Set(changed.map((c) => c.room_id)), ids = new Set();
      changed.forEach((c) => { ids.add(c.id); if (c.target_id) { ids.add(c.target_id); } });
      const kinds = new Set(changed.map((c) => c.kind));
      // Chronicle-derived answers that may differ now; knowledge and files are checked by revision below.
      forget((k) => k.startsWith('story') || k.startsWith('marks') || k.startsWith('search:') || k.startsWith('entries:')
        || [...roomsHit].some((r) => k.startsWith('room:' + r + ':')) || k.startsWith('rec:'));
      touched = route.v === 'story' || (route.v === 'room' && roomsHit.has(route.room))
        || (route.v === 'record' && (ids.has(route.id) || kinds.has('account') || kinds.has('correction') || kinds.has('decision') || kinds.has('selection')))
        || (route.v === 'marks' && (kinds.has('mark') || kinds.has('mark_view') || kinds.has('mark_release')));
    }
    // Knowledge and plain files have no change feed: the open one is compared by its revision (an open value of a note's
    // front matter by its own and its note's, in one read), an open shelf by its first page (a longer shelf says so:
    // what follows that page was not compared).
    let openError = '', partly = '', reread = false;   // reread: the open note, file or value changed and is read again
    try {
      if (!again && (shown.kind === 'note' || shown.kind === 'file') && ['note', 'doc', 'field'].includes(route.v)) {
        const now = await client.text(shown.address, { length: 1 });
        if (now.revision !== shown.revision || now.note_revision !== shown.note) { forget(heldBy(shown.address)); touched = reread = true; }
      }
      if (!again && route.v === 'knowledge') {
        const key = 'shelf:' + (route.scope || 'global') + ':' + (route.view || 'active');
        const before = states.get(key);
        const now = await client.list('shelf', { scope: route.scope || 'global', view: route.view || 'active', limit: 100 });
        const sig = (es) => JSON.stringify(es.slice(0, 100).map((e) => [e.topic, e.state, valueText(e.title), valueText(e.summary)]));
        if (!before || sig(before.entries) !== sig(now.entries)) { forget((k) => k.startsWith('shelf:')); touched = reread = true; }
        if (!readToEnd(now)) { partly = 'Only this shelf’s first 100 notes were compared.'; }
      }
    } catch (e) {
      if (e && e.code === 'not_found') { if (shown.address) { forget(heldBy(shown.address)); reread = true; } touched = true; } else { openError = 'Couldn’t check the open note or file: ' + (e.message || e.code); }
    }
    forget((k) => k.startsWith('documents') || k === 'shelves' || (k.startsWith('shelf:') && route.v !== 'knowledge'));
    refreshing = false;
    const n = changed.length;
    // The chronicle's answer, then what was not or could not be checked: neither is covered by "Nothing new". An open
    // note, file or value that changed is said first, in the announcement's words.
    refreshNote = (again ? 'Read again from the start at ' + since + '.'
      : (reread ? 'This place changed and was read again · ' : '')
        + (n ? plural(n, 'new record') + ' since the last check · ' : reread ? '' : 'Nothing new · ') + 'checked ' + since + '.')
      + (partly ? ' ' + partly : '') + (openError ? ' ' + openError : '');
    announce(touched ? 'This place changed and was read again' + (openError ? '. ' + openError : '') : refreshNote);
    if (touched) { const sc = scroller(); render(sc ? sc.scrollTop : 0); } else { redrawNav(); }
  }

  /* ---- the frame ---------------------------------------------------------------------------------------------- */
  function defaultPlace() {
    const main = rooms().find((r) => r.room_id === '1') || rooms().find((r) => r.room_id !== '');
    return main ? { v: 'room', room: main.room_id, label: main.label } : { v: 'story' };
  }
  function normalize() {   // a phone starts from the list of places; a wide card shows that list beside every place
    if (narrow()) { if (stack[0].v !== 'home') { stack.unshift({ v: 'home' }); scrolls.unshift(0); } return; }
    while (stack.length > 1 && stack[0].v === 'home') { stack.shift(); scrolls.shift(); }
    if (stack[0].v === 'home') { stack[0] = defaultPlace(); }
  }
  function pageBar(route) {
    const prev = stack[stack.length - 2];
    const bar = h('div', { class: 'bar' });
    if (prev) { bar.appendChild(button(routeLabel(prev), back, { quiet: true, icon: 'back', aria: 'Back to ' + routeLabel(prev) })); }
    bar.appendChild(h('span', { class: 'where meta' }, prev ? '' : routeLabel(route)));
    return bar;
  }
  function loadingNode() {
    const p = h('p', { class: 'loading meta', role: 'status' }, '');
    setTimeout(() => { p.textContent = 'Reading…'; }, LOADING_DELAY_MS);
    return p;
  }
  function errorNode(err, retry) {
    const missing = err && err.code === 'not_found';
    return h('div', { class: 'doc' }, h('div', { class: 'notice' },
      h('p', null, h('b', null, missing ? 'Not found in memory. ' : 'Couldn’t read this. '), (err && err.message) || ''),
      h('p', { class: 'meta' }, (err && err.code) || 'error'),
      button('Try again', retry)));
  }
  function render(scrollTop) {
    if (disposed) { return; }
    const token = ++renderToken;
    normalize();
    shown = {};
    const route = current(), nw = narrow();
    const oldNav = frame.querySelector('.nav');
    if (oldNav && !nw) { navScroll = oldNav.scrollTop; }
    frame.className = 'atlas' + (nw ? ' narrow' : '') + (kit ? '' : ' plain');
    frame.textContent = '';
    let slot = null, bar = null;
    if (!nw) {
      const pane = h('section', { class: 'pane', 'aria-label': routeLabel(route) });
      const sc = h('div', { class: 'scroll' });
      if (stack.length > 1 || route.v !== 'room') { bar = pageBar(route); sc.appendChild(bar); }
      slot = loadingNode();
      sc.appendChild(slot);
      pane.appendChild(sc);
      const listNav = nav(activeKey());
      frame.appendChild(h('div', { class: 'panes' }, listNav, pane));
      listNav.scrollTop = navScroll;
    } else if (route.v === 'home') {
      frame.appendChild(h('div', { class: 'scroll' }, nav('')));
    } else {
      const sc = h('div', { class: 'scroll' });
      bar = pageBar(route);
      sc.appendChild(bar);
      slot = loadingNode();
      sc.appendChild(slot);
      frame.appendChild(sc);
    }
    announcer = h('div', { class: 'sr', role: 'status', 'aria-live': 'polite' });
    frame.appendChild(announcer);
    const sc = scroller();
    if (!slot) { if (sc) { sc.scrollTop = scrollTop || 0; } settleFocus(); return; }
    viewFor(route).then((v) => {
      if (token !== renderToken || disposed) { return; }
      slot.replaceWith(v.node);
      if (bar && v.address && !nw) { bar.appendChild(copyButton(v.address, 'Copy address')); }
      if (sc) { sc.scrollTop = scrollTop || 0; }
      const focused = !scrollTop && frame.querySelector('[data-focus]');
      if (focused) { focused.scrollIntoView({ block: 'nearest' }); }   // the least scroll that shows it: the title stays when it can
      settleFocus();
    }, (err) => {
      if (token !== renderToken || disposed) { return; }
      slot.replaceWith(errorNode(err, () => render(0)));
    });
  }
  /* After a change of place: Back returns to the row that opened it; going forward lands on the new title. */
  function settleFocus() {
    const want = pendingFocus;
    pendingFocus = null;
    if (!want) { return; }
    const pane = frame.querySelector('.pane') || frame;
    let el = null;
    if (want.key) { el = pane.querySelector('[data-key="' + CSS.escape(want.key) + '"]') || frame.querySelector('[data-key="' + CSS.escape(want.key) + '"]'); }
    if (!el && want.title) { el = frame.querySelector('[data-view-title]'); }
    if (el) { el.focus({ preventScroll: !!want.title }); }
  }

  function unavailable(err) {
    const notConnected = err && err.code === 'reader_not_connected';
    frame.className = 'atlas' + (narrow() ? ' narrow' : '') + (kit ? '' : ' plain');
    frame.textContent = '';
    frame.appendChild(h('div', { class: 'state', role: 'alert' },
      h('p', { class: 'kicker' }, meta('Memory Atlas')),
      h('h1', null, notConnected ? 'Can’t read memory here' : 'Couldn’t read memory'),
      h('p', { class: 'state-text' }, (err && err.message) || 'The widget’s routes did not answer.'),
      notConnected ? null : h('p', { class: 'meta' }, (err && err.code) || 'error'),
      h('div', { class: 'state-acts' }, button('Try again', () => start()))));
  }

  /* Keys: Escape goes back; nothing else is taken from the page. */
  function onKey(e) {
    const t = e.target, editing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    if (e.key === 'Escape' && !editing && stack.length > 1) { e.preventDefault(); back(); }
  }
  let resizeTimer = 0, lastNarrow = narrow();
  function onResize() {
    clearTimeout(resizeTimer);
    // Only a change between phone and wide arrangement redraws; a width change inside one keeps the reading.
    resizeTimer = setTimeout(() => { if (overview && narrow() !== lastNarrow) { lastNarrow = narrow(); const sc = scroller(); render(sc ? sc.scrollTop : 0); } }, 60);
  }
  document.addEventListener('keydown', onKey);
  window.addEventListener('resize', onResize);

  async function start(initial) {
    if (disposed) { return; }
    frame.textContent = '';
    frame.appendChild(h('div', { class: 'state' }, loadingNode()));
    cache.clear(); states.clear();
    try { overview = await client.overview(); } catch (err) { if (!disposed) { unavailable(err); } return; }
    if (disposed) { return; }
    position = overview.position;
    lastNarrow = narrow();
    stack = initial && initial.length ? initial.slice() : [{ v: 'home' }];
    scrolls = [];
    render(0);
  }
  start();

  return {
    dispose() {
      disposed = true;
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', onResize);
      clearTimeout(resizeTimer);
      cache.clear(); states.clear();
      frame.remove();
    },
    /** Test and harness hook: open a given stack of routes (the same routes the rows push). */
    open(routes) { if (overview) { stack = routes.slice(); scrolls = []; pendingFocus = { title: true }; render(0); } },
    route() { return stack.length ? Object.assign({}, current()) : null; },
    refresh,
    position() { return position; },
  };
}
