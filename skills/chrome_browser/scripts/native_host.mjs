#!/usr/bin/env node
// One native message from the extension popup. This bootstrap is never a browser executor:
// it reads the existing pairing and answers once; it writes nothing and opens no socket.
import os from 'node:os';

import { packageOrigin, readPairing } from '../lib/pairing.mjs';

const MAX_REQUEST = 1024;
const TIMEOUT_MS = 5000;
const [dir, sender] = process.argv.slice(2);
const expectedSender = `${packageOrigin()}/`;
// Chrome frames every message with a 32-bit length in native byte order.
const LITTLE_ENDIAN = os.endianness() === 'LE';
let answered = false;

function reply(message, code = 0) {
  if (answered) return;
  answered = true;
  clearTimeout(timer);
  process.stdin.destroy();
  const body = Buffer.from(JSON.stringify(message));
  const header = Buffer.alloc(4);
  if (LITTLE_ENDIAN) header.writeUInt32LE(body.length); else header.writeUInt32BE(body.length);
  process.stdout.write(Buffer.concat([header, body]), () => process.exit(code));
}

function answer(request) {
  if (sender !== expectedSender) throw new Error('The local helper refused a caller that is not this extension.');
  if (!request || request.type !== 'bootstrap' || request.version !== 1 || Object.keys(request).length !== 2)
    throw new Error('Unsupported bootstrap request.');
  const pairing = readPairing(dir);
  if (!pairing) throw new Error('Ouroboros has not created pairing on this computer yet.');
  if (pairing.pinned_origin && pairing.pinned_origin !== packageOrigin())
    throw new Error('Pairing is pinned to a different extension. Ask Ouroboros to check the installed extension.');
  return { ok: true, credential: pairing.credential, port: pairing.port };
}

const timer = setTimeout(() => reply({ ok: false, error: 'No complete bootstrap request arrived in time.' }, 1), TIMEOUT_MS);
let header = Buffer.alloc(0);
let request = null;  // sized from the header, never larger than MAX_REQUEST
let filled = 0;
process.stdout.on('error', () => process.exit(1));
process.stdin.on('error', () => reply({ ok: false, error: 'The bootstrap request could not be read.' }, 1));
process.stdin.on('end', () => reply({ ok: false, error: 'Incomplete bootstrap request.' }, 1));
process.stdin.on('data', (chunk) => {
  if (answered) return;
  let offset = 0;
  if (request === null) {
    offset = Math.min(4 - header.length, chunk.length);
    header = Buffer.concat([header, chunk.subarray(0, offset)]);
    if (header.length < 4) return;
    const length = LITTLE_ENDIAN ? header.readUInt32LE(0) : header.readUInt32BE(0);
    if (length > MAX_REQUEST) return reply({ ok: false, error: 'Bootstrap request is too large.' }, 1);
    request = Buffer.alloc(length);
  }
  // Only what fits the announced request is kept; anything after it is ignored.
  filled += chunk.copy(request, filled, offset);
  if (filled < request.length) return;
  try { reply(answer(JSON.parse(request.toString('utf8')))); }
  catch (error) { reply({ ok: false, error: String(error.message || error) }); }
});
