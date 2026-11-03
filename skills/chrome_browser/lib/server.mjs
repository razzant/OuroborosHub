// The chrome_browser MCP server: the official Playwright MCP tools over the owner's Chrome,
// reached through the official Playwright Extension, plus two tools of this skill.
//
// initialize and tools/list are passive: no relay, no socket, no browser, no profile, no
// file is written. A browser session starts only inside an explicit browser_connect call.
import { AsyncLocalStorage } from 'node:async_hooks';
import fs from 'node:fs';
import path from 'node:path';
import { BrowserConnection, redact } from './connection.mjs';
import { BrowserGuard } from './guard.mjs';
import { createPointerHooks, InputFramePreparer, DELIVERY, unconfirmedInputMessage, unverifiedInputMessage } from './input.mjs';

export const SERVER_NAME = 'chrome_browser';

// Upstream tools this server does not offer, and why.
export const EXCLUDED_TOOLS = new Map([
  ['browser_run_code_unsafe', 'runs arbitrary code inside this server process, outside the request guard and the owner-tab rules'],
  ['browser_close', 'is replaced by browser_disconnect, which says what actually happens: the connection ends and the tabs stay open'],
]);

// Tools that send input to a page: each gets one discarded frame capture per document first.
export const INPUT_TOOLS = new Set([
  'browser_click', 'browser_hover', 'browser_drag', 'browser_type', 'browser_press_key',
  'browser_select_option', 'browser_fill_form', 'browser_drop',
]);

// Tools that take the agent to another page or tab. They are judged by the address they open
// (before) and by the page they leave the agent on (after), not by the page they start from,
// so a page the task may not read can always be left. Every other page tool is also judged by
// the page it starts on, as the built-in browser judges its page before an action.
export const NAVIGATION_TOOLS = new Set(['browser_navigate', 'browser_navigate_back', 'browser_tabs']);

// Tools that return records Playwright keeps from a tab's earlier documents (see withRecordChecks).
export const RECORD_TOOLS = new Set(['browser_console_messages', 'browser_network_requests', 'browser_network_request']);

// How long Playwright waits for a navigation. Upstream's 60 s plus its 5 s wait for "load" (pinned
// coreBundle.js, Tab.navigate) outlasts the host's default 60 s MCP_TOOL_TIMEOUT_SEC, whose expiry ends
// the task's session and with it the owner's approval. 45 s plus that 5 s leaves 10 s under the default
// for the checks around the call. If an owner lowers the setting below about 50 s, the host's timeout
// comes first.
export const NAVIGATION_TIMEOUT_MS = 45_000;

// The largest response body returned as an image: the host's image reader takes no larger file
// (Ouroboros tools/vision.py, _VLM_MAX_FILE_BYTES).
export const MAX_RESPONSE_IMAGE_BYTES = 20 * 1024 * 1024;

// Parameters removed from the advertised schemas (or never advertised) and refused if sent anyway.
const STRIPPED_PARAMETERS = {
  '*': {
    filename: 'This server returns results in the response (screenshots as images); it does not write them to a file you name, so "filename" is not accepted.',
    // No tool advertises it, but upstream's BrowserBackend.callTool reads json, raw and cwd from it (pinned coreBundle.js),
    // which changes how a result is formatted: the checks on what comes back read it as formatted by default.
    _meta: 'This server does not take upstream\'s "_meta" output options (cwd, raw, json); results always come in the advertised form.',
  },
  browser_tabs: { isolatedContext: 'Tabs are opened in the owner\'s own Chrome profile only; "isolatedContext" is not accepted.' },
};

export const CONNECT_TOOL = {
  name: 'browser_connect',
  description: [
    'Connect to the owner\'s own Chrome through the official Playwright Extension.',
    'Returns at once with a one-time approval link (chrome-extension://...). Give it only to the owner, in this conversation:',
    'they paste it into a NEW tab of their Chrome, pick the tab to share and choose "Allow & select".',
    'Chrome switches to that tab once at approval. browser_tabs never activates a tab, but a page opening a popup can (see the skill\'s limits).',
    'Until the owner approves, every other browser tool answers BROWSER_NEEDS_APPROVAL and nothing is queued.',
    'Calling it again while waiting returns the same link; after a lost, failed or ended connection it makes a new one and says why the last one ended.',
  ].join(' '),
  inputSchema: { type: 'object', properties: {}, additionalProperties: false },
  annotations: { title: 'Connect to the owner\'s Chrome', readOnlyHint: false, destructiveHint: false, openWorldHint: true },
};

export const DISCONNECT_TOOL = {
  name: 'browser_disconnect',
  description: 'End the connection to the owner\'s Chrome. The relay stops; the extension then detaches from the shared tab and the agent\'s tabs and removes their tab group, which this server cannot observe. Every tab stays open with what was typed into it. A later browser_connect needs a new approval.',
  inputSchema: { type: 'object', properties: {}, additionalProperties: false },
  annotations: { title: 'Disconnect from Chrome', readOnlyHint: false, destructiveHint: false, openWorldHint: false },
};

export const SERVER_INSTRUCTIONS = [
  'Works in the owner\'s own Chrome tab, shared through the official Playwright Extension.',
  'Start with browser_connect and give the link to the owner only. After approval, act in the shared tab and in background tabs you open with browser_tabs action "new"; those tabs open without being activated and selecting one does not bring it to front. A popup a page opens is another matter: clicking a target=_blank link or a button calling window.open can open it active and switch the owner\'s view, and nothing here prevents or undoes that.',
  'Action results are short: the Playwright code that ran, the page address and title when they change, and any dialog. Call browser_snapshot to see the page and get element refs.',
  'Handback is conversational: when the owner wants to do something themselves (a login, a code, a decision), stop sending actions until they say they are done, then take a fresh browser_snapshot. There is no lock; their edits are in the page.',
  'Requests from attached tabs are checked by the host\'s request guard; a refused request fails with ERR_BLOCKED_BY_CLIENT and the reason is reported.',
  'The guard also decides which addresses you may open and which pages you may read (their address and any redirect on the way); if it refuses, nothing is opened, or the output is withheld with its reason.',
  'Console entries and network requests kept from a page you have left are judged by where they came from: those you may not read are withheld and counted, and the rest are returned.',
].join(' ');

// The address upstream will open for a URL argument: Tab.checkUrlAndNavigate's own scheme
// completion (pinned coreBundle.js), written the way the browser parses it. This is not a policy:
// whether the address may be opened is the host's decision (BrowserGuard.decideTarget).
export function navigationTarget(url) {
  let target = url;
  const hasScheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(target) || /^(about|data|blob|javascript):/i.test(target);
  if (!hasScheme) {
    if (target.startsWith('localhost') || target.startsWith('127.0.0.1') || target.startsWith('0.0.0.0') || target.startsWith('[::1]'))
      target = 'http://' + target;
    else
      target = 'https://' + target;
  }
  try {
    return new URL(target).href;
  } catch {
    return target; // the browser refuses an unparseable address itself
  }
}

// The address a call opens, or undefined. A new tab opens about:blank first (pinned coreBundle.js,
// CRBrowserContext.doCreateNewPage) and stays there unless browser_tabs got a url (upstream tests
// `if (params.url)`), so without one about:blank is the address to judge.
export function navigationArgument(name, args) {
  if (name === 'browser_navigate' && typeof args.url === 'string')
    return args.url;
  if (name === 'browser_tabs' && args.action === 'new')
    return typeof args.url === 'string' && args.url ? args.url : 'about:blank';
  return undefined;
}

export function argumentRefusal(name, args) {
  for (const [scope, params] of Object.entries(STRIPPED_PARAMETERS)) {
    if (scope !== '*' && scope !== name)
      continue;
    for (const [param, why] of Object.entries(params)) {
      if (args[param] !== undefined && !(param === 'isolatedContext' && args[param] === 'default'))
        return `BROWSER_ARGUMENT_REFUSED: ${why}`;
    }
  }
  return '';
}

function stripSchema(name, schema) {
  const copy = JSON.parse(JSON.stringify(schema));
  const remove = { ...STRIPPED_PARAMETERS['*'], ...(STRIPPED_PARAMETERS[name] || {}) };
  for (const param of Object.keys(remove)) {
    if (copy.properties)
      delete copy.properties[param];
    if (Array.isArray(copy.required))
      copy.required = copy.required.filter((item) => item !== param);
  }
  return copy;
}

// Same shape as Playwright's toMcpTool.
export function toMcpTool(z, tool) {
  const readOnly = tool.schema.type === 'readOnly' || tool.schema.type === 'assertion';
  return {
    name: tool.schema.name,
    description: tool.schema.description,
    inputSchema: stripSchema(tool.schema.name, z.toJSONSchema(tool.schema.inputSchema)),
    annotations: { title: tool.schema.title, readOnlyHint: readOnly, destructiveHint: !readOnly, openWorldHint: true },
  };
}

// Same as Playwright's mergeTextParts: adjacent text parts joined, images and other parts kept as they are.
export function mergeTextParts(result) {
  const content = [];
  const text = [];
  for (const part of result.content || []) {
    if (part.type === 'text') {
      text.push(part.text);
      continue;
    }
    if (text.length) {
      content.push({ type: 'text', text: text.join('\n') });
      text.length = 0;
    }
    content.push(part);
  }
  if (text.length)
    content.push({ type: 'text', text: text.join('\n') });
  return { ...result, content };
}

export function errorResult(text) {
  return { content: [{ type: 'text', text: `### Error\n${redact(text)}` }], isError: true };
}

function textResult(text) {
  return { content: [{ type: 'text', text }] };
}

export async function upstreamConfig(tools) {
  // An explicit empty environment: PLAYWRIGHT_MCP_* variables cannot add capabilities,
  // init pages or scripts, file access, origins or a config file to this server.
  // snapshotMode "none": upstream otherwise writes each action's page snapshot to a file in the
  // server's private folder and returns only a link to it, not the snapshot; browser_snapshot
  // still returns the snapshot inline. codegen "typescript" (upstream's default): an action result
  // carries the Playwright code that ran, for the element the ref resolved to. It is rendered only
  // when the tool completed (a thrown error returns the error alone: BrowserBackend.callTool), so
  // it is upstream's record of the action, not the request read back. With "none" a successful
  // click, type or fill_form returned no text at all (QA 69293f27 F5).
  return await tools.resolveCLIConfigForMCP({ imageResponses: 'allow', webmcp: false, caps: [], snapshotMode: 'none', codegen: 'typescript', timeoutNavigation: NAVIGATION_TIMEOUT_MS }, {});
}

export function upstreamToolList(tools, config) {
  return tools.filteredTools(config).filter((tool) => !EXCLUDED_TOOLS.has(tool.schema.name));
}

// Upstream names files it kept in this server's private session folder by their path relative to
// that folder, in a few generated lines (pinned coreBundle.js, Response._build and the screenshot
// tool): under "### Events" the console line ("- New console entries: output/console-<time>.log#L3")
// and a download line; under "### Result" a file link ("- [Screenshot of viewport](output/page-
// <time>.png)"); under "### Ran Playwright code" the screenshot's comment and path ("// Screenshot
// viewport and save it as output/page-<time>.png", "path: 'output/page-<time>.png',"). The model
// cannot open these files, and they go when the server exits (QA 69293f27 F6). Exactly those
// lines are rewritten, and only for a file really in that folder, to say where the content is.
// Page text is left as it is, even where it names such a file (the owner may open one in the tab).
// The separator is "/", or "\" (escaped as "\\" inside generated code) on Windows.
const PRIVATE_FILE_LINES = new Map([
  ['Events', [
    { pattern: /^- New console entries: output(?:\\{1,2}|\/)([^\s#]+)#L\d+(?:-L\d+)?$/, name: 1, line: () => '- New console entries: read them with browser_console_messages' },
    { pattern: /^- Downloaded file (.+) to "output(?:\\{1,2}|\/)([^"]+)"$/, name: 2, line: (m) => `- Downloaded file ${m[1]}: kept in this server's private folder, which cannot be opened from here` },
  ]],
  ['Result', [
    {
      pattern: /^- \[([^\]\n]+)\]\(output(?:\\{1,2}|\/)([^)\s]+)\)$/,
      name: 2,
      line: (m, image) => (image && /^Screenshot\b/.test(m[1]) ? `- ${m[1]}: the image is attached to this result` : `- ${m[1]}: kept in this server's private folder, which cannot be opened from here`),
    },
  ]],
  ['Ran Playwright code', [
    { pattern: /^(\/\/ Screenshot .+ and save it as )output(?:\\{1,2}|\/)(\S+)$/, name: 2, line: (m) => `${m[1]}<server-private file>` },
    { pattern: /^(\s*path: ')output(?:\\{1,2}|\/)([^']+)(',?)$/, name: 2, line: (m) => `${m[1]}<server-private file>${m[3]}` },
  ]],
]);

export function describePrivateFiles(result, outputDir) {
  if (!outputDir)
    return result;
  // Only a name a generated line refers to is looked up, so the cost follows the result, not the folder.
  const inFolder = (name) => path.basename(name) === name && name !== '.' && name !== '..' && fs.existsSync(path.join(outputDir, name));
  const image = (result.content || []).some((part) => part.type === 'image');
  const rewrite = (value) => {
    let section = '';
    return value.split('\n').map((line) => {
      if (line.startsWith('### ')) {
        section = line.slice(4);
        return line;
      }
      for (const rule of PRIVATE_FILE_LINES.get(section) || []) {
        const match = rule.pattern.exec(line);
        if (match && inFolder(match[rule.name]))
          return rule.line(match, image);
      }
      return line;
    }).join('\n');
  };
  return { ...result, content: (result.content || []).map((part) => (part.type === 'text' ? { ...part, text: rewrite(part.text) } : part)) };
}

// The image type of these bytes, for the formats the host's image carrier and its vision providers take.
export function imageTypeOf(bytes) {
  if (bytes.length >= 8 && bytes.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])))
    return 'png';
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff)
    return 'jpeg';
  if (bytes.length >= 6 && ['GIF87a', 'GIF89a'].includes(bytes.toString('latin1', 0, 6)))
    return 'gif';
  if (bytes.length >= 12 && bytes.toString('latin1', 0, 4) === 'RIFF' && bytes.toString('latin1', 8, 12) === 'WEBP')
    return 'webp';
  return '';
}

// Upstream saves a body that is not text to a file in the private folder and answers with that file's
// bare relative path (pinned coreBundle.js, renderRequestPart and saveResponseBody), which the model
// cannot open. The file named here is the one upstream just wrote, never a path read from any result. An
// image goes out exactly as it came, in upstream's own image carrier; the file stays where upstream put it.
export function describeSavedBody(fileName, contentType) {
  let bytes;
  try {
    bytes = fs.readFileSync(fileName);
  } catch {
    return { text: '- Response body: saved by upstream, but it could not be read back here. It is not returned.' };
  }
  const type = imageTypeOf(bytes);
  if (type && bytes.length <= MAX_RESPONSE_IMAGE_BYTES)
    return { image: { data: bytes, type }, text: '- Response body: the image is attached to this result' };
  const declared = String(contentType ?? '').split(';')[0].trim().slice(0, 100) || 'no declared type';
  const why = type ? `larger than the ${MAX_RESPONSE_IMAGE_BYTES} bytes returned as an image` : 'not text or a supported image';
  return { text: `- Response body: ${bytes.length} bytes (${declared}), ${why}. It is not returned here, and this server's private folder cannot be opened from here.` };
}

function guardBlocksSince(guard, mark) {
  return guard.recentBlocks.filter((entry) => entry.seq > mark);
}

function originAndPath(url) {
  try {
    const parsed = new URL(url);
    return `${parsed.origin}${parsed.pathname}`;
  } catch {
    return String(url).slice(0, 200);
  }
}

// Keeps the last refusals so a tool result can report what the guard refused during that call.
export function recordGuardBlocks(guard) {
  guard.recentBlocks = [];
  guard.blockSeq = 0;
  const decide = guard.decideRequest.bind(guard);
  guard.decideRequest = async (facts) => {
    const block = await decide(facts);
    if (block) {
      guard.blockSeq += 1;
      guard.recentBlocks.push({ seq: guard.blockSeq, method: facts.method, where: originAndPath(facts.url), block });
      if (guard.recentBlocks.length > 50)
        guard.recentBlocks.shift();
    }
    return block;
  };
  return guard;
}

const textOf = (result) => (result.content || []).filter((part) => part.type === 'text').map((part) => part.text).join('\n');

// Names an address without its query, or anything past the scheme of a data:/javascript: address
// (their text is the content itself).
function addressLabel(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:')
      return `${parsed.origin}${parsed.pathname}`;
    return `an address with the ${parsed.protocol} scheme`;
  } catch {
    return 'an unparseable address';
  }
}

// Page output the task may not read is replaced, as the built-in browser returns only its reason.
function withheldResult(refusal, ran) {
  const what = ran
    ? 'The call ran, but its output is withheld'
    : 'Nothing was done';
  return errorResult([
    refusal.block,
    `${what}: this task may not read ${refusal.where} (the page's own address or a redirect hop on the way to it). Only what reaches you is withheld; a request already sent on the way is not undone.`,
    'If it is a tab you opened, browser_navigate it elsewhere or close it with browser_tabs. If it is the owner\'s tab, tell the owner instead of acting in it.',
  ].join('\n'));
}

// Clicks whose delivery is not confirmed are said so; none is repeated. A denied plain click
// makes the call an error; elsewhere upstream's own state check (checkboxes) stays the verdict.
function withDeliveryNotes(name, result, deliveries) {
  const denied = deliveries.filter((entry) => entry.verdict === DELIVERY.denied);
  const unverified = deliveries.filter((entry) => entry.verdict === DELIVERY.probeUnavailable || entry.verdict === DELIVERY.probeUnreadable);
  if (!denied.length && !unverified.length)
    return result;
  const lines = [
    ...denied.map((entry) => unconfirmedInputMessage(entry.action, entry.seen)),
    ...unverified.map((entry) => unverifiedInputMessage(entry.action, entry.verdict)),
  ];
  if (denied.length && name === 'browser_click' && !result.isError)
    return { ...result, content: [{ type: 'text', text: `### Error\n${lines.join('\n')}` }, ...(result.content || [])], isError: true };
  return { ...result, content: [...(result.content || []), { type: 'text', text: `### Input delivery\n${lines.join('\n')}` }] };
}

// runtime: { bundle, utils, playwright } from loadPinnedPlaywright (loaded on first use).
export function createServerCore({ loadRuntime, guardEnvironment, clientName = 'Ouroboros', approvalTtlMs, framePreparer = new InputFramePreparer() }) {
  let prepared = null;
  let connection = null;
  let guard = null;

  async function runtime() {
    if (prepared)
      return prepared;
    const loaded = loadRuntime();
    const { tools } = loaded.bundle;
    const config = await upstreamConfig(tools);
    const toolList = upstreamToolList(tools, config).map(withRecordChecks);
    const pointerHooks = createPointerHooks();
    tools.setPointerActionHooks(pointerHooks);
    const listed = [CONNECT_TOOL, DISCONNECT_TOOL, ...toolList.map((tool) => toMcpTool(loaded.utils.z, tool))];
    prepared = { ...loaded, tools, config, toolList, listed, pointerHooks, byName: new Map(toolList.map((tool) => [tool.schema.name, tool])) };
    return prepared;
  }

  // One call's decisions: the same address is asked once per call and asked again by the next
  // call, so the answer is the host's current one.
  const callDecisions = new AsyncLocalStorage();
  const newDecisions = () => {
    const asked = new Map();
    return (url) => {
      if (!asked.has(url))
        asked.set(url, guard.decideTarget(url));
      return asked.get(url);
    };
  };

  async function firstRefusal(urls, decide) {
    for (const url of urls) {
      const block = await decide(url);
      if (block)
        return { block, where: addressLabel(url) };
    }
    return null;
  }

  // The built-in browser's rule for what it returns (Ouroboros tools/browser.py,
  // _navigation_block_reason), asked of the host: the page's address and every redirect hop
  // recorded for its current document must be addresses this task may read. Only page.url() is
  // read here, never the page itself, so an open dialog cannot stall the check.
  async function pageRefusal(page, decide) {
    return page ? await firstRefusal(connection.navigationTargets(page), decide) : null;
  }

  // Tab lists in a result carry other tabs' titles (page content). A tab whose title appears
  // in the text, as upstream last rendered it, is judged like the current tab. One that closed
  // during the call has left upstream's list but not the text.
  async function listedTabRefusal(backend, text, decide) {
    const closed = connection.closedPages.map((page) => prepared.tools.Tab.forPage(page));
    for (const tab of [...closed, ...(backend._context?.tabs?.() ?? [])]) {
      if (!tab)
        continue;
      const title = tab._lastHeader?.title;
      if (!title || !text.includes(title))
        continue;
      const refusal = await pageRefusal(tab.page, decide);
      if (refusal)
        return refusal;
    }
    return null;
  }

  // What upstream's handlers read from a tab, narrowed to the records this task may read. They keep
  // rendering everything themselves; only the list they are handed is shorter. `withheld` collects
  // per kind what was left out, for one sentence after the result.
  function readableTab(tab, decide, withheld) {
    const judged = new Map();
    const documentRefusal = (document) => {
      if (!judged.has(document)) {
        judged.set(document, document.truncated
          ? Promise.resolve({ block: 'BROWSER_RECORD_ORIGIN_UNKNOWN: the page made more redirects than are kept for this check', where: 'a page with too many redirects to check' })
          : firstRefusal([...document.addresses], decide));
      }
      return judged.get(document);
    };
    const retained = async (kind, records) => {
      // In one step with the read: more records can arrive while the host is being asked.
      const documents = records.map((record, index) => connection.recordDocument(tab.page, kind, records.length - 1 - index));
      const kept = [];
      let refused = 0;
      for (const [index, record] of records.entries()) {
        const refusal = await documentRefusal(documents[index]);
        if (refusal) {
          refused += 1;
          withheld.first ??= refusal;
        } else {
          kept.push(record);
        }
      }
      withheld[kind] = refused;
      return kept;
    };
    const view = Object.create(tab);
    view.page = {
      consoleMessages: async (options) => await retained('console', await tab.page.consoleMessages(options)),
      pageErrors: async (options) => await retained('errors', await tab.page.pageErrors(options)),
    };
    view.requests = async (all) => {
      const kept = [];
      let refused = 0;
      for (const entry of [...await tab.requests(all)]) {
        const refusal = await firstRefusal(connection.requestTargets(tab.page, entry.request), decide);
        if (refusal) {
          refused += 1;
          withheld.first ??= refusal;
        } else {
          kept.push(entry);
        }
      }
      withheld.requests = refused;
      return kept;
    };
    return view;
  }

  // Upstream keeps a tab's console messages, page errors and requests across its documents and returns
  // them on request, long after the page's address stopped saying where they came from. So each record is
  // judged by the document and the redirect chain it really came from, and those the task may not read
  // stay out of the result while the rest of the history is returned.
  function withRecordChecks(tool) {
    if (!RECORD_TOOLS.has(tool.schema.name))
      return tool;
    return {
      ...tool,
      handle: async (context, params, response, signal) => {
        const decide = callDecisions.getStore() ?? newDecisions();
        if (tool.schema.name === 'browser_network_request')
          return await oneRequestRecord(tool, context, params, response, signal, decide);
        const withheld = { console: 0, errors: 0, requests: 0, first: null };
        const narrowed = Object.create(context, { ensureTab: { value: async () => readableTab(await context.ensureTab(), decide, withheld) } });
        await tool.handle(narrowed, params, response, signal);
        const refused = withheld.console + withheld.errors + withheld.requests;
        if (refused)
          response.addTextResult(`Withheld ${refused} ${refused === 1 ? 'record' : 'records'} here: this task may not read ${withheld.first.where} (the page that made them, or a redirect hop on the way). ${withheld.first.block}`);
      },
    };
  }

  // One record: refused as a whole, or returned as upstream renders it, with a saved body described.
  async function oneRequestRecord(tool, context, params, response, signal, decide) {
    const tab = await context.ensureTab();
    const record = await tab.request(params.index);
    const refusal = record ? await firstRefusal(connection.requestTargets(tab.page, record), decide) : null;
    if (refusal) {
      response.addError(`${refusal.block}\nThis request's record is withheld: this task may not read ${refusal.where} (the request's own address, a redirect hop on the way to it, or the page that made it). Other records can still be read.`);
      return;
    }
    // Upstream looks for the current tab again (defineTabTool), and the host was asked meanwhile: another tab can be
    // current by now. It reads the tab that was checked.
    const checked = Object.create(context, { ensureTab: { value: async () => tab } });
    const resolve = response.resolveClientOutputFile;
    const addText = response.addTextResult;
    let saved = null;
    response.resolveClientOutputFile = async function(template, title) {
      const file = await resolve.call(this, template, title);
      if (title === 'Response body')
        saved = file;
      return file;
    };
    response.addTextResult = function(text) {
      if (!saved || text !== saved.relativeName)
        addText.call(this, text);
    };
    try {
      await tool.handle(checked, params, response, signal);
    } finally {
      response.resolveClientOutputFile = resolve;
      response.addTextResult = addText;
    }
    if (!saved)
      return;
    const body = describeSavedBody(saved.fileName, record?.existingResponse()?.headers()['content-type']);
    response.addTextResult(body.text);
    if (body.image)
      await response.registerImageResult(body.image.data, body.image.type);
  }

  async function listTools() {
    return { tools: (await runtime()).listed };
  }

  function ensureConnection(rt) {
    if (connection)
      return connection;
    guard = recordGuardBlocks(new BrowserGuard({ endpoint: guardEnvironment.endpoint, token: guardEnvironment.token }));
    connection = new BrowserConnection({
      tools: rt.tools, playwright: rt.playwright, guard, config: rt.config, toolList: rt.toolList, clientName, approvalTtlMs,
    });
    return connection;
  }

  async function connect(rt) {
    if (!guardEnvironment.configured) {
      const why = guardEnvironment.problem === 'BROWSER_GUARD_REQUIRED'
        ? 'This server runs only with the host\'s request guard. Configure the MCP server with "task_session": true and "browser_policy": true.'
        : `The host started this server with an unusable request guard setting (${guardEnvironment.detail || 'malformed'}). That is a host setup problem, not something the owner can approve; no link was made.`;
      return errorResult(`${guardEnvironment.problem}: ${why} No browser was contacted.`);
    }
    const current = ensureConnection(rt);
    // Why the last attempt ended (lost, expired, a tab Chrome refused...), so a new link is not handed
    // out as if nothing happened: the owner may need to choose differently.
    const previous = current.state === 'closed' ? current.reason : '';
    const outcome = await current.connect();
    if (outcome.status === 'connected')
      return textResult('### Connected\nThe owner\'s Chrome is already connected. Use browser_tabs action "list" to see the tabs this session controls.');
    if (outcome.status === 'attaching')
      return textResult('### Approved\nThe owner approved; the connection is being set up. Call a browser tool again in a moment.');
    if (outcome.status !== 'needs_approval')
      return errorResult(`BROWSER_CONNECT_FAILED: the connection attempt was superseded. Call browser_connect again.`);
    return textResult([
      outcome.reused ? '### Still waiting for approval' : '### Approval needed',
      ...(previous && !outcome.reused ? [`The previous connection ended: ${previous}`] : []),
      'Give the owner this one-time link, only in this conversation:',
      outcome.url,
      'They open their own Chrome, paste it into a NEW tab, pick the tab to share and choose "Allow & select". Chrome switches to that tab once.',
      'The connect page warns that the connection reaches the whole browser profile (signed-in sessions and cookies). That is accurate: say so if they ask.',
      `If the link does not open, the Playwright Extension is not installed in that Chrome (Chrome Web Store id ${rt.extension.id}). If the page reports an unsupported protocol or version, the installed extension does not match the one this skill was tested with (${rt.extension.tested_version}, relay protocol ${rt.extension.relay_protocol_version}): tell the owner instead of retrying.`,
      'Until they approve, other browser tools answer BROWSER_NEEDS_APPROVAL; nothing is queued. The link stops working when this browser session ends or after 15 minutes without approval.',
    ].join('\n'));
  }

  async function currentTab(backend) {
    return backend._context?.currentTab?.() ?? null;
  }

  async function callTool(name, args = {}, signal) {
    let rt;
    try {
      rt = await runtime();
    } catch (error) {
      return errorResult(error.message);
    }
    if (name === CONNECT_TOOL.name)
      return await connect(rt);
    if (name === DISCONNECT_TOOL.name) {
      if (!connection)
        return textResult('### Not connected\nThere was no connection to end.');
      const outcome = await connection.disconnect();
      if (outcome.status === 'not_connected')
        return textResult('### Not connected\nThere was no connection to end.');
      return textResult('### Disconnected\nThe connection is closed and the relay stopped. Chrome\'s extension detaches when it sees that; this server cannot confirm the detach itself. All tabs stay open. A new browser_connect needs a new approval.');
    }
    if (EXCLUDED_TOOLS.has(name))
      return errorResult(`Tool "${name}" is not offered by this server: it ${EXCLUDED_TOOLS.get(name)}.`);
    if (!rt.byName.has(name))
      return errorResult(`Tool "${name}" not found`);
    if (!connection || connection.state !== 'ready')
      return errorResult(connection ? connection.refusal() : 'BROWSER_NOT_CONNECTED: No Chrome tab is connected. Call browser_connect; it returns a link for the owner to approve in their own Chrome. Nothing was done.');
    const params = args && typeof args === 'object' ? args : {};
    const refusal = argumentRefusal(name, params);
    if (refusal)
      return errorResult(refusal);
    const backend = connection.backend();
    connection.closedPages.length = 0;
    // No page tool runs unless the host acknowledged the guard for the very context it acts in.
    const tabForGuard = await currentTab(backend);
    if (!guard.isArmed(connection.context()) || (tabForGuard && !guard.isArmed(tabForGuard.page.context())))
      return errorResult('BROWSER_GUARD_NOT_ARMED: the request guard is not armed for this browser context, so nothing was done. Call browser_disconnect, then browser_connect.');
    // An address is opened only if the host would let this task open it (the built-in browser's
    // check before browse_page). Non-network addresses (javascript:, data:, about:, ...) send no
    // request a route could judge, so this is their only decision; for http(s) the route still
    // judges the request itself.
    const decide = newDecisions();
    const url = navigationArgument(name, params);
    if (url !== undefined) {
      const block = await decide(navigationTarget(url));
      if (block)
        return errorResult(`${block}\n${name === 'browser_tabs' ? 'No tab was opened.' : 'The address was not opened; the tab was left as it was.'}`);
    }
    if (!NAVIGATION_TOOLS.has(name)) {
      const before = await pageRefusal(tabForGuard?.page, decide);
      if (before)
        return withheldResult(before, false);
    }
    if (name === 'browser_evaluate') {
      const block = await guard.checkEvaluate(tabForGuard ? tabForGuard.page.url() : '');
      if (block)
        return errorResult(block);
    }
    let frameError = null;
    if (INPUT_TOOLS.has(name)) {
      if (tabForGuard && !tabForGuard.modalStates().length) {
        try {
          await framePreparer.prepare(tabForGuard.page);
        } catch (error) {
          frameError = error;
        }
      }
    }
    // All of that was asked about tabForGuard, and asking takes time. If the owner or the page closes that tab
    // meanwhile, upstream makes a neighbour current (pinned coreBundle.js, Context._onPageClosed), and a page tool
    // takes whichever tab is current when it starts (Context.ensureTab, which waits for no event once the tab has
    // initialized). Navigation is allowed to leave a refused page, but must not silently move into another tab.
    // Only explicit tab management may change this selection. Check before a failed frame's advice to retry,
    // which would otherwise act in the neighbour.
    if (name !== 'browser_tabs' && (await currentTab(backend)) !== tabForGuard)
      return errorResult('BROWSER_TAB_CHANGED: the tab this call was checked for closed or stopped being current while the host was being asked. Nothing was done, in it or in the tab current now. Take a fresh browser_snapshot before acting again.');
    if (frameError)
      return errorResult(frameError.message);
    const mark = guard.blockSeq;
    const deliveryMark = rt.pointerHooks.deliveryMark();
    let result;
    try {
      result = await callDecisions.run(decide, () => backend.callTool(name, params, signal));
    } catch (error) {
      result = errorResult(String(error));
    }
    result = describePrivateFiles(result, connection.outputDir());
    // What the call returns is judged by the page it left the agent on, by the page a page tool worked in (it can
    // have closed under the call, with another current by now), and by any tab title it lists.
    const after = await pageRefusal((await currentTab(backend))?.page, decide)
      ?? (name === 'browser_tabs' ? null : await pageRefusal(tabForGuard?.page, decide))
      ?? await listedTabRefusal(backend, textOf(result), decide);
    if (after)
      result = withheldResult(after, true);
    result = withDeliveryNotes(name, result, rt.pointerHooks.deliveriesSince(deliveryMark));
    const blocked = guardBlocksSince(guard, mark);
    if (blocked.length) {
      const lines = blocked.map((entry) => `- ${entry.method} ${entry.where}: ${entry.block}`);
      result = { ...result, content: [...(result.content || []), { type: 'text', text: `### Requests the guard refused while this call ran\n${lines.join('\n')}` }] };
    }
    return mergeTextParts(result);
  }

  async function shutdown() {
    await connection?.shutdown();
  }

  return { listTools, callTool, shutdown, get connection() { return connection; }, get guard() { return guard; }, runtime };
}
