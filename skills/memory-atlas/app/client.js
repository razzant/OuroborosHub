/* The widget's one data seam: a client of the memory-atlas.read/2 routes over the host's bridged fetch.
 *
 * Every answer names the contract and its source ({mode, label}); the widget shows its words only when
 * the answer is a success of this contract. A refusal keeps its code (reader_not_connected, not_found,
 * revision_mismatch, position_mismatch, unavailable, invalid_parameter, reader_failed) and, for a source
 * that changed, its current revision, so the frame can say what happened instead of showing a guess.
 */
export const CONTRACT = 'memory-atlas.read/2';
export const SKILL_PREFIX = '/api/extensions/memory-atlas/';
const ROUTES = 'atlas/v2/';
const TIMEOUT_MS = 120000;   // a large source can take the host a while; a retry would only read it again

export class AtlasError extends Error {
  constructor(code, message, status, currentRevision) {
    super(message || code);
    this.name = 'AtlasError';
    this.code = code;
    this.status = status || 0;
    this.currentRevision = currentRevision || null;
  }
}

function encode(value) {
  if (Array.isArray(value)) { return value.join(','); }
  if (value && typeof value === 'object') { return JSON.stringify(value); }
  return String(value);
}

export function createClient({ fetch, prefix = SKILL_PREFIX }) {
  if (typeof fetch !== 'function') { throw new TypeError('createClient needs a fetch function'); }
  async function get(op, params) {
    const query = new URLSearchParams();
    // Only an absent value (undefined, null) is left out: '' is a value, the host's room id among them.
    Object.keys(params || {}).forEach((k) => { const v = params[k]; if (v !== undefined && v !== null) { query.set(k, encode(v)); } });
    const url = prefix + ROUTES + op + (query.toString() ? '?' + query.toString() : '');
    let response;
    try {
      response = await fetch(url, { method: 'GET', headers: { Accept: 'application/json' }, timeoutMs: TIMEOUT_MS });
    } catch (error) {
      throw new AtlasError('transport', (error && error.message) || 'The request did not reach the widget’s routes', 0);
    }
    let body = null;
    try { body = await response.json(); } catch (error) { body = null; }
    if (!body || body.contract !== CONTRACT) {
      throw new AtlasError(response.ok ? 'contract_mismatch' : 'http_' + response.status,
        response.ok ? 'The answer is not a ' + CONTRACT + ' answer' : 'The route answered ' + response.status, response.status);
    }
    if (!response.ok || body.error) {
      const e = body.error || {};
      throw new AtlasError(e.code || 'http_' + response.status, e.message || 'The route answered ' + response.status, response.status, e.current_revision);
    }
    return body;
  }
  return {
    overview: () => get('overview', {}),
    /** One page of a core catalog area; `params` may be a core continuation ({area, ...}) as it came. */
    list: (area, params) => get('list', Object.assign({ area: area }, params || {})),
    continue: (next) => { const p = Object.assign({}, next); const area = p.area; delete p.area; return get('list', Object.assign({ area: area }, p)); },
    text: (address, opts) => get('text', Object.assign({ address: address }, opts || {})),
    entries: (ids) => get('entries', { ids: JSON.stringify(ids) }),
    changes: (position, limit) => get('changes', position && position.record_id ? { sequence: position.sequence, record_id: position.record_id, limit: limit } : {}),
    search: (q, cursor) => get('search', { q: q, cursor: cursor }),
    inbound: (scope, topic, cursor) => get('inbound', { scope: scope, topic: topic, cursor: cursor }),
  };
}
