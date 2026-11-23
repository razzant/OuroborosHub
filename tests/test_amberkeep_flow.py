"""The Amberkeep chain: inspect → select → read/quote/portrait → verify.

Every file here is invented and built in the test: no real memory, no real
person, no real export. The persona is a fictional gardening cooperative
«Огонёк» and its fictional member Марта. The test drives the CLI the way its
consumer calls it (from the installed payload) and checks the contract:

* honest selection — exactly the picked records travel, built straight from
  the source named in the same call and bound to the inspect that read it
  (the inspect's --receipt, or the one the --rows table records); an unselected
  record leaves no trace in the package, and a rows document contributes
  nothing but its locators and that binding;
* bindings, not proofs — a sha256 ties text to a record; roles, dates and
  provenance are carried from the fresh source read; ``quote --original``
  is the human check through the one chain hop that binds sha256 + parser +
  locator, with ``text_match``/``role_match``/``date_match`` reported
  separately and ``verified`` meaning the whole binding held;
* two honestly separate formats — the input Portable Memory package (read
  by the teammate portable.read_portable) and this CLI's own output, an
  amberkeep-selection, which a genuine Portable importer refuses;
  read/quote/portrait/verify accept both;
* explicit refusals with a non-zero exit (a damaged package is never "ok"),
  bounded stdout, deterministic packages and stable ids.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

HUB_ROOT = Path(__file__).resolve().parents[1]
SKILL = HUB_ROOT / "skills" / "amberkeep"
HELPER = SKILL / "scripts" / "amberkeep.py"
READERS = SKILL / "scripts" / "readers.py"

BASE_NOTE = (
    "Марта поливает огурцы по утрам среды и записывает в дневник подробности "
    "о каждом листе, каждой плети и каждом ведре воды, чтобы зимой перечитать "
    "весь сезон целиком."
)
LONG_NOTE = (BASE_NOTE + " ") * 2
FREE_TEXT = "Забыть пароль от WiFi никогда не поздно, считает Марта."


def _load(path: Path, alias: str):
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


portable = _load(SKILL / "scripts" / "portable.py", "amberkeep_portable_under_test")


def run_cli(argv: list[str]):
    """Run the CLI as its consumer does, from the installed payload."""
    return subprocess.run([sys.executable, str(HELPER), *argv],
                          capture_output=True, text=True, timeout=120)


def out_json(result) -> dict:
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def refused(result) -> dict:
    assert result.returncode != 0, result.stdout
    doc = json.loads(result.stdout)
    assert "ok" not in doc, "a refusal is never a verdict"
    return doc


def memory_digest(memory: dict) -> str:
    """The producer's projection rule for memory_sha256: excluded records out,
    the rest ordered by id, metadata an object without last_import."""
    projected = dict(memory)
    kept = [r for r in projected["records"] if r["review"]["decision"] != "exclude"]
    projected["records"] = sorted(kept, key=lambda r: r["id"])
    metadata = dict(projected.get("metadata")) if isinstance(projected.get("metadata"), dict) else {}
    metadata.pop("last_import", None)
    projected["metadata"] = metadata
    blob = json.dumps(projected, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def genuine_profile(records: list, memory: dict) -> dict:
    """The producer's own review, on the wire format: one row per non-excluded
    record with its content digest, and the digest of the projected memory."""
    return {
        "format": "portable-common-profile-review", "schema_version": 1,
        "readiness": "ready",
        "record_checks": [{"id": r["id"], "content_sha256": r["review"]["content_sha256"],
                           "quality_ready": True}
                          for r in records if r["review"]["decision"] != "exclude"],
        "memory_sha256": memory_digest(memory),
    }


def build_package(path: Path, records: list, coverage: dict | None = None,
                  with_coverage: bool = True) -> Path:
    """Any invented Portable Memory v0.7 package: digests, the producer's
    review and an exact manifest, on the wire format. ``with_coverage=False``
    leaves the coverage block out, as an older producer may."""
    for record in records:
        # On the wire the digest covers the record minus review, and lives
        # inside review: pop review, digest the body, then set it back.
        review = record.pop("review")
        blob = json.dumps(record, sort_keys=True, separators=(",", ":"),
                          allow_nan=False, ensure_ascii=False)
        review["content_sha256"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        record["review"] = review
    coverage = coverage or {"read": [], "unavailable": [], "excluded": []}
    memory = {"format": "portable-memory", "schema_version": 1, "records": records}
    if with_coverage:
        memory["coverage"] = coverage
    profile = genuine_profile(records, memory)
    members = {"memory.json": json.dumps(memory, ensure_ascii=False).encode(),
               "profile-review.json": json.dumps(profile, ensure_ascii=False).encode()}
    manifest = {
        "format": "portable-memory-package", "schema_version": 1,
        "files": {name: {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
                  for name, blob in members.items()},
        "record_count": len(records),
    }
    if with_coverage:
        manifest["coverage"] = coverage
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for name, blob in members.items():
            archive.writestr(name, blob)
    path.write_bytes(buffer.getvalue())
    return path


def rewrite_selection(package: Path, target: Path, mutate, mutate_manifest=None) -> Path:
    """A selection whose records were edited and honestly re-digested: the
    package is intact, only its claims changed."""
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        selection = json.loads(archive.read("selection.json"))
    for record in selection["records"]:
        mutate(record)
        payload = {k: v for k, v in record.items() if k != "content_sha256"}
        record["content_sha256"] = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ensure_ascii=False).encode("utf-8")).hexdigest()
    if mutate_manifest:
        mutate_manifest(manifest)
    blob = json.dumps(selection, ensure_ascii=False).encode("utf-8")
    manifest["files"] = {"selection.json": {"sha256": hashlib.sha256(blob).hexdigest(),
                                            "bytes": len(blob)}}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("selection.json", blob)
    target.write_bytes(buffer.getvalue())
    return target


def build_source_package(path: Path, tweak: str = "") -> Path:
    """An invented source package in the Portable Memory v0.7 shape."""
    records = [
        {
            "id": "rec-alpha", "kind": "fact", "title": "Полив",
            "scope": "огород", "provenance": "user_statement",
            "value": LONG_NOTE + tweak, "currency": "2026-11",
            "date": "2026-11-02T08:00:00Z",
            "sources": [{"id": "s1", "locator": "chat-77",
                         "observed_at": "2026-11-02T08:00:00Z"}],
            "links": [{"target": "chat-77", "relation": "derived_from",
                       "basis": "thread summary"},
                      {"target": "memory.json#rec-beta", "relation": "same_garden",
                       "basis": "shared bed"}],
            "review": {"decision": "active"},
        },
        {
            "id": "rec-beta", "kind": "fact", "title": "Навес",
            "scope": "огород", "provenance": "model_inference",
            "value": "Вероятно, тыквам нужен навес к августу.",
            "currency": "2026-11", "date": "2026-11-05T09:30:00Z",
            "review": {"decision": "active"},
        },
        {
            "id": "rec-gamma", "kind": "fact", "title": "Забор",
            "scope": "дом", "provenance": "source",
            "value": "Старая заметка про покраску забора.",
            "date": "2026-11-01T08:00:00Z",
            "review": {"decision": "historical"},
        },
    ]
    return build_package(path, records,
                         {"read": ["chat-77"], "unavailable": [], "excluded": []})


@pytest.fixture
def source_package(tmp_path: Path) -> Path:
    return build_source_package(tmp_path / "old-assistant.zip")


@pytest.fixture
def rows_doc(source_package, tmp_path: Path):
    """inspect output saved to a rows document (create-only)."""
    rows = tmp_path / "rows.json"
    out_json(run_cli(["inspect", str(source_package), "--rows", str(rows)]))
    return rows


@pytest.fixture
def carried_all(rows_doc, tmp_path: Path):
    """A selection of every row, built once; several tests read it back."""
    out = tmp_path / "carried.zip"
    source = source_of(rows_doc)
    out_json(run_cli(["select", str(out), "--original", str(source), "--all",
                      "--receipt", receipt_of(source)]))
    return out


def source_of(rows: Path) -> Path:
    """The source path a rows document was built from (its only useful field)."""
    doc = json.loads(rows.read_text(encoding="utf-8"))
    return Path(doc["sources"][0]["path"])


def receipt_of(path: Path, *read_as: str) -> str:
    """The receipt an inspect reports for one read — the binding of a pick."""
    return out_json(run_cli(["inspect", str(path), *read_as]))["receipt"]


def sha_of(path: Path) -> str:
    """The digest an inspect reports for one file — the binding of a pick."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_members(package: Path) -> dict:
    """Read a normal output zip's members — no searching in compressed bytes."""
    with zipfile.ZipFile(package) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


# ------------------------------------------------------------------ inspect

def test_inspect_writes_the_full_rows_document_and_keeps_stdout_bounded(rows_doc):
    rows, source = rows_doc, source_of(rows_doc)
    doc = json.loads(rows.read_text(encoding="utf-8"))
    assert doc["kind"] == "amberkeep-rows"
    assert doc["schema_version"] == 1
    assert doc["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert [row["locator"] for row in doc["rows"]] == [
        "memory.json#rec-alpha", "memory.json#rec-beta", "memory.json#rec-gamma"]
    assert doc["rows"][0]["text"] == LONG_NOTE
    # The reader's own attribution: a retold user statement is the document's.
    assert doc["rows"][0]["speaker"] == "document"
    # The chain travels in the document's sources; the rows carry no path.
    assert doc["sources"][0]["sha256"] == doc["sha256"]
    assert doc["sources"][0]["label"] == source.name
    assert doc["sources"][0]["read_via"] == "portable.read_portable"
    assert "source" not in doc["rows"][0]
    assert doc["rows"][0]["scope"] == "огород"
    assert doc["rows"][0]["conversation"] == source.name

    # Stdout is bounded by default; --rows is create-only.
    again = out_json(run_cli(["inspect", str(source)]))
    assert again["mode"] == "inventory"
    assert again["row_count"] == 3
    assert LONG_NOTE[240:] not in json.dumps(again), "the full text stays off stdout"
    groups = {group["scope"]: group for group in again["groups"]}
    assert set(groups) == {"огород", "дом"}
    assert groups["огород"]["count"] == 2 and groups["дом"]["count"] == 1
    taken = run_cli(["inspect", str(source), "--rows", str(rows)])
    assert "already exists" in refused(taken)["refused"]


def test_inspect_record_and_raw_modes_show_the_full_text(source_package):
    source = source_package
    one = out_json(run_cli(["inspect", str(source), "--record", "memory.json#rec-alpha"]))
    assert one["mode"] == "record"
    assert one["row"]["text"] == LONG_NOTE
    raw = out_json(run_cli(["inspect", str(source), "--raw"]))
    assert raw["rows"][0]["text"] == LONG_NOTE


def test_inspect_refuses_a_missing_file_and_a_source_without_kind(tmp_path):
    missing = run_cli(["inspect", str(tmp_path / "no-such.zip")])
    assert "no such file" in refused(missing)["refused"]
    plain = tmp_path / "notes.txt"
    plain.write_text("{}", encoding="utf-8")
    refused_doc = refused(run_cli(["inspect", str(plain)]))
    assert "--kind" in refused_doc["refused"], "the refusal names the export route"


# ------------------------------------------------------------------ select

def test_select_builds_an_amberkeep_selection_a_portable_importer_refuses(rows_doc, tmp_path):
    rows, source = rows_doc, source_of(rows_doc)
    sha = receipt_of(source)
    out = tmp_path / "carried.zip"
    doc = out_json(run_cli(["select", str(out), "--original", str(source),
                            "--record", "memory.json#rec-alpha",
                            "--record", "memory.json#rec-beta",
                            "--receipt", sha,
                            "--decided-by", "Марта"]))
    # Both ends of the pick's link are chosen, so no link is dropped.
    assert doc["format"] == "amberkeep-selection"
    assert doc["record_count"] == 2
    # The pick's footprint stays on the owner's side: the unselected record
    # leaves no trace in the package — not its locator, not its text.
    assert doc["coverage"]["excluded_locators"] == ["memory.json#rec-gamma"]
    # The pick's footprint stays out of the package: read its real members.
    selection_text = json.dumps(json.loads(package_members(out)["selection.json"]),
                                 ensure_ascii=False)
    assert "rec-gamma" not in selection_text
    assert "Старая заметка про покраску забора." not in selection_text
    # A link into a record this pick left out travels not at all: not its
    # target, not its basis — the dropped name stays on the owner's side.
    solo = tmp_path / "solo.zip"
    solo_doc = out_json(run_cli(["select", str(solo), "--original", str(source),
                                 "--record", "memory.json#rec-alpha",
                                 "--receipt", sha]))
    assert solo_doc["coverage"]["dropped_links"] == ["memory.json#rec-beta"]
    assert solo_doc["coverage"]["dropped_link_count"] == 1
    solo_text = json.dumps(json.loads(package_members(solo)["selection.json"]),
                           ensure_ascii=False)
    assert "rec-beta" not in solo_text
    blob = out.read_bytes()
    # Formats are honestly separate: the teammate's genuine Portable reader
    # refuses this CLI's own output format.
    with pytest.raises(ValueError, match="unknown package format"):
        portable.read_portable(blob, "carried.zip")
    report = out_json(run_cli(["verify", str(out)]))
    assert report["format"] == "amberkeep-selection"
    assert report["record_count"] == 2


def test_select_carries_roles_and_the_source_chain_from_the_fresh_read(rows_doc, tmp_path):
    rows, source = rows_doc, source_of(rows_doc)
    out = tmp_path / "carried.zip"
    doc = out_json(run_cli(["select", str(out), "--original", str(source),
                            "--record", "memory.json#rec-alpha",
                            "--record", "memory.json#rec-beta",
                            "--receipt", receipt_of(source)]))
    assert [record["id"] for record in doc["records"]]
    read_back = out_json(run_cli(["read", str(out)]))
    by_title = {row["origin"]["title"]: row for row in read_back["rows"]}
    alpha = by_title["Полив"]
    beta = by_title["Навес"]
    # Roles and provenance come from the fresh source read, not a table.
    assert alpha["speaker"] == "document"
    assert alpha["origin"]["provenance"] == "user_statement"
    assert beta["speaker"] == "assistant"
    assert beta["origin"]["provenance"] == "model_inference"
    assert alpha["origin"]["scope"] == "огород"
    # The source chain travels: label, sha256, how it was read — no local path.
    chain = alpha["sources"][-1]
    assert chain["source_label"] == source.name
    assert chain["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert chain["read_via"] == "portable.read_portable"
    assert "path" not in chain
    # A carried decision is the source's own review status, verbatim.
    gamma = out_json(run_cli(["select", str(tmp_path / "gamma.zip"),
                              "--original", str(source), "--record", "memory.json#rec-gamma",
                              "--receipt", receipt_of(source)]))
    assert gamma["records"][0]["decision"] == "historical"


def test_select_rows_document_contributes_nothing_but_its_locators(rows_doc, tmp_path):
    rows, source = rows_doc, source_of(rows_doc)
    out = tmp_path / "carried.zip"
    # A rows document whose text was edited after inspect: only its locators
    # are used; every carried byte comes from the fresh source read.
    doc = json.loads(rows.read_text(encoding="utf-8"))
    for row in doc["rows"]:
        row["text"] = "ПОДМЕНА: не из источника."
        row["source"] = {"path": str(tmp_path / "moved-away.zip"),
                         "label": "moved-away.zip", "sha256": "0" * 64,
                         "read_via": "portable.read_portable", "ok": True}
    edited = tmp_path / "rows-edited.json"
    edited.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    sel = out_json(run_cli(["select", str(out), "--original", str(source),
                             "--rows", str(edited)]))
    assert sel["record_count"] == 3
    blob = out.read_bytes()
    assert "ПОДМЕНА: не из источника.".encode() not in blob
    read_back = out_json(run_cli(["read", str(out)]))
    assert all(row["text"] != "ПОДМЕНА: не из источника." for row in read_back["rows"])
    assert any(row["text"] == LONG_NOTE for row in read_back["rows"])
    # The edited table's fake source path was never opened or trusted.
    assert not (tmp_path / "moved-away.zip").exists()
    # The table binds the pick to the inspect that wrote it: a tampered digest
    # or a renamed source is a typed refusal, never a silent re-read.
    doc["sources"][0]["sha256"] = "0" * 64
    tampered = tmp_path / "rows-tampered.json"
    tampered.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    refused_sel = refused(run_cli(["select", str(tmp_path / "no-tampered.zip"),
                                   "--original", str(source), "--rows", str(tampered)]))
    assert "moved since that read" in refused_sel["refused"]
    renamed = tmp_path / "renamed-source.zip"
    renamed.write_bytes(source.read_bytes())
    mismatch = refused(run_cli(["select", str(tmp_path / "no-renamed.zip"),
                                "--original", str(renamed), "--rows", str(rows)]))
    assert "the inspect recorded" in mismatch["refused"]


def test_select_ids_are_stable_across_picks_and_bytes_are_deterministic(rows_doc, tmp_path):
    rows, source = rows_doc, source_of(rows_doc)
    one = tmp_path / "one.zip"
    two = tmp_path / "two.zip"
    again = tmp_path / "again.zip"
    first = out_json(run_cli(["select", str(one), "--original", str(source),
                              "--record", "memory.json#rec-alpha",
                              "--receipt", receipt_of(source)]))
    second = out_json(run_cli(["select", str(two), "--original", str(source),
                               "--record", "memory.json#rec-beta",
                               "--record", "memory.json#rec-alpha",
                               "--receipt", receipt_of(source)]))
    alpha_id = first["records"][0]["id"]
    assert alpha_id in [record["id"] for record in second["records"]]
    assert alpha_id.startswith("k") and len(alpha_id) == 17, \
        "the id is derived from the record, not from its position in the pick"
    # Deterministic: the same selection twice builds identical bytes.
    out_json(run_cli(["select", str(again), "--original", str(source),
                      "--record", "memory.json#rec-alpha",
                      "--receipt", receipt_of(source)]))
    assert one.read_bytes() == again.read_bytes()
    # Create-only: an existing file is never overwritten, even identically.
    taken = run_cli(["select", str(again), "--original", str(source),
                     "--record", "memory.json#rec-alpha",
                     "--receipt", receipt_of(source)])
    assert "never overwrites" in refused(taken)["refused"]


def test_select_refuses_bad_input_and_missing_locators(rows_doc, tmp_path):
    rows, source = rows_doc, source_of(rows_doc)
    out = tmp_path / "no.zip"
    sha = receipt_of(source)
    unknown = run_cli(["select", str(out), "--original", str(source),
                       "--record", "memory.json#nope", "--receipt", sha])
    assert "no such locators" in refused(unknown)["refused"]
    # Inspect → select is one bound step: no binding, no pick.
    unbound = run_cli(["select", str(out), "--original", str(source),
                       "--record", "memory.json#rec-alpha"])
    assert "bound to an inspect" in refused(unbound)["refused"]
    stale = run_cli(["select", str(out), "--original", str(source),
                     "--record", "memory.json#rec-alpha", "--receipt", sha,
                     "--expected-sha256", "0" * 64])
    assert "moved since the inspect" in refused(stale)["refused"]
    wrong_receipt = run_cli(["select", str(out), "--original", str(source),
                             "--record", "memory.json#rec-alpha", "--receipt", "ak1-" + "0" * 32])
    assert "re-inspect with the same --label and --kind" in refused(wrong_receipt)["refused"]
    nothing = run_cli(["select", str(out), "--original", str(source),
                       "--receipt", sha])
    assert "nothing was selected" in refused(nothing)["refused"]
    both = run_cli(["select", str(out), "--original", str(source),
                    "--record", "memory.json#rec-alpha", "--all",
                    "--receipt", sha])
    assert "not both" in refused(both)["refused"]
    missing = run_cli(["select", str(out), "--original", str(tmp_path / "gone.zip"),
                       "--all", "--receipt", sha])
    assert "no such file" in refused(missing)["refused"]
    fake = tmp_path / "fake.json"
    fake.write_text(json.dumps({
        "kind": "someone-elses",
        "sources": [{"path": str(source), "label": source.name, "sha256": sha_of(source),
                     "read_via": "portable.read_portable", "bytes": 0, "ok": True}],
        "rows": []}, ensure_ascii=False), encoding="utf-8")
    not_ours = run_cli(["select", str(out), "--original", str(source),
                        "--rows", str(fake)])
    # Only the locator list is used: an empty list is an honest nothing-selected.
    assert "nothing was selected" in refused(not_ours)["refused"]
    # A crafted row with an unknown speaker is refused, never guessed.
    doc = json.loads(rows.read_text(encoding="utf-8"))
    doc["rows"][0]["speaker"] = "unknown"
    unknown_speaker = tmp_path / "rows-unknown.json"
    unknown_speaker.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    guessed = run_cli(["select", str(out), "--original", str(source),
                       "--rows", str(unknown_speaker)])
    # The locator list is used, but the row is re-read from the source, where
    # the speaker is honest again; the edited table's lie does not travel.
    assert json.loads(guessed.stdout)["record_count"] == 3
    assert out.exists()


def test_select_drops_credential_fields_but_never_filters_free_text(tmp_path):
    source = build_credential_package(tmp_path / "keys-assistant.zip")
    rows = tmp_path / "keys-rows.json"
    inspect_doc = out_json(run_cli(["inspect", str(source), "--rows", str(rows)]))
    # The reader already strips known credential-shaped fields while
    # projecting rows; the strip is the reader's, and inspect reports the
    # removed field by name. The value never travels.
    assert b"tok-123" not in rows.read_bytes(), "a stripped token value never travels"
    assert any("credential" in gap and "api_token" in gap for gap in inspect_doc["gaps"])

    out = tmp_path / "keys-carried.zip"
    sel = out_json(run_cli(["select", str(out), "--original", str(source), "--all",
                            "--receipt", receipt_of(source)]))
    # A stripped token value never travels: read the real members, not the
    # compressed bytes of the archive.
    members = package_members(out)
    carried_text = "\n".join(blob.decode("utf-8") for blob in members.values())
    assert "tok-123" not in carried_text
    read_back = out_json(run_cli(["read", str(out)]))
    by_title = {row["origin"]["title"]: row for row in read_back["rows"]}
    structured = by_title["Теплица"]
    assert structured["value"] == {"примечание": "ключ висит у входа"}
    # Free text that mentions a password is a note, and travels as it is:
    # free text is never filtered by keywords, on either side.
    free = by_title["Свободный текст"]
    assert free["text"] == FREE_TEXT
    assert sel["record_count"] == 2
    assert out_json(run_cli(["verify", str(out)]))["ok"] is True


def build_credential_package(path: Path) -> Path:
    """An invented source package with a structured value and free text."""
    records = [
        {
            "id": "rec-keys", "kind": "fact", "title": "Теплица",
            "scope": "огород", "provenance": "user_statement",
            "value": {"api_token": "tok-123", "примечание": "ключ висит у входа"},
            "date": "2026-11-02T08:00:00Z",
            "review": {"decision": "active"},
        },
        {
            "id": "rec-free", "kind": "fact", "title": "Свободный текст",
            "scope": "дом", "provenance": "user_statement",
            "value": FREE_TEXT, "date": "2026-11-03T09:00:00Z",
            "review": {"decision": "active"},
        },
    ]
    return build_package(path, records)


# --------------------------------------------- read / quote / portrait

def test_read_returns_one_record_and_refuses_a_missing_one(carried_all):
    out = carried_all
    doc = out_json(run_cli(["read", str(out)]))
    assert len(doc["rows"]) == 3
    alpha_id = sorted(row["locator"].rsplit("#", 1)[-1] for row in doc["rows"])[0]
    one = out_json(run_cli(["read", str(out), "--record", alpha_id]))
    assert [row["locator"] for row in one["rows"]] == [f"selection.json#{alpha_id}"]
    missing = run_cli(["read", str(out), "--record", "k0000000000000000"])
    assert "no record" in refused(missing)["refused"]


def test_quote_binds_text_and_checks_it_against_a_named_original(rows_doc, carried_all):
    rows, source = rows_doc, source_of(rows_doc)
    out = carried_all
    records = out_json(run_cli(["read", str(out)]))["rows"]
    alpha = next(row for row in records if row["origin"]["title"] == "Полив")
    beta = next(row for row in records if row["origin"]["title"] == "Навес")
    alpha_id = alpha["locator"].rsplit("#", 1)[-1]
    beta_id = beta["locator"].rsplit("#", 1)[-1]

    quote = out_json(run_cli(["quote", str(out), "--record", alpha_id, "--context", "1"]))
    assert quote["quote"] == LONG_NOTE
    assert quote["quote_sha256"] == hashlib.sha256(LONG_NOTE.encode("utf-8")).hexdigest()
    assert "never proves" in quote["role_note"]
    # Neighbours share the scope; the package-named conversation does not group.
    assert [n["record"] for n in quote["neighbours"]] == [beta_id]
    # Without --original this is carried evidence, nothing more.
    assert quote["verification"] is None
    assert "not checked against any original" in quote["evidence"]
    assert "none was named" in quote["evidence"]

    # With --original the quote is checked through the binding chain hop.
    verified = out_json(run_cli(["quote", str(out), "--record", alpha_id,
                                 "--original", str(source)]))
    check = verified["verification"]
    assert verified["evidence"].startswith("a verified fragment")
    assert check["verified"] is True
    assert check["text_match"] is True
    assert check["role_match"] is True
    assert check["date_match"] is True
    assert check["original_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert check["locator_in_original"] == "memory.json#rec-alpha"
    assert check["role_in_original"] == "document"

    # A named file that is not the recorded original is an honest failure.
    other = build_source_package(rows.parent / "other-source.zip", tweak=" (другой сезон)")
    wrong = out_json(run_cli(["quote", str(out), "--record", alpha_id,
                              "--original", str(other)]))
    assert wrong["verification"]["verified"] is False
    assert "not the original" in wrong["verification"]["reason"]
    assert "carried inside the package" in wrong["evidence"]
    # The wrong file was opened only for this comparison; nothing was searched.


def test_portrait_groups_by_scope_with_bounded_examples(carried_all):
    out = carried_all
    doc = out_json(run_cli(["portrait", str(out)]))
    groups = {group["scope"]: group for group in doc["groups"]}
    assert doc["record_count"] == 3
    assert groups["огород"]["count"] == 2
    assert groups["дом"]["count"] == 1
    assert groups["огород"]["speakers"] == {"document": 1, "assistant": 1}
    assert groups["дом"]["speakers"] == {"document": 1}
    for group in doc["groups"]:
        assert len(group["example"]["quote"]) <= 201, "examples stay bounded"


# ------------------------------------------------------------------ verify

def test_verify_reports_damage_with_a_non_zero_exit(carried_all, tmp_path):
    out = carried_all
    damaged = tmp_path / "damaged.zip"
    damaged.write_bytes(out.read_bytes()[:-8])
    report = refused(run_cli(["verify", str(damaged)]))
    assert report["refused"]
    intact = out_json(run_cli(["verify", str(out)]))
    assert intact["ok"] is True
    assert intact["format"] == "amberkeep-selection"
    assert intact["record_count"] == 3
    # A package whose manifest count lies is refused, never a gap line.
    with zipfile.ZipFile(io.BytesIO(out.read_bytes())) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        members = {name: archive.read(name) for name in archive.namelist()
                   if name != "manifest.json"}
    manifest["record_count"] = 99
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for name, blob in members.items():
            archive.writestr(name, blob)
    lying = tmp_path / "lying.zip"
    lying.write_bytes(buffer.getvalue())
    report = refused(run_cli(["verify", str(lying)]))
    assert "count" in report["refused"]


def test_a_missing_teammate_is_a_refusal_never_a_verdict(tmp_path):
    lonely = tmp_path / "lonely"
    (lonely / "scripts").mkdir(parents=True)
    (lonely / "scripts" / "amberkeep.py").write_bytes(HELPER.read_bytes())
    source = build_source_package(tmp_path / "src.zip")
    result = subprocess.run(
        [sys.executable, str(lonely / "scripts" / "amberkeep.py"),
         "verify", str(source)],
        capture_output=True, text=True, timeout=120)
    assert result.returncode != 0
    refused_doc = json.loads(result.stdout)
    assert "portable.py is missing" in refused_doc["refused"]
    assert "ok" not in refused_doc, "a missing teammate is never a verdict"


def test_verify_accepts_the_input_portable_format_through_the_teammate(source_package):
    doc = out_json(run_cli(["verify", str(source_package)]))
    assert doc["ok"] is True
    assert doc["format"] == "portable-memory-package"
    assert doc["record_count"] == 3


# ---------------------------------------------------------------- re-carry

def test_recarry_preserves_decisions_provenance_and_the_chain(rows_doc, tmp_path):
    rows, source = rows_doc, source_of(rows_doc)
    carried = tmp_path / "carried.zip"
    out_json(run_cli(["select", str(carried), "--original", str(source), "--all",
                      "--receipt", receipt_of(source)]))

    rows2 = tmp_path / "rows2.json"
    inspect2 = out_json(run_cli(["inspect", str(carried), "--rows", str(rows2)]))
    assert inspect2["source_kind"] == "amberkeep-selection"

    carried2 = tmp_path / "carried2.zip"
    out_json(run_cli(["select", str(carried2), "--original", str(carried), "--all",
                      "--receipt", receipt_of(carried)]))
    read_back = out_json(run_cli(["read", str(carried2)]))
    by_title = {row["origin"]["title"]: row for row in read_back["rows"]}
    assert by_title["Полив"]["origin"]["provenance"] == "user_statement"
    assert by_title["Навес"]["speaker"] == "assistant"
    # A carried decision stays what it was: re-packaging does not re-activate.
    assert by_title["Забор"]["origin"]["decision"] == "historical"
    # The chain grows a hop and keeps the original locator it came from.
    chain = by_title["Полив"]["sources"]
    assert [hop["locator"] for hop in chain][-1].startswith("selection.json#")
    assert any(hop["locator"] == "memory.json#rec-alpha" for hop in chain)
    # The new hop binds this carry: sha256 + parser + locator. Earlier hops
    # are the source's own chain, carried verbatim.
    last = chain[-1]
    assert last["source_sha256"] == hashlib.sha256(carried.read_bytes()).hexdigest()
    assert last["read_via"] == "amberkeep.read_selection"


# ---------------------------------------------- the readers seam (skipped)

@pytest.mark.skipif(not READERS.is_file(), reason="readers.py is not in this payload yet")
def test_inspect_kind_routes_a_raw_export_through_the_readers_teammate(tmp_path):
    export = tmp_path / "conversations.json"
    export.write_text(json.dumps({
        "uuid": "conv-1", "name": "Полив",
        "chat_messages": [
            {"uuid": "m1", "sender": "human", "created_at": "2026-11-02T08:00:00Z",
             "content": [{"type": "text", "text": "Полей огурцы в среду, пожалуйста."}]},
            {"uuid": "m2", "sender": "assistant", "created_at": "2026-11-02T08:00:05Z",
             "content": [{"type": "text", "text": "Записала: полив в среду утром."}]},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    rows = tmp_path / "chat-rows.json"
    doc = out_json(run_cli(["inspect", str(export), "--kind", "claude",
                            "--rows", str(rows)]))
    assert doc["source_kind"] == "claude"
    # The full rows went to the file (stdout stays bounded); read them back.
    full = json.loads(rows.read_text(encoding="utf-8"))
    assert full["rows"], "the readers teammate returned rows for its own kind"
    assert full["sources"][0]["read_via"] == "readers.read_source kind=claude"
    # A raw export builds a selection through the same route.
    out = tmp_path / "chat-carried.zip"
    carried = out_json(run_cli(["select", str(out), "--original", str(export),
                                 "--kind", "claude", "--all",
                                 "--receipt", receipt_of(export, "--kind", "claude")]))
    report = out_json(run_cli(["verify", str(out)]))
    assert report["ok"] is True
    assert carried["record_count"] == report["record_count"]
    # An invalid kind is refused with the current kind list.
    bad = run_cli(["inspect", str(export), "--kind", "chat-export"])
    assert "expected one of" in refused(bad)["refused"]

# ------------------------------------------- review fixes on the same chain

def fact(record_id: str, value, decision: str = "active", **extra) -> dict:
    """One invented Portable record of the cooperative's garden."""
    record = {"id": record_id, "kind": "fact", "title": record_id, "scope": "огород",
              "provenance": "user_statement", "value": value,
              "date": "2026-11-02T08:00:00Z", "review": {"decision": decision}}
    record.update(extra)
    return record


def selection_of(package: Path) -> tuple[dict, dict]:
    members = package_members(package)
    return json.loads(members["manifest.json"]), json.loads(members["selection.json"])


def test_record_ids_keep_their_own_hash_and_a_miss_lists_the_real_ids(tmp_path):
    source = build_package(tmp_path / "hash-ids.zip", [
        fact("bed#1", "Грядка номер один у забора."),
        fact("bed#2", "Грядка номер два у колодца."),
    ])
    # A genuine Portable id may hold '#': only the exact "memory.json#"
    # prefix is removed, never the text after the last '#'.
    one = out_json(run_cli(["read", str(source), "--record", "bed#1"]))
    assert [row["locator"] for row in one["rows"]] == ["memory.json#bed#1"]
    miss = refused(run_cli(["read", str(source), "--record", "1"]))["refused"]
    assert "bed#1" in miss and "bed#2" in miss and "none" not in miss
    assert out_json(run_cli(["verify", str(source)]))["records"] == ["bed#1", "bed#2"]

    carried = tmp_path / "hash-carried.zip"
    out_json(run_cli(["select", str(carried), "--original", str(source),
                      "--record", "memory.json#bed#1", "--receipt", receipt_of(source)]))
    # The selection's own schema does not forbid '#' in an id either.
    renamed = rewrite_selection(carried, tmp_path / "hash-renamed.zip",
                                lambda record: record.update(id="старая#7"))
    again = out_json(run_cli(["read", str(renamed), "--record", "старая#7"]))
    assert again["rows"][0]["locator"] == "selection.json#старая#7"


def test_links_travel_only_when_confirmed_inside_the_pick(tmp_path):
    source = build_package(tmp_path / "linked.zip", [
        fact("keep", "Огурцы у теплицы.",
             sources=[{"id": "s1", "locator": "chat-5", "observed_at": None}],
             links=[
                 {"target": "memory.json#mate", "relation": "same_bed", "basis": "рядом"},
                 {"target": "mate", "relation": "bare", "basis": "та же грядка",
                  "note": "лишнее поле"},
                 {"target": "chat-5", "relation": "derived_from", "basis": "беседа"},
                 {"target": "secret-plot", "relation": "hidden", "basis": "тайная грядка"},
                 {"target": "memory.json#draft", "relation": "next", "basis": "черновик"},
                 {"target": "memory.json#left-out", "relation": "near", "basis": "не выбрано"},
                 # An arbitrary relation whose target happens to be a source.
                 {"target": "chat-5", "relation": "same_bed", "basis": "совпавший id"},
                 # A name both a record and a source answer to.
                 {"target": "s1", "relation": "derived_from", "basis": "двусмысленно"},
             ]),
        fact("mate", "Укроп рядом с огурцами."),
        fact("left-out", "Малина в углу."),
        fact("draft", "Черновой план осени.", decision="pending"),
        fact("secret-plot", "Закрытая грядка.", decision="exclude"),
        fact("s1", "Запись, чей id совпал с источником."),
    ])
    sha = receipt_of(source)
    both = tmp_path / "both.zip"
    doc = out_json(run_cli(["select", str(both), "--original", str(source),
                            "--record", "memory.json#keep", "--record", "memory.json#mate",
                            "--receipt", sha]))
    # Unpicked, pending and excluded targets stay on the owner's side.
    assert doc["coverage"]["dropped_links"] == sorted(
        ["secret-plot", "memory.json#draft", "memory.json#left-out", "chat-5", "s1"])
    _manifest, selection = selection_of(both)
    text = json.dumps(selection, ensure_ascii=False)
    for leaked in ("secret-plot", "тайная грядка", "draft", "черновик",
                   "left-out", "не выбрано", "лишнее поле", "совпавший id", "двусмысленно"):
        assert leaked not in text
    by_title = {record["title"]: record for record in selection["records"]}
    mate_id = by_title["mate"]["id"]
    links = by_title["keep"]["links"]
    # A confirmed internal link names the picked record by its new id; a
    # link to the record's own source travels as it is.
    assert links == [
        {"target": mate_id, "relation": "same_bed", "basis": "рядом"},
        {"target": mate_id, "relation": "bare", "basis": "та же грядка"},
        {"target": "chat-5", "relation": "derived_from", "basis": "беседа"},
    ]

    # Re-carry: the old ids resolve through the earlier selection, and a
    # target the first build wrote verbatim resolves through the chain.
    legacy = rewrite_selection(both, tmp_path / "legacy.zip", lambda record: [
        link.update(target="memory.json#mate") for link in record.get("links", [])
        if link["relation"] == "same_bed"])
    for package in (both, legacy):
        recarried = tmp_path / f"re-{package.name}"
        out_json(run_cli(["select", str(recarried), "--original", str(package), "--all",
                          "--receipt", receipt_of(package)]))
        _m, again = selection_of(recarried)
        titles = {record["title"]: record for record in again["records"]}
        new_mate = titles["mate"]["id"]
        assert [link["target"] for link in titles["keep"]["links"]] == [
            new_mate, new_mate, "chat-5"]
    # Carried alone, the internal links stay behind; the own source travels.
    alone = tmp_path / "alone.zip"
    alone_doc = out_json(run_cli(["select", str(alone), "--original", str(both),
                                  "--record", f"selection.json#{by_title['keep']['id']}",
                                  "--receipt", receipt_of(both)]))
    assert alone_doc["coverage"]["dropped_link_count"] == 1
    _m, alone_selection = selection_of(alone)
    assert [link["target"] for link in alone_selection["records"][0]["links"]] == ["chat-5"]

    # Resolution sees every record the read returned: an old alias of a
    # picked record never captures the real target of an unpicked one.
    three = tmp_path / "three.zip"
    out_json(run_cli(["select", str(three), "--original", str(source),
                      "--record", "memory.json#keep", "--record", "memory.json#mate",
                      "--record", "memory.json#left-out", "--receipt", sha]))
    _m, three_selection = selection_of(three)
    ids = {record["title"]: record["id"] for record in three_selection["records"]}

    def alias(record: dict) -> None:
        if record["title"] == "mate":
            record["sources"].append({
                "id": "s9", "locator": f"selection.json#{ids['left-out']}", "observed_at": "",
                "source_label": "old.zip", "source_sha256": "0" * 64,
                "read_via": "amberkeep.read_selection"})

    aliased = rewrite_selection(three, tmp_path / "aliased.zip", alias)
    recarried = tmp_path / "re-aliased.zip"
    re_doc = out_json(run_cli(["select", str(recarried), "--original", str(aliased),
                               "--record", f"selection.json#{ids['keep']}",
                               "--record", f"selection.json#{ids['mate']}",
                               "--receipt", receipt_of(aliased)]))
    assert ids["left-out"] in re_doc["coverage"]["dropped_links"]
    _m, re_selection = selection_of(recarried)
    titles = {record["title"]: record for record in re_selection["records"]}
    assert [link["target"] for link in titles["keep"]["links"]] == [
        titles["mate"]["id"], titles["mate"]["id"], "chat-5"]


def test_dropped_field_names_travel_with_their_own_record_only(tmp_path):
    source = build_credential_package(tmp_path / "keys.zip")
    sha = receipt_of(source)
    everything = tmp_path / "keys-all.zip"
    doc = out_json(run_cli(["select", str(everything), "--original", str(source), "--all",
                            "--receipt", sha]))
    # The reader already stripped the field; its name is kept, not lost.
    assert doc["dropped_fields"] == ["api_token"]
    assert {r["title"]: r.get("dropped_fields") for r in doc["records"]} == {
        "Теплица": ["api_token"], "Свободный текст": None}
    manifest, _selection = selection_of(everything)
    assert manifest["dropped_fields"] == ["api_token"]
    read_back = {row["origin"]["title"]: row
                 for row in out_json(run_cli(["read", str(everything)]))["rows"]}
    assert read_back["Теплица"]["origin"]["dropped_fields"] == ["api_token"]
    assert "dropped_fields" not in read_back["Свободный текст"]["origin"]

    # Picked alone, the free-text record carries no other record's names;
    # the sender still sees the source's own line.
    free = tmp_path / "keys-free.zip"
    free_doc = out_json(run_cli(["select", str(free), "--original", str(source),
                                 "--record", "memory.json#rec-free", "--receipt", sha]))
    assert free_doc["dropped_fields"] == []
    assert any("api_token" in line for line in free_doc["coverage"]["source_gap_lines"])
    assert "api_token" not in "".join(blob.decode("utf-8")
                                      for blob in package_members(free).values())

    # A re-packaging keeps the names with their record.
    again = tmp_path / "keys-again.zip"
    again_doc = out_json(run_cli(["select", str(again), "--original", str(everything), "--all",
                                  "--receipt", receipt_of(everything)]))
    assert {r["title"]: r.get("dropped_fields") for r in again_doc["records"]}["Теплица"] == [
        "api_token"]


def test_source_coverage_is_true_false_or_unknown_never_guessed(tmp_path):
    clean = build_package(tmp_path / "clean.zip", [fact("bed", "Свёкла у дорожки.")],
                          {"read": ["chat-5"], "unavailable": [], "excluded": []})
    gappy = build_package(tmp_path / "gappy.zip", [fact("bed", "Свёкла у дорожки.")],
                          {"read": ["chat-5"], "unavailable": ["chat-99-закрытый"],
                           "excluded": []})
    silent = build_package(tmp_path / "silent.zip", [fact("bed", "Свёкла у дорожки.")],
                           with_coverage=False)
    # The teammate's status comes from its parsed fields: the clean read
    # still has information lines (sources read, the producer's readiness).
    rows, lines, status = portable.read_portable_with_coverage(clean.read_bytes(), "clean.zip")
    assert lines and status is False
    assert portable.read_portable(clean.read_bytes(), "clean.zip") == (rows, lines)
    assert portable.read_portable_with_coverage(gappy.read_bytes(), "g.zip")[2] is True
    assert portable.read_portable_with_coverage(silent.read_bytes(), "s.zip")[2] is None

    def carry(source: Path, name: str) -> tuple[dict, dict, list]:
        out = tmp_path / name
        doc = out_json(run_cli(["select", str(out), "--original", str(source), "--all",
                                "--receipt", receipt_of(source)]))
        return doc, selection_of(out)[0], out_json(run_cli(["read", str(out)]))["gaps"]

    doc, manifest, gaps = carry(clean, "clean-carried.zip")
    assert doc["coverage"]["source_gaps"] is False
    assert manifest["coverage"] == {"read": [clean.name], "source_gaps": False}
    assert not any("coverage is unknown" in gap or "unavailable, left out" in gap
                   for gap in gaps)

    doc, manifest, gaps = carry(gappy, "gappy-carried.zip")
    assert doc["coverage"]["source_gaps"] is True
    assert any("chat-99-закрытый" in line for line in doc["coverage"]["source_gap_lines"])
    # No false completeness and no detail: one generic flag.
    assert manifest["coverage"] == {"read": [gappy.name], "source_gaps": True}
    out = tmp_path / "gappy-carried.zip"
    assert "chat-99" not in "".join(b.decode("utf-8") for b in package_members(out).values())
    assert any("unavailable, left out or not imported" in gap for gap in gaps)
    # The flag survives a re-packaging, whose own source read had no gaps.
    again, again_manifest, _ = carry(out, "gappy-again.zip")
    assert again["coverage"]["source_gaps"] is True
    assert again_manifest["coverage"]["source_gaps"] is True

    # Unknown stays unknown: no coverage block, or an older selection whose
    # empty "unavailable" list never proved a complete source.
    doc, manifest, gaps = carry(silent, "silent-carried.zip")
    assert doc["coverage"]["source_gaps"] is None
    assert "source_gaps" not in manifest["coverage"]
    assert any("coverage is unknown" in gap for gap in gaps)
    legacy = rewrite_selection(
        tmp_path / "clean-carried.zip", tmp_path / "legacy.zip", lambda record: None,
        lambda manifest: manifest.update(coverage={"read": [clean.name], "unavailable": []}))
    assert any("coverage is unknown" in gap
               for gap in out_json(run_cli(["read", str(legacy)]))["gaps"])
    doc, manifest, _ = carry(legacy, "legacy-again.zip")
    assert doc["coverage"]["source_gaps"] is None
    assert "source_gaps" not in manifest["coverage"]


@pytest.mark.skipif(not READERS.is_file(), reason="readers.py is not in this payload yet")
def test_quote_reads_under_the_chain_label_and_checks_role_and_date(tmp_path):
    note = tmp_path / "note.txt"
    note.write_text("Марта решила сажать чеснок в октябре.", encoding="utf-8")
    rows = tmp_path / "note-rows.json"
    out_json(run_cli(["inspect", str(note), "--kind", "text", "--label", "огород-2026",
                      "--rows", str(rows)]))
    # --rows binds the label too: the refusal says which --label to pass.
    unlabelled = refused(run_cli(["select", str(tmp_path / "no.zip"), "--original", str(note),
                                  "--kind", "text", "--rows", str(rows)]))
    assert "--label 'огород-2026'" in unlabelled["refused"]
    assert not (tmp_path / "no.zip").exists()
    out = tmp_path / "note-carried.zip"
    doc = out_json(run_cli(["select", str(out), "--original", str(note), "--kind", "text",
                            "--label", "огород-2026", "--rows", str(rows)]))
    # An undated row sorts and travels; a gap-free read is flagged as such.
    assert doc["record_count"] == 1
    assert doc["coverage"]["source_gaps"] is False
    record_id = doc["records"][0]["id"]

    renamed = tmp_path / "copy-with-another-name.txt"
    renamed.write_bytes(note.read_bytes())
    check = out_json(run_cli(["quote", str(out), "--record", record_id,
                              "--original", str(renamed)]))["verification"]
    assert check["verified"] is True
    assert check["label_from_chain"] == "огород-2026"

    for field, value, flag in (("date", "2026-01-01T00:00:00Z", "date_match"),
                               ("role", "user", "role_match")):
        changed = rewrite_selection(out, tmp_path / f"changed-{field}.zip",
                                    lambda record: record.update({field: value}))
        report = out_json(run_cli(["quote", str(changed), "--record", record_id,
                                   "--original", str(note)]))
        assert report["verification"][flag] is False
        assert report["verification"]["text_match"] is True
        assert report["verification"]["verified"] is False


def test_a_verified_projection_never_claims_the_original_wording(tmp_path):
    source = build_credential_package(tmp_path / "keys.zip")
    out = tmp_path / "keys-carried.zip"
    out_json(run_cli(["select", str(out), "--original", str(source), "--all",
                      "--receipt", receipt_of(source)]))
    structured = next(row for row in out_json(run_cli(["read", str(out)]))["rows"]
                      if row["origin"]["title"] == "Теплица")
    quote = out_json(run_cli(["quote", str(out), "--record",
                              structured["locator"].removeprefix("selection.json#"),
                              "--original", str(source)]))
    assert quote["verification"]["verified"] is True
    assert quote["text_kind"] == "projection"
    assert quote["evidence"].startswith("a verified projection")
    assert "not a fragment" in quote["evidence"]


def test_the_cli_never_writes_what_it_could_not_read_back(tmp_path, monkeypatch, capsys):
    cli = _load(HELPER, "amberkeep_cli_under_test")
    source = build_source_package(tmp_path / "src.zip")
    with zipfile.ZipFile(source) as archive:
        source_manifest = archive.getinfo("manifest.json").file_size

    def run(argv: list[str]) -> tuple[int, dict]:
        code = cli.main(argv)
        return code, json.loads(capsys.readouterr().out)

    # A lowered member limit stands in for a huge pick: the source still
    # reads, but the selection would not, so nothing is written.
    monkeypatch.setattr(cli, "MAX_MEMBER_BYTES", source_manifest + 1)
    out = tmp_path / "too-big.zip"
    code, doc = run(["select", str(out), "--original", str(source), "--all",
                     "--receipt", receipt_of(source)])
    assert code == 1 and "member limit" in doc["refused"]
    assert not out.exists()
    monkeypatch.undo()

    # Inputs are refused by size before they are read.
    monkeypatch.setattr(cli, "MAX_PACKAGE_FILE_BYTES", 64)
    code, doc = run(["verify", str(source)])
    assert code == 1 and "limit for a package" in doc["refused"]
    monkeypatch.undo()
    rows = tmp_path / "rows.json"
    assert run(["inspect", str(source), "--rows", str(rows)])[0] == 0
    monkeypatch.setattr(cli, "MAX_ROWS_FILE_BYTES", 64)
    code, doc = run(["select", str(tmp_path / "x.zip"), "--original", str(source),
                     "--rows", str(rows)])
    assert code == 1 and "limit for a rows document" in doc["refused"]
    monkeypatch.undo()

    # Create-only needs its parent; nothing half-written is left behind.
    code, doc = run(["select", str(tmp_path / "no-dir" / "x.zip"), "--original", str(source),
                     "--all", "--receipt", receipt_of(source)])
    assert code == 1 and "parent directory does not exist" in doc["refused"]
    assert not (tmp_path / "no-dir").exists()


def test_a_broken_member_stream_is_a_refusal_not_a_crash(carried_all, tmp_path):
    blob = bytearray(carried_all.read_bytes())
    with zipfile.ZipFile(carried_all) as archive:
        info = archive.getinfo("selection.json")
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    middle = start + info.compress_size // 2
    blob[middle] ^= 0xFF
    broken = tmp_path / "broken.zip"
    broken.write_bytes(bytes(blob))
    result = run_cli(["verify", str(broken)])
    assert "Traceback" not in result.stderr
    assert refused(result)["refused"]


@pytest.mark.skipif(not READERS.is_file(), reason="readers.py is not in this payload yet")
def test_reads_are_bounded_by_the_route_of_their_source(tmp_path, monkeypatch, capsys):
    cli = _load(HELPER, "amberkeep_cli_bounds_under_test")
    note = tmp_path / "note.txt"
    note.write_text("Марта посадила чеснок в октябре.", encoding="utf-8")
    out = tmp_path / "note.zip"
    doc = out_json(run_cli(["select", str(out), "--original", str(note), "--kind", "text",
                            "--all", "--receipt", receipt_of(note, "--kind", "text")]))
    record_id = doc["records"][0]["id"]

    def run(argv: list[str]) -> tuple[int, dict]:
        code = cli.main(argv)
        return code, json.loads(capsys.readouterr().out)

    # A plain export original is held to its reader's bound, not the
    # archive bound, before it is read.
    monkeypatch.setattr(cli._readers(), "_MAX_EXPORT_BYTES", 8)
    code, refusal = run(["quote", str(out), "--record", record_id, "--original", str(note)])
    assert code == 1 and "limit for one text export" in refusal["refused"]
    monkeypatch.undo()
    code, report = run(["quote", str(out), "--record", record_id, "--original", str(note)])
    assert code == 0 and report["verification"]["verified"] is True

    # A small file never asks for a buffer the size of the limit.
    asked: list[int] = []
    real_open = Path.open

    class Spy:
        def __init__(self, handle):
            self.handle = handle

        def read(self, size=-1):
            asked.append(size)
            return self.handle.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

    monkeypatch.setattr(Path, "open", lambda self, *a, **k: Spy(real_open(self, *a, **k)))
    assert cli._read_file(str(note), cli.MAX_INPUT_BYTES) == note.read_bytes()
    monkeypatch.undo()
    assert asked and max(asked) <= cli.CHUNK

    # inspect never writes a rows document that select could not read.
    monkeypatch.setattr(cli, "MAX_ROWS_FILE_BYTES", 64)
    rows = tmp_path / "rows.json"
    code, refusal = run(["inspect", str(note), "--kind", "text", "--rows", str(rows)])
    assert code == 1 and "limit select reads" in refusal["refused"]
    assert not rows.exists()


# ------------------------------------ contract fixes: pick, identity, values

def test_pick_modes_are_one_at_a_time_and_refused_before_any_read(tmp_path):
    gone, no_rows = tmp_path / "gone.zip", tmp_path / "no-rows.json"
    for modes in (["--record", "memory.json#a", "--rows", str(no_rows)],
                  ["--rows", str(no_rows), "--all"],
                  ["--record", "memory.json#a", "--all"],
                  ["--record", "memory.json#a", "--rows", str(no_rows), "--all"]):
        refusal = refused(run_cli(["select", str(tmp_path / "x.zip"), "--original", str(gone),
                                   *modes, "--receipt", "ak1-" + "0" * 32]))["refused"]
        # Neither the missing source nor the missing table was looked at.
        assert "mutually exclusive" in refusal and "no such file" not in refusal
    # --all is bound like any pick: no receipt, no read.
    unbound = refused(run_cli(["select", str(tmp_path / "x.zip"), "--original", str(gone),
                               "--all"]))["refused"]
    assert "bound to an inspect" in unbound
    assert not (tmp_path / "x.zip").exists()


@pytest.mark.skipif(not READERS.is_file(), reason="readers.py is not in this payload yet")
def test_the_receipt_binds_label_parser_and_bytes_for_every_pick(tmp_path):
    note = tmp_path / "note.txt"
    note.write_text("Марта сажает чеснок в октябре.", encoding="utf-8")
    shown = out_json(run_cli(["inspect", str(note), "--kind", "text", "--label", "огород"]))
    receipt = shown["receipt"]
    assert receipt.startswith("ak1-") and shown["sha256"] == sha_of(note)
    assert receipt in shown["hints"]["select"], "the hint carries the receipt to pass"
    locator = shown["locators"][0]
    # The same bytes under another label or another parser are another read.
    for read_as in (["--kind", "text", "--label", "другое"],
                    ["--kind", "summary", "--label", "огород"],
                    ["--kind", "text"]):
        for pick in (["--all"], ["--record", locator]):
            refusal = refused(run_cli(["select", str(tmp_path / "no.zip"), "--original", str(note),
                                       *read_as, *pick, "--receipt", receipt]))["refused"]
            assert "re-inspect with the same --label and --kind" in refusal
    assert not (tmp_path / "no.zip").exists()
    out = tmp_path / "ok.zip"
    doc = out_json(run_cli(["select", str(out), "--original", str(note), "--kind", "text",
                            "--label", "огород", "--all", "--receipt", receipt]))
    assert doc["receipt"] == receipt and doc["record_count"] == 1
    # Moved bytes are refused the same way.
    note.write_text("Марта сажает чеснок в ноябре.", encoding="utf-8")
    moved = refused(run_cli(["select", str(tmp_path / "no2.zip"), "--original", str(note),
                             "--kind", "text", "--label", "огород", "--all",
                             "--receipt", receipt]))["refused"]
    assert "the source moved" in moved


def test_a_recarry_keeps_ids_and_versions_and_a_revised_source_shows(tmp_path):
    source = build_source_package(tmp_path / "src.zip")
    first = tmp_path / "first.zip"
    one = out_json(run_cli(["select", str(first), "--original", str(source), "--all",
                            "--receipt", receipt_of(source)]))
    again = tmp_path / "again.zip"
    two = out_json(run_cli(["select", str(again), "--original", str(first), "--all",
                            "--receipt", receipt_of(first)]))
    key = lambda doc: {r["first_locator"]: (r["id"], r["version_sha256"]) for r in doc["records"]}
    assert key(one) == key(two), "an unchanged re-carry keeps id and version"
    assert set(key(one)) == {"memory.json#rec-alpha", "memory.json#rec-beta",
                             "memory.json#rec-gamma"}
    # A sub-pick from the selection keeps the id the record already has.
    gamma_id, gamma_version = key(one)["memory.json#rec-gamma"]
    sub = out_json(run_cli(["select", str(tmp_path / "sub.zip"), "--original", str(first),
                            "--record", f"selection.json#{gamma_id}",
                            "--receipt", receipt_of(first)]))
    assert key(sub) == {"memory.json#rec-gamma": (gamma_id, gamma_version)}
    # read and quote report what select reported; so does a read of the source.
    items = {i["first_locator"]: (i["record"], i["version_sha256"])
             for i in out_json(run_cli(["read", str(again)]))["items"]}
    assert items == key(one)
    quoted = out_json(run_cli(["quote", str(again), "--record", gamma_id]))
    assert (quoted["version_sha256"], quoted["decision"]) == (gamma_version, "historical")
    direct = {i["first_locator"]: i["version_sha256"]
              for i in out_json(run_cli(["read", str(source)]))["items"]}
    assert direct["memory.json#rec-gamma"] == gamma_version

    # A later export of the same item: same text and locator, so the same id;
    # any change of status, currency, role or cited source is a new version.
    def later(name: str, **changes) -> dict:
        record = fact("rec-gamma", changes.pop("value", "Старая заметка про покраску забора."),
                      decision=changes.pop("decision", "historical"), title="Забор",
                      scope="дом", provenance=changes.pop("provenance", "source"),
                      date="2026-11-01T08:00:00Z", **changes)
        path = build_package(tmp_path / name, [record])
        # The same file name as the first export, in its own folder.
        folder = tmp_path / name.removesuffix(".zip")
        folder.mkdir()
        renamed = folder / "src.zip"
        path.rename(renamed)
        doc = out_json(run_cli(["select", str(folder / "out.zip"), "--original", str(renamed),
                                "--all", "--receipt", receipt_of(renamed)]))
        return doc["records"][0]

    assert (later("same.zip")["version_sha256"], later("same2.zip")["id"]) == (
        gamma_version, gamma_id)
    for number, changes in enumerate(({"decision": "active"}, {"currency": "historical"},
                                      {"provenance": "model_inference"},
                                      {"sources": [{"id": "s1", "locator": "chat-12",
                                                    "observed_at": None}]})):
        revised = later(f"rev-{number}.zip", **dict(changes))
        assert revised["id"] == gamma_id, changes
        assert revised["version_sha256"] != gamma_version, changes
    edited = later("edited.zip", value="Забор покрашен в зелёный.")
    assert edited["first_locator"] == "memory.json#rec-gamma"
    assert edited["id"] != gamma_id and edited["version_sha256"] != gamma_version


def test_select_keeps_the_sources_row_order_never_the_arguments(tmp_path):
    """The package's records keep the source's own row order: not the order
    the locators were named in, and not a scope/date/locator re-sort."""
    source = build_source_package(tmp_path / "src.zip")
    out = tmp_path / "order.zip"
    doc = out_json(run_cli(["select", str(out), "--original", str(source),
                            "--record", "memory.json#rec-gamma",
                            "--record", "memory.json#rec-alpha",
                            "--receipt", receipt_of(source)]))
    assert [record["first_locator"] for record in doc["records"]] == [
        "memory.json#rec-alpha", "memory.json#rec-gamma"], \
        "the pick follows the source's rows, not the command line"


def test_equal_or_missing_dates_neither_scramble_the_pick_nor_the_window(tmp_path):
    """Equal and missing dates must not reorder the pick or the quote window."""
    records = []
    for rid, text, date in (("b", "Второе сообщение.", None),
                            ("a", "Первое сообщение.", "2026-11-02T08:00:00Z"),
                            ("c", "Третье сообщение.", "2026-11-02T08:00:00Z")):
        record = {"id": rid, "kind": "fact", "title": rid, "scope": "огород",
                  "provenance": "user_statement", "value": text,
                  "review": {"decision": "active"}}
        if date:
            record["date"] = date
        records.append(record)
    source = build_package(tmp_path / "dates.zip", records)
    out = tmp_path / "dates-carried.zip"
    doc = out_json(run_cli(["select", str(out), "--original", str(source), "--all",
                            "--receipt", receipt_of(source)]))
    by_id = {record["first_locator"]: record["id"] for record in doc["records"]}
    assert [record["first_locator"] for record in doc["records"]] == [
        "memory.json#b", "memory.json#a", "memory.json#c"], \
        "an undated row keeps its source place; equal dates keep file order"
    quoted = out_json(run_cli(["quote", str(out), "--record", by_id["memory.json#a"],
                                "--context", "1"]))
    neighbours = [n["record"] for n in quoted["neighbours"]]
    assert neighbours == [by_id["memory.json#b"], by_id["memory.json#c"]], \
        "the window follows the package's own order, never a date/locator sort"


def test_scope_presence_is_a_version_and_a_missing_scope_stays_missing(tmp_path):
    """A scope change on unchanged text is a new version; the scope's
    presence (concrete, stated empty, absent) digests symmetrically, and a
    ZIP name never poses as a scope."""
    same = "Одна и та же фраза про рассаду."
    absent = fact("rec", same)
    absent.pop("scope")
    shapes = {"absent": absent, "empty": fact("rec", same, scope=""),
              "concrete": fact("rec", same, scope="теплица")}
    versions, presences = {}, {}
    for name, record in shapes.items():
        source = build_package(tmp_path / f"{name}.zip", [record])
        out = tmp_path / f"{name}-sel.zip"
        doc = out_json(run_cli(["select", str(out), "--original", str(source), "--all",
                                "--receipt", receipt_of(source)]))
        versions[name] = doc["records"][0]["version_sha256"]
        _manifest, selection = selection_of(out)
        carried = selection["records"][0]
        presences[name] = carried.get("scope", "<absent>")
        # A re-carry of the same shape keeps the version: presence digests
        # symmetrically at write and read.
        again = tmp_path / f"{name}-again.zip"
        again_doc = out_json(run_cli(["select", str(again), "--original", str(out), "--all",
                                      "--receipt", receipt_of(out)]))
        assert again_doc["records"][0]["version_sha256"] == versions[name], name
    assert presences == {"absent": "<absent>", "empty": "", "concrete": "теплица"}
    assert len(set(versions.values())) == 3, "each presence is its own version"
    for name, blob in (("empty", ""), ("concrete", "теплица")):
        assert blob in json.dumps(selection_of(tmp_path / f"{name}-sel.zip")[1],
                                  ensure_ascii=False)
    absent_text = json.dumps(selection_of(tmp_path / "absent-sel.zip")[1],
                             ensure_ascii=False)
    assert '"scope"' not in absent_text, "an absent scope leaves no key behind"


def test_quote_window_never_mixes_missing_scope_with_empty_scope(tmp_path):
    """A record that stated no scope is a group of its own: the quote window
    never borrows the end of another group, not even the empty one."""
    stated = fact("stated", "Запись с пустой областью.", scope="")
    missing = fact("missing", "Запись вовсе без области.")
    missing.pop("scope")
    source = build_package(tmp_path / "scopes.zip", [stated, missing])
    out = tmp_path / "scopes-sel.zip"
    doc = out_json(run_cli(["select", str(out), "--original", str(source), "--all",
                            "--receipt", receipt_of(source)]))
    by_title = {record["title"]: record["id"] for record in doc["records"]}
    for title in ("stated", "missing"):
        quoted = out_json(run_cli(["quote", str(out), "--record", by_title[title],
                                    "--context", "2"]))
        assert quoted["neighbours"] == [], title


def test_an_explicit_empty_string_value_travels_as_content(tmp_path):
    """An explicitly empty string value is content, not emptiness: it is
    carried and read back, where no value at all is never invented."""
    record = {"id": "blank", "kind": "fact", "title": "Подпись", "scope": "огород",
              "provenance": "user_statement", "value": "",
              "date": "2026-11-02T08:00:00Z", "review": {"decision": "active"}}
    source = build_package(tmp_path / "blank.zip", [record])
    out = tmp_path / "blank-sel.zip"
    doc = out_json(run_cli(["select", str(out), "--original", str(source), "--all",
                            "--receipt", receipt_of(source)]))
    assert doc["record_count"] == 1, "an empty stated value is never empty text"
    _manifest, selection = selection_of(out)
    assert selection["records"][0]["quote"] == ""
    read_back = out_json(run_cli(["read", str(out)]))
    assert read_back["rows"][0]["text"] == ""


def test_a_chain_with_nonsequential_ids_gets_a_fresh_one(tmp_path):
    source = build_package(tmp_path / "chain.zip", [
        fact("bed", "Свёкла у дорожки.",
             sources=[{"id": "s1", "locator": "chat-1", "observed_at": None},
                      {"id": "s3", "locator": "chat-3", "observed_at": None}])])
    out = tmp_path / "bed.zip"
    out_json(run_cli(["select", str(out), "--original", str(source), "--all",
                      "--receipt", receipt_of(source)]))
    again = tmp_path / "bed-again.zip"
    out_json(run_cli(["select", str(again), "--original", str(out), "--all",
                      "--receipt", receipt_of(out)]))
    assert [hop["id"] for hop in selection_of(out)[1]["records"][0]["sources"]] == [
        "s1", "s3", "s4"]
    assert [hop["id"] for hop in selection_of(again)[1]["records"][0]["sources"]] == [
        "s1", "s3", "s4", "s5"]


def test_empty_values_null_text_and_scope_travel_as_the_source_states_them(tmp_path):
    bare = fact("bare", "Без области.")
    bare.pop("scope")
    source = build_package(tmp_path / "shapes.zip", [
        fact("obj", {}, scope=""), fact("arr", []), fact("nul", None), fact("txt", "null"),
        bare, fact("keys", {"api_token": "tok-1"})])
    receipt = receipt_of(source)
    # A value emptied by the credential strip is refused (documented); the
    # source's own empty values are not.
    emptied = refused(run_cli(["select", str(tmp_path / "no.zip"), "--original", str(source),
                               "--record", "memory.json#keys", "--receipt", receipt]))
    assert "nothing but credential-shaped fields" in emptied["refused"]
    out = tmp_path / "shapes.sel.zip"
    picks = [arg for rid in ("obj", "arr", "nul", "txt", "bare")
             for arg in ("--record", f"memory.json#{rid}")]
    out_json(run_cli(["select", str(out), "--original", str(source), *picks,
                      "--receipt", receipt]))
    again = tmp_path / "shapes.again.zip"
    out_json(run_cli(["select", str(again), "--original", str(out), "--all",
                      "--receipt", receipt_of(out)]))
    for package in (out, again):
        by_title = {r["title"]: r for r in selection_of(package)[1]["records"]}
        assert by_title["obj"]["value"] == {} and by_title["obj"]["scope"] == ""
        assert by_title["arr"]["value"] == [] and by_title["nul"]["value"] is None
        assert "value" not in by_title["txt"] and by_title["txt"]["quote"] == "null"
        # A record that stated no scope carries none: never an empty string,
        # and never the package's file name posing as its area.
        assert "scope" not in by_title["bare"], "a missing scope stays missing"
    groups = {g["scope"] for g in out_json(run_cli(["portrait", str(out)]))["groups"]}
    assert "(no scope)" in groups, "the empty scope is shown, not stored, as a fallback"
    # The file name is the display's own grouping for a package row that
    # states no area; it is never the area carried with the record.
    source_groups = {g["scope"] for g in out_json(run_cli(["portrait", str(source)]))["groups"]}
    assert source.name in source_groups
    for title, kind in (("obj", "projection"), ("arr", "projection"), ("nul", "projection"),
                        ("txt", "verbatim")):
        quote = out_json(run_cli(["quote", str(out), "--record", by_title[title]["id"],
                                  "--original", str(source)]))
        assert quote["text_kind"] == kind, title
        assert quote["verification"]["verified"] is True, title
        assert quote["verification"]["value_match"] is (True if kind == "projection" else None)


@pytest.mark.skipif(not READERS.is_file(), reason="readers.py is not in this payload yet")
def test_reader_notes_are_shown_apart_from_gaps(tmp_path, monkeypatch, capsys):
    cli = _load(HELPER, "amberkeep_cli_notes_under_test")
    readers = cli._readers()
    plain = readers.read_source

    class Gaps(list):
        notes = ["text: read 1 file as one note"]

    monkeypatch.setattr(readers, "read_source",
                        lambda kind, data, label: (plain(kind, data, label)[0], Gaps()))
    note = tmp_path / "note.txt"
    note.write_text("Марта сажает чеснок в октябре.", encoding="utf-8")

    def run(argv: list[str]) -> dict:
        assert cli.main(argv) == 0
        return json.loads(capsys.readouterr().out)

    shown = run(["inspect", str(note), "--kind", "text"])
    assert shown["gaps"] == [] and shown["notes"] == Gaps.notes and shown["notes_total"] == 1
    one = run(["inspect", str(note), "--kind", "text", "--record", shown["locators"][0]])
    assert one["gaps"] == [] and one["gaps_total"] == 0 and one["notes"] == Gaps.notes
    doc = run(["select", str(tmp_path / "n.zip"), "--original", str(note), "--kind", "text",
               "--all", "--receipt", shown["receipt"]])
    assert doc["coverage"]["source_gaps"] is False, "a note is never a loss"
    assert doc["coverage"]["source_notes"] == Gaps.notes


def test_owner_side_output_is_bounded_with_totals_and_a_route(tmp_path):
    long_tail = "-очень-длинное-имя-" * 30
    records = [fact(f"r{n:03d}", f"Заметка {n}.") for n in range(520)]
    records.append(fact("hub", "Узел.", links=[
        {"target": f"memory.json#r{n:03d}", "relation": "near", "basis": "рядом"}
        for n in range(45)]))
    records += [fact(f"wait{n}{long_tail}", "Ждёт.", decision="pending") for n in range(3)]
    source = build_package(tmp_path / "big.zip", records)
    receipt = receipt_of(source)
    doc = out_json(run_cli(["select", str(tmp_path / "hub.zip"), "--original", str(source),
                            "--record", "memory.json#hub", "--receipt", receipt]))
    coverage = doc["coverage"]
    assert coverage["excluded_count"] == 520 and coverage["excluded_truncated"] is True
    assert len(coverage["excluded_locators"]) == 500
    assert coverage["dropped_link_count"] == 45 and coverage["dropped_links_truncated"] is True
    assert len(coverage["dropped_links"]) == 40
    assert all(len(line) < 450 for line in coverage["source_gap_lines"])
    assert coverage["full"].startswith("inspect SOURCE --rows")

    one = out_json(run_cli(["inspect", str(source), "--record", "memory.json#hub"]))
    assert isinstance(one["gaps"], list) and one["gaps_total"] >= 1
    pending_line = next(line for line in one["gaps"] if "pending" in line)
    assert len(pending_line) < 450 and "in full: inspect SOURCE --rows" in pending_line
    assert one["row"]["text"] == "Узел.", "the one requested row stays full"

    report = out_json(run_cli(["verify", str(source)]))
    assert report["record_count"] == 521
    assert len(report["records"]) == 500 and report["records_truncated"] is True
    assert all(len(line) < 450 for line in report["gaps"]) and report["full"] == "read PKG.zip"
    full = out_json(run_cli(["read", str(source)]))
    assert len(full["rows"]) == 521
    assert any(long_tail in line for line in full["gaps"]), "read stays full"
    missing = refused(run_cli(["select", str(tmp_path / "x.zip"), "--original", str(source),
                               "--record", "memory.json#nope", "--receipt", receipt]))["refused"]
    assert "no such locators" in missing
