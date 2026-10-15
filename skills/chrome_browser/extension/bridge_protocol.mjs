// Shared by the Node executor and the extension service worker: the bridge
// constants and pairing proofs. Both sides hash the package files on disk;
// this is not an attestation of a still-loaded worker after an in-place update.

export const BRIDGE_VERSION = 1;
export const DEFAULT_PORT = 47831;
export const BUILD_FILES = ['manifest.json', 'bridge_protocol.mjs', 'service_worker.js', 'page_agent.js',
  'popup.html', 'popup.js'];

export const READ_OPERATIONS = ['observe', 'read', 'get_text', 'get_attribute', 'snapshot', 'dom_snapshot',
  'ax_snapshot', 'screenshot', 'wait'];
export const NAVIGATION_OPERATIONS = ['navigate', 'goto', 'open_url'];
export const INPUT_OPERATIONS = ['click', 'double_click', 'fill', 'type', 'press', 'check', 'uncheck', 'scroll'];
export const UNSUPPORTED_OPERATIONS = ['select', 'drag', 'upload', 'submit'];
export const OPERATIONS = [...READ_OPERATIONS, ...NAVIGATION_OPERATIONS, ...INPUT_OPERATIONS];

export const CLOSE_MALFORMED = 4400;
export const CLOSE_BAD_PROOF = 4401;
export const CLOSE_BUILD_MISMATCH = 4409;

const encoder = new TextEncoder();
const hex = (buffer) => Array.from(new Uint8Array(buffer), (byte) => byte.toString(16).padStart(2, '0')).join('');

export function randomHex(bytes) {
  return hex(globalThis.crypto.getRandomValues(new Uint8Array(bytes)).buffer);
}

/** What the owner types is compared without case, spaces or dashes. */
export function normalizeCode(code) {
  return String(code || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
}

export async function sha256Hex(bytes) {
  return hex(await globalThis.crypto.subtle.digest('SHA-256', bytes));
}

export async function proof(code, role, nonceE, nonceS) {
  const key = await globalThis.crypto.subtle.importKey(
    'raw', encoder.encode(normalizeCode(code)), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return hex(await globalThis.crypto.subtle.sign('HMAC', key, encoder.encode(`${role}|${nonceE}|${nonceS}`)));
}

/** `readBytes(name)` returns the bytes of one extension file. */
export async function buildDigest(readBytes) {
  const lines = [];
  for (const name of BUILD_FILES)
    lines.push(`${name}:${await sha256Hex(await readBytes(name))}`);
  return sha256Hex(encoder.encode(`bridge/${BRIDGE_VERSION}\n${lines.join('\n')}`));
}
