import asyncio
import pathlib

from telegram_bot.api import TelegramClient


class FakeTransport:
    def __init__(self):
        self.json_calls = []
        self.multipart_calls = []
        self.download_calls = []

    async def post_json(self, url, payload, timeout_sec):
        self.json_calls.append((url, payload, timeout_sec))
        if url.endswith("/getFile"):
            return {"ok": True, "result": {"file_path": "docs/x.txt", "file_size": 4}}
        return {"ok": True, "result": {"message_id": len(self.json_calls)}}

    async def post_multipart(self, url, fields, file_field, file_path, timeout_sec):
        self.multipart_calls.append(
            (url, fields, file_field, pathlib.Path(file_path), timeout_sec)
        )
        return {"ok": True, "result": {"message_id": 50}}

    async def get_bytes(self, url, timeout_sec, max_bytes):
        self.download_calls.append((url, timeout_sec, max_bytes))
        return b"data"


def test_get_file_download_and_text_topic_reply(tmp_path):
    asyncio.run(_get_file_download_and_text_topic_reply(tmp_path))


async def _get_file_download_and_text_topic_reply(tmp_path):
    transport = FakeTransport()
    client = TelegramClient("token", transport=transport)
    target = await client.download_file("file-1", tmp_path / "staged.txt")
    assert target.read_bytes() == b"data"
    assert transport.json_calls[0][1] == {"file_id": "file-1"}
    assert "/file/bottoken/docs/x.txt" in transport.download_calls[0][0]

    receipts = await client.send_message(
        "-10",
        "hello",
        topic_id=7,
        reply_to_message_id=6,
    )
    assert receipts
    payload = transport.json_calls[-1][1]
    assert payload["message_thread_id"] == 7
    assert payload["reply_parameters"] == {"message_id": 6}


def test_send_photo_and_document_use_distinct_multipart_fields(tmp_path):
    asyncio.run(_send_photo_and_document_use_distinct_multipart_fields(tmp_path))


async def _send_photo_and_document_use_distinct_multipart_fields(tmp_path):
    transport = FakeTransport()
    client = TelegramClient("token", transport=transport)
    photo = tmp_path / "image.png"
    photo.write_bytes(b"png")
    document = tmp_path / "report.pdf"
    document.write_bytes(b"pdf")

    await client.send_photo("1", photo, caption="image", topic_id=3)
    await client.send_document("1", document, caption="report", reply_to_message_id=2)

    assert [call[2] for call in transport.multipart_calls] == ["photo", "document"]
    assert transport.multipart_calls[0][1]["message_thread_id"] == "3"
    assert "reply_parameters" in transport.multipart_calls[1][1]
