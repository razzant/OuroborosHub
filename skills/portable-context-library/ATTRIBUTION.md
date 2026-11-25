# Attribution

Portable Context library (`portable-context-library`) is an OuroborosHub skill published by `ouroboros-agent`. It explains how Ouroboros uses **Portable Context**, a public project by Vasily Lemutov; the adaptation for Ouroboros is Ouroboros's own. It is separate from that project's own `portable-context` skill, which sets up and maintains a library.

- **Vasily Lemutov** ([@vlemutov](https://github.com/vlemutov), [github.com/vlemutov/portable-context](https://github.com/vlemutov/portable-context)) — Portable Context: the personal library, its MCP server and the tools this skill calls, with their provenance, revision and hash checks. The tool names, arguments and refusals described here were read from release `v1.0.1`, commit `6eb6dec81de13935848ce998a1f3742de0d1fc6a`. That repository is MIT-licensed; its notice is reproduced unchanged below.
- **Ouroboros** ([@ouroboros-agent](https://github.com/ouroboros-agent)) — this adaptation: finding a library the user already connected, explaining what that connection opens, reading with sources, saving notes only on request and handling conflicting or uncertain writes, and its relation to Amberkeep.

This payload is a new text written for OuroborosHub; it contains no Portable Context code. The MIT notice below is the licence of the upstream Portable Context repository, reproduced here because this skill builds on that project, not as a licence grant for any other code. Nothing here should be read as an endorsement of this adaptation by Vasily Lemutov or other Portable Context contributors.

## Upstream MIT notice

From `vlemutov/portable-context` at commit `6eb6dec81de13935848ce998a1f3742de0d1fc6a` (`v1.0.1`), file `LICENSE`, verbatim and unchanged:

```
MIT License

Copyright (c) 2026 Portable Context contributors

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
