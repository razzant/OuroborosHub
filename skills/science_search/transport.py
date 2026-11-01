"""HTTP plumbing for science_search: one call deadline, typed source failures,
a cross-process per-source throttle and a bounded disk cache.

The throttle lock is held only while waiting for the source's minimum interval
and while the single HTTP request is in flight, never for a whole tool call.
Redirects are never followed: a provider's 3xx is a typed failure, so a granted
API key can never travel to a host that is not on the provider list.
"""
from __future__ import annotations

import datetime
import email.utils
import hashlib
import json
import math
import os
import pathlib
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

import httpx

USER_AGENT = "Ouroboros-science_search/0.1"
REQUEST_TIMEOUT_SEC = 20.0
MAX_BODY_BYTES = 8 * 1024 * 1024
RETRY_BACKOFF_SEC = (0.5, 1.5)
RETRY_AFTER_MAX_SEC = 86400.0
CACHE_MAX_FILES = 400
CACHE_MAX_ENTRY_BYTES = 2 * 1024 * 1024
CACHE_STALE_TEMP_SEC = 600.0
LOCK_POLL_SEC = 0.02

if os.name == "nt":  # pragma: no cover - exercised on Windows hosts only
    import msvcrt

    def _try_lock(handle: Any) -> bool:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(handle: Any) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(handle: Any) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(handle: Any) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def _now() -> float:
    """Monotonic clock behind every deadline (a single seam for tests)."""
    return time.monotonic()


class SourceError(Exception):
    """A typed failure of one source; never a claim that nothing exists."""

    def __init__(self, code: str, message: str = "", *, status: Optional[int] = None,
                 retry_after: Optional[float] = None) -> None:
        super().__init__(message or code)
        self.code, self.message, self.status, self.retry_after = code, message or code, status, retry_after

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": self.code, "message": self.message[:300]}
        if self.status is not None:
            out["http_status"] = self.status
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        return out


class Deadline:
    def __init__(self, seconds: float) -> None:
        self.end = _now() + seconds

    def remaining(self) -> float:
        return self.end - _now()

    def check(self, what: str) -> None:
        if self.remaining() <= 0:
            raise SourceError("deadline_exceeded", f"the call deadline passed before {what}")


class Throttle:
    """Per-source pacing shared by every process: a lock file holding the last request time."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    @contextmanager
    def slot(self, source: str, interval: float, deadline: Deadline) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / f"{source}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as handle:
            while not _try_lock(handle):
                deadline.check(f"the {source} throttle lock was free")
                time.sleep(LOCK_POLL_SEC)
            try:
                handle.seek(0)
                try:
                    last = float(handle.read().strip() or 0.0)
                except ValueError:
                    last = 0.0
                wait = last + interval - time.time()
                if wait > 0:
                    if wait > deadline.remaining():
                        raise SourceError("deadline_exceeded", f"waiting {wait:.1f}s for the {source} rate limit "
                                          "would pass the call deadline")
                    time.sleep(wait)
                try:
                    yield
                finally:
                    handle.seek(0)
                    handle.truncate()
                    handle.write(repr(time.time()))
                    handle.flush()
            finally:
                _unlock(handle)


class Cache:
    """Bounded JSON file cache; entries expire by age and the oldest files are pruned by count."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def _path(self, key: str) -> pathlib.Path:
        return self.root / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")

    def get(self, key: str, ttl: float) -> Any:
        path = self._path(key)
        try:
            if ttl <= 0 or time.time() - path.stat().st_mtime > ttl:
                return None
            return json.loads(path.read_text(encoding="utf-8"))["value"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def discard(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError:
            pass

    def put(self, key: str, value: Any) -> None:
        temporary: Optional[pathlib.Path] = None
        try:
            data = json.dumps({"value": value}, ensure_ascii=False).encode("utf-8")
            if len(data) > CACHE_MAX_ENTRY_BYTES:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._path(key)
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, path)
            temporary = None
            self._prune()
        except OSError:
            return
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _prune(self) -> None:
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for stale in files[: max(0, len(files) - CACHE_MAX_FILES)]:
            stale.unlink(missing_ok=True)
        cutoff = time.time() - CACHE_STALE_TEMP_SEC  # temporaries of a killed process
        for leftover in self.root.glob("*.tmp"):
            try:
                if leftover.stat().st_mtime < cutoff:
                    leftover.unlink(missing_ok=True)
            except OSError:
                continue


def _retry_after(headers: Any) -> Optional[float]:
    """Retry-After as a finite, bounded number of seconds: delta-seconds or an HTTP-date, else None."""
    raw = str(headers.get("retry-after", "") or "").strip()
    if not raw:
        return None
    try:
        delay = float(raw)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        delay = when.timestamp() - time.time()
    if not math.isfinite(delay):
        return None
    return min(max(0.0, delay), RETRY_AFTER_MAX_SEC)


class Http:
    """GET with the shared deadline, short retries, throttling and caching."""

    def __init__(self, state_dir: pathlib.Path, deadline: Deadline, *, mailto: str = "",
                 transport: Any = None) -> None:
        self.deadline = deadline
        self.throttle = Throttle(state_dir / "throttle")
        self.cache = Cache(state_dir / "cache")
        agent = f"{USER_AGENT} (mailto:{mailto})" if mailto else USER_AGENT
        self.client = httpx.Client(transport=transport, follow_redirects=False,
                                   headers={"User-Agent": agent, "Accept": "application/json, application/atom+xml;q=0.9, */*;q=0.5"})

    def close(self) -> None:
        self.client.close()

    def _send(self, source: str, url: str, params: Optional[Dict[str, Any]],
              headers: Optional[Dict[str, str]], request_timeout: float) -> Tuple[int, Any, bytes]:
        """One request within the absolute deadline: the per-read timeout is derived from what is left,
        and every streamed chunk re-checks the deadline so a trickling body cannot outlive the call."""
        self.deadline.check(f"requesting {source}")
        timeout = max(0.1, min(request_timeout, self.deadline.remaining()))
        body = bytearray()
        with self.client.stream("GET", url, params=params, headers=headers, timeout=timeout) as response:
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    raise SourceError("bad_response", f"response larger than {MAX_BODY_BYTES} bytes")
                if self.deadline.remaining() <= 0:
                    raise SourceError("deadline_exceeded", f"the call deadline passed while reading {source}")
            return response.status_code, response.headers, bytes(body)

    def get(self, source: str, url: str, *, params: Optional[Dict[str, Any]] = None,
            headers: Optional[Dict[str, str]] = None, interval: float = 0.0, ttl: float = 0.0,
            parse: str = "json", cache_key_extra: str = "", validate: Optional[Callable[[Any], None]] = None,
            complete: Optional[Callable[[Any], bool]] = None,
            request_timeout: float = REQUEST_TIMEOUT_SEC) -> Tuple[Any, bool]:
        """Return (parsed body, from_cache). Credentials never enter the cache key.

        ``validate`` inspects the parsed body of an HTTP 200 answer and raises SourceError when the
        provider's envelope is wrong (an error object, a missing container); such bodies are never cached.
        ``complete`` tells a valid but incomplete body (some requested items missing): it is returned to the
        caller but not cached. A cached body is checked again by both before use: one stored before a check
        existed (or under a weaker check) is dropped and requested afresh, never served.
        ``request_timeout`` is the per-read timeout of one request, always cut to the remaining deadline.
        """
        public = {k: v for k, v in (params or {}).items() if k not in {"api_key", "mailto", "email", "tool"}}
        key = json.dumps([source, url, sorted(public.items()), cache_key_extra], sort_keys=True, default=str)
        cached = self.cache.get(key, ttl)
        if cached is not None:
            try:
                if validate is not None:
                    validate(cached)
                if complete is None or complete(cached):
                    return cached, True
            except SourceError:
                pass
            self.cache.discard(key)
        attempt = 0
        while True:
            self.deadline.check(f"requesting {source}")
            failure: Optional[SourceError] = None
            with self.throttle.slot(source, interval, self.deadline):
                try:
                    status, response_headers, body = self._send(source, url, params, headers, request_timeout)
                except httpx.TimeoutException:
                    failure = SourceError("timeout", f"{source} did not answer in time")
                except httpx.HTTPError as exc:
                    failure = SourceError("network_error", f"{source}: {type(exc).__name__}")
            if failure is None:
                if status == 200:
                    value = self._parse(source, body, parse)
                    if validate is not None:
                        validate(value)
                    if ttl > 0 and (complete is None or complete(value)):
                        self.cache.put(key, value)
                    return value, False
                if 300 <= status < 400:
                    raise SourceError("bad_response", f"{source} answered with a redirect (HTTP {status}); "
                                      "redirects are not followed", status=status)
                if status == 404:
                    raise SourceError("not_found", f"{source} has no record for this request", status=404)
                if status in (401, 403):
                    raise SourceError("unauthorized", f"{source} refused the request (HTTP {status}); "
                                      "check the API key", status=status)
                if status == 429:
                    failure = SourceError("rate_limited", f"{source} rate limit reached", status=429,
                                          retry_after=_retry_after(response_headers))
                elif status >= 500:
                    failure = SourceError("server_error", f"{source} returned HTTP {status}", status=status)
                else:
                    raise SourceError("bad_request", f"{source} returned HTTP {status}", status=status)
            delay = failure.retry_after if failure.retry_after is not None else (
                RETRY_BACKOFF_SEC[attempt] if attempt < len(RETRY_BACKOFF_SEC) else None)
            if attempt >= len(RETRY_BACKOFF_SEC) or delay is None or delay > self.deadline.remaining():
                raise failure
            time.sleep(delay)
            attempt += 1

    @staticmethod
    def _parse(source: str, body: bytes, parse: str) -> Any:
        text = body.decode("utf-8", errors="replace")
        if parse == "json":
            try:
                return json.loads(text)
            except ValueError:
                raise SourceError("bad_response", f"{source} returned a body that is not JSON") from None
        if parse == "xml" and ("<!DOCTYPE" in text or "<!ENTITY" in text):
            raise SourceError("bad_response", f"{source} returned XML with a document type declaration")
        return text
