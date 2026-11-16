/* Words the frame builds from the contract's facts: dates, titles, bylines, statuses and addresses.
 * Nothing here decides what memory means; it only says what an answer already holds. */
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const MONTH = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

export const KIND = { page: 'Page', part: 'Part', account: 'Account', legacy: 'Earlier dialogue', gap: 'Gap', note: 'Note',
  mark: 'Mark', correction: 'Correction', decision: 'Decision', selection: 'Selection', mark_view: 'Mark view',
  mark_release: 'Mark release', activation: 'Activation', record: 'Record' };
export function kindWord(kind) { return KIND[kind] || String(kind || 'Record'); }

function parts(iso) { const p = String(iso || '').slice(0, 10).split('-'); return { y: +p[0], m: +p[1] - 1, d: +p[2] }; }
function valid(iso) { return /^\d{4}-\d{2}-\d{2}/.test(String(iso || '')); }
export function day(iso) { if (!valid(iso)) { return 'date not recorded'; } const p = parts(iso); return p.d + ' ' + MON[p.m]; }
export function dayYear(iso) { if (!valid(iso)) { return 'date not recorded'; } const p = parts(iso); return p.d + ' ' + MON[p.m] + ' ' + p.y; }
export function time(iso) { return String(iso || '').slice(11, 16); }
export function when(iso) { return valid(iso) ? dayYear(iso) + (time(iso) ? ', ' + time(iso) : '') : 'date not recorded'; }
export function monthYear(iso) { const p = parts(iso); return MONTH[p.m] + ' ' + p.y; }
export function clock(date) { return String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0'); }

export function andList(xs) { return xs.length < 2 ? xs.join('') : xs.slice(0, -1).join(', ') + ' and ' + xs[xs.length - 1]; }

/**
 * A room label as the core resolved it, split for the eye: `Project Garden sensors [chat_id=12]` reads
 * `Project Garden sensors` with `chat 12` beside it. The whole label stays in `full`.
 */
export function roomName(label) {
  const full = String(label || '');
  const m = /^(.*?)\s*\[chat_id=([^\]]+)\]$/.exec(full);
  return m ? { name: m[1] || full, chat: m[2], full } : { name: full, chat: '', full };
}

/** A transferred record: the core marked it as words written elsewhere and brought into this chronicle. */
export function isTransfer(rec) { return !!(rec && rec.transfer && typeof rec.transfer === 'object'); }
/** Display metadata only: make invisible controls and line separators visible in the attribution.
 * Stored values and the chapter's text never pass through this formatter. */
export function metadataText(v) {
  return String(v).replace(/[\p{Cf}\p{Cc}\p{Zl}\p{Zp}]/gu, (c) => '\\u{' + c.codePointAt(0).toString(16).toUpperCase() + '}');
}
export function metadataValue(v) {
  if (typeof v === 'string') { const text = metadataText(v); return text.trim() ? text : ''; }
  if (v && typeof v.preview === 'string') { return metadataText(v.preview) + '… (excerpt)'; }
  return v && v.ref ? 'recorded (open full value)' : '';
}
export function metadataAddress(v) { return v && typeof v === 'object' ? v.address || v.derived_from : null; }

/**
 * Who wrote a record, as the core signed it (`signer` is the focus words the chronicle shows). `rec` is the record
 * (or list entry) that carries the author: its `byline` (the core's own signature words) and `transfer` are used
 * only for words brought here from elsewhere, where the writer is the source's author and never the importer.
 * A `source_author` always says all three of its parts: the author, the model and what the attribution rests on,
 * each either stated or "not stated". A helper or host signature carried by a transfer says its own recorded writer,
 * operation and attribution, and nothing it does not hold. Every other record keeps its usual words; an answer
 * without these fields is worded exactly as before.
 */
export function byline(author, signer, rec) {
  const a = author || {};
  const signature = metadataValue(rec && rec.byline);
  if (!a.kind && signature) { return 'Core signature: ' + signature; }
  if (a.ref) { return 'Author ' + metadataValue(a); }
  if (a.kind === 'source_author') {
    const basis = ['source', 'importer', 'unknown'].includes(a.basis);
    if (!basis && signature) { return 'Core signature: ' + signature; }
    const label = metadataValue(a.label), model = metadataValue(a.model);
    return [label ? 'Written by ' + label : 'Author not stated', model ? 'model ' + model : 'model not stated',
      a.basis === 'source' ? 'signed in the source' : a.basis === 'importer' ? 'stated by the importer' :
        a.basis && a.basis.ref ? 'attribution ' + metadataValue(a.basis) : 'attribution not stated'].join(' · ');
  }
  if (isTransfer(rec) && (a.kind === 'legacy_helper' || a.kind === 'host')) {   // an old writer's signature, carried here as stated
    const said = [metadataValue(a.writer) && 'recorded writer ' + metadataValue(a.writer), metadataValue(a.operation) && 'operation ' + metadataValue(a.operation), metadataValue(a.attribution)].filter(Boolean);
    return [a.kind === 'host' ? 'Recorded by the host' : 'Retold by an earlier helper', ...said].join(' · ');
  }
  if (a.kind === 'mind') { return 'By Ouroboros' + (metadataValue(signer) ? ', ' + metadataValue(signer) : ''); }
  if (a.kind === 'helper') { return 'Draft by ' + (metadataValue(signer) || 'a helper'); }
  if (a.kind === 'legacy_helper') { return 'Imported from the earlier dialogue file'; }
  if (a.kind === 'host') { return 'Recorded by the host'; }
  return a.kind ? metadataValue(a.kind) : 'Author not recorded';
}

/**
 * The two dates of a transferred record, apart: `written_at` is when its source says the words were written
 * (a date or a time, as the source gave it); `ts` is when this installation published them. '' for any other record.
 */
export function transferDates(rec) {
  if (!isTransfer(rec)) { return ''; }
  const written = valid(rec.written_at) ? when(rec.written_at) : metadataValue(rec.written_at) || 'date not stated';
  return 'Written ' + written + ' (as its source states) · transferred here ' + when(rec.ts);
}

/** Who brought a record here and how, from the core's `transfer` object; any unknown part says so. */
export function transferWords(rec) {
  if (!isTransfer(rec)) { return ''; }
  const t = rec.transfer;
  if (t.ref) { return 'transfer details ' + metadataValue(t); }
  return 'by ' + (metadataValue(t.operator) || 'operator not recorded') + ' with ' + (metadataValue(t.tool) || 'tool not recorded') +
    ', task ' + (metadataValue(t.task_id) || 'not recorded');
}

/** A draft's status, as recorded; final records say nothing. */
export function statusWord(status) {
  if (status === 'draft') { return 'Draft · acting, no decision yet'; }
  if (status === 'candidate') { return 'Candidate · awaiting a decision'; }
  if (status === 'accepted') { return 'Accepted draft'; }
  if (status === 'rejected') { return 'Rejected draft'; }
  return '';
}

export function anchorText(anchor) {
  if (!anchor || typeof anchor !== 'object') { return 'no anchor'; }
  if (anchor.path) { return 'the path ' + anchor.path; }
  return (anchor.tool || 'a tool') + (anchor.text ? " with '" + anchor.text + "'" : '');
}

function call(tool, args) {
  return tool + '(' + Object.keys(args).map((k) => k + "='" + String(args[k]).replace(/\\/g, '\\\\').replace(/'/g, "\\'") + "'").join(', ') + ')';
}

/**
 * An address as the agent tool call to paste into a chat: memory_read(node_id='…') for a record (with the
 * revision when an earlier one is open), knowledge_read(topic='…', scope='…') for a note, a path for a file.
 */
export function addressText(address, extra) {
  if (!address) { return ''; }
  if (address.kind === 'record' && !address.field) {
    return call('memory_read', Object.assign({ node_id: address.id }, extra && extra.revision ? { revision: extra.revision } : {}));
  }
  if (address.kind === 'note' && !address.field) { return call('knowledge_read', { topic: address.topic, scope: address.scope }); }
  if (address.path) { return address.path; }
  return '';
}

/** Who spoke the words a record quotes, as the host checked it: the speaker's class, never a guessed name. */
export const SPEAKER = { human: 'A person', ouroboros: 'Ouroboros', child: 'A child task', host: 'The host',
  helper: 'A helper', unattributed: 'Speaker not attributed' };
export function speakerWord(speaker) { return SPEAKER[speaker] || 'Speaker not recorded'; }

/** A chat row address `row:<chat_id>@<ts>#<sha12>` as its parts (the room is the chat id), or null. */
export function parseRow(address) {
  const m = /^row:(-?\d+)@(.+)#([0-9a-f]{12})$/.exec(String(address || ''));
  return m ? { room: m[1], ts: m[2], sha: m[3] } : null;
}

/** The agent's read of exactly that chat row, to paste into a chat: memory_read(rows=true, …, from=row, to=row). */
export function rowAddressText(address) {
  const row = parseRow(address);
  if (!row) { return ''; }
  const q = (v) => "'" + String(v).replace(/\\/g, '\\\\').replace(/'/g, "\\'") + "'";
  return 'memory_read(rows=true, room_id=' + q(row.room) + ', from=' + q(address) + ', to=' + q(address) + ')';
}
const SPOKEN = { human: 'spoken by a person', ouroboros: 'spoken by Ouroboros', child: 'spoken by a child task',
  host: 'spoken by the host', helper: 'spoken by a helper', unattributed: 'with its speaker recorded as unattributed' };
export function spokenBy(speaker) { return SPOKEN[speaker] || 'by the speaker it names'; }

/* ---- a record's period, as the core dates it (period, source_period, period_basis) ---------------------------- */
/** The kinds the core dates; their period is coverage, so their write time is never shown in its place. */
export const DATED = ['page', 'part', 'account', 'legacy', 'gap'];
function dayOf(iso, long) { const p = parts(iso); return p.d + ' ' + (long ? MONTH : MON)[p.m]; }
/** `source_period` {start, end} in words: a day, a day range, months apart, or years apart (then with years). */
export function spanWords(span, long) {
  const a = span && valid(span.start) ? span.start : null, b = span && valid(span.end) ? span.end : null;
  if (!a && !b) { return ''; }
  const year = (iso) => parts(iso).y !== new Date().getFullYear() ? ' ' + parts(iso).y : '';
  if (!a) { return 'until ' + dayOf(b, long) + year(b); }
  if (!b) { return 'from ' + dayOf(a, long) + year(a); }
  const x = parts(a), y = parts(b), M = long ? MONTH : MON;
  if (x.y !== y.y) { return dayOf(a, long) + ' ' + x.y + ' – ' + dayOf(b, long) + ' ' + y.y; }
  if (x.m !== y.m) { return dayOf(a, long) + ' – ' + dayOf(b, long) + year(b); }
  return (x.d === y.d ? dayOf(a, long) : x.d + '–' + y.d + ' ' + M[x.m]) + year(a);
}
/**
 * A dated answer's period for a title (`long`) or a row: its span in words with what qualifies it (incomplete,
 * a block's period), else the core's own period text as it stands; '' when the period is unknown and null when
 * the answer carries no period at all. Nothing is read from a write time or from text.
 */
export function periodWords(e, long) {
  if (!e || !('period' in e || 'source_period' in e)) { return null; }
  const span = e.source_period;
  // A projected field is still known, not a missing date or a truthy incomplete flag.
  // The core also accepts ISO forms this display helper does not parse; keep its own words.
  // Stated dates can cross a UTC day: only shorten date-only or explicitly UTC values, without another ISO parser.
  const statedDay = (v) => v == null || (typeof v === 'string' && (/^\d{4}-\d{2}-\d{2}$/.test(v) || /(?:Z|[+-]00:00)$/.test(v)));
  const readable = span && !span.ref && [span.start, span.end].every((v) => v == null || (typeof v === 'string' && valid(v))) &&
    (span.incomplete == null || typeof span.incomplete === 'boolean') &&
    (e.period_basis !== 'stated' || [span.start, span.end].every(statedDay));
  if (readable && (span.start || span.end)) {
    const bases = Array.isArray(e.period_bases) ? e.period_bases : [];
    const mixed = e.period_basis === 'mixed' ? [bases.includes('block') || !bases.length ? 'partly block period' : '', bases.includes('stated') ? 'partly stated by its source' : ''] : [];
    const notes = [span.incomplete === true ? 'incomplete' : '', e.period_basis === 'block' ? 'block period' : '', e.period_basis === 'stated' ? 'stated by its source' : '', ...mixed].filter(Boolean);
    return spanWords(span, long) + (notes.length ? ' (' + notes.join(', ') + ')' : '');
  }
  const text = metadataValue(e.period);
  return text === 'period unknown' ? '' : text;
}
/** The core's period text exactly (a long one as its preview), for the record's facts. */
export function periodText(e) {
  return metadataValue(e.period);
}
