#!/usr/bin/env node
// Owner setup for the chrome_browser skill.
//
//   setup.mjs status            what is configured (no secret)
//   setup.mjs init [--port N]   create the pairing credential file if it is missing
//   setup.mjs pair [--wait S]   wait for the extension to prove the credential; pin its origin
//   setup.mjs rotate            replace the credential and forget the pinned extension
//
// The credential is written to a private file and never printed: this
// command's output is read by the model.

import fs from 'node:fs';
import path from 'node:path';

import { DEFAULT_PORT } from '../extension/bridge_protocol.mjs';
import {
  EXTENSION_DIR, Listener, SKILL_DIR, newCredential, packageBuild, packageOrigin, pairingPath, readPairing, stateDir,
  writePairing,
} from '../lib/pairing.mjs';

const SKILL_NAME = 'chrome_browser';
const [command = 'status', ...rest] = process.argv.slice(2);
const option = (name, fallback) => {
  const index = rest.indexOf(`--${name}`);
  return index >= 0 && rest[index + 1] !== undefined ? rest[index + 1] : fallback;
};
const print = (value) => process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
const fail = (message) => { print({ ok: false, error: message }); process.exit(1); };

const dir = stateDir();
if (!dir) fail('No state directory: set OUROBOROS_SKILL_STATE_DIR (skill_exec does) or OUROBOROS_CHROME_STATE_DIR.');

function facts(pairing) {
  return {
    ok: true,
    credential_file: pairingPath(dir),
    credential_present: Boolean(pairing),
    extension_paired: Boolean(pairing && pairing.pinned_origin),
    port: pairing ? pairing.port : DEFAULT_PORT,
    extension_folder: EXTENSION_DIR,
    extension_id: packageOrigin().slice('chrome-extension://'.length),
    mcp_server: {
      id: SKILL_NAME, name: SKILL_NAME, enabled: true,
      // The Node that ran this script (20 or newer), by path: a `node` found on PATH may be another one, or none.
      transport: 'stdio', command: process.execPath, args: [path.join(SKILL_DIR, 'scripts', 'mcp_server.mjs')],
      browser_bridge: true, browser_protocol: 'ouroboros_chrome',
      browser_skill_binding: { skill_name: path.basename(SKILL_DIR), script_path: 'scripts/mcp_server.mjs' },
      env: { OUROBOROS_CHROME_STATE_DIR: dir },
    },
  };
}

function parsePort(text) {
  const port = Number(text);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) fail('--port must be an integer between 1024 and 65535.');
  return port;
}

let pairing;
try {
  pairing = readPairing(dir);
} catch (error) {
  fail(String(error.message || error));
}

if (command === 'status') {
  print(facts(pairing));
} else if (command === 'init' || command === 'rotate') {
  const port = parsePort(option('port', pairing ? pairing.port : DEFAULT_PORT));
  const created = command === 'rotate' || !pairing;
  if (created) pairing = { credential: newCredential(), port, pinned_origin: null };
  else pairing = { ...pairing, port };
  writePairing(dir, pairing);
  print({
    ...facts(pairing), credential_created: created,
    next: [
      `In chrome://extensions enable Developer mode and "Load unpacked": ${EXTENSION_DIR}`,
      `Open ${pairingPath(dir)} yourself and copy the value of "credential". Do not paste it into a chat.`,
      'Click the extension icon, paste it under Pairing, check the port, and choose "Save and connect".',
      'Then run this script with "pair" to confirm that this extension is the paired one.',
    ],
  });
} else if (command === 'pair') {
  if (!pairing) fail('No credential yet: run "init" first.');
  const wait = Math.min(Math.max(Number(option('wait', 120)) || 120, 5), 280);
  let origin = null;
  let listener;
  let status;
  try {
    listener = new Listener({
      port: pairing.port, credential: pairing.credential, origin: null, build: await packageBuild(), executorGeneration: 0,
      onPaired: (socket, from) => { origin = from; socket.close(); },
    });
    status = await listener.start();
  } catch (error) {
    // For example the ws dependency is not installed: one JSON object, like every other answer of this script.
    fail(String((error && error.message) || error));
  }
  if (status === 'busy') fail(`Port ${pairing.port} is in use, probably by a running task's browser. Try again when it has finished.`);
  const deadline = Date.now() + wait * 1000;
  while (!origin && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 200));
  const refusal = listener.lastRefusal;
  await listener.close();
  if (!origin) {
    fail(refusal === 'build_mismatch'
      ? `The loaded extension is not this skill's build. Reload it from ${EXTENSION_DIR} in chrome://extensions.`
      : refusal === 'bad_proof' ? 'An extension answered with another credential. Paste the current one again.'
        : `No extension proved the credential within ${wait} s. Open its popup and choose "Save and connect".`);
  }
  writePairing(dir, { ...pairing, pinned_origin: origin });
  print({ ...facts(readPairing(dir)), paired_origin: origin, is_package_extension: origin === packageOrigin() });
  process.exit(0);
} else {
  fail(`Unknown command ${command}. Use status, init, pair or rotate.`);
}
