"""Holds on part of an account, from the engine's answer to the widget's words.

A cooldown or a model limit the engine reports apart from a window's share holds exactly the requests of its scope, and
one identity of that whole scope (quota_scope.hold_key) ties it to windows in the reserve, the account view and the
widget alike:

- a cooldown of image requests only, or of a scope that cannot be read, holds its own window (the one of exactly that
  scope), never the account: the account's text window with room stays unrestricted, the account is not "Cooling down"
  and its dot is not red; it reads "a limit on some requests held" and its cooldown "Cooling down · some requests";
- the real whole-account cooldowns are unchanged: the snapshot's own cooldown state, a cooldown with no scope, a cooldown
  governing both kinds, with a readable or an unreadable end; text-only retains the default-text account verdict;
- a cooldown of some models, by name or by family prefix alone ("gemini-"), and a model limit the engine reports out for
  a family prefix hold the model window of exactly that scope: its line says so (from a stale reading too), the account
  names the scope ("gemini-*", never "models not named"), and the prefixes travel with the hold.

Everything here is synthetic (the engine's shapes, schema/quota.js); nothing reads a real account.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

import plugin
import quota_scope
import quota_summary as qs
import test_quotas
from test_reserve import _node, payload, profile, with_i18n

WIDGET = Path(os.environ.get("PART_HOLDS_WIDGET_PATH", Path(__file__).with_name("widget.js"))).resolve()
FIVE_H = 18000
WEEK = 604800
IMAGE = {"applies_to_request_kinds": ["image"]}
TEXT = {"applies_to_request_kinds": ["text"]}
GEMINI = {"applies_to_model_prefixes": ["gemini-"]}


def _window(cid, used, window, reset, now, **fields):
    return dict({"id": cid, "label": cid, "used_ratio": used, "window_seconds": window,
                 "resets_at": qs.iso(now + reset)}, **fields)


def _snap(harness, sid, constraints, now, *, fresh=True, source="app", availability=None, observed=-60):
    return {"subject": {"harness": harness, "subject_id": sid}, "constraints": constraints,
            "availability": availability or {"state": "available"},
            "observed_at": qs.iso(now + observed), "freshness": "fresh" if fresh else "stale", "source": source}


def status(now):
    """Every case at the real clock (the widget reads Date.now())."""
    def shared():
        return _window("weekly", 0.2, WEEK, 86400, now)

    cool = qs.iso(now + 1800)
    codex = {
        # a healthy text window (80% left) and a cooling cap of image requests only
        "img": [_window("text_5h", 0.2, FIVE_H, 7200, now, **TEXT),
                _window("image_5h", 0.5, FIVE_H, 7200, now, cooldown_until=cool, **IMAGE)],
        # a cooling window whose model scope cannot be read (a string where the engine sends a list)
        "odd": [shared(), _window("odd_5h", 0.3, FIVE_H, 7200, now, applies_to_models="gpt-5", cooldown_until=cool)],
        # an image cap cooling with an end nobody can read: still held, still its own scope only
        "unread": [_window("text_5h", 0.2, FIVE_H, 7200, now, **TEXT),
                   _window("image_5h", 0.5, FIVE_H, 7200, now, cooldown_until="soon", **IMAGE)],
        # whole-account cooldowns, as before: no scope, a text cap, the snapshot's own state
        "bare": [shared(), {"id": "cooldown", "label": "cooldown", "used_ratio": None, "window_seconds": None,
                            "resets_at": None, "cooldown_until": qs.iso(now + 600)}],
        "textcap": [_window("text_5h", 0.2, FIVE_H, 7200, now, cooldown_until=qs.iso(now + 600), **TEXT)],
        "state": [shared()],
        # a model's cooldown by name
        "model": [shared(), _window("codex_5h", 0.3, FIVE_H, 7200, now, applies_to_models=["gpt-5-codex"],
                                    cooldown_until=cool)],
    }
    snaps = [_snap("codex", sid, rows, now) for sid, rows in codex.items() if sid != "state"]
    snaps.append(_snap("codex", "state", codex["state"], now,
                       availability={"state": "cooldown", "resets_at": qs.iso(now + 1200)}))
    # Antigravity: a gemini-* window scoped by prefix alone, held by a live cooldown that only a stale reading of
    # another source reports; and one the engine reports out (a model limit by prefix) while its own share has room.
    snaps += [
        _snap("agy", "gem", [shared(), _window("gemini_5h", 0.3, FIVE_H, 7200, now, **GEMINI)], now),
        _snap("agy", "gem", [dict({"id": "gemini_cool", "label": "Gemini cooldown", "used_ratio": None,
                                   "window_seconds": None, "resets_at": None, "cooldown_until": qs.iso(now + 2400)},
                                  **GEMINI)], now, fresh=False, source="other", observed=-900),
        _snap("agy", "gemx", [shared(), _window("gemini_5h", 0.3, FIVE_H, 7200, now, **GEMINI)], now,
              availability={"state": "available", "model_scoped_exhaustions": [
                  {"constraint_id": "gemini_5h", "applies_to_models": [], "applies_to_model_prefixes": ["gemini-"],
                   "resets_at": qs.iso(now + 7200)}]}),
    ]
    profiles = [profile("codex", sid) for sid in codex] + [profile("agy", sid) for sid in ("gem", "gemx")]
    return payload(snaps, profiles, harnesses=("codex", "agy"))


@pytest.fixture(scope="module")
def answer():
    now = float(int(time.time()))
    data = status(now)
    return {"now": now, "data": data, "view": plugin.build_view(data, "", now)}


def _account(view, harness, sid):
    group = next(g for g in view["groups"] if g["harness_id"] == harness)
    return next(a for a in group["accounts"] if a["subject_id"] == sid)


def _flags(summary, harness, meaning, sid):
    group = next(g for g in summary["groups"] if g["harness"] == harness and g["key"].split("|")[1] == meaning)
    bar = next(b for b in group["bars"] if b["account"] == f"{harness}:{sid}")
    return set(bar.get("flags") or ())


def test_hold_key_is_one_identity_of_the_whole_scope():
    of = lambda **fields: quota_scope.hold_key(quota_scope.scope_of(fields))
    assert of() == "-" and of(applies_to_models=[]) == "-" and of(**TEXT) != "-"
    assert of(applies_to_request_kinds=["text", "image"]) == "-"
    # a scope tied to models keeps the id such holds were always tied by (history keys unchanged)
    assert of(applies_to_models=["opus"]) == qs._scope_hash(("opus",)) != "-"
    assert of(**GEMINI) == qs._scope_hash((), ("gemini-",)) != "-"
    # other request kinds only, or an unreadable field: never the account, never another scope
    image, odd = of(**IMAGE), of(applies_to_models="gpt-5")
    assert image.startswith("-~") and odd.startswith("~") and image != odd
    assert of(applies_to_request_kinds=["video"]) not in ("-", image, odd)


def test_a_cooldown_of_part_of_the_requests_holds_its_own_window_never_the_account(answer):
    now, data = answer["now"], answer["data"]
    norm = qs.normalize(data, now)
    whole = {sid for hid, sid in norm.cooling_all if hid == "codex"}
    assert whole == {"bare", "state"}, whole
    computed = qs.compute(data, qs.HistoryView(state="empty"), now)
    summary = qs.build_summary(data, computed.view, now, state=computed, name_accounts=True)
    for sid in ("img", "unread"):
        assert "cooling" not in _flags(summary, "codex", "text_5h", sid), sid
        assert "cooling" in _flags(summary, "codex", "image_5h", sid), sid
    assert "cooling" not in _flags(summary, "codex", "weekly", "odd")
    assert "cooling" in _flags(summary, "codex", "odd_5h", "odd")
    for sid in ("bare", "state"):
        assert "cooling" in _flags(summary, "codex", "weekly", sid), sid
    assert "cooling" in _flags(summary, "codex", "text_5h", "textcap")
    assert "cooling" in _flags(summary, "codex", "codex_5h", "model")
    assert "cooling" not in _flags(summary, "codex", "weekly", "model")
    text = next(g for g in summary["groups"] if g["key"].split("|")[1] == "text_5h")
    assert "cooling" not in (text.get("restrictions") or {}) or text["restrictions"]["cooling"]["accounts"] == 1, \
        "only the text cap's own cooldown restricts the text window"


def test_the_account_view_names_what_a_cooldown_holds(answer):
    view = answer["view"]
    img = _account(view, "codex", "img")["quota"]
    assert img["state"] == "ok" and img["label"] == "20% used", img["label"]
    assert img["cooling_until"] == ""
    [cool] = img["cooldowns"]
    assert cool["scope"] == "requests" and cool["scope_fields"]["request_kinds"] == ["image"]
    windows = {c["id"]: c for c in img["constraints"]}
    assert cool["scope_key"] == windows["image_5h"]["scope_key"] != windows["text_5h"]["scope_key"] != "-"
    odd = _account(view, "codex", "odd")["quota"]
    assert odd["state"] == "ok"
    [cool] = odd["cooldowns"]
    assert cool["scope"] == "requests" and cool["scope_fields"]["models_state"] == "unreadable"
    assert cool["scope_key"] == {c["id"]: c for c in odd["constraints"]}["odd_5h"]["scope_key"]
    unread = _account(view, "codex", "unread")["quota"]
    assert unread["state"] == "ok" and unread["cooldowns"][0]["until_note"] == "unreadable"
    for sid in ("bare", "textcap", "state"):
        quota = _account(view, "codex", sid)["quota"]
        assert quota["state"] == "cooling" and quota["label"] == "Cooling down", sid
        assert [c["scope"] for c in quota["cooldowns"]] == (["requests"] if sid == "textcap" else ["account"]), sid
    model = _account(view, "codex", "model")["quota"]
    assert model["state"] == "ok" and [c["scope"] for c in model["cooldowns"]] == ["models"]
    # a family prefix travels with the hold and ties it by the whole scope
    gem = _account(view, "agy", "gem")["quota"]
    [cool] = gem["cooldowns"]
    assert cool["scope"] == "models" and cool["prefixes"] == ["gemini-"] and cool["models"] == []
    assert cool["freshness"] == "stale"
    assert cool["scope_key"] == {c["id"]: c for c in gem["constraints"]}["gemini_5h"]["scope_key"]
    gemx = _account(view, "agy", "gemx")["quota"]
    [limit] = gemx["model_exhaustions"]
    assert limit["live"] is True and limit["prefixes"] == ["gemini-"]
    assert limit["scope_key"] == {c["id"]: c for c in gemx["constraints"]}["gemini_5h"]["scope_key"]


def test_the_tool_and_snapshot_hold_only_the_window_of_the_scope(answer, tmp_path):
    now, data = answer["now"], answer["data"]
    out = plugin.answer_of(None, data, None, data, now, now)
    text_row = next(r for r in out["groups"] if r["key"].split("|")[1] == "text_5h")
    held = text_row.get("restrictions") or {}
    assert held.get("cooling", {}).get("accounts", 0) == 1, held  # textcap's own; the image caps' never
    import quota_context
    text = quota_context.snapshot_text(out)
    own = next(line for line in text.splitlines() if "text_5h" in line)
    assert "cooling 1" in own and "cooling 3" not in own, own


NODE_HEAD = r"""
const click = (env, key) => { const node = byFocus(env.root, key); assert.ok(node, 'missing control ' + key); node.listeners.click[0]({ stopPropagation() {} }); };
const state = (env, key) => { const row = byFocus(env.root, 'acct:' + key); assert.ok(row, 'no row ' + key); return classes(row, 'acc-state')[0]; };
const dot = (env, key) => String(classes(byFocus(env.root, 'acct:' + key), 'state-dot')[0].className);
const lines = (env) => classes(env.root, 'win-line').map((n) => [n.textContent, n.getAttribute('aria-label') || ''].join(' | '));
const holdsView = JSON.parse(require('fs').readFileSync(process.env.UI_FIXTURE_FILE, 'utf8'));
// The account view alone (no reserve read): every account's row stands in the family's list.
const open = async (id) => {
  const env = await boot(() => holdsView);
  await settle();
  click(env, 'harness:' + id);
  await settle();
  return env;
};
"""

NODE_PART = r"""
(async () => {
  const env = await open('codex');
  const words = (key) => state(env, key).textContent;
  // part of the requests: never the account
  for (const key of ['codex:img', 'codex:odd', 'codex:unread']) {
    assert.equal(words(key), 'a limit on some requests held', key);
    assert.doesNotMatch(dot(env, key), /\bbad\b/, key + ' ' + dot(env, key));
  }
  // the whole account, as before
  for (const key of ['codex:bare', 'codex:textcap', 'codex:state']) assert.equal(words(key), 'cooling down', key);
  assert.equal(words('codex:model'), 'model cooldown: Gpt-5-codex');
  // the picked account's card says what its cooldown holds
  click(env, 'acct:codex:img');
  await settle();
  const cooling = classes(env.root, 'quota-cooldown').map((n) => n.textContent);
  assert.ok(cooling.length && cooling.every((t) => /^Cooling down · some requests · until /.test(t)), cooling.join(' / '));
  assert.ok(classes(env.root, 'quota-cooldown').every((n) => /request kinds: image/.test(n.title)));
  env.disposeHooks[0]();
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exitCode = 1; });
"""

NODE_PREFIX = r"""
(async () => {
  // a family prefix: the window of exactly that scope is held, from a stale reading too, and the scope is named
  let env = await open('agy');
  assert.equal(state(env, 'agy:gem').textContent, 'model cooldown: gemini-*');
  assert.equal(state(env, 'agy:gemx').textContent, 'model limit reached: gemini-*');
  click(env, 'acct:agy:gem');
  await settle();
  const gem = lines(env).filter((t) => /gemini-\*/.test(t) && /% used/.test(t));
  assert.ok(gem.length, lines(env).join('\n'));
  assert.ok(gem.some((t) => /cooling down/.test(t) && /reported by a stale reading/.test(t)), gem.join('\n'));
  assert.match(classes(env.root, 'quota-cooldown').map((n) => n.textContent).join(' / '), /^Cooling down · gemini-\* · until /);
  env.disposeHooks[0]();
  env = await open('agy');
  click(env, 'acct:agy:gemx');
  await settle();
  assert.ok(lines(env).some((t) => /gemini-\*/.test(t) && /limit reported/.test(t)), lines(env).join('\n'));
  const reported = classes(env.root, 'quota-exhaustion').map((n) => n.textContent + ' | ' + n.title);
  assert.ok(reported.length && reported.every((t) => /^Model limit reached · gemini-\* /.test(t) && /gemini-\*/.test(t)), reported.join('\n'));
  env.disposeHooks[0]();
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exitCode = 1; });
"""


@pytest.fixture(scope="module")
def fixture_file(answer, tmp_path_factory):
    path = tmp_path_factory.mktemp("part-holds") / "view.json"
    path.write_text(json.dumps(answer["view"]), encoding="utf-8")
    return str(path)


def _run_node(fixture_file, script):
    node = _node()
    assert node is not None, "Node is required"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    result = subprocess.run([str(node), "-e", harness + NODE_HEAD + script], cwd=Path(__file__).parent,
                            env={**os.environ, "WIDGET_PATH": str(WIDGET), "UI_FIXTURE_FILE": fixture_file},
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_widget_holds_part_of_the_requests_never_the_account(fixture_file):
    _run_node(fixture_file, NODE_PART)


def test_the_widget_ties_a_family_prefix_hold_to_its_window(fixture_file):
    _run_node(fixture_file, NODE_PREFIX)


# ---------------------------------------------------------------------------
# The same in real engines

pytest.importorskip("playwright.sync_api")

STUB = with_i18n(r"""
(view) => {
  window.fetch = (url, options = {}) => {
    const post = ((options && options.method) || 'GET') !== 'GET';
    return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(post ? { ok: true, quota_updates: [] } : view) });
  };
}
""")


def _page(browser, view):
    page = browser.new_page(viewport={"width": 1120, "height": 1300})
    page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
    page.evaluate(STUB, view)
    page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
    page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
    return page


def _state(page, key):
    return page.locator(f'[data-focus="acct:{key}"] .acc-state').inner_text().strip()


def _shot(page, browser, name):
    evidence = os.environ.get("QUOTAS_EVIDENCE_DIR")
    if evidence:
        page.screenshot(path=str(Path(evidence) / f"{browser.browser_type.name}-{name}.png"), full_page=True)


def test_real_engines_hold_part_of_the_requests_never_the_account(browser, answer):
    page = _page(browser, answer["view"])
    try:
        page.click('[data-focus="harness:codex"]')
        dot = page.locator('[data-focus="acct:codex:img"] .state-dot').first.get_attribute("class")
        assert _state(page, "codex:img") == "a limit on some requests held" and "bad" not in dot.split(), dot
        assert _state(page, "codex:odd") == "a limit on some requests held"
        assert _state(page, "codex:state") == "cooling down" and _state(page, "codex:bare") == "cooling down"
        page.click('[data-focus="acct:codex:img"]')
        cooling = page.locator(".quota-cooldown").first.inner_text()
        assert cooling.startswith("Cooling down · some requests"), cooling
        _shot(page, browser, "part-holds-requests")
    finally:
        page.close()


def test_real_engines_tie_a_family_prefix_hold_to_its_window(browser, answer):
    page = _page(browser, answer["view"])
    try:
        page.click('[data-focus="harness:agy"]')
        assert _state(page, "agy:gem") == "model cooldown: gemini-*"
        assert _state(page, "agy:gemx") == "model limit reached: gemini-*"
        page.click('[data-focus="acct:agy:gem"]')
        line = page.locator(".win-line", has_text="% used").filter(has_text="gemini-*").first.inner_text()
        assert "cooling down" in line and "reported by a stale reading" in line, line
        _shot(page, browser, "part-holds-prefix")
    finally:
        page.close()


@pytest.mark.parametrize("scope", [{"applies_to_models": ["gpt-5"]}, GEMINI])
@pytest.mark.parametrize("kind", ["cooldown", "exhaustion"])
def test_named_and_prefix_holds_do_not_cross_request_kinds(scope, kind):
    now = float(int(time.time()))
    image_scope = dict(scope, **IMAGE)
    windows = [_window("text", .2, FIVE_H, 7200, now, **dict(scope, **TEXT)),
               _window("image", .2, FIVE_H, 7200, now, **image_scope)]
    row = _snap("codex", "a", windows, now)
    if kind == "cooldown":
        windows[1]["cooldown_until"] = qs.iso(now + 600)
    else:
        row["availability"]["model_scoped_exhaustions"] = [dict(image_scope, resets_at=qs.iso(now + 600))]
    data = payload([row], [profile("codex", "a")], harnesses=("codex",))
    summary = qs.build_summary(data, qs.HistoryView(state="empty"), now, name_accounts=True)
    flag = "cooling" if kind == "cooldown" else "model_exhausted"
    assert not _flags(summary, "codex", "text", "a"), summary
    assert flag in _flags(summary, "codex", "image", "a"), summary
    quota = _account(plugin.build_view(data, "", now), "codex", "a")["quota"]
    views = {c["id"]: c for c in quota["constraints"]}
    hold = quota["cooldowns" if kind == "cooldown" else "model_exhaustions"][0]
    assert hold["scope_key"] == views["image"]["scope_key"] != views["text"]["scope_key"]
    assert hold["scope_fields"]["request_kinds"] == ["image"]


def test_unreadable_kind_of_a_named_hold_never_matches_its_readable_scope():
    base = {"applies_to_model_prefixes": ["gemini-"]}
    key = lambda fields: quota_scope.hold_key(quota_scope.scope_of(fields))
    assert key(dict(base, applies_to_request_kinds=["video"])) != key(base)
    assert key(dict(base, applies_to_request_kinds=["text", "video"])) != key(dict(base, **TEXT))


def test_widget_pools_and_holds_keep_prefix_request_scopes_separate(tmp_path):
    now = float(int(time.time()))
    rows = [_window("text", .2, FIVE_H, 7200, now, **dict(GEMINI, **TEXT)),
            _window("image", .2, FIVE_H, 7200, now, **dict(GEMINI, **IMAGE), cooldown_until=qs.iso(now + 600))]
    data = payload([_snap("codex", "a", rows, now)], [profile("codex", "a")], harnesses=("codex",))
    view = plugin.build_view(data, "", now)
    path = tmp_path / "view.json"
    path.write_text(json.dumps(view))
    _run_node(str(path), r"""
(async () => {
  const env = await open('codex');
  click(env, 'acct:codex:a');
  await settle();
  const own = lines(env).filter(t => /gemini-\*/.test(t) && /20% used/.test(t));
  assert.equal(own.length, 2, lines(env).join('\n'));
  assert.equal(own.filter(t => /cooling down/.test(t)).length, 1, own.join('\n'));
  assert.equal(own.filter(t => !/cooling down/.test(t)).length, 1, own.join('\n'));
  env.disposeHooks[0]();
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
""")
