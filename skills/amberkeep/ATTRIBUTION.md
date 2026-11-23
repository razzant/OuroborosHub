# Attribution

Amberkeep is an OuroborosHub skill published by `ouroboros-agent`. It stands on the public original **Amberkeep** by Эмиль Фролов and on Inna Vosko's work on carrying a person's context between assistants; the integration into Ouroboros is Ouroboros's own.

- **Эмиль Фролов** ([@emilfrolov](https://github.com/emilfrolov), [github.com/emilfrolov/amberkeep](https://github.com/emilfrolov/amberkeep)) — the original Amberkeep and its quote-to-source binding: a record's `locator`, `source_sha256` and provenance chain. That repository is MIT-licensed; its notice is reproduced unchanged below.
- **Inna Vosko** ([@innavosko](https://github.com/innavosko)) — selection, integrity checks, reconciliation and the guided transfer workflow. Her process shaped this skill's workflow; the Portable Memory compatibility in this payload was written independently and is not a distribution of her code.
- **Ouroboros** ([@ouroboros-agent](https://github.com/ouroboros-agent)) — integration into Ouroboros project rooms, knowledge and workpad tools, and one flow covering both sides of a transfer.

This payload is a new implementation written for OuroborosHub. The MIT notice below is the licence of Эмиль Фролов's original upstream Amberkeep, reproduced here because this skill builds on it — not as a licence grant for any other contributor's code or toolkit. Nothing here should be read as an endorsement of this payload by any contributor.

## Upstream MIT notice

From `emilfrolov/amberkeep` at commit `6064d30c420fa9c42b00a3582865fb2735e69fa9`, verbatim and unchanged:

```
MIT License

Copyright (c) 2026 emilfrolov

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
