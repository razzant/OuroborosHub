"""'Could not score' never reads as "passed", and recovery paths send requests the
provider can accept."""
from __future__ import annotations

import asyncio
import json

import pytest

import anime_studio.pipeline as pl
from anime_studio.models import GenerationSettings, Job, Scene, Storyboard

DIMS = ("identity", "motion", "style", "artifacts", "composition")


def _scene():
    return Scene(index=1, description="hero walks", duration_sec=8, characters=[],
                 location="valley", camera_direction="static")


def _storyboard():
    return Storyboard(title="t", synopsis="s", style="modern anime", total_duration_sec=8.0,
                      scenes=[_scene()])


class _VlmClient:
    def __init__(self, reply):
        self.reply = reply

    async def analyze_multi_image_vlm(self, frame_paths, prompt):
        return dict(self.reply)


def _verify(tmp_path, reply):
    pipe = pl.Pipeline(client=_VlmClient(reply), state_dir=tmp_path)
    frames = []
    for n in range(2):
        frame = tmp_path / f"frame_{n}.jpg"
        frame.write_bytes(b"jpg")
        frames.append(str(frame))
    pipe._extract_video_frames = lambda video_path, num_frames=5, prefix="": list(frames)
    pipe._last_frame_extraction_error = ""
    job = Job(settings=GenerationSettings(theme="t"))
    result = asyncio.run(pipe._verify_video_multidim(job, "clip.mp4", _scene(), _storyboard()))
    return result, job


def _scores(value=8, **overrides):
    reply = {k: value for k in DIMS}
    reply.update(overrides)
    reply.setdefault("issues", [])
    reply.setdefault("suggestion", "")
    return reply


@pytest.mark.parametrize("reply, label", [
    ({"identity": 9, "motion": 9, "issues": [], "suggestion": ""}, "missing dimensions"),
    (_scores(composition=None), "null dimension"),
    (_scores(identity="9"), "string score"),
    (_scores(motion=True), "boolean score"),
    (_scores(style=float("nan")), "NaN"),
    (_scores(identity=85), "percentage-scale score"),
    (_scores(artifacts=-1), "negative score"),
])
def test_unusable_scores_are_indeterminate(tmp_path, reply, label):
    result, job = _verify(tmp_path, reply)
    assert result["verdict"] == "indeterminate", label
    assert result["passed"] is False
    assert result["reason"] == "multidim_schema_incomplete"
    assert any("UNVERIFIED" in w for w in job.progress.warnings)


def test_complete_scores_still_pass_or_fail_on_the_threshold(tmp_path):
    good, _ = _verify(tmp_path, _scores(8))
    assert good["verdict"] == "pass" and good["passed"] is True
    assert good["weighted_score"] == pytest.approx(8.0)

    bad, _ = _verify(tmp_path, _scores(3))
    assert bad["verdict"] == "fail" and bad["passed"] is False

    edge, _ = _verify(tmp_path, _scores(10, identity=0))
    assert edge["verdict"] == "pass"
    assert edge["weighted_score"] == pytest.approx(7.0)


def test_unknown_capabilities_respect_the_documented_hailuo3_floor(tmp_path):
    pipe = pl.Pipeline(client=object(), state_dir=tmp_path)
    pipe._video_caps = {}
    assert pipe._clamp_duration(4, "minimax/hailuo-3") == 5
    assert pipe._clamp_duration(12, "minimax/hailuo-3") == 12
    assert pipe._clamp_duration(40, "minimax/hailuo-3") == 15
    assert pipe._clamp_duration(4, "google/veo-3.1") == 4, "the floor is per model, not global"

    pipe._video_caps = {"minimax/hailuo-3": {"durations": [6, 10]}}
    assert pipe._clamp_duration(4, "minimax/hailuo-3") == 6, "live capabilities still win"


def test_resolution_reconciliation_is_read_only(tmp_path):
    pipe = pl.Pipeline(client=object(), state_dir=tmp_path)
    pipe._video_caps = {
        "google/veo-3.1": {"resolutions": ["720p", "1080p"]},
        "minimax/hailuo-3": {"resolutions": ["2K"]},
        "odd/model": {"resolutions": ["8K"]},
    }
    job = Job(settings=GenerationSettings(theme="t", resolution="2K"))

    assert pipe._resolution_for_model(job, "google/veo-3.1") == "1080p"
    assert pipe._resolution_for_model(job, "minimax/hailuo-3") == "2K"
    assert pipe._resolution_for_model(job, "unknown/model") == "2K", "no evidence keeps the request"
    assert pipe._resolution_for_model(job, "odd/model") == "2K"
    job.settings.resolution = "480p"
    assert pipe._resolution_for_model(job, "minimax/hailuo-3") == "2K"
    assert job.settings.resolution == "480p"


def test_hailuo3_is_a_recovery_option_for_the_advisor():
    assert "minimax/hailuo-3" in pl.Pipeline.AVAILABLE_VIDEO_MODELS


class _StoryboardClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        return json.dumps(self.payload)

    def parse_json_response(self, text):
        return json.loads(text)


def test_storyboard_with_surplus_keys_parses_on_the_first_paid_attempt(tmp_path, no_sleep):
    payload = {
        "title": "T", "synopsis": "S", "style": "modern anime", "total_duration_sec": 16,
        "characters": [{"name": "Hero", "description": "d", "visual_traits": "red scarf",
                        "age": 17, "voice": "calm"}],
        "locations": [{"name": "Valley", "description": "d", "visual_traits": "grass",
                       "time_of_day": "dawn"}],
        "scenes": [
            {"index": i, "description": f"scene {i}", "duration_sec": 8, "characters": ["Hero"],
             "location": "Valley", "camera_direction": "static", "lens": "35mm"}
            for i in (1, 2)
        ],
        "music_cues": [{"segment_index": 0, "mood": "calm", "tempo": "slow",
                        "style": "orchestral", "duration_sec": 16, "description": "d",
                        "bpm": 70}],
    }
    client = _StoryboardClient(payload)
    pipe = pl.Pipeline(client=client, state_dir=tmp_path)
    settings = GenerationSettings(theme="t", num_scenes=2, duration_sec=16)

    storyboard = asyncio.run(pipe._generate_scenario(settings))

    assert client.calls == 1, "each storyboard retry is a separately charged attempt"
    assert storyboard.characters[0].name == "Hero"
    assert storyboard.locations[0].name == "Valley"
    assert storyboard.music_cues[0].mood == "calm"
    assert len(storyboard.scenes) == 2
