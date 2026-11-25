"""Google Workspace's client of the Ouroboros sign-in service on its Host Service.

An Ouroboros whose ``GET /identity`` carries ``sign_in_version`` holds the owner's Google
sign-ins for this skill, as the manifest's top-level ``sign_in`` declares. This client reads
only what that host answers on loopback: the accounts it holds for Google Workspace, a
short-lived access token for one of them, and the one-time handover of the grant and client
the skill held before. A token stays in its caller's memory: nothing here prints, logs or
stores one, and the skill token never leaves a loopback address.
"""
from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

READY, ABSENT, UNREACHABLE, NO_HOST = 'ready', 'absent', 'unreachable', 'no_host'
SERVICE = 'google'   # the id of the manifest's sign_in entry
HANDOVER_TIMEOUT = 180


class HostError(Exception):
    """A Host Service refusal: its HTTP status and JSON body (the host never puts a token in one)."""

    def __init__(self, status: int, body: Dict[str, Any]):
        super().__init__(f'HTTP {status}')
        self.status, self.body = status, body


class HostSignIn:
    def __init__(self, base_url: str = '', token: str = ''):
        url = str(base_url or os.environ.get('HOST_SERVICE_URL') or '').rstrip('/')
        parts = urllib.parse.urlsplit(url)
        loopback = (parts.scheme == 'http' and parts.hostname in ('127.0.0.1', 'localhost', '::1')
                    and not parts.username and not parts.password)
        self.base_url = url if loopback else ''
        self._token = str(token or os.environ.get('HOST_SERVICE_TOKEN') or '').strip()
        self._state: Optional[str] = None
        self.handover_version: int = 0

    def state(self, retry: bool = False) -> str:
        """What this Ouroboros can offer.

        ``ready``: it holds sign-ins. ``absent``: it answered and holds none (an older
        Ouroboros, on which the skill keeps its own route). ``unreachable``: it was there
        but did not answer, or answered something unreadable — nothing is decided, and an
        unreadable answer is never read as an older Ouroboros. ``no_host``: this process has
        no Host Service address or token at all, so there is nobody to ask.
        """
        if self._state is None or (retry and self._state == UNREACHABLE):
            if not self.base_url or not self._token:
                self._state = NO_HOST
            else:
                try:
                    identity = self._call('GET', '/identity')
                except (HostError, OSError, ValueError):
                    identity = {}
                if identity.get('ok') is True:
                    raw_handover = identity.get('sign_in_handover_version')
                    self.handover_version = raw_handover if type(raw_handover) is int and raw_handover >= 0 else 0
                    self._state = READY if _number(identity.get('sign_in_version')) >= 1 else ABSENT
                else:
                    self.handover_version = 0
                    self._state = UNREACHABLE
        return self._state

    def available(self, retry: bool = False) -> bool:
        return self.state(retry) == READY

    def accounts(self, service: str = SERVICE) -> Optional[List[Dict[str, Any]]]:
        """The accounts the host holds for this skill's ``service``; None when it lists no such service."""
        answer = self._call('GET', '/sign-in/accounts')
        services = answer.get('services')
        if answer.get('ok') is not True or not isinstance(services, list) or any(
                not isinstance(item, dict) for item in services):
            raise ValueError('unreadable sign-in account list')
        matching = [item for item in services if item.get('id') == service]
        if not matching:
            return None
        rows = matching[0].get('accounts')
        if len(matching) != 1 or not isinstance(rows, list) or any(
                not isinstance(row, dict) or not isinstance(row.get('key'), str) or not row['key']
                for row in rows):
            raise ValueError('unreadable sign-in account list')
        return rows

    def token(self, account: str = '', *, connection_id: str = '', consent: str = '',
              rejected: Optional[str] = None, required_scope: str = '', min_valid: int = 120,
              service: str = SERVICE) -> Dict[str, Any]:
        """A short-lived access token for one account.

        ``account`` may stay empty when the host holds exactly one usable account. ``rejected``
        is the bearer the provider refused: the host refreshes once, and only if it still holds
        that very token. ``required_scope`` is the permission this call needs; when the grant
        does not carry it, the host answers with the owner's next step.
        """
        body: Dict[str, Any] = {'service': service, 'min_valid': min_valid}
        if account:
            body['account'] = account
        if connection_id:
            body['connection_id'] = connection_id
        if consent:
            body['consent'] = consent
        if rejected:
            body['rejected'] = rejected
        if required_scope:
            body['required_scope'] = required_scope
        return self._call('POST', '/sign-in/token', body)

    def handover(self, clients: List[Dict[str, Any]], grants: List[Dict[str, Any]],
                 service: str = SERVICE, *, recover: bool = False) -> Dict[str, Any]:
        """Hand the grant and the client the skill held before over to the host, once."""
        # The host checks every account with Google before it answers; that can take a while.
        body: Dict[str, Any] = {'service': service, 'clients': clients, 'grants': grants}
        if recover:
            body['recover'] = True
        return self._call('POST', '/sign-in/handover', body, timeout=HANDOVER_TIMEOUT)

    def handover_receipt(self, legacy_key: str, legacy_generation: str,
                         service: str = SERVICE) -> Optional[Dict[str, Any]]:
        """Read a settled handover outcome for an exact prior generation without sending credentials."""
        if self.handover_version < 2 or not legacy_key or not legacy_generation:
            return None
        body = {'service': service, 'receipt': {'legacy_key': legacy_key, 'legacy_generation': legacy_generation}}
        try:
            return self._call('POST', '/sign-in/handover', body)
        except (HostError, OSError, ValueError):
            return None

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
              timeout: float = 40) -> Dict[str, Any]:
        if not self.base_url or not self._token:
            raise OSError('no loopback Host Service address or skill token in this process')
        request = urllib.request.Request(self.base_url + path, method=method,
                                         data=json.dumps(body).encode('utf-8') if body is not None else None)
        request.add_header('X-Skill-Token', self._token)
        request.add_header('Content-Type', 'application/json')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise HostError(exc.code, _object(exc.read() if exc.fp else b'') or {}) from None
        except http.client.HTTPException as exc:
            raise OSError(f'Host Service connection broke: {type(exc).__name__}') from None
        answer = _object(raw)
        if answer is None:
            raise ValueError('the Host Service answered something other than a JSON object')
        return answer


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):   # the skill token never follows a redirect
        return None


def _object(raw: bytes) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(raw.decode('utf-8')) if raw else {}
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0
