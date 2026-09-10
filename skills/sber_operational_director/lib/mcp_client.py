"""Thin MCP client for Sber AI operational director agent server.

Tools: summarise.agent_check_collect, summarise.agent_get_data
Default: https://fintech.sberbank.ru:9443/fintech/api/transactional-agent/mcp
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

_MCP_PROD_URL = "https://fintech.sberbank.ru:9443/fintech/api/transactional-agent/mcp"
_MCP_TEST_URL = "https://iftfintech.testsbi.sberbank.ru:9443/fintech/api/transactional-agent/mcp"

# Negotiated by initialize; required for reliable tools/call on this server.
_MCP_PROTOCOL_VERSION = "2025-11-25"

# Gateway sometimes returns a Spring 404 with this *internal* path even when the
# public URL is correct — treat as transient and retry.
_TRANSIENT_404_MARKERS = (
    "/v2/corporate-cards/transactional-agent/mcp",
    '"status":404',
)

_ALLOWED_HOSTS = (
    "fintech.sberbank.ru",
    "iftfintech.testsbi.sberbank.ru",
)

_UUID4_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)

INTEGRATION_NAMES = (
    "business_card",
    "client_profile",
    "documents",
    "documentCancel",
    "tasklist",
    "udkz",
    "fskk",
    "authority",
    "account_turns"
)


def resolve_mcp_url(url_or_env: str = "") -> str:
    """Resolve MCP endpoint.

    Default is PROM. Optional override:
    - full URL (https://...)
    - short alias: ift | test | prod | prom | production
    """
    cleaned = (url_or_env or "").strip()
    if not cleaned:
        return _MCP_PROD_URL
    lower = cleaned.lower()
    if lower.startswith("https://") or lower.startswith("http://"):
        return cleaned
    if lower in {"prod", "production", "prom"}:
        return _MCP_PROD_URL
    if lower in {"ift", "test"}:
        return _MCP_TEST_URL
    return cleaned


def new_session_id() -> str:
    return str(uuid.uuid4())


def _validate_host(url: str) -> Optional[str]:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password:
            return "MCP URL must use HTTPS without embedded credentials"
    except Exception:
        return "invalid MCP URL"
    if host not in _ALLOWED_HOSTS:
        return f"host not allowed: {host}"
    return None


def validate_mcp_url(url: str) -> Optional[str]:
    return _validate_host(url)


def _validate_session_id(session_id: str) -> Optional[str]:
    cleaned = (session_id or "").strip()
    if not _UUID4_RE.match(cleaned):
        return "legalPersonSessionId must be a uuid4 string"
    return None


def _normalize_integration_names(values: Any) -> Tuple[Optional[List[str]], Optional[str]]:
    if values is None or values == "":
        return None, None
    if isinstance(values, str):
        raw_items = [part.strip() for part in values.split(",") if part.strip()]
    elif isinstance(values, Sequence):
        raw_items = [str(item).strip() for item in values if str(item).strip()]
    else:
        return None, "integrationName must be an array of strings or a comma-separated string"

    if not raw_items:
        return None, None

    allowed = set(INTEGRATION_NAMES)
    unknown = [item for item in raw_items if item not in allowed]
    if unknown:
        return None, (
            "unknown integrationName values: "
            + ", ".join(unknown)
            + f"; allowed: {', '.join(INTEGRATION_NAMES)}"
        )
    # preserve order, drop duplicates
    seen = set()
    ordered: List[str] = []
    for item in raw_items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered, None


def _build_tls(cert_path: str, key_path: str) -> Optional[Tuple[str, str]]:
    cert = (cert_path or "").strip()
    key = (key_path or "").strip()
    if not cert and not key:
        return None
    if not cert or not key:
        raise ValueError("both SBER_TLS_CERT_PATH and SBER_TLS_KEY_PATH are required for mTLS")
    return (cert, key)


def _resolve_ca_path(ca_path: str = "") -> str:
    cleaned = (ca_path or "").strip()
    if cleaned:
        return cleaned
    return os.environ.get("SBER_TLS_CA_PATH", "").strip()


def _httpx_tls_kwargs(
    _url: str,
    cert: Optional[Tuple[str, str]],
    ca_path: str = "",
) -> Dict[str, Any]:
    """Build httpx cert/verify kwargs (always verify with bank CA when set).

    When a CA bundle and client cert are both set, load them into one SSLContext
    and pass it as verify= only — required for mTLS against SynGX gateways.
    """
    import ssl

    ca = _resolve_ca_path(ca_path)
    if ca and cert:
        ctx = ssl.create_default_context(cafile=ca)
        ctx.load_cert_chain(certfile=cert[0], keyfile=cert[1])
        return {"cert": None, "verify": ctx}

    if ca:
        return {"cert": cert, "verify": ca}

    return {"cert": cert, "verify": True}


def _is_transient_gateway_404(status_code: int, body: str) -> bool:
    if status_code != 404:
        return False
    return any(marker in body for marker in _TRANSIENT_404_MARKERS)


def _decode_response(response: Any) -> Dict[str, Any]:
    """Decode JSON or one bounded JSON-RPC event from an MCP response."""
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" not in content_type:
        data = response.json()
        return data if isinstance(data, dict) else {"result": data}
    events = []
    for line in response.text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                events.append(payload)
    if not events:
        raise ValueError("MCP SSE response contained no data event")
    try:
        data = json.loads(events[-1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid MCP SSE JSON event: {exc}") from exc
    return data if isinstance(data, dict) else {"result": data}


def _unwrap_tool_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten MCP tools/call result.content[0].text JSON when present."""
    result = data.get("result")
    if not isinstance(result, dict):
        return data
    if result.get("isError"):
        return {"error": result}
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return data
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        return data
    text = first.get("text")
    if not isinstance(text, str) or not text.strip():
        return data
    try:
        parsed = json.loads(text)
    except Exception:
        return {"error": "MCP tool returned non-JSON text", "text": text[:500]}
    if isinstance(parsed, dict):
        return parsed
    return {"data": parsed}


def _post_json_rpc(
    *,
    url: str,
    access_token: str,
    method: str,
    params: Dict[str, Any],
    cert: Optional[Tuple[str, str]],
    timeout_sec: float,
    retries: int = 4,
    session_id: str = "",
    retry_http_5xx: bool = False,
    ca_path: str = "",
) -> Dict[str, Any]:
    import time

    import httpx

    host_error = _validate_host(url)
    if host_error:
        return {"error": host_error}

    token = (access_token or "").strip()
    if not token:
        return {"error": "SBER_ACCESS_TOKEN is not configured or not granted"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": _MCP_PROTOCOL_VERSION,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    tls_kwargs = _httpx_tls_kwargs(url, cert, ca_path=ca_path)

    last_error: Optional[str] = None
    attempts = max(1, retries)
    for attempt in range(attempts):
        request_id = uuid.uuid4().hex
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        if method != "notifications/initialized":
            payload["id"] = request_id
        try:
            with httpx.Client(
                timeout=timeout_sec,
                **tls_kwargs,
            ) as client:
                response = client.post(url, json=payload, headers=headers)
        except Exception as exc:
            last_error = f"MCP request failed: {type(exc).__name__}: {exc}"
            if attempt + 1 < attempts:
                time.sleep(0.4 * (attempt + 1))
                continue
            return {"error": last_error}

        if response.status_code >= 400:
            body = response.text[:500]
            last_error = f"MCP HTTP {response.status_code}: {body}"
            transient = _is_transient_gateway_404(response.status_code, body)
            if retry_http_5xx and 500 <= response.status_code < 600:
                transient = True
            if transient and attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
                continue
            out: Dict[str, Any] = {"error": last_error}
            return out

        if method == "notifications/initialized" and not response.content:
            return {}
        try:
            data = _decode_response(response)
        except Exception as exc:
            return {"error": f"invalid MCP response: {exc}"}

        if isinstance(data, dict) and data.get("error"):
            return {"error": data["error"]}
        if not isinstance(data, dict):
            return {"result": data}
        response_session = response.headers.get("Mcp-Session-Id", "").strip()
        if response_session:
            data = {**data, "_mcp_session_id": response_session}
        if method == "tools/call":
            return _unwrap_tool_result(data)
        return data

    return {"error": last_error or "MCP request failed"}


def _initialize(
    *,
    url: str,
    access_token: str,
    cert: Optional[Tuple[str, str]],
    timeout_sec: float,
    ca_path: str = "",
) -> Dict[str, Any]:
    result = _post_json_rpc(
        url=url,
        access_token=access_token,
        method="initialize",
        params={
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "sber_operational_director", "version": "0.2.10"},
        },
        cert=cert,
        timeout_sec=timeout_sec,
        retry_http_5xx=True,
        ca_path=ca_path,
    )
    if "error" in result:
        return result
    server_version = result.get("result", {}).get("protocolVersion")
    if server_version and server_version != _MCP_PROTOCOL_VERSION:
        return {
            "error": (
                "MCP protocol mismatch: server selected "
                f"{server_version}, client supports {_MCP_PROTOCOL_VERSION}"
            ),
        }
    session_id = result.get("_mcp_session_id", "")
    notification = _post_json_rpc(
        url=url,
        access_token=access_token,
        method="notifications/initialized",
        params={},
        cert=cert,
        timeout_sec=timeout_sec,
        session_id=session_id,
        retries=1,
        ca_path=ca_path,
    )
    if "error" in notification:
        return notification
    return {"session_id": session_id}


def _call_tool(
    *,
    url: str,
    access_token: str,
    tool_name: str,
    arguments: Dict[str, Any],
    cert: Optional[Tuple[str, str]],
    timeout_sec: float,
    ca_path: str = "",
) -> Dict[str, Any]:
    initialized = _initialize(
        url=url,
        access_token=access_token,
        cert=cert,
        timeout_sec=timeout_sec,
        ca_path=ca_path,
    )
    if "error" in initialized:
        return initialized
    return _post_json_rpc(
        url=url,
        access_token=access_token,
        method="tools/call",
        params={"name": tool_name, "arguments": arguments},
        cert=cert,
        timeout_sec=timeout_sec,
        session_id=initialized.get("session_id", ""),
        # A tools/call may have reached Sber before an HTTP 5xx response;
        # retrying it could duplicate an external operation.
        retry_http_5xx=False,
        ca_path=ca_path,
    )


def check_collect(
    *,
    legal_person_session_id: str = "",
    text_input: str = "",
    integration_name: Any = None,
    url: str,
    access_token: str,
    cert_path: str = "",
    key_path: str = "",
    ca_path: str = "",
    timeout_sec: float = 60.0,
) -> Dict[str, Any]:
    session_id = (legal_person_session_id or "").strip() or new_session_id()
    err = _validate_session_id(session_id)
    if err:
        return {"error": err}

    names, name_err = _normalize_integration_names(integration_name)
    if name_err:
        return {"error": name_err}

    arguments: Dict[str, Any] = {"legalPersonSessionId": session_id}
    text = (text_input or "").strip()
    if text:
        arguments["textInput"] = text
    if names is not None:
        arguments["integrationName"] = names

    try:
        cert = _build_tls(cert_path, key_path)
    except ValueError as exc:
        return {"error": str(exc)}

    result = _call_tool(
        url=url,
        access_token=access_token,
        tool_name="summarise.agent_check_collect",
        arguments=arguments,
        cert=cert,
        timeout_sec=timeout_sec,
        ca_path=ca_path,
    )
    if "error" not in result:
        result = {**result, "legalPersonSessionId": session_id}
        if names is not None:
            result = {**result, "integrationName": names}
    return result


def get_data(
    *,
    legal_person_session_id: str,
    integration_name: Any = None,
    url: str,
    access_token: str,
    cert_path: str = "",
    key_path: str = "",
    ca_path: str = "",
    timeout_sec: float = 60.0,
) -> Dict[str, Any]:
    session_id = (legal_person_session_id or "").strip()
    err = _validate_session_id(session_id)
    if err:
        return {"error": err}

    names, name_err = _normalize_integration_names(integration_name)
    if name_err:
        return {"error": name_err}

    arguments: Dict[str, Any] = {"legalPersonSessionId": session_id}
    if names is not None:
        arguments["integrationName"] = names

    try:
        cert = _build_tls(cert_path, key_path)
    except ValueError as exc:
        return {"error": str(exc)}

    return _call_tool(
        url=url,
        access_token=access_token,
        tool_name="summarise.agent_get_data",
        arguments=arguments,
        cert=cert,
        timeout_sec=timeout_sec,
        ca_path=ca_path,
    )


def as_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
