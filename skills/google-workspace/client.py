"""Google Workspace REST Client for Sheets, Docs, Drive, Slides and Calendar APIs."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit, parse_qs
import httpx

try:
    from .auth import CALENDAR_SCOPES, get_access_token, get_user_access_token
except ImportError:
    from auth import CALENDAR_SCOPES, get_access_token, get_user_access_token

SHEETS_BASE_URL = "https://sheets.googleapis.com/v4/spreadsheets"
DOCS_BASE_URL = "https://docs.googleapis.com/v1/documents"
DRIVE_BASE_URL = "https://www.googleapis.com/drive/v3/files"
SERVICE_BASE_URLS = {
    "drive": "https://www.googleapis.com/drive/v3",
    "docs": "https://docs.googleapis.com/v1",
    "sheets": "https://sheets.googleapis.com/v4",
    "slides": "https://slides.googleapis.com/v1",
    "calendar": "https://www.googleapis.com/calendar/v3",
}
# Service-account scopes that differ from the base set. Slides accepts the
# base Drive scope; Calendar needs its own. User OAuth tokens carry whatever
# the user granted at consent and ignore this table.
SERVICE_ACCOUNT_SCOPES = {"calendar": CALENDAR_SCOPES}

DEFAULT_TIMEOUT = 30.0
MAX_TEXT_EXPORT_CHARS = 100_000
MAX_SHEETS_ROWS = 5_000
MAX_SHEETS_CELLS = 50_000
VALUE_RENDER_OPTIONS = ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")
DRIVE_FILE_FIELDS = (
    "id,name,mimeType,size,modifiedTime,webViewLink,parents,owners,"
    "lastModifyingUser,version,capabilities"
)

# Pattern for valid Google Drive/Docs/Sheets resource IDs (alphanumeric, dashes, underscores)
RESOURCE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,256}$")

# MIME types supported for text extraction
SUPPORTED_TEXT_MIME_PREFIXES = ("text/",)
SUPPORTED_TEXT_MIMES = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
    "application/sql",
    "application/csv",
}


def _validate_resource_id(val: str, name: str = "resource_id") -> str:
    """Validate that an ID conforms to standard Google Drive resource identifier patterns."""
    if not val or not str(val).strip():
        raise ValueError(f"{name} is required.")
    cleaned = str(val).strip()
    parsed = urlsplit(cleaned)
    if parsed.scheme in ("http", "https") and parsed.hostname in ("docs.google.com", "drive.google.com"):
        parts = parsed.path.strip("/").split("/")
        for marker in ("d", "folders"):
            if marker in parts and parts.index(marker) + 1 < len(parts):
                cleaned = parts[parts.index(marker) + 1]
                break
        else:
            cleaned = parse_qs(parsed.query).get("id", [cleaned])[0]
    if cleaned == "root":
        return cleaned
    if not RESOURCE_ID_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid {name} format: '{val}'. Expected a valid Google Drive/Docs identifier (letters, numbers, underscores, dashes)."
        )
    return cleaned


def _file_context(item: Dict[str, Any], file_id: str) -> Dict[str, Any]:
    """Preserve provider metadata without inventing absent owners or permissions."""
    context = {"url": item.get("webViewLink") or f"https://drive.google.com/open?id={file_id}"}
    for source, target in (
        ("owners", "owners"), ("lastModifyingUser", "last_modifying_user"),
        ("version", "version"), ("modifiedTime", "modified_time"),
        ("capabilities", "capabilities"),
    ):
        if source in item:
            context[target] = item[source]
    return context


class GoogleWorkspaceClient:
    """Client for Sheets, Docs, Drive, Slides and Calendar using one explicitly selected identity."""

    def __init__(
        self,
        raw_sa_info: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
        *,
        access_token: Optional[str] = None,
        subject: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
    ) -> None:
        self.raw_sa_info = raw_sa_info
        # OAuth bearer tokens are accepted only when explicitly supplied.  A
        # service-account JWT is never silently converted into delegation.
        self.access_token = str(access_token or "").strip() or None
        self.subject = str(subject or "").strip() or None
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._external_client = http_client is not None
        self.client = http_client or httpx.Client(timeout=DEFAULT_TIMEOUT)

    def close(self) -> None:
        if not self._external_client:
            self.client.close()

    def __enter__(self) -> GoogleWorkspaceClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _get_headers(self, force_refresh: bool = False, scopes: Optional[List[str]] = None) -> Dict[str, str]:
        if self.access_token or self.refresh_token or self.client_id or self.client_secret:
            token = get_user_access_token(
                self.client_id, self.client_secret, self.refresh_token, self.client,
                access_token=self.access_token, force_refresh=force_refresh,
            )
        else:
            token = get_access_token(
                raw_info=self.raw_sa_info,
                http_client=self.client,
                force_refresh=force_refresh,
                subject=self.subject,
                scopes=scopes,
            )
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        content: Optional[bytes] = None,
        scopes: Optional[List[str]] = None,
    ) -> httpx.Response:
        """Execute an HTTP request with automatic 401 token refresh retry."""
        req_headers = self._get_headers(force_refresh=False, scopes=scopes)
        if headers:
            req_headers.update(headers)

        resp = self.client.request(
            method=method,
            url=url,
            headers=req_headers,
            params=params,
            json=json_body,
            content=content,
        )

        # Retry once on 401 Unauthorized with fresh token
        if resp.status_code == 401:
            req_headers = self._get_headers(force_refresh=True, scopes=scopes)
            if headers:
                req_headers.update(headers)
            resp = self.client.request(
                method=method,
                url=url,
                headers=req_headers,
                params=params,
                json=json_body,
                content=content,
            )

        if resp.status_code >= 400:
            error_detail = resp.text[:1000]
            try:
                err_json = resp.json()
                if "error" in err_json:
                    err_obj = err_json["error"]
                    if isinstance(err_obj, dict):
                        error_detail = str(err_obj.get("message", error_detail))[:1000]
                    elif isinstance(err_obj, str):
                        error_detail = str(err_obj)[:1000]
            except Exception:
                pass
            raise RuntimeError(f"Google API error ({resp.status_code} {resp.reason_phrase}): {error_detail}")

        return resp

    def workspace_request(
        self, service: str, method: str, path: str,
        query: Optional[Dict[str, Any]] = None, json_body: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Send a provider-shaped request using the same auth and HTTP path.

        Paths are relative to a service API root. No operation allowlist or
        automatic transport retry is imposed; Google enforces granted scopes
        and resource permissions. Binary transfers use the artifact tools.
        """
        if service not in SERVICE_BASE_URLS:
            raise ValueError("service must be drive, docs, sheets, slides or calendar.")
        parsed = urlsplit(path)
        if not path.strip() or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("path must be an API-relative path; supply query parameters in query.")
        response = self._request(
            method.upper(), f"{SERVICE_BASE_URLS[service]}/{path.lstrip('/')}",
            params=query, json_body=json_body, scopes=SERVICE_ACCOUNT_SCOPES.get(service),
        )
        result: Dict[str, Any] = {"status_code": response.status_code}
        if response.content:
            try:
                result["data"] = response.json()
            except ValueError:
                result["text"] = response.text
        return result

    # --- Sheets API ---

    def sheets_info(self, spreadsheet_id: str) -> Dict[str, Any]:
        """Discover workbook and tab metadata without downloading cell data."""
        clean_sid = _validate_resource_id(spreadsheet_id, "spreadsheet_id")
        params = {"fields": (
            "spreadsheetId,spreadsheetUrl,properties(title,locale,timeZone),"
            "sheets(properties(sheetId,title,index,sheetType,gridProperties,hidden))"
        )}
        data = self._request("GET", f"{SHEETS_BASE_URL}/{clean_sid}", params=params).json()
        properties = data.get("properties", {})
        sheets = []
        for sheet in data.get("sheets", []):
            values = sheet.get("properties", {})
            sheets.append({target: values[source] for source, target in (
                ("sheetId", "sheet_id"), ("title", "title"), ("index", "index"),
                ("sheetType", "sheet_type"), ("gridProperties", "grid_properties"),
                ("hidden", "hidden"),
            ) if source in values})
        return {
            "spreadsheet_id": data.get("spreadsheetId", clean_sid),
            "title": properties.get("title"), "locale": properties.get("locale"),
            "time_zone": properties.get("timeZone"),
            "url": data.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{clean_sid}/edit",
            "sheets": sheets,
        }

    def sheets_read(
        self,
        spreadsheet_id: str,
        range_name: str,
        max_rows: int = MAX_SHEETS_ROWS,
        value_render_option: str = "FORMATTED_VALUE",
    ) -> Dict[str, Any]:
        """Read values from a rectangular range in a Google Sheet with bounded rows and cell budget."""
        clean_sid = _validate_resource_id(spreadsheet_id, "spreadsheet_id")
        if not range_name or not range_name.strip():
            raise ValueError("range_name is required (e.g. 'Sheet1!A1:D10' or 'A1:B').")
        if value_render_option not in VALUE_RENDER_OPTIONS:
            raise ValueError("value_render_option must be FORMATTED_VALUE, UNFORMATTED_VALUE, or FORMULA.")

        effective_max_rows = max(1, min(int(max_rows), MAX_SHEETS_ROWS))

        # Percent-encode range name (spaces in sheet names etc.)
        encoded_range = quote(range_name.strip(), safe="!:")
        url = f"{SHEETS_BASE_URL}/{clean_sid}/values/{encoded_range}"
        resp = self._request("GET", url, params={"valueRenderOption": value_render_option})
        data = resp.json()
        all_values = data.get("values", [])
        original_row_count = len(all_values)

        # Calculate cell count and bound
        bounded_values = []
        total_cells = 0
        truncated = False

        for row in all_values[:effective_max_rows]:
            if not isinstance(row, list):
                row = [row]
            if total_cells + len(row) > MAX_SHEETS_CELLS:
                truncated = True
                break
            bounded_values.append(row)
            total_cells += len(row)

        if original_row_count > len(bounded_values):
            truncated = True

        return {
            "spreadsheet_id": clean_sid,
            "range": data.get("range", range_name),
            "major_dimension": data.get("majorDimension", "ROWS"),
            "value_render_option": value_render_option,
            "values": bounded_values,
            "row_count": len(bounded_values),
            "original_row_count": original_row_count,
            "cell_count": total_cells,
            "truncated": truncated,
        }

    def sheets_append(
        self,
        spreadsheet_id: str,
        range_name: str,
        rows: List[List[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> Dict[str, Any]:
        """Append rows to a spreadsheet table."""
        clean_sid = _validate_resource_id(spreadsheet_id, "spreadsheet_id")
        if not range_name or not range_name.strip():
            raise ValueError("range_name is required.")
        if not isinstance(rows, list):
            raise ValueError("rows must be a list of lists representing rows to append.")

        encoded_range = quote(range_name.strip(), safe="!:")
        url = f"{SHEETS_BASE_URL}/{clean_sid}/values/{encoded_range}:append"
        params = {"valueInputOption": value_input_option}
        payload = {"values": rows}

        resp = self._request("POST", url, params=params, json_body=payload)
        data = resp.json()
        updates = data.get("updates", {})
        return {
            "spreadsheet_id": clean_sid,
            "table_range": data.get("tableRange"),
            "updated_range": updates.get("updatedRange"),
            "updated_rows": updates.get("updatedRows", 0),
            "updated_columns": updates.get("updatedColumns", 0),
            "updated_cells": updates.get("updatedCells", 0),
        }

    def sheets_update(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: List[List[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> Dict[str, Any]:
        """Update a rectangular range using the Sheets values API."""
        clean_sid = _validate_resource_id(spreadsheet_id, "spreadsheet_id")
        if not range_name or not str(range_name).strip():
            raise ValueError("range_name is required.")
        if not isinstance(values, list) or any(not isinstance(row, list) for row in values):
            raise ValueError("values must be a list of rows.")
        encoded_range = quote(str(range_name).strip(), safe="!:")
        url = f"{SHEETS_BASE_URL}/{clean_sid}/values/{encoded_range}"
        resp = self._request(
            "PUT", url,
            params={"valueInputOption": str(value_input_option or "USER_ENTERED")},
            json_body={"range": str(range_name).strip(), "majorDimension": "ROWS", "values": values},
        )
        data = resp.json()
        return {
            "spreadsheet_id": clean_sid,
            "updated_range": data.get("updatedRange"),
            "updated_rows": data.get("updatedRows", 0),
            "updated_columns": data.get("updatedColumns", 0),
            "updated_cells": data.get("updatedCells", 0),
        }

    def sheets_batch_update(self, spreadsheet_id: str, requests: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Apply structural Sheets operations (add sheet, format, resize, etc.)."""
        clean_sid = _validate_resource_id(spreadsheet_id, "spreadsheet_id")
        if not isinstance(requests, list) or any(not isinstance(item, dict) for item in requests):
            raise ValueError("requests must be a list of JSON objects.")
        url = f"{SHEETS_BASE_URL}/{clean_sid}:batchUpdate"
        resp = self._request("POST", url, json_body={"requests": requests})
        data = resp.json()
        return {"spreadsheet_id": clean_sid, "replies": data.get("replies", [])}

    # --- Docs API ---

    def docs_create(
        self,
        title: str,
        folder_id: Optional[str] = None,
        template_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new Google Document from scratch or by copying a template."""
        if not title or not title.strip():
            raise ValueError("title is required.")

        title = title.strip()
        clean_folder = _validate_resource_id(folder_id, "folder_id") if folder_id else None
        clean_template = _validate_resource_id(template_id, "template_id") if template_id else None

        if clean_template:
            # Copy template via Drive v3 API
            copy_url = f"{DRIVE_BASE_URL}/{clean_template}/copy"
            params = {"supportsAllDrives": "true"}
            body: Dict[str, Any] = {"name": title}
            if clean_folder:
                body["parents"] = [clean_folder]

            resp = self._request("POST", copy_url, params=params, json_body=body)
            data = resp.json()
            doc_id = data["id"]
        else:
            if clean_folder:
                # Drive files.create accepts parents at creation time, including
                # Shared Drive folders.  This avoids a transient root document
                # and the service-account move failure that followed it.
                create_url = f"{DRIVE_BASE_URL}"
                resp = self._request(
                    "POST", create_url,
                    params={"supportsAllDrives": "true", "fields": "id,name,mimeType,parents"},
                    json_body={
                        "name": title,
                        "mimeType": "application/vnd.google-apps.document",
                        "parents": [clean_folder],
                    },
                )
                data = resp.json()
                doc_id = data["id"]
            else:
                resp = self._request("POST", DOCS_BASE_URL, json_body={"title": title})
                data = resp.json()
                doc_id = data["documentId"]

        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        return {
            "document_id": doc_id,
            "title": title,
            "url": doc_url,
            "folder_id": clean_folder,
            "template_id": clean_template,
        }

    # --- Drive API ---

    def drive_list(
        self,
        folder_id: Optional[str] = None,
        page_size: int = 50,
        page_token: Optional[str] = None,
        query: Optional[str] = None,
        name: Optional[str] = None,
        full_text: Optional[str] = None,
        corpora: Optional[str] = None,
        drive_id: Optional[str] = None,
        spaces: Optional[str] = None,
        order_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List files and folders shared with the Service Account or inside a folder."""
        page_size = max(1, min(page_size, 100))
        clean_folder = _validate_resource_id(folder_id, "folder_id") if folder_id else None

        q_parts = ["trashed = false"]
        if clean_folder:
            q_parts.append(f"'{clean_folder}' in parents")
        if query and str(query).strip():
            q_parts.append(str(query).strip())
        if name and str(name).strip():
            escaped = str(name).strip().replace("'", r"\'")
            q_parts.append(f"name = '{escaped}'")
        if full_text and str(full_text).strip():
            escaped = str(full_text).strip().replace("'", r"\'")
            q_parts.append(f"fullText contains '{escaped}'")

        params: Dict[str, Any] = {
            "q": " and ".join(q_parts),
            "pageSize": page_size,
            "fields": f"nextPageToken,files({DRIVE_FILE_FIELDS})",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        if corpora:
            params["corpora"] = str(corpora)
        if drive_id:
            params["driveId"] = _validate_resource_id(drive_id, "drive_id")
        if spaces:
            params["spaces"] = str(spaces)
        if order_by:
            params["orderBy"] = str(order_by)

        resp = self._request("GET", DRIVE_BASE_URL, params=params)
        data = resp.json()

        files_list = []
        for item in data.get("files", []):
            files_list.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "mime_type": item.get("mimeType"),
                "size": item.get("size"),
                "modified_time": item.get("modifiedTime"),
                "parents": item.get("parents", []),
                **_file_context(item, item.get("id")),
            })

        return {
            "folder_id": clean_folder,
            "count": len(files_list),
            "files": files_list,
            "next_page_token": data.get("nextPageToken"),
            "query": params["q"],
        }

    def drive_download(
        self,
        file_id: str,
        export_mime_type: Optional[str] = None,
        max_bytes: int = 50 * 1024 * 1024,
    ) -> Dict[str, Any]:
        """Download or export binary Drive content with an explicit byte bound."""
        clean_fid = _validate_resource_id(file_id, "file_id")
        max_bytes = max(1, int(max_bytes))
        meta = self._request(
            "GET", f"{DRIVE_BASE_URL}/{clean_fid}",
            params={"fields": "id,name,mimeType,size", "supportsAllDrives": "true"},
        ).json()
        mime_type = str(meta.get("mimeType") or "application/octet-stream")
        if export_mime_type:
            url = f"{DRIVE_BASE_URL}/{clean_fid}/export"
            params = {"mimeType": str(export_mime_type)}
            output_mime = str(export_mime_type)
        else:
            url = f"{DRIVE_BASE_URL}/{clean_fid}"
            params = {"alt": "media", "supportsAllDrives": "true"}
            output_mime = mime_type
        response = self._request("GET", url, params=params)
        content = response.content
        if len(content) > max_bytes:
            raise ValueError(f"Downloaded file exceeds max_bytes ({max_bytes}).")
        return {"file_id": clean_fid, "name": meta.get("name", clean_fid), "mime_type": output_mime, "content": content, "size": len(content)}

    def drive_export(self, file_id: str, mime_type: str, max_bytes: int = 50 * 1024 * 1024) -> Dict[str, Any]:
        return self.drive_download(file_id, export_mime_type=mime_type, max_bytes=max_bytes)

    def drive_upload(
        self,
        name: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        folder_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload immutable bytes using Drive's multipart endpoint."""
        if not str(name or "").strip():
            raise ValueError("name is required.")
        if not isinstance(content, (bytes, bytearray)):
            raise ValueError("content must be bytes.")
        parent = _validate_resource_id(folder_id, "folder_id") if folder_id else None
        metadata: Dict[str, Any] = {"name": str(name).strip(), "mimeType": str(mime_type or "application/octet-stream")}
        if parent:
            metadata["parents"] = [parent]
        headers = self._get_headers()
        headers["Accept"] = "application/json"
        files = {
            "metadata": (None, json.dumps(metadata), "application/json; charset=UTF-8"),
            "media": (str(name).strip(), bytes(content), metadata["mimeType"]),
        }
        url = "https://www.googleapis.com/upload/drive/v3/files"
        params = {"uploadType": "multipart", "supportsAllDrives": "true"}
        response = self.client.post(url, params=params, headers=headers, files=files)
        if response.status_code == 401:
            response = self.client.post(url, params=params, headers=self._get_headers(force_refresh=True), files=files)
        if response.status_code >= 400:
            raise RuntimeError(f"Google API error ({response.status_code} {response.reason_phrase}): {response.text[:1000]}")
        data = response.json()
        return {"file_id": data.get("id"), "name": data.get("name", name), "mime_type": data.get("mimeType", mime_type), "size": len(content), "parents": data.get("parents", [parent] if parent else [])}

    # --- Structured Docs API ---

    def docs_read(self, document_id: str) -> Dict[str, Any]:
        clean_id = _validate_resource_id(document_id, "document_id")
        data = self._request("GET", f"{DOCS_BASE_URL}/{clean_id}").json()
        return {"document_id": clean_id, "title": data.get("title", ""), "revision_id": data.get("revisionId"), "body": data.get("body", {}), "document": data}

    def docs_batch_update(self, document_id: str, requests: List[Dict[str, Any]], readback: bool = True,
                          write_control: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        clean_id = _validate_resource_id(document_id, "document_id")
        if not isinstance(requests, list) or any(not isinstance(item, dict) for item in requests):
            raise ValueError("requests must be a list of JSON objects.")
        payload: Dict[str, Any] = {"requests": requests}
        if write_control:
            payload["writeControl"] = write_control
        response = self._request("POST", f"{DOCS_BASE_URL}/{clean_id}:batchUpdate", json_body=payload).json()
        result = {"document_id": clean_id, "replies": response.get("replies", []), "write_control": response.get("writeControl")}
        if readback:
            result["readback"] = self.docs_read(clean_id)
        return result

    def docs_update(self, document_id: str, requests: List[Dict[str, Any]], readback: bool = True,
                    write_control: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.docs_batch_update(document_id, requests, readback=readback, write_control=write_control)

    def drive_read_text(self, file_id: str, max_chars: int = MAX_TEXT_EXPORT_CHARS) -> Dict[str, Any]:
        """Read or export the plain text content of a Google Doc, Sheet, or text file with bounded stream reading."""
        clean_fid = _validate_resource_id(file_id, "file_id")
        effective_max = min(max(1, int(max_chars)), MAX_TEXT_EXPORT_CHARS)

        # 1. Fetch file metadata
        meta_url = f"{DRIVE_BASE_URL}/{clean_fid}"
        meta_params = {
            "fields": DRIVE_FILE_FIELDS,
            "supportsAllDrives": "true",
        }
        resp = self._request("GET", meta_url, params=meta_params)
        meta = resp.json()
        mime_type = meta.get("mimeType", "")
        file_name = meta.get("name", "")
        remote_file_size = int(meta["size"]) if meta.get("size") and str(meta["size"]).isdigit() else None

        # Check if MIME type is supported for text export/reading
        is_supported_text = (
            mime_type in SUPPORTED_TEXT_MIMES
            or any(mime_type.startswith(prefix) for prefix in SUPPORTED_TEXT_MIME_PREFIXES)
        )
        if not is_supported_text:
            raise ValueError(
                f"Unsupported file type for text export: '{mime_type}'. "
                "drive_read_text supports Google Docs, Google Sheets, plain text, JSON, CSV, and markdown files."
            )

        # 2. Determine export/download URL & params
        export_context = {}
        if mime_type == "application/vnd.google-apps.document":
            url = f"{DRIVE_BASE_URL}/{clean_fid}/export"
            params = {"mimeType": "text/plain"}
        elif mime_type == "application/vnd.google-apps.spreadsheet":
            url = f"{DRIVE_BASE_URL}/{clean_fid}/export"
            params = {"mimeType": "text/csv"}
            export_context = {
                "export_scope": "first_sheet_only",
                "export_note": (
                    "CSV export contains only the first sheet. Use sheets_info to discover tabs "
                    "and sheets_read for their ranges. truncated reports character clipping only."
                ),
            }
        else:
            url = f"{DRIVE_BASE_URL}/{clean_fid}"
            params = {"alt": "media", "supportsAllDrives": "true"}

        # Read streaming response bounded to effective_max chars (approx 4x bytes headroom)
        max_bytes_to_read = effective_max * 4 + 4096
        req_headers = self._get_headers(force_refresh=False)

        def _read_stream(r_headers: Dict[str, str]) -> bytes:
            with self.client.stream("GET", url, headers=r_headers, params=params) as stream_resp:
                if stream_resp.status_code == 401:
                    fresh_headers = self._get_headers(force_refresh=True)
                    with self.client.stream("GET", url, headers=fresh_headers, params=params) as retry_resp:
                        if retry_resp.status_code >= 400:
                            err_text = retry_resp.read().decode("utf-8", errors="replace")[:1000]
                            raise RuntimeError(f"Google API error ({retry_resp.status_code} {retry_resp.reason_phrase}): {err_text}")
                        chunks = []
                        total_bytes = 0
                        for chunk in retry_resp.iter_bytes(chunk_size=8192):
                            chunks.append(chunk)
                            total_bytes += len(chunk)
                            if total_bytes >= max_bytes_to_read:
                                break
                        return b"".join(chunks)
                elif stream_resp.status_code >= 400:
                    err_text = stream_resp.read().decode("utf-8", errors="replace")[:1000]
                    raise RuntimeError(f"Google API error ({stream_resp.status_code} {stream_resp.reason_phrase}): {err_text}")
                else:
                    chunks = []
                    total_bytes = 0
                    for chunk in stream_resp.iter_bytes(chunk_size=8192):
                        chunks.append(chunk)
                        total_bytes += len(chunk)
                        if total_bytes >= max_bytes_to_read:
                            break
                    return b"".join(chunks)

        raw_bytes = _read_stream(req_headers)
        text_content = raw_bytes.decode("utf-8", errors="replace")
        truncated = False
        if len(text_content) > effective_max:
            text_content = text_content[:effective_max] + f"\n\n[... Truncated at {effective_max} characters ...]"
            truncated = True

        return {
            "file_id": clean_fid,
            "name": file_name,
            "mime_type": mime_type,
            "text": text_content,
            "length": len(text_content),
            "remote_size_bytes": remote_file_size,
            "truncated": truncated,
            **_file_context(meta, clean_fid),
            **export_context,
        }
