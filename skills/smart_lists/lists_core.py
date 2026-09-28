"""Authoritative skill-local store and list operations for smart_lists.

This module has no host imports so it can be exercised directly. One JSON
document under the skill state directory holds every group, entry and applied
request id. Writers are serialized (an in-process lock plus a cross-process
file lock: ``fcntl`` on POSIX, ``msvcrt`` on Windows, waited for at most
``LOCK_TIMEOUT_SEC``), reload the document from disk, and replace it
atomically, so agent tools and widget routes share one source of truth and
never observe a partial file.

Policies the owner chose, enforced here rather than in callers:

* entry text is stored exactly as given (no trimming, casing or merging);
* repeated text is never deduplicated silently, only reported;
* a due value is read as an instant only when it is an ISO-8601 date-time with
  an explicit UTC offset, otherwise it is kept as raw text and not interpreted;
* a mutation that carries a ``request_id`` is applied at most once: the last
  ``MAX_REQUESTS`` ids keep a replay record, older ones are folded into a
  fixed-size filter that never forgets an id (it may, rarely, refuse a new id
  as already used, which applies nothing);
* groups form one tree where every group has at most one parent;
* a document is accepted (loaded or restored) only if it is one this module
  could have written: every field, length, id, sibling name and parent link
  is checked, so a corrupt export is refused instead of restored.

Lifecycle rules, so lists never vanish, reappear empty or double silently:

* the store is created only by an explicit ``init`` or ``restore``; without it
  every operation refuses with ``store_not_initialized`` instead of showing an
  empty list;
* a small sentinel next to it (``store_meta.json``, no list content) records the
  last write, so a later missing ``store.json`` refuses with ``store_missing``
  and an older or foreign copy put in its place with ``store_mismatch``; the
  sentinel also names the exact file a write in flight replaces, so a failed
  or interrupted write leaves the previous store usable, not a false mismatch;
* ``init`` and ``restore`` never replace an existing ``store.json`` unless told
  to, and then copy it byte-for-byte into ``backups/`` first;
* deleting an entry moves it to a trash that can be undone; only ``erase``
  removes it for good, and only once it is deleted.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

try:  # POSIX cross-process lock.
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]
try:  # Windows cross-process lock.
    import msvcrt
except ImportError:  # pragma: no cover - platform dependent
    msvcrt = None  # type: ignore[assignment]

SCHEMA_VERSION = 3
STORE_FILE = "store.json"
LOCK_FILE = "store.lock"
META_FILE = "store_meta.json"
BACKUP_DIR = "backups"
EXPORT_FORMAT = "smart_lists.export"
EXPORT_FORMAT_VERSION = 1

MAX_GROUPS = 500
MAX_ENTRIES = 5000
MAX_BATCH = 100
MAX_TEXT_CHARS = 1000
MAX_NAME_CHARS = 80
MAX_DUE_CHARS = 120
MAX_REQUESTS = 500
MAX_REQUESTS_ACCEPTED = 5000  # validation bound; saves trim to MAX_REQUESTS
MAX_READ_LIMIT = 500
MAX_DUPLICATE_IDS = 5
MAX_IMPORT_BYTES = 64 * 1024 * 1024
MAX_LISTED_BACKUPS = 20
MAX_STAMP_CHARS = 64
MAX_SOURCE_CHARS = 32
MAX_OP_CHARS = 32
MAX_RESULT_DEPTH = 8
MAX_LEGACY_EXPIRED = 200000
MAX_COUNTER = 2 ** 53
LOCK_TIMEOUT_SEC = 10.0

# Request ids evicted from the replay journal: a Bloom filter over the 64-bit
# request digest. It never forgets an id; the chance that a new id is refused
# as already used grows with the number of retired ids (about 0.015% at
# 50,000, 0.65% at 100,000, 4% at 150,000) and is reported by store status.
BLOOM_BITS = 1 << 20
BLOOM_HASHES = 7
_BLOOM_BYTES = BLOOM_BITS // 8

STATUSES = ("open", "done")
GROUP_ACTIONS = ("create", "rename", "move", "delete")
ENTRY_ACTIONS = ("delete", "undo", "erase")
STORE_ACTIONS = ("status", "init", "export", "restore")

_GROUP_ID_RE = re.compile(r"^g_[0-9a-f]{10}$")
_ENTRY_ID_RE = re.compile(r"^e_[0-9a-f]{10}$")
_STORE_ID_RE = re.compile(r"^s_[0-9a-f]{16}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EXPLICIT_OFFSET_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{3}(?:\d{3})?)?)?(?:Z|[+-]\d{2}:\d{2})$"
)
_ITEM_KEYS = {"text", "due"}
_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DOC_KEYS = {"schema_version", "store_id", "generation", "next_seq", "groups", "entries", "requests", "updated_at"}
_DOC_KEYS_BY_VERSION = {1: _DOC_KEYS | {"expired_requests"}, 2: _DOC_KEYS | {"expired_requests"},
                        3: _DOC_KEYS | {"retired_requests"}}
_GROUP_KEYS = {"id", "name", "parent_id", "created_at", "updated_at"}
_ENTRY_KEYS = {"id", "seq", "group_id", "text", "status", "due", "source", "request_id", "created_at",
               "updated_at", "completed_at", "deleted_at"}
_REQUEST_KEYS = {"op", "fingerprint", "at", "result"}
_COUNT_KEYS = ("groups", "entries", "open", "done", "deleted")
_ACTION_RESULT_KEYS = {"delete": "deleted", "undo": "undeleted", "erase": "erased"}
_STATE_BY_CODE = {
    "store_not_initialized": "uninitialized",
    "store_missing": "missing",
    "store_unreadable": "unreadable",
    "store_mismatch": "mismatch",
}


class ListError(Exception):
    """A typed, owner-readable refusal. Nothing is written when it is raised."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}


class _Malformed(ValueError):
    """A store document failed validation; the caller picks the public error code."""


def _invalid(message: str) -> ListError:
    return ListError("invalid_input", message)


def _utf8(value: str, field: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _invalid(f"{field} must be valid Unicode text") from exc
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unreadable(detail: str) -> ListError:
    return ListError(
        "store_unreadable",
        f"The list store could not be read ({detail}). It was left untouched; "
        "no change was applied.",
    )


def _meta_summary(meta: Dict[str, Any]) -> str:
    counts = meta.get("counts") if isinstance(meta.get("counts"), dict) else {}
    parts = []
    if isinstance(meta.get("written_at"), str) and meta["written_at"]:
        parts.append(f"last written {meta['written_at']}")
    if _is_int(counts.get("groups")) and _is_int(counts.get("entries")):
        parts.append(f"{counts['groups']} groups, {counts['entries']} entries")
    return f" ({'; '.join(parts)})" if parts else ""


def _not_initialized() -> ListError:
    return ListError(
        "store_not_initialized",
        "No Smart Lists store exists in this installation yet, so there is nothing to read or change. "
        "If the owner kept lists here before (for example before a reinstall), restore their export "
        "with the store tool (action=restore); otherwise start an empty store with action=init once "
        "the owner confirms. Nothing was written.",
    )


def _missing(meta: Dict[str, Any]) -> ListError:
    return ListError(
        "store_missing",
        f"The list store file is missing although this installation saved one before{_meta_summary(meta)}. "
        "No empty list was created in its place and nothing was written. Restore an export with the "
        "store tool (action=restore), or start over with action=init and replace=true.",
    )


def _mismatch(doc: Dict[str, Any], meta: Dict[str, Any]) -> ListError:
    return ListError(
        "store_mismatch",
        f"The list store file is not the one this installation last saved: it holds generation "
        f"{doc['generation']} of store {doc.get('store_id') or '(unnamed)'}, the last save was generation "
        f"{meta['generation']} of store {meta['store_id']}. It may be an older or foreign copy; it was left "
        "untouched and no change was applied. Restore the copy you want with the store tool "
        "(action=restore, replace=true); the current file is backed up first.",
    )


def export_unrecorded_warning(failure: Dict[str, str]) -> str:
    return (f"The export made at {failure['at']} is complete and usable, but recording it in store_meta.json "
            f"failed ({failure['error']}), so the last export shown in status does not include it. The lists "
            "were not changed.")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def empty_document() -> Dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "store_id": None, "generation": 0, "next_seq": 1,
            "groups": {}, "entries": {}, "requests": {}, "retired_requests": None}


# ---------------------------------------------------------------------------
# Input cleaning (pure; runs before any lock is taken)
# ---------------------------------------------------------------------------

def clean_request_id(value: Any) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _REQUEST_ID_RE.match(value):
        raise _invalid(
            "request_id must be 1-128 characters of letters, digits, '.', '_', ':' or '-' "
            "and start with a letter or digit"
        )
    return value


def clean_text(value: Any, *, field: str = "text") -> str:
    """Validate entry text and return it unchanged (raw)."""
    if not isinstance(value, str):
        raise _invalid(f"{field} must be text")
    if not value.strip():
        raise _invalid(f"{field} must not be blank")
    if len(value) > MAX_TEXT_CHARS:
        raise _invalid(f"{field} is longer than {MAX_TEXT_CHARS} characters")
    return _utf8(value, field)


def parse_due(value: Any) -> Optional[Dict[str, str]]:
    """Return ``{"raw", "at"?}``; ``at`` only for an explicit-offset date-time."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid("due must be text")
    if not value.strip():
        return None
    if len(value) > MAX_DUE_CHARS:
        raise _invalid(f"due is longer than {MAX_DUE_CHARS} characters")
    _utf8(value, "due")
    due: Dict[str, str] = {"raw": value}
    candidate = value.strip()
    if _EXPLICIT_OFFSET_RE.match(candidate):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            due["at"] = parsed.isoformat()
    return due


def clean_name(value: Any) -> str:
    if not isinstance(value, str):
        raise _invalid("name must be text")
    name = " ".join(value.split())
    if not name:
        raise _invalid("name is required")
    if "/" in name:
        raise _invalid("group names cannot contain '/', which separates path segments")
    if len(name) > MAX_NAME_CHARS:
        raise _invalid(f"name is longer than {MAX_NAME_CHARS} characters")
    if _GROUP_ID_RE.match(name):
        raise _invalid("name must not look like a group id")
    return _utf8(name, "name")


def clean_items(value: Any) -> List[Tuple[str, Optional[Dict[str, str]]]]:
    if not isinstance(value, list) or not value:
        raise _invalid("items must be a non-empty list of {text, due?} objects")
    if len(value) > MAX_BATCH:
        raise _invalid(f"at most {MAX_BATCH} items per call")
    cleaned = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            raise _invalid(f"items[{index}] must be an object with text")
        unknown = sorted(set(item) - _ITEM_KEYS)
        if unknown:
            raise _invalid(
                f"items[{index}] has unsupported fields {unknown}; keep quantities and notes inside text"
            )
        cleaned.append((clean_text(item.get("text"), field=f"items[{index}].text"), parse_due(item.get("due"))))
    return cleaned


def clean_entry_ids(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise _invalid("entry_ids must be a non-empty list of entry ids (e_...)")
    if len(value) > MAX_BATCH:
        raise _invalid(f"at most {MAX_BATCH} entry ids per call")
    ids: List[str] = []
    for item in value:
        if not isinstance(item, str) or not _ENTRY_ID_RE.match(item.strip()):
            raise _invalid(f"{item!r} is not an entry id (e_ followed by 10 hex characters)")
        if item.strip() not in ids:
            ids.append(item.strip())
    return ids


def _clean_flag(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid(f"{field} must be true or false")
    return value


def _ref_text(value: Any, *, field: str = "group") -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{field} is required: a group id (g_...) or a path such as 'Home / Groceries'")
    return _utf8(value.strip(), field)


def _optional_ref(value: Any, *, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _invalid(f"{field} must be text")
    return _utf8(value.strip(), field)


def _ref_key(ref: str) -> str:
    """Canonical spelling of a group reference, so a retry that writes the same
    path differently ('home/groceries' vs 'Home / Groceries') is still a replay."""
    if not ref or _GROUP_ID_RE.match(ref):
        return ref
    return "/".join(_norm(part) for part in ref.split("/") if part.strip())


def _fingerprint(op: str, params: Dict[str, Any]) -> str:
    canonical = json.dumps({"op": op, "params": params}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _digest(document: Any) -> str:
    """Checksum of a store document, independent of key order and indentation."""
    canonical = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_digest(request_id: str) -> str:
    return hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Retired request ids (fixed-size filter; see BLOOM_BITS)
# ---------------------------------------------------------------------------

def _bloom_positions(digest: str) -> List[int]:
    # Double hashing over the 64-bit request digest, the only identity schema
    # version 2 kept for evicted ids, so those fold in without loss.
    value = int(digest, 16)
    first, step = value >> 32, (value & 0xFFFFFFFF) | 1
    return [(first + index * step) % BLOOM_BITS for index in range(BLOOM_HASHES)]


def _bloom_add(bits: bytearray, digest: str) -> None:
    for position in _bloom_positions(digest):
        bits[position >> 3] |= 1 << (position & 7)


def _bloom_has(bits: bytes, digest: str) -> bool:
    return all(bits[position >> 3] & (1 << (position & 7)) for position in _bloom_positions(digest))


def _bloom_decode(field: Any) -> bytearray:
    """The filter's bits; raises _Malformed for anything this module did not write."""
    if field is None:
        return bytearray(_BLOOM_BYTES)
    if (
        not isinstance(field, dict)
        or set(field) != {"bits", "hashes", "data"}
        or not _is_int(field["bits"]) or field["bits"] != BLOOM_BITS
        or not _is_int(field["hashes"]) or field["hashes"] != BLOOM_HASHES
        or not isinstance(field["data"], str)
    ):
        raise _Malformed("retired_requests is malformed")
    try:
        packed = base64.b64decode(field["data"].encode("ascii"), validate=True)
        inflater = zlib.decompressobj()
        bits = inflater.decompress(packed, _BLOOM_BYTES + 1)  # bounded: no decompression bomb
    except (ValueError, UnicodeEncodeError, zlib.error) as exc:
        raise _Malformed("retired_requests is malformed") from exc
    if len(bits) != _BLOOM_BYTES or not inflater.eof or inflater.unused_data:
        raise _Malformed("retired_requests is malformed")
    return bytearray(bits)


def _bloom_encode(bits: bytes) -> Optional[Dict[str, Any]]:
    if bits.count(0) == len(bits):
        return None
    return {"bits": BLOOM_BITS, "hashes": BLOOM_HASHES,
            "data": base64.b64encode(zlib.compress(bytes(bits), 9)).decode("ascii")}


def _bloom_union(left: bytes, right: bytes) -> bytearray:
    merged = int.from_bytes(left, "little") | int.from_bytes(right, "little")
    return bytearray(merged.to_bytes(_BLOOM_BYTES, "little"))


def _retire_overflow(doc: Dict[str, Any]) -> None:
    """Move the oldest replay records beyond MAX_REQUESTS into the filter."""
    retired = []
    while len(doc["requests"]) > MAX_REQUESTS:
        oldest = next(iter(doc["requests"]))
        del doc["requests"][oldest]
        retired.append(oldest)
    if retired:
        bits = _bloom_decode(doc.get("retired_requests"))
        for request_id in retired:
            _bloom_add(bits, _request_digest(request_id))
        doc["retired_requests"] = _bloom_encode(bits)


def _bloom_stats(field: Any) -> Dict[str, Any]:
    """Estimated retired ids and the chance that a new id is refused as used."""
    ones = bin(int.from_bytes(_bloom_decode(field), "big")).count("1")
    fill = ones / BLOOM_BITS
    estimate = None if ones == BLOOM_BITS else int(round(-(BLOOM_BITS / BLOOM_HASHES) * math.log(1 - fill)))
    return {"retired_ids_estimate": estimate, "false_refusal_rate": round(fill ** BLOOM_HASHES, 6)}


# ---------------------------------------------------------------------------
# Document validation and tree helpers
# ---------------------------------------------------------------------------

def _stamp_ok(value: Any, *, required: bool = False) -> bool:
    if value is None:
        return not required
    return isinstance(value, str) and bool(value) and len(value) <= MAX_STAMP_CHARS


def _depth_ok(value: Any, depth: int = 0) -> bool:
    if depth > MAX_RESULT_DEPTH:
        return False
    if isinstance(value, dict):
        return all(_depth_ok(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return all(_depth_ok(item, depth + 1) for item in value)
    return True


def _written_by_cleaner(value: Any, cleaner: Callable[[Any], Any]) -> bool:
    """True when ``value`` is exactly what ``cleaner`` stores for it."""
    try:
        return cleaner(value) == value
    except ListError:
        return False


def _check_group(group_id: Any, group: Any, groups: Dict[str, Any]) -> None:
    if (
        not isinstance(group_id, str) or not _GROUP_ID_RE.match(group_id)
        or not isinstance(group, dict) or not set(group) <= _GROUP_KEYS
        or group.get("id") != group_id
        or not _written_by_cleaner(group.get("name"), clean_name)
        or (group.get("parent_id") is not None and (
            not isinstance(group["parent_id"], str) or group["parent_id"] not in groups))
        or not _stamp_ok(group.get("created_at")) or not _stamp_ok(group.get("updated_at"))
    ):
        raise _Malformed(f"group {group_id!r} is malformed")


def _check_entry(entry_id: Any, entry: Any, groups: Dict[str, Any], next_seq: int) -> None:
    due = entry.get("due") if isinstance(entry, dict) else None
    if (
        not isinstance(entry_id, str) or not _ENTRY_ID_RE.match(entry_id)
        or not isinstance(entry, dict) or not set(entry) <= _ENTRY_KEYS
        or entry.get("id") != entry_id
        or not isinstance(entry.get("group_id"), str) or entry["group_id"] not in groups
        or not _written_by_cleaner(entry.get("text"), clean_text)
        or entry.get("status") not in STATUSES
        or not _is_int(entry.get("seq")) or not 1 <= entry["seq"] < next_seq
        or not (due is None or (isinstance(due, dict) and isinstance(due.get("raw"), str)
                                and _written_by_cleaner(due, lambda value: parse_due(value["raw"]))))
        or not (entry.get("source") is None or (isinstance(entry["source"], str)
                                                and len(entry["source"]) <= MAX_SOURCE_CHARS))
        or not (entry.get("request_id") is None or (isinstance(entry["request_id"], str)
                                                    and _REQUEST_ID_RE.match(entry["request_id"])))
        or not all(_stamp_ok(entry.get(key)) for key in ("created_at", "updated_at", "completed_at",
                                                          "deleted_at"))
    ):
        raise _Malformed(f"entry {entry_id!r} is malformed")


def _journal_entry(value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not _ENTRY_ID_RE.fullmatch(value["id"]):
        return False
    allowed = {"id", "text", "status", "group_id", "group_path", "due", "source", "created_at",
               "updated_at", "completed_at", "deleted_at"}
    required = {"id", "status", "group_id", "source", "created_at", "updated_at", "completed_at", "deleted_at"}
    if not required <= set(value) <= allowed:
        return False
    return (all(isinstance(value[key], str) for key in ("text", "status", "group_id", "group_path")
                if key in value)
            and ("text" not in value or _written_by_cleaner(value["text"], clean_text))
            and ("group_path" not in value or len(value["group_path"]) <= MAX_GROUPS * (MAX_NAME_CHARS + 3))
            and ("source" not in value or value["source"] is None or
                 (isinstance(value["source"], str) and len(value["source"]) <= MAX_SOURCE_CHARS))
            and all(_stamp_ok(value[key]) for key in ("created_at", "updated_at", "completed_at", "deleted_at")
                    if key in value)
            and ("status" not in value or value["status"] in STATUSES)
            and ("group_id" not in value or _GROUP_ID_RE.fullmatch(value["group_id"]) is not None)
            and ("due" not in value or value["due"] is None or
                 (isinstance(value["due"], dict) and isinstance(value["due"].get("raw"), str)
                  and _written_by_cleaner(value["due"], lambda due: parse_due(due["raw"])))))


def _journal_group(value: Any) -> bool:
    return (isinstance(value, dict) and {"id", "parent_id"} <= set(value) <= {"id", "name", "path", "parent_id"}
            and isinstance(value.get("id"), str) and _GROUP_ID_RE.fullmatch(value["id"]) is not None
            and all(isinstance(value[key], str) for key in ("name", "path") if key in value)
            and ("name" not in value or _written_by_cleaner(value["name"], clean_name))
            and ("path" not in value or len(value["path"]) <= MAX_GROUPS * (MAX_NAME_CHARS + 3))
            and ("parent_id" not in value or value["parent_id"] is None or
                 (isinstance(value["parent_id"], str) and _GROUP_ID_RE.fullmatch(value["parent_id"]) is not None)))


def _journal_entries(value: Any) -> bool:
    return (isinstance(value, list) and len(value) <= MAX_BATCH
            and all(_journal_entry(item) for item in value)
            and len({item["id"] for item in value}) == len(value))


def _journal_ids(value: Any) -> bool:
    return (isinstance(value, list) and len(value) <= MAX_BATCH
            and all(isinstance(item, str) and _ENTRY_ID_RE.fullmatch(item) for item in value)
            and len(set(value)) == len(value))


def _journal_shape(op: str, result: Dict[str, Any]) -> bool:
    if result.get("op") != op:
        return False
    fields = set(result) - {"op"}
    if op == "add":
        if not {"group", "added", "possible_duplicates"} <= fields or not fields <= {
            "group", "added", "possible_duplicates", "duplicates_omitted_on_replay"}:
            return False
        duplicates = result["possible_duplicates"]
        return (_journal_group(result["group"]) and _journal_entries(result["added"])
                and bool(result["added"])
                and all(item["group_id"] == result["group"]["id"] and item["status"] == "open"
                        for item in result["added"])
                and isinstance(duplicates, list) and len(duplicates) <= MAX_BATCH
                and all(isinstance(item, dict) and set(item) == {
                    "entry_id", "same_text_open_entries", "total", "truncated"}
                    and isinstance(item["entry_id"], str) and _ENTRY_ID_RE.fullmatch(item["entry_id"])
                    and _journal_ids(item["same_text_open_entries"])
                    and len(item["same_text_open_entries"]) <= MAX_DUPLICATE_IDS
                    and _is_int(item["total"]) and len(item["same_text_open_entries"]) <= item["total"] <= MAX_ENTRIES
                    and isinstance(item["truncated"], bool)
                    and item["entry_id"] in {entry["id"] for entry in result["added"]}
                    and item["entry_id"] not in item["same_text_open_entries"]
                    and item["truncated"] == (item["total"] > len(item["same_text_open_entries"]))
                    for item in duplicates)
                and len({item["entry_id"] for item in duplicates}) == len(duplicates)
                and ("duplicates_omitted_on_replay" not in result or
                     isinstance(result["duplicates_omitted_on_replay"], bool)))
    if op in ("update", "edit"):
        return (fields == {"entry", "changed"} and _journal_entry(result["entry"])
                and isinstance(result["changed"], list) and len(result["changed"]) <= 5
                and all(isinstance(item, str) and item in {"text", "due", "status", "group"}
                        for item in result["changed"]))
    if op in ("complete", "reopen", "move", "delete", "undo", "erase"):
        key = {"complete": "changed", "reopen": "changed", "move": "moved",
               "delete": "deleted", "undo": "undeleted", "erase": "erased"}[op]
        required = {key, "unchanged"} | ({"to_group"} if op == "move" else set())
        return (fields == required and _journal_entries(result[key]) and _journal_ids(result["unchanged"])
                and bool(result[key] or result["unchanged"])
                and len(result[key]) + len(result["unchanged"]) <= MAX_BATCH
                and not {item["id"] for item in result[key]}.intersection(result["unchanged"])
                and (op not in ("complete", "reopen") or all(
                    item["status"] == ("done" if op == "complete" else "open") for item in result[key]))
                and (op != "move" or (_journal_group(result["to_group"]) and all(
                    item["group_id"] == result["to_group"]["id"] for item in result[key]))))
    if op in ("group.create", "group.rename", "group.move", "group.delete"):
        key = "deleted" if op == "group.delete" else "group"
        return fields == {key} and _journal_group(result[key])
    return False


def _check_request(request_id: Any, record: Any) -> None:
    if (
        not isinstance(request_id, str) or not _REQUEST_ID_RE.match(request_id)
        or not isinstance(record, dict) or set(record) != _REQUEST_KEYS
        or not isinstance(record.get("op"), str) or not 0 < len(record["op"]) <= MAX_OP_CHARS
        or not isinstance(record.get("fingerprint"), str) or not _HEX64_RE.match(record["fingerprint"])
        or not _stamp_ok(record.get("at"), required=True)
        or not isinstance(record.get("result"), dict) or not _depth_ok(record["result"])
        or not _journal_shape(record["op"], record["result"])
    ):
        raise _Malformed(f"request record {request_id!r} is malformed")


def _check_unicode(doc: Any) -> None:
    try:
        json.dumps(doc, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _Malformed("it contains text that is not valid Unicode (an unpaired surrogate)") from exc
    except (TypeError, ValueError, RecursionError) as exc:
        raise _Malformed("it contains values that are not plain JSON") from exc


def _check_document(doc: Any) -> Dict[str, Any]:
    """Validate a store document completely, upgrading schema 1 or 2 in memory.

    Accepts only what this module could have written: known fields with the
    right types and lengths, ids in their formats, names and texts as the input
    cleaners store them, due values as ``parse_due`` records them, unique
    sibling names, parent links without cycles, seq numbers below ``next_seq``
    and valid Unicode throughout. Anything else raises ``_Malformed``, which
    callers turn into ``store_unreadable`` or ``invalid_export``.
    """
    if not isinstance(doc, dict):
        raise _Malformed("top level is not an object")
    version = doc.get("schema_version")
    if not _is_int(version) or version < 1:
        raise _Malformed("schema_version is missing")
    if version > SCHEMA_VERSION:
        raise _Malformed(f"schema_version {version} is newer than this skill supports ({SCHEMA_VERSION})")
    unknown = sorted(str(key) for key in set(doc) - _DOC_KEYS_BY_VERSION[version])
    if unknown:
        raise _Malformed(f"it has unknown top-level fields {unknown[:5]}")
    _check_unicode(doc)
    if version < SCHEMA_VERSION:
        # 0.1.0 (schema 1) had no lineage, trash or expired ids; the 0.2.0
        # draft (schema 2) kept evicted ids as a digest list, folded into the
        # filter here. The upgrade is saved with the next change, never on a read.
        expired = doc.get("expired_requests", [])
        if (not isinstance(expired, list) or len(expired) > MAX_LEGACY_EXPIRED
                or not all(isinstance(item, str) and _HEX16_RE.match(item) for item in expired)):
            raise _Malformed("expired_requests must be a list of 16-hex digests")
        bits = bytearray(_BLOOM_BYTES)
        for digest in expired:
            _bloom_add(bits, digest)
        # Schema 1 kept a minimal add replay ({op, added:[{id}]}). Its identity
        # is safe to retain, but the partial result must never be replayed.
        legacy_requests = doc.get("requests")
        if isinstance(legacy_requests, dict):
            for request_id, record in list(legacy_requests.items()):
                result = record.get("result") if isinstance(record, dict) else None
                if (version == 1 and isinstance(record, dict)
                        and isinstance(request_id, str) and _REQUEST_ID_RE.match(request_id)
                        and record.get("op") == "add" and isinstance(result, dict)
                        and set(result) == {"op", "added"} and result["op"] == "add"
                        and isinstance(result["added"], list) and len(result["added"]) <= MAX_BATCH
                        and all(isinstance(item, dict) and set(item) == {"id"}
                                and isinstance(item["id"], str) and _ENTRY_ID_RE.fullmatch(item["id"])
                                for item in result["added"])):
                    _bloom_add(bits, _request_digest(request_id))
                    del legacy_requests[request_id]
        doc = {key: value for key, value in doc.items() if key != "expired_requests"}
        doc.update(schema_version=SCHEMA_VERSION, retired_requests=_bloom_encode(bits))
        doc.setdefault("store_id", None)
        doc.setdefault("generation", 0)
    groups, entries, requests = doc.get("groups"), doc.get("entries"), doc.get("requests")
    if not all(isinstance(part, dict) for part in (groups, entries, requests)):
        raise _Malformed("groups, entries and requests must be objects")
    next_seq = doc.get("next_seq")
    if not _is_int(next_seq) or not 1 <= next_seq < MAX_COUNTER:
        raise _Malformed("next_seq is missing or out of range")
    store_id = doc.get("store_id")
    if store_id is not None and not (isinstance(store_id, str) and _STORE_ID_RE.match(store_id)):
        raise _Malformed("store_id is malformed")
    if not _is_int(doc.get("generation")) or not 0 <= doc["generation"] < MAX_COUNTER:
        raise _Malformed("generation is missing or out of range")
    if not _stamp_ok(doc.get("updated_at")):
        raise _Malformed("updated_at is malformed")
    _bloom_decode(doc.get("retired_requests"))
    if len(groups) > MAX_GROUPS or len(entries) > MAX_ENTRIES or len(requests) > MAX_REQUESTS_ACCEPTED:
        raise _Malformed(f"it holds more than {MAX_GROUPS} groups, {MAX_ENTRIES} entries or "
                         f"{MAX_REQUESTS_ACCEPTED} request records")
    siblings = set()
    for group_id, group in groups.items():
        _check_group(group_id, group, groups)
        key = (group.get("parent_id"), _norm(group["name"]))
        if key in siblings:
            raise _Malformed(f"two groups with the same parent are both named {group['name']!r}")
        siblings.add(key)
    for group_id in groups:
        seen = set()
        current: Optional[str] = group_id
        while current is not None:
            if current in seen:
                raise _Malformed(f"group {group_id!r} is part of a parent cycle")
            seen.add(current)
            current = groups[current].get("parent_id")
    seqs = set()
    for entry_id, entry in entries.items():
        _check_entry(entry_id, entry, groups, next_seq)
        if entry["seq"] in seqs:
            raise _Malformed(f"entry {entry_id!r} repeats seq {entry['seq']}")
        seqs.add(entry["seq"])
    for request_id, record in requests.items():
        _check_request(request_id, record)
    return doc


def _parse_store(raw: bytes) -> Dict[str, Any]:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise _unreadable("it is not UTF-8 text") from exc
    except (ValueError, RecursionError) as exc:
        raise _unreadable(f"invalid JSON: {exc}") from exc
    try:
        return _check_document(doc)
    except _Malformed as exc:
        raise _unreadable(str(exc)) from exc


def _lineage_conflict(doc: Dict[str, Any], meta: Optional[Dict[str, Any]]) -> bool:
    """True when store.json is older than, or foreign to, the last recorded save."""
    if not meta or not isinstance(meta.get("store_id"), str) or not _is_int(meta.get("generation")):
        return False
    return doc.get("store_id") != meta["store_id"] or doc["generation"] < meta["generation"]


def _interrupted_write(doc: Dict[str, Any], raw: bytes, meta: Optional[Dict[str, Any]]) -> bool:
    """True when store.json is byte-for-byte the file that the write the
    sentinel announced was about to replace: that write failed or was cut off
    before its commit point, so this file is the last committed state."""
    pending = (meta or {}).get("pending")
    return (isinstance(pending, dict) and pending.get("store_id") == doc.get("store_id")
            and pending.get("generation") == doc["generation"] and pending.get("sha256") == _sha256(raw))


def _clean_meta(meta: Any) -> Dict[str, Any]:
    """Keep only well-formed sentinel fields, so a damaged or hand-edited
    sentinel can never break status views; {} still proves a prior save."""
    if not isinstance(meta, dict):
        return {}
    clean: Dict[str, Any] = {}
    if isinstance(meta.get("store_id"), str) and _STORE_ID_RE.match(meta["store_id"]):
        clean["store_id"] = meta["store_id"]
    if _is_int(meta.get("generation")) and meta["generation"] >= 0:
        clean["generation"] = meta["generation"]
    if _stamp_ok(meta.get("written_at"), required=True):
        clean["written_at"] = meta["written_at"]
    counts = meta.get("counts")
    if isinstance(counts, dict) and all(_is_int(counts.get(key)) for key in _COUNT_KEYS):
        clean["counts"] = {key: counts[key] for key in _COUNT_KEYS}
    last = meta.get("last_export")
    if (isinstance(last, dict) and _stamp_ok(last.get("at"), required=True) and _is_int(last.get("generation"))
            and isinstance(last.get("sha256"), str) and isinstance(last.get("file"), str)):
        clean["last_export"] = {"at": last["at"], "generation": last["generation"],
                                "sha256": last["sha256"][:64], "file": last["file"][:255]}
    pending = meta.get("pending")
    if (isinstance(pending, dict) and _is_int(pending.get("generation"))
            and (pending.get("store_id") is None or isinstance(pending.get("store_id"), str))
            and isinstance(pending.get("sha256"), str) and _HEX64_RE.match(pending["sha256"])):
        clean["pending"] = {"store_id": pending.get("store_id"), "generation": pending["generation"],
                            "sha256": pending["sha256"]}
    return clean


def _counts(doc: Dict[str, Any]) -> Dict[str, int]:
    live = [entry for entry in doc["entries"].values() if not entry.get("deleted_at")]
    return {
        "groups": len(doc["groups"]),
        "entries": len(doc["entries"]),
        "open": sum(1 for entry in live if entry["status"] == "open"),
        "done": sum(1 for entry in live if entry["status"] == "done"),
        "deleted": len(doc["entries"]) - len(live),
    }


def _norm(name: str) -> str:
    return " ".join(name.split()).casefold()


def _children(doc: Dict[str, Any], parent_id: Optional[str]) -> List[Dict[str, Any]]:
    kids = [group for group in doc["groups"].values() if group.get("parent_id") == parent_id]
    return sorted(kids, key=lambda group: (_norm(group["name"]), group["id"]))


def _tree_order(doc: Dict[str, Any], root_id: Optional[str] = None) -> List[Tuple[Dict[str, Any], int]]:
    """Depth-first (group, depth) pairs; the whole forest or one subtree."""
    ordered: List[Tuple[Dict[str, Any], int]] = []
    stack = [(doc["groups"][root_id], 0)] if root_id else [(group, 0) for group in reversed(_children(doc, None))]
    while stack:
        group, depth = stack.pop()
        ordered.append((group, depth))
        stack.extend((child, depth + 1) for child in reversed(_children(doc, group["id"])))
    return ordered


def _paths(doc: Dict[str, Any]) -> Dict[str, str]:
    paths: Dict[str, str] = {}
    for group, _depth in _tree_order(doc):
        parent = group.get("parent_id")
        paths[group["id"]] = f"{paths[parent]} / {group['name']}" if parent else group["name"]
    return paths


def _entries_by_group(doc: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for entry in sorted(doc["entries"].values(), key=lambda item: item["seq"]):
        grouped.setdefault(entry["group_id"], []).append(entry)
    return grouped


def _known_paths_hint(doc: Dict[str, Any]) -> str:
    known = list(_paths(doc).values())
    if not known:
        return "No groups exist yet; create one first."
    shown = ", ".join(repr(path) for path in known[:10])
    more = f" and {len(known) - 10} more" if len(known) > 10 else ""
    return f"Known groups: {shown}{more}."


def resolve_group(doc: Dict[str, Any], ref: Any, *, field: str = "group") -> Dict[str, Any]:
    """Resolve a group id or a case-insensitive '/'-separated name path."""
    text = _ref_text(ref, field=field)
    if _GROUP_ID_RE.match(text):
        group = doc["groups"].get(text)
        if group is None:
            raise ListError("not_found", f"unknown group id {text}. {_known_paths_hint(doc)}")
        return group
    segments = [_norm(part) for part in text.split("/") if part.strip()]
    if not segments:
        raise _invalid(f"{field} is not a usable path")
    parent: Optional[str] = None
    current: Optional[Dict[str, Any]] = None
    for segment in segments:
        current = next((group for group in _children(doc, parent) if _norm(group["name"]) == segment), None)
        if current is None:
            raise ListError("not_found", f"no group at path {text!r}. {_known_paths_hint(doc)}")
        parent = current["id"]
    assert current is not None
    return current


def _entry(doc: Dict[str, Any], entry_id: str, *, live: bool = False) -> Dict[str, Any]:
    entry = doc["entries"].get(entry_id)
    if entry is None:
        raise ListError("not_found", f"unknown entry id {entry_id}")
    if live and entry.get("deleted_at"):
        raise ListError("conflict", f"entry {entry_id} is deleted; undo the deletion before changing it")
    return entry


def _new_id(existing: Dict[str, Any], prefix: str) -> str:
    while True:
        candidate = f"{prefix}{secrets.token_hex(5)}"
        if candidate not in existing:
            return candidate


def _assert_unique_sibling(doc: Dict[str, Any], parent_id: Optional[str], name: str, exclude_id: str = "") -> None:
    for sibling in _children(doc, parent_id):
        if sibling["id"] != exclude_id and _norm(sibling["name"]) == _norm(name):
            raise ListError(
                "conflict",
                f"{_paths(doc)[sibling['id']]!r} already exists; group names are unique among siblings",
            )


def _entry_view(entry: Dict[str, Any], paths: Dict[str, str]) -> Dict[str, Any]:
    return {
        "id": entry["id"],
        "text": entry["text"],
        "status": entry["status"],
        "group_id": entry["group_id"],
        "group_path": paths.get(entry["group_id"], ""),
        "due": entry.get("due"),
        "source": entry.get("source", ""),
        "created_at": entry.get("created_at", ""),
        "updated_at": entry.get("updated_at", ""),
        "completed_at": entry.get("completed_at"),
        "deleted_at": entry.get("deleted_at"),
    }


def _group_view(group: Dict[str, Any], paths: Dict[str, str]) -> Dict[str, Any]:
    return {"id": group["id"], "name": group["name"], "path": paths.get(group["id"], group["name"]),
            "parent_id": group.get("parent_id")}


def _journal_result(value: Any) -> Any:
    """Keep replay identity, not private text, labels or large duplicate lists."""
    if isinstance(value, list):
        return [_journal_result(item) for item in value]
    if isinstance(value, dict):
        copy = {key: _journal_result(item) for key, item in value.items() if key != "possible_duplicates"}
        if "possible_duplicates" in value:
            # Journals are re-sanitized on every write: keep a flag set earlier.
            copy["possible_duplicates"] = []
            copy["duplicates_omitted_on_replay"] = (bool(value["possible_duplicates"])
                                                    or value.get("duplicates_omitted_on_replay") is True)
        object_id = copy.get("id")
        if isinstance(object_id, str) and _ENTRY_ID_RE.fullmatch(object_id):
            for key in ("text", "due", "group_path"):
                copy.pop(key, None)
        if isinstance(object_id, str) and _GROUP_ID_RE.fullmatch(object_id):
            copy.pop("name", None)
            copy.pop("path", None)
        return copy
    return value


def _replay_result(value: Any, doc: Dict[str, Any], paths: Optional[Dict[str, str]] = None) -> Any:
    """Fill private fields from current entries/groups, not stale journal copies;
    an erased entry or deleted group is reported as ``gone``."""
    paths = _paths(doc) if paths is None else paths
    if isinstance(value, list):
        return [_replay_result(item, doc, paths) for item in value]
    if isinstance(value, dict):
        copy = {key: _replay_result(item, doc, paths) for key, item in value.items()}
        object_id = copy.get("id")
        if isinstance(object_id, str):
            if object_id in doc["entries"]:
                copy.update(_entry_view(doc["entries"][object_id], paths))
            elif object_id in doc["groups"]:
                copy.update(_group_view(doc["groups"][object_id], paths))
            elif _ENTRY_ID_RE.fullmatch(object_id) or _GROUP_ID_RE.fullmatch(object_id):
                copy = {"id": object_id, "gone": True}
        return copy
    return value


def _replay_known_request(doc: Dict[str, Any], op: str, fingerprint: str, request_id: str) -> Optional[Dict[str, Any]]:
    """The replay of an already-applied request id, or None when the id is new.

    The journal keeps full replay records for the last MAX_REQUESTS ids. An
    older id is found in the retired-id filter and refused rather than applied
    a second time; the filter has no false negatives, and a false positive only
    refuses a new id, which applies nothing.
    """
    prior = doc["requests"].get(request_id)
    if prior is not None:
        if prior.get("op") == op and prior.get("fingerprint") == fingerprint:
            return dict(_replay_result(prior["result"], doc), replayed=True)
        raise ListError(
            "request_conflict",
            f"request_id {request_id!r} was already used for a different change; "
            "use a new request_id for a new request",
        )
    retired = doc.get("retired_requests")
    if retired is not None and _bloom_has(_bloom_decode(retired), _request_digest(request_id)):
        rate = _bloom_stats(retired)["false_refusal_rate"]
        crowded = (f" The retired-id filter now refuses about {rate:.1%} of new ids this way."
                   if rate >= 0.01 else "")
        raise ListError(
            "request_expired",
            f"request_id {request_id!r} matches an id retired from the replay journal: it was applied long ago "
            "(or, rarely, it collides with one) and its result is no longer kept. Nothing was applied. Read the "
            f"current state; if this is a new change, repeat it with a new request_id.{crowded}",
        )
    return None


def _tree_rows(doc: Dict[str, Any], root_id: Optional[str] = None) -> List[Dict[str, Any]]:
    paths = _paths(doc)
    grouped = _entries_by_group(doc)
    rows = []
    for group, depth in _tree_order(doc, root_id):
        entries = grouped.get(group["id"], [])
        live = [entry for entry in entries if not entry.get("deleted_at")]
        rows.append({
            "id": group["id"],
            "path": paths[group["id"]],
            "depth": depth,
            "open": sum(1 for entry in live if entry["status"] == "open"),
            "done": sum(1 for entry in live if entry["status"] == "done"),
            "deleted": len(entries) - len(live),
        })
    return rows


# ---------------------------------------------------------------------------
# Document operations (mutate ``doc`` in place; validate before changing it)
# ---------------------------------------------------------------------------

def _op_update(doc: Dict[str, Any], entry_id: str, text: Optional[str], due: Optional[Dict[str, str]],
               clear_due: bool, now: str) -> List[str]:
    entry = _entry(doc, entry_id, live=True)
    changed = []
    if text is not None and text != entry["text"]:
        entry["text"] = text
        changed.append("text")
    if due is not None and due != entry.get("due"):
        entry["due"] = due
        changed.append("due")
    if clear_due and entry.get("due") is not None:
        entry["due"] = None
        changed.append("due")
    if changed:
        entry["updated_at"] = now
    return changed


def _op_set_done(doc: Dict[str, Any], entry_ids: List[str], done: bool, now: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    entries = [_entry(doc, entry_id, live=True) for entry_id in entry_ids]
    changed, unchanged = [], []
    for entry in entries:
        wanted = "done" if done else "open"
        if entry["status"] == wanted:
            unchanged.append(entry["id"])
            continue
        entry["status"] = wanted
        entry["completed_at"] = now if done else None
        entry["updated_at"] = now
        changed.append(entry)
    return changed, unchanged


def _op_move(doc: Dict[str, Any], entry_ids: List[str], target: Dict[str, Any], now: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    entries = [_entry(doc, entry_id, live=True) for entry_id in entry_ids]
    moved, unchanged = [], []
    for entry in entries:
        if entry["group_id"] == target["id"]:
            unchanged.append(entry["id"])
            continue
        entry["group_id"] = target["id"]
        entry["updated_at"] = now
        moved.append(entry)
    return moved, unchanged


def _op_lifecycle(doc: Dict[str, Any], entry_ids: List[str], action: str, now: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """delete: into the trash; undo: back out with status and group intact;
    erase: permanently remove entries that are already in the trash."""
    entries = [_entry(doc, entry_id) for entry_id in entry_ids]
    if action == "erase":
        live = [entry["id"] for entry in entries if not entry.get("deleted_at")]
        if live:
            raise ListError("conflict", f"erase only removes deleted entries; delete {live} first. "
                                        "Erasing cannot be undone.")
        for entry in entries:
            del doc["entries"][entry["id"]]
        return entries, []
    changed, unchanged = [], []
    for entry in entries:
        if bool(entry.get("deleted_at")) == (action == "delete"):
            unchanged.append(entry["id"])
            continue
        entry["deleted_at"] = now if action == "delete" else None
        entry["updated_at"] = now
        changed.append(entry)
    return changed, unchanged


def _possible_duplicates(doc: Dict[str, Any], created: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Report (never merge) open entries in the same group with the same text:
    the oldest MAX_DUPLICATE_IDS ids per new entry, plus the full count."""
    same_text: Dict[Tuple[str, str], List[str]] = {}
    for other in sorted(doc["entries"].values(), key=lambda item: item["seq"]):
        if other["status"] == "open" and not other.get("deleted_at"):
            same_text.setdefault((other["group_id"], _norm(other["text"])), []).append(other["id"])
    report = []
    for entry in created:
        matches = [other for other in same_text.get((entry["group_id"], _norm(entry["text"])), [])
                   if other != entry["id"]]
        if matches:
            report.append({"entry_id": entry["id"], "same_text_open_entries": matches[:MAX_DUPLICATE_IDS],
                           "total": len(matches), "truncated": len(matches) > MAX_DUPLICATE_IDS})
    return report


# ---------------------------------------------------------------------------
# Export files
# ---------------------------------------------------------------------------

def parse_restore_payload(raw: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Validate an export (or a bare store document) and return (document, source).

    An export must carry the checksum and counts written at export time, so a
    truncated or edited file is refused instead of restored partially, and the
    document must pass the full ``_check_document`` validation.
    """
    if len(raw) > MAX_IMPORT_BYTES:
        raise ListError("invalid_export", f"the export is larger than {MAX_IMPORT_BYTES} bytes")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ListError("invalid_export", "the export text is not valid Unicode (an unpaired surrogate). "
                                          "Nothing was restored.") from exc
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ListError("invalid_export", "this is not JSON; expected a Smart Lists export. Nothing was restored.") from exc
    try:
        _check_unicode(data)
    except _Malformed as exc:
        raise ListError("invalid_export", f"the export is not usable: {exc}. Nothing was restored.") from exc
    if isinstance(data, dict) and data.get("format") == EXPORT_FORMAT:
        version = data.get("format_version")
        if not _is_int(version) or version < 1:
            raise ListError("invalid_export", "the export has no format_version. Nothing was restored.")
        if version > EXPORT_FORMAT_VERSION:
            raise ListError("invalid_export", f"export format_version {version} is newer than this skill "
                                              f"supports ({EXPORT_FORMAT_VERSION}). Nothing was restored.")
        document = data.get("document")
        if not isinstance(document, dict) or _digest(document) != data.get("sha256"):
            raise ListError("invalid_export", "the export's checksum does not match its content: it was changed "
                                              "or damaged after export. Nothing was restored.")
        exported_at = data.get("exported_at")
        source = {"kind": "export", "exported_at": exported_at if _stamp_ok(exported_at, required=True) else "",
                  "sha256": data["sha256"]}
    elif isinstance(data, dict) and "schema_version" in data:
        document = data
        source = {"kind": "store_document", "exported_at": "", "sha256": _digest(data)}
    else:
        raise ListError("invalid_export", "this is not a Smart Lists export (no export format or store schema). "
                                          "Nothing was restored.")
    try:
        doc = _check_document(document)
    except _Malformed as exc:
        raise ListError("invalid_export", f"the export does not hold a valid list store ({exc}). "
                                          "Nothing was restored.") from exc
    if source["kind"] == "export" and data.get("counts") != _counts(doc):
        raise ListError("invalid_export", "the export's counts do not match its content. Nothing was restored.")
    return doc, source


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_KEEP = object()


def _busy() -> ListError:
    return ListError(
        "store_busy",
        f"Another process kept the list store locked for more than {LOCK_TIMEOUT_SEC:g} seconds; nothing was "
        "read or changed. Try again in a moment.",
    )


def _try_file_lock(handle: Any) -> bool:
    """One non-blocking attempt at the cross-process lock. True when it is held,
    or when the platform has neither fcntl nor msvcrt (thread lock only)."""
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    if msvcrt is not None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)):
                return False
            raise
        return True
    return True


def _release_file_lock(handle: Any) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass  # closing the handle releases the lock as well


def _replace(source: str, target: Path) -> None:
    """os.replace, retried briefly on Windows, where a process outside this
    skill (an indexer or antivirus) may hold the target open for a moment."""
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _fsync_dir(path: Path) -> None:
    """Make a completed rename durable on POSIX; best effort."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class ListStore:
    """The single authoritative JSON document under the skill state dir."""

    def __init__(self, state_dir: Any) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / STORE_FILE
        self.meta_path = self.state_dir / META_FILE
        self.backup_dir = self.state_dir / BACKUP_DIR
        self._lock_path = self.state_dir / LOCK_FILE
        self._thread_lock = threading.RLock()
        # Set when an export was delivered but could not be recorded in the
        # sentinel; kept in memory (the disk just refused a write) until the
        # next export is recorded.
        self.export_record_failure: Optional[Dict[str, str]] = None

    @contextmanager
    def _locked(self) -> Iterator[None]:
        # One deadline for both locks, so a caller never waits longer than
        # LOCK_TIMEOUT_SEC in total (below the host's tool timeout).
        deadline = time.monotonic() + LOCK_TIMEOUT_SEC
        if not self._thread_lock.acquire(timeout=LOCK_TIMEOUT_SEC):
            raise _busy()
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with open(self._lock_path, "a+b") as handle:
                delay = 0.005
                while not _try_file_lock(handle):
                    if time.monotonic() >= deadline:
                        raise _busy()
                    time.sleep(delay)
                    delay = min(delay * 2, 0.05)
                try:
                    yield
                finally:
                    _release_file_lock(handle)
        finally:
            self._thread_lock.release()

    def _meta_bytes(self) -> Optional[bytes]:
        try:
            return self.meta_path.read_bytes()
        except FileNotFoundError:
            return None

    def _read_meta(self) -> Optional[Dict[str, Any]]:
        """The sentinel: None when absent; {} when its content is damaged, which
        still proves that a store was saved here. An OS error reading it is
        raised (store_io) rather than silently skipping the lineage check."""
        raw = self._meta_bytes()
        if raw is None:
            return None
        try:
            meta = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            return {}
        return _clean_meta(meta)

    def _load_raw(self) -> Tuple[Dict[str, Any], bytes]:
        meta = self._read_meta()
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            raise (_missing(meta) if meta is not None else _not_initialized()) from None
        doc = _parse_store(raw)
        if _lineage_conflict(doc, meta) and not _interrupted_write(doc, raw, meta):
            raise _mismatch(doc, meta or {})
        return doc, raw

    def _load(self) -> Dict[str, Any]:
        return self._load_raw()[0]

    def _write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            _replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _fsync_dir(path.parent)

    def _write_meta(self, doc: Dict[str, Any], *, last_export: Any = _KEEP,
                    pending: Optional[Dict[str, Any]] = None) -> None:
        previous = self._read_meta() or {}
        if last_export is _KEEP:
            last_export = (previous.get("last_export")
                           if previous.get("store_id") in (None, doc.get("store_id")) else None)
        meta = {
            "note": "Sentinel for smart_lists store.json: proves a store was saved here. No list content.",
            "store_id": doc.get("store_id"),
            "generation": doc["generation"],
            "written_at": doc.get("updated_at", ""),
            "counts": _counts(doc),
            "last_export": last_export,
            "pending": pending,
        }
        self._write_atomic(self.meta_path, (json.dumps(meta, indent=1) + "\n").encode("utf-8"))

    def _put_back_meta(self, previous: Optional[bytes]) -> None:
        try:
            if previous is None:
                self.meta_path.unlink()
            else:
                self._write_atomic(self.meta_path, previous)
        except OSError:
            pass

    def _save(self, doc: Dict[str, Any], pending: Optional[Dict[str, Any]]) -> None:
        """Commit ``doc`` as the next generation of store.json.

        ``pending`` names the store.json this write replaces (store id,
        generation, SHA-256 of its bytes), or is None when there is none or it
        is not the last committed state.

        1. The sentinel is written first, with the new generation and
           ``pending``. If that fails, nothing changed and the error is raised.
        2. store.json is replaced atomically: the commit point. If that fails,
           the previous sentinel is put back. If even that fails, or the process
           dies between the two writes, ``pending`` lets the untouched file load
           as the last committed state instead of a false ``store_mismatch``.
        3. The sentinel is rewritten without ``pending``. A failure here is
           ignored: the change is committed, and the stale record only matches
           the exact file this write replaced.
        """
        if doc["generation"] >= MAX_COUNTER - 1:
            raise ListError("limit", "store generation has reached its safe counter limit; no change was applied")
        previous_meta = self._meta_bytes()
        doc["store_id"] = doc.get("store_id") or f"s_{secrets.token_hex(8)}"
        doc["generation"] = doc["generation"] + 1
        doc["updated_at"] = _now()
        data = (json.dumps(doc, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
        self._write_meta(doc, pending=pending)
        try:
            self._write_atomic(self.path, data)
        except BaseException:
            self._put_back_meta(previous_meta)
            raise
        try:
            self._write_meta(doc, pending=None)
        except OSError:
            pass

    def _write_backup(self, kind: str, data: bytes) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.backup_dir / f"{kind}-{stamp}-{secrets.token_hex(3)}.json"
        self._write_atomic(path, data)
        return path

    def _list_backups(self) -> List[Dict[str, Any]]:
        try:
            paths = [path for path in self.backup_dir.iterdir() if path.suffix == ".json"]
        except FileNotFoundError:
            return []
        listed = []
        for path in paths:
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:  # removed or unreadable meanwhile: not listed
                continue
            listed.append((stat.st_mtime, path.name, stat.st_size))
        listed.sort(reverse=True)
        return [{"file": name, "bytes": size,
                 "modified_at": datetime.fromtimestamp(mtime, timezone.utc).replace(microsecond=0).isoformat()}
                for mtime, name, size in listed]

    @contextmanager
    def _io_guard(self) -> Iterator[None]:
        # os.replace is the only commit point, so an OS failure before or during
        # it leaves the previous document in place.
        try:
            yield
        except OSError as exc:
            raise ListError(
                "store_io",
                f"The list store could not be accessed ({type(exc).__name__}); no change was applied.",
            ) from exc

    def read(self, fn: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        with self._io_guard(), self._locked():
            return fn(self._load())

    def mutate(self, op: str, params: Dict[str, Any], fn: Callable[[Dict[str, Any]], Dict[str, Any]],
               request_id: str = "") -> Dict[str, Any]:
        """Apply ``fn`` once per ``request_id`` and persist the whole document atomically."""
        fingerprint = _fingerprint(op, params)
        with self._io_guard(), self._locked():
            doc, raw = self._load_raw()
            pending = {"store_id": doc.get("store_id"), "generation": doc["generation"], "sha256": _sha256(raw)}
            if request_id:
                replay = _replay_known_request(doc, op, fingerprint, request_id)
                if replay is not None:
                    return replay
            result = fn(doc)
            if request_id:
                doc["requests"][request_id] = {"op": op, "fingerprint": fingerprint, "at": _now(),
                                                "result": result}
            _retire_overflow(doc)
            # Also remove private fields from journals written by earlier versions.
            for record in doc["requests"].values():
                record["result"] = _journal_result(record["result"])
            self._save(doc, pending)
        return dict(result, replayed=False)

    def inspect(self) -> Dict[str, Any]:
        """Lifecycle state for status views: a missing or unreadable store is
        reported here, not raised."""
        with self._io_guard(), self._locked():
            meta = self._read_meta() or {}
            info: Dict[str, Any] = {
                "state": "ready", "message": "", "store_id": None, "generation": None, "counts": None,
                "last_written_at": meta.get("written_at"), "last_export": meta.get("last_export"),
                "export_unrecorded": self.export_record_failure, "replay": None,
            }
            try:
                doc = self._load()
            except ListError as exc:
                if exc.code not in _STATE_BY_CODE:
                    raise
                info.update(state=_STATE_BY_CODE[exc.code], message=exc.message)
            else:
                info.update(store_id=doc.get("store_id"), generation=doc["generation"], counts=_counts(doc),
                            replay={"recent_ids": len(doc["requests"]), "recent_limit": MAX_REQUESTS,
                                    **_bloom_stats(doc.get("retired_requests"))})
            backups = self._list_backups()
            info.update(backup_dir=str(self.backup_dir), backups=backups[:MAX_LISTED_BACKUPS],
                        backups_total=len(backups))
            return info

    def export(self, *, to_file: bool) -> Tuple[Dict[str, Any], Optional[Path], Optional[Dict[str, str]]]:
        """A complete, checksummed copy of the document (journal included).

        Recording the export in the sentinel comes after the export exists; if
        that write fails the export is still returned, and the failure is
        returned too (and kept for status views) instead of being hidden.
        """
        with self._io_guard(), self._locked():
            doc = self._load()
            envelope = {
                "format": EXPORT_FORMAT,
                "format_version": EXPORT_FORMAT_VERSION,
                "exported_at": _now(),
                "store_id": doc.get("store_id"),
                "generation": doc["generation"],
                "counts": _counts(doc),
                "sha256": _digest(doc),
                "document": doc,
            }
            path = None
            if to_file:
                data = (json.dumps(envelope, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
                path = self._write_backup("export", data)
            record = {"at": envelope["exported_at"], "generation": doc["generation"],
                      "sha256": envelope["sha256"], "file": path.name if path else ""}
            try:
                self._write_meta(doc, last_export=record)
            except OSError as exc:
                self.export_record_failure = {"at": record["at"], "file": record["file"],
                                              "error": type(exc).__name__}
            else:
                self.export_record_failure = None
            return envelope, path, self.export_record_failure

    def install(self, new_doc: Dict[str, Any], *, replace: bool, reason: str) -> Dict[str, Any]:
        """Make ``new_doc`` the store (``reason`` is init or restore).

        An existing store.json, readable or not, is replaced only when
        ``replace`` is true, and is first copied byte-for-byte into backups/.
        Request ids it applied or retired that ``new_doc`` does not know are
        added to the retired-id filter, so a late retry is refused instead of
        applied again.
        """
        with self._io_guard(), self._locked():
            meta = self._read_meta()
            try:
                current_raw: Optional[bytes] = self.path.read_bytes()
            except FileNotFoundError:
                current_raw = None
            current: Optional[Dict[str, Any]] = None
            if current_raw is not None:
                try:
                    current = _parse_store(current_raw)
                except ListError:
                    current = None
            recorded = meta.get("generation") if meta and _is_int(meta.get("generation")) else 0
            if max(new_doc["generation"], recorded, current["generation"] if current else 0) >= MAX_COUNTER - 1:
                raise ListError("limit", "store generation has reached its safe counter limit; no change was applied")
            outcome: Dict[str, Any] = {"backup": None, "replaced": None}
            if current_raw is not None:
                if not replace:
                    held = ("an unreadable store file" if current is None else
                            "a list store with {groups} groups and {entries} entries".format(**_counts(current)))
                    incoming = ("" if reason == "init" else
                                " with this export ({groups} groups, {entries} entries)".format(**_counts(new_doc)))
                    raise ListError(
                        "store_exists",
                        f"This installation already has {held}; nothing was changed. To replace it{incoming}, "
                        "repeat with replace=true: the current file is copied to the backups folder first.",
                    )
                outcome["backup"] = str(self._write_backup(f"pre-{reason}", current_raw))
                outcome["replaced"] = _counts(current) if current is not None else "unreadable store file"
            elif meta is not None and reason == "init" and not replace:
                raise _missing(meta)
            elif meta is not None:
                outcome["replaced"] = f"missing store file{_meta_summary(meta)}"
            pending = None
            if current is not None and current_raw is not None:
                merged = _bloom_union(_bloom_decode(new_doc.get("retired_requests")),
                                      _bloom_decode(current.get("retired_requests")))
                for request_id in current["requests"]:
                    if request_id not in new_doc["requests"]:
                        _bloom_add(merged, _request_digest(request_id))
                new_doc["retired_requests"] = _bloom_encode(merged)
                if not _lineage_conflict(current, meta) or _interrupted_write(current, current_raw, meta):
                    pending = {"store_id": current.get("store_id"), "generation": current["generation"],
                               "sha256": _sha256(current_raw)}
            _retire_overflow(new_doc)
            new_doc["generation"] = max(new_doc["generation"], recorded, current["generation"] if current else 0)
            self._save(new_doc, pending)
            outcome.update(store_id=new_doc["store_id"], generation=new_doc["generation"], counts=_counts(new_doc))
            return outcome


# ---------------------------------------------------------------------------
# Service used by tools and routes
# ---------------------------------------------------------------------------

class SmartLists:
    def __init__(self, state_dir: Any) -> None:
        self.store = ListStore(state_dir)

    # -- entries -------------------------------------------------------------

    def add(self, group: Any, items: Any, *, request_id: Any = "") -> Dict[str, Any]:
        ref = _ref_text(group)
        cleaned = clean_items(items)
        request_id = clean_request_id(request_id)
        params = {"group": _ref_key(ref),
                  "items": [{"text": text, "due": due["raw"] if due else None} for text, due in cleaned]}

        def apply(doc: Dict[str, Any]) -> Dict[str, Any]:
            target = resolve_group(doc, ref)
            if len(doc["entries"]) + len(cleaned) > MAX_ENTRIES:
                raise ListError("limit", f"the store holds at most {MAX_ENTRIES} entries, deleted ones included "
                                         "until they are erased")
            if doc["next_seq"] + len(cleaned) >= MAX_COUNTER:
                raise ListError("limit", "entry sequence has reached its safe counter limit; no entry was added")
            now = _now()
            created = []
            for text, due in cleaned:
                entry = {
                    "id": _new_id(doc["entries"], "e_"),
                    "seq": doc["next_seq"],
                    "group_id": target["id"],
                    "text": text,
                    "status": "open",
                    "due": due,
                    "source": "chat",
                    "request_id": request_id or None,
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
                    "deleted_at": None,
                }
                doc["next_seq"] += 1
                doc["entries"][entry["id"]] = entry
                created.append(entry)
            paths = _paths(doc)
            return {
                "op": "add",
                "group": _group_view(target, paths),
                "added": [_entry_view(entry, paths) for entry in created],
                "possible_duplicates": _possible_duplicates(doc, created),
            }

        return self.store.mutate("add", params, apply, request_id)

    def update(self, entry_id: Any, *, text: Any = None, due: Any = None, clear_due: bool = False,
               request_id: Any = "") -> Dict[str, Any]:
        entry_ids = clean_entry_ids(entry_id)
        if len(entry_ids) != 1:
            raise _invalid("update changes exactly one entry")
        new_text = clean_text(text) if text is not None and text != "" else None
        new_due = parse_due(due)
        clear_due = _clean_flag(clear_due, "clear_due")
        if new_due is not None and clear_due:
            raise _invalid("give either due or clear_due, not both")
        if new_text is None and new_due is None and not clear_due:
            raise _invalid("nothing to change: give text, due or clear_due")
        request_id = clean_request_id(request_id)
        params = {"entry_id": entry_ids[0], "text": new_text, "due": new_due["raw"] if new_due else None,
                  "clear_due": clear_due}

        def apply(doc: Dict[str, Any]) -> Dict[str, Any]:
            changed = _op_update(doc, entry_ids[0], new_text, new_due, clear_due, _now())
            return {"op": "update", "entry": _entry_view(_entry(doc, entry_ids[0]), _paths(doc)), "changed": changed}

        return self.store.mutate("update", params, apply, request_id)

    def complete(self, entry_ids: Any, *, done: Any = True, request_id: Any = "") -> Dict[str, Any]:
        ids = clean_entry_ids(entry_ids)
        done = _clean_flag(done, "done")
        request_id = clean_request_id(request_id)
        op = "complete" if done else "reopen"

        def apply(doc: Dict[str, Any]) -> Dict[str, Any]:
            changed, unchanged = _op_set_done(doc, ids, done, _now())
            paths = _paths(doc)
            return {"op": op, "changed": [_entry_view(entry, paths) for entry in changed], "unchanged": unchanged}

        return self.store.mutate(op, {"entry_ids": ids}, apply, request_id)

    def move(self, entry_ids: Any, to_group: Any, *, request_id: Any = "") -> Dict[str, Any]:
        ids = clean_entry_ids(entry_ids)
        ref = _ref_text(to_group, field="to_group")
        request_id = clean_request_id(request_id)

        def apply(doc: Dict[str, Any]) -> Dict[str, Any]:
            target = resolve_group(doc, ref, field="to_group")
            moved, unchanged = _op_move(doc, ids, target, _now())
            paths = _paths(doc)
            return {"op": "move", "to_group": _group_view(target, paths),
                    "moved": [_entry_view(entry, paths) for entry in moved], "unchanged": unchanged}

        return self.store.mutate("move", {"entry_ids": ids, "to_group": _ref_key(ref)}, apply, request_id)

    def delete(self, entry_ids: Any, *, action: Any = "delete", request_id: Any = "") -> Dict[str, Any]:
        """Trash entries, undo that, or erase entries that are already trashed."""
        ids = clean_entry_ids(entry_ids)
        if action not in ENTRY_ACTIONS:
            raise _invalid(f"action must be one of {list(ENTRY_ACTIONS)}")
        request_id = clean_request_id(request_id)

        def apply(doc: Dict[str, Any]) -> Dict[str, Any]:
            paths = _paths(doc)
            changed, unchanged = _op_lifecycle(doc, ids, action, _now())
            return {"op": action, _ACTION_RESULT_KEYS[action]: [_entry_view(entry, paths) for entry in changed],
                    "unchanged": unchanged}

        return self.store.mutate(action, {"entry_ids": ids}, apply, request_id)

    def edit(self, entry_id: Any, *, text: Any = "", due: Any = "", clear_due: bool = False,
             status: Any = "", move_to: Any = "", request_id: Any = "") -> Dict[str, Any]:
        """Widget form: update, complete/reopen and move one entry in one atomic
        write, or delete/undo/erase it on its own."""
        ids = clean_entry_ids(entry_id)
        if len(ids) != 1:
            raise _invalid("edit changes exactly one entry")
        new_text = clean_text(text) if text not in (None, "") else None
        new_due = parse_due(due)
        clear_due = _clean_flag(clear_due, "clear_due")
        if new_due is not None and clear_due:
            raise _invalid("give either due or clear_due, not both")
        status = _optional_ref(status, field="status")
        destination = _optional_ref(move_to, field="move_to")
        if status in ENTRY_ACTIONS:
            if new_text is not None or new_due is not None or clear_due or destination:
                raise _invalid(f"{status} cannot be combined with other changes; apply it on its own")
            return self.delete(ids, action=status, request_id=request_id)
        if status not in ("",) + STATUSES:
            raise _invalid(f"status must be one of {list(STATUSES + ENTRY_ACTIONS)}")
        if new_text is None and new_due is None and not clear_due and not status and not destination:
            raise _invalid("nothing to change: fill in new text, due, status or a group to move to")
        request_id = clean_request_id(request_id)
        params = {"entry_id": ids[0], "text": new_text, "due": new_due["raw"] if new_due else None,
                  "clear_due": clear_due, "status": status, "move_to": _ref_key(destination)}

        def apply(doc: Dict[str, Any]) -> Dict[str, Any]:
            target = resolve_group(doc, destination, field="move_to") if destination else None
            now = _now()
            changed = _op_update(doc, ids[0], new_text, new_due, clear_due, now)
            if status:
                if _op_set_done(doc, ids, status == "done", now)[0]:
                    changed.append("status")
            if target is not None and _op_move(doc, ids, target, now)[0]:
                changed.append("group")
            return {"op": "edit", "entry": _entry_view(_entry(doc, ids[0]), _paths(doc)), "changed": changed}

        return self.store.mutate("edit", params, apply, request_id)

    # -- groups --------------------------------------------------------------

    def group(self, action: Any, *, group: Any = "", name: Any = "", parent: Any = "",
              request_id: Any = "") -> Dict[str, Any]:
        if action not in GROUP_ACTIONS:
            raise _invalid(f"action must be one of {list(GROUP_ACTIONS)}")
        ref = _ref_text(group) if action != "create" else ""
        new_name = clean_name(name) if action in ("create", "rename") else ""
        parent_ref = _optional_ref(parent, field="parent") if action in ("create", "move") else ""
        request_id = clean_request_id(request_id)
        params = {"action": action, "group": _ref_key(ref), "name": new_name, "parent": _ref_key(parent_ref)}

        def apply(doc: Dict[str, Any]) -> Dict[str, Any]:
            parent_group = resolve_group(doc, parent_ref, field="parent") if parent_ref else None
            parent_id = parent_group["id"] if parent_group else None
            now = _now()
            if action == "create":
                if len(doc["groups"]) >= MAX_GROUPS:
                    raise ListError("limit", f"the store holds at most {MAX_GROUPS} groups")
                _assert_unique_sibling(doc, parent_id, new_name)
                subject = {"id": _new_id(doc["groups"], "g_"), "name": new_name, "parent_id": parent_id,
                           "created_at": now, "updated_at": now}
                doc["groups"][subject["id"]] = subject
            else:
                subject = resolve_group(doc, ref)
            if action == "rename":
                _assert_unique_sibling(doc, subject.get("parent_id"), new_name, exclude_id=subject["id"])
                subject["name"] = new_name
                subject["updated_at"] = now
            elif action == "move":
                if parent_id is not None and any(item["id"] == parent_id for item, _ in _tree_order(doc, subject["id"])):
                    raise ListError("conflict", "a group cannot move under itself or one of its descendants")
                _assert_unique_sibling(doc, parent_id, subject["name"], exclude_id=subject["id"])
                subject["parent_id"] = parent_id
                subject["updated_at"] = now
            elif action == "delete":
                paths = _paths(doc)
                if _children(doc, subject["id"]):
                    raise ListError("conflict", f"{paths[subject['id']]!r} still has sub-groups; move or delete them first")
                held = [entry for entry in doc["entries"].values() if entry["group_id"] == subject["id"]]
                trashed = sum(1 for entry in held if entry.get("deleted_at"))
                if held:
                    raise ListError(
                        "conflict",
                        f"{paths[subject['id']]!r} still holds {len(held) - trashed} entries (open or done) and "
                        f"{trashed} deleted entries; move the entries away and undo or erase the deleted ones first",
                    )
                del doc["groups"][subject["id"]]
                return {"op": "group.delete", "deleted": _group_view(subject, paths)}
            return {"op": f"group.{action}", "group": _group_view(subject, _paths(doc))}

        return self.store.mutate(f"group.{action}", params, apply, request_id)

    # -- whole store -----------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return {"op": "status", **self.store.inspect()}

    def init(self, *, replace: Any = False) -> Dict[str, Any]:
        replace = _clean_flag(replace, "replace")
        return {"op": "init", **self.store.install(empty_document(), replace=replace, reason="init")}

    def export(self) -> Dict[str, Any]:
        envelope, path, unrecorded = self.store.export(to_file=True)
        assert path is not None
        result = {
            "op": "export",
            "file": str(path),
            "exported_at": envelope["exported_at"],
            "generation": envelope["generation"],
            "counts": envelope["counts"],
            "sha256": envelope["sha256"],
            "export_recorded": unrecorded is None,
            "note": "The file is inside the Ouroboros data directory, which uninstalling the skill or reinstalling "
                    "Ouroboros deletes. Copy it somewhere durable to keep it.",
        }
        if unrecorded:
            result["warning"] = export_unrecorded_warning(unrecorded)
        return result

    def export_document(self) -> Dict[str, Any]:
        """The export document itself (widget download). A failure to record it
        in the sentinel does not withhold it; status reports that failure."""
        return self.store.export(to_file=False)[0]

    def restore(self, *, file: Any = None, text: Any = None, replace: Any = False) -> Dict[str, Any]:
        replace = _clean_flag(replace, "replace")
        if bool(file) == bool(text):
            raise _invalid("restore needs one export: a file path, or the pasted export text")
        raw = self._read_export_file(file) if file else text
        if not isinstance(raw, str):
            raise _invalid("the export must be text")
        doc, source = parse_restore_payload(raw)
        return {"op": "restore", "source": source, **self.store.install(doc, replace=replace, reason="restore")}

    def _read_export_file(self, file: Any) -> str:
        """Read-only: an absolute path, or a bare file name in the backups folder."""
        if not isinstance(file, str) or not file.strip():
            raise _invalid("file must be a path")
        name = file.strip()
        path = Path(os.path.expanduser(name))
        if not path.is_absolute():
            if path.name != name:
                raise _invalid("file must be an absolute path or a bare file name from status.backups")
            path = self.store.backup_dir / name
        if path.suffix.lower() != ".json":
            raise _invalid("restore reads a .json export file")
        try:
            if path.stat().st_size > MAX_IMPORT_BYTES:
                raise ListError("invalid_export", f"the export is larger than {MAX_IMPORT_BYTES} bytes")
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ListError("not_found", f"no export file at {path}") from None
        except (OSError, UnicodeDecodeError) as exc:
            raise ListError("invalid_export", f"the export file could not be read ({type(exc).__name__})") from exc

    # -- reads ---------------------------------------------------------------

    def read(self, *, group: Any = "", status: Any = "open", subtree: Any = True, limit: Any = 100,
             offset: Any = 0) -> Dict[str, Any]:
        ref = _optional_ref(group, field="group")
        if status not in ("open", "done", "deleted", "all"):
            raise _invalid("status must be open, done, deleted or all")
        subtree = _clean_flag(subtree, "subtree")
        if not _is_int(limit) or not 1 <= limit <= MAX_READ_LIMIT:
            raise _invalid(f"limit must be an integer from 1 to {MAX_READ_LIMIT}")
        if not _is_int(offset) or not 0 <= offset <= MAX_ENTRIES:
            raise _invalid(f"offset must be an integer from 0 to {MAX_ENTRIES}")

        def wanted(entry: Dict[str, Any]) -> bool:
            if status == "deleted":
                return bool(entry.get("deleted_at"))
            return not entry.get("deleted_at") and (status == "all" or entry["status"] == status)

        def view(doc: Dict[str, Any]) -> Dict[str, Any]:
            root = resolve_group(doc, ref) if ref else None
            rows = _tree_rows(doc, root["id"] if root else None)
            if root is not None and not subtree:
                rows = rows[:1]
            grouped = _entries_by_group(doc)
            paths = _paths(doc)
            selected = [entry for row in rows for entry in grouped.get(row["id"], []) if wanted(entry)]
            return {
                "scope": paths[root["id"]] if root else "all groups",
                "groups": rows[:200],
                "groups_truncated": len(rows) > 200,
                "entries": [_entry_view(entry, paths) for entry in selected[offset:offset + limit]],
                "offset": offset,
                "next_offset": min(len(selected), offset + limit),
                "total": len(selected),
                "truncated": len(selected) > offset + limit,
            }

        return self.store.read(view)

    def select(self, group: Any, *, offset: Any = 0, limit: Any = MAX_READ_LIMIT) -> Dict[str, Any]:
        """Read-only: open entries of a group and all its descendants, in tree order."""
        ref = _ref_text(group)
        if not _is_int(offset) or not 0 <= offset <= MAX_ENTRIES:
            raise _invalid(f"offset must be an integer from 0 to {MAX_ENTRIES}")
        if not _is_int(limit) or not 1 <= limit <= MAX_READ_LIMIT:
            raise _invalid(f"limit must be an integer from 1 to {MAX_READ_LIMIT}")

        def view(doc: Dict[str, Any]) -> Dict[str, Any]:
            root = resolve_group(doc, ref)
            paths = _paths(doc)
            grouped = _entries_by_group(doc)
            order = _tree_order(doc, root["id"])
            entries = [
                {"entry_id": entry["id"], "text": entry["text"], "group_path": paths[group["id"]],
                 "due": entry.get("due")}
                for group, _depth in order
                for entry in grouped.get(group["id"], [])
                if entry["status"] == "open" and not entry.get("deleted_at")
            ]
            total = len(entries)
            return {
                "read_only": True,
                "scope": _group_view(root, paths),
                "groups_included": [paths[group["id"]] for group, _depth in order],
                "entries": entries[offset:offset + limit],
                "count": min(max(total - offset, 0), limit),
                "offset": offset,
                "next_offset": min(total, offset + limit),
                "total": total,
                "truncated": total > offset + limit,
            }

        return self.store.read(view)

    def overview(self, *, open_limit: int = 200, done_limit: int = 50, deleted_limit: int = 50) -> Dict[str, Any]:
        def view(doc: Dict[str, Any]) -> Dict[str, Any]:
            rows = _tree_rows(doc)
            grouped = _entries_by_group(doc)
            paths = _paths(doc)
            ordered = [entry for row in rows for entry in grouped.get(row["id"], [])]
            live = [entry for entry in ordered if not entry.get("deleted_at")]
            open_entries = [entry for entry in live if entry["status"] == "open"]
            done_entries = sorted(
                (entry for entry in live if entry["status"] == "done"),
                key=lambda entry: (entry.get("completed_at") or "", entry["seq"]),
                reverse=True,
            )
            deleted_entries = sorted(
                (entry for entry in ordered if entry.get("deleted_at")),
                key=lambda entry: (entry["deleted_at"], entry["seq"]),
                reverse=True,
            )
            return {
                "tree": rows,
                "open": [_entry_view(entry, paths) for entry in open_entries[:open_limit]],
                "open_total": len(open_entries),
                "done": [_entry_view(entry, paths) for entry in done_entries[:done_limit]],
                "done_total": len(done_entries),
                "deleted": [_entry_view(entry, paths) for entry in deleted_entries[:deleted_limit]],
                "deleted_total": len(deleted_entries),
            }

        return self.store.read(view)
