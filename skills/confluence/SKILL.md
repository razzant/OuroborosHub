---
name: confluence
description: Read, search and author Confluence Cloud pages, comments and attachments with an existing API token, and remove one selected Jira web link through Atlassian's official TWG CLI with checks before and after.
version: 0.2.0
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
permissions: [net, read_settings, tool]
env_from_settings: [CONFLUENCE_SITE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN, CONFLUENCE_AUTH_MODE, CONFLUENCE_CLOUD_ID]
dependencies: ["httpx>=0.27.0"]
timeout_sec: 180
when_to_use: Read or maintain documents in Confluence Cloud, find knowledge with CQL, inspect discussion threads, or transfer attachments. Also when one entry in a Jira issue's web links (remote issue links), such as a merged merge request, must be removed without touching the description, other links or the merge request.
model_experience:
  what_model_sees: Twelve ordinary tools for connection checks, spaces, pages, CQL, comments and attachments. Full page bodies use Confluence storage XHTML. Jira web links use the bundled jira_weblinks.py helper with run_command; it adds no tool schema.
  token_effect: Fixed tool schemas while enabled; requested page bodies and result pages add their actual content, without silent local truncation. Jira helper output is a compact JSON summary.
---

# Atlassian: Confluence & Jira

A normal extension over Atlassian's APIs. It has no background process,
separate memory, automatic content selection, login flow or custom dashboard.
The existing account's permissions and token scopes determine access.

## Configure

Add the following settings in the host's Secrets/settings surface and grant
them to the reviewed skill before enabling it:

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

The extension's own tools are Confluence-only. To remove one web link, use
Atlassian's official [TWG CLI](https://developer.atlassian.com/cloud/twg-cli/commands/commands-catalog/)
with the person's own login. The skill never installs it, signs in, reads its
stored credentials or uses Confluence settings for Jira. If `twg` is missing or
signed out, say so and stop. For a first-time setup, point the person to the
[official installation](https://developer.atlassian.com/cloud/twg-cli/getting-started/installation/)
(the macOS package and the Windows MSI install only the binary), then `twg login`
(usually needed once) and `twg doctor` ([authentication and login profiles](https://developer.atlassian.com/cloud/twg-cli/getting-started/how-authentication-works/)).

`jira_weblinks.py` (Python standard library only) plans and checks; it never
starts a process, opens a connection or follows a link. Run it and every `twg`
command it prints as separate ordinary `run_command` calls: the helper with
`cwd=skill_payload`, `bucket=ouroboroshub`, `skill_name=confluence`, for example
`["python3", "jira_weblinks.py", "list", "--workdir", "<dir>"]` (use the
interpreter name available on the host). Every printed `twg` command pins the
chosen `--profile` and `--site` and runs in agent mode with explicit output flags
(`--mode agent --output-summary none --output-file ... --output json`), so TWG
writes its full JSON result into `--workdir`: a new, absolute, task-owned
directory such as a folder in the task drive, one per removal, never the skill
folder. The helper creates it readable only by its owner.

1. `plan --workdir DIR --site SITE --profile NAME --issue DEMO-42 [--url URL]`
   binds the site prefix or cloud ID, the saved TWG login profile (`default` for
   the saved default) and the issue; add `--url` when the exact link URL is
   already known. Run the printed read once.
2. `list --workdir DIR` shows each web link's ID, URL, title, global ID and
   application, and whether its URL matches when one is bound. Choose the one the
   person meant; with several matches, confirm which.
3. `select --workdir DIR --link-id ID [--url URL]` requires that ID to exist
   once and carry the exact expected URL (`--url` is needed only if the plan has
   none and must equal it otherwise). It prints the delete (`twg jira workitem
   unlink weblink --id <issue ID> --link-id <ID>`, never a URL selector) and the
   follow-up read. Run the delete exactly once, then the read regardless of the
   delete's result.
4. `verify --workdir DIR` reports the delete outcome (`acknowledged`,
   `provider_error` with TWG's error code and status, or `no_acknowledgement`)
   separately from the follow-up read. `removed` (exit 0) needs the
   acknowledgement, the link gone, the same issue and site, and every other
   remote link returned as the identical provider record. Otherwise (exit 3):
   `refused` (TWG reported an error and the link is still there), `not_removed`,
   `acknowledged_but_present`, `absent_without_acknowledgement` (no longer seen,
   but this run cannot prove its own request removed it), `other_links_changed`,
   `identity_changed` or `unverified`.

A read that is a TWG error, reports warnings or partial data, lacks or garbles
`remoteLinks`, repeats an ID, names another issue or site, or is truncated is
refused, not treated as an empty list: TWG omits `remoteLinks` when it cannot
read them, and in agent mode its warning is not in the file. Observations come
through TWG, which itself lists an unexpected non-list provider response as
empty. A work directory never re-issues a delete: after any outcome other than
`removed`, report it and read again with a new plan before the person decides on
another attempt. The delete needs the login's Jira delete permission.

## API references

- [Personal Basic authentication](https://developer.atlassian.com/cloud/confluence/basic-auth-for-rest-apis/)
- [Scoped API tokens](https://support.atlassian.com/confluence/kb/scoped-api-tokens-in-confluence-cloud/)
- [Pages, versions and storage bodies](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/)
- [CQL search](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-search/)
- [Comments](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-comment/)
- [Attachment upload and download](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-content---attachments/)
- [Jira remote issue links](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-remote-links/)
