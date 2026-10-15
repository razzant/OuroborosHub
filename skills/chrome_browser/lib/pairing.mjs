// The standing pairing: one credential per installation in a private state
// file, the extension origin the owner paired, and the loopback listener that
// admits exactly that extension. The credential is never printed or logged.

import crypto from 'node:crypto';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  BRIDGE_VERSION, CLOSE_BAD_PROOF, CLOSE_BUILD_MISMATCH, CLOSE_MALFORMED, DEFAULT_PORT, buildDigest, proof, randomHex,
} from '../extension/bridge_protocol.mjs';

export const SKILL_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const EXTENSION_DIR = path.join(SKILL_DIR, 'extension');
const PAIRING_FILE = 'pairing.json';
const ORIGIN = /^chrome-extension:\/\/[a-p]{32}$/;

export function stateDir(env = process.env) {
  for (const key of ['OUROBOROS_CHROME_STATE_DIR', 'OUROBOROS_SKILL_STATE_DIR'])
    if (env[key]) return path.resolve(env[key]);
  // data/skills/<bucket>/<name>  ->  data/state/skills/<name>
  const state = path.resolve(SKILL_DIR, '..', '..', '..', 'state');
  return fs.existsSync(state) ? path.join(state, 'skills', path.basename(SKILL_DIR)) : null;
}

export const pairingPath = (dir) => path.join(dir, PAIRING_FILE);

/** The stored pairing, or null. A file other users could read is refused. */
export function readPairing(dir) {
  if (!dir) return null;
  let text;
  try {
    const file = pairingPath(dir);
    if ((fs.statSync(file).mode & 0o077) !== 0) throw new Error(`${file} must not be readable by other users (chmod 600)`);
    text = fs.readFileSync(file, 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
  const data = JSON.parse(text);
  if (typeof data.credential !== 'string' || data.credential.length < 32) throw new Error('pairing file has no usable credential');
  return {
    credential: data.credential,
    port: Number.isInteger(data.port) ? data.port : DEFAULT_PORT,
    pinned_origin: ORIGIN.test(data.pinned_origin || '') ? data.pinned_origin : null,
  };
}

export function writePairing(dir, pairing) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const file = pairingPath(dir);
  const temporary = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify({ version: 1, ...pairing }, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temporary, file);
  fs.chmodSync(file, 0o600);
}

export const newCredential = () => crypto.randomBytes(32).toString('hex').toUpperCase();

/** The origin Chrome gives this package's extension: derived from the manifest key. */
export function packageOrigin() {
  const manifest = JSON.parse(fs.readFileSync(path.join(EXTENSION_DIR, 'manifest.json'), 'utf8'));
  const digest = crypto.createHash('sha256').update(Buffer.from(manifest.key, 'base64')).digest().subarray(0, 16);
  return `chrome-extension://${Array.from(digest, (byte) => String.fromCharCode(97 + (byte >> 4), 97 + (byte & 15))).join('')}`;
}

export const packageBuild = () => buildDigest(async (name) => fs.promises.readFile(path.join(EXTENSION_DIR, name)));

/** `ws`, from the skill's isolated dependency directory. */
export function loadWs() {
  const roots = [path.join(SKILL_DIR, '.ouroboros_env', 'node', 'package.json'), path.join(SKILL_DIR, 'package.json')];
  for (const root of roots) {
    try {
      return createRequire(root)('ws');
    } catch (error) {
      if (error.code !== 'MODULE_NOT_FOUND') throw error;
    }
  }
  throw new Error('the ws dependency is not installed in this skill\'s .ouroboros_env');
}

const safeEqual = (a, b) => {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const left = Buffer.from(a, 'utf8');
  const right = Buffer.from(b, 'utf8');
  return left.length === right.length && crypto.timingSafeEqual(left, right);
};

/**
 * One loopback listener. `origin` is the pinned origin, or null in the owner's
 * pairing window, where the first extension that proves the credential is the
 * one that gets pinned. Before a valid proof the peer learns only a nonce.
 */
export class Listener {
  constructor({ port, credential, origin, build, executorGeneration, onPaired }) {
    Object.assign(this, { port, credential, origin, build, executorGeneration, onPaired });
    this.failed = 0;
    this.lastRefusal = '';
    this.active = null;
    this.server = null;
  }

  /** Resolves 'listening' or 'busy'. A busy port is never replaced by another one. */
  start() {
    const { WebSocketServer } = loadWs();
    return new Promise((resolve, reject) => {
      const server = new WebSocketServer({
        host: '127.0.0.1', port: this.port, maxPayload: 96 * 1024 * 1024,
        verifyClient: ({ origin }, answer) => answer(
          !this.active && ORIGIN.test(origin || '') && (this.origin === null || origin === this.origin), 403, 'Forbidden'),
      });
      server.once('listening', () => { this.server = server; resolve('listening'); });
      server.once('error', (error) => (error.code === 'EADDRINUSE' ? resolve('busy') : reject(error)));
      server.on('connection', (socket, request) => this.admit(socket, request.headers.origin).catch(() => socket.terminate()));
    });
  }

  async admit(socket, origin) {
    const inbox = [];
    let wake = null;
    const onMessage = (data) => { inbox.push(data); if (wake) wake(); };
    socket.on('message', onMessage);
    const next = async () => {
      const deadline = Date.now() + 10000;
      while (!inbox.length) {
        if (Date.now() > deadline || socket.readyState !== 1) throw new Error('handshake abandoned');
        await new Promise((resolve) => { wake = resolve; setTimeout(resolve, 250); });
      }
      const data = inbox.shift();
      if (data.length > 2048) throw new Error('oversized handshake message');
      return JSON.parse(data.toString('utf8'));
    };
    const reject = (code, kind) => { this.failed += 1; this.lastRefusal = kind; socket.close(code); };
    let hello;
    try {
      hello = await next();
    } catch (error) {
      return reject(CLOSE_MALFORMED, 'malformed');
    }
    if (hello.t !== 'hello' || hello.v !== BRIDGE_VERSION || !/^[0-9a-f]{32}$/.test(hello.nonce_e || ''))
      return reject(CLOSE_MALFORMED, 'malformed');
    const nonceS = randomHex(16);
    socket.send(JSON.stringify({ t: 'challenge', nonce_s: nonceS }));
    let auth;
    try {
      auth = await next();
    } catch (error) {
      return reject(CLOSE_MALFORMED, 'malformed');
    }
    if (auth.t !== 'auth' || !safeEqual(auth.proof, await proof(this.credential, 'ext', hello.nonce_e, nonceS)))
      return reject(CLOSE_BAD_PROOF, 'bad_proof');
    if (auth.build !== this.build) {
      this.lastRefusal = 'build_mismatch';
      socket.send(JSON.stringify({ t: 'refused', code: 'BUILD_MISMATCH' }));
      return socket.close(CLOSE_BUILD_MISMATCH);
    }
    if (this.active) return socket.close(CLOSE_MALFORMED);
    socket.off('message', onMessage);
    this.active = socket;
    this.lastRefusal = '';
    socket.once('close', () => { if (this.active === socket) this.active = null; });
    socket.send(JSON.stringify({
      t: 'paired', proof: await proof(this.credential, 'srv', hello.nonce_e, nonceS), executor_generation: this.executorGeneration,
    }));
    this.onPaired(socket, origin);
    return undefined;
  }

  close() {
    if (this.active) this.active.terminate();
    return new Promise((resolve) => (this.server ? this.server.close(() => resolve()) : resolve()));
  }
}
