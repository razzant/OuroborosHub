// The host's verdict over its private Unix socket: one JSON line out, one JSON
// line back, one connection per question. Only {"block": ""} grants.

import net from 'node:net';

const ANSWER_TIMEOUT_MS = 30000;

/** Resolves '' for a grant, otherwise the refusal text. Never rejects. */
export function askGuard(socketPath, facts, timeoutMs = ANSWER_TIMEOUT_MS) {
  if (!socketPath) return Promise.resolve('BROWSER_REQUEST_GUARD_UNAVAILABLE: the host gave no guard socket');
  return new Promise((resolve) => {
    const chunks = [];
    let settled = false;
    const finish = (reason) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(reason);
    };
    const socket = net.createConnection(socketPath);
    socket.setTimeout(timeoutMs, () => finish('BROWSER_REQUEST_GUARD_UNAVAILABLE: the host did not answer'));
    socket.on('connect', () => socket.end(`${JSON.stringify(facts)}\n`));
    socket.on('data', (chunk) => chunks.push(chunk));
    socket.on('error', (error) => finish(`BROWSER_REQUEST_GUARD_UNAVAILABLE: guard socket ${error.code || 'error'}`));
    socket.on('end', () => {
      let answer = null;
      try {
        answer = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      } catch (error) {
        answer = null;
      }
      if (answer && answer.block === '') finish('');
      else if (answer && typeof answer.block === 'string') finish(answer.block);
      else finish('BROWSER_POLICY_UNAVAILABLE: the host gave no allowance');
    });
  });
}
