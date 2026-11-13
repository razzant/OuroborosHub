"""The line's no-media limit: an answered call is hung up after 60 s without incoming RTP packets, and the
documents say exactly that. A party who stays silent keeps sending RTP, so its call goes on: the limit is no
silence detector, and no document may promise one."""
import asyncio
import pathlib
import time

from phone import config as cfg
from phone import manager as mgr
from phone.manager import ActiveCall, CallManager
from phone.provider import Leg

SKILL = pathlib.Path(__file__).resolve().parent.parent


def flat(name):
    return " ".join((SKILL / name).read_text(encoding="utf-8").split())


def test_the_documents_describe_the_limit_as_missing_rtp_not_as_silence():
    seconds = mgr.MEDIA_TIMEOUT_SECONDS
    readme, skill, quick = flat("README.md"), flat("SKILL.md"), flat("QUICKSTART.ru.md")
    assert f"receives no RTP packets from the line for {seconds} s" in readme
    assert "stays silent still sends RTP" in readme
    assert f"receives no audio packets (RTP) from the line for {seconds} s" in skill
    assert f"{seconds} секунд с линии не приходят RTP-пакеты" in quick
    for text in (readme, skill):
        assert "no sound from the line" not in text
    assert "минуты тишины" not in quick


class Line(Leg):
    """An answered leg whose packets arrive (carrying silence) while ``sending``."""

    def __init__(self):
        self.id, self.direction, self.remote_number, self.state = "leg", "out", "+15550001001", "answered"
        self.end_reason, self.end_status = "", 0
        self.hangups = []
        self.packet_at = time.monotonic()

    def read_frame(self):
        return bytes(320)  # what silent packets decode to

    def write_frame(self, pcm):
        pass

    async def send_dtmf(self, digits):
        return "rfc4733"

    async def hangup(self, reason="local_hangup"):
        self.hangups.append(reason)
        self.state, self.end_reason = "ended", reason

    def last_media_at(self):
        return self.packet_at


def test_silent_packets_keep_a_call_and_no_packets_end_it(tmp_path, monkeypatch):
    monkeypatch.setattr(mgr, "MEDIA_TIMEOUT_SECONDS", 0.3)

    async def scenario():
        manager = CallManager(tmp_path, cfg.DEFAULTS, "", None)
        line = Line()
        call = ActiveCall(direction="out", mode="place", number=line.remote_number)
        call.state, call.answered_mono, call.leg = "answered", time.monotonic(), line
        manager.active = call
        await manager._start_clock(call)
        try:
            for _ in range(50):                      # a second of silent packets, 20 ms apart
                line.packet_at = time.monotonic()
                await asyncio.sleep(0.02)
            kept = manager.active is call and line.hangups == []
            for _ in range(100):                     # then no packets at all
                if manager.active is None:
                    break
                await asyncio.sleep(0.02)
            return kept, line.hangups, manager.last.end_reason if manager.last else None
        finally:
            await manager.stop()

    kept, hangups, reason = asyncio.run(scenario())
    assert kept and hangups == ["media_timeout"] and reason == "media_timeout", (kept, hangups, reason)
