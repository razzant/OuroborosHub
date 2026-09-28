---
name: smart_lists
description: "Personal lists in one skill-local store: a free group tree, verbatim entries captured from clear owner intent in chat, completion and moves, a read-only subtree selection, and a compact declarative widget. No network, purchases or reminders."
version: 0.1.0
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [tool, route, widget]
env_from_settings: []
when_to_use: The owner clearly intends to capture a list note (even without saying 'add'), check or change list entries, mark items done, reorganise list groups, or gather the open entries of a group (for example before a shopping run). Do not infer capture from passing conversation or trigger purchase, ordering, reminder or other action skills.
model_experience:
  what_model_sees: Seven small Smart Lists tools (add, read, update, complete, move, group, select) returning compact JSON.
  token_effect: Small fixed schema cost per round while enabled; read and subtree selection results are bounded to 500 entries.
timeout_sec: 30
ui_tab:
  tab_id: lists
  title: Smart Lists
  icon: 📋
  render:
    kind: declarative
    schema_version: 1
    span: 2
    components:
    - {type: poll, route: view, method: GET, target: lists, auto_start: true, interval_ms: 30000, max_ticks: 100, label: Refresh lists, busy_label: Refreshing…}
    - {type: callout, target: lists, tone: success, path: notice, condition_key: notice}
    - {type: callout, target: lists, tone: warning, path: warning, condition_key: warning}
    - {type: callout, target: lists, tone: danger, path: error, condition_key: error}
    - {type: callout, target: lists, tone: info, path: empty_hint, condition_key: empty_hint}
    - type: group
      layout: cluster
      target: lists
      components:
      - {type: metric, target: lists, label: Groups, path: stats.groups}
      - {type: metric, target: lists, label: Open, path: stats.open}
      - {type: metric, target: lists, label: Done, path: stats.done}
    - type: tabs
      target: lists
      tabs:
      - label: Open
        components:
        - type: table
          target: lists
          path: open_rows
          columns:
          - {label: Item, path: item}
          - {label: Group, path: group}
          - {label: Due, path: due}
          - {label: ID, path: id}
        - {type: callout, target: lists, tone: info, path: open_note, condition_key: open_note}
      - label: Groups
        components:
        - type: table
          target: lists
          path: tree_rows
          columns:
          - {label: Group, path: group}
          - {label: Open, path: open, presentation: number}
          - {label: Done, path: done, presentation: number}
          - {label: ID, path: id}
      - label: Done
        components:
        - type: table
          target: lists
          path: done_rows
          columns:
          - {label: Item, path: item}
          - {label: Group, path: group}
          - {label: Completed (UTC), path: completed}
          - {label: ID, path: id}
    - type: tabs
      target: lists
      tabs:
      - label: Edit entry
        components:
        - type: form
          route: edit
          method: POST
          target: lists
          title: Edit one entry
          submit_label: Apply
          busy_label: Saving…
          columns: 2
          fields:
          - {name: entry_id, label: Entry ID, type: text, required: true, placeholder: e_…}
          - name: status
            label: Status
            type: select
            options:
            - {value: '', label: Keep}
            - {value: done, label: Mark done}
            - {value: open, label: Reopen}
          - {name: text, label: New text, type: text, span: 2, placeholder: Leave blank to keep}
          - {name: due, label: New due, type: text, placeholder: Leave blank to keep}
          - {name: move_to, label: Move to group, type: text, placeholder: Leave blank to keep}
          - {name: clear_due, label: Clear due, type: checkbox}
      - label: Edit groups
        components:
        - type: form
          route: group
          method: POST
          target: lists
          title: Create, rename, move or delete a group
          submit_label: Apply
          busy_label: Saving…
          columns: 2
          fields:
          - name: action
            label: Action
            type: select
            options:
            - {value: create, label: Create}
            - {value: rename, label: Rename}
            - {value: move, label: Move}
            - {value: delete, label: Delete (empty only)}
          - {name: group, label: Group, type: text, placeholder: 'Existing group (rename, move, delete)'}
          - {name: name, label: Name, type: text, placeholder: 'New name (create, rename)'}
          - {name: parent, label: Parent, type: text, placeholder: 'Blank = top level (create, move)'}
      - label: Subtree
        components:
        - type: form
          route: select
          method: GET
          target: selection
          title: Open entries in a group and its sub-groups (read-only)
          submit_label: Show
          busy_label: Reading…
          fields:
          - {name: group, label: Group, type: text, required: true, placeholder: Home}
        - {type: callout, target: selection, tone: warning, path: warning, condition_key: warning}
        - {type: callout, target: selection, tone: danger, path: error, condition_key: error}
        - type: kv
          target: selection
          condition_key: ok
          fields:
          - {label: Scope, path: scope}
          - {label: Shown open entries, path: count}
          - {label: Total open entries, path: total}
        - type: table
          target: selection
          condition_key: ok
          path: rows
          columns:
          - {label: Item, path: item}
          - {label: Group, path: group}
          - {label: Due, path: due}
          - {label: ID, path: id}
---

# Smart Lists

Smart Lists keeps personal lists — groceries, errands, packing, anything —
in one authoritative store inside this skill's state directory. The agent adds
entries when the owner clearly asks in chat, and a compact declarative widget
shows and edits the same data.

Version 0.1.0 starts with an empty store rather than importing another app's
lists. Each entry belongs to exactly one group; groups may be nested and moved.
An import/sync layer and multi-list labels are deliberately outside this version.

## Model

- **Groups** form one editable tree. Every group has at most one parent;
  top-level groups are the lists, children are sub-lists
  (`Home / Groceries / Dairy`). Names are unique among siblings
  (case-insensitive) and cannot contain `/`, which separates path segments.
  A group is addressed by id (`g_…`) or by its path.
- **Entries** hold the item text, an `open`/`done` status, an optional due
  value, their group, and timestamps. Entry ids look like `e_…`.

## Owner policies enforced by the store

| Policy | Behavior |
| --- | --- |
| Capture on clear intent only | The agent calls `add` when the owner clearly intends note capture, even without the word “add”; there is no passive listener or chat subscription. Passing conversation does not trigger capture or action skills. |
| Raw text | Entry text is stored exactly as given — no trimming, recasing, splitting or quantity parsing. |
| No silent dedup | Adding text that is already open in the same group creates a second entry; the result lists it under `possible_duplicates` so the agent can mention it. |
| Dates | A due value is read as an instant only when it is an ISO-8601 date-time with an explicit offset (`2026-10-01T18:00+03:00`, `…Z`); it is then stored as `at` next to the raw text. Anything else (`tomorrow`, `Friday`, `2026-10-01`) is kept only as raw text and never interpreted. No reminders are created. |
| Idempotent requests | Mutations accept a `request_id`. A retry with the same id and arguments returns a replay result (`replayed: true`) without applying anything; entry fields reflect their current values after later edits. The same id with different arguments is refused. The agent `add` tool requires one. The last 500 ids are remembered without duplicating entry text or due values in the request journal. |

## Agent tools

Tools are namespaced by the host (`ext_<n>_smart_lists_<name>`) and return
JSON: `{"ok": true, ...}` or `{"ok": false, "error": {"code", "message"}}`.
Unknown arguments are refused rather than ignored.

| Tool | Purpose |
| --- | --- |
| `add` | Add one or more verbatim entries to a group (`group`, `items[{text, due?}]`, `request_id`). |
| `read` | Group tree with open/done counts plus entries of one group (and its sub-groups by default) or all groups; `status` open/done/all, `limit` ≤ 500. |
| `update` | Replace the text and/or due of one entry, or clear its due. |
| `complete` | Mark entries done, or reopen them with `done: false`. |
| `move` | Move entries to another group. |
| `group` | `create`, `rename`, `move` (never under its own descendant) or `delete` (empty groups only) a group. |
| `select` | Read-only: up to 500 open entries of a group and all its descendants in tree order, with shown count, total and truncation flag; e.g. to prepare a shopping run. |

## Widget

A host-rendered declarative card (no custom JavaScript): a refresh poll,
counters, tabs for **Open** entries, the **Groups** tree and recently **Done**
entries, and small forms to edit one entry (text, due, status, group), edit
groups, and view a read-only **Subtree** selection. Capture new entries through
the agent's idempotent `add` tool. Changes made
in chat appear on the next automatic refresh (every 30 seconds while the card
is visible, up to 100 times; **Refresh lists** resumes it).

## HTTP routes

Mounted under `/api/extensions/smart_lists/`: `GET view`, `POST edit`,
`POST group`, `GET select`. They use the same store as the tools.
Routes always answer HTTP 200 with a JSON object; a refused change comes back
as `ok: false` with a `warning` next to the unchanged view, so the card keeps
showing the lists.

## Storage and limits

- One file, `store.json`, in the skill state directory, written atomically
  (temporary file + replace) under an in-process lock and, on POSIX, an
  advisory file lock (`store.lock`), so tools and routes never lose each
  other's writes.
- If the file is unreadable or has a newer schema, every operation refuses
  with `store_unreadable` and leaves the file untouched.
- Limits: 500 groups, 5000 entries, 100 items per call, 1000 characters per
  entry, 80 per group name, 120 per due value.

## Boundaries

- No network access, no settings or secrets, no subprocesses.
- Nothing is purchased, ordered, scheduled, reminded or sent anywhere; the
  subtree selection is read-only.
- Writes only inside the skill state directory.

## Known gaps

- No import from another list source, and no complete backup/export/restore
  interface yet. Before using this as the sole personal list store, preserve
  `state/skills/smart_lists/store.json` through reinstall and verify a restore
  on an isolated copy; a missing file otherwise opens an empty store.
- Single group membership per entry; no tags.
- Entries cannot be deleted yet (complete or move them instead). A group can
  be deleted after all its entries and sub-groups have been moved away; the
  store's 5,000-entry lifetime cap remains a v1 limit.
- The widget addresses groups and entries by typed path or id; there are no
  pickers.
- Subtree selection stops at 500 displayed entries and has no paging yet;
  `total` and `truncated` show when more entries exist.
- On platforms without `fcntl` only the in-process lock serializes writers.

## Tests

```bash
python -m unittest discover -s skills/smart_lists/tests -v
```

Set `OUROBOROS_CORE_ROOT=/path/to/ouroboros` to additionally validate both
widget declarations with the core's declarative-widget validator.
