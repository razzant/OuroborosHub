#!/usr/bin/env python3
"""Select hosted suites and normalize legacy publications for read-only CI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_index import refresh_legacy_entries, run_git

SUITES = {
    'perplexity': {'skills': ['perplexity'], 'tests': ['tests/test_perplexity.py'],
                   'dependencies': ['httpx', 'starlette']},
    'smart-lists': {'skills': ['smart_lists'], 'tests': ['skills/smart_lists/tests', 'tests/test_smart_lists_browser.py'],
                    'dependencies': ['starlette']},
    'gigasearch': {'skills': ['gigasearch'], 'tests': ['skills/gigasearch/test_gigasearch.py']},
    'cache-efficiency': {'skills': ['cache_efficiency_snapshot'],
                         'tests': ['skills/cache_efficiency_snapshot/test_cache_efficiency.py']},
    'widgets': {'skills': ['cache_efficiency_snapshot', 'claudexor_quotas', 'context-lens', 'memory-atlas',
                           'token-usage', 'smart_lists', 'calendar'],
                'tests': ['tests/test_widget_theme_contracts.py', 'tests/test_dashboard_theme_browser.py'],
                'shared': ['tests/fixtures/dashboard_theme_responses.json']},
    # The whole suite, real host and browsers included: catalog-check.yml gives this suite the pinned core (ATLAS_CORE),
    # its runtime and Playwright's browsers. Without them the suite stops with an error; it never skips.
    'memory-atlas': {'skills': ['memory-atlas'], 'tests': ['tests/memory_atlas'], 'dependencies': ['starlette']},
    'calendar-skill': {'skills': ['calendar'],
                       'tests': ['skills/calendar/tests', 'tests/test_calendar_skill.py',
                                 'tests/test_calendar_host_browser.py'],
                       'dependencies': ['icalendar', 'python-dateutil', 'cryptography'],
                       'core_env': 'OUROBOROS_THEME_CORE_ROOT'},
    'slack-bridge': {'skills': ['slack-bridge'],
                     'tests': ['skills/slack-bridge/tests', 'tests/test_slack_presence_core_consumer.py',
                               'tests/test_reaction_core_consumer.py'],
                     'dependencies': ['httpx', 'starlette', 'websockets']},
    'email-presence': {'skills': ['email-presence'], 'tests': ['skills/email-presence/tests'],
                       'dependencies': ['httpx', 'starlette'], 'cwd': 'skills/email-presence',
                       'core_env': 'OUROBOROS_SOURCE_DIR', 'env': {'OUROBOROS_RUN_MAIL_BROWSER': '1'}},
    'agentrq-bridge': {'skills': ['agentrq-bridge'], 'tests': ['skills/agentrq-bridge/tests'],
                      'dependencies': ['starlette']},
    'github': {'skills': ['github'], 'tests': ['skills/github/tests'],
               'dependencies': ['PyJWT[crypto]>=2.13,<3', 'httpx', 'starlette']},
    'telegram-bot': {'skills': ['telegram-bot'], 'tests': ['skills/telegram-bot/tests', 'tests/test_reaction_core_consumer.py'],
                     'dependencies': ['httpx', 'starlette', 'websockets']},
    'telegram-user-session': {'skills': ['telegram-user-session'],
                              'tests': ['skills/telegram-user-session/tests', 'tests/test_telegram_participant_widget_browser.py'],
                              'dependencies': ['telethon>=1.45,<2', 'PySocks>=1.7.1', 'httpx', 'starlette']},
    '2gis': {'skills': ['2gis'], 'tests': ['tests/test_2gis.py']},
    'keenable': {'skills': ['keenable'], 'tests': ['tests/test_keenable_transport.py', 'tests/test_keenable_widget.py'],
                 'shared': ['tests/fixtures/keenable/']},
    'whoop-health': {'skills': ['whoop-health'], 'tests': ['tests/test_whoop_recovery.py']},
    'office-reader': {'skills': ['office_reader'], 'tests': ['skills/office_reader/tests'],
                      'dependencies': ['markitdown[docx,pptx,xlsx,xls,outlook]>=0.1.8,<0.2', 'openpyxl>=3.1.5,<3.2',
                                       'python-docx', 'python-pptx', 'xlwt']},
    'science-search': {'skills': ['science_search'], 'tests': ['skills/science_search/tests'],
                       'dependencies': ['httpx>=0.28.1,<0.29']},
}
SHARED = {'.github/workflows/catalog-check.yml', 'scripts/ci_checks.py', 'tests/test_ci_checks.py',
          'scripts/build_index.py', 'scripts/release_digest.py',
          'conftest.py', 'tests/conftest.py', 'pytest.ini', 'pyproject.toml'}


def selected_suites(paths):
    """None means a main push/manual run; an empty PR diff selects no suites."""
    if paths is None or SHARED.intersection(paths):
        return list(SUITES)
    return [name for name, suite in SUITES.items() if any(
        path == dependency or path.startswith(dependency.rstrip('/') + '/')
        for path in paths for dependency in (
            [f'skills/{skill}/' for skill in suite['skills']] + suite['tests'] + suite.get('shared', [])))]


def changed_paths(root, event):
    pr = event['pull_request']
    revision = f"{pr['base']['sha']}...{pr['head']['sha']}"
    raw = run_git(['diff', '--name-only', '-z', '--no-renames', revision, '--'], root)
    return set(raw.split('\0')) - {''}


def prepare(root, event_name, event):
    paths = changed_paths(root, event) if event_name == 'pull_request' else None
    refresh_legacy_entries(root, paths)
    return selected_suites(paths)


def run_suite(name):
    suite = SUITES[name]
    env = {**os.environ, **suite.get('env', {}), 'PYTHONDONTWRITEBYTECODE': '1'}
    if core_env := suite.get('core_env'):
        configured = env.get(core_env)
        core = Path(configured).resolve() if configured else None
        if core is None or not all((core / path).is_file() for path in (
                'ouroboros/gateway/host_service.py', 'web/provider-return.html')):
            raise RuntimeError(f'{name} requires {core_env} to name the coordinated core checkout; '
                               'its host and provider return consumers must run, not skip')
        env[core_env] = str(core)
    subprocess.run([sys.executable, '-B', '-m', 'pip', 'install', 'PyYAML', 'pytest',
                    *suite.get('dependencies', [])], check=True)
    # Selection paths stay repository-relative; some suites import from their own payload.
    root = Path(__file__).resolve().parents[1]
    cwd = root / suite['cwd'] if 'cwd' in suite else None
    tests = [str(root / path) for path in suite['tests']] if cwd else suite['tests']
    # Each skill gets its own interpreter; sibling test imports cannot collide.
    subprocess.run([sys.executable, '-B', '-m', 'pytest', '-p', 'no:cacheprovider', '-q', '-rs',
                    *tests], check=True, cwd=cwd,
                   env=env)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == 'run':
        run_suite(sys.argv[2])
        return 0
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    suites = prepare(Path(__file__).resolve().parents[1], os.environ['GITHUB_EVENT_NAME'], event)
    with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as output:
        output.write('suites=' + json.dumps(suites) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
