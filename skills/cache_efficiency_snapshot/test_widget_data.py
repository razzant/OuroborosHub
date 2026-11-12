"""Synthetic consumer data from the production aggregator (no provider/network)."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location("cache_widget_test_plugin", Path(__file__).with_name("plugin.py"))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def fixtures():
    records = []
    for i in range(160):
        stamp = NOW - timedelta(days=i, minutes=37)
        raw = {"type": "llm_usage", "ts": stamp.isoformat(), "task_id": "synthetic",
               "model": "example/model-one", "provider": "fixture", "prompt_tokens": 1000 + i,
               "cached_tokens": 600, "cache_write_tokens": 50, "ledger_attempt_ids": [str(i)]}
        records.append(plugin._normalize_usage_event(raw))
    for i, (total, cached, written) in enumerate(((2000, None, None), (None, None, None), (0, 0, 0), (100, 0, None),
                                                  (300, 270, 20), (90, None, 30))):
        records.append(plugin._normalize_usage_event({"ts": (NOW - timedelta(minutes=10+i*5)).isoformat(),
            "provider": "fixture", "model": "example/edge", "prompt_tokens": total,
            "cached_tokens": cached, "cache_write_tokens": written, "ledger_attempt_ids": ["edge-"+str(i)]}))
    # Exact source identity must not collapse identical short model labels.
    for i in range(28):
        records.append(plugin._normalize_usage_event({"ts": (NOW - timedelta(minutes=25)).isoformat(),
            "provider": "fixture", "model": f"source-{i}/same-label", "prompt_tokens": 1,
            "cached_tokens": 0, "ledger_attempt_ids": ["model-"+str(i)]}))
    boots = [plugin._boot_from_event({"ts": (NOW-timedelta(days=180)).isoformat(), "git_sha": "a"*40, "pid":1}),
             plugin._boot_from_event({"ts": (NOW-timedelta(minutes=12)).isoformat(), "git_sha":"b"*40, "pid":2})]
    result = {}
    for tf in ("1H", "6H", "24H", "7D", "MONTH", "ALL"):
        result[tf] = plugin.calculate_analytics(records, tf, now=NOW, boots=boots,
                            calendar_daily=tf in ("7D","MONTH","ALL"), zone="Europe/Moscow")
    start = plugin._custom_bound("2026-11-09", zone="Europe/Moscow")
    end = plugin._custom_bound("2026-11-09", end=True, zone="Europe/Moscow")
    result["CUSTOM"] = plugin.calculate_analytics(records, "CUSTOM", now=NOW, boots=boots,
                            start_override=start, end_override=end, calendar_daily=True, zone="Europe/Moscow")
    result["DETAIL"] = plugin.calculate_analytics(records, "CUSTOM", now=NOW, boots=boots,
                            start_override=start, end_override=end, hourly=True, zone="Europe/Moscow")
    late = plugin._normalize_usage_event({"ts": (start+timedelta(hours=10)).isoformat(),
        "provider":"fixture", "model":"example/late", "prompt_tokens":900,
        "cached_tokens":700, "ledger_attempt_ids":["late-arrival"]})
    for name, daily in (("CUSTOM_REFRESH",True),("DETAIL_REFRESH",False)):
        result[name] = plugin.calculate_analytics([*records,late], "CUSTOM", now=NOW, boots=boots,
            start_override=start, end_override=end, calendar_daily=daily, hourly=not daily, zone="Europe/Moscow")
    result["DAY_DETAILS"] = {b["ts"]:plugin.calculate_analytics(records,"CUSTOM",now=NOW,boots=boots,
        start_override=datetime.fromisoformat(b["ts"]),end_override=datetime.fromisoformat(b["end_ts"]),
        hourly=True,zone="Europe/Moscow") for b in result["MONTH"]["buckets"][-3:]}
    return result


if __name__ == "__main__":
    print(json.dumps(fixtures()))
