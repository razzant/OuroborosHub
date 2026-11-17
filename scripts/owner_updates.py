#!/usr/bin/env python3
"""Publish a recorded owner's Community skill update automatically after deterministic checks.

The privileged path runs default-branch helpers only. It fetches Git objects from main,
the PR's head ref and the commit Validate Catalog tested, and reads them without a
checkout: no PR file is executed or imported. Each merge attempt writes its own bot
comment, the public notice and its in-flight marker, and updates only that comment; a
hold run writes its own confirmation. Decisions read all of them.
"""
from __future__ import annotations

import argparse
import http.client
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
from scripts.ci_checks import selected_suites
from scripts.github_api import APIError, GitHub

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = '.github/workflows/catalog-check.yml'
RECEIPT = 'owner-update-receipt'
# Jobs of catalog-check.yml every PR attempt runs; each selected hosted suite adds its own job, named by the suite.
REQUIRED_JOBS = {'catalog', 'checks', 'receipt'}
DEFINITE = {'merged', 'not_applied', 'held'}
# A rerun repeats the run's tested merge (the same GITHUB_SHA); only a new validation tests a new merge with main.
RERUN = 'rerun all Validate Catalog jobs'
RETEST = ('start a new validation by pushing, Update branch, or Draft then Ready; a rerun repeats the same '
          'tested merge')
# Failures of a sent request: APIError is a RuntimeError; a lost response can raise http.client.HTTPException.
TRANSPORT = (RuntimeError, OSError, ValueError, http.client.HTTPException)
HOLD_LABEL = 'owner-update-hold'
BOT = 'github-actions[bot]'
REFS = 'refs/owner-update/'
MARKER = '<!-- ouroboros-hub:owner-update -->'
CONTROL = re.compile(r'^<!-- owner-update-control: (\{[^\n]*\}) -->$', re.M)
ACKNOWLEDGED = re.compile(r'^Maintainer acknowledgement \((\d{4}-\d{2}-\d{2})\): *(\S[^\n]*)$', re.M)
SHA = re.compile(r'[0-9a-f]{40}')
FILE_MODES = {'100644', '100755'}
STATUSES = {'M': 'modified', 'A': 'added', 'D': 'removed', 'T': 'changed'}
# Between the tested commit and main only payloads and the generated catalog may differ. Everything else
# (workflows, scripts, shared tests and fixtures, configuration, catalog policy) is CI control.
PAYLOAD_PREFIXES = ('entries/', 'skills/')
GENERATED = {'catalog.json'}
NOTICE = ('**Automatic Community publication.** Owner Updates merges a pull request automatically when it '
          'updates an existing Community skill by the owner recorded on main and the deterministic checks pass. '
          'Independent maintainer review is not required on that path, and merging does not grant Official '
          'endorsement. Maintainers can stop it with the `owner-update-hold` label and the Hold command '
          'described in the README.')
COMMIT_MESSAGE = ('Automatic Community owner update after deterministic checks. Independent maintainer review '
                  'was not required; Official endorsement is not granted.')


class Ineligible(ValueError):
    """Not an automatic owner update now; maintainer review or a rerun applies."""


class Held(Ineligible):
    pass


class Unknown(RuntimeError):
    """A merge request has no definite outcome; an operator reads back the named run."""


class NotApplied(RuntimeError):
    """Definitely no merge, but the run needs attention (refusal, stale notice, unconfirmed hold)."""


def git(root, *args, binary=False):
    result = subprocess.run(['git', '--literal-pathspecs', *args], cwd=root, capture_output=True, timeout=120,
                            check=True, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    return result.stdout if binary else result.stdout.decode('utf-8').strip()


def remote_url(repository):
    return f'https://github.com/{repository}.git'


def parse_raw_diff(raw):
    """Strictly parse `git diff --raw -z --no-renames --no-abbrev` output."""
    fields = raw.split(b'\0')
    if fields[-1] != b'' or len(fields) % 2 == 0:
        raise ValueError('malformed diff output')
    files = []
    for meta, path in zip(fields[0:-1:2], fields[1:-1:2]):
        match = re.fullmatch(rb':([0-7]{6}) ([0-7]{6}) [0-9a-f]{40} [0-9a-f]{40} ([A-Z])', meta)
        if not match:
            raise ValueError('malformed diff record')
        source, target, code = (group.decode('ascii') for group in match.groups())
        try:
            filename = path.decode('utf-8')
        except UnicodeDecodeError:
            raise Ineligible('changed paths must be UTF-8') from None
        if code not in STATUSES:
            raise Ineligible('unsupported change type')
        if (code == 'A') != (source == '000000') or (code == 'D') != (target == '000000'):
            raise ValueError('diff status contradicts its modes')
        if (target != '000000' and target not in FILE_MODES) or (source != '000000' and source not in FILE_MODES | {'120000'}):
            raise Ineligible('symlinks, submodules and other non-file modes require maintainer review')
        files.append({'status': STATUSES[code], 'filename': filename})
    return files


def diff_files(root, base, head):
    return parse_raw_diff(git(root, 'diff', '--raw', '-z', '--no-renames', '--no-abbrev', '--no-ext-diff',
                              '--no-textconv', base, head, '--', binary=True))


def changed_entry(files):
    entries = [f for f in files if re.fullmatch(r'entries/[^/]+\.json', f.get('filename', ''))]
    if len(entries) != 1 or entries[0].get('status') != 'modified':
        raise Ineligible('requires exactly one existing entry update')
    name = PurePosixPath(entries[0]['filename']).stem
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
        raise Ineligible('invalid entry name')
    for item in files:
        path = item['filename']
        parts = PurePosixPath(path).parts
        if ('..' in parts or '\\' in path or path != PurePosixPath(path).as_posix()
                or (path != f'entries/{name}.json' and not path.startswith(f'skills/{name}/'))):
            raise Ineligible('changes include files outside this entry and its hosted payload')
    return name


def held(pr):
    return any(isinstance(label, dict) and label.get('name') == HOLD_LABEL for label in pr.get('labels') or [])


def eligible(pr, files, base_entry, head_entry, official, repository):
    name = changed_entry(files)
    if (pr.get('state') != 'open' or pr.get('draft') or pr.get('merged')
            or pr['base']['ref'] != 'main' or pr['base']['repo']['full_name'] != repository):
        raise Ineligible('requires an open, ready PR targeting this main branch')
    if not base_entry or base_entry.get('name') != name or not head_entry or head_entry.get('name') != name:
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
    """The regular JSON file at revision, or None when it is absent."""
    listing = git(root, 'ls-tree', '-z', '--full-tree', revision, '--', path, binary=True)
    if not listing:
        return None
    meta, _, found = listing[:-1].partition(b'\t')
    mode, kind, oid = meta.decode('ascii').split(' ')
    if found.decode('utf-8') != path or mode not in FILE_MODES or kind != 'blob':
        raise Ineligible(f'{path} must be a regular file')
    return json.loads(git(root, 'cat-file', 'blob', oid, binary=True))


def selected(root, revision, name):
    """Mode, type and object of the entry file and payload tree, absence included."""
    return git(root, 'ls-tree', '-z', '--full-tree', revision, '--', f'entries/{name}.json', f'skills/{name}', binary=True)


def parents(root, commit):
    return git(root, 'rev-list', '--parents', '-n', '1', commit).split()[1:]


def is_ancestor(root, older, newer):
    return subprocess.run(['git', 'merge-base', '--is-ancestor', older, newer], cwd=root, capture_output=True,
                          timeout=120, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}).returncode == 0


def control_differences(root, tested, base):
    raw = git(root, 'diff', '--name-only', '-z', '--no-renames', '--no-ext-diff', '--no-textconv', tested, base, '--',
              binary=True)
    paths = [path.decode('utf-8', 'replace') for path in raw.split(b'\0') if path]
    return [path for path in paths if not path.startswith(PAYLOAD_PREFIXES) and path not in GENERATED]


def fetch(root, repository, *refspecs):
    if git(root, 'rev-parse', '--is-shallow-repository') != 'false':
        raise RuntimeError('the trusted checkout must have full history')
    git(root, 'fetch', '--quiet', '--no-tags', '--no-recurse-submodules', remote_url(repository), *refspecs)


def read_receipt(archive):
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        infos = bundle.infolist()
        if len(infos) != 1 or infos[0].filename != 'receipt.json' or infos[0].file_size > 16_384:
            raise Ineligible('unexpected receipt archive contents')
        receipt = json.loads(bundle.read(infos[0]).decode('utf-8'))
    fields = {'schema_version', 'repository', 'run_id', 'run_attempt', 'pr_number', 'head_sha', 'tested_sha',
              'workflow_ref', 'workflow_sha'}
    if not isinstance(receipt, dict) or set(receipt) != fields or type(receipt['schema_version']) is not int \
            or receipt['schema_version'] != 1:
        raise Ineligible('invalid receipt schema')
    for key in ('run_id', 'run_attempt', 'pr_number'):
        if type(receipt[key]) is not int or receipt[key] <= 0:
            raise Ineligible('invalid receipt identity')
    for key in ('head_sha', 'tested_sha', 'workflow_sha'):
        if not isinstance(receipt[key], str) or not SHA.fullmatch(receipt[key]):
            raise Ineligible('invalid receipt SHA')
    if not isinstance(receipt['repository'], str) or not isinstance(receipt['workflow_ref'], str):
        raise Ineligible('invalid receipt workflow identity')
    return receipt


# Bot comments. A comment is written only by its own run, by a maintainer, or after a merge is observed, so a
# late write of one attempt cannot erase another attempt's record.

def control_of(body):
    match = CONTROL.search(body)
    try:
        control = json.loads(match.group(1)) if match else None
    except ValueError:
        control = None
    return control if isinstance(control, dict) and isinstance(control.get('phase'), str) else {'phase': 'unreadable'}


def records(api, prefix, number):
    """Every owner-update comment of the workflow's bot on this PR, with its control record."""
    return [(c, control_of(c['body'])) for c in api.pages(f'{prefix}/issues/{number}/comments')
            if (c.get('user') or {}).get('login') == BOT and (c.get('user') or {}).get('type') == 'Bot'
            and str(c.get('body') or '').startswith(MARKER)]


def open_attempt(comment, control, hold=False):
    """Whether this record leaves a merge request's outcome open: `attempting`, an undated acknowledgement, or
    an unreadable or unrecognized record. A dated maintainer acknowledgement authorizes a new merge attempt
    only; a hold is confirmed past it once a merge is observed, never earlier."""
    phase = control.get('phase')
    if phase == 'acknowledged_unknown' and ACKNOWLEDGED.search(comment['body']):
        return hold
    if phase in DEFINITE:  # Earlier single-comment records listed acknowledged unknown attempts under `prior`.
        return hold and phase != 'merged' and bool(control.get('prior'))
    return True


def described(rows):
    return '; '.join(f"comment {comment.get('id')} (Owner Updates run {control.get('owner_run')}, head "
                     f"{control.get('head')})" for comment, control in rows)


def run_link(repository, pair):
    if not (isinstance(pair, list) and len(pair) == 2 and all(type(n) is int and n > 0 for n in pair)):
        return 'unknown'
    return f'[{pair[0]} attempt {pair[1]}](https://github.com/{repository}/actions/runs/{pair[0]}/attempts/{pair[1]})'


def status_text(record):
    phase, detail = record['phase'], record.get('detail') or ''
    if phase == 'attempting':
        text = ('A merge attempt for this head started. Until this line changes, its outcome is unknown: '
                'no later run merges or confirms a hold before a maintainer reads back the Owner Updates run below.')
        return text + (' The merge request was sent and its outcome is unknown.' if record.get('outcome') == 'unknown' else '')
    if phase == 'merged' and record.get('observed_only'):
        return (f"GitHub reports this PR merged (commit `{record.get('merge_commit') or 'unknown'}`). This was observed "
                'by a later run, not verified by Owner Updates: which request merged it, the merge method and the '
                'parents were not checked.')
    if phase == 'merged':
        text = f"Merged by merge commit `{record.get('merge_commit') or 'unknown'}`"
        # Merged is a fact; the kept commits are claimed only after Owner Updates read back the parents.
        text += (", which keeps the contribution's commits: its parents were read back." if record.get('commits_kept')
                 else ". Owner Updates did not confirm the contribution's head among its parents.")
        return text + (f' Post-merge verification failed: {detail}. A maintainer should inspect this merge.' if detail else '')
    if phase == 'held':
        return ('Automatic merging is held by a maintainer, confirmed by the Hold run below. To release it, remove '
                'the `owner-update-hold` label, then validate again: rerunning all Validate Catalog jobs repeats '
                'the same tested merge and fits only while main has not moved CI control files or this skill; '
                'pushing, Update branch, or Draft then Ready tests a new merge.')
    return f'No merge was applied: {detail}.'


def render(repository, record):
    lines = [MARKER, NOTICE, '', f'**Status:** {status_text(record)}', '',
             f"- Pull request head: `{record.get('head')}`"]
    if record.get('validation_run'):
        lines.append(f"- Validation run: {run_link(repository, record['validation_run'])}")
    label = 'Hold run' if record['phase'] == 'held' else 'Owner Updates run'
    lines.append(f"- {label}: {run_link(repository, record.get('owner_run'))}")
    control = json.dumps(record, sort_keys=True, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e')
    return '\n'.join(lines + ['', f'<!-- owner-update-control: {control} -->'])


def post(api, prefix, number, repository, record):
    """A new comment of this run; raises unless GitHub confirmed it with its id."""
    comment = api.request('POST', f'{prefix}/issues/{number}/comments', {'body': render(repository, record)})
    if not isinstance(comment, dict) or type(comment.get('id')) is not int:
        raise ValueError('GitHub did not confirm the comment')
    return comment


def settle(api, prefix, comment, body):
    """Best effort: update one comment. False means the update was not confirmed: the comment may keep its
    earlier text or still receive this one later, and no other comment is touched."""
    try:
        api.request('PATCH', f"{prefix}/issues/comments/{comment['id']}", {'body': body})
        return True
    except Exception:  # A failed outcome note never turns a definite outcome into another one.
        return False


def close_if_merged(api, prefix, number, repository, pr):
    """A positively observed merge closes every attempt marker still open, keeping a maintainer's
    acknowledgement line. The PR is merged for good, so no later decision depends on these comments."""
    for comment, control in records(api, prefix, number):
        if control.get('phase') in {'attempting', 'acknowledged_unknown'}:
            # Clears the marker only; the later run did not read the merge's method or parents.
            body = render(repository, {**control, 'phase': 'merged', 'merge_commit': pr.get('merge_commit_sha'),
                                       'outcome': None, 'detail': '', 'observed_only': True})
            acknowledgement = ACKNOWLEDGED.search(comment['body'])
            settle(api, prefix, comment, body + (f'\n{acknowledgement.group(0)}' if acknowledgement else ''))


def validation_run(api, prefix, repository, run_id, attempt):
    """The validation run, still a successful PR catalog validation at exactly this attempt."""
    run = api.request('GET', f'{prefix}/actions/runs/{run_id}')
    if (run.get('id') != run_id or run['event'] != 'pull_request' or run['status'] != 'completed'
            or run['conclusion'] != 'success' or run['path'].split('@', 1)[0] != WORKFLOW
            or run['repository']['full_name'] != repository):
        raise Ineligible('not a successful PR catalog validation run')
    if run['run_attempt'] != attempt:
        raise Ineligible('a newer validation attempt supersedes this event')
    return run


def missing_jobs(jobs, attempt, expected):
    """Expected job names this attempt's own job listing does not show as run and passed in it."""
    by_name = {}
    for job in jobs:
        by_name.setdefault(job.get('name'), []).append(job)
    def passed(job):  # A job naming another attempt is inherited, not run here.
        return (job.get('status') == 'completed' and job.get('conclusion') == 'success'
                and job.get('run_attempt', attempt) == attempt)
    return sorted(name for name in expected if not by_name.get(name) or not all(map(passed, by_name[name])))


def expected_jobs(files):
    """The fixed jobs and every hosted suite catalog-check.yml selects for these paths, by the same rule
    (scripts/ci_checks.py from main; CI control equals main, checked below). Hosted jobs are named by suite."""
    return REQUIRED_JOBS | set(selected_suites({item['filename'] for item in files}))


def merge_owner_update(api, event, repository, root=ROOT, owner_run=(0, 0)):
    prefix = f'/repos/{repository}'
    run_id, attempt = event['workflow_run']['id'], event['workflow_run']['run_attempt']
    if type(run_id) is not int or type(attempt) is not int:
        raise ValueError('invalid workflow_run identity')
    run = validation_run(api, prefix, repository, run_id, attempt)
    # The attempt-specific listing; never /jobs?filter=latest, which can mix in jobs of earlier attempts.
    jobs = list(api.pages(f'{prefix}/actions/runs/{run_id}/attempts/{attempt}/jobs', 'jobs'))
    if missing_jobs(jobs, attempt, REQUIRED_JOBS):
        raise Ineligible(f'catalog, checks and receipt did not all run and pass in this attempt; {RERUN}')
    artifacts = [a for a in api.pages(f'{prefix}/actions/runs/{run_id}/artifacts', 'artifacts')
                 if a['name'] == f'{RECEIPT}-{run_id}-{attempt}']
    if len(artifacts) != 1 or artifacts[0]['expired'] or artifacts[0]['size_in_bytes'] > 65_536:
        raise Ineligible('missing, expired or colliding receipt for this attempt')
    receipt = read_receipt(api.request('GET', f"{prefix}/actions/artifacts/{artifacts[0]['id']}/zip", raw=True))
    head, tested = receipt['head_sha'], receipt['tested_sha']
    if (receipt['repository'] != repository or receipt['run_id'] != run_id or receipt['run_attempt'] != attempt
            or head != run['head_sha'] or receipt['workflow_sha'] != tested
            or not receipt['workflow_ref'].startswith(f'{repository}/{WORKFLOW}@')):
        raise Ineligible('receipt is not bound to this validation attempt')
    number = receipt['pr_number']
    associated = run.get('pull_requests') or []  # Fork runs can have an empty list.
    if associated and number not in {p['number'] for p in associated}:
        raise Ineligible('receipt refers to a different PR')
    pr = api.request('GET', f'{prefix}/pulls/{number}')
    if pr['head']['sha'] != head or pr['head']['repo']['full_name'] != run['head_repository']['full_name']:
        raise Ineligible('PR head changed since validation')
    if pr.get('merged'):
        close_if_merged(api, prefix, number, repository, pr)
        raise Ineligible('PR is already merged')
    if held(pr):
        raise Held(f'the {HOLD_LABEL} label stops automatic merging')
    base = api.request('GET', f'{prefix}/git/ref/heads/main')['object']['sha']

    # Scope, policy and the tested tree come from immutable objects; nothing is checked out.
    fetch(root, repository, f'+refs/heads/main:{REFS}main', f'+refs/pull/{number}/head:{REFS}head',
          f'+{tested}:{REFS}tested')
    if git(root, 'rev-parse', f'{REFS}head^{{commit}}') != head:
        raise Ineligible('the fetched PR head differs from the validated head')
    if git(root, 'rev-parse', f'{REFS}main^{{commit}}') != base:
        raise Ineligible(f'main changed during the check; {RERUN}')
    bases = subprocess.run(['git', 'merge-base', '--all', base, head], cwd=root, capture_output=True, timeout=120,
                           env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}).stdout.decode('ascii').split()
    if len(bases) != 1:
        raise Ineligible('the branch needs one unique merge base with main; update the branch')
    files = diff_files(root, bases[0], head)
    name = changed_entry(files)
    path = f'entries/{name}.json'
    base_entry, head_entry = git_json(root, base, path), git_json(root, head, path)
    official = git_json(root, base, 'official.json')
    eligible(pr, files, base_entry, head_entry, official, repository)
    if selected(root, bases[0], name) != selected(root, base, name):
        raise Ineligible('main changed this skill since the branch point; update the branch')
    tested_parents = parents(root, tested)
    if len(tested_parents) != 2 or tested_parents[1] != head or not is_ancestor(root, tested_parents[0], base):
        raise Ineligible(f'the tested commit is not this head merged into main; {RETEST}')
    if selected(root, tested, name) != selected(root, head, name):
        raise Ineligible(f'the tested tree differs from this head for the skill; {RETEST}')
    if control_differences(root, tested, base):
        raise Ineligible(f'validation ran with CI control files that differ from current main; {RETEST}')
    if gaps := missing_jobs(jobs, attempt, expected_jobs(files)):
        raise Ineligible(f"selected validation jobs did not all run and pass in this attempt ({', '.join(gaps)}); "
                         f'{RERUN}')

    if unresolved := [row for row in records(api, prefix, number) if open_attempt(*row)]:
        raise Unknown(f'an earlier merge attempt has no definite outcome: {described(unresolved)}; read it back '
                      'and follow the README')
    record = {'phase': 'attempting', 'pr': number, 'head': head, 'validation_run': [run_id, attempt],
              'owner_run': list(owner_run)}
    try:
        comment = post(api, prefix, number, repository, record)
    except TRANSPORT:
        raise NotApplied('the public notice was not confirmed, so no merge request was sent; a notice of this run '
                         'appearing later shows an attempt that sent none') from None

    def note(**changes):  # This attempt's own comment only; False: not confirmed, it may still land there later.
        return settle(api, prefix, comment, render(repository, {**record, **changes}))

    # Final reads happen after the visible intent and immediately before the request.
    try:
        latest = api.request('GET', f'{prefix}/pulls/{number}')
        if latest['head']['sha'] != head:
            raise Ineligible('PR head changed during the check')
        if held(latest):
            raise Held(f'the {HOLD_LABEL} label stops automatic merging')
        eligible(latest, files, base_entry, head_entry, official, repository)
        if api.request('GET', f'{prefix}/git/ref/heads/main')['object']['sha'] != base:
            raise Ineligible(f'main changed during the check; {RERUN}')
        # Last read before the request: a rerun started meanwhile supersedes this attempt. A rerun starting
        # between this read and the request remains possible; the request still names the exact head.
        if validation_run(api, prefix, repository, run_id, attempt)['head_sha'] != head:
            raise Ineligible('the validation run changed during the check')
    except Exception as exc:
        reason = str(exc) if isinstance(exc, Ineligible) else 'the final checks could not complete'
        if not note(phase='not_applied', detail=reason):
            raise NotApplied(f'{reason}; its notice may still show an attempt, but run {owner_run[0]} sent no merge '
                             'request') from None
        if isinstance(exc, Ineligible):
            raise
        raise NotApplied(reason) from None

    print(f'Sending merge request: PR #{number} head {head}, Owner Updates run {owner_run[0]} attempt '
          f'{owner_run[1]}, validation run {run_id} attempt {attempt}', flush=True)
    try:
        result = api.request('PUT', f'{prefix}/pulls/{number}/merge', {
            'sha': head, 'merge_method': 'merge', 'commit_title': f'Merge owner update #{number}: {name}',
            'commit_message': COMMIT_MESSAGE})
    except APIError as exc:
        if 400 <= exc.status < 500 and exc.status != 408:
            reason = f'GitHub refused the merge (HTTP {exc.status})'
            note(phase='not_applied', detail=reason)
            raise NotApplied(reason) from None
        note(outcome='unknown')
        raise Unknown(f'the merge request outcome is unknown (HTTP {exc.status}); run {owner_run[0]} will not resubmit') from None
    except TRANSPORT:  # Includes a response lost after the request was sent (http.client.IncompleteRead).
        note(outcome='unknown')
        raise Unknown(f'the merge request outcome is unknown; run {owner_run[0]} will not resubmit') from None
    if not isinstance(result, dict) or result.get('merged') is not True:
        if isinstance(result, dict) and result.get('merged') is False:
            note(phase='not_applied', detail='GitHub declined the exact-head merge')
            raise NotApplied('GitHub declined the exact-head merge')
        note(outcome='unknown')
        raise Unknown('GitHub returned no definite merge outcome')
    merge_commit = result.get('sha')
    kept, failure = post_merge_audit(root, repository, merge_commit, base, head, name, base_entry['owner'])
    notice_updated = note(phase='merged', merge_commit=merge_commit, commits_kept=kept, detail=failure or '')
    return {'number': number, 'head': head, 'merge_commit': merge_commit, 'commits_kept': kept,
            'verification_failure': failure, 'notice_updated': notice_updated}


def post_merge_audit(root, repository, merge_commit, base, head, name, owner):
    """(commits_kept, failure); failure is None when verified. The merge has already happened: this
    detects, it never reverts. commits_kept is True only once the parents were read back with H second."""
    kept = False
    try:
        if not isinstance(merge_commit, str) or not SHA.fullmatch(merge_commit):
            return kept, 'GitHub did not report the merge commit'
        fetch(root, repository, f'+{merge_commit}:{REFS}merged')
        actual = parents(root, merge_commit)
        if len(actual) != 2 or actual[1] != head:
            return kept, 'the merge commit does not keep the validated head as its second parent'
        kept = True
        if actual[0] == base:
            return kept, None
        if not is_ancestor(root, base, actual[0]):
            return kept, 'the actual main parent does not descend from the inspected main'
        if selected(root, actual[0], name) != selected(root, base, name):
            return kept, f'main moved to {actual[0]} with a different version of this skill'
        entry, official = git_json(root, actual[0], f'entries/{name}.json'), git_json(root, actual[0], 'official.json')
        if not entry or entry.get('owner') != owner or any(row.get('name') == name for row in official['endorsements']):
            return kept, f'policy on the actual main parent {actual[0]} no longer allows this owner update'
        return kept, None
    except (subprocess.SubprocessError, RuntimeError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return kept, ('the actual main parent could not be checked' if kept
                      else 'the merge commit could not be read back')


def hold(api, repository, number, hold_run):
    """Serialized with merges: confirm a maintainer hold only when no earlier attempt's outcome is unknown.
    On a merged PR it records the merge as observed, which is how a stale `attempting` notice gets closed."""
    prefix = f'/repos/{repository}'
    pr = api.request('GET', f'{prefix}/pulls/{number}')
    if not pr.get('merged') and not held(pr):
        api.request('POST', f'{prefix}/issues/{number}/labels', {'labels': [HOLD_LABEL]})
        pr = api.request('GET', f'{prefix}/pulls/{number}')
    if pr.get('merged'):
        close_if_merged(api, prefix, number, repository, pr)
        raise NotApplied(f"PR #{number} was merged before the hold (merge commit {pr.get('merge_commit_sha')})")
    if not held(pr):
        raise NotApplied(f'the {HOLD_LABEL} label was not observed on PR #{number}; hold not confirmed')
    if pr.get('state') != 'open':
        raise NotApplied(f'PR #{number} is closed; the label stays and blocks automation if it is reopened')
    # Every attempt's own record counts: a definite later attempt does not hide an unknown one, and a maintainer
    # acknowledgement authorizes a new merge attempt without showing that the old request cannot land.
    if unknown := [row for row in records(api, prefix, number) if open_attempt(*row, hold=True)]:
        raise Unknown(f'hold not confirmed: an earlier merge attempt has no definite outcome: {described(unknown)}; '
                      'the label stays and stops new merges')
    record = {'phase': 'held', 'pr': number, 'head': pr['head']['sha'], 'owner_run': list(hold_run)}
    try:  # Its own comment: a hold confirmation never rewrites an attempt's record.
        post(api, prefix, number, repository, record)
    except TRANSPORT:
        raise Unknown(f'the label is set on PR #{number}, but the confirmation write is unconfirmed; '
                      'the comment may appear later') from None
    return pr['head']['sha']


def summarize(text):
    if path := os.environ.get('GITHUB_STEP_SUMMARY'):
        try:
            with open(path, 'a', encoding='utf-8') as stream:
                stream.write(f'### Owner Updates\n\n{text}\n')
        except OSError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('merge', 'hold'))
    parser.add_argument('--event', type=Path, default=Path(os.environ.get('GITHUB_EVENT_PATH', 'event.json')))
    parser.add_argument('--pr')
    args = parser.parse_args(argv)
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    own_run = (int(os.environ.get('GITHUB_RUN_ID') or 0), int(os.environ.get('GITHUB_RUN_ATTEMPT') or 0))
    code = 1
    try:
        if args.mode == 'merge':
            event = json.loads(args.event.read_text(encoding='utf-8'))
            outcome = merge_owner_update(GitHub(), event, repository, ROOT, own_run)
            text = f"Merged owner update #{outcome['number']} at {outcome['head']} by merge commit {outcome['merge_commit']}"
            if not outcome['commits_kept']:
                text = f"{text}; the contribution's head was not confirmed among the merge commit's parents"
            if outcome['verification_failure']:
                text = f"{text}; merged; post-merge verification failed: {outcome['verification_failure']}"
            if not outcome['notice_updated']:
                text = (f'{text}; merged, but the update of the public notice was not confirmed and the comment may '
                        'still show the attempt as in progress; a Hold run dispatched for this PR records the merge '
                        'as observed')
            if not outcome['verification_failure'] and outcome['notice_updated']:
                code = 0
        elif os.environ.get('GITHUB_REF') != 'refs/heads/main':
            text = 'Hold runs only from main; nothing changed'
        elif not re.fullmatch(r'[1-9][0-9]{0,9}', args.pr or ''):
            text = 'Hold needs one positive pull request number; nothing changed'
        else:
            head = hold(GitHub(), repository, int(args.pr), own_run)
            text, code = f'Hold confirmed for PR #{args.pr} at head {head}', 0
    except Held as exc:
        text, code = f'Held: {exc}', 0
    except Ineligible as exc:
        text, code = f'Maintainer review: {exc}', 0
    except Unknown as exc:
        text = f'UNKNOWN: {exc}'
    except NotApplied as exc:
        text = f'Not applied: {exc}'
    except (*TRANSPORT, KeyError, TypeError, zipfile.BadZipFile, subprocess.SubprocessError):
        text = ('Owner-update processing failed; no merge request was sent' if args.mode == 'merge'
                else 'Hold processing failed; hold not confirmed')
    print(text, file=sys.stderr if code else sys.stdout)
    summarize(text)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
