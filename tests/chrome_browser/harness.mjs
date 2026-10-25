// Test harness for skills/chrome_browser: a disposable full Chromium with the
// real extension, a stand-in host (guard socket + MCP client), and a fixture
// web server. Nothing here touches a personal browser profile or the Keychain.

import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import { createRequire } from 'node:module';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const REPO = path.resolve(HERE, '..', '..');
// OUROBOROS_CHROME_TEST_SKILL runs the same tests against another copy of the package (a baseline, to see them fail).
export const SKILL = process.env.OUROBOROS_CHROME_TEST_SKILL || path.join(REPO, 'skills', 'chrome_browser');
export const FIXTURES = path.join(HERE, 'fixtures');
export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export async function until(read, { timeout = 8000, step = 50, what = 'condition' } = {}) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const value = await read();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await sleep(step);
  }
}

// ---------------------------------------------------------------- install replica

/**
 * A copy of the skill laid out as an installation: data/skills/external/<name>
 * with ws under .ouroboros_env/node (what the host's installer does), and the
 * skill state directory beside it.
 */
export function installReplica(root) {
  const skillDir = path.join(root, 'data', 'skills', 'external', 'chrome_browser');
  const stateDir = path.join(root, 'data', 'state', 'skills', 'chrome_browser');
  fs.mkdirSync(path.dirname(skillDir), { recursive: true });
  fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  fs.cpSync(SKILL, skillDir, { recursive: true });
  const nodeRoot = path.join(skillDir, '.ouroboros_env', 'node');
  fs.mkdirSync(nodeRoot, { recursive: true });
  const prebuilt = process.env.OUROBOROS_CHROME_TEST_NODE_ENV;
  if (prebuilt) {
    fs.cpSync(path.join(prebuilt, 'node_modules'), path.join(nodeRoot, 'node_modules'), { recursive: true });
    // Carry the installer witness with its dependency bytes; readiness still
    // requires the independently copied runtime-side deps.json to corroborate it.
    const fingerprint = path.join(path.dirname(prebuilt), 'fingerprint.json');
    if (fs.existsSync(fingerprint))
      fs.copyFileSync(fingerprint, path.join(path.dirname(nodeRoot), 'fingerprint.json'));
  } else {
    execFileSync('npm', ['install', '--ignore-scripts', '--no-audit', '--no-fund', '--prefix', nodeRoot, 'ws@8.22.0'],
      { stdio: 'ignore', cwd: nodeRoot });
  }
  const { WebSocket } = createRequire(path.join(nodeRoot, 'package.json'))('ws');
  return { skillDir, stateDir, WebSocket };
}

// ---------------------------------------------------------------- Chromium

const REQUIRED_FLAGS = ['--headless=new', '--use-mock-keychain', '--password-store=basic'];

/** The only launch recipe. Without --use-mock-keychain Chromium asks the macOS login Keychain. */
export function chromiumArgs({ profile, extension }) {
  return [
    '--headless=new', '--use-mock-keychain', '--password-store=basic',
    `--user-data-dir=${profile}`, '--remote-debugging-port=0',
    '--no-first-run', '--no-default-browser-check', '--no-service-autorun',
    '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-default-apps',
    '--disable-breakpad', '--metrics-recording-only',
    '--disable-features=MediaRouter,DialMediaRouteProvider,OptimizationHints,Translate',
    `--disable-extensions-except=${extension}`, `--load-extension=${extension}`,
    '--window-size=1100,800', 'about:blank',
  ];
}

export function assertSafeRecipe(args, profile) {
  for (const flag of REQUIRED_FLAGS)
    if (!args.includes(flag)) throw new Error(`refusing to launch Chromium without ${flag}`);
  const dataDir = args.find((arg) => arg.startsWith('--user-data-dir='));
  if (!dataDir || dataDir.slice('--user-data-dir='.length) !== profile || fs.readdirSync(profile).length)
    throw new Error('refusing to launch Chromium without a fresh, empty profile directory');
  if (args.some((arg) => /^--(headless=old|headless$|no-startup-window)/.test(arg)))
    throw new Error('refusing a launch that is not --headless=new');
}

export function processesMatching(needle) {
  try {
    return execFileSync('pgrep', ['-f', '--', needle], { encoding: 'utf8' }).split('\n').filter(Boolean).map(Number);
  } catch (error) {
    return [];  // pgrep exits 1 when nothing matches
  }
}

/** macOS: Keychain prompts securityd showed for this pid. null where it cannot be read. */
export function keychainPrompts(pid, sinceSeconds) {
  if (process.platform !== 'darwin') return null;
  try {
    const out = execFileSync('/usr/bin/log', ['show', '--last', `${Math.ceil(sinceSeconds) + 5}s`, '--style', 'compact', '--predicate',
      'process == "securityd" AND eventMessage CONTAINS "displaying keychain prompt"'], { encoding: 'utf8', timeout: 60000 });
    return out.split('\n').filter((line) => line.includes(`(${pid})`)).length;
  } catch (error) {
    return null;
  }
}

export class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.sequence = 0;
    this.waiting = new Map();
    socket.on('message', (data) => {
      const message = JSON.parse(data.toString('utf8'));
      const waiter = message.id && this.waiting.get(message.id);
      if (!waiter) return;
      this.waiting.delete(message.id);
      if (message.error) waiter.reject(new Error(`${waiter.method}: ${message.error.message}`));
      else waiter.resolve(message.result);
    });
  }

  send(method, params = {}, sessionId) {
    return new Promise((resolve, reject) => {
      const id = ++this.sequence;
      this.waiting.set(id, { resolve, reject, method });
      this.socket.send(JSON.stringify({ id, method, params, sessionId }));
    });
  }

  /** Opens a tab and attaches the harness to it. `background` keeps the current tab active. */
  async open(url, { background = false } = {}) {
    const { targetId } = await this.send('Target.createTarget', { url, background });
    const { sessionId } = await this.send('Target.attachToTarget', { targetId, flatten: true });
    const page = new Page(this, targetId, sessionId);
    await until(async () => (await page.evaluate('location.href !== "about:blank" && document.readyState')) === 'complete',
      { what: `load of ${url}` });
    return page;
  }
}

export class Page {
  constructor(cdp, targetId, sessionId) { Object.assign(this, { cdp, targetId, sessionId }); }

  async evaluate(expression) {
    const answer = await this.cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, this.sessionId);
    if (answer.exceptionDetails) throw new Error(`page evaluate failed: ${answer.exceptionDetails.exception?.description || answer.exceptionDetails.text}`);
    return answer.result.value;
  }

  /** A trusted click by the harness standing in for the owner's hand. */
  async click(selector) {
    const point = await this.evaluate(`(() => { const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()`);
    for (const type of ['mousePressed', 'mouseReleased'])
      await this.cdp.send('Input.dispatchMouseEvent', { type, ...point, button: 'left', clickCount: 1 }, this.sessionId);
  }

  async typeInto(selector, text) {
    await this.click(selector);
    await this.cdp.send('Input.insertText', { text }, this.sessionId);
  }

  activate() { return this.cdp.send('Target.activateTarget', { targetId: this.targetId }); }
  close() { return this.cdp.send('Target.closeTarget', { targetId: this.targetId }); }
}

/** `env` replaces Chromium's environment (for example a disposable HOME); by default it inherits this process's. */
export async function launchChromium({ executable, profile, extension, WebSocket, env = process.env }) {
  const args = chromiumArgs({ profile, extension });
  assertSafeRecipe(args, profile);
  const started = Date.now();
  const child = spawn(executable, args, { detached: true, stdio: ['ignore', 'ignore', 'pipe'], env });
  let stderr = '';
  child.stderr.on('data', (chunk) => { stderr = (stderr + chunk).slice(-4000); });
  const exited = new Promise((resolve) => child.once('exit', resolve));
  const killGroup = (signal) => { try { process.kill(-child.pid, signal); } catch (error) { /* gone */ } };
  const onExit = () => killGroup('SIGKILL');
  const onSignal = () => { killGroup('SIGKILL'); process.exit(1); };
  process.once('exit', onExit);
  process.once('SIGTERM', onSignal);
  process.once('SIGINT', onSignal);

  const close = async () => {
    killGroup('SIGTERM');
    const graceful = await Promise.race([exited.then(() => true), sleep(4000).then(() => false)]);
    if (!graceful) killGroup('SIGKILL');
    await Promise.race([exited, sleep(3000)]);
    let remaining = processesMatching(`--user-data-dir=${profile}`);
    for (let attempt = 0; remaining.length && attempt < 20; attempt += 1) {
      for (const pid of remaining) { try { process.kill(pid, 'SIGKILL'); } catch (error) { /* gone */ } }
      await sleep(150);
      remaining = processesMatching(`--user-data-dir=${profile}`);
    }
    process.removeListener('exit', onExit);
    process.removeListener('SIGTERM', onSignal);
    process.removeListener('SIGINT', onSignal);
    return {
      pid: child.pid, graceful, remaining,
      keychain_prompts: keychainPrompts(child.pid, (Date.now() - started) / 1000),
    };
  };

  try {
    const portFile = path.join(profile, 'DevToolsActivePort');
    const [port, browserPath] = await until(() => {
      if (child.exitCode !== null) throw new Error(`Chromium exited early: ${stderr}`);
      try { return fs.readFileSync(portFile, 'utf8').trim().split('\n'); } catch (error) { return null; }
    }, { timeout: 20000, what: 'DevToolsActivePort' });
    const socket = new WebSocket(`ws://127.0.0.1:${port}${browserPath}`, { perMessageDeflate: false });
    await new Promise((resolve, reject) => { socket.once('open', resolve); socket.once('error', reject); });
    const cdp = new Cdp(socket);
    const version = await cdp.send('Browser.getVersion');
    return { cdp, pid: child.pid, args, version, close: async () => { socket.close(); return close(); } };
  } catch (error) {
    await close();
    throw error;
  }
}

// ---------------------------------------------------------------- stand-in host

/** Guard socket plus MCP client: the two things the Ouroboros host is to the executor. */
export class FakeHost {
  constructor({ socketDir, skillDir, env = {}, owner = { task_id: 'task-test', attempt: '1' } }) {
    Object.assign(this, { skillDir, owner, extraEnv: env });
    this.socketPath = path.join(socketDir, `g${Math.random().toString(36).slice(2, 8)}.sock`);
    this.facts = [];
    this.violations = [];   // wire rules the real host enforces; must stay empty
    this.lastStep = new Map();
    this.active = new Set(); // invocation ids of tools/call requests in flight
    this.policy = (facts) => this.defaultPolicy(facts);
    this.sequence = 0;
    this.invocations = 0;
    this.waiting = new Map();
    this.stderr = '';
    this.exited = null;
  }

  async start() {
    this.guard = net.createServer({ allowHalfOpen: true }, (socket) => {
      const chunks = [];
      socket.on('data', (chunk) => chunks.push(chunk));
      socket.on('end', async () => {
        let block = 'BROWSER_POLICY_UNAVAILABLE: unreadable facts';
        try {
          const facts = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          this.facts.push(facts);
          block = this.wireCheck(facts) || await this.policy(facts);
          facts.verdict = block;
        } catch (error) { /* refused */ }
        socket.end(`${JSON.stringify({ block })}\n`);
      });
      socket.on('error', () => {});
    });
    await new Promise((resolve) => this.guard.listen(this.socketPath, resolve));
    this.child = spawn(process.execPath, [path.join(this.skillDir, 'scripts', 'mcp_server.mjs')], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: {
        PATH: process.env.PATH, OUROBOROS_BROWSER_PROTOCOL: 'ouroboros_chrome', OUROBOROS_BROWSER_GUARD: this.socketPath,
        OUROBOROS_BROWSER_OWNER_TASK_ID: this.owner.task_id, OUROBOROS_BROWSER_OWNER_ATTEMPT: this.owner.attempt, ...this.extraEnv,
      },
    });
    this.exited = new Promise((resolve) => this.child.once('exit', (code) => resolve(code)));
    this.child.stderr.on('data', (chunk) => { this.stderr += chunk; });
    let buffer = '';
    this.child.stdout.on('data', (chunk) => {
      buffer += chunk;
      for (let index = buffer.indexOf('\n'); index >= 0; index = buffer.indexOf('\n')) {
        const line = buffer.slice(0, index);
        buffer = buffer.slice(index + 1);
        if (!line.trim()) continue;
        const message = JSON.parse(line);
        const waiter = this.waiting.get(message.id);
        if (waiter) { this.waiting.delete(message.id); waiter(message); }
      }
    });
    const init = await this.request('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'Ouroboros', version: 'test' } });
    this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' })}\n`);
    return init.result;
  }

  /** The fixture's stand-in for host policy: no call in flight refuses, and so does any URL under /blocked. */
  defaultPolicy(facts) {
    if (facts.invocation_id === null || !this.active.has(facts.invocation_id)) return 'BROWSER_POLICY_UNAVAILABLE: no active Chrome invocation';
    if (facts.kind === 'request' && /\/blocked\b/.test(String(facts.url))) return 'BROWSER_REQUEST_BLOCKED: fixture policy';
    return '';
  }

  /** What the host's protocol module checks on every question, whatever the policy says. */
  wireCheck(facts) {
    const problems = [];
    const state = facts.state;
    const keys = ['protocol', 'version', 'owner', 'executor_generation', 'connected', 'lease', 'closure'];
    if (!state || JSON.stringify(Object.keys(state)) !== JSON.stringify(keys)) problems.push('state is not the seven contract members');
    else {
      if (state.protocol !== 'ouroboros_chrome' || state.version !== 1) problems.push('protocol/version');
      if (state.owner.task_id !== this.owner.task_id || String(state.owner.attempt) !== this.owner.attempt) problems.push('owner');
      if (!Number.isInteger(state.executor_generation) || state.executor_generation < 0) problems.push('executor_generation');
      if (state.connected !== (state.lease !== null)) problems.push('connected without lease');
      if (state.lease && (!Number.isInteger(state.lease.generation) || state.lease.generation < 0 || state.lease.guard_armed !== true
          || !['lease_id', 'tab_id', 'document_id', 'url'].every((key) => typeof state.lease[key] === 'string' && state.lease[key])))
        problems.push('lease fields');
    }
    if (!['primitive', 'request', 'document'].includes(facts.kind)) problems.push(`kind ${facts.kind}`);
    if (facts.invocation_id !== null) {
      const last = this.lastStep.get(facts.invocation_id) || 0;
      if (!Number.isInteger(facts.step_id) || facts.step_id <= last) problems.push(`step_id ${facts.step_id} after ${last}`);
      else this.lastStep.set(facts.invocation_id, facts.step_id);
    } else if (facts.step_id !== null) problems.push('step_id without invocation');
    if (facts.kind === 'request' && (typeof facts.request_id !== 'string' || typeof facts.url !== 'string' || typeof facts.method !== 'string'
        || typeof facts.complete !== 'boolean' || typeof facts.resource_type !== 'string' || typeof facts.is_main_frame !== 'boolean'
        || typeof facts.target_tab_id !== 'string' || !('post_data' in facts))) problems.push('request fields');
    if (facts.kind === 'document') {
      const before = facts.previous;
      const after = state && state.lease;
      if (!before || !after || after.lease_id === before.lease_id || after.generation <= before.generation
          || after.document_id === before.document_id) problems.push('document did not advance the lease');
    }
    for (const problem of problems) this.violations.push(`${facts.kind}#${facts.step_id}: ${problem}`);
    return problems.length ? `BROWSER_POLICY_UNAVAILABLE: ${problems.join('; ')}` : '';
  }

  request(method, params) {
    return new Promise((resolve) => {
      const id = ++this.sequence;
      this.waiting.set(id, resolve);
      this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
    });
  }

  /** tools/call with a host-generated invocation id. Returns the parsed first text item. */
  async call(name, args = {}, { invocationId } = {}) {
    const id = invocationId === undefined ? `inv-${++this.invocations}` : invocationId;
    const params = { name, arguments: args };
    if (id !== null) params._meta = { ouroboros_invocation_id: id };
    const started = Date.now();
    if (id !== null) this.active.add(id);
    const { result, error } = await this.request('tools/call', params).finally(() => this.active.delete(id));
    if (error) throw new Error(`JSON-RPC error: ${error.message}`);
    const texts = result.content.filter((item) => item.type === 'text');
    return {
      invocationId: id, elapsed_ms: Date.now() - started, isError: result.isError === true, content: result.content, json: JSON.parse(texts[0].text),
      images: result.content.filter((item) => item.type === 'image'),
    };
  }

  factsFor(invocationId) { return this.facts.filter((facts) => facts.invocation_id === invocationId); }

  /** Closes stdin (EOF) and waits for the process; returns its shutdown line. */
  async stop() {
    if (this.stopped) return this.stopped;
    this.stopped = this.end();
    return this.stopped;
  }

  async end() {
    if (this.child && this.child.exitCode === null) {
      this.child.stdin.end();
      await Promise.race([this.exited, sleep(5000).then(() => this.child.kill('SIGKILL'))]);
    }
    await new Promise((resolve) => this.guard.close(resolve));
    const line = this.stderr.split('\n').find((text) => text.includes('"event":"shutdown"'));
    return line ? JSON.parse(line) : null;
  }
}

// ---------------------------------------------------------------- fixture web server

export async function startFixtureServer() {
  const requests = [];
  const held = [];  // answers of /slow, sent only when the test releases them
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => {
      const body = Buffer.concat(chunks).toString('utf8');
      const url = new URL(request.url, 'http://fixture');
      requests.push({ method: request.method, path: url.pathname, query: url.search, body });
      if (url.pathname === '/slow') {
        held.push(() => {
          response.writeHead(200, { 'content-type': 'text/html' });
          response.end('<!doctype html><title>slow</title><p id="slow">loaded late</p>');
        });
      } else if (url.pathname === '/redirect') {
        response.writeHead(307, { location: '/echo' });
        response.end();
      } else if (url.pathname === '/go-second') {
        response.writeHead(302, { location: '/second.html' });
        response.end();
      } else if (url.pathname === '/echo' || url.pathname.startsWith('/blocked')) {
        response.writeHead(200, { 'content-type': 'text/html' });
        response.end(`<!doctype html><title>echo</title><pre id="echo">${request.method} ${url.pathname} ${body.length}</pre>`);
      } else {
        const file = path.join(FIXTURES, path.basename(url.pathname));
        fs.readFile(file, (error, data) => {
          response.writeHead(error ? 404 : 200, { 'content-type': file.endsWith('.js') ? 'text/javascript' : 'text/html', 'cache-control': 'no-store' });
          response.end(error ? 'not found' : data);
        });
      }
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return {
    origin: `http://127.0.0.1:${server.address().port}`, requests,
    held: () => held.length,
    release: () => { for (const answer of held.splice(0)) answer(); },
    close: () => new Promise((resolve) => { held.length = 0; server.closeAllConnections(); server.close(resolve); }),
  };
}

export function freePort() {
  return new Promise((resolve) => {
    const probe = net.createServer();
    probe.listen(0, '127.0.0.1', () => { const { port } = probe.address(); probe.close(() => resolve(port)); });
  });
}
