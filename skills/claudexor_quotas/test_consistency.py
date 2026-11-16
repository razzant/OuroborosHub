"""Supported consumers agree on scope and per-row freshness. Synthetic data only."""
import copy
import json
import os
import time
from pathlib import Path

import pytest

import plugin
import quota_context as qc
import quota_history
import quota_resources as qr
import quota_scope
import quota_summary as qs
from test_part_holds import _window, _snap, _flags, _account, _run_node, _page, _shot, FIVE_H, IMAGE, TEXT, GEMINI
from test_reserve import payload, profile
from test_scope_resources_context import facet

NOW = 1794729600.0


def data_of(windows, now=NOW, **kw):
    return payload([_snap('codex', 'a', windows, now, **kw)], [profile('codex', 'a')], harnesses=('codex',))


def window(cid, scope, ratio=.2, now=NOW):
    return _window(cid, ratio, FIVE_H, 7200, now, **scope)


@pytest.mark.parametrize('kind,other', [('text', 'image'), ('image', 'text')])
@pytest.mark.parametrize('scope', [{}, {'applies_to_models': ['gpt-5']}, GEMINI,
                                   {'applies_to_models': 'unreadable'}])
def test_cooldowns_keep_kinds_in_all_current_consumers(kind, other, scope):
    a = dict(scope, applies_to_request_kinds=[kind])
    b = dict(scope, applies_to_request_kinds=[other])
    hold = dict(a, id='cool', cooldown_until=qs.iso(NOW + 600))
    data = data_of([window('held', a), window('free', b), hold])
    summary = qs.build_summary(data, qs.HistoryView(state='empty'), NOW, name_accounts=True)
    assert 'cooling' in _flags(summary, 'codex', 'held', 'a')
    assert 'cooling' not in _flags(summary, 'codex', 'free', 'a')
    answer = plugin.answer_of(None, data, None, data, NOW, NOW)
    text = qc.snapshot_text(answer)
    assert 'cooling' not in next(line for line in text.splitlines() if 'free' in line)
    quota = _account(plugin.build_view(data, '', NOW), 'codex', 'a')['quota']
    if not scope:
        assert quota['state'] == ('cooling' if kind == 'text' else 'ok')
    assert quota['cooldowns'][0]['scope_key'] == next(c['scope_key'] for c in quota['constraints'] if c['id'] == 'held')


@pytest.mark.parametrize('spent,held', [(TEXT, ['text']), (IMAGE, ['image']), ({}, ['text', 'image', 'both']),
                                       ({'applies_to_models': 'gpt-5'}, []),
                                       ({'applies_to_model_prefixes': 'gemini-'}, []),
                                       ({'applies_to_request_kinds': ['video']}, []),
                                       ({'applies_to_request_kinds': []}, []), (GEMINI, []),
                                       ({'applies_to_models': ['gpt-5']}, [])])
def test_spent_other_window_requires_complete_known_kind_coverage(spent, held):
    data = data_of([window('spent', spent, 1), window('text', TEXT), window('image', IMAGE), window('both', {})])
    summary = qs.build_summary(data, qs.HistoryView(state='empty'), NOW, name_accounts=True)
    for cid in ['text', 'image', 'both']:
        assert ('other_limit_spent' in _flags(summary, 'codex', cid, 'a')) == (cid in held)
    answer = plugin.answer_of(None, data, None, data, NOW, NOW)
    for row in answer['groups']:
        if row['key'].split('|')[1] in ['text', 'image', 'both']:
            assert bool(row.get('restrictions')) == (row['key'].split('|')[1] in held)


@pytest.mark.parametrize('scope', [TEXT, IMAGE, dict(GEMINI, **IMAGE),
                                   {'applies_to_models': ['gpt-5'], **TEXT}])
@pytest.mark.parametrize('historical_group', [False, True])
@pytest.mark.parametrize('hold_kind', ['cooldown', 'exhaustion'])
def test_history_restoration_keeps_matching_live_cooldown_without_changing_series(tmp_path, scope, historical_group, hold_kind):
    store = quota_history.HistoryStore(tmp_path)
    rows = [_snap('codex', sid, [window('window', scope)], NOW - 120) for sid in ('a', 'b')]
    profiles = [profile('codex', sid) for sid in ('a', 'b')]
    old = payload(rows, profiles, harnesses=('codex',))
    plugin.persist_sweep(store, old, '', NOW - 120)
    key = qs.normalize(old, NOW).readings[0].key
    rows = [_snap('codex', 'a', [dict(scope, id='cool', cooldown_until=qs.iso(NOW + 600))], NOW)]
    if hold_kind == 'exhaustion':
        rows[0]['constraints'] = []
        rows[0]['availability']['model_scoped_exhaustions'] = [dict(scope, resets_at=qs.iso(NOW + 600))]
    if not historical_group:
        rows.append(_snap('codex', 'b', [window('window', scope)], NOW))
    data = payload(rows, profiles, harnesses=('codex',))
    out = plugin.reserve_view(store, data, NOW, NOW, name_accounts=True)
    group = next(g for g in out['summary']['groups'] if g['key'] == key)
    bar = next(b for b in group['bars'] if b['account'] == 'codex:a')
    assert bar['origin'] == 'history' and bar['state'] == 'last_known'
    assert ('cooling' if hold_kind == 'cooldown' else 'model_exhausted') in bar['flags']
    if historical_group:
        assert group['scope'] is None  # no invented field restoration
        assert group['model_scope'] == ('names_unknown' if quota_scope.scope_of(scope).scoped else 'none')


def test_historical_unreadable_scope_is_not_reconstructed_as_universal(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    data = data_of([window('odd', {'applies_to_models': 'unreadable'})])
    plugin.persist_sweep(store, data, '', NOW - 120)
    now_data = data_of([dict(id='cool', applies_to_models='unreadable', cooldown_until=qs.iso(NOW + 600))])
    out = plugin.reserve_view(store, now_data, NOW, NOW, name_accounts=True)
    group = out['summary']['groups'][0]
    assert group['scope'] is None  # legacy key cannot retain the unreadable field
    assert not group['bars'][0]['flags']  # cannot prove the scopes match


def test_engine_own_cooldown_still_holds_every_kind():
    data = data_of([window('text', TEXT), window('image', IMAGE)], availability={'state': 'cooldown'})
    summary = qs.build_summary(data, qs.HistoryView(state='empty'), NOW, name_accounts=True)
    for kind in ['text', 'image']:
        assert 'cooling' in _flags(summary, 'codex', kind, 'a')


def resource_data(now=NOW):
    data = data_of([window('shared', {}, now=now), window('primary', GEMINI, now=now),
                    window('images', IMAGE, now=now)], now)
    # Labels deliberately do not encode model family or modality.
    for c in data['quota'][0]['constraints']:
        c['label'] = '5 hours'
    def offer(cid, seconds):
        return dict(id=cid, label=cid, available_count=2, usable_now=True,
                    grants=[dict(available_count=2, expires_at=qs.iso(now + seconds))])
    resources = dict(target={'harness': 'codex', 'profile_id': 'a'},
                     resets=facet([offer('Expired', -1), offer('Current', 3600)]),
                     balances=facet([dict(id='Old balance', amount='20', unit='credits', period={'ends_at': qs.iso(now - 1)}),
                                     dict(id='New balance', amount='30', unit='credits')]))
    for name in ['resets', 'balances']:
        resources[name]['observed_at'] = qs.iso(now - 60)
    data['resources'] = [resources]
    return data


def test_mixed_resources_date_each_old_cohort_and_qualify_usability():
    data = resource_data()
    other = copy.deepcopy(data['resources'][0])
    other['target']['profile_id'] = 'b'
    for name in ['balances', 'resets']:
        other[name]['observed_at'] = qs.iso(NOW - 120)
    data['resources'].append(other)
    answer = plugin.answer_of(None, data, None, data, NOW, NOW)
    text = qc.snapshot_text(answer)
    old = [line for line in text.splitlines() if 'Expired' in line or 'Old balance' in line]
    assert len(old) == 4
    assert all('last known observed' in line and '; current' not in line for line in old)
    assert all('usable yes when read' in line for line in old if 'Expired' in line)
    assert all(line.count('reported boundary passed') == 1 for line in old if 'Old balance' in line)
    for date in [qs.iso(NOW - 60), qs.iso(NOW - 120)]:
        assert any(date in line for line in old)
    assert '(2 accounts)' in next(line for line in text.splitlines() if 'Current count' in line)


def test_display_omissions_are_not_a_complete_reset_total():
    data = resource_data()
    offers = [dict(id=str(i), available_count=2, usable_now=True) for i in range(13)]
    data['resources'][0]['resets']['value'] = offers
    result = qr.family_resets(data, 'codex', now=NOW)
    assert result['available'] == 24 and result['offers_uncounted'] == 1
    text = qc.snapshot_text(plugin.answer_of(None, data, None, data, NOW, NOW))
    assert '24 (partial subtotal)' in text and '1 offers uncounted' in text


def ui_view(now):
    data = resource_data(now)
    offer = data['resources'][0]['resets']['value'][1]
    offer['grants'] = [dict(available_count=1, expires_at=qs.iso(now + 5 * 86400)) for _ in range(12)]
    offer['grants'].append(dict(available_count=None, usable_now=True, expires_at=qs.iso(now + 3600)))
    view = plugin.build_view(data, '', now)
    view['reserve'] = plugin.reserve_view(None, data, now, now, name_accounts=True, analytics=True)
    return view


NODE_CHECK = r"""
(async () => {
  const env = await open('codex');
  const scopes = classes(env.root, 'ring-pick');
  const image = walk(env.root).find(n => n.getAttribute && /image only/.test(n.getAttribute('aria-label') || '') && n.listeners && n.listeners.click);
  assert.ok(image, 'image scope is named in the reserve');
  image.listeners.click[0]({stopPropagation(){}}); await settle();
  assert.match(classes(env.root, 'q-scope')[0].textContent, /image only/);
  assert.doesNotMatch(classes(env.root, 'q-scope')[0].textContent, /All models|only only|5 hours \(2\)/);
  assert.match(classes(env.root, 'q-res').map(n => n.textContent).join(' '), /next expires in (59m|1h)/);
  const prefix = walk(env.root).find(n => n.getAttribute && /gemini-\*/.test(n.getAttribute('aria-label') || '') && n.listeners && n.listeners.click);
  assert.ok(prefix, 'prefix scope is named in the reserve');
  prefix.listeners.click[0]({stopPropagation(){}}); await settle();
  assert.match(classes(env.root, 'q-scope')[0].textContent, /gemini-\*/);
  assert.doesNotMatch(classes(env.root, 'q-scope')[0].textContent, /All models/);
  env.disposeHooks[0]();
})().catch(error => {console.error(error.stack); process.exitCode = 1;});
"""


def test_node_scope_and_canonical_grant_consumer(tmp_path):
    view = ui_view(time.time())
    path = tmp_path / 'view.json'
    path.write_text(json.dumps(view))
    _run_node(str(path), NODE_CHECK)


def test_real_scope_and_mixed_resources(browser):
    page = _page(browser, ui_view(time.time()))
    try:
        page.locator('button').filter(has_text='image only').first.click()
        assert 'image only' in page.locator('.q-scope').first.inner_text()
        assert 'only only' not in page.locator('.q-scope').first.inner_text()
        page.locator('[data-focus="inspector-details"]').click()
        resources = page.locator('.account-resources').inner_text()
        assert 'usable when read' in resources and 'Current: 2 usable now' in resources
        assert 'observed' in resources.lower()
        _shot(page, browser, 'consistency-images-resources')
        page.locator('button').filter(has_text='gemini-*').first.click()
        assert 'gemini-*' in page.locator('.q-scope').first.inner_text()
        _shot(page, browser, 'consistency-prefix')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    finally:
        page.close()


@pytest.mark.parametrize('canonical', ['absent', 'none'])
def test_node_grant_expiry_legacy_fallback_is_only_for_omitted_field(tmp_path, canonical):
    view = ui_view(time.time())
    offer = view['groups'][0]['accounts'][0]['resources']['resets']['rows'][1]
    offer['grants'] = [{'available_count': None, 'usable_now': True, 'expires_at': qs.iso(time.time() + 3600)}]
    if canonical == 'absent':
        offer.pop('grants_expire_at')
    else:
        offer['grants_expire_at'] = None
    path = tmp_path / 'view.json'
    path.write_text(json.dumps(view))
    expectation = 'match' if canonical == 'absent' else 'doesNotMatch'
    _run_node(str(path), r"""
(async () => {
  const env = await open('codex');
  const text = classes(env.root, 'q-res').map(n => n.textContent).join(' ');
  assert.EXPECTATION(text, /next expires in (59m|1h)/);
  env.disposeHooks[0]();
})().catch(error => {console.error(error.stack); process.exitCode = 1;});
""".replace('EXPECTATION', expectation))


@pytest.mark.parametrize('kind', ['text', 'image'])
@pytest.mark.parametrize('hold', ['cooldown', 'spent'])
def test_node_account_window_meters_do_not_inherit_default_text_verdict(tmp_path, kind, hold):
    now = time.time()
    cap = dict(applies_to_request_kinds=[kind])
    windows = [window('Text', TEXT, now=now), window('Image', IMAGE, now=now)]
    windows.append(window('Cap', cap, ratio=1 if hold == 'spent' else .1, now=now))
    if hold == 'cooldown':
        windows[-1]['cooldown_until'] = qs.iso(now + 600)
    view = plugin.build_view(data_of(windows, now), '', now)
    path = tmp_path / 'view.json'
    path.write_text(json.dumps(view))
    _run_node(str(path), r"""
(async () => {
  const env = await open('codex');
  click(env, 'acct:codex:a'); await settle();
  const rows = classes(env.root, 'win-line');
  for (const label of ['Text','Image']) {
    const row = rows.find(n => n.textContent.startsWith(label));
    assert.ok(row, rows.map(n=>n.textContent).join(' / '));
    const held = classes(row, 'meter').some(n => /restricted/.test(n.className));
    assert.equal(held, label.toLowerCase() === 'KIND', label + ' scope');
  }
  env.disposeHooks[0]();
})().catch(error => {console.error(error.stack); process.exitCode = 1;});
""".replace('KIND', kind))


@pytest.mark.parametrize('named', [False, True])
def test_node_colliding_provider_labels_keep_prefixes_and_request_kinds(tmp_path, named):
    now = time.time()
    rows = [window(str(i), dict(applies_to_model_prefixes=[prefix],
                               applies_to_models=['same'] if named else [], **IMAGE), now=now)
            for i, prefix in enumerate(['gemini-', 'claude-'])]
    for row in rows:
        row['label'] = 'Pool 5h'
    data = data_of(rows, now)
    view = plugin.build_view(data, '', now)
    view['reserve'] = plugin.reserve_view(None, data, now, now, name_accounts=True, analytics=True)
    path = tmp_path / 'view.json'
    path.write_text(json.dumps(view))
    _run_node(str(path), r"""
(async () => {
  const env = await open('codex');
  const names = classes(env.root, 'q-ring-name').map(n => n.textContent);
  assert.ok(names.some(n => /gemini-\*/.test(n)), names.join(' / '));
  assert.ok(names.some(n => /claude-\*/.test(n)), names.join(' / '));
  assert.ok(names.every(n => /image only/.test(n)), names.join(' / '));
  env.disposeHooks[0]();
})().catch(error => {console.error(error.stack); process.exitCode = 1;});
""")
