---
name: anime_studio
description: AI-powered 2D anime generator with VLM-verified assets, video analysis via Gemini, sequential keyframes, scene continuity chain, multi-model image/video generation, LLM-powered error recovery, and parallel asset+music pipeline
version: 3.3.1
type: extension
entry: plugin.py
plugin_api: "2.0"
permissions: [net, route, widget, ws_handler, tool, read_settings, subprocess, companion_process, inject_chat]
env_from_settings: [OPENROUTER_API_KEY]
when_to_use: User wants to generate a short animated 2D anime cartoon, music video, or animated scene with consistent characters, VLM verification, and narrative continuity.
timeout_sec: 300
dependencies: [Pillow]
companion_processes:
  - name: anime_worker
    command: [python3, scripts/anime_worker.py]
    runtime: python3
    restart_policy: on_failure
ui_tab:
  tab_id: studio
  title: Anime Studio
  icon: film
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: form
        title: "🎬 Generate Anime"
        route: generate
        method: POST
        mode: job
        status_route: status
        fields:
          - name: theme
            label: Theme / Story
            type: textarea
            placeholder: "A young samurai discovers a magical sword in an ancient temple..."
            required: true
          - name: style
            label: Anime Style
            type: select
            options: ["modern anime", "retro 90s anime", "chibi cute anime", "dark gothic anime", "watercolor anime", "Studio Ghibli style", "cyberpunk anime", "shounen action anime"]
            default: modern anime
          - name: mood
            label: Mood
            type: select
            options: ["adventurous", "comedic", "dramatic", "melancholic", "mysterious", "romantic", "action-packed", "wholesome"]
            default: adventurous
          - name: duration_sec
            label: Duration (seconds)
            type: number
            default: 30
          - name: num_scenes
            label: Number of Scenes
            type: number
            default: 4
          - name: quality_mode
            label: Quality Mode
            type: select
            options: ["low", "medium", "max"]
            default: medium
          - name: budget_limit_usd
            label: "Budget limit USD (0 = auto)"
            type: number
            default: 0
          - name: resolution
            label: Resolution
            type: select
            options: ["480p", "720p", "1080p", "2K", "4K"]
            default: 720p
          - name: image_model
            label: Image Generator
            type: select
            options: ["gpt-image-2", "gpt-5-image", "gpt-5-image-mini", "nanobanana", "gemini-3-pro-image", "flux.2-pro", "flux.2-max", "seedream-4.5", "grok-imagine"]
            default: gpt-image-2
          - name: video_model
            label: Video Model
            type: select
            options: ["bytedance/seedance-2.0", "bytedance/seedance-2.0-fast", "bytedance/seedance-1-5-pro", "minimax/hailuo-3", "google/veo-3.1", "google/veo-3.1-fast", "google/veo-3.1-lite", "minimax/hailuo-2.3", "kwaivgi/kling-v3.0-pro", "kwaivgi/kling-v3.0-std", "kwaivgi/kling-video-o1"]
            default: "bytedance/seedance-2.0"
          - name: music_style
            label: Music Style
            type: select
            options: ["orchestral cinematic", "electronic ambient", "acoustic guitar folk", "j-pop instrumental", "lo-fi hip hop beats", "epic battle drums"]
            default: orchestral cinematic
        submit_label: "🎬 Generate Anime"
      # Mirrors plugin.py::register_ui_tab, which is what actually loads. The
      # manifest is the REVIEWED description of the trusted UI surface, so a
      # component present in code and absent here is a documentation defect.
      - type: file
        path: result_download_url
        label: "🎬 Download Video"
        condition_key: result_download_url
        filename: anime_video.mp4
      - type: subscription
        event: studio_progress
        render:
          - type: progress
            value_key: progress_pct
            label_key: message
          - type: gallery
            title: Character Sheets
            items_key: character_sheets
            item_type: image
            route_prefix: "asset?path="
          - type: gallery
            title: Keyframes
            items_key: keyframes
            item_type: image
            route_prefix: "asset?path="
          - type: key_value
            title: Verification
            items_key: verification_display
            condition_key: has_verification
          - type: key_value
            title: Warnings
            items_key: warnings_display
            condition_key: has_warnings
---

# Anime Studio v3.3.1

A professional-grade 2D anime cartoon generator with **VLM-verified assets**,
**video analysis via Gemini 3.1 Pro**, **sequential keyframes for continuity**,
**multi-model image/video generation**, **LLM-powered error recovery**, and a
**parallel asset + music pipeline** — now with explicit quality modes, a
self-contained budget rail, honest `partial` delivery, and a two-VLM identity
judge panel used as a hard gate.

## What's New in v3.0.0

### 1. Three quality modes

| Mode | Video candidates | Continuity policy | Continuity regens | Judges | Scene cap | Est. USD / scene |
|------|-----------------|-------------------|-------------------|--------|-----------|------------------|
| `low` | 1 | off | 0 | 0 | 4 | $3.20 (extrapolated) |
| `medium` (default) | 2 | adjacent | 1 | 1 | 8 | **$7.39 — MEASURED** |
| `max` | 3 | all_recheck | 2 | 2 | 24 | $13.00 (extrapolated) |

Cost honesty: `medium` is the ONLY measured figure — $7.39/scene, derived from
exactly one 2-scene run that cost $14.77. The `low` and `max` numbers are
extrapolations from that single data point (fewer/more candidates, judges, and
continuity passes), not facts. Treat every non-medium estimate as a planning
aid, never a billing statement.

### 2. Self-contained budget rail

This skill calls OpenRouter directly with a granted key, so **core accounting
cannot see its provider spend** — the host records the usage as
unknown/unmetered, and the core budget rail cannot protect a job. The skill
therefore carries its own rail:

- A **preflight estimate** decomposes the job into unit costs (storyboard,
  sheets, keyframes, video candidates, judges, cross-scene checks) before
  anything is generated.
- If the estimate exceeds the budget limit, the request is refused with
  **HTTP 402** naming the exact required amount — nothing generated, nothing
  spent.
- A **running ledger** of estimated spend is persisted into `job.json`
  (`progress.budget`) as the job runs.
- On limit breach, a **hard stop** stops starting new scenes while
  **preserving every asset already generated** — the job assembles what it has
  and ships `partial`, never discarding paid work.

### 3. `partial` status semantics

A job that finishes with missing or unverified scenes is still delivered — the
final video ships with the assembled scenes, and the job reports
`status: "partial"` with `missing_scenes` (indexes with no video clip),
`unverified_scenes` (indexes shipped without a passing verify), and
`partial_reasons` listed. A partial job **never** reports "Animation
complete!"; its final message names the delivered/expected scene counts, the
missing and unverified indexes, and the reasons.

### 4. Structural audio probe (present / absent / unknown)

The final-mix audio probe is now structural with three states: `present`,
`absent`, `unknown`. The previous substring-matching probe returned `False` on
ANY exception, and `False` routed the mix into the music-only branch — which
**silently destroyed the scenes' native spoken dialogue**. An `unknown` probe
now attempts the dialogue-preserving mix FIRST and falls back to music-only
only if that mix actually fails, with a warning either way.

### 5. VLM identity judge gate (hard gate; panel size is per-mode)

Scene clips are gated by VLM judges drawn from **DIFFERENT model families**,
never the generator's family. **The panel is two judges only at `max`; `medium`
runs ONE judge and `low` runs none** — see "Correction: `medium` runs ONE judge"
below, which is the authoritative statement of this contract. A single judge
cannot deliver the cross-family disagreement signal, so at `medium` this gate is
a fail-closed single-judge check, not a panel. Every comparison runs in **both
A/B orderings**; an order-inconsistent verdict is downgraded to `indeterminate`.
Judges answer **atomic per-attribute questions** (hair side, accessory, eye
colour, character count) rather than an overall quality score, and
`indeterminate` is a first-class outcome that is **never a pass** — "could not
judge" cannot look like "judged and passed".

This hard VLM gate was the owner's explicit choice over objective metrics.
The published VLM-judge bias literature (position bias, self-preference bias,
verbosity bias) is exactly why these mitigations — family diversity, order
swap, atomic questions — are mandatory rather than optional.

`identity_similarity_proxy` is a Pillow colour-histogram cosine kept as a
diagnostic breadcrumb — it is NOT DINOv2, NOT calibrated, has no threshold,
and gates nothing.

**Owner ruling (settled, not an open option): no local models.** Identity is
judged ONLY by the OpenRouter VLM panel above. This payload carries no local
ML dependency — no `torch`, no local DINOv2, no local face/ReID embedding —
and must not acquire one; `dependencies:` is `[Pillow]` on purpose. A local
embedding cosine is therefore not a future upgrade path here, and no value in
this skill may be named `identity_verified` or used to gate a clip.

### 6. Raised caps: 24 scenes, 240 seconds

The global scene cap is now 24 and the duration cap 240 seconds. They had to
move **together**: scene count is bounded by `duration_sec // 4`, so a 20+
scene job was structurally unreachable while duration was capped at 60s —
raising the scene cap alone would have changed nothing.

### 7. Continuity regenerates on minor drift too

Cross-scene identity drift now triggers regeneration on **minor** verdicts as
well, not only major ones — minor drift is exactly how identity erosion
compounds across a chain of scenes. Regeneration is bounded by the per-mode
regeneration budget (0/1/2 for low/medium/max); once exhausted, the job ships
with a disclosed warning and `partial` status rather than silently.

## Operational facts

- **These local edits are mortal.** This payload lives under
  `data/skills/ouroboroshub/`, and a marketplace update OVERWRITES it. Any
  local fix that matters must be upstreamed to the hub or it will be lost on
  the next install/update.
- **`kill <pid>` does NOT reload changed code.** The generation worker is a
  host-supervised companion process with a restart policy: killing it just
  respawns the same process image, and an already-running worker keeps
  executing the OLD code after payload edits. The skill must be reloaded
  (disable + enable, or a host reconcile) for code changes to take effect. A
  stale worker once held pre-revision code for 4.5 hours.
- **`inject_chat` is narrow by construction.** The manifest declares the
  permission ONLY for the structured PARTIAL/ERROR attention notification in
  `host_bridge.py`: a fixed template with numeric/enumerated substitutions
  (job id, mode, scene counts, whitelist-filtered reason keys), never
  free-form content — not the theme, not the storyboard, not dialogue, not
  warning strings. The skill functions fully without the grant; an ungranted
  call is a logged 403 and generation proceeds unaffected.

## Pipeline

1. **Scenario Generation** — LLM creates a structured storyboard with scene
   transitions, dynamic action descriptions, and narrative continuity cues
2. **Asset + Music Generation (parallel)** — Character sheets (parallel), location
   art (parallel), keyframes (SEQUENTIAL with inter-frame context), and music
   all run concurrently via `asyncio.gather`
3. **Image VLM Verification** — Each critical asset (characters, keyframes) is
   checked by Claude Sonnet for correctness and regenerated if needed (up to 2 retries)
4. **Video Animation** — Sequential scene generation with:
   - Full continuity chain (last frame → next scene reference)
   - ALL character sheets as references (not just first)
   - Explicit character identity descriptions in prompts
   - Negative prompting against text artifacts
   - **Video VLM verification via Gemini 3.1 Pro** (max 1 retry per scene)
   - **Smart error recovery** — LLM advisor recommends model switch on failure
5. **Assembly** — ffmpeg concatenation with audio mixing

## Models Used (via OpenRouter)

| Model | Purpose |
|-------|---------|
| `openai/gpt-image-2` | Character sheets, locations, keyframes (default, with fallback on transient failures) |
| `google/gemini-3.1-flash-image-preview` | Character sheets, locations, keyframes (nanobanana option and fallback) |
| `anthropic/claude-sonnet-4.6` | Scenario generation + image VLM verification |
| `google/gemini-3.5-flash` | **Error advisor + prompt condensation** — analyzes failures, condenses long prompts for models with character limits |
| `google/gemini-3.1-pro-preview` | **Video VLM verification** (multi-frame analysis) |
| `bytedance/seedance-2.0` | Video generation (default) |
| `google/veo-3.1` | Video generation (alternative — Google quality) |
| `minimax/hailuo-2.3` | Video generation (cheapest option) |
| `kwaivgi/kling-v3.0-pro` | Video generation (less strict copyright filter, auto-condensed prompts) |
| `google/lyria-3-pro-preview` | Original soundtrack clips |

## Requirements

- `OPENROUTER_API_KEY` with access to the models above
- Operator-provided `ffmpeg` **and** `ffprobe` on the host (video assembly +
  frame extraction). Install via package manager (`brew install ffmpeg`,
  `apt install ffmpeg`, etc.) or set `FFMPEG_PATH` / `FFPROBE_PATH` to
  executable files. The skill does **not** download binaries.

## v2.14.0 — the cross-scene identity gate actually gates

Diagnosed from job `bb72b5fb`, a 2-scene run whose delivered video visibly drifted
(a white hair streak swapped sides between scenes) even though the pipeline's own
reviewer had returned `severity: "major"`. The gate existed — it was defeated by its
own bounds check.

- **A `major` verdict can no longer be silently discarded.** The reviewer answered
  with a "third scene" in a two-scene job, so `0 <= 2 < 2` was false and
  `_regenerate_single_scene` was never called — no warning, no stat, nothing. An
  out-of-contract index now falls back to the last compared scene and records a loud
  `cross_scene_index_fallback` warning instead of cancelling the finding.
- **Frame indices map back to real scene indices.** The reviewer only sees frames
  that could be extracted, so its Nth answer meant "the Nth frame I was shown", not
  "scene N". A scene whose clip was missing used to shift every later index silently.
  The `(scene, frame)` pairing is now explicit and the prompt states the mapping and
  the frame count, so the reviewer cannot name a scene it was never shown.
- **The reviewer's critique is fed back into the regeneration prompt.** Re-running a
  byte-identical prompt had no reason to drift less.
- **The repair is re-verified once.** Identity is re-checked after regeneration and
  the outcome recorded as `cross_scene_resolved`; a drift that survives is warned
  about explicitly rather than shipped looking clean. It does not loop.
- **`resolution` is selectable everywhere.** The agent tool, the widget form and this
  manifest's form had no resolution field at all, so `_build_settings` fell to its
  `"720p"` default and 1080p was reachable only by hand-crafting an HTTP body.
- **Scene cap raised 8 → 12.** A request for 10 scenes was silently clamped to 8.
  *(Superseded in v3.0.0: the cap is now per-mode — 4 / 8 / 24 — with a global
  ceiling of 24 scenes and 240 s. `modes.py` is the single source; this line is
  kept only as history.)*

## v2.13.0 — quality fixes (visual oracle, character consistency, honest degradation)

Diagnosed from a real 45s run whose keyframes drifted (long flowing hair in scene 0
vs short-cropped in scene 1), whose own cross-scene check reported that drift, and
whose final video shipped 30s instead of 45s while a valid scene clip sat on disk.

- **Image generation moved to the canonical `POST /api/v1/images` endpoint**
  (`api_client.generate_image_unified`). The previous path used
  `/chat/completions`, which cannot carry reference images at all. The legacy
  path is retained as the last rung of the fallback ladder.
- **Reference-image conditioning between scenes.** Each keyframe is now
  conditioned on up to 4 references with reserved slots: the previous keyframe
  (only when it PASSED verification), up to two character sheets for the
  characters in the shot, then the location art. Character identity was
  previously carried by prose alone, which drifts.
- **A rejected frame is never fed forward** as a reference, so verification
  failure cannot compound into the rest of the run.
- **The visual oracle keeps the BEST candidate,** selected by a normalized 0-10
  score (`_normalize_vlm_image_result` — score is canonical, `passed` is derived,
  so a self-contradicting verdict resolves one way). It stays deliberately
  non-blocking (a keyframe is a required input to the video chain), but an
  all-rejected outcome is now stated loudly with its score and counted as
  `keyframe_rejected_used`, instead of silently shipping the last retry.
- **Every model degradation is LOUD.** A fallback to nanobanana (flash tier) or
  to the legacy path reaches `job.progress.warnings` and increments
  `image_model_fallbacks`. A parameter-shaped 4xx (too many references) retries
  the SAME model with fewer references rather than dropping to a weaker one.
- **Provider capabilities are read live** (`/images/models`, `/videos/models`)
  once per job. Optional request parameters are omitted when the catalog does not
  list them, and video durations are clamped from the live snapshot instead of a
  hardcoded per-model table.
- **A generated clip is never lost to a later failure.** Candidates are ranked
  (pass beats fail, then the existing multidim weighted score, ties keep the
  earlier attempt) and the best is recovered as `clips_recovered`.
- **Diagnostics name the exception TYPE** (`_describe_exc`). Many transport
  exceptions have an empty `str()`, which previously produced warnings like
  `Video scene 3 failed: ` — no cause, no type.
- **Story causality.** The storyboard schema requires a `causal_link` per scene in
  BECAUSE OF THAT / BUT form, and rules forbid an AND-THEN chain and require a
  setup -> complication -> escalation -> payoff arc.
- **`SKILL_VERSION` is stamped into every job** (`verification_stats.skill_version`),
  so a companion worker still running pre-edit cached code is detectable.

Verified offline with a fake client against the real pipeline methods (25 checks:
reference forwarding, same-model reference trim, loud fallback, best-of selection,
forward-reference trust gate, capability-driven duration clamp). No paid
end-to-end run was launched.

---

## v3.1 — reference conditioning actually reaches the model

### The defect this release fixes

The owner reported that a character matched the character sheet in the first
shot and then looked generated from the text description in the second. The
reference images were already being SENT (`input_references` was populated), but
**the prompt never mentioned them** while explicitly instructing the model to
follow the text description. Nothing bound image to instruction, so the model
was free to treat the attachments as decoration.

### Seedance multi-reference conditioning — what is documented vs measured

**Documented by the provider** (Seedance 2.0 API reference, docs.byteplus.com;
OpenRouter "Reference to Video" guide, openrouter.ai/docs/features/video-generation):

| Fact | Source |
|---|---|
| Reference images are accepted, 1–9 of them | BytePlus Seedance 2.0 API reference (`image_urls`, 1–9) |
| The prompt MUST address them as `@Image1`, `@Image2`, … | BytePlus Seedance 2.0 API reference |
| Over OpenRouter the transport field is `input_references` (`inputReferences` also documented) | OpenRouter reference-to-video guide |
| Reference support is per-model, not universal | OpenRouter reference-to-video guide |
| Hard first/last-frame anchoring is a SEPARATE field (`frame_images` + `frame_type`) from soft references | OpenRouter video docs + live `/videos/models` |

**Established empirically here, because no documentation states it.** OpenRouter
does not document a per-model maximum, an item schema for `input_references`, or
any signal that references were ignored:

- `input_references` is Zod-validated: each item must carry `image_url`,
  `audio_url` or `video_url`, otherwise the request is rejected before routing.
- `data:` URLs are accepted as reference items.
- **A misspelled field is silently ignored.** Sending `image_urls` (Seedance's
  own native name) through OpenRouter produced a normal accepted job with the
  references simply absent — no warning, no error. That silent-drop behaviour is
  why the redacted request audit below exists.
- **`@ImageN` resolves to `input_references` submission order.** Verified by a
  paid colour-discrimination probe: references `[solid RED, solid BLUE]` with a
  prompt commanding `@Image2`; the produced frame measured mean RGB
  `R=0 G=25 B=229` (blue). One-based, positional, and controllable.
- OpenRouter accepted 12 reference items without complaint, so its validator is
  NOT the enforcer of Seedance's documented 9 — the model is. The cap here is 9,
  from the vendor documentation.

**No provider mechanism reports that references were ignored.** There is no
field, warning, or status for it. That absence is the reason this release logs a
structural self-check instead of trusting the provider.

### How references are now assembled

`Pipeline._assemble_scene_references` is the SINGLE ordered source for both the
prompt labels and the `input_references` payload. Both are derived from the same
list after truncation, so the `@ImageN` number in the prompt cannot disagree with
submission order. Order is fixed: **character sheets first**, then the location
plate, then the previous approved frame — so the reference cap eats the
environment or the continuity anchor before it eats an identity anchor.

Labels are index-redundant on purpose (`@Image2 — the CHARACTER SHEET of Mochi:
…`), so even if numbering were ever mis-resolved the label still says WHO the
image shows. Authority is scoped **by kind**: a location plate governs
environment only, a continuity frame governs appearance across the cut, and the
scene text still decides action, camera and dialogue.

Reference state is explicit, never a silent text fallback:

- `complete` — every scene character had a sheet and nothing was dropped.
- `partial` — something is missing or was dropped by the cap; the job records a
  warning naming exactly what.
- `none` — nothing reached the request; the job records
  `verification_stats["scenes_without_references"]` and warns that identity is
  unanchored for that shot. Generation still proceeds; the state is disclosed.

### Request audit (host-verifiable, redacted)

Every `/videos` POST logs `VIDEO_REQUEST_BODY` and appends a redacted summary to
`verification_stats["video_requests"]` (a list, capped at 60, with
`video_requests_omitted` counting anything beyond — never a silent drop). The
audit sink is passed per call and fires BEFORE the POST, so a request that
raises still leaves evidence and retries cannot overwrite each other's record.

It carries counts, per-entry mime/byte-length/sha256 prefix, `frame_type`, and
`prompt_names_all_references` — a structural check that the prompt really named
every reference it attached. It deliberately carries **no** base64 payload and
**no** prompt text (the prompt contains owner-authored theme and dialogue).

### Reference cap raised 4 → 9

`VIDEO_MAX_INPUT_REFERENCES` was 4. With characters ordered first, a three-character
scene consumed the whole budget and the cap silently discarded the cross-scene
continuity frame — the exact anchor that keeps identity stable between shots. It
is now 9, the vendor-documented Seedance maximum. A video model accepting fewer
replies 4xx, and that refusal goes to the failure advisor like any other; only
the image path trims references and retries the same model.

### minimax/hailuo-3 added

From the live `/videos/models` catalog (not guessed):

| | `minimax/hailuo-3` | `bytedance/seedance-2.0` |
|---|---|---|
| catalog id | `minimax/hailuo-3` ("MiniMax: H3") | `bytedance/seedance-2.0` |
| resolutions | **`["2K"]` — 2K ONLY** | 480p / 720p / 1080p / 4K |
| durations | 5–15s | 4–15s |
| frame images | first_frame, last_frame | first_frame, last_frame |
| native audio | yes | yes |
| pricing SKUs | `duration_seconds: $0.13`, **`reference_images: $0.04`** | `video_tokens: $0.000007` |

**Does it support reference conditioning? Yes — and this is catalog evidence, not
an assumption:** it publishes a `reference_images` price SKU, so references are a
billed, first-class input. It has NOT been exercised live here, so its
reference-following QUALITY relative to Seedance is unmeasured.

**Is it cheaper? Yes, on published prices.** A 10-second shot costs
`10 × $0.13 = $1.30` plus `$0.04` per reference image — roughly `$1.46` with four
references, against a measured Seedance cost near `$3.3` per candidate at 720p/10s.
About 2× cheaper.

**Two honest caveats.**
1. It is **2K-only**. Selecting it with any other resolution is reconciled
   upward to 2K with a warning by the existing capability gate; `2K` was added to
   that gate's ladder, without which this model raised "supports none of the
   known resolutions" and was impossible to select at all.
2. **Cost estimation is calibrated for Seedance's token pricing.** MiniMax bills
   per second plus per reference image, which this estimator does not model, so
   its estimate for MiniMax is too HIGH. That errs toward stopping early rather
   than overspending, but it is an estimate mismatch and not a measurement.
   Kept on purpose in 3.3.1: every budget figure here is an approximate unit
   estimate, not a bill. For H3 the generic `$2.65` clip unit is deliberately
   conservative (about `$2.31` on catalog prices even at 15 s with 9
   references). It is no ceiling for other providers: a measured Seedance
   720p/10 s candidate cost near `$3.3`. A per-second price would have to
   follow the model, length and reference count of each actual dispatch.

It is **not** made the default for scenes with characters: Seedance's
reference-following is the behaviour actually measured here.

#### Independent corroboration from the model pages (read 2026-08-04)

The catalog table above was derived from `/videos/models`. The two OpenRouter
model pages were then read separately and agree with it, which is why these
numbers are stated as facts rather than as a single source's claim.

`minimax/hailuo-3` ("MiniMax: H3", released **2026-07-29**) — its own FAQ:

> "H3 generates video at 2K, supports 5–15 second clips, covers the 21:9, 16:9,
> 4:3, 1:1, 3:4 and 9:16 aspect ratios, can be steered with a first frame and
> last frame image and produces a matching audio track."

That independently confirms 2K-only, the 5–15s window, first/last-frame
steering and native audio. Its overview also states "native audiovisual output
for reference-driven generation".

`bytedance/seedance-2.0` — its own overview:

> "It supports text-to-video, image-to-video with first and last frame control,
> and multimodal reference-to-video. It is particularly strong at preserving
> character consistency, visual style, and camera movement from reference
> material. The number of tokens is given by
> (height of output video * width of output video * duration * 24) / 1024"

**Correction to the "cheaper" claim — the headline prices invert it.** The pages
show Seedance "from **$0.06726**/second" against H3 "from **$0.13**/second", so
per advertised second H3 is the *more* expensive of the two. The comparison above
is still the right one, but only because it is stated at comparable quality:
Seedance bills by tokens that scale with `height × width`, so its "from" price is
the floor at its cheapest resolution, while H3's per-second price is flat and
already includes 2K. Against the **measured** Seedance cost near `$3.3` per
720p/10s candidate, a 10-second H3 shot at 2K (`10 × $0.13` plus `$0.04` per
reference image ≈ `$1.46`) is cheaper at a higher resolution. Anyone comparing
the two headline `$/second` figures alone would reach the opposite conclusion,
which is exactly why this is written down.

**Minimum duration is 5s, not 4s.** When the capability snapshot has nothing for
a model (catalog unreachable, or the model absent from it), `_clamp_duration`
uses the global 4–15s range raised to that model's documented minimum from
`_MODEL_MIN_DURATION_SEC` (hailuo-3: 5). The map holds only minimums, so it can
move a value up toward a valid one and never invents a duration. The old
per-model allowed list had clamped `google/veo-3.1` to an invalid 5, and a global
floor of 5 would break Veo's real set `[4, 6, 8]`.

### Correction: `medium` runs ONE judge, not a panel of two

Earlier documentation implied a two-family judge panel at `medium`. That is true
only at `max`. At `medium` the identity gate runs a single judge
(`google/gemini-3.1-pro-preview`), which is a diagnostic gate: it can still fail
a shot and still refuses to green an unanswered checklist, but it provides no
cross-family disagreement signal. Two judges from different families remain a
`max` feature. This is a documentation correction, not a behaviour change.

### Identity checklist keys are now short ids

The judge checklist previously used the **entire question text** — which embedded
a character's whole `visual_traits` string — as the JSON key the model had to
reproduce verbatim, twice, for the forward and reversed pass to be matched. No
model reproduces hundreds of characters byte-identically, so coverage never
matched and every scene of the last live run returned `indeterminate` with an
empty `attributes` map. The gate could not pass by construction.

Keys are now short ordinal ids (`char_01_hair`, `char_02_eyes`,
`character_count`), collision-proof by construction rather than by slugifying
names that are frequently non-ASCII. Full descriptions stay in the question body.
Refusal remains fail-closed — partial coverage, order disagreement, a judge
error, or too few judges are all still `indeterminate`, never a pass — a green
verdict merely became reachable.

A character whose sheet did not reach the judge is **not** asked about (there
would be no ground truth) but its ids stay in the expected set, so an
unverifiable character forces `indeterminate` instead of a free pass.

### v3.2: ONE authority for the native-audio flag

`generate_audio` was recomputed independently in the main scene loop and again in
continuity regeneration, while the preflight capability check only **warned** — so
a model reporting `generate_audio=false` still received `generate_audio=true` on
all four paid dispatch paths, and a gate later applied to one path would have
silently missed the others. `_effective_generate_audio(job)` is now the single
authority: the owner's dialogue setting is the intent, the live capability
snapshot is the veto, and a suppression is recorded as
`verification_stats["native_audio_suppressed"]` so a mute clip states its cause
instead of looking like a generation failure. An unknown model keeps the owner's
intent, because a permissive flag can at worst be ignored by the provider, while
a restrictive one would mute a model that does support native speech.

### v3.3.1: changes over 3.2.0

**One response ceiling, 65536 tokens.** `DEFAULT_MAX_TOKENS` in `api_client.py`
is the `max_tokens` of every model call in this payload: chat, VLM verification,
the judge panel, comparison, condense, simplify and the advisor. A tight ceiling
on a call that must return structured output fails quietly: a reasoning model
spends the allowance on hidden reasoning before the JSON begins. With
`max_tokens=2048` the identity judge was reported to return `attributes: {}` on
every scene of a live job. 65536 is the largest value every text model called
here accepts in the public OpenRouter catalog read on 2026-11-16 (Gemini 3.x:
65,536; Claude Sonnet 4.6: 128,000); recheck it before raising the constant.
Only emitted tokens are billed, but OpenRouter can refuse a request whose
ceiling the remaining credit cannot cover (HTTP 402), and chat calls do not
retry that. When a structured reply comes back empty or cut off, check
`finish_reason` and whether `completion_tokens` sits at the ceiling first.

**Paid clips are kept, and an unknown outcome is never paid twice.** Every
network step of a video dispatch has a total deadline (httpx timeouts bound only
the gaps between bytes): the POST `VIDEO_POST_DEADLINE`, each status GET
`VIDEO_POLL_REQUEST_DEADLINE`, each download attempt `VIDEO_DOWNLOAD_TIMEOUT`,
all attempts together `VIDEO_DOWNLOAD_BUDGET`. Their sum is
`VIDEO_CALL_DEADLINE`, and the pipeline's outer `TIMEOUT_VIDEO` is derived from
it. Polls tolerate up to `VIDEO_POLL_MAX_TRANSIENT` consecutive transient
failures, a malformed status answer included; an empty 200 body is a failed
download attempt. Only an answer in `VideoDispatchRejected.KNOWN_REFUSAL_STATUS`
(400, 401, 402, 403, 404, 413, 422, 429) counts as a refusal that created no
job; such a refusal, a prompt refused before sending and a provider-reported
generation failure are the only dispatch outcomes that may go on to the failure
advisor. Every other outcome raises `VideoJobUnretrieved`: with the provider job
id once the acceptance was seen, and with no id (stage `dispatch`) when the POST
got no usable answer, whether a transport error, the deadline, a dropped or
malformed answer, an answer without an id or an ambiguous status such as 408 or
5xx. The pipeline records it in
`verification_stats["unretrieved_accepted_jobs"]` (id or `null`, `accepted`
`yes` or `unknown`, stage, raw reason), adds `accepted_clip_unretrieved:<scene>`
or `dispatch_outcome_unknown:<scene>` to `partial_reasons`, keeps the charge and
starts no further paid clip for that scene: neither the advisor nor the next
candidate, nor a later continuity or identity-panel regeneration, which is
skipped (`regeneration_skipped_unretrieved`) while the scene keeps its current
clip. When no scene has a clip, the final error lists these dispatches.

**Liveness while waiting.** The worker refreshes its job lease only from the
progress callback, and `LEASE_TTL_SEC` (900 s) is shorter than one video wait.
`Pipeline.run` sets `client.poll_heartbeat`; the client calls it at most every
`POLL_HEARTBEAT_INTERVAL` (60 s) while polling and before each download attempt,
and assembly emits progress before each clip it re-encodes.

**Refunds only for proven non-generation.** `BudgetLedger.charge` returns a
receipt and `refund(receipt)` credits that exact charge once: only when the
provider refused the request before starting a job (HTTP 400/422 at the POST,
`VideoDispatchRejected` with the provider's message) or when the prompt fit
refused before anything was sent. 429, 5xx, timeouts and every accepted-job
failure stay charged. A refused charge never becomes an uncharged dispatch.

**Prompts fit the model and keep their bindings.** `VIDEO_MODEL_PROMPT_LIMIT` in
`api_client.py` is the only limit table (Kling 2500; `minimax/hailuo-3` 6800,
under its 7000-character ceiling). An LLM-condensed candidate is accepted only
if every line naming an `@ImageN` token survives verbatim; a candidate that
swaps two characters' descriptions or keeps bare markers falls back to the
mechanical fit. Continuity regeneration is condensed like the other paths.
Right before the POST an over-long prompt keeps every `@ImageN` line and the
spoken-dialogue block; the lessons and fix notes after the dialogue are cut
first when the whole tail does not fit. When the reference lines alone cannot
fit, nothing is sent (`VideoPromptFitError`). Hailuo's "text too long" refusal
counts as a prompt-limit refusal.

**Recovery paths that can succeed.** `minimax/hailuo-3` is in the advisor's model
list. Without capability data for it, `_clamp_duration` applies its documented
5 s floor. An advisor model switch reconciles the resolution for the new model
without changing the job's settings (`_resolution_for_model`).

**Honest results.** A multidimensional score with a missing, non-numeric or
out-of-range (outside 0–10) dimension is `indeterminate`. Missing scenes add
`missing_scenes` to `partial_reasons`. Clips that disagree on size or audio
layout are re-encoded to one shape before the stream-copy concat, and the result
is checked again: a failed re-encode keeps the paid original and adds
`concat_layout_unverified`. The video stream of the final file is fully decoded
(`final_decode_failed` otherwise). The music mix ends at the picture even when a
clip's own audio is shorter. Failing to save prompt lessons no longer turns a
delivered video into ERROR. A stored job whose phase or status this version does
not know (or that lacks one) reads as terminal ERROR with the original values in
`verification_stats["unsupported_stored_state"]`; the worker neither claims nor
requeues it and never rewrites it.

### Known, not fixed (3.3.1)

- `LEASE_TTL_SEC` is a hardcoded constant shorter than `TIMEOUT_VIDEO`; the
  heartbeat covers the wait from the other side.
- A scene whose dispatch outcome is unknown is not retried, even when nothing
  may have been sent (a connection error is not taken as proof of that).
- Budget is charged at the logical unit, not the physical POST; `api_client` can
  issue up to three POSTs per charge, and condense/simplify/advisor calls do not
  charge at all.
- The LLM condenser does not have to keep the dialogue verbatim; the mechanical
  fit does.
- MiniMax has not been run live from this skill.
- `handle_jobs` lists only directory-shaped job records, so a legacy flat
  `jobs/<id>.json` stays reachable through `status` but is absent from the
  listing.
