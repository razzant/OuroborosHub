"""Resource expiry and hostile-length passive facts through the tool and snapshot.

Synthetic fixtures only. Values stay exact in the tool; the compact tail `text`
(`brief_text`) stays short and O(1) without resource/diagnostic listings, while
the optional `details` field (`snapshot_text`) quotes controls or explicitly
omits a value, never inventing an amount or ID.
"""
import copy
import json

import pytest

import plugin
import quota_context as qc
import quota_history
import quota_resources as qr
import quota_summary as qs
from test_reserve import NOW, payload, profile, snap, constraint, FIVE_H
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


def publish(data, path, now=NOW, *, store=None):
    latest = plugin.LatestRead()
    latest.put(data, "", now)
    assert plugin.publish_snapshot(_Api(path), store, data, "", now, latest) == "written"
    result = json.loads((path / qc.SNAPSHOT_FILE).read_text())
    assert host_reads(path / qc.SNAPSHOT_FILE)[0] == "fresh"
    assert len(result["text"]) <= qc.BRIEF_MAX_CHARS
    assert len(result.get("details") or "") <= qc.MAX_CHARS
    assert (path / qc.SNAPSHOT_FILE).stat().st_size <= qc.MAX_FILE_BYTES
    return result


@pytest.mark.parametrize("grant", [{"available_count": 2}, {"available_count": 0, "usable_now": True},
                                   {"available_count": 2, "usable_now": False}])
def test_positive_or_usable_grants_expire_facts_and_published_validity(tmp_path, grant):
    data = resources([offer([dict(grant, expires_at=qs.iso(NOW + 60)),
                             {"available_count": 1, "expires_at": qs.iso(NOW + 120)}])])
    current = answer(data)
    assert qs.parse_instant(qc.earliest_reset(current)) == NOW + 60
    first = publish(data, tmp_path)
    assert qs.parse_instant(first["valid_until"]) == NOW + 60
    assert "Manual resets" not in first["text"] and "Manual resets" in first["details"]
    assert qr.family_resets(data, "cursor", now=NOW + 59)["available"] == 2
    for at in (NOW + 60, NOW + 61):
        own = qr.account_resources(data, "cursor", "a", now=at)["resets"]
        assert own["state"] == "last_known" and own["rows"][0]["available_count"] == 2
        assert own["observed_at"] == data["resources"][0]["resets"]["observed_at"]
        assert qr.family_resets(data, "cursor", now=at)["available"] == 0
        out = answer(data, at)
        assert qc.earliest_reset(out) is None
        assert "last known observed" in (qc.snapshot_text(out) or "")
    later = publish(data, tmp_path, NOW + 61)
    assert later["text"] == first["text"]
    assert "last known observed" in later["details"]


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


@pytest.mark.parametrize("account_wide", [False, True], ids=["scoped-constraint", "whole-account"])
def test_published_cooldown_expires_at_its_known_end(tmp_path, account_wide):
    end = NOW + 30.125
    window = constraint("weekly", 0.25, reset=3600, models=["example"])
    reading = snap("codex", "a", [window])
    if account_wide:
        reading["availability"] = {"state": "cooldown", "resets_at": qs.iso_exact(end)}
    else:
        window["cooldown_until"] = qs.iso_exact(end)
    # The earlier, unmatched scope is not counted as a restriction here.
    reading["constraints"].append({"id": "image_cooldown", "applies_to_request_kinds": ["image"],
                                   "cooldown_until": qs.iso(NOW + 5)})
    data = payload([reading], [profile("codex", "a")], harnesses=("codex",))
    before = publish(data, tmp_path)
    assert "restriction flags" in before["text"] and "cooling 1" in before["details"]
    assert before["valid_until"] == qs.iso_exact(end)
    assert answer(data, end - 0.001)["groups"][0]["restrictions"]["cooling"]["accounts"] == 1
    after = publish(data, tmp_path, end)
    assert "restriction flags" not in after["text"] and "cooling" not in after["details"]
    assert qs.parse_instant(after["valid_until"]) == end + qc.VALID_FOR_SEC


@pytest.mark.parametrize("until", [None, "time not reported"])
def test_cooldown_without_a_known_end_does_not_invent_a_boundary(tmp_path, until):
    reading = snap("codex", "a", [constraint("weekly", 0.25, reset=3600)], availability="cooldown")
    reading["availability"]["resets_at"] = until
    data = payload([reading], [profile("codex", "a")], harnesses=("codex",))
    value = publish(data, tmp_path)
    assert "cooling 1" in value["details"] and "0.00 aw unflagged" in value["text"]
    assert qs.parse_instant(value["valid_until"]) == NOW + qc.VALID_FOR_SEC


def test_new_cooldown_deadline_tightens_unchanged_display(tmp_path):
    reading = snap("codex", "a", [constraint("weekly", 0.25, reset=3600)], availability="cooldown")
    data = payload([reading], [profile("codex", "a")], harnesses=("codex",))
    first = publish(data, tmp_path)
    end = NOW + 30.125
    reading["availability"]["resets_at"] = qs.iso_exact(end)
    second = publish(data, tmp_path, NOW + 1)
    assert first["text"] == second["text"] and first["details"] == second["details"]
    assert first["valid_until"] == qs.iso(NOW + qc.VALID_FOR_SEC)
    assert second["valid_until"] == qs.iso_exact(end)


def test_scoped_exhaustion_expires_snapshot_before_the_window_reset(tmp_path):
    end = NOW + 30.125
    window = constraint("weekly", 0.2, reset=3600, models=["example"])
    reading = snap("codex", "a", [window])
    reading["availability"]["model_scoped_exhaustions"] = [
        {"applies_to_models": ["other"], "resets_at": qs.iso(NOW + 5)},
        {"applies_to_models": ["example"], "resets_at": qs.iso_exact(end)},
        {"applies_to_models": ["example"], "resets_at": "unreadable"},
    ]
    data = payload([reading], [profile("codex", "a")], harnesses=("codex",))
    before = publish(data, tmp_path)
    assert before["valid_until"] == qs.iso_exact(end)
    assert "0.00 aw unflagged" in before["text"] and "held: model_exhausted 1" in before["details"]
    assert answer(data, end - 0.001)["groups"][0]["unrestricted_windows"] == 0.0
    assert answer(data, end)["groups"][0]["unrestricted_windows"] == 0.8
    after = publish(data, tmp_path, end)
    assert "0.80 aw" in after["text"] and "restriction flags" not in after["text"]
    assert "model_exhausted" not in after["details"]
    assert after["valid_until"] == qs.iso_exact(end + qc.VALID_FOR_SEC)

    # An already exhausted share doesn't get this restriction, so its own
    # unchanged reading needn't expire at the separate scope's earlier reset.
    window["used_ratio"] = 1.0
    spent = publish(data, tmp_path / "spent")
    assert "restriction flags" not in spent["text"]
    assert spent["valid_until"] == qs.iso_exact(NOW + qc.VALID_FOR_SEC)


@pytest.mark.parametrize("origin", ["payload", "history"])
def test_a_counted_last_known_window_expires_snapshot_at_its_reset(tmp_path, origin):
    end = NOW + 45.375
    kept = snap("codex", "b", [dict(constraint("weekly", 0.4), resets_at=qs.iso_exact(end))],
                fresh=(origin == "history"), observed=-120)
    data = payload([snap("codex", "a", [constraint("weekly", 0.2, reset=3600)]), kept],
                   [profile("codex", "a"), profile("codex", "b")], harnesses=("codex",))
    store = None
    if origin == "history":
        store = quota_history.HistoryStore(tmp_path)
        norm = qs.normalize(data, NOW - 120)
        store.record_sweep(qs.recordable(norm, NOW - 120), NOW - 120, ok=True, reason="")
        data["quota"].pop()

    before = publish(data, tmp_path, end - 0.001, store=store)
    assert before["valid_until"] == qs.iso_exact(end)
    assert "1 last-known" in before["text"] and "last known 0.60 from 1 not current" in before["details"]
    after = publish(data, tmp_path, end, store=store)
    assert "1 unknown" in after["text"] and "1 unknown" in after["details"]
    assert "last-known" not in after["text"] and "last known 0.60" not in after["details"]
    assert after["valid_until"] == qs.iso_exact(end + qc.VALID_FOR_SEC)


@pytest.mark.parametrize("origin", ["payload", "cached", "history"])
@pytest.mark.parametrize("window", [FIVE_H, None], ids=["five-hour", "no-window"])
def test_last_known_without_reset_uses_its_observation_age_bound(tmp_path, origin, window):
    read_at, end = NOW, NOW + 60.125
    observed = end - (window or qs.LAST_KNOWN_NO_WINDOW_SEC)
    kept = snap("codex", "b", [constraint("primary", 0.4, window)], fresh=origin != "payload")
    kept["observed_at"] = qs.iso_exact(observed)
    neighbor = snap("codex", "a", [constraint("primary", 0.2, window)])
    data = payload([neighbor, kept], [profile("codex", "a"), profile("codex", "b")], harnesses=("codex",))
    latest = plugin.LatestRead()
    store, cached, source = None, {}, data
    if origin == "history":
        store = quota_history.HistoryStore(tmp_path)
        store.record_sweep(qs.recordable(qs.normalize(data, observed), observed), observed, ok=True, reason="")
        data["quota"].pop()
    elif origin == "cached":
        latest.put(data, "", read_at - 5)
        source = {**data, "quota": [], "reads": {**data["reads"], "quota": "failed"}}
        data, cached = latest.effective(source)
        # Both readings are now carried; the older provider observation still sets the bound.
        assert cached == {"quota": read_at - 5}

    def at(moment):
        return plugin.answer_of(store, data, cached, source, read_at, moment, for_snapshot=True)

    before, equal, after = (at(t) for t in (read_at, end, end + 0.001))
    live_at_end = plugin.answer_of(store, data, cached, source, read_at, end)
    count = 2 if origin == "cached" else 1
    assert before["_quota_boundary"] == end
    assert equal["_quota_boundary"] == after["_quota_boundary"]
    assert equal["_quota_boundary"] is None or equal["_quota_boundary"] > end
    assert live_at_end["groups"][0]["last_known"]["accounts"] == count
    assert before["groups"][0]["last_known"]["accounts"] == count
    assert equal["groups"][0]["unknown_accounts"] == after["groups"][0]["unknown_accounts"] == 1
    assert equal["groups"][0].get("last_known", {}).get("accounts", 0) == count - 1
    assert all(qc.earliest_reset(row) is None for row in (before, equal, after))
    if origin == "cached":
        assert plugin.publish_snapshot(_Api(tmp_path), None, source, "", read_at, latest) == "skipped"
        assert not (tmp_path / qc.SNAPSHOT_FILE).exists()
    else:
        assert before["groups"][0]["remaining_windows"] == after["groups"][0]["remaining_windows"] == 0.8
        first = publish(data, tmp_path, read_at, store=store)
        assert first["valid_until"] == qs.iso_exact(end)
        assert "1 last-known" in first["text"] and "next reported reset" not in first["details"]
        second = publish(data, tmp_path, end, store=store)
        assert "1 unknown" in second["text"] and "1 last-known" not in second["text"]
        assert second["valid_until"] == qs.iso_exact(end + qc.VALID_FOR_SEC)


@pytest.mark.parametrize("prior_file", [True, False], ids=["replaces-fresh-file", "first-file"])
def test_exact_age_limit_does_not_suppress_fresh_changes_from_another_account(tmp_path, prior_file):
    end = NOW + 60.125
    a = snap("codex", "a", [constraint("primary", .1, FIVE_H)], observed=0)
    b = snap("codex", "b", [constraint("primary", .4, FIVE_H)], fresh=False)
    b["observed_at"] = qs.iso_exact(end - FIVE_H)
    profiles = [profile("codex", sid) for sid in ("a", "b")]
    path = tmp_path / qc.SNAPSHOT_FILE
    if prior_file:
        first = publish(payload([copy.deepcopy(a)], profiles, harnesses=("codex",)), tmp_path)
        assert "0.90 aw" in first["text"] and qs.parse_instant(first["valid_until"]) > end
        old_bytes = path.read_bytes()
    a["constraints"][0]["used_ratio"] = 1.0
    a["observed_at"] = qs.iso_exact(end)
    data = payload([a, b], profiles, harnesses=("codex",))
    if prior_file:
        assert host_reads(path)[0] == "fresh"
    second = publish(data, tmp_path, end)
    assert second["observed_at"] == qs.iso_exact(end)
    assert second["valid_until"] == qs.iso_exact(end + qc.VALID_FOR_SEC)
    assert "0.00 aw" in second["text"] and "1 unknown" in second["text"]
    assert "1 at the limit; 1 unknown" in second["details"] and "last known 0.60" not in second["details"]
    if prior_file:
        assert path.read_bytes() != old_bytes


@pytest.mark.parametrize("second_reset", [None, 240], ids=["value-conflict", "reset-conflict"])
def test_conflicting_current_sources_bound_snapshot_not_next_reset(tmp_path, second_reset):
    end = NOW + 30.125
    near = dict(constraint("weekly", .8), resets_at=qs.iso_exact(end))
    data = payload([
        snap("codex", "a", [constraint("weekly", .5)]),
        snap("codex", "b", [near], source="first"),
        snap("codex", "b", [constraint("weekly", .1, reset=second_reset)], source="second"),
    ], [profile("codex", sid) for sid in ("a", "b")], harnesses=("codex",))
    [before] = answer(data)["groups"]
    assert before["remaining_windows"] == .5 and before["of_accounts"] == 1
    assert before["coverage"]["conflicting"] == 1 and before["next_reset"] is None
    first = publish(data, tmp_path)
    assert first["valid_until"] == qs.iso_exact(end)
    assert "0.50 aw" in first["text"] and "next reported reset" not in first["details"]
    [after] = answer(data, end)["groups"]
    assert after["remaining_windows"] == 1.4 and after["of_accounts"] == 2
    second = publish(data, tmp_path, end)
    assert "1.40 aw" in second["text"] and second["text"] != first["text"]
    expected_end = NOW + second_reset if second_reset is not None else end + qc.VALID_FOR_SEC
    assert second["valid_until"] == qs.iso_exact(expected_end)


@pytest.mark.parametrize("reported", [True, False], ids=["known-end", "unknown-end"])
def test_visible_hold_uses_offscreen_blocker_reset_but_not_another_kind(tmp_path, reported):
    end = NOW + 30.125
    # The applicable and the earlier, image-only blockers sort beyond MAX_GROUPS.
    # Filler windows are on another account because the per-account input is also bounded.
    shown = dict(constraint("visible", 0.2, FIVE_H), applies_to_request_kinds=["text"])
    blocker = dict(constraint("hidden-shared", 1.0, 60000),
                   resets_at=qs.iso_exact(end) if reported else None)
    unrelated = dict(constraint("hidden-image", 1.0, 65000, reset=5), applies_to_request_kinds=["image"])
    filler = [constraint(f"filler-{i:02d}", 0.1, 30000) for i in range(qs.MAX_GROUPS - 1)]
    data = payload([snap("codex", "a", [shown, blocker, unrelated]), snap("codex", "b", filler)],
                   [profile("codex", "a"), profile("codex", "b")], harnesses=("codex",))
    full = plugin.answer_of(None, data, {}, data, NOW, NOW, for_snapshot=True)
    assert len(full["groups"]) == qs.MAX_GROUPS
    assert not any(g["limit"].startswith("hidden-") for g in full["groups"])
    assert qc.earliest_reset(full) is None

    def visible(at):
        return next(g for g in answer(data, at)["groups"] if g["limit"] == "visible")

    first = publish(data, tmp_path)
    assert "other_limit_spent 1" in first["details"] and "0.00 aw unflagged" in first["text"]
    assert visible(end - 0.001)["unrestricted_windows"] == 0.0
    if reported:
        assert first["valid_until"] == qs.iso_exact(end)
        assert not visible(end).get("restrictions") and visible(end)["unrestricted_windows"] == 0.8
        assert "other_limit_spent" not in publish(data, tmp_path, end)["details"]
    else:
        assert first["valid_until"] == qs.iso_exact(NOW + qc.VALID_FOR_SEC)
        assert visible(end)["restrictions"]["other_limit_spent"]["accounts"] == 1


@pytest.mark.parametrize(("used", "tool_words", "detail_words"), [
    (0.9999, "plus <0.01 last known", "last known <0.01 from 1 not current"),
    (1.0, "plus 0.0 last known", "last known 0.00 from 1 not current"),
    (None, "1 unknown, not counted", "1 unknown"),
])
def test_last_known_tiny_zero_and_unknown_keep_numeric_contract(tmp_path, used, tool_words, detail_words):
    data = payload([snap("codex", "a", [constraint("weekly", used, reset=3600)], fresh=False)],
                   [profile("codex", "a")], harnesses=("codex",))
    latest = plugin.LatestRead()
    latest.put(data, "", NOW)
    for detail in (False, True):
        [row] = plugin.tool_answer(_Api(tmp_path), latest, detail=detail, now=NOW)["groups"]
        assert tool_words in row["headline"]
        assert row["remaining_windows"] == 0.0 and row["of_accounts"] == 0
        assert "_last_known_at_limit" not in row
        if used is None:
            assert "last_known" not in row and row["unknown_accounts"] == 1
        else:
            assert row["last_known"] == {"windows": 0.0, "accounts": 1, "oldest_observed_at": qs.iso(NOW - 60)}
            assert row["remaining_windows_with_last_known"] == 0.0
    value = publish(data, tmp_path)
    assert detail_words in value["details"]
    assert ("1 unknown" if used is None else "1 last-known") in value["text"]


@pytest.mark.parametrize(("unflagged", "flagged", "shown"), [(0.9999, 1.0, "<0.01"), (1.0, 0.9999, "0.00")])
def test_swapped_tiny_and_zero_unflagged_reserve_agree_in_all_three_texts(tmp_path, unflagged, flagged, shown):
    data = payload([snap("codex", "a", [constraint("weekly", unflagged, reset=3600)]),
                    snap("codex", "b", [constraint("weekly", flagged, reset=3600)])],
                   [profile("codex", "a"), profile("codex", "b", enabled=False)], harnesses=("codex",))
    latest = plugin.LatestRead()
    latest.put(data, "", NOW)
    for detail in (False, True):
        [row] = plugin.tool_answer(_Api(tmp_path), latest, detail=detail, now=NOW)["groups"]
        assert row["remaining_windows"] == row["unrestricted_windows"] == 0.0
        assert row["accounts_at_limit"] == 1 and row["restrictions"] == {"disabled": {"accounts": 1, "windows": 0.0}}
        assert f"{shown} account-windows unflagged" in row["headline"]
        assert "_unrestricted_has_room" not in row
    value = publish(data, tmp_path)
    assert f"{shown} aw unflagged" in value["text"]
    assert f"{shown} account-windows unflagged" in value["details"]


def test_unknown_account_state_is_not_a_proven_hold_in_any_text(tmp_path):
    data = payload([snap("codex", "a", [constraint("weekly", 0.2, reset=3600)])],
                   harnesses=("codex",), unified=True,
                   reads={"catalog": "ok", "accounts": "failed", "quota": "ok"})
    [row] = answer(data)["groups"]
    assert row["restrictions"] == {"account_state_unknown": {"accounts": 1, "windows": 0.8}}
    assert row["unrestricted_windows"] == 0.0  # keep the numeric contract
    value = publish(data, tmp_path)
    for words in (row["headline"], value["text"], value["details"]):
        assert "account state unknown" in words
        assert "held:" not in words and "restriction flags" not in words and "0.00 aw unflagged" not in words


@pytest.mark.parametrize(("used", "fixed", "avg", "headline", "at_limit"), [
    (0.9999, "<0.01", "<0.1%", "<0.01 account-windows left", 0),
    (1.0, "0.00", "0.0%", "0.0 account-windows left", 1),
    (None, None, None, "no current reading", 0),
])
def test_legacy_tool_and_snapshot_distinguish_tiny_zero_and_no_reading(tmp_path, used, fixed, avg, headline, at_limit):
    data = payload([snap("codex", "a", [constraint("weekly", used, reset=3600)])],
                   [profile("codex", "a")], harnesses=("codex",))
    latest = plugin.LatestRead()
    latest.put(data, "", NOW)
    for detail in (False, True):
        [row] = plugin.tool_answer(_Api(tmp_path), latest, detail=detail, now=NOW)["groups"]
        assert headline in row["headline"]
        assert row["accounts_at_limit"] == at_limit
        assert row["remaining_windows"] == 0.0  # compatible projection, not a display assertion
        assert row["average_remaining_pct"] == (None if used is None else 0.0)
    value = publish(data, tmp_path)
    if used is None:
        assert "no current reading" in value["text"] and "no current reading" in value["details"]
        assert "0.00 aw" not in value["text"] and "avg " not in value["details"]
    else:
        assert f"{fixed} aw" in value["text"] and f"{fixed} left across" in value["details"]
        assert f"avg {avg}" in value["details"]


@pytest.mark.parametrize(("subject", "used", "fresh", "wording"), [
    ("a", 0.25, False, "last-known"),
    ("a", None, True, "1 unknown"),
    (None, 0.25, True, "No quota windows or account resources represented in this summary."),
])
def test_failed_roster_describes_projected_coverage_not_provider_absence(tmp_path, subject, used, fresh, wording):
    data = payload([snap("codex", subject, [constraint("weekly", used, reset=3600)], fresh=fresh)],
                   unified=True, reads={"accounts": "failed", "quota": "ok", "catalog": "ok"})
    value = publish(data, tmp_path)
    for view in ("text", "details"):
        assert "Account list: unknown; counts cover this summary only." in value[view]
        assert "only measured accounts" not in value[view]
    assert wording in value["text"]
    if subject is None:
        assert qs.attribute(data).unattributed["codex"] == 1 and wording in value["details"]
    else:
        assert "no current reading (0/1 measured" in value["text"]


def test_controls_in_scope_and_amount_are_quoted_without_changing_the_tool(tmp_path):
    data = resources()
    model, basis, number = "gpt\nINJECTED\r\tmodel", "quota\u2028INJECTED\x01basis", "12\nINJECTED\r34"
    data["availability_scope"].update(model=model, basis=basis)
    data["resources"][0]["balances"] = facet([{"id": "credit", "label": "Credit\nINJECTED", "amount": number,
                                               "unit": "usd", "currency": "USD"}])
    data["resources"][0]["spending"] = facet([{"id": "spend", "used": number, "limit": number, "unit": "usd"}])
    data["quota"] = [snap("cursor", "a", [constraint("5h", 0.2, FIVE_H, reset=3600, models=[model],
                                                     label="5h\nINJECTED")])]
    out = answer(data)
    assert out["availability_scope"]["model"] == model and out["availability_scope"]["basis"] == basis
    assert out["resources"]["cursor"]["facets"]["balances"][0]["rows"][0]["amount"] == number
    result = publish(data, tmp_path)
    text, details = result["text"], result["details"]
    assert "\\nINJECTED" in text
    assert all(not line.startswith("INJECTED") for line in text.splitlines())
    assert not any(c in text for c in "\r\t\x01\u2028\u2029")
    assert '"12\\nINJECTED\\r34"' not in text
    assert "\\nINJECTED" in details and "\\u2028INJECTED" in details
    assert all(not line.startswith("INJECTED") for line in details.splitlines())
    assert not any(c in details for c in "\r\t\x01\u2028\u2029")
    assert '"12\\nINJECTED\\r34"' in details


@pytest.mark.parametrize("character", ["x", "😀", "\x01", "\\"])
def test_oversized_non_droppable_scope_is_bounded_and_disclosed(tmp_path, character):
    data = resources()
    data["availability_scope"].update(model=character * 20000, basis=character * 20000)
    result = publish(data, tmp_path)
    text, details = result["text"], result["details"]
    assert "characters" in details and "quota_summary" in details
    assert len(text) <= qc.BRIEF_MAX_CHARS and len(details) <= qc.MAX_CHARS
    assert qc.envelope_bytes(text, details) <= qc.MAX_FILE_BYTES
    assert answer(data)["availability_scope"]["model"] == character * 20000


def test_omission_footer_is_bounded_for_many_long_family_names(tmp_path):
    data = resources()
    data["resources"] = [dict(copy.deepcopy(data["resources"][0]),
                              target={"harness": f"{n}-" + "😀" * 1000, "profile_id": "a"}) for n in range(90)]
    result = publish(data, tmp_path)
    assert "more families" in result["text"] and "left out" in result["text"]
    assert "more families" in result["details"] and "left out" in result["details"]


def test_large_amount_is_omitted_whole_never_a_shorter_number(tmp_path):
    data = resources()
    number = "1234567890" * 80
    data["resources"][0]["balances"] = facet([{"id": "credit", "amount": number, "unit": "minor",
                                               "currency": "USD", "decimal_places": 2}])
    data["resources"][0]["spending"] = facet([{"id": "spend", "used": number, "limit": number, "unit": "minor"}])
    result = publish(data, tmp_path)
    assert "amount not shown (800 characters" in result["details"] and number[:40] not in result["details"]
    assert number[:40] not in result["text"]
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
    result = publish(data, tmp_path)
    assert "3 resource rows omitted at input bound" in result["text"]
    assert "3 rows omitted at the input bound" in result["details"]


def test_small_explicit_bound_never_returns_an_oversized_fallback():
    data = answer(resources())
    text = qc.brief_text(data, bound=100)
    assert len(text) <= 100 and "omitted" in text
    with pytest.raises(ValueError, match="omission"):
        qc.brief_text(data, bound=1)
    detail = qc.snapshot_text(data, bound=100)
    assert detail is not None and len(detail) <= 100 and "omitted" in detail
    with pytest.raises(ValueError, match="omission"):
        qc.snapshot_text(data, bound=1)
    short = qc.brief_text(data)
    assert qc.snapshot_text(data, reserved_text=short, byte_bound=qc.envelope_bytes(short) + 10) is None
