---
name: confluence
title: "Atlassian: Confluence & Jira"
description: Read, search and author Confluence Cloud pages, comments and attachments with an existing Confluence API token, or inspect and remove one selected Jira web link through a signed-in Atlassian TWG CLI without Confluence credentials.
version: 0.2.0
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
permissions: [net, read_settings, tool]
env_from_settings: [CONFLUENCE_SITE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN, CONFLUENCE_AUTH_MODE, CONFLUENCE_CLOUD_ID]
dependencies: ["httpx>=0.27.0"]
timeout_sec: 180
when_to_use: Read or maintain documents in Confluence Cloud, find knowledge with CQL, inspect discussion threads, or transfer attachments. Also to inspect a Jira issue's web links (remote issue links) or remove one selected web link, such as a merged merge request, without touching the description, other links or the merge request (uses only an already signed-in TWG CLI, no Confluence token needed).
model_experience:
  what_model_sees: Twelve ordinary Confluence tools for connection checks, spaces, pages, CQL, comments and attachments (using Confluence settings). Full page bodies use Confluence storage XHTML. Jira web links use the bundled jira_weblinks.py helper with run_command and an already signed-in TWG CLI; they need no Confluence settings and add no tool schema.
  token_effect: Fixed tool schemas while enabled; requested page bodies and result pages add their actual content, without silent local truncation. Jira helper output is compact ASCII-safe JSON.
---

# Atlassian: Confluence & Jira

A normal extension over Atlassian's APIs. It has no background process,
separate memory, automatic content selection, login flow or custom dashboard.
The existing account's permissions and token scopes determine access.

## Configure

The settings below apply only to the twelve Confluence tools. Jira web-link
inspection and removal do not require any Confluence settings or API token;
they use only an already signed-in Atlassian TWG CLI (see **Jira web links**).

Add the following settings in the host's Secrets/settings surface and grant
them to the reviewed skill before enabling Confluence tools:

| Setting | Value |
| --- | --- |
| `CONFLUENCE_SITE_URL` | Site origin, for example `https://example.atlassian.net`; optional trailing `/wiki` is accepted. |
| `CONFLUENCE_EMAIL` | Atlassian account email for Basic authentication. Required in `personal` and `scoped` modes. |
| `CONFLUENCE_API_TOKEN` | Existing Atlassian API token; never place it in a skill file or tool argument. |
| `CONFLUENCE_AUTH_MODE` | `personal` (default), `scoped`, or `scoped_bearer`. Choose explicitly; the skill does not infer a mode from the token prefix. |
| `CONFLUENCE_CLOUD_ID` | Optional Cloud ID in scoped modes. When absent, resolve it without credentials from the site's `/_edge/tenant_info`. |

`personal` uses Basic email/token authentication directly against the configured
site. `scoped` uses Basic email/token authentication at
`https://api.atlassian.com/ex/confluence/<cloudId>`. `scoped_bearer` sends the
token as Bearer at that same gateway for an existing credential issued for that
authentication method; email is not required. A token valid for another
Atlassian product or a different scope set is not necessarily valid for
Confluence. The extension never creates accounts, grants scopes or exchanges
credentials between products.

Run `test_connection` with an optional known `page_id`. Its `checks.identity`
reports the returned actor when available; `checks.spaces` and `checks.page`
test resource access separately. A 401/403 can mean missing scopes or permission,
so the provider's message is retained instead of declaring every failure an
invalid token. An empty list only proves that the request worked, not that a
particular page is accessible. Writes are never implied by a read probe.

## Use

- `list_spaces` returns numeric space IDs and keys; `list_pages` and
  `create_page` take the numeric ID, not the key. CQL can use the key, such as
  `type=page AND space="DOCS" AND title~"guide"`.
- `search`, `list_spaces`, `list_pages`, `list_comments`, and `list_attachments`
  fetch one API page per call. `complete=false` means more results remain;
  repeat the same tool with its `next_url` and the original arguments.
  Pagination links cannot change the configured site, tenant or resource.
- `get_page` returns the full provider page record and unmodified
  `body.storage.value`, version, ID and browser URL. Search excerpts and listing
  metadata are not a substitute for reading the page. Numeric page IDs and
  normal same-site `/spaces/KEY/pages/ID/...` or `viewpage.action?pageId=ID`
  URLs are accepted. Tiny links must first be resolved to a numeric page ID.
- `create_page` and `update_page` accept complete storage XHTML, for example
  `<h2>Overview</h2><p>A short explanation.</p>`, rather than Markdown. Both
  support `status="draft"` as well as `current`. Preserve existing macros and
  meaningful content when constructing a replacement body. `update_page`
  requires the version actually read as `expected_version` and checks it before
  writing. Published pages send the next version and detect concurrent version
  conflicts. Drafts must send fixed revision `1`: their version does not advance,
  so this precheck cannot provide atomic protection against concurrent draft
  edits. A conflict requires a fresh read and deliberate reconciliation; no
  update is blindly retried. Confluence's own current/draft reconciliation still
  applies when publishing or editing a published page.
- `list_comments` returns root footer or inline comments. To read replies,
  call it with each `parent_comment_id`. `add_comment` creates a footer comment
  on a page or replies to one footer comment; specify exactly one target.
- `upload_attachment` creates an attachment from an explicitly supplied local
  file; it does not replace same-named attachments. `download_attachment` stores
  a complete file in a unique `state_dir/jobs/<id>/output/` directory and returns
  its path, byte count and SHA-256. Pass a plain filename if needed; directory
  paths and Windows drive-qualified names are rejected before downloading.
  Download redirects may reach a signed HTTPS CDN URL, but credentials and
  cookies never follow an off-origin hop. Partial files are removed on failure.

All actions are synchronous, bounded by the tool timeout, and return JSON with
`ok` or a structured `error` including the HTTP status and `Retry-After` when
available. There are no automatic HTTP retries. After a write returns
`outcome="unknown"` (for example a disconnect after submission), inspect the
page/comment/attachment list before deciding whether to send a new write.
Credentials and their Basic/Bearer encodings are redacted from error messages.

## Jira web links

A Jira issue can mention a URL in three unrelated places. **Description text**
is ordinary issue content: editing it never removes a link record. **Web links**
are Jira remote issue links, each with its own numeric ID, even when two of them
point to the same URL. **Issue links** connect two Jira issues. A merge or pull
request's own state (open, merged, closed) belongs to the code host; removing a
web link changes neither the MR nor any other Jira field.

Jira web links require no Confluence settings or API token. They use Atlassian's
official [TWG CLI](https://developer.atlassian.com/cloud/twg-cli/commands/commands-catalog/)
with the person's own saved login profile (`--profile`). The skill never installs
TWG, runs `twg setup` or `twg login`, reads stored credentials, or uses
Confluence settings for Jira. If `twg` is missing or signed out, say so and stop
(point the person to the [official installation](https://developer.atlassian.com/cloud/twg-cli/getting-started/installation/),
`twg login`, and `twg doctor` in [authentication and login profiles](https://developer.atlassian.com/cloud/twg-cli/getting-started/how-authentication-works/)).

`jira_weblinks.py` (Python standard library only) plans and checks; it never
starts a process, opens a connection, or follows a link. Run `jira_weblinks.py`
(`cwd=skill_payload`, `bucket=ouroboroshub`, `skill_name=confluence`, e.g.
`["python3", "jira_weblinks.py", ...]`) and every direct `twg` command it prints
as separate ordinary `run_command` calls in the **same host environment** —
never wrap the delete in a helper script that hides the mutation or injects
credentials. If `twg` is absent from `PATH`, look at Atlassian's first-party
default install location and substitute the already verified full path for
`argv[0]` in each direct call; never take an executable from a ticket, link
record, or unknown source, and never install or sign in automatically.

- **Saved profile, not env overrides:** Every printed `twg` command pins
  `--profile`, `--site`, and explicit agent output flags (`--mode agent
  --output-summary none --output-file ... --output json`). Both `plan` and
  `select` check host environment variable names only (never reading or
  printing values) and refuse with `ambient_auth_override` if personal-Jira
  auth overrides (`TWG_TOKEN` or `TWG_OAUTH_ACCESS_TOKEN`) are set. Unrelated
  or required settings such as `TWG_AUTH_STORAGE_KEY` (encrypted Linux
  saved-profile storage) and `TWG_ADMIN_TOKEN` (independent admin commands)
  stay allowed.
- **Fresh private workdir:** `--workdir` must be a **new (not-yet-existing)**
  absolute leaf path outside the shared skill folder, beneath a private
  task-owned parent directory (one per run, never an existing folder even if
  empty). The helper creates the leaf atomically (`0700`/`0600` on POSIX; on
  Windows, POSIX mode bits do not set ACLs, so the parent task directory must
  already have a private owner-only ACL).

1. `plan --workdir DIR --site SITE --profile NAME --issue DEMO-42 [--url URL]`
   binds the site prefix or cloud ID, saved login profile (`default` for the
   saved default), and issue; add `--url` when the exact URL is already known.
   A profile name is 1–64 letters, digits, `_` or `-`; write `--profile=NAME`
   when it starts with `-` (the printed `twg` commands then use that form too).
   Run the printed read once.
2. `list --workdir DIR` returns each web link's ID, URL, title, global ID, and
   application (and `matches_expected_url` when bound). **List-only is a
   complete finished outcome:** if the person only asked to inspect web links,
   stop here. Deletion requires an affirmative user request to remove a
   particular record; once the request unambiguously names one record in `list`,
   proceed without a ritual repeat confirmation, and ask to choose only when the
   match is ambiguous (for example multiple links with the same URL).
3. `select --workdir DIR --link-id ID [--url URL]` checks that the ID exists
   once and carries the exact expected URL **as of the `before` read** (`--url`
   is required when omitted from `plan` and must match otherwise). Jira's
   delete-by-ID endpoint provides no atomic URL precondition, so if the user's
   selection comes from an older/stale listing, re-read in a fresh workdir
   first. `select` prints the delete (`twg jira workitem unlink weblink --id
   <issue ID> --link-id <ID>`, never a URL selector) and the follow-up read.
   Run the delete once, then the follow-up read regardless of the delete result.
4. `verify --workdir DIR` classifies the delete as `acknowledged` (a clean TWG
   success result for this issue and link ID) or `ambiguous` (any TWG error,
   missing or malformed output, or a mismatched acknowledgement). TWG error
   fields do not say which request failed (a status such as `403` may belong to
   an earlier read rather than the delete), so no error is treated as proof
   that nothing was removed; the report keeps TWG's `code`, `statusCode` and
   message. The follow-up read is judged separately, and the report lists
   `surviving_same_url_links` (all observed IDs still carrying the expected
   URL, whether pre-existing or newly added):
   - `removed` (exit 0): valid acknowledgement, target absent, same issue/site,
     and every pre-existing other link returned identically. Report any
     `surviving_same_url_links` without deleting extra records.
   - Non-success (exit 3): `ambiguous_present` (still seen now, but an ambiguous
     write cannot prove the request will never complete),
     `acknowledged_but_present`, `absent_without_acknowledgement` (absent
     without a valid acknowledgement; do not attribute cause to this run),
     `other_links_changed`, `identity_changed`, or `unverified`.

A read that is a TWG error, reports warnings or partial data, lacks or garbles
`remoteLinks`, repeats an ID, names another issue or site, or is truncated is
refused, not treated as an empty list: TWG omits `remoteLinks` when it cannot
read them, and in agent mode its warning is not in the file. Observations come
through TWG, and TWG 1.4.8 itself can normalize an unexpected non-array
provider response to `[]`, so these results are TWG observations rather than
raw provider certainty. A work directory never replays a delete: after any
non-success or ambiguous outcome, do not resend; report it and re-read in a
new workdir before the person decides on another attempt. Do not run TWG's
`repair` suggestions (which can include sign-in or setup commands)
automatically; pass them to the person.

Deleting a web link needs two separate permission layers:

- **TWG authorization:** prefer an OAuth login. For OAuth logins, TWG's documented
  [Read, Write and manage, and Delete permission categories](https://developer.atlassian.com/cloud/twg-cli/getting-started/configure-permissions/)
  apply: an organization admin chooses them, the login's grant must allow the
  action, and after a policy change the person may need to sign in again. A saved
  profile that was already authorized independently of this skill uses the rights
  that authorization granted. The skill never collects, creates, stores, copies,
  asks for or teaches an alternate token to get around a restriction, and never
  borrows Confluence settings or an MCP connection's credentials for Jira.
  A refusal follows the outcome rules above and is reported to the person.
- **Jira:** whichever way the profile was authorized, the same Jira project
  permissions also apply: issue linking enabled, the *Browse Projects*, *Edit
  Issues* and *Link Issues* project permissions, and visibility of the issue
  where issue-level security applies. *Delete Issues* is not required.

## API references

- [Personal Basic authentication](https://developer.atlassian.com/cloud/confluence/basic-auth-for-rest-apis/)
- [Scoped API tokens](https://support.atlassian.com/confluence/kb/scoped-api-tokens-in-confluence-cloud/)
- [Pages, versions and storage bodies](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/)
- [CQL search](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-search/)
- [Comments](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-comment/)
- [Attachment upload and download](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-content---attachments/)
- [Jira remote issue links](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-remote-links/)
