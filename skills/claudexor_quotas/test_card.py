"""The card of the picked account, beside the bars of the plate.

What is held here (Node, the fake DOM of the other widget tests; the rectangles are test_window_tabs.py's):
- the card names the account, its address whole (when it is not the name), its share of the limit on screen as one figure with its %, when that limit
  comes back (in how long and at what clock time), its plan and how old the reading is, and its one state (at the limit, held without an invented blocker or return time, last known, not read, switched off);
- its bonus resets and credits, each as the engine dated it: the offers of resources.resets (usable now / granted, not usable now / granted, usability
  unknown — a count is never "available" by itself; a reported 0 and "none offered" are said; a last-known one is dated and never "usable now"; the soonest
  grant to expire is said), else the legacy counter quota.reset_credits (one source, never both); the credit balances of resources.balances (the included
  budget is the limit itself and stays off; a team balance says so; an amount is the engine's string, scaled only by its own decimal places; an amount not
  reported is said, never 0); nothing reported draws no line at all;
- the cooldown the engine reports stands on the card's face; everything else of the account waits under one button (its readings, its resources whole);
- Clear and a second press of the picked bar let the pick go; the words of the card are in the English table and the widget asks for them.
"""
import copy
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from datetime import datetime, timedelta, timezone

import test_quotas
from test_reserve import _node, _widget_fixture

WIDGET = Path(__file__).with_name("widget.js").resolve()
STRINGS = json.loads(Path(__file__).with_name("strings_en.json").read_text(encoding="utf-8"))
def _at(**ago):
    """An instant before now (or after it, with negative values), in the engine's form; taken when a view is built, so the ages the card says are the test's own."""
    return (datetime.now(timezone.utc) - timedelta(**ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

CARD_WORDS = ["card.pick", "card.left", "card.no_reading", "card.resets_in", "card.back_in", "card.cycle_ends_in", "card.signed_out", "card.not_read",
              "card.no_reset", "card.plan", "card.read", "card.res_label", "card.bonus", "card.credits", "card.team", "card.none_zero",
              "card.none_offered", "card.offer_usable", "card.offer_usable_then", "card.offer_not_usable",
              "card.offer_not_usable_then", "card.offer_unknown",
              "card.reported_unknown", "card.offer_title", "card.expires_in", "card.last_known", "card.age_unknown", "card.unknown",
              "card.state_off", "card.state_unknown", "card.state_last", "card.state_limit", "card.state_held",
              "acc.clear_label", "acc.card_label", "acc.details", "acc.more", "acc.less"]


@pytest.fixture(scope="module")
def base_view(tmp_path_factory):
    return _widget_fixture(tmp_path_factory.mktemp("card"))["view"]


def _with(base, **resources):
    """The fixture view with the picked account (c2, the top current bar) carrying an address, a plan and the resources given."""
    view = copy.deepcopy(base)
    account = next(a for a in view["groups"][0]["accounts"] if a["key"] == "codex:c2")
    account.update(email="someone.with.a.long.address@example.test", plan="Pro")
    account["resources"] = resources or None
    if "legacy" in resources:
        account["resources"] = None
        account["quota"]["reset_credits"] = resources["legacy"]
    return view


def _facet(rows, state="current", observed=None):
    observed = observed or _at(seconds=0)
    return {"state": state, "freshness": "fresh" if state == "current" else "stale", "observed_at": observed, "source": "fixture", "rows": rows}


def _views(base):
    offer = lambda **o: dict({"id": "granted", "label": "Granted reset", "available_count": 2, "eligible": True, "usable_now": None,
                              "reason": "", "resets_at": None, "weekly_limit_applies": None, "grants": []}, **o)
    balance = lambda **b: dict({"id": "credits", "label": "Credits", "amount": "1250", "unit": "credits", "currency": None,
                                "decimal_places": None, "has_balance": True, "unlimited": False, "scope": "personal", "usage_kind": "bonus"}, **b)
    return {
        "plain": _with(base),
        "usable": _with(base, resets=_facet([offer(usable_now=True, grants=[{"available_count": 1, "expires_at": _at(days=-3, hours=-4)}])])),
        "not_usable": _with(base, resets=_facet([offer(usable_now=False)])),
        "unknown_use": _with(base, resets=_facet([offer()])),
        "zero": _with(base, resets=_facet([offer(available_count=0)])),
        "none_offered": _with(base, resets=_facet([])),
        "kept": _with(base, resets=_facet([offer(usable_now=True)], state="last_known", observed=_at(days=1))),
        "kept_not_usable": _with(base, resets=_facet([offer(usable_now=False)], state="last_known", observed=_at(days=1))),
        # one offer whose reported reset passed is last known on its own, in a facet read now (quota_resources)
        "row_passed": _with(base, resets=_facet([offer(usable_now=True, state="last_known", resets_at=_at(hours=1),
                                                       note="last known: reported boundary passed")])),
        "not_reported": _with(base, resets={"state": "not_reported", "rows": []}, balances={"state": "not_reported", "rows": []}),
        "legacy": _with(base, legacy={"state": "current", "count": 3, "observed_at": _at(seconds=0), "source": "fixture", "label": "", "reason": ""}),
        "both": dict(_with(base, resets=_facet([offer(usable_now=True)])), legacy_count=3),
        "money": _with(base, balances=_facet([
            balance(id="usd", label="Extra usage", amount="2500", unit="minor", currency="USD", decimal_places=2),
            balance(id="team", label="Team pool", amount="900", unit="minor", currency="EUR", decimal_places=2, scope="team"),
            balance(id="included", label="Included", amount="2000", unit="minor", currency="USD", decimal_places=2, usage_kind="included"),
            balance(id="raw", label="Odd", amount="77", unit="minor", currency="USD", decimal_places=None),
            balance(id="gone", label="Unknown amount", amount=None),
            balance(id="credits")])),
    }


NODE_CARD = r"""
const has = (n, name) => String(n.className || '').split(/\s+/).includes(name);
const click = (env, key) => { const node = byFocus(env.root, key); assert.ok(node, 'missing control ' + key); node.listeners.click[0]({ stopPropagation() {} }); };
const card = (env) => classes(env.root, 'q-card')[0];
const res = (env) => { const r = classes(env.root, 'q-res'); return r.length ? r[0].textContent : ''; };
const settle2 = async () => { for (let i = 0; i < 6; i++) await settle(); };

(async () => {
  const views = JSON.parse(require('fs').readFileSync(process.env.CARD_VIEWS_FILE, 'utf8'));
  const both = views.both;
  // a view that carries the offers and the legacy counter at once: the card reads the offers alone
  both.groups[0].accounts.find((a) => a.key === 'codex:c2').quota.reset_credits = { state: 'current', count: both.legacy_count, observed_at: views.legacy.groups[0].accounts[1].quota.reset_credits.observed_at, source: 'fixture', label: '', reason: '' };
  let env = await boot(views.plain);
  const c = card(env);
  assert.ok(c && !has(c, 'q-card-none'), 'the top account is picked: its card stands');
  const text = c.textContent;
  // the name, the whole address, the share as one figure with its %, when the limit comes back, the plan and the age
  assert.match(text, /c2/);
  assert.equal(classes(c, 'q-em')[0].textContent, 'someone.with.a.long.address@example.test', 'the address whole');
  assert.match(classes(c, 'q-big')[0].textContent, /^\d+%$/, 'the share and its % are one figure');
  assert.match(text, /Resets in (\d+d )?\d+[hm]/);
  assert.match(text, /\d\d:\d\d/);
  assert.match(text, /Plan: Pro · Read /);
  assert.equal(res(env), '', 'nothing reported: no line of resets or credits');
  assert.equal(classes(c, 'q-st').length, 0, 'a current account with room left wears no state');
  // the details wait under one button, and open the account's readings
  assert.equal(byFocus(env.root, 'inspector-details').getAttribute('aria-expanded'), 'false');
  assert.equal(classes(c, 'win-line').length, 0);
  click(env, 'inspector-details');
  assert.equal(byFocus(env.root, 'inspector-details').getAttribute('aria-expanded'), 'true');
  assert.ok(classes(card(env), 'win-line').length > 0, 'the readings under the button');
  // Clear lets the pick go: the card asks for one
  click(env, 'inspector-clear');
  await settle2();
  assert.ok(has(card(env), 'q-card-none'), 'the pick is let go');
  assert.match(card(env).textContent, /Pick an account/);
  env.disposeHooks[0]();

  const line = async (name) => { const e = await boot(views[name]); const out = res(e); e.disposeHooks[0](); return out; };
  assert.match(await line('usable'), /^Bonus resets: 2 usable now · next expires in 3d \d+h · just now$/);
  assert.match(await line('not_usable'), /^Bonus resets: 2 granted · not usable now · just now$/);
  const unknown = await line('unknown_use');
  assert.match(unknown, /^Bonus resets: 2 granted · usability unknown · just now$/, 'a count with unknown usability is granted, never available');
  assert.doesNotMatch(unknown, /available|usable now/);
  // the account's details say the same of the offer: granted, usability unknown, never "available"
  env = await boot(views.unknown_use);
  click(env, 'inspector-details');
  const whole = classes(env.root, 'account-resources')[0].textContent;
  assert.match(whole, /Granted reset: 2 granted · usability unknown/);
  assert.doesNotMatch(whole, /available/);
  env.disposeHooks[0]();
  assert.match(await line('zero'), /^Bonus resets: none \(0\) · just now$/, 'a reported 0 is a zero');
  assert.match(await line('none_offered'), /^Bonus resets: none offered · just now$/);
  const kept = await line('kept');
  assert.match(kept, /^Bonus resets: 2 usable when read · last known · (24 h|1 d) ago$/, 'a kept offer is dated, never usable now: ' + kept);
  assert.equal(await line('not_reported'), '', 'not reported is no line, never 0');
  assert.match(await line('legacy'), /^Bonus resets: 3 reported · usability unknown · just now$/, 'the legacy counter where no offer is reported');
  env = await boot(both);
  assert.equal((res(env).match(/Bonus resets/g) || []).length, 1, 'one source, never both');
  assert.match(res(env), /^Bonus resets: 2 usable now · just now$/);
  env.disposeHooks[0]();
  const money = await line('money');
  assert.match(money, /Extra usage: 25\.00 USD · just now/, 'an amount in minor units scaled by its own places, with its currency');
  assert.match(money, /Team pool: 9\.00 EUR · team · just now/, 'a team balance says so');
  assert.doesNotMatch(money, /Included/, 'the included budget is the limit itself');
  assert.match(money, /Odd: 77 USD minor units \(scale not reported\)/, 'no scale: shown as sent, never guessed');
  assert.match(money, /Unknown amount: amount not reported/, 'an amount not reported is said, never 0');
  assert.match(money, /Credits: 1250 credits/, 'another unit as sent');

  // the states: at the limit and held without inventing a blocking scope
  const states = JSON.parse(JSON.stringify(views.plain));
  const groups = states.reserve.summary.groups;
  const bar = groups[0].bars.find((b) => b.account === 'codex:c2');
  bar.at_limit = true; bar.left = 0;
  env = await boot(states);
  click(env, 'bar:' + groups[0].key + ':codex:c2');
  await settle2();
  assert.equal(classes(card(env), 'q-st-limit')[0].textContent, 'At limit');
  assert.match(card(env).textContent, /Back in /);
  env.disposeHooks[0]();
  const held = JSON.parse(JSON.stringify(views.plain));
  const hg = held.reserve.summary.groups;
  hg[0].bars.find(b => b.account === 'codex:c2').restricted = true;
  hg[0].bars.find(b => b.account === 'codex:c2').flags = ['other_limit_spent'];
  const other = JSON.parse(JSON.stringify(hg[0]));
  other.key += ':unrelated'; other.label = 'Fable'; other.models = ['fable'];
  other.bars.forEach(b => { b.at_limit = true; b.left = 0; b.resets_at = new Date(Date.now() + 7200000).toISOString(); });
  hg.push(other);
  env = await boot(held);
  click(env, 'bar:' + hg[0].key + ':codex:c2');
  assert.equal(classes(card(env), 'q-st-held')[0].textContent, 'Held back', 'no invented blocker or return time');
  env.disposeHooks[0]();
  const absent = JSON.parse(JSON.stringify(views.plain));
  absent.reserve.summary.groups[0].bars = absent.reserve.summary.groups[0].bars.filter(b => b.account !== 'codex:c2');
  env = await boot(absent);
  click(env, 'accounts'); click(env, 'acct:codex:c2');
  assert.match(card(env).textContent, /may not apply/);
  assert.doesNotMatch(card(env).textContent, /Its window is not read/);
  env.disposeHooks[0]();
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exitCode = 1; });
"""


NODE_STALE_OFFERS = r"""
const click = (env, key) => { const node = byFocus(env.root, key); assert.ok(node, 'missing control ' + key); node.listeners.click[0]({ stopPropagation() {} }); };
const res = (env) => { const r = classes(env.root, 'q-res'); return r.length ? r[0].textContent : ''; };
// The account's details, opened: the resources whole, as the details say them.
const details = (env) => {
  const button = byFocus(env.root, 'inspector-details');
  if (button.getAttribute('aria-expanded') !== 'true') click(env, 'inspector-details');
  return classes(env.root, 'account-resources')[0].textContent;
};

(async () => {
  const views = JSON.parse(require('fs').readFileSync(process.env.CARD_VIEWS_FILE, 'utf8'));
  // A last-known offer was usable when it was read: the card and the details say so, neither says "now".
  let env = await boot(views.kept);
  assert.match(res(env), /^Bonus resets: 2 usable when read · last known · (24 h|1 d) ago$/);
  let whole = details(env);
  assert.match(whole, /Reset offers · last known · observed .+, not current/);
  assert.match(whole, /Granted reset: 2 usable when read/);
  assert.doesNotMatch(whole, /usable now|not usable now/, whole);
  env.disposeHooks[0]();
  env = await boot(views.kept_not_usable);
  assert.match(res(env), /^Bonus resets: 2 granted · not usable when read · last known · (24 h|1 d) ago$/);
  whole = details(env);
  assert.match(whole, /Granted reset: 2 granted · not usable when read/);
  assert.doesNotMatch(whole, /usable now/, whole);
  env.disposeHooks[0]();
  // One offer whose reported reset passed is last known in a facet read now: said "when read" in both places.
  env = await boot(views.row_passed);
  assert.match(res(env), /^Bonus resets: 2 usable when read · last known · just now$/);
  whole = details(env);
  assert.match(whole, /Granted reset: 2 usable when read · last known: reported boundary passed/);
  assert.doesNotMatch(whole, /usable now/, whole);
  env.disposeHooks[0]();
  // A reading that fails after a good one: the kept screen says nothing in it is current, and its resources too are
  // last known — dated by their own observation, never "usable now", in the card and in the details.
  let failed = false;
  env = await boot(() => (failed ? null : views.usable));
  assert.match(res(env), /^Bonus resets: 2 usable now · next expires in 3d \d+h · just now$/);
  assert.match(details(env), /Granted reset: 2 usable now/);
  failed = true;
  env.interval()();
  await settle(); await settle();
  assert.match(env.root.textContent, /nothing in it is current/);
  assert.match(res(env), /^Bonus resets: 2 usable when read · last known · just now$/, res(env));
  whole = details(env);
  assert.match(whole, /Reset offers · last known · observed .+, not current/);
  assert.match(whole, /Granted reset: 2 usable when read/);
  assert.doesNotMatch(whole + res(env), /usable now/);
  // the money and the counts stay as read: a reported count and an unknown usability are kept, nothing invented
  failed = false;
  env.interval()();
  await settle(); await settle();
  assert.match(res(env), /^Bonus resets: 2 usable now · next expires in 3d \d+h · just now$/, 'a good reading is current again');
  env.disposeHooks[0]();
  env = await boot((url, calls) => (calls.length > 1 ? null : views.unknown_use));
  env.interval()();
  await settle(); await settle();
  assert.match(res(env), /^Bonus resets: 2 granted · usability unknown · last known · just now$/);
  assert.match(details(env), /Granted reset: 2 granted · usability unknown/);
  env.disposeHooks[0]();
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exitCode = 1; });
"""


def test_node_a_last_known_offer_is_never_usable_now_in_the_card_or_the_details(base_view, tmp_path):
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    fixture_file = tmp_path / "stale-offer-views.json"
    fixture_file.write_text(json.dumps(_views(base_view)), encoding="utf-8")
    widget = Path(os.environ.get("QUOTAS_REVIEW_WIDGET_PATH", WIDGET))
    result = subprocess.run([str(node), "-e", harness + NODE_STALE_OFFERS], cwd=WIDGET.parent,
                            env={**os.environ, "WIDGET_PATH": str(widget), "CARD_VIEWS_FILE": str(fixture_file)},
                            capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_node_the_card_of_the_picked_account(base_view, tmp_path):
    # The views are built here, at the moment the card is drawn: an age the card says ("just now") is measured from this moment.
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    fixture_file = tmp_path / "card-views.json"
    fixture_file.write_text(json.dumps(_views(base_view)), encoding="utf-8")
    result = subprocess.run([str(node), "-e", harness + NODE_CARD], cwd=WIDGET.parent,
                            env={**os.environ, "WIDGET_PATH": str(WIDGET), "CARD_VIEWS_FILE": str(fixture_file)},
                            capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_words_of_the_card_are_in_the_english_table_and_the_widget_asks_for_them():
    widget = WIDGET.read_text(encoding="utf-8")
    for key in CARD_WORDS:
        assert key in STRINGS, key
        assert ("t('%s'" % key) in widget or ("'%s'" % key) in widget, "the widget never asks for %s" % key
    # A count with unknown usability is "granted", never "available"; a usable one says it; a kept one says when it was read.
    assert STRINGS["card.offer_unknown"] == "{n} granted · usability unknown" and "available" not in STRINGS["card.offer_unknown"]
    assert STRINGS["card.offer_usable"] == "{n} usable now" and STRINGS["card.offer_usable_then"] == "{n} usable when read"


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_limit_explanations_follow_bars_and_remain_open_on_account_redraw(base_view, engine):
    from test_family_tabs import _open
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, engine, _with(base_view), 320, has_touch=True, reduced_motion="reduce")
        try:
            notes = page.locator(".q-limit-details")
            assert notes.count() == 1
            assert not notes.evaluate("n => n.open")
            assert page.locator(".q-status .q-note").count() == 0
            assert page.locator(".q-head .q-stamp").is_visible()
            assert notes.locator(".q-note").count() > 0
            assert page.evaluate("document.querySelector('.q-accts').compareDocumentPosition(document.querySelector('.q-limit-details')) & Node.DOCUMENT_POSITION_FOLLOWING")
            notes.locator("summary").focus()
            page.keyboard.press("Enter")
            page.wait_for_function("document.querySelector('.q-limit-details').open")
            assert notes.locator(".q-note").first.is_visible()
            full_text = notes.text_content()
            page.click('[data-focus="inspector-clear"]')
            page.wait_for_selector(".q-card-none")
            assert page.locator(".q-limit-details").evaluate("n => n.open")
            assert page.locator(".q-limit-details").text_content() == full_text
            assert page.evaluate("matchMedia('(pointer: coarse)').matches")
            widths = page.locator(".q-bar").evaluate_all("ns => ns.map(n => n.getBoundingClientRect().width)")
            assert widths and min(widths) >= 28
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            browser.close()


@pytest.mark.parametrize('scope', ['model', 'request_kind'])
def test_card_does_not_infer_hold_cause_from_unrelated_scope(tmp_path, scope):
    import test_window_tabs as W

    def constraints(now, _i):
        short = W._row(now, 'five_hour', '5h', .2, 18000)
        week = W._row(now, 'seven_day', 'Weekly', 1, W.WEEK)
        week['resets_at'] = W.qs.iso(now + 72 * 3600)
        unrelated = W._row(now, 'special', 'Special', 1, W.WEEK, ['fable'] if scope == 'model' else None)
        if scope == 'request_kind':
            unrelated['applies_to_request_kinds'] = ['image']
        unrelated['resets_at'] = W.qs.iso(now + 2 * 3600)
        return [short, week, unrelated]

    view = W._family(tmp_path, 'claude', constraints, accounts=('s1',))
    group = next(g for g in view['reserve']['summary']['groups'] if g['window_seconds'] == 18000)
    assert group['bars'][0]['restricted'] and group['bars'][0]['left'] == .8
    harness = test_quotas.NODE_WIDGET_MATRIX.split('(async () => {')[0]
    script = r'''
(async () => {
 const view = JSON.parse(process.env.TEST_VIEW);
 const g = view.reserve.summary.groups.find(g => g.window_seconds === 18000);
 const env = await boot(view);
 byFocus(env.root, 'ring:claude:' + g.key).listeners.click[0]({stopPropagation(){}});
 await settle();
 const card = classes(env.root, 'q-card')[0];
 assert.match(card.textContent, /80%/);
 assert.equal(classes(card, 'q-st-held')[0].textContent, 'Held back');
 assert.doesNotMatch(classes(card, 'q-st-held')[0].textContent, /Special|2h|Weekly/);
 env.disposeHooks[0]();
})().catch(e => { console.error(e.stack || e); process.exit(1); });
'''
    result = subprocess.run([str(_node()), '-e', harness + script], cwd=WIDGET.parent,
                            env={**os.environ, 'WIDGET_PATH': os.environ.get('QUOTAS_REVIEW_WIDGET_PATH', str(WIDGET)),
                                 'TEST_VIEW': json.dumps(view)}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def _caps_and_offers_view():
    """The real backend's account view (plugin.build_view) of four Antigravity accounts: a spent gemini-* limit scoped
    by prefix alone, a spent cap of image requests only, a spent limit of every text request, and one with room; the
    first reports a last-known reset offer, the last a current one."""
    import time as _time
    import plugin
    from test_reserve import payload, profile
    now = _time.time()
    iso = plugin.qs.iso

    def snap(sid, capped):
        rows = [{"id": "shared", "label": "Shared", "used_ratio": 0.2, "window_seconds": 604800, "resets_at": iso(now + 86400)}]
        if capped is not None:
            rows.insert(0, dict({"id": "capped", "label": "Capped", "used_ratio": 1.0, "window_seconds": 18000,
                                 "resets_at": iso(now + 3600)}, **capped))
        return {"subject": {"harness": "agy", "subject_id": sid}, "constraints": rows,
                "availability": {"state": "available"}, "observed_at": iso(now - 60), "freshness": "fresh", "source": "fixture"}

    def facet(value, freshness, observed):
        return {"value": value, "source": "fixture", "observed_at": iso(observed), "freshness": freshness,
                "last_attempt_at": None, "last_error": None}

    offer = {"id": "granted", "kind": "granted_reset", "label": "Granted reset", "available_count": 2, "eligible": True,
             "usable_now": True, "reason": None, "resets_at": None, "weekly_limit_applies": None, "grants": None}
    accounts = {"prefix": {"applies_to_model_prefixes": ["gemini-"]}, "image": {"applies_to_request_kinds": ["image"]},
                "text": {"applies_to_request_kinds": ["text"]}, "plain": None}
    data = payload([snap(sid, capped) for sid, capped in accounts.items()], [profile("agy", sid) for sid in accounts],
                   harnesses=("agy",))
    data["resources"] = [{"target": {"harness": "agy", "profile_id": sid},
                          "balances": facet(None, "unknown", now), "spending": facet(None, "unknown", now),
                          "resets": facet([offer], freshness, now - age), "diagnostics": facet([], "fresh", now)}
                         for sid, freshness, age in (("prefix", "stale", 7200), ("plain", "fresh", 30))]
    return plugin.build_view(data, "", now)


CAPS_STUB = r"""
(view) => {
  window.__fail = false;
  window.fetch = (url, options = {}) => {
    const post = ((options && options.method) || 'GET') !== 'GET';
    const body = post ? { ok: true, quota_updates: [] } : (window.__fail ? null : view);
    return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(body) });
  };
}
"""


def test_browser_caps_and_last_known_offers_read_true_in_real_engines(browser):
    from test_reserve import with_i18n
    view = _caps_and_offers_view()
    page = browser.new_page(viewport={"width": 1120, "height": 1200})
    try:
        page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
        page.evaluate(with_i18n(CAPS_STUB), view)
        page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
        page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
        state = lambda sid: page.locator(f'[data-focus="acct:agy:{sid}"] .acc-state').inner_text().strip()
        dot = lambda sid: page.locator(f'[data-focus="acct:agy:{sid}"] .state-dot').first.get_attribute("class")
        assert state("prefix") == "a model at its limit" and "warn" in dot("prefix").split()
        assert state("image") == "a limit on some requests reached" and "warn" in dot("image").split()
        assert state("text") == "limit reached" and "bad" in dot("text").split()
        assert state("plain") == "ready"
        page.click('[data-focus="acct:agy:prefix"]')
        inspector = page.locator(".inspector")
        assert "20% used" in inspector.inner_text() and "Limit reached" not in inspector.inner_text()
        resources = page.locator(".inspector .account-resources").inner_text()
        assert "Granted reset: 2 usable when read" in resources and "usable now" not in resources, resources
        page.click('[data-focus="acct:agy:plain"]')
        assert "Granted reset: 2 usable now" in page.locator(".inspector .account-resources").inner_text()
        # the next reading fails: the kept screen is nothing current, its resources too
        page.evaluate("() => { window.__fail = true; }")
        page.click('[data-focus="refresh"]')
        page.wait_for_function("() => /nothing in it is current/.test(document.body.innerText)", timeout=10000)
        kept = page.locator(".inspector .account-resources").inner_text()
        assert "Granted reset: 2 usable when read" in kept and "usable now" not in kept, kept
        assert "last known" in kept and "not current" in kept
    finally:
        page.close()
