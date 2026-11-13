import struct
import warnings

import pytest

from phone import g711

ALL_PCM = struct.pack("<65536h", *range(-32768, 32768))
ALL_BYTES = bytes(range(256))


def _audioop():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            import audioop
        except ImportError:
            return None
    return audioop


@pytest.mark.parametrize("codec,enc,dec", [("PCMU", "lin2ulaw", "ulaw2lin"), ("PCMA", "lin2alaw", "alaw2lin")])
def test_matches_reference_for_every_value(codec, enc, dec):
    audioop = _audioop()
    if audioop is None:
        pytest.skip("audioop is gone from this Python; the 3.10/3.12 runs compare")
    assert g711.encode(ALL_PCM, codec) == getattr(audioop, enc)(ALL_PCM, 2)
    assert g711.decode(ALL_BYTES, codec) == getattr(audioop, dec)(ALL_BYTES, 2)


@pytest.mark.parametrize("codec", ["PCMU", "PCMA"])
def test_decode_encode_is_identity_on_codewords(codec):
    decoded = g711.decode(ALL_BYTES, codec)
    again = g711.encode(decoded, codec)
    # mu-law has two zero codewords (0x7F, 0xFF); every other codeword survives a round trip.
    mismatches = [i for i in range(256) if again[i] != i]
    assert mismatches in ([], [0x7F]) if codec == "PCMU" else mismatches == []


def test_silence_decodes_near_zero():
    for codec in ("PCMU", "PCMA"):
        samples = struct.unpack("<160h", g711.decode(g711.silence(codec), codec))
        assert max(abs(s) for s in samples) <= 8


def test_rejects_odd_pcm_and_unknown_codec():
    with pytest.raises(ValueError):
        g711.encode(b"\x00", "PCMU")
    with pytest.raises(ValueError):
        g711.encode(b"\x00\x00", "G722")
