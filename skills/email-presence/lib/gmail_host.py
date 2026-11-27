"""Mail's transition to host-owned Google consent. The companion is the only handover writer.

The account registry owns local ID and mail history. The host owns verified subject,
grant availability and scope. A handover is reserved before I/O; a lost receipt is
reconciled by reading the host's list and by asking it, with `recover`, for the outcome
it kept under the reserved account and generation, never by repeating an exchange. Only
the host's per-item `exchange` word, not a reason or a status code, lets the old path go on;
a request its rate limit turned away unread (`handover_started: false`), or one that never
reached it, changed nothing. A change is made under the access named when it began
(`queued_link`); a queued mail goes out under whichever confirmed sign-in of its mailbox's own
Google account serves it then (`sending_link`).
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from email.errors import HeaderParseError
from email.headerregistry import Address
from urllib.parse import urlsplit

import httpx

from .accounts import AccountError, bearer_mark
from .google_auth import FULL_MAIL_SCOPE, MAIL_SCOPES
from .host_adapter import HostContractError, _SkillToken, _loopback

SERVICE = 'google'
CARD = 'the email-presence card in Extensions › Skills'  # its technical name: the catalog's title for it may not be what is drawn
_HOST_KEY = re.compile(r'a-[0-9a-f]{20}')
_STATES = {'signed_in', 'sign_in_required'}
_KEPT = {'imported', 'already_present', 'kept_newer'}
_NOT_STARTED = 'not_started'  # an offer that exchanged nothing (_proven_unstarted)
_REFRESH_REJECTED = 'refresh_rejected'  # a final refusal, not permission to use or re-offer the saved grant
_PAUSE_LONGEST = 900  # seconds, at most, between one reserved transfer's recover requests (see _lookup)
_OFFER_LONGEST = 3600  # seconds, at most, before a transfer that started nothing is offered again (see sync_gmail)
_CHANGE_VALID = 30  # minimum bearer lifetime requested for a bound change, in seconds (the Gmail request timeout)
_SHORT_LIVED = ('The Google sign-in host could not provide an access token that stays valid for this change; '
                'nothing was sent or changed.')


class SignInError(AccountError):
    @property
    def mail_diagnostic(self):
        return str(self)  # All text is skill-authored, never a transport exception or a provider response.


class HandoverNotStarted(SignInError):
    """The host turned the handover away unread, or the connection was not made. That request changed nothing."""


class HostNotReached(SignInError):
    """A loopback request to the host never connected (a connect or pool failure): nothing reached it, so nothing was
    asked or changed there. Every reader treats it as any SignInError; only a delivery tells it apart."""


def _loopback_base(url):
    try:
        parts = urlsplit(url)
        return (_loopback(url) and parts.port != 0 and parts.path in ('', '/')
                and not parts.query and not parts.fragment and '\\' not in url
                and not any(char.isspace() or ord(char) < 32 for char in url))
    except ValueError:
        return False


def _host_key(value):
    return isinstance(value, str) and _HOST_KEY.fullmatch(value) is not None


def _consent(value):
    """The host names a consent with a nonempty opaque string."""
    return isinstance(value, str) and bool(value)


def _refresh_rejected(receipt):
    return (isinstance(receipt, dict) and receipt.get('result') == 'refused' and receipt.get('account') is None
            and receipt.get('reason') == _REFRESH_REJECTED and receipt.get('exchange') == 'unknown')


def mail_scope(scopes):
    return MAIL_SCOPES[-1] in scopes or FULL_MAIL_SCOPE in scopes


def bearer_scope(tokens):
    """The permissions Google named for exactly this saved bearer, or None: its `scope` counts only beside the mark of
    that very access token (`scope_of`), never one an older version left beside a token it renewed."""
    tokens = tokens or {}
    access, granted = tokens.get('access_token'), tokens.get('scope')
    if (not isinstance(access, str) or not access or not isinstance(granted, str) or not granted.split()
            or tokens.get('scope_of') != bearer_mark(access)):
        return None
    return granted.split()


def bearer_scopes(tokens, scopes):
    """What a local bearer may use: the permissions Google named for it (bearer_scope), within the saved limit when one
    is known; None, never the saved scopes, when that bearer has no such record."""
    words = bearer_scope(tokens)
    if words is None:
        return None
    return [scope for scope in scopes if scope in words] if isinstance(scopes, list) else words


class GmailSignIn:
    def __init__(self, url='', token='', *, http=None):
        self._url = str(url).rstrip('/')
        self._token = _SkillToken(token)
        self._http = http
        self._advertised_handover = None  # Publish host support and its handover version in the same assignment.
        # Reserved transfer -> (earliest next recover request, its pause). Live only: a restarted companion asks at once,
        # and progress written into a mailbox would restart its worker.
        self.pauses = {}
        # Mailbox -> (earliest next offer, its pause) after an offer that started nothing; live only, like `pauses`.
        self.offers = {}

    @property
    def handover_version(self):
        # 2 or later: the host takes a client whose return address a saved sign-in never recorded (`unknown`).
        return self._advertised_handover or 0

    @classmethod
    def environment(cls):
        # Both the isolated tool/route child and the companion receive these reserved host values.
        return cls(os.environ.get('HOST_SERVICE_URL', ''), os.environ.get('HOST_SERVICE_TOKEN', ''))

    def _call(self, method, path, *, body=None, timeout=10):
        if not _loopback_base(self._url) or not self._token.available:
            raise SignInError('The Google sign-in host is not available to Mail.')

        def request(client):
            return client.request(method, self._url + path,
                                  headers={'x-skill-token': self._token.use_in_request()},
                                  json=body, timeout=timeout, follow_redirects=False)
        try:
            if self._http is not None:
                response = request(self._http)
            else:
                with httpx.Client(trust_env=False) as client:
                    response = request(client)
            answer = response.json()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            # Nothing reached the host: not a readable answer, and nothing was asked or changed there.
            raise HostNotReached('The Google sign-in host did not return a readable answer.') from None
        except (httpx.HTTPError, httpx.InvalidURL, HostContractError, OSError, ValueError):
            raise SignInError('The Google sign-in host did not return a readable answer.') from None
        if response.status_code != 200 or not isinstance(answer, dict) or answer.get('ok') is not True:
            if (path == '/sign-in/token' and response.status_code == 409 and isinstance(answer, dict)
                    and answer.get('reason') == 'min_valid_unmet'):
                raise SignInError(_SHORT_LIVED)  # the grant is kept: this ends no sign-in or consent
            if path == '/sign-in/token' and response.status_code in (404, 409):
                raise SignInError(f'This Google sign-in is no longer usable. Check {CARD}.')
            if (path == '/sign-in/handover' and response.status_code == 429 and isinstance(answer, dict)
                    and answer.get('handover_started') is False):
                raise HandoverNotStarted('The Google sign-in host was busy and started no transfer.')
            raise SignInError('The Google sign-in host could not confirm this request.')
        return answer

    def mode(self, *, require_identity=False):
        if not self._url and not self._token.available:
            return 'unavailable' if require_identity else 'legacy'
        if self._advertised_handover is not None:
            return 'host'
        try:
            answer = self._call('GET', '/identity', timeout=5)
        except SignInError:
            return 'unavailable'
        if 'sign_in_version' not in answer:
            return 'legacy'
        if type(answer['sign_in_version']) is not int or answer['sign_in_version'] != 1:
            return 'unavailable'
        handover = answer.get('sign_in_handover_version')
        self._advertised_handover = handover if type(handover) is int else 0
        return 'host'

    def accounts(self):
        answer = self._call('GET', '/sign-in/accounts')
        services = answer.get('services')
        if (answer.get('complete', True) is not True or not isinstance(services, list)
                or not all(isinstance(row, dict) and isinstance(row.get('id'), str) for row in services)):
            raise SignInError('The Google sign-in list was incomplete.')
        matches = [row for row in services if row['id'] == SERVICE]
        rows = matches[0].get('accounts') if len(matches) == 1 and matches[0].get('complete', True) is True else None
        if not isinstance(rows, list) or not all(_account_row(row) for row in rows):
            raise SignInError('The Google sign-in list was incomplete.')
        verified = [row['subject'] for row in rows if row['verified']]
        held = {row['key']: row for row in rows}
        if len(held) != len(rows) or len(set(verified)) != len(verified):
            raise SignInError('The Google sign-in list contained conflicting account identities.')
        return held

    def token(self, key, *, rejected='', connection='', consent=''):
        # connection, consent: what a change may use, sent for a change only (a consent only from a host announcing
        # sign_in_handover_version 2); the answer must name the same ones, so a host that ignores consent is not used.
        if not _host_key(key):
            raise SignInError(f'Mail has no confirmed Google account key. Check {CARD}.')
        answer = self._call('POST', '/sign-in/token', timeout=35, body={
            'service': SERVICE, 'account': key, **({'rejected': rejected} if rejected else {}),
            **({'connection_id': connection, 'min_valid': _CHANGE_VALID} if connection else {}),
            **({'consent': consent} if consent else {}),
        })
        account, access, scope = answer.get('account'), answer.get('access_token'), answer.get('scope')
        if (not isinstance(account, dict) or account.get('key') != key or not isinstance(access, str)
                or not access or any(not 33 <= ord(char) <= 126 for char in access)
                or str(answer.get('token_type') or '').casefold() != 'bearer' or not isinstance(scope, str)
                or (connection and answer.get('connection_id') != connection)
                or (consent and answer.get('consent') != consent)):
            raise SignInError('The Google sign-in host did not confirm an access token for this mailbox.')
        expiry = answer.get('expires_at')
        if type(expiry) not in (int, float) or not math.isfinite(expiry):
            expiry = None  # A token with no reported expiry is never cached by Mail.
        elif expiry <= time.time():
            raise SignInError('The Google sign-in host returned an expired access token.')
        return access, scope.split(), expiry

    def handover(self, clients, grants, *, recover=False):
        # recover asks for the outcome the host kept for these same grants; it registers and exchanges nothing.
        try:
            return self._call('POST', '/sign-in/handover', timeout=60,
                              body={'service': SERVICE, **({'recover': True} if recover else {}),
                                    'clients': clients, 'grants': grants})
        except HostNotReached:
            if recover:
                raise
            # The fresh offer never reached the host. Losing a response after sending it is a different error.
            raise HandoverNotStarted('The Google sign-in host was not reached and started no transfer.') from None

    def receipt(self, legacy_key, legacy_generation):
        """Read the host's exact transfer outcome without credentials or an exchange."""
        asked = {'legacy_key': legacy_key, 'legacy_generation': legacy_generation}
        answer = self._call('POST', '/sign-in/handover', timeout=15, body={'service': SERVICE, 'receipt': asked})
        receipt = answer.get('receipt')
        if not isinstance(receipt, dict) or receipt.get('legacy_key') != legacy_key:
            return None
        if receipt.get('legacy_generation') == legacy_generation:
            return receipt
        # The addressed read only returns this final refusal for the requested generation; the host need not echo
        # that field on it. `none`, `resubmit` and a successful transfer still need their own generation in the answer.
        if 'legacy_generation' not in receipt and _refresh_rejected(receipt):
            return receipt
        return None


def _account_row(row):
    return (isinstance(row, dict) and _host_key(row.get('key')) and type(row.get('verified')) is bool
            and isinstance(row.get('state'), str) and row['state'] in _STATES
            and all(isinstance(row.get(key), str) for key in ('subject', 'label', 'scope', 'client_label'))
            and isinstance(row.get('connection_id') or '', str) and isinstance(row.get('consent') or '', str)
            and (not row['verified'] or bool(row['subject'].strip())))


def _gmail_records(rows):
    return {row['id']: row for row in rows if row['provider'] == 'gmail'}


def _usable(row):
    return row['verified'] and row['state'] == 'signed_in' and mail_scope(row['scope'].split())


def _local_material(record):
    oauth, tokens = record.get('oauth'), record.get('tokens')
    return (isinstance(oauth, dict) and isinstance(oauth.get('client_id'), str) and bool(oauth['client_id'].strip())
            and isinstance(tokens, dict) and isinstance(tokens.get('refresh_token'), str) and bool(tokens['refresh_token']))


def _legacy_material(record):
    scopes = record.get('scopes')
    return (_local_material(record) and isinstance(scopes, list)
            and all(isinstance(s, str) and s.split() == [s] for s in scopes) and mail_scope(scopes))


def _read_material(record):
    """Missing permissions do not stop an otherwise eligible read; explicit insufficient permissions do."""
    return _local_material(record) if record.get('scopes') is None else _legacy_material(record)


def _change_material(record):
    """What a saved grant needs to change mail: its recorded Gmail permissions or, where it never recorded a limit,
    the Gmail permission Google named for its current bearer (bearer_scope). Neither known keeps it reading only."""
    if record.get('scopes') is None:
        own = bearer_scope(record.get('tokens'))
        return _local_material(record) and own is not None and mail_scope(own)
    return _legacy_material(record)


def _proven_unstarted(claim):
    # Only proof that this mailbox's own first fresh submission exchanged nothing counts: the host's per-item
    # `exchange: none`, a whole request its rate limit turned away unread, or one that never reached the host
    # (GmailSignIn.handover). A recover answer never does.
    return claim.get('refusal') == _NOT_STARTED and not claim.get('withdrawn')


def _unconfirmed(mode):
    if mode == 'legacy':  # an older Ouroboros has no sign-in card: only a new local sign-in can serve this mailbox
        return 'Gmail sign-in has not been confirmed on this Ouroboros version. Sign in to Google again in Email accounts.'
    return f'Gmail sign-in has not been confirmed by the host. Check {CARD}.'


def _breaks(store):
    """The host's rows of Disconnect and Revoke (sign_in_withdrawn.json): [] without the file, None when unreadable."""
    try:
        data = json.loads((store.state_dir / 'sign_in_withdrawn.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return None
    entries = data.get('accounts') if isinstance(data, dict) and data.get('v') == 1 else None
    return entries if isinstance(entries, list) and all(isinstance(row, dict) for row in entries) else None


def _deferred(store, oauth):
    """An unstarted transfer whose saved sign-in never recorded its client's return address waits, unreserved, for a
    host announcing sign_in_handover_version 2, which takes `return_hint: "unknown"`; nothing is guessed meanwhile."""
    host = store.sign_in
    return not (oauth or {}).get('return_hint') and not (host is not None and host.handover_version >= 2)


def _withdrawn(store, record, consent=None, *, rows=None):
    """Whether the host's record of a Disconnect or Revoke concerns a saved local consent of this mailbox: its original
    fields, or `consent`, the owner's later local sign-in (`gmail_late_consent`).

    The host writes sign_in_withdrawn.json in this skill's state directory, without secrets, before it confirms either.
    A mailbox can have several entries; `via` and `at` decide nothing. Each entry is judged in this order. A connection
    both sides name decides alone: a break takes every version of its connection, whatever the generation numbers say,
    and another confirmed connection is judged on its own. Otherwise a key, subject, legacy key or generation both sides
    know and that differs makes it another mailbox or copy. What remains concerns a mailbox the host held (nothing
    separates it) and a mailbox it never held only by naming its own subject or legacy key. The saved original is kept
    only while the host row is the handover on the recorded connection (`_follow`), so that connection is its own.
    Nothing is guessed from an address. The file only grows: a consent this version saved is judged by the rows written
    after it (`withdrawals_seen`), so an earlier break never blocks a later sign-in; a shorter file is judged whole."""
    binding, claim = record.get('gmail_host') or {}, record.get('gmail_transfer') or {}
    touched = bool(binding or claim)
    entries = _breaks(store) if rows is None else rows  # reuse the erasing caller's one readable snapshot
    if entries is None:
        return True  # unreadable: the local copy cannot be used until its status is known
    seen = ((record if consent is None else consent).get('oauth') or {}).get('withdrawals_seen')
    if type(seen) is int and 0 <= seen <= len(entries):
        entries = entries[seen:]
    subject = record['id'].removeprefix('google:') if record['id'].startswith('google:') else ''
    ours = ((consent.get('generation'),) if consent is not None else
            tuple(value for value in (claim.get('generation'), claim.get('previous_legacy_generation'),
                                      binding.get('legacy_generation')) if value))
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('service') != SERVICE:
            continue
        if entry.get('connection_id') and binding.get('connection_id'):
            if entry['connection_id'] == binding['connection_id']:
                return True
            continue
        known = ((entry.get('account'), binding.get('key')), (entry.get('subject'), subject),
                 (entry.get('legacy_key'), record['id']))
        if (any(a and b and a != b for a, b in known)
                or (entry.get('legacy_generation') and ours and entry['legacy_generation'] not in ours)):
            continue
        if touched or any(a and a == b for a, b in known[1:]):
            return True
    return False


def _keeps_late(store, record):
    """Keep the later local sign-in and its own connection unless a recorded withdrawal applies to it.
    Recovery still needs the fields of a pending offer. Missing scope or an unreadable withdrawal file is
    not a reason to erase them; _late_usable judges whether the grant may actually serve."""
    late, claim = record.get('gmail_late_consent') or {}, record.get('gmail_transfer') or {}
    if not late:
        return False
    if claim.get('generation') and claim['generation'] == late.get('generation') and not claim.get('withdrawn'):
        return True
    rows = _breaks(store)
    return rows is None or not _withdrawn(store, record, late, rows=rows)


def local_grant(_store, record):
    """Select one saved local grant before judging its usability; never fall back to an earlier sign-in."""
    late = record.get('gmail_late_consent')
    if late is not None:
        if not isinstance(late, dict) or not _consent(late.get('generation')):
            return {}, ''
        return {key: late.get(key) for key in ('oauth', 'tokens', 'scopes')}, late['generation']
    return {key: record.get(key) for key in ('oauth', 'tokens', 'scopes')}, ''


def _local_allowed(store, record, mode, held, *, reading=False):
    binding = record.get('gmail_host')
    claim = record.get('gmail_transfer') or {}
    if not record['connected']:
        return False
    grant, generation = local_grant(store, record)
    if not (_read_material(grant) if reading else _change_material(grant)):
        return False
    late = record['gmail_late_consent'] if generation else None
    if late is not None and claim.get('generation') == generation:
        return False  # This very grant was offered; withdrawing the offer does not prove it was never exchanged.
    if mode == 'legacy':
        if late is not None or not (binding or claim):
            eligible = True
        elif binding:
            eligible = not claim and binding.get('verified') and binding.get('state') == 'signed_in'
        else:
            eligible = _proven_unstarted(claim)
    elif mode == 'host':
        newer = any(row['verified'] and record['id'] == 'google:' + row['subject'] for row in held.values())
        eligible = not binding and (not claim or _proven_unstarted(claim)) and not newer
    else:
        return False
    return bool(eligible and not _withdrawn(store, record, late))


def credential_owner(store, record, *, reading=False, read_accounts=None):
    """Who serves the mailbox: 'host' or 'legacy', else SignInError. Only an ordinary read may use
    read_accounts: the caller's lazy complete host.accounts() snapshot for this search or poll. A change
    always reads the current list when local ownership depends on it."""
    binding, claim = record.get('gmail_host'), record.get('gmail_transfer') or {}
    host = store.sign_in
    transitioned = bool(binding or claim)
    mode = (host.mode(require_identity=transitioned) if host is not None else
            'unavailable' if transitioned else 'legacy')
    if mode == 'host' and binding:
        if binding.get('state') == 'disconnected':
            # A separate later local sign-in may still serve on an older host; it does not reconnect this card.
            raise SignInError(_unconfirmed(mode))
        return 'host'
    held = {}
    if mode == 'host' and (not transitioned or _proven_unstarted(claim)):
        held = (read_accounts if reading and read_accounts is not None else host.accounts)()
    if _local_allowed(store, record, mode, held, reading=reading):
        return 'legacy'
    raise SignInError(_unconfirmed(mode))


def host_link(store, account):
    """The access a new change on the host's grant uses, frozen from one row of the host's current list (one loopback
    read; reading mail never makes it): its connection and, on a host announcing sign_in_handover_version 2, that row's
    consent. The host refuses any other one (connection_changed, consent_changed)."""
    host = store.sign_in
    row = host.accounts().get((account.get('gmail_host') or {}).get('key'))
    strict = host.handover_version >= 2  # such a host names a valid consent for each change; nothing less is used
    if (not row or not _usable(row) or account['id'] != 'google:' + row['subject'] or not row.get('connection_id')
            or (strict and not _consent(row.get('consent')))):
        raise SignInError(f'This Gmail sign-in names no confirmed connection for a change. Check {CARD}.')
    return {'host': row['connection_id'], **({'consent': row['consent']} if strict else {})}


def sending_link(store, account):
    """The access a change, or a queued mail now going out, is made under: on the host's grant its host_link (the row of
    this mailbox's own verified subject), on this mailbox's own saved sign-in its local connection (`id`). SignInError
    while neither can be named now (the host unreadable, the sign-in unconfirmed or ended)."""
    if credential_owner(store, account) == 'host':
        return host_link(store, account)
    return {'id': _local_connection(store, account)}


def queued_link(store, account, *, for_intake=False):
    """The access a mail queued now keeps: on the host's grant its host_link, on this mailbox's own saved sign-in its
    local connection (`id`). When neither can be named now (the host unreadable, the sign-in unconfirmed) nothing is
    queued: email_send reports it and the inbox retries, so a short outage never leaves a mail held without a record.

    `for_intake` is for mail that is arriving, before its turn runs. It reads the host's mode and complete list at most
    once, judges reading and changing on that one snapshot, and takes a host link only from the row of this mailbox's own
    subject. It returns the access a change keeps; None when reading is confirmed but nothing confirms a right to change
    (the mail is taken in, and no reply to it is sent automatically); and raises while anything is unread, incomplete or
    unconfirmed, so nothing begins and the claim is retried."""
    if not for_intake:
        try:
            return sending_link(store, account)
        except SignInError as error:
            raise SignInError(f'{error} Nothing was queued; try again.') from None
    binding, claim, host = account.get('gmail_host'), account.get('gmail_transfer') or {}, store.sign_in
    transitioned = bool(binding or claim)
    mode = (host.mode(require_identity=transitioned) if host is not None else
            'unavailable' if transitioned else 'legacy')
    if mode == 'host' and binding:
        row = None if binding.get('state') == 'disconnected' else host.accounts().get(binding.get('key'))
        if not (row and _usable(row) and account['id'] == 'google:' + row['subject']):
            raise SignInError(_unconfirmed(mode))
        strict = host.handover_version >= 2  # such a host names a valid consent for each change; nothing less is used
        if not row.get('connection_id') or (strict and not _consent(row.get('consent'))):
            return None
        return {'host': row['connection_id'], **({'consent': row['consent']} if strict else {})}
    held = host.accounts() if mode == 'host' and (_proven_unstarted(claim) or not transitioned) else {}
    if not _local_allowed(store, account, mode, held, reading=True):
        raise SignInError(_unconfirmed(mode))
    # Incoming mail may be read without a recorded scope; its reply gets a link only where the selected grant can change mail.
    changing = _local_allowed(store, account, mode, held)
    return {'id': _local_connection(store, account)} if changing else None


def _local_connection(store, account):
    """The local connection id of the mailbox snapshot that was just judged: the one that snapshot already records, taken
    from it and not read again from the registry, where a sign-in made since has an id of its own. Only a snapshot with
    none gets one made in the usual way, and it counts only while the registry still holds that very snapshot: making an
    id never changes the generation; a save, a reserve or a new sign-in always does. Otherwise nothing begins, and no other
    sign-in's id is borrowed for what this snapshot judged."""
    link = account.get('gmail_connection')
    if isinstance(link, dict) and isinstance(link.get('id'), str) and link['id']:
        return link['id']
    made = store.gmail_connection(account['id'])
    if store.get(account['id']).get('generation') != account.get('generation'):
        raise SignInError('This Gmail mailbox changed while its access was being named.')
    return made


def serves_locally(store, account):
    """Whether this mailbox's own saved sign-in serves it now (credential_owner): Mail, not the card, disconnects it."""
    try:
        return credential_owner(store, account, reading=True) == 'legacy'  # a mailbox that only reads is Mail's to disconnect
    except SignInError:
        return False


def local_withdrawals_seen(store):
    """The break rows already recorded when a fresh local consent is saved; unreadable is unknown."""
    entries = _breaks(store)
    return len(entries) if entries is not None else None


def read_view(store, *, status_only=False):
    """Read current scope/connection facts without writing data or starting a transfer."""
    records = store.list()
    gmail = _gmail_records(records)
    transitioned = any(row.get('gmail_host') or row.get('gmail_transfer') for row in gmail.values())
    host = store.sign_in
    mode = (host.mode(require_identity=transitioned) if host is not None and (not status_only or gmail)
            else 'unavailable' if transitioned else 'legacy')
    held = {}
    if mode == 'host':
        try:
            held = host.accounts()
        except SignInError:
            mode = 'unavailable'
    for record in gmail.values():
        binding = record.get('gmail_host') or {}
        claim = record.get('gmail_transfer') or {}
        local = _local_allowed(store, record, mode, held, reading=True)
        if mode == 'legacy' and not (binding or claim) and local:
            record['scopes'] = bearer_scopes(record.get('tokens'), record.get('scopes'))  # this bearer's, not its limit
            continue
        grant = local_grant(store, record)[0] if local else {}
        scopes = bearer_scopes(grant.get('tokens'), grant.get('scopes')) if local else None
        for field in ('oauth', 'tokens', 'gmail_legacy_copy', 'gmail_late_consent'):
            record.pop(field, None)
        if local:
            # Its saved sign-in serves it: nothing to sign in to on any host (the card can still move it).
            record.update(auth_owner='legacy', scopes=scopes, gmail_auth_state='signed_in')
            continue
        if mode == 'legacy':
            # An older Ouroboros has no sign-in card: a new sign-in in Email accounts serves the mailbox there.
            record.update(connected=False, auth_owner='legacy', oauth_client_label='',
                          gmail_auth_state='needs_sign_in' if record['connected'] else 'disconnected')
            continue
        row = held.get(binding.get('key'))
        matches = bool(row and row['verified'] and record['id'] == 'google:' + row['subject'])
        connected = bool(matches and record['connected'] and _usable(row))
        state = ('unavailable' if mode != 'host' else 'signed_in' if connected else
                 'disconnected' if row is None and (claim.get('withdrawn') or binding.get('verified')
                                                     or binding.get('state') == 'disconnected')
                 else 'needs_sign_in')
        record.update(connected=connected, scopes=row['scope'].split() if matches else None,
                      auth_owner='host', gmail_auth_state=state, oauth_client_label=row['client_label'] if matches else '')
    return mode, records


def _put(store, old, record, connected):
    if record == old and connected == old.get('connected'):
        return old
    # The stored record must still be `old`: a renewal or a queued mail's connection written since is never put back.
    return store.save(record, connected=connected, expected_generation=old.get('generation'), expected=old)


def _clear_legacy(record):
    for name in ('oauth', 'tokens', 'scopes', 'gmail_legacy_copy'):
        record.pop(name, None)


def _new_mailbox(account_id, label):
    try:
        address = Address(addr_spec=label)
        if not address.username or not address.domain or address.addr_spec != label:
            return None
    except (ValueError, HeaderParseError):
        return None
    return {'id': account_id, 'provider': 'gmail', 'address': label, 'label': label, 'senders': [label],
            'sender': label, 'senders_status': 'unavailable_with_current_grant', 'config': {}, 'folder': 'INBOX',
            'sending': 'on', 'words': [], 'poll_interval_sec': 30}


def _follow(store, before, held):
    seen = set()
    strict = store.sign_in is not None and store.sign_in.handover_version >= 2  # it names each sign-in's consent
    for row in held.values():
        if not row['verified']:
            continue  # An import without verified subject cannot select a mailbox's history or bearer.
        account_id = 'google:' + row['subject']
        old = before.get(account_id) or {}
        binding = old.get('gmail_host') or {}
        if binding.get('verified') and binding.get('key') != row['key']:
            continue
        record = dict(old) if old else _new_mailbox(account_id, row['label'])
        if record is None:
            continue
        seen.add(account_id)
        claim = old.get('gmail_transfer') or {}
        fresh_consent = row.get('via') in ('card', 'mind_link')
        if claim.get('withdrawn') and not fresh_consent:
            continue
        if (strict and claim.get('require_verified') is True and row.get('via') == 'handover'
                and claim.get('generation') and not claim.get('refusal')
                and row.get('legacy_generation') == claim['generation'] and binding.get('filed') != claim['generation']):
            # A version 2 reservation, filed but unanswered, waits for its receipt (_lookup). One made before version 2
            # has no such receipt: its verified row serves the mailbox, its earlier mail included.
            continue
        rejoined = bool(claim.get('withdrawn'))
        if binding.get('state') == 'signed_in' and not old['connected']:
            # An older skill can locally disconnect without knowing gmail_host.
            # Do not reattach the same grant merely because the host still lists it.
            if not (fresh_consent and binding.get('legacy_generation')):
                continue
            rejoined = True
        connected = _usable(row)
        generation = row.get('legacy_generation')
        own = (row.get('via') == 'handover' and isinstance(generation, str) and generation
               and generation == claim.get('generation')
               and (not strict or (bool(binding.get('imported')) and binding.get('imported') == row.get('consent'))))
        late = record.get('gmail_late_consent') or {}
        settled_late = bool(connected and own and late and generation == late.get('generation'))
        keep_late = bool(late) and not settled_late
        link = old.get('gmail_connection') or {}
        broken = rejoined or link.get('ended') or (binding.get('connection_id') and row.get('connection_id')
                                                   and binding['connection_id'] != row['connection_id'])
        if broken and not keep_late:
            # The retained manual sign-in, if any, has its own local id. The host's break still ends its continuity below.
            record['gmail_connection'] = {'id': uuid.uuid4().hex}
        same_copy = binding.get('legacy_generation')
        if not claim.get('renewal'):
            same_copy = claim.get('generation') or same_copy
        if not (connected and row.get('via') == 'handover' and generation and generation == same_copy
                and binding.get('connection_id', row.get('connection_id')) == row.get('connection_id')
                and _legacy_material(record) and set(record['scopes']) <= set(row['scope'].split())):
            _clear_legacy(record)
        # Keep the original oauth/tokens/scopes in their original private fields.
        # The host projection and the bearer have their own live scope.
        # A change begun on this mailbox's own local access continues through the host only through the handover of
        # that same grant, while the host keeps that connection (`continues`; GmailClient._host_credential). A renewal
        # hands over the later local sign-in such a change began under, so it continues too. A host announcing
        # sign_in_handover_version 2 must prove it is that very grant: the consent its transfer receipt named
        # (`imported`; a kept newer grant names none) must be the listed one, and each later list must keep it.
        # Queued mail needs none of this: it goes out under the mailbox's current sign-in (sending_link).
        same = binding.get('connection_id') == row.get('connection_id') and (not strict or (
            bool(binding.get('continues_consent')) and binding.get('continues_consent') == row.get('consent')))
        if broken:
            continues = proof = None
        elif own:
            continues, proof = claim.get('connection'), (binding.get('imported') if strict else None)  # its own link
        else:
            continues, proof = (binding.get('continues'), binding.get('continues_consent')) if same else (None, None)
        record['gmail_host'] = {'key': row['key'], 'verified': True,
                                'state': 'signed_in' if connected else 'sign_in_required',
                                **({'connection_id': row['connection_id']} if row.get('connection_id') else {}),
                                **({'consent': row['consent']} if row.get('consent') else {}),
                                **({'legacy_generation': generation} if row.get('via') == 'handover'
                                   and isinstance(generation, str) and generation else {}),
                                **({'continues': continues} if continues else {}),
                                **({'continues_consent': proof} if continues and proof else {})}
        if settled_late:
            record.pop('gmail_late_consent', None)
        if not keep_late or not claim.get('renewal'):
            record.pop('gmail_transfer', None)
        elif binding.get('filed') == claim.get('generation'):
            # An earlier list snapshot does not discard the receipt already read for this still-pending renewal.
            record['gmail_host'].update({name: binding[name] for name in ('filed', 'imported') if name in binding})
        _put(store, old, record, bool(connected or (keep_late and old['connected'])))

    for account_id, old in before.items():
        binding = old.get('gmail_host')
        if not binding or account_id in seen:
            continue
        record, connected = dict(old), False
        gone = binding['key'] not in held
        record['gmail_host'] = {**binding, 'state': 'disconnected' if gone else 'sign_in_required'}
        if gone:
            _clear_legacy(record)  # the grant the host no longer holds is never reopened
            if _keeps_late(store, record):
                connected = old['connected']  # keep the later local sign-in and its own connection
            else:
                record.pop('gmail_late_consent', None)
                record['gmail_connection'] = {'ended': True}
        _put(store, old, record, connected)


def _candidate(store, account, held):
    """(record to reserve, client, grant) for one mailbox, or None.

    A first offer moves the saved local grant, the owner's later local sign-in when it serves; a transfer the host proved
    it never started may be offered again. A renewal moves a later local consent onto a confirmed handover. A sign-in
    that never recorded its client's return address waits, unreserved, for a host that takes it (_deferred)."""
    binding, claim = account.get('gmail_host') or {}, account.get('gmail_transfer') or {}
    subject = account['id'].removeprefix('google:')
    if not account['id'].startswith('google:') or not subject:
        return None
    if binding:
        peer = held.get(binding['key']) or {}
        source = account.get('gmail_late_consent') or {}
        previous = peer.get('legacy_generation')
        # Renewal is additive. Do not infer it from the version number: require an actual confirmed predecessor.
        if (claim or not source or not peer.get('verified') or peer.get('subject') != subject
                or peer.get('via') != 'handover' or not isinstance(previous, str) or not previous
                or _deferred(store, source.get('oauth')) or _withdrawn(store, account, source)):
            return None
        offer = _offer(account, source, source['generation'], {'previous_legacy_generation': previous})
        return (account, *offer) if offer else None
    if (claim and not _proven_unstarted(claim)) or not account['connected']:
        return None
    grant, late = local_grant(store, account)
    if (not late and _withdrawn(store, account)) or _deferred(store, grant.get('oauth')):
        return None  # a withdrawn original is never offered again; an unrecorded return waits for the host
    record = {**account, **grant}
    if late:
        record.pop('gmail_late_consent')  # offered now as this mailbox's grant; a lost answer recovers these fields
    offer = _offer(record, record, account['generation'], {})
    return (record, *offer) if offer else None


def _pending(account):
    """The offer a reserved transfer made, rebuilt from its reservation and the fields it was offered from.

    Nothing the reservation did not keep is supplied: without the generation, a renewal's predecessor or the fields
    themselves there is no offer to ask about."""
    claim = account.get('gmail_transfer') or {}
    generation, previous = claim.get('generation'), claim.get('previous_legacy_generation')
    if not account['id'].startswith('google:') or not isinstance(generation, str) or not generation:
        return None
    terms = {'require_verified': True} if claim.get('require_verified') is True else {}  # a recover repeats them exactly
    if not claim.get('renewal'):
        return _offer(account, account, generation, terms)
    source = account.get('gmail_late_consent') or {}
    if source.get('generation') != generation or not isinstance(previous, str) or not previous:
        return None
    return _offer(account, source, generation, {'previous_legacy_generation': previous, **terms})


def _offer(account, source, generation, extra):
    """The client registration and grant offered for one saved original; None unless it is complete."""
    subject = account['id'].removeprefix('google:')
    oauth, tokens, scopes = source.get('oauth') or {}, source.get('tokens') or {}, source.get('scopes')
    client_id, refresh = oauth.get('client_id'), tokens.get('refresh_token')
    if (not isinstance(client_id, str) or not client_id.strip() or not isinstance(refresh, str) or not refresh
            or not isinstance(scopes, list) or not all(isinstance(scope, str) and scope and scope.split() == [scope] for scope in scopes)
            or not mail_scope(scopes)):
        return None
    # Only a return address the saved consent proved (google_auth._connect), else `unknown` for a host taking it.
    client = {'client_id': client_id, 'client_secret': oauth.get('client_secret') or '',
              'label_hint': 'Mail', 'return_hint': oauth.get('return_hint') or 'unknown'}
    # A bearer goes along only with exactly the permissions Google named for that very bearer, and only inside the
    # saved limit: a narrower one stays narrower, a wider one is never passed off as its intersection. Without that
    # record the host renews the grant once within the limit.
    own = bearer_scope(tokens)
    issued = bool(own) and set(own) <= set(scopes) and mail_scope(own)
    grant = {'legacy_key': account['id'], 'legacy_generation': generation, **extra,
             'subject': subject, 'label': account['address'], 'client_id': client_id,
             'tokens': {**{key: tokens[key] for key in ('access_token', 'expires_at') if issued and key in tokens},
                        'refresh_token': refresh, 'scope': ' '.join(own if issued else scopes)}}
    return client, grant


def _reservation(store, current, held, offered, claim):
    """The record reserving `offered`, rebuilt under the registry's write lock (AccountStore.reserve, itself under the
    account's own lock, so no local renewal of it is in flight) from the mailbox as it is now: None when that is no
    longer this offer. A refresh token Google replaced meanwhile is never written over; what is sent is what was saved."""
    candidate = _candidate(store, current, held)
    if candidate is None:
        return None
    record, _client, grant = candidate
    if grant['tokens']['refresh_token'] != offered['tokens']['refresh_token']:
        return None
    link = (current.get('gmail_connection') or {}).get('id')  # the local access its own queued mail was given
    return {**record, 'gmail_transfer': {**claim, **({'connection': link} if link else {})}}


def _file_refusal(store, key, generation, state):
    """File a classified outcome against its reservation; never replace an intervening preference or sign-in."""
    def apply(old):
        claim = old['gmail_transfer']
        if claim.get('withdrawn'):
            return None
        if state == 'account_disconnected':
            record = {**old, 'gmail_transfer': {**claim, 'withdrawn': True}}
            _clear_legacy(record)
            if binding := record.get('gmail_host'):
                record['gmail_host'] = {**binding, 'state': 'disconnected'}
            if not _keeps_late(store, record):
                record.pop('gmail_late_consent', None)
                record.update(gmail_connection={'ended': True}, connected=False)
            return record
        if claim.get('refusal'):
            return None
        if state == _REFRESH_REJECTED:
            return {**old, 'gmail_transfer': {**claim, 'refusal': _REFRESH_REJECTED}}
        if state == _NOT_STARTED:
            if claim.get('renewal'):
                return {name: value for name, value in old.items() if name != 'gmail_transfer'}
            if not old.get('gmail_host'):
                record = {**old, 'gmail_transfer': {**claim, 'refusal': _NOT_STARTED}}
                if not record.get('gmail_late_consent'):
                    record['connected'] = bool(_legacy_material(record) and not _withdrawn(store, record))
                return record
        return None

    return store.settle_gmail_transfer(key, generation, apply)


def _file_receipts(store, answer, offered, *, lookup=False, closed=False):
    """File each offered grant's receipt. Return the accounts whose answer the host kept unfinished (`exchange: saved`)
    and those it proved it never started (`exchange: none`), which a later pass may offer again. A `resubmit` changes
    nothing here: the reservation stays parked until _lookup reads the host's own record (_resubmit).

    `lookup` marks the answer to a recover request: it settles what the host kept and never makes the old path usable.
    `closed` marks the host's own receipt of one exact transfer, read by address: its `exchange: none` closes that attempt,
    which a recover answer never does."""
    saved, unstarted = set(), set()
    rows = answer.get('grants')
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return saved, unstarted
    for key, generation in offered.items():
        matches = [row for row in rows if row.get('legacy_key') == key]
        if len(matches) != 1:
            continue
        receipt = matches[0]
        account = receipt.get('account')
        host_key = account.get('key') if isinstance(account, dict) else None
        result = receipt.get('result')
        if not isinstance(result, str) or receipt.get('legacy_generation', generation) != generation:
            continue
        if result == 'refused':
            # Negative replies need not echo a generation. The per-item key and the current exact reservation bind them.
            reason, exchange = receipt.get('reason'), receipt.get('exchange')
            if reason == 'account_disconnected':
                _file_refusal(store, key, generation, 'account_disconnected')
            elif _refresh_rejected(receipt):
                _file_refusal(store, key, generation, _REFRESH_REJECTED)
            elif exchange == 'saved':
                try:
                    claim = store.get(key).get('gmail_transfer') or {}
                except AccountError:
                    continue
                if claim.get('generation') == generation and not (claim.get('withdrawn') or claim.get('refusal')):
                    saved.add(key)
            elif exchange == 'none' and (closed or not lookup) and account is None:
                if _file_refusal(store, key, generation, _NOT_STARTED) is not None:
                    unstarted.add(key)
            continue
        if result in _KEPT and _host_key(host_key) and receipt.get('legacy_generation') == generation:
            # Only an imported grant's own consent can confirm continuity; a list or a kept-newer result cannot supply it.
            proof = {'filed': generation, **({'imported': receipt['consent']}
                if result == 'imported' and _consent(receipt.get('consent')) else {})}

            def apply(old):
                claim, binding = old['gmail_transfer'], old.get('gmail_host')
                if claim.get('withdrawn') or claim.get('refusal'):
                    return None
                if not binding:
                    return {**old, 'gmail_host': {'key': host_key, 'verified': False, 'state': 'sign_in_required',
                                                  **proof}, 'connected': old['connected'] and _keeps_late(store, old)}
                if claim.get('renewal') and binding.get('key') == host_key:
                    return {**old, 'gmail_host': {**binding, **proof}}
                return None

            store.settle_gmail_transfer(key, generation, apply)
    return saved, unstarted


def _wait_to_offer(host, keys):
    """A transfer that started nothing is offered again only after a pause that grows from 30 seconds to an hour; the
    pauses live in the host object, like `pauses`."""
    for key in keys:
        pause = min(_OFFER_LONGEST, 2 * host.offers.get(key, (0, 15))[1])
        host.offers[key] = (time.monotonic() + pause, pause)


def _reserved_offer(account):
    """The offer this mailbox's open reservation made (_pending), rebuilt from what it still holds; None once that
    reservation was withdrawn, refused or filed."""
    claim = account.get('gmail_transfer') or {}
    if (not claim.get('generation') or claim.get('withdrawn') or claim.get('refusal')
            or (account.get('gmail_host') and not claim.get('renewal'))):
        return None
    return _pending(account)


def _resubmit(store, host, grant):
    """Offer once more, as an ordinary transfer, what this mailbox still holds under the reservation the host's own record
    named `resubmit`, with this mailbox's own registration; the saved sign-in stays parked. Nothing goes while a recorded
    withdrawal may concern the grant it would hand over (_withdrawn: the original, or a renewal's later local sign-in) or
    that record cannot be read. Only the answer's per-item word moves anything (_file_receipts), and an answer that never
    arrives leaves the reservation for the next pause."""
    try:
        current = store.get(grant['legacy_key'])
    except AccountError:
        return
    offer, claim = _reserved_offer(current), current.get('gmail_transfer') or {}
    late = current.get('gmail_late_consent') if claim.get('renewal') else None
    if offer is None or offer[1] != grant or _withdrawn(store, current, late):
        return  # it moved on since its record was read, or a withdrawal may concern what it would hand over
    try:
        answer = host.handover([offer[0]], [offer[1]])
    except SignInError:
        return  # not reached, turned away or unanswered: still parked, and the record is read again after the pause
    _, unstarted = _file_receipts(store, answer, {grant['legacy_key']: grant['legacy_generation']})
    _wait_to_offer(host, unstarted)


def _lookup(store, host):
    """Ask the host, with recover, for the outcome it kept of each reserved transfer whose answer never arrived.

    Every offer is rebuilt from its reservation and the fields it was offered from, so nothing is registered, exchanged
    or submitted again, and a `none` in the answer never makes the old path usable. A transfer is asked about at once,
    then after pauses doubling from 30 seconds to 15 minutes. The pauses live in the host object, not in the mailbox, so
    progress restarts no worker and a restarted companion asks again at once. A final refusal ends the asking. A host
    announcing sign_in_handover_version 2 is first asked for its own record of the transfer (GmailSignIn.receipt), and
    there its list alone never settles one (_follow); on any other host the list still settles a late import. That
    record's `exchange: none` for exactly this generation closes the attempt as a first answer's word does and gives the
    saved sign-in back, confirming no earlier mail, new owner, break or permission; a recover answer's `none` never does.
    Its `resubmit` alone sends an offer again: that mailbox's own, as an ordinary transfer (_resubmit)."""
    now, clients, grants, offered, reserved = time.monotonic(), {}, [], {}, set()
    for account in _gmail_records(store.list()).values():
        offer = _reserved_offer(account)
        if offer is None:
            continue
        client, grant = offer
        reservation = (grant['legacy_key'], grant['legacy_generation'])
        reserved.add(reservation)
        if host.pauses.get(reservation, (0, 0))[0] > now:
            continue
        pause = min(_PAUSE_LONGEST, 2 * host.pauses.get(reservation, (0, 15))[1])
        host.pauses[reservation] = (now + pause, pause)
        clients[grant['client_id']] = client
        grants.append(grant)
        offered[grant['legacy_key']] = grant['legacy_generation']
    for finished in set(host.pauses) - reserved:
        del host.pauses[finished]  # a transfer that is no longer reserved is no longer waited for
    if not grants:
        return False
    if host.handover_version >= 2:
        # The host's own record of each transfer, read without credentials or an exchange: one it names is filed with its
        # consent; one it closed without an exchange (`exchange: none` for exactly this generation) started nothing and
        # gives the saved sign-in back; one it says to offer again (`resubmit`) goes once more as reserved, still parked;
        # only the rest are asked to finish a kept attempt (recover).
        for grant in list(grants):
            try:
                receipt = host.receipt(grant['legacy_key'], grant['legacy_generation'])
            except SignInError:
                continue  # unread for now: recover is still asked, and the next pause reads it again
            refused = receipt is not None and receipt.get('result') == 'refused' and not receipt.get('account')
            closed = refused and receipt.get('exchange') == 'none'
            if refused and receipt.get('exchange') == 'resubmit':
                _resubmit(store, host, grant)
            elif receipt is not None and (receipt.get('result') in _KEPT or closed or _refresh_rejected(receipt)):
                if closed:  # the fact is read, not its reason's words: nothing but a closed attempt comes of it
                    receipt = {name: value for name, value in receipt.items() if name != 'reason'}
                _, unstarted = _file_receipts(store, {'grants': [receipt]},
                                              {grant['legacy_key']: grant['legacy_generation']}, lookup=True, closed=closed)
                _wait_to_offer(host, unstarted)
            else:
                continue
            grants.remove(grant)
            offered.pop(grant['legacy_key'], None)
        if not grants:
            return True
    try:
        _file_receipts(store, host.handover(list(clients.values()), grants, recover=True), offered, lookup=True)
    except SignInError:
        pass  # Still no answer: the next request comes after the pause set above.
    return True


def sync_gmail(store):
    """Reconcile once: follow the host's list, ask about a transfer whose answer never arrived, offer saved grants.

    Nothing is offered that no saved record knows: no installation default client is registered, and a return address
    a saved sign-in never recorded goes, as `unknown`, only to a host that takes it (_deferred)."""
    host = store.sign_in
    if host is None or host.mode() != 'host':
        return
    before = _gmail_records(store.list())
    try:
        held = host.accounts()
    except SignInError:
        return
    _follow(store, before, held)
    asked = _lookup(store, host)  # a transfer whose answer never arrived: one recover request, no new exchange

    now, candidates, registrations, conflicts = time.monotonic(), [], {}, set()
    for account in _gmail_records(store.list()).values():
        if host.offers.get(account['id'], (0, 0))[0] > now:
            continue  # a transfer that started nothing waits before it is offered again
        candidate = _candidate(store, account, held)
        if not candidate:
            continue
        record, client, grant = candidate
        client_id = grant['client_id']
        prior = registrations.setdefault(client_id, client)
        if prior['client_secret'] != client['client_secret']:
            conflicts.add(client_id)  # Never pick one credential for ambiguous issuing registrations.
        elif prior['return_hint'] == 'unknown':
            registrations[client_id] = client  # a return another saved sign-in of this client proved
        elif client['return_hint'] not in ('unknown', prior['return_hint']):
            conflicts.add(client_id)  # two proven return addresses: neither is chosen
        candidates.append((account, record, grant))
    grants, offered, clients = [], {}, {}
    terms = {'require_verified': True} if host.handover_version >= 2 else {}  # filed only under a confirmed identity
    for account, _record, grant in candidates:
        client_id, key, generation = grant['client_id'], grant['legacy_key'], grant['legacy_generation']
        if client_id in conflicts:
            continue
        renewal = 'previous_legacy_generation' in grant
        claim = {'generation': generation, 'renewal': renewal, **terms,
                 **({'previous_legacy_generation': grant['previous_legacy_generation']} if renewal else {})}
        with store.account_lock(account['id']) as free:  # never beside a local renewal of it; others do not wait
            reserved = store.reserve(account, lambda current: _reservation(store, current, held, grant, claim),
                                     connected=renewal and account['connected']) if free else None
        offer = _pending(reserved) if reserved is not None else None
        if offer is not None:  # sent as reserved: rebuilt from the saved fields, as a recover is
            offered[key] = generation
            clients[client_id] = registrations[client_id]
            grants.append(offer[1])
    if grants:
        try:
            saved, unstarted = _file_receipts(store, host.handover(list(clients.values()), grants), offered)
            if saved:
                _lookup(store, host)  # The host kept an answer for some: finish it once; nothing is exchanged again.
        except HandoverNotStarted:
            # Turned away unread, or the connection was never made. Settle each still-current reservation, not the
            # pre-request row: the owner may have saved preferences while the request was in flight.
            unstarted = {key for key, generation in offered.items()
                         if _file_refusal(store, key, generation, _NOT_STARTED) is not None}
        except SignInError:
            # Uncertain transport: the reservations stay inert and the next pass asks the host, with recover, for what it kept.
            return
        _wait_to_offer(host, unstarted)
        asked = True
    if asked:
        before = _gmail_records(store.list())
        try:
            held = host.accounts()
        except SignInError:
            return
        _follow(store, before, held)
