"""The top tile of the widget: the row of limit windows over one plate (capsule: window tabs).

The real widget.js runs under the same fake DOM as the other widget tests (no browser). The
views pass through the real backend (build_view + reserve_view): a Claude family with a 5-hour,
a weekly and two model-scoped weekly limits (and a history, so the graph has its Combined view),
a Codex family (a week and 30 days, no 5-hour), a Cursor family with no window at all, Cursor
families whose resources carry a budget per billing cycle (the engine's money, in minor units), and an
Antigravity family whose limits come in model groups ("Gemini-5h", "Claude and GPT-weekly").

What is held here:
- a family has a tab only for a window it reports (the places 5-hour / Weekly / 30-day, in that order): a place with no window is
  not drawn (no grey place), a window the reader hid in Rows keeps a dotted aria-disabled tab (out of the tab order, with its hint),
  and a family with no window at all has no rail and says so;
- the figure on a tab is the average left of the plate under it;
- the scope row exists only where a length has two limits or more; "All models" first, the rest by
  name; Antigravity's scopes are its groups, never a models list;
- the graph is the plate's continuation, not a second plate: no tab of the window in it and, in the window mode, no
  headline (both repeat the plate); Combined is a switch (aria-pressed) in the graph's row of controls, beside the span
  and the future, with the model choice across from them; on, it has its headline (its numbers are not the plate's);
  it never moves the window row, and a window picked above takes the graph back to the window;
- only the reader's own pick moves; a redraw of fresh data never does;
- no underline of its own: the pick is told by the number tab's dot and number (red); nothing is measured or watched;
- the stamp across from the tabs counts the picked window's accounts ("Read 14 of 16 · 18m ago"), the star sits on the percent of
  every window that is not read whole (the billing-cycle budget too), the note under the row names who is not read (the groups,
  the first three names and "N more"), the hidden copy of it keeps the plate where it is when another window wears the star, and a
  failed read has no star and no note; the row and the note fit a narrow card (no torn name, no sideways scroll), in a real browser;
- the capsule writes only what differs from the kit, and only for its own wt- classes: what the plate changes in the
  other parts' classes is written with those parts, under the window-tabs plate;
- Cursor with a budget per billing cycle: the 30-day place opens on it (not a window of time, and the tab
  says so), its plate is a window's plate in dollars - "$X left of $Y", one bar per account, the average
  share left, the cycle's end and the on-demand spend under them - it is the one tab, 5-hour and Weekly are not drawn, there is
  no chart ("No timeline for spend"), an unlimited account has no share, a last-known one is dated and not
  counted, an unknown one is not counted, and an account whose only fault is the engine's standing
  "no source" is not drawn as unavailable; without a budget there is no rail and the original text stands.
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
from conftest import RU_JSON, RU_JSON_SKIP
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


NODE_TABS = r"""
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// A clock the case holds: Date.now stands where it was when the case took it and moves only when the case moves it. The widget measures the span of a
// movement in Date.now, so a machine under load (a wait that took ten times as long) or a minute that turns (the steps of the ladder say their time in
// minutes) cannot change what a case sees. A case that holds it moves it with moveClock, never with wait.
const clockHeld = { at: 0 };
const holdClock = () => { clockHeld.at = Date.now(); Date.now = () => clockHeld.at; };
const moveClock = (ms) => { clockHeld.at += ms; };
const text = (n) => n.textContent;
const has = (n, c) => String(n.className || '').split(/\s+/).includes(c);
const click = (node) => node.listeners.click[0]({ stopPropagation() {} });
const tabsOf = (env) => classes(env.root, 'wt-tab');
// The rail says a window short ("5H", "7D", "30D"; the word on the row: shortOf); its full name is the first words of the tab's label
// ("Weekly — 42% avg left, ...", the title says the same), and nameOf is that name, the one these cases are told by.
const shortOf = (tab) => text(classes(tab, 'tab-mark-name')[0]);
const nameOf = (tab) => tab.getAttribute('aria-label').split(' — ')[0];
// The figure on a tab is the number alone, drawn inside the box of the ring (.tab-mark-count in .tab-mark-ring, the sign % in .tab-mark-unit);
// the star stands on the lower edge of the badge (the short name) as a part of its own (.tab-mark-note, aria-hidden), in the same box.
const pctOf = (tab) => text(classes(tab, 'tab-mark-count')[0]);
const noteOf = (tab) => classes(tab, 'tab-mark-note')[0];
const stampOf = (env) => classes(env.root, 'reserve-age')[0];
// The parts of a ring: svg nodes carry their class as an attribute (the fake DOM's className is the html nodes' alone).
const ringParts = (node, name) => walk(node).filter((n) => n.getAttribute && n.getAttribute('class') === name);
const footOf = (env) => classes(env.root, 'wt-foot')[0];
const groupsOf = (foot) => classes(foot, 'wt-foot-g');
// A name is a box and the comma between names stands inside the box of the name before it (a line may break before a box, so a comma
// outside it could open a line): whoOf is the names, rawWho the boxes as they are.
const rawWho = (group) => classes(group, 'wt-who').map(text);
const whoOf = (group) => rawWho(group).map((name) => name.replace(/,$/, ''));
const pickedTab = (env) => tabsOf(env).find((t) => t.getAttribute('aria-selected') === 'true');
// A fake layout for the head's tabs: one tab 100px wide after another in the order of the row; the frames wait for the test (the returned
// list); computed(node) answers getComputedStyle for a node. The fake page answers one selector alone, so the tabs of the head are
// asked for by their descendant selector here.
const withLayout = (env, computed) => {
  const frames = [];
  env.window.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
  env.window.getComputedStyle = (node) => computed(node);
  const place = (n) => { n.getBoundingClientRect = () => ({ left: (n.parentNode ? n.parentNode.childNodes.indexOf(n) : 0) * 100, top: 0 }); };
  classes(env.root, 'tab-mark').forEach(place);
  const asked = env.root.querySelectorAll.bind(env.root);
  env.root.querySelectorAll = (selector) => (selector === '.harness-seg [data-mark-key]'
    ? classes(env.root, 'harness-seg').flatMap((row) => walk(row).filter((n) => n.getAttribute && n.getAttribute('data-mark-key') !== null))
    : asked(selector));
  const make = env.document.createElement;
  env.document.createElement = (tag) => { const n = make(tag); place(n); return n; };
  return frames;
};
const plateOf = (env) => classes(env.root, 'wt-plate')[0];
const scopes = (env) => classes(env.root, 'wt-scope-opt').map(text);
const blockOf = (env) => classes(env.root, 'tl-block')[0];
const controlsOf = (env) => classes(blockOf(env), 'tl-controls')[0];
const jointSwitch = (env) => byFocus(env.root, 'graph:joint');
const heroOf = (env) => classes(env.root, 'tl-hero')[0];
const focusTab = (env, harness, seconds) => byFocus(env.root, 'win:' + harness + ':' + seconds);
// Under the bars of a plate: the legend (.lg, an item per state) and the reset (.nx, a row of cells).
const lgOf = (plate) => classes(plate, 'lg')[0];
const itemsOf = (plate) => (lgOf(plate) ? classes(lgOf(plate), 'legend-item') : []);
const nxOf = (plate) => classes(plate, 'nx')[0];
const cellsOf = (plate) => (nxOf(plate) ? nxOf(plate).childNodes.filter((n) => n.tagName === 'SPAN').map(text) : []);
const bolds = (node) => walk(node).filter((n) => n.tagName === 'B');
const figureOf = (item) => item.childNodes.find((n) => n.tagName === 'B');
const stripBars = (plate) => classes(classes(plate, 'bars')[0], 'bar');
// The words of a bar of an account are its aria-label (it carries no native title since the bars open to the side: the title would stand over the opening, in English);
// the label ends with what a press does (" — select this account", or " — selected"), which is not part of the words.
const barSaid = (bar) => bar.getAttribute('aria-label').replace(/ — (select this account|selected)$/, '');
// The whole spoken summary of the plate: the note its one control (the word Chart) is described by; the control has no label of its own.
// What is seen of a node: its text without the screen-reader-only parts (the plate's spoken note is one, and names the accounts).
const seen = (node) => classes(node, 'sr-only').reduce((rest, n) => rest.replace(text(n), ''), text(node));
const spokenOf = (env) => describedBy(env.root, classes(plateOf(env), 'l-chart')[0]);

// A ResizeObserver that counts its makers: the window row watches nothing (it had one for the sliding underline, which is gone).
globalThis.FakeRO = class {
  constructor(callback) { this.callback = callback; globalThis.FakeRO.all.push(this); }
  observe() {}
  disconnect() {}
};
globalThis.FakeRO.all = [];

(async () => {
  const views = JSON.parse(fs.readFileSync(process.env.TABS_FIXTURE_FILE, 'utf8'));
  const mode = process.env.TABS_CASE;
  const done = (env) => env.disposeHooks.forEach((hook) => hook());

  if (mode === 'places') {
    // Three places for every family, in this order, whatever the family reports.
    const enabled = { claude: ['5-hour', 'Weekly'], codex: ['Weekly', '30-day'], cursor: [], agy: ['5-hour', 'Weekly'] };
    for (const key of Object.keys(enabled)) {
      const env = await boot(views[key]);
      const tabs = tabsOf(env);
      if (!enabled[key].length) {
        // No limit at all: there is no rail (the card has the whole width) and no grey places; the note says why.
        assert.equal(tabs.length, 0, key + ': no limit, no grey places');
        assert.equal(classes(env.root, 'rail').length, 0, key + ': and no rail');
        assert.ok(!has(env.root.childNodes[1], 'has-rail'));
        done(env);
        continue;
      }
      // Only the windows the family reports have a row, in the order of the places: a place it has no window for is not drawn at all.
      assert.deepEqual(tabs.map(nameOf), enabled[key], key + ': a row for each window the family reports, and for no other');
      assert.equal(classes(env.root, 'wt-row')[0].getAttribute('role'), 'tablist');
      assert.ok(classes(env.root, 'wt-row')[0].listeners.keydown.length, key + ': arrow keys are handled on the row of windows');
      // No grey place: every row is live (a window the reader hid in Rows keeps a dotted row, which is another case).
      assert.equal(tabs.filter((t) => t.getAttribute('aria-disabled') === 'true').length, 0, key + ': no grey place');
      assert.equal(tabs.filter((t) => t.getAttribute('aria-selected') === 'true').length, 1, key);
      // One tab stop, on the picked tab.
      assert.equal(tabs.filter((t) => t.getAttribute('tabindex') === '0').length, 1, key);
      for (const t of tabs) {
        assert.ok(!t.listeners.click === false, key + ': a row opens its window');
        assert.ok(pctOf(t) !== '—' || classes(t, 'ring-track').length === 1, key + ': a row with no reading keeps its dotted ring and its dash');
      }
      // Each row has its number in the rail (the windows come down by it), a thread runs from the mark of the family down to the first
      // ring, and the rail has no caption.
      tabs.forEach((t, at) => assert.ok(t.style['--i'] === String(at), key + ': the number of the row'));
      assert.ok(classes(env.root, 'rail-lead').length === 1, key + ': the thread from the mark of the family to the first ring');
      assert.ok(classes(env.root, 'rail-cap').length === 0, key + ': and no caption');
      done(env);
    }
    // Cursor reports no time windows and no budget: no limit, so no rail (the card has the whole width), and the words say so.
    const cursor = await boot(views.cursor);
    assert.equal(tabsOf(cursor).length, 0, 'no grey places, no rail');
    assert.equal(classes(cursor.root, 'rail').length, 0);
    assert.match(cursor.root.textContent, /No quota limit reported for Cursor\./);
    assert.equal(classes(cursor.root, 'wt-plate').length, 0, 'no plate with no window');
    done(cursor);
  } else if (mode === 'average') {
    // The percent is said once: on the tab of its window. The plate under the tab does not say it again (no "avg left" line under the
    // bars, no badge "lowest left"), and the tab says it in words for the ear.
    for (const key of ['claude', 'codex', 'agy']) {
      const env = await boot(views[key]);
      const live = tabsOf(env).filter((t) => t.getAttribute('aria-disabled') !== 'true').map((t) => t.getAttribute('data-focus'));
      assert.ok(live.length >= 2, key);
      for (const focus of live) {
        const tab = byFocus(env.root, focus);
        if (tab.getAttribute('aria-selected') !== 'true') click(tab);
        const now = byFocus(env.root, focus);
        const pct = pctOf(now);
        assert.match(pct, /^(\d+|<1|>99)%$/, key + ' ' + focus);
        const plate = plateOf(env);
        assert.equal(classes(plate, 'l-sub').length, 0, key + ' ' + focus + ': no line of the average on the plate');
        assert.equal(classes(plate, 'rs-tight').length, 0, key + ' ' + focus + ': no badge of the lowest');
        assert.ok(!/avg left|lowest left/.test(text(plate)), key + ' ' + focus + ': ' + text(plate));
        // The chart of the pick stands in the plate now (.tl-top, right under the bars) and its Details say the share left on average in percent, and the card of the
        // picked account stands under the chart in the plate (.ins-wrap) and tells the shares of its windows: what is told is the plate's own parts, not the chart or the card in it.
        const own = plate.childNodes.filter((n) => !has(n, 'tl-top') && !has(n, 'ins-wrap')).map(text).join('');
        assert.ok(!own.includes('%'), key + ' ' + focus + ': the plate carries no percent: ' + own);
        assert.match(now.getAttribute('aria-label'), new RegExp(pct.replace(/[<>]/g, '\\$&') + ' avg left'));
      }
      done(env);
    }
  } else if (mode === 'scopes') {
    // Claude: the 5-hour has one limit (no scope row); the week has three - the family-wide one first, the rest by name.
    let env = await boot(views.claude);
    click(focusTab(env, 'claude', 18000));
    assert.equal(classes(env.root, 'wt-scope').length, 0, 'one limit of a length: no scope row');
    click(focusTab(env, 'claude', 604800));
    const week = scopes(env);
    assert.ok(week.length >= 2, week.join('|'));
    assert.equal(week[0], 'All models');
    const rest = week.slice(1);
    assert.deepEqual(rest, rest.slice().sort((a, b) => (a.toLowerCase() < b.toLowerCase() ? -1 : a.toLowerCase() > b.toLowerCase() ? 1 : 0)));
    assert.equal(classes(env.root, 'wt-scope')[0].getAttribute('role'), 'group');
    assert.equal(classes(env.root, 'wt-scope-opt').filter((b) => b.getAttribute('aria-pressed') === 'true').length, 1);
    done(env);
    // Codex: one limit in each length it has.
    env = await boot(views.codex);
    for (const seconds of [604800, 2592000]) {
      click(focusTab(env, 'codex', seconds));
      assert.equal(classes(env.root, 'wt-scope').length, 0, 'codex ' + seconds);
    }
    done(env);
    // Antigravity: two groups in each length. The scope is the group, by name - never a list of models.
    env = await boot(views.agy);
    for (const seconds of [18000, 604800]) {
      click(focusTab(env, 'agy', seconds));
      assert.deepEqual(scopes(env), ['Claude and GPT', 'Gemini'], 'agy ' + seconds + ': groups by name, no "All models"');
      assert.ok(!scopes(env).some((s) => /\+\d|gemini-|claude-|gpt-/i.test(s)));
      // The graph names the same group, short.
      for (const group of ['Gemini', 'Claude and GPT']) {
        click(byFocus(env.root, classes(env.root, 'wt-scope-opt').find((b) => text(b) === group).getAttribute('data-focus')));
        assert.equal(blockOf(env).getAttribute('aria-label'), 'Timeline of ' + (seconds === 18000 ? '5-hour' : 'Weekly') + ' · ' + group);
        assert.equal(classes(env.root, 'wt-scope-opt').filter((b) => b.getAttribute('aria-pressed') === 'true').map(text).join(), group);
        assert.match(spokenOf(env), new RegExp('^' + (seconds === 18000 ? '5-hour' : 'Weekly') + ' · ' + group + ' — '));
      }
    }
    done(env);
  } else if (mode === 'graph') {
    // The graph is the plate's continuation: no tab of the window in it, no headline in the window mode (the plate says
    // both), and Combined a switch in its row of controls - the other windows are chosen above.
    const env = await boot(views.claude);
    const shown = nameOf(pickedTab(env));
    assert.equal(classes(blockOf(env), 'wt-tab').length + classes(blockOf(env), 'tab-mark-num').length, 0, 'no tab in the graph');
    assert.ok(!walk(blockOf(env)).some((n) => ['tab', 'tablist'].includes(n.getAttribute('role'))), 'no tablist, not even of one');
    assert.ok(!walk(blockOf(env)).some((n) => has(n, 'tl-head') || has(n, 'wt-bar')), 'no head of its own, with or without its line');
    assert.ok(!text(blockOf(env)).startsWith(shown), 'the window\'s name is not repeated at the head of the graph: ' + text(blockOf(env)).slice(0, 40));
    assert.equal(classes(env.root, 'tl-hero').length, 0, 'the window mode draws no headline: the plate has those figures');
    assert.equal(blockOf(env).getAttribute('aria-label'), 'Timeline of ' + spokenOf(env).split(' — ')[0],
      'the graph still names its window for a screen reader');
    // Combined: a switch in the row of controls, off to begin with.
    const sw = jointSwitch(env);
    assert.ok(sw, 'Combined is there');
    assert.equal(sw.tagName, 'BUTTON');
    assert.equal(text(sw), 'Combined');
    assert.equal(sw.getAttribute('aria-pressed'), 'false');
    assert.equal(sw.getAttribute('role'), null);
    assert.match(sw.title, /^Per account the smaller of its binding limits, in units of its shortest window — a retrospective ratio of use, not compute$/);
    assert.ok(walk(controlsOf(env)).includes(sw), 'in the row of controls');
    assert.ok(classes(controlsOf(env), 'seg').length >= 3, 'the kit\'s segments: the span, the future, Combined');
    // The model choice is in the same row, after the others.
    const model = byFocus(env.root, 'tl-model');
    assert.ok(model, 'the model choice is drawn for this family');
    const order = walk(controlsOf(env));
    assert.ok(order.indexOf(model) > order.indexOf(sw) && order.indexOf(sw) > order.indexOf(byFocus(env.root, 'scenario:recent_pace')),
      'span, future, Combined, then the model choice');
    assert.equal(classes(blockOf(env), 'tl-controls').length, 1, 'one row of controls');
    // Combined on: pressed, with its headline (its numbers are not the plate's); the window row and its plate stay where they were.
    const before = pickedTab(env).getAttribute('data-focus');
    const plateKey = classes(plateOf(env), 'l-chart')[0].getAttribute('data-focus');
    click(sw);
    assert.equal(jointSwitch(env).getAttribute('aria-pressed'), 'true');
    assert.equal(classes(env.root, 'tl-hero').length, 1);
    assert.match(text(heroOf(env)), /equivalents left/);
    assert.match(text(heroOf(env)), /Next reset/);
    assert.ok(walk(blockOf(env)).includes(heroOf(env)));
    assert.equal(pickedTab(env).getAttribute('data-focus'), before, 'the window row is not touched by Combined');
    assert.equal(classes(plateOf(env), 'l-chart')[0].getAttribute('data-focus'), plateKey);
    // The same switch turns it off.
    click(jointSwitch(env));
    assert.equal(jointSwitch(env).getAttribute('aria-pressed'), 'false');
    assert.equal(classes(env.root, 'tl-hero').length, 0);
    // A window picked above takes the graph back to the window.
    click(jointSwitch(env));
    assert.equal(jointSwitch(env).getAttribute('aria-pressed'), 'true');
    const other = tabsOf(env).find((t) => t.getAttribute('aria-disabled') !== 'true' && t.getAttribute('aria-selected') !== 'true');
    assert.ok(other);
    click(other);
    await settle();
    assert.equal(jointSwitch(env).getAttribute('aria-pressed'), 'false', 'back on the window');
    assert.equal(pickedTab(env).getAttribute('data-focus'), other.getAttribute('data-focus'));
    // ... and so does a click on the window already picked, while the graph shows Combined.
    click(jointSwitch(env));
    assert.equal(jointSwitch(env).getAttribute('aria-pressed'), 'true');
    click(pickedTab(env));
    assert.equal(jointSwitch(env).getAttribute('aria-pressed'), 'false', 'a pick of the picked window still takes the graph back');
    done(env);
  } else if (mode === 'motion') {
    // Only the reader's own pick moves; a redraw of fresh data - the poll - never does.
    const env = await boot(views.claude);
    const moving = () => has(plateOf(env), 'wt-in');
    assert.ok(!moving(), 'the first drawing does not move');
    const other = tabsOf(env).find((t) => t.getAttribute('aria-disabled') !== 'true' && t.getAttribute('aria-selected') !== 'true');
    click(other);
    assert.ok(moving(), 'a window picked in the row moves');
    // A drawing inside the span of the motion (the read of the chart) carries it on: the figure and the stamp go on from where they were, they do not rise anew.
    await wait(40);
    env.interval()(); await settle();
    assert.ok(moving(), 'a drawing inside the span goes on with the motion');
    // The figure stands in the bubble of the fact (the plate's own child, the one that rises); the bubble carries the delay for its figure.
    const fig = classes(plateOf(env), 'wt-fact')[0], stamp = classes(plateOf(env), 'wt-stamp')[0];
    assert.ok(fig && classes(fig, 'l-fig').length === 1, 'the figure (l-fig) stands in the bubble of the fact');
    assert.ok(fig && stamp && parseFloat(fig.style.animationDelay) < 0 && parseFloat(stamp.style.animationDelay) < 0, 'the fact and the stamp go on from where they were: ' + fig.style.animationDelay + ' ' + stamp.style.animationDelay);
    await wait(320);
    env.interval()(); await settle();
    assert.ok(!moving(), 'the poll redraws without the class of motion');
    assert.equal(classes(env.root, 'wt-in').length, 0);
    click(jointSwitch(env));
    assert.ok(!moving(), 'the Combined switch changes the graph only: the plate does not replay its entry');
    await wait(320);
    env.interval()(); await settle();
    assert.ok(!moving(), 'and the poll after it does not');
    click(jointSwitch(env));
    assert.ok(!moving(), 'nor does turning it off');
    await wait(320);
    click(focusTab(env, 'claude', 604800));
    const pick = classes(env.root, 'wt-scope-opt').find((b) => b.getAttribute('aria-pressed') !== 'true');
    assert.ok(pick, 'the week has more than one scope');
    await wait(320);
    click(pick);
    assert.ok(moving(), 'a scope picked moves');
    await wait(320);
    env.interval()(); await settle();
    assert.ok(!moving());
    done(env);
  } else if (mode === 'family_pick') {
    // The reader's own pick of a family: the family picked stands first and the others follow in the order they had; the rail moves in
    // (start classes on the rail, the tabs slide), and only for that pick - a window's pick, the poll after the span and reduced motion
    // draw none of it.
    const merged = Object.assign({}, views.claude, { groups: [views.codex.groups[0], views.claude.groups[0], views.agy.groups[0]] });
    const heads = (e) => classes(classes(e.root, 'harness-seg')[0], 'tab-mark').map((t) => t.getAttribute('data-focus').replace('harness:', ''));
    const railOf = (e) => classes(e.root, 'rail')[0];
    holdClock();
    const env = await boot(merged);
    assert.deepEqual(heads(env), ['codex', 'claude', 'agy'], 'the family on screen stands first');
    assert.ok(classes(env.root, 'rail').every((r) => !has(r, 'rail-moving')), 'the first drawing does not move');
    // The places of the tabs: a fake layout, one tab 100px wide after another; frames wait for the test.
    const frames = [];
    env.window.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
    env.window.getComputedStyle = () => ({ transform: 'none', color: '' });
    const place = (n) => { n.getBoundingClientRect = () => ({ left: (n.parentNode ? n.parentNode.childNodes.indexOf(n) : 0) * 100, top: 0 }); };
    classes(env.root, 'tab-mark').forEach(place);
    // The fake page answers one selector alone; the tabs of the head are asked for by a descendant selector, so it answers that too.
    const asked = env.root.querySelectorAll.bind(env.root);
    env.root.querySelectorAll = (selector) => (selector === '.harness-seg [data-mark-key]'
      ? classes(env.root, 'harness-seg').flatMap((row) => walk(row).filter((n) => n.getAttribute && n.getAttribute('data-mark-key') !== null))
      : asked(selector));
    const make = env.document.createElement;
    env.document.createElement = (tag) => { const n = make(tag); place(n); return n; };
    click(byFocus(env.root, 'harness:claude'));
    await settle();
    assert.deepEqual(heads(env), ['claude', 'codex', 'agy'], 'the family picked stands first, the others after it in their order');
    const seg = classes(env.root, 'harness-seg')[0];
    const tab = (key) => classes(seg, 'tab-mark').find((t) => t.getAttribute('data-focus') === 'harness:' + key);
    assert.ok(tab('claude').getAttribute('aria-pressed') === 'true' && tab('codex').getAttribute('aria-pressed') === 'false');
    assert.ok(has(railOf(env), 'rail-moving'), 'the rail of the family picked moves in');
    assert.ok(railOf(env).style['--rail-rows'] === String(2 + 1 + classes(railOf(env), 'rail-step').length), 'it knows how many rows come down: the two windows, the head of the ladder and its steps');
    assert.ok(has(tab('claude'), 'tab-mark-slide') && tab('claude').style['--flip-x'] === '100.0px' && tab('claude').style['--flip-y'] === '0.0px',
      'the tab picked slides from its old place (the kit\'s keyframes draw it there: no frame is needed)');
    assert.ok(has(tab('codex'), 'tab-mark-slide') && tab('codex').style['--flip-x'] === '-100.0px', 'and the one it passes');
    assert.ok(!has(tab('agy'), 'tab-mark-slide'), 'a tab that did not move does not slide');
    assert.ok(classes(env.root, 'tab-mark-flip').length === 0, 'no class of an old place: the keyframes start from it');
    assert.ok(tab('claude').style['--rail-elapsed'] === undefined, 'the pick\'s own drawing starts the slide at its start');
    frames.splice(0).forEach((frame) => frame());
    assert.ok(has(tab('claude'), 'tab-mark-slide') && has(tab('codex'), 'tab-mark-slide'), 'a frame takes nothing off a slide: the animation is the kit\'s');
    // A drawing inside the span (the chart's read, a poll) carries the movement on: the same shifts on its new tabs and the slide from where it
    // was (a negative delay: --rail-elapsed), not a new slide from the places it reads (it reads none: see family_ride).
    moveClock(40);
    env.interval()(); await settle();
    assert.ok(has(railOf(env), 'rail-moving'), 'a drawing inside the span carries the movement on');
    const seg2 = classes(env.root, 'harness-seg')[0];
    const tab2 = (key) => classes(seg2, 'tab-mark').find((t) => t.getAttribute('data-focus') === 'harness:' + key);
    assert.ok(tab2('claude') !== tab('claude'), 'the tabs are new nodes');
    assert.ok(tab2('claude').style['--flip-x'] === '100.0px' && tab2('codex').style['--flip-x'] === '-100.0px', 'with the shifts of the pick, not new ones');
    assert.ok(has(tab2('claude'), 'tab-mark-slide') && has(tab2('codex'), 'tab-mark-slide') && !has(tab2('agy'), 'tab-mark-slide'), 'and the class of the slide');
    assert.ok(/^[1-9]\d*ms$/.test(tab2('claude').style['--rail-elapsed']) && tab2('codex').style['--rail-elapsed'] === tab2('claude').style['--rail-elapsed'],
      'the slide goes on from where it was: ' + tab2('claude').style['--rail-elapsed']);
    // After the span (1300 ms) a poll draws none of it, and a pick of a window never does.
    moveClock(1350);
    env.interval()(); await settle();
    assert.ok(!has(railOf(env), 'rail-moving'), 'the poll after the span does not move the rail');
    assert.ok(classes(env.root, 'tab-mark-slide').length === 0 && classes(env.root, 'tab-mark-yield').length === 0 && classes(env.root, 'tab-mark-ride').length === 0, 'nor slide a tab');
    click(tabsOf(env).find((t) => t.getAttribute('aria-selected') !== 'true'));
    assert.ok(!has(railOf(env), 'rail-moving'), 'a window picked in the row does not move the rail');
    // Picking the family on screen again changes nothing.
    click(byFocus(env.root, 'harness:claude'));
    assert.ok(!has(railOf(env), 'rail-moving'), 'the family already picked: no movement');
    done(env);
    // Reduced motion: the pick is drawn at its end.
    const calm = await boot(merged);
    calm.window.matchMedia = () => ({ matches: true });
    calm.window.requestAnimationFrame = (fn) => { fn(); return 1; };
    click(byFocus(calm.root, 'harness:claude'));
    await settle();
    assert.deepEqual(heads(calm), ['claude', 'codex', 'agy']);
    assert.ok(railOf(calm) && !has(railOf(calm), 'rail-moving'), 'under reduced motion the rail does not move');
    assert.ok(['tab-mark-flip', 'tab-mark-slide', 'tab-mark-yield', 'tab-mark-ride'].every((c) => classes(calm.root, c).length === 0), 'and no tab slides, dims or rides');
    done(calm);
  } else if (mode === 'family_mru') {
    // The family picked last stands first and the others keep the order they were seen in: a pick moves one tab to the front and the tabs it
    // passes shift one place the same way (the one before it is never sent back to where it was). The order lives in the widget's memory:
    // a fresh widget starts with the family on screen first and the rest as the answer lists them.
    const four = Object.assign({}, views.claude, { groups: [views.claude.groups[0], views.codex.groups[0], views.agy.groups[0], views.cursor.groups[0]] });
    const heads = (e) => classes(classes(e.root, 'harness-seg')[0], 'tab-mark').map((t) => t.getAttribute('data-focus').replace('harness:', ''));
    const picked = (e) => classes(classes(e.root, 'harness-seg')[0], 'tab-mark').filter((t) => t.getAttribute('aria-pressed') === 'true')
      .map((t) => t.getAttribute('data-focus').replace('harness:', ''));
    const pick = async (e, key) => { click(byFocus(e.root, 'harness:' + key)); await settle(); };
    const env = await boot(four);
    assert.deepEqual(heads(env), ['claude', 'codex', 'agy', 'cursor'], 'a fresh widget: the family on screen first, the rest as listed');
    await pick(env, 'codex');
    assert.deepEqual(heads(env), ['codex', 'claude', 'agy', 'cursor'], 'Claude, then Codex: Codex first, Claude second');
    await pick(env, 'agy');
    assert.deepEqual(heads(env), ['agy', 'codex', 'claude', 'cursor'], 'then Antigravity: Codex stays second and Claude third, they do not go back');
    assert.deepEqual(picked(env), ['agy']);
    env.interval()(); await settle();
    assert.deepEqual(heads(env), ['agy', 'codex', 'claude', 'cursor'], 'the poll does not reorder');
    click(byFocus(env.root, 'harness:agy')); await settle();
    assert.deepEqual(heads(env), ['agy', 'codex', 'claude', 'cursor'], 'the family on screen picked again changes nothing');
    await pick(env, 'cursor');
    assert.deepEqual(heads(env), ['cursor', 'agy', 'codex', 'claude']);
    await pick(env, 'claude');
    assert.deepEqual(heads(env), ['claude', 'cursor', 'agy', 'codex']);
    // A window's pick, About and Refresh keep the order too; the data-focus of a tab is the family's, so the keyboard stays on the picked tab.
    click(byFocus(env.root, 'about')); await settle();
    assert.deepEqual(heads(env), ['claude', 'cursor', 'agy', 'codex']);
    assert.ok(byFocus(env.root, 'harness:claude') !== undefined, 'the tab the keyboard was on is still found by its key, now first');
    done(env);
    // The order is in the memory of the widget and nowhere else: the next widget starts from the answer's order again.
    const again = await boot(four);
    assert.deepEqual(heads(again), ['claude', 'codex', 'agy', 'cursor'], 'nothing was kept');
    done(again);
  } else if (mode === 'family_ride') {
    // The tab picked rides over the tabs it passes while it slides (a layer for the slide), and only that one; the tabs that give way dim for
    // as long (.tab-mark-yield), and the one that did not move does neither. When its own slide ends it goes back to its layer (the end of an
    // animation of a part of it, which bubbles up to it, does not count). The shifts are worked out once, at the pick: one read of the places before
    // the drawing and one after (four tabs, four reads each), and a drawing inside the span (the chart's read, the poll) puts the same shifts on
    // its new tabs, the same classes and a negative delay, and reads no place at all. A quick second pick works new shifts out from where the tabs
    // are seen.
    const four = Object.assign({}, views.claude, { groups: [views.claude.groups[0], views.codex.groups[0], views.agy.groups[0], views.cursor.groups[0]] });
    const env = await boot(four);
    const frames = [];
    env.window.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
    env.window.getComputedStyle = () => ({ transform: 'none', color: '' });
    let reads = 0;
    const place = (n) => { n.getBoundingClientRect = () => { reads += 1; return { left: (n.parentNode ? n.parentNode.childNodes.indexOf(n) : 0) * 100, top: 0 }; }; };
    classes(env.root, 'tab-mark').forEach(place);
    const asked = env.root.querySelectorAll.bind(env.root);
    env.root.querySelectorAll = (selector) => (selector === '.harness-seg [data-mark-key]'
      ? classes(env.root, 'harness-seg').flatMap((row) => walk(row).filter((n) => n.getAttribute && n.getAttribute('data-mark-key') !== null))
      : asked(selector));
    const make = env.document.createElement;
    env.document.createElement = (tag) => { const n = make(tag); place(n); return n; };
    click(byFocus(env.root, 'harness:agy'));
    await settle();
    const tab = (key) => classes(classes(env.root, 'harness-seg')[0], 'tab-mark').find((t) => t.getAttribute('data-focus') === 'harness:' + key);
    assert.ok(reads === 8, 'one read of the places before the drawing and one after: 4 tabs, 2 reads each (' + reads + ')');
    assert.ok(tab('agy').style['--flip-x'] === '200.0px', 'it is drawn two places back: the tabs it passes shift one place the same way');
    assert.ok(tab('claude').style['--flip-x'] === '-100.0px' && tab('codex').style['--flip-x'] === '-100.0px', 'and the ones it passes sit one place back, both the same way');
    assert.ok(!has(tab('cursor'), 'tab-mark-slide') && !has(tab('cursor'), 'tab-mark-yield') && !has(tab('cursor'), 'tab-mark-ride'), 'the one that did not move does not slide');
    assert.ok(has(tab('agy'), 'tab-mark-slide') && has(tab('claude'), 'tab-mark-slide') && has(tab('codex'), 'tab-mark-slide'), 'the three that move slide');
    assert.ok(has(tab('agy'), 'tab-mark-ride'), 'the tab picked rides over the others');
    assert.ok(!has(tab('claude'), 'tab-mark-ride') && !has(tab('codex'), 'tab-mark-ride') && !has(tab('cursor'), 'tab-mark-ride'), 'and it alone');
    assert.ok(has(tab('claude'), 'tab-mark-yield') && has(tab('codex'), 'tab-mark-yield'), 'the tabs that give way dim');
    assert.ok(!has(tab('agy'), 'tab-mark-yield') && !has(tab('cursor'), 'tab-mark-yield'), 'the one that rides does not, nor the one that stands');
    frames.splice(0).forEach((frame) => frame());
    assert.ok(has(tab('agy'), 'tab-mark-slide') && has(tab('agy'), 'tab-mark-ride'), 'it rides for as long as it slides');
    assert.ok(tab('agy').listeners.animationend && tab('agy').listeners.animationend.length === 1, 'the end of its slide is heard');
    assert.ok(!tab('claude').listeners.animationend, 'the tabs that give way listen to nothing: their dimming ends with the slide');
    const ended = tab('agy').listeners.animationend[0];
    ended({ animationName: 'rail-yield', target: tab('agy') });
    ended({ animationName: 'rail-slide', target: classes(tab('agy'), 'tab-mark-icon')[0] || { not: 'the tab' } });
    assert.ok(has(tab('agy'), 'tab-mark-ride'), 'another animation, or one of its mark, does not end the ride');
    ended({ animationName: 'rail-slide', target: tab('agy') });
    assert.ok(!has(tab('agy'), 'tab-mark-ride') && has(tab('agy'), 'tab-mark-slide'), 'its own slide ends: it goes back to its layer');
    // A drawing inside the span (a poll stands for the chart's read): the same shifts on the new tabs, the same classes, the slide carried on by a
    // negative delay, and not one more read of any place.
    await wait(60);
    env.interval()(); await settle();
    assert.ok(reads === 8, 'a drawing inside the span reads no place (' + reads + ')');
    assert.ok(tab('agy').style['--flip-x'] === '200.0px' && tab('claude').style['--flip-x'] === '-100.0px' && tab('codex').style['--flip-x'] === '-100.0px', 'the same shifts');
    assert.ok(tab('agy').style['--flip-y'] === '0.0px' && tab('claude').style['--flip-y'] === '0.0px', 'on both axes');
    assert.ok(has(tab('agy'), 'tab-mark-slide') && has(tab('agy'), 'tab-mark-ride') && !has(tab('agy'), 'tab-mark-yield'), 'the one that rides rides on');
    assert.ok(has(tab('claude'), 'tab-mark-slide') && has(tab('claude'), 'tab-mark-yield') && has(tab('codex'), 'tab-mark-yield'), 'and the ones that give way dim on');
    assert.ok(!has(tab('cursor'), 'tab-mark-slide') && !has(tab('cursor'), 'tab-mark-yield'), 'the one that stands stands');
    const delay = tab('agy').style['--rail-elapsed'];
    assert.ok(/^[1-9]\d*ms$/.test(delay) && parseInt(delay, 10) < 450, 'the slide goes on from where it was: ' + delay);
    assert.ok(tab('claude').style['--rail-elapsed'] === delay && tab('codex').style['--rail-elapsed'] === delay, 'every tab from the same moment');
    assert.ok(tab('agy').listeners.animationend && tab('agy').listeners.animationend.length === 1, 'and the end of the ride is heard on the new tab');
    // Once the slide has run its time (450 ms of the 850 of the whole movement) no tab is made to slide again, though the drawing is inside the span.
    await wait(450);
    env.interval()(); await settle();
    assert.ok(reads === 8, 'still no place read (' + reads + ')');
    assert.ok(['tab-mark-slide', 'tab-mark-yield', 'tab-mark-ride'].every((c) => classes(env.root, c).length === 0), 'but no tab slides, dims or rides again');
    // A quick second pick works its shifts out again, from where the tabs are seen then (the fake places: where the tabs stood in the row).
    await wait(450);
    click(byFocus(env.root, 'harness:cursor'));
    await settle();
    assert.ok(reads === 16, 'the second pick reads the places again, once before and once after: ' + reads);
    assert.ok(tab('cursor').style['--flip-x'] === '300.0px' && has(tab('cursor'), 'tab-mark-ride'), 'the new tab picked comes from the last place');
    assert.ok(['agy', 'claude', 'codex'].every((k) => tab(k).style['--flip-x'] === '-100.0px' && has(tab(k), 'tab-mark-yield')), 'and the others step back one place');
    assert.ok(!tab('cursor').style['--rail-elapsed'] && !tab('agy').style['--rail-elapsed'], 'from the start of the slide');
    done(env);
  } else if (mode === 'hover_hold') {
    // After the pick of a family the head's tabs show no preview by the pointer: the row wears .tab-mark-still (the kit's stylesheet turns the
    // mark red under the pointer only in a row that does not). The pick sets the flag, it lives in the widget's memory (every drawing draws the
    // row wearing it) and the first real move of the pointer takes it off; a move that is none (the pointer where the click was) does not. The
    // preview is not asked of the pointer while the row is still. A window's pick and the family on screen set nothing.
    const four = Object.assign({}, views.claude, { groups: [views.claude.groups[0], views.codex.groups[0], views.agy.groups[0], views.cursor.groups[0]] });
    const env = await boot(four);
    withLayout(env, () => ({ transform: 'none', color: '' }));
    const seg = (e) => classes(e.root, 'harness-seg')[0];
    const tabIn = (e, key) => classes(seg(e), 'tab-mark').find((t) => t.getAttribute('data-focus') === 'harness:' + key);
    const move = (e, ev) => e.document.listeners.pointermove.forEach((handler) => handler(ev));
    assert.ok(!has(seg(env), 'tab-mark-still'), 'a fresh widget holds no preview');
    // The family on screen picked again, a window picked: nothing is set.
    click(tabIn(env, 'claude'));
    click(tabsOf(env).find((t) => t.getAttribute('aria-selected') !== 'true'));
    await settle();
    assert.ok(!has(seg(env), 'tab-mark-still'), 'neither the family on screen nor a window holds a preview');
    // 1. The pick of a family, with the pointer at (40, 12): the row drawn wears the flag.
    tabIn(env, 'codex').listeners.click[0]({ stopPropagation() {}, clientX: 40, clientY: 12 });
    await settle();
    assert.ok(has(seg(env), 'tab-mark-still') && tabIn(env, 'codex').getAttribute('aria-pressed') === 'true', 'the pick holds the previews by the pointer');
    assert.ok(classes(env.root, 'tab-mark-still').length === 1 && has(seg(env), 'harness-seg'), 'on the head\'s row alone');
    // 2. The flag lives in the widget's memory: a drawing draws the new row wearing it.
    const old = seg(env);
    env.interval()(); await settle();
    assert.ok(seg(env) !== old && has(seg(env), 'tab-mark-still'), 'a drawing (the poll) keeps it');
    // 3. A move that is none (the same place) takes nothing off; a move with no place is a move.
    move(env, { clientX: 40, clientY: 12 });
    assert.ok(has(seg(env), 'tab-mark-still'), 'the pointer where the click was has not moved');
    // 4. While the row is still the pointer is not asked for the preview (:hover), the keyboard is.
    const asked = [];
    const claude = tabIn(env, 'claude');
    claude.matches = (state) => { asked.push(state); return false; };
    click(claude);
    await settle();
    assert.deepEqual(asked, [':focus-visible'], 'a still row asks the keyboard alone');
    assert.ok(has(seg(env), 'tab-mark-still'), 'and a second pick holds it as well');
    // 5. The first real move takes the flag off the row at once, and a later drawing draws the row without it.
    move(env, { clientX: 41, clientY: 12 });
    assert.ok(!has(seg(env), 'tab-mark-still'), 'a real move gives the previews back');
    env.interval()(); await settle();
    assert.ok(!has(seg(env), 'tab-mark-still'), 'and a drawing after it draws none');
    move(env, { clientX: 90, clientY: 90 });
    assert.ok(!has(seg(env), 'tab-mark-still'), 'a move with nothing held is nothing');
    // 6. The pointer asked again once it has moved.
    const again = tabIn(env, 'agy');
    again.matches = (state) => { asked.push(state); return false; };
    click(again);
    await settle();
    assert.ok(asked.slice(1).join() === ':hover,:focus-visible', 'the pointer is asked while the row is not still: ' + asked.join());
    // 7. A pick by the keyboard (a click with no place) is held until the pointer moves anywhere.
    assert.ok(has(seg(env), 'tab-mark-still'));
    move(env, { clientX: 1, clientY: 1 });
    assert.ok(!has(seg(env), 'tab-mark-still'), 'any move releases a hold that has no place');
    // 8. Under reduced motion the tabs are not slid, but they are moved all the same: the hold is the same.
    done(env);
    const calm = await boot(four);
    calm.window.matchMedia = () => ({ matches: true });
    calm.window.requestAnimationFrame = (fn) => { fn(); return 1; };
    const calmSeg = () => classes(calm.root, 'harness-seg')[0];
    classes(calmSeg(), 'tab-mark').find((t) => t.getAttribute('data-focus') === 'harness:cursor').listeners.click[0]({ stopPropagation() {} });
    await settle();
    assert.ok(has(calmSeg(), 'tab-mark-still') && classes(calm.root, 'tab-mark-slide').length === 0, 'reduced motion holds the previews all the same');
    calm.document.listeners.pointermove.forEach((handler) => handler({ clientX: 3, clientY: 3 }));
    assert.ok(!has(calmSeg(), 'tab-mark-still'));
    done(calm);
  } else if (mode === 'yield_from') {
    // The tabs that give way dim in a curve (the kit's rail-yield: held while the tab picked passes). A second pick inside the movement starts
    // the dimming of a tab that is still dimming from the look it has then (--yield-from, one read of its opacity); a first pick, a pick after the
    // span and a drawing inside the span read none.
    const four = Object.assign({}, views.claude, { groups: [views.claude.groups[0], views.codex.groups[0], views.agy.groups[0], views.cursor.groups[0]] });
    const env = await boot(four);
    const seen = new Map();
    let reads = 0;
    withLayout(env, (n) => ({ transform: 'none', color: '', get opacity() { reads += 1; return seen.has(n) ? seen.get(n) : '1'; } }));
    const tab = (key) => classes(classes(env.root, 'harness-seg')[0], 'tab-mark').find((t) => t.getAttribute('data-focus') === 'harness:' + key);
    click(byFocus(env.root, 'harness:agy'));
    await settle();
    assert.ok(reads === 0, 'a first pick reads no opacity (' + reads + ')');
    assert.ok(has(tab('claude'), 'tab-mark-yield') && has(tab('codex'), 'tab-mark-yield') && !has(tab('agy'), 'tab-mark-yield'), 'two tabs give way');
    assert.ok(tab('claude').style['--yield-from'] === undefined && tab('codex').style['--yield-from'] === undefined, 'and start from the full look (the token\'s rest value)');
    // The two are seen dimmed (0.3141 and 0.43) when the reader picks again, inside the movement.
    seen.set(tab('claude'), '0.3141');
    seen.set(tab('codex'), '0.43');
    await wait(70);
    click(byFocus(env.root, 'harness:cursor'));
    await settle();
    assert.ok(reads === 2, 'one read of the opacity of each tab that was dimming (' + reads + ')');
    assert.ok(tab('claude').style['--yield-from'] === '0.314' && tab('codex').style['--yield-from'] === '0.43', 'the dimming goes on from the look seen');
    assert.ok(has(tab('agy'), 'tab-mark-yield') && has(tab('claude'), 'tab-mark-yield') && has(tab('codex'), 'tab-mark-yield'), 'all three give way now');
    assert.ok(tab('agy').style['--yield-from'] === undefined, 'the one that was not dimming starts from the full look');
    assert.ok(has(tab('cursor'), 'tab-mark-ride') && !has(tab('cursor'), 'tab-mark-yield') && tab('cursor').style['--yield-from'] === undefined, 'the tab picked dims none');
    // A drawing inside the span puts the same on its new tabs and reads nothing.
    await wait(40);
    env.interval()(); await settle();
    assert.ok(reads === 2, 'a drawing inside the span reads nothing (' + reads + ')');
    assert.ok(tab('claude').style['--yield-from'] === '0.314' && tab('codex').style['--yield-from'] === '0.43' && has(tab('claude'), 'tab-mark-yield'), 'and keeps what the pick worked out');
    // After the span (1300 ms: the rings, the ladder of resets and the foot) a pick reads no opacity.
    await wait(1350);
    seen.set(tab('claude'), '0.5');
    click(byFocus(env.root, 'harness:claude'));
    await settle();
    assert.ok(reads === 2, 'a pick after the span reads none (' + reads + ')');
    assert.ok(classes(env.root, 'tab-mark-yield').every((t) => t.style['--yield-from'] === undefined), 'and every tab starts from the full look');
    done(env);
  } else if (mode === 'clock_start') {
    // A drawing inside the movement goes on by the page's own clock: the time of the pick on document.timeline is kept at the pick, and after
    // every drawing of the movement (the pick's own and the ones inside its span) each animation of the movement takes it as its start time, once
    // (document.getAnimations, by the name of the keyframes: a transition is left alone), and the drawing puts no delay of its own
    // (--rail-elapsed). A page with no clock goes on by the delay, and nothing is touched under reduced motion or after the span.
    const four = Object.assign({}, views.claude, { groups: [views.codex.groups[0], views.claude.groups[0], views.agy.groups[0], views.cursor.groups[0]] });
    class Anim {
      constructor(name) { this.animationName = name; this.writes = 0; this.held = null; }
      get startTime() { return this.held; }
      set startTime(value) { this.writes += 1; this.held = value; }
    }
    // What the engine makes of the classes of the movement, one animation for each (the same object while the node stands).
    const engine = (env, timeline) => {
      const made = new Map();
      const state = { asks: 0, transition: { transitionProperty: 'color', startTime: null } };
      env.document.timeline = timeline;
      env.document.getAnimations = () => {
        state.asks += 1;
        const out = [state.transition];
        const add = (node, name) => {
          if (!made.has(node)) made.set(node, {});
          const kept = made.get(node);
          out.push(kept[name] || (kept[name] = new Anim(name)));
        };
        walk(env.root).forEach((n) => {
          if (has(n, 'tab-mark-slide')) add(n, 'rail-slide');
          if (has(n, 'tab-mark-yield')) add(n, 'rail-yield');
        });
        classes(env.root, 'rail-moving').forEach((rail) => walk(rail).forEach((n) => {
          if (has(n, 'rail-lead')) add(n, 'rail-draw');
          if (has(n, 'tab-mark-rail')) add(n, 'rail-drop');
          if (has(n, 'rail-bot') || has(n, 'wt-foot-in')) add(n, 'rail-fade');
        }));
        return out;
      };
      state.current = () => walk(env.root).flatMap((n) => (made.has(n) ? Object.values(made.get(n)) : []));
      return state;
    };
    holdClock();
    const env = await boot(four);
    withLayout(env, () => ({ transform: 'none', color: '' }));
    const timeline = { currentTime: 5000 };
    const clock = engine(env, timeline);
    const tab = (key) => classes(classes(env.root, 'harness-seg')[0], 'tab-mark').find((t) => t.getAttribute('data-focus') === 'harness:' + key);
    const names = () => Array.from(new Set(clock.current().map((a) => a.animationName))).sort().join();
    const noDelay = () => classes(env.root, 'tab-mark').concat(classes(env.root, 'rail')).every((n) => n.style['--rail-elapsed'] === undefined);
    // 1. The pick's own drawing: every animation of the movement starts at the time of the pick, once; no delay of the drawing's own.
    click(byFocus(env.root, 'harness:claude'));
    await settle();
    assert.ok(clock.asks > 0 || clock.current().length > 0, 'the animations were asked for');
    // (four: the fade was the foot's, with the stamp; the stamp stands in the plate now and rises with it, wt-rise, which is the plate's own; rail-fade stays only for the note over the plate, which this view has none of)
    assert.ok(names() === 'rail-draw,rail-drop,rail-slide,rail-yield', 'all four keyframes of the movement: ' + names());
    assert.ok(clock.current().every((a) => a.held === 5000 && a.writes === 1), 'each takes the time of the pick, once');
    assert.ok(clock.transition.startTime === null, 'a transition is left alone');
    assert.ok(noDelay(), 'the pick\'s own drawing puts no delay: the clock starts it');
    // 2. A drawing inside the span (the clock has gone on to 5060): new nodes, new animations, the same start time, no delay.
    timeline.currentTime = 5060;
    moveClock(60);
    const before = clock.current();
    env.interval()(); await settle();
    const now = clock.current();
    assert.ok(now.length > 0 && now.every((a) => !before.includes(a)), 'the drawing made new animations');
    assert.ok(names() === 'rail-draw,rail-drop,rail-slide,rail-yield', 'the same four: ' + names());
    assert.ok(now.every((a) => a.held === 5000 && a.writes === 1), 'each of them starts at the time of the pick (5000), not at the time of the drawing');
    assert.ok(noDelay() && has(tab('claude'), 'tab-mark-slide') && has(tab('codex'), 'tab-mark-yield'), 'the same classes and still no delay');
    // 3. A second pick inside the span is a new count from its own time.
    timeline.currentTime = 5200;
    click(byFocus(env.root, 'harness:codex'));
    await settle();
    assert.ok(clock.current().length > 0 && clock.current().every((a) => a.held === 5200 && a.writes === 1), 'a new pick starts the count again from its own time');
    // 4. After the span (the rail's 850 ms, and the sweep of the rings, 1400 ms: SWEEP_MS) a drawing moves nothing and asks for nothing.
    moveClock(1450);
    const asks = clock.asks;
    timeline.currentTime = 6500;
    env.interval()(); await settle();
    assert.ok(classes(env.root, 'rail-moving').length === 0 && clock.asks === asks, 'the poll after the span asks for no animation');
    done(env);
    // 5. A page whose clock does not run (currentTime null) goes on by the delay: the same drawing puts --rail-elapsed.
    const slow = await boot(four);
    withLayout(slow, () => ({ transform: 'none', color: '' }));
    const stopped = engine(slow, { currentTime: null });
    click(byFocus(slow.root, 'harness:claude'));
    await settle();
    moveClock(50);
    slow.interval()(); await settle();
    const slowTab = (key) => classes(classes(slow.root, 'harness-seg')[0], 'tab-mark').find((t) => t.getAttribute('data-focus') === 'harness:' + key);
    assert.ok(stopped.asks === 0 && stopped.current().length === 0, 'no clock: no animation is touched');
    assert.ok(/^[1-9]\d*ms$/.test(slowTab('claude').style['--rail-elapsed']), 'and the drawing inside the span goes on by the delay: ' + slowTab('claude').style['--rail-elapsed']);
    done(slow);
    // 6. Under reduced motion nothing moves and nothing is asked.
    const calm = await boot(four);
    calm.window.matchMedia = () => ({ matches: true });
    calm.window.requestAnimationFrame = (fn) => { fn(); return 1; };
    const still = engine(calm, { currentTime: 7000 });
    click(byFocus(calm.root, 'harness:claude'));
    await settle();
    assert.ok(still.asks === 0, 'under reduced motion no animation is asked for');
    done(calm);
  } else if (mode === 'foot_motion') {
    // The note under the rows (the accounts not read) moves with the rail on the pick of a family: it is not seen alone in the empty rail, it
    // fades in after the last window (wt-foot-in, on the rail's time) and the rail does not give it the rise of a window's pick (wt-in). A
    // window's pick gives it that rise as before, a drawing outside the span none of them.
    const merged = Object.assign({}, views.reading_both, { groups: [views.codex.groups[0], views.reading_both.groups[0]] });
    const env = await boot(merged);
    const railOf = (e) => classes(e.root, 'rail')[0];
    // The note stands in the card, over the plate (it does not fit the narrow rail): its rise is the card's (wt-in on the section), its fade
    // after the last window is its own, with the rail's numbers set on it (it is out of the rail's tree).
    const cardOf = (e) => classes(e.root, 'reserve')[0];
    const noteOf2 = (e) => classes(e.root, 'wt-foot').find((n) => n.getAttribute('role') === 'note');
    click(byFocus(env.root, 'harness:claude'));
    await settle();
    assert.ok(railOf(env) && has(railOf(env), 'rail-moving'), 'the rail of the family picked moves in');
    assert.ok(noteOf2(env) !== undefined, 'the family has accounts not read: the note');
    assert.ok(has(noteOf2(env), 'wt-foot-in'), 'the note moves with the rail');
    assert.ok(!has(cardOf(env), 'wt-in'), 'and not with the rise of a window\'s pick');
    const rowsAll = String(2 + 1 + classes(railOf(env), 'rail-step').length);
    assert.ok(tabsOf(env).length === 2 && railOf(env).style['--rail-rows'] === rowsAll && noteOf2(env).style['--rail-rows'] === rowsAll, 'it comes after the last row: the two windows and the ladder of resets');
    assert.ok(!classes(railOf(env), 'wt-foot').length && noteOf2(env).parentNode === plateOf(env), 'and it stands in the plate, not in the rail');
    await wait(1350);
    env.interval()(); await settle();
    assert.ok(!has(railOf(env), 'rail-moving') && !has(noteOf2(env), 'wt-foot-in') && !has(cardOf(env), 'wt-in'), 'the poll after the span moves nothing');
    // A window's pick: the note rises as before, and the rail does not move.
    click(tabsOf(env).find((t) => t.getAttribute('aria-selected') !== 'true'));
    await settle();
    assert.ok(!has(railOf(env), 'rail-moving') && !has(noteOf2(env) || railOf(env), 'wt-foot-in'), 'a window picked does not move the rail');
    done(env);
    // Under reduced motion neither class is worn.
    const calm = await boot(merged);
    calm.window.matchMedia = () => ({ matches: true });
    click(byFocus(calm.root, 'harness:claude'));
    await settle();
    assert.ok(classes(calm.root, 'wt-foot-in').length === 0 && classes(calm.root, 'rail-moving').length === 0 && classes(calm.root, 'wt-in').length === 0,
      'under reduced motion the note is at its place at once');
    done(calm);
  } else if (mode === 'ring_sweep') {
    // The rings sweep (kit: ring, motion) for the reader's own pick and for nothing else. A window picked: its ring alone wears ring-sweep (and the
    // property --ring-share, the share it sweeps to), at once; a family picked: every ring of the rail whose share is known, each waiting for its row
    // (ring-sweep-row); the first drawing, the 30 s poll after the span and a pick under reduced motion: none. A drawing inside the span (1400 ms)
    // keeps the sweep, so it does not stop half way; a scope's pick does not start one.
    const sweeping = (env) => walk(env.root).filter((n) => has(n, 'ring-sweep'));
    const two = Object.assign({}, views.claude, { groups: [views.claude.groups[0], views.codex.groups[0]] });
    const env = await boot(two);
    assert.ok(sweeping(env).length === 0, 'the first drawing sweeps nothing');
    const arcShare = (svg) => ringParts(svg, 'ring-arc')[0].getAttribute('stroke-dasharray').split(' ')[0];
    // 1. A window picked (a click, the keyboard presses the same button): the ring of that window alone.
    const other = tabsOf(env).find((t) => t.getAttribute('aria-selected') !== 'true' && t.getAttribute('aria-disabled') !== 'true');
    assert.ok(other, 'there is a window to pick');
    click(other);
    await settle();
    assert.ok(pickedTab(env) === classes(env.root, 'wt-tab').find((t) => t.getAttribute('aria-selected') === 'true'));
    const lit = sweeping(env);
    assert.ok(lit.length === 1, 'one ring sweeps: ' + lit.length);
    assert.ok(classes(pickedTab(env), 'ring').length === 1 && classes(pickedTab(env), 'ring')[0] === lit[0], 'it is the ring of the window picked');
    assert.ok(!has(lit[0], 'ring-sweep-row'), 'a window\'s pick waits for no row');
    assert.ok(lit[0].style['--ring-share'] === arcShare(lit[0]), 'the keyframes read the share the arc stands at: ' + lit[0].style['--ring-share']);
    // 2. A drawing inside the span (the poll comes at once here) carries the sweep on in the new rings; a scope's pick starts none of its own.
    env.interval()(); await settle();
    assert.ok(sweeping(env).length === 1 && classes(pickedTab(env), 'ring')[0] === sweeping(env)[0], 'a drawing inside the span keeps the sweep on the ring');
    // 3. After the span the poll sweeps nothing.
    await wait(1450);
    env.interval()(); await settle();
    assert.ok(sweeping(env).length === 0, 'the poll after the span sweeps nothing');
    // 4. A family picked: every ring with a share sweeps, waiting for its row.
    // (the fake page serves one answer, claude's: the other family is picked and left, and the pick back to claude is the one that draws its rail)
    click(byFocus(env.root, 'harness:codex'));
    await settle();
    await wait(1450);
    click(byFocus(env.root, 'harness:claude'));
    await settle();
    const rings = tabsOf(env).map((t) => classes(t, 'ring')[0]);
    const known = rings.filter((r) => ringParts(r, 'ring-arc').length === 1);
    assert.ok(known.length >= 1, 'the family has a ring with a share');
    assert.ok(sweeping(env).length === known.length && known.every((r) => has(r, 'ring-sweep') && has(r, 'ring-sweep-row')), 'every ring with a share sweeps with its row');
    assert.ok(rings.filter((r) => ringParts(r, 'ring-arc').length === 0).every((r) => !has(r, 'ring-sweep')), 'a ring with no share has nothing to sweep');
    assert.ok(known.every((r) => r.style['--ring-share'] === arcShare(r)), 'each keyframes read its own share');
    await wait(1450);
    env.interval()(); await settle();
    assert.ok(sweeping(env).length === 0, 'the poll after the span sweeps nothing');
    // 5. Under reduced motion nothing sweeps.
    env.window.matchMedia = (query) => ({ matches: /reduce/.test(query) });
    const next = tabsOf(env).find((t) => t.getAttribute('aria-selected') !== 'true' && t.getAttribute('aria-disabled') !== 'true');
    if (next) { click(next); await settle(); assert.ok(sweeping(env).length === 0, 'a pick under reduced motion sweeps nothing'); }
    click(byFocus(env.root, 'harness:claude'));
    await settle();
    assert.ok(sweeping(env).length === 0, 'nor does a family\'s');
    done(env);
  } else if (mode === 'lead_on') {
    // The thread from the mark of the family ends over the first ring, as high as the ring has grown when it is the one picked (the class
    // that carries the shorter end, rail-lead-on) and at its rest height when another window is picked.
    const env = await boot(views.claude);
    const lead = () => classes(env.root, 'rail-lead')[0];
    if (pickedTab(env) !== tabsOf(env)[0]) { click(tabsOf(env)[0]); await settle(); }
    assert.ok(pickedTab(env) === tabsOf(env)[0], 'the first window is the one picked');
    assert.ok(has(lead(), 'rail-lead-on'), 'the first ring is picked: the thread ends over the ring grown');
    click(tabsOf(env)[1]);
    await settle();
    assert.ok(pickedTab(env) === tabsOf(env)[1]);
    assert.ok(!has(lead(), 'rail-lead-on'), 'another ring is picked: the first is at its rest size, the thread ends at its rest height');
    click(tabsOf(env)[0]);
    await settle();
    assert.ok(has(lead(), 'rail-lead-on'), 'the first again');
    done(env);
    // Codex reports no 5-hour window: its first row is the weekly, and when that is picked the thread ends high too; the 30-day picked, low.
    const codex = await boot(views.codex);
    const codexLead = () => classes(codex.root, 'rail-lead')[0];
    assert.ok(pickedTab(codex) === tabsOf(codex)[0] === has(codexLead(), 'rail-lead-on'), 'the end follows the first row, whichever window it is');
    click(tabsOf(codex)[1]);
    await settle();
    assert.ok(!has(codexLead(), 'rail-lead-on'));
    done(codex);
  } else if (mode === 'chart_wait') {
    // While the chart of a window is read, its place has the layout of the drawn block - the plot's own empty canvas (the height drawChart draws
    // to: 210, and 180 on a narrow card), a line of legend and the row of Details - so the card keeps its height and the stamp at the foot of the
    // rail stands still.
    const env = await boot(views.claude);
    // A window picked: the chart of its limit is asked for, and until it arrives the place that waits for it is drawn (the drawing is at the click).
    click(tabsOf(env).find((t) => t.getAttribute('aria-selected') !== 'true'));
    const waits = classes(env.root, 'chart-wait');
    assert.ok(waits.length > 0, 'the chart of the window is not on hand: its place is drawn');
    const canvasOf = (n) => walk(n).find((k) => k.getAttribute('class') === 'chart-svg');
    for (const n of waits) {
      const canvas = canvasOf(n);
      assert.ok(canvas && canvas.getAttribute('height') === '210', 'the place holds the height of the plot (210 on a wide card): ' + (canvas && canvas.getAttribute('height')));
      assert.ok(/^0 0 \d+ 210$/.test(canvas.getAttribute('viewBox')), 'and scales as the plot does (the same viewBox): ' + canvas.getAttribute('viewBox'));
      assert.ok(/Loading the timeline/.test(text(n)), 'and says what it waits for: ' + JSON.stringify(text(n)));
    }
    // (the legend's line and the row of Details are held too, hidden from a reader of the screen, in the order the drawn block has them)
    assert.ok(classes(env.root, 'chart-hold').length === waits.length && classes(env.root, 'tl-body').some((b) => classes(b, 'calm-legend').length === 1 && classes(b, 'chart-wait').length === 1),
              'a line of legend under the canvas and the row of Details under the body');
    // On a narrow card (a width under 460 for the plot) the plot is 180 high, and so is its place.
    await settle();
    const make = env.document.createElement;
    env.document.createElement = (tag) => { const n = make(tag); n.clientWidth = 400; return n; };
    env.window.getComputedStyle = () => ({ paddingLeft: '8px', paddingRight: '8px', transform: 'none', color: '' });
    env.interval()(); await settle();
    const narrow = classes(env.root, 'chart-wait');
    assert.ok(narrow.length > 0 && narrow.every((n) => canvasOf(n).getAttribute('height') === '180'), 'the place holds 180 on a narrow card: ' + narrow.map((n) => canvasOf(n).getAttribute('height')));
    done(env);
  } else if (mode === 'hidden_row') {
    // A window the reader hid in Rows is still reported by the family: its row stays, dotted (aria-disabled, out of the tab order) with its
    // hint; a place the family has no window for (Claude: the 30-day one) has no row at all.
    const hid = JSON.parse(JSON.stringify(views.claude));
    const weekly = hid.reserve.summary.groups.filter((g) => g.window_seconds === 604800).map((g) => g.key);
    assert.ok(weekly.length > 0, 'the fixture has weekly limits to hide');
    hid.display = { hidden: weekly };
    const env = await boot(hid);
    const tabs = tabsOf(env);
    assert.deepEqual(tabs.map(nameOf), ['5-hour', 'Weekly'], 'the two windows Claude reports, and no 30-day row');
    const off = tabs.filter((t) => t.getAttribute('aria-disabled') === 'true');
    assert.ok(off.length === 1 && nameOf(off[0]) === 'Weekly', 'the window the reader hid keeps a dotted row');
    assert.ok(off[0].getAttribute('tabindex') === '-1', 'out of the tab order');
    assert.equal(off[0].title, 'Weekly \u2014 hidden in Rows');
    assert.equal(off[0].getAttribute('aria-label'), 'Weekly \u2014 hidden in Rows');
    assert.equal(pctOf(off[0]), '\u2014');
    assert.ok(tabs.filter((t) => t.getAttribute('aria-disabled') !== 'true').length === 1, 'the other one is a live row');
    done(env);
    // Every limit hidden: no plate to hold the stamp, so it stands in the head of the card (as where there is no window), once.
    const none = JSON.parse(JSON.stringify(views.claude));
    none.display = { hidden: none.reserve.summary.groups.map((g) => g.key) };
    const bare = await boot(none);
    assert.ok(classes(bare.root, 'wt-plate').length === 0, 'no plate with every limit hidden');
    const heads = classes(bare.root, 'reserve-head');
    assert.ok(heads.length === 1 && classes(heads[0], 'reserve-age').length === 1 && classes(bare.root, 'reserve-age').length === 1, 'the stamp is drawn, in the head: ' + heads.length + ' ' + classes(bare.root, 'reserve-age').length);
    assert.ok(/ago|Read|read/.test(text(classes(heads[0], 'reserve-age')[0])), 'and it says something: ' + text(classes(heads[0], 'reserve-age')[0]));
    done(bare);
  } else if (mode === 'no_ink') {
    // No underline of its own: the pick is told by the number tab's dot and number (red), so nothing is measured and nothing is watched.
    const bare = await boot(views.cursor);
    assert.equal(classes(bare.root, 'wt-row').length, 0, 'cursor: no limit, no row');
    done(bare);
    for (const key of ['claude']) {
      const env = await boot(views[key]);
      const row = classes(env.root, 'wt-row')[0];
      assert.ok(row, key + ': the row');
      assert.equal(classes(env.root, 'wt-ink').length, 0, key + ': no mark under the tabs');
      assert.equal(classes(env.root, 'wt-inked').length, 0);
      assert.equal(Object.keys(row.style).length, 0, 'and nothing measured is written on the row');
      assert.ok(tabsOf(env).every((t) => has(t, 'tab-mark') && has(t, 'tab-mark-num')), 'every tab is the kit\'s number tab');
      done(env);
    }
    assert.equal(globalThis.FakeRO.all.length, 0, 'nothing is watched: the row makes no ResizeObserver');
  } else if (mode === 'reading') {
    // The stamp across from the tabs counts the picked window's accounts; the star sits on the percent of a window not read whole.
    let env = await boot(views.reading_all);
    let stamp = stampOf(env);
    assert.match(text(stamp), /^Read 16 of 16 · /);
    assert.equal(classes(stamp, 'wt-n')[0].className, 'wt-n', 'all read: no mark of a lack');
    // Three short lines for the narrow rail: the word, the count, the dot with the age.
    assert.equal(text(classes(stamp, 'wt-read')[0]), 'Read', 'the first line of the stamp is the word');
    assert.equal(text(classes(stamp, 'wt-n')[0]), '16 of 16', 'the second line is the count');
    assert.ok(classes(stamp, 'wt-sep').length === 1 && classes(stamp, 'wt-age').length === 1, 'the third is the dot and the age');
    assert.equal(classes(env.root, 'tab-mark-note').length, 0, 'no star while all are read');
    assert.ok(footOf(env) === undefined, 'and no note');
    assert.equal(classes(env.root, 'wt-foot-hold').length, 0, 'and no place kept for one');
    assert.match(pickedTab(env).getAttribute('aria-label'), /avg left, 16 of 16 accounts read$/);
    done(env);
    env = await boot(views.reading_part);
    stamp = stampOf(env);
    assert.match(text(stamp), /^Read 14 of 16 · /);
    assert.equal(classes(stamp, 'wt-n')[0].className, 'wt-n lack', 'not all read: the number read is the darker');
    assert.equal(text(classes(stamp, 'wt-n')[0]), '14 of 16');
    const tab = pickedTab(env);
    assert.equal(noteOf(tab).textContent, '*');
    assert.equal(noteOf(tab).getAttribute('aria-hidden'), 'true', 'the star is for the eye: the label says it in words');
    assert.match(pctOf(tab), /^\d+%$/, 'the figure is the number alone');
    assert.match(tab.getAttribute('aria-label'), /avg left, 14 of 16 accounts read$/);
    assert.equal(classes(stamp, 'wt-sep').length, 1);
    // The pick is told to a reader of the screen by the tab's own state, not by the red of its dot and number: a tab of a tablist that
    // says it is selected (and the others that they are not), whose label names the window and what it counts.
    assert.equal(tab.getAttribute('role'), 'tab');
    assert.equal(tab.getAttribute('aria-selected'), 'true', 'the picked window is "selected" to a screen reader');
    assert.equal(tab.getAttribute('aria-controls'), 'quotas-win-panel');
    assert.ok(tabsOf(env).filter((t) => t !== tab).every((t) => t.getAttribute('role') === 'tab' && t.getAttribute('aria-selected') === 'false'),
      'every other place says it is not selected');
    assert.ok(/^(5-hour|Weekly|30-day) — /.test(tab.getAttribute('aria-label')), 'and the label starts with the name of the window');
    done(env);
    // Two windows: the count is the picked window's own, and the star stays on the tab of the window that is not read whole.
    env = await boot(views.reading_two);
    const starred = () => tabsOf(env).filter((t) => classes(t, 'tab-mark-note').length).map(nameOf);
    assert.match(text(stampOf(env)), /^Read 3 of 4 · /);
    assert.deepEqual(starred(), ['5-hour']);
    click(focusTab(env, 'claude', 604800));
    assert.match(text(stampOf(env)), /^Read 4 of 4 · /, 'the weekly is read whole');
    assert.deepEqual(starred(), ['5-hour'], 'the star stays on the 5-hour though the weekly is picked');
    assert.equal(classes(classes(env.root, 'tab-mark-num').find((t) => nameOf(t) === 'Weekly'), 'tab-mark-note').length, 0);
    done(env);
    // No window at all: no count, no star.
    env = await boot(views.cursor);
    assert.ok(!/Read \d+ of \d+/.test(text(stampOf(env))), text(stampOf(env)));
    assert.equal(classes(env.root, 'tab-mark-note').length, 0);
    done(env);
  } else if (mode === 'foot') {
    // The note under the row: the star, "Not read:", the groups (last known first, then each reason), the names, the first three and "N more".
    let env = await boot(views.reading_part);
    let foot = footOf(env);
    assert.ok(foot, 'the picked window has accounts not read: the note');
    assert.equal(foot.getAttribute('role'), 'note');
    assert.equal(foot.getAttribute('id'), 'quotas-foot');
    assert.ok(has(foot, 'meta-line') && !has(foot, 'wt-foot-hold'));
    assert.equal(pickedTab(env).getAttribute('aria-describedby'), 'quotas-foot', 'the picked tab is described by the note');
    assert.equal(tabsOf(env).filter((t) => t.getAttribute('aria-describedby')).length, 1);
    // The head of the note counts what it names: "No fresh data · 2", the number of accounts (one last known, one unreadable), then the reasons.
    // A group reads "<words> · <names>" and the groups stand apart by " · ".
    assert.match(text(foot), /^\* No fresh data · 2 read 3 ?h ago · c05 · unreadable · c12$/);
    assert.equal(text(classes(foot, 'wt-foot-head')[0]), 'No fresh data · 2', 'the head says the count');
    const star = classes(foot, 'wt-foot-star')[0];
    assert.equal(star.textContent, '*');
    assert.equal(star.getAttribute('aria-hidden'), 'true');
    const groups = groupsOf(foot);
    assert.deepEqual(groups.map((g) => g.getAttribute('data-k')), ['last', 'unknown']);
    assert.deepEqual(groups.map(whoOf), [['c05'], ['c12']]);
    // A group takes the keyboard and has a name: its words and its names. It knows the accounts it stands for, and the bars of the plate
    // know theirs, so the plate can keep the group's bars bright.
    assert.ok(groups.every((g) => g.getAttribute('tabindex') === '0' && g.getAttribute('role') === 'group'));
    assert.match(groups[0].getAttribute('aria-label'), /^read 3 ?h ago — c05$/);
    assert.equal(groups[1].getAttribute('aria-label'), 'unreadable — c12');
    assert.deepEqual(groups.map((g) => JSON.parse(g.getAttribute('data-accounts'))), [['claude:c05'], ['claude:c12']]);
    assert.deepEqual(groups.map((g) => g.getAttribute('data-focus')), ['foot:last_known', 'foot:unreadable']);
    assert.ok(groups.every((g) => ['mouseenter', 'mouseleave', 'focus', 'blur'].every((e) => g.listeners[e] && g.listeners[e].length === 1)));
    const barKeys = stripBars(plateOf(env)).map((b) => b.getAttribute('data-account'));
    assert.equal(barKeys.length, 16);
    assert.ok(['claude:c05', 'claude:c12'].every((k) => barKeys.includes(k)), 'the bars carry the account they stand for');
    done(env);
    // More than three names in a group: the first three and "N more", which opens the account list. Two reasons, two groups.
    env = await boot(views.reading_many);
    foot = footOf(env);
    const many = groupsOf(foot);
    assert.deepEqual(many.map((g) => g.getAttribute('data-k')), ['last', 'unknown', 'unknown']);
    assert.deepEqual(many.map(whoOf), [['c01', 'c02', 'c03'], ['c10'], ['c11']]);
    assert.deepEqual(many.map(rawWho), [['c01,', 'c02,', 'c03'], ['c10'], ['c11']], 'the comma is inside the name before it; the last name has none');
    assert.ok(many.every((g) => g.childNodes.filter((n) => n.tagName === '#text').every((n) => !/^\s*,/.test(n._text))),
      'and no text between the names opens with a comma');
    assert.match(text(many[0]), / · c01, c02, c03$/, 'the words, then the names after the dot');
    assert.deepEqual(many.map((g) => JSON.parse(g.getAttribute('data-accounts'))).map((a) => a.length), [5, 1, 1],
      'the group stands for all five accounts, though it names three');
    assert.match(many[1].getAttribute('aria-label'), /^unreadable — c10$/);
    assert.match(many[2].getAttribute('aria-label'), /^sources disagree — c11$/);
    const buttons = classes(foot, 'wt-foot-more');
    assert.equal(buttons.length, 1);
    assert.equal(buttons[0].tagName, 'BUTTON');
    assert.ok(has(buttons[0], 'cover-link'));
    assert.equal(text(buttons[0]), '2 more');
    assert.equal(buttons[0].getAttribute('aria-label'), '2 more accounts — open the account list');
    assert.equal(buttons[0].getAttribute('data-focus'), 'foot-more:last_known');
    // The word over the bars ("Accounts · N", data-focus=accounts) carries aria-pressed now; the bottom toggle with aria-expanded is gone.
    assert.equal(byFocus(env.root, 'accounts').getAttribute('aria-pressed'), 'false');
    click(buttons[0]);
    await settle();
    assert.equal(byFocus(env.root, 'accounts').getAttribute('aria-pressed'), 'true', 'the account list is open');
    done(env);
    // The window row of a window with nothing unread draws none (reading_all, above); a window the engine did not name the accounts of
    // counts them: "N accounts".
    const anon = JSON.parse(JSON.stringify(views.reading_part));
    for (const g of anon.reserve.summary.groups) for (const b of g.bars) delete b.account;
    env = await boot(anon);
    const nameless = groupsOf(footOf(env));
    assert.deepEqual(nameless.map(whoOf), [['1 account'], ['1 account']]);
    assert.ok(nameless.every((g) => g.getAttribute('data-accounts') === null), 'no account to light: the plate dims to the kind of bar');
    done(env);
  } else if (mode === 'failed') {
    // A failed read: the figures are dashes, the stamp says so in the ink, and nothing is told of who is not read - none is.
    const first = await boot(views.reading_part);
    const kept = first.testHooks.keptView(JSON.parse(JSON.stringify(views.reading_part)));
    done(first);
    let env = await boot(kept);
    assert.equal(pctOf(pickedTab(env)), '—');
    assert.equal(classes(env.root, 'tab-mark-note').length, 0, 'no star');
    assert.ok(footOf(env) === undefined, 'no note');
    assert.equal(classes(env.root, 'wt-foot-hold').length, 0, 'and no place kept for one');
    assert.equal(pickedTab(env).getAttribute('aria-describedby'), null);
    assert.ok(has(stampOf(env), 'is-ink'));
    assert.match(text(stampOf(env)), /^Claudexor not read now · all as read /);
    done(env);
    // The quota facet alone did not answer: the same, with its own words.
    const facet = JSON.parse(JSON.stringify(views.reading_part));
    facet.cached = facet.reserve.summary.cached = { quota: new Date(Date.now() - 600000).toISOString() };
    env = await boot(facet);
    assert.ok(footOf(env) === undefined);
    assert.equal(classes(env.root, 'tab-mark-note').length, 0);
    assert.match(text(stampOf(env)), /^Quota not read now · last known as read /);
    done(env);
  } else if (mode === 'compact') {
    // The compact rail: a window is said short on its row ("7D", "30D"; the full name stays in the tab's label and title), the share is drawn
    // inside the box of the ring and nothing stands at the right of the ring, the short name (the badge) and the star are inside that box too, the note of the accounts not read
    // is the first line of the card over the plate, the stamp is three short lines.
    const kinds = (node) => node.childNodes.map((n) => String(n.className || '').split(/\s+/)[0]);
    const env = await boot(views.codex);
    const tabs = tabsOf(env);
    assert.deepEqual(tabs.map(shortOf), ['7D', '30D'], 'the rail says a window short');
    assert.deepEqual(tabs.map(nameOf), ['Weekly', '30-day'], 'the full name stays in the label');
    for (const tab of tabs) {
      assert.ok(tab.title.indexOf(nameOf(tab)) === 0, 'and in the title');
      const box = classes(tab, 'tab-mark-ring')[0];
      assert.ok(box && classes(box, 'ring').length === 1 && classes(box, 'tab-mark-count').length === 1, 'the share is inside the box of the ring');
      assert.ok(tab.childNodes[0] === box, 'the box of the ring comes first');
      assert.ok(tab.childNodes.length === 1, 'the row holds the box of the ring alone: nothing stands at the right of the ring');
      const badgeName = classes(box, 'tab-mark-name');
      assert.ok(badgeName.length === 1 && badgeName[0].parentNode === box && badgeName[0].textContent === shortOf(tab), 'the short name stands in the box of the ring: the badge in its gap');
      const circles = ringParts(box, 'ring-badge');
      assert.ok(circles.length === 1 && circles[0].tagName.toLowerCase() === 'circle' && circles[0].parentNode === classes(box, 'ring')[0], 'the badge circle is drawn by the ring itself, once');
      assert.ok(circles[0].getAttribute('cx') === '92' && circles[0].getAttribute('cy') === '173' && circles[0].getAttribute('r') === '41' && circles[0].getAttribute('stroke-width') === '5',
        'on the lowest point of the path (92, 92 + 81), a stroke thinner than the track\'s 7');
      assert.ok(classes(box, 'ring')[0].childNodes[classes(box, 'ring')[0].childNodes.length - 1] === circles[0], 'drawn last, over the ends of the arc');
      assert.ok(/^(\d+%|—)$/.test(pctOf(tab)), 'the share, or a dash');
      assert.ok(classes(box, 'ring')[0].getAttribute('width') === '47', 'the large ring');
    }
    done(env);
    // A window not read whole wears the star after its word, and the note of the accounts not read stands in the plate, under its number row.
    const part = await boot(views.reading_part);
    const starred = pickedTab(part);
    const star = noteOf(starred);
    const starBox = classes(starred, 'tab-mark-ring')[0];
    assert.ok(star && star.parentNode === starBox && starBox.childNodes[starBox.childNodes.indexOf(star) - 1] === classes(starred, 'tab-mark-name')[0],
      'the star stands at the badge: in the box of the ring, after the short name');
    assert.ok(starred.childNodes.length === 1 && classes(starred, 'tab-mark-name')[0].parentNode === starBox, 'and the row holds that box alone');
    const card = classes(part.root, 'shell-card')[0];
    const section = classes(card, 'reserve')[0];
    const foot = classes(part.root, 'wt-foot')[0];
    const plate = plateOf(part);
    assert.ok(foot && foot.parentNode === plate && section.childNodes.indexOf(foot) === -1, 'the note is a part of the plate (not a line of the card over it)');
    assert.ok(plate.childNodes.indexOf(foot) > plate.childNodes.indexOf(classes(plate, 'bars')[0]) && plate.childNodes.indexOf(foot) > plate.childNodes.indexOf(classes(plate, 'l-chart')[0]),
      'under the bars and the number row');
    assert.ok(classes(classes(part.root, 'rail')[0], 'wt-foot').length === 0, 'and not in the rail');
    assert.ok(foot.getAttribute('role') === 'note' && starred.getAttribute('aria-describedby') === foot.getAttribute('id'), 'the star is tied to the note');
    // The stamp: the word, the count, the dot with the age.
    const lines = classes(stampOf(part), 'wt-read').concat(classes(stampOf(part), 'wt-n'), classes(stampOf(part), 'wt-age'));
    assert.deepEqual(lines.map(text), ['Read', '14 of 16', lines[2].textContent]);
    assert.ok(classes(stampOf(part), 'wt-sep').length === 1, 'the dot stands before the age');
    done(part);
  } else if (mode === 'rail') {
    // The windows stand in a rail at the card's side: nav, caption, the row of tabs with a ring, the note, the stamp at the foot; the
    // card keeps the plate. The ring of a tab says the share the tab says.
    const env = await boot(views.claude);
    const names = (node) => node.childNodes.map((n) => String(n.className || '').split(/\s+/)[0]);
    assert.deepEqual(names(env.root), ['control-bar', 'shell']);
    const frame = env.root.childNodes[1];
    assert.deepEqual(names(frame), ['rail', 'shell-card']);
    assert.ok(has(frame, 'has-rail'));
    const [rail, card] = frame.childNodes;
    assert.equal(rail.tagName, 'NAV');
    assert.equal(rail.getAttribute('aria-label'), 'Limit windows');
    const order = names(rail);
    assert.equal(order[0], 'rail-lead', 'the thread from the mark of the family comes first');
    assert.ok(rail.childNodes[0].getAttribute('aria-hidden') === 'true', 'out of the reading order');
    assert.ok(classes(rail, 'rail-cap').length === 0, 'no caption');
    assert.ok(!/average across accounts/.test(rail.textContent), 'the words of the old caption are gone');
    assert.equal(order[1], 'tab-mark-row');
    assert.ok(classes(rail, 'rail-bot').length === 0 && classes(rail, 'wt-stamp').length === 0, 'the rail has no foot and no stamp');
    const stamp = classes(classes(card, 'wt-plate')[0], 'wt-stamp')[0];
    assert.ok(stamp && has(stamp, 'reserve-age'), 'the stamp stands in the plate, in the row of the figure');
    assert.ok(classes(card, 'wt-plate')[0].childNodes.indexOf(stamp) > classes(card, 'wt-plate')[0].childNodes.indexOf(classes(card, 'l-fig')[0]), 'after the figure');
    assert.ok(classes(card, 'reserve-age').length === 1 && classes(card, 'reserve-age')[0] === stamp, 'the only stamp of the card is the one in the plate');
    assert.equal(classes(card, 'tab-mark').length + classes(card, 'wt-row').length + classes(card, 'wt-foot').length, 0,
      'the card holds neither the windows, nor the note');
    assert.equal(classes(card, 'wt-plate').length, 1);
    assert.equal(pickedTab(env).getAttribute('aria-controls'), plateOf(env).getAttribute('id'), 'the picked window controls the plate in the card');
    assert.equal(plateOf(env).getAttribute('aria-labelledby'), pickedTab(env).getAttribute('id'));
    assert.equal(tabsOf(env).length, 2, 'Claude reports the 5-hour and Weekly windows: two rows, no row for the 30-day it has not');
    assert.deepEqual(tabsOf(env).map(nameOf), ['5-hour', 'Weekly']);
    for (const tab of tabsOf(env)) {
      assert.ok(has(tab, 'tab-mark-rail'), 'a tab of the rail wears a ring');
      assert.deepEqual(names(tab), ['tab-mark-ring'], 'the row is the box of the ring alone');
      assert.ok(classes(classes(tab, 'tab-mark-ring')[0], 'tab-mark-name').length === 1, 'and the short name (the badge) stands in that box');
      const svg = classes(tab, 'ring')[0];
      assert.ok(svg && svg.getAttribute('aria-hidden') === 'true' && svg.getAttribute('focusable') === 'false' && svg.getAttribute('viewBox') === '0 0 184 184');
      const arcs = ringParts(tab, 'ring-arc');
      if (tab.getAttribute('aria-disabled') === 'true' || pctOf(tab) === '—') {
        assert.equal(arcs.length, 0, 'no share, no arc');
      } else {
        assert.equal(arcs.length, 1);
        assert.equal(arcs[0].getAttribute('stroke-dasharray'), parseFloat(pctOf(tab)) + ' 100', 'the arc is the tab\'s share');
      }
    }
    // A pick keeps the rail where it is and moves the plate.
    const other = tabsOf(env).find((tab) => tab.getAttribute('aria-disabled') !== 'true' && tab.getAttribute('aria-selected') !== 'true');
    click(other);
    await settle();
    assert.deepEqual(names(env.root.childNodes[1]), ['rail', 'shell-card']);
    assert.equal(pickedTab(env).getAttribute('data-focus'), other.getAttribute('data-focus'));
    done(env);
  } else if (mode === 'hold') {
    // Where another window wears the star and the picked one has none, the note's place stays: a hidden copy of the note the other window
    // would draw, the same words in the same width, so the plate does not jump as the reader moves along the row.
    const env = await boot(views.reading_two);
    const five = footOf(env);
    assert.ok(five && !has(five, 'wt-foot-hold'), 'the 5-hour is read in part: its own note');
    const fiveText = text(five);
    assert.ok(has(classes(env.root, 'rail')[0], 'wt-in') === false, 'the first drawing does not move');
    click(focusTab(env, 'claude', 604800));
    assert.equal(pickedTab(env).getAttribute('data-focus'), 'win:claude:604800');
    assert.equal(classes(env.root, 'wt-foot').length, 1);
    const hold = footOf(env);
    assert.ok(has(hold, 'wt-foot-hold'));
    assert.equal(hold.getAttribute('aria-hidden'), 'true');
    assert.equal(hold.getAttribute('id'), null);
    assert.equal(hold.getAttribute('role'), null);
    assert.equal(text(hold), fiveText, 'the same words as the note it stands for: the same height');
    assert.deepEqual(groupsOf(hold).map(whoOf), groupsOf(five).map(whoOf));
    assert.ok(walk(hold).every((n) => ['tabindex', 'data-focus', 'role', 'data-accounts', 'aria-label'].every((a) => n.getAttribute(a) === null)),
      'nothing to focus, nothing to name');
    assert.ok(walk(hold).every((n) => !Object.keys(n.listeners).length), 'and nothing that answers a pointer or a key');
    assert.equal(tabsOf(env).filter((t) => t.getAttribute('aria-describedby')).length, 0, 'no tab is described by a note that is not there');
    assert.ok(hold.parentNode === plateOf(env), 'the copy stands in the plate (the note\'s place is under the number row), hidden by the stylesheet');
    // Back on the 5-hour: its own note, and no copy.
    click(focusTab(env, 'claude', 18000));
    assert.equal(classes(env.root, 'wt-foot-hold').length, 0);
    assert.equal(text(footOf(env)), fiveText);
    assert.ok(has(plateOf(env), 'wt-in') && footOf(env).parentNode === plateOf(env), 'a note of the reader\'s own pick rises in, with the plate it stands in');
    done(env);
  } else if (mode === 'hold_zero') {
    // A window that read none of its accounts wears the dash and no star, yet it draws a note (who is not read). The place of that note is
    // kept, as the place of a starred window's is, when the reader moves to a window that has none: the plate does not jump along the row.
    const env = await boot(views.reading_zero);
    click(focusTab(env, 'claude', 18000));
    assert.equal(pctOf(pickedTab(env)), '—', 'the 5-hour read none: the dash');
    assert.equal(classes(env.root, 'tab-mark-note').length, 0, 'and no star anywhere in the row');
    const own = footOf(env);
    assert.ok(own && !has(own, 'wt-foot-hold'), 'its own note');
    const ownText = text(own);
    click(focusTab(env, 'claude', 604800));
    assert.equal(pickedTab(env).getAttribute('data-focus'), 'win:claude:604800');
    assert.equal(classes(env.root, 'tab-mark-note').length, 0, 'the weekly is read whole: no star');
    assert.equal(classes(env.root, 'wt-foot').length, 1);
    const hold = footOf(env);
    assert.ok(has(hold, 'wt-foot-hold'), 'the place of the 5-hour\'s note stays, unseen');
    assert.equal(hold.getAttribute('aria-hidden'), 'true');
    assert.equal(text(hold), ownText, 'the same words as the note it stands for: the same height');
    assert.equal(classes(hold, 'wt-foot-star').length, 0, 'and no star, as the real note has none');
    click(focusTab(env, 'claude', 18000));
    assert.ok(!has(footOf(env), 'wt-foot-hold'), 'back on the 5-hour: its own note');
    done(env);
  } else if (mode === 'stars') {
    // The star sits on the percent of EVERY window that is not read whole, picked or not; a window read whole carries none, and a window
    // the reader hid in Rows (the one grey tab) never. The stamp and the note are the picked window's own.
    let env = await boot(views.reading_both);
    const starred = () => tabsOf(env).filter((t) => classes(t, 'tab-mark-note').length).map(nameOf);
    assert.deepEqual(starred(), ['5-hour', 'Weekly']);
    assert.match(text(stampOf(env)), /^Read 3 of 4 · /);
    assert.deepEqual(groupsOf(footOf(env)).map(whoOf), [['a3']]);
    assert.equal(pickedTab(env).getAttribute('aria-describedby'), 'quotas-foot');
    const weekly = () => tabsOf(env).find((t) => nameOf(t) === 'Weekly');
    assert.equal(noteOf(weekly()).textContent, '*', 'a window not picked wears its star as a picked one does');
    assert.equal(noteOf(weekly()).getAttribute('aria-hidden'), 'true');
    assert.equal(weekly().getAttribute('aria-describedby'), null, 'only the picked tab is described by the note');
    assert.match(weekly().getAttribute('aria-label'), /avg left, 3 of 4 accounts read$/, 'the label says in words what the star says');
    click(focusTab(env, 'claude', 604800));
    assert.deepEqual(starred(), ['5-hour', 'Weekly'], 'both keep their star whichever is picked');
    assert.match(text(stampOf(env)), /^Read 3 of 4 · /);
    assert.deepEqual(groupsOf(footOf(env)).map(whoOf), [['a2']], 'the note is the picked window\'s own');
    assert.equal(classes(env.root, 'wt-foot-hold').length, 0, 'no copy of a note: the picked window has its own');
    assert.equal(tabsOf(env).filter((t) => t.getAttribute('aria-describedby')).length, 1);
    assert.equal(pickedTab(env).getAttribute('aria-describedby'), 'quotas-foot');
    assert.equal(tabsOf(env).filter((t) => t.getAttribute('aria-disabled') === 'true').every((t) => classes(t, 'tab-mark-note').length === 0), true);
    done(env);
    // Codex: the weekly and the 30-day windows of two accounts read whole carry none; Claude's whole windows carry none.
    for (const key of ['codex', 'reading_all']) {
      env = await boot(views[key]);
      assert.equal(classes(env.root, 'tab-mark-note').length, 0, key + ': read whole, no star');
      done(env);
    }
    // The billing-cycle budget is a window for this: its tab wears the star when it rests on a part of the accounts (cursor_mixed, below).
  } else if (mode === 'old') {
    // The oldest reading is told by its age; past the half hour the age is in the ink and semibold, as the number read short of all is.
    let env = await boot(views.reading_all);
    let stamp = stampOf(env);
    assert.equal(classes(stamp, 'wt-age')[0].className, 'wt-age', 'a fresh reading: the age is quiet');
    assert.equal(classes(stamp, 'wt-n')[0].className, 'wt-n');
    done(env);
    env = await boot(views.reading_old);
    stamp = stampOf(env);
    assert.match(text(stamp), /^Read 4 of 4 · (45|46) ?min ago$/, text(stamp));
    assert.equal(classes(stamp, 'wt-age')[0].className, 'wt-age is-old', 'old: the age in the ink');
    assert.equal(classes(stamp, 'wt-n')[0].className, 'wt-n', 'all read: the count is plain');
    assert.equal(classes(env.root, 'tab-mark-note').length, 0, 'old is not unread: no star');
    assert.ok(footOf(env) === undefined);
    assert.ok(!has(stamp, 'is-ink'), 'the stamp stays quiet: no colour of a state');
    done(env);
  } else if (mode === 'none') {
    // A window with no current reading at all: the figure is a dash, there is no star (the dash says "none"), the stamp counts none, and the
    // note names who is not read - all of them, last known - without a star.
    const env = await boot(views.reading_none);
    const tab = pickedTab(env);
    assert.equal(pctOf(tab), '—');
    assert.equal(classes(tab, 'tab-mark-note').length, 0, 'no star on a dash');
    assert.match(tab.getAttribute('aria-label'), /^5-hour — no current reading$/);
    const stamp = stampOf(env);
    assert.match(text(stamp), /^Read 0 of 4/, text(stamp));
    assert.equal(classes(stamp, 'wt-n')[0].className, 'wt-n lack');
    const foot = footOf(env);
    assert.ok(foot, 'the note names who is not read');
    assert.equal(classes(foot, 'wt-foot-star').length, 0, 'and has no star: the tab wears none');
    assert.match(text(foot), /^No fresh data · 4 read 4[56] ?min ago · /, text(foot));
    assert.deepEqual(groupsOf(foot).map(whoOf), [['c01', 'c02', 'c03']], 'the first three names');
    assert.equal(text(classes(foot, 'wt-foot-more')[0]), '1 more', 'and the rest counted');
    assert.equal(pickedTab(env).getAttribute('aria-describedby'), 'quotas-foot');
    done(env);
  } else if (mode === 'stamp_dot') {
    // The dot between "Read N of M" and its age blinks while a read the reader asked for (Refresh) is in the air, and not on a read nobody
    // asked for: the 30 s poll, a return to the page. (The Refresh word and its own dot still follow every read: that is the head's.)
    const dot = (env) => classes(stampOf(env), 'wt-sep')[0];
    const blinking = (env) => has(dot(env), 'is-busy');
    const refreshWord = (env) => byFocus(env.root, 'refresh');
    const hold = (env) => {
      const pending = [];
      env.window.fetch = (url, options = {}) => {
        env.calls.push({ url, method: options.method || 'GET' });
        return new Promise((resolve) => pending.push({ method: options.method || 'GET', resolve }));
      };
      return pending;
    };
    // 1. At rest the dot is still, and it is there (a reading with an age).
    let env = await boot(views.reading_part);
    assert.ok(dot(env), 'the stamp has its dot');
    assert.ok(!blinking(env), 'at rest the dot is still');
    // 2. The 30 s poll: a read is in the air (Refresh says so: disabled, aria-busy), the stamp's dot does not blink; it is still there
    // and still when the read lands.
    let pending = hold(env);
    env.interval()();
    await settle();
    assert.equal(pending.length, 1, 'the poll started one read');
    assert.equal(pending[0].method, 'GET', 'and it is no Refresh');
    assert.equal(refreshWord(env).disabled, true, 'a read is in the air');
    assert.ok(dot(env) && !blinking(env), 'a read nobody asked for does not blink the stamp\'s dot');
    pending[0].resolve(response(views.reading_part));
    await settle();
    assert.equal(refreshWord(env).disabled, false);
    assert.ok(!blinking(env), 'and the dot is still after it');
    // 3. A second poll, the same.
    pending = hold(env);
    env.interval()();
    await settle();
    assert.ok(!blinking(env), 'the next poll does not either');
    pending[0].resolve(response(views.reading_part));
    await settle();
    // 4. The reader's own Refresh: the dot blinks from the click, through the POST and the read after it, and stops when that read lands.
    pending = hold(env);
    refreshWord(env).listeners.click[0]();
    await settle();
    assert.equal(pending.length, 1);
    assert.equal(pending[0].method, 'POST', 'Refresh asks the host');
    assert.ok(blinking(env), 'the reader asked: the dot blinks');
    assert.equal(refreshWord(env).disabled, true);
    pending[0].resolve(response({ ok: true, quota_updates: [] }));
    await settle();
    assert.equal(pending.length, 2, 'then the read of the whole projection');
    assert.equal(pending[1].method, 'GET');
    assert.ok(blinking(env), 'still blinking while that read is in the air');
    pending[1].resolve(response(views.reading_part));
    await settle();
    assert.ok(dot(env) && !blinking(env), 'it stops when the read after Refresh has landed');
    assert.equal(refreshWord(env).disabled, false);
    // 5. A Refresh that fails ends the blink as well (the POST answers "not ok"), and the poll after it still does not blink.
    pending = hold(env);
    refreshWord(env).listeners.click[0]();
    await settle();
    assert.ok(blinking(env));
    pending[0].resolve(response({ ok: false }, 500));
    await settle();
    assert.ok(!blinking(env), 'a failed Refresh stops the blink');
    pending = hold(env);
    env.interval()();
    await settle();
    assert.ok(!blinking(env), 'and a poll after it does not start it');
    pending[0].resolve(response(views.reading_part));
    await settle();
    done(env);
  } else if (mode === 'cursor_budget') {
    // Cursor reports no time window, but money per billing cycle: the 30-day place opens on it, and says what it is.
    const NOTE = 'Cursor: included usage per billing cycle (not a time-window limit)';
    const env = await boot(views.cursor_budget);
    const tabs = tabsOf(env);
    // Cursor has one row, the 30-day place that opens on the budget: the 5-hour and Weekly places are windows it does not report, so no row.
    assert.deepEqual(tabs.map(nameOf), ['30-day']);
    assert.ok(tabs.filter((t) => t.getAttribute('aria-disabled') === 'true').length === 0, 'no grey place');
    const place = pickedTab(env);
    assert.equal(nameOf(place), '30-day');
    assert.equal(place.getAttribute('aria-disabled'), null);
    assert.equal(tabs.filter((t) => t.getAttribute('tabindex') === '0').length, 1, 'one tab stop');
    assert.equal(pctOf(place), '41%', 'the mean share left: (59% + 23%) / 2');
    assert.equal(classes(place, 'tab-mark-note').length, 0, 'both accounts are read: no star');
    assert.ok(footOf(env) === undefined, 'and no note');
    assert.equal(place.title, NOTE, 'it is a cycle\'s allowance, not a window of time: the hint says so');
    assert.equal(place.getAttribute('aria-label'), '30-day — 41% avg left, 2 of 2 accounts read — ' + NOTE,
      'the label counts the accounts read, as the label of every other window does');
    assert.equal(place.getAttribute('data-focus'), 'win:cursor:2592000');
    // The plate is a window's plate, in dollars.
    const plate = plateOf(env);
    assert.equal(plate.getAttribute('role'), 'tabpanel');
    assert.equal(plate.getAttribute('aria-labelledby'), 'quotas-win-2592000');
    assert.equal(text(classes(plate, 'l-fig')[0]), '$16.40 left of $40.00', 'the sum of what the two accounts have left, of what they are allowed');
    // The fact stands in the open, captioned with the budget's own word alone: "Period" (a cycle is no window of time).
    const fact = classes(plate, 'wt-fact')[0];
    assert.equal(text(classes(fact, 'wt-fact-cap')[0]), 'Period');
    assert.equal(fact.childNodes.indexOf(classes(fact, 'l-fig')[0]), 1, 'the caption, then the figure');
    // The decimal point of the sum is the crimson ink, a span of its own, and the text of the figure is the sum whole.
    assert.deepEqual(classes(fact, 'wt-dot').map(text), ['.']);
    assert.equal(text(classes(fact, 'wt-num')[0]), '$16.40');
    assert.equal(classes(plate, 'l-sub').length, 0, 'the average is the tab\'s alone: no line of it on the plate');
    assert.ok(!/avg left|%/.test(text(plate)), text(plate));
    const bars = stripBars(plate);
    assert.deepEqual(bars.map((b) => classes(b, 'fill')[0].style.height), ['59%', '23%'], 'a bar per account, as tall as its share left, fullest first');
    assert.match(barSaid(bars[0]), /^cursor1: 59% left — \$11\.80 of \$20\.00 · cycle ends \w{3} \d+ \w{3} \d\d:\d\d · read /);
    assert.match(barSaid(bars[1]), /^cursor2: 23% left — \$4\.60 of \$20\.00 · cycle ends /);
    assert.ok(bars.every((b) => !has(b, 'spent') && !has(b, 'held') && !has(b, 'last') && !has(b, 'unknown')));
    // Under the bars: every account is counted, so there is no state to tell (no legend); the cycle and its
    // reset stand in a row of cells, the time and the money bold, "billing cycle" the quiet words alone.
    assert.equal(classes(plate, 'lg').length, 0, 'no legend while every account is counted');
    assert.equal(classes(plate, 'l-tail').length, 0);
    const cells = cellsOf(plate);
    assert.equal(cells.length, 2, cells.join('|'));
    assert.equal(cells[0], 'billing cycle');
    assert.match(cells[1], /^next reset \w{3} \d+ \w{3} \d\d:\d\d$/, cells[1]);
    assert.ok(!cells.some((c) => /^\d+ accounts$/.test(c)), 'the two cycles end on different days');
    assert.ok(!cells.some((c) => /on-demand/.test(c)), 'the money beyond the plan is not a cell of the reset row any more');
    assert.deepEqual(bolds(nxOf(plate)).map(text), [cells[1].replace('next reset ', '')]);
    assert.ok(bolds(nxOf(plate)).every((b) => !b.className), 'and nothing of the row is red');
    const parts = nxOf(plate).childNodes.filter((n) => n.tagName === 'SPAN');
    assert.equal(parts.length, 2);
    assert.match(parts[0].title, /^Included usage renews when an account’s own billing cycle ends/);
    assert.match(parts[1].title, /^The soonest billing-cycle end among the accounts read now/);
    // What is spent beyond the plan is the plate's bubble of the pale crimson ("Over plan · $21.65"), in the place of the accounts at the
    // limit: it is no handle (no keyboard stop, no lit bars), and it keeps the hint of the sum.
    const over = classes(plate, 'wt-limit');
    assert.equal(over.length, 1);
    assert.equal(text(over[0]), 'Over plan · $21.65');
    assert.match(over[0].title, /^Personal on-demand spending this cycle, added over 2 accounts/);
    assert.ok(over[0].getAttribute('tabindex') === null && over[0].getAttribute('data-focus') === null && !over[0].listeners.mouseenter,
      'a sum is not a group of accounts: nothing to focus, nothing to light');
    // No history of money: no chart control, no graph - and the plate says so, once, small.
    assert.equal(classes(env.root, 'l-chart').length, 0);
    assert.equal(classes(classes(env.root, 'tl-block')[0], 'wt-tab').length, 0, 'no graph tabs');
    assert.ok(!jointSwitch(env) && !byFocus(env.root, 'tl-model'), 'and no graph controls: there is no graph');
    assert.equal(text(classes(env.root, 'tl-block')[0]), 'No timeline for spend');
    assert.ok(!/No quota limit reported/.test(env.root.textContent), 'with a budget, the original text is gone');
    // The section's status line is about the reading, as for any family.
    assert.match(text(stampOf(env)), /^Read 2 of 2 · /);
    // The list: the budget is one more column; the standing "no source" is not drawn as a fault.
    assert.deepEqual(classes(classes(env.root, 'acc-headrow')[0], 'acc-cell').map(text), ['30-day']);
    const rows = classes(env.root, 'acc-row');
    assert.equal(rows.length, 2);
    assert.deepEqual(rows.map((r) => classes(r, 'acc-cell').map(text)[0]), ['59%', '23%']);
    for (const r of rows) {
      const state = classes(r, 'acc-state')[0];
      assert.equal(text(state), 'budget only');
      assert.ok(has(state, 'muted') && !has(state, 'warn'), 'no tone is set from the money');
      assert.match(r.getAttribute('aria-label'), /No time window is reported for this account/);
    }
    assert.ok(!/quota unavailable|No live quota source|temporarily unavailable/i.test(env.root.textContent));
    // The tab is no limit to pick: a click changes nothing and starts no motion.
    click(place);
    assert.ok(plateOf(env) === plate, 'not redrawn');
    assert.ok(!has(plateOf(env), 'wt-in'));
    // A bar selects its account, and the card stands right under the plate, before the note.
    click(bars[1]);
    const card = classes(env.root, 'inspector')[0];
    assert.ok(card, 'the account\'s card');
    const box = classes(env.root, 'lrows')[0].childNodes;
    assert.ok(box.indexOf(plateOf(env)) < box.indexOf(card) && box.indexOf(card) < box.indexOf(classes(env.root, 'tl-block')[0]),
      'plate, then the card, then the note');
    // A card with no chart over it is a strip (the share, the money, the reset) and its resources wait under one button, shut at the start.
    assert.match(text(card), /\$4\.60 left of \$20\.00/);
    assert.ok(!/Personal included remaining/.test(text(card)), 'the resources are not drawn while the fold is shut');
    click(classes(classes(card, 'insp-diag')[0], 'pill-btn')[0]);
    const open = classes(env.root, 'inspector')[0];
    assert.match(text(open), /Personal included remaining: 4\.60 USD/);
    assert.match(text(open), /Personal included usage: 15\.40 USD of 20\.00 USD/);
    assert.ok(!/Quota temporarily unavailable|No live quota source/.test(text(open)), 'the card does not call it unavailable either');
    assert.equal(classes(env.root, 'bar').filter((b) => has(b, 'sel')).length, 1);
    done(env);
  } else if (mode === 'cursor_mixed') {
    // Every kind of reading beside a plain one. Counted: only the accounts read now with a cap.
    const env = await boot(views.cursor_mixed);
    const place = pickedTab(env);
    assert.equal(nameOf(place), '30-day');
    assert.equal(pctOf(place), '15%', 'the mean of 30% and 0% - the unlimited, the last known and the unread are not in it');
    // That mean rests on two of the five accounts: the star on its percent, the stamp, and the note under the row, as for a window.
    assert.equal(noteOf(place).textContent, '*');
    assert.match(text(stampOf(env)), /^Read 3 of 5 · /, 'the unlimited account is read, though it has no share');
    // The tab says to a reader who does not see it what the stamp says to one who does: 3 of 5 read.
    assert.equal(place.getAttribute('aria-label'),
      '30-day — 15% avg left, 3 of 5 accounts read — Cursor: included usage per billing cycle (not a time-window limit)');
    assert.equal(place.getAttribute('aria-describedby'), 'quotas-foot');
    assert.deepEqual(groupsOf(footOf(env)).map(whoOf), [['a3'], ['a4']], 'last known first, then the one not read');
    assert.deepEqual(groupsOf(footOf(env)).map((g) => g.getAttribute('data-k')), ['last', 'unknown']);
    const plate = plateOf(env);
    assert.equal(text(classes(plate, 'l-fig')[0]), '$6.00 left of $40.00');
    assert.equal(text(classes(plate, 'wt-fact-cap')[0]), 'Period');
    assert.equal(classes(plate, 'l-sub').length, 0, 'the average is the tab\'s alone');
    const bars = stripBars(plate);
    assert.equal(bars.length, 4, 'an unlimited account has no share, so no bar');
    assert.deepEqual(bars.map((b) => ['last', 'unknown'].find((c) => has(b, c)) || 'current'), ['current', 'current', 'last', 'unknown']);
    assert.ok(bars.every((b) => !has(b, 'spent')), 'used up is a real zero, not "at the limit"');
    assert.equal(classes(bars[1], 'fill').length, 0, '0% left: an empty bar');
    assert.match(barSaid(bars[1]), /^a5: 0% left — \$0\.00 of \$20\.00 /);
    assert.match(barSaid(bars[2]), /^a3: 50% left when last read — \$10\.00 of \$20\.00 · cycle ends .* · last known, read .*, not current$/);
    assert.match(barSaid(bars[3]), /^a4 .*: unknown — no usable reading — unknown, not counted, never a zero$/);
    // The accounts not counted are told in a legend, each kind with its number; the names are in the hints. An account with no usable
    // reading has no item (its bar is a "?" and the note under the windows names it); an item is its number and its words, no small bar.
    const items = itemsOf(plate);
    // (The budget's legend is whole: the last known one is told by the note of the accounts not read too, with its name and age, but only the legend says
    // "not counted", which is money's word. The limit's legend, with the note beside it, drops that item alone.)
    assert.deepEqual(items.map(text), ['1 unlimited', '1 last known — not counted']);
    assert.deepEqual(items.map((i) => i.getAttribute('data-k')), ['unlimited', 'last']);
    assert.match(text(footOf(env)), /^\* No fresh data · 2 read just now · a3 · /, text(footOf(env)));
    assert.equal(classes(lgOf(plate), 'bar').length, 0, 'no small bar in an item: the bars above are its sample');
    assert.ok(items.every((i) => !figureOf(i).className), 'no tone is set from the money');
    assert.equal(items[0].title, 'a2 — unlimited included usage\nUnlimited included usage has no share: it is not in the figure, the average or the bars.');
    for (const name of ['a1', 'a2', 'a3', 'a4', 'a5']) assert.ok(!text(lgOf(plate)).includes(name), name + ' stands on the screen');
    const cells = cellsOf(plate);
    assert.equal(cells.length, 3, cells.join('|'));
    assert.equal(cells[0], 'billing cycle');
    assert.match(cells[1], /^next reset /);
    assert.equal(cells[2], '2 accounts', 'a1 and a5 end their cycles together');
    // The money spent beyond the allowance is the bubble "Over plan" (the pale crimson), not a red figure of the reset row: nothing there is red.
    assert.deepEqual(bolds(nxOf(plate)).map((b) => b.className || ''), ['', '']);
    assert.deepEqual(classes(plate, 'wt-limit').map(text), ['Over plan · $5.00']);
    // The list says each in its own words.
    const rows = Object.fromEntries(classes(env.root, 'acc-row').map((r) => [text(classes(r, 'acc-name-text')[0]), r]));
    const cell = (k) => text(classes(rows[k], 'acc-cell')[0]);
    const state = (k) => text(classes(rows[k], 'acc-state')[0]);
    assert.deepEqual(['a1', 'a2', 'a3', 'a4', 'a5'].map(cell), ['30%', 'unlimited', '50%', '?', '0%']);
    assert.ok(has(classes(rows.a3, 'acc-cell')[0], 'last'), 'a last-known share is dated, not current');
    assert.deepEqual(['a1', 'a2', 'a5'].map(state), ['budget only', 'budget only', 'budget only']);
    // A stale dashboard, or none read at all, is a fault: the standing "no source" is then said as before.
    assert.deepEqual(['a3', 'a4'].map(state), ['quota unavailable · No live quota source', 'quota unavailable · No live quota source']);
    assert.ok(['a3', 'a4'].every((k) => has(classes(rows[k], 'acc-state')[0], 'warn')));
    // The unlimited account's card says so and does not set a share.
    click(rows.a2);
    assert.match(text(classes(env.root, 'inspector')[0]), /unlimited included usage/, 'the strip says it');
    click(classes(classes(classes(env.root, 'inspector')[0], 'insp-diag')[0], 'pill-btn')[0]);
    assert.match(text(classes(env.root, 'inspector')[0]), /Personal included remaining: unlimited/);
    done(env);
  } else if (mode === 'cursor_kept') {
    // A screen the widget keeps because the last read failed is nothing current: the budget is last known, dated, not counted.
    const first = await boot(views.cursor_budget);
    const kept = first.testHooks.keptView(JSON.parse(JSON.stringify(views.cursor_budget)));
    done(first);
    assert.equal(kept.kept, true);
    const env = await boot(kept);
    const place = pickedTab(env);
    assert.equal(nameOf(place), '30-day');
    assert.equal(pctOf(place), '—', 'no current reading is a dash, never 0');
    assert.equal(classes(env.root, 'tab-mark-note').length, 0, 'a failed read: no star');
    assert.ok(footOf(env) === undefined, 'and no note - every account is not read, the stamp says so');
    assert.equal(classes(env.root, 'wt-foot-hold').length, 0);
    assert.ok(has(stampOf(env), 'is-ink'));
    const plate = plateOf(env);
    assert.equal(text(classes(plate, 'l-fig')[0]), '— no current reading');
    assert.equal(classes(plate, 'l-sub').length, 0, 'the plate has no line of its own under the bars');
    const bars = stripBars(plate);
    assert.equal(bars.length, 2);
    assert.ok(bars.every((b) => has(b, 'last')), 'hatched: last known');
    assert.match(barSaid(bars[0]), /^cursor1: 59% left when last read — \$11\.80 of \$20\.00 · cycle ends .* · last known, read .*, not current$/);
    assert.deepEqual(itemsOf(plate).map(text), ['2 last known — not counted']);
    assert.deepEqual(cellsOf(plate), ['billing cycle'], 'no next reset, no spend: nothing current');
    assert.ok(classes(env.root, 'acc-state').every((n) => !/budget only/.test(text(n))), 'not healthy now: the standing no_source is not hidden');
    done(env);
  } else if (mode === 'legend_codex') {
    // Codex Weekly, accounts in three states. Under the bars: the legend, an item per state in one order, then the reset.
    const env = await boot(views.codex_states);
    const plate = plateOf(env);
    assert.equal(classes(env.root, 'l-tail').length, 0, 'the one-line tail is gone');
    const items = itemsOf(plate);
    // The accounts at the limit now are not an item of the legend (it would say them twice): they are the plate's bubble (limitBubble, below).
    // The legend tells the rest: held back. The one at the limit when last read is dated, so the note of the accounts not read (it stands in the
    // plate) names it with its age, and the legend does not say "last known" again. "At the limit when last read" stays: it is another meaning, and it is
    // what the dashed base of that bar says.
    assert.deepEqual(items.map(text), ['1 disabled', '1 at the limit when last read']);
    assert.deepEqual(items.map((i) => i.getAttribute('data-k')), ['held', 'was']);
    assert.ok(/ read 2\d ?min ago · gptpro7\b/.test(text(footOf(env))), 'the note names the dated one and its age: ' + text(footOf(env)));
    // No red digit in the legend: held back is plain, and the words carry no colour: a state is told by its words and by the
    // form of its bar above, not by a second or third colour.
    assert.deepEqual(items.map((i) => figureOf(i).className), ['', '']);
    assert.deepEqual(items.map((i) => text(figureOf(i))), ['1', '1']);
    // An item is its number and its words and nothing else: the bars above are its sample (no small bar of the kind in it).
    assert.equal(classes(lgOf(plate), 'bar').length, 0);
    assert.equal(bolds(lgOf(plate)).filter((b) => b.className === 'acc').length, 0, 'no red digit');
    // The bubble: "At the limit · 3", the three by name, and when the soonest of them is back; it is the plate's own child, a handle (the
    // keyboard stops on it, it names its accounts for the plate to light) and its hint is the old item's: a line for each, with its time.
    const bubbles = classes(plate, 'wt-limit');
    assert.equal(bubbles.length, 1);
    const bubble = bubbles[0];
    assert.ok(bubble.parentNode === plate, 'a part of the plate');
    assert.equal(text(classes(bubble, 'wt-limit-head')[0]), 'At the limit · 3');
    assert.match(text(bubble), /^At the limit · 3 gptpro1, gptpro2, gptpro3 · back in (?:<1m|\d+m|\d+h|\d+d(?: \d+h)?)$/, text(bubble));
    assert.deepEqual(whoOf(bubble).sort(), ['gptpro1', 'gptpro2', 'gptpro3']);
    assert.equal(classes(bubble, 'wt-foot-more').length, 0, 'three names: no "more"');
    assert.equal(bubble.getAttribute('tabindex'), '0');
    assert.equal(bubble.getAttribute('role'), 'group');
    assert.equal(bubble.getAttribute('data-k'), 'limit');
    assert.equal(bubble.getAttribute('data-focus'), 'atlimit:' + classes(plate, 'l-chart')[0].getAttribute('data-focus').slice('limit:'.length));
    assert.deepEqual(JSON.parse(bubble.getAttribute('data-accounts')).sort(), ['codex:gptpro1', 'codex:gptpro2', 'codex:gptpro3']);
    assert.match(bubble.getAttribute('aria-label'), /^At the limit · 3, gptpro1, gptpro2, gptpro3, back in (?:<1m|\d+m|\d+h|\d+d(?: \d+h)?)$/);
    assert.ok(['mouseenter', 'mouseleave', 'focus', 'blur', 'pointerdown', 'pointerup', 'pointercancel']
      .every((e) => bubble.listeners[e] && bubble.listeners[e].length === 1), 'hover, focus and touch are one thing');
    assert.deepEqual(bolds(bubble).map(text), ['At the limit · 3'], 'the head alone is bold');
    // Its place in the plate: after the bars, the fact and the chart word, before the note of the accounts not read (the grid puts it under the number row).
    const order = plate.childNodes;
    assert.ok(order.indexOf(classes(plate, 'bars')[0]) < order.indexOf(classes(plate, 'wt-fact')[0]) && order.indexOf(classes(plate, 'wt-fact')[0]) < order.indexOf(bubble));
    assert.ok(order.indexOf(classes(plate, 'l-chart')[0]) < order.indexOf(bubble) && order.indexOf(bubble) < order.indexOf(footOf(env)));
    const strip = stripBars(plate);
    assert.equal(strip.filter((b) => has(b, 'spent')).length, 3);
    assert.equal(strip.filter((b) => has(b, 'held')).length, 1);
    // The new mark: a last-known bar at the limit, told from an empty bar and from a red one.
    const was = strip.filter((b) => has(b, 'was'));
    assert.equal(was.length, 1);
    assert.ok(has(was[0], 'last') && !has(was[0], 'spent') && was[0].getAttribute('data-state') === 'last_known');
    assert.equal(strip.filter((b) => has(b, 'was') && !has(b, 'last')).length, 0);
    // Items take the keyboard, in order; the strip is the plate's own, one bar per account.
    assert.ok(items.every((i) => i.getAttribute('tabindex') === '0' && i.tagName === 'LI'));
    assert.equal(lgOf(plate).tagName, 'UL');
    assert.ok(has(lgOf(plate), 'calm-legend'), 'the kit\'s own legend');
    assert.equal(strip.length, 7);
    // The names are in the hints, a line each - and nowhere on the screen beside the numbers.
    // (the accounts at the limit now are named in the bubble, up to three: the legend and the reset row name none)
    const shown = text(lgOf(plate)) + ' | ' + text(nxOf(plate));
    for (const name of ['gptpro1', 'gptpro2', 'gptpro3', 'gptpro7', 'gptpro9']) assert.ok(!shown.includes(name), name + ' on screen: ' + shown);
    const lines = bubble.title.split('\n');
    assert.equal(lines.length, 3);
    assert.ok(lines.every((l) => /^gptpro[123] — at the limit until \w{3} \d+ \w{3} \d\d:\d\d$/.test(l)), bubble.title);
    assert.deepEqual(lines.map((l) => l.slice(0, 7)).sort(), ['gptpro1', 'gptpro2', 'gptpro3']);
    assert.equal(items[0].title, 'gptpro9 — disabled');
    // The reset: three cells, the figure of each bold, the hint of the whole row the old one.
    const cells = cellsOf(plate);
    assert.equal(cells.length, 3, cells.join('|'));
    assert.match(cells[0], /^next reset \w{3} \d+ \w{3} \d\d:\d\d$/);
    assert.equal(cells[1], '3 accounts');
    assert.equal(cells[2], '+3.00 if unused');
    assert.deepEqual(bolds(nxOf(plate)).map(text), [cells[0].replace('next reset ', ''), '3', '+3.00']);
    assert.equal(nxOf(plate).title, 'The soonest reported reset of a current account: a full refill then gives back the 3.00 account-windows they use now, if nothing more is used before it.');
    // Their place: after the bars, the legend, then the reset (no caption between them).
    const kids = plate.childNodes;
    assert.equal(classes(plate, 'l-sub').length, 0);
    assert.ok(kids.indexOf(classes(plate, 'bars')[0]) < kids.indexOf(lgOf(plate)) && kids.indexOf(lgOf(plate)) < kids.indexOf(nxOf(plate)));
    // Nothing the tail said is lost: the whole spoken summary (the note the word Chart is described by) keeps the full phrases, names and times.
    const label = spokenOf(env);
    assert.match(label, / — 3 at the limit — gptpro9 disabled — gptpro7 at the limit when last read \(27m ago\) — next reset \w{3} \d+ \w{3} \d\d:\d\d · 3 accounts · \+3\.00 if unused/);
    // The keyboard stays on an item through the redraw of fresh data.
    items[0].focus();
    const key = items[0].getAttribute('data-focus');
    assert.match(key, /^legend:/);
    env.interval()(); await settle();
    assert.equal(env.document.activeElement.getAttribute('data-focus'), key);
    assert.ok(has(env.document.activeElement, 'legend-item'));
    done(env);
  } else if (mode === 'legend_claude') {
    // Claude 5-hour: three at the limit, each to reset at its own time - one legend item, two cells of reset.
    const env = await boot(views.claude_five);
    const plate = plateOf(env);
    // Three at the limit and nothing else to tell: the legend is gone (the bubble says who is at the limit), and the bubble says it once.
    assert.deepEqual(itemsOf(plate).map(text), []);
    assert.ok(lgOf(plate) === undefined, 'no legend');
    const bubble = classes(plate, 'wt-limit')[0];
    assert.match(text(bubble), /^At the limit · 3 claude1, claude2, claude3 · back in (?:<1m|\d+m|\d+h|\d+d(?: \d+h)?)$/, text(bubble));
    assert.equal(bubble.title.split('\n').length, 3);
    const cells = cellsOf(plate);
    assert.equal(cells.length, 2, cells.join('|'));
    assert.match(cells[0], /^next reset \w{3} \d+ \w{3} \d\d:\d\d$/);
    assert.equal(cells[1], '+1.00 if unused', 'the one account that resets first gives back what it uses');
    assert.ok(!cells.some((c) => /accounts$/.test(c)), 'one account resets first: no count of accounts');
    assert.equal(nxOf(plate).title, 'The soonest reported reset of a current account: a full refill then gives back the 1.00 account-windows it uses now, if nothing more is used before it.');
    done(env);
  } else if (mode === 'legend_single') {
    // One account at the limit: the number is 1 and the name is in the hint, with its time.
    const env = await boot(views.claude_single);
    const plate = plateOf(env);
    assert.deepEqual(itemsOf(plate).map(text), []);
    const bubble = classes(plate, 'wt-limit')[0];
    assert.match(bubble.title, /^claude3 — at the limit until \w{3} \d+ \w{3} \d\d:\d\d$/);
    assert.match(text(bubble), /^At the limit · 1 claude3 · back in (?:<1m|\d+m|\d+h|\d+d(?: \d+h)?)$/, text(bubble));
    assert.ok(seen(plate).includes('claude3'), 'the name is on the screen: in the bubble');
    assert.match(spokenOf(env), / — claude3 at the limit until \w{3} \d+ \w{3} \d\d:\d\d — /,
      'the spoken summary keeps the old phrase, name and time');
    done(env);
  } else if (mode === 'legend_many') {
    // A hint names eight accounts and counts the rest.
    const env = await boot(views.claude_many);
    const plate = plateOf(env);
    assert.deepEqual(itemsOf(plate).map(text), []);
    const bubble = classes(plate, 'wt-limit')[0];
    const lines = bubble.title.split('\n');
    assert.equal(lines.length, 9);
    assert.ok(lines.slice(0, 8).every((l) => /^c\d\d — at the limit until /.test(l)), bubble.title);
    assert.equal(lines[8], '+2 more');
    // The bubble names the first three and counts the rest ("7 more", which opens the account list, as the note's does); the soonest
    // reset of the ten is the tail.
    assert.equal(classes(bubble, 'wt-who').length, 3);
    assert.equal(text(classes(bubble, 'wt-limit-head')[0]), 'At the limit · 10');
    assert.match(text(bubble), /^At the limit · 10 c\d\d, c\d\d, c\d\d 7 more · back in (?:<1m|\d+m|\d+h|\d+d(?: \d+h)?)$/, text(bubble));
    assert.equal(JSON.parse(bubble.getAttribute('data-accounts')).length, 10, 'it stands for all ten, though it names three');
    const more = classes(bubble, 'wt-foot-more');
    assert.equal(more.length, 1);
    assert.equal(more[0].tagName, 'BUTTON');
    assert.equal(text(more[0]), '7 more');
    assert.equal(more[0].getAttribute('aria-label'), '7 more accounts — open the account list');
    assert.match(more[0].getAttribute('data-focus'), /^atlimit-more:/);
    // The word over the bars ("Accounts · N", data-focus=accounts) carries aria-pressed now; the bottom toggle with aria-expanded is gone.
    assert.equal(byFocus(env.root, 'accounts').getAttribute('aria-pressed'), 'false');
    click(more[0]);
    await settle();
    assert.equal(byFocus(env.root, 'accounts').getAttribute('aria-pressed'), 'true', '"7 more" opens the account list');
    done(env);
  } else if (mode === 'legend_reasons') {
    // Held back for one or two named reasons: an item for each, in a fixed order; three, or an unnamed one: "restricted".
    let env = await boot(views.reasons_two);
    let plate = plateOf(env);
    let items = itemsOf(plate);
    assert.deepEqual(items.map(text), ['2 disabled', '1 check failed']);
    assert.ok(items.every((i) => i.getAttribute('data-k') === 'held' && !figureOf(i).className), 'held back is told by its words and its bar, in no colour of its own');
    assert.equal(new Set(items.map((i) => i.getAttribute('data-focus'))).size, 2, 'one focus key for each item');
    assert.equal(items[0].title, 'off1 — disabled\noff2 — disabled');
    assert.equal(items[1].title, 'bad1 — check failed');
    assert.ok(items.every((i) => classes(i, 'bar').length === 0), 'no small bar in an item');
    assert.equal(stripBars(plate).filter((b) => has(b, 'held')).length, 3, 'the three held-back bars stand in the strip');
    assert.match(spokenOf(env), / — 3 restricted — /, 'the spoken summary keeps its old phrase');
    done(env);
    env = await boot(views.reasons_three);
    plate = plateOf(env);
    items = itemsOf(plate);
    assert.deepEqual(items.map(text), ['3 restricted']);
    assert.equal(items[0].title, 'off1 — disabled\nout1 — signed out, check failed\nbad1 — check failed', 'fullest first, as the bars');
    done(env);
  } else if (mode === 'chart_word') {
    // The plate's one control is the kit's text action, the word "Chart": no pill, no frame, no icon, no label or hint of its own (the
    // word is its name). It stands after the bars, the fact, the scope row (where there is one) and the stamp, and before the notes under the plate
    // (the DOM is the order of the keyboard), is pressed while the chart is shown, folds and unfolds it, and keeps the keyboard.
    const env = await boot(views.claude);
    try {
      let plate = plateOf(env);
      const word = classes(plate, 'l-chart')[0];
      assert.equal(classes(plate, 'l-chart').length, 1, 'one control on the plate');
      assert.equal(word.tagName, 'BUTTON');
      assert.equal(word.getAttribute('type'), 'button');
      assert.ok(has(word, 'btn-word'), 'the kit\'s text action');
      assert.ok(!['pill-btn', 'btn-compact', 'on', 'chart-toggle'].some((c) => has(word, c)), 'no pill, no plate, no state class: ' + word.className);
      assert.equal(text(word), 'Chart');
      assert.equal(word.getAttribute('aria-label'), null, 'the word is its own name');
      assert.equal(word.getAttribute('title'), null);
      assert.equal(walk(word).filter((n) => n.tagName === 'SVG').length, 0, 'no icon');
      // The plate's own parts in the order of the keyboard: the bars, the chart (the top account is picked as the window opens, so the chart stands
      // in the plate, right under the bars: .tl-wrap), the card of the picked account (.ins-wrap: it hangs under the chart, in the plate), the fact, the scope row (the filter of the plate, there when the length has several limits), the
      // stamp, then the word; the notes under the plate (the accounts at the limit, not read, the credits, the legend, the reset row) come after it.
      // (Nodes are compared with ok, not equal: a miss of equal would print both whole trees.)
      const kids = plate.childNodes;
      const before = (list, at) => list.slice(0, list.indexOf(at)).map((n) => ['bars', 'tl-wrap', 'ins-wrap', 'wt-fact', 'wt-scope', 'wt-stamp'].find((c) => has(n, c)));
      assert.deepEqual(before(kids, word), ['bars', 'tl-wrap', 'ins-wrap', 'wt-fact', 'wt-scope', 'wt-stamp'], 'the parts before the word: ' + kids.map((n) => n.className).join(' | '));
      assert.ok(kids.slice(kids.indexOf(word) + 1).every((n) => ['sr-only', 'wt-limit', 'wt-foot', 'family-credits', 'lg', 'nx'].some((c) => has(n, c))), 'and the notes after it: ' + kids.map((n) => n.className).join(' | '));
      assert.ok(kids.some((n) => has(n, 'wt-scope')), 'this plate has a scope row (Weekly of Claude has three limits)');
      const bare = await boot(views.codex);
      assert.deepEqual(before(plateOf(bare).childNodes, classes(plateOf(bare), 'l-chart')[0]), ['bars', 'tl-wrap', 'ins-wrap', 'wt-fact', 'wt-stamp'],
        'with no scope row the word follows the bars, the chart, the card, the fact and the stamp');
      done(bare);
      assert.equal(word.getAttribute('aria-describedby'), 'quotas-win-say');
      const say = walk(plate).find((n) => n.getAttribute('id') === 'quotas-win-say');
      assert.ok(say && has(say, 'sr-only') && /^Weekly|^5-hour|^30-day/.test(text(say)), 'described by the whole spoken summary of the plate');
      // Pressed while the chart is shown (the chart opens with the screen); the word folds it, and the keyboard stays on the word.
      assert.equal(word.getAttribute('aria-pressed'), 'true');
      assert.equal(classes(env.root, 'tl-block').length, 1);
      word.focus();
      click(word);
      await settle();
      plate = plateOf(env);
      const folded = classes(plate, 'l-chart')[0];
      assert.equal(folded.getAttribute('aria-pressed'), 'false');
      assert.equal(text(folded), 'Chart', 'the same word, not Show/Hide');
      assert.equal(classes(env.root, 'tl-block').length, 0, 'folded: no chart under the plate');
      assert.equal(env.document.activeElement.getAttribute('data-focus'), word.getAttribute('data-focus'), 'the keyboard stays on the word through the redraw');
      click(folded);
      await settle();
      assert.equal(classes(plateOf(env), 'l-chart')[0].getAttribute('aria-pressed'), 'true');
      assert.equal(classes(env.root, 'tl-block').length, 1);
      assert.equal(env.document.activeElement.getAttribute('data-focus'), word.getAttribute('data-focus'));
    } catch (error) {
      console.error(error && error.stack ? error.stack : error);
      process.exit(1);
    } finally { done(env); }
  } else if (mode === 'one_red') {
    // The red of a plate is two things and no more: the decimal point of its figure (a span of its own, .wt-dot, in the crimson ink) and
    // the pale-crimson bubble of the accounts at the limit now (.wt-limit; in a billing cycle, of the money spent beyond the plan). No red
    // digit in the legend or the reset row (no b.acc), no state colour: held back, dated and unknown ones are told by their words.
    const reds = (plate) => walk(plate).filter((n) => n.tagName === 'B' && has(n, 'acc'));
    // The card of the picked account stands in the plate under the chart (.ins-wrap) and keeps its own status dots (the card tells the state of its account in colour, as it always did):
    // the plate's colour is told without it.
    const states = (plate) => plate.childNodes.filter((c) => !has(c, 'ins-wrap')).flatMap((c) => walk(c)).filter((n) => has(n, 'bad') || has(n, 'warn') || has(n, 'rs-bad') || has(n, 'rs-warn'));
    const dots = (plate) => classes(plate, 'wt-dot');
    const want = { codex_states: 1, claude_five: 1, claude_single: 1, claude_many: 1, reasons_two: 0, reasons_three: 0, cursor_budget: 1, cursor_mixed: 1 };
    for (const [key, n] of Object.entries(want)) {
      const env = await boot(views[key]);
      const plate = plateOf(env);
      assert.equal(reds(plate).length, 0, key + ': no red digit under the bars: ' + text(plate));
      assert.equal(classes(plate, 'wt-limit').length, n, key + ': the bubble is there when something is at the limit, and only then');
      assert.equal(states(plate).length, 0, key + ': no state colour on the plate');
      // The point of the figure: one span with the dot alone, and the number whole in its node.
      assert.equal(dots(plate).length, 1, key);
      assert.equal(text(dots(plate)[0]), '.', key);
      assert.ok(has(dots(plate)[0].parentNode, 'wt-num'), key + ': the dot stands in the number');
      assert.ok(dots(plate)[0].parentNode.parentNode.parentNode === classes(plate, 'wt-fact')[0], key + ': in the bubble of the fact');
      done(env);
    }
    // The bubble is the one place the accounts at the limit are told; the legend has no item for them.
    let env = await boot(views.codex_states);
    let plate = plateOf(env);
    assert.ok(!itemsOf(plate).some((i) => i.getAttribute('data-k') === 'limit'), 'no item "limit" in the legend');
    assert.equal(text(classes(plate, 'wt-limit-head')[0]), 'At the limit · 3');
    done(env);
    // A billing cycle: the money beyond the allowance is the bubble "Over plan", not a red figure of the reset row.
    env = await boot(views.cursor_budget);
    plate = plateOf(env);
    assert.deepEqual(classes(plate, 'wt-limit').map(text), ['Over plan · $21.65']);
    assert.equal(classes(nxOf(plate), 'acc').length, 0, 'nothing red in the reset row');
    assert.ok(lgOf(plate) === undefined, 'every account is counted: no legend');
    done(env);
    // A window with no current reading: the figure is a dash, and a dash has no point: nothing is red.
    env = await boot(views.reading_none);
    plate = plateOf(env);
    assert.equal(dots(plate).length, 0, 'no point, no red');
    assert.ok(has(classes(plate, 'wt-num')[0], 'none') && text(classes(plate, 'wt-num')[0]) === '—');
    assert.equal(classes(plate, 'wt-limit').length, 0, 'and nothing at the limit');
    done(env);
  } else if (mode === 'exceptions_in_words') {
    // Under the bars only the exceptions, in words (an item is the number and its words): at the limit now, held back (a reason each),
    // at the limit when last read, read earlier (with the sum of what they had); then the row of the reset. Not a word of the accounts
    // that are in the line, and no item for an account with no usable reading (its bar is a "?" and the note names it).
    const env = await boot(views.codex_states);
    const plate = plateOf(env);
    // At the limit now is the plate's note in words; the legend keeps the other exceptions. The ones read earlier (last known, at the limit when
    // last read) are in the note of the accounts not read, with their names and age, so the legend does not say "last known" again; "at the limit when last
    // read" stays (the dashed base of the bar).
    assert.deepEqual(itemsOf(plate).map(text), ['1 disabled', '1 at the limit when last read']);
    assert.ok(footOf(env) && footOf(env).parentNode === plate, 'the note of the accounts not read stands in the plate');
    assert.deepEqual(classes(plate, 'wt-limit-head').map(text), ['At the limit · 3']);
    assert.ok(!/healthy|in the line|ok\b/i.test(text(lgOf(plate))));
    assert.equal(classes(plate, 'l-sub').length, 0);
    assert.equal(stripBars(plate).filter((b) => has(b, 'unknown')).length, 0);
    done(env);
  } else if (mode === 'fact_bubble') {
    // The fact of the plate is the number set in the open, no bubble: the caption, the name of the window alone (the whole name of the picked tab; a
    // billing cycle says "Period"; the word "Remaining" is gone, its meaning stays in the hint of the figure), then the figure (l-fig) with its unit. The decimal point of the number is a span of its own (.wt-dot,
    // the crimson ink) and the text of the number is whole; a number with no point to colour (a dash, "<0.01") has no span, nothing red.
    const check = (env, label, caption) => {
      const plate = plateOf(env);
      const facts = classes(plate, 'wt-fact');
      assert.equal(facts.length, 1, label + ': one bubble of the fact');
      const fact = facts[0];
      assert.ok(fact.parentNode === plate, label + ': a part of the plate');
      assert.equal(fact.childNodes.length, 2, label + ': the caption, then the figure');
      assert.equal(fact.childNodes[0].className, 'wt-fact-cap');
      assert.equal(text(fact.childNodes[0]), caption, label);
      const fig = fact.childNodes[1];
      assert.ok(has(fig, 'l-fig'), label + ': the figure keeps the plate\'s class');
      assert.ok(fig.title.length > 40 && /Last-known and unknown accounts are not counted, never as 0\.$|ranking, not a verdict|The mean of each counted account/.test(fig.title), label + ': its hint is kept: ' + fig.title);
      const number = classes(fig, 'wt-num')[0];
      assert.equal(number.tagName, 'B');
      assert.ok(has(number, 'num-strong'), label);
      const said = text(number);
      const dots = classes(number, 'wt-dot');
      if (/^[^<\d]*\d+\.\d+$/.test(said)) {
        assert.equal(dots.length, 1, label + ': ' + said);
        assert.equal(text(dots[0]), '.');
        assert.equal(dots[0].tagName, 'SPAN');
        assert.equal(number.childNodes.length, 3, label + ': before the point, the point, after it');
        assert.equal(number.childNodes.map((n) => n.textContent).join(''), said, label + ': the number is whole in its node');
      } else {
        assert.equal(dots.length, 0, label + ': no point in "' + said + '", nothing red');
        assert.ok(number.childNodes.length <= 1, label + ': a word or a dash is one piece of text');
      }
      assert.equal(text(classes(fig, 'of')[0]).charAt(0), ' ', label + ': the unit follows the number after a space');
      return said;
    };
    for (const key of ['claude', 'codex', 'agy']) {
      const env = await boot(views[key]);
      const live = tabsOf(env).filter((t) => t.getAttribute('aria-disabled') !== 'true').map((t) => t.getAttribute('data-focus'));
      assert.ok(live.length >= 2, key);
      for (const focus of live) {
        if (byFocus(env.root, focus).getAttribute('aria-selected') !== 'true') click(byFocus(env.root, focus));
        const say = check(env, key + ' ' + focus, nameOf(pickedTab(env)));
        assert.match(say, /^(\d+\.\d\d|<0\.01|—)$/, key + ' ' + focus);
        // The unit says what it is, whole: "1.47 left of 20 accounts" (the bubble adds the caption and takes nothing from it).
        assert.match(text(classes(plateOf(env), 'l-fig')[0]), /^(\d+\.\d\d|<0\.01|—)( left of \d+ accounts?| no current reading)$/, key + ' ' + focus);
      }
      done(env);
    }
    // A sum so small that it reads "<0.01": no point, no red.
    const small = JSON.parse(JSON.stringify(views.claude));
    for (const g of small.reserve.summary.groups) g.measured = Object.assign({}, g.measured, { windows: 0.001, at_limit: 0 });
    let env = await boot(small);
    assert.equal(check(env, 'small', nameOf(pickedTab(env))), '<0.01');
    done(env);
    // No current reading: a dash in the quiet ink, no point.
    env = await boot(views.reading_none);
    assert.equal(check(env, 'none', nameOf(pickedTab(env))), '—');
    done(env);
    // A billing cycle: the caption is "Period", the sum is dollars with a point.
    env = await boot(views.cursor_budget);
    assert.equal(check(env, 'cycle', 'Period'), '$16.40');
    done(env);
    // The caption is the picked window's name when the reader moves along the row: the plate is built anew and says the new window.
    env = await boot(views.claude);
    const names = [];
    for (const focus of tabsOf(env).map((t) => t.getAttribute('data-focus'))) {
      const tab = byFocus(env.root, focus);
      if (tab.getAttribute('aria-disabled') === 'true') continue;
      if (tab.getAttribute('aria-selected') !== 'true') click(tab);
      names.push(text(classes(plateOf(env), 'wt-fact-cap')[0]));
    }
    assert.deepEqual(names, ['5-hour', 'Weekly']);
    done(env);
  } else if (mode === 'limit_bubble') {
    // The accounts at the limit now, in the plate's bubble (limitBubble): only when there are such accounts (a current reading at the limit);
    // the first three by name and "N more" for the rest; "back in 9h" is how long to the soonest reset still to come among them (not the day and the hour: those are in the title), none, no tail.
    const bubbleOf = (env) => classes(plateOf(env), 'wt-limit');
    // In how long, as the widget says it: the whole remainder in minutes rounded down; under a day the hours alone, then the days and the hours after them. The render was a moment
    // before this call, so the words of the last four seconds are all allowed.
    const leftAt = (iso, now) => { const m = Math.floor((Date.parse(iso) - now) / 60000); if (m < 1) return '<1m'; if (m < 60) return m + 'm'; const h = Math.floor(m / 60); if (h < 24) return h + 'h'; const d = Math.floor(h / 24); return h % 24 ? d + 'd ' + (h % 24) + 'h' : d + 'd'; };
    const leftAny = (iso) => [0, 500, 1000, 2000, 4000].map((ms) => leftAt(iso, Date.now() - ms));
    const atLimit = (view) => view.reserve.summary.groups.flatMap((g) => g.bars).filter((b) => b.state === 'current' && b.at_limit);
    // 1. None at the limit: no bubble, in any window of any family, and no place left for one.
    for (const key of ['claude', 'codex', 'agy', 'reasons_two', 'reasons_three', 'reading_part']) {
      const env = await boot(views[key]);
      const live = tabsOf(env).filter((t) => t.getAttribute('aria-disabled') !== 'true').map((t) => t.getAttribute('data-focus'));
      for (const focus of live) {
        if (byFocus(env.root, focus).getAttribute('aria-selected') !== 'true') click(byFocus(env.root, focus));
        assert.equal(bubbleOf(env).length, 0, key + ' ' + focus + ': nothing at the limit, no bubble');
        assert.ok(!/At the limit/.test(text(plateOf(env))), key + ' ' + focus);
      }
      done(env);
    }
    // 2. At the limit at the last reading only (dated): not now, so no bubble; the legend says it in its words.
    let env = await boot(views.reading_none);
    assert.equal(bubbleOf(env).length, 0, 'last known at the limit is not at the limit now');
    done(env);
    // 3. Three at the limit, each to reset at its own time: the soonest of them is the tail.
    const five = views.claude_five;
    const stops = atLimit(five).map((b) => b.resets_at).sort();
    assert.equal(stops.length, 3);
    env = await boot(five);
    let bubble = bubbleOf(env)[0];
    assert.equal(bubbleOf(env).length, 1);
    assert.equal(text(classes(bubble, 'wt-limit-back')[0]), text(bubble).split(' · back ')[1] ? 'back ' + text(bubble).split(' · back ')[1] : '', 'the tail is its own span');
    const tail = () => text(classes(bubble, 'wt-limit-back')[0]);
    assert.ok(leftAny(stops[0]).some((w) => tail() === 'back in ' + w), 'the soonest reset, in how long: ' + text(bubble) + ' / ' + stops[0]);
    assert.ok(!/\d\d:\d\d/.test(tail()) && !/\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b/.test(tail()), 'no hour and no day in the tail: it says in how long');
    assert.ok(bubble.title.split('\n').every((line) => / until [A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2} \d\d:\d\d$/.test(line)), 'and the moment, whole, is in the title of the bubble: ' + bubble.title);
    done(env);
    // 4. The soonest has already passed: the next one still to come is the tail.
    const passed = JSON.parse(JSON.stringify(five));
    const firstBar = atLimit(passed).find((b) => b.resets_at === stops[0]);
    firstBar.resets_at = new Date(Date.now() - 600000).toISOString();
    env = await boot(passed);
    bubble = bubbleOf(env)[0];
    assert.ok(leftAny(stops[1]).some((w) => text(classes(bubble, 'wt-limit-back')[0]) === 'back in ' + w), 'the next to come: ' + text(bubble));
    assert.match(text(bubble), /^At the limit · 3 /, 'all three are still at the limit now');
    done(env);
    // 5. No reset to come among them: no tail (no dot, no "back").
    const none = JSON.parse(JSON.stringify(five));
    for (const b of atLimit(none)) b.resets_at = null;
    env = await boot(none);
    bubble = bubbleOf(env)[0];
    assert.equal(classes(bubble, 'wt-limit-back').length, 0);
    assert.ok(!/back/.test(text(bubble)) && !/ · $/.test(text(bubble)), text(bubble));
    assert.ok(!/back/.test(bubble.getAttribute('aria-label')), bubble.getAttribute('aria-label'));
    assert.ok(/^At the limit · 3 \S+, \S+, \S+$/.test(text(bubble)), text(bubble));
    done(env);
    // 6. The bubble does not stand in the legend, and the legend keeps its other lines ("disabled" is one). Where the plate has the note of the
    // accounts not read, the line "last known" is not said twice: the note names those accounts and their age. "At the limit when last read" stays.
    env = await boot(views.codex_states);
    assert.ok(itemsOf(plateOf(env)).every((i) => i.getAttribute('data-k') !== 'limit'));
    assert.ok(itemsOf(plateOf(env)).some((i) => /disabled/.test(text(i))), 'the line of the disabled stays');
    assert.ok(footOf(env) && footOf(env).parentNode === plateOf(env), 'the note stands in the plate');
    assert.ok(itemsOf(plateOf(env)).every((i) => i.getAttribute('data-k') !== 'last'), 'the note says the dated ones and their age: no line for them');
    assert.ok(itemsOf(plateOf(env)).some((i) => i.getAttribute('data-k') === 'was'), 'the line of the dashed base stays: it is another meaning');
    done(env);
    // 7. One account; and a bubble that follows the window picked (the 5-hour of Claude has none here, the plate of another window is another).
    env = await boot(views.claude_single);
    assert.match(text(bubbleOf(env)[0]), /^At the limit · 1 claude3 · back /);
    done(env);
  } else if (mode === 'limit_hover') {
    // The bubble is a handle of its accounts: pointed at, touched or focused it dims the plate to their bars (data-dim=group, data-lit), as the
    // note's groups do; "N more" opens the account list and moves nothing of that; the keyboard stays on the bubble through a redraw.
    const probe = await boot(views.claude_many);
    const proto = Object.getPrototypeOf(plateOf(probe));
    if (!proto.removeAttribute) proto.removeAttribute = function (name) { delete this.attributes[name]; };
    if (!proto.matches) proto.matches = function (selector) { return selector === ':focus-visible' && this.ownerDocument.activeElement === this; };
    done(probe);
    for (const key of ['claude_many', 'codex_states']) {
      const env = await boot(views[key]);
      const plate = plateOf(env);
      const bubble = classes(plate, 'wt-limit')[0];
      const who = JSON.parse(bubble.getAttribute('data-accounts'));
      const lit = () => stripBars(plate).filter((b) => b.getAttribute('data-lit') !== null).map((b) => b.getAttribute('data-account'));
      assert.deepEqual(lit(), [], key);
      bubble.listeners.mouseenter[0]();
      assert.equal(plate.getAttribute('data-dim'), 'group', key);
      assert.deepEqual(lit().sort(), [...who].sort(), key + ': the bars of the accounts at the limit');
      assert.equal(stripBars(plate).filter((b) => has(b, 'spent')).length, who.length, key + ': the bars at the limit are those');
      bubble.listeners.mouseleave[0]();
      assert.equal(plate.getAttribute('data-dim'), null, key);
      assert.deepEqual(lit(), [], key);
      bubble.listeners.pointerdown[0]({ pointerType: 'touch' });
      assert.equal(plate.getAttribute('data-dim'), 'group', key + ': a finger lights them');
      bubble.listeners.pointerup[0]({ pointerType: 'touch' });
      assert.equal(plate.getAttribute('data-dim'), null, key);
      // The keyboard: the bubble takes it and lights their bars while it holds it; a redraw of fresh data gives it back to the new bubble.
      bubble.focus();
      bubble.listeners.focus[0]();
      assert.equal(plate.getAttribute('data-dim'), 'group', key);
      const focusKey = bubble.getAttribute('data-focus');
      assert.match(focusKey, /^atlimit:/);
      bubble.listeners.blur[0]();
      assert.equal(plate.getAttribute('data-dim'), null, key);
      bubble.focus();
      env.interval()(); await settle();
      assert.equal(env.document.activeElement.getAttribute('data-focus'), focusKey, key + ': the keyboard stays on the bubble');
      assert.ok(has(env.document.activeElement, 'wt-limit'), key);
      // "N more" (where the bubble counts more than three): a press opens the account list and the keyboard goes to it; the plate is not dimmed.
      const more = classes(classes(plateOf(env), 'wt-limit')[0], 'wt-foot-more')[0];
      if (key === 'claude_many') {
        assert.ok(more, key);
        assert.equal(plateOf(env).getAttribute('data-dim'), null);
        click(more);
        await settle();
        // The word over the bars ("Accounts · N", data-focus=accounts) carries aria-pressed now; the bottom toggle with aria-expanded is gone.
        assert.equal(byFocus(env.root, 'accounts').getAttribute('aria-pressed'), 'true');
        assert.equal(plateOf(env).getAttribute('data-dim'), null, 'the press moves no highlight');
      } else {
        assert.ok(!more, key + ': three names, no "more"');
      }
      done(env);
    }
  } else if (mode === 'credits_head') {
    // The credits over the plate: the head counts what is reported now ("Manual resets · 2"), then the parts - on how many accounts, and what is
    // not in the count; the reading's own words are kept and none of it is red.
    const credits = (patch, fields) => {
      const v = JSON.parse(JSON.stringify(views.codex));
      const base = { count: 0, current_accounts: 0, last_known_count: 0, last_known_accounts: 0, unknown_accounts: 0, unreadable_accounts: 0, conflict_accounts: 0, accounts: 21 };
      v.groups[0].reset_credits = Object.assign(base, patch);
      return boot(v);
    };
    let env = await credits({ count: 2, current_accounts: 2, unknown_accounts: 19 });
    let row = classes(env.root, 'family-credits')[0];
    assert.equal(text(row), 'Manual resets · 2 on 2 accounts · 19 unknown');
    assert.equal(text(classes(row, 'emph')[0]), 'Manual resets · 2', 'the head carries the count');
    assert.equal(walk(row).filter((n) => has(n, 'acc') || has(n, 'bad') || has(n, 'warn')).length, 0, 'and nothing red');
    assert.match(row.title, /^Provider-reported manual reset credits/);
    done(env);
    env = await credits({ count: 1, current_accounts: 1 });
    assert.equal(text(classes(env.root, 'family-credits')[0]), 'Manual resets · 1 on 1 account');
    done(env);
    // Read now as none: the zero is a reading, said as one; the rare parts stay (last known, unreadable, in conflict).
    env = await credits({ count: 0, current_accounts: 1, last_known_count: 3, last_known_accounts: 2, unreadable_accounts: 1, conflict_accounts: 1,
      last_known_oldest_observed_at: new Date(Date.now() - 3 * 3600000).toISOString() });
    row = classes(env.root, 'family-credits')[0];
    assert.match(text(row), /^Manual resets · 0 on 1 account · last known 3 across 2 accounts \(3 ?h ago\) · 1 unreadable · 1 with sources disagreeing$/, text(row));
    done(env);
    // Nothing read now: the head alone has no count, and the parts say what there is.
    env = await credits({ last_known_count: 2, last_known_accounts: 1 });
    row = classes(env.root, 'family-credits')[0];
    assert.match(text(row), /^Manual resets last known 2 across 1 account/, text(row));
    assert.ok(!/Manual resets · /.test(text(row)), 'no count of what is not read now');
    done(env);
    // Where there is a plate the credits stand in it, after the notes of the accounts at the limit and not read (when there are such) and before the
    // legend and the reset row; they are not a child of the section any more (the calls with no plate keep them there: the rows below).
    env = await credits({ count: 2, current_accounts: 2 });
    const section = classes(env.root, 'reserve')[0];
    const plate = plateOf(env);
    const kids = plate.childNodes;
    const at = kids.indexOf(classes(plate, 'family-credits')[0]);
    assert.ok(at >= 0, 'the credits are a child of the plate');
    assert.ok(!section.childNodes.some((n) => has(n, 'family-credits')), 'and not of the section');
    // (the chart of the picked top account stands in the plate, right under the bars: .tl-wrap, and its card under the chart: .ins-wrap)
    assert.ok(kids.slice(0, at).every((n) => ['bars', 'tl-wrap', 'ins-wrap', 'wt-fact', 'wt-scope', 'wt-stamp', 'l-chart', 'sr-only', 'wt-limit', 'wt-foot'].some((c) => has(n, c))), 'only the plate\'s own parts and the notes stand before them: ' + kids.slice(0, at).map((n) => n.className).join(' | '));
    assert.ok(kids.slice(at + 1).every((n) => has(n, 'lg') || has(n, 'nx')), 'the legend and the reset row follow: ' + kids.slice(at + 1).map((n) => n.className).join(' | '));
    done(env);
  } else if (mode === 'reset_handle') {
    // The row of the reset is a handle of the accounts that reset then, where it counts exactly them (the engine's number of accounts and of
    // what they give back): it takes the keyboard, says their names aloud, and under the pointer or a finger the plate dims to them (the other
    // bars step back, theirs wear data-lit). Where it does not count exactly them, it is plain text: no tab stop, no role, no event.
    // The fake element has no removeAttribute (a browser's has): give it the one a browser has, so the widget's own events can run here.
    const probe = await boot(views.codex_states);
    const proto = Object.getPrototypeOf(plateOf(probe));
    if (!proto.removeAttribute) proto.removeAttribute = function (name) { delete this.attributes[name]; };
    // ...and the ring: a node answers :focus-visible while the keyboard (the document's active element) is on it.
    if (!proto.matches) proto.matches = function (selector) { return selector === ':focus-visible' && this.ownerDocument.activeElement === this; };
    done(probe);
    let handles = 0;
    let plain = 0;
    for (const key of Object.keys(views)) {
      const env = await boot(views[key]);
      const plate = plateOf(env);
      const nx = plate ? nxOf(plate) : null;
      if (nx && nx.getAttribute('tabindex') !== null) {
        handles += 1;
        assert.equal(nx.getAttribute('tabindex'), '0', key);
        assert.equal(nx.getAttribute('role'), 'group', key);
        assert.equal(nx.getAttribute('data-k'), 'reset', key);
        assert.match(nx.getAttribute('data-focus'), /^nx:/, key);
        const who = JSON.parse(nx.getAttribute('data-accounts'));
        assert.ok(Array.isArray(who) && who.length >= 1, key);
        assert.match(nx.getAttribute('aria-label'), /^next reset .* — \S/, key + ': the words of the row, then the names');
        assert.ok(['mouseenter', 'mouseleave', 'focus', 'blur', 'pointerdown', 'pointerup', 'pointercancel']
          .every((e) => nx.listeners[e] && nx.listeners[e].length === 1), key + ': hover, focus and touch are one thing');
        const lit = () => stripBars(plate).filter((b) => b.getAttribute('data-lit') !== null).map((b) => b.getAttribute('data-account'));
        assert.deepEqual(lit(), [], key);
        // The pointer: the plate dims to the group, theirs are lit; it lets go on leaving.
        nx.listeners.mouseenter[0]();
        assert.equal(plate.getAttribute('data-dim'), 'group', key);
        assert.deepEqual(lit().sort(), [...who].sort(), key + ': the bars of the accounts that reset then');
        nx.listeners.mouseleave[0]();
        assert.equal(plate.getAttribute('data-dim'), null, key);
        assert.deepEqual(lit(), [], key);
        // A finger: it lights on landing, lets go on lifting or when the system takes the touch away; a mouse's pointer event does neither,
        // and the mouse events a touch makes right after its lift do not light the plate for good.
        nx.listeners.pointerdown[0]({ pointerType: 'mouse' });
        assert.equal(plate.getAttribute('data-dim'), null, key + ': a mouse press is not a touch');
        nx.listeners.pointerdown[0]({ pointerType: 'touch' });
        assert.equal(plate.getAttribute('data-dim'), 'group', key);
        assert.deepEqual(lit().sort(), [...who].sort(), key);
        nx.listeners.pointerup[0]({ pointerType: 'touch' });
        assert.equal(plate.getAttribute('data-dim'), null, key);
        nx.listeners.mouseenter[0]();
        assert.equal(plate.getAttribute('data-dim'), null, key + ': the mouse event a lift makes is not the pointer\'s');
        nx.listeners.pointerdown[0]({ pointerType: 'touch' });
        nx.listeners.pointercancel[0]({ pointerType: 'touch' });
        assert.equal(plate.getAttribute('data-dim'), null, key + ': a cancelled touch lets go');
        assert.deepEqual(lit(), [], key);
        // The keyboard stays on the row through the redraw of fresh data.
        nx.focus();
        const focusKey = nx.getAttribute('data-focus');
        env.interval()(); await settle();
        assert.equal(env.document.activeElement.getAttribute('data-focus'), focusKey, key + ': the keyboard stays on the row');
        assert.ok(has(env.document.activeElement, 'nx'), key);
      } else if (nx) {
        plain += 1;
        assert.ok(['tabindex', 'role', 'data-k', 'data-focus', 'data-accounts', 'aria-label'].every((a) => nx.getAttribute(a) === null), key + ': plain text');
        assert.ok(!Object.keys(nx.listeners).length, key + ': nothing answers a pointer');
      }
      done(env);
    }
    assert.ok(handles >= 2, 'the reset row is a handle in several of the plates: ' + handles);
    assert.ok(plain >= 1, 'and plain text where it does not count the accounts it names: ' + plain);
    // Two accounts whose resets are 30 s apart are one group to the engine (it counts the accounts within two minutes of the soonest, and what
    // they give back): the row counts them, names both and lights both; the third, much later, is not theirs.
    const close = await boot(views.reset_close);
    const closePlate = plateOf(close);
    const closeNx = nxOf(closePlate);
    assert.match(text(closeNx), /2 accounts/, 'the engine counted two: ' + text(closeNx));
    assert.equal(closeNx.getAttribute('tabindex'), '0', 'and the row points at them');
    const closeWho = JSON.parse(closeNx.getAttribute('data-accounts'));
    assert.equal(closeWho.length, 2, closeNx.getAttribute('data-accounts'));
    assert.ok(closeWho.every((a) => /close[12]$/.test(a)), closeNx.getAttribute('data-accounts'));
    closeNx.listeners.mouseenter[0]();
    const closeLit = stripBars(closePlate).filter((b) => b.getAttribute('data-lit') !== null).map((b) => b.getAttribute('data-account'));
    assert.deepEqual(closeLit.sort(), [...closeWho].sort(), 'both are lit');
    assert.equal(stripBars(closePlate).length, 3);
    done(close);
    // The keyboard holds a legend item (its ring shows) while the pointer passes over the reset row, and the other way round: the pointer
    // leaving gives the plate back to the handle the keyboard holds, whichever list of the plate it stands in.
    const kb = await boot(views.codex_states);
    const kbPlate = plateOf(kb);
    const kbNx = nxOf(kbPlate);
    const kbItem = itemsOf(kbPlate)[0];
    assert.ok(kbItem && kbNx.getAttribute('tabindex') === '0', 'a handle in each of the two lists');
    kbItem.focus();
    kbItem.listeners.focus[0]();
    assert.equal(kbPlate.getAttribute('data-dim'), kbItem.getAttribute('data-k'), 'the keyboard on the item dims to its kind');
    kbNx.listeners.mouseenter[0]();
    assert.equal(kbPlate.getAttribute('data-dim'), 'group', 'the pointer on the reset row dims to it');
    kbNx.listeners.mouseleave[0]();
    assert.equal(kbPlate.getAttribute('data-dim'), kbItem.getAttribute('data-k'), 'leaving the row gives the plate back to the item the keyboard holds');
    kbNx.focus();
    kbNx.listeners.focus[0]();
    assert.equal(kbPlate.getAttribute('data-dim'), 'group', 'the keyboard on the reset row dims to its accounts');
    kbItem.listeners.mouseenter[0]();
    assert.equal(kbPlate.getAttribute('data-dim'), kbItem.getAttribute('data-k'));
    kbItem.listeners.mouseleave[0]();
    assert.equal(kbPlate.getAttribute('data-dim'), 'group', 'and the other way round');
    // With the keyboard on neither (a bar, or nothing), the pointer leaving lets the plate go.
    kb.document.activeElement = null;
    kbItem.listeners.mouseenter[0]();
    kbItem.listeners.mouseleave[0]();
    assert.equal(kbPlate.getAttribute('data-dim'), null, 'no handle held by the keyboard: nothing stays dimmed');
    done(kb);
  } else if (mode === 'about_words') {
    // "About this widget" tells what the plate is now: the one word Chart (pressed while the chart is shown; no Hide/Show), the dated count of
    // the last-known accounts under the bars (no dated line under the figure), and no item for a label the plate does not draw: the ranking
    // ("lowest left") is said once, in the item of the figure, as the figure's hint says it.
    const env = await boot(views.claude);
    click(byFocus(env.root, 'about'));
    const about = classes(env.root, 'about-panel')[0];
    assert.ok(about, 'About is open');
    const body = text(about);
    assert.ok(!/Hide chart|Show chart/i.test(body), 'the plate has the one word Chart');
    assert.ok(!/Last known” line|dated “Last known”/.test(body), 'there is no line of its own under the figure');
    assert.match(body, /the plate’s word “Chart” \(pressed while the chart is shown\) folds the timeline/);
    const titles = classes(about, 'about-title').map(text);
    assert.ok(!titles.includes('Lowest left'), 'no item for a label the plate does not draw: ' + titles.join(' | '));
    const figure = classes(about, 'about-item').find((n) => text(classes(n, 'about-title')[0]) === 'Figure');
    assert.ok(figure, 'the item of the figure: ' + titles.join(' | '));
    assert.match(text(figure), /stand, dated, in the “No fresh data” note/);
    assert.doesNotMatch(text(figure), /count under the bars/);
    assert.match(text(figure), /lowest average share left is named in the figure’s hint, nowhere else: a ranking, not a verdict/);
    assert.match(text(figure), /binds only the models it names/);
    done(env);
  } else if (mode === 'legend_agy') {
    // Antigravity: no account is at the limit, held or unknown - no legend, and no empty row left for it.
    const env = await boot(views.agy);
    for (const seconds of [18000, 604800]) {
      click(focusTab(env, 'agy', seconds));
      for (const key of classes(env.root, 'wt-scope-opt').map((b) => b.getAttribute('data-focus'))) {
        click(byFocus(env.root, key));
        const plate = plateOf(env);
        assert.equal(classes(plate, 'lg').length, 0, key + ': no legend');
        assert.equal(classes(plate, 'l-tail').length, 0);
        const cells = cellsOf(plate);
        assert.match(cells[0], /^next reset \w{3} \d+ \w{3} \d\d:\d\d$/, key);
        assert.ok(cells.slice(1).every((c) => /^(\d+ accounts|\+\d+\.\d\d if unused)$/.test(c)), cells.join('|'));
        assert.equal(plate.childNodes.filter((n) => has(n, 'lg')).length, 0);
        assert.ok(plate.childNodes.indexOf(nxOf(plate)) > plate.childNodes.indexOf(classes(plate, 'bars')[0]));
      }
    }
    done(env);
  } else if (mode === 'cursor_bare') {
    // Resources with no included pool (on-demand only) are no budget: no place is drawn (no rail) and the original text stands.
    const env = await boot(views.cursor_bare);
    const tabs = tabsOf(env);
    assert.equal(tabs.length, 0, 'no limit: no grey places, no rail');
    assert.equal(classes(env.root, 'rail').length, 0);
    assert.equal(classes(env.root, 'wt-plate').length, 0);
    assert.match(env.root.textContent, /No quota limit reported for Cursor\./);
    assert.deepEqual(classes(env.root, 'acc-row').map((r) => text(classes(r, 'acc-state')[0])),
      ['quota unavailable · No live quota source', 'quota unavailable · No live quota source']);
    done(env);
  } else if (mode === 'ladder' || mode === 'ladder_fit' || mode === 'ladder_open' || mode === 'ladder_motion') {
    // The ladder of resets under the rings (kit: rail): the steps of the window picked, soonest first, each the soonest reset not taken yet with every reset
    // within two minutes after it, the accounts of the step and what they give back. Fake DOM: the markup and the numbers; the layout is a browser's.
    const NOW = Date.now();
    // (30 s over every whole second given: the reset is how long to it, rounded down, so the few ms between this clock and the widget's must not decide a minute)
    const iso = (s) => new Date(NOW + (s + 30) * 1000).toISOString();
    // A view whose first limit has these bars: `rows` are {left, in (seconds to the reset, null for none), state}; the accounts are the view's own.
    const reshape = (view, rows) => {
      const v = JSON.parse(JSON.stringify(view));
      const g = v.reserve.summary.groups[0];
      const own = g.bars.slice();
      g.bars = rows.map((r, i) => Object.assign({}, own[i], { state: r.state || 'current', left: r.left, at_limit: false, resets_at: r.in === null ? null : iso(r.in) }));
      return v;
    };
    const zoneOf = (env) => classes(env.root, 'rail-ladder-zone')[0];
    // (every step of the window is in the DOM now: the ones past the room, or past four while the ladder is folded, wear rail-fit-off; stepsOf counts the ones in sight)
    const allStepsOf = (env) => classes(env.root, 'rail-step').filter((n) => !has(n.parentNode.parentNode, 'rail-ladder-out'));
    const stepsOf = (env) => allStepsOf(env).filter((n) => !has(n, 'rail-fit-off'));
    const stepTab = (step) => step.getAttribute('tabindex') === '0';
    const moreOf = (env) => classes(env.root, 'rail-more')[0];
    const titleOf = (env) => classes(env.root, 'rail-ladder-title')[0];
    const accountsOfStep = (step) => JSON.parse(step.getAttribute('data-accounts'));
    const timeOf = (step) => text(classes(step, 'rail-step-time')[0]);
    const dotsOf = (step) => classes(step, 'rail-step-dot');
    const amountOf = (step) => text(classes(step, 'rail-step-amount')[0]);
    const lineOf = (env) => nxOf(plateOf(env));
    // The rows of a ten-account family: four to be told apart by the reset (A..J below).
    const ten = views.claude_many;
    // Ten accounts whose resets are two hours apart: ten steps that say ten different things in how long (1h, 3h .. 19h), so none is glued to its neighbour (the ladder draws four).
    const spread = reshape(ten, ten.reserve.summary.groups[0].bars.map((b, i) => ({ left: .5, in: 3600 + i * 7200 })));
    if (mode === 'ladder') {
      // 1. Order, clusters of 120 s, only the future, only the current. B resets in 1 h, C 119 s after it (one step with B), D 121 s after B (a step of its own: the
      // cluster is counted from the soonest not taken, not from the one before), A in 2 h; E has reset already, F is read earlier (last known), G has no reset
      // time, H no share, I resets in 9 days (a date, not a day), J in 10. (B is 58 min and a half away, so that D, 121 s after it, says "1h" and not the "58m" of B and C: neighbours that say
      // the same are glued into one step, and this block is about the clusters; the glue has its own block, 1b.)
      const rows = [
        { left: .5, in: 7200 }, { left: .2, in: 3480 }, { left: .9, in: 3599 }, { left: 0, in: 3601 }, { left: .3, in: -600 },
        { left: .1, in: 100, state: 'last_known' }, { left: .6, in: null }, { left: null, in: 500 }, { left: .4, in: 9 * 86400 }, { left: .7, in: 10 * 86400 }];
      const env = await boot(reshape(ten, rows));
      const keys = ten.reserve.summary.groups[0].bars.map((b) => b.account);
      assert.ok(classes(env.root, 'rail-ladder-zone').length === 1 && classes(env.root, 'rail-ladder').length === 1, 'one ladder');
      assert.ok(classes(env.root, 'rail')[0].childNodes.indexOf(zoneOf(env)) > classes(env.root, 'rail')[0].childNodes.indexOf(classes(env.root, 'wt-row')[0])
        && classes(env.root, 'rail')[0].childNodes.indexOf(zoneOf(env)) === classes(env.root, 'rail')[0].childNodes.length - 1, 'under the rings, the last in the rail');
      assert.ok(text(titleOf(env)) === 'Resets' && titleOf(env).getAttribute('aria-hidden') === 'true', 'the head: the word, no button');
      assert.ok(classes(env.root, 'rail-ladder')[0].getAttribute('aria-label') === 'Resets' && classes(env.root, 'rail-ladder')[0].getAttribute('role') === 'group');
      const steps = stepsOf(env);
      assert.ok(steps.length === 4, 'up to four steps: ' + steps.length);
      assert.ok(steps.every(stepTab), 'every step is a tab stop');
      const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
      assert.ok(same(accountsOfStep(steps[0]), [keys[1], keys[2]]), 'B and C (119 s apart) are one step, the soonest first');
      assert.ok(amountOf(steps[0]) === '+0.90', 'it gives back the sum of 1 - left: ' + amountOf(steps[0]));
      assert.ok(same(accountsOfStep(steps[1]), [keys[3]]) && amountOf(steps[1]) === '+1.00', 'D (121 s after B) is a step of its own');
      assert.ok(same(accountsOfStep(steps[2]), [keys[0]]) && amountOf(steps[2]) === '+0.50', 'A is next');
      assert.ok(same(accountsOfStep(steps[3]), [keys[8]]) && amountOf(steps[3]) === '+0.60', 'then I, 9 days away');
      const gone = [keys[4], keys[5], keys[6], keys[7]];
      assert.ok(!steps.some((s) => accountsOfStep(s).some((k) => gone.includes(k))), 'no past reset, no earlier reading, no reset without a time, no bar without a share');
      // Two lines under the bead: how long to the reset (every step has it; the whole remainder, rounded down: B 58 min and a half, D 60 and a half, A 120, I 9 days) and the amount.
      // No day, no date and no hour on the step: the moment, whole and local, is in its title (below).
      assert.ok(steps.every((s) => /^(<1m|\d+m|\d+h|\d+d( \d+h)?)$/.test(timeOf(s))), 'how long is there for every step: ' + steps.map(timeOf).join());
      assert.deepEqual(steps.map(timeOf), ['58m', '1h', '2h', '9d'], 'in how long, step by step: ' + steps.map(timeOf).join());
      assert.ok(steps.every((s) => s.childNodes.length === 3 && has(s.childNodes[0], 'rail-bead') && has(s.childNodes[1], 'rail-step-time') && has(s.childNodes[2], 'rail-step-amount')), 'a step is: the bead, how long, the amount');
      assert.ok(classes(env.root, 'rail-step').every((s) => !s.textContent.match(/\d\d:\d\d/) && !/\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b/.test(s.textContent)), 'no hour and no day written on a step');
      // The decimal point of every amount is a span of its own (the crimson ink), and the text of the amount stays whole: "+0.90".
      assert.ok(steps.every((s) => dotsOf(s).length === 1 && text(dotsOf(s)[0]) === '.' && dotsOf(s)[0].parentNode === classes(s, 'rail-step-amount')[0]), 'one point in the amount, a span of its own');
      assert.ok(steps.every((s) => /^\+\d+\.\d\d$/.test(amountOf(s))), 'the text of the amount is whole: ' + steps.map(amountOf).join());
      assert.ok(has(steps[0], 'rail-step-first') && !has(steps[1], 'rail-step-first'), 'the first step is the first one: its bead is the nearest reset\'s (the crimson ink)');
      assert.ok(steps.map((s) => s.style['--i']).join() === '1,2,3,4' && titleOf(env).style['--i'] === '0', 'rows numbered from the head');
      // The word that counts what did not fit is the last of the rows: the DOM and the number (after the last step that stands).
      const ladderBox = classes(env.root, 'rail-ladder')[0];
      assert.ok(ladderBox.childNodes[ladderBox.childNodes.length - 1] === moreOf(env) && moreOf(env).style['--i'] === '5', 'the word comes last: after the head (0) and the four steps (1-4), it is 5: ' + moreOf(env).style['--i']);
      steps.forEach((s) => {
        // (changed on purpose: a step is a button (the pick of its bar or the light of its bars), so its role is 'button' with aria-pressed, no longer 'group')
        assert.ok(s.getAttribute('aria-label') === s.title && s.getAttribute('role') === 'button' && s.getAttribute('aria-pressed') === 'false' && s.getAttribute('data-k') === 'reset' && /^rs:/.test(s.getAttribute('data-focus')), 'the step is told in words');
      });
      assert.ok(/^[A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2} \d\d:\d\d \u2014 2 accounts: .+ \u00b7 returns \+0\.90 if unused$/.test(steps[0].getAttribute('aria-label')), steps[0].getAttribute('aria-label'));
      assert.ok(/ \u2014 1 account: /.test(steps[1].getAttribute('aria-label')));
      // Five resets are in the DOM (the fifth wears rail-fit-off) and four stand: the word counts what is over four, as a button that opens the ladder below, in the rail.
      assert.ok(allStepsOf(env).length === 5 && has(allStepsOf(env)[4], 'rail-fit-off') && allStepsOf(env).slice(0, 4).every((s) => !has(s, 'rail-fit-off')), 'all five are in the DOM, the fifth is off');
      assert.ok(!has(moreOf(env), 'rail-more-void') && !has(moreOf(env), 'rail-fit-off') && text(moreOf(env)) === '1 more', 'the word: ' + text(moreOf(env)));
      assert.ok(moreOf(env).getAttribute('aria-label') === '1 more reset \u2014 show it below' && moreOf(env).getAttribute('type') === 'button' && moreOf(env).getAttribute('data-focus') === 'rs-more');
      assert.ok(moreOf(env).getAttribute('aria-expanded') === 'false', 'folded: aria-expanded is false');
      assert.ok(has(lineOf(env), 'nx-in-ladder'), 'the plate\'s row of the next reset wears the mark that hides it on the wide shell');
      // The word opens the ladder in the rail and touches nothing else: the plate, the chart and the Details are the same nodes (no drawing of the page),
      // and the right part is not changed. The steps all stand, the word reads "less" and says it is open.
      const plateBefore = plateOf(env);
      const ladderBefore = classes(env.root, 'rail-ladder')[0];
      const moreBefore = moreOf(env);
      const chartBefore = classes(env.root, 'l-chart').map((n) => n.getAttribute('aria-pressed'));
      const detailsBefore = classes(env.root, 'details').map((n) => String(n.open));
      moreOf(env).listeners.click[0]({ stopPropagation() {} });
      await settle();
      assert.ok(plateOf(env) === plateBefore && classes(env.root, 'rail-ladder')[0] === ladderBefore && moreOf(env) === moreBefore, 'the press redraws nothing: the plate, the ladder and the word are the same nodes');
      assert.ok(classes(env.root, 'rail-ladder').length === 1 && zoneOf(env), 'the ladder stands');
      assert.ok(moreOf(env).getAttribute('aria-expanded') === 'true' && text(moreOf(env)) === 'less' && moreOf(env).getAttribute('aria-label') === 'Less: fold the list of resets', 'open: "less", aria-expanded true');
      assert.ok(stepsOf(env).length === 5 && allStepsOf(env).every((s) => !has(s, 'rail-fit-off')), 'open: every step of the window stands');
      assert.ok(has(classes(env.root, 'rail-scroll')[0], 'rail-scroll-on'), 'the steps stand in the window that scrolls');
      assert.ok(classes(env.root, 'l-chart').map((n) => n.getAttribute('aria-pressed')).join() === chartBefore.join()
        && classes(env.root, 'details').map((n) => String(n.open)).join() === detailsBefore.join(), 'the chart and the Details are as they were');
      // The 30 s poll draws the ladder again: it stands open, with the same steps, in the new node.
      env.interval()(); await settle();
      assert.ok(moreOf(env).getAttribute('aria-expanded') === 'true' && text(moreOf(env)) === 'less' && stepsOf(env).length === 5, 'open across the poll');
      // The word again folds it: four steps and "1 more".
      moreOf(env).listeners.click[0]({ stopPropagation() {} });
      await settle();
      assert.ok(moreOf(env).getAttribute('aria-expanded') === 'false' && text(moreOf(env)) === '1 more' && stepsOf(env).length === 4 && !has(classes(env.root, 'rail-scroll')[0], 'rail-scroll-on'), 'folded again');
      done(env);
      // 1b. The glue: neighbouring steps that say the same in how long are ONE step: the earliest moment, the accounts of all, the sum of what they give back, and the title says the first and
      // the last moment ("Sun 1 Nov 12:31 \u2013 Sun 1 Nov 12:54"). P, Q, R (1 h, 1 h 15, 1 h 40: three steps apart by more than 120 s) all say "1h": one step; S ("2h") stands between and is not glued
      // to either side; T and U (3 h, 3 h 10) say "3h": one step; V (3 days) is its own. Equal words are never apart (the words only grow with the time), so what is glued is always neighbours.
      {
        const glue = await boot(reshape(ten, [{ left: .5, in: 3600 }, { left: .2, in: 4500 }, { left: 0, in: 6000 }, { left: .9, in: 7200 }, { left: .6, in: 10800 }, { left: .3, in: 11400 }, { left: .1, in: 3 * 86400 }]));
        const gs = stepsOf(glue);
        const same2 = (a, b) => JSON.stringify(a) === JSON.stringify(b);
        assert.ok(allStepsOf(glue).length === 4 && gs.length === 4, 'seven resets are four steps: ' + allStepsOf(glue).length);
        assert.deepEqual(gs.map(timeOf), ['1h', '2h', '3h', '3d'], 'one word each: ' + gs.map(timeOf).join());
        assert.ok(same2(accountsOfStep(gs[0]), [keys[0], keys[1], keys[2]]), 'P, Q and R are one step, the accounts of all, once each');
        assert.ok(same2(accountsOfStep(gs[1]), [keys[3]]), 'S stands alone between the two glued steps');
        assert.ok(same2(accountsOfStep(gs[2]), [keys[4], keys[5]]) && same2(accountsOfStep(gs[3]), [keys[6]]), 'T and U are one step, V is its own');
        assert.deepEqual(gs.map(amountOf), ['+2.30', '+0.10', '+1.10', '+0.90'], 'what they give back is added: ' + gs.map(amountOf).join());
        assert.ok(gs.every((s) => dotsOf(s).length === 1) && gs.map((s) => s.style['--i']).join() === '1,2,3,4', 'the markup of a glued step is the markup of any step');
        assert.ok(has(moreOf(glue), 'rail-more-void') || has(moreOf(glue), 'rail-fit-off'), 'four steps stand, nothing is counted over them (the word is not shown)');
        // the title and the spoken label: the first moment, an en dash, the last moment; the key of the words is the old one (the phrase after it is the same)
        const whenOf = (step) => step.getAttribute('aria-label').split(' \u2014 ')[0];
        const alone = async (r) => { const e = await boot(reshape(ten, [r])); const w = whenOf(stepsOf(e)[0]); done(e); return w; };
        const wP = await alone({ left: .5, in: 3600 }), wR = await alone({ left: 0, in: 6000 }), wT = await alone({ left: .6, in: 10800 }), wU = await alone({ left: .3, in: 11400 });
        assert.ok(whenOf(gs[0]) === wP + ' \u2013 ' + wR, 'the first moment is the earliest, the last is the latest: ' + whenOf(gs[0]) + ' | ' + wP + ' | ' + wR);
        assert.ok(whenOf(gs[2]) === wT + ' \u2013 ' + wU, 'T and U: ' + whenOf(gs[2]));
        assert.ok(/^[A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2} \d\d:\d\d \u2013 [A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2} \d\d:\d\d \u2014 3 accounts: .+ \u00b7 returns \+2\.30 if unused$/.test(gs[0].getAttribute('aria-label')), gs[0].getAttribute('aria-label'));
        assert.ok(gs[0].title === gs[0].getAttribute('aria-label') && gs[2].title === gs[2].getAttribute('aria-label'), 'the title and the spoken label are one phrase');
        assert.ok(whenOf(gs[1]).indexOf('\u2013') < 0 && whenOf(gs[3]).indexOf('\u2013') < 0, 'a step that glued nothing says one moment');
        assert.ok(/ \u2014 1 account: /.test(gs[1].getAttribute('aria-label')) && /returns \+0\.10 if unused$/.test(gs[1].getAttribute('aria-label')));
        done(glue);
        // the moments of the cluster of 120 s do not make a range: one step of two accounts a minute apart says one moment
        const near = await boot(reshape(ten, [{ left: .5, in: 3600 }, { left: .5, in: 3660 }]));
        assert.ok(stepsOf(near).length === 1 && whenOf(stepsOf(near)[0]).indexOf('\u2013') < 0 && accountsOfStep(stepsOf(near)[0]).length === 2, 'a cluster is no range');
        done(near);
        // five steps, two of them glued: four stand and the word counts the one over them (the glued steps are the steps of the window)
        const five = await boot(reshape(ten, [{ left: .5, in: 3600 }, { left: .5, in: 4500 }, { left: .5, in: 7200 }, { left: .5, in: 10800 }, { left: .5, in: 14400 }, { left: .5, in: 18000 }]));
        assert.ok(allStepsOf(five).length === 5 && stepsOf(five).length === 4 && text(moreOf(five)) === '1 more' && !has(moreOf(five), 'rail-more-void'), 'six resets, five steps: four stand and "1 more": ' + allStepsOf(five).length + ' ' + text(moreOf(five)));
        assert.ok(accountsOfStep(stepsOf(five)[0]).length === 2 && moreOf(five).style['--i'] === '5', 'the first step has two accounts');
        done(five);
      }
      // 2. The data of the stand: the first step is the plate's row of the next reset: the same moment, and the amount and the accounts no less (the first step may be a glued one, wider than the row).
      for (const key of ['claude_five', 'codex_states', 'reset_close', 'claude_single', 'claude_many', 'claude', 'codex']) {
        const e = await boot(views[key]);
        const plate = plateOf(e);
        const cells = cellsOf(plate);
        const first = stepsOf(e)[0];
        if (!first || !nxOf(plate) || !/^next reset /.test(cells[0])) { done(e); continue; }
        const when = cells[0].replace(/^next reset /, '');
        const label = first.getAttribute('aria-label');
        assert.ok(label.indexOf(when + ' \u2014 ') === 0 || label.indexOf(when + ' \u2013 ') === 0, key + ': the time of the first step is the plate\'s: ' + when + ' | ' + label);
        const back = /\+(\d+\.\d\d) if unused/.exec(cells.join(' '));
        if (back) assert.ok(Number(amountOf(first).slice(1)) >= Number(back[1]) - 1e-9, key + ': the amount is the plate\'s or more: ' + back[1] + ' | ' + amountOf(first));
        const n = /(\d+) accounts/.exec(cells.join(' '));
        assert.ok(accountsOfStep(first).length >= (n ? Number(n[1]) : 1), key + ': the accounts are the plate\'s or more');
        assert.ok(has(nxOf(plate), 'nx-in-ladder'), key + ': the plate\'s row wears the mark');
        done(e);
      }
      // 3. Ten steps: four are drawn, and the word counts the six over them with no layout asked.
      const many = await boot(spread);
      assert.ok(stepsOf(many).length === 4 && text(moreOf(many)) === '6 more' && !has(moreOf(many), 'rail-more-void'), 'four steps and "6 more": ' + stepsOf(many).length + ' ' + text(moreOf(many)));
      assert.ok(moreOf(many).style['--i'] === '5', 'and the word is the fifth row, after the fourth step');
      assert.ok(moreOf(many).getAttribute('aria-label') === '6 more resets \u2014 show them below' && allStepsOf(many).length === 10, 'the label counts the same; all ten are in the DOM');
      done(many);
      // 4. No reset to come (all past, or none told): the head and one line, "not reported", whose title is the full phrase of the plate.
      for (const rowsNone of [ten.reserve.summary.groups[0].bars.map(() => ({ left: .5, in: null })), ten.reserve.summary.groups[0].bars.map(() => ({ left: .5, in: -300 }))]) {
        const none = await boot(reshape(ten, rowsNone));
        const lines = stepsOf(none);
        assert.ok(lines.length === 1 && !stepTab(lines[0]) && text(lines[0]) === 'not reported' && lines[0].title === 'no reset time reported', 'not reported');
        assert.ok(text(titleOf(none)) === 'Resets' && moreOf(none) && (has(moreOf(none), 'rail-more-void') || has(moreOf(none), 'rail-fit-off')), 'the head, and no word that counts (void or off: it is not shown)');
        assert.ok(classes(none.root, 'rail-step-dot').length === 0 && classes(none.root, 'rail-step-time').length === 0, 'and no time, no point: nothing is told');
        done(none);
      }
      // 5. Cursor with a budget per billing cycle has no window of bars: no ladder, and its plate is not marked; one that reports nothing has no rail.
      for (const key of ['cursor_budget', 'cursor_mixed', 'cursor_bare', 'cursor']) {
        const e = await boot(views[key]);
        assert.ok(classes(e.root, 'rail-ladder-zone').length === 0 && classes(e.root, 'rail-step').length === 0 && classes(e.root, 'nx-in-ladder').length === 0, key + ': no ladder');
        done(e);
      }
      // 7a. In how long, at the edges of the units (30 s over each, see iso): under a minute "<1m", the minutes under an hour, the hours alone under a day, then days and hours.
      const edges = [[0, '<1m'], [40 * 60, '40m'], [59 * 60, '59m'], [3600, '1h'], [5 * 3600, '5h'], [23 * 3600 + 59 * 60, '23h'], [86400, '1d'], [86400 + 3600, '1d 1h'],
        [47 * 3600 + 59 * 60, '1d 23h'], [2 * 86400, '2d'], [2 * 86400 + 5 * 3600, '2d 5h'], [10 * 86400 + 23 * 3600, '10d 23h']];
      for (const [sec, want] of edges) {
        const e = await boot(reshape(ten, [{ left: .5, in: sec }]));
        assert.deepEqual(stepsOf(e).map(timeOf), [want], sec + ' s and a half minute is ' + want + ': ' + stepsOf(e).map(timeOf).join());
        done(e);
      }
      // 7b. The clock is the draw: the 30 s poll says it again from Date.now() (no timer of the step's own), and a step whose moment has passed goes at the next draw.
      {
        const e = await boot(reshape(ten, [{ left: .5, in: 3 * 3600 }, { left: .5, in: 5 * 3600 }]));
        assert.deepEqual(stepsOf(e).map(timeOf), ['3h', '5h']);
        const realNow = Date.now;
        try {
          Date.now = () => realNow() + 2 * 3600 * 1000;
          e.interval()(); await settle();
          assert.deepEqual(stepsOf(e).map(timeOf), ['1h', '3h'], 'two hours later the poll says 1h and 3h: ' + stepsOf(e).map(timeOf).join());
          Date.now = () => realNow() + 4 * 3600 * 1000;
          e.interval()(); await settle();
          assert.deepEqual(stepsOf(e).map(timeOf), ['1h'], 'the step whose moment passed is gone at the next draw: ' + stepsOf(e).map(timeOf).join());
          Date.now = () => realNow() + 7 * 3600 * 1000;
          e.interval()(); await settle();
          assert.equal(stepsOf(e).length, 1); assert.equal(text(stepsOf(e)[0]), 'not reported', 'and with none left the ladder says so');
        } finally {
          Date.now = realNow;
        }
        done(e);
      }
      // 6. A step lights the bars of its accounts on the plate under the pointer, the keyboard and a finger (the mechanism of the row of the next reset), and the
      // keyboard stays on it through the redraw of fresh data.
      const probe = await boot(views.claude_five);
      const proto = Object.getPrototypeOf(plateOf(probe));
      if (!proto.removeAttribute) proto.removeAttribute = function (name) { delete this.attributes[name]; };
      if (!proto.matches) proto.matches = function (selector) { return selector === ':focus-visible' && this.ownerDocument.activeElement === this; };
      done(probe);
      const env2 = await boot(views.claude_five);
      const plate = plateOf(env2);
      const second = stepsOf(env2)[1];
      const lit = () => stripBars(plate).filter((b) => b.getAttribute('data-lit') !== null).map((b) => b.getAttribute('data-account'));
      const who = accountsOfStep(second);
      assert.ok(['mouseenter', 'mouseleave', 'focus', 'blur', 'pointerdown', 'pointerup', 'pointercancel'].every((e) => second.listeners[e] && second.listeners[e].length === 1), 'hover, focus and touch are one thing');
      second.listeners.mouseenter[0]();
      assert.ok(plate.getAttribute('data-dim') === 'group' && JSON.stringify(lit().sort()) === JSON.stringify([...who].sort()), 'the pointer lights the bars of the step\'s accounts');
      second.listeners.mouseleave[0]();
      assert.ok(plate.getAttribute('data-dim') === null && lit().length === 0, 'and lets go');
      second.focus();
      second.listeners.focus[0]();
      assert.ok(plate.getAttribute('data-dim') === 'group' && lit().length === who.length, 'the keyboard lights them');
      second.listeners.blur[0]();
      assert.ok(plate.getAttribute('data-dim') === null && lit().length === 0);
      second.listeners.pointerdown[0]({ pointerType: 'touch' });
      assert.ok(plate.getAttribute('data-dim') === 'group' && lit().length === who.length, 'a finger lights them');
      second.listeners.pointerup[0]({ pointerType: 'touch' });
      assert.ok(plate.getAttribute('data-dim') === null);
      second.focus();
      const focusKey = second.getAttribute('data-focus');
      env2.interval()(); await settle();
      assert.ok(env2.document.activeElement.getAttribute('data-focus') === focusKey && has(env2.document.activeElement, 'rail-step'), 'the keyboard stays on the step through the redraw');
      done(env2);
    } else if (mode === 'ladder_fit') {
      // The fit: one read of the layout at the end of a drawing says how many steps stand whole in the room between the last ring and the foot (the head 36px, a
      // step 66px, the word 30px); the others are taken off and "N more" counts them; where no step stands the plate keeps its own row. The fake page has no
      // layout, so each node is given its height the way the stylesheet gives it, and the drawing is made again (the poll).
      const layout = (env, room) => {
        const make = env.document.createElement;
        env.document.createElement = (tag) => {
          const n = make(tag);
          const c = () => String(n.className || '');
          Object.defineProperty(n, 'clientHeight', { configurable: true, get() { return /(^| )rail-ladder-zone( |$)/.test(c()) ? room : 0; } });
          Object.defineProperty(n, 'offsetHeight', { configurable: true, get() {
            return /(^| )rail-ladder-title( |$)/.test(c()) ? 36 : /(^| )rail-step( |$)/.test(c()) ? 66 : /(^| )rail-more( |$)/.test(c()) ? 30 : 0; } });
          return n;
        };
      };
      const shape = async (env, room) => {
        layout(env, room);
        env.interval()(); await settle();
        const steps = stepsOf(env);
        return { drawn: steps.length, all: allStepsOf(env).length, head: !has(titleOf(env), 'rail-fit-off'),
          more: has(moreOf(env), 'rail-fit-off') ? '' : text(moreOf(env)), mark: has(lineOf(env), 'nx-in-ladder'), void: has(moreOf(env), 'rail-more-void'), at: moreOf(env).style['--i'] };
      };
      const cases = [
        ['spread', 400, { drawn: 4, head: true, more: '6 more', mark: true }],
        ['spread', 300, { drawn: 3, head: true, more: '7 more', mark: true }],
        ['spread', 200, { drawn: 2, head: true, more: '8 more', mark: true }],
        // (64px under the head: the word would stand, but the window it opens would hold no whole step, so the press would do nothing: neither the word nor the head is drawn)
        ['spread', 100, { drawn: 0, head: false, more: '', mark: false }],
        ['spread', 40, { drawn: 0, head: false, more: '', mark: false }],
        ['spread', 20, { drawn: 0, head: false, more: '', mark: false }],
        ['reset_close', 400, { drawn: 2, head: true, more: '', mark: true }],
        ['reset_close', 80, { drawn: 0, head: false, more: '', mark: false }]];
      for (const [key, room, want] of cases) {
        const env = await boot(key === 'spread' ? spread : views[key]);
        const got = await shape(env, room);
        assert.ok(got.drawn <= 4 && got.all >= got.drawn, key + ': never more than four steps are drawn (the rest are in the DOM, off)');
        assert.ok(got.drawn === want.drawn && got.head === want.head && got.more === want.more && got.mark === want.mark && !got.void,
          key + ' at ' + room + 'px: ' + JSON.stringify(got) + ' wanted ' + JSON.stringify(want));
        // The word comes down after the last step that stands: its number is the head's 0 and the drawn steps, then one.
        if (got.more) assert.ok(got.at === String(got.drawn + 1), key + ' at ' + room + 'px: the word comes after the last step that stands: ' + got.at + ' after ' + got.drawn);
        if (got.more) assert.ok(/more resets? \u2014 show (it|them) below$/.test(moreOf(env).getAttribute('aria-label')) && moreOf(env).getAttribute('aria-label').indexOf(got.more.split(' ')[0]) === 0, key + ': the label counts the same');
        done(env);
      }
      // The word counts every step that does not stand (all less those that do), not only those over four, and where it is drawn its press opens the window: three resets, room
      // for two of them and the word: "1 more", and the press brings the third (the window holds all three, 134px, "less").
      const three = reshape(ten, [0, 1, 2].map((i) => ({ left: .5, in: 3600 + i * 7200 })));
      const few = await boot(three);
      const gotFew = await shape(few, 200);
      assert.ok(gotFew.drawn === 2 && gotFew.all === 3 && gotFew.more === '1 more' && gotFew.mark, 'three resets, room for two: ' + JSON.stringify(gotFew));
      moreOf(few).listeners.click[0]({ stopPropagation() {} }); await settle();
      assert.ok(moreOf(few).getAttribute('aria-expanded') === 'true' && text(moreOf(few)) === 'less' && stepsOf(few).length === 3 && classes(few.root, 'rail-scroll')[0].style.height === '134px',
        'the word of a ladder of three opens it: ' + text(moreOf(few)) + ' ' + stepsOf(few).length + ' ' + classes(few.root, 'rail-scroll')[0].style.height);
      done(few);
      // "Not reported" stands as one step: where it stands whole the plate's row gives way; where it does not, the plate keeps it.
      const nothing = reshape(ten, ten.reserve.summary.groups[0].bars.map(() => ({ left: .5, in: null })));
      for (const [room, drawn, mark] of [[110, 1, true], [50, 0, false]]) {
        const env = await boot(nothing);
        const got = await shape(env, room);
        assert.ok(got.drawn === drawn && got.mark === mark && got.more === '', 'not reported at ' + room + 'px: ' + JSON.stringify(got));
        done(env);
      }
      // With no layout to ask (a bare document) nothing is taken off and nothing is invented.
      const bare = await boot(views.reset_close);
      assert.ok(allStepsOf(bare).length === 2 && stepsOf(bare).length === 2 && (has(moreOf(bare), 'rail-more-void') || has(moreOf(bare), 'rail-fit-off')), 'no layout: every step stands, the word is not shown (it has nothing to count)');
      done(bare);
    } else if (mode === 'ladder_open') {
      // The open ladder, as the fake page can tell it (the layout is a browser's: the nodes are given the heights the stylesheet gives them, the zone 'room' high, the head
      // 36, a step 66, the word 30, and a scroll node keeps scrollTop inside its content). The window: min(5.5 steps, the room under the head and over the word), whole pixels, set as the
      // height of .rail-scroll; the haze classes follow the scroll and change only where an edge is reached or left; the place of the scroll and the open ladder outlive the poll's
      // draw (the new node gets them); a pick of another window or of another family folds it; the steps that come into sight at the press come down (rail-step-in, --j, three at most).
      const room = { v: 498 };
      // (how often the draws touch the node of the window: reads and writes of its scrollTop, and the words of the word written over the same words)
      const io = { get: 0, set: 0, by: new Map() };
      const layoutOpen = (env) => {
        const make = env.document.createElement;
        env.document.createElement = (tag) => {
          const n = make(tag);
          const c = () => String(n.className || '');
          Object.defineProperty(n, 'clientHeight', { configurable: true, get() { return /(^| )rail-ladder-zone( |$)/.test(c()) ? room.v : 0; } });
          Object.defineProperty(n, 'offsetHeight', { configurable: true, get() {
            return /(^| )rail-ladder-title( |$)/.test(c()) ? 36 : /(^| )rail-step( |$)/.test(c()) ? 66 : /(^| )rail-more( |$)/.test(c()) ? 30 : 0; } });
          let top = 0;
          Object.defineProperty(n, 'scrollTop', { configurable: true, get() { io.get++; return top; }, set(v) { io.set++; io.by.set(n, (io.by.get(n) || 0) + 1); const h = parseFloat(n.style && n.style.height) || 0; top = Math.max(0, Math.min(v, Math.max(0, 660 - h))); } });
          const own = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(n), 'textContent');
          if (own && own.set) {
            n.same = 0;
            Object.defineProperty(n, 'textContent', { configurable: true, get() { return own.get.call(n); }, set(v) { if (own.get.call(n) === v) n.same++; own.set.call(n, v); } });
          }
          return n;
        };
      };
      let stage = '1';
      const live = (env) => { const l = classes(env.root, 'rail-ladder').find((n) => !has(n, 'rail-ladder-out')); if (!l) throw new Error('stage ' + stage + ': no ladder is drawn (' + classes(env.root, 'rail-ladder').length + ' with the fading ones)'); return l; };
      const scrollOf = (env) => classes(live(env), 'rail-scroll')[0];
      const wordOf = (env) => classes(live(env), 'rail-more')[0];
      const standing = (env) => classes(live(env), 'rail-step').filter((n) => !has(n, 'rail-fit-off'));
      const press = async (env) => { wordOf(env).listeners.click[0]({ stopPropagation() {} }); await settle(); };
      const scrollTo = (env, top) => { scrollOf(env).scrollTop = top; scrollOf(env).listeners.scroll[0](); };
      const prepared = async (view) => { const env = await boot(view); layoutOpen(env); env.interval()(); await settle(); return env; };
      const edges = (env) => [has(scrollOf(env), 'rail-scroll-start'), has(scrollOf(env), 'rail-scroll-end')].join();
      // 1. Folded: four steps, 'N more'. The press: the window 363px (5.5 steps), every step stands, the haze under the end only.
      const env = await prepared(spread);
      assert.ok(standing(env).length === 4 && text(wordOf(env)) === '6 more' && wordOf(env).getAttribute('aria-expanded') === 'false' && !has(scrollOf(env), 'rail-scroll-on'), 'folded: four steps and "6 more"');
      assert.ok(scrollOf(env).style.height === '' || scrollOf(env).style.height === undefined, 'folded: the window has no height of its own');
      await press(env);
      assert.ok(standing(env).length === 10 && text(wordOf(env)) === 'less' && wordOf(env).getAttribute('aria-expanded') === 'true' && has(scrollOf(env), 'rail-scroll-on'), 'open: ten steps, "less"');
      assert.ok(scrollOf(env).style.height === '363px', 'the window is 5.5 steps: ' + scrollOf(env).style.height);
      assert.ok(edges(env) === 'true,false', 'at the start: no haze over it, one under: ' + edges(env));
      // the steps that come into sight come down: three at most, numbered from 0, only at the press
      const inn = classes(live(env), 'rail-step').filter((n) => has(n, 'rail-step-in'));
      assert.ok(inn.length === 3 && inn.map((n) => n.style['--j']).join() === '0,1,2' && inn[0] === classes(live(env), 'rail-step')[4], 'the three past the fourth come down, one after another: ' + inn.length);
      // 2. The haze follows the scroll, at the edges only.
      stage = '2';
      scrollTo(env, 120);
      assert.ok(edges(env) === 'false,false', 'in the middle: haze on both sides: ' + edges(env));
      scrollTo(env, 1e6);
      assert.ok(scrollOf(env).scrollTop === 297 && edges(env) === 'false,true', 'at the end (297px): no haze under it: ' + scrollOf(env).scrollTop + ' ' + edges(env));
      scrollTo(env, 0);
      assert.ok(edges(env) === 'true,false', 'and back at the start');
      // 3. The poll draws again: the ladder stands open in a new node that has the place of the scroll, with no step coming down.
      scrollTo(env, 150);
      const was = scrollOf(env);
      env.interval()(); await settle();
      assert.ok(scrollOf(env) !== was, 'a new node (the draw)');
      assert.ok(wordOf(env).getAttribute('aria-expanded') === 'true' && text(wordOf(env)) === 'less' && standing(env).length === 10 && has(scrollOf(env), 'rail-scroll-on'), 'open across the poll');
      assert.ok(scrollOf(env).scrollTop === 150 && scrollOf(env).style.height === '363px' && edges(env) === 'false,false', 'the place of the scroll across the poll: ' + scrollOf(env).scrollTop + ' ' + edges(env));
      assert.ok(classes(live(env), 'rail-step-in').length === 0, 'and nothing comes down at the poll');
      // 4. The room: never higher than what is left under the head and over the word; where not even one step stands, folded (the state is kept).
      room.v = 300; env.interval()(); await settle();
      assert.ok(scrollOf(env).style.height === '234px' && wordOf(env).getAttribute('aria-expanded') === 'true', 'less room, a lower window: ' + scrollOf(env).style.height);
      room.v = 120; env.interval()(); await settle();
      assert.ok(wordOf(env).getAttribute('aria-expanded') === 'false' && !has(scrollOf(env), 'rail-scroll-on') && standing(env).length < 2, 'no room for a step in a window: folded');
      assert.ok(has(wordOf(env), 'rail-fit-off'), 'and no word that could not open it');
      room.v = 498; env.interval()(); await settle();
      assert.ok(wordOf(env).getAttribute('aria-expanded') === 'false' && !has(scrollOf(env), 'rail-scroll-on') && standing(env).length === 4 && text(wordOf(env)) === '6 more', 'the room is back: it stays folded, it does not open by itself');
      await press(env);
      assert.ok(wordOf(env).getAttribute('aria-expanded') === 'true' && scrollOf(env).style.height === '363px' && scrollOf(env).scrollTop === 0, 'and the press opens it, at the start');
      // 5. The word again folds it to four steps and the start of the scroll; opened again it starts at the start.
      scrollTo(env, 200);
      await press(env);
      assert.ok(wordOf(env).getAttribute('aria-expanded') === 'false' && text(wordOf(env)) === '6 more' && standing(env).length === 4 && scrollOf(env).scrollTop === 0 && classes(live(env), 'rail-step-in').length === 0, 'folded: four steps, the start');
      await press(env);
      assert.ok(scrollOf(env).scrollTop === 0 && edges(env) === 'true,false', 'opened again: at the start');
      done(env);
      // 6. Reduced motion: the press brings nothing down.
      stage = '6';
      const calm = await prepared(spread);
      calm.window.matchMedia = (query) => ({ matches: /reduced-motion/.test(String(query)) });   // (the narrow shell has no ladder: only the calm query answers yes)
      await press(calm);
      assert.ok(wordOf(calm).getAttribute('aria-expanded') === 'true' && standing(calm).length === 10 && classes(live(calm), 'rail-step-in').length === 0, 'reduced motion: open, nothing comes down');
      done(calm);
      // 7. Every window of Claude with ten resets: the pick of another window folds the ladder and starts it at the start.
      stage = '7';
      const ladders = (view) => { const v = JSON.parse(JSON.stringify(view)); v.reserve.summary.groups.forEach((g) => { if (g.bars) g.bars = JSON.parse(JSON.stringify(spread.reserve.summary.groups[0].bars)); }); return v; };
      const wide = await prepared(ladders(views.claude));
      assert.ok(tabsOf(wide).length >= 2, 'Claude has windows to pick');
      await press(wide);
      scrollTo(wide, 100);
      assert.ok(wordOf(wide).getAttribute('aria-expanded') === 'true' && scrollOf(wide).scrollTop === 100, 'open, scrolled');
      click(tabsOf(wide).find((t) => t.getAttribute('aria-selected') !== 'true'));
      await settle();
      assert.ok(wordOf(wide).getAttribute('aria-expanded') === 'false' && standing(wide).length === 4 && scrollOf(wide).scrollTop === 0 && !has(scrollOf(wide), 'rail-scroll-on'), 'another window: folded, at the start');
      done(wide);
      // 8. The pick of another family folds it (and the way back finds it folded).
      stage = '8';
      const two = ladders(Object.assign({}, views.reading_both, { groups: [views.codex.groups[0], views.reading_both.groups[0]] }));
      const fam = await prepared(two);
      click(byFocus(fam.root, 'harness:claude'));
      await settle();
      await press(fam);
      scrollTo(fam, 100);
      assert.ok(wordOf(fam).getAttribute('aria-expanded') === 'true' && scrollOf(fam).scrollTop === 100, 'open on Claude, scrolled');
      click(byFocus(fam.root, 'harness:codex'));
      await settle();
      assert.ok(classes(fam.root, 'rail-ladder').filter((n) => !has(n, 'rail-ladder-out')).every((l) => classes(l, 'rail-more').every((m) => m.getAttribute('aria-expanded') !== 'true')), 'another family: not open');
      click(byFocus(fam.root, 'harness:claude'));
      await settle();
      assert.ok(wordOf(fam).getAttribute('aria-expanded') === 'false' && scrollOf(fam).scrollTop === 0 && standing(fam).length === 4 && !has(scrollOf(fam), 'rail-scroll-on'), 'and back: folded, at the start');
      done(fam);
      // 9. The limit drawn gives way to another with no pick (the reader hid its row in Rows): the open state is the pressed limit's, so the other stands folded, and when the
      // first is shown again it does not open by itself.
      stage = '9';
      let current = ladders(views.claude);
      const keyed = await boot(() => current);
      layoutOpen(keyed); keyed.interval()(); await settle();
      await press(keyed);
      scrollTo(keyed, 100);
      const keyOf = (env) => /^rs:(.*)@[^@]*$/.exec(standing(env)[0].getAttribute('data-focus'))[1];
      const was9 = keyOf(keyed);
      assert.ok(wordOf(keyed).getAttribute('aria-expanded') === 'true' && scrollOf(keyed).scrollTop === 100, 'open on the limit picked, scrolled');
      current = JSON.parse(JSON.stringify(current));
      current.display = { hidden: [was9] };
      keyed.interval()(); await settle();
      assert.ok(keyOf(keyed) !== was9, 'another limit is drawn: ' + keyOf(keyed));
      assert.ok(wordOf(keyed).getAttribute('aria-expanded') === 'false' && scrollOf(keyed).scrollTop === 0 && standing(keyed).length === 4 && !has(scrollOf(keyed), 'rail-scroll-on'), 'and it stands folded, at the start');
      current = JSON.parse(JSON.stringify(current));
      current.display = { hidden: [] };
      keyed.interval()(); await settle();
      assert.ok(wordOf(keyed).getAttribute('aria-expanded') === 'false' && !has(scrollOf(keyed), 'rail-scroll-on'), 'the first limit shown again: still folded');
      done(keyed);
      // 10. Nothing measured (the shell narrowed under 860px and widened again) keeps the open ladder and the place of its scroll.
      stage = '10';
      const narrow = await prepared(spread);
      await press(narrow);
      scrollTo(narrow, 150);
      narrow.window.matchMedia = (query) => ({ matches: /max-width: 859\.98px/.test(String(query)) });
      narrow.interval()(); await settle();
      narrow.window.matchMedia = undefined;
      narrow.interval()(); await settle();
      assert.ok(wordOf(narrow).getAttribute('aria-expanded') === 'true' && scrollOf(narrow).scrollTop === 150 && scrollOf(narrow).style.height === '363px', 'back on the wide shell: open, at the place it was: ' + scrollOf(narrow).scrollTop);
      done(narrow);
      // 11. The draw touches the node of the window as little as it can: folded, it neither reads nor writes the scroll; open at the start, it writes none; open further down it writes
      // the place once and never reads it back; and the word is not written over the same words.
      stage = '11';
      const quiet = await prepared(spread);
      io.get = 0; io.set = 0;
      quiet.interval()(); await settle();
      assert.ok(io.get === 0 && io.set === 0, 'folded: the draw reads and writes no scroll: ' + io.get + '/' + io.set);
      assert.ok(wordOf(quiet).same === 0, 'folded: "6 more" is not written over itself: ' + wordOf(quiet).same);
      await press(quiet);
      io.get = 0; io.set = 0;
      quiet.interval()(); await settle();
      assert.ok(io.get === 0 && io.set === 0, 'open at the start: no read, no write: ' + io.get + '/' + io.set);
      assert.ok(wordOf(quiet).same === 0, 'open: "less" is not written over itself: ' + wordOf(quiet).same);
      scrollTo(quiet, 150);
      io.get = 0; io.set = 0; io.by.clear();
      quiet.interval()(); await settle();
      // (a poll may draw more than once; each node of the window is given the place once)
      assert.ok(io.get === 0 && io.by.size >= 1 && [...io.by.values()].every((c) => c === 1), 'open further down: the place is written once to a node and not read back: ' + io.get + '/' + io.set);
      assert.ok(scrollOf(quiet).scrollTop === 150, 'and it is the place');
      done(quiet);
    } else {
      // The movement. The reader's pick of a window: the steps that stood fade (the old ladder's node, aria-hidden and inert, stands over the new one at
      // opacity 0), the new ones come down (.rail-ladder-moving, numbered from the head); a drawing inside the span goes on with it and has no old node; the
      // poll after it moves nothing. The pick of a family: the head and the steps come down after the rings by the rail's own movement (--i counts on from
      // them, --rail-rows counts them all), and the ladder has none of its own. Under reduced motion neither.
      // Codex (a week and 30 days) is the family picked at first; Claude is the other.
      const merged = Object.assign({}, views.reading_both, { groups: [views.codex.groups[0], views.reading_both.groups[0]] });
      const env = await boot(views.reading_two);
      const ladderOf2 = (e) => classes(e.root, 'rail-ladder').find((n) => !has(n, 'rail-ladder-out'));
      const railOf2 = (e) => classes(e.root, 'rail')[0];
      assert.ok(ladderOf2(env) && !has(ladderOf2(env), 'rail-ladder-moving') && classes(env.root, 'rail-ladder-out').length === 0, 'the first drawing moves nothing: ' + (ladderOf2(env) && ladderOf2(env).className) + ' ' + classes(env.root, 'rail-ladder').length + ' ' + classes(env.root, 'rail-ladder-out').length);
      click(tabsOf(env).find((t) => t.getAttribute('aria-selected') !== 'true'));
      await settle();
      assert.ok(has(ladderOf2(env), 'rail-ladder-moving') && !has(railOf2(env), 'rail-moving'), 'a window picked: the ladder moves, the rail does not');
      assert.ok(titleOf(env).style['--i'] === '0' && stepsOf(env)[0].style['--i'] === '1', 'from the head');
      assert.ok(moreOf(env).style['--i'] === String(stepsOf(env).length + 1) && ladderOf2(env).childNodes[ladderOf2(env).childNodes.length - 1] === moreOf(env), 'and the word that counts last, after the last step');
      const out = classes(env.root, 'rail-ladder-out');
      assert.ok(out.length === 1 && out[0].getAttribute('aria-hidden') === 'true' && out[0].getAttribute('inert') !== null && has(out[0].parentNode, 'rail-ladder-zone'),
        'the steps that stood are kept to fade, out of the keyboard and the reader');
      assert.ok(stepsOf(env).every((s) => !has(s.parentNode.parentNode, 'rail-ladder-out')) && classes(env.root, 'rail-step').filter((n) => n.getAttribute('tabindex') === '0' && has(n.parentNode.parentNode, 'rail-ladder-out')).length >= 0);
      await wait(40);
      env.interval()(); await settle();
      assert.ok(has(ladderOf2(env), 'rail-ladder-moving') && classes(env.root, 'rail-ladder-out').length === 1, 'a drawing inside the span goes on with the movement and keeps the old steps over it');
      await wait(850);
      assert.ok(classes(env.root, 'rail-ladder-out').length === 0, 'the old steps leave the page at the end of the span, with no drawing to take them');
      env.interval()(); await settle();
      assert.ok(!has(ladderOf2(env), 'rail-ladder-moving') && classes(env.root, 'rail-ladder-out').length === 0 && !has(railOf2(env), 'rail-moving'), 'the poll after the span moves nothing');
      click(tabsOf(env).find((t) => t.getAttribute('aria-selected') === 'true'));
      await settle();
      assert.ok(!has(ladderOf2(env), 'rail-ladder-moving'), 'the window that stands, pressed again, moves nothing');
      done(env);
      const fam = await boot(merged);
      click(byFocus(fam.root, 'harness:claude'));
      await settle();
      const rings = tabsOf(fam).length;
      assert.ok(has(railOf2(fam), 'rail-moving') && !has(ladderOf2(fam), 'rail-ladder-moving') && classes(fam.root, 'rail-ladder-out').length === 0, 'a family picked: the rail\'s own movement');
      assert.ok(titleOf(fam).style['--i'] === String(rings) && stepsOf(fam)[0].style['--i'] === String(rings + 1), 'the head and the steps are numbered on from the rings');
      assert.ok(railOf2(fam).style['--rail-rows'] === String(rings + 1 + stepsOf(fam).length), 'and the foot comes after them');
      assert.ok(moreOf(fam).style['--i'] === String(rings + 1 + stepsOf(fam).length) && Number(moreOf(fam).style['--i']) <= Number(railOf2(fam).style['--rail-rows']), 'the word that counts comes down last of the ladder, and the rows do not grow for it');
      await wait(1350);
      fam.interval()(); await settle();
      assert.ok(!has(railOf2(fam), 'rail-moving') && !has(ladderOf2(fam), 'rail-ladder-moving'), 'the poll moves nothing');
      done(fam);
      const calm = await boot(views.reading_two);
      calm.window.matchMedia = () => ({ matches: true });
      click(tabsOf(calm).find((t) => t.getAttribute('aria-selected') !== 'true'));
      await settle();
      assert.ok(classes(calm.root, 'rail-ladder-moving').length === 0 && classes(calm.root, 'rail-ladder-out').length === 0, 'under reduced motion: at once');
      done(calm);
      const calmFam = await boot(merged);
      calmFam.window.matchMedia = () => ({ matches: true });
      click(byFocus(calmFam.root, 'harness:claude'));
      await settle();
      assert.ok(classes(calmFam.root, 'rail-moving').length === 0 && classes(calmFam.root, 'rail-ladder-moving').length === 0, 'and on a family');
      done(calmFam);
    }
  }
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exitCode = 1; });
"""


def _run(case, fixture_file):
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    # The widget looks for a ResizeObserver in its own world: give the fake one to the harness's context.
    # The sandbox context now ends with the setTimeout/clearTimeout pair, so the hook anchors on the last entry.
    hooked = harness.replace("    clearTimeout: (id) => window.clearTimeout(id),\n  });",
                             "    clearTimeout: (id) => window.clearTimeout(id),\n    ResizeObserver: globalThis.FakeRO,\n  });")
    assert hooked != harness, "harness hook for ResizeObserver failed"
    # The window is handed to the test, so a case can put its own fetch there and hold a request in the air.
    exposed = hooked.replace("    root: made.root,", "    window,\n    root: made.root,", 1)
    assert exposed != hooked, "harness hook for the window failed"
    hooked = exposed
    # The fake node has no nextSibling, a real node has: the widget places the chart right under the bars of the plate by `bars.nextSibling` (insertBefore with none appends), so
    # without it the fake plate would hold the chart last and the order of the keyboard would be tested on a layout no browser draws. The same getter as the real one.
    siblinged = hooked.replace("  get firstChild() { return this.childNodes[0] || null; }",
                               "  get firstChild() { return this.childNodes[0] || null; }\n"
                               "  get nextSibling() { const up = this.parentNode; return up ? (up.childNodes[up.childNodes.indexOf(this) + 1] || null) : null; }", 1)
    assert siblinged != hooked, "harness hook for nextSibling failed"
    hooked = siblinged
    widget = Path(os.environ.get("UI_WIDGET_PATH", Path(__file__).with_name("widget.js"))).resolve()
    result = subprocess.run([str(node), "-e", hooked + NODE_TABS], cwd=Path(__file__).parent,
                            env={**os.environ, "WIDGET_PATH": str(widget), "TABS_CASE": case,
                                 "TABS_FIXTURE_FILE": fixture_file},
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("case", ["places", "average", "scopes", "graph", "motion", "no_ink", "reading", "foot", "failed", "hold",
                                  "hold_zero", "stars", "old", "none", "stamp_dot", "cursor_budget", "cursor_mixed", "cursor_kept", "cursor_bare",
                                  "legend_codex", "legend_claude", "legend_single", "legend_many", "legend_reasons",
                                  "fact_bubble", "limit_bubble", "limit_hover", "credits_head",
                                  "legend_agy", "chart_word", "one_red", "exceptions_in_words", "reset_handle", "about_words", "rail", "family_pick", "hidden_row",
                                  "family_mru", "family_ride", "hover_hold", "yield_from", "ring_sweep", "clock_start", "foot_motion", "lead_on", "chart_wait", "compact",
                                  "ladder", "ladder_fit", "ladder_open", "ladder_motion"])
def test_window_row(fixture_file, case):
    _run(case, fixture_file)


def _style_rules(css=None):
    """The plain rules (one per line, class selectors) of the window-tabs capsule, or of the style given: all its parts."""
    if css is None:
        source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
        css = source.split("/* capsule: window tabs */", 1)[1].split("].join('');", 1)[0]
    return [m.group(1) for m in re.finditer(r'^\s*"(\.(?:[^"\\\n]|\\.)*)",?$', css, re.M)]


def test_the_capsule_writes_only_what_differs_from_the_kit():
    rules = _style_rules()
    assert rules, "the capsule's rules were not found"
    body = {}
    for rule in rules:
        selector, _, rest = rule.partition("{")
        body.setdefault(selector, "")
        body[selector] += rest
    # The row is the kit's row of number tabs in the kit's rail: the column, its gap and (on the narrow shell) the wrap onto the next line
    # are the kit's. The capsule asks for the room the row takes in the rail's column, and nothing else: no display, no gap, no line, no scroll.
    assert ".wt-row" in body and ".wt-bar" not in body and not [s for s in body if "tl-head" in s]
    assert not re.search(r"display:|gap|border-bottom|overflow-x|scrollbar", body[".wt-row"]), body[".wt-row"]
    assert body[".wt-row"] == "flex:0 0 auto;min-width:0}", body[".wt-row"]
    # The tab is the kit's rail tab: the capsule writes no padding, no height and no colour of a state on it but the ink of a window hidden in Rows (aria-disabled).
    assert not re.search(r"padding|min-height|grid-template", body[".wt-tab"]), body[".wt-tab"]
    # The scope row is the kit's pill segment (the graph's "Period": .seg, .seg-opt, the grey track and the picked option white): the capsule
    # writes its place and the cut of a long option, once, and nothing of the pill's look (no padding, height, colour, ground or border).
    assert ".wt-scope-opt" not in body, "the option's look is the kit's, written once for the graph's segment and the plate's"
    assert not re.search(r"padding|min-height|background|border|color:|gap", body[".wt-scope"]), body[".wt-scope"]
    opt = body[".wt-scope .wt-scope-opt"]
    assert opt.count("text-overflow:ellipsis") == 1 and "white-space:nowrap" in opt, opt
    assert not re.search(r"padding|min-height|background|border|color:", opt), opt
    assert "gap" not in body[".wt-scope"]
    # There is no underline of its own: no engine that measures the tabs and slides a mark under the picked one, no mark, no watcher.
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    assert not [sel for sel in body if "wt-ink" in sel or "wt-inked" in sel or "wt-go" in sel], sorted(body)
    assert not re.search(r"wt-ink|wt-inked|wt-go|ResizeObserver|inkBox|paintInk|settleInk", source)
    # The tab is the kit's number tab (markTab) and the row the kit's tablist (markRow); the capsule gives them their place and air.
    assert "el('nav', 'rail')" in source and "row.className += ' wt-row';" in source and "tl-head wt-bar" not in source
    assert "markTab({ key: tab.seconds, name: shortLength(tab.seconds), count: g ? avg : '—', note: incomplete ? '*' : '', selected: on," in source
    assert "ring: ring });" in source and "var ring = ringGauge(parseFloat(avg), g && avg !== '—' ? 'ok' : 'unknown', 'lg', true, sweeps);" in source
    assert "var row = markRow(items, true);" in source and "btn.className += ' wt-tab';" in source
    assert "tl-tab" not in source, "the tab wears nothing of the old graph's tabs"
    # The note's group wraps inside the card (it is one box of at most the line's width, top-aligned) and a name is a box that breaks
    # only where it is wider than the line: nothing of the note is nowrap, and nothing of the tab's text is allowed to wrap.
    assert re.search(r"display:inline-block;max-width:100%;min-width:0;vertical-align:top", body[".wt-foot-g"]), body[".wt-foot-g"]
    assert "white-space" not in body[".wt-foot-g"] and "white-space" not in body[".wt-who"], (body[".wt-foot-g"], body[".wt-who"])
    assert re.search(r"display:inline-block;max-width:100%;[^}]*overflow-wrap:anywhere", body[".wt-who"]), body[".wt-who"]
    assert "min-width:0" in body[".wt-foot"]
    assert "white-space:nowrap" in _section(_capsule()[1], "kit: mark tab").split("tab-mark-num{", 1)[1].split("}", 1)[0]
    # The scope option is the kit's segment option, wearing the kit's compact button (.btn-compact) and, picked, the kit's .active beside its
    # own wt- class: the kit classes carry its size and its look (the pill rules of the graph's segment reach the plate's: .wt-plate .seg).
    assert "el('div', 'seg wt-scope')" in source and "el('button', 'seg-opt btn-compact hover-quiet wt-scope-opt' + (on ? ' active' : '')" in source
    assert "tl-model-opts wt-scope" not in source and "'.wt-plate .seg" not in source


RING_GEOMETRY = r"""
const out = {};
class Node { constructor(tag) { this.tag = tag; this.attrs = {}; this.kids = []; this.classes = []; this.style = {}; this.classList = { add: (c) => this.classes.push(c) }; }
  setAttribute(k, v) { this.attrs[k] = String(v); } appendChild(n) { this.kids.push(n); return n; } }
const document = { createElementNS: (ns, tag) => new Node(tag) };
%(code)s
const show = (svg) => ({ attrs: svg.attrs, classes: svg.classes, style: svg.style, kids: svg.kids.map((k) => ({ tag: k.tag, attrs: k.attrs })) });
for (const [name, args] of Object.entries({ p0: [0, 'ok'], p42: [42, 'ok'], p100: [100, 'ok'], over: [150, 'ok'], unknown: [42, 'unknown'], nan: [NaN, 'ok'], none: [undefined],
    lg0: [0, 'ok', 'lg'], lg42: [42, 'ok', 'lg'], lg100: [100, 'ok', 'lg'], lgunknown: [42, 'unknown', 'lg'], lgnone: [undefined, 'ok', 'lg'],
    sw62: [62.5, 'ok', 'lg', true, true], swplain: [30, 'ok', undefined, false, true], swunknown: [42, 'unknown', 'lg', true, true], swnone: [undefined, 'ok', 'lg', true, true] })) {
  out[name] = show(ringGauge(...args));
}
console.log(JSON.stringify(out));
"""


def test_the_rail_says_a_window_short_in_english_and_in_russian():
    """shortLength(seconds): a count of days, hours or minutes - the largest that holds the length whole (a week is 7 days) - from the
    keys win.short.*: 5H 7D 30D in English, 5ч 7д 30д in Russian; a length that is none of them says n/a (н/д)."""
    import json
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    found = re.search(r"    function shortLength\(.*?\n    }\n", source, re.S)
    assert found and source.count("function shortLength(") == 1
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    english = json.loads(Path(__file__).with_name("strings_en.json").read_text(encoding="utf-8"))
    tables = {"en": {k: v for k, v in english.items() if k.startswith("win.short.")}}
    if RU_JSON is not None:
        entries = json.loads(RU_JSON.read_text(encoding="utf-8"))["entries"]
        tables["ru"] = {k[len("code:quotas.ui."):]: v["text"] for k, v in entries.items() if k.startswith("code:quotas.ui.win.short.")}
    expected = {"en": ["5H", "7D", "30D", "1D", "1H", "15M", "14D", "n/a", "n/a", "n/a"],
                "ru": ["5ч", "7д", "30д", "1д", "1ч", "15мин", "14д", "н/д", "н/д", "н/д"]}
    for language, table in tables.items():
        harness = ("const T = %s;\nconst t = (key, params) => String(T[key]).replace(/\\{(\\w+)\\}/g, (all, n) => params[n]);\n%s\n"
                   "console.log(JSON.stringify([18000, 604800, 2592000, 86400, 3600, 900, 1209600, 0, undefined, 90].map(shortLength)));"
                   % (json.dumps(table), found.group(0)))
        result = subprocess.run([str(node), "-e", harness], cwd=Path(__file__).parent, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == expected[language], (language, result.stdout)
    if RU_JSON is None:  # the English half has run; the Russian half needs the translation file
        pytest.skip(RU_JSON_SKIP)


def test_the_ring_is_drawn_by_one_builder_and_its_arc_and_dot_follow_the_share():
    """kit: ring. ringGauge(pct, form) makes the svg of the sheet (viewBox 184, one path of 300 degrees for the track and the arc, a dash
    of pct on a path of length 100, a dot at the angle 120 + 3 x pct) in the forms ok and unknown. The builder is run on a bare fake
    document, so the geometry is held at 0, 42 and 100 per cent without a page."""
    import json
    import math
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    assert source.count("function ringGauge(") == 1 and source.count("RING_PATH") >= 2 and source.count("function ringPath(") == 1
    parts = []
    for pattern in (r"    var SVG_NS = [^\n]*\n", r"    function svgCanvas\(.*?\n    }\n", r"    function svgEl\(.*?\n    }\n",
                    r"    var RING_PATH = [^\n]*\n", r"    var RING_SIZES = \{.*?\n    };\n", r"    function ringPath\(.*?\n    }\n",
                    r"    function setVar\(.*?\n    }\n",
                    r"    var BADGE_R = [^\n]*\n", r"    var BADGE_STROKE = [^\n]*\n",
                    r"    function ringGauge\(.*?\n    }\n"):
        found = re.search(pattern, source, re.S)
        assert found, pattern
        parts.append(found.group(0))
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    result = subprocess.run([str(node), "-e", RING_GEOMETRY % {"code": "".join(parts)}], cwd=Path(__file__).parent,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    rings = json.loads(result.stdout)
    path = "M57 152.62A70 70 0 1 1 127 152.62"
    for name, share in (("p0", 0), ("p42", 42), ("p100", 100), ("over", 100)):
        svg = rings[name]
        assert svg["classes"] == ["ring"]
        assert svg["attrs"] == {"viewBox": "0 0 184 184", "width": "28", "height": "28", "aria-hidden": "true", "focusable": "false"}, svg["attrs"]
        track, arc, dot = svg["kids"]
        assert [k["tag"] for k in (track, arc, dot)] == ["path", "path", "circle"]
        assert track["attrs"] == {"class": "ring-track", "d": path, "fill": "none", "stroke-width": "22", "stroke-linecap": "round"}, track
        assert arc["attrs"] == {"class": "ring-arc", "d": path, "fill": "none", "stroke-width": "26", "stroke-linecap": "round",
                                "pathLength": "100", "stroke-dasharray": "%d 100" % share}, arc
        angle = math.radians(120 + 3 * share)
        assert dot["attrs"]["class"] == "ring-dot" and dot["attrs"]["r"] == "22"
        assert abs(float(dot["attrs"]["cx"]) - (92 + 70 * math.cos(angle))) < 0.006, dot
        assert abs(float(dot["attrs"]["cy"]) - (92 + 70 * math.sin(angle))) < 0.006, dot
    # The ends of the arc: empty, the dot at the foot of the left side (57, 152.62); full, at the foot of the right (127, 152.62).
    assert (rings["p0"]["kids"][2]["attrs"]["cx"], rings["p0"]["kids"][2]["attrs"]["cy"]) == ("57.00", "152.62")
    assert (rings["p100"]["kids"][2]["attrs"]["cx"], rings["p100"]["kids"][2]["attrs"]["cy"]) == ("127.00", "152.62")
    # Unknown (the form, or a share that is no number): the track alone, dotted, no arc and no dot.
    for name in ("unknown", "nan", "none"):
        svg = rings[name]
        assert len(svg["kids"]) == 1 and svg["kids"][0]["attrs"]["class"] == "ring-track", name
        assert svg["kids"][0]["attrs"]["stroke-dasharray"] == "1 30", name
    # The large ring (the rail's, 47px, the share is drawn inside its box): the same ring on a path of a wider radius with thinner strokes,
    # the dot at the same angle (120 + 3 x pct) on that radius; the unknown form is the dotted track alone.
    big = "M51.5 162.15A81 81 0 1 1 132.5 162.15"
    for name, share in (("lg0", 0), ("lg42", 42), ("lg100", 100)):
        svg = rings[name]
        assert svg["classes"] == ["ring", "ring-lg"], svg["classes"]
        assert svg["attrs"] == {"viewBox": "0 0 184 184", "width": "47", "height": "47", "aria-hidden": "true", "focusable": "false"}, svg["attrs"]
        track, arc, dot = svg["kids"]
        assert track["attrs"] == {"class": "ring-track", "d": big, "fill": "none", "stroke-width": "7", "stroke-linecap": "round"}, track
        assert arc["attrs"] == {"class": "ring-arc", "d": big, "fill": "none", "stroke-width": "9", "stroke-linecap": "round",
                                "pathLength": "100", "stroke-dasharray": "%d 100" % share}, arc
        angle = math.radians(120 + 3 * share)
        assert dot["attrs"]["class"] == "ring-dot" and dot["attrs"]["r"] == "10.5"
        assert abs(float(dot["attrs"]["cx"]) - (92 + 81 * math.cos(angle))) < 0.006, dot
        assert abs(float(dot["attrs"]["cy"]) - (92 + 81 * math.sin(angle))) < 0.006, dot
        # The ring and its strokes leave a hole for a share of four characters: the inner radius of the arc, at 47px, is above 18px.
        assert (81 - 9 / 2) * 47 / 184 > 18, "room inside the large ring for 100%"
    for name in ("lgunknown", "lgnone"):
        svg = rings[name]
        assert len(svg["kids"]) == 1 and svg["kids"][0]["attrs"]["class"] == "ring-track" and svg["classes"] == ["ring", "ring-lg"], name
        assert svg["kids"][0]["attrs"]["stroke-dasharray"] == ".5 14", name
    # The sweep (the fifth argument): a ring of the form ok that is asked to wears ring-sweep and the share the keyframes read (--ring-share), and is
    # drawn as it stands otherwise (the same arc and dot: the keyframes move them, the builder does not); a ring that is not asked to, and a ring
    # with no share, wear none of it.
    for name in ("lg0", "lg42", "lg100", "p42"):
        assert "ring-sweep" not in rings[name]["classes"] and rings[name]["style"] == {}, name
    sw = rings["sw62"]
    assert sw["classes"] == ["ring", "ring-lg", "ring-sweep"] and sw["style"] == {"--ring-share": "62.5"}, sw
    assert sw["kids"][1]["attrs"]["stroke-dasharray"] == "62.5 100" and [k["attrs"]["class"] for k in sw["kids"]] == [
        "ring-track", "ring-arc", "ring-dot", "ring-badge"], sw
    assert rings["swplain"]["classes"] == ["ring", "ring-sweep"] and rings["swplain"]["style"] == {"--ring-share": "30"}, rings["swplain"]
    for name in ("swunknown", "swnone"):
        assert "ring-sweep" not in rings[name]["classes"] and rings[name]["style"] == {}, name


def test_limit_row_has_one_form_the_plate():
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    # The row of a limit is built in one form, the plate: its builders hold no branch for "no plate". (Not the whole file: the line of the pick looks the plate of the screen up
    # (arrowFit: `if (!plate) return;` on a query that finds none), which is not a form of the limit row.)
    for builder in ("function limitRow(", "function windowPlate("):
        body = source.split(builder, 1)[1].split("\n    }\n", 1)[0]
        assert "if (!plate)" not in body, builder
    # (the seventh argument is the window's own name, the fact's caption: "Weekly"; the eighth, `extras`, is what the plate takes in under its
    # number row: the model list, the note of the accounts not read and the family's credits)
    assert "function limitRow(g, name, chartKey, index, markTightest, stamp, caption, extras)" in source
    assert "function factBubble(caption, figure)" in source and "function limitBubble(spent, index, key, plate)" in source


def _capsule():
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    css = source.split("var STYLE = [", 1)[1].split("].join('');", 1)[0]
    return source, css, css.split("/* capsule: window tabs */", 1)[1]


def _section(css, name):
    """One section of the style: from its marker to the next kit or capsule marker (the window-tabs capsule is the last)."""
    start = css.index("/* %s */" % name)
    end = re.search(r"/\* (?:kit|capsule): ", css[start + 3:])
    return css[start:start + 3 + end.start()] if end else css[start:]


def test_the_capsule_styles_only_the_classes_of_its_own_prefix():
    # What the plate changes in the other parts' classes (the row, the stamp, the legend, the bars, the graph's row of
    # controls) is written with those parts, under the window-tabs plate: here stand only the wt- classes, in the media too.
    rules = re.findall(r'^\s*"((?:[^"\\]|\\.)*)",?$', _capsule()[2], re.M)
    heads = [h for rule in rules if not rule.startswith("@keyframes") for h in re.findall(r"([^{}]+)\{", rule)
             if not h.startswith("@")]
    classes = {c for h in heads for c in re.findall(r"\.([A-Za-z_][\w-]*)", h)}
    assert heads and classes, "the capsule's rules were not found"
    # The states of the stamp's words (the number read short of all, the oldest reading old, the dot while a read is in the air) are
    # worn beside a wt- class and styled only with it.
    states = {"lack", "is-old", "is-busy"}
    assert all(c.startswith("wt-") for c in classes - states), sorted(c for c in classes - states if not c.startswith("wt-"))
    assert all(re.search(r"\.wt-[\w-]+\.%s" % state, "".join(rules)) for state in states if state in classes), states


def test_the_plate_has_a_legend_and_a_reset_written_in_the_kits_own_parts():
    source, css, capsule = _capsule()
    # The plate's rules for the other parts' classes stand with those parts, under the window-tabs plate: the two rows, the
    # hover and the dimming, and the legend's small bar with the mark on it, in "bars and legend" (the legend's own parts: the
    # kit does not name them); the bar's mark in the bars.
    legend = _section(css, "capsule: bars and legend")
    bars = _section(css, "kit: bars")
    assert not re.search(r"l-tail|tailNode|limitTail|rs-bad|rs-warn", source), "the one-line tail is gone"
    # The style system has no gradient (the dashed mark is a dashed border) and no mask: the narrow rail's row wraps onto the next line,
    # it does not scroll and it does not fade, so every window is in sight and the picked one is never cut.
    # (the one exception is the ladder's open window in the rail, kit: rail: the haze of its two edges, .rail-scroll*; nowhere else)
    outside = "".join(r for r in _style_rules(css) if ".rail-scroll" not in r.partition("{")[0])
    assert outside.count("gradient(") == 0 and "mask-image" not in outside and "--haze-w" not in css, "no gradient, no mask but the haze of the ladder's window"
    assert not re.search(r"(?<![A-Za-z])tail", css), "the plate's rows are the figure, the bars, then the legend and the reset"
    # Both rows span the plate and take the rows after the named ones: an absent row leaves no gap behind it.
    assert ".lg,.nx{grid-column:1/-1;" in legend
    # The legend is the kit's .calm-legend (its flex, gap, size and colour are not written again); the reset is new.
    assert "el('ul', 'calm-legend lg')" in source and "el('li', 'legend-item')" in source
    lg = [r for r in _style_rules(css) if any(s.strip().startswith(".lg") for s in r.partition("{")[0].split(","))]
    assert lg and not any(re.search(r"flex-wrap|font-size|gap:", r) for r in lg), lg
    # An item is its number and its words in one line of text, not the kit's row of a mark and words (no mark stands in it any more):
    # that one display is the only one the legend writes.
    assert [r for r in lg if "display:" in r] == [".lg .legend-item{display:block}"], lg
    # Colours are the kit's variables; the red of the plate is the crimson of the kit: the decimal point of the figure (.wt-dot, the crimson ink),
    # and the dot and what follows it in the head of a note in words (.wt-head-rest, the accent: the accounts at the limit now, the money beyond the
    # allowance of a billing cycle, the accounts not read). The legend and the reset row have no red digit of their own (no b.acc, no accent), no
    # amber, no second colour of a state.
    assert not re.search(r"b\.acc(?![\w-])|\.acc(?![\w-])", css), "no red digit in the legend or the reset row"
    assert '".wt-dot{color:var(--tone-crimson-ink)}"' in css
    # The accounts at the limit now are words and not a bubble: no fill, no padding, no radius, no shadow; the quiet ink, the head of it (the name) in the
    # primary ink at the regular weight.
    bubble_rule = next(r for r in _style_rules(css) if r.startswith(".wt-limit{"))
    assert "color:var(--text-meta)" in bubble_rule and not re.search(r"background|padding|border-radius|box-shadow|tone-crimson", bubble_rule), bubble_rule
    assert not re.search(r"--(?:status-bad|status-warn|accent)", bubble_rule), bubble_rule
    head_rule = next(r for r in _style_rules(css) if r.startswith(".wt-limit-head{"))
    assert "color:var(--text-primary)" in head_rule and "font-weight:var(--weight-regular)" in head_rule, head_rule
    assert ".wt-head-rest{color:var(--accent)" in css, "the dot and the tail of a head are the accent"
    assert not re.search(r"\.wt-limit:(?:hover|focus-within)[^{]*\{[^}]*(?:box-shadow|background)", css), "no shadow on the pointer: the words are not a card"
    plate_rules = [r for r in _style_rules(css) if re.match(r"\.(?:lg|nx|bars?|lrow|l-fig|l-chart|wt-plate|wt-num)(?![\w-])", r)]
    assert len(plate_rules) > 20, len(plate_rules)
    for rule in plate_rules:
        assert not re.search(r"--(?:status-bad|status-warn|bad-text|warn-text|hatch-warn|ctl-w)|\.(?:bad|warn)\b", rule), rule
    assert "--hatch-warn" not in css and "--ctl-w" not in css and ".rs-tight" not in css and ".l-sub" not in css, "nothing reads them any more"
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", "".join(r for r in _style_rules(css) if ".lg" in r or ".nx" in r))
    # Hover and focus are one thing, and the kinds of bar step back: one rule for each kind, by the kind the plate wears
    # (data-dim, set by the item's own events), the kit's ring. No rule asks :has() about the item's :hover or focus:
    # WebKit does not follow it, and two behaviours would be one too many.
    assert ":has(.legend-item" not in css, "the legend's dimming is not a :has() on :hover / :focus-visible"
    for kind, mark in (("limit", "spent"), ("held", "held"), ("was", "was"), ("last", "last"), ("unknown", "unknown")):
        assert (".wt-plate[data-dim=%s] .bars .bar:not(.%s){opacity:var(--dim-others)}" % (kind, mark)) in legend or kind == "last", kind
    # The item "last known" counts the dated readings that are neither at the limit nor held back; those two wear .last as well, and step back with the rest.
    assert ".wt-plate[data-dim=last] .bars .bar:is(:not(.last),.was,.held){opacity:var(--dim-others)}" in legend
    for event in ("mouseenter", "mouseleave", "focus", "blur"):
        assert "li.addEventListener('%s'" % event in source, event
    assert "plate.setAttribute('data-dim', names ? 'group' : to)" in source and "matches(':focus-visible')" in source
    # the ring of an item the keyboard reaches is the kit's (kit: focus ring), not a body of the capsule
    assert ".lg .legend-item:focus-visible,.nx[tabindex]:focus-visible" in _section(css, "kit: focus ring")
    assert ".legend-item:focus-visible" not in legend
    assert ".lg .legend-item,.nx[tabindex]{position:relative;cursor:default}" in legend
    # The new mark of a bar, in the share ink; the strip's bars say it with their class.
    assert re.search(r"\.bar\.was:after,\.bar\.held\.last:after\{[^}]*border-bottom:3px dashed var\(--share-ink\)", bars)
    assert "if (known && bar.at_limit && bar.state === 'last_known') cls += ' was';" in source
    # On the legend's small bar (12 px) that dashed border runs into one line in Chromium: there the mark is two
    # segments of its own, 4 px each with a 4 px gap, in the share ink - not a gradient.
    # An item is its number and its words: the bars above are its sample, so the legend has no small bar and no rule for one.
    assert not re.search(r"\.lg \.bar|\.strip-legend \.bar\.was|legend-item[^{]* \.bar", legend), "no small bar in the legend"
    assert "el('div', 'bar" not in source.split("function legendNode(")[1].split("function resetNode(")[0]
    # Motion: opacity only, 120 ms, none under reduced motion (the bars', with the dimming); the note rises in with the capsule's
    # own pick and its group fades its ink (the pair of both under reduced motion); the rise comes in for both rows; the bars' growth
    # holds nothing after its end, or it would hold the opacity the dimming takes.
    # (the bar's one transition also carries the move of its opening, the basis and the most, kit: bars - widget.js ".bars .bar{transition:...}"; same exact string, not looser)
    # (the opening of a bar also moves its share of the free room (flex-grow), and the three run on their own
    # token pair, --dur-bars on --ease-bars; the fade of the dimming and the transform stay on --dur-1 and --dur-5)
    assert (".bars .bar{transition:opacity var(--dur-1),transform var(--dur-5) var(--ease),"
            "flex-basis var(--dur-bars) var(--ease-bars),flex-grow var(--dur-bars) var(--ease-bars),"
            "max-width var(--dur-bars) var(--ease-bars)}") in legend
    # (the plate's rise is put on the note only where the note has not its own, the fade after the rings of the family picked: .wt-foot-in)
    assert re.search(r"prefers-reduced-motion:reduce\)\{[^\n]*\.wt-foot-in,\.wt-in>\.wt-foot:not\(\.wt-foot-in\)\{animation:none\}\.wt-foot-g\{transition:none\}", capsule)
    assert ".wt-in>.wt-foot:not(.wt-foot-in){animation:wt-rise var(--dur-4) var(--ease) both}" in capsule
    # The group of the note lights its own bars: the plate dims to the group and the bars of its accounts wear data-lit.
    assert ".wt-plate[data-dim=group] .bars .bar:not([data-lit]){opacity:var(--dim-others)}" in legend
    assert re.search(r"prefers-reduced-motion:reduce\)\{[^\n]*\.bars \.bar\{transition:none\}", legend)
    assert ".wt-in>.lg,.wt-in>.nx{animation:wt-rise" in legend
    assert "animation:wt-grow var(--dur-2) var(--ease) backwards}" in legend
    assert "'lg', 'nx'" in source and "l-tail" not in source


GEOMETRY = """() => { const q = (s) => document.querySelector(s); const r = (e) => e && e.getBoundingClientRect();
  return {width: innerWidth, scroll: document.documentElement.scrollWidth, plate: r(q('.wt-plate')), bars: r(q('.bars')),
          lg: r(q('.lg')), nx: r(q('.nx')), items: [...document.querySelectorAll('.legend-item')].map(r)}; }"""
OPACITIES = "() => [...document.querySelectorAll('.bars .bar')].map((b) => [b.className.split(' ')[1] || '', getComputedStyle(b).opacity])"
DIMS = "() => [...document.querySelectorAll('[data-dim]')].map((n) => n.getAttribute('data-dim'))"
# The redraw of the 30 s timer (the visibility read draws the same way: load() > rerender() twice), the plate before it kept.
REDRAW = "() => { window.__plate = document.querySelector('.wt-plate'); document.dispatchEvent(new Event('visibilitychange')); }"


def test_the_bars_open_on_their_own_run_and_the_pair_keeps_its_sum():
    # rule: the bars no longer jerk when a pick shuts one and opens another. The open bar does not grow (flex-grow 0) and the
    # shut one does (1), and the grow moves with the basis, so the sum of the pair stays the same through the move and the bars to the right of the pair do not
    # leap; the move runs longer and more evenly (--dur-bars on --ease-bars) than the rest. Rolled back, the sum of the pair overshoots by ~16 px.
    source, css, capsule = _capsule()
    bars = _section(css, "kit: bars")
    legend = _section(css, "capsule: bars and legend")
    opened = "{flex-basis:calc(var(--bar-rest) + var(--bar-open));flex-grow:0;flex-shrink:0;max-width:calc(var(--bar-rest) + var(--bar-open))}"
    assert bars.count(opened) == 2, "both the fine pointer's and the coarse one's open bar take no share of the free room"
    assert re.search(r"\.bar\{[^}]*flex:1 1 0[;}]", bars), "a shut bar takes its share (the open one's 0 is the other end of the move)"
    assert re.search(r"--dur-bars:(\d+)ms", css) and 250 <= int(re.search(r"--dur-bars:(\d+)ms", css).group(1)) <= 400
    curve = [float(n) for n in re.search(r"--ease-bars:cubic-bezier\(([^,]+),([^,]+),([^,]+),([^)]+)\)", css).groups()]
    assert 0 <= curve[0] <= 1 and 0 <= curve[2] <= 1 and 0 <= curve[1] <= 1 and 0 <= curve[3] <= 1, "no bounce: " + str(curve)
    assert curve[1] < .5, "an even start: the curve may not give a third of the way in the first frames (the old --ease gave 30-36 %)"
    # the liquid holds its width for as long as the move takes: the hold follows the move's own duration
    assert ".bar>.fill{" in bars and "width 0s linear var(--dur-bars)" in bars and "width 0s linear var(--dur-5)" not in bars.split(".bar-i{")[0]
    assert ".bar>.bar-v,.bar:after{width:100%;transition:width 0s linear var(--dur-bars)}" in bars
    assert "flex-basis var(--dur-5)" not in css and "max-width var(--dur-5)" not in css, "no move of the opening is left on the old pair"
    assert re.search(r"prefers-reduced-motion:reduce\)\{[^\n]*\.bars \.bar\{transition:none\}", legend), "the pair of the move under reduced motion"


def test_the_pointer_opens_no_bar_under_a_pointer_that_has_not_moved_since_the_pick():
    # rule: hover still opens a bar, but a pick moves the bars under a pointer that stands where the click was (the shut bar
    # shrinks, the one picked slides left from under the pointer), and the bar that comes under it opened by hover at once and pushed the bars on its right back and forth
    # (100-150 px, 9-15 reversals). Until the pointer really moves the rows wear .bars-still and the pointer opens and lights nothing; the pick, the keyboard and the step open as ever.
    source, css, capsule = _capsule()
    bars = _section(css, "kit: bars")
    hovered = ":is(:hover:not(:disabled):not(:where(.bars-still>.bar)),:focus-visible,.sel,.bar-on)"
    assert bars.count(".bars>.bar:is(:hover:not(:disabled)") == 4 and bars.count(".bars>.bar" + hovered) == 4, "every part of the opening is under the one still rule"
    # (the colour of the liquid is the pointer's, the pick's and the step's, never the keyboard's focus ring: its :is() has no :focus-visible, as before the pick; the opening of the bar has)
    lit = ":is(:hover:not(:disabled):not(:where(.bars-still>.bar)),.sel,.bar-on)"
    assert ".bar" + lit + ">.fill{background:currentColor}" in bars and ".bar.last" + lit + ">.fill{" in bars, "the pointer lights nothing in a row that is still"
    assert bars.count("hover:not(:disabled)") == bars.count(":not(:where(.bars-still>.bar))"), "no hover of a bar is left outside the still rule"
    assert "pointer:coarse){.bars>.bar:is(.sel,.bar-on)" in bars and "(:where(.bars-still" not in bars.split("pointer:coarse")[1].split("}}")[0], "a finger has no hover to hold"
    assert "var(--" not in "".join(re.findall(r":not\(:where\(\.bars-still[^)]*\)\)", bars)), "the rule is a selector, no value"
    # the script: the press of the pointer (a detail above 0) holds the rows, a row drawn anew wears it, the first real move lets it go (the same place does not)
    assert "var strip = el('div', barStill ? 'bars bars-fitting bars-still' : 'bars bars-fitting');" in source
    assert re.search(r"b\.addEventListener\('click', function \(e\) \{\s*e\.stopPropagation\(\);\s*holdBarStill\(e\);\s*selectAccount\(", source)
    assert "if (!e || !(e.detail > 0)" in source, "the keyboard's press (a detail of 0) holds nothing"
    assert "if (barStill && !(barStillAt && e && e.clientX === barStillAt.x && e.clientY === barStillAt.y)) releaseBarStill();" in source


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_a_click_after_the_pointer_has_opened_a_bar_does_not_shake_the_bars_on_its_right(fixture_file, engine):
    # The owner's scenario: the pointer comes to a bar (it opens by hover), waits, clicks. The bar that was picked shuts and the one clicked slides left from under the pointer; the
    # bars it passes must not open by hover under the stand-still pointer, and the bars on the right of the pair move one way only (no reversal). Rolled back: 9-15 reversals.
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    record = """() => { const R = window.__still = {frames: [], on: true}; const t0 = performance.now();
        const tick = () => { if (!R.on) return; const row = document.querySelector('.wt-plate .bars'); const rb = row.getBoundingClientRect();
            R.frames.push([...row.querySelectorAll(':scope > .bar')].map((b) => { const r = b.getBoundingClientRect(); return [r.left - rb.left, r.width, b.classList.contains('sel') ? 1 : 0]; }));
            requestAnimationFrame(tick); };
        requestAnimationFrame(tick); }"""
    stop = "() => { window.__still.on = false; return window.__still.frames; }"

    def reversals(values):
        steps = [b - a for a, b in zip(values, values[1:]) if abs(b - a) > .3]
        return sum(1 for a, b in zip(steps, steps[1:]) if a * b < 0)

    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            frame = _mount_layout_frame(page, views["reading_many"], 900)
            frame.wait_for_function("() => document.querySelectorAll('.wt-plate .bars > .bar[data-account]').length > 8")
            page.mouse.move(2, 2)
            page.wait_for_timeout(900)
            bars = frame.locator(".wt-plate .bars > .bar[data-account]")
            assert frame.evaluate("() => matchMedia('(pointer:fine)').matches"), "the engine has a fine pointer"

            def centre(i):
                box = bars.nth(i).bounding_box()
                return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

            def picked():
                return frame.evaluate("() => [...document.querySelectorAll('.wt-plate .bars > .bar')].findIndex((b) => b.classList.contains('sel'))")

            # the pick is put on the first bar; the target is the third, with bars beyond it
            start = picked()
            if start != 0:
                x, y = centre(0)
                page.mouse.move(x, y)
                page.wait_for_timeout(500)
                page.mouse.down()
                page.mouse.up()
                page.mouse.move(2, 2)
                page.wait_for_timeout(1400)
                start = picked()
            assert start == 0, start
            target = start + 2
            assert bars.count() - target > 5
            x, y = centre(target)
            page.mouse.move(x, y)
            page.wait_for_timeout(900)
            assert frame.evaluate("() => document.querySelectorAll('.bars-still').length") == 0, "a pointer that came by itself holds nothing"
            assert bars.nth(target).bounding_box()["width"] > 100, "the bar opens by hover"
            # the reader goes on to the middle of the bar that opened (its words are there), waits a moment, and clicks: that is where the bar will slide away from
            x, y = centre(target)
            page.mouse.move(x, y)
            page.wait_for_timeout(500)
            frame.evaluate(record)
            page.wait_for_timeout(60)
            page.mouse.down()
            page.mouse.up()
            page.wait_for_timeout(1300)
            frames = frame.evaluate(stop)
            assert picked() == target
            # no bar but the pair opens: the widest bar of the others stays far below an open one
            opened = max(f[1] for fr in frames for i, f in enumerate(fr) if i not in (start, target))
            assert opened < 100, "a bar that came under the pointer opened by hover: %.0f px" % opened
            shaken = {i: reversals([fr[i][0] for fr in frames]) for i in range(target + 1, len(frames[0]))}
            assert not any(shaken.values()), "the bars on the right of the pair turn back: %s" % shaken
            assert frame.evaluate("() => document.querySelectorAll('.wt-plate .bars.bars-still').length") == 1, "the row is still: the pointer has not moved since the click"
            # the first real move gives the pointer its hold back: the bar under it opens again, the row wears nothing
            x, y = centre(target + 3)
            page.mouse.move(x, y)
            page.wait_for_timeout(900)
            assert frame.evaluate("() => document.querySelectorAll('.bars-still').length") == 0
            assert bars.nth(target + 3).bounding_box()["width"] > 100, "the pointer opens a bar again once it has moved"
            page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_legend_stands_under_the_bars_and_dims_the_other_kinds(fixture_file, engine):
    from test_bars import _mount_layout_frame, _let_go_the_pick
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            # 1. Where the rows stand, at the widths of a narrow card and the owner's: the legend and the reset are the last rows of the
            # notes under the number (the bars, the number row and the notes come first), the reset 4 px (--space-1, the gap between the
            # rows of the notes) under the legend; with no legend the reset stands under the number row, far under the bars; nothing
            # escapes the plate or the page.
            for key, legend in (("codex_states", True), ("agy", False), ("cursor_budget", False), ("cursor_mixed", True)):
                page = browser.new_page(viewport={"width": 1100, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                frame = _mount_layout_frame(page, views[key], 678)
                # The legend and the reset stand on the plate itself: the pick of the top account is let go first (picked, they stand in its card).
                _let_go_the_pick(frame)
                for width in (320, 380, 678):
                    page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                    frame.wait_for_function("(width) => innerWidth === width", arg=width)
                    g = frame.evaluate(GEOMETRY)
                    assert g["scroll"] <= g["width"] + 1, (key, width, g)
                    assert g["nx"] is not None, key
                    for box in [g["lg"], g["nx"], *g["items"]]:
                        if box:
                            assert g["plate"]["left"] - 1 <= box["left"] and box["right"] <= g["plate"]["right"] + 1, (key, width, g)
                    # The number row (16 px, --space-4, under the bars; 44 px of figure) stands between the bars and the notes, so the legend, or
                    # with no legend the reset, stands more than 16 + 44 px under the bars; the reset stands 4 px (--space-1) under the legend.
                    if legend:
                        assert g["lg"]["top"] - g["bars"]["bottom"] >= 16 + 44, (key, width, g)
                        assert abs(g["nx"]["top"] - g["lg"]["bottom"] - 4) <= 1, (key, width, g)
                    else:
                        assert g["lg"] is None and g["nx"]["top"] - g["bars"]["bottom"] >= 16 + 44, (key, width, g)
                assert errors == [], errors
                page.close()
            # 2. Focus is hover: the keyboard on an item dims the bars of the other kinds, in both engines.
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            frame = _mount_layout_frame(page, views["codex_states"], 678)
            _let_go_the_pick(frame)  # the legend items stand on the plate only with the pick let go
            frame.focus('.legend-item[data-k="was"]')
            page.keyboard.press("Shift+Tab")
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)
            assert frame.evaluate("() => document.activeElement.matches(':focus-visible')")
            seen = frame.evaluate(OPACITIES)
            assert [o for k, o in seen if k == "last"] == ["1"], seen
            assert [o for k, o in seen if k != "last"] and all(o == "0.35" for k, o in seen if k != "last"), seen
            # The item "last known", where the answer names no account to light (the plate then wears data-dim=last): the dated readings at the
            # limit (.was) and the held ones are not in it, and step back with the rest.
            frame.evaluate("() => document.querySelector('.wt-plate').setAttribute('data-dim', 'last')")
            page.wait_for_timeout(300)
            seen = frame.evaluate("() => [...document.querySelectorAll('.bars .bar')].map((b) => [b.className, getComputedStyle(b).opacity])")
            assert any(" was" in c for c, _ in seen), seen
            assert all(o == "0.35" for c, o in seen if " was" in c or " held" in c or " last" not in c), seen
            page.close()
            # 3. Hover does the same, and lifts when the pointer goes - in WebKit as in Chromium (the plate wears the kind
            # from the item's own events; a :has() on :hover did not follow there) - after the reader's own pick too, when
            # the plate has come in with motion (what the bars' growth held after its end would have kept at 1).
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            frame = _mount_layout_frame(page, views["two_windows"], 678)
            frame.click('[data-focus="win:claude:604800"]')
            page.wait_for_timeout(500)
            _let_go_the_pick(frame)  # the new window picks its top account again; its facts must stand on the plate
            assert "wt-in" in frame.evaluate("() => document.querySelector('.wt-plate').className")
            # (the accounts at the limit now are the plate's bubble, not an item of the legend: it names its accounts, so the plate dims to the group)
            frame.hover('.wt-plate .wt-limit .wt-limit-head')
            page.wait_for_timeout(300)
            seen = frame.evaluate(OPACITIES)
            assert any(k == "spent" for k, _ in seen), seen
            assert all(o == ("1" if k == "spent" else "0.35") for k, o in seen), seen
            assert frame.evaluate(DIMS) == ["group"]
            page.mouse.move(2, 2)
            page.wait_for_timeout(300)
            assert all(o == "1" for _, o in frame.evaluate(OPACITIES))
            assert frame.evaluate(DIMS) == []
            page.close()
            # 4. The mouse's own focus does not dim (the ring is not showing): a click on an item, then the pointer away.
            # Hover on to the next item takes the dim over, the one it leaves gives it up.
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            frame = _mount_layout_frame(page, views["codex_states"], 678)
            _let_go_the_pick(frame)  # the limit bubble stands on the plate only with the pick let go
            frame.click('.wt-plate .wt-limit .wt-limit-head')
            frame.hover('.legend-item[data-k="was"]')
            page.wait_for_timeout(300)
            assert frame.evaluate(DIMS) == ["was"]
            seen = frame.evaluate(OPACITIES)
            # (the bar at the limit when last read is `bar last was`: its kind in OPACITIES is the second class, last)
            assert [o for k, o in seen if k == "last"] == ["1"] and all(o == "0.35" for k, o in seen if k != "last"), seen
            page.mouse.move(2, 2)
            page.wait_for_timeout(300)
            assert frame.evaluate("() => document.activeElement.getAttribute('data-k')") == "limit"
            assert frame.evaluate(DIMS) == [], "focus by the mouse leaves nothing dimmed once the pointer is gone"
            assert all(o == "1" for _, o in frame.evaluate(OPACITIES))
            page.close()
            # 5. A redraw (the 30 s timer's) builds the plate anew, and the reader's dim does not outlive it: with the
            # pointer, then with the keyboard, nothing is left dimmed once the reader is off the item.
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            frame = _mount_layout_frame(page, views["codex_states"], 678)
            _let_go_the_pick(frame)  # the legend items stand on the plate only with the pick let go
            frame.hover('.legend-item[data-k="was"]')
            page.wait_for_timeout(200)
            assert frame.evaluate(DIMS) == ["was"]
            frame.evaluate(REDRAW)
            frame.wait_for_function("() => document.querySelector('.wt-plate') !== window.__plate")
            page.wait_for_timeout(300)
            page.mouse.move(2, 2)
            page.wait_for_timeout(300)
            assert frame.evaluate(DIMS) == [], "the pointer left: no dim stays on the plate the redraw built"
            assert all(o == "1" for _, o in frame.evaluate(OPACITIES))
            frame.focus('.legend-item[data-k="was"]')
            page.keyboard.press("Shift+Tab")
            page.keyboard.press("Tab")
            page.wait_for_timeout(200)
            assert frame.evaluate(DIMS) == ["was"]
            frame.evaluate(REDRAW)
            frame.wait_for_function("() => document.querySelector('.wt-plate') !== window.__plate")
            page.wait_for_timeout(300)
            frame.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")
            page.wait_for_timeout(300)
            assert frame.evaluate(DIMS) == [], "the keyboard left: no dim stays on the plate the redraw built"
            assert all(o == "1" for _, o in frame.evaluate(OPACITIES))
            page.close()
        finally:
            browser.close()


FIT = """() => { const q = (s) => [...document.querySelectorAll(s)]; const r = (e) => e.getBoundingClientRect();
  const lines = (e) => { const g = document.createRange(); g.selectNodeContents(e); return new Set([...g.getClientRects()].map((x) => Math.round(x.top))).size; };
  const foot = document.querySelector('.wt-foot');
  return {width: innerWidth, scroll: document.documentElement.scrollWidth, room: r(document.querySelector('.wt-row')).width,
    tabs: q('.wt-tab').map((t) => ({h: r(t).height, top: r(t).top, left: r(t).left, right: r(t).right, name: lines(t.querySelector('.tab-mark-name')), text: t.innerText})),
    foot: foot && {right: r(foot).right, left: r(foot).left, h: r(foot).height, scroll: foot.scrollWidth, client: foot.clientWidth},
    pieces: q('.wt-foot-g, .wt-who, .wt-foot-more').map((e) => ({cls: e.className, left: r(e).left, right: r(e).right})),
    commas: q('.wt-foot-g').reduce((n, g) => n + [...g.childNodes].filter((c) => c.nodeType === 3 && /^\\s*,/.test(c.textContent)).length, 0),
    // A comma at the end of a name that stands on a line lower than the letter before it opens that line.
    lead: q('.wt-who').reduce((n, who) => { const tn = who.firstChild; const s = tn && tn.nodeType === 3 ? tn.textContent : '';
      if (s.length < 2 || !/,$/.test(s)) return n;
      const at = (i) => { const g = document.createRange(); g.setStart(tn, i); g.setEnd(tn, i + 1); return g.getBoundingClientRect(); };
      return n + (at(s.length - 1).top > at(s.length - 2).top + 2 ? 1 : 0); }, 0),
    plate: r(document.querySelector('.wt-plate')).top}; }"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_row_and_the_note_fit_a_narrow_card_no_torn_name_no_sideways_scroll(fixture_file, engine):
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for key in ("reading_part", "reading_long", "reading_many", "reading_codex_month", "codex_states", "cursor_mixed", "cursor_budget"):
                page = browser.new_page(viewport={"width": 1100, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                frame = _mount_layout_frame(page, views[key], 678)
                for width in (282, 320, 360, 375, 520, 678):
                    page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                    frame.wait_for_function("(width) => innerWidth === width", arg=width)
                    fit = frame.evaluate(FIT)
                    where = (key, width, fit)
                    # No sideways scroll, whatever the names are.
                    assert fit["scroll"] <= fit["width"] + 1, where
                    # A tab is one line with a whole name (no break at its hyphen), 32 px, and stands whole inside the frame, the picked one too:
                    # the row of capsules wraps, it does not scroll and it does not fade, so no window is cut.
                    for tab in fit["tabs"]:
                        assert tab["name"] == 1 and tab["h"] == 32, where
                        assert tab["left"] >= -0.5 and tab["right"] <= fit["width"] + 0.5, where
                    # Where the capsules and their 6 px gaps are no wider than the row they stand in one line; where they are wider the last ones
                    # go down whole to the next line. From 420 px on three capsules stand in one line (their words are about 330 px).
                    need = sum(t["right"] - t["left"] for t in fit["tabs"]) + 6 * (len(fit["tabs"]) - 1)
                    stood = len({round(t["top"]) for t in fit["tabs"]})
                    assert (stood == 1) == (need <= fit["room"] + 0.5), (need, fit["room"], where)
                    if width >= 420:
                        assert stood == 1, where
                    # A comma between names stands in the box of the name before it: no text opens with one, so no line does.
                    assert fit["commas"] == 0, where
                    # ... and none opens a line on the screen either (the stand's Codex 30-day at 360 px: "gptpro14" / ", gptpro7").
                    assert fit["lead"] == 0, where
                    # The note and everything in it stay inside the card.
                    if fit["foot"]:
                        assert fit["foot"]["scroll"] <= fit["foot"]["client"] + 1 and fit["foot"]["right"] <= fit["width"] + 1, where
                        assert all(p["right"] <= fit["width"] + 1 and p["left"] >= -1 for p in fit["pieces"]), where
                assert errors == [], errors
                page.close()
        finally:
            browser.close()


AXIS = """() => { const q = (s, from) => (from || document).querySelector(s); const r = (e) => (e ? e.getBoundingClientRect() : null);
  const first = q('.rail .wt-tab'); const ring = r(q('.tab-mark-ring', first)); const plate = q('.lrow.wt-plate');
  return {width: innerWidth, scroll: document.documentElement.scrollWidth, rail: r(q('.rail')), card: r(q('.shell-card')), plate: r(plate),
          bars: r(q('.bars', plate)), tab: r(first), head: r(q('.harness-seg [aria-pressed=true]')), axis: ring.left + ring.width / 2, ringW: ring.width}; }"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_from_860px_the_rail_stands_left_the_card_right_and_the_plate_in_the_card_by_the_stylesheet_alone(fixture_file, engine):
    """The frame is the stylesheet's: from 860px the rail stands at the left of the card (side by side, no gap, at the same top), the first
    row's ring on the axis of the thread (the middle of the rail), and the plate of the picked window is in the card; under 860px the rail is
    a row over the card and the plate is in the card under it. Right after the width changed: the widget has no script that measures a name,
    no resize listener of its own for it, no inline padding."""
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    assert "alignPlate" not in source and "style.paddingLeft" not in source and "style.marginRight" not in source
    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    seen = []
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for key in ("reading_part", "reading_long", "reading_many", "reading_codex_month", "codex_states", "cursor_mixed", "cursor_budget"):
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                frame = _mount_layout_frame(page, views[key], 678)
                for width in (860, 1000, 1280, 860, 859, 678, 360):
                    page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                    frame.wait_for_function("(width) => innerWidth === width", arg=width)
                    axis = frame.evaluate(AXIS)
                    where = (key, width, axis)
                    rail, card, plate = axis["rail"], axis["card"], axis["plate"]
                    assert axis["scroll"] <= axis["width"] + 1, where
                    assert plate["left"] >= card["left"] - 0.5 and plate["right"] <= card["right"] + 0.5 and plate["top"] >= card["top"] - 0.5, where
                    assert axis["bars"]["left"] >= plate["left"] - 0.5 and axis["bars"]["right"] <= plate["right"] + 0.5, where
                    if width >= 860:
                        assert abs(rail["right"] - card["left"]) <= 1 and abs(rail["top"] - card["top"]) <= 1, where
                        # rule: the axis is the middle of the tab picked (the first of the head, at the edge of the widget), not the middle of the grey field; the rail holds the
                        # ring that stands on it whole: nothing of it is left of the edge of the widget, and at least the air (8px) is left between it and the card
                        assert abs(axis["tab"]["left"] - rail["left"]) <= 1 and abs(axis["axis"] - (axis["head"]["left"] + axis["head"]["right"]) / 2) <= 1, where
                        assert axis["axis"] - axis["ringW"] / 2 >= 0 and card["left"] - (axis["axis"] + axis["ringW"] / 2) >= 7.5, where
                        assert plate["left"] >= rail["right"] - 0.5, where
                    else:
                        assert rail["bottom"] <= card["top"] + 0.5 and plate["top"] >= rail["bottom"], where
                    seen.append((key, width, round(rail["right"] - card["left"], 2), round(axis["axis"], 2), round(axis["axis"] - axis["ringW"] / 2, 2), round(card["left"] - axis["axis"] - axis["ringW"] / 2, 2)))
                assert errors == [], errors
                page.close()
        finally:
            browser.close()


HOVER_MEDIA = "(hover: hover) and (not (max-width: 520.98px))"


def _raise_the_card(page, frame):
    """rule: the card of the plate is a strip at rest (the clip is closed, the note in its .kt-sum is hidden and takes no touch); a reader raises it
    with the pointer on the strip (a wide screen with a mouse) or a press (a narrow one or a finger), and a press pins it. This does the same and leaves the card pinned up."""
    toggle = '[data-focus="inspector-toggle"]'
    if frame.evaluate("(q) => window.matchMedia(q).matches", HOVER_MEDIA):
        frame.hover(toggle)
        frame.wait_for_function("() => !!document.querySelector('.inspector.kt-open')", timeout=4000)
    frame.click(toggle)
    page.mouse.move(2, 2)
    frame.wait_for_function("() => !!document.querySelector('.inspector.kt-open')", timeout=4000)
    frame.wait_for_timeout(700)


def _keep_the_card_up(page, frame):
    """rule: raise (and pin) the card of the plate unless it is up already (a second press on the strip would let it down again)."""
    if not frame.evaluate("() => !!document.querySelector('.inspector.kt-open')"):
        _raise_the_card(page, frame)


TOUCH = """() => { const q = (s) => document.querySelector(s); const r = (e) => e.getBoundingClientRect();
  const more = q('.wt-foot-more'); const b = r(more);
  const hits = (dy) => { const e = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2 + dy); return !!e && (e === more || more.contains(e)); };
  return {foot: r(q('.wt-foot')).height, button: b.height, up: hits(-21), far: hits(-30)}; }"""
FINGER = ("() => { for (const s of document.querySelectorAll('style')) "
          "s.textContent = s.textContent.split('@media(pointer:coarse)').join('@media all'); }")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_a_finger_does_not_stretch_the_note_and_the_more_button_keeps_its_44px_zone(fixture_file, engine):
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for key, width in (("reading_many", 360), ("reading_many", 390), ("reading_many", 678), ("reading_none", 390)):
                page = browser.new_page(viewport={"width": 1100, "height": 900})
                frame = _mount_layout_frame(page, views[key], width)
                # rule: the note stands in the card's strip-and-sum, which is closed at rest and takes no touch: raise the card first.
                if frame.locator(".wt-foot-more").count():
                    _raise_the_card(page, frame)
                mouse = frame.evaluate(TOUCH)
                # A finger (the coarse-pointer rules applied, which the kit gives a pressable part 44 px of height): "N more" sits in
                # the middle of a line of the note, so the line stays what it was; the 44 px is a zone around the button, not its box.
                frame.evaluate(FINGER)
                finger = frame.evaluate(TOUCH)
                where = (key, width, mouse, finger)
                assert abs(finger["foot"] - mouse["foot"]) <= 1, where
                assert finger["button"] <= 30 and abs(finger["button"] - mouse["button"]) <= 1, where
                assert not mouse["up"] and finger["up"] and not finger["far"], where
                page.close()
        finally:
            browser.close()


def test_real_browser_a_real_touch_screen_gets_the_same_note_and_a_44px_zone_around_the_more_button(fixture_file):
    """The stand's measure with the pointer media query itself (Chromium's touch emulation: pointer:coarse is true), not the rule
    rewritten to apply to a mouse: the note is the height it is under a mouse, the button keeps the height of its text, and a touch
    anywhere in the 44 px box around it presses it."""
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, "chromium")
        try:
            for key, width in (("reading_many", 390), ("reading_many", 360), ("reading_none", 390)):
                seen = {}
                for name, options in (("mouse", {}), ("finger", {"has_touch": True, "is_mobile": True})):
                    context = browser.new_context(viewport={"width": 1100 if name == "mouse" else width, "height": 900},
                                                  device_scale_factor=2, **options)
                    page = context.new_page()
                    frame = _mount_layout_frame(page, views[key], width)
                    assert frame.evaluate("() => matchMedia('(pointer:coarse)').matches") == (name == "finger"), (key, name)
                    # rule: the card is a strip at rest and its note takes no touch; a press raises it (and pins it) first.
                    if frame.locator(".wt-foot-more").count():
                        _raise_the_card(page, frame)
                    seen[name] = frame.evaluate(TOUCH)
                    context.close()
                where = (key, width, seen)
                assert abs(seen["finger"]["foot"] - seen["mouse"]["foot"]) <= 1, where
                assert abs(seen["finger"]["button"] - seen["mouse"]["button"]) <= 1 and seen["finger"]["button"] <= 30, where
                assert seen["finger"]["up"] and not seen["finger"]["far"] and not seen["mouse"]["up"], where
        finally:
            browser.close()


ROW_AT = """() => { const r = (e) => e.getBoundingClientRect(); const cs = (e, p) => getComputedStyle(e, p); const q = (s, from) => (from || document).querySelector(s);
  const ink = (v) => { const p = document.createElement('i'); p.style.color = 'var(' + v + ')'; document.body.appendChild(p); const c = cs(p).color; p.remove(); return c; };
  const mid = (e) => (r(e).top + r(e).bottom) / 2;
  const tabs = [...document.querySelectorAll('.rail .wt-tab')]; const stamp = q('.wt-stamp'); const n = q('.wt-n'); const sep = q('.wt-sep'); const age = q('.wt-age');
  return {width: innerWidth, ink: {primary: ink('--text-primary'), meta: ink('--text-meta')},
    tabs: tabs.map((t) => ({h: r(t).height, word: mid(q('.tab-mark-name', t)) - mid(q('.ring', t)), ringH: r(q('.ring', t)).height, ring: Math.abs(mid(q('.ring', t)) - mid(t)), capsule: cs(t, '::before').height})),
    // (the stamp stands in the plate, in the row of the figure; its baseline is read with a probe that sits on the last line of the text)
    base: (() => { const b = (e) => { const p = document.createElement('i'); p.style.cssText = 'display:inline-block;width:0;height:0;vertical-align:baseline'; e.appendChild(p); const v = r(p).bottom; p.remove(); return v; };
      return stamp && q('.wt-plate .l-fig .of') ? {stamp: b(stamp), of: b(q('.wt-plate .l-fig .of'))} : null; })(),
    place: stamp && {plate: r(q('.wt-plate')), fig: r(q('.wt-plate .l-fig')), fact: q('.wt-plate .wt-fact') ? r(q('.wt-plate .wt-fact')) : null, stamp: r(stamp), ctl: q('.wt-plate .l-chart') ? r(q('.wt-plate .l-chart')) : null, inRail: !!stamp.closest('.rail'),
      inPlate: stamp.parentNode === q('.wt-plate'), railBot: !!q('.rail-bot'), scrollW: document.documentElement.scrollWidth},
    stamp: stamp && {size: cs(stamp).fontSize, line: parseFloat(cs(stamp).lineHeight), weight: cs(stamp).fontWeight, color: cs(stamp).color, align: cs(stamp).textAlign, white: cs(stamp).whiteSpace},
    n: n && {weight: cs(n).fontWeight, color: cs(n).color, display: cs(n).display, box: r(n)},
    dot: sep && {w: r(sep).width, h: r(sep).height, radius: cs(sep).borderTopLeftRadius, bg: cs(sep).backgroundColor, right: cs(sep).marginRight, box: r(sep), anim: cs(sep).animationName,
                 words: sep.textContent},
    age: age && {weight: cs(age).fontWeight, color: cs(age).color, box: r(age)}}; }"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_words_and_the_ring_of_a_rail_row_stand_on_its_middle_it_keeps_its_height_and_the_stamp_is_12_on_16_with_a_disc_for_its_dot(fixture_file, engine):
    """kit: rail / capsule: window tabs. A row of the rail is 74 px high (the capsule of the narrow shell 32 px drawn, 44 px under a finger): its ring, with the short word
    as the badge in its gap, on its middle. The stamp (in the plate, in the row of the figure, on its right: the rail has no foot) is 12 on 16 in the meta ink and the
    regular weight, but the number read: the semibold weight in the ink; its dot is a 2 px disc at rest (5 px only while a read is in the air) of the stamp's ink with 4 px of
    air on both sides, whose words stay in it for a reader. One line at every width (the number, the dot, the age), on the baseline of the figure; tight (under 640 px) on a line of its own under the figure."""
    from test_bars import _mount_layout_frame, _let_go_the_pick
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    stamps = 0
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for key in ("reading_all", "reading_part"):
                for finger in (False, True):
                    page = browser.new_page(viewport={"width": 1400, "height": 900})
                    frame = _mount_layout_frame(page, views[key], 678)
                    # The stamp stands on the plate itself: the pick of the top account is let go (picked, the stamp stands in its card), again after the finger changes the environment.
                    _let_go_the_pick(frame)
                    if finger:
                        frame.evaluate(FINGER)
                        _let_go_the_pick(frame)
                    for width in (1280, 860, 859, 678, 360, 282):
                        page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                        frame.wait_for_function("(width) => innerWidth === width", arg=width)
                        page.wait_for_timeout(100)
                        at = frame.evaluate(ROW_AT)
                        wide = width >= 860
                        where = (key, finger, width, at)
                        height = 74 if wide else 44 if finger else 32
                        assert at["tabs"], where
                        for tab in at["tabs"]:
                            # the short name is the badge in the gap of the ring on the wide rail: its middle is 0.44 of the ring's height under the ring's middle (the lowest
                            # point of the path, 173 of the 184 of the viewBox, less the middle); in a capsule the word and the ring share one middle
                            assert abs(tab["h"] - height) <= 0.6 and tab["ring"] <= 1, where
                            assert abs(tab["word"] - (173 / 184 - 0.5) * tab["ringH"] if wide else tab["word"]) <= 1, where
                            if not wide:
                                assert tab["capsule"] == "32px", where
                        stamp, n, dot, age, ink = at["stamp"], at["n"], at["dot"], at["age"], at["ink"]
                        assert stamp and n and dot and age, where
                        stamps += 1
                        assert stamp["size"] == "12px" and 16 <= stamp["line"] <= 16.4 and stamp["weight"] == "400" and stamp["color"] == ink["meta"], where
                        assert n["weight"] == "400" and n["color"] == ink["primary"], where  # "15 of 20": the regular weight (part 3 of the top), still the primary ink
                        assert age["weight"] == "400" and age["color"] == ink["meta"], where
                        # the dot is a 2 px disc at rest (--sep-size: as quiet as the " · " of the notes; 5 px only while a read is in the air) of the stamp's ink with 4 px of air on
                        # both sides (a margin), and one line: the number, the dot, the age
                        assert (dot["w"], dot["h"], dot["radius"], dot["right"]) == (2, 2, "50%", "4px") and dot["bg"] == ink["meta"], where
                        assert dot["anim"] == "none" and dot["words"] == " \u00b7 ", where
                        assert n["display"] == "inline" and abs(age["box"]["top"] - n["box"]["top"]) <= 1, where
                        assert abs(dot["box"]["left"] - n["box"]["right"] - 4) <= 1.5 and abs(age["box"]["left"] - dot["box"]["right"] - 4) <= 1.5, where
                        # The stamp stands in the plate (not in the rail: the rail has no foot), in the row of the figure, on its right; tight (under 640 px) on a line of its
                        # own under the figure, at the left. No horizontal scroll at any width. (The top of the plate, part 3: the figure is the number and its two lines of words,
                        # no bubble; .l-fig is display:contents, a box of nothing, so the span of the figure is the span of .wt-fact.)
                        place = at["place"]
                        figure = place["fact"] or place["fig"]
                        assert place["inPlate"] and not place["inRail"] and not place["railBot"] and place["scrollW"] <= width, where
                        assert place["stamp"]["left"] >= place["plate"]["left"] - 0.5 and place["stamp"]["right"] <= place["plate"]["right"] + 0.5, where
                        if width >= 640:
                            assert stamp["align"] == "right" and stamp["white"] == "nowrap", where
                            assert place["stamp"]["left"] >= figure["right"] - 0.5, ("right of the figure", where)
                            if place["ctl"]:
                                assert place["stamp"]["right"] <= place["ctl"]["left"] + 0.5, ("before the chart word", where)
                            # (the stamp stands in the row of the figure, on the right: level with the chart word - the middle of the two within 2px - and within the vertical span
                            # of the figure, which is the row of the number)
                            assert place["stamp"]["top"] >= figure["top"] - 0.5 and place["stamp"]["bottom"] <= figure["bottom"] + 0.5, ("in the row of the figure", where)
                            if place["ctl"]:
                                assert abs((place["stamp"]["top"] + place["stamp"]["bottom"]) / 2 - (place["ctl"]["top"] + place["ctl"]["bottom"]) / 2) <= 2, ("level with the chart word", where)
                        else:
                            assert stamp["align"] == "left" and place["stamp"]["top"] >= figure["bottom"] - 0.5 and abs(place["stamp"]["left"] - figure["left"]) <= 1, ("tight: a line of its own under the figure, at the left edge of the figure", where)
                    page.close()
        finally:
            browser.close()
    assert stamps >= 8


HOLD = """() => { const r = (e) => e.getBoundingClientRect(); const q = (s) => document.querySelector(s);
  const foot = q('.wt-foot');
  return {hold: !!q('.wt-foot-hold'), display: foot ? getComputedStyle(foot).display : null, visibility: foot ? getComputedStyle(foot).visibility : null,
    foot: foot ? r(foot).height : 0, tabs: [...document.querySelectorAll('.wt-tab')].map((t) => r(t).top), plate: r(q('.wt-plate')).top, card: r(q('.shell-card')).top,
    tabsEnd: Math.max(...[...document.querySelectorAll('.rail .wt-tab')].map((t) => r(t).bottom)), rail: r(q('.rail')).bottom,
    inCard: !!foot && !!foot.closest('.shell-card .wt-plate') && !foot.closest('.rail'),
    first: !!foot && !!foot.parentElement && [...foot.parentElement.children].indexOf(foot) > [...foot.parentElement.children].indexOf(foot.parentElement.querySelector(':scope > .wt-fact')),
    above: foot ? r(foot).top >= r(q('.wt-plate')).top - 1 && r(foot).bottom <= r(q('.wt-plate')).bottom + 1 : null}; }"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_hidden_copy_keeps_the_place_of_the_note_on_the_wide_rail_and_takes_none_under_860px(fixture_file, engine):
    """A window with accounts not read draws a note under the rows; where another window has one and the picked window has none, a hidden copy of
    it stands in its place. From 860 px (the rail is a column beside the card) the copy keeps the note's height: the rows and the plate
    stay where they were along the rows. Under 860 px the rail is a row over the card and the copy would be an empty band under the tabs: it takes
    no room (display none), the rail ends at its last tab (the stamp is in the plate), and the card moves by the height of a note only where a note is drawn."""
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            # The 5-hour of reading_two wears the star (3 of 4 read); the 5-hour of reading_zero read none of its accounts (the dash, no
            # star, a note all the same): moving to the weekly, read whole, the weekly has a copy of the 5-hour's note, and no note of its own.
            for key in ("reading_two", "reading_zero"):
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                frame = _mount_layout_frame(page, views[key], 678)
                for width in (320, 360, 678, 859, 860, 1000, 1280):
                    page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                    frame.wait_for_function("(width) => innerWidth === width", arg=width)
                    wide = width >= 860
                    frame.click('[data-focus="win:claude:18000"]')
                    page.wait_for_timeout(350)
                    # rule: the note (and its hidden copy) stands in the card's sum, which is closed while the card is a strip: raise (pin) the card first.
                    _keep_the_card_up(page, frame)
                    with_note = frame.evaluate(HOLD)
                    where = (key, width, with_note)
                    assert not with_note["hold"] and with_note["foot"] > 0 and with_note["display"] != "none", where
                    # the note is a child of the plate (the card), after the number, inside the plate's box, and not in the rail
                    assert with_note["inCard"] and with_note["first"] and with_note["above"], ("the note in the plate of the card", where)
                    frame.click('[data-focus="win:claude:604800"]')
                    page.wait_for_timeout(350)
                    _keep_the_card_up(page, frame)
                    held = frame.evaluate(HOLD)
                    where = (key, width, with_note, held)
                    assert held["hold"], where
                    # the note stands in the plate (the card) at every width, so its copy keeps its place at every width: the plate, the card
                    # and the rows of the rail do not move as the window changes
                    assert held["display"] != "none" and held["visibility"] == "hidden", where
                    assert held["inCard"] and held["first"] and held["above"], ("the copy stands where the note stood", where)
                    assert abs(held["foot"] - with_note["foot"]) <= 1, where
                    assert abs(held["plate"] - with_note["plate"]) <= 1 and abs(held["card"] - with_note["card"]) <= 1, where
                    assert all(abs(a - b) <= 1 for a, b in zip(held["tabs"], with_note["tabs"])), where
                    assert abs(held["rail"] - held["tabsEnd"]) <= 1 or wide, where  # (under 860px the rail ends at its last row of tabs, it has no foot: no empty band under it)
                    # and back: the same places, with the note's own words
                    frame.click('[data-focus="win:claude:18000"]')
                    page.wait_for_timeout(350)
                    _keep_the_card_up(page, frame)
                    back = frame.evaluate(HOLD)
                    assert not back["hold"] and abs(back["plate"] - with_note["plate"]) <= 1 and abs(back["card"] - with_note["card"]) <= 1, (key, width, back)
                page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_a_group_of_the_note_keeps_the_bars_of_its_own_accounts_bright(fixture_file, engine):
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    seen_by = "() => [...document.querySelectorAll('.bars .bar')].map((b) => [b.getAttribute('data-account').split(':')[1], getComputedStyle(b).opacity])"
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            frame = _mount_layout_frame(page, views["reading_many"], 678)
            groups = frame.locator(".wt-foot-g")
            assert groups.count() == 3
            # rule: the note of the windows stands in the card's sum, which is hidden while the card is a strip: raise (pin) the card to see the groups.
            _raise_the_card(page, frame)
            # The group of the accounts that could not be read: only c10 stays bright - not c11, whose reading is also unknown for another
            # reason, which the dimming by the kind of bar left bright too.
            groups.nth(1).hover()
            page.wait_for_timeout(300)
            assert frame.evaluate(DIMS) == ["group"]
            assert {k: o for k, o in frame.evaluate(seen_by) if o == "1"} == {"c10": "1"}, frame.evaluate(seen_by)
            # The other unknown group, then the last known one (five accounts, three named): the bars of all five stay.
            groups.nth(2).hover()
            page.wait_for_timeout(300)
            assert sorted(k for k, o in frame.evaluate(seen_by) if o == "1") == ["c11"]
            groups.nth(0).hover()
            page.wait_for_timeout(300)
            assert sorted(k for k, o in frame.evaluate(seen_by) if o == "1") == ["c01", "c02", "c03", "c04", "c05"]
            page.mouse.move(2, 2)
            page.wait_for_timeout(300)
            assert frame.evaluate(DIMS) == []
            assert all(o == "1" for _, o in frame.evaluate(seen_by))
            assert frame.evaluate("() => document.querySelectorAll('[data-lit]').length") == 0
            # The keyboard does the same as the pointer: the ring showing, the group is lit; away from it, nothing is left.
            frame.focus(".wt-foot-g >> nth=1")
            page.keyboard.press("Shift+Tab")
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)
            assert frame.evaluate("() => document.activeElement.matches(':focus-visible')")
            assert sorted(k for k, o in frame.evaluate(seen_by) if o == "1") == ["c10"]
            frame.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")
            page.wait_for_timeout(300)
            assert frame.evaluate(DIMS) == []
            assert frame.evaluate("() => document.querySelectorAll('[data-lit]').length") == 0
            page.close()
        finally:
            browser.close()


# What the small bar of the legend and a bar of the strip show on their base, read off a screenshot of each: the runs of
# the mark's ink along the middle of the 3 px base, in css px. (Not the css: the engines drew one dashed border differently.)
BASE_RUNS = """async ([b64, scale]) => {
  const img = new Image(); img.src = 'data:image/png;base64,' + b64; await img.decode();
  const canvas = document.createElement('canvas'); canvas.width = img.width; canvas.height = img.height;
  const ctx = canvas.getContext('2d', { willReadFrequently: true }); ctx.drawImage(img, 0, 0);
  const own = ctx.getImageData(Math.floor(img.width / 2), Math.floor(img.height / 3), 1, 1).data;
  const row = ctx.getImageData(0, img.height - Math.round(1.5 * scale), img.width, 1).data;
  const ink = (x) => Math.abs(row[4 * x] - own[0]) + Math.abs(row[4 * x + 1] - own[1]) + Math.abs(row[4 * x + 2] - own[2]) > 150;
  const runs = []; let start = -1;
  for (let x = 0; x <= img.width; x++) {
    const on = x < img.width && ink(x);
    if (on && start < 0) start = x;
    if (!on && start >= 0) { runs.push([start / scale, x / scale]); start = -1; }
  }
  return runs;
}"""


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_legend_holds_no_small_bar_and_the_strips_bar_at_the_limit_when_last_read_shows_a_dashed_base(fixture_file, engine, theme):
    import base64

    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    scale = 4
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 900}, device_scale_factor=scale)
            frame = _mount_layout_frame(page, views["codex_states"], 678, theme=theme)
            page.wait_for_timeout(300)

            def runs(selector):
                png = frame.locator(selector).first.screenshot()
                return page.evaluate(BASE_RUNS, [base64.b64encode(png).decode(), scale])

            # The legend holds no small bar (an item is its number and its words; the bars above are its sample), in both engines. Its items are "disabled" and
            # "at the limit when last read" (the item of the last read stays: it is the only word of the dashed bar; the "last known" one goes, the note names it).
            assert frame.locator(".lg .bar").count() == 0 and frame.locator(".lg .legend-item").count() == 2
            assert frame.locator(".lg .legend-item").evaluate_all("(items) => items.map((e) => e.dataset.k)") == ["held", "was"]
            # The strip's bar keeps its dashed base: dashes, not a line, whatever their number in each engine (the bar is a tube now, 56 px wide at its widest, and its round
            # ends cut the dash nearest to each end at the row measured, so WebKit leaves two whole dashes in it where the bar of 20 px held three).
            real = runs(".bars .bar.was")
            assert 2 <= len(real) <= 6 and all(end - start <= 9 for start, end in real), (engine, theme, real)
        finally:
            browser.close()


def test_the_graph_under_the_plate_is_written_in_the_kits_parts_and_only_the_differences():
    source, css, _ = _capsule()
    # No head and no tab strip of its own in the graph: the window is picked in the row above (the one
    # tl-head left is that row's, tl-head wt-bar), and no tab of the window or of Combined repeats it.
    for gone in ("timelineTabs", "tabStrip", "pickTab", "What the graph shows", "'tab:'"):
        assert gone not in source, gone
    assert "el('div', 'tl-head')" not in source
    assert "el('div', 'tl-controls wt-controls')" in source
    # Combined is one switch in that row: the kit's segment with its one option, pressed or not.
    switch = source.split("function jointSwitch() {", 1)[1].split("\n    }\n", 1)[0]
    assert "el('div', 'seg')" in switch and "el('button', 'seg-opt' + (on ? ' active' : ''), t('chart.combined'))" in switch
    assert "aria-pressed" in switch and "role" not in switch and "b.title = " in switch
    # The headline is drawn for the combined view only (its numbers are not the plate's), and the model choice follows
    # the other controls in the same row.
    assert "if (shown.joint_note) box.appendChild(timelineHero(shown, sc));" in source
    assert "timelineHero(g," not in source and "account-windows left';" not in source.split("function timelineHero(", 1)[1].split("\n    }\n", 1)[0]
    block = source.split("function timelineBlock(ctx, g) {", 1)[1]
    assert block.index("jointSwitch()") < block.index("menu.button") < block.index("box.appendChild(controls)")
    # Written here is only what differs from the kit, in the kit's variables: no colour, no motion, no display or gap
    # (the kit's .tl-controls is the row, .seg its segments, .tl-model its button). The four rules are the chart's, under the
    # window-tabs plate, and no other rule of the style names the row.
    rules = [r for r in _style_rules(css) if ".wt-controls" in r]
    assert len(rules) == 4, rules
    assert [r.partition("{")[0] for r in rules] == [".tl-block .tl-controls.wt-controls", ".wt-controls>.tl-model",
                                                    ".wt-controls+.tl-model-panel", ".wt-controls~.tl-hero"]
    for rule in rules:
        assert not re.search(r"color|background|#[0-9a-fA-F]{3}|rgba?\(|transition|animation|display:|gap:|font", rule), rule
    chart = _section(css, "capsule: chart")
    assert all(rule in chart for rule in rules), "the four rules stand in the chart's capsule"
    assert ".tl-block .tl-controls.wt-controls{justify-content:flex-start;margin-top:0}" in chart
    assert ".wt-controls>.tl-model{margin:0 0 0 auto}" in chart


def test_the_captions_the_idle_option_and_the_folded_method_are_kit_rules_and_say_nothing_new_in_the_capsule():
    source, css, capsule = _capsule()
    kit = css.split("/* capsule: window tabs */", 1)[0]
    rules = (".tl-block .tl-controls .seg-label{padding-left:var(--space-2)}",
             ".tl-block .tl-controls .seg-opt.idle:not(.active){border-style:dashed;border-color:var(--edge-idle)}",
             ".chart-how{margin-top:var(--space-2)}", ".chart-how>summary{color:var(--text-meta)}")
    for rule in rules:
        assert rule in kit and rule not in capsule, rule
        assert not re.search(r"gradient\(|font-size:(?:[0-9]|1[01])(?:\.\d+)?px", rule), rule
    assert len([r for r in _style_rules(css) if ".wt-controls" in r]) == 4, "the plate's rules for the graph's row stay four"
    # Span and Future are captioned in view: neither segment is asked for quiet, and the caption has no data-focus.
    block = source.split("function timelineBlock(ctx, g) {", 1)[1].split("\n    }\n", 1)[0]
    assert "}, true));" not in block and block.count("segControl(") == 2
    seg = source.split("function segControl(", 1)[1].split("\n    }\n", 1)[0]
    assert "el('span', quiet ? 'seg-label sr-only' : 'seg-label', label)" in seg and "data-focus', focusPrefix" in seg
    # The fold in the Details has its own state, its summary takes the keyboard like the Details' own.
    assert "var howOpen = false;" in source and "howSummary.setAttribute('data-focus', 'chart-how');" in source


CONTROLS_GEOMETRY = r"""() => {
  const q = (s, r = document) => r.querySelector(s);
  const box = (e) => { if (!e) return null; const b = e.getBoundingClientRect(); return {left: b.left, right: b.right, top: b.top, bottom: b.bottom}; };
  const block = q('.tl-block'), row = q('.tl-controls'), plot = q('.chart-plot');
  const ruled = (n, side) => { const s = getComputedStyle(n); const c = s['border' + side + 'Color'];
    return parseFloat(s['border' + side + 'Width']) > 0 && s['border' + side + 'Style'] !== 'none' && !/^rgba\(.*,\s*0\)$|^transparent$/.test(c); };
  const above = [...block.querySelectorAll('*')].filter((n) => !n.contains(plot) && (n.compareDocumentPosition(plot) & Node.DOCUMENT_POSITION_FOLLOWING));
  return {width: innerWidth, scroll: document.documentElement.scrollWidth, plate: box(q('.wt-plate')), bars: box(q('.wt-plate>.bars')), row: box(row),
          rowScroll: [row.scrollWidth, row.clientWidth], switch: box(q('[data-focus="graph:joint"]')), model: box(q('.tl-model')),
          panel: box(q('.tl-model-panel')), hero: box(q('.tl-hero')), plot: box(plot), heroes: document.querySelectorAll('.tl-hero').length,
          heads: block.querySelectorAll('.tl-head,.tl-tab,.tl-tabs,[role=tab],[role=tablist]').length,
          pressed: q('[data-focus="graph:joint"]').getAttribute('aria-pressed'),
          rules: above.filter((n) => n.tagName !== 'BUTTON' && (ruled(n, 'Top') || ruled(n, 'Bottom'))).map((n) => n.className)};
}"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_graph_continues_the_plate_in_one_row_of_controls(fixture_file, engine):
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())

    def apart(a, b):
        return a["right"] <= b["left"] + 1 or b["right"] <= a["left"] + 1 or a["bottom"] <= b["top"] + 1 or b["bottom"] <= a["top"] + 1

    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_layout_frame(page, views["claude"], 678, theme="dark")
            # Both sides of the narrow breakpoint, the narrowest card and the owner's: the row wraps, never scrolls.
            for width in (320, 360, 519, 520, 521, 678):
                page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                frame.wait_for_function("(width) => innerWidth === width", arg=width)
                if frame.evaluate("() => document.querySelector('[data-focus=\"graph:joint\"]').getAttribute('aria-pressed')") == "true":
                    frame.click('[data-focus="graph:joint"]')
                g = frame.evaluate(CONTROLS_GEOMETRY)
                # The window mode: the chart stands in the plate (the top account is picked as the window opens), right under the bars, and in it the row at once; no head and
                # no line of its own, no headline, no tab.
                assert g["scroll"] <= g["width"] + 1 and g["rowScroll"][0] <= g["rowScroll"][1] + 1, (width, g)
                assert g["heads"] == 0 and g["heroes"] == 0 and g["rules"] == [] and g["pressed"] == "false", (width, g)
                assert g["plate"]["top"] <= g["bars"]["top"] and g["row"]["bottom"] <= g["plate"]["bottom"] + 1, ("the row is in the plate", width, g)
                assert 8 <= g["row"]["top"] - g["bars"]["bottom"] <= 40, ("the row follows the bars at once", width, g)
                assert g["plot"]["top"] > g["row"]["bottom"], (width, g)
                for part in (g["switch"], g["model"]):
                    assert g["row"]["left"] - 1 <= part["left"] and part["right"] <= g["row"]["right"] + 1, (width, g)
                assert apart(g["switch"], g["model"]), (width, g)
                assert abs(g["model"]["right"] - g["row"]["right"]) <= 1, ("the model choice takes the row's right end", width, g)
                # Combined on: the headline stands between the row and the plot, and the switch itself does not move.
                frame.click('[data-focus="graph:joint"]')
                frame.wait_for_selector(".tl-hero")
                on = frame.evaluate(CONTROLS_GEOMETRY)
                assert on["pressed"] == "true" and on["heroes"] == 1 and on["heads"] == 0 and on["rules"] == [], (width, on)
                assert on["row"]["bottom"] <= on["hero"]["top"] and on["hero"]["bottom"] <= on["plot"]["top"], (width, on)
                assert abs(on["switch"]["left"] - g["switch"]["left"]) <= 1 and abs(on["switch"]["top"] - g["switch"]["top"]) <= 1, (width, g, on)
                assert on["scroll"] <= on["width"] + 1, (width, on)
                # The model list opens under the row, not over it.
                frame.click('[data-focus="tl-model"]')
                frame.wait_for_selector(".tl-model-panel")
                open_ = frame.evaluate(CONTROLS_GEOMETRY)
                assert open_["panel"]["top"] >= open_["row"]["bottom"] + 4 and open_["panel"]["bottom"] <= open_["hero"]["top"] + 1, (width, open_)
                frame.click('[data-focus="tl-model"]')
                frame.click('[data-focus="graph:joint"]')
            assert errors == [], errors
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# The bars of the plate in a real browser: a finger's 44 px, the pick of a bar that is at the limit, the pointer's ink.
TOUCH_BARS = """() => { const r = (e) => e.getBoundingClientRect(); const strip = document.querySelector('.bars');
  const bars = [...strip.querySelectorAll('.bar')]; const b = r(bars[0]);
  const over = document.elementFromPoint(b.left + b.width / 2, b.top - 4);
  // The gap between two bars as it is drawn (5px, --bar-gap: from the padding box of one to the padding box of the next): under a finger the row has no gap of its
  // own and each bar wears half of it as a transparent border, so a point in the drawn gap belongs to a bar beside it; under a mouse it belongs to the strip.
  const c = r(bars[1]); const mid = b.top + b.height / 2;
  const side = (e, s) => parseFloat(getComputedStyle(e)['border' + s + 'Width']) || 0;
  const gl = b.right - side(bars[0], 'Right'); const gr = c.left + side(bars[1], 'Left');
  const inGap = [gl + 1.0, gr - 1.0].map((x) => { const e = document.elementFromPoint(x, mid); return !!e && e.classList.contains('bar'); });
  const cs = getComputedStyle(bars[0]); const st = getComputedStyle(strip);
  return {coarse: matchMedia('(pointer:coarse)').matches, strip: r(strip).height, bars: bars.map((x) => r(x).height), over: over === bars[0], n: bars.length,
          gap: gr - gl, inGap, rowGap: parseFloat(st.columnGap) || 0, border: [side(bars[0], 'Left'), side(bars[0], 'Right')],
          clip: cs.backgroundClip, visible: bars.map((x) => x.clientWidth), least: cs.minWidth}; }"""


def test_real_browser_a_finger_gets_a_44px_row_of_bars_and_a_zone_over_each_bar_and_a_mouse_keeps_its_40px(fixture_file):
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, "chromium")
        try:
            seen = {}
            for name, options in (("mouse", {}), ("finger", {"has_touch": True, "is_mobile": True})):
                context = browser.new_context(viewport={"width": 1100 if name == "mouse" else 390, "height": 900}, device_scale_factor=2, **options)
                frame = _mount_layout_frame(context.new_page(), views["codex_states"], 390)
                assert frame.evaluate("() => matchMedia('(pointer:coarse)').matches") == (name == "finger")
                seen[name] = frame.evaluate(TOUCH_BARS)
                context.close()
            assert seen["mouse"]["n"] == seen["finger"]["n"] == 7
            assert seen["mouse"]["strip"] == 88 and not seen["mouse"]["over"], seen  # the row is 88 px (--bars-h) for a mouse as for a finger
            # the strip is 44 px with its one-pixel base line, and each bar fills it above that line; the zone over a bar reaches above it
            assert seen["finger"]["strip"] >= 44 and all(h >= seen["finger"]["strip"] - 1 for h in seen["finger"]["bars"]), seen
            # (no zone over the bar for a finger any more: the row is 88 px, over the 44 px tap-h, and a row that scrolls sideways cuts what stands above the bars)
            assert not seen["finger"]["over"], seen
            # ...and under a finger the row has no gap of its own: each bar wears half of the drawn gap as a transparent border (the ground is clipped to the
            # padding box), so no touch falls between two bars; the drawn gap is the 5 px of a mouse (the engine snaps a border to whole device pixels: 2.5 px
            # is 2 px on a 1x screen, 4 px drawn), and a mouse has no such border
            assert 3.9 <= seen["finger"]["gap"] <= 5.5 and seen["finger"]["inGap"] == [True, True], seen
            assert seen["finger"]["rowGap"] == 0 and all(2 <= w <= 2.5 for w in seen["finger"]["border"]) and seen["finger"]["clip"] == "padding-box", seen
            assert seen["finger"]["least"] == "25px", seen  # --bar-min (20px) and --bar-gap (5px): the border is part of the width
            assert 4.5 <= seen["mouse"]["gap"] <= 5.5 and seen["mouse"]["inGap"] == [False, False], seen
            assert seen["mouse"]["rowGap"] == 5 and seen["mouse"]["border"] == [0, 0] and seen["mouse"]["least"] == "20px", seen
            # the drawn bar is as wide as the room gives it at least --bar-min, with the border or without
            assert all(w >= 19.5 for w in seen["finger"]["visible"] + seen["mouse"]["visible"]), seen
        finally:
            browser.close()


SELECTED_BAR = """() => { const r = (e) => e.getBoundingClientRect(); const probe = document.createElement('i'); probe.style.color = 'var(--accent)';
  document.body.appendChild(probe); const accent = getComputedStyle(probe).color; probe.remove();
  const bar = document.querySelector('.bars .bar.sel'); if (!bar) return null;
  const rest = [...document.querySelectorAll('.bars .bar.spent:not(.sel)')][0];
  return {cls: bar.className, color: getComputedStyle(bar).color, accent, transform: getComputedStyle(bar).transform,
          base: getComputedStyle(bar, '::after').backgroundColor, height: r(bar).height, rest: rest ? r(rest).height : null,
          fill: !!bar.querySelector('.fill')}; }"""
HOVER_INK = """() => { const bar = document.querySelector('.bars .bar.hov'); const fill = bar.querySelector('.fill');
  const probe = document.createElement('i'); probe.style.color = 'var(--accent)'; document.body.appendChild(probe);
  const accent = getComputedStyle(probe).color; probe.remove();
  return {fill: getComputedStyle(fill).backgroundColor, ink: getComputedStyle(bar).color, accent}; }"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_picked_bar_is_seen_even_when_it_is_empty_at_the_limit_and_the_pointer_turns_a_bar_to_its_ink(fixture_file, engine):
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            frame = _mount_layout_frame(page, views["codex_states"], 678)
            # The pointer: a bar's fill turns to the bar's ink (the primary one), never the accent (that is the pick's).
            # (not the picked one: the top account is picked as the window opens, and the pick's ink is the accent, which this step tells from the pointer's)
            frame.evaluate("() => [...document.querySelectorAll('.bars .bar')].find((b) => b.querySelector('.fill') && !b.classList.contains('last') && !b.classList.contains('sel')).classList.add('hov')")
            frame.hover('.bars .bar.hov')
            page.wait_for_timeout(400)
            ink = frame.evaluate(HOVER_INK)
            assert ink["fill"] == ink["ink"] and ink["ink"] != ink["accent"], ink
            page.mouse.move(2, 2)
            # The pick: a bar at the limit now has no fill at all, only its base; picked, the whole bar grows from its base and its ink
            # (the base, the ring of the unknown ones) is the accent - seen against the neighbour it stands among.
            frame.click('.bars .bar.spent >> nth=0')
            page.wait_for_timeout(500)
            picked = frame.evaluate(SELECTED_BAR)
            assert picked and "spent" in picked["cls"] and not picked["fill"], picked
            assert picked["color"] == picked["accent"] and picked["base"] == picked["accent"], picked
            # The picked bar does not grow (part 1 of the top: in a row that scrolls sideways a growth is cut): no transform, the height of its neighbours.
            assert picked["transform"] in ("none", "") and picked["rest"], picked
            assert abs(picked["height"] - picked["rest"]) <= 0.5, picked
            page.close()
        finally:
            browser.close()


# An unknown account's bar is a ring with a "?": picked, it turns red and is open as a bar the pointer is on (the picked bar is open at all times, on every pointer: the rule of
# :hover, kept by .sel), its height, its '?' and its place not changing (no transform: nothing grows by scale); wide or narrow.
UNKNOWN_BAR = """() => new Promise((resolve) => {
  const r = (e) => e.getBoundingClientRect();
  const bar = document.querySelector('.bars .bar.unknown'); if (!bar) { resolve(null); return; }
  const probe = document.createElement('i'); probe.style.color = 'var(--accent)'; document.body.appendChild(probe);
  const accent = getComputedStyle(probe).color; probe.remove();
  const read = () => { const b = r(bar), q = getComputedStyle(bar, '::after');
    return {w: b.width, h: b.height, transform: getComputedStyle(bar).transform, color: getComputedStyle(bar).color, shadow: getComputedStyle(bar).boxShadow,
            qTransform: q.transform, qColor: q.color, qSize: q.fontSize}; };
  const before = read(); bar.classList.add('sel');
  setTimeout(() => resolve({before, after: read(), accent, narrow: matchMedia('(max-width:640px)').matches}), 500);
})"""


@pytest.mark.parametrize("width", [678, 390])
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_picked_unknown_bar_turns_red_and_is_open_as_under_the_pointer_wide_or_narrow(fixture_file, engine, width):
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": max(width, 1100) if width > 640 else width, "height": 900})
            frame = _mount_layout_frame(page, views["cursor_mixed"], width)
            seen = frame.evaluate(UNKNOWN_BAR)
            assert seen, "the Cursor plate with an unknown account has an unknown bar"
            assert seen["narrow"] == (width <= 640), seen
            before, after = seen["before"], seen["after"]
            # It is open as a bar the pointer is on is (the same width, to the half pixel), on a narrow shell too; its height does not change, its '?' keeps its size and is not
            # scaled, and no transform is on it.
            frame.evaluate("() => document.querySelector('.bars .bar.unknown').classList.remove('sel')")
            frame.hover('.bars .bar.unknown')
            page.wait_for_timeout(500)
            under_pointer = frame.evaluate("() => document.querySelector('.bars .bar.unknown').getBoundingClientRect().width")
            assert after["w"] > before["w"] + 1 and abs(after["w"] - under_pointer) < 0.5, (seen, under_pointer)
            assert abs(after["h"] - before["h"]) < 0.5, seen
            assert after["transform"] in ("none", "") and after["qTransform"] in ("none", ""), seen
            assert after["qSize"] == before["qSize"], seen
            # It turns to the accent: the ring and its '?' (and was not red before).
            assert before["color"] != seen["accent"] and after["color"] == seen["accent"] and after["qColor"] == seen["accent"], seen
            assert seen["accent"] in after["shadow"] and seen["accent"] not in before["shadow"], seen
            page.close()
        finally:
            browser.close()


RAIL_GEOMETRY = """() => {
  const q = (s, r = document) => r.querySelector(s);
  const box = (n) => { if (!n) return null; const r = n.getBoundingClientRect(); return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height}; };
  const px = (v) => parseFloat(v); const cs = (n) => getComputedStyle(n);
  const tabs = [...document.querySelectorAll('.rail .tab-mark-rail')]; const row = q('.rail .tab-mark-row');
  const card = q('.shell-card'), first = q('.lrows > :first-child'), last = q('.lrows > :last-child'), picked = q('.rail .tab-mark-rail[aria-selected=true]');
  return {width: innerWidth, scroll: document.documentElement.scrollWidth, rail: box(q('.rail')), row: box(row), rowScroll: row.scrollWidth - row.clientWidth,
    card: box(card), plate: box(q('.wt-plate')), lead: cs(q('.rail-lead')).display, stamp: box(q('.wt-stamp')), stampIn: q('.wt-stamp') ? (q('.wt-stamp').parentNode === q('.wt-plate') ? 'plate' : q('.wt-stamp').closest('.rail') ? 'rail' : 'other') : null,
    railBot: !!q('.rail-bot'), foot: box(q('.wt-foot')),
    ring: picked ? new DOMMatrix(cs(innerWidth >= 860 ? q('.tab-mark-ring', picked) : q('.ring', picked)).transform).a : null,
    corners: {card: px(cs(card).borderTopLeftRadius), pad: px(cs(card).paddingTop), tl: px(cs(first).borderTopLeftRadius), tr: px(cs(first).borderTopRightRadius),
              bl: px(cs(last).borderBottomLeftRadius), br: px(cs(last).borderBottomRightRadius),
              firstPlate: first.classList.contains('tl-block'), lastPlate: last.classList.contains('tl-block')},
    widest: Math.max(0, ...[...document.querySelectorAll('.harness-seg [data-mark-key]')].map((t) => t.offsetWidth)),
    ringBox: picked && innerWidth >= 860 ? box(q('.tab-mark-ring', picked)) : null, ladder: box(q('.rail-ladder')),
    tabs: tabs.map((t) => { const n = q('.tab-mark-name', t); return Object.assign(box(t), {whole: n.scrollWidth <= n.clientWidth + 1}); })};
}"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_rail_is_a_column_from_860px_and_a_row_of_capsules_under_it_and_the_page_never_scrolls_sideways(fixture_file, engine):
    """kit: rail / capsule: shell. From 860px the rail stands left of the card (--rail-w wide, as tall as the card, one tab under the other, the
    rail has no foot: the stamp is in the plate); under it the same markup is a row of capsules over the card that wraps onto the next line (no scroll inside it,
    no fade), the caption gone, the rail ends at its last row. At 282, 360, 520, 678, 859, 860, 1020 and 1280 the page has no sideways scroll, every window
    stands whole inside the frame and none of its names is torn; the picked one's ring is x1.2; under a finger a row is 44 px; the corners of the
    gray block (the plate and what is under it) are the card's corner less the card's padding, all four."""
    from test_bars import _mount_layout_frame, _let_go_the_pick
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for key in ("reading_part", "reading_many", "codex_states", "cursor_budget"):
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                frame = _mount_layout_frame(page, views[key], 678)
                # The stamp stands in the plate (stampIn): the pick of the top account is let go first (picked, the stamp stands in its card).
                _let_go_the_pick(frame)
                for width in (282, 360, 520, 678, 859, 860, 1020, 1280):
                    page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                    frame.wait_for_function("(width) => innerWidth === width", arg=width)
                    g = frame.evaluate(RAIL_GEOMETRY)
                    where = (key, width, g)
                    assert g["scroll"] <= g["width"] + 1, where
                    assert g["tabs"] and all(t["whole"] for t in g["tabs"]), where
                    # no window is cut: each stands whole inside the frame, and the row has nothing to scroll
                    assert all(t["left"] >= -0.5 and t["right"] <= g["width"] + 0.5 for t in g["tabs"]), where
                    assert g["rowScroll"] <= 1, where
                    # the picked window's ring is x1.2 (--mark-on); no row is picked in a card without a window to pick
                    assert g["ring"] is None or abs(g["ring"] - 1.2) < 0.01, where
                    if width >= 860:
                        # rule: the rail is what the ring needs at the axis of the widest tab of the head's row: half of that tab, half of the ring (47px),
                        # the growth of the ring that is picked (x1.2) and 8px of air at the card; not 72px, and not the whole widest tab and the gap of the page
                        assert abs(g["rail"]["width"] - (g["widest"] / 2 + 47 / 2 + 47 * 0.2 / 2 + 8)) <= 1 and abs(g["rail"]["right"] - g["card"]["left"]) <= 1, where
                        # the picked ring stands whole in the rail with the air at the card at least, and the ladder's box (on the axis) is whole in it too
                        assert g["ringBox"] is None or (g["ringBox"]["left"] >= g["rail"]["left"] and g["rail"]["right"] - g["ringBox"]["right"] >= 7.5), where
                        assert g["ladder"] is None or (g["ladder"]["left"] >= g["rail"]["left"] - 0.5 and g["ladder"]["right"] <= g["rail"]["right"] + 0.5), where
                        assert abs(g["rail"]["height"] - g["card"]["height"]) <= 1, where
                        assert g["lead"] != "none" and len({round(t["left"]) for t in g["tabs"]}) == 1, where   # the thread from the head is drawn on the wide shell
                        assert all(abs(t["height"] - 74) <= 1 for t in g["tabs"]), where
                        assert g["rail"]["bottom"] >= max(t["bottom"] for t in g["tabs"]) - 1, where   # (the rail is as tall as the card: rings, then the ladder; no foot)
                    else:
                        assert g["lead"] == "none", where   # and there is none on the narrow one
                        assert 7 <= g["card"]["top"] - g["rail"]["bottom"] <= 9, where
                        assert all(t["height"] <= 44 for t in g["tabs"]), where
                        # the row wraps whole capsules: the lines are a 6 px gap apart, and the rail ends at the last of them (the stamp is in the plate)
                        tops = sorted({round(t["top"]) for t in g["tabs"]})
                        for above, below in zip(tops, tops[1:]):
                            assert abs((below - above) - (32 + 6)) <= 1.5, where
                        assert abs(g["rail"]["bottom"] - max(t["bottom"] for t in g["tabs"])) <= 1.5, where
                    if g["stamp"] and width >= 640:
                        assert g["stamp"]["height"] <= 17.5, ("one line", where)
                    assert g["plate"]["left"] >= g["card"]["left"] - 1 and g["plate"]["right"] <= g["card"]["right"] + 1, where
                    # The stamp is in the plate (the rail has no foot, on all widths), whole inside it and the card: never cut, no scroll across
                    assert g["stampIn"] == "plate" and not g["railBot"], where
                    assert g["stamp"]["left"] >= g["plate"]["left"] - 1 and g["stamp"]["right"] <= g["plate"]["right"] + 1 and g["stamp"]["top"] >= g["plate"]["top"] - 1 and g["stamp"]["bottom"] <= g["plate"]["bottom"] + 1, where
                    # the gray block: all four corners are the card's corner less the narrow shell's padding (--shell-card-inner-radius, 12 px).
                    # Under 860px the card's padding is that one (8 px), so the corners are concentric with the card's; from 860px the card has
                    # more room inside it (24 px over and under, 12 px at the sides, so that the parts do not stand against its
                    # edges), the block no longer stands at the card's corner, and its corners keep the same 12 px (not card minus the wide padding).
                    # (The one part that keeps its own four corners is the chart's plate (.tl-block, --radius-card, 18 px: test_chart_polish.py): where it is
                    # the first or the last part, its two corners are 18, not the card's less the padding; every other part's stay as written above.)
                    c = g["corners"]
                    own = {"tl": c["firstPlate"], "tr": c["firstPlate"], "bl": c["lastPlate"], "br": c["lastPlate"]}
                    want = {corner: 18 if own[corner] else 12 for corner in own}
                    if width < 860:
                        assert abs(c["card"] - c["pad"] - 12) <= 0.01, where
                        assert all(abs(c[corner] - (18 if own[corner] else c["card"] - c["pad"])) <= 0.01 for corner in ("tl", "tr", "bl", "br")), where
                    else:
                        assert abs(c["pad"] - 24) <= 0.01, where
                    assert all(abs(c[corner] - want[corner]) <= 0.01 for corner in ("tl", "tr", "bl", "br")), where
                assert errors == [], errors
                page.close()
            # under a finger a row is 44 px high, in the column and in the row of capsules alike
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            frame = _mount_layout_frame(page, views["reading_part"], 678)
            frame.evaluate(FINGER)
            for width in (360, 678, 859, 860, 1280):
                page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                frame.wait_for_function("(width) => innerWidth === width", arg=width)
                g = frame.evaluate(RAIL_GEOMETRY)
                assert g["tabs"] and all(abs(t["height"] - (74 if width >= 860 else 44)) <= 0.6 for t in g["tabs"]), (width, g)
                assert g["scroll"] <= g["width"] + 1, (width, g)
            page.close()
        finally:
            browser.close()


RAIL_EDGE = """() => {
  const nav = document.querySelector('.rail'); const rr = nav.getBoundingClientRect(); const out = [];
  const shown = (e) => { for (let n = e; n && n !== nav.parentElement; n = n.parentElement) { const c = getComputedStyle(n); if (c.display === 'none' || c.visibility === 'hidden' || parseFloat(c.opacity) < 0.02) return false; } return true; };
  const walk = document.createTreeWalker(nav, NodeFilter.SHOW_TEXT);
  for (let n; (n = walk.nextNode());) {
    if (!n.textContent.trim() || !n.parentElement || !shown(n.parentElement)) continue;
    const range = document.createRange(); range.selectNodeContents(n);
    for (const b of range.getClientRects()) if (b.width > 0 && (b.left < rr.left - 0.5 || b.right > rr.right + 0.5)) out.push([n.textContent.trim(), b.left - rr.left, rr.right - b.right]);
  }
  return {cut: out, left: rr.left, right: rr.right};
}"""

HEAD_COUNT = """() => [...document.querySelectorAll('.harness-seg [data-mark-key]')].map((t) => {
  const c = t.querySelector('.tab-mark-count'); const w = {};
  for (const text of ['1', '9', '10', '99']) { c.textContent = text; w[text] = t.getBoundingClientRect().width; }
  return {name: t.querySelector('.tab-mark-name').textContent, w};
})"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_rail_holds_the_widest_ring_the_ladder_and_its_longest_words_whole_and_the_count_of_accounts_moves_no_tab_up_to_99(fixture_file, engine):
    """kit: rail, kit: mark tab. The rail is as wide as the ring needs on the axis of the widest tab, so the grown ring of the picked row (and of a row under the pointer)
    keeps the air at the card; the ladder of resets, with the longest words it is asked to hold (29d 23h, +999.99, "more 99"), is never cut at the rail's edge, and its focus
    outline is whole; the number of a head tab holds two digits, so a tab is as wide with 1, 9, 10 and 99 accounts (the rail's width and the axis are read from the tabs)."""
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    seen_ladder = 0
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for key in ("reading_many", "codex_states", "claude"):
                for width in (860, 1000, 1280):
                    page = browser.new_page(viewport={"width": 1400, "height": 900})
                    frame = _mount_layout_frame(page, views[key], width)
                    where = (key, width)
                    rows = frame.locator(".rail .tab-mark-rail")
                    for i in range(rows.count()):
                        rows.nth(i).hover()
                        page.wait_for_timeout(400)
                        under = frame.evaluate("""() => { const nav = document.querySelector('.rail').getBoundingClientRect();
                            const r = [...document.querySelectorAll('.rail .tab-mark-rail')].filter((e) => e.matches(':hover'))[0].querySelector('.tab-mark-ring').getBoundingClientRect();
                            return {left: r.left - nav.left, air: nav.right - r.right}; }""")
                        assert under["left"] >= 0 and under["air"] >= 7.5, (where, i, under)
                    page.mouse.move(0, 0)
                    if frame.locator(".rail .rail-step").count():
                        seen_ladder += 1
                        frame.evaluate("""() => {
                            document.querySelectorAll('.rail .rail-step-time').forEach((e) => { e.textContent = '29d 23h'; });
                            document.querySelectorAll('.rail .rail-step-amount').forEach((e) => { e.textContent = '+999.99'; });
                            const more = document.querySelector('.rail .rail-more'); if (more) more.textContent = 'more 99'; }""")
                        edge = frame.evaluate(RAIL_EDGE)
                        assert edge["cut"] == [], (where, edge)
                        box = frame.evaluate("""() => { const n = document.querySelector('.rail').getBoundingClientRect();
                            return [...document.querySelectorAll('.rail .rail-ladder, .rail .rail-step, .rail .rail-more')].map((e) => { const r = e.getBoundingClientRect(); return r.width > 0 ? [r.left - n.left, n.right - r.right] : null; }).filter(Boolean); }""")
                        assert box and all(left >= -0.5 and gap >= 3.5 for left, gap in box), (where, box)
                    if key == "claude":
                        counts = frame.evaluate(HEAD_COUNT)
                        assert counts, where
                        for tab in counts:
                            assert max(tab["w"].values()) - min(tab["w"].values()) <= 0.5, (where, tab)
                    page.close()
        finally:
            browser.close()
    assert seen_ladder, "no fixture drew the ladder of resets: the test saw nothing"


def test_the_narrow_row_wraps_the_hidden_copy_takes_no_room_the_stamp_is_a_meta_line_with_a_disc_and_the_blocks_corners_are_concentric():
    """The four mends of the frame, by the stylesheet's own text: (1) the narrow rail's row wraps (gap 6 on both axes), a capsule is never wider
    than the row; no scroll, no mask, no --haze-w. (2) the hidden copy of the note is gone under 860px (display none), kept (hidden) above. (3) the
    stamp: 12 on 16, the meta ink, the regular weight; the number read the regular weight in the ink; the dot a --dot-size disc, 4px off the age,
    blinking only with .is-busy and still under reduced motion; the dot's words stay in it. (4) the gray block's four corners are the card's corner
    less the card's padding (--shell-card-inner-radius), the first part's top two and the last part's foot two."""
    source, css, capsule = _capsule()
    assert "--haze-w" not in source and "mask-image" not in "".join(r for r in _style_rules(css) if ".rail-scroll" not in r.partition("{")[0])
    assert '"@media(max-width:859.98px){.rail .tab-mark-row{flex-direction:row;flex-wrap:wrap;align-items:center;gap:var(--space-1h)}}"' in source
    assert ".tab-mark-rail{flex:0 0 auto;display:flex;width:auto;max-width:100%;min-height:var(--row-h);" in css
    # The note of the accounts not read stands in the plate now (under its number row), and the copy that only keeps its place keeps it on the narrow
    # shell as well (the plate does not jump as the window changes), so nothing hides it there. The stamp is in the plate's row of the figure at every width:
    # one line, and tight (under 640px) a line of its own under the figure, at the left, that may wrap.
    assert not re.search(r'"@media\(max-width:859\.98px\)\{\.wt-stamp', capsule), "the stamp has no narrow-shell rule of its own: it is one line at every width"
    tight = [r for r in re.findall(r'"(@media\(max-width:639\.98px\)\{\.wt-plate\.wt-plate[,{](?:[^"\\]|\\.)*)"', capsule)]
    # Under 640px the plate is a column in the order of its markup (the order of the keyboard too): the bars first, across, then the fact (its number
    # whole, across), the model list, the line of the stamp and the chart word, then the notes (the accounts at the limit, not read, the credits), the
    # legend and the reset, each across. No named rows (a row nobody fills would be the first free row of the legend and the reset, which the flow
    # places: they would climb over the bars) and no `order` (the bars stand first in the markup, so none has to be sent up to them).
    assert len(tight) == 1, tight
    block = tight[0]
    assert "grid-template-areas:none" in block and "grid-template-columns:minmax(0,1fr) auto" in block and "grid-template-areas:\\\"" not in block, block
    assert not re.search(r"(?<![\w-])order:", block + "".join(r for r in _style_rules(css) if ".wt-plate" in r.partition("{")[0])), "no `order`: the markup is the order"
    for sel in (".wt-fact", ".wt-scope"):
        assert re.search(r"\.wt-plate\.wt-plate>%s\{[^}]*grid-column:1/-1" % re.escape(sel), block), (sel, block)
    # (the other parts' classes - the chart word, the bars, the legend, the reset - are written with those parts: the bars span the row, the chart word joins
    # the stamp's row at the right; the notes and the two rows span the row by their own rules)
    rest = _section(css, "capsule: bars and legend")
    assert re.search(r"@media\(max-width:639\.98px\)\{[^\n]*\.wt-plate\.wt-plate>\.bars\{[^}]*grid-column:1/-1", rest)
    assert re.search(r"@media\(max-width:639\.98px\)\{[^\n]*\.wt-plate\.wt-plate>\.l-chart\{[^}]*grid-column:2;justify-self:end", rest)
    for rule in (".wt-limit{grid-column:1/-1;", ".wt-foot{grid-column:1/-1;", ".lrow>.family-credits{grid-column:1/-1;", ".lg,.nx{grid-column:1/-1;"):
        assert rule in css, rule
    assert re.search(r"\.wt-plate\.wt-plate>\.wt-stamp\{[^}]*justify-self:start;align-self:center;text-align:left;white-space:normal", block), block
    assert re.search(r"\.wt-plate\.wt-plate>\.wt-fact\{[^}]*justify-self:stretch", block), "the fact takes the width"
    assert ".wt-plate-cycle>.wt-stamp{grid-column:1/-1}" in block, "the budget has no chart word: its stamp has the line to itself"
    assert '".wt-foot-hold{visibility:hidden}"' in capsule, "the copy keeps the place, unseen"
    assert '".wt-foot{grid-column:1/-1;min-width:0;margin:0;letter-spacing:var(--track-meta)}"' in capsule, "the note takes the row under the number row, in the plate"
    # The stamp stands in the first line of the plate, at the right end of the model list's line (centred on it, as the chart word is); one
    # line, the dot a disc in the flow.
    # (the word and the count are spans in the flow of the one line: no rule gives them a display, the default is the inline they need)
    assert not re.search(r'\.wt-read\b', capsule), "the word is a part of the one line with no rule of its own"
    assert '".wt-age{overflow-wrap:anywhere}"' in capsule
    assert re.search(r'"\.wt-stamp\{grid-area:stamp;justify-self:end;align-self:center;position:relative;margin:0 0 var\(--space-2\);min-width:0;font-size:var\(--type-meta\);line-height:var\(--lh-ui\);'
                     r'font-weight:var\(--weight-regular\);color:var\(--text-meta\);text-align:right;white-space:nowrap\}"', capsule), "the stamp is 12 on 16, the meta ink, the regular weight, on the right of the first line"
    assert '".wt-n{color:var(--text-primary);font-weight:var(--weight-regular)}"' in capsule and "display:inline;color" not in capsule
    assert re.search(r'"\.wt-sep\{display:inline-block;width:var\(--sep-size\);height:var\(--sep-size\);margin:0 var\(--space-1\);'
                     r'border-radius:50%;background:currentColor;vertical-align:middle\}"', capsule), "the dot is a disc of the stamp's own ink, 2 px at rest"
    # ...and the one that blinks (a read is in the air) is the larger, 5 px: a size and nothing else (the blink itself is kit: motion's)
    assert '".wt-sep.is-busy{width:var(--dot-size);height:var(--dot-size)}"' in capsule
    assert "rail-bot" not in css, "the rail has no foot: the stamp is not in it"
    assert ".wt-sep{font-weight" not in capsule
    assert ".is-busy" not in capsule.replace(".wt-sep.is-busy{width:var(--dot-size);height:var(--dot-size)}", ""), "the blink is one rule of kit: motion (.is-busy and its pair), not the stamp's own"
    assert '".is-busy{animation:blink var(--dur-blink) var(--ease) infinite}"' in source and ".is-busy{animation:none;opacity:var(--blink-dim)}" in source, "the one rule and the pair of reduced motion"
    assert "var dot = el('span', refreshing ? 'wt-sep is-busy' : 'wt-sep');" in source and "dot.appendChild(el('span', 'sr-only', ' \u00b7 '));" in source
    assert "--dot-size:5px" in css and "--sep-size:2px" in css and "--shell-card-pad:var(--space-2)" in css
    assert "--shell-card-inner-radius:calc(var(--shell-card-radius) - var(--shell-card-pad))" in css
    assert ".shell-card{display:flex;flex-direction:column;gap:var(--space-4);min-width:0;padding:var(--shell-card-pad);" in css
    assert ".lrows>:first-child{border-top-left-radius:var(--shell-card-inner-radius);border-top-right-radius:var(--shell-card-inner-radius)}" in css
    assert (".lrows>:last-child{border-bottom-left-radius:var(--shell-card-inner-radius);border-bottom-right-radius:var(--shell-card-inner-radius);"
            "border-bottom-color:transparent}") in css


# ---------------------------------------------------------------------------
# The reader's pick of a family in a real browser (kit: rail, motion). One axis: the centre of the mark of the family picked (the first tab of
# the head) is the centre of the rings of its windows, and a thread of the rail runs down it from the mark to the first ring (from 860px). One
# movement for the pick alone: the tabs slide to their places (transform), the thread is drawn (scaleY from its top), the windows come down one
# after another and the foot of the rail comes last (opacity) - all within 850 ms, with no animation of a size, a place, a filter or a shadow.

FAMILY_KEYS = ("claude", "codex", "cursor_budget", "agy")
FAMILY_IDS = ("claude", "codex", "cursor", "agy")


def _family_views(views):
    """Every family answers for itself (the request names it: harness=<id>), and every answer carries the same four groups, so the head shows
    the four families whichever is picked."""
    groups = [views[key]["groups"][0] for key in FAMILY_KEYS]
    assert [g["harness_id"] for g in groups] == list(FAMILY_IDS), [g["harness_id"] for g in groups]
    # (the answer of a family lists that family first, as the card of a family is read from the first group of its answer)
    served = {group["harness_id"]: dict(views[key], groups=[group] + [g for g in groups if g is not group])
              for key, group in zip(FAMILY_KEYS, groups)}
    return served, FAMILY_IDS[0]


def _mount_family_frame(page, served, first, width, theme="light"):
    """The unmodified widget and its stylesheet in an iframe of the width given; only the data transport is stubbed."""
    import html as html_lib

    source = Path(__file__).with_name("widget.js").read_text()
    texts = json.loads(Path(__file__).with_name("strings_en.json").read_text(encoding="utf-8"))
    words = json.dumps({"language": "", "revision": 0, "plural_select": None, "strings": {}, "en": texts})
    stub = ("const SERVED = " + json.dumps(served) + ", FIRST = " + json.dumps(first) + ", WORDS = " + json.dumps(words) + ";"
            "window.fetch = async (u) => { const url = String(u), hit = /[?&]harness=([^&]*)/.exec(url);"
            " const body = url.indexOf('/i18n') >= 0 ? WORDS : JSON.stringify(SERVED[hit ? decodeURIComponent(hit[1]) : FIRST] || SERVED[FIRST]);"
            " return {ok: true, status: 200, text: async () => body}; };")
    doc = ('<!doctype html><html data-theme="' + theme + '"><head></head><body><div id="root"></div><script>' + stub + "</script><script>"
           + source.replace("</script", "<\\/script") + "</script></body></html>")
    page.set_content('<iframe sandbox="allow-scripts" style="border:0;width:' + str(width) + 'px;height:1400px" srcdoc="'
                     + html_lib.escape(doc, quote=True) + '"></iframe>')
    frame = page.frames[1]
    frame.wait_for_selector(".lrow")
    frame.wait_for_selector('[data-focus="refresh"]:not([disabled])')
    return frame


# Where the mark of the family picked (the first tab of the head), the thread and the first ring stand, as the browser lays them out.
FAMILY_AXIS = """() => {
  const q = (s, r = document) => r.querySelector(s);
  const tab = q('.harness-seg [data-mark-key]'), mark = q('.tab-mark-icon', tab);
  const ring = q('.rail .tab-mark-rail .ring'), lead = q('.rail-lead');
  const m = mark.getBoundingClientRect(), g = ring && ring.getBoundingClientRect(), l = lead && lead.getBoundingClientRect(), t = tab.getBoundingClientRect();
  return {width: innerWidth, scroll: document.documentElement.scrollWidth, first: tab.getAttribute('data-focus'), pressed: tab.getAttribute('aria-pressed'),
    tabCx: t.left + t.width / 2, tabLeft: t.left, tabTop: t.top, cardLeft: q('.shell-card').getBoundingClientRect().left,
    rail: !!q('.rail'), markCx: m.left + m.width / 2, markBottom: m.bottom,
    ringCx: g ? g.left + g.width / 2 : null, ringTop: g ? g.top : null,
    leadCx: l ? l.left + l.width / 2 : null, leadTop: l ? l.top : null, leadBottom: l ? l.bottom : null, leadShown: !!lead && getComputedStyle(lead).display !== 'none',
    rows: [...document.querySelectorAll('.rail .tab-mark-rail')].map((n) => q('.tab-mark-name', n).textContent),
    heads: [...document.querySelectorAll('.harness-seg [data-mark-key]')].map((n) => n.getAttribute('data-focus'))};
}"""

# A recorder of the frames: it is put in before a click and writes, frame after frame, where the tabs stand, how far the thread is drawn, how
# visible each row and the foot are, which animations run (those of the rail and the slide and dimming of a tab: all named rail-*, the others by name),
# where the middle of the mark of each tab and of the first ring stands, the sideways scroll, when a click came (clicks) and when the page was drawn
# again (swaps: the frames at which the first tab of the head is another node than a frame before).
FRAME_PROBE = """(ms) => {
  const rec = {frames: [], seen: {}, start: performance.now(), clicks: [], swaps: []};
  window.__probe = rec;
  document.addEventListener('click', () => rec.clicks.push(performance.now() - rec.start), true);
  let lastNode = null;
  const q = (s, r = document) => r.querySelector(s);
  const middle = (n) => { const b = n && n.getBoundingClientRect(); return b ? b.left + b.width / 2 : null; };
  const skip = ['offset', 'computedOffset', 'easing', 'composite'];
  const props = (a) => { try { return [...new Set(a.effect.getKeyframes().flatMap((k) => Object.keys(k)).filter((k) => !skip.includes(k)))].sort(); } catch (e) { return []; } };
  const kind = (a) => (typeof CSSAnimation !== 'undefined' && a instanceof CSSAnimation ? 'CSSAnimation'
    : typeof CSSTransition !== 'undefined' && a instanceof CSSTransition ? 'CSSTransition' : 'Animation');
  const tick = () => {
    const t = performance.now() - rec.start, tabs = [...document.querySelectorAll('.harness-seg [data-mark-key]')];
    const lead = q('.rail-lead'), rail = q('.rail'), bot = q('.rail-bot'), note = q('.wt-plate .wt-foot:not(.wt-foot-hold)');
    if (lastNode && tabs[0] && tabs[0] !== lastNode) rec.swaps.push(t);
    lastNode = tabs[0] || lastNode;
    let ours = 0;
    for (const a of document.getAnimations()) {
      const node = a.effect && a.effect.target, type = kind(a), name = a.animationName || a.transitionProperty || '';
      const own = type === 'CSSAnimation' && name.startsWith('rail-');
      if (own) ours += 1;
      const key = [type, name, node ? String(node.className.baseVal === undefined ? node.className : node.className.baseVal) : '', props(a).join('+')].join('|');
      rec.seen[key] = (rec.seen[key] || 0) + 1;
    }
    rec.frames.push({t, ours, heads: tabs.map((n) => n.getAttribute('data-focus')), lefts: tabs.map((n) => n.getBoundingClientRect().left),
      transforms: tabs.map((n) => getComputedStyle(n).transform), classes: tabs.map((n) => n.className), z: tabs.map((n) => getComputedStyle(n).zIndex),
      alpha: tabs.map((n) => +getComputedStyle(n).opacity), marks: tabs.map((n) => middle(n)), ring: middle(q('.rail .tab-mark-rail .tab-mark-ring')),
      lead: lead ? getComputedStyle(lead).transform : null, rows: [...document.querySelectorAll('.rail .tab-mark-rail')].map((n) => +getComputedStyle(n).opacity),
      foot: note ? +getComputedStyle(note).opacity : null,
      bot: bot ? +getComputedStyle(bot).opacity : null, moving: !!rail && rail.classList.contains('rail-moving'),
      // the ink of what the ladder draws, in the order it comes down: its head, the steps that stand, the word under them (the last row of the rail)
      ladder: [...document.querySelectorAll('.rail-ladder:not(.rail-ladder-out) :is(.rail-ladder-title, .rail-step, .rail-more)')]
        .filter((n) => n.getClientRects().length > 0).map((n) => +getComputedStyle(n).opacity),
      scroll: document.documentElement.scrollWidth, width: innerWidth, running: document.getAnimations().filter((a) => a.playState === 'running' || a.playState === 'pending').length});
    if (t < ms) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}"""
PROBE_DONE = "(ms) => !!window.__probe && performance.now() - window.__probe.start > ms + 60"
LEFT_OF = "(id) => document.querySelector('[data-focus=\"' + id + '\"]').getBoundingClientRect().left"
SCALE_Y = "(m) => { const p = String(m).match(/matrix\\(([^)]*)\\)/); return p ? parseFloat(p[1].split(',')[3]) : 1; }"
LAYOUT_PROPS = {"width", "height", "top", "left", "right", "bottom", "filter", "box-shadow", "margin", "padding", "min-width", "max-width"}


def _record(frame, action, ms=2000):
    """Start the recorder, do the action, wait for the recorder to end and hand back what it wrote."""
    frame.evaluate(FRAME_PROBE, ms)
    action()
    frame.wait_for_function(PROBE_DONE, arg=ms, timeout=ms + 4000)
    return frame.evaluate("window.__probe")


def _scale_y(matrix):
    if matrix in (None, "none"):
        return 1.0
    return float(matrix.split("(")[1].rstrip(")").split(",")[3])


def _first_frame(frames, test):
    return next((f for f in frames if test(f)), None)


def _settle_family(frame, family):
    """After a click on a family: the head puts it first, the data of the family has come, and the movement (within 850 ms of the pick's own
    drawing) has had the time to end."""
    frame.wait_for_function("(id) => { const t = document.querySelector('.harness-seg [data-mark-key]'); return t && t.getAttribute('data-focus') === id"
                            " && !!document.querySelector('[data-focus=\"refresh\"]:not([disabled])'); }", arg="harness:" + family)
    frame.wait_for_timeout(950)
    # (an animation that ended and keeps its last frame, like the rise of a figure, is still listed: only a running one counts.) The sweep of the rings
    # (kit: ring, motion) is the one that goes on after the 850 ms of the movement: the last ring of a family starts at 380 ms + 70 ms a row and draws for
    # 650 ms (1310 ms for the fifth row), so give it up to 700 ms more; an animation that never ends still fails here.
    running = "document.getAnimations().filter((a) => a.playState === 'running' || a.playState === 'pending').length"
    try:
        frame.wait_for_function("() => " + running + " === 0", timeout=700)
    except Exception:  # still running after the room given: the assertion below says so
        pass
    assert frame.evaluate(running) == 0


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_from_860px_the_mark_of_the_family_and_the_rings_of_its_windows_stand_on_one_axis_with_a_thread_between_them(fixture_file, engine):
    """kit: rail / capsule: head. From 860px, for each family picked (the card of each, opened on its own) and in both themes: the centre of the
    mark of the family picked (the first tab of the head) is the centre of the rings of the rail, 0.5px at most; the thread starts 2px (+-1)
    under the mark (picked, so grown by --mark-on) and ends 2px (+-1) over the box of the first ring; the rail holds the windows the family
    reports and no other (Claude has no 30-day window, so no 30-day row; Codex has its own); under 860px there is no thread, and at no width does
    the page scroll sideways."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, _ = _family_views(json.loads(Path(fixture_file).read_text()))
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            cards = {}
            for width in (360, 859, 860, 1020, 1280):
                for family in FAMILY_IDS:
                    page = browser.new_page(viewport={"width": 1400, "height": 900})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    frame = _mount_family_frame(page, served, family, width)
                    frame.wait_for_timeout(300)
                    for theme in ("light", "dark"):
                        frame.evaluate("(theme) => document.documentElement.setAttribute('data-theme', theme)", theme)
                        g = frame.evaluate(FAMILY_AXIS)
                        where = (engine, width, theme, family, g)
                        assert g["first"] == "harness:" + family and g["pressed"] == "true", where
                        assert g["scroll"] <= g["width"] + 1 and g["rail"] and g["rows"], where
                        if width >= 860:
                            assert g["leadShown"], where
                            # rule: the axis is the middle of the whole tab picked (it was the middle of its mark); the tab stands at the
                            # edge of the widget as far from it as from the top (8px); the rail is as wide for every family, so the card's edge stays where it was
                            assert abs(g["tabCx"] - g["ringCx"]) <= 1, ("the tab and the rings on one axis", where)
                            assert abs(g["tabLeft"] - 8) <= 1 and abs(g["tabTop"] - 8) <= 1, ("the tab stands 8px from the left and from the top", where)
                            cards.setdefault(width, set()).add(round(g["cardLeft"]))
                            assert abs(g["leadCx"] - g["ringCx"]) <= 0.5, ("the thread on the axis", where)
                            assert 1 <= g["leadTop"] - g["markBottom"] <= 3, ("the thread starts a hair under the mark", where)
                            assert 1 <= g["ringTop"] - g["leadBottom"] <= 3, ("and ends a hair over the first ring", where)
                        else:
                            assert not g["leadShown"], ("no thread on the narrow shell", where)
                    if family == "claude":
                        assert not any("30" in row for row in g["rows"]) and len(g["rows"]) == 2, ("Claude has no 30-day window", g["rows"])
                    if family == "codex":
                        assert "30D" in g["rows"], g["rows"]
                    if family == "cursor":
                        assert len(g["rows"]) == 1, g["rows"]   # Cursor: one row (30 days, $)
                    assert errors == [], errors
                    page.close()
            assert all(len(lefts) == 1 for w, lefts in cards.items() if w >= 860), ("the edge of the card does not move with the family picked", cards)
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_pick_of_a_family_slides_the_tabs_draws_the_thread_and_brings_the_windows_down_by_transform_and_opacity_alone(fixture_file, engine):
    """kit: rail, motion. The pick of a family (a click on its tab): the frame after it shows the tab picked at its old place (4px at most) under a
    transform that is not nil, then it slides to its place; the thread is drawn from the mark down (scaleY from nil), then the windows come down
    one after another (opacity from nil, the first before the second), the foot of the rail last; it all ends within 850 ms, leaves no animation
    and every row at opacity 1. Only the keyframes rail-draw/-drop/-fade and rail-slide/-yield (the slide of the tabs and the dimming of the tabs that
    give way) run for it, and they touch transform and opacity alone - no animation of any browser touches a size, a place, a filter or a shadow.
    The poll (Refresh) and the pick of a window in the family run none of it."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, first = _family_views(json.loads(Path(fixture_file).read_text()))
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_family_frame(page, served, first, 1280)
            # Codex first (its card has one window), then the pick that is recorded: Claude (two windows) goes first and Codex steps aside.
            frame.click('[data-focus="harness:codex"]')
            _settle_family(frame, "codex")
            heads = frame.evaluate(FAMILY_AXIS)["heads"]
            assert heads[:2] == ["harness:codex", "harness:claude"], heads
            old = {key: frame.evaluate(LEFT_OF, key) for key in heads}
            up, down = "harness:claude", "harness:codex"
            rec = _record(frame, lambda: frame.click('[data-focus="harness:claude"]'))
            frames = rec["frames"]
            k = _first_frame(frames, lambda f: f["heads"][0] == up)
            assert k is not None, "the head puts the family picked first"
            at = lambda f, key: f["lefts"][f["heads"].index(key)]
            new = {key: frame.evaluate(LEFT_OF, key) for key in heads}
            assert new[up] < old[up] - 20 and new[down] > old[down] + 20, (old, new)
            # the frame after the pick: both tabs that moved are drawn at their old places, under a transform that is not nil
            assert abs(at(k, up) - old[up]) <= 4 and abs(at(k, down) - old[down]) <= 4, (k, old)
            assert all(t not in ("none", "matrix(1, 0, 0, 1, 0, 0)") for t in k["transforms"][:2]), k["transforms"]
            # then they slide: some frame has the tab picked between its two places, and it ends on the new one
            assert any(new[up] + 3 < at(f, up) < old[up] - 3 for f in frames if f["t"] > k["t"]), "a slide"
            assert abs(at(frames[-1], up) - new[up]) <= 0.5 and abs(at(frames[-1], down) - new[down]) <= 0.5
            assert len(frames[-1]["rows"]) == 2, frames[-1]["rows"]   # Claude has two windows to bring down
            # the thread is nil, the windows and the foot are unseen in that frame; then the thread, the windows in their order, the foot
            assert _scale_y(k["lead"]) < 0.05 and all(o < 0.05 for o in k["rows"]) and (k["bot"] is None or k["bot"] < 0.05), k
            when = lambda test: (_first_frame([f for f in frames if f["t"] >= k["t"]], test) or {"t": None})["t"]
            draw_from, draw_to = when(lambda f: _scale_y(f["lead"]) > 0.05), when(lambda f: _scale_y(f["lead"]) > 0.99)
            row_in = [when(lambda f, i=i: len(f["rows"]) > i and f["rows"][i] > 0.5) for i in range(len(frames[-1]["rows"]))]
            foot_in = when(lambda f: f["bot"] is not None and f["bot"] > 0.5)
            assert None not in [draw_from, draw_to] + row_in, (draw_from, draw_to, row_in)
            # the thread starts when the tabs have gone about 70% of the way (~280 ms) and takes 200 ms; the rows start at ~380 ms, 70 ms apart
            assert 250 <= draw_from - k["t"] <= 420 and draw_from < row_in[0] and draw_to - k["t"] < 620, (k["t"], draw_from, draw_to, row_in)
            assert row_in == sorted(row_in) and len(set(row_in)) == len(row_in), ("one after another", row_in)
            assert row_in[0] - k["t"] >= 380 and (foot_in is None or foot_in > row_in[-1]), ("the foot after the last row", k["t"], row_in, foot_in)
            # nothing stands alone in an empty rail: when the foot first shows, every row has started
            shown = _first_frame([f for f in frames if f["t"] >= k["t"]], lambda f: f["bot"] is not None and f["bot"] > 0.05)
            assert shown is None or all(o > 0 for o in shown["rows"]), ("the foot is seen after the last row has started", shown)
            # the slide: a soft start (the first two frames go 15% of the way at most), one smooth run (no frame, by the clock, faster than twice
            # the mean speed), and the thread starts when the tabs have gone about 70% of the way
            reach = old[up] - new[up]
            seen = [(f["t"], (old[up] - at(f, up)) / reach) for f in frames if f["t"] >= k["t"]]
            assert seen[1][1] <= 0.15 and seen[0][1] <= 0.15, ("a soft start", seen[:3])
            speeds = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(seen, seen[1:]) if b[0] - a[0] >= 5 and a[1] < 0.995]
            assert max(speeds) <= 2.0 / 450, ("no leap", [round(v * 450, 2) for v in speeds])
            at_thread = min(seen, key=lambda pair: abs(pair[0] - draw_from))[1]
            ran = [f["t"] for f in frames if f["ours"] > 0]
            assert 0.6 <= at_thread <= 0.85, ("the thread starts when the tabs have gone about 70% of the way", at_thread)
            assert ran and max(ran) - k["t"] <= 850 + 60, ("within 850 ms", k["t"], max(ran))
            # over: no animation, every row and the foot seen, the thread whole
            last = frames[-1]
            assert last["running"] == 0 and all(o == 1 for o in last["rows"]) and last["bot"] in (None, 1), last
            assert last["lead"] in ("none", "matrix(1, 0, 0, 1, 0, 0)") and last["scroll"] <= last["width"] + 1, last
            # only transform and opacity: the keyframes of the rail, the transition of the tab's transform; nothing of any browser touches a layout property
            ours = {key: n for key, n in rec["seen"].items() if key.startswith("CSSAnimation|rail-")}
            assert ours, rec["seen"]
            for key in ours:
                kind, name, target, props = key.split("|")
                assert name in ("rail-draw", "rail-drop", "rail-fade", "rail-slide", "rail-yield") and set(props.split("+")) <= {"transform", "opacity"}, key
            # the slide of the tabs is the kit's keyframes (no transition of the tab's transform), and the tab that gives way dims beside it
            assert any(key.split("|")[1] == "rail-slide" for key in ours) and any(key.split("|")[1] == "rail-yield" for key in ours), ours
            assert not any(key.startswith("CSSTransition") and "tab-mark-slide" in key for key in rec["seen"]), rec["seen"]
            assert not {key.split("|")[1] for key in rec["seen"] if key.startswith("CSSTransition")} & LAYOUT_PROPS, rec["seen"]
            assert not any(set(key.split("|")[3].split("+")) & LAYOUT_PROPS for key in rec["seen"]), rec["seen"]
            # 2. The poll (Refresh) draws none of it.
            rec = _record(frame, lambda: frame.click('[data-focus="refresh"]'), 700)
            # (the rail of the pick keeps its class until the next drawing, so the poll's own is read at the last frame)
            assert all(f["ours"] == 0 for f in rec["frames"]) and not rec["frames"][-1]["moving"], "the poll moves nothing"
            assert not any(key.startswith("CSSAnimation|rail-") for key in rec["seen"]), rec["seen"]
            assert all("tab-mark-flip" not in c and "tab-mark-slide" not in c for c in rec["frames"][-1]["classes"]), rec["frames"][-1]["classes"]
            # 3. The pick of a window in the family draws none of it either.
            frame.wait_for_selector('[data-focus="refresh"]:not([disabled])')
            rec = _record(frame, lambda: frame.click('.rail .tab-mark-rail[aria-selected="false"]'), 700)
            assert all(f["ours"] == 0 and not f["moving"] for f in rec["frames"]), "the pick of a window moves nothing of the rail"
            assert not any(key.startswith("CSSAnimation|rail-") for key in rec["seen"]), rec["seen"]
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [360, 859])
def test_real_browser_on_the_narrow_shell_the_pick_of_a_family_moves_and_never_scrolls_the_page_sideways(fixture_file, engine, width):
    """Under 860px the thread is not drawn, but the tabs slide and the windows come down all the same; at no frame of it does the page scroll
    sideways, and when it is over nothing runs and every row is seen. (The narrow shell is not worked to the end - the debt of part 6 - this holds
    it from breaking.)"""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, first = _family_views(json.loads(Path(fixture_file).read_text()))
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            frame = _mount_family_frame(page, served, first, width)
            rec = _record(frame, lambda: frame.click('[data-focus="harness:codex"]'))
            frames = rec["frames"]
            assert all(f["scroll"] <= f["width"] + 1 for f in frames), max(f["scroll"] - f["width"] for f in frames)
            assert _first_frame(frames, lambda f: f["heads"][0] == "harness:codex") is not None
            last = frames[-1]
            assert last["running"] == 0 and all(o == 1 for o in last["rows"]) and last["bot"] in (None, 1), last
            assert not frame.evaluate(FAMILY_AXIS)["leadShown"]
            page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_under_reduced_motion_the_pick_of_a_family_runs_no_animation_and_the_tabs_do_not_slide(fixture_file, engine):
    """prefers-reduced-motion: the family picked stands first at once, no tab is drawn at its old place, no keyframe of the rail starts, and at no
    frame does any animation run."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, first = _family_views(json.loads(Path(fixture_file).read_text()))
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            context = browser.new_context(viewport={"width": 1400, "height": 900}, reduced_motion="reduce")
            page = context.new_page()
            frame = _mount_family_frame(page, served, first, 1280)
            assert frame.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches") is True
            old = frame.evaluate(LEFT_OF, "harness:codex")
            rec = _record(frame, lambda: frame.click('[data-focus="harness:codex"]'))
            frames = rec["frames"]
            k = _first_frame(frames, lambda f: f["heads"][0] == "harness:codex")
            assert k is not None
            assert abs(k["lefts"][0] - frame.evaluate(LEFT_OF, "harness:codex")) <= 0.5 and k["lefts"][0] < old - 20, "it stands at its place at once"
            assert all(f["ours"] == 0 and f["running"] == 0 and not f["moving"] for f in frames), [f for f in frames if f["running"]][:2]
            assert all("tab-mark-slide" not in c and "tab-mark-yield" not in c and "tab-mark-ride" not in c for f in frames for c in f["classes"]), "no tab slides, dims or rides"
            assert not any(key.startswith("CSSAnimation|rail-") for key in rec["seen"]), rec["seen"]
            assert all(o == 1 for o in frames[-1]["rows"]) and frames[-1]["bot"] in (None, 1), frames[-1]
            context.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_from_860px_the_axis_holds_for_each_family_picked_by_a_click_one_after_another(fixture_file, engine):
    """kit: rail / capsule: head. The pick by a click (not only the card opened first): the answer in hand carries the windows of every family,
    as the card of a family picked is drawn from it, and a click on each family in turn - Codex, Cursor, Antigravity, then Claude again - puts
    that family first with its own rows, its mark on the axis of its rings (0.5px at most) and its thread from the mark to the first ring (2px
    +-1 at both ends); when the movement is over nothing runs."""
    import copy

    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, first = _family_views(json.loads(Path(fixture_file).read_text()))
    every = [g for family in FAMILY_IDS for g in (((served[family].get("reserve") or {}).get("summary") or {}).get("groups") or [])]
    served = {family: copy.deepcopy(view) for family, view in served.items()}
    for view in served.values():
        view["reserve"]["summary"]["groups"] = copy.deepcopy(every)
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_family_frame(page, served, first, 1280)
            frame.wait_for_timeout(300)
            for family in ("codex", "cursor", "agy", "claude"):
                frame.click('[data-focus="harness:' + family + '"]')
                _settle_family(frame, family)
                g = frame.evaluate(FAMILY_AXIS)
                where = (engine, family, g)
                assert g["first"] == "harness:" + family and g["pressed"] == "true", where
                assert g["rail"] and g["rows"] and g["leadShown"] and g["scroll"] <= g["width"] + 1, where
                # rule: the axis is the middle of the whole tab picked (it was the middle of its mark): the tab and the rings
                # stand on one axis after each click, 1px at most; the thread on the rings' axis, 0.5px
                assert abs(g["tabCx"] - g["ringCx"]) <= 1 and abs(g["leadCx"] - g["ringCx"]) <= 0.5, ("on one axis", where)
                assert 1 <= g["leadTop"] - g["markBottom"] <= 3 and 1 <= g["ringTop"] - g["leadBottom"] <= 3, ("the thread's two ends", where)
                if family == "claude":
                    assert len(g["rows"]) == 2 and not any("30" in row for row in g["rows"]), ("Claude has no 30-day window", g["rows"])
                if family == "codex":
                    assert "30D" in g["rows"], g["rows"]
                if family == "cursor":
                    assert len(g["rows"]) == 1, g["rows"]
                # The thread ends 2px (+-1) over the first ring whether that ring is the one picked (grown to --mark-on, so its box is higher by
                # 3px: the thread ends higher too) or not: each window of the family is picked in turn, the first one last, and the end is measured
                # on the box as it is drawn once the ring's growth (500 ms) has settled.
                if len(g["rows"]) > 1:
                    for at in (1, 0):
                        frame.locator(".rail .tab-mark-rail").nth(at).click()
                        frame.wait_for_timeout(700)
                        h = frame.evaluate(FAMILY_AXIS)
                        assert frame.evaluate("(at) => document.querySelectorAll('.rail .tab-mark-rail')[at].getAttribute('aria-selected')", at) == "true", (family, at)
                        assert 1 <= h["ringTop"] - h["leadBottom"] <= 3, ("the thread's end over the first ring, picked: %s" % (at == 0), engine, family, h)
                        assert abs(h["leadCx"] - h["ringCx"]) <= 0.5 and h["scroll"] <= h["width"] + 1, (family, at, h)
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


# Where the foot of the rail (the stamp) stands, frame after frame, while the family picked is being read (the answer is held back).
FOOT_PROBE = """(ms) => {
  const rec = {frames: [], start: performance.now()};
  window.__foot = rec;
  const q = (s) => document.querySelector(s);
  const tick = () => {
    const t = performance.now() - rec.start, bot = q('.wt-stamp'), wait = q('.chart-wait'), plot = q('.chart-plot');   // (the stamp: the foot of the rail is gone, the stamp stands in the plate now)
    // (the layout place of the stamp, whose rise with the plate is a transform: offsets, not the rect)
    const at = (e) => { let y = 0; for (; e; e = e.offsetParent) y += e.offsetTop; return y; };
    rec.frames.push({t, bot: bot ? at(bot) : null, doc: document.documentElement.scrollHeight,
      ctl: (q('.tl-controls') || {offsetHeight: 0}).offsetHeight,
      wait: !!wait, plot: !!plot, head: (q('.harness-seg [data-mark-key]') || {getAttribute: () => null}).getAttribute('data-focus')});
    if (t < ms) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}"""
HOLD_BACK = """(ms) => { const f = window.fetch; window.fetch = (u, o) => new Promise((r) => setTimeout(r, ms)).then(() => f(u, o)); }"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_foot_of_the_rail_stands_still_while_the_chart_of_the_family_picked_is_read(fixture_file, engine):
    """kit: rail / capsule: chart. The pick of a family whose chart is not at hand yet (the answer is held back 700 ms): the place of the chart
    is as high as the chart will be, so from the first frame after the pick to the end of the reading the top of the foot of the rail moves
    2px at most - for every family, at 860, 1020 and 1280px."""
    import copy

    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, first = _family_views(json.loads(Path(fixture_file).read_text()))
    # (the answer in hand carries the windows of every family, so the pick draws the card at once, its chart still to come)
    every = [g for family in FAMILY_IDS for g in (((served[family].get("reserve") or {}).get("summary") or {}).get("groups") or [])]
    served = {family: copy.deepcopy(view) for family, view in served.items()}
    charted = {}
    for family, view in served.items():
        view["reserve"]["summary"]["groups"] = copy.deepcopy(every)
        chart = view["reserve"].get("chart")
        charted[family] = bool(chart)
        if chart:   # (the horizon the widget asks for a limit longer than a day: the answer held back is the one it waits for)
            chart["horizon"] = "7d" if int(str(chart["group_key"]).split("|")[2]) > 86400 else "24h"
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for width in (860, 1020, 1280):
                for family in ("codex", "agy", "cursor", "claude"):
                    page = browser.new_page(viewport={"width": 1400, "height": 900})
                    frame = _mount_family_frame(page, served, "claude" if family != "claude" else "codex", width)
                    frame.wait_for_timeout(250)
                    frame.evaluate(HOLD_BACK, 700)
                    frame.evaluate(FOOT_PROBE, 1500)
                    frame.click('[data-focus="harness:%s"]' % family)
                    frame.wait_for_function(PROBE_FOOT_DONE, arg=1500, timeout=6000)
                    frames = frame.evaluate("window.__foot")["frames"]
                    k = next(i for i, f in enumerate(frames) if f["head"] == "harness:" + family)
                    after = frames[k:]
                    waits = [f for f in after if f["wait"]]
                    loaded = [f for f in after if f["plot"]]
                    tops = [f["bot"] for f in after if f["bot"] is not None]
                    # (the row of controls above the chart is the family's own: the Combined switch of Claude wraps it at the narrowest wide card when
                    # it arrives with the chart, so the chart's place is measured from the foot less that row; the foot itself stands wherever the row
                    # stays one line - from 1020px, and for the families that have no such switch)
                    below = [f["bot"] - f["ctl"] for f in after if f["bot"] is not None]
                    assert (waits and loaded) or not charted[family], (engine, width, family, len(waits), len(loaded), charted)
                    if not tops:   # (a family with no timeline has no chart to wait for)
                        continue
                    assert max(below) - min(below) <= 2, ("the chart's place under the controls", engine, width, family, waits[0], loaded[-1])
                    if width >= 1020 or family != "claude":
                        assert max(tops) - min(tops) <= 2, (engine, width, family, round(max(tops) - min(tops), 1), waits[0], loaded[-1])
                    page.close()
        finally:
            browser.close()


PROBE_FOOT_DONE = "(ms) => !!window.__foot && performance.now() - window.__foot.start > ms + 60"


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_family_picked_last_goes_first_the_others_keep_their_order_and_a_quick_second_pick_starts_where_the_tabs_are(fixture_file, engine):
    """kit: rail. The order of the families is the last picked first, the others as they stood (not back to the first order): from Claude, Cursor,
    Codex, Antigravity: Codex -> [Codex, Claude, Cursor, Antigravity]; then Antigravity -> [Antigravity, Codex, Claude, Cursor]; the order is only
    in the document (a poll does not put it back, nothing is stored). One tab goes at a pick - the one that goes first - and the tab picked rides
    over those it passes (the layer token); a second pick 120 ms into the first starts from where the tabs are drawn then: no tab leaps from
    one frame to the next."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, first = _family_views(json.loads(Path(fixture_file).read_text()))
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            frame = _mount_family_frame(page, served, first, 1280)
            start = frame.evaluate(FAMILY_AXIS)["heads"]
            frame.click('[data-focus="harness:codex"]')
            _settle_family(frame, "codex")
            one = frame.evaluate(FAMILY_AXIS)["heads"]
            assert one == ["harness:codex"] + [h for h in start if h != "harness:codex"], (start, one)
            frame.click('[data-focus="harness:agy"]')
            _settle_family(frame, "agy")
            two = frame.evaluate(FAMILY_AXIS)["heads"]
            assert two == ["harness:agy"] + [h for h in one if h != "harness:agy"], (one, two)
            frame.click('[data-focus="refresh"]')
            frame.wait_for_selector('[data-focus="refresh"]:not([disabled])')
            frame.wait_for_timeout(300)
            assert frame.evaluate(FAMILY_AXIS)["heads"] == two, "a poll does not put the first order back"
            # The slide of a pick that passes three tabs: Cursor, last of [Antigravity, Codex, Claude, Cursor], goes first. The tab picked goes
            # left, over the three it passes; the three each step one place right, all the same way - no two of the others ever go towards each
            # other, never cross, and the one that rides is over them for as long as it slides.
            picked = "harness:cursor"
            others = [h for h in two if h != picked]
            old = {key: frame.evaluate(LEFT_OF, key) for key in two}
            rec = _record(frame, lambda: frame.click('[data-focus="harness:cursor"]'), 1100)
            frames = rec["frames"]
            new = {key: frame.evaluate(LEFT_OF, key) for key in two}
            assert frame.evaluate(FAMILY_AXIS)["heads"] == [picked] + others, (picked, others)
            dx = {key: new[key] - old[key] for key in two}
            assert dx[picked] < -60 and all(dx[key] > 20 for key in others), ("one goes left, the three it passes step right", dx)
            assert len({round(dx[key]) for key in others}) <= 2, ("each of the three steps one place (the picked tab's own width)", dx)
            k = _first_frame(frames, lambda f: f["heads"][0] == picked)
            assert k is not None
            at = lambda f, key: f["lefts"][f["heads"].index(key)]
            run = [f for f in frames if f["t"] >= k["t"]]
            for a, b in zip(run, run[1:]):
                assert at(b, picked) - at(a, picked) <= 0.5, ("the tab picked only goes left", a["t"], b["t"])
                for key in others:
                    assert at(b, key) - at(a, key) >= -0.5, ("none of the others goes the other way", key, a["t"], b["t"])
            for f in run:
                xs = [at(f, key) for key in others]
                assert xs == sorted(xs), ("the others never cross one another, they keep their order", f["t"], xs)
            layer = lambda f, key: 0 if f["z"][f["heads"].index(key)] == "auto" else int(f["z"][f["heads"].index(key)])
            sliding = [f for f in run if abs(at(f, picked) - new[picked]) > 1]
            assert sliding and all(layer(f, picked) > max(layer(f, key) for key in others) for f in sliding), (
                "the tab picked is over the others for as long as it slides", [(round(f["t"]), f["z"]) for f in sliding[:3]])
            assert all(max(layer(f, key) for key in others) == 0 for f in run), "and only that tab rides"
            settled = frames[-1]
            assert all(layer(settled, key) == 0 for key in two), ("the layer is given back", settled["z"])
            # a quick second pick: Codex, and 120 ms into its slide Claude
            rec = _record(frame, lambda: (frame.click('[data-focus="harness:codex"]'), frame.wait_for_timeout(120),
                                          frame.click('[data-focus="harness:claude"]')), 1600)
            frames = rec["frames"]
            by_id = lambda f, key: f["lefts"][f["heads"].index(key)] if key in f["heads"] else None
            for key in two:
                run = [by_id(f, key) for f in frames if by_id(f, key) is not None]
                steps = [abs(b - a) for a, b in zip(run, run[1:])]
                assert max(steps) <= 60, ("no tab leaps from one frame to the next", key, [round(v, 1) for v in steps if v > 30])
            last = frames[-1]
            assert last["heads"][:2] == ["harness:claude", "harness:codex"] and last["running"] == 0, last["heads"]
            ride = [c for f in frames for c in f["classes"] if "tab-mark-ride" in c]
            assert ride, "the tab picked rides over the others while it slides"
            assert all("tab-mark-ride" not in c for c in last["classes"]), "and the layer is given back when it has slid"
            page.close()
        finally:
            browser.close()



# Every start of an animation in the page, with how far on it began (elapsedTime of animationstart: nil for a start, the time already gone for an
# animation that goes on from where it was, by a negative delay).
STARTS = """() => { window.__starts = []; document.addEventListener('animationstart', (e) => {
  // elapsed is the delay's (a negative delay: where the animation was put); progress is where the animation stands on its own clock when it starts
  // (startTime: where the page's clock put it), which is how a drawing inside the movement goes on (clockSet).
  const own = e.target.getAnimations ? e.target.getAnimations().find((a) => a.animationName === e.animationName) : null;
  window.__starts.push({name: e.animationName, key: (e.target.getAttribute && e.target.getAttribute('data-focus')) || '', at: performance.now(),
    elapsed: e.elapsedTime, progress: own && typeof own.currentTime === 'number' ? own.currentTime / 1000 : null});
}, true); }"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_a_drawing_inside_the_movement_goes_on_with_the_slide_of_the_tabs_and_does_not_start_it_again(fixture_file, engine):
    """kit: rail, motion. The shifts of the tabs are worked out once, at the pick; a drawing inside the movement (Refresh, 300 ms after the pick,
    stands for the chart's read) puts the same shifts on its new tabs and the slide goes on from where it was (the animation of the new tab
    begins 0.3 s in: elapsedTime of animationstart), so the path of the tab picked is one run - it never goes back, no two steps in a row stand
    still before it has gone 95% of the way - and it rests within 500 ms of its first frame. The tabs that give way dim to the token and back
    in the same time (opacity alone), and the tab that rides is never dimmed."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, first = _family_views(json.loads(Path(fixture_file).read_text()))
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_family_frame(page, served, first, 1280)
            frame.click('[data-focus="harness:codex"]')
            _settle_family(frame, "codex")
            heads = frame.evaluate(FAMILY_AXIS)["heads"]
            up, down = "harness:claude", "harness:codex"
            assert heads[:2] == [down, up], heads
            old = {key: frame.evaluate(LEFT_OF, key) for key in heads}
            frame.evaluate(STARTS)
            rec = _record(frame, lambda: (frame.click('[data-focus="harness:claude"]'), frame.wait_for_timeout(300),
                                          frame.evaluate("document.querySelector('[data-focus=\"refresh\"]').click()")), 1400)
            frames, starts = rec["frames"], frame.evaluate("window.__starts")
            new = {key: frame.evaluate(LEFT_OF, key) for key in heads}
            k = _first_frame(frames, lambda f: f["heads"][0] == up)
            assert k is not None
            at = lambda f, key: f["lefts"][f["heads"].index(key)]
            dim = lambda f, key: f["alpha"][f["heads"].index(key)]
            # the redraw drew the tab again, and its slide began where the first had got to - it was not started again
            slides = [st for st in starts if st["name"] == "rail-slide" and st["key"] == up]
            assert len(slides) >= 2, ("the drawing inside the movement drew the tab again", starts)
            # (where it stands on its own clock when its animationstart fires: the page's clock puts the new animation where the old one was)
            seen = lambda st: max(st["elapsed"], st["progress"] or 0)
            assert seen(slides[0]) < 0.05 and all(seen(st) > 0.15 for st in slides[1:]), ("it goes on, it does not begin again", slides)
            run = [f for f in frames if f["t"] >= k["t"]]
            reach = old[up] - new[up]
            assert reach > 20, (old, new)
            way = [(f["t"], (old[up] - at(f, up)) / reach) for f in run]
            assert all(b[1] >= a[1] - 0.01 for a, b in zip(way, way[1:])), "the tab picked never goes back"
            steps = [abs(b[1] - a[1]) * reach < 0.5 for a, b in zip(way, way[1:]) if 0.03 <= a[1] < 0.95 and b[0] - a[0] >= 5]
            assert steps and not any(a and b for a, b in zip(steps, steps[1:])), ("no two steps in a row stand still before 95% of the way", steps)
            rest = next(f for f in run if all(abs(at(g, up) - new[up]) <= 0.5 for g in run if g["t"] >= f["t"]))
            assert rest["t"] - k["t"] <= 500, ("the tab picked rests within 500 ms of its first frame", rest["t"] - k["t"])
            # the tab that gives way dims (to the token, 0.3, and back) and the one that rides never does; at the end all are whole
            assert min(dim(f, down) for f in run) <= 0.45 and all(dim(f, up) == 1 for f in run), ([round(dim(f, down), 2) for f in run][:40])
            assert all(a == 1 for a in frames[-1]["alpha"]) and frames[-1]["running"] == 0, frames[-1]
            yields = [key for key in rec["seen"] if key.split("|")[1] == "rail-yield"]
            assert yields and all(key.split("|")[3] == "opacity" and "tab-mark-ride" not in key.split("|")[2] for key in yields), yields
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


def _every_window_in_every_answer(served):
    """The answer of each family carries the windows of every family (the card of a pick is drawn at once from the answer in hand) and a chart
    whose horizon is the one the widget asks for, so the answer held back is the one the chart waits for."""
    import copy

    every = [g for family in FAMILY_IDS for g in (((served[family].get("reserve") or {}).get("summary") or {}).get("groups") or [])]
    out = {family: copy.deepcopy(view) for family, view in served.items()}
    for view in out.values():
        view["reserve"]["summary"]["groups"] = copy.deepcopy(every)
        chart = view["reserve"].get("chart")
        if chart:
            chart["horizon"] = "7d" if int(str(chart["group_key"]).split("|")[2]) > 86400 else "24h"
    return out


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("trigger", ["answer", "poll"])
def test_real_browser_a_drawing_in_the_middle_of_the_slide_neither_stops_nor_restarts_it_and_the_mark_stands_on_the_axis_when_the_thread_is_drawn(fixture_file, engine, trigger):
    """kit: rail, motion. The pick of the last family of the head (the tab picked goes left over the three it passes, the three step one place
    right) and, in the middle of the slide, the page is drawn again as the chart's arrival draws it - trigger "answer": the answer of the family is
    held back 300 ms, and it brings the chart in; trigger "poll": the interval's read 250 ms after the click (the visibility event the widget
    reads on). Frame after frame (rAF), from the click: the page was really drawn again inside the slide; the path of the tab picked is one run,
    it never goes back and no two frames in a row show a step under 0.5px before it has gone 95% of the way; it rests within 470 ms of the click (by the clock, a frame's reading
    more allowed) and stays; the middle of its mark is on the axis of the rings (2px at most) from the frame the thread is drawn to the end; in the middle of
    the slide (35% to 65% of the way) each tab that gives way is at opacity 0.5 at most, without turning back on the way down and up, and at the
    end at 1; the tab that rides is at 1 the whole time. In both engines."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, first = _family_views(json.loads(Path(fixture_file).read_text()))
    served = _every_window_in_every_answer(served)
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_family_frame(page, served, first, 1280)
            frame.wait_for_timeout(300)
            heads = frame.evaluate(FAMILY_AXIS)["heads"]
            up = heads[-1]
            others = [h for h in heads if h != up]
            assert len(others) == 3, heads
            old = {key: frame.evaluate(LEFT_OF, key) for key in heads}
            if trigger == "answer":
                frame.evaluate(HOLD_BACK, 300)

            def act():
                frame.click('[data-focus="%s"]' % up)
                if trigger == "poll":
                    frame.wait_for_timeout(250)
                    frame.evaluate("document.dispatchEvent(new Event('visibilitychange'))")

            rec = _record(frame, act, 2000)
            frames, click = rec["frames"], rec["clicks"][0]
            new = {key: frame.evaluate(LEFT_OF, key) for key in heads}
            assert frame.evaluate(FAMILY_AXIS)["heads"] == [up] + others, (up, others)
            k = _first_frame(frames, lambda f: f["heads"][0] == up)
            assert k is not None
            idx = lambda f, key: f["heads"].index(key)
            at = lambda f, key: f["lefts"][idx(f, key)]
            alpha = lambda f, key: f["alpha"][idx(f, key)]
            run = [f for f in frames if f["t"] >= k["t"]]
            reach = old[up] - new[up]
            assert reach > 150 and all(new[key] - old[key] > 20 for key in others), ("one goes left, the three it passes step right", old, new)
            # 1. the page was really drawn again inside the slide (the pick's own drawing is the frame the head changes in)
            drawn_again = [round(t - click) for t in rec["swaps"] if t > k["t"] + 40]
            assert drawn_again and 150 <= drawn_again[0] <= 450, ("the page is drawn again in the middle of the slide", trigger, drawn_again)
            # 2. one run: the path of the tab picked never goes back; no two frames in a row with a step under 0.5px before 95% of the way
            way = [(f["t"], (old[up] - at(f, up)) / reach) for f in run]
            assert all((b[1] - a[1]) * reach >= -0.5 for a, b in zip(way, way[1:])), ("it never goes back", [round(v[1], 3) for v in way])
            steps = [abs(b[1] - a[1]) * reach < 0.5 for a, b in zip(way, way[1:]) if a[1] < 0.95 and b[0] - a[0] >= 5]
            assert steps and not any(a and b for a, b in zip(steps, steps[1:])), ("no two steps in a row stand still before 95% of the way", steps)
            # (the sine's own top speed is ~1.57 times the mean, and until the page is drawn again no frame goes over 2 times the mean. The drawing in
            # the middle carries the slide on by the clock of the script (a negative delay), and the first slide began a frame or two after that clock
            # began: in WebKit the step into the drawn-again frame is the natural one, in Chromium it can come to 3.5 times the mean - a skip forward of
            # up to ~1.5 frames, written in the print line, not held to a number here. What is held: it is a step and not a restart or a leap to the end.)
            swap_at = next(t for t in rec["swaps"] if t > k["t"] + 40)
            leaps = [(b[0], (b[1] - a[1]) / (b[0] - a[0]) * 450) for a, b in zip(way, way[1:]) if b[0] - a[0] >= 5 and a[1] < 0.995]
            early = [v for t, v in leaps if t < swap_at]   # (the step into the frame that shows the page drawn again is the first of the drawing's own)
            assert early and max(early) <= 2.0, ("no leap until the page is drawn again", round(swap_at - click), [(round(t - click), round(v, 2)) for t, v in leaps if t < swap_at and v > 1.7])
            assert max(v for t, v in leaps) <= 8.0, ("and none is a restart or a leap to the end (a third of the way in a frame) after it", [round(v, 2) for t, v in leaps])
            # 3. it rests within 470 ms of the click, and stays (the moment it comes within 0.5px, as the clock has it between the last frame it was
            # farther off and the first it stays near in)
            near = lambda f: abs(at(f, up) - new[up])
            rest = next(f for f in run if all(near(g) <= 0.5 for g in run if g["t"] >= f["t"]))
            before = [f for f in run if f["t"] < rest["t"]][-1:]
            rest_at = rest["t"] if not before else before[0]["t"] + (rest["t"] - before[0]["t"]) * (near(before[0]) - 0.5) / max(near(before[0]) - near(rest), 0.001)
            # (a frame is 16.7 ms and a transform animation starts a frame or two after the click, so the clock's reading of the rest stands within a frame
            # of the 470 ms; a slide started again by the drawing in the middle would rest at ~760 ms)
            assert rest_at - click <= 470 + 17, ("it rests within 470 ms of the click", round(rest_at - click), "first frame at rest", round(rest["t"] - click))
            assert rest["t"] - k["t"] <= 470, ("and within 470 ms of the first frame of the pick", round(rest["t"] - k["t"]))
            # 4. the middle of the tab picked (it was the middle of its mark) is on the axis of the rings (2px at most) from the frame the thread is whole
            whole = _first_frame(run, lambda f: f["lead"] is not None and _scale_y(f["lead"]) > 0.995)
            assert whole is not None, "the thread is drawn whole"
            tail = [f for f in run if f["t"] >= whole["t"]]
            gap = max(abs(f["marks"][idx(f, up)] - f["ring"]) for f in tail if f["ring"] is not None)
            assert all(f["ring"] is not None for f in tail) and gap <= 2, ("the tab is on the axis when the thread is drawn", round(gap, 2), round(whole["t"] - click))
            # 5. in the middle of the slide each tab that gives way is dim, with no flash; at the end it is whole; the tab that rides is whole all the time
            midway = [f for f in run if 0.35 <= (old[up] - at(f, up)) / reach <= 0.65]
            assert midway, "there are frames in the middle of the slide"
            for key in others:
                series = [alpha(f, key) for f in run]
                assert all(alpha(f, key) <= 0.5 for f in midway), ("a tab that gives way is dim in the middle of the slide", key, [round(alpha(f, key), 2) for f in midway])
                # (no flash: it goes down to its lowest and comes up from it, and never turns back on the way - a restart would go up to 1 and down again)
                low = series.index(min(series))
                assert all(b <= a + 0.02 for a, b in zip(series[:low + 1], series[1:low + 1])) and all(b >= a - 0.02 for a, b in zip(series[low:], series[low + 1:])), (
                    "and does not flash", key, [round(v, 2) for v in series])
                assert alpha(frames[-1], key) == 1, key
            assert all(alpha(f, up) == 1 for f in frames), ("the tab that rides is never dimmed", [round(alpha(f, up), 2) for f in frames if alpha(f, up) != 1])
            last = frames[-1]
            assert last["running"] == 0 and all(a == 1 for a in last["alpha"]) and all(o == 1 for o in last["rows"]), last
            assert all("tab-mark-ride" not in c for c in last["classes"]) and all(z in ("auto", "0") for z in last["z"]), ("the layer is given back", last["z"])
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_note_of_the_accounts_not_read_is_unseen_until_the_last_window_has_started_down_and_stands_at_full_ink_at_the_end(fixture_file, engine):
    """kit: rail / capsule: window tabs. The pick of a family whose windows have accounts not read: the note ("Not read: ...") is drawn with the
    rail, at opacity nil, and shows only after the last window has started to come down and after the whole ladder of resets under the rings has
    (its head, its steps and the word under them: --rail-rows counts the rings and the ladder's rows) - never alone in an empty rail, never before
    the first row - and together with the word under the steps, the last row of the rail (the stamp is no part of the rail: it stands in the
    plate); when the movement is over it is at opacity 1 and nothing runs. The poll (Refresh) does not bring it in again (it stands at 1 the
    whole time)."""
    import copy

    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, first = _family_views(json.loads(Path(fixture_file).read_text()))
    # (the answer in hand carries the windows of every family; one account of every window of Claude and of Codex is read in part)
    every = [g for family in FAMILY_IDS for g in (((served[family].get("reserve") or {}).get("summary") or {}).get("groups") or [])]
    fake = {"reserve": {"summary": {"groups": every}}}
    _partial(fake, str(FIVE_H), last=("b",))
    _partial(fake, str(WEEK), last=("b", "s2"))
    _partial(fake, str(MONTH), last=("s2",))
    served = {family: copy.deepcopy(view) for family, view in served.items()}
    for view in served.values():
        view["reserve"]["summary"]["groups"] = copy.deepcopy(every)
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_family_frame(page, served, first, 1280)
            frame.wait_for_timeout(300)
            # With the card in the plate the window's note stands in the card's .kt-sum, no longer a direct child of the plate.
            assert frame.evaluate("!!document.querySelector('.wt-plate .wt-foot:not(.wt-foot-hold)')"), "the card of the first family draws a note"
            for family in ("codex", "claude", "codex"):
                rec = _record(frame, lambda: frame.click('[data-focus="harness:%s"]' % family), 2000)
                frames = rec["frames"]
                k = _first_frame(frames, lambda f: f["heads"][0] == "harness:" + family)
                assert k is not None and k["foot"] is not None and k["foot"] < 0.05, ("the note is drawn with the rail, unseen", family, k)
                run = [f for f in frames if f["t"] >= k["t"] and f["foot"] is not None]
                rows = len(run[-1]["rows"])
                assert rows == 2, (family, run[-1]["rows"])
                row_in = _first_frame(run, lambda f: f["rows"][0] > 0.05)
                last_in = _first_frame(run, lambda f: f["rows"][-1] > 0.001)
                foot_in = _first_frame(run, lambda f: f["foot"] > 0.05)
                # (the ladder under the rings: its head and one step at least, and the word "N more" where its press opens something; the last of the
                # nodes drawn is the last row of the rail. The note comes in at the row after the steps' last: the word's row, or, with no word, the next)
                assert foot_in and len(foot_in["ladder"]) >= 2, (family, "the rail draws a ladder under the rings", foot_in and foot_in["ladder"])
                latest = lambda f: f["ladder"][-1] if f["ladder"] else 0
                began = lambda f: f["rows"][-1] > 0.001 and len(f["ladder"]) >= 2 and min(f["ladder"]) > 0.001   # (every ring and every row of the ladder has started)
                ladder_in = _first_frame(run, began)
                last_row_in = _first_frame(run, lambda f: latest(f) > 0.05)
                assert row_in and last_in and foot_in and ladder_in and last_row_in, (family, row_in, last_in, foot_in, ladder_in, last_row_in)
                # unseen while the last window or any row of the ladder has not started, and not before the first row shows; seen only when every row has started
                assert all(f["foot"] < 0.05 for f in run if f["rows"][-1] <= 0.001), ("unseen until the last window starts", family)
                assert all(f["foot"] < 0.05 for f in run if not began(f)), ("unseen until the last row of the ladder starts", family, [(round(f["t"] - k["t"]), round(f["foot"], 2)) for f in run if not began(f) and f["foot"] >= 0.05][:3])
                assert foot_in["t"] >= last_in["t"] and foot_in["t"] >= ladder_in["t"] and foot_in["t"] > row_in["t"], ("after the last window and the ladder", family, row_in["t"], last_in["t"], ladder_in["t"], foot_in["t"])
                assert all(o > 0 for o in foot_in["rows"] + foot_in["ladder"]), ("alone in an empty rail", family, foot_in)
                # and right after the last row of the ladder, not later: within the row's own step (--rail-row-step, 70 ms) of it, and 40 ms for the frame it is read in
                assert -40 <= foot_in["t"] - last_row_in["t"] <= 70 + 40, ("the note comes in with the last row of the ladder", family, round(foot_in["t"] - last_row_in["t"]))
                assert foot_in["t"] - k["t"] >= 380, ("not before the windows have begun", family, foot_in["t"] - k["t"])
                assert run[-1]["foot"] == 1 and run[-1]["running"] == 0, (family, run[-1])
                # (it only ever grows more visible: no flash)
                values = [f["foot"] for f in run]
                assert all(b >= a - 0.001 for a, b in zip(values, values[1:])), (family, [round(v, 2) for v in values if v < 1])
                # a poll runs none of it: the note stands at 1 the whole time (the pick of a window has its own rise of the note, the capsule's
                # wt-rise, that is not the movement of the rail and is not measured here)
                rec = _record(frame, lambda: frame.click('[data-focus="refresh"]'), 700)
                assert all(f["foot"] in (None, 1) for f in rec["frames"]), ("the poll does not bring the note in again", family)
                frame.wait_for_selector('[data-focus="refresh"]:not([disabled])')
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# In a real browser: a pointer that clicked a family and stands where it clicked gets no preview on the tab that slides under it (kit: mark tab, .tab-mark-still) until
# it moves. The tabs that give way stay dim for as long as the tab riding covers them, and a quick second pick dims them on from the look they have. A drawing in the
# middle of the slide goes on to the clock of the page, so the step of the tab picked at the frame of that drawing is no more than twice the steps beside it.

# What the head's tabs wear, frame after frame: where each stands, the ink of its mark and of its name, whether the browser holds the pointer
# on it (:hover), whether its row is still (.tab-mark-still), and every pointermove the page got.
HOVER_PROBE = """(ms) => {
  const rec = {frames: [], moves: [], start: performance.now()};
  window.__hover = rec;
  document.addEventListener('pointermove', (e) => rec.moves.push({t: performance.now() - rec.start, x: e.clientX, y: e.clientY}), true);
  const q = (s, r = document) => r.querySelector(s);
  const tick = () => {
    const t = performance.now() - rec.start, row = q('.harness-seg'), tabs = [...document.querySelectorAll('.harness-seg [data-mark-key]')];
    rec.frames.push({t, still: !!row && row.classList.contains('tab-mark-still'), heads: tabs.map((n) => n.getAttribute('data-focus')),
      pressed: tabs.map((n) => n.getAttribute('aria-pressed')), lefts: tabs.map((n) => n.getBoundingClientRect().left),
      ink: tabs.map((n) => getComputedStyle(q('.tab-mark-icon', n)).color), name: tabs.map((n) => getComputedStyle(q('.tab-mark-name', n)).color),
      hover: tabs.map((n) => n.matches(':hover'))});
    if (t < ms) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}"""
HOVER_DONE = "(ms) => !!window.__hover && performance.now() - window.__hover.start > ms + 60"
# The head now: is the row still, and for each tab the ink of its mark and of its name, :hover, :focus-visible, aria-pressed.
HEAD_NOW = """() => {
  const q = (s, r = document) => r.querySelector(s), row = q('.harness-seg'), out = {still: !!row && row.classList.contains('tab-mark-still'), tabs: {}};
  for (const n of document.querySelectorAll('.harness-seg [data-mark-key]')) {
    out.tabs[n.getAttribute('data-focus')] = {ink: getComputedStyle(q('.tab-mark-icon', n)).color, name: getComputedStyle(q('.tab-mark-name', n)).color,
      hover: n.matches(':hover'), visible: n.matches(':focus-visible'), pressed: n.getAttribute('aria-pressed')};
  }
  return out;
}"""


def _centre(frame, key):
    """The middle of a tab of the head in the coordinates of the page that holds the iframe (what the mouse of the page is moved by)."""
    box = frame.locator('[data-focus="%s"]' % key).bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_real_browser_a_pointer_that_clicked_a_family_and_stands_gets_no_preview_on_the_tab_that_slides_under_it_until_it_moves(fixture_file, engine, theme):
    """kit: mark tab (.tab-mark-still). A mouse that clicks the third of the head (Cursor, of Claude, Codex, Cursor, Antigravity) and stands: the tab
    that comes to the place under it (Codex, which steps one place right; not Claude, the family left, whose mark goes from the pick's red to the
    ink) is held under the pointer by the browser (:hover, in Chromium), yet the ink of its mark and of its name stand in the ink of a tab that
    stays (Antigravity) in every frame from the pick to the end of the slide and after the page is drawn again;
    the row wears .tab-mark-still all that time. The keyboard's preview is not taken away (a tab focused from the keyboard is red in a still row).
    The pointer then moves a few pixels: the tab under it is red (the preview by the pointer is back), the row is no more still, and the pointer
    on another tab lights that one and lets the first go. At 860 and 1280px, in both themes."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, first = _family_views(json.loads(Path(fixture_file).read_text()))
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for width in (860, 1280):
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                frame = _mount_family_frame(page, served, first, width, theme)
                frame.wait_for_timeout(300)
                heads = frame.evaluate(FAMILY_AXIS)["heads"]
                picked = heads[2]   # (the third of the head: the two before it step one place right, the pointer stays on its old place)
                at = heads.index(picked)
                assert at == 2 and len(heads) == 4, heads
                under, calm = heads[at - 1], heads[-1]   # (the tab that steps onto the place of the one picked, not the family left; a tab that stays and is never under the pointer)
                where = (engine, theme, width, heads, under, calm)
                # the pointer on the tab previews its pick (the mark turns red): the engine does hold a pointer on a tab, so the calm after is no accident
                x, y = _centre(frame, picked)
                page.mouse.move(x, y)
                frame.wait_for_timeout(450)
                lit = frame.evaluate(HEAD_NOW)["tabs"]
                red, ink = lit[picked]["ink"], lit[calm]["ink"]
                assert lit[picked]["hover"] and red != ink, ("the preview by the pointer is on before the pick", where, lit)
                # the click, and the head frame after frame
                frame.evaluate(HOVER_PROBE, 1300)
                page.mouse.down()
                page.mouse.up()
                frame.wait_for_function(HOVER_DONE, arg=1300, timeout=6000)
                rec = frame.evaluate("window.__hover")
                frames = rec["frames"]
                k = _first_frame(frames, lambda f: f["heads"][0] == picked)
                assert k is not None, ("the head puts the family picked first", where)
                run = [f for f in frames if f["t"] >= k["t"]]
                i = lambda f, key: f["heads"].index(key)
                assert all(f["still"] for f in run), ("the row is still from the pick on", where, [round(f["t"]) for f in run if not f["still"]][:5])
                bad = [(round(f["t"]), f["ink"][i(f, under)], f["name"][i(f, under)]) for f in run
                       if f["ink"][i(f, under)] != f["ink"][i(f, calm)] or f["name"][i(f, under)] != f["name"][i(f, calm)]]
                assert not bad, ("the tab that came under the pointer shows no preview, neither in its mark nor in its name", where, bad[:4], red)
                held = [round(f["t"]) for f in run if f["hover"][i(f, under)]]
                if engine == "chromium":
                    assert held, ("(the engine does hold the pointer on the tab that came under it: the calm is the widget's)", where)
                # a drawing under the resting pointer (a script's click: the pointer does not move) keeps the row still
                frame.evaluate("document.querySelector('[data-focus=\"refresh\"]').click()")
                frame.wait_for_selector('[data-focus="refresh"]:not([disabled])')
                frame.wait_for_timeout(350)
                now = frame.evaluate(HEAD_NOW)
                assert now["still"] and now["tabs"][under]["ink"] == now["tabs"][calm]["ink"] and now["tabs"][under]["name"] == now["tabs"][calm]["name"], (
                    "a drawing keeps the row still", where, now)
                # the keyboard's preview stays in a still row: a tab focused from the keyboard is red
                # (in Chromium: WebKit takes no :focus-visible for a focus a key moves to inside an iframe that a mouse clicked in, whatever the keys; the
                # keyboard's preview in WebKit is held by test_family_tabs.py, on the page itself)
                if engine == 'chromium':
                    other = heads[0]   # (Claude, the family left: second of the head now, its mark gone to the ink long since)
                    for _ in range(8):
                        page.keyboard.press("Tab")
                        if frame.evaluate("document.activeElement && document.activeElement.getAttribute('data-focus')") == other:
                            break
                    frame.wait_for_timeout(350)
                    now = frame.evaluate(HEAD_NOW)
                    assert now["still"] and now["tabs"][other]["visible"] and now["tabs"][other]["ink"] == red, ("the keyboard's preview is not taken away", where, now["tabs"][other])
                # the pointer moves a few pixels: the preview by the pointer is back on the tab under it, and the row is no more still
                page.mouse.move(x + 5, y + 3)
                frame.wait_for_timeout(450)
                now = frame.evaluate(HEAD_NOW)
                assert not now["still"] and now["tabs"][under]["hover"] and now["tabs"][under]["ink"] == red, ("after the move the preview is back", where, now)
                # and it goes on: the pointer on another tab lights that one and lets the first go
                x2, y2 = _centre(frame, calm)
                page.mouse.move(x2, y2)
                frame.wait_for_timeout(450)
                now = frame.evaluate(HEAD_NOW)
                assert now["tabs"][calm]["hover"] and now["tabs"][calm]["ink"] == red and now["tabs"][under]["ink"] == ink, ("the preview follows the pointer", where, now)
                assert errors == [], errors
                page.close()
        finally:
            browser.close()


def _alpha_step(frames, key, since, until=None):
    """The largest step of a tab's opacity from one frame to the next, from the first frame at or after `since`, and the time it came at (a frame
    that came late may step the more, so the step is read for 17 ms: a step over a longer frame is scaled down to that)."""
    series = [(f["t"], f["alpha"][f["heads"].index(key)]) for f in frames if f["t"] >= since and (until is None or f["t"] <= until)]
    steps = [(abs(b[1] - a[1]) * 17.0 / max(b[0] - a[0], 17.0), b[0]) for a, b in zip(series, series[1:])]
    return max(steps) if steps else (0.0, 0.0)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_tabs_that_give_way_stay_dim_while_the_tab_riding_covers_them_and_a_quick_second_pick_dims_them_on_from_their_look(fixture_file, engine):
    """kit: motion (rail-yield). (1) The last of the head picked: it rides left over the three, each of them steps one place right. In every frame
    of the slide in which the tab riding covers a tab that gives way by more than 20px, that tab is at opacity 0.35 at most (and there are such
    frames for each of the three); the three whole again at the end. (2) A quick second pick inside the first slide (120, 214 and 330 ms after
    the first click: Cursor, then Claude): the tabs that give way for the second pick - Cursor, that was riding and whole, and Codex, dim from the
    first - go on dimming from the look they have: no frame in which their opacity steps over 0.15 at the frame of the second pick (the flash
    0.31 -> 1 -> 0.43 of the old curve), none over the curve's own pace (0.25 a frame) in the rest, and they end whole."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, first = _family_views(json.loads(Path(fixture_file).read_text()))
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            # 1. one pick: the dimming holds while the rider covers the tab
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_family_frame(page, served, first, 1280)
            frame.wait_for_timeout(300)
            heads = frame.evaluate(FAMILY_AXIS)["heads"]
            up, others = heads[-1], heads[:-1]
            wide = {key: frame.evaluate("(id) => document.querySelector('[data-focus=\"' + id + '\"]').getBoundingClientRect().width", key) for key in heads}
            rec = _record(frame, lambda: frame.click('[data-focus="%s"]' % up), 1100)
            frames = rec["frames"]
            k = _first_frame(frames, lambda f: f["heads"][0] == up)
            assert k is not None
            run = [f for f in frames if f["t"] >= k["t"]]
            idx = lambda f, key: f["heads"].index(key)
            at = lambda f, key: f["lefts"][idx(f, key)]
            alpha = lambda f, key: f["alpha"][idx(f, key)]

            def covered(f, key):
                a, b = at(f, key), at(f, up)
                return min(a + wide[key], b + wide[up]) - max(a, b)

            for key in others:
                under = [f for f in run if covered(f, key) > 20]
                assert len(under) >= 3, ("the rider does cover this tab for some frames", engine, key, [round(covered(f, key)) for f in run][:40])
                # The dimming comes in over the first quarter of the slide (--yield-dim at 25%, a frame or two later on the screen than the click), and
                # the nearest tab is covered by 20px within that ramp: so the hold is held to 0.35 from the end of the ramp (100 ms after the first
                # frame) to the last frame the rider covers the tab by more than 20px, and the ramp itself to 0.5.
                held = [f for f in under if f["t"] - k["t"] >= 100]
                ramp = [f for f in under if f["t"] - k["t"] < 100]
                loud = [(round(f["t"] - k["t"]), round(alpha(f, key), 2), round(covered(f, key))) for f in held if alpha(f, key) > 0.35]
                assert held and not loud, ("a tab that gives way is dim for as long as the rider covers it by more than 20px", engine, key, loud)
                soft = [(round(f["t"] - k["t"]), round(alpha(f, key), 2), round(covered(f, key))) for f in ramp if alpha(f, key) > 0.5]
                assert not soft, ("and even in the ramp-in it is at 0.5 at most", engine, key, soft)
                assert alpha(frames[-1], key) == 1, key
            assert all(alpha(f, up) == 1 for f in frames), "the tab that rides is never dimmed"
            assert errors == [], errors
            page.close()
            # 2. a quick second pick: Codex, then (delay ms later) Claude
            for delay in (120, 214, 330):
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                frame = _mount_family_frame(page, served, first, 1280)
                frame.wait_for_timeout(300)
                heads = frame.evaluate(FAMILY_AXIS)["heads"]
                one, two = heads[2], heads[0]   # (Cursor, then Claude: Claude and Codex give way for the first, Cursor and Codex for the second)
                assert len(heads) == 4 and heads[1] not in (one, two), heads
                rec = _record(frame, lambda: (frame.click('[data-focus="%s"]' % one), frame.wait_for_timeout(delay),
                                                      frame.evaluate("(id) => document.querySelector('[data-focus=\"' + id + '\"]').click()", two)), 1700)   # (a script's click: it does not wait for the tab to stand still, nor does it miss a tab on the move)
                frames, clicks = rec["frames"], rec["clicks"]
                assert len(clicks) == 2, clicks
                k2 = _first_frame(frames, lambda f: f["heads"][0] == two and f["t"] >= clicks[1] - 30)
                assert k2 is not None, ("the second pick puts its family first", delay)
                n2 = frames.index(k2)
                since = frames[n2 - 1]["t"]
                giving = [key for key in k2["heads"] if "tab-mark-yield" in k2["classes"][idx(k2, key)] and "tab-mark-ride" not in k2["classes"][idx(k2, key)]]
                assert one in giving and len(giving) >= 2, ("the tabs that give way for the second pick: the first pick's rider and a tab dim from the first", delay, giving, k2["classes"])
                for key in giving:
                    # the step into the frame of the second pick and the step out of it (the drawing of the second pick is where a flash would show), then the rest
                    junction = _alpha_step(frames, key, since, frames[n2 + 1]["t"])
                    pace = _alpha_step(frames, key, frames[n2 + 1]["t"])
                    # (0.15 is the pace of the curve itself at its steepest in WebKit's first frame of the new dimming, 0.13-0.15 there; the flash it
                    # guards against is a step of 0.7 - a tab 0.31 whole in a frame, 0.43 the next - so 0.18 is the line)
                    assert junction[0] <= 0.18, ("no jump of opacity at the frame of the second pick", engine, delay, key, junction, [round(f["alpha"][idx(f, key)], 2) for f in frames[n2 - 2:n2 + 4]])
                    assert pace[0] <= 0.25, ("and none over the curve's own pace after it", engine, delay, key, pace)
                    assert frames[-1]["alpha"][idx(frames[-1], key)] == 1, (delay, key)
                assert frames[-1]["running"] == 0 and all(a == 1 for a in frames[-1]["alpha"]), frames[-1]
                page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("trigger", ["answer", "poll"])
def test_real_browser_the_step_of_the_tab_picked_at_the_frame_of_a_drawing_in_the_middle_of_the_slide_is_no_more_than_twice_the_steps_beside_it(fixture_file, engine, trigger):
    """kit: motion (the animations go on from the time of the pick on the page's clock). The last of the head picked, and the page drawn again in
    the middle of the slide - trigger "answer": the answer of the family held back 300 ms brings the chart in; trigger "poll": the interval's read
    250 ms after the click. In the frame that shows the page drawn again, the tab picked steps (by the clock: its path between two frames over the
    time between them) no more than twice the step into that frame and no more than twice the step out of it. Five runs in a row, each on a page of
    its own."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, first = _family_views(json.loads(Path(fixture_file).read_text()))
    served = _every_window_in_every_answer(served)
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for run_no in range(5):
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                frame = _mount_family_frame(page, served, first, 1280)
                frame.wait_for_timeout(300)
                heads = frame.evaluate(FAMILY_AXIS)["heads"]
                up = heads[-1]
                old = frame.evaluate(LEFT_OF, up)
                if trigger == "answer":
                    frame.evaluate(HOLD_BACK, 300)

                def act():
                    frame.click('[data-focus="%s"]' % up)
                    if trigger == "poll":
                        frame.wait_for_timeout(250)
                        frame.evaluate("document.dispatchEvent(new Event('visibilitychange'))")

                rec = _record(frame, act, 2000)
                frames, click = rec["frames"], rec["clicks"][0]
                k = _first_frame(frames, lambda f: f["heads"][0] == up)
                assert k is not None
                swap_at = next((t for t in rec["swaps"] if t > k["t"] + 40), None)
                assert swap_at is not None and 150 <= swap_at - click <= 450, ("the page is drawn again in the middle of the slide", engine, trigger, run_no, swap_at, click)
                j = next(n for n, f in enumerate(frames) if f["t"] == swap_at)
                way = [(f["t"], old - f["lefts"][f["heads"].index(up)]) for f in frames]
                pace = lambda n: (way[n][1] - way[n - 1][1]) / (way[n][0] - way[n - 1][0])
                before, at_swap, after = pace(j - 1), pace(j), pace(j + 1)
                assert before > 0 and after > 0 and at_swap > 0, ("the tab goes the one way through the drawing", engine, trigger, run_no, before, at_swap, after)
                assert at_swap <= 2 * before and at_swap <= 2 * after, (
                    "the step at the frame of the drawing is over twice the steps beside it", engine, trigger, run_no, round(before * 16.7, 1), round(at_swap * 16.7, 1),
                    round(after * 16.7, 1), "px a frame", "swap at", round(swap_at - click))
                assert errors == [], errors
                page.close()
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# The compact rail in a real browser (kit: rail). The share stands inside the ring, the short name of the window in the badge set in the gap
# of the ring, and nothing at the right of the ring: the rail is a narrow column.

COMPACT = """() => {
  const q = (s, r = document) => r.querySelector(s), all = (s, r = document) => [...r.querySelectorAll(s)];
  const cs = (n, p) => getComputedStyle(n, p), box = (n) => n.getBoundingClientRect();
  const rail = q('.rail'), rb = box(rail);
  const rows = all('.rail .tab-mark-rail');
  const ink = (v) => { const p = document.createElement('i'); p.style.color = 'var(' + v + ')'; document.body.appendChild(p); const c = cs(p).color; p.remove(); return c; };
  const accent = ink('--accent'), meta = ink('--text-meta'), primary = ink('--text-primary');
  const mk = q('.harness-seg [data-mark-key]'), mb = mk && box(mk), markCx = mb ? mb.left + mb.width / 2 : null;
  const wide = innerWidth >= 860;
  const tabs = rows.map((t, i) => {
    const ring = q('.ring', t), g = box(ring), count = q('.tab-mark-count', t), c = box(count), name = q('.tab-mark-name', t), nb = box(name);
    const path = q('.ring-track', ring), nums = path.getAttribute('d').match(/-?[\\d.]+/g).map(Number), r = nums[2];
    const stroke = Math.max(...all('.ring-track,.ring-arc', ring).map((p) => parseFloat(p.getAttribute('stroke-width'))));
    const unit = g.width / ring.viewBox.baseVal.width, inner = (r - stroke / 2) * unit, cx = g.left + g.width / 2, cy = g.top + g.height / 2;
    const far = Math.max(...[[c.left, c.top], [c.right, c.top], [c.left, c.bottom], [c.right, c.bottom]].map(([x, y]) => Math.hypot(x - cx, y - cy)));
    const before = cs(t, '::before'), tb = box(t);
    // the badge: the circle the ring draws (its centre and its radii in the svg's units, carried over by the box's scale), the name over it
    const bc = q('.ring-badge', ring), bsw = bc ? parseFloat(bc.getAttribute('stroke-width')) : 0;
    const badge = bc ? {cx: g.left + parseFloat(bc.getAttribute('cx')) * unit, cy: g.top + parseFloat(bc.getAttribute('cy')) * unit,
      outer: (parseFloat(bc.getAttribute('r')) + bsw / 2) * unit, inner: (parseFloat(bc.getAttribute('r')) - bsw / 2) * unit, shown: cs(bc).display !== 'none', fill: cs(bc).fill, stroke: cs(bc).stroke,
      strokeWidth: bsw * unit} : null;
    const lowest = {x: g.left + 92 * unit, y: g.top + (92 + r) * unit};
    const ends = [[nums[0], nums[1]], [nums[7], nums[8]]].map(([x, y]) => ({x: g.left + x * unit, y: g.top + y * unit}));
    const capR = Math.max(...all('.ring-arc', ring).map((p) => parseFloat(p.getAttribute('stroke-width'))).concat([stroke])) / 2 * unit;
    const rg = document.createRange(); rg.selectNodeContents(name); const tr = rg.getBoundingClientRect();
    const star = q('.tab-mark-note', t), sb = star && box(star);
    return {i, text: count.textContent, none: !count.querySelector('.tab-mark-unit'), picked: t.getAttribute('aria-selected') === 'true',
      ring: {top: g.top, bottom: g.bottom, left: g.left, right: g.right, width: g.width}, inner, far, margin: inner - far, cx, cy, tab: {top: tb.top, bottom: tb.bottom, left: tb.left, right: tb.right, height: tb.height},
      count: {left: c.left, right: c.right, top: c.top, bottom: c.bottom, size: cs(count).fontSize, weight: cs(count).fontWeight, color: cs(count).color},
      word: name.textContent, wordColor: cs(name).color, wordSize: cs(name).fontSize, wordWeight: cs(name).fontWeight, wordBox: {left: nb.left, right: nb.right},
      text_: {w: tr.width, cx: tr.left + tr.width / 2, cy: tr.top + tr.height / 2}, boxScale: g.width / parseFloat(cs(ring).width), badge, lowest, ends, capR,
      star: star ? {left: sb.left, right: sb.right, top: sb.top, bottom: sb.bottom, size: cs(star).fontSize, color: cs(star).color} : null,
      wordRight: Math.max(...[name, star].filter(Boolean).map((n) => box(n).right)), parent: name.parentNode.className, rowKids: t.children.length,
      thread: {on: before.content !== 'none', top: tb.top + parseFloat(before.top), height: parseFloat(before.height), left: tb.left + parseFloat(before.left), width: parseFloat(before.width)}};
  });
  const clipped = all('.rail *').filter((n) => cs(n).textOverflow === 'ellipsis' && n.scrollWidth > n.clientWidth + 1).map((n) => n.className);
  // no text of the rail is wider than its own box (an ellipsis or a hidden overflow would cut it; the words only for a screen reader are clipped on purpose), and the rail itself holds its rows
  const cut = all('.rail *').filter((n) => !(n instanceof SVGElement) && !n.closest('.sr-only') && n.clientWidth > 0 && n.scrollWidth > n.clientWidth + 1).map((n) => n.className)
    .concat(rail.scrollWidth > rail.clientWidth + 1 ? ['.rail'] : []);
  // the stamp at the foot: every line of its text stands inside the rail (no line runs out of the column or is cut)
  const stamp = q('.wt-plate .wt-stamp'); let stampRight = null, stampLeft = null, stampLines = 0;
  if (stamp) {
    const w = document.createTreeWalker(stamp, NodeFilter.SHOW_TEXT); let n; stampRight = -1; stampLeft = 1e9;
    while ((n = w.nextNode())) {
      if (n.parentNode.closest('.sr-only') || !n.data.trim()) continue;
      const r = document.createRange(); r.selectNodeContents(n);
      for (const k of r.getClientRects()) { stampRight = Math.max(stampRight, k.right); stampLeft = Math.min(stampLeft, k.left); }
    }
    stampLines = new Set([...stamp.querySelectorAll('.wt-read,.wt-n,.wt-age')].map((e) => Math.round(box(e).top))).size;
  }
  return {width: innerWidth, scroll: document.documentElement.scrollWidth, rail: {left: rb.left, right: rb.right, width: rb.width}, clipped, cut, markCx, accent, meta, primary, edge: ink('--ring-badge-edge'), track: ink('--ring-track'), arc: ink('--ring-arc'),
    dots: rail.textContent.includes('\\u2026'), stampRight, stampLeft, stampLines, railStamp: !!q('.rail .wt-stamp') || !!q('.rail-bot'), plate: box(q('.wt-plate')), tabs};
}"""


# The widest words of the badge ("30D" in English, "30д" in Russian) put in every badge, whatever the row says: the air that is left between the
# word and the inner edge of the circle (the word is a box as wide as its text and as high as the digits are, 0.72 of the font; its corners are the farthest
# points), in the row picked (x1.2) and in the others. The row is put back as it was.
WIDEST = """() => {
  const out = [];
  for (const t of document.querySelectorAll('.rail .tab-mark-rail')) {
    const name = t.querySelector('.tab-mark-name'), ring = t.querySelector('.ring'), was = name.textContent, g = ring.getBoundingClientRect();
    const bc = ring.querySelector('.ring-badge'), unit = g.width / ring.viewBox.baseVal.width, sw = parseFloat(bc.getAttribute('stroke-width'));
    const inner = (parseFloat(bc.getAttribute('r')) - sw / 2) * unit, bx = g.left + parseFloat(bc.getAttribute('cx')) * unit, by = g.top + parseFloat(bc.getAttribute('cy')) * unit;
    for (const word of ['30D', '30\\u0434', '5H', '7D', '5\\u0447', '7\\u0434']) {
      name.textContent = word;
      const rg = document.createRange(); rg.selectNodeContents(name); const tr = rg.getBoundingClientRect();
      // the ink of the word: the box of its glyphs on a canvas in the same face (the origin of the text is the left of its range, the baseline the ascent under its top)
      const k = g.width / parseFloat(getComputedStyle(ring).width), st = getComputedStyle(name), cv = document.createElement('canvas').getContext('2d');
      cv.font = st.fontWeight + ' ' + st.fontSize + ' ' + st.fontFamily;
      const m = cv.measureText(word), base = tr.top + m.fontBoundingBoxAscent * k;
      const xs = [tr.left - m.actualBoundingBoxLeft * k, tr.left + m.actualBoundingBoxRight * k], ys = [base - m.actualBoundingBoxAscent * k, base + m.actualBoundingBoxDescent * k];
      const far = Math.max(...xs.flatMap((x) => ys.map((y) => Math.hypot(x - bx, y - by))));
      out.push({word, picked: t.getAttribute('aria-selected') === 'true', weight: getComputedStyle(name).fontWeight, size: getComputedStyle(name).fontSize,
        air: inner - far, w: tr.width, centre: Math.hypot((tr.left + tr.width / 2) - bx, (tr.top + tr.height / 2) - by)});
    }
    name.textContent = was;
  }
  return out;
}"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_compact_rail_holds_its_share_inside_its_ring_and_the_short_name_in_a_badge_set_in_the_gap_of_the_ring(fixture_file, engine):
    """kit: rail / ring. The rail is a column of 80 px at most (the ring with its share inside it and, in the gap of the ring, a small circle - the
    badge - that holds the short name of the window; nothing at the right of the ring): from 860px the centre of the badge is on the lowest point of the
    ring's path (0.5px), the name is on its centre (0.5px), "30D" and "30д" stand inside it with 1px of air at least (by the ink of the glyphs, measured on a canvas in the
    same face; 7.5px), the circle does not cut the round ends of the arc by more than 2px, and it does not touch the share;
    the same for the ring picked (x1.2) and not; the share stands inside the hole of the ring with 2px of air from the arc; the thread between two rows is 8px long or more and keeps 2px (+-1) under the badge above (the star hangs a hair under the badge) and 2px (+-1) over the ring below,
    whichever ring is picked (x1.2) or none; no word and no share is cut by an ellipsis, the rail has no stamp (it stands whole inside the plate); at 282, 360, 859, 860 and 1280px the page has no sideways scroll; on the narrow shell the badge circle is not drawn, the short name is
    the plain word of the capsule and the share stands at its end."""
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    seen = {"hundred": 0, "dash": 0, "pairs": 0, "up": 0, "free": 0, "down": 0, "lengths": [], "narrow": 0, "badges": 0, "widest": set(), "stars": 0, "stamp": 0}
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for key in ("reading_part", "reading_zero", "codex_states"):
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                frame = _mount_layout_frame(page, views[key], 678)
                for width in (282, 360, 859, 860, 1020, 1280):
                    page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                    frame.wait_for_function("(width) => innerWidth === width", arg=width)
                    g = frame.evaluate(COMPACT)
                    where = (engine, key, width)
                    assert g["scroll"] <= g["width"] + 1 and g["clipped"] == [] and g["cut"] == [] and not g["dots"] and g["tabs"], (where, g)
                    if width >= 860:
                        assert g["rail"]["width"] <= 122.5, (where, g["rail"])  # rule: the widest tab (114px) and the gap, not 86.5px
                        picks = len(g["tabs"])
                        for at in range(picks):
                            if picks > 1:
                                frame.locator(".rail .tab-mark-rail").nth(at).click()
                            frame.wait_for_timeout(700)
                            # '100%' in place of the figure of every window that has one (the figure is text, so the box of '100' is the widest it gets)
                            frame.evaluate("() => { for (const c of document.querySelectorAll('.rail .tab-mark-count')) if (c.querySelector('.tab-mark-unit')) c.firstChild.data = '100'; }")
                            h = frame.evaluate(COMPACT)
                            assert h["scroll"] <= h["width"] + 1 and h["clipped"] == [] and h["cut"] == [], (where, at, h)
                            assert sum(t["picked"] for t in h["tabs"]) <= 1, (where, at, h)
                            # The rail holds no stamp (no foot); the stamp stands whole inside the plate, in one line (tight, under 640 px, it may wrap onto the next)
                            assert h["railStamp"] is False, ("the rail has no foot and no stamp", where, at)
                            if h["stampRight"] is not None:
                                assert h["stampRight"] <= h["plate"]["right"] + 0.5 and h["stampLeft"] >= h["plate"]["left"] - 0.5, ("the stamp inside the plate", where, at, h)
                                assert h["stampLines"] == 1 or h["width"] < 640, ("one line", where, at, h)
                                seen["stamp"] += 1
                            for t in h["tabs"]:
                                # inside the hole of the ring, 2px of air from the arc at least (the ring picked is x1.2: its hole and its figure grow together)
                                assert t["margin"] >= 2 - 0.01, ("the share inside the ring with air", where, at, t)
                                assert abs((t["count"]["left"] + t["count"]["right"]) / 2 - t["cx"]) <= 0.6, ("and on its middle", where, at, t)
                                # every ring of the rail stands on the axis, the middle of the whole tab of the family picked (it was the middle of its mark; markCx is the tab's now)
                                assert abs(t["cx"] - h["markCx"]) <= 0.5, ("the ring on the axis of the tab", where, at, t["cx"], h["markCx"])
                                assert (t["count"]["size"], t["count"]["weight"]) == ("11px", "600"), (where, at, t)
                                # the figure of the ring picked is the accent, a dash too, the others are the meta ink
                                want = h["accent"] if t["picked"] else h["meta"]
                                assert t["count"]["color"] == want, ("the ink of the figure", where, at, t["count"]["color"], want)
                                assert (abs(t["ring"]["width"] - (56.4 if t["picked"] else 47)) <= 0.6), ("the ring is 47 px, x1.2 picked", where, at, t)
                                # the badge: in the box of the ring, on the lowest point of the path, the name on its centre, the row holds the box alone
                                b = t["badge"]
                                assert b and b["shown"] and t["rowKids"] == 1 and "tab-mark-ring" in t["parent"], ("the badge is in the box of the ring", where, at, t)
                                assert abs(b["cx"] - t["lowest"]["x"]) <= 0.5 and abs(b["cy"] - t["lowest"]["y"]) <= 0.5, ("the badge on the lowest point of the path", where, at, b, t["lowest"])
                                assert abs(t["text_"]["cx"] - b["cx"]) <= 0.5 and abs(t["text_"]["cy"] - b["cy"]) <= 0.5, ("the name on the centre of the badge", where, at, t["text_"], b)
                                assert abs(b["strokeWidth"] - 5 * t["ring"]["width"] / 184) <= 0.05, ("a stroke thinner than the track's", where, at, b)
                                # the edge of the badge is its own tone: stronger than the track's, quieter than the arc's (it is seen on the dark ground too)
                                alpha = lambda c: float(c.rstrip(")").split(",")[3]) if c.count(",") == 3 else 1.0
                                assert alpha(h["track"]) < alpha(h["edge"]) < alpha(h["arc"]), ("the edge of the badge between the track and the arc", where, at, h["track"], h["edge"], h["arc"])
                                assert b["stroke"].replace(" ", "") == h["edge"].replace(" ", ""), ("the badge wears the edge token", where, at, b["stroke"], h["edge"])
                                assert (t["wordSize"], t["wordWeight"]) == ("7.5px", "600"), ("the name is 7.5px, semibold", where, at, t)
                                assert t["wordColor"] == (h["primary"] if t["picked"] else h["meta"]), ("the ink of the name", where, at, t["wordColor"])
                                # the circle cuts the round ends of the arc by 2px at most, and does not touch the share
                                over = max(b["outer"] + t["capR"] - ((e["x"] - b["cx"]) ** 2 + (e["y"] - b["cy"]) ** 2) ** 0.5 for e in t["ends"])
                                assert over <= 2 + 0.01, ("the badge on the ends of the arc by 2px at most", where, at, over, t)
                                assert b["cy"] - b["outer"] >= t["count"]["bottom"], ("the badge does not touch the share", where, at, b, t["count"])
                                if t["star"]:
                                    # the star of a window not read whole: 9px, a bead on the axis at the lower edge of the badge: the middle of its glyph (.38 of its box under the top)
                                    # is about 1.2px over the edge (1px either way), so the ink hangs a hair under it, and the thread under it starts 1 to 3px from its ink
                                    k = t["boxScale"]
                                    middle = t["star"]["top"] + 0.378 * 9 * k
                                    assert t["star"]["size"] == "9px" and abs((t["star"]["left"] + t["star"]["right"]) / 2 - b["cx"]) <= 0.6, ("the star on the axis", where, at, t["star"], b)
                                    assert abs(middle - (b["cy"] + b["outer"] - 1.2 * k)) <= 1.0, ("the star on the lower edge of the badge", where, at, middle, t["star"], b)
                                    t["starInkBottom"] = middle + 3.36 / 2 * k
                                    seen["star_gap"] = min(seen.get("star_gap", 9), (b["cy"] + b["outer"]) - (middle + 3.36 / 2 * k) + 2)
                                    seen["stars"] += 1
                                seen["badges"] += 1
                                if t["none"]:
                                    seen["dash"] += 1
                                else:
                                    seen["hundred"] += 1
                            # the thread between two rows keeps 2px (+-1) under the badge above (or the star that hangs under it) and 2px (+-1) over the ring below whichever ring is
                            # picked (a ring picked is x1.2: its top goes up, its badge goes down), and is 8px long at least
                            for above, below in zip(h["tabs"], h["tabs"][1:]):
                                assert below["thread"]["on"] and below["thread"]["width"] > 0, (where, at, below)
                                start, end = below["thread"]["top"], below["thread"]["top"] + below["thread"]["height"]
                                low = max(above["ring"]["bottom"], above["badge"]["cy"] + above["badge"]["outer"])
                                assert 1 <= start - low <= 3, ("the thread starts a hair under the badge above", where, at, above, below)
                                assert below["thread"]["height"] >= 8 - 0.01, ("the thread between two rows is 8px long at least", where, at, below["thread"]["height"], above, below)
                                assert 1 <= below["ring"]["top"] - end <= 3, ("and stops 2px (+-1) over the ring below, picked or not", where, at, above, below)
                                if above["star"]:
                                    assert 1 <= start - above["starInkBottom"] <= 3, ("and 2px (+-1) under the star", where, at, above, below)
                                seen["up" if above["picked"] else "down" if below["picked"] else "free"] += 1
                                seen["lengths"].append(round(below["thread"]["height"], 2))
                                assert abs(below["thread"]["left"] + below["thread"]["width"] / 2 - below["cx"]) <= 0.6, ("on the axis", where, at, below)
                                seen["pairs"] += 1
                            # the words are the short ones, whole
                            assert all(1 <= len(t["word"]) <= 3 for t in h["tabs"]), (where, [t["word"] for t in h["tabs"]])
                            # the widest words are inside the circle with 1px of air at least (by the ink), on its centre, picked (x1.2) or not
                            for w in frame.evaluate(WIDEST):
                                assert w["air"] >= 1 - 0.01 and w["centre"] <= 0.6, ("the word inside the badge with air", where, at, w)
                                assert (w["size"], w["weight"]) == ("7.5px", "600"), (where, at, w)
                                seen["widest"].add((w["word"], w["picked"]))
                                seen["air"] = min(seen.get("air", 9), w["air"])
                        # no row is picked (the attribute taken off every row): the thread between two rows that touch no ring picked is 2px (+-1) under the badge above and 2px (+-1)
                        # over the ring below (the rings and the badges are at their rest size: the pointer is taken off the rail, a row under it would grow to its preview)
                        page.mouse.move(2, 2)
                        frame.evaluate("() => { for (const t of document.querySelectorAll('.rail .tab-mark-rail')) t.setAttribute('aria-selected', 'false'); }")
                        frame.wait_for_timeout(700)
                        f = frame.evaluate(COMPACT)
                        for above, below in zip(f["tabs"], f["tabs"][1:]):
                            assert not above["picked"] and not below["picked"] and below["thread"]["on"], (where, above, below)
                            assert below["thread"]["height"] >= 8 - 0.01, ("the thread between two rows at rest is 8px long at least", where, below["thread"]["height"])
                            low = max(above["ring"]["bottom"], above["badge"]["cy"] + above["badge"]["outer"])
                            assert 1 <= below["thread"]["top"] - low <= 3 and 1 <= below["ring"]["top"] - (below["thread"]["top"] + below["thread"]["height"]) <= 3, ("the thread's two gaps at rest", where, above, below)
                            seen["free"] += 1
                        # (put the figures back as the widget had them)
                        page.close()
                        page = browser.new_page(viewport={"width": 1400, "height": 900})
                        frame = _mount_layout_frame(page, views[key], 678)
                        page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                        frame.wait_for_function("(width) => innerWidth === width", arg=width)
                    else:
                        for t in g["tabs"]:
                            # on the narrow shell the circle is not drawn, the name is the plain word of the capsule and the share stands at the end of the capsule,
                            # after the word and the star, inside the capsule
                            assert t["badge"] is None or not t["badge"]["shown"], ("no badge circle in a capsule", where, t)
                            assert t["wordBox"]["left"] >= t["ring"]["right"] - 0.5, ("the word after the ring", where, t)
                            assert t["count"]["left"] >= t["wordRight"] - 0.5 and t["count"]["right"] <= t["tab"]["right"] + 0.5, ("the share after the word", where, t)
                            assert abs(t["tab"]["height"] - 32) <= 0.6 and t["ring"]["left"] >= t["tab"]["left"] - 0.5, where
                            seen["narrow"] += 1
                assert errors == [], errors
                page.close()
        finally:
            browser.close()
    assert seen["hundred"] >= 6 and seen["dash"] >= 1 and seen["pairs"] >= 4 and seen["narrow"] >= 6 and seen["badges"] >= 8 and seen["stars"] >= 1 and seen["stamp"] >= 3, seen
    assert seen["up"] >= 2 and seen["free"] >= 1 and seen["down"] >= 2, ("the thread was measured with the row above picked and not", seen)
    assert {w for w, _ in seen["widest"]} == {"30D", "30д", "5H", "7D", "5ч", "7д"} and {p for _, p in seen["widest"]} == {True, False}, seen


# ---------------------------------------------------------------------------
# The hover and the press of a row of the rail in a real browser (kit: rail, motion): the app's way, made of tokens.

HOVER_PRESS = """() => {
  const ink = (v) => { const p = document.createElement('i'); p.style.color = 'var(' + v + ')'; document.body.appendChild(p); const c = getComputedStyle(p).color; p.remove(); return c; };
  return {rest: ink('--ring-arc'), hover: ink('--ring-arc-hover'), on: ink('--ring-arc-on'), edge: ink('--ring-badge-edge'), edgeHover: ink('--ring-badge-edge-hover'),
    rows: [...document.querySelectorAll('.rail .tab-mark-rail')].map((t) => {
      // the wide rail moves the box of the ring; on the narrow shell the box is display:contents and the ring (the svg) moves
      const box = t.querySelector('.tab-mark-ring'), arc = t.querySelector('.ring-arc'), badge = t.querySelector('.ring-badge');
      const moved = getComputedStyle(box).display === 'contents' ? t.querySelector('.ring') : box;
      return {picked: t.getAttribute('aria-selected') === 'true', scale: new DOMMatrix(getComputedStyle(moved).transform).a, arc: arc ? getComputedStyle(arc).stroke : null,
        edge: getComputedStyle(badge).stroke, duration: getComputedStyle(moved).transitionDuration};
    })};
}"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_a_rail_row_darkens_its_ring_under_the_pointer_presses_it_in_and_lets_it_spring_back(fixture_file, engine):
    """kit: rail, motion. A row not picked under the pointer grows its ring to x1.12 and takes the arc and the edge of the badge a step darker (the tokens
    --ring-arc-hover and --ring-badge-edge-hover, between the rest and the picked look); pressed (:active) its ring is --press (.94) of that size, quickly; let go
    it springs back (the base rule's --nav-spring) and the pick makes it x1.2. Under reduced motion the press does not shrink the ring and nothing is animated.
    The same on the wide rail at 1280 and 860px and on the narrow shell at 600px (there the ring itself moves: the box of the ring is display:contents)."""
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for reduced, width in ((False, 1280), (False, 860), (False, 600), (True, 1280), (True, 600)):
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                if reduced:
                    page.emulate_media(reduced_motion="reduce")
                frame = _mount_layout_frame(page, views["claude"], 678)
                page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                frame.wait_for_function("(width) => innerWidth === width", arg=width)
                page.wait_for_timeout(1200)
                where = (engine, reduced, width)
                rest = frame.evaluate(HOVER_PRESS)
                other = next(i for i, r in enumerate(rest["rows"]) if not r["picked"])
                assert all(abs(r["scale"] - (1.2 if r["picked"] else 1)) <= 0.01 for r in rest["rows"]), (where, rest)
                assert all(r["arc"] == (rest["on"] if r["picked"] else rest["rest"]) for r in rest["rows"] if r["arc"]), (where, rest)
                row = frame.locator(".rail .tab-mark-rail").nth(other)
                row.hover()
                page.wait_for_timeout(800)
                over = frame.evaluate(HOVER_PRESS)["rows"][other]
                assert abs(over["scale"] - 1.12) <= 0.01 and over["arc"] == rest["hover"] and over["edge"] == rest["edgeHover"], ("the preview", where, over, rest)
                page.mouse.down()
                page.wait_for_timeout(400)
                pressed = frame.evaluate(HOVER_PRESS)["rows"][other]
                if reduced:
                    assert abs(pressed["scale"] - 1) <= 0.01 and pressed["duration"] == "0s", ("no shrink under reduced motion", where, pressed)
                else:
                    assert abs(pressed["scale"] - 0.94) <= 0.01, ("pressed in to --press", where, pressed)
                page.mouse.up()
                page.wait_for_timeout(1200)
                after = frame.evaluate(HOVER_PRESS)["rows"]
                assert after[other]["picked"] and abs(after[other]["scale"] - 1.2) <= 0.01 and after[other]["arc"] == rest["on"], ("let go, the pick makes it x1.2", where, after)
                assert errors == [], errors
                page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1280, 600])
def test_real_browser_a_finger_tap_on_a_rail_row_picks_it_and_its_ring_comes_to_the_size_of_the_pick(fixture_file, width):
    """kit: rail, motion. A tap of a finger on a row not picked picks it: its ring ends at x1.2 (--mark-on) and the arc takes the ink of the pick, on the wide rail
    (1280px, the box of the ring moves) and on the narrow shell (600px, the ring itself). Chromium alone (WebKit has no touch here). The press itself (:active,
    --press) is the mouse test's: a finger held on a row is delivered by the DevTools protocol as touchstart and pointerdown, but headless Chromium sets no :active
    for it (checked at 100, 200 and 300ms: nothing matches :active), so a finger held cannot be tested here - it is the same pseudo-class the mouse test presses."""
    from test_bars import _mount_layout_frame
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, "chromium")
        try:
            context = browser.new_context(has_touch=True, viewport={"width": 1400, "height": 900})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_layout_frame(page, views["claude"], 678)
            page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
            frame.wait_for_function("(width) => innerWidth === width", arg=width)
            page.wait_for_timeout(1200)
            rest = frame.evaluate(HOVER_PRESS)
            other = next(i for i, r in enumerate(rest["rows"]) if not r["picked"])
            row = frame.locator(".rail .tab-mark-rail").nth(other)
            row.scroll_into_view_if_needed()
            box = row.bounding_box()
            page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.wait_for_timeout(1200)
            after = frame.evaluate(HOVER_PRESS)["rows"][other]
            assert after["picked"] and abs(after["scale"] - 1.2) <= 0.01 and after["arc"] == rest["on"], ("the tap picks the row, its ring is x1.2", width, after, rest["on"])
            assert errors == [], errors
            context.close()
        finally:
            browser.close()


# The sweep of a family's pick, frame by frame: every ring of the rail that wears ring-sweep (index = row), the arc as long as the share less the dash
# offset, and the dot against the end of that arc (the angle 120 + 3 x length, on the ring's radius).
FAMILY_SWEEP_REC = """(ms) => {
  window.__sweep = null;
  const t0 = performance.now(), rings = {};
  const step = () => {
    const t = performance.now() - t0;
    [...document.querySelectorAll('.tab-mark-rail')].forEach((row, i) => {
      const ring = row.querySelector('.ring-sweep');
      if (!ring) return;
      const arc = ring.querySelector('.ring-arc'), dot = ring.querySelector('.ring-dot');
      const share = parseFloat(ring.style.getPropertyValue('--ring-share'));
      const off = parseFloat(getComputedStyle(arc).strokeDashoffset) || 0;
      const rect = ring.getBoundingClientRect(), d = dot.getBoundingClientRect(), k = rect.width / 184, len = share - off;
      const th = (120 + 3 * len) * Math.PI / 180;
      (rings[i] = rings[i] || []).push({t, share, len, err: Math.hypot((d.left + d.right) / 2 - (rect.left + (92 + 81 * Math.cos(th)) * k),
        (d.top + d.bottom) / 2 - (rect.top + (92 + 81 * Math.sin(th)) * k))});
    });
    if (t < ms) requestAnimationFrame(step);
    else window.__sweep = {rings, left: document.getAnimations().filter((a) => String(a.animationName || '').indexOf('ring-sweep') === 0).length,
      swept: document.querySelectorAll('.ring-sweep').length};
  };
  requestAnimationFrame(step);
}"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_pick_of_a_family_sweeps_every_ring_of_its_rail_from_nothing_to_its_share_row_after_row_and_the_dot_runs_with_it(fixture_file, engine):
    """kit: ring, motion + rail. The pick of a family (a click on its tab) at 1280px: the rings of its rail wear ring-sweep (one per row with a share);
    frame by frame each arc grows, never shrinking, from about nothing to its share, the dot standing at the end of the arc within 1px at every
    frame; the rows go in order (the end of a row is not before the end of the row over it) and the whole show is over in 1600 ms from the click;
    then no sweep animation is left. Under reduced motion the pick of a family brings the plain rings: no sweep class, no animation."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    served, first = _family_views(json.loads(Path(fixture_file).read_text()))
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_family_frame(page, served, first, 1280)
            frame.click('[data-focus="harness:codex"]')
            _settle_family(frame, "codex")
            frame.evaluate(FAMILY_SWEEP_REC, 2200)
            frame.click('[data-focus="harness:claude"]')
            frame.wait_for_function("() => window.__sweep !== null", timeout=8000)
            got = frame.evaluate("window.__sweep")
            rows = [got["rings"][key] for key in sorted(got["rings"], key=int)]
            where = (engine, {key: (v[:2], v[-2:]) for key, v in got["rings"].items()}, got["left"])
            assert len(rows) >= 2, ("every row with a share sweeps", where)
            ends = []
            for frames in rows:
                assert len(frames) >= 10, where
                assert frames[0]["len"] <= 0.12 * frames[0]["share"] + 1, ("it starts from about nothing", where)
                assert all(b["len"] >= a["len"] - 0.05 for a, b in zip(frames, frames[1:])), ("the arc never shrinks", where)
                assert abs(frames[-1]["len"] - frames[-1]["share"]) <= 0.05, ("and ends on its share", where)
                assert max(f["err"] for f in frames) <= 1, ("the dot stands at the end of the arc", where)
                end = next(f["t"] for f in frames if f["len"] >= f["share"] - 0.05)
                assert end <= 1600, ("the show is over in time", end, where)
                ends.append(end)
            assert all(b >= a - 40 for a, b in zip(ends, ends[1:])), ("the rows go in order", ends, where)
            assert got["left"] == 0, ("no sweep animation is left", where)
            assert errors == [], errors
            page.close()
            page = browser.new_page(viewport={"width": 1400, "height": 900}, reduced_motion="reduce")
            frame = _mount_family_frame(page, served, first, 1280)
            frame.click('[data-focus="harness:codex"]')
            _settle_family(frame, "codex")
            frame.evaluate(FAMILY_SWEEP_REC, 800)
            frame.click('[data-focus="harness:claude"]')
            frame.wait_for_function("() => window.__sweep !== null", timeout=8000)
            quiet = frame.evaluate("window.__sweep")
            assert quiet["swept"] == 0 and quiet["left"] == 0 and quiet["rings"] == {}, ("reduced motion: the plain rings", quiet)
            page.close()
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# The ladder of resets under the rings (kit: rail, the second part), in real Chromium and WebKit at the widths of the widget from 860px.
# The data are the fixture's families (Claude, Codex, Antigravity: one reset each, the same time for every account) and, for several steps, the
# windows of the tests above (claude_five: four resets an hour apart; claude_many: ten accounts in clusters of three, _clustered_resets, so four steps; codex_states: two).
# What the fixture's own accounts give back, step by step (the share they leave unused: codex_states = 0.4 + 0.2 + 0.5 of the three that are not at the limit): soonest first.
LADDER_AMOUNTS = {"claude_five": ["+1.00", "+1.00", "+1.00", "+0.30"], "codex_states": ["+3.00", "+1.10"], "claude_many": ["+3.00", "+3.00", "+3.00", "+1.00"]}
# (claude_many as _clustered_resets sets it: four steps; as _glued_resets sets it: the first three clusters are one step)
LADDER_AMOUNTS_GLUED = ["+9.00", "+1.00"]


def _ladder_words(lang):
    """The answer of the text route: English alone, or the install's Russian (conftest.RU_JSON, as the other tests read it)."""
    texts = json.loads(Path(__file__).with_name("strings_en.json").read_text(encoding="utf-8"))
    if lang != "ru":
        return {"language": "", "revision": 0, "plural_select": None, "strings": {}, "en": texts}
    if RU_JSON is None:
        pytest.skip(RU_JSON_SKIP)
    entries = json.loads(RU_JSON.read_text(encoding="utf-8"))["entries"]
    strings = {k[len("code:quotas.ui."):]: (v["text"] if "text" in v else v["forms"]) for k, v in entries.items() if k.startswith("code:quotas.ui.")}
    pick = lambda n: "one" if n % 10 == 1 and n % 100 != 11 else "few" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "many"
    return {"language": "ru", "revision": 1, "plural_select": {"map": {str(n): pick(n) for n in range(101)}, "period": 100}, "strings": strings, "en": texts}


def _mount_ladder_frame(page, served, first, width, lang="en", theme="light"):
    """_mount_family_frame with a language: the unmodified widget in an iframe of the width given, the transport alone stubbed."""
    import html as html_lib

    source = Path(__file__).with_name("widget.js").read_text()
    words = json.dumps(_ladder_words(lang))
    stub = ("const SERVED = " + json.dumps(served) + ", FIRST = " + json.dumps(first) + ", WORDS = " + json.dumps(words) + ";"
            "window.fetch = async (u) => { const url = String(u), hit = /[?&]harness=([^&]*)/.exec(url);"
            " const body = url.indexOf('/i18n') >= 0 ? WORDS : JSON.stringify(SERVED[hit ? decodeURIComponent(hit[1]) : FIRST] || SERVED[FIRST]);"
            " return {ok: true, status: 200, text: async () => body}; };")
    doc = ('<!doctype html><html data-theme="' + theme + '"><head></head><body><div id="root"></div><script>' + stub + "</script><script>"
           + source.replace("</script", "<\\/script") + "</script></body></html>")
    page.set_content('<iframe sandbox="allow-scripts" style="border:0;width:' + str(width) + 'px;height:1400px" srcdoc="'
                     + html_lib.escape(doc, quote=True) + '"></iframe>')
    frame = page.frames[1]
    frame.wait_for_selector(".lrow")
    frame.wait_for_selector('[data-focus="refresh"]:not([disabled])')
    return frame


def _ladder_served(views, key, mutate=None):
    """The answers of the four families, with the view `key` standing for its own family (and `mutate` run on a copy of it)."""
    served, _ = _family_views(views)
    view = json.loads(json.dumps(views[key]))
    if mutate:
        mutate(view)
    hid = view["groups"][0]["harness_id"]
    view["groups"] = [view["groups"][0]] + [g for g in served[hid]["groups"] if g["harness_id"] != hid]   # (the head shows the four families whichever answers)
    served = dict(served)
    served[hid] = view
    return served, hid


def _spread_resets(view, every=7200, start=3600):
    """Every account of every window resets at its own time, `every` seconds after the one before: one step each (more than the ladder draws). Two hours apart, so that no two
    neighbours say the same in how long (1h, 3h .. 19h): neighbours that do are glued into one step (_glued_resets)."""
    now = time.time()
    for group in view["reserve"]["summary"]["groups"]:
        for i, bar in enumerate(group.get("bars") or []):
            bar["resets_at"] = qs.iso(now + start + every * i)


def _clustered_resets(view):
    """claude_many's ten accounts in clusters of three, 50 s apart inside a cluster (one step each), the clusters two hours apart (30m, 2h30, 4h30, 6h30): four steps that say four things."""
    now = time.time()
    for group in view["reserve"]["summary"]["groups"]:
        for i, bar in enumerate(group.get("bars") or []):
            bar["resets_at"] = qs.iso(now + 1830 + (i // 3) * 7200 + (i % 3) * 50)


def _glued_resets(view):
    """claude_many's ten accounts in four clusters of three, three, three and one: the first three clusters 10 and 15 minutes apart, all three saying '2h' (glued into one step of nine
    accounts, +9.00), the last 4 hours away (its own step, +1.00)."""
    now = time.time()
    for group in view["reserve"]["summary"]["groups"]:
        for i, bar in enumerate(group.get("bars") or []):
            bar["resets_at"] = qs.iso(now + (7230, 7830, 8430, 14430)[i // 3] + (i % 3) * 50)


def _no_reset(view):
    for group in view["reserve"]["summary"]["groups"]:
        for bar in group.get("bars") or []:
            bar["resets_at"] = None


# How long to a reset as a step says it, in English and in Russian: "<1m", "40m", "5h", "2d", "2d 5h" / "<1м", "40м", "5ч", "2д", "2д 5ч" (no space inside a number and its unit).
IN_HOW_LONG = r"<1[mм]|\d+[mм]|\d+[hч]|\d+[dд](?: \d+[hч])?"
IN_HOW_LONG_DAYS = r"\d+[dд](?: \d+[hч])?"


# Where everything of the ladder stands, as the browser laid it out: the ring of the first row (the axis), the zone, the head, each step that shows
# (the bead and the lines of text: the rects of the text itself, a Range, so the width is the writing's and not the box's), the word that counts, the
# row of the next reset on the plate and the heights of the shell, the card and the rail.
LADDER_GEOM = """() => {
  const q = (s, r = document) => r.querySelector(s), qa = (s, r = document) => [...r.querySelectorAll(s)];
  const box = (n) => { if (!n) return null; const r = n.getBoundingClientRect(); return {l: r.left, r: r.right, t: r.top, b: r.bottom, w: r.width, h: r.height, cx: (r.left + r.right) / 2}; };
  // (the rects of one visual line are one: the amount is three pieces of writing, "+2", the point and "70")
  const lines = (n) => { if (!n) return []; const range = document.createRange(); range.selectNodeContents(n);
    const rs = [...range.getClientRects()].filter((r) => r.width > 0).sort((a, b) => a.top - b.top || a.left - b.left), out = [];
    rs.forEach((r) => { const last = out[out.length - 1];
      if (last && Math.abs(r.top - last.t) <= 3) { last.l = Math.min(last.l, r.left); last.r = Math.max(last.r, r.right); } else out.push({l: r.left, r: r.right, t: r.top, b: r.bottom}); });
    return out.map((o) => ({l: o.l, r: o.r, t: o.t, b: o.b, w: o.r - o.l, cx: (o.l + o.r) / 2})); };
  // the crimson ink, the grey of a bead at rest and the primary ink as the page resolves them (a probe inside the rail, where the tokens stand)
  const ink = (name) => { const p = document.createElement('span'); p.style.color = 'var(' + name + ')'; q('.rail').appendChild(p); const c = getComputedStyle(p).color; p.remove(); return c; };
  const grey = (() => { const p = document.createElement('span'); p.style.backgroundColor = 'var(--ladder-bead-rest)'; q('.rail').appendChild(p); const c = getComputedStyle(p).backgroundColor; p.remove(); return c; })();
  const css = (n, prop) => n ? getComputedStyle(n)[prop] : null;
  const shown = (n) => !!n && n.getClientRects().length > 0 && getComputedStyle(n).visibility !== 'hidden';
  const ring = q('.rail .tab-mark-rail .ring'), zone = q('.rail-ladder-zone'), more = q('.rail-more');
  const nx = q('.meta-line.nx');
  return {width: innerWidth, scroll: document.documentElement.scrollWidth, axis: box(ring).cx, zone: box(zone), zoneShown: shown(zone),
    ink: {crimson: ink('--tone-crimson-ink'), primary: ink('--text-primary'), meta: ink('--text-meta'), grey: grey},
    title: shown(q('.rail-ladder-title')) ? {box: box(q('.rail-ladder-title')), lines: lines(q('.rail-ladder-title'))} : null,
    steps: qa('.rail-step').filter(shown).map((n) => ({cls: n.className, box: box(n), label: n.getAttribute('aria-label'), tab: n.getAttribute('tabindex'), accounts: n.getAttribute('data-accounts'),
      bead: box(q('.rail-bead', n)), time: lines(q('.rail-step-time', n)), amount: lines(q('.rail-step-amount', n)), none: lines(q('.rail-step-none', n)),
      timeText: (q('.rail-step-time', n) || {}).textContent, amountText: (q('.rail-step-amount', n) || {}).textContent,
      noneText: (q('.rail-step-none', n) || {}).textContent,
      beadColor: css(q('.rail-bead', n), 'backgroundColor'), dots: qa('.rail-step-dot', n).map((d) => ({text: d.textContent, color: getComputedStyle(d).color, inAmount: !!d.closest('.rail-step-amount')})),
      timeFont: [css(q('.rail-step-time', n), 'fontSize'), css(q('.rail-step-time', n), 'fontWeight'), css(q('.rail-step-time', n), 'color')],
      amountFont: [css(q('.rail-step-amount', n), 'fontSize'), css(q('.rail-step-amount', n), 'color')]})),
    all: Math.min(4, qa('.rail-step').length),   // (the steps in sight at most; the rest of the window is in the DOM, off, until the word opens them)
    more: shown(more) ? {text: more.textContent, label: more.getAttribute('aria-label'), box: box(more), lines: lines(more)} : null,
    nx: nx ? {shown: shown(nx), text: nx.textContent, cls: nx.className} : null,
    bot: box(q('.rail-bot')), foot: box(q('.rail')), shell: box(q('.shell')).h, card: box(q('.shell-card')).h, rail: box(q('.rail')).h};
}"""


def _check_ladder(g, where, total, amounts=None, plate=True, glued=False):
    """What must hold of the ladder as laid out: on the axis of the rings, every line of writing inside the 64px field and inside the zone and no
    wider than 60px, the heights the tokens give, the fit (whole steps only, as many as the room holds, the word that counts what did not stand),
    and the row of the plate: the first step is the next reset of the plate, and the plate hides its row only while the ladder shows it."""
    tol = 0.5
    assert g["scroll"] <= g["width"] + 1, ("no sideways scroll", where)
    if not g["zoneShown"] or g["title"] is None:
        assert g["steps"] == [] and g["more"] is None, ("a ladder that is not drawn shows nothing of itself", where, g)
        assert g["nx"] is None or g["nx"]["shown"], ("and the plate keeps its row of the next reset", where, g["nx"])
        return
    axis, zone = g["axis"], g["zone"]
    assert abs(g["title"]["box"]["cx"] - axis) <= tol and abs(g["title"]["box"]["h"] - 36) <= 0.5, ("the head stands on the axis, 36px high", where, g["title"])
    assert [abs(line["cx"] - axis) <= tol for line in g["title"]["lines"]] and all(abs(line["cx"] - axis) <= tol for line in g["title"]["lines"]), ("the word of the head", where)
    assert g["steps"] or g["more"], ("a head alone is not drawn", where)
    for k, step in enumerate(g["steps"]):
        assert step["noneText"] or (step["tab"] == "0" and step["label"]), ("a step is a tab stop that says itself whole", where, step)
        assert abs(step["box"]["h"] - 52) <= 0.5 and abs(step["box"]["cx"] - axis) <= tol, ("52px high, on the axis", where, step["box"])
        assert step["noneText"] or (step["bead"] and abs(step["bead"]["cx"] - axis) <= tol and abs(step["bead"]["w"] - 7) <= 0.1 and abs(step["bead"]["h"] - 7) <= 0.1), ("the bead is 7px on the axis", where, step["bead"])
        assert step["box"]["b"] <= zone["b"] + tol and step["box"]["t"] >= zone["t"] - tol, ("a step stands whole inside the zone, never cut", where, step["box"], zone)
        for name in ("time", "amount", "none"):
            for line in step[name]:
                # rule: the ladder is as wide as the tab picked (2 x its axis from the rail's edge), not 64px: a line of it is that wide at most
                assert line["w"] <= 2 * (axis - zone["l"]) + tol, ("a line is the width of the ladder at most", name, line["w"], step[name + "Text"], where)
                assert line["l"] >= zone["l"] - tol and line["r"] <= zone["r"] + tol and abs(line["cx"] - axis) <= tol, ("inside the zone, on the axis", name, line, where)
        if not step["none"]:
            assert len(step["time"]) == 1 and len(step["amount"]) == 1, ("one line of how long, one of the amount", where, step)
            # two lines under the bead, in this order: how long to the reset, the amount (no day, no date, no hour: the moment is in the title)
            assert step["bead"]["b"] <= step["time"][0]["t"] + tol and step["time"][0]["b"] <= step["amount"][0]["t"] + tol, \
                ("the bead, how long, the amount: top to bottom", where, step)
            assert re.fullmatch(IN_HOW_LONG, step["timeText"]), ("how long to the reset is there for every step", where, step["timeText"])
            assert re.fullmatch(r"\+\d+\.\d\d", step["amountText"]), ("the text of the amount stays whole", where, step["amountText"])
            # sizes and inks: how long 12px / 600 in the primary ink, the amount 11px in the meta ink, the point of the amount and the first bead crimson
            ink = g["ink"]
            assert step["timeFont"] == ["12px", "600", ink["primary"]], ("how long: 12px, 600, the primary ink", where, step["timeFont"], ink)
            assert step["amountFont"][0] == "11px" and step["amountFont"][1] == ink["meta"], ("the amount: 11px, the meta ink", where, step)
            assert len(step["dots"]) == 1 and step["dots"][0]["text"] == "." and step["dots"][0]["inAmount"] and step["dots"][0]["color"] == ink["crimson"], ("the point of the amount is crimson at rest", where, step["dots"], ink["crimson"])
            assert (step["beadColor"] == ink["crimson"]) == (k == 0) and (k == 0 or step["beadColor"] == ink["grey"]), ("the first bead is crimson at rest, the others grey", where, k, step["beadColor"], ink)
        if amounts and k < len(amounts):
            assert step["amountText"] == amounts[k], ("what a step gives back", where, step["amountText"], amounts)
    if glued:
        # the first three clusters say '2h' and are one step: the earliest moment to the latest ("Thu 8 Oct 22:59 – Thu 8 Oct 23:09"), the accounts of all nine; the last is its own, one moment
        first, second = g["steps"][0], g["steps"][1]
        assert len(g["steps"]) == 2 and g["more"] is None, ("three clusters glued: two steps, nothing over them", where, len(g["steps"]))
        assert re.fullmatch(r"2[hч]", first["timeText"]) and re.fullmatch(r"4[hч]", second["timeText"]), ("2h and 4h", where, first["timeText"], second["timeText"])
        assert len(json.loads(first["accounts"])) == 9 and len(json.loads(second["accounts"])) == 1, ("nine accounts in the glued step", where, first["accounts"])
        assert re.match(r"^\S+ \d{1,2} \S+ \d\d:\d\d – \S+ \d{1,2} \S+ \d\d:\d\d — 9 ", first["label"]), ("the first moment to the last", where, first["label"])
        assert " – " not in second["label"], ("one moment", where, second["label"])
    if g["steps"] and g["steps"][0]["noneText"]:
        assert len(g["steps"]) == 1 and g["more"] is None, ("a window with no reset to come: one line, 'not reported'", where)
    elif g["steps"] and plate:
        # the first step is the row of the next reset on the plate: the same moment (the first of a glued step's two), the amount and the count of accounts the plate says or more
        # (neighbouring steps that say the same in how long are glued into one, so the first step may be wider than the plate's row)
        label = g["steps"][0]["label"]
        when = label.split(" — ")[0].split(" – ")[0]
        nx = g["nx"]["text"] if g["nx"] else ""
        assert when in nx, ("the first step is the plate's next reset: the same moment", where, label, nx)
        plate_back = re.search(r"\+(\d+\.\d\d)", nx)
        if plate_back:
            assert float(g["steps"][0]["amountText"][1:]) >= float(plate_back.group(1)) - 0.005, ("and gives back the plate's amount or more", where, label, nx)
        count = re.search(r"— (\d+) \S+", label)
        plate_count = re.search(r"(\d+) (?:accounts|аккаунт)", nx)
        if plate_count:
            assert count and int(count.group(1)) >= int(plate_count.group(1)), ("with the plate's count of accounts or more", where, label, nx)
    if g["more"]:
        assert g["more"]["box"]["h"] >= 30 - tol and g["more"]["box"]["b"] <= zone["b"] + tol, ("the word that counts stands whole in the zone", where, g["more"], zone)
        assert not g["steps"] or g["more"]["box"]["t"] >= g["steps"][-1]["box"]["b"] - tol, ("the word that counts stands after the last step", where, g["more"]["box"], g["steps"][-1]["box"])
        assert all(line["w"] <= 60 and line["l"] >= zone["l"] - tol and abs(line["cx"] - axis) <= tol for line in g["more"]["lines"]), ("a line of the word", where, g["more"])
        n = int(re.search(r"(\d+)", g["more"]["text"]).group(1))
        assert n == total - len(g["steps"]), ("'N more' counts the resets that did not stand", where, g["more"]["text"], total, len(g["steps"]))
    elif g["steps"] and not g["steps"][0]["noneText"]:
        assert len(g["steps"]) == min(total, 4) or len(g["steps"]) < g["all"], ("every step stands, or the room cut some", where)
    # the fit: as many whole steps as the room holds - one more would not have stood (with the word, if some are left out)
    room, head = zone["h"], 36
    left = total - len(g["steps"])
    if g["steps"] and not g["steps"][0]["noneText"]:
        if left > 0:
            assert g["more"] is not None or room - head - len(g["steps"]) * 52 < 30, ("steps are left out, so the word that counts them stands if it fits", where, g["more"], room)
        if len(g["steps"]) < min(total, 4):
            assert room - head - (len(g["steps"]) + 1) * 52 - 30 < 0, ("one step more would have stood: the ladder gave room away", where, room, len(g["steps"]))
    # the plate: its row gives way on the wide shell while the ladder shows the next reset, and keeps its place when no step stands
    if g["nx"]:
        assert g["nx"]["shown"] == (not g["steps"]), ("the row of the plate hides while the ladder shows a step", where, g["nx"], len(g["steps"]))
    assert abs(g["shell"] - g["card"]) <= 1 and abs(g["rail"] - g["card"]) <= 1, ("the capsule is not stretched: shell, card and rail are as high", where, g["shell"], g["card"], g["rail"])
    # (the rail has no foot: the zone ends the rail, and the card's height is the rail's)
    assert g["bot"] is None and zone["b"] <= g["foot"]["b"] + tol, ("no foot in the rail: the zone is inside the rail", where, g["bot"], zone)


def _ladder_states(frame, page):
    """The states of the card the room of the ladder depends on: the chart open (the start), folded, and folded with the accounts unfolded."""
    yield "chart open"
    chart = frame.locator('.l-chart[aria-pressed="true"]')
    if chart.count():
        chart.first.click()
        page.wait_for_timeout(450)
        yield "chart folded"
    accounts = frame.locator('[data-focus="accounts"]')
    if accounts.count():
        accounts.first.click()
        page.wait_for_timeout(450)
        yield "chart folded, accounts open"


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_from_860px_the_ladder_of_resets_stands_on_the_axis_of_the_rings_in_lines_of_60px_and_never_stretches_the_capsule(fixture_file, engine):
    """kit: rail, the ladder. From 860px (860, 1020, 1280), for Codex, Claude and Antigravity of the fixture and for windows of several resets: the beads,
    the head and every line of the ladder stand on the axis of the rings (0.5px); no line is wider than 60px or outside the zone (English; the Russian
    'вс', '11:19', '30 нояб', 'не сообщены' and 'ещё N' too); the head is 36px and a step 66px (the bead, the time 12/600, the day and the amount 11, one line each; the first bead and the point of every amount are the crimson ink at rest, the other beads grey); the zone is cut at its edge and adds nothing to the rail: the
    shell, the card and the rail are as high (1px) with the chart open, folded and with the accounts unfolded, and as high as with the ladder taken away;
    the steps that stand are whole, as many as the room holds, and the word 'N more' counts the others; the first step is the next reset of the plate
    (the same moment, amount and count of accounts) and the plate's own row is hidden then (on the narrow shell it stays: the test after this one)."""
    from test_bars import _let_go_the_pick
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    cases = [("claude", None, 1), ("codex", None, 1), ("agy", None, 1), ("claude_five", None, 4), ("codex_states", None, 2), ("claude_many", _clustered_resets, 4),
             ("claude_many", _glued_resets, 2), ("claude_many", _spread_resets, 10), ("claude_five", _no_reset, 0)]
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            seen = {"claude_many/spread": False}
            for width in (860, 1020, 1280):
                for key, mutate, total in cases:
                    served, family = _ladder_served(views, key, mutate)
                    page = browser.new_page(viewport={"width": 1400, "height": 900})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    frame = _mount_ladder_frame(page, served, family, width)
                    page.wait_for_timeout(1500)
                    # The plate's row, the chart word and the heights are the plate's own: the pick of the top account is let go first (picked, they stand in its folded card).
                    _let_go_the_pick(frame)
                    for state in _ladder_states(frame, page):
                        where = (engine, width, key, mutate.__name__ if mutate else "", state)
                        g = frame.evaluate(LADDER_GEOM)
                        amounts = (LADDER_AMOUNTS_GLUED if mutate is _glued_resets else LADDER_AMOUNTS.get(key) if mutate is _clustered_resets or not mutate else None) if state == "chart open" else None
                        _check_ladder(g, where, total, amounts, plate=not mutate, glued=mutate is _glued_resets)
                        if mutate is _spread_resets and g["more"]:
                            seen["claude_many/spread"] = True
                        # the zone adds nothing: the same card with the ladder taken away is as high
                        frame.add_style_tag(content=".rail-ladder-zone{display:none !important}")
                        bare = frame.evaluate(LADDER_GEOM)
                        assert abs(bare["shell"] - g["shell"]) <= 1 and abs(bare["card"] - g["card"]) <= 1, ("the ladder adds no height", where, g["shell"], bare["shell"])
                        frame.evaluate("() => { const s = document.head.querySelector('style:last-of-type'); if (s && s.textContent.indexOf('display:none !important') >= 0) s.remove(); }")
                        page.wait_for_timeout(50)
                    assert errors == [], errors
                    page.close()
            assert seen["claude_many/spread"], "ten resets: the word 'N more' stood at least once (with the chart open)"
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_ladder_speaks_russian_in_lines_of_60px_in_how_long_not_in_dates_and_not_reported_wraps_inside_its_field(fixture_file, engine):
    """kit: rail, the ladder, in Russian and in English: '5ч' ('5h'), '2д 5ч' ('2d 5h'), '40м' ('40m'), '<1м' ('<1m'): how long to the reset, for every step (a day and an hour of it, or a date, are no more
    on the step), the full moment in the title of the step; a window with no reset to come says 'не сообщены' ('not reported') in lines no wider than the field (60px before the rail grew with the tab), wrapped inside the field, one
    step high; the word 'ещё N' ('N more') fits 60px. Every line stands on the axis and inside the zone."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())

    def far(view):
        _spread_resets(view, every=86400, start=9 * 86400)

    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for lang, soon_re, far_re, none_text in (("en", IN_HOW_LONG, IN_HOW_LONG_DAYS, "not reported"),
                                                    ("ru", IN_HOW_LONG, IN_HOW_LONG_DAYS, "не сообщены")):
                for key, mutate, kind in (("claude_five", None, "soon"), ("claude_many", far, "far"), ("claude_five", _no_reset, "none")):
                    served, family = _ladder_served(views, key, mutate)
                    page = browser.new_page(viewport={"width": 1400, "height": 900})
                    frame = _mount_ladder_frame(page, served, family, 1280, lang)
                    page.wait_for_timeout(1500)
                    g = frame.evaluate(LADDER_GEOM)
                    where = (engine, lang, key, kind, g["steps"])
                    assert g["steps"], where
                    if kind == "none":
                        step = g["steps"][0]
                        assert step["noneText"] == none_text and len(g["steps"]) == 1, where
                        # rule: the ladder is as wide as the tab picked (the field is 2 x the axis from the rail's edge, 92px for Claude), not 64px:
                        # a line of the phrase is no wider than the field and stays inside the step (the wrap inside the field is still held: every line within the box of the step)
                        field = 2 * (g["axis"] - g["zone"]["l"])
                        assert step["none"] and all(line["w"] <= field + 0.5 for line in step["none"]), ("the lines of the phrase, the field at most", field, where)
                        assert all(line["l"] >= step["box"]["l"] - 0.5 and line["r"] <= step["box"]["r"] + 0.5 for line in step["none"]), ("and inside the step", where)
                        assert abs(step["box"]["h"] - 52) <= 0.5, ("one step high", where)
                        assert all(abs(line["cx"] - g["axis"]) <= 0.5 and line["l"] >= g["zone"]["l"] - 0.5 for line in step["none"]), where
                        assert frame.evaluate("() => document.querySelector('.rail-step').title") == frame.evaluate("() => document.querySelector('.rail-step').title"), where
                        continue
                    for step in g["steps"]:
                        text = step["timeText"]
                        assert re.fullmatch(soon_re if kind == "soon" else far_re, text), (text, where)
                        assert (re.fullmatch(r"[^\d\s]*\d+[mhd]( \d+h)?", text) if lang == "en" else re.fullmatch(r"[^\d\s]*\d+[мчд]( \d+ч)?", text)), ("the unit of the language, no space inside the number and its unit", text, where)
                        assert not re.search(r"\d\d:\d\d|нояб|Nov|вс\b|Sun", step["timeText"] + step["amountText"]), ("no hour, no day, no date on the step", where)
                        assert re.fullmatch(r"\+\d+\.\d\d", step["amountText"]) and len(step["dots"]) == 1, ("the text of the amount is whole, its point a span", step, where)
                        assert all(line["w"] <= 60 for line in step["time"] + step["amount"]), ("60px at most", text, where)
                        assert re.search(r"\d{1,2}", step["label"]), where
                    if kind == "far":
                        assert g["more"] is None or all(line["w"] <= 60 for line in g["more"]["lines"]), where
                    page.close()
            # the word that counts, in both languages
            for lang, expect in (("en", r"^\d+ more$"), ("ru", r"^ещё \d+$")):
                served, family = _ladder_served(views, "claude_many", _spread_resets)
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                frame = _mount_ladder_frame(page, served, family, 1280, lang)
                page.wait_for_timeout(1500)
                g = frame.evaluate(LADDER_GEOM)
                assert g["more"] and re.match(expect, g["more"]["text"]), (engine, lang, g["more"])
                assert all(line["w"] <= 60 for line in g["more"]["lines"]), (engine, lang, g["more"])
                assert g["more"]["label"], ("the word has its spoken form", g["more"])
                page.close()
        finally:
            browser.close()


# How long to a reset, at the edges of the units (not the date and the day but how long: hours alone if hours, days and hours if days). Each row: the seconds to the
# reset (30 s are added in the data: the few seconds of the page's start must not decide a minute), then what the English and the Russian step say. Two views, for the resets within
# two minutes of each other are one step (59m30s and 1h30s are a minute apart).
LADDER_EDGES_A = [(0, "<1m", "<1м"), (40 * 60, "40m", "40м"), (59 * 60, "59m", "59м"), (5 * 3600, "5h", "5ч"), (23 * 3600 + 59 * 60, "23h", "23ч"),
                  (2 * 86400, "2d", "2д"), (2 * 86400 + 5 * 3600, "2d 5h", "2д 5ч")]
LADDER_EDGES_B = [(3600, "1h", "1ч"), (86400, "1d", "1д"), (47 * 3600 + 59 * 60, "1d 23h", "1д 23ч"), (3 * 86400 + 3600, "3d 1h", "3д 1ч")]


def _edge_resets(edges):
    def mutate(view):
        now = time.time()
        for group in view["reserve"]["summary"]["groups"]:
            for i, bar in enumerate(group.get("bars") or []):
                bar["resets_at"] = qs.iso(now + edges[i][0] + 30) if i < len(edges) else None
    return mutate


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_step_of_the_ladder_says_in_how_long_at_the_edges_of_the_units_in_english_and_russian(fixture_file, engine):
    """The step says how long, from the whole remainder rounded down: under a minute '<1m' ('<1м'), under an hour the minutes ('59m'), under a day the hours alone ('23h' at 23h59m,
    '1h' at 60 minutes), a day or more the days and the hours after them ('1d 23h', '2d 5h') and the days alone when no hour is left ('1d', '2d'); written as the windows are
    (5h, 7d; in Russian 5ч, 7д; no space inside a number and its unit), 60px at most, and the steps stand in the order of the time."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for lang, col in (("en", 1), ("ru", 2)):
                for edges in (LADDER_EDGES_A, LADDER_EDGES_B):
                    served, family = _ladder_served(views, "claude_many", _edge_resets(edges))
                    page = browser.new_page(viewport={"width": 1400, "height": 900})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    frame = _mount_ladder_frame(page, served, family, 1280, lang)
                    page.wait_for_timeout(600)
                    got = frame.evaluate("() => [...document.querySelectorAll('.rail-step-time')].map((n) => n.textContent)")
                    assert got == [row[col] for row in edges], (engine, lang, got, [row[col] for row in edges])
                    shown = frame.evaluate(LADDER_GEOM)
                    assert shown["steps"] and all(line["w"] <= 60 for step in shown["steps"] for line in step["time"]), (engine, lang, "60px at most", shown["steps"])
                    assert errors == [], errors
                    page.close()
        finally:
            browser.close()


WEEKDAY_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_same_reset_says_the_same_in_how_long_in_tokyo_and_new_york_and_its_title_has_the_local_moment_of_each(fixture_file, engine):
    """One moment of reset, two readers in two zones (Asia/Tokyo and America/New_York, the browser's own, 13 or 14 hours apart): 'in how long' is the same on the step and in the
    bubble at the limit ('5h', 'back in 5h'), because a remainder has no zone; the title and the spoken label of the step (and the title of the bubble) say the moment whole in the local time
    of each reader, 'Thu 30 Oct 22:59'. The data say the moment in UTC with a Z, as the engine does."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    moment = int(time.time()) + 5 * 3600 + 20 * 60 + 30

    def same_moment(view):
        for group in view["reserve"]["summary"]["groups"]:
            for bar in group.get("bars") or []:
                bar["resets_at"] = qs.iso(moment)

    def local(zone):
        at = datetime.fromtimestamp(moment, ZoneInfo(zone))
        return "%s %d %s %02d:%02d" % (WEEKDAY_EN[at.weekday()], at.day, MONTH_EN[at.month - 1], at.hour, at.minute)

    assert local("Asia/Tokyo") != local("America/New_York")
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            said = {}
            for zone in ("Asia/Tokyo", "America/New_York"):
                served, family = _ladder_served(views, "claude_five", same_moment)
                context = browser.new_context(viewport={"width": 1400, "height": 900}, timezone_id=zone)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                frame = _mount_ladder_frame(page, served, family, 1280)
                page.wait_for_timeout(600)
                assert frame.evaluate("() => new Date(2026, 0, 1).getTimezoneOffset()") == -ZoneInfo(zone).utcoffset(datetime(2026, 1, 1)).total_seconds() / 60, ("the browser lives in the zone", zone)
                step = frame.evaluate("() => { const n = document.querySelector('.rail-step'); return {time: n.querySelector('.rail-step-time').textContent, title: n.title, label: n.getAttribute('aria-label')}; }")
                bubble = frame.evaluate("() => { const n = document.querySelector('.wt-limit'); return n ? {text: n.textContent, title: n.title, label: n.getAttribute('aria-label')} : null; }")
                where = (engine, zone, step, bubble)
                assert step["time"] == "5h", where
                assert step["title"] == step["label"] and step["title"].startswith(local(zone) + " \u2014 "), ("the moment, local to the reader, whole", local(zone), where)
                assert bubble and bubble["text"].endswith("back in 5h") and bubble["label"].endswith("back in 5h"), where
                assert local(zone) in bubble["title"], ("and the bubble's title has the local moment", local(zone), where)
                said[zone] = (step["time"], bubble["text"])
                assert errors == [], errors
                context.close()
            assert said["Asia/Tokyo"] == said["America/New_York"], ("in how long is the same in every zone", said)
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [859, 600, 360])
def test_real_browser_on_the_narrow_shell_there_is_no_ladder_and_the_plate_keeps_its_row_of_the_next_reset(fixture_file, engine, width):
    """kit: rail, the ladder. Under 860px the zone is display:none (nothing of it is laid out, no step is a tab stop that shows) and the row of the next
    reset on the plate is there to read; from 860px it is the other way round."""
    from test_bars import _let_go_the_pick
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for key in ("claude", "codex", "agy", "claude_five"):
                served, family = _ladder_served(views, key)
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                frame = _mount_ladder_frame(page, served, family, width)
                page.wait_for_timeout(1300)
                # The row of the next reset is read on the plate: the pick of the top account is let go first (picked, the row stands in its folded card and is hidden).
                _let_go_the_pick(frame)
                g = frame.evaluate(LADDER_GEOM)
                where = (engine, width, key, g["nx"], g["steps"])
                assert not g["zoneShown"] and g["steps"] == [] and g["more"] is None, ("no ladder on the narrow shell", where)
                assert g["nx"] and g["nx"]["shown"] and "next reset" in g["nx"]["text"], ("the plate's row stays", where)
                assert g["scroll"] <= g["width"] + 1, where
                focusable = frame.evaluate("() => [...document.querySelectorAll('.rail-step, .rail-more')].filter((n) => n.getClientRects().length).length")
                assert focusable == 0, ("nothing of the ladder can take the keyboard", where)
                page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_a_step_under_the_pointer_or_the_keyboard_lights_the_bars_of_its_accounts_and_its_bead_grows(fixture_file, engine):
    """kit: rail, the ladder. The pointer on a step (and the keyboard on it, the ring showing) puts the plate to data-dim=group and keeps the bars of the
    step's accounts bright, the others dimmed; its bead grows to x1.4 and takes the crimson ink; the first bead (at rest) is the primary ink and the
    others a grey; away from it nothing is left lit; pressed (:active) the bead is --press of the grown size."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    seen_by = "() => [...document.querySelectorAll('.bars .bar')].map((b) => [b.getAttribute('data-account'), getComputedStyle(b).opacity])"
    bead = "(i) => { const b = document.querySelectorAll('.rail-step .rail-bead')[i]; const m = getComputedStyle(b).transform; const p = m.match(/matrix\\(([^)]*)\\)/);" \
           " return {scale: p ? parseFloat(p[1].split(',')[0]) : 1, bg: getComputedStyle(b).backgroundColor}; }"
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            served, family = _ladder_served(views, "codex_states")
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            frame = _mount_ladder_frame(page, served, family, 1280)
            page.wait_for_timeout(1500)
            steps = frame.locator(".rail-step")
            assert steps.count() == 2
            first_accounts = json.loads(steps.nth(0).get_attribute("data-accounts"))
            second_accounts = json.loads(steps.nth(1).get_attribute("data-accounts"))
            assert len(first_accounts) == 3 and len(second_accounts) == 3, (first_accounts, second_accounts)  # (the second: gptpro4, gptpro5 and the one switched off, gptpro9)
            rest = [frame.evaluate(bead, i) for i in (0, 1)]
            assert all(abs(r["scale"] - 1) <= 0.01 for r in rest) and rest[0]["bg"] != rest[1]["bg"], ("the first bead is the crimson ink, the other a grey", rest)
            steps.nth(0).hover()
            page.wait_for_timeout(500)
            assert frame.evaluate(DIMS) == ["group"], frame.evaluate(DIMS)
            bright = sorted(a for a, o in frame.evaluate(seen_by) if o == "1")
            assert bright == sorted(first_accounts), ("the bars of the step's own accounts stay bright", bright, first_accounts)
            hot = frame.evaluate(bead, 0)
            assert abs(hot["scale"] - 1.4) <= 0.02 and hot["bg"] == rest[0]["bg"], ("the first bead grows to x1.4 and keeps the crimson ink it has at rest", hot, rest[0])
            page.mouse.down()
            page.wait_for_timeout(300)
            assert abs(frame.evaluate(bead, 0)["scale"] - 1.4 * 0.94) <= 0.03, ("pressed in to --press of the grown size", frame.evaluate(bead, 0))
            page.mouse.up()
            steps.nth(1).hover()
            page.wait_for_timeout(500)
            assert sorted(a for a, o in frame.evaluate(seen_by) if o == "1") == sorted(second_accounts)
            hot = frame.evaluate(bead, 1)
            assert abs(hot["scale"] - 1.4) <= 0.02 and hot["bg"] == rest[0]["bg"], ("a grey bead grows and takes the crimson ink of the first", hot, rest)
            page.mouse.move(2, 2)
            page.wait_for_timeout(500)
            assert frame.evaluate(DIMS) == [] and frame.evaluate("() => document.querySelectorAll('[data-lit]').length") == 0
            assert abs(frame.evaluate(bead, 1)["scale"] - 1) <= 0.01
            # the keyboard: a step is a tab stop; the ring showing, it does what the pointer does; away from it nothing is left lit
            frame.focus(".rail-step >> nth=1")
            page.keyboard.press("Shift+Tab")
            page.keyboard.press("Tab")
            page.wait_for_timeout(500)
            assert frame.evaluate("() => document.activeElement.classList.contains('rail-step') && document.activeElement.matches(':focus-visible')")
            assert frame.evaluate(DIMS) == ["group"] and sorted(a for a, o in frame.evaluate(seen_by) if o == "1") == sorted(second_accounts)
            assert abs(frame.evaluate(bead, 1)["scale"] - 1.4) <= 0.02
            frame.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")
            page.wait_for_timeout(500)
            assert frame.evaluate(DIMS) == [] and frame.evaluate("() => document.querySelectorAll('[data-lit]').length") == 0
            page.close()
        finally:
            browser.close()


RIGHT_PART = """() => { const q = (s) => document.querySelector(s); const r = (e) => { const b = e.getBoundingClientRect(); return [b.left, b.top, b.width, b.height].map((v) => Math.round(v * 10) / 10); };
  const card = q('.shell-card'), shell = q('.shell');
  // (the age in the stamp ticks by itself: the markup is compared without the stamp's words; so are the other clock words in the card, "back in 59m" and
  // "read 3 min ago", which cross a minute or an hour while the test runs and are cut to "back in " and "read ago", in the text and in the labels alike)
  const bare = (n) => { const c = n.cloneNode(true); c.querySelectorAll('.wt-stamp').forEach((x) => { x.textContent = ''; x.removeAttribute('title'); });
    return c.outerHTML.replace(/back in [0-9][0-9a-z .]*?(?=[<",]|$)/g, 'back in ').replace(/read (?:a moment|[0-9.]+ ?(?:min|h|d)) ago/g, 'read ago'); };
  return {card: bare(card).length, cardBox: r(card), shell: r(shell), plate: q('.wt-plate') && r(q('.wt-plate')), plateHtml: q('.wt-plate') && bare(q('.wt-plate')),
    pressed: [...document.querySelectorAll('.l-chart')].map((b) => b.getAttribute('aria-pressed')), details: [...document.querySelectorAll('details')].map((d) => d.open),
    schedule: !!document.querySelector('table.schedule'), scrollY: window.scrollY}; }"""
LADDER_OPEN = """() => { const q = (s) => document.querySelector(s); const more = q('.rail-more'), sc = q('.rail-scroll'), one = q('.rail-step:not(.rail-fit-off)').offsetHeight;
  const steps = [...document.querySelectorAll('.rail-ladder:not(.rail-ladder-out) .rail-step')];
  return {text: more.textContent, expanded: more.getAttribute('aria-expanded'), label: more.getAttribute('aria-label'), top: sc.scrollTop, client: sc.clientHeight, scroll: sc.scrollHeight,
    one, standing: steps.filter((n) => getComputedStyle(n).display !== 'none').length, all: steps.length, bar: getComputedStyle(sc).scrollbarWidth, overscroll: getComputedStyle(sc).overscrollBehaviorY,
    start: sc.classList.contains('rail-scroll-start'), end: sc.classList.contains('rail-scroll-end'), on: sc.classList.contains('rail-scroll-on'), inner: sc.offsetWidth - sc.clientWidth,
    zone: q('.rail-ladder-zone').clientHeight, ladder: q('.rail-ladder').offsetHeight}; }"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_word_that_counts_opens_the_ladder_below_in_the_rail_and_does_not_touch_the_right_part(fixture_file, engine):
    """kit: rail, the ladder. The word 'N more' opens the ladder downward in the rail itself, into a window that scrolls (5.5 steps at most, no scroll bar, the haze
    of its edges following the scroll); it is a button with aria-expanded, and it reads 'less' while open. It does not touch the right part: the chart, the Details (open or
    folded as they were), the schedule, the plate and the card are not changed (the same markup, the same boxes), and the capsule is not stretched. The press again folds it
    (four steps and 'N more', the scroll at the start). The state outlives the redraw."""
    from test_bars import _let_go_the_pick
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            served, family = _ladder_served(views, "claude_many", _spread_resets)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_ladder_frame(page, served, family, 1280)
            page.wait_for_timeout(1500)
            # The chart word is pressed on the plate: the pick of the top account is let go first (picked, the word stands in its folded card and cannot be pressed).
            _let_go_the_pick(frame)
            for chart_open in (True, False):
                if not chart_open:
                    frame.locator('.l-chart[aria-pressed="true"]').first.click()
                    page.wait_for_timeout(500)
                g = frame.evaluate(LADDER_GEOM)
                assert g["more"] and re.match(r"^\d+ more$", g["more"]["text"]), ("the word that counts stands", chart_open, g["more"])
                before = frame.evaluate(RIGHT_PART)
                assert before["pressed"] == (["true"] if chart_open else ["false"]), before
                closed = frame.evaluate(LADDER_OPEN)
                assert closed["expanded"] == "false" and 1 <= closed["standing"] <= 4 and closed["all"] > closed["standing"] and not closed["on"], ("folded: up to four steps stand (the room says how many), the rest are in the DOM, off", closed)
                frame.locator(".rail-more").click()
                page.mouse.move(1390, 890)   # (the pointer leaves: a step that comes under it would light the bars of its accounts, as it should, and that is the plate's dimming, not a change)
                page.wait_for_timeout(1200)
                opened = frame.evaluate(LADDER_OPEN)
                after = frame.evaluate(RIGHT_PART)
                where = (chart_open, opened, before, after)
                # the right part is not changed by a pixel: the same markup of the card, the same boxes, the chart and the Details as they were, no schedule opened
                assert after == before, ("the right part is untouched", where)
                assert opened["expanded"] == "true" and opened["text"] == "less" and opened["label"], where
                assert opened["on"] and opened["standing"] == opened["all"] and opened["all"] > 4, ("the window holds every step of the window", where)
                # the window: at most 5.5 steps, the half of the next one under the haze says that there is more; no scroll bar; the page is not dragged
                assert opened["client"] <= 5.5 * opened["one"] + 1 and opened["scroll"] > opened["client"] + 1, ("a window that scrolls, 5.5 steps at most", where)
                assert opened["bar"] == "none" and opened["overscroll"] == "contain" and opened["inner"] == 0, ("no scroll bar, the page is not dragged", where)
                assert opened["start"] and not opened["end"], ("the haze: none over the start, one under the end", where)
                assert opened["ladder"] <= opened["zone"] + 1, ("the ladder never stretches the card", where)
                # the scroll: the edges follow it, the place outlives a redraw
                frame.evaluate("() => { document.querySelector('.rail-scroll').scrollTop = 40; }")
                page.wait_for_timeout(200)
                mid = frame.evaluate(LADDER_OPEN)
                assert mid["top"] > 0 and not mid["start"], ("scrolled: the haze over the start comes", mid)
                frame.evaluate("() => { document.querySelector('.rail-scroll').scrollTop = 1e6; }")
                page.wait_for_timeout(200)
                end = frame.evaluate(LADDER_OPEN)
                assert end["end"] and not end["start"], ("at the end: no haze under it", end)
                frame.evaluate(REDRAW)
                page.wait_for_timeout(500)
                kept = frame.evaluate(LADDER_OPEN)
                assert kept["expanded"] == "true" and abs(kept["top"] - end["top"]) <= 1, ("open, and the place of the scroll, across the redraw", kept, end)
                assert frame.evaluate(RIGHT_PART)["pressed"] == before["pressed"]
                # the word again folds it: four steps, 'N more', the scroll at the start; the right part still as it was
                frame.locator(".rail-more").click()
                page.mouse.move(1390, 890)
                page.wait_for_timeout(600)
                folded = frame.evaluate(LADDER_OPEN)
                assert folded["expanded"] == "false" and re.match(r"^\d+ more$", folded["text"]) and folded["standing"] == closed["standing"] and folded["top"] == 0 and not folded["on"], ("folded", folded)
                assert frame.evaluate(RIGHT_PART) == before
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


# The animations the ladder runs, as the recorder sees them frame by frame: name | the class of the node | the properties its keyframes touch.
LADDER_REC = """(ms) => {
  const rec = {seen: {}, start: performance.now(), done: false};
  window.__lrec = rec;
  const skip = ['offset', 'computedOffset', 'easing', 'composite'];
  const props = (a) => { try { return [...new Set(a.effect.getKeyframes().flatMap((k) => Object.keys(k)).filter((k) => !skip.includes(k)))].sort().join('+'); } catch (e) { return ''; } };
  const tick = () => {
    for (const a of document.getAnimations()) {
      const n = a.effect && a.effect.target, name = a.animationName || '';
      if (!n || !name) continue;
      const cls = String(n.className.baseVal === undefined ? n.className : n.className.baseVal).split(' ')
        .filter((c) => ['rail-step', 'rail-more', 'rail-ladder-title', 'rail-ladder-out', 'tab-mark-rail', 'rail-bot'].includes(c)).join('.');
      const key = name + '|' + cls;
      const timing = a.effect.getTiming();
      const at = rec.seen[key] || (rec.seen[key] = {n: 0, delays: [], props: props(a)});
      at.n += 1;
      if (!at.delays.includes(timing.delay)) at.delays.push(timing.delay);
    }
    if (performance.now() - rec.start < ms) requestAnimationFrame(tick); else rec.done = true;
  };
  requestAnimationFrame(tick);
}"""
LADDER_NAMES = "() => document.getAnimations().map((a) => (a.animationName || a.transitionProperty || '') + '|' + String(a.effect && a.effect.target && a.effect.target.className)).filter((s) => /ladder|rail-drop/.test(s))"


def _rec_ladder(frame, page, action, ms=1500):
    frame.evaluate(LADDER_REC, ms)
    action()
    frame.wait_for_function("() => window.__lrec.done", timeout=ms + 4000)
    return frame.evaluate("window.__lrec.seen")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_pick_of_a_window_brings_the_new_steps_down_and_fades_the_old_ones_and_a_poll_or_a_refresh_moves_nothing(fixture_file, engine):
    """kit: rail, motion. The reader's pick of a window (a click on its ring): the head and the steps of the new window come down by ladder-drop (transform and
    opacity alone), a row after another; the steps that stood fade by ladder-leave (opacity) and are inert and hidden from a reader; within a second no
    ladder animation is left and the steps stand at full ink. The 30 s poll (the visibility draw) and 'Refresh' bring no ladder animation; under reduced
    motion the pick brings the steps at once. (The pick of a family is the fake-DOM test's and the rail tests above; see the report.)"""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            served, family = _ladder_served(views, "claude")
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_ladder_frame(page, served, family, 1280)
            _settle_family(frame, family)
            assert frame.evaluate(LADDER_NAMES) == [], ("a quiet page runs no ladder animation", frame.evaluate(LADDER_NAMES))
            # the poll and the refresh: the numbers change in place
            seen = _rec_ladder(frame, page, lambda: frame.evaluate(REDRAW), 900)
            assert not [k for k in seen if k.startswith("ladder-") or k.split("|")[1] in ("rail-step", "rail-more", "rail-ladder-title")], ("the poll moves nothing", seen)
            seen = _rec_ladder(frame, page, lambda: frame.click('[data-focus="refresh"]'), 1200)
            assert not [k for k in seen if k.startswith("ladder-") or k.split("|")[1] in ("rail-step", "rail-more", "rail-ladder-title")], ("Refresh moves nothing", seen)
            # the pick of a window: the other ring of the rail
            before = frame.evaluate("() => [...document.querySelectorAll('.rail-step-time')].map((n) => n.textContent + '|' + n.parentNode.getAttribute('aria-label'))")
            other = frame.locator('[data-focus="win:claude:18000"]')   # the 5-hour: Claude opens on the weekly (the soonest reset is the weekly's)
            seen = _rec_ladder(frame, page, lambda: other.click(), 1500)
            drop = {k: v for k, v in seen.items() if k.startswith("ladder-drop|")}
            assert "ladder-drop|rail-step" in drop and "ladder-drop|rail-ladder-title" in drop, ("the new head and steps come down", seen)
            assert "ladder-leave|rail-ladder-out" in seen, ("the old steps fade", seen)
            assert all(v["props"] in ("opacity+transform", "opacity", "transform") for k, v in seen.items() if k.startswith("ladder-")), ("transform and opacity alone", seen)
            assert len(drop["ladder-drop|rail-step"]["delays"]) >= 2 or drop["ladder-drop|rail-step"]["n"] >= 1, seen
            title_delay, step_delay = min(drop["ladder-drop|rail-ladder-title"]["delays"]), min(drop["ladder-drop|rail-step"]["delays"])
            assert title_delay < step_delay or drop["ladder-drop|rail-step"]["n"] == 0, ("the head first, then the steps, a row after another", seen)
            # the word that counts (when it stands) comes down after the last step
            if "ladder-drop|rail-more" in drop:
                assert min(drop["ladder-drop|rail-more"]["delays"]) > max(drop["ladder-drop|rail-step"]["delays"]), ("the word that counts comes down last", seen)
            assert "rail-drop|rail-step" not in seen, ("the pick of a window does not use the family's drop", seen)
            frame.wait_for_timeout(300)
            assert frame.evaluate(LADDER_NAMES) == [], ("no ladder animation is left", frame.evaluate(LADDER_NAMES))
            assert frame.evaluate("() => document.querySelectorAll('.rail-ladder-out').length") <= 1
            ink = frame.evaluate("() => [...document.querySelectorAll('.rail-step, .rail-ladder-title')].filter((n) => !n.closest('.rail-ladder-out')).map((n) => +getComputedStyle(n).opacity)")
            assert ink and all(o == 1 for o in ink), ("the new steps stand at full ink", ink)
            after = frame.evaluate("() => [...document.querySelectorAll('.rail-step-time')].map((n) => n.textContent + '|' + n.parentNode.getAttribute('aria-label'))")
            assert after != before, ("the 5-hour has its own resets: the steps are another window's", before, after)
            page.close()
            # reduced motion: the steps stand at once
            page = browser.new_page(viewport={"width": 1400, "height": 900}, reduced_motion="reduce")
            frame = _mount_ladder_frame(page, served, family, 1280)
            page.wait_for_timeout(600)
            seen = _rec_ladder(frame, page, lambda: frame.locator('[data-focus="win:claude:18000"]').click(), 1200)
            assert not [k for k in seen if k.startswith("ladder-") or k.split("|")[1] in ("rail-step", "rail-more", "rail-ladder-title")], ("reduced motion: the steps and the word that counts come at once", seen)
            assert frame.evaluate("() => document.querySelectorAll('.rail-step').length") >= 1
            assert frame.evaluate("() => [...document.querySelectorAll('.rail-step')].every((n) => +getComputedStyle(n).opacity === 1)")
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# The word "N more" opens the ladder BELOW, in the rail, into a window that scrolls; "Read N of M" stands in the plate. The browser's side of it
# (the fake DOM's is the 'ladder_open' mode above), Chromium and WebKit, at the widths of the widget from 860px. The data: claude_many (ten accounts, spread by
# _spread_resets to half an hour apart: ten steps), and for the pick of a window / a family, every window of Claude with those ten.
def _ten_in_every_window(view, donor):
    """Every window of the view with the ten accounts of `donor` (claude_many), each resetting at its own time: ten steps in every window, and windows to pick."""
    many = donor["reserve"]["summary"]["groups"][0]["bars"]
    for group in view["reserve"]["summary"]["groups"]:
        if group.get("bars"):
            group["bars"] = json.loads(json.dumps(many))
    # (the pick of a bar needs its account in the family: the card is drawn for it - so the ten accounts stand in the family as well, as in a real answer)
    view["groups"][0]["accounts"] = json.loads(json.dumps(donor["groups"][0]["accounts"]))
    _spread_resets(view)


# The right part as the browser laid it out: the rect of every node of the card (but the stamp, whose age ticks), the plate, the graph, the shell, the pressed state of
# the chart word, the Details (open or not), the schedule (its rows, its "show all" button) and the scroll of the page.
LADDER_RIGHT = """() => { const q = (s) => document.querySelector(s);
  const r = (n) => { const b = n.getBoundingClientRect(); return [b.left, b.top, b.width, b.height].map((v) => Math.round(v * 10) / 10).join(','); };
  const card = q('.shell-card'), graph = q('.chart-plot');
  return {every: [...card.querySelectorAll('*')].filter((n) => !n.closest('.wt-stamp')).map((n) => String(n.getAttribute('class')) + '@' + r(n)).join('|'),
    shell: r(q('.shell')), card: r(card), plate: r(q('.wt-plate')), graph: graph ? r(graph) : null,
    pressed: [...document.querySelectorAll('.l-chart')].map((b) => b.getAttribute('aria-pressed')), details: [...document.querySelectorAll('details')].map((d) => d.open),
    rows: document.querySelectorAll('table.schedule tr').length, all: [...document.querySelectorAll('.schedule-more')].map((b) => b.getAttribute('aria-expanded') + '|' + b.textContent),
    scrollY: scrollY, shellH: q('.shell').getBoundingClientRect().height, cardH: card.getBoundingClientRect().height}; }"""
# The window of the open ladder: the word, the scroll, the edges, the bar, the room, the beads against the axis of the rings, the last step against the window.
LADDER_WIN = """() => { const q = (s) => document.querySelector(s), rb = (n) => n.getBoundingClientRect();
  const sc = q('.rail-scroll'), more = q('.rail-more'), zone = q('.rail-ladder-zone'), head = q('.rail-ladder-title'), ring = q('.rail .tab-mark-rail .ring'), cs = getComputedStyle(sc);
  const steps = [...document.querySelectorAll('.rail-ladder:not(.rail-ladder-out) .rail-step')].filter((n) => getComputedStyle(n).display !== 'none');
  const w = rb(sc), axis = (rb(ring).left + rb(ring).right) / 2, last = steps[steps.length - 1];
  const inside = steps.filter((n) => rb(n).bottom > w.top + 0.5 && rb(n).top < w.bottom - 0.5);
  return {expanded: more.getAttribute('aria-expanded'), text: more.textContent, label: more.getAttribute('aria-label'), top: sc.scrollTop, max: sc.scrollHeight - sc.clientHeight, client: sc.clientHeight,
    one: steps.length ? steps[0].offsetHeight : 0, standing: steps.length, room: zone.clientHeight - head.offsetHeight - more.offsetHeight, zone: zone.clientHeight, ladder: q('.rail-ladder').offsetHeight,
    start: sc.classList.contains('rail-scroll-start'), end: sc.classList.contains('rail-scroll-end'), on: sc.classList.contains('rail-scroll-on'),
    bar: cs.scrollbarWidth, barSupported: CSS.supports('scrollbar-width', 'none'), inner: sc.offsetWidth - sc.clientWidth, overscroll: cs.overscrollBehaviorY, overflowY: cs.overflowY,
    beads: inside.map((n) => { const b = rb(n.querySelector('.rail-bead')); return Math.abs((b.left + b.right) / 2 - axis); }), inside: inside.length,
    lastIn: last ? rb(last).bottom <= w.bottom + 1 && rb(last).top >= w.top - 1 : null, shell: rb(q('.shell')).height, card: rb(q('.shell-card')).height, scrollY: scrollY,
    haze: parseFloat(cs.getPropertyValue('--ladder-haze')) || 22}; }"""
LADDER_STEPS = ".rail-ladder:not(.rail-ladder-out) .rail-step"


def _ladder_open(browser, views, width, reduced=False, chart=True):
    served, family = _ladder_served(views, "claude_many", _spread_resets)
    page = browser.new_page(viewport={"width": 1400, "height": 900}, **({"reduced_motion": "reduce"} if reduced else {}))
    frame = _mount_ladder_frame(page, served, family, width)
    page.wait_for_timeout(1300)
    if not chart:
        frame.locator('.l-chart[aria-pressed="true"]').first.click()
        page.wait_for_timeout(500)
    return page, frame


def _ladder_fold_the_chart(page, frame):
    """rule: the chart word stands in the summary of the card, which is up in the place of the chart (pinned), under the 900 px viewport of the stand
    (the frame is 1400 px high). Its click makes the stand's page scroll to bring it in sight and leaves the pointer on the plate, where the graph draws its crosshair
    (plate-trace, trace-arm); a reader's hand does neither to the right part, so the page and the pointer are put back before it is measured."""
    frame.locator('.l-chart[aria-pressed="true"]').first.click()
    page.mouse.move(1390, 890)
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(500)
    # rule: the line of the pick is no longer drawn round the plate when the chart comes back or goes (the outline and the stem stand through the card): no .plate-trace is left, and the plate wears .tl-traced
    frame.wait_for_function("() => !document.querySelector('.plate-trace')", timeout=4000)
    page.wait_for_timeout(200)


def _ladder_press(page, frame, wait=1100):
    frame.locator(".rail-ladder:not(.rail-ladder-out) .rail-more").click()   # (the ladder that faded after a pick of a window stays, inert, until the next draw)
    page.mouse.move(1390, 890)   # (the pointer leaves: a step under it would light the bars of its accounts, as it should, and that is the plate's dimming)
    page.wait_for_timeout(wait)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_the_press_of_more_leaves_the_right_part_where_it_was_and_the_second_press_folds_to_four_steps_at_the_start(fixture_file, engine):
    """kit: rail, the ladder. 'N more' opens the ladder in the rail: aria-expanded true, the word reads 'less' (aria-label 'Less: fold the list of resets'); the plate, the graph,
    the capsule (every node of the card: the same rect), the Details (closed, or open with the schedule showing all rows when the reader opened them), the pressed chart word and
    the scroll of the page are as they were, with the chart open and folded, at 1280, 1020 and 860. The second press: four steps and 'N more' again, the scroll at the start."""
    from test_bars import _open_the_card_of_the_chart_word
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for width in (1280, 1020, 860):
                page, frame = _ladder_open(browser, views, width)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                for chart_open in (True, False):
                    if not chart_open:
                        # The chart word stands in the summary of the picked account's card (cardTake), which is folded: the card is opened first, as a reader does.
                        _open_the_card_of_the_chart_word(frame)
                        _ladder_fold_the_chart(page, frame)
                    before = frame.evaluate(LADDER_RIGHT)
                    closed = frame.evaluate(LADDER_WIN)
                    where = (engine, width, chart_open)
                    assert closed["expanded"] == "false" and re.match(r"^\d+ more$", closed["text"]) and 1 <= closed["standing"] <= 4 and not closed["on"], ("folded", where, closed)
                    assert abs(before["shellH"] - before["cardH"]) <= 1, ("the capsule is as high as the card", where, before["shellH"], before["cardH"])
                    assert not any(before["details"]), ("the Details are closed", where, before["details"])
                    _ladder_press(page, frame)
                    opened, after = frame.evaluate(LADDER_WIN), frame.evaluate(LADDER_RIGHT)
                    assert opened["expanded"] == "true" and opened["text"] == "less" and opened["label"] == "Less: fold the list of resets", ("open: the word reads less", where, opened)
                    assert opened["on"] and opened["standing"] == 10, ("every step stands in the window", where, opened)
                    assert after == before, ("the right part is not changed by a pixel", where, [k for k in before if before[k] != after[k]])
                    # the second press: back to the first steps and the word that counts, the scroll at the start
                    frame.evaluate("() => { document.querySelector('.rail-scroll').scrollTop = 90; }")
                    page.wait_for_timeout(200)
                    assert frame.evaluate(LADDER_WIN)["top"] > 0
                    _ladder_press(page, frame, 700)
                    folded = frame.evaluate(LADDER_WIN)
                    assert folded["expanded"] == "false" and folded["text"] == closed["text"] and folded["standing"] == closed["standing"] and folded["top"] == 0 and not folded["on"], ("folded", where, folded, closed)
                    assert folded["label"] == closed["label"] and frame.evaluate(LADDER_RIGHT) == before, ("and the right part is still as it was", where)
                    if chart_open:
                        # the Details opened by the reader, the schedule showing all its rows: the press touches neither (scheduleAll, details.open)
                        frame.locator("details.chart-table > summary").click()
                        page.wait_for_timeout(300)
                        if frame.locator(".schedule-more").count():
                            frame.locator(".schedule-more").first.click()
                            page.wait_for_timeout(300)
                        base = frame.evaluate(LADDER_RIGHT)
                        assert base["details"][0] is True and base["rows"] > 0, ("the Details with the schedule stand open", where, base["details"], base["rows"])
                        assert base["all"] and base["all"][0].startswith("true|"), ("the schedule shows all its rows", where, base["all"])
                        _ladder_press(page, frame)
                        assert frame.evaluate(LADDER_WIN)["expanded"] == "true"
                        assert frame.evaluate(LADDER_RIGHT) == base, ("open Details and the schedule are not touched", where)
                        _ladder_press(page, frame, 700)
                        assert frame.evaluate(LADDER_WIN)["expanded"] == "false" and frame.evaluate(LADDER_RIGHT) == base, ("nor by the fold", where)
                        frame.locator("details.chart-table > summary").click()   # (as it was: folded)
                        page.wait_for_timeout(300)
                assert errors == [], errors
                page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_the_window_is_5_5_steps_at_most_never_stretches_the_capsule_scrolls_to_the_last_step_has_no_bar_and_keeps_the_beads_on_the_axis(fixture_file, engine):
    """kit: rail, the ladder. The open window is 5.5 steps high at most and no higher than the room of the zone (under the head, over the word), and the capsule is not stretched
    (.shell = .shell-card, 1px), with the chart open and folded, at 1280, 1020 and 860. It scrolls to the last step whole; there is no bar (offsetWidth = clientWidth, scrollbar-width none
    where supported), overscroll-behavior contain (a wheel at the end does not drag the page); .rail-scroll-start at the start and .rail-scroll-end at the end (both only there); the
    beads of the steps in sight stand on the axis of the rings, 0.5px, at every place of the scroll."""
    from test_bars import _open_the_card_of_the_chart_word
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for width in (1280, 1020, 860):
                page, frame = _ladder_open(browser, views, width)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                for chart_open in (True, False):
                    if not chart_open:
                        # The chart word stands in the summary of the picked account's card (cardTake), which is folded: the card is opened first, as a reader does.
                        _open_the_card_of_the_chart_word(frame)
                        _ladder_fold_the_chart(page, frame)
                    shut = frame.evaluate(LADDER_WIN)
                    assert abs(shut["shell"] - shut["card"]) <= 1, ("folded: the capsule is as high as the card", engine, width, chart_open, shut["shell"], shut["card"])
                    _ladder_press(page, frame)
                    g = frame.evaluate(LADDER_WIN)
                    where = (engine, width, chart_open, g)
                    assert g["client"] <= 5.5 * g["one"] + 1 and g["client"] >= g["one"], ("5.5 steps at most (and a whole one at least)", where)
                    assert g["client"] <= g["room"] + 1 and g["ladder"] <= g["zone"] + 1, ("and never more than the room of the zone", where)
                    assert abs(g["shell"] - g["card"]) <= 1 and g["shell"] == shut["shell"], ("the capsule is not stretched", where)
                    assert g["max"] > g["one"], ("there is more to scroll to", where)
                    assert g["inner"] == 0 and g["overflowY"] == "auto" and g["overscroll"] == "contain", ("no bar taking room, the page is not dragged", where)
                    if g["barSupported"]:
                        assert g["bar"] == "none", ("scrollbar-width none", where)
                    # the place of the scroll: the start, in the middle, the end; the classes only at the ends; the beads on the axis in sight each time
                    for place in (0, 40, g["max"] // 2, g["max"] - 5, g["max"]):
                        frame.evaluate("(top) => { document.querySelector('.rail-scroll').scrollTop = top; }", place)
                        page.wait_for_timeout(150)
                        s = frame.evaluate(LADDER_WIN)
                        w = (engine, width, chart_open, place, s)
                        assert abs(s["top"] - place) <= 1 or place == g["max"], w
                        assert s["start"] == (s["top"] <= 1) and s["end"] == (s["top"] >= s["max"] - 1), ("the haze classes only where an edge is reached", w)
                        assert s["inside"] >= int(g["client"] // g["one"]) and s["beads"] and max(s["beads"]) <= 0.5, ("the beads in sight stand on the axis of the rings", w)
                        assert abs(s["shell"] - s["card"]) <= 1, ("and the capsule still", w)
                    assert s["end"] and not s["start"] and s["lastIn"], ("the end: the last step stands whole in the window, no haze under it", where, s)
                    # a wheel over the window at its end does not drag the page; one at its start moves the window and not the page
                    box = frame.locator(".rail-scroll").bounding_box()
                    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    page.mouse.wheel(0, 600)
                    page.wait_for_timeout(400)
                    end = frame.evaluate(LADDER_WIN)
                    assert page.evaluate("() => scrollY") == 0 and end["scrollY"] == 0 and end["top"] >= end["max"] - 1, ("a wheel at the end drags nothing else", engine, width, end)
                    frame.evaluate("() => { document.querySelector('.rail-scroll').scrollTop = 0; }")
                    page.wait_for_timeout(150)
                    page.mouse.wheel(0, 120)
                    page.wait_for_timeout(500)
                    moved = frame.evaluate(LADDER_WIN)
                    assert moved["top"] > 0 and page.evaluate("() => scrollY") == 0, ("a wheel moves the window, not the page", engine, width, moved)
                    _ladder_press(page, frame, 600)
                    fold = frame.evaluate(LADDER_WIN)
                    assert fold["top"] == 0 and fold["expanded"] == "false", fold
                assert errors == [], errors
                page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_the_poll_and_refresh_keep_the_ladder_open_at_its_place_without_motion_and_the_pick_of_a_window_or_a_family_folds_it(fixture_file, engine):
    """kit: rail, the ladder (state). The 30 s poll (the visibility draw) and 'Refresh' draw the ladder again: it stands open, at the same place of the scroll (the new node gets
    it), with no ladder animation at all. The pick of another window folds it (four steps, 'N more', the scroll at the start); so does the pick of another family (and back)."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            served, family = _ladder_served(views, "claude", lambda view: _ten_in_every_window(view, views["claude_many"]))
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_ladder_frame(page, served, family, 1280)
            _settle_family(frame, family)
            shut = frame.evaluate(LADDER_WIN)
            assert re.match(r"^\d+ more$", shut["text"]) and shut["standing"] <= 4, shut
            _ladder_press(page, frame)
            frame.evaluate("() => { document.querySelector('.rail-scroll').scrollTop = 120; }")
            page.wait_for_timeout(250)
            kept = frame.evaluate(LADDER_WIN)
            assert kept["expanded"] == "true" and 100 <= kept["top"] <= 140 and not kept["start"] and not kept["end"], kept
            frame.evaluate("() => { window.__scroll = document.querySelector('.rail-scroll'); return true; }")
            for how, act in (("the poll", lambda: frame.evaluate(REDRAW)), ("Refresh", lambda: frame.click('[data-focus="refresh"]'))):
                seen = _rec_ladder(frame, page, act, 1100)
                assert not [k for k in seen if k.startswith("ladder-") or k.split("|")[1] in ("rail-step", "rail-more", "rail-ladder-title")], (how + " moves nothing", seen)
                page.wait_for_timeout(300)
                now = frame.evaluate(LADDER_WIN)
                assert now["expanded"] == "true" and now["text"] == "less" and now["on"] and now["standing"] == kept["standing"], (how, "stays open", now)
                assert abs(now["top"] - kept["top"]) <= 1 and now["start"] == kept["start"] and now["end"] == kept["end"] and now["client"] == kept["client"], (how, "at the same place", now, kept)
                assert frame.evaluate(LADDER_NAMES) == [], (how, frame.evaluate(LADDER_NAMES))
            assert frame.evaluate("() => window.__scroll !== document.querySelector('.rail-scroll')"), "(the draw made a new node, and it got the place)"
            # the pick of another window
            tab = frame.evaluate("() => { const t = [...document.querySelectorAll('.rail .wt-tab')].find((n) => n.getAttribute('aria-selected') !== 'true'); return t && t.getAttribute('data-focus'); }")
            assert tab, "Claude has other windows to pick"
            frame.locator('[data-focus="' + tab + '"]').click()
            page.mouse.move(1390, 890)
            page.wait_for_timeout(1500)
            picked = frame.evaluate(LADDER_WIN)
            assert picked["expanded"] == "false" and re.match(r"^\d+ more$", picked["text"]) and picked["top"] == 0 and not picked["on"] and picked["standing"] <= 4, ("another window: folded", picked)
            # open again, scrolled; the pick of another family and the way back
            _ladder_press(page, frame)
            frame.evaluate("() => { document.querySelector('.rail-scroll').scrollTop = 150; }")
            page.wait_for_timeout(250)
            assert frame.evaluate(LADDER_WIN)["expanded"] == "true"
            frame.locator('[data-focus="harness:codex"]').click()
            _settle_family(frame, "codex")
            assert frame.evaluate("() => { const m = document.querySelector('.rail-more'); return !m || m.getAttribute('aria-expanded') !== 'true'; }"), "another family: the ladder is not open"
            frame.locator('[data-focus="harness:claude"]').click()
            _settle_family(frame, "claude")
            back = frame.evaluate(LADDER_WIN)
            assert back["expanded"] == "false" and back["top"] == 0 and not back["on"] and re.match(r"^\d+ more$", back["text"]), ("and back: folded", back)
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_a_step_below_the_fourth_lights_its_bars_and_tab_scrolls_the_window_to_the_hidden_step(fixture_file, engine):
    """kit: rail, the ladder. The pointer on a step past the fourth lights the bars of its accounts on the plate (data-dim group, data-lit; the others step back), as before.
    Tab (and Shift+Tab) through the open window: the step in focus stands in the window each time, out of the haze, and its bars light; Tab goes on to the word 'less'."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    probe = """() => { const plate = document.querySelector('.wt-plate'), a = document.activeElement, w = document.querySelector('.rail-scroll').getBoundingClientRect(), r = a.getBoundingClientRect(),
      sc = document.querySelector('.rail-scroll'), haze = parseFloat(getComputedStyle(sc).getPropertyValue('--ladder-haze')) || 22;
      return {cls: a.className, k: String([...document.querySelectorAll('.rail-ladder:not(.rail-ladder-out) .rail-step')].indexOf(a)), top: r.top - w.top, bottom: w.bottom - r.bottom, scroll: sc.scrollTop, max: sc.scrollHeight - sc.clientHeight, haze,
        start: sc.classList.contains('rail-scroll-start'), end: sc.classList.contains('rail-scroll-end'), dim: plate.getAttribute('data-dim'),
        lit: [...plate.querySelectorAll('.bars .bar[data-lit]')].map((b) => b.getAttribute('data-account')).sort(), accounts: a.getAttribute('data-accounts') ? JSON.parse(a.getAttribute('data-accounts')).sort() : null}; }"""
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page, frame = _ladder_open(browser, views, 1280)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            _ladder_press(page, frame)
            # the pointer: a step past the fourth, with the window scrolled to it
            for index in (5, 9):
                frame.locator(LADDER_STEPS).nth(index).hover()
                page.wait_for_timeout(350)
                now = frame.evaluate("""(k) => { const plate = document.querySelector('.wt-plate'), s = document.querySelectorAll('.rail-ladder:not(.rail-ladder-out) .rail-step')[k], bars = [...plate.querySelectorAll('.bars .bar')];
                  return {dim: plate.getAttribute('data-dim'), lit: bars.filter((b) => b.hasAttribute('data-lit')).map((b) => b.getAttribute('data-account')).sort(), accounts: JSON.parse(s.getAttribute('data-accounts')).sort(),
                    others: bars.filter((b) => !b.hasAttribute('data-lit')).map((b) => +getComputedStyle(b).opacity), n: bars.length}; }""", index)
                assert now["dim"] == "group" and now["lit"] == now["accounts"] and now["accounts"], (engine, index, "the bars of the step's accounts light", now)
                assert now["others"] and all(o < 1 for o in now["others"]), (engine, index, "and the others step back", now)
            page.mouse.move(1390, 890)
            page.wait_for_timeout(300)
            assert frame.evaluate("() => document.querySelector('.wt-plate').getAttribute('data-dim')") is None
            # the keyboard: from the fourth step down through the hidden ones to the word, and back up
            frame.evaluate("() => { document.querySelector('.rail-scroll').scrollTop = 0; }")
            page.wait_for_timeout(150)
            frame.evaluate("(sel) => document.querySelectorAll(sel)[3].focus()", LADDER_STEPS)
            page.wait_for_timeout(100)
            assert frame.evaluate(probe)["k"] == "3"
            for index in range(4, 10):
                page.keyboard.press("Tab")
                page.wait_for_timeout(250)
                p = frame.evaluate(probe)
                where = (engine, index, p)
                assert p["k"] == str(index) and "rail-step" in p["cls"], ("Tab goes to the next step", where)
                free_top, free_bottom = (0 if p["start"] else p["haze"]) - 1, (0 if p["end"] else p["haze"]) - 1
                assert p["top"] >= -1 and p["bottom"] >= -1, ("the step is in the window (scrolled to)", where)
                assert p["top"] >= free_top and p["bottom"] >= free_bottom, ("and out of the haze", where)
                assert p["dim"] == "group" and p["lit"] == p["accounts"], ("its bars light", where)
            page.keyboard.press("Tab")
            page.wait_for_timeout(250)
            after_last = frame.evaluate("() => document.activeElement.className")
            if engine == "chromium":
                assert "rail-more" in after_last, ("Tab goes on to the word", after_last)
            else:   # (WebKit's Tab passes the buttons by default, as Safari's does: it leaves the steps for the next thing)
                assert "rail-step" not in after_last, ("Tab leaves the steps", after_last)
            page.keyboard.press("Shift+Tab")
            for index in range(9, 0, -1):
                page.wait_for_timeout(250)
                p = frame.evaluate(probe)
                assert p["k"] == str(index) and p["top"] >= -1 and p["bottom"] >= -1, ("Shift+Tab back up", engine, index, p)
                free_top, free_bottom = (0 if p["start"] else p["haze"]) - 1, (0 if p["end"] else p["haze"]) - 1
                assert p["top"] >= free_top and p["bottom"] >= free_bottom, ("out of the haze going up too", engine, index, p)
                page.keyboard.press("Shift+Tab")
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_the_ring_of_focus_on_a_step_stands_inside_its_box_and_is_seen_whole_in_the_window(fixture_file, engine):
    """kit: rail, the ladder. The window of the steps and the zone cut what is outside them, so the ring of the keyboard's focus is drawn inside the box of the step and of
    the word (a negative outline-offset, --ring-inset): the pixels of the four edges of the step in focus are the colour of the ring, in the open window and in the folded ladder, and a
    ring is never cut. (The word is reached by Tab in Chromium; WebKit's Tab passes buttons, so there its rule is read.)"""
    from conftest import launch_engine
    from io import BytesIO

    PIL_Image = pytest.importorskip("PIL.Image")
    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    probe = """() => { const a = document.activeElement, cs = getComputedStyle(a), r = a.getBoundingClientRect(),
      z = document.querySelector('.rail-ladder-zone').getBoundingClientRect(), sc = document.querySelector('.rail-scroll').getBoundingClientRect();
      return {cls: a.className, visible: a.matches(':focus-visible'), offset: parseFloat(cs.outlineOffset), width: parseFloat(cs.outlineWidth), color: cs.outlineColor,
        box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
        inZone: r.left >= z.left - 0.5 && r.right <= z.right + 0.5 && r.top >= z.top - 0.5 && r.bottom <= z.bottom + 0.5,
        inWindow: r.left >= sc.left - 0.5 && r.right <= sc.right + 0.5}; }"""

    def edges(page, p):
        x, y, w, h = p["box"]
        frame_box = page.locator("iframe").bounding_box()
        x, y = x + frame_box["x"], y + frame_box["y"]
        image = PIL_Image.open(BytesIO(page.screenshot(clip={"x": x, "y": y, "width": w, "height": h}))).convert("RGB")
        want = tuple(int(v) for v in re.findall(r"\d+", p["color"])[:3])
        near = lambda px: sum(abs(a - b) for a, b in zip(px, want)) <= 60
        # the middle of each side, and a quarter along it (the thread of a step crosses the middle of its top); the second pixel in from the edge
        return {"top": near(image.getpixel((w // 4, 1))), "bottom": near(image.getpixel((w // 4, h - 2))),
                "left": near(image.getpixel((1, h // 2))), "right": near(image.getpixel((w - 2, h // 2)))}

    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page, frame = _ladder_open(browser, views, 1280)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            for opened in (False, True):
                if opened:
                    _ladder_press(page, frame)
                page.mouse.move(1390, 890)
                frame.evaluate("(sel) => document.querySelectorAll(sel)[0].focus()", LADDER_STEPS)
                page.keyboard.press("Tab")
                page.wait_for_timeout(300)
                p = frame.evaluate(probe)
                where = (engine, opened, p)
                assert "rail-step" in p["cls"] and p["visible"], ("a step in focus by the keyboard", where)
                assert p["offset"] + p["width"] <= 0.01 and p["width"] >= 2 - 0.01, ("the ring is inside the box of the step", where)
                assert p["inZone"] and p["inWindow"], ("and the box is inside the zone and the window", where)
                assert all(edges(page, p).values()), ("the ring is seen whole: its colour on the four sides", edges(page, p), where)
            # the word
            if engine == "chromium":
                frame.evaluate("(sel) => { const s = document.querySelectorAll(sel); s[s.length - 1].focus(); }", LADDER_STEPS)
                page.keyboard.press("Tab")
                page.wait_for_timeout(300)
                p = frame.evaluate(probe)
                assert "rail-more" in p["cls"] and p["visible"], ("the word in focus", p)
                assert p["offset"] + p["width"] <= 0.01 and p["inZone"], ("the ring of the word is inside its box, in the zone", p)
                assert all(edges(page, p).values()), ("the ring of the word is seen whole", edges(page, p), p)
            else:
                rule = frame.evaluate("""() => { const out = []; for (const sheet of document.styleSheets) for (const r of sheet.cssRules) if (r.selectorText && /\\.rail-more:focus-visible/.test(r.selectorText) && r.style.outlineOffset) out.push(r.style.outlineOffset);
                  return out; }""")
                assert rule and "ring-inset" in rule[-1], ("the word's ring is drawn inside", rule)
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_the_pointer_leaving_a_step_gives_the_plate_back_to_the_step_the_keyboard_holds(fixture_file, engine):
    """kit: rail, the ladder. The steps stand in the window (.rail-scroll), so the list a step is bound to is the window and not the ladder: a step in focus by the keyboard keeps the
    plate lit for its accounts when the pointer has been over another step and leaves it."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    probe = """() => { const plate = document.querySelector('.wt-plate'), a = document.activeElement;
      return {dim: plate.getAttribute('data-dim'), lit: [...plate.querySelectorAll('.bars .bar[data-lit]')].map((b) => b.getAttribute('data-account')).sort(),
        accounts: a.getAttribute('data-accounts') ? JSON.parse(a.getAttribute('data-accounts')).sort() : null, cls: a.className, visible: a.matches(':focus-visible')}; }"""
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page, frame = _ladder_open(browser, views, 1280)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            _ladder_press(page, frame)
            page.mouse.move(1390, 890)
            frame.evaluate("(sel) => document.querySelectorAll(sel)[1].focus()", LADDER_STEPS)
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)
            held = frame.evaluate(probe)
            assert "rail-step" in held["cls"] and held["visible"] and held["dim"] == "group" and held["lit"] == held["accounts"] and held["accounts"], ("the keyboard holds a step", held)
            # the pointer over the step above it (it stands in the window), then away
            frame.locator(LADDER_STEPS).nth(1).hover()
            page.wait_for_timeout(300)
            assert frame.evaluate(probe)["dim"] == "group"
            page.mouse.move(1390, 890)
            page.wait_for_timeout(400)
            now = frame.evaluate(probe)
            assert now["cls"] == held["cls"] and now["dim"] == "group" and now["lit"] == held["accounts"], ("the plate goes back to the step in focus", engine, held, now)
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_the_steps_past_the_fourth_come_down_by_ladder_drop_and_under_reduced_motion_nothing_moves(fixture_file, engine):
    """kit: rail, motion. The press of 'N more': the steps that come into sight below the fourth come down by ladder-drop (transform and opacity alone, three at most, one after
    another), nothing is left running after it, and the fold runs none. Under reduced motion the press runs no ladder animation at all and the window stands open."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    ladder = lambda seen: {k: v for k, v in seen.items() if k.startswith("ladder-") or k.split("|")[1] in ("rail-step", "rail-more", "rail-ladder-title")}
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page, frame = _ladder_open(browser, views, 1280)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            seen = _rec_ladder(frame, page, lambda: frame.locator(".rail-ladder:not(.rail-ladder-out) .rail-more").click(), 1400)
            drop = ladder(seen)
            assert "ladder-drop|rail-step" in drop, ("the steps below the fourth come down", seen)
            assert all(v["props"] in ("opacity+transform", "opacity", "transform") for k, v in drop.items()), ("transform and opacity alone", seen)
            assert 1 <= len(drop["ladder-drop|rail-step"]["delays"]) <= 3, ("the ones in sight only, one after another, a ceiling to the delay", seen)
            page.mouse.move(1390, 890)
            page.wait_for_timeout(700)
            assert frame.evaluate(LADDER_NAMES) == [], ("nothing is left running", frame.evaluate(LADDER_NAMES))
            assert frame.evaluate(LADDER_WIN)["expanded"] == "true"
            seen = _rec_ladder(frame, page, lambda: frame.locator(".rail-ladder:not(.rail-ladder-out) .rail-more").click(), 1000)
            assert not ladder(seen), ("the fold runs no ladder animation", seen)
            assert frame.evaluate(LADDER_WIN)["expanded"] == "false"
            page.close()
            page, frame = _ladder_open(browser, views, 1280, reduced=True)
            seen = _rec_ladder(frame, page, lambda: frame.locator(".rail-ladder:not(.rail-ladder-out) .rail-more").click(), 1000)
            assert not ladder(seen), ("reduced motion: no ladder animation at the press", seen)
            page.mouse.move(1390, 890)
            page.wait_for_timeout(300)
            opened = frame.evaluate(LADDER_WIN)
            assert opened["expanded"] == "true" and opened["standing"] == 10 and frame.evaluate(LADDER_NAMES) == [], ("and it stands open", opened)
            assert frame.evaluate("() => [...document.querySelectorAll('.rail-step')].every((n) => +getComputedStyle(n).opacity === 1)")
            seen = _rec_ladder(frame, page, lambda: frame.locator(".rail-ladder:not(.rail-ladder-out) .rail-more").click(), 800)
            assert not ladder(seen) and frame.evaluate(LADDER_WIN)["expanded"] == "false", seen
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


# Where the stamp stands: the rect of the stamp, the figure (the grid cell 'fig'), the chart word, the plate and the card, whether the stamp is whole, and the cut of anything in the plate.
LADDER_STAMP = """() => { const q = (s) => document.querySelector(s), r = (n) => { const b = n.getBoundingClientRect(); return {l: b.left, r: b.right, t: b.top, b: b.bottom, w: b.width, h: b.height}; };
  const st = q('.wt-plate .wt-stamp'), plate = q('.wt-plate'); if (!st) return {stamp: null, inRail: !!q('.rail .wt-stamp'), bot: !!q('.rail-bot')};
  const fig = q('.wt-plate .wt-fact') || q('.wt-plate .l-fig'), ctl = q('.wt-plate .l-chart'), cs = getComputedStyle(st);
  const cut = [...plate.querySelectorAll('*')].filter((n) => !n.closest('.sr-only') && !(n instanceof SVGElement) && !n.classList.contains('rail-scroll-x') && !(n.classList.contains('bar') && n.querySelector('.bar-i')) && n.clientWidth > 0 && n.scrollWidth > n.clientWidth + 1).map((n) => n.className);  // (a bar of an account holds its words shut in it, clipped by its own overflow until it opens: test_bars.py looks at them open) (the row of bars scrolls sideways by design when 16 px bars do not fit: part 1 of the top)
  return {width: innerWidth, scroll: document.documentElement.scrollWidth, stamp: r(st), fig: r(fig), figCls: fig.className, ctl: ctl ? r(ctl) : null, plate: r(plate), card: r(q('.shell-card')), text: st.textContent,
    whole: st.scrollWidth <= st.clientWidth + 1, inRail: !!q('.rail .wt-stamp'), bot: !!q('.rail-bot'), inPlate: st.parentNode === plate, white: cs.whiteSpace, align: cs.textAlign, cut}; }"""


def _ladder_stamp_check(at, width, where, row_from=640):
    assert at["stamp"] and at["inPlate"] and not at["inRail"] and not at["bot"], ("the stamp is in the plate; the rail has none and no foot", where, at)
    s, f, p = at["stamp"], at["fig"], at["plate"]
    assert at["scroll"] <= width and at["whole"] and not at["cut"], ("no sideways scroll, nothing cut", where, at)
    assert s["l"] >= p["l"] - 0.5 and s["r"] <= p["r"] + 0.5 and s["t"] >= p["t"] - 0.5 and s["b"] <= p["b"] + 0.5, ("inside the plate", where, at)
    if width >= row_from:
        assert s["h"] <= 17 and at["white"] == "nowrap", ("one line", where, at)
        assert s["l"] >= f["r"] - 0.5, ("right of the figure", where, at)
        assert s["t"] < f["b"] and s["b"] > f["t"], ("in the row of the figure", where, at)
        if at["ctl"]:
            assert s["r"] <= at["ctl"]["l"] + 0.5, ("before the chart word", where, at)
    else:
        assert s["t"] >= f["b"] - 0.5 and abs(s["l"] - f["l"]) <= 1, ("tight: a line of its own under the figure, at the left", where, at)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_the_stamp_stands_in_the_row_of_the_figure_before_the_chart_word_and_goes_under_it_from_639px(fixture_file, engine):
    """capsule: window tabs, the stamp. At 1280, 1020, 860 and down to 640 the stamp is one line at the right end of the first line of the plate, right of the fact's bubble (it spans the
    height of the bubble) and left of the chart word, whole, inside the plate; the rail has no stamp and no foot. At 639 (the media rule max-width:639.98px, measured on both sides of it), 360 and 282 it is a
    line of its own under the bubble of the fact (the model list and the bubble of the limit come before it), at the left. No
    sideways scroll and nothing cut at 1280, 860, 678, 640, 639, 360. Cursor (the plate of the budget, no chart word) has it in its own plate in the row of its figure. Russian words too."""
    from test_bars import _mount_layout_frame, _let_go_the_pick
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    seen = 0
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for key in ("claude_many", "reading_part", "cursor_budget", "cursor_mixed"):
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                frame = _mount_layout_frame(page, views[key], 1280)
                # The stamp stands in the plate, in the row of the figure: the pick of the top account is let go first (picked, the stamp stands in its card).
                _let_go_the_pick(frame)
                for width in (1280, 1020, 860, 678, 640, 639, 360, 282):
                    page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                    frame.wait_for_function("(width) => innerWidth === width", arg=width)
                    page.wait_for_timeout(120)
                    at = frame.evaluate(LADDER_STAMP)
                    if not at["stamp"]:
                        assert key.startswith("cursor") and "mixed" in key, ("only Cursor's mixed view may draw no stamp", key, width, at)
                        continue
                    seen += 1
                    _ladder_stamp_check(at, width, (engine, key, width))
                    assert re.match(r"^Read \d+ of \d+ · .+$", at["text"].strip()), at["text"]
                page.close()
            # the same in Russian (the longer words), through the narrow range where the row is tightest
            served, family = _ladder_served(views, "claude_many")
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            frame = _mount_ladder_frame(page, served, family, 1280, lang="ru")
            _let_go_the_pick(frame)  # (the same in Russian: the stamp is the plate's own only with the pick let go)
            for width in (1280, 860, 678, 640, 639, 360):
                page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                frame.wait_for_function("(width) => innerWidth === width", arg=width)
                page.wait_for_timeout(120)
                at = frame.evaluate(LADDER_STAMP)
                _ladder_stamp_check(at, width, (engine, "ru", width))
                assert at["text"].strip().startswith("Прочитано"), at["text"]
                seen += 1
            page.close()
        finally:
            browser.close()
    assert seen >= 20


# The top of the plate (capsule: window tabs): the bars over the whole width, under them the row of the number - the number at the left over two lines,
# at its right the window's name over "of N accounts", then the model list, the stamp and the chart word - and the notes under that row; one column under
# 640px, the bars first. No bubble: nothing here has a ground, a shadow, a corner or a padding. Where they stand, as the browser lays them out.
TOP_GEOMETRY = """() => { const q = (s, r = document) => r.querySelector(s);
  const box = (n) => { if (!n) return null; const b = n.getBoundingClientRect(); return {l: b.left, r: b.right, t: b.top, b: b.bottom, w: b.width, h: b.height}; };
  const plate = q('.wt-plate'), fact = q('.wt-plate > .wt-fact'), cs = fact && getComputedStyle(fact), ps = getComputedStyle(plate);
  const num = q('.wt-fact .wt-num', plate), of = q('.wt-fact .of', plate), cap = q('.wt-fact-cap', plate), limit = q('.wt-plate > .wt-limit');
  const cut = [...plate.querySelectorAll('.wt-fact, .wt-fact *, .wt-limit, .wt-limit *, .wt-scope, .wt-scope *')]
    .filter((n) => !n.closest('.sr-only') && !n.classList.contains('wt-scope-opt') && n.clientWidth > 0 && n.scrollWidth > n.clientWidth + 1)
    .map((n) => n.className + ' ' + n.scrollWidth + '/' + n.clientWidth + ' ' + n.textContent.slice(0, 40));
  const bare = (n) => { if (!n) return null; const c = getComputedStyle(n); return {ground: c.backgroundColor, shadow: c.boxShadow, radius: c.borderTopLeftRadius, pad: c.padding, border: c.borderTopWidth + ' ' + c.borderBottomWidth}; };
  return {width: innerWidth, scroll: document.documentElement.scrollWidth, plate: box(plate), fact: box(fact), cap: box(cap), num: box(num), of: box(of),
    scope: box(q('.wt-plate > .wt-scope')), limit: box(limit), stamp: box(q('.wt-plate > .wt-stamp')), ctl: box(q('.wt-plate > .l-chart')),
    bars: box(q('.wt-plate > .bars')), lg: box(q('.wt-plate > .lg')), nx: box(q('.wt-plate > .nx')), cut,
    opts: [...plate.querySelectorAll('.wt-scope-opt')].map(box), dot: box(q('.wt-fact .wt-dot', plate)),
    bareFact: bare(fact), barePlate: bare(plate), bareLimit: bare(limit),
    dotInk: q('.wt-fact .wt-dot', plate) ? getComputedStyle(q('.wt-fact .wt-dot', plate)).color : null,
    numInk: num ? getComputedStyle(num).color : null, numSize: num ? getComputedStyle(num).fontSize : null, numWeight: num ? getComputedStyle(num).fontWeight : null,
    capInk: cap ? getComputedStyle(cap).color : null, ofInk: of ? getComputedStyle(of).color : null, cap_text: (cap || {}).textContent, of_text: (of || {}).textContent}; }"""


def _claude_week_at_limit(views):
    """Claude's weekly (three limits: the model list) with one account at the limit now in each of them: the model list and the bubble together."""
    view = json.loads(json.dumps(views["claude"]))
    for g in view["reserve"]["summary"]["groups"]:
        if str(WEEK) in g["key"]:
            for b in [b for b in g["bars"] if b.get("state") == "current"][:1]:
                b.update(at_limit=True, left=0)
            g["measured"] = dict(g.get("measured", {}), at_limit=1)
    return view


def _pick_window(frame, focus):
    if frame.get_attribute('[data-focus="%s"]' % focus, "aria-selected") != "true":
        frame.click('[data-focus="%s"]' % focus)
    frame.wait_for_function("(f) => document.querySelector('[data-focus=\"' + f + '\"]').getAttribute('aria-selected') === 'true'", arg=focus)


def _top_check(g, width, where):
    p, fact, bars = g["plate"], g["fact"], g["bars"]
    assert fact and bars and g["scroll"] <= width, ("no sideways scroll", where, g)
    # (an option of the model list longer than the line is cut with an ellipsis by design, and stays inside the plate: the fixture has such a name)
    assert not g["cut"], ("nothing of the top is cut", where, g["cut"])
    assert all(o["l"] >= p["l"] - 0.5 and o["r"] <= p["r"] + 0.5 for o in g["opts"]), ("the options of the model list inside the plate", where, g["opts"])
    parts = [g[k] for k in ("fact", "scope", "limit", "stamp", "ctl") if g[k]]
    assert all(p["l"] - 0.5 <= b["l"] and b["r"] <= p["r"] + 0.5 and p["t"] - 0.5 <= b["t"] and b["b"] <= p["b"] + 0.5 for b in parts), ("inside the plate", where, g)
    # The left edge of the number is the left edge of the bars (and of the notes, the legend and the reset, which share it).
    assert abs(fact["l"] - bars["l"]) <= 0.5 and abs(g["num"]["l"] - bars["l"]) <= 0.5, ("the number and the bars share a left edge", where, fact["l"], bars["l"])
    # The number at the left over both lines (44px, thin); at its right, in one column, the window's name over "of N accounts" (the name in the ink, the rest quiet): no word
    # "Remaining" any more (its meaning is in the hint of the number).
    assert g["numSize"] == "44px" and g["numWeight"] == "300", ("the number is 44px at the light weight", where, g["numSize"], g["numWeight"])
    assert g["cap_text"] and "Remaining" not in g["cap_text"] and g["cap"] and g["of"] and g["of_text"], ("the name of the window, with no word Remaining, over the unit", where, g["cap_text"])
    assert g["num"]["r"] <= g["cap"]["l"] + 0.5 and g["num"]["r"] <= g["of"]["l"] + 0.5, ("the words stand at the right of the number", where, g)
    assert abs(g["cap"]["l"] - g["of"]["l"]) <= 0.5 and g["cap"]["b"] <= g["of"]["t"] + 0.5, ("the name over the unit, at one left edge", where, g)
    assert fact["l"] - 0.5 <= g["num"]["l"] and g["cap"]["r"] <= fact["r"] + 0.5 and g["of"]["r"] <= fact["r"] + 0.5, ("inside the fact", where, g)
    assert g["capInk"] != g["ofInk"], ("the name is the ink, the unit the quiet ink", where, g["capInk"], g["ofInk"])
    # No bubble: the fact and the note of the limit have no ground, shadow, corner or padding of their own, and the plate has no ground, shadow or line (it keeps only the air under
    # it, which is the room the graph needs: a padding at its foot, none at its sides).
    for k in ("bareFact", "barePlate", "bareLimit"):
        b = g[k]
        if b:
            assert b["ground"] == "rgba(0, 0, 0, 0)" and b["shadow"] == "none" and b["border"] == "0px 0px", ("no bubble: " + k, where, b)
            if k != "barePlate":
                assert b["radius"] == "0px" and b["pad"] == "0px", ("no bubble: " + k, where, b)
            else:
                assert b["pad"].startswith("0px 0px "), ("no air at the plate's top and sides: " + k, where, b)
    # The legend and the reset stand under the bars, the legend over the reset, at every width (the flow must not put them in a row over the bars).
    below = bars["b"]
    for k in ("lg", "nx"):
        if g[k] and g[k]["h"] > 0:  # (the reset row gives way to the ladder under the rings on the wide shell: it has no box then)
            assert g[k]["t"] >= below - 0.5, ("under the bars: " + k, where, g)
            below = g[k]["b"]
    if g["lg"] and g["lg"]["h"] > 0 and g["limit"]:
        assert g["lg"]["t"] >= g["limit"]["b"] - 0.5, ("the legend under the note of the limit", where, g)
    if g["dot"]:
        assert g["dotInk"] != g["numInk"], ("the decimal point is the crimson ink", where, g["dotInk"], g["numInk"])
    if width >= 640:
        # Wide: the bars over the whole width; under them one row - the number, the model list, the stamp, the chart word - and the note of the limit under that row, at the
        # left edge, over the whole width.
        assert bars["r"] >= p["r"] - 1 and bars["l"] <= p["l"] + 1, ("the bars take the whole width", where, bars, p)
        assert all(b["t"] >= bars["b"] - 0.5 for b in parts if b is not bars), ("the row of the number under the bars", where, g)
        for k in ("scope", "stamp", "ctl"):
            if g[k]:
                assert g[k]["l"] >= fact["r"] - 0.5, ("right of the number: " + k, where, g)
                # (in the row of the number: the middle of each stands on the middle of the number's two lines, within 8px - the model list may be taller than the number)
                assert abs((g[k]["t"] + g[k]["b"]) / 2 - (fact["t"] + fact["b"]) / 2) <= 8, ("in the row of the number: " + k, where, g)
        if g["scope"] and g["stamp"]:
            assert g["scope"]["r"] <= g["stamp"]["l"] + 0.5, ("the model list before the stamp", where, g)
        if g["stamp"] and g["ctl"]:
            assert g["stamp"]["r"] <= g["ctl"]["l"] + 0.5, ("the stamp before the chart word", where, g)
            assert abs((g["stamp"]["t"] + g["stamp"]["b"]) / 2 - (g["ctl"]["t"] + g["ctl"]["b"]) / 2) <= 8, ("the stamp level with the chart word", where, g)
        if g["limit"]:
            assert g["limit"]["t"] >= max(b["b"] for b in (fact, g["scope"], g["stamp"], g["ctl"]) if b) - 0.5, ("the note of the limit under the row of the number", where, g)
            assert abs(g["limit"]["l"] - bars["l"]) <= 0.5, ("the note of the limit at the left edge of the bars", where, g)
    else:
        # Narrow: one column, the bars first over the whole width, then the number, the model list, the line of the stamp and the chart word, the note of the limit.
        assert abs(bars["r"] - p["r"]) <= 1 and abs(bars["l"] - p["l"]) <= 1, ("the bars take the whole width", where, bars, p)
        assert abs(fact["r"] - bars["r"]) <= 1, ("the number's row takes the width of the bars", where, fact, bars)
        order = [bars] + [g[k] for k in ("fact", "scope") if g[k]]
        for above, below in zip(order, order[1:]):
            assert above["b"] <= below["t"] + 0.5, ("in a column, the bars first", where, g)
        line = [g[k] for k in ("stamp", "ctl") if g[k]]
        assert all(b["t"] >= order[-1]["b"] - 0.5 for b in line), ("the stamp line under the rest", where, g)
        if g["limit"]:
            assert g["limit"]["t"] >= max([order[-1]["b"]] + [b["b"] for b in line]) - 0.5, ("the note of the limit last", where, g)


def test_the_top_of_the_plate_keeps_the_press_of_its_bubble_its_counts_its_hover_its_padding_and_the_focus_key_of_a_step():
    """The top of the plate: the bubble at the limit keeps its press (its rise fills backwards, not both: a fill held after the end would stand over :active);
    "N more" of the bubble counts the accounts with no name as the note's does; a text action in the card's section is its word, not the whole line; the model list keeps the kit's
    quiet hover; the plate takes its right padding back in one column (521-639px) and only the chart word is drawn out to the edge; the focus key of a step follows its moment."""
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    assert '".wt-in>.wt-limit{animation:wt-rise var(--dur-4) var(--ease) backwards}"' in source
    bubble = source.split("function limitBubble(", 1)[1].split("\n    }\n", 1)[0]
    assert "var more = named.length - shown.length + (shown.length ? nameless : 0);" in bubble and "var nameless = spent.length - named.length;" in bubble
    assert '".reserve>.cover-link{flex:0 0 auto}"' in source and ".reserve>.wt-foot" not in source
    assert '".hover-quiet:hover{color:var(--text-primary);background:var(--tint-2)}"' in source and "btn-compact hover-quiet wt-scope-opt" in source
    # (The plate has no padding of its own now - it stands bare on the card - so there is no right padding to take back in one column, and the chart
    # word is not drawn out to the edge to make up for it: both rules are gone.)
    assert "padding-right:var(--space-2h)" not in source and ".l-chart{margin-right:calc(" not in source
    assert "'rs:' + g.key + '@' + step.iso" in source and ".tl-model-opts: its gap" not in source


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_top_of_the_plate_is_the_bubble_of_the_fact_the_model_list_and_the_bubble_of_the_limit_in_one_grid_and_a_column_under_640px(fixture_file, engine):
    """capsule: window tabs (part 3). Codex, Claude (the model list; one at the limit among them), Cursor (the budget) and Antigravity, at 1280, 1020, 860, 678, 640, 639 and 360: the bars over the
    whole width and, under them, the row of the number - the number (44px, thin) at the left with no bubble, at its right the window's name over "of N accounts" one under the other, then the
    model list, the stamp and the chart word - and the note of the limit under that row; the left edge of the number is the left edge of the bars (0.5px); under 640px a column, the bars
    first. No sideways scroll, nothing cut, all inside the plate. Where there is no model list or no limit their place is not left empty."""
    from test_bars import _mount_layout_frame, _let_go_the_pick
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    cases = [("claude_week", _claude_week_at_limit(views), "win:claude:604800"), ("claude", views["claude"], "win:claude:604800"),
             ("claude_many", views["claude_many"], None), ("codex_states", views["codex_states"], None), ("codex", views["codex"], None),
             ("cursor_budget", views["cursor_budget"], None), ("cursor_mixed", views["cursor_mixed"], None), ("agy", views["agy"], None)]
    seen = {"scope": 0, "limit": 0, "plain": 0}
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for name, view, focus in cases:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                frame = _mount_layout_frame(page, view, 1280)
                if focus:
                    _pick_window(frame, focus)
                # The top of the plate is the plate's own (the bubbles of the fact and the limit, the stamp, the chart word): the pick of the top account is let go after the mount and after the window pick (picked, they stand in its card).
                _let_go_the_pick(frame)
                for width in (1280, 1020, 860, 678, 640, 639, 360):
                    page.locator("iframe").evaluate("(e, width) => { e.style.width = width + 'px'; }", width)
                    frame.wait_for_function("(width) => innerWidth === width", arg=width)
                    page.wait_for_timeout(100)
                    g = frame.evaluate(TOP_GEOMETRY)
                    _top_check(g, width, (engine, name, width))
                    seen["scope" if g["scope"] else "limit" if g["limit"] else "plain"] += 1
                    seen["limit"] += 1 if g["scope"] and g["limit"] else 0
                assert errors == [], errors
                page.close()
        finally:
            browser.close()
    assert seen["scope"] >= 7 and seen["limit"] >= 21 and seen["plain"] >= 7, seen


BAR_OPACITY = "() => [...document.querySelectorAll('.wt-plate .bars .bar')].map((b) => [b.getAttribute('data-account'), b.className, +getComputedStyle(b).opacity])"
PLATE_DIM = "() => document.querySelector('.wt-plate').getAttribute('data-dim')"
# (the rise that has ended keeps its last frame and is still listed: only one that runs is a movement)
RISING = "() => document.getAnimations().filter((a) => a.animationName === 'wt-rise' && (a.playState === 'running' || a.playState === 'pending')).map((a) => a.effect.target.className)"


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_bubble_of_the_limit_lights_the_bars_of_its_accounts_and_n_more_opens_the_list_and_only_the_readers_pick_moves_the_top(fixture_file, engine):
    """capsule: window tabs (part 3). Pointed at, or focused by the keyboard, the bubble of the limit steps back every bar but those of its accounts (the plate wears data-dim=group; the bars at the
    limit stay at full ink); the pointer leaving gives them back. "N more" in the bubble opens the account list and lights nothing. The keyboard stays on the bubble through the redraw of fresh data. The
    bubble of the fact and the bubble of the limit rise in for the reader's own pick of a window (wt-rise, transform and opacity), never for a redraw nobody asked for, and not under reduced motion."""
    from test_bars import _mount_layout_frame, _let_go_the_pick
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            for key, width in (("claude_many", 1280), ("claude_many", 360), ("codex_states", 678)):
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                frame = _mount_layout_frame(page, views[key], width)
                # The bubble of the limit stands on the plate itself: the pick of the top account is let go first (picked, it stands in the folded card).
                _let_go_the_pick(frame)
                where = (engine, key, width)
                assert frame.evaluate(PLATE_DIM) is None, where
                bars = frame.evaluate(BAR_OPACITY)
                spent = [b[0] for b in bars if " spent" in b[1]]
                assert spent and all(b[2] == 1 for b in bars), (where, bars)
                # The pointer: every bar but theirs steps back.
                frame.hover(".wt-plate .wt-limit .wt-limit-head")
                page.wait_for_timeout(250)
                assert frame.evaluate(PLATE_DIM) == "group", where
                dimmed = frame.evaluate(BAR_OPACITY)
                assert all(b[2] == 1 for b in dimmed if b[0] in spent), ("theirs stay at full ink", where, dimmed)
                assert all(b[2] < 1 for b in dimmed if b[0] not in spent), ("the others step back", where, dimmed)
                page.mouse.move(2, 2)
                page.wait_for_timeout(250)
                assert frame.evaluate(PLATE_DIM) is None and all(b[2] == 1 for b in frame.evaluate(BAR_OPACITY)), where
                # The keyboard: the bubble takes it (its ring shows), lights the same bars, and keeps it through the redraw of fresh data.
                frame.focus(".wt-plate .wt-limit")
                page.keyboard.press("Shift+Tab")
                page.keyboard.press("Tab")
                page.wait_for_timeout(250)
                assert frame.evaluate("() => document.activeElement.classList.contains('wt-limit') && document.activeElement.matches(':focus-visible')"), where
                assert frame.evaluate(PLATE_DIM) == "group", where
                frame.evaluate(REDRAW)
                page.wait_for_timeout(500)
                assert frame.evaluate("() => (document.activeElement.getAttribute('data-focus') || '').startsWith('atlimit:')"), ("the keyboard stays", where)
                assert frame.evaluate("() => document.querySelectorAll('.wt-in').length") == 0 and frame.evaluate(RISING) == [], ("a redraw nobody asked for moves nothing", where)
                # "N more" opens the list of accounts and lights nothing.
                if key == "claude_many":
                    page.mouse.move(2, 2)
                    frame.click(".wt-plate .wt-limit .wt-foot-more")
                    page.wait_for_timeout(300)
                    # The word over the bars (data-focus=accounts) is aria-pressed now; the old aria-expanded bottom toggle is gone.
                    assert frame.evaluate("() => document.querySelector('[data-focus=\"accounts\"]').getAttribute('aria-pressed')") == "true", where
                    # (the pointer is still over the place of the bubble, so the plate may wear the group it stands on; the press itself lights nothing more:
                    # with the pointer gone the plate is plain)
                    page.mouse.move(2, 2)
                    page.wait_for_timeout(250)
                    assert frame.evaluate(PLATE_DIM) is None, ("the press lights nothing", where)
                page.close()
            # The reader's pick of another window brings the bubbles in; a poll after it does not; reduced motion has none of it.
            for reduce in (False, True):
                page = browser.new_page(viewport={"width": 1400, "height": 900}, reduced_motion="reduce" if reduce else "no-preference")
                frame = _mount_layout_frame(page, _claude_week_at_limit(views), 1280)
                _pick_window(frame, "win:claude:18000")
                page.wait_for_timeout(1200)
                assert frame.evaluate(RISING) == [], "the rise of the first pick has ended"
                frame.click('[data-focus="win:claude:604800"]')
                # The new window picks its top account at once, so the bubble of the limit stands in the folded card (cardTake): it is there, though not visible.
                frame.wait_for_selector(".wt-plate .wt-limit", state="attached")
                rising = frame.evaluate(RISING)
                if reduce:
                    assert rising == [] and frame.evaluate("() => getComputedStyle(document.querySelector('.wt-fact')).animationName") == "none", (engine, rising)
                else:
                    assert any("wt-fact" in c for c in rising) and any("wt-limit" in c for c in rising), (engine, rising)
                    assert all("opacity" in k and "transform" in k for k in frame.evaluate(
                        "() => document.getAnimations().filter((a) => a.animationName === 'wt-rise').map((a) => a.effect.getKeyframes().flatMap((f) => Object.keys(f)).join(','))")), engine
                page.wait_for_timeout(1200)
                frame.evaluate(REDRAW)
                page.wait_for_timeout(600)
                assert frame.evaluate(RISING) == [] and frame.evaluate("() => document.querySelectorAll('.wt-in').length") == 0, ("the poll moves nothing", engine, reduce)
                page.close()
        finally:
            browser.close()


# The pick of a family brings the ladder down after the rings (kit: rail, motion). The first answer holds the reserve of the families the head shows (the
# plate of a family is drawn from it at once, as the head's pick draws it before the poll asks for it: reserveGroups), so the page here is given Claude's and Codex's
# limits in one answer; Codex has three windows (5-hour, weekly, 30-day), three accounts in each, each account resetting at its own time.
def _two_families_served(tmp_path, served, family, accounts=("gptpro3", "gptpro5", "gptpro9")):
    codex = _family(tmp_path, "codex", lambda now, i: [
        _row(now, "codex:five", "codex 5 hour", .3 + .1 * i, FIVE_H) | {"resets_at": qs.iso(now + 3600 * (i + 1))},
        _row(now, "codex:week", "codex weekly", .2 + .1 * i, WEEK) | {"resets_at": qs.iso(now + 86400 * (i + 1))},
        _row(now, "codex:month", "codex 30 day", .1 + .1 * i, MONTH) | {"resets_at": qs.iso(now + 86400 * (4 + 6 * i))}],
        accounts=accounts, name="codex_three" if len(accounts) == 3 else "codex_" + str(len(accounts)))
    both = json.loads(json.dumps(served[family]))
    both["reserve"]["summary"]["groups"] = both["reserve"]["summary"]["groups"] + codex["reserve"]["summary"]["groups"]
    return {hid: both for hid in served}


# Frame by frame, from the click: every animation by name | class (its delays, the properties its keyframes touch, when it ends on the page's clock),
# and how visible the rings, the head of the ladder and its steps are. The ends are read off the animations themselves (startTime + endTime).
FAMILY_LADDER_REC = """(ms) => {
  const rec = {start: performance.now(), clickAt: null, frames: [], seen: {}, done: false};
  window.__fam = rec;
  document.addEventListener('click', () => { if (rec.clickAt === null) rec.clickAt = performance.now(); }, true);
  const skip = ['offset', 'computedOffset', 'easing', 'composite'];
  const props = (a) => { try { return [...new Set(a.effect.getKeyframes().flatMap((k) => Object.keys(k)).filter((k) => !skip.includes(k)))].sort().join('+'); } catch (e) { return ''; } };
  const shown = (n) => n.getClientRects().length > 0;
  const tick = () => {
    const now = performance.now();
    let running = 0;
    for (const a of document.getAnimations()) {
      const n = a.effect && a.effect.target, name = a.animationName || '';
      if (!n || !name) continue;
      const cls = String(n.className.baseVal === undefined ? n.className : n.className.baseVal).split(' ')
        .filter((c) => ['rail-step', 'rail-more', 'rail-ladder-title', 'rail-ladder-out', 'tab-mark-rail', 'rail-bot', 'rail-lead', 'tab-mark-ring'].includes(c)).join('.');
      const key = name + '|' + cls, timing = a.effect.getTiming(), over = a.effect.getComputedTiming().endTime;
      const at = rec.seen[key] || (rec.seen[key] = {n: 0, delays: [], props: props(a), end: 0, last: 0});
      at.n += 1;
      at.last = now;
      if (!at.delays.includes(timing.delay)) at.delays.push(timing.delay);
      // (an endless one - the soft pulse of the stand-ins of the chart, skel-pulse - has no end to wait for; it is gone when the line has met, which the last frame's `running` tells)
      if (typeof a.startTime === 'number' && Number.isFinite(over)) at.end = Math.max(at.end, a.startTime + over);
      if (a.playState === 'running' || a.playState === 'pending') running += 1;
    }
    const alpha = (n) => +getComputedStyle(n).opacity;
    rec.frames.push({t: now, running,
      rings: [...document.querySelectorAll('.rail .tab-mark-rail')].map(alpha),
      head: [...document.querySelectorAll('.rail-ladder-title')].filter(shown).map(alpha),
      steps: [...document.querySelectorAll('.rail-step')].filter(shown).map(alpha),
      more: [...document.querySelectorAll('.rail-more')].filter(shown).map(alpha),
      moving: !!document.querySelector('.rail.rail-moving')});
    if (now - rec.start < ms) requestAnimationFrame(tick); else rec.done = true;
  };
  requestAnimationFrame(tick);
}"""


def _rec_family(frame, action, ms=2200):
    frame.evaluate(FAMILY_LADDER_REC, ms)
    action()
    frame.wait_for_function("() => window.__fam.done", timeout=ms + 4000)
    return frame.evaluate("window.__fam")


def _family_ends(rec):
    """When the last animation of the pick ends, from the click, in ms: all of them, the rail's (rail-*), the ring's sweep, the ladder's (head and steps)."""
    click = rec["clickAt"]
    ends = {"all": 0, "rail": 0, "sweep": 0, "ladder": 0}
    for key, at in rec["seen"].items():
        name, cls = key.split("|")
        span = at["end"] - click if at["end"] else 0
        ends["all"] = max(ends["all"], span)
        if name.startswith("rail-"):
            ends["rail"] = max(ends["rail"], span)
        if name.startswith("ring-sweep"):
            ends["sweep"] = max(ends["sweep"], span)
        if name == "rail-drop" and ("rail-step" in cls or "rail-ladder-title" in cls or "rail-more" in cls):
            ends["ladder"] = max(ends["ladder"], span)
    return {k: round(v) for k, v in ends.items()}


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_pick_of_a_family_brings_the_head_and_the_steps_of_the_ladder_down_after_the_last_ring_by_transform_and_opacity_alone(fixture_file, engine, tmp_path):
    """kit: rail, motion. The pick of a family (Claude -> Codex, 1280, the first answer holding both families): the head of the ladder and its steps come down
    by the rail's own keyframes (rail-drop), numbered on from the rings (--i: the rings 0..2, the head 3, the steps 4..), so each starts a row (--rail-row-step,
    70 ms) after the one before and the first of them after the last ring; the foot of the rail last. They touch transform and opacity alone, are unseen
    until their turn (the head not seen before the last ring has started), and when it is over no animation is left and everything stands at full ink; the
    last animation of the pick ends within RAIL_MOTION_MS (+60 ms) and the ladder's own within the span of the rail (the ends, from the click, are printed:
    LADDER-END). The poll and 'Refresh' bring none of it; under reduced motion the head and the steps stand at once."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    source = Path(__file__).with_name("widget.js").read_text()
    span = int(re.search(r"var RAIL_MOTION_MS = (\d+);", source).group(1))
    step = 70   # --rail-row-step; the first row comes down at --rail-row-at (380 ms)
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            served, family = _ladder_served(views, "claude")
            served = _two_families_served(tmp_path, served, family)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_ladder_frame(page, served, family, 1280)
            _settle_family(frame, family)
            # 1. The pick of Codex: three rings, the head, the steps.
            rec = _rec_family(frame, lambda: frame.click('[data-focus="harness:codex"]'))
            seen, frames, click = rec["seen"], rec["frames"], rec["clickAt"]
            rings = seen.get("rail-drop|tab-mark-rail")
            head = seen.get("rail-drop|rail-ladder-title")
            steps = {k: v for k, v in seen.items() if k.startswith("rail-drop|rail-step")}
            assert rings and head and steps, ("the rings, the head and the steps of the pick come down by rail-drop", sorted(seen))
            ring_delays = sorted(round(d) for d in rings["delays"])
            step_delays = sorted({round(d) for v in steps.values() for d in v["delays"]})
            assert len(ring_delays) == 3 and len(step_delays) >= 2, ("Codex: three rings and the steps of a ladder", ring_delays, step_delays)
            head_delay = [round(d) for d in head["delays"]]
            assert head_delay == [ring_delays[-1] + step], ("the head comes down a row after the last ring", head_delay, ring_delays)
            assert step_delays[0] == head_delay[0] + step and all(b - a == step for a, b in zip(step_delays, step_delays[1:])), \
                ("the steps follow the head, a row apart, numbered on from the rings", head_delay, step_delays)
            assert all(b - a == step for a, b in zip(ring_delays, ring_delays[1:])), ring_delays
            for key in [k for k in seen if k.startswith("rail-drop|")]:
                assert set(seen[key]["props"].split("+")) <= {"opacity", "transform"}, ("transform and opacity alone", key, seen[key]["props"])
            # the others of the ladder (the window pick's own) are not here
            assert not [k for k in seen if k.startswith("ladder-")], ("the pick of a family uses the rail's keyframes, not the window's", sorted(seen))
            # seen over time: the head is unseen until the last ring has started; the steps until the head has; at the first frame none of them is seen
            first = next(f for f in frames if f["t"] >= click)
            assert all(o < 0.05 for o in first["head"] + first["steps"]), ("the head and the steps are unseen when the pick is made", first)
            start = lambda key: next((f["t"] for f in frames if f["t"] >= click and key(f) > 0.05), None)
            last_ring, head_in = start(lambda f: f["rings"][2] if len(f["rings"]) > 2 else 0), start(lambda f: f["head"][0] if f["head"] else 0)
            step_in = start(lambda f: f["steps"][0] if f["steps"] else 0)
            assert None not in (last_ring, head_in, step_in) and last_ring <= head_in <= step_in, ("the ladder starts after the last ring", last_ring, head_in, step_in)
            # the span: nothing runs after RAIL_MOTION_MS (+60); the ladder's own animations end before the foot's, which is last
            ends = _family_ends(rec)
            assert 0 < ends["ladder"] <= ends["rail"] <= span + 60, ("the movement of the pick ends within RAIL_MOTION_MS", ends, span)
            assert not [k for k in seen if "rail-bot" in k], ("the rail has no foot to come down last: the ladder ends it", sorted(seen))
            # over: no animation left, the head and the steps at full ink, the rings too
            last = frames[-1]
            assert last["running"] == 0 and all(o == 1 for o in last["rings"] + last["head"] + last["steps"]), ("nothing runs and all stands at full ink", last)
            # 2. The poll and Refresh: the numbers change in place; the ladder moves nothing of its own.
            for action in (lambda: frame.evaluate(REDRAW), lambda: frame.click('[data-focus="refresh"]')):
                rec = _rec_family(frame, action, 900)
                assert not [k for k in rec["seen"] if k.startswith("rail-drop|") or k.startswith("ladder-")], ("a poll or a refresh moves nothing", sorted(rec["seen"]))
                # (the rail of the pick keeps its class until the next drawing, so the poll's own drawing is read at the last frame)
                assert not rec["frames"][-1]["moving"], "and the rail the poll draws does not carry the movement of a pick"
            # 3. Back to Claude: its ladder (two rings) comes down the same way; the end of its movement, from the click.
            frame.wait_for_selector('[data-focus="refresh"]:not([disabled])')
            rec = _rec_family(frame, lambda: frame.click('[data-focus="harness:claude"]'))
            seen, frames = rec["seen"], rec["frames"]
            ring_delays = sorted(round(d) for d in seen["rail-drop|tab-mark-rail"]["delays"])
            head_delay = [round(d) for d in seen["rail-drop|rail-ladder-title"]["delays"]]
            step_delays = sorted({round(d) for k, v in seen.items() if k.startswith("rail-drop|rail-step") for d in v["delays"]})
            assert len(ring_delays) == 2 and head_delay == [ring_delays[-1] + step] and step_delays and step_delays[0] == head_delay[0] + step, (ring_delays, head_delay, step_delays)
            ends = _family_ends(rec)
            assert 0 < ends["ladder"] <= ends["rail"] <= span + 60, ends
            assert frames[-1]["running"] == 0 and all(o == 1 for o in frames[-1]["rings"] + frames[-1]["head"] + frames[-1]["steps"])
            assert errors == [], errors
            page.close()
            # 4. Reduced motion: the head and the steps stand at once, at full ink, and no animation of the rail's runs.
            page = browser.new_page(viewport={"width": 1400, "height": 900}, reduced_motion="reduce")
            frame = _mount_ladder_frame(page, served, family, 1280)
            page.wait_for_timeout(600)
            rec = _rec_family(frame, lambda: frame.click('[data-focus="harness:codex"]'), 900)
            assert not [k for k in rec["seen"] if k.startswith("rail-drop|") or k.startswith("ladder-")], ("reduced motion: no animation of the rail's", sorted(rec["seen"]))
            shown = [f for f in rec["frames"] if f["t"] >= rec["clickAt"] and f["head"]]
            assert shown and all(o == 1 for f in shown for o in f["head"] + f["steps"] + f["rings"]), ("the head and the steps stand at once at full ink", shown[:2])
            assert frame.evaluate("() => document.querySelectorAll('.rail-step').length") >= 1
            page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_the_word_that_counts_what_did_not_fit_comes_down_last_after_the_last_step_that_stands_and_the_foot_is_not_moved_by_it(fixture_file, engine, tmp_path):
    """kit: rail, motion. The pick of a family whose window has more resets than the ladder draws (Codex of six accounts, each at its own time: four steps and 'N more'):
    the word comes down by the rail's own keyframes (rail-drop), numbered on after the last step that stands (a row, 70 ms, after it), unseen until that step has started
    and not before it; the foot of the rail is no earlier than the word and still last (--rail-rows does not grow for the word); transform and opacity alone; the whole
    pick ends within RAIL_MOTION_MS (+60 ms); the word at full ink when it is over. The poll and Refresh bring none of it; under reduced motion the word stands at once."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    source = Path(__file__).with_name("widget.js").read_text()
    span = int(re.search(r"var RAIL_MOTION_MS = (\d+);", source).group(1))
    step = 70
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            served, family = _ladder_served(views, "claude")
            served = _two_families_served(tmp_path, served, family, accounts=("a1", "a2", "a3", "a4", "a5", "a6"))
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_ladder_frame(page, served, family, 1280)
            _settle_family(frame, family)
            rec = _rec_family(frame, lambda: frame.click('[data-focus="harness:codex"]'))
            seen, frames, click = rec["seen"], rec["frames"], rec["clickAt"]
            g = frame.evaluate(LADDER_GEOM)
            assert g["more"] and len(g["steps"]) >= 1 and g["more"]["box"]["t"] >= g["steps"][-1]["box"]["b"] - 0.5, ("the word that counts stands after the last step", g["more"], len(g["steps"]))
            step_delays = sorted({round(d) for k, v in seen.items() if k.startswith("rail-drop|rail-step") for d in v["delays"]})
            more = seen.get("rail-drop|rail-more")
            assert more and len(more["delays"]) == 1 and step_delays, ("the word that counts comes down by rail-drop", sorted(seen))
            more_delay = round(more["delays"][0])
            assert more_delay == step_delays[-1] + step, ("a row after the last step that stands", more_delay, step_delays)
            assert len(step_delays) == len(g["steps"]), ("one row for each step that stands", step_delays, len(g["steps"]))
            assert set(more["props"].split("+")) <= {"opacity", "transform"}, ("transform and opacity alone", more["props"])
            assert not [k for k in seen if "rail-bot" in k], ("no foot in the rail: the word is the last row, and its rows did not grow", sorted(seen))
            assert not [k for k in seen if k.startswith("ladder-")], sorted(seen)
            # seen over time: the word is unseen until the last step has started
            start = lambda key: next((f["t"] for f in frames if f["t"] >= click and key(f) > 0.05), None)
            last_step_in = start(lambda f: f["steps"][-1] if f["steps"] else 0)
            more_in = start(lambda f: f["more"][0] if f["more"] else 0)
            assert None not in (last_step_in, more_in) and last_step_in <= more_in, ("the word comes after the last step", last_step_in, more_in)
            ends = _family_ends(rec)
            assert 0 < ends["ladder"] <= ends["rail"] <= span + 60, ("the movement of the pick ends within RAIL_MOTION_MS", ends, span)
            last = frames[-1]
            assert last["running"] == 0 and all(o == 1 for o in last["head"] + last["steps"] + last["more"]), ("nothing runs and the word stands at full ink", last)
            # the poll and Refresh: nothing moves, the word included
            for action in (lambda: frame.evaluate(REDRAW), lambda: frame.click('[data-focus="refresh"]')):
                quiet = _rec_family(frame, action, 900)
                assert not [k for k in quiet["seen"] if k.startswith("rail-drop|") or k.startswith("ladder-")], ("a poll or a refresh moves nothing", sorted(quiet["seen"]))
            assert errors == [], errors
            page.close()
            # reduced motion: the word stands at once
            page = browser.new_page(viewport={"width": 1400, "height": 900}, reduced_motion="reduce")
            frame = _mount_ladder_frame(page, served, family, 1280)
            page.wait_for_timeout(600)
            rec = _rec_family(frame, lambda: frame.click('[data-focus="harness:codex"]'), 900)
            assert not [k for k in rec["seen"] if k.startswith("rail-drop|") or k.startswith("ladder-")], ("reduced motion: no animation", sorted(rec["seen"]))
            shown = [f for f in rec["frames"] if f["t"] >= rec["clickAt"] and f["more"]]
            assert shown and all(o == 1 for f in shown for o in f["more"]), ("the word stands at once at full ink", shown[:2])
            page.close()
        finally:
            browser.close()


# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# A step of the ladder of resets is a button; pressed, a step of one account is the pick of that account (kit: bars, .sel with .bar-on), a step of several accounts only lights their bars
# (data-step-lit; the others step back, as under the pointer) and opens none.
# What the page shows of the steps (their role, press, cursor and the ink of the time) and of the row of the bars (each bar's width, its words, the place of the row).
STEP_STATE = """() => {
  const q = (s) => [...document.querySelectorAll(s)];
  const strip = document.querySelector('.bars'), room = strip.getBoundingClientRect();
  const probe = (c) => { const i = document.createElement('i'); i.style.color = c; document.body.appendChild(i); const v = getComputedStyle(i).color; i.remove(); return v; };
  const bars = q('.bars > .bar').map((b) => { const r = b.getBoundingClientRect(), w = b.querySelector(':scope > .bar-i');
    return {account: b.getAttribute('data-account'), on: b.classList.contains('bar-on'), width: r.width, left: r.left - room.left, right: room.right - r.right,
      words: w ? parseFloat(getComputedStyle(w).opacity) : null, sel: b.classList.contains('sel'), pressed: b.getAttribute('aria-pressed'),
      lit: b.hasAttribute('data-step-lit'), opacity: parseFloat(getComputedStyle(b).opacity)}; });
  const steps = q('.rail-ladder:not(.rail-ladder-out) .rail-step[data-accounts]').map((s) => ({role: s.getAttribute('role'), pressed: s.getAttribute('aria-pressed'), tab: s.getAttribute('tabindex'),
    cursor: getComputedStyle(s).cursor, time: getComputedStyle(s.querySelector('.rail-step-time')).color, accounts: JSON.parse(s.getAttribute('data-accounts')), focus: s.getAttribute('data-focus'),
    height: s.getBoundingClientRect().height, out: s.classList.contains('rail-fit-off')}));
  return {rest: parseFloat(strip.style.getPropertyValue('--bar-rest')), scroll: strip.scrollWidth, client: strip.clientWidth, at: strip.scrollLeft, bars, steps, accent: probe('var(--accent)'), ink: probe('var(--text-primary)'),
    active: document.activeElement.getAttribute && document.activeElement.getAttribute('data-focus'), scrollY: scrollY, plate: window.__plate === document.querySelector('.wt-plate'),
    marked: !!document.querySelector('.bars[data-step]')};
}"""
OPEN_BY = 172   # --bar-open: the room of the words a bar opens by (kit: bars)


def _shut_or_open(state, accounts, tolerance=1.5):
    """Every bar of `accounts` (the one account of a step of one: its pick) is open (rest + 172 wide, its words shown, .bar-on), every other bar is shut (rest wide, no words, no .bar-on), and
    nothing is lit; none of the shut ones is wider than at rest."""
    rest = state["rest"]
    assert not state["marked"] and not any(bar["lit"] for bar in state["bars"]), ("a step of one account (or none) lights nothing", state["bars"])
    for bar in state["bars"]:
        if bar["account"] in accounts:
            # (the bar of a step of one account is the pick of that account: .sel, open at all times as the pointer's, with .bar-on)
            assert (bar["on"] or bar["sel"]) and abs(bar["width"] - (rest + OPEN_BY)) <= tolerance and bar["words"] == 1, ("a bar of the step is open", bar, rest)
        elif bar["sel"]:
            # The pick stays open on every pointer (the rule of :hover, kept by .sel); the top account is picked as the window opens, so it is the one open bar of the rest.
            assert abs(bar["width"] - (rest + OPEN_BY)) <= tolerance and bar["words"] == 1, ("the picked bar is open", bar, rest)
        else:
            assert not bar["on"] and abs(bar["width"] - rest) <= tolerance and not bar["words"], ("any other bar is shut", bar, rest)


def _lit_not_open(state, accounts, before, tolerance=1.5):
    """A step of several accounts is pressed: the bars of `accounts` are lit (data-step-lit, full ink) and none of them is opened or wears .bar-on (the picked one stays open, as the pick is);
    every other bar is shut and steps back (--dim-others, .35) but the picked one; the row is as wide as it was before the press."""
    rest = state["rest"]
    assert state["marked"] and state["scroll"] == before["scroll"] and state["client"] == before["client"], ("the press does not widen the row", state["scroll"], before["scroll"])
    for bar in state["bars"]:
        assert not bar["on"] or bar["sel"], ("no bar is held open by the step", bar)
        if bar["account"] in accounts:
            assert bar["lit"] and abs(bar["opacity"] - 1) < 0.02, ("a bar of the step is lit", bar)
            assert bar["sel"] or abs(bar["width"] - rest) <= tolerance and not bar["words"], ("and not opened", bar, rest)
        elif bar["sel"]:
            assert not bar["lit"] and abs(bar["opacity"] - 1) < 0.02 and abs(bar["width"] - (rest + OPEN_BY)) <= tolerance, ("the picked bar stays open and plain", bar)
        else:
            assert not bar["lit"] and abs(bar["opacity"] - 0.35) < 0.02 and abs(bar["width"] - rest) <= tolerance and not bar["words"], ("any other bar steps back, shut", bar, rest)


def _step_served(views, mutate):
    return _ladder_served(views, "claude_many", mutate)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_a_press_on_a_step_lights_or_picks_its_accounts_the_second_press_lets_go_and_another_step_hands_them_over(fixture_file, engine):
    """kit: rail, the ladder. A step is a button (role, aria-pressed, the pointer cursor, as high as before). The press of a step of one account opens its bar as the pointer does and the
    time of the step stands in the accent; the pointer leaving does not shut it. The second press shuts it. A step that glues several accounts lights their bars (the others step
    back) and opens none: the row is as wide as it was. Another step hands the bars over. The press on an open bar selects its account as before. Nothing is drawn again by a press."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            served, family = _step_served(views, _glued_resets)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_ladder_frame(page, served, family, 1280)
            page.wait_for_timeout(1300)
            frame.evaluate("() => { window.__plate = document.querySelector('.wt-plate'); }")
            rest = frame.evaluate(STEP_STATE)
            steps = [s for s in rest["steps"] if not s["out"]]
            assert len(steps) == 2 and [len(s["accounts"]) for s in steps] == [9, 1], ("the glued step of nine accounts and the one of its own", steps)
            for s in steps:
                assert s["role"] == "button" and s["pressed"] == "false" and s["tab"] == "0" and s["cursor"] == "pointer" and s["height"] >= 40, ("a step is a button", s)
                assert s["time"] == rest["ink"] and s["time"] != rest["accent"], ("at rest the time is the ink", s)
            _shut_or_open(rest, [])
            one, nine = steps[1]["accounts"], steps[0]["accounts"]
            # press the single step: its bar opens, its time is the accent; the pointer leaves and the bar stays open
            frame.locator(LADDER_STEPS).nth(1).click()
            page.wait_for_timeout(700)
            on = frame.evaluate(STEP_STATE)
            _shut_or_open(on, one)
            assert [s["pressed"] for s in on["steps"]] == ["false", "true"] and on["steps"][1]["time"] == on["accent"] and on["steps"][0]["time"] == on["ink"], on["steps"]
            assert on["plate"], "the press draws nothing again: the plate is the same node"
            page.mouse.move(1390, 890)
            page.wait_for_timeout(700)
            _shut_or_open(frame.evaluate(STEP_STATE), one)
            # the second press shuts it
            frame.locator(LADDER_STEPS).nth(1).click()
            page.mouse.move(1390, 890)
            page.wait_for_timeout(700)
            off = frame.evaluate(STEP_STATE)
            _shut_or_open(off, [])
            assert [s["pressed"] for s in off["steps"]] == ["false", "false"] and off["steps"][1]["time"] == off["ink"], off["steps"]
            # the glued step lights all nine and opens none: the row is as wide as it was, the others step back, and the light stays when the pointer has gone
            frame.locator(LADDER_STEPS).nth(0).click()
            page.mouse.move(1390, 890)
            page.wait_for_timeout(900)
            many = frame.evaluate(STEP_STATE)
            _lit_not_open(many, nine, rest)
            assert len([b for b in many["bars"] if b["lit"]]) == 9 and [s["pressed"] for s in many["steps"]] == ["true", "false"] and many["steps"][0]["time"] == many["accent"], many["steps"]
            # another step hands the light over (a step of one account is that account's pick: nothing is lit then)
            frame.locator(LADDER_STEPS).nth(1).click()
            page.mouse.move(1390, 890)
            page.wait_for_timeout(900)
            handed = frame.evaluate(STEP_STATE)
            _shut_or_open(handed, one)
            assert [s["pressed"] for s in handed["steps"]] == ["false", "true"], handed["steps"]
            # a step of one account is the pick of that account: its bar is already selected and open (.sel, aria-pressed) and the step is pressed
            picked = [b for b in handed["bars"] if b["sel"]]
            assert len(picked) == 1 and picked[0]["account"] == one[0] and picked[0]["pressed"] == "true", ("the account is selected by the step", picked)
            # the press on that selected bar lets it go - the second press - and the step with it; no pick comes back by itself in this window
            frame.locator(".bars > .bar.sel").click()
            page.mouse.move(1390, 890)
            page.wait_for_timeout(900)
            chosen = frame.evaluate(STEP_STATE)
            assert not [b for b in chosen["bars"] if b["sel"]], ("the pick is let go", chosen["bars"])
            _shut_or_open(chosen, [])
            assert [s["pressed"] for s in chosen["steps"]] == ["false", "false"], chosen["steps"]
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_a_step_of_several_accounts_is_let_go_by_every_pick_by_escape_and_by_a_fresh_answer_without_it_but_not_by_a_failed_one(fixture_file, engine):
    """kit: rail, the ladder. The glued step lights its bars and opens none (only the picked bar is open, and only it is in the accent). A pick of a bar, Enter on a bar and
    Escape (with a pick, and with none) let it go: no pressed step, no light. The draw of the poll keeps it. A fresh answer that has no such step lets it go for good (it does
    not come back with a step of the same moment); an answer that failed says nothing of it, and it stays."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            served, family = _step_served(views, _glued_resets)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_ladder_frame(page, served, family, 1280)
            page.wait_for_timeout(1300)
            nine = [s for s in frame.evaluate(STEP_STATE)["steps"] if not s["out"]][0]["accounts"]
            assert len(nine) == 9

            def press():
                before = frame.evaluate(STEP_STATE)
                frame.locator(LADDER_STEPS).nth(0).click()
                page.mouse.move(1390, 890)
                page.wait_for_timeout(700)
                state = frame.evaluate(STEP_STATE)
                _lit_not_open(state, nine, before)
                assert [s["pressed"] for s in state["steps"]][0] == "true", state["steps"]
                return state

            def let_go(why):
                page.mouse.move(1390, 890)
                page.wait_for_timeout(700)
                state = frame.evaluate(STEP_STATE)
                assert not state["marked"] and not any(b["lit"] for b in state["bars"]) and not any(s["pressed"] == "true" for s in state["steps"]), (why, state["steps"])
                return state

            press()
            # a pick of a bar lets it go, and the picked bar is the one open bar
            frame.locator(".bars > .bar:not(.sel)").nth(3).click()
            picked = let_go("a pick of a bar")
            assert len([b for b in picked["bars"] if b["sel"]]) == 1 and len([b for b in picked["bars"] if b["width"] > picked["rest"] + 100]) == 1, picked["bars"]
            # Enter on a bar is the same pick
            press()
            frame.evaluate("() => document.querySelectorAll('.bars > .bar:not(.sel)')[5].focus()")
            page.keyboard.press("Enter")
            let_go("Enter on a bar")
            # Escape lets the pick go, and the step with it
            press()
            page.keyboard.press("Escape")
            let_go("Escape with a pick")
            assert not [b for b in frame.evaluate(STEP_STATE)["bars"] if b["sel"]], "the pick is let go"
            # no pick to let go: Escape lets go of the step
            press()
            page.keyboard.press("Escape")
            let_go("Escape with no pick")
            # the draw of the poll builds the tree anew and keeps the step lit
            press()
            before = frame.evaluate(STEP_STATE)
            frame.evaluate(REDRAW)
            page.wait_for_timeout(700)
            kept = frame.evaluate(STEP_STATE)
            _lit_not_open(kept, nine, before)
            assert [s["pressed"] for s in kept["steps"]][0] == "true", kept["steps"]
            # an answer that failed leaves it as it is
            frame.evaluate("() => { window.__fetch = window.fetch; window.fetch = async () => { throw new Error('offline'); }; }")
            frame.evaluate(REDRAW)
            page.wait_for_timeout(900)
            assert not frame.evaluate(STEP_STATE)["marked"], "(the failed read draws no plate)"
            frame.evaluate("() => { window.fetch = window.__fetch; }")
            frame.evaluate(REDRAW)
            page.wait_for_timeout(900)
            again = frame.evaluate(STEP_STATE)
            _lit_not_open(again, nine, before)
            assert [s["pressed"] for s in again["steps"]][0] == "true", ("the failed answer did not let the step go", again["steps"])
            # a fresh answer in which the step is another moment lets it go, and it does not come back when the moment does
            shift = "(d) => { const v = SERVED[FIRST]; for (const g of v.reserve.summary.groups) for (const b of g.bars || []) b.resets_at = new Date(Date.parse(b.resets_at) + d * 1000).toISOString(); }"
            frame.evaluate(shift, 90)
            frame.evaluate(REDRAW)
            page.wait_for_timeout(900)
            let_go("an answer without the step")
            frame.evaluate(shift, -90)
            frame.evaluate(REDRAW)
            page.wait_for_timeout(900)
            let_go("and the same moment again")
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_enter_and_space_press_a_step_and_it_and_its_bars_outlive_the_redraw_of_the_poll_with_the_keyboard_on_it(fixture_file, engine):
    """kit: rail, the ladder. Enter and Space press a step as they press a button (the page does not scroll by the space); the keyboard stays on it. The 30 s draw builds the
    tree anew: the new step is pressed, its bars stand open from the start, and the keyboard is on the new step (data-focus)."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            served, family = _step_served(views, _glued_resets)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_ladder_frame(page, served, family, 1280)
            page.wait_for_timeout(1300)
            page.mouse.move(1390, 890)
            frame.evaluate("(sel) => document.querySelectorAll(sel)[1].focus()", LADDER_STEPS)
            before = frame.evaluate(STEP_STATE)
            key = before["steps"][1]["focus"]
            one = before["steps"][1]["accounts"]
            scrolled = page.evaluate("() => scrollY")
            page.keyboard.press("Enter")
            page.wait_for_timeout(700)
            on = frame.evaluate(STEP_STATE)
            _shut_or_open(on, one)
            assert [s["pressed"] for s in on["steps"]] == ["false", "true"] and on["active"] == key, ("Enter presses it and the keyboard stays", on["steps"], on["active"])
            page.keyboard.press(" ")
            page.wait_for_timeout(700)
            off = frame.evaluate(STEP_STATE)
            _shut_or_open(off, [])
            assert [s["pressed"] for s in off["steps"]] == ["false", "false"] and off["active"] == key, ("Space lets it go", off["steps"])
            assert page.evaluate("() => scrollY") == scrolled, "the page did not scroll by the space"
            page.keyboard.press(" ")
            page.wait_for_timeout(700)
            _shut_or_open(frame.evaluate(STEP_STATE), one)
            # the poll: the tree is built anew, the step and its bars are as they were, the keyboard is on the new step
            frame.evaluate("() => { window.__step = document.activeElement; window.__bar = document.querySelector('.bars > .bar.bar-on'); }")
            for _ in range(2):
                frame.evaluate(REDRAW)
                page.wait_for_timeout(700)
                kept = frame.evaluate(STEP_STATE)
                assert frame.evaluate("() => window.__step !== document.activeElement && window.__bar !== document.querySelector('.bars > .bar.bar-on')"), "(the draw made new nodes)"
                _shut_or_open(kept, one)
                assert [s["pressed"] for s in kept["steps"]] == ["false", "true"] and kept["active"] == key, ("pressed across the draw, the keyboard on it", kept["steps"], kept["active"])
                frame.evaluate("() => { window.__step = document.activeElement; window.__bar = document.querySelector('.bars > .bar.bar-on'); }")
            page.keyboard.press("Enter")
            page.wait_for_timeout(700)
            _shut_or_open(frame.evaluate(STEP_STATE), [])
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_a_step_pressed_stays_with_the_ladder_opened_by_more_and_a_step_past_the_fourth_can_be_pressed(fixture_file, engine):
    """kit: rail, the ladder. The word 'N more' and the scroll of the ladder go on as before with a step pressed: the press stays, a step in the open window past the fourth
    can be pressed (the one before it is let go), and the draw of the poll keeps both the open ladder and the step pressed."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            page, frame = _ladder_open(browser, views, 1280)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame.locator(LADDER_STEPS).nth(2).click()
            page.mouse.move(1390, 890)
            page.wait_for_timeout(700)
            first = frame.evaluate(STEP_STATE)
            _shut_or_open(first, first["steps"][2]["accounts"])
            _ladder_press(page, frame)
            opened = frame.evaluate(STEP_STATE)
            assert frame.evaluate("() => document.querySelector('.rail-more').getAttribute('aria-expanded')") == "true" and len(opened["steps"]) >= 8
            assert [i for i, s in enumerate(opened["steps"]) if s["pressed"] == "true"] == [2], ("'more' leaves the press where it is", opened["steps"])
            _shut_or_open(opened, opened["steps"][2]["accounts"])
            frame.evaluate("(sel) => document.querySelectorAll(sel)[7].scrollIntoView({block: 'nearest'})", LADDER_STEPS)
            page.wait_for_timeout(300)
            frame.locator(LADDER_STEPS).nth(7).click()
            page.mouse.move(1390, 890)
            page.wait_for_timeout(900)
            far = frame.evaluate(STEP_STATE)
            assert [i for i, s in enumerate(far["steps"]) if s["pressed"] == "true"] == [7], ("the step past the fourth is pressed, the other let go", far["steps"])
            _shut_or_open(far, far["steps"][7]["accounts"])
            frame.evaluate(REDRAW)
            page.wait_for_timeout(700)
            kept = frame.evaluate(STEP_STATE)
            assert frame.evaluate("() => document.querySelector('.rail-more').getAttribute('aria-expanded')") == "true"
            assert [i for i, s in enumerate(kept["steps"]) if s["pressed"] == "true"] == [7], ("the press outlives the draw with the ladder open", kept["steps"])
            _shut_or_open(kept, kept["steps"][7]["accounts"])
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_the_pick_of_a_window_or_a_family_lets_go_of_the_step_pressed_and_so_does_a_shell_narrowed_under_860px(fixture_file, engine):
    """kit: rail, the ladder. The pick of another window, of another family, and the shell narrowed under 860px (where the ladder is not drawn and nothing could press the
    step again) let go of the step: no bar stays open and no step is pressed, also on the way back to where it was pressed."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    nothing = "() => ({bars: document.querySelectorAll('.bars > .bar.bar-on').length, steps: document.querySelectorAll('.rail-step[aria-pressed=true]').length})"
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            served, family = _ladder_served(views, "claude", lambda view: (_ten_in_every_window(view, views["claude_many"]), _spread_resets(view)))
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_ladder_frame(page, served, family, 1280)
            _settle_family(frame, family)

            def press(index):
                frame.locator(LADDER_STEPS).nth(index).click()
                page.mouse.move(1390, 890)
                page.wait_for_timeout(700)
                assert frame.evaluate(nothing) == {"bars": 1, "steps": 1}, ("pressed", index, frame.evaluate(nothing))

            press(1)
            tab = frame.evaluate("() => { const t = [...document.querySelectorAll('.rail .wt-tab')].find((n) => n.getAttribute('aria-selected') !== 'true'); return t && t.getAttribute('data-focus'); }")
            home = frame.evaluate("() => { const t = [...document.querySelectorAll('.rail .wt-tab')].find((n) => n.getAttribute('aria-selected') === 'true'); return t && t.getAttribute('data-focus'); }")
            assert tab and home, "Claude has other windows to pick"
            frame.locator('[data-focus="' + tab + '"]').click()
            page.mouse.move(1390, 890)
            page.wait_for_timeout(1500)
            assert frame.evaluate(nothing) == {"bars": 0, "steps": 0}, ("another window: nothing is pressed", frame.evaluate(nothing))
            frame.locator('[data-focus="' + home + '"]').click()
            page.mouse.move(1390, 890)
            page.wait_for_timeout(1500)
            assert frame.evaluate(nothing) == {"bars": 0, "steps": 0}, ("and back: it does not come back by itself", frame.evaluate(nothing))
            press(0)
            frame.locator('[data-focus="harness:codex"]').click()
            _settle_family(frame, "codex")
            frame.locator('[data-focus="harness:claude"]').click()
            _settle_family(frame, "claude")
            assert frame.evaluate(nothing) == {"bars": 0, "steps": 0}, ("another family and back: nothing is pressed", frame.evaluate(nothing))
            # the shell narrowed under 860px: the bars are let go (the ladder is not drawn there), and wide again nothing is pressed
            press(1)
            page.evaluate("() => { document.querySelector('iframe').style.width = '800px'; }")
            page.wait_for_timeout(500)
            assert frame.evaluate("() => document.querySelectorAll('.bars > .bar.bar-on').length") == 0, "under 860px no bar is held open by a step"
            page.evaluate("() => { document.querySelector('iframe').style.width = '1280px'; }")
            page.wait_for_timeout(500)
            frame.evaluate(REDRAW)
            page.wait_for_timeout(700)
            assert frame.evaluate(nothing) == {"bars": 0, "steps": 0}, ("wide again: nothing is pressed", frame.evaluate(nothing))
            assert errors == [], errors
            page.close()
        finally:
            browser.close()


def _far_bar_first(view):
    """Forty accounts (the ten of claude_many four times over, each with a share of its own) in every window; the one with the least share left, far to the right in the row, is the first to
    reset (the first step, an hour away), the others two hours apart after it: one step each, so that the step of that bar names it alone."""
    now = time.time()
    for group in view["reserve"]["summary"]["groups"]:
        base = [b for b in (group.get("bars") or []) if b.get("state") == "current" and isinstance(b.get("left"), (int, float))]
        grown = []
        for n in range(4):
            for b in base:
                c = json.loads(json.dumps(b))
                c["account"] = "%s-%d" % (b["account"], n)
                c["left"] = max(0.0, b["left"] * (1 - n * 0.05) - n * 0.0007)
                grown.append(c)
        grown.sort(key=lambda c: -c["left"])   # (fullest first: the skill's order, which the row keeps)
        for k, c in enumerate(grown):
            c["resets_at"] = qs.iso(now + (3600 if k == len(grown) - 1 else 10800 + 7200 * k))
        group["bars"] = grown
    # (the pick of a bar needs its account in the family: the card is drawn for it - so the forty accounts stand in the family as well, as in a real answer)
    base_accounts = view["groups"][0]["accounts"]
    view["groups"][0]["accounts"] = [dict(json.loads(json.dumps(a)), key="%s-%d" % (a["key"], n)) for n in range(4) for a in base_accounts]


@pytest.mark.parametrize("reduced", [False, True])
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_ladder_the_bar_a_step_opens_at_the_end_of_the_row_is_brought_into_its_room_with_motion_and_without(fixture_file, engine, reduced):
    """kit: rail, the ladder. At 860px a row of forty bars goes sideways at rest; the step that opens the bar at its far end takes the row there until the bar stands whole in
    its room (at the end of the move; where nothing moves, under reduced motion, at once); the second press shuts it."""
    from conftest import launch_engine

    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser = launch_engine(playwright, engine)
        try:
            served, family = _step_served(views, _far_bar_first)
            page = browser.new_page(viewport={"width": 1400, "height": 900}, **({"reduced_motion": "reduce"} if reduced else {}))
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            frame = _mount_ladder_frame(page, served, family, 860)
            page.wait_for_timeout(1300)
            shut = frame.evaluate(STEP_STATE)
            first = shut["steps"][0]
            far = next(b for b in shut["bars"] if b["account"] == first["accounts"][0])
            assert len(first["accounts"]) == 1 and len(shut["bars"]) == 40 and shut["scroll"] > shut["client"] + 1, ("forty bars: the row goes sideways at rest", shut["scroll"], shut["client"])
            assert far["right"] < 0 and shut["at"] == 0, ("the bar of the first step stands out of the room, to the right", far)
            if shut["steps"][0]["pressed"] == "true":
                # the top account is picked as the window opens and the first step is its own: the press would let it go, so it is let go first and the press below picks it
                frame.locator(LADDER_STEPS).nth(0).click()
                page.mouse.move(850, 890)
                page.wait_for_timeout(900)
            frame.locator(LADDER_STEPS).nth(0).click()
            page.mouse.move(850, 890)
            page.wait_for_timeout(120 if reduced else 900)
            on = frame.evaluate(STEP_STATE)
            end = next(b for b in on["bars"] if b["account"] == first["accounts"][0])
            assert (end["on"] or end["sel"]) and on["at"] > 1, ("the bar is open and the row went sideways to it", end, on["at"])
            assert end["left"] >= -1.5 and end["right"] >= -1.5, ("the bar stands whole in the room of the row", end)
            frame.locator(LADDER_STEPS).nth(0).click()
            page.mouse.move(850, 890)
            page.wait_for_timeout(900)
            off = frame.evaluate(STEP_STATE)
            assert not [b for b in off["bars"] if b["on"]], "shut again"
            assert errors == [], errors
            page.close()
        finally:
            browser.close()
