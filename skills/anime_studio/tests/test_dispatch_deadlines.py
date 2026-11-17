"""One bounded duration contract for a video dispatch, exercised through the
real pipeline wrapper with scaled time and a counted provider: an accepted job
is never cut by the outer timeout, and a clip that was accepted but not
retrieved keeps its identity and never triggers a second paid POST."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import anime_studio.api_client as api
import anime_studio.budget as budget
import anime_studio.pipeline as pl
from anime_studio.models import GenerationSettings, Job, Scene, Storyboard

CLIP = b"\x00\x00\x00\x18ftypmp42" + b"x" * 64
SCALE = 0.001  # 1200 s of provider wait become 1.2 s
_REAL_SLEEP = asyncio.sleep  # stalls must not be shortened by the short_pauses fixture
_TIME_NAMES = (
    "VIDEO_MAX_WAIT", "VIDEO_POLL_INTERVAL", "VIDEO_DOWNLOAD_TIMEOUT", "VIDEO_POST_DEADLINE",
    "VIDEO_POLL_REQUEST_DEADLINE", "VIDEO_DOWNLOAD_BUDGET", "VIDEO_CALL_DEADLINE",
    "VIDEO_DOWNLOAD_RETRY_PAUSE", "POLL_HEARTBEAT_INTERVAL",
)


@pytest.fixture
def scaled_time(monkeypatch):
    """Scale every duration of the contract, the pipeline's outer wrapper included."""
    for name in _TIME_NAMES:
        if hasattr(api, name):
            monkeypatch.setattr(api, name, getattr(api, name) * SCALE)
    monkeypatch.setattr(pl, "TIMEOUT_VIDEO", pl.TIMEOUT_VIDEO * SCALE)


@pytest.fixture
def short_pauses(monkeypatch):
    """Retry pauses are fixed seconds in the client; keep them short in real time."""
    real_sleep = asyncio.sleep

    async def capped(delay, *args, **kwargs):
        await real_sleep(min(float(delay), 0.05))

    monkeypatch.setattr(asyncio, "sleep", capped)


def test_outer_wrapper_is_derived_from_and_above_the_client_contract():
    parts = (api.VIDEO_POST_DEADLINE + api.VIDEO_MAX_WAIT + api.VIDEO_POLL_INTERVAL
             + api.VIDEO_POLL_REQUEST_DEADLINE + api.VIDEO_DOWNLOAD_BUDGET)
    assert api.VIDEO_CALL_DEADLINE == pytest.approx(parts)
    assert pl.TIMEOUT_VIDEO > api.VIDEO_CALL_DEADLINE
    assert api.VIDEO_DOWNLOAD_BUDGET >= api.VIDEO_DOWNLOAD_TIMEOUT


class _Provider:
    """Counts POSTs; status and content answers follow per-test rules."""

    def __init__(self, *, completes_after=0.0, status=None, content=None):
        self.posts = 0
        self.downloads = 0
        self.t0 = 0.0
        self.completes_after = completes_after
        self.status = status
        self.content = content

    async def __call__(self, request: httpx.Request):
        if request.method == "POST":
            self.posts += 1
            self.t0 = time.monotonic()
            return httpx.Response(200, json={"id": f"job-{self.posts}"})
        if request.url.path.endswith("/content"):
            self.downloads += 1
            if self.content is not None:
                return await self.content(self)
            return httpx.Response(200, content=CLIP)
        if self.status is not None:
            return self.status(self)
        done = time.monotonic() - self.t0 >= self.completes_after
        return httpx.Response(200, json={"status": "completed" if done else "processing"})


def _scene_pipeline(tmp_path):
    client = api.OpenRouterClient("test-key", tmp_path)
    pipe = pl.Pipeline(client=client, state_dir=tmp_path)
    pipe.ledger = budget.BudgetLedger(limit_usd=100.0, estimate_usd=50.0, mode="low")
    advisor_calls: list[str] = []

    async def verify(job, video_path, scene, storyboard):
        return {"verdict": "pass", "passed": True, "weighted_score": 8.0, "scores": {}}

    async def panel(job, video_path, scene, storyboard):
        return {"verdict": "pass"}

    async def advisor(error, model, description):
        advisor_calls.append(error)
        return {"action": "retry_same_model", "reason": "looks transient", "suggested_model": None}

    pipe._verify_video_multidim = verify
    pipe._judge_identity_panel = panel
    pipe._extract_last_frame = lambda video_path, scene_index: None
    pipe._get_failure_advisor_recommendation = advisor
    return pipe, advisor_calls


def _one_scene():
    return Storyboard(title="t", synopsis="s", style="modern anime", total_duration_sec=8.0, scenes=[
        Scene(index=1, description="a hero walks", duration_sec=8, characters=[],
              location="nowhere", camera_direction="static"),
    ])


def _job():
    return Job(settings=GenerationSettings(theme="t", num_scenes=1, duration_sec=8,
                                           quality_mode="low", include_dialogue=False))


def test_late_completion_with_a_stalled_download_finishes_inside_the_real_wrapper(
    tmp_path, mock_http, scaled_time,
):
    async def stall_first(provider):
        if provider.downloads == 1:
            await asyncio.sleep(30)  # an httpx timeout never ends this
        return httpx.Response(200, content=CLIP)

    provider = _Provider(completes_after=0.85 * api.VIDEO_MAX_WAIT, content=stall_first)
    mock_http(provider)
    pipe, advisor_calls = _scene_pipeline(tmp_path)
    job = _job()

    asyncio.run(pipe._generate_videos(job, _one_scene()))

    assert provider.posts == 1, "the paid clip must not be generated twice"
    assert advisor_calls == []
    assert len(job.progress.video_clips) == 1
    assert open(job.progress.video_clips[0], "rb").read() == CLIP


def test_a_stalled_status_poll_is_bounded_and_retried(tmp_path, mock_http, scaled_time):
    state = {"polls": 0}

    async def handler(request: httpx.Request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job-1"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=CLIP)
        state["polls"] += 1
        if state["polls"] == 1:
            await asyncio.sleep(30)
        return httpx.Response(200, json={"status": "completed"})

    requests = mock_http(handler)
    path = asyncio.run(pl.run_with_timeout(
        api.OpenRouterClient("test-key", tmp_path).generate_video(prompt="a scene", filename="s.mp4"),
        timeout_sec=pl.TIMEOUT_VIDEO, description="scaled dispatch",
    ))
    assert open(path, "rb").read() == CLIP
    assert len([r for r in requests if r.method == "POST"]) == 1


def _content_always(status_code):
    async def answer(provider):
        return httpx.Response(status_code, text="busy")
    return answer


@pytest.mark.parametrize("mode", ["download_exhausted", "write_failure", "still_running", "poll_exhausted"])
def test_accepted_but_unretrieved_clip_keeps_identity_and_is_not_paid_again(
    tmp_path, mock_http, scaled_time, short_pauses, monkeypatch, mode,
):
    if mode == "download_exhausted":
        provider = _Provider(content=_content_always(503))
    elif mode == "still_running":
        provider = _Provider(completes_after=10 ** 6)
    elif mode == "poll_exhausted":
        provider = _Provider(status=lambda p: httpx.Response(503, text="busy"))
    else:
        provider = _Provider()
        real_write = type(tmp_path).write_bytes

        def full_disk(self, data):
            if self.suffix == ".mp4" and self.name.startswith("scene_"):
                raise OSError(28, "No space left on device")
            return real_write(self, data)

        monkeypatch.setattr(type(tmp_path), "write_bytes", full_disk)
    mock_http(provider)
    pipe, advisor_calls = _scene_pipeline(tmp_path)
    job = _job()

    asyncio.run(pipe._generate_videos(job, _one_scene()))

    assert provider.posts == 1, "an accepted clip must not be answered with a second paid POST"
    assert advisor_calls == [], "the advisor must not treat an accepted job as never generated"
    records = job.progress.verification_stats.get("unretrieved_accepted_jobs") or []
    assert [r["job_id"] for r in records] == ["job-1"]
    assert "accepted_clip_unretrieved:1" in job.progress.partial_reasons
    assert any("job-1" in w for w in job.progress.warnings)
    assert pipe.ledger.spent_usd == pytest.approx(budget.UNIT_COST_USD["video_clip"]), (
        "the accepted clip may be billed, so its charge stays"
    )


class _FirstPost:
    """The first POST follows `mode`; later POSTs are accepted normally.

    Every POST that reaches the handler is counted as received by the
    provider: a dropped answer or a connection error does not prove the bytes
    never left, so the request may have created a job.
    """

    def __init__(self, mode):
        self.mode = mode
        self.posts = 0

    async def __call__(self, request: httpx.Request):
        if request.method == "POST":
            self.posts += 1
            if self.posts == 1:
                return await self._first(request)
            return httpx.Response(200, json={"id": f"job-{self.posts}"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=CLIP)
        return httpx.Response(200, json={"status": "completed"})

    async def _first(self, request):
        mode = self.mode
        if mode == "dropped_response":
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.",
                                            request=request)
        if mode == "read_timeout":
            raise httpx.ReadTimeout("read timed out", request=request)
        if mode == "connect_error":
            raise httpx.ConnectError("connection reset by peer", request=request)
        if mode == "stalled":
            await _REAL_SLEEP(30)
        if mode == "invalid_body":
            return httpx.Response(200, text="<html>gateway</html>")
        if mode == "non_object_body":
            return httpx.Response(200, json=[])
        if mode == "no_id":
            return httpx.Response(200, json={"status": "queued"})
        if mode == "server_error":
            return httpx.Response(502, text="bad gateway from upstream")
        raise AssertionError(mode)


_RAW_REASON = {
    "dropped_response": "RemoteProtocolError", "read_timeout": "ReadTimeout",
    "connect_error": "ConnectError", "stalled": "no answer within",
    "invalid_body": "<html>gateway</html>", "non_object_body": "no usable job id",
    "no_id": "no usable job id", "server_error": "HTTP 502",
}


@pytest.mark.parametrize("mode", sorted(_RAW_REASON))
def test_ambiguous_dispatch_outcome_stops_the_scene_without_a_second_post(
    tmp_path, mock_http, scaled_time, short_pauses, mode,
):
    provider = _FirstPost(mode)
    mock_http(provider)
    pipe, advisor_calls = _scene_pipeline(tmp_path)
    job = _job()

    asyncio.run(pipe._generate_videos(job, _one_scene()))

    assert provider.posts == 1, "a dispatch that may have been accepted must not be paid again"
    assert advisor_calls == []
    assert job.progress.video_clips == []
    records = job.progress.verification_stats["unretrieved_accepted_jobs"]
    assert len(records) == 1
    assert records[0]["job_id"] is None, "no job id is invented"
    assert records[0]["accepted"] == "unknown" and records[0]["stage"] == "dispatch"
    assert _RAW_REASON[mode] in records[0]["detail"], "the raw reason is kept"
    assert "dispatch_outcome_unknown:1" in job.progress.partial_reasons
    assert pipe.ledger.spent_usd == pytest.approx(budget.UNIT_COST_USD["video_clip"])
    assert not [e for e in pipe.ledger.to_dict()["entries"] if e["usd"] < 0], "no refund"


@pytest.mark.parametrize("status, refunded", [(400, True), (422, True), (402, False), (429, False)])
def test_known_refusal_still_reaches_the_approved_recovery(
    tmp_path, mock_http, scaled_time, short_pauses, status, refunded,
):
    state = {"posts": 0}

    async def handler(request: httpx.Request):
        if request.method == "POST":
            state["posts"] += 1
            if state["posts"] == 1:
                return httpx.Response(status, text='{"error":"invalid params"}')
            return httpx.Response(200, json={"id": "job-2"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=CLIP)
        return httpx.Response(200, json={"status": "completed"})

    mock_http(handler)
    pipe, advisor_calls = _scene_pipeline(tmp_path)
    job = _job()

    asyncio.run(pipe._generate_videos(job, _one_scene()))

    assert len(advisor_calls) == 1 and state["posts"] == 2
    assert len(job.progress.video_clips) == 1
    unit = budget.UNIT_COST_USD["video_clip"]
    assert pipe.ledger.spent_usd == pytest.approx(unit if refunded else 2 * unit)


@pytest.mark.parametrize("always", [False, True], ids=["once", "always"])
def test_malformed_status_answer_keeps_the_accepted_id(
    tmp_path, mock_http, scaled_time, short_pauses, always,
):
    state = {"posts": 0, "polls": 0}

    async def handler(request: httpx.Request):
        if request.method == "POST":
            state["posts"] += 1
            return httpx.Response(200, json={"id": f"job-{state['posts']}"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=CLIP)
        state["polls"] += 1
        if always or state["polls"] == 1:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"status": "completed"})

    mock_http(handler)
    pipe, advisor_calls = _scene_pipeline(tmp_path)
    job = _job()

    asyncio.run(pipe._generate_videos(job, _one_scene()))

    assert state["posts"] == 1 and advisor_calls == []
    if always:
        records = job.progress.verification_stats["unretrieved_accepted_jobs"]
        assert [(r["job_id"], r["stage"]) for r in records] == [("job-1", "poll")]
    else:
        assert len(job.progress.video_clips) == 1


def _flaky_clients(monkeypatch, *, fail_init=(), fail_close=()):
    """Wrap the scripted client: setup or teardown of the n-th client fails."""
    base = httpx.AsyncClient
    count = {"n": 0}

    class Flaky(base):
        def __init__(self, *args, **kwargs):
            count["n"] += 1
            self._index = count["n"]
            if self._index in fail_init:
                raise OSError("synthetic client setup failure")
            super().__init__(*args, **kwargs)

        async def aclose(self):
            await super().aclose()
            if self._index in fail_close:
                raise OSError("synthetic client teardown failure")

        async def __aexit__(self, *exc_info):
            await super().__aexit__(*exc_info)
            if self._index in fail_close:
                raise OSError("synthetic client teardown failure")

    monkeypatch.setattr(httpx, "AsyncClient", Flaky)


def test_client_setup_failure_after_acceptance_keeps_the_id(
    tmp_path, mock_http, scaled_time, short_pauses, monkeypatch,
):
    provider = _Provider()
    mock_http(provider)
    _flaky_clients(monkeypatch, fail_init={2})  # 1 = dispatch POST, 2 = status polls
    pipe, advisor_calls = _scene_pipeline(tmp_path)
    job = _job()

    asyncio.run(pipe._generate_videos(job, _one_scene()))

    assert provider.posts == 1 and advisor_calls == []
    records = job.progress.verification_stats["unretrieved_accepted_jobs"]
    assert [(r["job_id"], r["stage"]) for r in records] == [("job-1", "poll")]
    assert "synthetic client setup failure" in records[0]["detail"]


@pytest.mark.parametrize("client_index", [2, 3], ids=["poll_client", "download_client"])
def test_client_teardown_failure_after_the_download_keeps_the_clip(
    tmp_path, mock_http, scaled_time, short_pauses, monkeypatch, client_index,
):
    provider = _Provider()
    mock_http(provider)
    _flaky_clients(monkeypatch, fail_close={client_index})
    pipe, advisor_calls = _scene_pipeline(tmp_path)
    job = _job()

    asyncio.run(pipe._generate_videos(job, _one_scene()))

    assert provider.posts == 1 and advisor_calls == []
    assert len(job.progress.video_clips) == 1
    assert open(job.progress.video_clips[0], "rb").read() == CLIP


def test_when_no_scene_has_a_clip_the_final_error_keeps_the_evidence(
    tmp_path, mock_http, scaled_time, short_pauses, monkeypatch,
):
    state = {"posts": 0}

    async def handler(request: httpx.Request):
        if request.method == "POST":
            state["posts"] += 1
            if state["posts"] == 1:
                raise httpx.RemoteProtocolError("Server disconnected", request=request)
            return httpx.Response(200, json={"id": f"job-{state['posts']}"})
        if request.url.path.endswith("/content"):
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"status": "completed"})

    mock_http(handler)
    pipe, advisor_calls = _scene_pipeline(tmp_path)

    async def no_caps():
        return {}

    async def storyboard(settings, job=None):
        return Storyboard(title="t", synopsis="s", style="modern anime", total_duration_sec=16.0,
                          scenes=[Scene(index=i, description=f"scene {i}", duration_sec=8,
                                        characters=[], location="nowhere",
                                        camera_direction="static") for i in (1, 2)])

    async def no_assets(job, storyboard):
        return None

    pipe.client.fetch_image_capabilities = no_caps
    pipe.client.fetch_video_capabilities = no_caps
    pipe._generate_scenario = storyboard
    pipe._generate_assets = no_assets
    pipe.ledger = None  # run() builds the job's own rail
    monkeypatch.setattr(pl, "ensure_ffmpeg", lambda cache: {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"})
    job = Job(settings=GenerationSettings(theme="t", num_scenes=2, duration_sec=16,
                                          quality_mode="low", include_dialogue=False,
                                          include_music=False))

    asyncio.run(pipe.run(job))

    assert state["posts"] == 2 and advisor_calls == [], "one dispatch per scene, none repeated"
    assert job.progress.status.value == "error"
    assert "unknown (no id came back)" in job.progress.error
    assert "job-2" in job.progress.error
    assert {"dispatch_outcome_unknown:1", "accepted_clip_unretrieved:2"} <= set(job.progress.partial_reasons)


def test_an_outer_wrapper_cut_is_an_unknown_outcome_too(
    tmp_path, mock_http, scaled_time, short_pauses, monkeypatch,
):
    provider = _FirstPost("stalled")
    mock_http(provider)
    # Force the safety net to fire first; the contract normally prevents it.
    monkeypatch.setattr(pl, "TIMEOUT_VIDEO", api.VIDEO_POST_DEADLINE / 4)
    pipe, advisor_calls = _scene_pipeline(tmp_path)
    job = _job()

    asyncio.run(pipe._generate_videos(job, _one_scene()))

    assert provider.posts == 1 and advisor_calls == []
    records = job.progress.verification_stats["unretrieved_accepted_jobs"]
    assert [(r["job_id"], r["stage"]) for r in records] == [(None, "dispatch")]
    assert "timed out" in records[0]["detail"]
