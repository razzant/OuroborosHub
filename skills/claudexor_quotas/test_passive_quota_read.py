"""Consumer contract for the dedicated quota view and query-ignoring cores.

All responses and accounts are synthetic. Registered handlers and the real
collector run against a stubbed HTTP boundary; no running host is contacted.
"""

import asyncio
import copy
import json
import threading

import pytest

import plugin
import quota_summary as qs
from test_reserve import NOW, _Host, constraint, payload, profile, snap


def envelope(*, accounts="ok", quota="ok", subjects=("fixture-a", "fixture-b")):
    """The supplied core contract: no catalog/daemon diagnostics on this view."""
    return {
        "view": "quota",
        "reads": {"catalog": "not_read", "accounts": accounts, "quota": quota},
        "profiles": ({"profiles": [profile("codex", sid) for sid in subjects],
                      "accountPools": []} if accounts == "ok" else {}),
        "unified_accounts": accounts == "ok",
        "quota": ([snap("codex", sid, [constraint("primary", .4, reset=86400)])
                   for sid in subjects] if quota == "ok" else []),
        "quota_absences": [],
        "timings_ms": {"discovery": 1.25, "accounts": 2, "quota": 3.75, "total": 7},
        "read_errors": {},
    }


def stub(monkeypatch, answers):
    calls = []
    queue = list(answers)

    def request(_port, path, method="GET", timeout_sec=plugin.STATUS_TIMEOUT_SEC):
        calls.append((method, path, timeout_sec))
        assert queue, "an unsolicited additional upstream request was sent"
        return queue.pop(0)

    monkeypatch.setattr(plugin, "_request_json", request)
    monkeypatch.setattr(plugin.time, "time", lambda: NOW)
    return calls


def route(host):
    return host.routes["quotas"]({"query_params": {"chart": "0"}})


@pytest.mark.parametrize(("marker", "mode"), [("quota", "quota"), (None, "legacy_status"),
                                              ("other", "legacy_status"), (True, "legacy_status")])
def test_one_get_and_an_exact_marker_are_required_to_claim_quota_view(monkeypatch, marker, mode):
    data = envelope()
    if marker is None:
        data.pop("view")
    else:
        data["view"] = marker
    original = copy.deepcopy(data)
    calls = stub(monkeypatch, [(data, "", 200)])
    result, error = plugin._fetch_status(8765)
    assert calls == [("GET", "/api/claudexor/status?view=quota", plugin.STATUS_TIMEOUT_SEC)]
    assert result == original and error == ""
    view = plugin.build_view(result, error, NOW)
    assert view["passive_read"]["mode"] == mode
    assert view["facets"]["catalog"] == "not_read"
    assert view["complete"] is (mode == "quota")
    assert view["facet_note"] == ("" if mode == "quota" else "catalog: not_read")
    assert view["daemon"]["state"] == ("" if mode == "quota" else "unknown")


def test_registered_route_and_tool_share_dedicated_envelope_without_catalog_calls(tmp_path, monkeypatch):
    data = envelope()
    calls = stub(monkeypatch, [(data, "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    view = route(host)
    answer = json.loads(host.tools["quota_summary"]["handler"](None, harness="codex"))
    assert calls == [("GET", "/api/claudexor/status?view=quota", plugin.STATUS_TIMEOUT_SEC)]
    assert view["complete"] is True and view["facet_note"] == ""
    assert view["facets"] == data["reads"] and answer["reads"] == data["reads"]
    assert view["passive_read"] == answer["passive_read"] == {
        "mode": "quota", "timings_ms": data["timings_ms"], "read_errors": {}}
    assert view["groups"][0]["catalog_known"] is False
    assert view["groups"][0]["harness_enabled"] is None
    assert view["groups"][0]["harness_status"] == ""
    assert view["reserve"]["summary"]["unified_accounts"] is True
    assert view["reserve"]["summary"]["roster"] == "current"
    assert answer["groups"][0]["remaining_windows"] == 1.2
    assert "fixture-a" not in json.dumps(answer)


def test_a_fresh_tool_worker_uses_the_same_get_with_its_own_timeout(tmp_path, monkeypatch):
    calls = stub(monkeypatch, [(envelope(), "", 200)])
    answer = plugin.tool_answer(_Host(tmp_path), plugin.LatestRead(), now=NOW)
    assert calls == [("GET", "/api/claudexor/status?view=quota", plugin.TOOL_STATUS_TIMEOUT_SEC)]
    assert answer["passive_read"]["mode"] == "quota"


def test_registered_collector_uses_the_same_get_and_its_result_is_reused(tmp_path, monkeypatch):
    calls = stub(monkeypatch, [(envelope(), "", 200)])
    persisted = threading.Event()
    real_persist = plugin.persist_sweep
    real_make = plugin.make_collector

    def persist(*args, **kwargs):
        result = real_persist(*args, **kwargs)
        persisted.set()
        return result

    def immediate(api, latest, stop):
        return real_make(api, latest, stop, interval_sec=60, first_delay_sec=0, clock=lambda: NOW)

    monkeypatch.setattr(plugin, "persist_sweep", persist)
    monkeypatch.setattr(plugin, "make_collector", immediate)
    host = _Host(tmp_path)
    plugin.register(host)

    async def run_once():
        task = asyncio.create_task(host.tasks[0][1]())
        try:
            async def wait_for_record():
                while not persisted.is_set():
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait_for_record(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run_once())
    answer = json.loads(host.tools["quota_summary"]["handler"](None, harness="codex"))
    assert calls == [("GET", "/api/claudexor/status?view=quota", plugin.STATUS_TIMEOUT_SEC)]
    assert answer["passive_read"]["mode"] == "quota"
    assert answer["history"]["state"] == "ok"


def test_query_ignoring_legacy_core_is_used_once_and_named_honestly(tmp_path, monkeypatch):
    legacy = payload([snap("codex", "fixture-a", [constraint("primary", .4, reset=86400)])],
                     [profile("codex", "fixture-a")], harnesses=("codex",))
    calls = stub(monkeypatch, [(legacy, "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    view = route(host)
    assert view["passive_read"]["mode"] == "legacy_status"
    assert view["complete"] is True and view["facets"]["catalog"] == "ok"
    assert view["reserve"]["summary"]["groups"][0]["measured"]["windows"] == .6
    assert calls == [("GET", "/api/claudexor/status?view=quota", plugin.STATUS_TIMEOUT_SEC)]


def test_a_previously_read_catalog_keeps_labels_but_no_current_harness_verdict(tmp_path, monkeypatch):
    legacy = payload([snap("codex", "fixture-a", [constraint("primary", .4, reset=86400)])],
                     [profile("codex", "fixture-a")], harnesses=("codex",))
    legacy["harnesses"][0].update(display_name="Fixture Codex", enabled=False, status="failed")
    calls = stub(monkeypatch, [(legacy, "", 200), (envelope(), "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    first = route(host)
    assert first["groups"][0]["harness_enabled"] is False
    view = route(host)
    family = view["groups"][0]
    assert len(calls) == 2 and view["complete"] is True
    assert view["facets"]["catalog"] == "not_read" and view["cached"]["catalog"] == qs.iso(NOW)
    assert family["family_label"] == "Fixture Codex"
    assert family["catalog_known"] is False
    assert family["harness_status"] == "" and family["harness_enabled"] is None


@pytest.mark.parametrize("read", ["failed", "not_read"])
def test_unread_empty_profiles_preserve_cached_roster_and_never_confirm_removal(tmp_path, monkeypatch, read):
    failure = envelope(accounts=read)
    failure["read_errors"] = {"accounts": {"code": "accounts_unavailable", "status_code": 503}}
    calls = stub(monkeypatch, [(envelope(), "", 200), (failure, "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    route(host)
    view = route(host)
    assert len(calls) == 2
    assert view["complete"] is False and view["facets"]["accounts"] == read
    assert view["facet_note"] == "accounts: " + read
    assert view["passive_read"]["read_errors"] == failure["read_errors"]
    assert [a["subject_id"] for a in view["groups"][0]["accounts"]] == ["fixture-a", "fixture-b"]
    summary = view["reserve"]["summary"]
    assert summary["roster"] == "cached" and summary["unified_accounts"] is True
    assert summary["groups"][0]["measured"]["accounts"] == 2
    assert all("account_state_unknown" in b["flags"] for b in summary["groups"][0]["bars"])


@pytest.mark.parametrize("read", ["failed", "not_read"])
def test_cold_unread_roster_is_unknown_not_authoritatively_empty(tmp_path, monkeypatch, read):
    calls = stub(monkeypatch, [(envelope(accounts=read), "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    view = route(host)
    assert len(calls) == 1 and not view["complete"]
    assert view["reserve"]["summary"]["roster"] == "unknown"
    assert all("account_state_unknown" in b["flags"]
               for g in view["reserve"]["summary"]["groups"] for b in g["bars"])


def test_successful_empty_roster_is_authoritative_even_with_empty_account_pools(tmp_path, monkeypatch):
    empty = envelope(subjects=())
    calls = stub(monkeypatch, [(envelope(), "", 200), (empty, "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    route(host)
    view = route(host)
    assert len(calls) == 2 and view["complete"] is True
    assert view["groups"] == [] and view["cached"] == {}
    assert view["reserve"]["summary"]["roster"] == "current"
    assert view["reserve"]["summary"]["unified_accounts"] is True


def test_failed_quota_keeps_numbers_dated_without_changing_source_freshness(tmp_path, monkeypatch):
    healthy = envelope()
    failure = envelope(quota="failed")
    failure["read_errors"] = {"quota": {"code": "quota_unavailable", "status_code": 503}}
    calls = stub(monkeypatch, [(healthy, "", 200), (failure, "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    route(host)
    view = route(host)
    assert len(calls) == 2 and not view["complete"]
    assert view["passive_read"]["mode"] == "quota"
    assert view["passive_read"]["read_errors"] == failure["read_errors"]
    assert view["facet_note"] == "quota: failed"
    summary = view["reserve"]["summary"]
    assert summary["groups"][0]["measured"]["accounts"] == 0
    assert {b["state"] for b in summary["groups"][0]["bars"]} == {"last_known"}
    assert {q["freshness"] for q in healthy["quota"]} == {"fresh"}


def test_failed_whole_request_keeps_roster_but_never_reuses_success_marker(tmp_path, monkeypatch):
    calls = stub(monkeypatch, [(envelope(), "", 200), (None, "timeout", -1), (None, "timeout", -1)])
    host = _Host(tmp_path)
    plugin.register(host)
    route(host)
    view = route(host)
    assert view["passive_read"] == {"mode": "unavailable", "timings_ms": {}, "read_errors": {}}
    assert not view["complete"] and not view["ok"]
    assert view["reserve"]["summary"]["roster"] == "cached"
    assert set(view["facets"].values()) == {"indeterminate"}
    answer = json.loads(host.tools["quota_summary"]["handler"](None))
    assert answer["passive_read"]["mode"] == "unavailable"
    assert "status_error" in answer and len(calls) == 3


def test_diagnostics_forward_only_safe_codes_statuses_and_bounded_numeric_timings():
    data = envelope()
    data["timings_ms"] = {"discovery": True, "accounts": float("nan"), "quota": -1,
                          "total": 7.5, "doctor": 9999}
    data["read_errors"] = {
        "accounts": {"code": "accounts_unavailable", "status_code": 503, "body": "private detail"},
        "quota": {"code": "quota_unavailable", "status_code": True, "path": "/private/detail"},
        "discovery": {"code": "raw path /private/detail"},
        "doctor": {"code": "private"},
    }
    info = plugin.passive_read_info(data)
    assert info == {"mode": "quota", "timings_ms": {"total": 7.5},
                    "read_errors": {"accounts": {"code": "accounts_unavailable", "status_code": 503},
                                    "quota": {"code": "quota_unavailable"}}}
    assert "private" not in json.dumps(info)
    data["timings_ms"] = {"discovery": float("inf"), "accounts": "2", "quota": 86400001}
    assert plugin.passive_read_info(data)["timings_ms"] == {}
    data["timings_ms"] = {"discovery": 10 ** 1000}
    assert plugin.passive_read_info(data)["timings_ms"] == {}


def test_reported_stale_quota_stays_stale_with_successful_passive_transport(tmp_path, monkeypatch):
    data = envelope()
    for row in data["quota"]:
        row["freshness"] = "stale"
    stub(monkeypatch, [(data, "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    view = route(host)
    assert view["complete"] is True
    assert view["reserve"]["summary"]["groups"][0]["measured"]["accounts"] == 0
    assert {b["state"] for b in view["reserve"]["summary"]["groups"][0]["bars"]} == {"last_known"}
