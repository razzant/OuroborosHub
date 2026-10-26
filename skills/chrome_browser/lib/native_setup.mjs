// Owner-invoked Chrome Native Messaging registration. No browser is launched.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { packageOrigin, SKILL_DIR } from './pairing.mjs';

export const NATIVE_HOST_NAME = 'com.ouroboros.chrome_browser';
// A browser reads a user's native hosts from <its user data directory>/NativeMessagingHosts. These are the
// default user data directories; a browser started with --user-data-dir reads that directory's instead.
export const BROWSERS = {
  chrome: { darwin: ['Google', 'Chrome'], linux: 'google-chrome' },
  'chrome-for-testing': { darwin: ['Google', 'Chrome for Testing'], linux: 'google-chrome-for-testing' },
  chromium: { darwin: ['Chromium'], linux: 'chromium' },
};
const quote = (value) => `'${value.replaceAll("'", "'\\''")}'`;

/** The registration file `browser` reads for this user. Nothing is written. */
export function nativeRegistration({
  browser, userDataDir, platform = process.platform, home = os.homedir(), env = process.env,
} = {}) {
  if (platform !== 'darwin' && platform !== 'linux')
    throw new Error('Native bootstrap setup supports macOS and Linux only.');
  if (!Object.hasOwn(BROWSERS, browser || ''))
    throw new Error(`Choose the browser: --browser ${Object.keys(BROWSERS).join(', --browser ')}.`);
  let dataDir = userDataDir;
  if (dataDir == null && platform === 'darwin')
    dataDir = path.join(home, 'Library', 'Application Support', ...BROWSERS[browser].darwin);
  else if (dataDir == null) {
    const config = [env.CHROME_CONFIG_HOME, env.XDG_CONFIG_HOME].find((value) => value && path.isAbsolute(value));
    dataDir = path.join(config || path.join(home, '.config'), BROWSERS[browser].linux);
  }
  if (!path.isAbsolute(dataDir) || !path.isAbsolute(home))
    throw new Error('Native bootstrap requires absolute home and user data paths.');
  return path.join(dataDir, 'NativeMessagingHosts', `${NATIVE_HOST_NAME}.json`);
}

export function installNativeHost(dir, { node = process.execPath, ...where } = {}) {
  const registration = nativeRegistration(where);
  if (!path.isAbsolute(dir) || !path.isAbsolute(node))
    throw new Error('Native bootstrap requires absolute state and Node paths.');
  const launcher = path.join(dir, 'chrome_browser_native_host.sh');
  const script = path.join(SKILL_DIR, 'scripts', 'native_host.mjs');
  const manifest = {
    name: NATIVE_HOST_NAME, description: 'Ouroboros Chrome Browser pairing bootstrap',
    path: launcher, type: 'stdio', allowed_origins: [`${packageOrigin()}/`],
  };
  const launcherText = `#!/bin/sh\nexec ${quote(node)} ${quote(script)} ${quote(dir)} "$@"\n`;
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.dirname(registration), { recursive: true, mode: 0o700 });
  for (const [file, content, mode] of [
    [launcher, launcherText, 0o700], [registration, `${JSON.stringify(manifest, null, 2)}\n`, 0o600],
  ]) {
    const temporary = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(temporary, content, { mode });
    fs.chmodSync(temporary, mode);
    fs.renameSync(temporary, file);
  }
  return { registration, launcher };
}
