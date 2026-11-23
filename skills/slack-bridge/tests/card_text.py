"""The card's own words for a status payload, from the real widget.js run in node (no browser, no DOM).

Only the kit's ``mount`` is replaced, in memory, by a hook that hands back the card's ``toModel``; every word the card
shows comes from the shipped file. One node process serves a whole batch of payloads.

Node is the one on PATH, else Playwright's bundled driver node. With ``TRANSPORT_WIDGET_REQUIRE_BROWSERS=1`` (the CI
job) a missing node fails the test instead of skipping it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

WIDGET = Path(__file__).resolve().parents[1] / "widget.js"

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
const value = (v) => Array.isArray(v) ? v.filter((p) => p && p.text).map((p) => p.text).join(' · ') : String(v);
const payloads = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(payloads.map((data) => {
    const model = ctx.__toModel(data);
    return { items: model.items.map((item) => ({ kind: item.kind, headline: item.headline, lines: item.lines.map(words) })),
             facts: model.facts.map((fact) => [fact[0], value(fact[1])]) };
})));
"""


def _node() -> str:
    found = shutil.which("node") or os.environ.get("OUROBOROS_PLAYWRIGHT_NODE_SELECTED") or ""
    if found and Path(found).exists():
        return found
    try:
        import playwright
    except ImportError:
        playwright = None
    if playwright is not None:
        bundled = Path(playwright.__file__).parent / "driver" / ("node.exe" if os.name == "nt" else "node")
        if bundled.exists():
            return str(bundled)
    if os.environ.get("TRANSPORT_WIDGET_REQUIRE_BROWSERS") == "1":
        pytest.fail("no node to run widget.js: neither on PATH nor Playwright's bundled driver")
    pytest.skip("node is not installed")
    return ""


def card_models(payloads: list[dict]) -> list[dict]:
    """For each payload, the card's attention items (kind, headline, lines in words) and its quiet facts."""
    done = subprocess.run([_node(), "-e", _RUNNER, str(WIDGET)], input=json.dumps(payloads), capture_output=True,
                          text=True, check=True, timeout=60)
    return json.loads(done.stdout)


def card_items(payloads: list[dict]) -> list[list[dict]]:
    """For each payload, the card's attention items: kind, headline and each line as the card words it."""
    return [model["items"] for model in card_models(payloads)]
