"""Offline host-child integration fixture, never an installed entrypoint."""

import json
import os
from pathlib import Path
import shutil
import sys


def main():
    repo, source_skill, temp_root = map(Path, sys.argv[1:])
    drive = temp_root / "drive"
    os.environ.update(OUROBOROS_DATA_DIR=str(drive), OUROBOROS_RUNTIME_MODE="advanced",
                      OUROBOROS_SETTINGS_PATH=str(drive / "settings.json"))
    sys.path.insert(0, str(repo))
    from ouroboros import extension_loader
    from ouroboros.skill_loader import (
        SkillReviewState, find_skill, save_enabled, save_review_state, save_skill_grants,
        requested_skill_permissions, skill_state_dir,
    )
    from ouroboros.tools.registry import ToolContext
    from supervisor import queue, state
    from tests._extension_loader_shared import _mark_isolated_deps_installed

    drive.mkdir(exist_ok=True)
    (drive / "settings.json").write_text(json.dumps({}), encoding="utf-8")
    state.init(drive)
    queue.init(drive)
    skills = temp_root / "skills"
    skill = skills / "email-presence"
    shutil.copytree(source_skill, skill, ignore=shutil.ignore_patterns("__pycache__", ".ouroboros_env", ".review-evidence", ".git"))
    shutil.move(skill / "plugin.py", skill / "original_plugin.py")
    # Replace only network-facing client methods. Registration, store, MIME source
    # staging, context injection, JSON return and subprocess transport remain real.
    (skill / "plugin.py").write_text('''
import base64
import json
from contextlib import nullcontext
from pathlib import Path
from . import original_plugin
from .lib.accounts import AccountStore, bearer_mark
from .lib.gmail import GmailClient
from .lib.gmail_host import GmailSignIn

HEADERLESS = b"Subject: no identity header\\r\\n\\r\\nbody\\r\\n"

def register(api):
    directory = api.get_state_dir()
    accounts = AccountStore(directory)
    # A whole saved sign-in, as one is stored: its client and refresh token beside the bearer, so Mail judges it usable.
    accounts.save({'id': 'google:fixture', 'provider': 'gmail', 'address': 'owner@example.invalid',
                   'senders': ['owner@example.invalid'], 'scopes': ['https://mail.google.com/'],
                   'oauth': {'client_id': 'fixture-client'},
                   # the permissions Google named for this fixture's bearer
                   'tokens': {'refresh_token': 'fixture-refresh', 'access_token': 'fixture-only',
                              'expires_at': 4102444800, 'scope': 'https://mail.google.com/',
                              'scope_of': bearer_mark('fixture-only')}})
    GmailSignIn.environment = classmethod(lambda cls: cls('', ''))  # no Host sign-in service in this offline child
    def request(self, method, path, **options):
        if method == 'GET':
            if path == '/labels':
                return {'labels': []}
            message = {'id': '7b', 'labelIds': ['DRAFT'], 'payload': {'headers': [
                {'name': 'Message-ID', 'value': '<own@example.invalid>'},
                {'name': 'References', 'value': '<held@example.invalid>'}]}}
            if path in ('/messages/7b', '/messages/7c') and (options.get('params') or {}).get('format') == 'raw':
                root = '<held@example.invalid>' if path.endswith('7b') else '<thread-root@example.invalid>'
                raw = ('Message-ID: <own@example.invalid>\\r\\nReferences: ' + root + '\\r\\n\\r\\nbody').encode()
                return {'id': path.rsplit('/', 1)[1], 'raw': base64.urlsafe_b64encode(raw).decode()}
            if path == '/drafts/one':
                return {'id': 'one', 'message': message}
            if path == '/messages/7c':  # replies in a thread whose Place stores conversation and thread
                return {'id': '7c', 'labelIds': ['INBOX'], 'payload': {'headers': [
                    {'name': 'Message-ID', 'value': '<own-7c@example.invalid>'},
                    {'name': 'References', 'value': '<thread-root@example.invalid>'}]}}
            if path == '/messages/7d':  # no identity header: intake keys it by the raw source's SHA-256
                if (options.get('params') or {}).get('format') == 'raw':
                    return {'id': '7d', 'labelIds': ['INBOX'],
                            'raw': base64.urlsafe_b64encode(HEADERLESS).decode().rstrip('=')}
                return {'id': '7d', 'labelIds': ['INBOX'], 'payload': {'headers': [
                    {'name': 'Subject', 'value': 'no identity header'}]}}
            assert path == '/messages/7b', path
            return message
        with (Path(directory) / 'provider-writes.jsonl').open('a') as stream:
            stream.write(json.dumps([method, path, options.get('body')]) + "\\n")
        return {}
    GmailClient._request = request
    # The child never contacts a Host service; queued binding capture reports itself unsupported.
    original_plugin.MailService._presence_host = lambda self: nullcontext(None)
    from .tests.child_identity_cases import install
    install(original_plugin, api, Path(__file__).parent)
    original_plugin.register(api)
''', encoding="utf-8")
    loaded = find_skill(drive, "email-presence", repo_path=str(skills))
    assert loaded is not None
    # These are throwaway fixtures for loading, not installation attestations.
    save_enabled(drive, loaded.name, True)
    save_review_state(drive, loaded.name, SkillReviewState(status="pass", content_hash=loaded.content_hash))
    permissions = requested_skill_permissions(list(loaded.manifest.permissions))
    keys = list(loaded.manifest.env_from_settings)
    save_skill_grants(drive, loaded.name, keys, content_hash=loaded.content_hash,
                      requested_keys=keys, granted_permissions=permissions, requested_permissions=permissions)
    _mark_isolated_deps_installed(drive, loaded)
    loaded = find_skill(drive, loaded.name, repo_path=str(skills))
    error = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive)
    assert error is None, error
    try:
        from ouroboros import places, tool_effects, place_guard
        from ouroboros.tools.registry import ToolRegistry
        from tests._safety_fixtures import supervisor
        from pytest import MonkeyPatch
        actual_effect, actual_facts = tool_effects.effect_of, place_guard.call_place_facts
        patch = MonkeyPatch()
        harness = supervisor.__wrapped__(patch, drive)
        h = next(harness)
        patch.setattr(tool_effects, 'effect_of', actual_effect)
        patch.setattr(place_guard, 'call_place_facts', actual_facts)
        # Only the model transport is simulated with SAFE verdicts. Host dispatch, Safety and guards stay real.
        registry = ToolRegistry(repo_dir=repo, drive_root=drive)
        ctx = ToolContext(repo_dir=repo, drive_root=drive, task_id='fixture', task_metadata={'work_kind': 'owner'})
        ctx._current_llm_call_meta = {'llm_call_id': 'fixture-round'}
        registry.set_context(ctx)
        places.connect_place(drive, connector='email-presence', provider='email', account_id='google:fixture', state='active')
        places.connect_place(drive, connector='email-presence', provider='email', account_id='google:fixture',
                             conversation_id='<held@example.invalid>', state='paused')
        places.connect_place(drive, connector='email-presence', provider='email', account_id='google:fixture',
                             conversation_id='held@example.invalid', state='silent')
        writes = skill_state_dir(drive, loaded.name) / 'provider-writes.jsonl'
        def invoke(name, args):
            surface = extension_loader.extension_surface_name(loaded.name, name)
            assert extension_loader.get_tool(surface)['out_of_process']
            result = registry.execute_result(surface, args)
            print(name, json.dumps(args), repr(result), flush=True)
            return result.text
        for selection in ({'action': 'delete'}, {'action': 'labels', 'add_labels': ['STARRED']},
                          {'action': 'delete_draft', 'draft_id': 'one'}):
            args = {'account_id': 'google:fixture', **selection}
            if selection['action'] != 'delete_draft':
                args.update(uid=123, uidvalidity=1)
            prepared = json.loads(invoke('email_mailbox', args))
            assert prepared['status'] == 'needs_arguments' and not writes.exists()
            refusal = invoke('email_mailbox', prepared['retry_arguments'])
            assert 'Paused' in refusal and not writes.exists()
        for explicit in (True, False):
            args = {'resource': 'sendAs', 'action': 'update', 'identifier': 'owner@example.invalid',
                    'values': [['replyToAddress', 'held@example.invalid']]}
            if explicit:
                args['account_id'] = 'google:fixture'
            refusal = invoke('email_settings', args)
            assert ('values must be an object' if explicit else 'cannot be attributed') in refusal and not writes.exists()
        # Thread Places (conversation and thread, as intake creates them) hold message mutations in the child too.
        from dataclasses import replace
        import hashlib
        headerless = '<sha256-' + hashlib.sha256(b"Subject: no identity header\r\n\r\nbody\r\n").hexdigest() + '@ouroboros.local>'
        for root in ('<thread-root@example.invalid>', headerless):
            held = places.connect_place(drive, connector='email-presence', provider='email', account_id='google:fixture',
                                        conversation_id=root, state='paused')
            places._write(drive, held.place_id, lambda row, root=root: replace(row, thread_id=root))
        for uid, root, selection in ((124, '<thread-root@example.invalid>', {'action': 'flags', 'flags': ['\\Flagged']}),
                                     (125, headerless, {'action': 'labels', 'add_labels': ['STARRED']})):
            prepared = json.loads(invoke('email_mailbox', {'account_id': 'google:fixture', 'uid': uid, 'uidvalidity': 1,
                                                           **selection}))
            retry = prepared['retry_arguments']
            assert retry['message_conversation_id'] == retry['message_thread_id'] == root and not writes.exists()
            assert 'Paused' in invoke('email_mailbox', retry) and not writes.exists()
        # References alone cannot claim an Active conversation; nothing is queued.
        places.connect_place(drive, connector='email-presence', provider='email', account_id='google:fixture',
                             conversation_id='<claimed@example.invalid>', state='active')
        claim = invoke('email_send', {'account_id': 'google:fixture', 'to': 'reader@example.invalid', 'subject': 'S',
                                      'body': 'B', 'references': ['<claimed@example.invalid>'], 'request_id': 'claim',
                                      'conversation_id': '<claimed@example.invalid>', 'thread_id': '<claimed@example.invalid>'})
        assert 'references continue a conversation' in claim
        assert json.loads(invoke('email_receipt', {'account_id': 'google:fixture', 'request_id': 'claim'})) is None
        # Content: a body-only owner card does not release a send carrying other HTML; Safety reads the exact call.
        ctx._owner_directives = [{'source': 'owner_quiz_answer', 'quiz_id': 'q-body-only',
                                  'reply_to': {'address': 'quiz q-body-only', 'text': 'Send?\nThank you.\nreader@example.invalid'}}]
        readings = len(h.calls)
        sent = invoke('email_send', {'account_id': 'google:fixture', 'to': 'reader@example.invalid', 'subject': 'Thanks',
                                     'body': 'Thank you.', 'html_body': '<p>Entirely different, unapproved words.</p>',
                                     'request_id': 'content'})
        assert len(h.calls) == readings + 1 and 'q-body-only' not in sent
        queued = json.loads(invoke('email_receipt', {'account_id': 'google:fixture', 'request_id': 'content'}))
        assert queued['request_id'] == 'content' and not writes.exists()  # queued (positive control), never provider-sent here
        ctx._owner_directives = []
        # A valid sole-account call still prepares its canonical account. Malformed values on that retry fail too.
        places.connect_place(drive, connector='email-presence', provider='email', account_id='google:fixture',
                             conversation_id='allowed@example.invalid', state='active')
        args = {'resource': 'sendAs', 'action': 'update', 'identifier': 'owner@example.invalid',
                'values': {'replyToAddress': 'allowed@example.invalid'}}
        retry = json.loads(invoke('email_settings', args))['retry_arguments']
        assert retry['account_id'] == 'google:fixture' and not writes.exists()
        malformed = {**retry, 'values': [['replyToAddress', 'held@example.invalid']]}
        assert 'values must be an object' in invoke('email_settings', malformed) and not writes.exists()
        hidden_filter = {**retry, 'resource': 'filters', 'action': 'create', 'identifier': '',
                         'values': {'action': [['forward', 'held@example.invalid']]}}
        assert 'must be an object' in invoke('email_settings', hidden_filter) and not writes.exists()
        # Positive control proves the child can reach the recorder with the exact approved object.
        invoke('email_settings', retry)
        assert json.loads(writes.read_text()) == ['PUT', '/settings/sendAs/owner%40example.invalid', {'replyToAddress': 'allowed@example.invalid'}]
        sys.path[:0] = [str(source_skill), str(source_skill / 'tests')]
        from child_identity_cases import exercise
        exercise(invoke, places, drive, skill_state_dir(drive, loaded.name) / 'identity-effects.jsonl')
        next(harness, None)
        patch.undo()
        print('verified real registry and child authority; zero negative provider writes')
    finally:
        extension_loader.unload_extension(loaded.name)


if __name__ == '__main__':
    main()
