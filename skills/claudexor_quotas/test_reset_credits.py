"""Manual reset counters are reported facts, separate from quota/refill math."""

import copy

import pytest

import plugin
import quota_summary as qs


NOW = qs.parse_instant("2026-10-09T08:00:00Z")
OBSERVED = "2026-10-09T07:59:00Z"


def snapshot(label="2 reset credits", *, subject="fixture-a", source="fixture-source",
             observed=OBSERVED, freshness="fresh", constraint_id="reset_credits", **extra):
    return {"subject": {"harness": "codex", "subject_id": subject}, "source": source,
            "observed_at": observed, "freshness": freshness,
            "constraints": [{"id": constraint_id, "label": label, **extra}]}


def payload(rows, subjects=("fixture-a",)):
    return {"reads": {"catalog": "ok", "accounts": "ok", "quota": "ok"},
            "unified_accounts": True,
            "harnesses": [{"id": "codex", "display_name": "Codex"}],
            "profiles": {"profiles": [
                {"profile": {"profile_id": sid, "harness_id": "codex", "enabled": True},
                 "status": {"verification": "passed"}} for sid in subjects]},
            "quota": rows}


def account(rows, subject="fixture-a", quota_read="ok"):
    return plugin.quota_for(rows, "codex", subject, quota_read, now=NOW)["reset_credits"]


@pytest.mark.parametrize(("label", "count"), [
    ("0 reset credits", 0), ("1 reset credit", 1), ("2 reset credits remaining", 2),
    ("Reset credits: 3", 3), ("Manual reset credits: 4 available", 4),
    ("Reset credits remaining: 5", 5), ("Reset credits (6 left)", 6),
    ("  7 RESET CREDITS  ", 7),
])
def test_explicit_single_count_label(label, count):
    assert qs.reset_credit_count(label) == count
    view = account([snapshot(label)])
    assert (view["state"], view["count"], view["count_origin"]) == ("current", count, "label")
    assert view["label"] == label.strip()


@pytest.mark.parametrize("label", [
    "", None, 2, "Reset credits", "unknown reset credits", "Reset credits: $2",
    "-1 reset credits", "1.5 reset credits", "2/3 reset credits", "1,000 reset credits",
    "2 reset credits until 2026-10-10", "2 reset credits or 3", "5-hour credits: 2",
    "Reset credits: 9007199254740992", "2 reset credits" + " " * 512,
])
def test_unreadable_is_never_zero_or_a_guessed_number(label):
    assert qs.reset_credit_count(label) is None
    view = account([snapshot(label)])
    assert (view["state"], view["count"], view["reason"]) == ("unreadable", None, "count_unreadable")
    assert view["reports"][0]["count"] is None


def test_exact_constraint_id_with_only_the_harness_namespace_stripped():
    assert account([snapshot(constraint_id="codex:reset_credits")])["count"] == 2
    for cid in ("other:reset_credits", "credits", "manual_resets", ""):
        view = account([snapshot(constraint_id=cid)])
        assert (view["state"], view["count"], view["reason"]) == ("unknown", None, "not_reported")


def test_zero_missing_and_facet_failure_are_three_different_answers():
    zero = account([snapshot("0 reset credits")])
    missing = account([])
    failed = account([snapshot()], quota_read="failed")
    assert (zero["state"], zero["count"]) == ("current", 0)
    assert (missing["state"], missing["count"], missing["reason"]) == ("unknown", None, "not_reported")
    assert (failed["state"], failed["count"], failed["reason"]) == ("unknown", None, "quota_failed")
    assert failed["reports"] == []


def test_stale_zero_stays_a_dated_counter_without_timed_refill_semantics():
    view = account([snapshot("0 reset credits", freshness="stale",
                             resets_at="2026-10-09T07:00:00Z", window_seconds=18000)])
    assert (view["state"], view["count"], view["age_seconds"]) == ("last_known", 0, 60)
    assert view["observed_at"] == OBSERVED
    assert view["source"] == "fixture-source"
    assert view["reports"][0]["freshness"] == "stale"
    assert "resets_at" not in view and "window_seconds" not in view


@pytest.mark.parametrize(("observed", "reason"), [
    (None, "no_observation_time"), ("broken", "no_observation_time"),
    ("2026-10-09T09:00:00Z", "observed_in_future"),
])
def test_unplaced_count_is_not_current_or_dated_last_known(observed, reason):
    view = account([snapshot(observed=observed)])
    assert (view["state"], view["count"], view["reason"]) == ("unknown", None, reason)
    assert view["reports"][0]["count"] == 2


def test_duplicate_sources_count_once_and_keep_provenance():
    rows = [snapshot(), snapshot(source="fixture-second")]
    view = account(rows)
    assert (view["state"], view["count"]) == ("current", 2)
    assert {r["source"] for r in view["reports"]} == {"fixture-source", "fixture-second"}
    family = plugin.build_view(payload(rows), "", NOW)["groups"][0]["reset_credits"]
    assert family["count"] == 2 and family["current_accounts"] == 1


@pytest.mark.parametrize("freshness", ["fresh", "stale"])
def test_same_moment_conflict_is_not_arbitrarily_resolved(freshness):
    rows = [snapshot("2 reset credits", freshness=freshness),
            snapshot("3 reset credits", freshness=freshness, source="fixture-second")]
    view = account(rows)
    assert (view["state"], view["count"], view["reason"]) == ("conflict", None, "sources_disagree")
    family = plugin.build_view(payload(rows), "", NOW)["groups"][0]["reset_credits"]
    assert family["conflict_accounts"] == 1 and family["count"] is None


def test_newest_report_replaces_an_older_count_but_unreadable_does_not_revive_it():
    rows = [snapshot("3 reset credits", observed="2026-10-09T07:00:00Z"),
            snapshot("1 reset credit", source="fixture-second")]
    assert account(rows)["count"] == 1
    rows[1]["constraints"][0]["label"] = "Reset credits unavailable"
    view = account(rows)
    assert (view["state"], view["count"]) == ("unreadable", None)
    assert view["reports"][0]["count"] == 3


def test_fractional_observation_times_do_not_create_a_false_source_conflict():
    rows = [snapshot("3 reset credits", observed="2026-10-09T07:59:00.100Z"),
            snapshot("1 reset credit", source="fixture-second", observed="2026-10-09T07:59:01.200Z")]
    view = account(rows)
    assert (view["state"], view["count"]) == ("current", 1)
    assert view["observed_at"] == "2026-10-09T07:59:01.200000Z"


def test_source_freshness_is_honoured_without_a_skill_owned_expiry():
    rows = [snapshot("1 reset credit", observed="2026-10-08T07:00:00Z"),
            snapshot("3 reset credits", freshness="stale")]
    view = account(rows)
    assert (view["state"], view["count"]) == ("current", 1)
    assert view["age_seconds"] == 90000


def test_family_coverage_and_dated_totals_do_not_absorb_unknowns():
    rows = [snapshot("0 reset credits"),
            snapshot("2 reset credits", subject="fixture-b"),
            snapshot("4 reset credits", subject="fixture-c", freshness="stale"),
            snapshot("Reset credits unknown", subject="fixture-d")]
    data = payload(rows, ("fixture-a", "fixture-b", "fixture-c", "fixture-d", "fixture-e"))
    family = plugin.build_view(data, "", NOW)["groups"][0]
    credits = family["reset_credits"]
    assert credits == {
        "accounts": 5, "count": 2, "current_accounts": 2,
        "last_known_count": 4, "last_known_accounts": 1,
        "unknown_accounts": 1, "unreadable_accounts": 1, "conflict_accounts": 0,
        "oldest_observed_at": OBSERVED, "newest_observed_at": OBSERVED,
        "last_known_oldest_observed_at": OBSERVED, "last_known_newest_observed_at": OBSERVED,
    }
    assert [a["quota"]["reset_credits"]["state"] for a in family["accounts"]] == [
        "current", "current", "last_known", "unreadable", "unknown"]


def test_failed_facet_keeps_only_dated_credit_counts_from_the_existing_cache():
    latest = plugin.LatestRead()
    original = payload([snapshot("0 reset credits")])
    latest.put(original, "", NOW)
    effective, cached = latest.effective({"reads": {"quota": "failed"}})
    view = plugin.build_view(effective, "", NOW + 120, reads={"quota": "failed"}, cached=cached)
    family = view["groups"][0]
    counter = family["accounts"][0]["quota"]["reset_credits"]
    assert (counter["state"], counter["count"], counter["age_seconds"]) == ("last_known", 0, 180)
    assert family["reset_credits"]["count"] is None
    assert family["reset_credits"]["last_known_count"] == 0
    assert original["quota"][0]["freshness"] == "fresh"


def test_exact_attribution_ignores_removed_accounts_and_inherits_default_alias_once():
    data = payload([snapshot(subject=None), snapshot("5 reset credits", subject="removed-fixture")],
                   ("codex-default",))
    family = plugin.build_view(data, "", NOW)["groups"][0]
    assert family["reset_credits"]["count"] == 2
    assert family["reset_credits"]["current_accounts"] == 1
    data["quota"].append(snapshot("1 reset credit", subject="codex-default"))
    assert plugin.build_view(data, "", NOW)["groups"][0]["reset_credits"]["count"] == 1


def test_credits_never_enter_quota_math_even_with_window_shaped_fields():
    rows = [snapshot("3 reset credits", used_ratio=1.0, window_seconds=18000,
                     resets_at="2026-10-09T20:00:00Z")]
    data = payload(rows)
    before = copy.deepcopy(data)
    summary = qs.build_summary(data, None, NOW)
    assert summary["groups"] == []
    assert qs.recordable(qs.normalize(data, NOW), NOW) == []
    quota = plugin.quota_for(rows, "codex", "fixture-a", "ok", now=NOW)
    assert quota["state"] == "no_data" and quota["resets_at"] == ""
    assert quota["constraints"][0]["used_pct"] is None
    assert quota["reset_credits"]["count"] == 3
    assert data == before


def test_family_all_zero_differs_from_all_missing():
    zero = plugin.build_view(payload([snapshot("0 reset credits")]), "", NOW)["groups"][0]
    missing = plugin.build_view(payload([]), "", NOW)["groups"][0]
    assert zero["reset_credits"]["count"] == 0
    assert missing["reset_credits"]["count"] is None
