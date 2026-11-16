# Addressed core-fix cloud phase

This is source/data preparation for one Linux Actions attempt. Runtime: **NOT_RUN**.
The existing full and frames-only phases, prior results and original ZIP are retained.
Do not rerun the 176/84-frame matrices or reinterpret A1–R5 through this phase.

## Parent binding

`qa/core-fix-subjects.template.json` is deliberately `UNBOUND`. Copy it to
`qa/core-fix-subjects.json` only after the final review and publication. Set actual
core/target commit and tree identities, the Hub product tree/manifest/path count,
and `state: "ready"`. Missing or unfinished pins fail before core checkout.

The template binds the observed date-precision postimages, including ISO test
SHA-256 `985efcdcdbc8cd30869ed68dca10cd48eaf04f7edb31dd84679671084ce3a75d`.
These bytes match the parent's `late-review-2102/core` artifacts, but this is not
an approval. If the parent changes them, update the source hashes, AST counts,
negative control and display expectation together. Do not reuse counts by name.
`core_source_files` binds all selected owning tests as well as the product delta;
prepare requires both frozen and composed files to match.

The two exact Hub postimages are under `qa/core-fix-inputs/tests/`:
`test_workflows.py` separates the chrome-or-atlas Node condition from chrome-only
npm; `memory_atlas/test_widget_transfer.py` retains the year-aware exact string.
The parent must integrate those bytes into the QA branch's product tests and then
recompute the product manifest. Preparation verifies this equality. This author
only wrote under `qa/`; the product test locations are unchanged here.

The retained product `.github/workflows/catalog-check.yml` is also under
`qa/core-fix-inputs/`. Catalog tests run in a separate exact Hub copy with that
workflow restored, because the temporary QA workflow is not their subject.
The copy's complete source manifest is checked before/after.

## Historical input

`qa/core-fix-history/` contains all 31 exact `n3/` files from the full R5 ZIP,
plus `fixture-source.py`. The source ZIP SHA-256 is
`54878c4e53f848936932e8ab4233cbe1bfebd2fb6a5686f792c8aefddbe56556`.
`qa/core-fix-inputs/history-manifest.json` records each path/hash and origin.
The old fixture comes from commit `42aab559faca79bac21bf7630a82ac9fb9667702`;
its SHA-256 `15f09c3979bfd54ef52407a442b176639eb253aac0585f5fe513b648997a44b4`
exactly matches the original writer receipt. The Git blob is checked again in
cloud. It must not be compared with the repaired `test_chronicle_transfer.py`.

The observer imports the retained fixture's helpers with the selected core first
on `sys.path`; its reader, store, view and fallback runtime are the selected core.
Reading never invokes the fixture's writer. Each observer gets a new full copy.
Only that copy's derived chronicle index is removed, with hashes recorded, to
force replay. Original history, CAS, writer receipt and prior observations stay
byte-identical. This historical transaction remains distinct from the owning ISO
test's 13 newly published synthetic records.

Selected data were inspected as data: synthetic Ada Example, operator-one,
Alpha/Beta and fixture conversations; text/binary scans found no owner identity,
private host paths or credential-like values. SQLite was inspected read-only and
immutable. Retain the whole exact input, including its SQLite rollback journals.

## Cloud matrix

AST enumeration is retained in `.review-evidence/20261117-167b0d34/ast-counts.json`.
The counts for these exact bytes are **224 on 3.10 and 225 on 3.12**, not 232/233.
The 115 oracle rows are included. The real publication/replay is one pytest case
which checks 13 records. Collection and JUnit must both match; any skip fails.

| Family | Selection | Expected |
| --- | --- | --- |
| ISO 3.10 | whole ISO file, deselect only native differential | 224 PASS |
| ISO 3.12 | whole ISO file, including native differential | 225 PASS |
| Capacity / inventory | capacity file / exact inventory test | 6 / 1 PASS |
| Transfer | transfer file | 80 PASS |
| Story | common_story, memory_view_story, memory_story_labels, legacy_memory_notice, chronicle_import | 81 PASS |
| Fallback | memory_fallback, sources, reachability, max | 79 PASS |
| Size | `-m size_ratchet tests/`, explicit target base | 7 PASS |
| Atlas | mixed-part test only, two engines × two viewports | 4 PASS |
| Catalog | corrected `tests/test_workflows.py`, actual product workflow | 16 PASS |
| Before 3.10 / 3.12 | exact real publication/replay ISO case over e6 | 1 / 1 EXPECTED_FAILURE |
| Historical readers | before 3.10, after 3.10, after 3.12 | observations, then pair comparison |

The before controls overlay only the exact new test file onto
`e6c85292beed8dce43abac5c6fe4002b6f57aec9`. They must fail at
`assert (first, end, next_start) == (0, total, None)` in the real node reader.
The failing `>` traceback line, AssertionError, node name, count, rc=1, no
errors/skips, and custody are required. Missing helpers, collection errors or
later unrelated assertions give no negative credit. Old 3.12 is not assumed PASS.

The historical pair retains node header/body, full/F3/fold, exact metadata,
original ISO and actual CAS bytes. Repaired readers must agree across Python
versions and show the stated period and date-only written date exactly, with the
whole authored body readable. Original bytes and journal remain unchanged.
An unbounded floor observation keeps `fit_status=UNVERIFIED`.

The existing workflow prepares the locked 3.12.13 writer environment before 3.10.
Its exact executable is passed through `safe_test`'s explicit spec environment.
Independent families continue after a red; 3.12 runs after an addressed 3.10 red.
Custody errors and known diagnostics are retained, never filtered into PASS.
Source audits and the existing small/full `always()` uploads remain mandatory.

Once bound, the parent can dispatch the existing temporary
`qa/memory-transfer-*` branch with `phase=core-fix`. Each Actions attempt has new
runner/evidence roots; do not reuse a previous output directory. Upload all input
files, including dotfiles, empty files and SQLite files that Git may ignore.
These QA files and synthetic artifacts do not belong in the production Hub PR.
