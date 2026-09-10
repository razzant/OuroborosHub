"""Sber AI operational director agent extension — collect/get client data via MCP.

Proxies summarise.agent_check_collect and summarise.agent_get_data.
Default endpoint: https://fintech.sberbank.ru:9443/fintech/api/transactional-agent/mcp
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

_SKILL_ROOT = Path(__file__).resolve().parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from starlette.requests import Request
from starlette.responses import JSONResponse

from lib.mcp_client import (
    INTEGRATION_NAMES,
    as_json,
    check_collect,
    get_data,
    resolve_mcp_url,
    validate_mcp_url,
)
from lib.tls_store import (
    ca_is_installed,
    ensure_p12_tls,
    install_ca_from_path,
    install_p12_from_path,
    resolve_ca_path,
    tls_is_installed,
)

_SETTINGS_KEYS = (
    "SBER_ACCESS_TOKEN",
    "SBER_TLS_P12_PASSWORD",
    "SBER_MCP_URL",
)

_CHECK_TITLES = {
    "SBER_ACCESS_TOKEN": "Access token (MCP)",
    "SBER_TLS_P12_PASSWORD": "Пароль P12",
    "client_tls": "Клиентский TLS",
    "p12_in_state": "P12 в state",
    "server_ca": "CA-цепочка банка",
    "SBER_MCP_URL": "MCP endpoint",
    "state_dir": "Каталог state",
}

_UI_RENDER = {
    "kind": "declarative",
    "schema_version": 1,
    "components": [
        {
            "type": "markdown",
            "text": (
                "## Настройка\n"
                "Проверьте токен, пароль P12, клиентский .p12 и CA банка. "
                "Корневой CA для PROM встроен; клиентский сертификат ставится из чата. "
                "В таблице: **OK** — готово, **Нет** — нужно настроить."
            ),
        },
        {
            "type": "action",
            "id": "check_setup",
            "route": "check_setup",
            "method": "POST",
            "target": "setup",
            "fields": [],
            "submit_label": "Проверить настройку",
            "busy_label": "Проверяю…",
        },
        {
            "type": "status",
            "target": "setup",
            "idle": "Нажмите «Проверить настройку»",
            "loading": "Проверяю конфигурацию…",
            "error": "Не удалось проверить настройку",
            "success": "Статус обновлён",
        },
        {
            "type": "kv",
            "target": "setup",
            "fields": [
                {"label": "Готовность", "path": "ready_label"},
                {"label": "Контур", "path": "stand"},
                {"label": "MCP URL", "path": "mcp_url"},
            ],
        },
        {
            "type": "table",
            "target": "setup",
            "path": "checks",
            "columns": [
                {"label": "Статус", "path": "mark"},
                {"label": "Параметр", "path": "title"},
                {"label": "Детали", "path": "detail"},
                {"label": "Как настроить", "path": "how"},
            ],
        },
        {
            "type": "markdown",
            "target": "setup",
            "path": "hints_md",
        },
        {
            "type": "markdown",
            "text": (
                "## Запрос к MCP\n"
                "Сформулируйте **бизнес-вопрос** (что нужно узнать по клиенту). "
                "Ключ сессии можно оставить пустым. "
                "Поле кодов разделов — только для отладки; в чате агент "
                "подбирает их сам по смыслу."
            ),
        },
        {
            "type": "form",
            "route": "check_collect",
            "method": "POST",
            "target": "mcp",
            "fields": [
                {
                    "name": "text_input",
                    "label": "Вопрос / инструкция для MCP",
                    "type": "text",
                    "placeholder": "Какие ограничения на счетах?",
                    "required": True,
                },
                {
                    "name": "integration_name",
                    "label": "Коды разделов (опционально, отладка)",
                    "type": "text",
                    "placeholder": "",
                    "required": False,
                },
                {
                    "name": "legal_person_session_id",
                    "label": "Ключ сессии (пусто = новый uuid4)",
                    "type": "text",
                    "placeholder": "",
                    "required": False,
                },
            ],
            "submit_label": "Отправить в MCP",
        },
        {
            "type": "status",
            "target": "mcp",
            "idle": "Введите вопрос и нажмите «Отправить в MCP»",
            "loading": "Запрос к MCP…",
            "error": "Ошибка запроса к MCP",
            "success": "Ответ MCP получен",
        },
        {
            "type": "kv",
            "target": "mcp",
            "fields": [
                {"label": "Session ID", "path": "legalPersonSessionId"},
                {"label": "Разделы", "path": "integrations_label"},
                {"label": "Сбор завершён", "path": "collected_label"},
                {"label": "Ошибка", "path": "error"},
            ],
        },
        {
            "type": "json",
            "target": "mcp",
            "label": "Ответ check_collect",
        },
        {
            "type": "markdown",
            "text": (
                "### Получить данные\n"
                "После `is_data_collected=true` запросите ответ с **тем же** Session ID "
                "и теми же разделами, что в check_collect (иначе снова будет кратко)."
            ),
        },
        {
            "type": "form",
            "route": "get_data",
            "method": "POST",
            "target": "data",
            "fields": [
                {
                    "name": "legal_person_session_id",
                    "label": "Session ID из ответа выше",
                    "type": "text",
                    "placeholder": "00000000-0000-4000-8000-000000000000",
                    "required": True,
                },
                {
                    "name": "integration_name",
                    "label": "Те же коды разделов, что в check_collect (если задавали)",
                    "type": "text",
                    "placeholder": "",
                    "required": False,
                },
            ],
            "submit_label": "Получить данные",
        },
        {
            "type": "status",
            "target": "data",
            "idle": "Укажите Session ID и нажмите «Получить данные»",
            "loading": "Запрашиваю данные…",
            "error": "Ошибка get_data",
            "success": "Данные получены",
        },
        {
            "type": "json",
            "target": "data",
            "label": "Ответ get_data",
        },
    ],
}


def _read_settings(api: Any) -> Dict[str, str]:
    try:
        raw = api.get_settings(list(_SETTINGS_KEYS)) or {}
    except Exception:
        raw = {}
    return {
        key: str(raw.get(key) or "") if key == "SBER_TLS_P12_PASSWORD"
        else str(raw.get(key) or "").strip()
        for key in _SETTINGS_KEYS
    }


def _state_dir(api: Any) -> str:
    try:
        raw = api.get_state_dir()
        return str(raw or "").strip()
    except Exception:
        return ""


def _resolve_tls_paths(api: Any, settings: Dict[str, str]) -> Tuple[str, str]:
    p12_password = settings.get("SBER_TLS_P12_PASSWORD", "")
    if not p12_password:
        return "", ""

    state_dir = _state_dir(api)
    if not state_dir:
        return "", ""
    return ensure_p12_tls(state_dir, p12_password)


def _resolve_ca_path(api: Any) -> str:
    state_dir = _state_dir(api)
    if not state_dir:
        return ""
    return resolve_ca_path(state_dir, resolve_mcp_url(_read_settings(api).get("SBER_MCP_URL", "")))


def _runtime_config(api: Any) -> Dict[str, str]:
    settings = _read_settings(api)
    cert_path, key_path = _resolve_tls_paths(api, settings)
    return {
        "url": resolve_mcp_url(settings.get("SBER_MCP_URL", "")),
        "access_token": settings.get("SBER_ACCESS_TOKEN", ""),
        "cert_path": cert_path,
        "key_path": key_path,
        "ca_path": _resolve_ca_path(api),
    }


def _path_exists(path: str) -> bool:
    text = (path or "").strip()
    if not text:
        return False
    try:
        return Path(text).expanduser().is_file()
    except Exception:
        return False


def _check_setup(api: Any) -> Dict[str, Any]:
    """Self-check: required settings/certs without leaking secret values."""
    settings = _read_settings(api)
    state_dir = _state_dir(api)
    mcp_url = resolve_mcp_url(settings.get("SBER_MCP_URL", ""))
    is_prom = "fintech.sberbank.ru" in mcp_url and "testsbi" not in mcp_url

    token = settings.get("SBER_ACCESS_TOKEN", "")
    p12_password = settings.get("SBER_TLS_P12_PASSWORD", "")
    p12_installed = bool(state_dir) and tls_is_installed(state_dir)
    ca_installed = bool(state_dir) and ca_is_installed(state_dir)
    ca_path = resolve_ca_path(state_dir, mcp_url)
    url_error = validate_mcp_url(mcp_url)
    ca_valid = False
    if ca_path:
        try:
            import ssl
            ssl.create_default_context(cafile=ca_path)
            ca_valid = True
        except Exception:
            pass

    cert_path, key_path = "", ""
    tls_error = ""
    try:
        cert_path, key_path = _resolve_tls_paths(api, settings)
        if cert_path and key_path:
            import ssl
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_cert_chain(cert_path, key_path)
    except Exception as exc:
        cert_path, key_path = "", ""
        tls_error = "не удалось прочитать P12; проверьте файл, пароль и доступ к state"

    checks: List[Dict[str, Any]] = []

    def add(
        item_id: str,
        *,
        ok: bool,
        required: bool,
        detail: str,
        how: str = "",
    ) -> None:
        checks.append(
            {
                "id": item_id,
                "title": _CHECK_TITLES.get(item_id, item_id),
                "mark": "OK" if ok else ("Нет" if required else "—"),
                "ok": ok,
                "required": required,
                "detail": detail,
                "how": how or ("—" if ok else "см. подсказки ниже"),
            }
        )

    add(
        "SBER_ACCESS_TOKEN",
        ok=bool(token),
        required=True,
        detail=("задан" if token else "отсутствует или нет grant"),
        how="Settings → Custom Keys, scope MCP_TRANSACT_AGENT, затем Grant скиллу",
    )
    add(
        "SBER_TLS_P12_PASSWORD",
        ok=bool(p12_password),
        required=True,
        detail=("задан" if p12_password else "отсутствует — нужен для установки .p12"),
        how="Settings → SBER_TLS_P12_PASSWORD + Grant",
    )
    add(
        "client_tls",
        ok=bool(cert_path and key_path and _path_exists(cert_path) and _path_exists(key_path)),
        required=True,
        detail=(
            (
                f"готово: cert={Path(cert_path).name}, key={Path(key_path).name}"
                if cert_path and key_path and _path_exists(cert_path) and _path_exists(key_path)
                else (
                    f"ошибка конвертации P12: {tls_error}"
                    if tls_error
                    else (
                        "P12 в state есть, но PEM не собран — проверьте пароль"
                        if p12_installed
                        else "нет клиентского сертификата"
                    )
                )
            )
        ),
        how="Прикрепите .p12 в чат → install_tls_certificate(source_path=...)",
    )
    add(
        "p12_in_state",
        ok=p12_installed,
        required=False,
        detail=(
            "установлен в state скилла"
            if p12_installed
            else "не установлен (ожидается install_tls_certificate)"
        ),
    )
    add(
        "server_ca",
        ok=ca_valid,
        required=True,
        detail=(("установлена пользователем" if ca_installed else "встроенный SberCA Root Ext (PROM)")
                if ca_valid else "отсутствует, повреждена или недоступна"),
        how="Прикрепите .crt/.pem CA банка в чат → install_ca_certificate(source_path=...)",
    )
    add(
        "SBER_MCP_URL",
        ok=not url_error,
        required=True,
        detail=f"эффективный endpoint: {mcp_url}",
        how="Пусто = PROM. Для IFT: ift или полный URL",
    )
    add(
        "state_dir",
        ok=bool(state_dir),
        required=True,
        detail=("доступен" if state_dir else "недоступен"),
    )

    missing = [c["id"] for c in checks if c["required"] and not c["ok"]]
    ready = not missing
    hints: List[str] = []
    steps: List[str] = []
    if not token:
        hints.append("Выдайте grant на SBER_ACCESS_TOKEN (scope MCP_TRANSACT_AGENT).")
        steps.append(
            "**Токен доступа** — Ouroboros: Settings → Custom Keys / Secrets → "
            "`SBER_ACCESS_TOKEN` (scope **MCP_TRANSACT_AGENT**), затем **Grant** "
            "на карточке скилла `sber_operational_director`."
        )
    if not p12_password:
        hints.append("Задайте SBER_TLS_P12_PASSWORD в Settings и выдайте grant.")
        steps.append(
            "**Пароль к .p12** — Settings → `SBER_TLS_P12_PASSWORD`, затем **Grant** "
            "скиллу."
        )
    if not (cert_path and key_path):
        hints.append(
            "Прикрепите клиентский .p12 в чат и вызовите install_tls_certificate."
        )
        steps.append(
            "**Клиентский сертификат** — прикрепите файл `.p12` / `.pfx` в **этот чат**; "
            "после загрузки я установлю его инструментом `install_tls_certificate`."
        )
        if p12_installed:
            steps[-1] = (
                "**Клиентский сертификат уже установлен, но не читается** — проверьте "
                "`SBER_TLS_P12_PASSWORD` и Grant в Settings. Если пароль верен, "
                "прикрепите исправный `.p12` / `.pfx` для повторной установки."
            )
    if not ca_valid:
        hints.append(
            "Прикрепите CA-цепочку банка (.crt/.pem) в чат и вызовите install_ca_certificate."
        )
        steps.append(
            "**CA банка** — прикрепите цепочку `.crt` / `.pem` (например `SberCA_Root_Ext.crt`) "
            "в **этот чат**; я установлю её через `install_ca_certificate`."
        )
    if url_error:
        hints.append("Исправьте SBER_MCP_URL: пусто или prod для PROM, ift для теста.")
        steps.append("**Адрес сервера** — исправьте `SBER_MCP_URL` в Settings: `prod` или `ift`; полный URL должен использовать HTTPS и разрешённый хост Сбера.")
    if not state_dir:
        hints.append("Каталог state скилла недоступен — переустановите/включите скилл.")
        steps.append(
            "**State скилла** — каталог состояния недоступен. В Skills заново Enable "
            "`sber_operational_director` или переустановите скилл."
        )
    if ready:
        hints.append("Конфигурация достаточна для работы.")

    hints_md = "## Подсказки\n" + "\n".join(f"- {hint}" for hint in hints) if hints else ""
    stand = "prom" if is_prom else ("ift" if "testsbi" in mcp_url else "other")

    if ready:
        chat_instruction = (
            "Локальная настройка скилла выполнена. Действительность токена, его права "
            "и доступность банка проверяются при запросе."
        )
    else:
        numbered = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, start=1))
        chat_instruction = (
            "Скилл «Операционный директор» пока **не настроен** — без этого "
            "запросы к банку выполнить нельзя.\n\n"
            "Нужно сделать следующее:\n\n"
            f"{numbered}\n\n"
            "Где смотреть статус: виджет **Операционный директор** → «Проверить настройку», "
            "либо напишите «проверь настройку операционного директора».\n\n"
            "Когда шаги выполнены (секреты + Grant + файлы в чате) — напишите снова, "
            "я проверю конфигурацию и продолжу ваш запрос."
        )

    return {
        "ready": ready,
        "ready_label": "Готово к работе" if ready else "Не готово — см. инструкцию для чата",
        "mcp_url": mcp_url,
        "stand": stand,
        "checks": checks,
        "missing_required": missing,
        "hints": hints,
        "hints_md": hints_md,
        "chat_instruction": chat_instruction,
    }


def _not_ready_payload(api: Any) -> Dict[str, Any]:
    """Config-error payload: agent must relay chat_instruction to the user."""
    setup = _check_setup(api)
    return {
        "error": "Скилл «Операционный директор» не настроен.",
        "ready": False,
        "missing_required": setup.get("missing_required") or [],
        "hints": setup.get("hints") or [],
        "chat_instruction": setup.get("chat_instruction") or "",
        "message_for_user": setup.get("chat_instruction") or "",
    }


def _invoke(api: Any, fn: Callable[..., Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
    if not _check_setup(api)["ready"]:
        return _not_ready_payload(api)
    cfg = _runtime_config(api)
    if (
        not cfg["access_token"]
        or not cfg["cert_path"]
        or not cfg["key_path"]
        or not _path_exists(cfg["ca_path"])
    ):
        return _not_ready_payload(api)
    try:
        result = fn(
            url=cfg["url"],
            access_token=cfg["access_token"],
            cert_path=cfg["cert_path"],
            key_path=cfg["key_path"],
            ca_path=cfg["ca_path"],
            **kwargs,
        )
    except Exception:
        result = {"error": "Не удалось выполнить запрос к банку. Проверьте TLS и доступность сервера."}
    if "error" in result:
        error = str(result["error"])
        if "HTTP 401" in error or "HTTP 403" in error:
            instruction = (
                "Банк отклонил авторизацию или доступ. Проверьте срок действия "
                "SBER_ACCESS_TOKEN, scope MCP_TRANSACT_AGENT и Grant скиллу в Settings. "
                "Убедитесь, что токен и клиентский сертификат относятся к выбранному контуру."
            )
        else:
            instruction = (
                "Запрос к банку завершился ошибкой; данные не получены. "
                "Проверьте доступность сервера и настройку скилла. "
                "При ошибке TLS проверьте пароль P12, клиентский сертификат и CA банка."
            )
        result = {**result, "chat_instruction": instruction, "message_for_user": instruction}
    return result


def _make_tool_handler(api: Any, fn: Callable[..., Dict[str, Any]], **fixed: Any):
    def _handler(**kwargs: Any) -> str:
        merged = {**fixed, **kwargs}
        return as_json(_invoke(api, fn, **merged))

    return _handler


def register(api: Any) -> None:
    """PluginAPI v1 entry point."""

    async def _route_check_setup(request: Request) -> JSONResponse:
        payload = await asyncio.to_thread(_check_setup, api)
        return JSONResponse(payload, status_code=200)

    async def _route_check_collect(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)

        payload = await asyncio.to_thread(
            _invoke,
            api,
            check_collect,
            legal_person_session_id=str(body.get("legal_person_session_id", "")),
            text_input=str(body.get("text_input", "")),
            integration_name=body.get("integration_name"),
        )
        if "error" not in payload:
            collected = bool(payload.get("is_data_collected"))
            names = payload.get("integrationName")
            if isinstance(names, list) and names:
                integrations_label = ", ".join(str(x) for x in names)
            else:
                integrations_label = "не заданы (краткий ответ)"
            payload = {
                **payload,
                "collected_label": "да" if collected else "нет — повторите с тем же Session ID",
                "integrations_label": integrations_label,
            }
        status = 200 if "error" not in payload else 502
        return JSONResponse(payload, status_code=status)

    async def _route_get_data(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)

        payload = await asyncio.to_thread(
            _invoke,
            api,
            get_data,
            legal_person_session_id=str(body.get("legal_person_session_id", "")),
            integration_name=body.get("integration_name"),
        )
        status = 200 if "error" not in payload else 502
        return JSONResponse(payload, status_code=status)

    async def _route_status(request: Request) -> JSONResponse:
        """Return one bounded snapshot of the bank-side async lifecycle."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)

        session_id = str(body.get("legal_person_session_id", ""))
        integration_name = body.get("integration_name")
        collected = await asyncio.to_thread(
            _invoke,
            api,
            check_collect,
            legal_person_session_id=session_id,
            text_input="",
            integration_name=integration_name,
        )
        if "error" in collected:
            return JSONResponse({**collected, "status": "failed"}, status_code=502)
        if not collected.get("is_data_collected"):
            return JSONResponse({"status": "pending", "check_collect": collected}, status_code=200)

        result = await asyncio.to_thread(
            _invoke,
            api,
            get_data,
            legal_person_session_id=str(collected.get("legalPersonSessionId") or session_id),
            integration_name=integration_name,
        )
        if "error" in result:
            return JSONResponse({**result, "status": "failed"}, status_code=502)
        if result.get("data") is None and integration_name:
            return JSONResponse({"status": "pending", "result": result}, status_code=200)
        return JSONResponse({"status": "completed", "result": result}, status_code=200)

    def _install_tls(source_path: str) -> str:
        settings = _read_settings(api)
        password = settings.get("SBER_TLS_P12_PASSWORD", "")
        state_dir = _state_dir(api)
        if not state_dir:
            return as_json({"error": "skill state directory is unavailable"})
        if not password:
            return as_json(
                {
                    "error": (
                        "Задайте SBER_TLS_P12_PASSWORD в Ouroboros Settings "
                        "и выдайте grant скиллу перед установкой сертификата."
                    )
                }
            )
        try:
            return as_json(install_p12_from_path(state_dir, source_path, password))
        except Exception as exc:
            return as_json({"error": str(exc)})

    def _install_ca(source_path: str) -> str:
        state_dir = _state_dir(api)
        if not state_dir:
            return as_json({"error": "skill state directory is unavailable"})
        try:
            return as_json(install_ca_from_path(state_dir, source_path))
        except Exception as exc:
            return as_json({"error": str(exc)})

    # Register SHORT tool names only. Ouroboros PluginAPI prefixes them to
    # ext_<len>_<skill>_<name> via extension_surface_name — do NOT pre-prefix.
    api.register_tool(
        "check_setup",
        lambda: as_json(_check_setup(api)),
        description=(
            "Самопроверка настройки скилла sber_operational_director. "
            "Если ready=false — ОБЯЗАТЕЛЬНО перескажите пользователю в чат поле "
            "chat_instruction целиком (что нужно и где настроить). "
            "Секреты не возвращает. Вызывайте в начале сценария и при любой "
            "ошибке настройки; не зовите check_collect, пока ready=false."
        ),
        schema={"type": "object", "properties": {}, "required": []},
        timeout_sec=30,
    )

    api.register_tool(
        "install_tls_certificate",
        _install_tls,
        description=(
            "Установить клиентский TLS-сертификат Sber API из прикреплённого в чате "
            ".p12/.pfx. Передайте source_path — путь к файлу из data/uploads/. "
            "Требует grant на SBER_TLS_P12_PASSWORD. После успеха обязательно вызовите "
            "check_setup; при ready=true выполните исходный запрос или проверку профиля "
            "через check_collect и get_data по SKILL.md. Не останавливайтесь на установке."
        ),
        schema={
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Абсолютный путь к загруженному .p12 или .pfx файлу.",
                },
            },
            "required": ["source_path"],
        },
        timeout_sec=90,
    )

    api.register_tool(
        "install_ca_certificate",
        _install_ca,
        description=(
            "Установить CA-цепочку банка для проверки TLS сервера из прикреплённого "
            "в чате .crt/.pem/.cer. Передайте source_path — путь к файлу из data/uploads/. "
            "Для PROM корневой CA уже встроен; отдельная цепочка нужна для IFT или замены. "
            "После успеха обязательно вызовите check_setup; при ready=true выполните "
            "исходный запрос или проверку профиля через check_collect и get_data по SKILL.md. "
            "Не останавливайтесь на установке."
        ),
        schema={
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Абсолютный путь к загруженному CA .crt/.pem файлу.",
                },
            },
            "required": ["source_path"],
        },
        timeout_sec=30,
    )

    api.register_tool(
        "check_collect",
        _make_tool_handler(api, check_collect),
        description=(
            "АСИНХРОННЫЙ запуск сбора данных ИИ-агента «Операционный директор» Сбера "
            "(MCP summarise.agent_check_collect). Ставит задачу на стороне банка и "
            "быстро возвращает флаги: data (есть ли что собирать) и is_data_collected "
            "(завершён ли сбор). Сам вызов НЕ ждёт результата. "
            "Если скилл не настроен — вернётся error + chat_instruction: "
            "перескажите chat_instruction пользователю в чат и НЕ продолжайте сбор. "
            "legal_person_session_id — ключ идемпотентности (uuid4): сгенерируйте его "
            "сами при первом запросе (или оставьте пустым — сгенерируется автоматически) "
            "и верните в ответе; НЕ спрашивайте его у пользователя. "
            "Для ПОДРОБНОГО ответа сами подберите integration_name по смыслу "
            "бизнес-вопроса клиента (не спрашивайте коды у пользователя): "
            "ограничения/блокировки→fskk, задолженность→udkz, справки→documents, "
            "отзыв документов→documentCancel, бизнес-карты→business_card, "
            "профиль→client_profile, задачи→tasklist, доверенности→authority, "
            "обороты→account_turns. Без кодов ответ будет кратким. "
            "Если is_data_collected=false — подождите 10-30 секунд и повторите "
            "check_collect с ТЕМ ЖЕ ключом и теми же разделами, пока не станет true; "
            "только после этого вызывайте get_data с тем же ключом и теми же разделами. "
            "Опционально text_input — формулировка клиента. "
            "В ответе клиенту — бизнес-вывод без перечисления кодов. "
            "Токен со scope MCP_TRANSACT_AGENT."
        ),
        schema={
            "type": "object",
            "properties": {
                "legal_person_session_id": {
                    "type": "string",
                    "description": (
                        "Ключ идемпотентности uuid4. Генерирует агент при запросе "
                        "(пусто = автогенерация). У пользователя НЕ спрашивать."
                    ),
                },
                "text_input": {
                    "type": "string",
                    "description": (
                        "Бизнес-вопрос или инструкция клиента для ИИ-агента "
                        "(без технических кодов разделов)."
                    ),
                },
                "integration_name": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(INTEGRATION_NAMES),
                    },
                    "description": (
                        "Коды разделов, подобранные агентом по смыслу вопроса. "
                        "Без них — краткий ответ. Не спрашивать у клиента. "
                        "Те же коды затем передайте в get_data."
                    ),
                },
            },
            "required": [],
        },
        timeout_sec=60,
    )

    api.register_tool(
        "get_data",
        _make_tool_handler(api, get_data),
        description=(
            "Получить результат АСИНХРОННОГО сбора ИИ-агента «Операционный директор» "
            "(MCP summarise.agent_get_data) по legal_person_session_id — тому же "
            "ключу идемпотентности, который агент сгенерировал/получил в check_collect. "
            "У пользователя этот ключ НЕ спрашивать. "
            "Вызывать только после того, как check_collect вернул is_data_collected=true. "
            "Передайте те же integration_name, что были в check_collect — иначе ответ "
            "будет кратким (short_summary). С фильтром разделов — полный ответ "
            "в поле data; data может быть null, пока генерация не завершилась — тогда "
            "подождите 10-30 секунд и повторите get_data с теми же аргументами. "
            "Пустой список data означает, что по сессии (ещё) нет данных. "
            "Клиенту отвечайте по смыслу, без перечисления кодов разделов."
        ),
        schema={
            "type": "object",
            "properties": {
                "legal_person_session_id": {
                    "type": "string",
                    "description": (
                        "Ключ идемпотентности uuid4 из ответа check_collect. "
                        "У пользователя НЕ спрашивать."
                    ),
                },
                "integration_name": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(INTEGRATION_NAMES),
                    },
                    "description": (
                        "Те же коды, что агент передал в check_collect. "
                        "Нужны для подробного ответа. У клиента не спрашивать."
                    ),
                },
            },
            "required": ["legal_person_session_id"],
        },
        timeout_sec=60,
    )

    api.register_route("check_setup", _route_check_setup, methods=("POST",))
    api.register_route("check_collect", _route_check_collect, methods=("POST",))
    api.register_route("get_data", _route_get_data, methods=("POST",))
    api.register_route("status", _route_status, methods=("POST",))
    api.register_ui_tab(
        "operational_director",
        "Операционный директор",
        icon="🏦",
        render=_UI_RENDER,
    )

    api.log(
        "info",
        "sber_operational_director: extension registered (5 tools, 4 routes, ui_tab)",
    )


__all__ = ["register"]
