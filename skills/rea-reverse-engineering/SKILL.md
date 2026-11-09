---
name: rea-reverse-engineering
title: REA reverse engineering
description: Use morluto/rea for evidence-grounded analysis of shipped binaries, packages, browser runtimes, captures, and reconstruction tasks without copying its large runtime into Ouroboros.
version: 0.1.0
type: instruction
when_to_use: Use when a claim depends on a shipped artifact, binary or package behavior, decompilation, runtime observation, browser capture, or a version comparison that ordinary source-repository tools cannot establish.
model_experience:
  what_model_sees: One on-demand playbook for selecting an REA workflow, preserving Evidence, and avoiding unscoped setup or target effects.
  token_effect: One compact Markdown body on rounds that use shipped-artifact or runtime evidence; no tools or automatic child work are added.
---

# REA reverse engineering

This skill is a small Ouroboros adaptation of the public **morluto/rea** workflow. It does not bundle REA, its MCP server, analysis engines, native bridges, or third-party dependencies. Current source/release facts observed for this adaptation: repository `https://github.com/morluto/rea`, release `rea-agents-6.8.0`, npm package `rea-agents` 6.8.0, MIT for REA-authored code. Re-check the current release and the connected server's actual catalog before relying on a newer feature.

## When to use it

Use REA when the answer depends on a shipped binary/package, decompilation, a browser or desktop runtime, a network capture, or behavior not established by a complete source repository. For ordinary source-repository architecture or code review, use Ouroboros's normal repository tools instead; REA is not a reason to duplicate source analysis.

Start by writing a small checklist: target identity, exact question, static versus runtime evidence, required provider, permitted effects, and what would remain unknown. Keep the target path and provenance explicit. A path or filename mentioned in unrelated history does not select a target.

## Route the smallest workflow

Prefer the connected REA MCP server when its actual tool list and schemas are available. Use the supplied tool directly; do not invent operation names from this document. Start with a compact summary/inventory and follow retained Evidence references for selected details. Keep the complete Evidence reachable and cite its identity in the result.

Typical routes are:

- ASAR or extracted Electron/JavaScript tree: `analyze_javascript_application`.
- Native executable, library, archive or package: `open_binary`, then the focused advertised analysis tool.
- Browser observation or a user-owned browser page: the advertised browser-target/capture workflow.
- Recorded HAR/SAZ/PCAP/qlog or other capture: the matching inspection workflow.
- Managed, Android, firmware, SQLite or other format: use the format-specific operation only when the connected catalog advertises it.

Static analysis does not establish runtime behavior. A runtime, browser, instrumentation, injection or write workflow is a separate effect and must name its target, scope, lifecycle, cleanup and evidence. Never treat a provider's readiness report as an observed result.

## If REA tools are absent

A skill-only install supplies guidance but does not register an MCP server or install engines. If package acquisition is within the task's authorized scope, a CLI fallback is possible from a trusted directory outside the target:

```bash
npx -y rea-agents@6.8.0 analyze-javascript-application /absolute/path/to/app --json
```

Prefer an existing `rea` installation when present. Package-runner startup, client registration, provider readiness and target analysis are separate facts. `rea setup`, client configuration edits, Hopper installation, engine installation, and provider sign-in require their own explicit scope; setup must be previewed and its proposed paths/changes approved before it writes. Never install Node, npm, Java, Ghidra, IDA, Hopper or another unrelated dependency just to make a workflow appear available.

If a connection or provider fails, preserve the typed failure and useful partial Evidence. Read the upstream recovery guide before retrying; do not repeat an unchanged call when it may already have started or changed a target. A missing provider is not proof that the target or capability does not exist.

## Safety and licensing boundary

Default to read-only static work. Do not execute, instrument, inject into, modify, delete, upload, or publish target material unless the task explicitly authorizes that exact effect and the workflow's evidence/cleanup contract supports it. Preserve caller-selected paths, URLs, digests, timestamps, omissions and unknowns; do not persist the ambient environment or secrets merely because a child process inherited them.

REA does not grant licenses for Hopper, IDA Pro/Hex-Rays, JEB, Ghidra or other caller-supplied engines. Bring-your-own commercial tools must remain within their licenses. Do not bypass activation, share keys, crack tools, or publish decompiled material whose license forbids it. REA's MIT license does not relicense its third-party dependencies or external engines.

## Reporting

Separate observations, inferences and unknowns. Report the Evidence/source identity, coverage boundary, target digest or version when available, provider/runtime actually used, and the exact gaps that matter. Do not call pseudocode original source, a static graph complete, a setup plan an installation, a provider check an execution, or a queued/accepted operation delivered. If the target was not executed, say so. If a workflow only prepared or inspected a candidate, say what remains before adoption or publication.

This skill is a playbook, not a permission grant. Ouroboros still chooses the route, owner, model, review, delegation and stopping point under the current task's authority and the upstream REA source's fail-closed guidance.
