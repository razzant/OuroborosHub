"""Changed-path suite selection and offline workflow execution contracts."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts import ci_checks as ci
from scripts.build_index import run_git

ROOT = Path(__file__).resolve().parents[1]


def package_name(spec):
    """The distribution name of a pip requirement, without its version range or extras."""
    for separator in ('[', ';', '<', '>', '=', '!', '~', ' '):
        spec = spec.split(separator)[0]
    return spec.strip().lower()


@contextmanager
def consumer_core():
    with tempfile.TemporaryDirectory(prefix='hub-consumer-core-') as tmp:
        core = Path(tmp)
        for name in ('ouroboros/gateway/host_service.py', 'web/provider-return.html'):
            path = core / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture', encoding='utf-8')
        with patch.dict(os.environ, {'OUROBOROS_SOURCE_DIR': str(core), 'OUROBOROS_THEME_CORE_ROOT': str(core),
                                     'OUROBOROS_PRESENCE_CORE_ROOT': str(core)}):
            yield core


# Two rows in the layout `ffmpeg -hide_banner -encoders` prints.
LIBX264 = ' V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)\n'
AAC = ' A....D aac                  AAC (Advanced Audio Coding)\n'


@contextmanager
def synthetic_ffmpeg(table=LIBX264 + AAC, tools=('ffmpeg', 'ffprobe')):
    """PATH names only these stand-ins; each prints `table` and encodes nothing."""
    with tempfile.TemporaryDirectory(prefix='hub-ffmpeg-') as tmp:
        for tool in tools:
            path = Path(tmp) / tool
            path.write_text(f'#!/bin/sh\nprintf %s {shlex.quote(table)}\n', encoding='utf-8')
            path.chmod(0o755)
        with patch.dict(os.environ, {'PATH': tmp}):
            yield


class SelectionTests(unittest.TestCase):
    def test_every_top_level_test_module_runs_in_ci(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/catalog-check.yml').read_text(encoding='utf-8'))
        scheduled = {path for suite in ci.SUITES.values() for path in suite['tests']}
        for step in workflow['jobs']['catalog']['steps']:
            command = shlex.split(step.get('run', ''))
            if 'pytest' in command:
                scheduled.update(command[command.index('pytest') + 1:])
        discovered = {path.relative_to(ROOT).as_posix() for path in (ROOT / 'tests').glob('test_*.py')}
        self.assertEqual(discovered - scheduled, set(),
                         'Add uncovered test modules to the catalog job or a hosted suite tests list')

    def test_cache_suite_includes_all_payload_regressions(self):
        self.assertIn('skills/cache_efficiency_snapshot', ci.SUITES['cache-efficiency']['tests'])
        for path in (ROOT / 'skills/cache_efficiency_snapshot').glob('test_*.py'):
            with self.subTest(path=path.name):
                self.assertIn('cache-efficiency', ci.selected_suites({path.relative_to(ROOT).as_posix()}))

    def test_metadata_prs_skip_hosted_suites_and_payload_prs_select_their_consumers(self):
        self.assertEqual(ci.selected_suites(set()), [])
        self.assertEqual(ci.selected_suites({'README.md', 'entries/calendar.json', 'official.json', 'catalog.json'}), [])
        consumers = {
            'smart_lists': {'smart-lists', 'widgets'}, 'gigasearch': {'gigasearch'},
            'cache_efficiency_snapshot': {'cache-efficiency', 'widgets'}, 'calendar': {'calendar-skill', 'widgets'},
            'slack-bridge': {'slack-bridge'}, 'telegram-bot': {'telegram-bot'}, 'keenable': {'keenable'},
            'perplexity': {'perplexity'}, 'duckduckgo': {'duckduckgo'}, '2gis': {'2gis'}, 'whoop-health': {'whoop-health'}, 'claudexor_quotas': {'widgets', 'claudexor-quotas'},
            'context-lens': {'widgets'}, 'memory-atlas': {'memory-atlas', 'widgets'}, 'token-usage': {'widgets'},
            'office_reader': {'office-reader'}, 'science_search': {'science-search'},
            'science_digest': {'science-digest'},
            'github': {'github'},
            'telegram-user-session': {'telegram-user-session'},
            'email-presence': {'email-presence'}, 'agentrq-bridge': {'agentrq-bridge'},
            'chrome_browser': {'chrome-browser'}, 'freshdesk': {'freshdesk'},
            'anime_studio': {'anime-studio'}, 'provider_keys': {'provider-keys'}, 'whisper-voice': {'whisper-voice'},
            'amberkeep': {'amberkeep'},
            'kirill-menshov-premium-black': {'premium-black'},
        }
        for skill, expected in consumers.items():
            with self.subTest(skill=skill):
                self.assertEqual(set(ci.selected_suites({f'skills/{skill}/plugin.py'})), expected)
                self.assertEqual(ci.selected_suites({f'skills/{skill}-unrelated/plugin.py'}), [])

    def test_suite_harness_and_shared_inputs_select_the_jobs_that_depend_on_them(self):
        for name, suite in ci.SUITES.items():
            for dependency in suite['tests'] + suite.get('shared', []):
                path = dependency if dependency.endswith('.py') or dependency.endswith('.json') else dependency.rstrip('/') + '/test_example.py'
                with self.subTest(suite=name, path=path):
                    self.assertIn(name, ci.selected_suites({path}))
        # The adapter is also what the real-core voice consumers in the Telegram suites drive; the rest of the
        # skill's payload is not.
        self.assertEqual(set(ci.selected_suites({'skills/whisper-voice/attachment_tool.py'})),
                         {'whisper-voice', 'telegram-bot', 'telegram-user-session'})
        self.assertEqual(ci.selected_suites({'skills/whisper-voice/whisper_runtime.py'}), ['whisper-voice'])
        # PyAV and NumPy are required in hosted CI: their absence fails the suite instead of skipping it.
        self.assertEqual(ci.SUITES['whisper-voice']['env'], {'WHISPER_REQUIRE_RUNTIME': '1'})
        self.assertEqual(ci.selected_suites({'tests/fixtures/keenable/new.json'}), ['keenable'])
        self.assertEqual(ci.selected_suites({'tests/fixtures/dashboard_theme_responses.json'}), ['widgets'])
        for path in ('.github/workflows/catalog-check.yml', 'scripts/ci_checks.py', 'scripts/build_index.py',
                     'scripts/release_digest.py', 'tests/test_ci_checks.py', 'tests/conftest.py'):
            with self.subTest(shared=path):
                self.assertEqual(set(ci.selected_suites({path})), set(ci.SUITES))

    def test_main_and_manual_runs_select_all_even_without_changed_paths(self):
        for event in ('push', 'workflow_dispatch'):
            with self.subTest(event=event), patch.object(ci, 'refresh_legacy_entries') as refresh:
                self.assertEqual(set(ci.prepare(Path('/synthetic'), event, {})), set(ci.SUITES))
                refresh.assert_called_once_with(Path('/synthetic'), None)

    def test_git_diff_includes_both_sides_of_a_cross_skill_rename(self):
        with tempfile.TemporaryDirectory(prefix='hub-paths-') as tmp:
            root = Path(tmp)
            def git(*args):
                return run_git(['-c', 'user.name=Test', '-c', 'user.email=test@example.test', *args], root)
            git('init', '-q', '-b', 'main')
            old, new = root / 'skills/slack-bridge/file.py', root / 'skills/telegram-bot/file.py'
            old.parent.mkdir(parents=True)
            new.parent.mkdir(parents=True)
            old.write_text('payload\n', encoding='utf-8')
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            old.rename(new)
            git('add', '.')
            git('commit', '-qm', 'move')
            paths = ci.changed_paths(root, {'pull_request': {'base': {'sha': base}, 'head': {'sha': git('rev-parse', 'HEAD')}}})
            self.assertEqual(paths, {'skills/slack-bridge/file.py', 'skills/telegram-bot/file.py'})
            self.assertEqual(set(ci.selected_suites(paths)), {'slack-bridge', 'telegram-bot'})

    def test_prepare_writes_matrix_output_for_the_workflow(self):
        with tempfile.TemporaryDirectory(prefix='hub-ci-output-') as tmp:
            event, output = Path(tmp) / 'event.json', Path(tmp) / 'output'
            event.write_text('{}', encoding='utf-8')
            with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_EVENT_PATH': str(event),
                                         'GITHUB_OUTPUT': str(output)}), patch.object(ci.sys, 'argv', ['ci_checks.py']), \
                    patch.object(ci, 'changed_paths', return_value={'skills/keenable/plugin.py'}), \
                    patch.object(ci, 'refresh_legacy_entries'):
                self.assertEqual(ci.main(), 0)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8').removeprefix('suites=')), ['keenable'])

    def test_selected_suites_install_dependencies_and_run_in_separate_interpreters(self):
        listed = subprocess.CompletedProcess([], 0, LIBX264 + AAC)
        for name, suite in ci.SUITES.items():
            with self.subTest(suite=name), consumer_core() as core, synthetic_ffmpeg(), \
                    patch.object(ci.subprocess, 'run', return_value=listed) as run:
                ci.run_suite(name)
                *probes, install, test = run.call_args_list
                self.assertEqual([probe.args[0][1:] for probe in probes],
                                 [['-hide_banner', '-encoders']] if 'ffmpeg_encoders' in suite else [])
                self.assertEqual(install.args[0][1:6], ['-B', '-m', 'pip', 'install', 'PyYAML'])
                self.assertTrue(set(suite.get('dependencies', [])) <= set(install.args[0]))
                self.assertEqual(test.args[0][1:8], ['-B', '-m', 'pytest', '-p', 'no:cacheprovider', '-q', '-rs'])
                expected_paths = ([str(ROOT / path) for path in suite['tests']]
                                  if 'cwd' in suite else suite['tests'])
                self.assertEqual(test.args[0][8:], expected_paths)
                self.assertEqual(test.kwargs['cwd'], ROOT / suite['cwd'] if 'cwd' in suite else None)
                self.assertTrue(install.kwargs['check'])
                self.assertTrue(test.kwargs['check'])
                self.assertEqual(test.kwargs['env']['PYTHONDONTWRITEBYTECODE'], '1')
                if suite.get('core_env'):
                    self.assertEqual(test.kwargs['env'][suite['core_env']], str(core.resolve()))
                for key, value in suite.get('env', {}).items():
                    self.assertEqual(test.kwargs['env'][key], value)

    def test_selected_consumers_refuse_absent_or_old_core_before_running_an_all_skipped_suite(self):
        for name, key in [('email-presence', 'OUROBOROS_SOURCE_DIR'), ('calendar-skill', 'OUROBOROS_THEME_CORE_ROOT'),
                          ('smart-lists', 'OUROBOROS_THEME_CORE_ROOT'),
                          ('slack-bridge', 'OUROBOROS_PRESENCE_CORE_ROOT'), ('telegram-bot', 'OUROBOROS_PRESENCE_CORE_ROOT'),
                          ('telegram-user-session', 'OUROBOROS_PRESENCE_CORE_ROOT'),
                          ('freshdesk', 'OUROBOROS_PRESENCE_CORE_ROOT')]:
            with self.subTest(suite=name), consumer_core() as core, patch.object(ci.subprocess, 'run') as run:
                for value in ('', str(core / 'missing')):
                    with patch.dict(os.environ, {key: value}), self.assertRaisesRegex(RuntimeError, key):
                        ci.run_suite(name)
                (core / 'web/provider-return.html').unlink()
                with self.assertRaisesRegex(RuntimeError, 'must run, not skip'):
                    ci.run_suite(name)
                run.assert_not_called()

    def test_anime_studio_runs_its_whole_payload_suite_only_with_the_ffmpeg_its_assembly_cases_need(self):
        self.assertEqual(ci.SUITES['anime-studio']['tests'], ['skills/anime_studio/tests'])
        for path in (ROOT / 'skills/anime_studio/tests').glob('test_*.py'):
            with self.subTest(path=path.name):
                self.assertEqual(ci.selected_suites({path.relative_to(ROOT).as_posix()}), ['anime-studio'])
        real_run, calls = subprocess.run, []

        def run(command, **kwargs):
            calls.append(command)
            if command[1:] == ['-hide_banner', '-encoders']:
                return real_run(command, **kwargs)  # the stand-in really runs and prints its table
            return subprocess.CompletedProcess(command, 0)  # pip and pytest are only recorded

        for table, tools, gap in [(LIBX264 + AAC, ('ffprobe',), 'ffmpeg on PATH'),
                                  (LIBX264 + AAC, ('ffmpeg',), 'ffprobe on PATH'),
                                  (AAC, ('ffmpeg', 'ffprobe'), 'the libx264 encoder'),
                                  (LIBX264, ('ffmpeg', 'ffprobe'), 'the aac encoder')]:
            calls.clear()
            with self.subTest(gap=gap), synthetic_ffmpeg(table, tools), \
                    patch.object(ci.subprocess, 'run', side_effect=run), \
                    self.assertRaisesRegex(RuntimeError, f'requires {gap}; its real ffmpeg cases must run, not skip'):
                ci.run_suite('anime-studio')
            self.assertEqual([command for command in calls if command[1:2] == ['-B']], [],
                             'neither pip nor pytest may start')
        calls.clear()
        with synthetic_ffmpeg(), patch.object(ci.subprocess, 'run', side_effect=run):
            ci.run_suite('anime-studio')
        probe, install, test = calls
        self.assertEqual(Path(probe[0]).name, 'ffmpeg')
        self.assertEqual(install[1:5], ['-B', '-m', 'pip', 'install'])
        self.assertEqual(test[8:], ['skills/anime_studio/tests'])

    def test_premium_black_lane_selects_the_payload_suite_and_agrees_with_the_manifest(self):
        suite = ci.SUITES['premium-black']
        self.assertEqual(suite['skills'], ['kirill-menshov-premium-black'])
        self.assertEqual(suite['tests'], ['skills/kirill-menshov-premium-black/tests'])
        # The payload directory and its CSS are the lane's own inputs; the generic
        # selection tests already cover test files and unrelated skill prefixes.
        self.assertEqual(ci.selected_suites({'skills/kirill-menshov-premium-black/tests'}), ['premium-black'])
        self.assertEqual(ci.selected_suites({'skills/kirill-menshov-premium-black/assets/deck.css'}), ['premium-black'])
        # The lane's runtime agrees with the manifest: it installs only what the payload declares,
        # and it covers the packages the offline build imports. Another dependency later is fine.
        manifest = (ROOT / 'skills/kirill-menshov-premium-black/SKILL.md').read_text(encoding='utf-8')
        for package in suite['dependencies']:
            with self.subTest(package=package):
                self.assertIn(package, manifest, 'the lane must test the versions the payload declares')
        installed = {package_name(package) for package in suite['dependencies']}
        for required in ('numpy', 'pillow'):
            with self.subTest(required=required):
                self.assertIn(required, installed,
                              'the offline build imports %s, so the lane must install it' % required)
        # A knob the lane sets is one the payload reads, so a later render lane stays possible.
        payload = '\n'.join(path.read_text(encoding='utf-8') for path in
                            (ROOT / 'skills/kirill-menshov-premium-black').rglob('*.py'))
        for key in suite.get('env', {}):
            with self.subTest(key=key):
                self.assertIn(key, payload, 'the lane may only set a knob the payload reads')

    def test_workflow_installs_ffmpeg_for_exactly_the_suites_that_require_it(self):
        self.assertEqual([name for name, suite in ci.SUITES.items() if 'ffmpeg_encoders' in suite], ['anime-studio'])
        workflow = yaml.safe_load((ROOT / '.github/workflows/catalog-check.yml').read_text(encoding='utf-8'))
        steps = workflow['jobs']['hosted-skills']['steps']
        install = next(step for step in steps if step.get('name') == "Install ffmpeg for anime-studio's assembly cases")
        self.assertEqual(install['if'], "matrix.suite == 'anime-studio'")
        self.assertLess(steps.index(install), len(steps) - 1)  # it precedes the suite run
        with tempfile.TemporaryDirectory(prefix='hub-ffmpeg-workflow-') as tmp:
            probes = Path(tmp) / 'probes'
            # Execute the actual shell step; substitute only sudo, which would reach the package manager.
            prelude = 'sudo() { printf "%s\\n" "$*" >> "$PROBES"; }\n'
            subprocess.run(['/bin/sh', '-c', prelude + install['run']], check=True, capture_output=True, text=True,
                           env={**os.environ, 'PROBES': str(probes)})
            self.assertEqual(probes.read_text(encoding='utf-8').splitlines(),
                             ['apt-get update', 'apt-get install --yes --no-install-recommends ffmpeg'])

    def test_payload_imports_work_in_the_actual_suite_subprocess(self):
        real_run = subprocess.run
        with tempfile.TemporaryDirectory(prefix='hub-suite-import-') as tmp:
            root = Path(tmp)
            payload = root / 'skills/email-presence'
            (payload / 'lib').mkdir(parents=True)
            (payload / 'tests').mkdir()
            (payload / 'lib/__init__.py').write_text('VALUE = 791\n', encoding='utf-8')
            (payload / 'tests/test_import.py').write_text(
                'import os\nfrom pathlib import Path\nfrom lib import VALUE\ndef test_value():\n'
                '    assert VALUE == 791\n'
                '    assert os.environ["OUROBOROS_RUN_MAIL_BROWSER"] == "1"\n'
                '    assert (Path(os.environ["OUROBOROS_SOURCE_DIR"]) / "web/provider-return.html").is_file()\n',
                encoding='utf-8')
            calls = []

            def run(command, **kwargs):
                calls.append((command, kwargs))
                if command[2:4] == ['-m', 'pip']:
                    return subprocess.CompletedProcess(command, 0)
                return real_run(command, **kwargs, capture_output=True, text=True)

            suite = dict(ci.SUITES['email-presence'])
            suite['tests'] = ['skills/email-presence/tests/test_import.py']
            with consumer_core(), patch.object(ci, '__file__', str(root / 'scripts/ci_checks.py')), \
                    patch.dict(ci.SUITES, {'email-presence': suite}), \
                    patch.dict(os.environ, {'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}), \
                    patch.object(ci.subprocess, 'run', side_effect=run):
                ci.run_suite('email-presence')
            self.assertEqual(calls[1][1]['cwd'], payload.resolve())
            self.assertIn('starlette', ci.SUITES['agentrq-bridge']['dependencies'])

