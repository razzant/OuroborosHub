// QA preload (node --require instrument.cjs): records every child-process call, every socket
// listen/connect and every filesystem call with a path, as JSON lines in $QA_INSTRUMENT_LOG.
// Observation only: each wrapped call still runs unchanged.
'use strict';
const fs = require('fs');
const net = require('net');
const childProcess = require('child_process');
const Module = require('module');

const logFile = process.env.QA_INSTRUMENT_LOG;
const writeSync = fs.writeSync.bind(fs);
const openSync = fs.openSync.bind(fs);
const fd = logFile ? openSync(logFile, 'a') : null;
let depth = 0;

function record(kind, detail) {
  if (fd === null || depth > 0)
    return;
  depth += 1;
  try {
    writeSync(fd, JSON.stringify({ t: Date.now(), pid: process.pid, kind, ...detail }) + '\n');
  } catch {
    // never disturb the observed process
  } finally {
    depth -= 1;
  }
}

for (const name of ['spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork']) {
  const original = childProcess[name];
  childProcess[name] = function (...args) {
    record('child_process', { fn: name, file: String(args[0]), args: Array.isArray(args[1]) ? args[1].map(String) : [] });
    return original.apply(this, args);
  };
}

const originalListen = net.Server.prototype.listen;
net.Server.prototype.listen = function (...args) {
  this.once('listening', () => {
    const address = this.address();
    record('listen', { address: typeof address === 'string' ? address : address && { address: address.address, port: address.port, family: address.family } });
  });
  return originalListen.apply(this, args);
};

const originalConnect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const first = args[0];
  let target;
  if (first && typeof first === 'object')
    target = first.path ? { path: first.path } : { host: first.host, port: first.port };
  else
    target = typeof first === 'string' && !/^\d+$/.test(first) ? { path: first } : { port: first, host: args[1] };
  record('connect', { target });
  return originalConnect.apply(this, args);
};

const pathFns = [
  'access', 'accessSync', 'appendFile', 'appendFileSync', 'chmod', 'chmodSync', 'copyFile', 'copyFileSync',
  'cp', 'cpSync', 'createReadStream', 'createWriteStream', 'exists', 'existsSync', 'lstat', 'lstatSync',
  'mkdir', 'mkdirSync', 'mkdtemp', 'mkdtempSync', 'open', 'openSync', 'opendir', 'opendirSync', 'readFile',
  'readFileSync', 'readdir', 'readdirSync', 'readlink', 'readlinkSync', 'realpath', 'realpathSync', 'rename',
  'renameSync', 'rm', 'rmSync', 'rmdir', 'rmdirSync', 'stat', 'statSync', 'symlink', 'symlinkSync', 'truncate',
  'truncateSync', 'unlink', 'unlinkSync', 'utimes', 'utimesSync', 'watch', 'writeFile', 'writeFileSync',
];
const WRITES = /^(appendFile|chmod|copyFile|cp|createWriteStream|mkdir|mkdtemp|rename|rm|rmdir|symlink|truncate|unlink|utimes|writeFile)/;

function wrapPathFn(target, name, prefix) {
  const original = target[name];
  if (typeof original !== 'function')
    return;
  const wrapped = function (...args) {
    const first = args[0];
    let write = WRITES.test(name);
    if ((name === 'open' || name === 'openSync') && typeof args[1] === 'string' && /[wa+]/.test(args[1]))
      write = true;
    if ((name === 'open' || name === 'openSync') && typeof args[1] === 'number' && (args[1] & (fs.constants.O_WRONLY | fs.constants.O_RDWR | fs.constants.O_CREAT)))
      write = true;
    if (typeof first === 'string' || first instanceof URL || Buffer.isBuffer(first))
      record('fs', { fn: `${prefix}${name}`, path: String(first), write });
    return original.apply(this, args);
  };
  Object.defineProperty(wrapped, 'name', { value: name });
  if (original[require('util').promisify.custom])
    wrapped[require('util').promisify.custom] = original[require('util').promisify.custom];
  target[name] = wrapped;
}

for (const name of pathFns)
  wrapPathFn(fs, name, '');
for (const name of pathFns)
  wrapPathFn(fs.promises, name, 'promises.');
Module.syncBuiltinESMExports();
record('instrument_ready', { argv: process.argv.slice(1) });
