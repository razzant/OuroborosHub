import pytest

from phone import sip_message as sm
from phone.sip_message import SipMessage, SipParseError, SipUri, parse as parse_sip

INVITE = (
    "INVITE sip:+15550001001@pbx.example SIP/2.0\r\n"
    "v: SIP/2.0/UDP 10.0.0.2:5062;rport;branch=z9hG4bKabc\r\n"
    "Via: SIP/2.0/UDP proxy.example;branch=z9hG4bKp1, SIP/2.0/UDP 1.2.3.4:5060;branch=z9hG4bKp2\r\n"
    "f: \"Agent, Bot\" <sip:agent@pbx.example>;tag=111\r\n"
    "t: <sip:+15550001001@pbx.example>\r\n"
    "i: call-1@10.0.0.2\r\n"
    "CSeq: 1 INVITE\r\n"
    "Contact: <sip:agent@10.0.0.2:5062>\r\n"
    "Subject: folded\r\n  continuation\r\n"
    "Record-Route: <sip:p1.example;lr>, <sip:p2.example;lr>\r\n"
    "c: application/sdp\r\n"
    "l: 4\r\n\r\nv=0\nEXTRA"
).encode()


def test_parse_compact_multi_and_folded():
    msg = sm.parse(INVITE)
    assert msg.is_request and msg.method == "INVITE"
    assert len(msg.get_all("Via")) == 3
    assert msg.top_via.branch == "z9hG4bKabc" and "rport" in msg.top_via.params
    assert msg.from_addr.display == "Agent, Bot" and msg.from_addr.tag == "111"
    assert msg.call_id == "call-1@10.0.0.2" and msg.cseq == (1, "INVITE")
    assert msg.get("Subject") == "folded continuation"
    assert msg.get_all("Record-Route") == ["<sip:p1.example;lr>", "<sip:p2.example;lr>"]
    assert msg.body == b"v=0\n"  # Content-Length bounds the body


def test_roundtrip_and_content_length():
    msg = sm.parse(INVITE)
    again = sm.parse(msg.encode())
    assert again.get_all("Via") == msg.get_all("Via") and again.body == msg.body
    assert b"Content-Length: 4\r\n" in msg.encode()


def test_response_to_copies_dialog_headers():
    msg = sm.parse(INVITE)
    resp = sm.response_to(msg, 180, to_tag="xyz")
    assert resp.status == 180 and resp.reason == "Ringing"
    assert resp.get_all("Via") == msg.get_all("Via")
    assert resp.to_addr.tag == "xyz" and resp.get_all("Record-Route")
    parsed = sm.parse(resp.encode())
    assert parsed.status == 180 and parsed.cseq == (1, "INVITE")


def test_uri_and_name_addr():
    uri = sm.SipUri.parse("sip:alice@[2001:db8::1]:5070;transport=udp")
    assert uri.host == "[2001:db8::1]" and uri.port == 5070 and uri.params["transport"] == "udp"
    assert sm.SipUri.parse("sip:alice:secret@host").user == "alice"
    na = sm.NameAddr.parse("sip:bob@example.com;tag=9")
    assert na.uri.user == "bob" and na.tag == "9"
    assert str(sm.NameAddr.parse('"Bob" <sip:bob@h:5062>;tag=1')) == '"Bob" <sip:bob@h:5062>;tag=1'


@pytest.mark.parametrize("raw", [b"", b"garbage\r\n\r\n", b"INVITE sip:x SIP/2.0\r\nVia: SIP/2.0/UDP h\r\n\r\n",
                                 b"SIP/2.0 abc OK\r\n\r\n"])
def test_rejects_malformed(raw):
    with pytest.raises(sm.SipParseError):
        sm.parse(raw)


def test_truncated_body_is_rejected():
    with pytest.raises(sm.SipParseError):
        sm.parse(INVITE.replace(b"l: 4", b"l: 400"))


def test_negative_content_length_and_non_ascii_sip_ints_rejected():
    raw = (b"SIP/2.0 200 OK\r\nVia: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK1\r\n"
           b"From: <sip:a@127.0.0.1>;tag=1\r\nTo: <sip:b@127.0.0.1>;tag=2\r\n"
           b"Call-ID: c1\r\nCSeq: 1 INVITE\r\nContent-Length: -5\r\n\r\nhello world")
    with pytest.raises(SipParseError):
        parse_sip(raw)

    msg = SipMessage(method="INVITE", uri="sip:b@127.0.0.1", headers=[("CSeq", "² INVITE")])
    with pytest.raises(SipParseError):
        _ = msg.cseq

    with pytest.raises(SipParseError):
        SipUri.parse("tel:+15550001001").address()
