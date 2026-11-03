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

/** Who wrote a record, as the core signed it (`signer` is the focus words the chronicle shows). */
export function byline(author, signer) {
  const a = author || {};
  if (a.kind === 'mind') { return 'By Ouroboros' + (signer ? ', ' + signer : ''); }
  if (a.kind === 'helper') { return 'Draft by ' + (signer || 'a helper'); }
  if (a.kind === 'legacy_helper') { return 'Imported from the earlier dialogue file'; }
  if (a.kind === 'host') { return 'Recorded by the host'; }
  return a.kind ? String(a.kind) : 'Author not recorded';
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
  if (!a) { return 'until ' + dayOf(b, long); }
  if (!b) { return 'from ' + dayOf(a, long); }
  const x = parts(a), y = parts(b), M = long ? MONTH : MON;
  if (x.y !== y.y) { return dayOf(a, long) + ' ' + x.y + ' – ' + dayOf(b, long) + ' ' + y.y; }
  if (x.m !== y.m) { return dayOf(a, long) + ' – ' + dayOf(b, long); }
  return x.d === y.d ? dayOf(a, long) : x.d + '–' + y.d + ' ' + M[x.m];
}
/**
 * A dated answer's period for a title (`long`) or a row: its span in words with what qualifies it (incomplete,
 * a block's period), else the core's own period text as it stands; '' when the period is unknown and null when
 * the answer carries no period at all. Nothing is read from a write time or from text.
 */
export function periodWords(e, long) {
  if (!e || !('period' in e || 'source_period' in e)) { return null; }
  const span = e.source_period;
  if (span && (span.start || span.end)) {
    const notes = [span.incomplete ? 'incomplete' : '', e.period_basis === 'block' ? 'block period' : e.period_basis === 'mixed' ? 'partly block period' : ''].filter(Boolean);
    return spanWords(span, long) + (notes.length ? ' (' + notes.join(', ') + ')' : '');
  }
  const text = typeof e.period === 'string' ? e.period : e.period && typeof e.period.preview === 'string' ? e.period.preview + '…' : '';
  return text === 'period unknown' ? '' : text;
}
/** The core's period text exactly (a long one as its preview), for the record's facts. */
export function periodText(e) {
  return typeof e.period === 'string' ? e.period : e.period && typeof e.period.preview === 'string' ? e.period.preview + '…' : '';
}
