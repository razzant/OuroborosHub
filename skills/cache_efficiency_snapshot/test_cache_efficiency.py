"""Cache measurements preserve unknown values and compare the same input totals."""
import copy
import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

PLUGIN_PATH = Path(__file__).parent / "plugin.py"
spec = importlib.util.spec_from_file_location("cache_efficiency_snapshot_plugin", PLUGIN_PATH)
assert spec is not None and spec.loader is not None
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

LedgerCache = plugin.LedgerCache
UsageEventsCache = plugin.UsageEventsCache
session_key = plugin._session_identity
_normalize_record = plugin._normalize_record
_normalize_usage_event = plugin._normalize_usage_event
calculate_analytics = plugin.calculate_analytics
register = plugin.register
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def row(**values: Any) -> Dict[str, Any]:
    result = {
        "state": "settled", "kind": "attempt", "provider": "openai",
        "model": "test-model", "ts": (NOW - timedelta(minutes=10)).isoformat(),
        "prompt_tokens": 100, "cached_tokens": 80, "cache_write_tokens": 0,
    }
    result.update(values)
    return result


def event(**values: Any) -> Dict[str, Any]:
    """One per-call ``llm_usage`` event as ``ouroboros/loop_llm_call.py`` emits it."""
    result = {
        "type": "llm_usage", "ts": (NOW - timedelta(minutes=10)).isoformat(),
        "task_id": "task-1", "model": "claudexor::codex=gpt-6-astra", "provider": "claudexor",
        "api_key_type": "claudexor", "source": "loop", "category": "task",
        "prompt_tokens": 100, "cached_tokens": 80, "cache_write_tokens": 0,
        "completion_tokens": 7, "llm_call_id": None, "ledger_attempt_ids": ["a1"],
    }
    result.update(values)
    return result


def measurement(total: Any, read: Any, write: Any = None) -> Dict[str, Any]:
    return {"total_tokens": total, "cache_read_tokens": read, "cache_write_tokens": write}


def normalized(*rows: Dict[str, Any]) -> list:
    result = [_normalize_record(item) for item in rows]
    assert all(item is not None for item in result)
    return result


def test_physical_attempt_uses_canonical_inclusive_counts_without_mutation() -> None:
    source = row(prompt_tokens=270, cached_tokens=130, cache_write_tokens=None)
    original = copy.deepcopy(source)
    result = _normalize_record(source)
    assert result["prompt_tokens"] == 270
    assert result["cached_tokens"] == 130
    assert result["cache_write_tokens"] is None
    assert result["uncached_tokens"] == 140
    assert result["eligible"] is True
    assert result["measured"] is True
    assert source == original


def test_explicit_measurement_wins_over_conflicting_legacy_counts() -> None:
    result = _normalize_record(row(input_token_usage=measurement(120, 30, 10)))
    assert result["prompt_tokens"] == 120
    assert result["cached_tokens"] == 30
    assert result["cache_write_tokens"] == 10
    assert result["uncached_tokens"] == 90


@pytest.mark.parametrize("value", [None, [], "unavailable", {}, measurement(None, None)])
def test_present_unknown_measurement_never_falls_back_to_legacy(value: Any) -> None:
    result = _normalize_record(row(input_token_usage=value))
    for category in ("prompt_tokens", "cached_tokens", "cache_write_tokens", "uncached_tokens"):
        assert result[category] is None
    assert result["eligible"] is False
    assert result["measured"] is False


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, "80"])
def test_invalid_category_stays_unknown_without_discarding_known_total(bad: Any) -> None:
    result = _normalize_record(row(input_token_usage=measurement(100, bad)))
    assert result["prompt_tokens"] == 100
    assert result["cached_tokens"] is None
    assert result["eligible"] is False
    assert result["uncached_tokens"] is None


def test_unknown_write_does_not_hide_valid_read_measurement() -> None:
    result = _normalize_record(row(input_token_usage=measurement(100, 80)))
    assert result["cache_write_tokens"] is None
    assert result["cached_tokens"] == 80
    assert result["eligible"] is True


@pytest.mark.parametrize("explicit", [False, True])
def test_read_exceeding_total_is_unknown_not_clipped_or_guessed(explicit: bool) -> None:
    source = row(prompt_tokens=20, cached_tokens=80, cache_write_tokens=10)
    if explicit:
        source["input_token_usage"] = measurement(20, 80, 10)
    result = _normalize_record(source)
    assert result["prompt_tokens"] == 20
    assert result["cached_tokens"] is None
    assert result["uncached_tokens"] is None
    assert result["eligible"] is False


@pytest.mark.parametrize("provider", ["claude", "cursor", "other"])
def test_legacy_non_codex_sessions_do_not_reconstruct_combined_cache(provider: str) -> None:
    result = _normalize_record(row(kind="subscription_session", provider=provider,
                                  prompt_tokens=20, cached_tokens=80, cache_write_tokens=10))
    for category in ("prompt_tokens", "cached_tokens", "cache_write_tokens"):
        assert result[category] is None
    assert result["eligible"] is False


def test_legacy_codex_session_preserves_proven_total_and_read_only() -> None:
    result = _normalize_record(row(kind="subscription_session", provider="codex",
                                  subscription_route="codex",
                                  prompt_tokens=50, cached_tokens=20, cache_write_tokens=None))
    assert result["prompt_tokens"] == 50
    assert result["cached_tokens"] == 20
    assert result["cache_write_tokens"] is None
    assert result["eligible"] is True


@pytest.mark.parametrize("route,expected", [("codex", True), ("cursor", False), ("claude", False),
                                            (None, False)])
def test_legacy_codex_session_requires_recorded_codex_route(route: Any, expected: bool) -> None:
    source = row(kind="subscription_session", provider="codex", subscription_route=route)
    if route is None:
        del source["subscription_route"]  # the writer always records it; absence proves nothing
    result = _normalize_record(source)
    assert result["eligible"] is expected
    assert result["prompt_tokens"] == (100 if expected else None)
    assert result["cached_tokens"] == (80 if expected else None)


def test_missing_kind_is_a_physical_attempt_like_the_core_ledger_reader() -> None:
    source = row()
    del source["kind"]
    result = _normalize_record(source)
    assert result["kind"] == "attempt"
    assert result["prompt_tokens"] == 100
    assert result["cached_tokens"] == 80
    summary = calculate_analytics([result], now=NOW)["summary"]
    assert summary["request_count"] == 1
    assert summary["other_count"] == 0


def test_nested_canonical_attempt_usage_and_missing_read() -> None:
    source = row()
    for key in ("prompt_tokens", "cached_tokens", "cache_write_tokens"):
        del source[key]
    source["usage"] = {"prompt_tokens": 100, "cached_tokens": 0}
    result = _normalize_record(source)
    assert result["prompt_tokens"] == 100
    assert result["cached_tokens"] == 0
    assert result["eligible"] is True
    del source["usage"]["cached_tokens"]
    result = _normalize_record(source)
    assert result["cached_tokens"] is None
    assert result["eligible"] is False


def test_mixed_coverage_uses_same_eligible_rows_for_numerator_and_denominator() -> None:
    records = normalized(
        row(model="physical", prompt_tokens=100, cached_tokens=80),
        row(kind="subscription_session", provider="claude", model="claude-session",
            input_token_usage=measurement(120, 30, 10)),
        row(kind="subscription_session", provider="codex", model="codex-session",
            subscription_route="codex", prompt_tokens=50, cached_tokens=20, cache_write_tokens=None),
        row(model="unknown-read", input_token_usage=measurement(90, None)),
        row(model="unknown-total", input_token_usage=measurement(None, 7)),
        row(model="zero", input_token_usage=measurement(0, 0)),
        row(kind="subscription_session", provider="claude", model="old-session"),
    )
    original = copy.deepcopy(records)
    response = calculate_analytics(records, timeframe="1H", now=NOW)
    summary = response["summary"]
    expected = {
        "prompt_tokens": 360, "cached_tokens": 137,
        "eligible_prompt_tokens": 270, "eligible_cached_tokens": 130,
        "total_records": 7, "request_count": 4, "session_count": 3, "other_count": 0,
        "measured_records": 4, "rate_records": 3, "unknown_total_records": 2,
        "unknown_read_records": 2, "zero_volume_records": 1, "unknown_prompt_tokens": 90,
    }
    for key, value in expected.items():
        assert summary[key] == value, key
    assert summary["cache_read_rate"] == pytest.approx(48.15)
    populated = [bucket for bucket in response["buckets"] if bucket["total_records"]]
    assert len(populated) == 1
    for key, value in {"prompt": 360, "cached": 130, "uncached": 140, "unknown": 90}.items():
        assert populated[0][key] == value, key
    assert populated[0]["rate"] == summary["cache_read_rate"]
    assert records == original


def test_zero_read_zero_volume_unknown_and_idle_are_different() -> None:
    cases = [
        (measurement(100, 0), 0.0, 1, 1, 0),
        (measurement(0, 0), None, 1, 0, 1),
        (measurement(100, None), None, 0, 0, 0),
        (measurement(None, None), None, 0, 0, 0),
    ]
    for values, rate, measured, eligible, zero in cases:
        response = calculate_analytics(normalized(row(input_token_usage=values)), now=NOW)
        summary = response["summary"]
        assert summary["cache_read_rate"] == rate
        assert summary["measured_records"] == measured
        assert summary["rate_records"] == eligible
        assert summary["zero_volume_records"] == zero
        assert all(bucket["rate"] is None for bucket in response["buckets"]
                   if bucket["total_records"] == 0)
    empty = calculate_analytics([], now=NOW)["summary"]
    assert empty["prompt_tokens"] is None
    assert empty["cached_tokens"] is None
    assert empty["cache_read_rate"] is None
    assert empty["total_records"] == 0


@pytest.mark.parametrize("timeframe,hours,count", [("1H", 1, 12), ("6H", 6, 12),
                                                 ("24H", 24, 24), ("7D", 168, 28)])
def test_timeframes_have_half_open_bounds_without_an_extra_endpoint(
    timeframe: str, hours: int, count: int,
) -> None:
    start = NOW - timedelta(hours=hours)
    step = timedelta(hours=hours / count)
    stamps = [start - timedelta(microseconds=1), start, start + step,
              NOW - timedelta(microseconds=1), NOW, NOW + timedelta(minutes=1)]
    response = calculate_analytics(normalized(*(row(ts=stamp.isoformat()) for stamp in stamps)),
                                   timeframe=timeframe.lower(), now=NOW)
    assert response["timeframe"] == timeframe
    assert response["summary"]["total_records"] == 3
    buckets = response["buckets"]
    assert len(buckets) == count
    assert datetime.fromisoformat(buckets[0]["ts"]) == start
    assert datetime.fromisoformat(buckets[-1]["ts"]) == NOW - step
    assert [buckets[index]["total_records"] for index in (0, 1, count - 1)] == [1, 1, 1]
    assert sum(bucket["total_records"] for bucket in buckets) == 3


def test_all_time_has_regular_buckets_and_retains_gaps_between_observations() -> None:
    stamps = [NOW - timedelta(hours=20), NOW - timedelta(hours=1), NOW]
    response = calculate_analytics(normalized(*(row(ts=stamp.isoformat()) for stamp in stamps)),
                                   timeframe="ALL", now=NOW)
    assert response["summary"]["total_records"] == 2
    buckets = response["buckets"]
    assert len(buckets) > 2
    dates = [datetime.fromisoformat(bucket["ts"]) for bucket in buckets]
    intervals = {after - before for before, after in zip(dates, dates[1:])}
    assert len(intervals) == 1
    assert next(iter(intervals)) > timedelta(0)
    assert all(stamp < NOW for stamp in dates)
    assert any(bucket["total_records"] == 0 and bucket["rate"] is None for bucket in buckets)
    assert sum(bucket["total_records"] for bucket in buckets) == 2
    assert all(bucket["label"].startswith("2026-") for bucket in buckets)
    day = calculate_analytics(normalized(row()), timeframe="24H", now=NOW)["buckets"]
    assert all(not bucket["label"].startswith("2026") for bucket in day)


def test_all_time_labels_stay_distinct_across_a_year_boundary() -> None:
    stamps = [NOW - timedelta(days=400), NOW - timedelta(days=35)]
    response = calculate_analytics(normalized(*(row(ts=stamp.isoformat()) for stamp in stamps)),
                                   timeframe="ALL", now=NOW)
    labels = [bucket["label"] for bucket in response["buckets"]]
    assert len(labels) == len(set(labels))


def test_model_groups_use_exact_provider_model_and_kind_not_display_names() -> None:
    records = normalized(
        row(provider="openai", model="same-model", prompt_tokens=100, cached_tokens=90),
        row(provider="codex", model="same-model", prompt_tokens=100, cached_tokens=10),
        row(provider="codex", model="same-model", kind="subscription_session",
            input_token_usage=measurement(200, 100)),
        row(provider="openai", model="different-model", prompt_tokens=100, cached_tokens=0),
    )
    for record in records:
        record["display_name"] = "Identical label"
    response = calculate_analytics(records, now=NOW)
    groups = {(item["provider"], item["model"], item["kind"]): item for item in response["models"]}
    assert len(groups) == 4
    assert groups[("openai", "same-model", "attempt")]["rate"] == 90.0
    assert groups[("codex", "same-model", "attempt")]["rate"] == 10.0
    session = groups[("codex", "same-model", "subscription_session")]
    assert session["rate"] == 50.0
    assert session["session_count"] == 1
    assert session["request_count"] == 0


def test_analytics_has_no_invented_money_or_ambiguous_call_counts() -> None:
    response = calculate_analytics(normalized(row()), now=NOW)
    serialized = json.dumps(response, allow_nan=False)
    for obsolete in ("savings_usd", "gross_cost_usd", "net_cost_usd", "pricing_estimated",
                     "total_calls", "call_hit_rate"):
        assert obsolete not in serialized
    assert response["status"] == "ok"
    assert calculate_analytics([], meta={"read_error": "file_not_found"}, now=NOW)["status"] == "degraded"


def write_rows(path: Path, *rows: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")


def append_rows(path: Path, *rows: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write("".join(json.dumps(item) + "\n" for item in rows))


# ---------------------------------------------------------------------------
# Requests: the per-call usage events
# ---------------------------------------------------------------------------

def test_usage_event_keeps_inclusive_counts_and_unknowns() -> None:
    measured = _normalize_usage_event(event(prompt_tokens=337290, cached_tokens=228352))
    assert (measured["prompt_tokens"], measured["cached_tokens"], measured["uncached_tokens"]) == (337290, 228352, 108938)
    assert measured["kind"] == "attempt" and measured["measured"] and measured["eligible"]
    unknown_read = _normalize_usage_event(event(cached_tokens=None))
    assert unknown_read["prompt_tokens"] == 100 and unknown_read["cached_tokens"] is None
    assert unknown_read["measured"] is False and unknown_read["uncached_tokens"] is None
    too_many = _normalize_usage_event(event(prompt_tokens=10, cached_tokens=11))
    assert too_many["cached_tokens"] is None and too_many["measured"] is False
    assert _normalize_usage_event(event(provider="", api_key_type="openai"))["provider"] == "openai"
    assert _normalize_usage_event(event(ts=None)) is None


def test_usage_events_are_read_from_live_log_and_skip_other_rows(tmp_path: Path) -> None:
    live = tmp_path / "logs" / "events.jsonl"
    write_rows(live, {"type": "llm_round", "ts": NOW.isoformat(), "prompt_tokens": 5},
               event(model="first"), event(model="second", prompt_tokens=0, cached_tokens=0),
               {"type": "task_checkpoint", "ts": NOW.isoformat(), "note": "llm_usage mentioned in text"})
    cache = UsageEventsCache()
    records, meta = cache.update(live, tmp_path / "archive", now=NOW)
    assert [item["model"] for item in records] == ["first", "second"]
    assert meta["exists"] is True and meta["read_error"] is None
    assert meta["raw_stats"]["usage_events"] == 2
    assert meta["raw_stats"]["non_usage_lines"] == 2
    assert meta["raw_stats"]["archives_read"] == 0
    summary = calculate_analytics(records, timeframe="1H", now=NOW)["summary"]
    assert summary["request_count"] == 2 and summary["session_count"] == 0
    assert summary["zero_volume_records"] == 1 and summary["cache_read_rate"] == 80.0
    repeated, same = cache.update(live, tmp_path / "archive", now=NOW)
    assert repeated == records and same["raw_stats"] == meta["raw_stats"]
    cache.clear()
    assert cache.records == [] and cache.seen == set()


def test_usage_events_torn_line_waits_and_duplicates_count_once(tmp_path: Path) -> None:
    live = tmp_path / "logs" / "events.jsonl"
    write_rows(live, event(model="first", ledger_attempt_ids=["a1"]))
    second = json.dumps(event(model="second", ledger_attempt_ids=["a2"])).encode()
    with live.open("ab") as stream:
        stream.write(second[: len(second) // 2])
    cache = UsageEventsCache()
    records, meta = cache.update(live, tmp_path / "archive", now=NOW)
    assert [item["model"] for item in records] == ["first"]
    with live.open("ab") as stream:
        stream.write(second[len(second) // 2:] + b"\n{\"type\": \"llm_usage\", broken\nnot-json\n")
    records, meta = cache.update(live, tmp_path / "archive", now=NOW)
    assert [item["model"] for item in records] == ["first", "second"]
    assert meta["raw_stats"]["malformed_lines"] == 1  # a broken usage line
    assert meta["raw_stats"]["non_usage_lines"] == 1  # a line that is not a usage event at all
    append_rows(live, event(model="second", ledger_attempt_ids=["a2"]))  # a byte-identical replay
    records, meta = cache.update(live, tmp_path / "archive", now=NOW)
    assert [item["model"] for item in records] == ["first", "second"]
    assert meta["raw_stats"]["duplicate_events_skipped"] == 1


def test_rotation_moves_the_live_tail_into_an_archive_without_gaps_or_doubles(tmp_path: Path) -> None:
    live = tmp_path / "logs" / "events.jsonl"
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    write_rows(live, event(model="one", ledger_attempt_ids=["a1"]))
    cache = UsageEventsCache()
    assert [r["model"] for r in cache.update(live, archive_dir, now=NOW)[0]] == ["one"]
    # The core appends more, then rotates: rename live -> archive, start a new live file.
    append_rows(live, event(model="two", ledger_attempt_ids=["a2"]))
    live.rename(archive_dir / "events_20260911T115900.jsonl")
    write_rows(live, event(model="three", ledger_attempt_ids=["a3"]))
    records, meta = cache.update(live, archive_dir, now=NOW)
    assert [r["model"] for r in records] == ["one", "two", "three"]
    assert meta["raw_stats"]["archives_read"] == 1
    assert meta["raw_stats"]["duplicate_events_skipped"] == 1  # "one" met again inside the archive
    # Truncation of the live file restarts its offset and keeps what was read.
    live.write_text("", encoding="utf-8")
    append_rows(live, event(model="four", ledger_attempt_ids=["a4"]))
    records, meta = cache.update(live, archive_dir, now=NOW)
    assert [r["model"] for r in records] == ["one", "two", "three", "four"]
    assert meta["raw_stats"]["archives_read"] == 1  # an unchanged archive is not re-read


def test_archives_outside_the_window_are_ignored_and_old_records_pruned(tmp_path: Path) -> None:
    live = tmp_path / "logs" / "events.jsonl"
    archive_dir = tmp_path / "archive"
    write_rows(live, event(model="live"))
    old = archive_dir / "events_20260801T000000.jsonl"
    write_rows(old, event(model="ancient", ts=(NOW - timedelta(days=40)).isoformat()))
    stale = (NOW - timedelta(days=40)).timestamp()
    os.utime(old, (stale, stale))
    recent = archive_dir / "events_20260911T100000.jsonl"
    write_rows(recent, event(model="recent", ts=(NOW - timedelta(days=2)).isoformat()),
               event(model="expired", ts=(NOW - timedelta(days=9)).isoformat()))
    cache = UsageEventsCache()
    records, meta = cache.update(live, archive_dir, now=NOW)
    assert sorted(r["model"] for r in records) == ["live", "recent"]
    assert meta["raw_stats"]["archives_read"] == 1
    assert meta["archive_window_days"] == plugin.USAGE_ARCHIVE_WINDOW_DAYS
    assert calculate_analytics(records, timeframe="7D", now=NOW)["summary"]["request_count"] == 2


def test_missing_live_log_is_degraded_but_archives_still_count(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    write_rows(archive_dir / "events_20260911T100000.jsonl", event(model="archived"))
    records, meta = UsageEventsCache().update(tmp_path / "logs" / "events.jsonl", archive_dir, now=NOW)
    assert [r["model"] for r in records] == ["archived"]
    assert meta["exists"] is False and meta["read_error"] == "file_not_found"


# ---------------------------------------------------------------------------
# Sessions and code versions: also from the event log
# ---------------------------------------------------------------------------

def session_event(route: str = "codex", run_id: str = "run-1", **values: Any) -> Dict[str, Any]:
    """A reviewer session's ``llm_usage`` row (``review_session_usage.session_usage_facts``)."""
    result = event(source="review_substrate:plan_review", category="review", model="gpt-6-astra",
                   prompt_tokens=2043012, cached_tokens=1846016, cache_write_tokens=None,
                   claudexor={"run_id": run_id, "requested_route": route, "delegated_route": route,
                              "observed_attempt": {"harness_id": route, "model": "gpt-6-astra"}})
    result.update(values)
    return result


def ledger_event(run_id: str = "run-1", route: str = "claude", **values: Any) -> Dict[str, Any]:
    result = {"type": "delegate_run_ledger_recorded", "ts": (NOW - timedelta(minutes=9)).isoformat(),
              "run_id": run_id, "task_id": "t", "route": route}
    result.update(values)
    return result


def boot(sha: str, minutes_ago: float, pid: int = 1) -> Dict[str, Any]:
    return {"type": "worker_boot", "ts": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "pid": pid, "git_branch": "ouroboros", "git_sha": sha}


def test_a_session_total_is_a_session_never_a_request() -> None:
    codex = _normalize_usage_event(session_event())
    assert codex["kind"] == "subscription_session" and codex["identity"] == session_key("run-1")
    assert (codex["prompt_tokens"], codex["cached_tokens"], codex["measured"]) == (2043012, 1846016, True)
    # Claude's native input excludes cache reads and the event names no cache writes:
    # its counters cannot form an inclusive total, so the session stays unknown.
    claude = _normalize_usage_event(session_event(route="claude", prompt_tokens=4, cached_tokens=248987))
    assert claude["kind"] == "subscription_session" and claude["provider"] == "claude"
    assert (claude["prompt_tokens"], claude["cached_tokens"], claude["measured"]) == (None, None, False)
    # A normalized split, when the core sends one, is authoritative on every route.
    split = _normalize_usage_event(session_event(route="claude", prompt_tokens=4, cached_tokens=248987,
                                                 input_token_usage=measurement(248991, 245981, 3006)))
    assert (split["prompt_tokens"], split["cached_tokens"], split["measured"]) == (248991, 245981, True)
    # A per-call event of a native review episode has no run receipt, and a Claudexor
    # model call's receipt names an operation, not a run: both are requests.
    assert _normalize_usage_event(event(source="review_substrate:plan_review"))["kind"] == "attempt"
    loop_call = _normalize_usage_event(event(claudexor={"operation_id": "op-1", "model_role": "main"}))
    assert loop_call["kind"] == "attempt" and loop_call["prompt_tokens"] == 100
    summary = calculate_analytics([codex, claude, _normalize_usage_event(event())], timeframe="1H", now=NOW)["summary"]
    assert (summary["request_count"], summary["session_count"]) == (1, 2)
    assert summary["eligible_prompt_tokens"] == 2043012 + 100


@pytest.mark.parametrize("usage_first", [True, False])
def test_a_usage_event_supplies_the_tokens_of_a_recorded_session(tmp_path: Path, usage_first: bool) -> None:
    live = tmp_path / "logs" / "events.jsonl"
    rows = [session_event(run_id="run-1"), ledger_event(run_id="run-1", route="codex")]
    write_rows(live, *(rows if usage_first else rows[::-1]), ledger_event(run_id="run-2"))
    records, meta = UsageEventsCache().update(live, tmp_path / "archive", now=NOW)
    sessions = {r["identity"]: r for r in records}
    first, second = session_key("run-1"), session_key("run-2")
    assert set(sessions) == {first, second}
    assert sessions[first]["prompt_tokens"] == 2043012 and sessions[first]["token_facts"]
    assert sessions[second]["model"] == plugin.NO_USAGE_EVENT_MODEL
    assert sessions[second]["measured"] is False and not sessions[second]["token_facts"]
    analytics = calculate_analytics(records, timeframe="1H", now=NOW)
    assert analytics["summary"]["session_count"] == 2 and analytics["summary"]["request_count"] == 0
    assert analytics["quality"]["sessions_without_usage_event"] == 1
    assert meta["raw_stats"]["session_usage_events"] == 1
    assert meta["raw_stats"]["session_ledger_events"] == (1 if usage_first else 2)
    assert meta["raw_stats"]["duplicate_events_skipped"] == (1 if usage_first else 0)


# A run can emit several ``llm_usage`` session events: an early failed one without the
# normalized split, a later recovered one with it. Evidence ranks per run:
# normalized ``input_token_usage`` > legacy counters / tokenless usage event > ledger event.
# Equal rank keeps the first event; a lower rank never replaces or fills a higher one.

RUN = "run-recovered"


def _legacy_unknown(**values: Any) -> Dict[str, Any]:
    """A failed review's session event: Claude route, legacy counters, no normalized split."""
    return session_event(route="claude", run_id=RUN, prompt_tokens=4, cached_tokens=248987,
                         cache_write_tokens=None, **values)


def _normalized(total: Any = 20488, read: Any = 12738, write: Any = 7746, **values: Any) -> Dict[str, Any]:
    return session_event(route="claude", run_id=RUN, prompt_tokens=4, cached_tokens=248987,
                         input_token_usage=measurement(total, read, write), **values)


def _read_session(tmp_path: Path, *rows: Dict[str, Any]):
    live = tmp_path / "logs" / "events.jsonl"
    write_rows(live, *rows)
    records, meta = UsageEventsCache().update(live, tmp_path / "archive", now=NOW)
    sessions = [r for r in records if r["kind"] == "subscription_session"]
    return records, sessions, meta


def test_later_normalized_split_replaces_an_earlier_legacy_event_of_the_same_run(tmp_path: Path) -> None:
    records, sessions, meta = _read_session(
        tmp_path, _legacy_unknown(ts=(NOW - timedelta(minutes=9)).isoformat()),
        _normalized(ts=(NOW - timedelta(minutes=5)).isoformat()))
    assert len(records) == 1 and len(sessions) == 1  # one run, one session, never summed
    session = sessions[0]
    assert session["identity"] == session_key(RUN) and session["token_facts"] is True
    assert (session["prompt_tokens"], session["cached_tokens"], session["cache_write_tokens"]) == (20488, 12738, 7746)
    assert session["measured"] is True and session["uncached_tokens"] == 20488 - 12738
    assert meta["raw_stats"]["duplicate_events_skipped"] == 0
    analytics = calculate_analytics(records, timeframe="1H", now=NOW)
    summary = analytics["summary"]
    assert (summary["session_count"], summary["request_count"]) == (1, 0)
    assert (summary["prompt_tokens"], summary["cached_tokens"]) == (20488, 12738)
    assert (summary["eligible_prompt_tokens"], summary["eligible_cached_tokens"]) == (20488, 12738)
    assert summary["cache_read_rate"] == round(12738 / 20488 * 100, 2)
    assert (summary["unknown_total_records"], summary["unknown_read_records"]) == (0, 0)
    assert analytics["quality"]["sessions_without_usage_event"] == 0


def test_later_legacy_event_never_replaces_or_downgrades_a_normalized_split(tmp_path: Path) -> None:
    for legacy in (_legacy_unknown(ts=(NOW - timedelta(minutes=5)).isoformat()),
                   # a legacy Codex counter pair is measured, still not authoritative over the split
                   session_event(route="codex", run_id=RUN, ts=(NOW - timedelta(minutes=5)).isoformat())):
        records, sessions, meta = _read_session(tmp_path, _normalized(ts=(NOW - timedelta(minutes=9)).isoformat()), legacy)
        assert len(sessions) == 1
        assert (sessions[0]["prompt_tokens"], sessions[0]["cached_tokens"], sessions[0]["cache_write_tokens"]) == (20488, 12738, 7746)
        assert meta["raw_stats"]["duplicate_events_skipped"] == 1
        assert calculate_analytics(records, timeframe="1H", now=NOW)["summary"]["session_count"] == 1


def test_authoritative_nulls_of_a_normalized_split_are_not_filled_from_legacy_counters(tmp_path: Path) -> None:
    unknown_write = _normalized(20488, 12738, None)
    codex_legacy = session_event(route="codex", run_id=RUN, prompt_tokens=999, cached_tokens=111, cache_write_tokens=5)
    for rows in ((unknown_write, codex_legacy), (codex_legacy, unknown_write)):
        records, sessions, meta = _read_session(tmp_path, *rows)
        assert len(sessions) == 1
        # Either order: the normalized split wins, its null write stays null, nothing is borrowed.
        assert (sessions[0]["prompt_tokens"], sessions[0]["cached_tokens"], sessions[0]["cache_write_tokens"]) == (20488, 12738, None)
    all_null = _normalized(None, None, None)
    records, sessions, _ = _read_session(tmp_path, all_null, codex_legacy)
    assert (sessions[0]["prompt_tokens"], sessions[0]["cached_tokens"], sessions[0]["measured"]) == (None, None, False)
    assert sessions[0]["token_facts"] is True  # unknown tokens, but the run does have a usage event
    assert calculate_analytics(records, timeframe="1H", now=NOW)["quality"]["sessions_without_usage_event"] == 0


@pytest.mark.parametrize("malformed", [None, [], "unavailable"])
def test_a_malformed_split_is_not_normalized_evidence(tmp_path: Path, malformed: Any) -> None:
    # Present but not an object: stays an authoritative-unknown legacy-rank event (first wins).
    records, sessions, _ = _read_session(
        tmp_path, _legacy_unknown(), _normalized(), session_event(route="claude", run_id=RUN, input_token_usage=malformed))
    assert len(sessions) == 1 and sessions[0]["prompt_tokens"] == 20488
    records, sessions, _ = _read_session(
        tmp_path, session_event(route="claude", run_id=RUN, input_token_usage=malformed), _normalized())
    assert len(sessions) == 1 and sessions[0]["prompt_tokens"] == 20488  # a later valid split still supersedes


def test_equal_rank_events_of_one_run_keep_the_first_as_before(tmp_path: Path) -> None:
    # Two legacy events: first wins. Two normalized events: first wins (a materially different
    # later split is reported in the skill notes, never merged or summed).
    records, sessions, meta = _read_session(
        tmp_path, session_event(route="codex", run_id=RUN, prompt_tokens=100, cached_tokens=40),
        session_event(route="codex", run_id=RUN, prompt_tokens=900, cached_tokens=800))
    assert [s["prompt_tokens"] for s in sessions] == [100] and meta["raw_stats"]["duplicate_events_skipped"] == 1
    records, sessions, meta = _read_session(tmp_path, _normalized(100, 40, 1), _normalized(900, 800, 2),
                                            _normalized(None, None, None))
    assert [(s["prompt_tokens"], s["cached_tokens"]) for s in sessions] == [(100, 40)]
    assert meta["raw_stats"]["duplicate_events_skipped"] == 2


def test_evidence_order_survives_ledger_events_archives_and_incremental_reads(tmp_path: Path) -> None:
    live, archive_dir = tmp_path / "logs" / "events.jsonl", tmp_path / "archive"
    write_rows(archive_dir / "events_20260911T100000.jsonl", ledger_event(run_id=RUN, route="claude"),
               _legacy_unknown(ts=(NOW - timedelta(minutes=9)).isoformat()))
    cache = UsageEventsCache()
    first, _ = cache.update(live, archive_dir, now=NOW)
    assert [(r["prompt_tokens"], r["token_facts"]) for r in first] == [(None, True)]
    write_rows(live, _normalized(ts=(NOW - timedelta(minutes=5)).isoformat()), ledger_event(run_id=RUN, route="claude"))
    records, meta = cache.update(live, archive_dir, now=NOW)
    assert len(records) == 1 and records[0]["prompt_tokens"] == 20488 and records[0]["cache_write_tokens"] == 7746
    append_rows(live, _legacy_unknown(), _normalized(1, 0, 0, ts=(NOW - timedelta(minutes=4)).isoformat()))
    records, meta = cache.update(live, archive_dir, now=NOW)  # later, lower or equal rank: nothing changes
    assert len(records) == 1 and records[0]["prompt_tokens"] == 20488
    assert calculate_analytics(records, timeframe="1H", now=NOW)["summary"]["session_count"] == 1


def test_route_reports_the_recovered_session_once_with_normalized_tokens(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    early, late = ((now - timedelta(minutes=m)).isoformat() for m in (9, 5))
    write_rows(tmp_path / "logs" / "events.jsonl", _legacy_unknown(ts=early), ledger_event(ts=early, run_id=RUN, route="claude"),
               _normalized(ts=late), event(ts=late, prompt_tokens=100, cached_tokens=90))
    api = MockAPI(tmp_path)
    register(api)
    try:
        response = api.routes["data"]({"query_params": {"timeframe": "1H"}})
        summary = response["summary"]
        assert (summary["request_count"], summary["session_count"]) == (1, 1)
        assert (summary["eligible_prompt_tokens"], summary["eligible_cached_tokens"]) == (20488 + 100, 12738 + 90)
        assert response["quality"]["sessions_without_usage_event"] == 0
        sessions = [m for m in response["models"] if m["kind"] == "subscription_session"]
        assert [(m["prompt_tokens"], m["cached_tokens"], m["measured_records"]) for m in sessions] == [(20488, 12738, 1)]
    finally:
        api.unload_fn()


def test_code_marks_are_first_boots_on_a_new_commit(tmp_path: Path) -> None:
    live = tmp_path / "logs" / "events.jsonl"
    write_rows(live, boot("a" * 40, 200), boot("a" * 40, 50, pid=2), boot("b" * 40, 45),
               boot("b" * 40, 44, pid=3), boot("c" * 40, 10), boot("", 5))
    cache = UsageEventsCache()
    records, meta = cache.update(live, tmp_path / "archive", now=NOW)
    assert records == [] and meta["raw_stats"]["worker_boots"] == 5
    marks = calculate_analytics(records, timeframe="1H", now=NOW, boots=cache.boot_snapshot())["markers"]
    # The earliest boot has no predecessor; restarts on the same commit mark nothing.
    assert [(m["code"], m["position"]) for m in marks] == [("bbbbbbbbb", 3.0), ("ccccccccc", 10.0)]
    assert all(m["branch"] == "ouroboros" and m["ts"].endswith("+00:00") for m in marks)
    day = calculate_analytics(records, timeframe="24H", now=NOW, boots=cache.boot_snapshot())
    assert [m["code"] for m in day["markers"]] == ["bbbbbbbbb", "ccccccccc"]
    assert all(0 <= m["position"] < len(day["buckets"]) for m in day["markers"])
    assert calculate_analytics(records, timeframe="1H", now=NOW)["markers"] == []


def test_a_mark_needs_only_a_different_predecessor_even_outside_the_window(monkeypatch) -> None:
    def boots(*pairs):
        return [plugin._boot_from_event(boot(sha, minutes)) for sha, minutes in pairs]

    hour = calculate_analytics([], timeframe="1H", now=NOW, boots=boots(("a" * 40, 200), ("b" * 40, 45)))
    assert [m["code"] for m in hour["markers"]] == ["bbbbbbbbb"] and hour["quality"]["markers_omitted"] == 0
    monkeypatch.setattr(plugin, "MAX_MARKERS_RETURNED", 2)
    many = calculate_analytics([], timeframe="1H", now=NOW,
                               boots=boots(*((sha * 40, 50 - 10 * i) for i, sha in enumerate("abcde"))))
    assert [m["code"] for m in many["markers"]] == ["ddddddddd", "eeeeeeeee"]
    assert many["quality"]["markers_omitted"] == 2


def test_sessions_and_boots_leave_with_the_window(tmp_path: Path) -> None:
    live = tmp_path / "logs" / "events.jsonl"
    old = (NOW - timedelta(days=9)).isoformat()
    write_rows(live, session_event(ts=old, run_id="old"), ledger_event(ts=old, run_id="older"),
               {**boot("a" * 40, 0), "ts": old}, session_event(run_id="new"), boot("b" * 40, 5))
    cache = UsageEventsCache()
    records, meta = cache.update(live, tmp_path / "archive", now=NOW)
    assert [r["identity"] for r in records] == [session_key("new")]
    assert [b["sha"][:1] for b in cache.boot_snapshot()] == ["b"]
    assert meta["raw_stats"]["worker_boots"] == 2


# ---------------------------------------------------------------------------
# The JSONL ledger: live before the usage store (7.6.0 and older), retired after
# ---------------------------------------------------------------------------

def test_ledger_keeps_sessions_and_skips_requests_baseline_and_unsettled(tmp_path: Path) -> None:
    path = tmp_path / "usage_attempts.jsonl"
    header = {"kind": "usage_baseline", "attempt_id": "baseline-1", "state": "settled", "seq": 1,
              "ts": (NOW - timedelta(minutes=5)).isoformat(), "compaction_epoch": 1,
              "folded_attempt_count": 3, "group_count": 1, "retained_row_count": 3}
    write_rows(path, header, row(kind="usage_baseline_group", state="settled", folded_attempt_count=3),
               row(), row(kind="subscription_session", provider="claude", input_token_usage=measurement(0, 0)),
               row(kind="subscription_session", provider="claude", input_token_usage=measurement(100, 90)),
               row(state="dispatched"))
    cache = LedgerCache()
    records, meta = cache.update(path)
    assert len(records) == 2
    assert {item["kind"] for item in records} == {"subscription_session"}
    assert meta["raw_stats"]["non_settled_lines"] == 1
    assert meta["raw_stats"]["compacted_records_skipped"] == 2
    assert meta["raw_stats"]["request_rows_skipped"] == 1  # requests come from usage events
    assert meta["raw_stats"]["valid_records"] == 2
    assert meta["raw_stats"]["total_lines_read"] == 6
    summary = calculate_analytics(records, timeframe="1H", now=NOW)["summary"]
    assert summary["request_count"] == 0 and summary["session_count"] == 2
    repeated, same_meta = cache.update(path)
    assert repeated == records and same_meta["raw_stats"] == meta["raw_stats"]
    cache.clear()
    assert cache.records == []


def test_ledger_rotation_and_truncation_replace_cached_records(tmp_path: Path) -> None:
    path = tmp_path / "usage_attempts.jsonl"
    session = dict(kind="subscription_session", provider="claude", input_token_usage=measurement(10, 5))
    write_rows(path, row(model="first", **session), row(model="second", **session))
    cache = LedgerCache()
    assert len(cache.update(path)[0]) == 2
    replacement = tmp_path / "new.jsonl"
    write_rows(replacement, row(model="replacement", **session))
    replacement.replace(path)
    records, meta = cache.update(path)
    assert [item["model"] for item in records] == ["replacement"]
    assert meta["raw_stats"]["total_lines_read"] == 1
    write_rows(path, row(model="x", **session))
    records, meta = cache.update(path)
    assert [item["model"] for item in records] == ["x"]


def test_a_live_ledger_supplies_the_tokens_of_its_sessions_and_a_retired_one_is_skipped(tmp_path: Path) -> None:
    sha = lambda run: hashlib.sha256(run.encode()).hexdigest()  # noqa: E731 -- the ledger's own key, not the plugin's
    horizon = NOW - timedelta(days=plugin.USAGE_ARCHIVE_WINDOW_DAYS)
    tokenless = [plugin._session_from_ledger_event(ledger_event(run_id=run)) for run in ("run-a", "run-b")]
    measured = _normalize_usage_event(session_event(run_id="run-c"))
    rows = normalized(*(row(kind="subscription_session", provider="claude", session_id_sha256=sha(run),
                            input_token_usage=measurement(100, 90)) for run in ("run-a", "run-c")))
    merged = plugin._merge_ledger_sessions([*tokenless, measured], rows, horizon)
    by_key = {r["identity"]: r for r in merged}
    assert set(by_key) == {session_key("run-a"), session_key("run-b"), session_key("run-c")}
    assert by_key[session_key("run-a")]["prompt_tokens"] == 100  # the ledger row replaced the tokenless event
    assert by_key[session_key("run-b")]["token_facts"] is False
    assert by_key[session_key("run-c")]["prompt_tokens"] == 2043012  # a measured event keeps its tokens
    ledger = tmp_path / "usage_attempts.jsonl"
    write_rows(ledger, row())
    assert plugin._ledger_is_live(ledger, horizon=datetime.now(timezone.utc) - timedelta(days=7))
    stale = (datetime.now(timezone.utc) - timedelta(days=12)).timestamp()
    os.utime(ledger, (stale, stale))
    assert not plugin._ledger_is_live(ledger, horizon=datetime.now(timezone.utc) - timedelta(days=7))
    assert not plugin._ledger_is_live(tmp_path / "missing.jsonl", horizon=horizon)


class MockAPI:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self.routes: Dict[str, Any] = {}
        self.ui_tabs: Dict[str, Any] = {}

    def get_runtime_info(self) -> Dict[str, Any]:
        return {"data_dir": str(self._data_dir)}

    def register_route(self, route_name: str, handler: Any, methods: Any = None) -> None:
        self.routes[route_name] = handler

    def register_ui_tab(self, tab_id: str, title: str, icon: str = "extension", render: Any = None) -> None:
        self.ui_tabs[tab_id] = {"title": title, "icon": icon, "render": render}

    def on_unload(self, fn: Any) -> None:
        self.unload_fn = fn


def test_plugin_route_reads_requests_sessions_and_marks_from_events_and_unload_clears(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    stamp = (now - timedelta(minutes=10)).isoformat()
    live = tmp_path / "logs" / "events.jsonl"
    write_rows(live, event(ts=stamp, prompt_tokens=100, cached_tokens=90, ledger_attempt_ids=["a1"]),
               session_event(ts=stamp, prompt_tokens=100, cached_tokens=90),
               ledger_event(ts=stamp, run_id="run-2"),
               {**boot("a" * 40, 0), "ts": (now - timedelta(minutes=40)).isoformat()},
               {**boot("b" * 40, 0), "ts": (now - timedelta(minutes=20)).isoformat()})
    write_rows(tmp_path / "archive" / "events_20260911T100000.jsonl",
               event(ts=stamp, model="archived", prompt_tokens=100, cached_tokens=90, ledger_attempt_ids=["a2"]))
    # A JSONL ledger the usage store retired (unchanged for longer than the window) is not read.
    retired = tmp_path / "state" / "usage_attempts.jsonl"
    write_rows(retired, row(ts=stamp, kind="subscription_session", provider="claude", input_token_usage=measurement(100, 90)))
    old = (now - timedelta(days=12)).timestamp()
    os.utime(retired, (old, old))
    api = MockAPI(tmp_path)
    register(api)
    assert "data" in api.routes
    assert api.ui_tabs["cache_efficiency"]["render"]["kind"] == "module"
    assert api.ui_tabs["cache_efficiency"]["render"]["appearance"] == "host"

    class MockRequest:
        query_params = {"timeframe": "1H"}

    try:
        response = api.routes["data"](MockRequest())
        assert response["status"] == "ok"
        assert response["summary"]["cache_read_rate"] == 90.0
        assert response["summary"]["request_count"] == 2
        assert response["summary"]["session_count"] == 2
        assert len(response["buckets"]) == 12
        assert [m["code"] for m in response["markers"]] == ["bbbbbbbbb"]
        quality = response["quality"]
        assert quality["events_path"] == str(live) and quality["ledger_live"] is False
        assert quality["raw_stats"]["archives_read"] == 1
        assert quality["sessions_without_usage_event"] == 1
        assert "event log" in quality["coverage"]
        assert api.routes["data"]({"query_params": {"timeframe": "6H"}})["timeframe"] == "6H"
    finally:
        api.unload_fn()
    assert plugin._EVENTS.records == [] and plugin._EVENTS.sessions == {} and plugin._EVENTS.boots == []
    assert plugin._CACHE.records == []


def test_a_live_ledger_route_counts_each_session_once_with_its_tokens(tmp_path: Path) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    write_rows(tmp_path / "logs" / "events.jsonl", event(ts=stamp), ledger_event(ts=stamp, run_id="run-a"))
    write_rows(tmp_path / "state" / "usage_attempts.jsonl",
               row(ts=stamp, kind="subscription_session", provider="claude", input_token_usage=measurement(100, 90),
                   session_id_sha256=hashlib.sha256(b"run-a").hexdigest()),
               # older history of a live ledger stays outside the window, ALL included
               row(ts=(datetime.now(timezone.utc) - timedelta(days=12)).isoformat(), kind="subscription_session",
                   provider="claude", input_token_usage=measurement(100, 90), session_id_sha256="0" * 64))
    api = MockAPI(tmp_path)
    register(api)
    try:
        response = api.routes["data"]({"query_params": {"timeframe": "1H"}})
        assert response["quality"]["ledger_live"] is True
        assert (response["summary"]["request_count"], response["summary"]["session_count"]) == (1, 1)
        assert response["quality"]["sessions_without_usage_event"] == 0
        assert response["summary"]["eligible_prompt_tokens"] == 200
        assert "live JSONL ledger" in response["quality"]["coverage"]
        everything = api.routes["data"]({"query_params": {"timeframe": "ALL"}})["summary"]
        assert (everything["request_count"], everything["session_count"]) == (1, 1)
    finally:
        api.unload_fn()
