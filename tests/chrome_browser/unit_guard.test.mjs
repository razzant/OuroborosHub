// The JS client of ouroboros/mcp_browser_guard.py's private request callback.
import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import path from 'node:path';
import fs from 'node:fs';
import { scratchDir, socketDir } from './helpers/env.mjs';
import { startMockGuard, coreLikePolicy, OWNER_READONLY_SENTENCE } from './helpers/mock_guard.mjs';
import { retainingIntake, sha256 } from './helpers/mock_intake.mjs';
import { BrowserGuard, parseGuardEndpoint, takeGuardEnvironment, exchange, GUARD_MAX_LINE_BYTES, GUARD_ENV, GUARD_TOKEN_ENV, INTAKE_MAX_FILES, INTAKE_MAX_BYTES } from '../../skills/chrome_browser/lib/guard.mjs';

const TOKEN = 'a'.repeat(64);

test('endpoint forms: 127.0.0.1:<port>, absolute Unix socket path, Windows pipe; everything else refused', () => {
  assert.deepEqual(parseGuardEndpoint('127.0.0.1:41234'), { kind: 'tcp', host: '127.0.0.1', port: 41234 });
  assert.deepEqual(parseGuardEndpoint('/tmp/guard.sock'), { kind: 'path', path: '/tmp/guard.sock' });
  assert.deepEqual(parseGuardEndpoint('\\\\.\\pipe\\ouroboros-guard'), { kind: 'path', path: '\\\\.\\pipe\\ouroboros-guard' });
  for (const bad of ['localhost:1', '0.0.0.0:1', '127.0.0.1', '127.0.0.1:0', '127.0.0.1:70000', '10.0.0.1:80', '[::1]:80', 'guard.sock', '', 'http://127.0.0.1:1'])
    assert.throws(() => parseGuardEndpoint(bad), (error) => error.code === 'BROWSER_GUARD_INVALID', bad);
});

test('the guard environment is read once and removed from the process environment', () => {
  const env = { [GUARD_ENV]: '127.0.0.1:5555', [GUARD_TOKEN_ENV]: TOKEN, OTHER: 'x' };
  const taken = takeGuardEnvironment(env);
  assert.equal(taken.configured, true);
  assert.equal(taken.token, TOKEN);
  assert.deepEqual(env, { OTHER: 'x' });
  assert.deepEqual(takeGuardEnvironment({}), { configured: false, problem: 'BROWSER_GUARD_REQUIRED' });
  const invalid = takeGuardEnvironment({ [GUARD_ENV]: 'nonsense', [GUARD_TOKEN_ENV]: TOKEN });
  assert.deepEqual([invalid.configured, invalid.problem], [false, 'BROWSER_GUARD_INVALID']);
});

test('wire bytes: arm, request and evaluate lines have exactly the agreed keys in the agreed order', async () => {
  const guard = await startMockGuard({ token: TOKEN });
  const client = new BrowserGuard({ endpoint: parseGuardEndpoint(guard.endpoint), token: TOKEN });
  await client.arm({});
  await client.decideRequest({ url: 'http://127.0.0.1:9/x', method: 'GET', resourceType: 'document', ...BrowserGuard.bodyFacts(null) });
  await client.decideRequest({ url: 'http://127.0.0.1:9/api/settings', method: 'POST', resourceType: 'fetch', ...BrowserGuard.bodyFacts(Buffer.from('{"a":"ü"}')) });
  await client.checkEvaluate('http://127.0.0.1:9/x');
  assert.deepEqual(guard.received.map((entry) => entry.line), [
    `{"armed":true,"token":"${TOKEN}"}`,
    `{"url":"http://127.0.0.1:9/x","method":"GET","post_data":null,"resource_type":"document","body_present":false,"token":"${TOKEN}"}`,
    `{"url":"http://127.0.0.1:9/api/settings","method":"POST","post_data":"{\\"a\\":\\"ü\\"}","resource_type":"fetch","body_present":true,"token":"${TOKEN}"}`,
    `{"action":"evaluate","url":"http://127.0.0.1:9/x","token":"${TOKEN}"}`,
  ]);
  await guard.close();
});

test('body facts: empty, readable, non-UTF-8, withheld and partial bodies', () => {
  assert.deepEqual(BrowserGuard.bodyFacts(null), { postData: null, bodyPresent: false });
  assert.deepEqual(BrowserGuard.bodyFacts(Buffer.alloc(0)), { postData: null, bodyPresent: false });
  assert.deepEqual(BrowserGuard.bodyFacts(null, { 'content-length': '0' }), { postData: null, bodyPresent: false });
  assert.deepEqual(BrowserGuard.bodyFacts(Buffer.from('a=1&b=2')), { postData: 'a=1&b=2', bodyPresent: true });
  assert.deepEqual(BrowserGuard.bodyFacts(Buffer.from('﻿{"x":1}')), { postData: '﻿{"x":1}', bodyPresent: true }, 'a BOM is kept: exact text');
  assert.deepEqual(BrowserGuard.bodyFacts(Buffer.from([0x7b, 0xff, 0xfe, 0x7d])), { postData: null, bodyPresent: true }, 'not UTF-8: unreadable, not a lossy string');
  assert.deepEqual(BrowserGuard.bodyFacts(null, { 'content-length': '12' }), { postData: null, bodyPresent: true }, 'announced but withheld');
  assert.deepEqual(BrowserGuard.bodyFacts(null, { 'transfer-encoding': 'chunked' }), { postData: null, bodyPresent: true });
  assert.deepEqual(BrowserGuard.bodyFacts(Buffer.from('abc'), { 'content-length': '300' }), { postData: null, bodyPresent: true }, 'partial (file parts withheld)');
});

test('a line over 32 MiB is announced, not sent: post_data null, body_present true', () => {
  const big = 'x'.repeat(GUARD_MAX_LINE_BYTES);
  const line = BrowserGuard.requestMessage({ url: 'http://h/api/settings', method: 'POST', postData: big, resourceType: 'fetch', bodyPresent: true }, TOKEN);
  assert.equal(line, `{"url":"http://h/api/settings","method":"POST","post_data":null,"resource_type":"fetch","body_present":true,"token":"${TOKEN}"}\n`);
  const fits = BrowserGuard.requestMessage({ url: 'http://h/', method: 'POST', postData: 'y'.repeat(1024), resourceType: 'fetch', bodyPresent: true }, TOKEN);
  assert.ok(fits.includes('"post_data":"yyy'));
});

test('core-like decisions through the real client: GET allowed, empty POST elsewhere allowed, owner POSTs refused with and without a readable body', async () => {
  const owner = 'http://127.0.0.1:8765';
  const guard = await startMockGuard({ token: TOKEN, decide: coreLikePolicy({ ownerOrigins: [owner] }) });
  const client = new BrowserGuard({ endpoint: parseGuardEndpoint(guard.endpoint), token: TOKEN });
  const decide = (url, method, buffer, headers) => client.decideRequest({ url, method, resourceType: 'fetch', ...BrowserGuard.bodyFacts(buffer, headers) });
  assert.equal(await decide(`${owner}/`, 'GET', null), '');
  assert.equal(await decide('http://127.0.0.1:9999/api/settings', 'POST', null), '', 'unrelated service on the same path');
  assert.equal(await decide(`${owner}/api/settings`, 'POST', null), '', 'a genuinely empty POST is an empty body');
  assert.match(await decide(`${owner}/api/settings`, 'POST', Buffer.from('{"OUROBOROS_REVIEW_ENFORCEMENT":"off"}')), /^BROWSER_OWNER_CONTROL_BLOCKED/);
  assert.match(await decide(`${owner}/api/decisions`, 'POST', Buffer.from([0xff, 0xfe])), /^BROWSER_OWNER_CONTROL_BLOCKED/, 'unreadable body');
  assert.match(await decide(`${owner}/api/settings`, 'POST', null, { 'content-length': '40' }), /^BROWSER_OWNER_CONTROL_BLOCKED/, 'withheld body');
  assert.deepEqual(client.counters, { allowed: 3, blocked: 3, failed_closed: 0 });
  await guard.close();
});

test('evaluate refusal is surfaced verbatim', async () => {
  const guard = await startMockGuard({ token: TOKEN, decide: coreLikePolicy({ evaluateBlock: OWNER_READONLY_SENTENCE }) });
  const client = new BrowserGuard({ endpoint: parseGuardEndpoint(guard.endpoint), token: TOKEN });
  assert.equal(await client.checkEvaluate('http://x/'), OWNER_READONLY_SENTENCE);
  await guard.close();
});

test('arming: only {"armed": true} arms, and only the context it was called for', async () => {
  const ok = await startMockGuard({ token: TOKEN });
  const contextA = {};
  const contextB = {};
  const client = new BrowserGuard({ endpoint: parseGuardEndpoint(ok.endpoint), token: TOKEN });
  assert.equal(client.isArmed(contextA), false);
  await client.arm(contextA);
  assert.equal(client.isArmed(contextA), true);
  assert.equal(client.isArmed(contextB), false);
  const wrongToken = new BrowserGuard({ endpoint: parseGuardEndpoint(ok.endpoint), token: 'b'.repeat(64) });
  await assert.rejects(wrongToken.arm(contextB), (error) => error.code === 'BROWSER_GUARD_NOT_ARMED' && /BROWSER_GUARD_TOKEN_INVALID/.test(error.message));
  assert.equal(wrongToken.isArmed(contextB), false);
  await ok.close();
  for (const answer of [{ armed: 'true' }, { armed: 1 }, {}, [true], { armed: false, block: 'BROWSER_SESSION_CLOSED: closing' }]) {
    const guard = await startMockGuard({ token: TOKEN, decide: () => answer });
    const c = new BrowserGuard({ endpoint: parseGuardEndpoint(guard.endpoint), token: TOKEN });
    await assert.rejects(c.arm({}), (error) => error.code === 'BROWSER_GUARD_NOT_ARMED', JSON.stringify(answer));
    await guard.close();
  }
});

test('fail closed: refused connection, silence, malformed, non-string block, oversized answer', async () => {
  const closedPort = await new Promise((resolve) => {
    const server = net.createServer();
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
  const request = { url: 'http://x/', method: 'GET', resourceType: 'document', postData: null, bodyPresent: false };
  const dead = new BrowserGuard({ endpoint: { kind: 'tcp', host: '127.0.0.1', port: closedPort }, token: TOKEN });
  assert.match(await dead.decideRequest(request), /^BROWSER_GUARD_UNAVAILABLE/);
  assert.match(await dead.checkEvaluate('http://x/'), /^BROWSER_GUARD_UNAVAILABLE/);
  const silent = await startMockGuard({ token: TOKEN, respond: false });
  const slow = new BrowserGuard({ endpoint: parseGuardEndpoint(silent.endpoint), token: TOKEN, timeoutMs: 300 });
  assert.match(await slow.decideRequest(request), /^BROWSER_GUARD_TIMEOUT/);
  await silent.close();
  for (const raw of ['not json\n', '{"block": 0}\n', '{"allow": true}\n', '{"block": null}\n', `{"block": "${'z'.repeat(70 * 1024)}"}\n`]) {
    const guard = await startMockGuard({ token: TOKEN, decide: () => raw });
    const client = new BrowserGuard({ endpoint: parseGuardEndpoint(guard.endpoint), token: TOKEN });
    const block = await client.decideRequest(request);
    assert.ok(block.length > 0, `must block on ${raw.slice(0, 30)}`);
    assert.equal(client.counters.failed_closed, 1);
    await guard.close();
  }
});

test('Unix socket endpoint works the same way (tests only; the host uses TCP)', async () => {
  const dir = socketDir();
  const socketPath = path.join(dir, 'g.sock');
  const guard = await startMockGuard({ token: TOKEN, socketPath });
  const answer = await exchange(parseGuardEndpoint(socketPath), { armed: true, token: TOKEN });
  assert.deepEqual(answer, { armed: true });
  await guard.close();
  fs.rmSync(dir, { recursive: true, force: true });
});

// The host's intake and the client: one decide function for both kinds of message the tests send.
const intakeGuard = async (intake) => await startMockGuard({ token: TOKEN, decide: (message) => ('armed' in message ? { armed: true } : message.action === 'file_intake' ? intake(message) : { block: '' }) });

test('file intake: the agreed wire line, and exactly the retained copies, in the order named, with their original names', async () => {
  const work = scratchDir('intake-work');
  const kept = scratchDir('intake-kept');
  const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 1, 2, 3]);
  fs.writeFileSync(path.join(work, 'photo one.png'), png);
  fs.writeFileSync(path.join(work, 'notes.txt'), 'notes');
  fs.writeFileSync(path.join(work, 'empty'), '');
  const intake = retainingIntake({ dir: kept, roots: { work } });
  const guard = await intakeGuard(intake);
  const client = new BrowserGuard({ endpoint: parseGuardEndpoint(guard.endpoint), token: TOKEN });
  const answer = await client.intakeFiles([{ path: path.join(work, 'photo one.png') }, { path: 'notes.txt', root: 'work' }, { path: 'empty', root: 'work' }]);
  const photo = JSON.stringify(path.join(work, 'photo one.png'));
  assert.deepEqual(guard.received.map((entry) => entry.line), [
    `{"action":"file_intake","files":[{"path":${photo}},{"path":"notes.txt","root":"work"},{"path":"empty","root":"work"}],"token":"${TOKEN}"}`,
  ], 'one message: the paths as named, a root only where one was given');
  assert.equal(answer.block, '');
  assert.deepEqual(answer.files.map(({ name, size, sha256: hash }) => [name, size, hash]), [
    ['photo one.png', png.length, sha256(png)], ['notes.txt', 5, sha256(Buffer.from('notes'))], ['empty', 0, sha256(Buffer.alloc(0))],
  ]);
  assert.ok(answer.files[0].buffer.equals(png) && answer.files[1].buffer.equals(Buffer.from('notes')) && answer.files[2].buffer.length === 0);
  assert.deepEqual(Object.keys(answer.files[0]).sort(), ['buffer', 'name', 'sha256', 'size'], 'no path leaves the client');
  await guard.close();
});

test('file intake reads only the copy the host names, never the path the agent named', async (t) => {
  const kept = scratchDir('intake-copy');
  const copy = path.join(kept, 'copy-1');
  fs.writeFileSync(copy, 'the retained bytes');
  const named = path.join(kept, 'named.txt');
  fs.writeFileSync(named, 'the bytes at the named path, changed after retention');
  const guard = await intakeGuard(() => ({ block: '', files: [{ path: copy, name: 'named.txt', size: 18, sha256: sha256(Buffer.from('the retained bytes')) }] }));
  const client = new BrowserGuard({ endpoint: parseGuardEndpoint(guard.endpoint), token: TOKEN });
  const opened = t.mock.method(fs.promises, 'open');
  const answer = await client.intakeFiles([{ path: named }]);
  assert.equal(answer.block, '');
  assert.deepEqual([answer.files[0].name, answer.files[0].buffer.toString()], ['named.txt', 'the retained bytes']);
  assert.deepEqual(opened.mock.calls.map((call) => call.arguments[0]), [copy]);
  await guard.close();
});

test('file intake refusals: the host\'s reason verbatim, and a request out of bounds is never sent', async () => {
  const kept = scratchDir('intake-refused');
  const intake = retainingIntake({ dir: kept, admit: (entry) => (entry.path.includes('secret') ? 'READ_BLOCKED: this task may not read that file' : '') });
  const guard = await intakeGuard(intake);
  const client = new BrowserGuard({ endpoint: parseGuardEndpoint(guard.endpoint), token: TOKEN });
  assert.deepEqual(await client.intakeFiles([{ path: '/x/secret.txt' }]), { block: 'READ_BLOCKED: this task may not read that file' });
  const sent = guard.received.length;
  for (const [entries, why] of [
    [Array.from({ length: INTAKE_MAX_FILES + 1 }, (_, index) => ({ path: `/x/${index}` })), /17 files were named; at most 16/],
    [[], /no file was named/], [undefined, /no file was named/],
    [[{ path: '' }], /non-empty path/], [[{ path: 'a\0b' }], /non-empty path/], [[{ path: 'a', root: '' }], /non-empty path/], [[{ path: 3 }], /non-empty path/],
  ]) {
    const refused = await client.intakeFiles(entries);
    assert.match(refused.block, /^BROWSER_FILE_INTAKE_INVALID: /);
    assert.match(refused.block, why);
    assert.equal(refused.files, undefined);
  }
  assert.equal(guard.received.length, sent, 'nothing out of bounds reached the host');
  await guard.close();
});

test('file intake fails closed: an answer out of the agreed form, or a copy that is not what the host stated, gives the page nothing', async (t) => {
  const kept = scratchDir('intake-forms');
  const bytes = Buffer.from('retained bytes');
  const copy = path.join(kept, 'copy');
  fs.writeFileSync(copy, bytes);
  const good = { path: copy, name: 'a.txt', size: bytes.length, sha256: sha256(bytes) };
  fs.symlinkSync(copy, path.join(kept, 'link'));
  fs.mkdirSync(path.join(kept, 'folder'));
  const protocol = [
    { block: '' }, { block: '', files: [] }, { block: '', files: [good, good] }, { block: '', files: 'x' },
    { block: '', files: [{ ...good, path: 'relative/copy' }] }, { block: '', files: [{ ...good, path: '' }] },
    { block: '', files: [{ ...good, name: 'dir/a.txt' }] }, { block: '', files: [{ ...good, name: 'dir\\a.txt' }] }, { block: '', files: [{ ...good, name: '..' }] },
    { block: '', files: [{ ...good, name: 'a\n.txt' }] }, { block: '', files: [{ ...good, name: '' }] },
    { block: '', files: [{ ...good, size: -1 }] }, { block: '', files: [{ ...good, size: 1.5 }] }, { block: '', files: [{ ...good, size: '14' }] },
    { block: '', files: [{ ...good, sha256: good.sha256.toUpperCase() }] }, { block: '', files: [{ ...good, sha256: good.sha256.slice(1) }] },
    { block: '', files: [null] }, { allow: true, files: [good] }, { block: null, files: [good] },
  ];
  const mismatch = [
    [{ ...good, size: bytes.length + 1 }, /has 14 bytes, not the 15/], [{ ...good, sha256: sha256(Buffer.from('other')) }, /does not have the sha256/],
    [{ ...good, path: path.join(kept, 'missing') }, /could not be read \(ENOENT\)/], [{ ...good, path: path.join(kept, 'folder') }, /not a regular file|could not be read/],
    ...(process.platform === 'win32' ? [] : [[{ ...good, path: path.join(kept, 'link') }, /could not be read \(ELOOP\)/]]),
  ];
  let answer = null;
  const guard = await startMockGuard({ token: TOKEN, decide: () => answer });
  const client = new BrowserGuard({ endpoint: parseGuardEndpoint(guard.endpoint), token: TOKEN });
  const reads = t.mock.method(fs.promises, 'open');
  for (const form of protocol) {
    answer = form;
    const refused = await client.intakeFiles([{ path: '/x/a.txt' }]);
    assert.match(refused.block, /^BROWSER_GUARD_PROTOCOL: /, JSON.stringify(form));
    assert.equal(refused.files, undefined);
  }
  // Over the agreed total, refused before any file is opened.
  answer = { block: '', files: [{ ...good, size: INTAKE_MAX_BYTES }, { ...good, size: 1 }] };
  assert.match((await client.intakeFiles([{ path: '/x/a' }, { path: '/x/b' }])).block, new RegExp(`^BROWSER_GUARD_PROTOCOL: .*${INTAKE_MAX_BYTES + 1} bytes, more than the ${INTAKE_MAX_BYTES} agreed`));
  assert.equal(reads.mock.callCount(), 0, 'no file is opened for an answer out of form');
  for (const [file, why] of mismatch) {
    answer = { block: '', files: [good, file] };
    const refused = await client.intakeFiles([{ path: '/x/a' }, { path: '/x/b' }]);
    assert.match(refused.block, /^BROWSER_FILE_INTAKE_MISMATCH: the host's retained copy of file 2 /);
    assert.match(refused.block, why);
    assert.equal(refused.files, undefined, 'the first, good file is not given alone');
    assert.ok(!refused.block.includes(kept) && !refused.block.includes(TOKEN), 'no path or token in the reason');
  }
  // A refusal carries no files, even if the answer lists some.
  answer = { block: 'READ_BLOCKED: no', files: [good] };
  assert.deepEqual(await client.intakeFiles([{ path: '/x/a' }]), { block: 'READ_BLOCKED: no' });
  await guard.close();
  const silent = await startMockGuard({ token: TOKEN, respond: false });
  const slow = new BrowserGuard({ endpoint: parseGuardEndpoint(silent.endpoint), token: TOKEN, timeoutMs: 300 });
  assert.match((await slow.intakeFiles([{ path: '/x/a' }])).block, /^BROWSER_GUARD_TIMEOUT: the request guard did not answer the file intake, so no file was given to the page$/);
  await silent.close();
});
