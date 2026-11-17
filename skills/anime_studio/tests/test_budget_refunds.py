"""The spend rail never under-counts: a video charge is refunded only when the
provider proved nothing was generated, exactly once, and only for the dispatch
that failed; every clip is charged the generic unit."""
from __future__ import annotations

import asyncio

import httpx
import pytest

import anime_studio.api_client as api
import anime_studio.budget as budget
import anime_studio.pipeline as pl
from anime_studio.models import GenerationSettings, Job, Scene, Storyboard

CLIP_UNIT = budget.UNIT_COST_USD["video_clip"]


def _ledger(limit=100.0):
    return budget.BudgetLedger(limit_usd=limit, estimate_usd=50.0, mode="low")


def test_charge_returns_a_receipt_and_refund_is_exact_and_idempotent():
    ledger = _ledger()
    first = ledger.charge("video_clip", note="scene 1")
    second = ledger.charge("video_clip", note="scene 2")
    assert isinstance(first, dict) and first["id"] != second["id"]
    assert ledger.spent_usd == pytest.approx(2 * CLIP_UNIT)

    assert ledger.refund(second, note="HTTP 400") is True
    assert ledger.spent_usd == pytest.approx(CLIP_UNIT)
    assert ledger.refund(second, note="again") is False, "a receipt refunds once"
    assert ledger.spent_usd == pytest.approx(CLIP_UNIT)

    refund_rows = [e for e in ledger.to_dict()["entries"] if e["usd"] < 0]
    assert len(refund_rows) == 1
    assert refund_rows[0]["receipt_id"] == second["id"]
    assert refund_rows[0]["usd"] == pytest.approx(-CLIP_UNIT)


@pytest.mark.parametrize("receipt", [None, {}, {"id": 999}, {"id": "x"}, {"id": 0}])
def test_unknown_receipts_refund_nothing(receipt):
    ledger = _ledger()
    ledger.charge("video_clip")
    assert ledger.refund(receipt) is False
    assert ledger.spent_usd == pytest.approx(CLIP_UNIT)


def test_hailuo3_catalog_price_at_its_limits_stays_under_the_generic_unit():
    # Catalog SKUs recorded in SKILL.md: $0.13 per second, $0.04 per reference
    # image; H3 accepts at most 15 s and the payload attaches at most
    # VIDEO_MAX_INPUT_REFERENCES references. The generic unit must cover that.
    worst = 15 * 0.13 + pl.VIDEO_MAX_INPUT_REFERENCES * 0.04
    assert worst < CLIP_UNIT


def test_estimate_prices_every_video_model_with_the_generic_unit():
    estimate = budget.estimate_job_usd(4, "medium", include_music=True)
    clips = 4 * budget.mode_config("medium")["video_candidates"]
    assert estimate["breakdown"]["video_clip"] == pytest.approx(clips * CLIP_UNIT)


def _job(model="bytedance/seedance-2.0", scenes=2, mode="low"):
    return Job(settings=GenerationSettings(
        theme="t", video_model=model, num_scenes=scenes, duration_sec=30, quality_mode=mode,
        include_dialogue=False,
    ))


def test_pipeline_charges_a_hailuo3_clip_the_generic_unit(tmp_path):
    pipe = pl.Pipeline(client=object(), state_dir=tmp_path)
    pipe.ledger = _ledger()
    job = _job(model="minimax/hailuo-3", scenes=4)
    receipt = pipe._charge(job, "video_clip", note="scene 1")
    assert receipt
    assert pipe.ledger.spent_usd == pytest.approx(CLIP_UNIT)


def _rejected(status):
    return api.VideoDispatchRejected(status, "provider said no", "minimax/hailuo-3")


@pytest.mark.parametrize("make_exc, refunded", [
    (lambda: _rejected(400), True),
    (lambda: _rejected(422), True),
    (lambda: api.VideoPromptFitError("reference lines do not fit"), True),
    (lambda: _rejected(402), False),
    (lambda: _rejected(429), False),
    (lambda: _rejected(500), False),
    (lambda: _rejected(503), False),
    (lambda: httpx.ReadTimeout("slow"), False),
    (lambda: TimeoutError("Video scene 1 timed out"), False),
    (lambda: RuntimeError("Video generated but download failed after 3 attempts"), False),
    (lambda: RuntimeError("Video generation failed: content policy"), False),
])
def test_refund_only_for_proven_non_generation(tmp_path, make_exc, refunded):
    pipe = pl.Pipeline(client=object(), state_dir=tmp_path)
    pipe.ledger = _ledger()
    job = _job()
    receipt = pipe._charge(job, "video_clip")
    assert pipe._refund_if_rejected(job, receipt, make_exc()) is refunded
    assert pipe.ledger.spent_usd == pytest.approx(0.0 if refunded else CLIP_UNIT)
    assert job.progress.budget["spent_usd"] == pytest.approx(round(pipe.ledger.spent_usd, 2))


# ─── The scene loop, with the provider and the judges stubbed ──────────


class _ScriptedVideoClient:
    """generate_video follows a per-call script; nothing touches the network."""

    def __init__(self, tmp_path, script):
        self.tmp_path = tmp_path
        self.script = list(script)
        self.calls: list[dict] = []

    async def generate_video(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        path = self.tmp_path / kwargs["filename"]
        path.write_bytes(b"clip")
        return str(path)


def _storyboard(n):
    return Storyboard(
        title="t", synopsis="s", style="modern anime", total_duration_sec=8.0 * n,
        scenes=[
            Scene(index=i, description=f"scene {i}", duration_sec=8, characters=[],
                  location="nowhere", camera_direction="static")
            for i in range(1, n + 1)
        ],
    )


def _scene_loop(tmp_path, script, *, condense=None, advisor=None):
    client = _ScriptedVideoClient(tmp_path, script)
    pipe = pl.Pipeline(client=client, state_dir=tmp_path)
    pipe.ledger = _ledger()
    advisor_calls: list[str] = []

    async def verify(job, video_path, scene, storyboard):
        return {"verdict": "pass", "passed": True, "weighted_score": 8.0, "scores": {}}

    async def panel(job, video_path, scene, storyboard):
        return {"verdict": "pass"}

    async def ask_advisor(error, model, description):
        advisor_calls.append(error)
        return advisor or {"action": "skip", "reason": "", "suggested_model": None}

    pipe._verify_video_multidim = verify
    pipe._judge_identity_panel = panel
    pipe._extract_last_frame = lambda video_path, scene_index: None
    pipe._get_failure_advisor_recommendation = ask_advisor
    if condense is not None:
        pipe._condense_prompt_for_model = condense
    return pipe, client, advisor_calls


def test_scene_loop_refunds_only_the_rejected_dispatch(tmp_path):
    pipe, client, _ = _scene_loop(tmp_path, ["ok", _rejected(400)])
    job = _job(scenes=2)
    asyncio.run(pipe._generate_videos(job, _storyboard(2)))

    assert len(client.calls) == 2
    assert pipe.ledger.spent_usd == pytest.approx(CLIP_UNIT), "scene 1 paid, scene 2 refunded"
    assert job.progress.missing_scenes == [] and len(job.progress.video_clips) == 1


def test_a_failure_before_the_charge_never_refunds_an_earlier_clip(tmp_path):
    calls = {"n": 0}

    async def condense(prompt, model):
        calls["n"] += 1
        if calls["n"] == 2:
            raise api.VideoPromptFitError("reference lines do not fit")
        return prompt

    pipe, client, _ = _scene_loop(tmp_path, ["ok"], condense=condense)
    job = _job(scenes=2)
    asyncio.run(pipe._generate_videos(job, _storyboard(2)))

    assert len(client.calls) == 1
    assert pipe.ledger.spent_usd == pytest.approx(CLIP_UNIT), (
        "scene 1's clip exists and was paid; scene 2 failed before its own charge"
    )
    refunds = [e for e in pipe.ledger.to_dict()["entries"] if e["usd"] < 0]
    assert refunds == []


def test_text_too_long_refusal_is_a_prompt_limit_and_skips_the_paid_advisor(tmp_path):
    refusal = api.VideoDispatchRejected(
        400, '{"error":"invalid params, content[0].text too long: 8640 > 7000 characters"}',
        "minimax/hailuo-3",
    )
    pipe, client, advisor_calls = _scene_loop(
        tmp_path, [refusal],
        advisor={"action": "retry_same_model", "reason": "transient", "suggested_model": None},
    )
    job = _job(model="minimax/hailuo-3", scenes=1)
    asyncio.run(pipe._generate_videos(job, _storyboard(1)))

    assert advisor_calls == [], "a definitive prompt-limit refusal must not reach the advisor"
    assert len(client.calls) == 1, "no second paid dispatch for the same refused prompt"
    assert pipe.ledger.spent_usd == pytest.approx(0.0)


def test_failed_advisor_retry_refunds_its_own_rejected_charge(tmp_path):
    pipe, client, advisor_calls = _scene_loop(
        tmp_path, [httpx.ReadTimeout("slow"), _rejected(422)],
        advisor={"action": "retry_same_model", "reason": "timeout", "suggested_model": None},
    )
    job = _job(scenes=1)
    asyncio.run(pipe._generate_videos(job, _storyboard(1)))

    assert len(advisor_calls) == 1 and len(client.calls) == 2
    # The timed-out first attempt may have been billed and stays charged; the
    # advisor retry was refused before generation and is refunded.
    assert pipe.ledger.spent_usd == pytest.approx(CLIP_UNIT)


def test_advisor_switch_reconciles_resolution_for_the_new_model(tmp_path):
    pipe, client, _ = _scene_loop(
        tmp_path, [_rejected(500), "ok"],
        advisor={"action": "switch_model", "reason": "model down", "suggested_model": "google/veo-3.1"},
    )
    pipe._video_caps = {
        "minimax/hailuo-3": {"resolutions": ["2K"], "durations": [6, 10]},
        "google/veo-3.1": {"resolutions": ["720p", "1080p"], "durations": [4, 6, 8]},
    }
    job = _job(model="minimax/hailuo-3", scenes=1)
    job.settings.resolution = "2K"
    asyncio.run(pipe._generate_videos(job, _storyboard(1)))

    assert [c["model"] for c in client.calls] == ["minimax/hailuo-3", "google/veo-3.1"]
    assert client.calls[0]["resolution"] == "2K"
    assert client.calls[1]["resolution"] == "1080p"
    assert job.settings.resolution == "2K", "a one-scene recovery must not change the job"


def test_a_charge_refused_after_admission_never_becomes_an_uncharged_dispatch(tmp_path):
    calls = {"condense": 0}
    pipe, client, advisor_calls = _scene_loop(
        tmp_path, [_rejected(500), "ok"],
        advisor={"action": "retry_same_model", "reason": "server error", "suggested_model": None},
    )
    pipe.ledger = _ledger(limit=6.0)

    async def condense_with_concurrent_spend(prompt, model):
        calls["condense"] += 1
        if calls["condense"] == 2:  # the advisor's retry, after its admission
            pipe.ledger.charge("keyframe", 2, note="spend that landed meanwhile")
        return prompt

    pipe._condense_prompt_for_model = condense_with_concurrent_spend
    job = _job(scenes=1)
    asyncio.run(pipe._generate_videos(job, _storyboard(1)))

    assert len(advisor_calls) == 1
    assert len(client.calls) == 1, "the refused retry must not be dispatched"
    charged_clips = [e for e in pipe.ledger.to_dict()["entries"] if e["kind"] == "video_clip"]
    assert len(charged_clips) == 1


def test_continuity_regeneration_is_condensed_before_its_paid_dispatch(tmp_path):
    critique = "CRITIQUE: Hero 1's scarf turned blue in the last second; keep it red."
    seen: list[str] = []

    async def condense(prompt, model):
        seen.append(prompt)
        return "condensed regeneration prompt keeps the critique: " + critique

    client = _ScriptedVideoClient(tmp_path, ["ok"])
    pipe = pl.Pipeline(client=client, state_dir=tmp_path)
    pipe.ledger = _ledger()
    pipe._condense_prompt_for_model = condense
    pipe._identity_continuity_note = lambda text: text
    storyboard = _storyboard(1)
    storyboard.scenes[0].description = "A long walk through the valley. " * 120
    job = _job(model="kwaivgi/kling-v3.0-pro", scenes=1)

    asyncio.run(pipe._regenerate_single_scene(job, storyboard, 0, critique))

    assert len(seen) == 1 and critique in seen[0], "the regeneration prompt goes through condensation"
    assert len(client.calls) == 1
    assert client.calls[0]["prompt"].startswith("condensed regeneration prompt")
