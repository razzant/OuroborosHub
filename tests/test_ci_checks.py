"""Changed-path suite selection and offline workflow execution contracts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts import ci_checks as ci
from scripts.build_index import run_git

ROOT = Path(__file__).resolve().parents[1]


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

    def test_metadata_prs_skip_hosted_suites_and_payload_prs_select_their_consumers(self):
        self.assertEqual(ci.selected_suites(set()), [])
        self.assertEqual(ci.selected_suites({'README.md', 'entries/calendar.json', 'official.json', 'catalog.json'}), [])
        consumers = {
            'smart_lists': {'smart-lists', 'widgets'}, 'gigasearch': {'gigasearch'},
            'cache_efficiency_snapshot': {'cache-efficiency', 'widgets'}, 'calendar': {'calendar-skill', 'widgets'},
            'slack-bridge': {'slack-bridge'}, 'telegram-bot': {'telegram-bot'}, 'keenable': {'keenable'},
            'perplexity': {'perplexity'}, '2gis': {'2gis'}, 'whoop-health': {'whoop-health'}, 'claudexor_quotas': {'widgets'},
            'context-lens': {'widgets'}, 'memory-atlas': {'widgets'}, 'token-usage': {'widgets'},
            'office_reader': {'office-reader'}, 'science_search': {'science-search'},
            'github-presence': {'github-presence'},
            'telegram-user-session': {'telegram-user-session'},
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
        for name, suite in ci.SUITES.items():
            with self.subTest(suite=name), patch.object(ci.subprocess, 'run') as run:
                ci.run_suite(name)
                install, test = run.call_args_list
                self.assertEqual(install.args[0][1:6], ['-B', '-m', 'pip', 'install', 'PyYAML'])
                self.assertTrue(set(suite.get('dependencies', [])) <= set(install.args[0]))
                self.assertEqual(test.args[0][1:8], ['-B', '-m', 'pytest', '-p', 'no:cacheprovider', '-q', '-rs'])
                self.assertEqual(test.args[0][8:], suite['tests'])
                self.assertTrue(install.kwargs['check'])
                self.assertTrue(test.kwargs['check'])
                self.assertEqual(test.kwargs['env']['PYTHONDONTWRITEBYTECODE'], '1')

