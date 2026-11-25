"""Calendar's client of the Ouroboros sign-in service on its Host Service.

An Ouroboros whose ``GET /identity`` carries ``sign_in_version`` holds the owner's Google sign-ins for this
skill, as its manifest's top-level ``sign_in`` declares. This client only reads what that host answers on
loopback: the accounts it holds for Calendar, a short-lived access token for one of them, and the
one-time handover of the tokens and clients the skill held before. A token stays in its caller's memory:
nothing here prints, logs or stores one, and the skill token never leaves a loopback address.
"""
from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

READY, ABSENT, UNREACHABLE = 'ready', 'absent', 'unreachable'
SERVICE = 'google'   # the id of the manifest's sign_in entry


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

    def state(self, retry: bool = False) -> str:
        """``ready``: this Ouroboros holds sign-ins; ``absent``: it answered without them (an older core, on
        which the skill keeps its own); ``unreachable``: no usable answer, so nothing is decided. A process
        asks once; ``retry`` asks again after an unreachable answer (the long-lived worker, every pass)."""
        if self._state is None or (retry and self._state == UNREACHABLE):
            try:
                identity = self._call('GET', '/identity')
            except (HostError, OSError, ValueError):
                identity = {}
            self._state = (UNREACHABLE if identity.get('ok') is not True else
                           READY if _number(identity.get('sign_in_version')) >= 1 else ABSENT)
        return self._state

    def available(self, retry: bool = False) -> bool:
        return self.state(retry) == READY

    def accounts(self, service: str = SERVICE) -> Optional[List[Dict[str, Any]]]:
        """A complete account list, or None for an undeclared service; unreadable is never empty."""
        answer = self._call('GET', '/sign-in/accounts')
        services = answer.get('services')
        if (answer.get('ok') is not True or not isinstance(services, list) or answer.get('complete', True) is not True
                or any(not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']
                       for item in services)):
            raise ValueError('Unreadable Host Service sign-in listing')
        matches = [item for item in services if item['id'] == service]
        if not matches:
            return None
        rows = matches[0].get('accounts')
        if (len(matches) != 1 or matches[0].get('complete', True) is not True or not isinstance(rows, list)
                or not all(_account_row_readable(row) for row in rows)
                or len({row['key'] for row in rows}) != len(rows)):
            raise ValueError('Unreadable Host Service account listing')
        return rows

    def token(self, account: str, *, rejected: Optional[str] = None, min_valid: int = 120,
              service: str = SERVICE, connection_id: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {'service': service, 'account': account, 'min_valid': min_valid}
        if rejected:
            body['rejected'] = rejected   # the access token Google refused: the host refreshes once if it still holds it
        if connection_id:
            body['connection_id'] = connection_id
        return self._call('POST', '/sign-in/token', body)

    def handover(self, clients: List[Dict[str, Any]], grants: List[Dict[str, Any]],
                 service: str = SERVICE, *, recover: bool = False) -> Dict[str, Any]:
        # The host checks every account with Google before it answers; that can take a while per account.
        body: Dict[str, Any] = {'service': service, 'clients': clients, 'grants': grants}
        if recover:
            body['recover'] = True
        return self._call('POST', '/sign-in/handover', body, timeout=180)

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 40) -> Dict[str, Any]:
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


def _account_row_readable(row):
    return (isinstance(row, dict) and isinstance(row.get('key'), str) and bool(row['key'].strip())
            and isinstance(row.get('verified'), bool) and isinstance(row.get('subject'), str)
            and (not row['verified'] or bool(row['subject'].strip()))
            and ('connection_id' not in row or row['connection_id'] is None or isinstance(row['connection_id'], str))
            and row.get('state') in ('signed_in', 'sign_in_required'))


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
