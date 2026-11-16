"""Synthetic transferred records for the Memory Atlas consumer tests. Development only; nobody's memory.

Written into a running test host's data root by the core's own producers, in the importer's documented order:

1. ``chronicle_import.ensure_activated(store, wait=True)``: this installation's own old dialogue memory first;
2. ``chat_chain.retain_memory_source`` for the exact bytes of every source, read back and compared at once;
3. ``ChronicleStore.publish`` of the records in the source's own order (stable ids, so a repeat is ``unchanged``),
   then ``ChronicleStore.publish_part`` over three legacy records with block and stated period bases.
   One deliberate exception: ``tr-period-basic`` restores a synthetic historical journal transaction and reindexes
   it through the core. Basic ISO was publishable on Python 3.11+, but not 3.10; both must read that saved history.
   This is not a new-publication example or a substitute reader. Sources are retained/read back exactly as above.

The records (ids ``tr-*``, one old-style retelling ``old-block-synth``):

``tr-source``          known author and model, signed in the source; written 2026-03-03, events of 10-14 Nov 2025
``tr-importer``        author stated by the importer, model unknown; written with a time; no period stated
``tr-unknown-author``  author unknown, model known, stated by the importer; an open, incomplete stated period
``tr-all-unknown``     nothing known (room ``legacy``): author, model, operator, tool, task, written date, period
``tr-moved-helper``    the old helper kind carrying a transfer (an old retelling brought here)
``tr-special``         HTML, newline and very long names in label, model, operator, tool and task; a body with markup
``tr-period-budget``   an old helper's extra fields exhaust the normal entry budget inside its stated period
``tr-period-basic``    a restored historical basic ISO period that the widget cannot format itself
``tr-period-offset``   positive/negative offsets crossing UTC days; the widget must keep the core's period
``tr-mix-rows``        a stated-period transfer (historical fixture id); foreign coordinates never bind local rows
``tr-mix-stated``      a transfer with only a stated period
``old-block-synth``    an old-style retelling (historical helper, no transfer) whose block period is recorded
``tr-mix-part``        the part folding the last three (id read from the answer): block and stated periods

``write(root)`` returns the ids and the facts the tests compare against. The suite writes it through the ``transfers``
fixture (``conftest.ensure_transfers``, a child of the host's own isolated environment); to LOOK at a host started by
``dev/realhost.py`` (its run directory ``<run>`` holds ``repo`` and ``data``), attach the fixture to it and leave it running::

    ATLAS_HOST_ROOT=<run> python -m pytest -p no:cacheprovider -q tests/memory_atlas/test_widget_transfer.py -k author_filter

or, without pytest and outside that environment (not verified here), by hand::

    python3 -I -B tests/memory_atlas/dev/transfer_fixture.py --core <run>/repo --data <run>/data

The module's top level imports nothing of the core; ``write`` needs a core whose chronicle accepts
``metadata.transfer`` and the author kind ``source_author``. A core that refuses them stops here with its refusal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from types import SimpleNamespace
from typing import Any, Dict, List

TEST_LABEL = "Synthetic test text, not anyone's memory."
SOURCE_TASK = "t-synthetic-import"   # the task under which every retained source lives (an artifact directory of its own)
MIND = {"kind": "mind", "task_id": "t-main", "focus": {"role": "main chat", "task_id": "t-main"}}
TRANSFER = {"operator": "synthetic-operator", "tool": "synthetic-importer 1.0", "task_id": SOURCE_TASK}
UNKNOWN_TRANSFER = {"operator": None, "tool": None, "task_id": None}
UNKNOWN_RANGE = {"status": "unknown", "pos": None, "first": None, "last": None, "ts_span": None}

BODY_SPECIAL = "A <b>tag</b>, \"quotes\" & an ampersand.\n\n    indented line\twith a tab\nLast line, no final newline"
LONG_NAME = "Synthetic Writer With A Very Long Display Name " + "Unbroken" * 40
# Markup that would run if a name were ever parsed as HTML: each sets a flag on the page's window.
XSS_IMG = "<img src=x onerror=\"window.__atlasXss=1\">"
XSS_SCRIPT = "<script>window.__atlasXss=1</script>"
SPECIAL_AUTHOR_LABEL = "Writer\u202e\u2066\n\t\u0085\u2028\u2029 " + LONG_NAME + " " + XSS_IMG + "\u202E\u2066"
SPECIAL_AUTHOR_MODEL = "model\u2069\u200b\nwith newline " + XSS_SCRIPT + " M" * 180 + "\u2069"
SPECIAL_TRANSFER = {"operator": "Operator\u061c " + XSS_IMG + " & co" + " O" * 180 + "\u202C", "tool": "importer " + XSS_SCRIPT + " 1.0" + " T" * 180,
                    "task_id": "task-<b>" + "x" * 360 + "</b>"}

# The period basis each member of the mixed part is meant to have, and the one the part must then report.
MIX_MEMBERS = {"tr-mix-rows": "stated", "old-block-synth": "block", "tr-mix-stated": "stated"}


def _source_author(label, model, basis) -> Dict[str, Any]:
    return {"kind": "source_author", "label": label, "model": model, "basis": basis}


def _meta(written, *, transfer=None, period=None, **more) -> Dict[str, Any]:
    out: Dict[str, Any] = {"transfer": dict(TRANSFER if transfer is None else transfer), "legacy_written_at": written, **more}
    if period:
        out["stated_period"] = period
    return out


def _transfers() -> List[Dict[str, Any]]:
    """The transfers, in the order they are published (the three of the mixed part are first, so they are the first
    transfers of their room and so adjacent to the last old retelling there)."""
    words = lambda what: what + " " + TEST_LABEL   # noqa: E731
    return [
        {"id": "tr-mix-rows", "room": "1", "text": words("Words with a source-stated period and no binding to this installation's rows."),
         "author": _source_author("Synthetic Local-Rows Writer", "synthetic-model-c", "source"),
         "metadata": _meta("2026-07-01", period={"start": "2025-12-01T00:00:00+00:00", "end": "2025-12-02T00:00:00+00:00",
                                                  "incomplete": False}, legacy_type="chapter")},
        {"id": "tr-mix-stated", "room": "1", "text": words("Words whose source states the period they tell of."),
         "author": _source_author("Synthetic Stated Writer", None, "importer"),
         "metadata": _meta("2026-03-03", period={"start": "2025-11-10T00:00:00+00:00", "end": "2025-11-14T00:00:00+00:00",
                                                  "incomplete": False}, legacy_type="chapter")},
        {"id": "tr-source", "room": "1", "text": words("Words of a known writer."),
         "author": _source_author("Synthetic Writer", "synthetic-model-a", "source"),
         "metadata": _meta("2026-03-03", period={"start": "2025-11-10T00:00:00+00:00", "end": "2025-11-14T00:00:00+00:00",
                                                  "incomplete": False}, label="Chapter one", legacy_type="chapter")},
        {"id": "tr-importer", "room": "1", "text": words("Words whose writer the importer states."),
         "author": _source_author("Importer Stated Writer", None, "importer"),
         "metadata": _meta("2026-03-04T09:30:00+00:00", legacy_type="chapter")},
        {"id": "tr-unknown-author", "room": "1", "text": words("Words of an unknown writer, known model."),
         "author": _source_author(None, "synthetic-model-b", "importer"),
         "metadata": _meta(None, period={"start": "2026-02-01T00:00:00+00:00", "end": None, "incomplete": True})},
        {"id": "tr-all-unknown", "room": "legacy", "text": words("Words of an unknown writer and model."),
         "author": _source_author(None, None, "unknown"), "metadata": _meta(None, transfer=UNKNOWN_TRANSFER)},
        {"id": "tr-moved-helper", "room": "1", "text": words("An old helper's retelling, moved here."),
         "author": None, "metadata": _meta("2026-01-15")},   # author: the importer's own LEGACY_AUTHOR (read from the core)
        {"id": "tr-special", "room": "1", "text": BODY_SPECIAL,
         "author": _source_author(SPECIAL_AUTHOR_LABEL, SPECIAL_AUTHOR_MODEL, "source"),
         "metadata": _meta("2026-03-06", transfer=SPECIAL_TRANSFER)},
        {"id": "tr-period-budget", "room": "1", "text": words("A complete period with projected fields."),
         # memory_reader: ENTRY_BUDGET=3000, PREVIEW_CHARS=280; _project charges 8 per value plus keys/text.
         # Sorted stored keys leave 342 after author, 70 after transfer (operator padding 220), 52 after written_at.
         # stated_period's object/keys leave 26: end costs 18 -> 8, incomplete costs 8 -> 0 (ref), start -> ref.
         # Deliberately pins this nested-ref shape; if projection changes, retune here, never weaken the false check.
         "author": {"kind": "legacy_helper", "writer": "w", **{f"x{i}": "x" * 280 for i in range(9)}},
         "metadata": _meta("2026-08-10", transfer={"operator": "o" * 220, "tool": "s", "task_id": None},
                           period={"start": "2026-07-01", "end": "2026-07-31", "incomplete": False})},
        {"id": "tr-period-basic", "room": "1", "text": words("A complete period in basic ISO notation."),
         "author": _source_author("Basic ISO Writer", None, "source"),
         "metadata": _meta("2026-08-10", period={"start": "20260701", "end": "20260731", "incomplete": False})},
        {"id": "tr-period-offset", "room": "1", "text": words("A complete period crossing UTC day boundaries."),
         "author": _source_author("Offset Writer", None, "source"),
         "metadata": _meta("2026-08-10", period={"start": "2026-07-02T00:30:00+03:00",
                                                 "end": "2026-07-31T23:30:00-03:00", "incomplete": False})},
    ]


def _retain(root: pathlib.Path, source_id: str, data: bytes, extension: str) -> Dict[str, Any]:
    """The exact bytes in the core's own immutable source storage, read back and compared before any record cites them."""
    from ouroboros import chat_chain
    from ouroboros.artifacts import read_actor_source_bytes

    ref = chat_chain.retain_memory_source(SimpleNamespace(drive_root=root, task_id=SOURCE_TASK), source_id, data, extension)
    if read_actor_source_bytes(root, SOURCE_TASK, ref) != data or ref.get("sha256") != hashlib.sha256(data).hexdigest():
        raise RuntimeError(f"the retained source {source_id} does not read back as the bytes written")
    return ref


def _published(result: Any, what: str) -> Any:
    if not result.ok:
        raise RuntimeError(f"the core refused {what}: {result}")
    return result


def write(root: Any) -> Dict[str, Any]:
    """Write the fixture into the data root of a running test host and return its ids and facts (see the module)."""
    from ouroboros.chronicle_import import LEGACY_AUTHOR, ensure_activated
    from ouroboros.chronicle_store import ChronicleStore
    from ouroboros.utils import append_jsonl

    root = pathlib.Path(root).resolve()
    store = ChronicleStore(root)
    receipt = ensure_activated(store, wait=True)   # 1. the one-time import of this installation's own old dialogue memory
    if receipt.get("kind") in ("import_pending", "import_refused"):
        raise RuntimeError(f"the legacy import did not finish: {receipt}")

    # 2. the exact bytes of every source (its own words), then the old retelling's block file
    specs = _transfers()
    refs: Dict[str, Dict[str, Any]] = {}
    sources: Dict[str, Dict[str, Any]] = {}
    for spec in specs:
        data = spec["text"].encode("utf-8")
        refs[spec["id"]] = _retain(root, "synthetic_" + spec["id"], data, "md")
        sources[spec["id"]] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "task_id": SOURCE_TASK,
                               "path": refs[spec["id"]]["path"]}
    earlier = [(r.get("metadata") or {}).get("legacy_block") for r in store.records("1", kinds=("legacy",)) if r["id"] != "old-block-synth"]
    block = max([b for b in earlier if type(b) is int], default=-1) + 1
    old_blocks = json.dumps([{"ts": "2026-04-30T10:00:00+00:00", "type": "summary", "range": "2026-04-01 00:00 - 2026-04-30 00:00",
                              "message_count": 0, "content": "### Synthetic old block", "rooms": []}]).encode("utf-8")
    old_ref = _retain(root, "synthetic_old_blocks", old_blocks, "json")
    old_text = "An old helper's retelling of one period, written before any transfer existed. " + TEST_LABEL

    def record(spec: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": spec["id"], "kind": "legacy", "room_id": spec["room"], "text": spec["text"],
                "author": spec["author"] or dict(LEGACY_AUTHOR), "source_refs": [{**refs[spec["id"]], "location": "whole file"}],
                "covers": {"room_id": spec["room"], "raw_range": UNKNOWN_RANGE}, "metadata": spec["metadata"]}

    old_block = {"id": "old-block-synth", "kind": "legacy", "room_id": "1", "text": old_text, "author": dict(LEGACY_AUTHOR),
                 "source_refs": [{**old_ref, "location": str(block)}],
                 # a block whose period is recorded but whose room holds no rows of it: its period stays the block's
                 "covers": {"room_id": "1", "raw_range": {"status": "exact", "pos": [2, 2], "first": None, "last": None,
                                                          "ts_span": {"start": "2026-04-01T00:00:00+00:00",
                                                                      "end": "2026-04-30T00:00:00+00:00", "incomplete": False}}},
                 "metadata": {"legacy_type": "summary", "legacy_block": block, "label": "Synthetic old block",
                              "legacy_range_text": "2026-04-01 00:00 - 2026-04-30 00:00", "legacy_message_count": 0,
                              "room_message_count": 0, "legacy_written_at": "2026-04-30T10:00:00+00:00",
                              "legacy_sha": hashlib.sha256(old_blocks).hexdigest(), "legacy_source_ref": None,
                              "legacy_gap_id": ""}}
    by_id = {spec["id"]: spec for spec in specs}

    # 3. the records, in the source's own order: the three the part folds first, then the part, then the others
    _published(store.publish([old_block, record(by_id["tr-mix-rows"]), record(by_id["tr-mix-stated"])]), "the mixed members")
    members = ["old-block-synth", "tr-mix-rows", "tr-mix-stated"]
    held = [p for p in store.records("1", kinds=("part",)) if set(members) <= set((p.get("covers") or {}).get("member_ids") or [])]
    if held:
        part = held[0]
    else:
        part = _published(store.publish_part(
            room_id="1", member_ids=members, author=MIND, expected_sequence=store.room_head("1"),
            text="A part folding an old block and two transfers with stated periods. " + TEST_LABEL),
            "the mixed part").record
    _published(store.publish([record(spec) for spec in specs if spec["id"] not in [*members, "tr-period-basic"]]),
               "the other transfers")

    # Like core test_historical_nonlegacy_transfer_is_replayed_not_revalidated: restore saved history, not publish
    # unsupported grammar. Keep the running host's index: its normal locked catch-up replays the appended transaction.
    historical = {**record(by_id["tr-period-basic"]), "ts": "2026-08-10T00:00:00+00:00"}
    with store._publishing() as db:
        if store._get(db, historical["id"]) is None:
            if not append_jsonl(store.log_path, {"kind": "transaction", "schema": 1, "records": [historical],
                                                "scan_state": {}}, ensure_record_boundary=True, require_lock=True):
                raise RuntimeError("the historical basic ISO transaction was not restored")
            store._catch_up(db)
        replayed = store._get(db, historical["id"])
        if replayed is None or any(replayed.get(key) != value for key, value in historical.items()):
            raise RuntimeError("the historical basic ISO record did not replay unchanged")

    ids: Dict[str, Any] = {spec["id"]: spec["id"] for spec in specs}
    ids.update({"old-block-synth": "old-block-synth", "tr-mix-part": part["id"], "tr-mix-members": members,
                "tr-mix-bases": MIX_MEMBERS, "tr-sources": sources, "tr-special-text": BODY_SPECIAL,
                "tr-special-author": {"label": SPECIAL_AUTHOR_LABEL, "model": SPECIAL_AUTHOR_MODEL},
                "tr-special-transfer": SPECIAL_TRANSFER, "tr-source-task": SOURCE_TASK})
    return ids


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--core", required=True, type=pathlib.Path, help="the core checkout the host runs (importable)")
    ap.add_argument("--data", required=True, type=pathlib.Path, help="the host's data root")
    args = ap.parse_args()
    sys.path.insert(0, str(args.core.resolve()))
    print(json.dumps(write(args.data), ensure_ascii=False))


if __name__ == "__main__":
    main()
