// Readers for the skill's MCP results and the harness's own guard rails. Pure functions (covered by selftest.mjs).

export const HANDLE_FORM = /^tab-[0-9a-f]{32}$/;

export const textOf = (result) => (result?.content || []).filter((part) => part.type === 'text').map((part) => part.text).join('\n');
export const imagesOf = (result) => (result?.content || []).filter((part) => part.type === 'image');

// The skill's (or this harness's guard's) error code in a text, or null. Codes are the only words of a tool's text
// that go into the published summary.
export const codeOf = (text) => /\b((?:CHROME_)?BROWSER_[A-Z_]+|SMOKE_GUARD_[A-Z_]+)\b/.exec(String(text ?? ''))?.[1] ?? null;

// Upstream's tab list lines: "- 0: (current) [Title](url)". The first line for an index wins.
export function parseTabs(text) {
  const tabs = new Map();
  for (const match of String(text).matchAll(/^- (\d+): (\(current\) )?\[(.*)\]\((.*)\)[ \t]*$/gm)) {
    const index = Number(match[1]);
    if (!tabs.has(index))
      tabs.set(index, { index, current: !!match[2], title: match[3], url: match[4] });
  }
  return [...tabs.values()];
}

// This skill's handle lines under a tab list: "- 1: tabHandle tab-<32 hex>".
export function parseHandles(text) {
  const handles = new Map();
  for (const match of String(text).matchAll(/^- (\d+): tabHandle (tab-[0-9a-f]{32})[ \t]*$/gm)) {
    if (!handles.has(Number(match[1])))
      handles.set(Number(match[1]), match[2]);
  }
  return handles;
}

export function tabsWithHandles(text) {
  const handles = parseHandles(text);
  return parseTabs(text).map((tab) => ({ ...tab, handle: handles.get(tab.index) ?? null }));
}

// The one-time approval link of browser_connect's answer, checked to be the pinned extension's own connect page with
// a loopback relay and no bypass token. Returns the link exactly as the skill wrote it (kept in memory only).
export function parseConnectLink(text, extensionId) {
  const line = String(text).split('\n').map((l) => l.trim()).find((l) => l.startsWith('chrome-extension://'));
  if (!line)
    throw new Error('the answer has no chrome-extension:// link');
  const url = new URL(line);
  if (url.protocol !== 'chrome-extension:' || url.host !== extensionId || url.pathname !== '/connect.html')
    throw new Error('the link is not the pinned extension\'s connect page');
  if (url.searchParams.has('token'))
    throw new Error('the link carries a bypass token');
  if (url.searchParams.get('protocolVersion') !== '2')
    throw new Error('the link does not ask for relay protocol 2');
  const relay = new URL(url.searchParams.get('mcpRelayUrl') || 'invalid:');
  if (relay.protocol !== 'ws:' || !['[::1]', '127.0.0.1'].includes(relay.hostname) || !/^\/extension\/[0-9a-f-]{36}$/.test(relay.pathname))
    throw new Error('the relay is not a loopback ws://.../extension/<uuid> address');
  let client = null;
  try {
    client = JSON.parse(url.searchParams.get('client') || 'null');
  } catch {
    client = null;
  }
  return { href: line, client, protocolVersion: 2 };
}

// For the local diagnostics only (nothing of this is published): the relay's per-connection path, raw or
// percent-encoded, every query of an extension connect page, and the extension's bypass token are blanked.
export const redact = (text) => String(text ?? '')
  .replace(/(chrome-extension:\/\/[a-p]{32}\/connect\.html)\?[^\s"'<>)\]]*/gi, '$1?<redacted>')
  .replace(/((?:\/|%2F)(?:cdp|extension)(?:\/|%2F))[0-9a-f-]{36}/gi, '$1<redacted>')
  .replace(/(PLAYWRIGHT_MCP_EXTENSION_TOKEN(?:=|%3D))[^\s&"'<>]+/gi, '$1<redacted>');

// Switches this smoke test must never give Chrome: they weaken a sandbox, web or extension check, run headless,
// open a debugging port to other clients, mark the browser as automated or silence the debugging notice.
export const FORBIDDEN_FLAGS = [
  '--no-sandbox', '--disable-setuid-sandbox', '--disable-seccomp-filter-sandbox', '--disable-namespace-sandbox',
  '--disable-gpu-sandbox', '--no-zygote-sandbox', '--disable-web-security', '--disable-site-isolation-trials',
  '--allow-running-insecure-content', '--ignore-certificate-errors', '--headless', '--remote-debugging-port',
  '--remote-allow-origins', '--enable-automation', '--disable-features', '--enable-features',
  '--disable-extensions-file-access-check', '--extensions-on-chrome-urls', '--silent-debugger-extension-api',
  '--user-agent', '--test-type',
];
export const forbiddenFlags = (args) => args.filter((arg) => FORBIDDEN_FLAGS.some((flag) => arg === flag || arg.startsWith(`${flag}=`)));
