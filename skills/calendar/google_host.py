"""Calendar's two paths to Google sign-in.

On an Ouroboros whose Host Service says ``sign_in_version`` 1 the host holds Calendar's Google sign-ins.
The worker hands over the tokens and clients the skill held, checking each pass for a late legacy consent,
then follows
the accounts the host lists for Calendar (``follow``). Every Google account recorded in ``MOVED`` takes its
access tokens from the host (``HostGoogleAdapter``) and is never refreshed here: one owner of each refresh
token. On an older Ouroboros nothing here runs and the skill signs in, stores and refreshes as before; the
skill's untouched copy of a moved account's tokens serves exactly that case (a core downgrade).
An intentional disconnect is recorded with the account's removal (``WITHDRAWN``), so a leftover credential
copy is never handed back; a verified subject's former key (``IDENTITIES``) restores its calendars on re-consent.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, Optional, Tuple

from host_sign_in import ABSENT, READY, SERVICE, HostError, HostSignIn
from model import PROVIDER_GOOGLE, iso_utc, now_utc, parse_stored
from ops import ProviderError
from providers_google import GoogleAdapter, client_credentials, load_tokens

MOVED = 'google_moved_to_host'       # calendar key → {account: the host's key, label, subject, scope, via, connection_id?, at}
REFUSED = 'google_handover_refused'  # calendar key → {reason, at, legacy_generation?, attempt?, exchange?}: refusal or reserved exchange
UNCONFIRMED = 'handover_unconfirmed'
UNCONFIRMED_CONTINUITY = 'prior queued writes not carried over: authorization continuity across changed host key could not be confirmed'
WITHDRAWN_MARKER = 'sign_in_withdrawn.json'  # core-written pre-confirmed Disconnect/break ledger in Calendar's state_dir
UNCONFIRMED_DISCONNECT = 'held for explicit review: prior sign-in status in sign_in_withdrawn.json could not be confirmed before new local PKCE; not sent'
UNCONFIRMED_TRANSFER = "held for explicit review: the prior sign-in's move to Ouroboros was not confirmed before a new local sign-in; not sent"
OFFERED = 'google_handover_clients'  # installation clients already handed over: one the owner removes stays removed
WITHDRAWN = 'google_withdrawn'       # calendar key → {generation, host_account?, at}: an intentional disconnect, recorded with the removal
IDENTITIES = 'google_subject_ids'    # verified Google subject → {key, at}: non-secret continuity evidence across disconnects
KEPT = ('imported', 'already_present', 'kept_newer')   # the host holds a grant for the account: it is moved
CARD = "Calendar's card in Extensions › Skills"
RETURN_PAGE = '/onboarding/static/provider-return.html'


class HostGoogleAdapter(GoogleAdapter):
    """A Google account whose sign-in Ouroboros holds: its access tokens come from the Host Service and live
    in this process's memory only; the skill never refreshes or stores them."""

    def __init__(self, key: str, mark: Dict[str, Any], host: HostSignIn, *, store=None):
        super().__init__(str(mark.get('label') or key), '', '', now_utc(), '', '', None,
                         credential_key=key, scopes=mark.get('scope') or None)
        self._host, self._host_account, self._store = host, str(mark['account']), store
        self._connection_id = _text(mark.get('connection_id'))
        # The offline grant is the host's fact: no answer of the handover or the token call proves a
        # refreshable grant, so offline_access stays unknown (null) — never claimed here.
        self._offline_access = None

    def _prepare_write(self) -> bool:
        # Before every new mutating request, verify local connection identity (account + connection_id, or a
        # confirmed transfer of the same connection_id) and ask the host for a token under expected connection_id.
        if self._store is not None:
            current = (self._store.get_setting(MOVED) or {}).get(self.credential_key)
            if not isinstance(current, dict) or not _text(current.get('account')):
                raise _connection_changed(not self._sent, disconnected=True)
            cur_account, cur_conn = _text(current.get('account')), _text(current.get('connection_id'))
            old_binding = getattr(self, '_binding', None) or {}
            if self._connection_id:
                if cur_conn != self._connection_id:
                    raise _connection_changed(not self._sent)
                self._host_account = cur_account
            else:
                if cur_account != self._host_account:
                    raise _connection_changed(not self._sent)
                if cur_conn:
                    if not _continuous_authorization(old_binding, {**current, 'verified': True}):
                        raise _connection_changed(not self._sent)
                    self._connection_id = cur_conn
                else:
                    old_via, cur_via = _text(old_binding.get('via')), _text(current.get('via'))
                    old_gen, cur_gen = _text(old_binding.get('legacy_generation')), _text(current.get('legacy_generation'))
                    if (old_via and cur_via and cur_via != old_via) or ((old_gen or cur_gen) and old_gen != cur_gen):
                        raise _connection_changed(not self._sent)
            self._binding = dict(current)
        self._refresh_for_request(for_write=True)
        return True

    def _refresh(self) -> None:
        rejected = getattr(self, '_rejected_token', None)  # set only after an actual 401 from Google
        for_write = bool(getattr(self, '_for_write', False))
        try:
            answer = self._host.token(self._host_account, rejected=rejected,
                                      connection_id=(self._connection_id or None) if for_write else None)
        except HostError as exc:
            # unsent only while nothing of this operation has gone out: a refresh the provider's 401
            # demanded, or any later step of a composite action, happened after the send started.
            raise _refusal(exc, unsent=not self._sent) from None
        except (OSError, ValueError) as exc:
            raise ProviderError('network', 'The Ouroboros sign-in service did not answer '
                                           f'({type(exc).__name__}); nothing was refreshed here.') from None
        if not answer.get('access_token'):
            raise ProviderError('server', 'The Ouroboros sign-in service answered without an access token.')
        returned_conn = _text(answer.get('connection_id'))
        if for_write:
            if self._connection_id and returned_conn != self._connection_id:
                raise _connection_changed(not self._sent)
            if not self._connection_id and returned_conn:
                msg = (f'This Google sign-in on {CARD} gained a confirmed connection; waiting for Calendar to '
                       + ('reconcile its queue before sending.' if not self._sent
                          else 'confirm a later step; compare before retrying.'))
                raise ProviderError('uncertain' if self._sent else 'retry', msg, unsent=not self._sent)
        self.access_token = str(answer['access_token'])
        self.expires_at = _expiry(answer.get('expires_at'))
        if answer.get('scope'):
            self.scopes = set(str(answer['scope']).split())

    def calendar_capabilities(self):
        return {**super().calendar_capabilities(), 'offline_access': self._offline_access, 'sign_in': 'ouroboros'}


def _binding(session, key):
    # The reservation and its replacement by MOVED must be seen in one SQLite snapshot.
    with session['store']._conn() as conn:
        rows = {row['key']: json.loads(row['value_json']).get(key) for row in conn.execute(
            'SELECT key,value_json FROM settings WHERE key IN (?,?,?)', (MOVED, REFUSED, WITHDRAWN))}
    return rows.get(MOVED), rows.get(REFUSED) or {}, rows.get(WITHDRAWN)


def _read_host_withdrawn(state_dir: str):
    """Read core's pre-confirmed sign_in_withdrawn.json in Calendar's state_dir.
    Missing -> []; unreadable or malformed -> None (never treated as empty)."""
    path = os.path.join(state_dir, WITHDRAWN_MARKER)
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('v') != 1 or not isinstance(data.get('accounts'), list):
        return None
    if not all(isinstance(row, dict) for row in data['accounts']):
        return None
    return [row for row in data['accounts'] if _text(row.get('service')) == SERVICE]


_FRESH = object()   # host_withdrawn_state reads sign_in_withdrawn.json itself unless a pass hands it its one reading


def host_withdrawn_state(state_dir: str, key: str, entry: Dict[str, Any], *,
                         mark: Optional[Dict[str, Any]] = None, withdrawn: Optional[Dict[str, Any]] = None,
                         rows: Any = _FRESH) -> str:
    """Classify core's sign_in_withdrawn.json for this local entry: ``withdrawn``, ``unknown`` or ``clear``.
    Requires an exact identifier match (stable connection_id or specific local legacy_generation); weak fields
    (account, subject, legacy_key) may only narrow. Unreadable JSON is ``unknown``, never ``withdrawn``.
    ``rows``: the one reading a handover pass takes for all its entries; every check before a write reads anew."""
    e_gen, e_sub = _text(entry.get('generation')), _text(entry.get('subject'))
    m_account, m_conn = _text((mark or {}).get('account')), _text((mark or {}).get('connection_id'))
    m_sub, m_gen = _text((mark or {}).get('subject')), _text((mark or {}).get('legacy_generation'))
    fresh_gen = _text((withdrawn or {}).get('fresh_local_generation'))
    is_fresh_local = bool(fresh_gen and e_gen and e_gen == fresh_gen and m_gen != e_gen)
    rows = _read_host_withdrawn(state_dir) if rows is _FRESH else rows
    if rows is None:
        return 'clear' if is_fresh_local else ('unknown' if (mark or entry) else 'clear')
    entry_sub = e_sub or m_sub
    same_mark_gen = not e_gen or not m_gen or e_gen == m_gen
    ambiguous = False
    for row in rows:
        r_account, r_conn = _text(row.get('account')), _text(row.get('connection_id'))
        r_sub, r_lkey, r_lgen = _text(row.get('subject')), _text(row.get('legacy_key')), _text(row.get('legacy_generation'))
        if r_lkey and key and r_lkey != key:
            continue
        if r_sub and entry_sub and r_sub != entry_sub:
            continue
        if r_account and m_account and same_mark_gen and r_account != m_account:
            continue
        if r_lgen:
            target_gen = e_gen or m_gen
            if not target_gen or target_gen != r_lgen:
                continue
            if r_conn and m_conn and same_mark_gen and r_conn != m_conn:
                continue
            return 'withdrawn'
        if r_conn and m_conn and same_mark_gen and r_conn == m_conn:
            return 'withdrawn'
        if not r_lgen and not (r_conn and m_conn) and not is_fresh_local and same_mark_gen:
            if (r_account and m_account and r_account == m_account) or (not e_gen and not m_conn and r_lkey and r_lkey == key and r_account):
                ambiguous = True
    return 'unknown' if ambiguous else 'clear'


def host_withdrawn(state_dir: str, key: str, entry: Dict[str, Any], *,
                   mark: Optional[Dict[str, Any]] = None, withdrawn: Optional[Dict[str, Any]] = None) -> bool:
    """True only when sign_in_withdrawn.json positively proves this local entry's grant was disconnected."""
    return host_withdrawn_state(state_dir, key, entry, mark=mark, withdrawn=withdrawn) == 'withdrawn'


def _renewal_refused_before_exchange(attempted: Optional[Dict[str, Any]], entry: Dict[str, Any]) -> bool:
    """True when a pending local renewal was confirmed refused before exchange; the prior host grant remains usable."""
    return bool(attempted and attempted.get('exchange') == 'none'
                and attempted.get('reason') not in (UNCONFIRMED, 'account_disconnected', '')
                and attempted.get('legacy_generation')
                and attempted.get('legacy_generation') == entry.get('generation'))


def _unresolvable(attempted: Optional[Dict[str, Any]], entry: Optional[Dict[str, Any]],
                  withdrawn: Optional[Dict[str, Any]] = None) -> bool:
    """A reserved exchange whose outcome Calendar will never learn: the host answered that it cannot tell
    (``terminal``), the owner withdrew that generation here, or a later local consent replaced the grant a recover
    would carry. A lost answer about the grant still held is read back instead (``_handover_input``), and a later
    grant whose refresh complete_google showed to differ (``fresh_generation``) first makes its own attempt."""
    attempted, entry_generation = attempted or {}, (entry or {}).get('generation')
    generation = attempted.get('legacy_generation')
    if entry_generation and entry_generation == attempted.get('fresh_generation'):
        return False
    return attempted.get('reason') == UNCONFIRMED and (
        attempted.get('terminal') is True or withdrawn_generation(withdrawn, {'generation': generation})
        or entry_generation != generation)


def _card_consent(account: Dict[str, Any], subject: Any) -> bool:
    """The owner's own working sign-in on the card for this verified subject: a new consent on its own connection.
    It proves nothing about an earlier exchange, and the listing names no consent time, so not that it came later."""
    return bool(_text(subject) and _verified_subject(account) == subject and _text(account.get('via')) == 'card'
                and account.get('state') == 'signed_in' and _verified_connection_id(account))


def _vault_generation(providers, key: str) -> Any:
    """The generation the vault holds for ``key`` now; None when it holds none or cannot be read."""
    try:
        vault = load_tokens(providers.state_dir, str((getattr(providers, 'secrets', None) or {}).get('CALENDAR_TOKEN_KEY') or ''))
    except (ProviderError, OSError, ValueError):
        return None
    held = vault.get(key) if isinstance(vault, dict) else None
    return held.get('generation') if isinstance(held, dict) else None


def _check_legacy(session, binding, entry, sent=False, *, key='', current=None):
    mark, attempted, withdrawn = binding
    state = session['host'].state()
    attempted_gen = attempted.get('legacy_generation')
    same_attempt_gen = not attempted_gen or not entry.get('generation') or entry.get('generation') == attempted_gen
    hw = host_withdrawn_state(session['store'].state_dir, key, entry, mark=mark, withdrawn=withdrawn)
    if not mark and state != ABSENT and same_attempt_gen and attempted.get('reason') == UNCONFIRMED:
        message = (f"Ouroboros cannot tell how this Google sign-in's transfer ended; sign in on {CARD} to restore the "
                   'account (writes waiting from before it are withdrawn, not sent).'
                   if _unresolvable(attempted, entry, withdrawn) else
                   f'Google sign-in transfer is not confirmed; writes wait. If it cannot be confirmed, sign in on {CARD}.')
    elif not mark and state != ABSENT and same_attempt_gen and attempted.get('reason') == 'account_disconnected':
        message = f'This Google account was disconnected on {CARD}; the old sign-in is not used.'
    elif mark and state != ABSENT:
        message = 'The Google sign-in has moved to Ouroboros; use its current connection instead of the old copy.'
    elif withdrawn_generation(withdrawn, entry) or hw == 'withdrawn':
        message = 'This Google account was disconnected; the old sign-in is not used.'
    elif hw == 'unknown':
        message = 'Could not confirm this Google sign-in against Ouroboros ledger (sign_in_withdrawn.json); waiting rather than using the old Google sign-in.'
    elif (not mark and state != ABSENT and current is not None and attempted.get('legacy_generation')
          and entry.get('generation') != attempted['legacy_generation']
          and not (entry.get('generation') and entry.get('generation') == current())):
        # A copy read before this account's generation changed (an older Providers, a cached adapter): the reservation
        # names another generation, so it serves only while the vault confirms the same non-empty generation.
        message = 'This Google sign-in was replaced by a newer one here; the earlier copy is not used.'
    else:
        return
    raise ProviderError('uncertain' if sent else 'retry', message, unsent=not sent)


def adapter_for(providers, key: str, cached=None):
    """Resolve current custody before choosing the host adapter, legacy adapter or a hold."""
    session = _session(providers)
    binding = _binding(session, key)
    mark = binding[0]
    entry = (getattr(providers, 'saved_accounts', {}) or {}).get(key) or {}
    if mark:
        state = session['host'].state()
        if (state != ABSENT and handover_pending(mark, entry) and not withdrawn_generation(binding[2], entry)
                and not _renewal_refused_before_exchange(binding[1], entry)):
            raise ProviderError('retry', (f"Ouroboros cannot tell how the move of this account's newer sign-in ended; sign in "
                                          f'again on {CARD} to continue (writes waiting from before it are withdrawn, not sent).')
                                if _unresolvable(binding[1], entry, binding[2]) else
                                'The account has a newer sign-in still moving to Ouroboros; its writes '
                                          'wait until the move is confirmed.')
        if state == READY:
            # Never hand a cached host access token across commands: each resolution starts fresh with the host.
            adapter = HostGoogleAdapter(key, mark, session['host'], store=session['store'])
            adapter._binding = mark
            return adapter
        if state != ABSENT:   # unknown whether this core holds it: never refresh a moved account here meanwhile
            raise ProviderError('network', 'The Ouroboros sign-in service did not answer; the Google sign-in it '
                                           'holds is kept and nothing is refreshed here.')
    def current():   # read only on the disputed path of _check_legacy
        return _vault_generation(providers, key)
    _check_legacy(session, binding, entry, key=key, current=current)
    if providers.vault_error:
        raise ProviderError('auth', providers.vault_error)
    if type(cached) is GoogleAdapter and cached._legacy_guard is not None:
        return cached
    adapter = GoogleAdapter.for_account(providers.secrets, providers.state_dir, key, tokens=providers.saved_accounts)
    if adapter is not None:
        attempted = binding[1]
        if (attempted.get('reason') == UNCONFIRMED and entry.get('generation')
                and entry['generation'] not in (attempted.get('legacy_generation'), attempted.get('fresh_generation'))):
            # A later local grant whose refresh is not shown to differ from the unconfirmed one keeps only its own access
            # token until it expires; that refresh is never used here (complete_google records a distinct one).
            adapter.refresh_token = ''
        # An adapter resolved just before reservation must not authorize a later legacy dispatch.
        adapter._legacy_guard = lambda sent: _check_legacy(session, _binding(session, key), entry, sent, key=key, current=current)
    return adapter


def sign_in_kind(providers) -> str:
    """``ouroboros`` when this Ouroboros holds Calendar's Google sign-ins, ``skill`` when an older core
    answered without them (its own path, as before), ``unreachable`` when no usable answer says which."""
    session = _session(providers)
    if session is None:
        return 'skill'
    state = session['host'].state()
    return {'ready': 'ouroboros', 'absent': 'skill'}.get(state, state)


def host_of(providers) -> HostSignIn:
    return _session(providers)['host']


def held_by_host(providers, account_id: str) -> bool:
    """Whether Ouroboros holds this Google account's sign-in: it moved there, and this core is not an older one
    (while the host does not answer, the card stays the place: a disconnect here would come back with it)."""
    session = _session(providers)
    prefix = PROVIDER_GOOGLE + ':'
    return bool(session and str(account_id).startswith(prefix) and _binding(session, str(account_id)[len(prefix):])[0]
                and session['host'].state() != ABSENT)


def on_card(account: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What a connect or disconnect here answers on an Ouroboros that holds the sign-ins."""
    who = f" of {account.get('login') or account['id']}" if account else ''
    return {'status': 'sign_in_on_card', **({'account_id': account['id']} if account else {}),
            'message': f'Ouroboros holds the Google sign-in{who}. Add an account, sign in again or disconnect on '
                       f'{CARD}, choosing the client (personal or IT-approved) for each account before Google '
                       'opens; Calendar follows it on its next background pass.'}


def worker_pass(store, secrets, state_dir, host: HostSignIn, handed_over, log):
    """Offer local grants at startup and notice new or late local consents on the next ordinary pass."""
    if not host.available(retry=True):
        return handed_over, None
    startup = (handed_over is None or handed_over.get('status') in ('unreachable', 'vault_unreadable')
               or (handed_over.get('status') == 'refused' and handed_over.get('http') == 429))
    result = handover(store, secrets, state_dir, host, retry_refused=startup)
    if startup or result['status'] != 'nothing':
        handed_over = result
        log('google sign-in handover', **{key: value for key, value in result.items() if key != 'message'})
    return handed_over, {'handover': handed_over, 'follow': follow(store, secrets, state_dir, host)}


def handover_pending(mark, entry) -> bool:
    """A later consent is present, but the host has not yet accounted for that generation."""
    return bool(mark and 'legacy_generation' in mark and entry.get('generation')
                and entry.get('provider', PROVIDER_GOOGLE) == PROVIDER_GOOGLE
                and entry['generation'] != mark['legacy_generation'])


def withdrawn_generation(record, entry) -> bool:
    if not record:
        return False
    if record.get('generation_unknown') is True:
        allowed = record.get('allowed_generation')
        return not (allowed and entry.get('generation') == allowed)
    return 'generation' in record and record['generation'] == entry.get('generation')


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ''


def _verified_connection_id(account: Dict[str, Any]) -> str:
    """Real core emits connection_id only for verified accounts; ignore any unverified value."""
    if 'verified' in account and account.get('verified') is not True:
        return ''
    return _text(account.get('connection_id'))


def _connection_replaced(mark: Dict[str, Any], account: Dict[str, Any]) -> bool:
    old_id, new_id = _text(mark.get('connection_id')), _verified_connection_id(account)
    return bool(old_id and new_id and old_id != new_id)


def _read_vault(state_dir, secrets):
    vault = load_tokens(state_dir, str(secrets.get('CALENDAR_TOKEN_KEY') or ''))
    if not isinstance(vault, dict) or not all(isinstance(row, dict) for row in vault.values()):
        raise ProviderError('auth', 'The saved Calendar grants could not be read.')
    return vault


def _build_grant(key, entry, mark, client_id):
    tokens = {name: entry[name] for name in ('refresh_token', 'access_token', 'scope') if entry.get(name)}
    expires = parse_stored(entry.get('expires_at'))
    if expires and tokens.get('access_token'):
        tokens['expires_at'] = expires.timestamp()
    label = str(entry.get('email') or key)
    grant = {'legacy_key': key, 'label': label, 'client_id': client_id, 'tokens': tokens}
    if entry.get('subject'):
        grant['subject'] = entry['subject']
    if entry.get('generation'):
        grant['legacy_generation'] = entry['generation']
    if mark:
        grant['previous_legacy_generation'] = mark['legacy_generation']
        prev_conn = _text(mark.get('connection_id'))
        if prev_conn:
            grant['connection_id'] = prev_conn
    return grant, label


def _handover_input(view, secrets, state_dir, retry_refused):
    vault = _read_vault(state_dir, secrets)
    moved, offered = view.get_setting(MOVED) or {}, set(view.get_setting(OFFERED) or [])
    withdrawn, refused = view.get_setting(WITHDRAWN) or {}, view.get_setting(REFUSED) or {}
    returns, clients, grants, recover_grants, fresh_primary = _return_hint(view), {}, [], [], {}
    ledger = _read_host_withdrawn(state_dir)   # one reading of core's disconnect ledger for this pass's entries
    if retry_refused:
        for prefix in ('GOOGLE_OAUTH', 'GOOGLE_CALENDAR'):
            client_id = str(secrets.get(prefix + '_CLIENT_ID') or '').strip()
            if client_id and client_id not in offered:
                clients[client_id] = _client(client_id, secrets.get(prefix + '_CLIENT_SECRET'), f'Calendar ({prefix}_CLIENT_ID)', returns)
    for key, entry in vault.items():
        if (entry.get('provider', PROVIDER_GOOGLE) != PROVIDER_GOOGLE
                or not (entry.get('refresh_token') or entry.get('access_token'))
                or withdrawn_generation(withdrawn.get(key), entry)
                or host_withdrawn_state(state_dir, key, entry, mark=moved.get(key), withdrawn=withdrawn.get(key),
                                        rows=ledger) != 'clear'):
            continue
        mark, subject, generation = moved.get(key), entry.get('subject'), entry.get('generation')
        prior = refused.get(key) or {}
        # A later local grant whose refresh complete_google showed to differ from the unconfirmed one makes its own ordinary
        # attempt; the unconfirmed one is never read back or sent again, and the core decides whether this one moves.
        fresh = bool(generation and prior.get('reason') == UNCONFIRMED and prior.get('fresh_generation') == generation
                     and prior.get('legacy_generation') != generation)
        if prior.get('reason') == UNCONFIRMED and not fresh:
            # Reconcile/resume a previously started identity check without submitting a second exchange.
            same_gen = prior.get('legacy_generation') == generation
            eligible = (bool(handover_pending(mark, entry) and subject and mark.get('subject') == subject)
                        if mark else not (subject and any(m.get('subject') == subject for m in moved.values())))
            if same_gen and eligible and (retry_refused or not prior.get('terminal')):
                client_id, _ = client_credentials(secrets, entry)
                if client_id:
                    grant, _ = _build_grant(key, entry, mark, client_id)
                    recover_grants.append(grant)
            continue
        if mark:
            if (not handover_pending(mark, entry) or not subject or mark.get('subject') != subject
                    or prior.get('legacy_generation') == generation):
                continue
        else:
            if subject and any(m.get('subject') == subject for m in moved.values()):
                continue
            same_refused = bool(prior) and ('legacy_generation' not in prior or prior.get('legacy_generation') == generation)
            if same_refused and not retry_refused:
                continue
        client_id, client_secret = client_credentials(secrets, entry)
        if not client_id:
            continue
        grant, label = _build_grant(key, entry, mark, client_id)
        if client_id not in offered:
            clients.setdefault(client_id, _client(client_id, client_secret, f'Calendar (signed in {label})', returns))
        # Reserve the first exchange too. The old grant cannot be used while custody is unknown.
        attempt_id = uuid.uuid4().hex
        refused[key] = {'reason': UNCONFIRMED, 'at': iso_utc(now_utc()), 'legacy_generation': generation,
                        **({'attempt': attempt_id} if not mark else {})}
        if not mark:
            fresh_primary[key] = attempt_id
        grants.append(grant)
    if grants:
        view.set_setting(REFUSED, refused)
    return clients, grants, recover_grants, fresh_primary


def _reserved(refused, grant):
    attempt = refused.get(grant['legacy_key']) or {}
    return attempt.get('reason') == UNCONFIRMED and attempt.get('legacy_generation') == grant.get('legacy_generation')


def handover(store, secrets: Dict[str, Any], state_dir: str, host: HostSignIn, *, retry_refused: bool = True) -> Dict[str, Any]:
    """Offer local grants once; reserve each exchange before I/O and confirm/recover its outcome separately."""
    try:
        with store.transaction() as view:
            clients, grants, recover_grants, fresh_primary = _handover_input(view, secrets, state_dir, retry_refused)
    except (ProviderError, OSError, ValueError):
        return {'status': 'vault_unreadable', 'message': 'The saved Calendar grants could not be read.'}
    if not clients and not grants and not recover_grants:
        return {'status': 'nothing'}
    counts: Dict[str, int] = {}
    if recover_grants:
        try:
            recovered = host.handover([], recover_grants, recover=True)
        except HostError as exc:
            if not clients and not grants:
                return {'status': 'refused', 'http': exc.status, 'code': str(exc.body.get('code') or '')}
            recovered = None
        except (OSError, ValueError) as exc:
            if not clients and not grants:
                return {'status': 'unreachable', 'error': type(exc).__name__}
            recovered = None
        if recovered is not None:
            if recovered.get('ok') is not True or not isinstance(recovered.get('grants'), list):
                if not clients and not grants:
                    return {'status': 'unreachable', 'error': 'incomplete_handover_receipt'}
            else:
                try:
                    with store.transaction() as view:
                        got = _record_handover(view, _read_vault(state_dir, secrets), recover_grants, recovered, recover=True)
                    for k, v in got.items():
                        counts[k] = counts.get(k, 0) + v
                except (ProviderError, OSError, ValueError):
                    return {'status': 'vault_unreadable', 'message': 'The saved Calendar grants could not be read.'}
    if not clients and not grants:
        return {'status': 'ok', 'results': counts}
    try:
        answer = host.handover(list(clients.values()), grants)
    except HostError as exc:
        # Only HTTP 429 with explicit handover_started=False clears this call's fresh primary own reservation.
        if grants and exc.status == 429 and exc.body.get('handover_started') is False:
            with store.transaction() as view:
                refused = view.get_setting(REFUSED) or {}
                for grant in grants:
                    key = grant['legacy_key']
                    expected_attempt = fresh_primary.get(key)
                    if (expected_attempt and _reserved(refused, grant)
                            and (refused.get(key) or {}).get('attempt') == expected_attempt):
                        refused.pop(key, None)
                view.set_setting(REFUSED, refused)
        return {'status': 'refused', 'http': exc.status, 'code': str(exc.body.get('code') or '')}
    except (OSError, ValueError) as exc:
        return {'status': 'unreachable', 'error': type(exc).__name__}
    if answer.get('ok') is not True or not isinstance(answer.get('grants'), list):
        return {'status': 'unreachable', 'error': 'incomplete_handover_receipt'}
    try:
        with store.transaction() as view:
            got = _record_handover(view, _read_vault(state_dir, secrets), grants, answer)
            view.set_setting(OFFERED, sorted(set(view.get_setting(OFFERED) or []) | set(clients)))
        for k, v in got.items():
            counts[k] = counts.get(k, 0) + v
    except (ProviderError, OSError, ValueError):
        return {'status': 'vault_unreadable', 'message': 'The saved Calendar grants could not be read.'}
    return {'status': 'ok', 'results': counts}


def _record_handover(view, fresh, grants, answer, *, recover: bool = False):
    marks, refused = view.get_setting(MOVED) or {}, view.get_setting(REFUSED) or {}
    withdrawn, identities = view.get_setting(WITHDRAWN) or {}, view.get_setting(IDENTITIES) or {}
    counts, at = {}, iso_utc(now_utc())
    for grant, result, host_key, reason, label, echoed, via, connection_id, exchange in _outcomes(answer, grants):
        key, generation = grant['legacy_key'], grant.get('legacy_generation')
        renewal = 'previous_legacy_generation' in grant
        entry, record, mark = fresh.get(key), withdrawn.get(key), marks.get(key)
        if (renewal or echoed is not None) and echoed != generation and result != 'refused':
            result, reason = '', UNCONFIRMED
        counts[result or 'unanswered'] = counts.get(result or 'unanswered', 0) + 1
        if not _reserved(refused, grant):
            continue  # a confirmed read or a later binding has already settled this reservation
        if result in KEPT and host_key and (entry is None or withdrawn_generation(record, entry)):
            refused[key] = {'reason': 'withdrawn_during_handover' if record is not None else 'local_copy_gone', 'at': at}
            if record is not None:
                record['host_account'] = host_key
                if connection_id:
                    record['host_connection_id'] = connection_id
            continue
        if renewal:
            # A completed follow, a withdrawal or another binding has superseded this receipt.
            if (not mark or mark.get('legacy_generation') != grant['previous_legacy_generation']
                    or (host_key and host_key != mark.get('account'))):
                continue
            if _connection_replaced(mark, {'connection_id': connection_id}):
                # Never overwrite two distinct known connection_ids silently; let follow() withdraw old intents first.
                refused.pop(key, None)
                continue
        elif mark:
            continue
        if result not in KEPT or not host_key:
            # Neither reason nor HTTP proves no exchange: only explicit exchange=='none' for the same generation
            # without disconnect returns the old local right.
            same_refusal_gen = echoed is None or echoed == generation
            if reason == 'account_disconnected':
                refused[key] = {'reason': 'account_disconnected', 'at': at, 'legacy_generation': generation}
            elif not recover and result == 'refused' and exchange == 'none' and same_refusal_gen:
                refused[key] = {'reason': reason or 'refused', 'at': at, 'legacy_generation': generation, 'exchange': 'none'}
            elif result == 'refused' and exchange == 'saved':
                refused[key] = {'reason': UNCONFIRMED, 'at': at, 'legacy_generation': generation, 'exchange': 'saved'}
            elif result == 'refused' and exchange == 'unknown':
                refused[key] = {'reason': UNCONFIRMED, 'at': at, 'legacy_generation': generation,
                                'exchange': 'unknown', 'terminal': True}
            continue
        if entry.get('generation') != generation and (not grant.get('subject') or entry.get('subject') != grant['subject']):
            refused[key] = {'reason': 'superseded_locally', 'at': at}
            continue
        # Queue continuity requires confirmed same connection_id or confirmed handover of the exact generation,
        # never via=card, kept_newer without proof, or an absent older connection_id/generation.
        same_echo = bool(generation and echoed == generation)
        if renewal:
            old_conn = _text(mark.get('connection_id'))
            continuous = (bool(old_conn and connection_id and old_conn == connection_id)
                          or (not old_conn and via == 'handover' and same_echo
                              and (result == 'imported' or (not connection_id and result == 'already_present'))))
        else:
            continuous = (via == 'handover' and result in ('imported', 'already_present')
                          and (same_echo or (not generation and result == 'imported')))
        if not continuous:
            view.cancel_intents_for_account(PROVIDER_GOOGLE + ':' + key, reason=UNCONFIRMED_CONTINUITY)
        # A newer local consent of the same subject may already exist. Record only the offered generation.
        marks[key] = {'account': host_key, 'label': label or grant['label'], 'subject': grant.get('subject'),
                      'scope': str(grant['tokens'].get('scope') or ''), 'via': via, 'result': result,
                      'at': at, 'legacy_generation': generation,
                      **({'connection_id': connection_id} if connection_id else {})}
        refused.pop(key, None)
        withdrawn.pop(key, None)
        if grant.get('subject'):
            remember_subject(identities, str(grant['subject']), key, at=at)
        if renewal:
            view.set_setting('sync_requested_at', at)
    view.set_setting(MOVED, marks)
    view.set_setting(REFUSED, refused)
    view.set_setting(WITHDRAWN, withdrawn)
    view.set_setting(IDENTITIES, identities)
    return counts


def follow(store, secrets: Dict[str, Any], state_dir: str, host: HostSignIn) -> Dict[str, Any]:
    """Follow the accounts the host lists for Calendar. Decide every row's applicability and queue fate
    before mutating anything, then commit bindings, withdrawals and intent cancellations in one transaction."""
    previous = store.get_setting(MOVED) or {}
    try:
        listed = host.accounts()
    except (HostError, OSError, ValueError):
        return {'status': 'unreachable'}
    if listed is None:
        return {'status': 'undeclared'}
    held = {account['key']: account for account in listed}
    for mark in previous.values():
        subject = _verified_subject(held.get(mark.get('account')) or {})
        if subject and mark.get('subject') and subject != mark['subject']:
            return {'status': 'unreachable'}  # do not apply an inconsistent identity projection
    passphrase = str(secrets.get('CALENDAR_TOKEN_KEY') or '')
    added, withdrawn_count, vault_cleanups = [], 0, []
    with store.transaction() as view:
        try:
            vault = _read_vault(state_dir, secrets)
        except (ProviderError, OSError, ValueError):
            vault = None
        marks, refused = view.get_setting(MOVED) or {}, view.get_setting(REFUSED) or {}
        withdrawn = view.get_setting(WITHDRAWN) or {}
        identities = view.get_setting(IDENTITIES) or {}
        for mark in marks.values():
            subject = _verified_subject(held.get(mark.get('account')) or {})
            if subject and mark.get('subject') and subject != mark['subject']:
                return {'status': 'unreachable'}  # do not apply an inconsistent identity projection
        # Phase 1: decide applicability, replacements, rebinds and queue fate without mutating DB or marks.
        clear_suppression = set()
        for key, record in withdrawn.items():
            host_account = str(record.get('host_account') or '')
            if not host_account:
                continue
            current = held.get(host_account)
            current_conn = _verified_connection_id(current) if current else ''
            if current is None or (record.get('host_connection_id') and current_conn
                                   and current_conn != _text(record.get('host_connection_id'))):
                clear_suppression.add(key)
        suppressed = {str(rec['host_account']) for k, rec in withdrawn.items()
                      if rec.get('host_account') and k not in clear_suppression}
        stale_keys, stale_hosts, replaced = set(), set(), []
        for key, mark in previous.items():
            current = held.get(mark.get('account'))
            if current and _connection_replaced(mark, current):
                if marks.get(key) == mark:
                    replaced.append((key, mark))
                else:
                    stale_keys.add(key)
        missing_with_conn = {key for key, mark in previous.items()
                             if mark.get('account') not in held and _text(mark.get('connection_id'))}
        if missing_with_conn:
            bound_prev = {str(mark.get('account')) for mark in previous.values()}
            for host_key, account in held.items():
                if host_key in bound_prev or account.get('state') != 'signed_in':
                    continue
                subject = _verified_subject(account)
                if not subject:
                    continue
                key = resolve_calendar_key(
                    view, subject=subject, label='', vault=vault, marks=previous,
                    identities=identities, withdrawn={}, refused={}, allow_bound=True, fallback_key=None,
                )
                if key in missing_with_conn and _connection_replaced(previous[key], account):
                    if marks.get(key) == previous[key]:
                        replaced.append((key, previous[key]))
                    else:
                        stale_keys.add(key)
                        stale_hosts.add(host_key)
                    missing_with_conn.discard(key)
        replaced_keys = {key for key, _ in replaced}
        effective_marks = {key: mark for key, mark in marks.items() if key not in replaced_keys}
        projected_identities = {s: dict(rec) for s, rec in identities.items()}
        for key, mark in replaced:
            subj = ((vault or {}).get(key) or {}).get('subject') or mark.get('subject')
            if subj:
                remember_subject(projected_identities, str(subj), key)
        # Decide host-key rebinds (provisional or disappeared host key -> verified subject's new host key).
        bound_hosts = {str(mark.get('account')) for mark in effective_marks.values()}
        plan_rebinds = {}   # host_key -> (key, updated_mark, cancel_queue)
        rebound_keys = set()
        for host_key, account in held.items():
            if host_key in bound_hosts or host_key in stale_hosts or host_key in suppressed or account.get('state') != 'signed_in':
                continue
            subject = _verified_subject(account)
            if not subject:
                continue
            candidates = [k for k in _subject_keys(subject, vault, effective_marks, projected_identities, include_marks=True)
                          if k in effective_marks and k not in rebound_keys]
            if len(candidates) != 1:
                continue
            key = candidates[0]
            mark = effective_marks[key]
            if key in stale_keys or mark != previous.get(key) or mark.get('subject') not in (None, '', subject):
                continue
            prior = held.get(str(mark.get('account') or ''))
            if not ((prior is not None and _provisional(prior)) or (prior is None and not _connection_replaced(mark, account))):
                continue
            entry = (vault or {}).get(key) or {}
            base = dict(mark)
            if (handover_pending(mark, entry) and not withdrawn_generation(withdrawn.get(key), entry)
                    and not _renewal_refused_before_exchange(refused.get(key), entry)):
                proof = _generation_proof(mark, entry, account, refused.get(key) or {})
                if proof is None:
                    continue  # row not applicable yet: leave mark and intents untouched
                base.update(proof)
            elif vault is None and ((refused.get(key) or {}).get('legacy_generation')):
                continue
            via, connection_id = _text(account.get('via')), _verified_connection_id(account)
            continuous = _continuous_authorization(base, account)
            updated = {**base, 'account': host_key, 'label': str(account.get('label') or base.get('label') or key),
                       'subject': subject or base.get('subject'),
                       'scope': str(account.get('scope') or base.get('scope') or ''),
                       'via': via or base.get('via') or ''}
            if connection_id:
                updated['connection_id'] = connection_id
            else:
                updated.pop('connection_id', None)
            plan_rebinds[host_key] = (key, updated, not continuous, subject)
            bound_hosts.add(host_key)
            rebound_keys.add(key)
        # Decide ordinary rows (unchanged host keys, newly added host accounts, confirmed first handovers).
        mapped = {str(mark.get('account')): key for key, mark in effective_marks.items() if key not in rebound_keys}
        first = {key: attempt for key, attempt in refused.items()
                 if key not in effective_marks and attempt.get('reason') == UNCONFIRMED}
        plan_apply = []            # list of (key, updated_mark, cancel_queue, subject, triggers_rebound)
        plan_withdrawn_host = []   # list of (key, host_key, connection_id)
        claimed_keys = set(rebound_keys)
        for host_key, account in held.items():
            if host_key in stale_hosts or host_key in plan_rebinds:
                continue
            via, connection_id, subject = _text(account.get('via')), _verified_connection_id(account), _verified_subject(account)
            key = mapped.get(host_key)
            if key is None:
                if _provisional(account) or host_key in suppressed:
                    continue
                generation = account.get('legacy_generation')
                imported = via == 'handover'
                candidates = [local for local, attempt in first.items()
                              if imported and generation and generation == attempt.get('legacy_generation')]
                if len(candidates) > 1:
                    continue
                if candidates:
                    if vault is None:
                        continue
                    key = candidates[0]
                else:
                    key = resolve_calendar_key(
                        view, subject=subject, label=str(account.get('label') or ''), vault=vault,
                        marks={**effective_marks, **{k: {} for k in claimed_keys}},
                        identities=projected_identities, withdrawn=withdrawn, refused=refused,
                        allow_bound=False, fallback_key='host:' + host_key,
                    )
                    if key is None:
                        continue
                if imported and not generation and key not in (vault or {}) and key not in first and any(
                        not attempt.get('legacy_generation') for attempt in first.values()):
                    continue  # an untagged import cannot be linked to an unresolved old grant
            if key in stale_keys or key in claimed_keys:
                continue
            existing, prev = effective_marks.get(key), previous.get(key)
            if existing is not None and existing != prev:
                continue  # a newer pass already changed this mark while our listing was in flight
            if existing is None and prev is not None and key not in replaced_keys:
                if str(prev.get('account') or '') == host_key and not _connection_replaced(prev, account):
                    continue  # a concurrent withdrawal removed this binding while our listing was in flight
            mark = dict(existing) if existing is not None else {'at': iso_utc(now_utc())}
            if existing is not None and _connection_replaced(mark, account):
                continue
            entry = (vault or {}).get(key) or {}
            cancel_queue, triggers_rebound = False, False
            if key in first:
                if vault is None:
                    continue
                attempted = first[key]
                if via == 'handover' and withdrawn_generation(
                        withdrawn.get(key), {'generation': attempted.get('legacy_generation')}):
                    plan_withdrawn_host.append((key, host_key, connection_id))
                    continue
                local_entry = ({**entry, 'subject': subject}
                               if not entry.get('subject') and (projected_identities.get(subject) or {}).get('key') == key else entry)
                # A listing row never attributes or clears an unconfirmed handover by via=card + subject alone. Once its
                # outcome can no longer be learnt (or a replaced connection ended the renewal it guarded), the owner's own
                # card consent takes the account as a new connection and the queue before it is withdrawn. An account
                # disconnected here first keeps out the card it then sees, until that card is gone or replaced.
                proof = _generation_proof(None, local_entry, account, attempted)
                if proof is not None:
                    mark.update(proof)
                elif not (_card_consent(account, local_entry.get('subject'))
                          and (key in replaced_keys or _unresolvable(attempted, entry, withdrawn.get(key)))):
                    continue
                elif key not in replaced_keys and key in withdrawn and 'host_account' not in withdrawn[key]:
                    plan_withdrawn_host.append((key, host_key, connection_id))
                    continue
                else:
                    cancel_queue = True
                triggers_rebound = True
            elif (existing is not None and handover_pending(mark, entry) and not withdrawn_generation(withdrawn.get(key), entry)
                  and not _renewal_refused_before_exchange(refused.get(key), entry)):
                proof = _generation_proof(existing, entry, account, refused.get(key) or {})
                if proof is not None:
                    mark.update(proof)
                elif _unresolvable(refused.get(key), entry, withdrawn.get(key)) and _card_consent(account, mark.get('subject')):
                    # The owner signed in again on the card after the renewal's move became unknowable: that consent ends
                    # the renewal, which is never recorded as moved, and the queue before it is withdrawn.
                    mark.pop('legacy_generation', None)
                    cancel_queue = True
                else:
                    continue  # first read of another consent never proves a pending local handover
                triggers_rebound = True
            elif vault is None and ((refused.get(key) or {}).get('legacy_generation')):
                continue  # cannot clear a recorded exchange using an unreadable local projection
            elif existing is None and key not in replaced_keys and (view.get_account(PROVIDER_GOOGLE + ':' + key) is not None or key in (vault or {})):
                # Newly binding an existing local account: only a confirmed handover of its exact generation carries queued writes.
                e_gen, a_gen = _text(entry.get('generation')), _text(account.get('legacy_generation'))
                same_gen = bool(e_gen and a_gen and a_gen == e_gen)
                if not (via == 'handover' and same_gen):
                    cancel_queue = True
            if existing is not None and not cancel_queue:
                old_conn = _text(mark.get('connection_id'))
                if old_conn:
                    if not connection_id and not _continuous_authorization({**mark, 'connection_id': ''}, account):
                        cancel_queue = True
                elif connection_id:
                    if not _continuous_authorization(mark, account):
                        cancel_queue = True
                else:
                    old_via = _text(mark.get('via'))
                    old_gen, new_gen = _text(mark.get('legacy_generation')), _text(account.get('legacy_generation'))
                    if (old_via and via and via != old_via) or ((old_gen or new_gen) and old_gen != new_gen):
                        cancel_queue = True
            updated = {**mark, 'account': host_key, 'label': str(account.get('label') or mark.get('label') or key),
                       'subject': subject or mark.get('subject'),
                       'scope': str(account.get('scope') or mark.get('scope') or ''),
                       'via': via or mark.get('via') or ''}
            if connection_id:
                updated['connection_id'] = connection_id
            else:
                updated.pop('connection_id', None)
            claimed_keys.add(key)
            plan_apply.append((key, updated, cancel_queue, subject, triggers_rebound))
        # Decide gone bindings (missing from host and not replaced or rebound).
        gone = [(key, mark) for key, mark in effective_marks.items()
                if key not in rebound_keys and mark.get('account') not in held
                and mark == previous.get(key)
                and (vault is None or not handover_pending(mark, vault.get(key) or {})
                     or withdrawn_generation(withdrawn.get(key), vault.get(key) or {})
                     or _renewal_refused_before_exchange(refused.get(key), vault.get(key) or {})
                     or (refused.get(key) or {}).get('reason') == 'account_disconnected')]
        # Phase 2: commit withdrawals, bindings and queue fates atomically inside this one transaction.
        for key, mark in replaced:
            ok, cleanup = _withdraw_in_tx(view, state_dir, passphrase, key, mark['account'], expect_mark=mark)
            if ok:
                withdrawn_count += 1
                if cleanup:
                    vault_cleanups.append(cleanup)
        if replaced:
            marks = view.get_setting(MOVED) or {}
            withdrawn = view.get_setting(WITHDRAWN) or {}
            identities = view.get_setting(IDENTITIES) or {}
        before_commit = json.dumps([marks, refused, withdrawn, identities], sort_keys=True)
        for key in clear_suppression:
            record = withdrawn.get(key)
            if record and record.get('host_account'):
                record['host_account'] = None
                record.pop('host_connection_id', None)
        rebound = False
        planned = [(key, updated, cancel_queue, subject, True)
                   for key, updated, cancel_queue, subject in plan_rebinds.values()] + plan_apply
        for key, updated, cancel_queue, subject, triggers_rebound in planned:
            if cancel_queue:
                view.cancel_intents_for_account(PROVIDER_GOOGLE + ':' + key, reason=UNCONFIRMED_CONTINUITY)
            if triggers_rebound:
                rebound = True
            marks[key] = updated
            if (not _renewal_refused_before_exchange(refused.get(key), (vault or {}).get(key) or {})
                    or updated.get('legacy_generation') == (refused.get(key) or {}).get('legacy_generation')):
                refused.pop(key, None)
            record = withdrawn.get(key)
            if record is not None:
                local_blocked = (vault is None or record.get('generation_unknown') is True
                                 or (key in vault and withdrawn_generation(record, vault[key])))
                if not local_blocked:
                    withdrawn.pop(key, None)
                elif record.get('host_account'):
                    record['host_account'] = None
                    record.pop('host_connection_id', None)
            if subject:
                remember_subject(identities, str(subject), key)
            if view.get_account(PROVIDER_GOOGLE + ':' + key) is None:
                view.upsert_account({'id': PROVIDER_GOOGLE + ':' + key, 'provider': PROVIDER_GOOGLE, 'login': updated['label'],
                                     'alias': updated['label'], 'status': 'connected'})
                added.append(key)
        for key, host_key, connection_id in plan_withdrawn_host:
            if key in withdrawn:
                withdrawn[key]['host_account'] = host_key
                if connection_id:
                    withdrawn[key]['host_connection_id'] = connection_id
        if json.dumps([marks, refused, withdrawn, identities], sort_keys=True) != before_commit:
            view.set_setting(MOVED, marks)
            view.set_setting(REFUSED, refused)
            view.set_setting(WITHDRAWN, withdrawn)
            view.set_setting(IDENTITIES, identities)
        if added or rebound:
            view.set_setting('sync_requested_at', iso_utc(now_utc()))
        for key, mark in gone:
            ok, cleanup = _withdraw_in_tx(view, state_dir, passphrase, key, mark['account'], expect_mark=mark)
            if ok:
                withdrawn_count += 1
                if cleanup:
                    vault_cleanups.append(cleanup)
    # Phase 3: drop withdrawn local vault entries only after the SQLite transaction committed.
    if vault_cleanups:
        import providers_google as gp
        for key, generation in vault_cleanups:
            gp.forget_account(state_dir, passphrase, key, expected_generation=generation)
    return {'status': 'ok', 'added': len(added), 'withdrawn': withdrawn_count}


def _generation_proof(mark, entry, account, attempted):
    """Only a confirmed handover listing carrying the matching legacy generation proves a transfer.
    Reading an unrelated consent (such as via=card) never proves an old handover."""
    subject = _verified_subject(account)
    local_subject = entry.get('subject')
    if not subject or (local_subject and subject != local_subject):
        return None
    if mark is not None and (subject != mark.get('subject') or subject != local_subject):
        return None
    via, generation = _text(account.get('via')), account.get('legacy_generation')
    if via == 'handover' and generation:
        matches = (generation == attempted.get('legacy_generation') if mark is None else
                   generation != mark['legacy_generation'] and generation in (entry.get('generation'), attempted.get('legacy_generation')))
        if matches:
            return {'legacy_generation': generation, 'via': 'handover', 'result': 'observed_filed'}
    return None


def _forget_in_tx(view, key: str, expect_account: Optional[str] = None, *, expect_mark=None) -> bool:
    marks = view.get_setting(MOVED) or {}
    mark = marks.get(key)
    if (mark is None or (expect_account is not None and str(mark.get('account')) != str(expect_account))
            or (expect_mark is not None and mark != expect_mark)):
        return False
    marks.pop(key)
    view.set_setting(MOVED, marks)
    if mark.get('subject'):
        identities = view.get_setting(IDENTITIES) or {}
        if remember_subject(identities, str(mark['subject']), key):
            view.set_setting(IDENTITIES, identities)
    return True


def forget(store, key: str, expect_account: Optional[str] = None, *, expect_mark=None) -> bool:
    """Drop the record that Ouroboros holds account ``key`` (its disconnect here, or on the card).
    With ``expect_account`` the mark must still map to that exact host key: a rebound binding is kept."""
    with store.transaction() as view:
        return _forget_in_tx(view, key, expect_account=expect_account, expect_mark=expect_mark)


def _withdraw_in_tx(view, state_dir, passphrase, key, host_key: str, *, expect_mark=None):
    """Withdraw only the binding the complete listing showed as gone, inside the caller's transaction."""
    from accounts import withdraw_account_in_tx
    account = view.get_account(PROVIDER_GOOGLE + ':' + key)
    if account:
        done, cleanup = withdraw_account_in_tx(view, state_dir, passphrase, account,
                                               expect_host_key=host_key, expect_host_mark=expect_mark)
        return done.get('status') not in ('rebound', 'unreachable'), cleanup
    return _forget_in_tx(view, key, expect_account=host_key, expect_mark=expect_mark), None


def _session(providers) -> Optional[Dict[str, Any]]:
    """Reuse a host client and a store, not a historical snapshot of credential custody."""
    state_dir = getattr(providers, 'state_dir', None)
    if not state_dir:
        return None
    session = getattr(providers, 'google_host', None)
    if session is None:
        session = providers.google_host = {'host': HostSignIn()}
    if 'store' not in session:
        from store import Store
        session['store'] = Store(state_dir)
    return session


def _verified_subject(account: Dict[str, Any]) -> str:
    subject = account.get('subject')
    return subject if account.get('verified') is True and isinstance(subject, str) else ''


def _provisional(account: Dict[str, Any]) -> bool:
    return account.get('verified') is False and account.get('state') == 'sign_in_required'


def remember_subject(identities: Dict[str, Any], subject: str, key: str, *, at: Optional[str] = None) -> bool:
    """Record a verified subject's Calendar key once; ordinary re-consent never rewrites an existing binding."""
    subject, key = _text(subject), _text(key)
    if not subject or not key or _text((identities.get(subject) or {}).get('key')):
        return False
    identities[subject] = {'key': key, 'at': at or iso_utc(now_utc())}
    return True


def _subject_keys(subject: str, vault: Optional[Dict[str, Any]], marks: Dict[str, Any],
                  identities: Dict[str, Any], *, include_marks: bool = True) -> set:
    if not subject:
        return set()
    keys = set()
    known = _text((identities.get(subject) or {}).get('key'))
    if known:
        keys.add(known)
    if include_marks:
        keys.update(key for key, mark in marks.items() if mark.get('subject') == subject)
    if vault is not None:
        # finish_auth records sub from verified Google userinfo, never from the login or calendar label.
        keys.update(key for key, entry in vault.items()
                    if entry.get('provider', PROVIDER_GOOGLE) == PROVIDER_GOOGLE and entry.get('subject') == subject)
    return keys


def _key_free(store, key: str, vault: Optional[Dict[str, Any]], marks: Dict[str, Any],
              identities: Dict[str, Any], withdrawn: Dict[str, Any], refused: Dict[str, Any],
              *, subject: str = '') -> bool:
    if not key or vault is None or key in marks or key in vault or key in withdrawn or key in refused:
        return False
    if any(other != subject and _text((rec or {}).get('key')) == key for other, rec in identities.items()):
        return False
    account_id = PROVIDER_GOOGLE + ':' + key
    return (store.get_account(account_id) is None
            and not store.list_calendars(include_deleted=True, account_id=account_id))


def resolve_calendar_key(store, *, subject: str, label: str, vault: Optional[Dict[str, Any]],
                         marks: Dict[str, Any], identities: Dict[str, Any], withdrawn: Dict[str, Any],
                         refused: Dict[str, Any], allow_bound: bool = False,
                         fallback_key: Optional[str] = None) -> Optional[str]:
    """Single owner of verified Google subject ↔ Calendar key resolution.
    A verified subject keeps its recorded historical key; a display name or email only names an
    unoccupied fresh key and never joins different or unverified subjects."""
    subject, label = _text(subject), _text(label)
    if subject:
        known = _text((identities.get(subject) or {}).get('key'))
        if known:
            mark_ok = ((allow_bound or known not in marks)
                       and (not marks.get(known) or marks[known].get('subject') in (None, '', subject)))
            vault_ok = vault is None or known not in vault or vault[known].get('subject') in (None, '', subject)
            if mark_ok and vault_ok:
                return known
        matches = _subject_keys(subject, vault, marks, identities, include_marks=allow_bound)
        if len(matches) == 1:
            key = next(iter(matches))
            claimed_other = any(other != subject and _text((rec or {}).get('key')) == key for other, rec in identities.items())
            mark_ok = ((allow_bound or key not in marks)
                       and (not marks.get(key) or marks[key].get('subject') in (None, '', subject)))
            vault_ok = vault is None or key not in vault or vault[key].get('subject') == subject
            if not claimed_other and mark_ok and vault_ok:
                return key
        if known or matches:
            return fallback_key
    if vault is None:
        return None
    for candidate in (label, 'oidc:' + subject if subject else ''):
        if _key_free(store, candidate, vault, marks, identities, withdrawn, refused, subject=subject):
            return candidate
    return fallback_key


def _continuous_authorization(mark: Dict[str, Any], account: Dict[str, Any]) -> bool:
    """True only when host facts confirm the same connection or the same handed-over generation.
    Never inferred from email, via=card, subject match alone, or unverified imports (which have no connection_id)."""
    old_conn, new_conn = _text(mark.get('connection_id')), _verified_connection_id(account)
    if old_conn:
        return bool(new_conn and old_conn == new_conn)
    old_gen, new_gen = _text(mark.get('legacy_generation')), _text(account.get('legacy_generation'))
    return bool(old_gen and _text(mark.get('via')) in ('handover', '') and mark.get('result') != 'kept_newer'
                and _text(account.get('via')) == 'handover' and new_gen == old_gen)


def legacy_return_key(store, vault, subject, email, *, on_card: bool = True):
    """Select a reuse by verified provider subject; the email may only name an unoccupied new key."""
    marks, identities = store.get_setting(MOVED) or {}, store.get_setting(IDENTITIES) or {}
    withdrawn, refused = store.get_setting(WITHDRAWN) or {}, store.get_setting(REFUSED) or {}
    key = resolve_calendar_key(
        store, subject=subject or '', label=email or '', vault=vault,
        marks=marks, identities=identities, withdrawn=withdrawn, refused=refused,
        allow_bound=True, fallback_key=None,
    )
    if key:
        return key
    if not on_card:
        # On an older core there is no card path: never permanently refuse an unknown identity, keep separate calendars.
        return 'account:' + uuid.uuid4().hex[:12]
    raise ProviderError('auth', f'Calendar could not establish which local account to keep. Sign in on {CARD}.')


def _outcomes(answer: Dict[str, Any], grants) -> Iterator[Tuple[Dict[str, Any], str, str, str, str, Optional[str], str, str, str]]:
    """Pair each grant with its receipt, including generation, provenance, connection_id and exchange state."""
    items = {str(item.get('legacy_key')): item for item in answer.get('grants') or [] if isinstance(item, dict)}
    for grant in grants:
        item = items.get(grant['legacy_key']) or {}
        result, reason = str(item.get('result') or ''), str(item.get('reason') or '')
        if result.startswith('refused(') and result.endswith(')'):   # also accept the legacy spelling refused(<reason>)
            result, reason = 'refused', reason or result[len('refused('):-1]
        account = item.get('account') if isinstance(item.get('account'), dict) else {}
        yield (grant, result, str(account.get('key') or ''), reason, str(account.get('label') or ''),
               item.get('legacy_generation'), _text(item.get('via')), _verified_connection_id(item),
               _text(item.get('exchange')))


def _client(client_id, secret, label_hint, return_hint) -> Dict[str, Any]:
    return {'client_id': client_id, **({'client_secret': str(secret)} if secret else {}),
            'label_hint': label_hint, 'return_hint': return_hint}


def _return_hint(store) -> str:
    """A remote installation's saved address means a Web client registered for it; otherwise this computer."""
    base = str(store.get_setting('public_base_url') or '').rstrip('/')
    return 'exact:' + base + RETURN_PAGE if base else 'loopback'


def _connection_changed(unsent: bool, *, disconnected: bool = False) -> ProviderError:
    lead = (f'This Google sign-in changed or was disconnected on {CARD}; ' if disconnected else
            f'This Google sign-in was replaced by a new connection on {CARD}; ')
    tail = 'this operation was not sent.' if unsent else 'a later step could not be authorized; compare before retrying.'
    return ProviderError('auth', lead + tail, 409, unsent=unsent, reason='connection_changed')


def _refusal(exc: HostError, unsent: bool = True) -> ProviderError:
    body = exc.body or {}
    reason = _text(body.get('reason'))
    if exc.status == 409:   # SIGN_IN_REQUIRED: the host's sentence for the mind, then the skill's note for the owner
        words = ' '.join(str(body.get(name) or '').strip() for name in ('hint', 'note') if body.get(name))
        if reason == 'connection_changed' and not words:
            return _connection_changed(unsent)
        fallback = f"Ouroboros needs the owner's Google sign-in again on {CARD} ({reason or 'sign_in_required'})."
        return ProviderError('auth', words or fallback, 409, unsent=unsent, reason=reason)
    if exc.status == 404:
        return ProviderError('auth', f'Ouroboros holds no Google sign-in for this account any more; add it on {CARD}.', 404,
                             unsent=unsent, reason=reason or 'unknown_account')
    if exc.status == 503:
        return ProviderError('server', 'Ouroboros could not reach Google to refresh this sign-in '
                                       f"({reason or 'provider_unreachable'}); the sign-in is kept.", 503,
                             unsent=unsent, reason=reason or 'provider_unreachable')
    if exc.status == 403:
        return ProviderError('server', "Ouroboros refused this skill's token (the skill is off, or its review is not "
                                       'current for this version); nothing was refreshed here.', 403, unsent=unsent)
    return ProviderError('server', f'The Ouroboros sign-in service refused the call (HTTP {exc.status}).', exc.status, unsent=unsent)


def _expiry(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), timezone.utc)
    parsed = parse_stored(value) if isinstance(value, str) else None
    return parsed or now_utc() + timedelta(days=365)   # no stated expiry: used until Google refuses it, then asked again
