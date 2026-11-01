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
Optional `title` is the human name the manifest gives a skill (for example
`Email tools`); the canonical name stays its identity. A connector needs none:
its place `label` already names the service.
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

The initial Official set is: `a2a`, `adaptive-code-review`, `anti-slop`,
`cache_efficiency_snapshot`, `claudexor_quotas`, `confluence`, `context-lens`,
`duckduckgo`, `email`, `email-presence`, `github`, `google-workspace`, `keenable`,
`memory-atlas`, `perplexity`, `slack-bridge`, `telegram-bot`, `pdf_tools`, `word_tools`.
The recommended hub starters are `duckduckgo`, `pdf_tools`, `word_tools`,
`anti-slop` and `memory-atlas`. The app manages recommendations for its built-ins.

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
release digest, and manifest-derived `icon`/`connects`/`title` fields. `--digest NAME`
prints the digest without changing files. Neither command renews Official
endorsements. `scripts/build_catalog.py` remains a
compatible alias for the generator and its `--check` mode.

## Update, withdraw or adopt a skill

Change only `entries/<name>.json` and files under `skills/<name>/` for an owner
update. Bump the manifest version when publishing changed payloads: older apps
use it to discover updates. Run `--refresh NAME`, regenerate the catalog locally
and run validation and the relevant skill tests. Commit the entry and payload;
leave the generated catalog to publication if you want owner automerge.

A PR is automatically merged only when it changes one existing entry (plus that
skill's hosted files), its author is the owner recorded on main, the owner stays
unchanged, the name has no Official endorsement, and the aggregate `checks` job
passes. It requires catalog validation and every selected hosted suite to pass;
unaffected suites may be skipped. The merge uses the exact validated head SHA. New names, ownership transfers,
Official changes, workflow changes and changes outside that skill need maintainer
review.

Validation runs on `pull_request` with read permissions and writes a small verdict
artifact. The privileged `workflow_run` job executes helpers from main only. It
reads the artifact as JSON, rechecks the PR, files, owner and endorsements through
the API, and merges by SHA. It never checks out or executes PR code with its write
token.

If validation needs to be retried, rerun all jobs so the verdict and green checks
belong to the same run attempt.

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

After a main push or an owner automerge, **Sync Joi Lab Mirror** refreshes legacy
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

Telegram's opt-in real-host/browser qualification lives in
[`tests/telegram_group_discovery`](tests/telegram_group_discovery/README.md).
Set `TG_DISCOVERY_CORE`, `TG_DISCOVERY_VENV` and `PLAYWRIGHT_BROWSERS_PATH`, then
run its `run.sh` with an evidence directory. It checks the existing Places picker
against synthetic Bot API updates and an upgrade from 0.8, without live credentials.

The selected `calendar-skill` and `email-presence` CI jobs check out the exact core
pinned in `catalog-check.yml`, install its Python runtime requirements and
Chromium/WebKit, and exercise the real Host Service and widget bridge with fake
providers. Missing core or browsers fail those jobs. For local backend-only
calendar checks, which do not qualify its widget or sign-in return:

```bash
python -m pip install icalendar python-dateutil cryptography pytest
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q skills/calendar/tests --ignore=skills/calendar/tests/test_widget_browser.py
```

For the complete selected consumers, use Python 3.10 with the pinned core's
requirements and browsers installed, set `OUROBOROS_SOURCE_DIR` and
`OUROBOROS_THEME_CORE_ROOT` to that checkout, then run
`python -B scripts/ci_checks.py run email-presence` and
`python -B scripts/ci_checks.py run calendar-skill` separately. The mail runner
enables its browser cases and real child-tool/Place-binding checks.

Memory Atlas keeps its tests outside the payload, in `tests/memory_atlas`. Most of them start one
isolated Ouroboros from the core checkout named by `ATLAS_CORE`, which must provide PluginAPI 2.1
`read_memory` (`ouroboros/memory_reader.py`): they copy it into a new directory under `/tmp`, write
synthetic memory there through Ouroboros's own producers, install the payload into that data root
only and drive the card in Chromium and WebKit. The test runner's Python needs the core's runtime
requirements, which include Playwright, and Playwright's Chromium and WebKit. Nothing in this suite
is skipped: without `ATLAS_CORE`, a compatible core, Playwright or starlette, a run that selects
these tests stops before the first one and says what is missing. CI runs the whole suite on macOS
against the `razzant/ouroboros` commit pinned in `.github/workflows/catalog-check.yml`. Locally:

```bash
ATLAS_CORE=/path/to/ouroboros PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q -rs tests/memory_atlas
```

`python tests/memory_atlas/dev/realhost.py --core /path/to/ouroboros --root /tmp/<new directory>`
starts the same host to look at and prints `READY <url>`; open `<url>/#widgets` and press Start on
Memory Atlas.

The hub tests include mocked GitHub APIs for owner authorization, signals and
catalog-to-mirror sequencing. The release-identity test compares all hosted skills
with the core checkout selected by `OUROBOROS_IDENTITY_CORE_ROOT`, using the test
runner's Python with core requirements installed. Without that variable the
cross-repository check is reported as skipped. Every hosted skill that contributes
a place is checked as a connector: declared tool effects, the place contract,
wake facts, its status route and a bounded monochrome `ui/icon.svg`. The hub keeps
a mirror of the core rules for this; the same checkout also runs every connector
through the core's manifest, contribution and icon validators.

Module widgets can declare `render.appearance: host` and feature-detect
`OuroborosWidget.onTheme`. Preserve state while applying Light/Dark, unsubscribe
on disposal, and retain a usable palette on older hosts. The real-bridge
browser suites require a local core checkout with git history, Playwright,
Chromium and WebKit:

```bash
OUROBOROS_THEME_CORE_ROOT=/path/to/ouroboros python -m pytest -p no:cacheprovider -q tests/test_dashboard_theme_browser.py tests/test_calendar_host_browser.py tests/test_smart_lists_browser.py
```

These standalone invocations are local opt-ins; Calendar's selected CI job requires
its browser suite. Smart Lists' optional manifest probe separately uses
`OUROBOROS_CORE_ROOT`. Skips in optional suites do not qualify missing consumers.

## License

Skills should declare their own license in `SKILL.md` when relevant. OuroborosHub
is maintained by Joi Lab. See also [Ouroboros Desktop](https://github.com/razzant/ouroboros)
and the [original Ouroboros](https://github.com/joi-lab/ouroboros).
