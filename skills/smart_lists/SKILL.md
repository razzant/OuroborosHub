---
name: smart_lists
description: "Personal lists in one skill-local store: a free group tree, verbatim entries captured from clear owner intent in chat, completion, moves and an undoable trash, a read-only subtree selection, checksummed export/restore with typed refusals for a missing store, and a Today checklist widget with local-day rollover. No network, purchases or reminders."
version: 0.3.3
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [tool, route, widget]
env_from_settings: []
when_to_use: The owner clearly intends to capture a list note (even without saying 'add'), check or change list entries, mark items done, delete or restore entries, reorganise list groups, gather the open entries of a group (for example before a shopping run), or back up and restore their lists. Do not infer capture from passing conversation or trigger purchase, ordering, reminder or other action skills.
model_experience:
  what_model_sees: Nine small Smart Lists tools (add, read, update, complete, move, delete, group, select, store) returning compact JSON.
  token_effect: Small fixed schema cost per round while enabled; tool JSON stays below the host 15k cap with visible omitted counts. Read and subtree selection support offset pagination.
timeout_sec: 30
ui_tab:
  tab_id: lists
  title: Smart Lists
  icon: 📋
  render:
    kind: module
    entry: widget.js
    start: auto
    appearance: host
    span: 2
    height: 560
---

# Smart Lists

Smart Lists keeps personal lists — groceries, errands, packing, anything —
in one authoritative store inside this skill's state directory. The agent adds
entries when the owner clearly asks in chat, and a Today checklist widget
shows and edits the same data.

Smart Lists does not import lists from another app. Each entry belongs to
exactly one group; groups may be nested and moved. An import/sync layer and
multi-list labels are deliberately outside this version.

## Model

- **Groups** form one editable tree. Every group has at most one parent;
  top-level groups are the lists, children are sub-lists
  (`Home / Groceries / Dairy`). Names are unique among siblings
  (case-insensitive) and cannot contain `/`, which separates path segments.
  A group is addressed by id (`g_…`) or by its path.
- **Entries** hold the item text, an `open`/`done` status, an optional due
  value, their group, timestamps and, once deleted, `deleted_at`. Entry ids
  look like `e_…`.

## Owner policies enforced by the store

| Policy | Behavior |
| --- | --- |
| Capture on clear intent only | The agent calls `add` when the owner clearly intends note capture, even without the word “add”; there is no passive listener or chat subscription. Passing conversation does not trigger capture or action skills. |
| Raw text | Entry text is stored exactly as given — no trimming, recasing, splitting or quantity parsing. |
| No silent dedup | Adding text that is already open in the same group creates a second entry; the result lists it under `possible_duplicates` so the agent can mention it: per new entry, the 5 oldest matching open entry ids plus `total` and `truncated`, so the report stays small however many copies exist. |
| Dates | A due value is read as an instant only when it is an ISO-8601 date-time with an explicit offset (`2026-10-01T18:00+03:00`, `…Z`); it is then stored as `at` next to the raw text. Anything else (`tomorrow`, `Friday`, `2026-10-01`) is kept only as raw text and never interpreted. No reminders are created. |
| Idempotent requests | Mutations accept a `request_id`; the agent `add` tool requires one. A retry with the same id and arguments returns a replay result (`replayed: true`) without applying anything; entry fields show their current values, and an entry erased since then shows as `{"id", "gone": true}`. The same id with different arguments is refused (`request_conflict`). See [Replay window](#replay-window) for how long ids are remembered. |

## Store lifecycle

| State (`store` → `status`) | Meaning | What every other tool does |
| --- | --- | --- |
| `uninitialized` | No `store.json` and no sign that one was ever saved in this state directory: a first run, or a directory that was wiped (uninstall, reinstall). The two cannot be told apart from inside the directory. | Refuses with `store_not_initialized`. The agent asks the owner whether they have an export to restore, then calls `restore` or `init`. |
| `ready` | `store.json` is readable and matches the last save's byte digest (or, on older sentinels without a digest, saved counts). | Works normally. |
| `missing` | `store.json` is gone but the sentinel shows it was saved here. | Refuses with `store_missing` (with the last known counts). No empty store is created in its place. `restore` recovers; `init` needs `replace: true`. |
| `mismatch` | `store.json` holds an older generation or another store's id, or its bytes differ from the last recorded save without a matching interrupted-write record. A malformed sentinel is also insufficient to certify its contents. | Refuses with `store_mismatch` and leaves the file untouched. `restore` with `replace: true` backs the file up and installs the chosen export (which may be that same file). |
| `unreadable` | Invalid JSON, invalid Unicode, a newer schema, or a document that fails [validation](#validation). | Refuses with `store_unreadable` and leaves the file untouched. |

The sentinel is `store_meta.json` next to the store. It holds the store id,
the generation (a counter bumped on every save), the SHA-256 of the exact saved
`store.json` bytes, the time and counts of the last save and the last export,
and no entry text or group names. Older sentinels have no byte digest: their
saved counts catch entry loss but cannot detect same-count content corruption
until a verified read followed by a new save or export establishes the digest. While a
save is in flight it also names the exact file being replaced (`pending`:
store id, generation, SHA-256).

Two refusals are about access rather than content, and change nothing:
`store_io` (an operating-system error reading or writing the store or the
sentinel, for example a full disk or missing permission) and `store_busy`
(another process held the store lock for more than 10 seconds).

### Writes and failures

Every change is written in three steps: the sentinel with the new
generation and `pending`, then `store.json` (temporary file, `fsync`,
atomic replace, directory `fsync` on POSIX), then the sentinel again
without `pending`. The replace is the commit point.

- If the first sentinel write fails, nothing changed (`store_io`).
- If the store write fails, the previous sentinel is put back and the
  change is refused (`store_io`); the store stays `ready`. If putting it
  back fails too, or the process dies between the two writes, the sentinel's
  `pending` record matches the untouched `store.json` byte for byte, so it
  loads as the last committed state instead of a false `mismatch`. Any
  other older copy is still a `mismatch`.
- If only the last sentinel write fails, the change is committed and
  reported as done; the announced digest still matches the new file, while
  the stale `pending` record matches only the file this write replaced and
  is cleared by the next save.
- A failed first `init` removes its sentinel again, so the store stays
  `uninitialized` rather than looking `missing`.

## Export and restore

- **Export** (`store` → `export`) writes one complete JSON file to
  `backups/export-<UTC time>-<random>.json` in the state directory and
  returns its path. It contains the whole store document — groups, every
  entry including the trash, due values, timestamps and the request journal —
  plus `counts` and a SHA-256 checksum of the document. The widget's low-emphasis Download button uses the retained `GET export` route; the agent can also
  export using the `store` tool and help save a copy outside the skill directory.
  An older store's request journal is sanitized in the exported copy, without
  rewriting the live file: an erased entry's former plaintext must not travel
  into a new backup through the journal.
- **Restore** (`store` → `restore`) takes an absolute path or a bare file name
  from the backups folder (tools). The retained `POST store` route also accepts pasted export text. It accepts
  a Smart Lists export, whose checksum and counts must match, or a bare
  `store.json` document. It refuses (`invalid_export`) and writes nothing for
  anything else: invalid JSON (including nesting too deep to parse), text
  that is not valid Unicode (an unpaired surrogate, pasted or escaped as
  `\ud800`), a changed or truncated export, a newer export or schema
  version, or a document that fails [validation](#validation).
- **Recording an export** in the sentinel happens after the export exists.
  If only that write fails, the export is still delivered: the tool returns
  `export_recorded: false` with a `warning`, the download completes, and
  `status` (`export_unrecorded`) and the widget show a warning until an
  export is recorded again. The flag lives in memory, because the disk just
  refused a write.
- **Never a silent overwrite.** If `store.json` exists — readable or not —
  `restore` and `init` refuse with `store_exists`, naming what would be
  replaced and what would replace it. With `replace: true` the current file is
  first copied byte for byte to `backups/pre-restore-…json` (or
  `pre-init-…`), and only then replaced. Restoring into an `uninitialized` or
  `missing` store needs no flag, because nothing is overwritten.
- After a restore the store keeps the export's store id, and its generation
  continues above every earlier save, so the sentinel accepts it. Request ids
  from a **verified** replaced store that the export does not know are kept as
  expired ids, so a late retry is refused rather than applied again. A
  mismatched, unreadable or unmarked replaced store is backed up but its replay
  filter and ids are not trusted or merged: the result warns
  `replay_history_untrusted`, and old mutations must not be blindly retried.
- The skill never deletes files in `backups/`. They are ordinary files the
  owner or the agent can copy elsewhere or remove.

### Validation

A document is loaded or restored only if this skill could have written it;
otherwise it is refused as a whole (`store_unreadable` for `store.json`,
`invalid_export` for a restore), so a corrupt copy is never installed and
never breaks the widget later. Checked:

- only known fields at every level (document, group, entry, request record,
  retired-id filter), each with its type;
- ids in their formats (`g_…`, `e_…`, `s_…`, request ids);
- group names exactly as the name cleaner stores them (non-blank, trimmed,
  at most 80 characters, no `/`, not id-like), unique among siblings
  (case-insensitive), with existing parents and no parent cycles;
- entry text exactly as the text cleaner stores it (non-blank, at most 1000
  characters), status `open`/`done`, a group that exists, a `seq` that is
  unique and below `next_seq`, `due` exactly as recorded from its raw text
  (at most 120 characters), timestamps as short strings or null;
- request records with a known shape and a result nested at most 8 levels;
- counters as integers in range, at most 500 groups, 5000 entries and 5000
  request records (the next save keeps the newest 500 and retires the rest),
  and valid Unicode throughout.

## Deletion

- `delete` (tool action `delete`, through the chat tool)
  moves entries to the trash: they disappear from open/done reads, the
  subtree selection and duplicate reports, but keep their text, status and
  group. `read` with `status: deleted` show them.
- `undo` brings deleted entries back with their previous status and group.
  Deleted entries cannot be edited, completed or moved until undone.
- `erase` permanently removes entries that are already in the trash; it
  refuses live entries. Erasing cannot be undone, except by restoring an
  export that still contains the entry. The request journal never keeps entry
  plaintext, so an erased entry's text is removed from the current entry.
  The request journal may retain an unsalted argument digest that can confirm
  guesses about short text; exports and backup copies made earlier still
  contain the original text. Erase is not secure deletion.
- Deleted entries count toward the 5,000-entry limit until erased, and a group
  cannot be deleted while it holds entries, including deleted ones.

## Replay window

- The journal keeps a replay record (ids and fields, no private text) for the
  500 most recent request ids. Duplicate suggestions are shown on the initial
  add result only; a replay sets `duplicates_omitted_on_replay` instead.
  Read current entries for the current duplicate picture.
- An id evicted from the journal is added to a fixed-size filter
  (`retired_requests`: a Bloom filter of 2^20 bits and 7 hashes over the
  id's 64-bit digest, zlib-compressed, at most about 175 KB in `store.json`).
  The filter never forgets: a retry with any retired id is refused with
  `request_expired` and nothing is applied a second time. There is no hard
  request-count cutoff, but the filter saturates eventually: new ids become
  increasingly likely to be refused, and this version has no safe history-reset
  operation. This is a long-horizon capacity limit, not infinite replay.
- The price is that a new id can collide with a retired one and is then
  refused the same way; nothing is applied, and the agent repeats the change
  with a new id. The chance is negligible for a personal list (about 0.015%
  after 50,000 retired ids, 0.65% after 100,000, 4% after 150,000);
  `store` → `status` reports `replay.retired_ids_estimate` and
  `replay.false_refusal_rate`, and the refusal names the rate once it reaches
  1%. The widget supplies a fresh request id for each checkbox action.
- After the full record is evicted, a retry is refused even if the entry
  survives: its old arguments are no longer available to check for conflict.
  Reusing the same id with changed text must never silently report success.
- Restore adds the ids applied or retired by a **verified** replaced store,
  but unknown to the export, to the filter; retired ids also travel inside
  exports. It never imports a damaged store's filter into a sound backup.
- Stores from the 0.1.0 Draft may already have discarded old ids; this
  release cannot reconstruct that lost history. The 0.2.0 candidate's list of
  evicted digests is folded into the filter without loss.

## Agent tools

Tools are namespaced by the host (`ext_<n>_smart_lists_<name>`) and return
JSON: `{"ok": true, ...}` or `{"ok": false, "error": {"code", "message"}}`.
Unknown arguments are refused rather than ignored.

| Tool | Purpose |
| --- | --- |
| `add` | Add one or more verbatim entries to a group (`group`, `items[{text, due?}]`, `request_id`). |
| `read` | Group tree with open/done/deleted counts plus entries of one group (and its sub-groups by default) or all groups; `status` open/done/deleted/all (`all` = open and done), `limit` ≤ 500, `offset` for the next page. |
| `update` | Replace the text and/or due of one entry, or clear its due. |
| `complete` | Mark entries done, or reopen them with `done: false`. |
| `move` | Move entries to another group. |
| `delete` | `action` `delete` (to the trash), `undo`, or `erase` (trashed entries only, permanent). |
| `group` | `create`, `rename`, `move` (never under its own descendant) or `delete` (empty groups only) a group. |
| `select` | Read-only open entries of a group and all its descendants in tree order; `limit` ≤ 500 and `offset` page through them. Returns shown count, total, truncation flag and `next_offset`; e.g. to prepare a shopping run. |
| `store` | `status`, `init`, `export` or `restore` (`file`, `replace`) the whole store. |

## Widget

The module widget requests a wide (two-column) card and a fixed-height frame;
the checklist scrolls inside it while tabs and backup stay visible. The skill
cannot resize its own outer card; owner resizing requires the host Widgets
layout. Text-size buttons affect this mounted frame and reset on restart.

The module widget shows **Today** with a tab for all entries and one tab per
top-level group. A tab includes that group's nested subgroups; empty groups
still have tabs. Within a tab, section labels show where entries belong, but
only checkboxes, text-size controls and a backup download are actionable. Group creation, entry
capture and all other edits happen through ordinary chat. Every open entry appears once,
regardless of when it was added. Tomorrow's list is the same still-open entries,
not duplicated records or a midnight job. The date is computed by the host on each read in
the browser-reported IANA timezone and shown beside Today; an invalid timezone refuses
rather than guessing a date. Checking an item marks it done immediately but keeps it visibly checked in
Today through the end of the browser's local day. On the next day it moves to
the collapsible archive; unchecking a completed item reopens it. The archive
preview shows 50 recent earlier-day completions; older ones
remain accessible through chat. Today's open and checked entries page 100 at a time. A low-emphasis backup button downloads a full JSON export outside the skill data directory. A rejected or
uncertain checkbox request keeps the current list visible and shows an error,
never silently treats a timed-out request as undone. On uncertainty, ask in chat
or refresh before deciding whether to act again. The widget refreshes every 30 seconds while
visible, including across midnight; creation, editing, groups, restore, export and trash management remain
available through the nine agent tools in ordinary chat. This UI has no editing or group-management controls.

## HTTP routes

Mounted under `/api/extensions/smart_lists/`: the widget calls `GET today`
with the browser's IANA timezone, an optional top-level group id and a page offset, then
`POST check` with an entry id, boolean done and unique request id. Both use
the existing store; checkbox mutation delegates to `complete`, with its
idempotent journal. The older `GET view`, `POST edit`, `POST group`, `GET select`,
`POST store`, `GET export` routes remain for compatibility and exports but are
not displayed in the new widget. Tool `store` still exports and restores with
the same validation and warnings; use it to save an external backup.

## Storage and limits

- `store.json` in the skill state directory holds everything, written as
  described in [Writes and failures](#writes-and-failures) under an
  in-process lock plus a cross-process lock on `store.lock` (`fcntl.flock`
  on POSIX, `msvcrt.locking` on Windows), waited for at most 10 seconds
  before refusing with `store_busy`, so tools, routes and other processes
  never lose each other's writes. `store_meta.json` is the sentinel described
  above and `backups/` holds exports and pre-replacement copies.
- Schema version 3. A version 1 store (Smart Lists 0.1.0) or version 2 store
  (the 0.2.0 candidate) is read as is and rewritten as version 3 on its first
  change: version 1 gains a store id, a generation and the sentinel; version
  2's `expired_requests` digests move into `retired_requests`. A schema 1 add
  journal record with only entry IDs is retired into that filter; retrying its
  request ID is refused rather than replaying an incomplete result. Older versions
  refuse a version 3 store as newer (`store_unreadable`) instead of ignoring
  retired ids, and refuse a version 3 export (`invalid_export`).
- Limits: 500 groups, 5000 entries (deleted ones included until erased), 100
  items per call, 1000 characters per entry, 80 per group name, 120 per due
  value, 64 MiB per export read for restore (enough for the worst-case
  escaped text and due fields at the declared entry limit).
- Agent tool responses are complete JSON under 14,500 UTF-8 bytes (below the
  host's 15k cap). Oversized arrays carry `result_counts` with total, shown
  and truncated; read/select expose `next_offset` to continue. Long scalar
  fields are shortened only when needed and named in `truncated_fields`.

## Boundaries

- No network access, no settings or secrets, no subprocesses.
- Nothing is purchased, ordered, scheduled, reminded or sent anywhere; the
  subtree selection is read-only.
- Writes only inside the skill state directory. `restore` may read an export
  file the agent names anywhere on disk; it only parses it.

## Known gaps

- **Everything lives inside the Ouroboros data directory.** Uninstalling the
  skill (the startup sweep clears its state directory except host grants) or
  reinstalling Ouroboros deletes `store.json`, the sentinel and `backups/`
  together. Afterwards the store is `uninitialized`, which is indistinguishable
  from a first run; the explicit `init` step is what keeps it from silently
  appearing empty. Only an export copied outside that directory (through the
  widget's Download button, or the agent's `store` export and a separate copy)
  survives. The skill cannot
  write there itself and does not schedule exports.
- Restoring after a `missing` store cannot carry over the request ids of the
  lost file. A retry of a change that only the lost file had applied is
  therefore applied again to the restored store, whose copy lacks that change.
- A crash during a first `init` or a restore into an `uninitialized` or
  `missing` store (there is no previous file to name in `pending`) leaves
  the sentinel ahead of a missing file: the store then shows as `missing`
  until `restore` or `init` with `replace: true`. No empty list is
  fabricated.
- If the last sentinel write of a save fails, a copy of exactly the
  previous generation put back by hand before the next save would load
  without a `mismatch`.
- An unrecorded export is remembered in memory only; after a restart the
  status again shows the last recorded export.
- Single group membership per entry; no tags.
- The widget displays entries and toggles their status only. Other changes,
  including restore and export, are available through chat.
- The Today widget pages 100 open entries at a time and previews 50 archived entries. Agent tools can page through all 5000 entries with `offset`, subject to the per-response byte cap.
- On platforms with neither `fcntl` nor `msvcrt` only the in-process lock
  serializes writers. The Windows lock path is covered by tests with a
  simulated `msvcrt`, not on a Windows machine.
- A new request id can, rarely, be refused as already used (see
  [Replay window](#replay-window)); it is never applied twice.

## Tests

```bash
python -m unittest discover -s skills/smart_lists/tests -v
```

Set `OUROBOROS_CORE_ROOT=/path/to/ouroboros` to additionally validate both
widget declarations with the core's widget validator.
