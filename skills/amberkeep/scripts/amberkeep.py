#!/usr/bin/env python3
"""Amberkeep: carry chosen memory from one AI assistant to another.

The owner picks what travels. This CLI reads one named source, shows a
bounded portrait, builds a package of exactly the picked records, and lets
a person check a carried quote against a named original. Offline,
standard library only, no network. Six commands:

    inspect   SOURCE [--kind KIND] [--label LABEL] [--rows ROWS.json]
              [--raw | --record LOCATOR | --page N [--limit N]]
    select    OUT.zip --original SOURCE [--kind KIND] [--label LABEL]
              (--record LOCATOR [--record ...] --receipt RECEIPT
               | --all --receipt RECEIPT | --rows ROWS.json)
              [--expected-sha256 SHA256] [--decided-by NAME] [--note TEXT]
    read      PKG.zip [--record ID]
    quote     PKG.zip --record ID [--context N] [--original PATH]
    portrait  PKG.zip
    verify    PKG.zip

Two package formats, honestly separate:

* input — "portable-memory-package" (Portable Memory v0.7), read by the
  teammate ``portable.read_portable``; a source export of another kind is
  read by the teammate ``readers.read_source`` (``--kind``). Neither
  teammate is changed or re-implemented here.
* output — "amberkeep-selection" (schema_version 1, members manifest.json
  and selection.json): a selection this CLI built, under its own name and
  version. It is NOT a Portable Memory package, and a genuine Portable
  importer is expected to refuse it. read/quote/portrait/verify accept
  both formats.

What this CLI keeps honest:

* Only files named on the command line are read. Nothing scans the disk, a
  profile or an account, nothing touches the network, and no path or field
  from an imported document or table is ever opened or trusted.
* ``select`` builds the chosen records straight from the source file named
  in that same call (``--original``): role, date, decision, links and
  value come from that fresh read, never from an editable table. A rows
  document (``--rows``) contributes nothing but its locator list, plus the
  binding of the inspect that wrote it. The package's records keep the
  source's own row order — never the order the locators were named in, and
  never a scope/date/locator re-sort.
* Inspect → select is one bound step: the pick carries the inspect's
  ``receipt`` — a short digest over the source's label, parser and sha256 —
  named with ``--receipt`` or recorded in the ``--rows`` document by that
  inspect (it catches an accidental mismatch; it is no proof). Exactly
  one way of picking (``--record``, ``--rows`` or ``--all``) is accepted,
  refused before anything is read. A source that moved, or one read under
  another label or parser, is a typed refusal before anything is built.
* A link travels only when it is confirmed. Its target is resolved against
  every record the source read returned (locator, bare id, the locators of
  its own chain): one picked record — the link is rewritten to its new id;
  anything else, or a name two records or two roles share, stays behind.
  A ``derived_from`` link to one of the record's own sources travels as it
  is — unless that name is also a record the source read withheld (excluded
  or pending): an ambiguous link stays behind. Whatever stays behind travels
  not at all: not its target, not its basis.
* ``quote --original`` reuses the chain's own label once the bytes match,
  so a renamed copy of the same original still verifies. A structured value
  read back is a projection (``text_kind``), never verbatim speech — even a
  verified projection is a round-trip of the structure, not a fragment of
  the original's wording — and a record with no review status is carried
  as none; ``active`` is never invented.
* ``quote``'s neighbour window is the record's own scope group — a missing
  scope is a group of its own, never the empty one, so unrelated
  conversations never mix — in the package's own row order, never
  re-sorted by date or locator.
* A package carries exactly the picked records. An unselected record does
  not travel: not its text, not its locator, not a count, not a link, not
  the names of fields stripped from it. The pick's footprint and the
  source's own gap lines stay in this command's output, on the owner's
  side; the package carries only the generic flag ``coverage.source_gaps``
  (true: part of the source was unavailable, left out or not imported;
  false: the read said it had all of it; absent: unknown), kept through
  every re-packaging, and each record's own ``dropped_fields`` names.
* A carried ``decision`` is the source's own review status, verbatim —
  never this CLI's or the receiver's judgement — and re-packaging never
  re-activates a historical or conflicting record.
* ``quote --original PATH`` is the human check: one named file, one chain
  hop that must bind sha256 + parser + locator, and then text, role and
  date compared one by one. ``text_match`` says the text alone;
  ``verified`` says the whole binding held. Neither says who said it.
* Records keep a stable id derived from their source locator and text —
  not from the position in the pick — and a record re-carried from a
  selection keeps the id it has, whatever else is picked; plus a content
  sha256. ``version_sha256`` digests
  what the item means, its status and its sources (text, structured value,
  role, provenance, decision, currency, scope, the first original's locator,
  parser and label, and the record's own source citations; not later
  re-packaging hops or container digests): the same ``first_locator`` with
  another version is a changed item to reconcile, never a new permission —
  and so is a scope change on unchanged text: the scope's presence
  (concrete, stated empty, or absent) digests symmetrically at write and
  read, and a package's file name never poses as a scope.
* A value stays what the source stated: a structured value — an empty
  object, list or ``null`` travels as such when nothing was stripped from
  it — stays a projection through ``quote --original``, which compares it
  as structure; an explicitly empty string value travels as the record's
  own empty text, and no value is invented where the source stated none.
* Writing is create-only: an existing file is never overwritten, not even
  with the same bytes.
* ``inspect`` keeps stdout bounded: an inventory, one page and the gaps by
  default; the full rows document goes to a path you name (``--rows``) or
  to stdout only when you ask (``--raw``).
* Known typed credential *fields* are dropped before packaging; free text
  is never filtered by keywords (a note that mentions a password stays a
  note).
* A damaged or unknown package is one explicit refusal with a non-zero
  exit — never half a success, never a crash, never a false "ok".
* Building is deterministic: the same source and the same picks build
  byte-identical packages, and a package is written only after this CLI
  has read it back within its own reading limits.

``decided_by`` names who picked, not the receiver's consent: applying a
package is the receiver's own judgment, in its own tools. The semantic
work — duplicates, contradictions with fresh memory, what is still
current — is the agent's, not this script's.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import sys
import zipfile
import zlib
from pathlib import Path

SCHEMA_VERSION = 1
ROWS_KIND = "amberkeep-rows"
PACKAGE_FORMAT = "portable-memory-package"  # input, Portable Memory v0.7
SELECTION_FORMAT = "amberkeep-selection"    # this CLI's own output
MANIFEST_NAME = "manifest.json"
SELECTION_NAME = "selection.json"
# The record locators the two package readers build: "<member>#<record id>".
# Only this exact prefix is removed to name a record; an id may hold '#'.
RECORD_PREFIXES = ("memory.json#", SELECTION_NAME + "#")

# The speakers this CLI can honestly stand behind when it carries a row.
# "unknown" is deliberately absent: a record without a speaker this CLI can
# vouch for is refused, never guessed.
SPEAKER_TO_PROVENANCE = {
    "user": "user_statement",
    "assistant": "model_inference",
    "document": "source",
}
ROLES = tuple(SPEAKER_TO_PROVENANCE)
PROVENANCES = ("user_statement", "source", "model_inference")
CARRY_DECISIONS = ("active", "historical", "aliases", "conflicts")
NOT_CARRY_DECISIONS = ("pending", "exclude")
ALL_DECISIONS = CARRY_DECISIONS + NOT_CARRY_DECISIONS

# A package larger than this is refused rather than half-read.
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
# Named files are refused by size before they are read. A package holds at
# most its listed members plus a manifest, plus ZIP headers; a rows document
# is the full inspect of at most one bounded export; only a source export
# ZIP (--kind) may be larger, since its reader streams one chosen member.
MAX_PACKAGE_FILE_BYTES = MAX_TOTAL_BYTES + MAX_MEMBER_BYTES + 4 * 1024 * 1024
MAX_ROWS_FILE_BYTES = 4 * MAX_MEMBER_BYTES
MAX_INPUT_BYTES = 512 * 1024 * 1024
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
READERS_VIA = "readers.read_source kind="
CHUNK = 1024 * 1024

# Bounded stdout: an inventory page and a locator index have caps, so a
# large source never dumps all its text into the model by default.
SNIPPET_CHARS = 200
LINE_CHARS = 300
SAMPLE_CAP = 40
LOCATOR_INDEX_CAP = 500
GROUPS_CAP = 50
GAPS_CAP = 40
PAGE_DEFAULT = 20
PAGE_MAX = 200

# A fixed ZIP timestamp so identical inputs build identical packages.
ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)

# What an inspect receipt digests: the read as the person confirmed it.
RECEIPT_SCHEMA = "amberkeep-inspect-receipt/1"


class CommandError(Exception):
    """An explicit refusal: nothing was half-done."""


# ---------------------------------------------------------------- teammates

_TEAMED: dict = {}


def _load_module(filename: str, alias: str):
    path = Path(__file__).resolve().parent / filename
    if not path.is_file():
        raise CommandError(
            f"{filename} is missing from this skill payload; this command cannot run without it")
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _portable():
    """load once: the teammate that reads a Portable Memory package."""
    if "portable" not in _TEAMED:
        portable = _load_module("portable.py", "amberkeep_portable_via_cli")
        missing = [name for name in ("read_portable", "read_portable_resolving",
                                     "_strip_credentials")
                   if not callable(getattr(portable, name, None))]
        if missing:
            raise CommandError(
                "portable.py no longer provides " + ", ".join(missing)
                + "; amberkeep.py and portable.py must stay in sync")
        _TEAMED["portable"] = portable
    return _TEAMED["portable"]


def _readers():
    """load once: the teammate that reads a source export of a kind."""
    if "readers" not in _TEAMED:
        _TEAMED["readers"] = _load_module("readers.py", "amberkeep_readers_via_cli")
    return _TEAMED["readers"]


def _strip_credentials(node, dropped: list):
    """Drop credential-shaped fields the same way the reader drops them."""
    return _portable()._strip_credentials(node, dropped)


# ------------------------------------------------------------------ helpers

def _is_str(value) -> bool:
    return isinstance(value, str)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compact_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _snippet(text: str) -> str:
    return text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else "")


def _line(text: str, route: str) -> str:
    """One diagnostic string, bounded, with the route to its full form."""
    text = str(text)
    if len(text) <= LINE_CHARS:
        return text
    return f"{text[:LINE_CHARS]}… (+{len(text) - LINE_CHARS} chars; in full: {route})"


def _sample(items, route: str, cap: int = SAMPLE_CAP) -> dict:
    """A bounded sample of a list of strings, keeping its true total."""
    items = list(items)
    return {"sample": [_line(item, route) for item in items[:cap]],
            "total": len(items), "truncated": len(items) > cap,
            **({"full": route} if len(items) > cap
               or any(len(str(item)) > LINE_CHARS for item in items[:cap]) else {})}


def _names(items, route: str, cap: int = SAMPLE_CAP) -> str:
    """A bounded, comma-joined list of names for a refusal message."""
    items = [str(item) for item in items]
    clipped = any(len(item) > SNIPPET_CHARS for item in items[:cap])
    text = ", ".join(_snippet(item) for item in items[:cap]) or "none"
    if len(items) > cap:
        text += f" … and {len(items) - cap} more"
    if clipped or len(items) > cap:
        text += f" (names clipped or omitted; in full: {route})"
    return text


def _receipt(label: str, read_via: str, sha256: str) -> str:
    """The inspect receipt: a short digest binding label, parser and bytes."""
    blob = _compact_json({"schema": RECEIPT_SCHEMA, "label": label,
                          "read_via": read_via, "sha256": sha256})
    return "ak1-" + _sha256_hex(blob.encode("utf-8"))[:32]


def _read_file(path: str, limit: int = MAX_INPUT_BYTES, what: str = "a file",
               route: str = "share a smaller file") -> bytes:
    """Read one named file, refused by size before the read and never past ``limit``."""
    target = Path(path)
    if not target.is_file():
        raise CommandError(f"{path}: no such file; this CLI reads only files you name explicitly")
    size = target.stat().st_size
    if size <= limit:
        # In chunks, so a small file never asks for a buffer the size of the
        # limit, and a file that grows meanwhile still stops one byte past it.
        chunks: list[bytes] = []
        read = 0
        with target.open("rb") as handle:
            while read <= limit:
                chunk = handle.read(min(CHUNK, limit + 1 - read))
                if not chunk:
                    break
                chunks.append(chunk)
                read += len(chunk)
        data = b"".join(chunks)
        size = max(size, read)
    if size > limit:
        raise CommandError(
            f"{path}: the file is {size} bytes or more, over the {limit}-byte limit for {what}; "
            f"{route}")
    if not data:
        raise CommandError(f"{path}: the file is empty")
    return data


def _read_package_file(path: str) -> bytes:
    return _read_file(path, MAX_PACKAGE_FILE_BYTES, "a package",
                      "a source export is read with --kind KIND")


def _read_source_file(path: str, kind) -> bytes:
    """A source by its route: a package without --kind; an export with it.

    An export ZIP may be large (its reader streams one chosen member); any
    other export is refused before the read at the reader's own bound.
    """
    if not kind:
        return _read_package_file(path)
    target = Path(path)
    if target.is_file():
        with target.open("rb") as handle:
            head = handle.read(4)
        if not head.startswith(ZIP_MAGIC):
            limit = getattr(_readers(), "_MAX_EXPORT_BYTES", MAX_MEMBER_BYTES)
            return _read_file(path, limit, f"one {kind} export",
                              "pass a smaller file, for example only the conversations you need")
    return _read_file(path, MAX_INPUT_BYTES, "one export archive",
                      "share a smaller archive, or pass its chosen conversations.json alone")


def _write_output(path: Path, blob: bytes) -> None:
    """Create-only, atomically: an existing file is never overwritten, not even
    identically, and a failed write leaves no half-written file behind."""
    try:
        handle = open(path, "xb")
    except FileExistsError:
        raise CommandError(f"{path}: already exists; this CLI never overwrites a file") from None
    except FileNotFoundError:
        raise CommandError(f"{path}: the parent directory does not exist") from None
    try:
        with handle:
            handle.write(blob)
    except OSError:
        path.unlink(missing_ok=True)
        raise


def _bare_id(locator) -> str | None:
    """The record id inside a package locator: exactly one known prefix removed."""
    if _is_str(locator):
        for prefix in RECORD_PREFIXES:
            if locator.startswith(prefix) and len(locator) > len(prefix):
                return locator[len(prefix):]
    return None


def _record_id(row: dict) -> str:
    locator = str(row.get("locator", ""))
    return _bare_id(locator) or locator


def _binding_hops(row: dict) -> list:
    """The chain hops this CLI wrote: each binds sha256 + parser + locator."""
    return [hop for hop in row.get("sources") or []
            if isinstance(hop, dict)
            and all(_is_str(hop.get(key)) and hop[key]
                    for key in ("source_sha256", "read_via", "locator"))]


def _first_locator(row: dict) -> str:
    """The record's locator in its first original: the earliest chain hop
    this CLI wrote, or its own locator when it has no chain yet."""
    hops = _binding_hops(row)
    return hops[0]["locator"] if hops else str(row.get("locator", ""))


def _stated_scope(row: dict):
    """The scope a row's record stated, with its presence kept: ``None`` —
    the record stated none; ``""`` — stated empty; a string — stated
    concrete. A row from an export carries its own conversation as its
    area; a package's file name never poses as a scope here."""
    origin = row.get("origin") if isinstance(row.get("origin"), dict) else {}
    if "scope" in origin:
        return origin["scope"]
    if origin:
        return None
    return row.get("scope") or ""


def _original_refs(item: dict, read_via: str, label: str) -> dict:
    """The meaningful original source references of a row or record: where
    it was first read (locator, parser, label of the first chain hop, or the
    package now read when there is none) and its own source citations. Later
    re-packaging hops and container byte digests are left out."""
    hops = _binding_hops(item)
    first = ({"locator": hops[0]["locator"], "read_via": hops[0]["read_via"],
              "label": hops[0].get("source_label") or ""} if hops
             else {"locator": str(item.get("locator", "")), "read_via": read_via, "label": label})
    written = {id(hop) for hop in hops}
    citations = [{key: entry[key] for key in ("id", "locator", "observed_at") if key in entry}
                 for entry in item.get("sources") or []
                 if isinstance(entry, dict) and id(entry) not in written]
    return {"first": first, "citations": citations}


def _item_version(text: str, value, has_value: bool, role, provenance, decision,
                  currency, refs: dict, scope) -> str:
    """What an item means, its status and its sources, digested: text, a
    structured value, role, provenance, decision, currency, scope (with its
    presence: concrete, stated empty, or absent) and its original source
    references. An unchanged re-carry keeps it; any change there is a
    changed item to reconcile."""
    payload = {"text": text, "role": role, "provenance": provenance or None,
               "decision": decision or None, "currency": currency or None, "sources": refs,
               "scope": scope}
    if has_value and not (isinstance(value, str) and value == text):
        payload["value"] = value
    return _sha256_hex(_compact_json(payload).encode("utf-8"))


def _version_of_row(row: dict, read_via: str, label: str) -> str:
    """The version of a row read back from a package (either format)."""
    origin = row.get("origin") if isinstance(row.get("origin"), dict) else {}
    return _item_version(str(row.get("text", "")), row.get("value"), "value" in row,
                         row.get("speaker"), origin.get("provenance"), origin.get("decision"),
                         row.get("currency"), _original_refs(row, read_via, label),
                         _stated_scope(row))


def _version_of_record(record: dict) -> str:
    """The version of a record this CLI builds — equal to its read-back row's."""
    return _item_version(record["quote"], record.get("value"), "value" in record,
                         record["role"], record["provenance"], record.get("decision"),
                         record.get("currency"), _original_refs(record, "", ""),
                         record["scope"] if "scope" in record else None)


def _stable_id(locator: str, text: str) -> str:
    """The pick's position must not define a record: its source locator and
    text do. The same record carries the same id across re-selections, and
    an edited text becomes a new id instead of hijacking an old one."""
    return "k" + hashlib.sha256(f"{locator}\u0000{text}".encode("utf-8")).hexdigest()[:16]


def _selection_digest(record: dict) -> str:
    """This format's own integrity rule: sha256 over the record minus its digest."""
    payload = {key: value for key, value in record.items() if key != "content_sha256"}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _groups_of(rows: list) -> list:
    """Groups by scope: counts, speakers, dates, one bounded example each."""

    def scope_of(row: dict) -> str:
        # A scope the record states is shown as stated (an empty one as
        # "(no scope)"). Where it states none, the display groups by the
        # row's own conversation — an export's conversation, or the package
        # whose file name the reader put there. That fallback is for this
        # grouping only: it is never carried as the record's area.
        origin = row.get("origin") if isinstance(row.get("origin"), dict) else {}
        if "scope" in origin:
            return origin["scope"] or "(no scope)"
        return row.get("scope") or row.get("conversation") or "(no scope)"

    ordered = sorted(rows, key=lambda r: (scope_of(r), r.get("date") or "", str(r.get("locator"))))
    grouped, order = {}, []
    for row in ordered:
        key = scope_of(row)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)
    groups = []
    for key in order:
        members = grouped[key]
        dates = sorted({member.get("date") for member in members if member.get("date")})
        speakers: dict = {}
        for member in members:
            speaker = member.get("speaker", "")
            speakers[speaker] = speakers.get(speaker, 0) + 1
        example = members[0]
        groups.append({
            "scope": _snippet(key),
            "count": len(members),
            "speakers": speakers,
            "first_date": _snippet(str(dates[0])) if dates else "",
            "last_date": _snippet(str(dates[-1])) if dates else "",
            "example": {"locator": _snippet(str(example.get("locator", ""))),
                        "date": (_snippet(str(example["date"])) if example.get("date")
                                 else example.get("date")),
                        "quote": _snippet(str(example.get("text", "")))},
        })
    return groups


# ------------------------------------------------------------- source rows

def _normalize_row(raw, index: int) -> dict:
    """One row of a source read, in the shape the commands work with."""
    where = f"row {index}"
    if not isinstance(raw, dict):
        raise CommandError(f"{where}: not an object")
    locator = raw.get("locator")
    if not _is_str(locator) or not locator.strip():
        raise CommandError(f"{where}: no locator")
    speaker = raw.get("speaker")
    if not _is_str(speaker) or not speaker.strip():
        raise CommandError(f"{locator}: no speaker")
    text = raw.get("text")
    if not _is_str(text):
        raise CommandError(f"{locator}: no text")
    date = raw.get("date")
    if date is not None and not _is_str(date):
        raise CommandError(f"{locator}: date is not a string")
    conversation = raw.get("conversation")
    if conversation is not None and not _is_str(conversation):
        raise CommandError(f"{locator}: conversation is not a string")
    origin = raw.get("origin") if isinstance(raw.get("origin"), dict) else {}

    def pick(key: str) -> str:
        value = raw.get(key)
        if value is None:
            value = origin.get(key)
        return value if _is_str(value) else ""

    # A record's own scope travels as stated, an empty one included. A row
    # read from a package (portable or selection) whose record states none has
    # no area: its conversation is the package's own file name, never the area
    # to carry. Only a row from an export, whose conversation is the
    # conversation it came from, groups by that conversation.
    if _is_str(raw.get("scope")):
        scope = raw["scope"]
    elif _is_str(origin.get("scope")):
        scope = origin["scope"]
    elif origin:
        scope = ""
    else:
        scope = conversation if _is_str(conversation) else ""

    links = raw.get("links")
    if links is not None:
        if not isinstance(links, list) or any(not isinstance(link, dict) for link in links):
            raise CommandError(f"{locator}: links is not a list of objects")
        for link in links:
            for key in ("target", "relation", "basis"):
                if key in link and not _is_str(link[key]):
                    raise CommandError(f"{locator}: a link has a non-text {key}")
    sources = raw.get("sources")
    if sources is not None and (
            not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources)):
        raise CommandError(f"{locator}: sources is not a list of objects")

    # A missing value is the text; an explicit null is a value of its own.
    has_value = "value" in raw
    value = raw["value"] if has_value else text
    row = {
        "locator": locator,
        "speaker": speaker,
        "text": text,
        "date": date or "",
        "conversation": conversation or "",
        "kind": pick("kind"),
        "title": pick("title"),
        "scope": scope,
    }
    # The reader's own seam marks travel with the row: text_kind for a
    # re-serialized structure, dropped_fields for what this record lost.
    if isinstance(raw.get("origin"), dict):
        row["origin"] = dict(raw["origin"])
    currency = pick("currency")
    if currency:
        row["currency"] = currency
    provenance = pick("provenance")
    if provenance in PROVENANCES:
        row["provenance"] = provenance
    decision = raw.get("decision")
    if decision is None:
        decision = origin.get("decision")
    if decision is not None and not _is_str(decision):
        raise CommandError(f"{locator}: decision is not a string")
    if _is_str(decision) and decision:
        # The source's own review status, verbatim: never this CLI's or the
        # receiver's judgement, and never re-activated by a re-packaging.
        row["decision"] = decision
    if links is not None:
        row["links"] = links
    if sources:
        row["sources"] = sources
    if has_value:
        # A value the source stated is a value — an explicit empty string
        # included; a row that stated none stays text-only, and no value
        # is invented for it.
        row["value"] = value
    return row


def _read_source(path: str, kind, label=None):
    """Read exactly one named source into normalized rows.

    The dispatch is explicit: ``--kind`` routes through ``readers.read_source``;
    without it the manifest's format decides between the two package readers.
    Nothing but the named file is opened. Returns rows, gap lines, the
    source description, the names of records the read withheld (for link
    resolution only; never printed or packaged) and the reader's
    informational notes (never a loss).
    """
    data = _read_source_file(path, kind)
    label = label or Path(path).name
    sha = _sha256_hex(data)
    withheld: frozenset = frozenset()
    notes: list = []
    if kind:
        rows, gaps = _readers().read_source(kind, data, label)
        # This teammate's gap lines are losses; its informational
        # observations ride on gaps.notes and never count as a loss.
        source_gaps = bool(gaps)
        notes = [str(note) for note in getattr(gaps, "notes", None) or []]
        read_via, source_kind = READERS_VIA + kind, kind
    else:
        fmt = _sniff_format(data, label)
        if fmt == PACKAGE_FORMAT:
            # The teammate's own coverage status, from its parsed fields: its
            # gap lines also hold plain information (what was read, the
            # producer's readiness), so their presence proves no loss.
            rows, gaps, source_gaps, withheld = _portable().read_portable_resolving(data, label)
            read_via, source_kind = "portable.read_portable", PACKAGE_FORMAT
        else:
            # A selection's own gap lines are about the selection, not its
            # source; whether its source was read with gaps is its flag.
            rows, gaps, source_gaps, withheld = _open_selection(data, label)
            read_via, source_kind = "amberkeep.read_selection", SELECTION_FORMAT
    normalized = [_normalize_row(raw, index) for index, raw in enumerate(rows, 1)]
    source = {"path": str(Path(path)), "label": label, "sha256": sha,
              "bytes": len(data), "read_via": read_via, "source_kind": source_kind, "ok": True,
              "source_gaps": source_gaps, "receipt": _receipt(label, read_via, sha)}
    return normalized, list(gaps or []), source, withheld, notes


def _reread_original(data: bytes, label: str, read_via: str) -> list:
    """Re-parse an original exactly the way its chain hop records it was read."""
    if read_via == "portable.read_portable":
        rows, _gaps = _portable().read_portable(data, label)
    elif read_via == "amberkeep.read_selection":
        rows, _gaps = _read_selection(data, label)
    elif read_via.startswith(READERS_VIA):
        rows, _gaps = _readers().read_source(read_via[len(READERS_VIA):], data, label)
    else:
        raise CommandError(f"the chain records no way to re-read its original (read_via {read_via!r})")
    return [_normalize_row(raw, index) for index, raw in enumerate(rows, 1)]


# ------------------------------------------------- package sniffing and I/O

def _sniff_format(data: bytes, label: str) -> str:
    """Which package format is this — size-checked before anything is read."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = _zip_json(label, archive, MANIFEST_NAME)
    except KeyError:
        raise CommandError(
            f"{label}: not a ZIP archive holding a {MANIFEST_NAME}; pass --kind KIND to read it "
            "as a source export (readers.read_source decides which kinds it supports)") from None
    except zipfile.BadZipFile as exc:
        raise CommandError(
            f"{label}: not a readable ZIP archive: {exc}; pass --kind KIND to read it as a "
            "source export (readers.read_source decides which kinds it supports)") from exc
    fmt = manifest.get("format") if isinstance(manifest, dict) else None
    if fmt in (PACKAGE_FORMAT, SELECTION_FORMAT):
        return fmt
    raise CommandError(
        f"{label}: unknown package format {fmt!r}; this CLI reads Portable Memory packages "
        f"({PACKAGE_FORMAT!r}) and Amberkeep selections ({SELECTION_FORMAT!r})")


def _stream_member(label: str, archive: zipfile.ZipFile, name: str, on_chunk) -> int:
    """Stream one member, so a lying ZIP header cannot over-read, and a broken
    stream is one refusal rather than a crash."""
    info = archive.getinfo(name)
    if info.flag_bits & 0x1:
        raise CommandError(f"{label}: {name} is encrypted; encrypted packages are not supported")
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise CommandError(
            f"{label}: {name} uses unsupported compression method {info.compress_type}")
    if info.file_size > MAX_MEMBER_BYTES:
        raise CommandError(f"{label}: {name} is larger than this reader accepts")
    size = 0
    try:
        with archive.open(info) as handle:
            while True:
                chunk = handle.read(min(CHUNK, max(1, MAX_MEMBER_BYTES - size + 1)))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_MEMBER_BYTES:
                    raise CommandError(f"{label}: {name} is larger than this reader accepts")
                on_chunk(chunk)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError,
            OSError, zlib.error) as exc:
        raise CommandError(f"{label}: {name} could not be read from the archive: {exc}") from exc
    return size


def _zip_member(label: str, archive: zipfile.ZipFile, name: str) -> bytes:
    chunks: list[bytes] = []
    _stream_member(label, archive, name, chunks.append)
    return b"".join(chunks)


def _zip_json(label: str, archive: zipfile.ZipFile, name: str):
    try:
        return json.loads(_zip_member(label, archive, name).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CommandError(f"{label}: {name} is not UTF-8 JSON: {exc}") from exc


def _digest_member(label: str, archive: zipfile.ZipFile, name: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = _stream_member(label, archive, name, digest.update)
    return digest.hexdigest(), size


def _check_manifest_inventory(label: str, manifest) -> tuple[dict, int, dict]:
    if not isinstance(manifest, dict):
        raise CommandError(f"{label}: manifest.json is not a JSON object")
    if manifest.get("format") != SELECTION_FORMAT:
        raise CommandError(f"{label}: unknown package format {manifest.get('format')!r}")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise CommandError(
            f"{label}: unsupported manifest schema_version {manifest.get('schema_version')!r}")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise CommandError(f"{label}: manifest.json lists no files")
    inventory = {}
    for name, entry in files.items():
        if (not _is_str(name) or not name or name == MANIFEST_NAME
                or not isinstance(entry, dict)):
            raise CommandError(f"{label}: manifest.json has a file entry that is not a member")
        digest, size = entry.get("sha256"), entry.get("bytes")
        if (not (_is_str(digest) and len(digest) == 64
                 and all(char in "0123456789abcdef" for char in digest.lower()))
                or not (isinstance(size, int) and not isinstance(size, bool) and size >= 0)):
            raise CommandError(f"{label}: the manifest entry for {name!r} has no sha256 and byte count")
        inventory[name] = {"sha256": digest.lower(), "bytes": size}
    declared = manifest.get("record_count")
    if not isinstance(declared, int) or isinstance(declared, bool) or declared < 0:
        raise CommandError(f"{label}: manifest.json has no record_count")
    coverage = manifest.get("coverage")
    if coverage is not None and not isinstance(coverage, dict):
        raise CommandError(f"{label}: the manifest coverage is not an object")
    for key in ("read", "unavailable"):
        value = (coverage or {}).get(key)
        if value is not None and (
                not isinstance(value, list) or any(not _is_str(item) for item in value)):
            raise CommandError(f"{label}: coverage.{key} is not a list of strings")
    if not isinstance((coverage or {}).get("source_gaps", False), bool):
        raise CommandError(f"{label}: coverage.source_gaps is not true or false")
    return inventory, declared, coverage or {}


def _read_selection(data: bytes, label: str) -> tuple[list[dict], list[str]]:
    rows, gaps, _source_gaps, _withheld = _open_selection(data, label)
    return rows, gaps


def _open_selection(data: bytes, label: str) -> tuple[list[dict], list[str], bool | None,
                                                      frozenset]:
    """Read an amberkeep-selection: this CLI's own output format.

    Checked before anything is projected: the format and version, that the
    manifest's inventory is exactly the archive's, every member's size and
    sha256, each record's structure, and each record's content_sha256.
    Not checked, not claimed: who said anything, that a statement is true,
    or that a record applies where it is imported.

    Returns rows, gap lines, whether this selection's own source was read
    with gaps — the flag a re-packaging keeps — and the names of the records
    it withheld (for link resolution only).
    """
    label = str(label)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise CommandError(f"{label}: a package is given as bytes")
    blob = bytes(data)
    if not blob:
        raise CommandError(f"{label}: the package is empty")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise CommandError(f"{label}: the package is not a readable ZIP archive: {exc}") from exc

    with archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if len(names) != len(set(names)):
            raise CommandError(f"{label}: the archive lists a member twice")
        for name in names:
            if name.startswith("/") or "\\" in name or ".." in name.split("/"):
                raise CommandError(f"{label}: {name!r} is not a plain relative name")
        if MANIFEST_NAME not in names:
            raise CommandError(f"{label}: {MANIFEST_NAME} is missing from the package")
        manifest = _zip_json(label, archive, MANIFEST_NAME)
        inventory, declared, coverage = _check_manifest_inventory(label, manifest)
        if SELECTION_NAME not in inventory:
            raise CommandError(f"{label}: {SELECTION_NAME} is not listed in the manifest")

        present = set(names) - {MANIFEST_NAME}
        if set(inventory) != present:
            raise CommandError(
                f"{label}: the manifest inventory does not match the archive; "
                f"listed but absent: {sorted(set(inventory) - present)}; "
                f"present but not listed: {sorted(present - set(inventory))}")
        total = 0
        for name, entry in sorted(inventory.items()):
            digest, size = _digest_member(label, archive, name)
            total += size
            if total > MAX_TOTAL_BYTES:
                raise CommandError(f"{label}: the package is larger than this reader accepts")
            if size != entry["bytes"]:
                raise CommandError(f"{label}: {name} holds {size} bytes, the manifest says {entry['bytes']}")
            if digest != entry["sha256"]:
                raise CommandError(f"{label}: {name} does not match the sha256 in the manifest")

        selection = _zip_json(label, archive, SELECTION_NAME)
        if not isinstance(selection, dict):
            raise CommandError(f"{label}: {SELECTION_NAME} is not a JSON object")
        if selection.get("format") != SELECTION_FORMAT:
            raise CommandError(f"{label}: unknown selection format {selection.get('format')!r}")
        if selection.get("schema_version") != SCHEMA_VERSION:
            raise CommandError(
                f"{label}: unsupported selection schema_version {selection.get('schema_version')!r}")
        records = selection.get("records")
        if not isinstance(records, list):
            raise CommandError(f"{label}: {SELECTION_NAME} has no records list")

        rows: list[dict] = []
        gaps: list[str] = []
        pending: list[str] = []
        undecided: list[str] = []
        excluded: list[str] = []
        withheld: set[str] = set()
        seen: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise CommandError(f"{label}: {SELECTION_NAME} holds a record that is not an object")
            record_id = record.get("id")
            if not _is_str(record_id) or not record_id.strip():
                raise CommandError(f"{label}: a record has no id")
            if record_id in seen:
                raise CommandError(f"{label}: the id {record_id!r} is listed twice")
            seen.add(record_id)
            decision = record.get("decision")
            if decision is not None and not _is_str(decision):
                raise CommandError(f"{label}: record {record_id!r} has a non-text decision")
            if _is_str(decision) and decision not in ALL_DECISIONS:
                raise CommandError(
                    f"{label}: record {record_id!r} has an unknown decision {decision!r}")
            if _is_str(decision) and decision in NOT_CARRY_DECISIONS:
                # Nothing of a pending or excluded record is returned; its
                # names are kept only so no link can point into it.
                (excluded if decision == "exclude" else pending).append(record_id)
                names = {record_id, f"{SELECTION_NAME}#{record_id}"}
                if isinstance(record.get("sources"), list):
                    names |= {hop["locator"] for hop in _binding_hops(record)}
                withheld |= names | {bare for bare in map(_bare_id, list(names)) if bare}
                continue
            if decision is None:
                # No review status is invented: a record without one travels
                # as none, and re-packaging never re-activates it.
                undecided.append(record_id)
            role = record.get("role")
            if role not in ROLES:
                raise CommandError(f"{label}: record {record_id!r} has an unknown role {role!r}")
            provenance = record.get("provenance")
            if provenance not in PROVENANCES:
                raise CommandError(
                    f"{label}: record {record_id!r} has an unknown provenance {provenance!r}")
            quote = record.get("quote")
            if not _is_str(quote):
                # An explicit empty quote travels as the record's own empty
                # text; a missing one is refused, never invented.
                raise CommandError(f"{label}: record {record_id!r} has no quote text")
            digest = record.get("content_sha256")
            if not (_is_str(digest) and len(digest) == 64):
                raise CommandError(f"{label}: record {record_id!r} has no content_sha256")
            if digest.lower() != _selection_digest(record):
                raise CommandError(
                    f"{label}: record {record_id!r} does not match its content_sha256")
            for key in ("kind", "title", "scope", "date"):
                if key in record and not _is_str(record[key]):
                    raise CommandError(f"{label}: record {record_id!r} has a non-text {key}")
            record_dropped = record.get("dropped_fields")
            if record_dropped is not None and (
                    not isinstance(record_dropped, list)
                    or any(not _is_str(item) for item in record_dropped)):
                raise CommandError(
                    f"{label}: record {record_id!r} has a dropped_fields that is not a list of names")
            for key, fields in (("sources", ("id", "locator", "source_label",
                                             "source_sha256", "read_via")),
                                ("links", ("target", "relation", "basis"))):
                items = record.get(key)
                if items is None:
                    continue
                if not isinstance(items, list):
                    raise CommandError(f"{label}: record {record_id!r} has a {key} that is not a list")
                for item in items:
                    if not isinstance(item, dict):
                        raise CommandError(
                            f"{label}: record {record_id!r} has a {key} entry that is not an object")
                    for field in fields:
                        if field in item and not _is_str(item[field]):
                            raise CommandError(
                                f"{label}: record {record_id!r} has a {key} with a non-text {field}")
                    if key == "sources":
                        observed = item.get("observed_at")
                        if observed is not None and not _is_str(observed):
                            raise CommandError(
                                f"{label}: record {record_id!r} has a source with a non-text "
                                "observed_at (null travels as null)")

            date = record.get("date")
            # A missing value is the quote; an explicit null is a value.
            value = record["value"] if "value" in record else quote
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
                # The same seam the readers teammate marks: a structured value
                # read back is a projection, never verbatim speech.
                origin["text_kind"] = "projection"
            if record_dropped:
                # Names only: which fields this record lost before it travelled.
                origin["dropped_fields"] = sorted(set(record_dropped))
            row = {
                "locator": f"{SELECTION_NAME}#{record_id}",
                "speaker": role,
                "text": quote,
                "date": date.strip() if _is_str(date) and date.strip() else None,
                "conversation": label,
                "value": value,
                "origin": origin,
            }
            if _is_str(record.get("currency")) and record["currency"]:
                row["currency"] = record["currency"]
            if isinstance(record.get("sources"), list) and record["sources"]:
                row["sources"] = record["sources"]
            if isinstance(record.get("links"), list) and record["links"]:
                row["links"] = record["links"]
            rows.append(row)

        if declared != len(records):
            raise CommandError(
                f"{label}: the manifest counts {declared} record(s) while {SELECTION_NAME} holds "
                f"{len(records)}; a package whose count lies is refused")
        if undecided:
            gaps.append(f"{len(undecided)} record(s) carry no review status: they travel as "
                        f"none, never re-activated: {', '.join(undecided)}")
        if excluded:
            gaps.append(f"{len(excluded)} excluded record(s) were not imported: {', '.join(excluded)}")
        if pending:
            gaps.append(f"{len(pending)} pending record(s) were not imported: {', '.join(pending)}")
        dropped = manifest.get("dropped_fields")
        if isinstance(dropped, list) and dropped:
            gaps.append("known typed credential fields were removed before packaging: "
                        + ", ".join(sorted(set(item for item in dropped if _is_str(item)))))
        if coverage.get("read"):
            gaps.append(f"the selection reports {len(coverage['read'])} source(s) as read: "
                        + ", ".join(coverage["read"]))
        if coverage.get("unavailable"):
            gaps.append(f"the selection could not read {len(coverage['unavailable'])} source(s): "
                        + ", ".join(coverage["unavailable"]))
        # True: the source read was short of its source; False: it said it
        # read all of it; absent: unknown — as in an older selection, whose
        # empty "unavailable" list never proved a complete source. A record
        # this reader did not return is a known gap of its own.
        flag = coverage.get("source_gaps")
        if flag is True or coverage.get("unavailable") or pending or excluded:
            source_gaps = True
        else:
            source_gaps = False if flag is False else None
        if flag is True:
            gaps.append("the sender's read of this selection's source found part of it unavailable, "
                        "left out or not imported; the details stayed with the sender")
        elif flag is None:
            gaps.append("this selection does not record whether its source was read completely; "
                        "its coverage is unknown")
        if manifest.get("decided_by"):
            gaps.append(f"decided_by names who picked ({manifest['decided_by']}), not the receiver's "
                        "consent; applying this selection is the receiver's own judgment")
        return rows, gaps, source_gaps, frozenset(withheld)


_READ_VIA = {PACKAGE_FORMAT: "portable.read_portable", SELECTION_FORMAT: "amberkeep.read_selection"}


def _read_package(path: str):
    """Read a package of either format back as rows, plus its gap lines."""
    data = _read_package_file(path)
    label = Path(path).name
    fmt = _sniff_format(data, label)
    if fmt == PACKAGE_FORMAT:
        rows, gaps = _portable().read_portable(data, label)
    else:
        rows, gaps = _read_selection(data, label)
    return label, fmt, rows, list(gaps or []), len(data)


# ---------------------------------------------------------- record building

def _sources_for_row(row: dict, source: dict) -> list:
    """The record's source chain: what the source record carried, plus this hop.

    The chain carries the source's label, its sha256 and how it was read —
    never a local path: a sha identifies the original, and the person
    verifying it names the file themselves.
    """
    sources = [dict(item) for item in row.get("sources") or [] if isinstance(item, dict)]
    # The next free sN: a chain whose ids are not sequential keeps them unique.
    taken = {item.get("id") for item in sources}
    number = len(sources) + 1
    while f"s{number}" in taken:
        number += 1
    sources.append({
        "id": f"s{number}",
        "locator": row["locator"],
        "observed_at": row.get("date") or "",
        "source_label": source["label"],
        "source_sha256": source["sha256"],
        "read_via": source["read_via"],
    })
    return sources


# The one relation the material gives a link into a record's own source.
SOURCE_RELATIONS = ("derived_from",)


def _confirmed_links(visible: list, chosen: list, new_ids: dict, withheld=frozenset()) -> list:
    """Keep only the links a pick can stand behind; return the dropped targets.

    A target is resolved against every record the source read returned, by
    every name the format gives one — its locator, its bare id, and the
    locators of the chain hops this CLI wrote (an id in an earlier selection
    or the original). One picked record: the link is rewritten to its new id.
    An unpicked record, or a name two records share, stays behind — so an old
    alias never captures a real target. A name no record carries may be one
    of the record's own sources (already in its chain): it travels as it is
    only as a ``derived_from`` link, and only when no record answers to it —
    including the records the read withheld (excluded or pending), which are
    resolved here by name only: a link into one, or a name it shares, stays
    behind whole, and nothing about why travels.
    Everything else travels not at all, basis included. Only the target,
    relation and basis of a kept link travel.
    """
    index: dict = {}
    for row in visible:
        names = {row["locator"]} | {hop["locator"] for hop in _binding_hops(row)}
        names |= {bare for bare in map(_bare_id, list(names)) if bare}
        for name in names:
            index[name] = row["locator"] if index.get(name, row["locator"]) == row["locator"] else None
    for name in withheld:
        index[name] = None
    picked = {row["locator"] for row in chosen}
    dropped_targets = []
    for row in chosen:
        binding = [id(hop) for hop in _binding_hops(row)]
        own_sources = {item.get(key) for item in row.get("sources") or []
                       if isinstance(item, dict) and id(item) not in binding
                       for key in ("id", "locator") if _is_str(item.get(key)) and item.get(key)}
        kept = []
        for link in row.get("links") or []:
            target = link.get("target") if isinstance(link, dict) else None
            if not _is_str(target) or not target:
                dropped_targets.append("(a link without a target)")
                continue
            if target in index:
                resolved = index[target]
                if resolved in picked and target not in own_sources:
                    carried = dict(link, target=new_ids[resolved])
                else:
                    dropped_targets.append(target)
                    continue
            elif target in own_sources and link.get("relation") in SOURCE_RELATIONS:
                carried = dict(link)
            else:
                dropped_targets.append(target)
                continue
            kept.append({key: carried[key] for key in ("target", "relation", "basis")
                         if key in carried})
        row["links"] = kept
    return dropped_targets


def _record_from_row(row: dict, source: dict, record_id: str) -> tuple[dict, list]:
    """One carried record, built straight from the fresh source read, plus the
    names of the fields it lost (the reader's strip and this one's)."""
    dropped: list = []
    seam = row.get("origin") if isinstance(row.get("origin"), dict) else {}
    already = seam.get("dropped_fields")
    if isinstance(already, list):
        dropped.extend(item for item in already if _is_str(item))
    has_value = "value" in row
    value = _strip_credentials(row["value"] if has_value else row["text"], dropped)
    if dropped and value in ({}, []):
        # Only a value a strip emptied (the reader's or this one) is refused;
        # an empty object, list or null the source itself holds travels as it is.
        raise CommandError(
            f"{row['locator']}: holds nothing but credential-shaped fields; "
            "deselect it or drop the field")
    role = row["speaker"]
    original = row.get("provenance") or ""
    if (original in PROVENANCES
            and (role == "assistant") == (original == "model_inference")):
        provenance = original
    else:
        provenance = SPEAKER_TO_PROVENANCE[role]
    record = {
        "id": record_id,
        "kind": row.get("kind") or "carried",
        "title": row.get("title") or "",
        "role": role,
        "provenance": provenance,
        "quote": row["text"],
        "sources": _strip_credentials(_sources_for_row(row, source), dropped),
        "links": _strip_credentials(row.get("links") or [], dropped),
    }
    scope = _stated_scope(row)
    if scope is not None:
        # The source's own scope, an empty one included; a row that states
        # none carries no scope at all — never the package's file name, and
        # never a scope the source did not state.
        record["scope"] = scope
    decision = row.get("decision")
    if _is_str(decision) and decision in CARRY_DECISIONS:
        # The source's own review status, verbatim; never invented here.
        record["decision"] = decision
    if row.get("date"):
        record["date"] = row["date"]
    if row.get("currency"):
        record["currency"] = row["currency"]
    if has_value and not (isinstance(value, str) and value == row["text"]):
        record["value"] = value
    if dropped:
        record["dropped_fields"] = sorted(set(dropped))
    record["content_sha256"] = _selection_digest(record)
    return record, dropped


def _build_selection(records: list, source: dict, decided_by: str, note, dropped: list,
                     label: str) -> bytes:
    """Build the package, then read it back within this CLI's own limits:
    a selection that its own reader would refuse is never written."""
    default_note = ("an Amberkeep selection: exactly the records the owner picked from one named "
                    "source; unselected records do not travel, and decided_by is not the "
                    "receiver's consent")
    selection = {
        "format": SELECTION_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "records": records,
    }
    manifest = {
        "format": SELECTION_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "files": {},
        "record_count": len(records),
        "decided_by": decided_by,
        "dropped_fields": sorted(set(dropped)),
        "note": note or default_note,
        # Generic on purpose: the source's own gap lines stay with the sender,
        # and an unknown coverage is left unsaid rather than turned into a claim.
        "coverage": {"read": [source["label"]]},
    }
    if source["source_gaps"] is not None:
        manifest["coverage"]["source_gaps"] = source["source_gaps"]
    blob = _compact_json(selection).encode("utf-8")
    manifest["files"] = {SELECTION_NAME: {"sha256": _sha256_hex(blob), "bytes": len(blob)}}
    manifest_blob = _compact_json(manifest).encode("utf-8")
    for name, payload in ((MANIFEST_NAME, manifest_blob), (SELECTION_NAME, blob)):
        if len(payload) > MAX_MEMBER_BYTES:
            raise CommandError(
                f"the {name} of this selection would hold {len(payload)} bytes, over the "
                f"{MAX_MEMBER_BYTES}-byte member limit its reader accepts; pick fewer records")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in ((MANIFEST_NAME, manifest_blob), (SELECTION_NAME, blob)):
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    package = buffer.getvalue()
    if len(package) > MAX_PACKAGE_FILE_BYTES:
        raise CommandError(
            f"this selection would be a {len(package)}-byte file, over the "
            f"{MAX_PACKAGE_FILE_BYTES}-byte limit its reader accepts; pick fewer records")
    try:
        _open_selection(package, label)
    except (CommandError, ValueError, RecursionError) as exc:
        raise CommandError(f"the built selection would not read back, so it was not written: "
                           f"{exc}") from exc
    return package


def _rows_document(path: str) -> dict:
    data = _read_file(path, MAX_ROWS_FILE_BYTES, "a rows document",
                      "pick with --record LOCATOR and --receipt instead")
    try:
        doc = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise CommandError(f"{path}: the rows document is not UTF-8 JSON: {exc}") from exc
    except (json.JSONDecodeError, RecursionError) as exc:
        raise CommandError(f"{path}: the rows document is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise CommandError(f"{path}: the rows document is not a JSON object")
    return doc


def _locators_from_rows(path: str) -> tuple[list, dict]:
    """A rows document contributes its locators and its inspect binding.

    No field of a row is trusted or carried: not its text, not its source
    paths, not its metadata. Only the locators name what to pick from the
    source given in the same call; the source binding the inspect recorded
    (label, sha256, parser) is read only to bind this pick to that same read.
    """
    doc = _rows_document(path)
    rows = doc.get("rows")
    if not isinstance(rows, list):
        raise CommandError(f"{path}: the rows document has no rows list")
    locators, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise CommandError(f"{path}: a row of the rows document is not an object")
        locator = row.get("locator")
        if not _is_str(locator) or not locator.strip():
            raise CommandError(f"{path}: a row of the rows document has no locator")
        if locator in seen:
            raise CommandError(f"{path}: the rows document lists locator {locator!r} twice")
        seen.add(locator)
        locators.append(locator)
    sources = doc.get("sources")
    if (not isinstance(sources, list) or not sources
            or not isinstance(sources[0], dict)):
        raise CommandError(f"{path}: the rows document records no source binding from its inspect")
    binding = {}
    for key in ("label", "sha256", "read_via", "receipt"):
        value = sources[0].get(key)
        if _is_str(value) and value:
            binding[key] = value
        elif value is not None:
            raise CommandError(f"{path}: the rows document's source binding has a non-text {key}")
    for key in ("label", "sha256", "read_via"):
        if not binding.get(key):
            raise CommandError(f"{path}: the rows document's source binding records no {key}")
    # The receipt is the one the inspect supplied, never one made up here.
    return locators, binding


def _bind_to_inspect(args, source: dict, table_binding: dict | None) -> None:
    """Inspect → select is one bound step.

    The pick carries the inspect's receipt — a digest over the label, the
    parser and the source sha256 the person confirmed — named with
    ``--receipt``, or recorded by the inspect in the ``--rows`` table. A
    source that moved, or that this call reads under another label or
    parser, is a typed refusal, before anything is built: this catches an
    accidental mismatch, it proves nothing about who made the table.
    ``--expected-sha256`` is an optional extra check of the bytes alone.
    """
    named = args.receipt.strip() if args.receipt else None
    if table_binding is None and named is None:
        raise CommandError(
            "select is bound to an inspect: name the receipt the inspect reported with "
            "--receipt RECEIPT, or pass the --rows ROWS.json table that inspect wrote")
    expected = args.expected_sha256.strip().lower() if args.expected_sha256 else None
    if expected is not None and expected != source["sha256"]:
        raise CommandError(
            f"{args.original}: the source reads sha256 {source['sha256'][:16]}…, not the "
            f"{expected[:16]}… named; the source moved since the inspect — re-inspect and "
            "select from the fresh read")
    if table_binding is not None:
        recorded = table_binding.get("receipt")
        if recorded is None and named is None:
            raise CommandError(
                f"{args.rows}: the rows document records no receipt from its inspect; pass "
                "--receipt RECEIPT as that inspect reported it, or re-inspect with --rows")
        if named is not None and recorded is not None and named != recorded:
            raise CommandError(
                f"the --receipt named is not the receipt of the --rows table "
                f"({_line(recorded, 'the rows document')}); they come from different inspects")
        if table_binding["sha256"] != source["sha256"]:
            raise CommandError(
                f"{args.original}: the source reads sha256 {source['sha256'][:16]}…, not the "
                f"{table_binding['sha256'][:16]}… the inspect recorded; the source moved since "
                "that read — re-inspect and select from the fresh read")
        if table_binding["label"] != source["label"]:
            # The label is part of the read (a reader may build locators from
            # it), so a --rows pick always reads under the inspect's label.
            raise CommandError(
                f"{args.original}: the source now reads as label {source['label']!r}, not the "
                f"{table_binding['label']!r} the inspect recorded; pass --label "
                f"{table_binding['label']!r} as that inspect did, or re-inspect under this name")
        if table_binding["read_via"] != source["read_via"]:
            raise CommandError(
                f"{args.original}: the source reads via {source['read_via']!r}, not the "
                f"{table_binding['read_via']!r} the inspect recorded; pass the same "
                "--kind and re-inspect")
        named = named or recorded
    if named != source["receipt"]:
        raise CommandError(
            f"{args.original}: this read (label {source['label']!r}, via {source['read_via']!r}, "
            f"sha256 {source['sha256'][:16]}…) has receipt {source['receipt']}, not the "
            f"{_line(named, 'the inspect output')} named; the source moved, or this call reads "
            "it under another --label or --kind than that inspect — re-inspect with the same "
            "--label and --kind and select with the receipt it reports")


# ----------------------------------------------------------------- commands

INSPECT_ROUTE = 'inspect SOURCE --rows ROWS.json (or --raw)'
PACKAGE_ROUTE = "read PKG.zip"


def cmd_inspect(args) -> dict:
    """Read exactly one named source (JSON on stdout, bounded by default)."""
    rows, gaps, source, _withheld, notes = _read_source(args.file, args.kind, args.label)
    doc = {
        "kind": ROWS_KIND,
        "schema_version": SCHEMA_VERSION,
        "command": "inspect",
        "source_kind": source["source_kind"],
        "label": source["label"],
        "sha256": source["sha256"],
        "receipt": source["receipt"],
        "bytes": source["bytes"],
        "sources": [dict(source)],
        "rows": rows,
        "gaps": gaps,
        "notes": notes,
    }
    match = [row for row in rows if row["locator"] == args.record] if args.record else []
    if args.record and not match:
        # Refused before anything is written: no half-done inspect.
        available = sorted({row["locator"] for row in rows})
        raise CommandError(f"no row {_line(args.record, 'the command line')!r} in "
                           f"{source['label']}; available: " + _names(available, INSPECT_ROUTE))
    written = None
    if args.rows:
        target = Path(args.rows)
        blob = (_json_text(doc) + "\n").encode("utf-8")
        if len(blob) > MAX_ROWS_FILE_BYTES:
            # select reads a rows document only up to this bound: a document
            # it could not read back is never written.
            raise CommandError(
                f"{target}: the rows document would be {len(blob)} bytes, over the "
                f"{MAX_ROWS_FILE_BYTES}-byte limit select reads; pick with --record LOCATOR "
                "and --receipt instead, or inspect a smaller source")
        _write_output(target, blob)
        written = {"path": str(target), "bytes": len(blob)}
    if args.raw:
        return doc
    if args.record:
        return {"command": "inspect", "mode": "record", "label": source["label"],
                "sha256": source["sha256"], "receipt": source["receipt"],
                "source_kind": source["source_kind"],
                "row": match[0], "rows_document": written,
                "gaps": [_line(gap, INSPECT_ROUTE) for gap in gaps[:GAPS_CAP]],
                "gaps_total": len(gaps), "gaps_truncated": len(gaps) > GAPS_CAP,
                "notes": [_line(note, INSPECT_ROUTE) for note in notes[:GAPS_CAP]],
                "notes_total": len(notes), "notes_truncated": len(notes) > GAPS_CAP}

    limit = max(1, min(PAGE_MAX, args.limit if args.limit else PAGE_DEFAULT))
    page = max(1, args.page)
    offset = (page - 1) * limit
    page_rows = [{
        "locator": _line(row["locator"], INSPECT_ROUTE), "speaker": row["speaker"],
        "date": _snippet(row["date"]) if row.get("date") else row.get("date"),
        "scope": _snippet(str(row.get("scope") or "")),
        "text": _snippet(row["text"]),
    } for row in rows[offset:offset + limit]]
    locators = [row["locator"] for row in rows]
    groups = _groups_of(rows)
    return {
        "command": "inspect", "mode": "inventory", "kind": ROWS_KIND,
        "schema_version": SCHEMA_VERSION,
        "source_kind": source["source_kind"], "label": source["label"],
        "sha256": source["sha256"], "receipt": source["receipt"], "bytes": source["bytes"],
        "row_count": len(rows),
        "groups": groups[:GROUPS_CAP],
        "groups_total": len(groups),
        "groups_truncated": len(groups) > GROUPS_CAP,
        "locators": [_line(locator, INSPECT_ROUTE) for locator in locators[:LOCATOR_INDEX_CAP]],
        "locators_total": len(locators),
        "locators_truncated": len(locators) > LOCATOR_INDEX_CAP,
        "page": {"page": page, "limit": limit, "offset": offset,
                 "rows": page_rows, "total": len(rows)},
        "rows_document": written,
        "gaps": [_line(gap, INSPECT_ROUTE) for gap in gaps[:GAPS_CAP]],
        "gaps_total": len(gaps),
        "gaps_truncated": len(gaps) > GAPS_CAP,
        # The reader's informational notes, apart from the gaps: never a loss.
        "notes": [_line(note, INSPECT_ROUTE) for note in notes[:GAPS_CAP]],
        "notes_total": len(notes),
        "notes_truncated": len(notes) > GAPS_CAP,
        "hints": {
            "rows": "inspect SOURCE --rows ROWS.json   (the full rows document, create-only)",
            "record": "inspect SOURCE --record LOCATOR   (one row, full text)",
            "raw": "inspect SOURCE --raw   (the full rows document on stdout)",
            "select": (f"select OUT.zip --original SOURCE --receipt {source['receipt']} "
                       "--record LOCATOR [...]   (or --all; the same --kind and --label as "
                       "this inspect)"),
        },
    }


def cmd_select(args) -> dict:
    """Build a package of exactly the records the owner picked, bound to
    the inspect that read them."""
    # Exactly one way of picking, settled before anything is read.
    modes = [name for name, given in (("--record", bool(args.record)), ("--rows", bool(args.rows)),
                                      ("--all", bool(args.all))) if given]
    if len(modes) > 1:
        raise CommandError(f"choose one way of picking, not both: {' and '.join(modes)}; "
                           "--record, --rows and --all are mutually exclusive")
    if not modes:
        raise CommandError("nothing was selected: pass --record LOCATOR (repeat per record), "
                           "--rows ROWS.json (a locator list), or --all")
    if not args.rows and not args.receipt:
        raise CommandError(
            "select is bound to an inspect: name the receipt the inspect reported with "
            "--receipt RECEIPT, or pass the --rows ROWS.json table that inspect wrote")
    table_binding = None
    wanted: list = list(args.record)
    if args.rows:
        wanted, table_binding = _locators_from_rows(args.rows)
        if not wanted:
            raise CommandError("nothing was selected: the --rows table lists no locator")
    if not Path(args.original).is_file():
        raise CommandError(f"{args.original}: no such file; select reads the source you name")
    rows, source_gap_lines, source, withheld, notes = _read_source(
        args.original, args.kind, args.label)
    _bind_to_inspect(args, source, table_binding)
    by_locator: dict = {}
    for row in rows:
        if row["locator"] in by_locator:
            raise CommandError(f"the source lists locator {_line(row['locator'], INSPECT_ROUTE)!r} "
                               "twice")
        by_locator[row["locator"]] = row
    if args.all:
        chosen = list(rows)
        if not chosen:
            raise CommandError("nothing was selected: the source read returned no row")
    else:
        missing = [locator for locator in wanted if locator not in by_locator]
        if missing:
            raise CommandError("no such locators: " + _names(missing, "the command line")
                               + "; pick from the source you named")
        chosen, seen = [], set()
        for locator in wanted:
            if locator not in seen:
                seen.add(locator)
                chosen.append(by_locator[locator])
    unknown = [row["locator"] for row in chosen if row["speaker"] not in SPEAKER_TO_PROVENANCE]
    if unknown:
        raise CommandError("cannot honestly assign provenance to: " + _names(unknown, INSPECT_ROUTE)
                           + " (unknown speaker)")
    # Only a row that stated no value and has no text carries nothing; an
    # explicit empty string value is content, not emptiness.
    empty = [row["locator"] for row in chosen
             if not str(row["text"]).strip() and "value" not in row]
    if empty:
        raise CommandError("refusing to carry empty text: " + _names(empty, INSPECT_ROUTE))

    chosen_locators = {row["locator"] for row in chosen}
    # A record re-carried from an Amberkeep selection keeps the id it
    # already has, whatever else is picked; a record from any other source
    # gets the stable id of its locator and text.
    if source["read_via"] == "amberkeep.read_selection":
        new_ids = {row["locator"]: _bare_id(row["locator"]) for row in chosen}
    else:
        new_ids = {row["locator"]: _stable_id(row["locator"], row["text"]) for row in chosen}
    dropped_links = sorted(set(_confirmed_links(rows, chosen, new_ids, withheld)))

    dropped: list = []
    # The pick keeps the source's own row order — never the order the
    # locators were named in, and never a scope/date/locator re-sort: equal
    # or missing dates must not scramble the sequence of messages.
    order = {row["locator"]: index for index, row in enumerate(rows)}
    chosen.sort(key=lambda row: order[row["locator"]])
    records = []
    for row in chosen:
        record, record_dropped = _record_from_row(row, source, new_ids[row["locator"]])
        records.append(record)
        dropped.extend(record_dropped)
    out = Path(args.package)
    blob = _build_selection(records, source, args.decided_by, args.note, dropped, out.name)
    _write_output(out, blob)
    excluded = sorted(set(by_locator) - chosen_locators)

    def _summary_of(record: dict) -> dict:
        summary = {"id": record["id"], "kind": record["kind"],
                   "title": _line(record["title"], PACKAGE_ROUTE),
                   "scope": _line(record.get("scope", ""), PACKAGE_ROUTE),
                   "role": record["role"],
                   "provenance": record["provenance"],
                   "first_locator": _line(_first_locator(record), PACKAGE_ROUTE),
                   "version_sha256": _version_of_record(record)}
        for key in ("decision", "dropped_fields"):
            if key in record:
                summary[key] = record[key]
        return summary

    excluded_sample = _sample(excluded, INSPECT_ROUTE, LOCATOR_INDEX_CAP)
    links_sample = _sample(dropped_links, INSPECT_ROUTE + " — the picked rows' links")
    gap_sample = _sample(source_gap_lines, INSPECT_ROUTE)
    note_sample = _sample(notes, INSPECT_ROUTE)
    return {
        "command": "select",
        "package": str(out),
        "format": SELECTION_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "receipt": source["receipt"],
        "bytes": len(blob),
        "record_count": len(records),
        "records": [_summary_of(record) for record in records[:LOCATOR_INDEX_CAP]],
        "records_truncated": len(records) > LOCATOR_INDEX_CAP,
        # The pick's footprint and the source's own gap lines stay here, on
        # the owner's side; the package carries only the source_gaps flag.
        # Each list is a bounded sample with its true total.
        "coverage": {"read": [source["label"]],
                     "source_gaps": source["source_gaps"],
                     "source_gap_lines": gap_sample["sample"],
                     "source_gap_lines_total": gap_sample["total"],
                     "source_gap_lines_truncated": gap_sample["truncated"],
                     # The reader's informational notes: never a loss.
                     "source_notes": note_sample["sample"],
                     "source_notes_total": note_sample["total"],
                     "source_notes_truncated": note_sample["truncated"],
                     "excluded_locators": excluded_sample["sample"],
                     "excluded_count": excluded_sample["total"],
                     "excluded_truncated": excluded_sample["truncated"],
                     "dropped_links": links_sample["sample"],
                     "dropped_link_count": links_sample["total"],
                     "dropped_links_truncated": links_sample["truncated"],
                     "full": INSPECT_ROUTE},
        "dropped_fields": sorted(set(dropped)),
        "note": ("only these explicitly picked records travel, built straight from the named "
                 "source; show the coverage, the source's gap lines and the package size to "
                 "the owner — the package itself says only that the source read had gaps"),
    }


def cmd_read(args) -> dict:
    """Read a package back as rows (and its gap lines, unchanged)."""
    label, fmt, rows, gaps, _size = _read_package(args.package)
    if args.record:
        rows = [_record_row(label, rows, args.record)]
    # The explicit full read: every row and gap line, plus each item's
    # identity and version, to reconcile against what is already saved.
    items = [{"record": _record_id(row), "first_locator": _first_locator(row),
              "quote_sha256": _sha256_hex(str(row.get("text", "")).encode("utf-8")),
              "version_sha256": _version_of_row(row, _READ_VIA[fmt], label)} for row in rows]
    return {"command": "read", "package": label, "rows": rows, "items": items, "gaps": gaps}


def _record_row(label: str, rows: list, record: str) -> dict:
    """The one row a record id names, or a refusal that lists the real ids."""
    match = [row for row in rows if _record_id(row) == record]
    if not match:
        available = sorted({_record_id(row) for row in rows})
        raise CommandError(f"no record {_line(record, 'the command line')!r} in {label}; "
                           "available: " + _names(available, PACKAGE_ROUTE))
    return match[0]


def cmd_quote(args) -> dict:
    """One reproducible binding: verbatim text, its sha256, role, date, context.

    Without --original this is evidence carried inside the package, nothing
    more. With --original PATH the quote is checked against that one named
    file, through the one chain hop that must bind sha256 + parser +
    locator; text_match says the text alone, verified says the whole
    binding held.
    """
    label, fmt, rows, _gaps, _size = _read_package(args.package)
    row = _record_row(label, rows, args.record)
    context = max(0, min(10, args.context))
    origin = row.get("origin") if isinstance(row.get("origin"), dict) else {}
    scope = origin.get("scope", "")
    text_kind = origin.get("text_kind") if _is_str(origin.get("text_kind")) else "verbatim"
    # The window is built strictly around the chosen record: its own scope
    # group — a missing scope is a group of its own, never the empty one,
    # so unrelated conversations never mix — in the package's own row
    # order, never re-sorted by date or locator: equal or missing dates
    # must not scramble the sequence of messages.
    group = _stated_scope(row)
    ordered = [other for other in rows if _stated_scope(other) == group]
    position = ordered.index(row) if row in ordered else len(ordered)
    window = ordered[max(0, position - context):position + context + 1]
    neighbours = [{"record": _record_id(other), "locator": other.get("locator"),
                   "date": other.get("date")}
                  for other in window if other is not row]
    quote = str(row.get("text", ""))
    result = {
        "command": "quote",
        "package": label,
        "record": args.record,
        "locator": row.get("locator"),
        "quote": quote,
        "quote_sha256": _sha256_hex(quote.encode("utf-8")),
        # Identity and version: the same first_locator with another version
        # is a changed item (text, value, role, provenance, decision,
        # currency) — reconcile it, never reuse an earlier verdict blindly.
        "first_locator": _first_locator(row),
        "version_sha256": _version_of_row(row, _READ_VIA[fmt], label),
        "decision": origin.get("decision"),
        "currency": row.get("currency"),
        "speaker": row.get("speaker"),
        "provenance": origin.get("provenance", ""),
        "text_kind": text_kind,
        "role_note": ("a sha256 binds this text to this record; it never proves who said it "
                      "or that it is true"),
        "date": row.get("date"),
        "scope": scope,
        "sources": row.get("sources", []),
        "neighbours": neighbours,
        "chain": {
            "carried": "this package carries the record; the original source file was not carried",
            "late_application": ("a package is a snapshot, not the present; re-read the actual "
                                 "current revision before applying late"),
        },
    }
    if text_kind == "projection":
        result["value"] = row.get("value")
        result["text_note"] = (
            "this text is the reader's own re-serialization of a structured value; a matching "
            "check shows the structure round-trips, never that this exact wording was spoken")
    if not args.original:
        result["verification"] = None
        result["evidence"] = ("carried inside the package; not checked against any original: "
                               "none was named, and none was searched for")
    else:
        verification = _verify_quote_against(row, quote, text_kind, args.original)
        result["verification"] = verification
        if verification["verified"] and text_kind == "projection":
            result["evidence"] = ("a verified projection: one chain hop binds sha256 + parser + "
                                  "locator, and the named original's structured value, role and "
                                  "date round-trip to this record; the text is a re-serialization, "
                                  "not a fragment of the original's wording")
        elif verification["verified"]:
            result["evidence"] = ("a verified fragment of the named original: one chain hop "
                                  "binds sha256 + parser + locator, and text, role and date match")
        else:
            result["evidence"] = ("carried inside the package; the check against the named "
                                  f"original failed: {verification['reason']}")
    return result


def _text_kind_of(row: dict) -> str:
    """Verbatim text, or a projection of a structured value (null included)."""
    origin = row.get("origin") if isinstance(row.get("origin"), dict) else {}
    if origin.get("text_kind") == "projection":
        return "projection"
    if "value" in row and not isinstance(row["value"], str):
        return "projection"
    return "verbatim"


def _verify_quote_against(row: dict, quote: str, text_kind: str, original_path: str) -> dict:
    """Check one quote against one named original — the file the person chose.

    One chain hop must bind sha256 + parser + locator; then text, role and
    date are compared one by one. A projection matches only a projection,
    compared as structure (``value_match``); verbatim text only verbatim
    text. Nothing is searched for: only this named file is opened, and every
    check is reported.
    """
    binding = _binding_hops(row)
    # The original is read by the same route, and under the same bound, as
    # the source it once was: a plain export at its reader's limit, an export
    # ZIP at the archive limit, a package at the package limit.
    kinds = [hop["read_via"][len(READERS_VIA):] for hop in binding
             if hop["read_via"].startswith(READERS_VIA)]
    data = _read_source_file(original_path, kinds[0] if kinds else None)
    label = Path(original_path).name
    original_sha = _sha256_hex(data)
    report = {
        "original": str(Path(original_path)),
        "original_sha256": original_sha,
        "role": row.get("speaker"),
        "text_kind": text_kind,
        "text_match": None,
        "value_match": None,
        "role_match": None,
        "date_match": None,
        "verified": False,
        "reason": "",
    }
    if not binding:
        report["reason"] = ("no chain hop binds an original (sha256 + parser + locator); "
                            "nothing to check against, and none was searched for")
        return report
    for hop in binding:
        if hop["source_sha256"] != original_sha:
            continue
        report["read_via"] = hop["read_via"]
        report["locator_in_original"] = hop["locator"]
        chain_label = hop.get("source_label") if _is_str(hop.get("source_label")) else None
        if chain_label:
            # The chain's own label re-reads the original, so a renamed copy
            # of the same bytes still verifies and label-built locators resolve.
            report["label_from_chain"] = chain_label
        try:
            again = _reread_original(data, chain_label or label, hop["read_via"])
        except (CommandError, ValueError, zipfile.BadZipFile, RecursionError) as exc:
            report["reason"] = f"the named original cannot be re-read: {exc}"
            return report
        match = [other for other in again if other.get("locator") == hop["locator"]]
        if not match:
            report["reason"] = (f"the chain's locator {hop['locator']!r} is not in the named original")
            return report
        original = match[0]
        original_kind = _text_kind_of(original)
        same_kind = original_kind == text_kind
        report.update({
            "role_in_original": original.get("speaker"),
            "text_kind_in_original": original_kind,
            # The same characters in another form are not the same quote.
            "text_match": same_kind and original.get("text") == quote,
            "role_match": original.get("speaker") == row.get("speaker"),
            "date_match": (original.get("date") or "") == (row.get("date") or ""),
        })
        if text_kind == "projection":
            # Compared as structure, with the same credential strip the
            # carried value went through: an empty object, a list or null
            # stays itself on both sides.
            report["value_match"] = same_kind and (
                _strip_credentials(original.get("value", original.get("text")), [])
                == row.get("value"))
        if (report["text_match"] and report["role_match"] and report["date_match"]
                and report["value_match"] is not False):
            report["verified"] = True
            report["reason"] = ("one chain hop binds sha256 + parser + locator, and text, role "
                                "and date match the named original; still not who said it")
        else:
            report["reason"] = ("the chain hop binds the named original, but its text, form, "
                                "value, role or date do not fully match it")
        return report
    report["reason"] = ("the named file's sha256 is not the original any chain hop records; "
                        "it is a different file")
    return report


def cmd_portrait(args) -> dict:
    """A quick portrait: groups, counts, dates, one bounded example each."""
    label, _fmt, rows, gaps, _size = _read_package(args.package)
    all_dates = sorted({row.get("date") for row in rows if row.get("date")})
    groups = _groups_of(rows)
    return {
        "command": "portrait",
        "package": label,
        "record_count": len(rows),
        "first_date": all_dates[0] if all_dates else "",
        "last_date": all_dates[-1] if all_dates else "",
        "groups": groups[:GROUPS_CAP],
        "groups_total": len(groups),
        "groups_truncated": len(groups) > GROUPS_CAP,
        "gaps": [_line(gap, PACKAGE_ROUTE) for gap in gaps[:GAPS_CAP]],
        "gaps_total": len(gaps),
        "gaps_truncated": len(gaps) > GAPS_CAP,
    }


def cmd_verify(args) -> dict:
    """Check a package: a refusal with a non-zero exit when it is damaged.

    A damaged or unknown package is never a report with "ok", and a missing
    teammate is a refusal, never a verdict about a package.
    """
    label, fmt, rows, gaps, size = _read_package(args.package)
    ids = _sample(sorted(_record_id(row) for row in rows), PACKAGE_ROUTE, LOCATOR_INDEX_CAP)
    lines = _sample(gaps, PACKAGE_ROUTE)
    return {
        "command": "verify",
        "ok": True,
        "package": label,
        "format": fmt,
        "bytes": size,
        "record_count": len(rows),
        # Bounded samples with their true totals; `read` gives everything.
        "records": ids["sample"],
        "records_truncated": ids["truncated"],
        "gaps": lines["sample"],
        "gaps_total": lines["total"],
        "gaps_truncated": lines["truncated"],
        "full": PACKAGE_ROUTE,
    }


# --------------------------------------------------------------------- main

COMMANDS = {
    "inspect": cmd_inspect,
    "select": cmd_select,
    "read": cmd_read,
    "quote": cmd_quote,
    "portrait": cmd_portrait,
    "verify": cmd_verify,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="amberkeep.py",
        description="Carry chosen memory from one AI assistant to another.")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_p = sub.add_parser("inspect", help="read one named source into rows")
    inspect_p.add_argument("file")
    inspect_p.add_argument("--kind", default=None,
                           help="source kind; readers.read_source decides which it supports")
    inspect_p.add_argument("--label", default=None,
                           help="the label rows carry; defaults to the file name")
    inspect_p.add_argument("--rows", default=None, metavar="ROWS.json",
                           help="write the full rows document here (create-only)")
    inspect_p.add_argument("--raw", action="store_true",
                           help="print the full rows document to stdout anyway")
    inspect_p.add_argument("--record", default=None, metavar="LOCATOR",
                           help="print one row, full text")
    inspect_p.add_argument("--page", type=int, default=1, help="page of the bounded preview (default 1)")
    inspect_p.add_argument("--limit", type=int, default=None,
                           help=f"rows per page (default {PAGE_DEFAULT}, at most {PAGE_MAX})")

    select_p = sub.add_parser("select", help="build a package of only the records the owner picked")
    select_p.add_argument("package", help="the .zip to write (create-only)")
    select_p.add_argument("--original", required=True, metavar="SOURCE",
                          help="the source file to build from, named in this same call")
    select_p.add_argument("--kind", default=None,
                          help="source kind; readers.read_source decides which it supports")
    select_p.add_argument("--label", default=None,
                          help="the label rows carry; defaults to the file name. With --rows it "
                               "must be the label that inspect recorded")
    select_p.add_argument("--receipt", default=None, metavar="RECEIPT",
                          help="the receipt the inspect reported (label + parser + sha256); "
                               "the pick is bound to it. Not needed with --rows")
    select_p.add_argument("--expected-sha256", default=None, metavar="SHA256",
                          help="optional extra check: the source sha256 the inspect reported")
    select_p.add_argument("--record", action="append", default=[], metavar="LOCATOR",
                          help="a locator to carry; repeat per record. Not with --rows or --all")
    select_p.add_argument("--rows", default=None, metavar="ROWS.json",
                          help="a rows document from inspect: its locators, plus the binding of "
                               "that read. Not with --record or --all")
    select_p.add_argument("--all", action="store_true",
                          help="carry every row of the source. Not with --record or --rows")
    select_p.add_argument("--decided-by", default="the owner", help="who picked the records")
    select_p.add_argument("--note", default=None, help="a note carried in the manifest")

    read_p = sub.add_parser("read", help="read a package back as rows")
    read_p.add_argument("package")
    read_p.add_argument("--record", default=None, help="only this record id")

    quote_p = sub.add_parser("quote", help="one reproducible binding for one record")
    quote_p.add_argument("package")
    quote_p.add_argument("--record", required=True, help="the record id to bind")
    quote_p.add_argument("--context", type=int, default=2, help="neighbours on each side (0-10)")
    quote_p.add_argument("--original", default=None, metavar="PATH",
                         help="check the quote against this one named original file")

    portrait_p = sub.add_parser("portrait", help="a quick portrait of a package")
    portrait_p.add_argument("package")

    verify_p = sub.add_parser("verify", help="check a package and report")
    verify_p.add_argument("package")

    args = parser.parse_args(argv)
    try:
        document = COMMANDS[args.command](args)
    except (CommandError, ValueError, zipfile.BadZipFile, zlib.error, EOFError,
            RecursionError) as exc:
        print(_json_text({"refused": str(exc), "command": args.command}))
        return 1
    except OSError as exc:
        print(_json_text({"refused": f"{args.command}: {exc}", "command": args.command}))
        return 1
    print(_json_text(document))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())