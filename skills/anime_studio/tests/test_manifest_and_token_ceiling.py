"""Manifest identity for this release and the single, large response ceiling."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx

import anime_studio.api_client as api
import anime_studio.plugin as plugin

SKILL_DIR = Path(__file__).resolve().parent.parent

RELEASE = "3.3.1"


def _frontmatter() -> str:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    return text.split("\n---", 1)[0]


def test_manifest_names_this_release_and_keeps_plugin_api():
    head = _frontmatter()
    assert re.search(r'^plugin_api:\s*"2\.0"\s*$', head, re.MULTILINE), "plugin_api 2.0 was lost once"
    assert re.search(rf"^version:\s*{re.escape(RELEASE)}\s*$", head, re.MULTILINE)
    assert plugin.SKILL_VERSION == RELEASE, "the worker's stale-code check reads this"


# max_completion_tokens from the public OpenRouter catalog, read 2026-11-16.
CATALOG_OUTPUT_CEILINGS = {
    "anthropic/claude-sonnet-4.6": 128000,
    "google/gemini-3.1-pro-preview": 65536,
    "google/gemini-3.5-flash": 65536,
}


def test_response_ceiling_is_the_largest_value_every_called_model_accepts():
    assert api.DEFAULT_MAX_TOKENS == min(CATALOG_OUTPUT_CEILINGS.values()) == 65536


def test_ceiling_table_names_every_text_model_the_payload_calls():
    named = set()
    for path in sorted(SKILL_DIR.rglob("*.py")):
        if path.parent.name == "tests":
            continue
        named |= set(re.findall(r'"((?:anthropic|google)/(?:claude|gemini)-[^"]+)"',
                                path.read_text(encoding="utf-8")))
    text_models = {m for m in named if "image" not in m and "lyria" not in m}
    assert text_models == set(CATALOG_OUTPUT_CEILINGS)


def test_no_call_site_writes_its_own_token_number():
    literal = re.compile(r"\bmax_tok(?:en)?s[\"']?\s*[:=]\s*(?:int\s*=\s*)?\d")
    offenders = []
    for path in sorted(SKILL_DIR.rglob("*.py")):
        if path.parent.name == "tests":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]  # explanatory comments may quote old values
            if literal.search(code):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == []


def test_every_model_call_sends_the_ceiling(tmp_path, mock_http, no_sleep):
    sent: list[dict] = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        sent.append(body)
        if body.get("response_format"):
            content = '{"passed": true, "score": 8, "issues": [], "suggestion": "", "winner": 1, "reason": "r"}'
        else:
            content = "plain answer"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    mock_http(handler)
    client = api.OpenRouterClient("test-key", tmp_path)
    image = tmp_path / "frame.png"
    from PIL import Image
    Image.new("RGB", (64, 64), (10, 20, 30)).save(image)

    asyncio.run(client.chat(messages=[{"role": "user", "content": "hi"}]))
    asyncio.run(client.verify_image_vlm(str(image), "a hero in a red scarf"))
    asyncio.run(client.compare_images_vlm(str(image), str(image), "pick the better sheet"))
    asyncio.run(client.analyze_multi_image_vlm([str(image)], "score these frames"))

    assert len(sent) == 4
    assert {body["max_tokens"] for body in sent} == {api.DEFAULT_MAX_TOKENS}
