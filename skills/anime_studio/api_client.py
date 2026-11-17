"""OpenRouter API client for image, video, music, LLM calls, and VLM verification."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import struct
import time
from pathlib import Path, PurePosixPath
from typing import Optional

from io import BytesIO

import httpx
from PIL import Image as _PILImage

logger = logging.getLogger("anime_studio.api")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
VIDEO_POLL_INTERVAL = 5  # seconds
VIDEO_MAX_WAIT = 1200  # 20 min max wait per clip
VLM_MAX_IMAGES = 6
VLM_RETRIES = 2
VLM_RETRY_STATUS = {400, 408, 409, 425, 429, 500, 502, 503, 504}
CHAT_RETRIES = 2

# THE response-token ceiling for EVERY model call this payload makes — chat,
# VLM verification, judge panel, comparisons, condense/simplify/advisor.
#
# WHY ONE CONSTANT AND WHY THIS BIG (owner decision, 2026-08-04). A tight
# max_tokens on a call that must return STRUCTURED output is an anti-pattern,
# not an economy: a reasoning model spends its completion allowance on HIDDEN
# reasoning BEFORE the first character of useful output, so the ceiling runs out
# before the JSON begins. The failure then reads as "the model did not answer"
# rather than "the budget ran out": `max_tokens=2048` on the identity judge was
# reported to return `attributes: {}` on every scene of a live job and an
# unterminated JSON string on the cross-scene check, making the hard identity
# gate STRUCTURALLY unreachable.
#
# The ceiling caps the RESPONSE: only emitted tokens are billed. OpenRouter can
# still refuse a request whose ceiling the remaining credit cannot cover (HTTP
# 402), and chat calls do not retry that. Economy comes from a SHORT prompt and
# a strict response format, never from clipping the answer.
#
# WHY 65536. It is the largest value every text model called here accepts: the
# public OpenRouter catalog (read 2026-11-16) lists max_completion_tokens 65536
# for google/gemini-3.1-pro-preview and google/gemini-3.5-flash and 128000 for
# anthropic/claude-sonnet-4.6. A value above the smallest of them could turn
# every call to that model into a refusal; recheck the catalog before raising it.
#
# It is a single constant on purpose — a number written at each call site
# drifts apart again. Do not re-inline it, and do not lower it because it
# "looks large": that is the defect, not tuning.
DEFAULT_MAX_TOKENS = 65536

# Minimum seconds between liveness heartbeats emitted from inside the video poll
# loop (see generate_video). Well under the worker's lease TTL, well above the
# 5s poll cadence: the point is to prove the worker is alive during a silent
# 20-minute wait, not to write a job file every poll.
POLL_HEARTBEAT_INTERVAL = 60.0
# Transient tolerance around an ALREADY-PAID provider video job. These bound
# recovery, not generation: the provider job id and its content URL stay valid,
# so a retry costs nothing, while raising discards a finished clip and lets the
# caller pay for a second one.
VIDEO_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
VIDEO_POLL_MAX_TRANSIENT = 5  # consecutive, reset by any successful poll
VIDEO_DOWNLOAD_ATTEMPTS = 3

# THE duration contract of one generate_video call. httpx timeouts bound each
# connect/read/write step, not a whole request (a slow trickle can outlive
# them), so every network step also runs under asyncio.wait_for with its own
# total deadline. The sum is the longest the call can run before it raises by
# itself; the pipeline's outer wrapper (TIMEOUT_VIDEO) is derived from it and
# stays above it, so the outer cut can never be what ends an accepted job.
VIDEO_POST_DEADLINE = 120.0          # the dispatch POST, total
VIDEO_POLL_REQUEST_DEADLINE = 60.0   # one status GET, total
VIDEO_DOWNLOAD_TIMEOUT = 300.0       # one download attempt, total (a 2K clip can need minutes)
VIDEO_DOWNLOAD_BUDGET = 600.0        # every download attempt and pause together
VIDEO_DOWNLOAD_RETRY_PAUSE = 2.0     # pause before retry n is n times this, inside the budget
VIDEO_CALL_DEADLINE = (
    VIDEO_POST_DEADLINE
    + VIDEO_MAX_WAIT
    + VIDEO_POLL_INTERVAL
    + VIDEO_POLL_REQUEST_DEADLINE
    + VIDEO_DOWNLOAD_BUDGET
)


# ─── Prompt length: ONE owner for every model's hard limit ──────────
#
# WHY HERE. This module owns the only physical POST to /videos, so a limit
# enforced anywhere else is advice, not a guarantee. A live run reported the
# cost of not having this: minimax/hailuo-3 rejects a request whose prompt
# exceeds 7000 characters ("invalid params, content[0].text too long:
# 8640 > 7000 characters") and the model was absent from the pipeline's own
# limit map entirely, so NO clamp existed for it — every scene that carried a
# continuity anchor was refused with HTTP 400 while the ledger still charged
# for it.
#
# 6800 for hailuo-3: the provider's hard ceiling is 7000, and the single
# prompt reported to succeed live measured 6800, so this is the largest value
# known to pass, with margin for the marker this fitter appends.
VIDEO_MODEL_PROMPT_LIMIT: dict[str, int] = {
    "kwaivgi/kling-v3.0-pro": 2500,
    "kwaivgi/kling-v3.0-std": 2500,
    "kwaivgi/kling-video-o1": 2500,
    "minimax/hailuo-3": 6800,
}

PROMPT_TRUNCATION_MARKER = (
    "\n[…prompt shortened to fit this model's character limit…]\n"
)


class VideoPromptFitError(RuntimeError):
    """The prompt cannot be fitted without dropping a REQUIRED reference token.

    Raised BEFORE any POST. Sending a prompt that no longer names the images it
    attached is the exact defect the owner reported ("the second scene looks
    generated from the text"), so an unfittable prompt is refused rather than
    silently unbound.
    """


class VideoDispatchRejected(RuntimeError):
    """The provider ANSWERED the dispatch with a refusal of the request itself.

    Raised only for KNOWN_REFUSAL_STATUS: answers that refuse the request before
    any job exists (validation, credentials, credits, moderation, missing
    model, size, rate limit). Every other answer status, 408, 409 and 5xx in
    particular, can sit on top of an accepted job and is VideoJobUnretrieved.

    `refundable` is derived from the status code in ONE place, never by keyword
    matching at each call site: only a 400/422 structural validation refusal
    is credited back. The other known refusals keep their charge; the ledger
    is an approximate estimate and may over-count, never the reverse.
    """

    REFUNDABLE_STATUS = frozenset({400, 422})
    KNOWN_REFUSAL_STATUS = frozenset({400, 401, 402, 403, 404, 413, 422, 429})

    def __init__(self, status_code: int, body: str, model: str = ""):
        self.status_code = int(status_code)
        self.body = body
        self.model = model
        self.refundable = self.status_code in self.REFUNDABLE_STATUS
        super().__init__(
            f"video dispatch rejected by {model or 'provider'}: "
            f"HTTP {self.status_code}: {body}"
        )


class VideoGenerationFailed(RuntimeError):
    """The provider reported the accepted job itself as failed (a definite outcome)."""


class VideoJobUnretrieved(RuntimeError):
    """A paid dispatch the provider accepted, or may have accepted, whose clip
    did not reach disk here.

    `job_id` is the provider's id when its acceptance was seen. It is None at
    the "dispatch" stage, when the POST got no usable answer (transport error,
    deadline, dropped or malformed answer, ambiguous server status): sent bytes
    may have created a job all the same, and no id is ever invented for it.
    Either way the job may exist and be billed, so this is never refundable and
    the caller must not answer it with a fresh paid generation for the scene.
    """

    def __init__(self, job_id: Optional[str], model: str, stage: str, detail: str):
        self.job_id = str(job_id) if job_id not in (None, "") else None
        self.model = model
        self.stage = stage
        self.detail = detail
        if self.job_id is None:
            text = (
                f"video dispatch to {model or 'provider'} has an unknown outcome at the "
                f"{stage} stage (no job id came back; it may have been accepted): {detail}"
            )
        else:
            text = (
                f"provider accepted video job {self.job_id} ({model or 'provider'}) "
                f"but the clip was not retrieved at the {stage} stage: {detail}"
            )
        super().__init__(text)


def _answer_snippet(resp, limit: int = 300) -> str:
    try:
        return resp.text[:limit]
    except Exception:
        return "<response body unavailable>"


async def _close_quietly(client, job_id) -> None:
    """Close an httpx client without letting teardown discard a known outcome."""
    try:
        await client.aclose()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "video client close failed for job %s: %s: %s", job_id, type(exc).__name__, exc,
        )


DIALOGUE_HEADER = "\n\nSPOKEN DIALOGUE"
# Blocks the pipeline appends AFTER the dialogue. They are advice for the
# model, not speech, so they never ride inside the protected dialogue block.
_AFTER_DIALOGUE_HEADERS = (
    "\n\nLEARNED FROM PREVIOUS GENERATIONS",
    "\n\nCRITICAL FIXES REQUIRED",
    "\n\nADDITIONAL NEGATIVE CONSTRAINTS",
)


def split_protected_dialogue(text: str, room: int) -> tuple[str, str]:
    """Split *text* into (cuttable body, protected tail) for a prompt fitter.

    The tail is everything from the SPOKEN DIALOGUE header to the end when that
    fits in *room*. Otherwise it is the dialogue block alone, and the lessons
    and fix notes that followed it go back into the cuttable body, so a long
    lessons block can no longer drag the dialogue out of the prompt with it.
    When even the dialogue block does not fit, nothing is protected.
    """
    idx = text.find(DIALOGUE_HEADER)
    if idx == -1 or room <= 0:
        return text, ""
    remainder = text[idx:]
    if len(remainder) < room:
        return text[:idx], remainder
    ends = [remainder.find(h, len(DIALOGUE_HEADER)) for h in _AFTER_DIALOGUE_HEADERS]
    end = min([e for e in ends if e != -1], default=len(remainder))
    dialogue = remainder[:end]
    if len(dialogue) < room:
        return text[:idx] + remainder[end:], dialogue
    return text, ""


def video_prompt_limit(model: str) -> Optional[int]:
    """The hard prompt character limit for *model*, or None when unknown."""
    return VIDEO_MODEL_PROMPT_LIMIT.get(model)


def expected_reference_tokens(reference_count: int) -> tuple[str, ...]:
    """The @ImageN tokens a prompt MUST name for *reference_count* references.

    Derived from the same count `_describe_video_request` audits, so the fitter
    protects exactly the tokens the audit checks for.
    """
    return tuple(f"@Image{n}" for n in range(1, max(0, int(reference_count)) + 1))


def fit_prompt_to_limit(
    prompt: str, limit: int, required_tokens: tuple[str, ...] = ()
) -> str:
    """Fit *prompt* to *limit* characters, disclosing the loss inside the text.

    Two kinds of content are protected rather than tail-cut, because a bare
    `prompt[:limit]` silently deletes whatever was appended LAST:

      * every line naming a required @ImageN token — the reference binding;
      * the SPOKEN DIALOGUE block, when it still leaves room for the scene
        (see split_protected_dialogue: the lessons after it are not speech).

    Fails CLOSED (`VideoPromptFitError`) when the protected reference lines plus
    the disclosure marker cannot themselves fit: an unbound prompt must never be
    dispatched.
    """
    if len(prompt) <= limit:
        return prompt

    lines = prompt.split("\n")
    protected_idx = {
        i for i, line in enumerate(lines)
        if any(tok in line for tok in required_tokens)
    }
    required_block = "\n".join(lines[i] for i in sorted(protected_idx))
    marker = PROMPT_TRUNCATION_MARKER

    reserved = len(marker) + len(required_block)
    if reserved >= limit:
        raise VideoPromptFitError(
            f"reference binding needs {reserved} chars but the model limit is "
            f"{limit}; refusing to dispatch a prompt that would drop "
            f"{len(required_tokens)} reference token(s)"
        )

    body = "\n".join(lines[i] for i in range(len(lines)) if i not in protected_idx)

    # Keep the dialogue only while the head still gets meaningful room:
    # protecting a tail that eats the whole remaining budget would silently
    # delete the scene it is supposed to be spoken in.
    had_dialogue = DIALOGUE_HEADER in body
    body, tail = split_protected_dialogue(body, (limit - reserved) // 2)

    head_room = limit - reserved - len(tail)
    if head_room <= 0:
        head_room = 0
    fitted = body[:head_room] + marker + required_block + tail

    missing = [tok for tok in required_tokens if tok not in fitted]
    if missing:
        raise VideoPromptFitError(
            f"prompt fit lost reference token(s) {missing}; refusing dispatch"
        )
    if len(fitted) > limit:
        raise VideoPromptFitError(
            f"prompt fit produced {len(fitted)} chars over the {limit} limit"
        )
    logger.warning(
        "Prompt fitted: %d -> %d chars (limit %d); %d reference line(s) kept, "
        "dialogue block: %s",
        len(prompt), len(fitted), limit, len(protected_idx),
        "kept" if tail else ("dropped" if had_dialogue else "none in prompt"),
    )
    return fitted


def _describe_media_entry(entry) -> dict:
    """Redacted description of ONE input_references / frame_images entry.

    Deliberately carries no payload: a base64 data: URL is many megabytes of
    image and must never reach a log. What survives is exactly enough to prove
    an image of a given size really was attached, plus a sha256 prefix so the
    same asset can be recognised across scenes without revealing it.
    """
    if not isinstance(entry, dict):
        return {"type": "malformed", "url_scheme": "", "bytes": 0}
    url = ((entry.get("image_url") or {}).get("url") or "") if isinstance(
        entry.get("image_url"), dict
    ) else ""
    out = {
        "type": entry.get("type") or ("frame" if entry.get("frame_type") else "unknown"),
        "url_scheme": url.split(":", 1)[0].lower() if ":" in url else "",
        "bytes": len(url),
    }
    if entry.get("frame_type"):
        out["frame_type"] = entry["frame_type"]
    if url.startswith("data:"):
        header, _, b64 = url.partition(",")
        out["mime"] = header[5:].split(";", 1)[0]
        out["bytes"] = len(b64)
        out["sha256_prefix"] = hashlib.sha256(b64.encode()).hexdigest()[:12]
    elif url:
        # Never log the URL itself — it can carry a signed token.
        out["sha256_prefix"] = hashlib.sha256(url.encode()).hexdigest()[:12]
    return out


def _describe_video_request(payload: dict) -> dict:
    """Redacted, host-loggable summary of a /videos request body.

    The owner asked for proof that the character sheet and the previous approved
    frame are REALLY in the request rather than a claim that the source code
    says so. This is that proof, and it is bound to the actual dict about to be
    POSTed rather than to the intent that built it.

    The prompt itself is NOT logged: it contains owner-authored theme and
    dialogue text, so bounding it would not make it redacted. Its length, a
    digest, and a structural check that the @ImageN tokens are present are
    enough to verify the binding without disclosing the story.
    """
    prompt = payload.get("prompt") or ""
    refs = payload.get("input_references") or []
    frames = payload.get("frame_images") or []
    expected_tokens = [f"@Image{n}" for n in range(1, len(refs) + 1)]
    return {
        "model": payload.get("model"),
        "duration": payload.get("duration"),
        "resolution": payload.get("resolution"),
        "aspect_ratio": payload.get("aspect_ratio"),
        "generate_audio": payload.get("generate_audio"),
        "prompt_chars": len(prompt),
        "prompt_sha256_prefix": hashlib.sha256(prompt.encode()).hexdigest()[:12],
        "input_references_count": len(refs),
        "input_references": [_describe_media_entry(r) for r in refs],
        "frame_images_count": len(frames),
        "frame_images": [_describe_media_entry(f) for f in frames],
        # Structural check, not a quality judgement: did the prompt actually
        # name every reference it attached? A False here is the exact defect
        # that made a scene look text-generated.
        "prompt_names_all_references": bool(expected_tokens) and all(
            tok in prompt for tok in expected_tokens
        ),
    }


def _safe_filename(filename: str) -> str:
    """Sanitize a filename to prevent path traversal."""
    name = PurePosixPath(filename).name
    name = name.split("\\")[-1]
    name = name.replace("..", "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    if not name or name.startswith("."):
        name = f"asset_{int(time.time())}.bin"
    return name


# ─── Timeout Helper ─────────────────────────────────────────────────


async def run_with_timeout(coro, timeout_sec: float, description: str = "operation"):
    """Wrap a coroutine with a timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout_sec)
    except asyncio.TimeoutError:
        raise TimeoutError(f"{description} timed out after {timeout_sec}s")


# ─── Client ─────────────────────────────────────────────────────────


class OpenRouterClient:
    """Async client for all OpenRouter API interactions."""

    # Optional zero-argument liveness hook, set by the caller that owns the job
    # (Pipeline.run). Declared here rather than left implicit so the contract is
    # visible: it is called while a video job is being polled and downloaded,
    # throttled to POLL_HEARTBEAT_INTERVAL, and its exceptions are swallowed.
    # Default None = no heartbeat, which is exactly the pre-existing behaviour.
    poll_heartbeat = None
    _last_poll_heartbeat = 0.0

    def __init__(self, api_key: str, state_dir: Path):
        self.api_key = api_key
        self.state_dir = state_dir
        self.assets_dir = state_dir / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)

    def _poll_heartbeat_tick(self) -> None:
        """Throttled liveness signal during the silent part of a video job.

        The worker refreshes its job lease only from the progress callback, and
        its lease TTL (900s) is shorter than one video wait (VIDEO_MAX_WAIT) and
        than the download budget. Without a signal from inside these waits a
        live worker's lease could expire, another worker generation would
        requeue the job and pay for it again. A heartbeat must never abort a
        paid generation, so its failures are logged and swallowed.
        """
        heartbeat = getattr(self, "poll_heartbeat", None)
        if heartbeat is None:
            return
        now = time.time()
        if now - self._last_poll_heartbeat < POLL_HEARTBEAT_INTERVAL:
            return
        self._last_poll_heartbeat = now
        try:
            heartbeat()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"poll heartbeat failed: {type(e).__name__}: {e}")

    def _headers(self) -> dict:
        auth_value = f"Bearer {self.api_key}"
        return {
            "Authorization": auth_value,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://ouroboros.local",
            "X-Title": "Anime Studio",
        }

    # ─── Robust JSON / VLM Helpers ─────────────────────────────────

    @staticmethod
    def _strip_json_fences(text: str) -> str:
        text = (text or "").strip()
        if not text.startswith("```"):
            return text
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return text.strip()

    @staticmethod
    def _extract_balanced_json_object(text: str) -> str:
        """Extract the first balanced JSON object from a model response."""
        text = OpenRouterClient._strip_json_fences(text)
        for start, char in enumerate(text):
            if char != "{":
                continue
            depth = 0
            in_string = False
            escaped = False
            for idx in range(start, len(text)):
                ch = text[idx]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start:idx + 1]
        return text

    def parse_json_response(self, text: str) -> dict:
        """Parse a JSON object from raw LLM output with fence/object fallback."""
        stripped = self._strip_json_fences(text)
        try:
            data = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            data = json.loads(self._extract_balanced_json_object(stripped))
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            data = data[0]
        if not isinstance(data, dict):
            raise ValueError("LLM response JSON root is not an object")
        return data

    def _vlm_image_parts(self, image_paths: list[str]) -> list[dict]:
        parts: list[dict] = []
        for path in image_paths[:VLM_MAX_IMAGES]:
            try:
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": self.get_image_url(path, compress=True)},
                })
            except Exception as e:
                logger.warning(f"Failed to load VLM image {path}: {e}")
        return parts

    async def _vlm_json_request(
        self,
        *,
        label: str,
        model: str,
        content_parts: list[dict],
        default: dict,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict:
        """Post a VLM request and parse JSON robustly; return default on failure."""
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content_parts}],
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=90) as client:
            for attempt in range(VLM_RETRIES + 1):
                try:
                    resp = await client.post(
                        f"{OPENROUTER_BASE}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    )
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        last_error = exc
                        status = exc.response.status_code
                        body = exc.response.text[:300]
                        logger.warning(f"{label} VLM HTTP {status}: {body}")
                        if attempt < VLM_RETRIES and status in VLM_RETRY_STATUS:
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        break
                    data = resp.json()
                    if not data.get("choices"):
                        last_error = RuntimeError(f"no choices in response: {data.get('error', {})}")
                        if attempt < VLM_RETRIES:
                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                        break
                    text = data["choices"][0]["message"]["content"]
                    return self.parse_json_response(text)
                except (json.JSONDecodeError, ValueError) as exc:
                    last_error = exc
                    logger.warning(f"{label} VLM returned non-JSON: {exc}")
                    if attempt < VLM_RETRIES:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    break
                except Exception as exc:
                    last_error = exc
                    logger.warning(f"{label} VLM call failed: {exc}")
                    if attempt < VLM_RETRIES:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    break
        result = dict(default)
        if "vlm_error" in result:
            result["vlm_error"] = True
        if last_error and "issues" in result:
            result["issues"] = [f"VLM error: {last_error}"]
        elif last_error and "reason" in result and not result.get("reason"):
            result["reason"] = f"VLM error: {last_error}"
        return result

    # ─── LLM Chat ───────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict],
        model: str = "anthropic/claude-sonnet-4.6",
        max_toks: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.7,
        json_mode: bool = False,
    ) -> str:
        """Standard LLM chat completion. Returns content text.

        Args:
            json_mode: If True, sets response_format={"type":"json_object"} so the
                model is guaranteed to return valid JSON. Use for structured storyboard
                and scenario generation calls.
        """
        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_toks,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=300) as client:
            for attempt in range(CHAT_RETRIES + 1):
                try:
                    resp = await client.post(
                        f"{OPENROUTER_BASE}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if not data.get("choices"):
                        raise RuntimeError(f"No choices in LLM response: {data.get('error', {})}")
                    content = (data["choices"][0]["message"].get("content") or "").strip()
                    if not content:
                        raise RuntimeError("Empty LLM response content")
                    if json_mode:
                        self.parse_json_response(content)
                    return content
                except Exception as exc:
                    last_error = exc
                    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    should_retry = status is None or status in VLM_RETRY_STATUS
                    if attempt < CHAT_RETRIES and should_retry:
                        logger.warning(f"LLM chat retry {attempt + 1} for {model}: {exc}")
                        await asyncio.sleep(1.0 * (attempt + 1))
                        continue
                    break
        raise last_error or RuntimeError("LLM chat failed")

    # ─── Image Generation (GPT-Image-2) ────────────────────────────

    async def generate_image(
        self,
        prompt: str,
        filename: str,
        aspect_ratio: str = "1:1",
        size: str = "auto",
        model: str = "openai/gpt-image-2",
    ) -> str:
        """Generate image via OpenRouter chat completions (gpt-image-2 or gpt-5.4-image-2).

        Uses /chat/completions with modalities=["image","text"] — the OpenRouter-supported
        path for OpenAI image models. Pass model="openai/gpt-image-2" for the latest model
        or "openai/gpt-5.4-image-2" for the previous generation.
        """
        filename = _safe_filename(filename)
        async with httpx.AsyncClient(timeout=360) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers=self._headers(),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "modalities": ["image", "text"],
                },
            )
            resp.raise_for_status()
            data = resp.json()

        image_b64 = self._extract_image_b64(data)
        if not image_b64:
            choices = data.get("choices", [])
            detail = list(choices[0]["message"].keys()) if choices else ["(empty choices)"]
            logger.error(f"No image data in response for {filename}. keys: {detail}")
            raise RuntimeError(f"No image data in response for {filename}")

        filepath = self.assets_dir / filename
        filepath.write_bytes(base64.b64decode(image_b64))
        logger.info(f"Generated image (gpt-image-2): {filepath}")
        return str(filepath)

    # ─── Image Generation (GPT-Image-2 native via Images API) ──────

    # Maps common aspect-ratio strings to gpt-image-2 size strings.
    # gpt-image-2 accepts arbitrary WxH divisible by 16 in the [1:3, 3:1] range.
    _GPT_IMAGE_ASPECT_SIZE: dict[str, str] = {
        "16:9": "1344x768",
        "9:16": "768x1344",
        "1:1":  "1024x1024",
        "4:3":  "1024x768",
        "3:4":  "768x1024",
    }

    async def generate_image_gpt(
        self,
        prompt: str,
        filename: str,
        aspect_ratio: str = "16:9",
    ) -> str:
        """Generate image via gpt-image-2 using the native OpenAI Images API.

        Uses /v1/images/generations with model=gpt-image-2.
        Returns local file path. Response is always b64_json for this model.
        """
        filename = _safe_filename(filename)
        size = self._GPT_IMAGE_ASPECT_SIZE.get(aspect_ratio, "1344x768")
        async with httpx.AsyncClient(timeout=360) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/images/generations",
                headers=self._headers(),
                json={
                    "model": "openai/gpt-image-2",
                    "prompt": prompt,
                    "n": 1,
                    "size": size,
                    "quality": "medium",
                    "output_format": "png",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        # gpt-image-2 always returns b64_json, never a URL
        image_b64 = None
        for item in data.get("data", []):
            b64 = item.get("b64_json") or item.get("url", "")
            if b64.startswith("data:"):
                b64 = b64.split(",", 1)[1]
            if b64 and not b64.startswith("http"):
                image_b64 = b64
                break

        if not image_b64:
            logger.error(f"No b64_json in gpt-image-2 response for {filename}: {data}")
            raise RuntimeError(f"No image data in gpt-image-2 response for {filename}")

        filepath = self.assets_dir / filename
        filepath.write_bytes(base64.b64decode(image_b64))
        logger.info(f"Generated image (gpt-image-2 native): {filepath}")
        return str(filepath)

    # ─── Image Generation (Nanobanana / Gemini) ─────────────────────

    async def generate_image_nanobanana(
        self,
        prompt: str,
        filename: str,
        aspect_ratio: str = "1:1",
    ) -> str:
        """Generate image via Nanobanana (google/gemini-3.1-flash-image-preview).
        Returns local file path.
        """
        filename = _safe_filename(filename)
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers=self._headers(),
                json={
                    "model": "google/gemini-3.1-flash-image-preview",
                    "messages": [{"role": "user", "content": prompt}],
                    "modalities": ["image", "text"],
                },
            )
            resp.raise_for_status()
            data = resp.json()

        image_b64 = self._extract_image_b64(data)
        if not image_b64:
            raise RuntimeError(f"No image data in Nanobanana response for {filename}")

        filepath = self.assets_dir / filename
        filepath.write_bytes(base64.b64decode(image_b64))
        logger.info(f"Generated image (nanobanana): {filepath}")
        return str(filepath)

    # ─── VLM Verification (Image) ──────────────────────────────────

    async def verify_image_vlm(
        self,
        image_path: str,
        original_prompt: str,
        character_ref_description: str = "None provided",
    ) -> dict:
        """Use a VLM to verify a generated image matches specifications.
        Returns {passed: bool, issues: [...], suggestion: str}
        """
        from .prompts import VLM_VERIFY_IMAGE_PROMPT

        verify_prompt = VLM_VERIFY_IMAGE_PROMPT.format(
            original_prompt=original_prompt,
            character_ref_description=character_ref_description,
        )
        content_parts = [{"type": "text", "text": verify_prompt}, *self._vlm_image_parts([image_path])]
        if len(content_parts) < 2:
            return {"passed": True, "issues": [], "suggestion": "", "vlm_error": True}
        return await self._vlm_json_request(
            label="image verification",
            model="anthropic/claude-sonnet-4.6",
            content_parts=content_parts,
            max_tokens=DEFAULT_MAX_TOKENS,
            default={"passed": False, "issues": [], "suggestion": "", "vlm_error": True},
        )

    # ─── VLM Compare Two Images ─────────────────────────────────────

    async def compare_images_vlm(
        self,
        image_path_1: str,
        image_path_2: str,
        prompt: str,
    ) -> dict:
        """Compare two images via VLM. Returns parsed JSON from the model."""
        images = self._vlm_image_parts([image_path_1, image_path_2])
        if len(images) < 2:
            return {"winner": 1, "reason": "comparison skipped, defaulting to first"}
        return await self._vlm_json_request(
            label="image comparison",
            model="anthropic/claude-sonnet-4.6",
            content_parts=[
                {"type": "text", "text": prompt},
                {"type": "text", "text": "Image 1:"},
                images[0],
                {"type": "text", "text": "Image 2:"},
                images[1],
            ],
            max_tokens=DEFAULT_MAX_TOKENS,
            default={"winner": 1, "reason": "comparison failed, defaulting to first"},
        )

    # ─── VLM Multi-Image Analysis ───────────────────────────────────

    async def analyze_multi_image_vlm(
        self,
        image_paths: list[str],
        prompt: str,
        model: str = "google/gemini-3.1-pro-preview",
    ) -> dict:
        """Send multiple images to a VLM for cross-frame/cross-scene analysis.

        The default carries `vlm_error: False` DELIBERATELY: `_vlm_json_request`
        only flips a failure marker that already exists in the default, and
        without it a total transport/parse failure returned the optimistic
        `consistent: True, severity: "none"` body — so callers scored a check
        that never ran as a clean pass. Callers MUST branch on `vlm_error`.
        A successful call returns the model's own parsed JSON, which carries no
        `vlm_error` key at all, so the marker can only ever mean "this failed".
        """
        content_parts = [{"type": "text", "text": prompt}, *self._vlm_image_parts(image_paths)]

        failed = {
            "consistent": True,
            "worst_scene_index": None,
            "drift_description": "",
            "severity": "none",
            "vlm_error": False,
            "reason": "",
        }

        if len(content_parts) < 2:
            unusable = dict(failed)
            unusable["vlm_error"] = True
            unusable["reason"] = "no usable images could be attached to the VLM request"
            return unusable

        return await self._vlm_json_request(
            label="multi-image analysis",
            model=model,
            content_parts=content_parts,
            max_tokens=DEFAULT_MAX_TOKENS,
            default=failed,
        )

    # NOTE: `verify_video_vlm` was DELETED here (v3.1). It was unreachable — the
    # live path is Pipeline._verify_video_multidim -> analyze_multi_image_vlm —
    # and it returned {"passed": True, "score": 7} when it had no frames or could
    # not attach images: "could not check" rendered as "checked and passed",
    # exactly the fail-open shape the rest of this payload was rewritten to
    # remove. Dead code with a false-green default is a landmine for the next
    # caller, so it is gone rather than merely unused. VLM_VERIFY_VIDEO_PROMPT
    # went with it.

    # ─── Video Generation ───────────────────────────────────────────

    async def generate_video(
        self,
        prompt: str,
        filename: str,
        duration: int = 8,
        resolution: str = "720p",
        aspect_ratio: str = "16:9",
        input_references: Optional[list[dict]] = None,
        frame_images: Optional[list[dict]] = None,
        model: str = "bytedance/seedance-2.0",
        generate_audio: bool = True,
        audit_sink=None,
    ) -> str:
        """Generate video via OpenRouter /videos API. Returns local file path.

        Args:
            frame_images: Hard anchor frames. Example:
                [{"image_url": {"url": "data:..."}, "frame_type": "first_frame"}]
                When provided, this becomes an image-to-video generation with hard
                visual conditioning on the first/last frame.
            input_references: Soft visual guidance images (character sheets, etc.)
            audit_sink: Optional callable invoked with a REDACTED summary of the
                request body immediately BEFORE the physical POST. Called before
                the network call on purpose, so the evidence survives a request
                that raises; and passed per call rather than stored on self, so
                candidate retries, advisor retries, model switches and
                regeneration cannot overwrite each other's record.
        """
        filename = _safe_filename(filename)

        # LAST-CHANCE prompt fit, applied to the FINAL text immediately before
        # the body is built. Callers may fit earlier (the pipeline condenses via
        # an LLM), but only this point sees what is actually sent: a caller that
        # clamps and THEN appends a continuity anchor was reported to send an
        # 8461-character prompt against a 7000-character ceiling and lose the
        # whole scene to an HTTP 400. The reference tokens are protected, so a fit can shorten the
        # description but can never unbind the images.
        _limit = video_prompt_limit(model)
        _prompt_chars_before = len(prompt)
        _required_tokens = expected_reference_tokens(len(input_references or []))
        _fit_applied = False
        if _limit and _prompt_chars_before > _limit:
            prompt = fit_prompt_to_limit(prompt, _limit, _required_tokens)
            _fit_applied = True

        payload: dict = {
            "model": model,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "generate_audio": generate_audio,
        }
        if frame_images:
            payload["frame_images"] = frame_images
        if input_references:
            payload["input_references"] = input_references

        request_audit = _describe_video_request(payload)
        request_audit["prompt_limit"] = _limit
        request_audit["prompt_chars_before_fit"] = _prompt_chars_before
        request_audit["prompt_fit_applied"] = _fit_applied
        logger.info("VIDEO_REQUEST_BODY %s", json.dumps(request_audit, sort_keys=True))
        if audit_sink is not None:
            try:
                audit_sink(request_audit)
            except Exception as e:  # never let auditing break a paid generation
                logger.warning(f"video request audit sink failed: {type(e).__name__}: {e}")

        # Building the client sends nothing; from the first byte of the POST on,
        # a failure says nothing about the provider side.
        client = httpx.AsyncClient(timeout=30)
        resp = None
        try:
            async with client:
                resp = await asyncio.wait_for(
                    client.post(
                        f"{OPENROUTER_BASE}/videos",
                        headers=self._headers(),
                        json=payload,
                    ),
                    timeout=VIDEO_POST_DEADLINE,
                )
        except Exception as exc:  # noqa: BLE001
            if resp is None:
                # Transport error, dropped answer or deadline: the request may
                # have been received and a job created. Unknown outcome, no id,
                # no refund, and nothing here sends it again. A connection error
                # is not taken as proof that no bytes left.
                reason = (
                    f"no answer within {VIDEO_POST_DEADLINE:.0f}s"
                    if isinstance(exc, asyncio.TimeoutError)
                    else f"{type(exc).__name__}: {exc}"
                )
                raise VideoJobUnretrieved(None, model, "dispatch", reason) from exc
            logger.warning(
                "video dispatch client teardown failed after the answer: %s: %s",
                type(exc).__name__, exc,
            )
        if resp.status_code >= 400:
            # The BODY is the only place the provider says WHY. Discarding it
            # left a bare HTTPStatusError in the job, and the real cause
            # ("content[0].text too long: 8640 > 7000 characters") had to be
            # recovered later by an external probe. Bounded and secret-free:
            # this is the provider's own validation text, not our request.
            body = _answer_snippet(resp, 1500)
            if resp.status_code in VideoDispatchRejected.KNOWN_REFUSAL_STATUS:
                raise VideoDispatchRejected(resp.status_code, body, model)
            raise VideoJobUnretrieved(
                None, model, "dispatch", f"provider answered HTTP {resp.status_code}: {body}",
            )
        try:
            job_data = resp.json() if 200 <= resp.status_code < 300 else None
        except ValueError:
            job_data = None
        job_id = (job_data.get("id") or job_data.get("jobId")) if isinstance(job_data, dict) else None
        if isinstance(job_id, bool) or not isinstance(job_id, (str, int)) or not str(job_id).strip():
            # An answer without a usable id (not JSON, not an object, no id,
            # an unexpected status) may still stand for an accepted job.
            raise VideoJobUnretrieved(
                None, model, "dispatch",
                f"HTTP {resp.status_code} answer carried no usable job id: {_answer_snippet(resp)}",
            )
        job_id = str(job_id)

        # From here on the provider has ACCEPTED the job. Every failure below,
        # except a provider-reported generation failure, is VideoJobUnretrieved
        # with this id: the clip may exist and be billed.
        filepath = self.assets_dir / filename
        try:
            return await self._await_accepted_job(job_id, model, filepath)
        except (VideoJobUnretrieved, VideoGenerationFailed):
            raise
        except Exception as exc:  # noqa: BLE001
            # Client setup, an unexpected answer shape, anything unforeseen:
            # the id must survive instead of reaching the advisor as an
            # ordinary failure that invites a second paid clip.
            raise VideoJobUnretrieved(
                job_id, model, "poll", f"{type(exc).__name__}: {exc}",
            ) from exc

    async def _await_accepted_job(self, job_id: str, model: str, filepath: Path) -> str:
        """Poll an accepted job until it completes, then fetch its clip."""
        start = time.time()
        transient_failures = 0
        client = httpx.AsyncClient(timeout=30)
        try:
            while time.time() - start < VIDEO_MAX_WAIT:
                await asyncio.sleep(VIDEO_POLL_INTERVAL)
                # A POLL is not the generation. The clip is ALREADY being made and
                # billed by the provider; `job_id` stays valid, so re-polling after
                # a transient failure costs nothing. A non-transient status (auth,
                # 404, malformed request) ends the wait at once because re-polling
                # it would only burn the window.
                try:
                    resp = await asyncio.wait_for(
                        client.get(
                            f"{OPENROUTER_BASE}/videos/{job_id}",
                            headers=self._headers(),
                        ),
                        timeout=VIDEO_POLL_REQUEST_DEADLINE,
                    )
                    resp.raise_for_status()
                    status_data = resp.json()
                    if not isinstance(status_data, dict):
                        raise ValueError(
                            f"status answer is not a JSON object: {str(status_data)[:200]}"
                        )
                except (
                    httpx.HTTPStatusError, httpx.RequestError, ValueError, asyncio.TimeoutError,
                ) as exc:
                    status_code = (
                        exc.response.status_code
                        if isinstance(exc, httpx.HTTPStatusError)
                        else None
                    )
                    if status_code is not None and status_code not in VIDEO_RETRY_STATUS:
                        raise VideoJobUnretrieved(
                            job_id, model, "poll", f"status poll answered HTTP {status_code}",
                        ) from exc
                    transient_failures += 1
                    if transient_failures > VIDEO_POLL_MAX_TRANSIENT:
                        raise VideoJobUnretrieved(
                            job_id, model, "poll",
                            f"{transient_failures} consecutive failed status polls; "
                            f"last: {type(exc).__name__}: {exc}",
                        ) from exc
                    logger.warning(
                        "video poll transient failure %d/%d for job %s: %s: %s",
                        transient_failures,
                        VIDEO_POLL_MAX_TRANSIENT,
                        job_id,
                        type(exc).__name__,
                        exc,
                    )
                    continue
                transient_failures = 0
                status = status_data.get("status", "")

                # Liveness during the one operation that is silent for up to 20
                # minutes; see _poll_heartbeat_tick for why the lease needs it.
                self._poll_heartbeat_tick()

                if status == "completed":
                    try:
                        return await self._download_accepted_clip(job_id, model, filepath)
                    except VideoJobUnretrieved:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        raise VideoJobUnretrieved(
                            job_id, model, "download", f"{type(exc).__name__}: {exc}",
                        ) from exc
                elif status in ("failed", "error"):
                    error = status_data.get("error", "Unknown error")
                    raise VideoGenerationFailed(f"Video generation failed: {error}")

            raise VideoJobUnretrieved(
                job_id, model, "wait", f"still not completed after {VIDEO_MAX_WAIT}s",
            )
        finally:
            await _close_quietly(client, job_id)

    async def _download_accepted_clip(self, job_id: str, model: str, filepath: Path) -> str:
        """Fetch a COMPLETED clip within VIDEO_DOWNLOAD_BUDGET; never a new generation.

        The clip exists and is paid for, and its content URL stays valid, so a
        retry costs nothing while giving up costs a whole regenerated scene. Each
        attempt runs under its own total deadline (an httpx timeout bounds only
        the gaps between bytes), and attempts plus pauses share one budget.
        """
        content_url = f"{OPENROUTER_BASE}/videos/{job_id}/content?index=0"
        deadline = time.monotonic() + VIDEO_DOWNLOAD_BUDGET
        last = "no attempt fitted in the download budget"
        dl = httpx.AsyncClient(timeout=VIDEO_DOWNLOAD_TIMEOUT)
        try:
            for attempt in range(1, VIDEO_DOWNLOAD_ATTEMPTS + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # The retries can outlast the lease on their own.
                self._poll_heartbeat_tick()
                try:
                    dl_resp = await asyncio.wait_for(
                        dl.get(content_url, headers=self._headers()),
                        timeout=min(VIDEO_DOWNLOAD_TIMEOUT, remaining),
                    )
                    dl_resp.raise_for_status()
                    content = dl_resp.content
                    if not content:
                        raise RuntimeError("empty video content body")
                except (
                    httpx.HTTPStatusError, httpx.RequestError, RuntimeError, asyncio.TimeoutError,
                ) as exc:
                    code = (
                        exc.response.status_code
                        if isinstance(exc, httpx.HTTPStatusError)
                        else None
                    )
                    if code is not None and code not in VIDEO_RETRY_STATUS:
                        raise VideoJobUnretrieved(
                            job_id, model, "download", f"content answered HTTP {code}",
                        ) from exc
                    last = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "video download attempt %d/%d failed for job %s: %s",
                        attempt, VIDEO_DOWNLOAD_ATTEMPTS, job_id, last,
                    )
                    if attempt < VIDEO_DOWNLOAD_ATTEMPTS:
                        pause = min(
                            VIDEO_DOWNLOAD_RETRY_PAUSE * attempt,
                            max(0.0, deadline - time.monotonic()),
                        )
                        await asyncio.sleep(pause)
                    continue
                try:
                    filepath.write_bytes(content)
                except OSError as exc:
                    raise VideoJobUnretrieved(
                        job_id, model, "write", f"{type(exc).__name__}: {exc}",
                    ) from exc
                logger.info(
                    f"Generated video: {filepath} ({len(content)} bytes, attempt {attempt})"
                )
                return str(filepath)
        finally:
            # A teardown error after the clip was written must not discard it.
            await _close_quietly(dl, job_id)
        raise VideoJobUnretrieved(
            job_id, model, "download",
            f"{VIDEO_DOWNLOAD_ATTEMPTS} attempts or the {VIDEO_DOWNLOAD_BUDGET:.0f}s "
            f"budget exhausted; last: {last}",
        )

    # ─── Music Generation (SSE Streaming via Lyria 3 Pro) ───────────

    async def generate_music(
        self,
        prompt: str,
        filename: str,
    ) -> str:
        """Generate music clip via Lyria 3 Pro Preview (SSE streaming).
        Returns local file path.
        """
        filename = _safe_filename(filename)
        TIMEOUT = 180
        MAX_AUDIO_BYTES = 20 * 1024 * 1024  # 20 MB
        deadline = time.time() + TIMEOUT
        audio_buf = bytearray()

        payload = {
            "model": "google/lyria-3-pro-preview",
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["text", "audio"],
            "audio": {"format": "wav"},
            "stream": True,
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, connect=30)) as client:
            async with client.stream(
                "POST",
                f"{OPENROUTER_BASE}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if time.time() > deadline:
                        raise TimeoutError(f"Music generation timed out after {TIMEOUT}s")
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
                    audio = delta.get("audio", {})
                    if not isinstance(audio, dict):
                        continue
                    b64_chunk = audio.get("data", "")
                    if b64_chunk:
                        padding = "=" * (-len(b64_chunk) % 4)
                        try:
                            decoded = base64.b64decode(b64_chunk + padding)
                        except Exception:
                            continue
                        if len(audio_buf) + len(decoded) > MAX_AUDIO_BYTES:
                            raise RuntimeError(
                                f"Audio exceeds {MAX_AUDIO_BYTES // (1024 * 1024)} MB cap"
                            )
                        audio_buf.extend(decoded)

        if not audio_buf:
            raise RuntimeError(f"No audio bytes returned for {filename}")

        # Detect format from file signature
        head = bytes(audio_buf[:16])
        ext = "wav"
        if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            ext = "wav"
        elif head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
            ext = "mp3"
        elif head[:4] == b"OggS":
            ext = "ogg"
        else:
            # Raw PCM — wrap as WAV (48kHz stereo 16-bit)
            sample_rate = 48000
            channels = 2
            bits = 16
            byte_rate = sample_rate * channels * (bits // 8)
            block_align = channels * (bits // 8)
            data_size = len(audio_buf)
            fmt_chunk = struct.pack(
                "<4sIHHIIHH",
                b"fmt ", 16, 1, channels, sample_rate, byte_rate, block_align, bits,
            )
            data_chunk = struct.pack("<4sI", b"data", data_size)
            riff_size = 4 + len(fmt_chunk) + len(data_chunk) + data_size
            riff_header = struct.pack("<4sI4s", b"RIFF", riff_size, b"WAVE")
            audio_buf = bytearray(riff_header + fmt_chunk + data_chunk + bytes(audio_buf))
            ext = "wav"

        final_filename = _safe_filename(filename.rsplit(".", 1)[0] + "." + ext)
        filepath = self.assets_dir / final_filename
        filepath.write_bytes(bytes(audio_buf))
        logger.info(f"Generated music: {filepath} ({len(audio_buf)} bytes, format: {ext})")
        return str(filepath)

    # ─── TTS ────────────────────────────────────────────────────────

    async def generate_tts(
        self,
        text: str,
        filename: str,
        voice: str = "nova",
    ) -> str:
        """Generate speech audio via TTS. Returns local file path."""
        filename = _safe_filename(filename)
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/audio/speech",
                headers=self._headers(),
                json={
                    "model": "openai/gpt-4o-mini-tts-2025-12-15",
                    "input": text,
                    "voice": voice,
                },
            )
            resp.raise_for_status()

        filepath = self.assets_dir / filename
        filepath.write_bytes(resp.content)
        logger.info(f"Generated TTS: {filepath}")
        return str(filepath)

    # ─── Helpers ────────────────────────────────────────────────────

    def _extract_image_b64(self, data: dict) -> Optional[str]:
        """Extract base64 image data from an OpenRouter response."""
        if not data.get("choices"):
            return None
        msg = data["choices"][0]["message"]
        image_b64 = None

        # Path 1: message.images[] (OpenRouter canonical format)
        images = msg.get("images", [])
        for img in images:
            if isinstance(img, dict):
                url = ""
                if "image_url" in img and isinstance(img["image_url"], dict):
                    url = img["image_url"].get("url", "")
                elif "url" in img:
                    url = img["url"]
                if url.startswith("data:"):
                    image_b64 = url.split(",", 1)[1]
                    break
            elif isinstance(img, str) and img.startswith("data:"):
                image_b64 = img.split(",", 1)[1]
                break

        # Path 2: content as list with image_url parts (Gemini format)
        if not image_b64:
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            image_b64 = url.split(",", 1)[1]
                            break

        return image_b64

    def _compress_image_for_api(self, filepath: str, max_dim: int = 1280, quality: int = 85) -> tuple[str, str]:
        """Compress and resize image for API payload size reduction.

        Returns (base64_string, mime_type).
        """
        Image = _PILImage

        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Asset not found: {filepath}")

        try:
            img = Image.open(path)
        except Exception:
            # Fallback: PIL cannot parse this image (e.g. nanobanana PNG variant);
            # return raw bytes with the original MIME type — no resize/recompress.
            raw = path.read_bytes()
            ext = path.suffix.lstrip(".").lower()
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "webp": "image/webp"}.get(ext, "image/png")
            return base64.b64encode(raw).decode(), mime

        # Resize if larger than max_dim
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            new_w, new_h = int(w * scale), int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)

        # Convert RGBA to RGB with white background
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return b64, "image/jpeg"

    def get_image_url(self, filepath: str, compress: bool = False) -> str:
        """Convert local file to data URL for API input references.

        Args:
            compress: If True, resize to ~1280px max dimension and convert to JPEG for smaller payloads.
        """
        if compress:
            b64, mime = self._compress_image_for_api(filepath)
            return f"data:{mime};base64,{b64}"

        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Asset not found: {filepath}")
        b64 = base64.b64encode(path.read_bytes()).decode()
        ext = path.suffix.lstrip(".")
        mime = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
        }.get(ext, "image/png")
        return f"data:{mime};base64,{b64}"

    def make_input_reference(self, filepath: str) -> dict:
        """Create an OpenRouter input_references entry from a local file."""
        return {
            "type": "image_url",
            "image_url": {"url": self.get_image_url(filepath, compress=True)},
        }

    def make_frame_image(self, filepath: str, frame_type: str = "first_frame") -> dict:
        """Create an OpenRouter frame_images entry for hard visual anchoring.

        Args:
            filepath: Local image file path.
            frame_type: "first_frame" or "last_frame" — hard condition for video start/end.
        """
        return {
            "type": "image_url",
            "image_url": {"url": self.get_image_url(filepath, compress=True)},
            "frame_type": frame_type,
        }

    # ─── Live capability snapshots (replace hardcoded provider tables) ───

    async def fetch_image_capabilities(self) -> dict:
        """GET /images/models -> {model_id: {"params": set[str]}}.

        Fail-SOFT by design: an empty dict means "capabilities unknown", and every
        caller must then omit optional parameters rather than guess. A hardcoded
        per-model table is the anti-pattern this replaces.
        """
        out: dict = {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{OPENROUTER_BASE}/images/models", headers=self._headers())
                resp.raise_for_status()
                rows = (resp.json() or {}).get("data") or []
            for row in rows:
                mid = row.get("id")
                if not mid:
                    continue
                sp = row.get("supported_parameters")
                if isinstance(sp, dict):
                    names = set(sp.keys())
                elif isinstance(sp, list):
                    names = {str(x) for x in sp}
                else:
                    names = set()
                out[mid] = {"params": names}
        except Exception as e:
            logger.warning(f"Image capability catalog unavailable: {type(e).__name__}: {e}")
            return {}
        return out

    async def fetch_video_capabilities(self) -> dict:
        """GET /videos/models -> {model_id: {durations, resolutions, generate_audio}}. Fail-soft: {}."""
        out: dict = {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{OPENROUTER_BASE}/videos/models", headers=self._headers())
                resp.raise_for_status()
                rows = (resp.json() or {}).get("data") or []
            for row in rows:
                mid = row.get("id")
                if not mid:
                    continue
                durs = row.get("supported_durations") or []
                out[mid] = {
                    "durations": [int(d) for d in durs if isinstance(d, (int, float))],
                    "resolutions": row.get("supported_resolutions") or [],
                    "generate_audio": row.get("generate_audio"),
                }
        except Exception as e:
            logger.warning(f"Video capability catalog unavailable: {type(e).__name__}: {e}")
            return {}
        return out

    # ─── Unified Image API (the only path that can carry reference images) ───

    async def generate_image_unified(
        self,
        prompt: str,
        filename: str,
        aspect_ratio: str = "16:9",
        model: str = "google/gemini-3-pro-image-preview",
        reference_images: Optional[list] = None,
        supported_params: Optional[set] = None,
    ) -> str:
        """Generate an image via POST /images, optionally conditioned on reference images.

        This is the canonical image endpoint. The legacy /chat/completions path
        (generate_image / generate_image_nanobanana) has no endpoint for several
        real image models and cannot carry input_references at all; it is retained
        only as a terminal fallback rung.

        Optional parameters are sent ONLY when the live per-model capability record
        permits them — an unknown capability means "send nothing optional".
        """
        filename = _safe_filename(filename)
        payload: dict = {"model": model, "prompt": prompt}

        refs = []
        for path in (reference_images or []):
            try:
                refs.append(self.make_input_reference(path))
            except Exception as e:
                logger.warning(f"Skipping unreadable reference image {path}: {type(e).__name__}: {e}")
        if refs:
            payload["input_references"] = refs

        params = supported_params or set()
        if "aspect_ratio" in params:
            payload["aspect_ratio"] = aspect_ratio
        if "resolution" in params:
            payload["resolution"] = "2K"
        if "quality" in params:
            payload["quality"] = "high"

        async with httpx.AsyncClient(timeout=360.0) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/images",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json() or {}

            entries = data.get("data") or []
            if not entries:
                raise RuntimeError(
                    f"Image API returned no data for {filename} (keys: {sorted(data.keys())})"
                )
            entry = entries[0] or {}
            raw: Optional[bytes] = None
            b64 = entry.get("b64_json")
            if b64:
                raw = base64.b64decode(b64)
            else:
                url = entry.get("url")
                if not url:
                    raw_iu = entry.get("image_url")
                    url = raw_iu.get("url") if isinstance(raw_iu, dict) else raw_iu
                if isinstance(url, str) and url.startswith("data:"):
                    raw = base64.b64decode(url.split(",", 1)[1])
                elif isinstance(url, str) and url.startswith("http"):
                    img_resp = await client.get(url)
                    img_resp.raise_for_status()
                    raw = img_resp.content
            if not raw:
                raise RuntimeError(
                    f"No usable image payload for {filename} (entry keys: {sorted(entry.keys())})"
                )

        filepath = self.assets_dir / filename
        filepath.write_bytes(raw)
        logger.info(
            f"Image generated via /images: {filepath} "
            f"(model={model}, refs={len(refs)}, opt_params={sorted(k for k in payload if k in ('aspect_ratio', 'resolution', 'quality'))})"
        )
        return str(filepath)
