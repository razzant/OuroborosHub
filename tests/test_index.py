"""Canonical hub inputs, release bytes and old-client projection.

Set OUROBOROS_IDENTITY_CORE_ROOT to opt into digest parity with a core checkout.
"""
from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import pytest

from scripts import build_catalog as wrapper
from scripts import build_index as builder
from scripts import ci_checks, sync_mirror
from scripts.release_digest import (DIGEST_DOMAIN, PER_INSTALL_NOTE_FILENAMES, declaration_json,
                                    manifest_declaration, payload_digest, payload_files, release_declaration)

ROOT = Path(__file__).resolve().parents[1]


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-index-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'entries').mkdir()
        (self.root / 'skills').mkdir()
        self.write('hub.json', {'schema_version': 2, 'name': 'OuroborosHub', 'maintainer': 'razzant'})
        self.write('categories.json', {'schema_version': 1, 'categories': [
            {'id': 'search', 'label': 'Search & research'}, {'id': 'writing', 'label': 'Writing & review'}]})
        self.write('official.json', {'schema_version': 1, 'maintainer': 'razzant', 'endorsements': []})
        self.entry = self.add('demo')

    def write(self, name, value):
        (self.root / name).write_bytes(builder.json_bytes(value))

    def add(self, name):
        directory = self.root / 'skills' / name
        directory.mkdir()
        (directory / 'SKILL.md').write_text(
            f'---\nname: {name}\nversion: "nightly blue"\ntype: instruction\n---\nSkill body\n', encoding='utf-8')
        entry = {'schema_version': 2, 'name': name, 'description': 'Curated card text', 'category': ['search'],
                 'owner': 'publisher', 'version': 'nightly blue', 'source': {'kind': 'hosted', 'path': f'skills/{name}'},
                 'digest': payload_digest(directory), 'withdrawn': None, 'when_to_use': 'When research is needed.'}
        self.write(f'entries/{name}.json', entry)
        return entry

    def test_deterministic_sorted_projection_and_compatible_cli(self):
        self.add('aaa')
        self.add('zzz')
        self.assertEqual(wrapper.main([], root=self.root), 0)
        path = self.root / 'catalog.json'
        before = path.read_bytes(), path.stat().st_mtime_ns
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main(['--check'], root=self.root), 0)
            self.assertEqual(wrapper.main(['--check'], root=self.root), 0)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(builder.json_bytes(builder.build_catalog(self.root)), before[0])
        catalog = builder.read_json(path)
        self.assertEqual([r['slug'] for r in catalog['skills']], ['aaa', 'demo', 'zzz'])
        self.assertEqual(catalog['skills'][1]['description'], self.entry['description'])
        self.assertEqual(catalog['skills'][1]['when_to_use'], self.entry['when_to_use'])
        self.assertNotIn('owner', catalog['skills'][1])
        builder.validate_catalog(self.root, catalog)

    def test_check_refuses_stale_missing_and_malformed_inputs_without_rewriting(self):
        builder.main([], root=self.root)
        path = self.root / 'catalog.json'
        for raw in (b'{}', path.read_bytes() + b'\n'):
            path.write_bytes(raw)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(builder.main(['--check'], root=self.root), 1)
            self.assertEqual(path.read_bytes(), raw)
        path.unlink()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(builder.main(['--check'], root=self.root), 1)
        self.assertFalse(path.exists())
        for text in ('{', '[]', '{"schema_version":2,"schema_version":2}'):
            (self.root / 'hub.json').write_text(text, encoding='utf-8')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(builder.main([], root=self.root), 1)
            self.assertFalse(path.exists())

    def test_schema_guards_accept_valid_shapes_and_refuse_invalid_ones(self):
        builder.validate_entry(self.entry, {'search', 'writing'})
        valid = copy.deepcopy(self.entry)
        valid.update(category=['search', 'writing'], withdrawn={'reason': 'Replaced', 'at': '2026-10-17'})
        builder.validate_entry(valid, {'search', 'writing'})
        mutations = [
            {'schema_version': True}, {'schema_version': 1}, {'owner': ''}, {'owner': 'two people'},
            {'category': []}, {'category': ['other']}, {'category': ['search'] * 2},
            {'category': ['search', 'writing', 'search']}, {'category': [{}]}, {'category': 'search'},
            {'name': '../escape'}, {'name': 'x' * 65}, {'description': ''}, {'version': 7},
            {'digest': 'a' * 63}, {'digest': 'sha256v2:' + 'a' * 64}, {'digest': 'G' * 64},
            {'when_to_use': 1}, {'type': 'unknown'}, {'withdrawn': True},
            {'withdrawn': {'reason': 'why', 'at': '2026-02-30'}},
            {'withdrawn': {'reason': '', 'at': '2026-01-01'}},
            {'withdrawn': {'reason': 'why', 'at': '20261017'}},
            {'withdrawn': {'reason': 'why', 'at': '2026-01-01', 'extra': True}},
            {'source': {'kind': 'hosted', 'path': 'skills/another'}},
            {'source': {'kind': 'hosted', 'path': '../demo'}}, {'source': {}},
        ]
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                builder.validate_entry({**self.entry, **changes}, {'search', 'writing'})
        for key in self.entry:
            row = dict(self.entry)
            del row[key]
            if key != 'when_to_use':
                with self.subTest(missing=key), self.assertRaises(ValueError):
                    builder.validate_entry(row, {'search'})

    def test_category_and_entry_uniqueness(self):
        for rows in ([], [{'id': 'search', 'label': 'Search'}] * 2, [{'id': 'bad id', 'label': 'Search'}]):
            self.write('categories.json', {'schema_version': 1, 'categories': rows})
            with self.assertRaises(ValueError):
                builder.load_hub(self.root)
        self.write('categories.json', {'schema_version': 1, 'categories': [{'id': 'search', 'label': 'Search'}]})
        self.write('entries/duplicate.json', self.entry)
        with self.assertRaises(ValueError):
            builder.load_hub(self.root)

        (self.root / 'entries/duplicate.json').unlink()
        (self.root / 'entries/demo.json').rename(self.root / 'entries/wrong.json')
        with self.assertRaisesRegex(ValueError, 'filename'):
            builder.load_hub(self.root)

    def test_connector_metadata_refresh_validation_and_legacy_projection(self):
        manifest = self.root / 'skills/demo/SKILL.md'
        manifest.write_text('---\nversion: v2\nicon: ui/icon.svg\ncontributes:\n'
                            '  - {id: slack, provides: place, destination: "vendor:Slack"}\n'
                            '  - {id: mic, provides: stt, destination: "vendor:Speech"}\n---\n', encoding='utf-8')
        self.assertEqual(builder.main(['--refresh', 'demo'], root=self.root), 0)
        entry = builder.read_json(self.root / 'entries/demo.json')
        self.assertEqual((entry['icon'], entry['connects']), ('ui/icon.svg', ['Slack']))
        builder.validate_entry(entry, {'search'})
        catalog = builder.build_catalog(self.root)
        self.assertEqual(catalog['schema_version'], 1)
        self.assertEqual(catalog['skills'][0]['connects'], ['Slack'])
        for changes in ({'icon': '../bad.svg'}, {'icon': 'https://bad/icon.svg'},
                        {'connects': 'Slack'}, {'connects': ['Slack', 'Slack']}, {'connects': [None]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                builder.validate_entry({**entry, **changes}, {'search'})
        self.write('entries/demo.json', {**entry, 'connects': ['Other']})
        with self.assertRaisesRegex(ValueError, 'must derive'):
            builder.build_catalog(self.root)
        # An old publisher may omit the new keys; consumers still get derived facts.
        self.write('entries/demo.json', {k: v for k, v in entry.items() if k not in {'connects', 'icon'}})
        self.assertEqual(builder.build_catalog(self.root)['skills'][0]['connects'], ['Slack'])
        manifest.write_text('---\nversion: v3\n---\n', encoding='utf-8')
        self.assertEqual(builder.main(['--refresh', 'demo'], root=self.root), 0)
        self.assertNotIn('connects', builder.read_json(self.root / 'entries/demo.json'))
        self.assertNotIn('icon', builder.build_catalog(self.root)['skills'][0])

    def test_endorsements_bind_exact_release_and_recommendations_are_boolean(self):
        endorsement = {'name': 'demo', 'digest': self.entry['digest'], 'recommended': True}
        def write(rows, maintainer='razzant'):
            self.write('official.json', {'schema_version': 1, 'maintainer': maintainer, 'endorsements': rows})
        write([endorsement])
        builder.build_catalog(self.root)
        for rows in ([endorsement] * 2, [{**endorsement, 'name': 'missing'}],
                     [{**endorsement, 'digest': '0' * 64}], [{**endorsement, 'recommended': 1}]):
            write(rows)
            with self.assertRaises(ValueError):
                builder.build_catalog(self.root)
        write([], 'different')
        with self.assertRaises(ValueError):
            builder.build_catalog(self.root)

    def test_payload_change_requires_digest_and_manifest_version_agrees(self):
        builder.main([], root=self.root)
        before = (self.root / 'catalog.json').read_bytes()
        payload = self.root / 'skills/demo/asset.bin'
        payload.write_bytes(b'\x00inert asset\xff')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(builder.main([], root=self.root), 1)
        self.assertEqual((self.root / 'catalog.json').read_bytes(), before)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(builder.main(['--digest', 'demo'], root=self.root), 0)
        self.entry['digest'] = printed.getvalue().strip()
        self.write('entries/demo.json', self.entry)
        self.assertEqual(builder.main([], root=self.root), 0)
        self.entry['version'] = 'different'
        self.write('entries/demo.json', self.entry)
        with self.assertRaisesRegex(ValueError, 'version'):
            builder.build_catalog(self.root)

    def test_hosted_path_missing_and_escape(self):
        directory = self.root / 'skills/demo'
        directory.rename(self.root / 'hidden')
        with self.assertRaisesRegex(ValueError, 'hosted path'):
            builder.build_catalog(self.root)
        directory.symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'hosted path'):
            builder.build_catalog(self.root)

    def test_withdrawn_and_left_source_retain_legacy_delivery(self):
        self.entry['withdrawn'] = {'reason': 'Use another release', 'at': '2026-10-17'}
        self.write('entries/demo.json', self.entry)
        self.assertEqual(builder.build_catalog(self.root)['skills'][0]['slug'], 'demo')
        self.add('other')
        (self.root / 'entries/demo.json').unlink()
        self.assertEqual([r['slug'] for r in builder.build_catalog(self.root)['skills']], ['demo', 'other'])

    def test_per_version_withdrawals_remain_in_v2_entries_and_leave_v1_unchanged(self):
        self.assertEqual(builder.main([], root=self.root), 0)
        before = (self.root / 'catalog.json').read_bytes()
        releases = [{'digest': 'a' * 64, 'version': 'nightly red', 'reason': 'Incorrect results'},
                    {'digest': 'b' * 64, 'version': '', 'reason': 'Release label unknown'}]
        self.entry['withdrawn_releases'] = releases
        self.write('entries/demo.json', self.entry)
        self.assertEqual(builder.main([], root=self.root), 0)
        self.assertEqual(builder.main(['--refresh', 'demo'], root=self.root), 0)
        self.assertEqual(builder.load_hub(self.root)[1]['demo']['withdrawn_releases'], releases)
        self.assertEqual(builder.read_json(self.root / 'entries/demo.json')['withdrawn_releases'], releases)
        self.assertEqual((self.root / 'catalog.json').read_bytes(), before)
        self.assertEqual(builder.main(['--check'], root=self.root), 0)

    def test_per_version_withdrawals_reject_invalid_digests_reasons_and_current_release(self):
        release = {'digest': 'a' * 64, 'version': 'v1', 'reason': 'Incorrect results'}
        for valid in ([], [release]):
            builder.validate_entry({**self.entry, 'withdrawn_releases': valid}, {'search'})
        malformed = [None, {}, '', [None], [release, release],
                     [{**release, 'digest': self.entry['digest']}]]
        for changes in ({'digest': 'A' * 64}, {'digest': 'a' * 63}, {'digest': 'g' * 64}, {'digest': 1},
                        {'version': 1}, {'reason': ''}, {'reason': ' \n\t'}, {'reason': None}, {'extra': 'unexpected'}):
            malformed.append([{**release, **changes}])
        malformed.extend([{k: v for k, v in release.items() if k != missing}] for missing in release)
        for releases in malformed:
            with self.subTest(releases=releases), self.assertRaises(ValueError):
                builder.validate_entry({**self.entry, 'withdrawn_releases': releases}, {'search'})

    def test_git_schema_exact_pin_and_digest_verification(self):
        source = {'kind': 'git', 'repo': 'https://github.com/author/repo', 'path': '', 'commit': 'a' * 40}
        self.entry['source'] = source
        builder.validate_entry(self.entry, {'search'})
        for changes in ({'commit': 'main'}, {'commit': 'a' * 39}, {'path': '/root'}, {'path': '../x'},
                        {'repo': 'https://secret@github.com/a/b'}, {'repo': 'file:///tmp/repo'}, {'path': 'a\\b'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                builder.validate_entry({**self.entry, 'source': {**source, **changes}}, {'search'})
        self.write('entries/demo.json', self.entry)
        # A pointer still checks fetched bytes; its old raw payload remains a v1 row.
        with patch.object(builder, 'source_directory', return_value=contextlib.nullcontext(self.root / 'skills/demo')):
            self.assertEqual(builder.build_catalog(self.root)['skills'][0]['slug'], 'demo')
            self.entry['digest'] = '0' * 64
            self.write('entries/demo.json', self.entry)
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                builder.build_catalog(self.root)

    def test_git_fetch_resolves_exact_commit_confines_path_and_releases_checkout(self):
        upstream = self.root / 'upstream'
        upstream.mkdir()
        def git(*args):
            return builder.run_git(['-c', 'user.name=Test', '-c', 'user.email=test@example.test', *args], upstream)
        git('init', '-q', '-b', 'main')
        (upstream / 'SKILL.md').write_text('original', encoding='utf-8')
        git('add', '.')
        git('commit', '-qm', 'first')
        commit = git('rev-parse', 'HEAD')
        (upstream / 'SKILL.md').write_text('later', encoding='utf-8')
        git('commit', '-qam', 'second')
        # The production schema requires HTTPS. Only this isolated transport test uses a local repo.
        entry = {**self.entry, 'source': {'kind': 'git', 'repo': str(upstream), 'path': '', 'commit': commit}}
        with builder.source_directory(self.root, entry) as checkout:
            self.assertEqual((checkout / 'SKILL.md').read_text(encoding='utf-8'), 'original')
        self.assertFalse(checkout.exists())
        entry['source']['path'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'path missing'):
            with builder.source_directory(self.root, entry):
                self.fail('missing path accepted')
        with patch.object(builder, 'run_git', side_effect=['', '', '0' * 40]), self.assertRaisesRegex(ValueError, 'different commit'):
            with builder.source_directory(self.root, entry):
                self.fail('wrong commit accepted')

    def test_refresh_copies_raw_declaration_version_and_digest_without_renewing_endorsements(self):
        self.write('official.json', {'schema_version': 1, 'maintainer': 'razzant',
                                    'endorsements': [{'name': 'demo', 'digest': self.entry['digest']}]})
        pinned = (self.root / 'official.json').read_bytes()
        manifest = self.root / 'skills/demo/SKILL.md'
        manifest.write_text('---\nversion: v2\ndependencies:\n  manual: ["Install café"]\n---\nBody', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'raw manifest declaration'):
            builder.build_catalog(self.root)
        self.assertEqual(builder.main(['--refresh', 'demo'], root=self.root), 0)
        refreshed = builder.read_json(self.root / 'entries/demo.json')
        self.assertEqual(refreshed['install_specs'], {'manual': ['Install café']})
        self.assertEqual(refreshed['version'], 'v2')
        self.assertEqual(refreshed['digest'], payload_digest(manifest.parent))
        self.assertEqual((self.root / 'official.json').read_bytes(), pinned)
        with self.assertRaisesRegex(ValueError, 'endorsed release'):
            builder.build_catalog(self.root)
        self.write('official.json', {'schema_version': 1, 'maintainer': 'razzant', 'endorsements': []})
        self.assertEqual(builder.build_catalog(self.root)['skills'][0]['install_specs'], refreshed['install_specs'])
        manifest.write_text('body without dependencies', encoding='utf-8')
        builder.main(['--refresh', 'demo'], root=self.root)
        self.assertNotIn('install_specs', builder.read_json(self.root / 'entries/demo.json'))
        builder.build_catalog(self.root)

    def legacy_change(self):
        manifest = self.root / 'skills/demo/SKILL.md'
        manifest.write_text('---\nversion: v2\ndependencies:\n  manual: ["Install café"]\n---\nBody', encoding='utf-8')
        self.write('catalog.json', {'schema_version': 1, 'skills': [builder.catalog_row(manifest.parent)]})
        return {'catalog.json', 'skills/demo/SKILL.md'}

    def git(self, *args):
        return builder.run_git(['-c', 'user.name=Test', '-c', 'user.email=test@example.test', *args], self.root)

    def test_legacy_pr_refreshes_unchanged_entry_before_validation(self):
        builder.main([], root=self.root)
        self.git('init', '-q', '-b', 'main')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')
        base = self.git('rev-parse', 'HEAD')
        paths = self.legacy_change()
        self.git('commit', '-qam', 'legacy publisher')
        event = {'pull_request': {'base': {'sha': base}, 'head': {'sha': self.git('rev-parse', 'HEAD')}}}
        self.assertEqual(ci_checks.changed_paths(self.root, event), paths)
        with self.assertRaisesRegex(ValueError, 'raw manifest declaration'):
            builder.build_catalog(self.root)
        ci_checks.prepare(self.root, 'pull_request', event)
        refreshed = builder.read_json(self.root / 'entries/demo.json')
        self.assertEqual(refreshed['version'], 'v2')
        self.assertEqual(refreshed['install_specs'], {'manual': ['Install café']})
        self.assertEqual(refreshed['digest'], payload_digest(self.root / 'skills/demo'))
        self.assertEqual(refreshed['description'], self.entry['description'])
        self.assertEqual(builder.main([], root=self.root), 0)
        self.assertEqual(builder.main(['--check'], root=self.root), 0)

    def test_legacy_refresh_does_not_repair_canonical_prs_or_git_pointers(self):
        paths = self.legacy_change()
        before = (self.root / 'entries/demo.json').read_bytes()
        for changes in (paths | {'entries/demo.json'}, paths - {'catalog.json'}, {'catalog.json'}, set()):
            with self.subTest(paths=changes):
                self.assertEqual(builder.refresh_legacy_entries(self.root, changes), [])
                self.assertEqual((self.root / 'entries/demo.json').read_bytes(), before)
                with self.assertRaises(ValueError):
                    builder.build_catalog(self.root)
        self.entry['source'] = {'kind': 'git', 'repo': 'https://github.com/author/repo', 'path': '', 'commit': 'a' * 40}
        self.write('entries/demo.json', self.entry)
        with patch.object(builder, 'source_directory') as source:
            self.assertEqual(builder.refresh_legacy_entries(self.root, paths), [])
            self.assertEqual(builder.refresh_legacy_entries(self.root), [])
        source.assert_not_called()

    def test_legacy_official_change_still_requires_exact_reendorsement(self):
        self.write('official.json', {'schema_version': 1, 'maintainer': 'razzant',
                                    'endorsements': [{'name': 'demo', 'digest': self.entry['digest']}]})
        before = (self.root / 'official.json').read_bytes()
        paths = self.legacy_change()
        self.assertEqual(builder.refresh_legacy_entries(self.root, paths), ['entries/demo.json'])
        self.assertEqual((self.root / 'official.json').read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'mismatched endorsed release'):
            builder.build_catalog(self.root)
        refreshed = builder.read_json(self.root / 'entries/demo.json')
        self.write('official.json', {'schema_version': 1, 'maintainer': 'razzant',
                                    'endorsements': [{'name': 'demo', 'digest': refreshed['digest']}]})
        builder.build_catalog(self.root)

    def test_post_merge_publication_commits_refreshed_entry_and_catalog_together(self):
        builder.main([], root=self.root)
        self.git('init', '-q', '-b', 'main')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')
        self.legacy_change()
        self.git('commit', '-qam', 'merge legacy publisher')
        pushes = []
        def transport(root, args, token=None):
            if args[0] == 'push':
                pushes.append((args, token))
                return ''
            return builder.run_git(args, root)
        with patch.object(sync_mirror, 'git', side_effect=transport):
            sync_mirror.sync(self.root, 'synthetic-repo', 'synthetic-mirror')
            published = self.git('rev-parse', 'HEAD')
            self.assertEqual(self.git('diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD').splitlines(),
                             ['catalog.json', 'entries/demo.json'])
            self.assertEqual(builder.read_json(self.root / 'entries/demo.json')['version'], 'v2')
            self.assertEqual(builder.main(['--check'], root=self.root), 0)
            self.assertEqual(self.git('status', '--porcelain'), '')
            sync_mirror.sync(self.root, 'synthetic-repo', 'synthetic-mirror')
            self.assertEqual(self.git('rev-parse', 'HEAD'), published)
        self.assertEqual([token for _args, token in pushes], ['synthetic-repo', 'synthetic-mirror', 'synthetic-mirror'])

    def test_legacy_pin_tracks_committed_payload_and_survives_generated_commits(self):
        def git(*args):
            return builder.run_git(['-c', 'user.name=Test', '-c', 'user.email=test@example.test', *args], self.root)
        git('init', '-q', '-b', 'main')
        git('add', '.')
        git('commit', '-qm', 'initial payload')
        first = git('rev-parse', 'HEAD')
        self.assertTrue(builder.legacy_raw_base(self.root).endswith('/' + first))
        builder.main([], root=self.root)
        before = (self.root / 'catalog.json').read_bytes()
        git('add', 'catalog.json')
        git('commit', '-qm', 'generated catalog')
        self.assertEqual(builder.json_bytes(builder.build_catalog(self.root)), before)
        self.entry['description'] = 'New card text'
        self.write('entries/demo.json', self.entry)
        git('commit', '-qam', 'metadata update')
        self.assertTrue(builder.legacy_raw_base(self.root).endswith('/' + first))
        (self.root / 'skills/demo/asset.txt').write_text('new', encoding='utf-8')
        self.assertTrue(builder.legacy_raw_base(self.root).endswith('/main'))
        builder.main(['--refresh', 'demo'], root=self.root)
        git('add', 'skills', 'entries')
        git('commit', '-qm', 'new payload')
        latest = git('rev-parse', 'HEAD')
        self.assertNotEqual(first, latest)
        self.assertTrue(builder.build_catalog(self.root)['raw_base_url'].endswith('/' + latest))


class ReleaseIdentityTests(unittest.TestCase):
    def test_all_fifty_v1_rows_keep_every_original_key(self):
        original = json.loads(builder.run_git(['show', '6d604c9:catalog.json'], ROOT))
        previous = {row['slug']: row for row in original['skills']}
        generated = builder.build_catalog(ROOT)
        current = {row['slug']: row for row in generated['skills']}
        self.assertEqual(len(previous), 50)
        self.assertTrue(set(previous) <= set(current))
        self.assertTrue(original.keys() <= generated.keys())
        for name, row in previous.items():
            with self.subTest(skill=name):
                self.assertTrue(row.keys() <= current[name].keys())
                self.assertNotIn('withdrawn_releases', current[name])

    def test_walk_notes_cache_assets_declared_entries_and_dependency_identity(self):
        with tempfile.TemporaryDirectory(prefix='hub-digest-') as tmp:
            root = Path(tmp) / 'payload'
            root.mkdir()
            alias = Path(tmp) / 'linked-payload'
            alias.symlink_to(root, target_is_directory=True)
            (root / 'SKILL.md').write_text('payload', encoding='utf-8')
            initial = payload_digest(root)
            for name in PER_INSTALL_NOTE_FILENAMES:
                (root / name).write_text('machine note', encoding='utf-8')
            (root / '__pycache__').mkdir()
            (root / '__pycache__/ignored.pyc').write_bytes(b'cache')
            self.assertEqual(payload_digest(root), initial)
            (root / 'nested').mkdir()
            (root / 'nested/.clawhub.json').write_text('payload note', encoding='utf-8')
            self.assertNotEqual(payload_digest(root), initial)
            (root / 'data.wasm').write_bytes(b'\0asset')
            (root / '.env.example').write_text('example', encoding='utf-8')
            for directory in (root, alias):
                with self.subTest(directory=directory.name):
                    resolved = directory.resolve()
                    names = [p.relative_to(resolved).as_posix() for p in payload_files(directory)]
                    self.assertEqual(names, ['.env.example', 'SKILL.md', 'data.wasm', 'nested/.clawhub.json'])
                    self.assertIn('__pycache__/ignored.pyc', [p.relative_to(resolved).as_posix() for p in payload_files(
                        directory, manifest_entry='__pycache__/ignored.pyc')])
            base = payload_digest(root)
            (root / '.clawhub.json').write_text(json.dumps({'install_specs': {
                'raw': [{'kind': 'brew', 'package': 'x'}]}}), encoding='utf-8')
            # Manual declarations are part of the release too; nothing is normalized away.
            self.assertNotEqual(payload_digest(root), base)
            (root / '.env').write_text('synthetic', encoding='utf-8')
            with self.assertRaises(ValueError):
                payload_digest(root)

    def test_exact_raw_recipe_sorting_and_sidecar_precedence(self):
        with tempfile.TemporaryDirectory(prefix='hub-raw-digest-') as tmp:
            root = Path(tmp)
            (root / 'a').mkdir()
            (root / 'a/z.txt').write_text('nested', encoding='utf-8')
            (root / 'a.txt').write_text('sibling', encoding='utf-8')
            front = {'install_specs': [], 'install': {'manual': 'café'}, 'dependencies': ['ignored']}
            (root / 'SKILL.md').write_text('\ufeff' + json.dumps(front), encoding='utf-8')
            self.assertEqual(release_declaration(root), front['install'])
            expected = hashlib.sha256(DIGEST_DOMAIN)
            for path in sorted(('SKILL.md', 'a.txt', 'a/z.txt')):
                expected.update(path.encode() + b'\0' + hashlib.sha256((root / path).read_bytes()).digest())
            expected.update(b'\0install_specs\0' + hashlib.sha256(
                declaration_json(front['install']).encode()).hexdigest().encode())
            self.assertEqual(payload_digest(root), expected.hexdigest())
            for key in ('raw', 'auto'):
                for name in ('.clawhub.json', '.ouroboroshub.json'):
                    (root / name).write_text(json.dumps({'install_specs': {key: name}}), encoding='utf-8')
                    self.assertEqual(release_declaration(root), name)
                (root / '.ouroboroshub.json').unlink()
            (root / '.clawhub.json').write_text('{invalid', encoding='utf-8')
            self.assertEqual(release_declaration(root), front['install'])
            self.assertIsNone(manifest_declaration({'install_specs': {}, 'install': '', 'dependencies': None}))
            (root / 'SKILL.md').rename(root / 'skill.json')
            self.assertEqual(release_declaration(root), front['install'])

    def test_core_digest_check_skips_without_an_explicit_checkout(self):
        with patch.dict(os.environ), patch.object(subprocess, 'run') as run:
            os.environ.pop('OUROBOROS_IDENTITY_CORE_ROOT', None)
            with self.assertRaisesRegex(unittest.SkipTest, 'OUROBOROS_IDENTITY_CORE_ROOT'):
                self.test_all_hosted_releases_match_core_read_only()
            run.assert_not_called()

    def test_all_hosted_releases_match_core_read_only(self):
        configured = os.environ.get('OUROBOROS_IDENTITY_CORE_ROOT', '').strip()
        if not configured:
            self.skipTest('set OUROBOROS_IDENTITY_CORE_ROOT for the cross-repository digest check')
        core = Path(configured).resolve()
        rows = [builder.read_json(path) for path in sorted((ROOT / 'entries').glob('*.json'))]
        cases = [{'path': str(ROOT / r['source']['path']),
                  'front': builder.read_manifest(ROOT / r['source']['path'])} for r in rows if r['source']['kind'] == 'hosted']
        code = '''import json,sys
from pathlib import Path
from ouroboros.skill_identity import payload_digest
cases=json.load(sys.stdin)
print(json.dumps([payload_digest(Path(c['path']), manifest_entry=c['front'].get('entry',''),
    manifest_scripts=c['front'].get('scripts')) for c in cases]))
'''
        with tempfile.TemporaryDirectory(prefix='hub-core-identity-') as tmp:
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': tmp, 'PYTHONPATH': str(core),
                   'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                   'OUROBOROS_APP_ROOT': tmp, 'OUROBOROS_DATA_DIR': tmp + '/data',
                   'OUROBOROS_REPO_DIR': tmp + '/repo', 'OUROBOROS_SETTINGS_PATH': tmp + '/settings.json'}
            run = subprocess.run([sys.executable, '-B', '-c', code], input=json.dumps(cases),
                                 text=True, encoding='utf-8', capture_output=True, cwd=tmp, env=env, timeout=90)
        self.assertEqual(run.returncode, 0, run.stderr)
        expected = json.loads(run.stdout.strip().splitlines()[-1])
        actual = [payload_digest(Path(c['path']), manifest_entry=c['front'].get('entry', ''),
                  manifest_scripts=c['front'].get('scripts')) for c in cases]
        self.assertEqual(actual, expected)
        self.assertEqual(actual, [r['digest'] for r in rows if r['source']['kind'] == 'hosted'])

    def test_initial_membership_ownership_categories_and_official_split(self):
        membership = builder.read_json(ROOT / 'tests/fixtures/initial_membership.json')
        hub, entries, official = builder.load_hub(ROOT)
        self.assertEqual(len(membership), 50)
        self.assertTrue(set(membership) <= set(entries))
        for name, facts in membership.items():
            self.assertEqual(entries[name]['owner'], facts['owner'])
        categories = builder.read_json(ROOT / 'categories.json')['categories']
        self.assertEqual(len(categories), 12)
        expected = set('a2a adaptive-code-review anti-slop cache_efficiency_snapshot claudexor_quotas confluence '
                       'context-lens duckduckgo email email-presence github-presence google-workspace keenable memory-atlas perplexity '
                       'slack-bridge telegram-bot pdf_tools word_tools'.split())
        self.assertEqual({r['name'] for r in official['endorsements']}, expected)
        self.assertEqual({r['name'] for r in official['endorsements'] if r.get('recommended')},
                         set('duckduckgo pdf_tools word_tools anti-slop memory-atlas'.split()))
        generated = builder.build_catalog(ROOT)
        self.assertEqual({r['slug'] for r in generated['skills']}, set(entries))
        for entry in entries.values():
            with builder.source_directory(ROOT, entry) as directory:
                self.assertEqual(declaration_json(entry.get('install_specs')),
                                 declaration_json(manifest_declaration(builder.read_manifest(directory))))


CONNECTORS = {"slack-bridge": ("Slack", "slack_"), "telegram-bot": ("Telegram", "telegram_"),
              "email-presence": ("Email", "email_"), "a2a": ("A2A", "a2a_"), "agentrq-bridge": ("AgentRQ", "rq_")}


def exported_names(directory, prefix):
    names = set()
    for path in [directory / "plugin.py", *directory.glob("lib/read_tools.py")]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                index = 1 if function == "register_json_tool" else 0
                if function in {"tool", "register_tool", "register_json_tool"} and len(node.args) > index:
                    arg = node.args[index]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        names.add(arg.value)
            # Dynamic registration descriptors enumerate the same tool names.
            if isinstance(node, ast.Tuple) and node.elts and isinstance(node.elts[0], ast.Constant):
                name = node.elts[0].value
                if isinstance(name, str) and name.startswith(prefix):
                    names.add(name)
    return names


@pytest.mark.parametrize("name", CONNECTORS)
def test_every_exported_tool_has_an_effect_and_destination(name):
    directory = ROOT / "skills" / name
    manifest = builder.read_manifest(directory)
    service, prefix = CONNECTORS[name]
    rows = manifest["tools"]
    assert {row["name"] for row in rows} == exported_names(directory, prefix)
    assert len(rows) == len({row["name"] for row in rows})
    for row in rows:
        assert row["effect"] in {"read", "message", "write"}
        target = row["target"]
        assert isinstance(target, (str, list))
        for argument in ([target] if isinstance(target, str) else target):
            assert re.fullmatch(r"[a-z][a-z0-9_]*", argument)
    places = [row for row in manifest["contributes"] if row["provides"] == "place"]
    assert len(places) == 1
    place = places[0]
    assert place["label"] == service and place["destination"] == "vendor:" + service
    assert place["scope"] == "account" and place["wakes"]
    assert "presence" in manifest["permissions"] and "inject_chat" not in manifest["permissions"]
    assert manifest["icon"] == place["icon"] == "ui/icon.svg"
    icon = ET.parse(directory / place["icon"]).getroot()
    assert icon.attrib["viewBox"] == "0 0 24 24"
    assert "currentColor" in icon.attrib.values()
    assert all(element.tag.rsplit("}", 1)[-1] in {"svg", "path", "rect", "circle", "line", "polyline", "polygon"} for element in icon.iter())
    assert not any(key.startswith("on") or key in {"href", "style"} for element in icon.iter() for key in element.attrib)


def test_mixed_tools_remain_write_and_email_covers_all_recipients():
    manifests = {name: {row["name"]: row for row in builder.read_manifest(ROOT / "skills" / name)["tools"]} for name in CONNECTORS}
    for connector, name in [("slack-bridge", "slack_api"), ("email-presence", "email_read"),
                            ("email-presence", "email_mailbox"), ("telegram-bot", "telegram_operation")]:
        assert manifests[connector][name]["effect"] == "write"
    assert manifests["slack-bridge"]["slack_history"]["effect"] == "read"
    assert manifests["email-presence"]["email_send"]["effect"] == "message"
    assert manifests["email-presence"]["email_send"]["target"] == ["to", "cc", "bcc"]
