"""A scene whose paid dispatch was accepted, or may have been, is never paid for
again by the improvement loops. Full Pipeline.run in `max` mode (two
continuity regenerations allowed, identity panel active) against a counted
fake provider that serves a real tiny MP4; network refused by the suite."""
from __future__ import annotations

import asyncio
import shutil
import subprocess

import httpx
import pytest

import anime_studio.api_client as api
import anime_studio.budget as budget
import anime_studio.pipeline as pl
from anime_studio.models import GenerationSettings, Job, Scene, Storyboard

SCALE = 0.001
_TIME_NAMES = (
    "VIDEO_MAX_WAIT", "VIDEO_POLL_INTERVAL", "VIDEO_DOWNLOAD_TIMEOUT", "VIDEO_POST_DEADLINE",
    "VIDEO_POLL_REQUEST_DEADLINE", "VIDEO_DOWNLOAD_BUDGET", "VIDEO_CALL_DEADLINE",
    "VIDEO_DOWNLOAD_RETRY_PAUSE", "POLL_HEARTBEAT_INTERVAL",
)
CLIP_UNIT = budget.UNIT_COST_USD["video_clip"]

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe needed to serve and assemble real clips",
)


@pytest.fixture(scope="module")
def clip_bytes(tmp_path_factory):
    path = tmp_path_factory.mktemp("clip") / "clip.mp4"
    subprocess.run([
        shutil.which("ffmpeg"), "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True, capture_output=True, timeout=120)
    return path.read_bytes()


@pytest.fixture
def scaled_time(monkeypatch):
    for name in _TIME_NAMES:
        if hasattr(api, name):
            monkeypatch.setattr(api, name, getattr(api, name) * SCALE)
    monkeypatch.setattr(pl, "TIMEOUT_VIDEO", pl.TIMEOUT_VIDEO * SCALE)


class _Provider:
    """POST n follows plan[n] ("ok", "dropped" or an HTTP refusal status)."""

    def __init__(self, plan, clip):
        self.plan = dict(plan)
        self.clip = clip
        self.posts = 0

    async def __call__(self, request: httpx.Request):
        if request.method == "POST":
            self.posts += 1
            step = self.plan.get(self.posts, "ok")
            if step == "dropped":
                raise httpx.RemoteProtocolError("Server disconnected", request=request)
            if isinstance(step, int):
                return httpx.Response(step, text='{"error":"refused"}')
            return httpx.Response(200, json={"id": f"job-{self.posts}"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=self.clip)
        return httpx.Response(200, json={"status": "completed"})


def _run_max_job(tmp_path, monkeypatch, *, continuity, panel_fail=(), verify_fail=()):
    """Run the whole pipeline in max mode with scripted judges.

    continuity: successive answers of the cross-scene reviewer (scene positions
    or None). panel_fail: scene indexes the identity panel fails in the main
    loop. verify_fail: (scene index, attempt number) pairs whose multidim
    verification fails, so the candidate loop tries again.
    """
    client = api.OpenRouterClient("test-key", tmp_path)
    pipe = pl.Pipeline(client=client, state_dir=tmp_path)
    advisor_calls: list[str] = []
    continuity_answers = list(continuity)
    seen_verify: dict[int, int] = {}

    async def no_caps():
        return {}

    async def storyboard(settings, job=None):
        return Storyboard(title="t", synopsis="s", style="modern anime", total_duration_sec=2.0,
                          scenes=[Scene(index=i, description=f"scene {i}", duration_sec=1,
                                        characters=[], location="nowhere",
                                        camera_direction="static") for i in (0, 1)])

    async def no_assets(job, storyboard):
        return None

    async def verify(job, video_path, scene, storyboard):
        seen_verify[scene.index] = seen_verify.get(scene.index, 0) + 1
        if (scene.index, seen_verify[scene.index]) in verify_fail:
            return {"verdict": "fail", "passed": False, "weighted_score": 3.0, "scores": {},
                    "issues": ["hair colour drifted"], "suggestion": ""}
        return {"verdict": "pass", "passed": True, "weighted_score": 8.0, "scores": {}}

    async def panel(job, video_path, scene, storyboard):
        if scene.index in panel_fail and "_regen" not in str(video_path):
            return {"verdict": "fail", "reason": "hair colour drifted"}
        return {"verdict": "pass"}

    async def cross_scene(job, storyboard):
        answer = continuity_answers.pop(0) if continuity_answers else None
        if answer is not None:
            job.progress.verification_stats["cross_scene_drift"] = "hair colour drifted"
        return answer

    async def advisor(error, model, description):
        advisor_calls.append(error)
        return {"action": "skip", "reason": "", "suggested_model": None}

    client.fetch_image_capabilities = no_caps
    client.fetch_video_capabilities = no_caps
    pipe._generate_scenario = storyboard
    pipe._generate_assets = no_assets
    pipe._verify_video_multidim = verify
    pipe._judge_identity_panel = panel
    pipe._cross_scene_identity_check = cross_scene
    pipe._extract_last_frame = lambda video_path, scene_index: None
    pipe._get_failure_advisor_recommendation = advisor
    monkeypatch.setattr(pl, "ensure_ffmpeg", lambda cache: {
        "ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"),
    })
    job = Job(settings=GenerationSettings(theme="t", num_scenes=2, duration_sec=8,
                                          quality_mode="max", include_dialogue=False,
                                          include_music=False))
    asyncio.run(pipe.run(job))
    return pipe, job, advisor_calls


def _video_charges(pipe):
    return [e for e in pipe.ledger.to_dict()["entries"] if e["kind"] == "video_clip"]


def test_continuity_does_not_pay_again_after_an_unknown_first_regeneration(
    tmp_path, mock_http, scaled_time, monkeypatch, clip_bytes,
):
    provider = _Provider({3: "dropped"}, clip_bytes)  # 1, 2 = scenes; 3 = first regeneration
    mock_http(provider)

    pipe, job, advisor_calls = _run_max_job(tmp_path, monkeypatch, continuity=[0, 0, 0])

    assert provider.posts == 3, "the re-selected scene must not be paid for a second time"
    assert advisor_calls == []
    scene0 = job.progress.storyboard.scenes[0]
    assert scene0.video_url.endswith("scene_0_v0.mp4"), "the scene keeps its delivered clip"
    records = job.progress.verification_stats["unretrieved_accepted_jobs"]
    assert [(r["scene"], r["attempt"], r["job_id"], r["stage"]) for r in records] == [
        (0, "continuity_regen", None, "dispatch"),
    ]
    assert job.progress.verification_stats["regeneration_skipped_unretrieved"] == [0]
    assert {"dispatch_outcome_unknown:0", "continuity_drift_accepted_after_1_regens"} <= set(
        job.progress.partial_reasons)
    charges = _video_charges(pipe)
    assert len(charges) == 3 and all(e["usd"] > 0 for e in charges), "the expense stays"
    assert job.progress.status.value == "partial"
    assert job.progress.final_video_url and job.progress.missing_scenes == []


def test_identity_panel_does_not_pay_for_a_scene_with_an_unresolved_main_dispatch(
    tmp_path, mock_http, scaled_time, monkeypatch, clip_bytes,
):
    # Scene 0: candidate 1 fails verification, candidate 2 is lost in transit;
    # the first clip is kept and the panel fails it.
    provider = _Provider({2: "dropped"}, clip_bytes)
    mock_http(provider)

    pipe, job, advisor_calls = _run_max_job(
        tmp_path, monkeypatch, continuity=[None], panel_fail={0}, verify_fail={(0, 1)},
    )

    assert provider.posts == 3, "two main POSTs for scene 0, one for scene 1, no regeneration"
    assert advisor_calls == []
    assert job.progress.storyboard.scenes[0].video_url.endswith("scene_0_v0.mp4")
    records = job.progress.verification_stats["unretrieved_accepted_jobs"]
    assert [(r["scene"], r["attempt"], r["job_id"]) for r in records] == [(0, "candidate_2", None)]
    assert job.progress.verification_stats["regeneration_skipped_unretrieved"] == [0]
    assert {"dispatch_outcome_unknown:0", "identity_panel_failed_unresolved:0"} <= set(
        job.progress.partial_reasons)
    assert 0 in job.progress.unverified_scenes
    assert job.progress.status.value == "partial"


def test_identity_panel_does_not_pay_again_after_an_unknown_continuity_regeneration(
    tmp_path, mock_http, scaled_time, monkeypatch, clip_bytes,
):
    provider = _Provider({3: "dropped"}, clip_bytes)
    mock_http(provider)

    pipe, job, advisor_calls = _run_max_job(
        tmp_path, monkeypatch, continuity=[0, None], panel_fail={0},
    )

    assert provider.posts == 3
    records = job.progress.verification_stats["unretrieved_accepted_jobs"]
    assert [(r["scene"], r["attempt"]) for r in records] == [(0, "continuity_regen")]
    assert "identity_panel_failed_unresolved:0" in job.progress.partial_reasons
    assert job.progress.storyboard.scenes[0].video_url.endswith("scene_0_v0.mp4")


def test_successful_regeneration_still_replaces_the_clip(
    tmp_path, mock_http, scaled_time, monkeypatch, clip_bytes,
):
    provider = _Provider({}, clip_bytes)
    mock_http(provider)

    pipe, job, advisor_calls = _run_max_job(tmp_path, monkeypatch, continuity=[0, None])

    assert provider.posts == 3
    assert job.progress.storyboard.scenes[0].video_url.endswith("scene_0_regen.mp4")
    assert "unretrieved_accepted_jobs" not in job.progress.verification_stats
    assert job.progress.verification_stats["cross_scene_resolved"] is True
    assert job.progress.status.value == "done"


@pytest.mark.parametrize("status, refunded", [(422, True), (429, False)])
def test_known_refusal_still_allows_the_next_regeneration(
    tmp_path, mock_http, scaled_time, monkeypatch, clip_bytes, status, refunded,
):
    provider = _Provider({3: status}, clip_bytes)
    mock_http(provider)

    pipe, job, advisor_calls = _run_max_job(tmp_path, monkeypatch, continuity=[0, 0, None])

    assert provider.posts == 4, "a refusal created no job, so the second regeneration may run"
    assert job.progress.storyboard.scenes[0].video_url.endswith("scene_0_regen.mp4")
    assert "unretrieved_accepted_jobs" not in job.progress.verification_stats
    refunds = [e for e in pipe.ledger.to_dict()["entries"] if e["usd"] < 0]
    assert len(refunds) == (1 if refunded else 0)
    assert pipe.ledger.spent_usd == pytest.approx((3 if refunded else 4) * CLIP_UNIT)
