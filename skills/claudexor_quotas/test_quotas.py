"""Tests for Claudexor quota normalization, routes, and the real module widget.

The Python cases cover projection and actual registered handlers. A bundled-Node
in-process harness executes widget.js itself (without a browser framework) for:
- Per-facet provenance and notes (ok, not_read, failed, indeterminate, missing reads).
- Timestamp and future-detection handling.
- Constraint views: out-of-range and NaN ratios refused (never clamped), unrounded
  percents with display-only rounding, and the at-limit verdict.
- Quota projection: fresh, stale, no-data, degraded facet, global exhaustion vs per-model caps.
- Verification view tones and "last known" degradation.
- Account and group structuring (native vs profile, next_up resolution, harness ordering).
- Fresh/stale/exhausted rendering, approved absence actions, exact-subject merging,
  passive polling, foreground refresh, old-host failure, ARIA/title honesty, and teardown.
- Full view building and transport error handling.
- Display preferences: what the skill agrees to remember, and what it drops.
"""

import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
import pytest

import plugin
from plugin import (
    DEFAULT_PREFS,
    FOLD_REASONS,
    MAX_MODEL_ENTRIES,
    clean_prefs,
    read_prefs,
    write_prefs,
    FACETS,
    READ_OK,
    facet_states,
    facet_note,
    _subject_key,
    _is_future,
    _constraint_view,
    _spent,
    _used_text,
    quota_for,
    verification_view,
    build_groups,
    build_view,
    build_quota_updates,
)


class TestFacetStates:
    def test_all_facets_ok(self):
        payload = {"reads": {"catalog": "ok", "accounts": "ok", "quota": "ok"}}
        assert facet_states(payload) == {
            "catalog": "ok",
            "accounts": "ok",
            "quota": "ok",
        }

    def test_mixed_facet_states(self):
        payload = {"reads": {"catalog": "ok", "accounts": "failed", "quota": "not_read"}}
        assert facet_states(payload) == {
            "catalog": "ok",
            "accounts": "failed",
            "quota": "not_read",
        }

    def test_invalid_facet_values_become_indeterminate(self):
        payload = {"reads": {"catalog": "unknown", "accounts": None, "quota": 123}}
        assert facet_states(payload) == {
            "catalog": "indeterminate",
            "accounts": "indeterminate",
            "quota": "indeterminate",
        }

    def test_missing_or_non_dict_payload_is_indeterminate(self):
        assert facet_states(None) == {f: "indeterminate" for f in FACETS}
        assert facet_states({}) == {f: "indeterminate" for f in FACETS}
        assert facet_states({"reads": "invalid"}) == {f: "indeterminate" for f in FACETS}


class TestFacetNote:
    def test_all_ok_returns_empty_string(self):
        states = {"catalog": "ok", "accounts": "ok", "quota": "ok"}
        assert facet_note(states) == ""

    def test_degraded_facets_listed(self):
        states = {"catalog": "ok", "accounts": "failed", "quota": "not_read"}
        note = facet_note(states)
        assert "accounts: failed" in note
        assert "quota: not_read" in note
        assert "catalog" not in note


class TestSubjectKey:
    def test_subject_key_normalizes_null_and_empty(self):
        assert _subject_key(None) == ""
        assert _subject_key("") == ""
        assert _subject_key("   ") == ""

    def test_subject_key_preserves_strings(self):
        assert _subject_key("prof_123") == "prof_123"
        assert _subject_key(" prof_abc ") == "prof_abc"


class TestIsFuture:
    def test_future_iso_timestamp(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()
        assert _is_future(future) is True

    def test_past_iso_timestamp(self):
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
        assert _is_future(past) is False

    def test_iso_with_z_suffix(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        assert _is_future(future) is True

    def test_invalid_and_empty_returns_none(self):
        assert _is_future("") is None
        assert _is_future(None) is None
        assert _is_future("not-a-timestamp") is None


class TestConstraintView:
    def test_valid_ratio_converted_to_pct(self):
        c = {
            "id": "c1",
            "label": "5-hour window",
            "used_ratio": 0.456,
            "window_seconds": 18000,
            "resets_at": "2026-08-16T00:00:00Z",
            "cooldown_until": "",
            "applies_to_models": ["claude-3-opus"],
        }
        view = _constraint_view(c)
        assert view["label"] == "5-hour window"
        # The unrounded percent for bars and tones; the words round for the eye.
        assert view["used_pct"] == 45.6
        assert view["used_text"] == "45.6"
        assert view["at_limit"] is False and view["ratio_problem"] == ""
        assert view["window_seconds"] == 18000
        assert view["scoped_models"] == ["claude-3-opus"]

    def test_out_of_range_ratio_is_refused_never_clamped(self):
        # The reserve refuses these (quota_summary.ratio_of); the account view
        # used to clamp 1.5 into a "100%" nobody reported.
        for bad, problem in ((1.5, "out_of_range"), (-0.2, "out_of_range"), ("0.5", "not_a_number"),
                             (True, "not_a_number")):
            view = _constraint_view({"used_ratio": bad}, at_limit=True)
            assert view["used_pct"] is None and view["used_text"] is None, bad
            assert view["ratio_problem"] == problem and view["at_limit"] is False, bad

    def test_missing_or_nan_ratio(self):
        assert _constraint_view({"used_ratio": None})["used_pct"] is None
        assert _constraint_view({"used_ratio": None})["ratio_problem"] == ""
        assert _constraint_view({"used_ratio": float("nan")})["used_pct"] is None
        assert _constraint_view({"used_ratio": float("nan")})["ratio_problem"] == "not_finite"
        assert _constraint_view({})["used_pct"] is None

    @pytest.mark.parametrize(("ratio", "text"), [
        (0.996, "99.6"), (1.0, "100"), (1 - 5e-10, "100"), (0.9999999, "<100"), (0.9996, "<100"),
        (0.57, "57"), (0.0, "0"), (1e-9, ">0"), (0.0004, ">0"),
        (0.456, "45.6"), (0.4, "40"), (0.004, "0.4"),
    ])
    def test_display_rounding_never_reads_as_full_or_empty(self, ratio, text):
        assert _used_text(ratio) == text

    def test_a_full_last_known_reading_still_reads_full_without_a_verdict(self):
        # Stale or not current: the known fact is printed as reported, and
        # carries no spent verdict (the caller gives none).
        view = _constraint_view({"used_ratio": 1.0})
        assert view["used_text"] == "100" and view["at_limit"] is False


class TestSpentLogic:
    def test_spent_only_on_the_at_limit_verdict_not_a_rounded_percent(self):
        assert _spent({"used_pct": 100.0, "at_limit": True, "cooldown_until": ""}) is True
        # 99.6% used rounds to "100" on a whole-percent screen; it is not spent.
        assert _spent({"used_pct": 99.6, "at_limit": False, "cooldown_until": ""}) is False
        assert _spent({"used_pct": 100, "cooldown_until": ""}) is False

    def test_a_live_cooldown_is_not_a_spent_share(self):
        # 0.6.1 account closure: a cooldown is carried as a cooldown of its
        # own (quota["cooldowns"]), never as a window at its limit.
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()
        assert _spent({"used_pct": 10, "cooldown_until": future}) is False

    def test_not_spent_when_cooldown_in_past(self):
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat()
        assert _spent({"used_pct": 50, "cooldown_until": past}) is False

    def test_an_unparseable_cooldown_is_not_a_spent_share(self):
        assert _spent({"used_pct": 20, "cooldown_until": "corrupt-date"}) is False


AUG_NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc).timestamp()
AUG_OBSERVED = "2026-08-15T11:59:00Z"


class TestQuotaFor:
    def test_quota_facet_not_ok_returns_not_checked(self):
        res = quota_for([], "claude", "prof_1", "failed")
        assert res["state"] == "not_checked"
        assert "Limits not checked" in res["label"]
        assert res["constraints"] == []

    def test_no_data_when_no_matching_snapshots(self):
        snapshots = [
            {"subject": {"harness": "cursor", "subject_id": "prof_1"}, "freshness": "fresh"}
        ]
        res = quota_for(snapshots, "claude", "prof_1", READ_OK)
        assert res["state"] == "no_data"
        assert "No quota window reported" in res["label"]

    def test_stale_snapshot_reported_without_gating(self):
        snapshots = [
            {
                "subject": {"harness": "claude", "subject_id": "prof_1"},
                "freshness": "stale",
                "observed_at": "2026-08-15T12:00:00Z",
                "constraints": [{"label": "Weekly", "used_ratio": 0.8}],
            }
        ]
        res = quota_for(snapshots, "claude", "prof_1", READ_OK)
        assert res["state"] == "no_fresh_window"
        assert len(res["stale"]) == 1
        assert res["stale"][0]["freshness"] == "stale"
        assert res["constraints"] == []

    def test_fresh_quota_ok_state(self):
        snapshots = [
            {
                "subject": {"harness": "claude", "subject_id": ""},
                "freshness": "fresh",
                "observed_at": AUG_OBSERVED,
                "availability": {"state": "available"},
                "constraints": [
                    {
                        "label": "Hourly",
                        "used_ratio": 0.35,
                        "resets_at": "2026-08-15T22:00:00Z",
                        "applies_to_models": [],
                    },
                    {
                        "label": "Daily",
                        "used_ratio": 0.70,
                        "resets_at": "2026-08-16T00:00:00Z",
                        "applies_to_models": [],
                    },
                ],
            }
        ]
        res = quota_for(snapshots, "claude", "", READ_OK, now=AUG_NOW)
        assert res["state"] == "ok"
        assert "70% used" in res["label"]
        assert res["resets_at"] == "2026-08-16T00:00:00Z"
        assert len(res["constraints"]) == 2

    def test_fresh_quota_exhausted_global_constraint(self):
        snapshots = [
            {
                "subject": {"harness": "claude", "subject_id": "p1"},
                "freshness": "fresh",
                "observed_at": AUG_OBSERVED,
                "constraints": [
                    {
                        "label": "5-Hour",
                        "used_ratio": 1.0,
                        "resets_at": "2026-08-15T23:00:00Z",
                        "applies_to_models": [],
                    }
                ],
            }
        ]
        res = quota_for(snapshots, "claude", "p1", READ_OK, now=AUG_NOW)
        assert res["state"] == "exhausted"
        assert res["label"] == "Limit reached"
        assert res["resets_at"] == "2026-08-15T23:00:00Z"

    def test_per_model_scoped_exhaustion_does_not_exhaust_account(self):
        snapshots = [
            {
                "subject": {"harness": "claude", "subject_id": "p1"},
                "freshness": "fresh",
                "observed_at": AUG_OBSERVED,
                "constraints": [
                    {
                        "label": "Opus Cap",
                        "used_ratio": 1.0,
                        "resets_at": "2026-08-16T00:00:00Z",
                        "applies_to_models": ["claude-3-opus"],
                    },
                    {
                        "label": "General Cap",
                        "used_ratio": 0.40,
                        "resets_at": "2026-08-16T04:00:00Z",
                        "applies_to_models": [],
                    },
                ],
            }
        ]
        res = quota_for(snapshots, "claude", "p1", READ_OK, now=AUG_NOW)
        assert res["state"] == "ok"
        assert "40% used" in res["label"]
        assert "per-model caps spent: Opus Cap" in res["note"]

    def test_exact_subject_matching_isolation(self):
        snapshots = [
            {
                "subject": {"harness": "claude", "subject_id": None},
                "freshness": "fresh",
                "observed_at": AUG_OBSERVED,
                "constraints": [{"label": "Native", "used_ratio": 0.99}],
            },
            {
                "subject": {"harness": "claude", "subject_id": "profile_1"},
                "freshness": "fresh",
                "observed_at": AUG_OBSERVED,
                "constraints": [{"label": "Profile", "used_ratio": 0.10}],
            },
        ]
        native_res = quota_for(snapshots, "claude", "", READ_OK, now=AUG_NOW)
        profile_res = quota_for(snapshots, "claude", "profile_1", READ_OK, now=AUG_NOW)
        assert native_res["constraints"][0]["label"] == "Native"
        assert profile_res["constraints"][0]["label"] == "Profile"

    @pytest.mark.parametrize(
        ("reason", "retry_ms", "action_kind"),
        [
            ("not_logged_in", None, "sign_in_if_unverified"),
            ("auth_revoked", None, "sign_in_if_unverified"),
            ("no_source", None, "source_missing"),
            ("rate_limited", 240_000, "retry"),
            ("rate_limited", None, ""),
            ("transport_unavailable", None, ""),
            ("platform_unsupported", None, ""),
            ("refresh_failed", None, ""),
            ("probe_skipped_rate_limited", None, ""),
            ("poll_paced", None, ""),
            ("credential_profile_ambiguous", None, ""),
        ],
    )
    def test_typed_absence_mapping_is_generic(self, reason, retry_ms, action_kind):
        absence = {
            "subject": {"harness": "claude", "subject_id": "p1"},
            "reason": reason,
            "detail": "/private/secret/path vendor body",
            "observed_at": "2026-09-01T08:00:00+00:00",
        }
        if retry_ms is not None:
            absence["retry_after_ms"] = retry_ms
        result = quota_for([], "claude", "p1", READ_OK, [absence])
        assert result["absence"]["message"] == "Quota temporarily unavailable"
        assert result["absence"]["action_kind"] == action_kind
        visible_model = json.dumps(result)
        assert reason not in visible_model
        assert "/private/secret/path" not in visible_model
        assert "vendor body" not in visible_model

    def test_refresh_skip_supplies_retry_without_erasing_stale(self):
        stale = {
            "subject": {"harness": "claude", "subject_id": "p1"},
            "freshness": "stale",
            "observed_at": "2026-09-01T08:00:00+00:00",
            "constraints": [{
                "label": "Weekly",
                "used_ratio": 0.83,
                "cooldown_until": "2099-09-01T09:00:00+00:00",
            }],
        }
        result = quota_for(
            [stale],
            "claude",
            "p1",
            READ_OK,
            [{
                "subject": {"harness": "claude", "subject_id": "p1"},
                "reason": "poll_paced",
                "observed_at": "2026-09-01T08:01:00+00:00",
            }],
            [{"vendor": "claude", "not_before": "2099-09-01T08:05:00+00:00"}],
        )
        # 0.6.1 account closure: the stale reading's live cooldown holds the
        # account, as it does in the reserve's "cooling" restriction.
        assert (result["state"], result["label"]) == ("cooling", "Cooling down")
        assert [(c["freshness"], c["until"]) for c in result["cooldowns"]] == [("stale", "2099-09-01T09:00:00Z")]
        assert result["stale"][0]["constraints"][0]["used_pct"] == 83
        assert result["absence"]["action_kind"] == "retry"
        assert result["absence"]["retry_at"] == "2099-09-01T08:05:00+00:00"
        assert "do not grant routing" in result["note"]
        assert "cooldown evidence may still deny or rank" in result["note"]

    def test_vendor_refresh_skip_discloses_fresh_snapshot_is_last_known(self):
        fresh = {
            "subject": {"harness": "claude", "subject_id": "p1"},
            "freshness": "fresh",
            "observed_at": "2026-09-01T08:00:00+00:00",
            "constraints": [{"label": "Weekly", "used_ratio": 0.41}],
        }
        result = quota_for(
            [fresh],
            "claude",
            "p1",
            READ_OK,
            [],
            [{"vendor": "claude", "not_before": "2099-09-01T08:05:00+00:00"}],
        )
        assert result["state"] == "ok"
        assert result["constraints"][0]["used_pct"] == 41
        assert result["absence"]["action_kind"] == "retry"
        assert result["absence"]["retry_at"] == "2099-09-01T08:05:00+00:00"

    def test_vendor_refresh_skip_raises_older_rate_limit_deadline(self):
        result = quota_for(
            [],
            "claude",
            "p1",
            READ_OK,
            [{
                "subject": {"harness": "claude", "subject_id": "p1"},
                "reason": "rate_limited",
                "observed_at": "2099-09-01T08:00:00+00:00",
                "retry_after_ms": 300_000,
            }],
            [{"vendor": "claude", "not_before": "2099-09-01T08:20:00+00:00"}],
        )
        assert result["absence"]["action_kind"] == "retry"
        assert result["absence"]["retry_at"] == "2099-09-01T08:20:00+00:00"

    def test_vendor_refresh_skip_does_not_replace_subject_sign_in_action(self):
        result = quota_for(
            [],
            "claude",
            "p1",
            READ_OK,
            [{
                "subject": {"harness": "claude", "subject_id": "p1"},
                "reason": "auth_revoked",
                "observed_at": "2026-09-01T08:00:00+00:00",
            }],
            [{"vendor": "claude", "not_before": "2099-09-01T08:05:00+00:00"}],
        )
        assert result["absence"]["action_kind"] == "sign_in_if_unverified"
        assert result["absence"]["retry_at"] == ""


def _fresh(sid, constraints, *, source="claude_oauth_usage", observed="2026-08-15T11:59:00Z"):
    row = {"subject": {"harness": "claude", "subject_id": sid}, "freshness": "fresh",
           "source": source, "constraints": constraints}
    if observed is not None:
        row["observed_at"] = observed
    return row


def _window(ratio, reset="2026-08-15T16:00:00Z", **extra):
    return dict({"id": "five_hour", "label": "5 hour", "used_ratio": ratio,
                 "window_seconds": 18000, "resets_at": reset}, **extra)


class TestAccountViewUsesTheReserveRules:
    """The account view draws a current window only for the reading the
    reserve overview counts (quota_summary.reading_of / resolve_member):
    the same validity, the same reset rule, the same source policy. Rounding
    is for display; the spent verdict is taken on the unrounded share."""

    def test_nearly_full_is_not_the_limit(self):
        res = quota_for([_fresh("p", [_window(0.996)])], "claude", "p", READ_OK, now=AUG_NOW)
        assert res["state"] == "ok" and res["label"] == "99.6% used"
        view = res["constraints"][0]
        assert view["used_text"] == "99.6" and view["at_limit"] is False
        assert view["used_pct"] == pytest.approx(99.6)

    def test_exactly_full_is_the_limit(self):
        res = quota_for([_fresh("p", [_window(1.0)])], "claude", "p", READ_OK, now=AUG_NOW)
        assert res["state"] == "exhausted" and res["label"] == "Limit reached"
        assert res["constraints"][0]["at_limit"] is True and res["constraints"][0]["used_text"] == "100"

    @pytest.mark.parametrize(("constraint", "observed", "why"), [
        (_window(1.4), "2026-08-15T11:59:00Z", "ratio outside 0–100%"),
        (_window("0.4"), "2026-08-15T11:59:00Z", "ratio not a number"),
        (_window(0.3, reset="2026-08-15T11:00:00Z"), "2026-08-15T10:59:00Z", "its reported reset has passed"),
        (_window(0.3), None, "no observation time"),
        (_window(0.3), "2026-08-15T13:00:00Z", "observed in the future"),
    ])
    def test_a_reading_the_reserve_refuses_is_not_current(self, constraint, observed, why):
        res = quota_for([_fresh("p", [constraint], observed=observed)], "claude", "p", READ_OK, now=AUG_NOW)
        assert res["state"] == "not_current" and res["label"] == "No current reading — " + why
        assert res["constraints"] == []  # no current bar, no spent verdict
        assert "Limit reached" not in json.dumps(res)
        aside = res["stale"][0]
        assert aside["why"] == why and aside["freshness"] == "fresh"
        # The known fact stays in view, as it was reported (never clamped).
        view = aside["constraints"][0]
        assert view["at_limit"] is False
        if constraint["used_ratio"] == 1.4:
            assert view["used_pct"] is None and view["ratio_problem"] == "out_of_range"
        elif isinstance(constraint["used_ratio"], float):
            assert view["used_pct"] == pytest.approx(30.0)

    def test_sources_that_disagree_at_one_moment_draw_no_current_bar(self):
        rows = [_fresh("p", [_window(0.2)], source="a"), _fresh("p", [_window(0.5)], source="b")]
        res = quota_for(rows, "claude", "p", READ_OK, now=AUG_NOW)
        assert res["state"] == "not_current" and res["label"] == "No current reading — its sources disagree"
        assert res["constraints"] == []
        assert sorted(v["used_pct"] for e in res["stale"] for v in e["constraints"]) == [20.0, 50.0]

    def test_two_sources_of_one_limit_are_one_window_the_newest(self):
        rows = [_fresh("p", [_window(0.2)], source="a", observed="2026-08-15T11:50:00Z"),
                _fresh("p", [dict(_window(0.3), id="claude:five_hour")], source="b")]
        res = quota_for(rows, "claude", "p", READ_OK, now=AUG_NOW)
        assert res["state"] == "ok" and res["label"] == "30% used"
        assert len(res["constraints"]) == 1 and res["stale"] == []

    def test_a_window_with_no_ratio_stays_a_window_without_a_bar(self):
        res = quota_for([_fresh("p", [_window(None), {"id": "reset_credits", "label": "1 reset credit"}])],
                        "claude", "p", READ_OK, now=AUG_NOW)
        assert res["state"] == "no_data"
        assert [v["label"] for v in res["constraints"]] == ["5 hour", "1 reset credit"]
        assert all(v["used_pct"] is None and v["ratio_problem"] == "" for v in res["constraints"])

    def test_a_live_cooldown_still_counts_where_the_share_does_not(self):
        future = (dt.datetime.fromtimestamp(AUG_NOW, dt.timezone.utc) + dt.timedelta(days=400)).isoformat()
        res = quota_for([_fresh("p", [_window(1.4, cooldown_until=future)])], "claude", "p", READ_OK,
                        now=AUG_NOW)
        # The cooldown is its own reported fact with its own time; the share
        # beside it is refused and never read as full. It is "Cooling down"
        # until its end — not "Limit reached", and its end is not a reset.
        assert (res["state"], res["label"], res["resets_at"]) == ("cooling", "Cooling down", "")
        assert res["cooling_until"] == plugin.qs.iso(plugin.qs.parse_instant(future))
        assert [(c["scope"], c["until_note"]) for c in res["cooldowns"]] == [("account", "")]
        assert "Limit reached" not in json.dumps(res)
        assert res["constraints"] == [] and res["stale"][0]["constraints"][0]["used_pct"] is None

    def test_a_healthy_neighbour_is_unaffected(self):
        rows = [_fresh("bad", [_window(1.4)]), _fresh("ok", [_window(0.3)])]
        assert quota_for(rows, "claude", "ok", READ_OK, now=AUG_NOW)["label"] == "30% used"
        assert quota_for(rows, "claude", "bad", READ_OK, now=AUG_NOW)["state"] == "not_current"


class TestVerificationView:
    def test_vendor_live_passed(self):
        view = verification_view("passed", "vendor", READ_OK, signed_in=True)
        assert view["tone"] == "ok"
        assert view["label"] == "Verified live"

    def test_local_store_passed(self):
        view = verification_view("passed", "local_store", READ_OK, signed_in=True)
        assert view["tone"] == "muted"
        assert "not verified live (local_store)" in view["label"]

    def test_failed_verification(self):
        view = verification_view("failed", "vendor", READ_OK, signed_in=False)
        assert view["tone"] == "warn"
        assert view["label"] == "Verification failed"

    def test_a_failed_check_projects_signed_out_and_failed_together(self):
        """The shape the widget matrix's "both-one" stands on: a failed check
        with the profile not available is signed_in false and tone warn at
        once — two reasons on one account."""
        row = {"profile": {"profile_id": "both", "harness_id": "claude", "enabled": True},
               "status": {"verification": "failed", "verification_source": "vendor",
                          "availability": "unavailable"}}
        account = plugin._profile_account(row, [], [], "claude", READ_OK, READ_OK)
        assert account["signed_in"] is False
        assert account["verification_state"] == "failed"
        assert account["verification"] == {"tone": "warn", "label": "Verification failed"}

    def test_degraded_accounts_facet_appends_last_known(self):
        view = verification_view("passed", "vendor", "failed", signed_in=True)
        assert "last known" in view["label"]
        assert view["tone"] == "muted"


class TestBuildGroupsAndView:
    def test_build_groups_native_and_profiles(self):
        payload = {
            "harnesses": [
                {
                    "id": "claude",
                    "display_name": "Anthropic Claude",
                    "status": "ready",
                    "enabled": True,
                    "provider_family": "anthropic",
                }
            ],
            "profiles": {
                "harnessAccounts": [
                    {
                        "harness_id": "claude",
                        "native_credentials_enabled": True,
                        "native_login_detected": True,
                        "identity": {"email": "user@example.com", "plan": "Pro"},
                        "next_up": {"kind": "native"},
                    }
                ],
                "profiles": [
                    {
                        "profile": {
                            "profile_id": "prof_1",
                            "harness_id": "claude",
                            "display_name": "Work Account",
                            "credential_kind": "oauth",
                            "enabled": True,
                        },
                        "status": {
                            "verification": "passed",
                            "verification_source": "vendor",
                            "availability": "available",
                        },
                        "identity": {"email": "work@company.com", "plan": "Team"},
                    }
                ],
            },
            "quota": [
                {
                    "subject": {"harness": "claude", "subject_id": None},
                    "freshness": "fresh",
                    "observed_at": "2026-09-01T08:00:00Z",
                    "constraints": [{"label": "Session", "used_ratio": 0.2}],
                }
            ],
            "reads": {"catalog": "ok", "accounts": "ok", "quota": "ok"},
            "daemon": {"state": "running", "engine_version": "3.3.15"},
        }
        states = facet_states(payload)
        groups = build_groups(payload, states)
        assert len(groups) == 1
        group = groups[0]
        assert group["harness_id"] == "claude"
        assert group["family_label"] == "Anthropic Claude"
        assert len(group["accounts"]) == 2

        native_acc = group["accounts"][0]
        assert native_acc["kind"] == "native"
        assert native_acc["subject_id"] is None
        assert native_acc["next_up"] is True
        assert native_acc["quota"]["state"] == "ok"

        prof_acc = group["accounts"][1]
        assert prof_acc["kind"] == "profile"
        assert prof_acc["subject_id"] == "prof_1"
        assert prof_acc["verified_live"] is True
        assert prof_acc["next_up"] is False
        assert prof_acc["verification"]["label"] == "Verified live"
        assert prof_acc["quota"]["state"] == "no_data"

    def test_build_view_with_transport_error(self):
        view = build_view(None, "Connection refused")
        assert view["ok"] is False
        assert view["transport_error"] == "Connection refused"
        assert view["facets"] == {f: "indeterminate" for f in FACETS}
        assert view["groups"] == []

    def test_build_view_keeps_auth_data_but_projects_quota_age(self):
        payload = {
            "reads": {"catalog": "ok", "accounts": "ok", "quota": "ok"},
            "harnesses": [{"id": "claude", "display_name": "Claude"}],
            "profiles": {
                "harnessAccounts": [],
                "profiles": [{
                    "profile": {
                        "profile_id": "p1",
                        "harness_id": "claude",
                        "display_name": "Personal",
                        "enabled": True,
                    },
                    "status": {
                        "verification": "passed",
                        "verification_source": "vendor",
                        "availability": "available",
                        "last_verified_at": "2026-09-01T07:00:00+00:00",
                    },
                }],
            },
            "quota": [{
                "subject": {"harness": "claude", "subject_id": "p1"},
                "freshness": "fresh",
                "observed_at": "2026-09-01T08:00:00+00:00",
                "constraints": [{"label": "Weekly", "used_ratio": 0.3}],
            }],
            "quota_absences": [],
        }
        account = build_view(payload, "")["groups"][0]["accounts"][0]
        assert account["last_verified_at"] == "2026-09-01T07:00:00+00:00"
        assert account["verification_state"] == "passed"
        assert account["verification_source"] == "vendor"
        assert account["quota"]["observed_at"] == "2026-09-01T08:00:00+00:00"

    def test_failed_accounts_read_cannot_suppress_auth_revoked_action(self):
        payload = {
            "reads": {"catalog": "ok", "accounts": "failed", "quota": "ok"},
            "harnesses": [{"id": "claude", "display_name": "Claude"}],
            "profiles": {
                "harnessAccounts": [],
                "profiles": [{
                    "profile": {
                        "profile_id": "p1",
                        "harness_id": "claude",
                        "display_name": "Personal",
                        "enabled": True,
                    },
                    "status": {
                        "verification": "passed",
                        "verification_source": "vendor",
                        "availability": "available",
                        "last_verified_at": "2026-09-01T07:00:00+00:00",
                    },
                }],
            },
            "quota": [],
            "quota_absences": [{
                "subject": {"harness": "claude", "subject_id": "p1"},
                "reason": "auth_revoked",
                "observed_at": "2026-09-01T08:00:00+00:00",
            }],
        }
        account = build_view(payload, "")["groups"][0]["accounts"][0]
        assert account["verification_state"] == "passed"
        assert account["verification_source"] == "vendor"
        assert account["last_verified_at"] == "2026-09-01T07:00:00+00:00"
        assert account["verification"] == {
            "tone": "muted",
            "label": "Verified live — last known",
        }
        assert account["verified_live"] is False
        assert account["quota"]["absence"] == {
            "message": "Quota temporarily unavailable",
            "action_kind": "sign_in_if_unverified",
            "retry_at": "",
            "observed_at": "2026-09-01T08:00:00+00:00",
        }


def test_foreground_updates_are_exact_subject_quota_only():
    payload = {
        "snapshots": [
            {
                "subject": {"harness": "claude", "subject_id": None},
                "freshness": "fresh",
                "observed_at": "2026-09-01T08:00:00+00:00",
                "constraints": [{"label": "Native", "used_ratio": 0.9}],
            },
            {
                "subject": {"harness": "claude", "subject_id": "p1"},
                "freshness": "fresh",
                "observed_at": "2026-09-01T08:01:00+00:00",
                "constraints": [{"label": "Named", "used_ratio": 0.2}],
            },
        ],
        "absences": [],
        "refreshed_at": "2026-09-01T08:02:00+00:00",
    }
    result = build_quota_updates(payload)
    assert [(row["harness"], row["subject_id"]) for row in result["quota_updates"]] == [
        ("claude", None),
        ("claude", "p1"),
    ]
    assert result["quota_updates"][0]["quota"]["constraints"][0]["label"] == "Native"
    assert result["quota_updates"][1]["quota"]["constraints"][0]["label"] == "Named"


def test_foreground_update_rejects_malformed_success_envelope():
    assert build_quota_updates({}) == {
        "ok": False,
        "message": "Live quota refresh returned an invalid response",
    }


class _MockAPI:
    """A MOCKED host: it records registrations the way a worker process does
    and never starts a supervised task. The real host lifecycle (publication,
    cancellation on disable/unload) is exercised by the parent's live checks."""

    def __init__(self, state_dir=None):
        self.routes = {}
        self.tabs = {}
        self.tools = {}
        self.tasks = []
        self.unload = []
        self.logs = []
        self._state_dir = state_dir

    def get_runtime_info(self):
        return {"server_port": 8765}

    def get_state_dir(self):
        if self._state_dir is None:
            raise RuntimeError("no state dir in this mock")
        return str(self._state_dir)

    def register_route(self, name, handler, methods=("GET",)):
        self.routes[name] = {"handler": handler, "methods": methods}

    def register_ui_tab(self, tab_id, title, icon=None, render=None):
        self.tabs[tab_id] = {"title": title, "icon": icon, "render": render}

    def register_tool(self, name, handler, *, description, schema, timeout_sec=60):
        self.tools[name] = {"handler": handler, "description": description,
                            "schema": schema, "timeout_sec": timeout_sec}

    def register_supervised_task(self, name, factory, *, restart_policy="on_failure",
                                 max_restarts=5, backoff_seconds=2.0):
        self.tasks.append({"name": name, "factory": factory, "restart_policy": restart_policy,
                           "max_restarts": max_restarts, "backoff_seconds": backoff_seconds})

    def on_unload(self, callback):
        self.unload.append(callback)

    def log(self, level, message):
        self.logs.append((level, message))


def test_real_plugin_routes_keep_get_passive_and_post_foreground(monkeypatch):
    calls = []
    status_payload = {
        "reads": {"catalog": "ok", "accounts": "ok", "quota": "ok"},
        "harnesses": [],
        "profiles": {"harnessAccounts": [], "profiles": []},
        "quota": [],
        "quota_absences": [],
    }

    def fake_request(_port, path, method="GET", timeout_sec=plugin.STATUS_TIMEOUT_SEC):
        calls.append((method, path, timeout_sec))
        if method == "GET":
            return status_payload, "", 200
        return {"snapshots": [], "absences": [], "refreshed_at": None}, "", 200

    monkeypatch.setattr(plugin, "_request_json", fake_request)
    api = _MockAPI()
    plugin.register(api)
    assert api.routes["quotas"]["methods"] == ("GET",)
    assert api.tabs["quotas"]["render"]["appearance"] == "host"
    assert api.routes["refresh"]["methods"] == ("POST",)

    assert api.routes["quotas"]["handler"]({})["ok"] is True
    assert calls == [("GET", plugin.STATUS_PATH, plugin.STATUS_TIMEOUT_SEC)]
    assert api.routes["refresh"]["handler"]({})["ok"] is True
    assert calls == [
        ("GET", plugin.STATUS_PATH, plugin.STATUS_TIMEOUT_SEC),
        ("POST", plugin.REFRESH_PATH, plugin.REFRESH_TIMEOUT_SEC),
    ]


def test_real_plugin_old_host_failure_is_honest_and_does_not_get(monkeypatch):
    calls = []

    def old_host(_port, path, method="GET", timeout_sec=plugin.STATUS_TIMEOUT_SEC):
        calls.append((method, path, timeout_sec))
        return None, f"HTTP 404 from {path}", 404

    monkeypatch.setattr(plugin, "_request_json", old_host)
    api = _MockAPI()
    plugin.register(api)
    result = api.routes["refresh"]["handler"]({})
    assert result == {
        "ok": False,
        "compatibility_error": True,
        "message": "Live refresh requires a newer Ouroboros host",
    }
    assert calls == [
        ("POST", plugin.REFRESH_PATH, plugin.REFRESH_TIMEOUT_SEC),
    ]


NODE_WIDGET_MATRIX = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const widgetSource = fs.readFileSync(process.env.WIDGET_PATH, 'utf8');
const instrumentedWidgetSource = widgetSource.replace(
  '    start();\n})();',
  '    window.__quotaTest = { keptView: keptView };\n    start();\n})();',
);
assert.notEqual(instrumentedWidgetSource, widgetSource, 'widget test hook insertion failed');

class Element {
  constructor(tag, document) {
    this.tagName = String(tag).toUpperCase();
    this.ownerDocument = document;
    this.childNodes = [];
    this.parentNode = null;
    this.className = '';
    this.attributes = {};
    this.listeners = {};
    this.style = {};
    // className is the whole of it here, so classList reads and writes that
    // string. The widget toggles one class on #root while the settings panel
    // is open, and a stub without it would fail on a standard DOM call.
    this.classList = {
      contains: (name) => this.className.split(/\s+/).includes(name),
      add: (name) => {
        if (!this.classList.contains(name)) {
          this.className = (this.className ? this.className + ' ' : '') + name;
        }
      },
      remove: (name) => {
        this.className = this.className.split(/\s+/).filter((x) => x && x !== name).join(' ');
      },
      toggle: (name, force) => {
        const on = force === undefined ? !this.classList.contains(name) : !!force;
        if (on) this.classList.add(name); else this.classList.remove(name);
        return on;
      },
    };
    this.disabled = false;
    this._text = '';
  }
  set id(value) {
    this.attributes.id = String(value);
    this.ownerDocument.ids[String(value)] = this;
  }
  get id() { return this.attributes.id || ''; }
  set textContent(value) {
    this._text = value === undefined || value === null ? '' : String(value);
    this.childNodes.forEach((child) => { child.parentNode = null; });
    this.childNodes = [];
  }
  get textContent() {
    return this._text + this.childNodes.map((child) => child.textContent).join('');
  }
  get firstChild() { return this.childNodes[0] || null; }
  appendChild(child) {
    if (child.parentNode) {
      const at = child.parentNode.childNodes.indexOf(child);
      if (at >= 0) child.parentNode.childNodes.splice(at, 1);
    }
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  insertBefore(child, before) {
    if (!before) return this.appendChild(child);
    if (child.parentNode) {
      const old = child.parentNode.childNodes.indexOf(child);
      if (old >= 0) child.parentNode.childNodes.splice(old, 1);
    }
    const at = this.childNodes.indexOf(before);
    child.parentNode = this;
    this.childNodes.splice(at < 0 ? this.childNodes.length : at, 0, child);
    return child;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] === undefined ? null : this.attributes[name]; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  focus() { this.ownerDocument.activeElement = this; }
  querySelectorAll(selector) {
    const out = [];
    const visit = (node) => {
      node.childNodes.forEach((child) => {
        if (selector === '[data-focus]' && child.getAttribute('data-focus') !== null) out.push(child);
        visit(child);
      });
    };
    visit(this);
    return out;
  }
}

function makeDocument() {
  const document = {
    ids: {}, listeners: {}, visibilityState: 'visible', activeElement: null,
    createElement(tag) { return new Element(tag, document); },
    createElementNS(_ns, tag) { return new Element(tag, document); },
    createTextNode(text) { const node = new Element('#text', document); node._text = String(text); return node; },
    getElementById(id) { return document.ids[id] || null; },
    addEventListener(name, handler) { (document.listeners[name] ||= []).push(handler); },
    removeEventListener(name, handler) {
      document.listeners[name] = (document.listeners[name] || []).filter((item) => item !== handler);
    },
  };
  document.head = document.createElement('head');
  document.body = document.createElement('body');
  const root = document.createElement('div');
  root.id = 'root';
  document.body.appendChild(root);
  return { document, root };
}

function response(value, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(value),
  };
}

function walk(root) {
  const out = [root];
  root.childNodes.forEach((child) => out.push(...walk(child)));
  return out;
}

function byFocus(root, key) {
  return walk(root).find((node) => node.getAttribute('data-focus') === key);
}

function classes(root, name) {
  return walk(root).filter((node) => String(node.className || '').split(/\s+/).includes(name));
}

function allSpoken(root) {
  return walk(root).map((node) => [
    node.textContent,
    node.getAttribute('aria-label') || '',
    node.title || '',
  ].join(' ')).join(' ');
}

async function settle() {
  for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve));
}

async function boot(view, postValue) {
  const made = makeDocument();
  const calls = [];
  const windowListeners = {};
  const disposeHooks = [];
  let intervalCallback = null;
  let intervalCleared = false;
  const window = {
    fetch(url, options = {}) {
      const method = options.method || 'GET';
      // The body is recorded only when there is one: a GET call with an extra
      // undefined field no longer equals the call the older assertions expect.
      const call = { url, method };
      if (options.body !== undefined) call.body = options.body;
      calls.push(call);
      // A view may be a function of the request and every call so far, for
      // a host whose answer changes (after a Refresh, a removed account).
      return Promise.resolve(method === 'POST'
        ? response(postValue || { ok: true, quota_updates: [] })
        : response(typeof view === 'function' ? view(url, calls) : view));
    },
    setInterval(callback) { intervalCallback = callback; return 17; },
    clearInterval(id) { if (id === 17) intervalCleared = true; },
    addEventListener(name, handler) { (windowListeners[name] ||= []).push(handler); },
    setTimeout: (callback, ms) => setTimeout(callback, ms),
    // 0.7.0: the widget bounds every request with a backstop timer and
    // clears it when the request settles.
    clearTimeout: (id) => clearTimeout(id),
    __ouroWidgetOnDispose(fn) { disposeHooks.push(fn); },
  };
  const context = vm.createContext({
    window,
    document: made.document,
    console,
    Date,
    Math,
    Object,
    Array,
    String,
    Number,
    RegExp,
    Promise,
    setImmediate,
  });
  vm.runInContext(instrumentedWidgetSource, context, { filename: 'widget.js' });
  await settle();
  return {
    root: made.root,
    document: made.document,
    calls,
    windowListeners,
    disposeHooks,
    testHooks: window.__quotaTest,
    interval: () => intervalCallback,
    intervalCleared: () => intervalCleared,
  };
}

function quota(overrides = {}) {
  return Object.assign({
    state: 'ok', label: '30% used', resets_at: '', note: '',
    constraints: [{
      id: 'weekly', label: 'Weekly', used_pct: 30, resets_at: '',
      cooldown_until: '', scoped_models: [], window_seconds: 604800,
    }],
    stale: [], availability: 'available',
    observed_at: new Date(Date.now() - 120000).toISOString(), absence: null,
  }, overrides);
}

function account(subjectId, q, overrides = {}) {
  return Object.assign({
    key: 'claude:' + (subjectId || 'native'), kind: subjectId ? 'profile' : 'native',
    subject_id: subjectId || '', label: subjectId ? 'Same label' : 'Same label',
    caption: 'oauth', email: subjectId ? 'named@example.com' : 'native@example.com',
    plan: 'Pro', enabled: true, signed_in: true, next_up: false,
    last_verified_at: new Date(Date.now() - 3600000).toISOString(),
    verification_state: 'passed', verification_source: 'vendor', verified_live: true,
    verification: { tone: 'ok', label: 'Verified live' }, detail: '', quota: q,
  }, overrides);
}

function view(accounts) {
  return {
    ok: true, transport_error: '',
    facets: { catalog: 'ok', accounts: 'ok', quota: 'ok' }, facet_note: '',
    daemon: { state: 'running', engine_version: '3.9.4' },
    groups: [{
      harness_id: 'claude', family_label: 'Claude', harness_status: 'ok',
      harness_enabled: true, provider_family: 'anthropic', catalog_known: true,
      accounts, accounts_signed_in: accounts.length, accounts_unavailable: false,
    }],
  };
}

(async () => {
  const click = (env, key) => {
    const node = byFocus(env.root, key);
    assert.ok(node, 'missing control ' + key);
    node.listeners.click[0]({ stopPropagation() {} });
  };
  const inspector = (env) => classes(env.root, 'inspector')[0];
  const winLines = (env) => classes(inspector(env), 'win-line').map((node) => node.textContent);
  const stateOf = (env, key) => classes(byFocus(env.root, 'acct:' + key), 'acc-state')[0].textContent;

  // 0.8.0: with no reserve overview the account list stands open on its own,
  // nothing is selected until a row is clicked, and the selected account
  // shows its windows, its check and the age of its quota reading.
  let env = await boot(view([account('p1', quota())]));
  assert.equal(inspector(env), undefined, 'no account selects itself');
  assert.equal(classes(env.root, 'acc-row').length, 1);
  assert.equal(byFocus(env.root, 'accounts'), undefined, 'with no overview the list is not folded away');
  assert.equal(stateOf(env, 'claude:p1'), 'ready');
  click(env, 'acct:claude:p1');
  assert.match(inspector(env).textContent, /30% used/);
  assert.match(inspector(env).textContent, /Quota observed .* ago/);
  assert.match(inspector(env).textContent, /Verified live/);
  assert.equal(byFocus(env.root, 'acct:claude:p1').getAttribute('aria-pressed'), 'true');
  assert.doesNotMatch(allSpoken(env.root), /Checked |checked /);
  assert.equal(classes(env.root, 'stale').length, 0);
  click(env, 'inspector-clear');
  assert.equal(inspector(env), undefined, 'Clear drops the selection');
  assert.equal(byFocus(env.root, 'acct:claude:p1').getAttribute('aria-pressed'), 'false');

  env = await boot(view([account('p1', quota({
    state: 'no_data', label: 'No quota window reported', constraints: [],
  }))]));
  click(env, 'acct:claude:p1');
  assert.match(allSpoken(inspector(env)), /quota observed .* ago/i);
  assert.match(inspector(env).textContent, /No quota window reported/);
  assert.doesNotMatch(allSpoken(env.root), /Checked |checked /);

  // The same 100% constraint marked stale stays visible as a last-known
  // reading — muted, with its reported cooldown — never exhaustion red and
  // never a share held back now.
  const staleConstraint = {
    id: 'weekly', label: 'Weekly', used_pct: 100, resets_at: '',
    cooldown_until: new Date(Date.now() + 3600000).toISOString(),
    scoped_models: ['fable'], window_seconds: 604800,
  };
  env = await boot(view([account('p1', quota({
    state: 'no_fresh_window', label: 'No fresh reading — last reading is stale',
    constraints: [], observed_at: new Date(Date.now() - 360000).toISOString(),
    note: 'Stale percentages do not grant routing; live cooldown evidence may still deny or rank.',
    stale: [{
      observed_at: new Date(Date.now() - 360000).toISOString(),
      freshness: 'stale', source: 'claude_oauth_usage', constraints: [staleConstraint],
    }],
  }))]));
  assert.equal(stateOf(env, 'claude:p1'), 'no current reading');
  click(env, 'acct:claude:p1');
  const card = inspector(env).textContent;
  assert.match(card, /100% used/);
  assert.match(card, /not used to grant routing/);
  assert.match(card, /cooldown reported/);
  assert.match(card, /cooldown evidence may still deny or rank/);
  assert.ok(classes(env.root, 'win-line').some((node) => /\bstale\b/.test(String(node.className))));
  assert.equal(walk(env.root).filter((node) => /\bmeter\b.*\b(spent|restricted)\b/.test(String(node.className))).length, 0);
  assert.ok(walk(env.root).some((node) => /\bmeter\b.*\bstale\b/.test(String(node.className))));
  assert.doesNotMatch(env.root.textContent, /Limit reached|cooling down/);

  // Fresh exhaustion remains the distinct red state, in the list and the card.
  env = await boot(view([account('p1', quota({
    state: 'exhausted', label: 'Limit reached',
    constraints: [Object.assign({}, staleConstraint, { cooldown_until: '' })], stale: [],
  }))]));
  assert.equal(stateOf(env, 'claude:p1'), 'limit reached');
  click(env, 'acct:claude:p1');
  assert.match(inspector(env).textContent, /Limit reached/);
  // At the limit: no fill, only the red base — the reserve's own mark.
  const spentBars = walk(env.root).filter((node) => /\bmeter\b.*\bspent\b/.test(String(node.className)));
  assert.ok(spentBars.length);
  spentBars.forEach((b) => assert.equal(b.childNodes.length, 0));

  // Approved absence actions only — in the list's state and in the card —
  // with raw diagnostics excluded from text, ARIA and titles.
  for (const item of [
    ['sign_in_if_unverified', false, 'Sign-in required'],
    ['sign_in_if_unverified', true, null],
    ['source_missing', true, 'No live quota source'],
    ['retry', true, 'Retry after'],
    ['', true, null],
  ]) {
    const absence = {
      message: 'Quota temporarily unavailable', action_kind: item[0],
      retry_at: new Date(Date.now() + 240000).toISOString(),
      raw_reason: 'auth_revoked', detail: '/private/secret/path vendor body',
    };
    env = await boot(view([account('p1', quota({ absence }), {
      verified_live: item[1], detail: '/private/other/account/path vendor response body',
    })]));
    assert.match(stateOf(env, 'claude:p1'), /^quota unavailable/);
    if (item[2]) assert.match(stateOf(env, 'claude:p1'), new RegExp(item[2]));
    click(env, 'acct:claude:p1');
    const spoken = allSpoken(env.root);
    assert.match(spoken, /Quota temporarily unavailable/);
    if (item[2]) assert.match(spoken, new RegExp(item[2]));
    if (!item[2]) assert.doesNotMatch(spoken, /Sign-in required|No live quota source|Retry after/);
    assert.doesNotMatch(spoken, /auth_revoked|private\/secret|vendor body|private\/other/);
  }

  // A failed accounts facet makes a previous vendor pass last-known only, so
  // a fresh typed auth_revoked absence still exposes the approved owner action.
  const revokedAfterFailedAccountsRead = view([account('p1', quota({
    absence: {
      message: 'Quota temporarily unavailable',
      action_kind: 'sign_in_if_unverified', retry_at: '',
      observed_at: new Date().toISOString(),
    },
  }), {
    verification_state: 'passed', verification_source: 'vendor',
    verified_live: false,
    verification: { tone: 'muted', label: 'Verified live — last known' },
  })]);
  revokedAfterFailedAccountsRead.facets.accounts = 'failed';
  revokedAfterFailedAccountsRead.facet_note = 'accounts: failed';
  env = await boot(revokedAfterFailedAccountsRead);
  assert.match(allSpoken(env.root), /Sign-in required/);
  click(env, 'acct:claude:p1');
  assert.match(allSpoken(env.root), /Verified live — last known/);

  const degraded = view([account('p1', quota(), {
    detail: '/private/account/path raw vendor response',
  })]);
  degraded.ok = false;
  degraded.transport_error = '/private/transport/path refused vendor body';
  degraded.daemon = {
    state: 'unreachable', engine_version: '3.9.4',
    last_error: '/private/daemon/path raw provider response',
  };
  env = await boot(degraded);
  click(env, 'acct:claude:p1');
  click(env, 'about');
  assert.doesNotMatch(allSpoken(env.root), /private\/(account|transport|daemon)|vendor body|provider response/);

  // Automatic polling stays GET-only. Refresh is one POST, disabled while in
  // flight, and then one read of the whole projection: what is drawn is that
  // read — every part of the screen from one reading made after the
  // Refresh — never the POST's own quota merged into an older screen.
  const named = (used) => quota({ label: used + '% used', constraints: [Object.assign({}, staleConstraint, {
    used_pct: used, cooldown_until: '', scoped_models: [], label: 'Named',
  })] });
  const base = view([
    account('', quota({ label: '10% used', constraints: [Object.assign({}, staleConstraint, {
      used_pct: 10, cooldown_until: '', scoped_models: [], label: 'Native',
    })] })),
    account('p1', named(20)),
  ]);
  const after = JSON.parse(JSON.stringify(base));
  after.groups[0].accounts[1].quota = named(91);
  const post = { ok: true, quota_updates: [{ harness: 'claude', subject_id: 'p1', quota: named(55) }] };
  env = await boot((url, calls) => (calls.some((call) => call.method === 'POST') ? after : base), post);
  const PREFIX = process.env.WIDGET_ROUTE_PREFIX;
  assert.equal(env.calls.length, 1);
  assert.equal(env.calls[0].method, 'GET');
  assert.ok(env.calls[0].url.startsWith(PREFIX + 'quotas?'), env.calls[0].url);
  env.interval()();
  await settle();
  assert.equal(env.calls[1].method, 'GET');
  click(env, 'acct:claude:p1');
  assert.match(inspector(env).textContent, /20% used/);
  const refresh = byFocus(env.root, 'refresh');
  refresh.listeners.click[0]();
  refresh.listeners.click[0]();
  assert.equal(byFocus(env.root, 'refresh').disabled, true);
  await settle();
  const posts = env.calls.filter((call) => call.method === 'POST');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, PREFIX + 'refresh');
  const afterPost = env.calls.slice(env.calls.indexOf(posts[0]) + 1);
  assert.equal(afterPost.length, 1, afterPost.map((c) => c.url).join());
  assert.equal(afterPost[0].method, 'GET');
  assert.doesNotMatch(afterPost[0].url, /reuse=1/, 'the read after a Refresh is a new status read');
  assert.equal(byFocus(env.root, 'refresh').disabled, false);
  assert.match(inspector(env).textContent, /91% used/);
  assert.doesNotMatch(env.root.textContent, /55% used/, 'the POST answer is not merged in');
  assert.match(env.root.textContent, /named@example.com/);
  assert.match(env.root.textContent, /Verified live/);
  click(env, 'acct:claude:native');
  assert.match(inspector(env).textContent, /10% used/);
  assert.doesNotMatch(inspector(env).textContent, /91% used/);

  // Old hosts fail honestly and never fall back to a read of their own.
  env = await boot(view([account('p1', quota())]), {
    ok: false, compatibility_error: true,
    message: 'Live refresh requires a newer Ouroboros host',
  });
  byFocus(env.root, 'refresh').listeners.click[0]();
  await settle();
  assert.match(env.root.textContent, /Live refresh requires a newer Ouroboros host/);
  assert.deepEqual(env.calls.map((call) => call.method), ['GET', 'POST']);

  // 0.8.0: there is no settings panel. The skill's legacy display choices
  // (row detail, model filter, folding) travel with each reading for older
  // widgets; this one draws nothing from them and never saves any.
  const withPrefs = Object.assign(view([account('p1', quota()), account('p2', quota(), { enabled: false })]), {
    prefs: { density: 'compact', models: { claude: 'models' }, fold: { failed: false, disabled: false, signed_out: false } },
  });
  env = await boot(withPrefs);
  assert.equal(byFocus(env.root, 'settings'), undefined);
  const plain = await boot(view([account('p1', quota()), account('p2', quota(), { enabled: false })]));
  assert.equal(env.root.textContent, plain.root.textContent);
  click(env, 'acct:claude:p1');
  env.interval()();
  await settle();
  assert.ok(env.calls.every((call) => !/\/prefs$/.test(call.url)), 'no display choice is posted');

  // Every family is named with something in front of it — its own mark, or
  // the ring with its initial — and never somebody else's logo.
  const twoFamilies = view([account('p1', quota())]);
  twoFamilies.groups.push({
    harness_id: 'openrouter', family_label: 'OpenRouter', harness_status: 'ok',
    harness_enabled: true, provider_family: '', catalog_known: true,
    accounts: [], accounts_signed_in: 0, accounts_unavailable: false,
  });
  env = await boot(twoFamilies);
  assert.equal(classes(env.root, 'harness-initial').length, 1);
  assert.equal(classes(byFocus(env.root, 'harness:openrouter'), 'harness-initial').length, 1);
  click(env, 'harness:openrouter');
  assert.match(env.root.textContent, /No accounts in OpenRouter/);

  // 0.8.0: a family's button carries no colour for its worst account — one
  // account at its limit is not the family's state. A harness that is down or
  // switched off is, and says so. Each account's own state is its dot.
  const pipTone = (root) => {
    const pip = classes(byFocus(root, 'harness:claude'), 'seg-pip')[0];
    return pip ? String(pip.className).split(/\s+/).find((c) => ['ok', 'warn', 'bad', 'muted'].includes(c)) : null;
  };
  const accountDot = (root) => String(classes(byFocus(root, 'acct:claude:p1'), 'state-dot')[0].className);
  env = await boot(view([account('p1', quota())]));
  assert.equal(pipTone(env.root), null);
  assert.match(accountDot(env.root), /\bok\b/);
  env = await boot(view([account('p1', quota({
    label: '90% used',
    constraints: [{ id: 'weekly', label: 'Weekly', used_pct: 90, resets_at: '',
      cooldown_until: '', scoped_models: [], window_seconds: 604800 }],
  }))]));
  assert.equal(pipTone(env.root), null);
  assert.equal(stateOf(env, 'claude:p1'), 'nearly used');
  assert.match(accountDot(env.root), /\bwarn\b/);
  env = await boot(view([account('p1', quota({
    state: 'exhausted', label: 'Limit reached',
    constraints: [{ id: 'weekly', label: 'Weekly', used_pct: 100, resets_at: '',
      cooldown_until: '', scoped_models: [], window_seconds: 604800 }],
  }))]));
  assert.equal(pipTone(env.root), null, 'one spent account is not the family');
  assert.match(accountDot(env.root), /\bbad\b/);
  const down = view([account('p1', quota())]);
  down.groups[0].harness_status = 'unavailable';
  env = await boot(down);
  assert.equal(pipTone(env.root), 'warn');
  assert.match(byFocus(env.root, 'harness:claude').getAttribute('aria-label'), /harness unavailable/);
  ['ok', 'warn', 'bad', 'muted'].forEach((tone) => {
    assert.match(widgetSource, new RegExp(`\\.pip\\.${tone}[^{}]*\\{background:`), `.pip.${tone} has no colour`);
  });

  // The 30-second redraw keeps the selection and the keyboard where they were.
  env = await boot(view([account('p1', quota()), account('p2', quota())]));
  click(env, 'acct:claude:p2');
  byFocus(env.root, 'acct:claude:p2').focus();
  env.interval()();
  await settle();
  assert.equal(byFocus(env.root, 'acct:claude:p2').getAttribute('aria-pressed'), 'true');
  assert.ok(inspector(env));
  assert.equal(env.document.activeElement.getAttribute('data-focus'), 'acct:claude:p2');
  // An account that is gone takes its selection with it: nothing else is
  // selected in its place.
  const both = view([account('p1', quota()), account('p2', quota())]);
  const gone = view([account('p1', quota())]);
  env = await boot((url, calls) => (calls.length > 1 ? gone : both));
  click(env, 'acct:claude:p2');
  env.interval()();
  await settle();
  assert.equal(inspector(env), undefined);
  assert.equal(byFocus(env.root, 'acct:claude:p1').getAttribute('aria-pressed'), 'false');

  // A spent window's line says once when it comes back ("in 6d", never "in in 6d").
  const sixDays = new Date(Date.now() + 6 * 86400000).toISOString();
  env = await boot(view([
    account('p1', quota({
      state: 'exhausted', label: 'Limit reached', resets_at: sixDays,
      constraints: [{
        id: 'weekly', label: 'Weekly', used_pct: 100, resets_at: sixDays,
        cooldown_until: '', scoped_models: [], window_seconds: 604800,
      }],
    })),
  ]));
  click(env, 'acct:claude:p1');
  const backIn = classes(inspector(env), 'rel-time').map((node) => node.textContent);
  assert.ok(backIn.length >= 1 && backIn.every((t) => t === 'in 6d'), backIn.join('|'));
  assert.match(winLines(env)[0], /^weekWeekly|^week/);
  assert.match(winLines(env)[0], /spent.*in 6d$/);

  // Codex keeps two pools, and the engine lists their windows in an order
  // that changes from one reading to the next. The card keeps none of it:
  // windows are grouped by pool, the pool's chip in front of each line, and
  // the order is the same whichever order came in.
  const CODEX = {
    'codex-week': { id: 'cw', label: 'codex primary', used_pct: 100, resets_at: sixDays,
      cooldown_until: '', scoped_models: [], window_seconds: 604800 },
    'spark-5h': { id: 's5', label: 'GPT-5.3-Codex-Spark primary', used_pct: 0, resets_at: '',
      cooldown_until: '', scoped_models: [], window_seconds: 18000 },
    'spark-week': { id: 'sw', label: 'GPT-5.3-Codex-Spark secondary', used_pct: 0, resets_at: '',
      cooldown_until: '', scoped_models: [], window_seconds: 604800 },
  };
  const openedWith = async (order) => {
    const e = await boot(view([
      account('p1', quota({ state: 'exhausted', label: 'Limit reached', resets_at: sixDays,
        constraints: order.map((key) => CODEX[key]) })),
    ]));
    click(e, 'acct:claude:p1');
    return e;
  };
  const pools = (e) => classes(inspector(e), 'win-line').map((line) => {
    const chip = classes(line, 'acct-pool')[0];
    return classes(line, 'win-tag')[0].textContent + '|' + (chip ? chip.textContent : '');
  });
  env = await openedWith(['spark-week', 'codex-week', 'spark-5h']);
  assert.deepEqual(pools(env), ['week|codex', '5 hours|GPT-5.3-Codex-Spark', 'week|GPT-5.3-Codex-Spark']);
  assert.match(winLines(env)[0], /^weekcodex100% usedspent.*in 6d$/);
  assert.match(winLines(env)[1], /^5 hoursGPT-5\.3-Codex-Spark0% used/);
  const seenOnce = pools(env);
  env = await openedWith(['spark-5h', 'spark-week', 'codex-week']);
  assert.deepEqual(pools(env), seenOnce);

  // Two spellings of one pool name are two pools, side by side, in an order
  // of their own — never the order the engine happened to send them in.
  CODEX['codex-5h'] = { id: 'c5', label: 'codex secondary', used_pct: 0, resets_at: '',
    cooldown_until: '', scoped_models: [], window_seconds: 18000 };
  CODEX['Codex-5h'] = { id: 'C5', label: 'Codex primary', used_pct: 0, resets_at: '',
    cooldown_until: '', scoped_models: [], window_seconds: 18000 };
  CODEX['Codex-week'] = { id: 'Cw', label: 'Codex secondary', used_pct: 0, resets_at: '',
    cooldown_until: '', scoped_models: [], window_seconds: 604800 };
  const spelled = ['5 hours|Codex', 'week|Codex', '5 hours|codex', 'week|codex'];
  env = await openedWith(['codex-week', 'Codex-5h', 'codex-5h', 'Codex-week']);
  assert.deepEqual(pools(env), spelled);
  env = await openedWith(['Codex-week', 'codex-5h', 'Codex-5h', 'codex-week']);
  assert.deepEqual(pools(env), spelled);

  // Two windows of one length inside one pool take their role word as well,
  // and only then: the everyday line keeps "week" on its own.
  CODEX['codex-week-2'] = { id: 'cw2', label: 'codex secondary', used_pct: 0, resets_at: '',
    cooldown_until: '', scoped_models: [], window_seconds: 604800 };
  env = await openedWith(['codex-week-2', 'codex-week']);
  assert.deepEqual(pools(env), ['week primary|codex', 'week secondary|codex']);

  // Claude names no pool for its plain windows; those stand first, and the
  // window scoped to a model follows under that model's chip.
  env = await boot(view([account('p1', quota({ constraints: [
    { id: 'f', label: '7 day (Fable)', used_pct: 100, resets_at: sixDays, cooldown_until: '',
      scoped_models: ['fable', 'claude-fable-5'], window_seconds: 604800 },
    { id: 'w', label: '7 day', used_pct: 40, resets_at: '', cooldown_until: '', scoped_models: [], window_seconds: 604800 },
    { id: 'h', label: '5 hour', used_pct: 20, resets_at: '', cooldown_until: '', scoped_models: [], window_seconds: 18000 },
  ] }))]));
  click(env, 'acct:claude:p1');
  assert.deepEqual(pools(env), ['5 hours|', 'week|', 'week|Fable']);

  // Two words and two colours, each the same everywhere at once — the pool's
  // chip, the line's name, its words, the family mark. A model window cooling
  // until a date nobody can read is held: amber, "cooling down", its end
  // unreadable (not a reset). Red is the measured share at its limit only.
  env = await boot(view([account('p1', quota({ constraints: [
    { id: 'f', label: '7 day (Fable)', used_pct: 10, resets_at: '', cooldown_until: 'not-a-date',
      scoped_models: ['fable'], window_seconds: 604800 },
    { id: 'w', label: '7 day', used_pct: 40, resets_at: '', cooldown_until: '', scoped_models: [], window_seconds: 604800 },
  ] }))]));
  click(env, 'acct:claude:p1');
  const toneOf = (node) => String(node.className);
  const fableLine = classes(inspector(env), 'win-line').find((n) => /Fable/.test(n.textContent));
  assert.match(toneOf(classes(fableLine, 'acct-pool')[0]), /\bheld\b/);
  assert.doesNotMatch(toneOf(classes(fableLine, 'acct-pool')[0]), /\bexhausted\b/);
  assert.match(toneOf(classes(fableLine, 'win-tag')[0]), /\bwarn\b/);
  assert.match(fableLine.textContent, /cooling downend time unreadable/);
  // No cooldown list in this answer: the window's own cooldown says it.
  assert.equal(stateOf(env, 'claude:p1'), 'a model held');
  assert.equal(classes(env.root, 'exhausted').length, 0);
  assert.equal(classes(env.root, 'bad').filter((n) => /win-/.test(n.className)).length, 0);
  // The same window measured at its limit is spent: red, with its reset.
  env = await boot(view([account('p1', quota({ constraints: [
    { id: 'f', label: '7 day (Fable)', used_pct: 100, at_limit: true, resets_at: sixDays,
      cooldown_until: '', scoped_models: ['fable'], window_seconds: 604800 },
    { id: 'w', label: '7 day', used_pct: 40, resets_at: '', cooldown_until: '', scoped_models: [], window_seconds: 604800 },
  ] }))]));
  click(env, 'acct:claude:p1');
  const spentLine = classes(inspector(env), 'win-line').find((n) => /Fable/.test(n.textContent));
  assert.match(toneOf(classes(spentLine, 'acct-pool')[0]), /\bexhausted\b/);
  assert.match(toneOf(classes(spentLine, 'win-tag')[0]), /\bbad\b/);
  assert.match(spentLine.textContent, /spent.*in 6d$/);
  assert.equal(stateOf(env, 'claude:p1'), 'a model at its limit');
  assert.equal(classes(inspector(env), 'held').length, 0);

  // Accounts that cannot run anything stand at the bottom of the list under
  // their reasons, in the engine's order, never as alarms: a switched-off or
  // signed-out account is grey; a failed check stays red, as the fault it is.
  const mixedAccounts = () => [
    account('live1', quota(), { label: 'live-one' }),
    account('out1', quota(), { label: 'out-one', signed_in: false,
      verification: { tone: 'muted', label: 'Not verified' } }),
    // A failed check arrives from the engine as tone 'warn'.
    account('broken', quota(), { label: 'broken-one', verification_state: 'failed',
      verified_live: false, verification: { tone: 'warn', label: 'Verification failed' } }),
    account('off1', quota(), { label: 'off-one', enabled: false }),
    account('live2', quota(), { label: 'live-two' }),
  ];
  const namesOf = (root) => classes(root, 'acc-name-text').map((node) => node.textContent);
  const dotOf = (root, key) => String(classes(byFocus(root, 'acct:' + key), 'state-dot')[0].className);
  env = await boot(view(mixedAccounts()));
  assert.deepEqual(namesOf(env.root), ['live-one', 'live-two', 'out-one', 'broken-one', 'off-one']);
  assert.match(classes(env.root, 'acc-sec')[0].textContent, /^Not running/);
  assert.equal(stateOf(env, 'claude:out1'), 'not signed in');
  assert.equal(stateOf(env, 'claude:broken'), 'verification failed');
  assert.equal(stateOf(env, 'claude:off1'), 'switched off in Claudexor');
  assert.match(dotOf(env.root, 'claude:out1'), /\bmuted\b/);
  assert.match(dotOf(env.root, 'claude:off1'), /\bmuted\b/);
  assert.match(dotOf(env.root, 'claude:broken'), /\bbad\b/);
  assert.doesNotMatch(allSpoken(env.root), /need attention|needs attention/);
  // The family's account count is of the accounts switched on.
  assert.equal(classes(byFocus(env.root, 'harness:claude'), 'harness-count')[0].textContent, '4');
  // A not-running account can still be selected and read.
  click(env, 'acct:claude:off1');
  assert.match(inspector(env).textContent, /switched off in Claudexor/);

  // Two reasons at once is a shape the engine really produces: plugin.py
  // answers a failed check with signed_in false (TestVerificationView). The
  // account stands under its first reason, not signed in, and the failed
  // check is still said and still red in the list — before anyone selects it.
  // A switched-off account whose check failed is said the same way.
  env = await boot(view([
    account('live1', quota(), { label: 'live-one' }),
    account('both', quota(), { label: 'both-one', signed_in: false,
      verification_state: 'failed', verified_live: false,
      verification: { tone: 'warn', label: 'Verification failed' } }),
    account('offbad', quota(), { label: 'offbad-one', enabled: false,
      verification_state: 'failed', verified_live: false,
      verification: { tone: 'warn', label: 'Verification failed' } }),
  ]));
  assert.deepEqual(namesOf(env.root), ['live-one', 'both-one', 'offbad-one']);
  assert.match(classes(env.root, 'acc-sec')[0].textContent, /^Not running/);
  assert.equal(stateOf(env, 'claude:both'), 'not signed in · verification failed');
  assert.equal(stateOf(env, 'claude:offbad'), 'switched off in Claudexor · verification failed');
  assert.match(dotOf(env.root, 'claude:both'), /\bbad\b/);
  assert.match(dotOf(env.root, 'claude:offbad'), /\bbad\b/);
  assert.match(byFocus(env.root, 'acct:claude:both').getAttribute('aria-label'),
    /^both-one · not signed in · verification failed/);
  assert.equal(inspector(env), undefined, 'said before any selection');

  // The accounts facet did not answer: every state on screen is last known,
  // so no account is said not to run — the list keeps the engine's order.
  const unread = view(mixedAccounts());
  unread.facets = { catalog: 'ok', accounts: 'not_read', quota: 'ok' };
  unread.facet_note = 'accounts: not_read';
  env = await boot(unread);
  assert.equal(classes(env.root, 'acc-sec').length, 0);
  assert.deepEqual(namesOf(env.root), ['live-one', 'out-one', 'broken-one', 'off-one', 'live-two']);

  // The frame is disposable: one dispose hook that stops polling at once.
  env = await boot(view([account('p1', quota())]));
  assert.equal(env.disposeHooks.length, 1);
  assert.equal(env.disposeHooks[0](), undefined);
  assert.equal(env.intervalCleared(), true);
  env.interval()();
  await settle();
  assert.equal(env.calls.length, 1, 'a disposed frame reads nothing more');

  // Teardown owns the one poll timer and removes the document listeners.
  env = await boot(view([account('p1', quota())]));
  env.windowListeners.pagehide[0]();
  assert.equal(env.intervalCleared(), true);
  assert.equal((env.document.listeners.click || []).length, 0);
  assert.equal((env.document.listeners.keydown || []).length, 0);
  assert.equal((env.document.listeners.pointermove || []).length, 0);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def _widget_route_prefix(widget_path: Path) -> str:
    """The route prefix the widget actually asks for, read out of its own
    source. Asserting a literal here is what let a renamed copy of this skill
    drift away from its tests."""
    text = widget_path.read_text(encoding="utf-8")
    found = re.search(r"var ROUTE = '([^']*/)[a-z]+';", text)
    assert found, "widget.js must declare ROUTE as a single-quoted literal"
    return found.group(1)


def test_real_widget_in_process_matrix():
    candidates = [
        os.environ.get("OUROBOROSHUB_NODE", ""),
        str(Path.home() / ".claudexor" / "node" / "bin" / "node"),
        "/Applications/Claudexor.app/Contents/Resources/node",
        shutil.which("node") or "",
    ]
    node = next((Path(item) for item in candidates if item and Path(item).is_file()), None)
    assert node is not None, "a Node runtime is required for widget tests"
    widget_path = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", textwrap.dedent(NODE_WIDGET_MATRIX)],
        cwd=widget_path.parent,
        env={
            **dict(os.environ),
            "WIDGET_PATH": str(widget_path),
            # The prefix comes from the widget itself. Spelling it out a second
            # time here is how the pair drifted apart: a copy of this skill
            # under another name renamed its routes and left the assertions
            # asserting the old ones.
            "WIDGET_ROUTE_PREFIX": _widget_route_prefix(widget_path),
        },
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

class _Api:
    """The two calls this plugin makes on the host, and nothing else."""

    def __init__(self, state_dir, broken=False):
        self._state_dir = state_dir
        self._broken = broken
        self.logged = []

    def get_state_dir(self):
        if self._broken:
            raise RuntimeError("no state dir for this skill")
        return str(self._state_dir)

    def log(self, level, message):
        self.logged.append((level, message))


ALL_FOLDED = {reason: True for reason in FOLD_REASONS}


class TestPrefs:
    def test_clean_prefs_defaults_on_junk(self):
        for junk in (None, "", 0, [], "density", {"density": "huge"}):
            assert clean_prefs(junk) == DEFAULT_PREFS

    def test_clean_prefs_keeps_known_values(self):
        cleaned = clean_prefs({"density": "detailed", "models": {"claude": "models"}})
        assert cleaned == {
            "density": "detailed", "models": {"claude": "models"}, "fold": ALL_FOLDED,
        }

    def test_clean_prefs_drops_unknown_choice_but_keeps_the_rest(self):
        cleaned = clean_prefs({
            "density": "compact",
            "models": {"claude": "models", "codex": "everything", "": "all"},
        })
        assert cleaned == {
            "density": "compact", "models": {"claude": "models"}, "fold": ALL_FOLDED,
        }

    def test_clean_prefs_ignores_a_models_value_that_is_not_a_map(self):
        assert clean_prefs({"density": "compact", "models": ["claude"]}) == {
            "density": "compact", "models": {}, "fold": ALL_FOLDED,
        }

    def test_clean_prefs_caps_the_number_of_families(self):
        many = {"h%d" % i: "models" for i in range(MAX_MODEL_ENTRIES + 20)}
        cleaned = clean_prefs({"models": many})
        assert len(cleaned["models"]) <= MAX_MODEL_ENTRIES

    def test_write_then_read_round_trips(self, tmp_path):
        api = _Api(tmp_path)
        stored, error = write_prefs(api, {"density": "compact", "models": {"claude": "shared"}})
        assert error == ""
        assert stored == {
            "density": "compact", "models": {"claude": "shared"}, "fold": ALL_FOLDED,
        }
        assert read_prefs(api) == stored

    def test_read_returns_defaults_when_nothing_was_written(self, tmp_path):
        assert read_prefs(_Api(tmp_path)) == DEFAULT_PREFS

    def test_read_survives_a_corrupt_file(self, tmp_path):
        api = _Api(tmp_path)
        (tmp_path / "prefs.json").write_text("{not json", encoding="utf-8")
        assert read_prefs(api) == DEFAULT_PREFS

    def test_a_failed_write_leaves_the_last_choice_readable(self, tmp_path, monkeypatch):
        api = _Api(tmp_path)
        write_prefs(api, {"density": "detailed", "models": {"claude": "models"}})

        def refuse(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(plugin.os, "replace", refuse)
        stored, error = write_prefs(api, {"density": "compact"})
        assert error.startswith("OSError")
        # The file the widget reads is the one it read before, not a half of the
        # new one; and the temporary file does not stay behind.
        assert read_prefs(api)["density"] == "detailed"
        assert [item.name for item in tmp_path.iterdir()] == ["prefs.json"]

    def test_no_state_directory_is_reported_not_raised(self, tmp_path):
        api = _Api(tmp_path, broken=True)
        stored, error = write_prefs(api, {"density": "detailed"})
        assert stored == {"density": "detailed", "models": {}, "fold": ALL_FOLDED}
        assert error == "no state directory"
        assert read_prefs(api) == DEFAULT_PREFS

    def test_stored_file_holds_only_the_cleaned_shape(self, tmp_path):
        api = _Api(tmp_path)
        write_prefs(api, {"density": "detailed", "models": {"claude": "models"}, "token": "secret"})
        written = json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8"))
        assert written == {
            "density": "detailed", "models": {"claude": "models"}, "fold": ALL_FOLDED,
        }

    def test_fold_folds_every_reason_until_the_reader_says_otherwise(self):
        assert clean_prefs({"density": "normal"})["fold"] == ALL_FOLDED

    def test_fold_keeps_a_reason_switched_off(self):
        cleaned = clean_prefs({"fold": {"failed": False}})
        assert cleaned["fold"] == {"failed": False, "disabled": True, "signed_out": True}

    def test_fold_ignores_a_value_that_is_not_a_boolean(self):
        cleaned = clean_prefs({"fold": {"failed": "no", "disabled": 0, "signed_out": None}})
        assert cleaned["fold"] == ALL_FOLDED

    def test_fold_drops_a_reason_this_skill_does_not_know(self):
        cleaned = clean_prefs({"fold": {"exhausted": False, "disabled": False}})
        assert cleaned["fold"] == {"failed": True, "disabled": False, "signed_out": True}

    def test_fold_that_is_not_a_map_leaves_every_reason_folded(self):
        for junk in (["failed"], "failed", 0, None, True):
            assert clean_prefs({"fold": junk})["fold"] == ALL_FOLDED
