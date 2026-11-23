"""Core generation pipeline with VLM verification, video analysis, scene continuity,
progressive prompt learning, best-of-N selection, multi-dimensional scoring,
cross-scene identity check, adaptive simplification, and parallel generation."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from .ffmpeg_bootstrap import ensure_ffmpeg
from .api_client import (
    DEFAULT_MAX_TOKENS,
    VIDEO_CALL_DEADLINE,
    VIDEO_MODEL_PROMPT_LIMIT,
    OpenRouterClient,
    VideoDispatchRejected,
    VideoJobUnretrieved,
    VideoPromptFitError,
    fit_prompt_to_limit,
    run_with_timeout,
    split_protected_dialogue,
)
from .budget import BudgetLedger, derive_limit_usd, estimate_job_usd
from .judges import (
    ATTRIBUTE_QUESTION_PROMPT,
    build_attribute_checks,
    combine_order_swap,
    expected_attribute_ids,
    identity_similarity_proxy,
    model_family,
    panel_verdict,
    render_attribute_lines,
    select_judges,
)
from .modes import mode_config
from .models import (
    _fields_of,
    Character,
    GenerationSettings,
    Job,
    JobPhase,
    JobProgress,
    JobStatus,
    Location,
    MusicCue,
    Scene,
    Storyboard,
    VerificationResult,
)
from .prompts import (
    ADAPTIVE_SIMPLIFY_SCENE_PROMPT,
    CROSS_SCENE_IDENTITY_CHECK_PROMPT,
    IMAGE_CHARACTER_SHEET_PROMPT,
    IMAGE_KEYFRAME_PROMPT,
    IMAGE_KEYFRAME_SEQUENTIAL_PROMPT,
    IMAGE_LOCATION_PROMPT,
    MUSIC_PROMPT_TEMPLATE,
    SCENARIO_SYSTEM,
    SCENARIO_USER_TEMPLATE,
    SCENE_TRANSITION_TEMPLATE,
    VLM_COMPARE_CHARACTER_SHEETS_PROMPT,
    VLM_VERIFY_VIDEO_MULTIDIM_PROMPT,
    build_reference_block,
    build_video_prompt,
)

logger = logging.getLogger("anime_studio.pipeline")


def _describe_exc(exc: BaseException) -> str:
    """Exception TYPE plus message.

    Never interpolate a bare exception into a user-visible warning: many httpx
    transport/timeout exceptions have an EMPTY str(), which previously produced
    warnings like "Video scene 3 failed: " — no cause, no type, nothing to act on.
    """
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else f"{type(exc).__name__}: (no message)"


def _normalize_vlm_image_result(data: dict) -> dict:
    """Single authority for image-verify results: score is canonical, passed is derived.

    The model can return a missing score, a non-numeric score, an out-of-range score,
    or a self-contradiction like {"passed": true, "score": 3}. Deriving passed from a
    coerced score means exactly one rule decides acceptance, retries, best-of
    selection, warnings and stats.
    """
    raw_passed = bool(data.get("passed", False))
    try:
        score = int(round(float(data.get("score"))))
    except (TypeError, ValueError):
        score = 7 if raw_passed else 3
    score = max(0, min(10, score))
    return {
        "passed": score >= 7,
        "score": score,
        "issues": data.get("issues") or [],
        "suggestion": data.get("suggestion") or "",
        "vlm_error": bool(data.get("vlm_error")),
    }

TIMEOUT_SCENARIO = 240
TIMEOUT_IMAGE = 400
TIMEOUT_MUSIC = 200
# api_client.VIDEO_CALL_DEADLINE is the AUTHORITATIVE per-clip bound: the
# client's POST, poll and download deadlines add up to it, and the client raises
# by itself (VideoJobUnretrieved, with the provider job id) when an accepted job
# runs out of time. This outer run_with_timeout is only a safety net for hangs,
# so it is DERIVED from that sum and stays above it: the outer cut must never be
# what ends an accepted job, because it would lose the job id and invite a
# second paid clip. Earlier fixed values (660, then 1320) were each overtaken by
# a longer inner wait.
TIMEOUT_VIDEO = VIDEO_CALL_DEADLINE + 60
TIMEOUT_VERIFY = 45
TIMEOUT_VIDEO_VERIFY = 180

MAX_VERIFY_RETRIES = 2
MAX_VIDEO_VERIFY_RETRIES = 2  # Raised from 1: video is the most expensive asset
IMAGE_GENERATION_RETRIES = 1

# Cap for video input_references, mirroring the keyframe reference cap (4).
# Character sheets are assembled FIRST, so this cap drops the location plate /
# continuity frame before an identity anchor — and never silently (see
# _assemble_scene_references, which records every dropped item in "missing").
# Seedance's own API documentation states image_urls accepts 1-9 reference
# images (docs.byteplus.com, Seedance 2.0 API reference). This was 4, which
# silently dropped the continuity frame as soon as a scene had 3 characters —
# characters are ordered first, so the cap ate exactly the cross-scene anchor.
# A model that accepts fewer answers 4xx. Only the IMAGE path trims references
# and retries the same model (_is_reference_parameter_rejection); a refused
# video dispatch goes to the failure advisor like any other error.
VIDEO_MAX_INPUT_REFERENCES = 9

# Multi-dimensional scoring: weighted average threshold
MULTIDIM_PASS_THRESHOLD = 6.5
MULTIDIM_WEIGHTS = {"identity": 0.30, "motion": 0.20, "style": 0.15, "artifacts": 0.25, "composition": 0.10}

_STDERR_CAP_BYTES = 65536  # 64 KB

_LESSONS_FILENAME = "prompt_lessons.json"

class Pipeline:
    """Full anime pipeline: VLM verification, progressive learning,
    best-of-2 selection, multi-dim scoring, identity checks, parallel gen."""

    def __init__(
        self,
        client: OpenRouterClient,
        state_dir: Path,
        on_progress: Optional[Callable[[Job], None]] = None,
        shutdown_event: Optional[threading.Event] = None,
        lessons_dir: Optional[Path] = None,
        ffmpeg_cache_dir: Optional[Path] = None,
    ):
        self.client = client
        self.state_dir = state_dir
        self.on_progress = on_progress
        self.shutdown_event = shutdown_event
        self.lessons_dir = lessons_dir or state_dir
        self.ffmpeg_cache_dir = ffmpeg_cache_dir or state_dir
        self._active_procs: list[subprocess.Popen] = []
        self._procs_lock = threading.Lock()
        self._learned_lessons: list[str] = []
        self._load_lessons()

        self._ffmpeg_path: str = "ffmpeg"
        # Live provider capability snapshots, fetched ONCE at job start (async boundary).
        # Downstream reads are plain synchronous dict lookups — no site does I/O.
        self._image_caps: dict = {}
        self._video_caps: dict = {}
        # Location lookup for the scene-reference seam; refreshed from the
        # storyboard by _generate_videos and _regenerate_single_scene.
        self._locations_by_name: dict = {}
        # Normalized score of the most recently verified keyframe, so the keyframe loop
        # can refuse to feed a REJECTED frame forward as a reference image.
        self._last_keyframe_score: int = -1
        self._ffprobe_path: str = "ffprobe"
        # Budget rail. Constructed in run() before the first paid call; this is
        # the ONLY spend protection a job has (the skill calls the provider
        # directly, so core accounting sees its usage as unknown/unmetered).
        self.ledger = None

    # ─── Progressive Learning ───

    def _load_lessons(self):
        """Load accumulated prompt lessons from previous jobs."""
        lessons_path = self.lessons_dir / _LESSONS_FILENAME
        if lessons_path.exists():
            try:
                data = json.loads(lessons_path.read_text())
                self._learned_lessons = data.get("image_lessons", [])[-10:] + data.get("video_lessons", [])[-10:]
            except Exception:
                pass

    def _persist_lessons(self, image_lessons: list[str], video_lessons: list[str]):
        """Save lessons to disk for next job."""
        lessons_path = self.lessons_dir / _LESSONS_FILENAME
        existing = {}
        if lessons_path.exists():
            try:
                existing = json.loads(lessons_path.read_text())
            except Exception:
                pass
        existing_img = existing.get("image_lessons", [])
        existing_vid = existing.get("video_lessons", [])
        all_img = list(dict.fromkeys(existing_img + image_lessons))[-20:]
        all_vid = list(dict.fromkeys(existing_vid + video_lessons))[-20:]
        lessons_path.write_text(json.dumps({
            "image_lessons": all_img,
            "video_lessons": all_vid,
        }, ensure_ascii=False, indent=2))

    def _get_lessons_text(self) -> str:
        """Format accumulated lessons for injection into prompts."""
        if not self._learned_lessons:
            return "No lessons yet — this is a fresh generation."
        return "\n".join(f"- {lesson}" for lesson in self._learned_lessons[-8:])

    def _add_lesson(self, lesson: str, category: str = "video"):
        """Add a lesson from a VLM rejection to the accumulated knowledge."""
        if lesson and lesson not in self._learned_lessons:
            self._learned_lessons.append(lesson)

    # ─── Utility ───

    def _emit(self, job: Job):
        if self.on_progress:
            self.on_progress(job)

    def _warn(self, job: Job, msg: str):
        job.progress.warnings.append(msg)
        logger.warning(msg)

    def _record_indeterminate_scene(self, job: Job, scene_index: int):
        """An INDETERMINATE verification is not a pass: the scene ships UNVERIFIED
        and the job record says so (de-duplicated index + event counter)."""
        if scene_index not in job.progress.unverified_scenes:
            job.progress.unverified_scenes.append(scene_index)
        stats = job.progress.verification_stats
        stats["video_indeterminate"] = stats.get("video_indeterminate", 0) + 1

    def _refuse_paid_dispatch(self, job: Job, kind: str, count: int) -> None:
        """Record the pre-dispatch hard stop ONCE (`ledger.stopped` guards
        duplicates). Factored out so `_charge` and `_admit` share one refusal —
        two copies of this wording would drift apart. Never raises."""
        if self.ledger.stopped:
            return
        self.ledger.hard_stop(
            f"estimated spend ${self.ledger.spent_usd:.2f} + next {kind} x{count} "
            f"would reach limit ${self.ledger.limit_usd:.2f} at pre_dispatch:{kind}"
        )
        job.progress.partial_reasons.append(f"budget_hard_stop:pre_dispatch:{kind}")
        job.progress.budget = self.ledger.to_dict()
        self._warn(
            job,
            f"BUDGET ADMISSION REFUSED at pre_dispatch:{kind}: estimated spend "
            f"${self.ledger.spent_usd:.2f} plus the next {kind} would reach the "
            f"limit ${self.ledger.limit_usd:.2f}. The call was NOT dispatched; "
            f"already-generated assets are preserved.",
        )
        self._emit(job)

    def _charge(self, job: Job, kind: str, count: int = 1, note: str = ""):
        """Admit-then-charge the in-job estimated-spend ledger and refresh the
        persisted snapshot. A missing ledger (direct method calls in offline
        verification) charges nothing and admits.

        CRITICAL review finding (fixed): `_admit` was the real pre-dispatch
        gate but only 3 of ~13 paid sites called it, so a paid call still went
        out after the limit was reached. Admission now lives HERE — the one
        seam every paid dispatch already passes through — instead of as a
        reminder at every call site. On refusal nothing is charged, the hard
        stop is recorded once, and a falsy value is returned; callers skip the
        dispatch. Never raises: a raise would unwind past the assembly phase
        in run() and destroy already-generated clips.

        Returns a truthy RECEIPT on success (falsy on refusal), so every
        existing `if not self._charge(...)` caller is unchanged while a video
        dispatch can refund its exact charge when the provider proves it never
        generated anything.
        """
        if self.ledger is None:
            return {"id": 0, "kind": kind, "count": count, "usd": 0.0}
        if self.ledger.would_exceed(kind, count):
            self._refuse_paid_dispatch(job, kind, count)
            return None
        receipt = self.ledger.charge(kind, count, note=note)
        # Refreshed after EVERY charge batch so a crash leaves the real spend
        # on disk (job.progress is persisted by the on_progress callback).
        job.progress.budget = self.ledger.to_dict()
        return receipt

    def _refund(self, job: Job, receipt, note: str = "") -> bool:
        """Credit back one proven-ungenerated dispatch (400/422 validation
        refusal). Idempotent by receipt id; never raises."""
        if self.ledger is None or not receipt:
            return False
        try:
            if self.ledger.refund(receipt, note=note):
                job.progress.budget = self.ledger.to_dict()
                return True
        except Exception:
            # `Pipeline` has no `self.log`; using it here turned a defensive
            # refund-failure log into an AttributeError that aborted the whole
            # job and masked the provider rejection underneath it.
            logger.warning("budget refund failed for %s", note, exc_info=True)
        return False

    def _refund_if_rejected(self, job: Job, receipt, exc: BaseException) -> bool:
        """Refund a video charge ONLY when the provider proved it generated
        nothing: a request-validation refusal (HTTP 400/422) answered before
        any inference. Reported from a live job: six such refusals accumulated
        $15.90 of phantom spend against a ~$28 rail, enough to hard-stop a job.

        Deliberately narrow. A timeout, a 5xx, a 429, or a poll failure may sit
        on top of real generation, so those are NEVER refunded — over-charging
        is a wrong number, under-charging is a broken safety rail.

        `receipt` must be the charge of THIS dispatch, reset to None before it:
        a receipt left over from an earlier, successful clip must never be
        handed here.
        """
        # A client-side prompt-fit refusal never reached the network at all, so
        # it is the STRONGEST refund case, not an exception to the rule: the
        # charge happens before the dispatch call, and `fit_prompt_to_limit`
        # raises inside it when the protected @ImageN lines cannot fit.
        if isinstance(exc, VideoPromptFitError):
            return self._refund(
                job, receipt, note="prompt fit refused before any request was sent"
            )
        if not isinstance(exc, VideoDispatchRejected) or not exc.refundable:
            return False
        return self._refund(
            job, receipt, note=f"provider rejected before generation (HTTP {exc.status_code})"
        )

    @staticmethod
    def _paid_outcome_unknown(exc: BaseException, model: str) -> Optional[VideoJobUnretrieved]:
        """The typed outcome when a paid video dispatch may have been accepted.

        VideoJobUnretrieved already carries it, with or without a job id. A
        TimeoutError reaching a dispatch site can only be the outer
        run_with_timeout cutting a dispatch in flight, so its provider side is
        unknown too. Anything else is an ordinary failure (None).
        """
        if isinstance(exc, VideoJobUnretrieved):
            return exc
        if isinstance(exc, TimeoutError):
            return VideoJobUnretrieved(None, model, "dispatch", f"{type(exc).__name__}: {exc}")
        return None

    def _record_unretrieved(
        self, job: Job, scene_index: int, attempt: str, exc: VideoJobUnretrieved,
    ) -> None:
        """Keep the evidence of a dispatch that was, or may have been, accepted.

        Its charge stays (the provider may bill it) and no fresh paid clip is
        started for it: the caller stops working on that scene. The provider
        job id is kept when one came back and stays None when none did; the
        raw reason is kept either way.
        """
        accepted = exc.job_id is not None
        stats = job.progress.verification_stats
        stats.setdefault("unretrieved_accepted_jobs", []).append({
            "scene": scene_index, "attempt": attempt, "job_id": exc.job_id,
            "accepted": "yes" if accepted else "unknown",
            "model": exc.model, "stage": exc.stage, "detail": str(exc.detail)[:500],
        })
        reason = (
            f"accepted_clip_unretrieved:{scene_index}" if accepted
            else f"dispatch_outcome_unknown:{scene_index}"
        )
        if reason not in job.progress.partial_reasons:
            job.progress.partial_reasons.append(reason)
        if accepted:
            what = (
                f"provider job {exc.job_id} ({exc.model}) was accepted but its clip was "
                f"not retrieved ({exc.stage}: {exc.detail})"
            )
        else:
            what = (
                f"the video dispatch to {exc.model} may have been accepted, but no job id "
                f"came back ({exc.detail})"
            )
        self._warn(
            job,
            f"Scene {scene_index}: {what}. It may still be billed; no new paid clip was "
            f"started for it.",
        )

    def _no_clips_error(self, job: Job) -> str:
        """The final error when no scene has a clip, with the paid-dispatch evidence."""
        message = "No video clips were generated successfully"
        records = job.progress.verification_stats.get("unretrieved_accepted_jobs") or []
        if records:
            parts = [
                f"scene {r.get('scene')}: job {r.get('job_id') or 'unknown (no id came back)'}"
                f" at {r.get('stage')}"
                for r in records[:8]
            ]
            message += (
                f"; {len(records)} dispatch(es) were accepted or may have been accepted and "
                f"may be billed, with no clip retrieved and none started again: "
                + "; ".join(parts)
            )
        return message

    # `_resolution_for_model` lives beside the assembly/recovery helpers below,
    # next to `_RESOLUTION_LADDER`, so the ladder has exactly one owner.

    def _admit(self, job: Job, kind: str, count: int = 1) -> bool:
        """Pre-dispatch budget admission — called BEFORE every provider dispatch.

        FINDING D (fixed): `_charge` already returned False once the limit was
        reached, but paid call sites ignored the return value and dispatched
        anyway, so the rail recorded overruns without preventing a single paid
        call. Since core accounting cannot see this skill's spend, this ledger
        is the ONLY protection — admission must refuse the dispatch itself.
        (`_charge` now runs this same admission via `_refuse_paid_dispatch`;
        `_admit` stays for loops that probe BEFORE committing spend.)

        Returns True when there is no ledger (legacy direct method calls) or
        the charge fits. On refusal the hard stop is recorded once and visibly
        (same fields `_budget_stop` uses; `ledger.stopped` guards duplicates)
        and False is returned. Never raises.
        """
        if self.ledger is None:
            return True
        if not self.ledger.would_exceed(kind, count):
            return True
        self._refuse_paid_dispatch(job, kind, count)
        return False

    def _budget_stop(self, job: Job, phase_note: str) -> bool:
        """True when the budget is exceeded. First detection records the hard
        stop LOUDLY; callers break out of their loop — they never raise, and a
        hard stop never destroys already-generated assets."""
        # `stopped` also counts: an admission refusal (_admit) marks the ledger
        # stopped before `exceeded` flips, and downstream loops must still stop.
        if self.ledger is None or not (self.ledger.exceeded or self.ledger.stopped):
            return False
        if not self.ledger.stopped:
            self.ledger.hard_stop(
                f"estimated spend ${self.ledger.spent_usd:.2f} reached limit "
                f"${self.ledger.limit_usd:.2f} at {phase_note}"
            )
            job.progress.partial_reasons.append(f"budget_hard_stop:{phase_note}")
            job.progress.budget = self.ledger.to_dict()
            self._warn(
                job,
                f"BUDGET HARD STOP at {phase_note}: estimated spend "
                f"${self.ledger.spent_usd:.2f} reached the limit "
                f"${self.ledger.limit_usd:.2f}. Remaining paid work is skipped; "
                f"already-generated assets are preserved and the job continues "
                f"to assembly with whatever clips exist.",
            )
            self._emit(job)
        return True

    def _check_shutdown(self):
        if self.shutdown_event and self.shutdown_event.is_set():
            raise RuntimeError("Extension unloading — pipeline cancelled")

    def _run_ffmpeg(
        self, cmd: list[str], timeout: int = 120, capture_stdout: bool = False
    ) -> subprocess.CompletedProcess:
        """Run ffmpeg/ffprobe as a tracked child process.

        Child processes inherit the server's process group so the host's
        panic cleanup (os._exit / process-group kill) reaps them automatically.
        Normal cleanup uses _active_procs tracking + kill_active_processes().

        Environment is scrubbed to only PATH so no unrelated secrets leak to
        child processes. Absolute ffmpeg/ffprobe paths from ensure_ffmpeg are
        used in cmd; PATH is kept for any helper tools the binary may need.
        """
        import os

        stdout_target = subprocess.PIPE if capture_stdout else subprocess.DEVNULL
        system_path = os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")
        scrubbed_env = {"PATH": system_path}
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_target,
            stderr=subprocess.PIPE,
            env=scrubbed_env,
        )

        with self._procs_lock:
            self._active_procs.append(proc)
        try:
            stdout_data, stderr_data = proc.communicate(timeout=timeout)
            if stderr_data and len(stderr_data) > _STDERR_CAP_BYTES:
                stderr_data = stderr_data[:_STDERR_CAP_BYTES]
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=proc.returncode,
                stdout=stdout_data.decode(errors="replace") if stdout_data else "",
                stderr=stderr_data.decode(errors="replace") if isinstance(stderr_data, bytes) else (stderr_data or ""),
            )
        except subprocess.TimeoutExpired:
            self._kill_proc(proc)
            proc.wait()
            raise
        finally:
            with self._procs_lock:
                if proc in self._active_procs:
                    self._active_procs.remove(proc)

    def _kill_proc(self, proc: subprocess.Popen):
        """Kill a subprocess on timeout/unload."""
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass

    def kill_active_processes(self):
        with self._procs_lock:
            for proc in self._active_procs:
                self._kill_proc(proc)
            self._active_procs.clear()

    # ─── Image Generation Router ───

    # Provider-DOCUMENTED minimum clip length, consulted ONLY when the live
    # capability snapshot has nothing for that model. A floor, never an
    # allowed-list (see _clamp_duration). minimax/hailuo-3: 5s minimum,
    # confirmed twice on 2026-08-04 (OpenRouter model page + provider FAQ).
    _MODEL_MIN_DURATION_SEC: dict[str, int] = {
        "minimax/hailuo-3": 5,
    }

    def _clamp_duration(self, desired_sec: int, model: str) -> int:
        """Return the nearest duration the provider actually accepts for this model.

        Reads the live capability snapshot fetched once at job start; NEVER performs
        I/O (this is called inside the scene loop from synchronous code).

        There is deliberately no hardcoded per-model table any more. The previous one
        had gone stale in the dangerous direction: it listed google/veo-3.1 as [5, 8]
        while the provider accepts [4, 6, 8], so a 5-second scene was clamped to an
        INVALID 5 and the request was rejected outright. When capabilities are unknown
        we clamp only to the global 4-15s range: a permissive fallback can truncate a
        scene, but it cannot invent a value the provider refuses.
        """
        allowed = ((self._video_caps.get(model) or {}).get("durations")) or []
        if not allowed:
            # Two DIFFERENT unknowns, and conflating them is what breaks a model
            # whose real floor is above the global one. "Catalog unreachable"
            # (empty snapshot) and "catalog fetched, this model absent from it"
            # both land here, so the only safe fallback is the global range —
            # PLUS a per-model documented MINIMUM where one is known. That map is
            # deliberately NOT the stale allowed-list this function used to
            # carry: a floor can only RAISE a value toward a documented-valid
            # one, it cannot invent a duration the provider refuses (the old
            # table clamped veo-3.1 to an invalid 5 and every request 400'd).
            # Raising the GLOBAL floor to 5 instead would break veo-3.1, whose
            # real set is [4, 6, 8].
            floor = max(4, self._MODEL_MIN_DURATION_SEC.get(model, 4))
            return min(15, max(floor, desired_sec))
        return min(allowed, key=lambda v: abs(v - desired_sec))

    def _effective_generate_audio(self, job: Job, model: Optional[str] = None) -> bool:
        """ONE authority for the generate_audio flag on EVERY video dispatch.

        `model` names the id ACTUALLY being dispatched, defaulting to the job's
        configured video model. It exists because the failure advisor can switch
        to a different model mid-scene: reusing the flag computed for the
        original model would send an unsupported `generate_audio=true` to the
        switched model, or needlessly mute one that does support speech. A seam
        that answered only for the configured model would have reproduced the
        very per-path divergence it was introduced to remove.

        The owner's dialogue setting is the intent; the live capability snapshot
        is the veto. Before this seam the flag was recomputed independently in
        the main scene loop and again in continuity regeneration, while the
        preflight capability check only WARNED — so a model reporting
        generate_audio=false still received generate_audio=true on all four
        paid dispatch paths, and a gate later applied to one path would have
        silently missed the others.

        Reads the snapshot fetched once at job start; never performs I/O, so it
        is safe inside the synchronous scene loop. An unknown model (empty
        snapshot) keeps the owner's intent: a permissive fallback can only
        produce a flag the provider ignores, while a restrictive one would mute
        a model that does support native speech.
        """
        if not bool(job.settings.include_dialogue):
            return False
        target = str(model or job.settings.video_model)
        caps = self._video_caps.get(target) or {}
        if caps.get("generate_audio") is False:
            # Preflight already warned once; record the effective decision so
            # the job states why its clips are mute instead of leaving the
            # owner to infer a generation failure. A LIST, because one job can
            # dispatch more than one model (advisor switch) and a scalar would
            # have quietly overwritten the first suppression with the second.
            suppressed = job.progress.verification_stats.setdefault(
                "native_audio_suppressed", []
            )
            if target not in suppressed:
                suppressed.append(target)
                suppressed.sort()
            return False
        return True

    # Mapping from short UI image model names to OpenRouter model IDs.
    _IMAGE_MODEL_MAP: dict[str, str] = {
        # Must match what SKILL.md's model table and api_client.generate_image
        # name as the default. This entry used to remap the DOCUMENTED default
        # onto openai/gpt-5.4-image-2 — the previous generation — so an owner
        # picking the default silently got the older model and nothing warned.
        "gpt-image-2":        "openai/gpt-image-2",
        "gpt-5.4-image-2":    "openai/gpt-5.4-image-2",
        "gpt-5-image":        "openai/gpt-5-image",
        "gpt-5-image-mini":   "openai/gpt-5-image-mini",
        "gemini-3-pro-image": "google/gemini-3-pro-image-preview",
        "flux.2-pro":         "black-forest-labs/flux.2-pro",
        "flux.2-max":         "black-forest-labs/flux.2-max",
        "seedream-4.5":       "bytedance-seed/seedream-4.5",
        "grok-imagine":       "x-ai/grok-imagine-image-quality",
    }

    _NANOBANANA_ID = "google/gemini-3.1-flash-image-preview"

    def _resolved_image_model_id(self, requested: str) -> str:
        """Concrete OpenRouter id for a UI image-model name.

        `nanobanana` is a SHORT ALIAS and is deliberately NOT a key in
        `_IMAGE_MODEL_MAP`, so a plain map lookup returned it unchanged and
        `model_family()` then read its family as "nanobanana" instead of
        "google". `_judge_identity_panel` uses that family to exclude the
        GENERATORS' families, so a Google-generated image could be judged by the
        Google judge and the self-preference mitigation was silently defeated.
        Both generation and family exclusion now resolve ids here, so the alias
        can never be handled in one place and missed in the other.
        """
        name = str(requested or "").strip()
        if name == "nanobanana":
            return self._NANOBANANA_ID
        return self._IMAGE_MODEL_MAP.get(name, name)

    @staticmethod
    def _is_reference_parameter_rejection(exc: BaseException) -> bool:
        """True when a 4xx complains about the request PARAMETERS, not the model itself.

        A parameter-shaped rejection (too many references, unsupported field) must be
        answered by trimming the request and retrying the SAME model — never by silently
        dropping to a weaker model, which is the exact defect this ladder removes.
        """
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
        if not isinstance(status, int) or not (400 <= status < 500):
            return False
        try:
            body = (resp.text or "").lower()
        except Exception:
            body = ""
        return any(tok in body for tok in ("input_references", "reference", "parameter", "unsupported"))

    def _image_params_for(self, openrouter_id: str) -> set:
        return (self._image_caps.get(openrouter_id) or {}).get("params") or set()

    def _count_image_fallback(self, job: Job) -> None:
        stats = job.progress.verification_stats
        stats["image_model_fallbacks"] = stats.get("image_model_fallbacks", 0) + 1

    async def _generate_image(
        self, job: Job, prompt: str, filename: str, aspect_ratio: str = "16:9",
        reference_images: Optional[list] = None,
    ) -> str:
        """Generate one image through a LOUD fallback ladder.

        Every degradation reaches job.progress.warnings (not just the log), because a
        silent downgrade to a flash-tier model was indistinguishable from success and
        is a prime suspect for "the output is low quality".

        Rungs: requested model via /images (with reference images) -> same model with
        references trimmed, if the rejection was parameter-shaped -> nanobanana via
        /images -> the legacy /chat/completions path, which cannot carry references at
        all and is therefore the last resort. The legacy rung is kept LIVE rather than
        as dead compatibility code so that a defect in the new endpoint cannot take
        down the primary and the safety net together.
        """
        requested = job.settings.image_model
        requested_id = self._resolved_image_model_id(requested)
        refs = [p for p in (reference_images or []) if p]
        last_error: Optional[BaseException] = None

        # Rung 1 — requested model on the canonical image endpoint, with references.
        for attempt in range(IMAGE_GENERATION_RETRIES + 1):
            try:
                return await self.client.generate_image_unified(
                    prompt=prompt, filename=filename, aspect_ratio=aspect_ratio,
                    model=requested_id, reference_images=refs,
                    supported_params=self._image_params_for(requested_id),
                )
            except Exception as exc:
                last_error = exc
                # Rung 1a — parameter-shaped rejection: trim references, same model.
                if refs and self._is_reference_parameter_rejection(exc):
                    self._warn(
                        job,
                        f"{requested_id} rejected the reference images for {filename} "
                        f"({_describe_exc(exc)}); retrying the SAME model with 1 reference",
                    )
                    try:
                        return await self.client.generate_image_unified(
                            prompt=prompt, filename=filename, aspect_ratio=aspect_ratio,
                            model=requested_id, reference_images=refs[:1],
                            supported_params=self._image_params_for(requested_id),
                        )
                    except Exception as exc2:
                        last_error = exc2
                    break
                if attempt >= IMAGE_GENERATION_RETRIES:
                    break
                logger.warning(
                    f"Image generation retry {attempt + 1} for {filename} via {requested_id}: "
                    f"{_describe_exc(exc)}"
                )
                await asyncio.sleep(2.0 * (attempt + 1))

        # Rung 2 — nanobanana (flash tier) on the canonical endpoint, references kept.
        if requested_id != self._NANOBANANA_ID:
            self._warn(
                job,
                f"Image model {requested} unavailable for {filename} "
                f"({_describe_exc(last_error) if last_error else 'unknown error'}); "
                f"falling back to nanobanana (flash tier — lower quality)",
            )
            self._count_image_fallback(job)
            try:
                return await self.client.generate_image_unified(
                    prompt=prompt, filename=filename, aspect_ratio=aspect_ratio,
                    model=self._NANOBANANA_ID, reference_images=refs,
                    supported_params=self._image_params_for(self._NANOBANANA_ID),
                )
            except Exception as exc:
                last_error = exc

        # Rung 3 — legacy /chat/completions. Cannot carry reference images.
        self._warn(
            job,
            f"Falling back to the legacy chat/completions image path for {filename} "
            f"({_describe_exc(last_error) if last_error else 'unknown error'}); "
            f"reference-image conditioning is LOST for this asset",
        )
        self._count_image_fallback(job)
        for legacy in ("requested", "nanobanana"):
            try:
                if legacy == "requested" and requested_id != self._NANOBANANA_ID:
                    return await self.client.generate_image(
                        prompt=prompt, filename=filename, aspect_ratio=aspect_ratio,
                        model=requested_id,
                    )
                if legacy == "nanobanana":
                    return await self.client.generate_image_nanobanana(
                        prompt=prompt, filename=filename, aspect_ratio=aspect_ratio,
                    )
            except Exception as exc:
                last_error = exc

        raise last_error or RuntimeError(f"Image generation failed for {filename}")

    # ─── Character Identity Block Builder ───

    def _build_characters_identity_block(self, storyboard: Storyboard, scene_chars: list[str] = None) -> str:
        chars = storyboard.characters
        if scene_chars:
            filtered = [c for c in chars if c.name in scene_chars]
            if filtered:
                chars = filtered
        lines = []
        for char in chars:
            lines.append(f"- {char.name}: {char.visual_traits}")
        return "\n".join(lines) if lines else "No specific character references."

    # ─── Scene Reference Assembly (single ordered seam) ─────────────

    def _assemble_scene_references(self, scene, scene_chars, prev_frame_url, max_refs):
        """SINGLE source of truth for prompt labels and input_references order.

        ONE ordered list — (1) character sheets, (2) location plate, (3) the
        previous approved frame — feeds BOTH the @ImageN labels and the
        input_references payload. Labels are derived AFTER truncation from the
        same surviving list, so the number named in the prompt can never
        disagree with submission order. Characters come first, so a max_refs
        cap drops the location/continuity frame before an identity anchor —
        and never silently: every absent or truncated item lands in "missing".
        """
        entries: list[dict] = []
        missing: list[str] = []

        for char in scene_chars or []:
            name = (getattr(char, "name", "") or "").strip() or "unnamed character"
            sheet = getattr(char, "sheet_url", None)
            if sheet and Path(sheet).exists():
                entries.append({"kind": "character", "path": sheet, "name": name})
            else:
                missing.append(f"character sheet for '{name}' unavailable")

        loc = self._locations_by_name.get(getattr(scene, "location", None))
        loc_art = getattr(loc, "art_url", None) if loc else None
        if loc_art and Path(loc_art).exists():
            entries.append({"kind": "location", "path": loc_art, "name": getattr(loc, "name", "")})

        if prev_frame_url:
            if Path(prev_frame_url).exists():
                entries.append({"kind": "continuity_frame", "path": prev_frame_url, "name": ""})
            else:
                missing.append("continuity frame from the previous shot missing on disk")

        cap = max(0, int(max_refs))
        kept, dropped = entries[:cap], entries[cap:]
        for e in dropped:
            what = e["kind"] + (f" '{e['name']}'" if e["name"] else "")
            missing.append(f"{what} dropped by the max_refs cap ({cap})")

        labels: list[str] = []
        for i, e in enumerate(kept, start=1):
            if e["kind"] == "character":
                labels.append(
                    f"@Image{i} — the CHARACTER SHEET of {e['name']}: match this "
                    f"character's face, hair, eye colour, outfit and accessories EXACTLY."
                )
            elif e["kind"] == "location":
                labels.append(f"@Image{i} — the LOCATION plate: environment design only.")
            else:
                labels.append(
                    f"@Image{i} — the CONTINUITY FRAME from the previous shot: keep "
                    f"the same character appearance and art style."
                )

        references = [self.client.make_input_reference(e["path"]) for e in kept]
        # The previous approved frame is ALSO a hard anchor (frame_images),
        # exactly as before this seam existed — independent of whether its soft
        # input_references slot survived the cap; it is counted in the labels
        # only when it is in input_references.
        frame_images: list[dict] = []
        if prev_frame_url and Path(prev_frame_url).exists():
            frame_images.append(self.client.make_frame_image(prev_frame_url, "first_frame"))

        if not references:
            state = "none"
        elif missing:
            state = "partial"
        else:
            state = "complete"

        return {
            "references": references,
            "frame_images": frame_images,
            "labels": labels,
            "state": state,
            "missing": missing,
            "kinds": [e["kind"] for e in kept],
        }

    def _record_reference_state(self, job: Job, scene_index: int, assembled: dict) -> None:
        """Disclose a scene's reference state on the job record.

        Never skips generation and never raises: the shot still gets made, but
        an unanchored or partial reference set is stated, not silently shipped.
        """
        state = assembled.get("state")
        if state == "none":
            no_refs = job.progress.verification_stats.setdefault("scenes_without_references", [])
            if scene_index not in no_refs:
                no_refs.append(scene_index)
            self._warn(
                job,
                f"Scene {scene_index}: NO reference images reached the video request — "
                f"identity is unanchored for this shot.",
            )
        elif state == "partial":
            self._warn(
                job,
                f"Scene {scene_index}: reference set is PARTIAL — "
                f"{'; '.join(assembled.get('missing') or []) or 'unspecified gap'}",
            )

    def _video_audit_sink(self, job: Job, scene_index: int, attempt: str):
        """Per-request sink for api_client.generate_video's redacted audit dict.

        Appends to verification_stats["video_requests"] (a LIST — never
        overwritten), capped at 60 entries; anything beyond the cap increments
        verification_stats["video_requests_omitted"] instead of vanishing.
        """
        def _sink(audit: dict) -> None:
            stats = job.progress.verification_stats
            entries = stats.setdefault("video_requests", [])
            if len(entries) >= 60:
                stats["video_requests_omitted"] = int(stats.get("video_requests_omitted", 0) or 0) + 1
                return
            record = {"scene_index": int(scene_index), "attempt": str(attempt)}
            record.update(audit if isinstance(audit, dict) else {"malformed_audit": True})
            entries.append(record)
        return _sink

    # ─── VLM Verification with Retry (Images) ───

    async def _verify_and_retry(
        self, job: Job, image_path: str, original_prompt: str,
        filename: str, aspect_ratio: str, char_ref_desc: str = "None",
        reference_images: Optional[list] = None, charge_kind: str = "keyframe",
    ) -> str:
        """Verify a generated image via VLM, keeping the BEST candidate.

        The verifier is deliberately non-blocking — a keyframe is a required input to
        the video anchoring chain, so aborting the job on rejection would destroy an
        otherwise usable run. But it must not be MEANINGLESS either: previously this
        returned whatever the LAST retry produced, so a run where all candidates were
        rejected shipped the final attempt even when the first was better. Now the
        normalized score selects the best candidate, ties keep the earlier attempt
        (deterministic), and an all-rejected outcome is stated loudly with the score.
        """
        stats = job.progress.verification_stats
        current_prompt = original_prompt
        best_path = image_path
        best_score = -1
        last_issues: list = []

        for attempt in range(MAX_VERIFY_RETRIES + 1):
            if attempt > 0:
                retry_filename = f"{Path(filename).stem}_r{attempt}{Path(filename).suffix}"
                if not self._charge(job, charge_kind, note=f"retry {attempt} {filename}"):
                    # A refused retry keeps the best candidate so far — same
                    # exit as a failed retry generation, never a raise.
                    self._warn(job, f"Retry {attempt} for {filename} skipped: budget hard stop")
                    break
                try:
                    image_path = await run_with_timeout(
                        self._generate_image(
                            job, current_prompt, retry_filename, aspect_ratio,
                            reference_images=reference_images,
                        ),
                        timeout_sec=TIMEOUT_IMAGE,
                        description=f"Retry {attempt} for {filename}",
                    )
                except Exception as e:
                    self._warn(job, f"Retry {attempt} generation failed for {filename}: {_describe_exc(e)}")
                    break

            if not self._charge(job, "vlm_image_verify", note=filename):
                # Same exit as a verify transport failure: the image ships
                # UNVERIFIED rather than the job aborting.
                self._warn(job, f"VLM verify for {filename} skipped: budget hard stop; image ships UNVERIFIED")
                stats["skipped"] = stats.get("skipped", 0) + 1
                self._last_keyframe_score = -1
                return image_path
            try:
                raw = await run_with_timeout(
                    self.client.verify_image_vlm(image_path, original_prompt, char_ref_desc),
                    timeout_sec=TIMEOUT_VERIFY,
                    description=f"VLM verify {filename}",
                )
            except Exception as e:
                logger.warning(f"VLM verification skipped for {filename}: {_describe_exc(e)}")
                stats["skipped"] = stats.get("skipped", 0) + 1
                self._last_keyframe_score = -1
                return image_path

            result = _normalize_vlm_image_result(raw)

            if result["vlm_error"]:
                stats["skipped"] = stats.get("skipped", 0) + 1
                self._last_keyframe_score = -1
                return image_path

            if result["score"] > best_score:
                best_score, best_path = result["score"], image_path
            last_issues = result["issues"] or last_issues

            if result["passed"]:
                stats["passed"] = stats.get("passed", 0) + 1
                self._last_keyframe_score = result["score"]
                return image_path

            logger.info(
                f"VLM rejected {filename} (attempt {attempt+1}, score {result['score']}/10): "
                f"{result['issues']}"
            )
            stats["retried"] = stats.get("retried", 0) + 1

            if result["suggestion"]:
                self._add_lesson(result["suggestion"], "image")
                current_prompt = f"{original_prompt}\n\nCRITICAL: {result['suggestion']}"

        stats["failed"] = stats.get("failed", 0) + 1
        stats["keyframe_rejected_used"] = stats.get("keyframe_rejected_used", 0) + 1
        self._last_keyframe_score = best_score
        self._warn(
            job,
            f"VLM rejected every candidate for {filename} ({MAX_VERIFY_RETRIES + 1} attempts); "
            f"using the best one (score {best_score}/10). Issues: "
            f"{'; '.join(str(i) for i in last_issues[:3]) or 'not reported'}",
        )
        return best_path

    # ─── Best-of-2 Character Sheet Selection ───

    async def _generate_best_of_2_character_sheet(
        self, job: Job, char: Character, index: int, style: str
    ) -> Optional[str]:
        """Generate 2 character sheets in parallel, VLM picks the best one."""
        prompt = IMAGE_CHARACTER_SHEET_PROMPT.format(
            name=char.name, visual_traits=char.visual_traits, style=style,
        )
        base_name = f"char_{index}_{char.name.lower().replace(' ', '_')}"

        if not self._charge(job, "character_sheet", count=2, note=f"best-of-2 candidates '{char.name}'"):
            # Same exit as both candidates failing: the caller already treats
            # None as "no sheet"; nothing was generated, nothing is lost.
            self._warn(job, f"Character sheet for '{char.name}' skipped: budget hard stop")
            return None
        tasks = [
            run_with_timeout(
                self._generate_image(job, prompt, f"{base_name}_a.png", "1:1"),
                timeout_sec=TIMEOUT_IMAGE,
                description=f"Character sheet '{char.name}' candidate A",
            ),
            run_with_timeout(
                self._generate_image(job, prompt, f"{base_name}_b.png", "1:1"),
                timeout_sec=TIMEOUT_IMAGE,
                description=f"Character sheet '{char.name}' candidate B",
            ),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        candidates = []
        for r in results:
            if not isinstance(r, Exception):
                candidates.append(r)

        if not candidates:
            self._warn(job, f"Both character sheet candidates failed for '{char.name}'")
            return None
        if len(candidates) == 1:
            return await self._verify_and_retry(
                job, candidates[0], prompt, f"{base_name}.png", "1:1",
                char_ref_desc=char.visual_traits, charge_kind="character_sheet",
            )

        compare_prompt = VLM_COMPARE_CHARACTER_SHEETS_PROMPT.format(
            name=char.name, visual_traits=char.visual_traits, style=style,
        )
        if not self._charge(job, "vlm_image_verify", note=f"compare sheets '{char.name}'"):
            # Same fallback as a failed comparison below: candidate A wins.
            self._warn(job, f"Sheet comparison for '{char.name}' skipped: budget hard stop; using first candidate")
            return candidates[0]
        try:
            comparison = await run_with_timeout(
                self.client.compare_images_vlm(candidates[0], candidates[1], compare_prompt),
                timeout_sec=TIMEOUT_VERIFY,
                description=f"Compare sheets for '{char.name}'",
            )
            winner_idx = comparison.get("winner", 1) - 1  # 1-based to 0-based
            winner_idx = max(0, min(1, winner_idx))
            logger.info(f"Best-of-2 for '{char.name}': winner={winner_idx+1}, reason={comparison.get('reason', '?')}")
        except Exception as e:
            logger.warning(f"Character comparison failed, using first: {e}")
            winner_idx = 0

        return candidates[winner_idx]

    # ─── Multi-Dimensional Video Verification ───────────────────────

    @staticmethod
    def _is_multidim_score(value) -> bool:
        """True for a real number on the 0-10 scale VLM_VERIFY_VIDEO_MULTIDIM_PROMPT asks for."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return math.isfinite(float(value)) and 0.0 <= float(value) <= 10.0

    async def _verify_video_multidim(
        self, job: Job, video_path: str, scene: Scene, storyboard: Storyboard,
    ) -> dict:
        """Verify video with multi-dimensional scoring (identity/motion/style/artifacts/composition).

        Returns a three-state result: {"verdict": "pass"|"fail"|"indeterminate", ...}.
        "indeterminate" means the check COULD NOT RUN (no frames, VLM error) —
        previously both of those returned passed=True, so "could not check" was
        indistinguishable from "checked and passed". `passed` is kept for
        backward compatibility but is DERIVED as (verdict == "pass").
        """
        chars_desc = self._build_characters_identity_block(storyboard, scene.characters)
        frame_paths = self._extract_video_frames(video_path, num_frames=5, prefix=f"s{scene.index}")

        if not frame_paths:
            cause = self._last_frame_extraction_error or (
                "no frames produced (ffmpeg returned nonzero or empty video)"
            )
            self._warn(
                job,
                f"Video scene {scene.index}: verification could not run — "
                f"frame extraction failed ({cause}); clip is UNVERIFIED, not passed",
            )
            return {
                "verdict": "indeterminate",
                "passed": False,
                "reason": "frame_extraction_failed",
                "scores": {},
                "issues": [],
                "suggestion": "",
            }

        verify_prompt = VLM_VERIFY_VIDEO_MULTIDIM_PROMPT.format(
            scene_description=scene.description,
            characters_description=chars_desc,
            style=storyboard.style,
            camera_direction=scene.camera_direction,
            learned_lessons=self._get_lessons_text(),
        )
        if not self._charge(job, "vlm_video_verify", note=f"scene {scene.index}"):
            # Budget refusal is another could-not-run — never a pass. Frames
            # are cleaned here because the finally below is not reached.
            for fp in frame_paths:
                try:
                    Path(fp).unlink(missing_ok=True)
                except Exception:
                    pass
            return {
                "verdict": "indeterminate",
                "passed": False,
                "reason": "budget_hard_stop",
                "scores": {},
                "issues": [],
                "suggestion": "",
            }

        try:
            result = await run_with_timeout(
                self.client.analyze_multi_image_vlm(frame_paths, verify_prompt),
                timeout_sec=TIMEOUT_VIDEO_VERIFY,
                description=f"Video multidim verify scene {scene.index}",
            )

            if result.get("vlm_error"):
                # The check did not run. Scoring defaults here would have
                # produced a weighted 7.0 = "pass" for a non-event.
                cause = str(result.get("reason") or "VLM request failed")
                self._warn(
                    job,
                    f"Video scene {scene.index}: verification could not run — "
                    f"{cause}; clip is UNVERIFIED, not passed",
                )
                return {
                    "verdict": "indeterminate",
                    "passed": False,
                    "reason": "vlm_error",
                    "scores": {},
                    "issues": [cause],
                    "suggestion": "",
                }

            # A dimension the model did not score is NOT a 7. The old
            # `result.get(k, 7)` default meant a valid-JSON reply with wrong or
            # missing keys scored a weighted 7.0 — above MULTIDIM_PASS_THRESHOLD
            # — and recorded verdict="pass" for a clip that was never scored:
            # the same "could not check renders as checked-and-passed" shape this
            # payload removed everywhere else (deleted verify_video_vlm, the
            # vlm_error marker above). Incomplete scoring is INDETERMINATE, so
            # the clip ships UNVERIFIED and says so. A number outside the
            # prompt's 0-10 scale is no score either: 85 on a percentage reading
            # would otherwise sail past the threshold.
            missing = [
                k for k in MULTIDIM_WEIGHTS
                if not self._is_multidim_score(result.get(k))
            ]
            if missing:
                self._warn(
                    job,
                    f"Video scene {scene.index}: verification returned no usable score for "
                    f"{', '.join(sorted(missing))}; clip is UNVERIFIED, not passed",
                )
                return {
                    "verdict": "indeterminate",
                    "passed": False,
                    "reason": "multidim_schema_incomplete",
                    "scores": {},
                    "issues": [f"unscored dimensions: {', '.join(sorted(missing))}"],
                    "suggestion": "",
                }

            scores = {k: float(result[k]) for k in MULTIDIM_WEIGHTS}
            weighted_avg = sum(scores[k] * MULTIDIM_WEIGHTS[k] for k in scores)
            result["verdict"] = "pass" if weighted_avg >= MULTIDIM_PASS_THRESHOLD else "fail"
            result["passed"] = result["verdict"] == "pass"
            result["weighted_score"] = round(weighted_avg, 2)
            result["scores"] = scores
            return result
        except Exception as e:
            logger.warning(
                f"Video multidim verification unavailable for scene {scene.index}: {_describe_exc(e)}"
            )
            return {
                "verdict": "indeterminate",
                "passed": False,
                "reason": _describe_exc(e),
                "scores": {},
                "issues": [],
                "suggestion": "",
                "vlm_error": True,
            }
        finally:
            for fp in frame_paths:
                try:
                    Path(fp).unlink(missing_ok=True)
                except Exception as cleanup_exc:
                    logger.warning(
                        f"Verify frame cleanup failed for {fp}: {_describe_exc(cleanup_exc)}"
                    )

    # ─── Identity Judge Panel (owner-chosen hard gate; see judges.py) ─

    async def _judge_identity_panel(
        self, job: Job, video_path: str, scene: Scene, storyboard: Storyboard,
    ) -> dict:
        """Two-family VLM judge panel over reference sheet vs clip frames.

        Mitigations are structural (judges.py): different families than the
        generators, order-swap per judge, atomic attribute questions. Any
        failure to run yields "indeterminate" — never a pass.
        """
        requested = int(mode_config(job.settings.quality_mode)["judges"])
        if requested <= 0:
            return {"verdict": "skipped", "judges": []}

        stats = job.progress.verification_stats
        panel_store = stats.setdefault("identity_panel", {})

        # A judge must never come from a generator's family (self-preference bias).
        exclude_families = {
            model_family(job.settings.video_model),
            model_family(self._resolved_image_model_id(job.settings.image_model)),
        }
        judges = select_judges(requested, exclude_families=exclude_families)
        if len(judges) < requested:
            self._warn(
                job,
                f"Identity panel scene {scene.index} DEGRADED: only {len(judges)} of "
                f"{requested} judges available after excluding generator families "
                f"{sorted(exclude_families)}; a degraded panel cannot verdict 'pass'",
            )

        scene_chars = [
            c for name in scene.characters for c in storyboard.characters if c.name == name
        ]
        # ALL available character sheets reach the judge: the attribute questions
        # cover every scene character, so passing only the first sheet made every
        # multi-character question unanswerable by construction.
        from .api_client import VLM_MAX_IMAGES
        sheets = [
            (c.name, c.sheet_url) for c in scene_chars
            if c.sheet_url and Path(c.sheet_url).exists()
        ]
        frames = self._extract_video_frames(video_path, num_frames=2, prefix=f"judge_{scene.index}")
        try:
            # Respect the VLM image cap: the frames keep their slots, sheets fill
            # the rest. A character whose sheet did not fit is judged UNANCHORED —
            # its ids stay in the expected set (coverage forces indeterminate) but
            # no question is asked without ground truth.
            sheet_budget = max(0, VLM_MAX_IMAGES - len(frames))
            judged_sheets = sheets[:sheet_budget]
            dropped_sheets = sheets[sheet_budget:]
            if dropped_sheets:
                self._warn(
                    job,
                    f"Identity panel scene {scene.index}: {len(dropped_sheets)} character "
                    f"sheet(s) did not fit the VLM image cap ({VLM_MAX_IMAGES}); "
                    f"{', '.join(n for n, _ in dropped_sheets)} judged UNANCHORED",
                )
            if not judged_sheets or len(frames) < 1:
                missing = "character sheet reference" if not judged_sheets else (
                    f"video frames ({self._last_frame_extraction_error or 'none extracted'})"
                )
                record = {
                    "verdict": "indeterminate",
                    "reason": f"missing input: {missing}",
                    "judges": judges,
                }
                panel_store[str(scene.index)] = record
                return record

            # ONE build: the questions asked, the anchored set, and the coverage
            # set checked afterwards all derive from the same checks list, or the
            # gate would demand answers to questions it never asked.
            anchored_names = {name for name, _ in judged_sheets}
            checks = build_attribute_checks(scene_chars, anchored_names=anchored_names)
            attribute_lines = "\n".join(
                f"{n+1}. {line}" for n, line in enumerate(render_attribute_lines(checks))
            )
            per_judge: list[dict] = []
            sheet_desc = {
                path: f"reference character sheet of {name}" for name, path in judged_sheets
            }
            forward = [path for _, path in judged_sheets] + frames
            for judge in judges:
                contribution: dict = {"judge": judge}
                # Order-swap means TWO provider calls per judge — charge both.
                if not self._charge(job, "vlm_judge", count=2, note=f"scene {scene.index} judge {judge.get('model', '?')} order-swap"):
                    # A panel that could not run is NEVER a pass — same shape
                    # as the missing-input branch above.
                    record = {
                        "verdict": "indeterminate",
                        "reason": "budget_hard_stop: judge calls not dispatched",
                        "judges": judges,
                        "per_judge": per_judge,
                    }
                    panel_store[str(scene.index)] = record
                    return record
                try:
                    passes = []
                    for order, label in ((forward, "forward"), (list(reversed(forward)), "reversed")):
                        labels = ", ".join(
                            f"image {n} = " + sheet_desc.get(p, "video frame")
                            for n, p in enumerate(order)
                        )
                        prompt = ATTRIBUTE_QUESTION_PROMPT.format(
                            attribute_lines=attribute_lines,
                            frame_labels=labels,
                            style=storyboard.style,
                        )
                        passes.append(await run_with_timeout(
                            self.client.analyze_multi_image_vlm(order, prompt, model=judge["model"]),
                            timeout_sec=TIMEOUT_VIDEO_VERIFY,
                            description=f"Identity judge {judge['model']} scene {scene.index} ({label})",
                        ))
                    contribution.update(combine_order_swap(passes[0], passes[1]))
                except Exception as e:
                    # A failed judge call is INDETERMINATE with its cause — never a pass.
                    contribution.update({"attributes": {}, "error": _describe_exc(e)})
                per_judge.append(contribution)

            record = panel_verdict(
                per_judge,
                requested=requested,
                expected_attributes=expected_attribute_ids(checks),
            )
            record["per_judge"] = per_judge
            panel_store[str(scene.index)] = record
            # Proxy is computed AFTER the verdict is final, so by construction it
            # cannot affect it. Diagnostic only; NOT DINOv2, NOT calibrated.
            proxy = identity_similarity_proxy(forward[0], frames[0])
            record["identity_similarity_proxy"] = proxy
            logger.info(
                f"Identity panel scene {scene.index}: verdict={record['verdict']} "
                f"({record['reason']}); similarity proxy={proxy}"
            )
            return record
        finally:
            for fp in frames:
                try:
                    Path(fp).unlink(missing_ok=True)
                except Exception as cleanup_exc:
                    logger.warning(
                        f"Judge frame cleanup failed for {fp}: {_describe_exc(cleanup_exc)}"
                    )

    # ─── Video Frame Extraction ─────────────────────────────────────

    def _extract_video_frames(self, video_path: str, num_frames: int = 5, prefix: str = "") -> list[str]:
        """Extract evenly-spaced frames from a video using tracked _run_ffmpeg.

        Args:
            prefix: unique prefix to avoid filename collisions between concurrent
                    callers (e.g. "s0" for scene 0, "xcheck_1" for cross-scene check).
        """
        output_dir = self.state_dir / "assets"
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        tag = f"{prefix}_" if prefix else ""
        # Reset per call: the caller reads this to name a real cause in its
        # warning when the returned list is empty (an empty list with only a
        # log line made "could not check" invisible to the job record).
        self._last_frame_extraction_error = ""
        try:
            probe = self._run_ffmpeg(
                [self._ffprobe_path, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", video_path],
                timeout=15, capture_stdout=True,
            )
            duration_str = probe.stdout.strip() if probe.stdout else ""
            duration = float(duration_str) if duration_str else 5.0

            interval = duration / (num_frames + 1)
            for i in range(num_frames):
                timestamp = interval * (i + 1)
                output_path = str(output_dir / f"_vframe_{tag}{i}.png")
                result = self._run_ffmpeg(
                    [self._ffmpeg_path, "-y", "-ss", f"{timestamp:.2f}", "-i", video_path,
                     "-vframes", "1", "-q:v", "2", output_path],
                    timeout=15,
                )
                if result.returncode == 0 and Path(output_path).exists():
                    frames.append(output_path)
        except Exception as e:
            self._last_frame_extraction_error = _describe_exc(e)
            logger.warning(f"Frame extraction failed: {self._last_frame_extraction_error}")
        return frames

    # ─── Last Frame Extraction ──────────────────────────────────────

    def _extract_last_frame(self, video_path: str, scene_index: int) -> Optional[str]:
        """Extract the last frame of a video clip for scene continuity."""
        output_path = str(self.state_dir / "assets" / f"lastframe_{scene_index}.png")
        try:
            probe = self._run_ffmpeg(
                [self._ffprobe_path, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", video_path],
                timeout=15, capture_stdout=True,
            )
            duration_str = probe.stdout.strip() if probe.stdout else ""
            duration = float(duration_str) if duration_str else 5.0
            timestamp = max(0, duration - 0.1)

            result = self._run_ffmpeg(
                [self._ffmpeg_path, "-y", "-ss", str(timestamp), "-i", video_path,
                 "-vframes", "1", "-q:v", "2", output_path],
                timeout=30,
            )
            if result.returncode == 0 and Path(output_path).exists():
                return output_path
        except Exception as e:
            logger.warning(f"Failed to extract last frame for scene {scene_index}: {e}")
        return None

    # ─── Adaptive Scene Simplification ──────────────────────────────

    async def _simplify_scene(self, scene: Scene, issues: list[str]) -> tuple[str, str, str]:
        """Ask LLM to simplify a scene that repeatedly fails video generation."""
        prompt = ADAPTIVE_SIMPLIFY_SCENE_PROMPT.format(
            scene_description=scene.description,
            camera_direction=scene.camera_direction,
            characters=", ".join(scene.characters),
            duration_sec=int(scene.duration_sec),
            issues_summary="\n".join(f"- {issue}" for issue in issues[-5:]),
        )
        try:
            response = await run_with_timeout(
                self.client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    model="anthropic/claude-sonnet-4.6",
                    max_toks=DEFAULT_MAX_TOKENS,
                    temperature=0.3,
                    json_mode=True,
                ),
                timeout_sec=60,
                description=f"Simplify scene {scene.index}",
            )
            data = self.client.parse_json_response(response)
            return (
                data.get("simplified_description", scene.description),
                data.get("simplified_camera", scene.camera_direction),
                data.get("negative_constraints", ""),
            )
        except Exception as e:
            logger.warning(f"Scene simplification failed: {e}")
            return scene.description, "static medium shot", ""

    # ─── Cross-Scene Identity Verification ──────────────────────────

    @staticmethod
    def _identity_continuity_note(critique: str = "") -> str:
        """Continuity instruction for a video prompt, optionally carrying a rejection."""
        note = (
            "CRITICAL: Character identity must match other scenes exactly. "
            "Pay extra attention to hair color, outfit details, and proportions."
        )
        if critique:
            note += (
                "\nA cross-scene identity reviewer REJECTED an earlier take of this "
                "scene for exactly this drift. Fix it and do not reintroduce it: "
                f"{critique}"
            )
        return note

    async def _cross_scene_identity_check(self, job: Job, storyboard: Storyboard) -> Optional[int]:
        """After all videos: extract 1 frame per scene, check identity consistency.
        Returns the worst_scene_index if drift is major, else None.
        """
        # (real scene index, frame path) pairs. The reviewer only ever sees the
        # frames we could actually extract, so its Nth answer means "the Nth frame
        # I was shown" — never "scene N". Keeping the mapping explicit is what lets
        # a verdict be translated back to a scene that really exists.
        compared = []
        for i, scene in enumerate(storyboard.scenes):
            if scene.video_url and Path(scene.video_url).exists():
                mid_frames = self._extract_video_frames(scene.video_url, num_frames=1, prefix=f"xcheck_{i}")
                if mid_frames:
                    compared.append((i, mid_frames[0]))

        # The provider call silently truncates to VLM_MAX_IMAGES. With more scenes than
        # that, the prompt below would advertise frames that were never sent and every
        # label after the cut would name the wrong scene. Sample evenly instead —
        # keeping the first and last scene, where drift shows most — and say so.
        from .api_client import VLM_MAX_IMAGES
        if len(compared) > VLM_MAX_IMAGES:
            step = (len(compared) - 1) / (VLM_MAX_IMAGES - 1)
            picks = sorted({int(round(k * step)) for k in range(VLM_MAX_IMAGES)})
            dropped = [si for n, (si, _) in enumerate(compared) if n not in picks]
            for n, (_, fp) in enumerate(compared):
                if n not in picks:
                    try:
                        Path(fp).unlink(missing_ok=True)
                    except Exception:
                        pass
            compared = [compared[n] for n in picks]
            self._warn(
                job,
                f"Cross-scene identity: only {len(compared)} frames fit one review "
                f"call, so scenes {dropped} were NOT compared for identity drift",
            )

        valid_frames = [fp for _, fp in compared]
        scene_of_frame = [si for si, _ in compared]
        # Every exit records WHY it returned, because `None` alone means four very
        # different things (clean / minor / not enough frames / reviewer failed) and
        # the caller must not read "no target scene" as "identity verified clean".
        if len(valid_frames) < 2:
            job.progress.verification_stats["cross_scene_last_verdict"] = "unavailable"
            return None

        if not self._charge(job, "vlm_cross_scene"):
            # Same could-not-run exit as the reviewer failing: scenes stay
            # UNCHECKED, never "verified clean". Frames are cleaned here
            # because the finally below is not reached.
            for fp in valid_frames:
                try:
                    Path(fp).unlink(missing_ok=True)
                except Exception:
                    pass
            job.progress.verification_stats["cross_scene_last_verdict"] = "unavailable"
            return None
        chars_desc = self._build_characters_identity_block(storyboard)
        prompt = CROSS_SCENE_IDENTITY_CHECK_PROMPT.format(
            characters_description=chars_desc,
            style=storyboard.style,
            frame_count=len(valid_frames),
            frame_labels=", ".join(
                f"frame {n} = scene {si}" for n, si in enumerate(scene_of_frame)
            ),
        )

        try:
            result = await run_with_timeout(
                self.client.analyze_multi_image_vlm(valid_frames, prompt),
                timeout_sec=TIMEOUT_VIDEO_VERIFY,
                description="Cross-scene identity check",
            )
            if result.get("vlm_error"):
                # The reviewer never answered. Reading its optimistic default
                # here recorded "none" — a CLEAN verdict for a check that did
                # not run, which is the exact class this function's other
                # exits were written to avoid.
                cause = str(result.get("reason") or "VLM request failed")
                job.progress.verification_stats["cross_scene_last_verdict"] = "unavailable"
                self._warn(
                    job,
                    f"Cross-scene identity check could not run ({cause}); scenes are "
                    f"UNCHECKED for identity drift, not verified clean",
                )
                return None

            severity = str(result.get("severity", "none")).strip().lower()
            drift = result.get("drift_description", "unknown drift")
            stats = job.progress.verification_stats
            # MINOR drift also returns a target now: minor drift is exactly how a
            # character's hair strand changed sides in a shipped job. The caller's
            # per-mode continuity_regen_max decides whether anything is regenerated.
            if severity in ("major", "minor"):
                raw = result.get("worst_scene_index")
                if isinstance(raw, bool):  # bool is an int subclass; not an index
                    raw = None
                self._warn(job, f"Cross-scene identity drift ({severity}): {drift}")
                stats["cross_scene_drift"] = drift
                stats["cross_scene_severity"] = severity
                if isinstance(raw, int) and 0 <= raw < len(scene_of_frame):
                    target = scene_of_frame[raw]
                else:
                    # Observed in job bb72b5fb: a 2-scene job came back naming a
                    # "third scene", so the old in-range check silently discarded a
                    # MAJOR verdict and shipped the drift. A reviewer that breaks its
                    # own index contract must not be able to cancel its own finding:
                    # fall back to the last compared scene and say so out loud.
                    target = scene_of_frame[-1]
                    self._warn(
                        job,
                        f"Cross-scene identity: reviewer returned unusable "
                        f"worst_scene_index={raw!r} for {len(scene_of_frame)} compared "
                        f"frame(s); falling back to scene {target}",
                    )
                    stats["cross_scene_index_fallback"] = True
                stats["cross_scene_target_scene"] = target
                stats["cross_scene_last_verdict"] = severity
                return target
            stats["cross_scene_last_verdict"] = "none"
            return None
        except Exception as e:
            logger.warning(f"Cross-scene identity check failed: {_describe_exc(e)}")
            job.progress.verification_stats["cross_scene_last_verdict"] = "error"
            self._warn(job, f"Cross-scene identity check unavailable: {_describe_exc(e)}")
            return None
        finally:
            for fp in valid_frames:
                try:
                    Path(fp).unlink(missing_ok=True)
                except Exception:
                    pass

    # ─── Main Pipeline ──────────────────────────────────────────────

    async def run(self, job: Job) -> Job:
        """Execute the full pipeline."""
        try:
            # Prove liveness DURING the longest silent operation. The worker
            # refreshes its job lease only from this progress callback, and a
            # single video wait can run 22 minutes against a 15-minute lease TTL,
            # so a LIVE worker's lease could expire mid-generation, another worker
            # generation could treat the job as abandoned and the whole paid job
            # could run twice. The client throttles these to
            # POLL_HEARTBEAT_INTERVAL and swallows their failures; the ONLY thing
            # this closure may do is emit progress for THIS job.
            self.client.poll_heartbeat = lambda: self._emit(job)

            job.progress.message = "Resolving operator-provided ffmpeg/ffprobe..."
            job.progress.progress_pct = 1.0
            self._emit(job)

            try:
                # Never downloads: PATH or FFMPEG_PATH/FFPROBE_PATH only.
                paths = ensure_ffmpeg(self.ffmpeg_cache_dir)
                self._ffmpeg_path = paths["ffmpeg"]
                self._ffprobe_path = paths["ffprobe"]
                job.progress.message = f"Using ffmpeg at {self._ffmpeg_path}"
                job.progress.progress_pct = 2.0
                self._emit(job)
            except Exception as exc:
                job.progress.phase = JobPhase.ERROR
                job.progress.status = JobStatus.ERROR
                job.progress.error = f"ffmpeg setup failed: {exc}"
                job.progress.message = job.progress.error
                return job

            # Provider capabilities are fetched exactly ONCE here, at an async boundary,
            # so every downstream site (including the synchronous duration clamp inside
            # the scene loop) reads a plain dict and never performs I/O.
            self._image_caps = await self.client.fetch_image_capabilities()
            self._video_caps = await self.client.fetch_video_capabilities()
            job.progress.verification_stats["capability_snapshot"] = {
                "image_models": len(self._image_caps),
                "video_models": len(self._video_caps),
            }
            if not self._image_caps:
                self._warn(job, "Image capability catalog unavailable; optional image parameters will be omitted")
            if not self._video_caps:
                self._warn(job, "Video capability catalog unavailable; using the global 4-15s duration range")
            # Model-presence preflight BEFORE any paid work. A video model absent from a
            # non-empty catalog has no fallback ladder, so the job would otherwise pay for
            # the storyboard and every asset and only then fail at the animation phase.
            if self._video_caps and job.settings.video_model not in self._video_caps:
                available = ", ".join(sorted(self._video_caps)[:8]) or "none reported"
                job.progress.phase = JobPhase.ERROR
                job.progress.status = JobStatus.ERROR
                job.progress.error = (
                    f"Video model '{job.settings.video_model}' is not in the provider's "
                    f"live catalog. Available: {available}. Nothing was generated."
                )
                job.progress.message = job.progress.error
                return job
            # An absent IMAGE model is only a warning: the fallback ladder recovers, and a
            # hard failure here would kill a usable job over a catalog naming difference.
            _img_id = self._resolved_image_model_id(job.settings.image_model)
            if self._image_caps and _img_id not in self._image_caps:
                self._warn(
                    job,
                    f"Image model '{job.settings.image_model}' ({_img_id}) is not in the live "
                    f"catalog; the fallback ladder will be used and quality WILL be lower",
                )

            _vc = self._video_caps.get(job.settings.video_model) or {}
            # Resolution is selectable (including 4K), so an unsupported choice must be
            # caught HERE — before the storyboard and every asset is paid for — instead
            # of surfacing as a rejection at the animation phase.
            _supported_res = _vc.get("resolutions") or []
            if _supported_res and job.settings.resolution not in _supported_res:
                # 2K sits between 4K and 1080p. It is here because
                # minimax/hailuo-3 reports supported_resolutions == ["2K"] and
                # NOTHING else: without this rung the reconciler found no
                # candidate and raised "supports none of the known
                # resolutions", making that model impossible to select at all.
                # It also makes the reverse case sane — Seedance has no 2K, so
                # a 2K request now degrades to 1080p instead of failing.
                _ladder = ["4K", "2K", "1080p", "720p", "480p"]
                _requested_rank = (
                    _ladder.index(job.settings.resolution)
                    if job.settings.resolution in _ladder else 0
                )
                _downgrade = next(
                    (r for r in _ladder[_requested_rank:] if r in _supported_res), None
                ) or next((r for r in _ladder if r in _supported_res), None)
                if _downgrade:
                    self._warn(
                        job,
                        f"Video model {job.settings.video_model} does not support "
                        f"{job.settings.resolution}; using {_downgrade} instead "
                        f"(supported: {', '.join(_supported_res)})",
                    )
                    job.settings.resolution = _downgrade
                else:
                    raise RuntimeError(
                        f"Video model {job.settings.video_model} supports none of the "
                        f"known resolutions {_ladder} (reports: {_supported_res}). "
                        f"Nothing was generated."
                    )
            if job.settings.include_dialogue and _vc and _vc.get("generate_audio") is False:
                self._warn(
                    job,
                    f"Video model {job.settings.video_model} reports generate_audio=false — "
                    f"spoken dialogue will NOT be generated natively; the flag is "
                    f"forced off on every video dispatch by _effective_generate_audio",
                )

            # ── Budget rail: constructed AFTER preflight, BEFORE the first paid
            # call. The plugin populated job.progress.budget at enqueue time
            # with estimate_usd/limit_usd; a legacy job.json without those keys
            # gets the same estimate recomputed from its settings.
            budget_state = job.progress.budget or {}
            if "estimate_usd" in budget_state and "limit_usd" in budget_state:
                self.ledger = BudgetLedger(
                    limit_usd=budget_state["limit_usd"],
                    estimate_usd=budget_state["estimate_usd"],
                    mode=job.settings.quality_mode,
                )
            else:
                _est = estimate_job_usd(
                    int(job.settings.num_scenes or 1),
                    job.settings.quality_mode,
                    include_music=job.settings.include_music,
                )
                _limit = derive_limit_usd(
                    _est["estimate_usd"], job.settings.budget_limit_usd
                )
                self.ledger = BudgetLedger(
                    limit_usd=_limit,
                    estimate_usd=_est["estimate_usd"],
                    mode=job.settings.quality_mode,
                )
                self.ledger.entries.append({
                    "kind": "budget_recomputed", "count": 0, "usd": 0.0,
                    "note": (
                        "legacy job.json carried no preflight budget keys; "
                        "estimate and limit recomputed from settings at run start"
                    ),
                })
            # A REQUEUED job must continue against the same limit, not a fresh
            # one. `BudgetLedger.__init__` starts at spent 0.0, so before this
            # the worker's stale-lease recovery path (_recover_stale_running
            # sets QUEUED again) handed the next worker a zeroed rail and the
            # job could spend roughly TWICE its declared limit — and this rail
            # is the only spend protection the skill has, because core
            # accounting cannot meter it. The persisted spend was being read
            # for nothing; it is now carried over and disclosed.
            try:
                prior_spent = float(budget_state.get("spent_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                prior_spent = 0.0
            if prior_spent > 0.0:
                self.ledger.spent_usd = round(prior_spent, 4)
                self.ledger.entries.append({
                    "kind": "budget_resumed",
                    "count": 0,
                    "usd": round(prior_spent, 4),
                    "note": (
                        "carried over from a previous attempt of this job "
                        "(worker recovery); the limit is NOT reset"
                    ),
                })
                self._warn(
                    job,
                    f"Budget resumed from a previous attempt: ${prior_spent:.2f} "
                    f"already spent against the ${self.ledger.limit_usd:.2f} limit, "
                    f"so this attempt continues on the SAME rail rather than a fresh one.",
                )

            # Persisted immediately so a crash leaves the real spend on disk.
            job.progress.budget = self.ledger.to_dict()

            self._check_shutdown()
            job.progress.phase = JobPhase.SCENARIO
            job.progress.status = JobStatus.RUNNING
            job.progress.message = "Generating storyboard..."
            job.progress.progress_pct = 5.0
            self._emit(job)

            # Storyboard admission + per-attempt charging live INSIDE
            # _generate_scenario (finding D: one charged unit previously covered
            # up to 3 parse attempts, each with provider retries).
            storyboard = await run_with_timeout(
                self._generate_scenario(job.settings, job=job),
                timeout_sec=TIMEOUT_SCENARIO,
                description="Storyboard generation",
            )
            job.progress.storyboard = storyboard
            job.progress.progress_pct = 15.0
            job.progress.message = f"Storyboard ready: {storyboard.title} ({len(storyboard.scenes)} scenes)"
            self._emit(job)

            self._check_shutdown()
            job.progress.phase = JobPhase.ASSETS
            if job.settings.include_music:
                job.progress.message = "Generating assets + music in parallel..."
            else:
                job.progress.message = "Generating assets (best-of-2 character sheets, keyframes)..."
            job.progress.progress_pct = 18.0
            self._emit(job)

            parallel_tasks = [self._generate_assets(job, storyboard)]
            if job.settings.include_music:
                parallel_tasks.append(self._generate_music(job, storyboard))
            await asyncio.gather(*parallel_tasks)

            job.progress.phase = JobPhase.VERIFICATION
            stats = job.progress.verification_stats
            passed = stats.get("passed", 0)
            retried = stats.get("retried", 0)
            failed = stats.get("failed", 0)
            music_count = len(job.progress.music_clips)
            job.progress.message = (
                f"Verification: {passed} passed, {retried} retried, {failed} failed"
                + (f" | Music: {music_count} clips" if job.settings.include_music else "")
            )
            job.progress.progress_pct = 55.0
            self._emit(job)

            job.progress.message = "Assets ready. Starting animation with multi-dim scoring..."
            self._emit(job)

            self._check_shutdown()
            job.progress.phase = JobPhase.ANIMATION
            job.progress.message = "Animating scenes (multidim scoring + frame anchoring)..."
            self._emit(job)

            await self._generate_videos(job, storyboard)

            self._check_shutdown()
            # `low` declares continuity "off" and budget._cross_scene_checks
            # estimates ZERO checks for it, but this call used to run (and
            # charge vlm_cross_scene) unconditionally — the mode matrix said one
            # thing and the ledger did another. Honour the declared policy so
            # the estimate and the real spend describe the same job.
            continuity_policy = str(mode_config(job.settings.quality_mode)["continuity"])
            if continuity_policy == "off":
                job.progress.verification_stats["cross_scene_last_verdict"] = "skipped_mode_off"
                worst_scene = None
            else:
                job.progress.message = "Running cross-scene identity check..."
                self._emit(job)
                worst_scene = await self._cross_scene_identity_check(job, storyboard)
            stats = job.progress.verification_stats
            # Bounded per-mode regeneration budget. continuity_regen_max == 0 (low
            # mode) means: check nothing extra, but if the check above already ran
            # and found drift, record and DISCLOSE it without regenerating.
            continuity_regen_max = int(mode_config(job.settings.quality_mode)["continuity_regen_max"])
            regens_used = 0
            while worst_scene is not None and regens_used < continuity_regen_max:
                # HARD STOP: a continuity regeneration is a fresh paid video —
                # never started once the budget is exhausted.
                if self._budget_stop(job, "continuity_regeneration"):
                    break
                if not (0 <= worst_scene < len(storyboard.scenes)):
                    self._warn(
                        job,
                        f"Cross-scene identity: target scene {worst_scene} is out of "
                        f"range for {len(storyboard.scenes)} scene(s); no regeneration "
                        f"was performed.",
                    )
                    worst_scene = None
                    break
                critique = str(stats.get("cross_scene_drift", ""))
                job.progress.message = f"Identity drift detected in scene {worst_scene}. Regenerating..."
                self._emit(job)
                if not await self._regenerate_single_scene(
                    job, storyboard, worst_scene, critique=critique
                ):
                    # Nothing changed and nothing may be paid for this scene:
                    # re-checking the same clips cannot help. The drift ships
                    # disclosed below.
                    break
                regens_used += 1
                # A gate that never inspects its own repair is not a gate. Re-run
                # the same reviewer once and record whether the drift is actually
                # gone, so "shipped with drift" can never look like "shipped clean".
                job.progress.message = "Re-checking identity after regeneration..."
                self._emit(job)
                residual = await self._cross_scene_identity_check(job, storyboard)
                stats["cross_scene_regen_scene"] = worst_scene
                stats["cross_scene_resolved"] = residual is None
                stats["cross_scene_regens_used"] = regens_used
                worst_scene = residual
            if worst_scene is not None:
                # Regeneration budget exhausted (or 0) and drift persists: ship it
                # DISCLOSED, never silently.
                severity = str(stats.get("cross_scene_severity", "unknown"))
                stats["continuity_accepted_with_drift"] = True
                job.progress.partial_reasons.append(
                    f"continuity_drift_accepted_after_{regens_used}_regens"
                )
                self._warn(
                    job,
                    f"Cross-scene identity drift ({severity}) PERSISTS in scene "
                    f"{worst_scene} after {regens_used} regeneration(s) "
                    f"(mode budget {continuity_regen_max}); shipping with DISCLOSED "
                    f"drift rather than looping.",
                )

            # ── HARD identity-gate CONSEQUENCE (owner item 3).
            # The panel used to append failures to
            # verification_stats["identity_panel_regen_candidates"] and NOTHING
            # ever read that list, so the "hard gate" could return `fail` and the
            # clip shipped exactly as if it had passed. Failures are consumed
            # here, inside the SAME per-mode regeneration budget as the
            # cross-scene loop above (`regens_used` continues) — one shared
            # budget, deliberately not a second unbounded loop. A scene that is
            # not repaired never ships as if the gate had passed: it lands in
            # unverified_scenes with a partial reason, which forces PARTIAL.
            panel_failed = list(stats.get("identity_panel_regen_candidates", []))
            panel_store = stats.get("identity_panel") or {}
            consumed: list = stats.setdefault("identity_panel_regen_consumed", [])
            for scene_index in panel_failed:
                if not (0 <= scene_index < len(storyboard.scenes)):
                    self._warn(
                        job,
                        f"Identity panel: target scene {scene_index} is out of range "
                        f"for {len(storyboard.scenes)} scene(s); not regenerated.",
                    )
                    continue
                record = panel_store.get(str(scene_index)) or {}
                attrs = record.get("failed_attributes") or []
                budget_spent = regens_used >= continuity_regen_max
                if budget_spent or self._budget_stop(job, "identity_panel_regeneration"):
                    self._record_indeterminate_scene(job, scene_index)
                    job.progress.partial_reasons.append(
                        f"identity_panel_failed_unresolved:{scene_index}"
                    )
                    self._warn(
                        job,
                        f"Identity panel FAILED scene {scene_index} "
                        f"(attributes: {', '.join(attrs) or 'unspecified'}) and the "
                        f"regeneration budget is exhausted "
                        f"({regens_used}/{continuity_regen_max}); the scene ships "
                        f"UNVERIFIED and the job is PARTIAL.",
                    )
                    continue
                critique = str(
                    record.get("reason") or "identity drift reported by the judge panel"
                )
                job.progress.message = (
                    f"Identity panel failed scene {scene_index}. Regenerating..."
                )
                self._emit(job)
                if not await self._regenerate_single_scene(
                    job, storyboard, scene_index, critique=critique
                ):
                    # Not regenerated (an earlier dispatch for it is unresolved),
                    # so it ships exactly like an unrepaired panel failure.
                    self._record_indeterminate_scene(job, scene_index)
                    job.progress.partial_reasons.append(
                        f"identity_panel_failed_unresolved:{scene_index}"
                    )
                    continue
                regens_used += 1
                consumed.append(scene_index)
                stats["identity_panel_regens_used"] = regens_used
                # A gate that never inspects its own repair is not a gate.
                scene_obj = storyboard.scenes[scene_index]
                if scene_obj.video_url and Path(scene_obj.video_url).exists():
                    recheck = await self._judge_identity_panel(
                        job, scene_obj.video_url, scene_obj, storyboard
                    )
                else:
                    recheck = {
                        "verdict": "indeterminate",
                        "reason": "regeneration produced no usable clip",
                    }
                stats.setdefault("identity_panel_recheck", {})[str(scene_index)] = (
                    recheck.get("verdict")
                )
                if recheck.get("verdict") != "pass":
                    self._record_indeterminate_scene(job, scene_index)
                    job.progress.partial_reasons.append(
                        f"identity_panel_failed_after_regen:{scene_index}"
                    )
                    self._warn(
                        job,
                        f"Identity panel still {recheck.get('verdict')} for scene "
                        f"{scene_index} after regeneration "
                        f"({recheck.get('reason', '?')}); shipping UNVERIFIED with "
                        f"DISCLOSED drift.",
                    )

            job.progress.progress_pct = 92.0
            job.progress.message = "All scenes animated. Assembling final video..."
            self._emit(job)

            self._check_shutdown()
            job.progress.phase = JobPhase.ASSEMBLY
            self._emit(job)

            assembly = await self._assemble(job, storyboard)
            job.progress.final_video_url = assembly["path"]
            job.progress.phase = JobPhase.DONE
            job.progress.progress_pct = 100.0

            # A missing scene is recorded, never silently dropped: a real 45s
            # request once shipped as 30.18s with a green DONE status because
            # assembly filtered absent clips without a trace.
            job.progress.missing_scenes = sorted(
                set(job.progress.missing_scenes) | set(assembly["missing_scenes"])
            )
            stats = job.progress.verification_stats
            unverified = sorted(set(job.progress.unverified_scenes))
            partial_reasons = list(job.progress.partial_reasons)
            is_partial = bool(
                job.progress.missing_scenes
                or unverified
                or partial_reasons
                or stats.get("continuity_accepted_with_drift")
            )
            job.progress.status = JobStatus.PARTIAL if is_partial else JobStatus.DONE
            stats["outcome"] = {
                "status": "partial" if is_partial else "done",
                "clips_used": assembly["clips_used"],
                "scenes_total": assembly["scenes_total"],
                "missing_scenes": list(job.progress.missing_scenes),
                "unverified_scenes": unverified,
                "partial_reasons": partial_reasons,
            }

            img_lessons = [l for l in self._learned_lessons if "text" in l.lower() or "character" in l.lower()]
            vid_lessons = [l for l in self._learned_lessons if l not in img_lessons]
            # Isolated on purpose: this runs AFTER final_video_url, phase=DONE and
            # the partial/done status are already set, and `_persist_lessons`
            # writes with an unguarded write_text. An unguarded IO failure here
            # would propagate to the outer `except Exception`, which overwrites
            # phase and status with ERROR — reporting a fully assembled, DELIVERED
            # video as a failed job over a cosmetic cache write. Cross-job
            # learning is optional; the delivered result is not.
            try:
                self._persist_lessons(img_lessons, vid_lessons or self._learned_lessons)
            except Exception as exc:  # noqa: BLE001
                self._warn(
                    job,
                    "Prompt-lesson persistence failed "
                    f"({type(exc).__name__}: {exc}); the delivered video is unaffected, "
                    "only cross-job learning was not saved",
                )

            if is_partial:
                # A partial outcome must be unmissable — never "Animation complete!".
                job.progress.message = (
                    f"PARTIAL delivery: {assembly['clips_used']}/"
                    f"{assembly['scenes_total']} scenes in the final video; "
                    f"{len(job.progress.missing_scenes)} missing scene(s) "
                    f"{job.progress.missing_scenes}, {len(unverified)} unverified "
                    f"scene(s) {unverified}. Reasons: "
                    f"{'; '.join(partial_reasons) or 'see warnings'}"
                )
            elif job.progress.warnings:
                job.progress.message = f"Done with {len(job.progress.warnings)} warning(s)."
            else:
                job.progress.message = "Animation complete!"
            self._emit(job)

        except Exception as e:
            logger.exception("Pipeline error")
            job.progress.phase = JobPhase.ERROR
            job.progress.status = JobStatus.ERROR
            job.progress.error = str(e)
            job.progress.message = f"Error: {e}"
            self._emit(job)

        return job

    # ─── Phase 1: Scenario ──────────────────────────────────────────

    async def _generate_scenario(self, settings: GenerationSettings, job: Optional[Job] = None) -> Storyboard:
        """Generate storyboard via LLM with automatic retry on malformed JSON."""
        prompt = SCENARIO_USER_TEMPLATE.format(
            theme=settings.theme, style=settings.style,
            duration_sec=settings.duration_sec, num_scenes=settings.num_scenes,
            mood=settings.mood, include_dialogue=settings.include_dialogue,
            music_style=settings.music_style,
        )
        max_attempts = 3
        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            self._check_shutdown()
            if attempt > 1 and job is not None:
                job.progress.message = f"Storyboard parse failed, retrying ({attempt}/{max_attempts})..."
                self._emit(job)
            # FINDING D (fixed): the storyboard was previously charged ONE unit
            # in run() while this loop could dispatch up to 3 attempts. Each
            # attempt is now admitted and charged individually.
            if job is not None:
                if not self._admit(job, "storyboard"):
                    last_error = RuntimeError("budget admission refused for storyboard attempt")
                    break
                self._charge(job, "storyboard", note=f"attempt {attempt}")
            response = await self.client.chat(
                messages=[
                    {"role": "system", "content": SCENARIO_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                model="anthropic/claude-sonnet-4.6",
                max_toks=DEFAULT_MAX_TOKENS, temperature=0.8,
                json_mode=True,
            )
            try:
                data = self.client.parse_json_response(response)
                scenes = []
                for s in data["scenes"]:
                    scenes.append(Scene(
                        index=s["index"], description=s["description"],
                        duration_sec=s["duration_sec"], characters=s["characters"],
                        location=s["location"], camera_direction=s["camera_direction"],
                        dialogue=s.get("dialogue"), mood=s.get("mood", "neutral"),
                        transition_from=s.get("transition_from"),
                        causal_link=s.get("causal_link"),
                    ))
                return Storyboard(
                    title=data["title"], synopsis=data["synopsis"],
                    style=data["style"], total_duration_sec=data["total_duration_sec"],
                    # Field-filtered like every other construction site: a model
                    # that volunteers one extra key raises TypeError, and each
                    # retry of this parse is a SEPARATELY CHARGED storyboard
                    # attempt. Missing REQUIRED keys still raise, which is the
                    # intended retry signal; only surplus keys are tolerated.
                    characters=[Character(**_fields_of(Character, c)) for c in data["characters"]],
                    locations=[Location(**_fields_of(Location, loc)) for loc in data["locations"]],
                    scenes=scenes,
                    music_cues=[MusicCue(**_fields_of(MusicCue, m)) for m in data["music_cues"]],
                )
            except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
                last_error = e
                logger.warning(
                    f"Scenario parse failed (attempt {attempt}/{max_attempts}): {e}"
                )
                if attempt < max_attempts:
                    await asyncio.sleep(1)  # brief pause before retry
        raise ValueError(
            f"Failed to generate valid storyboard after {max_attempts} attempts. "
            f"Last error: {last_error}"
        )

    # ─── Phase 2: Assets (Best-of-2 chars parallel, locs parallel, keyframes sequential)

    async def _generate_assets(self, job: Job, storyboard: Storyboard):
        """Generate assets with maximum parallelism where dependencies allow."""

        char_tasks = [
            self._generate_best_of_2_character_sheet(job, char, i, storyboard.style)
            for i, char in enumerate(storyboard.characters)
        ]
        char_results = await asyncio.gather(*char_tasks, return_exceptions=True)

        for i, result in enumerate(char_results):
            self._check_shutdown()
            if isinstance(result, Exception):
                self._warn(job, f"Character sheet '{storyboard.characters[i].name}' failed: {_describe_exc(result)}")
            elif result:
                storyboard.characters[i].sheet_url = result
                job.progress.character_sheets.append(result)

        job.progress.progress_pct = 30.0
        job.progress.message = f"Character sheets (best-of-2): {len(job.progress.character_sheets)}/{len(storyboard.characters)}"
        self._emit(job)
        self._check_shutdown()

        loc_tasks = []
        # Admission runs BEFORE the coroutines are created: refusing after
        # building them would leave never-awaited coroutines behind.
        locations = list(storyboard.locations)
        if locations and not self._charge(job, "location_art", count=len(locations)):
            self._warn(job, f"Location art ({len(locations)} location(s)) skipped: budget hard stop")
            locations = []
        for i, loc in enumerate(locations):
            prompt = IMAGE_LOCATION_PROMPT.format(
                name=loc.name, visual_traits=loc.visual_traits, style=storyboard.style,
            )
            loc_tasks.append(
                run_with_timeout(
                    self._generate_image(job, prompt, f"loc_{i}_{loc.name.lower().replace(' ', '_')}.png", "16:9"),
                    timeout_sec=TIMEOUT_IMAGE,
                    description=f"Location '{loc.name}'",
                )
            )
        loc_results = await asyncio.gather(*loc_tasks, return_exceptions=True)
        for i, result in enumerate(loc_results):
            if isinstance(result, Exception):
                self._warn(job, f"Location '{storyboard.locations[i].name}' art failed: {_describe_exc(result)}")
            else:
                storyboard.locations[i].art_url = result
                job.progress.location_arts.append(result)

        self._check_shutdown()

        char_ref_desc = "; ".join(f"{c.name}: {c.visual_traits}" for c in storyboard.characters)
        chars_identity_block = self._build_characters_identity_block(storyboard)
        prev_keyframe_desc = None
        prev_keyframe_path: Optional[str] = None
        prev_keyframe_ok = False

        for i, scene in enumerate(storyboard.scenes):
            self._check_shutdown()
            char_names = ", ".join(scene.characters)
            loc = next((l for l in storyboard.locations if l.name == scene.location), None)
            loc_desc = loc.visual_traits if loc else scene.location

            # Reference images with RESERVED SLOTS (cap 4), because character identity
            # carried by prose alone drifts: a run whose own cross-scene check reported
            # "hair is long and flowing in the first scene, which contradicts her
            # short-cropped design in the second" is exactly this failure.
            #
            # Slot 0 is the previous keyframe, but ONLY when it passed verification.
            # Conditioning the next frame on a REJECTED one teaches the model the drift
            # and compounds it down the run, so a bad frame is never fed forward.
            # The character sheet is the stable identity anchor; the previous frame only
            # provides shot-to-shot continuity and must not be allowed to poison identity.
            refs: list = []
            if prev_keyframe_path and prev_keyframe_ok:
                refs.append(prev_keyframe_path)
            scene_sheets = [
                c.sheet_url for name in scene.characters
                for c in storyboard.characters
                if c.name == name and c.sheet_url
            ]
            refs.extend(scene_sheets[:2])
            if loc and loc.art_url:
                refs.append(loc.art_url)
            elif len(scene_sheets) > 2:
                refs.append(scene_sheets[2])
            refs = [p for p in refs if p and Path(p).exists()][:4]
            job.progress.verification_stats.setdefault("references_used", {})[str(scene.index)] = [
                Path(p).name for p in refs
            ]

            if i == 0 or prev_keyframe_desc is None:
                prompt = IMAGE_KEYFRAME_PROMPT.format(
                    scene_description=scene.description, characters=char_names,
                    location_description=loc_desc, camera_direction=scene.camera_direction,
                    mood=scene.mood, style=storyboard.style,
                )
            else:
                prompt = IMAGE_KEYFRAME_SEQUENTIAL_PROMPT.format(
                    scene_description=scene.description, characters=char_names,
                    location_description=loc_desc, camera_direction=scene.camera_direction,
                    mood=scene.mood, style=storyboard.style,
                    prev_keyframe_context=prev_keyframe_desc,
                    characters_identity_block=chars_identity_block,
                )

            # Story causality is APPENDED rather than injected as a template
            # placeholder, so no existing .format() call site can raise KeyError.
            if scene.causal_link:
                prompt += (
                    f"\n\nSTORY CAUSALITY (this shot exists because of the previous one): "
                    f"{scene.causal_link}"
                )

            if not self._charge(job, "keyframe", note=f"scene {scene.index}"):
                # The scene simply has no keyframe — same as a failed
                # generation below; earlier keyframes are kept.
                self._warn(job, f"Keyframe scene {scene.index} skipped: budget hard stop")
                continue
            try:
                result = await run_with_timeout(
                    self._generate_image(
                        job, prompt, f"keyframe_{scene.index}.png", "16:9",
                        reference_images=refs,
                    ),
                    timeout_sec=TIMEOUT_IMAGE,
                    description=f"Keyframe scene {scene.index}",
                )
                verified_path = await self._verify_and_retry(
                    job, result, prompt, f"keyframe_{scene.index}.png", "16:9",
                    char_ref_desc=char_ref_desc, reference_images=refs,
                )
                scene.keyframe_url = verified_path
                job.progress.keyframes.append(verified_path)
                prev_keyframe_desc = f"Scene {scene.index}: {scene.description} (camera: {scene.camera_direction})"
                prev_keyframe_path = verified_path
                prev_keyframe_ok = self._last_keyframe_score >= 7
            except Exception as e:
                self._warn(job, f"Keyframe scene {i} failed: {_describe_exc(e)}")

            base_pct = 32.0
            increment = 16.0 / max(1, len(storyboard.scenes))
            job.progress.progress_pct = base_pct + increment * (i + 1)
            job.progress.message = f"Keyframes: {len(job.progress.keyframes)}/{len(storyboard.scenes)}"
            self._emit(job)

    # ─── Phase 2b: Music ────────────────────────────────────────────

    async def _generate_music(self, job: Job, storyboard: Storyboard):
        """Generate music clips in parallel."""
        music_tasks = []
        # Admission BEFORE coroutine creation, same reason as location art.
        cues = list(storyboard.music_cues)
        if cues and not self._charge(job, "music_cue", count=len(cues)):
            self._warn(job, f"Music ({len(cues)} cue(s)) skipped: budget hard stop")
            cues = []
        for cue in cues:
            prompt = MUSIC_PROMPT_TEMPLATE.format(
                mood=cue.mood, tempo=cue.tempo, style=cue.style,
                duration_sec=cue.duration_sec, description=cue.description,
            )
            music_tasks.append(
                run_with_timeout(
                    self.client.generate_music(prompt=prompt, filename=f"music_{cue.segment_index}.mp3"),
                    timeout_sec=TIMEOUT_MUSIC,
                    description=f"Music cue {cue.segment_index}",
                )
            )
        results = await asyncio.gather(*music_tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                self._warn(job, f"Music cue {i} failed: {_describe_exc(result)}")
            else:
                storyboard.music_cues[i].audio_url = result
                job.progress.music_clips.append(result)
        job.progress.message = f"Music: {len(job.progress.music_clips)}/{len(storyboard.music_cues)} clips"
        self._emit(job)

    # ─── Failure Advisor (LLM-powered error recovery) ─────────────

    AVAILABLE_VIDEO_MODELS = [
        "bytedance/seedance-2.0",
        "bytedance/seedance-2.0-fast",
        "bytedance/seedance-1-5-pro",
        "google/veo-3.1",
        "google/veo-3.1-fast",
        "google/veo-3.1-lite",
        "minimax/hailuo-2.3",
        # hailuo-3 must be listed here or the failure advisor physically cannot
        # switch TO it (and, when the owner selects it, cannot switch AWAY and
        # back). Absence from this list is not neutral: it silently removes a
        # model from every recovery path while the UI still offers it.
        "minimax/hailuo-3",
        "kwaivgi/kling-v3.0-pro",
        "kwaivgi/kling-v3.0-std",
        "kwaivgi/kling-video-o1",
    ]

    # ONE prompt-limit table, owned by api_client (the module that actually
    # enforces it at the POST). A second copy here had already drifted: it
    # omitted minimax/hailuo-3, so an over-long H3 prompt skipped the good
    # LLM condensation and only met the mechanical hard-fit at dispatch time.
    _VIDEO_MODEL_PROMPT_LIMIT = VIDEO_MODEL_PROMPT_LIMIT

    _REFERENCE_TOKEN_RE = re.compile(r"@Image\d+")

    async def _condense_prompt_for_model(self, prompt: str, model: str) -> str:
        """Shorten *prompt* via LLM if it exceeds the model's character limit.

        The condensed candidate is REJECTED unless every line that names an
        `@ImageN` token survives VERBATIM. Token presence alone is not a binding:
        a candidate that swaps two characters' descriptions, or keeps only "use
        @Image1 and @Image2", still names every token while the attached images
        no longer mean who the prompt says. This is the owner's original
        complaint reproduced through the success branch: in a live job the LLM
        condenser dropped the reference block and the images stayed attached to
        a prompt that no longer bound them. The POST-time fitter cannot catch
        it, because a condensed prompt is already UNDER the limit.
        """
        limit = self._VIDEO_MODEL_PROMPT_LIMIT.get(model)
        if not limit or len(prompt) <= limit:
            return prompt
        required = tuple(sorted(set(self._REFERENCE_TOKEN_RE.findall(prompt))))
        bindings = [
            line.strip() for line in prompt.split("\n")
            if self._REFERENCE_TOKEN_RE.search(line)
        ]
        target = limit - 50
        logger.info(f"Prompt {len(prompt)} chars > {model} limit {limit}, condensing to ~{target}")
        req = (
            "You are a video prompt editor. Rewrite the prompt below to fit within "
            f"{target} characters. Keep ALL character visuals, camera, action, mood, "
            "and style. Remove boilerplate/negative constraints. "
            + (
                "You MUST reproduce VERBATIM every line that names a reference image "
                f"({', '.join(required)}) — those tokens bind the attached reference "
                "images to specific characters and locations; dropping one silently "
                "unbinds it. "
                if required else ""
            )
            + "Return ONLY the text.\n\n"
            f"--- ORIGINAL ---\n{prompt}\n--- END ---"
        )
        try:
            condensed = (await run_with_timeout(
                self.client.chat(
                    messages=[{"role": "user", "content": req}],
                    model="google/gemini-3.5-flash", max_toks=DEFAULT_MAX_TOKENS, temperature=0.2,
                ), timeout_sec=30, description="Prompt condensation",
            )).strip()
            missing = [t for t in required if t not in condensed]
            kept_lines = {line.strip() for line in condensed.split("\n")}
            altered = [line for line in bindings if line not in kept_lines]
            if 100 < len(condensed) <= limit and not missing and not altered:
                logger.info(
                    "Prompt condensed: %d → %d chars (%d reference token(s) preserved)",
                    len(prompt), len(condensed), len(required),
                )
                return condensed
            if missing or altered:
                logger.warning(
                    "Condensation changed the reference binding (dropped token(s): %s; "
                    "%d binding line(s) not kept verbatim) — rejecting the candidate; "
                    "the reference images would stay attached to a prompt that no "
                    "longer binds them as written",
                    ", ".join(missing) or "none", len(altered),
                )
            else:
                logger.warning("Condensation returned %d chars, hard-fitting", len(condensed))
            return self._fit_preserving_references(prompt, limit, required)
        except Exception as e:
            logger.warning(f"Condensation failed ({_describe_exc(e)}), hard-fitting")
            return self._fit_preserving_references(prompt, limit, required)

    def _fit_preserving_references(
        self, prompt: str, limit: int, required: tuple[str, ...]
    ) -> str:
        """Mechanical fit through the SAME token-preserving fitter the POST uses.

        A second, weaker hard-fit here is how the reference block could be lost on
        the fallback path while `api_client.fit_prompt_to_limit` protected it on
        the dispatch path. When even that fitter cannot fit the protected lines it
        raises; we return the prompt UNCHANGED so the single fail-closed gate at
        the POST is the one place that refuses, instead of two places guessing.
        """
        if not required:
            return self._hard_fit_prompt(prompt, limit)
        try:
            return fit_prompt_to_limit(prompt, limit, required)
        except VideoPromptFitError:
            logger.warning(
                "Protected reference lines do not fit %d chars; deferring the refusal "
                "to the dispatch gate", limit,
            )
            return prompt

    _TRUNCATION_MARKER = "\n[…prompt shortened to fit this model's character limit…]\n"

    def _hard_fit_prompt(self, prompt: str, limit: int) -> str:
        """Fit *prompt* to *limit* with the loss DISCLOSED inside the prompt itself.

        A bare prompt[:limit] silently deleted whatever was appended LAST — which is
        precisely the SPOKEN DIALOGUE block and the learned-lessons block — so a scene
        could lose its speech requirement with no trace at all. The dialogue block is
        preserved by cutting the MIDDLE instead of the tail, and the cut is marked so a
        reader of the prompt can see that material was removed.
        """
        marker = self._TRUNCATION_MARKER
        # Only protect the tail while there is still room for meaningful head
        # content; the lessons after the dialogue are not part of it when the
        # whole remainder does not fit (split_protected_dialogue).
        body, tail = split_protected_dialogue(prompt, limit // 2 - len(marker))
        head_room = limit - len(marker) - len(tail)
        if head_room <= 0:
            logger.warning("Prompt limit %d too small to disclose truncation; hard slice", limit)
            return prompt[:limit]
        logger.warning(
            "Prompt hard-fit: %d -> %d chars (limit %d); dialogue block %s",
            len(prompt), head_room + len(marker) + len(tail), limit,
            "preserved" if tail else "NOT preserved",
        )
        return body[:head_room] + marker + tail

    async def _get_failure_advisor_recommendation(
        self, error: str, current_model: str, scene_description: str,
    ) -> dict:
        """Ask a fast LLM to analyze a video generation failure and recommend an action."""
        alternatives = [m for m in self.AVAILABLE_VIDEO_MODELS if m != current_model]
        prompt = (
            "You are an AI video generation advisor. A video generation request failed.\n\n"
            f"Error: {error}\n"
            f"Current model: {current_model}\n"
            f"Scene: {scene_description[:300]}\n\n"
            f"Available alternative models: {', '.join(alternatives)}\n\n"
            "Analyze the error and recommend ONE action:\n"
            '- "retry_same_model" — if the error is transient (timeout, rate limit, server error, 500/502/503)\n'
            '- "switch_model" — if the error is model-specific (copyright filter, content policy, '
            "unsupported feature). Pick the best alternative from the list above.\n"
            '- "skip" — if the error is fundamental and no model can help (invalid prompt, impossible request)\n\n'
            'Return ONLY valid JSON: {"action": "...", "reason": "...", "suggested_model": "model_id_or_null"}'
        )
        last_error: Exception | None = None
        for model in ("google/gemini-3.5-flash", "anthropic/claude-sonnet-4.6"):
            try:
                response = await run_with_timeout(
                    self.client.chat(
                        messages=[{"role": "user", "content": prompt}],
                        model=model,
                        max_toks=DEFAULT_MAX_TOKENS,
                        temperature=0.1,
                        json_mode=True,
                    ),
                    timeout_sec=45,
                    description=f"Failure advisor ({model})",
                )
                result = self.client.parse_json_response(response)
                action = result.get("action", "skip")
                if action not in ("retry_same_model", "switch_model", "skip"):
                    action = "skip"
                suggested = result.get("suggested_model")
                if action == "switch_model" and suggested not in alternatives:
                    suggested = alternatives[0] if alternatives else None
                    if not suggested:
                        action = "skip"
                return {"action": action, "reason": result.get("reason", ""), "suggested_model": suggested}
            except Exception as e:
                last_error = e
                logger.warning(f"Failure advisor call failed via {model}: {e}")
        return {"action": "skip", "reason": f"Advisor unavailable: {last_error}", "suggested_model": None}

    # ─── Phase 3: Video Animation ──────────────────────────────────

    async def _generate_videos(self, job: Job, storyboard: Storyboard):
        """Generate video for each scene with multidim scoring and adaptive simplification."""
        prev_frame_path: Optional[str] = None
        chars_identity_block = self._build_characters_identity_block(storyboard)
        self._locations_by_name = {l.name: l for l in storyboard.locations}

        for i, scene in enumerate(storyboard.scenes):
            self._check_shutdown()
            # HARD STOP before each new scene: break (never raise), record the
            # unreached scenes, keep every asset already generated.
            if self._budget_stop(job, f"before_scene_{scene.index}"):
                for remaining in storyboard.scenes[i:]:
                    if remaining.index not in job.progress.missing_scenes:
                        job.progress.missing_scenes.append(remaining.index)
                break

            # Reference assembly goes through ONE ordered seam so the @ImageN
            # labels in the prompt and the input_references submission order
            # can never disagree (see _assemble_scene_references).
            scene_char_objs = [
                c for name in scene.characters for c in storyboard.characters if c.name == name
            ]
            assembled = self._assemble_scene_references(
                scene, scene_char_objs, prev_frame_path, VIDEO_MAX_INPUT_REFERENCES,
            )
            self._record_reference_state(job, scene.index, assembled)
            references = assembled["references"]
            frame_images = assembled["frame_images"] or None

            continuity_note = ""
            if i > 0 and scene.transition_from:
                continuity_note = SCENE_TRANSITION_TEMPLATE.format(
                    prev_scene_description=storyboard.scenes[i - 1].description,
                    transition_type=scene.transition_from,
                )
            if i > 0 and scene.causal_link:
                continuity_note += f"\nNarrative causality: {scene.causal_link}"

            base_prompt = build_video_prompt(
                scene_description=scene.description,
                reference_block=build_reference_block(assembled["labels"]),
                characters_identity_block=chars_identity_block,
                camera_direction=scene.camera_direction,
                mood=scene.mood, style=storyboard.style,
                duration_sec=int(scene.duration_sec),
                continuity_note=continuity_note,
            )
            # Seedance native audio: put spoken lines in the video prompt so the
            # model generates speech, not just silent lips. include_dialogue gates it.
            if job.settings.include_dialogue and scene.dialogue:
                base_prompt += (
                    "\n\nSPOKEN DIALOGUE (characters MUST speak this aloud with clear "
                    "native audio / speech — not silent mime, not subtitles):\n"
                    f"{scene.dialogue}"
                )

            lessons_text = self._get_lessons_text()
            if lessons_text and "No lessons" not in lessons_text:
                base_prompt += f"\n\nLEARNED FROM PREVIOUS GENERATIONS (apply these):\n{lessons_text}"

            # Native Seedance/Seedream audio on by default when dialogue is wanted.
            # Hardcoded False previously produced silent clips even with music mix.
            # Routed through the ONE effective-options seam so this path, both
            # advisor retries, and continuity regeneration cannot disagree.
            want_native_audio = self._effective_generate_audio(job)

            video_path = None
            # Best clip generated for THIS scene so far. A clip that downloaded
            # successfully is never thrown away because a LATER attempt raised: a real
            # run produced a valid 15s scene_3 clip on disk and still shipped a final
            # video without that scene, because the advisor's retry failed afterwards
            # and the handler reset video_path to None.
            best_video_path: Optional[str] = None
            best_video_rank: tuple = (-1, -1.0)
            all_issues: list[str] = []
            current_description = scene.description
            current_camera = scene.camera_direction

            # FINDING A (contract defeat, fixed): this loop previously always
            # allowed MAX_VIDEO_VERIFY_RETRIES + 1 (= 3) ordinary candidate
            # attempts in EVERY mode, so `low` generated up to 3 clips while
            # declaring 1 and the whole mode/budget matrix was invalidated. The
            # ordinary candidate budget now comes from the mode's
            # `video_candidates`. The advisor retry/model-switch path in the
            # except-handler below is an ADDITIONAL recovery attempt beyond this
            # candidate budget; the ledger charges it as its own `video_clip`
            # unit. Do not re-couple this loop to the module constant.
            candidate_attempts = max(1, int(mode_config(job.settings.quality_mode)["video_candidates"]))
            for attempt in range(candidate_attempts):
                current_prompt = base_prompt
                if attempt > 0 and all_issues:
                    current_prompt += "\n\nCRITICAL FIXES REQUIRED: " + "; ".join(all_issues[-3:])

                if attempt >= 2 and all_issues:
                    simplified_desc, simplified_cam, neg_constraints = await self._simplify_scene(
                        scene, all_issues
                    )
                    current_description = simplified_desc
                    current_camera = simplified_cam
                    current_prompt = build_video_prompt(
                        scene_description=simplified_desc,
                        reference_block=build_reference_block(assembled["labels"]),
                        characters_identity_block=chars_identity_block,
                        camera_direction=simplified_cam,
                        mood=scene.mood, style=storyboard.style,
                        duration_sec=int(scene.duration_sec),
                        continuity_note=continuity_note,
                    )
                    if neg_constraints:
                        current_prompt += f"\n\nADDITIONAL NEGATIVE CONSTRAINTS: {neg_constraints}"
                    logger.info(f"Scene {scene.index} simplified for attempt {attempt+1}")

                # Reset per attempt: the except-branch refund must see THIS
                # dispatch's charge or nothing, never an earlier clip's receipt.
                _clip_receipt = None
                try:
                    final_prompt = await self._condense_prompt_for_model(
                        current_prompt, job.settings.video_model,
                    )

                    if not self._admit(job, "video_clip"):
                        video_path = None
                        break
                    _clip_receipt = self._charge(
                        job, "video_clip", note=f"scene {scene.index} attempt {attempt + 1}"
                    )
                    video_path = await run_with_timeout(
                        self.client.generate_video(
                            prompt=final_prompt,
                            filename=f"scene_{scene.index}_v{attempt}.mp4",
                            duration=self._clamp_duration(int(scene.duration_sec), job.settings.video_model),
                            resolution=job.settings.resolution,
                            aspect_ratio=job.settings.aspect_ratio,
                            input_references=references if references else None,
                            frame_images=frame_images,
                            model=job.settings.video_model,
                            generate_audio=want_native_audio,
                            audit_sink=self._video_audit_sink(job, scene.index, f"candidate_{attempt + 1}"),
                        ),
                        timeout_sec=TIMEOUT_VIDEO,
                        description=f"Video scene {scene.index} (attempt {attempt+1})",
                    )

                    # Multi-dimensional verification
                    verify_result = await self._verify_video_multidim(job, video_path, scene, storyboard)
                    stats = job.progress.verification_stats

                    # Rank this candidate: a PASS always beats a FAIL, then the existing
                    # multidim weighted score decides, and ties keep the EARLIER attempt
                    # (strict > below), so selection is deterministic.
                    _rank = (
                        1 if verify_result.get("passed", False) else 0,
                        float(verify_result.get("weighted_score") or 0.0),
                    )
                    if _rank > best_video_rank:
                        best_video_rank, best_video_path = _rank, video_path

                    _verdict = verify_result.get(
                        "verdict", "pass" if verify_result.get("passed", False) else "fail"
                    )
                    if _verdict == "indeterminate":
                        # "Could not check" is NOT "checked and passed": keep the clip,
                        # but ship it flagged as unverified instead of retrying blind.
                        self._record_indeterminate_scene(job, scene.index)
                        logger.info(
                            f"Video scene {scene.index} verification indeterminate "
                            f"({verify_result.get('reason', '?')}); clip kept UNVERIFIED"
                        )
                        break
                    elif _verdict == "pass":
                        stats["video_passed"] = stats.get("video_passed", 0) + 1
                        scores = verify_result.get("scores", {})
                        logger.info(
                            f"Video scene {scene.index} passed (weighted={verify_result.get('weighted_score', '?')}, "
                            f"scores={scores})"
                        )
                        break
                    else:
                        stats["video_retried"] = stats.get("video_retried", 0) + 1
                        issues = verify_result.get("issues", [])
                        suggestion = verify_result.get("suggestion", "")
                        all_issues.extend(issues)
                        if suggestion:
                            self._add_lesson(suggestion, "video")
                        scores = verify_result.get("scores", {})
                        logger.info(
                            f"Video scene {scene.index} failed multidim (attempt {attempt+1}): "
                            f"weighted={verify_result.get('weighted_score', '?')}, scores={scores}"
                        )
                        if attempt >= candidate_attempts - 1:
                            stats["video_failed"] = stats.get("video_failed", 0) + 1
                            self._warn(job, f"Video scene {scene.index} failed after {candidate_attempts} attempt(s)")

                except Exception as e:
                    error_str = str(e)
                    logger.exception("Video scene %s generation failed", scene.index)
                    self._warn(job, f"Video scene {i} failed: {_describe_exc(e)}")
                    # A validation refusal generated nothing: give the money back.
                    self._refund_if_rejected(job, _clip_receipt, e)
                    _unknown = self._paid_outcome_unknown(e, job.settings.video_model)
                    if _unknown is not None:
                        # The provider accepted, or may have accepted, this clip;
                        # it may exist and be billed. No advisor and no further
                        # candidate: a fresh paid clip for the scene could pay twice.
                        self._record_unretrieved(
                            job, scene.index, f"candidate_{attempt + 1}", _unknown,
                        )
                        video_path = None
                        break

                    _err_lower = error_str.lower()
                    _is_prompt_limit = any(
                        kw in _err_lower
                        for kw in (
                            "prompt: size must be", "prompt too long", "prompt length",
                            "maximum prompt",
                            # minimax/hailuo-3's exact wording, which none of the
                            # patterns above matched: "invalid params,
                            # content[0].text too long: 8640 > 7000 characters".
                            "text too long",
                        )
                    )
                    if _is_prompt_limit:
                        alts = [m for m in self.AVAILABLE_VIDEO_MODELS if m != job.settings.video_model]
                        self._warn(
                            job,
                            f"Model {job.settings.video_model} rejected the prompt as too long. "
                            f"Try switching to a different model (e.g. {alts[0] if alts else 'N/A'})."
                        )
                        video_path = None
                        break

                    job.progress.message = f"Scene {i} failed — consulting advisor..."
                    self._emit(job)
                    advice = await self._get_failure_advisor_recommendation(
                        error_str, job.settings.video_model, scene.description,
                    )
                    action = advice.get("action", "skip")
                    reason = advice.get("reason", "")
                    suggested_model = advice.get("suggested_model")

                    if reason:
                        self._warn(job, f"Advisor ({action}): {reason}")

                    # An advisor retry/switch is a fresh paid clip: admit it
                    # like any other dispatch (finding D).
                    if action in ("retry_same_model", "switch_model") and not self._admit(job, "video_clip"):
                        action = "skip"

                    if action == "retry_same_model":
                        job.progress.message = f"Advisor: retrying scene {i} with {job.settings.video_model}..."
                        self._emit(job)
                        _retry_receipt = None
                        try:
                            retry_prompt = await self._condense_prompt_for_model(
                                current_prompt, job.settings.video_model,
                            )
                            _retry_receipt = self._charge(
                                job, "video_clip", note=f"scene {scene.index} advisor retry"
                            )
                            if not _retry_receipt:
                                # Admission ran before the awaited condensation; a
                                # refused charge must never become an uncharged
                                # paid dispatch.
                                raise RuntimeError(
                                    "budget rail refused the charge; the clip was not dispatched"
                                )
                            video_path = await run_with_timeout(
                                self.client.generate_video(
                                    prompt=retry_prompt,
                                    filename=f"scene_{scene.index}_advisor_retry.mp4",
                                    duration=self._clamp_duration(int(scene.duration_sec), job.settings.video_model),
                                    resolution=job.settings.resolution,
                                    aspect_ratio=job.settings.aspect_ratio,
                                    input_references=references if references else None,
                                    frame_images=frame_images,
                                    model=job.settings.video_model,
                                    generate_audio=want_native_audio,
                                    audit_sink=self._video_audit_sink(job, scene.index, "advisor_retry"),
                                ),
                                timeout_sec=TIMEOUT_VIDEO,
                                description=f"Advisor retry scene {scene.index}",
                            )
                        except Exception as retry_e:
                            self._warn(job, f"Advisor retry also failed: {_describe_exc(retry_e)}")
                            self._refund_if_rejected(job, _retry_receipt, retry_e)
                            _unknown = self._paid_outcome_unknown(retry_e, job.settings.video_model)
                            if _unknown is not None:
                                self._record_unretrieved(job, scene.index, "advisor_retry", _unknown)
                            video_path = None
                    elif action == "switch_model" and suggested_model:
                        job.progress.message = f"Advisor: switching to {suggested_model} for scene {i}..."
                        self._emit(job)
                        _switch_receipt = None
                        try:
                            switch_prompt = await self._condense_prompt_for_model(
                                current_prompt, suggested_model,
                            )
                            _switch_receipt = self._charge(
                                job, "video_clip",
                                note=f"scene {scene.index} advisor switch {suggested_model}",
                            )
                            if not _switch_receipt:
                                raise RuntimeError(
                                    "budget rail refused the charge; the clip was not dispatched"
                                )
                            video_path = await run_with_timeout(
                                self.client.generate_video(
                                    prompt=switch_prompt,
                                    filename=f"scene_{scene.index}_alt.mp4",
                                    duration=self._clamp_duration(int(scene.duration_sec), suggested_model),
                                    # Duration was already re-negotiated for the new
                                    # model; resolution was NOT, so a 2K job handed to
                                    # veo-3.1 died on `Invalid resolution: 2k`.
                                    resolution=self._resolution_for_model(job, suggested_model),
                                    aspect_ratio=job.settings.aspect_ratio,
                                    input_references=references if references else None,
                                    frame_images=frame_images,
                                    model=suggested_model,
                                    generate_audio=self._effective_generate_audio(job, suggested_model),
                                    audit_sink=self._video_audit_sink(job, scene.index, f"advisor_switch:{suggested_model}"),
                                ),
                                timeout_sec=TIMEOUT_VIDEO,
                                description=f"Scene {scene.index} with {suggested_model}",
                            )
                        except Exception as switch_e:
                            self._warn(job, f"Alternative model {suggested_model} also failed: {_describe_exc(switch_e)}")
                            self._refund_if_rejected(job, _switch_receipt, switch_e)
                            _unknown = self._paid_outcome_unknown(switch_e, suggested_model)
                            if _unknown is not None:
                                self._record_unretrieved(
                                    job, scene.index, f"advisor_switch:{suggested_model}", _unknown,
                                )
                            video_path = None
                    else:
                        video_path = None

                    if video_path:
                        verify_result = await self._verify_video_multidim(job, video_path, scene, storyboard)
                        stats = job.progress.verification_stats
                        _rank = (
                            1 if verify_result.get("passed", False) else 0,
                            float(verify_result.get("weighted_score") or 0.0),
                        )
                        if _rank > best_video_rank:
                            best_video_rank, best_video_path = _rank, video_path
                        _verdict = verify_result.get(
                            "verdict", "pass" if verify_result.get("passed", False) else "fail"
                        )
                        if _verdict == "indeterminate":
                            self._record_indeterminate_scene(job, scene.index)
                            logger.info(
                                f"Video scene {scene.index} advisor fallback verification "
                                f"indeterminate ({verify_result.get('reason', '?')}); clip kept UNVERIFIED"
                            )
                        elif _verdict == "pass":
                            stats["video_passed"] = stats.get("video_passed", 0) + 1
                            logger.info(f"Video scene {scene.index} advisor fallback passed VLM score={verify_result.get('weighted_score', '?')}")
                        else:
                            stats["video_retried"] = stats.get("video_retried", 0) + 1
                            logger.warning(f"Video scene {scene.index} advisor fallback failed VLM: {verify_result.get('issues', [])}")
                            self._warn(job, "Advisor fallback video failed quality check, using as-is")

                    break

            # A clip that generated successfully is never lost because a LATER attempt
            # failed. Without this, a valid scene clip sat on disk while the final video
            # shipped without that scene entirely.
            if not video_path and best_video_path:
                stats = job.progress.verification_stats
                stats["clips_recovered"] = stats.get("clips_recovered", 0) + 1
                self._warn(
                    job,
                    f"Scene {scene.index}: later attempts failed, recovering the best clip "
                    f"already generated ({Path(best_video_path).name}, "
                    f"passed={bool(best_video_rank[0])} score={best_video_rank[1]})",
                )
                video_path = best_video_path

            if video_path:
                scene.video_url = video_path
                job.progress.video_clips.append(video_path)

                # HARD identity gate (owner-chosen judge panel). fail -> the scene is
                # a continuity-regeneration candidate (phase 2B owns the regeneration
                # budget; no second regen loop here); indeterminate -> ships UNVERIFIED.
                panel = await self._judge_identity_panel(job, video_path, scene, storyboard)
                panel_v = panel.get("verdict")
                stats = job.progress.verification_stats
                if panel_v == "fail":
                    candidates = stats.setdefault("identity_panel_regen_candidates", [])
                    if scene.index not in candidates:
                        candidates.append(scene.index)
                    self._warn(
                        job,
                        f"Identity panel FAILED scene {scene.index}: {panel.get('reason', '?')} "
                        f"— marked as continuity-regeneration candidate",
                    )
                elif panel_v == "indeterminate":
                    self._record_indeterminate_scene(job, scene.index)

                prev_frame_path = self._extract_last_frame(video_path, scene.index)
                scene.prev_frame_url = prev_frame_path
            else:
                prev_frame_path = None

            base = 55.0
            increment = 35.0 / max(1, len(storyboard.scenes))
            job.progress.progress_pct = base + increment * (i + 1)
            job.progress.message = f"Animated scene {i + 1}/{len(storyboard.scenes)} (multidim scoring)"
            self._emit(job)

    # ─── Regenerate Single Scene (for cross-scene identity fix) ─────

    async def _regenerate_single_scene(
        self, job: Job, storyboard: Storyboard, scene_idx: int, critique: str = ""
    ) -> bool:
        """Regenerate a single scene that failed cross-scene identity check.

        `critique` is the reviewer's own drift description; it is fed back into the
        prompt because re-running an identical prompt has no reason to drift less.

        Returns False, without any paid call, when an earlier dispatch for this
        scene was accepted or may have been and its clip never arrived (the
        `unretrieved_accepted_jobs` record): another paid clip could pay twice.
        The scene keeps its current clip. Returns True otherwise.
        """
        scene = storyboard.scenes[scene_idx]
        blocked = [
            r for r in (job.progress.verification_stats.get("unretrieved_accepted_jobs") or [])
            if isinstance(r, dict) and r.get("scene") == scene.index
        ]
        if blocked:
            skipped = job.progress.verification_stats.setdefault(
                "regeneration_skipped_unretrieved", []
            )
            if scene.index not in skipped:
                skipped.append(scene.index)
            ids = ", ".join(str(r.get("job_id") or "unknown") for r in blocked)
            self._warn(
                job,
                f"Scene {scene.index} regeneration skipped: an earlier dispatch for this "
                f"scene was accepted or may have been (job {ids}) and its clip was not "
                f"retrieved; no new paid clip, the current clip stays.",
            )
            return False
        chars_identity_block = self._build_characters_identity_block(storyboard)
        self._locations_by_name = {l.name: l for l in storyboard.locations}

        # Same single reference seam as the main path: the labels in the prompt
        # describe exactly the reference set submitted with THIS request.
        scene_char_objs = [
            c for name in scene.characters for c in storyboard.characters if c.name == name
        ]
        prev_frame_url = None
        if scene_idx > 0:
            prev_frame_url = storyboard.scenes[scene_idx - 1].prev_frame_url
        assembled = self._assemble_scene_references(
            scene, scene_char_objs, prev_frame_url, VIDEO_MAX_INPUT_REFERENCES,
        )
        self._record_reference_state(job, scene.index, assembled)
        references = assembled["references"]
        frame_images = assembled["frame_images"] or None

        prompt = build_video_prompt(
            scene_description=scene.description,
            reference_block=build_reference_block(assembled["labels"]),
            characters_identity_block=chars_identity_block,
            camera_direction=scene.camera_direction,
            mood=scene.mood, style=storyboard.style,
            duration_sec=int(scene.duration_sec),
            continuity_note=self._identity_continuity_note(critique),
        )

        _regen_receipt = None
        try:
            if job.settings.include_dialogue and scene.dialogue:
                prompt += (
                    "\n\nSPOKEN DIALOGUE (characters MUST speak this aloud with clear "
                    "native audio / speech — not silent mime, not subtitles):\n"
                    f"{scene.dialogue}"
                )
            # Same LLM condensation as the main and advisor paths. Without it an
            # over-long prompt met only the dispatch-time fit, which keeps the
            # head and the @ImageN lines but can cut the reviewer's critique, so
            # the paid regeneration reran an essentially unchanged prompt.
            prompt = await self._condense_prompt_for_model(prompt, job.settings.video_model)
            _regen_receipt = self._charge(
                job, "video_clip", note=f"scene {scene.index} continuity regeneration"
            )
            if not _regen_receipt:
                # Regeneration is an improvement pass; refusal keeps the
                # existing clip rather than destroying a delivered scene.
                self._warn(job, f"Scene {scene.index} continuity regeneration skipped: budget hard stop")
                return True
            video_path = await run_with_timeout(
                self.client.generate_video(
                    prompt=prompt,
                    filename=f"scene_{scene.index}_regen.mp4",
                    duration=self._clamp_duration(int(scene.duration_sec), job.settings.video_model),
                    resolution=job.settings.resolution,
                    aspect_ratio=job.settings.aspect_ratio,
                    input_references=references if references else None,
                    frame_images=frame_images,
                    model=job.settings.video_model,
                    generate_audio=self._effective_generate_audio(job),
                    audit_sink=self._video_audit_sink(job, scene.index, "continuity_regen"),
                ),
                timeout_sec=TIMEOUT_VIDEO,
                description=f"Regenerate scene {scene.index} (identity fix)",
            )
            scene.video_url = video_path
            for idx, clip in enumerate(job.progress.video_clips):
                if f"scene_{scene.index}_" in clip:
                    job.progress.video_clips[idx] = video_path
                    break
            logger.info(f"Regenerated scene {scene.index} for identity consistency")
        except Exception as e:
            logger.exception("Scene %s regeneration failed", scene.index)
            self._warn(job, f"Scene {scene.index} regeneration failed: {_describe_exc(e)}")
            self._refund_if_rejected(job, _regen_receipt, e)
            _unknown = self._paid_outcome_unknown(e, job.settings.video_model)
            if _unknown is not None:
                self._record_unretrieved(job, scene.index, "continuity_regen", _unknown)
        return True

    # ─── Phase 4: Assembly ──────────────────────────────────────────

    async def _assemble(self, job: Job, storyboard: Storyboard) -> dict:
        """Assemble final video from clips using ffmpeg.

        Returns a dict naming exactly what was delivered. Missing scenes are
        RECORDED, never silently filtered: a 45s request once shipped as 30.18s
        with status DONE because absent clips were dropped without a trace.
        """
        output_dir = self.state_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{job.job_id}_final.mp4"

        clips = []
        missing_scenes: list[int] = []
        for scene in storyboard.scenes:
            if scene.video_url and Path(scene.video_url).exists():
                clips.append(scene.video_url)
            else:
                missing_scenes.append(scene.index)
        if missing_scenes:
            self._warn(
                job,
                f"Final assembly is MISSING scene(s) {missing_scenes}: no video "
                f"clip exists for them; the delivered video is shorter than requested",
            )
            # STRUCTURAL, not just prose. A live job shipped missing_scenes=[1]
            # with partial_reasons=[] — the warning text carried the cause while
            # the machine-readable field said the result was whole, so every
            # consumer that reads the structure (widget, tool result, tests)
            # saw a clean delivery. A constant key: the indexes already live in
            # `missing_scenes`, and the owner notification keeps only reason
            # keys matching ^[a-z0-9_:]+$, so "missing_scenes:1,3" was dropped.
            if "missing_scenes" not in job.progress.partial_reasons:
                job.progress.partial_reasons.append("missing_scenes")

        if not clips:
            raise RuntimeError(self._no_clips_error(job))

        if len(clips) == 1:
            shutil.copy2(clips[0], output_path)
        else:
            # Concat demuxer + `-c copy` never reconciles clip parameters. With
            # mixed geometry the output changes resolution mid-stream (reproduced
            # offline with ffmpeg 8.1), and one live job with 1080x1080 /
            # 1440x1440 / 2560x1440 clips was reported to decode as garbage
            # ("sps_id 32 out of range") while ffmpeg returned rc=0, so the
            # re-encode fallback below never fired. Normalize to one
            # geometry/audio shape FIRST; only then is stream copy sound.
            clips = self._normalize_clips_for_concat(job, clips)

            concat_file = self.state_dir / "concat.txt"
            with open(concat_file, "w") as f:
                for clip in clips:
                    f.write(f"file '{clip}'\n")

            cmd = [
                self._ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_file), "-c", "copy", str(output_path),
            ]
            proc = self._run_ffmpeg(cmd, timeout=120)

            if proc.returncode != 0:
                self._check_shutdown()
                cmd_reencode = [
                    self._ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat_file),
                    "-c:v", "libx264", "-preset", "fast", "-c:a", "aac",
                    str(output_path),
                ]
                proc = self._run_ffmpeg(cmd_reencode, timeout=300)
                if proc.returncode != 0:
                    raise RuntimeError(f"ffmpeg assembly failed: {proc.stderr[-500:]}")

        # Always attempt music bed after concat OR single-clip copy.
        # (Previously single-clip returned early and never mixed music.)
        music_clips = [
            mc.audio_url for mc in storyboard.music_cues
            if mc.audio_url and Path(mc.audio_url).exists()
        ]
        if music_clips:
            self._check_shutdown()
            await self._mix_audio(job, output_path, music_clips)

        # A return code is not a playable file. Decode the FINAL artifact (after
        # the music mix, which rewrites it) and record the verdict, so "assembled"
        # can never again mean "ffmpeg exited 0 over an unplayable MP4".
        decode_ok, decode_detail = self._decode_check(output_path)
        if not decode_ok:
            self._warn(
                job,
                f"Final video failed a full decode check ({decode_detail}); "
                f"delivering it anyway — it is the only artifact — but it may not play",
            )
            if "final_decode_failed" not in job.progress.partial_reasons:
                job.progress.partial_reasons.append("final_decode_failed")

        logger.info(f"Final video assembled: {output_path}")
        return {
            "path": str(output_path),
            "clips_used": len(clips),
            "scenes_total": len(storyboard.scenes),
            "missing_scenes": missing_scenes,
            "transitions": "concat",
            "decode_verified": bool(decode_ok),
            "decode_detail": decode_detail,
        }

    _RESOLUTION_LADDER = ["4K", "2K", "1080p", "720p", "480p"]

    def _resolution_for_model(self, job: Job, model: str) -> str:
        """The job's resolution reconciled against *model*'s own capabilities.

        Read-only: it returns a value and never mutates `job.settings`, because a
        one-scene recovery attempt on another model must not silently redefine the
        resolution of every remaining scene.

        Duration was already re-negotiated per model by `_clamp_duration`;
        resolution was not, so an advisor switch carried a 2K job (H3's only
        resolution) to google/veo-3.1 and got `Invalid resolution: 2k`
        (INVALID_ARGUMENT) — a recovery attempt that could not possibly succeed.
        An unknown/empty capability list keeps the requested value: absence of
        evidence is not evidence of non-support.
        """
        requested = job.settings.resolution
        supported = ((self._video_caps or {}).get(model) or {}).get("resolutions") or []
        if not supported or requested in supported:
            return requested
        rank = (
            self._RESOLUTION_LADDER.index(requested)
            if requested in self._RESOLUTION_LADDER else 0
        )
        chosen = next(
            (r for r in self._RESOLUTION_LADDER[rank:] if r in supported), None
        ) or next((r for r in self._RESOLUTION_LADDER if r in supported), None)
        if not chosen:
            # Nothing in common: send the requested value and let the provider
            # answer. Inventing a resolution the model never advertised would be
            # a guess dressed as reconciliation.
            self._warn(
                job,
                f"{model} advertises resolutions {supported}, none of them known; "
                f"sending {requested} unchanged",
            )
            return requested
        if chosen != requested:
            self._warn(
                job,
                f"Recovery model {model} does not support {requested}; that attempt "
                f"uses {chosen} (supported: {', '.join(supported)})",
            )
        return chosen

    def _probe_video_geometry(self, clip: str) -> dict:
        """Width/height/audio-presence of one clip. Unknown fields stay None."""
        probe = getattr(self, "_ffprobe_path", None) or "ffprobe"
        out = {"width": None, "height": None, "has_audio": None}
        try:
            proc = self._run_ffmpeg(
                [
                    probe, "-v", "error",
                    "-show_entries", "stream=codec_type,width,height",
                    "-of", "json", str(clip),
                ],
                timeout=30,
                capture_stdout=True,
            )
            if proc.returncode != 0:
                return out
            streams = (json.loads(proc.stdout or "{}") or {}).get("streams") or []
        except Exception:
            return out
        has_audio = False
        for st in streams:
            if not isinstance(st, dict):
                continue
            if st.get("codec_type") == "video" and out["width"] is None:
                w, h = st.get("width"), st.get("height")
                if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                    out["width"], out["height"] = w, h
            elif st.get("codec_type") == "audio":
                has_audio = True
        out["has_audio"] = has_audio
        return out

    def _normalize_clips_for_concat(self, job: Job, clips: list[str]) -> list[str]:
        """Re-encode clips to ONE geometry + audio shape when they disagree.

        Returns the original list when every clip already agrees (the common
        case: one model, one resolution) so a homogeneous job pays nothing. An
        UNKNOWN geometry counts as disagreement — guessing that an unprobeable
        clip matches is exactly how the corrupt-concat defect stayed invisible.
        A per-clip re-encode failure keeps that clip's ORIGINAL file rather than
        dropping a paid scene.
        """
        geo = [self._probe_video_geometry(c) for c in clips]
        sizes = {(g["width"], g["height"]) for g in geo}
        audio_flags = {g["has_audio"] for g in geo}
        unknown = any(g["width"] is None for g in geo)
        if not unknown and len(sizes) == 1 and len(audio_flags) == 1:
            return clips

        widths = [g["width"] for g in geo if g["width"]]
        heights = [g["height"] for g in geo if g["height"]]
        target_w = max(widths) if widths else 1920
        target_h = max(heights) if heights else 1080
        # H.264 needs even dimensions.
        target_w += target_w % 2
        target_h += target_h % 2
        self._warn(
            job,
            f"Clips disagree on geometry/audio ({sorted(str(s) for s in sizes)}, "
            f"audio={sorted(str(a) for a in audio_flags)}"
            f"{', some unprobeable' if unknown else ''}); normalizing to "
            f"{target_w}x{target_h} before concat so the final file decodes",
        )

        norm_dir = self.state_dir / "normalized"
        norm_dir.mkdir(parents=True, exist_ok=True)
        out_clips: list[str] = []
        for idx, clip in enumerate(clips):
            self._check_shutdown()
            # Each re-encode can take minutes; the progress write doubles as the
            # worker's lease heartbeat, so a long assembly does not look abandoned.
            job.progress.message = (
                f"Normalizing clip {idx + 1}/{len(clips)} to {target_w}x{target_h} before concat..."
            )
            self._emit(job)
            dest = norm_dir / f"norm_{idx:02d}.mp4"
            vf = (
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
                f"fps=24,format=yuv420p"
            )
            cmd = [self._ffmpeg_path, "-y", "-i", str(clip)]
            silent = geo[idx]["has_audio"] is False
            if silent:
                # Silent clip: give it a real silent track so every concat input
                # has the same stream layout.
                cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            cmd += [
                "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "aac", "-ar", "48000", "-ac", "2",
            ]
            if silent:
                # Only here: the synthetic track is endless and must stop with
                # the picture. A clip's OWN audio is never allowed to cut its video.
                cmd += ["-shortest"]
            cmd += ["-movflags", "+faststart", str(dest)]
            try:
                proc = self._run_ffmpeg(cmd, timeout=300)
            except Exception as exc:
                self._warn(job, f"Normalizing clip {idx} raised {_describe_exc(exc)}; using original")
                out_clips.append(clip)
                continue
            if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
                self._warn(
                    job,
                    f"Normalizing clip {idx} failed (rc={proc.returncode}); using original",
                )
                out_clips.append(clip)
                continue
            out_clips.append(str(dest))

        # Promise one shape only after checking it. A failed re-encode keeps
        # the paid original, and an unprobeable clip may lack the audio track
        # the others have; stream-copy concat of such a set can still decode
        # while changing shape mid-file, so the result is PARTIAL, not whole.
        after = [self._probe_video_geometry(c) for c in out_clips]
        unknown_after = any(g["width"] is None or g["has_audio"] is None for g in after)
        if (unknown_after or len({(g["width"], g["height"]) for g in after}) != 1
                or len({g["has_audio"] for g in after}) != 1):
            self._warn(
                job,
                "Clips still disagree on geometry/audio after normalization "
                f"({[(g['width'], g['height'], g['has_audio']) for g in after]}); "
                "the paid originals are kept and concatenated, but the final file "
                "may change shape mid-stream",
            )
            if "concat_layout_unverified" not in job.progress.partial_reasons:
                job.progress.partial_reasons.append("concat_layout_unverified")
        return out_clips

    def _decode_check(self, video_path) -> tuple[bool, str]:
        """Fully decode the picture of *video_path*; (ok, detail).

        The corrupt-concat defect lives in the video stream, so only that is
        decoded: a cosmetic audio decoder message must not mark a playable video
        partial. A probe failure is NOT ok.
        """
        try:
            proc = self._run_ffmpeg(
                [
                    self._ffmpeg_path, "-v", "error", "-xerror",
                    "-i", str(video_path), "-map", "0:v:0", "-f", "null", "-",
                ],
                timeout=300,
            )
        except Exception as exc:
            return False, f"decode probe raised {_describe_exc(exc)}"
        if proc.returncode != 0:
            return False, f"rc={proc.returncode}: {(proc.stderr or '')[-300:]}"
        return True, "full decode clean"

    def _probe_audio_streams(self, video_path) -> tuple[str, str]:
        """Structural audio probe: ("present"|"absent"|"unknown", detail).

        Replaces a substring probe over combined stdout+stderr that returned
        False on ANY exception — and False routed the mix into the music-only
        branch, silently destroying the scenes' native spoken dialogue. A probe
        FAILURE is an ERROR condition ("unknown"), never evidence of silence.
        """
        probe = getattr(self, "_ffprobe_path", None) or "ffprobe"
        try:
            # Route through _run_ffmpeg (it handles ffmpeg AND ffprobe) so this probe
            # gets the same PATH-only scrubbed environment and tracked-child cleanup.
            # A bare subprocess.run here let ffprobe inherit the companion's
            # OPENROUTER_API_KEY and HOST_SERVICE_TOKEN, which it has no use for.
            proc = self._run_ffmpeg(
                [
                    probe,
                    "-v", "error",
                    "-select_streams", "a",
                    "-show_entries", "stream=index,codec_type",
                    "-of", "json",
                    str(video_path),
                ],
                timeout=30,
                capture_stdout=True,
            )
        except Exception as exc:
            return "unknown", _describe_exc(exc)
        if proc.returncode != 0:
            return "unknown", f"ffprobe rc={proc.returncode}: {(proc.stderr or '')[-300:]}"
        try:
            data = json.loads(proc.stdout or "")
            streams = data.get("streams")
            if not isinstance(streams, list):
                raise ValueError("ffprobe JSON carries no 'streams' list")
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
            return "unknown", f"unparseable ffprobe JSON: {_describe_exc(exc)}"
        if streams:
            return "present", f"{len(streams)} audio stream(s)"
        return "absent", "no audio streams reported"

    async def _mix_audio(self, job: Job, video_path, music_clips: list[str]):
        """Mix music bed with video. If video is silent, attach music alone.

        Previous filter assumed [0:a] always exists — silent Seedance clips
        (generate_audio=False) made amix fail, so finals stayed mute.
        """
        music_concat = self.state_dir / "music_concat.txt"
        with open(music_concat, "w") as f:
            for clip in music_clips:
                f.write(f"file '{clip}'\n")

        # Container/codec must MATCH the inputs. `generate_music` asks OpenRouter
        # for WAV and wraps raw PCM as WAV, so the clips on disk are pcm_s16le
        # .wav files. The old `-c copy` into an .mp3 container asked the MP3
        # muxer to carry a PCM stream, which ffmpeg rejects — so on the normal
        # path this concat ALWAYS failed and the music that had already been
        # generated and paid for never reached the video. Re-encoding to AAC in
        # an .m4a container accepts the real inputs instead of assuming MP3.
        music_merged = self.state_dir / "music_merged.m4a"
        concat_proc = self._run_ffmpeg(
            [self._ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
             "-i", str(music_concat), "-c:a", "aac", "-b:a", "192k", str(music_merged)],
            timeout=120,
        )

        # Inspect the return code, not just file existence: a partially written
        # music_merged.mp3 from a FAILED concat would otherwise be mixed in as if
        # it were valid, and the failure left no diagnostic at all.
        # A log line is not a job diagnostic: both of these paths mean the music
        # that was already GENERATED AND PAID FOR does not reach the final file,
        # so the job itself must carry the warning, not just the worker's stderr.
        if concat_proc.returncode != 0:
            logger.warning(
                "Music concat failed (rc=%s); skipping the music mix. stderr: %s",
                concat_proc.returncode, (concat_proc.stderr or "")[-800:],
            )
            self._warn(
                job,
                f"Music concat FAILED (rc={concat_proc.returncode}); the video ships "
                f"WITHOUT the generated music bed. stderr tail: "
                f"{(concat_proc.stderr or '')[-400:]}",
            )
            job.progress.verification_stats["music_mix_failed"] = True
            return

        if not music_merged.exists():
            logger.warning("Music concat reported success but produced no file; skipping the music mix")
            self._warn(
                job,
                "Music concat reported success but produced no file; the video ships "
                "WITHOUT the generated music bed",
            )
            job.progress.verification_stats["music_mix_failed"] = True
            return

        self._check_shutdown()

        video_path = Path(video_path)
        temp_output = video_path.with_suffix(".tmp.mp4")
        audio_state, audio_detail = self._probe_audio_streams(video_path)

        amix_cmd = [
            self._ffmpeg_path, "-y",
            "-i", str(video_path),
            "-i", str(music_merged),
            "-filter_complex",
            # duration=longest + apad + -shortest ends the output at the PICTURE.
            # duration=first ended the mix with the clips' own audio, and
            # -shortest then cut the video to it, dropping the end of the last
            # scene whenever its native audio was shorter than its picture.
            "[0:a]volume=1.0[va];[1:a]volume=0.35[ma];"
            "[va][ma]amix=inputs=2:duration=longest:dropout_transition=2,apad[out]",
            "-map", "0:v", "-map", "[out]",
            "-c:v", "copy", "-c:a", "aac", "-shortest",
            str(temp_output),
        ]
        # Silent video: mux music under the picture (no [0:a] to amix).
        music_only_cmd = [
            self._ffmpeg_path, "-y",
            "-i", str(video_path),
            "-i", str(music_merged),
            # apad + -shortest ends the output at the PICTURE, not at the music.
            # Bare -shortest with a music track shorter than the video silently
            # discarded the remaining paid scenes.
            "-filter_complex", "[1:a]apad[ma]",
            "-map", "0:v", "-map", "[ma]",
            "-c:v", "copy", "-c:a", "aac", "-shortest",
            str(temp_output),
        ]

        if audio_state == "present":
            attempts = [("amix", amix_cmd)]
        elif audio_state == "absent":
            attempts = [("music_only", music_only_cmd)]
        else:
            # A failed probe must NEVER pick the dialogue-destroying branch:
            # attempt the dialogue-preserving amix FIRST, fall back only if it fails.
            self._warn(
                job,
                f"Audio probe FAILED ({audio_detail}); attempting the "
                f"dialogue-preserving mix first — the music-only branch would "
                f"destroy any native spoken dialogue",
            )
            attempts = [("amix", amix_cmd), ("music_only", music_only_cmd)]

        proc = None
        for n, (label, cmd) in enumerate(attempts):
            proc = self._run_ffmpeg(cmd, timeout=120)
            if proc.returncode == 0 and temp_output.exists():
                temp_output.replace(video_path)
                return
            if n + 1 < len(attempts):
                self._warn(
                    job,
                    f"Dialogue-preserving amix failed on unknown probe "
                    f"(rc={proc.returncode}); falling back to the music-only mix",
                )
        # Previously a failed final mix skipped temp_output.replace silently,
        # losing successfully generated music with NO warning at all.
        rc = proc.returncode if proc is not None else "n/a"
        tail = (proc.stderr or "")[-500:] if proc is not None else ""
        self._warn(
            job,
            f"Final music mix FAILED (rc={rc}); the video ships WITHOUT the "
            f"generated music bed. stderr tail: {tail}",
        )
        job.progress.verification_stats["music_mix_failed"] = True
