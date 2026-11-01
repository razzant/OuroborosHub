#!/usr/bin/env python3
"""Produce an unprivileged verdict; recheck it using trusted API data before merging.

The privileged path imports only this default-branch code and stdlib helpers. It
never downloads executable PR files, checks out a PR, or runs an artifact as code.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.github_api import APIError, GitHub

ARTIFACT = 'owner-update-verdict'
WORKFLOW = '.github/workflows/catalog-check.yml'
REQUIRED_JOBS = {'checks'}


class Ineligible(ValueError):
    pass


def changed_entry(files):
    entries = [f for f in files if re.fullmatch(r'entries/[^/]+\.json', f.get('filename', ''))]
    if len(entries) != 1 or entries[0].get('status') != 'modified':
        raise Ineligible('requires exactly one existing entry update')
    name = PurePosixPath(entries[0]['filename']).stem
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
        raise Ineligible('invalid entry name')
    for item in files:
        for path in (item['filename'], item.get('previous_filename', item['filename'])):
            parts = PurePosixPath(path).parts
            if ('..' in parts or '\\' in path or path != PurePosixPath(path).as_posix()
                    or (path != f'entries/{name}.json' and not path.startswith(f'skills/{name}/'))):
                raise Ineligible('changes include files outside this entry and its hosted payload')
    return name


def eligible(pr, files, base_entry, head_entry, official, repository):
    name = changed_entry(files)
    if (pr.get('state') != 'open' or pr.get('draft') or pr.get('merged')
            or pr['base']['ref'] != 'main' or pr['base']['repo']['full_name'] != repository):
        raise Ineligible('requires an open, ready PR targeting this main branch')
    if not base_entry or base_entry.get('name') != name or head_entry.get('name') != name:
        raise Ineligible('new or renamed entries require maintainer review')
    owner = base_entry.get('owner')
    if not isinstance(owner, str) or not owner or pr['user']['login'].casefold() != owner.casefold():
        raise Ineligible('PR author is not the owner recorded on main')
    if head_entry.get('owner') != owner:
        raise Ineligible('owner changes require maintainer review')
    if any(row.get('name') == name for row in official['endorsements']):
        raise Ineligible('Official releases require maintainer review')
    return name


def git_json(root, revision, path):
    data = subprocess.check_output(['git', 'show', f'{revision}:{path}'], cwd=root, timeout=30, stderr=subprocess.DEVNULL)
    return json.loads(data)


def make_verdict(root, event, run_id, attempt):
    pr = event['pull_request']
    verdict = {'schema_version': 1, 'repository': event['repository']['full_name'], 'pr_number': pr['number'],
               'head_sha': pr['head']['sha'], 'base_sha': pr['base']['sha'],
               'run_id': int(run_id), 'run_attempt': int(attempt), 'eligible': False, 'name': None}
    if not all(re.fullmatch(r'[0-9a-f]{40}', verdict[k]) for k in ('head_sha', 'base_sha')):
        raise ValueError('invalid PR commit identity')
    try:
        # --no-renames makes a rename across the permitted boundary visible as a deletion/addition.
        raw = subprocess.check_output(['git', 'diff', '--name-status', '-z', '--no-renames',
                                       f"{verdict['base_sha']}...{verdict['head_sha']}"], cwd=root, timeout=30)
        fields = raw.decode('utf-8').split('\0')[:-1]
        statuses = {'M': 'modified', 'A': 'added', 'D': 'removed', 'T': 'changed'}
        files = [{'status': statuses.get(fields[i], 'unknown'), 'filename': fields[i + 1]}
                 for i in range(0, len(fields), 2)]
        name = changed_entry(files)
        path = f'entries/{name}.json'
        base = git_json(root, verdict['base_sha'], path)
        head = git_json(root, verdict['head_sha'], path)
        official = git_json(root, verdict['base_sha'], 'official.json')
        verdict['name'] = eligible(pr, files, base, head, official, verdict['repository'])
        verdict['eligible'] = True
    except (Ineligible, subprocess.CalledProcessError, ValueError, KeyError):
        # Ordinary maintainer PRs still validate; the artifact grants no authority.
        pass
    return verdict


def read_verdict(archive):
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        infos = bundle.infolist()
        if len(infos) != 1 or infos[0].filename != 'verdict.json' or infos[0].file_size > 16_384:
            raise Ineligible('unexpected verdict archive contents')
        verdict = json.loads(bundle.read(infos[0]).decode('utf-8'))
    fields = {'schema_version', 'repository', 'pr_number', 'head_sha', 'base_sha',
              'run_id', 'run_attempt', 'eligible', 'name'}
    if not isinstance(verdict, dict) or set(verdict) != fields or type(verdict['schema_version']) is not int or verdict['schema_version'] != 1:
        raise Ineligible('invalid verdict schema')
    for key in ('pr_number', 'run_id', 'run_attempt'):
        if type(verdict[key]) is not int or verdict[key] <= 0:
            raise Ineligible('invalid verdict identity')
    if verdict['eligible'] is not True:
        raise Ineligible('validation did not nominate an owner update')
    for key in ('head_sha', 'base_sha'):
        if not isinstance(verdict[key], str) or not re.fullmatch(r'[0-9a-f]{40}', verdict[key]):
            raise Ineligible('invalid verdict SHA')
    return verdict


def merge_owner_update(api, event, repository):
    run_id = event['workflow_run']['id']
    prefix = f'/repos/{repository}'
    run = api.request('GET', f'{prefix}/actions/runs/{run_id}')
    if (run['event'] != 'pull_request' or run['status'] != 'completed' or run['conclusion'] != 'success'
            or run['path'].split('@', 1)[0] != WORKFLOW or run['repository']['full_name'] != repository):
        raise Ineligible('not a successful PR catalog validation run')
    jobs = list(api.pages(f'{prefix}/actions/runs/{run_id}/attempts/{run["run_attempt"]}/jobs', 'jobs'))
    passed = {j['name'] for j in jobs if j['status'] == 'completed' and j['conclusion'] == 'success'}
    if not REQUIRED_JOBS <= passed:
        raise Ineligible('required validation jobs are not all green')
    artifacts = [a for a in api.pages(f'{prefix}/actions/runs/{run_id}/artifacts', 'artifacts')
                 if a['name'] == ARTIFACT and not a['expired']]
    if len(artifacts) != 1 or artifacts[0]['size_in_bytes'] > 65_536:
        raise Ineligible('missing or ambiguous verdict artifact')
    artifact = artifacts[0]
    verdict = read_verdict(api.request('GET', f'{prefix}/actions/artifacts/{artifact["id"]}/zip', raw=True))
    if (verdict['repository'] != repository or verdict['run_id'] != run_id
            or verdict['run_attempt'] != run['run_attempt'] or verdict['head_sha'] != run['head_sha']):
        raise Ineligible('verdict is not bound to this validation run')
    number = verdict['pr_number']
    pr = api.request('GET', f'{prefix}/pulls/{number}')
    if (pr['head']['sha'] != verdict['head_sha']
            or pr['head']['repo']['full_name'] != run['head_repository']['full_name']):
        raise Ineligible('PR head changed since validation')
    associated = run.get('pull_requests') or []  # Fork runs can have an empty list.
    if associated and number not in {p['number'] for p in associated}:
        raise Ineligible('verdict refers to a different PR')
    files = list(api.pages(f'{prefix}/pulls/{number}/files'))
    if len(files) != pr['changed_files']:
        raise Ineligible('incomplete changed-file listing')
    name = changed_entry(files)
    if name != verdict['name']:
        raise Ineligible('entry name differs from the verdict')
    main_sha = api.request('GET', f'{prefix}/git/ref/heads/main')['object']['sha']
    path = f'entries/{name}.json'
    try:
        base = api.contents(repository, path, main_sha)
    except APIError as exc:
        if exc.status == 404:
            raise Ineligible('entry does not exist on main') from None
        raise
    head = api.contents(repository, path, verdict['head_sha'])
    official = api.contents(repository, 'official.json', main_sha)
    eligible(pr, files, base, head, official, repository)
    # Re-fetch metadata just before merging; the merge API itself compares the head SHA atomically.
    latest = api.request('GET', f'{prefix}/pulls/{number}')
    if latest['head']['sha'] != verdict['head_sha'] or latest['changed_files'] != len(files):
        raise Ineligible('PR changed during the ownership check')
    eligible(latest, files, base, head, official, repository)
    if api.request('GET', f'{prefix}/git/ref/heads/main')['object']['sha'] != main_sha:
        raise Ineligible('main changed during the ownership check; rerun validation')
    result = api.request('PUT', f'{prefix}/pulls/{number}/merge',
                         {'sha': verdict['head_sha'], 'merge_method': 'squash'})
    if not result.get('merged'):
        raise RuntimeError('GitHub declined the exact-head merge')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('verdict', 'merge'))
    parser.add_argument('--event', type=Path, default=Path(os.environ.get('GITHUB_EVENT_PATH', 'event.json')))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    event = json.loads(args.event.read_text(encoding='utf-8'))
    try:
        if args.mode == 'verdict':
            if args.output is None:
                parser.error('--output is required for verdict')
            verdict = make_verdict(Path(__file__).resolve().parents[1], event,
                                   os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT'])
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(verdict, sort_keys=True) + '\n', encoding='utf-8')
        else:
            number = merge_owner_update(GitHub(), event, os.environ['GITHUB_REPOSITORY'])
            print(f'Merged owner update #{number} at its validated head SHA')
    except Ineligible as exc:
        print(f'Maintainer review: {exc}')
    except (APIError, OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile):
        print('Owner-update processing failed; no unverified merge attempted', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
