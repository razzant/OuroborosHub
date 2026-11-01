---
name: memory-atlas
title: Memory Atlas
description: Read Ouroboros's memory room by room, beside the record you open, with corrections, drafts and decisions, knowledge notes, marks and identity, and copy an exact address to paste into a chat. Long lists and texts continue where they stopped; Refresh shows what changed. Read-only, no model calls.
version: 0.3.0
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.1"
permissions: [route, widget]
when_to_use: The owner wants to read what Ouroboros remembers, see who wrote a record and what was corrected later, or get the exact address of a record or note.
model_experience:
  what_model_sees: Nothing while enabled. The skill registers no tools; it adds one Widgets card and read-only routes.
  token_effect: None. The widget runs in the owner's browser and makes no model calls.
ui_tab:
  tab_id: atlas
  title: Memory Atlas
  icon: "◈"
  render:
    kind: module
    entry_type: module
    entry: app/main.js
    assets: [app/atlas.js, app/client.js, app/words.js, app/markdown.js, app/parts.js, app/atlas.css]
    start: manual
    appearance: host
    height: 600
    span: 2
---

# Memory Atlas

A reading room for Ouroboros's memory. Rooms come first and stay beside the record
being read: each room of the chronicle, then My story across rooms, then what is
kept across rooms (knowledge, marks, identity, other sources). On a phone the same
list is the first screen and a record opens after a choice.

## What it shows

- **Rooms**: candidate pages and parts awaiting a decision first, then the story in
  the room, decisions on drafts and the marks written there.
- **A record**: its own words with every correction signed under them, the words it
  quotes from the chat (each by the speaker the host checked, opening the address of its
  chat row), its status as recorded (draft, candidate, accepted, rejected), the decision
  and its reason, the part it belongs to and the accounts that tell or cite it. "Stored
  record" shows the record as stored; a long stored value shows its first words and reads
  whole.
- **An account**: what it selected into My story, and each source as it cited it
  (revision and sha256), with what changed in that source afterwards.
- **Knowledge**: every shelf's notes with their authored summaries ("No summary" when
  there is none), active and archived, links out and the links in found on the same
  shelf, versions from the shelf's journal and a compare of two full texts.
- **Marks**, **Identity**, the scratchpad, World and each Project's workpad and journal.
- **Search memory text**: literal, any case, a bounded step at a time, saying what it read.
- **Address**: `memory_read(…)` or `knowledge_read(…)` to paste into a chat.

Lists show a page and continue with Show more; a long text shows its beginning and
continues with Read on, always from the same version. Refresh asks the chronicle
what was written since the widget opened and re-reads what is open only when it
changed. It claims nothing about what is in a model's context: it shows stored facts only.

## Data

The widget reads only its own routes under `/api/extensions/memory-atlas/atlas/v2/`
(the `memory-atlas.read/2` contract). They answer from the host's supported memory
read, `api.read_memory` (PluginAPI 2.1, contract `ouroboros.memory/1`); the skill
parses no memory file and keeps no index. An Ouroboros without PluginAPI 2.1 refuses
the skill. Search and the links into a note are the skill's own bounded reads over
that interface and say what they covered.

Shared controls come from the installed `web/ui.css` through the `author-kit` route;
if that fails the widget stays readable in plain native colours.
