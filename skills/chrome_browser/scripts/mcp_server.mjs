#!/usr/bin/env node
// MCP stdio server of the chrome_browser skill: JSON-RPC 2.0, one message per
// line. The host starts it for one task attempt; see ../PROTOCOL.md.

import readline from 'node:readline';

import { BrowserError, EXECUTOR_OPERATIONS, Executor } from '../lib/executor.mjs';
import { OPERATIONS } from '../extension/bridge_protocol.mjs';

const SERVER = { name: 'ouroboros-chrome-browser', version: '0.1.0' };
const MCP_VERSIONS = ['2025-06-18', '2025-03-26', '2024-11-05'];
const NO_ARGUMENTS = { type: 'object', properties: {}, additionalProperties: false };
const TOOLS = [
  { name: 'browser_state', description: 'The executor state as one JSON object: owner, executor generation, lease, closure. Opens nothing in the browser.', inputSchema: NO_ARGUMENTS },
  {
    name: 'browser_connect',
    description: 'Attach to one tab: exactly {"tab_id"} for a tab the owner shared in the extension, or exactly {"url"} to open the agent\'s own background tab.',
    inputSchema: {
      type: 'object', additionalProperties: false, minProperties: 1, maxProperties: 1,
      properties: { tab_id: { type: 'string' }, url: { type: 'string' } },
    },
  },
  {
    name: 'browser_action',
    description: 'One typed operation on the leased tab. Input operations use trusted pointer and keyboard events and refuse an obscured, hidden, disabled or moving target. "sequence" runs up to 32 {operation,args} steps in order and stops at the first failure; a read with cache_key keeps its text for "cached_text".',
    inputSchema: {
      type: 'object', additionalProperties: false, required: ['operation', 'args'],
      properties: { operation: { type: 'string', enum: [...OPERATIONS, ...EXECUTOR_OPERATIONS] }, args: { type: 'object' } },
    },
  },
  {
    name: 'browser_execute',
    description: 'Run a script body as async (browser, vars, console, sleep) in the task\'s persistent realm. browser.<operation>(args) are the same typed operations; vars keeps values between calls. Not a sandbox.',
    inputSchema: { type: 'object', additionalProperties: false, required: ['code'], properties: { code: { type: 'string' } } },
  },
  { name: 'browser_handoff', description: 'Give the tab to the owner now: the debugger is detached, the page and its unsaved fields stay.', inputSchema: NO_ARGUMENTS },
  { name: 'browser_resume', description: 'Attach again to the handed-off tab and read its document afresh.', inputSchema: NO_ARGUMENTS },
  { name: 'browser_disconnect', description: 'End the lease now and report whether Chrome confirmed the detach.', inputSchema: NO_ARGUMENTS },
];

const executor = new Executor(process.env);
const write = (message) => process.stdout.write(`${JSON.stringify(message)}\n`);
const text = (value, isError = false) => ({ content: [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value) }], isError });

async function control(run) {
  await executor.ensureListener();
  await run();
  await executor.settleGuard();
  return text(executor.state());
}

async function callTool(name, args, invocationId) {
  // Before anything else, also before the loopback port is bound: a host that does not speak this protocol gets no listener.
  executor.requireHost();
  switch (name) {
    case 'browser_state': return control(async () => {});
    case 'browser_connect': return control(() => executor.connect(args, invocationId));
    case 'browser_handoff': return control(() => executor.handoffToOwner());
    case 'browser_resume': return control(() => executor.resume(invocationId));
    case 'browser_disconnect': return control(() => executor.disconnect());
    case 'browser_action':
    case 'browser_execute': {
      await executor.ensureListener();
      const receipt = name === 'browser_action'
        ? await executor.action(args.operation, args.args, invocationId)
        : await executor.execute(typeof args.code === 'string' ? args.code : '', invocationId);
      const result = text(receipt.text, receipt.isError);
      for (const image of receipt.images) result.content.push({ type: 'image', data: image.data, mimeType: image.mimeType });
      return result;
    }
    default:
      throw new BrowserError('BROWSER_POLICY_UNAVAILABLE', `unknown tool ${name}`);
  }
}

async function handle(message) {
  const { id, method, params } = message;
  const answer = (result) => { if (id !== undefined) write({ jsonrpc: '2.0', id, result }); };
  switch (method) {
    case 'initialize':
      answer({
        protocolVersion: MCP_VERSIONS.includes(params && params.protocolVersion) ? params.protocolVersion : MCP_VERSIONS[0],
        capabilities: { tools: { listChanged: false } }, serverInfo: SERVER,
      });
      return;
    case 'ping': answer({}); return;
    case 'tools/list': answer({ tools: TOOLS }); return;
    case 'tools/call': {
      const args = params && typeof params.arguments === 'object' && params.arguments !== null ? params.arguments : {};
      const meta = params && params._meta;
      try {
        answer(await callTool(params && params.name, args, meta && meta.ouroboros_invocation_id));
      } catch (error) {
        const code = error instanceof BrowserError ? error.code : 'BROWSER_INTERNAL';
        await executor.settleGuard();  // a refusal reports state too, and no lease the host has not witnessed
        answer(text({ ok: false, error: { code, message: String((error && error.message) || error) }, state: executor.state() }, true));
      }
      return;
    }
    default:
      if (id !== undefined) write({ jsonrpc: '2.0', id, error: { code: -32601, message: `Method not found: ${method}` } });
  }
}

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
lines.on('line', (line) => {
  if (!line.trim()) return;
  let message;
  try {
    message = JSON.parse(line);
  } catch (error) {
    write({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } });
    return;
  }
  // null, a number, a string or a batch array is JSON but no request: answered, and nothing else is read from it.
  if (message === null || typeof message !== 'object' || Array.isArray(message)) {
    write({ jsonrpc: '2.0', id: null, error: { code: -32600, message: 'Invalid Request' } });
    return;
  }
  // Not awaited: a control call is answered while a script is still running.
  handle(message).catch((error) => {
    if (message.id !== undefined) write({ jsonrpc: '2.0', id: message.id, error: { code: -32603, message: String(error && error.message) } });
  });
});

// A script may drop a promise that later carries one of the executor's own
// refusals: a helper still waiting on a sleep or a step when its invocation is
// closed. That is the script's promise, not a fault of this process, and it
// must not end the executor while a tab is attached. Anything else unhandled
// still ends it, as Node would.
process.on('unhandledRejection', (reason) => {
  if (!(reason instanceof BrowserError)) throw reason;
  process.stderr.write(`${JSON.stringify({ event: 'dropped_refusal', code: reason.code })}\n`);
});

let closing = false;
async function shutdown() {
  if (closing) return;
  closing = true;
  // stdio EOF proves nothing about Chrome: say separately whether the detach was confirmed.
  const acknowledged = await executor.shutdown();
  process.stderr.write(`${JSON.stringify({ event: 'shutdown', debugger_detach_ack: acknowledged })}\n`);
  process.exit(0);
}
lines.on('close', shutdown);
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
