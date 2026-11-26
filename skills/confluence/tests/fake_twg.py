"""Offline stand-in for the two TWG 1.4.8 commands the Jira web-link helper prints.

State lives in the JSON file named by FAKE_TWG_STATE. Every value is synthetic.
Shapes follow TWG 1.4.8's JSON written through `--output-file`: a success is the
v2 envelope {apiVersion, command, request, data, meta[, warnings]} (agent mode
drops warnings); a failure is {ok: false, error: {code, kind, message, ...}}.
Faults are consumed in order from state["faults"][<command>].
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SITE_BASE = 'https://example.invalid'
ROOT_OPTIONS = {'--profile', '--mode', '--output-summary', '--output-file'}


def take(argv, flag):
    index = argv.index(flag)
    value = argv[index + 1]
    del argv[index:index + 2]
    return value


def meta(resource, **extra):
    return {'backend': 'agg', 'resourceClass': 'entity', 'sourceMode': 'rest', 'apiVersion': 'rest-v3',
            'resourceType': resource, **extra}


def error(code, message, status=None, *, kind='jira', retryable=False):
    body = {'code': code, 'kind': kind, 'message': message, 'retry': {'retryable': retryable}}
    if status is not None:
        body['statusCode'] = status
    return {'ok': False, 'error': body}


def unlink_ok(issue_ref, link_id, *, site=None):
    request = {'linkId': link_id}
    if site is not None:
        request['site'] = site
    return {'apiVersion': 'v2', 'command': 'jira.workitem.unlink.weblink',
            'request': request,
            'data': {'success': True, 'issue': {'idOrKey': issue_ref}, 'result': {'deleted': True},
                     'weblink': {'url': None, 'title': None, 'globalId': None, 'relationship': None,
                                 'applicationName': None, 'applicationType': None}},
            'meta': meta('jira.workitem-weblink')}


def main():
    state_path = Path(os.environ['FAKE_TWG_STATE'])
    state = json.loads(state_path.read_text(encoding='utf-8'))
    argv = sys.argv[1:]
    root = {}
    while argv and argv[0].startswith('--'):
        flag = argv.pop(0)
        root[flag] = argv.pop(0)
    assert set(root) == ROOT_OPTIONS, root
    assert root['--mode'] == 'agent' and root['--output-summary'] == 'none', root
    assert root['--profile'] == state['profile'], 'the login profile is pinned per plan'
    output = Path(root['--output-file'])
    assert take(argv, '--output') == 'json'
    site = take(argv, '--site')
    assert site == state['site'], 'the site is pinned per plan'
    state['calls'].append(argv[:])
    issue = state['issue']
    faults = state['faults']

    def finish(code=0, document=None, raw=None):
        if document is not None or raw is not None:
            output.write_text(raw if raw is not None else json.dumps(document, indent=2), encoding='utf-8')
            print(f'stdout={output}')
        state_path.write_text(json.dumps(state, indent=2), encoding='utf-8')
        return code

    if argv[:3] == ['jira', 'workitem', 'get']:
        assert argv[3:] == [issue['key'], '--remote-links', '--fields', 'summary'], argv
        fault = faults['get'].pop(0) if faults['get'] else None
        if fault == 'no_file':
            print('Error: socket hang up', file=sys.stderr)
            return finish(1)
        if fault == 'provider_error':
            return finish(1, error('unauthorized', 'Jira REST API 403', 403))
        if fault == 'drop_other':  # an unrelated concurrent edit right before this read
            state['links'] = [link for link in state['links'] if link['id'] != state['other_id']]
        if fault == 'retitle_other':
            for link in state['links']:
                if link['id'] == state['other_id']:
                    link['object'] = {**link['object'], 'title': 'Renamed elsewhere'}
        if fault == 'add_same_url_sibling':  # another actor added a new same-URL link concurrently
            url = state['mr']['url']
            state['links'].append({
                'id': 10004, 'self': f'{SITE_BASE}/rest/api/3/issue/{issue["id"]}/remotelink/10004',
                'globalId': f'system={url}#concurrent',
                'application': {'type': 'com.example.git', 'name': 'Example Git'},
                'relationship': 'links to', 'object': {'url': url, 'title': '!7 added concurrently'},
            })
        item = {'expand': 'renderedFields', 'id': issue['id'], 'self': f'{SITE_BASE}/rest/api/3/issue/{issue["id"]}',
                'key': issue['key'], 'summary': issue['summary'], 'url': f'{SITE_BASE}/browse/{issue["key"]}'}
        if fault == 'omit_remote_links':
            pass  # TWG drops the key; its warning is not in an agent-mode file
        elif fault == 'non_list':
            item['remoteLinks'] = {'errorMessages': ['unexpected']}
        elif fault == 'duplicate_ids':
            item['remoteLinks'] = state['links'] + state['links'][:1]
        else:
            item['remoteLinks'] = state['links']
        if fault == 'other_issue':  # the key now names a different work item
            item['id'] = '10999'
        if fault == 'wrong_key':
            item.update(key='DEMO-43', url=f'{SITE_BASE}/browse/DEMO-43')
        document = {'apiVersion': 'v2', 'command': 'jira.workitem.get',
                    'request': {'issueIdOrKey': issue['key'], 'site': site, 'fields': ['summary'], 'remoteLinks': True},
                    'data': [item], 'meta': meta('jira.workitem', **({'partial': True} if fault == 'partial' else {}))}
        if fault == 'warnings':
            document['warnings'] = ['remote links read reported a warning']
        text = json.dumps(document, indent=2)
        return finish(0, raw=text[:len(text) // 2] if fault == 'truncated' else text)

    if argv[:4] == ['jira', 'workitem', 'unlink', 'weblink']:
        rest = argv[4:]
        issue_ref, link_id = take(rest, '--id'), take(rest, '--link-id')
        assert rest == [], 'only --id and --link-id select the record; never --url'
        fault = faults['unlink'].pop(0) if faults['unlink'] else None
        if fault == 'refuse':
            return finish(1, error('unauthorized', 'Jira REST API 403: delete:jira-platform:twg-cli missing', 403))
        if fault == 'http_401':
            return finish(1, error('unauthenticated', 'Jira REST API 401: authentication required', 401))
        if fault == 'timeout_present':
            return finish(1, error('timeout', 'Request timed out waiting for Jira', None, kind='network', retryable=True))
        if fault == 'http_408_present':
            return finish(1, error('request_timeout', 'Jira REST API 408: Request Timeout', 408, retryable=True))
        if fault == 'http_504_present':
            return finish(1, error('gateway_timeout', 'Jira REST API 504: Gateway Timeout', 504, retryable=True))
        if fault == 'http_500_present':
            return finish(1, error('server_error', 'Jira REST API 500: Internal Server Error', 500, retryable=True))
        if fault == 'unfamiliar_present':
            return finish(1, error('unfamiliar_fault', 'Unexpected upstream response', None, kind='unknown'))
        if fault == 'mismatched_ack':
            return finish(0, unlink_ok(issue_ref, '10001'))
        if fault == 'ack_still_present':  # TWG acknowledged delete, yet the record remains in state
            return finish(0, unlink_ok(issue_ref, link_id))
        if fault == 'crash':  # nothing reached Jira and nothing was written
            print('Error: interrupted', file=sys.stderr)
            return finish(1)
        if issue_ref not in {issue['id'], issue['key']} or link_id not in {str(link['id']) for link in state['links']}:
            return finish(1, error('not-found', 'Jira REST API 404', 404))
        state['links'] = [link for link in state['links'] if str(link['id']) != link_id]
        state['deletes'].append([issue_ref, link_id])
        if fault == 'timeout_absent':  # Jira applied it, then the client timed out
            return finish(1, error('timeout', 'Request timed out waiting for Jira', None, kind='network', retryable=True))
        if fault == 'http_504_absent':  # Jira applied it, then the gateway returned 504
            return finish(1, error('gateway_timeout', 'Jira REST API 504: Gateway Timeout', 504, retryable=True))
        if fault == 'lost_ack':  # Jira applied it, but the caller never received the answer
            print('Error: socket hang up', file=sys.stderr)
            return finish(1)
        return finish(0, unlink_ok(issue_ref, link_id))
    raise SystemExit(f'unsupported fake command: {argv}')


if __name__ == '__main__':
    raise SystemExit(main())
