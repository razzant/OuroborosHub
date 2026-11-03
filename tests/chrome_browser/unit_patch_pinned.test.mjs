// Patch exactness, pin verification, and the patched bundle's connection entry points.
// RED controls run the same checks against the stock bundle text where that is safe.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import crypto from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { SKILL, NODE_MODULES, NODE, INSTRUMENT, scratchDir, fakeHome, requirePinnedInstall } from './helpers/env.mjs';
import { applyUnifiedDiff, parseUnifiedDiff, PatchError } from '../../skills/chrome_browser/lib/patch.mjs';
import { verifyDependencies, describeProblems, PINS } from '../../skills/chrome_browser/lib/pinned.mjs';

requirePinnedInstall();
const sha256 = (data) => crypto.createHash('sha256').update(data).digest('hex');
const CORE = path.join(NODE_MODULES, 'playwright-core', 'lib', 'coreBundle.js');
const PATCH = fs.readFileSync(path.join(SKILL, PINS.patch.file), 'utf8');
const stock = fs.readFileSync(CORE, 'utf8');

test('the pinned patch turns the pinned preimage into the pinned postimage', () => {
  assert.equal(sha256(stock), PINS.patch.preimage_sha256);
  const patched = applyUnifiedDiff(stock, PATCH);
  assert.equal(sha256(Buffer.from(patched, 'utf8')), PINS.patch.postimage_sha256);
  assert.equal(parseUnifiedDiff(PATCH).hunks.length, 14);
});

test('one changed byte inside a hunk is refused', () => {
  const tampered = stock.replace('await tab2.page.bringToFront();\n        await tab2.updateWebMCPTools();', 'await tab2.page.bringToFront();\n        await tab2.updateWebMCPTools() ;');
  assert.notEqual(tampered, stock);
  assert.throws(() => applyUnifiedDiff(tampered, PATCH), (error) => error instanceof PatchError && error.code === 'PATCH_PREIMAGE_MISMATCH');
});

test('a shifted file is refused (no offset search, no fuzz)', () => {
  const shifted = '// one extra line\n' + stock;
  assert.throws(() => applyUnifiedDiff(shifted, PATCH), (error) => error.code === 'PATCH_PREIMAGE_MISMATCH');
});

test('malformed patches are refused', () => {
  assert.throws(() => parseUnifiedDiff('no headers here'), (error) => error.code === 'PATCH_MALFORMED');
  assert.throws(() => parseUnifiedDiff('--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\n'), (error) => error.code === 'PATCH_MALFORMED');
  assert.throws(() => parseUnifiedDiff('--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n--- a/y\n+++ b/y\n@@ -1 +1 @@\n-a\n+b\n'), (error) => error.code === 'PATCH_MALFORMED');
});

test('stock source (RED) vs patched source (GREEN): the three chosen behaviours at their call sites', () => {
  const patched = applyUnifiedDiff(stock, PATCH);
  // background tab creation
  assert.ok(stock.includes('this._sendToExtension("chrome.tabs.create", [{ url: url3 }])'), 'RED: stock creates foreground tabs');
  assert.ok(patched.includes('this._sendToExtension("chrome.tabs.create", [{ url: url3, active: false }])'));
  // selecting does not bring to front
  const selectStock = stock.slice(stock.indexOf('      async selectTab(index) {'), stock.indexOf('      async ensureTab() {'));
  const selectPatched = patched.slice(patched.indexOf('      async selectTab(index) {'), patched.indexOf('      async ensureTab() {'));
  assert.match(selectStock, /bringToFront/, 'RED: stock select brings the tab to front');
  assert.doesNotMatch(selectPatched, /bringToFront/);
  // no implicit tab
  const ensureStock = stock.slice(stock.indexOf('      async ensureTab() {'), stock.indexOf('      async closeTab(index) {'));
  const ensurePatched = patched.slice(patched.indexOf('      async ensureTab() {'), patched.indexOf('      async closeTab(index) {'));
  assert.match(ensureStock, /await this\.newTab\(\)/, 'RED: stock replaces a missing tab with about:blank');
  assert.doesNotMatch(ensurePatched, /newTab\(|page\.close\(/);
  // no spawn and no profile discovery in the relay and the extension factory
  const relayStock = stock.slice(stock.indexOf('    CDPRelayServer = class {'), stock.indexOf('    ExtensionConnection = class {'));
  const relayPatched = patched.slice(patched.indexOf('    CDPRelayServer = class {'), patched.indexOf('    ExtensionConnection = class {'));
  assert.match(relayStock, /import_child_process5\.spawn\)\(executablePath, args/, 'RED: stock relay spawns a browser');
  assert.match(relayStock, /registry\.findExecutable/, 'RED: stock relay looks for a browser');
  assert.doesNotMatch(relayPatched, /child_process\d*\.spawn|findExecutable|PLAYWRIGHT_MCP_EXTENSION_TOKEN|PWTEST_EXTENSION_PROTOCOL/);
  const factoryStock = stock.slice(stock.indexOf('async function createExtensionBrowser('), stock.indexOf('var import_path59, debug16, debugLogger3;'));
  const factoryPatched = patched.slice(patched.indexOf('async function createExtensionBrowser('), patched.indexOf('var import_path59, debug16, debugLogger3;'));
  assert.match(factoryStock, /findPlaywrightExtensionProfile|defaultUserDataDirForChannel/, 'RED: stock discovers profiles');
  assert.doesNotMatch(factoryPatched, /findPlaywrightExtensionProfile|defaultUserDataDirForChannel|connectOverCDP/);
  // preflights reach the server instead of a permissive 204
  assert.match(stock, /"Fetch\.fulfillRequest", \{\n\s+requestId: requestPausedEvent\.requestId,\n\s+responseCode: 204/, 'RED: stock answers CORS preflights itself');
  assert.doesNotMatch(patched, /responseCode: 204,\n\s+responsePhrase: statusText\(204\)/);
});

test('pins: the scratch install matches every pinned file', () => {
  const verified = verifyDependencies(NODE_MODULES);
  assert.equal(verified.ok, true, describeProblems(verified.problems));
});

test('pins: missing and different installs are named precisely', () => {
  const empty = scratchDir('nm-empty');
  const missing = verifyDependencies(empty);
  assert.equal(missing.ok, false);
  assert.ok(missing.problems.every((p) => p.code === 'missing'));
  const tampered = scratchDir('nm-tampered');
  fs.cpSync(path.join(NODE_MODULES, 'playwright-core'), path.join(tampered, 'playwright-core'), { recursive: true });
  fs.cpSync(path.join(NODE_MODULES, '@playwright'), path.join(tampered, '@playwright'), { recursive: true });
  fs.appendFileSync(path.join(tampered, 'playwright-core', 'lib', 'coreBundle.js'), '\n// changed\n');
  const different = verifyDependencies(tampered);
  assert.equal(different.ok, false);
  assert.deepEqual(different.problems.map((p) => [p.code, p.file]), [['hash_mismatch', 'playwright-core/lib/coreBundle.js']]);
  fs.rmSync(empty, { recursive: true, force: true });
  fs.rmSync(tampered, { recursive: true, force: true });
});

// Runs a probe script in a child with HOME and TMPDIR in scratch and the instrument preload.
function probe(name, source, env = {}) {
  const dir = scratchDir(name);
  const script = path.join(dir, 'probe.mjs');
  fs.writeFileSync(script, source);
  const home = path.join(dir, 'home');
  const tmp = path.join(dir, 'tmp');
  fs.mkdirSync(home);
  fs.mkdirSync(tmp);
  const result = spawnSync(NODE, ['--require', INSTRUMENT, script], {
    env: { PATH: '/usr/bin:/bin', HOME: home, USERPROFILE: home, TMPDIR: tmp, TEMP: tmp, TMP: tmp,
      QA_INSTRUMENT_LOG: path.join(dir, 'instrument.jsonl'), OUROBOROS_CHROME_NODE_MODULES: NODE_MODULES, ...env },
    encoding: 'utf8',
    timeout: 60_000,
  });
  const log = fs.existsSync(path.join(dir, 'instrument.jsonl')) ? fs.readFileSync(path.join(dir, 'instrument.jsonl'), 'utf8').split('\n').filter(Boolean).map((line) => JSON.parse(line)) : [];
  return { dir, result, log, out: result.stdout ? JSON.parse(result.stdout.trim().split('\n').pop() || '{}') : {} };
}

test('stock extension factory reads the profile it finds (RED, synthetic HOME); patched factory reads nothing (GREEN)', () => {
  const { home, chrome } = fakeHome('home-discovery');
  const source = `
    import os from 'node:os';
    if (os.homedir() !== ${JSON.stringify(home)}) { console.log(JSON.stringify({ aborted: 'HOME is not the synthetic one' })); process.exit(3); }
    const mode = process.argv[2];
    const results = {};
    if (mode !== 'patched') {
      const stock = (await import('node:module')).createRequire(import.meta.url)(${JSON.stringify(CORE)});
      try { await stock.tools.createBrowserWithInfo({ extension: true, browser: { launchOptions: {}, contextOptions: {} }, timeouts: {} }, { clientName: 'qa', cwd: ${JSON.stringify(home)} }, {}, { title: 'qa' }); results.stock = 'resolved'; }
      catch (error) { results.stock = String(error.message).split('\\n')[0]; }
    } else {
      const { loadPinnedPlaywright } = await import(${JSON.stringify(pathToFileURL(path.join(SKILL, 'lib', 'pinned.mjs')).href)});
      const { bundle } = loadPinnedPlaywright();
      try { await bundle.tools.createBrowserWithInfo({ extension: true, browser: { launchOptions: {}, contextOptions: {} }, timeouts: {} }, { clientName: 'qa', cwd: ${JSON.stringify(home)} }, {}, { title: 'qa' }); results.patched = 'resolved'; }
      catch (error) { results.patched = String(error.message).split('\\n')[0]; }
    }
    console.log(JSON.stringify(results));
  `;
  for (const mode of ['stock', 'patched']) {
    const run = probe(`discovery-${mode}`, source.replace("const mode = process.argv[2];", `const mode = ${JSON.stringify(mode)};`), { HOME: home, TMPDIR: path.join(home, 'tmp') });
    fs.writeFileSync(path.join(run.dir, 'result.json'), JSON.stringify({ mode, status: run.result.status, out: run.out, stderr: run.result.stderr }, null, 2));
    const profileTouches = run.log.filter((entry) => entry.kind === 'fs' && entry.path.startsWith(chrome));
    const spawns = run.log.filter((entry) => entry.kind === 'child_process');
    if (mode === 'stock') {
      assert.match(run.out.stock, /Playwright Extension not found/);
      assert.ok(profileTouches.some((entry) => entry.path.endsWith('Local State')), 'RED: stock reads Local State');
      assert.ok(profileTouches.some((entry) => entry.path.endsWith('Preferences')), 'RED: stock reads Preferences');
    } else {
      assert.match(run.out.patched, /disabled in this server/);
      assert.deepEqual(profileTouches, []);
    }
    assert.deepEqual(spawns, []);
  }
});

test('stock relay spawns a browser with the connect URL (RED, harmless /usr/bin/true); patched relay returns the URL and spawns nothing (GREEN)', () => {
  const source = `
    const mode = process.argv[2];
    const out = {};
    let tools;
    if (mode === 'stock') tools = (await import('node:module')).createRequire(import.meta.url)(${JSON.stringify(CORE)}).tools;
    else tools = (await import(${JSON.stringify(pathToFileURL(path.join(SKILL, 'lib', 'pinned.mjs')).href)})).loadPinnedPlaywright().bundle.tools;
    const Relay = tools.CDPRelayServer;
    if (!Relay) { out.exported = false; console.log(JSON.stringify(out)); process.exit(0); }
    const relay = new Relay('chrome', '/usr/bin/true', undefined, undefined);
    await relay.start();
    out.url = relay.connectPageUrl ? relay.connectPageUrl('Ouroboros') : null;
    try { await Promise.race([relay.establishExtensionConnection('Ouroboros'), new Promise((r) => setTimeout(() => r('waiting'), 300))]); out.establish = 'waiting'; }
    catch (error) { out.establish = String(error.message); }
    relay.stop();
    console.log(JSON.stringify(out));
  `;
  const stockRun = probe('relay-stock', source.replace("const mode = process.argv[2];", "const mode = 'stock';"));
  // The stock bundle does not export the relay class: reach it the way upstream does, through the
  // extension factory with an explicit executable (no discovery when executablePath is given).
  const stockFactory = probe('relay-stock-factory', `
    const tools = (await import('node:module')).createRequire(import.meta.url)(${JSON.stringify(CORE)}).tools;
    process.env.PWTEST_EXTENSION_CONNECT_TIMEOUT = '300';
    const out = {};
    try { await Promise.race([tools.createBrowserWithInfo({ extension: true, browser: { launchOptions: {}, contextOptions: {} }, timeouts: {} }, { clientName: 'qa', cwd: '/' }, { executablePath: '/usr/bin/true' }, { title: 'qa' }), new Promise((r) => setTimeout(r, 800))]); out.stock = 'waited'; }
    catch (error) { out.stock = String(error.message).split('\\n')[0]; }
    console.log(JSON.stringify(out));
    process.exit(0);
  `);
  const patchedRun = probe('relay-patched', source.replace("const mode = process.argv[2];", "const mode = 'patched';"));
  for (const run of [stockRun, stockFactory, patchedRun])
    fs.writeFileSync(path.join(run.dir, 'result.json'), JSON.stringify({ status: run.result.status, out: run.out, stderr: run.result.stderr, log: run.log.filter((e) => e.kind !== 'fs') }, null, 2));
  assert.equal(stockRun.out.exported, false, 'stock bundle exports no relay class');
  const stockSpawn = stockFactory.log.filter((entry) => entry.kind === 'child_process');
  assert.equal(stockSpawn.length, 1, 'RED: stock spawns once');
  assert.equal(stockSpawn[0].file, '/usr/bin/true');
  assert.match(stockSpawn[0].args.at(-1), /^chrome-extension:\/\/mmlmfjhmonkocbjadbfplnigmagldckm\/connect\.html\?mcpRelayUrl=/);
  const url = new URL(patchedRun.out.url);
  assert.equal(url.protocol, 'chrome-extension:');
  assert.equal(url.host, PINS.extension.id);
  assert.equal(url.pathname, '/connect.html');
  const relayUrl = new URL(url.searchParams.get('mcpRelayUrl'));
  // Upstream listens on "localhost"; the connect page admits exactly 127.0.0.1 and [::1].
  assert.ok(['127.0.0.1', '[::1]'].includes(relayUrl.hostname), relayUrl.hostname);
  assert.match(relayUrl.pathname, /^\/extension\/[0-9a-f-]{36}$/);
  assert.equal(url.searchParams.get('client'), '{"name":"Ouroboros"}');
  assert.equal(url.searchParams.get('protocolVersion'), '2');
  assert.equal(url.searchParams.get('token'), null);
  assert.match(patchedRun.out.establish, /never starts, opens or looks for a browser/);
  assert.deepEqual(patchedRun.log.filter((entry) => entry.kind === 'child_process'), []);
  const listens = patchedRun.log.filter((entry) => entry.kind === 'listen');
  assert.equal(listens.length, 1);
  assert.ok(['127.0.0.1', '::1'].includes(listens[0].address.address), listens[0].address.address);
});
