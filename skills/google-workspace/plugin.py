"""Google Workspace Extension Plugin for Ouroboros.

Exposes tools for Google Sheets, Docs, Drive, Slides and Calendar, settings
configuration, and a declarative status UI tab.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

try:
    from starlette.responses import JSONResponse as StarletteJSONResponse
except ImportError:
    StarletteJSONResponse = None

try:
    from .auth import parse_service_account_info
    from .client import GoogleWorkspaceClient, VALUE_RENDER_OPTIONS
    from .results import file_extension, result_handler, store_state_bytes
except ImportError:
    from auth import parse_service_account_info
    from client import GoogleWorkspaceClient, VALUE_RENDER_OPTIONS
    from results import file_extension, result_handler, store_state_bytes

if TYPE_CHECKING:
    from contracts.plugin_api import PluginAPI

logger = logging.getLogger(__name__)


def _format_json(data: Any) -> str:
    """Format dictionary/list into compact JSON string."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _get_client_sa_json(api: PluginAPI) -> Optional[str]:
    """Retrieve the granted GOOGLE_SERVICE_ACCOUNT_JSON secret from host settings."""
    try:
        settings_dict = api.get_settings(["GOOGLE_SERVICE_ACCOUNT_JSON"])
        val = settings_dict.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if val and str(val).strip():
            return str(val).strip()
        return None
    except Exception as exc:
        try:
            api.log("error", f"Failed to retrieve settings/secrets from host: {exc}")
        except Exception:
            pass
        raise RuntimeError(f"Error reading GOOGLE_SERVICE_ACCOUNT_JSON from host settings: {exc}") from exc


def _get_oauth_credentials(api: PluginAPI) -> Dict[str, Optional[str]]:
    """Read only the user OAuth secrets explicitly granted to this skill."""
    names = {
        "access_token": "GOOGLE_OAUTH_ACCESS_TOKEN",
        "client_id": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret": "GOOGLE_OAUTH_CLIENT_SECRET",
        "refresh_token": "GOOGLE_OAUTH_REFRESH_TOKEN",
    }
    settings = api.get_settings(list(names.values()))
    return {key: str(settings.get(name) or "").strip() or None for key, name in names.items()}


def _make_client(api: PluginAPI, auth_mode: str = "service_account", subject: Optional[str] = None) -> GoogleWorkspaceClient:
    """Construct one of the two explicit Google auth routes.

    ``subject`` is deliberately an operation argument.  Domain-wide delegation
    is never activated from a setting or inferred from a service-account email.
    """
    mode = str(auth_mode or "service_account").strip().lower()
    if mode == "oauth":
        credentials = _get_oauth_credentials(api)
        if not credentials["access_token"] and not all(credentials[key] for key in ("client_id", "client_secret", "refresh_token")):
            raise RuntimeError("Configure and grant GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REFRESH_TOKEN, or a short-lived GOOGLE_OAUTH_ACCESS_TOKEN.")
        return GoogleWorkspaceClient(**credentials)
    if mode != "service_account":
        raise ValueError("auth_mode must be 'service_account' or 'oauth'.")
    return GoogleWorkspaceClient(raw_sa_info=_get_client_sa_json(api), subject=subject)


def _get_local_settings(api: PluginAPI) -> Dict[str, Any]:
    """Read local settings from the skill state directory with strict type validation."""
    try:
        state_dir = Path(api.get_state_dir())
        settings_path = state_dir / "settings.json"
        if not settings_path.is_file():
            return {}
        parsed = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            try:
                api.log("warning", f"Local settings.json is not a JSON object: {type(parsed).__name__}")
            except Exception:
                pass
            return {}
        return parsed
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        try:
            api.log("warning", f"Failed to read local settings.json: {exc}")
        except Exception:
            pass
        return {}


def _resolve_folder_id(api: PluginAPI, passed_folder_id: Optional[str]) -> Optional[str]:
    """Resolve target folder ID from parameter or default setting."""
    if passed_folder_id and passed_folder_id.strip():
        return passed_folder_id.strip()
    local_cfg = _get_local_settings(api)
    return local_cfg.get("DEFAULT_FOLDER_ID") or None


def _resolve_template_id(api: PluginAPI, template_id: Optional[str]) -> Optional[str]:
    """Resolve template ID from raw ID or template map with typed error handling."""
    if not template_id or not template_id.strip():
        return None
    tid = template_id.strip()
    local_cfg = _get_local_settings(api)
    templates_raw = local_cfg.get("TEMPLATES_JSON")
    if templates_raw:
        try:
            template_map = json.loads(templates_raw) if isinstance(templates_raw, str) else templates_raw
            if isinstance(template_map, dict) and tid in template_map:
                return str(template_map[tid]).strip()
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            try:
                api.log("warning", f"Invalid TEMPLATES_JSON configuration mapping: {exc}")
            except Exception:
                pass
    return tid


# --- Tool Handlers ---


def _make_workspace_auth_status(api: PluginAPI):
    def workspace_auth_status(auth_mode: str = "service_account", subject: str = "") -> str:
        """Verify the actual Google actor with Drive about.user, without writes."""
        mode = str(auth_mode or "service_account").strip().lower()
        payload: Dict[str, Any] = {
            "status": "error", "auth_mode": mode, "configured": False,
            "verified": False, "actor": None, "resource_access": "not_checked",
        }
        try:
            with _make_client(api, mode, subject.strip() or None) as client:
                if mode == "service_account":
                    info = parse_service_account_info(client.raw_sa_info)
                    payload.update(client_email=info.get("client_email"), project_id=info.get("project_id"),
                                   delegated_subject=subject.strip() or None)
                else:
                    payload["refresh_configured"] = bool(client.client_id and client.client_secret and client.refresh_token)
                payload["configured"] = True
                result = client.workspace_request("drive", "GET", "about", query={"fields": "user"})
                actor = result.get("data", {}).get("user")
                if not isinstance(actor, dict) or not (actor.get("emailAddress") or actor.get("permissionId")):
                    raise RuntimeError("Google Drive did not return a verifiable user identity.")
                payload.update(status="ready", verified=True, actor=actor,
                               verification_scope="drive.about.user",
                               message="Google Drive verified this actor. Individual file access and write permissions require separate checks.")
        except Exception as exc:
            payload["message"] = str(exc)
        return _format_json(payload)

    return workspace_auth_status


def _make_workspace_request(api: PluginAPI):
    def workspace_request(service: str, method: str, path: str,
                          query: Optional[Dict[str, Any]] = None, json_body: Optional[Any] = None,
                          auth_mode: str = "service_account") -> str:
        with _make_client(api, auth_mode) as client:
            return _format_json(client.workspace_request(service, method, path, query, json_body))
    return workspace_request


def _make_sheets_info(api: PluginAPI):
    def sheets_info(spreadsheet_id: str, auth_mode: str = "service_account") -> str:
        with _make_client(api, auth_mode) as client:
            return _format_json(client.sheets_info(spreadsheet_id=spreadsheet_id))

    return sheets_info


def _make_sheets_read(api: PluginAPI):
    def sheets_read(spreadsheet_id: str, range: str, max_rows: int = 5000,
                    value_render_option: str = "FORMATTED_VALUE", auth_mode: str = "service_account") -> str:
        """Read cell values from a rectangular range in a Google Sheet."""
        try:
            with _make_client(api, auth_mode) as client:
                res = client.sheets_read(spreadsheet_id=spreadsheet_id, range_name=range,
                                         max_rows=max_rows, value_render_option=value_render_option)
                return _format_json(res)
        except Exception as exc:
            raise RuntimeError(f"sheets_read failed: {exc}") from exc

    return sheets_read


def _make_sheets_append(api: PluginAPI):
    def sheets_append(spreadsheet_id: str, range: str, rows: List[List[Any]], value_input_option: str = "USER_ENTERED", auth_mode: str = "service_account") -> str:
        """Append rows of data to a spreadsheet table."""
        try:
            with _make_client(api, auth_mode) as client:
                res = client.sheets_append(
                    spreadsheet_id=spreadsheet_id,
                    range_name=range,
                    rows=rows,
                    value_input_option=value_input_option,
                )
                return _format_json(res)
        except Exception as exc:
            raise RuntimeError(f"sheets_append failed: {exc}") from exc

    return sheets_append


def _make_docs_create(api: PluginAPI):
    def docs_create(title: str, folder_id: Optional[str] = None, template_id: Optional[str] = None, auth_mode: str = "service_account") -> str:
        """Create a new Google Document from scratch or by copying a template."""
        effective_folder_id = _resolve_folder_id(api, folder_id)
        effective_template_id = _resolve_template_id(api, template_id)

        try:
            with _make_client(api, auth_mode) as client:
                res = client.docs_create(
                    title=title,
                    folder_id=effective_folder_id,
                    template_id=effective_template_id,
                )
                return _format_json(res)
        except Exception as exc:
            raise RuntimeError(f"docs_create failed: {exc}") from exc

    return docs_create


def _make_drive_list(api: PluginAPI):
    def drive_list(folder_id: Optional[str] = None, page_size: int = 50, page_token: Optional[str] = None,
                  query: Optional[str] = None, name: Optional[str] = None, full_text: Optional[str] = None,
                  corpora: Optional[str] = None, drive_id: Optional[str] = None, spaces: Optional[str] = None, order_by: Optional[str] = None,
                  auth_mode: str = "service_account") -> str:
        """List files and folders shared with the Service Account or inside a folder."""
        effective_folder_id = _resolve_folder_id(api, folder_id)

        try:
            with _make_client(api, auth_mode) as client:
                res = client.drive_list(
                    folder_id=effective_folder_id,
                    page_size=page_size,
                    page_token=page_token,
                    query=query, name=name, full_text=full_text,
                    corpora=corpora, drive_id=drive_id, spaces=spaces, order_by=order_by,
                )
                return _format_json(res)
        except Exception as exc:
            raise RuntimeError(f"drive_list failed: {exc}") from exc

    return drive_list


def _make_drive_read_text(api: PluginAPI):
    def drive_read_text(file_id: str, max_chars: int = 100000, auth_mode: str = "service_account") -> str:
        """Read or export the plain text content of a Google Doc, Sheet, or text file."""
        try:
            with _make_client(api, auth_mode) as client:
                res = client.drive_read_text(file_id=file_id, max_chars=max_chars)
                return _format_json(res)
        except Exception as exc:
            raise RuntimeError(f"drive_read_text failed: {exc}") from exc

    return drive_read_text


def _stage_drive_bytes(api: PluginAPI, result: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a provider response in skill state and return an inspectable path."""
    content = bytes(result.get("content") or b"")
    mime = str(result.get("mime_type") or "")
    extension = file_extension(mime)
    stored = store_state_bytes(api, content, name=str(result.get("name") or result.get("file_id") or "download"), extension=extension)
    return {k: v for k, v in result.items() if k != "content"} | stored


def _make_drive_download(api: PluginAPI):
    def drive_download(file_id: str, export_mime_type: Optional[str] = None, max_bytes: int = 50 * 1024 * 1024,
                       auth_mode: str = "service_account") -> str:
        with _make_client(api, auth_mode) as client:
            result = client.drive_download(file_id=file_id, export_mime_type=export_mime_type, max_bytes=max_bytes)
        return _format_json(_stage_drive_bytes(api, result))

    return drive_download


def _make_drive_export(api: PluginAPI):
    def drive_export(file_id: str, mime_type: str, max_bytes: int = 50 * 1024 * 1024,
                     auth_mode: str = "service_account") -> str:
        with _make_client(api, auth_mode) as client:
            result = client.drive_export(file_id=file_id, mime_type=mime_type, max_bytes=max_bytes)
        return _format_json(_stage_drive_bytes(api, result))

    return drive_export


def _make_drive_upload(api: PluginAPI):
    def drive_upload(name: str = "", content_base64: str = "", local_path: str = "",
                     mime_type: str = "application/octet-stream", folder_id: Optional[str] = None,
                     auth_mode: str = "service_account") -> str:
        if local_path:
            source = Path(local_path).expanduser()
            if not source.is_file():
                raise ValueError("local_path must point to a file.")
            content = source.read_bytes()
            effective_name = name or source.name
        elif content_base64:
            try:
                content = base64.b64decode(content_base64, validate=True)
            except Exception as exc:
                raise ValueError(f"content_base64 is invalid: {exc}") from exc
            effective_name = name
        else:
            raise ValueError("Provide content_base64 or local_path.")
        with _make_client(api, auth_mode) as client:
            result = client.drive_upload(name=effective_name, content=content, mime_type=mime_type, folder_id=folder_id)
        return _format_json(result)

    return drive_upload


def _make_docs_read(api: PluginAPI):
    def docs_read(document_id: str, auth_mode: str = "service_account") -> str:
        with _make_client(api, auth_mode) as client:
            return _format_json(client.docs_read(document_id=document_id))
    return docs_read


def _make_docs_update(api: PluginAPI):
    def docs_update(document_id: str, requests: List[Dict[str, Any]], readback: bool = True,
                    write_control: Optional[Dict[str, Any]] = None, auth_mode: str = "service_account") -> str:
        with _make_client(api, auth_mode) as client:
            return _format_json(client.docs_update(document_id=document_id, requests=requests, readback=readback, write_control=write_control))
    return docs_update


def _make_sheets_update(api: PluginAPI):
    def sheets_update(spreadsheet_id: str, range: str, values: List[List[Any]],
                      value_input_option: str = "USER_ENTERED", auth_mode: str = "service_account") -> str:
        with _make_client(api, auth_mode) as client:
            return _format_json(client.sheets_update(spreadsheet_id=spreadsheet_id, range_name=range, values=values, value_input_option=value_input_option))
    return sheets_update


def _make_sheets_batch_update(api: PluginAPI):
    def sheets_batch_update(spreadsheet_id: str, requests: List[Dict[str, Any]], auth_mode: str = "service_account") -> str:
        with _make_client(api, auth_mode) as client:
            return _format_json(client.sheets_batch_update(spreadsheet_id=spreadsheet_id, requests=requests))
    return sheets_batch_update


# --- Settings Save Route Handler ---


def _make_settings_save(api: PluginAPI):
    async def settings_save(request: Any) -> Any:
        if getattr(request, "method", "POST") == "GET":
            current = _get_local_settings(api)
            values = {key: current.get(key, "") for key in ("DEFAULT_FOLDER_ID", "TEMPLATES_JSON")}
            return StarletteJSONResponse(values) if StarletteJSONResponse is not None else values
        try:
            payload = await request.json()
        except Exception as exc:
            err_data = {"status": "error", "message": f"Invalid JSON payload: {exc}"}
            if StarletteJSONResponse is not None:
                return StarletteJSONResponse(err_data, status_code=400)
            return err_data

        if not isinstance(payload, dict):
            err_data = {"status": "error", "message": "Expected JSON object."}
            if StarletteJSONResponse is not None:
                return StarletteJSONResponse(err_data, status_code=400)
            return err_data

        default_folder = str(payload.get("DEFAULT_FOLDER_ID", "")).strip()
        templates_json = str(payload.get("TEMPLATES_JSON", "")).strip()

        if templates_json:
            try:
                parsed_t = json.loads(templates_json)
                if not isinstance(parsed_t, dict):
                    err_data = {"status": "error", "message": "TEMPLATES_JSON must be a JSON object mapping alias -> doc_id."}
                    if StarletteJSONResponse is not None:
                        return StarletteJSONResponse(err_data, status_code=400)
                    return err_data
            except Exception as exc:
                err_data = {"status": "error", "message": f"Invalid JSON in TEMPLATES_JSON: {exc}"}
                if StarletteJSONResponse is not None:
                    return StarletteJSONResponse(err_data, status_code=400)
                return err_data

        state_dir = Path(api.get_state_dir())
        state_dir.mkdir(parents=True, exist_ok=True)
        settings_path = state_dir / "settings.json"

        settings_data = {
            "DEFAULT_FOLDER_ID": default_folder,
            "TEMPLATES_JSON": templates_json,
        }
        try:
            settings_path.write_text(json.dumps(settings_data, indent=2), encoding="utf-8")
        except Exception as exc:
            err_data = {"status": "error", "message": f"Failed to persist settings: {exc}"}
            if StarletteJSONResponse is not None:
                return StarletteJSONResponse(err_data, status_code=500)
            return err_data

        ok_data = {"status": "ok", "message": "Settings saved successfully."}
        if StarletteJSONResponse is not None:
            return StarletteJSONResponse(ok_data, status_code=200)
        return ok_data

    return settings_save


# --- Plugin Registration Entrypoint ---


def register(api: PluginAPI) -> None:
    """Register Google Workspace tools, settings sections, and declarative UI tab with Ouroboros."""

    # 1. Register Tools
    def register_tool(*, name, handler, description, **options):
        api.register_tool(name=name, handler=result_handler(api, name, handler),
                          description=description + " Large JSON/text results include a complete file and a read_file pointer. Binary exports provide a file path for document/image tools or scripts. Small JSON responses remain inline.", **options)

    register_tool(
        name="workspace_auth_status",
        handler=_make_workspace_auth_status(api),
        description="Verify the selected OAuth or Service Account actor with a read-only Google Drive user request. File permissions remain separate.",
        schema={
            "type": "object",
            "properties": {
                "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"},
                "subject": {"type": "string", "description": "Optional explicit domain-wide delegation subject. Never inferred from settings."},
            },
        },
        timeout_sec=90,
    )

    register_tool(
        name="workspace_request",
        handler=_make_workspace_request(api),
        description="Call any Drive v3, Docs v1, Sheets v4, Slides v1 or Calendar v3 REST operation with the selected identity. Supply a relative API path, query and JSON body; Google enforces scopes and permissions. Examples: hide a slide with slides POST presentations/ID:batchUpdate and an updateSlideProperties request (fields 'isSkipped'); attach a Drive file with calendar PATCH calendars/primary/events/EVENT_ID, query supportsAttachments=true and the event's full attachments list, only when the event shows organizer.self or guestsCanModify true, then read it back. calendars/primary is the OAuth user's calendar; a service account must use the ID of a calendar shared with it. No automatic retry after transport errors.",
        schema={"type": "object", "properties": {
            "service": {"type": "string", "enum": ["drive", "docs", "sheets", "slides", "calendar"]},
            "method": {"type": "string", "description": "HTTP method, e.g. GET, POST, PATCH, PUT, DELETE."},
            "path": {"type": "string", "description": "Relative to service API root, e.g. files/ID/permissions, documents/ID:batchUpdate, presentations/ID:batchUpdate or calendars/primary/events/EVENT_ID. Percent-encode calendar IDs that contain # or /."},
            "query": {"type": "object"}, "json_body": {},
            "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"},
        }, "required": ["service", "method", "path"]}, timeout_sec=90,
    )

    register_tool(
        name="sheets_info",
        handler=_make_sheets_info(api),
        description="Discover spreadsheet title, locale, timezone and tabs (IDs, names and grid dimensions) without reading cells. Use tab names in sheets_read ranges.",
        schema={
            "type": "object",
            "properties": {"spreadsheet_id": {"type": "string", "description": "Google Spreadsheet ID."}, "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"}},
            "required": ["spreadsheet_id"],
        },
        timeout_sec=60,
    )

    register_tool(
        name="sheets_read",
        handler=_make_sheets_read(api),
        description="Read rectangular cell values from a Google Spreadsheet range (e.g. 'Sheet1!A1:D10' or 'A1:C').",
        schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Google Spreadsheet ID (found in URL: https://docs.google.com/spreadsheets/d/<ID>/edit).",
                },
                "range": {
                    "type": "string",
                    "description": "A1 notation range to read (e.g. 'Sheet1!A1:E20', 'RawData!A:C').",
                },
                "max_rows": {
                    "type": "integer",
                    "description": "Maximum rows to return (default 5000).",
                    "default": 5000,
                },
                "value_render_option": {
                    "type": "string",
                    "enum": list(VALUE_RENDER_OPTIONS),
                    "default": "FORMATTED_VALUE",
                    "description": "FORMATTED_VALUE returns displayed values; UNFORMATTED_VALUE returns calculated values without formatting; FORMULA returns formulas instead of their calculated results.",
                },
                "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"},
            },
            "required": ["spreadsheet_id", "range"],
        },
        timeout_sec=60,
    )

    register_tool(
        name="sheets_append",
        handler=_make_sheets_append(api),
        description="Append rows of data to a table in a Google Spreadsheet.",
        schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Google Spreadsheet ID.",
                },
                "range": {
                    "type": "string",
                    "description": "A1 range or sheet name to append to (e.g. 'Sheet1!A1' or 'Sheet1').",
                },
                "rows": {
                    "type": "array",
                    "description": "Array of rows (each row is an array of cell values) to append.",
                    "items": {"type": "array", "items": {}},
                },
                "value_input_option": {
                    "type": "string",
                    "description": "How input data should be interpreted: 'USER_ENTERED' (parses numbers/dates/formulas) or 'RAW'.",
                    "enum": ["USER_ENTERED", "RAW"],
                    "default": "USER_ENTERED",
                },
                "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"},
            },
            "required": ["spreadsheet_id", "range", "rows"],
        },
        timeout_sec=60,
    )

    register_tool(
        name="docs_create",
        handler=_make_docs_create(api),
        description="Create a new Google Document from scratch or by copying an existing Google Doc template. Returns the document ID and edit URL.",
        schema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Title of the new document.",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Optional Google Drive folder ID where the document should be created (falls back to DEFAULT_FOLDER_ID setting).",
                },
                "template_id": {
                    "type": "string",
                    "description": "Optional Google Doc template ID or template alias name configured in settings.",
                },
                "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"},
            },
            "required": ["title"],
        },
        timeout_sec=60,
    )

    register_tool(
        name="drive_list",
        handler=_make_drive_list(api),
        description="List accessible files and folders, including owners, last modifier, version and capabilities when Google provides them. Follow next_page_token for more files.",
        schema={
            "type": "object",
            "properties": {
                "folder_id": {
                    "type": "string",
                    "description": "Optional parent folder ID to list files from (falls back to DEFAULT_FOLDER_ID setting).",
                },
                "page_size": {
                    "type": "integer",
                    "description": "Number of files to return (1-100, default 50).",
                    "default": 50,
                },
                "page_token": {
                    "type": "string",
                    "description": "Optional pagination token from previous list call.",
                },
                "query": {"type": "string", "description": "Optional Drive q fragment."},
                "name": {"type": "string", "description": "Exact file name filter."},
                "full_text": {"type": "string", "description": "Full-text contains filter."},
                "corpora": {"type": "string", "description": "Drive corpus, for example user, domain, drive, or allDrives."},
                "drive_id": {"type": "string", "description": "Shared Drive ID when corpora=drive."},
                "spaces": {"type": "string", "description": "Drive spaces, for example drive or appDataFolder."},
                "order_by": {"type": "string", "description": "Drive orderBy expression."},
                "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"},
            },
        },
        timeout_sec=60,
    )

    register_tool(
        name="drive_read_text",
        handler=_make_drive_read_text(api),
        description="Read plain text from a Google Doc or text file with available owner/version metadata. Google Sheets CSV includes only the first tab; use sheets_info and sheets_read for others. truncated reports character clipping, not workbook coverage.",
        schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "Google Drive file ID.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 100000).",
                    "default": 100000,
                },
                "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"},
            },
            "required": ["file_id"],
        },
        timeout_sec=60,
    )

    register_tool(
        name="drive_download",
        handler=_make_drive_download(api),
        description="Download or export Drive bytes to an immutable task-readable file and return its path, MIME type, size and checksum.",
        schema={"type": "object", "properties": {
            "file_id": {"type": "string"}, "export_mime_type": {"type": "string"},
            "max_bytes": {"type": "integer", "default": 52428800},
            "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"},
        }, "required": ["file_id"]}, timeout_sec=120,
    )

    register_tool(
        name="drive_export",
        handler=_make_drive_export(api),
        description="Export a native Google document to a requested MIME type and return the immutable file path, MIME type, size and checksum.",
        schema={"type": "object", "properties": {
            "file_id": {"type": "string"}, "mime_type": {"type": "string"},
            "max_bytes": {"type": "integer", "default": 52428800},
            "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"},
        }, "required": ["file_id", "mime_type"]}, timeout_sec=120,
    )

    register_tool(
        name="drive_upload",
        handler=_make_drive_upload(api),
        description="Upload binary content from an explicit local path or base64 payload into Drive.",
        schema={"type": "object", "properties": {
            "name": {"type": "string"}, "content_base64": {"type": "string"},
            "local_path": {"type": "string"}, "mime_type": {"type": "string", "default": "application/octet-stream"},
            "folder_id": {"type": "string"}, "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"},
        }}, timeout_sec=120,
    )

    register_tool(
        name="docs_read",
        handler=_make_docs_read(api),
        description="Read structured Google Docs JSON including body elements and revision metadata.",
        schema={"type": "object", "properties": {"document_id": {"type": "string"}, "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"}}, "required": ["document_id"]}, timeout_sec=60,
    )

    register_tool(
        name="docs_update",
        handler=_make_docs_update(api),
        description="Apply Google Docs batchUpdate requests and optionally read the document back.",
        schema={"type": "object", "properties": {"document_id": {"type": "string"}, "requests": {"type": "array", "items": {"type": "object"}}, "readback": {"type": "boolean", "default": True}, "write_control": {"type": "object"}, "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"}}, "required": ["document_id", "requests"]}, timeout_sec=90,
    )

    register_tool(
        name="sheets_update",
        handler=_make_sheets_update(api),
        description="Update a rectangular Google Sheets range.",
        schema={"type": "object", "properties": {"spreadsheet_id": {"type": "string"}, "range": {"type": "string"}, "values": {"type": "array", "items": {"type": "array", "items": {}}}, "value_input_option": {"type": "string", "enum": ["USER_ENTERED", "RAW"], "default": "USER_ENTERED"}, "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"}}, "required": ["spreadsheet_id", "range", "values"]}, timeout_sec=90,
    )

    register_tool(
        name="sheets_batch_update",
        handler=_make_sheets_batch_update(api),
        description="Apply structural Google Sheets batchUpdate requests.",
        schema={"type": "object", "properties": {"spreadsheet_id": {"type": "string"}, "requests": {"type": "array", "items": {"type": "object"}}, "auth_mode": {"type": "string", "enum": ["service_account", "oauth"], "default": "service_account"}}, "required": ["spreadsheet_id", "requests"]}, timeout_sec=90,
    )

    # 2. Register HTTP Routes

    api.register_route(
        path="settings/save",
        handler=_make_settings_save(api),
        methods=("GET", "POST"),
    )

    # 3. Register Settings Section

    api.register_settings_section(
        section_id="google_workspace",
        title="Google Workspace",
        schema={
            "components": [
                {
                    "type": "markdown",
                    "text": "Configure default folder targets and template mappings for Google Workspace tools.",
                },
                {
                    "type": "form",
                    "route": "settings/save",
                    "method": "POST",
                    "submit_label": "Save Workspace Settings",
                    "fields": [
                        {
                            "name": "DEFAULT_FOLDER_ID",
                            "label": "Default Drive Folder ID",
                            "type": "text",
                            "placeholder": "e.g. 1aBcDeFgHiJkLmNoPqRsTuVwXyZ",
                            "help": "Default Google Drive folder used when no folder_id parameter is passed.",
                        },
                        {
                            "name": "TEMPLATES_JSON",
                            "label": "Document Templates Map (JSON)",
                            "type": "textarea",
                            "placeholder": '{\n  "weekly_report": "1xYzDocId...",\n  "meeting_notes": "1aBcDocId..."\n}',
                            "help": "Optional JSON dictionary mapping template alias names to template Google Doc IDs.",
                        },
                    ],
                },
            ]
        },
    )

    # 4. Register Declarative UI Tab

    api.register_ui_tab(
        tab_id="google_workspace",
        title="Google Workspace",
        render={
            "kind": "declarative",
            "schema_version": 1,
            "components": [
                {
                    "type": "callout",
                    "title": "Google Workspace Integration",
                    "tone": "info",
                    "text": "Access Sheets, Docs, Drive, Slides and Calendar using an explicitly selected OAuth user or service account. Verify the authenticated actor and the actual resource access separately.",
                },
                {
                    "type": "group",
                    "title": "Quick Start Guide",
                    "components": [
                        {
                            "type": "markdown",
                            "text": (
                                "1. **Choose credentials**: Add `GOOGLE_SERVICE_ACCOUNT_JSON`, or the `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` and `GOOGLE_OAUTH_REFRESH_TOKEN` granted by your user OAuth flow, under **Settings → Secrets**. A temporary `GOOGLE_OAUTH_ACCESS_TOKEN` also works.\n"
                                "2. **Verify access**: Call `workspace_auth_status(auth_mode='oauth')` for the user route or `auth_mode='service_account'` for the shared-resource route; then read the intended document. Service accounts do not inherit domain-wide sharing.\n"
                                "3. **Set Defaults**: Open Settings to configure `DEFAULT_FOLDER_ID` and template mappings."
                            ),
                        }
                    ],
                },
                {
                    "type": "group",
                    "title": "Available Tools",
                    "components": [
                        {
                            "type": "markdown",
                            "text": (
                                "- `sheets_info(spreadsheet_id)`: Discover spreadsheet metadata and tabs without reading cells.\n"
                                "- `sheets_read(spreadsheet_id, range, max_rows=5000, value_render_option='FORMATTED_VALUE')`: Read values or formulas from a selected range.\n"
                                "- `sheets_append(spreadsheet_id, range, rows)`: Append rows to Google Sheets.\n"
                                "- `docs_create(title, folder_id, template_id)`: Create or duplicate Google Docs.\n"
                                "- `drive_list(...)`: Search by name or content and paginate accessible files.\n"
                                "- `drive_read_text(file_id)`: Export text from Docs, Sheets, or plain text files.\n"
                                "- `docs_read` / `docs_update`: Read document structure and apply provider batch requests.\n"
                                "- `sheets_update` / `sheets_batch_update`: Update values or workbook structure.\n"
                                "- `drive_download` / `drive_export` / `drive_upload`: Exchange file bytes.\n"
                                "- `workspace_request`: Use Drive, Docs, Sheets, Slides or Calendar REST operations with the selected credentials.\n"
                                "- `workspace_auth_status`: Verify the selected route and return its actual actor."
                            ),
                        }
                    ],
                },
            ],
        },
    )

    def cleanup() -> None:
        pass

    api.on_unload(cleanup)
