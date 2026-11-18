// A minimal MCP client over stdio (newline-delimited JSON-RPC 2.0), enough to drive the skill's server the way a host
// does: initialize, tools/list, tools/call. No SDK and no dependency.
import { spawn } from 'node:child_process';
import fs from 'node:fs';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export const PROTOCOL_VERSION = '2025-06-18';

export class McpClient {
  static start({ command, args, env, cwd, stderrFile }) {
    const child = spawn(command, args, { env, cwd, stdio: ['pipe', 'pipe', 'pipe'] });
    return new McpClient(child, stderrFile);
  }

  constructor(child, stderrFile) {
    this.child = child;
    this.nextId = 1;
    this.pending = new Map();
    this.notifications = [];
    this.exit = null;
    this.exited = new Promise((resolve) => {
      child.on('exit', (code, signal) => {
        this.exit = { code, signal, at: Date.now() };
        for (const waiter of this.pending.values()) {
          clearTimeout(waiter.timer);
          waiter.reject(new Error(`the MCP server exited (code ${code}, signal ${signal})`));
        }
        this.pending.clear();
        resolve(this.exit);
      });
    });
    child.on('error', (error) => {
      this.spawnError = String(error?.message || error);
    });
    const stderr = fs.createWriteStream(stderrFile, { flags: 'a' });
    stderr.on('error', () => {});
    child.stderr.pipe(stderr);
    child.stdin.on('error', () => {});
    let buffer = Buffer.alloc(0);
    child.stdout.on('data', (part) => {
      buffer = buffer.length ? Buffer.concat([buffer, part]) : part;
      let newline;
      while ((newline = buffer.indexOf(0x0a)) >= 0) {
        const line = buffer.subarray(0, newline).toString('utf8').trim();
        buffer = buffer.subarray(newline + 1);
        if (line)
          this._onLine(line);
      }
    });
  }

  _onLine(line) {
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      this.notifications.push({ at: Date.now(), unparsed: line.slice(0, 200) });
      return;
    }
    if (message.id !== undefined && message.method === undefined) {
      const waiter = this.pending.get(message.id);
      if (!waiter)
        return;
      this.pending.delete(message.id);
      clearTimeout(waiter.timer);
      if (message.error)
        waiter.reject(Object.assign(new Error(`MCP error ${message.error.code}: ${message.error.message}`), { rpc: message.error }));
      else
        waiter.resolve(message.result);
      return;
    }
    if (message.method !== undefined && message.id !== undefined) {
      // The server asked the client something; this client offers no client features.
      this._send({ jsonrpc: '2.0', id: message.id, error: { code: -32601, message: 'not supported by this smoke-test client' } });
      this.notifications.push({ at: Date.now(), request: message.method });
      return;
    }
    this.notifications.push({ at: Date.now(), method: message.method });
  }

  _send(message) {
    this.child.stdin.write(`${JSON.stringify(message)}\n`);
  }

  request(method, params, timeoutMs = 60_000) {
    if (this.exit)
      return Promise.reject(new Error('the MCP server is not running'));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`no answer to ${method} within ${timeoutMs} ms`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this._send({ jsonrpc: '2.0', id, method, params });
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(error);
      }
    });
  }

  notify(method, params) {
    this._send({ jsonrpc: '2.0', method, ...(params ? { params } : {}) });
  }

  async initialize() {
    const result = await this.request('initialize', {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: {},
      clientInfo: { name: 'hub324-tabhandle-smoke', version: '1.0.0' },
    }, 30_000);
    this.notify('notifications/initialized');
    return result;
  }

  async listTools() {
    return (await this.request('tools/list', {}, 60_000)).tools || [];
  }

  async callTool(name, args, timeoutMs = 90_000) {
    return await this.request('tools/call', { name, arguments: args }, timeoutMs);
  }

  // Ends the server the way a host does (its input closes), then stops only this child if it does not exit.
  async close(timeoutMs = 8_000) {
    if (this.exit)
      return { ...this.exit, how: 'already-exited' };
    try {
      this.child.stdin.end();
    } catch {}
    let exit = await Promise.race([this.exited, sleep(timeoutMs).then(() => null)]);
    if (exit)
      return { ...exit, how: 'stdin-closed' };
    this.child.kill('SIGTERM');
    exit = await Promise.race([this.exited, sleep(3_000).then(() => null)]);
    if (exit)
      return { ...exit, how: 'SIGTERM' };
    this.child.kill('SIGKILL');
    return { ...(await this.exited), how: 'SIGKILL' };
  }
}
