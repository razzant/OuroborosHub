// Locate, verify and load the pinned official Playwright MCP server code.
//
// The installed npm files are read once, checked against pins.json, and the patched
// coreBundle is compiled from those exact bytes (bootstrap and utilsBundle too), then
// registered in Node's module cache under the real file names. Nothing on disk is changed,
// and no second, unpatched copy of the bundle can be loaded into this process afterwards.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import Module from 'node:module';
import { fileURLToPath } from 'node:url';
import { applyUnifiedDiff } from './patch.mjs';

export const SKILL_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const PINS = JSON.parse(fs.readFileSync(path.join(SKILL_ROOT, 'pins.json'), 'utf8'));

export class SetupError extends Error {
  constructor(code, message, problems = []) {
    super(`${code}: ${message}`);
    this.name = 'SetupError';
    this.code = code;
    this.problems = problems;
  }
}

const sha256 = (buffer) => crypto.createHash('sha256').update(buffer).digest('hex');

// The host installs the skill's npm dependency into <skill>/.ouroboros_env/node/node_modules.
// OUROBOROS_CHROME_NODE_MODULES names another node_modules directory (disposable test roots).
export function nodeModulesDir(env = process.env) {
  const override = env.OUROBOROS_CHROME_NODE_MODULES;
  if (override)
    return path.resolve(override);
  return path.join(SKILL_ROOT, '.ouroboros_env', 'node', 'node_modules');
}

function readPinnedFile(root, pkg, relative, expected, problems) {
  const file = path.join(root, pkg, relative);
  let real;
  let bytes;
  try {
    real = fs.realpathSync(file);
    bytes = fs.readFileSync(real);
  } catch {
    problems.push({ code: 'missing', file: `${pkg}/${relative}` });
    return null;
  }
  const actual = sha256(bytes);
  if (expected && actual !== expected)
    problems.push({ code: 'hash_mismatch', file: `${pkg}/${relative}`, expected, actual });
  return { file: real, bytes, sha256: actual };
}

// Checks only; nothing is compiled or executed.
export function verifyDependencies(root = nodeModulesDir()) {
  const problems = [];
  const files = {};
  for (const [pkg, pin] of Object.entries(PINS.npm)) {
    if (!pin.files_sha256)
      continue;
    for (const [relative, expected] of Object.entries(pin.files_sha256))
      files[`${pkg}/${relative}`] = readPinnedFile(root, pkg, relative, expected, problems);
    const manifest = files[`${pkg}/package.json`];
    if (manifest) {
      try {
        const parsed = JSON.parse(manifest.bytes.toString('utf8'));
        if (parsed.version !== pin.version)
          problems.push({ code: 'version_mismatch', package: pkg, expected: pin.version, actual: parsed.version });
      } catch {
        problems.push({ code: 'unreadable', file: `${pkg}/package.json` });
      }
    }
  }
  const mcp = files['@playwright/mcp/package.json'];
  if (mcp && !problems.some((p) => p.file === '@playwright/mcp/package.json')) {
    const declared = JSON.parse(mcp.bytes.toString('utf8')).dependencies?.['playwright-core'];
    if (declared !== PINS.npm['playwright-core'].version)
      problems.push({ code: 'dependency_mismatch', package: '@playwright/mcp', expected: PINS.npm['playwright-core'].version, actual: declared ?? null });
  }
  return { ok: problems.length === 0, root, problems, files };
}

export function describeProblems(problems) {
  return problems.map((p) => {
    switch (p.code) {
      case 'missing': return `${p.file} is missing`;
      case 'hash_mismatch': return `${p.file} has sha256 ${p.actual}, expected ${p.expected}`;
      case 'version_mismatch': return `${p.package} is ${p.actual}, expected ${p.expected}`;
      case 'dependency_mismatch': return `${p.package} depends on playwright-core ${p.actual}, expected ${p.expected}`;
      default: return `${p.file || p.package}: ${p.code}`;
    }
  }).join('; ');
}

function setupMessage(root, problems) {
  const allMissing = problems.length > 0 && problems.every((p) => p.code === 'missing');
  const want = `@playwright/mcp ${PINS.npm['@playwright/mcp'].version} with playwright-core ${PINS.npm['playwright-core'].version}`;
  if (allMissing)
    return `chrome_browser needs ${want} in ${root}. They are installed when the skill is enabled (Ouroboros runs npm for its declared dependency; npm must be on PATH). Nothing was started.`;
  return `chrome_browser found a different Playwright than it was written for in ${root} (${describeProblems(problems)}). It needs exactly ${want}; reinstall the skill's dependencies. Nothing was started.`;
}

export function compileCommonJs(filename, source) {
  const existing = Module._cache[filename];
  if (existing)
    return existing.exports;
  const mod = new Module(filename, null);
  mod.filename = filename;
  mod.paths = Module._nodeModulePaths(path.dirname(filename));
  Module._cache[filename] = mod;
  try {
    mod._compile(source, filename);
  } catch (error) {
    delete Module._cache[filename];
    throw error;
  }
  mod.loaded = true;
  return mod.exports;
}

let loaded = null;
let utilsLoaded = null;

// The MCP protocol layer (the official SDK inside utilsBundle), verified on its own so that a
// problem with the patched bundle can still be reported over MCP.
export function loadPinnedUtils(root = nodeModulesDir()) {
  if (utilsLoaded)
    return utilsLoaded;
  const problems = [];
  const pin = PINS.npm['playwright-core'].files_sha256;
  const bootstrap = readPinnedFile(root, 'playwright-core', 'lib/bootstrap.js', pin['lib/bootstrap.js'], problems);
  const utilsBundle = readPinnedFile(root, 'playwright-core', 'lib/utilsBundle.js', pin['lib/utilsBundle.js'], problems);
  if (problems.length)
    throw new SetupError(problems.every((p) => p.code === 'missing') ? 'CHROME_BROWSER_NOT_INSTALLED' : 'CHROME_BROWSER_PIN_MISMATCH', setupMessage(root, problems), problems);
  compileCommonJs(bootstrap.file, bootstrap.bytes.toString('utf8'));
  utilsLoaded = compileCommonJs(utilsBundle.file, utilsBundle.bytes.toString('utf8'));
  return utilsLoaded;
}

// Verified, patched and compiled once per process.
export function loadPinnedPlaywright(root = nodeModulesDir()) {
  if (loaded)
    return loaded;
  const verified = verifyDependencies(root);
  if (!verified.ok)
    throw new SetupError(verified.problems.every((p) => p.code === 'missing') ? 'CHROME_BROWSER_NOT_INSTALLED' : 'CHROME_BROWSER_PIN_MISMATCH', setupMessage(root, verified.problems), verified.problems);
  const core = verified.files['playwright-core/lib/coreBundle.js'];
  const patchText = fs.readFileSync(path.join(SKILL_ROOT, PINS.patch.file), 'utf8');
  let patched;
  try {
    patched = applyUnifiedDiff(core.bytes.toString('utf8'), patchText);
  } catch (error) {
    throw new SetupError('CHROME_BROWSER_PATCH_FAILED', `the pinned patch does not apply: ${error.message}`);
  }
  const postimage = sha256(Buffer.from(patched, 'utf8'));
  if (postimage !== PINS.patch.postimage_sha256)
    throw new SetupError('CHROME_BROWSER_PATCH_FAILED', `the patched bundle has sha256 ${postimage}, expected ${PINS.patch.postimage_sha256}`);
  if (Module._cache[core.file])
    throw new SetupError('CHROME_BROWSER_PATCH_FAILED', 'an unpatched Playwright bundle is already loaded in this process');
  const utils = loadPinnedUtils(root);
  const bundle = compileCommonJs(core.file, patched);
  if (typeof bundle.tools?.CDPRelayServer !== 'function' || typeof bundle.tools?.setPointerActionHooks !== 'function')
    throw new SetupError('CHROME_BROWSER_PATCH_FAILED', 'the patched bundle does not export the adapter entry points');
  loaded = {
    bundle,
    utils,
    playwright: bundle.inprocess.playwright,
    identity: {
      node_modules: root,
      '@playwright/mcp': PINS.npm['@playwright/mcp'].version,
      'playwright-core': PINS.npm['playwright-core'].version,
      coreBundle_preimage_sha256: core.sha256,
      coreBundle_postimage_sha256: postimage,
    },
  };
  return loaded;
}
