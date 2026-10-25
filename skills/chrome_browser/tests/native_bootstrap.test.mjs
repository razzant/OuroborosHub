import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { packageOrigin } from '../lib/pairing.mjs';
import { installNativeHost, NATIVE_HOST_NAME, nativeRegistration } from '../lib/native_setup.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const setup = path.join(root, 'scripts', 'setup.mjs');
const LE = os.endianness() === 'LE';
const header = (length) => {
  const bytes = Buffer.alloc(4);
  if (LE) bytes.writeUInt32LE(length); else bytes.writeUInt32BE(length);
  return bytes;
};
const frame = (value) => {
  const body = Buffer.from(JSON.stringify(value));
  return Buffer.concat([header(body.length), body]);
};
const unframe = (data) => {
  assert.equal(LE ? data.readUInt32LE(0) : data.readUInt32BE(0), data.length - 4, 'exactly one framed answer');
  return JSON.parse(data.subarray(4).toString('utf8'));
};
const BOOTSTRAP = { type: 'bootstrap', version: 1 };

/** A disposable HOME and state directory; the environment names nothing else (no XDG or Chrome config home). */
function sandbox() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'ocb-native-'));
  const home = path.join(temp, 'home');
  const state = path.join(temp, 'state');
  fs.mkdirSync(home);
  const env = { PATH: process.env.PATH, HOME: home, OUROBOROS_CHROME_STATE_DIR: state };
  const run = (...args) => spawnSync(process.execPath, [setup, ...args], { env, encoding: 'utf8' });
  return { temp, home, state, env, run, file: path.join(state, 'pairing.json') };
}

/** Runs the registered launcher as Chrome would; `writes` are Buffers, or numbers of ms to wait. */
function launch(launcher, origin, writes, { end = true } = {}) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn(launcher, [origin], { stdio: ['pipe', 'pipe', 'pipe'] });
    const out = [];
    child.stdout.on('data', (chunk) => out.push(chunk));
    child.stdin.on('error', () => {});
    child.once('exit', (status) => resolve({ status, stdout: Buffer.concat(out), elapsed: Date.now() - started }));
    (async () => {
      for (const item of writes) {
        if (typeof item === 'number') await new Promise((done) => setTimeout(done, item));
        else if (!child.stdin.destroyed) child.stdin.write(item);
      }
      if (end && !child.stdin.destroyed) child.stdin.end();
    })();
  });
}

test('registration resolves under the chosen browser family or user data directory, and only there', () => {
  const home = '/home/owner';
  const at = (browser, platform, extra = {}) => nativeRegistration({ browser, platform, home, env: {}, ...extra });
  const file = `${NATIVE_HOST_NAME}.json`;
  assert.equal(at('chrome', 'darwin'), `${home}/Library/Application Support/Google/Chrome/NativeMessagingHosts/${file}`);
  assert.equal(at('chrome-for-testing', 'darwin'), `${home}/Library/Application Support/Google/Chrome for Testing/NativeMessagingHosts/${file}`);
  assert.equal(at('chromium', 'darwin'), `${home}/Library/Application Support/Chromium/NativeMessagingHosts/${file}`);
  assert.equal(at('chrome', 'linux'), `${home}/.config/google-chrome/NativeMessagingHosts/${file}`);
  assert.equal(at('chrome-for-testing', 'linux'), `${home}/.config/google-chrome-for-testing/NativeMessagingHosts/${file}`);
  assert.equal(at('chromium', 'linux'), `${home}/.config/chromium/NativeMessagingHosts/${file}`);
  assert.equal(at('chrome', 'linux', { env: { XDG_CONFIG_HOME: '/x' } }), `/x/google-chrome/NativeMessagingHosts/${file}`);
  assert.equal(at('chrome', 'linux', { env: { XDG_CONFIG_HOME: '/x', CHROME_CONFIG_HOME: '/c' } }), `/c/google-chrome/NativeMessagingHosts/${file}`);
  assert.equal(at('chrome', 'linux', { env: { XDG_CONFIG_HOME: 'relative' } }), `${home}/.config/google-chrome/NativeMessagingHosts/${file}`);
  for (const platform of ['darwin', 'linux'])
    assert.equal(at('chromium', platform, { userDataDir: '/tmp/profile' }), `/tmp/profile/NativeMessagingHosts/${file}`);
  assert.throws(() => at(undefined, 'darwin'), /--browser chrome, --browser chrome-for-testing, --browser chromium/);
  assert.throws(() => at('edge', 'darwin'), /Choose the browser/);
  assert.throws(() => at('chrome', 'win32'), /macOS and Linux/);
  assert.throws(() => at('chrome', 'darwin', { userDataDir: 'relative' }), /absolute/);
});

test('setup tells an existing profile to forget its old pairing after a credential or port change', () => {
  const box = sandbox();
  try {
    const next = (...args) => JSON.parse(box.run(...args).stdout).next.at(-1);
    assert.doesNotMatch(next('init'), /Forget pairing/);
    assert.doesNotMatch(next('init'), /Forget pairing/);
    assert.match(next('init', '--port', '47999'), /"Forget pairing", then "Connect"/);
    assert.match(next('rotate'), /"Forget pairing", then "Connect"/);
  } finally { fs.rmSync(box.temp, { recursive: true, force: true }); }
});

test('install-native registers an absolute launcher for the chosen browser only, under the disposable HOME', () => {
  const box = sandbox();
  try {
    assert.equal(JSON.parse(box.run('init').stdout).ok, true);
    const refused = box.run('install-native');
    assert.equal(refused.status, 1);
    assert.match(JSON.parse(refused.stdout).error, /Choose the browser/);
    const expected = nativeRegistration({ browser: 'chrome-for-testing', home: box.home, env: {} });
    assert.ok(expected.startsWith(`${box.home}${path.sep}`), 'resolution stays in the disposable HOME before anything is written');
    assert.equal(fs.existsSync(path.join(box.home, 'Library')) || fs.existsSync(path.join(box.home, '.config')), false,
      'a refused registration wrote nothing');
    const installed = JSON.parse(box.run('install-native', '--browser', 'chrome-for-testing').stdout);
    assert.equal(installed.ok, true);
    assert.equal(installed.browser, 'chrome-for-testing');
    assert.equal(installed.native_host, expected);
    const manifest = JSON.parse(fs.readFileSync(installed.native_host, 'utf8'));
    assert.deepEqual(manifest, {
      name: NATIVE_HOST_NAME, description: 'Ouroboros Chrome Browser pairing bootstrap',
      path: path.join(box.state, 'chrome_browser_native_host.sh'), type: 'stdio', allowed_origins: [`${packageOrigin()}/`],
    });
    assert.equal(fs.statSync(manifest.path).mode & 0o777, 0o700);
    assert.equal(fs.statSync(installed.native_host).mode & 0o777, 0o600);
    assert.ok(fs.readFileSync(manifest.path, 'utf8').includes(process.execPath));
    assert.equal(box.run('install-native', '--browser', 'chrome-for-testing').status, 0, 'registration is idempotent');
    const profile = path.join(box.temp, 'profile');
    const custom = JSON.parse(box.run('install-native', '--browser', 'chromium', '--user-data-dir', profile).stdout);
    assert.equal(custom.native_host, path.join(profile, 'NativeMessagingHosts', `${NATIVE_HOST_NAME}.json`));
    for (const platform of ['darwin', 'linux']) {
      const registration = installNativeHost(box.state, { browser: 'chrome', platform, home: box.home, env: {} }).registration;
      assert.ok(registration.startsWith(`${box.home}${path.sep}`) && fs.existsSync(registration));
    }
    assert.throws(() => installNativeHost(box.state, { browser: 'chrome', platform: 'win32', home: box.home }), /macOS and Linux/);
  } finally { fs.rmSync(box.temp, { recursive: true, force: true }); }
});

test('the native host answers one bounded request, reads only valid existing pairing, and never waits forever', async () => {
  const box = sandbox();
  try {
    box.run('init');
    const credential = JSON.parse(fs.readFileSync(box.file, 'utf8')).credential;
    assert.equal(box.run('install-native', '--browser', 'chrome').status, 0);
    const launcher = path.join(box.state, 'chrome_browser_native_host.sh');
    const origin = `${packageOrigin()}/`;
    const ask = async (writes, sender = origin, options) => {
      const answer = await launch(launcher, sender, writes, options);
      return { ...answer, reply: answer.stdout.length ? unframe(answer.stdout) : null };
    };
    const before = fs.readFileSync(box.file);
    const statBefore = fs.statSync(box.file);

    const good = await ask([frame(BOOTSTRAP)]);
    assert.deepEqual([good.status, good.reply], [0, { ok: true, credential, port: 47831 }]);
    // Split anywhere, also inside the header: still one request and one answer.
    const bytes = frame(BOOTSTRAP);
    const split = await ask([bytes.subarray(0, 2), 30, bytes.subarray(2, 5), 30, bytes.subarray(5)]);
    assert.deepEqual(split.reply, { ok: true, credential, port: 47831 });
    // Two requests in one write: one answer, and nothing after it.
    const twice = await ask([Buffer.concat([frame(BOOTSTRAP), frame(BOOTSTRAP)])]);
    assert.equal(twice.reply.ok, true);

    assert.equal((await ask([frame(BOOTSTRAP)], 'chrome-extension://abcdefghijklmnopabcdefghijklmnop/')).reply.ok, false);
    assert.equal((await ask([frame({ type: 'bootstrap', version: 2 })])).reply.ok, false);
    assert.equal((await ask([frame({ ...BOOTSTRAP, extra: 1 })])).reply.ok, false);
    assert.equal((await ask([header(5), Buffer.from('{oops')])).reply.ok, false);

    // A header announcing more than the bound is answered at once, before any body is read or kept.
    const huge = await ask([header(64 * 1024 * 1024), Buffer.alloc(4096, 0x20)], origin, { end: false });
    assert.deepEqual([huge.status, huge.reply], [1, { ok: false, error: 'Bootstrap request is too large.' }]);
    assert.ok(huge.elapsed < 3000, `answered without waiting for the announced body (${huge.elapsed} ms)`);
    const short = await ask([header(40), Buffer.from('{"type"')]);
    assert.deepEqual([short.status, short.reply], [1, { ok: false, error: 'Incomplete bootstrap request.' }]);
    // Chrome keeps the pipe open: a request that never completes is answered when the time is up.
    const stalled = await ask([header(40), Buffer.from('{"type"')], origin, { end: false });
    assert.deepEqual([stalled.status, stalled.reply], [1, { ok: false, error: 'No complete bootstrap request arrived in time.' }]);
    assert.ok(stalled.elapsed >= 4500 && stalled.elapsed < 9000, `${stalled.elapsed} ms`);

    assert.deepEqual(fs.readFileSync(box.file), before, 'the host never writes pairing');
    assert.equal(fs.statSync(box.file).mtimeMs, statBefore.mtimeMs);
    assert.deepEqual(fs.readdirSync(box.state).sort(), ['chrome_browser_native_host.sh', 'pairing.json']);

    fs.rmSync(box.file);
    assert.match((await ask([frame(BOOTSTRAP)])).reply.error, /not created pairing/);
    assert.equal(fs.existsSync(box.file), false, 'a missing pairing is not created by the host');
  } finally { fs.rmSync(box.temp, { recursive: true, force: true }); }
});

test('malformed pairing is never replaced silently; recover --confirm moves its bytes and mode aside first', async () => {
  const box = sandbox();
  try {
    box.run('init');
    const credential = JSON.parse(fs.readFileSync(box.file, 'utf8')).credential;
    box.run('install-native', '--browser', 'chrome');
    const launcher = path.join(box.state, 'chrome_browser_native_host.sh');
    const ask = async () => unframe((await launch(launcher, `${packageOrigin()}/`, [frame(BOOTSTRAP)])).stdout);
    for (const bad of [`{"credential":"${credential}","port":null,"pinned_origin":null}`,
      `{"credential":"${credential}","port":47831,"pinned_origin":"chrome-extension://wrong"}`, '{not json']) {
      fs.rmSync(box.file, { force: true });
      fs.writeFileSync(box.file, bad, { mode: 0o400 });
      assert.equal((await ask()).ok, false);
      for (const command of [['init'], ['status'], ['install-native', '--browser', 'chrome'], ['rotate'], ['recover']])
        assert.equal(box.run(...command).status, 1, `${command.join(' ')} leaves malformed state alone`);
      assert.match(JSON.parse(box.run('recover').stdout).error, /Nothing was changed.*recover --confirm/);
      assert.equal(fs.readFileSync(box.file, 'utf8'), bad);
      assert.equal(fs.statSync(box.file).mode & 0o777, 0o400);
    }
    const recovered = JSON.parse(box.run('recover', '--confirm').stdout);
    assert.equal(recovered.ok, true);
    assert.equal(fs.readFileSync(recovered.quarantined_file, 'utf8'), '{not json', 'the malformed bytes are kept');
    assert.equal(fs.statSync(recovered.quarantined_file).mode & 0o777, 0o400, 'and their mode');
    const fresh = JSON.parse(fs.readFileSync(box.file, 'utf8'));
    assert.notEqual(fresh.credential, credential);
    assert.equal(fresh.pinned_origin, null);
    assert.equal(fs.statSync(box.file).mode & 0o777, 0o600);
    assert.deepEqual(await ask(), { ok: true, credential: fresh.credential, port: 47831 });
    assert.equal(box.run('recover', '--confirm').status, 1, 'valid pairing is not recovered');
  } finally { fs.rmSync(box.temp, { recursive: true, force: true }); }
});
