"""The browser harnesses' lease host (tests/widget_lease_gateway.py) accepts what the pinned core's lease controls
accept and refuses, with their status, what they refuse (ouroboros/gateway/widget_assets.py api_widget_asset_lease)."""
from __future__ import annotations

import json

import pytest

from widget_lease_gateway import CONTROLS, TOKEN, LeaseGateway

TAB = {'skill': 'telegram-user-session', 'tab_id': 'participant', 'generation': 'fixture-generation'}
OTHER = {'skill': 'context-lens', 'tab_id': 'dashboard', 'generation': 'fixture-generation'}
# A same-origin fetch from the parent page, as the browsers send it.
PARENT = {'Host': '127.0.0.1:8123', 'Origin': 'http://127.0.0.1:8123', 'Sec-Fetch-Site': 'same-origin',
          'Content-Type': 'application/json'}


def gateway():
    return LeaseGateway({card['skill']: (card['tab_id'], card['generation']) for card in (TAB, OTHER)})


def post(host, path, body, method='POST', **changed):
    """One request from the parent page; `changed` replaces headers (None drops one)."""
    headers = {name: value for name, value in {**PARENT, **{key.replace('_', '-'): value
                                                           for key, value in changed.items()}}.items() if value is not None}
    return host.answer(method, path, headers, body if isinstance(body, bytes) else json.dumps(body).encode())


def test_a_card_mints_renews_reestablishes_and_revokes_its_route_lease():
    host = gateway()
    status, lease = post(host, CONTROLS, {**TAB, 'scope': 'routes'})
    assert status == 200 and TOKEN.fullmatch(lease['token'])
    assert lease == {'token': lease['token'], 'base_url': f"/api/widget-resources/{lease['token']}/",
                     'expires_in': 120, 'renew_after': 40}
    token = lease['token']
    assert post(host, CONTROLS + '/renew', {'token': token}, Content_Type='application/json; charset=utf-8') == (200, lease)
    # The running frame's token re-establishes the same lease under whichever generation is live now.
    assert post(host, CONTROLS, {**TAB, 'generation': 'newer', 'scope': 'routes', 'token': token}) == (200, lease)
    # A lease the core lost is re-established under the frame's token; a native client sends no browser headers.
    lost = 'L' * 43
    assert post(host, CONTROLS, {**TAB, 'scope': 'routes', 'token': lost}, Origin=None, Sec_Fetch_Site=None)[0] == 200
    assert post(host, CONTROLS + '/renew', {'token': lost}, Sec_Fetch_Site=None) == (200, host.lease(lost))
    # Revocation ends a token for good, any well-shaped one included; a new mint gets a new token.
    assert post(host, CONTROLS + '/revoke', {'token': token}) == (200, {'ok': True})
    assert post(host, CONTROLS + '/revoke', {'token': 'never-minted'}) == (200, {'ok': True})
    assert post(host, CONTROLS, {**TAB, 'scope': 'routes', 'token': token})[0] == 410
    assert post(host, CONTROLS + '/renew', {'token': token})[0] == 409
    status, again = post(host, CONTROLS, {**TAB, 'scope': 'routes'})
    assert status == 200 and again['token'] != token
    assert [status for status, *_ in host.refused] == [410, 409]


@pytest.mark.parametrize('request_, status', [
    (('GET', CONTROLS, {**TAB, 'scope': 'routes'}, {}), 405),                     # the controls are POST only
    (('POST', CONTROLS + '/renew/again', {'token': 'x'}, {}), 404),                # exactly two routes
    (('POST', CONTROLS + 'x', {**TAB, 'scope': 'routes'}, {}), 404),
    (('POST', CONTROLS, {**TAB, 'scope': 'routes'}, {'Content-Type': 'text/plain'}), 400),  # JSON only
    (('POST', CONTROLS, {**TAB, 'scope': 'routes'}, {'Content-Type': None}), 400),
    (('POST', CONTROLS, {**TAB, 'scope': 'routes'}, {'Content-Type': 'Application/JSON'}), 400),
    (('POST', CONTROLS, {**TAB, 'scope': 'routes'}, {'Sec-Fetch-Site': 'cross-site'}), 403),  # another site's page
    (('POST', CONTROLS, {**TAB, 'scope': 'routes'}, {'Origin': 'null', 'Sec-Fetch-Site': None}), 403),  # the frame
    (('POST', CONTROLS, {**TAB, 'scope': 'routes'}, {'Origin': 'http://example.test', 'Sec-Fetch-Site': None}), 403),
    (('POST', CONTROLS, {**TAB, 'scope': 'routes', 'tab_id': 'p' * 2048}, {}), 413),
    (('POST', CONTROLS, b'not json', {}), 400),
    (('POST', CONTROLS, [TAB], {}), 400),
    (('POST', CONTROLS, {'skill': TAB['skill'], 'scope': 'routes'}, {}), 400),     # no tab id or generation
    (('POST', CONTROLS, {**TAB, 'scope': 'routes', 'extra': 1}, {}), 400),
    (('POST', CONTROLS, {**TAB, 'scope': 'routes', 'tab_id': ''}, {}), 400),
    (('POST', CONTROLS, {**TAB, 'scope': 'routes', 'generation': 'g' * 129}, {}), 400),
    (('POST', CONTROLS, {**TAB, 'scope': 'routes', 'token': 'short'}, {}), 400),  # not the core's token shape
    (('POST', CONTROLS, {**TAB, 'token': 'T' * 43}, {}), 400),                      # a token re-establishes routes only
    (('POST', CONTROLS, {**TAB, 'scope': 'everything'}, {}), 400),
    (('POST', CONTROLS + '/renew', {'token': 'T' * 43, 'scope': 'routes'}, {}), 400),
    (('POST', CONTROLS + '/revoke', {}, {}), 400),
    (('POST', CONTROLS + '/extend', {'token': 'T' * 43}, {}), 404),                # no such operation
    (('POST', CONTROLS, TAB, {}), 409),                                             # a classic card has no file lease
    (('POST', CONTROLS, {**TAB, 'generation': 'stale', 'scope': 'routes'}, {}), 409),  # a first mint needs the live one
    (('POST', CONTROLS, {**TAB, 'skill': 'memory-atlas', 'scope': 'routes'}, {}), 409),  # a card the host lists not
    (('POST', CONTROLS, {**TAB, 'tab_id': 'other', 'scope': 'routes'}, {}), 409),
    (('POST', CONTROLS + '/renew', {'token': 'T' * 43}, {}), 409),                 # never minted
])
def test_the_host_refuses_what_the_core_refuses_with_its_status(request_, status):
    host = gateway()
    method, path, body, changed = request_
    assert post(host, path, body, method=method, **{key.replace('-', '_'): value for key, value in changed.items()})[0] == status
    assert [row[0] for row in host.refused] == [status]


def test_a_token_held_by_one_card_does_not_re_establish_another():
    host = gateway()
    token = post(host, CONTROLS, {**TAB, 'scope': 'routes'})[1]['token']
    assert post(host, CONTROLS, {**OTHER, 'scope': 'routes', 'token': token})[0] == 409
    assert post(host, CONTROLS, {**TAB, 'scope': 'routes', 'token': token})[0] == 200
