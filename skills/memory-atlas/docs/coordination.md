# Memory Atlas — coordination record

## Owner decisions
- Owner answer, 2026-09-06, quiz `3e91c716bd4a4f16b01782dfa41a6eb3`: initially selected core memory plus projects. Its exclusion of consolidated dialogue is superseded by the later direct owner instruction below.
- Superseding owner answer, 2026-09-06, direct instruction: some memory files were missing, specifically the dialogue chronicles / summary blocks. Include consolidated dialogue memory (`memory/dialogue_blocks.json`, `memory/dialogue_meta.json`, and legacy `memory/dialogue_summary.md`) as a first-class source because it contains consolidated summary blocks, not raw chat. Continue to exclude raw `logs/chat.jsonl` and its archives, `owner_mailbox`, control state, settings, secrets, and tool/execution logs. All served sources remain read-only and unpublished. This is the single current authorization scope.
- Owner answer, 2026-09-06, quiz `07f584afef014a08ae11bdfe32046572`: Atlas + comfortable reading.
- Owner answer, 2026-09-06, quiz `fb337665b6e94268a8ef807c0a3d2cf2`: text on click. Its provisional allowance for a separately labelled relationship-guessing layer was superseded by the later direct owner instruction requiring a deterministic graph of proven links only, with no additional model calls.
- Working assumptions: none in the current authorization scope; implementation details not explicitly identified above as owner answers remain engineering decisions.

## Authorship and topology
Substantial planning and implementation MUST run through configured Agent sessions, not root API authorship.
- Root: integration, questions, lifecycle, verification.
- Astra `subagent_sa5g1l`, task `2115ee41`: architecture planning; requested Luna `subagent_0p9q5f_copy_4ebdkr` inventory and Terra `subagent_0p9q5f` API-contract grandchildren.
- Opus `primary-builder`, task `161b4dd9`: visual planning; requested Cursor `fast-scout` creative critique grandchild.
- Fable `subagent_osabav`: one later bounded high-value critique.
- Sol `subagent_7caw34`: independent correctness/security verification.
Target host-visible depth: 2. These are requests, NOT execution receipts; actual results must be reconciled.

## Budget
Owner target <= $200; runtime task-start graceful ceiling $135.690387, stricter internal target $130. Planning $25, builds $45, integration/tests $25, review/live QA $30, reserve $10 (approximate planning envelopes, not settings).
Count root + nanny + review API spend as recorded by host; disclose session spend separately and include disclosed charges. Unknown subscription spend remains unknown (no fabricated bound or $0). Checkpoint a reviewable workspace payload by $70 if work progresses; stop optional scope by $90, reserve rest for live integration/review. If blocked, preserve plan/evidence instead of claiming a payload exists.

## Consumer contract
New `memory-atlas` extension, PluginAPI 2.0; `kind: module` opaque-origin iframe. Own-prefix `OuroborosWidget.fetch` only, no eval/CDN/storage/same-origin assumptions, local reviewed dependencies. Dispose via `__ouroWidgetOnDispose`, host owns frame lifecycle. English interface; Russian owner dialogue. Reference claudexor_quotas v0.4.1 exists and is read-only reference, not a modification target.

## Ownership before builders
Architecture and visual plans are separate returned documents. Implementation ownership will be fixed from the integrated contracts before builder admission: backend/API tests versus widget UI/tests; no overlapping files. Only root materializes live external skill payload manifest-first and performs skill lifecycle.

## Initial review
Plan fingerprint d2e6abf6afaab523d0fcf4aff1087c7f907758729385720636c0f55f780fd699: REVISE_PLAN, advisory. Continuing planning/evidence only; concrete roster, iframe, accounting and owner allowlist concerns accepted for next spec.

## Settled decisions and explicit reassignment (2026-09-06)
- Answered `07f584afef014a08ae11bdfe32046572`: Atlas + comfortable reading.
- Answered `fb337665b6e94268a8ef807c0a3d2cf2`: text on click. Its relationship-guessing clause was later superseded by the proven-links-only owner instruction recorded above.
- Astra physical run `run-164dda091dfa` failed unsupported installed Codex CLI; no Astra authored plan. Luna `2a12bc10` / `run-fece25eb9d7b` and Terra `825b6d79` / `run-2c0db4a5330c` succeeded at host-visible depth 2.
- Cursor `c9d6e8e8` / `run-e6320f8aa442` succeeded at depth 2 under Opus visual branch (parent handoff pending).
- Fable `27f7f08c` / `run-95dd886b371d` failed exhausted credential pool; no Fable docs. Empty capture rejected by owner nanny. No retry loop or secret actor substitution.
- Explicit reassignment: Sol `6f0dfcc8` authors backend + exact schema docs first. Sol is therefore NOT independent verifier of its own backend. Independent finished-backend verification will go to a fresh Terra task (not backend author); original role plan was preliminary and revised for actual route availability. Opus authors UI only after schema handoff.
- Plan cycle 2 `21ca9f084d0967c362082f5bb3966f44ac434eded3d3c8983cf81641cdd3ad39` is advisory REVIEW_REQUIRED: reviewer disputes Sol role change. We preserve independence by a distinct verifier, not by calling author review independent. Continue with review open and disclose it.
