// Process entry for the chrome_browser MCP server (stdio).
import { loadPinnedPlaywright, loadPinnedUtils, nodeModulesDir, PINS } from './pinned.mjs';
import { takeGuardEnvironment } from './guard.mjs';
import { createServerCore, SERVER_NAME, SERVER_INSTRUCTIONS } from './server.mjs';

const SCRUBBED_PREFIXES = ['PLAYWRIGHT_', 'PWTEST_', 'PW_'];
const SCRUBBED_NAMES = ['PWDEBUG', 'DEBUG', 'DEBUG_FILE', 'DEBUG_COLORS', 'NODE_DEBUG'];
export const SHUTDOWN_TIMEOUT_MS = 5_000;

// Upstream Playwright reads many environment variables (token bypass, protocol override,
// init pages, capabilities, debug logs that print relay URLs). None of them may steer this server.
export function scrubEnvironment(env = process.env) {
  const removed = [];
  for (const key of Object.keys(env)) {
    if (SCRUBBED_PREFIXES.some((prefix) => key.startsWith(prefix)) || SCRUBBED_NAMES.includes(key)) {
      delete env[key];
      removed.push(key);
    }
  }
  return removed;
}

// The tab-group label the owner sees in Chrome ("Playwright · <label>").
export function clientLabel(env = process.env) {
  const raw = String(env.OUROBOROS_CHROME_CLIENT_LABEL || '').replace(/[^\p{L}\p{N} ._-]/gu, '').trim().slice(0, 40);
  return raw || 'Ouroboros';
}

export async function main({ env = process.env, stdin = process.stdin, stdout = process.stdout, exit = (code) => process.exit(code) } = {}) {
  scrubEnvironment(env);
  const guardEnvironment = takeGuardEnvironment(env);
  const root = nodeModulesDir(env);

  // The MCP protocol layer is the official SDK inside the pinned utilsBundle. Without a verified
  // copy this server cannot speak MCP at all, so it says why on stderr (no secrets) and exits.
  let utils;
  try {
    utils = loadPinnedUtils(root);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    exit(1);
    return null;
  }

  // The patched Playwright bundle is loaded on the first tools/list or tools/call, not at start.
  let runtime = null;
  const loadRuntime = () => {
    runtime ??= { ...loadPinnedPlaywright(root), extension: PINS.extension };
    return runtime;
  };
  const approvalTtlMs = Number(env.OUROBOROS_CHROME_APPROVAL_TTL_MS) > 0 ? Number(env.OUROBOROS_CHROME_APPROVAL_TTL_MS) : undefined;
  const core = createServerCore({ loadRuntime, guardEnvironment, clientName: clientLabel(env), approvalTtlMs });

  const server = new utils.Server(
      { name: SERVER_NAME, version: `${PINS.npm['@playwright/mcp'].version}+chrome_browser` },
      { capabilities: { tools: { listChanged: false } }, instructions: SERVER_INSTRUCTIONS });
  server.setRequestHandler(utils.ListToolsRequestSchema, async () => await core.listTools());
  server.setRequestHandler(utils.CallToolRequestSchema, async (request, extra) => await core.callTool(request.params.name, request.params.arguments || {}, extra?.signal));

  let shuttingDown = false;
  const shutdown = async (code = 0) => {
    if (shuttingDown)
      return;
    shuttingDown = true;
    const timer = setTimeout(() => exit(code), SHUTDOWN_TIMEOUT_MS);
    timer.unref?.();
    try {
      await core.shutdown();
    } catch {
      // closing is best effort; the timer still ends the process
    }
    clearTimeout(timer);
    exit(code);
  };

  server.onclose = () => void shutdown(0);
  stdin.on('end', () => void shutdown(0));
  stdin.on('close', () => void shutdown(0));
  for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP'])
    process.on(signal, () => void shutdown(0));
  process.on('unhandledRejection', () => {
    // A stray rejection must not drop the owner's connection; its text is not printed for the same reason.
    process.stderr.write('CHROME_BROWSER_UNHANDLED_REJECTION: an internal promise failed; the server continues.\n');
  });
  process.on('uncaughtException', () => {
    // Never print the error itself: Playwright messages can contain relay endpoints.
    process.stderr.write('CHROME_BROWSER_INTERNAL_ERROR: an unexpected error stopped the server.\n');
    void shutdown(1);
  });
  await server.connect(new utils.StdioServerTransport(stdin, stdout));
  return { server, core };
}
