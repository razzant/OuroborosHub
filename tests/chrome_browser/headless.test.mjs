// Consumer test of skills/chrome_browser against a real Chromium.
//
//   OUROBOROS_CHROME_TEST_CHROMIUM=/path/to/full/chromium node --test tests/chrome_browser/executor.test.mjs tests/chrome_browser/headless.test.mjs
//
// It loads the real extension into a disposable --headless=new profile and
// drives the real MCP server the way the host does. The pairing credential is
// injected through the extension's own popup by the harness: that stands in
// for the owner's manual paste and is not evidence about a personal Chrome.

import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import test from 'node:test';

import {
  FakeHost, Page, REPO, assertSafeRecipe, chromiumArgs, freePort, installReplica, launchChromium, processesMatching, sleep,
  startFixtureServer, until,
} from './harness.mjs';

const executable = process.env.OUROBOROS_CHROME_TEST_CHROMIUM;
const evidenceDir = process.env.OUROBOROS_CHROME_TEST_EVIDENCE || path.join(REPO, '.chrome_browser_evidence');
const CONTRACT_KEYS = ['protocol', 'version', 'owner', 'executor_generation', 'connected', 'lease', 'closure'];

test('launch recipe: headless=new, mock keychain, basic password store, fresh profile', () => {
  const profile = fs.mkdtempSync('/tmp/ob-cb-recipe-');
  try {
    const args = chromiumArgs({ profile, extension: '/x' });
    assertSafeRecipe(args, profile);
    for (const flag of ['--headless=new', '--use-mock-keychain', '--password-store=basic']) {
      assert.ok(args.includes(flag));
      assert.throws(() => assertSafeRecipe(args.filter((arg) => arg !== flag), profile), /refusing to launch/);
    }
    fs.writeFileSync(path.join(profile, 'Cookies'), 'x');
    assert.throws(() => assertSafeRecipe(args, profile), /fresh, empty profile/);
  } finally {
    fs.rmSync(profile, { recursive: true, force: true });
  }
});

test('headless consumer', { skip: executable ? false : 'OUROBOROS_CHROME_TEST_CHROMIUM is not set', timeout: 240000 }, async (t) => {
  assert.ok(!/headless[_-]shell/i.test(executable), 'the full Chromium is required, not the headless shell');
  const root = fs.mkdtempSync('/tmp/ob-cb-');
  const evidence = { started_at: new Date().toISOString(), executable, observations: {} };
  const note = (key, value) => { evidence.observations[key] = value; };
  const install = installReplica(root);
  const port = await freePort();
  const setupEnv = { PATH: process.env.PATH, OUROBOROS_SKILL_STATE_DIR: install.stateDir };
  const setupScript = path.join(install.skillDir, 'scripts', 'setup.mjs');
  const setup = (...args) => JSON.parse(execFileSync(process.execPath, [setupScript, ...args], { env: setupEnv, encoding: 'utf8' }));
  const fixture = await startFixtureServer();
  const profile = fs.mkdtempSync(path.join(root, 'profile-'));
  let browser = null;
  const hosts = [];
  const newHost = async (env = {}) => {
    const host = new FakeHost({ socketDir: root, skillDir: install.skillDir, env });
    hosts.push(host);
    await host.start();
    return host;
  };

  try {
    // ---- owner setup, as skill_exec would run it
    const init = setup('init', '--port', String(port));
    const stored = JSON.parse(fs.readFileSync(init.credential_file, 'utf8'));
    assert.equal(fs.statSync(init.credential_file).mode & 0o777, 0o600);
    assert.ok(stored.credential.length >= 64);
    assert.ok(!JSON.stringify(init).includes(stored.credential), 'setup output must not contain the credential');
    assert.equal(init.extension_paired, false);

    browser = await launchChromium({ executable, profile, extension: path.join(install.skillDir, 'extension'), WebSocket: install.WebSocket });
    evidence.chromium = { product: browser.version.product, pid: browser.pid, args: browser.args };
    const { cdp } = browser;
    const worker = await until(async () => (await cdp.send('Target.getTargets')).targetInfos
      .find((target) => target.type === 'service_worker' && target.url.endsWith('/service_worker.js')),
    { what: 'the extension service worker in headless Chromium' });
    assert.equal(new URL(worker.url).host, init.extension_id, 'the manifest key gives the extension the id setup reports');
    const popupUrl = `chrome-extension://${init.extension_id}/popup.html`;

    const pageA = await cdp.open(`${fixture.origin}/a.html`);
    const pageB = await cdp.open(`${fixture.origin}/fg.html`, { background: true });

    /** The owner opens the popup on `page`: the harness opens popup.html as a background tab of that window. */
    const ownerPopup = async (page) => {
      await page.activate();
      const popup = await cdp.open(popupUrl, { background: true });
      await until(async () => (await popup.evaluate('document.getElementById("tab").textContent')) !== '…', { what: 'popup status' });
      popup.text = (id) => popup.evaluate(`document.getElementById(${JSON.stringify(id)}).textContent`);
      popup.press = async (id) => {
        const before = await popup.text('tab');
        await popup.click(`#${id}`);
        await sleep(250);
        return before;
      };
      return popup;
    };
    /** Does the extension itself hold a debugger attachment on this tab? (Harness inspection only.) */
    const extensionAttached = (popup, tabId) => popup.evaluate(`chrome.debugger.sendCommand({ tabId: ${Number(tabId)} }, 'Page.getFrameTree')
      .then(() => true, (error) => (/not attached/i.test(String(error.message)) ? false : String(error.message)))`);

    // ---- pairing: fixture injection of the credential through the real popup
    let popup = await ownerPopup(pageA);
    assert.match(await popup.text('summary'), /Not paired/);
    const pairing = spawn(process.execPath, [setupScript, 'pair', '--wait', '30'], { env: setupEnv, stdio: ['ignore', 'pipe', 'inherit'] });
    let pairOutput = '';
    pairing.stdout.on('data', (chunk) => { pairOutput += chunk; });
    const pairingDone = new Promise((resolve) => pairing.once('exit', resolve));
    await until(() => new Promise((resolve) => {
      const probe = net.connect(port, '127.0.0.1');
      probe.once('connect', () => { probe.destroy(); resolve(true); });
      probe.once('error', () => resolve(false));
    }), { what: 'pairing listener' });
    await popup.click('details > summary');  // the owner opens Manual pairing; Connect's native path is native_onboarding.test.mjs
    await popup.evaluate(`document.getElementById('port').value = '${port}'`);
    await popup.typeInto('#credential', stored.credential);
    await popup.press('save');
    await pairingDone;
    const paired = JSON.parse(pairOutput);
    assert.equal(paired.ok, true, pairOutput);
    assert.equal(paired.paired_origin, `chrome-extension://${init.extension_id}`);
    assert.equal(paired.is_package_extension, true);
    assert.ok(!pairOutput.includes(stored.credential));
    // The pairing listener closes right after its proof: the extension does not count that socket as a connection.
    const afterPair = await popup.evaluate('chrome.runtime.sendMessage({ type: "status" })');
    assert.equal(afterPair.connected, false, JSON.stringify(afterPair));
    await popup.close();
    note('pairing', { origin_pinned: paired.paired_origin, credential_in_setup_output: false, method: 'harness fixture injection via the popup' });

    // ================= executor 1: the main flow =================
    const host = await newHost();
    await t.test('discovery opens nothing', async () => {
      const listed = await host.request('tools/list', {});
      assert.deepEqual(listed.result.tools.map((tool) => tool.name),
        ['browser_state', 'browser_connect', 'browser_action', 'browser_execute', 'browser_handoff', 'browser_resume', 'browser_disconnect']);
      const refused = await new Promise((resolve) => {
        const probe = net.connect(port, '127.0.0.1');
        probe.once('connect', () => { probe.destroy(); resolve(false); });
        probe.once('error', () => resolve(true));
      });
      assert.ok(refused, 'initialize and tools/list must not bind the loopback port');
    });

    let state;
    await t.test('unconnected state is the exact contract and carries no secret', async () => {
      const answer = await host.call('browser_state', {}, { invocationId: null });
      assert.equal(answer.isError, false);
      assert.equal(answer.content.length, 1);
      state = answer.json;
      for (const key of CONTRACT_KEYS) assert.ok(key in state, key);
      assert.equal(state.protocol, 'ouroboros_chrome');
      assert.equal(state.version, 1);
      assert.deepEqual(state.owner, { task_id: 'task-test', attempt: '1' });
      assert.ok(Number.isInteger(state.executor_generation) && state.executor_generation >= 0);
      assert.equal(state.connected, false);
      assert.equal(state.lease, null);
      assert.deepEqual(state.closure, { revoked: false, detached: false });
      assert.ok(!answer.content[0].text.includes(stored.credential));
      assert.equal(state.pairing.listener, 'listening');
    });

    await t.test('admission: wrong origin, wrong credential and a web page are refused', async () => {
      const dial = (origin) => new Promise((resolve) => {
        const socket = new install.WebSocket(`ws://127.0.0.1:${port}`, { origin });
        socket.once('open', () => resolve({ socket }));
        socket.once('unexpected-response', (request, response) => resolve({ status: response.statusCode }));
        socket.once('error', (error) => resolve({ error: String(error.message) }));
      });
      assert.equal((await dial('http://127.0.0.1')).status, 403);
      assert.equal((await dial('chrome-extension://abcdefghijklmnopabcdefghijklmnop')).status, 403);
      assert.equal((await dial(undefined)).status, 403);
      // The right origin (forged by a local process) but no credential: nothing but a nonce comes back.
      const { socket } = await dial(paired.paired_origin);
      const received = [];
      socket.on('message', (data) => received.push(JSON.parse(data.toString('utf8'))));
      const closedWith = new Promise((resolve) => socket.once('close', (code) => resolve(code)));
      socket.send(JSON.stringify({ t: 'hello', v: 1, nonce_e: 'ab'.repeat(16) }));
      await until(() => received.length === 1, { what: 'challenge' });
      assert.deepEqual(Object.keys(received[0]).sort(), ['nonce_s', 't']);
      socket.send(JSON.stringify({ t: 'auth', proof: 'f'.repeat(64), build: 'x' }));
      assert.equal(await closedWith, 4401);
      assert.equal(received.length, 1, 'no state, owner or lease fact before authentication');
      // Sixty-four JS characters can be 128 UTF-8 bytes. This must take the
      // same ordinary bad-proof path, including its refusal counter.
      const unicode = await dial(paired.paired_origin);
      const nonAscii = unicode.socket;
      const unicodeReceived = [];
      nonAscii.on('message', (data) => unicodeReceived.push(JSON.parse(data.toString('utf8'))));
      const unicodeClosed = new Promise((resolve) => nonAscii.once('close', (code) => resolve(code)));
      nonAscii.send(JSON.stringify({ t: 'hello', v: 1, nonce_e: 'cd'.repeat(16) }));
      await until(() => unicodeReceived.length === 1, { what: 'Unicode proof challenge' });
      nonAscii.send(JSON.stringify({ t: 'auth', proof: 'é'.repeat(64), build: 'x' }));
      assert.equal(await unicodeClosed, 4401);
      assert.equal(unicodeReceived.length, 1);
      // A real page in the browser cannot open the socket either.
      const fromPage = await pageA.evaluate(`new Promise((resolve) => { const s = new WebSocket('ws://127.0.0.1:${port}');
        s.onopen = () => resolve('open'); s.onerror = () => resolve('refused'); })`);
      assert.equal(fromPage, 'refused');
      const after = (await host.call('browser_state', {}, { invocationId: null })).json;
      assert.equal(after.pairing.paired, false);
      assert.ok(after.pairing.failed_attempts >= 2, 'ASCII and non-ASCII bad proofs are counted');
      note('admission', { wrong_origin: 403, missing_origin: 403, forged_origin_bad_proof_close: 4401, unicode_bad_proof_close: 4401, page_websocket: fromPage });
    });

    let tabA;
    await t.test('the owner shares one tab; no other id can be connected', async () => {
      popup = await ownerPopup(pageA);  // opening the popup makes the extension try the port
      await until(async () => /Connected/.test(await popup.text('summary')), { what: 'extension connection' });
      state = (await host.call('browser_state', {}, { invocationId: null })).json;
      assert.equal(state.pairing.paired, true);
      assert.deepEqual(state.approved_tabs, []);
      const tabs = await popup.evaluate('chrome.tabs.query({}).then((list) => list.map((tab) => ({ id: tab.id, active: tab.active, url: tab.url || null })))');
      assert.ok(tabs.every((tab) => tab.url === null || tab.url === popupUrl), 'only the extension\'s own popup URL may be visible');
      assert.ok(tabs.filter((tab) => tab.url !== null).every((tab) => tab.url === popupUrl));
      assert.match(await popup.text('tab'), /not shared/);
      tabA = /Tab (\d+)/.exec(await popup.text('tab'))[1];
      const refused = await host.call('browser_connect', { tab_id: tabA });
      assert.equal(refused.isError, true);
      assert.equal(refused.json.error.code, 'BROWSER_TAB_NOT_APPROVED');
      assert.equal(await extensionAttached(popup, tabA), false);

      await popup.press('share');
      state = (await host.call('browser_state', {}, { invocationId: null })).json;
      assert.deepEqual(state.approved_tabs, [{ tab_id: tabA }]);
      const others = tabs.map((tab) => String(tab.id)).filter((id) => id !== tabA);
      for (const id of others) {
        const neighbour = await host.call('browser_connect', { tab_id: id });
        assert.equal(neighbour.json.error.code, 'BROWSER_TAB_NOT_APPROVED', `tab ${id} was never shared`);
        assert.equal(await extensionAttached(popup, id), false);
      }
      for (const bad of [{}, { tab_id: tabA, url: 'https://example.org/' }, { tab_id: '' }, { tab: tabA }])
        assert.equal((await host.call('browser_connect', bad)).json.error.code, 'BROWSER_POLICY_UNAVAILABLE');
      note('tab_selection', { shared: tabA, neighbours_refused: others.length });
    });

    const foreground = async () => ({
      a: await pageA.evaluate('document.visibilityState'), b: await pageB.evaluate('document.visibilityState'),
      b_log: await pageB.evaluate('window.visibility'), b_blurs: await pageB.evaluate('window.blurs'),
    });

    await t.test('connect attaches in the background and asks the guard with the armed lease', async () => {
      await pageB.activate();
      await until(async () => (await pageA.evaluate('document.visibilityState')) === 'hidden', { what: 'tab A in the background' });
      const before = await foreground();
      const answer = await host.call('browser_connect', { tab_id: tabA });
      assert.equal(answer.isError, false, JSON.stringify(answer.json));
      state = answer.json;
      assert.equal(state.connected, true);
      assert.equal(state.lease.tab_id, tabA);
      assert.equal(state.lease.url, `${fixture.origin}/a.html`);
      assert.equal(state.lease.guard_armed, true);
      assert.ok(Number.isInteger(state.lease.generation) && state.lease.generation >= 0);
      assert.ok(state.lease.lease_id && state.lease.document_id);
      const facts = host.factsFor(answer.invocationId);
      assert.equal(facts.length, 1);
      assert.deepEqual({ kind: facts[0].kind, step_id: facts[0].step_id, operation: facts[0].operation, args: facts[0].args },
        { kind: 'primitive', step_id: 1, operation: 'connect', args: { tab_id: tabA } });
      assert.deepEqual(Object.keys(facts[0].state), CONTRACT_KEYS);
      assert.deepEqual(facts[0].state.lease, state.lease);
      assert.equal(await extensionAttached(popup, tabA), true);
      assert.deepEqual(await foreground(), before, 'attaching changed no visibility and no focus');
      note('connect', { lease: state.lease, foreground: before });
    });

    await t.test('trusted click and text input reach the background tab; the foreground tab stays', async () => {
      const before = await foreground();
      assert.deepEqual([before.a, before.b], ['hidden', 'visible']);
      await pageA.evaluate('window.events.length = 0');
      const click = await host.call('browser_action', { operation: 'click', args: { selector: '#go' } });
      assert.equal(click.isError, false, JSON.stringify(click.json));
      assert.equal(click.json.steps[0].outcome, 'dispatched');
      assert.equal(await pageA.evaluate('document.getElementById("count").textContent'), '1');
      const pointer = (await pageA.evaluate('window.events')).filter((event) => event.target === 'go');
      note('debug_pointer', pointer.map((event) => event.type));
      assert.deepEqual(pointer.map((event) => event.type).filter((type) => !/move|over/.test(type)), ['pointerdown', 'mousedown', 'mouseup', 'click']);
      assert.ok(pointer.every((event) => event.trusted === true && event.visibility === 'hidden'));

      const fill = await host.call('browser_action', { operation: 'fill', args: { selector: '#name', text: 'Ada Lovelace' } });
      assert.equal(fill.json.steps[0].outcome, 'confirmed', JSON.stringify(fill.json));
      assert.equal(await pageA.evaluate('document.getElementById("name").value'), 'Ada Lovelace');
      const typed = await host.call('browser_action', { operation: 'type', args: { selector: '#email', text: 'ada@example.org' } });
      assert.equal(typed.isError, false, JSON.stringify(typed.json));
      assert.equal(await pageA.evaluate('document.getElementById("email").value'), 'ada@example.org');
      const check = await host.call('browser_action', { operation: 'check', args: { selector: '#agree' } });
      assert.equal(check.json.steps[0].outcome, 'confirmed', JSON.stringify(check.json));
      const inputs = (await pageA.evaluate('window.events')).filter((event) => ['beforeinput', 'input', 'keydown', 'change'].includes(event.type));
      assert.ok(inputs.length >= 4 && inputs.every((event) => event.trusted === true), JSON.stringify(inputs.filter((event) => !event.trusted)));
      assert.ok(inputs.some((event) => event.type === 'keydown' && event.target === 'email'));

      const guard = host.factsFor(click.invocationId);
      assert.deepEqual(guard.map((facts) => [facts.kind, facts.step_id, facts.operation]), [['primitive', 1, 'click']]);
      assert.deepEqual(guard[0].args, { selector: '#go' });
      assert.deepEqual(await foreground(), before, 'the owner\'s foreground tab saw no visibility change and no blur');
      note('trusted_input_in_background', {
        pointer_events: pointer, input_events_trusted: inputs.length, foreground_after: await foreground(),
        elapsed_ms: { click: click.elapsed_ms, fill: fill.elapsed_ms, type_15_chars: typed.elapsed_ms, check: check.elapsed_ms },
      });
    });

    await t.test('type refuses keyboard when a trusted click redirects focus to another field', async () => {
      await pageA.evaluate(`(() => {
        window.events.length = 0;
        document.getElementById('name').value = '';
        document.getElementById('email').value = '';
        window.redirectEmailFocus = () => document.getElementById('name').focus();
        document.getElementById('email').addEventListener('click', window.redirectEmailFocus);
      })()`);
      try {
        const refused = await host.call('browser_action', { operation: 'type', args: { selector: '#email', text: 'wrong field' } });
        assert.equal(refused.isError, true, JSON.stringify(refused.json));
        assert.equal(refused.json.error.code, 'BROWSER_TARGET_NOT_VISIBLE');
        assert.equal(refused.json.steps[0].outcome, 'dispatched', 'the trusted pointer click already happened');
        const result = await pageA.evaluate(`({
          active: document.activeElement.id,
          email: document.getElementById('email').value,
          name: document.getElementById('name').value,
          events: window.events.filter((event) => ['click', 'keydown', 'beforeinput', 'input'].includes(event.type))
        })`);
        assert.deepEqual([result.active, result.email, result.name], ['name', '', '']);
        assert.ok(result.events.some((event) => event.type === 'click' && event.target === 'email' && event.trusted));
        assert.equal(result.events.filter((event) => ['keydown', 'beforeinput', 'input'].includes(event.type)).length, 0);
      } finally {
        await pageA.evaluate(`document.getElementById('email').removeEventListener('click', window.redirectEmailFocus)`);
      }
      const accepted = await host.call('browser_action', { operation: 'type', args: { selector: '#email', text: 'right field' } });
      assert.equal(accepted.isError, false, JSON.stringify(accepted.json));
      assert.equal(await pageA.evaluate('document.getElementById("email").value'), 'right field');
    });

    const count = () => pageA.evaluate('document.getElementById("count").textContent');
    const action = (operation, args = {}) => host.call('browser_action', { operation, args });
    const execute = (code) => host.call('browser_execute', { code });
    const stepIds = (invocationId) => host.factsFor(invocationId).map((facts) => facts.step_id);
    const increasing = (ids) => ids.every((id, index) => Number.isInteger(id) && (index === 0 || id > ids[index - 1]));

    await t.test('an obscured, moving, ambiguous or unsupported target is refused before any input', async () => {
      const covered = await action('click', { selector: '#covered' });
      assert.equal(covered.json.error.code, 'BROWSER_TARGET_OBSCURED', JSON.stringify(covered.json));
      assert.match(covered.json.error.message, /div#overlay/);
      assert.equal(covered.json.steps[0].outcome, 'not_dispatched');
      assert.equal(await pageA.evaluate('document.getElementById("covered").dataset.clicked || ""'), '');
      // Fully transparent, itself or through an ancestor: a click the owner could not see is not sent.
      for (const id of ['ghost', 'ghostChild']) {
        const ghost = await action('click', { selector: `#${id}` });
        assert.equal(ghost.json.error.code, 'BROWSER_TARGET_NOT_VISIBLE', JSON.stringify(ghost.json));
        assert.equal(ghost.json.steps[0].outcome, 'not_dispatched');
        assert.equal(await pageA.evaluate(`document.getElementById(${JSON.stringify(id)}).dataset.clicked || ""`), '');
      }
      assert.equal((await action('click', { selector: 'input' })).json.error.code, 'BROWSER_TARGET_AMBIGUOUS');
      assert.equal((await action('click', { selector: '#absent' })).json.error.code, 'BROWSER_TARGET_NOT_FOUND');
      const unsupported = await action('select', { selector: '#name', value: 'x' });
      assert.equal(unsupported.json.error.code, 'BROWSER_OPERATION_UNSUPPORTED');
      assert.equal(host.factsFor(unsupported.invocationId).length, 0, 'an unsupported operation is not even asked');
      assert.equal((await action('get_text', { selector: '#secret' })).json.error.code, 'BROWSER_ARGUMENT_INVALID');
      const snapshot = await action('snapshot', {});
      assert.ok(!JSON.stringify(snapshot.json).includes('hunter2'), 'a password value never leaves the page');
      // Geometry that keeps changing: only observable while the tab's timers run, so the
      // harness (as the owner) brings the tab forward for this one check.
      await pageA.activate();
      await until(async () => (await pageA.evaluate('document.visibilityState')) === 'visible', { what: 'tab A visible' });
      await pageA.evaluate('window.shake(4000)');
      const moving = await action('click', { selector: '#mover' });
      assert.equal(moving.json.error.code, 'BROWSER_TARGET_UNSTABLE', JSON.stringify(moving.json));
      assert.equal(moving.json.steps[0].outcome, 'not_dispatched');
      assert.equal(await pageA.evaluate('document.getElementById("mover").dataset.clicked || ""'), '');
      await pageB.activate();
      await until(async () => (await pageA.evaluate('document.visibilityState')) === 'hidden', { what: 'tab A hidden again' });
      note('refusals', { obscured: covered.json.error.message, unstable: moving.json.error.message, transparent: 'BROWSER_TARGET_NOT_VISIBLE' });
    });

    await t.test('sequence: delayed DOM handled in one typed call, one guard question per step', async () => {
      const steps = [
        { operation: 'click', args: { selector: '#start' } },
        { operation: 'wait', args: { selector: '#step2', timeout_ms: 8000 } },
        { operation: 'fill', args: { selector: '#step2', text: 'delayed value' } },
        { operation: 'click', args: { selector: '#next' } },
        { operation: 'wait', args: { selector: '#done', text: 'done: delayed value', timeout_ms: 8000 } },
        { operation: 'get_text', args: { selector: '#done', cache_key: 'done' } },
      ];
      const answer = await action('sequence', { steps });
      assert.equal(answer.isError, false, JSON.stringify(answer.json));
      assert.equal(answer.json.result.length, 6);
      assert.equal(answer.json.result[5].value.text, 'done: delayed value');
      assert.ok(answer.json.result[1].value.polls > 1, 'the element was not there yet when the wait began');
      const facts = host.factsFor(answer.invocationId);
      assert.deepEqual(facts.map((item) => [item.kind, item.operation, item.args]), steps.map((step) => ['primitive', step.operation, step.args]));
      assert.ok(increasing(stepIds(answer.invocationId)));
      assert.deepEqual(answer.json.steps.map((step) => step.outcome), ['dispatched', 'observed', 'confirmed', 'dispatched', 'observed', 'observed']);
      const cached = await action('cached_text', { cache_key: 'done' });
      assert.equal(cached.json.result.text, 'done: delayed value');
      const failing = await action('sequence', { steps: [{ operation: 'get_text', args: { selector: 'h1' } }, { operation: 'click', args: { selector: '#absent' } }, { operation: 'click', args: { selector: '#go' } }] });
      assert.equal(failing.json.error.code, 'BROWSER_TARGET_NOT_FOUND');
      assert.equal(failing.json.partial_result[0].value.text, 'Fixture A');
      assert.equal(host.factsFor(failing.invocationId).length, 2, 'the step after the failure is never asked or sent');
      assert.equal((await action('sequence', { steps: new Array(33).fill({ operation: 'observe', args: {} }) })).json.error.code, 'BROWSER_ARGUMENT_INVALID');
      note('sequence_delayed_dom', { elapsed_ms: answer.elapsed_ms, polls: answer.json.result[1].value.polls, step_ids: stepIds(answer.invocationId) });
    });

    let generation;
    await t.test('script realm keeps values between calls; the second call touches no browser', async () => {
      const first = await execute('vars.snap = await browser.snapshot({}); vars.calls = (vars.calls || 0) + 1;'
        + ' return { n: vars.snap.elements.length, go: vars.snap.elements.find((e) => e.id === "go").handle };');
      assert.equal(first.isError, false, JSON.stringify(first.json));
      generation = first.json.state.executor_generation;
      const second = await execute('vars.calls += 1; return { calls: vars.calls, buttons: vars.snap.elements.filter((e) => e.tag === "button").map((e) => e.name) };');
      assert.equal(second.json.result.calls, 2);
      assert.ok(second.json.result.buttons.includes('Go'));
      assert.deepEqual(second.json.steps, []);
      assert.equal(host.factsFor(second.invocationId).length, 0);
      assert.equal(second.json.state.executor_generation, generation);
      const before = Number(await count());
      const byHandle = await action('click', { handle: first.json.result.go });
      assert.equal(byHandle.isError, false, JSON.stringify(byHandle.json));
      assert.equal(Number(await count()), before + 1);
      note('realm', { executor_generation: generation, cached_buttons: second.json.result.buttons.length });
    });

    await t.test('a call that arrives after its invocation ended is refused and asks nothing', async () => {
      const before = await count();
      // An un-awaited chain that outlives the script, and a reference kept for later.
      const early = await execute('vars.kept = browser; const slow = browser.wait({ selector: "#never", timeout_ms: 900 });'
        + ' await sleep(200); slow.catch(() => null).then(() => browser.click("#go")).catch((error) => { vars.late = error.code; });'
        + ' return "returned early";');
      assert.equal(early.json.result, 'returned early');
      assert.deepEqual(early.json.steps.map((step) => [step.operation, step.outcome, step.error && step.error.code]),
        [['wait', 'not_dispatched', 'BROWSER_INVOCATION_CLOSED']], 'the admitted step was settled before the receipt; no click was admitted');
      assert.ok(early.elapsed_ms < 800, `closing interrupted the 900 ms wait (${early.elapsed_ms} ms)`);
      const facts = host.facts.length;
      const later = await execute('let kept; try { await vars.kept.click("#go"); kept = "acted"; } catch (error) { kept = error.code; } return { late: vars.late, kept };');
      assert.deepEqual(later.json.result, { late: 'BROWSER_INVOCATION_CLOSED', kept: 'BROWSER_INVOCATION_CLOSED' });
      assert.equal(host.facts.length, facts, 'neither late call reached the guard');
      assert.equal(await count(), before);
      note('late_calls', later.json.result);
    });

    await t.test('navigation: Document request, then the document callback with CDP correlation; handles stale, cached text kept', async () => {
      const second = `${fixture.origin}/second.html`;
      const prior = (await host.call('browser_state', {}, { invocationId: null })).json.lease;
      const answer = await execute(`await browser.read({ cache_key: "pageA" }); const snap = await browser.snapshot({});
        vars.staleHandle = snap.elements.find((e) => e.id === "go").handle;
        const nav = await browser.navigate({ url: ${JSON.stringify(second)} }); return { nav, title: await browser.get_text("#title2") };`);
      assert.equal(answer.isError, false, JSON.stringify(answer.json));
      assert.equal(answer.json.result.title.text, 'Second page');
      assert.equal(answer.json.result.nav.committed, true);
      const facts = host.factsFor(answer.invocationId);
      assert.ok(increasing(facts.map((item) => item.step_id)), JSON.stringify(facts.map((item) => [item.kind, item.step_id])));
      const request = facts.find((item) => item.kind === 'request' && item.resource_type === 'Document');
      const document = facts.find((item) => item.kind === 'document');
      assert.ok(request && document);
      assert.deepEqual({ url: request.url, method: request.method, main: request.is_main_frame, tab: request.target_tab_id, complete: request.complete },
        { url: second, method: 'GET', main: true, tab: tabA, complete: true });
      assert.deepEqual(request.state.lease, prior, 'the Document request is judged under the old lease');
      assert.equal(document.request_id, request.request_id, 'the committed document is the request the host admitted');
      assert.deepEqual(document.previous, { lease_id: prior.lease_id, generation: prior.generation, document_id: prior.document_id });
      const next = document.state.lease;
      assert.ok(next.lease_id !== prior.lease_id && next.generation > prior.generation && next.document_id !== prior.document_id);
      assert.equal(next.url, second);
      assert.equal(next.tab_id, tabA);
      assert.ok(facts.indexOf(request) < facts.indexOf(document));
      const after = facts[facts.length - 1];
      assert.deepEqual([after.kind, after.operation], ['primitive', 'get_text']);
      assert.deepEqual(after.state.lease, next, 'the step after the navigation runs under the witnessed lease');
      assert.deepEqual(answer.json.state.lease, next);
      assert.deepEqual((await host.call('browser_state', {}, { invocationId: null })).json.lease, next);

      const stale = await execute('try { await browser.click({ handle: vars.staleHandle }); return "acted"; } catch (error) { return error.code; }');
      assert.equal(stale.json.result, 'BROWSER_HANDLE_STALE');
      assert.equal(stale.json.steps[0].outcome, 'not_dispatched');
      const kept = await action('cached_text', { cache_key: 'pageA', find: 'Fixture' });
      assert.equal(kept.json.result.matches[0].text, 'Fixture A');

      const hop = await action('navigate', { url: `${fixture.origin}/go-second` });
      assert.equal(hop.isError, false, JSON.stringify(hop.json));
      const hopFacts = host.factsFor(hop.invocationId);
      const hops = hopFacts.filter((item) => item.kind === 'request' && item.resource_type === 'Document');
      assert.deepEqual(hops.map((item) => item.url), [`${fixture.origin}/go-second`, second], 'a redirect hop is judged as its own request');
      assert.equal(hopFacts.find((item) => item.kind === 'document').request_id, hops[1].request_id);

      const blockedBefore = fixture.requests.length;
      const lease = (await host.call('browser_state', {}, { invocationId: null })).json.lease;
      const blocked = await action('navigate', { url: `${fixture.origin}/blocked/page` });
      assert.equal(blocked.json.error.code, 'BROWSER_REQUEST_BLOCKED', JSON.stringify(blocked.json));
      assert.ok(blocked.json.blocked_requests[0].includes('/blocked/page'));
      assert.equal(fixture.requests.slice(blockedBefore).filter((item) => item.path.startsWith('/blocked')).length, 0, 'the refused request never left Chrome');
      assert.deepEqual(blocked.json.state.lease, lease, 'a refused navigation leaves the document and the lease as they were');
      assert.equal((await action('get_text', { selector: '#title2' })).json.result.text, 'Second page');
      note('navigation', {
        document_request: { request_id: request.request_id, url: request.url, resource_type: request.resource_type, is_main_frame: request.is_main_frame },
        document_callback: { request_id: document.request_id, previous: document.previous, lease: next, step_id: document.step_id },
        redirect_hops: hops.map((item) => item.url), refused_navigation_kept_document: true,
      });
    });

    await t.test('a click that leaves the page is followed inside its invocation; a later page-started navigation is refused', async () => {
      const back = await execute('const click = await browser.click("#back"); return { click, title: await browser.get_text("h1") };');
      assert.equal(back.isError, false, JSON.stringify(back.json));
      assert.equal(back.json.result.title.text, 'Fixture A');
      assert.equal(back.json.result.click.navigated.url, `${fixture.origin}/a.html`);
      const facts = host.factsFor(back.invocationId);
      assert.deepEqual(facts.map((item) => item.kind === 'primitive' ? item.operation : item.kind === 'request' ? `request:${item.resource_type}` : 'document'),
        ['click', 'request:Document', 'document', 'get_text']);
      assert.equal(facts[2].request_id, facts[1].request_id);
      assert.ok(increasing(facts.map((item) => item.step_id)));
      // The page starts a navigation by itself about a second after the click, when no call is running.
      const lease = back.json.state.lease;
      const loadId = await pageA.evaluate('document.getElementById("loadId").textContent');
      const clicked = await action('click', { selector: '#later' });
      assert.equal(clicked.isError, false);
      assert.equal(clicked.json.result.navigated, null);
      assert.equal(clicked.json.result.navigation, null, 'the click itself asked for no navigation');
      const orphan = await until(() => host.facts.find((item) => item.kind === 'request' && item.invocation_id === null && item.url.endsWith('/second.html')),
        { timeout: 6000, what: 'the page-started Document request' });
      assert.deepEqual([orphan.resource_type, orphan.is_main_frame, orphan.step_id], ['Document', true, null]);
      assert.match(orphan.verdict, /no active Chrome invocation/);
      await sleep(400);
      assert.equal(await pageA.evaluate('document.getElementById("loadId").textContent'), loadId, 'the refused navigation left the document alone');
      assert.deepEqual((await host.call('browser_state', {}, { invocationId: null })).json.lease, lease);
      note('click_navigation', { followed_in_invocation: facts.map((item) => [item.kind, item.step_id]), page_started_later: { verdict: orphan.verdict, document_kept: true } });
    });

    await t.test('paused requests carry method, complete body and redirect hops; a refused one never leaves', async () => {
      const seen = fixture.requests.length;
      const answer = await action('sequence', { steps: [
        { operation: 'click', args: { selector: '#fetchOk' } },
        { operation: 'click', args: { selector: '#fetchBlocked' } },
        { operation: 'click', args: { selector: '#postRedirect' } },
        { operation: 'wait', args: { selector: '#net', text: 'redirect:', timeout_ms: 8000 } },
        { operation: 'get_text', args: { selector: '#net' } },
      ] });
      assert.equal(answer.isError, false, JSON.stringify(answer.json));
      const text = answer.json.result[4].value.text;
      assert.match(text, /ok:200 echoPOST \/echo 16/);
      assert.match(text, /blocked:FAILED/);
      assert.match(text, /redirect:200 echoPOST \/echo 12/);
      const requests = host.factsFor(answer.invocationId).filter((item) => item.kind === 'request');
      assert.deepEqual(requests.map((item) => item.has_post_data), [true, false, true, true]);
      const summary = requests.map((item) => ({ url: new URL(item.url).pathname, method: item.method, post_data: item.post_data, complete: item.complete, type: item.resource_type, verdict: item.verdict }));
      assert.deepEqual(summary, [
        { url: '/echo', method: 'POST', post_data: 'alpha=1&beta=two', complete: true, type: 'XHR', verdict: '' },
        { url: '/blocked/data', method: 'GET', post_data: null, complete: true, type: 'XHR', verdict: 'BROWSER_REQUEST_BLOCKED: fixture policy' },
        { url: '/redirect', method: 'POST', post_data: 'carried=body', complete: true, type: 'XHR', verdict: '' },
        { url: '/echo', method: 'POST', post_data: 'carried=body', complete: true, type: 'XHR', verdict: '' },
      ]);
      assert.ok(increasing(stepIds(answer.invocationId)));
      const server = fixture.requests.slice(seen).map((item) => `${item.method} ${item.path} ${item.body}`);
      assert.deepEqual(server, ['POST /echo alpha=1&beta=two', 'POST /redirect carried=body', 'POST /echo carried=body']);
      assert.ok(answer.json.blocked_requests[0].includes('/blocked/data'));
      note('requests', summary);
    });

    await t.test('a form post is a main-frame Document request with its complete body', async () => {
      const posted = await execute('await browser.fill({ selector: "#name", text: "Grace Hopper" });'
        + ' const click = await browser.click("#send"); return { click, echo: await browser.get_text("#echo") };');
      assert.equal(posted.isError, false, JSON.stringify(posted.json));
      assert.match(posted.json.result.echo.text, /^POST \/echo \d+$/);
      const facts = host.factsFor(posted.invocationId);
      const post = facts.find((item) => item.kind === 'request');
      assert.equal(post.has_post_data, true);
      assert.deepEqual([post.method, post.resource_type, post.is_main_frame, post.complete], ['POST', 'Document', true, true]);
      assert.match(post.post_data, /name=Grace\+Hopper/);
      assert.ok(!post.post_data.includes('hunter2') || post.post_data.includes('secret='), 'the body is whatever the form sends, unedited');
      assert.equal(facts.find((item) => item.kind === 'document').request_id, post.request_id);
      assert.ok(increasing(facts.map((item) => item.step_id)));
      assert.equal(posted.json.result.click.navigation.status, 'committed', JSON.stringify(posted.json.result.click));
      assert.equal((await action('navigate', { url: `${fixture.origin}/a.html` })).isError, false);
      note('form_post', { method: post.method, resource_type: post.resource_type, complete: post.complete, body_chars: post.post_data.length });
    });

    // The page (the harness stands in for its own script) holds a navigation in beforeunload, so Chrome starts the
    // POST the click asked for well after the click's 120 ms grace.
    const holdNavigation = (ms) => pageA.evaluate(`window.addEventListener('beforeunload', () => { const end = Date.now() + ${ms}; while (Date.now() < end); }); true`);

    await t.test('a form post Chrome starts late is still the click\'s: judged in its invocation, committed before the next read', async () => {
      const rounds = [];
      for (let round = 0; round < 3; round += 1) {
        await holdNavigation(600);
        const seen = fixture.requests.length;
        const posted = await execute(`await browser.fill({ selector: "#name", text: "Late ${round}" });`
          + ' const click = await browser.click("#send"); return { click, echo: await browser.get_text("#echo") };');
        assert.equal(posted.isError, false, JSON.stringify(posted.json));
        const { click, echo } = posted.json.result;
        assert.equal(click.navigation.status, 'committed', JSON.stringify(click));
        assert.equal(click.navigated.document_id, click.navigation.document_id);
        assert.match(echo.text, /^POST \/echo \d+$/);
        const facts = host.factsFor(posted.invocationId);
        assert.deepEqual(facts.map((item) => item.kind === 'primitive' ? item.operation : item.kind === 'request' ? `request:${item.resource_type}` : 'document'),
          ['fill', 'click', 'request:Document', 'document', 'get_text'], 'the late request is asked inside the click, before the next step');
        const post = facts[2];
        assert.deepEqual([post.method, post.is_main_frame, post.verdict, post.causal_step_id], ['POST', true, '', facts[1].step_id]);
        assert.match(post.post_data, new RegExp(`name=Late\\+${round}`));
        assert.equal(facts[3].request_id, post.request_id);
        assert.deepEqual(posted.json.steps.map((step) => [step.operation, step.outcome]),
          [['fill', 'confirmed'], ['click', 'dispatched'], ['document', 'observed'], ['get_text', 'observed']]);
        assert.deepEqual(fixture.requests.slice(seen).map((item) => `${item.method} ${item.path}`), ['POST /echo'], 'sent once, never repeated');
        assert.equal(host.facts.filter((item) => item.invocation_id === null && item.kind === 'request').some((item) => item.url.endsWith('/echo')), false,
          'no request of the post was left for the time between calls');
        rounds.push(posted.elapsed_ms);
        assert.equal((await action('navigate', { url: `${fixture.origin}/a.html` })).isError, false);
      }
      note('late_form_post', { rounds_ms: rounds });
    });

    await t.test('a form post the host refuses is reported refused by the click; the request never leaves and the page stays', async () => {
      const lease = (await host.call('browser_state', {}, { invocationId: null })).json.lease;
      const loadId = await pageA.evaluate('document.getElementById("loadId").textContent');
      await pageA.evaluate('document.getElementById("form").action = "/blocked/form"; true');
      await holdNavigation(400);
      const seen = fixture.requests.length;
      const refused = await action('click', { selector: '#send' });
      assert.equal(refused.isError, false, JSON.stringify(refused.json));
      assert.equal(refused.json.result.navigated, null);
      assert.equal(refused.json.result.navigation.status, 'refused', JSON.stringify(refused.json.result));
      assert.match(refused.json.result.navigation.url, /\/blocked\/form$/);
      assert.ok(refused.json.blocked_requests[0].includes('/blocked/form'));
      const request = host.factsFor(refused.invocationId).find((item) => item.kind === 'request');
      assert.deepEqual([request.method, request.resource_type, request.causal_step_id], ['POST', 'Document', 1]);
      assert.equal(fixture.requests.slice(seen).length, 0, 'the refused post never left Chrome');
      await sleep(300);
      assert.equal(await pageA.evaluate('document.getElementById("loadId").textContent'), loadId, 'the page kept its document');
      assert.deepEqual(refused.json.state.lease, lease);
      const plain = await action('click', { selector: '#go' });
      assert.equal(plain.json.result.navigation, null, 'a click that asks for nothing claims nothing');
      // A fresh a.html: neither the changed action nor the held beforeunload outlives this test.
      assert.equal((await action('navigate', { url: `${fixture.origin}/a.html` })).isError, false);
      note('refused_form_post', { status: refused.json.result.navigation.status, blocked: refused.json.blocked_requests });
    });

    await t.test('screenshot of the leased background tab', async () => {
      const before = await foreground();
      const shot = await action('screenshot', {});
      if (shot.isError) {
        assert.equal(shot.json.error.code, 'BROWSER_SCREENSHOT_UNAVAILABLE', JSON.stringify(shot.json));
        note('screenshot', { available: false, reason: shot.json.error.message });
      } else {
        assert.equal(shot.images.length, 1);
        const png = Buffer.from(shot.images[0].data, 'base64');
        assert.deepEqual(Array.from(png.subarray(0, 8)), [137, 80, 78, 71, 13, 10, 26, 10]);
        fs.mkdirSync(evidenceDir, { recursive: true });
        fs.writeFileSync(path.join(evidenceDir, 'target-screenshot.png'), png);
        const element = await action('screenshot', { selector: '#form' });
        assert.equal(element.isError, false, JSON.stringify(element.json));
        fs.writeFileSync(path.join(evidenceDir, 'target-form-screenshot.png'), Buffer.from(element.images[0].data, 'base64'));
        note('screenshot', { available: true, bytes: png.length, width: png.readUInt32BE(16), height: png.readUInt32BE(20), elapsed_ms: shot.elapsed_ms, tab_was: before.a });
      }
      assert.deepEqual(await foreground(), before);
    });

    await t.test('handoff keeps the page and its unsaved fields; resume reads the same document afresh', async () => {
      const loadId = await pageA.evaluate('document.getElementById("loadId").textContent');
      const filled = await action('fill', { selector: '#name', text: 'Ada Lovelace' });
      assert.equal(filled.json.steps[0].outcome, 'confirmed');
      const prior = filled.json.state.lease;
      const handoff = await host.call('browser_handoff', {});
      assert.equal(handoff.isError, false, JSON.stringify(handoff.json));
      assert.deepEqual([handoff.json.connected, handoff.json.lease, handoff.json.phase], [false, null, 'handoff']);
      assert.deepEqual(handoff.json.closure, { revoked: false, detached: true });
      assert.deepEqual(handoff.json.last_closure, { reason: 'handoff', debugger_detach_ack: true });
      assert.equal(await extensionAttached(popup, tabA), false, 'Chrome confirms the extension is detached');
      const during = await action('click', { selector: '#go' });
      assert.equal(during.json.error.code, 'BROWSER_HANDOFF_ACTIVE');
      assert.equal(host.factsFor(during.invocationId).length, 0);
      // The owner works in the page (the harness types as the owner would).
      await pageA.typeInto('#notes', 'the owner wrote this by hand');
      const resumed = await host.call('browser_resume', {});
      assert.equal(resumed.isError, false, JSON.stringify(resumed.json));
      const lease = resumed.json.lease;
      assert.ok(lease.lease_id !== prior.lease_id && lease.generation > prior.generation);
      assert.equal(lease.document_id, prior.document_id, 'same document: the page was not reloaded');
      assert.deepEqual(resumed.json.closure, { revoked: false, detached: false });
      const resumeFacts = host.factsFor(resumed.invocationId);
      assert.deepEqual([resumeFacts[0].kind, resumeFacts[0].operation, resumeFacts[0].args, resumeFacts[0].step_id], ['primitive', 'resume', {}, 1]);
      assert.deepEqual(resumeFacts[0].state.lease, lease);
      const values = await action('sequence', { steps: [
        { operation: 'get_text', args: { selector: '#name' } }, { operation: 'get_text', args: { selector: '#notes' } },
        { operation: 'get_text', args: { selector: '#loadId' } },
      ] });
      assert.deepEqual(values.json.result.map((item) => item.value.text), ['Ada Lovelace', 'the owner wrote this by hand', loadId]);
      note('handoff_resume', { detach_ack: true, same_document: true, fields_after_resume: values.json.result.map((item) => item.value.text) });
    });

    await t.test('handoff is out of band: it stops a running step and nothing queued behind it is sent', async () => {
      const before = await count();
      const running = execute('const slow = browser.wait({ selector: "#never", timeout_ms: 20000 }).catch((error) => error.code);'
        + ' const click = browser.click("#go").catch((error) => error.code); return [await slow, await click];');
      await sleep(400);
      const handoff = await host.call('browser_handoff', {});
      assert.ok(handoff.elapsed_ms < 2000, `handoff did not wait for the script (${handoff.elapsed_ms} ms)`);
      assert.equal(handoff.json.last_closure.debugger_detach_ack, true);
      const receipt = await running;
      assert.ok(receipt.elapsed_ms < 5000, `the 20 s wait was cut short (${receipt.elapsed_ms} ms)`);
      assert.deepEqual(receipt.json.result.length, 2);
      assert.ok(['BROWSER_INVOCATION_CLOSED', 'BROWSER_LEASE_CHANGED'].includes(receipt.json.result[0]), receipt.json.result[0]);
      assert.equal(receipt.json.result[1], 'BROWSER_INVOCATION_CLOSED');
      assert.deepEqual(receipt.json.steps.map((step) => [step.operation, step.outcome]), [['wait', 'not_dispatched']]);
      assert.equal(await count(), before, 'the click queued behind the wait never happened');
      assert.equal((await host.call('browser_resume', {})).isError, false);
      note('out_of_band_handoff', { handoff_ms: handoff.elapsed_ms, script_ended_ms: receipt.elapsed_ms, results: receipt.json.result });
    });

    await t.test('a handoff ends a script waiting on its own promise; a sleep it dropped does not end the executor', async () => {
      const running = execute('(async () => { await sleep(20000); })(); await new Promise(() => {}); return "never";');
      await sleep(300);
      const handoff = await host.call('browser_handoff', {});
      assert.equal(handoff.json.phase, 'handoff', JSON.stringify(handoff.json));
      const receipt = await running;
      assert.equal(receipt.json.error.code, 'BROWSER_INVOCATION_CLOSED', JSON.stringify(receipt.json));
      assert.ok(receipt.elapsed_ms < 3000, `cut at the handoff, not at the execute timeout (${receipt.elapsed_ms} ms)`);
      await sleep(300);
      assert.equal(host.child.exitCode, null, `the executor still runs: ${host.stderr.slice(-400)}`);
      assert.match(host.stderr, /"event":"dropped_refusal","code":"BROWSER_INVOCATION_CLOSED"/);
      assert.equal((await host.call('browser_resume', {})).isError, false);
      note('own_promise_cut', { script_ended_ms: receipt.elapsed_ms, executor_alive: true });
    });

    await t.test('real MV3 resume racing disconnect cannot resurrect the old handoff', async () => {
      assert.equal((await host.call('browser_handoff', {})).json.phase, 'handoff');
      let answerResume;
      const held = new Promise((resolve) => { answerResume = resolve; });
      host.policy = (facts) => facts.kind === 'primitive' && facts.operation === 'resume'
        ? held : host.defaultPolicy(facts);
      try {
        const resuming = host.call('browser_resume', {});
        await until(() => host.facts.some((facts) => facts.operation === 'resume' && !('verdict' in facts)),
          { what: 'pending real MV3 resume guard' });
        const disconnecting = host.call('browser_disconnect', {});
        await until(async () => !(await extensionAttached(popup, tabA)), { what: 'disconnect detached the resumed tab' });
        answerResume('BROWSER_POLICY_UNAVAILABLE: refused resume');
        const disconnected = await disconnecting;
        assert.equal(disconnected.json.phase, 'idle', JSON.stringify(disconnected.json));
        const refused = await resuming;
        assert.equal(refused.json.error.code, 'BROWSER_POLICY_UNAVAILABLE', JSON.stringify(refused.json));
        const after = (await host.call('browser_state', {}, { invocationId: null })).json;
        assert.equal(after.phase, 'idle', JSON.stringify(after));
        assert.equal(after.handoff, undefined);
        const fresh = await host.call('browser_connect', { tab_id: tabA });
        assert.equal(fresh.isError, false, JSON.stringify(fresh.json));
        note('resume_disconnect_race', { detached: true, stale_handoff: false, new_connect: true });
      } finally {
        answerResume('BROWSER_POLICY_UNAVAILABLE: refused resume');
        host.policy = (facts) => host.defaultPolicy(facts);
      }
    });

    await t.test('disconnect reports Chrome\'s detach; an agent-opened background tab comes and goes without activation', async () => {
      const disconnected = await host.call('browser_disconnect', {});
      assert.deepEqual([disconnected.json.connected, disconnected.json.lease], [false, null]);
      assert.deepEqual(disconnected.json.closure, { revoked: false, detached: true });
      assert.deepEqual(disconnected.json.last_closure, { reason: 'disconnect', debugger_detach_ack: true });
      assert.equal(await extensionAttached(popup, tabA), false);

      const tabsBefore = await popup.evaluate('chrome.tabs.query({}).then((list) => list.map((tab) => ({ id: tab.id, active: tab.active })))');
      const before = await foreground();
      const own = await host.call('browser_connect', { url: `${fixture.origin}/second.html` });
      assert.equal(own.isError, false, JSON.stringify(own.json));
      assert.equal(own.json.lease.url, `${fixture.origin}/second.html`);
      const ownFacts = host.factsFor(own.invocationId);
      assert.deepEqual([ownFacts[0].operation, ownFacts[0].args, ownFacts[0].step_id], ['connect', { url: `${fixture.origin}/second.html` }, 1]);
      const tabsDuring = await popup.evaluate('chrome.tabs.query({}).then((list) => list.map((tab) => ({ id: tab.id, active: tab.active })))');
      assert.equal(tabsDuring.length, tabsBefore.length + 1);
      assert.deepEqual(tabsDuring.filter((tab) => tab.active).map((tab) => tab.id), tabsBefore.filter((tab) => tab.active).map((tab) => tab.id), 'the active tab did not change');
      assert.ok(!tabsBefore.some((tab) => String(tab.id) === own.json.lease.tab_id));
      const typed = await action('fill', { selector: '#q', text: 'typed in the agent tab' });
      assert.equal(typed.json.steps[0].outcome, 'confirmed', JSON.stringify(typed.json));
      assert.deepEqual(await foreground(), before);
      const closed = await host.call('browser_disconnect', {});
      assert.equal(closed.json.last_closure.debugger_detach_ack, true);
      const tabsAfter = await popup.evaluate('chrome.tabs.query({}).then((list) => list.map((tab) => tab.id))');
      assert.deepEqual(tabsAfter, tabsBefore.map((tab) => tab.id), 'the agent\'s own tab was closed again');
      note('own_background_tab', { tab_id: own.json.lease.tab_id, active_tab_unchanged: true, closed_on_disconnect: true });
    });

    /** The harness, standing in for the owner, opens the page of a tab the extension created. */
    const pageOf = async (url, except = []) => {
      const target = await until(async () => (await cdp.send('Target.getTargets')).targetInfos
        .find((info) => info.type === 'page' && info.url === url && !except.includes(info.targetId)), { what: `the tab at ${url}` });
      const { sessionId } = await cdp.send('Target.attachToTarget', { targetId: target.targetId, flatten: true });
      return new Page(cdp, target.targetId, sessionId);
    };
    const tabIds = (view) => view.evaluate('chrome.tabs.query({}).then((list) => list.map((tab) => String(tab.id)))');
    const backToA = async () => {
      await pageA.evaluate('location.assign("/a.html")').catch(() => null);
      await until(async () => (await pageA.evaluate('document.readyState === "complete" && document.title').catch(() => '')) === 'Fixture A', { what: 'tab A back on a.html' });
    };

    await t.test('a same-document URL change keeps the lease; a refused document is released and fails its call', async () => {
      const connected = await host.call('browser_connect', { tab_id: tabA });
      assert.equal(connected.isError, false, JSON.stringify(connected.json));
      const lease = connected.json.lease;
      try {
        // The page changes its URL by history.pushState while the host is deciding the step.
        host.policy = async (facts) => {
          if (facts.kind === 'primitive' && facts.operation === 'get_text') {
            await pageA.evaluate('history.pushState({}, "", "#pushed")');
            await sleep(300);
          }
          return host.defaultPolicy(facts);
        };
        const read = await action('get_text', { selector: 'h1' });
        assert.equal(read.isError, false, JSON.stringify(read.json));
        assert.equal(read.json.result.text, 'Fixture A');
        assert.deepEqual([read.json.state.lease.lease_id, read.json.state.lease.generation, read.json.state.lease.url],
          [lease.lease_id, lease.generation, `${fixture.origin}/a.html#pushed`]);
        // The host admits the Document request, then refuses the committed document while the new page changes its URL.
        host.policy = async (facts) => {
          if (facts.kind !== 'document') return host.defaultPolicy(facts);
          await until(() => pageA.evaluate('history.replaceState({}, "", "#after-commit"); true').catch(() => false), { what: 'replaceState in the new document' });
          await sleep(300);
          return 'BROWSER_REQUEST_BLOCKED: fixture refuses this document';
        };
        const nav = await action('navigate', { url: `${fixture.origin}/second.html` });
        assert.equal(nav.isError, true, JSON.stringify(nav.json));
        assert.equal(nav.json.error.code, 'BROWSER_REQUEST_BLOCKED');
        const navStep = nav.json.steps.find((step) => step.operation === 'navigate');
        assert.ok(['confirmed', 'dispatched'].includes(navStep.outcome), JSON.stringify(nav.json.steps));
        assert.equal(nav.json.steps.find((step) => step.operation === 'document').error.code, 'BROWSER_REQUEST_BLOCKED');
        assert.deepEqual([nav.json.state.connected, nav.json.state.lease], [false, null]);
        const after = (await host.call('browser_state', {}, { invocationId: null })).json;
        assert.deepEqual([after.connected, after.phase, after.last_closure.reason, after.last_closure.debugger_detach_ack],
          [false, 'idle', 'navigation_refused', true]);
        assert.equal(await extensionAttached(popup, tabA), false, 'Chrome confirms the refused lease is detached');
        assert.equal(await pageA.evaluate('document.title'), 'Fixture Second', 'the commit had happened; the page was left on it');
        note('refused_document', { url_during_primitive: read.json.state.lease.url, error: nav.json.error.code, nav_outcome: navStep.outcome, last_closure: after.last_closure });
      } finally {
        host.policy = (facts) => host.defaultPolicy(facts);
        if ((await host.call('browser_state', {}, { invocationId: null })).json.connected) await host.call('browser_disconnect', {});
        await backToA();
      }
    });

    await t.test('an agent-opened tab handed to the owner is the owner\'s: resume works, and EOF does not close it', async () => {
      if ((await host.call('browser_state', {}, { invocationId: null })).json.connected) await host.call('browser_disconnect', {});
      const own = await host.call('browser_connect', { url: `${fixture.origin}/second.html` });
      assert.equal(own.isError, false, JSON.stringify(own.json));
      const ownTab = own.json.lease.tab_id;
      assert.equal((await action('fill', { selector: '#q', text: 'agent text' })).json.steps[0].outcome, 'confirmed');
      assert.equal((await host.call('browser_handoff', {})).json.phase, 'handoff');
      const ownerPage = await pageOf(`${fixture.origin}/second.html`);
      await ownerPage.evaluate('document.getElementById("q").value = "the owner\'s unsaved draft"');
      const resumed = await host.call('browser_resume', {});
      assert.equal(resumed.isError, false, JSON.stringify(resumed.json));
      assert.equal((await action('get_text', { selector: '#q' })).json.result.text, 'the owner\'s unsaved draft');
      assert.equal((await host.call('browser_handoff', {})).json.phase, 'handoff');
      const shutdown = await host.stop();
      assert.deepEqual(shutdown, { event: 'shutdown', debugger_detach_ack: null }, 'nothing was attached at EOF, so no ack is claimed');
      await sleep(800);
      assert.ok((await tabIds(popup)).includes(ownTab), 'the owner-held tab survived the executor\'s exit');
      assert.equal(await ownerPage.evaluate('document.getElementById("q").value'), 'the owner\'s unsaved draft');
      await ownerPage.close();
      note('owner_held_agent_tab', { tab_id: ownTab, survived: ['resume', 'eof'] });
    });

    assert.deepEqual(host.violations, [], 'every guard question satisfied the host\'s wire rules');
    fs.mkdirSync(evidenceDir, { recursive: true });
    fs.writeFileSync(path.join(evidenceDir, 'guard-facts.json'), `${JSON.stringify(host.facts, null, 1)}\n`);
    await popup.close();
    evidence.shutdown_without_lease = await host.stop();

    await t.test('an agent-opened tab handed to the owner survives a restart of the extension worker', async () => {
      const run = await newHost();
      await run.call('browser_state', {}, { invocationId: null });
      const view = await ownerPopup(pageA);
      await until(async () => /Connected/.test(await view.text('summary')), { what: 'extension connection' });
      await pageB.activate();
      const own = await run.call('browser_connect', { url: `${fixture.origin}/second.html?restart` });
      assert.equal(own.isError, false, JSON.stringify(own.json));
      const ownTab = own.json.lease.tab_id;
      assert.equal((await run.call('browser_handoff', {})).json.phase, 'handoff');
      await view.close();
      const dying = (await cdp.send('Target.getTargets')).targetInfos
        .find((target) => target.type === 'service_worker' && target.url.endsWith('/service_worker.js'));
      await cdp.send('Target.closeTarget', { targetId: dying.targetId });
      const woken = await ownerPopup(pageA);  // a new worker runs its start-up clean-up
      await until(async () => /Connected/.test(await woken.text('summary')), { what: 'extension reconnection' });
      await sleep(800);
      assert.ok((await tabIds(woken)).includes(ownTab), 'the restarted worker left the owner-held tab open');
      const left = await pageOf(`${fixture.origin}/second.html?restart`);
      await left.close();
      await woken.close();
      await run.stop();
      note('owner_held_agent_tab_worker_restart', { tab_id: ownTab, survived: true });
    });

    /** A new task attempt: a fresh executor; the owner opens the popup and shares tab A again. */
    const attempt = async (env = {}) => {
      for (const earlier of hosts) await earlier.stop();
      const next = await newHost(env);
      await next.call('browser_state', {}, { invocationId: null });
      const view = await ownerPopup(pageA);
      await until(async () => /Connected/.test(await view.text('summary')), { what: 'extension connection' });
      await view.press('share');
      await pageB.activate();
      await until(async () => (await pageA.evaluate('document.visibilityState')) === 'hidden', { what: 'tab A in the background' });
      const connected = await next.call('browser_connect', { tab_id: tabA });
      assert.equal(connected.isError, false, JSON.stringify(connected.json));
      return { host: next, popup: view, lease: connected.json.lease };
    };

    // ================= executor 2: the owner revokes =================
    await t.test('the owner stops sharing: revoked, detached, and final for this executor', async () => {
      const run = await attempt();
      assert.notEqual(run.lease.lease_id, state.lease.lease_id);
      assert.notEqual((await run.host.call('browser_state', {}, { invocationId: null })).json.executor_generation, generation,
        'a new process is a new executor generation');
      assert.equal(await extensionAttached(run.popup, tabA), true);
      await run.popup.press('stop');
      const after = (await run.host.call('browser_state', {}, { invocationId: null })).json;
      assert.deepEqual([after.connected, after.lease, after.phase], [false, null, 'closed']);
      assert.deepEqual(after.closure, { revoked: true, detached: true });
      assert.deepEqual(after.last_closure, { reason: 'owner_revoked', debugger_detach_ack: true });
      assert.deepEqual(after.approved_tabs, []);
      assert.equal(await extensionAttached(run.popup, tabA), false);
      assert.equal((await run.host.call('browser_action', { operation: 'click', args: { selector: '#go' } })).json.error.code, 'BROWSER_SESSION_REVOKED');
      await run.popup.press('share');
      assert.equal((await run.host.call('browser_connect', { tab_id: tabA })).json.error.code, 'BROWSER_SESSION_REVOKED');
      assert.equal(await extensionAttached(run.popup, tabA), false);
      assert.deepEqual(run.host.violations, []);
      await run.popup.close();
      await run.host.stop();
      note('owner_revocation', after.closure);
    });

    // ================= executor 3: timeout, then stdio EOF with a live lease =================
    await t.test('a script that runs out of time is closed on both sides; EOF asks for the detach and reports the ack', async () => {
      const run = await attempt({ OUROBOROS_CHROME_EXECUTE_TIMEOUT_MS: '1200' });
      const before = await count();
      const timed = await run.host.call('browser_execute', { code: 'vars.kept = browser; await browser.wait({ selector: "#never", timeout_ms: 20000 }); return "never";' });
      assert.equal(timed.json.error.code, 'BROWSER_EXECUTE_TIMEOUT', JSON.stringify(timed.json));
      assert.ok(timed.elapsed_ms < 4000, `the 20 s wait ended with the invocation (${timed.elapsed_ms} ms)`);
      assert.deepEqual(timed.json.steps.map((step) => [step.operation, step.outcome, step.error.code]), [['wait', 'not_dispatched', 'BROWSER_INVOCATION_CLOSED']]);
      const facts = run.host.facts.length;
      const late = await run.host.call('browser_execute', { code: 'try { await vars.kept.click("#go"); return "acted"; } catch (error) { return error.code; }' });
      assert.equal(late.json.result, 'BROWSER_INVOCATION_CLOSED');
      assert.equal(run.host.facts.length, facts);
      assert.equal(await count(), before);
      assert.equal((await run.host.call('browser_action', { operation: 'click', args: { selector: '#go' } })).isError, false, 'the lease itself is still good');
      assert.deepEqual(run.host.violations, []);
      const shutdown = await run.host.stop();
      assert.deepEqual(shutdown, { event: 'shutdown', debugger_detach_ack: true });
      await until(async () => (await extensionAttached(run.popup, tabA)) === false, { what: 'detach after EOF' });
      await run.popup.close();
      note('timeout_and_eof', { timeout_ms: timed.elapsed_ms, late_call: late.json.result, shutdown });
    });

    // ================= executor 4: the transport dies under a running input step =================
    await t.test('a lost transport leaves the step unknown, keeps the partial effect, and nothing is repeated or re-attached', async () => {
      const run = await attempt();
      await pageA.evaluate('document.getElementById("notes").value = ""');
      const typing = run.host.call('browser_action', { operation: 'type', args: { selector: '#notes', text: 'x'.repeat(600) } });
      await until(async () => (await pageA.evaluate('document.getElementById("notes").value.length')) > 20, { what: 'typing under way' });
      // The extension's service worker dies mid-step, and its socket with it.
      const dying = (await cdp.send('Target.getTargets')).targetInfos
        .find((target) => target.type === 'service_worker' && target.url.endsWith('/service_worker.js'));
      await cdp.send('Target.closeTarget', { targetId: dying.targetId });
      const lost = await typing;
      assert.equal(lost.json.error.code, 'BROWSER_ACTION_OUTCOME_UNKNOWN', JSON.stringify(lost.json));
      assert.deepEqual(lost.json.steps.map((step) => [step.operation, step.outcome]), [['type', 'unknown']]);
      assert.deepEqual([lost.json.state.connected, lost.json.state.lease], [false, null]);
      const partial = await pageA.evaluate('document.getElementById("notes").value.length');
      assert.ok(partial > 20 && partial < 600, `partial effect kept: ${partial} of 600 characters`);
      await run.popup.close().catch(() => null);
      const view = await ownerPopup(pageA);  // opening the popup wakes a new worker, which cleans up and reconnects
      await until(async () => /Connected/.test(await view.text('summary')), { what: 'extension reconnection' });
      await sleep(1200);
      assert.equal(await pageA.evaluate('document.getElementById("notes").value.length'), partial, 'nothing typed the rest later');
      const after = (await run.host.call('browser_state', {}, { invocationId: null })).json;
      assert.equal(after.pairing.paired, true, 'the paired extension is back');
      assert.deepEqual([after.connected, after.lease, after.transport_lost, after.phase], [false, null, true, 'closed']);
      assert.deepEqual(after.closure, { revoked: false, detached: false }, 'no detach was confirmed, so none is claimed');
      assert.deepEqual(after.last_closure, { reason: 'transport_lost', debugger_detach_ack: false });
      assert.equal(await extensionAttached(view, tabA), false, 'the restarted worker dropped the attachment it found');
      await view.press('share');
      assert.equal((await run.host.call('browser_connect', { tab_id: tabA })).json.error.code, 'BROWSER_SESSION_DETACHED');
      assert.equal(run.host.facts.filter((item) => item.operation === 'type').length, 1, 'the step was asked once and never again');
      assert.equal(await extensionAttached(view, tabA), false);
      await view.close();
      assert.equal(await run.host.stop().then((line) => line.debugger_detach_ack), null, 'nothing was attached at EOF, so no ack is claimed');
      note('transport_loss', { typed_before_loss: partial, of: 600, state_after: { closure: after.closure, last_closure: after.last_closure } });
    });

    // ================= executor 5: Forget pairing while the agent's own tab is still being attached =================
    await t.test('Forget pairing while the agent\'s own tab is still loading: nothing is attached, revoked, the tab stays the owner\'s', async () => {
      for (const earlier of hosts) await earlier.stop();
      const run = await newHost();
      await run.call('browser_state', {}, { invocationId: null });
      const view = await ownerPopup(pageA);
      await until(async () => /Connected/.test(await view.text('summary')), { what: 'extension connection' });
      const before = await tabIds(view);
      const connecting = run.call('browser_connect', { url: `${fixture.origin}/slow` });
      await until(() => fixture.held() === 1, { what: 'the agent tab waiting for its first load' });
      const [opened] = await until(async () => {
        const ids = (await tabIds(view)).filter((id) => !before.includes(id));
        return ids.length ? ids : null;
      }, { what: 'the agent tab' });
      await sleep(200);
      await view.press('forget');
      await until(async () => (await view.text('summary')) === 'Not paired.', { what: 'Forget pairing answered' });
      // Chrome stops the worker after that answer and before the first load ends; the next worker cleans up at start.
      const dying = (await cdp.send('Target.getTargets')).targetInfos
        .find((target) => target.type === 'service_worker' && target.url.endsWith('/service_worker.js'));
      await cdp.send('Target.closeTarget', { targetId: dying.targetId });
      await until(async () => {
        await view.evaluate('chrome.runtime.sendMessage({ type: "status" }).then(() => true, () => false)').catch(() => false);
        return (await cdp.send('Target.getTargets')).targetInfos.some((target) => target.type === 'service_worker'
          && target.url.endsWith('/service_worker.js') && target.targetId !== dying.targetId);
      }, { what: 'a restarted worker' });
      await sleep(800);
      assert.ok((await tabIds(view)).includes(opened), 'the restarted worker did not close the tab Forget gave to the owner');
      fixture.release();
      const refused = await connecting;
      assert.equal(refused.isError, true, JSON.stringify(refused.json));
      assert.ok(['BROWSER_SESSION_REVOKED', 'BROWSER_SESSION_DETACHED'].includes(refused.json.error.code), JSON.stringify(refused.json));
      assert.deepEqual(run.factsFor(refused.invocationId), [], 'the host is asked nothing about a lease that never existed');
      const after = (await run.call('browser_state', {}, { invocationId: null })).json;
      assert.deepEqual([after.connected, after.lease, after.closure.revoked], [false, null, true]);
      await sleep(800);
      assert.ok((await tabIds(view)).includes(opened), 'the tab is the owner\'s now: Forget did not close it');
      assert.equal(await extensionAttached(view, opened), false, 'nothing was attached after its load finished');
      // The owner closes that tab and pairs again, as after any Forget.
      await (await pageOf(`${fixture.origin}/slow`)).close();
      await view.click('details > summary');
      await view.evaluate(`document.getElementById('port').value = '${port}'`);
      await view.typeInto('#credential', stored.credential);
      await view.press('save');
      await until(async () => /Connected/.test(await view.text('summary')), { what: 'the extension paired again' });
      await view.close();
      assert.deepEqual(run.violations, []);
      await run.stop();
      note('forget_while_attaching', { code: refused.json.error.code, closure: after.closure, tab_kept: true, kept_across_worker_restart: true });
    });

    // ================= raw bridge: the extension's own fences =================
    await t.test('the extension refuses a stale generation, a closed invocation, queued steps after close or release, and raw CDP names', async () => {
      for (const earlier of hosts) await earlier.stop();
      const { Listener, packageBuild, readPairing } = await import(path.join(install.skillDir, 'lib', 'pairing.mjs'));
      const pinned = readPairing(install.stateDir);
      let socket = null;
      const inbox = [];
      const listener = new Listener({
        port, credential: pinned.credential, origin: pinned.pinned_origin, build: await packageBuild(), executorGeneration: 7,
        onPaired: (peer) => { socket = peer; peer.on('message', (data) => inbox.push(JSON.parse(data.toString('utf8')))); },
      });
      assert.equal(await listener.start(), 'listening');
      let view = null;
      const send = (message) => socket.send(JSON.stringify(message));
      const answer = (id) => until(() => inbox.find((message) => message.id === id), { what: `answer ${id}` });
      const step = (id, invocation, generation, operation, args) => send({ t: 'step', id, lease_id: 'lraw', generation, invocation_id: invocation, step_id: id, operation, args });
      const refusal = async (id) => { const reply = await answer(id); return [reply.outcome, reply.error && reply.error.code]; };
      try {
        view = await ownerPopup(pageA);
        await until(() => socket, { what: 'raw pairing' });
        await view.press('share');
        await pageB.activate();
        send({ t: 'attach', id: 1, lease_id: 'lraw', generation: 5, tab_id: tabA });
        assert.equal((await answer(1)).ok, true);
        const before = Number(await count());
        step(2, 'A', 5, 'click', { selector: '#go' });
        assert.deepEqual(await refusal(2), ['not_dispatched', 'BROWSER_INVOCATION_CLOSED'], 'no invocation is open yet');
        send({ t: 'open', invocation_id: 'A' });
        step(3, 'A', 4, 'click', { selector: '#go' });
        assert.deepEqual(await refusal(3), ['not_dispatched', 'BROWSER_LEASE_CHANGED'], 'a step for an older generation');
        step(4, 'B', 5, 'click', { selector: '#go' });
        assert.deepEqual(await refusal(4), ['not_dispatched', 'BROWSER_INVOCATION_CLOSED'], 'a step of another invocation');
        step(5, 'A', 5, 'Runtime.evaluate', { expression: 'document.title = "owned"' });
        assert.deepEqual(await refusal(5), ['not_dispatched', 'BROWSER_OPERATION_UNSUPPORTED'], 'a CDP method name is not an operation');
        assert.equal(Number(await count()), before);
        step(6, 'A', 5, 'click', { selector: '#go' });
        assert.equal((await answer(6)).outcome, 'dispatched');
        assert.equal(Number(await count()), before + 1);

        // Queued behind a running step when the invocation closes.
        step(7, 'A', 5, 'wait', { selector: '#never', timeout_ms: 20000 });
        step(8, 'A', 5, 'click', { selector: '#go' });
        send({ t: 'close', id: 9, invocation_id: 'A' });
        assert.deepEqual(await refusal(7), ['not_dispatched', 'BROWSER_INVOCATION_CLOSED']);
        assert.deepEqual(await refusal(8), ['not_dispatched', 'BROWSER_INVOCATION_CLOSED']);
        assert.equal((await answer(9)).t, 'closed');
        assert.ok(inbox.findIndex((message) => message.id === 9) > inbox.findIndex((message) => message.id === 8), 'the close ack follows the answers it waited for');
        step(10, 'A', 5, 'click', { selector: '#go' });
        assert.deepEqual(await refusal(10), ['not_dispatched', 'BROWSER_INVOCATION_CLOSED'], 'a late step of the closed invocation, same lease');
        assert.equal(Number(await count()), before + 1);

        // Queued behind a running step when the lease is released.
        send({ t: 'open', invocation_id: 'C' });
        step(11, 'C', 5, 'wait', { selector: '#never', timeout_ms: 20000 });
        step(12, 'C', 5, 'click', { selector: '#go' });
        send({ t: 'release', id: 13, tab_id: tabA, reason: 'handoff' });
        assert.deepEqual(await refusal(11), ['not_dispatched', 'BROWSER_LEASE_CHANGED']);
        assert.deepEqual(await refusal(12), ['not_dispatched', 'BROWSER_LEASE_CHANGED']);
        assert.equal((await answer(13)).detached, true);
        assert.equal(Number(await count()), before + 1);
        assert.equal(await extensionAttached(view, tabA), false);
        step(14, 'C', 5, 'click', { selector: '#go' });
        assert.deepEqual(await refusal(14), ['not_dispatched', 'BROWSER_LEASE_CHANGED'], 'nothing re-attaches for a late step');

        // A tab the extension created, handed to the owner, then Forget pairing: the tab stays.
        send({ t: 'attach', id: 15, lease_id: 'lraw2', generation: 6, url: `${fixture.origin}/second.html?forget` });
        const created = await until(() => inbox.find((message) => message.id === 15), { timeout: 20000, what: 'own tab attached' });
        assert.deepEqual([created.ok, created.created], [true, true], JSON.stringify(created));
        send({ t: 'release', id: 16, tab_id: created.tab_id, reason: 'handoff' });
        assert.deepEqual([(await answer(16)).detached, (await answer(16)).closed_tab], [true, false]);
        await view.press('forget');
        await until(() => socket.readyState !== 1, { what: 'the extension drops the socket on Forget' });
        await sleep(500);
        const ids = await view.evaluate('chrome.tabs.query({}).then((list) => list.map((tab) => String(tab.id)))');
        assert.ok(ids.includes(String(created.tab_id)), 'Forget pairing left the owner-held tab open');
        note('extension_fences', { stale_generation: 'refused', closed_invocation: 'refused', queued_after_close: 'refused', queued_after_release: 'refused', cdp_method_name: 'refused', forget_kept_owner_tab: true });
      } finally {
        if (view) await view.close().catch(() => null);
        await listener.close();
      }
    });

    fs.mkdirSync(evidenceDir, { recursive: true });
    evidence.finished_at = new Date().toISOString();
  } finally {
    for (const host of hosts) evidence.shutdown = await host.stop().catch(() => null) || evidence.shutdown;
    if (browser) {
      const closed = await browser.close();
      evidence.chromium_closed = closed;
      assert.deepEqual(closed.remaining, [], 'no Chromium process of this run is left');
      if (closed.keychain_prompts !== null) assert.equal(closed.keychain_prompts, 0, 'Chromium asked the Keychain');
    }
    await fixture.close();
    assert.deepEqual(processesMatching(root), [], 'no process of this run is left');
    fs.mkdirSync(evidenceDir, { recursive: true });
    fs.writeFileSync(path.join(evidenceDir, 'headless-run.json'), `${JSON.stringify(evidence, null, 2)}\n`);
    fs.rmSync(root, { recursive: true, force: true });
  }
});
