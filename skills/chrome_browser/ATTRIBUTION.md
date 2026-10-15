# Attribution

**This payload** — the extension, the executor, the setup script and the wire
contract — is original source written for OuroborosHub. No code was copied
from another browser-automation project.

**License.** MIT.

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

**Dependency.** [`ws`](https://github.com/websockets/ws) 8.22.0 (MIT,
Copyright (c) 2011 Einar Otto Stangvik and contributors) is installed by the
host into the skill's isolated `.ouroboros_env`; it is not vendored here.

**Extension key.** `extension/manifest.json` carries a public key so that the
unpacked extension has the same id on every machine. Its private half was
discarded when it was generated; nothing is signed with it.

**Design sources.** The split into one persistent executor with typed steps
follows the owner's comparison of existing browser agents on one public pull
request; the protocols those products use were observed, not reused.
