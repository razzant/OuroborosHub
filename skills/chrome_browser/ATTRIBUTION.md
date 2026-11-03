# Attribution and provenance

## Microsoft Playwright (Apache-2.0)

The browser automation, the MCP tools, the extension relay protocol and the Chrome
extension are Microsoft's:

- `@playwright/mcp` 0.0.84 (npm, git head `be26c080edfae7c761db1e757c4aff9f6453b4d7`,
  <https://github.com/microsoft/playwright-mcp>) and its exact dependencies
  `playwright-core` / `playwright` 1.65.0-alpha-2026-10-30 (git head
  `018adb570c757898e7035f23e29ebb689d1d6ead`, <https://github.com/microsoft/playwright>).
  They are installed from the npm registry by the host into the skill's isolated
  environment; they are not vendored here. Tarball integrity values and the sha256 of
  every file this skill loads or checks are in [`pins.json`](pins.json).
- Playwright Extension 0.4.0 (`packages/extension` at the same commit, Chrome Web Store
  id `mmlmfjhmonkocbjadbfplnigmagldckm`). The owner installs it; this skill does not
  ship or modify it.

[`patches/playwright-core@1.65.0-alpha-2026-10-30.patch`](patches/playwright-core@1.65.0-alpha-2026-10-30.patch)
modifies `playwright-core/lib/coreBundle.js`. It contains Microsoft's code and is
distributed under the Apache License 2.0
([`patches/LICENSE-Apache-2.0.txt`](patches/LICENSE-Apache-2.0.txt), the package's own
`LICENSE`), with the package's notice
([`patches/NOTICE-playwright.txt`](patches/NOTICE-playwright.txt)). Its preamble
states every change. The skill applies it in memory when its server starts; installed
files are not modified.

## Design sources from the community contribution

This skill draws on a contribution by **Andrei Kaznacheev (GitHub `kazzand`)**:
OuroborosHub pull request #119 (`chrome_browser`, head
`74e56033458c9230700659c56e47b3b1caeb9657`) and Ouroboros pull request #1401 (core task
sessions, head `4384d057ae948d999093498f429edbdc6219b1ce`). It is a different
implementation. The owner chose how the work should go: their own existing tab with one
agreed switch to it, new tabs for the same work, and conversational handback instead of an
explicit handoff lock. Building that on Microsoft's official extension instead of a custom
one was Ouroboros's implementation decision. These ideas from that work are carried over,
rewritten for Playwright rather than copied:

- background input that never activates a tab: one discarded frame capture before the
  first input into a document, geometry sampled on a real-time clock instead of animation
  frames, a hit test that refuses covered targets, and the rule that an input whose
  effect is unknown is never sent again (PR #119, `extension/service_worker.js`:
  `prepareInputFrame`, `stablePoint`, `pointerClick`; constants 60 ms and 8 s);
- agent tabs created in the background with `active: false` (PR #119,
  `service_worker.js:258`);
- the host's request policy applied to every page request through a private, lifetime-owned
  callback that runs the host's existing Python predicate, rather than a copy of it in
  JavaScript (PR #1401, `ouroboros/mcp_task_sessions.py` lines 22-40, 106-151 and 340-353).
  The present wire contract was agreed separately with the core lane. The redirect-chain
  check before page output follows Ouroboros's built-in browser (`tools/browser.py`); keeping it
  per frame until a commit, and judging retained records by their origin, are this skill's own.

Not carried over: the custom extension and its pairing, native-messaging host and
credential, the executor and its lease/handoff/resume state machine, the
`browser_execute` script realm, and the guard's per-tool tables. Original history and
credit for those pull requests are preserved by the maintainer when this lands.

## This skill's own code

`lib/`, `scripts/`, `SKILL.md`, `pins.json` and this file: MIT License.

```text
Copyright (c) 2026 Ouroboros contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
