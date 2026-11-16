"""A send-as replyToAddress routes every later reply to that address: the Host checks it as a destination first.

Gmail's SendAs contract accepts an empty replyToAddress to remove Reply-To. Any other value is exactly one bare
address in the declared field; another spelling of the field or a list/named address is refused before any write.
"""
import pytest

from test_declared_target_preparation import published_targets
from test_write_targets import host_guard

pytest_plugins = ["test_write_targets"]

HELD = 'held@example.invalid'


def update(account, value, **extra):
    args = {'resource': 'sendAs', 'action': 'update', 'identifier': 'owner@example.invalid',
            'values': {'displayName': 'Owner', 'replyToAddress': value, **extra}}
    return {**args, 'account_id': account} if account else args


@pytest.mark.parametrize('state', ['paused', 'silent'])
@pytest.mark.parametrize('explicit', [True, False])
def test_a_held_reply_to_destination_is_refused_before_the_sendas_update(tmp_path, monkeypatch, sole_gmail, state, explicit):
    account, call, writes = sole_gmail
    guard = host_guard(tmp_path, monkeypatch, 'email_settings', account, [('', 'active'), (HELD, state)])
    args = update(account if explicit else '', HELD)
    if not explicit and not guard(args):  # an omitted sole account is prepared, then the destination is checked
        prepared = call('email_settings', args)
        assert prepared['code'] == 'canonical_account_required' and not writes
        args = prepared['retry_arguments']
    assert published_targets('email_settings', args)['values.replyToAddress'] == [HELD]
    assert state.title() in guard(args) and not writes


@pytest.mark.parametrize('value', [HELD, ''])
def test_an_active_or_cleared_reply_to_writes_exactly_the_callers_values(tmp_path, monkeypatch, sole_gmail, value):
    account, call, writes = sole_gmail
    guard = host_guard(tmp_path, monkeypatch, 'email_settings', account, [('', 'active'), (HELD, 'active')])
    args = update(account, value)
    assert not guard(args)
    call('email_settings', args)
    assert writes == [('PUT', '/settings/sendAs/owner@example.invalid', args['values'])]


@pytest.mark.parametrize('extra,value', [
    ({'reply_to_address': HELD}, ''),          # proto JSON also accepts the field's original name
    ({'ReplyToAddress': HELD}, ''),
    ({}, 'Held <held@example.invalid>'),
    ({}, 'held@example.invalid, other@example.invalid'),
    ({}, 'held@example.invalid other@example.invalid'),
    ({}, ['held@example.invalid']),
    ({}, 7),
])
def test_hidden_or_composite_reply_to_spellings_are_refused_before_any_write(sole_gmail, extra, value):
    account, call, writes = sole_gmail
    with pytest.raises(ValueError, match='replyToAddress'):
        call('email_settings', update(account, value, **extra))
    assert not writes

@pytest.mark.parametrize('explicit', [True, False])
@pytest.mark.parametrize('values', [[["replyToAddress", HELD]], [], '', 0, False])
def test_non_object_values_cannot_hide_reply_to(sole_gmail, explicit, values):
    account, call, writes = sole_gmail
    args = update(account if explicit else '', HELD)
    args['values'] = values
    with pytest.raises(ValueError, match='values must be an object'):
        call('email_settings', args)
    assert not writes

@pytest.mark.parametrize('values', [[["action", {"forward": HELD}]], {'action': [["forward", HELD]]}])
def test_filter_preparation_rejects_non_object_values_and_actions(sole_gmail, values):
    account, call, writes = sole_gmail
    with pytest.raises(ValueError, match='object'):
        call('email_settings', {'account_id': account, 'resource': 'filters', 'action': 'create', 'values': values})
    assert not writes
