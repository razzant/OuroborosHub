"""A dispatched video prompt fits the model's limit and keeps every @ImageN line
it carried; when that is impossible, nothing is sent."""
from __future__ import annotations

import asyncio

import httpx
import pytest

import anime_studio.api_client as api
import anime_studio.pipeline as pl

HAILUO = "minimax/hailuo-3"
KLING = "kwaivgi/kling-v3.0-pro"


def _labels(count: int, width: int = 120) -> list[str]:
    out = []
    for n in range(1, count + 1):
        text = f"@Image{n} — the CHARACTER SHEET of Hero {n}: match face, hair, outfit EXACTLY."
        out.append(text.ljust(width, "."))
    return out


def _prompt(description_chars: int, refs: int, dialogue: str | None = "Hero 1: We go now!") -> str:
    parts = [
        "Anime scene, modern style.",
        "REFERENCE IMAGES (attached in this order):",
        *_labels(refs),
        "SCENE: " + ("A wide valley at dawn, wind in the grass. " * 400)[:description_chars],
        "CAMERA: slow dolly forward.",
    ]
    text = "\n".join(parts)
    if dialogue:
        text += "\n\nSPOKEN DIALOGUE (characters MUST speak this aloud):\n" + dialogue
    return text


def _limit_table():
    table = getattr(api, "VIDEO_MODEL_PROMPT_LIMIT", None)
    assert table is not None, "api_client must own the single prompt-limit table"
    return table


def test_limit_table_covers_hailuo3_under_its_provider_ceiling():
    table = _limit_table()
    assert table[HAILUO] < 7000, "the provider refuses prompts over 7000 characters"
    assert table[KLING] == 2500
    assert pl.Pipeline._VIDEO_MODEL_PROMPT_LIMIT is table, "the pipeline must not keep a second copy"


def test_fit_keeps_reference_lines_dialogue_and_the_limit():
    prompt = _prompt(9000, refs=3)
    fitted = api.fit_prompt_to_limit(prompt, 2500, api.expected_reference_tokens(3))

    assert len(fitted) <= 2500
    for line in _labels(3):
        assert line in fitted
    assert api.PROMPT_TRUNCATION_MARKER in fitted
    assert fitted.endswith("Hero 1: We go now!")


def test_fit_returns_short_prompt_unchanged():
    prompt = _prompt(200, refs=2)
    assert api.fit_prompt_to_limit(prompt, 6800, api.expected_reference_tokens(2)) == prompt


def test_fit_refuses_when_reference_lines_alone_cannot_fit():
    prompt = _prompt(4000, refs=4)
    with pytest.raises(api.VideoPromptFitError):
        api.fit_prompt_to_limit(prompt, 400, api.expected_reference_tokens(4))


def test_dispatch_fits_an_overlong_hailuo3_prompt_and_audits_it(tmp_path, mock_http, no_sleep):
    sent: list[dict] = []
    audits: list[dict] = []

    def handler(request: httpx.Request):
        if request.method == "POST":
            import json as _json
            sent.append(_json.loads(request.content))
            return httpx.Response(200, json={"id": "job-1"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"clip-bytes")
        return httpx.Response(200, json={"status": "completed"})

    mock_http(handler)
    prompt = _prompt(8640, refs=2)
    assert len(prompt) > 7000
    refs = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}] * 2

    asyncio.run(api.OpenRouterClient("test-key", tmp_path).generate_video(
        prompt=prompt, filename="scene_1.mp4", model=HAILUO, input_references=refs,
        audit_sink=audits.append,
    ))

    assert len(sent) == 1
    dispatched = sent[0]["prompt"]
    assert len(dispatched) <= _limit_table()[HAILUO] < 7000
    assert "@Image1" in dispatched and "@Image2" in dispatched
    assert audits[0]["prompt_fit_applied"] is True
    assert audits[0]["prompt_chars_before_fit"] == len(prompt)
    assert audits[0]["prompt_limit"] == _limit_table()[HAILUO]
    assert audits[0]["prompt_names_all_references"] is True


def test_dispatch_sends_nothing_when_the_prompt_cannot_keep_its_references(
    tmp_path, mock_http, no_sleep, monkeypatch,
):
    requests = mock_http(lambda request: httpx.Response(200, json={"id": "job-1"}))
    monkeypatch.setitem(_limit_table(), KLING, 400)
    refs = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}] * 4

    with pytest.raises(api.VideoPromptFitError):
        asyncio.run(api.OpenRouterClient("test-key", tmp_path).generate_video(
            prompt=_prompt(4000, refs=4), filename="scene_1.mp4", model=KLING,
            input_references=refs,
        ))
    assert requests == [], "an unfittable prompt must be refused before any request"


class _ChatClient:
    """Stands in for OpenRouterClient.chat during condensation."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.reply(kwargs) if callable(self.reply) else self.reply


def _pipeline(tmp_path, client):
    return pl.Pipeline(client=client, state_dir=tmp_path)


def test_condenser_rejects_a_candidate_that_drops_a_reference(tmp_path):
    prompt = _prompt(9000, refs=2)
    lossy = "\n".join(["Short scene.", _labels(1)[0], "A valley at dawn. " * 20])
    client = _ChatClient(lossy)

    result = asyncio.run(_pipeline(tmp_path, client)._condense_prompt_for_model(prompt, KLING))

    assert client.calls, "the condenser should have been asked"
    assert "@Image1" in result and "@Image2" in result
    assert len(result) <= 2500
    assert "@Image2" in client.calls[0]["messages"][0]["content"].split("--- ORIGINAL ---")[0]


def test_condenser_accepts_a_candidate_that_keeps_every_reference(tmp_path):
    prompt = _prompt(9000, refs=2)
    good = "\n".join(["Short scene."] + _labels(2) + [("A valley at dawn. " * 20).strip()])
    result = asyncio.run(_pipeline(tmp_path, _ChatClient(good))._condense_prompt_for_model(prompt, KLING))
    assert result == good


def test_condenser_covers_hailuo3(tmp_path):
    prompt = _prompt(8640, refs=2)
    good = "\n".join(["Short scene."] + _labels(2) + [("A valley at dawn. " * 20).strip()])
    client = _ChatClient(good)
    result = asyncio.run(_pipeline(tmp_path, client)._condense_prompt_for_model(prompt, HAILUO))
    assert client.calls and result == good


def test_condenser_failure_falls_back_to_the_reference_preserving_fit(tmp_path):
    def broken(_kwargs):
        raise RuntimeError("provider down")

    prompt = _prompt(9000, refs=3)
    result = asyncio.run(_pipeline(tmp_path, _ChatClient(broken))._condense_prompt_for_model(prompt, KLING))
    assert len(result) <= 2500
    for line in _labels(3):
        assert line in result


def _real_prompt(description_chars: int, refs: int) -> str:
    """The production layout: the reference block comes AFTER the scene text."""
    labels = [
        f"@Image{n} — the CHARACTER SHEET of Hero {n}: match this character's face, "
        f"hair, eye colour, outfit and accessories EXACTLY."
        for n in range(1, refs + 1)
    ]
    prompt = pl.build_video_prompt(
        scene_description=("A wide valley at dawn, wind in the grass. " * 400)[:description_chars],
        reference_block=pl.build_reference_block(labels),
        characters_identity_block="- Hero 1: red scarf",
        camera_direction="slow dolly forward", mood="calm", style="modern anime",
        duration_sec=8, continuity_note="",
    )
    return prompt + (
        "\n\nSPOKEN DIALOGUE (characters MUST speak this aloud with clear native audio / "
        "speech — not silent mime, not subtitles):\nHero 1: We go now!"
    )


def test_condenser_fallback_keeps_references_in_the_real_prompt_layout(tmp_path):
    def broken(_kwargs):
        raise RuntimeError("provider down")

    prompt = _real_prompt(9000, refs=3)
    result = asyncio.run(_pipeline(tmp_path, _ChatClient(broken))._condense_prompt_for_model(prompt, KLING))

    assert len(result) <= 2500
    for n in (1, 2, 3):
        assert f"@Image{n}" in result
    assert "Hero 1: We go now!" in result


def test_dispatch_fit_keeps_references_in_the_real_prompt_layout(tmp_path, mock_http, no_sleep):
    sent: list[str] = []

    def handler(request: httpx.Request):
        if request.method == "POST":
            import json as _json
            sent.append(_json.loads(request.content)["prompt"])
            return httpx.Response(200, json={"id": "job-1"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"clip-bytes")
        return httpx.Response(200, json={"status": "completed"})

    mock_http(handler)
    refs = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}] * 3
    asyncio.run(api.OpenRouterClient("test-key", tmp_path).generate_video(
        prompt=_real_prompt(9000, refs=3), filename="scene_1.mp4", model=KLING,
        input_references=refs,
    ))

    assert len(sent) == 1 and len(sent[0]) <= 2500
    for n in (1, 2, 3):
        assert f"@Image{n}" in sent[0]


def _binding_lines(prompt: str) -> list[str]:
    return [line.strip() for line in prompt.split("\n") if "@Image" in line]


def _crossed(prompt: str) -> str:
    """Every token kept, but Hero 1's and Hero 2's descriptions swapped."""
    lines = _binding_lines(prompt)
    swapped = [
        lines[0].replace("Hero 1", "Hero X").replace("Hero 2", "Hero 1").replace("Hero X", "Hero 2"),
        lines[1].replace("Hero 2", "Hero X").replace("Hero 1", "Hero 2").replace("Hero X", "Hero 1"),
    ]
    return "\n".join(["Short scene in a valley at dawn, two heroes meet."] + swapped
                     + [("A valley at dawn. " * 20).strip()])


def _bare_markers(_prompt: str) -> str:
    return "\n".join([
        "Short scene in a valley at dawn, two heroes meet.",
        "Use @Image1 and @Image2 for the two heroes.",
        ("A valley at dawn. " * 20).strip(),
    ])


@pytest.mark.parametrize("make_candidate", [_crossed, _bare_markers], ids=["crossed", "bare_markers"])
def test_condenser_rejects_candidates_that_keep_tokens_but_change_bindings(tmp_path, make_candidate):
    prompt = _real_prompt(9000, refs=2)
    candidate = make_candidate(prompt)
    assert "@Image1" in candidate and "@Image2" in candidate  # every token is still named

    result = asyncio.run(_pipeline(tmp_path, _ChatClient(candidate))._condense_prompt_for_model(prompt, KLING))

    assert result != candidate
    assert len(result) <= 2500
    kept = {line.strip() for line in result.split("\n")}
    for line in _binding_lines(prompt):
        assert line in kept, "each binding line must survive exactly as written"


def _lessons_prompt(description_chars: int, lessons_chars: int) -> str:
    prompt = _real_prompt(description_chars, refs=2)
    return prompt + "\n\nLEARNED FROM PREVIOUS GENERATIONS (apply these):\n" + (
        "- keep the scarf red in every frame\n" * 200
    )[:lessons_chars]


def test_dispatch_fit_keeps_dialogue_when_long_lessons_follow_it():
    prompt = _lessons_prompt(3000, 4000)
    fitted = api.fit_prompt_to_limit(prompt, 2500, api.expected_reference_tokens(2))

    assert len(fitted) <= 2500
    assert "Hero 1: We go now!" in fitted, "the lessons must not drag the dialogue out"
    for line in _binding_lines(prompt):
        assert line in fitted


def test_short_tail_is_still_kept_whole():
    prompt = _lessons_prompt(9000, 300)
    fitted = api.fit_prompt_to_limit(prompt, 2500, api.expected_reference_tokens(2))
    assert fitted.endswith(prompt[prompt.index("\n\nSPOKEN DIALOGUE"):])


def test_reference_free_hard_fit_keeps_dialogue_when_long_lessons_follow_it(tmp_path):
    prompt = pl.build_video_prompt(
        scene_description=("A wide valley at dawn, wind in the grass. " * 200)[:3000],
        reference_block=pl.build_reference_block([]),
        characters_identity_block="- Hero 1: red scarf",
        camera_direction="slow dolly forward", mood="calm", style="modern anime",
        duration_sec=8, continuity_note="",
    ) + (
        "\n\nSPOKEN DIALOGUE (characters MUST speak this aloud with clear native audio / "
        "speech — not silent mime, not subtitles):\nHero 1: We go now!"
        "\n\nLEARNED FROM PREVIOUS GENERATIONS (apply these):\n"
        + "- keep the scarf red in every frame\n" * 120
    )
    fitted = _pipeline(tmp_path, _ChatClient("unused"))._hard_fit_prompt(prompt, 2500)

    assert len(fitted) <= 2500
    assert "Hero 1: We go now!" in fitted
    assert fitted.count("Hero 1: We go now!") == 1, "the dialogue is not duplicated by the head cut"
