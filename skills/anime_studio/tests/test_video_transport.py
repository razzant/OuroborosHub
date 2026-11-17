"""Video job transport: a paid clip survives transient poll/download failures,
liveness is reported during the silent waits, and a provider refusal keeps its
reason. No retry here may POST a second generation."""
from __future__ import annotations

import asyncio

import httpx
import pytest

import anime_studio.api_client as api

CLIP = b"\x00\x00\x00\x18ftypmp42" + b"x" * 64


def _client(tmp_path):
    return api.OpenRouterClient("test-key", tmp_path)


def _video_posts(requests):
    return [r for r in requests if r.method == "POST" and r.url.path.endswith("/videos")]


def _content_gets(requests):
    return [r for r in requests if r.method == "GET" and r.url.path.endswith("/content")]


def _scripted(statuses, downloads):
    """Handler: POST -> job id; status polls and content downloads follow the scripts."""
    statuses = list(statuses)
    downloads = list(downloads)

    def handler(request: httpx.Request):
        path = request.url.path
        if request.method == "POST" and path.endswith("/videos"):
            return httpx.Response(200, json={"id": "job-1"})
        if path.endswith("/content"):
            step = downloads.pop(0)
        else:
            step = statuses.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(request)
        return step

    return handler


def _status(value):
    return httpx.Response(200, json={"status": value})


def _raises(make):
    """A script step that raises the httpx error built by `make(request)`."""
    def step(request):
        raise make(request)
    return step


def _run(coro):
    return asyncio.run(coro)


def test_transient_poll_and_download_failures_keep_the_paid_clip(tmp_path, mock_http, no_sleep):
    timeout = _raises(lambda request: httpx.ReadTimeout("slow", request=request))
    requests = mock_http(_scripted(
        statuses=[
            httpx.Response(503, text="busy"),
            _raises(lambda request: httpx.ConnectError("reset", request=request)),
            _status("processing"),
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, text="not json"),
            _status("completed"),
        ],
        downloads=[timeout, httpx.Response(200, content=CLIP)],
    ))

    path = _run(_client(tmp_path).generate_video(prompt="a scene", filename="scene_1.mp4"))

    assert open(path, "rb").read() == CLIP
    assert len(_video_posts(requests)) == 1, "a poll/download retry must never POST a new generation"
    assert len(_content_gets(requests)) == 2


def test_poll_gives_up_after_bounded_consecutive_transients(tmp_path, mock_http, no_sleep):
    limit = api.VIDEO_POLL_MAX_TRANSIENT
    requests = mock_http(_scripted(
        statuses=[httpx.Response(503, text="busy")] * (limit + 1),
        downloads=[],
    ))

    with pytest.raises(api.VideoJobUnretrieved) as caught:
        _run(_client(tmp_path).generate_video(prompt="a scene", filename="scene_1.mp4"))
    assert caught.value.job_id == "job-1" and caught.value.stage == "poll"

    polls = [r for r in requests if r.method == "GET"]
    assert len(polls) == limit + 1
    assert len(_video_posts(requests)) == 1


def test_successful_poll_resets_the_transient_budget(tmp_path, mock_http, no_sleep):
    limit = api.VIDEO_POLL_MAX_TRANSIENT
    busy = [httpx.Response(503, text="busy")] * limit
    requests = mock_http(_scripted(
        statuses=busy + [_status("processing")] + busy + [_status("completed")],
        downloads=[httpx.Response(200, content=CLIP)],
    ))

    path = _run(_client(tmp_path).generate_video(prompt="a scene", filename="scene_1.mp4"))

    assert open(path, "rb").read() == CLIP
    assert len(_video_posts(requests)) == 1


def test_non_transient_poll_status_raises_at_once(tmp_path, mock_http, no_sleep):
    requests = mock_http(_scripted(
        statuses=[httpx.Response(401, text="unauthorized"), _status("completed")],
        downloads=[httpx.Response(200, content=CLIP)],
    ))

    with pytest.raises(api.VideoJobUnretrieved) as caught:
        _run(_client(tmp_path).generate_video(prompt="a scene", filename="scene_1.mp4"))

    assert caught.value.stage == "poll" and caught.value.job_id == "job-1"
    assert len([r for r in requests if r.method == "GET"]) == 1


def test_empty_download_body_is_retried_and_never_written(tmp_path, mock_http, no_sleep):
    mock_http(_scripted(
        statuses=[_status("completed")],
        downloads=[httpx.Response(200, content=b""), httpx.Response(200, content=CLIP)],
    ))
    path = _run(_client(tmp_path).generate_video(prompt="a scene", filename="scene_1.mp4"))
    assert open(path, "rb").read() == CLIP

    attempts = api.VIDEO_DOWNLOAD_ATTEMPTS
    other = tmp_path / "other"
    other.mkdir()
    requests = mock_http(_scripted(
        statuses=[_status("completed")],
        downloads=[httpx.Response(200, content=b"")] * attempts,
    ))
    with pytest.raises(api.VideoJobUnretrieved) as caught:
        _run(api.OpenRouterClient("test-key", other).generate_video(
            prompt="a scene", filename="scene_2.mp4",
        ))
    assert caught.value.stage == "download" and caught.value.job_id == "job-1"
    assert not (other / "assets" / "scene_2.mp4").exists(), "an empty body must not become a 0-byte MP4"
    assert len(_content_gets(requests)) == attempts
    assert len(_video_posts(requests)) == 1


def test_download_uses_a_long_timeout_client(tmp_path, mock_http, no_sleep):
    seen_timeouts: list[float] = []

    def handler(request: httpx.Request):
        path = request.url.path
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job-1"})
        if path.endswith("/content"):
            seen_timeouts.append(request.extensions["timeout"]["read"])
            return httpx.Response(200, content=CLIP)
        return _status("completed")

    mock_http(handler)
    _run(_client(tmp_path).generate_video(prompt="a scene", filename="scene_1.mp4"))
    assert seen_timeouts and min(seen_timeouts) >= 300


def test_heartbeat_runs_while_polling_and_before_each_download_attempt(
    tmp_path, mock_http, no_sleep, monkeypatch,
):
    monkeypatch.setattr(api, "POLL_HEARTBEAT_INTERVAL", 0.0)
    events: list[str] = []

    def heartbeat():
        events.append("beat")
        if len(events) == 1:
            raise OSError("job.json write failed")  # must be swallowed

    mock_http(_scripted(
        statuses=[_status("processing"), _status("processing"), _status("completed")],
        downloads=[
            httpx.Response(503, text="busy"),
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, content=CLIP),
        ],
    ))
    client = _client(tmp_path)
    client.poll_heartbeat = heartbeat

    path = _run(client.generate_video(prompt="a scene", filename="scene_1.mp4"))

    assert open(path, "rb").read() == CLIP
    # 3 successful polls + 3 download attempts; the first heartbeat raised.
    assert len(events) == 6


def test_heartbeat_is_throttled(tmp_path, mock_http, no_sleep):
    beats: list[int] = []
    mock_http(_scripted(
        statuses=[_status("processing")] * 5 + [_status("completed")],
        downloads=[httpx.Response(200, content=CLIP)],
    ))
    client = _client(tmp_path)
    client.poll_heartbeat = lambda: beats.append(1)
    _run(client.generate_video(prompt="a scene", filename="scene_1.mp4"))
    assert len(beats) == 1, "within one POLL_HEARTBEAT_INTERVAL the worker is told once"


@pytest.mark.parametrize("status, refundable", [
    (400, True), (422, True), (401, False), (402, False), (403, False), (404, False),
    (413, False), (429, False),
])
def test_known_refusal_keeps_provider_reason(tmp_path, mock_http, no_sleep, status, refundable):
    body = '{"error":"invalid params, content[0].text too long: 8640 > 7000 characters"}'
    requests = mock_http(lambda request: httpx.Response(status, text=body))

    with pytest.raises(api.VideoDispatchRejected) as caught:
        _run(_client(tmp_path).generate_video(
            prompt="a scene", filename="scene_1.mp4", model="minimax/hailuo-3",
        ))

    exc = caught.value
    assert exc.status_code == status
    assert exc.refundable is refundable
    assert "text too long" in str(exc) and "text too long" in exc.body
    assert len(requests) == 1


@pytest.mark.parametrize("status", [408, 409, 500, 502, 503, 504])
def test_ambiguous_server_answer_is_an_unknown_outcome_without_an_id(
    tmp_path, mock_http, no_sleep, status,
):
    requests = mock_http(lambda request: httpx.Response(status, text="upstream said something"))

    with pytest.raises(api.VideoJobUnretrieved) as caught:
        _run(_client(tmp_path).generate_video(prompt="a scene", filename="scene_1.mp4"))

    exc = caught.value
    assert exc.job_id is None and exc.stage == "dispatch"
    assert f"HTTP {status}" in exc.detail and "upstream said something" in exc.detail
    assert len(requests) == 1


def test_write_failure_keeps_the_accepted_job_identity(tmp_path, mock_http, no_sleep, monkeypatch):
    mock_http(_scripted(
        statuses=[_status("completed")],
        downloads=[httpx.Response(200, content=CLIP)],
    ))
    real_write = type(tmp_path).write_bytes

    def full_disk(self, data):
        if self.name == "scene_1.mp4":
            raise OSError(28, "No space left on device")
        return real_write(self, data)

    monkeypatch.setattr(type(tmp_path), "write_bytes", full_disk)
    with pytest.raises(api.VideoJobUnretrieved) as caught:
        _run(_client(tmp_path).generate_video(prompt="a scene", filename="scene_1.mp4"))
    assert caught.value.stage == "write" and caught.value.job_id == "job-1"
    assert "No space left" in caught.value.detail


def test_job_still_running_at_the_wait_limit_keeps_its_identity(
    tmp_path, mock_http, no_sleep, monkeypatch,
):
    monkeypatch.setattr(api, "VIDEO_MAX_WAIT", 0.05)
    mock_http(lambda request: (
        httpx.Response(200, json={"id": "job-7"}) if request.method == "POST"
        else _status("processing")
    ))
    with pytest.raises(api.VideoJobUnretrieved) as caught:
        _run(_client(tmp_path).generate_video(prompt="a scene", filename="scene_1.mp4"))
    assert caught.value.stage == "wait" and caught.value.job_id == "job-7"


def test_provider_reported_failure_stays_an_ordinary_error(tmp_path, mock_http, no_sleep):
    mock_http(_scripted(
        statuses=[httpx.Response(200, json={"status": "failed", "error": "content policy"})],
        downloads=[],
    ))
    with pytest.raises(RuntimeError, match="content policy") as caught:
        _run(_client(tmp_path).generate_video(prompt="a scene", filename="scene_1.mp4"))
    assert not isinstance(caught.value, api.VideoJobUnretrieved)
