"""Data models for Anime Studio pipeline."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


def _known_value(enum_cls, raw) -> bool:
    if isinstance(raw, enum_cls):
        return True
    try:
        enum_cls(raw)
    except ValueError:
        return False
    return True


def unsupported_stored_state(progress_data) -> Optional[dict]:
    """The raw phase/status of a stored record when this version cannot run it.

    A job.json written by a NEWER payload version can carry a phase or status
    this version has never heard of, and a record without them is equally
    opaque. Returns None for an ordinary record, else the original values.

    Such a record is TERMINAL for this version as a whole: phase AND status
    read as ERROR. Mapping them independently left `status="queued"` beside an
    unknown phase, and the worker, which selects jobs by status, re-ran the
    whole paid pipeline for it. Every consumer (status route, worker claim and
    stale recovery) reads this one answer, and nothing rewrites the record.
    """
    data = progress_data if isinstance(progress_data, dict) else {}
    phase, status = data.get("phase"), data.get("status")
    if _known_value(JobPhase, phase) and _known_value(JobStatus, status):
        return None
    return {"phase": phase, "status": status}


def _fields_of(cls, data: dict) -> dict:
    """Keep only the keys `cls` actually declares.

    ONE helper rather than an inline comprehension per call site: the previous
    shape filtered Scene and GenerationSettings but built Character, Location and
    MusicCue from raw dicts, so one extra key in a job.json written by another
    payload version raised TypeError and an existing job read back as "not
    found". A single helper cannot be applied to three of four sites by accident.
    """
    if not isinstance(data, dict):
        return {}
    allowed = cls.__dataclass_fields__
    return {k: v for k, v in data.items() if k in allowed}


class JobPhase(str, Enum):
    QUEUED = "queued"
    SCENARIO = "scenario"
    ASSETS = "assets"
    VERIFICATION = "verification"
    MUSIC = "music"
    ANIMATION = "animation"
    ASSEMBLY = "assembly"
    DONE = "done"
    ERROR = "error"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    PARTIAL = "partial"  # finished with some scenes missing/unverified
    ERROR = "error"


@dataclass
class VerificationResult:
    """Result of VLM verification of a generated asset."""
    passed: bool = False
    issues: list[str] = field(default_factory=list)
    suggestion: str = ""
    retry_count: int = 0


@dataclass
class Character:
    name: str
    description: str
    visual_traits: str  # detailed visual description for image gen
    sheet_url: Optional[str] = None  # generated character sheet image URL


@dataclass
class Location:
    name: str
    description: str
    visual_traits: str
    art_url: Optional[str] = None  # generated location concept art URL


@dataclass
class Scene:
    index: int
    description: str
    duration_sec: float  # 4-15 seconds
    characters: list[str]  # character names involved
    location: str  # location name
    camera_direction: str  # e.g. "slow dolly forward", "close-up pan"
    dialogue: Optional[str] = None  # spoken dialogue text
    mood: str = "neutral"
    transition_from: Optional[str] = None  # how this scene connects from previous
    causal_link: Optional[str] = None  # BECAUSE OF THAT / BUT — why this scene follows
    keyframe_url: Optional[str] = None  # generated keyframe image
    video_url: Optional[str] = None  # generated video clip URL
    audio_url: Optional[str] = None  # scene audio (TTS dialogue)
    prev_frame_url: Optional[str] = None  # last frame of previous scene for continuity


@dataclass
class MusicCue:
    segment_index: int
    mood: str
    tempo: str  # "slow", "medium", "fast"
    style: str  # "orchestral", "electronic", "acoustic", etc.
    duration_sec: float
    description: str  # text description for music gen
    audio_url: Optional[str] = None  # generated music clip URL


@dataclass
class Storyboard:
    title: str
    synopsis: str
    style: str  # anime sub-style
    total_duration_sec: float
    characters: list[Character] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)
    scenes: list[Scene] = field(default_factory=list)
    music_cues: list[MusicCue] = field(default_factory=list)


@dataclass
class GenerationSettings:
    theme: str = ""
    style: str = "modern anime"  # anime sub-style
    duration_sec: float = 30.0  # total target duration
    num_scenes: int = 4
    mood: str = "adventurous"
    resolution: str = "720p"
    aspect_ratio: str = "16:9"
    video_model: str = "bytedance/seedance-2.0"
    image_model: str = "gpt-image-2"  # "gpt-image-2" or "nanobanana"
    include_dialogue: bool = True
    include_music: bool = True
    music_style: str = "orchestral"
    quality_mode: str = "medium"  # see modes.QUALITY_MODES
    budget_limit_usd: float = 0.0  # 0 = auto (estimate * headroom)


@dataclass
class JobProgress:
    phase: JobPhase = JobPhase.QUEUED
    status: JobStatus = JobStatus.QUEUED
    progress_pct: float = 0.0
    message: str = ""
    # Intermediate results
    storyboard: Optional[Storyboard] = None
    character_sheets: list[str] = field(default_factory=list)
    location_arts: list[str] = field(default_factory=list)
    keyframes: list[str] = field(default_factory=list)
    music_clips: list[str] = field(default_factory=list)
    video_clips: list[str] = field(default_factory=list)
    final_video_url: Optional[str] = None
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)  # non-fatal asset failures
    verification_stats: dict = field(default_factory=dict)  # {passed: N, retried: N, failed: N}
    budget: dict = field(default_factory=dict)  # BudgetLedger.to_dict() snapshot / preflight estimate
    missing_scenes: list[int] = field(default_factory=list)  # scene indexes with no video clip
    unverified_scenes: list[int] = field(default_factory=list)  # scene indexes shipped without a passing verify
    partial_reasons: list[str] = field(default_factory=list)  # why status == partial


@dataclass
class Job:
    job_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    settings: GenerationSettings = field(default_factory=GenerationSettings)
    progress: JobProgress = field(default_factory=JobProgress)
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        settings_data = data.get("settings", {})
        # Handle unknown fields gracefully (same helper as every nested record)
        settings = GenerationSettings(**_fields_of(GenerationSettings, settings_data))

        progress_data = data.get("progress", {})
        # Reconstruct nested objects
        storyboard = None
        if progress_data.get("storyboard"):
            sb = progress_data["storyboard"]
            storyboard = Storyboard(
                title=sb.get("title", ""),
                synopsis=sb.get("synopsis", ""),
                style=sb.get("style", ""),
                total_duration_sec=sb.get("total_duration_sec", 0),
                # EVERY nested record is field-filtered, not just Scene. A
                # persisted job.json written by a different payload version — which
                # this marketplace skill explicitly expects after a hub overwrite —
                # carries extra keys; raw `Character(**c)` then raises TypeError,
                # plugin._load_job swallows it and reports an existing job as "not
                # found", and the worker's stale-job recovery leaves it stuck
                # forever. Forward/backward tolerance belongs on all of them.
                characters=[Character(**_fields_of(Character, c)) for c in sb.get("characters", [])],
                locations=[Location(**_fields_of(Location, loc)) for loc in sb.get("locations", [])],
                scenes=[Scene(**_fields_of(Scene, s)) for s in sb.get("scenes", [])],
                music_cues=[MusicCue(**_fields_of(MusicCue, m)) for m in sb.get("music_cues", [])],
            )
        unsupported = unsupported_stored_state(progress_data)
        verification_stats = progress_data.get("verification_stats", {})
        error = progress_data.get("error")
        if unsupported is None:
            phase = JobPhase(progress_data.get("phase"))
            status = JobStatus(progress_data.get("status"))
        else:
            phase, status = JobPhase.ERROR, JobStatus.ERROR
            note = (
                f"Stored job state phase={unsupported['phase']!r}, "
                f"status={unsupported['status']!r} is not supported by this payload "
                f"version; the record is shown as ERROR and left untouched."
            )
            error = f"{note} Stored error: {error}" if error else note
            verification_stats = dict(verification_stats or {})
            verification_stats["unsupported_stored_state"] = unsupported
        progress = JobProgress(
            phase=phase,
            status=status,
            progress_pct=progress_data.get("progress_pct", 0.0),
            message=progress_data.get("message", ""),
            storyboard=storyboard,
            character_sheets=progress_data.get("character_sheets", []),
            location_arts=progress_data.get("location_arts", []),
            keyframes=progress_data.get("keyframes", []),
            music_clips=progress_data.get("music_clips", []),
            video_clips=progress_data.get("video_clips", []),
            final_video_url=progress_data.get("final_video_url"),
            error=error,
            warnings=progress_data.get("warnings", []),
            verification_stats=verification_stats,
            budget=progress_data.get("budget", {}),
            missing_scenes=progress_data.get("missing_scenes", []),
            unverified_scenes=progress_data.get("unverified_scenes", []),
            partial_reasons=progress_data.get("partial_reasons", []),
        )
        return cls(
            job_id=data.get("job_id", str(uuid.uuid4())[:8]),
            settings=settings,
            progress=progress,
            created_at=data.get("created_at", ""),
        )
