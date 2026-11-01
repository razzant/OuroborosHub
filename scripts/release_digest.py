"""Portable release identity v2; never import or execute a skill or the core runtime.

Contract: ouroboros.skill_identity.payload_digest and skill_loader._iter_payload_files.
Keep the walk (including inert assets and declared entries under cache directories),
top-level note exclusions, reduction and raw declaration hash in sync with core WP-B2.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat

import yaml

DIGEST_DOMAIN = b'ouroboros-skill-release/2\0'
PER_INSTALL_NOTE_FILENAMES = frozenset({
    '.seed-origin', '.ouroboroshub.json', '.clawhub.json', '.self_authored.json',
})
CACHE_NAMES = frozenset({
    '__pycache__', 'node_modules', '.git', '.hg', '.svn', '.idea', '.vscode',
    '.tox', '.pytest_cache', '.ouroboros_env', '.DS_Store',
})
SENSITIVE_NAMES = frozenset({
    '.env', '.env.local', '.env.production', '.env.staging', '.env.development',
    '.env.dev', '.env.test', 'credentials.json', 'service-account.json',
    'secrets.yaml', 'secrets.json', 'secrets.toml', 'secrets.ini',
    'aws-credentials.json', 'gcp-service-account.json', 'id_rsa', 'id_ed25519',
    'id_ecdsa', 'id_dsa', '.git-credentials', '.netrc', '.npmrc', '.pypirc',
})


def payload_files(skill_dir: Path, *, manifest_entry='', manifest_scripts=None) -> list[Path]:
    """The loader's sorted, confined regular-file walk in ordinary runtime mode."""
    root = skill_dir.resolve()
    files: set[Path] = set()
    pending = [root] if root.is_dir() else []
    candidates = []
    while pending:
        for path in pending.pop().glob('*'):
            if path.name in CACHE_NAMES:
                continue
            if path.is_dir() and not path.is_symlink():
                pending.append(path)
            else:
                candidates.append(path)
    for path in sorted(candidates):
        if not path.is_file():
            continue
        if path.name.lower() in SENSITIVE_NAMES or path.name.lower().endswith('.env'):
            raise ValueError(f'{path.relative_to(root)}: credential-shaped payload file')
        try:
            real = path.resolve()
        except (OSError, RuntimeError):
            continue
        if real.is_relative_to(root):
            files.add(path)

    def add_declared(value):
        relative = str(value or '').strip()
        if not relative or relative.startswith(('/', '~')) or '..' in PurePosixPath(relative).parts:
            return
        resolved = (root / relative).resolve()
        try:
            if resolved.is_relative_to(root) and stat.S_ISREG(resolved.stat().st_mode):
                files.add(resolved)
        except (FileNotFoundError, NotADirectoryError):
            pass

    add_declared(manifest_entry)
    for script in manifest_scripts or []:
        if isinstance(script, dict):
            name = str(script.get('name') or '').strip()
            if name:
                add_declared(name)
                if '/' not in name:
                    add_declared(f'scripts/{name}')
    return [p for p in sorted(files) if not (
        p.parent == root and p.name in PER_INSTALL_NOTE_FILENAMES)]


def file_facts(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def read_manifest(directory: Path):
    """Core's JSON / YAML frontmatter / body-only selection, without runtime imports."""
    for name in ('SKILL.md', 'skill.json'):
        path = directory / name
        if path.is_file():
            text = path.read_text(encoding='utf-8').lstrip('\ufeff')
            break
    else:
        raise ValueError(f'{directory.name}: missing skill manifest')
    try:
        if text.lstrip().startswith('{'):
            front = json.loads(text.lstrip())
        else:
            match = re.match(r'\A---\s*\n(.*?)\n---\s*\n?(.*)\Z', text, re.DOTALL)
            front = (yaml.safe_load(match[1]) or {}) if match else {}
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError(f'{directory.name}: invalid skill manifest') from exc
    if not isinstance(front, dict):
        raise ValueError(f'{directory.name}: manifest must be an object')
    return front


def empty(value):
    return value is None or value == '' or value == [] or value == {}


def manifest_declaration(front):
    for key in ('install_specs', 'install', 'dependencies'):
        if not empty(front.get(key)):
            return front[key]
    return None


def declaration_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)


def release_declaration(directory: Path):
    for name in ('.ouroboroshub.json', '.clawhub.json'):
        try:
            row = json.loads((directory / name).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        specs = row.get('install_specs') if isinstance(row, dict) else None
        if isinstance(specs, dict):
            for key in ('raw', 'auto'):
                if not empty(specs.get(key)):
                    return specs[key]
    return manifest_declaration(read_manifest(directory))


def payload_digest(skill_dir: Path, *, manifest_entry='', manifest_scripts=None) -> str:
    root = skill_dir.resolve()
    digest = hashlib.sha256(DIGEST_DOMAIN)
    # Sort POSIX strings, not Path components: a.txt precedes a/z.txt.
    pairs = {path.relative_to(root).as_posix(): file_facts(path)[0] for path in payload_files(
        root, manifest_entry=manifest_entry, manifest_scripts=manifest_scripts)}
    for relative in sorted(pairs):
        digest.update(relative.encode('utf-8'))
        digest.update(b'\0')
        digest.update(bytes.fromhex(pairs[relative]))
    declaration = release_declaration(root)
    if not empty(declaration):
        digest.update(b'\0install_specs\0')
        digest.update(hashlib.sha256(declaration_json(declaration).encode('utf-8')).hexdigest().encode('ascii'))
    return digest.hexdigest()
