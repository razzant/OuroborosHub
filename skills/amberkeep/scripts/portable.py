"""Read a Portable Memory v0.7 package (ZIP bytes) into rows and gaps.

``read_portable(data: bytes, label: str) -> (rows, gaps)``

* ``rows`` — ``list[dict]``. Every row has ``locator``, ``speaker``, ``text``,
  ``date`` (a string, or ``None`` when the package offers none) and
  ``conversation``; ``value``, ``currency``, ``links``, ``sources`` and
  ``origin`` travel beside them when the record has them.
* ``gaps`` — ``list[str]``: what the package could not carry, what this reader
  did not import, and what the producer left unverified.

``read_portable_with_coverage`` adds the coverage status, and
``read_portable_resolving`` also the names of the withheld (excluded or
pending) records, so a caller can refuse a link into one; those names are for
resolution only and never leave the caller.

The wire contract
-----------------
``manifest.json`` — ``format="portable-memory-package"``, ``schema_version=1``,
``files`` (the exact inventory of the other members, ``{name: {sha256,
bytes}}``), ``record_count`` (exactly the number of records in
``memory.json``) and ``coverage``.
``memory.json`` — ``format="portable-memory"``, ``schema_version=1``,
``records`` (a list) and ``coverage`` with ``read`` / ``unavailable`` /
``excluded`` string lists.
A record — ``id``, ``kind``, ``title``, ``scope``, ``value``, ``provenance``
(``user_statement`` / ``source`` / ``model_inference``), ``currency``
(``current`` / ``historical`` / ``unknown``), ``sources`` (``id`` / ``locator``
/ ``observed_at``, which may be null), ``links`` (``target`` / ``relation`` /
``basis``) and ``review.decision`` (``pending`` / ``active`` / ``historical`` /
``exclude``). An accepted judgement carries ``review.content_sha256``: the
canonical sha256 of the record without its ``review``.
``profile-review.json`` — the producer's own review (``format=
"portable-common-profile-review"``, ``schema_version=1``) with ``readiness``,
per-record ``record_checks`` and a ``memory_sha256`` over its projection. For
that review this reader recomputes both: the projection digest, and every
``record_checks`` row — they must name exactly the non-excluded records and
carry the digest of each one's content. A review that disagrees with the
memory it ships with is a damaged package; its readiness and quality marks
stay the producer's own status, never this reader's approval.
Optional members — ``PROFILE.md``, ``ACTIVE_PROFILE.md``, ``graph.json``,
``wiki/*``, ``assets/*``: size and digest are checked, contents are never
read, executed or unpacked.

What is checked, before anything is projected: the format and version, that
the manifest's inventory is exactly the archive's, every listed member's size
and sha256, the manifest's record count against the records actually there,
the structure of every record, every content digest the package carries, and
the profile review's own consistency — its ``memory_sha256`` against the
canonical projection of ``memory.json``, its ``record_checks`` against the
records' ids and content digests.

What is not checked, and not claimed: that a source really says what the
package attributes to it, that the person said anything, that a record applies
to the place it is imported into, that an authorisation still holds, or that
an optional file is true. A checksum is not a signature: it shows the bytes
match what the manifest lists, and nothing more. This reader does not claim
that any other implementation reads what it writes, and it does not interpret
a producer's readiness as this reader's agreement.

What this reader never does: resolve a locator against the network or the
filesystem, restore an authorisation, execute anything from the package, or
speak in the owner's voice. A retelling inside the package is the document's
speech, not the person's. A record the package marks ``exclude`` is not
returned at all: not its text, not its id.

Only the standard library is used, and only the bytes handed in are read.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
import zlib

PACKAGE_FORMAT = "portable-memory-package"
MEMORY_FORMAT = "portable-memory"
PROFILE_REVIEW_FORMAT = "portable-common-profile-review"
SCHEMA_VERSION = 1

MANIFEST_NAME = "manifest.json"
MEMORY_NAME = "memory.json"
PROFILE_REVIEW_NAME = "profile-review.json"

PROVENANCES = ("user_statement", "source", "model_inference")
DECISIONS = ("pending", "active", "historical", "exclude")
CURRENCIES = ("current", "historical", "unknown")
# Verdicts whose records become rows. "pending" is not settled, and "exclude"
# is never admitted — and a record keeps the verdict it arrived with.
IMPORTED_DECISIONS = ("active", "historical")

# A package larger than this is refused rather than half-read: a big package
# must not produce a partial success.
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
CHUNK = 1024 * 1024

# Known typed credential fields, by name only. These are dropped from a
# structured value. Nothing else is judged: arbitrary free text travels as it
# is, and this reader does not promise to anonymise it.
CREDENTIAL_FIELDS = frozenset({
    "apikey", "apitoken", "accesstoken", "refreshtoken", "idtoken", "bottoken",
    "authtoken", "bearertoken", "token", "secret", "secretkey", "clientsecret",
    "privatekey", "password", "passwd", "passphrase", "authorization", "auth",
    "cookie", "sessioncookie", "credential", "credentials", "otp", "totp",
    "accesskey", "accesskeyid", "secretaccesskey",
})
_HEX64 = re.compile(r"\A[0-9a-fA-F]{64}\Z")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _fail(label: str, message: str) -> ValueError:
    return ValueError(f"{label}: {message}")


def _is_str(value) -> bool:
    return isinstance(value, str)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _has_supported_schema_version(doc: dict) -> bool:
    version = doc.get("schema_version")
    return _is_int(version) and version == SCHEMA_VERSION


def _check_zip_entry(label: str, info: zipfile.ZipInfo, limit: int) -> None:
    """Refuse encrypted members, unsupported compression, or oversized entries."""
    name = info.filename
    if info.flag_bits & 0x1:
        raise _fail(label, f"{name} is encrypted; encrypted packages are not supported")
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise _fail(label, f"{name} uses unsupported compression method {info.compress_type}")
    if info.file_size > limit:
        raise _fail(label, f"{name} is larger than this reader accepts")


def _stream_member(label: str, archive: zipfile.ZipFile, name: str, limit: int, on_chunk) -> int:
    """Stream one member up to ``limit`` bytes so a lying ZIP header cannot over-read."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise _fail(label, f"{name} is missing from the package") from None
    _check_zip_entry(label, info, limit)
    size = 0
    try:
        with archive.open(info) as handle:
            while True:
                chunk = handle.read(min(CHUNK, max(1, limit - size + 1)))
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise _fail(label, f"{name} is larger than this reader accepts")
                on_chunk(chunk)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, zlib.error) as exc:
        raise _fail(label, f"{name} could not be read from the archive: {exc}") from exc
    return size


def _load_json(label: str, archive: zipfile.ZipFile, name: str):
    """Read one member and parse it as JSON."""
    chunks: list[bytes] = []
    _stream_member(label, archive, name, MAX_FILE_BYTES, chunks.append)
    raw = b"".join(chunks)
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise _fail(label, f"{name} is not UTF-8 text") from exc
    except json.JSONDecodeError as exc:
        raise _fail(label, f"{name} is not valid JSON: {exc}") from exc


def _digest_member(label: str, archive: zipfile.ZipFile, name: str) -> tuple[str, int]:
    """Stream one member and return its sha256 and its uncompressed size."""
    digest = hashlib.sha256()
    size = _stream_member(label, archive, name, MAX_FILE_BYTES, digest.update)
    return digest.hexdigest(), size


def _canonical_digest(record: dict) -> str:
    """The producer's rule: sha256 of the record without its review.

    The verdict and the digest the verdict carries both live in ``review``, so
    dropping that one key drops them together.
    """
    payload = {key: value for key, value in record.items() if key != "review"}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _declared_digest(record: dict) -> str | None:
    """The content digest the record's own review carries, if it carries one."""
    review = record.get("review")
    if isinstance(review, dict) and _is_str(review.get("content_sha256")):
        return review["content_sha256"]
    return None


def _check_manifest(label: str, manifest) -> dict:
    """Validate manifest.json and return its inventory, count and coverage."""
    if not isinstance(manifest, dict):
        raise _fail(label, "manifest.json is not a JSON object")
    if manifest.get("format") != PACKAGE_FORMAT:
        raise _fail(label, f"unknown package format {manifest.get('format')!r}")
    if not _has_supported_schema_version(manifest):
        raise _fail(label, f"unsupported manifest schema_version {manifest.get('schema_version')!r}")

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise _fail(label, "manifest.json lists no files")
    inventory = {}
    for name, entry in files.items():
        if not _is_str(name) or not name:
            raise _fail(label, "manifest.json has a file entry without a name")
        if name == MANIFEST_NAME:
            raise _fail(label, "manifest.json must not list itself in 'files'")
        if not isinstance(entry, dict):
            raise _fail(label, f"the manifest entry for {name!r} is not an object")
        digest = entry.get("sha256")
        size = entry.get("bytes")
        if not _is_str(digest) or not _HEX64.match(digest):
            raise _fail(label, f"the manifest entry for {name!r} has no sha256 hex digest")
        if not _is_int(size) or size < 0:
            raise _fail(label, f"the manifest entry for {name!r} has no byte count")
        inventory[name] = {"sha256": digest.lower(), "bytes": size}

    record_count = manifest.get("record_count")
    if not _is_int(record_count) or record_count < 0:
        raise _fail(label, "manifest.json has no record_count")

    raw_coverage = manifest.get("coverage")
    coverage = _coverage_lists(label, raw_coverage, "the manifest") if raw_coverage is not None else None
    return {"files": inventory, "record_count": record_count, "coverage": coverage}


def _coverage_lists(label: str, coverage, where: str) -> dict:
    """Read a coverage block: three string lists."""
    if coverage is None:
        return {"read": [], "unavailable": [], "excluded": []}
    if not isinstance(coverage, dict):
        raise _fail(label, f"{where} has no coverage object")
    result = {}
    for key in ("read", "unavailable", "excluded"):
        value = coverage.get(key)
        if value is None:
            result[key] = []
        elif isinstance(value, list) and all(_is_str(item) for item in value):
            result[key] = list(value)
        else:
            raise _fail(label, f"{where} coverage.{key} is not a list of strings")
    return result


def _check_records(label: str, records, declared: int) -> list[dict]:
    """Validate every record's structure and digest, before anything is projected."""
    if not isinstance(records, list):
        raise _fail(label, "memory.json has no records list")
    if declared != len(records):
        raise _fail(label, f"the manifest counts {declared} records, memory.json holds "
                           f"{len(records)}")

    checked = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise _fail(label, "memory.json holds a record that is not an object")
        record_id = record.get("id")
        if not _is_str(record_id) or not record_id.strip():
            raise _fail(label, "memory.json holds a record without an id")
        if record_id in seen:
            raise _fail(label, f"memory.json holds the id {record_id!r} twice")
        seen.add(record_id)

        review = record.get("review")
        if not isinstance(review, dict):
            raise _fail(label, f"record {record_id!r} has no review object")
        decision = review.get("decision")
        if decision not in DECISIONS:
            raise _fail(label, f"record {record_id!r} has an unknown review decision {decision!r}")

        for key in ("id", "kind", "title", "scope", "provenance"):
            if key in record and not _is_str(record[key]):
                raise _fail(label, f"record {record_id!r} has a non-text {key}")
        if not _is_str(record.get("provenance")):
            raise _fail(label, f"record {record_id!r} has no provenance")
        if decision in IMPORTED_DECISIONS and "value" not in record:
            raise _fail(label, f"record {record_id!r} has no value")

        currency = record.get("currency")
        if currency is not None and not _is_str(currency):
            raise _fail(label, f"record {record_id!r} has a non-text currency")

        for key, fields in (("sources", ("id", "locator")),
                            ("links", ("target", "relation", "basis"))):
            items = record.get(key)
            if items is None:
                continue
            if not isinstance(items, list):
                raise _fail(label, f"record {record_id!r} has a {key} list that is not a list")
            for item in items:
                if not isinstance(item, dict):
                    raise _fail(label, f"record {record_id!r} has a {key} entry that is not an object")
                for field in fields:
                    if field in item and not _is_str(item[field]):
                        raise _fail(label, f"record {record_id!r} has a {key} with a non-text {field}")
                if key == "sources":
                    observed = item.get("observed_at")
                    if observed is not None and not _is_str(observed):
                        raise _fail(label, f"record {record_id!r} has a source with a non-text "
                                           "observed_at")

        digest = _declared_digest(record)
        if digest is not None:
            if not _HEX64.match(digest):
                raise _fail(label, f"record {record_id!r} has a malformed content_sha256")
            if decision in IMPORTED_DECISIONS:
                # Checked on the record as it was delivered, before anything is
                # projected: it shows the bytes are the ones the producer
                # signed for, nothing more. A record we do not admit is not
                # checked — its content never reaches the answer.
                try:
                    actual = _canonical_digest(record)
                except ValueError as exc:
                    raise _fail(label, f"record {record_id!r} cannot be canonicalised: "
                                       f"{exc}") from exc
                if actual != digest.lower():
                    raise _fail(label, f"record {record_id!r} does not match its content_sha256")
        checked.append(record)
    return checked


def _project_memory_digest(label: str, memory: dict) -> str:
    """The producer's projection rule for memory_sha256.

    The complete memory document, with the excluded records left out, the
    remaining records ordered by id, and ``metadata`` an object whose
    ``last_import`` is left out — it marks the import, not the content.
    """
    projected = dict(memory)
    kept = [record for record in projected.get("records", [])
            if record["review"]["decision"] != "exclude"]
    projected["records"] = sorted(kept, key=lambda record: record["id"])
    metadata = projected.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata.pop("last_import", None)
    projected["metadata"] = metadata
    try:
        blob = json.dumps(projected, sort_keys=True, separators=(",", ":"),
                          allow_nan=False, ensure_ascii=False)
    except ValueError as exc:
        raise _fail(label, f"memory.json cannot be canonicalised: {exc}") from exc
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _check_profile_review(label: str, profile_review: dict, memory: dict,
                          records: list[dict]) -> None:
    """Check the genuine profile review against the memory it ships with.

    Its ``record_checks`` must name exactly the non-excluded records, each row
    carrying the digest of that record's content, and its ``memory_sha256``
    must match the canonical projection of ``memory.json``. The rows' other
    marks stay the producer's own status: this is agreement between two parts
    of one package, never the receiver's approval of what they say.
    """
    checks = profile_review.get("record_checks")
    if not isinstance(checks, list):
        raise _fail(label, "the profile review carries no record_checks list")
    by_id = {record["id"]: record for record in records}
    non_excluded = {record["id"] for record in records
                    if record["review"]["decision"] != "exclude"}
    row_ids: set[str] = set()
    for row in checks:
        if not isinstance(row, dict):
            raise _fail(label, "a record_checks row is not an object")
        row_id = row.get("id")
        if not _is_str(row_id) or not row_id:
            raise _fail(label, "a record_checks row has no id")
        if row_id in row_ids:
            raise _fail(label, f"record {row_id!r} is named twice in record_checks")
        row_ids.add(row_id)
        digest = row.get("content_sha256")
        if not _is_str(digest) or not _HEX64.match(digest):
            raise _fail(label, f"the record_checks row for {row_id!r} has no "
                               "content_sha256 hex digest")
        if row_id not in by_id:
            continue
        try:
            actual = _canonical_digest(by_id[row_id])
        except ValueError as exc:
            raise _fail(label, f"record {row_id!r} cannot be canonicalised: "
                               f"{exc}") from exc
        if actual != digest.lower():
            raise _fail(label, f"record {row_id!r} does not match the content_sha256 "
                               "in its record_checks row")
    if row_ids != non_excluded:
        raise _fail(label, "the profile review's record_checks does not name exactly "
                           f"the non-excluded records; missing: "
                           f"{sorted(non_excluded - row_ids)}; unknown: "
                           f"{sorted(row_ids - non_excluded)}")
    declared = profile_review.get("memory_sha256")
    if not _is_str(declared) or not _HEX64.match(declared):
        raise _fail(label, "the profile review carries no memory_sha256 hex digest")
    if _project_memory_digest(label, memory) != declared.lower():
        raise _fail(label, "the profile review's memory_sha256 does not match this "
                           "package's memory.json")


def _strip_credentials(node, dropped: list[str]):
    """Remove known typed credential fields from a structured value."""
    if isinstance(node, dict):
        clean = {}
        for key, item in node.items():
            if _NON_ALNUM.sub("", str(key).lower()) in CREDENTIAL_FIELDS:
                dropped.append(str(key))
                continue
            clean[key] = _strip_credentials(item, dropped)
        return clean
    if isinstance(node, list):
        return [_strip_credentials(item, dropped) for item in node]
    return node


def _render_text(value) -> str:
    """The text of one row: a string stays a string, a structure stays complete."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_date(record: dict, sources: list) -> str | None:
    """The date the record offers, or None. None is never invented into a date.

    A date comes from the record itself or from the moment a source was
    observed. ``currency`` is the producer's freshness marker, not a date.
    """
    for key in ("date", "observed_at", "updated_at"):
        value = record.get(key)
        if _is_str(value) and value.strip():
            return value.strip()
    for source in sources:
        observed = source.get("observed_at")
        if _is_str(observed) and observed.strip():
            return observed.strip()
    return None


def _speaker_of(provenance: str) -> str:
    """Who the row is spoken by. A retelling is the document's speech."""
    if provenance == "model_inference":
        return "assistant"
    if provenance in PROVENANCES:
        return "document"
    return "unknown"


def _unresolved_in(profile_review) -> list[str]:
    """What the producer left unresolved: conflicts and aliases, named by kind.

    Only the kinds and counts are read. Their content stays in the producer's
    file; this reader does not republish a private history, and it does not
    read an unresolved item as a new permission.
    """
    if not isinstance(profile_review, dict):
        return []
    found = []
    for section in ("semantic", "metadata"):
        block = profile_review.get(section)
        if not isinstance(block, dict):
            continue
        for key in ("conflicts", "aliases"):
            items = block.get(key)
            if isinstance(items, list) and items:
                found.append(f"{section}.{key} ({len(items)})")
    return found


def _format_coverage_sources(items: list[str], *, reported: bool = True) -> str:
    if not reported:
        return "not reported"
    return ", ".join(items) if items else "empty"


def _coverage_gaps(coverage: dict, coverage_present: bool, manifest_coverage,
                   left_out: bool) -> bool | None:
    """Whether this read is short of its source, from parsed fields only.

    True — something was unavailable, left out, or not imported (either
    coverage block lists an unavailable or excluded source, a record was
    excluded or left pending, an optional file was not read). None — the
    package does not say what was read, or its two coverage blocks disagree.
    False — neither. Sources reported as read and the producer's readiness
    are information, never a gap.
    """
    blocks = [coverage] + ([manifest_coverage] if manifest_coverage is not None else [])
    if left_out or any(block.get(key) for block in blocks
                       for key in ("unavailable", "excluded")):
        return True
    if not coverage_present or (manifest_coverage is not None and any(
            list(manifest_coverage.get(key) or []) != coverage[key]
            for key in ("read", "unavailable", "excluded"))):
        return None
    return False


def _project(label: str, records: list[dict], coverage: dict, coverage_present: bool,
             inventory: dict, profile_review, manifest_coverage
             ) -> tuple[list[dict], list[str], bool | None, frozenset]:
    """Turn validated records into rows, gap lines, the coverage status and
    the names of the records withheld (excluded or pending), for resolution only."""
    rows: list[dict] = []
    withheld: set[str] = set()
    gaps: list[str] = []
    pending: list[str] = []
    undated: list[str] = []
    unknown_provenance: list[str] = []
    unknown_currency: list[str] = []
    dropped_fields: list[str] = []
    excluded = 0

    for record in records:
        record_id = record["id"]
        decision = record["review"]["decision"]
        if decision == "exclude":
            # Nothing of an excluded record is returned: not its text, not its id.
            excluded += 1
            withheld.update((record_id, f"{MEMORY_NAME}#{record_id}"))
            continue
        if decision not in IMPORTED_DECISIONS:
            pending.append(record_id)
            withheld.update((record_id, f"{MEMORY_NAME}#{record_id}"))
            continue

        provenance = record["provenance"]
        if provenance not in PROVENANCES:
            unknown_provenance.append(record_id)
        currency = record.get("currency")
        if _is_str(currency) and currency not in CURRENCIES:
            unknown_currency.append(record_id)

        record_dropped: list[str] = []
        value = _strip_credentials(record["value"], record_dropped)
        if record_dropped:
            dropped_fields.extend(record_dropped)
        sources = record.get("sources") or []
        links = record.get("links") or []
        date = _record_date(record, sources)
        if date is None:
            undated.append(record_id)

        origin = {
            "package": label,
            "kind": record.get("kind", ""),
            "title": record.get("title", ""),
            "provenance": provenance,
            "decision": decision,
        }
        if "scope" in record:
            # Stated scope, even an empty one; a missing scope stays missing.
            origin["scope"] = record["scope"]
        if not isinstance(value, str):
            origin["text_kind"] = "projection"
        if record_dropped:
            origin["dropped_fields"] = sorted(set(record_dropped))

        row = {
            "locator": f"{MEMORY_NAME}#{record_id}",
            "speaker": _speaker_of(provenance),
            "text": _render_text(value),
            "date": date,
            "conversation": label,
            "value": value,
            "origin": origin,
        }
        if currency is not None:
            row["currency"] = currency
        if links:
            row["links"] = links
        if sources:
            row["sources"] = sources
        rows.append(row)

    if excluded:
        gaps.append(f"{excluded} excluded record(s) were not imported; nothing of them is returned")
    if pending:
        gaps.append(f"{len(pending)} pending record(s) were not imported as settled memory: "
                    f"{', '.join(pending)}")
    if unknown_provenance:
        gaps.append(f"{len(unknown_provenance)} record(s) carry a provenance this reader does not "
                    f"know and are spoken by 'unknown': {', '.join(unknown_provenance)}")
    if unknown_currency:
        gaps.append(f"{len(unknown_currency)} record(s) carry a currency this reader does not "
                    f"know; it is carried as it stands: {', '.join(unknown_currency)}")
    if undated:
        gaps.append(f"{len(undated)} row(s) carry no date from the record or its sources: "
                    f"{', '.join(undated)}")
    if dropped_fields:
        gaps.append(f"known typed credential fields were removed from structured values: "
                    f"{', '.join(sorted(set(dropped_fields)))}; arbitrary free text travels as it is")
    if not coverage_present:
        gaps.append("memory.json carries no coverage block, so what was read, unavailable and "
                    "left out is unknown")
    if coverage["read"]:
        gaps.append(f"the package reports {len(coverage['read'])} source(s) as read: "
                    f"{', '.join(coverage['read'])}")
    if coverage["unavailable"]:
        gaps.append(f"the package could not read {len(coverage['unavailable'])} source(s): "
                    f"{', '.join(coverage['unavailable'])}")
    if coverage["excluded"]:
        gaps.append(f"the exporter left {len(coverage['excluded'])} source(s) out: "
                    f"{', '.join(coverage['excluded'])}")
    if manifest_coverage is not None:
        for key in ("read", "unavailable", "excluded"):
            listed = list(manifest_coverage.get(key) or [])
            if listed != coverage[key]:
                manifest_text = _format_coverage_sources(listed, reported=True)
                memory_text = _format_coverage_sources(coverage[key], reported=coverage_present)
                gaps.append(
                    f"the manifest and memory.json describe different {key} sources; "
                    f"both readings are carried, neither is resolved here "
                    f"(manifest: {manifest_text}; memory.json: {memory_text})"
                )
    optional = sorted(name for name in inventory
                      if name not in (MEMORY_NAME, PROFILE_REVIEW_NAME))
    if optional:
        gaps.append(f"{len(optional)} optional file(s) were checked by digest only, not read, "
                    f"not executed and not unpacked: {', '.join(optional)}")
    readiness = profile_review.get("readiness")
    gaps.append(f"the producer's own profile review reports readiness {readiness!r}; that is "
                "their status, not this reader's agreement")
    unresolved = _unresolved_in(profile_review)
    if unresolved:
        gaps.append(f"the producer left {len(unresolved)} kind(s) of item unresolved; they "
                    f"are carried as unresolved and their content is not republished: "
                    f"{', '.join(unresolved)}")
    left_out = bool(excluded or pending or optional)
    return (rows, gaps, _coverage_gaps(coverage, coverage_present, manifest_coverage, left_out),
            frozenset(withheld))


def read_portable(data: bytes, label: str) -> tuple[list[dict], list[str]]:
    """Read a Portable Memory v0.7 package and return ``(rows, gaps)``."""
    rows, gaps, _coverage = read_portable_with_coverage(data, label)
    return rows, gaps


def read_portable_with_coverage(data: bytes, label: str
                                ) -> tuple[list[dict], list[str], bool | None]:
    """Read a Portable Memory v0.7 package and return ``(rows, gaps, coverage_gaps)``.

    ``coverage_gaps`` comes from the parsed fields, never from the gap text:
    True when something was unavailable, left out or not imported, None when
    the package does not settle what was read, False when it says all of it.
    """
    rows, gaps, coverage, _withheld = read_portable_resolving(data, label)
    return rows, gaps, coverage


def read_portable_resolving(data: bytes, label: str
                            ) -> tuple[list[dict], list[str], bool | None, frozenset]:
    """``read_portable_with_coverage`` plus the names of withheld records.

    The fourth value names the excluded and pending records (their id and
    their ``memory.json#`` locator) so a caller can tell that a link points
    at a record it must not carry. It is for resolution only: never print,
    package or return it to anyone.

    ``data`` is the ZIP bytes the owner handed over; nothing is read from the
    filesystem and nothing is fetched from the network. ``label`` names the
    package in the rows and in the error messages.

    Raises ``ValueError`` when the package is damaged, of an unknown format, or
    carries a record whose content contradicts the checksum the package holds.
    """
    label = str(label)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise _fail(label, "a package is given as bytes")
    blob = bytes(data)
    if not blob:
        raise _fail(label, "the package is empty")

    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise _fail(label, f"the package is not a readable ZIP archive: {exc}") from exc

    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        names = [info.filename for info in entries]
        if len(names) != len(set(names)):
            raise _fail(label, "the archive lists a member twice")
        for name in names:
            if name.startswith("/") or name.startswith("\\") or "\\" in name:
                raise _fail(label, f"{name!r} is not a relative name")
            if any(part in ("..", "") for part in name.split("/")):
                raise _fail(label, f"{name!r} is not a plain relative name")
        if MANIFEST_NAME not in names:
            raise _fail(label, f"{MANIFEST_NAME} is missing from the package")

        manifest = _check_manifest(label, _load_json(label, archive, MANIFEST_NAME))
        inventory = manifest["files"]

        listed = set(inventory)
        present = set(names) - {MANIFEST_NAME}
        if listed != present:
            raise _fail(label, "the manifest inventory does not match the archive; listed but "
                               f"absent: {sorted(listed - present)}; present but not listed: "
                               f"{sorted(present - listed)}")
        for required in (MEMORY_NAME, PROFILE_REVIEW_NAME):
            if required not in inventory:
                raise _fail(label, f"{required} is not listed in the manifest")

        total = 0
        for name, entry in sorted(inventory.items()):
            digest, size = _digest_member(label, archive, name)
            total += size
            if total > MAX_TOTAL_BYTES:
                raise _fail(label, "the package is larger than this reader accepts")
            if size != entry["bytes"]:
                raise _fail(label, f"{name} holds {size} bytes, the manifest says {entry['bytes']}")
            if digest != entry["sha256"]:
                raise _fail(label, f"{name} does not match the sha256 in the manifest")

        memory = _load_json(label, archive, MEMORY_NAME)
        profile_review = _load_json(label, archive, PROFILE_REVIEW_NAME)
        if not isinstance(memory, dict):
            raise _fail(label, "memory.json is not a JSON object")
        if memory.get("format") != MEMORY_FORMAT:
            raise _fail(label, f"unknown memory format {memory.get('format')!r}")
        if not _has_supported_schema_version(memory):
            raise _fail(label, f"unsupported memory schema_version "
                               f"{memory.get('schema_version')!r}")
        if not isinstance(profile_review, dict):
            raise _fail(label, f"{PROFILE_REVIEW_NAME} is not a JSON object")
        if profile_review.get("format") != PROFILE_REVIEW_FORMAT:
            raise _fail(label, f"unknown profile review format {profile_review.get('format')!r}")
        if not _has_supported_schema_version(profile_review):
            raise _fail(label, f"unsupported profile review schema_version "
                               f"{profile_review.get('schema_version')!r}")

        coverage_present = memory.get("coverage") is not None
        coverage = _coverage_lists(label, memory.get("coverage"), "memory.json")
        records = _check_records(label, memory.get("records"), manifest["record_count"])
        _check_profile_review(label, profile_review, memory, records)
        return _project(label, records, coverage, coverage_present, inventory, profile_review,
                        manifest.get("coverage"))
