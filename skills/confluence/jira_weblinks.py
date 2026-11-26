#!/usr/bin/env python3
"""Remove one selected Jira web link through Atlassian's TWG CLI, then verify it.

Pure standard-library helper: it never starts a process, opens a network
connection or follows a link. It writes a plan into a fresh, owner-only,
task-owned work directory, prints the exact `twg` argv for the agent to run with
the host's ordinary run_command, and checks the JSON that TWG writes there
through its agent full-result file (`--mode agent ... --output-file`).

    plan    bind site, login profile and issue (URL optional); print the read argv
    list    compact view of the web links read before (or after) the change
    select  bind one exact remote link ID to the expected URL; print the delete
            argv and the follow-up read argv
    verify  report the delete outcome and the follow-up read separately

Output is one JSON object on stdout. Exit status: 0 success, 2 refused input or
unusable snapshot, 3 verification finished without a clean removal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

PLAN, SELECTION = 'plan.json', 'selection.json'
FILES = {'before': 'before.json', 'unlink': 'unlink.json', 'after': 'after.json'}
ISSUE = re.compile(r'(?:[A-Z][A-Z0-9_]*-[1-9][0-9]*|[1-9][0-9]*)')
LINK_ID = re.compile(r'[1-9][0-9]{0,18}')
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}')
GET_COMMAND, UNLINK_COMMAND = 'jira.workitem.get', 'jira.workitem.unlink.weblink'


class Refused(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def emit(payload, status=0):
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return status


def read_json(path, label):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise Refused('missing_file', f'{label} was not written: {path.name} is absent.') from None
    try:
        return json.loads(raw.decode('utf-8')), hashlib.sha256(raw).hexdigest()
    except (UnicodeDecodeError, ValueError):
        raise Refused('malformed_json', f'{label} is not complete JSON (truncated or not JSON output).') from None


def write_new(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write('\n')


def checked(value, pattern, label):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Refused('invalid_argument', f'{label} has an unsupported form.')
    return value


def checked_url(value):
    parts = urlsplit(value or '')
    if parts.scheme not in {'http', 'https'} or not parts.netloc or any(c.isspace() for c in value):
        raise Refused('invalid_argument', '--url must be one absolute http(s) URL, exactly as stored in the web link.')
    return value


def absolute(value):
    path = Path(value)
    if not path.is_absolute():
        raise Refused('invalid_argument', '--workdir must be an absolute task-owned directory.')
    return path.resolve()


def argv(plan, *command, output):
    # Agent mode with explicit output flags, as Atlassian's agent guide asks; the full result goes to the file.
    return ['twg', '--profile', plan['profile'], '--mode', 'agent', '--output-summary', 'none',
            '--output-file', str(output), *command, '--site', plan['site'], '--output', 'json']


def read_argv(plan, workdir, name):
    return argv(plan, 'jira', 'workitem', 'get', plan['issue'], '--remote-links', '--fields', 'summary',
                output=workdir / FILES[name])


def load_plan(workdir):
    plan, _ = read_json(workdir / PLAN, 'plan')
    if not isinstance(plan, dict) or plan.get('schema') != 2:
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
    """acknowledged, provider_error (TWG's {ok:false, error}) or no_acknowledgement."""
    try:
        doc, _ = read_json(workdir / FILES['unlink'], 'delete')
    except Refused as exc:
        return {'status': 'no_acknowledgement', 'reason': exc.code, 'detail': str(exc)}
    if isinstance(doc, dict) and doc.get('ok') is False and isinstance(doc.get('error'), dict):
        error = doc['error']
        return {'status': 'provider_error', 'error': {key: error.get(key) for key in
                                                      ('code', 'kind', 'statusCode', 'message', 'retry')}}
    data = doc.get('data') if isinstance(doc, dict) else None
    request = doc.get('request') if isinstance(doc, dict) else None
    ok = (isinstance(data, dict) and isinstance(request, dict) and doc.get('apiVersion') == 'v2'
          and doc.get('command') == UNLINK_COMMAND and data.get('success') is True
          and isinstance(data.get('result'), dict) and data['result'].get('deleted') is True
          and isinstance(data.get('issue'), dict) and data['issue'].get('idOrKey') == selection['issue']['id']
          and str(request.get('linkId')) == selection['link']['id'])
    if ok:
        return {'status': 'acknowledged'}
    return {'status': 'no_acknowledgement', 'reason': 'unrecognized_output',
            'detail': 'The delete output is neither a success for this issue and link ID nor a TWG error.'}


def cmd_plan(args):
    workdir = absolute(args.workdir)
    if workdir.is_relative_to(Path(__file__).resolve().parent):
        raise Refused('invalid_argument', '--workdir must not be inside the shared skill folder.')
    if workdir.exists() and (not workdir.is_dir() or any(workdir.iterdir())):
        raise Refused('workdir_not_fresh', '--workdir must be new or empty; use one per removal.')
    plan = {'schema': 2, 'profile': checked(args.profile, TOKEN, '--profile'), 'site': checked(args.site, TOKEN, '--site'),
            'issue': checked(args.issue.upper(), ISSUE, '--issue'),
            'expected_url': checked_url(args.url) if args.url is not None else None}
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    workdir.chmod(0o700)
    write_new(workdir / PLAN, plan)
    return emit({'ok': True, 'step': 'read_before', 'workdir': str(workdir),
                 'run_command': {'cmd': read_argv(plan, workdir, 'before')},
                 'next': 'Run this read once, then `list`, then `select --link-id <ID>`'
                         + ('.' if plan['expected_url'] else ' with `--url <exact URL>`.')})


def cmd_list(args):
    workdir = absolute(args.workdir)
    plan = load_plan(workdir)
    return emit({'ok': True, 'snapshot': args.which,
                 **compact(snapshot(workdir, plan, args.which), plan['expected_url'])})


def cmd_select(args):
    workdir = absolute(args.workdir)
    plan = load_plan(workdir)
    used = [name for name in (SELECTION, FILES['unlink'], FILES['after']) if (workdir / name).exists()]
    if used:
        raise Refused('already_attempted', 'This work directory already has a selection or a later step; '
                      'a delete is never replayed. Verify it, or start a new plan after a fresh read.', files=used)
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
    return emit({'ok': True, 'step': 'delete_once_then_read_after', 'selected': target,
                 'other_links_with_same_url': [link['id'] for link in view['links']
                                               if link['url'] == expected and link['id'] != link_id],
                 'run_command': [{'cmd': delete}, {'cmd': read_argv(plan, workdir, 'after')}],
                 'next': 'Run the delete exactly once, then the read whatever the delete printed, then `verify`.'})


def cmd_verify(args):
    workdir = absolute(args.workdir)
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
    report.update(target_present=selection['link']['id'] in after['raw'], other_links=unchanged,
                  new_links=sorted(set(after['raw']) - set(others) - {selection['link']['id']}, key=int))
    if unchanged['missing'] or unchanged['changed']:
        outcome = 'other_links_changed'
    elif report['target_present']:
        outcome = {'acknowledged': 'acknowledged_but_present', 'provider_error': 'refused'}.get(
            write['status'], 'not_removed')
    else:
        outcome = 'removed' if write['status'] == 'acknowledged' else 'absent_without_acknowledgement'
    return emit({**report, 'ok': outcome == 'removed', 'outcome': outcome}, 0 if outcome == 'removed' else 3)


def parser():
    root = argparse.ArgumentParser(prog='jira_weblinks.py', description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = root.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan', help='bind the context and print the first read')
    plan.add_argument('--workdir', required=True)
    plan.add_argument('--site', required=True, help='TWG site prefix or cloud ID, e.g. example')
    plan.add_argument('--profile', required=True, help='saved TWG login profile; `default` for the saved default')
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
