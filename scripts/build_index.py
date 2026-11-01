#!/usr/bin/env python3
"""Validate hub v2 releases and generate the deterministic v1 catalog."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.release_digest import declaration_json, manifest_declaration, payload_digest, payload_files, read_manifest

def _canonical_name(slug: str) -> str:
    """Ouroboros skill_manifest canonical_skill_name: installation identity.

    Display names are independent. Keep this small public naming contract
    consistent with the consumer without importing its application runtime.
    """
    cleaned = ''.join(ch if ch.isalnum() or ch in '-_.' else '_' for ch in slug.strip()).strip('._')
    return cleaned[:64] if cleaned else '_unnamed'


def _file_facts(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def validate_catalog(root: Path, catalog: object) -> None:
    """Validate published declarations, preserving curated metadata and membership."""
    if not isinstance(catalog, dict) or type(catalog.get('schema_version')) is not int or catalog['schema_version'] != 1:
        raise ValueError('catalog.schema_version must be 1')
    # The consumer derives an omitted/empty base from its GitHub catalog URL.
    # Explicit bases must support appending /skills/... under the same public
    # URL contract as ouroboros.marketplace.ouroboroshub._fetch_bytes.
    raw_base = catalog.get('raw_base_url')
    if raw_base is not None and raw_base != '':
        if not isinstance(raw_base, str):
            raise ValueError('catalog.raw_base_url must be a URL string')
        url = urlsplit(raw_base)
        if (url.scheme not in {'http', 'https'}
                or url.hostname not in {'raw.githubusercontent.com', 'github.com', 'localhost', '127.0.0.1'}
                or (url.scheme == 'http' and url.hostname not in {'localhost', '127.0.0.1'})
                or url.username is not None or url.password is not None or url.query or url.fragment):
            raise ValueError('catalog.raw_base_url must be a supported absolute base URL without credentials, query or fragment')
        try:
            url.port
        except ValueError as exc:
            raise ValueError('catalog.raw_base_url has an invalid port') from exc
    entries = catalog.get('skills')
    if not isinstance(entries, list):
        raise ValueError('catalog.skills must be a list')
    identities: dict[str, str] = {}
    skills_root = (root / 'skills').resolve()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get('slug'), str) or not entry['slug'].strip():
            raise ValueError(f'catalog.skills[{index}].slug must be a non-empty string')
        slug = entry['slug'].strip()
        if '/' in slug or '\\' in slug or ':' in slug or slug in {'.', '..'}:
            raise ValueError(f'{slug!r}: slug must name one skill directory')
        canonical = _canonical_name(slug)
        if canonical == '_unnamed':
            raise ValueError(f'{slug!r}: slug has no usable installation identity')
        if canonical in identities:
            raise ValueError(f'canonical slug collision: {identities[canonical]!r} and {slug!r} install as {canonical!r}')
        identities[canonical] = slug
        if not isinstance(entry.get('version'), str) or not entry['version'].strip():
            raise ValueError(f'{slug}: version must be a non-empty string')
        if 'name' in entry and not isinstance(entry['name'], str):
            raise ValueError(f'{slug}: name must be a string')
        if 'type' in entry and (not isinstance(entry['type'], str) or entry['type'] not in {'instruction', 'script', 'extension'}):
            raise ValueError(f'{slug}: unsupported skill type')
        files = entry.get('files')
        if not isinstance(files, list) or not files:
            raise ValueError(f'{slug}: files must be a non-empty list')
        skill_root = (skills_root / slug).resolve()
        if not skill_root.is_relative_to(skills_root) or not skill_root.is_dir():
            raise ValueError(f'{slug}: skill directory is missing or outside skills/')
        declared: set[str] = set()
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                raise ValueError(f'{slug}: file path must be a string')
            text = item['path'].strip()
            path = PurePosixPath(text)
            if ('\\' in text or ':' in text or not path.parts or path.is_absolute() or '..' in path.parts
                    or any(part in {'node_modules', '.ouroboros_env', '__pycache__'} for part in path.parts)
                    or any(part in {'.git', '.pytest_cache'} for part in path.parts)):
                raise ValueError(f'{slug}: unsupported catalog file path {text!r}')
            relative = path.as_posix()
            if relative in declared:
                raise ValueError(f'{slug}: duplicate declared file {relative!r}')
            declared.add(relative)
            target = (skill_root / path).resolve()
            if not target.is_relative_to(skill_root) or not target.is_file():
                raise ValueError(f'{slug}/{relative}: declared file is missing or outside its skill')
            expected = item.get('sha256')
            if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected.strip()):
                raise ValueError(f'{slug}/{relative}: sha256 must contain 64 hex characters')
            actual, size = _file_facts(target)
            if actual != expected.strip().lower():
                raise ValueError(f'{slug}/{relative}: sha256 mismatch; update the declared hash')
            if 'size' in item and (type(item['size']) is not int or item['size'] != size):
                raise ValueError(f'{slug}/{relative}: size must equal the file byte count ({size})')
        if 'SKILL.md' not in declared:
            raise ValueError(f'{slug}: declared files must include SKILL.md')


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def read_json(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'{path.name}: duplicate JSON key {key}')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)


def object_fields(value, required, optional=(), *, label):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError(f'{label}: expected fields {", ".join(required)}; optional {", ".join(optional)}')


def text_field(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}: must be a non-empty string')


def schema(value, version, label):
    if type(value.get('schema_version')) is not int or value['schema_version'] != version:
        raise ValueError(f'{label}.schema_version must be {version}')


def login(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', value):
        raise ValueError(f'{label}: must be a GitHub login')


def release_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('digest: expected 64 lowercase hex digits (release identity v2)')


def relative_path(value, *, allow_root=False):
    if not isinstance(value, str):
        raise ValueError('source.path must be a relative path')
    path = PurePosixPath(value)
    if (path.is_absolute() or '..' in path.parts or '\\' in value or ':' in value or '\0' in value
            or value != value.strip() or (not path.parts and not allow_root)):
        raise ValueError('source.path must stay inside its repository')
    if value and path.as_posix() != value:
        raise ValueError('source.path must be normalized')
    return path


def validate_entry(entry, categories):
    object_fields(entry, ('schema_version', 'name', 'description', 'category', 'owner', 'version', 'source', 'digest', 'withdrawn'),
                  ('install_specs', 'when_to_use', 'author', 'type', 'withdrawn_releases', 'icon', 'connects', 'title'),
                  label='entry')
    schema(entry, 2, 'entry')
    for field in ('name', 'description', 'version'):
        text_field(entry[field], field)
    name = entry['name']
    if name != _canonical_name(name):
        raise ValueError(f'{name!r}: name must be its canonical installation slug')
    login(entry['owner'], 'owner')
    release_hash(entry['digest'])
    chosen = entry['category']
    if (not isinstance(chosen, list) or not 1 <= len(chosen) <= 2
            or any(not isinstance(c, str) or c not in categories for c in chosen)
            or len(set(chosen)) != len(chosen)):
        raise ValueError(f'{name}: category must contain 1–2 distinct ids from categories.json')
    for field in ('when_to_use', 'author'):
        if field in entry:
            text_field(entry[field], field)
    if 'title' in entry:
        text_field(entry['title'], 'title')
        if entry['title'] != entry['title'].strip() or any(ch in entry['title'] for ch in '\r\n'):
            raise ValueError('title must be one line without surrounding spaces')
    if 'type' in entry and entry['type'] not in ('instruction', 'script', 'extension'):
        raise ValueError(f'{name}: invalid type')
    if 'icon' in entry:
        text_field(entry['icon'], 'icon')
        if '/' in entry['icon']:
            relative_path(entry['icon'])
            if not entry['icon'].startswith('ui/') or not entry['icon'].endswith('.svg'):
                raise ValueError('icon must be a host icon name or ui/*.svg')
        elif not re.fullmatch(r'[a-z][a-z0-9_-]*', entry['icon']):
            raise ValueError('icon must be a host icon name or ui/*.svg')
    if 'connects' in entry:
        connects = entry['connects']
        if (not isinstance(connects, list) or any(not isinstance(v, str) or not v.strip() for v in connects)
                or len(set(connects)) != len(connects)):
            raise ValueError('connects must contain distinct non-empty service names')
    withdrawn = entry['withdrawn']
    if withdrawn is not None:
        object_fields(withdrawn, ('reason', 'at'), label='withdrawn')
        text_field(withdrawn['reason'], 'withdrawn.reason')
        if not isinstance(withdrawn['at'], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', withdrawn['at']):
            raise ValueError('withdrawn.at must be YYYY-MM-DD')
        date.fromisoformat(withdrawn['at'])
    releases = entry.get('withdrawn_releases', [])
    if not isinstance(releases, list):
        raise ValueError('withdrawn_releases must be a list')
    withdrawn_digests = set()
    for release in releases:
        object_fields(release, ('digest', 'version', 'reason'), label='withdrawn release')
        release_hash(release['digest'])
        if not isinstance(release['version'], str):
            raise ValueError('withdrawn release version must be a string')
        text_field(release['reason'], 'withdrawn release reason')
        if release['digest'] == entry['digest'] or release['digest'] in withdrawn_digests:
            raise ValueError('withdrawn release digests must be unique and cannot equal the current release')
        withdrawn_digests.add(release['digest'])
    source = entry['source']
    if not isinstance(source, dict):
        raise ValueError('source must be an object')
    if source.get('kind') == 'hosted':
        object_fields(source, ('kind', 'path'), label='source')
        relative_path(source['path'])
        if source['path'] != f'skills/{name}':
            raise ValueError(f'{name}: hosted source.path must be skills/{name} for v1 delivery')
    elif source.get('kind') == 'git':
        object_fields(source, ('kind', 'repo', 'path', 'commit'), label='source')
        relative_path(source['path'], allow_root=True)
        if not isinstance(source['repo'], str):
            raise ValueError('source.repo must be an HTTPS repository URL')
        url = urlsplit(source['repo'])
        if (url.scheme != 'https' or not url.hostname or not url.path.strip('/') or url.username is not None
                or url.password is not None or url.query or url.fragment):
            raise ValueError('source.repo must be an HTTPS repository URL without credentials, query or fragment')
        if not isinstance(source['commit'], str) or not re.fullmatch(r'[0-9a-f]{40}', source['commit']):
            raise ValueError('source.commit must be an exact 40-hex commit')
    else:
        raise ValueError('source.kind must be hosted or git')


def load_hub(root: Path):
    hub = read_json(root / 'hub.json')
    object_fields(hub, ('schema_version', 'name', 'maintainer'), label='hub')
    schema(hub, 2, 'hub')
    text_field(hub['name'], 'hub.name')
    login(hub['maintainer'], 'hub.maintainer')
    categories = read_json(root / 'categories.json')
    object_fields(categories, ('schema_version', 'categories'), label='categories')
    schema(categories, 1, 'categories')
    if not isinstance(categories['categories'], list) or not categories['categories']:
        raise ValueError('categories must be a non-empty list')
    ids = set()
    for category in categories['categories']:
        object_fields(category, ('id', 'label'), label='category')
        text_field(category['id'], 'category.id')
        text_field(category['label'], 'category.label')
        if not re.fullmatch(r'[a-z][a-z0-9-]*', category['id']) or category['id'] in ids:
            raise ValueError('category ids must be unique slugs')
        ids.add(category['id'])
    entries = {}
    for path in sorted((root / 'entries').glob('*.json')):
        entry = read_json(path)
        validate_entry(entry, ids)
        name = entry['name']
        if name in entries:
            raise ValueError(f'duplicate entry name: {name}')
        if path.stem != name:
            raise ValueError(f'{path.name}: filename must match entry name')
        entries[name] = entry
    if not entries:
        raise ValueError('entries/ must contain skill entries')
    official = read_json(root / 'official.json')
    object_fields(official, ('schema_version', 'maintainer', 'endorsements'), label='official')
    schema(official, 1, 'official')
    if official['maintainer'] != hub['maintainer']:
        raise ValueError('official.maintainer must equal hub.maintainer')
    if not isinstance(official['endorsements'], list):
        raise ValueError('official.endorsements must be a list')
    endorsed = set()
    for item in official['endorsements']:
        object_fields(item, ('name', 'digest'), ('recommended',), label='endorsement')
        text_field(item['name'], 'endorsement.name')
        release_hash(item['digest'])
        if 'recommended' in item and type(item['recommended']) is not bool:
            raise ValueError('recommended must be boolean')
        name = item['name']
        if name in endorsed or name not in entries or entries[name]['digest'] != item['digest']:
            raise ValueError(f'{name}: duplicate, missing or mismatched endorsed release')
        endorsed.add(name)
    return hub, entries, official


def run_git(args, directory, *, env=None):
    # A deadline owns the entire git/transport process group, including transport helpers.
    with subprocess.Popen(['git', *args], cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True, env={**(env or os.environ), 'GIT_TERMINAL_PROMPT': '0'}) as process:
        try:
            stdout, _stderr = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise ValueError('git command timed out') from None
        if process.returncode:
            raise ValueError('git command failed; check the repository, commit and access')
        return stdout.decode('utf-8').strip()


@contextmanager
def source_directory(root, entry):
    source = entry['source']
    if source['kind'] == 'hosted':
        directory = (root / source['path']).resolve()
        if not directory.is_relative_to(root.resolve()) or not directory.is_dir():
            raise ValueError(f"{entry['name']}: hosted path missing or outside repository")
        yield directory
    else:
        with tempfile.TemporaryDirectory(prefix='hub-source-') as scratch:
            checkout = Path(scratch)
            run_git(['init', '-q'], checkout)
            run_git(['fetch', '-q', '--depth=1', source['repo'], source['commit']], checkout)
            if run_git(['rev-parse', 'FETCH_HEAD'], checkout) != source['commit']:
                raise ValueError('git source resolved to a different commit')
            run_git(['checkout', '-q', '--detach', 'FETCH_HEAD'], checkout)
            directory = (checkout / source['path']).resolve()
            if not directory.is_relative_to(checkout.resolve()) or not directory.is_dir():
                raise ValueError('git source path missing or outside repository')
            yield directory


def release_digest(directory, entry):
    front = read_manifest(directory)
    return payload_digest(directory, manifest_entry=front.get('entry', ''), manifest_scripts=front.get('scripts'))


DISCOVERY_FACTS = ('icon', 'connects', 'title')


def discovery_facts(front):
    """Discovery facts derive from the reviewed manifest, never a second tag list."""
    facts = {}
    for key in ('icon', 'title'):
        if front.get(key):
            facts[key] = front[key]
    services = set()
    for contribution in front.get('contributes') or []:
        if isinstance(contribution, dict) and contribution.get('provides') == 'place':
            destination = contribution.get('destination', '')
            if isinstance(destination, str) and destination.startswith('vendor:') and destination[7:].strip():
                services.add(destination[7:])
    if services:
        facts['connects'] = sorted(services)
    return facts


def refresh_entry(root, entry):
    """Explicit author operation; an endorsement is never renewed automatically."""
    with source_directory(root, entry) as directory:
        front = read_manifest(directory)
        declaration = manifest_declaration(front)
        if declaration is None:
            entry.pop('install_specs', None)
        else:
            entry['install_specs'] = json.loads(declaration_json(declaration))
        entry['version'] = str(front.get('version', '0.1.0'))
        entry['digest'] = release_digest(directory, entry)
        for key in DISCOVERY_FACTS:
            entry.pop(key, None)
        entry.update(discovery_facts(front))
    return entry


def refresh_legacy_entries(root, changed_paths=None):
    """Normalize old publishers' hosted releases without renewing endorsements.

    PRs must change the catalog and payload while leaving that entry untouched.
    Publication scans main's hosted entries, including legacy merges queued before
    the current checkout. Canonical PR entries still undergo ordinary validation.
    """
    if changed_paths is not None:
        changed_paths = set(changed_paths)
        if 'catalog.json' not in changed_paths:
            return []
        names = {p.split('/')[1] for p in changed_paths if p.startswith('skills/') and len(p.split('/')) > 2}
    else:
        names = {p.stem for p in (root / 'entries').glob('*.json')}
    refreshed = []
    for name in sorted(names):
        relative = f'entries/{name}.json'
        path = root / relative
        if not path.is_file() or (changed_paths is not None and relative in changed_paths):
            continue
        entry = read_json(path)
        if entry['source']['kind'] != 'hosted':
            continue
        updated = refresh_entry(root, dict(entry))
        if updated != entry:
            path.write_bytes(json_bytes(updated))
            refreshed.append(relative)
    return refreshed


def catalog_row(directory, entry=None):
    front = read_manifest(directory)
    row = {'slug': directory.name, 'name': directory.name, 'description': front.get('description', ''),
           'version': str(front.get('version', '0.1.0')), 'type': front.get('type', 'instruction')}
    metadata = entry if entry is not None else front
    for field in ('description', 'version', 'type', 'install_specs', 'when_to_use', 'author'):
        if field in metadata:
            row[field] = metadata[field]
    row.update(discovery_facts(front))
    row['files'] = []
    for path in payload_files(directory, manifest_entry=front.get('entry', ''), manifest_scripts=front.get('scripts')):
        digest, size = _file_facts(path)
        row['files'].append({'path': path.relative_to(directory.resolve()).as_posix(), 'sha256': digest, 'size': size})
    return row


def legacy_raw_base(root):
    """Pin legacy downloads to their payload commit, stable across catalog-only commits.

    Uncommitted author previews (or exported trees without history) use main. CI and
    publication run from a clean checkout with full history and always get an exact pin.
    """
    ref = 'main'
    if (root / '.git').exists() and not run_git(['status', '--porcelain', '--', 'skills'], root):
        commit = run_git(['log', '-1', '--first-parent', '--format=%H', '--', 'skills'], root)
        if re.fullmatch(r'[0-9a-f]{40}', commit):
            ref = commit
    return f'https://raw.githubusercontent.com/razzant/OuroborosHub/{ref}'


def build_catalog(root: Path) -> dict:
    hub, entries, _official = load_hub(root)
    for entry in entries.values():
        with source_directory(root, entry) as directory:
            declaration = manifest_declaration(read_manifest(directory))
            if declaration_json(entry.get('install_specs')) != declaration_json(declaration):
                raise ValueError(f"{entry['name']}: install_specs must copy the raw manifest declaration; use --refresh")
            if release_digest(directory, entry) != entry['digest']:
                raise ValueError(f"{entry['name']}: release digest mismatch; update the entry and any endorsement")
            if str(read_manifest(directory).get('version', '0.1.0')) != entry['version']:
                raise ValueError(f"{entry['name']}: entry version must match SKILL.md")
            facts = discovery_facts(read_manifest(directory))
            # Optional for older schema-2 publishers; supplied facts must be truthful.
            for key in DISCOVERY_FACTS:
                if key in entry and entry[key] != facts.get(key):
                    raise ValueError(f"{entry['name']}: {key} must derive from SKILL.md; use --refresh")
    # Retained hosted copies remain deliverable to old clients if a v2 entry is
    # later withdrawn, converted to a git pointer or removed from discovery.
    rows = []
    for directory in sorted((root / 'skills').iterdir()):
        if directory.is_dir() and (directory / 'SKILL.md').is_file():
            entry = entries.get(directory.name)
            rows.append(catalog_row(directory, entry if entry and entry['source']['kind'] == 'hosted' else None))
    catalog = {'schema_version': 1, 'name': hub['name'], 'description': 'Official Ouroboros skills catalog.',
               'raw_base_url': legacy_raw_base(root), 'skills': rows}
    validate_catalog(root, catalog)
    return catalog


def main(argv=None, *, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='validate releases and require byte-identical generated catalog')
    mode.add_argument('--digest', metavar='NAME', help='print a release digest without rewriting files')
    mode.add_argument('--refresh', metavar='NAME', help='copy manifest declaration/version and release digest into an entry')
    args = parser.parse_args(argv)
    root = root or Path(__file__).resolve().parents[1]
    try:
        if args.digest or args.refresh:
            name = args.digest or args.refresh
            if name != _canonical_name(name):
                raise ValueError('invalid name')
            path = root / 'entries' / f'{name}.json'
            entry = read_json(path)
            if entry['name'] != name:
                raise ValueError('entry filename and name must match')
            if args.refresh:
                path.write_bytes(json_bytes(refresh_entry(root, entry)))
            else:
                with source_directory(root, entry) as directory:
                    print(release_digest(directory, entry))
            return 0
        catalog = build_catalog(root)
        encoded = json_bytes(catalog)
        path = root / 'catalog.json'
        if args.check:
            if not path.is_file() or path.read_bytes() != encoded:
                raise ValueError('catalog.json is stale; run python scripts/build_index.py')
            print(f"Catalog valid: {len(catalog['skills'])} skills")
        else:
            path.write_bytes(encoded)
    except (OSError, ValueError) as exc:
        print(f'Catalog validation failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
