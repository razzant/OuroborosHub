// The chrome_browser MCP server: the official Playwright MCP tools over the owner's Chrome,
// reached through the official Playwright Extension, plus two tools of this skill.
//
// initialize and tools/list are passive: no relay, no socket, no browser, no profile, no
// file is written. A browser session starts only inside an explicit browser_connect call.
import { AsyncLocalStorage } from 'node:async_hooks';
import { constants as bufferConstants } from 'node:buffer';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { BrowserConnection, redact } from './connection.mjs';
import { BrowserGuard, INTAKE_MAX_FILES, INTAKE_MAX_BYTES } from './guard.mjs';
import { DOWNLOAD_MAX_BYTES, DOWNLOAD_SETTLE_MS, MAX_BEGINS, MAX_CHAINS, MAX_RESPONSES, targetIdOf } from './download.mjs';
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

// Tools that give the page files: the host admits and copies each named file first (see withFileIntake).
export const FILE_TOOLS = new Set(['browser_file_upload', 'browser_drop']);

// Tools that can capture the HTTP download they start, when asked to (see withDownloadCapture).
export const DOWNLOAD_TOOLS = new Set(['browser_click']);

// What a screenshot that names its tab ("tabHandle", see withTabTarget) is told when it is not that tab's image. The
// first two say no capture was attempted; the third, that one was and nothing of it is passed on. None is followed by
// work in another tab: neither the current tab nor a closed tab's neighbour is captured in its place.
const TAB_HANDLE_UNKNOWN = 'BROWSER_TAB_HANDLE_UNKNOWN: "tabHandle" names no open tab of this connection: its tab closed or was detached, or the value is from before the last browser_connect or is not a handle. No capture was attempted, in any tab. Call browser_tabs action "list" for the handles there are now.';
const TAB_CHANGED_WHILE_ASKED = 'BROWSER_TAB_CHANGED: a navigation committed in the tab this "tabHandle" names while the host was being asked about it, so the page there is not the one that was checked. No capture was attempted, in it or in any other tab. Call again: the page is checked as it is then.';
const captureWithheld = (why) => `BROWSER_TAB_CAPTURE_WITHHELD: ${why}. A capture was attempted in the tab this "tabHandle" names, and nothing it returned is passed on: no image, and no other tab was captured in its place. Call browser_tabs action "list", then capture again if the tab is still there.`;

// A tab's handle: the full guid suffix Playwright itself gives the tab's page (pinned coreBundle.js, SdkObject: "page@"
// and 16 random bytes, new for every page of every connection). It stays with its tab while tab indexes shift, an earlier
// connection's names nothing in a later one, and nothing is kept here to resolve it (namedTab).
const handleOf = (page) => `tab-${page._guid.slice('page@'.length)}`;

// How long Playwright waits for a navigation. Upstream's 60 s plus its 5 s wait for "load" (pinned
// coreBundle.js, Tab.navigate) outlasts the host's default 60 s MCP_TOOL_TIMEOUT_SEC, whose expiry ends
// the task's session and with it the owner's approval. 45 s plus that 5 s leaves 10 s under the default
// for the checks around the call. If an owner lowers the setting below about 50 s, the host's timeout
// comes first.
export const NAVIGATION_TIMEOUT_MS = 45_000;

// The largest response body returned as an image: the host's image reader takes no larger file
// (Ouroboros tools/vision.py, _VLM_MAX_FILE_BYTES).
export const MAX_RESPONSE_IMAGE_BYTES = 20 * 1024 * 1024;
// The largest other binary response body returned, as an embedded resource: the bound a file intake has.
export const MAX_RESPONSE_BODY_BYTES = INTAKE_MAX_BYTES;

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
    'Keep the same task and browser tool session alive while waiting for approval; use a supported warm wait, or relay the pending approval to the parent without finalizing a child task.',
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
  'Approval and handback require the same live task and browser tool session. Use the warm wait the current role supports; a child relays the pending owner step to its parent without finalizing. If no warm wait exists, explain that the link cannot survive the task ending. After reapproval take a fresh snapshot and reassess; never replay an action whose result was lost.',
  'Requests from attached tabs are checked by the host\'s request guard; a refused request fails with ERR_BLOCKED_BY_CLIENT and the reason is reported.',
  'The guard also decides which addresses you may open and which pages you may read (their address and any redirect on the way); if it refuses, nothing is opened, or the output is withheld with its reason.',
  'Console entries and network requests kept from a page you have left are judged by where they came from: those you may not read are withheld and counted, and the rest are returned.',
  'A click expected to start a file download (a link or a form that returns a file) can be given captureDownload: true. Then each document response in that tab whose request began while the call ran, and which Chrome did not show as a page, can come back as a copy, with its address, type and sha256, and the result says why for the others. A request a page timer or the owner started meanwhile cannot be told apart from the click\'s. Chrome\'s own downloads go on and are listed apart, by Chrome\'s name and state: Chrome does not say which response a download came from.',
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
  // A tab named by its handle is never dropped in silence: upstream's schemas ignore a parameter they do not know, and
  // the tool would then work in the current tab as if that were the tab named.
  if (args.tabHandle !== undefined) {
    if (name !== 'browser_take_screenshot')
      return 'BROWSER_ARGUMENT_REFUSED: "tabHandle" is taken only by browser_take_screenshot. This tool works in the current tab; it was not run there in place of the tab named. Nothing was done.';
    if (args.target !== undefined || args.element !== undefined)
      return 'BROWSER_ARGUMENT_REFUSED: with "tabHandle" a screenshot is of that tab\'s viewport or full page. An element reference ("target", "element") comes from a snapshot of the current tab and is not accepted with it. No capture was attempted.';
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

// The JSON-RPC message that carries a result is one JavaScript string: the pinned SDK's stdio transport writes
// JSON.stringify({ result, jsonrpc: '2.0', id }) + '\n' (utilsBundle.js serializeMessage), and a string longer than the
// runtime allows (buffer.constants.MAX_STRING_LENGTH, 536870888 characters on 64-bit Node 24) throws RangeError there.
// The SDK then sends no answer at all (Protocol._onrequest ends in .catch(_onerror)): every copy of the call would be lost
// and the host would wait for its timeout. So a result's binary resources are attached in order while that message,
// with its id, still fits; one that does not fit is left out (a later, smaller one can still fit), its own line in the
// result says so with its size and sha256, and a last line counts what was left out. Below the limit nothing changes.
// The bound is the whole message, measured exactly: base64 needs no JSON escaping, so each blob adds its length.
export const TRANSPORT_MAX_CHARS = bufferConstants.MAX_STRING_LENGTH;
const ATTACHED = 'attached to this result as a binary resource';
// The server's own line of each binary resource it keeps (reportCapture's copy, a response body) holds, in place of
// ATTACHED, this mark of that resource in that call: a random value the page never sees. fitToTransport replaces exactly
// these marks, so text from anywhere else, a page's or Playwright's, that reads like such a line is never touched.
const attachMark = (resources, k) => `[[attached ${resources.mark} ${k}]]`;
// The server's own notes about what is left out: full, shorter, shortest. A shorter one is used only when the fuller do
// not fit. The shortest names the reason and that the notes were cut in each copy's own line, adds no closing line, and
// is no longer than ATTACHED: with nothing attached, the answer is then no longer than the rest of it, so it fits
// whenever the rest does (the empty baseline). Shortest per copy is not always least in all: with many copies left out,
// the shorter lines and their one closing line can take less room than the shortest lines.
const NOT_ATTACHED = [
  (max) => `not attached to this result: with it, this result's JSON-RPC message would be longer than the ${max} characters this server's JSON transport can send, so it is not returned`,
  () => 'not attached (transport limit)',
  () => 'not attached: transport limit, notes cut',
];
const LEFT_OUT = [
  (left, count, bytes, max) => `- ${left.length} of ${count} binary resources (${bytes} bytes) not attached: the JSON transport carries at most ${max} characters in one message. Not returned: ${left.map((resource) => resource.uri).join(', ')}.`,
  (left, count, bytes) => `- ${left.length} of ${count} binary resources (${bytes} bytes) not attached (transport limit; notes shortened).`,
  null,
];
export function fitToTransport(result, resources, { maxChars = TRANSPORT_MAX_CHARS, id = 0 } = {}) {
  const sizeOf = (resource) => (resource.blob.length / 4) * 3 - (resource.blob.endsWith('==') ? 2 : resource.blob.endsWith('=') ? 1 : 0);
  const build = (keep, level, blobs) => {
    const left = resources.filter((_, k) => !keep[k]);
    const content = (result.content || []).map((part) => {
      if (part.type !== 'text')
        return part;
      let { text } = part;
      resources.forEach((_, k) => { text = text.split(attachMark(resources, k)).join(keep[k] ? ATTACHED : NOT_ATTACHED[level](maxChars)); });
      return { ...part, text };
    });
    if (left.length && LEFT_OUT[level])
      content.push({ type: 'text', text: LEFT_OUT[level](left, resources.length, left.reduce((sum, resource) => sum + sizeOf(resource), 0), maxChars) });
    resources.forEach((resource, k) => { if (keep[k]) content.push({ type: 'resource', resource: blobs ? resource : { ...resource, blob: '' } }); });
    return mergeTextParts({ ...result, content });
  };
  const measure = (keep, level) => JSON.stringify({ result: build(keep, level, false), jsonrpc: '2.0', id }).length + 1
    + resources.reduce((sum, resource, k) => sum + (keep[k] ? resource.blob.length : 0), 0);
  const all = resources.map(() => true);
  if (measure(all, 0) <= maxChars)
    return build(all, 0, true);
  // Content first: what is attached is chosen in order, each choice measured with whichever of the notes takes the least
  // room for it, from an empty baseline that fits; then the fullest notes that fit are used. If even the baseline does
  // not fit, the rest of the answer alone is over the limit: nothing of it is cut here, and nothing is attached.
  const least = (keep) => Math.min(...NOT_ATTACHED.map((_, at) => measure(keep, at)));
  const keep = resources.map(() => false);
  if (least(keep) <= maxChars)
    for (let k = 0; k < resources.length; k++) {
      keep[k] = true;
      if (least(keep) > maxChars)
        keep[k] = false;
    }
  const sizes = NOT_ATTACHED.map((_, at) => measure(keep, at));
  const level = sizes.findIndex((size) => size <= maxChars);
  return build(keep, level < 0 ? sizes.indexOf(Math.min(...sizes)) : level, true);
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

// The media type an embedded resource is labelled with. A declared image type stays only on a supported
// image (which goes out as an image anyway), so a host attaching images by type is never handed other bytes.
function resourceMimeType(contentType) {
  const declared = String(contentType ?? '').split(';')[0].trim().toLowerCase();
  if (!/^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$/.test(declared) || declared.startsWith('image/'))
    return 'application/octet-stream';
  return declared;
}

// Exact bytes as an embedded resource (MCP BlobResourceContents) under a label the caller makes unique.
function binaryResource(bytes, contentType, uri) {
  return { uri, mimeType: resourceMimeType(contentType), blob: bytes.toString('base64') };
}

// Upstream saves a body that is not text to a file in the private folder and answers with that file's
// bare relative path (pinned coreBundle.js, renderRequestPart and saveResponseBody), which the model
// cannot open. The file named here is the one upstream just wrote, never a path read from any result. An
// image goes out exactly as it came, in upstream's own image carrier; other bytes up to the bound go out
// exactly as they came in an embedded resource (MCP BlobResourceContents), labelled by the request's index
// and the bytes' sha256 so two bodies never share a label, but only to a host that said it takes embedded
// resources (embeddedResources; BrowserGuard.embeddedResources): an older host would show their base64 as
// text, so for it they are described as before. The file stays where upstream put it. Only the first bytes
// are read to tell an image; the whole body only when it will be returned. `attached` is the resource's own
// clause in its line (the caller's mark of it, for fitToTransport; ATTACHED by default), written here, after the
// site's Content-Type, so that the line is never searched for it afterwards.
const IMAGE_SIGNATURE_BYTES = 12;
export function describeSavedBody(fileName, contentType, index = 0, { embeddedResources = true, attached = ATTACHED } = {}) {
  let bytes;
  let size;
  try {
    size = fs.statSync(fileName).size;
    bytes = Buffer.alloc(Math.min(size, IMAGE_SIGNATURE_BYTES));
    const fd = fs.openSync(fileName, 'r');
    try {
      bytes = bytes.subarray(0, fs.readSync(fd, bytes, 0, bytes.length, 0));
    } finally {
      fs.closeSync(fd);
    }
    const type = imageTypeOf(bytes);
    if ((type && size <= MAX_RESPONSE_IMAGE_BYTES) || (!type && embeddedResources && size <= MAX_RESPONSE_BODY_BYTES)) {
      bytes = fs.readFileSync(fileName);
      size = bytes.length;
    }
  } catch {
    return { text: '- Response body: saved by upstream, but it could not be read back here. It is not returned.' };
  }
  const type = imageTypeOf(bytes);
  if (type && size <= MAX_RESPONSE_IMAGE_BYTES)
    return { image: { data: bytes, type }, text: '- Response body: the image is attached to this result' };
  const declared = String(contentType ?? '').split(';')[0].trim().slice(0, 100) || 'no declared type';
  if (!type && size <= MAX_RESPONSE_BODY_BYTES && bytes.length === size && embeddedResources) {
    const sha256 = crypto.createHash('sha256').update(bytes).digest('hex');
    return {
      resource: binaryResource(bytes, contentType, `chrome-browser://response-body/request-${index}/${sha256}`),
      text: `- Response body: ${size} bytes (${declared}), ${attached}, sha256 ${sha256}`,
    };
  }
  const why = type
    ? `larger than the ${MAX_RESPONSE_IMAGE_BYTES} bytes returned as an image`
    : !embeddedResources
      ? 'not text or a supported image, and this host did not say it takes binary resources from this server (capabilities.embedded_resources when the guard was armed)'
      : `larger than the ${MAX_RESPONSE_BODY_BYTES} bytes returned as a binary resource`;
  return { text: `- Response body: ${size} bytes (${declared}), ${why}. It is not returned here, and this server's private folder cannot be opened from here.` };
}

function guardBlocksSince(guard, mark) {
  return guard.recentBlocks.filter((entry) => entry.seq > mark);
}

// A cancelled MCP call aborts its signal, but the pinned upstream handlers do not look at it before they act, and the
// host's file admission and a capture's set-up are awaited before them. So the file and capture paths check it once
// everything their action needs is ready, right before the one step that reaches the page: an action not yet sent is
// refused, and nothing already sent is undone.
function cancelledBeforeAction(signal, response, what) {
  if (!signal?.aborted)
    return false;
  response.addError(`The call was cancelled ${what}`);
  return true;
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

// Why a response of the click was not copied (download.mjs, outcomeOf).
const NOT_COPIED = {
  page: () => 'Chrome showed it as a page.',
  canceled: () => 'Chrome dropped it while it was held, before passing it on (its navigation was replaced or stopped).',
  failed: (record) => `Chrome received it, then reported the request failed (${record.failure}), not that it went to a download.`,
  unsettled: () => 'by the end of this call\'s wait Chrome had not reported whether it showed the response as a page, so no copy is returned.',
  late: () => 'Chrome handed over the body only after this call had stopped waiting; it was not kept. The response was passed on unchanged.',
  read_failed: (record) => `Chrome did not hand over the body (${record.error}). The response was passed on unchanged.`,
  too_large: (record) => `the response declared ${record.declared} bytes, more than the ${DOWNLOAD_MAX_BYTES} read here. It was passed on unread.`,
  unknown_length: () => 'the response declared no length (a chunked response, for example), so reading it could not be bounded beforehand. It was passed on unread.',
  chunked: (record) => `the response is sent with Transfer-Encoding ${record.transfer.slice(0, 40)}, which overrides any Content-Length, so reading it could not be bounded beforehand. It was passed on unread.`,
  over_read: (record) => `Chrome handed over ${record.size} bytes, more than the ${DOWNLOAD_MAX_BYTES} returned here, although the response declared ${record.declared}. They were dropped, not returned.`,
  encoded: (record) => `the response is content-encoded (${record.encoding.slice(0, 40)}), so the length Chrome would hand over is not declared. It was passed on unread.`,
  no_request: () => 'Chrome reported no network request for it (as for a link with the download attribute), so the addresses on the way to it, and what became of it, cannot be known. It was passed on unread.',
  chain_incomplete: () => 'not every address on the way to it was seen (its request began before this click, or was past the requests one capture keeps), so they cannot all be checked. It was passed on unread.',
};

// What Chrome reported of one of its downloads.
function downloadState(progress) {
  if (progress?.state === 'completed')
    return `completed, as Chrome reported${Number.isFinite(progress.receivedBytes) ? ` (${progress.receivedBytes} bytes)` : ''}. Where Chrome saved it is not known here.`;
  if (progress?.state === 'canceled')
    return 'canceled, as Chrome reported.';
  if (progress?.state === 'inProgress')
    return `still in progress when this call returned (${progress.receivedBytes} of ${progress.totalBytes || 'an unknown number of'} bytes); whether it finishes is not known here.`;
  return 'unknown; Chrome reported no progress for it.';
}

// runtime: { bundle, utils, playwright } from loadPinnedPlaywright (loaded on first use).
export function createServerCore({ loadRuntime, guardEnvironment, clientName = 'Ouroboros', approvalTtlMs, framePreparer = new InputFramePreparer(), downloadSettleMs = DOWNLOAD_SETTLE_MS, transportMaxChars = TRANSPORT_MAX_CHARS }) {
  let prepared = null;
  let connection = null;
  let guard = null;

  async function runtime() {
    if (prepared)
      return prepared;
    const loaded = loadRuntime();
    const { tools } = loaded.bundle;
    const config = await upstreamConfig(tools);
    const toolList = upstreamToolList(tools, config).map(withRecordChecks).map((tool) => withFileIntake(tool, loaded.utils.z)).map((tool) => withDownloadCapture(tool, loaded.utils.z))
        .map((tool) => withTabTarget(tool, loaded.utils.z)).map(withTabHandles);
    const pointerHooks = createPointerHooks();
    tools.setPointerActionHooks(pointerHooks);
    const listed = [CONNECT_TOOL, DISCONNECT_TOOL, ...toolList.map((tool) => toMcpTool(loaded.utils.z, tool))];
    prepared = { ...loaded, tools, config, toolList, listed, pointerHooks, byName: new Map(toolList.map((tool) => [tool.schema.name, tool])) };
    return prepared;
  }

  // One call's decisions: the same address is asked once per call and asked again by the next
  // call, so the answer is the host's current one.
  const callDecisions = new AsyncLocalStorage();
  // One call's embedded resources, returned after its output checks (none if its output is withheld).
  const callResources = new AsyncLocalStorage();
  // The tab one call named by its handle, once callTool resolved it and the guard was asked about its page (withTabTarget).
  const callTarget = new AsyncLocalStorage();
  // Labels of the response copies captureDownload returns, unique for this server's life.
  let downloadCount = 0;
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

  // Tab lists carry page titles and addresses, including tabs closed before the reply.
  // Use this call's pages: teardown empties upstream's tabs, and a newer call can clear
  // connection.closedPages. An empty title still needs its address checked.
  async function listedTabRefusal(pages, text, decide) {
    for (const page of pages) {
      const tab = prepared.tools.Tab.forPage(page);
      if (!tab)
        continue;
      const { title, url } = tab._lastHeader ?? {};
      if (!(title && text.includes(title)) && !(url && text.includes(url)))
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
    // A body returned as a binary resource: describeSavedBody writes that resource's own mark (the next one of this call)
    // in place of ATTACHED in its line (fitToTransport); the site's Content-Type in the same line is never searched.
    const resources = callResources.getStore();
    const body = describeSavedBody(saved.fileName, record?.existingResponse()?.headers()['content-type'], params.index,
      { embeddedResources: guard.embeddedResources(tab.page.context()), attached: resources ? attachMark(resources, resources.length) : ATTACHED });
    if (body.resource && !resources) {
      response.addTextResult('- Response body: binary, and this call has no way to return it. It is not returned.');
      return;
    }
    if (body.resource)
      resources.push(body.resource);
    response.addTextResult(body.text);
    if (body.image)
      await response.registerImageResult(body.image.data, body.image.type);
  }

  // Upload and drop take files from the host, never from a path read here: the host admits each named file as
  // read_file would and retains a bounded copy (BrowserGuard.intakeFiles), and the page gets those exact bytes
  // with the original file name through Playwright's own buffer branch (FileChooser.setFiles and Locator.drop
  // with { name, buffer } payloads). "paths" keeps upstream's form, each string asked as { path }; "files" may
  // also name read_file's own selectors (root, and for a skill payload file bucket and skill_name), passed on as
  // named, empty ones too: the host's admission decides what they select. An empty or omitted selection (cancelling the chooser, a drop of
  // data only) is upstream's own handler, unchanged. Admission comes before the chooser is consumed: a refusal
  // leaves it open for another try.
  function withFileIntake(tool, z) {
    if (!FILE_TOOLS.has(tool.schema.name))
      return tool;
    const upload = tool.schema.name === 'browser_file_upload';
    const how = `The host reads each file with its own file admission and keeps a copy; the page gets only that copy's bytes and the file's original name. At most ${INTAKE_MAX_FILES} files and ${INTAKE_MAX_BYTES} bytes in one call.`;
    const inputSchema = tool.schema.inputSchema.extend({
      paths: z.array(z.string()).max(INTAKE_MAX_FILES).optional().describe(upload
        ? `Paths of the files to upload, as read_file takes them. If neither paths nor files is given, the file chooser is cancelled. ${how}`
        : `Paths of the files to drop onto the element, as read_file takes them. ${how}`),
      files: z.array(z.object({
        path: z.string().min(1).describe('The file\'s path, as read_file takes it.'),
        root: z.string().optional().describe('The root the host reads the path from, as read_file names it. Omit for the default.'),
        bucket: z.string().optional().describe('As read_file takes it: required only for root skill_payload.'),
        skill_name: z.string().optional().describe('As read_file takes it: required only for root skill_payload.'),
      }).strict()).max(INTAKE_MAX_FILES).optional().describe(`Instead of paths: the files with the root (and bucket and skill_name) each is read from, as read_file takes them. ${how}`),
    });
    const description = upload ? tool.schema.description : tool.schema.description.replace('"paths" or "data"', '"paths", "files" or "data"');
    return {
      ...tool,
      schema: { ...tool.schema, description, inputSchema },
      handle: async (context, params, response, signal) => {
        if (params.paths?.length && params.files?.length) {
          response.addError('Give the files either as "paths" or as "files", not both. Nothing was done.');
          return;
        }
        const entries = [...(params.paths ?? []).map((where) => ({ path: where })), ...(params.files ?? [])];
        if (!entries.length)
          return await tool.handle(context, { ...params, files: undefined }, response, signal);
        const tab = await context.ensureTab();
        // defineTabTool's own modal-state rule, which upstream's handler would have applied.
        const chooser = tab.modalStates().find((state) => state.type === 'fileChooser');
        if (upload && !chooser) {
          response.addError(`Error: The tool "${tool.schema.name}" can only be used when there is related modal state present.`);
          return;
        }
        if (!upload && tab.modalStates().length) {
          response.addError(`Error: Tool "${tool.schema.name}" does not handle the modal state.`);
          return;
        }
        response.setIncludeSnapshot();
        const target = upload ? null : await tab.targetLocator(params);
        const intake = await guard.intakeFiles(entries);
        if (intake.block) {
          response.addError(`${intake.block}\n${upload ? 'No file was given to the page; the file chooser is still open.' : 'Nothing was dropped.'}`);
          return;
        }
        const payloads = intake.files.map(({ name, buffer }) => ({ name, buffer }));
        // The code shows the names the page gets, not the bytes or any path.
        const given = `[${intake.files.map((file, index) => `{ name: ${JSON.stringify(file.name)}, buffer: hostCopies[${index}] }`).join(', ')}]`;
        if (upload) {
          // The host may take a while: the page or the owner can have closed the chooser meanwhile.
          if (!tab.modalStates().includes(chooser)) {
            response.addError('BROWSER_FILE_CHOOSER_GONE: the file chooser closed while the host was reading the files. No file was given to the page.');
            return;
          }
          if (cancelledBeforeAction(signal, response, 'while the host was reading the files. No file was given to the page; the file chooser is still open.'))
            return;
          response.addCode(`await fileChooser.setFiles(${given})`);
          tab.clearModalState(chooser);
          try {
            await tab.waitForCompletion(async () => {
              await chooser.fileChooser.setFiles(payloads);
            });
          } catch (e) {
            tab.setModalState(chooser);
            response.addError(e instanceof Error ? e.message : String(e));
            return;
          }
        } else {
          if (cancelledBeforeAction(signal, response, 'while the host was reading the files. Nothing was dropped.'))
            return;
          const payload = { files: payloads, ...(params.data ? { data: params.data } : {}) };
          await tab.waitForCompletion(async () => {
            await target.locator.drop(payload, tab.actionTimeoutOptions);
          });
          response.addCode(`await page.${target.resolved}.drop({ files: ${given}${params.data ? `, data: ${JSON.stringify(params.data)}` : ''} });`);
        }
        for (const file of intake.files)
          response.addTextResult(`- Gave the page ${JSON.stringify(file.name)}: ${file.size} bytes, sha256 ${file.sha256}`);
      },
    };
  }

  // A click the agent expects to start a download can ask for copies of the document responses its tab gets while it
  // runs. Only then, and only in the tab it acts in, Chrome pauses this tab's document responses once more, at the response stage, and
  // the server reads a body it can bound before letting the same response go on, to Chrome's own download or wherever
  // Chrome takes it (download.mjs). Requests that Playwright routes still reach the host's request guard. Afterwards
  // the server waits, at most downloadSettleMs, for Chrome to say what became of each response and of each download it
  // began, then restores Playwright's own interception and reports apart: each response with its copy (judged as a
  // page's address is, then returned as an embedded resource) or why there is none, and Chrome's downloads as Chrome
  // reports them, tied to no copy.
  function withDownloadCapture(tool, z) {
    if (!DOWNLOAD_TOOLS.has(tool.schema.name))
      return tool;
    const inputSchema = tool.schema.inputSchema.extend({
      captureDownload: z.boolean().optional().describe(`Set to true when this click is expected to start a file download over HTTP (a link or a form that returns a file). The call then waits up to ${downloadSettleMs / 1000} s after the click; a click that only loads a page waits all of it. A document response in this tab is read once, as Chrome receives it, and goes on unchanged (nothing is fetched again) if its request and every redirect hop were seen from the start during the call and it declares its length (at most ${DOWNLOAD_MAX_BYTES} bytes), is not sent in chunks and is not content-encoded. It comes back as an embedded resource with its address, type and sha256 - a copy of the server's response, whether or not Chrome saved a file - only if Chrome received it and then ended its request with net::ERR_ABORTED (not shown as a page, not dropped or failed). One call keeps at most ${MAX_RESPONSES} responses, and copies are attached only while the result fits one JSON-RPC message; the rest are named, not returned. A request a page timer or the owner started meanwhile cannot be told apart from the click's. Chrome's own downloads are listed apart by Chrome's name and state, since Chrome does not say which response a download came from. Only this tab; a download the page makes itself (blob: or data:) is not captured.`),
    });
    return {
      ...tool,
      schema: { ...tool.schema, inputSchema },
      handle: async (context, params, response, signal) => {
        const { captureDownload, ...upstreamParams } = params;
        if (!captureDownload)
          return await tool.handle(context, upstreamParams, response, signal);
        const tab = await context.ensureTab();
        // defineTabTool's own modal-state rule, before anything is changed.
        if (tab.modalStates().length) {
          response.addError(`Error: Tool "${tool.schema.name}" does not handle the modal state.`);
          return;
        }
        // A copy can be returned only to a host that takes embedded resources; otherwise nothing is changed or clicked.
        if (!guard.embeddedResources(tab.page.context())) {
          response.addError('BROWSER_DOWNLOAD_CAPTURE_UNSUPPORTED: this host did not say it takes binary resources from this server (capabilities.embedded_resources when the guard was armed: an older Ouroboros, or a session no task owns), so a captured copy could not be returned.\nNothing was clicked.');
          return;
        }
        const capture = connection.downloads();
        let window;
        try {
          if (!capture)
            throw new Error('BROWSER_DOWNLOAD_CAPTURE_UNAVAILABLE: the connection is not ready');
          window = await capture.open(targetIdOf(tab.page));
        } catch (error) {
          response.addError(`${error.message}\nNothing was clicked.`);
          return;
        }
        if (cancelledBeforeAction(signal, response, 'while its download capture was being set up. Nothing was clicked.')) {
          try {
            await capture.close(window);
          } finally {
            window.release();
          }
          return;
        }
        // Upstream looks for the current tab again (defineTabTool): it clicks in the tab the capture is open in.
        const checked = Object.create(context, { ensureTab: { value: async () => tab } });
        const addError = response.addError;
        let failure = null;
        let refused = false;
        try {
          response.addError = function(error) {
            refused = true;
            return addError.call(this, error);
          };
          try {
            await tool.handle(checked, upstreamParams, response, signal);
          } catch (error) {
            failure = error;
          } finally {
            response.addError = addError;
          }
          // A click that failed may still have gone out: what Chrome already reported is said, without waiting.
          await capture.settle(window, failure || refused ? 0 : downloadSettleMs, signal);
        } finally {
          await capture.close(window);
        }
        const report = window.report();
        let text;
        try {
          // A click that failed and got nothing, with no response still held, is upstream's failure, unchanged.
          if ((failure || refused) && !report.responses.length && !report.downloads.length && !report.elsewhere.length && !report.held.length) {
            if (failure)
              throw failure;
            return;
          }
          text = await reportCapture(report, failure || refused, window);
        } finally {
          // The copies are in the result now (as base64) or not returned: the window keeps no bytes.
          window.release();
        }
        // Upstream answers a thrown error with that error's text alone (BrowserBackend.callTool), so a click that
        // failed after a response or a download came carries the report in it.
        if (failure)
          throw Object.assign(new Error(text), { toString: () => `${String(failure)}\n${text}` });
        response.addTextResult(text);
      },
    };
  }

  // The one open tab of this connection with that handle, or null. `context` is upstream's own: a tab the owner
  // closed, or the extension detached (dragged out of the group), has left its list, and a later connection has
  // another context with other pages.
  function namedTab(context, handle) {
    const tabs = typeof handle === 'string' ? context.tabs().filter((tab) => handleOf(tab.page) === handle) : [];
    return tabs.length === 1 ? tabs[0] : null;
  }

  // Why what a screenshot got from the tab it named is not passed on, or ''. The capture is bound to what that tab
  // showed when the host was asked about it before (callTool reads the tab's version in one step with those
  // addresses): the connection is still the one the call began on, the tab is still one of its open tabs, and no
  // navigation has committed in the tab since. Then every document it showed in between is one the host was asked
  // about, before and after. A tab that left and came back to the same address does not pass; neither does a reload.
  function targetLoss(backend, attempt, target) {
    if (connection.state !== 'ready' || connection.diagnostic !== attempt || attempt?.terminal)
      return 'the connection ended or changed during the call';
    if (namedTab(backend._context, target.handle) !== target.tab)
      return 'the tab closed or was detached from this connection during the call';
    if (connection.documentVersion(target.tab.page) !== target.version)
      return 'a navigation committed in the tab between the check before the capture and the check after it, so the image may show a page that was not checked, even if the tab is back at the same address';
    return '';
  }

  // browser_take_screenshot captures whichever tab is current when it starts, and closing a tab hands "current" to a
  // neighbour. With "tabHandle" (handleOf; browser_tabs lists them) the call names its tab instead. Upstream's own
  // handler runs unchanged and is handed that tab where it asks for the current one (defineTabTool calls
  // context.ensureTab), so the capture is that tab's own Page.screenshot: nothing is selected or brought to front.
  // callTool resolved the handle, read the tab's version and asked the guard about its page before this runs. Asking
  // takes time, so the tab is looked at once more as upstream asks for it: closed, detached or navigated meanwhile, it
  // is not captured. What becomes of the result is callTool's last look (targetLoss). Without "tabHandle" the call is
  // upstream's, untouched.
  function withTabTarget(tool, z) {
    if (tool.schema.name !== 'browser_take_screenshot')
      return tool;
    const inputSchema = tool.schema.inputSchema.extend({
      tabHandle: z.string().optional().describe('Capture the tab with this handle instead of the current tab, without selecting it or bringing it to front. Every browser_tabs result lists the handles ("- 1: tabHandle tab-..."); unlike an index, a handle keeps naming the same tab when other tabs close. A handle whose tab closed or was detached, or one from an earlier connection, is an error with no image, and so is a navigation in that tab during the call: no other tab is captured in its place. Viewport or "fullPage" only, not with "target" or "element".'),
    });
    return {
      ...tool,
      schema: { ...tool.schema, inputSchema },
      handle: async (context, params, response, signal) => {
        const { tabHandle, ...upstreamParams } = params;
        if (tabHandle === undefined)
          return await tool.handle(context, params, response, signal);
        // Only callTool hands a tab over, after its checks: a call that reached this handler another way has none.
        const target = callTarget.getStore();
        const named = Object.create(context, { ensureTab: { value: async () => {
          const problem = !target || namedTab(context, tabHandle) !== target.tab ? TAB_HANDLE_UNKNOWN
            : connection.documentVersion(target.tab.page) !== target.version ? TAB_CHANGED_WHILE_ASKED : '';
          if (problem)
            throw Object.assign(new Error(problem), { toString: () => problem });
          // defineTabTool's own modal-state rule comes next, with nothing awaited in between: without a dialog or a
          // file chooser, upstream's capture starts.
          target.attempted = !target.tab.modalStates().length;
          return target.tab;
        } } });
        // The copy upstream writes to the private folder as it captures (callTool removes it).
        const resolveFile = response.resolveClientOutputFile;
        response.resolveClientOutputFile = async function(...args) {
          const file = await resolveFile.apply(this, args);
          target.file = file.fileName;
          return file;
        };
        try {
          await tool.handle(named, upstreamParams, response, signal);
        } finally {
          response.resolveClientOutputFile = resolveFile;
        }
        if (target.attempted)
          response.addTextResult(`- Tab captured: tabHandle ${tabHandle}, not selected or brought to front. A "Page" or "Open tabs" section here describes the current tab and the tab list, not this capture.`);
        else
          response.addError('No capture was attempted: the tab this "tabHandle" names has an open dialog or file chooser.');
      },
    };
  }

  // browser_tabs names tabs by index, a position in a list that a closing tab shortens. Its result also gives the
  // handle of each tab it lists, by that index. Upstream reads the list once, last, whatever the action (pinned
  // coreBundle.js, browserTabs.handle); the handles are written for that same read, so a tab that closes during the
  // title reads after it moves no handle to another tab's line. Everything else upstream asks of its context goes to
  // the real one. The handles come back with the list or not at all: the result is judged by every tab it lists.
  function withTabHandles(tool) {
    if (tool.schema.name !== 'browser_tabs')
      return tool;
    return {
      ...tool,
      handle: async (context, params, response, signal) => {
        let listed = [];
        const listing = new Proxy(context, { get: (real, key) => {
          if (key === 'tabs')
            return () => (listed = [...real.tabs()]);
          const value = real[key];
          return typeof value === 'function' ? value.bind(real) : value;
        } });
        await tool.handle(listing, params, response, signal);
        if (listed.length)
          response.addTextResult(['Tab handles for browser_take_screenshot "tabHandle", by the indexes above:', ...listed.map((tab, index) => `- ${index}: tabHandle ${handleOf(tab.page)}`)].join('\n'));
      },
    };
  }

  async function reportCapture(report, failed, window) {
    const decide = callDecisions.getStore() ?? newDecisions();
    const resources = callResources.getStore();
    const lines = [];
    let copies = 0;
    for (const [index, { record, outcome }] of report.responses.entries()) {
      const label = `- Response ${index + 1}`;
      // The bytes and the facts of a response come from its address and every hop on the way to it.
      const refusal = await firstRefusal(record.chain, decide);
      if (refusal) {
        lines.push(`${label}: withheld: this task may not read ${refusal.where} (its address or a redirect hop on the way to it). ${refusal.block}`);
        continue;
      }
      const type = record.contentType.split(';')[0].trim().slice(0, 100);
      lines.push(`${label}: ${record.method} ${record.status} from ${addressLabel(record.url)}${type ? ` (${type})` : ''}`);
      if (outcome !== 'copy') {
        lines.push(`  - Not copied: ${NOT_COPIED[outcome](record)}`);
        continue;
      }
      const { bytes, size, sha256, declared, contentType, continued, continueError } = record;
      const notes = [
        ...(declared !== size ? [`The response declared ${declared} bytes.`] : []),
        ...(continued === false ? [`Passing the response on to Chrome afterwards failed (${continueError}).`] : []),
      ];
      const what = 'It is the body of this response, read once as Chrome received it (not fetched again). Chrome passed the response on and did not show it as a page; it does not say which download, if any, it became.';
      if (resources) {
        resources.push(binaryResource(bytes, contentType, `chrome-browser://download/${++downloadCount}/${sha256}`));
        // Its base64 is in the result: the Buffer is not kept as well.
        window.dropBytes(record, 'returned');
        copies += 1;
        lines.push(`  - Copy: ${size} bytes, sha256 ${sha256}, ${attachMark(resources, resources.length - 1)}. ${what}${notes.length ? ` ${notes.join(' ')}` : ''}`);
      } else {
        lines.push(`  - Copy: ${size} bytes, sha256 ${sha256}, but this call has no way to return it. It is not returned.${notes.length ? ` ${notes.join(' ')}` : ''}`);
      }
    }
    if (report.downloads.length) {
      lines.push('- Downloads Chrome began in this tab while this call waited, as Chrome reports them; Chrome does not say which response a download comes from, so none is tied to a copy:');
      for (const entry of report.downloads) {
        const refusal = await firstRefusal([entry.url], decide);
        lines.push(refusal
          ? `  - A download: withheld: this task may not read ${refusal.where}. ${refusal.block}`
          : `  - ${JSON.stringify(entry.suggestedFilename || 'unnamed')} from ${addressLabel(entry.url)}: ${downloadState(entry.native)}`);
      }
    } else if (report.responses.length) {
      lines.push('- Chrome reported no download in this tab while this call waited.');
    }
    for (const entry of report.elsewhere) {
      const refusal = await firstRefusal([entry.url], decide);
      lines.push(refusal
        ? `- A download in another tab: withheld: this task may not read ${refusal.where}. ${refusal.block}`
        : `- A download began in another tab (${JSON.stringify(entry.suggestedFilename || 'unnamed')} from ${addressLabel(entry.url)}); not captured: capture covers only the tab this click acts in.`);
    }
    if (report.held.length) {
      const one = report.held.length === 1;
      lines.push(`- ${one ? 'A document response' : `${report.held.length} document responses`} in this tab (${report.held.join(', ')}) ${one ? 'was' : 'were'} still being read when this call stopped waiting, so nothing was copied from ${one ? 'it' : 'them'}. Chrome gets ${one ? 'it' : 'them'} once read and may then download or show ${one ? 'it' : 'them'} after this call; whether the click made ${one ? 'it' : 'them'} is not known here. Do not repeat the click. Take a fresh browser_snapshot, or ask the owner to look in their Chrome downloads.`);
    } else if (!copies) {
      const nothing = !report.responses.length && !report.downloads.length;
      lines.push(failed
        ? `- ${nothing ? 'Nothing had come in this tab when the click ended in an error' : 'Nothing was copied before the click ended in an error'}; no copy is returned.`
        : `- No copy was returned${nothing ? `: no document response and no download came in this tab within ${downloadSettleMs / 1000} s after the click` : ''}. The click was sent and is not undone, so a page or a download may still follow. Do not repeat it; take a fresh browser_snapshot, or ask the owner to look in their Chrome downloads.`);
    }
    const { responses, begins, chains } = report.dropped;
    if (responses + begins + chains)
      lines.push(`- More came during this click than one capture keeps (${MAX_RESPONSES} document responses, ${MAX_BEGINS} downloads, ${MAX_CHAINS} document requests): ${responses} responses were passed on unread and are not listed, ${begins} downloads and ${chains} requests were not recorded. What is listed above is unaffected.`);
    if (report.restored === false)
      lines.push('- Chrome did not confirm that this call\'s response interception was removed. Later document responses in this tab are still passed on unchanged.');
    else if (report.restored === 'deferred')
      lines.push('- This call\'s response interception stays in this tab until Chrome has handed over the body it is still reading; document responses there meanwhile are passed on unread and unchanged.');
    return lines.join('\n');
  }

  async function listTools() {
    return { tools: (await runtime()).listed };
  }

  function ensureConnection(rt) {
    if (connection)
      return connection;
    guard = recordGuardBlocks(new BrowserGuard({ endpoint: guardEnvironment.endpoint, token: guardEnvironment.token }));
    connection = new BrowserConnection({
      tools: rt.tools, playwright: rt.playwright, guard, config: rt.config, toolList: rt.toolList, clientName, approvalTtlMs, extension: rt.extension,
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
      'Keep this task and its browser tool session alive while waiting, using a supported warm wait. A child should relay the pending owner step to its parent without finalizing. If this role cannot wait warmly, explain that the link cannot survive the task ending.',
    ].join('\n'));
  }

  async function currentTab(backend) {
    return backend._context?.currentTab?.() ?? null;
  }

  // requestId: the JSON-RPC id the answer will carry (main.mjs passes the SDK's extra.requestId), part of the message size.
  async function callTool(name, args = {}, signal, requestId) {
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
    const attempt = connection.diagnostic;
    connection.closedPages.length = 0;
    // A screenshot that names its tab is for that tab alone. A handle that names no open tab of this connection ends
    // the call here, before anything is asked or done: it does not become a call for the current tab.
    let target = null;
    if (params.tabHandle !== undefined) {
      const tab = namedTab(backend._context, params.tabHandle);
      if (!tab)
        return errorResult(TAB_HANDLE_UNKNOWN);
      target = { handle: params.tabHandle, tab, version: undefined, attempted: false, file: '' };
    }
    // No page tool runs unless the host acknowledged the guard for the very context it acts in.
    const tabForGuard = await currentTab(backend);
    if (!guard.isArmed(connection.context()) || (tabForGuard && !guard.isArmed(tabForGuard.page.context())) || (target && !guard.isArmed(target.tab.page.context())))
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
      let before = await pageRefusal(tabForGuard?.page, decide);
      // A tab named by its handle is judged as the current tab is, and as well as it: the result can still say where
      // the current tab is (upstream's "Page" section). Its version is read in one step with the addresses the host
      // is asked about (pageRefusal reads them before it first waits), and the capture is bound to it from here on.
      if (!before && target) {
        target.version = connection.documentVersion(target.tab.page);
        before = await pageRefusal(target.tab.page, decide);
      }
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
    if (connection.diagnostic !== attempt)
      return errorResult('BROWSER_CONNECTION_CHANGED: The earlier connection ended while this call was being checked. Nothing was done. Take a fresh browser_snapshot after approval before acting.');
    if (attempt?.terminal)
      return errorResult('BROWSER_CONNECTION_LOST: The connection began closing while this call was being checked. Nothing was done. Call browser_connect for a new approval, then take a fresh browser_snapshot.');
    if (connection.state !== 'ready')
      return errorResult(connection.refusal());
    const mark = guard.blockSeq;
    const deliveryMark = rt.pointerHooks.deliveryMark();
    const browserContext = connection.context();
    const outputDir = connection.outputDir();
    const outputPages = new Set(browserContext.pages());
    const rememberPage = (page) => outputPages.add(page);
    browserContext.on('page', rememberPage);
    // The resources a call keeps, with the random mark their lines carry until fitToTransport replaces it (attachMark).
    const resources = Object.assign([], { mark: crypto.randomBytes(16).toString('hex') });
    let result;
    try {
      result = await callDecisions.run(decide, () => callResources.run(resources, () => callTarget.run(target, () => backend.callTool(name, params, signal))));
    } catch (error) {
      result = errorResult(String(error));
    } finally {
      browserContext.off('page', rememberPage);
    }
    // Upstream can return observed data even after loss during settling, including
    // a Result alongside an Error. Keep both through the ordinary output checks.
    result = describePrivateFiles(result, outputDir);
    // Upstream writes a copy of each screenshot to the private folder as it is taken, before any check of the result.
    // A named tab's capture keeps none: its image is in this result or nowhere.
    if (target?.file) {
      try {
        fs.rmSync(target.file, { force: true });
      } catch {
        return errorResult(captureWithheld('this server could not remove the copy of the capture that upstream wrote to its private folder'));
      }
    }
    // What the call returns is judged by the page it left the agent on, by the page a page tool worked in (it can
    // have closed under the call, with another current by now), and by any tab title it lists. A tab named by its
    // handle is asked about afresh, not from this call's earlier answers: the host's answer now is the one that counts.
    const after = await pageRefusal((await currentTab(backend))?.page, decide)
      ?? (name === 'browser_tabs' ? null : await pageRefusal(tabForGuard?.page, decide))
      ?? (target ? await pageRefusal(target.tab.page, newDecisions()) : null)
      ?? await listedTabRefusal(outputPages, textOf(result), decide);
    // The last look at a tab named by its handle comes after the host's last answer: nothing is awaited from here to
    // the return. A capture that was never attempted keeps its own refusal.
    const loss = target && !after ? targetLoss(backend, attempt, target) : '';
    if (after)
      result = withheldResult(after, !target || target.attempted);
    else if (loss && target.attempted)
      result = errorResult(captureWithheld(loss));
    result = withDeliveryNotes(name, result, rt.pointerHooks.deliveriesSince(deliveryMark));
    const blocked = guardBlocksSince(guard, mark);
    if (blocked.length) {
      const lines = blocked.map((entry) => `- ${entry.method} ${entry.where}: ${entry.block}`);
      result = { ...result, content: [...(result.content || []), { type: 'text', text: `### Requests the guard refused while this call ran\n${lines.join('\n')}` }] };
    }
    // Receiving a backend response establishes neither every external effect nor when
    // Chrome completed it. Report loss separately, also when output was withheld.
    if (connection.state !== 'ready' || connection.diagnostic !== attempt || attempt?.terminal) {
      const note = connection.diagnostic === attempt
        ? connection.reason || 'BROWSER_CONNECTION_LOST: The connection began closing.'
        : 'BROWSER_CONNECTION_CHANGED: The connection changed.';
      result = { ...result, content: [...(result.content || []), { type: 'text', text: `### Connection during call\n${note} This call was dispatched; the backend response does not establish all of its effects. Do not repeat it automatically. After new approval, take a fresh browser_snapshot and reassess.` }] };
    }
    // Last, after every note: binary output the call kept for its result, unless that output was withheld, as much of it as
    // one JSON-RPC message can carry (fitToTransport).
    if (!after && resources.length)
      result = fitToTransport(mergeTextParts(result), resources, { maxChars: transportMaxChars, id: requestId ?? 0 });
    return mergeTextParts(result);
  }

  async function shutdown() {
    await connection?.shutdown();
  }

  return { listTools, callTool, shutdown, get connection() { return connection; }, get guard() { return guard; }, runtime };
}
