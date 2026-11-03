// A stand-in for ouroboros/mcp_browser_guard.py's private request callback:
// one JSON line in, one JSON line out, then close. It records the raw lines it received.
// The decision function is the test's; the default mirrors the core semantics for a fake
// "owner service" origin so request shapes can be checked end to end.
import net from 'node:net';
import fs from 'node:fs';

export const OWNER_READONLY_SENTENCE = 'BROWSER_LOCAL_READONLY_BLOCKED: local-readonly subagents cannot run arbitrary browser JavaScript.';

export function coreLikePolicy({ ownerOrigins = [], evaluateBlock = '' } = {}) {
  return (message) => {
    if ('armed' in message)
      return { armed: true };
    if (message.action === 'evaluate')
      return { block: evaluateBlock };
    let parsed;
    try {
      parsed = new URL(message.url);
    } catch {
      return { block: 'BROWSER_POLICY_UNAVAILABLE: bad url' };
    }
    const owner = ownerOrigins.includes(parsed.origin);
    const ownerShaped = message.method === 'POST' && ['/api/settings', '/api/decisions'].includes(parsed.pathname.replace(/\/$/, ''));
    if (owner && ownerShaped) {
      // An unreadable body at an owner endpoint is refused; a genuinely empty one is an empty body.
      if (message.post_data === null && message.body_present === true)
        return { block: 'BROWSER_OWNER_CONTROL_BLOCKED: this operation belongs to the owner' };
      if (typeof message.post_data === 'string' && message.post_data.includes('OUROBOROS_REVIEW_ENFORCEMENT'))
        return { block: 'BROWSER_OWNER_CONTROL_BLOCKED: this operation belongs to the owner' };
    }
    return { block: '' };
  };
}

export async function startMockGuard({ token, decide = coreLikePolicy(), socketPath, respond = true, delayMs = 0 } = {}) {
  const received = [];
  const server = net.createServer((socket) => {
    let buffer = Buffer.alloc(0);
    socket.on('data', (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      const newline = buffer.indexOf(0x0a);
      if (newline === -1)
        return;
      const line = buffer.subarray(0, newline).toString('utf8');
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        message = null;
      }
      received.push({ line, message });
      if (!respond)
        return;
      let answer;
      if (!message || message.token !== token)
        answer = 'armed' in (message || {}) ? { armed: false, block: 'BROWSER_GUARD_TOKEN_INVALID: wrong token' } : { block: 'BROWSER_GUARD_TOKEN_INVALID: wrong token' };
      else
        answer = decide(message);
      setTimeout(() => {
        socket.end(typeof answer === 'string' ? answer : JSON.stringify(answer) + '\n');
      }, delayMs);
    });
    socket.on('error', () => {});
  });
  if (socketPath) {
    try {
      fs.unlinkSync(socketPath);
    } catch {}
    await new Promise((resolve) => server.listen(socketPath, resolve));
  } else {
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  }
  const address = server.address();
  const endpoint = socketPath ? socketPath : `127.0.0.1:${address.port}`;
  return {
    endpoint,
    received,
    messages: () => received.map((entry) => entry.message),
    close: () => new Promise((resolve) => server.close(() => resolve())),
  };
}
