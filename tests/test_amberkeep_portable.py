"""The Portable Memory v0.7 reader: what it accepts, what it refuses, what it says it cannot.

Every package here is invented and built in the test: no real memory, no real
person, no real source. The reader only ever sees bytes, and the test checks
that what comes back is exactly what the package says — no more, no less.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "amberkeep" / "scripts" / "portable.py"


def _load():
    """Load the reader as a module, the way a skill script is loaded."""
    spec = importlib.util.spec_from_file_location("amberkeep_portable", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["amberkeep_portable"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


portable = _load()


def canonical_digest(record: dict) -> str:
    """The producer's own rule: sha256 of the record without its review."""
    payload = {key: value for key, value in record.items() if key != "review"}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def memory_document(records, memory_extra=None) -> dict:
    """The invented memory.json document, as the producer writes it."""
    memory = {"format": "portable-memory", "schema_version": 1, "records": records,
              "coverage": {"read": ["chat-1"], "unavailable": [], "excluded": []}}
    if memory_extra:
        memory.update(memory_extra)
    return memory


def memory_digest(memory: dict) -> str:
    """The producer's projection rule for memory_sha256: the complete memory,
    with the excluded records left out, the rest ordered by id, and metadata
    an object without its last_import."""
    projected = dict(memory)
    kept = [r for r in projected.get("records", [])
            if r["review"]["decision"] != "exclude"]
    projected["records"] = sorted(kept, key=lambda r: r["id"])
    metadata = projected.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata.pop("last_import", None)
    projected["metadata"] = metadata
    blob = json.dumps(projected, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def genuine_profile(records, memory, **extra) -> dict:
    """The producer's own review: one row per non-excluded record, with the
    digest of that record's content, and the digest of the projected memory."""
    profile = {
        "format": "portable-common-profile-review", "schema_version": 1,
        "readiness": "ready",
        "record_checks": [{"id": r["id"], "content_sha256": canonical_digest(r),
                           "quality_ready": True}
                          for r in records if r["review"]["decision"] != "exclude"],
        "memory_sha256": memory_digest(memory),
    }
    profile.update(extra)
    return profile


def record(record_id: str, *, value, provenance="source", decision="active",
           currency="current", digest=True, **extra) -> dict:
    """One invented record, digested the way the producer digests it."""
    review = {"decision": decision}
    body = {
        "id": record_id,
        "kind": extra.pop("kind", "fact"),
        "title": extra.pop("title", f"Record {record_id}"),
        "scope": extra.pop("scope", "work"),
        "value": value,
        "provenance": provenance,
        "currency": currency,
        "sources": extra.pop("sources", []),
        "links": extra.pop("links", []),
    }
    body.update(extra)
    body["review"] = review
    if digest:
        review["content_sha256"] = canonical_digest(body)
    return body


def package(records, *, files=None, record_count=None, manifest_extra=None,
            memory_extra=None, manifest_override=None, profile=None) -> bytes:
    """A complete invented package: manifest, memory and profile review."""
    members: dict[str, bytes] = {}
    if files is None:
        memory = memory_document(records, memory_extra)
        members["memory.json"] = json.dumps(memory, ensure_ascii=False).encode("utf-8")
        review = profile if profile is not None else genuine_profile(records, memory)
        members["profile-review.json"] = json.dumps(review, ensure_ascii=False).encode("utf-8")
    else:
        members.update(files)
        if record_count is None:
            memory_in_files = json.loads(members["memory.json"].decode("utf-8"))
            record_count = len(memory_in_files.get("records", []))

    manifest = {
        "format": "portable-memory-package",
        "schema_version": 1,
        "files": {name: {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
                  for name, blob in members.items()},
        "record_count": len(records) if record_count is None else record_count,
        "coverage": {"read": ["chat-1"], "unavailable": [], "excluded": []},
    }
    if manifest_extra:
        manifest.update(manifest_extra)
    if manifest_override:
        manifest.update(manifest_override)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for name, blob in members.items():
            archive.writestr(name, blob)
    return buffer.getvalue()


def _rebuild(data: bytes, mutate) -> bytes:
    """The same package with its manifest changed."""
    source = zipfile.ZipFile(io.BytesIO(data))
    members = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(members["manifest.json"].decode("utf-8"))
    mutate(manifest)
    members["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, blob in members.items():
            archive.writestr(name, blob)
    return buffer.getvalue()


def gap_about(gaps, fragment: str) -> str:
    """The one gap line that speaks of this thing."""
    matching = [gap for gap in gaps if fragment in gap]
    assert matching, f"no gap mentions {fragment!r}: {gaps}"
    return matching[0]


# ------------------------------------------------------------------ accepted package

def test_a_valid_package_with_two_provenances_yields_exactly_those_rows():
    """One user_statement record and one model_inference record, read apart."""
    spoken = record("r-user", value="Антон предпочитает краткие отчёты.",
                    provenance="user_statement",
                    sources=[{"id": "s1", "locator": "chat-42",
                              "observed_at": "2026-11-20T09:00:00Z"}])
    inferred = record("r-model", value="Вероятно, лучше спросить раньше.",
                      provenance="model_inference")

    rows, gaps = portable.read_portable(package([spoken, inferred]), "amberkeep-0.7.zip")

    assert [row["locator"] for row in rows] == ["memory.json#r-user", "memory.json#r-model"]
    for row in rows:
        assert set(("locator", "speaker", "text", "date", "conversation")) <= set(row)

    # A retelling of what the person said is the document's speech, not the person's.
    assert rows[0]["speaker"] == "document"
    assert rows[0]["text"] == "Антон предпочитает краткие отчёты."
    assert rows[0]["date"] == "2026-11-20T09:00:00Z"
    assert rows[0]["conversation"] == "amberkeep-0.7.zip"
    assert rows[0]["origin"]["provenance"] == "user_statement"
    assert "text_kind" not in rows[0]["origin"]
    assert "dropped_fields" not in rows[0]["origin"]
    assert rows[0]["sources"] == [{"id": "s1", "locator": "chat-42",
                                   "observed_at": "2026-11-20T09:00:00Z"}]

    # A model inference is the assistant's speech.
    assert rows[1]["speaker"] == "assistant"
    assert rows[1]["text"] == "Вероятно, лучше спросить раньше."
    assert rows[1]["date"] is None
    assert "r-model" in gap_about(gaps, "carry no date")

    # The producer's readiness is carried as their status, not as our agreement.
    assert "their status, not this reader's agreement" in gap_about(gaps, "readiness")


def test_a_structured_value_is_kept_as_it_stands_with_currency_and_links():
    """A retelling or a source description must not be flattened into a claim."""
    body = record(
        "r-obj",
        value={"summary": "Результат обсуждения", "quotes": ["первая фраза", "вторая фраза"],
               "attributed_to": "Антон", "context": {"thread": "chat-7"}},
        provenance="user_statement",
        currency="historical",
        links=[{"target": "chat-7", "relation": "derived_from", "basis": "thread summary"}],
        sources=[{"id": "s2", "locator": "chat-7", "observed_at": "2026-10-31T18:00:00Z"}],
    )

    rows, _ = portable.read_portable(package([body]), "p.zip")

    row = rows[0]
    assert row["value"] == body["value"], "the object is carried as it stands"
    assert json.loads(row["text"]) == body["value"]
    assert row["origin"]["text_kind"] == "projection"
    assert row["currency"] == "historical"
    assert row["links"] == [{"target": "chat-7", "relation": "derived_from",
                              "basis": "thread summary"}]
    assert row["sources"][0]["locator"] == "chat-7"
    assert row["speaker"] == "document", "a structured retelling is still not the person's voice"


def test_a_historical_record_keeps_its_verdict():
    """An old decision travels as history, not as a fresh active one."""
    old = record("r-old", value="Раньше мы выбрали первый вариант.", decision="historical")
    fresh = record("r-new", value="Теперь мы выбрали второй вариант.")

    rows, _ = portable.read_portable(package([old, fresh]), "p.zip")

    assert [row["origin"]["decision"] for row in rows] == ["historical", "active"]
    assert rows[0]["text"] == "Раньше мы выбрали первый вариант."


def test_excluded_records_are_not_admitted_and_nothing_of_them_is_returned():
    """An excluded record contributes neither a row nor its id."""
    kept = record("r-keep", value="Оставленное суждение")
    dropped = record("r-drop", value="Ранее исключённая информация", decision="exclude")

    rows, gaps = portable.read_portable(package([kept, dropped]), "p.zip")

    assert [row["locator"] for row in rows] == ["memory.json#r-keep"]
    # Nothing of the excluded record is anywhere in the answer, gaps included.
    assert "r-drop" not in json.dumps(rows, ensure_ascii=False)
    assert "r-drop" not in json.dumps(gaps, ensure_ascii=False)
    assert "Ранее исключённая информация" not in json.dumps(rows, ensure_ascii=False)
    assert "Ранее исключённая информация" not in json.dumps(gaps, ensure_ascii=False)
    assert "1 excluded record" in gap_about(gaps, "excluded record")


def test_pending_records_are_reported_but_not_imported():
    """A record that is not settled memory is named as a gap, not imported."""
    pending = record("r-pending", value="Ещё не проверено", decision="pending")
    settled = record("r-ok", value="Проверенное суждение")

    rows, gaps = portable.read_portable(package([pending, settled]), "p.zip")

    assert [row["locator"] for row in rows] == ["memory.json#r-ok"]
    assert "r-pending" in gap_about(gaps, "pending record")


def test_optional_files_are_verified_by_digest_only_and_are_not_unpacked():
    """PROFILE.md and friends are checked, not read, not executed, not extracted."""
    memory = memory_document([record("r1", value="Факт")],
                             {"coverage": {"read": [], "unavailable": [], "excluded": []}})
    files = {
        "memory.json": json.dumps(memory, ensure_ascii=False).encode("utf-8"),
        "profile-review.json": json.dumps(genuine_profile(memory["records"], memory),
                                          ensure_ascii=False).encode("utf-8"),
        "PROFILE.md": b"# Profile\n\nInvented profile text.\n",
        "wiki/notes.md": b"Invented wiki text.\n",
        "assets/diagram.png": b"\x89PNG\r\n\x1a\ninvented",
    }

    rows, gaps = portable.read_portable(package([], files=files), "p.zip")

    assert len(rows) == 1
    line = gap_about(gaps, "optional file")
    assert "PROFILE.md" in line and "wiki/notes.md" in line and "assets/diagram.png" in line
    assert "Invented profile text" not in json.dumps(rows, ensure_ascii=False)


# ------------------------------------------------------------------ refused packages

def test_a_manifest_whose_sizes_disagree_with_the_archive_is_refused():
    """The sum in the inventory must match the bytes actually in the package."""
    data = package([record("r1", value="Факт")])
    broken = _rebuild(data, mutate=lambda manifest: manifest["files"]["memory.json"].update(
        {"bytes": manifest["files"]["memory.json"]["bytes"] + 1}))

    with pytest.raises(ValueError) as answer:
        portable.read_portable(broken, "p.zip")
    assert "bytes" in str(answer.value)


def test_a_manifest_whose_digest_disagrees_with_the_archive_is_refused():
    """The digest in the inventory must match the bytes actually in the package."""
    data = package([record("r1", value="Факт")])
    broken = _rebuild(data, mutate=lambda manifest: manifest["files"]["memory.json"].update(
        {"sha256": "0" * 64}))

    with pytest.raises(ValueError) as answer:
        portable.read_portable(broken, "p.zip")
    assert "does not match the sha256 in the manifest" in str(answer.value)


def test_bytes_changed_in_the_archive_without_updating_the_manifest_are_refused():
    """The ordinary integrity case: a record edited, the manifest left as it was."""
    data = package([record("r1", value="Оригинальное суждение")])
    source = zipfile.ZipFile(io.BytesIO(data))
    members = {name: source.read(name) for name in source.namelist()}
    memory = json.loads(members["memory.json"].decode("utf-8"))
    # One letter lowercased: the same number of bytes, so only the digest can
    # tell the two apart.
    memory["records"][0]["value"] = memory["records"][0]["value"].replace("О", "о", 1)
    members["memory.json"] = json.dumps(memory, ensure_ascii=False).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, blob in members.items():
            archive.writestr(name, blob)

    with pytest.raises(ValueError) as answer:
        portable.read_portable(buffer.getvalue(), "p.zip")
    assert "does not match the sha256 in the manifest" in str(answer.value)


def test_a_record_whose_content_contradicts_its_digest_is_refused():
    """A tampered record must not be read as if it were the reviewed one."""
    tampered = dict(record("r1", value="Оригинальное суждение"), value="Подменённое суждение")
    data = package([tampered])

    with pytest.raises(ValueError) as answer:
        portable.read_portable(data, "p.zip")
    assert "content_sha256" in str(answer.value)


def test_a_record_count_that_disagrees_with_the_records_is_refused():
    """The manifest's count is exactly the number of records there."""
    data = package([record("r1", value="Факт")], record_count=7)

    with pytest.raises(ValueError) as answer:
        portable.read_portable(data, "p.zip")
    assert "counts 7 records" in str(answer.value)


def test_an_inventory_that_does_not_match_the_archive_is_refused():
    """A file in the archive that the manifest does not list is a broken package."""
    data = package([record("r1", value="Факт")])
    broken = _rebuild(data, mutate=lambda manifest: manifest["files"].pop("profile-review.json"))

    with pytest.raises(ValueError) as answer:
        portable.read_portable(broken, "p.zip")
    assert "inventory" in str(answer.value)


def test_an_unknown_package_format_is_refused():
    data = package([record("r1", value="Факт")])
    broken = _rebuild(data, mutate=lambda manifest: manifest.update(
        {"format": "portable-memory-package-v2"}))

    with pytest.raises(ValueError) as answer:
        portable.read_portable(broken, "p.zip")
    assert "unknown package format" in str(answer.value)


def test_an_unknown_schema_version_is_refused():
    data = package([record("r1", value="Факт")])
    broken = _rebuild(data, mutate=lambda manifest: manifest.update({"schema_version": 2}))

    with pytest.raises(ValueError) as answer:
        portable.read_portable(broken, "p.zip")
    assert "schema_version" in str(answer.value)


def test_bytes_that_are_not_an_archive_are_refused():
    with pytest.raises(ValueError) as answer:
        portable.read_portable(b"not a zip at all", "p.zip")
    assert "ZIP" in str(answer.value)


def test_a_package_without_a_manifest_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("memory.json", "{}")
    with pytest.raises(ValueError) as answer:
        portable.read_portable(buffer.getvalue(), "p.zip")
    assert "manifest.json" in str(answer.value)


def test_a_package_without_the_profile_review_is_refused():
    """Both required members must be there; one missing is not a package."""
    files = {"memory.json": json.dumps(
        {"format": "portable-memory", "schema_version": 1, "records": [],
         "coverage": {"read": [], "unavailable": [], "excluded": []}},
        ensure_ascii=False).encode("utf-8")}
    data = package([], files=files)

    with pytest.raises(ValueError) as answer:
        portable.read_portable(data, "p.zip")
    assert "profile-review.json" in str(answer.value)


def test_a_record_without_a_provenance_is_refused_not_crashed():
    """A damaged record must raise ValueError, so the caller can report it."""
    broken = record("r1", value="Факт")
    del broken["provenance"]
    broken["review"]["content_sha256"] = canonical_digest(broken)

    with pytest.raises(ValueError) as answer:
        portable.read_portable(package([broken]), "p.zip")
    assert "provenance" in str(answer.value)


def test_a_record_with_an_unknown_verdict_is_refused():
    """A verdict outside the wire contract is not a package this reader supports."""
    strange = record("r1", value="Факт", decision="under_revision")

    with pytest.raises(ValueError) as answer:
        portable.read_portable(package([strange]), "p.zip")
    assert "review decision" in str(answer.value)


def test_a_manifest_larger_than_the_limit_is_refused_before_it_is_read():
    """Even the manifest is bounded: a huge package is refused, not half-read."""
    limit = portable.MAX_FILE_BYTES
    try:
        portable.MAX_FILE_BYTES = 10
        data = package([record("r1", value="Факт")])
        with pytest.raises(ValueError) as answer:
            portable.read_portable(data, "p.zip")
        assert "larger than this reader accepts" in str(answer.value)
    finally:
        portable.MAX_FILE_BYTES = limit


# ------------------------------------------------------------------ what it says

def test_known_credential_fields_are_dropped_but_free_text_is_not_filtered():
    """A typed credential field goes; a sentence that mentions one stays."""
    with_secret = record("r-secret", value={"note": "Обычная заметка", "api_key": "sk-invented",
                                             "refresh_token": "rt-invented"})
    talking = record("r-text", value="В заметке упомянут пароль от старого сервиса.")

    rows, gaps = portable.read_portable(package([with_secret, talking]), "p.zip")

    first = rows[0]
    assert "api_key" not in first["value"]
    assert "refresh_token" not in first["value"]
    assert "sk-invented" not in json.dumps(rows)
    assert first["value"]["note"] == "Обычная заметка"
    assert first["origin"]["text_kind"] == "projection"
    assert first["origin"]["dropped_fields"] == ["api_key", "refresh_token"]
    assert "api_key" in gap_about(gaps, "credential fields")

    assert rows[1]["text"] == "В заметке упомянут пароль от старого сервиса."
    assert rows[1]["speaker"] == "document"
    assert "dropped_fields" not in rows[1]["origin"]
    assert "text_kind" not in rows[1]["origin"]


def test_a_credential_field_nested_deep_in_a_value_is_dropped_too():
    """A credential hidden in a nested structure does not travel."""
    nested = record("r-nested", value={"outer": {"inner": [{"api_key": "sk-invented",
                                                            "keep": "Обычный текст"}]}})

    rows, gaps = portable.read_portable(package([nested]), "p.zip")

    assert "sk-invented" not in json.dumps(rows)
    assert rows[0]["value"]["outer"]["inner"][0]["keep"] == "Обычный текст"
    assert rows[0]["origin"]["dropped_fields"] == ["api_key"]
    assert rows[0]["origin"]["text_kind"] == "projection"
    assert "api_key" in gap_about(gaps, "credential fields")


def test_the_package_coverage_is_carried_into_the_gaps_unchanged():
    """What could not be read stays visible after the import."""
    memory = memory_document([record("r1", value="Факт")],
                             {"coverage": {"read": ["chat-1"], "unavailable": ["chat-2"],
                                           "excluded": ["chat-3"]}})
    files = {"memory.json": json.dumps(memory, ensure_ascii=False).encode("utf-8"),
             "profile-review.json": json.dumps(genuine_profile(memory["records"], memory),
                                               ensure_ascii=False).encode("utf-8")}

    rows, gaps = portable.read_portable(package([], files=files), "p.zip")

    assert len(rows) == 1
    assert "chat-1" in gap_about(gaps, "as read")
    assert "chat-2" in gap_about(gaps, "could not read")
    assert "chat-3" in gap_about(gaps, "left")


def test_a_package_without_a_coverage_block_says_so_instead_of_claiming_full_coverage():
    """An absent coverage block is a gap, not an assumption that all was read."""
    memory = {"format": "portable-memory", "schema_version": 1,
              "records": [record("r1", value="Факт")]}
    files = {"memory.json": json.dumps(memory, ensure_ascii=False).encode("utf-8"),
             "profile-review.json": json.dumps(genuine_profile(memory["records"], memory),
                                               ensure_ascii=False).encode("utf-8")}

    rows, gaps = portable.read_portable(package([], files=files), "p.zip")

    assert len(rows) == 1
    assert gap_about(gaps, "no coverage block")


def test_a_source_locator_is_never_resolved_and_no_file_is_touched(tmp_path, monkeypatch):
    """The reader works on the bytes it was given and reaches nothing else."""
    monkeypatch.chdir(tmp_path)
    body = record("r1", value="Факт", sources=[{"id": "s1", "locator": "/etc/passwd",
                                                "observed_at": "2026-11-01"}])
    rows, _ = portable.read_portable(package([body]), "p.zip")

    assert rows[0]["sources"][0]["locator"] == "/etc/passwd", "the locator is carried, not read"
    assert list(tmp_path.iterdir()) == [], "nothing was written and nothing was unpacked"


def test_a_source_without_an_observed_date_is_read_without_one():
    """observed_at may be null; the row then carries no date."""
    body = record("r1", value="Факт", sources=[{"id": "s1", "locator": "chat-1",
                                                "observed_at": None}])

    rows, gaps = portable.read_portable(package([body]), "p.zip")

    assert rows[0]["date"] is None
    assert rows[0]["sources"][0]["observed_at"] is None
    assert "carry no date" in gap_about(gaps, "carry no date")


# -------------------------------------------------- the profile review's own consistency

def test_a_genuine_review_whose_memory_sha256_disagrees_is_refused():
    """The producer's digest over its projection is checked, not carried on trust."""
    body = record("r1", value="Факт")
    lying = genuine_profile([body], memory_document([body]))
    lying["memory_sha256"] = "0" * 64

    with pytest.raises(ValueError) as answer:
        portable.read_portable(package([body], profile=lying), "p.zip")
    assert "memory_sha256" in str(answer.value)
    assert "does not match" in str(answer.value)


def test_a_genuine_review_whose_row_digest_disagrees_is_refused():
    """A row's digest is the record's content; a swap is a damaged package."""
    body = record("r1", value="Факт")
    lying = genuine_profile([body], memory_document([body]))
    lying["record_checks"][0]["content_sha256"] = "1" * 64

    with pytest.raises(ValueError) as answer:
        portable.read_portable(package([body], profile=lying), "p.zip")
    assert "record_checks" in str(answer.value)


def test_a_genuine_review_that_omits_a_records_row_is_refused():
    """Every non-excluded record is named exactly once, and none may be left out."""
    kept = record("r-kept", value="Проверенное суждение")
    skipped = record("r-skipped", value="Другое суждение")
    lying = genuine_profile([kept], memory_document([kept, skipped]))

    with pytest.raises(ValueError) as answer:
        portable.read_portable(package([kept, skipped], profile=lying), "p.zip")
    assert "does not name exactly the non-excluded records" in str(answer.value)
    assert "r-skipped" in str(answer.value)


def test_a_genuine_review_that_names_an_unknown_record_is_refused():
    """A row for a record the memory does not hold is a damaged package."""
    body = record("r1", value="Факт")
    lying = genuine_profile([body], memory_document([body]))
    lying["record_checks"].append({"id": "r-ghost", "content_sha256": "2" * 64})

    with pytest.raises(ValueError) as answer:
        portable.read_portable(package([body], profile=lying), "p.zip")
    assert "r-ghost" in str(answer.value)


def test_a_genuine_review_without_its_checks_or_its_digest_is_refused():
    """A review of the real format that carries neither part is not a review."""
    body = record("r1", value="Факт")
    full = genuine_profile([body], memory_document([body]))

    no_checks = {key: value for key, value in full.items() if key != "record_checks"}
    with pytest.raises(ValueError) as answer:
        portable.read_portable(package([body], profile=no_checks), "p.zip")
    assert "record_checks" in str(answer.value)

    no_digest = {key: value for key, value in full.items() if key != "memory_sha256"}
    with pytest.raises(ValueError) as answer:
        portable.read_portable(package([body], profile=no_digest), "p.zip")
    assert "memory_sha256" in str(answer.value)


def test_a_genuine_review_whose_checks_are_not_a_list_is_refused():
    """The wire shape is one row per record; a map of verdicts is not it."""
    body = record("r1", value="Факт")
    lying = genuine_profile([body], memory_document([body]))
    lying["record_checks"] = {"r1": {"ok": True}}

    with pytest.raises(ValueError) as answer:
        portable.read_portable(package([body], profile=lying), "p.zip")
    assert "record_checks" in str(answer.value)


def test_a_genuine_review_checks_every_non_excluded_record_and_no_excluded_one():
    """Rows cover the pending record and skip the excluded one; the digest
    covers the non-excluded records in id order, not file order."""
    late = record("b-second", value="Второй в файле, первый по алфавиту.")
    waiting = record("a-first", value="Ожидает решения.", decision="pending")
    gone = record("c-gone", value="Исключено ревью.", decision="exclude")

    rows, gaps = portable.read_portable(package([late, waiting, gone]), "p.zip")

    assert [row["locator"] for row in rows] == ["memory.json#b-second"]
    assert "a-first" in gap_about(gaps, "pending record")
    assert "1 excluded record" in gap_about(gaps, "excluded record")


def test_last_import_is_left_out_of_the_projected_memory_digest():
    """metadata.last_import marks the import, not the content; the digest omits it."""
    body = record("r1", value="Факт")
    data = package([body], memory_extra={"metadata": {
        "last_import": "2026-11-21T00:00:00Z", "kept": "остальное входит"}})

    rows, _ = portable.read_portable(data, "p.zip")
    assert len(rows) == 1


def test_the_producers_unresolved_items_are_carried_without_their_content():
    """Conflicts and aliases stay unresolved; their text is not republished."""
    body = record("r1", value="Факт")
    profile = genuine_profile([body], memory_document([body]),
                              semantic={"conflicts": [{"id": "c1", "text": "Частная история"}]},
                              metadata={"aliases": [{"from": "a", "to": "b", "note": "Ещё частное"}]})

    rows, gaps = portable.read_portable(package([body], profile=profile), "p.zip")

    assert len(rows) == 1
    line = gap_about(gaps, "unresolved")
    assert "semantic.conflicts" in line and "metadata.aliases" in line
    assert "Частная история" not in json.dumps(gaps, ensure_ascii=False)
    assert "Ещё частное" not in json.dumps(gaps, ensure_ascii=False)


def test_an_unknown_or_lightweight_profile_review_is_refused():
    """An unverified or substituted profile-review must not pass as verified v0.7."""
    body = record("r1", value="Факт")
    for bad_profile in (
        {"format": "profile-review", "schema_version": 1, "selected": ["r1"]},
        {"format": "unknown-review", "schema_version": 1},
        {},
    ):
        with pytest.raises(ValueError) as answer:
            portable.read_portable(package([body], profile=bad_profile), "p.zip")
        assert "unknown profile review format" in str(answer.value)


def test_disagreeing_manifest_and_memory_coverage_shows_both_sides():
    """When manifest and memory disagree on unavailable sources, both sides appear in gaps."""
    memory = memory_document(
        [record("r1", value="Факт")],
        {"coverage": {"read": ["chat-1"], "unavailable": ["memory-chat"], "excluded": []}},
    )
    files = {
        "memory.json": json.dumps(memory, ensure_ascii=False).encode("utf-8"),
        "profile-review.json": json.dumps(genuine_profile(memory["records"], memory),
                                          ensure_ascii=False).encode("utf-8"),
    }
    data = package(
        [],
        files=files,
        manifest_override={"coverage": {"read": ["chat-1"], "unavailable": ["manifest-chat"], "excluded": []}},
    )

    rows, gaps = portable.read_portable(data, "p.zip")

    assert len(rows) == 1
    mismatch = gap_about(gaps, "different unavailable sources")
    assert "manifest-chat" in mismatch
    assert "memory-chat" in mismatch

    # An explicitly empty list in memory.coverage stays distinguished from an absent coverage block.
    memory_empty = memory_document(
        [record("r1", value="Факт")],
        {"coverage": {"read": [], "unavailable": [], "excluded": []}},
    )
    files_empty = {
        "memory.json": json.dumps(memory_empty, ensure_ascii=False).encode("utf-8"),
        "profile-review.json": json.dumps(genuine_profile(memory_empty["records"], memory_empty),
                                          ensure_ascii=False).encode("utf-8"),
    }
    data_empty = package(
        [],
        files=files_empty,
        manifest_override={"coverage": {"read": [], "unavailable": ["manifest-gap"], "excluded": []}},
    )
    _, gaps_empty = portable.read_portable(data_empty, "p.zip")
    mismatch_empty = gap_about(gaps_empty, "different unavailable sources")
    assert "manifest-gap" in mismatch_empty
    assert "empty" in mismatch_empty

    # When memory.json omits coverage altogether, report 'not reported' rather than 'empty'/'none'.
    memory_no_cov = {"format": "portable-memory", "schema_version": 1, "records": [record("r1", value="Факт")]}
    files_no_cov = {
        "memory.json": json.dumps(memory_no_cov, ensure_ascii=False).encode("utf-8"),
        "profile-review.json": json.dumps(genuine_profile(memory_no_cov["records"], memory_no_cov),
                                          ensure_ascii=False).encode("utf-8"),
    }
    data_no_cov = package(
        [],
        files=files_no_cov,
        manifest_override={"coverage": {"read": [], "unavailable": ["manifest-only-gap"], "excluded": []}},
    )
    _, gaps_no_cov = portable.read_portable(data_no_cov, "p.zip")
    mismatch_no_cov = gap_about(gaps_no_cov, "different unavailable sources")
    assert "manifest-only-gap" in mismatch_no_cov
    assert "not reported" in mismatch_no_cov


def test_encrypted_alternative_compression_and_streaming_limit_are_refused(monkeypatch):
    """Encrypted entries, alternative compression, and over-limit streams fail with ValueError."""
    def _mock_archive(*, flag_bits: int = 0, compress_type: int = zipfile.ZIP_STORED,
                      file_size: int = 2, stream_bytes: bytes | None = None):
        entry = SimpleNamespace(
            filename="manifest.json",
            flag_bits=flag_bits,
            compress_type=compress_type,
            file_size=file_size,
            is_dir=lambda: False,
        )
        open_mock = MagicMock(side_effect=lambda _info: io.BytesIO(stream_bytes or b"{}"))

        class FakeArchive:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def infolist(self):
                return [entry]

            def getinfo(self, name):
                if name == "manifest.json":
                    return entry
                raise KeyError(name)

            open = open_mock

        return FakeArchive(), open_mock

    enc_archive, enc_open = _mock_archive(flag_bits=0x1)
    monkeypatch.setattr(portable.zipfile, "ZipFile", lambda *_a, **_kw: enc_archive)
    with pytest.raises(ValueError) as enc:
        portable.read_portable(b"PK\x03\x04stub", "enc.zip")
    assert "encrypted" in str(enc.value)
    enc_open.assert_not_called()

    for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA, 93):
        alt_archive, alt_open = _mock_archive(compress_type=method)
        monkeypatch.setattr(portable.zipfile, "ZipFile", lambda *_a, **_kw: alt_archive)
        with pytest.raises(ValueError) as comp:
            portable.read_portable(b"PK\x03\x04stub", "alt.zip")
        assert "unsupported compression method" in str(comp.value)
        alt_open.assert_not_called()

    limit_archive, _ = _mock_archive(
        file_size=5,
        stream_bytes=b'{"format":"portable-memory-package","padding":"' + b"x" * 60 + b'"}',
    )
    monkeypatch.setattr(portable.zipfile, "ZipFile", lambda *_a, **_kw: limit_archive)
    monkeypatch.setattr(portable, "MAX_FILE_BYTES", 50)
    with pytest.raises(ValueError) as bounded:
        portable.read_portable(b"PK\x03\x04stub", "limit.zip")
    assert "larger than this reader accepts" in str(bounded.value)


def test_bool_and_float_schema_versions_are_refused():
    """schema_version must be integer 1, never True or 1.0."""
    body = record("r1", value="Факт")
    for bad_version in (True, 1.0):
        with pytest.raises(ValueError) as man:
            portable.read_portable(package([body], manifest_override={"schema_version": bad_version}), "p.zip")
        assert "schema_version" in str(man.value)

        mem_doc = memory_document([body], {"schema_version": bad_version})
        prof = genuine_profile([body], mem_doc)
        files = {
            "memory.json": json.dumps(mem_doc, ensure_ascii=False).encode("utf-8"),
            "profile-review.json": json.dumps(prof, ensure_ascii=False).encode("utf-8"),
        }
        with pytest.raises(ValueError) as mem:
            portable.read_portable(package([], files=files), "p.zip")
        assert "schema_version" in str(mem.value)

        bad_prof = genuine_profile([body], memory_document([body]), schema_version=bad_version)
        with pytest.raises(ValueError) as prf:
            portable.read_portable(package([body], profile=bad_prof), "p.zip")
        assert "schema_version" in str(prf.value)


# ------------------------------------------------- withheld names and scope

def test_withheld_records_are_named_for_resolution_only():
    """Excluded and pending records give the caller their names, nothing else."""
    kept = record("kept", value="Грядка у забора.", sources=[{"id": "s1", "locator": "chat-1"}])
    plan = record("plan", value="Тайный план осени.", decision="exclude")
    draft = record("draft", value="Черновик.", decision="pending")
    data = package([kept, plan, draft])
    rows, gaps, coverage, withheld = portable.read_portable_resolving(data, "p.zip")
    assert [row["locator"] for row in rows] == ["memory.json#kept"]
    assert withheld == frozenset({"plan", "memory.json#plan", "draft", "memory.json#draft"})
    # The public readers are unchanged and never return those names.
    assert portable.read_portable_with_coverage(data, "p.zip") == (rows, gaps, coverage)
    assert portable.read_portable(data, "p.zip") == (rows, gaps)
    assert "Тайный план" not in json.dumps([rows, gaps], ensure_ascii=False)


def test_a_stated_empty_scope_stays_empty_and_a_missing_one_stays_missing():
    stated = record("stated", value="Пустая область.", scope="")
    missing = record("missing", value="Без области.")
    missing.pop("scope")
    missing["review"]["content_sha256"] = canonical_digest(missing)
    rows, _ = portable.read_portable(package([stated, missing]), "p.zip")
    by_id = {row["locator"]: row for row in rows}
    assert by_id["memory.json#stated"]["origin"]["scope"] == ""
    assert "scope" not in by_id["memory.json#missing"]["origin"]


def test_empty_structures_and_null_stay_structured_values():
    rows, _ = portable.read_portable(package([
        record("obj", value={}), record("arr", value=[]), record("nul", value=None),
        record("txt", value="null")]), "p.zip")
    by_id = {row["locator"].split("#", 1)[1]: row for row in rows}
    for key, value, text in (("obj", {}, "{}"), ("arr", [], "[]"), ("nul", None, "null")):
        assert by_id[key]["value"] == value and by_id[key]["text"] == text
        assert by_id[key]["origin"]["text_kind"] == "projection"
    # The same characters as plain text are verbatim, not a projection.
    assert by_id["txt"]["text"] == "null" and "text_kind" not in by_id["txt"]["origin"]
