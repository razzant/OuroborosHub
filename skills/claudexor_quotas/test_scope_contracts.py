"""Three scope contracts of 0.13.1 found by independent review (15 Nov 2026).

* A window whose scope field cannot be read is its own limit: it never shares a group, and so never a descriptor,
  with a readable one. Recent pace then drains each readable account and holds only the unknown one, in whichever
  order the snapshots arrive.
* A new scope on one account retires only that account's old series: another account's still-valid kept reading
  of the old key is restored, as a group and as a member of a live group alike.
* The context snapshot's limit line says the whole scope: names and family prefixes together, request kinds, the
  unspecified-model flag, unreadable fields, the history's digest-only scope, and what a short list leaves out.

Synthetic accounts through the real collector (persist_sweep) and route (reserve_view) paths; nothing here reads a
real account.
"""
import itertools

import pytest

import plugin
import quota_context as qc
import quota_history
import quota_scope
import quota_summary as qs
from test_recent_pace_binding import H, NOW, STEP, WEEK, limit, rising, snapshot, split, value_at
from test_reserve import payload, profile

FIVE_H = 18000
PLAIN = "codex|primary|604800|-"
LEGACY = "codex|cap|18000|-"
TEXT_KEY = "codex|cap|18000|-~2405c4"  # Codex's text bucket as the history has stored it since 0.13.0


def status(t, rows_of, order, roster):
    return payload([row for sid in order for row in rows_of[sid](t)],
                   [profile("codex", sid) for sid in roster], harnesses=("codex",))


def feed(folder, rows_of, order, roster, start, end=NOW):
    folder.mkdir(parents=True, exist_ok=True)
    store = quota_history.HistoryStore(folder)
    t = start
    while t <= end + 1e-6:
        plugin.persist_sweep(store, status(t, rows_of, order, roster), "", t)
        t += STEP
    return store


def bars(result):
    return {g["key"]: sorted((b["account"], b["state"]) for b in g["bars"]) for g in result["summary"]["groups"]}


# ---------------------------------------------------------------------------
# Q1: unreadable is not absent


def test_key_token_keeps_every_readable_key_and_splits_only_unreadable_scopes():
    of = lambda **f: quota_scope.scope_of(f)  # noqa: E731
    # readable keys the history already stores do not move
    assert of().key_token() == "-" and of(applies_to_models=[]).key_token() == "-"
    assert of(applies_to_request_kinds=["text"]).key_token() == "-~2405c4"
    assert of(applies_to_models=["opus"]).key_token() == qs._scope_hash(("opus",))
    values = {"applies_to_models": [None, [], ["opus"], "x", [5]],
              "applies_to_model_prefixes": [None, ["gemini-"], 3],
              "applies_to_unspecified_model": [None, True, False],
              "applies_to_request_kinds": [None, [], ["text"], ["image"], ["text", "video"], "text"]}
    seen = {}
    for combo in itertools.product(*values.values()):
        scope = of(**{k: v for k, v in zip(values, combo) if v is not None})
        seen.setdefault(scope.key_token(), set()).add(scope.unreadable)
    assert all(len(kinds) == 1 for kinds in seen.values()), "a readable and an unreadable scope share a key"
    # the cases the review named: an unreadable list is not an absent one, an unreadable kind list not a readable one
    assert of(applies_to_models="unreadable").key_token() != of().key_token()
    assert of(applies_to_request_kinds=["text", "video"]).key_token() != of(applies_to_request_kinds=["text"]).key_token()
    assert of(applies_to_request_kinds="text").key_token() != of(applies_to_request_kinds=[]).key_token()


@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")])
def test_recent_pace_drains_the_readable_account_and_holds_only_the_unreadable_one(tmp_path, order):
    rows_of = {"a": lambda t: [snapshot("a", t, [limit(rising(0.05, 0.3)(t))])],
               "b": lambda t: [snapshot("b", t, [limit(rising(0.05, 0.3)(t), applies_to_models="unreadable")])]}
    store = feed(tmp_path, rows_of, order, ("a", "b"), start=NOW - 20 * 60)
    data = status(NOW, rows_of, order, ("a", "b"))
    result = plugin.reserve_view(store, data, NOW, NOW, harness="codex", group=PLAIN, horizon="7d", analytics=True,
                                 name_accounts=True)
    window = result["chart"]["analytics"]["window"]
    sc = window["scenarios"]["recent_pace"]
    assert window["key"] == PLAIN and window["scope_applies"] is True and window["holds"] == {}
    assert split(sc) == {"accounts": 1, "at_pace": 1, "held": 0, "zero_growth": 0, "pace_held": 0, "pace_unknown": 0}
    left = 1 - rising(0.05, 0.3)(NOW)
    assert sc["line"][0][1] == pytest.approx(left, abs=1e-4)
    assert value_at(sc["line"], NOW + H / 2) == pytest.approx(left - 0.15, abs=1e-4)
    # the unreadable account is its own limit; charted, it is held at its share, applicability unknown
    odd = next(k for k in bars(result) if k.startswith("codex|primary|604800|-~"))
    assert bars(result)[odd] == [("codex:b", "current")]
    held = plugin.reserve_view(store, data, NOW, NOW, harness="codex", group=odd, horizon="7d",
                               analytics=True)["chart"]["analytics"]["window"]
    assert held["scope_applies"] is None and held["holds"] == {"scope_unknown": 1}
    assert split(held["scenarios"]["recent_pace"])["pace_held"] == 1


@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")])
def test_two_readable_accounts_keep_one_group_in_either_order(tmp_path, order):
    """Healthy neighbour: the split concerns only unreadable fields."""
    rows_of = {sid: (lambda s: lambda t: [snapshot(s, t, [limit(rising(0.05, 0.3)(t))])])(sid) for sid in "ab"}
    store = feed(tmp_path, rows_of, order, ("a", "b"), start=NOW - 20 * 60)
    result = plugin.reserve_view(store, status(NOW, rows_of, order, ("a", "b")), NOW, NOW, harness="codex",
                                 group=PLAIN, horizon="7d", analytics=True, name_accounts=True)
    assert list(bars(result)) == [PLAIN]
    assert split(result["chart"]["analytics"]["window"]["scenarios"]["recent_pace"])["at_pace"] == 2


# ---------------------------------------------------------------------------
# Q2: a new scope retires only its own account's old series


def cap(used, **extra):
    return limit(used, window=FIVE_H, reset=NOW + 4 * H, cid="cap", **extra)


def kept(tmp_path, olds):
    rows_of = {sid: (lambda s: lambda t: [snapshot(s, t, [cap(0.3)])])(sid) for sid in olds}
    return feed(tmp_path, rows_of, tuple(olds), tuple(olds), start=NOW - 40 * 60, end=NOW - 30 * 60)


@pytest.mark.parametrize("a_scope", [{}, {"applies_to_request_kinds": ["text"]}])
def test_another_accounts_kept_old_scope_is_restored_beside_a_new_scope(tmp_path, a_scope):
    store = kept(tmp_path, "ab")
    rows_of = {"a": lambda t: [snapshot("a", t, [cap(0.35, **a_scope)])], "b": lambda t: []}
    result = plugin.reserve_view(store, status(NOW, rows_of, "ab", "ab"), NOW, NOW, harness="codex",
                                 horizon="24h", name_accounts=True)
    if not a_scope:
        assert bars(result) == {LEGACY: [("codex:a", "current"), ("codex:b", "last_known")]}
    else:
        # a's old series is retired by its own new scope; b's is still its own valid evidence
        assert bars(result) == {TEXT_KEY: [("codex:a", "current")], LEGACY: [("codex:b", "last_known")]}


def test_a_new_scope_is_not_restored_as_a_member_of_a_live_old_scope(tmp_path):
    store = kept(tmp_path, "ac")
    rows_of = {"a": lambda t: [snapshot("a", t, [cap(0.35, applies_to_request_kinds=["text"])])],
               "c": lambda t: [snapshot("c", t, [cap(0.3)])]}
    result = plugin.reserve_view(store, status(NOW, rows_of, "ac", "ac"), NOW, NOW, harness="codex",
                                 horizon="24h", name_accounts=True)
    assert bars(result) == {LEGACY: [("codex:c", "current")], TEXT_KEY: [("codex:a", "current")]}


def test_an_account_with_no_reading_keeps_its_kept_reading_beside_a_live_old_scope(tmp_path):
    """Healthy neighbour of the case above: no new scope, history_only restoration as before."""
    store = kept(tmp_path, "ac")
    rows_of = {"a": lambda t: [], "c": lambda t: [snapshot("c", t, [cap(0.3)])]}
    result = plugin.reserve_view(store, status(NOW, rows_of, "ac", "ac"), NOW, NOW, harness="codex",
                                 horizon="24h", name_accounts=True)
    assert bars(result) == {LEGACY: [("codex:a", "last_known"), ("codex:c", "current")]}


# ---------------------------------------------------------------------------
# Q3: the snapshot line says the whole scope


def lines(*constraints):
    data = payload([snapshot("a", NOW, list(constraints))], [profile("codex", "a")], harnesses=("codex",))
    state = qs.compute(data, None, NOW)
    answer = qs.compact(qs.build_summary(data, state.view, NOW, state=state))
    return answer, [qc._limit_line(row) for row in answer["groups"]]


def test_snapshot_lines_keep_request_kinds_and_the_unspecified_flag_apart():
    _a, (text, image) = lines(limit(0.2, cid="cap", applies_to_request_kinds=["text"]),
                              limit(0.2, cid="cap", applies_to_request_kinds=["image"]))
    assert "text requests only" in text and "image requests only" in image and text != image
    _a, both = lines(limit(0.2, cid="cap", applies_to_models=["gpt-5"], applies_to_unspecified_model=True),
                     limit(0.2, cid="cap", applies_to_models=["gpt-5"]))
    assert len(set(both)) == 2 and sum("requests naming no model" in line for line in both) == 1


def test_snapshot_line_names_models_and_prefixes_together_and_counts_what_it_leaves_out():
    _a, [line] = lines(limit(0.2, cid="cap", applies_to_models=["gpt-5"], applies_to_model_prefixes=["gemini-"]))
    assert "models gpt-5 and model families gemini-*" in line
    answer, [line] = lines(limit(0.2, cid="cap", applies_to_model_prefixes=["a-", "b-", "c-", "d-", "e-"]))
    assert "a-*, b-*, c-* (+2 more" in line and answer["groups"][0]["model_prefixes"] == ["a-", "b-", "c-", "d-", "e-"]
    many = [f"p{i:02d}-" for i in range(30)]
    answer, [line] = lines(limit(0.2, cid="cap", applies_to_model_prefixes=many))
    row = answer["groups"][0]
    assert row["model_prefixes_omitted"] == 30 - quota_scope.MAX_SHOWN and "(+27 more" in line


def test_snapshot_line_says_an_unreadable_scope_and_a_digest_only_history_scope():
    odd, plain = sorted(lines(limit(0.2, cid="cap", applies_to_models="unreadable"),
                              limit(0.2, cid="cap"))[1], key=lambda line: "unreadable" not in line)
    assert "scope unreadable: models (applicability unknown)" in odd and "unreadable" not in plain
    restored = qc._limit_line({"limit": "cap", "duration": "5h", "key": TEXT_KEY,
                               "request_scope": "unknown; recorded only as a digest in the history",
                               "recent_pace": {}})
    assert "request scope unknown" in restored


def test_compact_marks_a_history_only_digest_scope(tmp_path):
    store = kept(tmp_path, "ab")
    rows_of = {"a": lambda t: [snapshot("a", t, [cap(0.3, applies_to_request_kinds=["text"])])]}
    # a kept text-only reading: only its digest is recorded
    feed(tmp_path, rows_of, "a", "ab", start=NOW - 20 * 60, end=NOW - 10 * 60)
    now_rows = {"a": lambda t: [], "b": lambda t: []}
    result = plugin.reserve_view(store, status(NOW, now_rows, "ab", "ab"), NOW, NOW, harness="codex",
                                 horizon="24h")
    rows = {r["key"]: r for r in qs.compact(result["summary"])["groups"]}
    assert rows[TEXT_KEY]["request_scope"].startswith("unknown") and "request_scope" not in rows[LEGACY]


# ---------------------------------------------------------------------------
# Opus's hypothesis, judged against the declared semantics (SKILL.md "Holds on part of an account")


def test_a_text_only_cooldown_holds_its_own_scope_never_a_window_of_both_kinds():
    """A both-kinds window is one shared unit image requests can still draw on: a text-only cooldown does not
    hold it (the account's default-text verdict says "Cooling down" separately), an image-only window is never
    held by it, and the text-only window of the same account is."""
    rows = [snapshot("a", NOW, [limit(0.2, cid="shared"),
                                {"id": "cool", "label": "cool", "cooldown_until": qs.iso(NOW + 600),
                                 "applies_to_request_kinds": ["text"]},
                                limit(0.2, cid="textcap", applies_to_request_kinds=["text"]),
                                limit(0.2, cid="imagecap", applies_to_request_kinds=["image"])])]
    state = qs.compute(payload(rows, [profile("codex", "a")], harnesses=("codex",)), None, NOW)
    flags = {g.meaning: g.members[0].flags for g in state.groups}
    assert flags == {"shared": (), "textcap": ("cooling",), "imagecap": ()}
