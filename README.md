# OuroborosHub

OuroborosHub is a skill source for [Ouroboros Desktop](https://github.com/razzant/ouroboros).
Skills add tools, widgets, scripts and task-specific instructions. Their payloads
remain under `skills/<name>/`.

The app presents this repository as two sources: **Ouroboros** contains Official
releases and defaults to Trusted; **Ouroboros Community** contains the other releases
and defaults to Review each. Each source has its own switch. Built-in skills ship
with the app and use a separate source. The hub's `telegram-bot` is a public bot for
presences and `telegram-user-session` is a dedicated ordinary account for the same purpose;
the built-in `telegram` is the owner's connection to their own Ouroboros.

## Repository format

| File | Purpose |
| --- | --- |
| [`hub.json`](hub.json) | Hub format version, name and maintainer. |
| [`entries/`](entries/) | Canonical release metadata, one JSON file per skill. |
| [`official.json`](official.json) | Maintainer endorsements of exact release digests and recommended starter choices. |
| [`categories.json`](categories.json) | Fixed category IDs and English labels. |
| [`skills/`](skills/) | Hosted payloads, including retained copies for old clients. |
| [`catalog.json`](catalog.json) | Generated v1 view for older app versions. |
| `signals.json` on branch `signals` | Daily community votes, comment counts and author-repository stars. |

An entry names its GitHub owner, a version, a description, one or two categories,
a source, a release digest and a withdrawal status. Optional `withdrawn_releases`
records withdrawn versions by digest. Optional `icon` names a host icon or a
local `ui/*.svg` geometry asset. `connects` is derived from `provides: place`
contributions in the reviewed manifest; older consumers can ignore both fields.
Optional `title` is the human name of a skill (for example `Email tools`); it is
seeded from the manifest and may be reworded for people; the canonical name stays
its identity. Optional `pitch` is a one-line description of what the skill does,
`needs` says in a few words what it needs (empty when nothing) and `mark` is that
need in two or three words (`Gemini key`); the setup wizard's Skills step in
Ouroboros shows them when present. A connector needs no separate manifest title:
its place `label` names the service. Its catalog entry can still supply setup wording.
Connector marks are monochrome `currentColor` geometry: the Slack and GitHub
marks use Lucide geometry (ISC License) and the Telegram mark uses Tabler Icons
geometry (MIT License); product names and marks remain the property of their
owners. Optional `when_to_use` supplies additional discovery text. `install_specs` is
generated from the manifest's first
nonempty `install_specs`, `install` or `dependencies` value, without normalization.

A source can be hosted here:

```json
{"kind": "hosted", "path": "skills/example_skill"}
```

Or it can point to an author's repository at an exact 40-character commit SHA:

```json
{"kind": "git", "repo": "https://github.com/author/skills", "path": "example_skill", "commit": "0123456789abcdef0123456789abcdef01234567"}
```

Use `"path": ""` for a skill at the repository root. The validator fetches the
pinned commit and verifies its payload without executing it. An unavailable
upstream prevents new installs and validation of that release; it does not erase
an already installed copy. Hosted storage remains available to authors.

## Official and recommended releases

**Official** means the maintainer vouches for this exact release. An endorsement
binds a name to its digest. Updating the payload or dependency declaration changes
the digest; CI requires the maintainer to renew or remove the old endorsement.
Membership in the hub alone does not confer Official status.

The current Official set and recommended starter choices live in
[`official.json`](official.json). Recommended hub starters are `anti-slop`,
`chrome_browser`, `duckduckgo`, `keenable`, `memory-atlas`, `office_reader`,
`pdf_tools`, `table_tools` and `word_tools`. The app manages recommendations for
its built-ins. An Official release without `recommended: true` remains available
for manual selection. Completed starter setup does not rerun when recommendations change;
an unfinished default starter may use the current recommendations when retried.

Source trust controls the app's acceptance flow. Dependencies, owner grants,
runtime permissions and enablement still apply. Ouroboros can install and review
skills through the app's ordinary lifecycle. Skills must not manufacture review,
grant or enablement records.

## Categories

Choose one or two IDs from [`categories.json`](categories.json):

| ID | Label |
| --- | --- |
| `messaging` | Messaging |
| `documents` | Documents |
| `search` | Search & research |
| `media` | Images, video & music |
| `planning` | Planning |
| `ouroboros` | About Ouroboros |
| `health` | Health |
| `city` | City & services |
| `writing` | Writing & review |
| `business` | Business & finance |
| `computer` | Computer control |
| `games` | Games |

## Add a skill

Start with the core [author guide](https://github.com/razzant/ouroboros/blob/ouroboros/docs/CREATING_SKILLS.md)
and [review checklist](https://github.com/razzant/ouroboros/blob/ouroboros/docs/CHECKLISTS.md#skill-review-checklist).
Keep permissions and dependency declarations explicit. Do not vendor credentials
or generated dependency/cache directories. Payload assets are included in release
identity along with code and instructions.

1. Add `skills/<name>/SKILL.md` and the payload, or prepare an exact git source pin.
2. Add `entries/<name>.json`, using an existing entry as a template. Set the GitHub
   `owner`, description, category and source. Use `"withdrawn": null`. Keep the
   canonical name stable; version strings are not restricted to SemVer.
3. Generate its manifest fields and digest, then validate the catalog:

   ```bash
   python -m pip install PyYAML pytest httpx starlette
   python scripts/build_index.py --refresh example_skill
   python scripts/build_index.py
   python scripts/build_index.py --check
   python -m unittest discover -s tests -v
   ```

4. Submit the entry and payload in a PR. New names require maintainer review.
   `catalog.json` is generated during validation and publication; it need not be
   included in an entry PR.

`--refresh` updates only the named entry's version, raw dependency declaration,
release digest, and manifest-derived `icon` and `connects`. It keeps existing
`title`, `pitch`, `needs` and `mark`, seeding `title` from the manifest only
if absent. The v1 catalog still uses the manifest title. `--digest NAME`
prints the digest without changing files. Neither command renews Official
endorsements. `scripts/build_catalog.py` remains a
compatible alias for the generator and its `--check` mode.

## Update, withdraw or adopt a skill

Change only `entries/<name>.json` and files under `skills/<name>/` for an owner
update. Bump the manifest version when publishing changed payloads: older apps
use it to discover updates. Run `--refresh NAME`, regenerate the catalog locally
and run validation and the relevant skill tests. Commit the entry and payload;
leave the generated catalog to publication if you want owner automerge.

**Automatic Community publication.** A PR is merged automatically only when, between
its head and its merge base with main, it modifies one existing entry and changes
nothing outside that entry and `skills/<name>/` (regular files only); its author is
the owner recorded on main, the owner stays unchanged, the name has no Official
endorsement on main, and the aggregate `checks` job passes. Independent maintainer
review is not required on that path, and merging does not grant Official
endorsement. New names, ownership transfers, Official changes, workflow changes and
changes outside that skill need maintainer review. The merge is an ordinary merge
commit at the exact validated head, so the contribution's commits, including
intermediate ones, enter history; the checks inspect the final trees, not every
historical commit.

Validation runs on `pull_request` with read permissions and checks out one tested
commit, GitHub's merge of the head into main. After `checks` passes, a `receipt` job
on a fresh runner records that run attempt's tested commit from GitHub's contexts as
`owner-update-receipt-<run>-<attempt>`. The privileged **Owner Updates** job runs
helpers from main only. It fetches main, `refs/pull/<n>/head` and the tested commit
as Git objects and never checks out or executes PR files. It requires the tested
commit to merge exactly this head into main, with the same skill files as the head
and the same files outside `entries/`, `skills/` and `catalog.json` as current main.
The selected entry and payload must be unchanged on main since the branch point;
otherwise update the branch. Other skills may change on main meanwhile. That one
attempt's own job list must show `catalog`, `checks`, `receipt` and every hosted
suite `scripts/ci_checks.py` selects for the change as run and passed; a partial
rerun, a superseded attempt, a moved head or a moved main refuses. Rerunning all
jobs repeats the same tested merge. It helps after a moved main only while main
kept the CI control files and this skill; otherwise push, use Update branch, or
convert to Draft and back to Ready, which tests a new merge.

Before merging, each merge attempt writes its own `github-actions[bot]` comment on
the PR with this notice and an `attempting` marker naming the head and both runs,
then rereads the PR, main and the validation run attempt and merges. Later it updates
only that comment, so a delayed write cannot erase another attempt's record. It never
resubmits a merge whose outcome is unknown. After merging it reads the merge commit's parents and says the
contribution's commits were kept only once that readback succeeds; if main had
moved, it rechecks this skill and its policy on the actual parent. Any mismatch or
failed readback reports `merged; post-merge verification failed`, without reverting.

**Hold.** Adding the `owner-update-hold` label (and converting to Draft) is an
immediate stop request, not yet a confirmation. Run **Owner Updates** from main
with `workflow_dispatch` and the PR number: queued with merges, it sets the label if
needed, reads the PR open and unmerged, and records the hold in a new bot comment
with its run. A merge job checks the label on entry and before merging; marking the
PR Ready does not clear it. To release, remove the label, then validate again as
above.

**Unknown outcome.** While any attempt's comment shows `attempting`, or an
owner-update bot comment is unreadable, no later merge proceeds and no hold is
confirmed. Read back the named Owner Updates run and the PR. A Hold run dispatched
for a merged PR records the merge as observed; nothing else is guaranteed to. If
that merge landed after **Sync Joi Lab Mirror** had read main, run Sync from main
with `workflow_dispatch`. To authorize a new attempt anyway, edit that attempt's
comment: keep its text, change only `"phase":"attempting"` to
`"phase":"acknowledged_unknown"` in the control line, and add a line
`Maintainer acknowledgement (YYYY-MM-DD): <reason>`. This accepts the risk for a new
merge attempt only. It does not prove the earlier request had no effect: the comment
stays, and Hold stays unconfirmed (the label still stops new merges) until a merge
is observed.

To withdraw the entire entry from new installs, set `withdrawn` to a reason and date:

```json
{"reason": "Superseded by another skill", "at": "2026-10-17"}
```

To withdraw one older version while keeping the current release available, add
`withdrawn_releases` to the entry:

```json
{"withdrawn_releases": [{"digest": "<64 lowercase hex digits of the withdrawn release>", "version": "0.10.0", "reason": "Incorrect results"}]}
```

Digests must be unique and cannot equal the entry's current digest; each reason
must be nonempty. Version labels are strings, not ordered SemVer. Holders of that
exact digest see the withdrawal and can remove it. To offer a previous release,
revert the entry and its hosted payload (or git commit pin) to that release, then
record the bad digest in `withdrawn_releases`. Renew an Official endorsement if
needed. The app offers the reverted release by its different digest. Withdrawal
records stay in the v2 entries read by the app; the v1 catalog remains unchanged
by those records.

Keep legacy hosted payloads when converting a hosted entry to a git pointer or
removing it from discovery. An adoption or pointer conversion requires verified
authority and comparison of the actual release bytes and declarations. A changed
release follows the ordinary acceptance process. This migration moves no skill
out of the public hub.

## Older app versions and publication

Older apps keep reading the same v1 `catalog.json` URL and downloading
`skills/<name>/` files with per-file SHA-256 checks. All 50 existing names remain
available, including retained payloads if a v2 entry is later withdrawn or becomes
a pointer. New pointer-only skills can be v2-only.
Older apps retain their historical hub badge; the Official/Community split is
available in newer clients.

The generator sorts rows by slug and emits deterministic UTF-8 JSON. In a clean
checkout with full history it pins the raw download base to the last commit that
changed `skills/`. That pin stays stable across generated-catalog commits and
keeps old catalogs usable while new metadata is being published. Uncommitted
local previews or exported trees without git history use `main`; publication
always uses a clean checkout with full history.

After a main push or any finished Owner Updates run, **Sync Joi Lab Mirror** refreshes legacy
hosted entries, validates and regenerates the v1 catalog, publishes the generated
commit to main, then mirrors it to `joi-lab/OuroborosHub`. It does not depend on a
`GITHUB_TOKEN` push starting another workflow. When `CATALOG_DEPLOY_KEY` is set,
canonical publication uses SSH with that write-enabled repository deploy key;
otherwise it uses `GITHUB_TOKEN`. The key exists only in a mode-600 file under
the runner's temporary directory during the push. GitHub's published Ed25519 host
key is pinned in `known_hosts`; no live key scan is trusted. Mirroring uses the
existing `JOILAB_HUB_MIRROR_PAT`. Neither push is forced.

Before protecting main, configure `CATALOG_DEPLOY_KEY` and list **Deploy keys** as
a bypass actor in the main ruleset so this generated publication can land.
Use that key only for catalog publication; nothing else bypasses the required PR
and green `checks` job. `GITHUB_TOKEN` cannot bypass a ruleset. The workflow does
not change repository settings. A deploy-key push can trigger another sync run;
unchanged generated files produce no further commit. If the catalog push is
refused, the workflow still mirrors the original checked-out main commit and
reports failure; it leaves the unpublished generated commit out of the mirror so
a later retry can fast-forward. Old clients receive the refreshed catalog after
canonical publication succeeds.

Older publishers can still submit payload and whole-catalog PRs. Before validation,
CI refreshes the changed hosted skill's version, declaration and digest when its
entry was not edited. Publication repeats that refresh on main and commits the
refreshed entries together with the regenerated catalog. These PRs require a
maintainer merge. Official endorsements are never refreshed automatically: an
Official payload change still fails until the maintainer renews its endorsement.

## Community signals

**Skill Signals** runs daily and can be dispatched manually. When Discussions are
enabled and a category named **Skills** exists, it creates missing topics titled
with each skill's name and reuses existing topics. GitHub users supply upvotes on
those topics. The snapshot records GraphQL `upvoteCount`, comment counts and
Discussion links; git entries also receive their author repository's star count
when available. If duplicate topics already exist, it consistently uses the oldest.

The workflow publishes `signals.json` only to branch `signals`, creating it when
needed. With Discussions disabled or the category missing, it writes an empty
`skills` map and succeeds. An unavailable count is omitted, not reported as zero.
It does not enable Discussions or create categories. Signals are informational;
they do not confer Official status or grant execution authority.

## Tests and widgets

CI always validates the catalog and maintenance workflows. On PRs, each hosted
suite runs only when its skill, tests or shared dependencies change; paths and
dependencies live in `scripts/ci_checks.py`. Main pushes and manual runs select
every suite. Require the single `checks` job in branch protection: it fails on any
failed or cancelled dependency and accepts skipped suites. Slack and Telegram run
in separate jobs so their `test_retained_custody.py` modules and sibling-test
imports can coexist:

```bash
python -m pip install pytest httpx starlette websockets
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q -rs skills/slack-bridge/tests
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q -rs skills/telegram-bot/tests
```

Every suite whose tests use a real core (`core_env` in `scripts/ci_checks.py`: Memory Atlas,
mail, Calendar, Smart Lists, the dashboard widgets, GitHub, the phone line and the Presence consumers of
Freshdesk, Slack and Telegram) runs against one `razzant/ouroboros` commit, `OUROBOROS_CORE_SHA`
at the top of `.github/workflows/catalog-check.yml`, checked out once per job; the catalog job
checks every hosted release and connector against the same commit. With one pin every seam
between a hosted skill and the core is tested on the same reproducible core, and no suite stays
behind on an older one; `tests/test_workflows.py` fails on a second pin in any `.yml` or `.yaml`
workflow, whether a checkout or a run step's git command takes it. That check is a lint against an
accidental second pin, not a boundary: a step that moves the checked-out core with a git command run
from `working-directory: core` or after a shell `cd` is not seen by it. To move the pin, open a PR that
changes only that value. The workflow is shared, so that PR runs every suite on the new core, and it
merges when all of them pass. A suite that fails there is fixed in the hub when the skill or its test
is wrong against the core's documented contract, and reported to the core when the core broke it.

`scripts/ci_checks.py run <suite>` refuses a suite whose core is not named, and puts that core first
on its tests' `PYTHONPATH`, ahead of anything inherited, so the skill tests that check their wire
bodies with the core's own validators run them. The Slack and Telegram bridge suites also list the
core functions those tests import (`core_imports`): a named core without one, or an import that
resolves outside the named checkout, stops the suite instead of letting the check skip. Locally each
variable a suite names is checked on its own, so two of them (Telegram User Session's Presence and
theme roots, or Memory Atlas's `ATLAS_CORE` beside `ATLAS_HOST_ROOT`) can name different checkouts;
CI names one checkout per job.

The `claudexor-quotas` suite needs Node 24, pytest, Playwright and its Chromium
and WebKit builds. `OUROBOROSHUB_NODE` selects an existing Node executable;
`PLAYWRIGHT_BROWSERS_PATH` selects the browser cache. Run with
`QUOTAS_REQUIRE_RUNTIME=1 python -m pytest skills/claudexor_quotas` to fail on
missing runtimes. `QUOTAS_RU_JSON` optionally supplies an installation-generated
Russian translation memory for additional checks; CI covers translation behavior
with synthetic memory, and skips only those optional memory-file checks.

Telegram's opt-in real-host/browser qualification lives in
[`tests/telegram_group_discovery`](tests/telegram_group_discovery/README.md).
Set `TG_DISCOVERY_CORE`, `TG_DISCOVERY_VENV` and `PLAYWRIGHT_BROWSERS_PATH`, then
run its `run.sh` with an evidence directory. It checks the existing Places picker
against synthetic Bot API updates and an upgrade from 0.8, without live credentials.

The selected `calendar-skill`, `email-presence` and `smart-lists` CI jobs install the
pinned core's Python runtime requirements and Chromium/WebKit. Calendar and mail
exercise the real Host Service and widget bridge with fake providers. Smart Lists
mounts its widget through that core's real
module bridge (`widget_module.js`), served by a test HTTP server over the skill's
routes registered on `FakePluginAPI`, without the Host Service. A missing core fails
those jobs; so do missing Playwright or browsers (Smart Lists: `SMART_LISTS_REQUIRE_BROWSERS`,
set by `scripts/ci_checks.py`). For local backend-only
calendar checks, which do not qualify its widget or sign-in return:

```bash
python -m pip install icalendar python-dateutil cryptography pytest
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q skills/calendar/tests --ignore=skills/calendar/tests/test_widget_browser.py
```

For each complete consumer suite, use Python 3.10 with the requirements and browsers
from the pinned core. Set `OUROBOROS_SOURCE_DIR` and
`OUROBOROS_THEME_CORE_ROOT` to that checkout, then run
`python -B scripts/ci_checks.py run email-presence` and
`python -B scripts/ci_checks.py run calendar-skill` separately. The mail runner
enables its browser cases and real child-tool/Place-binding checks.

Chrome (`chrome_browser`) keeps its tests outside the payload, in
[`tests/chrome_browser`](tests/chrome_browser/README.md). They need no browser, but they need Node 24 and
the exact npm packages `skills/chrome_browser/pins.json` records, named by `OUROBOROS_CHROME_NODE_MODULES`.
Nothing there skips: without them, a run says what to install.

Memory Atlas keeps its tests outside the payload, in `tests/memory_atlas`. Most of them start one
isolated Ouroboros from the core checkout named by `ATLAS_CORE`, which must provide PluginAPI 2.1
`read_memory` (`ouroboros/memory_reader.py`) and the transfer contract (`source_author`, transfer metadata,
`period_bases`); transfer tests fail preflight against an earlier core. They copy it into a new directory under `/tmp`, write
synthetic memory there through Ouroboros's own producers, install the payload into that data root
only and drive the card in Chromium and WebKit. The test runner's Python needs the core's runtime
requirements, which include Playwright, and Playwright's Chromium and WebKit. The pure formatter tests
need Node 20 or newer on PATH (CI uses Node 24). Nothing in this suite
is skipped: without `ATLAS_CORE`, a compatible core, Node, Playwright or starlette, a run that selects
these tests stops before the first one and says what is missing. CI runs the whole suite on macOS
against the `razzant/ouroboros` commit pinned in `.github/workflows/catalog-check.yml`. Run the owning
suite on a dedicated test machine or VM: it starts a real core host. On that test machine:

```bash
ATLAS_CORE=/path/to/ouroboros PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q -rs tests/memory_atlas
```

`python -B scripts/ci_checks.py run memory-atlas` takes either `ATLAS_CORE` or `ATLAS_HOST_ROOT`
(a host already started by `realhost.py`, whose copy of its core is `repo/`).

`python tests/memory_atlas/dev/realhost.py --core /path/to/ouroboros --root /tmp/<new directory>`
starts the same host to look at and prints `READY <url>`; open `<url>/#widgets` and press Start on
Memory Atlas.

The `telephony` job runs the phone line's call bridge against the real live-call doors (`LiveCallHub` and
the peer socket pump) of the one core pinned in `catalog-check.yml`, named for it by `TELEPHONY_CORE_REPO`.
`TELEPHONY_REQUIRE_CORE=1` turns a missing core, or a missing starlette, uvicorn or websockets, into a
failure instead of a skip. Locally:

```bash
python -m pip install pytest starlette uvicorn websockets
TELEPHONY_CORE_REPO=/path/to/ouroboros TELEPHONY_REQUIRE_CORE=1 PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q -rs skills/telephony/tests
```

The hub tests include mocked GitHub APIs for owner authorization, signals and
catalog-to-mirror sequencing. The release-identity test compares all hosted skills
with the core checkout selected by `OUROBOROS_IDENTITY_CORE_ROOT`; it imports only
core modules, which need nothing beyond PyYAML. CI's catalog job sets it to the
pinned core. Without that variable the cross-repository check is reported as
skipped. Every hosted skill that contributes
a place is checked as a connector: declared tool effects, the place contract,
wake facts, its status route and a bounded monochrome `ui/icon.svg`. The hub keeps
a mirror of the core rules for this; the same checkout also runs every connector
through the core's manifest, contribution and icon validators.

Module widgets can declare `render.appearance: host` and feature-detect
`OuroborosWidget.onTheme`. Preserve state while applying Light/Dark, unsubscribe
on disposal, and retain a usable palette on older hosts. The real-bridge
browser suites require a local core checkout with git history, Playwright,
Chromium and WebKit. CI gives the `widgets`, `calendar-skill`, `smart-lists` and
`telegram-user-session` jobs the pinned core, its runtime and both browsers; only
the dashboard suite (`widgets`) reads the core's history:

```bash
OUROBOROS_THEME_CORE_ROOT=/path/to/ouroboros python -m pytest -p no:cacheprovider -q tests/test_dashboard_theme_browser.py tests/test_calendar_host_browser.py tests/test_smart_lists_browser.py tests/test_telegram_participant_widget_browser.py
```

These standalone invocations are local opt-ins; the selected Calendar and Smart Lists
CI jobs require their browser suites. Smart Lists' optional manifest probe separately uses
`OUROBOROS_CORE_ROOT`. Skips in optional suites do not qualify missing consumers.

## License

Skills should declare their own license in `SKILL.md` when relevant. OuroborosHub
is maintained by Joi Lab. See also [Ouroboros Desktop](https://github.com/razzant/ouroboros)
and the [original Ouroboros](https://github.com/joi-lab/ouroboros).
