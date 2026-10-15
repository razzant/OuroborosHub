"""chrome_browser: what the package may contain, and an opt-in real-browser run.

The static checks need nothing but Python. The consumer run needs Node and a
full Chromium and is skipped unless its executable is named:

    OUROBOROS_CHROME_TEST_CHROMIUM=/path/to/chromium python -m unittest discover -s tests -p 'test_chrome_browser.py' -v
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'chrome_browser'
EXTENSION = SKILL / 'extension'
ALLOWED_CDP = {
    'Page.enable', 'Page.getFrameTree', 'Page.createIsolatedWorld', 'Page.navigate', 'Page.captureScreenshot',
    'Network.enable', 'Fetch.enable', 'Fetch.continueRequest', 'Fetch.failRequest', 'Fetch.fulfillRequest',
    'Runtime.callFunctionOn', 'Accessibility.getFullAXTree',
    'Input.dispatchMouseEvent', 'Input.dispatchKeyEvent', 'Input.insertText',
}


def source(name: str) -> str:
    return (EXTENSION / name).read_text(encoding='utf-8')


def frontmatter() -> str:
    return (SKILL / 'SKILL.md').read_text(encoding='utf-8').split('---', 2)[1]


class PackageTests(unittest.TestCase):
    def test_manifest_declares_a_node_script_skill_with_one_pinned_dependency(self):
        front = frontmatter()
        for line in ('name: chrome_browser', 'version: 0.1.2', 'type: script', 'runtime: node', 'permissions: [net, fs, subprocess]',
                     '  - name: mcp_server.mjs', '  - name: setup.mjs', '    package: ws@8.22.0'):
            self.assertIn(line, front)
        self.assertEqual(front.count('kind:'), 1)
        for absent in ('companion_processes', 'scheduled_tasks', 'entry:', 'ui_tab'):
            self.assertNotIn(absent, front)

    def test_payload_is_text_only_and_carries_no_installed_dependency(self):
        files = [path for path in SKILL.rglob('*') if path.is_file()]
        self.assertEqual({path.suffix for path in files}, {'.md', '.mjs', '.js', '.json', '.html'})
        self.assertFalse(any(part in {'node_modules', '.ouroboros_env'} for path in files for part in path.parts))
        for path in files:
            path.read_text(encoding='utf-8')

    def test_catalog_entry_matches_the_manifest(self):
        catalog = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
        entry = next(item for item in catalog['skills'] if item['slug'] == 'chrome_browser')
        self.assertEqual((entry['version'], entry['type']), ('0.1.2', 'script'))
        self.assertEqual(entry['install_specs'], [{'kind': 'npm', 'package': 'ws@8.22.0'}])
        self.assertEqual({item['path'] for item in entry['files']},
                         {path.relative_to(SKILL).as_posix() for path in SKILL.rglob('*') if path.is_file()})
        # The earlier instruction skill stays an instruction skill, at whatever version its own manifest declares.
        playwright = next(item for item in catalog['skills'] if item['slug'] == 'playwright_extension')
        declared = yaml.safe_load((ROOT / 'skills' / 'playwright_extension' / 'SKILL.md').read_text(encoding='utf-8').split('---', 2)[1])
        self.assertEqual((playwright['version'], playwright['type']), (str(declared['version']), 'instruction'))

    def test_mcp_initialize_version_matches_the_package(self):
        version = str(yaml.safe_load(frontmatter())['version'])
        server = (SKILL / 'scripts' / 'mcp_server.mjs').read_text(encoding='utf-8')
        self.assertIn(f"version: '{version}'", server)
        self.assertEqual(json.loads(source('manifest.json'))['version'], version)

    def test_extension_asks_for_three_permissions_and_no_site_access(self):
        manifest = json.loads(source('manifest.json'))
        self.assertEqual(manifest['manifest_version'], 3)
        self.assertEqual(manifest['version'], '0.1.2')
        self.assertEqual(manifest['permissions'], ['debugger', 'storage', 'alarms'])
        self.assertTrue(manifest['key'])
        for absent in ('host_permissions', 'optional_permissions', 'optional_host_permissions', 'content_scripts',
                       'externally_connectable', 'web_accessible_resources'):
            self.assertNotIn(absent, manifest)

    def test_extension_never_lists_tabs_takes_focus_or_uses_another_cdp_method(self):
        worker, popup = source('service_worker.js'), source('popup.js')
        for text in (worker, popup, source('page_agent.js')):
            for forbidden in ('tabs.update', 'chrome.windows', 'tabs.highlight', 'tabs.move', 'Page.bringToFront',
                              'Target.', 'Browser.', 'Storage.', 'Profiler.', 'Fetch.disable', 'chrome.scripting', 'eval('):
                self.assertNotIn(forbidden, text)
        # The only tab the extension creates is a background one, and it never asks which tabs exist.
        self.assertEqual(set(re.findall(r'chrome\.tabs\.(\w+)', worker)), {'create', 'get', 'remove', 'onUpdated', 'onRemoved'})
        self.assertEqual(re.findall(r'chrome\.tabs\.create\(([^)]*)\)', worker), ['{ url: message.url, active: false }'])
        self.assertNotIn('active: true', worker)
        self.assertEqual(worker.count('chrome.debugger.sendCommand('), 1)
        declared = re.search(r'const CDP_METHODS = new Set\(\[(.*?)\]\);', worker, re.S).group(1)
        self.assertEqual(set(re.findall(r"'([A-Za-z]+\.[A-Za-z]+)'", declared)), ALLOWED_CDP)
        events = {'Page.frameNavigated', 'Page.navigatedWithinDocument', 'Page.frameRequestedNavigation', 'Page.loadEventFired', 'Page.frameStartedLoading',
                  'Page.frameStoppedLoading', 'Page.javascriptDialogOpening', 'Page.javascriptDialogClosed', 'Fetch.requestPaused'}
        named = set(re.findall(r"'((?:Page|Network|Fetch|Runtime|Accessibility|Emulation|Input|DOM|CSS|Security)\.[A-Za-z]+)'", worker))
        self.assertEqual(named - events, ALLOWED_CDP)

    def test_popup_reads_only_the_tab_it_was_opened_on(self):
        popup = source('popup.js')
        self.assertEqual(re.findall(r'chrome\.tabs\.\w+\([^)]*\)', popup), ['chrome.tabs.query({ active: true, currentWindow: true })'])

    def test_page_code_installs_nothing_and_never_clicks_by_script(self):
        agent = source('page_agent.js')
        for forbidden in ('.click(', 'addEventListener', 'setTimeout', 'setInterval', 'requestAnimationFrame',
                          'MutationObserver', 'IntersectionObserver', 'fetch(', 'XMLHttpRequest', 'postMessage',
                          '.submit(', 'dispatchEvent'):
            self.assertNotIn(forbidden, agent)
        self.assertNotIn('requestAnimationFrame', source('service_worker.js'))
        self.assertNotIn('awaitPromise: true', source('service_worker.js'))

    def test_no_secret_reaches_a_tool_result_or_the_setup_output(self):
        executor = (SKILL / 'lib' / 'executor.mjs').read_text(encoding='utf-8')
        server = (SKILL / 'scripts' / 'mcp_server.mjs').read_text(encoding='utf-8')
        setup = (SKILL / 'scripts' / 'setup.mjs').read_text(encoding='utf-8')
        reported = executor[executor.index('  contractState() {'):executor.index('  requireHost() {')]
        self.assertEqual(set(re.findall(r'credential\w*', reported)), {'credential_present'})
        self.assertNotIn('credential', server)
        printed = setup[setup.index('function facts(pairing) {'):setup.index('function parsePort(text) {')]
        self.assertEqual(set(re.findall(r'credential\w*', printed)), {'credential_file', 'credential_present'})
        self.assertNotIn('pairing.credential', setup.replace('credential: pairing.credential', ''))

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_every_script_parses(self):
        for path in sorted(SKILL.rglob('*')):
            if path.suffix in {'.mjs', '.js'}:
                subprocess.run(['node', '--check', str(path)], check=True, capture_output=True)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_executor_lifecycle_without_a_browser(self):
        run = subprocess.run(['node', '--test', str(ROOT / 'tests' / 'chrome_browser' / 'executor.test.mjs')],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout[-6000:] + run.stderr[-2000:])

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_package_regressions_without_a_browser(self):
        run = subprocess.run(['node', '--test', '--test-force-exit', str(SKILL / 'tests' / 'regressions.test.mjs')],
                             capture_output=True, text=True, timeout=120, cwd=SKILL)
        self.assertEqual(run.returncode, 0, run.stdout[-6000:] + run.stderr[-2000:])

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_observation_startup_without_a_browser(self):
        run = subprocess.run(['node', '--test', str(SKILL / 'tests' / 'observation_startup.test.mjs')],
                             capture_output=True, text=True, timeout=120, cwd=SKILL)
        self.assertEqual(run.returncode, 0, run.stdout[-6000:] + run.stderr[-2000:])

    @unittest.skipUnless(shutil.which('node') and os.environ.get('OUROBOROS_CHROME_TEST_CHROMIUM'),
                         'set OUROBOROS_CHROME_TEST_CHROMIUM to a full Chromium executable to run the real-browser test')
    def test_real_extension_in_headless_chromium(self):
        run = subprocess.run(['node', '--test', str(ROOT / 'tests' / 'chrome_browser' / 'headless.test.mjs'),
                             str(ROOT / 'tests' / 'chrome_browser' / 'input_frame.test.mjs')],
                             capture_output=True, text=True, timeout=600)
        self.assertEqual(run.returncode, 0, run.stdout[-6000:] + run.stderr[-2000:])


if __name__ == '__main__':
    unittest.main()
