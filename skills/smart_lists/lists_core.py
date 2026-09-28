"""Authoritative skill-local store and list operations for smart_lists.

This module has no host imports so it can be exercised directly. One JSON
document under the skill state directory holds every group, entry and applied
request id. Writers are serialized (an in-process lock plus an advisory file
lock where the platform provides one), reload the document from disk, and
replace it atomically, so agent tools and widget routes share one source of
truth and never observe a partial file.

Policies the owner chose, enforced here rather than in callers:

* entry text is stored exactly as given (no trimming, casing or merging);
* repeated text is never deduplicated silently, only reported;
* a due value is read as an instant only when it is an ISO-8601 date-time with
  an explicit UTC offset, otherwise it is kept as raw text and not interpreted;
* a mutation that carries a ``request_id`` is applied at most once;
* groups form one tree where every group has at most one parent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

try:  # POSIX advisory lock; elsewhere only the in-process lock applies.
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
STORE_FILE = "store.json"
LOCK_FILE = "store.lock"

MAX_GROUPS = 500
MAX_ENTRIES = 5000
MAX_BATCH = 100
MAX_TEXT_CHARS = 1000
MAX_NAME_CHARS = 80
MAX_DUE_CHARS = 120
MAX_REQUESTS = 500
MAX_READ_LIMIT = 500

STATUSES = ("open", "done")
GROUP_ACTIONS = ("create", "rename", "move", "delete")

_GROUP_ID_RE = re.compile(r"^g_[0-9a-f]{10}$")
_ENTRY_ID_RE = re.compile(r"^e_[0-9a-f]{10}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EXPLICIT_OFFSET_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{3}(?:\d{3})?)?)?(?:Z|[+-]\d{2}:\d{2})$"
)
_ITEM_KEYS = {"text", "due"}


class ListError(Exception):
    """A typed, owner-readable refusal. Nothing is written when it is raised."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}


def _invalid(message: str) -> ListError:
    return ListError("invalid_input", message)


def _utf8(value: str, field: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _invalid(f"{field} must be valid Unicode text") from exc
    return value


def _unreadable(detail: str) -> ListError:
    return ListError(
        "store_unreadable",
        f"The list store could not be read ({detail}). It was left untouched; "
        "no change was applied.",
    )


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def empty_document() -> Dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "next_seq": 1, "groups": {}, "entries": {}, "requests": {}}


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


# ---------------------------------------------------------------------------
# Document validation and tree helpers
# ---------------------------------------------------------------------------

def _check_document(doc: Any) -> Dict[str, Any]:
    if not isinstance(doc, dict):
        raise _unreadable("top level is not an object")
    version = doc.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise _unreadable("schema_version is missing")
    if version > SCHEMA_VERSION:
        raise _unreadable(f"schema_version {version} is newer than this skill supports ({SCHEMA_VERSION})")
    groups, entries, requests = doc.get("groups"), doc.get("entries"), doc.get("requests")
    if not all(isinstance(part, dict) for part in (groups, entries, requests)):
        raise _unreadable("groups, entries and requests must be objects")
    if not isinstance(doc.get("next_seq"), int) or isinstance(doc.get("next_seq"), bool):
        raise _unreadable("next_seq is missing")
    for group_id, group in groups.items():
        if (
            not isinstance(group, dict)
            or group.get("id") != group_id
            or not isinstance(group.get("name"), str)
            or not group["name"]
            or (group.get("parent_id") is not None and (
                not isinstance(group.get("parent_id"), str) or group["parent_id"] not in groups
            ))
        ):
            raise _unreadable(f"group {group_id!r} is malformed")
    for group_id in groups:
        seen = set()
        current: Optional[str] = group_id
        while current is not None:
            if current in seen:
                raise _unreadable(f"group {group_id!r} is part of a parent cycle")
            seen.add(current)
            current = groups[current].get("parent_id")
    for entry_id, entry in entries.items():
        due = entry.get("due") if isinstance(entry, dict) else None
        if (
            not isinstance(entry, dict)
            or entry.get("id") != entry_id
            or not isinstance(entry.get("group_id"), str)
            or entry["group_id"] not in groups
            or not isinstance(entry.get("text"), str)
            or entry.get("status") not in STATUSES
            or not isinstance(entry.get("seq"), int)
            or not (due is None or (isinstance(due, dict) and isinstance(due.get("raw"), str)))
        ):
            raise _unreadable(f"entry {entry_id!r} is malformed")
    for request_id, record in requests.items():
        if not isinstance(record, dict) or not isinstance(record.get("result"), dict):
            raise _unreadable(f"request record {request_id!r} is malformed")
    return doc


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


def _entry(doc: Dict[str, Any], entry_id: str) -> Dict[str, Any]:
    entry = doc["entries"].get(entry_id)
    if entry is None:
        raise ListError("not_found", f"unknown entry id {entry_id}")
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
    }


def _group_view(group: Dict[str, Any], paths: Dict[str, str]) -> Dict[str, Any]:
    return {"id": group["id"], "name": group["name"], "path": paths.get(group["id"], group["name"]),
            "parent_id": group.get("parent_id")}


def _journal_result(value: Any, entries: Dict[str, Any]) -> Any:
    """Keep replay identity, never old private entry text or group labels."""
    if isinstance(value, list):
        return [_journal_result(item, entries) for item in value]
    if isinstance(value, dict):
        copy = {key: _journal_result(item, entries) for key, item in value.items()}
        if isinstance(copy.get("id"), str) and copy["id"] in entries:
            copy.pop("text", None)
            copy.pop("due", None)
            copy.pop("group_path", None)
        if isinstance(copy.get("id"), str) and _GROUP_ID_RE.fullmatch(copy["id"]):
            copy.pop("name", None)
            copy.pop("path", None)
        return copy
    return value


def _replay_result(value: Any, doc: Dict[str, Any]) -> Any:
    """Fill private fields from current entries/groups, not stale journal copies."""
    if isinstance(value, list):
        return [_replay_result(item, doc) for item in value]
    if isinstance(value, dict):
        copy = {key: _replay_result(item, doc) for key, item in value.items()}
        entry_id = copy.get("id")
        entry = doc["entries"].get(entry_id) if isinstance(entry_id, str) else None
        if entry is not None:
            current = _entry_view(entry, _paths(doc))
            copy.update(current)
        group = doc["groups"].get(entry_id) if isinstance(entry_id, str) else None
        if group is not None:
            copy.update(_group_view(group, _paths(doc)))
        return copy
    return value


def _tree_rows(doc: Dict[str, Any], root_id: Optional[str] = None) -> List[Dict[str, Any]]:
    paths = _paths(doc)
    grouped = _entries_by_group(doc)
    rows = []
    for group, depth in _tree_order(doc, root_id):
        entries = grouped.get(group["id"], [])
        rows.append({
            "id": group["id"],
            "path": paths[group["id"]],
            "depth": depth,
            "open": sum(1 for entry in entries if entry["status"] == "open"),
            "done": sum(1 for entry in entries if entry["status"] == "done"),
        })
    return rows


# ---------------------------------------------------------------------------
# Document operations (mutate ``doc`` in place; validate before changing it)
# ---------------------------------------------------------------------------

def _op_update(doc: Dict[str, Any], entry_id: str, text: Optional[str], due: Optional[Dict[str, str]],
               clear_due: bool, now: str) -> List[str]:
    entry = _entry(doc, entry_id)
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
    entries = [_entry(doc, entry_id) for entry_id in entry_ids]
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
    entries = [_entry(doc, entry_id) for entry_id in entry_ids]
    moved, unchanged = [], []
    for entry in entries:
        if entry["group_id"] == target["id"]:
            unchanged.append(entry["id"])
            continue
        entry["group_id"] = target["id"]
        entry["updated_at"] = now
        moved.append(entry)
    return moved, unchanged


def _possible_duplicates(doc: Dict[str, Any], created: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Report (never merge) open entries in the same group with the same text."""
    report = []
    for entry in created:
        key = _norm(entry["text"])
        matches = [
            other["id"]
            for other in sorted(doc["entries"].values(), key=lambda item: item["seq"])
            if other["id"] != entry["id"]
            and other["group_id"] == entry["group_id"]
            and other["status"] == "open"
            and _norm(other["text"]) == key
        ]
        if matches:
            report.append({"entry_id": entry["id"], "same_text_open_entries": matches})
    return report


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class ListStore:
    """The single authoritative JSON document under the skill state dir."""

    def __init__(self, state_dir: Any) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / STORE_FILE
        self._lock_path = self.state_dir / LOCK_FILE
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with open(self._lock_path, "a+", encoding="utf-8") as handle:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load(self) -> Dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return empty_document()
        except (OSError, UnicodeDecodeError) as exc:
            raise _unreadable(type(exc).__name__) from exc
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise _unreadable(f"invalid JSON: {exc}") from exc
        return _check_document(doc)

    def _save(self, doc: Dict[str, Any]) -> None:
        doc["updated_at"] = _now()
        text = json.dumps(doc, ensure_ascii=False, indent=1) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".store-", suffix=".tmp", dir=str(self.state_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

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
            doc = self._load()
            if request_id:
                prior = doc["requests"].get(request_id)
                if prior is not None:
                    if prior.get("op") == op and prior.get("fingerprint") == fingerprint:
                        return dict(_replay_result(prior["result"], doc), replayed=True)
                    raise ListError(
                        "request_conflict",
                        f"request_id {request_id!r} was already used for a different change; "
                        "use a new request_id for a new request",
                    )
            result = fn(doc)
            if request_id:
                doc["requests"][request_id] = {"op": op, "fingerprint": fingerprint, "at": _now(),
                                                "result": result}
                while len(doc["requests"]) > MAX_REQUESTS:
                    doc["requests"].pop(next(iter(doc["requests"])))
            # Also remove private fields from journals written by earlier versions.
            for record in doc["requests"].values():
                record["result"] = _journal_result(record["result"], doc["entries"])
            self._save(doc)
        return dict(result, replayed=False)


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
                raise ListError("limit", f"the store holds at most {MAX_ENTRIES} entries")
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
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": None,
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
        if not isinstance(clear_due, bool):
            raise _invalid("clear_due must be true or false")
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
        if not isinstance(done, bool):
            raise _invalid("done must be true or false")
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

    def edit(self, entry_id: Any, *, text: Any = "", due: Any = "", clear_due: bool = False,
             status: Any = "", move_to: Any = "", request_id: Any = "") -> Dict[str, Any]:
        """Widget form: update, complete/reopen and move one entry in one atomic write."""
        ids = clean_entry_ids(entry_id)
        if len(ids) != 1:
            raise _invalid("edit changes exactly one entry")
        new_text = clean_text(text) if text not in (None, "") else None
        new_due = parse_due(due)
        if not isinstance(clear_due, bool):
            raise _invalid("clear_due must be true or false")
        if new_due is not None and clear_due:
            raise _invalid("give either due or clear_due, not both")
        status = _optional_ref(status, field="status")
        if status not in ("",) + STATUSES:
            raise _invalid("status must be open or done")
        destination = _optional_ref(move_to, field="move_to")
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
                if held:
                    raise ListError(
                        "conflict",
                        f"{paths[subject['id']]!r} still holds {len(held)} entries (open or done); move them first",
                    )
                del doc["groups"][subject["id"]]
                return {"op": "group.delete", "deleted": _group_view(subject, paths)}
            return {"op": f"group.{action}", "group": _group_view(subject, _paths(doc))}

        return self.store.mutate(f"group.{action}", params, apply, request_id)

    # -- reads ---------------------------------------------------------------

    def read(self, *, group: Any = "", status: Any = "open", subtree: Any = True, limit: Any = 100) -> Dict[str, Any]:
        ref = _optional_ref(group, field="group")
        if status not in ("open", "done", "all"):
            raise _invalid("status must be open, done or all")
        if not isinstance(subtree, bool):
            raise _invalid("subtree must be true or false")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_READ_LIMIT:
            raise _invalid(f"limit must be an integer from 1 to {MAX_READ_LIMIT}")

        def view(doc: Dict[str, Any]) -> Dict[str, Any]:
            root = resolve_group(doc, ref) if ref else None
            rows = _tree_rows(doc, root["id"] if root else None)
            if root is not None and not subtree:
                rows = rows[:1]
            grouped = _entries_by_group(doc)
            paths = _paths(doc)
            selected = [
                entry
                for row in rows
                for entry in grouped.get(row["id"], [])
                if status == "all" or entry["status"] == status
            ]
            return {
                "scope": paths[root["id"]] if root else "all groups",
                "groups": rows[:200],
                "groups_truncated": len(rows) > 200,
                "entries": [_entry_view(entry, paths) for entry in selected[:limit]],
                "total": len(selected),
                "truncated": len(selected) > limit,
            }

        return self.store.read(view)

    def select(self, group: Any) -> Dict[str, Any]:
        """Read-only: open entries of a group and all its descendants, in tree order."""
        ref = _ref_text(group)

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
                if entry["status"] == "open"
            ]
            total = len(entries)
            return {
                "read_only": True,
                "scope": _group_view(root, paths),
                "groups_included": [paths[group["id"]] for group, _depth in order],
                "entries": entries[:MAX_READ_LIMIT],
                "count": min(total, MAX_READ_LIMIT),
                "total": total,
                "truncated": total > MAX_READ_LIMIT,
            }

        return self.store.read(view)

    def overview(self, *, open_limit: int = 200, done_limit: int = 50) -> Dict[str, Any]:
        def view(doc: Dict[str, Any]) -> Dict[str, Any]:
            rows = _tree_rows(doc)
            grouped = _entries_by_group(doc)
            paths = _paths(doc)
            ordered = [entry for row in rows for entry in grouped.get(row["id"], [])]
            open_entries = [entry for entry in ordered if entry["status"] == "open"]
            done_entries = sorted(
                (entry for entry in ordered if entry["status"] == "done"),
                key=lambda entry: (entry.get("completed_at") or "", entry["seq"]),
                reverse=True,
            )
            return {
                "tree": rows,
                "open": [_entry_view(entry, paths) for entry in open_entries[:open_limit]],
                "open_total": len(open_entries),
                "done": [_entry_view(entry, paths) for entry in done_entries[:done_limit]],
                "done_total": len(done_entries),
            }

        return self.store.read(view)
