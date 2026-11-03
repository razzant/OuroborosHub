#!/usr/bin/env node
// The chrome_browser MCP server (stdio). The host's task-lifetime MCP holder starts it from the
// Settings → MCP entry printed by `setup.mjs status`; it is not meant to be run by hand.
import { main } from '../lib/main.mjs';

main().catch(() => {
  process.stderr.write('CHROME_BROWSER_START_FAILED: the server could not start.\n');
  process.exit(1);
});
