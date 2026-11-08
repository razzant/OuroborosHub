"""The rail of rings, the ladder of resets and the plate of the picked limit (capsule: window tabs), as the owner's composition draws them.

The real widget.js runs under the same fake DOM as the other widget tests; the views pass through the real backend (build_view + reserve_view): a
Claude family with a 5-hour, a weekly and model-scoped weekly limits, a Codex family (a week and 30 days), a Cursor family with no window at all and
Cursor families whose resources carry a budget per billing cycle, an Antigravity family whose limits are its engine's model groups ("Gemini-5h",
"Claude and GPT-weekly"), and families of accounts in every state. Real browsers (Chromium and WebKit) hold the rectangles: 1, 4, 20 and 40 accounts
at the card's width in the owner's window (598px) and on a phone (390px), in both themes.

What is held here:
- one ring per limit a family reports, in the order 5-hour / Weekly / 30-day (a window's family-wide limit first, then its scopes): its short name
  ("5h", "7d", "Fable 7d", "Gemini 5h"), its average share left (the skill's figure, "—" with none, never 0) as one 12px unit with its %, a star where
  accounts are not read; a place the family has no window for is not drawn; a window the reader hid in Rows keeps a dotted ring out of the tab order;
  the pressed ring is the limit on screen, and a ring opens its limit (one read, with reuse); a family with no window at all has no rail; Cursor's
  budget per billing cycle is one ring (Cycle) and a plate of money;
- the ladder of resets of the limit on screen, soonest first, up to four steps and the word that counts the rest;
- the plate: the window and its scope said once, the account-windows left, who is at the limit and when the first comes back, who is not read, who
  is held and by what, the family's manual resets, the whole of it spoken to a reader; Antigravity's prefix scope says every model it covers;
- every account's bar with no figure in it, its state in its class and its words, and the legend of only the states that stand in the row;
- the capsule styles only its own classes, every size and colour a token.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

import plugin
import quota_history
import quota_summary as qs
import test_quotas
from test_reserve import _node, payload, profile, WEEK, FIVE_H
from test_ui_timeline import _views

MONTH = 2592000
GEMINI = ["gemini-3.1-pro-low", "gemini-3.8-flash-high", "gemini-3.8-flash-low", "gemini-3.8-flash-medium"]
CLAUDE_GPT = ["claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium"]


def _row(now, cid, label, used, window, models=None):
    out = {"id": cid, "label": label, "used_ratio": used, "window_seconds": window, "resets_at": qs.iso(now + 3600)}
    if models is not None:
        out["applies_to_models"] = models
    return out


def _family(tmp_path, harness, constraints, accounts=("s1", "s2"), name=None):
    """One family's view as the widget's first read gets it, built by the real backend."""
    now = float(int(time.time()))
    snaps = [{"subject": {"harness": harness, "subject_id": sid, "plan_label": None},
              "constraints": constraints(now, i), "availability": {"state": "available"},
              "observed_at": qs.iso(now - 20), "freshness": "fresh", "source": "app"}
             for i, sid in enumerate(accounts)] if constraints else []
    status = payload(snaps, [profile(harness, sid) for sid in accounts], harnesses=(harness,))
    view = plugin.build_view(status, "")
    store = quota_history.HistoryStore(tmp_path / (name or harness))
    view["reserve"] = plugin.reserve_view(store, status, now, now, harness=harness, name_accounts=True)
    return view


def _partial(view, window, last=(), unknown=None, ago=3 * 3600):
    """The window whose key holds `window`, with some of its accounts read in part, as the engine's answer would tell it: the ids in
    `last` were read `ago` seconds back (last known), the ids in `unknown` (id -> why) have no usable reading. The window's own sums
    follow (measured, last known, unknown, coverage); its slots stay: the window still applies to every account."""
    now = time.time()
    unknown = unknown or {}
    for g in view["reserve"]["summary"]["groups"]:
        if window not in g["key"]:
            continue
        for b in g["bars"]:
            sid = (b.get("account") or "").split(":", 1)[-1]
            if sid in last:
                b.update(state="last_known", observed_at=qs.iso(now - ago), origin="history", age_seconds=ago)
            elif sid in unknown:
                for k in ("age_seconds", "origin", "pace"):
                    b.pop(k, None)
                b.update(state="unknown", why=unknown[sid], left=None, observed_at=None, resets_at=None, at_limit=False)
        bars = g["bars"]
        cur = [b for b in bars if b["state"] == "current"]
        old = [b for b in bars if b["state"] == "last_known"]
        gone = [b for b in bars if b["state"] == "unknown"]
        windows = round(sum(b.get("left") or 0 for b in cur), 2)
        g["measured"] = dict(g.get("measured", {}), accounts=len(cur), windows=windows,
                             average_remaining_pct=round(windows / len(cur) * 100, 1) if cur else None)
        at = sorted(b["observed_at"] for b in old if b.get("observed_at"))
        g["last_known"] = {"accounts": len(old), "windows": round(sum(b.get("left") or 0 for b in old), 2),
                           "oldest_observed_at": at[0] if at else None, "newest_observed_at": at[-1] if at else None}
        g["unknown"] = {"accounts": len(gone), "reasons": {}}
        g["coverage"] = dict(g.get("coverage", {}), measured=len(cur), stale_only=len(old), invalid=len(gone))
    return view


def _states_family(tmp_path, name, harness, window, rows):
    """One family with one limit and accounts in chosen states, built by the real backend. A row is a dict: sid, used
    (the share used), reset (seconds ahead), fresh (False: a last-known reading), ago (seconds since it was read),
    enabled (False: the owner switched the account off) and verification / availability (the engine's check and
    sign-in: a failed check, a credential unavailable) - an account the owner holds back, its reading held back."""
    now = float(int(time.time()))
    snaps = [{"subject": {"harness": harness, "subject_id": r["sid"], "plan_label": None},
              "constraints": [_row(now, "primary", "primary", r["used"], window)
                              | {"resets_at": qs.iso(now + r.get("reset", 3600))}],
              "availability": {"state": "available"}, "observed_at": qs.iso(now - r.get("ago", 20)),
              "freshness": "fresh" if r.get("fresh", True) else "stale", "source": "app"} for r in rows]
    profiles = []
    for r in rows:
        one = profile(harness, r["sid"], enabled=r.get("enabled", True), verification=r.get("verification", "passed"))
        if r.get("availability"):
            one["status"]["availability"] = r["availability"]
        profiles.append(one)
    status = payload(snaps, profiles, harnesses=(harness,))
    view = plugin.build_view(status, "")
    store = quota_history.HistoryStore(tmp_path / name)
    view["reserve"] = plugin.reserve_view(store, status, now, now, harness=harness, name_accounts=True)
    return view


def _long_names(view):
    """The accounts c05 and c12 named by their addresses, as an account with a label of its own is."""
    names = {"c05": "olga.petrova.team@example-company.com", "c12": "ivan.sidorov.backup@example-company.com"}
    for group in view["groups"]:
        for account in group["accounts"]:
            sid = account["key"].split(":", 1)[-1]
            if sid in names:
                account["label"] = account["email"] = names[sid]
    return view


def _two_windows(tmp_path):
    """Claude with a 5-hour and a weekly window, in both of them accounts at the limit, held, last known."""
    now = float(int(time.time()))
    rows = [("a1", 1.0, 0.3, True, True), ("a2", 1.0, 1.0, True, True), ("a3", 0.4, 0.5, True, False),
            ("a4", 0.2, 1.0, False, True), ("a5", 0.6, 0.4, True, True)]
    snaps = [{"subject": {"harness": "claude", "subject_id": sid, "plan_label": None},
              "constraints": [_row(now, "five_hour", "5 hour", five, FIVE_H), _row(now, "weekly", "weekly", week, WEEK)],
              "availability": {"state": "available"}, "observed_at": qs.iso(now - (20 if fresh else 1620)),
              "freshness": "fresh" if fresh else "stale", "source": "app"} for sid, five, week, fresh, _ in rows]
    status = payload(snaps, [profile("claude", sid, enabled=on) for sid, _, _, _, on in rows], harnesses=("claude",))
    view = plugin.build_view(status, "")
    view["reserve"] = plugin.reserve_view(quota_history.HistoryStore(tmp_path / "two_windows"), status, now, now,
                                          harness="claude", name_accounts=True)
    return view


def _cursor_resources(pid, now, *, included=None, unlimited=False, on_demand=None, team=None,
                      freshness="fresh", ends_in=11 * 86400):
    """One account's resources as the engine's parseCursorResources gives them (B:189226-189324): money in
    minor units of USD, balances and spending in pairs under one id, the cycle's period on every row.
    included / on_demand / team are (used, limit, remaining) in cents, None where the dashboard said nothing."""
    reset = now + ends_in
    period = {"starts_at": qs.iso(reset - MONTH), "ends_at": qs.iso(reset)}
    common = {"unit": "minor", "currency": "USD", "decimal_places": 2, "period": period, "fee_included": None}
    balances, spending = [], []

    def pair(scope, kind, values, flag):
        used, limit, remaining = (None if v is None else str(v) for v in values)
        rid = f"cursor_{scope}_{kind}"
        who = "Team pool" if scope == "team" else "Personal"
        spending.append({**common, "id": rid, "label": who + (" included usage" if kind == "included" else " on-demand spending"),
                         "scope": scope, "usage_kind": kind, "used": used, "limit": limit, "enabled": True,
                         "resets_at": qs.iso(reset), "reason": "Internal dashboard observation; not execution availability"})
        balances.append({**common, "id": rid, "label": who + (" included" if kind == "included" else " on-demand") + " remaining",
                         "scope": scope, "usage_kind": kind, "amount": remaining,
                         "has_balance": None if remaining is None else int(remaining) > 0, "unlimited": flag})

    if included is not None:
        pair("personal", "included", included, unlimited)
    if on_demand is not None:
        pair("personal", "on_demand", on_demand, None)
    if team is not None:
        pair("team", "on_demand", team, None)

    def facet(value):
        return {"value": value, "source": "cursor_dashboard", "observed_at": qs.iso(now - 40), "freshness": freshness,
                "last_attempt_at": qs.iso(now - 40), "last_error": None}
    empty = {"value": None, "source": None, "observed_at": None, "freshness": "unknown",
             "last_attempt_at": None, "last_error": None}
    return {"target": {"harness": "cursor", "profile_id": pid}, "balances": facet(balances), "spending": facet(spending),
            "resets": dict(empty), "diagnostics": facet([
                {"code": "cursor_dashboard_internal_api",
                 "detail": "Internal dashboard schema may change; numeric resources do not establish execution availability"}])}


def _cursor_family(tmp_path, name, rows):
    """A Cursor family as the widget's first read gets it: no snapshot at all, the engine's standing no_source for
    every account, and the dashboard's resources (None: the answer carries none for that account). Built by the
    real backend. `rows` are (profile id, resources or None)."""
    now = float(int(time.time()))
    status = payload([], [profile("cursor", pid) for pid, _ in rows], harnesses=("cursor",))
    status["quota_absences"] = [{"subject": {"harness": "cursor", "credential_route": "vendor_native", "plan_label": None,
                                             "subject_id": pid},
                                 "reason": "no_source", "detail": None, "observed_at": qs.iso(now)} for pid, _ in rows]
    status["resources"] = [res(pid, now) for pid, res in rows if res]
    view = plugin.build_view(status, "", now)
    store = quota_history.HistoryStore(tmp_path / name)
    view["reserve"] = plugin.reserve_view(store, status, now, now, harness="cursor", name_accounts=True)
    return view


def _cursor_views(tmp):
    """Cursor with the fixture's budget (two accounts, $20.00 each: $11.80 and $4.60 left, on-demand $3.20 and
    $18.45 spent), Cursor with every kind of reading beside a plain one, and Cursor whose resources hold no
    included pool."""
    def res(**kw):
        return lambda pid, now: _cursor_resources(pid, now, **kw)

    def late(pid, now, **kw):
        return _cursor_resources(pid, now, ends_in=17 * 86400, **kw)
    budget = _cursor_family(tmp, "cursor_budget", [
        ("cursor1", res(included=(820, 2000, 1180), on_demand=(320, 5000, 4680))),
        ("cursor2", lambda pid, now: late(pid, now, included=(1540, 2000, 460), on_demand=(1845, 5000, 3155)))])
    mixed = _cursor_family(tmp, "cursor_mixed", [
        # 30% left; $3.00 on-demand of its own, and a team pool whose spend is the team's, not counted
        ("a1", res(included=(1400, 2000, 600), on_demand=(300, 5000, 4700), team=(99999, 500000, 400001))),
        # no cap: no share; its on-demand spend still counts
        ("a2", res(included=(None, None, None), unlimited=True, on_demand=(200, 5000, 4800))),
        # the dashboard is stale: last known, dated, not counted (and not healthy: the standing no_source is a fault now)
        ("a3", res(included=(1000, 2000, 1000), on_demand=(700, 5000, 4300), freshness="stale")),
        # nothing read for this one
        ("a4", None),
        # used up: 0% left, a real zero - not at the limit, not red
        ("a5", res(included=(2000, 2000, 0)))])
    # an on-demand pool only: no included usage, so no budget to show
    bare = _cursor_family(tmp, "cursor_bare", [
        ("b1", res(on_demand=(100, 5000, 4900))), ("b2", res(on_demand=(0, 5000, 5000)))])
    return {"cursor_budget": budget, "cursor_mixed": mixed, "cursor_bare": bare}


@pytest.fixture(scope="module")
def fixture_file(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("windowtabs")
    views = {
        # Claude: a 5-hour, the weekly, two model-scoped weekly limits; a history, so Combined exists.
        "claude": _views(tmp, kinds=False)["default"],
        "codex": _family(tmp, "codex", lambda now, i: [
            _row(now, "codex:primary", "codex primary", .4 + .1 * i, WEEK),
            _row(now, "codex:secondary", "codex secondary", .2 + .1 * i, MONTH)]),
        "cursor": _family(tmp, "cursor", None),
        **_cursor_views(tmp),
        # Codex Weekly with accounts in three states: three at the limit (one reset), one switched off, one at the
        # limit when last read (27 minutes ago); two healthy.
        "codex_states": _states_family(tmp, "codex_states", "codex", WEEK, [
            {"sid": "gptpro1", "used": 1.0, "reset": 7200}, {"sid": "gptpro2", "used": 1.0, "reset": 7200},
            {"sid": "gptpro3", "used": 1.0, "reset": 7200}, {"sid": "gptpro4", "used": 0.4, "reset": 90000},
            {"sid": "gptpro5", "used": 0.2, "reset": 90000},
            {"sid": "gptpro9", "used": 0.5, "enabled": False, "reset": 90000},
            {"sid": "gptpro7", "used": 1.0, "fresh": False, "ago": 27 * 60, "reset": 90000}]),
        # Claude 5-hour with three at the limit, each to reset at its own time, and one healthy.
        "claude_five": _states_family(tmp, "claude_five", "claude", FIVE_H, [
            {"sid": "claude1", "used": 1.0, "reset": 3600}, {"sid": "claude2", "used": 1.0, "reset": 7200},
            {"sid": "claude3", "used": 1.0, "reset": 10800}, {"sid": "claude4", "used": 0.3, "reset": 14400}]),
        # Held back for two reasons: two switched off, one whose check failed.
        "reasons_two": _states_family(tmp, "reasons_two", "codex", WEEK, [
            {"sid": "off1", "used": 0.3, "enabled": False}, {"sid": "off2", "used": 0.6, "enabled": False},
            {"sid": "bad1", "used": 0.5, "verification": "failed"}, {"sid": "ok1", "used": 0.2}]),
        # Held back for three: switched off, check failed, signed out (which also failed its check).
        "reasons_three": _states_family(tmp, "reasons_three", "codex", WEEK, [
            {"sid": "off1", "used": 0.3, "enabled": False}, {"sid": "bad1", "used": 0.5, "verification": "failed"},
            {"sid": "out1", "used": 0.4, "verification": "failed", "availability": "unavailable"},
            {"sid": "ok1", "used": 0.2}]),
        "two_windows": _two_windows(tmp),
        # The reading of a window, counted: sixteen accounts, all read; the same sixteen with one read 3 h ago and one unreadable (14 of
        # 16); twenty with five read 3 h ago and two unreadable for two reasons (the note names three and counts the rest); two windows
        # of which the 5-hour is read in part and the weekly whole; and the same part with the addresses of two accounts for names.
        "reading_all": _states_family(tmp, "reading_all", "claude", FIVE_H,
                                      [{"sid": "c%02d" % i, "used": .1 + i / 40} for i in range(1, 17)]),
        "reading_part": _partial(_states_family(tmp, "reading_part", "claude", FIVE_H,
                                                [{"sid": "c%02d" % i, "used": .1 + i / 40} for i in range(1, 17)]),
                                 str(FIVE_H), last=("c05",), unknown={"c12": "unreadable"}),
        "reading_many": _partial(_states_family(tmp, "reading_many", "claude", FIVE_H,
                                                [{"sid": "c%02d" % i, "used": .1 + i / 50} for i in range(1, 21)]),
                                 str(FIVE_H), last=("c01", "c02", "c03", "c04", "c05"),
                                 unknown={"c10": "unreadable", "c11": "sources_disagree"}),
        "reading_two": _partial(_family(tmp, "claude", lambda now, i: [_row(now, "five_hour", "5 hour", .2 + .1 * i, FIVE_H),
                                                                       _row(now, "weekly", "weekly", .1 + .1 * i, WEEK)],
                                        accounts=("a1", "a2", "a3", "a4"), name="reading_two"), str(FIVE_H), last=("a3",)),
        # Both windows read in part (a different account each): the star stands on both tabs, the note is the picked window's own.
        "reading_both": _partial(_partial(_family(tmp, "claude", lambda now, i: [_row(now, "five_hour", "5 hour", .2 + .1 * i, FIVE_H),
                                                                                 _row(now, "weekly", "weekly", .1 + .1 * i, WEEK)],
                                                  accounts=("a1", "a2", "a3", "a4"), name="reading_both"), str(FIVE_H), last=("a3",)),
                                 str(WEEK), last=("a2",)),
        # The 5-hour read none of its four accounts (the window reset after the readings: a dash, no star, a note all the same), the weekly
        # read whole: the note the 5-hour draws is the place the weekly keeps.
        "reading_zero": _partial(_family(tmp, "claude", lambda now, i: [_row(now, "five_hour", "5 hour", .2 + .1 * i, FIVE_H),
                                                                        _row(now, "weekly", "weekly", .1 + .1 * i, WEEK)],
                                         accounts=("a1", "a2", "a3", "a4"), name="reading_zero"), str(FIVE_H),
                                 unknown={sid: "reset_passed" for sid in ("a1", "a2", "a3", "a4")}),
        # Four accounts read 45 minutes ago (current readings, old: past the half hour); and four with no current reading at all.
        "reading_old": _states_family(tmp, "reading_old", "claude", FIVE_H,
                                      [{"sid": "c%02d" % i, "used": .1 + i / 40, "ago": 45 * 60} for i in range(1, 5)]),
        "reading_none": _states_family(tmp, "reading_none", "claude", FIVE_H,
                                       [{"sid": "c%02d" % i, "used": .1 + i / 40, "ago": 45 * 60, "fresh": False} for i in range(1, 5)]),
        # Codex's 30-day window, 21 accounts, three of them read three days ago: the note names three accounts and wraps on a narrow card
        # (the commas between the names stay at the names).
        "reading_codex_month": _partial(_states_family(tmp, "reading_codex_month", "codex", MONTH,
                                                       [{"sid": "gptpro%d" % i, "used": .1 + i / 60} for i in range(1, 22)]),
                                        str(MONTH), last=("gptpro14", "gptpro7", "gptpro21"), ago=3 * 86400),
        "reading_long": _long_names(_partial(_states_family(
            tmp, "reading_long", "claude", FIVE_H, [{"sid": "c%02d" % i, "used": .1 + i / 40} for i in range(1, 17)]),
            str(FIVE_H), last=("c05",), unknown={"c12": "unreadable"})),
        # Ten at the limit: the hint names eight and counts the rest.
        "claude_many": _states_family(tmp, "claude_many", "claude", FIVE_H,
                                      [{"sid": f"c{i:02d}", "used": 1.0, "reset": 3600 + 60 * i} for i in range(10)]),
        # One account at the limit among healthy ones.
        "claude_single": _states_family(tmp, "claude_single", "claude", FIVE_H, [
            {"sid": "claude1", "used": 0.2, "reset": 7200}, {"sid": "claude2", "used": 0.5, "reset": 7200},
            {"sid": "claude3", "used": 1.0, "reset": 5400}]),
        # Two accounts whose resets are 30 s apart (the engine counts them as one group, within two minutes of the soonest), one much later.
        "reset_close": _states_family(tmp, "reset_close", "claude", FIVE_H, [
            {"sid": "close1", "used": 0.5, "reset": 3600}, {"sid": "close2", "used": 0.6, "reset": 3630},
            {"sid": "close3", "used": 0.2, "reset": 7200}]),
        "agy": _family(tmp, "agy", lambda now, i: [
            _row(now, "Gemini-5h", "5h", .5 + .1 * i, FIVE_H, GEMINI),
            _row(now, "Gemini-weekly", "weekly", .3, WEEK, GEMINI),
            _row(now, "Claude and GPT-5h", "5h", .2 + .1 * i, FIVE_H, CLAUDE_GPT),
            _row(now, "Claude and GPT-weekly", "weekly", .4, WEEK, CLAUDE_GPT)], accounts=("agy1",)),
    }
    path = tmp / "views.json"
    path.write_text(json.dumps(views))
    return str(path)




NODE_PLATE = r"""
const text = (n) => n.textContent;
const has = (n, c) => String(n.className || '').split(/\s+/).includes(c);
const click = (node) => node.listeners.click[0]({ stopPropagation() {} });
const done = (env) => env.disposeHooks[0]();
const rings = (env) => classes(env.root, 'q-ring');
const live = (env) => rings(env).filter((r) => !has(r, 'q-ring-hidden'));
// (the window is joined to the scope's last word by a no-break space, so the name wraps before the scope, never before "7d")
const nameOf = (ring) => text(classes(ring, 'q-ring-name')[0]).replace(/\*$/, '').replace(/\u00a0/g, ' ');
const pcOf = (ring) => text(classes(ring, 'q-pc')[0]);
const pressed = (env) => live(env).filter((r) => r.getAttribute('aria-pressed') === 'true');
const plate = (env) => classes(env.root, 'q-plate')[0];
const said = (env) => { const s = walk(env.root).find((n) => n.getAttribute('id') === 'quotas-win-say'); return s ? text(s) : ''; };
const bars = (env) => classes(classes(env.root, 'q-bars')[0], 'q-bar');
const settle2 = async () => { for (let i = 0; i < 6; i++) await settle(); };
const groupsOf = (view) => view.reserve.summary.groups;

(async () => {
  const views = JSON.parse(fs.readFileSync(process.env.PLATE_FIXTURE_FILE, 'utf8'));
  const mode = process.env.PLATE_CASE;

  if (mode === 'rings') {
    // One ring per limit, in the order of the places, the family-wide limit of a window first; the short name is the window and the scope.
    const want = { claude: ['5h', '7d', 'mmm', 'model-one 7d'], codex: ['7d', '30d'], agy: ['Claude and GPT 5h', 'Gemini 5h', 'Claude and GPT 7d', 'Gemini 7d'] };
    for (const key of Object.keys(want)) {
      const env = await boot(views[key]);
      const names = live(env).map(nameOf);
      assert.equal(names.length, want[key].length, key + ': one ring per limit: ' + names);
      want[key].forEach((w, i) => assert.ok(names[i].toLowerCase().startsWith(w.toLowerCase()), key + ': ' + names[i] + ' is ' + w));
      assert.equal(classes(env.root, 'q-rings')[0].getAttribute('role'), 'group');
      assert.ok(classes(env.root, 'q-rings')[0].listeners.keydown.length, key + ': the arrow keys move along the rings');
      assert.equal(pressed(env).length, 1, key + ': one ring is the limit on screen');
      // the ring's figure is the skill's average share left, the digits and the % one unit; the plate is the pressed ring's
      groupsOf(views[key]).forEach((g) => {
        const ring = live(env).find((r) => r.getAttribute('data-limit') === g.key);
        assert.ok(ring, key + ': a ring for ' + g.key);
        const avg = g.measured && g.measured.accounts ? Math.round(g.measured.average_remaining_pct) + '%' : '—';
        assert.equal(pcOf(ring), avg, key + ': ' + g.key);
        assert.match(ring.getAttribute('aria-label'), /avg left|no current reading/);
      });
      assert.equal(classes(env.root, 'q-plate').length, 1, key + ': one plate');
      done(env);
    }
    for (const pct of [99.6, 0.4, 50]) {
      const v = JSON.parse(JSON.stringify(views.claude));
      groupsOf(v)[0].measured.average_remaining_pct = pct;
      const edge = await boot(v);
      const r = live(edge).find(r => r.getAttribute('data-limit') === groupsOf(v)[0].key);
      assert.equal(walk(r).filter(n => n.getAttribute('class') === 'ring-arc').length, 1, 'known ring retains arc at ' + pct);
      done(edge);
    }
    // No window at all: no rail and the words say so.
    const cursor = await boot(views.cursor);
    assert.equal(rings(cursor).length, 0);
    assert.equal(classes(cursor.root, 'q-rail').length, 0);
    assert.match(cursor.root.textContent, /No quota limit reported for Cursor\./);
    done(cursor);
  } else if (mode === 'pick') {
    // A ring opens its limit: its plate, its scope, one read of its chart with reuse; the ring on screen pressed again asks nothing.
    const view = views.claude;
    let calls = 0;
    const env = await boot((url) => { calls++; return view; });
    const scoped = groupsOf(view).find((g) => g.key.includes('weekly_one'));
    const before = calls;
    click(byFocus(env.root, 'ring:claude:' + scoped.key));
    await settle2();
    assert.equal(pressed(env).map((r) => r.getAttribute('data-limit')).join(), scoped.key);
    assert.match(text(classes(plate(env), 'q-head')[0]), /^7-day window/);
    assert.match(text(classes(plate(env), 'q-scope')[0]), /^model-one only$/i, 'the scope said once, in the head: ' + text(classes(plate(env), 'q-scope')[0]));
    assert.ok(has(classes(plate(env), 'q-scope')[0], 'own'));
    assert.ok(calls > before, 'the chart of the limit is asked for');
    const asked = calls;
    click(byFocus(env.root, 'ring:claude:' + scoped.key));
    await settle2();
    assert.equal(calls, asked, 'the ring on screen asks nothing');
    // the family-wide limit says All models
    click(byFocus(env.root, 'ring:claude:' + groupsOf(view)[0].key));
    await settle2();
    assert.equal(text(classes(plate(env), 'q-scope')[0]), 'All models');
    done(env);
  } else if (mode === 'agy') {
    // Antigravity's limits are its engine's model groups: the ring and the scope name the group, never a list of models; the models it
    // names stand in the hint, and a prefix scope says it covers every model whose id starts so (Argon in Gemini's, no limit of its own).
    const env = await boot(views.agy);
    assert.ok(live(env).every((r) => !/gemini-3|claude-sonnet|gpt-oss/.test(nameOf(r))), 'no model list in a ring');
    const gemini = groupsOf(views.agy).find((g) => g.key.includes('Gemini-5h'));
    click(byFocus(env.root, 'ring:agy:' + gemini.key));
    await settle2();
    assert.equal(text(classes(plate(env), 'q-scope')[0]), 'Gemini only');
    assert.match(classes(plate(env), 'q-scope')[0].title, /models: gemini-3\.1-pro-low/);
    done(env);
    const prefixed = JSON.parse(JSON.stringify(views.agy));
    groupsOf(prefixed).forEach((g) => { if (g.key.includes('Gemini')) g.scope = { prefixes: { names: ['gemini-'], omitted: 0 }, prefixes_state: 'listed' }; });
    const env2 = await boot(prefixed);
    click(byFocus(env2.root, 'ring:agy:' + gemini.key));
    await settle2();
    assert.equal(text(classes(plate(env2), 'q-cov')[0]), 'Covers every gemini-* model');
    assert.equal(live(env2).filter((r) => /argon/i.test(nameOf(r))).length, 0, 'no ring of its own for a model the prefix covers');
    done(env2);
  } else if (mode === 'hidden') {
    // A limit the reader hid in Rows has no ring; a window all of whose limits are hidden keeps a dotted ring out of the tab order.
    const view = JSON.parse(JSON.stringify(views.codex));
    view.display = { hidden: [groupsOf(view)[1].key] };
    const env = await boot(view);
    assert.deepEqual(live(env).map(nameOf), ['7d']);
    const dotted = classes(env.root, 'q-ring-hidden');
    assert.equal(dotted.length, 1);
    assert.equal(dotted[0].getAttribute('aria-disabled'), 'true');
    assert.equal(dotted[0].getAttribute('tabindex'), '-1');
    assert.match(dotted[0].getAttribute('aria-label'), /^30-day — hidden in Rows$/);
    done(env);
  } else if (mode === 'reading') {
    // The stamp counts the accounts of the limit on screen; the star stands on a ring whose accounts are not all read; the note names who.
    const env = await boot(views.reading_part);
    assert.match(text(classes(env.root, 'q-stamp')[0]), /^Read 14 of 16 · /);
    assert.equal(classes(live(env)[0], 'q-star').length, 1, 'the star');
    const foot = classes(env.root, 'wt-foot')[0];
    assert.ok(foot && has(foot, 'q-note'), 'the note stands in the plate');
    assert.match(text(foot), /No fresh data · 2/);
    assert.match(said(env), /14 current of 16 accounts/);
    done(env);
    const all = await boot(views.reading_all);
    assert.equal(classes(all.root, 'q-star').length, 0, 'read whole: no star');
    assert.equal(classes(all.root, 'wt-foot').length, 0, 'and no note');
    done(all);
  } else if (mode === 'states') {
    // Codex Weekly: three at the limit (one reset), one switched off, one at the limit when last read, two healthy.
    const env = await boot(views.codex_states);
    const b = bars(env);
    assert.equal(b.length, 7);
    assert.equal(b.filter((x) => has(x, 'spent')).length, 3, 'at the limit: a red base');
    assert.equal(b.filter((x) => has(x, 'was')).length, 1, 'at the limit when last read: its own base, dated');
    assert.equal(b.filter((x) => has(x, 'off')).length, 1, 'switched off');
    // no figure in a bar: the card says the exact share; a bar's words are its label
    assert.ok(b.every((x) => !/\d/.test(text(x))), 'no digits in a bar');
    assert.ok(b.every((x) => /% left|unknown/.test(x.getAttribute('aria-label'))));
    // who is at the limit, and in how long the first comes back; the legend names the dated one; the keys say only the states that stand
    const limit = classes(plate(env), 'wt-limit')[0];
    // (three at the limit are counted, not named: the hint names them, and the bubble lights their bars)
    assert.match(text(limit), /^At the limit · 3 · back in \d+h/);
    assert.match(limit.title, /gptpro1 — at the limit/);
    assert.ok(has(limit, 'q-note'));
    const keys = classes(env.root, 'q-key').map(text);
    assert.ok(['at its limit', 'switched off'].every((k) => keys.includes(k)) && keys.every((k) => ['at its limit', 'last known', 'switched off', 'held by another limit', 'not read'].includes(k)), 'the keys: ' + keys);
    assert.match(said(env), /3 at the limit/);
    done(env);
  } else if (mode === 'ladder') {
    // The ladder of the limit on screen: soonest first, up to four steps and the word that counts the rest; what each gives back if unused.
    // ten accounts, each to come back an hour after the one before: ten steps
    const spread = JSON.parse(JSON.stringify(views.claude_many));
    groupsOf(spread)[0].bars.forEach((b, i) => { b.resets_at = new Date(Date.now() + (i + 1) * 3600000 + 600000).toISOString(); });
    const env = await boot(spread);
    const steps = classes(env.root, 'q-step');
    assert.equal(steps.length, 4);
    const more = classes(env.root, 'q-ladder-more')[0];
    assert.match(text(more), /^\d+ more$/);
    assert.equal(more.getAttribute('aria-expanded'), 'false');
    assert.match(more.getAttribute('aria-label'), /more resets — show them below/);
    assert.ok(steps.every((s) => /^\d+[mhd]|<1m/.test(text(classes(s, 'q-step-when')[0]))));
    assert.ok(steps.every((s) => /^\+\d+\.\d$/.test(text(classes(s, 'q-step-back')[0]))));
    assert.match(steps[0].getAttribute('aria-label'), /returns \+1\.0 if unused/);
    click(more);
    await settle2();
    assert.ok(classes(env.root, 'q-step').length > 4, 'all the steps');
    assert.equal(text(classes(env.root, 'q-ladder-more')[0]), 'less');
    done(env);
    const none = await boot(views.reading_none);
    assert.equal(text(classes(none.root, 'q-ladder-none')[0]), 'not reported', 'no reset to come: said, not drawn empty');
    done(none);
  } else if (mode === 'ladder_poll') {
    let view = JSON.parse(JSON.stringify(views.claude_many));
    const g = groupsOf(view)[0];
    const at = Date.now() + 3600000;
    g.bars.forEach((b, i) => { b.state='current'; b.left=.2; b.at_limit=false; b.resets_at=new Date(i<2 ? at : at+(i+1)*3600000).toISOString(); });
    const env = await boot(() => view);
    click(byFocus(env.root, 'ring:claude:' + g.key)); await settle2();
    click(classes(env.root, 'q-step')[0]); await settle2();
    const lit = () => bars(env).filter(b=>has(b,'lit')).map(b=>b.getAttribute('data-focus')).sort();
    assert.equal(lit().length, 2);
    const key = account => 'bar:' + g.key + ':' + account;
    view = JSON.parse(JSON.stringify(view));
    groupsOf(view)[0].bars[1].resets_at = new Date(at+9*3600000).toISOString();
    groupsOf(view)[0].bars[2].resets_at = new Date(at).toISOString();
    env.interval()(); await settle2();
    assert.deepEqual(lit(), [key(g.bars[0].account),key(g.bars[2].account)].sort(), 'same step updates its current members');
    view = JSON.parse(JSON.stringify(view));
    groupsOf(view)[0].bars.forEach(b=>{if(Date.parse(b.resets_at)===at){b.resets_at=new Date(at+24*3600000).toISOString();b.left=1;}});
    env.interval()(); await settle2();
    assert.equal(lit().length, 0, 'removed reset no longer highlights old accounts');
    assert.equal(classes(env.root, 'q-step').filter(s=>has(s,'on')).length, 0);
    assert.equal(classes(env.root, 'q-bars')[0].getAttribute('data-step'), null);
    done(env);
  } else if (mode === 'unlimited_stale') {
    for (const kept of [false,true]) {
      for (const mixed of [false,true]) {
        const v = JSON.parse(JSON.stringify(views.cursor_mixed));
        if (!mixed) v.groups[0].accounts = v.groups[0].accounts.filter(a=>a.key==='cursor:a2');
        v.kept = kept;
        const a = v.groups[0].accounts.find(a=>a.key==='cursor:a2');
        if (!kept) a.resources.balances.state='last_known';
        const env=await boot(v);
        assert.ok(classes(env.root,'q-plate-cycle').length, 'dated unlimited still has a plate');
        click(byFocus(env.root,'accounts'));
        const row=byFocus(env.root,'acct:cursor:a2');
        // Cycle-list rows toggle selection; normalize the auto-pick before selecting explicitly.
        if(row.getAttribute('aria-pressed')==='true') click(row);
        click(byFocus(env.root,'acct:cursor:a2'));
        assert.equal(byFocus(env.root,'acct:cursor:a2').getAttribute('aria-pressed'),'true');
        const card=text(classes(env.root,'q-card')[0]);
        assert.match(card,/unlimited included usage/i);
        assert.match(card,/Last known/); assert.match(card,/Read /);
        const alternate=text(classes(env.root,'insp-sum')[0]);
        assert.match(alternate,/unlimited included usage/i);
        assert.match(alternate,/last known/i, 'the alternate inspector dates retained unlimited too');
        assert.doesNotMatch(card,/Not read|no reading|Its window is not read/);
        assert.ok(!bars(env).some(b=>b.getAttribute('data-focus').endsWith(':cursor:a2')));
        if(!mixed) assert.equal(pcOf(live(env)[0]),'—');
        done(env);
      }
    }
  } else if (mode === 'cycle') {
    // Cursor's budget per billing cycle: one ring (Cycle), a plate of money, the bars and the card of the top account, no chart.
    const env = await boot(views.cursor_budget);
    assert.deepEqual(live(env).map(nameOf), ['Cycle']);
    assert.equal(pressed(env).length, 1);
    assert.equal(classes(env.root, 'q-ladder').length, 0, 'a budget has no ladder of resets');
    const p = classes(env.root, 'q-plate-cycle')[0];
    assert.ok(p);
    assert.match(text(classes(p, 'q-head')[0]), /^Billing cycle/);
    assert.match(text(classes(p, 'q-money')[0]), /^\$[\d,.]+ left of \$[\d,.]+$/);
    assert.match(env.root.textContent, /No timeline for spend/);
    assert.ok(bars(env).length > 0);
    assert.ok(bars(env).every((x) => !/\d/.test(text(x))));
    assert.match(text(classes(env.root, 'q-card')[0]), /\$[\d,.]+ left of \$[\d,.]+/, 'the card says the account\'s money');
    done(env);
    const mixed = await boot(views.cursor_mixed);
    assert.match(mixed.root.textContent, /unlimited/i);
    click(byFocus(mixed.root, 'accounts'));
    click(byFocus(mixed.root, 'acct:cursor:a2'));
    assert.match(text(classes(mixed.root, 'q-card')[0]), /unlimited/i);
    assert.doesNotMatch(text(classes(mixed.root, 'q-card')[0]), /no reading|not read/i);
    assert.equal(bars(mixed).some(b => /cursor:a2/.test(b.getAttribute('data-focus'))), false);
    done(mixed);
    const soleView = JSON.parse(JSON.stringify(views.cursor_mixed));
    soleView.groups[0].accounts = soleView.groups[0].accounts.filter(a => a.key === 'cursor:a2');
    const sole = await boot(soleView);
    const soleCard = text(classes(sole.root, 'q-card')[0]);
    assert.match(soleCard, /unlimited/i);
    assert.match(soleCard, /Cycle ends in/);
    assert.match(soleCard, /Read just now/);
    assert.doesNotMatch(soleCard, /no reading|not read/i);
    assert.equal(bars(sole).length, 0);
    assert.equal(pcOf(live(sole)[0]), '—', 'unlimited is not a numeric percentage');
    done(sole);
    for (const pct of [99.6, 0.4, 50]) {
      const v = JSON.parse(JSON.stringify(views.cursor_budget));
      v.groups[0].accounts.forEach(a => {
        a.resources.balances.rows.filter(r => r.usage_kind === 'included').forEach(r => { r.amount = String(pct * 100); });
        a.resources.spending.rows.filter(r => r.usage_kind === 'included').forEach(r => { r.limit = '10000'; r.used = String(10000 - pct * 100); });
      });
      const edge = await boot(v);
      assert.equal(walk(live(edge)[0]).filter(n => n.getAttribute('class') === 'ring-arc').length, 1, 'known cycle ring retains arc at ' + pct);
      done(edge);
    }
  } else if (mode === 'failed') {
    // A failed read: the screen kept says so; no star and no note tell who is not read.
    const first = await boot(views.reading_part);
    const kept = first.testHooks.keptView(JSON.parse(JSON.stringify(views.reading_part)));
    done(first);
    const env = await boot(kept);
    assert.equal(classes(env.root, 'q-star').length, 0, 'no star');
    assert.equal(classes(env.root, 'wt-foot').length, 0, 'no note');
    assert.match(text(classes(env.root, 'q-stamp')[0]), /^Claudexor not read now · all as read /);
    done(env);
  } else {
    throw new Error('unknown case ' + mode);
  }
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exitCode = 1; });
"""

CASES = ["rings", "pick", "agy", "hidden", "reading", "states", "ladder", "ladder_poll", "unlimited_stale", "cycle", "failed"]


@pytest.mark.parametrize("case", CASES)
def test_the_rail_and_the_plate(fixture_file, case):
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    result = subprocess.run([str(node), "-e", harness + NODE_PLATE], cwd=Path(__file__).parent,
                            env={**os.environ, "WIDGET_PATH": os.environ.get("QUOTAS_REVIEW_WIDGET_PATH", str(Path(__file__).with_name("widget.js").resolve())),
                                 "PLATE_CASE": case, "PLATE_FIXTURE_FILE": fixture_file},
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# The capsule's style: its own classes alone, every size a token.

def _style():
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    return source.split("var STYLE = [", 1)[1].split("].join('');", 1)[0]


def test_the_plate_styles_only_its_own_classes_and_names_its_sizes():
    css = _style()
    section = css.split("/* capsule: window tabs */", 1)[1]
    rules = [line.strip().strip(",").strip('"') for line in section.splitlines() if line.strip().startswith('"')]
    q_rules = [r for r in rules if ".q-" in r or ".k-" in r]
    assert len(q_rules) > 60, "the plate's rules were not found"
    for rule in q_rules:
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rule), rule
        assert not re.search(r"font-size:\d", rule), "a type size is a token: " + rule
        assert "gradient(" not in rule, rule
    # the ring's value and its % are one unit, 12px, 500, tabular; the ring is 46px; the opened account's share is 14px
    assert ".q-pc{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:var(--type-meta);font-weight:var(--weight-medium);line-height:1;letter-spacing:0;font-variant-numeric:tabular-nums}" in css
    assert "--q-ring:46px;" in css and "--type-meta:12px;" in css and "--type-body:14px;" in css
    assert ".q-big{font-size:var(--type-body);" in css
    # no figure in a bar: no rule draws one, and the script writes none
    rules_only = "".join(line for line in css.splitlines() if line.strip().startswith('"'))
    assert ".q-bar .v" not in rules_only and "bar-v" not in rules_only


# ---------------------------------------------------------------------------
# Real browsers: the rectangles of the rail, the plate, the card and the bars.

FIXED = """() => { const r = (e) => e.getBoundingClientRect(); const q = (s) => document.querySelector(s); const qa = (s) => [...document.querySelectorAll(s)];
  const bars = qa('.q-bar').map(r); const plate = r(q('.q-plate')); const card = q('.q-card') ? r(q('.q-card')) : null; const pc = q('.q-pc');
  const cs = pc ? getComputedStyle(pc) : null; const gauge = q('.q-gauge') ? r(q('.q-gauge')) : null; const em = q('.q-em');
  return { scrollX: document.documentElement.scrollWidth - innerWidth, n: bars.length,
    inside: bars.every((b) => b.left >= plate.left - 0.5 && b.right <= plate.right + 0.5), rows: new Set(bars.map((b) => Math.round(b.top))).size,
    minW: Math.min(...bars.map((b) => b.width)), maxW: Math.max(...bars.map((b) => b.width)),
    pc: cs ? [cs.fontSize, cs.fontWeight, cs.fontVariantNumeric] : null, gauge: gauge ? [Math.round(gauge.width), Math.round(gauge.height)] : null,
    cardBesideBars: card && bars.length ? card.right <= bars[0].left : null, emailWraps: em ? em.scrollWidth <= em.clientWidth + 1 : null,
    digitsInBars: qa('.q-bar').some((b) => /\\d/.test(b.textContent)) };
}"""


STUB = r"""(view) => {
  window.fetch = (url, options = {}) => {
    const post = ((options && options.method) || 'GET') !== 'GET';
    return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(post ? { ok: true } : view) });
  };
}"""


def _many(view, n):
    """The view with n accounts on the limit on screen (copies of its bars, renamed), so the row is 1, 4, 20 or 40 bars."""
    view = json.loads(json.dumps(view))
    for g in view["reserve"]["summary"]["groups"]:
        base = [b for b in g["bars"] if b.get("account")]
        g["bars"] = [dict(base[i % len(base)], account="%s~%d" % (base[i % len(base)]["account"], i)) for i in range(n)]
        g["slots"] = n
    return view


@pytest.mark.parametrize("width", [598, 390])
@pytest.mark.parametrize("count", [1, 4, 20, 40])
def test_real_browser_the_plate_holds_every_account_without_sideways_scroll(browser, fixture_file, width, count):
    from test_reserve import with_i18n
    views = json.loads(Path(fixture_file).read_text())
    view = _many(views["codex_states"], count)
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.set_content('<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div></body></html>')
    page.evaluate(with_i18n(STUB), view)
    page.add_script_tag(content=Path(__file__).with_name("widget.js").read_text(encoding="utf-8"))
    page.wait_for_selector(".q-plate .q-bar", timeout=8000)
    page.wait_for_timeout(200)
    got = page.evaluate(FIXED)
    page.close()
    assert got["scrollX"] <= 0, got
    assert got["n"] == count and got["inside"], got
    assert got["minW"] >= 19.5 and got["maxW"] <= 56.5, got
    assert not got["digitsInBars"], got
    assert got["pc"] is None or got["pc"][:2] == ["12px", "500"] and "tabular-nums" in got["pc"][2], got
    if width == 598:
        assert got["gauge"] == [46, 46] and got["cardBesideBars"], got
    assert got["emailWraps"] in (None, True), got
    assert got["rows"] <= (1 if count <= 4 else 3 if count <= 20 else 5), got
