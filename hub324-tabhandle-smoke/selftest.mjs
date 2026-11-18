#!/usr/bin/env node
// Browser-free checks of the harness's own logic, run before the browser step (bin/run.sh): the PNG reader, the
// picture code (also after downscaling), the result and link parsers, the switch guard rail, the stand-in guard and the
// fixture's page, push and report path. No Chrome, no skill code. Exit 0 only if every check passed.
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import fs from 'node:fs';
import path from 'node:path';
import { random32, pageHtml, startFixture } from './lib/fixture.mjs';
import { startGuard } from './lib/guard_standin.mjs';
import { codeOf, forbiddenFlags, parseConnectLink, redact, tabsWithHandles } from './lib/parse.mjs';
import { decodePng, encodePng } from './lib/png.mjs';
import { cellsFor, COLUMNS, decodeGrid, GRID_HEIGHT, hex32, PALETTE, ROWS } from './lib/visual_code.mjs';

const results = [];
async function test(name, body) {
  try {
    await body();
    results.push({ name, ok: true });
  } catch (error) {
    results.push({ name, ok: false, error: String(error?.stack || error) });
  }
}

// The page layout of fixture.mjs, painted into RGBA pixels: grid on top, grey below, A's white draft box.
function render(role, value, width, height) {
  const rgba = new Uint8Array(width * height * 4);
  const cells = cellsFor(role, value);
  const gridHeight = height * GRID_HEIGHT;
  const grey = [58, 58, 58];
  const white = [255, 255, 255];
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      let colour = grey;
      if (y < gridHeight)
        colour = PALETTE[cells[Math.min(ROWS - 1, Math.floor(y / (gridHeight / ROWS))) * COLUMNS + Math.min(COLUMNS - 1, Math.floor(x / (width / COLUMNS)))]];
      else if (role === 'A' && y >= height * 0.65 && y < height * 0.97 && x >= width * 0.02 && x < width * 0.98)
        colour = white;
      const o = (y * width + x) * 4;
      rgba[o] = colour[0];
      rgba[o + 1] = colour[1];
      rgba[o + 2] = colour[2];
      rgba[o + 3] = 255;
    }
  }
  return rgba;
}

// Polls a condition the harness side observes (the fixture's own waitUntil re-checks only on server events).
async function waitFor(probe, timeoutMs, what) {
  const end = Date.now() + timeoutMs;
  while (!probe()) {
    if (Date.now() > end)
      throw new Error(`timed out waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

function rgbOnly(rgba) {
  const rgb = new Uint8Array((rgba.length / 4) * 3);
  for (let i = 0, j = 0; i < rgba.length; i += 4, j += 3)
    rgb.set([rgba[i], rgba[i + 1], rgba[i + 2]], j);
  return rgb;
}

// Bilinear downscale, as an image library would when it fits a capture into a message.
function scale(rgba, width, height, factor) {
  const w = Math.max(1, Math.floor(width * factor));
  const h = Math.max(1, Math.floor(height * factor));
  const out = new Uint8Array(w * h * 4);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const sx = Math.min(width - 1.001, (x + 0.5) / factor - 0.5);
      const sy = Math.min(height - 1.001, (y + 0.5) / factor - 0.5);
      const x0 = Math.max(0, Math.floor(sx));
      const y0 = Math.max(0, Math.floor(sy));
      const fx = Math.max(0, sx - x0);
      const fy = Math.max(0, sy - y0);
      for (let k = 0; k < 4; k++) {
        const at = (xx, yy) => rgba[(yy * width + xx) * 4 + k];
        const top = at(x0, y0) * (1 - fx) + at(x0 + 1, y0) * fx;
        const bottom = at(x0, y0 + 1) * (1 - fx) + at(x0 + 1, y0 + 1) * fx;
        out[(y * w + x) * 4 + k] = Math.round(top * (1 - fy) + bottom * fy);
      }
    }
  }
  return { width: w, height: h, rgba: out };
}

await test('PNG: RGBA and RGB round trip through every scanline filter', () => {
  const width = 37;
  const height = 23;
  const rgba = new Uint8Array(width * height * 4);
  for (let i = 0; i < rgba.length; i++)
    rgba[i] = (i * 7919 + (i >> 3) * 31) & 0xff;
  for (let i = 3; i < rgba.length; i += 4)
    rgba[i] = 255;
  const decoded = decodePng(encodePng(width, height, rgba, { filters: [0, 1, 2, 3, 4] }));
  assert.equal(decoded.width, width);
  assert.equal(decoded.height, height);
  assert.deepEqual(Buffer.from(decoded.rgba), Buffer.from(rgba));
  const fromRgb = decodePng(encodePng(width, height, rgbOnly(rgba), { channels: 3, filters: [4, 3, 2, 1, 0] }));
  assert.deepEqual(Buffer.from(fromRgb.rgba), Buffer.from(rgba));
});

await test('PNG: a damaged file is refused, not misread', () => {
  const png = encodePng(4, 4, new Uint8Array(64).fill(200));
  const damaged = Buffer.from(png);
  damaged[40] ^= 0xff;
  assert.throws(() => decodePng(damaged), /CRC|truncated|IDAT|data/);
  assert.throws(() => decodePng(Buffer.from('not an image')), /not a PNG/);
});

await test('picture code: each role and value reads back exactly, at capture sizes', () => {
  for (const [width, height] of [[1280, 838], [801, 597]]) {
    for (const role of ['A', 'B']) {
      for (const value of [0, 0xffffffff, random32()]) {
        const decoded = decodeGrid(decodePng(encodePng(width, height, render(role, value, width, height), { filters: [1, 4] })));
        assert.equal(decoded.ok, true, `${role} ${hex32(value)} at ${width}x${height}`);
        assert.equal(decoded.role, role);
        assert.equal(decoded.value, value >>> 0);
        assert.equal(decoded.unreadable, 0);
      }
    }
  }
});

await test('picture code: survives the downscaling upstream may apply to a large capture', () => {
  for (const factor of [0.79, 0.5, 0.33]) {
    const value = random32();
    const image = scale(render('B', value, 1920, 1000), 1920, 1000, factor);
    const decoded = decodeGrid(image);
    assert.equal(decoded.ok, true, `factor ${factor}`);
    assert.equal(decoded.role, 'B');
    assert.equal(decoded.value, value >>> 0);
  }
});

await test('picture code: a blank, foreign or mixed picture is not read as a tab', () => {
  const width = 640;
  const height = 400;
  const blank = decodeGrid({ width, height, rgba: new Uint8Array(width * height * 4).fill(255) });
  assert.equal(blank.ok, false);
  assert.equal(blank.role, null);
  // A's identity row over B's value and check: the check byte is bound to the role, so this is no tab's picture.
  const a = render('A', 0x0badf00d, width, height);
  const b = render('B', 0x0badf00d, width, height);
  const rowHeight = Math.ceil((height * GRID_HEIGHT) / ROWS);
  const mixed = Uint8Array.from(b);
  mixed.set(a.subarray(0, rowHeight * width * 4), 0);
  const decoded = decodeGrid({ width, height, rgba: mixed });
  assert.equal(decoded.role, 'A');
  assert.equal(decoded.checkOk, false);
  assert.equal(decoded.ok, false);
  assert.equal(decodeGrid({ width: 10, height: 10, rgba: new Uint8Array(400) }).ok, false);
});

await test('picture code: the copy written into the pages is the same function', () => {
  const copy = new Function(`return (${cellsFor.toString()});`)();
  for (const role of ['A', 'B']) {
    for (const value of [1, 0xdeadbeef, random32()])
      assert.deepEqual(copy(role, value), cellsFor(role, value));
  }
  assert.equal(cellsFor('A', 1).length, COLUMNS * ROWS);
  const html = pageHtml('A', 'abc123', 7);
  assert.match(html, /<title>Fixture A abc123<\/title>/);
  assert.match(html, /<textarea id="draft" autofocus/);
  assert.doesNotMatch(pageHtml('B', 'abc123', 7), /textarea/);
});

await test('parsers: tab lists, handles and the approval link', () => {
  const text = [
    '### Result',
    '- 0: (current) [Fixture A r1](http://127.0.0.1:41234/r/r1/a)',
    '- 1: [Fixture B r1](http://127.0.0.1:41234/r/r1/b)',
    'Tab handles for browser_take_screenshot "tabHandle", by the indexes above:',
    `- 0: tabHandle tab-${'a'.repeat(32)}`,
    `- 1: tabHandle tab-${'0123456789abcdef'.repeat(2)}`,
  ].join('\n');
  const tabs = tabsWithHandles(text);
  assert.equal(tabs.length, 2);
  assert.deepEqual(tabs[0], { index: 0, current: true, title: 'Fixture A r1', url: 'http://127.0.0.1:41234/r/r1/a', handle: `tab-${'a'.repeat(32)}` });
  assert.equal(tabs[1].current, false);
  assert.equal(tabs[1].handle, `tab-${'0123456789abcdef'.repeat(2)}`);
  const id = 'mmlmfjhmonkocbjadbfplnigmagldckm';
  const uuid = '0f8fad5b-d9cb-469f-a165-70867728950e';
  const link = `chrome-extension://${id}/connect.html?mcpRelayUrl=ws%3A%2F%2F%5B%3A%3A1%5D%3A41235%2Fextension%2F${uuid}&client=%7B%22name%22%3A%22Ouroboros%22%7D&protocolVersion=2`;
  const parsed = parseConnectLink(`### Approval needed\nGive the owner this one-time link, only in this conversation:\n${link}\nThey open...`, id);
  assert.equal(parsed.href, link);
  assert.deepEqual(parsed.client, { name: 'Ouroboros' });
  assert.throws(() => parseConnectLink(`${link}&token=abc`, id), /bypass token/);
  assert.throws(() => parseConnectLink(link.replace('protocolVersion=2', 'protocolVersion=1'), id), /protocol 2/);
  assert.throws(() => parseConnectLink(link.replace('%5B%3A%3A1%5D', 'example.com'), id), /loopback/);
  assert.throws(() => parseConnectLink(link, 'a'.repeat(32)), /connect page/);
  // Redaction (local diagnostics): the raw and the percent-encoded relay path, the whole query of a connect page.
  assert.equal(redact(`ws://[::1]:41235/extension/${uuid}`), 'ws://[::1]:41235/extension/<redacted>');
  assert.equal(redact(`relay ws%3A%2F%2F%5B%3A%3A1%5D%3A41235%2Fextension%2F${uuid} end`), 'relay ws%3A%2F%2F%5B%3A%3A1%5D%3A41235%2Fextension%2F<redacted> end');
  assert.equal(redact(`open ${link} now`), `open chrome-extension://${id}/connect.html?<redacted> now`);
  assert.equal(redact(`{"url":"${link}"}`), `{"url":"chrome-extension://${id}/connect.html?<redacted>"}`);
  assert.equal(redact(`${link}`).includes(uuid), false);
  assert.equal(redact('PLAYWRIGHT_MCP_EXTENSION_TOKEN=secretvalue x'), 'PLAYWRIGHT_MCP_EXTENSION_TOKEN=<redacted> x');
  assert.equal(redact('PLAYWRIGHT_MCP_EXTENSION_TOKEN%3Dsecretvalue&x'), 'PLAYWRIGHT_MCP_EXTENSION_TOKEN%3D<redacted>&x');
  // Only error codes of a tool's text are published.
  assert.equal(codeOf('### Error\nBROWSER_TAB_HANDLE_UNKNOWN: "tabHandle" names no open tab'), 'BROWSER_TAB_HANDLE_UNKNOWN');
  assert.equal(codeOf('CHROME_BROWSER_PIN_MISMATCH: x'), 'CHROME_BROWSER_PIN_MISMATCH');
  assert.equal(codeOf('### Result\n- 0: (current) [Fixture A](http://127.0.0.1:1/r/x/a)'), null);
});

await test('guard rail: switches that weaken Chrome are recognised', () => {
  assert.deepEqual(forbiddenFlags(['--no-first-run', '--load-extension=/x', '--remote-debugging-pipe']), []);
  assert.deepEqual(forbiddenFlags(['--no-sandbox', '--headless=new', '--disable-features=X', '--remote-debugging-port=0', '--silent-debugger-extension-api']),
      ['--no-sandbox', '--headless=new', '--disable-features=X', '--remote-debugging-port=0', '--silent-debugger-extension-api']);
});

const ask = (endpoint, message) => new Promise((resolve, reject) => {
  const [host, port] = endpoint.split(':');
  const socket = net.connect({ host, port: Number(port) }, () => socket.write(`${JSON.stringify(message)}\n`));
  let data = '';
  socket.on('data', (part) => {
    data += part;
  });
  socket.on('end', () => resolve(JSON.parse(data)));
  socket.on('error', reject);
});

await test('stand-in guard: arms, allows only the fixture origin, refuses everything else', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hub324-selftest-'));
  const token = 'f'.repeat(64);
  const guard = await startGuard({ token, allowOrigin: 'http://127.0.0.1:9', logFile: path.join(dir, 'guard.jsonl') });
  try {
    assert.deepEqual(await ask(guard.endpoint, { armed: true, token }), { armed: true, capabilities: { embedded_resources: true } });
    assert.equal((await ask(guard.endpoint, { armed: true, token: 'e'.repeat(64) })).armed, false);
    assert.deepEqual(await ask(guard.endpoint, { url: 'http://127.0.0.1:9/r/x/a', method: 'GET', post_data: null, resource_type: 'document', body_present: false, token }), { block: '' });
    assert.deepEqual(await ask(guard.endpoint, { url: 'about:blank', method: 'GET', post_data: null, resource_type: 'document', body_present: false, token }), { block: '' });
    for (const url of ['http://127.0.0.1:10/', 'https://example.com/', 'data:text/html,x', 'file:///etc/passwd', 'javascript:1'])
      assert.match((await ask(guard.endpoint, { url, method: 'GET', post_data: null, resource_type: 'document', body_present: false, token })).block, /SMOKE_GUARD_REFUSED/);
    assert.match((await ask(guard.endpoint, { url: 'http://127.0.0.1:9/', method: 'GET', token: 'e'.repeat(64) })).block, /SMOKE_GUARD_TOKEN/);
    assert.match((await ask(guard.endpoint, { action: 'file_intake', files: [{ path: '/etc/hosts' }], token })).block, /NO_FILE_INTAKE/);
    assert.match((await ask(guard.endpoint, { action: 'capabilities', token })).block, /UNKNOWN_ACTION/);
    assert.equal((await ask(guard.endpoint, { action: 'evaluate', url: 'http://127.0.0.1:9/r/x/a', token })).block, '');
    const logged = fs.readFileSync(path.join(dir, 'guard.jsonl'), 'utf8');
    assert.equal(logged.includes(token), false, 'the token is never logged');
  } finally {
    await guard.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

await test('fixture: serves both pages, pushes a command over SSE, takes a report', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hub324-selftest-'));
  const fixture = await startFixture({ run: 'selftest1', logFile: path.join(dir, 'fixture.jsonl') });
  let stream = null;
  try {
    const page = await fetch(fixture.urlA);
    assert.equal(page.status, 200);
    assert.match(await page.text(), /Fixture A selftest1/);
    assert.equal((await fetch(`${fixture.origin}/elsewhere`)).status, 404);
    const received = [];
    stream = await new Promise((resolve, reject) => {
      const request = http.get(`${fixture.origin}/r/selftest1/events?role=B&instance=i1`, (response) => {
        response.setEncoding('utf8');
        response.on('data', (part) => received.push(part));
        resolve(request);
      });
      request.on('error', reject);
    });
    await fixture.waitUntil(() => fixture.listening('B', 'i1'), 5_000, 'the SSE client');
    const seq = fixture.push('B', { cmd: 'paint', value: 42 });
    await waitFor(() => received.join('').includes(`"seq":${seq}`), 5_000, 'the pushed command');
    assert.match(received.join(''), /"cmd":"paint","value":42/);
    const posted = await fetch(`${fixture.origin}/r/selftest1/report`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ run: 'selftest1', role: 'B', instance: 'i1', kind: 'painted', seq, value: 42, state: { t: Date.now() }, events: [{ i: 0, kind: 'paint', t: 1 }, { i: 1, kind: 'visibility', vis: 'hidden', t: 2 }] }),
    });
    assert.equal(posted.status, 204);
    const report = await fixture.waitUntil(() => fixture.reports.find((r) => r.seq === seq), 5_000, 'the report');
    assert.equal(report.value, 42);
    assert.deepEqual(fixture.events('B').map((event) => event.kind), ['paint', 'visibility']);
    assert.equal((await fetch(`${fixture.origin}/r/selftest1/report`, { method: 'POST', body: '{"run":"other"}' })).status, 400);
    assert.throws(() => fixture.push('A', { cmd: 'report' }), /no A page/);
  } finally {
    stream?.destroy();
    await fixture.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

const failed = results.filter((result) => !result.ok);
for (const result of results)
  process.stdout.write(`${result.ok ? 'ok  ' : 'FAIL'} ${result.name}${result.ok ? '' : `\n${result.error}`}\n`);
process.stdout.write(`${results.length - failed.length}/${results.length} self-checks passed\n`);
process.exitCode = failed.length ? 1 : 0;
