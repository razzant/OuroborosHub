#!/usr/bin/env python3
"""Select hosted suites and normalize legacy publications for read-only CI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# Owner Updates imports selected_suites with the standard library only; build_index needs PyYAML.
def run_git(args, directory):
    from scripts.build_index import run_git as run
    return run(args, directory)


def refresh_legacy_entries(root, changed_paths=None):
    from scripts.build_index import refresh_legacy_entries as refresh
    return refresh(root, changed_paths)


SUITES = {
    'perplexity': {'skills': ['perplexity'], 'tests': ['tests/test_perplexity.py'],
                   'dependencies': ['httpx', 'starlette']},
    'duckduckgo': {'skills': ['duckduckgo'], 'tests': ['tests/test_duckduckgo.py'],
                   'dependencies': ['httpx', 'starlette']},
    # The browser regressions mount the widget through the core's own module bridge in Chromium and WebKit:
    # catalog-check.yml gives this suite the pinned core, its runtime and both browsers.
    # SMART_LISTS_REQUIRE_BROWSERS turns a skip for want of Playwright or Chromium into a failure.
    'smart-lists': {'skills': ['smart_lists'], 'tests': ['skills/smart_lists/tests', 'tests/test_smart_lists_browser.py'],
                    'dependencies': ['starlette'], 'core_env': 'OUROBOROS_THEME_CORE_ROOT',
                    'env': {'SMART_LISTS_REQUIRE_BROWSERS': '1'}},
    # Offline: the suite fakes the model (no weights, no faster-whisper); PyAV and numpy decode real silent MP4 for
    # the length limits, and starlette serves the plugin route. The core consumer runs in the Telegram suites.
    'whisper-voice': {'skills': ['whisper-voice'], 'tests': ['skills/whisper-voice/tests'],
                      'dependencies': ['starlette', 'numpy', 'av>=17.1.0,<19'],
                      # WHISPER_REQUIRE_RUNTIME turns a skip for want of PyAV or NumPy into a failure.
                      'env': {'WHISPER_REQUIRE_RUNTIME': '1'}},
    'gigasearch': {'skills': ['gigasearch'], 'tests': ['skills/gigasearch/test_gigasearch.py']},
    'cache-efficiency': {'skills': ['cache_efficiency_snapshot'],
                         'tests': ['skills/cache_efficiency_snapshot', 'tests/test_cache_dashboard_browser.py'],
                         'dependencies': ['playwright==1.62.0'],
                         'env': {'CACHE_DASHBOARD_REQUIRE_RUNTIME': '1'}},
    # The dashboard cards through the core's own module bridge and theme in Chromium and WebKit, and once through its
    # historical pre-onTheme bridge (read with git show): catalog-check.yml gives this suite the pinned core with its
    # history, its runtime and both browsers. The browser harnesses' lease host (tests/widget_lease_gateway.py) is
    # checked against the core's lease controls here.
    'widgets': {'skills': ['cache_efficiency_snapshot', 'claudexor_quotas', 'context-lens', 'memory-atlas',
                           'token-usage', 'smart_lists', 'calendar', 'telegram-bot'],
                'tests': ['tests/test_widget_theme_contracts.py', 'tests/test_dashboard_theme_browser.py',
                          'tests/test_widget_lease_gateway.py'],
                'shared': ['tests/fixtures/dashboard_theme_responses.json', 'tests/widget_lease_gateway.py'],
                'core_env': 'OUROBOROS_THEME_CORE_ROOT'},
    # The quota widget's whole payload suite: its Node harnesses and its real Chromium and WebKit cases. catalog-check.yml
    # gives it Node and Playwright's browsers; QUOTAS_REQUIRE_RUNTIME turns a skip for want of them into a failure.
    'claudexor-quotas': {'skills': ['claudexor_quotas'], 'tests': ['skills/claudexor_quotas'],
                         'dependencies': ['playwright>=1.50,<2'], 'cwd': 'skills/claudexor_quotas',
                         'env': {'QUOTAS_REQUIRE_RUNTIME': '1'}},
    # The whole suite, real host and browsers included: catalog-check.yml gives this suite the pinned core (ATLAS_CORE),
    # its runtime and Playwright's browsers. Without them the suite stops with an error; it never skips. Locally it may
    # attach to a host already started from a core instead (ATLAS_HOST_ROOT, tests/memory_atlas/conftest.py).
    'memory-atlas': {'skills': ['memory-atlas'], 'tests': ['tests/memory_atlas'], 'dependencies': ['starlette'],
                     'core_env': 'ATLAS_CORE', 'attach_env': 'ATLAS_HOST_ROOT'},
    'calendar-skill': {'skills': ['calendar'],
                       'tests': ['skills/calendar/tests', 'tests/test_calendar_skill.py',
                                 'tests/test_calendar_host_browser.py'],
                       'dependencies': ['icalendar', 'python-dateutil', 'cryptography'],
                       'core_env': 'OUROBOROS_THEME_CORE_ROOT'},
    # Suites that list a Presence consumer (tests/test_*_core_consumer.py): catalog-check.yml gives them the pinned
    # core (OUROBOROS_PRESENCE_CORE_ROOT), and the consumers drive the connectors through its Presence routes.
    # Without that core the suite stops with an error; its consumers never skip. The bridges' own tests also check
    # what they send and hear with the core's validators in-process (core_imports): with the core first on their
    # PYTHONPATH those checks run, and a named core that does not itself provide one stops the suite instead of letting
    # the check skip or return.
    'slack-bridge': {'skills': ['slack-bridge'],
                     'tests': ['skills/slack-bridge/tests', 'tests/test_slack_conversation_core_consumer.py',
                               'tests/test_reaction_core_consumer.py', 'tests/test_gesture_core_consumer.py'],
                     'dependencies': ['httpx', 'starlette', 'websockets'],
                     'core_env': 'OUROBOROS_PRESENCE_CORE_ROOT',
                     'core_imports': ['ouroboros.place_intake:reaction_refusal', 'ouroboros.presence_delivery:validate_delivery',
                                      'ouroboros.contracts.skill_manifest:parse_skill_manifest_text',
                                      'ouroboros.extension_ui_validation:validate_contributions',
                                      'ouroboros.extension_ui_validation:validate_settings_schema']},
    'email-presence': {'skills': ['email-presence'], 'tests': ['skills/email-presence/tests'],
                       'dependencies': ['httpx', 'starlette', 'requests', 'requests-ntlm', 'defusedxml'], 'cwd': 'skills/email-presence',
                       'core_env': 'OUROBOROS_SOURCE_DIR', 'env': {'OUROBOROS_RUN_MAIL_BROWSER': '1'}},
    'freshdesk': {'skills': ['freshdesk'], 'tests': ['skills/freshdesk/tests', 'tests/test_freshdesk_core_consumer.py'],
                  'dependencies': ['httpx', 'starlette', 'websockets'], 'core_env': 'OUROBOROS_PRESENCE_CORE_ROOT'},
    'agentrq-bridge': {'skills': ['agentrq-bridge'], 'tests': ['skills/agentrq-bridge/tests'],
                      'dependencies': ['starlette']},
    # Its owner-account contract runs through the pinned core's places module (OUROBOROS_IDENTITY_CORE_ROOT).
    'github': {'skills': ['github'], 'tests': ['skills/github/tests'],
               'dependencies': ['PyJWT[crypto]>=2.13,<3', 'httpx', 'starlette'],
               'core_env': 'OUROBOROS_IDENTITY_CORE_ROOT'},
    'telegram-bot': {'skills': ['telegram-bot'], 'tests': ['skills/telegram-bot/tests', 'tests/test_reaction_core_consumer.py',
                                                           'tests/test_gesture_core_consumer.py',
                                                           'tests/test_voice_attachment_core_consumer.py',
                                                           'tests/test_telegram_bot_media_core_consumer.py',
                                                           'tests/test_telegram_bot_typing_core_consumer.py',
                                                           # The card mounts through that core's real module bridge.
                                                           'tests/test_telegram_bot_widget_browser.py'],
                     'dependencies': ['httpx', 'starlette', 'websockets', 'telethon>=1.45,<2', 'PySocks>=1.7.1',
                                      'playwright==1.62.0'],
                     'env': {'TRANSPORT_WIDGET_REQUIRE_BROWSERS': '1'},
                     # The real-core voice consumer drives whisper-voice's attachment adapter; the card's browser
                     # test answers its route lease through the shared lease host.
                     'shared': ['skills/whisper-voice/attachment_tool.py', 'tests/widget_lease_gateway.py'],
                     'core_env': 'OUROBOROS_PRESENCE_CORE_ROOT',
                     'core_imports': ['ouroboros.places:connect_place', 'ouroboros.places:edit_place',
                                      'ouroboros.places:get_place', 'ouroboros.place_intake:decide_attention',
                                      'ouroboros.place_intake:reaction_refusal', 'ouroboros.presence_delivery:validate_delivery',
                                      'ouroboros.contracts.skill_manifest:parse_skill_manifest_text',
                                      'ouroboros.extension_contributions:project_contributions',
                                      'ouroboros.extension_ui_validation:validate_contributions',
                                      'ouroboros.extension_registry_state:live_contribution_projection',
                                      'ouroboros.presence_admission:admit_presence_turn',
                                      'ouroboros.presence_context:focus_metadata',
                                      'ouroboros.presence_runner:PresenceTurnEvent',
                                      'ouroboros.presence_runner:capture_presence_event']},
    # Its participant widget mounts through the core's own module bridge in Chromium and WebKit
    # (OUROBOROS_THEME_CORE_ROOT), beside the Presence consumer: catalog-check.yml gives it the runtime and both browsers.
    'telegram-user-session': {'skills': ['telegram-user-session'],
                              'tests': ['skills/telegram-user-session/tests', 'tests/test_telegram_participant_widget_browser.py',
                                        'tests/test_voice_attachment_core_consumer.py'],
                              'shared': ['tests/widget_lease_gateway.py', 'skills/whisper-voice/attachment_tool.py'],
                              'dependencies': ['telethon>=1.45,<2', 'PySocks>=1.7.1', 'httpx', 'starlette'],
                              'core_env': ('OUROBOROS_PRESENCE_CORE_ROOT', 'OUROBOROS_THEME_CORE_ROOT')},
    '2gis': {'skills': ['2gis'], 'tests': ['tests/test_2gis.py']},
    'keenable': {'skills': ['keenable'], 'tests': ['tests/test_keenable_transport.py', 'tests/test_keenable_widget.py'],
                 'shared': ['tests/fixtures/keenable/']},
    'whoop-health': {'skills': ['whoop-health'], 'tests': ['tests/test_whoop_recovery.py']},
    'office-reader': {'skills': ['office_reader'], 'tests': ['skills/office_reader/tests'],
                      'dependencies': ['markitdown[docx,pptx,xlsx,xls,outlook]>=0.1.8,<0.2', 'openpyxl>=3.1.5,<3.2',
                                       'python-docx', 'python-pptx', 'xlwt']},
    'science-search': {'skills': ['science_search'], 'tests': ['skills/science_search/tests'],
                       'dependencies': ['httpx>=0.28.1,<0.29']},
    'science-digest': {'skills': ['science_digest'], 'tests': ['skills/science_digest/test_digest.py']},
    # Node's own tests (node --test) behind one pytest wrapper, no browser. catalog-check.yml gives this suite Node and
    # the exact npm closure skills/chrome_browser/pins.json records (OUROBOROS_CHROME_NODE_MODULES). Without them the
    # files that need them fail and say what is missing; they never skip.
    'chrome-browser': {'skills': ['chrome_browser'], 'tests': ['tests/chrome_browser']},
    # The whole Anime Studio payload suite, offline: provider traffic is an in-memory transport and sockets are refused.
    # Its assembly cases encode synthetic clips with the real ffmpeg. catalog-check.yml installs it; without ffmpeg,
    # ffprobe and these encoders the suite stops with an error, so those cases run and never skip.
    'anime-studio': {'skills': ['anime_studio'], 'tests': ['skills/anime_studio/tests'],
                     'dependencies': ['httpx', 'Pillow', 'starlette'], 'ffmpeg_encoders': ['libx264', 'aac']},
    # The provider keys widget's issuer-binding regressions: purely synthetic settings and an
    # intercepted HTTP boundary keep the suite offline; it needs nothing beyond the interpreter.
    'provider-keys': {'skills': ['provider_keys'], 'tests': ['tests/test_provider_keys.py']},
    # The Amberkeep regressions: invented Portable Memory packages built in the test,
    # so the suite is offline and needs nothing beyond the interpreter. The reader's
    # own test, the CLI's flow test and the offline export readers' test travel
    # together, so every Amberkeep test runs in CI.
    'amberkeep': {'skills': ['amberkeep'],
                  'tests': ['tests/test_amberkeep_portable.py', 'tests/test_amberkeep_flow.py',
                            'tests/test_amberkeep_readers.py']},
    'table-tools': {'skills': ['table_tools'], 'tests': ['skills/table_tools/tests'],
                    'dependencies': ['openpyxl']},
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


def ffmpeg_gaps(encoders):
    """What the payload's own skip conditions check: ffmpeg and ffprobe on PATH, then each encoder ffmpeg lists."""
    tools = {tool: shutil.which(tool) for tool in ('ffmpeg', 'ffprobe')}
    if missing := [f'{tool} on PATH' for tool, path in tools.items() if not path]:
        return missing
    listed = subprocess.run([tools['ffmpeg'], '-hide_banner', '-encoders'], capture_output=True, text=True,
                            timeout=60).stdout
    return [f'the {encoder} encoder' for encoder in encoders if f' {encoder} ' not in listed]


CORE_FILES = ('ouroboros/gateway/host_service.py', 'web/provider-return.html')
# Prints, as JSON, each `module:name` argument (after the JSON list of named core roots) that the interpreter's import
# path cannot provide from one of those roots, with the reason: a module or name found in another tree is a gap too.
CORE_IMPORT_PROBE = '''import importlib, json, pathlib, sys
roots = [pathlib.Path(root).resolve() for root in json.loads(sys.argv[1])]
def outside(module):
    path = getattr(module, "__file__", None)
    path = pathlib.Path(path).resolve() if path else None
    return None if path and any(path.is_relative_to(root) for root in roots) else str(path)
missing = {}
for spec in sys.argv[2:]:
    module, _, name = spec.partition(":")
    try:
        loaded = importlib.import_module(module)
        found = getattr(loaded, name, None) if name else None
        owner = sys.modules.get(getattr(found, "__module__", None) or "") or loaded
        if outside(loaded) is not None:
            missing[spec] = "imported from " + outside(loaded) + ", outside the named core"
        elif name and not hasattr(loaded, name):
            missing[spec] = "absent"
        elif outside(owner) is not None:
            missing[spec] = "defined in " + outside(owner) + ", outside the named core"
    except Exception as exc:
        missing[spec] = type(exc).__name__ + ": " + str(exc)
print(json.dumps(missing))
'''


def core_envs(suite):
    """The variables naming the pinned core for a suite's tests: `core_env` holds one name or several."""
    names = suite.get('core_env', ())
    return (names,) if isinstance(names, str) else tuple(names)


def core_checkout(name, suite, core_env, env):
    """The core a suite's tests read, resolved, and whether the suite attaches to a host started from one. A missing
    or partial core stops the suite: its real-core tests must run, not skip."""
    attach = suite.get('attach_env')
    if attach and env.get(attach) and not env.get(core_env):
        core, named = Path(env[attach]).resolve() / 'repo', attach  # the host's own copy of its core
    else:
        core, named = (Path(env[core_env]).resolve() if env.get(core_env) else None), core_env
    if core is None or not all((core / path).is_file() for path in CORE_FILES):
        raise RuntimeError(f'{name} requires {named} to name the coordinated core checkout; '
                           'its real-core consumers must run, not skip')
    return core, named == attach


def core_import_gaps(imports, env, cores):
    """{`module:name`: why} for what the suite's tests import from the core in-process and this environment does not
    provide from the named core checkouts `cores`."""
    probe = subprocess.run([sys.executable, '-B', '-c', CORE_IMPORT_PROBE, json.dumps(cores), *imports], env=env,
                           capture_output=True, text=True, timeout=300)
    try:
        gaps = json.loads(probe.stdout.strip().splitlines()[-1]) if probe.returncode == 0 else None
    except (IndexError, ValueError):
        gaps = None
    return gaps if isinstance(gaps, dict) else {spec: 'the import probe failed: ' + probe.stderr.strip()[-300:]
                                                for spec in imports}


def run_suite(name):
    suite = SUITES[name]
    env = {**os.environ, **suite.get('env', {}), 'PYTHONDONTWRITEBYTECODE': '1'}
    cores = []
    for core_env in core_envs(suite):
        core, attached = core_checkout(name, suite, core_env, env)
        if not attached:
            env[core_env] = str(core)
        cores.append(str(core))
    if cores:
        # The skill tests that import the core themselves get the same core, ahead of any inherited PYTHONPATH entry
        # (their own directories still come first: the working directory and the paths pytest and the tests insert).
        env['PYTHONPATH'] = os.pathsep.join(dict.fromkeys(filter(None, [*cores,
                                                                         *env.get('PYTHONPATH', '').split(os.pathsep)])))
    if 'ffmpeg_encoders' in suite and (gaps := ffmpeg_gaps(suite['ffmpeg_encoders'])):
        raise RuntimeError(f'{name} requires {", ".join(gaps)}; its real ffmpeg cases must run, not skip')
    subprocess.run([sys.executable, '-B', '-m', 'pip', 'install', 'PyYAML', 'pytest',
                    *suite.get('dependencies', [])], check=True)
    if suite.get('core_imports') and (gaps := core_import_gaps(suite['core_imports'], env, cores)):
        raise RuntimeError(f'{name} requires the named core to provide '
                           f'{", ".join(f"{spec} ({why})" for spec, why in gaps.items())}; '
                           'its in-process core checks must run, not skip')
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
