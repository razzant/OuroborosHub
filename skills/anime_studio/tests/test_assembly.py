"""Assembly reports what it delivered: mixed clips are normalized before the
stream-copy concat, the final picture is decoded, and missing scenes or an
undecodable file make the result partial. Real-ffmpeg cases use synthetic
lavfi clips in the test's own temporary directory and skip without ffmpeg."""
from __future__ import annotations

import asyncio
import functools
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import anime_studio.host_bridge as host_bridge
import anime_studio.pipeline as pl
from anime_studio.models import GenerationSettings, Job, MusicCue, Scene, Storyboard


@functools.lru_cache(maxsize=1)
def _ffmpeg_ready() -> bool:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return False
    try:
        encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                                  text=True, timeout=30).stdout
    except Exception:
        return False
    return " libx264 " in encoders and " aac " in encoders


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg/ffprobe with libx264 and aac not on PATH")


def _make_clip(path, size, *, audio: bool, seconds: float = 1.0):
    cmd = [shutil.which("ffmpeg"), "-v", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return str(path)


def _probe(path) -> dict:
    out = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-show_entries",
         "stream=codec_type,width,height", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    return json.loads(out)


def _frame_sizes(path) -> set:
    out = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "frame=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=120,
    ).stdout
    sizes = set()
    for line in out.splitlines():
        fields = [f for f in line.strip().split(",") if f]
        if len(fields) >= 2:
            sizes.add(f"{fields[0]},{fields[1]}")
    return sizes


def _decodes_cleanly(path) -> bool:
    proc = subprocess.run(
        [shutil.which("ffmpeg"), "-v", "error", "-xerror", "-i", str(path),
         "-map", "0:v:0", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )
    return proc.returncode == 0 and not proc.stderr.strip()


def _storyboard(clips):
    scenes = [
        Scene(index=i, description=f"scene {i}", duration_sec=1, characters=[],
              location="nowhere", camera_direction="static", video_url=clip)
        for i, clip in enumerate(clips, start=1)
    ]
    return Storyboard(title="t", synopsis="s", style="modern anime",
                      total_duration_sec=float(len(scenes)), scenes=scenes)


def _pipeline(tmp_path, progress=None):
    state = tmp_path / "job"
    state.mkdir()
    pipe = pl.Pipeline(client=object(), state_dir=state, on_progress=progress)
    pipe._ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
    pipe._ffprobe_path = shutil.which("ffprobe") or "ffprobe"
    return pipe


def _job():
    return Job(settings=GenerationSettings(theme="t"))


@needs_ffmpeg
def test_mixed_geometry_clips_are_normalized_and_the_final_picture_decodes(tmp_path):
    a = _make_clip(tmp_path / "a.mp4", "320x240", audio=True)
    b = _make_clip(tmp_path / "b.mp4", "480x272", audio=False)
    pipe, job = _pipeline(tmp_path), _job()

    result = asyncio.run(pipe._assemble(job, _storyboard([a, b])))

    out = result["path"]
    # Stream-copy concat of these clips changes the frame size mid-stream.
    assert _frame_sizes(out) == {"480,272"}, "every frame of the final file has one geometry"
    assert _decodes_cleanly(out)
    assert result["decode_verified"] is True, result.get("decode_detail")
    kinds = sorted(s["codec_type"] for s in _probe(out)["streams"])
    assert kinds == ["audio", "video"]
    assert "final_decode_failed" not in job.progress.partial_reasons
    assert any("normalizing to 480x272" in w for w in job.progress.warnings)


@needs_ffmpeg
def test_missing_scenes_reach_the_owner_notification(tmp_path):
    a = _make_clip(tmp_path / "a.mp4", "320x240", audio=True)
    pipe, job = _pipeline(tmp_path), _job()

    result = asyncio.run(pipe._assemble(job, _storyboard([a, None, None])))

    assert result["missing_scenes"] == [2, 3]
    assert result["decode_verified"] is True
    text = host_bridge._build_text(job.job_id, {
        "status": "partial", "mode": "medium", "missing_scenes": 2,
        "partial_reasons": list(job.progress.partial_reasons),
    })
    assert "Partial reasons: missing_scenes" in text, "a comma-joined key was dropped by the filter"


@needs_ffmpeg
def test_undecodable_final_file_is_reported_partial(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is not a video stream " * 200)
    pipe, job = _pipeline(tmp_path), _job()

    result = asyncio.run(pipe._assemble(job, _storyboard([str(broken)])))

    assert result["decode_verified"] is False
    assert "final_decode_failed" in job.progress.partial_reasons
    assert any("failed a full decode check" in w for w in job.progress.warnings)


# ─── Command shape, without running ffmpeg ─────────────────────────────


def _recording_pipeline(tmp_path, geometry, rc=None):
    emitted: list[str] = []
    pipe = _pipeline(tmp_path, progress=lambda job: emitted.append(job.progress.message))
    commands: list[list[str]] = []
    rc = rc or {}

    def run(cmd, timeout=120, capture_stdout=False):
        commands.append(list(cmd))
        dest = cmd[-1]
        index = len(commands) - 1
        code = rc.get(index, 0)
        if code == 0 and dest.endswith(".mp4"):
            with open(dest, "wb") as handle:
                handle.write(b"normalized")
        return SimpleNamespace(returncode=code, stdout="", stderr="" if code == 0 else "boom")

    known = [g for g in geometry.values() if g["width"]]
    normalized = {
        "width": max((g["width"] for g in known), default=1920),
        "height": max((g["height"] for g in known), default=1080),
        "has_audio": True,
    }

    def probe(clip):
        # A re-encoded output has the shape the command asked for.
        return dict(geometry[clip]) if clip in geometry else dict(normalized)

    pipe._run_ffmpeg = run
    pipe._probe_video_geometry = probe
    return pipe, commands, emitted


def test_shortest_is_used_only_with_the_synthetic_silent_track(tmp_path):
    geometry = {
        "with_audio.mp4": {"width": 1280, "height": 720, "has_audio": True},
        "silent.mp4": {"width": 1920, "height": 1080, "has_audio": False},
    }
    pipe, commands, emitted = _recording_pipeline(tmp_path, geometry)

    out = pipe._normalize_clips_for_concat(_job(), list(geometry))

    assert len(commands) == 2 and all(o.endswith(".mp4") and "normalized" in o for o in out)
    own_audio, silent = commands
    assert "-shortest" not in own_audio, "a clip's own audio must never cut its picture"
    assert not any("anullsrc" in part for part in own_audio)
    assert "-shortest" in silent and any("anullsrc" in part for part in silent)
    for cmd in commands:
        vf = cmd[cmd.index("-vf") + 1]
        assert "scale=1920:1080" in vf and "pad=1920:1080" in vf
    assert len(emitted) >= 2, "each re-encode reports progress, which refreshes the lease"


def test_homogeneous_clips_are_left_alone(tmp_path):
    geometry = {
        "one.mp4": {"width": 1920, "height": 1080, "has_audio": True},
        "two.mp4": {"width": 1920, "height": 1080, "has_audio": True},
    }
    pipe, commands, _ = _recording_pipeline(tmp_path, geometry)
    assert pipe._normalize_clips_for_concat(_job(), list(geometry)) == list(geometry)
    assert commands == []


def test_unprobeable_clip_counts_as_disagreement_and_failed_reencode_keeps_the_original(tmp_path):
    geometry = {
        "one.mp4": {"width": 1920, "height": 1080, "has_audio": True},
        "two.mp4": {"width": None, "height": None, "has_audio": None},
    }
    pipe, commands, _ = _recording_pipeline(tmp_path, geometry, rc={1: 1})
    job = _job()

    out = pipe._normalize_clips_for_concat(job, list(geometry))

    assert len(commands) == 2
    assert out[0].endswith("norm_00.mp4")
    assert out[1] == "two.mp4", "a failed re-encode keeps the paid clip"
    assert any("Normalizing clip 1 failed" in w for w in job.progress.warnings)
    assert "concat_layout_unverified" in job.progress.partial_reasons


def test_decode_check_reads_the_video_stream(tmp_path):
    pipe, commands, _ = _recording_pipeline(tmp_path, {})
    ok, detail = pipe._decode_check(tmp_path / "final.mp4")
    assert ok is True
    cmd = commands[0]
    assert cmd[cmd.index("-map") + 1] == "0:v:0"
    assert "-xerror" in cmd



@needs_ffmpeg
def test_failed_normalization_is_reported_partial_and_keeps_the_paid_original(tmp_path):
    a = _make_clip(tmp_path / "a.mp4", "320x240", audio=True)
    b = _make_clip(tmp_path / "b.mp4", "480x272", audio=False)
    pipe, job = _pipeline(tmp_path), _job()
    real_run = pipe._run_ffmpeg

    def run(cmd, timeout=120, capture_stdout=False):
        if str(cmd[-1]).endswith("norm_01.mp4"):
            return SimpleNamespace(returncode=1, stdout="", stderr="synthetic encoder failure")
        return real_run(cmd, timeout=timeout, capture_stdout=capture_stdout)

    pipe._run_ffmpeg = run
    result = asyncio.run(pipe._assemble(job, _storyboard([a, b])))

    assert result["clips_used"] == 2 and (tmp_path / "b.mp4").exists(), "the paid original is kept"
    assert "concat_layout_unverified" in job.progress.partial_reasons, (
        "a mixed concat must not be reported as a whole delivery"
    )


@needs_ffmpeg
def test_unknown_audio_layout_is_not_reported_whole(tmp_path):
    a = _make_clip(tmp_path / "a.mp4", "320x240", audio=True)
    b = _make_clip(tmp_path / "b.mp4", "480x272", audio=False)
    pipe, job = _pipeline(tmp_path), _job()
    real_probe = pipe._probe_video_geometry

    def probe(clip):
        found = real_probe(clip)
        if str(clip) == b:
            found["has_audio"] = None  # the audio probe of this original failed
        return found

    pipe._probe_video_geometry = probe
    asyncio.run(pipe._assemble(job, _storyboard([a, b])))

    assert "concat_layout_unverified" in job.progress.partial_reasons


def _frames(path) -> int:
    out = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True, timeout=120,
    ).stdout
    return int(out.strip().split(",")[0])


@needs_ffmpeg
def test_music_mix_keeps_the_whole_picture_when_the_clip_audio_is_shorter(tmp_path):
    clip = tmp_path / "scene.mp4"
    subprocess.run([
        shutil.which("ffmpeg"), "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip),
    ], check=True, capture_output=True, timeout=120)
    music = tmp_path / "music.wav"
    subprocess.run([
        shutil.which("ffmpeg"), "-v", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=220:duration=3", str(music),
    ], check=True, capture_output=True, timeout=120)
    assert _frames(clip) == 72
    storyboard = _storyboard([str(clip)])
    storyboard.music_cues = [MusicCue(segment_index=0, mood="calm", tempo="slow", style="ambient",
                                      duration_sec=3, description="bed", audio_url=str(music))]
    pipe, job = _pipeline(tmp_path), _job()

    result = asyncio.run(pipe._assemble(job, storyboard))

    assert job.progress.verification_stats.get("music_mix_failed") is None
    assert _frames(result["path"]) == 72, "the music mix must not cut the picture to the clip audio"
    kinds = sorted(s["codec_type"] for s in _probe(result["path"])["streams"])
    assert kinds == ["audio", "video"]
