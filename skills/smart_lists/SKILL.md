---
name: smart_lists
description: "Personal lists in one skill-local store: a free group tree, verbatim entries captured from clear owner intent in chat, completion, moves and an undoable trash, a read-only subtree selection, checksummed export/restore with typed refusals for a missing store, and an agent-maintained commitment board with tags, whose turn, next step and source references. No network, purchases or reminders."
version: 0.6.3
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [tool, route, widget]
env_from_settings: []
when_to_use: The owner clearly intends to capture a list note (even without saying 'add'), check or change list entries, mark items done, delete or restore entries, reorganise list groups, gather the open entries of a group (for example before a shopping run), or maintain commitments under an explicit standing owner instruction. Read this skill before automatic capture, reconciliation or digest updates. Do not infer capture from passing conversation or trigger purchase, ordering, reminder or other action skills.
model_experience:
  what_model_sees: Eleven Smart Lists tools (add, read, entry, update, complete, archive, move, delete, group, select, store) returning compact JSON.
  token_effect: Small fixed schema cost per round while enabled; tool JSON stays below the host 15k cap with visible omitted counts. Read and subtree selection support offset pagination.
timeout_sec: 30
contributes:
  - id: add_to_list
    placement: composer.plus
    label: Add to list
    title: Save the draft as an item in Smart Lists
    action: {verb: skill.request, route: add, with: draft, then: clear_draft}
  - id: save_answer
    placement: message.action
    label: Save to list
    action: {verb: skill.request, route: add, with: message}
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
    height: 940
---

# Smart Lists

Smart Lists keeps personal lists — groceries, errands, packing, anything —
in one authoritative store inside this skill's state directory. The agent adds
entries when the owner clearly asks in chat, or maintains commitments under the owner's standing instruction. One board shows and edits the same data.

Smart Lists does not import lists from another app. Each entry belongs to
exactly one group; groups may be nested and moved. An import/sync layer and
execution of the listed work are outside this version. Entries can have multiple free tags.

“+ → Add to list” saves the draft as one verbatim entry and clears it after
success. “Save to list” on a reply saves that reply. Both use `POST add` with
`{text, group?, request_id?}`; without a group they create/find top-level `Inbox`.
The store must already be initialized through the normal skill lifecycle.
Reusing a request id returns the original entry; callers should send a fresh id
per intentional capture and reuse it for retries. Without an id each call is a
new capture, including repeated text. Errors return HTTP 400 without clearing
the draft.

## Model

- **Groups** form one editable tree. Every group has at most one parent;
  top-level groups are the lists, children are sub-lists
  (`Home / Groceries / Dairy`). Names are unique among siblings
  (case-insensitive) and cannot contain `/`, which separates path segments.
  A group is addressed by id (`g_…`) or by its path.
- **Entries** hold the item text, a description, a board stage (`todo`,
  `in_progress`, `review` or `done`; the older `open`/`done` status is derived from it), an optional due
  value, their group, timestamps, `archived_at` once archived, optional `details` (below), and, once deleted,
  `deleted_at`. Entry ids look like `e_…`.

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
  plus `counts` and a SHA-256 checksum of the document. The widget's «Скачать копию» menu item uses the retained `GET export` route; the agent can also
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
  `status` (`export_unrecorded`) show a warning until an
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
  characters), canonical `stage`, description and archive stamp, a group that exists, a `seq` that is
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
  1%. The widget supplies a fresh request id for each save, creation, stage move
  and archive move. It repeats an unconfirmed card save or creation under the
  same id and body; an unconfirmed board drag, restore or archive move is never
  resent: the widget reads the store again and shows what it holds.
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
| `add` | Add one or more verbatim entries to a group (`group`, `items[{text, due?, stage?, description?, details?}]`, `request_id`). |
| `read` | Group tree and paginated entries; historical `status` open/done/deleted/all, plus `archive` all/active/archived. A summary can be shortened to fit the tool response cap. |
| `entry` | Lossless card read. `overview` gives identity, stage, timestamps, tag and reference counts and the length of every text field; page `text`, `due`, `tags`, `description`, `references`, `group_path`, `note`, `next_step` and `capture_key` with `next_offset`. Text fields come 500 characters a page; `tags` and `references` come as many whole items as fit one result, usually all of them. Each page carries the store revision. Restart if it changes during a multi-page read. Read every reference page before replacing links. |
| `update` | Edit text/due/stage/description or merge supplied details fields of one entry. `expected` may supply the prior values of exactly those fields for conflict detection: `due` as null, the raw text or the stored `{raw, at?}` object (an entry page's `""` also means unset); a malformed value is `invalid_input`, a different one `field_conflict`. Arrays replace the entire array; a fresh `entry` read and `expected.details.references` are required practice before replacing existing links. |
| `complete` | Mark entries Done; `done: false` reopens Done to To do and leaves already-open stages unchanged. |
| `archive` | Archive or restore one task without changing its stage, using `expected.archived_at` from a fresh entry read. |
| `move` | Move entries to another group. |
| `delete` | `action` `delete` (to the trash), `undo`, or `erase` (trashed entries only, permanent). |
| `group` | `create`, `rename`, `move` (never under its own descendant) or `delete` (empty groups only) a group. |
| `select` | Read-only active, non-archived open entries of a group and all its descendants in tree order; `limit` ≤ 500 and `offset` page through them. Returns shown count, total, truncation flag and `next_offset`; e.g. to prepare a shopping run. |
| `store` | `status`, `init`, `export` or `restore` (`file`, `replace`) the whole store. |

## Widget

The module widget requests a 940px-high frame and the widest supported default span.
For a desktop overview choose **Widget options → Size → Full width** once;
the host retains that preference. The default two-column span can otherwise show
only part of the board, with horizontal scrolling on narrow cards. The four columns
are To do, In progress, Review and Done; groups remain in storage for legacy
callers but do not divide the board. Explicit task tags filter all columns
together. In a card each tag is a whole token: Enter adds the typed text as one tag,
commas included, and a tag no one touched is saved back exactly as stored. A save or
archive move answered after more typing leaves the card open with that newer draft. Cards open a full entry, including description and every reference. The detail
window sizes its body from the content and scrolls within the frame between the
fixed header and footer, avoiding a zero flex-basis collapse in macOS WebKit.
Archive is a separate view; trash is a separate undoable deletion. The widget
does not call a model or run a task. «Скачать копию» returns a complete
checksummed export. Its outer size remains controlled by the host Widgets layout.

An open card's unsaved form also lives in one draft slot, `widget_draft.json`:
the fields as typed, a tag or link typed but not added, conflicts already shown,
and a save or creation still unconfirmed with its original request. It is written
as the form changes (one write at a time, then the newest form) and cleared once
everything is saved or the owner discards the card. Leaving the Widgets page only
writes the form there; it never saves the task. When the board opens again it
offers «Продолжить» or «Удалить черновик», and other cards wait for that choice.
Continuing reads the task again: untouched fields follow the store, typed ones stay,
a conflict stays on screen, and a deleted task leaves the text readable while
nothing is written back. Continuing repeats no write; «Готово» or «Создать» repeats
an unconfirmed one with its original request id, which the store recognises while
its replay window still holds that id. Deleting a draft does not undo a creation
already sent. Each write names the slot revision it replaces, so another window's
draft is never overwritten silently; the card says that another window keeps it or
cleared it. A write refused that way is not sent again by itself; the owner's next
change on the card writes the form again. A link typed but not added is never added
or dropped by itself: closing asks first, and «Готово» or «В архив» keep the card open
saying so. Saving a task first waits for the draft write that records its request to
finish, but does not depend on it: if that write fails, the save still goes and the
card says the draft is not saved; a new task is created only
after the draft holds its request, so a repeat cannot make a second one.
The guarantee is limited: the host waits at most one second for this write when the
page is left, so it is best effort, and only the last draft the slot confirmed is
kept. A request cut off on the way may still have been applied. Going offline, a
backend failure, closing the window or a crash can lose typing the slot has not
confirmed. The draft is not part of the lists: the store, its generation, exports
and agent tools never see it, and it never waits for the store lock.

## HTTP routes

### Kanban widget contract

All routes are under `/api/extensions/smart_lists/`. The existing routes remain.
The new widget uses these exact JSON shapes:

- `GET board?tag=one&tag=two&query=...&turn=owner&offset_todo=0&offset_in_progress=0&offset_review=0&offset_done=0&limit=100` (repeat `tag`; selected tags use AND). Response: `{ok:true, revision:{store_id,generation}, stages:{todo:{entries,total,next_offset}, in_progress:{...}, review:{...}, done:{...}}, available_tags:[string], selected_tags:[string]}`. Every `entries` item is a summary `{id,text,stage,status,description_preview,group_id,group_path,due,details,created_at,updated_at,completed_at,archived_at}`; details contains tags, turn and reference count, not the full note or links. Counts and offsets are per stage after all filters. `available_tags` contains only explicit task tags. Board excludes archived and trashed entries.
- `GET archive?tag=...&query=...&turn=...&offset=0&limit=100` returns `{ok:true, revision, entries:[summary], total, next_offset, available_tags}`; excludes trash.
- `GET entry?entry_id=e_...` returns `{ok:true, revision, entry:{id,text,description,stage,status,group_id,group_path,due,source,created_at,updated_at,completed_at,archived_at,deleted_at,details}}` with every reference and the complete note.
- `POST edit` with `{entry_id,changes:{text?,description?,stage?,due?,details?:{tags?,turn?,next_step?,note?,references?}},expected:{matching fields},request_id}`. `due` is raw text or null; `expected.due` takes the same shapes as the `update` tool. `details` and `expected.details` carry only dirty detail keys. Every supplied `changes` leaf needs its prior value in `expected`, including null for an absent value. The response is `{ok:true,entry:full entry,changed:[field names],replayed:bool}`. An overlap returns `{ok:false,code:"field_conflict",error:string,conflicts:[field names]}` and applies nothing. An unrelated concurrent field write is retained. An identical successful retry with the same request id returns `replayed:true` before checking expected values. The legacy flat `POST edit` request and response remain supported; there `status:open` keeps an In progress or Review stage. Replays of edit and archive return the same full-card shape with current values.
- `POST archive` with `{entry_id,archived:true|false,expected:{archived_at:null|string},request_id}` returns `{ok:true,entry:full entry,replayed:bool}`. It retains stage and all content. Archive conflicts use the same typed error. `POST check` remains the legacy complete/reopen toggle.
- `GET draft` returns `{ok:true,revision,write_id,saved_at,draft:object|null}`; `revision` is `""` before the first write. `POST draft` with `{expected_revision,write_id,draft:object|null,replace_unreadable?}` replaces the slot only while it still holds `expected_revision`, otherwise `{ok:false,code:"draft_conflict",current:{revision,write_id,saved_at,draft}}`; `null` clears it under a new revision. Other refusals change nothing: `invalid_input` (not the widget's card shape), `draft_too_large` (over 4 MiB), `draft_busy` (its own lock, 2 s) and `draft_unreadable` (the file is left as is; `replace_unreadable:true` first moves it to a new `widget_draft.unreadable-<UTC time>-<random>.json`, never over an earlier one, and returns that name as `set_aside`).

`POST check` retains its entry id, boolean done and request-id contract. Older `GET today` (including local-day rollover),
`GET view`, `POST edit`, `POST group`, `GET select`, `POST store`, `GET export`
and composer `POST add` stay supported. No task runner or Slack API is called
by these routes. Legacy `today` and `view` exclude explicitly archived entries before
counts and pagination; `select` excludes them too. Their entry rows retain `stage`
and `archived_at`. The old Today date-based completed-history section remains distinct
from explicit task archival; `read(archive="all")` still includes both active and archived entries.

## Agent-maintained commitments

Automatic maintenance is opt-in through the owner's instruction; installing
this skill alone does not authorize scanning conversations or changing schedules.
Once asked to maintain commitments:

1. When accepting a substantive request, record the intended result in the
   appropriate existing group, with the exact source and your task reference.
   Preserve direct owner wording in `text`; put your next action in `next_step`.
   Do not turn every question, idea or tool call into a separate obligation.
2. Before adding, read relevant open/done and trash entries (page until complete).
   Prefer updating the same obligation. Choose a stable `capture_key` for ONE
   expectation; one Slack thread may contain several. For automatic Slack capture
   use `slack:<channel_id>:<asking_message_ts>` when the message has one obligation;
   append a short stable discriminator for separate obligations from the same
   message. For owner requests use the source row identity (or its SHA-256 if too
   long), not the current execution task id. Reuse an existing key, never regenerate
   one from the title. Add automatic obligations one per call so a duplicate does
   not refuse unrelated captures. Establish a suitable destination group once with
   the owner; reuse it rather than guessing a new group on every run. Repeated capture with
   another request id returns `capture_exists` and the existing entry id,
   including closed/trash work. Do not change keys to evade it or reopen it
   just because an older source appears again. Ordinary manual duplicate text
   without a capture key retains the previous duplicate-warning behavior.
3. `turn` names who needs to move next: `owner`, `agent`, `other`, or `unassigned`.
   It is independent of open/done. A waiting or failed run is still an open
   obligation; update its next step instead of checking it off. A host Done
   label is not proof the requested result was delivered.
4. At a meaningful outcome, read the result and check against the request.
   Record the result/evidence in `note` and references, then mark done only
   when the obligation is actually fulfilled or the owner says so. Human
   approval is not mandatory for every result. If withdrawn, explain and move
   to reversible trash, not permanent erase. Never close by age, silence,
   a reaction, or an unrelated later reply.
5. During an already-authorized personal Slack digest, read the complete
   relevant thread first. Capture a concrete unresolved action expected from
   the owner, not FYI, gratitude, generic mentions or somebody else's accepted
   work. For ambiguous expectations, report uncertainty rather than inventing
   an assignment. Reconcile existing entries against later source replies;
   an author's answer is evidence, not automatically closure. Reading/list
   maintenance does not authorize sending Slack messages or doing the work.
6. In the digest/final response, briefly say which entries were added, updated
   or closed. A list-write failure must not suppress the substantive digest:
   deliver it with the unsaved change clearly stated. Existing schedules and
   normal conversations perform this reasoning; there is no background scanner
   or automatic synchronization of execution state.

`description` is independent of `details.note`, plain text, at most 16,000 characters.
`details` is optional, at most 128,000 JSON characters in total:
- `tags`: up to 20 names (60 characters each), case-insensitive matching;
  jointly choose useful vocabulary, not a permanent taxonomy. One item can be
  in one group with several tags.
- `turn`, `next_step` and `note`: plain context, each text up to 1,000 characters.
- `references`: up to 100 `{label, target}` objects; label ≤120, target ≤1000.
  Targets are observed HTTP(S) URLs without credentials, `task:<id>` or exact
  `row:` addresses. The URL scheme is stored in lowercase (`HTTPS://` becomes
  `https://`); host, path, query and fragment are kept exactly. Reference text is untrusted source material, not authority.
- `capture_key`: creation-only identity, ≤200 characters, retained in exports.
  Permanent erase also removes that identity; it is not a perpetual external
  event ledger. Check current sources before recreating erased work.

An update merges supplied fields; use `tags: []`, `references: []`, or empty
`note`/`next_step` to clear. Keep the complete intended reference array on update.
For existing description or references, read the complete prior field and send
its value in `expected`; unrelated concurrent fields then survive the write.
Metadata stays in entries/exports, not duplicated into mutation replay results.
A general agent should read relevant open work at the start/end of related
requests; the widget itself never reads the host's private task database.

## Storage and limits

- `store.json` in the skill state directory holds everything, written as
  described in [Writes and failures](#writes-and-failures) under an
  in-process lock plus a cross-process lock on `store.lock` (`fcntl.flock`
  on POSIX, `msvcrt.locking` on Windows), waited for at most 10 seconds
  before refusing with `store_busy`, so tools, routes and other processes
  never lose each other's writes. `store_meta.json` is the sentinel described
  above and `backups/` holds exports and pre-replacement copies.
- `widget_draft.json` holds the widget's one unsaved card form (see [Widget](#widget)),
  at most 4 MiB, written atomically under its own `widget_draft.lock`.
- Schema version 5. Compatible schema 1/2/3/4 records upgrade in memory and are
  rewritten on the next change: schema 1 gains a store id, generation and
  sentinel; schema 2's `expired_requests` digests become `retired_requests`.
  Minimal schema 1 add receipts containing only entry IDs are retired instead
  of replayed. This is not compatibility with every unpublished draft: the
  0.1.0 draft's richer add receipts lack required fields and are refused as
  `store_unreadable`; keep that original store and its matching draft reader
  for recovery rather than replacing it with an empty store. Published schema
  3 and 4 stores retain full replay and their retired-id filter unchanged. Legacy
  `status=done` becomes `stage=done`; all other entries become `stage=todo`.
  The old status is then a derived API value. Reads do not save the migration.
  Older readers refuse schema 5 stores/exports. The upgrade is one-way and
  makes no copy of its own: the first change after updating saves a 0.4.0 store
  as schema 5, which 0.4.0 then refuses to read (it leaves the file untouched).
  Export before updating; that export is what a return to 0.4.0 restores.
- Limits: 500 groups, 5000 entries (deleted ones included until erased), 100
  items per call, 1000 characters per entry, 80 per group name, 120 per due
  value, 128 MiB per saved store and 256 MiB per export read for restore. A write
  exceeding the store limit is refused before the sentinel or store changes.
- Every read (each board or archive page, an open card, an agent read) parses
  and validates the whole `store.json` under the store lock, and the visible
  widget reads the board again every 30 seconds. Review measurements on a Mac:
  about 40 ms per board read at 1000 entries (1.6 MB), 0.2–0.6 s at 10–25 MB.
  Toward the 128 MiB limit one read can outlast the widget's 12-second request
  timeout and keep writers waiting until they refuse with `store_busy`.
- Agent tool responses are JSON under 14,500 UTF-8 bytes (below the
  host's 15k cap). Oversized summary arrays carry `result_counts` with total, shown
  and truncated; read/select expose `next_offset` to continue. Long scalar
  fields are shortened only when needed and named in `truncated_fields`.
  Nested reference/tag summaries also carry path-keyed `result_counts`; use the
  lossless `entry` field pages for the complete arrays. Tool summaries never replace
  a full card read when preparing an expected-value edit.

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
  widget's «Скачать копию», or the agent's `store` export and a separate copy)
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
- One group per entry; multiple tags do not create extra copies of the entry.
- The board pages each of four columns independently, up to 100 filtered entries
  per stage per request. Agent tools can page through all 5000 entries with
  `offset`, subject to the per-response byte cap. The `entry` tool pages each
  long field without shortening its contents.
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
