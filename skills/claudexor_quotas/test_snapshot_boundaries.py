"""Resource expiry and hostile-length passive facts through the tool and snapshot.

Synthetic fixtures only. Values stay exact in the tool; the bounded snapshot
quotes controls or explicitly omits a value, never inventing an amount or ID.
"""
import copy
import json

import pytest

import plugin
import quota_context as qc
import quota_resources as qr
import quota_summary as qs
from test_reserve import NOW, payload, profile
from test_scope_resources_context import _Api, facet, host_reads


def resources(offers=None):
    data = payload([], [profile("cursor", "a")], harnesses=("cursor",))
    data["availability_scope"] = {"model": None, "requestKind": "text", "basis": "quota_block"}
    data["resources"] = [{"target": {"harness": "cursor", "profile_id": "a"},
                          "balances": facet([]), "spending": facet([]),
                          "resets": facet(offers or []), "diagnostics": facet([])}]
    return data


def offer(grants):
    return {"id": "reset", "label": "Manual resets", "kind": "granted_reset", "available_count": 2,
            "usable_now": True, "grants": grants}


def answer(data, now=NOW):
    return plugin.answer_of(None, data, None, data, now, now)


def publish(data, path, now=NOW):
    latest = plugin.LatestRead()
    latest.put(data, "", now)
    assert plugin.publish_snapshot(_Api(path), None, data, "", now, latest) == "written"
    result = json.loads((path / qc.SNAPSHOT_FILE).read_text())
    assert host_reads(path / qc.SNAPSHOT_FILE)[0] == "fresh"
    assert len(result["text"]) <= qc.MAX_CHARS
    assert (path / qc.SNAPSHOT_FILE).stat().st_size <= qc.MAX_FILE_BYTES
    return result


@pytest.mark.parametrize("grant", [{"available_count": 2}, {"available_count": 0, "usable_now": True},
                                   {"available_count": 2, "usable_now": False}])
def test_positive_or_usable_grants_expire_facts_and_published_validity(tmp_path, grant):
    data = resources([offer([dict(grant, expires_at=qs.iso(NOW + 60)),
                             {"available_count": 1, "expires_at": qs.iso(NOW + 120)}])])
    current = answer(data)
    assert qs.parse_instant(qc.earliest_reset(current)) == NOW + 60
    assert qs.parse_instant(publish(data, tmp_path)["valid_until"]) == NOW + 60
    assert qr.family_resets(data, "cursor", now=NOW + 59)["available"] == 2
    for at in (NOW + 60, NOW + 61):
        own = qr.account_resources(data, "cursor", "a", now=at)["resets"]
        assert own["state"] == "last_known" and own["rows"][0]["available_count"] == 2
        assert own["observed_at"] == data["resources"][0]["resets"]["observed_at"]
        assert qr.family_resets(data, "cursor", now=at)["available"] == 0
        out = answer(data, at)
        assert qc.earliest_reset(out) is None
        assert "last known observed" in qc.snapshot_text(out)
    later = publish(data, tmp_path, NOW + 61)
    assert "last known observed" in later["text"]


def test_expired_grant_downgrades_only_its_offer():
    data = resources([offer([{"available_count": 1, "expires_at": qs.iso(NOW)}]),
                      dict(offer([{"available_count": 1, "expires_at": qs.iso(NOW + 120)}]), id="later")])
    view = qr.account_resources(data, "cursor", "a", now=NOW)["resets"]
    assert view["state"] == "current" and view["rows"][0]["state"] == "last_known"
    assert view["rows"][1].get("state") != "last_known"
    assert qr.family_resets(data, "cursor", now=NOW)["available"] == 2
    assert qs.parse_instant(qc.earliest_reset(answer(data))) == NOW + 120


def test_zero_unknown_unreadable_and_boolean_grants_never_invent_an_expiry():
    grants = [{"available_count": count, "expires_at": qs.iso(NOW)} for count in (0, None, True, -1, "2")]
    grants += [{"available_count": 2, "expires_at": "unknown"}]
    data = resources([offer(grants)])
    assert qr.account_resources(data, "cursor", "a", now=NOW)["resets"]["state"] == "current"
    assert qc.earliest_reset(answer(data)) is None


def test_grant_beyond_display_bound_still_expires_aggregate(tmp_path):
    grants = [{"available_count": 0, "expires_at": qs.iso(NOW - 60)} for _ in range(qr.MAX_ROWS)]
    grants.append({"available_count": 2, "expires_at": qs.iso(NOW + 60)})
    data = resources([offer(grants)])
    row = qr.account_resources(data, "cursor", "a", now=NOW)["resets"]["rows"][0]
    assert len(row["grants"]) == qr.MAX_ROWS and row["grants_omitted"] == 1
    assert qs.parse_instant(publish(data, tmp_path)["valid_until"]) == NOW + 60
    assert qr.family_resets(data, "cursor", now=NOW + 60)["available"] == 0


def test_controls_in_scope_and_amount_are_quoted_without_changing_the_tool(tmp_path):
    data = resources()
    model, basis, number = "gpt\nINJECTED\r\tmodel", "quota\u2028INJECTED\x01basis", "12\nINJECTED\r34"
    data["availability_scope"].update(model=model, basis=basis)
    data["resources"][0]["balances"] = facet([{"id": "credit", "label": "Credit\nINJECTED", "amount": number,
                                               "unit": "usd", "currency": "USD"}])
    data["resources"][0]["spending"] = facet([{"id": "spend", "used": number, "limit": number, "unit": "usd"}])
    out = answer(data)
    assert out["availability_scope"]["model"] == model and out["availability_scope"]["basis"] == basis
    assert out["resources"]["cursor"]["facets"]["balances"][0]["rows"][0]["amount"] == number
    text = publish(data, tmp_path)["text"]
    assert "\\nINJECTED" in text and "\\u2028INJECTED" in text
    assert all(not line.startswith("INJECTED") for line in text.splitlines())
    assert not any(c in text for c in "\r\t\x01\u2028\u2029")
    assert '"12\\nINJECTED\\r34"' in text


@pytest.mark.parametrize("character", ["x", "😀", "\x01", "\\"])
def test_oversized_non_droppable_scope_is_bounded_and_disclosed(tmp_path, character):
    data = resources()
    data["availability_scope"].update(model=character * 20000, basis=character * 20000)
    text = publish(data, tmp_path)["text"]
    assert "characters" in text and "quota_summary" in text
    assert len(text) <= qc.MAX_CHARS and qc.envelope_bytes(text) <= qc.MAX_FILE_BYTES
    assert answer(data)["availability_scope"]["model"] == character * 20000


def test_omission_footer_is_bounded_for_many_long_family_names(tmp_path):
    data = resources()
    data["resources"] = [dict(copy.deepcopy(data["resources"][0]),
                              target={"harness": f"{n}-" + "😀" * 1000, "profile_id": "a"}) for n in range(90)]
    text = publish(data, tmp_path)["text"]
    assert "more families" in text and "left out" in text


def test_large_amount_is_omitted_whole_never_a_shorter_number(tmp_path):
    data = resources()
    number = "1234567890" * 80
    data["resources"][0]["balances"] = facet([{"id": "credit", "amount": number, "unit": "minor",
                                               "currency": "USD", "decimal_places": 2}])
    data["resources"][0]["spending"] = facet([{"id": "spend", "used": number, "limit": number, "unit": "minor"}])
    text = publish(data, tmp_path)["text"]
    assert "amount not shown (800 characters" in text and number[:40] not in text
    assert answer(data)["resources"]["cursor"]["facets"]["balances"][0]["rows"][0]["amount"] == number


def test_resource_ids_are_exact_not_truncated_or_whitespace_folded():
    data = resources()
    row = data["resources"][0]
    ids = ["a" * 200 + "1", "a" * 200 + "2", " spaced ", "spaced"]
    data["resources"] = [dict(copy.deepcopy(row), target={"harness": "cursor", "profile_id": key},
                              balances=facet([{"amount": str(n), "unit": "usd"}])) for n, key in enumerate(ids)]
    for n, key in enumerate(ids):
        own = qr.account_resources(data, "cursor", key, now=NOW)
        assert own and own["balances"]["rows"][0]["amount"] == str(n)
    assert qr.account_resources(data, "cursor", "a" * 200, now=NOW) is None


def test_resource_input_bound_names_the_rows_left_out(monkeypatch, tmp_path):
    monkeypatch.setattr(qs, "MAX_SNAPSHOTS", 2)
    data = resources()
    data["resources"] = [dict(copy.deepcopy(data["resources"][0]),
                              target={"harness": "cursor", "profile_id": str(n)}) for n in range(5)]
    out = answer(data)
    assert out["resources"]["cursor"]["accounts"] == 2 and out["resource_rows_omitted"] == 3
    assert "3 rows omitted at the input bound" in publish(data, tmp_path)["text"]


def test_small_explicit_bound_never_returns_an_oversized_fallback():
    data = answer(resources())
    text = qc.snapshot_text(data, bound=100)
    assert len(text) <= 100 and "omitted" in text
    with pytest.raises(ValueError, match="omission"):
        qc.snapshot_text(data, bound=1)
