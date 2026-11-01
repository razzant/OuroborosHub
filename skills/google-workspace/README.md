# Google Workspace Skill (`google-workspace`)

A universal, standalone, public extension skill for Ouroboros providing Google Sheets, Google Docs, Google Drive, Google Slides and Google Calendar integration via renewable user OAuth or Google Cloud Service Accounts.

---

## Features

- **Google Sheets (`sheets_info`, `sheets_read`, `sheets_update`, `sheets_append`, `sheets_batch_update`)**: Discover tabs, read/update/append ranges, and apply structural requests.
- **Google Docs (`docs_create`, `docs_read`, `docs_update`)**: Create blank/template documents, read structured body data, and batch-update with optional readback.
- **Google Drive (`drive_list`, `drive_read_text`, `drive_download`, `drive_export`, `drive_upload`)**: Search and paginate across folders or Shared Drives, export text, and exchange bounded binary artifacts through skill state.
- **General API (`workspace_request`)**: Send standard JSON requests across Drive v3, Docs v1, Sheets v4, Slides v1 and Calendar v3 using the same selected identity.
- **Preflight Check (`workspace_auth_status`)**: Verify the actual Google Drive actor without modifying Workspace data or returning credentials.
- **Settings & UI Tab**: Configure default working folders and template name mappings.

---

## Architecture & Dependency Rationale

This skill uses **pure REST via `httpx` and `cryptography`** rather than the official `google-api-python-client` / `google-auth` SDKs.

### Why Pure REST?
1. **Minimal Dependencies**: The official Google client libraries pull in 15+ transitive packages (`google-auth`, `google-api-python-client`, `google-auth-httplib2`, `uritemplate`, `cachetools`, `rsa`, `pyasn1`, `pyasn1-modules`, `httplib2`). Pure REST requires only `httpx` and `cryptography`.
2. **Deterministic & Auditable**: All HTTP endpoints, OAuth2 token exchanges, request parameters, and response parsers are explicit in readable Python code without dynamic discovery document overhead.
3. **Instant Startup**: Zero startup lag from dynamic API discovery on initialization.

---

## Setup & Configuration

Enable Google Drive, Docs and Sheets APIs in the Google Cloud project used by
your chosen credential route, plus Slides and Calendar when you call them. A
disabled API returns a Google error that names the project and links to enabling
the API. For
user OAuth that is the OAuth client's project; for a service account, its own
project. Configure secrets under **Settings → Secrets** and
grant only the keys for that route to this skill. Secrets belong to the host's
granted settings, never the skill's folder or template settings form.

### Persistent user OAuth

1. Use an owner-approved Google OAuth client and consent flow with offline access
   to obtain a refresh token for the user and required scopes. The skill does not
   launch consent, sign in, switch accounts or request new scopes. Grant the
   scopes for the APIs you will call: `drive`, `documents` and `spreadsheets`
   for the file tools, Slides calls work with `drive` or `presentations`, and
   Calendar calls need a Calendar scope: `calendar.events` or `calendar` to change
   events; read-only Calendar scopes are enough for reading.
2. Configure and grant `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and
   `GOOGLE_OAUTH_REFRESH_TOKEN`. These three optional manifest keys form the
   renewable route together. Updating a skill payload requires current review and
   grants for the new content; an old bearer-only grant does not grant these keys.
3. Pass `auth_mode="oauth"` to tools. The skill refreshes at Google's fixed token
   endpoint, caches access tokens in memory by the complete credential fingerprint,
   and honors the returned expiry. Isolated tool processes can independently
   refresh using the same persistent host secrets.
4. Run `workspace_auth_status(auth_mode="oauth")`, check the returned actor, then
   read the intended document with the same mode. OAuth acts as that user within
   both granted scopes and resource permissions. The Drive user probe does not
   certify every file, every API, or write access.

`GOOGLE_OAUTH_ACCESS_TOKEN` alone remains supported for short-lived use; an
expired token cannot renew without all three refresh credentials. An explicitly
supplied access token is reused initially. A definite API 401 triggers at most
one refresh and request retry; timeouts and other HTTP errors never trigger an
automatic mutation retry. A revoked/expired refresh grant returns an error and
requires owner action. Google app publishing status, organization policies and
revocation affect refresh-token validity; a refresh token is not a permanent
entitlement. Neither credentials nor refreshed tokens are written to skill state.

### Service account

Create a service account JSON key, configure and grant
`GOOGLE_SERVICE_ACCOUNT_JSON`, and share the intended Drive files/folders with its
`client_email`. `auth_mode="service_account"` remains the default for compatibility.
OAuth is selected explicitly and never falls back to the service account. The
status probe's optional `subject` checks explicit domain-wide delegation only;
operational tools do not infer delegation from an email address.

A service account requests the `spreadsheets`, `documents` and `drive` scopes for
Drive, Docs, Sheets and Slides calls, and only the `calendar` scope for Calendar
calls, so a delegation entry authorized for the original scopes keeps working.
Operational tools never use delegation, so a service account sees only calendars
shared with its `client_email`, its own `calendars/primary` is an empty calendar
of its own, and it cannot invite attendees. Use `auth_mode="oauth"` to work in a
user's calendar.

---

## Tool Reference

### `workspace_auth_status(auth_mode="service_account", subject="")`
- **Description**: Calls Drive `about?fields=user` through the explicitly selected route. No Workspace write occurs; renewable routes may exchange credentials for an access token.
- **Returns**: `status`, `auth_mode`, `configured`, `verified`, `actor`, `resource_access="not_checked"`, and a message. A configured but invalid token reports `verified=false`, never `ready`. Successful probes include `verification_scope="drive.about.user"`; service-account configuration metadata stays separate from the actual provider actor.

### `workspace_request(service, method, path, query=None, json_body=None, auth_mode="service_account")`
- **Services**: `drive` starts at `https://www.googleapis.com/drive/v3`, `docs` at `https://docs.googleapis.com/v1`, `sheets` at `https://sheets.googleapis.com/v4`, `slides` at `https://slides.googleapis.com/v1`, and `calendar` at `https://www.googleapis.com/calendar/v3`.
- **Parameters**: Standard HTTP method, API-relative path, query object, and optional JSON body. Provider operations and permissions are decided by the caller and Google; this tool adds no operation allowlist. Percent-encode calendar IDs that contain `#` or `/` (for example `calendars/team%23x%40group.calendar.google.com/events`); `primary` needs no encoding.
- **Examples**: `service="drive", method="GET", path="files/ID", query={"fields":"id,name,capabilities"}`; `service="drive", method="POST", path="files/ID/permissions", json_body={"type":"user","role":"reader","emailAddress":"reader@example.com"}`; `service="docs", method="POST", path="documents/ID:batchUpdate", json_body={"requests":[...]}`.
- **Slides**: read slide IDs and their skip state with `service="slides", method="GET", path="presentations/ID", query={"fields":"slides(objectId,slideProperties/isSkipped)"}`. Hide a slide in presentation mode with `method="POST", path="presentations/ID:batchUpdate", json_body={"requests":[{"updateSlideProperties":{"objectId":"SLIDE_ID","slideProperties":{"isSkipped":true},"fields":"isSkipped"}}]}`; send `false` to show it again.
- **Calendar**: with `auth_mode="oauth"`, `calendars/primary` is the signed-in user's calendar; with a service account use the ID of a calendar shared with it, because its own `primary` is empty. List upcoming events with `service="calendar", method="GET", path="calendars/primary/events", query={"q":"Friday","timeMin":"2026-10-26T00:00:00Z","singleEvents":"true","orderBy":"startTime"}`. A meeting link is in `conferenceData.entryPoints`, `hangoutLink`, `location` or `description`, depending on how it was added. Attach a Drive file with `method="PATCH", path="calendars/primary/events/EVENT_ID", query={"supportsAttachments":"true"}, json_body={"attachments":[...existing attachments..., {"fileUrl":"<the file's webViewLink>","title":"Deck","mimeType":"application/vnd.google-apps.presentation"}]}`; the file tools return that link as `url`. PATCH replaces the whole `attachments` list, so read the event first and send the existing entries too. With `singleEvents=true` each occurrence of a recurring event has its own ID, so changing one occurrence leaves the series alone. Change an event only when the GET shows `organizer.self` or `guestsCanModify` true, or when you edit a calendar you can write to: a guest without that right may change only their own copy, which other guests never see. Read the event back after the PATCH to confirm. A file needs its own sharing for guests to open it.
- **Returns**: `status_code` plus the provider `data` for JSON, `text` for a text response, or just the status for an empty response. Use the existing download/export/upload tools for binary artifacts. Query fields, pagination and write preconditions follow the provider API.

All existing tool names remain available. Their resource ID parameters accept
bare IDs or ordinary Google Docs, Sheets, Drive file/folder and `open?id=...`
links. A general request path remains API-relative; it is not a document URL.
All operational tools accept `auth_mode` with the same explicit route semantics.

Tool results use compact JSON without removing fields. Pretty-printing deeply
nested Docs structures can exceed the host's isolated-child response bound even
for a short document; compact serialization preserves the complete parsed result
while avoiding indentation and extra whitespace on that wire.

### `sheets_info(spreadsheet_id)`
- **Description**: Read spreadsheet title, locale, timezone, URL, and tab IDs, titles, order, type, visibility and grid properties. Does not fetch cell values or formatting.
- **Returns**: `spreadsheet_id`, `title`, `locale`, `time_zone`, `url`, and `sheets`. Each tab preserves the properties Google returned; non-grid tabs may omit `grid_properties`.
- **Use**: Choose a returned tab title for `sheets_read`. Grid dimensions are allocated rows/columns, not the count of populated records; frozen rows are provider facts, not an inferred header schema.

### `sheets_read(spreadsheet_id, range, max_rows=5000, value_render_option="FORMATTED_VALUE")`
- **Parameters**:
  - `spreadsheet_id` (string, required): Google Spreadsheet ID from URL.
  - `range` (string, required): A1 notation (e.g. `'Sheet1!A1:D50'`, `'Summary!A:C'`).
  - `max_rows` (integer, optional): Maximum rows to return (default: 5000).
  - `value_render_option` (string, optional): `FORMATTED_VALUE` (default, displayed calculated values), `UNFORMATTED_VALUE` (calculated values without display formatting), or `FORMULA` (formulas instead of calculated results).
- **Returns**: JSON object with `range`, `major_dimension`, `value_render_option`, `values`, `row_count`, `original_row_count`, `truncated`.

### `sheets_append(spreadsheet_id, range, rows, value_input_option="USER_ENTERED")`
- **Parameters**:
  - `spreadsheet_id` (string, required): Google Spreadsheet ID.
  - `range` (string, required): A1 notation or table name.
  - `rows` (list of lists, required): Rows of cell data to append.
  - `value_input_option` (string, optional): `'USER_ENTERED'` or `'RAW'`.
- **Returns**: JSON object with `updated_rows`, `updated_columns`, `updated_cells`.

### `sheets_update` and `sheets_batch_update`
- **Description**: Update a rectangular A1 range or apply structural Sheets `batchUpdate` requests.
- **Returns**: Updated range counts or provider replies.

### `docs_create(title, folder_id=None, template_id=None)`
- **Parameters**:
  - `title` (string, required): Document title.
  - `folder_id` (string, optional): Target Google Drive folder ID (or uses configured default).
  - `template_id` (string, optional): Template Document ID or template alias name to copy from.
- **Returns**: JSON object with `document_id`, `title`, `url`, `folder_id`.
- **Placement**: A blank document with a target folder is created directly in that folder through Drive. If the folder is missing or inaccessible, creation fails without creating a root document as a fallback. Template copying and creation without a target folder remain supported.

### `docs_read` and `docs_update`
- **Description**: Read structured Docs body data and apply standard Docs `batchUpdate` requests. Updates read the document back by default and accept an optional `write_control`.

### `drive_list(folder_id=None, page_size=50, page_token=None, query=None, name=None, full_text=None, corpora=None, drive_id=None, order_by=None)`
- **Parameters**:
  - `folder_id` (string, optional): Folder ID to list from (or uses configured default).
  - `page_size` (integer, optional): Number of files to return (1-100, default: 50).
  - `page_token` (string, optional): Continuation token.
- **Returns**: JSON object with `files` list containing `id`, `name`, `mime_type`, `url`, `modified_time`.
  Available provider fields also include `owners`, `last_modifying_user`, `version` and `capabilities`. Follow `next_page_token` until it is absent to finish a listing.

### `drive_download`, `drive_export`, and `drive_upload`
- **Description**: Download/export bounded bytes into immutable skill state, or upload a local path/base64 payload with an explicit MIME type. The download tools return a staged `path` and metadata; upload returns the Drive file ID.

### `drive_read_text(file_id, max_chars=100000)`
- **Parameters**:
  - `file_id` (string, required): Google Drive file ID.
  - `max_chars` (integer, optional): Max text character limit (default: 100000).
- **Returns**: JSON object with `file_id`, `name`, `mime_type`, `text`, `length`, `truncated`, `url`, and the same available provider metadata as `drive_list`.
- **Sheets coverage**: CSV contains only the first sheet. `export_scope="first_sheet_only"` and `export_note` disclose this even when `truncated=false`; that flag describes character clipping only. Use `sheets_info` and explicit `sheets_read` ranges for other tabs.

Ownership and capability fields are returned only when Google supplies them.
Shared-drive files have no `owners` field; an absent owner or last modifier is
not an anonymous person or a permission denial. `version` is Google's file
change counter, not a Docs revision ID or an edit precondition. Capability
booleans describe the calling account's access, not a promise that a later write
will succeed.

### Structured updates and binary artifacts

`docs_read(document_id)` returns the structured document body and revision ID.
`docs_update(document_id, requests, readback=true)` sends standard Docs
`batchUpdate` requests and includes a fresh readback by default.
`sheets_update` writes a rectangular A1 range, while `sheets_batch_update`
accepts structural Sheets requests such as adding tabs or changing formatting.

`drive_list` accepts `query`, `name`, `full_text`, `corpora`, `drive_id`, `spaces`, and
`order_by` in addition to folder and pagination parameters. `drive_download` and
`drive_export` store bounded response bytes in the skill state directory and
return their staged path. `drive_upload` accepts a local file path or base64
payload and uploads it with an explicit MIME type.

---

## Known Constraints & Disclosures

1. **Sheets API Range Bounding**:
   - Google Sheets API v4 `spreadsheets.values.get` returns all rows within the requested A1 range in a single response payload (the upstream REST endpoint does not accept a `maxRows` query parameter).
   - In `sheets_read`, client-side row truncation (`max_rows`) is applied immediately upon receiving the response. For very large sheets, callers should supply explicit bounded ranges (e.g. `'Sheet1!A1:Z500'`) rather than unbounded column ranges (`'A:Z'`) to minimize upstream transfer size.
2. **Verification**:
   - Tests use disposable RSA keys and mocked Google responses. They do not certify permissions or API enablement in a particular Google project; run `workspace_auth_status` after setup.
   - Native signing is imported during execution, after extension registration. Loading native cryptography during registration and retaining its classes after isolated-dependency cleanup can cause `Expected instance of hashes.HashAlgorithm` on the first tool call, even when the host and isolated package versions match.
   - Run `PYTHONPATH=skills/google-workspace python -m pytest -q skills/google-workspace/tests` from the Hub checkout. Set `OUROBOROS_SOURCE_DIR` to an Ouroboros source checkout to include repeated authentication and document reads through the real isolated child loader. This integration test uses temporary state, local test grants, mocked HTTP, and signature verification; it does not change a live installation.

---

## Security Boundaries

- **Provider Access Control**: OAuth uses the selected user's granted scopes and resource permissions. Service accounts use resources available to their own identity or an explicitly delegated subject.
- **Credential Protection**: Granted Google credentials and access tokens are never returned by authentication status or persisted in skill state. Refresh failures report the OAuth error code without echoing the response body.
- **Endpoint Protection**: OAuth2 token exchanges are strictly pinned to verified Google OAuth domains.
- **Zero Core Mutation**: Confined to `data/skills/external/google-workspace/` and its state directory.

## API references

- [Drive file metadata](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
- [Sheets metadata without grid data](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/get)
- [Values rendering options](https://developers.google.com/workspace/sheets/api/reference/rest/v4/ValueRenderOption)
- [Export formats and first-sheet CSV scope](https://developers.google.com/workspace/drive/api/guides/ref-export-formats)

- [Slides batchUpdate and UpdateSlidePropertiesRequest](https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/request#UpdateSlidePropertiesRequest)
- [Calendar events.patch and attachments](https://developers.google.com/workspace/calendar/api/v3/reference/events/patch)

- [User OAuth offline access and refresh](https://developers.google.com/identity/protocols/oauth2/web-server#offline)
- [Drive authenticated user](https://developers.google.com/workspace/drive/api/reference/rest/v3/about/get)
