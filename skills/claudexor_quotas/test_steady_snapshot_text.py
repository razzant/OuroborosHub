"""The snapshot text changes when a printed fact changes, not when a poll does.

CREATING_SKILLS "Passive context snapshots": keep changing poll timestamps out of
`text`; the envelope dates the visible claim. Each rewrite is a new full note in
every eligible context, so poll jitter must leave the bytes alone.
"""

import quota_context as qc

STAMPS = ("2026-10-20T20:12:38.075Z", "2026-10-20T20:12:41.821Z")


def _answer(*, observed=STAMPS, state="current", remaining=14.39, pace=0.414, hours=156.64,
            reset="2026-10-20T22:00:00.371434Z", period_end="2026-11-01T00:00:00.251Z",
            offer_reset="2026-10-27T06:31:57.402Z"):
    row = {"family": "claude", "key": "claude|5h", "limit": "5 hour", "duration": "5 hours",
           "of_accounts": 15, "accounts_known_to_apply": 17, "remaining_windows": remaining,
           "average_remaining_pct": 95.9, "next_reset": {"at": reset, "accounts": 1}, "tightest_in_family": True,
           "recent_pace": {"windows_per_hour": pace, "accounts_known": 14, "of": 15, "state": "watched"}}
    spending = [{"state": state, "observed_at": stamp,
                 "rows": [{"label": "Monthly extra usage", "used": None, "limit": None, "unit": "minor",
                           "period": {"ends_at": period_end}}]}
                for stamp in observed]
    resets = [{"state": state, "observed_at": stamp,
               "rows": [{"label": "Reset", "available_count": 1, "usable_now": True, "resets_at": offer_reset}]}
              for stamp in observed]
    return {"groups": [row], "resources": {"claude": {"facets": {"spending": spending, "resets": resets}}},
            "joint": {"claude": {"available": True, "windows": ["5 hour", "7 day"], "scope": {},
                                 "unit": "5 hours window", "same_account_capacity": 3.6, "accounts_counted": 15,
                                 "accounts": 17, "conditional_account_hours": hours, "hours_known_accounts": 15}},
            "availability_scope": {}}


def test_poll_jitter_leaves_the_text_byte_identical():
    first = qc.snapshot_text(_answer())
    later = qc.snapshot_text(_answer(observed=("2026-10-20T20:17:39.104Z", "2026-10-20T20:17:42.917Z"),
                                     pace=0.365, hours=156.61, reset="2026-10-20T21:59:59.596646Z",
                                     period_end="2026-10-31T23:59:59.873Z", offer_reset="2026-10-27T06:31:56.998Z"))
    assert first == later
    assert "current (2 accounts)" in first, "equal current facts fold into one counted line"
    assert "20:12:38" not in first and "20:17:39" not in first, "no poll time of a current reading"
    assert "next reported reset 2026-10-20T22:00:00Z" in first
    assert "period/reset ends 2026-11-01T00:00:00Z" in first and "reset 2026-10-27T06:31:57Z" in first
    assert "measured pace 0.4 account-windows/h" in first and "≈156.6 account-hours" in first


def test_a_printed_fact_that_changes_still_rewrites_the_text():
    base = qc.snapshot_text(_answer())
    for changed in (_answer(remaining=14.09), _answer(reset="2026-10-21T03:00:00Z"),
                    _answer(hours=146.3), _answer(pace=1.3)):
        assert qc.snapshot_text(changed) != base


def test_last_known_resources_keep_their_own_dates():
    text = qc.snapshot_text(_answer(state="last_known"))
    assert f"last known observed {STAMPS[0]}" in text and f"last known observed {STAMPS[1]}" in text


def test_small_figures_stay_distinct_from_zero():
    assert qc._rate(0.01) == "<0.1" and qc._rate(0.0) == "0.0" and qc._rate(0.414) == "0.4"
    assert qc.rounded_hours(0) == "0.0" and qc.rounded_hours(0.003) == "<0.1"
    assert qc.rounded_hours(0.049) == "<0.1" and qc.rounded_hours(156.64) == "156.6"
    assert qc._second("2026-10-20T21:59:59.596646Z") == "2026-10-20T22:00:00Z"
    assert qc._second("not a time") == "not a time"
