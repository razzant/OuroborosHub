"""Bounded review falsifiers: real projections, independent arithmetic, no provider I/O."""
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
import asyncio
import copy
import json
import threading

import pytest
import plugin
import quota_analytics as qa
import quota_context as qc
import quota_history as qh
import quota_joint as qj
import quota_resources as qr
import quota_summary as qs
from test_reserve import NOW, FIVE_H, WEEK, constraint, payload, profile, snap
from test_joint_view import account_snap

H = 3600.0


def two_windows(short, week, *, short_reset=H, week_reset=2*H, sid="a"):
    return payload([snap("claude", sid, [constraint("five_hour", short, FIVE_H, reset=short_reset),
                    constraint("seven_day", week, WEEK, reset=week_reset)], observed_abs=NOW)],
                   [profile("claude", sid)], harnesses=("claude",))


def test_text_exhaustion_does_not_hold_image_and_shared_kind_still_holds():
    def view(kinds):
        text = constraint("codex", 1.0, FIVE_H, reset=H)
        text["applies_to_request_kinds"] = kinds
        image = constraint("image_gen", .2, WEEK, reset=2*H)
        image["applies_to_request_kinds"] = ["image"]
        data = payload([snap("codex", "a", [text, image], observed_abs=NOW)],
                       [profile("codex", "a")], harnesses=("codex",))
        return plugin.answer_of(None, data, {}, data, NOW, NOW)
    image = next(r for r in view(["text"])["groups"] if r["limit"] == "image_gen")
    assert image["remaining_windows"] == pytest.approx(float(Fraction(4, 5)))
    assert image["unrestricted_windows"] == pytest.approx(.8)
    assert "other_limit_spent" not in image.get("restrictions", {})
    shared = next(r for r in view(["text", "image"])["groups"] if r["limit"] == "image_gen")
    assert shared["unrestricted_windows"] == 0
    assert shared["restrictions"]["other_limit_spent"]["accounts"] == 1


def test_uncalibrated_zero_is_known_only_until_its_exhausted_window_refills():
    data = two_windows(1.0, .5)
    joint = plugin.reserve_view(None, data, NOW, NOW, harness="claude", analytics=True)["chart"]["analytics"]["joint"]
    assert joint["figure"] == 0 and joint["known"] == 1
    for sc in joint["scenarios"].values():
        assert sc["line"][0][1] == 0
        assert sc["line"][-1][1] is None
        assert all(c["value"] is None for c in sc["checkpoints"] if c["at"] >= NOW+H)
        assert sc["schedule"][0]["adds"] is None
        assert sc["schedule"][0]["known_after"] == 0
        assert sc["coverage"][-1] == {"at": NOW+24*H, "known": 0, "unknown": 1}
    assert joint["table"][-1]["scenario_no_new_use"] is None


def test_unknown_joint_contribution_does_not_erase_a_known_cohort():
    wins = [qj.Win("short", 0, NOW+H, unit=True), qj.Win("week", .5, NOW+2*H)]
    known = [qj.Win("short", .25, unit=True), qj.Win("week", .5, k=2)]
    ts = [(qj.simulate(w, NOW, NOW+3*H, False), w) for w in (wins, known)]
    assert qj.contribution(*ts[0], NOW) == 0
    assert qj.contribution(*ts[0], NOW+H) is None
    assert qj.total_line(ts, NOW, NOW+3*H, True)[-1][1] == float(Fraction(1,4))
    assert qj.joint_value([qj.Win("short", 1, unit=True), qj.Win("week", 1)]).value is None


def rate_store(tmp_path, *, precision=False, change_scope=False):
    store = qh.HistoryStore(tmp_path)
    start = NOW-8*H
    last = None
    for step in range(241):
        t = start+step*120
        hours = Decimal(step)/Decimal(30)
        if precision:
            cons = [constraint("half_day", float(Decimal(".5999")+Decimal(".05")*hours), 43200, reset=3*H),
                    constraint("seven_day", float(Decimal(".5")+Decimal(".01")*hours), WEEK, reset=24*H)]
        else:
            active = max(Decimal(0), hours-7)
            cons = [constraint("five_hour", float(Decimal(".1")+active*Decimal(".1")), FIVE_H, reset=24*H),
                    constraint("seven_day", float(Decimal(".3")+active*Decimal(".02")), WEEK, reset=48*H)]
            if change_scope:
                cons.append(constraint("seven_day_opus", .2, WEEK, reset=48*H,
                                       models=["opus-4"] if hours < 6 else ["opus-4", "opus-5"]))
        last = payload([snap("claude", "a", cons, observed_abs=t)], [profile("claude", "a")], harnesses=("claude",))
        plugin.persist_sweep(store, last, "", t)
    return store, last


def test_positive_hours_are_not_quantized_to_zero_in_tool_snapshot_or_widget_data(tmp_path):
    store, data = rate_store(tmp_path, precision=True)
    answer = plugin.answer_of(store, data, {}, data, NOW, NOW)
    expected = Decimal(".0001")/Decimal(".05")
    hours = answer["joint"]["claude"]["conditional_account_hours"]
    assert hours == pytest.approx(float(expected), abs=1e-10)
    assert hours > 0
    assert "conditional ≈<0.1 account-hours" in qc.snapshot_text(answer)
    assert "common-watched-period pace" in qc.snapshot_text(answer)
    assert answer["joint"]["claude"]["hours_pace_basis"] == "common_watched_period"
    assert qc.rounded_hours(0) == "0.0"  # a real zero stays distinct


def test_per_window_uses_trailing_hour_not_the_joint_calibration_average(tmp_path):
    store, data = rate_store(tmp_path)
    result = plugin.reserve_view(store, data, NOW, NOW, harness="claude", group="claude|five_hour|18000|-", analytics=True)
    a = result["chart"]["analytics"]
    # Independent slopes: final hour .1/h versus .1/8h across the common period.
    g = next(g for g in result["summary"]["groups"] if g["meaning"] == "five_hour")
    assert g["recent_pace"]["windows_per_hour"] == pytest.approx(.1, abs=1e-5)
    future = a["window"]["scenarios"]["recent_pace"]
    assert next(c["value"] for c in future["checkpoints"] if c["after_seconds"] == 12*H) == 0  # .8 / .1 = 8h; reset at 24h
    assert a["joint"]["hours"]["value"] > 24
    assert a["window"]["pace_basis"] == "trailing_hour"
    assert a["window"]["table"][0]["scenario_no_new_use"] == pytest.approx(.8)


def test_history_only_legacy_model_scope_cannot_poison_unspecified_request(tmp_path):
    store, data = rate_store(tmp_path, change_scope=True)
    result = plugin.reserve_view(store, data, NOW, NOW, harness="claude", group="claude|five_hour|18000|-", analytics=True)
    a = result["chart"]["analytics"]
    assert a["scope"]["unreadable_windows"] == 0
    assert a["joint"]["known"] == 1 and a["joint"]["figure"] > 0
    assert a["window"]["scenarios"]["recent_pace"]["at_pace"] == 1
    # Lost model lists cannot establish which named request an old series binds.
    named = plugin.reserve_view(store, data, NOW, NOW, harness="claude", analytics=True, model="opus-4")
    assert named["chart"]["analytics"]["scope"]["unreadable_windows"] == 1
    assert named["chart"]["analytics"]["joint"]["known"] == 0


def resources_with_boundary(when):
    return [{"target": {"harness": "claude", "profile_id": "a"},
             "resets": {"value": [{"id": "offer", "label": "Offer", "available_count": 2,
                                     "usable_now": True, "resets_at": when}],
                        "freshness": "fresh", "observed_at": qs.iso(NOW-60)}}]


class Api:
    def __init__(self, path): self.path = path; self.routes = {}; self.logs = []
    def get_state_dir(self): return str(self.path)
    def log(self, *args): self.logs.append(args)
    def register_route(self, name, fn, **kwargs): self.routes[name] = fn
    def register_tool(self, *args, **kwargs): pass
    def register_supervised_task(self, *args, **kwargs): pass
    def register_ui_tab(self, *args, **kwargs): pass
    def on_unload(self, *args, **kwargs): pass


def test_passed_resource_is_dated_not_whole_snapshot_suppressed(tmp_path):
    data = two_windows(.2, .5)
    data["resources"] = resources_with_boundary(qs.iso(NOW-30))
    latest = plugin.LatestRead(); latest.put(data, "", NOW)
    api = Api(tmp_path)
    assert plugin.publish_snapshot(api, None, data, "", NOW, latest) == "written"
    body = json.loads((tmp_path/qc.SNAPSHOT_FILE).read_text())
    assert "last known" in body["text"] and "same-account" in body["text"]
    assert "usable yes" in body["text"]  # a dated vendor statement, not renewed usability
    assert qs.parse_instant(body["valid_until"]) == NOW+qc.VALID_FOR_SEC
    answer = plugin.answer_of(None, data, {}, data, NOW, NOW)
    assert answer["resources"]["claude"]["facets"]["resets"][0]["state"] == "last_known"
    assert answer["manual_reset_offers"]["claude"]["accounts_current"] == 0
    assert qr.account_resources(data,"claude","a",now=NOW)["resets"]["state"] == "last_known"


@pytest.mark.parametrize("iso", ["2026-09-21T14:00:00.5Z", "2026-09-21T14:00:00.123456789Z"])
def test_snapshot_uses_existing_timestamp_parser(iso):
    answer = {"resources": {"claude": {"facets": {"resets": [{"state":"current", "rows":[{"resets_at":iso}]}]}}}}
    assert qc.earliest_reset(answer) == iso
    assert qs.parse_instant(iso) is not None


def test_mixed_resource_boundary_keeps_other_current_fact():
    data = {"resources": resources_with_boundary(qs.iso(NOW-30))}
    data["resources"][0]["resets"]["value"].append({"id":"future", "available_count":3, "usable_now":True, "resets_at":qs.iso(NOW+60)})
    family = qr.family_resources(data, now=NOW)
    facet = family["claude"]["facets"]["resets"][0]
    assert facet["state"] == "current"
    assert facet["rows"][0]["state"] == "last_known"
    assert qr.family_resets(data,"claude",now=NOW)["available"] == 3
    assert qc.earliest_reset({"resources":family}) == qs.iso(NOW+60)


def test_stop_during_snapshot_computation_discards_result(tmp_path, monkeypatch):
    data = two_windows(.2, .5); latest = plugin.LatestRead();latest.put(data,"",NOW)
    control = qh.StopControl()
    original = plugin.answer_of
    def answer(*a, **kw):
        result = original(*a, **kw);control.set();return result
    monkeypatch.setattr(plugin,"answer_of",answer)
    with pytest.raises(qh.HistoryStopped):
        plugin.publish_snapshot(Api(tmp_path),None,data,"",NOW,latest,control=control)
    assert not (tmp_path/qc.SNAPSHOT_FILE).exists()


def test_stop_before_snapshot_replace_preserves_previous(tmp_path):
    control = qh.StopControl(); control.set()
    old = b"unique previous result"; path=tmp_path/qc.SNAPSHOT_FILE;path.write_bytes(old)
    with pytest.raises(qh.HistoryStopped):
        qc.publish(tmp_path,"new",NOW,None,qs.iso_exact,qs.parse_instant,control=control)
    assert path.read_bytes() == old and not list(tmp_path.glob("*.tmp"))


def test_newest_hides_survive_the_bound():
    values = ["old-%d" % i for i in range(plugin.MAX_HIDDEN)] + ["new"]
    hidden = plugin.clean_display({"hidden":values})["hidden"]
    assert len(hidden) == plugin.MAX_HIDDEN and hidden[-1] == "new" and "old-0" not in hidden


def test_bounded_history_read_shares_newest_evidence_instead_of_starving_later_pairs(tmp_path):
    store = qh.HistoryStore(tmp_path)
    for step in range(31):
        t = NOW-H+step*120
        rows = [snap("claude", str(i), [constraint("five_hour", .1+.01*step, FIVE_H, reset=H),
                constraint("seven_day", .2+.001*step, WEEK, reset=24*H)], observed_abs=t) for i in range(4)]
        data = payload(rows,[profile("claude", str(i)) for i in range(4)],harnesses=("claude",))
        plugin.persist_sweep(store,data,"",t)
    def requests(salt):
        return {(qs.pseudo_id(salt,"claude",str(i)),key):NOW-7*86400 for i in range(4)
                for key in ("claude|five_hour|18000|-","claude|seven_day|604800|-")}
    view = store.read(requests,NOW,max_rows=80)
    assert view.truncated and len(view.runs) == 8
    assert sum(len(r) for r in view.runs.values()) <= 80
    assert all(len(r) >= 10 and r[-1].last_seen == NOW for r in view.runs.values())
    state = qs.compute(data,view,NOW)
    assert all(m.rate.get("windows_per_hour") is not None for g in state.groups for m in g.measured)


def test_display_save_sequence_matches_existing_preferences_contract(tmp_path):
    api=Api(tmp_path);plugin.register(api)
    class Request:
        def __init__(self, body): self.body=body
        async def json(self): return self.body
    async def run():
        await api.routes["display"](Request({"hidden":["new"],"frame":"one","seq":2}))
        await api.routes["display"](Request({"hidden":["old"],"frame":"one","seq":1}))
    asyncio.run(run())
    assert plugin.read_display(api)=={"hidden":["new"]}


def test_widget_future_svg_and_cursor_do_not_coerce_unknown_to_zero(tmp_path):
    """The actual renderer consumes production-built nulls; no prototype values."""
    import os
    import subprocess
    import test_quotas
    from test_reserve import _node
    data = two_windows(1.0, .5)
    view = plugin.build_view(data, "", NOW)
    view["reserve"] = plugin.reserve_view(None,data,NOW,NOW,harness="claude",analytics=True)
    view["display"] = {"hidden":[]}
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    js = r'''
(async () => {
  const fx=JSON.parse(process.env.GAP_FIXTURE), env=await boot(()=>fx);
  byFocus(env.root,'tab:joint').listeners.click[0]({stopPropagation(){}});await settle();
  const line=walk(env.root).find(n=>n.tagName==='PATH' && n.getAttribute('class')==='line-scenario');assert.ok(line);
  const xs=[...line.getAttribute('d').matchAll(/[ML]([\d.]+) /g)].map(m=>+m[1]);
  const reset=walk(env.root).find(n=>n.getAttribute('class')==='reset-dot');assert.ok(reset);
  assert.ok(Math.max(...xs)<=+reset.getAttribute('cx')+.11,'unknown after first refill must break SVG, not extend zero');
  const plot=classes(env.root,'chart-plot')[0];plot.listeners.keydown[0]({key:'End',preventDefault(){}});
  assert.match(classes(env.root,'chart-readout')[0].textContent,/no new use not drawn/);
  assert.match(classes(env.root,'checkpoints')[0].textContent,/unknown/);
  env.disposeHooks[0]();console.log('FUTURE_GAP_RENDER_PASS');
})().catch(e=>{console.error(e.stack);process.exitCode=1});
'''
    widget=Path(os.environ.get("UI_WIDGET_PATH",Path(__file__).with_name("widget.js"))).resolve()
    p=subprocess.run([str(_node()),"-e",harness+js],cwd=widget.parent,
                     env={**os.environ,"WIDGET_PATH":str(widget),"GAP_FIXTURE":json.dumps(view)},
                     text=True,capture_output=True,timeout=30)
    assert p.returncode==0,p.stdout+p.stderr
    assert "FUTURE_GAP_RENDER_PASS" in p.stdout
