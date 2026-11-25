---
name: google-workspace
version: 0.4.0
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
# The service an Ouroboros with sign_in_version 1 signs in to for Google Workspace
# (older cores keep this key unused). The scopes are the skill's own:
# user_oauth.SCOPES keeps the two equal (tests/test_host_sign_in.py checks it).
sign_in:
  - id: google
    label: Google
    issuer: https://accounts.google.com
    scopes: [openid, email]
    scope_labels: {openid: "identity", email: "email address"}
    scope_spellings: {email: ["https://www.googleapis.com/auth/userinfo.email"]}
    optional_scopes:
      - {scope: "https://www.googleapis.com/auth/drive", label: "Full Google Drive (read/change files and folders; also covers Docs, Sheets and Slides)", default: true}
      - {scope: "https://www.googleapis.com/auth/documents", label: "Google Docs only (read/change; covered by full Drive; folders/templates need Drive)", default: true}
      - {scope: "https://www.googleapis.com/auth/spreadsheets", label: "Google Sheets only (read/change; covered by full Drive)", default: true}
      - {scope: "https://www.googleapis.com/auth/presentations", label: "Google Slides only (read/change; covered by full Drive)", default: false}
      - {scope: "https://www.googleapis.com/auth/calendar.events", label: "Google Calendar events (read and change)", default: false}
    authorize_params: {access_type: offline, prompt: consent}
    accounts: many
    optional: true
    notes:
      client: >-
        Create an OAuth client of type Desktop app in your own Google Cloud project and enable the
        Google APIs you use there. While the project's publishing status is Testing, Google ends every
        sign-in after 7 days; publishing it keeps them (personal use needs no verification; Google shows
        an "unverified app" screen). A client approved by your organization can be used instead; the
        owner chooses the client per account on the skill's card.
      refused: >-
        admin_policy_enforced: your Workspace administrator approved only some permissions or another
        client; org_internal: this client belongs to another organization.
      refresh_rejected: >-
        If your Google Cloud app is in Testing, Google ends sign-ins after 7 days.
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
Two explicit routes, never mixed: `auth_mode="service_account"` (the default) and
`auth_mode="oauth"`.

**Service account** — grant `GOOGLE_SERVICE_ACCOUNT_JSON` and share the resources with
that service account. An explicit `subject` is supported by the authentication probe for
domain-wide delegation. A service account asks for the `calendar` scope only on Calendar
calls; the other APIs keep the original three scopes, so existing delegation entries keep
working. It sees only calendars shared with it; its own `calendars/primary` is empty, so
use `auth_mode="oauth"` for a user's calendar.

**User OAuth** — on an Ouroboros that holds sign-ins (its `GET /identity` carries
`sign_in_version: 1`), the owner signs in for this skill on its card in Extensions ›
Skills, choosing the Google client (personal or IT-approved) and the account. The skill
then asks the host for a short-lived access token; the host does not return refresh tokens.
The card asks for `openid` and `email` always, full Drive, Docs and Sheets by default, and
Slides and Calendar events only when chosen. Full `drive` also covers Docs, Sheets and
Slides calls; individual Docs/Sheets/Slides scopes narrow access only when `drive` is
unchecked, and `docs_create` with a folder or template still requires `drive`. An
untouched manual Settings sign-in keeps serving without scanning host accounts while
eligible and no card account is selected. A temporary host outage leaves a confirmed Settings
connection in use without starting a transfer. It moves only on a host advertising
`sign_in_handover_version: 2`, when an ordinary renewal
reports eligible permissions including one that can verify the account, and the host confirms
the account. API-only permissions stay on Settings; there is no extra renewal to discover scope.
`workspace_accounts` shows the active route; the README explains pending and settled transfers.

On an Ouroboros without that service the skill keeps its own path: configure and grant
`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` and `GOOGLE_OAUTH_REFRESH_TOKEN`
in Settings → Secrets, then pass `auth_mode="oauth"`. `GOOGLE_OAUTH_ACCESS_TOKEN`
remains optional for short-lived access; once a command step succeeds with it, later steps
stop instead of switching mid-command to `GOOGLE_OAUTH_REFRESH_TOKEN`. User tokens carry only the scopes granted at
consent: Docs, Sheets and Slides work with full `drive` as well as their own scopes, and Calendar events use
`calendar.events` for both reading and changing them. The broader `calendar` scope is
not requested on the user route.

`workspace_accounts` lists the accounts Ouroboros holds for this skill, their exact `a-...`
keys and which connection is in use (an email is not shown for every account). Paste the
exact key in `DEFAULT_GOOGLE_ACCOUNT` and save in the skill's settings to select a card
account (leave empty for eligible Settings on an older core). Without an explicit choice, adding another account does not replace an unmoved
Settings connection; a confirmed transfer can become the implicit choice once it still
matches Settings and no newer account choice applies. Without
an existing connection (confirmed via `get_settings_state` on hosts that provide it),
choose among several host accounts or leave a sole usable one implicit. To switch away
from an active Settings connection or after an unconfirmed/disconnected transfer, sign
in on the card and select and save that account in the skill's settings. The old
Settings copy is not used after an uncertain or confirmed transfer. After a recorded refresh
rejection, a demonstrably new short-lived token can serve a new command without using the
rejected refresh grant.

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
