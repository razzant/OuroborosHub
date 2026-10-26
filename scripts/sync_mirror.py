#!/usr/bin/env python3
"""Publish generated releases/catalog from main and explicitly update its mirror."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import shlex
import sys
import tempfile

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_index import build_catalog, json_bytes, refresh_legacy_entries, run_git

AUTHOR = ['-c', 'user.name=Ouroboros', '-c', 'user.email=311266734+ouroboros-agent@users.noreply.github.com']
# https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
# SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU
GITHUB_HOST_KEY = 'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n'


def git(root, args, token=None, *, ssh_command=None):
    env = dict(os.environ)
    if token:
        # The token never appears in the URL, argv, Git config on disk or output.
        auth = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                   GIT_CONFIG_VALUE_0=f'AUTHORIZATION: basic {auth}')
    if ssh_command:
        env['GIT_SSH_COMMAND'] = ssh_command
    return run_git(args, root, env=env)


def publish_catalog(root, repo_token, deploy_key):
    if not deploy_key:
        git(root, ['push', 'https://github.com/razzant/OuroborosHub.git', 'HEAD:refs/heads/main'], repo_token)
        return
    runner_temp = os.environ.get('RUNNER_TEMP')
    if not runner_temp:
        raise ValueError('RUNNER_TEMP must be configured for deploy-key publication')
    with tempfile.TemporaryDirectory(prefix='hub-catalog-ssh-', dir=runner_temp) as tmp:
        key, hosts = Path(tmp) / 'key', Path(tmp) / 'known_hosts'
        # Restrict permissions at creation, before any private bytes are written.
        with os.fdopen(os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as stream:
            stream.write(deploy_key.rstrip() + '\n')
        hosts.write_text(GITHUB_HOST_KEY, encoding='utf-8')
        command = shlex.join(['ssh', '-F', '/dev/null', '-i', str(key), '-o', 'IdentitiesOnly=yes',
                              '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                              '-o', f'UserKnownHostsFile={hosts}', '-o', 'GlobalKnownHostsFile=/dev/null'])
        git(root, ['push', 'git@github.com:razzant/OuroborosHub.git', 'HEAD:refs/heads/main'], ssh_command=command)


def sync(root, repo_token, mirror_token, deploy_key=None):
    if not (repo_token or deploy_key) or not mirror_token:
        raise ValueError('GITHUB_TOKEN or CATALOG_DEPLOY_KEY, and JOILAB_HUB_MIRROR_PAT must be configured')
    if git(root, ['branch', '--show-current']) != 'main':
        raise ValueError('catalog publication must run on the main branch')
    refreshed = refresh_legacy_entries(root)
    catalog = root / 'catalog.json'
    generated = json_bytes(build_catalog(root))
    publication_failed = False
    if refreshed or not catalog.is_file() or catalog.read_bytes() != generated:
        catalog.write_bytes(generated)
        git(root, ['add', '--', 'catalog.json', *refreshed])
        git(root, [*AUTHOR, 'commit', '-m', 'Refresh hosted releases and the legacy skill catalog'])
        try:
            publish_catalog(root, repo_token, deploy_key)
        except (OSError, ValueError, RuntimeError):
            publication_failed = True
    # GITHUB_TOKEN pushes do not trigger another push workflow. This is deliberately
    # in the same job, including when Owner Updates merged using GITHUB_TOKEN.
    # A refused catalog push must not stop main from reaching the mirror. Send
    # the original main commit on failure so retries can still fast-forward.
    mirror_ref = 'HEAD^' if publication_failed else 'HEAD'
    git(root, ['push', 'https://github.com/joi-lab/OuroborosHub.git', f'{mirror_ref}:refs/heads/main'], mirror_token)
    if publication_failed:
        raise ValueError('Catalog publication failed; mirrored main without the unpublished generated commit')


def main():
    try:
        sync(Path(__file__).resolve().parents[1], os.environ.get('GITHUB_TOKEN'), os.environ.get('MIRROR_PAT'),
             os.environ.get('CATALOG_DEPLOY_KEY'))
    except (OSError, ValueError, RuntimeError):
        print('Catalog/mirror publication failed; check configuration and rerun from current main', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
