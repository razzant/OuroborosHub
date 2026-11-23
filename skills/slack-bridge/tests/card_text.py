"""The card's own words for a status payload, from the real widget.js run in node (no browser, no DOM).

Only the kit's ``mount`` is replaced, in memory, by a hook that hands back the card's ``toModel``; every word the card
shows comes from the shipped file. One node process serves a whole batch of payloads.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

WIDGET = Path(__file__).resolve().parents[1] / "widget.js"
NODE = shutil.which("node") or os.environ.get("OUROBOROS_PLAYWRIGHT_NODE_SELECTED") or ""

_RUNNER = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const hook = 'var K = TransportKit;';
if (!source.includes(hook)) throw new Error('widget.js no longer binds K = TransportKit');
const ctx = { Intl, Date, Math, JSON, String, Number, Array, Object, isFinite };
ctx.globalThis = ctx; ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(source.replace(hook, hook + ' K.mount = function (o) { globalThis.__toModel = o.toModel; };'), ctx);
const words = (line) => typeof line === 'string' ? line : [line.strong, line.text].filter(Boolean).join(' ');
const payloads = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(payloads.map((data) => ctx.__toModel(data).items.map((item) => ({
    kind: item.kind, headline: item.headline, lines: item.lines.map(words) })))));
"""


def card_items(payloads: list[dict]) -> list[list[dict]]:
    """For each payload, the card's attention items: kind, headline and each line as the card words it."""
    if not NODE or not Path(NODE).exists():
        pytest.skip("node is not installed")
    done = subprocess.run([NODE, "-e", _RUNNER, str(WIDGET)], input=json.dumps(payloads), capture_output=True,
                          text=True, check=True, timeout=60)
    return json.loads(done.stdout)


def card_text(items: list[dict]) -> str:
    return "\n".join([item["headline"], *item["lines"]][i] for item in items for i in range(len(item["lines"]) + 1))
