"""Calendar continuity through the Host Service's account listing, with no network."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SKILL = str(Path(__file__).resolve().parents[1])
if SKILL not in sys.path:
    sys.path.insert(0, SKILL)

import accounts  # noqa: E402
import google_host  # noqa: E402
import providers_google as gp  # noqa: E402
import tools  # noqa: E402
from host_sign_in import HostError, HostSignIn  # noqa: E402
from management import cal_manage  # noqa: E402
from model import calendar_id, now_utc  # noqa: E402
from ops import ProviderError, retry_due_intents, run_leased_intent  # noqa: E402
from providers import Providers  # noqa: E402
from store import Store  # noqa: E402

LOGIN = 'owner@example.test'
ACCOUNT = f'google:{LOGIN}'
SUBJECT = 'owner-subject'
PROVISIONAL = 'a-00000000000000000001'
VERIFIED = 'a-00000000000000000002'
CONFIRMED = {'account': VERIFIED, 'label': LOGIN, 'subject': SUBJECT, 'connection_id': 'conn-card-1'}   # the mark of a confirmed connection
TOKEN = {'access_token': 'fixture-access-token', 'connection_id': 'conn-card-1'}   # what the host answers for that connection


class MemoryHost(HostSignIn):
    def __init__(self):
        self._state = None
        # An unconfirmed import never has a connection_id: only a confirmed connection does.
        self.rows = [dict(key=PROVISIONAL, label=LOGIN, subject='', verified=False, via='handover',
                          state='sign_in_required', reason='refresh_rejected')]
        self.token_bodies, self.recoveries, self.started = [], [], {}
        self.verified_imports = False
        self.lose_answer = False
        self.recoverable = True   # whether the host can still report how an exchange it began ended
        self.handover_generations = []
        self.handover_connection_id = 'conn-h1'

    @property
    def token_accounts(self):
        return [body['account'] for body in self.token_bodies]

    def recovered(self, grant):
        # The answer to a recover request: the receipt of an exchange the host began, never a new exchange or exchange='none'.
        began = self.started.get(grant['legacy_key'])
        if not self.recoverable or began is None or began['generation'] != grant.get('legacy_generation') or not began.get('receipt'):
            return {'legacy_key': grant['legacy_key'], 'result': 'refused', 'reason': 'handover_unconfirmed', 'exchange': 'unknown'}
        return dict(began['receipt'])

    def _call(self, method, path, body=None, timeout=40):
        if (method, path) == ('GET', '/identity'):
            return {'ok': True, 'sign_in_version': 1}
        if (method, path) == ('GET', '/sign-in/accounts'):
            return {'ok': True, 'services': [{'id': 'google', 'accounts': list(self.rows)}]}
        if (method, path) == ('POST', '/sign-in/handover'):
            if not body['grants']:
                return {'ok': True, 'grants': []}
            [grant] = body['grants']
            if body.get('recover'):
                self.recoveries.append(body)
                return {'ok': True, 'grants': [self.recovered(grant)]}
            began = self.started.get(grant['legacy_key'])
            if began and not began.get('receipt') and began['generation'] != grant.get('legacy_generation'):
                # As b59c: an exchange of this sign-in that left and is unresolved is never displaced; another grant is
                # refused before its own exchange is claimed, and the answer can only call that exchange unknown.
                return {'ok': True, 'grants': [{'legacy_key': grant['legacy_key'], 'result': 'refused',
                                                'reason': 'handover_unconfirmed', 'exchange': 'unknown'}]}
            if self.verified_imports:
                generation = grant['legacy_generation']
                self.handover_generations.append(generation)
                self.rows = [dict(verified_row(via='handover', connection_id=self.handover_connection_id),
                                  legacy_generation=generation)]
                receipt = {'legacy_key': grant['legacy_key'], 'result': 'imported', 'legacy_generation': generation,
                           'via': 'handover', 'state': 'signed_in', 'connection_id': self.handover_connection_id,
                           'account': {'key': VERIFIED, 'label': LOGIN}}
                self.started[grant['legacy_key']] = dict(generation=generation, receipt=receipt)
                if self.lose_answer:
                    raise OSError('The reply to the saved handover was lost')
                return {'ok': True, 'grants': [receipt]}
            return {'ok': True, 'grants': [{'legacy_key': grant['legacy_key'], 'result': 'imported', 'via': 'handover',
                    'state': 'sign_in_required', 'account': {'key': PROVISIONAL, 'label': LOGIN}}]}
        if (method, path) == ('POST', '/sign-in/token'):
            self.token_bodies.append(dict(body))
            row = next((row for row in self.rows if row['key'] == body['account']), None)
            if row is None:
                raise HostError(404, {'ok': False, 'reason': 'unknown_account'})
            if body.get('connection_id') and body['connection_id'] != row.get('connection_id'):
                raise HostError(409, {'ok': False, 'code': 'SIGN_IN_REQUIRED', 'reason': 'connection_changed'})
            if row['state'] != 'signed_in':
                raise HostError(409, {'ok': False, 'code': 'SIGN_IN_REQUIRED', 'reason': 'refresh_rejected'})
            return {'ok': True, 'access_token': f'fixture-access-token-{len(self.token_bodies)}',
                    **({'connection_id': row['connection_id']} if row['verified'] and row.get('connection_id') else {})}
        raise AssertionError(f'Unexpected Host Service call: {method} {path}')


class HostSignInContinuityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store, self.host = Store(tmp.name), MemoryHost()
        self.store.upsert_account(dict(id=ACCOUNT, provider='google', alias='Owner alias', login=LOGIN, status='auth_failed'))
        self.calendar_ids = []
        for name, role in [('personal', dict(role_visible=True, role_busy=False, is_default=True)),
                           ('work', dict(role_visible=False, role_publish=True, publish_mode='full'))]:
            cid = calendar_id('google', LOGIN, name)
            self.store.upsert_calendar(dict(id=cid, account_id=ACCOUNT, provider='google', external_id=name, **role))
            self.calendar_ids.append(cid)
        event = self.store.insert_event(dict(calendar_id=self.calendar_ids[0], title='Planning', sync_state='pending',
                                            start_utc='2026-12-01T10:00:00+00:00', end_utc='2026-12-01T11:00:00+00:00'))
        self.intent = self.store.add_intent('create', ACCOUNT, self.calendar_ids[0], event['id'], {})
        vault = {LOGIN: dict(provider='google', email=LOGIN, subject=SUBJECT, scope=' '.join(gp.SCOPES),
                             client_id='fixture-oauth-client', refresh_token='fixture-old-refresh-token')}
        loader = patch.object(google_host, 'load_tokens', return_value=vault)
        loader.start()
        self.addCleanup(loader.stop)
        handed, facts = google_host.worker_pass(self.store, {}, tmp.name, self.host, None, lambda *a, **kw: None)
        self.assertEqual(handed['results'], {'imported': 1})
        self.assertEqual(facts['follow'], {'status': 'ok', 'added': 0, 'withdrawn': 0})
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['account'], PROVISIONAL)
        self.assertEqual(self.store.get_intent(self.intent['id'])['state'], 'pending')
        self.account_before = self.store.get_account(ACCOUNT)
        self.calendars_before = self.store.list_calendars(account_id=ACCOUNT, include_deleted=True)

    def follow(self):
        return google_host.follow(self.store, {}, self.store.state_dir, self.host)

    def assert_local_rows_unchanged(self):
        self.assertEqual(self.store.get_account(ACCOUNT), self.account_before)
        self.assertEqual(self.store.list_calendars(account_id=ACCOUNT, include_deleted=True), self.calendars_before)
        self.assertEqual(self.store.get_intent(self.intent['id']), self.intent)

    def assert_queue_dropped(self):
        # Another consent keeps the account, its calendars and their roles, not the right to the queue before it.
        self.assertEqual(self.store.get_account(ACCOUNT), self.account_before)
        self.assertEqual(self.store.list_calendars(account_id=ACCOUNT, include_deleted=True), self.calendars_before)
        settled = self.store.get_intent(self.intent['id'])
        self.assertEqual((settled['state'], json.loads(settled['result_json'])['note']),
                         ('resolved', google_host.UNCONFIRMED_CONTINUITY))

    def assert_same_account_after_sign_in(self, *, connection_id='conn-card-1'):
        # A fresh label is deliberately different: only the provider's subject gives continuity of the account,
        # and a card that was not the import is not the connection the queue was waiting for.
        self.host.rows.append(verified_row(label='renamed@example.test', connection_id=connection_id))
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
        settled = follow_state(self.store, self.intent['id'])
        later = self.store.add_intent('create', ACCOUNT, self.calendar_ids[0], self.intent['event_id'], {})
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
        self.assertEqual(follow_state(self.store, self.intent['id']), settled)   # a repeat changes nothing
        self.assertEqual(self.store.get_intent(later['id'])['state'], 'pending')   # and spares a newer command
        self.assertEqual([row['id'] for row in self.store.list_accounts('google')], [ACCOUNT])
        self.assertEqual({row['id'] for row in self.calendars_before}, set(self.calendar_ids))
        self.assert_queue_dropped()
        marks = self.store.get_setting(google_host.MOVED)
        self.assertEqual(set(marks), {LOGIN})
        self.assertEqual((marks[LOGIN]['account'], marks[LOGIN]['connection_id']), (VERIFIED, connection_id))
        self.assertTrue(self.store.get_setting('sync_requested_at'))
        providers = SimpleNamespace(state_dir=self.store.state_dir, google_host={'host': self.host, 'moved': marks})
        adapter = google_host.adapter_for(providers, LOGIN)
        self.assertEqual(adapter.account_id, ACCOUNT)
        adapter._refresh()
        self.assertEqual((self.host.token_accounts, [body.get('connection_id') for body in self.host.token_bodies]),
                         ([VERIFIED], [None]))   # ordinary read refresh omits connection_id; writes check it

    def test_reauthentication_keeps_local_rows_but_not_the_queue_when_expired_handover_is_still_listed(self):
        self.assert_same_account_after_sign_in()

    def test_reauthentication_also_keeps_local_rows_when_only_verified_key_remains(self):
        # The expired import had no connection_id, so the verified card that replaces it is another consent:
        # key and roles stay, the queue before it is withdrawn, and a repeat adds nothing.
        self.host.rows = [verified_row(label='renamed@example.test', connection_id='conn-card-1')]
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
        settled = follow_state(self.store, self.intent['id'])
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
        self.assertEqual(follow_state(self.store, self.intent['id']), settled)
        self.assertEqual([row['id'] for row in self.store.list_accounts('google')], [ACCOUNT])
        self.assert_queue_dropped()
        marks = self.store.get_setting(google_host.MOVED)
        self.assertEqual((set(marks), marks[LOGIN]['account'], marks[LOGIN]['connection_id']), ({LOGIN}, VERIFIED, 'conn-card-1'))

    def test_changed_provisional_key_without_continuity_proof_keeps_account_and_drops_old_queue(self):
        in_flight = self.store.insert_event(dict(calendar_id=self.calendar_ids[0], title='Sent?', sync_state='conflict',
                                                 start_utc='2026-12-02T10:00:00+00:00', end_utc='2026-12-02T11:00:00+00:00'))
        uncertain = self.store.add_intent('create', ACCOUNT, self.calendar_ids[0], in_flight['id'], {})
        self.store.settle_intent(uncertain['id'], 'uncertain', {'error': 'lost after send', 'kind': 'network'})
        # The import had no connection_id and no generation to carry: the card's connection is another one.
        self.host.rows = [verified_row(label='renamed@example.test', connection_id='conn-card-1')]
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
        settled = follow_state(self.store, self.intent['id'])
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
        self.assertEqual(follow_state(self.store, self.intent['id']), settled)
        self.assertEqual(self.store.get_account(ACCOUNT), self.account_before)
        self.assertEqual(self.store.list_calendars(account_id=ACCOUNT, include_deleted=True), self.calendars_before)
        self.assertIsNotNone(self.store.get_event(self.intent['event_id']))
        dropped = self.store.get_intent(self.intent['id'])
        note = json.loads(dropped['result_json'])['note']
        self.assertEqual((dropped['state'], note), ('resolved', google_host.UNCONFIRMED_CONTINUITY))
        self.assertNotIn('disconnected', note.lower())
        self.assertEqual(self.store.get_intent(uncertain['id'])['state'], 'uncertain')   # an owed outcome stays for comparison
        self.assertFalse(self.store.get_setting(google_host.WITHDRAWN))
        marks = self.store.get_setting(google_host.MOVED)
        self.assertEqual((set(marks), marks[LOGIN]['account'], marks[LOGIN]['connection_id']), ({LOGIN}, VERIFIED, 'conn-card-1'))
        providers = SimpleNamespace(state_dir=self.store.state_dir, google_host={'host': self.host, 'moved': marks})
        adapter = google_host.adapter_for(providers, LOGIN)
        self.assertEqual(adapter.account_id, ACCOUNT)
        adapter._refresh()
        self.assertEqual((self.host.token_accounts, [body.get('connection_id') for body in self.host.token_bodies]),
                         ([VERIFIED], [None]))

    def test_personal_to_it_client_change_on_same_key_keeps_queue_with_matching_connection_id(self):
        # Re-authorizing Personal->IT on the same host key keeps the same connection_id and carries commands queued on it.
        self.host.rows = [verified_row(connection_id='conn-card-1')]
        self.follow()
        queued = self.store.add_intent('create', ACCOUNT, self.calendar_ids[0], self.intent['event_id'], {})
        self.host.rows = [dict(verified_row(label='renamed@example.test', connection_id='conn-card-1'),
                               client_id='it-oauth-client')]
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
        settled = follow_state(self.store, queued['id'])
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
        self.assertEqual(follow_state(self.store, queued['id']), settled)
        mark = self.store.get_setting(google_host.MOVED)[LOGIN]
        self.assertEqual((mark['account'], mark['connection_id'], mark['label']), (VERIFIED, 'conn-card-1', 'renamed@example.test'))
        self.assertEqual(self.store.get_intent(queued['id'])['state'], 'pending')
        self.assertEqual(self.store.list_calendars(account_id=ACCOUNT, include_deleted=True), self.calendars_before)

    def rekey_under_generation(self, handed_generation):
        marks = self.store.get_setting(google_host.MOVED)
        marks[LOGIN]['legacy_generation'] = 'g1'
        self.store.set_setting(google_host.MOVED, marks)
        self.host.rows = [dict(verified_row(label='renamed@example.test', via='handover', connection_id='conn-h2'),
                               legacy_generation=handed_generation)]
        for _ in range(2):
            self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['account'], VERIFIED)

    def test_changed_key_keeps_queue_with_confirmed_same_handover_generation(self):
        self.rekey_under_generation('g1')
        self.assert_local_rows_unchanged()
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['connection_id'], 'conn-h2')

    def test_changed_key_with_another_handover_generation_drops_old_queue(self):
        self.rekey_under_generation('g-other')
        self.assert_queue_dropped()

    def test_another_or_unconfirmed_sign_in_does_not_replace_the_owner_account(self):
        # A label is presentation data; neither a different verified sub nor an unknown sub matches it.
        self.host.rows.extend([
            dict(key=VERIFIED, label=LOGIN, subject='different-subject', verified=True, state='signed_in', via='card', connection_id='conn-other'),
            dict(key='a-00000000000000000003', label=LOGIN, subject='', verified=False,
                 state='sign_in_required', reason='refresh_rejected', via='handover'),
        ])
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 1, 'withdrawn': 0})
        marks = self.store.get_setting(google_host.MOVED)
        self.assertEqual(marks[LOGIN]['account'], PROVISIONAL)
        self.assertEqual({mark['account'] for key, mark in marks.items() if key != LOGIN}, {VERIFIED})
        self.assertNotIn('a-00000000000000000003', {mark['account'] for mark in marks.values()})
        self.assertEqual({row['id'] for row in self.store.list_accounts('google')},
                         {ACCOUNT, 'google:oidc:different-subject'})
        self.assertEqual({sub: rec['key'] for sub, rec in (self.store.get_setting(google_host.IDENTITIES) or {}).items()},
                         {SUBJECT: LOGIN, 'different-subject': 'oidc:different-subject'})
        self.assert_local_rows_unchanged()


class FakeVault:
    """In-memory credential custody: the same dict every reader sees, a forget that can be interrupted."""
    def __init__(self, entries):
        self.entries, self.fail, self.keep = entries, False, False

    def load(self, state_dir, passphrase):
        if self.fail:
            raise ProviderError('auth', 'fixture vault unreadable')
        return self.entries

    def forget(self, state_dir, passphrase, key, *, expected_generation=None):
        if self.keep or self.entries.get(key, {}).get('generation') != expected_generation:
            return False   # an interrupted removal leaves the copy behind
        return self.entries.pop(key, None) is not None


class ContinuityHost(HostSignIn):
    def __init__(self):
        self._state = None
        self.rows = []
        self.complete = True
        self.handover_reply = None
        self.on_handover = None
        self.on_accounts = None
        self._conn_seq = 0
        self._key_seq = 2
        self._keys = {('sub', SUBJECT): VERIFIED}

    def _host_key(self, grant):
        subject = grant.get('subject')
        token = ('sub', subject) if subject else ('legacy', grant['legacy_key'])
        if token not in self._keys:
            if not subject and PROVISIONAL not in self._keys.values():
                self._keys[token] = PROVISIONAL
            else:
                self._key_seq += 1
                self._keys[token] = f'a-{self._key_seq:020x}'
        return self._keys[token]

    def _call(self, method, path, body=None, timeout=40):
        if (method, path) == ('GET', '/identity'):
            return {'ok': True, 'sign_in_version': 1}
        if (method, path) == ('GET', '/sign-in/accounts'):
            snapshot = list(self.rows)
            if self.on_accounts:
                self.on_accounts()
            return {'ok': True, 'complete': self.complete, 'services': [{'id': 'google', 'accounts': snapshot}]}
        if (method, path) == ('POST', '/sign-in/handover'):
            if self.on_handover:
                self.on_handover(body)
            if self.handover_reply is not None:
                return self.handover_reply
            assert not body.get('recover'), 'a recover request never starts an exchange'
            items = []
            for g in body['grants']:
                key = self._host_key(g)
                existing = next((row for row in self.rows if row.get('key') == key), None)
                if existing is None:
                    self._conn_seq += 1
                    conn_id = f'conn-{self._conn_seq}' if g.get('subject') else ''   # an unconfirmed import has none
                    subject = g.get('subject') or ''
                    row = (verified_row(key=key, subject=subject, label=g['label'], via='handover', connection_id=conn_id)
                           if subject else dict(key=key, label=g['label'], subject='', verified=False,
                                                state='signed_in', via='handover'))
                    if g.get('legacy_generation'):
                        row['legacy_generation'] = g['legacy_generation']
                    self.rows.append(row)
                else:
                    row = existing
                    conn_id = row.get('connection_id')
                items.append({
                    'legacy_key': g['legacy_key'], 'result': 'imported', 'state': row['state'],
                    **({'via': row['via']} if row.get('via') is not None else {}),
                    **({'connection_id': conn_id} if conn_id else {}),
                    **({'legacy_generation': g['legacy_generation']} if g.get('legacy_generation') else {}),
                    'account': {'key': key, 'label': row['label']},
                })
            return {'ok': True, 'grants': items}
        raise AssertionError(f'Unexpected Host Service call: {method} {path}')


def verified_row(key=VERIFIED, subject=SUBJECT, label=LOGIN, via='card', connection_id='conn-1'):
    return dict(key=key, label=label, subject=subject, verified=True, state='signed_in',
                **({'via': via} if via is not None else {}),
                **({'connection_id': connection_id} if connection_id else {}))


def follow_state(store, intent_id):
    # Everything a repeated follow could still change: bindings, reservations, withdrawals and the command.
    return [store.get_setting(name) for name in (google_host.MOVED, google_host.REFUSED, google_host.WITHDRAWN,
                                                 google_host.IDENTITIES)] + [store.get_intent(intent_id)]


def google_accepts(request, timeout=None):
    # Google accepts a write and answers with what it was sent.
    reply = MagicMock()
    reply.__enter__.return_value, reply.status, reply.headers = reply, 200, {}
    reply.read.return_value = json.dumps({**json.loads(request.data or b'{}'), 'etag': 'v1'}).encode()
    return reply


class WithdrawalContinuityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store, self.host = Store(tmp.name), ContinuityHost()
        self.vault = FakeVault({LOGIN: dict(provider='google', email=LOGIN, subject=SUBJECT, client_id='fixture-client',
                                            refresh_token='fixture-refresh', scope=' '.join(gp.SCOPES), generation='g1')})
        for target in ('google_host.load_tokens', 'providers_google.load_tokens', 'credentials.load_tokens'):
            patcher = patch(target, side_effect=self.vault.load)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch('providers_google.forget_account', side_effect=self.vault.forget)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store.upsert_account(dict(id=ACCOUNT, provider='google', alias='Owner', login=LOGIN, status='ok'))
        self.cid = calendar_id('google', LOGIN, 'personal')
        self.store.upsert_calendar(dict(id=self.cid, account_id=ACCOUNT, provider='google', external_id='personal',
                                        role_visible=True, role_busy=False, is_default=True))
        event = self.store.insert_event(dict(calendar_id=self.cid, title='Draft', sync_state='pending',
                                             start_utc='2026-12-01T10:00:00+00:00', end_utc='2026-12-01T11:00:00+00:00'))
        self.intent = self.store.add_intent('create', ACCOUNT, self.cid, event['id'], {})

    def follow(self):
        return google_host.follow(self.store, {}, self.store.state_dir, self.host)

    def card_disconnect(self):
        self.host.rows = []
        return self.follow()

    def confirmed_host(self):
        # A host that holds the owner's confirmed connection, and the mark Calendar kept for it.
        host = MemoryHost()
        host.rows = [verified_row(connection_id=CONFIRMED['connection_id'])]
        self.store.set_setting(google_host.MOVED, {LOGIN: dict(CONFIRMED)})
        return host

    def queue_event(self, title):
        event = self.store.insert_event(dict(calendar_id=self.cid, title=title, sync_state='pending',
                                             start_utc='2026-12-03T10:00:00+00:00', end_utc='2026-12-03T11:00:00+00:00'))
        return self.store.add_intent('create', ACCOUNT, self.cid, event['id'], {})

    def test_first_exchange_waits_and_reads_back_the_saved_generation_without_repeating_it(self):
        host = MemoryHost()
        host.verified_imports = host.lose_answer = True
        host.rows = []
        before = (self.store.get_account(ACCOUNT), self.store.get_calendar(self.cid), self.store.get_intent(self.intent['id']))
        with patch.object(google_host, 'HostSignIn', return_value=host):
            providers = Providers({}, self.store.state_dir)
            legacy = providers.adapter_for(ACCOUNT)
            self.assertIsInstance(legacy, gp.GoogleAdapter)
            exchange = host.handover
            def during_transfer(*args):
                self.assertIsNone(providers.adapter_for(ACCOUNT))
                with patch.object(gp, '_token_request') as refresh, patch.object(gp.urllib.request, 'urlopen') as send:
                    with self.assertRaises(ProviderError) as held:
                        legacy.list_calendars()
                    self.assertTrue(held.exception.unsent)
                    refresh.assert_not_called()
                    send.assert_not_called()
                return exchange(*args)
            with patch.object(host, 'handover', side_effect=during_transfer):
                self.assertEqual(google_host.handover(self.store, {}, self.store.state_dir, host)['status'], 'unreachable')
            self.assertIsNone(Providers({}, self.store.state_dir).adapter_for(ACCOUNT))
            old_rows, reserved = host.rows, follow_state(self.store, self.intent['id'])
            host.rows = [dict(verified_row(), via='handover', legacy_generation='earlier-generation')]   # a foreign consent
            self.assertEqual(google_host.follow(self.store, {}, self.store.state_dir, host)['status'], 'ok')
            self.assertEqual(follow_state(self.store, self.intent['id']), reserved)   # one foreign read settles nothing
            self.assertIsNone(Providers({}, self.store.state_dir).adapter_for(ACCOUNT))
            host.rows = old_rows
            google_host.worker_pass(self.store, {}, self.store.state_dir, host, None, lambda *a, **kw: None)
            self.assertIsInstance(providers.adapter_for(ACCOUNT), google_host.HostGoogleAdapter)
        self.assertEqual(host.handover_generations, ['g1'])
        self.assertEqual([body['recover'] for body in host.recoveries], [True])   # its receipt was asked for, never exchanged again
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['connection_id'], 'conn-h1')
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['legacy_generation'], 'g1')
        self.assertEqual((self.store.get_account(ACCOUNT), self.store.get_calendar(self.cid),
                          self.store.get_intent(self.intent['id'])), before)

    def test_confirmed_gateway_refusal_leaves_the_legacy_grant_eligible(self):
        with patch.object(google_host, 'HostSignIn', return_value=self.host):
            providers = Providers({}, self.store.state_dir)
            with patch.object(self.host, 'handover', side_effect=HostError(429, {'ok': False, 'error': 'rate limit exceeded', 'handover_started': False})):
                refused = google_host.handover(self.store, {}, self.store.state_dir, self.host)
            self.assertEqual((refused['status'], refused['http']), ('refused', 429))
            self.assertNotIn(LOGIN, self.store.get_setting(google_host.REFUSED))
            self.assertIs(type(providers.adapter_for(ACCOUNT)), gp.GoogleAdapter)
            handed, _ = google_host.worker_pass(self.store, {}, self.store.state_dir, self.host, refused, lambda *a, **kw: None)
            self.assertEqual(handed['results'], {'imported': 1})
            self.assertIsInstance(providers.adapter_for(ACCOUNT), google_host.HostGoogleAdapter)

    def test_a_bare_429_or_a_refused_recover_request_does_not_free_the_legacy_grant(self):
        with patch.object(google_host, 'HostSignIn', return_value=self.host):
            providers = Providers({}, self.store.state_dir)
            bare = HostError(429, {'ok': False, 'error': 'rate limit exceeded'})   # the gateway may refuse after the host began
            with patch.object(self.host, 'handover', side_effect=bare):
                self.assertEqual(google_host.handover(self.store, {}, self.store.state_dir, self.host)['http'], 429)
            self.assertEqual(self.store.get_setting(google_host.REFUSED)[LOGIN]['reason'], google_host.UNCONFIRMED)
            self.assertIsNone(providers.adapter_for(ACCOUNT))
            early = HostError(429, {'ok': False, 'error': 'rate limit exceeded', 'handover_started': False})
            with patch.object(self.host, 'handover', side_effect=early) as recover:
                self.assertEqual(google_host.handover(self.store, {}, self.store.state_dir, self.host)['status'], 'refused')
            self.assertTrue(recover.call_args.kwargs['recover'])   # a prior uncertainty is only reconciled: this proves nothing
            self.assertEqual(self.store.get_setting(google_host.REFUSED)[LOGIN]['reason'], google_host.UNCONFIRMED)
            self.assertIsNone(providers.adapter_for(ACCOUNT))

    def test_an_unknown_answer_to_recover_keeps_the_old_copy_held_and_never_exchanges_again(self):
        host = MemoryHost()
        host.rows, host.recoverable = [], False   # the host began the exchange but cannot say how it ended
        host.started[LOGIN] = dict(generation='g1', receipt=None)
        self.store.set_setting(google_host.REFUSED, {LOGIN: {'reason': google_host.UNCONFIRMED, 'legacy_generation': 'g1'}})
        before = (self.store.get_account(ACCOUNT), self.store.get_calendar(self.cid), self.store.get_intent(self.intent['id']))
        with patch.object(google_host, 'HostSignIn', return_value=host):
            for _ in range(2):
                google_host.worker_pass(self.store, {}, self.store.state_dir, host, None, lambda *a, **kw: None)
                self.assertIsNone(Providers({}, self.store.state_dir).adapter_for(ACCOUNT))
        self.assertEqual((len(host.recoveries), host.handover_generations), (2, []))   # asked twice, exchanged never
        self.assertEqual(self.store.get_setting(google_host.REFUSED)[LOGIN]['reason'], google_host.UNCONFIRMED)
        self.assertFalse(self.store.get_setting(google_host.MOVED))
        self.assertEqual((self.store.get_account(ACCOUNT), self.store.get_calendar(self.cid),
                          self.store.get_intent(self.intent['id'])), before)

    def test_temporary_token_failure_before_calendar_dispatch_preserves_the_unsent_phase(self):
        host = self.confirmed_host()
        for failure in (HostError(503, {'reason': 'provider_unreachable'}), OSError('sign-in temporarily unavailable')):
            with self.subTest(kind=type(failure).__name__), patch.object(google_host, 'HostSignIn', return_value=host), \
                 patch.object(host, 'token', side_effect=failure), patch.object(gp.urllib.request, 'urlopen') as send:
                providers = Providers({}, self.store.state_dir)
                ctx = SimpleNamespace(store=self.store, providers=providers)
                result = json.loads(cal_manage(ctx, action='create', account_id=ACCOUNT,
                                               changes={'name': 'Another calendar'}, confirm=True))
                self.assertEqual((result['status'], result['result']['unsent']), ('failed', True))
                leased = self.store.lease_intent(self.intent['id'], 'test')
                result = run_leased_intent(self.store, providers, leased)
                self.assertEqual(result['status'], 'pending')
                held = self.store.get_intent(self.intent['id'])
                self.assertTrue(json.loads(held['result_json'])['unsent'])
                self.assertEqual(held['event_id'], self.intent['event_id'])
                send.assert_not_called()

    def test_a_token_failure_after_googles_401_refusal_keeps_the_write_queued_unsent(self):
        host = self.confirmed_host()
        for failure in (OSError('sign-in temporarily unavailable'), HostError(409, {'reason': 'refresh_rejected'})):
            with self.subTest(failure=type(failure).__name__), patch.object(google_host, 'HostSignIn', return_value=host), \
                 patch.object(host, 'token', side_effect=[TOKEN, failure]) as token, \
                 patch.object(gp.urllib.request, 'urlopen', side_effect=HTTPError('https://fixture.invalid', 401, 'expired', None, None)) as send:
                providers = Providers({}, self.store.state_dir)
                self.assertIsInstance(providers.adapter_for(ACCOUNT), google_host.HostGoogleAdapter)
                leased = self.store.lease_intent(self.intent['id'], 'test')
                self.assertEqual(run_leased_intent(self.store, providers, leased)['status'], 'pending')   # the 401 applied nothing
                held = self.store.get_intent(self.intent['id'])
                self.assertEqual((held['event_id'], self.store.unknown_delivery(held)), (self.intent['event_id'], {}))
                self.assertEqual(retry_due_intents(self.store, providers), [])   # later: after the backoff or the sign-in
                send.assert_called_once()
                self.assertEqual(token.call_count, 2)   # the check before the write, then the refresh the provider's 401 asked for
        self.assertEqual(self.store.get_account(ACCOUNT)['status'], 'auth_failed')

    def test_a_lost_create_stays_disputed_through_the_sign_in_wait_and_is_read_back_once(self):
        applied, posts = {}, []
        host = self.confirmed_host()
        def google(request, timeout=None):
            if request.get_method() == 'POST':
                posts.append(request.full_url)
                if applied:   # the stable creation id: a second POST of the same event is refused with 409
                    raise HTTPError(request.full_url, 409, 'duplicate id', None, None)
                applied.update(json.loads(request.data), etag='"v1"')
                raise ConnectionResetError('the reply broke off after Google applied the write')
            reply = MagicMock()
            reply.__enter__.return_value, reply.status, reply.headers = reply, 200, {}
            reply.read.return_value = json.dumps(applied).encode()
            return reply
        api = SimpleNamespace(get_state_dir=lambda: self.store.state_dir, get_settings=lambda keys: {})
        def disputed():
            free = json.loads(tools.cal_free(tools.Context(api), start='2026-12-01T00:00:00+00:00',
                                             end='2026-12-02T00:00:00+00:00', sources=[self.cid]))
            return self.intent['event_id'] in free['conflict_event_ids']
        tokens = [dict(TOKEN, access_token='fixture-1'), HostError(409, {'reason': 'refresh_rejected'}), dict(TOKEN, access_token='fixture-2')]
        with patch.object(google_host, 'HostSignIn', return_value=host), patch.object(host, 'token', side_effect=tokens) as token, \
             patch.object(gp.urllib.request, 'urlopen', side_effect=google):
            providers = Providers({}, self.store.state_dir)
            def attempt():
                return run_leased_intent(self.store, providers, self.store.lease_intent(self.intent['id'], 'test'))
            self.assertEqual((attempt()['status'], disputed()), ('pending', True))   # retried only through its stable id
            # every attempt resolves a fresh adapter and asks the host first: no cached token carries over
            waiting = attempt()   # the host needs the owner's sign-in again: this attempt sends nothing
            self.assertEqual((waiting['status'], disputed(), len(posts)), ('pending', True, 1))
            self.assertNotIn('запись не отправлена', waiting['message'])
            self.assertEqual(attempt()['status'], 'done')   # after the sign-in the retry reads the first write back
        self.assertEqual((len(posts), disputed()), (2, False))
        self.assertEqual(token.call_count, 3)   # each write attempt asked the host first
        self.assertEqual(self.store.get_event(self.intent['event_id'])['external_id'], applied['id'])

    def test_a_collection_write_is_unsent_only_when_its_connection_never_formed(self):
        host = self.confirmed_host()
        unreadable = MagicMock()
        unreadable.__enter__.return_value, unreadable.status, unreadable.headers = unreadable, 200, {}
        unreadable.read.return_value = b'<html>proxy page</html>'
        for failure, outcome in ((URLError(ConnectionRefusedError('refused')), ('failed', True)),
                                 (URLError(TimeoutError('timed out')), ('uncertain', False)),   # may time out after the request left
                                 (ConnectionResetError('the reply broke off'), ('uncertain', False)),
                                 (lambda *args, **kwargs: unreadable, ('uncertain', False))):   # accepted, its answer unreadable
            with self.subTest(failure=repr(failure)), patch.object(google_host, 'HostSignIn', return_value=host), \
                 patch.object(gp.urllib.request, 'urlopen', side_effect=failure):
                ctx = SimpleNamespace(store=self.store, providers=Providers({}, self.store.state_dir))
                result = json.loads(cal_manage(ctx, action='create', account_id=ACCOUNT, changes={'name': 'Team'}, confirm=True))
                self.assertEqual((result['status'], result['result']['unsent']), outcome)

    def test_a_later_token_failure_does_not_erase_an_earlier_accepted_write(self):
        adapter = google_host.HostGoogleAdapter(LOGIN, dict(CONFIRMED), self.host)
        with patch.object(self.host, 'token', return_value=TOKEN), \
             patch.object(gp.urllib.request, 'urlopen') as send:
            response = send.return_value.__enter__.return_value
            response.status, response.headers, response.read.return_value = 200, {}, b'{}'
            for earlier, outcome in ((adapter.list_calendars, ('server', True)),   # an answered read changed nothing
                                     (lambda: adapter.set_default_reminders({'external_id': 'personal'}, []), ('uncertain', False))):
                adapter.begin_operation()
                earlier()
                adapter.expires_at = now_utc()
                with patch.object(self.host, 'token', side_effect=HostError(503, {})), self.assertRaises(ProviderError) as held:
                    adapter.list_calendars()
                self.assertEqual((held.exception.kind, held.exception.unsent), outcome)
            self.assertEqual(send.call_count, 2)

    def test_every_new_write_asks_the_host_under_its_connection_and_only_a_real_401_names_a_rejected_token(self):
        host, calls = self.confirmed_host(), []
        def google(request, timeout=None):
            calls.append(request.get_method())
            if len(calls) == 1:   # the first write is refused once: the token just issued is no longer good
                raise HTTPError(request.full_url, 401, 'expired', None, None)
            return google_accepts(request, timeout)
        providers = SimpleNamespace(state_dir=self.store.state_dir, google_host={'host': host})
        with patch.object(gp.urllib.request, 'urlopen', side_effect=google):
            adapter = google_host.adapter_for(providers, LOGIN)
            for _ in range(2):   # the second write finds a valid token and asks the host anyway
                adapter.begin_operation()
                adapter.set_default_reminders({'external_id': 'personal'}, [])
        self.assertEqual(len(calls), 3)   # refused, retried, then the next write
        self.assertEqual([body.get('rejected') for body in host.token_bodies], [None, 'fixture-access-token-1', None])
        self.assertEqual({body.get('connection_id') for body in host.token_bodies}, {CONFIRMED['connection_id']})

    def test_the_same_connection_waits_for_its_sign_in_while_a_replaced_or_missing_one_never_inherits_the_write(self):
        host = self.confirmed_host()
        with patch.object(google_host, 'HostSignIn', return_value=host):
            with patch.object(gp.urllib.request, 'urlopen', side_effect=google_accepts) as send:
                providers = Providers({}, self.store.state_dir)
                def attempt(intent):
                    return run_leased_intent(self.store, providers, self.store.lease_intent(intent['id'], 'test'))
                host.rows[0]['state'] = 'sign_in_required'   # the same connection needs the owner's sign-in again
                self.assertEqual(attempt(self.intent)['status'], 'pending')   # 409 refresh_rejected of the same connection waits
                send.assert_not_called()
                host.rows[0]['state'] = 'signed_in'   # reauthorized, say from the personal to the IT client: the same connection_id
                self.assertEqual(google_host.follow(self.store, {}, self.store.state_dir, host),
                                 {'status': 'ok', 'added': 0, 'withdrawn': 0})
                self.assertEqual(self.store.get_intent(self.intent['id'])['state'], 'pending')   # the deferred command is carried
                self.assertEqual(attempt(self.intent)['status'], 'done')
                self.assertEqual(send.call_count, 1)
                for name, rows in (('replaced', [verified_row(connection_id='conn-card-2')]), ('missing', [])):
                    with self.subTest(name):
                        host.rows = rows   # disconnected and signed in again, or gone, before the next pass
                        queued = self.queue_event(name)
                        attempt(queued)
                        held = self.store.get_intent(queued['id'])
                        self.assertEqual(held['state'], 'failed')   # not parked: a new connection never inherits the write
                        self.assertTrue(json.loads(held['result_json'])['unsent'])
                self.assertEqual(send.call_count, 1)   # only the first write ever reached Google

    def test_old_return_files_new_consent_and_recovers_its_receipt_without_repeating_it(self):
        import providers_google as gp
        host = MemoryHost()
        host.verified_imports = True
        secrets = {'GOOGLE_OAUTH_CLIENT_ID': 'fixture-client'}
        state = gp.start_auth(self.store, secrets, 'http://127.0.0.1/callback')['state']
        def worker(prior=None):
            return google_host.worker_pass(self.store, secrets, self.store.state_dir, host, prior, lambda *a, **kw: None)
        handed, _ = worker()
        before = self.store.get_account(ACCOUNT), self.store.get_calendar(self.cid), self.store.get_intent(self.intent['id'])
        providers = SimpleNamespace(state_dir=self.store.state_dir, google_host={'host': host, 'moved': {}})
        ctx = SimpleNamespace(store=self.store, state_dir=self.store.state_dir, secrets=secrets, providers=providers)
        def userinfo(adapter):
            adapter._identity = {'sub': SUBJECT}
            return 'renamed@example.test'
        with patch.object(gp, '_token_request', return_value={'access_token': 'fixture-new-access',
                'refresh_token': 'fixture-new-refresh', 'scope': ' '.join(gp.SCOPES), 'expires_in': 3600}), \
             patch.object(gp.GoogleAdapter, 'primary_email', userinfo), \
             patch.object(gp, '_mutate_tokens', side_effect=lambda path, key, mutate: mutate(self.vault.entries)):
            done = accounts.complete_google(ctx, code='fixture-code', state=state)
        self.assertEqual((done['ok'], done['account_id']), (True, ACCOUNT))
        new = self.vault.entries[LOGIN]['generation']
        self.assertNotEqual(new, 'g1')
        host.lose_answer = True
        original_call = host._call
        def incomplete_account_listing(method, path, *args, **kwargs):
            if path == '/sign-in/accounts':
                return {'ok': True, 'services': [{'id': 'google'}]}
            return original_call(method, path, *args, **kwargs)
        with patch.object(host, '_call', side_effect=incomplete_account_listing):
            result, facts = worker(handed)
            self.assertEqual((result['status'], facts['follow']['status']), ('unreachable', 'unreachable'))
            self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['legacy_generation'], 'g1')
        # A fresh worker reads the filed generation; it does not retransmit the previously submitted consent.
        worker()
        worker()
        self.assertEqual(host.handover_generations, ['g1', new])
        self.assertEqual(len(host.recoveries), 1)   # the lost renewal was read back once, never exchanged again
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['legacy_generation'], new)
        self.assertEqual([row['id'] for row in self.store.list_accounts('google')], [ACCOUNT])
        self.assertEqual((self.store.get_account(ACCOUNT), self.store.get_calendar(self.cid),
                          self.store.get_intent(self.intent['id'])), before)
        self.assertEqual(host.token_accounts, [])

    def test_card_disconnect_then_verified_reconsent_restores_key_ids_and_roles(self):
        self.host.rows = [verified_row(connection_id='conn-1')]
        self.follow()
        queued = self.queue_event('Before disconnect')   # queued on the bound connection after first binding
        self.assertEqual(self.store.get_setting(google_host.IDENTITIES)[SUBJECT]['key'], LOGIN)
        self.assertEqual(self.card_disconnect()['withdrawn'], 1)
        self.assertIsNone(self.store.get_account(ACCOUNT))
        self.assertTrue(self.store.get_calendar(self.cid)['deleted_at'])
        self.assertEqual(self.store.get_intent(queued['id'])['state'], 'resolved')
        self.assertNotIn(LOGIN, self.vault.entries)
        # verified re-consent under a fresh label, new connection_id and empty vault: only the subject gives continuity
        self.host.rows = [verified_row(label='renamed@example.test', connection_id='conn-2')]
        self.assertEqual(self.follow()['added'], 1)
        self.assertIsNotNone(self.store.get_account(ACCOUNT))
        mark = self.store.get_setting(google_host.MOVED)[LOGIN]
        self.assertEqual((mark['account'], mark['connection_id']), (VERIFIED, 'conn-2'))
        self.assertEqual(self.store.get_intent(queued['id'])['state'], 'resolved')   # never re-queued
        row = self.store.get_calendar(self.cid)
        self.assertEqual((row['account_id'], row['role_busy'], row['is_default']), (ACCOUNT, 0, 1))
        # the worker's discovery call lifts the retirement without touching the owner's roles
        self.store.upsert_calendar(dict(id=self.cid, account_id=ACCOUNT, provider='google', external_id='personal',
                                        name='Personal', role_visible=True, role_busy=False, is_default=True), restore=True)
        self.assertIsNone(self.store.get_calendar(self.cid)['deleted_at'])
        self.assertEqual(self.store.get_calendar(self.cid)['role_busy'], 0)

    def test_orphaned_credential_copy_is_never_offered_again(self):
        self.host.rows = [verified_row()]
        self.follow()
        self.vault.keep = True   # the credential drop is interrupted; the copy survives the withdrawal
        self.card_disconnect()
        self.vault.keep = False
        self.assertIn(LOGIN, self.vault.entries)
        self.assertEqual(google_host.handover(self.store, {}, self.store.state_dir, self.host)['status'], 'nothing')
        self.assertEqual(self.store.get_setting(google_host.MOVED), {})
        self.vault.entries[LOGIN]['generation'] = 'g2'   # a fresh sign-in replaces the withdrawn generation
        handed = google_host.handover(self.store, {}, self.store.state_dir, self.host)
        self.assertEqual(handed['results'], {'imported': 1})
        self.assertEqual(self.store.get_setting(google_host.WITHDRAWN), {})

    def test_local_disconnect_during_handover_wins_and_imported_grant_stays_out(self):
        def disconnect_mid_transfer(body):
            self.vault.keep = True  # Local cleanup did not finish; ContinuityHost itself stores the via='handover' row.
            accounts.withdraw_account(self.store, self.store.state_dir, '', {'id': ACCOUNT, 'provider': 'google'})
        self.host.on_handover = disconnect_mid_transfer
        handed, facts = google_host.worker_pass(self.store, {}, self.store.state_dir, self.host, None, lambda *a, **kw: None)
        self.assertEqual(handed['results'], {'imported': 1})   # the host kept it; the local intent still wins
        self.assertEqual(self.host.rows[0]['via'], 'handover')
        self.assertEqual(facts['follow']['added'], 0)
        self.assertIsNone(self.store.get_account(ACCOUNT))
        self.assertEqual(self.store.get_setting(google_host.MOVED), {})
        self.assertEqual(self.store.get_setting(google_host.REFUSED)[LOGIN]['reason'], 'withdrawn_during_handover')
        self.assertEqual(self.store.get_setting(google_host.WITHDRAWN)[LOGIN]['host_account'], VERIFIED)
        self.host.on_handover = None
        with patch.object(self.host, 'accounts', side_effect=ValueError('Unreadable account listing')):
            self.assertEqual(self.follow()['status'], 'unreachable')
        self.assertEqual(self.store.get_setting(google_host.WITHDRAWN)[LOGIN]['host_account'], VERIFIED)
        self.assertEqual(self.follow()['added'], 0)   # the imported grant stays out while the card holds it
        self.host.rows = []
        self.follow()   # the owner disconnects the imported grant on the card
        self.assertIsNone(self.store.get_setting(google_host.WITHDRAWN)[LOGIN]['host_account'])
        self.assertIn(LOGIN, self.vault.entries)  # A complete empty listing clears only the host suppression.
        self.assertEqual(google_host.handover(self.store, {}, self.store.state_dir, self.host)['status'], 'nothing')
        self.host.rows = [verified_row(label='renamed@example.test', connection_id='conn-2')]
        self.assertEqual(self.follow()['added'], 1)   # a fresh consent restores the former key
        self.assertIsNotNone(self.store.get_account(ACCOUNT))

    def test_unreadable_vault_still_rebinds_by_the_marks_known_subject(self):
        # An unconfirmed import has no connection_id: the verified card is another consent. It rebinds the key by
        # the mark's known subject, but the queue before it is withdrawn.
        self.host.handover_reply = {'ok': True, 'grants': [{'legacy_key': LOGIN, 'result': 'imported', 'via': 'handover',
                                    'state': 'sign_in_required', 'account': {'key': PROVISIONAL, 'label': LOGIN}}]}
        self.host.rows = [dict(key=PROVISIONAL, label=LOGIN, subject='', verified=False, via='handover',
                               state='sign_in_required', reason='refresh_rejected')]
        google_host.worker_pass(self.store, {}, self.store.state_dir, self.host, None, lambda *a, **kw: None)
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['account'], PROVISIONAL)
        self.vault.fail = True   # the vault cannot be read; the mark still names the verified subject
        self.host.rows.append(verified_row())
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['account'], VERIFIED)
        self.assertEqual(self.store.get_intent(self.intent['id'])['state'], 'resolved')
        self.assertEqual(self.store.get_setting(google_host.IDENTITIES)[SUBJECT]['key'], LOGIN)

        # A confirmed connection that another one replaces withdraws its queue even while the vault is unreadable.
        self.setUp()
        self.host.rows = [verified_row(connection_id='conn-1')]
        self.follow()
        queued = self.store.add_intent('create', ACCOUNT, self.cid, self.intent['event_id'], {})
        self.vault.fail = True
        self.host.rows = [verified_row(connection_id='conn-card-2')]
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 1, 'withdrawn': 1})
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['account'], VERIFIED)
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['connection_id'], 'conn-card-2')
        self.assertEqual(self.store.get_intent(queued['id'])['state'], 'resolved')
        self.assertTrue(self.store.get_setting(google_host.WITHDRAWN)[LOGIN]['generation_unknown'])
        row = self.store.get_calendar(self.cid)
        self.assertEqual((row['account_id'], row['role_busy'], row['is_default']), (ACCOUNT, 0, 1))

    def test_withdrawal_guard_spares_a_rebound_mark(self):
        self.host.rows = [verified_row()]
        self.follow()
        queued = self.store.add_intent('create', ACCOUNT, self.cid, self.intent['event_id'], {})
        stale = accounts.withdraw_account(self.store, self.store.state_dir, '', self.store.get_account(ACCOUNT),
                                          expect_host_key='a-stale-key')
        self.assertEqual(stale['status'], 'rebound')   # the public seam
        with self.store.transaction() as view:
            done, cleanup = google_host._withdraw_in_tx(view, self.store.state_dir, '', LOGIN, 'a-stale-key')
        self.assertEqual((bool(done), cleanup), (False, None))   # the in-transaction seam
        self.assertIsNotNone(self.store.get_account(ACCOUNT))
        self.assertEqual(self.store.get_intent(queued['id'])['state'], 'pending')
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['account'], VERIFIED)
        with self.store.transaction() as view:
            done, cleanup = google_host._withdraw_in_tx(view, self.store.state_dir, '', LOGIN, VERIFIED)
        self.assertTrue(done and cleanup)   # the vault copy is dropped by the caller after the commit
        self.assertIn(LOGIN, self.vault.entries)
        self.assertIsNone(self.store.get_account(ACCOUNT))
        self.assertEqual(self.store.get_intent(queued['id'])['state'], 'resolved')

    def test_same_email_different_subject_never_joins_history(self):
        self.host.rows = [verified_row()]
        self.follow()
        self.card_disconnect()
        self.host.rows = [verified_row(key='a-00000000000000000009', subject='other-subject', label=LOGIN)]
        self.assertEqual(self.follow()['added'], 1)
        marks = self.store.get_setting(google_host.MOVED)
        self.assertNotIn(LOGIN, marks)
        self.assertEqual(marks['oidc:other-subject']['account'], 'a-00000000000000000009')
        retired = self.store.get_calendar(self.cid)
        self.assertEqual((retired['account_id'], bool(retired['deleted_at'])), (ACCOUNT, True))

    def test_ordinary_reconsent_keeps_queue_while_changed_connection_id_withdraws_it_and_keeps_roles(self):
        self.host.rows = [verified_row(connection_id='conn-1')]
        self.follow()
        queued = self.store.add_intent('create', ACCOUNT, self.cid, self.intent['event_id'], {})   # a command of this connection
        # Ordinary re-consent (same connection_id, e.g. a label or a personal-to-IT client change) preserves key, roles and this queue.
        self.host.rows = [verified_row(label='renamed@example.test', connection_id='conn-1')]
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
        self.assertEqual(self.store.get_intent(queued['id'])['state'], 'pending')
        self.assertEqual(self.store.get_setting(google_host.IDENTITIES)[SUBJECT]['key'], LOGIN)

        # Disconnect + new sign-in between two polls changes connection_id: old queue is withdrawn, key/IDs/roles stay.
        self.host.rows = [verified_row(label='returned@example.test', connection_id='conn-2')]
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 1, 'withdrawn': 1})
        self.assertEqual(self.store.get_intent(queued['id'])['state'], 'resolved')
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['connection_id'], 'conn-2')
        self.assertEqual(self.store.get_setting(google_host.IDENTITIES)[SUBJECT]['key'], LOGIN)
        row = self.store.get_calendar(self.cid)
        self.assertEqual((row['account_id'], row['role_busy'], row['is_default']), (ACCOUNT, 0, 1))

        # A host that predates connection ids (a verified row without one) is no replacement: nothing is withdrawn.
        marks = self.store.get_setting(google_host.MOVED)
        marks[LOGIN].pop('connection_id', None)
        self.store.set_setting(google_host.MOVED, marks)
        second_intent = self.store.add_intent('create', ACCOUNT, self.cid, self.intent['event_id'], {})
        self.host.rows = [verified_row(connection_id=None)]
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
        self.assertEqual(self.store.get_intent(second_intent['id'])['state'], 'pending')

    def test_stale_follow_and_renewal_receipt_never_overwrite_newer_connection_id(self):
        self.host.rows = [verified_row(connection_id='conn-1')]
        self.follow()
        new_intent = {}
        def advance_mid_listing():
            self.host.on_accounts = None
            self.host.rows = [verified_row(connection_id='conn-2')]
            self.assertEqual(self.follow(), {'status': 'ok', 'added': 1, 'withdrawn': 1})
            new_intent['row'] = self.store.add_intent('create', ACCOUNT, self.cid, self.intent['event_id'], {})
        # Outer follow() receives stale conn-1 after inner follow() already moved SQLite to conn-2.
        self.host.on_accounts = advance_mid_listing
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['connection_id'], 'conn-2')
        self.assertEqual(self.store.get_intent(new_intent['row']['id'])['state'], 'pending')

        # A stale follow() observing conn-2 after SQLite already moved to conn-3 does not continue past failed expect_mark.
        self.host.rows = [verified_row(connection_id='conn-2')]
        def advance_to_conn3():
            self.host.on_accounts = None
            self.host.rows = [verified_row(connection_id='conn-3')]
            self.assertEqual(self.follow(), {'status': 'ok', 'added': 1, 'withdrawn': 1})
        self.host.on_accounts = advance_to_conn3
        self.follow()
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['connection_id'], 'conn-3')

        # A renewal handover receipt with a different known connection_id never silently replaces conn-3.
        marks = self.store.get_setting(google_host.MOVED)
        marks[LOGIN]['legacy_generation'] = 'g1'
        self.store.set_setting(google_host.MOVED, marks)
        self.vault.entries[LOGIN] = dict(provider='google', email=LOGIN, subject=SUBJECT, client_id='fixture-client',
                                         refresh_token='fixture-refresh-2', scope=' '.join(gp.SCOPES), generation='g2')
        self.host.handover_reply = {'ok': True, 'grants': [{'legacy_key': LOGIN, 'result': 'imported', 'via': 'handover',
                                    'legacy_generation': 'g2', 'connection_id': 'conn-4',
                                    'account': {'key': VERIFIED, 'label': LOGIN}}]}
        google_host.handover(self.store, {}, self.store.state_dir, self.host)
        self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['connection_id'], 'conn-3')

    def test_unreadable_vault_changes_keep_old_local_copy_blocked_on_downgrade_until_explicit_local_complete(self):
        self.host.rows = [verified_row(connection_id='conn-1')]
        self.follow()
        queued = self.store.add_intent('create', ACCOUNT, self.cid, self.intent['event_id'], {})   # the first card sign-in already withdrew the earlier queue
        self.vault.fail = True

        # Incomplete listing changes nothing.
        self.host.complete = False
        self.assertEqual(self.follow()['status'], 'unreachable')
        self.assertEqual(self.store.get_intent(queued['id'])['state'], 'pending')
        self.host.complete = True

        # Confirmed replacement in a complete listing applies in SQLite while keeping WITHDRAWN for the unforgotten vault copy.
        self.host.rows = [verified_row(connection_id='conn-2')]
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 1, 'withdrawn': 1})
        self.assertEqual(self.store.get_intent(queued['id'])['state'], 'resolved')
        self.assertTrue(self.store.get_setting(google_host.WITHDRAWN)[LOGIN]['generation_unknown'])

        # Confirmed new host connection stays visible in reload_google despite WITHDRAWN guarding the local copy.
        class FakeAdapter:
            def list_calendars(inner):
                return [dict(id=self.cid, account_id=ACCOUNT, provider='google', external_id='personal', name='Personal', writable=True)]
        secrets = {'GOOGLE_OAUTH_CLIENT_ID': 'fixture-client'}
        providers = SimpleNamespace(state_dir=self.store.state_dir, google_host={'host': self.host, 'store': self.store},
                                    errors={}, adapter_for=lambda aid: FakeAdapter())
        ctx = SimpleNamespace(store=self.store, state_dir=self.store.state_dir, secrets=secrets, providers=providers)
        with patch.object(accounts, 'Providers', return_value=providers):
            reloaded = accounts.reload_google(ctx)
        self.assertEqual((reloaded['status'], len(reloaded['accounts'])), ('ok', 1))

        # Disconnect on the card while the vault is still unreadable so generation_unknown remains in force.
        self.assertEqual(self.card_disconnect()['withdrawn'], 1)
        self.assertTrue(self.store.get_setting(google_host.WITHDRAWN)[LOGIN]['generation_unknown'])

        # Unlocking the vault on a core downgrade still keeps the unforgotten old local copy g1 blocked.
        self.vault.fail = False
        downgraded = SimpleNamespace(state=lambda retry=False: 'absent')
        old_providers = SimpleNamespace(state_dir=self.store.state_dir, secrets=secrets, vault_error='',
                                        saved_accounts=self.vault.entries,
                                        google_host={'host': downgraded, 'store': self.store})
        with self.assertRaises(ProviderError):
            google_host.adapter_for(old_providers, LOGIN)

        # Explicit local complete after generation_unknown allows only the generation it wrote and does not carry
        # over the previously withdrawn refresh token when the provider omits one.
        def userinfo(adapter):
            adapter._identity = {'sub': SUBJECT}
            return LOGIN
        def complete_local(token_reply):
            state = gp.start_auth(ctx.store, ctx.secrets, 'http://127.0.0.1/callback')['state']
            with patch.object(gp, '_token_request', return_value=token_reply), \
                 patch.object(gp.GoogleAdapter, 'primary_email', userinfo), \
                 patch.object(gp, '_mutate_tokens', side_effect=lambda p, k, fn: fn(self.vault.entries)):
                return accounts.complete_google(ctx, code='fixture-code', state=state)
        access_only = complete_local({'access_token': 'new-a1', 'scope': ' '.join(gp.SCOPES), 'expires_in': 3600})
        self.assertEqual((access_only['ok'], access_only['account_id'], access_only['offline_access']), (True, ACCOUNT, False))
        self.assertEqual(self.vault.entries[LOGIN]['refresh_token'], '')
        first_gen = self.vault.entries[LOGIN]['generation']
        record = self.store.get_setting(google_host.WITHDRAWN)[LOGIN]
        self.assertTrue(record.get('generation_unknown'))
        self.assertFalse(google_host.withdrawn_generation(record, {'generation': first_gen}))
        self.assertTrue(google_host.withdrawn_generation(record, {'generation': 'g1'}))

        # Continuous confirmed local re-consent on the same client keeps its working refresh token when the
        # provider omits a new one, and reopens the saved local path without claiming core handover of a withdrawn account.
        with_refresh = complete_local({'access_token': 'new-a2', 'refresh_token': 'new-r',
                                       'scope': ' '.join(gp.SCOPES), 'expires_in': 3600})
        self.assertTrue(with_refresh['ok'] and with_refresh['offline_access'])
        kept_refresh = complete_local({'access_token': 'new-a3', 'scope': ' '.join(gp.SCOPES), 'expires_in': 3600})
        self.assertTrue(kept_refresh['ok'] and kept_refresh['offline_access'])
        self.assertEqual(self.vault.entries[LOGIN]['refresh_token'], 'new-r')
        final_gen = self.vault.entries[LOGIN]['generation']
        record = self.store.get_setting(google_host.WITHDRAWN)[LOGIN]
        self.assertFalse(google_host.withdrawn_generation(record, {'generation': final_gen}))
        self.assertTrue(google_host.withdrawn_generation(record, {'generation': 'g1'}))
        self.assertIsInstance(google_host.adapter_for(old_providers, LOGIN), gp.GoogleAdapter)

    def test_ordinary_pass_hands_over_two_distinct_local_accounts_without_restart(self):
        patcher = patch.object(google_host, '_read_host_withdrawn', wraps=google_host._read_host_withdrawn)
        ledger = patcher.start()
        self.addCleanup(patcher.stop)
        second = 'saved@example.test'
        self.vault.entries[second] = dict(provider='google', email=second, subject='sub-saved',
                                          client_id='fixture-client', refresh_token='r-saved', scope=' '.join(gp.SCOPES))
        handed, facts = google_host.worker_pass(self.store, {'GOOGLE_OAUTH_CLIENT_ID': 'fixture-client'},
                                                self.store.state_dir, self.host,
                                                {'status': 'ok', 'results': {'imported': 1}}, lambda *a, **kw: None)
        self.assertEqual(handed['results'], {'imported': 2})
        self.assertEqual(ledger.call_count, 1)   # one reading of core's disconnect ledger for the pass's two grants
        self.assertEqual(facts['follow'], {'status': 'ok', 'added': 1, 'withdrawn': 0})
        marks = self.store.get_setting(google_host.MOVED)
        self.assertEqual(set(marks), {LOGIN, second})
        self.assertNotEqual(marks[LOGIN]['account'], marks[second]['account'])
        self.assertNotEqual(marks[LOGIN]['connection_id'], marks[second]['connection_id'])

    def test_card_listing_does_not_clear_unconfirmed_handover_reservation_and_missing_via_is_not_guessed(self):
        host = MemoryHost()
        host.verified_imports = host.lose_answer = True
        host.rows = []
        self.assertEqual(google_host.handover(self.store, {}, self.store.state_dir, host)['status'], 'unreachable')
        self.assertEqual(self.store.get_setting(google_host.REFUSED)[LOGIN]['reason'], google_host.UNCONFIRMED)
        host.rows = [verified_row(via='card', connection_id='conn-card-2'),
                     verified_row(key='a-00000000000000000008', subject='sub-plain', label='plain@example.test', via=None)]
        self.assertEqual(google_host.follow(self.store, {}, self.store.state_dir, host), {'status': 'ok', 'added': 1, 'withdrawn': 0})
        marks = self.store.get_setting(google_host.MOVED)
        self.assertNotIn(LOGIN, marks)   # reading the card proves nothing about the transfer
        self.assertEqual(self.store.get_setting(google_host.REFUSED)[LOGIN]['reason'], google_host.UNCONFIRMED)
        self.assertEqual(self.store.get_intent(self.intent['id'])['state'], 'pending')
        self.assertEqual(marks['plain@example.test']['via'], '')

    def test_older_core_unknown_identity_keeps_working_with_separate_calendars(self):
        self.host.rows = [verified_row()]
        self.follow()
        self.card_disconnect()
        secrets = {'GOOGLE_OAUTH_CLIENT_ID': 'fixture-client'}
        state = gp.start_auth(self.store, secrets, 'http://127.0.0.1/callback')['state']
        old_host = SimpleNamespace(state=lambda retry=False: 'absent')
        ctx = SimpleNamespace(store=self.store, state_dir=self.store.state_dir, secrets=secrets,
                              providers=SimpleNamespace(state_dir=self.store.state_dir, google_host={'host': old_host}))
        with patch.object(gp, '_token_request', return_value={'access_token': 'old-a', 'refresh_token': 'old-r',
                'scope': ' '.join(gp.SCOPES), 'expires_in': 3600}), \
             patch.object(gp.GoogleAdapter, 'primary_email', return_value=LOGIN), \
             patch.object(gp, '_mutate_tokens', side_effect=lambda p, k, fn: fn(self.vault.entries)):
            done = accounts.complete_google(ctx, code='fixture-code', state=state)
        self.assertTrue(done['ok'])
        self.assertNotEqual(done['account_id'], ACCOUNT)
        self.assertTrue(self.store.get_calendar(self.cid)['deleted_at'])

    def lost_first_answer(self, host):
        # The worker's first handover of g1 got no answer: its exchange is reserved and unconfirmed.
        with patch.object(host, 'handover', side_effect=OSError('the reply was lost')):
            self.assertEqual(google_host.handover(self.store, {}, self.store.state_dir, host)['status'], 'unreachable')

    def moved_with_unconfirmed_renewal(self, host, generation):
        # R holds the account (g0 on conn-card-1); the renewal of ``generation`` got no answer the host can give.
        host.rows = [dict(verified_row(via='handover', connection_id=CONFIRMED['connection_id']), legacy_generation='g0')]
        self.store.set_setting(google_host.MOVED, {LOGIN: dict(CONFIRMED, via='handover', legacy_generation='g0')})
        self.store.set_setting(google_host.REFUSED, {LOGIN: {'reason': google_host.UNCONFIRMED, 'legacy_generation': generation,
                                                             'exchange': 'unknown', 'terminal': True}})
        return self.store.get_setting(google_host.MOVED)[LOGIN]

    def late_sign_in(self, host, refresh):
        # A sign-in the old path started before the update completes for the same subject; returns its generation.
        secrets = {'GOOGLE_OAUTH_CLIENT_ID': 'fixture-client'}
        state = gp.start_auth(self.store, secrets, 'http://127.0.0.1/callback')['state']
        ctx = SimpleNamespace(store=self.store, state_dir=self.store.state_dir, secrets=secrets,
                              providers=SimpleNamespace(state_dir=self.store.state_dir, google_host={'host': host}))

        def userinfo(adapter):
            adapter._identity = {'sub': SUBJECT}
            return LOGIN
        with patch.object(gp, '_token_request', return_value={'access_token': 'fixture-late-access', 'refresh_token': refresh,
                                                              'scope': ' '.join(gp.SCOPES), 'expires_in': 3600}), \
                patch.object(gp.GoogleAdapter, 'primary_email', userinfo), \
                patch.object(gp, '_mutate_tokens', side_effect=lambda path, key, mutate: mutate(self.vault.entries)):
            done = accounts.complete_google(ctx, code='fixture-code', state=state)
        self.assertEqual((done['ok'], done['offline_access']), (True, False))   # no lasting access is promised meanwhile
        return self.vault.entries[LOGIN]['generation']

    def test_card_consent_after_a_settled_unknown_exchange_restores_the_account_without_its_queue(self):
        # R5: recover keeps answering exchange=unknown for g1 (terminal). The owner then signs in on the card: that new
        # consent restores the key, calendars and roles without becoming the old exchange's success, so the writes
        # waiting from before it are withdrawn and an uncertain one stays for comparison.
        host = MemoryHost()
        host.rows, host.recoverable = [], False
        host.started[LOGIN] = dict(generation='g1', receipt=None)
        self.store.set_setting(google_host.REFUSED, {LOGIN: {'reason': google_host.UNCONFIRMED, 'legacy_generation': 'g1'}})
        sent = self.queue_event('Sent?')
        self.store.settle_intent(sent['id'], 'uncertain', {'error': 'lost after send', 'kind': 'network'})
        before = (self.store.get_account(ACCOUNT), self.store.get_calendar(self.cid))

        def worker():
            google_host.worker_pass(self.store, {}, self.store.state_dir, host, None, lambda *a, **kw: None)
        with patch.object(google_host, 'HostSignIn', return_value=host):
            worker()
            self.assertTrue(self.store.get_setting(google_host.REFUSED)[LOGIN]['terminal'])
            providers = Providers({}, self.store.state_dir)
            self.assertIsNone(providers.adapter_for(ACCOUNT))
            self.assertIn('withdrawn, not sent', providers.errors[ACCOUNT])   # the hold names the card and the queue's fate
            # The same address under another verified subject, or this subject's card while not signed in, is no consent.
            host.rows = [verified_row(key='a-00000000000000000009', subject='other-subject', connection_id='conn-other'),
                         dict(verified_row(label='renamed@example.test', connection_id='conn-card-1'), state='sign_in_required')]
            worker()
            self.assertNotIn(LOGIN, self.store.get_setting(google_host.MOVED))
            self.assertEqual(self.store.get_intent(self.intent['id'])['state'], 'pending')
            host.rows[1]['state'] = 'signed_in'
            worker()
            mark = self.store.get_setting(google_host.MOVED)[LOGIN]
            self.assertEqual((mark['account'], mark['via'], mark['connection_id']), (VERIFIED, 'card', 'conn-card-1'))
            self.assertFalse({'legacy_generation', 'result'} & set(mark))   # nothing records the old exchange as done
            self.assertNotIn(LOGIN, self.store.get_setting(google_host.REFUSED))
            adapter = Providers({}, self.store.state_dir).adapter_for(ACCOUNT)
            self.assertIsInstance(adapter, google_host.HostGoogleAdapter)
            adapter._refresh()   # an ordinary read asks the host without naming a connection
            recovered = len(host.recoveries)
            worker()
        self.assertEqual((len(host.recoveries), host.handover_generations), (recovered, []))   # never read back or exchanged again
        self.assertEqual((host.token_accounts, [body.get('connection_id') for body in host.token_bodies]), ([VERIFIED], [None]))
        self.assertEqual((self.store.get_account(ACCOUNT), self.store.get_calendar(self.cid)), before)
        dropped = self.store.get_intent(self.intent['id'])
        self.assertEqual((dropped['state'], json.loads(dropped['result_json'])['note']),
                         ('resolved', google_host.UNCONFIRMED_CONTINUITY))
        self.assertEqual(self.store.get_intent(sent['id'])['state'], 'uncertain')

    def test_card_reconsent_after_an_unknowable_renewal_restores_the_account_without_its_queue(self):
        host = MemoryHost()
        moved = self.moved_with_unconfirmed_renewal(host, 'g2')
        self.vault.entries[LOGIN] = dict(self.vault.entries[LOGIN], generation='g2', refresh_token='fixture-refresh-2')
        with patch.object(google_host, 'HostSignIn', return_value=host), \
                patch.object(gp.urllib.request, 'urlopen', side_effect=google_accepts) as send:
            providers = Providers({}, self.store.state_dir)
            self.assertIsNone(providers.adapter_for(ACCOUNT))
            self.assertIn(google_host.CARD, providers.errors[ACCOUNT])   # the hold says where it ends
            google_host.follow(self.store, {}, self.store.state_dir, host)
            self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN], moved)   # the handed-over consent ends nothing
            # Signed in again on the card: the host keeps this subject's connection and the account's origin.
            host.rows = [dict(verified_row(label='renamed@example.test', connection_id=moved['connection_id']), legacy_generation='g0')]
            google_host.follow(self.store, {}, self.store.state_dir, host)
            mark = self.store.get_setting(google_host.MOVED)[LOGIN]
            self.assertEqual((mark['via'], mark['connection_id'], 'legacy_generation' in mark), ('card', 'conn-card-1', False))
            self.assertNotIn(LOGIN, self.store.get_setting(google_host.REFUSED))
            dropped = self.store.get_intent(self.intent['id'])
            self.assertEqual((dropped['state'], json.loads(dropped['result_json'])['note']),
                             ('resolved', google_host.UNCONFIRMED_CONTINUITY))   # the new sign-in gives the old command no right
            self.assertEqual(google_host.handover(self.store, {}, self.store.state_dir, host)['status'], 'nothing')
            later = self.queue_event('After the card')
            report = retry_due_intents(self.store, Providers({}, self.store.state_dir), owner='test')
        self.assertEqual(([(item['intent'], item['status']) for item in report], send.call_count, host.recoveries),
                         ([(later['id'], 'done')], 1, []))

    def test_a_late_sign_in_moves_only_with_another_refresh_and_never_takes_the_queue_before_it(self):
        # g1's first move got no answer, then the old path's sign-in completes. Before any pass the queue before it is held
        # for review. The same refresh is neither offered nor refreshed here; another refresh makes its own ordinary
        # attempt, which the core takes when g1's exchange never left and refuses while that exchange is outgoing.
        for case in ('same refresh', 'g1 never left', 'g1 outgoing'):
            with self.subTest(case):
                self.setUp()
                host = MemoryHost()
                host.rows, host.verified_imports = [], True
                with patch.object(google_host, 'HostSignIn', return_value=host):
                    stale = Providers({}, self.store.state_dir)   # read while g1 was the working local grant
                    stale.saved_accounts = {name: dict(saved) for name, saved in stale.saved_accounts.items()}   # as a real read: a copy
                    kept = stale.adapter_for(ACCOUNT)
                self.lost_first_answer(host)
                if case == 'g1 outgoing':
                    host.started[LOGIN] = dict(generation='g1', receipt=None)
                new = self.late_sign_in(host, 'fixture-refresh' if case == 'same refresh' else 'fixture-refresh-late')
                held = self.store.get_intent(self.intent['id'])
                self.assertEqual((held['state'], json.loads(held['result_json'])['note']), ('conflict', google_host.UNCONFIRMED_TRANSFER))
                with patch.object(google_host, 'HostSignIn', return_value=host):
                    adapter = Providers({}, self.store.state_dir).adapter_for(ACCOUNT)
                    self.assertIs(type(adapter), gp.GoogleAdapter)   # before a pass: the new grant's own fresh access
                    self.assertEqual(bool(adapter.refresh_token), case != 'same refresh')   # the same refresh is never used here
                    google_host.worker_pass(self.store, {}, self.store.state_dir, host, None, lambda *a, **kw: None)
                    after = Providers({}, self.store.state_dir).adapter_for(ACCOUNT)
                    self.assertIsNot(type(stale.adapter_for(ACCOUNT)), gp.GoogleAdapter)   # never that older copy; the host may serve
                    with self.assertRaises(ProviderError):
                        kept._legacy_guard(False)   # nor refreshed or sent through the adapter chosen then
                self.assertEqual((host.recoveries, host.handover_generations), ([], [new] if case == 'g1 never left' else []))
                self.assertEqual(self.store.get_intent(self.intent['id'])['state'], 'conflict')   # the queue before it stays apart
                if case == 'g1 never left':   # the core took it: an ordinary moved account
                    self.assertEqual(self.store.get_setting(google_host.MOVED)[LOGIN]['legacy_generation'], new)
                    self.assertIsInstance(after, google_host.HostGoogleAdapter)
                elif case == 'g1 outgoing':   # refused while g1's exchange is unresolved: never displaced, the card decides
                    self.assertEqual(self.store.get_setting(google_host.REFUSED)[LOGIN]['legacy_generation'], new)
                    self.assertIsNone(after)
                else:   # not offered: only its own access token, and the reservation stays about g1
                    self.assertEqual(self.store.get_setting(google_host.REFUSED)[LOGIN]['legacy_generation'], 'g1')
                    self.assertIs(type(after), gp.GoogleAdapter)

    def test_a_moved_accounts_late_sign_in_with_another_refresh_renews_on_its_own(self):
        # R (g0) holds the account and the renewal of g1 is unconfirmed; the old path's sign-in with another refresh then
        # makes its own ordinary renewal of R. The queue before it stays held; a new command goes once the core took it.
        host = MemoryHost()
        host.verified_imports, host.handover_connection_id = True, CONFIRMED['connection_id']
        self.moved_with_unconfirmed_renewal(host, 'g1')
        new = self.late_sign_in(host, 'fixture-refresh-late')
        self.assertEqual(self.store.get_intent(self.intent['id'])['state'], 'conflict')
        with patch.object(google_host, 'HostSignIn', return_value=host), \
                patch.object(gp.urllib.request, 'urlopen', side_effect=google_accepts) as send:
            google_host.worker_pass(self.store, {}, self.store.state_dir, host, None, lambda *a, **kw: None)
            later = self.queue_event('After the renewal')
            report = retry_due_intents(self.store, Providers({}, self.store.state_dir), owner='test')
        mark = self.store.get_setting(google_host.MOVED)[LOGIN]
        self.assertEqual((mark['legacy_generation'], mark['connection_id']), (new, CONFIRMED['connection_id']))
        self.assertEqual(([(item['intent'], item['status']) for item in report], send.call_count), ([(later['id'], 'done')], 1))
        self.assertEqual((host.recoveries, host.handover_generations), ([], [new]))

    def test_an_account_disconnected_here_comes_back_only_on_another_card_connection(self):
        # The owner disconnects here while g1's move is unanswered and the owner's card is already listed: that card keeps
        # the account out; only another connection on the card brings it back.
        self.lost_first_answer(self.host)
        self.host.rows = [verified_row(connection_id='conn-card-1')]
        self.assertEqual(self.follow()['added'], 0)   # while the answer can still be read back, the card settles nothing
        accounts.withdraw_account(self.store, self.store.state_dir, '', self.store.get_account(ACCOUNT))
        for _ in range(2):   # the card already listed, signed in again or not, does not undo the disconnect
            self.assertEqual(self.follow(), {'status': 'ok', 'added': 0, 'withdrawn': 0})
            self.assertIsNone(self.store.get_account(ACCOUNT))
        self.assertEqual(self.store.get_setting(google_host.WITHDRAWN)[LOGIN]['host_connection_id'], 'conn-card-1')
        self.host.rows = [verified_row(label='renamed@example.test', connection_id='conn-card-2')]   # disconnected and signed in again
        self.assertEqual(self.follow(), {'status': 'ok', 'added': 1, 'withdrawn': 0})
        mark = self.store.get_setting(google_host.MOVED)[LOGIN]
        self.assertEqual((mark['account'], mark['connection_id'], 'legacy_generation' in mark), (VERIFIED, 'conn-card-2', False))
        self.assertNotIn(LOGIN, self.store.get_setting(google_host.REFUSED))
        self.assertEqual(self.store.get_intent(self.intent['id'])['state'], 'resolved')   # the disconnect's withdrawal stands
        row = self.store.get_calendar(self.cid)
        self.assertEqual((row['account_id'], row['role_busy'], row['is_default']), (ACCOUNT, 0, 1))
        providers = SimpleNamespace(state_dir=self.store.state_dir, google_host={'host': self.host})
        self.assertIsInstance(google_host.adapter_for(providers, LOGIN), google_host.HostGoogleAdapter)


if __name__ == '__main__':
    unittest.main()
