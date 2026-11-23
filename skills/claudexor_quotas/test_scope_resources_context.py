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


def overlapping_restrictions_payload():
    """One account, with a disabled credential and a spent sibling window."""
    data = payload(
        [snap("codex", "synthetic", [
            constraint("five_hour", 1.0, FIVE_H, reset=240),
            constraint("seven_day", 0.25, WEEK, reset=3600),
            constraint("unread", None, WEEK, reset=3600),
        ], observed_abs=NOW)],
        [profile("codex", "synthetic", enabled=False)], harnesses=("codex",),
    )
    data["availability_scope"] = {"model": "sample-restrictions-model", "requestKind": "text",
                                  "basis": "quota_block"}
    return data


@pytest.mark.parametrize(("other_ratio", "expected"), [(None, "0.00"), (0.6, "0.40"), (0.9999, "<0.01")])
def test_published_reserve_does_not_count_overlapping_restrictions_twice(tmp_path, other_ratio, expected):
    data = overlapping_restrictions_payload()
    if other_ratio is not None:
        data["profiles"]["profiles"].append(profile("codex", "unflagged"))
        data["quota"].append(snap("codex", "unflagged", [constraint("seven_day", other_ratio, WEEK, reset=3600)]))
    weekly = next(g for g in plugin.answer_of(None, data, {}, data, NOW, NOW)["groups"]
                  if g["limit"] == "seven_day")
    assert weekly["of_accounts"] == (1 if other_ratio is None else 2)
    assert weekly["restrictions"] == {
        "disabled": {"accounts": 1, "windows": 0.75},
        "other_limit_spent": {"accounts": 1, "windows": 0.75},
    }
    assert weekly["unrestricted_windows"] == (0.40 if other_ratio == 0.6 else 0.0)
    assert "_unrestricted_has_room" not in weekly

    latest = plugin.LatestRead()
    latest.put(data, "", NOW)
    assert plugin.publish_snapshot(_Api(tmp_path), None, data, "", NOW, latest) == "written"
    state, value = host_reads(tmp_path / quota_context.SNAPSHOT_FILE)
    assert state == "fresh"
    lines = value["text"].splitlines()
    reserve = next(line for line in lines if "seven_day" in line)
    assert "restriction flags" in reserve and f"{expected} aw unflagged" in reserve
    assert "at limit" not in reserve
    assert not re.search(r"\b\d+ held\b", reserve)
    spent = next(line for line in lines if "five_hour" in line)
    assert "0.00 aw" in spent and "1 at limit" in spent
    unread = next(line for line in lines if "unread" in line)
    assert "no current reading" in unread and "1 unknown" in unread and "0.00 aw" not in unread
    weekly_detail = next(line for line in value["details"].splitlines() if "seven_day" in line)
    assert "held: disabled 1, other_limit_spent 1" in weekly_detail


def _answer(tmp_path):
    store = fed_store(tmp_path)
    data = at_time(NOW)
    latest = plugin.LatestRead()
    latest.put(data, "", NOW)
    effective, cached = latest.effective(data)
    return plugin.answer_of(store, effective, cached, data, NOW, NOW), store, latest, data


def test_snapshot_text_is_deterministic_bounded_and_matches_the_tool_numbers(tmp_path):
    answer, _store, _latest, _data = _answer(tmp_path)
    text = quota_context.brief_text(answer)
    details = quota_context.snapshot_text(answer, reserved_text=text)
    assert text == quota_context.brief_text(answer) and len(text) <= quota_context.BRIEF_MAX_CHARS
    assert details == quota_context.snapshot_text(answer, reserved_text=text) and details is not None
    assert len(details) <= quota_context.MAX_CHARS
    claude = answer["joint"]["claude"]
    assert claude["same_account_capacity"] == pytest.approx(0.5, abs=1e-3)
    assert "same-account" not in text and "account-hours" not in text
    assert (f"≈{round(claude['same_account_capacity'], 1):.1f} from {claude['accounts_counted']} "
            f"of {claude['accounts']}") in details
    assert f"≈{quota_context.rounded_hours(claude['conditional_account_hours'])} account-hours" in details
    for row in answer["groups"]:  # every limit's figure in both views is the tool's own
        assert f"{row['remaining_windows']:.2f} aw" in text or "no current reading" in text
        assert f"{row['remaining_windows']:.2f} left" in details or "no current reading" in details
    # no relative age, no per-refresh clock, no shortened text
    assert not re.search(r"\b\d+\s*(min|h|s)\s+ago\b", text) and "[:" not in text
    assert quota_context.DETAIL_READER in text and "quota_summary" in text
    assert quota_context.envelope_bytes(text, details) <= quota_context.MAX_FILE_BYTES
    tiny_brief = quota_context.brief_text(answer, bound=385)
    assert len(tiny_brief) <= 385 and "left out" in tiny_brief and tiny_brief.startswith(quota_context.BRIEF_HEADER)
    small_brief = quota_context.brief_text(answer, byte_bound=500)
    assert (quota_context.envelope_bytes(small_brief) <= 500 and "left out" in small_brief
            and small_brief.startswith(quota_context.BRIEF_HEADER))
    tiny_detail = quota_context.snapshot_text(answer, bound=700)
    assert len(tiny_detail) <= 700 and "left out" in tiny_detail and tiny_detail.startswith(quota_context.HEADER)
    small_detail = quota_context.snapshot_text(answer, byte_bound=900)
    assert (quota_context.envelope_bytes(small_detail) <= 900 and "left out" in small_detail
            and small_detail.startswith(quota_context.HEADER))


# The host's reader of the file (ouroboros/skill_context_snapshot.py _read_snapshot), as this skill must satisfy it:
# at most MAX_SNAPSHOT_BYTES of the whole file (one byte more is "snapshot_oversized"), UTF-8 JSON, schema_version
# the integer 1, a non-empty text, timezone-aware dates with observed_at not after valid_until.
HOST_MAX_SNAPSHOT_BYTES = 32768


def host_reads(path):
    with open(path, "rb") as stream:
        raw = stream.read(HOST_MAX_SNAPSHOT_BYTES + 1)
    if len(raw) > HOST_MAX_SNAPSHOT_BYTES:
        return "snapshot_oversized", None
    value = json.loads(raw.decode("utf-8"))
    observed, valid = (qs.parse_instant(value[k]) for k in ("observed_at", "valid_until"))
    assert type(value["schema_version"]) is int and value["schema_version"] == 1
    assert isinstance(value["text"], str) and value["text"].strip() and observed <= valid
    value["text"].encode("utf-8")
    return "fresh", value


def wide_resources(accounts=12, rows=12):
    """A resource-only family whose engine text is multi-byte and needs JSON escapes: every account reports
    twelve diagnostics, each code 64 characters (the reader's cap) of four-byte, three-byte, quote, backslash and
    control characters. Distinct per account, so no line folds into a counted cohort."""
    data = payload([], [profile("cursor", f"c{a}") for a in range(accounts)], harnesses=("cursor",))
    data["availability_scope"] = {"model": None, "requestKind": "text", "basis": "quota_block"}
    data["resources"] = [{
        "target": {"harness": "cursor", "profile_id": f"c{a}"},
        "balances": facet(None, "unknown"), "spending": facet(None, "unknown"), "resets": facet(None, "unknown"),
        "diagnostics": facet([{"code": (f"{a:02d}{r:02d}" + "\U0001F600" * 56 + "\u20ac\"\\\u0001")[:64]}
                              for r in range(rows)]),
    } for a in range(accounts)]
    return data


def test_multibyte_and_escaped_provider_text_is_published_within_the_hosts_byte_bound(tmp_path):
    data = wide_resources()
    data["resources"].append(dict(data["resources"][0], target={"harness": "late", "profile_id": "synthetic"},
                                  diagnostics=facet([{"code": "SYNTHETIC_LAST_VALUE"}])))
    data["profiles"]["profiles"].append(profile("late", "synthetic"))
    latest = plugin.LatestRead()
    latest.put(data, "", NOW)
    effective, cached = latest.effective(data)
    answer = plugin.answer_of(None, effective, cached, data, NOW, NOW)
    text = quota_context.brief_text(answer)
    assert len(text) <= quota_context.BRIEF_MAX_CHARS and "\U0001F600" not in text
    assert "late: no window in this summary; resources: see quota_summary" in text
    whole = quota_context.snapshot_text(answer, bound=10 ** 9, byte_bound=10 ** 9)
    # under the detail character bound, over the host's file bound: what 12,000 characters alone let through
    assert whole is not None and len(whole) <= quota_context.MAX_CHARS
    assert quota_context.envelope_bytes(text, whole) > HOST_MAX_SNAPSHOT_BYTES
    details = quota_context.snapshot_text(answer, reserved_text=text)
    assert details is not None and len(details) <= quota_context.MAX_CHARS
    assert "SYNTHETIC_LAST_VALUE" in whole and "SYNTHETIC_LAST_VALUE" not in details
    assert quota_context.envelope_bytes(text, details) <= HOST_MAX_SNAPSHOT_BYTES
    lines, kept = details.split("\n"), set(whole.split("\n"))
    omission = [line for line in lines if "left out" in line]
    assert len(omission) == 1 and omission[0] == lines[-1] and "cursor" in omission[0] and "quota_summary" in omission[0]
    assert all(line in kept for line in lines[:-1]), "only whole lines of the full detail, none cut"
    assert details.startswith(quota_context.HEADER) and details != whole
    # the producer's own file, read as the host reads it
    assert plugin.publish_snapshot(_Api(tmp_path), None, data, "", NOW, latest) == "written"
    path = tmp_path / quota_context.SNAPSHOT_FILE
    state, value = host_reads(path)
    assert state == "fresh" and value["text"] == text and value["details"] == details
    assert path.stat().st_size <= HOST_MAX_SNAPSHOT_BYTES
    raw = path.read_bytes()
    assert b"\\u0001" in raw and "\U0001F600".encode("utf-8") in raw  # escaped control, raw UTF-8 emoji in details
    assert len(raw) <= quota_context.envelope_bytes(text, details)


def test_an_oversized_previous_file_is_no_snapshot_and_an_oversized_text_is_never_written(tmp_path):
    answer, _store, latest, data = _answer(tmp_path)
    api = _Api(tmp_path)
    text = quota_context.brief_text(answer)
    path = tmp_path / quota_context.SNAPSHOT_FILE
    # A file the host reads as oversized, with this very text and a validity far ahead: never "unchanged".
    big = {"schema_version": 1, "observed_at": qs.iso(NOW), "valid_until": qs.iso(NOW + 400), "text": text,
           "pad": "x" * HOST_MAX_SNAPSHOT_BYTES}
    path.write_text(json.dumps(big), encoding="utf-8")
    assert host_reads(path)[0] == "snapshot_oversized"
    assert quota_context.read_snapshot(path) is None
    assert plugin.publish_snapshot(api, None, data, "", NOW + 10, latest) == "written"
    assert host_reads(path)[0] == "fresh"
    before = path.read_bytes()
    # A text or detail payload that exceeds MAX_FILE_BYTES is refused before anything is written.
    with pytest.raises(ValueError):
        quota_context.publish(tmp_path, "\U0001F600" * 9000, NOW + 20, None, qs.iso_exact, qs.parse_instant)
    with pytest.raises(ValueError):
        quota_context.publish(tmp_path, text, NOW + 25, None, qs.iso_exact, qs.parse_instant,
                              details="\U0001F600" * 9000)
    assert path.read_bytes() == before and not list(tmp_path.glob("context-snapshot.json.*.tmp"))


def test_snapshot_is_written_once_renewed_near_expiry_and_never_for_a_failed_sweep(tmp_path):
    answer, store, latest, data = _answer(tmp_path)
    api = _Api(tmp_path)
    assert plugin.publish_snapshot(api, store, data, "", NOW, latest) == "written"
    path = tmp_path / quota_context.SNAPSHOT_FILE
    first = json.loads(path.read_text())
    assert first["schema_version"] == 1 and first["observed_at"] == qs.iso(NOW)
    assert isinstance(first.get("details"), str) and first["details"]
    assert qs.parse_instant(first["valid_until"]) <= NOW + quota_context.VALID_FOR_SEC
    # an unchanged snapshot two minutes later is not rewritten: observed_at does not churn
    assert plugin.publish_snapshot(api, store, data, "", NOW + 120, latest) == "unchanged"
    assert json.loads(path.read_text()) == first
    # near the end of its validity the same snapshot is renewed with a fresh observation
    assert plugin.publish_snapshot(api, store, data, "", NOW + 300, latest) == "written"
    renewed = json.loads(path.read_text())
    assert renewed["text"] == first["text"] and renewed["details"] == first["details"]
    assert renewed["observed_at"] == qs.iso(NOW + 300)
    # a failed or unreadable sweep writes nothing at all: the file simply expires
    assert plugin.publish_snapshot(api, store, None, "boom", NOW + 600, latest) == "skipped"
    assert plugin.publish_snapshot(api, store, dict(data, reads={"catalog": "ok", "accounts": "ok", "quota": "failed"}),
                                   "", NOW + 600, latest) == "skipped"
    assert json.loads(path.read_text()) == renewed
    assert not list(tmp_path.glob("context-snapshot.json.*.tmp"))


def test_short_snapshot_is_account_count_independent_and_omits_resources_and_accounts(tmp_path):
    # Single simple window via live publish stays substantially below 581 chars.
    single = payload([snap("claude", "a", [constraint("five_hour", 0.25, FIVE_H, reset=3600)])],
                     [profile("claude", "a")], harnesses=("claude",))
    single_latest = plugin.LatestRead()
    single_latest.put(single, "", NOW)
    single_dir = tmp_path / "single"
    assert plugin.publish_snapshot(_Api(single_dir), None, single, "", NOW, single_latest) == "written"
    single_state, single_pub = host_reads(single_dir / quota_context.SNAPSHOT_FILE)
    assert single_state == "fresh" and len(single_pub["text"]) < 350
    assert "0.75 aw (1/1 measured)" in single_pub["text"] and quota_context.DETAIL_READER in single_pub["text"]
    assert single_pub["details"].startswith(quota_context.HEADER)

    def make_wide(n_accounts):
        snaps, profs, res = [], [], []
        for idx in range(n_accounts):
            pid = f"acct_{idx:02d}"
            profs.append(profile("claude", pid))
            snaps.append(snap("claude", pid, [
                constraint("five_hour", 0.25, FIVE_H, reset=3600),
                constraint("seven_day", 0.40, WEEK, reset=86400),
                constraint("weekly_fable", 0.10, WEEK, reset=86400, models=["fable"], label="7 day (Fable)"),
            ]))
            profs.append(profile("codex", pid))
            snaps.append(snap("codex", pid, [
                constraint("primary", 0.30, FIVE_H, reset=7200),
                constraint("secondary", 0.50, WEEK, reset=172800),
            ]))
            profs.append(profile("cursor", pid))
            res.append({
                "target": {"harness": "cursor", "profile_id": pid},
                "balances": facet([{"id": f"b{idx}", "label": "Credit", "amount": str(9950 + idx),
                                    "unit": "minor", "currency": "USD", "decimal_places": 2,
                                    "scope": "personal", "usage_kind": "bonus"}]),
                "spending": facet([{"id": f"s{idx}", "label": "Spend", "used": str(100 + idx),
                                    "limit": "5000", "unit": "minor", "currency": "USD", "decimal_places": 2}]),
                "resets": facet([{"id": f"r{idx}", "kind": "granted_reset", "label": "Manual reset",
                                  "available_count": 1, "usable_now": True}]),
                "diagnostics": facet([{"code": f"DIAG_{idx:02d}"}]),
            })
        data = payload(snaps, profs, harnesses=("claude", "codex", "cursor"))
        data["resources"] = res
        data["availability_scope"] = {"model": None, "requestKind": "text", "basis": "quota_block"}
        return data

    def publish_wide(n_accounts, subdir):
        data = make_wide(n_accounts)
        latest = plugin.LatestRead()
        latest.put(data, "", NOW)
        out_dir = tmp_path / subdir
        assert plugin.publish_snapshot(_Api(out_dir), None, data, "", NOW, latest) == "written"
        path = out_dir / quota_context.SNAPSHOT_FILE
        state, published = host_reads(path)
        assert state == "fresh" and path.stat().st_size <= HOST_MAX_SNAPSHOT_BYTES
        return published

    small_pub = publish_wide(3, "small")
    large_pub = publish_wide(60, "large")
    small_text, large_text = small_pub["text"], large_pub["text"]
    assert len(small_text) <= quota_context.BRIEF_MAX_CHARS and len(large_text) <= quota_context.BRIEF_MAX_CHARS
    assert abs(len(large_text) - len(small_text)) <= 32, "short text must not grow linearly with account count"
    for forbidden in ("acct_00", "acct_59", "99.50", "DIAG_00", "Manual reset", "same-account"):
        assert forbidden not in small_text and forbidden not in large_text
    assert quota_context.DETAIL_READER in large_text and "quota_summary" in large_text
    assert "99.50 USD" in small_pub["details"] and "DIAG_00" in small_pub["details"]
    assert "99.50 USD" in large_pub["details"] and "manual reset offers reported usable: 60" in large_pub["details"]
    assert "left out" in large_pub["details"] and len(large_pub["details"]) <= quota_context.MAX_CHARS


def test_short_snapshot_prioritizes_fresh_families_and_bounds_many_windows(tmp_path):
    harnesses = ("claude", "codex", "agy", "gemini", "qwen")
    snaps, profs = [], []
    for hid in harnesses:
        profs.append(profile(hid, "a"))
        snaps.append(snap(hid, "a", [
            constraint("five_hour", 0.6, FIVE_H, reset=3600),
            constraint("seven_day", 0.3, WEEK, reset=86400),
            constraint("scoped_m", 0.2, WEEK, reset=86400, models=[f"{hid}-pro"], label=f"{hid} pro"),
        ]))
        profs.append(profile(hid, "stale"))
        snaps.append(snap(hid, "stale", [
            constraint("stale_only", 0.5, WEEK, reset=86400, label=f"{hid} stale"),
        ], fresh=False))
    data = payload(snaps, profs, harnesses=harnesses)
    latest = plugin.LatestRead()
    latest.put(data, "", NOW)
    assert plugin.publish_snapshot(_Api(tmp_path), None, data, "", NOW, latest) == "written"
    path = tmp_path / quota_context.SNAPSHOT_FILE
    state, published = host_reads(path)
    assert state == "fresh" and path.stat().st_size <= HOST_MAX_SNAPSHOT_BYTES
    text = published["text"]
    assert len(text) <= quota_context.BRIEF_MAX_CHARS and "left out" in text
    # Fresh measured family representation takes precedence over unmeasured stale windows.
    lines = text.splitlines()
    for hid in harnesses:
        assert any(line.startswith(f"{hid.title()} five_hour (5 hours): 0.40 aw (1/1 measured")
                   for line in lines), text
    assert "stale_only" not in text and "stale" not in text.lower()
    assert "stale" in published["details"].lower()


def test_short_snapshot_keeps_windows_scopes_and_missing_data_honest(tmp_path):
    rows = [
        snap("claude", "a", [
            constraint("five_hour", 0.5, FIVE_H, reset=3600),
            constraint("weekly_fable", 1.0, WEEK, reset=86400, models=["fable"], label="7 day (Fable)"),
        ]),
        snap("claude", "b", [
            constraint("five_hour", 0.5, FIVE_H, reset=3600),
        ]),
        snap("codex", "c1", [
            constraint("primary", 0.996, FIVE_H, reset=3600),
        ]),
        snap("agy", "g1", [
            dict(constraint("gemini_img", 0.2, FIVE_H, reset=3600, label="Gemini image"),
                 applies_to_model_prefixes=["gemini-"], applies_to_request_kinds=["image"]),
        ], fresh=False),
    ]
    data = payload(rows, [profile("claude", "a"), profile("claude", "b"), profile("codex", "c1"), profile("agy", "g1")],
                   harnesses=("claude", "codex", "agy"))
    latest = plugin.LatestRead()
    latest.put(data, "", NOW)
    effective, cached = latest.effective(data)
    ans = plugin.answer_of(None, effective, cached, data, NOW, NOW)
    near_zero_row = next(g for g in ans["groups"] if g["key"].startswith("codex|primary|"))
    assert near_zero_row["remaining_windows"] == 0.0 and near_zero_row["of_accounts"] == 1
    assert near_zero_row["accounts_at_limit"] == 0
    assert plugin.publish_snapshot(_Api(tmp_path), None, data, "", NOW, latest) == "written"
    state, published = host_reads(tmp_path / quota_context.SNAPSHOT_FILE)
    assert state == "fresh"
    lines = published["text"].splitlines()
    assert "1 aw = one account's full quota window" in lines[0] and "1h watched pace" in lines[0]
    five_h = next(line for line in lines if "five_hour (5 hours):" in line)
    fable = next(line for line in lines if "7 day (Fable) (week) [model fable]:" in line)
    near_zero = next(line for line in lines if "Codex primary (5 hours):" in line)
    gemini = next(line for line in lines if "Gemini image (5 hours) [prefix gemini-*; kinds image]:" in line)
    assert "1.00 aw (2/2 measured)" in five_h
    assert "0.00 aw (1/1 measured, 1 at limit, 1 applicability unknown)" in fable
    assert "<0.01 aw (1/1 measured)" in near_zero and "0.00 aw" not in near_zero
    assert "no current reading (0/1 measured, 1 last-known)" in gemini and "0.00 aw" not in gemini


def test_scope_names_count_display_omissions_separately_from_summary_cap(tmp_path):
    models = [f"model-{n:02d}" for n in range(30)]
    prefixes = [f"family-{n:02d}-" for n in range(30)]
    window = dict(constraint("weekly", 0.25, reset=3600, models=models), applies_to_model_prefixes=prefixes)
    data = payload([snap("codex", "a", [window])], [profile("codex", "a")], harnesses=("codex",))
    latest = plugin.LatestRead()
    latest.put(data, "", NOW)
    [row] = plugin.tool_answer(_Api(tmp_path), latest, detail=True, now=NOW)["groups"]
    assert (row["models"], row["models_omitted"]) == (models[:24], 6)
    assert (row["model_prefixes"], row["model_prefixes_omitted"]) == (prefixes[:24], 6)
    assert plugin.publish_snapshot(_Api(tmp_path), None, data, "", NOW, latest) == "written"
    _, published = host_reads(tmp_path / quota_context.SNAPSHOT_FILE)
    assert "model-00 (+23 retained in row; 6 outside summary)" in published["text"]
    assert "family-00-* (+23 retained in row; 6 outside summary)" in published["text"]
    assert "model-00, model-01, model-02 (+21 retained in row; 6 outside summary)" in published["details"]
    assert "family-00-*, family-01-*, family-02-* (+21 retained in row; 6 outside summary)" in published["details"]
    assert not any(name in published[view] for view in ("text", "details") for name in (models[3], prefixes[3]))


def test_detail_change_rewrites_snapshot_when_short_text_is_unchanged(tmp_path):
    def make_data(amount):
        data = payload(
            [snap("claude", "a", [constraint("five_hour", 0.2, FIVE_H, reset=3600)])],
            [profile("claude", "a")],
            harnesses=("claude",),
        )
        data["availability_scope"] = {"model": None, "requestKind": "text", "basis": "quota_block"}
        data["resources"] = [{
            "target": {"harness": "claude", "profile_id": "a"},
            "balances": facet([{"id": "credit", "label": "Credit", "amount": amount,
                                "unit": "minor", "currency": "USD", "decimal_places": 2,
                                "scope": "personal", "usage_kind": "bonus"}]),
            "spending": facet([]), "resets": facet([]), "diagnostics": facet([]),
        }]
        return data

    api = _Api(tmp_path)
    latest = plugin.LatestRead()
    first_data = make_data("1000")
    latest.put(first_data, "", NOW)
    assert plugin.publish_snapshot(api, None, first_data, "", NOW, latest) == "written"
    path = tmp_path / quota_context.SNAPSHOT_FILE
    first = json.loads(path.read_text())
    assert "10.00 USD" in first["details"] and "10.00 USD" not in first["text"]

    second_data = make_data("2500")
    latest.put(second_data, "", NOW + 120)
    assert plugin.publish_snapshot(api, None, second_data, "", NOW + 120, latest) == "written"
    second = json.loads(path.read_text())
    assert second["text"] == first["text"]
    assert second["details"] != first["details"] and "25.00 USD" in second["details"]
    assert host_reads(path)[0] == "fresh"

    latest.put(second_data, "", NOW + 150)
    assert plugin.publish_snapshot(api, None, second_data, "", NOW + 150, latest) == "unchanged"


def test_legacy_snapshot_without_details_is_replaced_on_next_sweep(tmp_path):
    answer, store, latest, data = _answer(tmp_path)
    api = _Api(tmp_path)
    path = tmp_path / quota_context.SNAPSHOT_FILE
    legacy = {"schema_version": 1, "observed_at": qs.iso(NOW), "valid_until": qs.iso(NOW + 400),
              "text": "Legacy 0.13.1 long snapshot text without details"}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    state, before = host_reads(path)
    assert state == "fresh" and "details" not in before

    assert plugin.publish_snapshot(api, store, data, "", NOW + 10, latest) == "written"
    state, after = host_reads(path)
    assert state == "fresh" and after["text"] == quota_context.brief_text(answer)
    assert isinstance(after.get("details"), str) and after["details"].startswith(quota_context.HEADER)

    # Even if an older file carried identical `text` but lacked `details`, the next sweep populates `details`.
    legacy_same_text = {"schema_version": 1, "observed_at": qs.iso(NOW + 10), "valid_until": qs.iso(NOW + 410),
                        "text": after["text"]}
    path.write_text(json.dumps(legacy_same_text), encoding="utf-8")
    assert plugin.publish_snapshot(api, store, data, "", NOW + 20, latest) == "written"
    assert "details" in json.loads(path.read_text())


def test_tool_route_and_snapshot_share_one_figure(tmp_path):
    answer, store, _latest, data = _answer(tmp_path)
    route = plugin.reserve_view(store, data, NOW, NOW, harness="claude", analytics=True)["chart"]["analytics"]["joint"]
    assert route["figure"] == answer["joint"]["claude"]["same_account_capacity"]
    assert route["hours"]["value"] == answer["joint"]["claude"]["conditional_account_hours"]
    assert (route["known"], route["accounts"]) == (answer["joint"]["claude"]["accounts_counted"],
                                                   answer["joint"]["claude"]["accounts"])
