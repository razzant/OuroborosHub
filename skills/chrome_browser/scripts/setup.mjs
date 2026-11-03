#!/usr/bin/env node
// `setup.mjs status`: checks the pinned dependency and the patch, and prints the document for this
// skill's server to paste into Settings → Advanced → MCP Servers → Import mcp.json. It writes
// nothing, starts no browser and contacts no service.
import path from 'node:path';
import { verifyDependencies, loadPinnedPlaywright, nodeModulesDir, describeProblems, PINS, SKILL_ROOT } from '../lib/pinned.mjs';

const command = process.argv[2] || 'status';
if (command !== 'status') {
  process.stderr.write('usage: setup.mjs status\n');
  process.exit(2);
}

const root = nodeModulesDir();
const dependencies = verifyDependencies(root);
let patch = { ok: false, detail: 'not checked: the dependency is not ready' };
if (dependencies.ok) {
  try {
    const loaded = loadPinnedPlaywright(root);
    patch = { ok: true, postimage_sha256: loaded.identity.coreBundle_postimage_sha256 };
  } catch (error) {
    patch = { ok: false, detail: error.message };
  }
}

const ready = dependencies.ok && patch.ok;
const server = { command: process.execPath, args: [path.join(SKILL_ROOT, 'scripts', 'server.mjs')] };
const report = {
  skill: 'chrome_browser',
  ready,
  dependencies: {
    ok: dependencies.ok,
    node_modules: root,
    expected: `@playwright/mcp ${PINS.npm['@playwright/mcp'].version}, playwright-core ${PINS.npm['playwright-core'].version}`,
    problems: dependencies.ok ? '' : describeProblems(dependencies.problems),
  },
  patch,
  node: { path: process.execPath, version: process.version },
  // What the owner pastes into Import mcp.json. The key is the server ID, which names every tool
  // (mcp_chrome_browser__*); the import takes it as the identity, so it is not this skill's title.
  mcp_import_document: {
    mcpServers: {
      chrome_browser: { type: 'stdio', ...server, task_session: true, browser_policy: true },
    },
  },
  // The same server as one saved MCP_SERVERS row, for a tool that writes settings itself. Import refuses this shape.
  mcp_server_entry: {
    id: 'chrome_browser',
    name: 'Chrome (shared tab)',
    transport: 'stdio',
    ...server,
    enabled: true,
    task_session: true,
    browser_policy: true,
  },
  extension: {
    name: PINS.extension.name,
    id: PINS.extension.id,
    install_from: PINS.extension.store_url,
    tested_version: PINS.extension.tested_version,
    relay_protocol_version: PINS.extension.relay_protocol_version,
  },
  owner_steps: [
    `In the Chrome profile to share, install "${PINS.extension.name}" by Microsoft from the Chrome Web Store (${PINS.extension.store_url}). This skill was tested with version ${PINS.extension.tested_version}.`,
    'In Ouroboros open Settings → Advanced → MCP Servers and choose "Import mcp.json". Paste mcp_import_document (the whole object, its outer braces included), choose "Preview", then "Apply 1 to draft". If the preview says "Not imported: browser_policy, task_session", this Ouroboros cannot run the server yet: stop here and tell Ouroboros what it says.',
    'The server arrives switched off. Turn on its "Enabled" box and "Enable MCP client", then choose "Save Settings". Ouroboros lists its tools by itself; that opens no browser.',
    'When a task needs your browser, Ouroboros gives you a one-time link. Open your Chrome, paste it into a new tab, pick the tab to share and choose "Allow & select".',
  ],
};
process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
process.exit(ready ? 0 : 1);
