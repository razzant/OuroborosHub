"""Pins the companion's copy of the live call protocol to the host's own modules."""
from phone import callproto as ours


def test_constants_match_host(core):
    cp = core
    assert ours.PROTOCOL_VERSION == cp.PROTOCOL_VERSION
    assert ours.HEADER.format == "<2sBBHIIH" and ours.HEADER_SIZE == cp.HEADER_SIZE
    assert (ours.FRAME_AUDIO_IN, ours.FRAME_AUDIO_OUT) == (cp.FRAME_AUDIO_IN, cp.FRAME_AUDIO_OUT)
    assert ours.MAX_FRAME_PAYLOAD == cp.MAX_FRAME_PAYLOAD and ours.MAX_CONTROL_BYTES == cp.MAX_CONTROL_BYTES
    assert ours.PEER_MEETING == cp.PEER_MEETING
    assert set(ours.PEER_MESSAGE_TYPES) == set(cp.PEER_MESSAGE_TYPES)
    assert set(ours.SERVER_MESSAGE_TYPES) == set(cp.SERVER_MESSAGE_TYPES)
    from ouroboros.call_peers import MIN_RATE, MAX_RATE
    assert (ours.MIN_RATE, ours.MAX_RATE) == (MIN_RATE, MAX_RATE)


def test_frames_cross_decode(core):
    cp = core
    pcm = bytes(range(256)) * 3 + b"\x00\x01"
    mine = ours.encode_frame(ours.AudioFrame(ours.FRAME_AUDIO_IN, 70000, 2**32 + 5, 24000, 0, pcm))
    theirs = cp.decode_frame(mine)
    assert (theirs.kind, theirs.epoch, theirs.seq, theirs.rate, theirs.pcm) == (1, 70000 & 0xFFFF, 5, 24000, pcm)
    back = ours.decode_frame(cp.encode_frame(cp.AudioFrame(cp.FRAME_AUDIO_OUT, 3, 9, 24000, 0, pcm)))
    assert (back.kind, back.epoch, back.seq, back.pcm) == (2, 3, 9, pcm)


def test_controls_pass_the_host_door(host_doors):
    from ouroboros.gateway.call_ws import _control
    from ouroboros.gateway.call_contracts import HostCallOpenRequest
    from ouroboros.gateway.schema import validate_ingress
    hello = ours.hello(24000, "Phone +15550001001")
    assert _control(ours.encode_control(hello)) == hello
    for message in ({"type": "played", "item_id": "it", "played_ms": 120},
                    {"type": "context", "text": "[Phone line notice] key 5", "source": "phone_line"},
                    {"type": "bye", "reason": "remote_hangup"}):
        assert _control(ours.encode_control(message)) == message
    body = {"role": "meeting", "mic": True, "speaker": True, "label": "Phone +1555", "call_id": "abc"}
    assert not validate_ingress(body, HostCallOpenRequest)


def test_presence_event_shape_parses(core, tmp_path):
    from ouroboros.presence_events import parse_presence_event
    from phone.manager import ActiveCall, CallManager
    manager = CallManager(tmp_path, {"outbound": {}}, "", None)
    for direction in ("in", "out"):
        call = ActiveCall(direction=direction, mode="place", number="+15550001001", purpose="test")
        event = manager._event(call)
        parsed = parse_presence_event(event)
        assert parsed.provider == "phone" and parsed.conversation_id == "+15550001001"
        assert parsed.actor["platform_actor_id"] == "+15550001001" and parsed.actor["authenticated"] is False
        assert parsed.facts == ("direct_message",)
