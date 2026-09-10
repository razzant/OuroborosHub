# Visual contract — Opus + Cursor handoff

Provenance: Opus task 161b4dd9 / run-e8e3aaba6eaf, Cursor child c9d6e8e8 / run-e6320f8aa442, both succeeded; subscription $0 final each. This is a mechanically materialized corrected planning handoff, not root-authored design.

## Strata Atlas → Palimpsest Reader
- Global: Identity, Working memory, Knowledge. Project: Knowledge, Workpad, Journal. Clearly selected scope; Global + one selected project, not every project stacked.
- Compact Search, Project scope, and Refresh toolbar.
- Horizontal category lanes on shared chronology. Capsules span earliest retained observation to latest revision, not claimed document lifespan. Unknown dates in Undated; missing history says History unavailable.
- Map receives metadata only. Selecting capsule opens Markdown reader without a modal and compresses atlas into a provenance strip.
- At wide widget interiors use ~200px navigation/relationship rail plus reader around 68ch. Narrow rail becomes disclosure.
- Revision strip shows timestamps/provenance/completeness. Compare two reconstructible revisions; missing/digest-only cannot become fake full versions.
- Local relationships only; every edge has recorded evidence and one of the four
  backend kinds (`markdown_link`, `wiki_link`, `journal_source_ref`, or
  `shared_task_id`); provide an equivalent accessible list.
- Borrow quotas glass materials, not dashboard layout: restrained hairlines, drawn stroke icons, subtle accent, honest unavailable states. Current host type tokens 12/14/16/24px, sentence case, visible focus, selectable text. Category colours not status alarms. Reduced motion supported.
- Actual iframe interior is smaller than desktop 1100x722. Bound reader scroller height; no vh resize feedback loops. Keyboard and pointer flows; no shortcut hijack in editable fields.
- Map search also has an accessible result/list path; don't hide loss behind dimming. Dense journals may use labelled counts/drilldown, never silently dropped entries.

## Corrections from planning host
Use vetted locally bundled Markdown/sanitization code where feasible; sandbox supports sibling scripts and classic entry. No custom parser mandated. Tables/tasklists required in tests. Neutral unresolved links, not red failures. Source tokens from canonical docs/DESIGN.md and web/style.css; planner had read an older worktree for some excerpts. View preferences route only if backend contract actually includes it; do not assume storage.

## Owner settlements
- Owner answer, 2026-09-06: atlas + comfortable reading; text on click.
- Superseding owner answer, 2026-09-06, direct instruction: include consolidated
  dialogue memory (`memory/dialogue_blocks.json`, `memory/dialogue_meta.json`, and
  legacy `memory/dialogue_summary.md`) as first-class memory because these are
  consolidated summary blocks, not raw chat. This supersedes the earlier
  core-plus-projects quiz scope that excluded dialogue. Raw `logs/chat.jsonl` and
  its archives, `owner_mailbox`, control state, settings, secrets, and
  tool/execution logs remain excluded. This is the single current authorization
  scope.
- Superseding owner answer, 2026-09-06, direct instruction: relationships must be
  deterministic and proven, with no additional model calls. This supersedes the
  earlier working settlement that allowed an optional relationship-guessing layer.
- Working assumption: use the Working memory current shelf plus history, Global
  plus one explicitly chosen project, and neutral labels.

## Ownership
UI builder only widget.js, tests/test_widget_ui.py and synthetic UI fixtures, any declared vendored UI dependencies. Backend builder owns manifest/Python/data tests and exact API contract. Root integrates/lifecycle/real Widgets visual QA.
