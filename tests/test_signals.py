"""Signals use GitHub data; tests never contact or mutate GitHub."""
from __future__ import annotations

import copy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.github_api import APIError
from scripts import update_signals as signals

REPO = 'razzant/OuroborosHub'


def connection(nodes, more=False, cursor=None):
    return {'nodes': nodes, 'pageInfo': {'hasNextPage': more, 'endCursor': cursor}}


class API:
    def __init__(self):
        self.calls = []
        self.enabled = True
        self.categories = {'id': 'skills-category', 'name': 'Skills'}
        self.category_pages = False
        self.discussion_pages = False
        self.discussions = [{'title': 'alpha', 'url': 'https://github.com/discussions/1',
                             'upvoteCount': 7, 'comments': {'totalCount': 3}}]
        self.exists = True
        self.stars = 4

    def graphql(self, query, variables):
        self.calls.append(('graphql', query, variables))
        if query == signals.REPOSITORY_QUERY:
            return {'repository': {'id': 'repo-id', 'hasDiscussionsEnabled': self.enabled}}
        if query == signals.CATEGORIES_QUERY:
            assert self.enabled, 'disabled Discussions must not query category connections'
            first_page = self.category_pages and not variables['cursor']
            return {'repository': {'id': 'repo-id', 'hasDiscussionsEnabled': self.enabled,
                'discussionCategories': connection([] if first_page or self.categories is None else [self.categories],
                                                   bool(first_page), 'category-cursor')}}
        if query == signals.DISCUSSIONS_QUERY:
            first_page = self.discussion_pages and not variables['cursor']
            return {'repository': {'discussions': connection([] if first_page else self.discussions,
                                                             bool(first_page), 'discussion-cursor')}}
        if query == signals.CREATE_DISCUSSION:
            row = {'title': variables['title'], 'url': 'https://github.com/discussions/2',
                   'upvoteCount': 0, 'comments': {'totalCount': 0}}
            self.discussions.append(row)
            return {'createDiscussion': {'discussion': row}}
        raise AssertionError(query)

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith('/git/ref/heads/signals'):
            if not self.exists:
                raise APIError(404)
            return {'object': {'sha': 'old'}}
        if method == 'GET':
            if isinstance(self.stars, Exception):
                raise self.stars
            return {'stargazers_count': self.stars}
        if path.endswith('/git/blobs'):
            return {'sha': 'blob'}
        if path.endswith('/git/trees'):
            return {'sha': 'tree'}
        if path.endswith('/git/commits'):
            return {'sha': 'commit'}
        return {}


class SignalsTests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.entries = {'alpha': {'source': {'kind': 'hosted'}},
                        'beta': {'source': {'kind': 'git', 'repo': 'https://github.com/author/repo'}}}

    def snapshot(self, pause=lambda: None):
        return signals.snapshot(self.api, REPO, self.entries, generated_at='2026-10-17T00:00:00Z', pause=pause)

    def test_paginates_reuses_topics_creates_missing_and_reads_author_stars(self):
        self.api.category_pages = self.api.discussion_pages = True
        document = self.snapshot()
        self.assertEqual(document['schema_version'], 1)
        self.assertEqual(document['generated_at'], '2026-10-17T00:00:00Z')
        self.assertEqual(document['skills']['alpha'], {'votes': 7, 'comments': 3, 'discussion_url': 'https://github.com/discussions/1'})
        self.assertEqual(document['skills']['beta']['votes'], 0)  # A measured zero.
        self.assertEqual(document['author_repos'], {'https://github.com/author/repo': {'stars': 4}})
        creations = [c for c in self.api.calls if c[0] == 'graphql' and c[1] == signals.CREATE_DISCUSSION]
        self.assertEqual([c[2]['title'] for c in creations], ['beta'])
        self.assertEqual(creations[0][2]['category'], 'skills-category')
        self.assertIn('upvoteCount', signals.DISCUSSIONS_QUERY)
        self.assertEqual(self.snapshot(), document)
        self.assertEqual(len([c for c in self.api.calls if c[1] == signals.CREATE_DISCUSSION]), 1)

    def test_creations_are_paced_and_a_creation_limit_still_publishes_the_rest(self):
        self.entries = {name: {'source': {'kind': 'hosted'}} for name in ('alpha', 'beta', 'delta', 'gamma')}
        self.entries['epsilon'] = {'source': {'kind': 'git', 'repo': 'https://github.com/author/repo'}}
        real, attempts, pauses = self.api.graphql, [], []

        def limited(query, variables):
            if query == signals.CREATE_DISCUSSION:
                attempts.append(variables['title'])
                if len(attempts) > 1:
                    raise RuntimeError('GitHub GraphQL request failed (RATE_LIMITED)')
            return real(query, variables)

        self.api.graphql = limited
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            document = self.snapshot(pause=lambda: pauses.append(1))
        self.assertEqual(attempts, ['beta', 'delta'])  # no more attempts after the limit
        self.assertEqual(pauses, [1])  # a pause before every creation but the first
        self.assertEqual(sorted(document['skills']), ['alpha', 'beta'])  # no topic, no signal (never zero)
        self.assertEqual(document['author_repos'], {'https://github.com/author/repo': {'stars': 4}})
        self.assertIn('Topic for delta not created (GitHub GraphQL request failed (RATE_LIMITED))', errors.getvalue())
        with tempfile.TemporaryDirectory(prefix='hub-signals-') as tmp:
            output = Path(tmp) / 'signals.json'
            attempts.clear()
            with patch.object(signals, 'GitHub', return_value=self.api), patch.dict(os.environ, {'GITHUB_REPOSITORY': REPO}), \
                    patch.object(signals, 'CREATE_PAUSE_SEC', 0), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(signals.main(['--output', str(output)]), 0)
            self.assertEqual(self.api.calls[-1][1], f'/repos/{REPO}/git/refs/heads/signals')

    def test_disabled_discussions_or_missing_category_return_empty_without_creating_anything(self):
        for enabled, category in ((False, self.api.categories), (True, None)):
            with self.subTest(enabled=enabled):
                self.api.enabled, self.api.categories = enabled, category
                self.api.calls.clear()
                document = self.snapshot()
                self.assertEqual(document['skills'], {})
                self.assertEqual(document['author_repos'], {})
                self.assertEqual(len(self.api.calls), 2 if enabled else 1)
                self.assertEqual(self.api.calls[0][1], signals.REPOSITORY_QUERY)
                signals.publish(self.api, REPO, document)  # Empty is publishable, not a failing job.

    def test_duplicate_topics_reuse_oldest_and_unknown_counts_are_not_zero(self):
        self.api.discussions.append({**self.api.discussions[0], 'url': 'new-duplicate', 'upvoteCount': 100})
        self.api.discussions[0].pop('upvoteCount')
        self.api.discussions[0]['comments'] = {}
        row = self.snapshot()['skills']['alpha']
        self.assertEqual(row, {'discussion_url': 'https://github.com/discussions/1'})

    def test_unknown_stars_and_non_github_sources_are_omitted_and_repos_deduplicated(self):
        self.entries['gamma'] = copy.deepcopy(self.entries['beta'])
        self.entries['delta'] = {'source': {'kind': 'git', 'repo': 'https://example.org/author/repo'}}
        for stars in (None, True, -1, APIError(404), APIError(403)):
            self.api.stars = stars
            self.api.calls.clear()
            self.assertEqual(self.snapshot()['author_repos'], {})
            self.assertEqual([c[1] for c in self.api.calls if c[0] == 'GET'], ['/repos/author/repo'])
        self.api.stars = 0
        self.assertEqual(self.snapshot()['author_repos']['https://github.com/author/repo'], {'stars': 0})
        self.api.stars = APIError(500)
        with self.assertRaises(APIError):
            self.snapshot()

    def test_publication_only_creates_or_advances_signals_and_never_main(self):
        for exists in (False, True):
            with self.subTest(exists=exists):
                self.api.exists = exists
                self.api.calls.clear()
                document = {'schema_version': 1, 'generated_at': 'now', 'skills': {}, 'author_repos': {}}
                signals.publish(self.api, REPO, document)
                commit = next(c[2] for c in self.api.calls if c[1].endswith('/git/commits'))
                self.assertEqual(commit['parents'], ['old'] if exists else [])
                tree = next(c[2] for c in self.api.calls if c[1].endswith('/git/trees'))
                self.assertEqual(tree['tree'], [{'path': 'signals.json', 'mode': '100644', 'type': 'blob', 'sha': 'blob'}])
                last = self.api.calls[-1]
                if exists:
                    self.assertEqual(last, ('PATCH', f'/repos/{REPO}/git/refs/heads/signals', {'sha': 'commit', 'force': False}))
                else:
                    self.assertEqual(last[2], {'ref': 'refs/heads/signals', 'sha': 'commit'})
                self.assertFalse(any('main' in str(c) for c in self.api.calls))

    def test_cli_disabled_discussions_writes_empty_file_and_exits_successfully(self):
        self.api.enabled = False
        with tempfile.TemporaryDirectory(prefix='hub-signals-') as tmp:
            output = Path(tmp) / 'signals.json'
            with patch.object(signals, 'GitHub', return_value=self.api), patch.dict(os.environ, {'GITHUB_REPOSITORY': REPO}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(signals.main(['--output', str(output)]), 0)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['skills'], {})
            self.assertEqual(self.api.calls[-1][1], f'/repos/{REPO}/git/refs/heads/signals')
            output.unlink()
            failure = io.StringIO()
            with patch.object(signals, 'GitHub', side_effect=RuntimeError('synthetic')), \
                    contextlib.redirect_stderr(failure):
                self.assertEqual(signals.main(['--output', str(output)]), 1)
            self.assertFalse(output.exists())
            self.assertIn('(RuntimeError: synthetic)', failure.getvalue())  # the cause, never a credential
