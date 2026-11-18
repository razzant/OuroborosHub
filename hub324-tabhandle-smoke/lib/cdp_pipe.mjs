// Chrome for Testing, started headed with --remote-debugging-pipe: the harness's control channel is the two pipes
// Chrome inherits (fd 3 in, fd 4 out, NUL-terminated JSON), so no debugging port is open to anyone else.
//
// The channel is deliberately narrow (see ALLOWED and permitPage in smoke.mjs): the harness opens the approval link
// in this test browser, presses its one button for the synthetic tab A, reads chrome://sandbox, lists targets and
// closes tab B as the owner would. It has no screenshot, script or input command for any other page: every image this
// test judges comes from the skill. Its log is a local diagnostic: page addresses in it are redacted (parse.mjs).
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import { redact } from './parse.mjs';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export const ALLOWED = new Set([
  'Browser.getVersion', 'Browser.close', 'Target.getTargets', 'Target.createTarget', 'Target.attachToTarget',
  'Target.detachFromTarget', 'Target.closeTarget', 'Runtime.evaluate', 'Input.dispatchMouseEvent',
]);
// Methods that act inside one page: only on sessions the harness attached to one of its own permitted pages.
const SESSION_METHODS = new Set(['Runtime.evaluate', 'Input.dispatchMouseEvent', 'Target.detachFromTarget']);

export class ChromePipe {
  // `permitPage(url)` says which pages the harness may open, attach to or close (see smoke.mjs).
  static launch({ executable, args, env, logFile, cdpLogFile, permitPage }) {
    const child = spawn(executable, args, { env, stdio: ['ignore', 'pipe', 'pipe', 'pipe', 'pipe'] });
    return new ChromePipe(child, { logFile, cdpLogFile, permitPage });
  }

  constructor(child, { logFile, cdpLogFile, permitPage }) {
    this.child = child;
    this.pid = child.pid;
    this.permitPage = permitPage;
    this.nextId = 1;
    this.pending = new Map();
    this.sessions = new Map();
    this.targets = new Map();
    this.exit = null;
    this.cdpLog = fs.createWriteStream(cdpLogFile, { flags: 'a' });
    this.cdpLog.on('error', () => {});
    this.cdpLogOpen = true;
    const log = fs.createWriteStream(logFile, { flags: 'a' });
    log.on('error', () => {});
    child.stdout.pipe(log);
    child.stderr.pipe(log);
    child.on('error', (error) => {
      this.spawnError = String(error?.message || error);
    });
    this.exited = new Promise((resolve) => {
      child.on('exit', (code, signal) => {
        this.exit = { code, signal, at: Date.now() };
        for (const waiter of this.pending.values()) {
          clearTimeout(waiter.timer);
          waiter.reject(new Error(`Chrome exited (code ${code}, signal ${signal})`));
        }
        this.pending.clear();
        resolve(this.exit);
      });
    });
    this.writer = child.stdio[3];
    const reader = child.stdio[4];
    this.writer.on('error', () => {});
    reader.on('error', () => {});
    let buffer = Buffer.alloc(0);
    reader.on('data', (part) => {
      buffer = buffer.length ? Buffer.concat([buffer, part]) : part;
      let end;
      while ((end = buffer.indexOf(0)) >= 0) {
        const raw = buffer.subarray(0, end).toString('utf8');
        buffer = buffer.subarray(end + 1);
        let message;
        try {
          message = JSON.parse(raw);
        } catch {
          continue;
        }
        if (message.id === undefined)
          continue;
        const waiter = this.pending.get(message.id);
        if (!waiter)
          continue;
        this.pending.delete(message.id);
        clearTimeout(waiter.timer);
        if (message.error)
          waiter.reject(new Error(`${waiter.method}: ${message.error.message} (${message.error.code})`));
        else
          waiter.resolve(message.result);
      }
    });
  }

  _log(entry) {
    if (this.cdpLogOpen)
      this.cdpLog.write(`${JSON.stringify(entry)}\n`);
  }

  _refuse(method, why) {
    this._log({ at: Date.now(), method, refused: why });
    return Promise.reject(new Error(`harness guard rail: ${method} ${why}`));
  }

  async send(method, params = {}, sessionId, timeoutMs = 30_000) {
    if (!ALLOWED.has(method))
      return this._refuse(method, 'is not a command this harness uses');
    if (SESSION_METHODS.has(method) && method !== 'Target.detachFromTarget' && !this.sessions.has(sessionId))
      return this._refuse(method, 'is only sent to a page the harness attached to');
    if (method === 'Target.createTarget' && !this.permitPage(params.url, 'open'))
      return this._refuse(method, 'only opens the approval link or chrome://sandbox');
    if (method === 'Target.attachToTarget' && !this.permitPage(this.targets.get(params.targetId), 'attach'))
      return this._refuse(method, 'only attaches to the approval page or chrome://sandbox');
    if (method === 'Target.closeTarget' && !this.permitPage(this.targets.get(params.targetId), 'close'))
      return this._refuse(method, 'only closes tab B or chrome://sandbox');
    if (method === 'Target.detachFromTarget' && !this.sessions.has(params.sessionId))
      return this._refuse(method, 'only detaches the harness\'s own sessions');
    const id = this.nextId++;
    const entry = { at: Date.now(), id, method };
    if (params.url)
      entry.url = redact(params.url);
    if (params.targetId)
      entry.target = redact(this.targets.get(params.targetId) ?? 'unknown target');
    if (sessionId)
      entry.session = redact(this.sessions.get(sessionId) ?? '');
    if (method === 'Input.dispatchMouseEvent')
      entry.event = { type: params.type, x: params.x, y: params.y };
    this._log(entry);
    const result = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method}: no answer within ${timeoutMs} ms`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer, method });
      try {
        this.writer.write(`${JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) })}\0`);
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(error);
      }
    });
    if (method === 'Target.getTargets')
      for (const info of result.targetInfos || [])
        this.targets.set(info.targetId, info.url);
    if (method === 'Target.createTarget')
      this.targets.set(result.targetId, params.url);
    if (method === 'Target.attachToTarget')
      this.sessions.set(result.sessionId, this.targets.get(params.targetId));
    if (method === 'Target.detachFromTarget')
      this.sessions.delete(params.sessionId);
    return result;
  }

  async targetsNow() {
    return (await this.send('Target.getTargets')).targetInfos || [];
  }

  async evaluate(sessionId, expression, timeoutMs = 10_000) {
    const result = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId, timeoutMs);
    if (result.exceptionDetails)
      throw new Error(`evaluate failed: ${result.exceptionDetails.text} ${result.exceptionDetails.exception?.description || ''}`.trim());
    return result.result?.value;
  }

  async click(sessionId, x, y) {
    await this.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none' }, sessionId);
    await this.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 }, sessionId);
    await this.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 }, sessionId);
  }

  // Asks Chrome to close (Browser.close), then stops only this child process if it does not exit.
  async close() {
    if (this.exit)
      return { ...this.exit, how: 'already-exited' };
    try {
      await this.send('Browser.close', {}, undefined, 5_000);
    } catch {}
    let exit = await Promise.race([this.exited, sleep(10_000).then(() => null)]);
    if (exit)
      return { ...exit, how: 'Browser.close' };
    this.child.kill('SIGTERM');
    exit = await Promise.race([this.exited, sleep(5_000).then(() => null)]);
    if (exit)
      return { ...exit, how: 'SIGTERM' };
    this.child.kill('SIGKILL');
    return { ...(await this.exited), how: 'SIGKILL' };
  }

  async endLog() {
    this.cdpLogOpen = false;
    await new Promise((resolve) => this.cdpLog.end(resolve));
  }
}
