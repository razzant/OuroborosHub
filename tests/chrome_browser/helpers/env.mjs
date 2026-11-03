// Paths and disposable scratch for the chrome_browser tests; they run from any Hub checkout.
//
// Everything disposable lives under one directory in os.tmpdir(). It is removed when the process
// exits cleanly and kept (its path is printed) when the process fails, so a failed run can be read.
//
// The pinned-transform tests need the exact npm closure that skills/chrome_browser/pins.json records.
// OUROBOROS_CHROME_NODE_MODULES names its node_modules directory, the variable the skill's own loader
// reads for test roots; without it the skill's installed default is used.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
export const HUB = path.resolve(here, '..', '..', '..');
export const SKILL = path.join(HUB, 'skills', 'chrome_browser');
export const INSTRUMENT = path.join(here, 'instrument.cjs');
export const NODE = process.execPath;
export const NODE_MODULES = path.resolve(process.env.OUROBOROS_CHROME_NODE_MODULES || path.join(SKILL, '.ouroboros_env', 'node', 'node_modules'));

// The tests that load the stock Playwright bundle fail, saying what to install, when it is not there. They never skip.
export function requirePinnedInstall() {
  if (fs.existsSync(path.join(NODE_MODULES, 'playwright-core', 'lib', 'coreBundle.js')))
    return;
  const npm = JSON.parse(fs.readFileSync(path.join(SKILL, 'pins.json'), 'utf8')).npm;
  const mcp = `@playwright/mcp@${npm['@playwright/mcp'].version}`;
  throw new Error(`These tests need ${mcp} with playwright-core ${npm['playwright-core'].version} (pins.json) in ${NODE_MODULES}. `
    + `Install it with: npm install --ignore-scripts --no-audit --no-fund --prefix <dir> ${mcp}; then set `
    + 'OUROBOROS_CHROME_NODE_MODULES=<dir>/node_modules. They do not skip without it.');
}

let scratch = null;
export function scratchDir(name) {
  if (!scratch) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cb-'));
    scratch = root;
    process.on('exit', (code) => {
      if (code === 0)
        fs.rmSync(root, { recursive: true, force: true });
      else
        process.stderr.write(`scratch kept for inspection: ${root}\n`);
    });
  }
  return fs.mkdtempSync(path.join(scratch, `${name}-`));
}

// A Unix socket path is limited to about 100 bytes (104 on macOS), so a socket gets a short directory of its own.
export function socketDir() {
  for (const base of [os.tmpdir(), '/tmp']) {
    const dir = fs.mkdtempSync(path.join(base, 'cbs-'));
    if (Buffer.byteLength(path.join(dir, 'g.sock')) < 96)
      return dir;
    fs.rmSync(dir, { recursive: true, force: true });
  }
  throw new Error('no temporary directory with a path short enough for a Unix socket');
}

// Where the stock Playwright looks for the owner's Chrome profile, relative to HOME, on the platforms these tests have run on.
const PROFILE_DIR = {
  darwin: ['Library', 'Application Support', 'Google', 'Chrome'],
  linux: ['.config', 'google-chrome'],
};

// A HOME with a synthetic Chrome profile whose every access is visible in the instrument log.
export function fakeHome(name) {
  const layout = PROFILE_DIR[process.platform];
  if (!layout)
    throw new Error(`the synthetic Chrome profile is laid out for ${Object.keys(PROFILE_DIR).join(' and ')}, not ${process.platform}`);
  const home = scratchDir(name);
  const chrome = path.join(home, ...layout);
  fs.mkdirSync(path.join(chrome, 'Default'), { recursive: true });
  fs.writeFileSync(path.join(chrome, 'Local State'), JSON.stringify({ profile: { last_used: 'Default' } }));
  fs.writeFileSync(path.join(chrome, 'Default', 'Preferences'), JSON.stringify({ synthetic: true }));
  fs.writeFileSync(path.join(chrome, 'Default', 'Secure Preferences'), JSON.stringify({ synthetic: true }));
  fs.mkdirSync(path.join(home, 'tmp'));
  return { home, chrome, tmp: path.join(home, 'tmp') };
}
