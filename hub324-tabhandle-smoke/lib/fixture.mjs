// The two synthetic pages and their loopback server.
//
// Tab A is the owner's: a draft textarea under the grid (see visual_code.mjs). Tab B is the background tab. Each page
// keeps its own log of focus, visibility, pointer, key and paint events and sends it with every report. The harness
// talks to a page only through this server: it pushes commands over Server-Sent Events ("paint this value", "report",
// "focus the draft") and the page answers with a POST. The harness never runs script in A or B.
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import { cellsFor, PALETTE } from './visual_code.mjs';

export const random32 = () => crypto.randomBytes(4).readUInt32BE(0);

const MAX_REPORT_BYTES = 4 * 1024 * 1024;

export function pageHtml(role, run, initialValue) {
  const title = `Fixture ${role} ${run}`;
  const draftCss = role === 'A'
    ? '#draft{position:fixed;left:2vw;top:65vh;width:96vw;height:32vh;box-sizing:border-box;margin:0;padding:8px;font:20px/1.3 monospace;resize:none;background:#fff;color:#000;border:3px solid #999;outline:none}'
    : '';
  const draftHtml = role === 'A' ? '<textarea id="draft" autofocus spellcheck="false" autocomplete="off" aria-label="Draft"></textarea>' : '';
  const script = `
"use strict";
const ROLE = ${JSON.stringify(role)};
const RUN = ${JSON.stringify(run)};
const PALETTE = ${JSON.stringify(PALETTE)};
const cellsFor = ${cellsFor.toString()};
const instance = crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
const draft = document.getElementById('draft');
const log = [];
let painted = null;
let sent = 0;
const idOf = (node) => (node ? node.id || node.nodeName : null);
function note(kind, extra) {
  log.push(Object.assign({ i: log.length, kind, t: Date.now(), vis: document.visibilityState, focus: document.hasFocus(), active: idOf(document.activeElement) }, extra || {}));
}
function paint(value) {
  const grid = document.getElementById('grid');
  const cells = cellsFor(ROLE, value);
  while (grid.children.length < cells.length) grid.appendChild(document.createElement('div'));
  cells.forEach((name, index) => {
    const c = PALETTE[name];
    grid.children[index].style.backgroundColor = 'rgb(' + c[0] + ', ' + c[1] + ', ' + c[2] + ')';
  });
  painted = value >>> 0;
}
function state() {
  const rect = draft ? draft.getBoundingClientRect() : null;
  return {
    role: ROLE, instance, url: location.href, title: document.title, painted,
    visibility: document.visibilityState, hasFocus: document.hasFocus(), active: idOf(document.activeElement),
    draft: draft ? { value: draft.value, selectionStart: draft.selectionStart, selectionEnd: draft.selectionEnd,
      rect: { left: rect.left, top: rect.top, width: rect.width, height: rect.height } } : null,
    screen: { screenX: window.screenX, screenY: window.screenY, outerWidth: window.outerWidth, outerHeight: window.outerHeight,
      innerWidth: window.innerWidth, innerHeight: window.innerHeight, devicePixelRatio: window.devicePixelRatio },
    t: Date.now(),
  };
}
async function report(kind, extra) {
  const from = sent;
  const events = log.slice(from);
  sent = log.length;
  const body = JSON.stringify(Object.assign({ role: ROLE, run: RUN, instance, kind, state: state(), events }, extra || {}));
  try {
    const response = await fetch('/r/' + RUN + '/report', { method: 'POST', headers: { 'content-type': 'application/json' }, body, cache: 'no-store' });
    if (!response.ok) throw new Error('status ' + response.status);
  } catch (error) {
    sent = Math.min(sent, from);
    note('report-failed', { error: String((error && error.message) || error) });
  }
}
window.addEventListener('focus', () => note('window-focus'));
window.addEventListener('blur', () => note('window-blur'));
document.addEventListener('visibilitychange', () => note('visibility'));
document.addEventListener('focusin', (event) => note('focusin', { target: idOf(event.target) }));
document.addEventListener('focusout', (event) => note('focusout', { target: idOf(event.target) }));
window.addEventListener('pagehide', (event) => note('pagehide', { persisted: event.persisted }));
window.addEventListener('pageshow', (event) => note('pageshow', { persisted: event.persisted }));
window.addEventListener('resize', () => note('resize', { w: window.innerWidth, h: window.innerHeight }));
document.addEventListener('keydown', (event) => note('keydown', { key: event.key, trusted: event.isTrusted, target: idOf(event.target) }), true);
document.addEventListener('pointerdown', (event) => note('pointerdown', { trusted: event.isTrusted, target: idOf(event.target), x: event.clientX, y: event.clientY }), true);
if (draft) draft.addEventListener('input', () => note('input', { length: draft.value.length }));
paint(${initialValue >>> 0});
note('load');
const source = new EventSource('/r/' + RUN + '/events?role=' + ROLE + '&instance=' + instance);
source.addEventListener('open', () => note('sse-open'));
source.addEventListener('error', () => note('sse-error'));
source.addEventListener('message', (message) => {
  let command;
  try { command = JSON.parse(message.data); } catch (error) { return; }
  if (command.cmd === 'paint') {
    paint(command.value);
    const probe = getComputedStyle(document.getElementById('grid').children[8]).backgroundColor;
    note('paint', { value: painted, seq: command.seq });
    report('painted', { seq: command.seq, value: painted, probe });
  } else if (command.cmd === 'report') {
    report('state', { seq: command.seq });
  } else if (command.cmd === 'focus-draft' && draft) {
    draft.focus();
    note('focus-draft', { seq: command.seq });
    report('state', { seq: command.seq });
  }
});
report('load');
`;
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>${title}</title>
<style>
html,body{margin:0;height:100%;overflow:hidden;background:#3a3a3a;color:#eee;font:18px/1.3 sans-serif}
#grid{position:fixed;left:0;top:0;width:100vw;height:60vh;display:grid;grid-template-columns:repeat(8,1fr);grid-template-rows:repeat(6,1fr)}
#grid>div{min-width:0;min-height:0}
#label{position:fixed;left:2vw;top:61vh;font-size:2.4vh}
${draftCss}
</style></head><body>
<div id="grid"></div>
<div id="label">${role === 'A' ? 'Tab A: the owner\'s draft' : 'Tab B: background tab'} &middot; ${run}</div>
${draftHtml}
<script>${script}</script>
</body></html>
`;
}

// Starts the server on 127.0.0.1 (a free port). Every request, push and report is logged to `logFile`.
export async function startFixture({ run, logFile }) {
  const log = fs.createWriteStream(logFile, { flags: 'a' });
  log.on('error', () => {});
  let logOpen = true;
  const write = (entry) => {
    if (logOpen)
      log.write(`${JSON.stringify({ at: Date.now(), ...entry })}\n`);
  };
  const initial = { A: random32(), B: random32() };
  const clients = new Set();
  const reports = [];
  const waiters = new Set();
  let seq = 0;
  const base = `/r/${run}`;
  const notify = () => {
    for (const waiter of [...waiters])
      waiter.check();
  };

  const server = http.createServer((request, response) => {
    let url;
    try {
      url = new URL(request.url, 'http://fixture.invalid');
    } catch {
      response.writeHead(400);
      response.end();
      return;
    }
    const role = url.searchParams.get('role');
    write({ kind: 'http', method: request.method, path: url.pathname, role });
    if (request.method === 'GET' && (url.pathname === `${base}/a` || url.pathname === `${base}/b`)) {
      const pageRole = url.pathname.endsWith('/a') ? 'A' : 'B';
      const body = pageHtml(pageRole, run, initial[pageRole]);
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store', 'content-length': Buffer.byteLength(body) });
      response.end(body);
      return;
    }
    if (request.method === 'GET' && url.pathname === `${base}/events` && (role === 'A' || role === 'B')) {
      response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store', connection: 'keep-alive' });
      response.write(': open\n\n');
      const client = { role, instance: url.searchParams.get('instance') || '', response, opened: Date.now() };
      clients.add(client);
      write({ kind: 'sse-open', role, instance: client.instance });
      notify();
      // The response's close (not the request's, which ends with the request itself) says the page went away.
      response.on('close', () => {
        clients.delete(client);
        write({ kind: 'sse-close', role, instance: client.instance });
        notify();
      });
      return;
    }
    if (request.method === 'POST' && url.pathname === `${base}/report`) {
      const chunks = [];
      let size = 0;
      request.on('data', (part) => {
        size += part.length;
        if (size <= MAX_REPORT_BYTES)
          chunks.push(part);
      });
      request.on('end', () => {
        let report = null;
        try {
          report = size <= MAX_REPORT_BYTES ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : null;
        } catch {
          report = null;
        }
        if (!report || typeof report !== 'object' || report.run !== run) {
          response.writeHead(400);
          response.end();
          return;
        }
        report.received = Date.now();
        reports.push(report);
        write({ kind: 'report', report });
        response.writeHead(204, { 'cache-control': 'no-store' });
        response.end();
        notify();
      });
      return;
    }
    if (url.pathname === '/favicon.ico') {
      response.writeHead(204, { 'cache-control': 'no-store' });
      response.end();
      return;
    }
    response.writeHead(404, { 'content-type': 'text/plain' });
    response.end('not part of this fixture\n');
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const origin = `http://127.0.0.1:${server.address().port}`;

  // Resolves with the first truthy value of `probe()` (re-checked on every request, report and SSE change).
  function waitUntil(probe, timeoutMs, label) {
    return new Promise((resolve, reject) => {
      const waiter = {
        check: () => {
          let value;
          try {
            value = probe();
          } catch (error) {
            done();
            reject(error);
            return;
          }
          if (value) {
            done();
            resolve(value);
          }
        },
      };
      const timer = setTimeout(() => {
        done();
        reject(new Error(`timed out after ${timeoutMs} ms waiting for ${label}`));
      }, timeoutMs);
      const done = () => {
        clearTimeout(timer);
        waiters.delete(waiter);
      };
      waiters.add(waiter);
      waiter.check();
    });
  }

  // Sends one command to every listening page of `role`; returns the command's sequence number.
  function push(role, command) {
    const targets = [...clients].filter((client) => client.role === role);
    if (!targets.length)
      throw new Error(`no ${role} page is listening`);
    const id = ++seq;
    const message = { ...command, seq: id };
    for (const client of targets)
      client.response.write(`data: ${JSON.stringify(message)}\n\n`);
    write({ kind: 'push', role, command: message, pages: targets.length });
    return id;
  }

  // Every event a page of `role` has reported so far, in its own order, without duplicates.
  function events(role) {
    const seen = new Set();
    const out = [];
    for (const report of reports) {
      if (report.role !== role || !Array.isArray(report.events))
        continue;
      for (const event of report.events) {
        const key = `${report.instance}:${event.i}`;
        if (seen.has(key))
          continue;
        seen.add(key);
        out.push({ instance: report.instance, ...event });
      }
    }
    return out;
  }

  const listening = (role, instance) => [...clients].some((client) => client.role === role && (!instance || client.instance === instance));

  async function close() {
    for (const client of clients)
      client.response.end();
    const closed = new Promise((resolve) => server.close(() => resolve()));
    server.closeAllConnections?.();
    await closed;
    logOpen = false;
    await new Promise((resolve) => log.end(resolve));
  }

  return {
    origin,
    urlA: `${origin}${base}/a`,
    urlB: `${origin}${base}/b`,
    titleA: `Fixture A ${run}`,
    titleB: `Fixture B ${run}`,
    initial,
    reports,
    push,
    waitUntil,
    events,
    listening,
    close,
  };
}
