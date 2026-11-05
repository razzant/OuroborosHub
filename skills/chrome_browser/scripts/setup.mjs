#!/usr/bin/env node
// Passive dependency status and older-host import fallback. The manifest owns the server
// declaration; modern hosts provide Connect tools without a manual import.
import path from 'node:path';
import fs from 'node:fs';
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
// This skill uses a JSON-compatible YAML flow list, deliberately sharing one declaration
// with the host's YAML manifest reader instead of maintaining a second server definition.
const frontmatter = fs.readFileSync(path.join(SKILL_ROOT, 'SKILL.md'), 'utf8').split(/^---\s*$/m)[1];
const declarationLine = frontmatter?.split('\n').find(line => line.startsWith('mcp_servers: '));
const declarations = declarationLine && JSON.parse(declarationLine.slice('mcp_servers: '.length));
if (!Array.isArray(declarations) || declarations.length !== 1) {
  throw new Error('Expected the single JSON-flow mcp_servers declaration in SKILL.md');
}
const declaration = declarations[0];
if (!declaration.id || declaration.runtime !== 'node' || !declaration.entry
    || path.isAbsolute(declaration.entry) || declaration.entry.split(/[\\/]/).includes('..')) {
  throw new Error('Invalid Node MCP declaration in SKILL.md');
}
const server = {
  command: process.execPath, args: [path.join(SKILL_ROOT, declaration.entry)],
  task_session: declaration.task_session === true, browser_policy: declaration.browser_policy === true,
};
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
      [declaration.id]: { type: 'stdio', ...server },
    },
  },
  // The same server as one saved MCP_SERVERS row, for a tool that writes settings itself. Import refuses this shape.
  mcp_server_entry: {
    id: declaration.id,
    name: declaration.title || declaration.id,
    transport: 'stdio',
    ...server,
    enabled: true,
  },
  extension: {
    name: PINS.extension.name,
    id: PINS.extension.id,
    install_from: PINS.extension.store_url,
    tested_version: PINS.extension.tested_version,
    relay_protocol_version: PINS.extension.relay_protocol_version,
  },
  connection: {
    preferred: 'Choose Connect tools on the skill card, or ask Ouroboros to connect it; confirm the displayed changes. No JSON import is needed on hosts supporting mcp_servers.',
    readiness: 'This status checks dependencies and the patch, not host registration or browser approval.',
  },
  // Kept for older consumers; use connection.preferred on a host supporting Connect tools.
  owner_steps: [
    `In the Chrome profile to share, install "${PINS.extension.name}" by Microsoft from the Chrome Web Store (${PINS.extension.store_url}). This skill was tested with version ${PINS.extension.tested_version}.`,
    'In Ouroboros open Settings → Advanced → MCP Servers and choose "Import mcp.json". Paste mcp_import_document (the whole object, its outer braces included), choose "Preview", then "Apply 1 to draft". If the preview says "Not imported: browser_policy, task_session", this Ouroboros cannot run the server yet: stop here and tell Ouroboros what it says.',
    'The server arrives switched off. Turn on its "Enabled" box and "Enable MCP client", then choose "Save Settings". Ouroboros lists its tools by itself; that opens no browser.',
    'When a task needs your browser, Ouroboros gives you a one-time link. Open your Chrome, paste it into a new tab, pick the tab to share and choose "Allow & select".',
  ],
};
process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
process.exit(ready ? 0 : 1);
