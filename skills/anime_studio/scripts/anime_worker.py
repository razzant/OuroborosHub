"""Anime Studio companion worker.

The host (server process) spawns and supervises this long-lived process. It owns
the generation pipeline: it polls the skill state dir for QUEUED jobs, runs each
job's pipeline to completion, persists job state to disk, and relays progress to
browser clients through the loopback Host Service `POST /ui/ws-message` bridge.

Why a companion: out-of-process (isolated-dep) extensions dispatch tools/routes in
short-lived per-call children, so a generation thread started inside a route would
die the moment the HTTP response returns. A host-supervised companion is the
persistent execution surface for long-running work (Ouroboros >= v6.15.0).

Env provided by the host:
- OUROBOROS_SKILL_STATE_DIR : the skill's private state dir (job store lives here)
- HOST_SERVICE_URL / HOST_SERVICE_TOKEN : loopback Host Service for WS relay
- OPENROUTER_API_KEY : granted provider key (via manifest env_from_settings)
"""
from __future__ import annotations

import asyncio
import importlib
import ipaddress
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("anime_studio.worker")

STATE_DIR = Path(os.environ.get("OUROBOROS_SKILL_STATE_DIR") or ".").resolve()
JOBS_DIR = STATE_DIR / "jobs"
HOST_SERVICE_URL = (os.environ.get("HOST_SERVICE_URL") or "http://127.0.0.1:8767").rstrip("/")
HOST_SERVICE_TOKEN = os.environ.get("HOST_SERVICE_TOKEN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
POLL_INTERVAL_SEC = 2.0
LEASE_TTL_SEC = 900

_shutdown = threading.Event()

# WS relay failures since the last progress save; surfaced into
# job.progress.verification_stats["ws_relay_failures"] by _on_progress.
_ws_relay_failures = 0
_ws_relay_lock = threading.Lock()


def _is_loopback(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# Import the skill package modules. pipeline.py uses package-relative imports
# (`from .models import ...`), so they must be imported as `<pkg>.pipeline` with
# the skill dir's parent on sys.path. cwd is the skill payload dir; the package
# name is the skill dir's name (e.g. "anime_studio").
_SKILL_DIR = Path(__file__).resolve().parent.parent
_PKG = _SKILL_DIR.name
if str(_SKILL_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR.parent))

_models = importlib.import_module(f"{_PKG}.models")
_api_client = importlib.import_module(f"{_PKG}.api_client")
_pipeline_mod = importlib.import_module(f"{_PKG}.pipeline")

Job = _models.Job
JobStatus = _models.JobStatus
JobPhase = _models.JobPhase
unsupported_stored_state = _models.unsupported_stored_state
OpenRouterClient = _api_client.OpenRouterClient
Pipeline = _pipeline_mod.Pipeline

_HOST_BRIDGE_OK = bool(HOST_SERVICE_TOKEN) and _is_loopback(HOST_SERVICE_URL)


def _status_value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _post_ws(message_type: str, data: dict) -> None:
    """Best-effort WS relay to browser clients via the loopback Host Service."""
    if not _HOST_BRIDGE_OK:
        return
    body = json.dumps({"message_type": message_type, "data": data}).encode("utf-8")
    request = urllib.request.Request(
        f"{HOST_SERVICE_URL}/ui/ws-message",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "x-skill-token": HOST_SERVICE_TOKEN},
    )
    try:
        with urllib.request.urlopen(request, timeout=5):  # noqa: S310 - loopback Host Service
            return
    except Exception as exc:  # noqa: BLE001 - relay is best-effort, but never silent
        global _ws_relay_failures
        with _ws_relay_lock:
            _ws_relay_failures += 1
        log.warning("WS relay to Host Service failed: %s: %s", type(exc).__name__, exc)
        return


def _atomic_write_text(path: Path, text: str, *, fsync: bool = False) -> None:
    """Write via a temp sibling + os.replace, so no reader sees a partial file.

    A direct `write_text` truncates in place: an HTTP status route reading
    job.json concurrently could observe a half-written document, and a lease
    caught mid-rewrite looks unreadable/expired to another worker generation --
    which is exactly the signal that lets it "recover" a job that is very much
    alive and pay for the whole expensive job twice. `fsync` is used for the
    lease because the duplicate-execution guard rests on that durability.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        if fsync:
            os.fsync(handle.fileno())
    os.replace(tmp, path)


def _save_job(job) -> None:
    job_dir = JOBS_DIR / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(job_dir / "job.json", job.to_json())


# --- Job lease (one worker owns one job) -----------------------------------

def _lease_path(job_id: str) -> Path:
    return JOBS_DIR / job_id / "lease.json"


def _lease_payload(acquired_at: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": acquired_at or now.isoformat(),
        "expires_at": (now + timedelta(seconds=LEASE_TTL_SEC)).isoformat(),
        "heartbeat_at": now.isoformat(),
    }


def _read_lease(job_id: str) -> dict | None:
    """Best-effort read of an existing lease; None when absent/unreadable."""
    try:
        return json.loads(_lease_path(job_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - a corrupt lease is diagnostic, not fatal
        log.warning("Unreadable lease for job %s: %s: %s", job_id, type(exc).__name__, exc)
        return None


def _lease_expired(lease: dict | None) -> bool:
    """True when the lease carries no parseable future expiry."""
    if not lease:
        return True
    try:
        expires = datetime.fromisoformat(str(lease.get("expires_at", "")))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)


def _try_acquire_lease(job_id: str) -> bool:
    """Atomically claim a job via O_CREAT|O_EXCL. False = another worker owns it."""
    path = _lease_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (0, 1):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt == 0 and _lease_expired(_read_lease(job_id)):
                # A crash between lease creation and the RUNNING write can leave
                # an expired lease on a QUEUED job; remove it and retry once.
                log.warning("Removing expired leftover lease for job %s", job_id)
                _remove_lease(job_id)
                continue
            return False
        except Exception as exc:  # noqa: BLE001 - claim failure means skip, not crash
            log.warning("Lease acquire failed for job %s: %s: %s", job_id, type(exc).__name__, exc)
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(_lease_payload(), fh)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Lease write failed for job %s: %s: %s", job_id, type(exc).__name__, exc)
            _remove_lease(job_id)
            return False
    return False


def _refresh_lease(job_id: str) -> None:
    """Refresh heartbeat_at/expires_at. Failure is logged, never fatal."""
    try:
        lease = _read_lease(job_id)
        if lease is None:
            # A heartbeat must never mint a claim: creating the lease here
            # would bypass the O_CREAT|O_EXCL acquisition contract.
            log.warning("Lease refresh skipped for job %s: no lease on disk", job_id)
            return
        if lease.get("pid") != os.getpid():
            # Never steal another owner's lease.
            log.warning(
                "Lease refresh skipped for job %s: lease owned by pid %s, not %s",
                job_id,
                lease.get("pid"),
                os.getpid(),
            )
            return
        payload = _lease_payload(acquired_at=lease.get("acquired_at"))
        _atomic_write_text(_lease_path(job_id), json.dumps(payload), fsync=True)
    except Exception as exc:  # noqa: BLE001 - heartbeat loss must not kill the job
        log.warning("Lease refresh failed for job %s: %s: %s", job_id, type(exc).__name__, exc)


def _remove_lease(job_id: str) -> None:
    try:
        os.remove(str(_lease_path(job_id)))
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("Lease removal failed for job %s: %s: %s", job_id, type(exc).__name__, exc)


def _on_progress(job) -> None:
    """Persist job state and relay a namespaced progress event to the browser."""
    global _ws_relay_failures
    with _ws_relay_lock:
        relay_failures = _ws_relay_failures
    if relay_failures:
        job.progress.verification_stats["ws_relay_failures"] = relay_failures
    _refresh_lease(job.job_id)  # progress fires regularly; it doubles as the heartbeat
    _save_job(job)
    p = job.progress
    stats = p.verification_stats or {}
    _post_ws("studio_progress", {
        "job_id": job.job_id,
        "phase": p.phase.value if hasattr(p.phase, "value") else str(p.phase),
        "status": _status_value(p.status),
        "progress_pct": p.progress_pct,
        "message": p.message,
        "character_sheets": p.character_sheets,
        "keyframes": p.keyframes,
        "video_clips": p.video_clips,
        "music_clips": p.music_clips,
        "final_video_url": p.final_video_url,
        "result_download_url": f"/api/extensions/{_PKG}/result?job_id={job.job_id}" if p.final_video_url else "",
        "warnings": p.warnings,
        "has_warnings": bool(p.warnings),
        "warnings_display": [{"key": f"\u26a0\ufe0f {i+1}", "value": w} for i, w in enumerate(p.warnings)] if p.warnings else [],
        "has_verification": bool(stats),
        "verification_display": [
            {"key": "\u2705 Passed", "value": str(stats.get("passed", 0))},
            {"key": "\U0001f504 Retried", "value": str(stats.get("retried", 0))},
            {"key": "\u274c Failed", "value": str(stats.get("failed", 0))},
        ] if stats else [],
    })


# Job ids already logged as unsupported, so a 2-second scan does not repeat it.
_unsupported_logged: set[str] = set()


def _is_unsupported(job_id: str, data: dict) -> bool:
    """True for a record this payload version must neither claim nor requeue.

    The record is left exactly as written (by a newer version, or without a
    phase/status): rewriting it would destroy the only copy of its real state,
    and running it would re-pay for work this version cannot reason about.
    """
    raw = unsupported_stored_state(data.get("progress") or {})
    if raw is None:
        return False
    if job_id not in _unsupported_logged:
        _unsupported_logged.add(job_id)
        log.warning(
            "Job %s has a stored state this payload version does not support "
            "(phase=%r, status=%r); leaving it untouched",
            job_id, raw.get("phase"), raw.get("status"),
        )
    return True


def _recover_stale_running(job_dir: Path, data: dict) -> None:
    """Requeue a RUNNING job whose worker died (lease missing/expired), once.

    A second recovery of the same job means it keeps killing workers (or the
    same crash keeps recurring); mark it ERROR instead of requeueing forever.
    """
    job_id = job_dir.name
    if _is_unsupported(job_id, data):
        return
    lease = _read_lease(job_id)
    if lease is not None and not _lease_expired(lease):
        return  # a live worker owns it
    try:
        job = Job.from_dict(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot deserialize stale RUNNING job %s: %s: %s", job_id, type(exc).__name__, exc)
        return
    if job.progress.status is not JobStatus.RUNNING:
        return  # only the normalized state decides; never requeue anything else
    dead_pid = (lease or {}).get("pid", "unknown")
    expired_at = (lease or {}).get("expires_at", "no lease file")
    stats = job.progress.verification_stats
    recovered = int(stats.get("worker_recovered", 0) or 0)
    if recovered + 1 >= 2:
        job.progress.status = JobStatus.ERROR
        job.progress.phase = JobPhase.ERROR
        job.progress.error = (
            f"Abandoned after {recovered + 1} worker deaths (last lease pid "
            f"{dead_pid}, expired {expired_at}); not requeueing again."
        )
        job.progress.message = job.progress.error
        log.error("Job %s: %s", job_id, job.progress.error)
    else:
        stats["worker_recovered"] = recovered + 1
        job.progress.status = JobStatus.QUEUED
        job.progress.warnings.append(
            f"Requeued after worker death: lease pid {dead_pid} expired at "
            f"{expired_at}; the job was RUNNING with no live worker."
        )
        log.warning(
            "Requeued stale RUNNING job %s (dead pid %s, lease expired %s)",
            job_id, dead_pid, expired_at,
        )
    _remove_lease(job_id)
    _save_job(job)


def _claim_next_job():
    """Return the oldest QUEUED job, atomically claimed (lease + status -> RUNNING).

    The scan also recovers stale RUNNING jobs: a job whose lease file is
    missing or expired has no live worker and is requeued (once) or ERRORed.
    """
    if not JOBS_DIR.is_dir():
        return None
    candidates = []
    for job_dir in JOBS_DIR.iterdir():
        job_file = job_dir / "job.json"
        if not job_file.is_file():
            continue
        try:
            data = json.loads(job_file.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Skipping unreadable job file %s: %s", job_file, exc)
            continue
        status = _status_value((data.get("progress") or {}).get("status"))
        if status == JobStatus.RUNNING.value:
            _recover_stale_running(job_dir, data)
            continue
        if status != JobStatus.QUEUED.value:
            continue
        if _is_unsupported(job_dir.name, data):
            continue
        candidates.append((data.get("created_at") or "", job_dir, data))
    candidates.sort(key=lambda item: item[0])
    for _, job_dir, data in candidates:
        if not _try_acquire_lease(job_dir.name):
            continue  # another worker owns this job; keep scanning
        try:
            job = Job.from_dict(data)
        except Exception as exc:
            log.warning(
                "Failed to deserialize queued job at %s: %s: %s",
                job_dir, type(exc).__name__, exc,
            )
            _remove_lease(job_dir.name)
            continue
        if job.progress.status is not JobStatus.QUEUED:
            _remove_lease(job_dir.name)  # the normalized state is the authority
            continue
        # Claim before running so a restart/second poll does not double-process it.
        job.progress.status = JobStatus.RUNNING
        if job.progress.phase == JobPhase.QUEUED:
            job.progress.phase = JobPhase.SCENARIO
        job.progress.message = job.progress.message or "Picked up by the anime worker…"
        _save_job(job)
        return job
    return None


def _run_job(job) -> None:
    job_dir = JOBS_DIR / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    pipeline = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client = OpenRouterClient(api_key=OPENROUTER_API_KEY, state_dir=job_dir)
        pipeline = Pipeline(
            client=client,
            state_dir=job_dir,
            on_progress=_on_progress,
            shutdown_event=_shutdown,
            lessons_dir=STATE_DIR,       # shared across jobs for progressive learning
            ffmpeg_cache_dir=STATE_DIR,  # legacy arg; ffmpeg is operator-provided (no download)
        )
        loop.run_until_complete(pipeline.run(job))
        _request_agent_attention(job)
    except Exception as exc:  # noqa: BLE001 - record any pipeline failure on the job
        job.progress.status = JobStatus.ERROR
        job.progress.phase = JobPhase.ERROR
        job.progress.error = str(exc)
        job.progress.message = f"Pipeline crashed: {exc}"
        _on_progress(job)
        _request_agent_attention(job)
    finally:
        if pipeline is not None:
            try:
                pipeline.kill_active_processes()
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort, never silent
                log.warning(
                    "kill_active_processes failed for job %s: %s: %s",
                    job.job_id, type(exc).__name__, exc,
                )
        _save_job(job)
        _remove_lease(job.job_id)
        try:
            loop.close()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Event loop close failed for job %s: %s: %s",
                job.job_id, type(exc).__name__, exc,
            )


def _request_agent_attention(job) -> None:
    """Nudge the owner's agent about a PARTIAL/ERROR job — at most once per job.

    Imported lazily so a missing/broken host_bridge can never stop generation;
    host_bridge itself never raises and rate-limits per job id.
    """
    status = _status_value(job.progress.status)
    if status not in (JobStatus.PARTIAL.value, JobStatus.ERROR.value):
        return
    try:
        host_bridge = importlib.import_module(f"{_PKG}.host_bridge")
    except Exception as exc:  # noqa: BLE001 - notification is optional, generation is not
        log.warning("host_bridge import failed: %s: %s", type(exc).__name__, exc)
        return
    try:
        result = host_bridge.request_agent_attention(job.job_id, {
            "status": status,
            "mode": job.settings.quality_mode,
            "missing_scenes": len(job.progress.missing_scenes),
            "unverified_scenes": len(job.progress.unverified_scenes),
            "partial_reasons": list(job.progress.partial_reasons),
        })
    except Exception as exc:  # noqa: BLE001 - host_bridge promises not to raise; belt and braces
        log.warning("request_agent_attention failed: %s: %s", type(exc).__name__, exc)
        return
    log.info("Agent attention request for job %s: %s", job.job_id, result)


def _handle_signal(_signum, _frame) -> None:
    _shutdown.set()


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    while not _shutdown.is_set():
        try:
            job = _claim_next_job()
        except Exception as exc:  # noqa: BLE001 - scan failure: log, back off, keep polling
            log.error("Job scan/claim failed: %s: %s", type(exc).__name__, exc)
            _shutdown.wait(POLL_INTERVAL_SEC)
            continue
        if job is None:
            _shutdown.wait(POLL_INTERVAL_SEC)
            continue
        _run_job(job)


if __name__ == "__main__":
    main()
