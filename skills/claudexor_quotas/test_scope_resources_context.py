"""Scope fidelity, passive resources, the display set and the context snapshot."""

import json
import re

import pytest

import plugin
import quota_context
import quota_history
import quota_resources as qr
import quota_scope
import quota_summary as qs
from test_reserve import FIVE_H, NOW, WEEK, constraint, payload, profile, snap
from test_joint_view import at_time, fed_store, START, STEP


# --- scope fidelity ----------------------------------------------------------


def test_scope_fields_keep_absent_empty_and_false_apart():
    absent = quota_scope.scope_of({"id": "x"})
    empty = quota_scope.scope_of({"id": "x", "applies_to_models": [], "applies_to_model_prefixes": [],
                                  "applies_to_unspecified_model": False})
    assert (absent.models_state, absent.unspecified) == ("absent", None)
    assert (empty.models_state, empty.prefixes_state, empty.unspecified) == ("empty", "empty", False)
    assert absent.describe() != empty.describe()
    # …while the engine's own identity (and so the group key) treats them as one vendor-wide window
    assert absent.key_token() == empty.key_token() == "-"


def test_legacy_windows_keep_their_exact_key_and_new_fields_split_it():
    plain = qs.reading_of(constraint("seven_day", 0.2, WEEK), "claude", "a", source="s", fresh=True, observed=NOW)
    named = qs.reading_of(constraint("m", 0.2, WEEK, models=["fable"]), "claude", "a", source="s", fresh=True, observed=NOW)
    assert plain.key == "claude|seven_day|604800|-"
    assert named.key == "claude|m|604800|" + qs._scope_hash(["fable"])  # byte-identical to the stored id
    prefixed = qs.reading_of(dict(constraint("m", 0.2, WEEK), applies_to_model_prefixes=["gemini-"]),
                             "agy", "a", source="s", fresh=True, observed=NOW)
    flagged = qs.reading_of(dict(constraint("m", 0.2, WEEK), applies_to_unspecified_model=True),
                            "agy", "a", source="s", fresh=True, observed=NOW)
    imaged = qs.reading_of(dict(constraint("m", 0.2, WEEK), applies_to_request_kinds=["image"]),
                           "agy", "a", source="s", fresh=True, observed=NOW)
    keys = {prefixed.key, flagged.key, imaged.key, plain.key.replace("claude", "agy").replace("seven_day", "m")}
    assert len(keys) == 4
    assert prefixed.scope != "-" and flagged.scope == "-"  # the restriction tie is models + prefixes only


def test_applicability_follows_the_engines_projection_rule():
    wide = quota_scope.scope_of({})
    scoped = quota_scope.scope_of({"applies_to_model_prefixes": ["claude-opus-"], "applies_to_models": ["opus"]})
    loose = quota_scope.scope_of({"applies_to_model_prefixes": ["gemini-"], "applies_to_unspecified_model": True})
    image_only = quota_scope.scope_of({"applies_to_request_kinds": ["image"]})
    assert quota_scope.applies(wide, None) and quota_scope.applies(wide, "anything")
    assert quota_scope.applies(scoped, "opus") and quota_scope.applies(scoped, "claude-opus-5-5")
    assert not quota_scope.applies(scoped, "claude-sonnet-5") and not quota_scope.applies(scoped, None)
    assert quota_scope.applies(loose, None) and quota_scope.applies(loose, "gemini-4-argon")
    assert quota_scope.applies(image_only, None, "image") and not quota_scope.applies(image_only, None, "text")
    assert quota_scope.applies(quota_scope.scope_of({"applies_to_request_kinds": ["video"], "applies_to_models": ["a"]}),
                               "a") is None  # an unreadable scope is never read as vendor-wide


def test_a_prefix_only_window_is_not_a_shared_limit_and_its_cooldown_holds_models():
    row = {"subject": {"harness": "agy", "subject_id": "a", "plan_label": None},
           "constraints": [dict(constraint("gemini", 1.0, WEEK, reset=3600), applies_to_models=[],
                                applies_to_model_prefixes=["gemini-"], cooldown_until=qs.iso(NOW + 600)),
                           constraint("claude", 0.1, WEEK, reset=3600)],
           "availability": {"state": "available"}, "observed_at": qs.iso(NOW - 30), "freshness": "fresh",
           "source": "s"}
    data = payload([row], [profile("agy", "a")], harnesses=("agy",))
    norm, groups = qs.prepare(data, NOW)
    assert not norm.spent  # the spent prefix window blocks no other window: it is scoped
    view = plugin.build_view(data, "", NOW)
    cooldown = view["groups"][0]["accounts"][0]["quota"]["cooldowns"][0]
    assert cooldown["scope"] == "models"
    windows = view["groups"][0]["accounts"][0]["quota"]["constraints"]
    prefixed = next(w for w in windows if w["id"] == "gemini")
    assert prefixed["scoped_prefixes"] == ["gemini-"] and prefixed["scope_fields"]["scoped"]


def test_unknown_verification_never_becomes_signed_out():
    unknown = profile("claude", "a", verification="not_run")
    unknown["status"]["availability"] = "unknown"
    off = profile("claude", "b", verification="not_run")
    off["status"]["availability"] = "unavailable"
    failed = profile("claude", "c", verification="failed")
    failed["status"]["availability"] = "unknown"
    rows = [snap("claude", s, [constraint("five_hour", 0.2, FIVE_H, reset=3600)]) for s in "abc"]
    data = payload(rows, [unknown, off, failed], harnesses=("claude",))
    accounts = {k[1]: v for k, v in qs.normalize(data, NOW).accounts.items()}
    assert accounts["a"].signed_in is None and accounts["b"].signed_in is False and accounts["c"].signed_in is False
    summary, _ = qs.build_summary(data, qs.HistoryView(state="empty"), NOW), None
    flags = summary["groups"][0]["restrictions"]
    assert flags["signed_out"]["accounts"] == 2  # b and c, never a
    view = plugin.build_view(data, "", NOW)
    assert [a["signed_in"] for a in view["groups"][0]["accounts"]] == [None, False, False]


# --- resources ------------------------------------------------------------------


def facet(value, freshness="fresh"):
    return {"value": value, "source": "fixture", "observed_at": "2026-10-15T10:00:00Z", "freshness": freshness,
            "last_attempt_at": None, "last_error": None}


RESOURCES = {"resources": [{
    "target": {"harness": "cursor", "profile_id": "c1"},
    "balances": facet([{"id": "inc", "label": "Included", "amount": "0", "unit": "usd", "currency": "USD",
                        "decimal_places": 2, "has_balance": True, "unlimited": False, "scope": "team",
                        "usage_kind": "included", "period": {"starts_at": "2026-10-01T00:00:00Z", "ends_at": None},
                        "fee_included": None},
                       {"id": "bonus", "label": "Bonus", "amount": None, "unit": "usd", "currency": None,
                        "decimal_places": None, "has_balance": None, "unlimited": None}]),
    "spending": facet(None, "unknown"),
    "resets": facet([{"id": "r", "kind": "granted_reset", "label": "Reset", "description": None,
                      "available_count": 2, "eligible": True, "usable_now": True, "reason": None,
                      "resets_at": None, "weekly_limit_applies": True, "grants": None}], "stale"),
    "diagnostics": facet([]),
}], "availability_scope": {"model": None, "requestKind": "text", "basis": "admission"}}


def test_resources_missing_is_not_zero_team_is_not_personal_and_stale_is_dated():
    view = qr.account_resources(RESOURCES, "cursor", "c1")
    inc, bonus = view["balances"]["rows"]
    assert inc["amount"] == "0" and "team figure" in inc["note"] and "not a block" in inc["note"]
    assert bonus["amount"] is None  # never 0
    assert view["spending"]["state"] == "not_reported" and view["spending"]["rows"] is None
    assert view["resets"]["state"] == "last_known" and view["resets"]["freshness"] == "stale"
    assert view["diagnostics"]["rows"] == [] and view["diagnostics"]["state"] == "current"
    assert qr.account_resources(RESOURCES, "cursor", "other") is None  # no row: nothing invented
    # a facet kept from an earlier read is never current
    assert qr.account_resources(RESOURCES, "cursor", "c1", kept=True)["balances"]["state"] == "last_known"


def test_family_resets_count_current_offers_only_and_never_add_to_a_window():
    rows = json.loads(json.dumps(RESOURCES))
    rows["resources"][0]["resets"]["freshness"] = "fresh"
    counted = qr.family_resets(rows, "cursor")
    assert counted == {"available": 2, "accounts_current": 1, "accounts_last_known": 0,
                       "accounts_not_reported": 0, "offers_uncounted": 0}
    assert qr.family_resets(RESOURCES, "cursor")["available"] == 0  # stale: not summed
    echo = qr.scope_echo(RESOURCES)
    assert echo["stated"] and echo["basis"] == "admission" and echo["request_kind"] == "text"
    assert qr.scope_echo({"availability_scope": {"model": None, "requestKind": "text", "basis": "quota_block"}})["basis"] == "quota_block"
    assert qr.scope_echo({"availability_scope": {"requestKind": "audio"}})["stated"] is False


# --- display set --------------------------------------------------------------


class _Api:
    def __init__(self, directory):
        self._dir = directory

    def get_state_dir(self):
        return str(self._dir)

    def log(self, *args):
        pass


def test_display_set_round_trips_and_removes_nothing_from_the_reserve(tmp_path):
    api = _Api(tmp_path)
    assert plugin.read_display(api) == {"hidden": []}
    saved, error = plugin.write_display(api, {"hidden": ["gpt-reserve", "gpt-reserve", 7, "x" * 500, "ok"]})
    assert error == "" and saved == {"hidden": ["gpt-reserve", "ok"]}
    assert plugin.read_display(api) == saved
    data = at_time(NOW)
    view = plugin.build_view(data, "", NOW)
    again = plugin.build_view(data, "", NOW)
    assert view == again  # display state is never an input of the account view
    assert plugin.write_display(api, {"hidden": []})[0] == {"hidden": []}  # reversible


# --- context snapshot ------------------------------------------------------------


def _answer(tmp_path):
    store = fed_store(tmp_path)
    data = at_time(NOW)
    latest = plugin.LatestRead()
    latest.put(data, "", NOW)
    effective, cached = latest.effective(data)
    return plugin.answer_of(store, effective, cached, data, NOW, NOW), store, latest, data


def test_snapshot_text_is_deterministic_bounded_and_matches_the_tool_numbers(tmp_path):
    answer, _store, _latest, _data = _answer(tmp_path)
    text = quota_context.snapshot_text(answer)
    assert text == quota_context.snapshot_text(answer) and len(text) <= quota_context.MAX_CHARS
    claude = answer["joint"]["claude"]
    assert claude["same_account_capacity"] == pytest.approx(0.5, abs=1e-3)
    assert (f"≈{round(claude['same_account_capacity'], 1):.1f} from {claude['accounts_counted']} "
            f"of {claude['accounts']}") in text
    assert f"≈{quota_context.rounded_hours(claude['conditional_account_hours'])} account-hours" in text
    for row in answer["groups"]:  # every limit's figure in the text is the tool's own
        assert f"{row['remaining_windows']:.2f} left" in text or "no current reading" in text
    # no relative age, no per-refresh clock, no shortened text
    assert not re.search(r"\b\d+\s*(min|h|s)\s+ago\b", text) and "[:" not in text
    tiny = quota_context.snapshot_text(answer, bound=700)
    assert len(tiny) <= 700 and "left out" in tiny and tiny.startswith(quota_context.HEADER)


def test_snapshot_is_written_once_renewed_near_expiry_and_never_for_a_failed_sweep(tmp_path):
    answer, store, latest, data = _answer(tmp_path)
    api = _Api(tmp_path)
    assert plugin.publish_snapshot(api, store, data, "", NOW, latest) == "written"
    path = tmp_path / quota_context.SNAPSHOT_FILE
    first = json.loads(path.read_text())
    assert first["schema_version"] == 1 and first["observed_at"] == qs.iso(NOW)
    assert qs.parse_instant(first["valid_until"]) <= NOW + quota_context.VALID_FOR_SEC
    # an unchanged text two minutes later is not rewritten: observed_at does not churn
    assert plugin.publish_snapshot(api, store, data, "", NOW + 120, latest) == "unchanged"
    assert json.loads(path.read_text()) == first
    # near the end of its validity the same text is renewed with a fresh observation
    assert plugin.publish_snapshot(api, store, data, "", NOW + 300, latest) == "written"
    renewed = json.loads(path.read_text())
    assert renewed["text"] == first["text"] and renewed["observed_at"] == qs.iso(NOW + 300)
    # a failed or unreadable sweep writes nothing at all: the file simply expires
    assert plugin.publish_snapshot(api, store, None, "boom", NOW + 600, latest) == "skipped"
    assert plugin.publish_snapshot(api, store, dict(data, reads={"catalog": "ok", "accounts": "ok", "quota": "failed"}),
                                   "", NOW + 600, latest) == "skipped"
    assert json.loads(path.read_text()) == renewed
    assert not list(tmp_path.glob("context-snapshot.json.*.tmp"))


def test_tool_route_and_snapshot_share_one_figure(tmp_path):
    answer, store, _latest, data = _answer(tmp_path)
    route = plugin.reserve_view(store, data, NOW, NOW, harness="claude", analytics=True)["chart"]["analytics"]["joint"]
    assert route["figure"] == answer["joint"]["claude"]["same_account_capacity"]
    assert route["hours"]["value"] == answer["joint"]["claude"]["conditional_account_hours"]
    assert (route["known"], route["accounts"]) == (answer["joint"]["claude"]["accounts_counted"],
                                                   answer["joint"]["claude"]["accounts"])
