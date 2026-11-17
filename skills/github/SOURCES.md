# Checked contracts and public sources

Public API documentation opened on **2026-10-23**. These are source checks,
not live credential/provider qualification. Query code uses the documented
REST `2026-03-10` version and GraphQL fields. No GitHub mutation was performed.

| Source | Applied contract |
| --- | --- |
| [API versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions) | Explicit version header; 2026-03-10 is a supported version in the checked documentation |
| [Issues](https://docs.github.com/en/rest/issues/issues) | All-state repository listing, PRs represented as issues, narrow state/state_reason PATCH |
| [Issue comments](https://docs.github.com/en/rest/issues/comments) | Repository and thread pages, comment creation and ID readback |
| [Review comments](https://docs.github.com/en/rest/pulls/comments) | Inline feed and reply-to-top-level-comment endpoint |
| [PR reviews](https://docs.github.com/en/rest/pulls/reviews) | Paginated review submissions and verdict/body observations |
| [Review requests](https://docs.github.com/en/rest/pulls/review-requests) | Requested users and teams, separate from submitted reviews |
| [Notifications](https://docs.github.com/en/rest/activity/notifications) | Classic PAT only; sticky reason semantics; 50-item pages, since, conditional polling and X-Poll-Interval |
| [REST best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api) | Canonical primary/secondary refusal classification, Retry-After/reset, serial provider requests, conditional GETs and mutation spacing |
| [Discussions guide](https://docs.github.com/en/graphql/guides/using-the-graphql-api-for-discussions) | Repository discussions, parent comments, replies, addDiscussionComment and replyToId |
| [Discussions schema](https://docs.github.com/en/graphql/reference/discussions) | Numeric IDs, timestamps, locked/closed, replyTo, nested connections and mutation inputs |
| [Repositories schema](https://docs.github.com/en/graphql/reference/repos) | Repository lookup, rename behavior and stable database identity |
| [Users schema](https://docs.github.com/en/graphql/reference/users) | Actor/login projection; numeric attribution is resolved through authenticated REST users |
| [Repository REST](https://docs.github.com/en/rest/repos/repos) | Numeric identity, visibility, archive and permission metadata |

The older `/graphql/reference/objects` URL redirects to the category index.
The `/graphql/reference/repositories` guess was unavailable; the actual primary
repository schema link is `/graphql/reference/repos`. No implementation relies
on those unavailable URLs.

The accepted decision/delta specifications supersede conflicting historical v2
claims. Host reference inspection covered manifest/PluginAPI contracts, place
ingress, receipt and queue validators, registration and declarative rendering.
No serving source was modified or imported in its original location.

`tests/check_host_contract.py` prepares private HOME/data/repo/state before host
imports and copies only imported Python modules into temporary dependency-only
storage. The real parser, contributions validator, PluginAPI staging methods,
receipt and queue validators run unchanged. Staging is not installation or an
executable review/grant. Source hashes and counts are in the raw host-check log;
the temporary source copies are discarded. Native access was not OS-sandboxed.

The real Ouroboros contract parser is authoritative for this extension, rather
than a Codex-specific frontmatter validator.

## Repair source checks (2026-10-23)

- [App installation authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation):
  installation requests use expiring installation tokens and App attribution;
  this requires a distinct auth/identity adapter. The installation adapter now
  verifies App/installation/repository/bot identity and renews scoped tokens.
  Notifications still need classic PATs; fixture checks do not qualify live App use.
- [GHES REST setup](https://docs.github.com/en/enterprise-server@3.19/rest/using-the-rest-api/getting-started-with-the-rest-api)
  and [GHES GraphQL calls](https://docs.github.com/en/enterprise-server@3.19/graphql/guides/forming-calls-with-graphql):
  explicit server paths/version/TLS are configuration, with separate REST and
  GraphQL endpoints. Local route tests do not qualify a deployed GHES version.
- Issues/comments/inline-comment documentation above was reopened for `since`,
  `sort=updated` and complete `Link` traversal. Discussions/Repositories schema
  categories were reopened for nested connections and descending updated order.

The repair's actual host reference is a read-only host candidate,
copied before import to a disposable prepared directory.
The exact per-module SHA-256 inventory, commands, exit statuses, failed controls,
raw output and JUnit are in the separately returned `.review-evidence/`;
verification reports are not installed skill state. The host's unchanged
PluginAPI, delivery validator, queue validator, queue persistence/readback and
staged status handler were exercised, as was the actual place resolver with a
temporary synthetic Silent discovery place. Existing shared Main/late-answer/followup
model flows and the UI were not qualified by these checks.

After the parent's stable-ready announcement, the narrow consumer check also
passed against `9b1a974c04e14821bc27acefd959b789c6e5eecc` plus its repaired host
delta. It copied only 193 imported modules (4,908,557 bytes) before import; every
recorded module hash still matched the source afterward. `host-aligned.txt` and
`aligned-host-source.json` retain that evidence. Earlier pre-alignment checks
remain preserved. This qualifies those named local consumers, not a real
server-to-Main session, model followup or browser rendering.

`read_only: provider` was checked against CLIENT_CONTRACT.md and
`extension_ui_validation.py` (provider can withhold writes); it is provider
support, not an assertion that a writable PAT is intrinsically read-only. The
manifest declaration is retained; the second notifications client is GET-only.

## Attention repair sources (2026-11-17)

- [Public GraphQL schema](https://docs.github.com/public/fpt/schema.docs.graphql)
  (SHA-256 `d928ebbb2d08cba348c0c5713ab3c8756a39e838324089ff66fcddf1bab2c496`): `databaseId` lives on `User`, `Bot`,
  `Mannequin` and `Organization`, not on `Actor`; `UserContentEdit` has `editedAt`,
  `editor` and `deletedAt`; `Comment.userContentEdits(first:)` and `lastEditedAt`;
  `timelineItems(since:, last:, before:, itemTypes:)` with `PageInfo.startCursor`, whose
  Issue item types lack review and merge events; every timeline event has `id: ID!`
  (its identity; `Actor` has none); `Discussion` and `DiscussionComment` have
  `lastEditedAt`; `ReviewRequestedEvent.requestedReviewer` includes `Team`,
  `AssignedEvent.assignee` does not. Every document the connector builds was captured
  from its own call path and validated against that schema (graphql-core 3.2.6, 13
  documents on 2026-11-17, again after the edit window became `first: 50`); six
  mutated negative controls were refused.
- [REST OpenAPI description](https://github.com/github/rest-api-description)
  (`api.github.com.json`, SHA-256 `f7a3de68869181d34b232d88840d31f8157ade45cad1746eaab90cd3c801b31c`): an issue row for a pull
  request carries `draft` and `pull_request.merged_at`, so the Issues feed alone
  observes a pull request's state; reviews have `submitted_at` and no `updated_at`.
- Three public reads on 2026-11-17 (no mutation; one object each, not a
  qualification of every installation): `userContentEdits` lists edits newest
  first (`first: 2` returned the two newest with `hasNextPage`, `last: 2` the two
  oldest with `hasPreviousPage`), so the connector asks `first: 50` and treats
  `hasNextPage` as older edits outside its window; a REST issue row of a merged
  pull request has `state`, `closed_at` and nested `pull_request.merged_at` and no
  top-level `closed`, `merged` or `merged_at` (`closed` is GraphQL `Discussion`'s);
  one Bot's GraphQL `databaseId` equalled its REST `[bot]` user id. App-mode
  delivery through that id is still not live-qualified. A fourth read the same
  day: `timelineItems` lists events oldest first (`last: 2` returned the two
  newest with `hasPreviousPage`), so `last: 100` is the newest window there.
- The same public schema on a Discussion's words: `lastEditedAt` ("The moment the
  editor made the last edit"), `editor` ("The actor who edited the comment") and
  `userContentEdits` ("A list of edits to this content") are its `Comment` fields
  and describe its text; `title` has no edit field and a Discussion no timeline
  (`RenamedTitleSubject` is `Issue | PullRequest`);
  `updatedAt` is "the date and time when the object was last updated". Nothing
  there says a title change sets `lastEditedAt`, so the connector takes a
  Discussion's whole words for its author's only while `updatedAt` equals
  `createdAt`. Not read live: whether GitHub ever stamps a new Discussion's
  `updatedAt` later than its `createdAt`; if it does, that Discussion arrives with
  an unknown origin (attention unchanged), never as wrongly proven.

## Authentication and lock sources (2026-10-23)

- [App JWT claims](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app):
  RS256, backdated `iat`, expiry at most ten minutes ahead, client ID or App ID
  issuer, and Bearer authorization. Signing uses PyJWT/cryptography.
- [App REST endpoints](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app):
  JWT-authenticated App/installation lookup and token exchange, up to 500 explicit
  repository IDs, permission restriction, returned expiry and hour-long tokens.
  Tokens are treated as opaque strings, including the newer token format.
- [Installation repositories](https://docs.github.com/en/rest/apps/installations#list-repositories-accessible-to-the-app-installation):
  verify every page of the token's repository selection before using it.
- [GitHub's App-token action](https://github.com/actions/create-github-app-token/blob/main/README.md):
  resolve the App bot's user ID through `/users/{app-slug}[bot]`; it is distinct
  from the App and installation IDs. Numeric author/readback checks remain in use.
- [PyJWT cryptographic dependency](https://pyjwt.readthedocs.io/en/stable/installation.html):
  the `crypto` extra supplies cryptography for RSA. Imports are confined to App
  setup so a missing optional signer does not disable the PAT adapter.
- [Python CRT locking](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking):
  byte ranges start at the descriptor position, may extend past EOF, and
  `LK_NBLCK` fails immediately on contention. The companion owns byte zero
  until unlock/close; POSIX keeps its existing `flock` primitive.

Local evidence uses generated RSA keys, literal-loopback HTTP servers and
temporary process state. It includes invalid-key/signature controls, scoped
token renewal/concurrency/expiry and failure tests, origin confinement, PAT
regression, and native macOS competing-process/release checks. Windows has a
CRT-call contract test; native Windows/Linux and live GitHub App/GHES operations
remain NOT_RUN. The separately retained QA bundle contains raw outputs and
integration prerequisites; it is not part of installed skill state.
