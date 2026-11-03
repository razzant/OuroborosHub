/* DOM built from nodes only: memory text is never parsed as HTML.
 *
 * `h(tag, attrs, ...children)` makes an element (strings become text nodes). `markdown(text, links)` lays out
 * a subset of Markdown for reading: headings, paragraphs (single newlines are soft), lists, block quotes,
 * fenced code, rules and pipe tables (a `\|` stays in its cell); inline **bold**, *em*, `code` and
 * [label](dest). It asks `links(label, dest)` for the node a link becomes. Anything else (raw HTML, images,
 * nested lists, footnotes) stays literal text. The exact text is always the Source view, never this layout.
 */
export function h(tag, attrs) {
  const el = document.createElement(tag);
  if (attrs) {
    Object.keys(attrs).forEach((k) => {
      const v = attrs[k];
      if (v == null || v === false) { return; }
      if (k === 'class') { el.className = v; } else if (k === 'text') { el.textContent = v; } else if (k === 'on') {
        Object.keys(v).forEach((ev) => { el.addEventListener(ev, v[ev]); });
      } else { el.setAttribute(k, v === true ? '' : v); }
    });
  }
  for (let i = 2; i < arguments.length; i++) { add(el, arguments[i]); }
  return el;
}

export function add(el, c) {
  if (c == null || c === false) { return; }
  if (Array.isArray(c)) { c.forEach((x) => add(el, x)); return; }
  el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
}

function inline(text, links) {
  const out = [], re = /(\*\*([^*]+)\*\*|\*([^*\s][^*]*)\*|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\))/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) { out.push(text.slice(last, m.index)); }
    if (m[2]) { out.push(h('strong', null, m[2])); } else if (m[3]) { out.push(h('em', null, m[3])); } else if (m[4]) { out.push(h('code', null, m[4])); } else {
      out.push(links ? links(m[5], m[6]) : h('span', null, m[5]));
    }
    last = re.lastIndex;
  }
  if (last < text.length) { out.push(text.slice(last)); }
  return out;
}

/** Cells of one table row: `|` splits unless escaped (`\|`) or inside a code span. */
function cells(row) {
  const s = row.trim().replace(/^\|/, '').replace(/\|$/, '');
  const out = [];
  let cell = '', code = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === '\\' && s[i + 1] === '|') { cell += '|'; i++; continue; }
    if (c === '`') { code = !code; }
    if (c === '|' && !code) { out.push(cell.trim()); cell = ''; continue; }
    cell += c;
  }
  out.push(cell.trim());
  return out;
}

const FENCE = /^\s*(```|~~~)/;
const HEAD = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const ITEM = /^(\s*)([-*+]|\d+[.)])\s+/;
const QUOTE = /^\s*>\s?/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const BLOCK = /^(#{1,6}\s|\s*\||\s*([-*+]|\d+[.)])\s|\s*>|\s*(```|~~~))/;

export function markdown(text, links) {
  const frag = document.createDocumentFragment(), lines = String(text || '').split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const fence = FENCE.exec(line);
    if (fence) {
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) { body.push(lines[i]); i++; }
      i++;
      frag.appendChild(h('pre', { class: 'code' }, h('code', null, body.join('\n'))));
      continue;
    }
    const hm = HEAD.exec(line);
    if (hm) { frag.appendChild(h('h' + Math.min(hm[1].length, 3), null, inline(hm[2], links))); i++; continue; }
    if (RULE.test(line)) { frag.appendChild(h('hr')); i++; continue; }
    if (/^\s*\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(lines[i]); i++; }
      const all = rows.map(cells);
      const head = all[0], body = all.slice(/^[\s|:-]+$/.test(rows[1] || '') ? 2 : 1);
      frag.appendChild(h('div', { class: 'tablewrap' }, h('table', null,
        h('thead', null, h('tr', null, head.map((c) => h('th', null, inline(c, links))))),
        h('tbody', null, body.map((r) => h('tr', null, r.map((c) => h('td', null, inline(c, links)))))))));
      continue;
    }
    if (QUOTE.test(line)) {
      const quoted = [];
      while (i < lines.length && QUOTE.test(lines[i])) { quoted.push(lines[i].replace(QUOTE, '')); i++; }
      frag.appendChild(h('blockquote', null, markdown(quoted.join('\n'), links)));
      continue;
    }
    const lm = ITEM.exec(line);
    if (lm) {
      const list = h(/\d/.test(lm[2]) ? 'ol' : 'ul');
      while (i < lines.length && ITEM.test(lines[i])) { list.appendChild(h('li', null, inline(lines[i].replace(ITEM, ''), links))); i++; }
      frag.appendChild(list);
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !BLOCK.test(lines[i])) { para.push(lines[i]); i++; }
    if (!para.length) { para.push(line); i++; }
    frag.appendChild(h('p', null, inline(para.join(' '), links)));
  }
  return frag;
}

/** Plain text in paragraphs: a blank line starts a new one; line breaks inside one are kept as written. */
export function paragraphs(text) { return String(text || '').split(/\n{2,}/).map((p) => h('p', { class: 'kept' }, p)); }
