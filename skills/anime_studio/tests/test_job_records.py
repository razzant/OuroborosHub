"""Job records written by another payload version stay reachable through the
real consumers (plugin status loader, companion worker) instead of vanishing
as "Job not found"; unknown states read as terminal."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import anime_studio.models as models
import anime_studio.plugin as plugin


def _record(job_id="abc123", *, phase="queued", status="queued", extra=True):
    job = models.Job(job_id=job_id, settings=models.GenerationSettings(theme="t"),
                     created_at="2026-11-16T00:00:00Z")
    data = job.to_dict()
    data["progress"]["phase"] = phase
    data["progress"]["status"] = status
    data["progress"]["storyboard"] = {
        "title": "T", "synopsis": "S", "style": "modern anime", "total_duration_sec": 8,
        "characters": [{"name": "Hero", "description": "d", "visual_traits": "v",
                        "voice_id": "from-a-newer-version"}],
        "locations": [{"name": "Valley", "description": "d", "visual_traits": "v",
                       "lighting": "dawn"}],
        "scenes": [{"index": 1, "description": "s", "duration_sec": 8, "characters": ["Hero"],
                    "location": "Valley", "camera_direction": "static", "shot_id": "x"}],
        "music_cues": [{"segment_index": 0, "mood": "calm", "tempo": "slow",
                        "style": "orchestral", "duration_sec": 8, "description": "d",
                        "stem": "strings"}],
    }
    if extra:
        data["settings"]["new_setting"] = True
    return data


def test_surplus_keys_in_every_nested_record_load():
    job = models.Job.from_dict(_record())
    sb = job.progress.storyboard
    assert sb.characters[0].name == "Hero"
    assert sb.locations[0].name == "Valley"
    assert sb.scenes[0].index == 1
    assert sb.music_cues[0].mood == "calm"
    assert job.progress.status is models.JobStatus.QUEUED


@pytest.mark.parametrize("phase, status", [
    ("normalizing", "queued"), ("normalizing", "running"), ("done", "archived"),
    (None, "queued"), (None, None),
])
def test_unsupported_stored_state_is_terminal_as_a_whole(phase, status):
    data = _record(phase=phase, status=status)
    if phase is None:
        del data["progress"]["phase"]
    if status is None:
        del data["progress"]["status"]
    job = models.Job.from_dict(data)
    assert job.progress.phase is models.JobPhase.ERROR
    assert job.progress.status is models.JobStatus.ERROR, "a queued status beside an unknown phase is not runnable"
    kept = job.progress.verification_stats["unsupported_stored_state"]
    assert kept == {"phase": phase, "status": status}, "the original values stay readable"
    assert "not supported by this payload version" in job.progress.error


def test_known_states_round_trip_unchanged():
    for phase in models.JobPhase:
        for status in models.JobStatus:
            job = models.Job.from_dict(_record(phase=phase.value, status=status.value, extra=False))
            assert job.progress.phase is phase and job.progress.status is status


def test_plugin_status_loader_returns_the_record(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_state_dir", tmp_path)
    job_dir = tmp_path / "jobs" / "abc123"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(
        json.dumps(_record(phase="normalizing", status="queued")), encoding="utf-8",
    )

    job = plugin._load_job("abc123")

    assert job is not None, "an existing job must not read as 'Job not found'"
    payload = plugin._status_payload(job)
    assert payload["status"] == "error", "the widget poller must stop on it"
    assert "normalizing" in payload["error"]
    assert payload["verification_stats"]["unsupported_stored_state"]["status"] == "queued"


def _worker(tmp_path, monkeypatch):
    """The real companion worker module, pointed at a task-local state dir.

    Pipeline and OpenRouterClient are replaced by tripwires: the only way a
    paid dispatch can start is _run_job, which builds both.
    """
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("HOST_SERVICE_TOKEN", raising=False)
    script = Path(plugin.__file__).resolve().parent / "scripts" / "anime_worker.py"
    spec = importlib.util.spec_from_file_location("anime_worker_under_test", script)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    monkeypatch.setattr(worker, "JOBS_DIR", tmp_path / "jobs")

    started: list[str] = []

    def tripwire(*args, **kwargs):
        # Recorded, then refused: _run_job catches exceptions, so an assertion
        # raised here would be swallowed instead of failing the test.
        started.append("paid pipeline construction")
        raise RuntimeError("tripwire: a paid pipeline was started")

    monkeypatch.setattr(worker, "Pipeline", tripwire)
    monkeypatch.setattr(worker, "OpenRouterClient", tripwire)
    worker.paid_starts = started
    return worker


def test_worker_claims_a_queued_job_with_surplus_keys(tmp_path, monkeypatch):
    worker = _worker(tmp_path, monkeypatch)

    job_dir = tmp_path / "jobs" / "abc123"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(json.dumps(_record()), encoding="utf-8")

    job = worker._claim_next_job()

    assert job is not None and job.job_id == "abc123"
    saved = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    assert saved["progress"]["status"] == "running"
    assert (job_dir / "lease.json").exists()


@pytest.mark.parametrize("status, lease", [
    ("queued", None), ("running", None), ("running", "expired"),
])
def test_worker_never_claims_requeues_or_rewrites_an_unsupported_record(
    tmp_path, monkeypatch, status, lease,
):
    worker = _worker(tmp_path, monkeypatch)
    job_dir = tmp_path / "jobs" / "abc123"
    job_dir.mkdir(parents=True)
    original = json.dumps(_record(phase="normalizing", status=status)).encode("utf-8")
    (job_dir / "job.json").write_bytes(original)
    if lease == "expired":
        (job_dir / "lease.json").write_text(json.dumps({
            "pid": 1, "expires_at": "2000-01-01T00:00:00+00:00",
        }), encoding="utf-8")

    claims = []
    for _scan in range(3):  # three worker polls, run the way main() runs them
        claimed = worker._claim_next_job()
        claims.append(claimed)
        if claimed is not None:
            worker._run_job(claimed)

    assert worker.paid_starts == [], "no paid pipeline may start for it"
    assert claims == [None, None, None]
    assert (job_dir / "job.json").read_bytes() == original, "the stored facts are never rewritten"
    if lease is None:
        assert not (job_dir / "lease.json").exists()


def test_worker_still_runs_an_ordinary_stale_running_job_once(tmp_path, monkeypatch):
    worker = _worker(tmp_path, monkeypatch)
    job_dir = tmp_path / "jobs" / "abc123"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(json.dumps(_record(phase="animation", status="running")),
                                      encoding="utf-8")

    job = worker._claim_next_job()  # requeues the dead worker's job ...
    assert job is None
    saved = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    assert saved["progress"]["status"] == "queued"
    job = worker._claim_next_job()  # ... and claims it on the next scan
    assert job is not None and job.progress.status is models.JobStatus.RUNNING
