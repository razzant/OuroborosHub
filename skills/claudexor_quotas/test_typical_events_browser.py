"""Real browser (bundled Chromium and WebKit): what the verdict strip says after the events fix, Details closed.
All readings are synthetic.

What is held here:
- a real stop of 5 minutes before the reset is said as running out, with a warning, never "almost"; a small margin
  is "almost", with what is left;
- a small positive reserve and pace read "<0.01 win" and "<0.001 win/h", never 0.00 / 0.000;
- the first stop's mark stands on the drawn line: past the 24 h chart's end (inside its last axis hour) there is no
  mark, on the 7 d chart there is one;
- while the answer for a newly chosen model is read, the previous verdict is neither shown nor spoken;
- accounts at their limit (the estimator's ``all_observed_at_limit``), the pool empty now too, have an explanation of
  their own, not a count of watched hours;
- an account that cannot run now: the stop still on at the first reset is said ("No usable … from ~… to ~…"), what
  is on restricted accounts then, the reset's own figures, and the stop from now in a few words ("earlier stop now");
- a translation pinned for the older per-account "runs out" sentences never stands in for the pool's (no "{n}").
The 48-hour rate is given (quota_forecast.estimate_typical_pace is replaced for the test; the estimator is tested
elsewhere); a short synthetic record keeps the chart real.
"""
import json
import re
import time
from pathlib import Path

import pytest

import plugin
import quota_forecast as qf
import quota_history
import quota_summary as qs
from test_reserve import FIVE_H, WEEK, payload, with_i18n

pytest.importorskip("playwright.sync_api")

HERE = Path(__file__).parent
WIDGET = (HERE / "widget.js").resolve()
H = 3600.0
PAGE = '<!doctype html><html><head></head><body><div id="root"></div></body></html>'
FETCH = r"""
  window.fetch = (url, options = {}) => {
    const post = ((options && options.method) || 'GET') !== 'GET';
    const answer = post ? { ok: true } : (String(url).indexOf('horizon=24h') >= 0 ? view['24h'] : view['7d']);
    return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(answer) });
  };
"""
# The widget asks for its first chart at 24 h, then at the weekly limit's own span (7 d): each span gets its own view.
STUB = with_i18n("(view) => {" + FETCH + "}")
READ = """() => {
  const strip = document.querySelector('.tl-block .tl-verdict');
  const head = strip && strip.querySelector('.tl-verdict-head');
  return {
    head: head ? head.textContent : null, tone: head ? head.className : '',
    text: strip ? strip.textContent : '',
    figs: strip ? [...strip.querySelectorAll('.tl-vfig')].map((f) => f.textContent) : [],
    marks: document.querySelectorAll('.tl-block .chart-svg .deplete-dot').length,
    lines: document.querySelectorAll('.tl-block .chart-svg path.line-scenario').length,
    updating: !!document.querySelector('.tl-block .tl-updating'),
    legend: (document.querySelector('.tl-block .calm-legend') || {}).textContent || '',
    detailsOpen: !!(document.querySelector('.tl-block .chart-table') || {}).open,
    typicalTitle: (document.querySelector('[data-focus="scenario:typical"]') || { getAttribute: () => '' }).getAttribute('title') || '',
    overflow: document.documentElement.scrollWidth > window.innerWidth,
  };
}"""


def _now():
    return float(int(time.time() // 3600) * 3600 + 600)  # a whole hour + 10 min: the 24 h end is not an axis hour


def _con(cid, used, window, reset_at, label):
    return {"id": cid, "label": label, "used_ratio": round(used, 6), "window_seconds": window,
            "resets_at": qs.iso(reset_at) if reset_at is not None else None}


def _snap(sid, constraints, t):
    return {"subject": {"harness": "codex", "subject_id": sid, "plan_label": "plus"}, "constraints": constraints,
            "availability": {"state": "available"}, "observed_at": qs.iso(t - 10), "freshness": "fresh",
            "source": "app"}


def _profile(sid, enabled=True):
    return {"profile": {"harness_id": "codex", "profile_id": sid, "enabled": enabled, "display_name": sid},
            "status": {"verification": "passed", "availability": "available", "detail": ""},
            "identity": {"email": "", "plan": ""}}


def _rate(value):
    return {"state": "ok", "reason": "", "rate_per_hour": value, "valid_seconds": 48 * 3600, "valid_hours": 48.0,
            "observed_burn": value * 48, "window_hours": 48, "min_valid_hours": 12}


def _views(tmp_path, monkeypatch, snaps_at, info, *, disabled=(), model=None):
    """The weekly limit's view on each span, from three synthetic sweeps and the given 48-hour estimate."""
    monkeypatch.setattr(qf, "estimate_typical_pace", lambda calc, state: dict(info))
    now = _now()
    store = quota_history.HistoryStore(tmp_path)
    status = None
    for t in (now - 600.0, now - 300.0, now):
        snaps = snaps_at(t, now)
        profiles = [_profile(s["subject"]["subject_id"], enabled=s["subject"]["subject_id"] not in disabled)
                    for s in snaps]
        status = payload(snaps, profiles, harnesses=("codex",))
        plugin.persist_sweep(store, status, "", t)
    views = {}
    for horizon in ("24h", "7d"):
        view = plugin.build_view(status, "")
        first = plugin.reserve_view(store, status, now, now, harness="codex", horizon=horizon, name_accounts=True)
        week = next(g["key"] for g in first["summary"]["groups"] if g["window_seconds"] == WEEK)
        view["reserve"] = plugin.reserve_view(store, status, now, now, harness="codex", group=week, horizon=horizon,
                                              name_accounts=True, analytics=True, model=model)
        views[horizon] = view
    return views, now


def _open(browser, views, stub=STUB, width=1120, wait=".tl-block .tl-verdict"):
    page = browser.new_page(viewport={"width": width, "height": 1300})
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.set_content(PAGE)
    page.evaluate(stub, views)
    page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
    page.wait_for_selector(wait)
    return page, errors


def _one(used, reset_in):
    return lambda t, now: [_snap("acct-a", [_con("primary", used, WEEK, now + reset_in, "Weekly")], t)]


def test_a_short_stop_reads_as_running_out_and_a_small_margin_as_almost(browser, tmp_path, monkeypatch):
    views, _now_ = _views(tmp_path / "stop", monkeypatch, _one(0.90, 65 * 60), _rate(0.10))
    page, errors = _open(browser, views)
    try:
        got = page.evaluate(READ)
        assert "runs out 5m before the" in got["head"] and "almost" not in got["head"], got
        assert "warn" in got["tone"].split() and not got["detailsOpen"], got
        assert not errors, errors
    finally:
        page.close()
    views, _now_ = _views(tmp_path / "margin", monkeypatch, _one(0.87, 65 * 60), _rate(0.10))
    page, errors = _open(browser, views)
    try:
        got = page.evaluate(READ)
        assert "almost runs out before the" in got["head"], got
        assert "0.02 win left before reset" in got["text"], got
        assert not errors, errors
    finally:
        page.close()


def test_a_small_positive_reserve_and_pace_read_below_the_precision(browser, tmp_path, monkeypatch):
    views, _now_ = _views(tmp_path, monkeypatch, _one(0.996, 5 * H), _rate(0.0002))
    page, errors = _open(browser, views)
    try:
        got = page.evaluate(READ)
        assert "<0.01 win" in got["text"] and "<0.001 win/h" in got["text"], got
        assert "0.000 win/h" not in got["text"] and not re.search(r"(?<![\d.])0\.00 win", got["text"]), got
        assert "<0.001 win/h" in got["legend"], got
        assert not errors, errors
    finally:
        page.close()


def test_the_first_stops_mark_stands_on_the_drawn_line_only(browser, tmp_path, monkeypatch):
    """0.50 left at 0.50/24.33 win/h: empty 24 h 20 min from now, past the 24 h chart's end (now + 24 h) but inside
    its last axis hour; the reset is at +30 h."""
    views, _now_ = _views(tmp_path, monkeypatch, _one(0.50, 30 * H), _rate(0.50 / (24 + 1 / 3)))
    page, errors = _open(browser, views)
    try:
        week = page.evaluate(READ)
        assert week["marks"] == 1 and week["lines"] == 1, week
        page.click('[data-focus="horizon:24h"]')
        page.wait_for_function("() => !document.querySelector('.tl-block .chart-wait')"
                               " && document.querySelector('.tl-block .tl-verdict')")
        day = page.evaluate(READ)
        assert day["marks"] == 0 and day["lines"] == 1, day
        assert day["head"] == week["head"], (day, week)  # one future: the verdict does not change with the span
        assert not errors, errors
    finally:
        page.close()


def test_the_previous_models_verdict_is_not_shown_while_the_new_one_is_read(browser, tmp_path, monkeypatch):
    views, _now_ = _views(tmp_path / "stale", monkeypatch, _one(0.60, 30 * H), _rate(0.05), model="m1")
    page, errors = _open(browser, views, wait=".tl-block .tl-updating")
    try:
        got = page.evaluate(READ)
        assert got["updating"] and got["head"] is None and not got["text"], got
        assert not errors, errors
    finally:
        page.close()
    views, _now_ = _views(tmp_path / "fresh", monkeypatch, _one(0.60, 30 * H), _rate(0.05))
    page, errors = _open(browser, views)
    try:
        got = page.evaluate(READ)
        assert not got["updating"] and got["head"], got
        assert not errors, errors
    finally:
        page.close()


def test_accounts_at_their_limit_have_an_explanation_of_their_own(browser, tmp_path, monkeypatch):
    info = {"state": "insufficient", "reason": "all_observed_at_limit", "rate_per_hour": None, "valid_seconds": 0,
            "valid_hours": 0.0, "observed_burn": 0.0, "window_hours": 48, "min_valid_hours": 12}

    def both_spent(t, now):
        return [_snap("acct-a", [_con("primary", 1.0, WEEK, now + 3 * H, "Weekly")], t),
                _snap("acct-b", [_con("primary", 1.0, WEEK, now + 5 * H, "Weekly")], t)]

    views, _now_ = _views(tmp_path, monkeypatch, both_spent, info)
    page, errors = _open(browser, views)
    try:
        got = page.evaluate(READ)
        assert "pace unknown: accounts sat at the limit" in got["head"], got
        assert "Use cannot be measured while accounts sit at the limit" in got["text"], got
        assert "valid" not in got["text"] and "Not enough 48h history" not in got["text"], got
        assert "use not measurable at the limit — not drawn" in got["legend"], got
        assert "sat at the limit" in got["typicalTitle"], got
        assert got["lines"] == 0 and got["marks"] == 0 and not got["detailsOpen"], got
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("width", [1120, 360])
def test_an_account_that_cannot_run_now_is_said_so(browser, tmp_path, monkeypatch, width):
    """A is disabled with 0.20 left; B (0.40 left) has its 5-hour window spent until +5h: no usable reserve now.
    B then runs out at +13h and nothing usable is left until the reset at +60h: that stop is said, A's 0.20 on a
    restricted account, and the one from now is kept as the earlier stop."""
    def accounts(t, now):
        return [
            _snap("acct-a", [_con("primary", 0.80, WEEK, now + 60 * H, "Weekly"),
                             _con("five_hour", 0.10, FIVE_H, now + 4 * H, "5 hour")], t),
            _snap("acct-b", [_con("primary", 0.60, WEEK, now + 60 * H, "Weekly"),
                             _con("five_hour", 1.0, FIVE_H, now + 5 * H, "5 hour")], t),
        ]

    views, _now_ = _views(tmp_path, monkeypatch, accounts, _rate(0.05), disabled=("acct-a",))
    page, errors = _open(browser, views, width=width)
    try:
        got = page.evaluate(READ)
        assert got["head"].startswith("No usable ") and " from ~" in got["head"] and " to ~" in got["head"], got
        assert "0.20 win is on restricted accounts" in got["text"] and "earlier stop now to ~" in got["text"], got
        assert [f.split("~")[0].split(" ")[0] for f in got["figs"]] == ["Usable", "Resumes", "Before"], got
        assert "1 account is restricted with no reported end" in got["text"], got
        assert not got["overflow"], f"horizontal overflow at {width}px"
        assert not errors, errors
    finally:
        page.close()


def test_a_translation_pinned_for_the_older_sentences_never_stands_in_for_the_pools(browser, tmp_path, monkeypatch):
    english = json.loads((HERE / "strings_en.json").read_text(encoding="utf-8"))
    pinned = {
        "runs.before": {"forms": {"one": "{n} аккаунт закончится до сброса, первый {when}",
                                  "few": "{n} аккаунта закончатся до сброса, первый {when}",
                                  "many": "{n} аккаунтов закончатся до сброса, первый {when}",
                                  "other": "{n} аккаунта закончатся до сброса, первый {when}"}},
        "runs.lead": "При таком темпе {parts}.",
    }
    answer = json.dumps(json.dumps({"language": "ru", "revision": 1, "plural_select": None, "en": english,
                                    "strings": pinned}))
    stub = ("(view) => {\n  window.__i18n = " + answer + ";\n"
            + "  const fetchView = (url, options) => {" + FETCH.replace("window.fetch = ", "const inner = ")
            + "    return inner(url, options);\n  };\n"
            + "  window.fetch = (url, options = {}) => String(url).indexOf('/i18n') >= 0\n"
            + "    ? Promise.resolve({ ok: true, status: 200, text: async () => window.__i18n })\n"
            + "    : fetchView(url, options);\n}")
    views, _now_ = _views(tmp_path, monkeypatch, _one(0.90, 30 * H), _rate(0.10))
    page, errors = _open(browser, views, stub=stub)
    try:
        page.click('[data-focus="chart-table"]')
        page.wait_for_selector(".tl-block .chart-table[open] .tl-facts")
        facts = page.evaluate("() => document.querySelector('.tl-block .tl-facts').textContent")
        assert "At typical pace, the pool runs out before the next reported reset" in facts, facts
        assert "аккаунт" not in facts and "При таком темпе" not in facts, facts
        assert "{n}" not in page.evaluate("() => document.body.textContent")
        assert not errors, errors
    finally:
        page.close()
