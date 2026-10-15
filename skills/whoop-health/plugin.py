"""WHOOP Health — Ouroboros extension skill (v0.3.1).

Full OAuth2 Authorization Code flow:
  whoop_auth_url     — generate browser auth URL
  whoop_exchange_code — exchange code for tokens (persisted to state dir)
  whoop_check        — connection health-check
  whoop_summary      — last N days recovery + sleep + strain
  whoop_fetch        — detailed data by type (RECOVERY / SLEEP / WORKOUT / CYCLE)

Tokens are saved in api.get_state_dir() and auto-refreshed on 401/403.
"""

from __future__ import annotations

import json
import os
import sys

# Ensure sibling imports work inside the extension loader.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import importlib  # noqa: E402
if "whoop_client" in sys.modules:
    importlib.reload(sys.modules["whoop_client"])
import whoop_client  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────


def _get_credentials(api) -> tuple[str, str, str]:
    """Read WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET from secrets.

    Returns (state_dir, client_id, client_secret).
    """
    settings = api.get_settings(["WHOOP_CLIENT_ID", "WHOOP_CLIENT_SECRET"])
    client_id = settings.get("WHOOP_CLIENT_ID") or ""
    client_secret = settings.get("WHOOP_CLIENT_SECRET") or ""
    state_dir = api.get_state_dir()
    return state_dir, client_id, client_secret


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


_MISSING_CREDS = _json({
    "status": "error",
    "error": "missing_credentials",
    "detail": (
        "WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET not set. "
        "Add them in Settings → Secrets, then run whoop_auth_url."
    ),
})

_NO_TOKENS = _json({
    "status": "error",
    "error": "not_authorized",
    "detail": (
        "No WHOOP tokens found. Run whoop_auth_url to get the "
        "authorization URL, open it, then run whoop_exchange_code "
        "with the code from the redirect."
    ),
})

_FETCHERS = {
    "RECOVERY": whoop_client.fetch_recovery,
    "SLEEP": whoop_client.fetch_sleep,
    "WORKOUT": whoop_client.fetch_workout,
    "CYCLE": whoop_client.fetch_cycle,
}


# ── registration ──────────────────────────────────────────────────


def register(api):
    """Called by the Ouroboros extension loader."""

    # ── whoop_auth_url ────────────────────────────────────────────

    def _auth_url(redirect_uri="", scope="", **kwargs) -> str:
        state_dir, client_id, client_secret = _get_credentials(api)
        if not client_id:
            return _MISSING_CREDS
        uri = redirect_uri or "http://localhost:8080/callback"
        sc = scope or whoop_client.DEFAULT_SCOPES
        url = whoop_client.build_auth_url(client_id, uri, sc)
        return _json({
            "status": "ok",
            "action": "open_in_browser",
            "auth_url": url,
            "redirect_uri": uri,
            "instructions": (
                "1. Open the auth_url in a browser\n"
                "2. Log in to WHOOP and authorize\n"
                "3. Copy the 'code' parameter from the redirect URL\n"
                "4. Run whoop_exchange_code with that code"
            ),
        })

    api.register_tool(
        name="whoop_auth_url",
        description=(
            "Генерирует URL для OAuth2 авторизации WHOOP. "
            "Пользователь открывает URL, авторизуется и копирует "
            "code из redirect для whoop_exchange_code."
        ),
        schema={
            "type": "object",
            "properties": {
                "redirect_uri": {
                    "type": "string",
                    "description": (
                        "Redirect URI, зарегистрированный в WHOOP app "
                        "(по умолчанию http://localhost:8080/callback)"
                    ),
                    "default": "",
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "OAuth scopes через пробел (по умолчанию все read)"
                    ),
                    "default": "",
                },
            },
        },
        handler=_auth_url,
    )

    # ── whoop_exchange_code ───────────────────────────────────────

    def _exchange_code(code="", redirect_uri="", **kwargs) -> str:
        state_dir, client_id, client_secret = _get_credentials(api)
        if not client_id or not client_secret:
            return _MISSING_CREDS
        if not code:
            return _json({
                "status": "error",
                "error": "missing_code",
                "detail": "Provide the authorization code from the redirect.",
            })
        uri = redirect_uri or "http://localhost:8080/callback"
        try:
            tokens = whoop_client.exchange_code(
                code, client_id, client_secret, uri)
            whoop_client.save_tokens(
                state_dir, tokens["access_token"], tokens["refresh_token"])
            return _json({
                "status": "ok",
                "message": (
                    "Tokens saved successfully. All WHOOP tools "
                    "are now ready to use."
                ),
                "has_refresh_token": bool(tokens.get("refresh_token")),
            })
        except Exception as exc:
            return _json({"status": "error", "error": str(exc)})

    api.register_tool(
        name="whoop_exchange_code",
        description=(
            "Обменивает authorization code на access + refresh tokens. "
            "Код получается после авторизации по URL из whoop_auth_url."
        ),
        schema={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Authorization code из redirect URL",
                },
                "redirect_uri": {
                    "type": "string",
                    "description": (
                        "Тот же redirect_uri, что в whoop_auth_url"
                    ),
                    "default": "",
                },
            },
            "required": ["code"],
        },
        handler=_exchange_code,
    )

    # ── whoop_check ───────────────────────────────────────────────

    def _check(**kwargs) -> str:
        state_dir, client_id, client_secret = _get_credentials(api)
        if not client_id or not client_secret:
            return _MISSING_CREDS
        try:
            profile = whoop_client.check(
                state_dir, client_id, client_secret)
            return _json({"status": "ok", "profile": profile})
        except RuntimeError as exc:
            if "No tokens found" in str(exc):
                return _NO_TOKENS
            return _json({"status": "error", "error": str(exc)})
        except Exception as exc:
            return _json({"status": "error", "error": str(exc)})

    api.register_tool(
        name="whoop_check",
        description=(
            "Проверка подключения к WHOOP API. "
            "Возвращает профиль пользователя."
        ),
        schema={"type": "object", "properties": {}},
        handler=_check,
    )

    # ── whoop_summary ─────────────────────────────────────────────

    def _summary(days_back="1", **kwargs) -> str:
        state_dir, client_id, client_secret = _get_credentials(api)
        if not client_id or not client_secret:
            return _MISSING_CREDS
        try:
            days = max(1, min(int(days_back), 30))
        except (TypeError, ValueError):
            days = 1
        try:
            data = whoop_client.summary(
                state_dir, client_id, client_secret, days=days)
            return _json({"status": "ok", **data})
        except RuntimeError as exc:
            if "No tokens found" in str(exc):
                return _NO_TOKENS
            return _json({"status": "error", "error": str(exc)})
        except Exception as exc:
            return _json({"status": "error", "error": str(exc)})

    api.register_tool(
        name="whoop_summary",
        description=(
            "Сводка WHOOP за последние N дней: recovery (HRV, resting HR, "
            "SpO2), сон (стадии, performance), strain (нагрузка, HR). "
            "recovery_zone уже вычислен для числового процента 0–100: "
            "red ≤33, yellow >33 и ≤66, green >66. "
            "null означает, что зона неизвестна; не угадывай её. "
            "user_calibrating=true означает недостаточно данных при калибровке."
        ),
        schema={
            "type": "object",
            "properties": {
                "days_back": {
                    "type": "string",
                    "description": "Количество дней назад (по умолчанию 1)",
                    "default": "1",
                },
            },
        },
        handler=_summary,
        timeout_sec=90,
    )

    # ── whoop_fetch ───────────────────────────────────────────────

    def _fetch(data_type="", days_back="7", **kwargs) -> str:
        state_dir, client_id, client_secret = _get_credentials(api)
        if not client_id or not client_secret:
            return _MISSING_CREDS

        dt = (data_type or "").upper()
        fetcher = _FETCHERS.get(dt)
        if not fetcher:
            return _json({
                "status": "error",
                "error": (
                    f"Unknown data_type '{dt}'. "
                    f"Use one of: {', '.join(_FETCHERS)}"
                ),
            })

        try:
            days = max(1, min(int(days_back), 90))
        except (TypeError, ValueError):
            days = 7

        try:
            records = fetcher(
                state_dir, client_id, client_secret, days=days)
            return _json({
                "status": "ok",
                "data_type": dt,
                "count": len(records),
                "records": records,
            })
        except RuntimeError as exc:
            if "No tokens found" in str(exc):
                return _NO_TOKENS
            return _json({"status": "error", "error": str(exc)})
        except Exception as exc:
            return _json({"status": "error", "error": str(exc)})

    api.register_tool(
        name="whoop_fetch",
        description=(
            "Детальные данные WHOOP по типу: RECOVERY, SLEEP, WORKOUT, CYCLE. "
            "Возвращает массив записей за указанный период. Для RECOVERY: "
            "recovery_zone уже вычислен для числового процента 0–100: "
            "red ≤33, yellow >33 и ≤66, green >66. "
            "null означает, что зона неизвестна; не угадывай её. "
            "user_calibrating=true означает недостаточно данных при калибровке."
        ),
        schema={
            "type": "object",
            "properties": {
                "data_type": {
                    "type": "string",
                    "description": "Тип данных: RECOVERY, SLEEP, WORKOUT, CYCLE",
                    "enum": ["RECOVERY", "SLEEP", "WORKOUT", "CYCLE"],
                },
                "days_back": {
                    "type": "string",
                    "description": "Количество дней назад (по умолчанию 7)",
                    "default": "7",
                },
            },
            "required": ["data_type"],
        },
        handler=_fetch,
        timeout_sec=90,
    )
