"""HTTP Digest authentication for SIP (RFC 3261 §22, RFC 2617, RFC 7616, RFC 8760).

Answers a ``WWW-Authenticate``/``Proxy-Authenticate`` challenge with MD5 or
SHA-256 (and their ``-sess`` variants), with ``qop=auth``/``auth-int`` when
offered and without qop for old RFC 2069 servers. When a response carries
several challenges, ``choose`` prefers SHA-256. The password never leaves
this module except inside the hash.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Dict, List, Optional

from .sip_message import split_commas

_HASHES = {"MD5": hashlib.md5, "SHA-256": hashlib.sha256}


class DigestError(ValueError):
    pass


@dataclass
class Challenge:
    realm: str
    nonce: str
    algorithm: str = "MD5"
    qop: tuple = ()
    opaque: Optional[str] = None
    stale: bool = False

    @property
    def base_algorithm(self) -> str:
        return self.algorithm.upper().removesuffix("-SESS")

    @property
    def session(self) -> bool:
        return self.algorithm.upper().endswith("-SESS")


def _parse_pairs(text: str) -> Dict[str, str]:
    pairs: Dict[str, str] = {}
    for item in split_commas(text):
        key, sep, value = item.partition("=")
        if sep:
            value = value.strip()
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            pairs[key.strip().lower()] = value
    return pairs


def parse_challenge(header: str) -> Challenge:
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "digest":
        raise DigestError(f"unsupported authentication scheme {scheme!r}")
    pairs = _parse_pairs(rest)
    if "nonce" not in pairs or "realm" not in pairs:
        raise DigestError("challenge lacks realm or nonce")
    algorithm = pairs.get("algorithm", "MD5").upper()
    if algorithm.removesuffix("-SESS") not in _HASHES:
        raise DigestError(f"unsupported digest algorithm {algorithm}")
    qop = tuple(q.strip().lower() for q in pairs.get("qop", "").split(",") if q.strip())
    return Challenge(pairs["realm"], pairs["nonce"], algorithm, qop, pairs.get("opaque"),
                     pairs.get("stale", "").lower() == "true")


def choose(headers: List[str]) -> Challenge:
    """The strongest supported challenge among several (SHA-256 before MD5)."""
    parsed = []
    for header in headers:
        try:
            parsed.append(parse_challenge(header))
        except DigestError:
            continue
    if not parsed:
        raise DigestError("no supported digest challenge")
    parsed.sort(key=lambda c: 0 if c.base_algorithm == "SHA-256" else 1)
    return parsed[0]


def compute_response(challenge: Challenge, *, method: str, uri: str, username: str, password: str,
                     cnonce: str = "", nc: int = 1, qop: Optional[str] = None, body: bytes = b"") -> str:
    hasher = _HASHES[challenge.base_algorithm]

    def h(text: str) -> str:
        return hasher(text.encode("utf-8")).hexdigest()

    ha1 = h(f"{username}:{challenge.realm}:{password}")
    if challenge.session:
        ha1 = h(f"{ha1}:{challenge.nonce}:{cnonce}")
    if qop == "auth-int":
        ha2 = h(f"{method}:{uri}:{hasher(body).hexdigest()}")
    else:
        ha2 = h(f"{method}:{uri}")
    if qop:
        return h(f"{ha1}:{challenge.nonce}:{nc:08x}:{cnonce}:{qop}:{ha2}")
    return h(f"{ha1}:{challenge.nonce}:{ha2}")


def authorization(challenge: Challenge, *, method: str, uri: str, username: str, password: str,
                  nc: int = 1, cnonce: Optional[str] = None, body: bytes = b"") -> str:
    """The ``Authorization``/``Proxy-Authorization`` value answering ``challenge``."""
    qop = "auth" if "auth" in challenge.qop else "auth-int" if "auth-int" in challenge.qop else None
    if qop is None and challenge.session:
        raise DigestError("-sess algorithms need qop")
    cnonce = cnonce or secrets.token_hex(8)
    response = compute_response(challenge, method=method, uri=uri, username=username, password=password,
                                cnonce=cnonce, nc=nc, qop=qop, body=body)
    parts = [f'username="{username}"', f'realm="{challenge.realm}"', f'nonce="{challenge.nonce}"',
             f'uri="{uri}"', f'response="{response}"', f"algorithm={challenge.algorithm}"]
    if qop:
        parts += [f"qop={qop}", f"nc={nc:08x}", f'cnonce="{cnonce}"']
    if challenge.opaque is not None:
        parts.append(f'opaque="{challenge.opaque}"')
    return "Digest " + ", ".join(parts)
