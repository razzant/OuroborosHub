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

// Project this skill's single-line JSON declaration into the older-host import format.
// This is deliberately not a general YAML reader or the host's manifest validator:
// other valid YAML spellings and multiple declarations need the host's Connect tools.
// Unsupported input gets a reason instead of a guessed import.
const KEYS = ['id', 'title', 'runtime', 'entry', 'task_session', 'browser_policy'];
const CACHE_DIRS = new Set(['__pycache__', 'node_modules', '.git', '.hg', '.svn', '.idea', '.vscode', '.tox',
  '.pytest_cache', '.ouroboros_env', '.DS_Store']);

function realFile(file) {
  try {
    const real = fs.realpathSync(file);
    return real.startsWith(fs.realpathSync(SKILL_ROOT) + path.sep) && fs.statSync(real).isFile();
  } catch {
    return false;
  }
}

// The one declared server, or a sentence saying what is wrong with it.
function readDeclaration() {
  const front = fs.readFileSync(path.join(SKILL_ROOT, 'SKILL.md'), 'utf8').replace(/^\uFEFF/, '').split(/^---\s*$/m)[1] ?? '';
  const lines = front.split(/\r?\n/);
  const field = (key) => lines.find((line) => line.startsWith(`${key}: `))?.slice(key.length + 2).trim();
  let list;
  try {
    list = JSON.parse(field('mcp_servers') ?? '');
  } catch {}
  const item = Array.isArray(list) && list.length === 1 ? list[0] : null;
  if (!item || typeof item !== 'object' || Array.isArray(item))
    return 'SKILL.md must declare exactly one server as one line of JSON: mcp_servers: [{"id": ..., "entry": ...}]';
  const unknown = Object.keys(item).filter((key) => !KEYS.includes(key));
  if (unknown.length)
    return `mcp_servers has unknown keys ${unknown.join(', ')}; keep only ${KEYS.join(', ')}`;
  const { id, entry, title = id } = item;
  if (typeof id !== 'string' || id.length > 24 || !/^[a-z0-9]+(?:_[a-z0-9]+)*$/.test(id))
    return `mcp_servers id ${JSON.stringify(id)} must be lowercase letters and digits joined by single "_", at most 24 characters`;
  const parts = typeof entry === 'string' ? entry.split('/') : [];
  if (!entry || typeof entry !== 'string' || /[\\:]/.test(entry) || entry.startsWith('/') || entry.endsWith('/')
      || parts.includes('..') || parts.some((part) => CACHE_DIRS.has(part)) || !realFile(path.join(SKILL_ROOT, entry)))
    return `mcp_servers ${id} entry ${JSON.stringify(entry)} must be a relative path of a file in this skill, outside dependency and cache folders`;
  const runtime = Object.hasOwn(item, 'runtime') ? item.runtime : field('runtime')?.replace(/^["']|["']$/g, '');
  if (typeof runtime !== 'string' || runtime.trim().toLowerCase() !== 'node')
    return `mcp_servers ${id} must use the node runtime`;
  if (typeof title !== 'string' || !title.trim()
      || [item.task_session, item.browser_policy].some((flag) => flag !== undefined && typeof flag !== 'boolean'))
    return `mcp_servers ${id}: title must be text; task_session and browser_policy are true or false`;
  if (item.browser_policy && !item.task_session)
    return `mcp_servers ${id} sets browser_policy without task_session`;
  return { id, title: title.trim(), entry, task_session: item.task_session === true, browser_policy: item.browser_policy === true };
}

const status = {
  skill: 'chrome_browser',
  ready: dependencies.ok && patch.ok,
  dependencies: {
    ok: dependencies.ok,
    node_modules: root,
    expected: `@playwright/mcp ${PINS.npm['@playwright/mcp'].version}, playwright-core ${PINS.npm['playwright-core'].version}`,
    problems: dependencies.ok ? '' : describeProblems(dependencies.problems),
  },
  patch,
  node: { path: process.execPath, version: process.version },
};
const declaration = readDeclaration();
if (typeof declaration === 'string') {
  const failure = {
    ...status,
    ready: false,
    error: {
      code: 'CHROME_BROWSER_DECLARATION_INVALID',
      problem: declaration,
      fix: 'Update or reinstall the skill so that SKILL.md is the published one; if the published file fails this check, report it to the skill author. Nothing is printed to import for a server the manifest does not validly declare.',
    },
  };
  process.stdout.write(`${JSON.stringify(failure, null, 2)}\n`);
  process.exit(1);
}

const server = {
  command: process.execPath, args: [path.join(SKILL_ROOT, declaration.entry)],
  task_session: declaration.task_session, browser_policy: declaration.browser_policy,
};
const report = {
  ...status,
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
    name: declaration.title,
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
    'Only on an older Ouroboros that does not offer Connect tools: open Settings → Advanced → MCP Servers and choose "Import mcp.json". Paste mcp_import_document (the whole object, its outer braces included), choose "Preview", then "Apply 1 to draft". If the preview says "Not imported: browser_policy, task_session", this Ouroboros cannot run the server yet: stop here and tell Ouroboros what it says.',
    'The server arrives switched off. Turn on its "Enabled" box and "Enable MCP client", then choose "Save Settings". Ouroboros lists its tools by itself; that opens no browser.',
    'When a task needs your browser, Ouroboros gives you a one-time link. Open your Chrome, paste it into a new tab, pick the tab to share and choose "Allow & select".',
  ],
};
process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
process.exit(report.ready ? 0 : 1);
