# Temporary memory-transfer qualification

Copy this directory and the companion `catalog-check.yml` only to a `qa/memory-transfer-*`
branch of OuroborosHub. The existing workflow filename already supports manual dispatch.
This is one disposable Linux Actions job, not a product workflow or publication gate.
It uses no repository secrets; it reads published Git sources and synthetic test memory.

`subjects.json` binds the frozen core, actual target and Hub product tree (all original
tracked paths except the replaced workflow). No downloaded QA archives are required.
The source commit must be published by its integrating parent before dispatch.
Preparation uses Git's mechanical merge tree, without a merge commit or conflict resolution.

For the addressed R4 repeat select dispatch input `phase=frames-only`. This selects
only the unchanged 21 views × 4 cells and one N3 observation (C1 historical journal
written on 3.12.13, replayed/rendered on shipped 3.10.22). `selection.json` explicitly
names omitted suites; it does not credit them again. N3 runs even when frames fail.
Its full/F3 texts, snapshot, record, selected C1 setup and lengths are retained;
the existing unbounded floor path is exercised, while physical fit stays
`UNVERIFIED` without a source-bound model window and complete request envelope.
The normal `full` dispatch remains available with its original selections.

The repeat adds one diagnostic request header through the existing bridge, observes
the product's own body reads (no clone or extra drain), and records request identity,
step boundaries, native EOF, bridge bytes/end, consumer JSON completion, abort stack,
port close, frame detach and browser close. Raw `*.requests.jsonl` survives failures.
Only a unique request with 200, identical retained native/delivered bytes, EOF,
bridge end and successful Atlas JSON consumption strictly before its own abort may
be classified `PROVEN_ABORT_AFTER_CONSUMPTION`. Other failures stay red, including
missing/ambiguous identity or chronology. Classification occurs after close;
raw request errors are never removed. This instrumentation can affect timing and
does not retroactively explain R4: its 12 aborts and frames FAIL remain unchanged.

Order: exact shipped 3.10.22; whole transfer and owning suites on frozen and composed
core; size against the actual target; out-of-process 2.1/2.5 compatibility; whole Atlas;
existing real-host capture scenarios; C1/C3/F3 negative witnesses; five datetime cases
on 3.12.13. Every pytest selection is collected first and executed once. Each independent family records its own failure and the remaining families continue;
the overall run still fails. Source preparation and mutation identity checks
remain fatal: invalid subjects never authorize the comparison. Planned unrun stages
remain `NOT_RUN`. The 3.12 comparison runs after product failures only when `run`
reaches the end of its source checks; its aggregate verdict retains earlier reds. No skip/xfail is accepted.
macOS runner acquisition failed before any step; this job uses Linux with both browser engines.
It does not claim native macOS qualification. The 3.12 comparison exercises the known
`fromisoformat` difference for historical long ISO fields, not a replacement runtime.

The core's setup action runs at the workspace root. Hub is moved to runner temporary
storage first, preventing Hub from being copied recursively into each real host.
Dependencies use the core lock with no editable project install. Browser versions come
from that lock; Node 24 resolves through setup-node. Actual versions are in runtime.json.
Downloads can fail: there is no automatic Python fallback.

The `memory-transfer-small-*` artifact uploads first: all top-level JSON, stdout,
stderr, XML, text and setup logs, plus raw PNG/DOM and visual manifests/text. It excludes
source/state tarballs. `memory-transfer-full-*` separately retains the entire evidence
directory even if the small upload fails. Both uploads run after failed qualification.
The full artifact contains raw command/stdout/stderr/return codes, collected nodeids, JUnit,
results.json, source manifests and before/after archives, full retained synthetic
state, and visual/frames raw PNG, DOM and text. A separate negative-source archive plus
the core-before archive and retained patch reconstruct each mutant. Capture uses the
existing RealHost/Card/transfer producers and unchanged B8 scenarios. All 21 views
reopen their own route in each of four cells; the manifest checks and retains the
actual route before and after capture, including exact-field and stored-record views.
A wrong route is a failed case, not visual evidence for its filename. It records all
HTTP failures; the suite's own card contract judges widget-route failures (unrelated
host maintenance probes may return 503). No DOM rewriting or mock widget replaces it.
The hosted runner has network access; the synthetic host uses the core ModelStub.

`EXPECTED_FAILURE` requires exact testcase names, failure sites, nonzero pytest exit
and no collection errors/skips. It is not a product PASS. `ASSERTION_FAILURE` identifies
an executed assertion, not its root cause. `COLLECTION_ERROR`, `EXECUTION_ERROR` and
`INCOMPLETE_OR_SETUP_ERROR` require diagnosis, never a negative-control credit.
Every `safe_test` invocation, including collection, also reads the terminal
`result.json` at its announced `SAFE_TEST_EVIDENCE` path. Missing, unreadable or
nonterminal receipts and containment/evidence errors make `qualification_status`
`FAIL`, independently of test status, rc and JUnit. Raw receipts and custody facts
are retained separately; ordinary `--keep` debug retention is not an error.
Setup-action logs remain in the Actions run log; retain that log with the artifact.

Keep all temporary files out of the final product PR. Qualification does not update
Hub endorsement, catalog generation or the production core pin. The parent handles
those after the real core publication and final independent review. A green workflow
is not visual acceptance: inspect the PNG/DOM evidence at its actual viewport sizes.
