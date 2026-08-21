from __future__ import annotations

import asyncio

import httpx
import pytest

from lib.slack_api import SlackClient, SlackConfigurationError, chunk_message


def test_chunking_is_bounded_and_lossless() -> None:
    text = ("one two three\n" * 700) + "tail"
    chunks = chunk_message(text, max_length=128)
    assert all(0 < len(chunk) <= 128 for chunk in chunks)
    assert "".join(chunks) == text


def test_missing_or_wrong_token_types_fail_before_network() -> None:
    with pytest.raises(SlackConfigurationError, match="SLACK_BOT_TOKEN"):
        SlackClient("", "xapp-good")
    with pytest.raises(SlackConfigurationError, match="bot token"):
        SlackClient("xoxp-user", "xapp-good")
    with pytest.raises(SlackConfigurationError, match="app-level"):
        SlackClient("xoxb-good", "xoxb-not-app")


def test_private_file_download_uses_bot_authorization_and_stages_bytes(
    tmp_path,
) -> None:
    asyncio.run(
        _private_file_download_uses_bot_authorization_and_stages_bytes(tmp_path)
    )


async def _private_file_download_uses_bot_authorization_and_stages_bytes(
    tmp_path,
) -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"private bytes")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    slack = SlackClient("xoxb-secret", "xapp-secret", http_client=http)
    staged = await slack.stage_private_files(
        [
            {
                "file_id": "F1",
                "name": "../report.txt",
                "mimetype": "text/plain",
                "size": 13,
                "url_private": "https://files.slack.com/files-pri/T/F/report.txt",
            }
        ],
        destination=tmp_path / "staged",
    )

    assert observed["authorization"] == "Bearer xoxb-secret"
    assert staged[0].name == "report.txt"
    assert (tmp_path / "staged" / "00-report.txt").read_bytes() == b"private bytes"
    await slack.aclose()
    assert slack.closed is True
    assert http.is_closed is False
    await http.aclose()
