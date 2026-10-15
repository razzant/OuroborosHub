"""Offline contract tests for the opt-in, numeric-only dossier store."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "userbot_dossier", Path(__file__).resolve().parents[2] / "skills" / "telegram-user-session" / "userbot" / "dossier.py"
)
_module = importlib.util.module_from_spec(_SPEC)
# Dataclasses inspect the importing module's namespace on some Python versions.
import sys
sys.modules[_SPEC.name] = _module
_SPEC.loader.exec_module(_module)
DossierStore = _module.DossierStore
MAX_FACTS_PER_PEER = _module.MAX_FACTS_PER_PEER
MAX_FACTS_PER_USER = _module.MAX_FACTS_PER_USER

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def make_store(tmp_path):
    clock = [NOW.timestamp()]
    return DossierStore(tmp_path / "facts.sqlite", clock=lambda: clock[0]), clock


def add(store, *, account=101, user=202, peer=-303, message=404, text="Likes books", days=90,
        source_at=NOW):
    return store.add_fact(
        account, user, source_peer_id=peer, source_message_id=message,
        source_at=source_at, fact=text, ttl_days=days,
    )


def test_explicit_fact_provenance_and_reopen(tmp_path):
    store, clock = make_store(tmp_path)
    with store:
        first = add(store)
        assert first.source_at == NOW
        assert first.source_peer_id == -303
        assert first.source_message_id == 404
        assert first.expires_at == NOW + timedelta(days=90)
        assert add(store).id == first.id  # duplicate replay, not a second fact
        assert [fact.id for fact in store.list_facts(101, 202, peer_id=-303)] == [first.id]
    with DossierStore(tmp_path / "facts.sqlite", clock=lambda: clock[0]) as reopened:
        assert reopened.list_facts(101, 202, peer_id=-303) == [first]


def test_strict_account_user_peer_isolation_and_deletion(tmp_path):
    with make_store(tmp_path)[0] as store:
        a = add(store)
        b = add(store, account=102, message=405, text="Other account")
        c = add(store, user=203, message=406, text="Other person")
        d = add(store, peer=-304, message=407, text="Other peer")
        assert store.list_facts(101, 202, peer_id=-303) == [a]
        assert store.list_facts(102, 202, peer_id=-303) == [b]
        assert store.list_facts(101, 203, peer_id=-303) == [c]
        assert store.list_facts(101, 202, peer_id=-304) == [d]
        assert store.delete_peer(101, 202, -303) == 1
        assert store.list_facts(101, 202, peer_id=-303) == []
        assert store.list_facts(101, 202, peer_id=-304) == [d]
        assert store.delete_user(101, 202) == 1
        assert store.list_facts(101, 202, peer_id=-304) == []
        assert store.list_facts(102, 202, peer_id=-303) == [b]
        assert store.list_facts(101, 203, peer_id=-303) == [c]


def test_expiry_in_read_and_physical_purge(tmp_path):
    store, clock = make_store(tmp_path)
    with store:
        add(store, days=1)
        clock[0] += 86400
        assert store.list_facts(101, 202, peer_id=-303) == []
        assert store.purge_expired() == 1
        assert store.purge_expired() == 0
        with pytest.raises(ValueError, match="expired"):
            add(store, message=405, days=1)
        assert add(store, message=406, source_at=NOW + timedelta(days=1)).fact == "Likes books"


def test_peer_quota_rejects_instead_of_silently_eviction(tmp_path):
    with make_store(tmp_path)[0] as store:
        for index in range(MAX_FACTS_PER_PEER):
            add(store, message=index + 1, text=f"fact {index}")
        with pytest.raises(ValueError, match="limit reached"):
            add(store, message=1000, text="overflow")
        assert len(store.list_facts(101, 202, peer_id=-303)) == MAX_FACTS_PER_PEER
        # Existing duplicate is idempotent even at capacity.
        assert add(store, message=1, text="fact 0").id > 0
        assert store.delete_peer(101, 202, -303) == MAX_FACTS_PER_PEER
        add(store, message=1000, text="now allowed")


def test_user_quota_spans_peers_but_not_accounts(tmp_path):
    with make_store(tmp_path)[0] as store:
        for index in range(MAX_FACTS_PER_USER):
            add(store, peer=-(index // MAX_FACTS_PER_PEER + 1),
                message=index + 1, text=f"fact {index}")
        with pytest.raises(ValueError, match="limit reached"):
            add(store, peer=-5, message=999, text="overflow")
        assert add(store, account=102, peer=-5, message=999, text="separate").fact == "separate"


@pytest.mark.parametrize("bad", [0, -1, True, "123", 1.5, 2**63])
def test_non_numeric_or_nonpositive_identity_is_rejected(tmp_path, bad):
    with make_store(tmp_path)[0] as store:
        with pytest.raises(ValueError):
            add(store, account=bad)
        with pytest.raises(ValueError):
            add(store, user=bad)
        with pytest.raises(ValueError):
            store.list_facts(101, bad, peer_id=-303)


def test_source_and_fact_limits(tmp_path):
    with make_store(tmp_path)[0] as store:
        for kw in (
            {"peer": 0}, {"peer": True}, {"message": 0}, {"message": "1"},
            {"text": " "}, {"text": "x" * 501}, {"text": "a\x00b"},
            {"days": 0}, {"days": 366}, {"days": True},
            {"source_at": datetime(2026, 10, 12)},
            {"source_at": NOW + timedelta(minutes=6)},
        ):
            with pytest.raises(ValueError):
                add(store, **kw)
        assert store.list_facts(101, 202, peer_id=-303) == []
        fact = add(store, text="  a safe fact  ")
        assert fact.fact == "a safe fact"
