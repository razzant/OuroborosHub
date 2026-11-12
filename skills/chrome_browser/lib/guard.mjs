// Client of the host's private browser-request guard (Ouroboros `browser_policy: true`).
//
// Wire contract with ouroboros/mcp_browser_guard.py: one connection per message,
// one JSON line out, one JSON line back.
//   arm:      {"armed": true, "token": T}                                   -> {"armed": true}
//   request:  {"url", "method", "post_data", "resource_type", "body_present", "token"} -> {"block": ""} or {"block": "<reason>"}
//   evaluate: {"action": "evaluate", "url", "token"}                          -> {"block": ""} or {"block": "<reason>"}
//   intake:   {"action": "file_intake", "files": [{"path", "root"?}], "token"}
//             -> {"block": "", "files": [{"path": <retained absolute copy>, "name": <original basename>, "size", "sha256"}]}
//                or {"block": "<reason>"}
// A request is allowed only when the answer's block is exactly "", and arming succeeds only
// when armed is exactly true. Every other outcome fails closed. Neither the token nor a
// request body is ever logged, echoed in an error or passed to another process.
//
// File intake: the host admits each named file as read_file would and keeps a bounded copy; its
// answer names that copy, never the bytes. Only the copies are read here, each checked against the
// size and sha256 the host stated. A path the agent named is never opened by this server.
//
// The request message also carries the host's target decision for an address no route sees:
// a navigation target before it is opened, and a page's address and redirect hops before its
// content is returned. It is sent as the GET of a document with no body, the same line the
// route sends for a navigation, so the host answers with the same predicate either way.
import crypto from 'node:crypto';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';

export const GUARD_ENV = 'OUROBOROS_BROWSER_GUARD';
export const GUARD_TOKEN_ENV = 'OUROBOROS_BROWSER_GUARD_TOKEN';
// The host issues secrets.token_hex(32) for every browser_policy session.
const GUARD_TOKEN_FORM = /^[0-9a-f]{64}$/;
export const GUARD_TIMEOUT_MS = 10_000;
export const GUARD_MAX_RESPONSE_BYTES = 64 * 1024;
// The host reads at most this many bytes per line; a larger body is described, not sent.
export const GUARD_MAX_LINE_BYTES = 32 * 1024 * 1024;
// Agreed with the host for one file intake: at most this many files, and bytes in all.
export const INTAKE_MAX_FILES = 16;
export const INTAKE_MAX_BYTES = 20 * 1024 * 1024;

export class GuardError extends Error {
  constructor(code, message) {
    super(`${code}: ${message}`);
    this.name = 'GuardError';
    this.code = code;
  }
}

export function parseGuardEndpoint(value) {
  const text = String(value ?? '');
  const tcp = /^127\.0\.0\.1:(\d{1,5})$/.exec(text);
  if (tcp) {
    const port = Number(tcp[1]);
    if (port >= 1 && port <= 65535)
      return { kind: 'tcp', host: '127.0.0.1', port };
    throw new GuardError('BROWSER_GUARD_INVALID', 'the guard port is out of range');
  }
  if (/^\\\\[.?]\\pipe\\[^\\/:*?"<>|]+$/.test(text))
    return { kind: 'path', path: text };
  if (text.startsWith('/') && path.posix.isAbsolute(text) && !text.includes('\0'))
    return { kind: 'path', path: text };
  throw new GuardError('BROWSER_GUARD_INVALID', `${GUARD_ENV} must be 127.0.0.1:<port>, an absolute Unix socket path or a \\\\.\\pipe\\ name`);
}

// Reads the guard settings once and removes them from this process's environment. The host
// always sets both together; one without the other, or a token not in the host's form, is a
// host setup fault reported before anything is started. `detail` never contains the token.
export function takeGuardEnvironment(env = process.env) {
  const rawEndpoint = env[GUARD_ENV];
  const rawToken = env[GUARD_TOKEN_ENV];
  delete env[GUARD_ENV];
  delete env[GUARD_TOKEN_ENV];
  const hasToken = rawToken !== undefined && rawToken !== '';
  if (rawEndpoint === undefined || rawEndpoint === '') {
    if (hasToken)
      return { configured: false, problem: 'BROWSER_GUARD_INVALID', detail: `${GUARD_TOKEN_ENV} is set but ${GUARD_ENV} is not` };
    return { configured: false, problem: 'BROWSER_GUARD_REQUIRED' };
  }
  let endpoint;
  try {
    endpoint = parseGuardEndpoint(rawEndpoint);
  } catch (error) {
    return { configured: false, problem: error.code || 'BROWSER_GUARD_INVALID', detail: `${GUARD_ENV} is not a guard address this server can use` };
  }
  if (!hasToken)
    return { configured: false, problem: 'BROWSER_GUARD_INVALID', detail: `${GUARD_ENV} is set but ${GUARD_TOKEN_ENV} is missing` };
  if (!GUARD_TOKEN_FORM.test(rawToken))
    return { configured: false, problem: 'BROWSER_GUARD_INVALID', detail: `${GUARD_TOKEN_ENV} is not the 64 lowercase hex characters the host issues` };
  return { configured: true, endpoint, token: rawToken };
}

export function encodeLine(message) {
  return JSON.stringify(message) + '\n';
}

// One message, one answer. Rejects on any transport or framing problem.
export function exchange(endpoint, message, { timeoutMs = GUARD_TIMEOUT_MS, maxResponseBytes = GUARD_MAX_RESPONSE_BYTES } = {}) {
  const line = typeof message === 'string' ? message : encodeLine(message);
  return new Promise((resolve, reject) => {
    let settled = false;
    const chunks = [];
    let received = 0;
    const socket = endpoint.kind === 'tcp' ? net.connect({ host: endpoint.host, port: endpoint.port }) : net.connect({ path: endpoint.path });
    const finish = (error, value) => {
      if (settled)
        return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      if (error)
        reject(error);
      else
        resolve(value);
    };
    const timer = setTimeout(() => finish(new GuardError('BROWSER_GUARD_TIMEOUT', `no answer within ${timeoutMs} ms`)), timeoutMs);
    socket.setNoDelay?.(true);
    socket.on('connect', () => socket.write(line));
    socket.on('data', (chunk) => {
      const newline = chunk.indexOf(0x0a);
      const part = newline === -1 ? chunk : chunk.subarray(0, newline);
      received += part.length;
      if (received > maxResponseBytes)
        return finish(new GuardError('BROWSER_GUARD_PROTOCOL', 'the answer is larger than allowed'));
      chunks.push(part);
      if (newline === -1)
        return;
      let parsed;
      try {
        parsed = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      } catch {
        return finish(new GuardError('BROWSER_GUARD_PROTOCOL', 'the answer is not one JSON line'));
      }
      finish(null, parsed);
    });
    socket.on('error', (error) => finish(new GuardError('BROWSER_GUARD_UNAVAILABLE', error.code || 'connection failed')));
    socket.on('close', () => finish(new GuardError('BROWSER_GUARD_UNAVAILABLE', 'the guard closed the connection without an answer')));
  });
}

function blockOf(answer) {
  if (answer && typeof answer === 'object' && !Array.isArray(answer) && typeof answer.block === 'string')
    return answer.block;
  throw new GuardError('BROWSER_GUARD_PROTOCOL', 'the answer has no block string');
}

const nonEmptyString = (value) => typeof value === 'string' && value !== '' && !value.includes('\0');

// What the agent named, in the host's request form, or the reason it cannot be asked.
function intakeRequest(entries) {
  if (!Array.isArray(entries) || !entries.length)
    return { problem: 'no file was named' };
  if (entries.length > INTAKE_MAX_FILES)
    return { problem: `${entries.length} files were named; at most ${INTAKE_MAX_FILES} can be given in one call` };
  const files = [];
  for (const entry of entries) {
    if (!entry || !nonEmptyString(entry.path) || (entry.root !== undefined && !nonEmptyString(entry.root)))
      return { problem: 'each file needs a non-empty path, and a root, when given, must be a non-empty string' };
    files.push(entry.root === undefined ? { path: entry.path } : { path: entry.path, root: entry.root });
  }
  return { files };
}

// The host's answer for a successful intake, one retained copy per file named, or the reason it is not one.
// A name is the page's file name only: a basename, with no separator or control character.
function retainedCopies(answer, count) {
  const files = answer.files;
  if (!Array.isArray(files) || files.length !== count)
    return { problem: `it names ${Array.isArray(files) ? files.length : 'no list of'} retained files for the ${count} named` };
  let total = 0;
  for (const [index, file] of files.entries()) {
    const ok = file && typeof file === 'object'
      && nonEmptyString(file.path) && path.isAbsolute(file.path)
      && nonEmptyString(file.name) && file.name !== '.' && file.name !== '..' && !/[/\\\u0000-\u001f\u007f]/.test(file.name)
      && Number.isSafeInteger(file.size) && file.size >= 0
      && typeof file.sha256 === 'string' && /^[0-9a-f]{64}$/.test(file.sha256);
    if (!ok)
      return { problem: `retained file ${index + 1} is not an absolute path with a basename, a size and a sha256` };
    total += file.size;
  }
  if (total > INTAKE_MAX_BYTES)
    return { problem: `the retained files hold ${total} bytes, more than the ${INTAKE_MAX_BYTES} agreed` };
  return { files: files.map(({ path: where, name, size, sha256 }) => ({ path: where, name, size, sha256 })) };
}

// Exactly the bytes the host retained: a regular file (the last component not a link) of the stated
// size, with the stated sha256. Reads at most size + 1 bytes.
async function readRetained(file) {
  let handle;
  try {
    handle = await fs.promises.open(file.path, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0));
    const stat = await handle.stat();
    if (!stat.isFile())
      return { problem: 'is not a regular file' };
    if (stat.size !== file.size)
      return { problem: `has ${stat.size} bytes, not the ${file.size} the host stated` };
    const buffer = Buffer.alloc(file.size);
    let offset = 0;
    while (offset < file.size) {
      const { bytesRead } = await handle.read(buffer, offset, file.size - offset, offset);
      if (!bytesRead)
        return { problem: `ended after ${offset} of the ${file.size} bytes the host stated` };
      offset += bytesRead;
    }
    if ((await handle.read(Buffer.alloc(1), 0, 1, file.size)).bytesRead)
      return { problem: `holds more than the ${file.size} bytes the host stated` };
    if (crypto.createHash('sha256').update(buffer).digest('hex') !== file.sha256)
      return { problem: 'does not have the sha256 the host stated' };
    return { buffer };
  } catch (error) {
    return { problem: `could not be read (${error.code || 'error'})` };
  } finally {
    await handle?.close().catch(() => {});
  }
}

export class BrowserGuard {
  constructor({ endpoint, token, timeoutMs = GUARD_TIMEOUT_MS, exchangeImpl = exchange }) {
    this._endpoint = endpoint;
    this._token = token ?? '';
    this._timeoutMs = timeoutMs;
    this._exchange = exchangeImpl;
    this.counters = { allowed: 0, blocked: 0, failed_closed: 0 };
    // Browser contexts whose route is installed AND whose arming the host acknowledged.
    this._armedContexts = new WeakSet();
  }

  isArmed(context) {
    return !!context && this._armedContexts.has(context);
  }

  async _send(message) {
    return await this._exchange(this._endpoint, message, { timeoutMs: this._timeoutMs });
  }

  // Call after the context's route is installed. Resolves only on {"armed": true}, and only then
  // records that context as armed; anything else throws with the host's reason if it gave one.
  async arm(context) {
    let answer;
    try {
      answer = await this._send({ armed: true, token: this._token });
    } catch (error) {
      throw new GuardError('BROWSER_GUARD_NOT_ARMED', error.message);
    }
    if (answer && typeof answer === 'object' && answer.armed === true) {
      if (context)
        this._armedContexts.add(context);
      return true;
    }
    const reason = answer && typeof answer.block === 'string' && answer.block ? answer.block : 'the guard did not confirm arming';
    throw new GuardError('BROWSER_GUARD_NOT_ARMED', reason);
  }

  static requestMessage({ url, method, postData, resourceType, bodyPresent }, token) {
    const message = { url, method, post_data: postData ?? null, resource_type: resourceType, body_present: !!bodyPresent, token };
    const line = encodeLine(message);
    if (Buffer.byteLength(line, 'utf8') <= GUARD_MAX_LINE_BYTES)
      return line;
    // Agreed with the host: an oversized body is announced, not sent.
    return encodeLine({ url, method, post_data: null, resource_type: resourceType, body_present: true, token });
  }

  // '' allows; any other string is the reason. Never throws: an unanswered request is blocked.
  async decideRequest(facts) {
    try {
      const block = blockOf(await this._send(BrowserGuard.requestMessage(facts, this._token)));
      if (block === '')
        this.counters.allowed += 1;
      else
        this.counters.blocked += 1;
      return block;
    } catch (error) {
      this.counters.failed_closed += 1;
      return error.code ? `${error.code}: the request guard did not answer, so the request was blocked` : 'BROWSER_GUARD_UNAVAILABLE';
    }
  }

  async checkEvaluate(url) {
    try {
      return blockOf(await this._send({ action: 'evaluate', url: String(url ?? ''), token: this._token }));
    } catch (error) {
      return `${error.code || 'BROWSER_GUARD_UNAVAILABLE'}: the request guard did not answer, so the script was not run`;
    }
  }

  // The host's decision for an address no route will see (see the header). '' allows; an
  // unanswered question blocks. Not counted or listed with the routed requests: nothing was sent.
  async decideTarget(url) {
    try {
      return blockOf(await this._send(BrowserGuard.requestMessage({ url: String(url ?? ''), method: 'GET', postData: null, resourceType: 'document', bodyPresent: false }, this._token)));
    } catch (error) {
      return `${error.code || 'BROWSER_GUARD_UNAVAILABLE'}: the request guard did not answer, so the address was treated as refused`;
    }
  }

  // The host's admission of files for the page (see the header). entries: [{ path, root? }] as the agent
  // named them. Resolves { block: '', files: [{ name, buffer, size, sha256 }] }, in the order named, or
  // { block: reason }. Never throws; any doubt is a refusal, and then no file is read at all.
  async intakeFiles(entries) {
    const request = intakeRequest(entries);
    if (request.problem)
      return { block: `BROWSER_FILE_INTAKE_INVALID: ${request.problem}.` };
    let block;
    let answer;
    try {
      answer = await this._send({ action: 'file_intake', files: request.files, token: this._token });
      block = blockOf(answer);
    } catch (error) {
      return { block: `${error.code || 'BROWSER_GUARD_UNAVAILABLE'}: the request guard did not answer the file intake, so no file was given to the page` };
    }
    if (block)
      return { block };
    const retained = retainedCopies(answer, request.files.length);
    if (retained.problem)
      return { block: `BROWSER_GUARD_PROTOCOL: the host's file intake answer is not in the agreed form (${retained.problem}), so no file was given to the page` };
    const files = [];
    for (const [index, file] of retained.files.entries()) {
      const read = await readRetained(file);
      if (read.problem)
        return { block: `BROWSER_FILE_INTAKE_MISMATCH: the host's retained copy of file ${index + 1} ${read.problem}, so no file was given to the page` };
      files.push({ name: file.name, buffer: read.buffer, size: file.size, sha256: file.sha256 });
    }
    return { block: '', files };
  }

  // Body facts for the request message, as agreed with the host:
  //   no body, or an empty one                      -> post_data null,  body_present false
  //   a complete body that is valid UTF-8           -> post_data exact, body_present true
  //   a body that exists but cannot be sent as text -> post_data null,  body_present true
  //     (not valid UTF-8; or the headers announce more bytes than Playwright exposes, as when
  //     Chrome withholds an upload's file parts; or a body announced by headers but not exposed)
  // A line over 32 MiB is reduced to null/true by requestMessage.
  static bodyFacts(buffer, headers = {}) {
    const declaredLength = Number(headers['content-length']);
    const chunked = headers['transfer-encoding'] !== undefined;
    const exposed = buffer ? buffer.length : 0;
    if (exposed === 0) {
      if ((Number.isFinite(declaredLength) && declaredLength > 0) || chunked)
        return { postData: null, bodyPresent: true };
      return { postData: null, bodyPresent: false };
    }
    if (Number.isFinite(declaredLength) && declaredLength > exposed)
      return { postData: null, bodyPresent: true };
    try {
      return { postData: new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(buffer), bodyPresent: true };
    } catch {
      return { postData: null, bodyPresent: true };
    }
  }

  // Playwright route handler: every request Playwright routes for the attached tabs, the owner's included.
  routeHandler() {
    return async (route, request) => {
      let block;
      try {
        block = await this.decideRequest({
          url: request.url(),
          method: request.method(),
          resourceType: request.resourceType(),
          ...BrowserGuard.bodyFacts(request.postDataBuffer(), request.headers()),
        });
      } catch {
        block = 'BROWSER_GUARD_UNAVAILABLE';
      }
      if (block)
        await route.abort('blockedbyclient').catch(() => {});
      else
        await route.fallback().catch(() => {});
    };
  }
}
