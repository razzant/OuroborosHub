"""The one seam between the Memory Atlas routes and memory.

A reader answers the operations of the ``memory-atlas.read/2`` contract (``CONTRACT.md`` beside the
package in the development repository). ``CoreReader`` is the production reader: every answer is the
host's own ``api.read_memory`` (PluginAPI 2.1, contract ``ouroboros.memory/1``), which names the data
root, writes nothing and interprets the chronicle, knowledge and plain sources by their owners. This
module only chooses which core reads one route makes and passes their answers on; it parses no
journal, opens no index and decides nothing about what a record means.

``UnconnectedReader`` answers a host without ``read_memory``: every operation is refused with
``reader_not_connected`` so the widget says it cannot read memory instead of showing a guess. (A
manifest declaring ``plugin_api: "2.1"`` is refused by an older host before this is reached.)

``mode`` names where a reader's words come from: ``"core"`` for this installation's memory and
``"none"`` for no reader. The widget marks every mode other than ``"core"`` as not your memory.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

MEMORY_CONTRACT = "ouroboros.memory/1"
OVERVIEW_ROOMS, OVERVIEW_SHELVES, OVERVIEW_DOCUMENTS = 200, 100, 100
ENTRIES_MAX = 20  # record ids one ``entries`` call resolves (each one record-sized read)
PREVIEW_CHARS = 280  # the core's list preview, in code points

# The core's refusal codes and the HTTP status each becomes.
CORE_STATUS = {"invalid_parameter": 400, "not_found": 404, "revision_mismatch": 409,
               "position_mismatch": 409, "unavailable": 503}


class ReaderError(Exception):
    """A refusal the routes report as ``{"error": {"code", "message", ...}}``."""

    status = 500
    code = "reader_failed"

    def __init__(self, message: str, *, code: Optional[str] = None, status: Optional[int] = None,
                 current_revision: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.current_revision = current_revision


class ReaderUnavailable(ReaderError):
    status = 503
    code = "reader_not_connected"


class NotFound(ReaderError):
    status = 404
    code = "not_found"


class BadRequest(ReaderError):
    status = 400
    code = "bad_request"


def _core_refusal(exc: BaseException) -> Optional[ReaderError]:
    """A ``MemoryReadError`` as this contract's error; anything else is not a refusal (None).

    Matched by its shape (class name and a known ``code``) so this module imports nothing from the core.
    """
    code = getattr(exc, "code", None)
    if type(exc).__name__ != "MemoryReadError" or code not in CORE_STATUS:
        return None
    return ReaderError(str(getattr(exc, "message", "") or exc), code=code, status=CORE_STATUS[code],
                       current_revision=getattr(exc, "current_revision", None))


def _strip(answer: Mapping[str, Any]) -> Dict[str, Any]:
    """A core answer without its envelope: the route's envelope names both contracts."""
    return {key: value for key, value in answer.items() if key not in ("contract", "operation")}


class CoreReader:
    """``api.read_memory`` behind the routes: one or a few core reads per route, never a scan of its own."""

    mode = "core"
    label = "This installation's memory, read by the host (PluginAPI 2.1, ouroboros.memory/1)"

    def __init__(self, read_memory: Callable[..., Dict[str, Any]]) -> None:
        self._read_memory = read_memory

    def call(self, operation: str, parameters: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            answer = self._read_memory(operation, dict(parameters))
        except Exception as exc:
            refusal = _core_refusal(exc)
            if refusal is None:
                raise
            raise refusal from None
        if not isinstance(answer, Mapping) or answer.get("contract") != MEMORY_CONTRACT:
            raise ReaderError("the host answered outside " + MEMORY_CONTRACT, code="contract_mismatch")
        return _strip(answer)

    def catalog(self, area: str, **params: Any) -> Dict[str, Any]:
        return self.call("catalog", {"area": area, **params})

    # --- routes -------------------------------------------------------------------------------------

    def overview(self) -> Dict[str, Any]:
        """The list of places: the first pages of rooms, shelves and plain sources, and a change baseline."""
        rooms = self.catalog("rooms", limit=OVERVIEW_ROOMS)
        shelves = self.catalog("shelves", limit=OVERVIEW_SHELVES)
        documents = self.catalog("documents", limit=OVERVIEW_DOCUMENTS)
        baseline = self.call("changes", {})
        return {"memory_contract": MEMORY_CONTRACT, "chronicle": rooms.pop("chronicle", None),
                "rooms": rooms, "shelves": shelves, "documents": documents,
                "position": baseline.get("position"), "since_known": baseline.get("since_known")}

    def list(self, area: str, **params: Any) -> Dict[str, Any]:
        return {"memory_contract": MEMORY_CONTRACT, **self.catalog(area, **params)}

    def text(self, **params: Any) -> Dict[str, Any]:
        return {"memory_contract": MEMORY_CONTRACT, **self.call("document", params)}

    def entries(self, ids: List[str]) -> Dict[str, Any]:
        """Each named record as a list shows it (its document's ``record``, label and teller), or missing.

        A record document carries no list preview, so the first range of its text stands in: the composed
        text begins with the record's own words (``chars`` of them), and only those are kept.
        """
        out = []
        for ident in ids[:ENTRIES_MAX]:
            try:
                answer = self.call("document", {"address": {"kind": "record", "id": ident}, "length": PREVIEW_CHARS})
            except ReaderError as exc:
                if exc.code != "not_found":
                    raise
                out.append({"id": ident, "missing": True})
                continue
            entry = dict(answer.get("record") or {})
            own = entry.get("chars")
            entry.update(room_label=answer.get("room_label"), told_by=answer.get("told_by"),
                         current_revision=answer.get("current_revision"),
                         preview=str(answer.get("text") or "")[:own if isinstance(own, int) else PREVIEW_CHARS])
            # A dated record's period stands at the document's top level, as its list entry carries it.
            entry.update({key: answer[key] for key in ("period", "source_period", "period_basis") if key in answer})
            out.append({"id": ident, "missing": False, "record": entry})
        return {"memory_contract": MEMORY_CONTRACT, "entries": out}

    def changes(self, position: Optional[Dict[str, Any]], limit: Optional[int] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if position is not None:
            params["position"] = position
        if limit is not None:
            params["limit"] = limit
        return {"memory_contract": MEMORY_CONTRACT, **self.call("changes", params)}

    def search(self, q: str, cursor: Optional[str]) -> Dict[str, Any]:
        from .atlas_search import search

        return {"memory_contract": MEMORY_CONTRACT, **search(self, q, cursor)}

    def inbound(self, scope: str, topic: str, cursor: Optional[str]) -> Dict[str, Any]:
        from .atlas_search import inbound

        return {"memory_contract": MEMORY_CONTRACT, **inbound(self, scope, topic, cursor)}


class UnconnectedReader:
    """A host without ``read_memory``: every operation is refused, nothing is guessed."""

    mode = "none"
    label = "This host has no supported memory read (PluginAPI 2.1)"
    MESSAGE = ("This Ouroboros has no supported memory read for skills, so Memory Atlas shows nothing "
               "rather than a guess. It needs Ouroboros with PluginAPI 2.1.")

    def _refuse(self, *_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        raise ReaderUnavailable(self.MESSAGE)

    overview = list = text = entries = changes = search = inbound = _refuse


def reader_for(api: Any) -> Any:
    """``CoreReader`` over the host's ``read_memory`` when it has one, else ``UnconnectedReader``."""
    read_memory = getattr(api, "read_memory", None)
    return CoreReader(read_memory) if callable(read_memory) else UnconnectedReader()
