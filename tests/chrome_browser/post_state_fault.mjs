// Test-only wire fault: the real Node/MV3 action completes, then the next
// browser_state JSON-RPC response is an error. The SDK (not a mock) raises McpError.
import { spawn } from 'node:child_process';
import readline from 'node:readline';

const child = spawn(process.execPath, [process.argv[2]], {
  env: process.env, stdio: ['pipe', 'pipe', 'inherit'],
});
const targeted = new Set();
let armed = false;
let injected = false;
readline.createInterface({ input: process.stdin }).on('line', (line) => {
  const request = JSON.parse(line);
  if (armed && !injected && request.method === 'tools/call'
      && request.params?.name === 'browser_state') {
    injected = true;
    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: request.id,
      error: { code: -32603, message: 'TEST_POST_RESPONSE_SDK_ERROR' } }) + '\n');
    return;
  }
  if (!injected && request.method === 'tools/call'
      && request.params?.name === 'browser_action'
      && request.params?.arguments?.operation === 'click'
      && request.params?.arguments?.args?.selector === '#go') targeted.add(request.id);
  child.stdin.write(line + '\n');
}).on('close', () => child.stdin.end());
readline.createInterface({ input: child.stdout }).on('line', (line) => {
  const response = JSON.parse(line);
  if (targeted.delete(response.id) && response.result) armed = true;
  process.stdout.write(line + '\n');
});
child.on('error', (error) => { console.error(error.message); process.exitCode = 1; });
child.on('exit', (code, signal) => { process.exitCode = code ?? (signal ? 1 : 0); });
