# Temporary memory-transfer qualification

Copy this directory and the companion `catalog-check.yml` only to a `qa/memory-transfer-*`
branch of OuroborosHub. The existing workflow filename already supports manual dispatch.
This is one disposable macOS Actions job, not a product workflow or publication gate.
It uses no repository secrets; it reads published Git sources and synthetic test memory.

`subjects.json` binds the frozen core, actual target and Hub product tree (all original
tracked paths except the replaced workflow). No downloaded QA archives are required.
The source commit must be published by its integrating parent before dispatch.
Preparation uses Git's mechanical merge tree, without a merge commit or conflict resolution.

Order: exact shipped 3.10.22; whole transfer and owning suites on frozen and composed
core; size against the actual target; out-of-process 2.1/2.5 compatibility; whole Atlas;
existing real-host capture scenarios; C1/C3/F3 negative witnesses; five datetime cases
on 3.12.13. Every pytest selection is collected first and executed once. First red
stops subsequent work; planned unrun stages remain `NOT_RUN`. No skip/xfail is accepted.
No Linux or second browser matrix is added. The 3.12 comparison exercises the known
`fromisoformat` difference for historical long ISO fields, not a replacement runtime.

The core's setup action runs at the workspace root. Hub is moved to runner temporary
storage first, preventing Hub from being copied recursively into each real host.
Dependencies use the core lock with no editable project install. Browser versions come
from that lock; Node 24 resolves through setup-node. Actual versions are in runtime.json.
Downloads can fail: there is no automatic Python fallback.

The artifact contains raw command/stdout/stderr/return codes, collected nodeids, JUnit,
results.json, source manifests and before/after archives, full retained synthetic
state, and visual/frames raw PNG, DOM and text. A separate negative-source archive plus
the core-before archive and retained patch reconstruct each mutant. Capture uses the
existing RealHost/Card/transfer producers and unchanged B8 scenarios. It records all
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
