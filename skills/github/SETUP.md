# Setup and operation

Install and configure this connector only under the owner's authorization.
Installation alone creates no Places and enables no collection or public writes.
The host must satisfy the capability contract below. Offline source-bound tests
cover the shared consumers; a live Silent trial remains required before cutover.

## Supported configuration

Authentication adapters cover a user PAT and a GitHub App installation with
Python 3.10+. GitHub.com defaults to REST `2026-03-10` plus GraphQL. App signing
uses the declared `PyJWT[crypto]>=2.13,<3` dependency (PyJWT and cryptography).
Those imports are lazy: missing App dependencies produce an App-specific error
and do not prevent PAT use. Route handlers use host Starlette.

The companion holds an OS lock throughout recovery, network work and shutdown:
`flock` on macOS/Linux, a nonblocking CRT byte lock on Windows. The lock file
stays in place; its existence alone does not mean a companion is running.
Native process checks ran on macOS. Windows has implementation and CRT-call
contract tests only; native Windows and Linux runs are **NOT_RUN** here.
Synthetic RSA/loopback tests do not establish live GitHub App or GHES support.

Both adapters can use explicit GHES `api_base_url`, same-origin `graphql_url`,
`web_base_url`, `api_version`, and an optional `ca_bundle` path. Typical GHES
paths are `/api/v3` and `/api/graphql`; use the selected server's documented
version. TLS verification cannot be disabled. Endpoint routing and server
identity separation have local tests; no GHES release is live-qualified.

`private_repository_ids` is an explicit subset of selected repository IDs,
empty by default. A broad credential alone never selects a private repository.
Public-only is the default; private selection has synthetic checks,
not an actual private connection. A built-in reader may not share these
credentials or reach a GHES/private source: disclose that source gap instead
of forwarding the connector secret. Changing server/writer while custody exists
is refused as `state_namespace_changed_preserve_old_custody`; retain and
reconcile the old state before an explicit separate setup.

Use a dedicated writer identity. The PAT author is verified through `/user`;
a configured username cannot change who publishes. Events written by the writer
are heard, not woken on; if the writer is your own account, confirm it on its
place (This is my account), and your posts there wake it again. Store only these custom keys
in the host's Secrets UI and grant this payload access when installation is
authorized:

| Secret | Use |
| --- | --- |
| `GITHUB_PRESENCE_WRITER_TOKEN` | Selected repository reads and authorized comments/closure as its verified user |
| `GITHUB_PRESENCE_NOTIFICATIONS_TOKEN` | Optional **different** user's Notifications; a separate GET-only client also verifies `/user` |
| `GITHUB_PRESENCE_APP_PRIVATE_KEY` | App mode only: the App's RSA private key in PEM format, with actual line breaks; never stored in configuration |

There is no `GITHUB_TOKEN`, `gh`, environment-token or personal-owner fallback.
In PAT mode the writer may also have a Notifications reader (`writer_notifications: true`).
That reader uses a GET-only client; it does not grant the second reader mutation
access. The host's skill token is sent only to the literal-loopback Host Service.
Provider credentials are confined to their exact HTTPS origin; redirects never
forward them to another origin. TLS certificate verification stays enabled.

Select the appropriate token permissions for the destination: Issues, Pull
requests and Discussions read/write for the corresponding operations; metadata
read for repository checks. A classic PAT with `public_repo` can cover public
repository participation where fine-grained PAT limitations apply. A classic
PAT for Notifications needs `notifications` (or `repo`); fine-grained tokens are
not supported by that API. The Notifications scope itself also permits changes;
GET-only is a client guarantee. No administration, workflow, merge or contents
write permission is needed by this connector. These facts come from the current
[GitHub sources](SOURCES.md), not a live scope qualification of any token.

## App installation configuration

Set `auth_mode: "app"`, positive numeric `app_id` and `installation_id`, and
`expected_writer_id` to the App **bot user's numeric ID**. These are three
different identities. Optional `app_client_id` selects GitHub's recommended
JWT issuer; otherwise the documented numeric App ID is used. Keep
`writer_notifications: false`. An installation has no user Notifications feed;
`notifications_user_id` plus the separate classic PAT can still provide its
user's feed. PAT mode continues to support both user feeds.

Select 1–500 repositories explicitly. Each token request includes exactly their
numeric IDs. The connector verifies `/app`, `/app/installations/{id}`, every
`/installation/repositories` page and `/users/{verified-app-slug}[bot]`.
The returned bot record must match `expected_writer_id` and have type `Bot`.
It never uses `/user` for an installation or treats the installation's owner
as the writer. An arbitrary configured username cannot change attribution.

The requested token permissions are the installation's existing Issues, Pull
requests and Discussions read/write grants plus Metadata read. Additional App
permissions are excluded, and a read grant is never promoted to write. Missing
operation permissions remain provider refusals. App issue closure requires the
verified token's Issues write grant for the selected repository; no user `push`
permission is synthesized. This token design does not support Git/Contents
access. Actual write capability still needs separate destination-specific
qualification.

RS256 JWTs have a backdated issuance time and a lifetime below GitHub's ten
minute bound. Installation tokens stay only in process memory. Renewal occurs
at least 60 seconds before the returned expiry, with a monotonic deadline and
one refresh at a time per client. Failed refreshes expose a diagnostic and
pause authentication attempts for at least 60 seconds; they never use an
expired token or fall back to a PAT. The next independent operation after a
401 obtains a new token. No REST/GraphQL operation is replayed merely because
a token was renewed, including a failed or ambiguous mutation. Existing outbox
uncertainty and readback rules continue to apply.

App account IDs include the exact API origin, App ID and installation ID;
the bot user ID remains the author evidence in receipts and self-echo checks.
PAT account IDs retain their existing encoding. Do not reuse state across a
PAT/App or installation change while old custody remains.

For example, add these nonsecret fields to the configuration below, replacing
the synthetic IDs with the selected installation and its verified bot:

```json
{
  "auth_mode": "app",
  "app_id": 77,
  "app_client_id": "Iv.example-client",
  "installation_id": 88,
  "expected_writer_id": 901,
  "writer_notifications": false
}
```

## Initial Silent connection

The host's existing three version numbers are not sufficient for this scope.
Its authenticated `/identity` must also publish `presence_wake_facts` including
all nine facts listed in [HOST_CONTRACT.md](HOST_CONTRACT.md), and
`presence_main_contact_version: 1` and `presence_work_output_version: 1`.
Missing, malformed or incomplete fields
produce a visible `host_capability_gap` and hold companion startup. These exact advertisements are the agreed host seam. Registration and
schema checks cannot substitute for the actual Main/answer/followup consumers. `host_contract_ready` must be true in companion status
before any Active trial. The payload never changes a place to Active.

1. After the parent/owner's separate review and installation decision, save
   nonsecret configuration in the skill's Settings section. Keep `enabled: false`.
   Use numeric user/repository IDs obtained from GitHub, not arbitrary logins as
   identities. The example below is synthetic; replace every identity/repository.
2. Use **Verify identities and repositories** while collection remains disabled.
   This probe checks writer, watched identities, public repository IDs,
   and enabled Notifications access. It creates no Places and performs no test
   write. App mode creates a short-lived authentication token through GitHub's
   token endpoint. The status route lists connection IDs, verified bot/App/
   installation identity, token expiry and identity verification time;
   this provider-only probe does not prove host readiness, write scopes or actual
   provider writes.
3. Connect each displayed account/repository through the existing Places UI or
   `configure_presence`. Rows appear once `expected_writer_id` names the writer;
   before that the status route offers none. Make sure each new place is **Silent
   before enabling collection**: a host that starts new places Silent needs no
   change, and an older host's Connect creates Active. For public Notifications
   outside the selected repositories, also connect **Account-wide notifications**
   (a **blank** conversation ID: the host resolves that blank scope to `*`; do not
   enter a literal `*` as the stored conversation ID) and keep it **Silent**. On a
   host advertising `presence_change_context`, choose `mention`, `assigned` and
   `review_requested`: each proven message or action arrives once as its own event;
   a notification update itself is background evidence. An older host receives
   `any_message` alone, which wakes on every notification.
   The connector never creates or edits
   a place. An absent binding retains a visible admission gap; a notification
   never selects a repository for external writes. No place mode is stored here.
4. Choose wakes, notes and model in Places. The connector suggests `new_issue`,
   `new_pull_request`, `mention`, `my_thread`, `review_requested` and `assigned`;
   these facts are never disguised as mentions. "New discussion"
   (`signal.new_discussion`) is an opt-in choice. `any_message` is an explicit choice
   that adds every other conversation and change, except that a host advertising
   `presence_change_context` keeps a change whose actor GitHub's records do not
   establish in the background. A merge, close, reopen or edit carries only a newly
   added mention, assignee or reviewer (older hosts also `any_message`); without such a fact it
   waits for the next own wake. Bots still require `from_bot` explicitly. A blank model inherits the
   consciousness model slot; verify that inheritance if the desired model is Main.
5. Enable collection in the configuration. Inspect baseline progress, per-stream
   completions/gaps, waiting work and receipts. Use a short Silent trial and a
   collection comparison before considering an Active place. The connector does
   not change Safety, autonomy, budgets, models, schedules or existing place notes.

```json
{
  "schema_version": 1,
  "enabled": false,
  "auth_mode": "pat",
  "api_base_url": "https://api.github.com",
  "expected_writer_id": 101,
  "notifications_user_id": 202,
  "writer_notifications": true,
  "watched_identities": [
    {"id": 101, "login": "example-writer"},
    {"id": 202, "login": "example-observer"}
  ],
  "repositories": [{"id": 303, "full_name": "example/project"}],
  "poll_seconds": 120,
  "overlap_seconds": 120,
  "reconcile_seconds": 21600,
  "quota_reserve": 100,
  "private_repository_ids": []
}
```

Settings are strict: a damaged file is an error, not an empty configuration.
Only the host-provided `state_dir` contains `settings.json`, `transport.sqlite3`
and the companion lock. The payload directory remains immutable. Configuration
changes are noticed by the companion; outstanding reads finish with sending
stopped before reconstruction. When Settings remove a Notifications reader, its
notification observation still waiting for a read goes out at its next retry without
a read, as if its retries had run out: `notification_recipient_removed` lists any
reads still open, each also an explicit gap. Secret rotation requires a
controlled companion restart because its existing environment is not refreshed. No restart is performed
by this payload. If the second reader's credential is missing or fails identity
verification, repository collection and its waiting notifications go on, its own feed
and discovery wait (`notification_reader_unavailable` when missing), and a visible
gap requires credential repair/restart.

## Collection guarantees and limits

Every supported content observation is retained with original provider data,
provenance, exact text, reply route, facts and gaps. The host decides whether to
wake. Repository names are labels; repository/user IDs determine identity.
Same-origin repository renames resolve through numeric identity checks; an issue
or Discussion transferred into another repository fails the pre-send target check.

Each Issues/PR, general comment, inline comment and Discussion stream has its own
persisted work queue. A pull request is one object observed through the Issues feed;
its work adds submitted reviews and requested users/teams. REST `Link` and independent GraphQL `pageInfo` traversal have no
content cap. Every Discussion parent is revisited, including old ones. Page
observations and the next checkpoint commit together. Equal timestamps are not
used as a stopping rule. Actual content A→B→A creates three observation revisions;
unchanged retries or a timestamp-only bump do not invent an event.
An old object updated during its initial scan is a live observation with unknown
editor and unavailable prior revision; it is not labelled as a newly opened
issue or proof of a newly added assignment/mention. Previously observed objects
retain their normal revision history. For a first setup that replaces an old watch, set `initial_since` explicitly to
its retained last covered timestamp (or the earliest across independent streams).
It sets the first-observation boundary before collection; it is never changed
automatically after initialization. Rows created or updated in that uncovered
interval remain live observations, with honest unknown prior/editor facts.
This does not import old promises, replies or unknown effects: retain their exact
source-linked handoff in ordinary host knowledge/tasks before retiring the watch.

Normal REST scans use `since` with an overlapping watermark, committed only
when the stream's pages are complete. Fast Discussion headers are newest first
with overlap. Independent full reconciliation (default six hours) still visits
old PRs, Discussions, every comment and every nested reply connection, regardless
of parent update timestamps. Child pages have separate custody; their failures
do not hold the next header scan. Streams rotate one page at a time, with no
content cap. Equal timestamps are retained. Completion dates and pending/gapped
jobs must be checked; six hours is a cadence, not a completion SLA.

Old baseline observations do not each fetch and store the entire thread. Live
changes still collect complete context for factual attribution, and sends read
current context again. Conditional GETs retain both content and pagination links
across 304s and restarts. Requests share serial network access; collection leaves
`quota_reserve` REST requests/GraphQL points for effects and readback. The two
Notifications readers obey `X-Poll-Interval`; their API uses pages of at most 50.
Repeated rate refusals back off exponentially up to an hour. A rate hold never
expires the original intent. These are local controls, not production load or
latency qualification. A very large single changed thread still costs a full
read; snapshot polling cannot promise an exhaustive historical event log.
Unused conditional-response cache expires after a day; a later read refetches
it. This expires only a rebuildable optimization, never source or delivery custody.

Notifications preserve the exact recipient, reason and source. Selected
repositories get prompt feed rescans. For outside-selected **public** subjects,
a separate durable job verifies repository identity/visibility and current
Issue/PR/Discussion context, then submits a real notification observation through
the ordinary place intake. A sticky reason is never a fresh mention, review request
or assignment. On a capable host, inside the window since that reader's previous
observation, new words naming a watched account, words an edit added (attributed to
that edit) and its own timeline events prove one, each one event keyed by GitHub's
node id whichever reader saw it; older hosts get `any_message`. Both readers'
notifications stay as separate evidence. A proof reads at most five timeline pages
and one edit record per mentioning comment whose own fields show a change. The discovery event is read-only even if a place is mistakenly
Active; neither final output nor the explicit tools can send from it. Main
reports/questions use the shared host tools under the owner-created Silent
place. Missing places, unsupported subject types and private/unproven visibility
remain explicit gaps. No private subject outside selection is fetched.
Notification retention on GitHub and scan completion bound recoverable coverage.

`mention` is a syntactic approximation: exact login boundaries excluding ordinary
fenced/inline code and HTML comments, with parser version and source offsets.
Watched identities can include verified App logins such as `example-app[bot]`.
Parser version 2 matches the complete bot login, including its suffix; a bot
mention does not also match a human login with the same prefix. Setup verifies
the numeric ID, login and Bot type before accepting a watched bot.
It is not GitHub's structural mention parser. Escapes, malformed Markdown,
reference links and indented code can differ. `my_thread` uses actually read
history with timestamps/numeric users, including browser posts; explicit parent
links prove `reply_to_me`. Missing history remains a gap, not proof of absence.
Review teams are retained but do not prove a watched member was addressed.
Edits retain the original author separately. On a host with `presence_change_context`
one GraphQL read per changed object attributes the change to the account GitHub's
edit and timeline records name for every observed change since the previous
snapshot; several accounts, a missing record or an unknown number stay unknown.
Words their first author edits are that account's own only while the object's
complete edit record (its newest 50 edits) names no other editor and no earlier
observed change of that text was another account's or unestablished; otherwise the
text is marked as not theirs alone. An issue or pull request retained before its
title was ever judged (older than the first scan, or kept by an earlier release)
has every rename since it was opened read once, with its author's first revision.
Older hosts receive every revision with an unknown actor (0.1.6). A read that
decides attention or whose words they are and fails transiently is retried before
the event is first offered (`enriching` in status; at most six attempts or 30 minutes, three for a
record GitHub has not yet returned); later new words are not held. An outside comment
that says it changed while its edit record shows nothing is never its author's words
alone. In a selected repository a REST comment's later update time with a record that
says "never edited" is read again, then GitHub's record stands; an object that itself
reports an edit its record never shows stays unproven.
`User` accounts with agent-like names do not become `Bot` facts.

Snapshot polling cannot recover content created and deleted between polls, every
intermediate edit or unobserved assignment changes; a change GitHub keeps no
record for has no attributed actor (a closed Discussion; a reviewer who leaves the
requested list by submitting their review, reported as the list and not reread).
Words first observed after they were written are their author's own when nobody
else can have changed them (no later update time; a GraphQL comment never edited; an
inline comment last touched when its review was published) or when GitHub's edit
record, one read per such object, names that author alone. Otherwise they arrive
as new words under their author with an unknown origin. A Discussion is its
author's own only while its update time is its creation time: nothing records who
set its title, and "never edited" speaks for its text alone, so any later one
arrives with that unknown origin (`title_record_unavailable`), as does its
author's first revision of one retained before this was judged; the mark stays
with that Discussion. Not knowable:
edits older than a record's newest 50. A Bot, or
an author GitHub gives no number, is not read: no person's words are at stake.
Deletion is not inferred from 404; the diagnostic is `no_longer_visible`.

## Delivery and recovery

Automatic output, explicit replies and explained closure enter one outbox.
`source_event_id` is looked up locally; callers cannot supply an arbitrary reply
route. Requests are immutable under their ID. Long bodies are split into ordered
12,000-character parts, each with a stable opaque marker; no text is dropped.

Each physical mutation checks the current authenticated place/grant, configured
writer identity, explicitly selected repository and fresh exact thread. Freshness
changes hold an intent with its full new context and `current_fingerprint`.
An author can deliberately reaffirm the same immutable request by passing that
fingerprint as `reconsider_fingerprint`; it must inspect the evidence first.
Held intent observations (original source/delivery IDs, immutable intent,
current context and fingerprint) are retained on correlated host work via its
existing `transport_queue` snapshot. The next genuine event in the same
conversation carries a frozen snapshot and a readable
`github-held-intents-untrusted-context.json` attachment. Its source address is
shown in the focus input; read it as connector custody facts and untrusted
provider context, then inspect current status before deciding. An attachment or
queue ACK does not prove reconsideration. The terminal author stays terminal;
no provider event or model scheduler is fabricated. Retries preserve provider
text/IDs, the wire payload and attachment bytes. Automatic intents are retained for inspection; there is no automatic rewrite of
their output identity. Replacing an automatic held draft with a new explicit
request is a new authored decision, never an uncertain-send retry.

`queued` is local custody. `held` is not a delivery. Confirmed provider replies
have numeric IDs, readback text, author and route evidence. Current state may
change after the final lookup; a started HTTP effect cannot be recalled.
Timeouts, 5xx and ambiguous mutation/GraphQL responses become `uncertain` and
are never blindly posted again. A successful POST followed by any failed
readback (including 429/403/404) is also uncertain; its candidate response is
retained and cannot authorize reposting or closure. Read-only reconciliation retries failed reads and periodically seeks
the exact marker/body, numeric author and route. A positive match confirms the
part; a negative or interrupted lookup does not authorize reposting. Recovered
parts require deliberate reconsideration before any further part or closure.
One typed provider classification controls both comment and closure refusals.
Known rate refusals retry only the unchanged intent; definite non-rate 4xx
refusals fail visibly. A later exact receipt retains any earlier unknown report
and uses the host's existing `(delivery_id, part_id, state)` identity.

Closure confirms every explanatory comment part before its PATCH, checks current
place/target again, and verifies the resulting issue state. A failed/uncertain
explanation holds closure. An uncertain closure is not repeated; observing an
already closed issue does not prove this intent closed it. This narrow capability
does not decide whether an issue is a duplicate/non-bug or grant authority to act.

Physical receipts commit locally before host reporting. Report ACK failures
retry the immutable report, never the GitHub operation. The status tool can inspect
an exact event or delivery. Ask Ouroboros for GitHub diagnostics: the status tool
lists outstanding IDs/refs, queued allowance work, uncertainty, gaps, quota,
database bytes and heartbeat time. The existing status GET route remains available
for technical inspection and Places. There is no GitHub card on Widgets;
collection runs independently in the companion. No queue expires automatically.
Routine status is read-only and paginated (100 rows
per list); use `status_offset`/`next_offsets` to see every retained obligation.
Exact event/delivery lookup returns the complete retained evidence. State/receipt backups must be taken as a consistent
SQLite backup by the eventual operator; deleting the database destroys custody.

## Returning to an earlier release

0.1.6 reads neither an `enriching` event nor an `enrich` job: left as they are, a
waiting event is never offered and its job fails every minute. Before installing
it over this state:

1. Stop the companion (disable the skill in the host); collection state stays.
2. Run, with the host's state directory for this skill:
   `OUROBOROS_SKILL_STATE_DIR=<state_dir> python3 scripts/companion.py --release-waiting`.
   It takes the companion lock (and refuses while a companion runs), reads nothing
   from GitHub, and completes every waiting event once: what its retries already
   read stays, each read still open becomes an explicit gap beside
   `released_before_retries_finished`. It prints the released event ids.
3. Continue only when it exits 0 with `still_enriching: 0` and
   `enrich_jobs_pending: 0`.

`undelivered_change_observations` counts events in the 0.2.0 change form that 0.1.6
would offer to any host, without checking `presence_change_context`: let a capable
host take them first. 0.1.6 also resumes its separate pull-request details stream,
so each pull request first seen under 0.2.0 yields one details observation there.
Per-message outside snapshots and retained thread openings are ignored by 0.1.6.

## Cutover from an existing watch

No schedule or old state is changed automatically. Before retirement, the
integrating operator must retain a dated handoff containing:

- Old last cursors/coverage intervals and exact source/reply references; a new
  baseline must not consume the uncovered interval silently.
- Every unresolved promise/question, the current owner-authorized next step and
  its ordinary task/followup reference (or an explicit owner decision still owed).
- Every unknown send/closure, exact author/destination, any provider receipt and
  its investigation owner. Unknown effects cannot become new requests.
- New collection comparison, Silent trial results and outstanding connector
  event/ref/delivery IDs. Preserve the actual facts, not only a count.
- The point when future old runs are stopped, the disposition of any admitted
  old writer, and proof there is only one author before Active transition.

The host's shared knowledge and task/followup system own commitments. There is
no private cognitive memory or model cron here. Main reports/questions and their
answer routing require the shared host changes in [HOST_CONTRACT.md](HOST_CONTRACT.md).
Future scheduled roots explicitly use the connector send tool. These live
consumer checks and the cutover remain the parent's responsibility.
