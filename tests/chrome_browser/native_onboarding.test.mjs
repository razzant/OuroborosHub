// Native onboarding consumer of skills/chrome_browser against a real Chrome for Testing (or Chromium).
//
//   OUROBOROS_CHROME_TEST_CHROMIUM="/path/to/Google Chrome for Testing" node --test tests/chrome_browser/native_onboarding.test.mjs
//
// One disposable root holds everything: HOME, the browser profile, the skill
// installation and its state, and the native host registration, which setup
// writes into that profile's user data directory. The popup's Connect reaches
// the generated launcher through chrome.runtime.sendNativeMessage; the harness
// never types, reads into the page or injects the credential. The pairing the
// helper returns then carries the ordinary WebSocket/HMAC handshake into a
// trusted input, a sequence, a handoff and a disconnect. No personal Chrome,
// profile or registration is used, and the owner's registration paths are
// only checked, by metadata, for being left as they were.

import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';

import {
  FakeHost, REPO, freePort, installReplica, launchChromium, processesMatching, sleep, startFixtureServer, until,
} from './harness.mjs';

const executable = process.env.OUROBOROS_CHROME_TEST_CHROMIUM;
const evidenceDir = process.env.OUROBOROS_CHROME_TEST_EVIDENCE || path.join(REPO, '.chrome_browser_evidence');
const skip = !executable ? 'OUROBOROS_CHROME_TEST_CHROMIUM is not set'
  : !['darwin', 'linux'].includes(process.platform) ? 'native bootstrap registration exists on macOS and Linux only' : false;
const listening = (port) => new Promise((resolve) => {
  const probe = net.connect(port, '127.0.0.1');
  probe.once('connect', () => { probe.destroy(); resolve(true); });
  probe.once('error', () => resolve(false));
});
const statOf = (file) => {
  try {
    const stat = fs.statSync(file);
    return { size: stat.size, mtime_ms: stat.mtimeMs, ino: stat.ino };
  } catch (error) {
    return error.code === 'ENOENT' ? null : error.code;
  }
};
const filesUnder = (dir) => (fs.existsSync(dir)
  ? fs.readdirSync(dir, { recursive: true }).map(String).filter((name) => fs.statSync(path.join(dir, name)).isFile()) : []);

test('native onboarding consumer', { skip, timeout: 300000 }, async (t) => {
  assert.ok(!/headless[_-]shell/i.test(executable), 'the full browser is required, not the headless shell');
  const root = fs.mkdtempSync('/tmp/ob-cb-native-');
  const home = path.join(root, 'home');
  fs.mkdirSync(home, { mode: 0o700 });
  const redact = (value) => String(value).replaceAll(root, '<root>');
  const evidence = { started_at: new Date().toISOString(), executable, platform: process.platform, commands: [], observations: {}, screenshots: {} };
  const note = (key, value) => { evidence.observations[key] = value; };
  const install = installReplica(root);
  const port = await freePort();
  // Only these names: no XDG_CONFIG_HOME or CHROME_CONFIG_HOME can move a registration out of the root.
  const setupEnv = { PATH: process.env.PATH, HOME: home, OUROBOROS_SKILL_STATE_DIR: install.stateDir };
  const setupScript = path.join(install.skillDir, 'scripts', 'setup.mjs');
  const setup = (...args) => {
    evidence.commands.push(['node', '<skill>/scripts/setup.mjs', ...args.map(redact)].join(' '));
    return JSON.parse(execFileSync(process.execPath, [setupScript, ...args], { env: setupEnv, encoding: 'utf8' }));
  };
  const { BROWSERS, nativeRegistration } = await import(pathToFileURL(path.join(install.skillDir, 'lib', 'native_setup.mjs')).href);
  // The account's real home, from the user database whatever HOME says: its registrations must not change.
  const ownerFiles = Object.keys(BROWSERS).map((browser) => nativeRegistration({ browser, home: os.userInfo().homedir, env: {} }));
  const ownerBefore = ownerFiles.map(statOf);
  const fixture = await startFixtureServer();
  const profile = fs.mkdtempSync(path.join(root, 'profile-'));
  let browser = null;
  const hosts = [];

  try {
    // ---- Ouroboros prepares pairing; no browser is involved yet
    const init = setup('init', '--port', String(port));
    const stored = JSON.parse(fs.readFileSync(init.credential_file, 'utf8'));
    assert.ok(!JSON.stringify(init).includes(stored.credential), 'setup output must not contain the credential');
    const origin = `chrome-extension://${init.extension_id}`;

    const chromeEnv = { ...process.env, HOME: home };
    delete chromeEnv.XDG_CONFIG_HOME;
    delete chromeEnv.CHROME_CONFIG_HOME;
    browser = await launchChromium({ executable, profile, extension: path.join(install.skillDir, 'extension'), WebSocket: install.WebSocket, env: chromeEnv });
    evidence.chromium = { product: browser.version.product, args: browser.args.map(redact), home: '<root>/home' };
    const { cdp } = browser;
    const worker = await until(async () => (await cdp.send('Target.getTargets')).targetInfos
      .find((target) => target.type === 'service_worker' && target.url.endsWith('/service_worker.js')), { what: 'the extension service worker' });
    assert.equal(new URL(worker.url).host, init.extension_id);
    const popupUrl = `${origin}/popup.html`;
    const pageA = await cdp.open(`${fixture.origin}/a.html`);

    /** The owner opens the popup on tab A: popup.html as a background tab of that window. */
    const ownerPopup = async () => {
      await pageA.activate();
      const popup = await cdp.open(popupUrl, { background: true });
      popup.text = (id) => popup.evaluate(`document.getElementById(${JSON.stringify(id)}).textContent`);
      popup.disabled = (id) => popup.evaluate(`document.getElementById(${JSON.stringify(id)}).disabled`);
      popup.status = () => popup.evaluate('chrome.runtime.sendMessage({ type: "status" })');
      await until(async () => (await popup.text('tab')) !== '…', { what: 'popup status' });
      return popup;
    };
    /** The popup at its own size (344 CSS px wide), at device scale 2. */
    const shot = async (popup, name) => {
      const box = await popup.evaluate('(() => { const r = document.body.getBoundingClientRect(); return { width: Math.ceil(r.width), height: Math.ceil(r.height) }; })()');
      const { data } = await cdp.send('Page.captureScreenshot', { format: 'png', clip: { x: 0, y: 0, ...box, scale: 2 } }, popup.sessionId);
      const file = path.join(evidenceDir, name);
      fs.mkdirSync(evidenceDir, { recursive: true });
      fs.writeFileSync(file, Buffer.from(data, 'base64'));
      const shown = { summary: await popup.text('summary'), error: await popup.text('error'), tab: await popup.text('tab'),
        connect_disabled: await popup.disabled('connect'), forget_disabled: await popup.disabled('forget'), share_disabled: await popup.disabled('share') };
      evidence.screenshots[name] = { css_size: box, scale: 2, ...shown };
      return shown;
    };
    const holdsFileCredential = async (popup) => {
      // Harness inspection only: compared here, never written to the evidence.
      const held = await popup.evaluate('chrome.storage.local.get(["credential", "port"])');
      return held.credential === stored.credential && held.port === port;
    };

    // ---- before: not paired; Connect without a registered helper fails and saves nothing
    let popup = await ownerPopup();
    assert.equal(await popup.text('summary'), 'Not paired.');
    assert.equal(await popup.text('error'), '', 'a fresh popup reports nothing as failed');
    assert.equal(await popup.disabled('connect'), false);
    await shot(popup, '01-not-paired.png');
    await popup.click('#connect');
    const missing = await until(async () => (await popup.text('error')) || null, { what: 'the missing-helper error' });
    assert.match(missing, /native messaging host not found/i);
    assert.match(missing, /Ask Ouroboros to set up the local helper/);
    assert.equal((await popup.status()).has_credential, false);
    assert.equal(await popup.disabled('connect'), false, 'Connect can be tried again');
    await shot(popup, '02-helper-missing.png');
    note('helper_missing', { error: missing, has_credential: false });

    // ---- Ouroboros registers the helper for exactly this browser's user data directory, nowhere else
    const expected = nativeRegistration({ browser: 'chrome-for-testing', userDataDir: profile, home, env: {} });
    assert.ok(expected.startsWith(`${profile}${path.sep}`), 'resolution is inside the disposable profile before anything is written');
    assert.equal(fs.existsSync(expected), false);
    // The browser creates this directory itself: where it looks for a user's native hosts with this --user-data-dir.
    const browserMadeDir = fs.existsSync(path.dirname(expected)) ? fs.readdirSync(path.dirname(expected)) : null;
    const native = setup('install-native', '--browser', 'chrome-for-testing', '--user-data-dir', profile);
    assert.equal(native.ok, true);
    assert.equal(native.native_host, expected);
    const manifest = JSON.parse(fs.readFileSync(expected, 'utf8'));
    assert.deepEqual(manifest.allowed_origins, [`${origin}/`]);
    assert.equal(manifest.path, path.join(install.stateDir, 'chrome_browser_native_host.sh'));
    const launcher = fs.readFileSync(manifest.path, 'utf8');
    for (const part of [process.execPath, path.join(install.skillDir, 'scripts', 'native_host.mjs'), install.stateDir]) assert.ok(launcher.includes(part));
    assert.deepEqual(filesUnder(home).filter((name) => name.includes('NativeMessagingHosts')), [], 'nothing registered under the disposable HOME');
    assert.deepEqual(ownerFiles.map(statOf), ownerBefore, 'the owner\'s registrations are as they were');
    note('registration', { browser_created_dir_before_setup: browserMadeDir, file: redact(expected), launcher: redact(manifest.path), allowed_origins: manifest.allowed_origins, mode: (fs.statSync(expected).mode & 0o777).toString(8) });

    // ---- Ouroboros waits in `pair`; the owner chooses Connect once
    const pairing = spawn(process.execPath, [setupScript, 'pair', '--wait', '60'], { env: setupEnv, stdio: ['ignore', 'pipe', 'inherit'] });
    evidence.commands.push('node <skill>/scripts/setup.mjs pair --wait 60');
    let pairOutput = '';
    pairing.stdout.on('data', (chunk) => { pairOutput += chunk; });
    const pairingDone = new Promise((resolve) => pairing.once('exit', resolve));
    await until(() => listening(port), { what: 'the pairing listener' });
    const clicked = Date.now();
    await popup.click('#connect');
    assert.equal(await pairingDone, 0, pairOutput);
    const paired = JSON.parse(pairOutput);
    assert.deepEqual([paired.ok, paired.paired_origin, paired.is_package_extension], [true, origin, true]);
    assert.ok(!pairOutput.includes(stored.credential));
    await until(async () => /Pairing saved/.test(await popup.text('summary')), { what: 'the popup answer to Connect' });
    assert.equal(await popup.evaluate('document.getElementById("credential").value'), '', 'the manual field was never used');
    assert.equal(await popup.evaluate('document.querySelector("details").open'), false);
    assert.equal(await holdsFileCredential(popup), true, 'the extension holds the pairing that the helper read from the file');
    const prepared = await shot(popup, '03-pairing-saved.png');
    assert.equal(prepared.error, '', 'the proof the pair command closed after is not reported as a failure');
    assert.deepEqual([prepared.connect_disabled, prepared.forget_disabled, prepared.share_disabled], [true, false, true]);
    note('connect', { pair_ms: Date.now() - clicked, pinned_origin: paired.paired_origin, popup: prepared });
    await popup.close();

    // ---- a task's executor holds the port; the owner opens the popup and shares tab A
    const host = new FakeHost({ socketDir: root, skillDir: install.skillDir });
    hosts.push(host);
    await host.start();
    assert.equal((await host.call('browser_state', {}, { invocationId: null })).json.pairing.listener, 'listening');
    popup = await ownerPopup();
    await until(async () => /Connected/.test(await popup.text('summary')), { what: 'the extension connection to the executor' });
    const connectedView = await shot(popup, '04-connected.png');
    assert.equal(connectedView.error, '');
    const tabA = /Tab (\d+)/.exec(await popup.text('tab'))[1];
    await popup.click('#share');
    await until(async () => (await host.call('browser_state', {}, { invocationId: null })).json.approved_tabs.length === 1, { what: 'the shared tab' });

    await t.test('connect, then a trusted first input', async () => {
      const connected = await host.call('browser_connect', { tab_id: tabA });
      assert.equal(connected.isError, false, JSON.stringify(connected.json));
      assert.deepEqual([connected.json.connected, connected.json.lease.tab_id, connected.json.lease.guard_armed], [true, tabA, true]);
      await pageA.evaluate('window.events.length = 0');
      const click = await host.call('browser_action', { operation: 'click', args: { selector: '#go' } });
      assert.equal(click.isError, false, JSON.stringify(click.json));
      assert.equal(click.json.steps[0].outcome, 'dispatched');
      assert.equal(await pageA.evaluate('document.getElementById("count").textContent'), '1');
      const pointer = (await pageA.evaluate('window.events')).filter((event) => event.target === 'go' && !/move|over/.test(event.type));
      assert.deepEqual(pointer.map((event) => event.type), ['pointerdown', 'mousedown', 'mouseup', 'click']);
      assert.ok(pointer.every((event) => event.trusted === true));
      const fill = await host.call('browser_action', { operation: 'fill', args: { selector: '#name', text: 'Ada Lovelace' } });
      assert.equal(fill.json.steps[0].outcome, 'confirmed', JSON.stringify(fill.json));
      note('first_input', { lease: connected.json.lease, pointer: pointer.map((event) => [event.type, event.trusted]), guard: host.factsFor(click.invocationId).map((facts) => [facts.kind, facts.step_id, facts.operation]) });
    });

    await t.test('a sequence handles a delayed DOM in one call, one guard question per step', async () => {
      const steps = [
        { operation: 'click', args: { selector: '#start' } },
        { operation: 'wait', args: { selector: '#step2', timeout_ms: 8000 } },
        { operation: 'fill', args: { selector: '#step2', text: 'delayed value' } },
        { operation: 'click', args: { selector: '#next' } },
        { operation: 'wait', args: { selector: '#done', text: 'done: delayed value', timeout_ms: 8000 } },
        { operation: 'get_text', args: { selector: '#done' } },
      ];
      const answer = await host.call('browser_action', { operation: 'sequence', args: { steps } });
      assert.equal(answer.isError, false, JSON.stringify(answer.json));
      assert.equal(answer.json.result[5].value.text, 'done: delayed value');
      assert.deepEqual(host.factsFor(answer.invocationId).map((facts) => [facts.kind, facts.operation]), steps.map((step) => ['primitive', step.operation]));
      note('sequence', { outcomes: answer.json.steps.map((step) => step.outcome), elapsed_ms: answer.elapsed_ms });
    });

    await t.test('handoff keeps the page; resume continues it; disconnect reports the detach', async () => {
      const prior = (await host.call('browser_state', {}, { invocationId: null })).json.lease;
      const handoff = await host.call('browser_handoff', {});
      assert.deepEqual([handoff.json.connected, handoff.json.phase], [false, 'handoff'], JSON.stringify(handoff.json));
      assert.deepEqual(handoff.json.last_closure, { reason: 'handoff', debugger_detach_ack: true });
      await pageA.typeInto('#notes', 'written by the owner');
      const resumed = await host.call('browser_resume', {});
      assert.equal(resumed.isError, false, JSON.stringify(resumed.json));
      assert.equal(resumed.json.lease.document_id, prior.document_id, 'the same document, not reloaded');
      const values = await host.call('browser_action', { operation: 'sequence', args: { steps: [
        { operation: 'get_text', args: { selector: '#name' } }, { operation: 'get_text', args: { selector: '#notes' } }] } });
      assert.deepEqual(values.json.result.map((item) => item.value.text), ['Ada Lovelace', 'written by the owner']);
      const disconnected = await host.call('browser_disconnect', {});
      assert.deepEqual([disconnected.json.connected, disconnected.json.lease], [false, null]);
      assert.deepEqual(disconnected.json.last_closure, { reason: 'disconnect', debugger_detach_ack: true });
      note('handoff_resume_disconnect', { handoff: handoff.json.last_closure, resumed_same_document: true, disconnect: disconnected.json.last_closure });
    });

    await t.test('Forget pairing, then Connect again: the helper restores the same pairing without a copy', async () => {
      // Disconnect gave the port back; the agent's next call binds it again and the popup finds it.
      assert.equal((await host.call('browser_state', {}, { invocationId: null })).json.pairing.listener, 'listening');
      await popup.close();
      popup = await ownerPopup();
      await until(async () => /Connected/.test(await popup.text('summary')), { what: 'the connection to the rebound port' });
      await popup.click('#forget');
      await until(async () => (await popup.text('summary')) === 'Not paired.', { what: 'Forget pairing answered' });
      assert.equal(await popup.text('error'), '');
      assert.equal((await popup.status()).has_credential, false);
      await until(async () => !(await host.call('browser_state', {}, { invocationId: null })).json.pairing.paired, { what: 'the executor losing the forgotten connection' });
      await popup.click('#connect');
      await until(async () => /Connected/.test(await popup.text('summary')), { what: 'the reconnection after Connect' });
      assert.equal(await holdsFileCredential(popup), true);
      assert.equal((await host.call('browser_state', {}, { invocationId: null })).json.pairing.paired, true);
      note('forget_then_connect', { reconnected: true, manual_copy: false });
    });

    // ---- the task ends: the pairing stays prepared; the popup says what may be wrong without inventing it
    evidence.shutdown = await host.stop();
    await popup.close();
    popup = await ownerPopup();
    await until(async () => /another Chrome profile may be connected/.test(await popup.text('error')), { what: 'the generic connection failure' });
    const idle = await shot(popup, '05-saved-no-task.png');
    assert.match(idle.summary, /Pairing saved/);
    note('no_task', idle);

    await t.test('rotate a paired profile: Forget pairing enables Connect and the new credential pairs through the helper', async () => {
      const rotated = setup('rotate');
      const rotatedStored = JSON.parse(fs.readFileSync(rotated.credential_file, 'utf8'));
      assert.notEqual(rotatedStored.credential, stored.credential);
      assert.match(rotated.next.at(-1), /"Forget pairing", then "Connect"/);
      assert.equal(await popup.disabled('connect'), true, 'old pairing disables Connect');
      assert.equal((await popup.status()).has_credential, true);
      await shot(popup, '06-rotated-old-pairing.png');

      const pairing = spawn(process.execPath, [setupScript, 'pair', '--wait', '60'], { env: setupEnv, stdio: ['ignore', 'pipe', 'inherit'] });
      evidence.commands.push('node <skill>/scripts/setup.mjs pair --wait 60');
      let pairOutput = '';
      pairing.stdout.on('data', (chunk) => { pairOutput += chunk; });
      const pairingDone = new Promise((resolve) => pairing.once('exit', resolve));
      await until(() => listening(port), { what: 'the rotated pairing listener' });
      await popup.click('#forget');
      await until(async () => (await popup.text('summary')) === 'Not paired.', { what: 'Forget pairing after rotation' });
      assert.equal(await popup.disabled('connect'), false);
      await popup.click('#connect');
      assert.equal(await pairingDone, 0, pairOutput);
      const paired = JSON.parse(pairOutput);
      assert.deepEqual([paired.ok, paired.paired_origin, paired.is_package_extension], [true, origin, true]);
      assert.ok(!pairOutput.includes(rotatedStored.credential));
      await until(async () => /Pairing saved/.test(await popup.text('summary')), { what: 'new pairing saved' });
      const held = await popup.evaluate('chrome.storage.local.get(["credential", "port"])');
      assert.equal(held.credential, rotatedStored.credential);
      assert.equal(held.port, port);
      assert.equal(await popup.evaluate('document.getElementById("credential").value'), '', 'no manual credential entry');
      const repaired = await shot(popup, '07-rotated-pairing-saved.png');
      assert.equal(repaired.error, '');
      note('rotate_forget_connect', { connect_disabled_before_forget: true, credential_changed: true, new_pairing_saved: true, manual_copy: false });
    });
    await popup.close();
    fs.writeFileSync(path.join(evidenceDir, 'native-onboarding-guard-facts.json'), `${JSON.stringify(host.facts, null, 1)}\n`);
    evidence.finished_at = new Date().toISOString();
  } finally {
    for (const host of hosts) await host.stop().catch(() => null);
    if (browser) {
      const closed = await browser.close();
      evidence.chromium_closed = { graceful: closed.graceful, remaining: closed.remaining, keychain_prompts: closed.keychain_prompts };
      assert.deepEqual(closed.remaining, [], 'no browser process of this run is left');
      if (closed.keychain_prompts !== null) assert.equal(closed.keychain_prompts, 0, 'the browser asked the Keychain');
    }
    await fixture.close();
    await sleep(200);
    assert.deepEqual(processesMatching(root), [], 'no process of this run (browser, native host, executor) is left');
    evidence.owner_registrations = ownerFiles.map((file, index) => ({ file, before: ownerBefore[index], after: statOf(file) }));
    assert.deepEqual(ownerFiles.map(statOf), ownerBefore, 'the owner\'s registrations are as they were');
    fs.mkdirSync(evidenceDir, { recursive: true });
    fs.writeFileSync(path.join(evidenceDir, 'native-onboarding-run.json'), `${JSON.stringify(evidence, null, 2)}\n`);
    fs.rmSync(root, { recursive: true, force: true });
  }
});
