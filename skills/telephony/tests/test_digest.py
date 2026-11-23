import pytest

from phone import digest


def test_rfc2617_md5_vector():
    ch = digest.parse_challenge('Digest realm="testrealm@host.com", qop="auth,auth-int", '
                                'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"')
    assert ch.qop == ("auth", "auth-int")
    resp = digest.compute_response(ch, method="GET", uri="/dir/index.html", username="Mufasa",
                                   password="Circle Of Life", cnonce="0a4f113b", nc=1, qop="auth")
    assert resp == "6629fae49393a05397450978507c4ef1"


RFC7616 = dict(method="GET", uri="/dir/index.html", username="Mufasa", password="Circle of Life",
               cnonce="f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ", nc=1, qop="auth")
NONCE = "7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v"


def test_rfc7616_sha256_and_md5_vectors():
    sha = digest.parse_challenge(f'Digest realm="http-auth@example.org", qop="auth, auth-int", algorithm=SHA-256, '
                                 f'nonce="{NONCE}", opaque="FQhe/qaU925kfnzjCev0ciny7QMkPqMAFRtzCUYo5tdS"')
    md5 = digest.parse_challenge(f'Digest realm="http-auth@example.org", qop="auth, auth-int", algorithm=MD5, '
                                 f'nonce="{NONCE}", opaque="FQhe/qaU925kfnzjCev0ciny7QMkPqMAFRtzCUYo5tdS"')
    assert digest.compute_response(sha, **RFC7616) == "753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1"
    assert digest.compute_response(md5, **RFC7616) == "8ca523f5e9506fed4657c9700eebdbec"


def test_choose_prefers_sha256_and_header_shape():
    chosen = digest.choose(['Digest realm="r", nonce="n", algorithm=MD5, qop="auth"',
                            'Digest realm="r", nonce="n", algorithm=SHA-256, qop="auth"'])
    assert chosen.base_algorithm == "SHA-256"
    header = digest.authorization(chosen, method="REGISTER", uri="sip:pbx", username="u", password="p",
                                  cnonce="abc", nc=1)
    assert header.startswith("Digest ") and 'qop=auth' in header and "nc=00000001" in header
    assert "p\"" not in header and "password" not in header


def test_rfc2069_without_qop_and_unsupported():
    ch = digest.parse_challenge('Digest realm="asterisk", nonce="1234"')
    assert "qop" not in digest.authorization(ch, method="INVITE", uri="sip:x", username="u", password="p")
    with pytest.raises(digest.DigestError):
        digest.parse_challenge('Digest realm="r", nonce="n", algorithm=SHA-512-256')
    with pytest.raises(digest.DigestError):
        digest.parse_challenge('Basic realm="r"')
