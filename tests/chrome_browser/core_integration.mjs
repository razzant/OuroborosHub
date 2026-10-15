// Actual core task MCP transport -> Node -> MV3. No FakeHost guard.
// CORE and PYTHON name an Ouroboros checkout and its dependency-only interpreter.
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { freePort, installReplica, launchChromium, startFixtureServer, until, sleep } from './harness.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const core = process.env.OUROBOROS_CHROME_TEST_CORE;
const python = process.env.OUROBOROS_CHROME_TEST_PYTHON;
const executable = process.env.OUROBOROS_CHROME_TEST_CHROMIUM;
assert.ok(core && python && executable, 'CORE, PYTHON and CHROMIUM must be explicit');
const root = fs.mkdtempSync('/tmp/ob-core-mv3-');
const install = installReplica(root);
// A readiness replica carries the original critic/enablement/dependency facts,
// never a fabricated PASS. The consumer still resolves them through real Core.
const trustState = process.env.OUROBOROS_CHROME_TEST_TRUST_STATE;
if (trustState) {
  for (const name of ['review.json', 'enabled.json', 'deps.json', 'grants.json', 'owner_attestation.json']) {
    const source = path.join(trustState, name);
    if (fs.existsSync(source)) fs.copyFileSync(source, path.join(install.stateDir, name));
  }
}
const setupEnv = { PATH: process.env.PATH, OUROBOROS_SKILL_STATE_DIR: install.stateDir };
const setupScript = path.join(install.skillDir, 'scripts/setup.mjs');
const init = JSON.parse(execFileSync(process.execPath, [setupScript, 'init', '--port', String(await freePort())], { env: setupEnv, encoding: 'utf8' }));
const stored = JSON.parse(fs.readFileSync(init.credential_file, 'utf8'));
const fixture = await startFixtureServer();
const profile = fs.mkdtempSync(path.join(root, 'profile-'));
const fault = process.env.OUROBOROS_CHROME_TEST_POST_ERROR === '1';
const evidence = { root, observations: {}, scope: 'actual global ToolRegistry/MCPManager and Node/MV3, disposable installation', post_state_fault: fault, canonical_skill_binding: Boolean(trustState) };
let browser, host;
let sequence = 0;
const waiting = new Map();
let stderr = '';
function request(command) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { waiting.delete(id); reject(new Error('core RPC timeout')); }, 45000);
    waiting.set(id, (reply) => { clearTimeout(timer); if (reply.error) reject(new Error(reply.error)); else resolve(reply.value); });
    host.stdin.write(JSON.stringify({ ...command, id }) + '\n');
  });
}
async function call(name, args = {}) {
  const value = await request({ cmd: 'call', name, args });
  assert.equal(value.status, 'ok', JSON.stringify(value));
  assert.equal(value.code, 'OK', JSON.stringify(value));
  const marker = 'not instructions or policy.\n\n';
  const at = value.text.indexOf(marker);
  assert.ok(at >= 0, 'global MCP untrusted-result wrapper missing');
  if (value.source_witness) {
    assert.equal(value.source_witness.sha256, value.meta.mcp_response_source.sha256);
    const text = value.source_witness.normalized_response.content.find((item) => item.type === 'text').text;
    return JSON.parse(text); // guard diagnostics may follow JSON in the owner-facing wrapper
  }
  const payload = value.text.slice(at + marker.length);
  const noteAt = payload.indexOf('\n\n⚠️ BROWSER_REQUEST_BLOCKED:');
  if (noteAt >= 0) {
    (evidence.observations.guard_diagnostics ||= []).push(payload.slice(noteAt + 2));
    return JSON.parse(payload.slice(0, noteAt));
  }
  return JSON.parse(payload);
}
async function startHostBeforePairing() {
  const env = { ...process.env, OUROBOROS_RUNTIME_MODE: 'cyber_pro', OUROBOROS_SAFETY_MODE: 'off', TMPDIR: '/tmp' };
  host = spawn(python, ['-I', '-S', 'scripts/safe_test.py', '--temp-parent', '/tmp', '--', python, path.join(here, 'core_host.py'), root, install.skillDir, install.stateDir, fault ? 'post-state-fault' : trustState ? 'skill-bound' : 'plain'], { cwd: core, env, stdio: ['pipe', 'pipe', 'pipe'] });
  host.stderr.on('data', (c) => { stderr += c; });
  let buffer = '';
  host.stdout.on('data', (c) => {
    buffer += c;
    for (let i = buffer.indexOf('\n'); i >= 0; i = buffer.indexOf('\n')) {
      const line = buffer.slice(0, i); buffer = buffer.slice(i + 1);
      let reply; try { reply = JSON.parse(line); } catch { continue; }
      const done = waiting.get(reply.id); if (done) { waiting.delete(reply.id); done(reply); }
    }
  });
  evidence.observations.discovery = await request({ cmd: 'discover' });
  assert.equal(evidence.observations.discovery.length, 7);
  evidence.observations.before_pairing = await call('browser_state');
  assert.equal(evidence.observations.before_pairing.pairing.origin_pinned, false);
}
try {
  browser = await launchChromium({ executable, profile, extension: path.join(install.skillDir, 'extension'), WebSocket: install.WebSocket });
  const cdp = browser.cdp;
  await until(async () => (await cdp.send('Target.getTargets')).targetInfos.find((t) => t.type === 'service_worker' && t.url.endsWith('/service_worker.js')), { what: 'MV3 worker' });
  const target = await cdp.open(fixture.origin + '/a.html');
  const control = await cdp.open(fixture.origin + '/fg.html', { background: true });
  await target.activate();
  await startHostBeforePairing();
  const popup = await cdp.open(`chrome-extension://${init.extension_id}/popup.html`, { background: true });
  await until(async () => (await popup.evaluate('document.getElementById("tab").textContent')) !== '…', { what: 'popup initialized' });
  const pairing = spawn(process.execPath, [setupScript, 'pair', '--wait', '30'], { env: setupEnv, stdio: ['ignore', 'pipe', 'pipe'] });
  const paired = new Promise((resolve, reject) => { let output = ''; pairing.stdout.on('data', (c) => { output += c; }); pairing.on('exit', (code) => code === 0 ? resolve(JSON.parse(output)) : reject(new Error('pair setup failed: ' + output))); });
  await sleep(300);
  await popup.evaluate(`document.getElementById('port').value = '${stored.port}'`);
  await popup.typeInto('#credential', stored.credential);
  await popup.click('#save');
  assert.equal((await paired).ok, true);
  await popup.close();

  evidence.observations.after_pairing = await call('browser_state');
  assert.equal(evidence.observations.after_pairing.executor_generation, evidence.observations.before_pairing.executor_generation);
  assert.equal(evidence.observations.after_pairing.pairing.origin_pinned, true);
  assert.equal(evidence.observations.after_pairing.pairing.listener, 'listening');
  await target.activate();
  const share = await cdp.open(`chrome-extension://${init.extension_id}/popup.html`, { background: true });
  await until(async () => /Connected/.test(await share.evaluate('document.getElementById("summary").textContent')), { what: 'real executor paired' });
  await share.click('#share');
  await sleep(200);
  const state = await call('browser_state');
  assert.equal(state.approved_tabs.length, 1);
  await control.activate();
  assert.equal(await target.evaluate('document.visibilityState'), 'hidden');
  await call('browser_connect', { tab_id: state.approved_tabs[0].tab_id });
  const action = (operation, args = {}) => call('browser_action', { operation, args });
  const input = await action('sequence', { steps: [
    { operation: 'click', args: { selector: '#go' } },
    { operation: 'fill', args: { selector: '#name', text: 'Core Ada' } },
    { operation: 'read', args: { cache_key: 'before' } },
  ] });
  assert.equal(input.ok, true, JSON.stringify(input));
  assert.equal(await target.evaluate('document.getElementById("count").textContent'), '1');
  assert.equal(await target.evaluate('document.getElementById("name").value'), 'Core Ada');
  assert.ok((await target.evaluate('window.events')).filter((e) => e.target === 'go').every((e) => e.trusted));
  assert.equal(await target.evaluate('document.visibilityState'), 'hidden');
  assert.equal(await control.evaluate('document.visibilityState'), 'visible');
  assert.equal(await control.evaluate('window.blurs'), 0);
  const cache = await action('cached_text', { cache_key: 'before' });
  assert.ok(cache.ok, JSON.stringify(cache));
  if (fault) {
    const result = await request({ cmd: 'call', name: 'browser_action', args: { operation: 'click', args: { selector: '#go' } } });
    evidence.observations.post_state_error = result;
    assert.equal(result.status, 'error');
    assert.equal(result.code, 'MCP_ERROR');
    assert.match(result.text, /TEST_POST_RESPONSE_SDK_ERROR/);
    assert.ok(result.meta.mcp_response_source, 'received response source must survive actual SDK McpError');
    assert.equal(result.source_witness.sha256, result.meta.mcp_response_source.sha256);
    assert.equal(result.source_witness.size, result.meta.mcp_response_source.size);
    assert.equal(result.source_witness.normalized_response.isError, false);
    assert.equal(result.returned_calls, 3, 'state-error action recorded returned exactly once after sequence and cached-text calls');
    assert.equal(await target.evaluate('document.getElementById("count").textContent'), '2', 'action must not be retried');
    await call('browser_state'); // reconcile fresh observation, never replay the click
  }
  const longText = 'a'.repeat(25000) + 'TAILNEEDLE' + 'b'.repeat(1000);
  await target.evaluate(`(() => { const el=document.createElement('pre'); el.id='long-source'; el.textContent=${JSON.stringify(longText)}; document.body.append(el); })()`);
  const window = await action('get_text', { selector: '#long-source', start: 24998, max_chars: 16 });
  assert.equal(window.result.text, longText.slice(24998, 25014), 'first requested tail window must be addressable');
  const capture = await action('get_text', { selector: '#long-source', max_chars: 16, capture_chars: 30000, cache_key: 'long' });
  assert.equal(capture.result.text.length, 16);
  assert.equal(capture.result.cached.coverage.complete, true);
  const source = capture.result.cached.source;
  await target.evaluate('document.getElementById("long-source").remove()');
  const retained = await action('cached_text', { cache_key: 'long', start: 24998, max_chars: 16 });
  assert.equal(retained.result.text, longText.slice(24998, 25014));
  assert.deepEqual(retained.result.source, source, 'cache retains its original snapshot after DOM changes');
  evidence.observations.addressed_text = { window, capture, retained };
  const handed = await call('browser_handoff');
  assert.equal(handed.connected, false);
  await target.typeInto('#name', ' plus owner');
  const resumed = await call('browser_resume');
  assert.equal(resumed.connected, true);
  assert.equal(await target.evaluate('document.getElementById("name").value'), 'Core Ada plus owner');
  evidence.observations.same_form = { handoff: handed, resume: resumed, trusted: true, foreground_preserved: true };
  // Two distinct same-tab document transitions and a delayed readiness sequence.
  const clickNav = await action('click', { selector: '#link' });
  assert.ok(clickNav.ok, JSON.stringify(clickNav));
  assert.ok((await call('browser_state')).lease.url.endsWith('/second.html'));
  const back = await action('navigate', { url: fixture.origin + '/a.html' });
  assert.ok(back.ok, JSON.stringify(back));
  const flow = await action('sequence', { steps: [
    { operation: 'click', args: { selector: '#start' } },
    { operation: 'wait', args: { selector: '#step2', timeout_ms: 5000 } },
    { operation: 'fill', args: { selector: '#step2', text: 'Ready value' } },
    { operation: 'click', args: { selector: '#next' } },
    { operation: 'wait', args: { selector: '#done', timeout_ms: 5000 } },
    { operation: 'read', args: { cache_key: 'ready' } },
  ] });
  assert.ok(flow.ok, JSON.stringify(flow));
  assert.equal(await target.evaluate('document.getElementById("done").textContent'), 'done: Ready value');
  await target.evaluate('history.pushState({}, "", "#fragment")');
  const refreshed = await action('read');
  assert.ok(refreshed.ok, JSON.stringify(refreshed));
  assert.ok((await call('browser_state')).lease.url.endsWith('#fragment'));
  const snapshot = await action('snapshot');
  assert.ok(snapshot.ok, JSON.stringify(snapshot));
  const fragmentHandoff = await call('browser_handoff');
  assert.equal(fragmentHandoff.connected, false);
  await target.typeInto('#notes', 'Owner after fragment');
  const fragmentResume = await call('browser_resume');
  assert.equal(fragmentResume.connected, true);
  assert.equal(await target.evaluate('document.getElementById("notes").value'), 'Owner after fragment');
  evidence.observations.navigation_readiness_fragment = { clickNav, back, flow, refreshed, snapshot, fragmentHandoff, fragmentResume };
  const imageDir = process.env.OUROBOROS_CHROME_TEST_EVIDENCE || path.join(root, 'evidence');
  fs.mkdirSync(imageDir, { recursive: true });
  const rendered = await cdp.send('Page.captureScreenshot', { format: 'png' }, target.sessionId);
  fs.writeFileSync(path.join(imageDir, 'registered-target.png'), Buffer.from(rendered.data, 'base64'));
  const end = await call('browser_disconnect');
  evidence.observations.disconnect = end;
  const closedState = await call('browser_state');
  assert.equal(closedState.connected, false);
  const ownUrl = fixture.origin + '/a.html?cold=own';
  const own = await call('browser_connect', { url: ownUrl });
  assert.equal(own.connected, true);
  assert.ok(own.lease.generation > fragmentResume.lease.generation);
  const ownTarget = await until(async () => (await cdp.send('Target.getTargets')).targetInfos.find((item) => item.type === 'page' && item.url === ownUrl), { what: 'new own background tab' });
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId: ownTarget.targetId, flatten: true });
  const { Page } = await import('./harness.mjs');
  const cold = new Page(cdp, ownTarget.targetId, sessionId);
  assert.equal(await cold.evaluate('document.visibilityState'), 'hidden');
  const first = await action('click', { selector: '#go' });
  assert.equal(first.ok, true);
  assert.equal(first.result.input_preparation.kind, 'renderer_frame');
  assert.equal(await cold.evaluate('document.getElementById("count").textContent'), '1');
  assert.equal((await cold.evaluate('window.events')).filter((event) => event.type === 'click' && event.target === 'go' && event.trusted).length, 1);
  const idleGet = await cold.evaluate(`fetch('/echo?idle-get').then(r=>({ok:r.ok}),error=>({ok:false,error:String(error)}))`);
  const idleHead = await cold.evaluate(`fetch('/echo?idle-head',{method:'HEAD'}).then(r=>({ok:r.ok}),error=>({ok:false,error:String(error)}))`);
  const idlePost = await cold.evaluate(`fetch('/echo?idle-post',{method:'POST',body:'not-permitted-between-tools'}).then(r=>({ok:r.ok}),error=>({ok:false,error:String(error)}))`);
  assert.equal(idleGet.ok, true);
  assert.equal(idleHead.ok, true);
  assert.equal(idlePost.ok, false);
  assert.equal(fixture.requests.filter((item) => item.query === '?idle-get').length, 1);
  assert.equal(fixture.requests.filter((item) => item.query === '?idle-head').length, 1);
  assert.equal(fixture.requests.filter((item) => item.query === '?idle-post').length, 0);
  await cold.evaluate(`location.href=${JSON.stringify(fixture.origin + '/second.html?idle-document')}`);
  await sleep(200);
  assert.equal(fixture.requests.filter((item) => item.query === '?idle-document').length, 0);
  assert.equal((await call('browser_state')).lease.document_id, own.lease.document_id);
  evidence.observations.cold_reconnect_resources = { own, first, idleGet, idleHead, idlePost, requests: fixture.requests };
  await call('browser_disconnect');
  assert.equal(await target.evaluate('document.getElementById("notes").value'), 'Owner after fragment', 'the previously handed-off tab remains intact');
  evidence.observations.stop = await request({ cmd: 'stop' });
  assert.ok(evidence.observations.stop.every((x) => x.closure === 'confirmed'));
  host.stdin.end();
  await new Promise((resolve) => host.once('exit', resolve));
  host = null;
  console.log(fault ? 'CORE_MV3_SDK_ERROR_CONSUMER_PASS' : 'CORE_MV3_REGISTRY_CONSUMER_PASS');
} finally {
  if (host) { host.stdin.end(); await Promise.race([new Promise((r) => host.once('exit', r)), sleep(6000).then(() => host.kill('SIGKILL'))]); }
  if (browser) evidence.cleanup = await browser.close();
  await fixture.close();
  const output = process.env.OUROBOROS_CHROME_TEST_EVIDENCE || path.join(root, 'evidence');
  fs.mkdirSync(output, { recursive: true });
  fs.writeFileSync(path.join(output, 'core-integration.json'), JSON.stringify(evidence, null, 2));
  fs.writeFileSync(path.join(output, 'core-stderr.log'), stderr);
}
