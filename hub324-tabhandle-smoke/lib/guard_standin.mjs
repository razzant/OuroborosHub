// A stand-in for the host's browser-request guard (Ouroboros browser_policy), on the wire contract the skill's own
// lib/guard.mjs speaks: one connection per message, one JSON line out, one JSON line back.
//
// It is stricter than a host would be, not looser: with the right token it lets the browser read only this run's
// loopback fixture (and about:blank, which sends nothing), and refuses every other address, every file intake and
// every unknown action. Arming answers like a current host that owns the session (capabilities.embedded_resources).
// The token is checked on every message and never written anywhere; request bodies are logged by size only.
import fs from 'node:fs';
import net from 'node:net';

const MAX_LINE_BYTES = 40 * 1024 * 1024;

export async function startGuard({ token, allowOrigin, logFile }) {
  const log = fs.createWriteStream(logFile, { flags: 'a' });
  log.on('error', () => {});
  let logOpen = true;
  const counts = { arm: 0, allowed: 0, refused: 0, bad_token: 0, malformed: 0 };
  const refusedUrls = [];
  const record = (entry, answer) => {
    if (logOpen)
      log.write(`${JSON.stringify({ at: Date.now(), ...entry, answer: answer.block === undefined ? answer : { block: answer.block } })}\n`);
    return answer;
  };
  const allowed = (url) => {
    if (url === 'about:blank')
      return true;
    try {
      return new URL(url).origin === allowOrigin;
    } catch {
      return false;
    }
  };
  const REFUSED = 'SMOKE_GUARD_REFUSED: this smoke test lets the browser read only its own loopback fixture';

  function decide(message) {
    if (!message || typeof message !== 'object' || Array.isArray(message)) {
      counts.malformed += 1;
      return record({ kind: 'malformed' }, { block: 'SMOKE_GUARD_PROTOCOL: not one JSON object' });
    }
    const kind = message.armed === true ? 'arm' : message.action ?? 'request';
    if (message.token !== token) {
      counts.bad_token += 1;
      return record({ kind, token: 'wrong' }, message.armed === true ? { armed: false, block: 'SMOKE_GUARD_TOKEN: wrong token' } : { block: 'SMOKE_GUARD_TOKEN: wrong token' });
    }
    if (kind === 'arm') {
      counts.arm += 1;
      return record({ kind }, { armed: true, capabilities: { embedded_resources: true } });
    }
    if (kind === 'evaluate') {
      const ok = allowed(String(message.url ?? ''));
      counts[ok ? 'allowed' : 'refused'] += 1;
      if (!ok)
        refusedUrls.push(String(message.url ?? ''));
      return record({ kind, url: message.url }, { block: ok ? '' : REFUSED });
    }
    if (kind === 'file_intake') {
      counts.refused += 1;
      return record({ kind, files: Array.isArray(message.files) ? message.files.length : null }, { block: 'SMOKE_GUARD_NO_FILE_INTAKE: this smoke test gives pages no files' });
    }
    if (kind !== 'request') {
      counts.refused += 1;
      return record({ kind }, { block: `SMOKE_GUARD_UNKNOWN_ACTION: ${String(kind).slice(0, 40)}` });
    }
    const url = String(message.url ?? '');
    const ok = allowed(url);
    counts[ok ? 'allowed' : 'refused'] += 1;
    if (!ok)
      refusedUrls.push(url);
    return record({
      kind, url, method: message.method, resource_type: message.resource_type, body_present: !!message.body_present,
      post_data_bytes: typeof message.post_data === 'string' ? Buffer.byteLength(message.post_data) : 0,
    }, { block: ok ? '' : REFUSED });
  }

  const server = net.createServer((socket) => {
    let buffer = Buffer.alloc(0);
    let answered = false;
    socket.on('data', (part) => {
      if (answered)
        return;
      buffer = Buffer.concat([buffer, part]);
      const newline = buffer.indexOf(0x0a);
      if (newline < 0) {
        if (buffer.length > MAX_LINE_BYTES) {
          answered = true;
          socket.destroy();
        }
        return;
      }
      answered = true;
      let message = null;
      try {
        message = JSON.parse(buffer.subarray(0, newline).toString('utf8'));
      } catch {
        message = null;
      }
      socket.end(`${JSON.stringify(decide(message))}\n`);
    });
    socket.on('error', () => {});
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });

  return {
    endpoint: `127.0.0.1:${server.address().port}`,
    counts,
    refusedUrls,
    async close() {
      await new Promise((resolve) => server.close(() => resolve()));
      logOpen = false;
      await new Promise((resolve) => log.end(resolve));
    },
  };
}
