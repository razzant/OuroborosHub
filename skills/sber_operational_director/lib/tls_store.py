"""Store TLS material for Sber API mTLS and server CA verification.

Primary flow in Ouroboros:
- user attaches P12 in chat → install_tls_certificate → PEM in skill state_dir
- user attaches bank CA (.crt/.pem) → install_ca_certificate → CA in skill state_dir

Docs: https://developers.sber.ru/docs/ru/sber-api/start/tls
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Dict, Tuple

_STORED_P12 = Path("tls") / "client.p12"
_STORED_CA = Path("tls") / "ca.pem"
_BUNDLED_CA = Path(__file__).resolve().parents[1] / "certs" / "SberCA_Root_Ext.crt"
_MAX_P12_BYTES = 1_048_576
_MAX_CA_BYTES = 2_097_152
_ALLOWED_P12_SUFFIXES = {".p12", ".pfx"}
_ALLOWED_CA_SUFFIXES = {".crt", ".pem", ".cer", ".ca-bundle"}


def stored_p12_path(state_dir: str) -> Path:
    return Path(state_dir) / _STORED_P12


def stored_ca_path(state_dir: str) -> Path:
    return Path(state_dir) / _STORED_CA


def tls_is_installed(state_dir: str) -> bool:
    return stored_p12_path(state_dir).is_file()


def ca_is_installed(state_dir: str) -> bool:
    return stored_ca_path(state_dir).is_file()


def resolve_stored_ca_path(state_dir: str) -> str:
    path = stored_ca_path(state_dir)
    return str(path) if path.is_file() else ""


def resolve_ca_path(state_dir: str, url: str) -> str:
    """Explicit CA takes precedence; the bundled production root is not for IFT."""
    from urllib.parse import urlparse

    stored = resolve_stored_ca_path(state_dir) if state_dir else ""
    if stored:
        return stored
    if urlparse(url).hostname == "fintech.sberbank.ru" and _BUNDLED_CA.is_file():
        return str(_BUNDLED_CA)
    return ""


def install_p12_from_path(state_dir: str, source_path: str, password: str) -> Dict[str, str]:
    """Copy chat upload (or any readable path) into state_dir and validate P12."""
    src = Path(source_path.strip()).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"TLS P12 file not found: {src}")
    if src.suffix.lower() not in _ALLOWED_P12_SUFFIXES:
        raise ValueError("source file must have .p12 or .pfx extension")

    size = src.stat().st_size
    if size <= 0 or size > _MAX_P12_BYTES:
        raise ValueError(f"P12 file size out of range (max {_MAX_P12_BYTES} bytes)")

    pwd = (password or "").encode("utf-8")
    if not password:
        raise ValueError("SBER_TLS_P12_PASSWORD is empty")

    p12_bytes = src.read_bytes()
    _p12_to_pem(p12_bytes, pwd)

    dest = stored_p12_path(state_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    _invalidate_pem_cache(Path(state_dir))
    ensure_p12_tls(state_dir, password)
    return {
        "status": "ok",
        "message": "Клиентский TLS-сертификат установлен",
    }


def install_ca_from_path(state_dir: str, source_path: str) -> Dict[str, str]:
    """Copy bank CA / chain from chat upload into state_dir and validate."""
    src = Path(source_path.strip()).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"CA file not found: {src}")
    if src.suffix.lower() not in _ALLOWED_CA_SUFFIXES:
        raise ValueError("CA file must have .crt, .pem, .cer or .ca-bundle extension")

    size = src.stat().st_size
    if size <= 0 or size > _MAX_CA_BYTES:
        raise ValueError(f"CA file size out of range (max {_MAX_CA_BYTES} bytes)")

    raw = src.read_bytes()
    pem = _normalize_ca_to_pem(raw)
    dest = stored_ca_path(state_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(pem)
    return {
        "status": "ok",
        "message": "CA-цепочка банка установлена",
    }


def ensure_p12_tls(
    state_dir: str,
    password: str,
    *,
    p12_path: str = "",
) -> Tuple[str, str]:
    """Return PEM cert/key paths, converting stored or legacy P12 when needed."""
    base = Path(state_dir)
    base.mkdir(parents=True, exist_ok=True)

    pwd = (password or "").encode("utf-8")
    if not password:
        raise ValueError("SBER_TLS_P12_PASSWORD is empty")

    if p12_path:
        src = Path(p12_path.strip()).expanduser()
    else:
        src = stored_p12_path(state_dir)

    if not src.is_file():
        raise FileNotFoundError(f"TLS P12 file not found: {src}")

    p12_bytes = src.read_bytes()
    digest = hashlib.sha256(p12_bytes + pwd).hexdigest()

    cert_path = base / "sber_client.pem"
    key_path = base / "sber_client.key"
    marker = base / "sber_tls_p12.fingerprint"

    if (
        marker.is_file()
        and marker.read_text(encoding="utf-8").strip() == digest
        and cert_path.is_file()
        and key_path.is_file()
    ):
        return str(cert_path), str(key_path)

    cert_pem, key_pem = _p12_to_pem(p12_bytes, pwd)
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)
    marker.write_text(digest + "\n", encoding="utf-8")
    return str(cert_path), str(key_path)


def _invalidate_pem_cache(state_dir: Path) -> None:
    for name in ("sber_client.pem", "sber_client.key", "sber_tls_p12.fingerprint"):
        path = state_dir / name
        if path.is_file():
            path.unlink()


def _normalize_ca_to_pem(raw: bytes) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    text = raw.decode("utf-8", errors="ignore")
    if "BEGIN CERTIFICATE" in text:
        try:
            certs = x509.load_pem_x509_certificates(raw)
        except Exception as exc:
            raise ValueError(f"invalid CA PEM bundle: {exc}") from exc
        if not certs:
            raise ValueError("CA PEM bundle contains no certificates")
        return b"".join(cert.public_bytes(Encoding.PEM) for cert in certs)

    try:
        cert = x509.load_der_x509_certificate(raw)
    except Exception as exc:
        raise ValueError(f"invalid CA certificate (expected PEM or DER): {exc}") from exc
    return cert.public_bytes(Encoding.PEM)


def _p12_to_pem(p12_bytes: bytes, password: bytes) -> Tuple[bytes, bytes]:
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        pkcs12,
    )

    try:
        private_key, certificate, additional = pkcs12.load_key_and_certificates(
            p12_bytes,
            password,
        )
    except Exception as exc:
        raise ValueError(f"invalid P12 file or password: {exc}") from exc

    if private_key is None or certificate is None:
        raise ValueError("P12 does not contain a usable client certificate and private key")

    cert_parts = [certificate.public_bytes(Encoding.PEM)]
    for extra in additional or ():
        cert_parts.append(extra.public_bytes(Encoding.PEM))
    key_pem = private_key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        NoEncryption(),
    )
    return b"".join(cert_parts), key_pem
