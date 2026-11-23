"""The core's widget lease controls, as the browser harnesses' fixture hosts answer them.

tests/test_dashboard_theme_browser.py and tests/test_telegram_participant_widget_browser.py mount real cards through
the core's own module bridge against a fixture host. LeaseGateway stands in for the pinned core's lease controls
(ouroboros/gateway/router.py, widget_assets.py api_widget_asset_lease and WidgetAssetLeases, and
browser_boundary.same_origin_browser) for the classic cards a fixture lists. It refuses what they refuse, with their
status: another path (404) or method (405), a request that is not the parent page's (403), a body that is not
application/json (400), is longer than 2048 bytes (413) or is not a JSON object (400), other fields than the
operation's (400), an unknown operation (404), a card whose publication is not live or a token another card holds
(409), and a revoked token (410). A fixture host has one owner and never idles for the 120-second lease, so the owner,
the password epoch and expiry are not modelled. tests/test_widget_lease_gateway.py pins both directions.
"""
from __future__ import annotations

import json
import re
import secrets

CONTROLS = '/api/widgets/asset-leases'
RESOURCES = '/api/widget-resources/'
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
ORIGIN = re.compile(r'(https?)://([^/?#@\s]+)')
UNAVAILABLE = {'error': 'widget publication or lease unavailable'}


def controls(path):
    """Whether a request path belongs to the lease controls (the two routes and anything a client sends beside them)."""
    return path == CONTROLS or path.startswith(CONTROLS + '/')


def same_origin(headers):
    """browser_boundary.same_origin_browser: Sec-Fetch-Site decides when present; otherwise Origin must name this
    host; a request with neither is a native client's. `headers` maps lower-case names to values."""
    origin, site = headers.get('origin'), headers.get('sec-fetch-site')
    match = ORIGIN.fullmatch(origin.strip().lower()) if origin is not None else None
    if origin is not None and match is None:
        return False
    if site is not None:
        return site.strip().lower() in {'same-origin', 'none'}
    return match is None or f'{match.group(1)}://{match.group(2)}' == 'http://' + headers.get('host', '').strip().lower()


class LeaseGateway:
    def __init__(self, cards):
        """`cards` maps each listed classic card's skill to its live publication, (tab_id, generation)."""
        self.cards = dict(cards)
        self.held = {}  # token -> (skill, tab_id)
        self.ended = set()
        self.refused = []  # (status, method, path, body) of every request answered with an error

    @staticmethod
    def lease(token):
        return {'token': token, 'base_url': f'{RESOURCES}{token}/', 'expires_in': 120, 'renew_after': 40}

    def answer(self, method, path, headers, payload):
        """(status, JSON body) for one request to the controls; `headers` is any mapping of header names."""
        status, body = self._answer(method, path, {name.lower(): value for name, value in headers.items()}, payload)
        if status != 200:
            self.refused.append((status, method, path, payload.decode('utf-8', 'replace')))
        return status, body

    def _answer(self, method, path, headers, payload):
        op = None if path == CONTROLS else path.removeprefix(CONTROLS + '/')
        if op is not None and (op == path or not op or '/' in op):
            return 404, {'error': 'Not Found'}  # router.py routes exactly these two paths
        if method != 'POST':
            return 405, {'error': 'Method Not Allowed'}
        if not same_origin(headers):
            return 403, {'error': 'widget lease controls require the parent origin'}
        if headers.get('content-type', '').split(';', 1)[0] != 'application/json':
            return 400, {'error': 'expected JSON'}
        if len(payload) > 2048:
            return 413, {'error': 'lease request too large'}
        try:
            body = json.loads(payload)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return 400, {'error': 'expected lease object'}
        fields = ('skill', 'tab_id', 'generation') if op is None else ('token',)
        kind = body.pop('scope', 'files') if op is None else 'files'
        token = body.pop('token', None) if op is None and kind == 'routes' else None
        if (kind not in ('files', 'routes') or set(body) != set(fields)
                or not all(isinstance(body[key], str) and 0 < len(body[key]) <= 128 for key in fields)
                or (token is not None and not (isinstance(token, str) and TOKEN.fullmatch(token)))):
            return 400, {'error': 'invalid lease fields'}
        if op is None:
            return self._open(body['skill'], body['tab_id'], body['generation'], kind, token)
        if op == 'renew':
            return (200, self.lease(body['token'])) if body['token'] in self.held else (409, UNAVAILABLE)
        if op == 'revoke':
            if TOKEN.fullmatch(body['token']):
                self.held.pop(body['token'], None)
                self.ended.add(body['token'])
            return 200, {'ok': True}
        return 404, {'error': 'unknown lease operation'}

    def _open(self, skill, tab_id, generation, kind, token):
        if token in self.ended:
            return 410, {'error': 'this widget lease was ended'}
        held, card = self.held.get(token), self.cards.get(skill)
        # A route lease needs its card's live publication: the minted generation on a first mint, whichever is live
        # once the frame holds a token. A file lease serves a module entry's files, and a classic card has none.
        live = kind == 'routes' and card is not None and card[0] == tab_id and (token is not None or card[1] == generation)
        if not live or (held is not None and held != (skill, tab_id)):
            return 409, UNAVAILABLE
        token = token or secrets.token_urlsafe(32)
        self.held[token] = (skill, tab_id)
        return 200, self.lease(token)
