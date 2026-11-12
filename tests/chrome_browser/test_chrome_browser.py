"""chrome_browser regression tests that need no browser, run through pytest like every other hosted suite.

Node's own runner (node:test) runs the files next to this module: the request guard's wire and fail-closed
behaviour, the trusted-input preparations and delivery verdicts, a click of unknown effect sent once through the
pinned Playwright's own pointer path, the pinned patch against the stock bundle, and the connection and server over
Playwright's own consumers with a stand-in Chrome. README.md says where they came from and what was left out.

Node with --test-force-exit and --test-timeout must be on PATH (verified on 24, used in CI). The last three files need the npm files that
skills/chrome_browser/pins.json records (OUROBOROS_CHROME_NODE_MODULES; the hosted workflow installs it). Without it they
fail and say what to install. Nothing here skips.
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest

HERE = Path(__file__).resolve().parent
# Tests each file must still run. Node counts a file that registers none as one passing test, so an emptied file would pass.
MINIMUM_TESTS = {'unit_guard.test.mjs': 14, 'unit_input.test.mjs': 10, 'unit_click_once.test.mjs': 5, 'unit_patch_pinned.test.mjs': 9, 'unit_server.test.mjs': 57}
FILES = tuple(MINIMUM_TESTS)
ANSI = re.compile(r'\x1b\[[0-9;]*m')
# Node's spec reporter ends with "ℹ tests 29", "ℹ pass 29", ... (the symbol is a letter to \w, so it is matched as a word).
SUMMARY = re.compile(r'^(?:\S+\s+)?(tests|pass|fail|cancelled|skipped|todo)\s+(\d+)\s*$', re.M)


def test_every_node_test_file_is_scheduled():
    discovered = {path.name for path in HERE.glob('*.test.mjs')}
    assert discovered == set(FILES), 'Add new Node test files to MINIMUM_TESTS, and remove deleted ones'


@pytest.mark.parametrize('name', FILES)
def test_node_file_passes_without_skips(name):
    node = shutil.which('node')
    assert node, 'Node must be on PATH (CI uses 24); these tests are Node\'s own and do not skip without it'
    # --test-force-exit: a failed assertion can leave a mock server open; the run must end and say what failed.
    # --test-timeout: a test that never ends fails by name (the whole file gets 300 s) instead of hanging the job.
    try:
        run = subprocess.run([node, '--test', '--test-force-exit', '--test-timeout=60000', '--test-reporter=spec', str(HERE / name)],
                             capture_output=True, text=True, timeout=300, cwd=HERE)
    except subprocess.TimeoutExpired as hung:
        partial = hung.stdout.decode(errors='replace') if isinstance(hung.stdout, bytes) else (hung.stdout or '')
        pytest.fail(f'{name} did not finish within 300 s:\n{ANSI.sub("", partial)}')
    stdout = ANSI.sub('', run.stdout)
    shown = f'\n{stdout}\n{ANSI.sub("", run.stderr)}'
    assert run.returncode == 0, f'{name} failed (exit {run.returncode}):{shown}'
    found = {key: int(value) for key, value in SUMMARY.findall(stdout)}
    counts = {key: found.get(key, 0) for key in ('tests', 'pass', 'fail', 'cancelled', 'skipped', 'todo')}
    assert counts['tests'] >= MINIMUM_TESTS[name], \
        f'{name} ran {counts["tests"]} tests, at least {MINIMUM_TESTS[name]} expected (an emptied file, or a changed summary format):{shown}'
    # A skipped, todo or cancelled test is not a pass: this suite never skips.
    assert counts['pass'] == counts['tests'] and not (counts['fail'] or counts['cancelled'] or counts['skipped'] or counts['todo']), \
        f'{name}: {counts}{shown}'
