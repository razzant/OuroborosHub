"""Exact reset instants and bounded display provenance, synthetic only."""
import plugin
import quota_history
import quota_summary as qs
from test_coherent_history import NOW, data, detail_at, record, result


def test_fractional_passed_reset_changes_provenance_without_refill(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    reading = data(-1200, {"atlas": .8})
    reset = NOW - 600.25
    reading["quota"][0]["constraints"][0]["resets_at"] = qs.iso_exact(reset)
    plugin.persist_sweep(store, reading, "", NOW - 1200)
    reading["quota"][0]["freshness"] = "stale"
    history = result(store, reading)["chart"]["history"]
    assert detail_at(history, -600.26)["reset_passed"] == 0
    after = detail_at(history, -600.25)
    assert after["at"] == reset and after["reset_passed"] == 1
    assert {v for _, v in history["line"] if v is not None} == {.2}


def test_clipped_history_keeps_line_and_provenance_aligned(tmp_path, monkeypatch):
    store = quota_history.HistoryStore(tmp_path)
    for offset in range(-2400, 0, 120):
        values = {"atlas": (offset + 2400) / 4800}
        if offset >= -600:
            values["birch"] = .3
        record(store, offset, values)
    monkeypatch.setattr(qs, "MAX_PAST_VERTICES", 5)
    history = result(store, data(0, {"atlas": .6, "birch": .3}))["chart"]["history"]
    assert len(history["line"]) == len(history["details"]) == 5
    assert history["clipped_before"] == history["line"][0][0]
    for point, detail in zip(history["line"], history["details"]):
        assert point == [detail["at"], detail["value"]]
        assert detail["accounts"] == 2
        if detail["carried"]:
            assert detail["oldest_observed_at"] is not None and detail["sources"] == ["app"]
