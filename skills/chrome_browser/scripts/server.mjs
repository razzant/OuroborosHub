#!/usr/bin/env node
// The chrome_browser MCP server (stdio). The host's task-lifetime MCP holder starts it from its
// Settings → MCP row (written by Connect tools, or imported from `setup.mjs status` on older
// hosts); it is not meant to be run by hand.
import { main } from '../lib/main.mjs';

main().catch(() => {
  process.stderr.write('CHROME_BROWSER_START_FAILED: the server could not start.\n');
  process.exit(1);
});
