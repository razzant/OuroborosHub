---
name: google-workspace
version: 0.3.0
type: extension
entry: plugin.py
runtime: python3
plugin_api: "2.0"
description: "Universal Google Workspace integration for Google Sheets, Docs, Drive, Slides and Calendar via explicit OAuth or Service Account authentication."
permissions:
  - tool
  - read_settings
  - widget
  - route
  - net
env_from_settings:
  - GOOGLE_SERVICE_ACCOUNT_JSON
  - GOOGLE_OAUTH_ACCESS_TOKEN
  - GOOGLE_OAUTH_CLIENT_ID
  - GOOGLE_OAUTH_CLIENT_SECRET
  - GOOGLE_OAUTH_REFRESH_TOKEN
requires:
  - "httpx>=0.24.0"
  - "cryptography>=41.0.0"
install:
  - "httpx>=0.24.0"
  - "cryptography>=41.0.0"
dependencies:
  - "httpx>=0.24.0"
  - "cryptography>=41.0.0"
timeout_sec: 60
---

# Google Workspace Skill

Universal, standalone extension skill providing Google Sheets, Docs, Drive, Slides and Calendar integration using renewable user OAuth or Service Account credentials.

## Features
- **Google Sheets**: Discover workbook metadata and tabs, read/update/append ranges, and apply structural batch updates.
- **Google Docs**: Structured read, batch update with readback, and create new blank/template documents.
- **Google Drive**: Search/list with pagination and Shared Drive options; upload/download/export bounded binary artifacts.
- **General API**: `workspace_request` accepts provider-shaped requests across Drive v3, Docs v1, Sheets v4, Slides v1 and Calendar v3, sharing the existing client and credentials.
- **Slides and Calendar** through `workspace_request`: hide or show a slide in presentation mode with an `updateSlideProperties` request (`fields: "isSkipped"`); read events and their meeting links; attach a Drive file to an event with `PATCH` and `supportsAttachments=true`, sending the existing attachments too because PATCH replaces the list. Edit an event only when it shows `organizer.self` or `guestsCanModify` true (or its calendar is writable to you), then read it back. The README has exact request shapes.
- **Settings & UI Tab**: Configure default working folders and template mappings.

Google Sheets CSV export in `drive_read_text` contains only the first tab;
`export_scope` reports that coverage independently from character `truncated`.
Use `sheets_info` to discover tabs and `sheets_read` for their explicit ranges.
Metadata omitted by Google (such as owners for shared-drive files) remains absent.

## Authentication
For persistent user access, configure and grant `GOOGLE_OAUTH_CLIENT_ID`,
`GOOGLE_OAUTH_CLIENT_SECRET` and `GOOGLE_OAUTH_REFRESH_TOKEN` in Settings → Secrets,
then pass `auth_mode="oauth"`. Obtain the refresh token through an owner-approved
Google OAuth consent flow with offline access; this skill does not start login.
User tokens carry only the scopes granted at consent: Slides works with `drive`
or `presentations`, Calendar needs a Calendar scope (`calendar.events` or
`calendar` to change events).
`GOOGLE_OAUTH_ACCESS_TOKEN` remains optional for short-lived access and is reused
until Google rejects it or a cached refreshed token expires. Renewable tokens are
cached in memory; each isolated tool process can refresh from the granted secrets.

Alternatively grant `GOOGLE_SERVICE_ACCOUNT_JSON` and share resources with that
service account. The default remains `auth_mode="service_account"`; the skill
never silently changes identity or falls back between routes. An explicit
`subject` is supported by the authentication probe for domain-wide delegation.
A service account asks for the `calendar` scope only on Calendar calls; the
other APIs keep the original three scopes, so existing delegation entries keep
working. It sees only calendars shared with it; its own `calendars/primary` is
empty, so use `auth_mode="oauth"` for a user's calendar.

`workspace_auth_status` performs a Drive user read and returns `configured`,
`verified` and the actual provider `actor`. Success verifies that request only,
not access to a particular file or write permission. Read the intended resource
with the same `auth_mode` to verify its access. Resource ID parameters accept
common Google Docs, Sheets and Drive links as well as bare IDs.

## Large results and files

Small JSON responses remain byte-for-byte inline. Larger responses are saved in
full before the extension IPC limit and return `source_ref`, SHA-256, byte size,
`result_format` and an existing `read_file` call. Use that call and its range
continuation, or read the file from the task artifact directory with existing
script tools. `result_complete` means the complete response to this request;
pagination, range limits, first-sheet exports and provider omissions still apply.
No summary replaces the saved bytes. Google export-size errors remain provider
errors; another explicitly selected export format may work.

Downloads and exports keep immutable content-addressed files in skill state and
also return canonical task-owned paths when invoked by an agent. Text results
include a `read_file` call; binary exports expose their MIME type and a path for
existing document/image tools or scripts, without a UTF-8 reader hint. Files live
in the canonical task artifact store and survive child task cleanup. Repeating a
request cannot overwrite an earlier result with changed content or another
export format. Standalone calls without task context return a stored absolute
path with `actor_readable=false`; that is not a claim of access by task tools.
