#!/usr/bin/env python3
"""Inspect Jira web links or remove one selected link via Atlassian's TWG CLI.

Pure standard-library helper: it never starts a process, opens a network
connection or follows a link. It creates a fresh leaf work directory outside
the shared skill beneath a task-owned private parent (0700/0600 on POSIX; on
Windows, use a parent that already carries a private owner-only ACL), prints
the exact `twg` argv for the agent to run in the same host environment via
ordinary `run_command`, and checks the JSON that TWG writes through
`--mode agent ... --output-file`.

    plan    bind site, saved login profile and issue (URL optional); print the
            read argv (stop after `list` for read-only inspection)
    list    compact view of the web links read before (or after) the change
    select  bind one exact remote link ID to the expected URL as of `before`;
            print the single delete argv and the follow-up read argv
    verify  classify the delete outcome separately from the follow-up read

A delete is either `acknowledged` by a clean TWG success envelope or
`ambiguous`: TWG's error fields do not say which request failed, so no reported
error is treated as proof that nothing was removed.

Output is one ASCII-escaped JSON object on stdout (parseable and lossless
regardless of piped terminal encoding). Exit status: 0 success, 2 refused
input or unusable snapshot, 3 verification finished without a clean removal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

PLAN, SELECTION = 'plan.json', 'selection.json'
FILES = {'before': 'before.json', 'unlink': 'unlink.json', 'after': 'after.json'}
ISSUE = re.compile(r'(?:[A-Z][A-Z0-9_]*-[1-9][0-9]*|[1-9][0-9]*)')
LINK_ID = re.compile(r'[1-9][0-9]{0,18}')
SITE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}')
PROFILE = re.compile(r'[A-Za-z0-9_-]{1,64}')  # TWG login profile names may start with "_" or "-"
GET_COMMAND, UNLINK_COMMAND = 'jira.workitem.get', 'jira.workitem.unlink.weblink'

# Personal-Jira TWG auth override variables that bypass a saved login profile.
# Unrelated or required settings (such as TWG_AUTH_STORAGE_KEY for encrypted Linux
# auth.db, TWG_ADMIN_TOKEN for independent admin commands, and TWG_PROFILE
# overridden by --profile) stay allowed.
AMBIENT_AUTH_OVERRIDES = frozenset({'TWG_TOKEN', 'TWG_OAUTH_ACCESS_TOKEN'})


class Refused(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def emit(payload, status=0):
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return status


def read_json(path, label):
    if path.is_symlink():
        raise Refused('invalid_file', f'{label} must be a regular file, not a symlink.')
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise Refused('missing_file', f'{label} was not written: {path.name} is absent.') from None
    try:
        return json.loads(raw.decode('utf-8')), hashlib.sha256(raw).hexdigest()
    except (UnicodeDecodeError, ValueError):
        raise Refused('malformed_json', f'{label} is not complete JSON (truncated or not JSON output).') from None


def write_new(path, value):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if os.name == 'posix':
            os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, 'w', encoding='utf-8')
    except Exception:
        os.close(descriptor)
        raise
    with stream:
        json.dump(value, stream, ensure_ascii=True, indent=2, sort_keys=True)
        stream.write('\n')


def checked(value, pattern, label):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Refused('invalid_argument', f'{label} has an unsupported form.')
    return value


def checked_url(value):
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 0x20 for c in value):
        raise Refused('invalid_argument', '--url must be one absolute http(s) URL, exactly as stored in the web link.')
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        raise Refused('invalid_argument', '--url must be one absolute http(s) URL, exactly as stored in the web link.') from None
    if parts.scheme not in {'http', 'https'} or not parts.hostname:
        raise Refused('invalid_argument', '--url must be one absolute http(s) URL, exactly as stored in the web link.')
    return value


def ambient_auth_overrides(env=None):
    """Return names of personal-Jira TWG auth override variables present in env without reading values."""
    mapping = os.environ if env is None else env
    return sorted(key for key in mapping if key.upper() in AMBIENT_AUTH_OVERRIDES)


def require_no_ambient_auth_overrides():
    found = ambient_auth_overrides()
    if found:
        raise Refused(
            'ambient_auth_override',
            'Personal-Jira TWG auth override environment variables are set in this host environment; '
            'unset them so TWG uses the named saved login profile directly.',
            variables=found,
        )


def fresh_workdir(value):
    if not isinstance(value, str) or not value or any(ord(c) < 0x20 for c in value):
        raise Refused('invalid_argument', '--workdir must be an absolute path to a new task-owned directory.')
    raw = Path(value)
    if not raw.is_absolute() or raw.name in {'', '.', '..'} or '..' in raw.parts:
        raise Refused('invalid_argument', '--workdir must be an absolute path to a new task-owned directory.')
    if os.path.lexists(raw):
        raise Refused(
            'workdir_not_fresh',
            '--workdir must be a new directory that does not already exist; use a fresh path for each run.',
        )
    if raw.parent.is_symlink():
        raise Refused('invalid_argument', '--workdir parent must not be a symlink.')
    if raw.parent.exists() and not raw.parent.is_dir():
        raise Refused('invalid_argument', '--workdir parent must be a directory.')
    workdir = raw.parent.resolve() / raw.name
    if os.path.lexists(workdir):
        raise Refused(
            'workdir_not_fresh',
            '--workdir must be a new directory that does not already exist; use a fresh path for each run.',
        )
    if workdir.is_relative_to(Path(__file__).resolve().parent):
        raise Refused('invalid_argument', '--workdir must not be inside the shared skill folder.')
    return workdir


def existing_workdir(value):
    if not isinstance(value, str) or not value or any(ord(c) < 0x20 for c in value):
        raise Refused('invalid_argument', '--workdir must be an absolute task-owned directory.')
    raw = Path(value)
    if not raw.is_absolute() or '..' in raw.parts or raw.is_symlink():
        raise Refused('invalid_argument', '--workdir must be an absolute non-symlink task-owned directory.')
    workdir = raw.resolve()
    if workdir.is_relative_to(Path(__file__).resolve().parent):
        raise Refused('invalid_argument', '--workdir must not be inside the shared skill folder.')
    return workdir


def argv(plan, *command, output):
    # Agent mode with explicit output flags, as Atlassian's agent guide asks; the full result goes to the file.
    profile = plan['profile']
    # A separate "-name" argument would read as another option; the joined form keeps it the profile value.
    selector = [f'--profile={profile}'] if profile.startswith('-') else ['--profile', profile]
    return ['twg', *selector, '--mode', 'agent', '--output-summary', 'none',
            '--output-file', str(output), *command, '--site', plan['site'], '--output', 'json']


def read_argv(plan, workdir, name):
    return argv(plan, 'jira', 'workitem', 'get', plan['issue'], '--remote-links', '--fields', 'summary',
                output=workdir / FILES[name])


def load_plan(workdir):
    plan, _ = read_json(workdir / PLAN, 'plan')
    if not isinstance(plan, dict) or plan.get('schema') != 2 \
            or not all(isinstance(plan.get(k), str) for k in ('profile', 'site', 'issue')):
        raise Refused('invalid_plan', 'plan.json was not written by this helper version.')
    return plan


def link_record(raw, position):
    if not isinstance(raw, dict) or not isinstance(raw.get('object'), dict):
        raise Refused('malformed_remote_links', f'remoteLinks[{position}] is not a remote link record.')
    identifier = raw.get('id')
    if isinstance(identifier, bool) or not isinstance(identifier, (int, str)) \
            or not LINK_ID.fullmatch(str(identifier)):
        raise Refused('malformed_remote_links', f'remoteLinks[{position}] has no usable numeric id.')
    url = raw['object'].get('url')
    if not isinstance(url, str) or not url:
        raise Refused('malformed_remote_links', f'remoteLinks[{position}] has no object.url.')
    application = raw.get('application') if isinstance(raw.get('application'), dict) else {}
    summary = {'id': str(identifier), 'url': url, 'title': raw['object'].get('title'),
               'global_id': raw.get('globalId'), 'relationship': raw.get('relationship'),
               'application': {'name': application.get('name'), 'type': application.get('type')}}
    return summary, raw


def snapshot(workdir, plan, name):
    """A single-issue `jira workitem get --remote-links` envelope, or a refusal."""
    doc, digest = read_json(workdir / FILES[name], f'{name} read')
    if isinstance(doc, dict) and doc.get('ok') is False:
        error = doc.get('error') if isinstance(doc.get('error'), dict) else {}
        raise Refused('provider_error', f'{name} read failed in TWG: {error.get("message", "no message")}',
                      provider_error={key: error.get(key) for key in ('code', 'statusCode', 'message')})
    if not isinstance(doc, dict) or doc.get('apiVersion') != 'v2' or doc.get('command') != GET_COMMAND:
        raise Refused('unrecognized_output', f'{name} read is not a TWG v2 {GET_COMMAND} envelope.')
    request = doc.get('request') if isinstance(doc.get('request'), dict) else {}
    if str(request.get('issueIdOrKey', '')).upper() != plan['issue'] or request.get('site') != plan['site'] \
            or request.get('remoteLinks') is not True:
        raise Refused('context_mismatch', f'{name} read was made for another issue, site or without --remote-links.',
                      request=request)
    meta = doc.get('meta') if isinstance(doc.get('meta'), dict) else {}
    if doc.get('warnings') or meta.get('partial'):
        raise Refused('incomplete_read', f'{name} read reports warnings or partial data; nothing is inferred from it.',
                      warnings=doc.get('warnings'))
    data = doc.get('data')
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise Refused('unrecognized_output', f'{name} read does not hold exactly one work item.')
    item = data[0]
    issue_id, key, url = item.get('id'), item.get('key'), item.get('url')
    if not (isinstance(issue_id, str) and issue_id.isdigit() and isinstance(key, str) and isinstance(url, str)
            and url.endswith('/browse/' + key) and plan['issue'] in {issue_id, key.upper()}):
        raise Refused('context_mismatch', f'{name} read does not identify the planned issue.')
    if 'remoteLinks' not in item:
        # TWG omits the key when it could not read remote links (agent mode also drops the warning).
        raise Refused('remote_links_missing', f'{name} read has no remoteLinks, so their state is unknown, not empty.')
    if not isinstance(item['remoteLinks'], list):
        raise Refused('malformed_remote_links', f'{name} read has a non-list remoteLinks.')
    pairs = [link_record(raw, position) for position, raw in enumerate(item['remoteLinks'])]
    ids = [summary['id'] for summary, _ in pairs]
    if len(set(ids)) != len(ids):
        raise Refused('duplicate_link_id', f'{name} read lists one remote link ID more than once.')
    return {'issue': {'id': issue_id, 'key': key, 'url': url, 'site': request['site']},
            'links': [summary for summary, _ in pairs], 'raw': {summary['id']: raw for summary, raw in pairs},
            'sha256': digest}


def compact(view, expected_url):
    return {'issue': view['issue'], 'expected_url': expected_url,
            'web_links': [{**link, **({'matches_expected_url': link['url'] == expected_url} if expected_url else {})}
                          for link in view['links']]}


def delete_outcome(workdir, selection):
    """Classify unlink.json: acknowledged, or ambiguous."""
    try:
        doc, _ = read_json(workdir / FILES['unlink'], 'delete')
    except Refused as exc:
        return {'status': 'ambiguous', 'reason': exc.code, 'detail': str(exc)}
    if not isinstance(doc, dict):
        return {'status': 'ambiguous', 'reason': 'unrecognized_output',
                'detail': 'The delete output is not a JSON object.'}
    if doc.get('ok') is False and isinstance(doc.get('error'), dict):
        # Neither a code nor a statusCode says the DELETE itself was declined (a transport failure can carry the
        # status of an earlier read), so every reported error is retained as ambiguous.
        error = doc['error']
        payload = {key: error.get(key) for key in ('code', 'kind', 'statusCode', 'message', 'retry')}
        return {'status': 'ambiguous', 'reason': 'provider_error', 'error': payload,
                'detail': 'TWG reported an error; whether the delete applied or will apply is uncertain.'}
    data = doc.get('data')
    request = doc.get('request')
    meta = doc.get('meta') if isinstance(doc.get('meta'), dict) else {}
    if (doc.get('ok') is not False and doc.get('apiVersion') == 'v2' and doc.get('command') == UNLINK_COMMAND
            and isinstance(data, dict) and isinstance(request, dict)
            and data.get('success') is True
            and isinstance(data.get('result'), dict) and data['result'].get('deleted') is True):
        req_site = request.get('site')
        req_issue = request.get('id') if 'id' in request else request.get('issueIdOrKey')
        issue_ok = isinstance(data.get('issue'), dict) and data['issue'].get('idOrKey') == selection['issue']['id']
        req_issue_ok = req_issue is None or str(req_issue).upper() in {selection['issue']['id'], selection['issue']['key'].upper()}
        link_ok = str(request.get('linkId')) == selection['link']['id']
        site_ok = req_site is None or req_site == selection['issue']['site']
        clean_ok = not doc.get('warnings') and not meta.get('partial')
        if issue_ok and req_issue_ok and link_ok and site_ok and clean_ok:
            return {'status': 'acknowledged'}
        return {'status': 'ambiguous', 'reason': 'mismatched_acknowledgement',
                'detail': 'The delete acknowledgement does not cleanly match the selected issue, site, and link ID.'}
    return {'status': 'ambiguous', 'reason': 'unrecognized_output',
            'detail': 'The delete output is neither a valid acknowledgement for this link nor a TWG error.'}


def cmd_plan(args):
    workdir = fresh_workdir(args.workdir)
    require_no_ambient_auth_overrides()
    plan = {
        'schema': 2,
        'profile': checked(args.profile, PROFILE, '--profile'),
        'site': checked(args.site, SITE, '--site'),
        'issue': checked(args.issue.upper(), ISSUE, '--issue'),
        'expected_url': checked_url(args.url) if args.url is not None else None,
    }
    if not workdir.parent.exists():
        workdir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        workdir.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError:
        raise Refused(
            'workdir_not_fresh',
            '--workdir must be a new directory that does not already exist; use a fresh path for each run.',
        ) from None
    if os.name == 'posix':
        os.chmod(workdir, 0o700)
    write_new(workdir / PLAN, plan)
    select_hint = '`select --link-id <ID>`' if plan['expected_url'] else '`select --link-id <ID> --url <exact URL>`'
    return emit({
        'ok': True,
        'step': 'read_before',
        'workdir': str(workdir),
        'run_command': {'cmd': read_argv(plan, workdir, 'before')},
        'next': f'Run this read once in the same host environment, then `list`. '
                f'Stop after `list` for a read-only check; only run {select_hint} when the user '
                f'affirmatively requested removing that specific record.',
    })


def cmd_list(args):
    workdir = existing_workdir(args.workdir)
    plan = load_plan(workdir)
    if args.which == 'before':
        select_hint = '`select --link-id <ID>`' if plan['expected_url'] else '`select --link-id <ID> --url <exact URL>`'
        next_hint = (f'Read-only listing complete; stop here if only inspecting web links. '
                     f'To remove one record the user affirmatively requested, run {select_hint} '
                     f'(re-read in a fresh workdir first if this listing is stale).')
    else:
        next_hint = 'Follow-up listing complete; run `verify` for the full comparison.'
    return emit({'ok': True, 'snapshot': args.which, 'next': next_hint,
                 **compact(snapshot(workdir, plan, args.which), plan['expected_url'])})


def cmd_select(args):
    workdir = existing_workdir(args.workdir)
    plan = load_plan(workdir)
    used = [name for name in (SELECTION, FILES['unlink'], FILES['after']) if (workdir / name).exists()]
    if used:
        raise Refused('already_attempted', 'This work directory already has a selection or a later step; '
                      'a delete is never replayed. Verify it, or start a new plan after a fresh read.', files=used)
    require_no_ambient_auth_overrides()
    link_id = checked(args.link_id, LINK_ID, '--link-id')
    expected = checked_url(args.url) if args.url is not None else plan['expected_url']
    if expected is None:
        raise Refused('url_required', 'Pass --url with the exact URL the chosen web link must carry.')
    if plan['expected_url'] is not None and expected != plan['expected_url']:
        raise Refused('url_mismatch', '--url differs from the URL bound in the plan.')
    view = snapshot(workdir, plan, 'before')
    target = next((link for link in view['links'] if link['id'] == link_id), None)
    if target is None:
        raise Refused('link_not_found', f'Remote link {link_id} is not on {view["issue"]["key"]}.',
                      **compact(view, expected))
    if target['url'] != expected:
        raise Refused('url_mismatch', f'Remote link {link_id} points to another URL; nothing was selected.',
                      selected_url=target['url'], expected_url=expected)
    write_new(workdir / SELECTION, {'link': target, 'expected_url': expected, 'issue': view['issue'],
                                    'before_sha256': view['sha256'],
                                    'others': {i: raw for i, raw in view['raw'].items() if i != link_id}})
    delete = argv(plan, 'jira', 'workitem', 'unlink', 'weblink', '--id', view['issue']['id'], '--link-id', link_id,
                  output=workdir / FILES['unlink'])
    return emit({
        'ok': True,
        'step': 'delete_once_then_read_after',
        'selected': target,
        'other_links_with_same_url': [link['id'] for link in view['links']
                                      if link['url'] == expected and link['id'] != link_id],
        'url_check_scope': 'Verified against before.json as of that read; Jira delete by --link-id has no atomic URL precondition.',
        'run_command': [{'cmd': delete}, {'cmd': read_argv(plan, workdir, 'after')}],
        'next': 'Run the delete once in the same host environment, then the follow-up read regardless of the delete result, then `verify`.',
    })


def cmd_verify(args):
    workdir = existing_workdir(args.workdir)
    plan = load_plan(workdir)
    selection, _ = read_json(workdir / SELECTION, 'selection')
    before = snapshot(workdir, plan, 'before')
    if before['sha256'] != selection['before_sha256']:
        raise Refused('before_changed', 'before.json changed after selection; this run cannot be verified.')
    write = delete_outcome(workdir, selection)
    report = {'issue': selection['issue'], 'link': selection['link'], 'write': write,
              'observation': 'read through TWG CLI', 'retry': 'Never re-run the delete from this directory.'}
    try:
        after = snapshot(workdir, plan, 'after')
    except Refused as exc:
        return emit({**report, 'ok': False, 'outcome': 'unverified', 'read_after': {'error': exc.code,
                     'detail': str(exc)}, 'next': 'Read the issue again with a new plan before any further change.'}, 3)
    if after['issue'] != selection['issue']:
        return emit({**report, 'ok': False, 'outcome': 'identity_changed', 'after_issue': after['issue']}, 3)
    others = selection['others']
    # Every other remote link must come back as the identical provider record, field for field.
    unchanged = {'missing': sorted((i for i in others if i not in after['raw']), key=int),
                 'changed': sorted((i for i in others if i in after['raw'] and after['raw'][i] != others[i]), key=int)}
    target_present = selection['link']['id'] in after['raw']
    new_links = sorted(set(after['raw']) - set(others) - {selection['link']['id']}, key=int)
    surviving_same_url = sorted(
        (link['id'] for link in after['links'] if link['url'] == selection['expected_url']),
        key=int,
    )
    report.update(
        target_present=target_present,
        other_links=unchanged,
        new_links=new_links,
        surviving_same_url_links=surviving_same_url,
    )
    if unchanged['missing'] or unchanged['changed']:
        outcome = 'other_links_changed'
    elif target_present:
        if write['status'] == 'acknowledged':
            outcome = 'acknowledged_but_present'
        else:
            outcome = 'ambiguous_present'
            report['note'] = ('The link is still present in this read, but the delete outcome is ambiguous; '
                              'this cannot prove the write will never complete. Do not resend.')
    elif write['status'] == 'acknowledged':
        outcome = 'removed'
        if surviving_same_url:
            report['note'] = ('The selected link was removed, but other surviving link IDs still carry this URL; '
                              'do not delete extra records unless the user asks for them.')
    else:
        outcome = 'absent_without_acknowledgement'
        report['note'] = ('The link is absent in this read, but this run has no valid acknowledgement '
                          'and cannot attribute its absence to this request.')
    return emit({**report, 'ok': outcome == 'removed', 'outcome': outcome}, 0 if outcome == 'removed' else 3)


def parser():
    root = argparse.ArgumentParser(prog='jira_weblinks.py', description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = root.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan', help='bind the context and print the first read')
    plan.add_argument('--workdir', required=True, help='new absolute leaf path under a private task-owned parent')
    plan.add_argument('--site', required=True, help='TWG site prefix or cloud ID, e.g. example')
    plan.add_argument('--profile', required=True,
                      help='saved TWG login profile; `default` for the saved default; '
                           'write --profile=NAME for a name starting with "-"')
    plan.add_argument('--issue', required=True, help='issue key, e.g. DEMO-42')
    plan.add_argument('--url', help='exact URL stored in the web link, when already known')
    listing = commands.add_parser('list', help='show compact web links')
    listing.add_argument('--workdir', required=True)
    listing.add_argument('--which', choices=('before', 'after'), default='before')
    select = commands.add_parser('select', help='bind one remote link ID and print the delete')
    select.add_argument('--workdir', required=True)
    select.add_argument('--link-id', required=True)
    select.add_argument('--url', help='exact URL the chosen link must carry; required when the plan has none')
    verify = commands.add_parser('verify', help='check the delete outcome and the follow-up read')
    verify.add_argument('--workdir', required=True)
    return root


def main(arguments=None):
    args = parser().parse_args(arguments)
    try:
        return {'plan': cmd_plan, 'list': cmd_list, 'select': cmd_select, 'verify': cmd_verify}[args.command](args)
    except Refused as exc:
        return emit({'ok': False, 'error': exc.code, 'detail': str(exc), **exc.details}, 2)
    except OSError as exc:
        return emit({'ok': False, 'error': 'local_io_error', 'detail': type(exc).__name__}, 2)


if __name__ == '__main__':
    raise SystemExit(main())
