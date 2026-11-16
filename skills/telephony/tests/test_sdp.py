import pytest

from phone import sdp

OFFER = ("v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\ns=-\r\nc=IN IP4 192.0.2.1\r\nt=0 0\r\n"
         "m=audio 40000 RTP/AVP 8 0 101 18\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:0 PCMU/8000\r\n"
         "a=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-16\r\na=rtpmap:18 G729/8000\r\na=ptime:20\r\na=sendrecv\r\n")


def test_parse_and_answer_follow_offer_order():
    media = sdp.parse(OFFER)
    assert media.address == "192.0.2.1" and media.port == 40000 and media.dtmf_pt() == 101
    neg = sdp.negotiate(media)
    assert neg.codec == "PCMA" and neg.payload_type == 8 and neg.remote == ("192.0.2.1", 40000)
    ours = sdp.SessionDescription("10.0.0.5", 20000)
    answer = sdp.parse(ours.answer(neg))
    assert answer.payload_types == [8, 101] and answer.port == 20000 and answer.direction == "sendrecv"


def test_offer_lists_both_codecs_and_dtmf():
    body = sdp.SessionDescription("10.0.0.5", 20002).offer()
    media = sdp.parse(body)
    assert media.payload_types == [0, 8, 101] and media.codec_for(0) == "PCMU" and media.dtmf_pt() == 101


def test_hold_direction_and_no_common_codec():
    held = sdp.parse(OFFER.replace("a=sendrecv", "a=sendonly"))
    assert sdp.negotiate(held).direction == "sendonly"
    assert "a=recvonly" in sdp.SessionDescription("h", 1).answer(sdp.negotiate(held))
    with pytest.raises(sdp.SdpError):
        sdp.negotiate(sdp.parse(OFFER.replace("m=audio 40000 RTP/AVP 8 0 101 18", "m=audio 40000 RTP/AVP 18")))
    with pytest.raises(sdp.SdpError):
        sdp.parse("v=0\r\nm=video 1 RTP/AVP 96\r\n")
